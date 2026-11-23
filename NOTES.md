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
- **`download_fragments: 4`** (new setting): yt-dlp fetches that many pieces of a VOD at once (`--concurrent-fragments`). One at a time was slow on the laptop.
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
- **Google Colab as the GPU** (owner's choice, 2026-11-09). Colab has no SSH, so `run_remote.sh` can't drive it;
  `deploy/colab/` moves files through Google Drive instead. `pack.sh` bundles pending audio + muted spans,
  voiceprints, `voices/` and the private `config.yaml` into `colab/bundle.tar` (never `.env`); `transcribe.ipynb`
  reads `HF_TOKEN` from Colab Secrets, runs `enroll_voices.py` and/or `process.py`, and writes transcripts and logs
  straight to `My Drive/vod-archive/out` so a disconnect loses at most the VOD in progress; `unpack.sh` merges that
  back with `run_remote.sh pull`'s rules. Uploads and downloads are by hand in the Drive web UI (no rclone yet), and
  the nightly job can't transcribe through Colab: new VODs wait for a manual Colab run. `pack.sh` and `unpack.sh`
  were run on the laptop; the notebook has not been run in Colab yet.

- **Website on the laptop via Tailscale Funnel** (no domain, no port forwarding). `deploy/laptop/install.sh` writes
  systemd units for the logger, site, admin app, nightly timer and a 10-minute GitHub auto-update
  (`deploy/laptop/update.sh`, tested against a local git remote with a stand-in systemctl; install.sh itself is
  syntax-checked only). The old `deploy/*.service` files are replaced by these.
- **Site password is checked by the public app itself** (`site_password_required`, `SITE_USER`/`SITE_PASSWORD` in
  `site.env`) because Funnel has no Caddy in front. It fails closed: no password configured means 503, never an open
  site. Caddyfile kept for a future own-domain setup.
- **archive.db moved to `archive/archive.db`.** The public site runs as the same user as the pipeline but with
  systemd `InaccessiblePaths` over `archive/`, `data/`, `voices/`, `voiceprints/` and `.env` (the spec asked for a
  separate user; a folder mask also covers the `-wal`/`-shm` files that come and go).
- **`site_domain: auto`** takes the Twitch embed `parent` from the host the visitor used.
- **Unverified (tailscale.com not reachable from the build container):** that Funnel passes the visitor's IP in
  `X-Forwarded-For` (if not, the search rate limit is shared by all visitors), and that `tailscale serve` on 8443
  stays tailnet-only alongside Funnel on 443. Check both on the laptop.

- **`deploy/laptop/status.sh`** (not in the spec): one read-only report of the laptop for pasting into a cloud session,
  since those sessions cannot reach the laptop. It also answers two setup questions: whether Funnel forwards visitor
  addresses (counted from uvicorn's access log, which shows the forwarded address under `--proxy-headers`; addresses
  are never printed) and whether the admin app is tailnet-only. Tested here for parsing only, not on the laptop.

- **Rhetoric study** (not in the spec; owner's request 2026-11-10): `research/` holds the design, pre-registered
  predictions, a sourced watchlist (`watchlist.csv`: antisemitism under IHRA and JDA side by side, Holocaust
  distortion, Finkelstein and Mearsheimer talking points, anti-US framing) and the socialscope Kremlin-narratives list.
  Patterns only find candidates; Claude codes stance, and only `endorse` counts. `tests/test_watchlist.py` checks every
  pattern. `pipeline/rhetoric.py` runs the chat part: `scan` (local, free) finds candidates and a
  random sample of the rest; `code` sends them to Claude as one Message Batch (half price; prints a cost estimate and
  submits only with `--yes`); `collect`; `report`. Account names never leave the laptop: Claude sees aliases (u1, u2...).
  Output goes to `archive/rhetoric/` (masked from the public site); `report.md` has aggregates only and `accounts.tsv`
  names accounts (owner's choice; never commit or publish it). Tested with a fake Batches client
  (`tests/test_rhetoric.py`); never run against the real API from the cloud session.

- **Whisper filler lines are filtered at load time** (`hallucinated()` in `common.py`, settings
  `hallucination_phrases` / `hallucination_isolation_seconds`). A segment whose whole text is a stock phrase ("you",
  "Thank you.", "so", "[Music]", ...) or has no letters, with no real speech within 8 s, is left out of `turns`, search,
  QA and the rhetoric study. The transcript JSON is untouched. The filter settings are part of `transcript_hash`, so
  changing them makes `build_db.py` reload every VOD's turns (not its chat). A real "Thank you." said with 8 s of
  silence on both sides would also be dropped; lower the gap if that shows up.

- **Weekly reel** (`pipeline/reel.py`, `web/static/reel.html`, `/api/reels`): the week's most egregious things Hasan
  said, chosen and explained by Claude (`claude-opus-5-5`, Batches API). Pass 1 screens every transcript line of the
  Sunday-Saturday week in 12-minute windows (all speakers labelled; effort low); pass 2 re-judges the strongest 30 with
  6 minutes of transcript and up to 400 chat messages (effort high) against a fixed rubric in `reel.py` (own words only;
  readings, condemnations, mainstream policy criticism excluded; same standard for every group). Quotes and chat
  examples not found verbatim in the input are dropped, so nothing invented is published. Results are cached per stream
  window and per candidate, so a late transcript costs only its own stream. Reads public.db only and writes
  `reels/<week>.json` (public). `nightly.sh` runs `reel.py auto` every hour, including quick runs; it does nothing until
  Sunday `build_after_hour` (10:00), then submits, collects on later hours, writes the reel, and rebuilds only when the
  week's transcripts change. `max_usd_per_week` (15) caps what auto submits. Clips are the Twitch/YouTube embed cued to
  the clip and paused at its end (no video is downloaded); when the VOD is gone the page plays transcript and chat on a
  clock. Tested with a fake API client; the real prompts have not yet run.
- **Reel segments are Claude's choice** (2026-11-22): 15 s to 15 min (`clip_min_s`/`clip_max_s`), with a stated reason;
  the judge pass sees ±10 min of transcript (`context_s` 600) and up to 400 chat messages sampled from 2 min before to
  8 min after the moment.
- **Fact-checks** (2026-11-22): pass 1 also lists checkable factual claims Hasan states as fact (topic, importance 1-5);
  the top 40 by importance (`max_facts`) are checked by Claude with the `web_search_20260209` tool (max 5 searches each),
  as of the day he spoke. Verdicts: accurate, mostly_accurate, misleading, false, unsupported (not_checkable is dropped).
  Deliberately **not "lies"**: a transcript can show a claim is wrong, not that he knew it. Citations can't be combined
  with a JSON schema, so the answer ends in a JSON object parsed from text; only source URLs that the search tool
  actually returned are kept, and a negative verdict with no such source is not published. Shown in the reel page's
  "Fact-checks" view with all-weeks totals by topic (`/api/factchecks`). Estimated ~$8/week in total with the reel.
- The watch page's phone CSS now targets `main.watch #player` / `#slider` instead of the bare ids; the unscoped rules
  reordered the reel page's player on phones.

- **Backups** (`pipeline/backup.py`, 2026-11-23): restic, encrypted and incremental, to each repository in
  `backup.repos` (Google Drive via the laptop's rclone remote, and/or a USB drive). Backs up a consistent snapshot of
  archive.db (SQLite online backup, in steps so the chat logger keeps writing), transcripts, chat replay, muted spans,
  voices, voiceprints, reels, the reel/rhetoric caches, QA labels and logs. Leaves out `.env`/`site.env` (secrets),
  public.db/index (rebuilt), and audio unless `include_audio`. Runs from the hourly job, at most once a day; prunes
  (7 daily, 8 weekly, 12 monthly) and checks 5% of the data weekly. `backup.py restore-test` restores the latest
  archive.db and runs `PRAGMA integrity_check`. Status in `logs/backup-status.json`, `qa.py --health` and `status.sh`.
  Tested against real restic 0.16 with local repositories; Google Drive not tested from the build container.

- **Reader reports and corrections** (`pipeline/feedback.py`, 2026-11-23): every reel clip and fact-check has a
  "Report a problem" form (reason + optional note; 10 per hour per visitor; no IP or name stored). Reports go to
  `feedback/feedback.db`, the only path the public site may write (`ReadWritePaths` in the vod-web unit; re-run
  `install.sh` once). The owner resolves them in the admin app (`flags.html`, "Reports") or with `feedback.py resolve`:
  dismiss, add a public correction note, or withdraw the item. Corrections are applied when a reel is served; withdrawn
  items drop out of the reel and the all-weeks totals and are counted publicly. Items have stable ids
  (`c-<video>-<second>`, `f-<video>-<second>`).

- **Backfill and trends** (2026-11-23): `reel.py auto` works on one week at a time (`archive/reel/auto.json`): the
  latest finished week first, then earlier weeks newest-first from `reel.backfill_from` (rounded to its Sunday), at
  most `max_backfill_weeks` (default 12). Off unless `backfill_from` is set, so importing old streams never starts
  Claude spending by itself. `/api/trends` and the reel page's "Trends" view: per week, clips and false/misleading
  claims per 10 stream-hours, worst and mean severity, verdict counts (corrections applied). Charts follow the dataviz
  method: one small chart per measure (no shared axis), single blue series validated on light and dark, hover
  tooltips, latest value labelled, a table view.

- **Share links** (2026-11-23): `/r/<week>/<item id>` serves a small page with Open Graph / Twitter tags (title,
  severity or verdict, Claude's summary or correction, any review note; all text HTML-escaped) and redirects to the
  item on reel.html. A "Share" button on every clip and fact-check uses the phone's share sheet or copies the link.
  Text-only previews (no image). Withdrawn items return 404. The reviewer's correction note is `review_note` (a
  fact-check's own `correction` field is Claude's "what's accurate").

- **Chat response numbers** (2026-11-23, reel VERSION 4): each clip gets `chat_metrics`, computed from public chat
  without Claude: messages/min 5 min before, during and 5 min after; share of messages echoing his distinctive words
  (5+ letters, from the quote and his lines) vs the 5 minutes before; first-time-chatter share vs the stream; and
  copy-paste bursts: the same message (3+ real words after dropping @mentions, has* channel emotes and common Twitch
  emotes) from 5+ accounts within 60 s. Heuristics, shown as counts on the card under "Chat in numbers"; a burst is a
  signal of coordinated posting, not proof. Existing reels rebuild with them from cache (no Claude cost).

- **Own domain** (`deploy/laptop/domain.sh`, 2026-11-23): Tailscale Funnel only serves `*.ts.net`, so a custom
  domain goes through a Cloudflare Tunnel: cloudflared on the laptop, tunnel `vod-archive`, `DOMAIN` and `www.DOMAIN`
  routed to 127.0.0.1:8000 (never the admin app), run as the `cloudflared` system service. Funnel keeps working.
  cloudflared passes the visitor address in X-Forwarded-For, which uvicorn already trusts from 127.0.0.1. Untested
  from the build container (Cloudflare unreachable there); syntax-checked only.

## Library versions checked
Signatures were read from the wheels, not run: whisperx 3.8.6 (`whisperx.diarize.DiarizationPipeline(token=...)`,
`__call__(..., return_embeddings=True)` returning `(df, {speaker: vector})`), pyannote.audio 4.0.7
(`Model.from_pretrained(..., token=...)`, `Inference(model, window="whole")`, `.crop(file, Segment)`). Older spellings
(`use_auth_token`, no `return_embeddings`) are handled by fallbacks.

## Open questions for the user
1. **publish.py rebuilds public.db from scratch.** That was cheap when it held transcripts only. With every chat
   message it will take minutes and grow with the archive; an incremental publish may be needed after the backfill.
2. **Opt-outs are applied on the next publish**, i.e. up to a day later with the nightly schedule.
