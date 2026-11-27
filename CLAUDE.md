# Instructions for Claude sessions

Searchable archive of a Twitch streamer's VODs: transcripts with speaker labels, chat on the same timeline, keyword and
semantic search, and a synced watch page.

## Start here
1. **Attach the private repo `carrier3rd/thehasanprojectprivate`** (with `add_repo` in cloud sessions; clone it next to
   this one as `../thehasanprojectprivate`).
2. Read its `STATUS.md`: where things stand, the machines, what's done, the next steps, the unknowns and the open
   decisions. Its `SPEC-v4.md` is the original design.
3. Read `NOTES.md` here for every place the build deviates from the spec, and why.
4. When you finish a step, learn a fact or the owner makes a decision, update `STATUS.md` in the private repo.

## How the project runs
- **Windows PC (since 2026-11-26; the laptop's RAM corrupts data):** everything. The archive's main copy is the
  `hasan-hub` clone on the PC (`~\projects\hasan-hub`).
  - Always-on programs (`win/daemons.ps1`, each under `win/keep_running.py`): the public site (`web.app`) behind the
    Cloudflare tunnel for thehasanproject.org, the live chat logger and the admin app.
  - `win/hub.py run`, hourly: downloads, then rebuild and publish when something new arrived, then the daily backup.
    `win/hub.py features`, nightly.
  - Transcription on the PC's GPU (`deploy/pc/windows/runner.py`, installed in `~/vod-archive`), lowest priority.
  - There is no auto-update: after pushing to `main`, `git pull` in the hub clone (and copy runner.py to
    `~/vod-archive/win/` if it changed). Run `python -m pytest` first.
- **Laptop (Ubuntu):** retired for the archive. Its copy of the data is as of 2026-11-26. `deploy/laptop/` and
  `deploy/nightly.sh` describe how it ran until then.
- **Rented GPU (by the hour):** transcription and voice enrollment, driven by `deploy/gpu/run_remote.sh` (unused
  since the PC took over transcription).
- **Cloud Claude sessions** can't reach the laptop, Twitch, YouTube or Hugging Face. The owner runs commands on the
  laptop and pastes the output. Don't pretend a step ran when it didn't.

## Code map
- `pipeline/` holds the scripts:
  - `common.py`: config (with the private overlay), DB schema, chat clock anchors;
  - one script per spec phase;
  - `webcore.py`: the FastAPI app shared by the public and admin apps;
  - `search.py`: FTS, the semantic index, hybrid ranking.
- `web/static/`: the frontend, plain HTML/JS with no build step, shared by both apps (`/api/config` says which mode).
- `tests/`: synthetic data, no GPU or network needed: `python -m pytest`.

## Rules that must hold
- `public.db` never contains user_ids, msg_ids, opted-out users or deleted text (unless `publish_deleted_text`).
  `publish.py`'s check fails the publish otherwise.
- The public app never reads `archive/`, `data/`, `voices/`, `voiceprints/` or `.env`.
- The admin app stays on `127.0.0.1`.
- Audio only: never download or rehost video.
- Ask the owner before downloading more than 5 VODs, deleting data, or making the site public (removing the password).
- No secrets in git: `HF_TOKEN` goes in `.env`, the site password in `site.env`, both on the laptop only.
