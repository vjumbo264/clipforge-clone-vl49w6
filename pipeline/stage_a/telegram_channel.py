"""ClipForge Stage A — PRESERVED SUBSYSTEM #1: public Telegram channel-link
MTProto download (ARCHITECTURE.md §9.1).

Ported essentially verbatim from ``_legacy/scripts/download_drive.py``'s
Telegram path (``_download_telegram_mtproto`` / ``download_telegram_mtproto`` /
``mtproto_credentials_available`` / ``download_telegram_public_post`` /
``telegram_no_media_error``). The bounded parallel transfer itself is NOT
duplicated here — it was already extracted to ``relay/mtproto_transfer.py``
during the relay phase and is reused unchanged.

The restriction (preserved exactly, two enforcement layers):

1. **Server-side repo gate** — this module refuses to run unless
   ``GITHUB_REPOSITORY == 'motionssalt/clipforge'`` (the new-design rule from
   §9.1; the legacy equivalent is ``permitsLegacyTelegramMtproto`` in
   ``_legacy/telegram-bot/src/index.js``).
2. **Missing-secrets fail-closed** — the MTProto secrets
   (``CLIPFORGE_TELEGRAM_API_ID/HASH/SESSION``) exist only as Actions secrets
   on the original repo. If they are absent or invalid, this module fails
   closed before touching Telegram.

Do NOT redesign this file. Accepts only ``t.me/<channel>/<msg_id>`` public
*channel* posts; rejects groups, private links, and non-post pages.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

import requests

# Reuse the already-extracted bounded parallel transfer (verbatim port, §9).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "relay"))
from mtproto_transfer import (  # noqa: E402
    _ParallelTelegramTransferError,
    _download_telegram_parallel,
)

from .ingest import (  # noqa: E402
    IngestError,
    MAX_TELEGRAM_MEDIA_BYTES,
    REQUEST_TIMEOUT,
    telegram_public_post_url,
)

ORIGINAL_CLIPFORGE_REPOSITORY = "motionssalt/clipforge"

# Secrets (names only — values live exclusively in the original repo's
# Actions secret store; clones never receive them, which IS the security
# boundary's second layer).
MTPROTO_SECRET_NAMES = (
    "CLIPFORGE_TELEGRAM_API_ID",
    "CLIPFORGE_TELEGRAM_API_HASH",
    "CLIPFORGE_TELEGRAM_SESSION",
)


class TelegramChannelError(IngestError):
    """A user-facing failure of the §9.1 channel download (gate, auth, media)."""


def running_on_original_repository(environ: dict | None = None) -> bool:
    """Layer 1: server-side re-verification that this is the original repo."""
    env = os.environ if environ is None else environ
    repo = (env.get("GITHUB_REPOSITORY") or "").strip().lower()
    return bool(repo) and repo == ORIGINAL_CLIPFORGE_REPOSITORY


def mtproto_credentials_available(environ: dict | None = None) -> bool:
    """Layer 2: fail closed unless all three MTProto secrets are present."""
    env = os.environ if environ is None else environ
    return all((env.get(name) or "").strip() for name in MTPROTO_SECRET_NAMES)


def require_original_repo(environ: dict | None = None) -> None:
    """Raise unless BOTH §9.1 enforcement layers pass. Fails closed."""
    if not running_on_original_repository(environ):
        env = os.environ if environ is None else environ
        repo = (env.get("GITHUB_REPOSITORY") or "").strip() or "(unknown)"
        raise TelegramChannelError(
            "Telegram channel sources are restricted to the original ClipForge "
            f"repository ({ORIGINAL_CLIPFORGE_REPOSITORY}); running repo is "
            f"{repo}. Pick a different source kind."
        )


async def _download_telegram_mtproto(canonical: str, output_path: str, api_id: int) -> None:
    """Download a public channel post via the dedicated authenticated session.

    Verbatim port of the legacy ``_download_telegram_mtproto``.
    """
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as error:
        raise TelegramChannelError("Telegram MTProto support is unavailable on this runner.") from error
    _, channel, message_id = urllib.parse.urlparse(canonical).path.split("/")
    api_hash = os.environ["CLIPFORGE_TELEGRAM_API_HASH"]
    session = os.environ["CLIPFORGE_TELEGRAM_SESSION"]
    client = TelegramClient(
        StringSession(session), api_id, api_hash,
        connection_retries=3, request_retries=3, retry_delay=1,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise TelegramChannelError(
                "The dedicated Telegram media session is no longer authorized. "
                "Re-authorize it in ClipForge settings."
            )
        entity = await client.get_entity(channel)
        if not getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
            raise TelegramChannelError(
                "This is a public Telegram group link. ClipForge downloads social video "
                "only from a public Telegram channel post. Create a public channel—not a "
                "group—forward or upload the video there, then send that channel post link."
            )
        message = await client.get_messages(entity, ids=int(message_id))
        if not message or not message.media:
            raise TelegramChannelError("This public Telegram channel post has no media attachment.")
        mime_type = str(getattr(getattr(message, "file", None), "mime_type", "") or "").lower()
        if not getattr(message, "video", None) and not mime_type.startswith("video/"):
            raise TelegramChannelError(
                "This public Telegram channel post does not contain a video attachment."
            )
        media_size = int(getattr(getattr(message, "file", None), "size", 0) or 0)
        if media_size and media_size > MAX_TELEGRAM_MEDIA_BYTES:
            raise TelegramChannelError(
                f"Telegram media exceeds the {MAX_TELEGRAM_MEDIA_BYTES // (1024 ** 3)} GiB "
                "Stage A safety limit."
            )
        print(
            "Downloading one public Telegram channel-post video through the dedicated "
            "authenticated media session.",
            flush=True,
        )
        os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
        try:
            written_path = await asyncio.wait_for(
                _download_telegram_parallel(client, message, output_path, media_size),
                timeout=45 * 60,
            )
        except _ParallelTelegramTransferError as error:
            if os.path.exists(output_path):
                os.unlink(output_path)
            print(
                f"Parallel Telegram transfer fell back to Telethon single-connection mode: {error}",
                flush=True,
            )
            written_path = await asyncio.wait_for(
                client.download_media(message, file=output_path), timeout=45 * 60,
            )
        if not written_path or not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
            raise TelegramChannelError(
                "Telegram authenticated media retrieval completed without a video file."
            )
        if os.path.getsize(output_path) > MAX_TELEGRAM_MEDIA_BYTES:
            os.unlink(output_path)
            raise TelegramChannelError(
                f"Telegram media exceeds the {MAX_TELEGRAM_MEDIA_BYTES // (1024 ** 3)} GiB "
                "Stage A safety limit."
            )
        print(f"Done. Wrote authenticated public Telegram post video to {output_path}.", flush=True)
    except asyncio.TimeoutError as error:
        if os.path.exists(output_path):
            os.unlink(output_path)
        raise TelegramChannelError(
            "Telegram authenticated media download exceeded the 45-minute safety limit."
        ) from error
    finally:
        await client.disconnect()


def download_telegram_mtproto(canonical: str, output_path: str) -> None:
    """Synchronous wrapper (verbatim port of the legacy wrapper)."""
    try:
        api_id = int(os.environ["CLIPFORGE_TELEGRAM_API_ID"])
    except ValueError as error:
        raise TelegramChannelError("The configured Telegram API ID is invalid.") from error
    asyncio.run(_download_telegram_mtproto(canonical, output_path, api_id))


def telegram_no_media_error(url: str) -> str:
    """Explain the common public-group case without relying on account access."""
    try:
        response = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; clipforge-downloader/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        }, timeout=REQUEST_TIMEOUT)
        page = response.text.lower() if response.ok else ""
    except requests.RequestException:
        page = ""
    if "view in group" in page:
        return (
            "This is a public Telegram group post, not a public channel post. "
            "Telegram does not expose a downloadable media file for this group link. "
            "Create a public channel (not a group), forward or upload the video there, "
            "then send its exact https://t.me/<channel>/<message_id> post link."
        )
    return (
        "Telegram did not expose one downloadable video for this public post. "
        "Use a public channel (not a group), make sure the post visibly contains one video, "
        "and send that exact post link."
    )


def download_telegram_public_post(canonical: str, output_path: str) -> None:
    """Download one public Telegram post video without any account session.

    Verbatim port of the legacy ``download_telegram_public_post`` (yt-dlp).
    """
    output_dir = tempfile.mkdtemp(prefix="clipforge-telegram-post-")
    template = os.path.join(output_dir, "source.%(ext)s")
    command = [
        sys.executable, "-m", "yt_dlp", "--no-config", "--no-playlist",
        "--abort-on-error", "--no-warnings", "--restrict-filenames", "--no-keep-video",
        "--retries", "3", "--socket-timeout", "60", "--max-filesize", str(MAX_TELEGRAM_MEDIA_BYTES),
        "--format", "bv*+ba/b", "--merge-output-format", "mp4", "-o", template, "--", canonical,
    ]
    try:
        print(
            "Downloading one public Telegram channel-post video with yt-dlp "
            "(no account login or cookies).",
            flush=True,
        )
        try:
            subprocess.run(command, check=True, timeout=45 * 60)
        except subprocess.TimeoutExpired as exc:
            raise TelegramChannelError(
                "Telegram public-post download exceeded the 45-minute safety limit."
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise TelegramChannelError(
                "Telegram could not provide a downloadable public video from this post. "
                "Confirm the channel and post are public and that the post contains a video, "
                "then try again."
            ) from exc
        files = [entry for entry in os.scandir(output_dir) if entry.is_file() and entry.stat().st_size > 0]
        if len(files) != 1:
            if not files:
                raise TelegramChannelError(telegram_no_media_error(canonical))
            names = ", ".join(sorted(entry.name for entry in files))
            raise TelegramChannelError(
                f"Telegram post produced multiple final media files ({names}); "
                "send a post containing exactly one video."
            )
        os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
        shutil.move(files[0].path, output_path)
        print(f"Done. Wrote public Telegram post video to {output_path}.", flush=True)
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


def download_channel_post(url: str, output_path: str, *, environ: dict | None = None) -> str:
    """Download one public Telegram channel-post video to ``output_path``.

    Applies both §9.1 enforcement layers, canonicalizes the link (rejecting
    groups/private/non-post links), then prefers the authenticated MTProto
    path when the secrets are present and falls back to the anonymous yt-dlp
    public-post path otherwise — exactly the legacy dispatch semantics.

    Returns the canonical post URL actually used.
    """
    require_original_repo(environ)
    if not mtproto_credentials_available(environ):
        # Layer 2 (§9.1 new-design rule): fail closed without the MTProto
        # secrets. They exist only on the original repo, so this is the
        # security boundary doing its job even if layer 1 is ever bypassed.
        raise TelegramChannelError(
            "Telegram channel downloads require the dedicated MTProto session "
            "secrets (CLIPFORGE_TELEGRAM_API_ID/HASH/SESSION), which are only "
            "configured on the original ClipForge repository. Failing closed."
        )
    canonical = telegram_public_post_url(url)
    if not canonical:
        raise TelegramChannelError(
            "Use a public Telegram channel post link in the form "
            "https://t.me/<channel>/<message_id>. Private and non-post Telegram "
            "links are not supported."
        )
    download_telegram_mtproto(canonical, output_path)
    return canonical


__all__ = [
    "ORIGINAL_CLIPFORGE_REPOSITORY",
    "MTPROTO_SECRET_NAMES",
    "TelegramChannelError",
    "running_on_original_repository",
    "mtproto_credentials_available",
    "require_original_repo",
    "download_telegram_mtproto",
    "download_telegram_public_post",
    "telegram_no_media_error",
    "download_channel_post",
]
