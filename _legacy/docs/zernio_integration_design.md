# ClipForge × Zernio Integration Design

**Status:** Approved for implementation from verified Zernio documentation as of 22 August 2026. This design preserves ClipForge’s existing **Stage A → production plan → Stage B → final.mp4** lifecycle. Publishing is an optional, independent follow-on layer.

## Verified capability summary

| Area | Confirmed Zernio capability | Design decision |
| --- | --- | --- |
| Authentication | Bearer API key sent to `https://zernio.com/api/v1`. | Store the raw key only as the repository Actions secret `ZERNIO_API_KEY`, encrypted from the browser with the existing sealed-box secret path. |
| Account discovery | `GET /v1/accounts` returns connected platform accounts and their IDs. | A publish workflow refreshes a sanitized account snapshot. The UI only renders active TikTok/YouTube account targets returned by Zernio. |
| Video upload | `POST /v1/media/presign` yields a binary upload URL and a public media URL; Zernio accepts MP4 media. | The workflow downloads ClipForge’s existing Release `final.mp4`, uploads it unchanged through the presigned URL, then creates the Zernio post. |
| Immediate and scheduled posts | `POST /v1/posts` supports `publishNow: true`; future `scheduledFor` values with IANA timezone; and `queuedFromProfile` for native queues. | Manual publish now and manual schedule are supported. Smart scheduling uses the native Zernio queue, not a runner-held timer or home-grown next-slot calculation. |
| Queue behavior | Queue slots are profile-owned, timezone-aware, DST-safe, lock during `queuedFromProfile` creation, and can be previewed. | The UI can configure one ClipForge-managed profile queue made from recurring day/time slots. Posting uses `queuedFromProfile` plus `queueId`; preview is display-only. |
| Multi-platform outcome | Post lifecycle includes `scheduled`, `publishing`, `published`, `partial`, and `failed`; posts expose per-platform records and published URLs. | ClipForge stores a separate `publishing` object in a job’s persistent `status.json`; its top-level Stage B `stage` remains `complete`. |
| Safe retries | Zernio offers `x-request-id` idempotency plus content-hash duplicate protection and `POST /v1/posts/{postId}/retry`. | Persist Zernio post IDs and request IDs. Failed-target retries use Zernio’s retry endpoint, not a second create request. |
| Schedule modification/cancellation | Zernio documents post edit and deletion endpoints. Exact state-transition details must be confirmed in the post response at runtime. | The first UI exposes only actions whose prerequisites are known: manual creation, status refresh, and failed-target retry. It does not claim schedule editing, cancel, or publish-now override until the returned post state permits it and the workflow verifies the operation. |

## Persistent model

The repository remains the database. No new external database is introduced.

| Repository record | Contains | Never contains |
| --- | --- | --- |
| `branding/zernio_settings.json` | Default profile, selected account IDs, default publishing mode, queue preferences, and masked-key metadata. | Raw Zernio API key. |
| `branding/zernio_accounts.json` | Sanitized discovered account records, connection result, and timestamp. | Raw Zernio API key, bearer headers, or Zernio request bodies with credentials. |
| `jobs/<job-id>/status.json` | Existing Stage A/Stage B fields plus a sibling `publishing` object with provider, state, post ID, schedule, account outcomes, and sanitized error details. | Raw Zernio API key or presigned upload URL credentials. |
| `jobs/<job-id>/publishing-request.json` | A user-selected publish intent: account IDs, mode, schedule data, request ID, and source-artifact identity. | Raw Zernio API key. |

> **Invariant:** A publishing error updates only `publishing.status`; it must never change the Stage B `stage` from `complete` or cause Stage B to rerun.

## Workflow design

A separate `zernio-publish.yml` is dispatched manually from the static UI or automatically after Stage B finishes. It has its own per-job concurrency group and writes its own publishing state. The workflow does the following:

1. Validates that the job’s top-level Stage B state is `complete` and its Release exposes `final.mp4`.
2. Reads the requested account targets and validates them against a current Zernio account-discovery response.
3. Downloads the existing private Release asset using `GITHUB_TOKEN`; it never re-runs Stage B, regenerates narration, or re-encodes the video.
4. Requests an upload URL, uploads `final.mp4`, then creates an immediate, manual-scheduled, or native-queue Zernio post.
5. Persists only post identifiers, target results, status, schedule time, and sanitized errors.
6. Handles a non-successful Zernio response without changing the Stage B result.

## Smart-scheduling interpretation

The request asks for configurable interval, preferred time, queue limit, and next available slot. Zernio’s documented queue supports recurring local-time slots, queue previews, and atomic next-slot assignment. It does **not** document a per-post arbitrary interval parameter or a maximum queued-post setting.

ClipForge therefore maps a configured whole-day interval to a recurring native queue schedule: the selected preferred time on weekday positions derived from the interval. This can exactly represent a one-day cadence and selected weekly cadences, but it cannot represent arbitrary "every N days" sequences across calendar weeks for every N without changing the queue after every post. The initial implementation will expose a native queue configuration and display its authoritative preview rather than falsely promise an arbitrary interval queue. The configured `maximum_queue` is enforced by ClipForge before dispatch using its persistent publishing records; the default behavior is to stop automatic submission when the limit is reached.

## Security controls

The browser uses the existing GitHub Actions public-key/sealed-box mechanism to put or delete `ZERNIO_API_KEY`. The static site never sends that key to Zernio. Only the private workflow receives it as a secret. Logs and persisted records retain masked fingerprints, status codes, human-readable messages, and provider identifiers only. The test harness must reject source, JSON, and workflow output that contains the API key value.

## Verification boundary

Local tests use a deterministic mock Zernio HTTP service. The repository currently has no discoverable Actions-secret read permission for the authenticated automation session, and no Zernio key has been supplied to this task. Consequently, no real social post will be created during implementation. A post-test using a user-configured `ZERNIO_API_KEY` remains required before claiming live TikTok or YouTube publication is verified.

## Sources

1. [Zernio API overview](https://docs.zernio.com/)
2. [Zernio complete API documentation](https://docs.zernio.com/llms-full.txt)
3. [Zernio platform overview](https://docs.zernio.com/platforms)

