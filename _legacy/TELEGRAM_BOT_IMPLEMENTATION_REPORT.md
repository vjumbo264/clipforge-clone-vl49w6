# ClipForge Telegram Bot Implementation Report

**Author:** Manus AI  
**Scope:** A new Cloudflare Workers Telegram operator interface for ClipForge.  
**Implementation status:** The shared bot and the legacy-settings/torrent-routing update are published and deployed.

## Outcome

A self-contained Cloudflare Worker project now lives at [`telegram-bot/`](telegram-bot/). One shared Telegram bot receives webhook updates, stores persistent state in Workers KV, application-encrypts per-user credentials, and lets each private chat either connect an existing ClipForge clone or create its own private Shadow Clone. That clone remains the source of truth for the user’s tasks, status, workflow dispatches, and releases.

| Capability | Implemented behavior |
| --- | --- |
| Private-chat control plane | The bot accepts only direct/private Telegram chats, preventing a group chat from sharing an operator’s repository controls. |
| Repository setup | An unconfigured `/start` offers **Create private Shadow Clone** or **Connect existing clone**. The first path creates a private repository in the authenticated user’s account, copies only shared ClipForge source files, and excludes jobs, branding, audio-library content, and key/account/queue paths. The second validates an existing `owner/repository`. Both paths encrypt the per-chat credential record before KV storage. |
| Gemini setup and migration | The bot recognises existing masked fingerprints in `branding/gemini_keys.json` as an already configured opaque `GEMINI_API_KEYS` Actions secret. It does not reveal or silently replace the existing secret. A deliberately confirmed replacement uploads a new encrypted key set through GitHub’s Actions public-key and sealed-box protocol, while the repository retains only masked fingerprints. |
| Existing user isolation | Task labels and state are namespace-scoped by Telegram chat id; each chat operates only the repository configured for that chat. A newly created Shadow Clone is private, belongs to the authenticated user, and contains no other user’s job, branding, media-library, credential, or queue state. |
| Task operations | `/manual`, `/automatic`, `/tasks`, `/status`, and `/done` use existing GitHub status files, workflow inputs, releases, and final asset URLs. Manual and Automatic source collection accepts public URLs, magnets, or a bounded `.torrent` manifest. |
| Manual handoff | Production plans are validated against the existing `production_plan_contract.json` rules before `jobs/<id>/production.json` is committed and Stage B is dispatched. |
| Workflow controls | Status views provide refresh, Stage A retry, Stage B retry, production-plan upload, confirmed Stage B cancellation, and persistent torrent-video selection controls when the current status permits them. |
| Deployment documentation | [`TELEGRAM_BOT_SETUP.md`](TELEGRAM_BOT_SETUP.md) explains Cloudflare Workers Git deployment, KV binding, secrets, BotFather, webhook registration, testing, and recovery. |

## Files read before implementation

The bot deliberately reuses existing behavior rather than inventing field names or status vocabulary. I reviewed the following authoritative files.

| File | Reused contract |
| --- | --- |
| [`app.js`](app.js) | GitHub REST behavior, Stage A and Stage B dispatch inputs, job IDs, status polling, production upload, Edge TTS settings, watermark format, masked Gemini metadata, music-default behavior, and task lifecycle. |
| [`automatic.html`](automatic.html) | Automatic Mode’s source, focus, duration, whisper, language, music, Gemini, torrent-selection, release, and Stage B operator expectations. |
| [`shadow-clone.js`](shadow-clone.js) | The existing source/clone separation, excluded paths, and user-clone PAT contract. The file is untouched. |
| [`.github/workflows/stage-a.yml`](.github/workflows/stage-a.yml) | Exact Stage A `workflow_dispatch` input names, Automatic Mode flag, run-name/job-id behavior, and persisted status/release lifecycle. |
| [`.github/workflows/stage-b.yml`](.github/workflows/stage-b.yml) | Exact Stage B inputs, current-code `code_ref` behavior, status lifecycle, output artifacts, and Edge TTS production contract. |
| [`scripts/write_status.py`](scripts/write_status.py) | The complete valid stage vocabulary and `status.json` field structure. |
| [`scripts/read_automatic_music.py`](scripts/read_automatic_music.py) | Automatic Mode’s safe music-selection document and allowed music references. |
| [`schemas/production_plan_contract.json`](schemas/production_plan_contract.json) | The existing manual `production.json` validation contract. |

## Credential storage and encryption

The Worker uses the following layered approach.

> GitHub PATs and raw Gemini keys are never committed to the repository, never placed in `wrangler.jsonc`, never returned in bot responses, and are not logged by the Worker.

| Secret or state | Storage location | Protection |
| --- | --- | --- |
| Worker Telegram bot token | Cloudflare Worker secret `TELEGRAM_BOT_TOKEN` | Cloudflare secret binding; never in repository code. |
| Webhook verification value | Cloudflare Worker secret `TELEGRAM_WEBHOOK_SECRET` | Required for every `POST /webhook` request via Telegram’s secret header. |
| KV record encryption key | Cloudflare Worker secret `KV_ENCRYPTION_KEY` | Base64 32-byte key used for AES-256-GCM. |
| GitHub PAT and Gemini-key list | Per-chat KV credential record | AES-256-GCM with a fresh 96-bit nonce and chat-id-bound authenticated additional data, on top of Cloudflare KV encryption at rest. |
| GitHub `GEMINI_API_KEYS` update | GitHub repository Actions secret | The Worker retrieves GitHub’s Actions public key and performs a libsodium-compatible sealed-box encryption before upload. |
| `branding/gemini_keys.json` | User’s clone repository | Only masked key fingerprints and timestamps; no raw key material. |

The bot attempts to delete a successful inbound PAT or Gemini-key message from the direct chat as an additional best-effort protection. The operator should still delete the message manually if it remains visible.

## Existing website files

The existing HTML/JavaScript site files were **kept**. This follows the requested transition-safe approach: `task.html`, `automatic.html`, `settings.html`, `app.js`, `shadow-clone.js`, and all current pipeline artifacts remain available as a fallback while the Telegram interface is deployed and tested. The bot is a new controller, not a destructive website removal.

## Setup guide

The final operator-facing deployment guide is [`TELEGRAM_BOT_SETUP.md`](TELEGRAM_BOT_SETUP.md). It documents the exact Worker project root, KV binding, three Worker secrets, BotFather configuration, webhook registration command, first private-chat setup, required GitHub access, validation commands, and recovery steps.

## Intentional constraints and deviations

| Item | Decision and reason |
| --- | --- |
| Existing static console | Retained instead of deleted, as explicitly permitted and safer for transition. |
| Bot access scope | Restricted to private chats; this is a security hardening measure for credential and clone isolation. |
| Torrent manifest upload | The bot accepts a non-empty `.torrent` document up to 1 MB only while a new Manual or Automatic source is expected. It uploads raw bytes only to `jobs/<job-id>/source.torrent`, starts the existing Stage A selection substage with no file index, reads `torrent-selection.json`, and dispatches a user-selected, workflow-validated 1-based index. It does not parse torrent metadata or download media in the Worker. |
| Automatic Mode backend | Remains the current direct-Gemini Action path. The earlier FreeLLMAPI experiment remains removed and is not referenced by this bot. |
| Cloudflare deployment | The update was deployed to the existing shared Worker without changing its KV binding or secret bindings. The public health endpoint returned `ok`, and an unauthenticated webhook request remained rejected with HTTP `401`. |

## Validation

| Validation | Result |
| --- | --- |
| Worker unit tests | **14 passed.** Tests cover AES-GCM encryption/chat binding, sealed-box compatibility, task-label isolation, conversation isolation, webhook-secret enforcement, existing masked-Gemini recognition, torrent-selection controls, bounded raw torrent downloads, isolated GitHub binary uploads, source-command validation, and production-plan validation. |
| Worker bundle | **Passed.** `wrangler deploy --dry-run` bundled the project and recognized the `CLIPFORGE_BOT_KV` binding without publishing it. |
| Automatic Mode regression | **Passed.** Existing regression confirms chronological evidence retrieval, correction handling, bounded temporary-failure retry, key failover, and fallback behavior. |
| Automatic workflow contract | **Passed.** Existing regression confirms Automatic Mode is opt-in, direct-Gemini-native, bounded, validated, status-visible, and dispatches only the current Stage B path. |
| Music and production-plan contracts | **Passed.** Existing music-default and production-plan import regression suites both passed. |
| Edge TTS voiceover configuration test | **Passed after configuration-test alignment.** The test now verifies the active 10-voice Edge TTS catalog, persisted narrator settings, 24 kHz mono PCM output contract, and existing speech-clarity mastering targets. |

## Publication status

The legacy-settings and torrent-routing update was published to `motionssalt/clipforge` at commit `1955a0b41453134c1070e630041103c8a31df709` and deployed to the existing shared Worker. The published code includes the settings migration guard, raw-byte bounded torrent intake, persistent Stage A video selection, documentation, and regression coverage. The deployment preserved the existing KV binding and Worker secret bindings. Post-deployment validation returned `ok` from `/health`, while a `POST /webhook` without Telegram’s secret header returned HTTP `401`.

## References

[1]: https://core.telegram.org/bots/api#setwebhook "Telegram Bot API: setWebhook"
[2]: https://developers.cloudflare.com/workers/runtime-apis/web-crypto/ "Cloudflare Workers Web Crypto API"
[3]: https://developers.cloudflare.com/kv/reference/data-security/ "Cloudflare KV data security"
[4]: https://developers.cloudflare.com/workers/ci-cd/builds/git-integration/ "Cloudflare Workers Git integration"
[5]: https://developers.cloudflare.com/kv/get-started/ "Cloudflare Workers KV"
[6]: https://developers.cloudflare.com/workers/configuration/secrets/ "Cloudflare Workers secrets"
