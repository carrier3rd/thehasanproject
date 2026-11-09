"""Semantic index over public.db turns -> index/vecs.npy + index/meta.json (incremental)."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3

import numpy as np

from common import P, get_logger, load_config, make_dirs

log = get_logger("embed")


def make_windows(turns: list[tuple], win: float, overlap: float) -> list[tuple[float, float, str]]:
    """turns: (start, speaker, text) sorted by start -> [(window_start, window_end, text)]"""
    if not turns:
        return []
    step = win - overlap
    out, k, i = [], int(turns[0][0] // step), 0
    last = turns[-1][0]
    while k * step <= last:
        ws = k * step
        while i < len(turns) and turns[i][0] < ws:
            i += 1
        j, lines = i, []
        while j < len(turns) and turns[j][0] < ws + win:
            lines.append(f"{turns[j][1]}: {turns[j][2]}")
            j += 1
        if lines:
            out.append((ws, ws + win, "\n".join(lines)))
        k += 1
    return out


def build(cfg: dict, embed_docs, include_media: bool = False, rebuild: bool = False) -> dict:
    """embed_docs: f(list[str]) -> float32 unit vectors. Returns stats."""
    idx = P(cfg, "index")
    idx.mkdir(parents=True, exist_ok=True)
    mp, vp = idx / "meta.json", idx / "vecs.npy"
    old_meta, old_vecs = {"videos": {}, "windows": []}, None
    if mp.exists() and vp.exists() and not rebuild:
        old_meta, old_vecs = json.loads(mp.read_text()), np.load(vp)
        if old_meta.get("model") != cfg["embed_model"] or old_meta.get("include_media") != include_media:
            old_meta, old_vecs = {"videos": {}, "windows": []}, None
    old_rows = {}
    for i, w in enumerate(old_meta["windows"]):
        old_rows.setdefault(w[0], []).append(i)

    con = sqlite3.connect(f"file:{P(cfg, 'public_db')}?mode=ro", uri=True)
    windows, parts, videos, stats = [], [], {}, {"kept": 0, "embedded": 0}
    for (vid,) in con.execute("SELECT id FROM videos ORDER BY id").fetchall():
        sql = "SELECT start, speaker, text FROM turns WHERE video_id=?" + ("" if include_media else " AND is_media=0") + " ORDER BY start"
        turns = con.execute(sql, (vid,)).fetchall()
        digest = hashlib.sha1(json.dumps(turns).encode()).hexdigest()
        if old_vecs is not None and old_meta["videos"].get(vid) == digest:
            rows = old_rows.get(vid, [])
            windows += [old_meta["windows"][i] for i in rows]
            if rows:
                parts.append(old_vecs[rows])
            stats["kept"] += 1
        else:
            wins = make_windows(turns, cfg["embed_window_seconds"], cfg["embed_window_overlap_seconds"])
            if wins:
                parts.append(np.asarray(embed_docs([w[2] for w in wins]), dtype=np.float32))
                windows += [[vid, w[0], w[1]] for w in wins]
            stats["embedded"] += 1
        videos[vid] = digest
    vecs = np.concatenate(parts) if parts else np.zeros((0, 1), dtype=np.float32)
    tmp_v, tmp_m = idx / "vecs.tmp.npy", idx / "meta.tmp.json"
    np.save(tmp_v, vecs)
    tmp_m.write_text(json.dumps({"model": cfg["embed_model"], "include_media": include_media,
                                 "videos": videos, "windows": windows}))
    tmp_v.replace(vp)
    tmp_m.replace(mp)
    stats["windows"] = len(windows)
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--include-media", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    make_dirs(cfg)
    from search import Embedder
    stats = build(cfg, Embedder(cfg).docs, a.include_media, a.rebuild)
    print(f"index: {stats['windows']} windows ({stats['embedded']} VOD(s) embedded, {stats['kept']} unchanged)")


if __name__ == "__main__":
    main()
