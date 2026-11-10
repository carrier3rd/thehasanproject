# vod-archive

Searchable archive of one streamer's VODs: transcripts with named speakers, chat on the same timeline, keyword and
semantic search, and a watch page that keeps video, transcript and chat in sync. See `NOTES.md` for build status and
decisions.

## Two machines

| Machine | Runs |
|---|---|
| **Laptop** (always on, no GPU needed) | chat logger, inventory, downloads, muted spans, chat replay, alignment, database, publish, search index, web + admin apps |
| **Rented NVIDIA GPU** (by the hour) | transcription (`process.py`) and voice enrollment, driven from the laptop by `deploy/gpu/run_remote.sh` |

### Laptop setup (Ubuntu)
```bash
sudo apt install -y git gh
gh auth login                      # GitHub account; answer yes to "authenticate Git"
cd ~
gh repo clone carrier3rd/thehasanproject
gh repo clone carrier3rd/thehasanprojectprivate
cd thehasanproject
deploy/laptop/bootstrap.sh         # packages, Python env (CPU PyTorch), TwitchDownloaderCLI, no sleep
sudo deploy/laptop/install.sh      # services + site password (see "The website" below)
```
`bootstrap.sh` turns off sleep and suspend (lid closed or idle) so the chat logger never stops; it prints the undo
command. Put `HF_TOKEN=...` in `.env` here before the first GPU run. Never put it in git.

### Rented GPU (RunPod, Vast.ai, Lambda, ...)
Rent a Linux machine with an NVIDIA card of at least 16 GB (24 GB such as an RTX 4090, A10 or L4 is plenty) and a
PyTorch/CUDA image, add your SSH key, and copy its SSH host and port. Then, from the laptop:
```bash
export GPU_HOST=root@<host>  GPU_PORT=<port>
deploy/gpu/run_remote.sh setup             # installs WhisperX + pyannote there, checks the GPU
deploy/gpu/run_remote.sh enroll            # voice prints from voices/<Name>/*.wav
BATCH=20 deploy/gpu/run_remote.sh process  # transcribe up to 20 pending VODs, bring the results back
```
It uploads only audio that still needs a transcript (about 30 MB per stream-hour), and every step resumes if the
connection drops. Run it inside `tmux`. **Stop or delete the rented machine when it finishes**; most providers bill
until you do, some also for stopped storage.

## Pipeline (run from the repo root)
```bash
python pipeline/inventory.py
python pipeline/download.py --limit 5
python pipeline/muted.py
python pipeline/chat_replay.py
python pipeline/enroll_voices.py --cut <id> <HH:MM:SS> <seconds> <Name>   # cut clips on the laptop
deploy/gpu/run_remote.sh enroll                  # build voice prints on the GPU
deploy/gpu/run_remote.sh process --model large-v3        # pilot: repeat with --model large-v3-turbo
python pipeline/qa.py                            # report + qa/qa_labels.tsv; then qa.py --score
python pipeline/align_chat.py
python pipeline/build_db.py
python pipeline/publish.py
python pipeline/embed.py
python pipeline/ask.py "what did he say about rent control"
```
The live chat logger runs separately: `python pipeline/chat_logger.py` (systemd unit in `deploy/`).

## The website (on the laptop)
After the laptop setup above:
```bash
sudo deploy/laptop/install.sh
```
It asks for the site password (user name `viewer`), then installs and starts these services:
- the chat logger
- the public site on `127.0.0.1:8000`
- the admin app on `127.0.0.1:8001`
- the nightly pipeline at 06:00
- a check every 10 minutes that pulls new code and settings from GitHub and restarts what changed

So anything pushed to `main` here or in the private repo reaches the laptop within 10 minutes.

To put the site on the internet, use Tailscale Funnel. It's free, gives a fixed `https://<name>.ts.net` address,
and needs no domain or router changes:
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale funnel --bg 8000
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8001    # admin: only your own Tailscale devices
```
The site is offline whenever the laptop is off or asleep.

## Tests
```bash
python -m pytest
```
