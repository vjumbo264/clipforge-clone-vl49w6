# Private Telegram Relay Deployment Checklist

The direct-forward feature uses two shared bots and a temporary trusted workflow. **Do not put any raw credential in this repository, a workflow input, or a Shadow Clone.**

## BotFather and group prerequisites

Both Bot A and Bot B must be members of the configured private **basic Telegram group** (a negative ID such as `-5405387856`, not a `-100…` supergroup/channel ID). Make both administrators where the group permits it. Enable **Bot-to-Bot Communication Mode** for at least Bot B, and disable Bot B’s Group Privacy Mode. Confirm that each bot’s `getChat` call succeeds for the configured group before production use; a `chat not found` response means that bot has not been added. The relay requests the single authenticated media message directly and deliberately never enumerates dialogs, which Telegram disallows for bot accounts.

## Bot A Worker bindings

Existing Bot A bindings remain unchanged. Add these non-secret variables and preserve existing secrets.

| Binding | Kind | Purpose |
|---|---|---|
| `INTERNAL_RELAY_GROUP_CHAT_ID` | variable | Basic private group chat ID shared by Bot A and Bot B; do not use a `-100…` supergroup/channel ID. |
| `ORIGINAL_CLIPFORGE_REPOSITORY` | variable | The original repository permitted to use the existing authenticated public-channel MTProto path. |

## Bot B Worker bindings

Deploy `wrangler.bot-b.jsonc` as a separate Worker sharing the existing `CLIPFORGE_BOT_KV` namespace. Set the following as Worker secrets, except explicitly marked variables.

| Binding | Kind | Purpose |
|---|---|---|
| `BOT_B_TELEGRAM_WEBHOOK_SECRET` | secret | Validates Telegram’s webhook to Bot B. |
| `RELAY_ENCRYPTION_KEY` | secret | Not required by Bot B. Bot A uses this key to seal a per-job payload; Bot B forwards only that opaque ciphertext. |
| `RELAY_GITHUB_TOKEN` | secret | Central repository token used only to dispatch `telegram-relay.yml`. |
| `RELAY_GITHUB_REPOSITORY` | variable | Trusted central repository holding `telegram-relay.yml`. |
| `INTERNAL_RELAY_GROUP_CHAT_ID` | variable | The same basic private group chat ID; do not use a `-100…` supergroup/channel ID. |
| `BOT_A_TELEGRAM_ID` | variable | Numeric Bot A user ID; lets Bot B ignore all non-Bot-A group traffic. |

## Central relay workflow secrets

Set these only in the trusted central repository. The relay encryption key is also set on Bot A so it can seal each job before Bot B sees it. None of these secrets is copied to a Shadow Clone.

| Secret | Purpose |
|---|---|
| `RELAY_ENCRYPTION_KEY` | Decrypts Bot A’s sealed per-job routing envelope. |
| `BOTB_MTPROTO_API_ID` | Bot B’s dedicated Telegram application ID. |
| `BOTB_MTPROTO_API_HASH` | Bot B’s dedicated Telegram application hash. |
| `BOTB_MTPROTO_BOT_TOKEN` | Authorizes the temporary Bot B MTProto session. |

## Webhook setup

Set Bot B’s webhook to the deployed Bot B Worker’s `/webhook` endpoint and supply the matching webhook secret. Confirm both `/health` endpoints return `ok` before testing media.

## Mandatory test sequence

First prove a small direct video reaches Bot A, is copied to the private group, triggers Bot B, and starts target Stage A. Then prove a large source, followed by two near-simultaneous tasks sent from separate clone records. Confirm no Bot B credential exists in either clone’s Actions secrets, repository files, workflow inputs, or logs. Keep the original repository’s public-channel link path enabled throughout these proofs; Shadow Clones receive no corresponding session secret and the shared Worker rejects their public Telegram channel links.
