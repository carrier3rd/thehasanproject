"""Encrypted, incremental backups of what exists only on this laptop, with restic.

  backup.py init --repo URL   create a repository (Google Drive via rclone: rclone:gdrive:backup, or a drive path such
                              as /media/probs/USB/vod-backup) and, the first time, the password file
  backup.py run [--force]     back up now (the hourly job calls this; it runs at most every `every_hours`)
  backup.py status            last backups, per repository
  backup.py restore-test      restore the latest archive.db to a temp folder and check it opens and is intact

What is backed up: a consistent snapshot of archive.db (SQLite online backup, safe while the logger writes), data/
transcripts, chat, muted spans, voices, voiceprints, reels and the Claude caches, QA labels, inventory and logs.
Not backed up: .env and site.env (secrets: keep them in a password manager), audio unless `include_audio` (re-
downloadable while the VODs exist), public.db and index/ (rebuilt from the archive).
Repositories are listed in config `backup.repos` (set in the private repo's config.yaml). Everything is encrypted with
the password in `backup.password_file`. **Without that password the backups cannot be read: keep a copy of it
somewhere other than this laptop.**
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from common import P, get_logger, load_config, make_dirs, try_lock  # noqa: F401 (try_lock moved to common)

log = get_logger("backup")
DEFAULTS = {"repos": [], "password_file": "~/.config/vod-archive/restic-password", "include_audio": False,
            "every_hours": 24, "keep_daily": 7, "keep_weekly": 8, "keep_monthly": 12, "check_every_days": 7,
            "prune_every_days": 7}


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("backup") or {})}


def status_path(cfg) -> Path:
    return P(cfg, "logs") / "backup-status.json"


def read_status(cfg) -> dict:
    p = status_path(cfg)
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def write_status(cfg, st: dict) -> None:
    p = status_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(p)


def password_file(bst: dict) -> Path:
    return Path(os.path.expanduser(bst["password_file"]))


def restic_env(bst: dict) -> dict:
    env = dict(os.environ, RESTIC_PASSWORD_FILE=str(password_file(bst)))
    env.pop("RESTIC_PASSWORD", None)
    return env


def rclone_program(bst: dict | None = None) -> str | None:
    for c in ((bst or {}).get("rclone_bin"), shutil.which("rclone"), os.path.expanduser("~/.local/bin/rclone")):
        if c and os.path.exists(c):
            return c
    return None


def restic(bst: dict, repo: str, *args, capture=True, check=True) -> subprocess.CompletedProcess:
    exe = bst.get("restic_bin") or shutil.which("restic")   # restic_bin: e.g. winget's restic_0.20.0_windows_amd64.exe
    if not exe or not shutil.which(exe):
        raise RuntimeError("restic is not installed (sudo apt install restic; on Windows winget install restic.restic "
                           "and set backup.restic_bin)")
    cmd = [exe, "-r", repo]
    if repo.startswith("rclone:"):
        prog = rclone_program(bst)
        if not prog:
            raise RuntimeError("rclone not found (needed for rclone: repositories)")
        cmd += ["-o", f"rclone.program={prog}"]
    r = subprocess.run(cmd + list(args), env=restic_env(bst), capture_output=capture, text=True, encoding="utf-8",
                       errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"restic {args[0]} failed: {(r.stderr or r.stdout or '').strip()[-400:]}")
    return r


def snapshot_db(cfg, dest: Path) -> Path:
    """Consistent copy of archive.db via SQLite's online backup API (works while the logger is writing)."""
    src_path = P(cfg, "archive_db")
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "archive.db"
    if out.exists():
        out.unlink()
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=60)
    dst = sqlite3.connect(out)
    try:
        # One step. A stepped backup restarts from page 1 whenever another connection writes, and the chat logger
        # writes constantly, so on a 4 GB database it never finished (2026-11-25). In WAL mode a single read
        # transaction does not block the logger's writes; the copy takes seconds.
        src.backup(dst, pages=-1)
    finally:
        dst.close()
        src.close()
    return out


def paths_to_back_up(cfg, bst: dict, staging: Path) -> list[str]:
    root = Path(cfg["_root"])
    archive = P(cfg, "archive_db").parent
    cand = [staging, P(cfg, "transcripts"), P(cfg, "chat"), P(cfg, "muted"), P(cfg, "voices"), P(cfg, "voiceprints"),
            P(cfg, "qa"), archive / "reel", archive / "rhetoric", root / "reels", P(cfg, "logs"), root / "older",
            archive / "features", root / "features", root / "chatflags", root / "feedback"]   # features: approved receipts
    if bst["include_audio"]:
        cand.append(P(cfg, "audio"))
    return [str(p) for p in cand if p.exists()]


def excludes(cfg) -> list[str]:
    # live database files (the snapshot stands in for them), locks, temp files, secrets
    archive = P(cfg, "archive_db")
    out = []
    for pat in (str(archive), str(archive) + "-wal", str(archive) + "-shm", "*.lock", "*.tmp", "*.part", ".env", "site.env"):
        out += ["--exclude", pat]
    return out


def init(cfg, repo: str) -> int:
    bst = settings(cfg)
    pw = password_file(bst)
    if not pw.exists():
        pw.parent.mkdir(parents=True, exist_ok=True)
        pw.write_text(secrets.token_urlsafe(32) + "\n")
        os.chmod(pw, 0o600)
        print(f"Created the backup password in {pw}.")
        print("SAVE THIS PASSWORD SOMEWHERE ELSE NOW (a password manager). If this laptop dies, the backups cannot be")
        print("read without it. Show it with:  cat " + str(pw))
    if restic(bst, repo, "cat", "config", check=False).returncode == 0:
        print(f"{repo}: already a restic repository; nothing to do")
    else:
        restic(bst, repo, "init")
        print(f"{repo}: repository created")
    repos = settings(cfg)["repos"]
    if repo not in repos:
        print(f"\nAdd it to the private repo's config.yaml so the nightly job uses it:\n  backup:\n    repos: [{', '.join(repos + [repo])}]")
    return 0


def run(cfg, force: bool = False, now: float | None = None) -> str:
    bst = settings(cfg)
    now = now or time.time()
    st = read_status(cfg)
    if not bst["repos"]:
        log.warning("no backup repository configured (backup.repos); nothing backed up")
        st["warning"] = "no repository configured"
        write_status(cfg, st)
        return "not_configured"
    if not password_file(bst).exists():
        log.error("backup password file %s missing; run backup.py init", password_file(bst))
        return "no_password"
    if not force and now - st.get("last_ok", 0) < bst["every_hours"] * 3600 * 0.95:
        return "not_due"
    staging = P(cfg, "archive_db").parent / "archive-snapshot"     # fixed path: same place in every snapshot
    # one run at a time: two runs share the staging path, and the second deleted the first one's snapshot
    lock = open(P(cfg, "archive_db").parent / "backup.lock", "w")
    if not try_lock(lock):
        log.info("another backup run is going; skipping")
        lock.close()
        return "busy"
    try:
        snap = snapshot_db(cfg, staging)
        con = sqlite3.connect(snap)
        ok = con.execute("PRAGMA quick_check").fetchone()[0]
        con.close()
        if ok != "ok":
            raise RuntimeError(f"archive.db snapshot failed its integrity check: {ok}")
        paths = paths_to_back_up(cfg, bst, staging)
        results = {}
        for repo in bst["repos"]:
            rs = st.setdefault("repos", {}).setdefault(repo, {})
            t0 = time.time()
            try:
                r = restic(bst, repo, "backup", "--json", "--tag", "vod-archive", *excludes(cfg), *paths)
                summary = next((json.loads(l) for l in reversed(r.stdout.splitlines())
                                if l.startswith("{") and '"message_type":"summary"' in l.replace(" ", "")), {})
                keep = ["--keep-daily", str(bst["keep_daily"]), "--keep-weekly", str(bst["keep_weekly"]),
                        "--keep-monthly", str(bst["keep_monthly"])]
                if now - rs.get("last_prune", 0) > bst["prune_every_days"] * 86400:
                    restic(bst, repo, "forget", "--tag", "vod-archive", *keep, "--prune")
                    rs["last_prune"] = now
                if now - rs.get("last_check", 0) > bst["check_every_days"] * 86400:
                    restic(bst, repo, "check", "--read-data-subset", "5%")
                    rs["last_check"] = now
                rs.update(last_ok=now, error=None, seconds=round(time.time() - t0),
                          snapshot=summary.get("snapshot_id"), added_bytes=summary.get("data_added"),
                          total_bytes=summary.get("total_bytes_processed"), files=summary.get("total_files_processed"))
                results[repo] = "ok"
                log.info("%s: backed up %s files, %.1f MB new, in %ds", repo, summary.get("total_files_processed"),
                         (summary.get("data_added") or 0) / 1e6, rs["seconds"])
            except Exception as e:          # one destination failing must not stop the others
                rs.update(error=str(e)[-400:], last_error=now)
                results[repo] = "failed"
                log.error("%s: %s", repo, e)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        lock.close()
    if any(v == "ok" for v in results.values()):
        st["last_ok"] = now
    st["last_run"], st["warning"] = now, None
    write_status(cfg, st)
    return "ok" if all(v == "ok" for v in results.values()) else ("partial" if "ok" in results.values() else "failed")


def status(cfg) -> int:
    bst = settings(cfg)
    st = read_status(cfg)
    if not bst["repos"]:
        print("No backup repository configured (backup.repos in the private config). Nothing is being backed up.")
    age = lambda t: f"{(time.time() - t) / 3600:.1f} h ago" if t else "never"
    print(f"last successful backup: {age(st.get('last_ok'))}")
    for repo in bst["repos"]:
        rs = st.get("repos", {}).get(repo, {})
        print(f"{repo}: last ok {age(rs.get('last_ok'))}, {rs.get('files')} files, "
              f"{(rs.get('total_bytes') or 0) / 1e9:.2f} GB, last check {age(rs.get('last_check'))}"
              + (f"\n  LAST ERROR: {rs['error']}" if rs.get("error") else ""))
    return 0


def restore_test(cfg, repo: str | None = None) -> int:
    bst = settings(cfg)
    repo = repo or (bst["repos"][0] if bst["repos"] else None)
    if not repo:
        print("no repository configured")
        return 1
    with tempfile.TemporaryDirectory(prefix="vod-restore-") as tmp:
        restic(bst, repo, "restore", "latest", "--tag", "vod-archive", "--target", tmp, "--include", "archive.db")
        found = list(Path(tmp).rglob("archive-snapshot/archive.db"))
        if not found:
            print("restore-test FAILED: archive.db not found in the latest snapshot")
            return 1
        con = sqlite3.connect(found[0])
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("videos", "turns", "chat", "live_msgs")}
        con.close()
        print(f"restore-test {'OK' if ok == 'ok' else 'FAILED: ' + ok}: {repo} latest archive.db "
              f"({found[0].stat().st_size / 1e6:.0f} MB) " + ", ".join(f"{k} {v:,}" for k, v in counts.items()))
        return 0 if ok == "ok" else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["init", "run", "status", "restore-test"])
    ap.add_argument("--repo", help="init/restore-test: repository URL")
    ap.add_argument("--force", action="store_true", help="run: back up even if the last backup is recent")
    a = ap.parse_args()
    cfg = load_config()
    make_dirs(cfg)
    if a.cmd == "init":
        if not a.repo:
            sys.exit("init needs --repo (e.g. rclone:gdrive:backup or /media/USER/DRIVE/vod-backup)")
        return init(cfg, a.repo)
    if a.cmd == "run":
        result = run(cfg, force=a.force)
        print("backup:", result)
        return 0 if result in ("ok", "not_due", "not_configured", "busy") else 1   # busy: another run has it
    if a.cmd == "status":
        return status(cfg)
    return restore_test(cfg, a.repo)


if __name__ == "__main__":
    raise SystemExit(main())
