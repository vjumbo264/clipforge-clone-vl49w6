# Zernio API validation notes

The official Zernio documentation states that a post moves through `scheduled → publishing → published` (or `failed` / `partial`) and that the publish response includes per-platform results; status changes can be delivered through webhooks. The same documentation identifies `post.platform.published` and `post.platform.failed` as per-platform terminal events, and recommends subscribing to post webhooks instead of polling for status changes.

The current ClipForge implementation does not expose a webhook receiver. Its GitHub Actions publisher records the initial Zernio response and persists `publishing.posts[]`, so a TikTok post can remain at its submission-time `publishing` state unless the workflow explicitly reconciles the post with a later API read. The official docs also use `content` as the general post body/caption field; platform-specific title fields are distinct where supported.

Sources: https://docs.zernio.com/llms-full.txt, https://docs.zernio.com/platforms/tiktok, https://docs.zernio.com/webhooks/posts, https://docs.zernio.com/changelog
