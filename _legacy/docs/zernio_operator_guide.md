# ClipForge Zernio Publishing Operator Guide

## Purpose and boundary

ClipForge’s **Stage B remains the media-production authority**. It validates, releases, and marks the final cut complete before optional distribution is considered. Zernio publishing is a separate workflow and persists only a `publishing` sibling within the job’s `status.json`; no platform outage can change a completed Stage B result.

> **Operational rule:** a finished video is available from its ClipForge Release whether Zernio is unconfigured, disabled, delayed, or reports a provider-side failure.

## Initial configuration

Open **Repository settings → Zernio publishing** in the ClipForge console. Enter the Zernio key and save it. The browser encrypts the value using the repository’s GitHub Actions public key before writing the `ZERNIO_API_KEY` Actions secret. The raw key is never committed to this repository, returned from GitHub, or stored in a job artifact.

Next, refresh connected accounts. The `zernio-publish.yml` discovery run calls Zernio server-side and saves a non-secret TikTok/YouTube snapshot to `branding/zernio_accounts.json`. Select only the active accounts intended for posting, then save the settings. Account selections and scheduling preferences persist at `branding/zernio_settings.json`.

| Setting | Meaning | Guardrail |
| --- | --- | --- |
| **Enable publishing controls** | Displays per-job publishing controls after Stage B completes. | Does not publish anything by itself. |
| **Automatically submit eligible outputs** | Dispatches a separate Zernio workflow after a successful Stage B completion. | Best-effort only; Stage B remains complete even if this dispatch fails. |
| **Automatic mode** | Chooses immediate publish or ClipForge smart scheduling. | The Zernio workflow, not Stage B, contacts the provider. |
| **IANA timezone** | Defines local scheduling semantics, for example `Europe/London`. | Validated again server-side through Python `zoneinfo`. |
| **Cadence and preferred time** | Defines the local recurring schedule for smart-scheduled videos. | The durable queue prevents concurrent jobs from using the same planned slot. |
| **Maximum queue depth** | Displays the intended operating limit for the queue. | Review queue depth before enabling automatic schedule additions. |

## Publishing a completed job

After Stage B reaches **complete**, open the task and use its **Publish with Zernio** panel. The panel offers the two direct operator choices requested for completed output:

| Mode | Action | Result |
| --- | --- | --- |
| **Publish now** | Submit the final MP4 immediately. | Zernio receives a `publishNow: true` post request. |
| **Choose date and time** | Enter a local datetime. | ClipForge submits `scheduledFor` with the selected IANA timezone. |
| **Add to smart schedule** | Use the saved cadence, preferred time, and queue state. | ClipForge calculates the next local collision-free time and creates a native Zernio scheduled post. |

Each logical publish attempt carries a durable idempotency key. Zernio treats a same-key retry within its documented idempotency window as the original request; separately, Zernio’s content-hash safeguard returns the original post ID for duplicate content within 24 hours. The workflow records the existing post rather than deliberately creating another platform post. [1]

## Scheduling and queue model

ClipForge owns the requested **interval / preferred-local-time / custom-start / depth** policy because it is an application-specific schedule. It serializes the publishing workflow globally, records active ClipForge schedule entries in `branding/zernio_queue.json`, and also reads Zernio’s currently scheduled posts before assigning a smart-schedule slot. Scheduling arithmetic is done in the selected IANA timezone rather than by adding UTC days.

The resulting post is still a **native Zernio scheduled post**. Zernio receives the local `scheduledFor` time plus `timezone` and performs the eventual publication, so no GitHub runner waits for the scheduled time. Zernio also supports its own profile queues through `queuedFromProfile`, but its documentation specifically warns that `GET /v1/queue/next-slot` is preview-only and must not be copied into `scheduledFor`; ClipForge does not use that unsafe pattern. [1]

## Status, retry, reschedule, and cancellation

The status panel retains Stage B’s top-level `complete` stage and adds an independent `publishing` object. It records provider status, mode, schedule, timezone, masked/safe metadata provenance, post IDs, selected platform results, and public post links once provided by Zernio.

A failed or partial platform result exposes a **Retry failed target** control, which calls Zernio’s `POST /v1/posts/{postId}/retry` endpoint using the original post ID. For scheduled posts, the panel also offers **Publish now**, **Reschedule**, and **Cancel**, which act on the stored post ID through documented update/delete operations rather than creating a new post. [2]

| Persistent path | Purpose | Contains a secret? |
| --- | --- | --- |
| `branding/zernio_settings.json` | Enablement, selected account IDs, scheduling policy. | No. |
| `branding/zernio_accounts.json` | Sanitized TikTok/YouTube discovery snapshot. | No. |
| `branding/zernio_queue.json` | Active ClipForge smart-schedule reservations. | No. |
| `jobs/<job-id>/status.json` | Stage B state plus sibling provider publishing state. | No. |
| GitHub Actions `ZERNIO_API_KEY` secret | Server-only Zernio credential. | Yes; never committed. |

## Troubleshooting

When publishing fails, open the linked **Zernio workflow run**. The workflow retains sanitized error information and does not print the API key. Confirm that the selected account is active and does not require reconnection; Zernio account records expose `isActive`, `enabled`, and `needsReconnection` fields for this purpose. [3]

A scheduled output is already safely preserved in Zernio. Use **Reschedule**, **Publish now**, or **Cancel** from the task panel only when its saved Zernio post status permits the operation. Do not submit the video as a new publishing request simply to recover a failed target; use the saved post’s retry action.

## References

[1]: https://docs.zernio.com/llms-full.txt "Zernio API documentation — create posts, idempotency, media uploads, and scheduling"
[2]: https://docs.zernio.com/posts/update-post "Zernio API reference — updating, deleting, and retrying posts"
[3]: https://docs.zernio.com/llms-full.txt "Zernio API documentation — connected account fields"
