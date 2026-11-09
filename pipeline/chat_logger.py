"""24/7 anonymous Twitch IRC logger -> archive.db (live_msgs, sessions)."""
from __future__ import annotations

import argparse
import random
import socket
import time

from common import P, connect_archive, get_logger, load_config, make_dirs

log = get_logger("chat_logger")
_UNESC = {"s": " ", ":": ";", "\\": "\\", "r": "\r", "n": "\n"}


def unescape_tag(v: str) -> str:
    out, i = [], 0
    while i < len(v):
        if v[i] == "\\" and i + 1 < len(v):
            out.append(_UNESC.get(v[i + 1], v[i + 1]))
            i += 2
        else:
            out.append(v[i])
            i += 1
    return "".join(out)


def parse_line(line: str) -> dict:
    """Parse one IRC line into {tags, prefix, command, params}."""
    tags, prefix = {}, ""
    if line.startswith("@"):
        raw, line = line[1:].split(" ", 1)
        for kv in raw.split(";"):
            k, _, v = kv.partition("=")
            tags[k] = unescape_tag(v)
    if line.startswith(":"):
        prefix, line = line[1:].split(" ", 1)
    head, sep, trailing = line.partition(" :")
    parts = head.split()
    params = parts[1:] + ([trailing] if sep else [])
    return {"tags": tags, "prefix": prefix, "command": parts[0] if parts else "", "params": params}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class Store:
    """Batched writes plus session tracking."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        make_dirs(cfg)
        self.con = connect_archive(cfg)
        self.gap_ms = cfg["session_gap_minutes"] * 60_000
        self.pending = 0
        self.last_commit = time.monotonic()
        self.sessions = {}  # channel -> [rowid, start_ms, end_ms, dirty]
        for ch in cfg["log_channels"]:
            r = self.con.execute("SELECT rowid, start_ms, end_ms FROM sessions WHERE channel=? "
                                 "ORDER BY start_ms DESC LIMIT 1", (ch,)).fetchone()
            if r:
                self.sessions[ch] = [r[0], r[1], r[2], False]

    def _touch_session(self, channel: str, ts: int):
        s = self.sessions.get(channel)
        if s is None or ts - s[2] >= self.gap_ms:
            cur = self.con.execute("INSERT INTO sessions(channel, start_ms, end_ms) VALUES(?,?,?)", (channel, ts, ts))
            self.sessions[channel] = [cur.lastrowid, ts, ts, False]
        elif ts > s[2]:
            s[2], s[3] = ts, True

    def handle(self, m: dict):
        cmd, tags, params = m["command"], m["tags"], m["params"]
        if not params:
            return
        channel = params[0].lstrip("#")
        ts = _int(tags.get("tmi-sent-ts")) or int(time.time() * 1000)
        if cmd == "PRIVMSG" and len(params) > 1:
            text = params[1]
            if text.startswith("\x01ACTION ") and text.endswith("\x01"):
                text = "/me " + text[8:-1]
            self.con.execute(
                "INSERT INTO live_msgs(ts_ms, channel, room_id, msg_id, user_id, author, text, badges, "
                "first_msg, returning_chatter, subscriber) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ts, channel, tags.get("room-id"), tags.get("id"), tags.get("user-id"),
                 m["prefix"].split("!")[0], text, tags.get("badges"), _int(tags.get("first-msg")),
                 _int(tags.get("returning-chatter")), _int(tags.get("subscriber"))))
            self._touch_session(channel, ts)
        elif cmd == "CLEARMSG":
            self.con.execute("UPDATE live_msgs SET deleted=1 WHERE msg_id=?", (tags.get("target-msg-id"),))
        elif cmd == "CLEARCHAT" and len(params) > 1:
            dur = tags.get("ban-duration")
            self.con.execute(
                "INSERT INTO live_msgs(ts_ms, channel, room_id, user_id, author, text, kind) VALUES(?,?,?,?,?,?,?)",
                (ts, channel, tags.get("room-id"), tags.get("target-user-id"), params[1],
                 f"timeout {dur}s" if dur else "ban", "mod_action"))
            if self.cfg.get("hide_messages_on_ban", True) and tags.get("target-user-id"):
                # Twitch removes a timed-out/banned user's messages from chat; mirror that
                s = self.sessions.get(channel)
                since = s[1] if s and ts - s[2] < self.gap_ms else ts - self.gap_ms
                self.con.execute("UPDATE live_msgs SET deleted=1 WHERE channel=? AND user_id=? AND kind='msg' "
                                 "AND ts_ms BETWEEN ? AND ?", (channel, tags["target-user-id"], since, ts))
        else:
            return
        self.pending += 1

    def maybe_commit(self, force: bool = False):
        due = time.monotonic() - self.last_commit >= self.cfg["logger_batch_seconds"]
        if self.pending and (force or due or self.pending >= self.cfg["logger_batch_messages"]):
            for s in self.sessions.values():
                if s[3]:
                    self.con.execute("UPDATE sessions SET end_ms=? WHERE rowid=?", (s[2], s[0]))
                    s[3] = False
            self.con.commit()
            self.pending = 0
            self.last_commit = time.monotonic()


def run_once(cfg: dict, store: Store, deadline: float | None = None) -> None:
    """One connection lifetime. Raises on any socket error; returns only at `deadline`."""
    sock = socket.create_connection((cfg["irc_host"], cfg["irc_port"]), timeout=30)
    try:
        sock.settimeout(1.0)

        def send(s):
            sock.sendall((s + "\r\n").encode())

        send("CAP REQ :twitch.tv/tags twitch.tv/commands")
        send(f"NICK justinfan{random.randint(10000, 99999)}")
        for ch in cfg["log_channels"]:
            send(f"JOIN #{ch.lower()}")
        log.info("connected; joined %s", ", ".join(cfg["log_channels"]))
        buf, last_rx = b"", time.monotonic()
        heartbeat, beat = P(cfg, "logs") / "chat_logger.heartbeat", 0.0
        while deadline is None or time.monotonic() < deadline:
            try:
                data = sock.recv(65536)
                if not data:
                    raise ConnectionError("server closed the connection")
                last_rx = time.monotonic()
                buf += data
            except socket.timeout:
                if time.monotonic() - last_rx > 360:  # Twitch pings about every 5 min
                    raise ConnectionError("no data for 6 minutes")
            while b"\r\n" in buf:
                raw, buf = buf.split(b"\r\n", 1)
                line = raw.decode("utf-8", "replace")
                if line.startswith("PING"):
                    send("PONG" + line[4:])
                    continue
                try:
                    m = parse_line(line)
                    if m["command"] == "RECONNECT":
                        raise ConnectionError("server requested reconnect")
                    store.handle(m)
                except ConnectionError:
                    raise
                except Exception as e:  # one bad line must not kill the logger
                    log.warning("unparsed line (%s): %.200s", e, line)
            store.maybe_commit()
            if time.monotonic() - beat > 60:
                heartbeat.write_text(str(int(time.time())))
                beat = time.monotonic()
    finally:
        store.maybe_commit(force=True)
        sock.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, help="stop after this long (for testing)")
    a = ap.parse_args()
    cfg = load_config()
    make_dirs(cfg)
    store = Store(cfg)
    deadline = time.monotonic() + a.seconds if a.seconds else None
    backoff = 1.0
    while deadline is None or time.monotonic() < deadline:
        started = time.monotonic()
        try:
            run_once(cfg, store, deadline)
        except KeyboardInterrupt:
            break
        except Exception as e:
            if time.monotonic() - started > 60:
                backoff = 1.0
            log.warning("disconnected (%s); reconnecting in %.0fs", e, backoff)
            time.sleep(backoff if deadline is None else min(backoff, max(deadline - time.monotonic(), 0)))
            backoff = min(backoff * 2, 300.0)


if __name__ == "__main__":
    main()
