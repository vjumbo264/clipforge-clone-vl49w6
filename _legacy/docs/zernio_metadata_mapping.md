# Production Metadata → Zernio Mapping

ClipForge treats `jobs/<job-id>/production.json` as the sole source of posting metadata. The publisher reads that file at workflow runtime; the browser never asks the user to re-enter the title, caption, hashtags, or tags.

## Current production schema

The repository’s actual example at `jobs/magnet-1787407050747/production.json` contains a top-level `title`, a `hashtags` array whose entries already begin with `#`, a `youtube_tags` array of plain keywords, and the cut/narration fields. It does **not** contain a separate caption or description field. The publisher therefore uses the title as the truthful caption fallback. If a future production file supplies an explicit non-empty `caption` or `description`, the publisher uses it in preference to that fallback without changing the current schema contract.

| production.json field | Normalization | Zernio mapping |
| --- | --- | --- |
| `title` | Trimmed string; YouTube is capped at 100 characters by the payload builder. | Root `title` for YouTube. It is also the caption fallback in root `content` when no explicit caption/description exists. It is not sent as a TikTok-only title field. |
| `caption` or `description` (optional forward-compatible fields) | First non-empty string wins. | Root `content` for the post, including TikTok and YouTube. |
| `hashtags` | Leading `#` characters are collapsed to exactly one; whitespace entries are ignored; case-insensitive duplicates are removed while preserving order. | Root `hashtags` array. No raw JSON or duplicated `#` characters are sent. |
| `youtube_tags` | Plain keywords; a leading `#` is removed, commas are normalized to spaces, duplicates are removed, each item is capped at 100 characters, and the combined payload is capped at 500 characters. | Root `tags` for YouTube only. |
| `tags` (optional forward-compatible alias) | Used only when `youtube_tags` is absent, with the same normalization. | Root `tags` for YouTube only. |

The publisher builds one Zernio post request per platform family. A TikTok payload contains `content`, `hashtags`, media, and TikTok account targets, but no YouTube `title` or `tags`. A YouTube payload contains `content`, `hashtags`, `title`, `tags`, media, and YouTube account targets. The same derived metadata snapshot is retained in the persisted `publishing` state so scheduling and retries do not lose or regenerate it.

## Verified behavior

The offline regression suite `scripts/test_zernio_metadata.py` uses the repository’s actual production example and covers the title fallback, explicit caption/description precedence, hashtag normalization, YouTube tag normalization, platform separation, CLI file sourcing, and preservation through the publishing-state merge. It does not create a real social post.

## References

1. [ClipForge production workflow documentation](../README.md)
2. [Zernio complete API documentation](https://docs.zernio.com/llms-full.txt)
3. [Zernio YouTube platform reference](https://docs.zernio.com/platforms/youtube)
4. [Zernio TikTok platform reference](https://docs.zernio.com/platforms/tiktok)
