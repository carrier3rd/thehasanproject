"""Chunked transcribe + align + diarize + name speakers -> data/transcripts/<id>.json.

The GPU-free parts (stitching, speaker naming, unknown clustering, clean-up) are plain
functions so `--rename-only` and the tests run without torch.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import re
import time
from pathlib import Path

import numpy as np

from common import P, get_logger, load_config, log_failure, make_dirs

log = get_logger("process")
SR = 16000


# ---------- pure helpers ----------

def l2(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def chunk_bounds(total_s: float, chunk_s: float, overlap_s: float) -> list[tuple[float, float]]:
    """[(start, end)] with each chunk extended `overlap_s` into the next one."""
    if total_s <= chunk_s + overlap_s:
        return [(0.0, total_s)]
    out, start = [], 0.0
    while start < total_s:
        out.append((start, min(start + chunk_s + overlap_s, total_s)))
        start += chunk_s
    if len(out) > 1 and out[-1][1] - out[-1][0] <= overlap_s:  # last chunk lies inside the previous overlap
        out.pop()
    return out


def keep_windows(bounds, overlap_s: float) -> list[tuple[float, float]]:
    """Global-time window each chunk owns: overlaps are split at their midpoint, so every
    word comes from the chunk in which it is further from the chunk edge."""
    out = []
    for i, (start, end) in enumerate(bounds):
        lo = 0.0 if i == 0 else start + overlap_s / 2
        hi = float("inf") if i == len(bounds) - 1 else bounds[i + 1][0] + overlap_s / 2
        out.append((lo, hi))
    return out


def fill_word_times(seg: dict) -> list[dict]:
    """whisperx leaves some words (numerals, symbols) without timestamps; borrow neighbours'."""
    words, prev_end = [], seg["start"]
    for w in seg.get("words") or []:
        w = dict(w)
        if w.get("start") is None or w.get("end") is None:
            w["start"] = w["end"] = prev_end
        prev_end = w["end"]
        words.append(w)
    return words


def split_by_speaker(seg: dict) -> list[dict]:
    """Split an aligned segment at word-level speaker changes."""
    words = fill_word_times(seg)
    if not words:
        return [{"start": seg["start"], "end": seg["end"], "speaker": seg.get("speaker"),
                 "text": (seg.get("text") or "").strip(), "words": []}]
    out, cur = [], seg.get("speaker")
    for w in words:
        spk = w.get("speaker") or cur
        cur = spk
        if out and out[-1]["speaker"] == spk:
            out[-1]["words"].append(w)
        else:
            out.append({"speaker": spk, "words": [w]})
    for s in out:
        s["start"], s["end"] = s["words"][0]["start"], s["words"][-1]["end"]
        s["text"] = " ".join(w["word"].strip() for w in s["words"]).strip()
    return out


def stitch(chunks: list[list[dict]], bounds, overlap_s: float) -> list[dict]:
    """chunks[i] = segments of chunk i in chunk-local time with local `speaker` labels.
    Returns global-time segments with speaker_raw = 'c<i>:<label>'."""
    out = []
    for i, (segs, (cstart, _), (lo, hi)) in enumerate(zip(chunks, bounds, keep_windows(bounds, overlap_s))):
        for seg in segs:
            for part in split_by_speaker(seg):
                words = []
                for w in part["words"]:
                    w = dict(w, start=round(w["start"] + cstart, 3), end=round(w["end"] + cstart, 3))
                    w.pop("speaker", None)
                    if lo <= (w["start"] + w["end"]) / 2 < hi:
                        words.append(w)
                if part["words"]:
                    if not words:
                        continue
                    start, end = words[0]["start"], words[-1]["end"]
                    text = " ".join(w["word"].strip() for w in words).strip()
                else:
                    start, end = part["start"] + cstart, part["end"] + cstart
                    if not lo <= (start + end) / 2 < hi:
                        continue
                    text = part["text"]
                if not text:
                    continue
                out.append({"start": round(start, 3), "end": round(end, 3),
                            "speaker_raw": f"c{i:03d}:{part['speaker']}" if part["speaker"] else None,
                            "text": text, "words": words})
    out.sort(key=lambda s: (s["start"], s["end"]))
    return out


def cluster_unknowns(vecs: np.ndarray, threshold: float) -> list[int]:
    """Average-linkage agglomerative clustering on cosine similarity. Returns a cluster index per row."""
    n = len(vecs)
    clusters = [[i] for i in range(n)]
    sim = vecs @ vecs.T if n else np.zeros((0, 0))
    while len(clusters) > 1:
        best, pair = threshold, None
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                s = float(sim[np.ix_(clusters[a], clusters[b])].mean())
                if s >= best:
                    best, pair = s, (a, b)
        if pair is None:
            break
        a, b = pair
        clusters[a] += clusters.pop(b)
    labels = [0] * n
    for ci, members in enumerate(sorted(clusters, key=min)):
        for m in members:
            labels[m] = ci
    return labels


def name_speakers(embs: dict[str, np.ndarray], prints: dict[str, np.ndarray],
                  match_thr: float, unknown_thr: float):
    """embs: speaker_raw -> unit vector. Returns (speaker_map, speaker_scores, unknown_clusters)."""
    speaker_map, scores, unknown = {}, {}, []
    names = sorted(prints)
    for key in sorted(embs):
        best_name, best = None, -1.0
        for n in names:
            if prints[n].shape == embs[key].shape:
                s = float(prints[n] @ embs[key])
                if s > best:
                    best_name, best = n, s
        scores[key] = round(best, 4) if best_name else None
        if best_name and best >= match_thr:
            speaker_map[key] = best_name
        else:
            unknown.append(key)
    clusters = {}
    if unknown:
        labels = cluster_unknowns(np.stack([embs[k] for k in unknown]), unknown_thr)
        for key, lab in zip(unknown, labels):
            name = f"Unknown_{lab + 1}"
            speaker_map[key] = name
            clusters.setdefault(name, []).append(key)
    return speaker_map, scores, clusters


def apply_names(segments: list[dict], speaker_map: dict, scores: dict) -> None:
    for s in segments:
        raw = s.get("speaker_raw")
        s["speaker"] = speaker_map.get(raw, "Unknown_0")  # Unknown_0: no usable voice sample
        s["score"] = scores.get(raw)


def drop_muted(segments: list[dict], muted: list) -> list[dict]:
    if not muted:
        return segments

    def inside(t):
        return any(a <= t <= b for a, b in muted)

    out = []
    for s in segments:
        if s["words"]:
            words = [w for w in s["words"] if not inside((w["start"] + w["end"]) / 2)]
            if not words:
                continue
            if len(words) != len(s["words"]):
                s = dict(s, words=words, start=words[0]["start"], end=words[-1]["end"],
                         text=" ".join(w["word"].strip() for w in words))
        elif inside((s["start"] + s["end"]) / 2):
            continue
        out.append(s)
    return out


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def drop_repeats(segments: list[dict], n: int) -> list[dict]:
    """Drop runs of >= n consecutive segments with the same text (Whisper looping)."""
    out, i = [], 0
    while i < len(segments):
        j = i
        while j + 1 < len(segments) and _norm(segments[j + 1]["text"]) == _norm(segments[i]["text"]):
            j += 1
        if j - i + 1 < n or not _norm(segments[i]["text"]):
            out.extend(segments[i:j + 1])
        i = j + 1
    return out


def load_voiceprints(cfg: dict) -> dict[str, np.ndarray]:
    return {p.stem: l2(np.load(p)) for p in sorted(P(cfg, "voiceprints").glob("*.npy"))}


def out_dir(cfg: dict, model: str | None) -> Path:
    d = P(cfg, "transcripts")
    if model:
        d = d / model
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_embs(path: Path, embs: dict[str, np.ndarray]) -> None:
    keys = sorted(embs)
    np.savez(path, keys=np.array(keys), vecs=np.stack([embs[k] for k in keys]) if keys else np.zeros((0, 0)))


def load_embs(path: Path) -> dict[str, np.ndarray]:
    z = np.load(path, allow_pickle=False)
    return {str(k): v for k, v in zip(z["keys"], z["vecs"])}


def finish(video_id: str, model: str, bounds, segments: list[dict], embs: dict, cfg: dict) -> dict:
    """Steps 5-8 from stitched segments + chunk-speaker embeddings."""
    speaker_map, scores, clusters = name_speakers(
        embs, load_voiceprints(cfg), cfg["speaker_match_threshold"], cfg["unknown_cluster_threshold"])
    apply_names(segments, speaker_map, scores)
    return {"video_id": video_id, "model": model, "chunks": [list(b) for b in bounds],
            "speaker_map": speaker_map, "speaker_scores": scores, "unknown_clusters": clusters,
            "thresholds": {"match": cfg["speaker_match_threshold"], "unknown": cfg["unknown_cluster_threshold"]},
            "segments": segments}


def write_json(path: Path, doc: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False))
    tmp.replace(path)


def rename_only(cfg: dict, model: str | None, only: str | None) -> int:
    n = 0
    for jp in sorted(out_dir(cfg, model).glob("*.json")):
        vid = jp.stem
        if only and vid != only:
            continue
        ep = jp.with_name(f"{vid}.emb.npz")
        try:
            doc = json.loads(jp.read_text())
            new = finish(vid, doc.get("model"), doc.get("chunks", []), doc["segments"], load_embs(ep), cfg)
            if json.dumps(new, ensure_ascii=False) != jp.read_text():
                write_json(jp, new)
                n += 1
        except Exception as e:
            log.error("rename failed %s: %s", vid, e)
            log_failure(cfg, vid, "rename", e)
    print(f"renamed speakers in {n} transcript(s)")
    return 0


# ---------- GPU stage ----------

class Models:
    def __init__(self, cfg: dict, model_name: str):
        import torch
        import whisperx
        self.torch, self.wx, self.cfg = torch, whisperx, cfg
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        token = os.environ.get("HF_TOKEN")
        compute = cfg["compute_type"] if self.device == "cuda" else "int8"
        self.asr = whisperx.load_model(model_name, self.device, compute_type=compute, language="en")
        self.align_model, self.align_meta = whisperx.load_align_model(language_code="en", device=self.device)
        try:
            from whisperx.diarize import DiarizationPipeline
        except ImportError:
            DiarizationPipeline = whisperx.DiarizationPipeline
        kw = {"device": self.device}
        if cfg.get("diarization_model"):
            kw["model_name"] = cfg["diarization_model"]
        try:
            self.diar = DiarizationPipeline(token=token, **kw)
        except TypeError:  # older whisperx
            self.diar = DiarizationPipeline(use_auth_token=token, **kw)
        self._embedder = None

    def embedder(self):
        if self._embedder is None:
            from enroll_voices import load_embedder
            self._embedder = load_embedder(self.cfg)
        return self._embedder

    def free(self):
        gc.collect()
        if self.device == "cuda":
            self.torch.cuda.empty_cache()

    def run_chunk(self, audio: np.ndarray, want_dim: int | None):
        """-> (aligned segments with speakers, {local speaker label: unit vector})"""
        wx, cfg = self.wx, self.cfg
        result = self.asr.transcribe(audio, batch_size=cfg["batch_size"], language="en")
        result = wx.align(result["segments"], self.align_model, self.align_meta, audio, self.device,
                          return_char_alignments=False)
        embs = None
        try:
            df, raw = self.diar(audio, return_embeddings=True)
            if raw and cfg.get("use_diarizer_embeddings", True):
                embs = {k: l2(v) for k, v in raw.items() if np.isfinite(v).all() and np.any(v)}
                if want_dim and any(v.shape[0] != want_dim for v in embs.values()):
                    embs = None  # different embedding space from the voiceprints
        except TypeError:  # older whisperx: no return_embeddings
            df = self.diar(audio)
        result = wx.assign_word_speakers(df, result)
        if embs is None:
            embs = self.crop_embeddings(audio, df)
        return result["segments"], embs

    def crop_embeddings(self, audio: np.ndarray, df) -> dict[str, np.ndarray]:
        from pyannote.core import Segment
        wave = {"waveform": self.torch.from_numpy(audio[None, :]), "sample_rate": SR}
        inf, out = self.embedder(), {}
        for spk, grp in df.groupby("speaker"):
            segs = sorted(((r.end - r.start, r.start, r.end) for r in grp.itertuples()), reverse=True)
            vecs = [l2(inf.crop(wave, Segment(s, e))) for d, s, e in segs[:8] if d >= 2.0]
            if vecs:
                out[spk] = l2(np.mean(vecs, axis=0))
        return out


def process_one(audio_path: Path, dest: Path, models: Models, model_name: str, cfg: dict) -> tuple[float, float]:
    vid = audio_path.stem
    t0 = time.time()
    audio = models.wx.load_audio(str(audio_path))
    total = len(audio) / SR
    bounds = chunk_bounds(total, cfg["chunk_minutes"] * 60, cfg["chunk_overlap_seconds"])
    prints = load_voiceprints(cfg)
    want_dim = next(iter(prints.values())).shape[0] if prints else None
    chunks, embs = [], {}
    for i, (s, e) in enumerate(bounds):
        segs, local = models.run_chunk(audio[int(s * SR):int(e * SR)], want_dim)
        chunks.append(segs)
        embs.update({f"c{i:03d}:{k}": v for k, v in local.items()})
        models.free()
        log.info("%s chunk %d/%d done", vid, i + 1, len(bounds))
    del audio
    segments = stitch(chunks, bounds, cfg["chunk_overlap_seconds"])
    mp = P(cfg, "muted") / f"{vid}.json"
    segments = drop_muted(segments, json.loads(mp.read_text()) if mp.exists() else [])
    segments = drop_repeats(segments, cfg["hallucination_repeat_n"])
    save_embs(dest.with_name(f"{vid}.emb.npz"), embs)
    write_json(dest, finish(vid, model_name, bounds, segments, embs, cfg))
    return total, time.time() - t0


def in_shard(vid: str, shard: str | None) -> bool:
    if not shard:
        return True
    i, n = (int(x) for x in shard.split("/"))
    return int(hashlib.sha1(vid.encode()).hexdigest(), 16) % n == i


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only")
    ap.add_argument("--model", help="Whisper model; output goes to data/transcripts/<model>/ (pilot)")
    ap.add_argument("--rename-only", action="store_true", help="redo speaker naming from saved embeddings (no GPU)")
    ap.add_argument("--shard", help="i/n: process only ids whose hash mod n == i")
    ap.add_argument("--allow-cpu", action="store_true", help="run without an NVIDIA GPU (very slow)")
    a = ap.parse_args()
    cfg = load_config()
    make_dirs(cfg)
    if a.rename_only:
        return rename_only(cfg, a.model, a.only)
    model_name = a.model or cfg["whisper_model"]
    dest_dir = out_dir(cfg, a.model)
    todo = [p for p in sorted(P(cfg, "audio").glob(f"*.{cfg['audio_format']}"))
            if not (dest_dir / f"{p.stem}.json").exists() and in_shard(p.stem, a.shard)
            and (not a.only or p.stem == a.only)]
    if a.limit:
        todo = todo[:a.limit]
    if not todo:
        print("nothing to process")
        return 0
    if not a.allow_cpu:
        try:
            import torch
            has_gpu = torch.cuda.is_available()
        except ImportError:
            has_gpu = False
        if not has_gpu:
            print(f"{len(todo)} VOD(s) need transcription, but this machine has no NVIDIA GPU. "
                  "Run them on a GPU machine with deploy/gpu/run_remote.sh (or pass --allow-cpu).")
            return 3
    if not load_voiceprints(cfg):
        log.warning("no voiceprints found; every speaker will be Unknown_n")
    models = Models(cfg, model_name)
    times = P(cfg, "logs") / "process_times.tsv"
    if not times.exists():
        times.write_text("id\tmodel\taudio_seconds\twall_seconds\trealtime_factor\n")
    for p in todo:
        try:
            audio_s, wall = process_one(p, dest_dir / f"{p.stem}.json", models, model_name, cfg)
            rtf = audio_s / wall if wall else 0
            with open(times, "a") as f:
                f.write(f"{p.stem}\t{model_name}\t{audio_s:.0f}\t{wall:.0f}\t{rtf:.1f}\n")
            log.info("%s: %.1f h in %.0f min (%.1fx realtime)", p.stem, audio_s / 3600, wall / 60, rtf)
        except Exception as e:
            log.exception("failed %s", p.stem)
            log_failure(cfg, p.stem, "process", e)
            models.free()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
