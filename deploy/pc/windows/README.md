# Transcription on the Windows PC

Since 2026-11-14 the PC's RTX 4050 transcribes natively on Windows (it ran in WSL before; WSL's VM kept memory
tight on the 15 GB machine). The laptop still does everything else.

`runner.py` replaces the old bash scripts (pc_backfill2.sh, older_transcribe.sh, gpu_thermostat.sh, live.sh,
night_runner.sh, run_now.sh):

- **backfill:** VODs on the laptop with no transcript, newest first; copies audio down with scp, runs
  `pipeline/process.py`, copies the transcript, embeddings and `process_times.tsv` line back (never overwrites).
- **older queue:** the newest 181 no-caption YouTube re-uploads in `older/sync.jsonl` (the owner's approved number):
  audio only, transcripts stay in `older/transcripts/`. After 3 failed downloads in a row (YouTube's bot check) it
  waits 3 h, slows down, and retries the failures once at the end.
- **thermostat:** suspends transcription at 85 °C, resumes at 78 °C (`HOT`/`COOL` env vars).
- **status:** one line every ~2 min in `logs/live-status.log`; `runner.py status` prints one now.

```
.venv\Scripts\python.exe deploy\pc\windows\runner.py run --until 06:00   # both queues, stop at 6 AM
.venv\Scripts\python.exe deploy\pc\windows\runner.py status
.venv\Scripts\python.exe deploy\pc\windows\runner.py stop
```

## Setup on a new PC

1. Python 3.12 (`winget install Python.Python.3.12 --scope user`), FFmpeg (`winget install Gyan.FFmpeg.Shared`), Deno
   for yt-dlp (`winget install DenoLand.Deno`). The runner finds winget's per-user FFmpeg and Deno itself.
2. `powershell -ExecutionPolicy Bypass -File deploy\pc\windows\setup.ps1`: builds `.venv` with CUDA 12.8 torch and
   the same package versions as before (`constraints.txt`), and checks the GPU.
3. Put `.env` (`HF_TOKEN=...`) and `private/config.yaml` (compute_type int8_float16, batch_size 4 for 6 GB VRAM)
   in the project folder; add an ssh alias `laptop` with a key the laptop accepts.
4. `.venv\Scripts\python.exe deploy\pc\windows\prefetch.py` downloads the models (~3 GB).
5. Scheduled tasks (current user, "run only when logged on", no admin), action
   `conhost.exe --headless <project>\.venv\Scripts\python.exe <runner.py> run --until 06:00`, start in the project
   folder, no time limit, allowed on battery: "VOD night transcription" daily at 00:00, and "VOD transcription run
   now" with no trigger for daytime runs. The runner holds a lock, so a second start exits.
