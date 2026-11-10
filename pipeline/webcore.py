"""FastAPI app factory shared by the public app (public.db) and the admin app (archive.db)."""
from __future__ import annotations

import base64
import hmac
import json
import os
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from common import ROOT, Anchors, P, cfg_file
from search import Index, fts_execute, hybrid, window_results

PAGE = 50
RATE_LIMITED = ("/api/search", "/api/semantic", "/api/chat/search")


def _date(s: str | None) -> str | None:
    s = (s or "").replace("-", "").strip()
    return s if len(s) == 8 and s.isdigit() else None


def create_app(cfg: dict, mode: str = "public") -> FastAPI:
    assert mode in ("public", "admin")
    admin = mode == "admin"
    db_path = P(cfg, "archive_db" if admin else "public_db")
    app = FastAPI(title=f"vod-archive ({mode})", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.index = Index(P(cfg, "index"))
    app.state.embedder = None
    hits: dict[str, deque] = defaultdict(deque)
    static_dirs = ([ROOT / "admin" / "static"] if admin else []) + [ROOT / "web" / "static"]
    thr, margin = cfg["speaker_match_threshold"], cfg["low_confidence_margin"]

    def db() -> sqlite3.Connection:
        # a fresh read-only connection per request: always sees the current file after a public.db swap
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.OperationalError:
            raise HTTPException(503, "database not available yet")
        con.row_factory = sqlite3.Row
        return con

    def rows(cur) -> list[dict]:
        return [dict(r) for r in cur]

    need_password = not admin and cfg.get("site_password_required", True)
    site_user, site_password = os.environ.get("SITE_USER", "viewer"), os.environ.get("SITE_PASSWORD", "")

    def authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return False
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:
            return False
        return hmac.compare_digest(user.encode(), site_user.encode()) & hmac.compare_digest(pw.encode(), site_password.encode())

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if need_password:
            if not site_password:   # fail closed: never serve the site open by accident
                return JSONResponse({"detail": "site password not configured (SITE_PASSWORD in site.env)"}, status_code=503)
            if not authorized(request):
                return JSONResponse({"detail": "password required"}, status_code=401,
                                    headers={"WWW-Authenticate": 'Basic realm="VOD archive"'})
        if not admin and path.startswith(RATE_LIMITED):
            ip = request.client.host if request.client else "?"
            fwd = request.headers.get("x-forwarded-for")
            if fwd and ip in ("127.0.0.1", "::1"):
                ip = fwd.split(",")[-1].strip()   # set by our own reverse proxy
            now, q = time.monotonic(), hits[ip]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= cfg["rate_limit_per_minute"]:
                return JSONResponse({"detail": "rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"})
            q.append(now)
            if len(hits) > 50000:
                hits.clear()
        resp = await call_next(request)
        if cfg.get("robots_noindex", True) or admin:
            resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp

    @app.get("/api/config")
    def config(request: Request):
        domain = cfg.get("site_domain") or "auto"
        if domain == "auto":
            domain = request.url.hostname or "localhost"
        return {"mode": mode, "site_domain": domain, "chat_window_seconds": cfg["chat_window_seconds"],
                "contact_email": cfg.get("contact_email", ""), "semantic": len(app.state.index) > 0}

    turn_cols = ("t.speaker_score, t.speaker_raw, "
                 f"(t.speaker_score IS NOT NULL AND abs(t.speaker_score - {float(thr)}) < {float(margin)}) AS low_confidence"
                 if admin else "t.low_confidence")
    video_filter = " AND v.transcript_hash IS NOT NULL" if admin else ""

    @app.get("/api/search")
    def search(q: str = "", speaker: str = "", from_: str = Query("", alias="from"), to: str = "",
               include_media: int = 0, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        if not q.strip():
            return {"hits": []}
        where, params = "", []
        if speaker:
            where += " AND t.speaker = ?"
            params.append(speaker)
        if _date(from_):
            where += " AND v.upload_date >= ?"
            params.append(_date(from_))
        if _date(to):
            where += " AND v.upload_date <= ?"
            params.append(_date(to))
        if not include_media:
            where += " AND t.is_media = 0"
        con = db()
        try:
            got = fts_execute(con, f"""
                SELECT t.id, t.video_id, t.start, t.speaker, t.is_media, {turn_cols},
                       snippet(turns_fts, 0, '[[', ']]', ' … ', 32) AS snippet, v.title, v.upload_date
                FROM turns_fts JOIN turns t ON t.id = turns_fts.rowid JOIN videos v ON v.id = t.video_id
                WHERE turns_fts MATCH ? {where} ORDER BY bm25(turns_fts) LIMIT ? OFFSET ?""",
                              q, (*params, limit, offset))
            return {"hits": rows(got)}
        finally:
            con.close()

    @app.get("/api/semantic")
    def semantic(q: str = "", mode: str = "hybrid", k: int = Query(15, ge=1, le=50)):
        index: Index = app.state.index
        index.reload()
        if not q.strip():
            return {"hits": []}
        if not len(index):
            raise HTTPException(503, "semantic index not built yet")
        if app.state.embedder is None:
            try:
                from search import Embedder
                app.state.embedder = Embedder(cfg)
            except Exception:
                raise HTTPException(503, "embedding model unavailable")
        qvec = app.state.embedder.query(q)
        con = db()
        try:
            found = index.semantic(qvec, k) if mode == "semantic" else hybrid(con, index, q, qvec, k)
            return {"hits": window_results(con, index, found)}
        finally:
            con.close()

    @app.get("/api/videos")
    def videos(from_: str = Query("", alias="from"), to: str = "", page: int = Query(1, ge=1)):
        where, params = "WHERE 1=1" + video_filter, []
        if _date(from_):
            where += " AND v.upload_date >= ?"
            params.append(_date(from_))
        if _date(to):
            where += " AND v.upload_date <= ?"
            params.append(_date(to))
        con = db()
        try:
            total = con.execute(f"SELECT COUNT(*) FROM videos v {where}", params).fetchone()[0]
            got = con.execute(f"SELECT v.id, v.title, v.upload_date, v.duration, v.embed_type FROM videos v {where} "
                              "ORDER BY v.upload_date DESC, v.id DESC LIMIT ? OFFSET ?", (*params, PAGE, (page - 1) * PAGE))
            return {"videos": rows(got), "page": page, "pages": max((total + PAGE - 1) // PAGE, 1), "total": total}
        finally:
            con.close()

    def get_video(con, vid: str):
        v = con.execute("SELECT * FROM videos v WHERE v.id=?" + video_filter, (vid,)).fetchone()
        if not v:
            raise HTTPException(404, "no such video")
        return v

    @app.get("/api/videos/{vid}")
    def video(vid: str):
        con = db()
        try:
            v = dict(get_video(con, vid))
            for k in ("transcript_hash", "chat_sig"):
                v.pop(k, None)
            turns = con.execute(f"SELECT t.id, t.start, t.end, t.speaker, t.is_media, t.text, {turn_cols} "
                                "FROM turns t WHERE t.video_id=? ORDER BY t.start", (vid,))
            return {"video": v, "turns": rows(turns),
                    "muted": [[r[0], r[1]] for r in con.execute("SELECT start, end FROM muted WHERE video_id=? ORDER BY start", (vid,))]}
        finally:
            con.close()

    @app.get("/api/videos/{vid}/chat_stats")
    def chat_stats(vid: str):
        con = db()
        try:
            get_video(con, vid)
            if not admin:
                got = rows(con.execute("SELECT minute, msgs, distinct_chatters, first_msg_share, top_terms "
                                       "FROM chat_minutes WHERE video_id=? ORDER BY minute", (vid,)))
                for r in got:
                    r["top_terms"] = json.loads(r["top_terms"] or "[]")
                return {"minutes": got}
            anchors, per = Anchors.load(con, vid), defaultdict(lambda: [0, set(), 0])
            for r in con.execute("SELECT t, user_id, first_msg FROM chat WHERE video_id=? AND kind='msg'", (vid,)):
                t = anchors.to_transcript(r["t"])
                if t is not None and t >= 0:
                    m = per[int(t // 60)]
                    m[0] += 1
                    m[1].add(r["user_id"])
                    m[2] += 1 if r["first_msg"] else 0
            return {"minutes": [{"minute": k, "msgs": m[0], "distinct_chatters": len(m[1]),
                                 "first_msg_share": round(m[2] / m[0], 4), "top_terms": []} for k, m in sorted(per.items())]}
        finally:
            con.close()

    @app.get("/api/videos/{vid}/chat")
    def chat(vid: str, start: float = 0, end: float = 60):
        end = min(end, start + 600)
        con = db()
        try:
            get_video(con, vid)
            if not admin:
                return {"messages": rows(con.execute(
                    "SELECT id, t, author, badges, first_msg, subscriber, text, deleted, kind FROM chat "
                    "WHERE video_id=? AND t >= ? AND t < ? ORDER BY t, id", (vid, start, end)))}
            out = []
            for lo, hi, off in Anchors.load(con, vid).chat_ranges(start, end):
                for r in con.execute("SELECT id, t, user_id, author, badges, first_msg, returning_chatter, subscriber, text, "
                                     "deleted, kind, source FROM chat WHERE video_id=? AND t >= ? AND t < ? ORDER BY t, id",
                                     (vid, lo, hi)):
                    d = dict(r)
                    d["t"] = round(d["t"] - off, 3)
                    out.append(d)
            return {"messages": out}
        finally:
            con.close()

    @app.get("/api/chat/search")
    def chat_search(q: str = "", author: str = "", user_id: str = "", from_: str = Query("", alias="from"), to: str = "",
                    first_msg: int = 0, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        if not admin:
            user_id = ""
        if not (q.strip() or author.strip() or user_id.strip()):
            return {"hits": []}
        where, params = "", []
        if author.strip():
            where += " AND c.author = ? COLLATE NOCASE"
            params.append(author.strip().lstrip("@"))
        if user_id.strip():
            where += " AND c.user_id = ?"
            params.append(user_id.strip())
        if _date(from_):
            where += " AND v.upload_date >= ?"
            params.append(_date(from_))
        if _date(to):
            where += " AND v.upload_date <= ?"
            params.append(_date(to))
        if first_msg:
            where += " AND c.first_msg = 1"
        extra = ", c.user_id, c.source" if admin else ""
        con = db()
        try:
            if q.strip():
                got = fts_execute(con, f"""
                    SELECT c.id, c.video_id, c.t, c.author, c.badges, c.first_msg, c.deleted, c.kind{extra},
                           snippet(chat_fts, 0, '[[', ']]', ' … ', 32) AS snippet, v.title, v.upload_date
                    FROM chat_fts JOIN chat c ON c.id = chat_fts.rowid JOIN videos v ON v.id = c.video_id
                    WHERE chat_fts MATCH ? {where} ORDER BY bm25(chat_fts) LIMIT ? OFFSET ?""",
                                  q, (*params, limit, offset))
            else:
                got = con.execute(f"""
                    SELECT c.id, c.video_id, c.t, c.author, c.badges, c.first_msg, c.deleted, c.kind{extra},
                           c.text AS snippet, v.title, v.upload_date
                    FROM chat c JOIN videos v ON v.id = c.video_id
                    WHERE 1=1 {where} ORDER BY v.upload_date DESC, c.video_id, c.t LIMIT ? OFFSET ?""",
                                  (*params, limit, offset)).fetchall()
            out = rows(got)
            if admin:   # archive chat is on the chat clock; report transcript-clock time
                cache = {}
                for r in out:
                    a = cache.get(r["video_id"]) or cache.setdefault(r["video_id"], Anchors.load(con, r["video_id"]))
                    r["chat_t"], r["t"] = r["t"], a.to_transcript(r["t"])
            return {"hits": out}
        finally:
            con.close()

    @app.get("/api/speakers")
    def speakers():
        con = db()
        try:
            if admin:
                return {"speakers": [r[0] for r in con.execute("SELECT DISTINCT speaker FROM turns ORDER BY 1")]}
            return {"speakers": [r[0] for r in con.execute("SELECT name FROM speakers ORDER BY 1")]}
        finally:
            con.close()

    if admin:
        @app.get("/api/export")
        def export(video_id: str = "", from_: str = Query("", alias="from"), to: str = "", format: str = "jsonl"):
            if format != "jsonl":
                raise HTTPException(400, "only format=jsonl is supported")
            where, params = "WHERE 1=1", []
            if video_id:
                where += " AND c.video_id = ?"
                params.append(video_id)
            if _date(from_):
                where += " AND v.upload_date >= ?"
                params.append(_date(from_))
            if _date(to):
                where += " AND v.upload_date <= ?"
                params.append(_date(to))

            def gen():
                con = db()
                try:
                    for r in con.execute(f"""
                            SELECT c.video_id, v.upload_date, v.stream_start_unix, c.t, c.user_id, c.author, c.text, c.deleted,
                                   c.kind, c.badges, c.first_msg, c.returning_chatter, c.subscriber, c.source
                            FROM chat c JOIN videos v ON v.id = c.video_id {where} ORDER BY c.video_id, c.t""", params):
                        d = dict(r)
                        d["ts"] = (d["stream_start_unix"] + d["t"]) if d["stream_start_unix"] else None
                        yield json.dumps(d, ensure_ascii=False) + "\n"
                finally:
                    con.close()
            return StreamingResponse(gen(), media_type="application/x-ndjson",
                                     headers={"Content-Disposition": "attachment; filename=chat-export.jsonl"})

    @app.get("/{path:path}")
    def static(path: str):
        path = path or "index.html"
        for base in static_dirs:
            target = (base / path).resolve()
            if target.is_file() and base.resolve() in target.parents:
                return FileResponse(target)
        raise HTTPException(404, "not found")

    return app
