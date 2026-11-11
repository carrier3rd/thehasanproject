#!/usr/bin/env python3
"""Transcribe pending VODs on a Colab GPU through Google's Colab CLI (no notebook, no Drive). Runs on the laptop.

    deploy/colab/cli_run.py                  # one batch of up to 10 pending VODs on a T4
    deploy/colab/cli_run.py --batch 5 --gpu L4
    deploy/colab/cli_run.py --loop           # batch after batch until nothing is pending
    deploy/colab/cli_run.py --stop           # stop the runtime (billing) and exit

Needs the CLI at ~/.local/share/colab-cli-venv/bin/colab (or $COLAB_CLI), signed in once by a person with
`colab --auth=oauth2 sessions`. Each batch: start a runtime, send the code + config + voiceprints + HF_TOKEN (only that
line of .env), install the GPU packages in the background while the audio uploads, run process.py detached on the
runtime, poll every minute and download each finished transcript, then stop the runtime (always, even on errors).
Progress goes to ~/.cache/colab-status.txt, which the Claude Code status line shows. Safe to re-run: transcripts
already on the laptop are never sent again, and a run that finds the session still busy picks up where it was.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRIV = Path(os.environ.get("VOD_ARCHIVE_PRIVATE_DIR", ROOT.parent / "thehasanprojectprivate"))
CLI = os.environ.get("COLAB_CLI", str(Path.home() / ".local/share/colab-cli-venv/bin/colab"))
STATUS = Path.home() / ".cache/colab-status.txt"
REMOTE = "/content/vod"
SESSION = "vod"
RTF_GUESS = 10.7          # realtime factor of large-v3 on a T4 (pilot); refined from logs/process_times.tsv
UPLOAD_GUESS = 1.8e6      # bytes/s, measured 2026-11-10
POLL = 60
PART = 40 * 1024 * 1024   # the CLI uploads a file as one base64 request; Colab's proxy rejected 189 MB (400), took 50 MB


# ---------------------------------------------------------------- progress (pure functions, tested)

def parse_progress(log: str) -> dict:
    """Read process.py's log: which VOD of the batch, which chunk, which stage, and VODs finished."""
    out = {"vod_k": 0, "vod_n": 0, "vid": "", "chunk_i": 0, "chunk_n": 0, "stage": "", "finished": []}
    for m in re.finditer(r"VOD (\d+)/(\d+): (\S+)", log):
        out["vod_k"], out["vod_n"], out["vid"] = int(m[1]), int(m[2]), m[3]
    out["finished"] = re.findall(r"(\S+): [\d.]+ h in \d+ min \([\d.]+x realtime\)", log)
    if out["vid"]:
        for m in re.finditer(re.escape(out["vid"]) + r" chunk (\d+)/(\d+)(?:: ([a-z ]+)| (done))", log):
            out["chunk_i"], out["chunk_n"], out["stage"] = int(m[1]), int(m[2]), (m[3] or m[4]).strip()
    return out


def batch_fraction(p: dict, hours: dict[str, float]) -> float:
    """Share of the batch's audio hours done: finished VODs + the running VOD's finished chunks."""
    total = sum(hours.values()) or 1.0
    done = sum(hours.get(v, 0) for v in p["finished"])
    if p["vid"] and p["vid"] not in p["finished"] and p["chunk_n"]:
        within = (p["chunk_i"] - (0 if p["stage"] == "done" else 1)) / p["chunk_n"]
        done += hours.get(p["vid"], 0) * within
    return min(done / total, 1.0)


def bar(frac: float, width: int = 10) -> str:
    f = int(max(0.0, min(frac, 1.0)) * width)   # round down: full only at 100%
    return "[" + "#" * f + "-" * (width - f) + f"] {frac:.0%}"


def hm(seconds: float) -> str:
    s = max(int(seconds), 0)
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


# ---------------------------------------------------------------- laptop-side state

def inventory_hours() -> dict[str, float]:
    import csv
    path = ROOT / "logs/inventory.tsv"
    if not path.exists():
        return {}
    return {r["id"]: float(r["duration"] or 0) / 3600 for r in csv.DictReader(open(path), delimiter="\t")}


def audio_hours(vid: str) -> float:
    try:
        return float(json.loads((ROOT / f"data/audio/{vid}.info.json").read_text()).get("duration") or 0) / 3600
    except (OSError, ValueError):
        return inventory_hours().get(vid, 0.0)


def overall() -> str:
    inv = inventory_hours()
    done = {p.stem for p in (ROOT / "data/transcripts").glob("*.json")}
    have = {p.stem for p in (ROOT / "data/audio").glob("*.opus")}
    h_all = sum(inv.values()) or 1.0
    h_done = sum(h for v, h in inv.items() if v in done)
    return (f"backfill {bar(h_done / h_all)} {len(done & inv.keys())}/{len(inv)} VODs, "
            f"{h_done:.0f}/{h_all:.0f} h · audio {len(have & inv.keys())}/{len(inv)}")


_last = {"line": ""}


def _write_status() -> None:
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(f"☁ Colab: {_last['line']} · {overall()}")
    tmp.replace(STATUS)


def say(line: str) -> None:
    """One status line for the Claude Code status bar (and the terminal)."""
    _last["line"] = line
    _write_status()
    print(time.strftime("%H:%M:%S"), line, flush=True)


def _heartbeat() -> None:
    """Rewrite the status file every 30 s: the status line hides it once it is 2 min old (e.g. during an upload)."""
    while True:
        time.sleep(30)
        if _last["line"]:
            try:
                _write_status()
            except OSError:
                pass


def measured_rtf() -> float:
    path = ROOT / "logs/process_times.tsv"
    try:
        vals = [float(l.split("\t")[4]) for l in path.read_text().splitlines()[1:] if l.count("\t") >= 4]
        return sum(vals) / len(vals) if vals else RTF_GUESS
    except (OSError, ValueError):
        return RTF_GUESS


# ---------------------------------------------------------------- Colab CLI wrappers

class CliCrashed(RuntimeError):
    """The colab CLI process itself died on a signal (it once segfaulted, -11, mid-batch): the runtime is fine."""


def colab(*args: str, check: bool = True, timeout: int = 900, stdin: str | None = None, both: bool = False) -> str:
    cmd = [CLI, "--auth=oauth2", *args]
    r = subprocess.run(cmd, input=stdin if stdin is not None else "", capture_output=True, text=True, timeout=timeout)
    if check and r.returncode < 0:
        raise CliCrashed(f"colab {' '.join(args[:2])} crashed (signal {-r.returncode}): {redact((r.stderr or r.stdout)[-800:])}")
    if check and r.returncode:
        raise RuntimeError(f"colab {' '.join(args[:2])} failed ({r.returncode}): {redact((r.stderr or r.stdout)[-800:])}")
    return r.stdout + ("\n" + r.stderr if both and r.stderr else "")


def redact(text: str) -> str:
    """Error messages from the CLI include the runtime's proxy URL with a short-lived access token."""
    return re.sub(r"(token=)[A-Za-z0-9._-]+", r"\1<redacted>", text)


def upload(local: Path, remote: str, progress=lambda done: None) -> None:
    """Upload in PART-sized pieces, join them on the runtime and check the SHA-256."""
    size, digest = local.stat().st_size, hashlib.sha256()
    parts = []
    with open(local, "rb") as f, tempfile.TemporaryDirectory(prefix="colab-up-") as tmp:
        i = 0
        while chunk := f.read(PART):
            digest.update(chunk)
            piece = Path(tmp) / f"part{i:04d}"
            piece.write_bytes(chunk)
            rp = f"{remote}.part{i:04d}"
            colab("upload", "-s", SESSION, str(piece), rp, timeout=1800)
            piece.unlink()
            parts.append(rp)
            i += 1
            progress(min(i * PART, size))
    # safe to repeat (a join once hung 10 min): the pieces are deleted only after the joined file checks out
    want = digest.hexdigest()
    res = rpy(f"""
import hashlib, json, os
parts, out, want = {parts!r}, {remote!r}, {want!r}
def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 24): h.update(b)
    return h.hexdigest()
if not (os.path.exists(out) and sha(out) == want):
    with open(out + ".tmp", "wb") as o:
        for p in parts:
            with open(p, "rb") as f: o.write(f.read())
    os.replace(out + ".tmp", out)
got = sha(out)
if got == want:
    for p in parts:
        if os.path.exists(p): os.remove(p)
print("@@ " + json.dumps({{"sha": got, "size": os.path.getsize(out)}}))
""", timeout=300, tries=3)
    if res["sha"] != want or res["size"] != size:
        raise RuntimeError(f"upload of {local.name} arrived damaged ({res['size']} of {size} bytes)")


def rpy(code: str, timeout: int = 120, tries: int = 3) -> dict:
    """Run Python in the session's kernel; the code must print one line '@@ <json>'.
    Retried on a hang (a fresh runtime once hung 5 min on its first exec) or any CLI failure: the laptop's CLI has
    segfaulted and once failed to import its own charset_normalizer, both while the runtime was fine. Every snippet
    sent must therefore be safe to run twice."""
    compile(code, "<runtime code>", "exec")   # fail here, with a traceback, rather than as a silent remote error
    for attempt in range(1, tries + 1):
        try:
            out = colab("exec", "-s", SESSION, stdin=code, timeout=timeout, both=True)
            break
        except (subprocess.TimeoutExpired, RuntimeError) as e:   # every call is safe to repeat
            if attempt == tries:
                raise RuntimeError(f"the runtime did not answer ({timeout} s limit), {tries} times: {e}") from e
            why = ("colab CLI crashed" if isinstance(e, CliCrashed) else "runtime slow to answer"
                   if isinstance(e, subprocess.TimeoutExpired) else f"colab CLI failed ({str(e)[-120:].strip()})")
            print(time.strftime("%H:%M:%S"), f"{why}; retry {attempt}/{tries - 1}", flush=True)
            time.sleep(20)
    for line in reversed(out.splitlines()):
        if line.startswith("@@ "):
            return json.loads(line[3:])
    raise RuntimeError(f"no result from the runtime:\n{redact(out[-1500:]) or '(no output at all)'}")


def session_alive() -> bool:
    r = subprocess.run([CLI, "--auth=oauth2", "status", "-s", SESSION], input="", capture_output=True, text=True,
                       timeout=120)
    return r.returncode == 0 and "Status:" in r.stdout


def start_job(name: str, shell: str) -> None:
    """Start a detached shell job on the runtime; it survives this CLI call. Exit code lands in logs/<name>.exit."""
    script = (f"cd {REMOTE} && set -a && . ./.env && set +a && export VOD_ARCHIVE_PRIVATE_DIR=private "
              f"PYTHONUNBUFFERED=1 && {{ {shell} ; }} > logs/{name}.log 2>&1; echo $? > logs/{name}.exit")
    # safe to repeat: a retried call finds this call's token and does not start a second copy
    token = uuid.uuid4().hex
    rpy(f"""
import subprocess, json, os
d = {REMOTE!r} + "/logs/"
os.makedirs(d, exist_ok=True)
tok = d + "{name}.token"
if not (os.path.exists(tok) and open(tok).read() == "{token}"):
    for f in ("{name}.exit", "{name}.log"):
        if os.path.exists(d + f): os.remove(d + f)
    subprocess.Popen(["bash", "-c", {script!r}], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    open(tok, "w").write("{token}")
print("@@ {{}}")
""", tries=3)


def job_state(name: str, log_tail: int = 0) -> dict:
    return rpy(tries=5, code=f"""
import json, os, glob
d = {REMOTE!r} + "/logs/"
ex = open(d + "{name}.exit").read().strip() if os.path.exists(d + "{name}.exit") else None
log = open(d + "{name}.log", errors="replace").read() if os.path.exists(d + "{name}.log") else ""
if len(log) > 2_000_000:   # keep the start (VOD lines) readable: drop whisper's progress noise
    log = chr(10).join(l for l in log.splitlines() if "Progress:" not in l)[-2_000_000:]
out = {{"exit": ex, "log": log if {log_tail} == 0 else log[-{log_tail}:],
        "done": sorted(os.path.basename(p)[:-5] for p in glob.glob({REMOTE!r} + "/data/transcripts/*.json"))}}
print("@@ " + json.dumps(out))
""")


# ---------------------------------------------------------------- steps

def pending_ids(n: int) -> list[str]:
    out = subprocess.run([str(ROOT / ".venv/bin/python"), "pipeline/pending.py", "--batch", str(n)], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [Path(l).stem for l in out.split() if l.endswith(".opus")]


def code_bundle() -> Path:
    """git-tracked files + private config + voiceprints + .env with HF_TOKEN only."""
    tmp = Path(tempfile.mkdtemp(prefix="colab-code-"))
    tar = tmp / "code.tar.gz"
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
    token = ""
    for line in (ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else []:
        m = re.match(r"^(?:export +)?HF_TOKEN=(.*)$", line.strip())
        if m:
            token = m[1].strip().strip("'\"")
    if not token:
        raise SystemExit("no HF_TOKEN in .env; diarization would fail")
    env = tmp / ".env"
    env.touch(mode=0o600)
    env.write_text(f"HF_TOKEN={token}\n")
    with tarfile.open(tar, "w:gz") as t:
        for f in filter(None, files):
            if (ROOT / f).is_file():
                t.add(ROOT / f, arcname=f)
        if (PRIV / "config.yaml").exists():
            t.add(PRIV / "config.yaml", arcname="private/config.yaml")
        for vp in sorted((ROOT / "voiceprints").glob("*.npy")):
            t.add(vp, arcname=f"voiceprints/{vp.name}")
        t.add(env, arcname=".env")
    env.unlink()
    return tar


def fetch(vids: list[str], got: set[str]) -> None:
    dest = ROOT / "data/transcripts"
    for vid in vids:
        if vid in got:
            continue
        for ext in (".emb.npz", ".json"):     # json last: its presence marks the VOD as done locally
            if not (dest / f"{vid}{ext}").exists():
                colab("download", "-s", SESSION, f"{REMOTE}/data/transcripts/{vid}{ext}", str(dest / f"{vid}{ext}"))
        got.add(vid)


def merge_logs() -> None:
    for name in ("process_times.tsv", "failed.tsv"):
        tmp = Path(tempfile.mktemp(suffix=name))
        r = subprocess.run([CLI, "--auth=oauth2", "download", "-s", SESSION, f"{REMOTE}/logs/{name}", str(tmp)],
                           input="", capture_output=True, text=True, timeout=300)
        if r.returncode or not tmp.exists():
            continue
        local = ROOT / "logs" / name
        old = local.read_text().splitlines() if local.exists() else []
        new = [l for l in tmp.read_text().splitlines() if l not in set(old)]
        if new:
            if old:
                new = [l for l in new if not l.startswith(("id\t", "timestamp\t"))]
            with open(local, "a") as f:
                f.write("".join(l + "\n" for l in new))
        tmp.unlink()


def stop_runtime(tries: int = 5) -> bool:
    """Stop the session and check it is gone; a stop once failed silently because the CLI itself crashed."""
    for attempt in range(tries):
        try:
            subprocess.run([CLI, "--auth=oauth2", "stop", "-s", SESSION], input="", capture_output=True, text=True,
                           timeout=300)
            r = subprocess.run([CLI, "--auth=oauth2", "sessions"], input="", capture_output=True, text=True,
                               timeout=120)
            if r.returncode == 0 and f"[{SESSION}]" not in r.stdout:   # positively gone, not a crashed check
                return True
        except (subprocess.TimeoutExpired, OSError):
            pass
        time.sleep(20)
    return False


def run_batch(ids: list[str], gpu: str, label: str) -> int:
    hours = {v: audio_hours(v) for v in ids}
    resume = session_alive()
    if not resume:
        say(f"{label}: starting a {gpu} runtime")
        colab("new", "-s", SESSION, "--gpu", gpu, timeout=600)
        rpy("print('@@ {}')", timeout=180, tries=5)   # wait until the fresh kernel answers
    state = rpy(f"""
import json, os
print("@@ " + json.dumps({{"setup": os.path.exists("{REMOTE}/logs/setup.exit"),
  "job": os.path.exists("{REMOTE}/logs/job.log"), "job_done": os.path.exists("{REMOTE}/logs/job.exit")}}))
""")
    if resume and state["job"] and not state["job_done"]:
        # follow the job's own VOD list, not the laptop's next batch
        remote = rpy(f"import json, glob, os; print('@@ ' + json.dumps(sorted(os.path.basename(p)[:-5] "
                     f"for p in glob.glob('{REMOTE}/data/audio/*.opus'))))")
        # the runtime keeps earlier batches' audio: keep this job's VODs (in its log) and any not yet on the laptop
        in_log = set(re.findall(r"VOD \d+/\d+: (\S+)", job_state("job")["log"]))
        ids = [v for v in remote if (ROOT / f"data/audio/{v}.opus").exists()
               and (v in in_log or not (ROOT / f"data/transcripts/{v}.json").exists())]
        hours = {v: audio_hours(v) for v in ids}
        say(f"{label}: found the runtime still transcribing {len(ids)} VOD(s); following it")
    else:
        say(f"{label}: sending code, config, voiceprint")
        tar = code_bundle()
        rpy(f"import os, json; os.makedirs('{REMOTE}/data/audio', exist_ok=True); "
            f"os.makedirs('{REMOTE}/data/muted', exist_ok=True); print('@@ {{}}')")
        colab("upload", "-s", SESSION, str(tar), f"{REMOTE}/code.tar.gz")
        tar.unlink()
        tar.parent.rmdir()
        rpy(f"import subprocess, json, os\n"      # a retry finds the archive already unpacked and removed
            f"if os.path.exists('{REMOTE}/code.tar.gz'):\n"
            f"    subprocess.run(['tar', '-xzf', 'code.tar.gz'], cwd='{REMOTE}', check=True)\n"
            f"    os.chmod('{REMOTE}/.env', 0o600); os.remove('{REMOTE}/code.tar.gz')\n"
            f"print('@@ {{}}')")
        if not state["setup"]:
            # same check as the notebook: skip pip when every pinned version is already there
            start_job("setup", "pip install --dry-run --no-deps -r requirements-gpu.txt 2>&1 | grep -q 'Would install' "
                               "&& pip install -r requirements-gpu.txt || echo 'packages already installed'")
        sizes = {v: (ROOT / f"data/audio/{v}.opus").stat().st_size for v in ids}
        total, sent, t0 = sum(sizes.values()), 0, time.time()
        for i, vid in enumerate(ids, 1):
            def shown(done, i=i, vid=vid, base=sent):
                now = base + done
                rate = now / (time.time() - t0) if now else UPLOAD_GUESS
                say(f"{label}: uploading audio {i}/{len(ids)} {vid} {bar(now / total)} "
                    f"({sizes[vid] / 1e6:.0f} MB, ~{hm((total - now) / rate)} left)")
            shown(0)
            upload(ROOT / f"data/audio/{vid}.opus", f"{REMOTE}/data/audio/{vid}.opus", shown)
            muted = ROOT / f"data/muted/{vid}.json"
            if muted.exists():
                colab("upload", "-s", SESSION, str(muted), f"{REMOTE}/data/muted/{vid}.json")
            sent += sizes[vid]
        while True:
            s = job_state("setup", log_tail=300)
            if s["exit"] is not None:
                break
            last = s["log"].strip().splitlines()[-1:] or [""]
            say(f"{label}: installing GPU packages (~5 min) · {last[0][:60]}")
            time.sleep(20)
        if s["exit"] != "0":
            raise RuntimeError(f"package install failed:\n{s['log'][-1500:]}")
        say(f"{label}: starting transcription of {len(ids)} VOD(s), {sum(hours.values()):.1f} h of audio")
        start_job("job", "nvidia-smi --query-gpu=name --format=csv,noheader; python pipeline/process.py")

    rtf, got, t_start = measured_rtf(), set(), time.time()
    while True:
        s = job_state("job")
        p = parse_progress(s["log"])
        # only VODs whose "x realtime" line is logged: their json is completely written
        fetch([v for v in s["done"] if v in ids and v in p["finished"]], got)
        frac = batch_fraction(p, hours)
        left = sum(hours.values()) * 3600 * (1 - frac) / rtf
        if s["exit"] is not None:
            break
        where = ("loading models" if not p["vid"] else
                 f"VOD {p['vod_k']}/{p['vod_n']} {p['vid']} loading audio" if not p["chunk_n"] else
                 f"VOD {p['vod_k']}/{p['vod_n']} {p['vid']} chunk {p['chunk_i']}/{p['chunk_n']} {p['stage']}")
        say(f"{label}: {where} · batch {bar(frac)} · ~{hm(left)} left · {len(got)} back on laptop")
        time.sleep(POLL)
    merge_logs()
    fetch([v for v in s["done"] if v in ids], got)
    missing = [v for v in ids if v not in got]
    if s["exit"] != "0" or missing:
        tail = "\n".join(s["log"].splitlines()[-30:])
        say(f"{label}: ✖ process.py exit {s['exit']}; {len(got)}/{len(ids)} back, missing {' '.join(missing) or '-'}")
        print(tail, file=sys.stderr)
        return 1
    say(f"{label}: ✔ {len(got)} VOD(s) transcribed in {hm(time.time() - t_start)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--batch", type=int, default=10, help="VODs per batch (default 10)")
    ap.add_argument("--gpu", default="T4", help="T4 (default), L4, A100, H100, G4")
    ap.add_argument("--loop", action="store_true", help="keep running batches until nothing is pending")
    ap.add_argument("--keep", action="store_true", help="don't stop the runtime at the end (debugging)")
    ap.add_argument("--stop", action="store_true", help="stop the runtime and exit")
    a = ap.parse_args()
    if a.stop:
        ok = stop_runtime()
        say("runtime stopped" if ok else "✖ could not stop the runtime")
        return 0 if ok else 1
    status, n = 0, 0
    threading.Thread(target=_heartbeat, daemon=True).start()
    try:
        while True:
            ids = pending_ids(a.batch)
            if not ids:
                say("✔ nothing left to transcribe" if n else "nothing to transcribe")
                break
            n += 1
            status = run_batch(ids, a.gpu, f"batch {n}")
            if status or not a.loop:
                break
    except KeyboardInterrupt:
        say("stopped by Ctrl+C")
        status = 130
    except Exception as e:  # noqa: BLE001 - always report, then stop the runtime below
        say(f"✖ {redact(str(e)).splitlines()[0][:120]}")
        print(redact(str(e)), file=sys.stderr)
        status = 1
    finally:
        if not a.keep:
            print("runtime stopped" if stop_runtime() else "✖ COULD NOT STOP THE RUNTIME: run cli_run.py --stop",
                  flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
