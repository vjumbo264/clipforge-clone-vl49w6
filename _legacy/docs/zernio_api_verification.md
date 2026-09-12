# Zernio API Verification Notes

**Status:** In progress. This note records only capabilities confirmed from Zernio’s official documentation retrieved on 22 August 2026. It is not an implementation claim.

## Confirmed API contract

| Capability | Verified behavior | Integration consequence |
| --- | --- | --- |
| Base URL and authentication | The API base URL is `https://zernio.com/api/v1`. Requests use `Authorization: Bearer <API_KEY>`; Zernio documents `ZERNIO_API_KEY` environment-variable use. | Keep the key in a GitHub Actions secret and make all Zernio calls server-side. Never put the key in job files, status, releases, or browser requests. |
| Connected accounts | `GET /v1/accounts` lists connected accounts; each has an account `_id`, platform, and connection state. Profiles group accounts. | Discover connected TikTok/YouTube accounts rather than hardcoding platform availability. |
| Multi-platform post creation | `POST /v1/posts` accepts a `platforms` array containing platform/account targets. | One ClipForge publishing intent can include multiple platform targets while ClipForge records their returned state independently. |
| Immediate, scheduled, and draft modes | `publishNow: true` publishes immediately. `scheduledFor` plus an explicit IANA `timezone` creates a scheduled post. Omitting both defaults to a draft. | Use Zernio’s native scheduler; do not keep a GitHub runner alive. |
| Status retrieval | `GET /v1/posts/{postId}` returns a post. Documented lifecycle is `draft`, `scheduled`, `publishing`, `published`, `failed`, or `partial`; published targets expose `platformPostUrl`. | Store Zernio post IDs and derive ClipForge publishing status separately from Stage B status. |
| Duplicate protection | `x-request-id` gives five-minute idempotency for a logical request. Independently, Zernio rejects same account/platform/content/media fingerprints within 24 hours with HTTP 409 and an `existingPostId`. | Generate one stable idempotency key per ClipForge publish attempt, persist it, and never republish already-successful targets during a failed-target retry. |
| Queue scheduling | `queuedFromProfile` schedules a post on the profile’s next free native queue slot. Documentation warns not to emulate the queue by fetching a next slot and sending it as `scheduledFor`, because that bypasses locking. | Any native-queue option must call the documented queue flow, not client-side slot math. User-defined interval semantics still require further verification. |
| Errors and rate limits | Error envelopes include human-readable `error`, `type`, `code`, optional `platform`, raw `platformError`, and structured details. Documented 429 cases include API rate limits, an account velocity limit of 25 posts/hour, cooldowns, and daily platform limits. | Translate errors into job-safe messages while storing sanitized technical detail. Rate limits must not change Stage B completion. |

## Initial limitations and open verification items

The current documentation confirms native profile queues but does **not yet confirm** that a profile queue can be configured with ClipForge’s requested arbitrary day interval, preferred local time, maximum depth, or per-job custom queue policy. The implementation must not claim those controls until the queue endpoints and schemas are verified.

The current documentation confirms upload is a presign flow returning an upload URL and a public URL used in posts. Exact request/response fields, temporary-storage behavior, and TikTok/YouTube video restrictions require further extraction from the official specification before implementation.

## Official sources

1. [Zernio API documentation overview](https://docs.zernio.com/)
2. [Zernio complete documentation corpus](https://docs.zernio.com/llms-full.txt)
3. [Zernio platform overview](https://docs.zernio.com/platforms)


## Additional verified platform and media findings

| Capability | Verified behavior | Integration consequence |
| --- | --- | --- |
| Media upload transport | Zernio documents `POST /v1/media/presign` with `filename` and `contentType`, returning an `uploadUrl` for a binary `PUT` and a `publicUrl` to use in `mediaItems`. Official documentation lists a 5 GB upload ceiling and accepts `video/mp4`, `video/quicktime`, and `video/webm` among other types. | A separate GitHub Actions publish workflow can retrieve the existing `final.mp4`, request a presigned URL with the server-only secret, upload the unchanged MP4, then create the post from the returned public URL. |
| TikTok and YouTube | Official account-connect guidance lists both `tiktok` and `youtube` as supported platforms. The posting reference contains `TikTokPlatformData`; the platform configuration reference lists TikTok privacy and AI-disclosure settings, and YouTube visibility, category, playlist, thumbnail, tags, and synthetic-media disclosure controls. | Discover actual connected account records and expose only active TikTok/YouTube targets. Start with the existing job title/caption/tags metadata and send only minimal documented per-platform fields. |
| YouTube constraints | The official schema records a YouTube title maximum of 100 characters; tags are individually limited to 100 characters and combined to 500 characters. | ClipForge must trim or reject unsupported title/tag data before submission. |
| Per-platform retry | The official post reference contains `POST /v1/posts/{postId}/retry`, and post states include `partial`. | Preserve the Zernio post ID and use the documented retry endpoint only when the authoritative post state identifies failed targets. Do not create a new post to retry a partial failure. |
| Edit and cancel | The reference exposes post edit and delete endpoints. The exact valid transitions still need endpoint-schema verification before the UI enables schedule edits, cancellation, or a publish-now override. | Keep such controls disabled unless the post state and verified endpoint contract permit them. |
| Webhook opportunity | Official docs describe `post.published` and related webhook events, including a TikTok URL-resolution event. | The current GitHub Actions and Telegram-bot architecture has no webhook receiver. The first implementation should persist synchronous response/status refreshes; webhook-driven updates require a separately hosted receiver and must not be implied by the automation. |

Sources: [Zernio complete documentation corpus](https://docs.zernio.com/llms-full.txt), [Zernio TikTok reference](https://docs.zernio.com/platforms/tiktok), and [Zernio YouTube reference](https://docs.zernio.com/platforms/youtube).

## Verification refresh — 22 August 2026

The current official reference was re-checked against Zernio’s complete documentation corpus and API pages. The implementation must follow the contracts below rather than relying on the earlier draft assumptions.

| Capability | Verified contract | Implementation decision |
| --- | --- | --- |
| Authentication | Requests use `Authorization: Bearer <ZERNIO_API_KEY>` against `https://zernio.com/api/v1`. | The key remains an encrypted GitHub Actions secret and is never committed, returned to the browser, or written into job state. |
| Connected accounts | `GET /v1/accounts` returns `accounts`; usable records include `_id`, `platform`, `profileId`, `isActive`, `enabled`, and `needsReconnection`. | The UI must show only active, enabled TikTok/YouTube accounts and must surface disconnected/unavailable state honestly. |
| Media upload | `POST /v1/media/presign` accepts `filename`, `contentType`, and optional `size`; it returns an `uploadUrl` and `publicUrl`. The documented maximum is 5 GB, and a presigned URL expires after one hour. | Publishing downloads the existing Stage B `final.mp4`, presigns it, uploads it unchanged, and then uses the returned public URL in a post. |
| Immediate and scheduled posts | `POST /v1/posts` accepts `publishNow: true`, or `scheduledFor` plus an IANA `timezone`; the documented post lifecycle includes `draft`, `scheduled`, `publishing`, `published`, `failed`, and `partial`. | Manual and smart scheduling submit individual native Zernio scheduled posts. GitHub Actions never waits for the scheduled publication time. |
| Duplicate protection | `x-request-id` prevents duplicate handling of the same logical request for roughly five minutes; a separate 24-hour content-hash rule returns HTTP 409 with `details.existingPostId`. | Persist one idempotency key per logical publish attempt and retain returned/existing post IDs before any retry. |
| Per-platform retry | `POST /v1/posts/{postId}/retry` immediately retries a failed post and returns the updated post; it may return partial success. | Retrying a partial post uses the original Zernio post ID rather than creating another cross-platform post. |
| Schedule updates and cancellation | `PUT /v1/posts/{postId}` may edit draft, scheduled, failed, partial, and cancelled posts; `DELETE /v1/posts/{postId}` deletes draft or scheduled posts, not published posts. | Edit-schedule, publish-now override, and cancel controls operate on the stored Zernio post IDs and never create a duplicate first. |
| Native profile queues | `queuedFromProfile` lets Zernio allocate a profile queue slot. `GET /v1/queue/next-slot` is explicitly preview-only and must not be converted into `scheduledFor`, because doing so bypasses queue locking. | ClipForge’s required arbitrary N-day cadence, preferred local time, queue depth, and per-job start choice are calculated in ClipForge and emitted as individual `scheduledFor` posts rather than substituted with `queuedFromProfile`. |
| TikTok and YouTube metadata | Root `title`, `content`, `hashtags`, and `tags` are supported on post creation. The reference documents YouTube title <= 100 characters and tags <= 100 characters each / <= 500 characters combined; TikTok supports platform settings including `videoMadeWithAi`. | The publisher isolates the two platform payloads: YouTube receives title and normalized tags; TikTok receives a caption and normalized hashtags. |

Official sources: [Zernio API documentation corpus](https://docs.zernio.com/llms-full.txt), [Create Post API reference](https://docs.zernio.com/posts/create-post), [Update Post API reference](https://docs.zernio.com/posts/update-post), [Media uploads guide](https://docs.zernio.com/guides/media-uploads), and [Queue scheduling guide](https://docs.zernio.com/guides/queue-scheduling).
