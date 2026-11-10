"""Shared helpers: config, logging, failed.tsv, lock file, archive.db schema, chat anchors."""
from __future__ import annotations

import bisect
import csv
import fcntl
import logging
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("VOD_ARCHIVE_ROOT", Path(__file__).resolve().parent.parent))
# Tools installed in the virtualenv (yt-dlp) must be found even when it isn't activated (systemd, cron).
_BIN = str(Path(sys.executable).parent)
if _BIN not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _BIN + os.pathsep + os.environ.get("PATH", "")
SPEC_VERSION = "4"

ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos(
  id TEXT PRIMARY KEY, platform TEXT, source_url TEXT, title TEXT,
  upload_date TEXT,
  twitch_vod_id TEXT,
  stream_start_unix INTEGER,
  stream_start_source TEXT,
  duration INTEGER,
  embed_type TEXT,
  embed_id TEXT,
  chat_source TEXT,
  transcript_hash TEXT,
  chat_sig TEXT,                 -- signature of the chat inputs last loaded (see NOTES.md)
  processed_at TEXT);

CREATE TABLE IF NOT EXISTS chat_anchors(
  video_id TEXT, transcript_t REAL, offset REAL,
  confidence REAL,
  method TEXT,
  PRIMARY KEY(video_id, transcript_t));

CREATE TABLE IF NOT EXISTS muted(video_id TEXT, start REAL, end REAL);
CREATE INDEX IF NOT EXISTS idx_muted_vid ON muted(video_id);

CREATE TABLE IF NOT EXISTS turns(
  id INTEGER PRIMARY KEY, video_id TEXT, start REAL, end REAL,
  speaker TEXT, speaker_raw TEXT, speaker_score REAL,
  is_media INTEGER DEFAULT 0, text TEXT);
CREATE INDEX IF NOT EXISTS idx_turns_vid ON turns(video_id, start);
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(text, speaker, content='turns', content_rowid='id');

CREATE TABLE IF NOT EXISTS chat(
  id INTEGER PRIMARY KEY, video_id TEXT, t REAL,
  user_id TEXT, author TEXT, text TEXT,
  deleted INTEGER DEFAULT 0, kind TEXT DEFAULT 'msg',
  badges TEXT, first_msg INTEGER, returning_chatter INTEGER, subscriber INTEGER,
  source TEXT);
CREATE INDEX IF NOT EXISTS idx_chat_vid_t ON chat(video_id, t);
CREATE INDEX IF NOT EXISTS idx_chat_author ON chat(author);
CREATE INDEX IF NOT EXISTS idx_chat_user ON chat(user_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chat_fts USING fts5(text, author, content='chat', content_rowid='id');

CREATE TABLE IF NOT EXISTS speakers(name TEXT PRIMARY KEY, n_clips INTEGER, updated_at TEXT);

CREATE TABLE IF NOT EXISTS live_msgs(
  ts_ms INTEGER, channel TEXT, room_id TEXT, msg_id TEXT, user_id TEXT, author TEXT, text TEXT,
  badges TEXT, first_msg INTEGER, returning_chatter INTEGER, subscriber INTEGER,
  deleted INTEGER DEFAULT 0, kind TEXT DEFAULT 'msg');
CREATE INDEX IF NOT EXISTS idx_live_ts ON live_msgs(channel, ts_ms);
CREATE INDEX IF NOT EXISTS idx_live_id ON live_msgs(msg_id);

CREATE TABLE IF NOT EXISTS sessions(channel TEXT, start_ms INTEGER, end_ms INTEGER);
CREATE INDEX IF NOT EXISTS idx_sessions ON sessions(channel, start_ms);
"""


def clean_hf_token() -> None:
    """A token pasted with a trailing space or newline makes every Hugging Face request fail with
    'Invalid leading whitespace ... in header value' (seen on Colab); strip it once for every script."""
    tok = os.environ.get("HF_TOKEN")
    if tok is not None and tok != tok.strip():
        os.environ["HF_TOKEN"] = tok.strip()


def load_config(path: str | os.PathLike | None = None) -> dict:
    clean_hf_token()
    path = Path(path or os.environ.get("VOD_ARCHIVE_CONFIG", ROOT / "config.yaml"))
    with open(path) as f:
        cfg = yaml.safe_load(f)
    cfg["_root"] = str(path.resolve().parent)
    return merge_private(cfg)


def merge_private(cfg: dict) -> dict:
    """Overlay <private_dir>/config.yaml (site-specific settings kept out of the public repo).
    Relative file settings in the overlay (optout_file) resolve against the private dir."""
    pdir = os.environ.get("VOD_ARCHIVE_PRIVATE_DIR") or cfg.get("private_dir")
    if not pdir:
        return cfg
    pdir = Path(pdir)
    if not pdir.is_absolute():
        pdir = Path(cfg["_root"]) / pdir
    overlay_path = pdir / "config.yaml"
    if not overlay_path.is_file():
        return cfg
    overlay = yaml.safe_load(overlay_path.read_text()) or {}
    for key, value in overlay.items():
        if key == "paths" and isinstance(value, dict):
            cfg["paths"] = {**cfg.get("paths", {}), **value}
        elif key == "optout_file" and not Path(value).is_absolute():
            cfg[key] = str(pdir / value)
        else:
            cfg[key] = value
    cfg["_private"] = str(pdir.resolve())
    return cfg


def P(cfg: dict, key: str) -> Path:
    """Resolve a configured path (paths.<key>) against the config's directory."""
    p = Path(cfg["paths"][key])
    return p if p.is_absolute() else Path(cfg["_root"]) / p


def cfg_file(cfg: dict, name: str) -> Path:
    p = Path(cfg[name])
    return p if p.is_absolute() else Path(cfg["_root"]) / p


def make_dirs(cfg: dict) -> None:
    for key in ("audio", "chat", "transcripts", "muted", "index", "voices", "voiceprints", "logs", "qa"):
        P(cfg, key).mkdir(parents=True, exist_ok=True)


def get_logger(name: str, cfg: dict | None = None) -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


def log_failure(cfg: dict, video_id: str, step: str, error) -> None:
    """Append to logs/failed.tsv (id, step, error, timestamp). Never raises."""
    try:
        path = P(cfg, "logs") / "failed.tsv"
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with open(path, "a", newline="") as f:
            w = csv.writer(f, delimiter="\t")
            if new:
                w.writerow(["id", "step", "error", "timestamp"])
            msg = " ".join(str(error).split())[:500]
            w.writerow([video_id, step, msg, time.strftime("%Y-%m-%dT%H:%M:%S%z")])
    except Exception:  # pragma: no cover
        pass


@contextmanager
def lock(cfg: dict, name: str):
    """Exclusive non-blocking lock; exits quietly if another run holds it."""
    path = P(cfg, "logs") / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"{name}: another run holds {path}; exiting", file=sys.stderr)
        sys.exit(0)
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def connect_archive(cfg: dict, init: bool = True) -> sqlite3.Connection:
    P(cfg, "archive_db").parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(P(cfg, "archive_db"), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    if init:
        con.executescript(ARCHIVE_SCHEMA)
        con.commit()
    return con


def read_tsv(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict], fields: list[str]) -> None:
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def parse_ts(text: str) -> float:
    parts = [float(x) for x in str(text).split(":")]
    out = 0.0
    for p in parts:
        out = out * 60 + p
    return out


def site_url(cfg: dict) -> str:
    """Base URL for links printed by the CLI tools."""
    d = cfg.get("site_domain") or "auto"
    return "http://localhost:8000/" if d == "auto" else f"https://{d}/"


def watch_link(video_id: str, t: float) -> str:
    return f"watch.html?v={video_id}&t={int(t)}"


class Anchors:
    """Piecewise chat<->transcript clock mapping for one video.

    chat_t = transcript_t + offset, using the latest anchor with transcript_t <= t.
    With no anchors the two clocks are treated as identical.
    """

    def __init__(self, rows):
        rows = sorted((float(t), float(o)) for t, o in rows)
        self.ts = [r[0] for r in rows]
        self.offs = [r[1] for r in rows]
        # chat-clock start of each anchor segment
        self.cstarts = [t + o for t, o in rows]

    @classmethod
    def load(cls, con: sqlite3.Connection, video_id: str) -> "Anchors":
        return cls(con.execute(
            "SELECT transcript_t, offset FROM chat_anchors WHERE video_id=? ORDER BY transcript_t",
            (video_id,)).fetchall())

    def to_chat(self, t: float) -> float:
        if not self.ts:
            return t
        i = max(bisect.bisect_right(self.ts, t) - 1, 0)
        return t + self.offs[i]

    def to_transcript(self, c: float):
        """Chat-clock time -> transcript time, or None if it falls in a cut (not in the audio)."""
        if not self.ts:
            return c
        # segments are ordered on both clocks unless offsets decrease sharply; scan from the end
        for i in range(len(self.ts) - 1, -1, -1):
            t = c - self.offs[i]
            lo = self.ts[i] if i > 0 else float("-inf")
            hi = self.ts[i + 1] if i + 1 < len(self.ts) else float("inf")
            if lo <= t < hi:
                return t
        return None

    def chat_ranges(self, start: float, end: float) -> list[tuple[float, float, float]]:
        """Transcript range -> list of (chat_lo, chat_hi, offset) pieces covering it."""
        if not self.ts:
            return [(start, end, 0.0)]
        out = []
        for i in range(len(self.ts)):
            lo = self.ts[i] if i > 0 else float("-inf")
            hi = self.ts[i + 1] if i + 1 < len(self.ts) else float("inf")
            a, b = max(start, lo), min(end, hi)
            if a < b:
                out.append((a + self.offs[i], b + self.offs[i], self.offs[i]))
        return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Project housekeeping")
    ap.add_argument("--make-dirs", action="store_true")
    ap.add_argument("--init-db", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    if a.make_dirs:
        make_dirs(cfg)
        print("directories created")
    if a.init_db:
        connect_archive(cfg).close()
        print("archive.db initialised")
