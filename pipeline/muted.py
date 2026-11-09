"""Detect DMCA-muted spans -> data/muted/<id>.json as [[start, end], ...]."""
from __future__ import annotations

import argparse
import json
import re
import subprocess

from common import P, get_logger, load_config, log_failure, make_dirs

log = get_logger("muted")


def from_metadata(info: dict):
    """Muted segments if the Twitch metadata exposes them (key names vary by extractor version)."""
    for key in ("muted_segments", "mutedSegments"):
        segs = info.get(key)
        if isinstance(segs, dict):
            segs = segs.get("nodes")
        if segs:
            out = []
            for s in segs:
                start = s.get("offset", s.get("start"))
                dur = s.get("duration")
                end = s.get("end", (start + dur) if start is not None and dur is not None else None)
                if start is not None and end is not None:
                    out.append([float(start), float(end)])
            return out
    return None


def parse_silencedetect(stderr: str, duration: float | None = None) -> list[list[float]]:
    spans, start = [], None
    for m in re.finditer(r"silence_(start|end): (-?[\d.]+)", stderr):
        if m.group(1) == "start":
            start = max(float(m.group(2)), 0.0)
        elif start is not None:
            spans.append([start, float(m.group(2))])
            start = None
    if start is not None and duration:
        spans.append([start, float(duration)])
    return spans


def detect(path, cfg: dict, duration: float | None = None) -> list[list[float]]:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
         "-af", f"silencedetect=noise={cfg['mute_noise_db']}dB:d={cfg['mute_min_seconds']}", "-f", "null", "-"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[-300:])
    return parse_silencedetect(out.stderr, duration)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    make_dirs(cfg)
    n = 0
    for audio in sorted(P(cfg, "audio").glob(f"*.{cfg['audio_format']}")):
        vid = audio.stem
        dest = P(cfg, "muted") / f"{vid}.json"
        if (a.only and vid != a.only) or (dest.exists() and not a.force):
            continue
        try:
            info_path = audio.with_suffix(".info.json")
            info = json.loads(info_path.read_text()) if info_path.exists() else {}
            spans = from_metadata(info)
            if spans is None:
                spans = detect(audio, cfg, info.get("duration"))
            dest.write_text(json.dumps(spans))
            n += 1
            log.info("%s: %d muted span(s)", vid, len(spans))
        except Exception as e:
            log.error("failed %s: %s", vid, e)
            log_failure(cfg, vid, "muted", e)
    print(f"muted spans written for {n} VOD(s)")


if __name__ == "__main__":
    main()
