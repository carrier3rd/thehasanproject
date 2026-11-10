"""CLI semantic / hybrid search over the transcript index, optionally answered by an LLM."""
from __future__ import annotations

import argparse
import sqlite3

from common import P, fmt_ts, load_config, site_url, watch_link
from search import Embedder, Index, hybrid, window_results

INSTRUCTION = ("Answer only from these excerpts, cite the link for every claim, "
               "say if they don't answer it.")


def answer_with_llm(cfg: dict, question: str, excerpts: str) -> str:
    import anthropic
    client = anthropic.Anthropic()
    try:
        resp = client.beta.messages.create(
            model=cfg["llm_model"], max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            system=INSTRUCTION,
            messages=[{"role": "user", "content": f"Question: {question}\n\nExcerpts:\n\n{excerpts}"}])
    except anthropic.AuthenticationError:
        return "LLM error: no valid Anthropic credentials (set ANTHROPIC_API_KEY)."
    except anthropic.RateLimitError:
        return "LLM error: rate limited; try again shortly."
    except anthropic.APIStatusError as e:
        return f"LLM error: {e.status_code} {e.message}"
    except anthropic.APIConnectionError:
        return "LLM error: could not reach the API."
    if resp.stop_reason == "refusal":
        return "The model declined to answer this request."
    return "".join(b.text for b in resp.content if b.type == "text")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("question")
    ap.add_argument("--mode", choices=["semantic", "hybrid"], default="hybrid")
    ap.add_argument("--k", type=int, default=15)
    ap.add_argument("--llm", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    index = Index(P(cfg, "index"))
    if not len(index):
        print("index is empty; run embed.py first")
        return 1
    con = sqlite3.connect(f"file:{P(cfg, 'public_db')}?mode=ro", uri=True)
    qvec = Embedder(cfg).query(a.question)
    hits = index.semantic(qvec, a.k) if a.mode == "semantic" else hybrid(con, index, a.question, qvec, a.k)
    base = site_url(cfg)
    blocks = []
    for r in window_results(con, index, hits):
        link = base + watch_link(r["video_id"], r["start"])
        body = "\n".join(f"  {t['speaker']}: {t['text']}" for t in r["turns"])
        blocks.append(f"[{r['upload_date']} {r['title']} @ {fmt_ts(r['start'])}] {link}\n{body}")
    text = "\n\n".join(blocks)
    print(text)
    if a.llm:
        print("\n=== answer ===\n" + answer_with_llm(cfg, a.question, text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
