# NOTES

Decisions and adaptations made during the build. Spec = `PROJECT SPEC v4`.

## Status
Built and tested in a cloud container with **no GPU and no access to Twitch, YouTube or Hugging Face**.

| Phase | Code | Verified here |
|---|---|---|
| 1 Setup / check_env | written | runs; GPU, HF, TwitchDownloaderCLI checks fail here as expected |
| 2 Inventory + download | written | planning logic (dedupe, live/recent skip) unit-tested; yt-dlp calls never run |
| 2b Muted spans | written | tested on generated wav + opus (span within ±5 s) |
| 3 Chat replay | written | never run (needs Twitch) |
| 3 Live logger | written | tested against a local fake IRC server; never run against Twitch |
| 4 Voice enrollment | written | never run (needs pyannote models) |
| 5 Processing | written | stitching, naming, clustering, clean-up, `--rename-only` unit-tested; the GPU stage never run |
| 6 Chat alignment | written | cross-correlation tested on synthetic envelopes (trim + mid-stream cut, all spot checks within 2 s) |
| 7 build_db | written | tested |
| 7b publish | written | tested, including the opt-out test failing closed |
| 8 Semantic search | written | index build + hybrid ranking tested with a stand-in embedder; bge model never loaded |
| 9a/9b Web + admin | written | API tests + headless-browser run of every page |
| 10 Deploy | written | `nightly.sh` syntax-checked only |

Run `python -m pytest` from the repo root (27 tests, no GPU or network needed).

## Where the spec contradicts itself (v4 was followed)
- Section 1 ("Item 2 is public only as aggregates") and the Section 4 comments (`web/static/watch.html # ... chat activity
  graph`, `publish.py # allowlist + aggregates`) still describe v3. The v4 change list, Section 1a, 6, 7b and 9a say
  full chat is public; that is what is built. `chat_minutes` aggregates are kept as well (activity strip).

## Deviations
- **`videos.chat_sig`** (extra column in archive.db). build_db reloads a video's chat when its inputs change (new live
  messages, a new replay file, a changed start time or anchors) independently of the transcript hash.
- **Who writes what in Phases 6/7.** align_chat.py writes `stream_start_unix` and `chat_anchors` (creating a stub
  `videos` row if needed). build_db.py loads turns, muted spans and chat (live preferred over replay). The spec had both
  phases writing chat rows, and the videos row did not exist yet when Phase 6 ran.
- **`turn_max_seconds: 30`** (new setting). Without a cap a solo monologue merges into one multi-minute turn, which
  breaks "exact timestamp" links, the watch-page highlight and the 60 s embedding windows.
- **Full-text search matches the text column only.** `turns_fts` and `chat_fts` keep the spec's `(text, speaker)` /
  `(text, author)` columns, but queries are scoped to `text`; otherwise searching "guest" returns every Guest turn.
  Speaker and author are filters.
- **`author` is the login name** from both the logger and the replay (the spec says "login" for the logger), so author
  search matches across sources. Display-name capitalisation is not stored.
- **Admin frontend reuses the public pages.** v4 gave the public watch page the full chat pane, so `admin/app.py`
  serves `web/static/` and the pages switch on `/api/config` (`mode: admin` shows user_id, raw speaker labels and
  scores, deleted text). `admin/static/chat.html` redirects to the Chat tab; there is no separate `admin/static/watch.html`.
- **Shared server code** lives in `pipeline/webcore.py` and `pipeline/search.py`; `web/app.py` and `admin/app.py` are
  thin entry points. The public app opens only `public.db`.
- **public.db connection**: one read-only connection per request instead of reopen-on-inode-change. Same effect, no state.
- **Opt-out matching** is deliberately wide: a listed name excludes every user_id that ever used it, and every other
  name those user_ids used. A different person who later takes an opted-out name is excluded too.
- **download.py refuses more than 5 pending VODs without `--yes`**, and `nightly.sh` does not pass `--yes`. The first
  backfill is a manual, deliberate run.
- **Logger heartbeat** (`logs/chat_logger.heartbeat`, once a minute) so `qa.py --health` can tell "logger down" from
  "stream offline". In-stream gaps are reported from `live_msgs`.
- **Diarizer embeddings** are used only when their dimension matches the voiceprints; otherwise the crop-and-embed
  fallback runs. Models stay loaded across chunks; "free GPU memory after each chunk" is `gc` + `empty_cache`.

- **Timeouts and bans hide that user's earlier messages** from the current stream (`hide_messages_on_ban`), as
  Twitch does in its own chat. Public shows the placeholder; the text stays in archive.db.
- **Private settings** (domain, contact, sources, opt-out list) live in the private repo `thehasanprojectprivate`,
  cloned next to this one; its `config.yaml` overrides this repo's (`private_dir`, or `VOD_ARCHIVE_PRIVATE_DIR`).

- **Two machines.** The always-on laptop has no NVIDIA GPU (Intel Iris Plus only), so transcription and voice
  enrollment run on a rented GPU via `deploy/gpu/run_remote.sh`; everything else runs on the laptop.
  `requirements.txt` is the laptop set (CPU PyTorch), `requirements-gpu.txt` the GPU set, `check_env.py --role`.
  `process.py` refuses to run without a GPU unless given `--allow-cpu`; `nightly.sh` transcribes only when `GPU_HOST`
  is set, otherwise it reports how many VODs are waiting. `run_remote.sh` was dry-run against a local stand-in for
  SSH (sync, batch limit, token-only `.env`, pull without overwriting local transcripts); never against a real GPU host.

## Library versions checked
Signatures were read from the wheels, not run: whisperx 3.8.6 (`whisperx.diarize.DiarizationPipeline(token=...)`,
`__call__(..., return_embeddings=True)` returning `(df, {speaker: vector})`), pyannote.audio 4.0.7
(`Model.from_pretrained(..., token=...)`, `Inference(model, window="whole")`, `.crop(file, Segment)`). Older spellings
(`use_auth_token`, no `return_embeddings`) are handled by fallbacks.

## Open questions for the user
1. **publish.py rebuilds public.db from scratch.** That was cheap when it held transcripts only. With every chat
   message it will take minutes and grow with the archive; an incremental publish may be needed after the backfill.
2. **Opt-outs are applied on the next publish**, i.e. up to a day later with the nightly schedule.
