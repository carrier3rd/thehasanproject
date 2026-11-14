# Builds the Windows GPU environment for the transcription pipeline (same package versions as the old WSL venv).
$ErrorActionPreference = 'Stop'
# the project folder: the nearest parent holding pipeline\process.py
$root = $PSScriptRoot; while (-not (Test-Path (Join-Path $root 'pipeline\process.py'))) { $root = Split-Path $root }
Set-Location $root
if (-not (Test-Path .venv)) { py -3.12 -m venv .venv }
$pip = '.venv\Scripts\python.exe', '-m', 'pip'
& $pip[0] $pip[1..2] install -q --upgrade pip
& $pip[0] $pip[1..2] install torch==2.8.0 torchaudio==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
& $pip[0] $pip[1..2] install -r requirements-gpu.txt psutil "yt-dlp[default]" -c (Join-Path $PSScriptRoot 'constraints.txt') --extra-index-url https://download.pytorch.org/whl/cu128
& .venv\Scripts\python.exe -c "import torch, ctranslate2, whisperx, pyannote.audio; print('torch', torch.__version__, 'cuda', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-'); print('ct2 cuda devices', ctranslate2.get_cuda_device_count())"
