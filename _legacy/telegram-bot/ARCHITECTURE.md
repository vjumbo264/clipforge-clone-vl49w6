# ClipForge Telegram Bot Architecture

## Purpose

This directory contains a **single shared Cloudflare Workers Telegram bot** that becomes a new operator interface for ClipForge. Each private Telegram chat can either connect an existing isolated ClipForge clone or create its own private Shadow Clone through the bot. The bot copies only shared source files from `motionssalt/clipforge`, explicitly excluding branding, jobs, audio-library files, and key/account/queue paths. It does not change `shadow-clone.js`, job identifiers, job directories, workflow inputs, `production.json`, or the Stage A/Stage B pipeline logic.

The existing HTML and JavaScript console remains in the repository as a safe transition fallback. It is not deleted by this implementation.

## Request path

| Component | Responsibility | Persistent data |
| --- | --- | --- |
| Telegram | Delivers user messages and inline-button callbacks by HTTPS webhook | Telegram’s own update stream |
| Cloudflare Worker | Validates webhook requests, resolves conversation state, encrypts credentials, calls GitHub, and replies in Telegram | No in-memory state is relied upon between requests |
| Cloudflare KV (`CLIPFORGE_BOT_KV`) | Stores encrypted credentials plus non-secret per-user conversation state and task labels | Per-chat records |
| GitHub user clone | Remains the authoritative task database and workflow host | `jobs/<job-id>/status.json`, releases, Actions secrets, branding preferences |
| GitHub Actions | Runs unchanged Stage A and Stage B workflows | Existing pipeline behavior |

## Security model

Telegram delivers updates to `POST /webhook`. The Worker accepts them only when `X-Telegram-Bot-Api-Secret-Token` equals the configured `TELEGRAM_WEBHOOK_SECRET`; all other webhook requests receive `401 Unauthorized`. The deployment setup registers this secret with Telegram’s `setWebhook` API. [1]

The Worker stores GitHub PATs and raw Gemini keys in a single per-chat credential record. Before the record is written to KV, it is encrypted with **AES-256-GCM** through the Workers Web Crypto API. A fresh random 96-bit IV is generated for every write, and authenticated additional data includes the chat id and schema version, preventing a ciphertext for one user from being substituted for another. Cloudflare KV’s encryption at rest is an additional platform protection rather than the only protection. [2] [3]

The `KV_ENCRYPTION_KEY` Worker secret must be a base64-encoded random 32-byte key. It is never committed, returned in a Telegram response, sent to GitHub, or logged. The Worker never logs inbound update bodies, GitHub Authorization headers, raw PATs, Gemini keys, encrypted payloads, or Telegram file URLs.

Raw Gemini keys exist only in request-local variables during a user’s settings flow and in the decrypted per-user credential record. To update `GEMINI_API_KEYS`, the Worker retrieves the repository Actions public key, creates a GitHub-compatible libsodium sealed-box ciphertext, and sends only that ciphertext to GitHub. The plain key is not committed to the clone; `branding/gemini_keys.json` continues to contain only masked fingerprints, matching the existing application contract. When an existing clone contains those fingerprints but the bot has no raw key copy, the fingerprints count as an existing site-managed Automatic Mode configuration. The Worker neither prompts for replacement nor overwrites the opaque Actions secret unless the operator explicitly starts and confirms a replacement flow.

Zernio follows the same sealed Actions-secret boundary. The raw API key exists only while the operator supplies it and while the Worker encrypts it for `ZERNIO_API_KEY`; it is then removed from the incoming private Telegram message and is never committed, displayed, retained in KV, or included in account snapshots. The Worker persists only the existing non-secret `branding/zernio_settings.json` schema and the workflow-created `branding/zernio_accounts.json` snapshot in the connected clone. Target toggles filter out disabled, inactive, and reconnect-required accounts before they can be sent to `zernio-publish.yml`.

An uploaded torrent is accepted only while the current chat is collecting a new Manual or Automatic source. The Worker requires a non-empty `.torrent` filename at or below 1 MB, downloads it as bounded raw bytes, checks the bencoded dictionary prefix, and writes it through the authenticated clone’s GitHub Contents API only to `jobs/<job-id>/source.torrent`. It never stores the torrent bytes in KV. Stage A’s established preflight validates the manifest, writes the authoritative `torrent-selection.json`, and changes the job state to `awaiting_torrent_selection`. Candidate-selection callbacks resolve the chat-local task label, re-read that manifest, validate the requested 1-based index, and dispatch Stage A using the existing `torrent_file_index` contract.

## Per-user state

KV keys are namespaced by Telegram chat id. The bot uses four record types:

| Key shape | Contents | Encryption |
| --- | --- | --- |
| `user:<chat-id>:credentials` | GitHub PAT, `owner/repo`, and the raw Gemini-key list required to rebuild the opaque GitHub secret | AES-256-GCM application encryption |
| `user:<chat-id>:state` | Conversation state, pending non-secret task fields, and current task context | Plain JSON; no credential material |
| `user:<chat-id>:tasks` | Compact mapping such as `A → manual-...` used in Telegram callbacks | Plain JSON; no credential material |
| `telegram:update:<update-id>` | Duplicate-delivery marker with a short TTL | Plain marker only |

Task labels are assigned in arrival order and remain scoped to one Telegram chat. A callback never trusts a label by itself: it resolves the label from that chat’s map before reading a job path or issuing a workflow dispatch.

## Command and callback model

Each incoming message reloads the state record before it is interpreted, which makes multi-message setup and task flows safe across independent Worker invocations.

| Command | Behavior |
| --- | --- |
| `/start` | For an unconfigured chat, offers **Create private Shadow Clone** or **Connect existing clone**; otherwise shows the user’s isolated operator menu without exposing credentials. |
| `/settings` | Lets the user switch or connect their own clone, shows safe existing Gemini metadata plus narrator, watermark, music defaults, and Zernio state; explicitly replaces Gemini keys when requested; selects and previews an Edge TTS narrator; saves a creator watermark; and exposes every existing Zernio preference. |
| `/tasks` | Lists the current user clone’s tasks as short labels, statuses, and inline buttons. |
| `/status` | Lists task labels, then lets the operator choose a task for the latest `status.json` and workflow link. |
| `/manual` | Collects a source URL, magnet URI, or bounded `.torrent` manifest, then optional focus, target duration, and optional music choice; writes a manual Stage A request and dispatches unchanged `stage-a.yml`. |
| `/automatic` | Collects the same source types, focus, target duration, and optional music choice; writes the compatible automatic music selection and dispatches unchanged `stage-a.yml` with `automatic_mode=true`. |
| `/done` | Shows completed tasks, provides the existing final video and ZIP release URLs, and exposes Zernio publish-now, manual scheduling, smart scheduling, retry, existing-post update/reschedule, and cancellation controls. |

Callback data stays short enough for Telegram by using small action prefixes and per-chat letter labels rather than raw job ids. Destructive or expensive actions have an explicit confirmation callback. The bot uses Telegram’s `answerCallbackQuery` before updating the message thread.

## Existing contracts reused

The implementation calls GitHub REST endpoints to dispatch the workflows with the exact workflow inputs already used by the web console:

- Stage A: `video_url`, `torrent_file_index`, `job_id`, `whisper_model`, `language`, `target_duration_seconds`, `focus`, and `automatic_mode`.
- Stage B: `job_id`, `production_ref`, `music_ref`, and the latest default-branch `code_ref`.
- Status: `jobs/<job-id>/status.json` remains authoritative, with the existing stage vocabulary from `scripts/write_status.py`.
- Manual handoff: the bot validates a submitted `production.json` against `schemas/production_plan_contract.json`, commits it to `jobs/<job-id>/production.json`, then dispatches `stage-b.yml`.
- Automatic music: an explicit empty selection, a safe library selection, or the same job’s `music.mp3` path is written in the existing `automatic_music.json` format expected by `scripts/read_automatic_music.py`.
- Torrent uploads: the manifest path is exactly `jobs/<job-id>/source.torrent`, Stage A first runs its persistent torrent-selection substage with blank `torrent_file_index`, and the bot resumes only by dispatching a candidate index present in `jobs/<job-id>/torrent-selection.json`.
- Zernio settings: the Worker writes the established `branding/zernio_settings.json` document, supports `enabled`, `auto_publish`, `automatic_mode`, TikTok/YouTube account targets, IANA timezone, cadence, preferred time, queue depth, and next-available or custom first smart-schedule slot. Account discovery and all completed-job actions dispatch the unchanged `zernio-publish.yml` contract (`discover`, `publish`, `retry`, `update`, and `cancel`).

## Operational boundaries

The Worker receives Telegram commands through a webhook and performs on-demand GitHub status reads. A private Shadow Clone is created under the authenticated user’s GitHub account and is bound only to the initiating Telegram chat. It does not use the sandbox as a background service and does not alter completed job folders. Automatic Mode continues to use its current direct-Gemini execution path; the removed FreeLLMAPI evaluation is not reintroduced.

## References

[1]: https://core.telegram.org/bots/api#setwebhook "Telegram Bot API: setWebhook"
[2]: https://developers.cloudflare.com/workers/runtime-apis/web-crypto/ "Cloudflare Workers Web Crypto API"
[3]: https://developers.cloudflare.com/kv/reference/data-security/ "Cloudflare KV data security"
