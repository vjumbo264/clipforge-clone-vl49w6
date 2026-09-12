# ClipForge — Architecture v1 (rebuild)

**Status:** Final. Every future session builds against this document verbatim.
**Authority:** This file is the *only* authoritative description of the new
ClipForge design. If the code and this document ever disagree, this document
wins — fix the code, not the document. If you believe this document is wrong,
record the concern in `BUILD_PROGRESS.json` under `architecture_concerns` and
keep building against it as written; do not silently redesign.

---

## 1. Purpose (unchanged from the original tool)

ClipForge turns a **source video** into a **short, edited, narrated, captioned
vertical clip** and optionally **publishes** it. A user drives the whole thing
from a Telegram bot. The system supports two operating modes:

- **Manual mode** — the pipeline produces an analysis bundle and a *prompt*;
  a human pastes that prompt into an external AI agent (any agent, any tool);
  the agent returns a `production.json`; the pipeline renders from it.
- **Series mode** — orthogonal to Manual. When enabled, one source
  video is consumed sequentially in parts; each part ends on a cliffhanger
  and the next part continues from the prior part's end timestamp, reusing
  the original Stage A evidence.

The rebuild must preserve **every** capability the old tool offered. It is
free to restructure everything *except* the two preserved subsystems listed in
§9.

---

## 2. Guiding principles (why this design looks the way it does)

1. **One job, one lifecycle, one state machine.** The old system had states
   scattered across Telegram chat-state storage, `jobs/<id>/status.json`, workflow inputs, and
   release metadata, which is why users found it confusing. The new design has
   a single `Job` entity with a small, explicit state machine (§6) and every
   stage *writes* and *reads* only that entity.
2. **Everything asynchronous is a GitHub Actions workflow; everything
   interactive is the Telegram bot.** The bot never blocks. Workflows are
   idempotent and restartable by `job_id` alone.
3. **All heavy artifacts live in GitHub Releases; all small state lives in
   `jobs/<job_id>/`.** This is retained — it works and it is the only cheap,
   durable, per-repo storage the architecture has.
4. **The contract between "analyze" and "render" is a single JSON file with a
   versioned schema** (`production.json`, §7.3). Manual and Series
   jobs all produce and consume the exact same file — one identical contract
   for every job.
5. **Security boundaries are structural, not conventional.** Cloned
   repositories must be *incapable* of reaching the two original-repo-only
   subsystems, not merely *asked not to*. See §9.
6. **A fresh session must be able to build any one stage in isolation.** Every
   stage's inputs and outputs are fully specified here so that two sessions
   that never talk produce compatible code.

---

## 3. High-level architecture

```
                         ┌─────────────────────────────────────────┐
   USER (Telegram)  ◄──► │  ClipForge Telegram Bot (Cloudflare      │
                         │  Worker — interactive control plane)     │
                         └───────┬───────────────────────┬─────────┘
                                 │ writes job record,     │ polls status,
                                 │ dispatches workflows   │ sends prompts/results
                                 ▼                       ▼
              ┌────────────────────────────────────────────────────┐
              │        USER'S GITHUB REPO (the "clone")            │
              │  jobs/<job_id>/  (small JSON state)                │
              │  GitHub Releases    (large binary artifacts)       │
              │  GitHub Actions     (all heavy compute)            │
              │                                                    │
              │   ┌──────────┐   ┌──────────────┐   ┌───────────┐ │
              │   │ STAGE A  │──►│ PLAN (manual)│──►│  STAGE B  │ │
              │   │ ingest + │   │              │   │  render + │ │
              │   │ analyze  │   │ production.  │   │  publish  │ │
              │   │          │   │ json         │   │           │ │
              │   └──────────┘   └──────────────┘   └───────────┘ │
              └────────────────────────────────────────────────────┘
                                 ▲
                                 │ original repo ONLY
              ┌──────────────────┴──────────────────┐
              │  Central relay (Bot B + workflow)   │  ← preserved subsystem #2
              │  MTProto channel download           │  ← preserved subsystem #1
              └─────────────────────────────────────┘
```

### 3.1 Actors

| Actor | What it is | Trust level |
|---|---|---|
| **Bot A** ("main bot") | Cloudflare Worker, the only Telegram bot the user talks to. Multi-tenant: one deployment serves every user. | Holds per-user encrypted credentials and Bot A→Bot B relay staging records in KV (the only KV keys left after the kv-minimization migration). Holds the relay *encryption* key (shared secret with the central repo). |
| **Bot B** ("relay bot") | Separate Cloudflare Worker. Exists solely to sit in the private internal relay group and route ready-markers to the central repo's `telegram-relay.yml`. | Holds the central repo's PAT. No user ever interacts with it. |
| **User's clone repo** | A private GitHub repo the user owns. Hosts Actions, job state, releases, branding. | Holds user-scoped secrets only (`ZERNIO_API_KEY`). Never holds MTProto or Bot-B credentials. |
| **Original repo** (`motionssalt/clipforge`) | The central, trusted repo. | The *only* place MTProto session secrets and the relay workflow run. |
| **External analysis agent** (Manual mode) | Any AI the human pastes a prompt into. | Untrusted. Its only output is a `production.json` that must pass validation. |

---

## 4. Repository layout (target state for the new version)

Future sessions build this layout. Paths in **bold** already have a design
contract in this document; the rest are named here so all sessions put files
in the same place.

```
/
├── ARCHITECTURE.md                  ← this file
├── BUILD_PROGRESS.json              ← checkpoint, updated on every push
├── NEXT_SESSION_PROMPT.md           ← reusable handoff prompt
├── README.md
├── .gitignore
│
├── bot/                             ← Bot A + Bot B (Cloudflare Workers, JS)
│   ├── package.json
│   ├── wrangler.bot-a.jsonc         ← main bot (was wrangler.jsonc)
│   ├── wrangler.bot-b.jsonc         ← relay bot
│   ├── src/
│   │   ├── index.js                 ← Bot A entry (webhook, router)
│   │   ├── relay-worker.js          ← Bot B entry  [PRESERVED SUBSYSTEM #2]
│   │   ├── relay.js                 ← relay caption/marker codecs [#2]
│   │   ├── crypto.js                ← AES-256-GCM credential + relay sealing
│   │   ├── storage.js               ← storage layer: KV (credentials + relay) / D1 (§8.10)
│   │   ├── github.js                ← GitHub API client (dispatch, files, releases)
│   │   ├── commands/                ← one module per user-facing command (§8)
│   │   ├── jobs.js                  ← job state machine read/write (§6)
│   │   └── plan.js                  ← production.json validation (§7.3)
│   └── test/
│
├── pipeline/                        ← all Python that runs in GitHub Actions
│   ├── stage_a/
│   │   ├── ingest.py                ← source resolution (URL/Drive/torrent/relay/TG-channel)
│   │   ├── telegram_channel.py      ← MTProto public-channel download [#1]
│   │   ├── transcribe.py
│   │   ├── scenes.py                ← shot boundaries, key moments, composites
│   │   └── bundle.py                ← build analysis bundle + 00_READ_THIS_FIRST.txt
│   ├── plan/
│   │   ├── schema.py                ← production.json contract (shared, §7.3)
│   │   └── series.py                ← series continuation derivation
│   ├── stage_b/
│   │   ├── voiceover.py             ← Edge TTS synthesis
│   │   ├── render.py                ← cut/mix/concat (mobile-safe)
│   │   ├── reframe.py               ← vertical reframing
│   │   ├── captions.py              ← subtitles/caption styling
│   │   ├── watermark.py             ← creator watermark
│   │   ├── enhance.py               ← quality filter chain
│   │   └── compress.py              ← delivery compression
│   └── publish/
│       └── zernio.py                ← Zernio publish/schedule/queue
│
├── relay/                           ← central-repo-only relay runner [#2]
│   └── telegram_relay.py            ← MTProto relay download + handoff
│
├── schemas/
│   ├── job_status.schema.json       ← §6.2
│   ├── stage_a_request.schema.json  ← §7.1
│   ├── production_plan.schema.json  ← §7.3
│   └── analysis_bundle.schema.json  ← §7.2 (manifest of release assets)
│
├── .github/workflows/
│   ├── stage-a.yml                  ← ingest + analyze
│   ├── stage-b.yml                  ← render
│   ├── publish.yml                  ← Zernio (replaces zernio-publish + manual)
│   ├── telegram-relay.yml           ← central relay [#2]
│   ├── deploy-bots.yml              ← deploy Bot A + Bot B on bot/ changes
│   ├── cleanup.yml                  ← expire jobs/releases
│   └── diagnostics.yml              ← gemini capability + telegram intake checks
│
├── branding/                        ← per-clone user preferences (small JSON)
│   ├── tts_settings.json
│   ├── creator_watermark.json
│   ├── music_default.json
│   ├── series_settings.json
│   ├── zernio_settings.json
│   ├── zernio_accounts.json
│   └── zernio_queue.json
│
├── assets/                          ← fonts, LUTs, TTS previews (static)
├── audio-library/                   ← user's music tracks (clone-local)
├── jobs/                            ← per-job small JSON state (transient)
└── _legacy/                         ← the entire previous implementation, frozen
```

### 4.1 Migration note for builders

The new code may freely **copy** logic out of `_legacy/` and adapt it into the
new layout above. It must not **import from or execute** anything under
`_legacy/` at runtime. The two preserved subsystems (§9) are the *only*
exception: their logic is ported essentially verbatim, and their security
boundaries are re-established in the new layout exactly as specified in §9.

---

## 5. Source intake

A job always begins with the user supplying a source in the bot. The new
design keeps every intake path the old tool supported and presents them as
one uniform step ("send me the video or a link"). The bot normalizes all of
them into a single `source` object (§7.1).

| Source kind | User input | How Stage A obtains it | Availability |
|---|---|---|---|
| Direct video to bot | User forwards/uploads a video message to Bot A | Private relay (§9.2): Bot A → internal group → Bot B → `telegram-relay.yml` → temporary release asset in the clone → Stage A downloads it | All clones |
| Public Telegram channel post | `https://t.me/<channel>/<msg_id>` link | MTProto user-authorized download (§9.1) | **Original repo only** |
| Direct file URL | `https://…/video.mp4` | Plain HTTP(S) download | All clones |
| Google Drive | anyone-with-link URL or file id | Drive download with confirm-token handling | All clones |
| Magnet URI | `magnet:?…` | aria2 metadata fetch → video candidate list → user picks one → torrent download | All clones |
| `.torrent` file | Upload `.torrent` (≤ 1 MB) to the bot | Torrent metadata parsed → video candidate list → user picks one → torrent download | All clones |

**Deliberately disabled** (unchanged from old): YouTube, TikTok, Instagram,
Facebook, X/Twitter, Vimeo, Reddit page links. The user is directed to put the
video on a public Telegram channel or send it directly. The bot rejects these
hosts at intake with a helpful message.

**Torrent/magnet selection flow:** when the source resolves to multiple video
files, Stage A does *not* pick one. It writes a `torrent-selection.json` into
the job, sets the job state to `awaiting_torrent_selection`, and stops. The
bot presents the candidate list (paginated) and, on the user's pick,
re-dispatches Stage A with `torrent_file_index` set. This matches the old
behavior and must be preserved.

---

## 6. The Job state machine

This replaces the old scattered status vocabulary. There is **one** job state
machine. Every stage reads and writes a single `jobs/<job_id>/status.json`.

### 6.1 States

```
                 ┌──────────┐
                 │  queued  │   job record created, Stage A dispatched
                 └────┬─────┘
                      ▼
            ┌───────────────────┐
            │  stage_a_running  │──► error
            └────┬───────────┬──┘
                 │ torrent   │ single/multi-file resolved
                 │ needed    ▼
                 │   ┌──────────────────────────┐
                 │   │ awaiting_torrent_        │──(user picks)──► stage_a_running
                 │   │ selection                │
                 │   └──────────────────────────┘
                 ▼
        ┌────────────────────────┐
        │ awaiting_plan          │  (waiting for the human
        │                        │   to return production.json)
        └──────────┬─────────────┘
                   │ plan uploaded & valid
                   ▼
        ┌────────────────┐
        │ stage_b_queued │──► stage_b_running ──► complete
        │ stage_b_queued │──► stage_b_running ──► complete
        └────────────────┘            │
                                      ▼
                                   error / cancelled
```

| State | Meaning | Terminal? |
|---|---|---|
| `queued` | Record exists; Stage A not yet observed running | no |
| `stage_a_running` | Ingest/transcription/analysis in progress | no |
| `awaiting_torrent_selection` | Multi-file source; waiting on user's file pick | no |
| `awaiting_plan` | Bundle ready; waiting for `production.json` | no |
| `stage_b_queued` | Valid plan present; Stage B dispatched | no |
| `stage_b_running` | Render in progress | no |
| `complete` | Final asset(s) published to the release | **yes** |
| `error` | A stage failed; message says which and why | **yes** |
| `cancelled` | User cancelled a running Stage B | **yes** |

Publishing (Zernio) is **not** a job state. It is metadata attached to a
`complete` job (`status.publishing`) so a publish failure never moves the job
out of `complete`. This is a deliberate simplification over the old flow.

### 6.2 `jobs/<job_id>/status.json` schema

```json
{
  "version": 2,
  "job_id": "manual-1787692652625",
  "mode": "manual",
  "series": {
    "enabled": false,
    "series_id": "",
    "part": 0,
    "start_seconds": 0,
    "is_final": false
  },
  "state": "awaiting_plan",
  "message": "Human-readable progress line",
  "created_at_epoch": 1787692652,
  "updated_at_epoch": 1787693010,
  "expires_at_epoch": 1787736000,
  "release_tag": "clipforge-manual-1787692652625",
  "release_url": "https://github.com/<owner>/<repo>/releases/tag/clipforge-...",
  "assets": {
    "analysis_bundle_url": "https://…",
    "final_mp4": "https://…",
    "final_zip": "https://…"
  },
  "run": {
    "workflow_run_id": 0,
    "workflow_run_url": "https://…",
    "code_ref": "<sha this run executed>"
  },
  "publishing": {
    "status": "not_requested | publishing | scheduled | published | partial | failed | cancelled",
    "posts": [],
    "idempotency_key": ""
  }
}
```

Rules:

- A stage writes status **before** starting its risky work and **after**
  finishing it. A crashed run therefore always leaves a resumable state.
- `expires_at_epoch` drives cleanup (§12). It is computed at job-creation
  time as `created_at_epoch + TTL`, where the TTL comes from ONE configured
  source — the repo Actions variable `CLIPFORGE_TTL_SECONDS` (currently
  172800 = 48h) — threaded into every writer (see §12). The hardcoded
  `DEFAULT_TTL_SECONDS = 12 * 3600` in pipeline/status.py and bot/src/jobs.js
  is only the no-config fallback (local runs / tests).
- All writes are small, atomic single-file commits. Workflows must
  `git pull --rebase --autostash` before pushing to survive concurrent jobs.

### 6.3 Job identity and labels

- `job_id` = `<mode>-<epoch_ms>` (e.g. `manual-1787692652625`). Series parts
  get `<series_id>-p<N>`. Character set `[A-Za-z0-9._-]`, max 120 chars.
- The bot assigns a short, stable **human label** per job (`A`, `B`, `C`…)
  scoped to the Telegram chat, stored in the D1 `task_labels` table (§8.10).
  Users never type job ids.

---

## 7. Stage contracts

Every handoff is a versioned artifact with a precise shape. Two sessions
building against these contracts independently must produce compatible code.

### 7.1 Stage A request — `jobs/<job_id>/stage-a-request.json`

Written by the bot *before* dispatching Stage A; re-read on every Stage A
restart. This is the durable record of "what the user asked for."

```json
{
  "version": 2,
  "job_id": "manual-1787692652625",
  "source": {
    "kind": "url | drive | magnet | torrent_file | telegram_channel | telegram_relay",
    "value": "https://…  | magnet:… | path:jobs/<id>/source.torrent | relay:private",
    "relay": {
      "release_tag": "",
      "expected_size_bytes": "",
      "sha256": ""
    },
    "torrent_file_index": ""
  },
  "options": {
    "whisper_model": "tiny | base | small",
    "language": "auto",
    "task": "translate_to_english | transcribe",
    "target_duration_seconds": 120,
    "focus": "",
    "enable_vision_assist": true
  },
  "mode": "manual",
  "series": {
    "enabled": false,
    "series_id": "",
    "source_job_id": "",
    "part": 0,
    "start_seconds": 0,
    "context": ""
  },
  "music": {
    "ref": "",
    "source": "none | default | explicit_library | job_upload"
  },
  "saved_at_epoch": 1787692652
}
```

Notes:
- `source.kind = telegram_channel` is only ever accepted when the running repo
  is the original repo (§9.1). Stage A re-validates this; the bot's gate is a
  UX convenience, not the security boundary.
- `source.kind = telegram_relay` is the Bot A → Bot B relay path (§9.2). The
  relay workflow rewrites this record with the real `relay.release_tag`,
  `expected_size_bytes`, and `sha256` before dispatching Stage A.
- `series.context` is the pre-joined prior-part summaries (≤ 8000 chars),
  derived the same way the old `series_state.py` derived it.

### 7.2 Stage A output — the analysis bundle (GitHub Release assets)

On success Stage A creates/updates release `clipforge-<job_id>` containing:

| Asset | Content |
|---|---|
| `source_input.bin` | The original, full-quality source video (kept for Stage B). |
| `transcript.json` | Timestamped faster-whisper transcript. |
| `screenshots.zip` | Baseline 6-frame composites (one per 6s window). |
| `event_composites.zip` | Dense composites for high-signal beats (if vision assist on). |
| `scene_index.json` | Shot boundaries. |
| `key_moments.json` | Ranked key-moment shortlist. |
| `00_READ_THIS_FIRST.txt` | The analysis prompt (see below). |
| `manifest.json` | Machine-readable index of all of the above (name, size, sha256, purpose). |

**`source_input.bin` and the 2 GiB per-asset limit (bug-69).** GitHub imposes a hard
2 GiB limit per release asset. Stage A always attempts the FULL bundle (source
included) first — there is no pre-emptive size skip. Only if that upload actually
fails on the oversized source does Stage A retry the release WITHOUT
`source_input.bin` (keeping every other asset), and records the intentional
omission unambiguously: the release body carries `source-omitted: true` and
`manifest.json` gains `source_omitted: true` + `source_omitted_reason` (with the
`source_input.bin` asset entry dropped). This is NOT a broken release. When the
source is absent, Stage B (or a series continuation) re-fetches it directly from
the original source reference durably saved in `jobs/<id>/stage-a-request.json`,
via `pipeline/stage_a/refetch_source.py` — which reuses `ingest.py`'s existing
per-kind resolution (url / drive / telegram_channel / telegram_relay / magnet /
torrent_file) and, for torrents, the exact saved `torrent_file_index` with no
interactive re-prompt. Series continuations share Part 1's underlying source and
re-fetch from Part 1's saved source reference.

**`00_READ_THIS_FIRST.txt`** is the *prompt* for Manual mode. It fully
describes the task, the available evidence, the narration-length target
(spoken-narration pacing only — never a video or cut length; the TOTAL
narration word budget `target x (188/60)` at 90%-115% is what converges the
final runtime to that target, since Stage B retimes footage to narration), any focus
directive, and — most importantly — the exact `production.json` shape to
return (§7.3). **This file is generated by `pipeline/plan/prompt.py` logic and its
wording is part of the contract** — change it deliberately, not incidentally.

### 7.3 The production plan — `production.json` (the core contract)

This is the single most important artifact in the system. It is what the
external agent produces, and what Stage B
consumes. It is the same file for every job and every series part.

```json
{
  "version": 2,
  "job_id": "manual-1787692652625",
  "title": "Optional human title",
  "video_duration_seconds": 1320,
  "target_total_duration_seconds": 120,
  "cuts": [
    {
      "start_seconds": 12,
      "end_seconds": 30,
      "voiceover_text": "The exact words the narrator speaks over this cut."
    }
  ],
  "hashtags": ["#one", "#two", "#three", "#four", "#five"],
  "youtube_tags": ["tag1", "tag2", "…up to 20"],
  "series": {
    "series_id": "",
    "part": 0,
    "start_seconds": 0,
    "end_seconds": 0,
    "is_final": false,
    "summary": "≤1200 chars continuity note for the next part"
  }
}
```

Validation rules (enforced identically by the bot on upload *and* by Stage B
before rendering — never trust the producer):

- `video_duration_seconds`, `target_total_duration_seconds`: required positive
  integers.
- `cuts`: required, ≥ 1 entry. Each cut: integer `start_seconds ≥ 0`,
  `end_seconds > start_seconds`, `end_seconds ≤ video_duration_seconds`. Cuts
  are strictly sorted and non-overlapping. `voiceover_text` is a non-empty
  string. (Legacy field `raw_narration` is accepted and treated as
  `voiceover_text`.)
- `hashtags`: optional; if present 5–8 entries, each `#`-prefixed, no
  whitespace, case-insensitively unique.
- `youtube_tags`: optional; if present 10–20 entries, no `#` prefix, no
  commas, unique.
- `series.*`: present only for series parts. `summary` ≤ 1200 chars.
- Unknown top-level fields are allowed (forward compatibility).

The canonical schema lives at `schemas/production_plan.schema.json` and the
single validator is `pipeline/plan/schema.py` (exposed to the bot as
`bot/src/plan.js`). **There is exactly one source of truth for these rules.**

#### 7.3.1 The single reference timeline (scene-accuracy invariant)

Every timestamp that flows through the pipeline — shot boundaries in
`scene_index.json`, segment start/end in `transcript.json`, the seconds
encoded into screenshot-composite filenames (`frame_NNNNNN.jpg` covers source
seconds `[N, N+6)`, `event_MMMMMMMMM.jpg` is centered at `M/1000`s), and every
cut's `start_seconds`/`end_seconds` in `production.json` — is expressed on
**one and only one timeline: the original ingested source video's own
zero-based presentation-time timeline** (`work/original.<ext>`, the file
recorded in `jobs/<job_id>/ingest.json` as `original_path`).

Producers and consumers of that timeline, and why they agree:

- **Scene detection** (`pipeline/stage_a/scenes.py`) runs ffmpeg's
  `select='gt(scene,T)',showinfo` directly on the original and parses
  `pts_time` — the container's own timestamps, no re-encode, no offset.
- **Transcription** (`pipeline/stage_a/transcribe.py`) runs on
  `work/audio.wav`, extracted from the SAME original with
  `ffmpeg -i <original> -vn -ac 1 -ar 16000` (no `-ss`/`-t`), so the WAV is
  1:1 time-locked to the source and Whisper's segment times are source times.
- **Screenshot composites** are extracted from the same original; their
  filenames encode source seconds; the window size used to
  build them is recorded in the bundle and the prompt, with no offset.
- **Stage B extraction** (`pipeline/stage_b/render.py`) cuts with per-cut
  input-side `-ss <start> -to <end> -i <original>` against the same original
  re-obtained for the render. `setpts` retiming (speed reconciliation against
  the voiceover) changes ONLY playback speed of the selected footage — it
  never moves which source frames a cut selects.

**The one value that can silently break this contract is the plan's declared
`video_duration_seconds`.** It records the length of the source the planner
saw; every cut's seconds are only meaningful if the file Stage B renders is
that same source. The schema validator checks each cut's `end_seconds ≤
video_duration_seconds`, and Stage B separately checks each cut against the
REAL probed duration — but a plan written against a *different-length* source
(stale plan, wrong/older re-fetched source via the bug-69 path, or a
hallucinated duration) satisfies both checks while pointing every cut at the
wrong scene.

To make that impossible, Stage B
(`render.assert_declared_duration_matches_source`) refuses to render when the
declared `video_duration_seconds` and the source file's probed duration differ
by more than **2 s** (a tolerance that absorbs only integer rounding of the
ffprobe value), failing with an error that names both durations. A render that
would produce footage mismatched to its narration is never produced silently.
Regression coverage: `pipeline/tests/test_scene_accuracy.py`.

### 7.4 Stage B output

Stage B appends to the same release:

| Asset | Content |
|---|---|
| `final.mp4` | The rendered vertical clip (mobile-safe H.264/AAC). |
| `final.zip` | final.mp4 + production.json + metadata for archival. |

Stage B also writes per-cut voiceover WAVs and intermediate files to the
runner workspace only — these are **not** released (only the final merged MP4
and ZIP ship). On success it sets state `complete`; on user cancel,
`cancelled`; on failure, `error` with a safe message.

---

## 8. Telegram bot — command and flow design (the UX redesign)

The old bot's confusion came from three things: too many top-level commands,
settings mixed with task actions, and a setup flow that silently spanned many
messages. The new design fixes this with **one persistent menu, one task
wizard, and settings behind a single screen.**

### 8.1 Principles

1. `/start` always shows the **same home screen** — a single self-editing
   message with inline buttons. Navigation edits that one message instead of
   piling up new ones (the old bot did this with a stored `activeViewId`;
   kv-minimization made it stateless — the edit target is now implicit in
   the update itself: button presses edit `callback.message.message_id`,
   and renders answering a user-sent message always send fresh, §8.9).
2. **Exactly one way to make a video:** the "New video" wizard. There are
   no separate per-mode commands.
3. **Settings never interrupt a task.** All settings live behind "Settings".
4. Every multi-step flow has explicit **Back** and **Cancel** on every screen.
5. Secrets typed into chat (PATs, API keys) are deleted immediately after
   receipt.

### 8.2 Command surface (the *entire* list)

| Command | Action |
|---|---|
| `/start` or `/help` | Show the home screen (same as tapping "Menu"). |
| `/new` | Start the New-video wizard. |
| `/tasks` | List active tasks. |
| `/done` | List completed tasks (with download links). |
| `/settings` | Open settings. |
| `/cancel` | Abort the current wizard/input flow. |

No other commands.

### 8.3 Home screen

```
ClipForge
Connected to: owner/repo   ·   Narrator: Andrew   ·   Series: off
Zernio: smart schedule on

[ 🎬 New video ]
[ 📋 Tasks ]        [ ✅ Completed ]
[ ⚙️ Settings ]
```

### 8.4 The New-video wizard (single linear flow)

Step order is fixed. Each step is one screen with Back/Cancel.

1. **Series toggle** — `[Series ▢→☑ toggle]`: chain this video into
   sequential cliffhanger parts. (Every job uses the same manual
   external-agent flow — there is no mode choice in the wizard.)
2. **Source** — "Send the video, or paste a link / magnet / upload a
   `.torrent`." Accepted kinds per §5. Invalid input → specific error, stay on
   this step.
3. **Focus** *(skipped when Series is on — series has no editorial focus)* —
   "Optionally narrow the analysis to one thread, or send `-` for the whole
   video."
4. **Length** — pick the target spoken-narration length (pacing only,
   never a video/cut length): `30s · 60s · 120s · 180s · 300s`.
5. **Music** — `[No music] [Use saved default] [Choose library track]`.
6. **Confirm & start** — a summary of every choice, then `[▶ Start]`.

On confirm, the bot: writes `stage-a-request.json`, handles relay sealing if
the source was a direct video, dispatches Stage A, and lands the user on the
new task's status screen.

### 8.5 Task status screen

Shows current state, progress message, links (workflow run, release), and
**contextual buttons only for actions valid in this state**:

- `awaiting_torrent_selection` → **Choose video file** (paginated list).
- `awaiting_plan` → **Get agent prompt** + **Upload production.json**.
- `stage_b_running/queued` → **Cancel Stage B** (with confirm).
- `error`/`cancelled` → **Restart Stage A** / **Restart Stage B**.
- `complete` → **Download**, **Publish (Zernio)**, and — for non-final manual
  series parts — **Start next part**.
- Always: **Refresh**, and (when terminal) **Delete task**.

Restarts always resolve the *current* default-branch SHA and pass it as
`code_ref`, so a restart never re-runs stale code (carried over from old).

### 8.6 Settings

A single screen listing: GitHub clone, Gemini keys, Narrator (Edge TTS, with
per-voice preview), Music library (upload/preview/default/delete), Watermark,
Series Mode default, Zernio publishing. Each opens its own sub-screen. All
secrets are written as GitHub Actions *sealed* secrets; the repo only ever
stores masked fingerprints.

### 8.7 Manual-mode agent handoff (the core loop, redesigned for clarity)

The old loop was conceptually fine but buried. The new presentation:

1. When a Manual job reaches `awaiting_plan`, the status screen shows a
   **"Get agent prompt"** button.
2. Tapping it sends (a) a copyable prompt text and (b) an **Open GitHub
   Release** URL button. The prompt tells any external agent: open the
   release, read `00_READ_THIS_FIRST.txt`, inspect the evidence, and return
   exactly one `production.json`.
3. The user runs their agent, gets `production.json`, and taps **"Upload
   production.json"**, then sends it as a file (`.json`, `.txt`, `.md`, or
   any text file containing the plan — the pasted-text upload path was
   deliberately removed; see the `upl` row below).
4. The bot validates it against §7.3. If invalid, it replies with the specific
   errors and stays in the upload state. If valid, it commits the plan and
   dispatches Stage B.

That is the entire loop. No hidden state, no extra files, no ambiguity.

### 8.9 Stateless menu/flow wire format (kv-minimization migration)

Bot A keeps **no per-chat navigation record in any datastore**. Neither KV
nor D1 ever stores `flow`, `pending`, `currentTask`, or `activeViewId`.
The current step of an input flow travels inside the bot's own messages:

**The flow marker.** Every input prompt and every wizard/keyboard message
ends with an invisible `text_link` entity whose URL carries the payload:

```
<a href="https://cf.invalid/f/cf:<op>:<arg1>:<arg2>">&ZeroWidthSpace;</a>

op   — [a-z][a-z0-9]{1,11}
args — [A-Za-z0-9._~-]*  (never the ':' separator; base64url for blobs)
```

The `.invalid` TLD is reserved (RFC 2606): the link never resolves and only
the zero-width-space anchor exists in the visible text. There is **no
64-byte limit** on this channel (that limit applies to `callback_data`,
which this design leaves unchanged); message text allows ~4 KB, so large
records (the wizard object, the music-upload staging list) travel as
base64url-encoded JSON token args (`encodeToken`/`decodeToken` in
`bot/src/flow.js`).

**Reply flows (free-text input).** The prompt is sent with
`reply_markup = { force_reply: true, selective: true }`
(`sendForceReply` in `bot/src/telegram.js`), optionally with an inline
keyboard (e.g. a Cancel button) on the same message. The user's answer
arrives as `message.reply_to_message` pointing back at the prompt;
`parseFlowReply()` accepts the message only when the replied-to message
(a) was sent by a bot and (b) carries a well-formed marker. Matching is by
**opcode string**, not message id.

**Button flows.** The same marker rides in the keyboard message's own
text, so the callback handler reads its state back out of
`callback.message` (`parseFlowMessage`) instead of a stored row, and
`callback_data` stays a tiny opcode (`wz:dur:60`, `wz:confirm`, …).

**Current opcodes** (args in brackets are payload args):

| op | args | flow |
| --- | --- | --- |
| `wzs` | wizard-token | wizard step (button steps: source/focus text, length, music, confirm) |
| `upl` | label | production.json upload — file upload only, any UTF-8-decodable text file (the `uplb` multi-bubble paste-reassembly opcode and its D1 fragment buffer were deliberately removed; migration 0004 drops `plan_upload_buffer`) |
| `clname` | — | Shadow Clone repo-name entry |
| `patnew` | b64url(`{ n: clone-name }`) | PAT entry for clone creation (`n: ''` = auto-name sentinel; `decodeToken` returns only objects, so the name rides a one-field record, not a bare string) |
| `patc` | — | PAT entry for connecting an existing clone |
| `repo` | — | owner/repo entry (PAT rides in encrypted credentials KV) |
| `gemkey` | — | Gemini API key entry (dead UI since bug-30, converted anyway) |
| `wm` | — | watermark text |
| `news` | — | news broadcast compose |
| `zkey` | — | Zernio API key |
| `zsch` | field | smart-schedule field (timezone/interval/time/depth/custom_start) |
| `tpm` | label | Zernio manual publish time |
| `tppm` | label, post-id | Zernio manual reschedule time |
| `mupl` | list-token | music-upload batch staging list (rides the collected-files view) |

**Security note.** Markers are visible to the chat owner and can be
re-injected. Decoded args are untrusted input: handlers shape-validate
them (`decodeWizardToken` in `bot/src/wizard.js`), and the commit-boundary
gates (e.g. the §9.1 clone gate re-check in `startJob`) stay in place.

---

### 8.10 Storage backends (kv-minimization migration — authoritative)

Bot A uses two Cloudflare storage backends with strictly separated duties.
Nothing else may touch either backend:

**KV (`CLIPFORGE_BOT_KV`) — three key families only:**
| Key | Writer/reader | Purpose |
| --- | --- | --- |
| `user:<chatId>:credentials` | `getCredentials`/`putCredentials`/`deleteCredentials` (storage.js) | AES-256-GCM envelope of the chat's GitHub PAT + repo (§10) |
| `relay:<chatId>:<jobId>` | `putRelayJob`/`getRelayJob` (storage.js, TTL 12 h) | Bot A → Bot B staged relay handoff record (§9.2) |
| relay update-dedup keys | `markRelayUpdateSeen`/`alreadySeenRelayUpdate` (relay-worker.js, Bot B only) | Bot B's dedup gate on the internal relay group — protects the MTProto/bot-A→bot-B path; deliberately NOT removed by the migration |

**D1 (`CLIPFORGE_BOT_D1`, database `clipforge-bot-state`) — Bot A only:**
| Table | Primary key | Contents |
| --- | --- | --- |
| `task_labels` | (chat_id, label) | task label → job_id bindings; lowest-free-label reuse (§6.3) |
| `task_options` | (chat_id, job_id) | per-task options JSON blob |
| `clone_jobs` | (chat_id) | Shadow Clone resume records (bug-51), incl. the encrypted PAT envelope (same crypto.js AES-GCM, storage location only changed); a row's existence IS the cron sweep's scan set |
| `announcements` | (chat_id, kind) | last-announced markers for kinds `update_notice`, `deploy_failure`, `news_notice` |

Bot B (relay-worker) has **no D1 binding** — it never reads task, clone, or
announcement data. Menu/flow navigation state lives in **no** datastore
(§8.9). Bot A's webhook has **no update-dedup marker**: every handler is
idempotent by construction (phase-6 audit in MIGRATION_PROGRESS.json).
Schema source of truth: `bot/migrations/`.

---

## 9. Two subsystems that must not be redesigned

These are fragile, hard-won, and security-sensitive. **Port their logic
essentially verbatim. Do not redesign them.** You may change *where the code
lives* and *how it is organized*, but not the mechanics, credential flow, or
trust boundaries.

### 9.1 Public Telegram channel-link MTProto download — **original repo only**

- **What it does:** downloads video from a *public* Telegram channel post link
  using a user-authorized MTProto session (Telethon). Handles large media via
  a bounded parallel-connection transfer with direct-offset writes and a
  single-connection fallback. Accepts only `t.me/<channel>/<msg_id>` public
  *channel* posts; rejects groups, private links, and non-post pages.
- **Where it lives (new):** `pipeline/stage_a/telegram_channel.py` (ported
  from `_legacy/scripts/download_drive.py`'s Telegram path, incl.
  `_download_telegram_parallel`).
- **The restriction (preserve exactly):** this path is gated on the running
  repository being the original ClipForge repo. The legacy enforcement is the
  check `credentials.repo === ORIGINAL_CLIPFORGE_REPOSITORY
  ('motionssalt/clipforge')` in the bot (`permitsLegacyTelegramMtproto` in
  `_legacy/telegram-bot/src/index.js`), **plus** the fact that the MTProto
  secrets (`CLIPFORGE_TELEGRAM_API_ID/HASH/SESSION`) exist *only* as Actions
  secrets on the original repo — clones never receive them.
- **New-design rule:** Stage A must independently re-verify
  `github.repository == 'motionssalt/clipforge'` before attempting an MTProto
  channel download, and must fail closed if the secrets are absent. The bot
  keeps its UX-side rejection for clones, but the *security boundary* is the
  missing secrets + the server-side repo check. Do not weaken either layer.

### 9.2 Direct-video-to-bot relay — Bot A → private group → Bot B → Actions

- **What it does:** lets any user (including clone owners) send a video
  straight to Bot A without a link. Bot A copies the message into a private
  internal Telegram group; Bot B (in that group) observes it, and when Bot A
  posts a signed "ready" marker, Bot B dispatches `telegram-relay.yml` **on the
  central repo**. That workflow uses Bot-B-authorized MTProto to download the
  video from the group, then hands it to the *target clone's* Stage A as a
  temporary private release asset (with expected size + SHA-256), and finally
  deletes the temp asset.
- **Where it lives (new):**
  - Bot A side: `bot/src/relay.js` + the relay branch of `bot/src/index.js`.
  - Bot B side: `bot/src/relay-worker.js`.
  - Central workflow: `.github/workflows/telegram-relay.yml` +
    `relay/telegram_relay.py` (ported from `_legacy/scripts/telegram_relay.py`).
- **Security boundary (preserve exactly):**
  - The relay payload Bot A hands to Bot B → the central workflow is an
    **AES-256-GCM sealed envelope** keyed by `RELAY_ENCRYPTION_KEY`, with AAD
    `clipforge-telegram-relay:v1:<job_id>`. It contains the target repo, the
    user's sealed PAT, the Stage A inputs, and the Telegram media coordinates.
  - Bot-B and MTProto credentials (`BOTB_MTPROTO_*`, `CLIPFORGE_TELEGRAM_*`,
    `RELAY_ENCRYPTION_KEY`) exist **only** as secrets on the central repo and
    as Worker secrets on the bots. **They must never become reachable from a
    cloned repository's workflow, files, logs, or workflow inputs.** The clone
    only ever receives a *temporary, prerelease, checksummed* release asset.
  - The envelope carries the *user's own* PAT for writing into the *user's*
    clone; for the central repo itself the workflow substitutes its own
    `github.token` (`handoff_token` logic). Preserve this.
  - Bot B only acts on updates that are (a) in the internal group, (b) from
    Bot A's bot id, and (c) shaped like a relay caption or ready-marker —
    checked *before* any KV access.
- Size cap: 1800 MiB per relayed video (GitHub release per-asset limit with
  headroom). Preserve.

**Bug-fix exception:** you may touch these only for a narrowly-scoped, clearly
justified bug fix, and the fix must be minimal — never a rewrite. Document any
such fix in your session summary and in `BUILD_PROGRESS.json`.

---

## 10. Multi-tenancy and Shadow Clones

- One shared Bot A deployment serves all users. Each private Telegram chat is
  bound to exactly one GitHub repo (that user's clone).
- Onboarding offers **Create private Shadow Clone** (bot creates a private
  repo under the user's account and copies shared source, excluding
  `branding/`, `jobs/`, `audio-library/`, and any `keys|accounts|queue`
  paths) or **Connect existing clone** (validates `owner/repo` + PAT scopes).
- Per-chat credentials (GitHub PAT, Gemini keys) are AES-256-GCM encrypted in
  Workers KV with per-chat AAD; raw secrets are never logged, echoed, or
  committed.
- Gemini keys are uploaded to the clone as the sealed `GEMINI_API_KEYS`
  Actions secret (libsodium sealed box against the repo's Actions public key).
  The repo stores only masked fingerprints in `branding/gemini_keys.json`.
  Existing site-managed secrets are treated as opaque and never overwritten
  without an explicit, confirmed replacement flow.

---

## 11. Modes in the new structure

- **Manual mode:** Stage A → `awaiting_plan` → human/agent loop (§8.7) →
  Stage B → (optional) Publish.
- **Series mode (orthogonal):** when on, Part 1 starts at source second 0. On
  completion of a non-final part, the *next* part is derived from persisted
  series state (`pipeline/plan/series.py`): new job id `<series_id>-p<N+1>`,
  `series_start_seconds` = prior part's `series_end_seconds`, and a context
  string of prior part summaries. The bot exposes a **Start next part**
  button on the completed part. Reuse of Part 1's Stage A evidence is
  preserved. The final part sets `series.is_final = true`, which stops the
  chain.

---

## 12. Background workflows

- **cleanup.yml** (hourly): deletes jobs (and their releases/tags/ref) whose
  `expires_at_epoch` has passed. A job's explicit `expires_at_epoch` is
  authoritative; `CLIPFORGE_TTL_SECONDS` (env, sourced from the repo Actions
  variable of the same name, currently 172800 = 48h) only governs jobs with
  NO readable `expires_at_epoch` (legacy "older than N seconds" rule).
- **TTL single source of truth** (fix-ttl-config-disconnect, see
  TTL_FIX_PROGRESS.json): the job lifetime is configured ONCE as the repo
  Actions variable `CLIPFORGE_TTL_SECONDS` (Settings → Secrets and variables
  → Actions → Variables), currently 172800 = 48h. That ONE value is consumed
  by every place a TTL is needed:
  1. `cleanup.yml` env `CLIPFORGE_TTL_SECONDS: ${{ github.event.inputs.ttl_seconds
     || vars.CLIPFORGE_TTL_SECONDS || '172800' }}` (dispatch input override,
     then the variable, then a matching fallback);
  2. `stage-a.yml` env `CLIPFORGE_TTL_SECONDS: ${{ vars.CLIPFORGE_TTL_SECONDS
     || '172800' }}`, passed to `python -m pipeline.status --ttl-seconds` on
     the job-creation writes;
  3. the Telegram bot mirrors it as `CLIPFORGE_JOB_TTL_SECONDS` in
     `bot/wrangler.*.jsonc` (the bot writes every new job's initial
     status.json via `bot/src/jobs.js::newStatus`).
  ALL of these must agree. `pipeline/status.py::DEFAULT_TTL_SECONDS` (12h)
  is only the no-config fallback. Drift between the creation-time TTL and
  the sweep TTL was the root cause of the operator's "48h not taking effect"
  report; the cross-referencing comments at each site must be kept in sync.
  Note the fallback literals ('172800') are in-repo mirrors of the variable —
  if the variable changes, update them too (they exist so the intended TTL
  holds even before the variable exists / for forks).
- **deploy-bots.yml** (on `bot/**` changes): runs bot tests, deploys Bot A and
  Bot B Workers.
- **diagnostics.yml** (manual): the public
  Telegram intake check (positive: a known public channel post downloads;
  negative: a public *group* link fails). Preserved behavior.

---

## 13. Security invariants (must hold in the new build)

1. No secret is ever committed, logged, printed, or sent to Telegram. The bot
   redacts token-shaped strings in error messages.
2. Clones can never reach §9.1 or the §9.2 central credentials. This is
   enforced by missing secrets + server-side repo checks, not by politeness.
3. Webhooks authenticate via `X-Telegram-Bot-Api-Secret-Token`; all other
   requests get 401. Bot A serves private chats only.
4. All external input (URLs, torrents, plans, captions) is validated and
   size-bounded before use. Zip extraction is bounded (entry count, per-member
   and total bytes).
5. `production.json` is *always* treated as untrusted and re-validated at the
   Stage B boundary even though the bot validated it at upload.

---

## 14. What is intentionally *not* changing

- GitHub Releases as artifact storage and `jobs/<id>/` as state storage.
- Edge TTS for narration and the curated voice catalog (with previews).
- The mobile-safe single-pass render policy (H.264 High@L4.0 / yuv420p /
  30fps CFR / AAC-LC 48kHz / faststart, no B-frames). These are hard-won.
- The `production.json` core contract — cut/hashtag/tag field names and
  validation semantics (§7.3). Series data uses the nested `series` object
  per §7.3; the legacy flat `series_*` siblings are accepted as input only,
  for backward compatibility with plans already in flight, never emitted.
- The series continuation derivation semantics.
- The Zernio publishing model (discover/publish/retry/update/cancel, smart
  schedule, per-post management, durable serial queue).

---

## 15. Build order (recommended, and encoded in BUILD_PROGRESS.json)

1. **Contracts & schemas** — `schemas/*.schema.json`,
   `pipeline/plan/schema.py`, `bot/src/plan.js` (the single validator),
   `pipeline/write_status` equivalent, job state machine module.
2. **Stage B core render** — voiceover → render → reframe → captions →
   watermark → enhance → compress, driven by a hand-written valid
   `production.json`. (No bot needed to test.)
3. **Stage A ingest + analyze** — all source kinds *except* the two preserved
   subsystems, producing the bundle + `00_READ_THIS_FIRST.txt`.
4. **Bot A core** — webhook, KV, crypto, home/tasks/settings, New-video
   wizard, manual handoff, status actions.
5. **Preserved subsystem #2 (relay)** — Bot B + `telegram-relay.yml` +
   `relay/telegram_relay.py`, wired into Bot A's direct-video source.
6. **Preserved subsystem #1 (channel MTProto)** — `telegram_channel.py` gated
   to the original repo.
7. **Series mode** — continuation derivation + manual "next part" button.
8. **Publish (Zernio)** — workflow + settings + per-post management.
9. **Background workflows** — cleanup, deploy-bots, diagnostics.

Each step is independently testable and independently committable. Build them
in this order unless `BUILD_PROGRESS.json` says otherwise.
