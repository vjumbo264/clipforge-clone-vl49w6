# Social-link intake research

The implementation uses the maintained `yt-dlp` public extractor only for an explicit allowlist of public social-video hosts. The upstream project describes itself as a command-line downloader supporting thousands of sites, documents standard installation through PyPI, and lists dedicated extractors for Instagram, Facebook/Reels, and many other platforms. Its supported-site list also states that platform support can change and a public URL must be attempted to establish current availability.

Key implementation decisions:

- Allow only public, single-video URLs for recognised social hosts; no login, cookies, private media, or playlists.
- Use `--no-config`, `--no-playlist`, bounded retries, a socket timeout, one expected output, a 5 GiB maximum, and a 45-minute runtime ceiling.
- Preserve existing Google Drive, direct-file, magnet, and `.torrent` intake behavior.

Sources:

1. https://github.com/yt-dlp/yt-dlp
2. https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md
