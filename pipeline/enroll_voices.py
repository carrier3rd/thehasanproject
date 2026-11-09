"""voices/<Name>/*.wav -> voiceprints/<Name>.npy, with a leave-one-out self-test."""
from __future__ import annotations

import argparse
import os
import subprocess
import time

import numpy as np

from common import P, connect_archive, get_logger, load_config, make_dirs, parse_ts

log = get_logger("enroll")


def l2(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def load_embedder(cfg: dict):
    """Returns f(path_or_dict) -> unit vector, using pyannote Inference(window='whole')."""
    import torch
    from pyannote.audio import Inference, Model
    try:
        model = Model.from_pretrained(cfg["voice_embedding_model"], token=os.environ.get("HF_TOKEN"))
    except TypeError:  # pyannote.audio < 4 spells it use_auth_token
        model = Model.from_pretrained(cfg["voice_embedding_model"], use_auth_token=os.environ.get("HF_TOKEN"))
    inf = Inference(model, window="whole")
    inf.to(torch.device("cuda" if torch.cuda.is_available() else "cpu"))
    return inf


def cut_clip(cfg: dict, video_id: str, start: str, seconds: float, name: str):
    src = P(cfg, "audio") / f"{video_id}.{cfg['audio_format']}"
    dest_dir = P(cfg, "voices") / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{video_id}_{int(parse_ts(start))}.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(parse_ts(start)),
                    "-t", str(seconds), "-i", str(src), "-ac", "1", "-ar", "16000", str(dest)], check=True)
    print(f"wrote {dest}")


def self_test(embs: dict[str, list[np.ndarray]]) -> tuple[list[str], np.ndarray]:
    """Leave-one-out: score each clip against every person's print built without that clip."""
    names = sorted(embs)
    conf = np.zeros((len(names), len(names)), dtype=int)
    for i, name in enumerate(names):
        for k, clip in enumerate(embs[name]):
            scores = []
            for other in names:
                pool = [e for j, e in enumerate(embs[other]) if not (other == name and j == k)]
                scores.append(float(clip @ l2(np.mean(pool, axis=0))) if pool else -1.0)
            conf[i, int(np.argmax(scores))] += 1
    return names, conf


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cut", nargs=4, metavar=("VIDEO_ID", "HH:MM:SS", "SECONDS", "NAME"))
    a = ap.parse_args()
    cfg = load_config()
    make_dirs(cfg)
    if a.cut:
        cut_clip(cfg, a.cut[0], a.cut[1], float(a.cut[2]), a.cut[3])
        return 0
    embed = load_embedder(cfg)
    embs = {}
    for d in sorted(p for p in P(cfg, "voices").iterdir() if p.is_dir()):
        clips = sorted(d.glob("*.wav"))
        if clips:
            embs[d.name] = [l2(embed(str(c))) for c in clips]
    if not embs:
        log.error("no clips under voices/<Name>/*.wav")
        return 1
    con = connect_archive(cfg)
    for name, vecs in embs.items():
        np.save(P(cfg, "voiceprints") / f"{name}.npy", l2(np.mean(vecs, axis=0)))
        con.execute("INSERT OR REPLACE INTO speakers(name, n_clips, updated_at) VALUES(?,?,?)",
                    (name, len(vecs), time.strftime("%Y-%m-%dT%H:%M:%S")))
        print(f"{name}: {len(vecs)} clip(s)")
    con.commit()
    names, conf = self_test(embs)
    print("\nleave-one-out confusion (rows = true, cols = predicted):")
    print(" " * 14 + " ".join(f"{n[:10]:>10}" for n in names))
    for n, row in zip(names, conf):
        print(f"{n[:14]:<14}" + " ".join(f"{v:>10}" for v in row))
    ok = int(np.trace(conf)) == int(conf.sum())
    print("self-test:", "PASS" if ok else "FAIL (some clips match another person better; replace them)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
