# ClipForge

ClipForge is an automated video-production pipeline built around GitHub Actions, Python media-processing scripts, and Telegram bot workflows. It stages approved source media, prepares production artifacts, renders finished videos, and maintains task state inside the repository and GitHub Releases.

## Operation

The supported entry points are the Telegram bots and GitHub Actions workflows. Telegram bot users configure and start jobs through the bot interface; the bot creates task metadata and dispatches the relevant pipeline workflow. The workflows run the processing stages and publish task artifacts to the corresponding GitHub Release.

| Component | Responsibility |
| --- | --- |
| `telegram-bot/` | Telegram bot interfaces, encrypted task credentials, and workflow-dispatch logic. |
| `.github/workflows/` | Stage execution, private Telegram relay, Worker deployment, and repository automation. |
| `scripts/` | Source handling, transcription, subtitle alignment, rendering, and media utilities. |
| `jobs/` | Per-task request and status metadata created by the active automation. |

## Pipeline

Stage A ingests a supported source, produces its analysis artifacts, and stages the approved input in a GitHub Release. Stage B validates the production plan, creates voiceover and subtitles, renders the final video, and updates the task artifacts. Cleanup automation removes expired job data and temporary releases according to the configured retention policy.

## Development checks

Run focused checks after changing pipeline code:

```bash
python3 scripts/test_subtitle_alignment.py
python3 scripts/test_telegram_relay.py
node --test telegram-bot/test/core.test.js
```

Changes beneath `telegram-bot/` automatically deploy the two Telegram Workers through the repository deployment workflow.

## Security

Use repository and Worker secrets for credentials. Do not commit personal access tokens, Telegram bot tokens, API keys, encrypted task payloads, or private release URLs.
