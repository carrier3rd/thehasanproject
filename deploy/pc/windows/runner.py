"""GPU transcription runner for Windows (replaces the WSL bash scripts, 2026-11-14).

Setup on a new Windows PC: deploy/pc/windows/setup.ps1, then copy .env (HF_TOKEN) and private/config.yaml next to
pipeline/, add ssh alias `laptop`, and run prefetch.py once. Scheduled tasks: see README.md here.

  runner.py run [--until HH:MM]   backfill the laptop's pending VODs, then the older YouTube queue; stop at --until
  runner.py status                print one status line now
  runner.py stop                  ask a running `run` to stop (it kills the current transcription and exits)

What the old scripts did, and where it is now:
  pc_backfill2.sh      -> backfill()         VODs with no transcript on the laptop, newest first, results copied back
  older_transcribe.sh  -> older()            newest N no-caption YouTube uploads: audio only, transcripts stay in older/
  gpu_thermostat.sh    -> thermostat()       safety net: suspend transcription at HOT C, resume at COOL C
  live.sh              -> status_loop()      one line every ~2 min in logs/live-status.log
  night_runner/run_now -> main()             the stop time; the Windows scheduled tasks start this script
Everything is resumable: a VOD cut off part-way starts again from its beginning next run.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import msvcrt
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

# the project folder: the nearest parent holding pipeline/process.py (deploy/pc/windows/ in the repo, win/ in the old copy)
ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pipeline" / "process.py").exists())
LOGS = ROOT / "logs"
DATA = ROOT / "data"
OLDER = ROOT / "older"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
YTDLP = ROOT / ".venv" / "Scripts" / "yt-dlp.exe"
STOP_FILE = LOGS / "STOP"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "laptop"]
REMOTE = "thehasanproject"
OLDER_N = 181  # owner approved the newest 181 no-caption videos (2026-11-12); more need a new OK
AHEAD = 30  # the downloader keeps at most this many audio files waiting
HOT = int(os.environ.get("HOT", 85))
COOL = int(os.environ.get("COOL", 78))
NOWIN = subprocess.CREATE_NO_WINDOW

# ffmpeg/ffprobe and deno (yt-dlp's JavaScript runtime) were installed per-user by winget; a process started before
# that install, or by a scheduled task, may not have them on PATH, so add their folders ourselves.
_pkgs = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
_extra = [str(p.parent) for pat in ("Gyan.FFmpeg*/*/bin/ffmpeg.exe", "DenoLand.Deno*/deno.exe") for p in _pkgs.glob(pat)]
os.environ["PATH"] = os.pathsep.join(_extra + [os.environ.get("PATH", "")])

stop = threading.Event()
state = {"job": "idle", "vod": "", "started": 0.0, "est_min": 0, "paused": False, "proc": None}


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(name: str, msg: str, status: str | None = None) -> None:
    with open(LOGS / name, "a", encoding="utf-8") as f:
        f.write(f"{now()} {msg}\n")
    if status:
        (LOGS / status).write_text(msg + "\n", encoding="utf-8")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NOWIN, **kw)


def ssh(command: str, stdin: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return run(SSH + [command], input=stdin, timeout=timeout)


def env() -> dict:
    e = dict(os.environ)
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = re.sub(r"^export\s+", "", line.strip())
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            e[k.strip()] = v.strip().strip("'\"")
    e["VOD_ARCHIVE_PRIVATE_DIR"] = "private"
    e["PYTHONUTF8"] = "1"
    return e


def duration_min(path: Path) -> int:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]).stdout.strip()
    try:
        return int(float(out)) // 60
    except ValueError:
        return 0


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        parent = psutil.Process(proc.pid)
        for p in parent.children(recursive=True) + [parent]:
            try:
                p.resume()
                p.kill()
            except psutil.Error:
                pass
    except psutil.Error:
        pass


def transcribe(vid: str, logname: str) -> bool:
    """Run process.py on one VOD. False if it failed or we were told to stop."""
    with open(LOGS / logname, "a", encoding="utf-8") as out:
        proc = subprocess.Popen([str(PY), "pipeline/process.py", "--only", vid], cwd=ROOT, env=env(),
                                stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=NOWIN)
        state["proc"] = proc
        try:
            while proc.poll() is None:
                if stop.wait(2):
                    kill_tree(proc)
                    proc.wait()
                    return False
        finally:
            state["proc"] = None
            state["paused"] = False
    return proc.returncode == 0 and (DATA / "transcripts" / f"{vid}.json").exists()


# ---------------------------------------------------------------- laptop backfill
def scp_down(remote: str, local: Path) -> bool:
    part = local.with_name(local.name + ".part")
    ok = run(["scp", "-q", "-o", "BatchMode=yes", f"laptop:{remote}", str(part)], timeout=3600).returncode == 0
    if ok:
        part.replace(local)
    else:
        part.unlink(missing_ok=True)
    return ok


def scp_up_new(local: Path, remote_dir: str) -> bool:
    """Copy to the laptop unless a file of that name is already there (never overwrite: it may have been re-labelled)."""
    if ssh(f"test -f {remote_dir}/{local.name}").returncode == 0:
        return True
    tmp = f"{remote_dir}/.{local.name}.part"
    if run(["scp", "-q", "-o", "BatchMode=yes", str(local), f"laptop:{tmp}"], timeout=3600).returncode != 0:
        return False
    return ssh(f"mv -n {tmp} {remote_dir}/{local.name}").returncode == 0


def new_streams() -> list[str]:
    """Livestream VODs on the laptop with audio but no transcript yet (what backfill() would do), newest first."""
    r = ssh(f"cd {REMOTE} && .venv/bin/python pipeline/pending.py 2>/dev/null")
    if r.returncode != 0:
        return []
    return sorted({m[:-5] for m in re.findall(r"v[0-9]+\.opus", r.stdout)}, reverse=True)


def backfill() -> None:
    say = lambda m: log("pc-backfill.log", m, "pc-status.txt")
    for d in ("audio", "muted", "transcripts"):
        (DATA / d).mkdir(parents=True, exist_ok=True)
    r = ssh(f"cd {REMOTE} && .venv/bin/python pipeline/pending.py 2>/dev/null")
    if r.returncode != 0:
        say(f"could not reach the laptop for the pending list (ssh exit {r.returncode}); skipping backfill")
        return
    ids = sorted({m[:-5] for m in re.findall(r"v[0-9]+\.opus", r.stdout)}, reverse=True)
    if not ids:
        say("nothing pending")
        return
    say(f"windows backfill: {len(ids)} VOD(s) pending: {' '.join(ids)}")
    for vid in ids:
        if stop.is_set():
            return
        if ssh(f"test -f {REMOTE}/data/transcripts/{vid}.json").returncode == 0:
            say(f"{vid} already on laptop, skipping")
            continue
        audio = DATA / "audio" / f"{vid}.opus"
        if not audio.exists() and not scp_down(f"{REMOTE}/data/audio/{vid}.opus", audio):
            say(f"could not copy {vid} audio")
            return
        muted = DATA / "muted" / f"{vid}.json"
        if not muted.exists():
            scp_down(f"{REMOTE}/data/muted/{vid}.json", muted)
        mins = duration_min(audio)
        est = mins * 10 // 125
        state.update(job="backfill", vod=vid, started=time.time(), est_min=est)
        say(f"transcribing {vid} ({mins} min audio, ~{est}m)")
        if not transcribe(vid, "pc-process.log"):
            say(f"{vid} stopped by the runner" if stop.is_set() else f"{vid} FAILED (see logs/pc-process.log); stopping")
            return
        tdir = f"{REMOTE}/data/transcripts"
        emb = DATA / "transcripts" / f"{vid}.emb.npz"
        if emb.exists():
            scp_up_new(emb, tdir)
        if not scp_up_new(DATA / "transcripts" / f"{vid}.json", tdir):
            say(f"{vid} transcribed but could not be copied to the laptop; stopping")
            return
        times = LOGS / "process_times.tsv"
        if times.exists():
            lines = [l for l in times.read_text(encoding="utf-8").splitlines() if l.startswith(vid + "\t")]
            if lines:
                f = f"{REMOTE}/logs/process_times.tsv"
                ssh(f'line=$(cat); grep -qxF -- "$line" {f} || printf "%s\\n" "$line" >> {f}', stdin=lines[-1])
        say(f"{vid} done and on the laptop")
        audio.unlink(missing_ok=True)
    say("backfill finished")


# ---------------------------------------------------------------- older YouTube uploads
def older_queue() -> list[str]:
    rows = [json.loads(l) for l in (OLDER / "sync.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    nc = sorted((r for r in rows if r["status"] == "no_captions"), key=lambda r: r["date"], reverse=True)
    return [r["yt"] for r in nc[:OLDER_N]]


BLOCK_AFTER = 3  # this many failed downloads in a row means YouTube is blocking us ("confirm you're not a bot")
BACKOFF_H = 3  # then wait this long before trying again
GAP = (30, 90)  # seconds between downloads normally; (120, 300) after a block


def download_one(y: str) -> bool:
    adir = OLDER / "audio"
    with open(LOGS / "older-dl.log", "a", encoding="utf-8") as out:
        proc = subprocess.Popen(
            [str(YTDLP), "-q", "--no-warnings", "-x", "--audio-format", "opus", "--audio-quality", "64K",
             "-f", "bestaudio/best", "--no-playlist", "--concurrent-fragments", "4",
             "-o", str(adir / f"yt_{y}.part.%(ext)s"), f"https://www.youtube.com/watch?v={y}"],
            stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=NOWIN | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        while proc.poll() is None:
            if stop.wait(2):
                kill_tree(proc)
                proc.wait()
    part = adir / f"yt_{y}.part.opus"
    ok = part.exists() and not stop.is_set()
    if ok:
        part.replace(adir / f"yt_{y}.opus")
        log("older-transcribe.log", f"downloaded {y}")
    elif not stop.is_set():
        log("older-transcribe.log", f"download FAILED {y}")
    for leftover in adir.glob(f"yt_{y}.part.*"):
        leftover.unlink(missing_ok=True)
    return ok


def downloader(ids: list[str]) -> None:
    """Two passes over the queue: everything once, then one retry of what failed. Backs off when blocked."""
    adir = OLDER / "audio"
    have = lambda y: any(p.exists() for p in (OLDER / "transcripts" / f"yt_{y}.json", adir / f"yt_{y}.opus",
                                                DATA / "audio" / f"yt_{y}.opus"))
    gap = GAP
    todo = list(ids)
    for attempt in (1, 2):
        failed, streak = [], 0
        for y in todo:
            if stop.is_set():
                return
            if have(y):
                continue
            while len(list(adir.glob("*.opus"))) >= AHEAD:
                if stop.wait(120):
                    return
            if download_one(y):
                streak = 0
            else:
                failed.append(y)
                streak += 1
                if streak >= BLOCK_AFTER and not stop.is_set():
                    log("older-transcribe.log", f"{streak} downloads failed in a row: YouTube is blocking; "
                                                f"backing off {BACKOFF_H}h (failed ones are retried later)")
                    gap, streak = (120, 300), 0
                    if stop.wait(BACKOFF_H * 3600):
                        return
            if stop.wait(random.randint(*gap)):
                return
        if not failed:
            break
        todo = failed
        if attempt == 1:
            log("older-transcribe.log", f"first pass done; retrying {len(failed)} failed download(s)")
    log("older-transcribe.log", "downloader finished" + (f"; {len(failed)} could not be downloaded" if failed else ""))


def older() -> None:
    say = lambda m: log("older-transcribe.log", m, "older-status.txt")
    if not (OLDER / "sync.jsonl").exists():
        say("no older/sync.jsonl here; skipping the older queue")
        return
    for d in (OLDER / "audio", OLDER / "transcripts", DATA / "audio", DATA / "transcripts"):
        d.mkdir(parents=True, exist_ok=True)
    ids = older_queue()
    n = len(ids)
    say(f"queue: {n} videos")
    dl = threading.Thread(target=downloader, args=(ids,), daemon=True)
    dl.start()
    done = lambda y: (OLDER / "transcripts" / f"yt_{y}.json").exists()
    ready = lambda y: (OLDER / "audio" / f"yt_{y}.opus").exists() or (DATA / "audio" / f"yt_{y}.opus").exists()
    tried: set[str] = set()
    while not stop.is_set():
        # new livestreams always come first (owner, 2026-11-20): before every older video, ask the laptop whether a
        # new stream is waiting and transcribe it first. A stream that backfill could not finish is not retried in
        # this run, so a broken copy can't stall the older queue.
        fresh = [v for v in new_streams() if v not in tried]
        if fresh:
            say(f"new livestream(s) waiting on the laptop: {' '.join(fresh)}; doing them first")
            tried.update(fresh)
            backfill()
            if stop.is_set():
                return
            continue
        # the next video, in queue order, that has audio; a failed download no longer holds up the ones after it
        y = next((y for y in ids if not done(y) and ready(y)), None)
        if y is None:
            if not dl.is_alive():
                break
            state.update(job="older: waiting for downloads", proc=None)
            if stop.wait(60):
                return
            continue
        k = sum(map(done, ids)) + 1
        vid = f"yt_{y}"
        waiting = OLDER / "audio" / f"{vid}.opus"
        audio = DATA / "audio" / f"{vid}.opus"
        if waiting.exists():
            shutil.move(waiting, audio)
        mins = duration_min(audio)
        state.update(job=f"older {k}/{n}", vod=vid, started=time.time(), est_min=mins * 10 // 125)
        say(f"[{k}/{n}] transcribing {vid} ({mins} min audio)")
        if transcribe(vid, "older-process.log"):
            for suffix in (".json", ".emb.npz"):
                src = DATA / "transcripts" / f"{vid}{suffix}"
                if src.exists():
                    shutil.move(src, OLDER / "transcripts" / src.name)
            audio.unlink(missing_ok=True)
            say(f"[{k}/{n}] {vid} done")
        else:
            shutil.move(audio, waiting)
            if not stop.is_set():
                say(f"[{k}/{n}] {vid} FAILED (see logs/older-process.log); stopping")
                stop.set()
            return
    left = [y for y in ids if not done(y)]
    say(f"older queue finished: {n - len(left)} of {n} transcribed" + (f"; {len(left)} had no audio" if left else ""))


# ---------------------------------------------------------------- thermostat and status
def gpu() -> dict | None:
    r = run(["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,memory.used,power.draw", "--format=csv,noheader,nounits"])
    try:
        u, t, m, p = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
        return {"util": int(u), "temp": int(t), "vram": int(m), "watts": int(float(p))}
    except (IndexError, ValueError):
        return None


def set_paused(paused: bool) -> None:
    proc = state["proc"]
    if proc is None:
        return
    try:
        parent = psutil.Process(proc.pid)
        for p in [parent] + parent.children(recursive=True):
            p.suspend() if paused else p.resume()
        state["paused"] = paused
    except psutil.Error:
        pass


def thermostat() -> None:
    log("gpu-thermostat.log", f"thermostat on: pause at {HOT}C, resume at {COOL}C")
    while not stop.wait(10):
        g = gpu()
        if not g:
            continue
        if not state["paused"] and g["temp"] >= HOT and state["proc"] is not None:
            set_paused(True)
            log("gpu-thermostat.log", f"{g['temp']}C: paused transcription")
        elif state["paused"] and g["temp"] <= COOL:
            set_paused(False)
            log("gpu-thermostat.log", f"{g['temp']}C: resumed")


def status_line() -> str:
    g = gpu() or {"util": "?", "temp": "?", "vram": "?", "watts": "?"}
    cpu = round(psutil.cpu_percent(interval=3))
    vm = psutil.virtual_memory()
    job = state["job"]
    if state["proc"] is not None:
        job += f" {state['vod']} {int((time.time() - state['started']) / 60)}/{state['est_min'] or '?'}m"
    hot = " PAUSED(hot)" if state["paused"] else ""
    return (f"{dt.datetime.now():%H:%M} GPU {g['util']}% {g['temp']}C {g['watts']}W vram {g['vram']}M{hot} | CPU {cpu}% | "
            f"RAM {(vm.total - vm.available) / 2**30:.1f}/{vm.total / 2**30:.1f}G | {job}")


def status_loop() -> None:
    while True:
        with open(LOGS / "live-status.log", "a", encoding="utf-8") as f:
            f.write(status_line() + "\n")
        if stop.wait(117):
            return


# ---------------------------------------------------------------- main
def deadline(hhmm: str) -> dt.datetime:
    h, m = map(int, hhmm.split(":"))
    d = dt.datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
    return d if d > dt.datetime.now() else d + dt.timedelta(days=1)


def watchdog(until: dt.datetime | None) -> None:
    while not stop.wait(5):
        if STOP_FILE.exists():
            log("night-runner.log", "stop requested (runner.py stop)")
            stop.set()
        elif until and dt.datetime.now() >= until:
            log("night-runner.log", f"stop time {until:%H:%M} reached")
            stop.set()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "status", "stop"])
    ap.add_argument("--until", help="HH:MM local time to stop at (next occurrence)")
    ap.add_argument("--only", choices=["backfill", "older"], help="run just one of the two queues")
    a = ap.parse_args()
    LOGS.mkdir(exist_ok=True)

    if a.cmd == "status":
        last = (LOGS / "live-status.log").read_text(encoding="utf-8").splitlines()[-1:] if (LOGS / "live-status.log").exists() else []
        print("last logged:", *last)
        print("now:        ", status_line().rsplit(" | ", 1)[0])
        for f in ("pc-status.txt", "older-status.txt"):
            if (LOGS / f).exists():
                print(f"{f}: {(LOGS / f).read_text(encoding='utf-8').strip()}")
        return
    if a.cmd == "stop":
        STOP_FILE.write_text(now(), encoding="utf-8")
        print("stop requested; the runner exits within a few seconds")
        return

    lock = open(LOGS / "runner.lock", "w")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("another runner is already going; exiting", file=sys.stderr)
        return
    STOP_FILE.unlink(missing_ok=True)
    until = deadline(a.until) if a.until else None
    log("night-runner.log", f"windows runner start (pid {os.getpid()}), until {until:%F %H:%M}" if until
        else f"windows runner start (pid {os.getpid()}), no stop time")
    for target, args in ((watchdog, (until,)), (thermostat, ()), (status_loop, ())):
        threading.Thread(target=target, args=args, daemon=True).start()
    try:
        if a.only != "older" and not stop.is_set():
            backfill()
        if a.only != "backfill" and not stop.is_set():
            older()
    finally:
        stopped = stop.is_set()
        stop.set()
        for leftover in (OLDER / "audio").glob("*.part.*") if (OLDER / "audio").exists() else []:
            leftover.unlink(missing_ok=True)
        state.update(job="idle")
        msg = "stopped: transcription is off until the next run" if stopped else "all queues finished"
        for f in ("pc-status.txt", "older-status.txt"):
            (LOGS / f).write_text(msg + "\n", encoding="utf-8")
        STOP_FILE.unlink(missing_ok=True)
        log("night-runner.log", f"windows runner end ({msg})")


if __name__ == "__main__":
    main()
