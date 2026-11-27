"""Keep one long-running program alive on the Windows PC: the PC's stand-in for a systemd service with Restart=always.
Added Nov 26 2026, when every always-on job moved off the laptop (its RAM corrupts data).

  python keep_running.py run NAME [--cwd DIR] [--env-file FILE] [--log FILE] -- COMMAND ...
  python keep_running.py stop NAME        ask a running copy to stop its program and exit (waits up to 60 s)
  python keep_running.py status           every program it knows: running or not, pid, since when

- One copy per NAME: a second start exits at once, so a scheduled task can re-launch it every few minutes as a watchdog.
- The program is restarted 10 s after it exits; if it keeps dying within a minute the wait doubles, up to 5 minutes.
- While it runs, Windows is told the system is in use (SetThreadExecutionState), so idle sleep can't stop it. The
  systemd units used systemd-inhibit for the same thing. Closing the lid or a battery shutdown still can.
- --env-file: KEY=VALUE lines added to the environment (systemd's EnvironmentFile).
- Output goes to --log (default %LOCALAPPDATA%\\keep_running\\NAME.log), moved to NAME.log.1 at a restart once
  it is over 50 MB.
- Stopping: `stop NAME` writes NAME.stop; the supervisor ends the program (terminate, then kill after 15 s) and exits.
  Windows has no SIGTERM, so the program gets no chance to clean up; every program run this way must survive being
  killed (the loggers save a cursor or append whole lines; SQLite rolls back an unfinished write).

The same file is in socialscope-private/ops/pc/keep_running.py; the state folder is shared, so `stop` and `status`
work for programs started from either copy.
"""
import argparse
import ctypes
import datetime as dt
import json
import msvcrt
import os
import subprocess
import sys
import time

STATE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "keep_running")
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def paths(name):
    b = os.path.join(STATE, name)
    return {"lock": b + ".lock", "stop": b + ".stop", "info": b + ".json", "log": b + ".log"}


def read_env(path):
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            env[k] = v
    return env


def run(args):
    os.makedirs(STATE, exist_ok=True)
    p = paths(args.name)
    lock = open(p["lock"], "a+")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return 0                                    # already running: the watchdog trigger found it alive
    log_path = args.log or p["log"]
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)

    def say(msg):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{now()} [keep_running {args.name}] {msg}\n")

    try:
        os.remove(p["stop"])                        # a stale stop request from before must not stop this start
    except FileNotFoundError:
        pass
    env = dict(os.environ)
    if args.env_file:
        env.update(read_env(args.env_file))
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    delay, started_at = 10, now()
    say(f"supervisor start (pid {os.getpid()}): {subprocess.list2cmdline(args.command)}")
    while True:
        if os.path.exists(log_path) and os.path.getsize(log_path) > 50 << 20:
            os.replace(log_path, log_path + ".1")
        out = open(log_path, "a", encoding="utf-8")
        t0 = time.time()
        try:
            child = subprocess.Popen(args.command, cwd=args.cwd, env=env, stdout=out, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL)
        except OSError as e:                        # missing program or folder: keep trying, it may come back
            out.close()
            delay = min(delay * 2, 300)
            say(f"could not start the program ({e}); trying again in {delay} s")
            for _ in range(delay):
                if os.path.exists(p["stop"]):
                    say("stopped on request")
                    os.remove(p["stop"])
                    return 0
                time.sleep(1)
            continue
        with open(p["info"], "w", encoding="utf-8") as f:
            json.dump({"name": args.name, "supervisor_pid": os.getpid(), "pid": child.pid, "since": started_at,
                       "child_since": now(), "command": args.command, "log": log_path}, f)
        stop = False
        while child.poll() is None:
            if os.path.exists(p["stop"]):
                stop = True
                child.terminate()
                try:
                    child.wait(15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                break
            time.sleep(2)
        out.close()
        if stop:
            say("stopped on request")
            os.remove(p["stop"])
            os.remove(p["info"])
            return 0
        ran = time.time() - t0
        delay = 10 if ran > 60 else min(delay * 2, 300)
        say(f"program exited with code {child.returncode} after {ran:.0f} s; restarting in {delay} s")
        for _ in range(delay):
            if os.path.exists(p["stop"]):
                break
            time.sleep(1)


def stop(name):
    p = paths(name)
    lock = open(p["lock"], "a+") if os.path.exists(p["lock"]) else None
    try:
        if lock:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            print(f"{name}: not running")
            return 0
    except OSError:
        pass
    if lock is None:
        print(f"{name}: not running")
        return 0
    lock.close()
    open(p["stop"], "w").close()
    for _ in range(60):
        time.sleep(1)
        if not os.path.exists(p["stop"]):
            print(f"{name}: stopped")
            return 0
    print(f"{name}: still running after 60 s (stop request left in place)")
    return 1


def status():
    if not os.path.isdir(STATE):
        return 0
    for fn in sorted(os.listdir(STATE)):
        if not fn.endswith(".lock"):
            continue
        name = fn[:-5]
        p = paths(name)
        with open(p["lock"], "a+") as lock:
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                running = False
            except OSError:
                running = True
        info = {}
        if running and os.path.exists(p["info"]):
            with open(p["info"], encoding="utf-8") as f:
                info = json.load(f)
        print(f"{name:<18} {'running' if running else 'stopped':<8} pid {info.get('pid', '-')!s:<7} "
              f"since {info.get('since', '-')}  program since {info.get('child_since', '-')}")
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "stop":
        return stop(sys.argv[2])
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        return status()
    argv = sys.argv[1:]
    if "--" not in argv:
        sys.exit("usage: keep_running.py run NAME [--cwd DIR] [--env-file FILE] [--log FILE] -- COMMAND ...")
    cut = argv.index("--")                          # split here: argparse.REMAINDER would swallow the options too
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["run"])
    ap.add_argument("name")
    ap.add_argument("--cwd")
    ap.add_argument("--env-file")
    ap.add_argument("--log")
    args = ap.parse_args(argv[:cut])
    args.command = argv[cut + 1:]
    if not args.command:
        ap.error("no command given after --")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
