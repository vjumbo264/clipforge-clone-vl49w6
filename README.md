# ClipForge

ClipForge is a **Telegram bot that turns a source video into a short, narrated,
captioned vertical clip.** You talk to one shared bot; the bot drives a private
GitHub repository (your "Shadow Clone") that actually runs the video pipeline
in GitHub Actions. Manual mode and Series mode are
supported — see [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

This repository is the **central ClipForge source of truth**. It is also a
multi-session rebuild coordinated through committed files:

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the design contract every session
  builds against.
- **[BUILD_PROGRESS.json](BUILD_PROGRESS.json)** — the resumable build
  checkpoint.
- **[FIX_STATE.json](FIX_STATE.json)** — the resumable bug-sweep state.
- **[NEXT_SESSION_PROMPT.md](NEXT_SESSION_PROMPT.md)** — the reusable prompt
  handed to every future building session. Never commit filled-in credentials.

The previous implementation is preserved under **[`_legacy/`](_legacy/)** in its
original layout for reference and comparison.

---

## Using ClipForge (Shadow Clone setup)

You never deploy the bot yourself. ClipForge runs as **one shared Bot A** that
serves every user; your private Telegram chat is bound to **your own private
GitHub clone** of this repo, and that clone is where your videos are processed.
Setting that up is done entirely inside the bot.

### 1. Create or connect a clone (in the bot)

Open the bot and send `/start`, then either:

- **Create private Shadow Clone** — the bot creates a brand-new private
  repository under your GitHub account for you.
  1. Send a repository name (letters, numbers, dots, hyphens, underscores), or
     tap **✨ Choose a name for me**.
  2. Send a **GitHub personal access token** with `repo` and `workflow` scopes
     (a classic PAT needs `repo`; a fine-grained PAT needs *Administration*
     write so it can create the repository). Your token message is deleted
     immediately and the token is stored encrypted.
  3. The bot creates the repo, bootstraps it, and dispatches a one-time
     GitHub Actions **copy workflow** inside your new repo that fills in all
     the shared source files. This takes a few minutes — runner startup alone
     is ~30–60s. You do not need to keep the chat open: the bot watches the
     copy and messages you the moment it **finishes** or **fails** (it always
     ends in one or the other — never silence).
  4. On success you are connected automatically and land on the home screen.

- **Connect existing clone** — if you already have a clone repo, send a PAT
  (same scopes) and then the repository as `owner/repository`.

### 2. Add the secrets your clone needs to run

Your clone runs GitHub Actions, and some workflows need secrets. Add them in
your clone under **Settings → Secrets and variables → Actions → New repository
secret**.

**Required for the clone to deploy and clean up:**

| Secret | Why the clone needs it | Used by |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | Cloudflare API token with Workers edit rights. Without it the **Deploy Bots** workflow fails immediately (`it's necessary to set a CLOUDFLARE_API_TOKEN environment variable`). | `deploy-bots.yml`, `cleanup.yml` |
| `CLOUDFLARE_ACCOUNT_ID` | Your Cloudflare account id. | `deploy-bots.yml`, `cleanup.yml` |

> **Fresh-clone note:** the very first **Deploy Bots** run on a brand-new clone
> will fail until these two secrets exist — that is expected. The shared bot
> detects the failed run and DMs you a pointer to it (you'll see this the next
> time your home screen opens). Add the two secrets, then re-run the failed
> workflow from the Actions tab.

**Optional — only needed for specific features:**

| Secret | Feature it enables | Used by |
|---|---|---|
| `ZERNIO_API_KEY` | Social publishing via Zernio. Absent → publish steps skip/fail only when you actually trigger them. | `stage-b.yml`, `publish.yml` |

**Not needed on a clone (central-only):** `BOTB_MTPROTO_*`,
`CLIPFORGE_TELEGRAM_*`, `DEPLOY_ALERT_CHAT_ID`, and `RELAY_ENCRYPTION_KEY` live only on the central
repo/Worker. A clone never receives them and must not — see the next section.

### 3. Sending a video directly (the private relay)

You can send or forward a video **straight into the bot chat** as a source —
no link needed. This uses the **central Bot A → private relay group → Bot B →
GitHub Actions** relay, and it **works for clone owners with no extra setup**:
your clone does not need its own relay bots, relay group, or encryption key.
The central relay downloads your video and hands it to *your* clone's Stage A
as a temporary, checksummed release asset. (One caveat: a public `t.me`
*channel-post* link download is a separate, original-repo-only feature — see
ARCHITECTURE.md §9.1. Direct video forwarding is available to everyone.)

---

## Bot command reference

| Command | What it does |
|---|---|
| `/start` | Home menu for the connected clone |
| `/help` | In-app command reference |
| `/new` | Start the new-video wizard — manual or series mode |
| `/tasks` | Active task list; finished and errored tasks remain visible with their terminal status |
| `/done` | Completed tasks |
| `/settings` | Clone settings: narrator voice, watermark, music library, Zernio |
| `/cancel` | Cancel the current setup or input flow |

Inside `/new`, accepted sources: a directly forwarded/uploaded video, a direct
`https://` video URL, a Google Drive share link, a magnet URI, a `.torrent`
file (≤ 1 MB), or — on the original repo only — a public `t.me` channel-post
link.

Legacy commands removed in the rebuild: `/status` is folded into `/tasks`.

---

## For the operator / human running build sessions

Feed **`NEXT_SESSION_PROMPT.md`** unchanged to every future build session,
after filling in the credential placeholders at its top for that run only.
For bug-fix sessions, follow **`FIX_STATE.json`** at the repo root — it is the
source of truth for which fixes are done and which are pending.
