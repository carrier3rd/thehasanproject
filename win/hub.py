"""The PC is the whole VOD archive since 2026-11-26: downloads, live chat, build, publish, the site, backups.

History: the laptop's RAM corrupts data. On the morning of Nov 26 this PC took over building and publishing (it pulled
the laptop's downloads and live chat and pushed public.db back); that evening every other job moved here too:
  - the site (web/app.py) and the Cloudflare tunnel, the live chat logger and the admin app: win/daemons.ps1;
  - downloads (inventory, download, muted, chat_replay): the first part of every hourly run below;
  - transcription (deploy/pc/windows/runner.py) reads and writes this folder instead of the laptop's.
The laptop keeps its copy of the data as it was on Nov 26 and runs nothing for the archive.

  python win/hub.py run [--force]   download new streams, then rebuild and publish when something new arrived
  python win/hub.py features        the nightly features job (Spotlight, Chat vs Hasan, Receipts, quiz)
  python win/hub.py reel            fact-checks + reel for the week in progress, so every new stream is screened and
                                    fact-checked soon after it is published, the newest day first (level 3.5)
  python win/hub.py reel-backfill   reel.py weekly: the finished week, then one earlier week (level 4)
  python win/hub.py chatflags       the chat check behind the weekly trophies, newest stream first, then its second review
  python win/hub.py reddit          the weekly Reddit pull for Spotlight
  python win/hub.py update          git pull; restart the site when its code changed, re-register tasks when they changed

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
_tools = Path.home() / "tools" / "ffmpeg"          # the server laptop: ffmpeg copied there (winget refused over ssh)
if (_tools / "ffmpeg.exe").exists():
    _extra.append(str(_tools))
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
# 3.5 fact-checking the current day's VOD, then the rest of the week in progress (hub.py reel, hourly)
# 4  transcription: passive, catches up whenever there is room; the reel backfill of earlier weeks (hub.py reel-backfill)
# A job pauses every running job below it that competes for the same resources (memory, GPU). Today the only one
# that can be paused is transcription; Reddit pulls only use the network, so they leave it running. The Windows
# tasks' CPU priority follows the level too (Task Scheduler priority 4 / 6 / 7 / 9).
LEVELS = {"publish": 1, "backup": 2, "features": 2, "reddit": 3, "reel": 3.5, "chatflags": 3.5, "transcription": 4,
          "reel-backfill": 4}
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


def reel() -> int:
    """The week in progress: every new stream screened, judged and fact-checked. Every Claude call is claude -p on the
    subscription, and results are cached per stream window and per claim, so an hour with no new stream sends nothing.
    The newest day first. Network only (level 3.5): it leaves transcription running."""
    import datetime as dt
    today = dt.date.today()
    week = (today - dt.timedelta(days=(today.weekday() + 1) % 7)).isoformat()   # the Sunday that starts this week
    ok = step("pipeline/reel.py", "build", "--week", week, "--yes", "--latest-first")
    step("pipeline/report_tags.py", "run")   # the topic reports (Fact-checks page) take in the new fact-checks
    return 0 if ok else 1


def reel_backfill() -> int:
    """Level 4: the finished week (rebuilt when its streams changed), then one earlier week."""
    ok = step("pipeline/reel.py", "weekly")
    step("pipeline/report_tags.py", "run")
    return 0 if ok else 1


def chatflags() -> int:
    """The archive-wide chat check (weekly trophies): newest stream day first, then the stricter second review of the
    messages that could count. claude -p on the subscription; cached; a usage limit stops it until the next run."""
    ok = step("pipeline/chatflags.py", "run")
    return 0 if step("pipeline/chatflags.py", "verify") and ok else 1


def reddit() -> int:
    return 0 if step("pipeline/reddit_pull.py") else 1


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def update() -> int:
    """What the old laptop's vod-update.timer did: pull the code, and restart only what the pull changed. Static pages
    need nothing (served from disk per request). Server code (pipeline/, web/, admin/): the site and admin daemons
    are ended and keep_running starts them again within seconds. win/*.ps1: the tasks are registered again."""
    old = git("rev-parse", "HEAD")
    git("pull", "-q", "--ff-only")
    new = git("rev-parse", "HEAD")
    if new == old:
        return 0
    changed = git("diff", "--name-only", old, new).splitlines()
    say(f"updated {old[:7]} -> {new[:7]}: {len(changed)} file(s)")
    ps = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass"]
    code = [f for f in changed if f.endswith(".py") and f.split("/")[0] in ("pipeline", "web", "admin")]
    if code:
        say("restarting the site and the admin app (their code changed)")
        subprocess.run(ps + ["-Command", "Get-CimInstance Win32_Process | ? { $_.CommandLine -match 'uvicorn' -and $_.CommandLine -notmatch "
                             "'keep_running' } | "
                             "% { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"], timeout=120)
    if "pipeline/chat_logger.py" in changed:
        say("restarting the chat logger (its code changed)")
        subprocess.run(ps + ["-Command", "Get-CimInstance Win32_Process | ? { $_.CommandLine -match 'chat_logger[.]py' "
                             "-and $_.CommandLine -notmatch 'keep_running' } | % { Stop-Process -Id $_.ProcessId -Force "
                             "-ErrorAction SilentlyContinue }"], timeout=120)
    if any(f in ("win/tasks.ps1", "win/tasklib.ps1") for f in changed):
        say("registering the scheduled tasks again (win/tasks.ps1 changed)")
        r = subprocess.run(ps + ["-File", str(ROOT / "win" / "tasks.ps1"), "-Server"], capture_output=True, text=True,
                           timeout=300)
        say(r.stdout.strip() or r.stderr.strip())
    return 0


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
    ap.add_argument("cmd", choices=["run", "features", "reel", "reel-backfill", "chatflags", "reddit", "update"])
    ap.add_argument("--force", action="store_true", help="rebuild and publish even if no files changed")
    a = ap.parse_args()
    if a.cmd == "update":                   # every 10 minutes: only speaks when something changed
        try:
            return update()
        except Exception as e:
            say(f"!!! update: {e}")
            return 1
    say(f"=== hub {a.cmd} start")
    try:
        rc = {"features": features, "reel": reel, "reel-backfill": reel_backfill, "chatflags": chatflags, "reddit": reddit,
              "update": update}.get(a.cmd, lambda: run(a.force))()
    except Exception as e:
        say(f"!!! {e}")
        rc = 1
    say(f"=== hub {a.cmd} end (exit {rc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
