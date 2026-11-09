"""Shared search code: FTS query sanitising, the semantic index, hybrid ranking (RRF)."""
from __future__ import annotations

import bisect
import json
import sqlite3
from pathlib import Path

import numpy as np

RRF_K = 60


def fts_execute(con: sqlite3.Connection, sql: str, q: str, params: tuple = (), column: str = "text"):
    """Run an FTS5 query against one column; on a syntax error retry with the query as one quoted
    phrase. `sql` must have the MATCH parameter first. Matching is limited to `column` so that a
    search for "guest" does not return every turn labelled Guest."""
    try:
        return con.execute(sql, (f"{column} : ({q})", *params)).fetchall()
    except sqlite3.OperationalError:
        phrase = '"' + q.replace('"', '""') + '"'
        try:
            return con.execute(sql, (f"{column} : {phrase}", *params)).fetchall()
        except sqlite3.OperationalError:
            return []


class Embedder:
    def __init__(self, cfg: dict):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(cfg["embed_model"])
        self.prefix = cfg.get("embed_query_prefix", "")

    def docs(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self.model.encode(texts, normalize_embeddings=True, batch_size=64,
                                            show_progress_bar=len(texts) > 500), dtype=np.float32)

    def query(self, q: str) -> np.ndarray:
        return np.asarray(self.model.encode([self.prefix + q], normalize_embeddings=True)[0], dtype=np.float32)


class Index:
    """index/vecs.npy + index/meta.json; reloads itself when the files change."""

    def __init__(self, index_dir):
        self.dir = Path(index_dir)
        self.stamp = None
        self.vecs = np.zeros((0, 1), dtype=np.float32)
        self.windows: list[list] = []
        self.by_video: dict[str, tuple[list[float], list[int]]] = {}
        self.reload()

    def reload(self) -> bool:
        mp, vp = self.dir / "meta.json", self.dir / "vecs.npy"
        if not (mp.exists() and vp.exists()):
            return False
        stamp = (mp.stat().st_mtime_ns, vp.stat().st_mtime_ns)
        if stamp == self.stamp:
            return False
        meta = json.loads(mp.read_text())
        vecs = np.load(vp)
        if len(vecs) != len(meta["windows"]):
            return False  # caught mid-swap; try again on the next request
        self.vecs, self.windows, self.stamp = vecs, meta["windows"], stamp
        self.by_video = {}
        for i, (vid, start, _end) in enumerate(self.windows):
            starts, idxs = self.by_video.setdefault(vid, ([], []))
            starts.append(start)
            idxs.append(i)
        return True

    def __len__(self):
        return len(self.windows)

    def semantic(self, qvec: np.ndarray, k: int) -> list[tuple[int, float]]:
        if not len(self.windows):
            return []
        scores = self.vecs @ qvec
        k = min(k, len(scores))
        top = np.argpartition(-scores, k - 1)[:k]
        return [(int(i), float(scores[i])) for i in top[np.argsort(-scores[top])]]

    def window_of(self, video_id: str, t: float):
        """Latest window starting at or before t (windows overlap, so it contains t)."""
        entry = self.by_video.get(video_id)
        if not entry:
            return None
        j = bisect.bisect_right(entry[0], t) - 1
        return entry[1][max(j, 0)]


def hybrid(con: sqlite3.Connection, index: Index, q: str, qvec: np.ndarray, k: int, pool: int = 200) -> list[tuple[int, float]]:
    """Reciprocal rank fusion of vector hits and FTS5 bm25 turn hits mapped onto windows."""
    fused: dict[int, float] = {}
    for rank, (i, _s) in enumerate(index.semantic(qvec, pool)):
        fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank + 1)
    rows = fts_execute(con, "SELECT t.video_id, t.start FROM turns_fts JOIN turns t ON t.id = turns_fts.rowid "
                            "WHERE turns_fts MATCH ? ORDER BY bm25(turns_fts) LIMIT ?", q, (pool,))
    seen, rank = set(), 0
    for vid, start in rows:
        i = index.window_of(vid, start)
        if i is None or i in seen:
            continue
        seen.add(i)
        fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank + 1)
        rank += 1
    return sorted(fused.items(), key=lambda x: -x[1])[:k]


def window_results(con: sqlite3.Connection, index: Index, hits: list[tuple[int, float]]) -> list[dict]:
    out = []
    for i, score in hits:
        vid, start, end = index.windows[i]
        v = con.execute("SELECT title, upload_date FROM videos WHERE id=?", (vid,)).fetchone()
        if not v:
            continue
        turns = con.execute("SELECT start, speaker, text FROM turns WHERE video_id=? AND start >= ? AND start < ? "
                            "ORDER BY start", (vid, start, end)).fetchall()
        out.append({"video_id": vid, "title": v[0], "upload_date": v[1], "start": start, "end": end,
                    "score": round(score, 4),
                    "turns": [{"start": t[0], "speaker": t[1], "text": t[2]} for t in turns]})
    return out
