"""List files a GPU run needs: audio without a transcript, plus its muted-span file (paths relative to the repo)."""
from __future__ import annotations

import argparse

from common import P, load_config
from process import in_shard, out_dir


def pending(cfg: dict, model: str | None = None, only: str | None = None, shard: str | None = None) -> list[str]:
    dest = out_dir(cfg, model)
    return [p.stem for p in sorted(P(cfg, "audio").glob(f"*.{cfg['audio_format']}"))
            if not (dest / f"{p.stem}.json").exists() and in_shard(p.stem, shard) and (not only or p.stem == only)]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch", type=int, default=0, help="at most N VODs (0 = all)")
    # the same selection flags process.py takes, so the remote run and the upload agree
    ap.add_argument("--model")
    ap.add_argument("--only")
    ap.add_argument("--shard")
    ap.add_argument("--limit", type=int)
    a, _ = ap.parse_known_args()
    cfg = load_config()
    ids = pending(cfg, a.model, a.only, a.shard)
    n = min(x for x in (a.batch or len(ids), a.limit or len(ids), len(ids)))
    root = P(cfg, "audio").parent.parent
    for vid in ids[:n]:
        for f in (P(cfg, "audio") / f"{vid}.{cfg['audio_format']}", P(cfg, "muted") / f"{vid}.json"):
            if f.exists():
                print(f.relative_to(root) if f.is_relative_to(root) else f)


if __name__ == "__main__":
    main()
