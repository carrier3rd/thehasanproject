"""Stream start time + chat anchor map (chat_anchors) for each VOD.

chat_t = transcript_t + offset, using the latest anchor with transcript_t <= t.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from common import P, connect_archive, get_logger, load_config, log_failure, make_dirs

log = get_logger("align_chat")
ENV_HZ = 100          # envelope frames per second
_DECODE_SR = 8000


# ---------- audio envelope + cross-correlation ----------

def envelope(path) -> np.ndarray:
    """Log-energy envelope at ENV_HZ, decoded through ffmpeg in a stream (low memory)."""
    hop = _DECODE_SR // ENV_HZ
    proc = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn",
                             "-ac", "1", "-ar", str(_DECODE_SR), "-f", "s16le", "-"], stdout=subprocess.PIPE)
    out, rest = [], b""
    while True:
        buf = proc.stdout.read(hop * 2 * 6000)
        if not buf:
            break
        buf = rest + buf
        n = len(buf) // (hop * 2) * (hop * 2)
        rest = buf[n:]
        x = np.frombuffer(buf[:n], dtype="<i2").astype(np.float32).reshape(-1, hop)
        out.append(np.log1p(np.sqrt((x * x).mean(axis=1))))
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg could not decode {path}")
    return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)


class Matcher:
    """Finds where a short window of `needle_env` occurs in `hay_env` (normalised cross-correlation)."""

    def __init__(self, hay: np.ndarray, window_s: float):
        self.hay = hay.astype(np.float64)
        self.n = int(window_s * ENV_HZ)
        self.size = 1 << int(np.ceil(np.log2(len(hay) + self.n)))
        self.fhay = np.fft.rfft(self.hay, self.size)
        c1 = np.concatenate([[0.0], np.cumsum(self.hay)])
        c2 = np.concatenate([[0.0], np.cumsum(self.hay ** 2)])
        self.valid = len(hay) - self.n + 1
        if self.valid > 0:
            s1 = c1[self.n:] - c1[:-self.n]
            s2 = c2[self.n:] - c2[:-self.n]
            self.local_std = np.sqrt(np.maximum(s2 - s1 ** 2 / self.n, 0.0) / self.n)

    def find(self, needle: np.ndarray) -> tuple[float, float]:
        """-> (position of the needle's first frame in the hay, seconds; confidence 0..1)"""
        if self.valid <= 0 or len(needle) < self.n:
            return 0.0, 0.0
        x = needle[:self.n].astype(np.float64)
        sd = x.std()
        if sd < 1e-3:
            return 0.0, 0.0  # silence: nothing to lock onto
        x = (x - x.mean()) / sd
        corr = np.fft.irfft(self.fhay * np.conj(np.fft.rfft(x, self.size)), self.size)[:self.valid]
        ncc = corr / (self.n * np.maximum(self.local_std, 1e-3))
        ncc[self.local_std < 1e-3] = 0.0
        k = int(np.argmax(ncc))
        p1 = float(ncc[k])
        guard = 2 * ENV_HZ
        rest = ncc.copy()
        rest[max(k - guard, 0):k + guard + 1] = -1.0
        p2 = max(float(rest.max()), 0.0) if len(rest) else 0.0
        conf = 0.0 if p1 <= 0 else float(np.clip(1.0 - p2 / p1, 0.0, 1.0))
        return k / ENV_HZ, conf


def build_anchors(yt_env: np.ndarray, tw_env: np.ndarray, cfg: dict) -> list[tuple[float, float, float]]:
    """-> [(transcript_t, offset, confidence)] from a re-upload envelope and the original's envelope."""
    win = cfg["chat_anchor_window_seconds"]
    step = cfg["chat_anchor_every_minutes"] * 60
    jump = cfg["chat_anchor_max_jump_seconds"]
    dur = len(yt_env) / ENV_HZ
    matcher = Matcher(tw_env, win)

    def measure(t: float) -> tuple[float, float]:
        """Offset for the window centred on t."""
        a = min(max(t - win / 2, 0.0), max(dur - win, 0.0))
        pos, conf = matcher.find(yt_env[int(a * ENV_HZ):int(a * ENV_HZ) + matcher.n])
        return pos - a, conf

    grid = []
    t = 0.0
    while t < max(dur - win / 2, 1.0):
        off, conf = measure(t)
        grid.append((t, off, conf))
        t += step
    good = [g for g in grid if g[2] >= 0.5]
    if not good:
        best = max(grid, key=lambda g: g[2]) if grid else (0.0, 0.0, 0.0)
        return [(0.0, round(best[1], 2), round(best[2], 3))]
    anchors = [(0.0 if good[0][0] <= step else good[0][0], good[0][1], good[0][2])]

    def locate(lo, off_lo, hi, off_hi, conf_hi, depth=0):
        """A cut lies in (lo, hi): bisect, then add an anchor where the offset changes."""
        while hi - lo > cfg["chat_anchor_bisect_seconds"]:
            mid = (lo + hi) / 2
            off, conf = measure(mid)
            if abs(off - off_lo) <= jump:
                lo = mid
            elif abs(off - off_hi) <= jump:
                hi = mid
            elif conf >= 0.5 and depth < 6:   # a third offset: more than one cut in this span
                locate(lo, off_lo, mid, off, conf, depth + 1)
                locate(mid, off, hi, off_hi, conf_hi, depth + 1)
                return
            else:
                conf_hi = min(conf_hi, 0.4)   # could not narrow further; flag for review
                break
        anchors.append(((lo + hi) / 2, off_hi, conf_hi))

    for prev, cur in zip(good, good[1:]):
        if abs(cur[1] - prev[1]) > jump:
            locate(prev[0], prev[1], cur[0], cur[1], cur[2])
        elif abs(cur[1] - anchors[-1][1]) > 0.2:  # slow drift: keep following it
            anchors.append(cur)
    anchors.sort()
    return [(round(t, 2), round(o, 2), round(c, 3)) for t, o, c in anchors]


# ---------- stream start ----------

def stream_start(con, cfg: dict, vid: str, meta: dict, info: dict, row) -> tuple[int | None, str | None]:
    if row and row["stream_start_source"] == "manual":
        return row["stream_start_unix"], "manual"
    if meta.get("platform") == "twitch" and info.get("timestamp"):
        return int(info["timestamp"]), "twitch_info"
    if meta.get("twitch_timestamp"):
        return int(meta["twitch_timestamp"]), "twitch_info"
    date, dur = info.get("upload_date"), info.get("duration")
    if date and dur:
        day = dt.datetime.strptime(date, "%Y%m%d").replace(tzinfo=dt.timezone.utc).timestamp()
        best = None
        for s in con.execute("SELECT start_ms, end_ms FROM sessions WHERE channel=? AND start_ms BETWEEN ? AND ?",
                             (cfg["channel_name"], (day - 86400) * 1000, (day + 2 * 86400) * 1000)):
            diff = abs((s["end_ms"] - s["start_ms"]) / 1000 - dur) / dur
            if diff <= 0.25 and (best is None or diff < best[0]):
                best = (diff, s["start_ms"] // 1000)
        if best:
            return int(best[1]), "live_logger"
    return None, None


# ---------- per-video ----------

def download_twitch_audio(vod_id: str, dest_dir: Path) -> Path:
    out = subprocess.run(["yt-dlp", "-f", "audio_only/bestaudio/worst", "--no-playlist",
                          "-o", str(dest_dir / "tw.%(ext)s"), f"https://www.twitch.tv/videos/{vod_id}"],
                         capture_output=True, text=True)
    files = list(dest_dir.glob("tw.*"))
    if out.returncode != 0 or not files:
        raise RuntimeError(out.stderr.strip()[-300:] or "Twitch audio unavailable")
    return files[0]


def set_anchors(con, vid: str, anchors, method: str):
    con.execute("DELETE FROM chat_anchors WHERE video_id=?", (vid,))
    con.executemany("INSERT INTO chat_anchors(video_id, transcript_t, offset, confidence, method) VALUES(?,?,?,?,?)",
                    [(vid, t, o, c, method) for t, o, c in anchors])


def align_one(con, cfg: dict, vid: str, force: bool = False) -> str:
    audio = P(cfg, "audio")
    meta = json.loads((audio / f"{vid}.meta.json").read_text())
    ip = audio / f"{vid}.info.json"
    info = json.loads(ip.read_text()) if ip.exists() else {}
    con.execute("INSERT OR IGNORE INTO videos(id) VALUES(?)", (vid,))
    row = con.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
    start, source = stream_start(con, cfg, vid, meta, info, row)
    con.execute("UPDATE videos SET stream_start_unix=?, stream_start_source=?, twitch_vod_id=? WHERE id=?",
                (start, source, meta.get("twitch_vod_id"), vid))
    existing = con.execute("SELECT method, confidence FROM chat_anchors WHERE video_id=?", (vid,)).fetchall()
    if existing and not force:
        return "kept"
    has_replay = (P(cfg, "chat") / f"{vid}.jsonl").exists()
    if meta.get("platform") == "twitch":
        set_anchors(con, vid, [(0.0, 0.0, 1.0)], "same_source")
        return "same_source"
    if meta.get("twitch_vod_id"):
        try:
            with tempfile.TemporaryDirectory(dir=audio) as tmp:   # temp audio is deleted on exit
                tw = download_twitch_audio(meta["twitch_vod_id"], Path(tmp))
                anchors = build_anchors(envelope(audio / f"{vid}.{cfg['audio_format']}"), envelope(tw), cfg)
            set_anchors(con, vid, anchors, "xcorr")
            return f"xcorr ({len(anchors)} anchor(s))"
        except Exception as e:
            log.warning("%s: cross-correlation unavailable (%s)", vid, e)
    if has_replay or start:
        # Twitch copy gone: assume the re-upload starts at stream start; flagged for manual review
        set_anchors(con, vid, [(0.0, float(meta.get("youtube_start_trim") or 0.0), 0.0)], "manual")
        return "manual (needs review)"
    return "no chat"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only")
    ap.add_argument("--force", action="store_true", help="recompute anchors that already exist")
    ap.add_argument("--set", nargs=3, metavar=("VIDEO_ID", "TRANSCRIPT_T", "OFFSET"), help="add/replace one manual anchor")
    ap.add_argument("--clear", metavar="VIDEO_ID", help="remove all anchors for a video")
    ap.add_argument("--set-start", nargs=2, metavar=("VIDEO_ID", "UNIX"), help="set stream_start_unix manually")
    ap.add_argument("--list-low-confidence", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    make_dirs(cfg)
    con = connect_archive(cfg)
    if a.set:
        vid, t, off = a.set[0], float(a.set[1]), float(a.set[2])
        con.execute("DELETE FROM chat_anchors WHERE video_id=? AND confidence < 0.5", (vid,))
        con.execute("INSERT OR REPLACE INTO chat_anchors VALUES(?,?,?,?,?)", (vid, t, off, 1.0, "manual"))
        con.execute("UPDATE videos SET chat_sig=NULL WHERE id=?", (vid,))
        con.commit()
        print(f"{vid}: anchor at {t}s offset {off}s")
        return 0
    if a.clear:
        con.execute("DELETE FROM chat_anchors WHERE video_id=?", (a.clear,))
        con.commit()
        return 0
    if a.set_start:
        con.execute("INSERT OR IGNORE INTO videos(id) VALUES(?)", (a.set_start[0],))
        con.execute("UPDATE videos SET stream_start_unix=?, stream_start_source='manual', chat_sig=NULL WHERE id=?",
                    (int(a.set_start[1]), a.set_start[0]))
        con.commit()
        return 0
    if a.list_low_confidence:
        rows = con.execute("SELECT video_id, COUNT(*) n, MIN(confidence) c, GROUP_CONCAT(DISTINCT method) m "
                           "FROM chat_anchors GROUP BY video_id HAVING MIN(confidence) < 0.5 ORDER BY video_id").fetchall()
        for r in rows:
            print(f"{r['video_id']}\tanchors={r['n']}\tmin_confidence={r['c']:.2f}\t{r['m']}")
        print(f"{len(rows)} VOD(s) need review")
        return 0
    for mp in sorted(P(cfg, "audio").glob("*.meta.json")):
        vid = mp.name[:-len(".meta.json")]
        if a.only and vid != a.only:
            continue
        try:
            result = align_one(con, cfg, vid, a.force)
            con.commit()
            if result != "kept":
                log.info("%s: %s", vid, result)
        except Exception as e:
            con.rollback()
            log.error("failed %s: %s", vid, e)
            log_failure(cfg, vid, "align_chat", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
