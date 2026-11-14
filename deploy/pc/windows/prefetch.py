"""Download the models the pipeline uses into the Windows Hugging Face cache (no GPU needed)."""
import os, re
from pathlib import Path
from huggingface_hub import snapshot_download
root = next(p for p in Path(__file__).resolve().parents if (p / "pipeline" / "process.py").exists())
for line in (root / ".env").read_text().splitlines():
    m = re.match(r"(?:export\s+)?HF_TOKEN=(.*)", line.strip())
    if m: os.environ["HF_TOKEN"] = m.group(1).strip().strip("'\"")
for repo in ("Systran/faster-whisper-large-v3", "pyannote/speaker-diarization-community-1"):
    p = snapshot_download(repo, token=os.environ.get("HF_TOKEN"))
    print("ok", repo, sum(f.stat().st_size for f in Path(p).rglob("*") if f.is_file()) // 2**20, "MB")
