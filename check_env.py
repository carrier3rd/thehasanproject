"""Verify the machine is ready: ffmpeg, CUDA + VRAM, HF_TOKEN, TwitchDownloaderCLI, model downloads."""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "pipeline"))
from common import load_config, make_dirs  # noqa: E402

GREEN, RED, END = "\033[32m", "\033[31m", "\033[0m"
results = []


def check(name, fn):
    try:
        detail = fn()
        results.append(True)
        print(f"{GREEN}[ ok ]{END} {name}" + (f": {detail}" if detail else ""))
    except Exception as e:
        results.append(False)
        print(f"{RED}[FAIL]{END} {name}: {e}")


def need(cmd):
    path = shutil.which(cmd) or (cmd if Path(cmd).is_file() else None)
    if not path:
        raise RuntimeError(f"{cmd} not found on PATH")
    return path


def gpu():
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA not available to torch")
    props = torch.cuda.get_device_properties(0)
    gb = props.total_memory / 2 ** 30
    if gb < 9.5:
        raise RuntimeError(f"{props.name} has {gb:.1f} GB VRAM; need >= 10 GB")
    return f"{props.name}, {gb:.1f} GB VRAM, CUDA {torch.version.cuda}"


def fts5():
    sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(a)")
    return f"SQLite {sqlite3.sqlite_version}"


def hf_token():
    if not os.environ.get("HF_TOKEN"):
        raise RuntimeError("HF_TOKEN is not set")


def pyannote_models(cfg):
    from pyannote.audio import Model, Pipeline
    token = os.environ.get("HF_TOKEN")
    Model.from_pretrained(cfg["voice_embedding_model"], token=token)
    import whisperx.diarize as d
    import inspect
    name = cfg.get("diarization_model") or inspect.signature(d.DiarizationPipeline.__init__).parameters["model_name"].default \
        or "pyannote/speaker-diarization-community-1"
    if Pipeline.from_pretrained(name, token=token) is None:
        raise RuntimeError(f"could not load {name}; accept its terms on Hugging Face")
    return f"{cfg['voice_embedding_model']}, {name}"


def embed_model(cfg):
    from sentence_transformers import SentenceTransformer
    dim = SentenceTransformer(cfg["embed_model"]).get_sentence_embedding_dimension()
    return f"{cfg['embed_model']} ({dim} dims)"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true", help="skip model download checks")
    ap.add_argument("--role", choices=["laptop", "gpu", "all"], default="all",
                    help="laptop: logger/downloads/site; gpu: transcription only")
    a = ap.parse_args()
    cfg = load_config()
    make_dirs(cfg)
    check("python >= 3.11", lambda: sys.version.split()[0] if sys.version_info >= (3, 11) else (_ for _ in ()).throw(RuntimeError(sys.version)))
    check("directories", lambda: "created")
    laptop, gpu_role = a.role in ("laptop", "all"), a.role in ("gpu", "all")
    check("ffmpeg", lambda: subprocess.run([need("ffmpeg"), "-version"], capture_output=True, text=True).stdout.split("\n")[0])
    check("SQLite FTS5", fts5)
    if laptop:
        check("yt-dlp", lambda: subprocess.run([need("yt-dlp"), "--version"], capture_output=True, text=True).stdout.strip())
        check("TwitchDownloaderCLI", lambda: need(cfg["twitch_downloader_cli"]))
        check("fastapi + uvicorn", lambda: __import__("fastapi") and __import__("uvicorn") and None)
        if not a.offline:
            check("embedding model", lambda: embed_model(cfg))
    if gpu_role:
        check("GPU", gpu)
        check("HF_TOKEN", hf_token)
        check("whisperx import", lambda: __import__("whisperx") and None)
        if not a.offline:
            check("pyannote models", lambda: pyannote_models(cfg))
    ok = all(results)
    print(f"\n{GREEN + 'all green' if ok else RED + f'{results.count(False)} check(s) failed'}{END}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
