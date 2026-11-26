"""PC data machine: build and publish on the Windows PC while the laptop only downloads, logs chat and serves the site.

Added 2026-11-26 because the laptop's RAM corrupts archive.db. The laptop's nightly.sh skips its build, publish and
backup steps while `logs/hub-elsewhere` exists there; this script does those steps here instead.

  python win/hub.py run [--force]   pull new files and live chat from the laptop, rebuild, publish, push public.db
  python win/hub.py sync            only pull from the laptop
  python win/hub.py push            only push the current public.db (and index/) to the laptop
  python win/hub.py features        run the features job (Spotlight, Chat vs Hasan, Receipts, quiz) and push features/

Everything flows one way: the laptop's files are the source, this copy is rebuilt from them, and only public.db and
index/ go back. Each file pulled is checked against the laptop's sha256 (the laptop's RAM can flip bits in transit).
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REMOTE = "~/thehasanproject"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "laptop"]
SYNC_DIRS = ["data/transcripts", "data/chat", "data/muted", "data/audio"]
SKIP = (".opus", ".part", ".tmp", ".ytdl")
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
STATE = ROOT / "logs" / "hub-state.json"
LOG = ROOT / "logs" / f"hub-{time.strftime('%Y-%m-%d')}.log"


def say(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def ssh(cmd: str, stdin: bytes | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(SSH + [cmd], input=stdin, capture_output=True, timeout=timeout)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- pull
def remote_listing() -> dict[str, tuple[int, str]]:
    r = ssh(f"cd {REMOTE} && find {' '.join(SYNC_DIRS)} -type f -printf '%s %T@ %p\\n'")
    if r.returncode != 0:
        raise RuntimeError(f"laptop listing failed: {r.stderr.decode(errors='replace')[:300]}")
    out = {}
    for line in r.stdout.decode().splitlines():
        size, mtime, path = line.split(" ", 2)
        if not path.endswith(SKIP):
            out[path] = (int(size), mtime)
    return out


def fetch(paths: list[str]) -> list[str]:
    """Copy these laptop files here, verified by sha256. Returns the paths that failed twice."""
    bad = []
    for attempt in (1, 2):
        todo = paths if attempt == 1 else bad
        if not todo:
            break
        listing = "\0".join(todo).encode()
        r = ssh(f"cd {REMOTE} && tar -cf - --null -T -", stdin=listing, timeout=3600)
        if r.returncode != 0:
            raise RuntimeError(f"tar from laptop failed: {r.stderr.decode(errors='replace')[:300]}")
        sums = ssh(f"cd {REMOTE} && xargs -0 sha256sum", stdin=listing, timeout=3600)
        want = {p: h for h, p in (l.split("  ", 1) for l in sums.stdout.decode().splitlines())}
        bad = []
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tar:
            for m in tar.getmembers():
                if not m.isfile() or m.name not in want:
                    continue
                dest = ROOT / m.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                part = dest.with_name(dest.name + ".hubpart")
                with tar.extractfile(m) as src, open(part, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if sha(part) == want[m.name]:
                    os.replace(part, dest)
                else:
                    part.unlink()
                    bad.append(m.name)
    return bad


def sync_files(st: dict) -> int:
    remote = remote_listing()
    seen = st.get("files", {})
    changed = [p for p, (size, mtime) in remote.items()
               if seen.get(p) != [size, mtime] or not (ROOT / p).exists()]
    failed = []
    for i in range(0, len(changed), 200):
        failed += fetch(changed[i:i + 200])
    for p in changed:
        if p not in failed:
            seen[p] = list(remote[p])
    # a file removed on the laptop (moved to data/trash) is removed here too, so it is not published
    gone = 0
    for d in SYNC_DIRS:
        for f in (ROOT / d).glob("*"):
            rel = f.relative_to(ROOT).as_posix()
            if f.is_file() and rel not in remote and not rel.endswith(SKIP + (".hubpart",)):
                trash = ROOT / "data" / "trash" / "hub" / f.name
                trash.parent.mkdir(parents=True, exist_ok=True)
                os.replace(f, trash)
                seen.pop(rel, None)
                gone += 1
    st["files"] = seen
    say(f"files: {len(changed) - len(failed)} pulled, {len(failed)} failed checksum twice, {gone} removed")
    if failed:
        say("  failed: " + " ".join(failed[:10]))
    return len(changed) - len(failed) + gone


LIVE_DUMP = r"""
import json, sqlite3, sys
after = int(sys.argv[1])
c = sqlite3.connect("file:archive/archive.db?mode=ro", uri=True, timeout=60)
cur = c.execute("SELECT rowid, * FROM live_msgs WHERE rowid > ? ORDER BY rowid", (after,))
cols = [d[0] for d in cur.description]
for row in cur:
    print(json.dumps(dict(zip(cols, row))))
print(json.dumps({"sessions": [list(r) for r in c.execute("SELECT rowid, channel, start_ms, end_ms FROM sessions")]}))
"""


def sync_live() -> int:
    con = sqlite3.connect(ROOT / "archive" / "archive.db", timeout=60)
    after = con.execute("SELECT COALESCE(MAX(rowid), 0) FROM live_msgs").fetchone()[0]
    r = ssh(f"cd {REMOTE} && .venv/bin/python - {after}", stdin=LIVE_DUMP.encode(), timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"live chat dump failed: {r.stderr.decode(errors='replace')[:300]}")
    lines = r.stdout.decode().splitlines()
    sessions = json.loads(lines.pop())["sessions"]
    rows = [json.loads(l) for l in lines]
    if rows:
        cols = list(rows[0])
        con.executemany(f"INSERT OR IGNORE INTO live_msgs({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                        [[r[c] for c in cols] for r in rows])
    for rowid, channel, start, end in sessions:
        con.execute("INSERT OR REPLACE INTO sessions(rowid, channel, start_ms, end_ms) VALUES(?,?,?,?)",
                    (rowid, channel, start, end))
    con.commit()
    con.close()
    say(f"live chat: {len(rows)} new message(s), {len(sessions)} session(s)")
    return len(rows)


# ---------------------------------------------------------------- build + push
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
            if f.is_file():
                h.update(f"{f.name} {f.stat().st_size}\n".encode())
    return h.hexdigest()


def push() -> None:
    pub = ROOT / "public.db"
    want = sha(pub)
    r = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", str(pub), f"laptop:{REMOTE}/public.upload.db"],
                       capture_output=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"scp public.db failed: {r.stderr.decode(errors='replace')[:300]}")
    r = ssh(f"cd {REMOTE} && test \"$(sha256sum public.upload.db | cut -c1-64)\" = {want} "
            f"&& mv -f public.upload.db public.db && echo swapped")
    if b"swapped" not in r.stdout:
        ssh(f"rm -f {REMOTE}/public.upload.db")
        raise RuntimeError("public.db arrived damaged on the laptop; the site keeps the old one")
    say(f"pushed public.db ({pub.stat().st_size / 1e6:.0f} MB, sha256 checked)")

    latest = ROOT / "features" / "latest.json"
    if latest.exists():
        want = sha(latest)
        r = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", str(latest), f"laptop:{REMOTE}/features/latest.json.tmp"],
                           capture_output=True, timeout=600)
        r2 = ssh(f"cd {REMOTE}/features && test \"$(sha256sum latest.json.tmp | cut -c1-64)\" = {want} "
                 f"&& mv -f latest.json.tmp latest.json && echo swapped") if r.returncode == 0 else None
        if r2 is None or b"swapped" not in r2.stdout:
            say("!!! features/latest.json did not arrive intact; the home page keeps the previous one")
        else:
            say("pushed features/latest.json (the home page's latest-stream section)")

    idx = ROOT / "index"
    local = {f.name: sha(f) for f in idx.glob("*") if f.is_file()}
    r = ssh(f"cd {REMOTE}/index && sha256sum *")
    remote = {p: h for h, p in (l.split("  ", 1) for l in r.stdout.decode().splitlines())}
    if local == remote:
        return
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in local:
            tar.add(idx / name, arcname=name)
    r = ssh(f"cd {REMOTE} && rm -rf index.new && mkdir index.new && tar -xf - -C index.new",
            stdin=buf.getvalue(), timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"index upload failed: {r.stderr.decode(errors='replace')[:300]}")
    sums = "".join(f"{h}  {n}\n" for n, h in local.items()).encode()
    r = ssh(f"cd {REMOTE}/index.new && sha256sum -c --quiet - && cd .. && rm -rf index.old && mv index index.old "
            f"&& mv index.new index && rm -rf index.old && pkill -f '[u]vicorn web.app:app'; echo done-$?",
            stdin=sums, timeout=600)
    if b"done-0" not in r.stdout:
        raise RuntimeError(f"index push failed (the site keeps its old index): {r.stdout.decode(errors='replace')[-300:]}")
    say("pushed index/ and restarted the site so it loads the new search index")


def push_features() -> None:
    """Send features/ (what the site serves at /api/features/...) to the laptop, every file sha256-checked, then swap
    the folder in whole so the site never sees half of a run."""
    src = ROOT / "features"
    files = sorted(p for p in src.rglob("*") if p.is_file() and not p.name.endswith((".tmp", ".lock")))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in files:
            tar.add(p, arcname=p.relative_to(src).as_posix())
    r = ssh(f"cd {REMOTE} && rm -rf features.new && mkdir features.new && tar -xf - -C features.new",
            stdin=buf.getvalue(), timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"features upload failed: {r.stderr.decode(errors='replace')[:300]}")
    sums = "".join(f"{sha(p)}  {p.relative_to(src).as_posix()}\n" for p in files).encode()
    r = ssh(f"cd {REMOTE}/features.new && sha256sum -c --quiet - && cd .. && rm -rf features.old "
            f"&& mv features features.old && mv features.new features && rm -rf features.old && echo swapped",
            stdin=sums, timeout=600)
    if b"swapped" not in r.stdout:
        ssh(f"rm -rf {REMOTE}/features.new")
        raise RuntimeError("features/ arrived damaged on the laptop; the site keeps the previous files")
    say(f"pushed features/ ({len(files)} files, sha256 checked)")


# ---------------------------------------------------------------- priority levels (owner, 2026-11-26)
# 1  the owner using the PC (gaming); the site and live chat logger (laptop); publishing a new stream (hourly hub)
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
    """The nightly features job (Spotlight, Chat vs Hasan, Receipts, quiz), run here instead of on the laptop."""
    sync_live()
    with make_room("features", "the features job"):
        ok = step("pipeline/features.py", "run")
        push_features()        # even after a partial run: what finished is already in features/
    return 0 if ok else 1


def publish(st: dict, sig: str) -> int:
    step("pipeline/align_chat.py")
    step("pipeline/build_db.py")
    if not step("pipeline/publish.py"):
        say("!!! publish failed; public.db left unchanged here and on the laptop")
        return 1
    step("pipeline/embed.py")
    step("pipeline/latest.py")   # the home page's latest-stream section (claude -p, subscription); failure is not fatal
    push()
    st["published_sig"] = sig
    save_state(st)
    return 0


def run(force: bool) -> int:
    st = load_state()
    sync_files(st)
    save_state(st)
    sync_live()
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
    ap.add_argument("cmd", choices=["run", "sync", "push", "features"])
    ap.add_argument("--force", action="store_true", help="rebuild and publish even if no files changed")
    a = ap.parse_args()
    say(f"=== hub {a.cmd} start")
    try:
        if a.cmd == "sync":
            st = load_state()
            sync_files(st)
            save_state(st)
            sync_live()
            rc = 0
        elif a.cmd == "push":
            push()
            rc = 0
        elif a.cmd == "features":
            rc = features()
        else:
            rc = run(a.force)
    except Exception as e:
        say(f"!!! {e}")
        rc = 1
    say(f"=== hub {a.cmd} end (exit {rc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
