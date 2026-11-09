# vod-archive

Searchable archive of one streamer's VODs: transcripts with named speakers, chat on the same timeline, keyword and
semantic search, and a watch page that keeps video, transcript and chat in sync. See `NOTES.md` for build status and
decisions.

## Setup (GPU machine)
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export HF_TOKEN=...            # after accepting the pyannote model terms on Hugging Face
python check_env.py            # must be all green
```
Clone the private settings repo next to this one (`../thehasanprojectprivate`) and fill in its `config.yaml`
(domain, contact address, sources, TwitchDownloaderCLI path). Put `HF_TOKEN` in `.env` here, never in git.

## Pipeline (run from the repo root)
```bash
python pipeline/inventory.py
python pipeline/download.py --limit 5
python pipeline/muted.py
python pipeline/chat_replay.py
python pipeline/enroll_voices.py                 # after putting clips in voices/<Name>/
python pipeline/process.py --model large-v3      # pilot: repeat with large-v3-turbo
python pipeline/qa.py                            # report + qa/qa_labels.tsv; then qa.py --score
python pipeline/align_chat.py
python pipeline/build_db.py
python pipeline/publish.py
python pipeline/embed.py
python pipeline/ask.py "what did he say about rent control"
```
The live chat logger runs separately: `python pipeline/chat_logger.py` (systemd unit in `deploy/`).

## Apps
```bash
uvicorn web.app:app --host 127.0.0.1 --port 8000      # public, reads public.db only
uvicorn admin.app:app --host 127.0.0.1 --port 8001    # private, reads archive.db; never proxy this
```

## Tests
```bash
python -m pytest
```
