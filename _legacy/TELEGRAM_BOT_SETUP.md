# ClipForge Telegram Bot Setup

This guide deploys the new Telegram operator interface found in [`telegram-bot/`](telegram-bot/). It leaves the existing ClipForge HTML/JavaScript console in place as a fallback while the bot is adopted.

> **What this deploys:** one shared Cloudflare Worker bot that receives Telegram webhook updates and stores each private chat’s encrypted configuration in Workers KV. On first use, each person can create their own private Shadow Clone under their GitHub account or connect an existing ClipForge clone. The selected clone becomes that chat’s isolated task database and GitHub Actions host. The bot does **not** replace `shadow-clone.js`, `stage-a.yml`, `stage-b.yml`, or the media pipeline.

## 1. Prerequisites

You need a Cloudflare account, a GitHub account, and a Telegram account. You do **not** need to create another Telegram bot for each person. The one deployed bot prompts every new private chat to either create a private Shadow Clone in that person’s GitHub account or connect an existing clone. The bot copies shared ClipForge source only and excludes jobs, branding, audio-library content, keys, accounts, and queue data from a new clone.

| Item | Why it is needed | Where it is stored |
| --- | --- | --- |
| Telegram bot token | Lets the Worker receive and reply to Telegram updates | Cloudflare Worker secret only |
| Telegram webhook secret | Proves incoming webhook requests came from Telegram | Cloudflare Worker secret only |
| 32-byte KV encryption key | Application-encrypts each user’s GitHub PAT and Gemini-key list before KV storage | Cloudflare Worker secret only |
| GitHub personal access token | Lets the bot create or connect the user’s clone, read status, write compatible files, dispatch workflows, and manage the Actions Gemini secret | Encrypted per private Telegram chat in KV |
| Gemini key(s) | Powers the existing Automatic Mode in GitHub Actions | Encrypted in KV and uploaded as the GitHub Actions secret `GEMINI_API_KEYS` |

Cloudflare supports a GitHub-connected Worker that redeploys after repository pushes. [1] Cloudflare also recommends storing sensitive values as Worker secrets rather than plaintext variables. [2]

## 2. Create the Telegram bot

1. In Telegram, open [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, choose a display name such as `ClipForge Operator`, then choose a unique username ending in `bot`.
3. Copy the token BotFather returns. Treat it as a password: do not commit it, paste it into GitHub, or send it in a group chat.
4. In BotFather, optionally use `/setcommands` and paste the following command list:

   ```text
   start - Open the ClipForge menu
   settings - Configure GitHub, Gemini, narrator, and watermark
   tasks - List current tasks
   status - Inspect a task and control it
   manual - Start a manual task
   automatic - Start an Automatic Mode task
   done - Show completed downloads
   cancel - Cancel the current bot input flow
   ```

The Worker accepts only private Telegram chats. Do not put the bot in a group for operator access.

## 3. Create the Workers KV namespace

1. In Cloudflare, open **Workers & Pages** → **KV** → **Create instance**.
2. Name the namespace `clipforge_telegram_bot_state`.
3. Open [`telegram-bot/wrangler.jsonc`](telegram-bot/wrangler.jsonc) in your clone.
4. Replace only `REPLACE_WITH_YOUR_KV_NAMESPACE_ID` with the new namespace ID. Keep the binding name exactly as `CLIPFORGE_BOT_KV`.
5. Commit and push that non-secret configuration change to your clone.

The Worker reads this binding as `env.CLIPFORGE_BOT_KV`; the exact KV namespace name is your choice, but the binding name is part of the code contract. [3]

## 4. Connect the clone repository to a Cloudflare Worker

1. In the Cloudflare dashboard, open **Workers & Pages** and create or select a Worker named `clipforge-telegram-bot`.
2. In the Worker’s **Settings** → **Builds** section, connect the GitHub repository containing this code. Cloudflare will prompt you to install or authorize its GitHub integration if needed. [1]
3. Set the project’s root/build directory to `telegram-bot`.
4. Configure the build command as:

   ```text
   pnpm install --frozen-lockfile
   ```

5. Configure the deploy command as:

   ```text
   pnpm exec wrangler deploy
   ```

6. Save the build configuration and trigger the first deployment. After deployment, copy the Worker URL, typically:

   ```text
   https://clipforge-telegram-bot.<your-workers-subdomain>.workers.dev
   ```

The Worker already has a non-sensitive health endpoint at `/health`. Before configuring Telegram, opening `https://…/health` should return `ok`.

## 5. Set the required Worker secrets

Open **Workers & Pages** → `clipforge-telegram-bot` → **Settings** → **Variables and Secrets** → **Add**. Create all three as **Secret** values, not plaintext variables. [2]

| Secret name | Value | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | The token from BotFather | Authenticates requests to the Telegram Bot API |
| `TELEGRAM_WEBHOOK_SECRET` | A new random URL-safe secret | The Worker checks Telegram’s webhook header against it |
| `KV_ENCRYPTION_KEY` | A new base64-encoded random 32-byte key | AES-256-GCM encryption key for per-chat credentials in KV |

Generate the two random values locally. Keep the terminal private and do not save the output in the repository:

```bash
# Webhook secret: URL-safe, high-entropy value
openssl rand -base64 32 | tr '+/' '-_' | tr -d '='

# KV encryption key: base64 encoding of exactly 32 random bytes
openssl rand -base64 32
```

Select **Deploy** after saving the secrets. Cloudflare keeps deployed secret values hidden in its dashboard and command-line tooling after they are set. [2]

> **Do not rotate `KV_ENCRYPTION_KEY` casually.** It encrypts every saved user credential record. Rotating it without a migration will intentionally make old records undecryptable, requiring each operator to reconnect GitHub and re-enter Gemini keys.

## 6. Register the Telegram webhook

Use a private terminal session. Replace `WORKER_URL` with the deployed Worker URL; do not add a trailing slash.

```bash
export TELEGRAM_BOT_TOKEN='paste-the-BotFather-token-here'
export TELEGRAM_WEBHOOK_SECRET='paste-the-matching-Cloudflare-secret-here'
export WORKER_URL='https://clipforge-telegram-bot.<your-workers-subdomain>.workers.dev'

curl --silent --show-error --fail \
  --request POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${WORKER_URL}/webhook" \
  --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["message","callback_query"]'
```

Telegram sends HTTPS POST updates to the registered URL and, when configured, includes the webhook secret in `X-Telegram-Bot-Api-Secret-Token`. [4] The Worker rejects requests without that exact header.

To verify the registration without exposing the token in a browser address bar, run:

```bash
curl --silent --show-error --fail \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
```

The result should show the `/webhook` URL and no persistent `last_error_message`.

## 7. First private-chat setup

1. Open the bot’s direct message thread in Telegram and send `/start`.
2. Select **Settings** → **GitHub connection**.
3. Send a GitHub PAT, then send your clone as `owner/repository`.
4. Open **Settings** to review the current Clone’s narrator, watermark, music default, and Automatic Mode key status. If the Clone was previously configured through the ClipForge site, the bot reads the non-secret `branding/gemini_keys.json` fingerprints and recognises the existing opaque `GEMINI_API_KEYS` Actions secret without attempting to reveal, duplicate, or overwrite it.
5. Select **Settings** → **Gemini API key** only when you deliberately want to replace the existing Actions secret with a new key set. The replacement confirmation is explicit because GitHub Actions secrets cannot be read back.
6. Select **Settings** → **Narrator**. The bot offers all ten Edge TTS narrators, a **Preview** button for each committed MP3 sample, and a **Use** button that saves the selected narrator for future Stage B work.
7. Optionally set the creator watermark.
8. Select **Settings** → **Zernio publishing** to operate the clone’s complete publishing configuration in Telegram. Save or replace the Zernio API key, refresh connected TikTok and YouTube accounts, select active target accounts, enable or disable publishing controls, enable or disable automatic Stage B publication, choose immediate or smart-scheduled automatic publication, and configure timezone, cadence, preferred local time, queue depth, and either the next available or a custom first schedule slot. The bot writes the same `branding/zernio_settings.json` schema used by the website and never commits or displays `ZERNIO_API_KEY`.

Use a PAT that can create a private repository in the user’s own GitHub account, read and write the clone’s contents, manage the `GEMINI_API_KEYS` GitHub Actions secret, dispatch and cancel Actions workflows, and access workflow files. A classic token should use `repo` plus `workflow`; for a fine-grained token, grant the equivalent repository **Contents**, **Actions**, and **Workflows** write capabilities and ensure it can create the selected personal repository. Existing-clone connection does not need repository-creation permission.

The bot best-effort deletes successful PAT, Gemini-key, and Zernio-key messages from the private chat. Delete the local message yourself as well if it remains visible. Never send credentials in a group.

## 8. Operator workflow

| Telegram action | Existing ClipForge contract reused |
| --- | --- |
| `/manual` | Collects a public source URL, magnet URI, or an uploaded non-empty `.torrent` manifest up to 1 MB, then optional focus, duration, and music. It writes `stage-a-request.json` and dispatches `stage-a.yml` with `automatic_mode=false`. At **Awaiting production plan**, **Get agent prompt** supplies a concise, copyable instruction containing the exact GitHub Release URL; the external agent reads `00_READ_THIS_FIRST.txt` and the remaining release assets there before returning `production.json`. |
| `/automatic` | Collects the same URL, magnet, or `.torrent` source inputs, focus, duration, and music; writes the compatible `automatic_music.json` selection when needed; dispatches `stage-a.yml` with `automatic_mode=true`. Existing masked Gemini-key metadata counts as configured Automatic Mode credentials unless the operator explicitly replaces the secret. |
| `/tasks` / `/status` | Reads the authoritative `jobs/<job-id>/status.json`; labels tasks locally as `A`, `B`, and so on for short inline buttons. Manual Stage A tasks also expose the exact agent-prompt text file when ready. When a torrent manifest reaches `awaiting_torrent_selection`, **Choose torrent video** reads the workflow-generated candidate manifest and starts Stage A only for the selected 1-based video entry. |
| Upload production plan | Validates the existing production-plan contract, writes `jobs/<job-id>/production.json`, then dispatches unchanged `stage-b.yml` |
| Retry/cancel controls | Reuse saved Stage A input documents, current default-branch `code_ref`, and the workflow run ID saved in `status.json` |
| `/done` | Sends the final MP4 and ZIP URLs recorded in the task status document and exposes **Zernio publishing** for each completed task. The Telegram flow supports publish now, manual local-date scheduling, smart scheduling, failed/partial post retry, existing-post publish-now update, rescheduling, and cancellation through the unchanged `zernio-publish.yml` workflow. |

## 9. Updating the bot

Push changes under `telegram-bot/` to the connected repository. The Cloudflare Git integration rebuilds and redeploys the Worker automatically. [1] Secrets and the KV namespace are independent of code pushes and should remain configured.

A Telegram `.torrent` document is downloaded as raw bytes only after the bot verifies the `.torrent` filename, non-empty size, 1 MB limit, and bencoded dictionary prefix. It is written only to `jobs/<job-id>/source.torrent`; the unchanged Stage A preflight validates the manifest and persists the authoritative candidate list before any selected video is started.

Before a production change, run the local checks from the bot directory:

```bash
cd telegram-bot
pnpm install --frozen-lockfile
pnpm test
pnpm exec wrangler deploy --dry-run
```

## 10. Recovery and security checklist

If the bot reports an authentication or permission failure, reconnect the GitHub token from `/settings` after correcting its repository permissions. If Telegram reports webhook errors, first confirm `/health` responds and then run `getWebhookInfo` again.

If a credential was pasted in the wrong chat, exposed in an image, or committed accidentally, revoke or rotate it at the upstream provider immediately. The Worker’s encrypted KV records do not remove the need for upstream key rotation after a disclosure.

## References

[1]: https://developers.cloudflare.com/workers/ci-cd/builds/git-integration/ "Cloudflare Workers Git integration"
[2]: https://developers.cloudflare.com/workers/configuration/secrets/ "Cloudflare Workers secrets"
[3]: https://developers.cloudflare.com/kv/get-started/ "Cloudflare Workers KV getting started"
[4]: https://core.telegram.org/bots/api#setwebhook "Telegram Bot API: setWebhook"
