"""The PC is the whole VOD archive since 2026-11-26: downloads, live chat, build, publish, the site, backups.

History: the laptop's RAM corrupts data. On the morning of Nov 26 this PC took over building and publishing (it pulled
the laptop's downloads and live chat and pushed public.db back); that evening every other job moved here too:
  - the site (web/app.py) and the Cloudflare tunnel, the live chat logger and the admin app: win/daemons.ps1;
  - downloads (inventory, download, muted, chat_replay): the first part of every hourly run below;
  - transcription (deploy/pc/windows/runner.py) reads and writes this folder instead of the laptop's.
The laptop keeps its copy of the data as it was on Nov 26 and runs nothing for the archive.

  python win/hub.py run [--force]   download new streams, then rebuild and publish when something new arrived
  python win/hub.py features        the nightly features job (Spotlight, Chat vs Hasan, Receipts, quiz)

The site reads public.db, index/ and features/ from this folder: a fresh read-only connection per request, and the
search index reloads itself when its files change, so a publish needs no restart (common.replace_file waits out the
moment a request has public.db open; Windows refuses to replace an open file).
"""
import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
STATE = ROOT / "logs" / "hub-state.json"
LOG = ROOT / "logs" / f"hub-{time.strftime('%Y-%m-%d')}.log"

# ffmpeg/ffprobe and deno (yt-dlp's JavaScript runtime) were installed per-user by winget; a scheduled task may not
# have their folders on PATH, so add them (same as deploy/pc/windows/runner.py).
_pkgs = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
_extra = [str(p.parent) for pat in ("Gyan.FFmpeg*/*/bin/ffmpeg.exe", "DenoLand.Deno*/deno.exe") for p in _pkgs.glob(pat)]
os.environ["PATH"] = os.pathsep.join(_extra + [str(ROOT / ".venv" / "Scripts"), os.environ.get("PATH", "")])


def say(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def step(script: str, *args: str) -> bool:
    say(f"--- {script} {' '.join(args)}")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    with open(LOG, "a", encoding="utf-8") as f:
        r = subprocess.run([PY, script, *args], cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, env=env)
    if r.returncode != 0:
        say(f"!!! {script} failed (exit {r.returncode})")
    return r.returncode == 0


def signature() -> str:
    h = hashlib.sha256()
    for d in ("data/transcripts", "data/audio", "data/muted"):
        for f in sorted((ROOT / d).glob("*")):
            if f.is_file() and not f.name.endswith((".part", ".ytdl", ".tmp")):
                h.update(f"{f.name} {f.stat().st_size}\n".encode())
    return h.hexdigest()


def downloads() -> None:
    """What the laptop's vod-hourly / vod-nightly timers did before the build (deploy/nightly.sh's first four steps).
    download.py refuses, and logs it, when more than 5 VODs are pending (the owner's rule): run it by hand with --yes."""
    for script in ("pipeline/inventory.py", "pipeline/download.py", "pipeline/muted.py", "pipeline/chat_replay.py"):
        step(script)


# ---------------------------------------------------------------- priority levels (owner, 2026-11-26)
# 1  the owner using the PC (gaming); the site, the live chat logger; publishing a new stream (hourly hub)
# 2  daily upkeep: the backup, the features job
# 3  data collection: Reddit pulls (weekly, comments backfill); they resume where they stopped
# 4  transcription: passive, catches up whenever there is room
# A job pauses every running job below it that competes for the same resources (memory, GPU). Today the only one
# that can be paused is transcription; Reddit pulls only use the network, so they leave it running. The Windows
# tasks' CPU priority follows the level too (Task Scheduler priority 4 / 6 / 7 / 9).
LEVELS = {"publish": 1, "backup": 2, "features": 2, "reddit": 3, "transcription": 4}
RUNNER = Path.home() / "vod-archive"
RUNNER_TASK = "VOD transcription run now"


def transcription_running() -> bool:
    """The runner holds a lock on logs/runner.lock (offset 0) while it works."""
    lockf = RUNNER / "logs" / "runner.lock"
    if not lockf.exists():
        return False
    import msvcrt
    with open(lockf, "a") as f:
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return True
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    return False


@contextlib.contextmanager
def make_room(job: str, why: str):
    """Pause the lower-priority jobs that compete with `job` for memory and GPU, and restart them afterwards."""
    if LEVELS[job] >= LEVELS["reddit"]:          # level 3 and below: network only, nothing to pause
        yield
        return
    with transcription_paused(f"{why} (priority {LEVELS[job]} over transcription's {LEVELS['transcription']})"):
        yield


@contextlib.contextmanager
def transcription_paused(why: str):
    was = transcription_running()
    if was:
        say(f"pausing transcription for {why}")
        subprocess.run([str(RUNNER / ".venv" / "Scripts" / "python.exe"), "win/runner.py", "stop"], cwd=RUNNER,
                       capture_output=True, timeout=60)
        for _ in range(60):                 # it stops its GPU work within a few seconds; give it up to 3 minutes
            if not transcription_running():
                break
            time.sleep(3)
        else:
            say("!!! transcription did not stop within 3 minutes; going ahead anyway")
    try:
        yield
    finally:
        if was:
            r = subprocess.run(["schtasks", "/Run", "/TN", RUNNER_TASK], capture_output=True, text=True)
            say("resumed transcription" if r.returncode == 0 else f"!!! could not restart transcription: {r.stderr.strip()}")


def backup_due() -> bool:
    sys.path.insert(0, str(ROOT / "pipeline"))
    import backup
    from common import load_config
    cfg = load_config()
    bst = backup.settings(cfg)
    return bool(bst["repos"]) and time.time() - backup.read_status(cfg).get("last_ok", 0) >= bst["every_hours"] * 3600 * 0.95


def features() -> int:
    """The nightly features job (Spotlight, Chat vs Hasan, Receipts, quiz). The site serves features/ directly."""
    with make_room("features", "the features job"):
        ok = step("pipeline/features.py", "run")
    return 0 if ok else 1


def publish(st: dict, sig: str) -> int:
    step("pipeline/align_chat.py")
    step("pipeline/build_db.py")
    if not step("pipeline/publish.py"):
        say("!!! publish failed; public.db left unchanged")
        return 1
    step("pipeline/embed.py")
    step("pipeline/latest.py")   # the home page's latest-stream section (claude -p, subscription); failure is not fatal
    st["published_sig"] = sig
    save_state(st)
    say("published (the site picks up public.db and index/ at its next request)")
    return 0


def run(force: bool) -> int:
    st = load_state()
    downloads()
    sig = signature()
    new = force or st.get("published_sig") != sig
    due = backup_due()
    if not new:
        say("nothing new since the last publish")
    if not new and not due:
        return 0
    rc = 0
    with make_room("publish" if new else "backup", "publishing" if new else "the daily backup"):
        if new:
            rc = publish(st, sig)
        if due:
            step("pipeline/backup.py", "run")   # encrypted backup to Drive, at most once a day (backup.every_hours)
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "features"])
    ap.add_argument("--force", action="store_true", help="rebuild and publish even if no files changed")
    a = ap.parse_args()
    say(f"=== hub {a.cmd} start")
    try:
        rc = features() if a.cmd == "features" else run(a.force)
    except Exception as e:
        say(f"!!! {e}")
        rc = 1
    say(f"=== hub {a.cmd} end (exit {rc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
