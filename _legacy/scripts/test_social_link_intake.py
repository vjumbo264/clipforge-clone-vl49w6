#!/usr/bin/env python3
"""Deterministic contract checks for Telegram-only social source intake."""
from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('download_drive', ROOT / 'scripts' / 'download_drive.py')
module = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(module)


def test_public_telegram_post_policy() -> None:
    assert module.telegram_public_post_url('https://t.me/europa_press/613') == 'https://t.me/europa_press/613'
    assert module.telegram_public_post_url('https://telegram.me/europa_press/613?single') == 'https://t.me/europa_press/613'
    assert module.telegram_public_post_url('https://t.me/s/europa_press/613') == 'https://t.me/europa_press/613'
    invalid = [
        'https://t.me/c/123456/613',
        'https://t.me/+privateInvite',
        'https://t.me/joinchat/privateInvite',
        'https://t.me/europa_press',
        'https://t.me/europa_press/not-a-message',
        'https://t.me.evil.example/europa_press/613',
        'ftp://t.me/europa_press/613',
    ]
    for url in invalid:
        assert module.telegram_public_post_url(url) is None, url


def test_other_social_hosts_are_explicitly_disabled() -> None:
    cases = {
        'https://www.youtube.com/watch?v=abc': 'youtube.com',
        'https://www.tiktok.com/@creator/video/123': 'tiktok.com',
        'https://www.instagram.com/reel/abc/': 'instagram.com',
        'https://www.facebook.com/reel/123': 'facebook.com',
        'https://x.com/creator/status/123': 'x.com',
        'https://vimeo.com/123': 'vimeo.com',
        'https://www.reddit.com/r/example/comments/abc/video/': 'reddit.com',
    }
    for url, expected in cases.items():
        assert module.disabled_social_host(url) == expected, url
    assert module.disabled_social_host('https://instagram.com.evil.example/reel/a') is None


def test_stage_a_uses_no_social_session_or_youtube_runtime() -> None:
    requirements = (ROOT / 'scripts' / 'requirements.txt').read_text(encoding='utf-8')
    workflow = (ROOT / '.github' / 'workflows' / 'stage-a.yml').read_text(encoding='utf-8')
    assert 'yt-dlp[default]>=2026.1,<2027' in requirements
    assert 'curl-cffi' not in requirements
    assert 'CLIPFORGE_YOUTUBE_COOKIES' not in workflow
    assert 'clipforge-youtube-cookies' not in workflow
    assert 'actions/setup-node@v4' not in workflow
    assert 'Telegram public post' in workflow
    assert 'CLIPFORGE_TELEGRAM_API_ID' in workflow
    assert 'CLIPFORGE_TELEGRAM_API_HASH' in workflow
    assert 'CLIPFORGE_TELEGRAM_SESSION' in workflow
    assert 'cryptg>=0.4,<1' in requirements
    assert 'telethon>=1.44,<2' in requirements
    assert 'source_type' in workflow
    assert 'telegram_bot_forward' in workflow
    assert 'RELAY_RELEASE_TAG' in workflow
    assert 'gh release download "$RELAY_RELEASE_TAG"' in workflow
    assert 'Temporary relay source integrity validation failed' in workflow
    relay_workflow = (ROOT / '.github' / 'workflows' / 'telegram-relay.yml').read_text(encoding='utf-8')
    assert 'BOTB_MTPROTO_BOT_TOKEN' in relay_workflow
    assert 'RELAY_ENCRYPTION_KEY' in relay_workflow
    assert 'telegram_relay.py' in relay_workflow


def test_telegram_command_uses_best_streams_without_cookie_or_login() -> None:
    with tempfile.TemporaryDirectory() as temp:
        output = Path(temp) / 'final.mp4'
        calls: list[list[str]] = []

        def fake_run(command: list[str], check: bool, timeout: int) -> None:
            calls.append(command)
            template = command[command.index('-o') + 1]
            Path(template.replace('%(ext)s', 'mp4')).write_bytes(b'video')

        with patch.dict(os.environ, {'CLIPFORGE_YOUTUBE_COOKIES_FILE': '/not/used'}, clear=False):
            with patch.object(module.subprocess, 'run', fake_run):
                module.download_telegram_public_post('https://t.me/europa_press/613', str(output))
        command = calls[0]
        assert output.read_bytes() == b'video'
        assert '--no-config' in command and '--no-playlist' in command and '--no-keep-video' in command
        assert '--cookies' not in command
        assert '--impersonate' not in command
        assert '--extractor-args' not in command
        assert command[command.index('--format') + 1] == 'bv*+ba/b'
        assert command[-1] == 'https://t.me/europa_press/613'


def test_mtproto_parallel_transfer_is_bounded_and_has_fallback() -> None:
    source = (ROOT / 'scripts' / 'download_drive.py').read_text(encoding='utf-8')
    assert 'TELEGRAM_PARALLEL_WORKERS = 4' in source
    assert 'TELEGRAM_PARALLEL_PART_BYTES = 512 * 1024' in source
    assert 'Downloading the public Telegram channel video through {workers} authenticated MTProto connections.' in source
    assert 'Parallel Telegram transfer fell back to Telethon single-connection mode' in source
    assert 'await asyncio.gather(*(child.disconnect() for child in children), return_exceptions=True)' in source


def test_mtproto_secret_gate_requires_complete_credentials() -> None:
    keys = ['CLIPFORGE_TELEGRAM_API_ID', 'CLIPFORGE_TELEGRAM_API_HASH', 'CLIPFORGE_TELEGRAM_SESSION']
    with patch.dict(os.environ, {key: '' for key in keys}, clear=False):
        assert module.mtproto_credentials_available() is False
    with patch.dict(os.environ, dict(zip(keys, ['123', 'hash', 'session'])), clear=False):
        assert module.mtproto_credentials_available() is True


def test_group_post_without_exposed_media_has_actionable_error() -> None:
    class GroupPage:
        ok = True
        text = '<a>View In Group</a>'

    with tempfile.TemporaryDirectory() as temp:
        output = Path(temp) / 'final.mp4'
        with patch.object(module.subprocess, 'run', lambda *args, **kwargs: None):
            with patch.object(module.requests, 'get', lambda *args, **kwargs: GroupPage()):
                try:
                    module.download_telegram_public_post('https://t.me/motionsaltdownloads/2', str(output))
                    raise AssertionError('group post with no exposed media was accepted')
                except RuntimeError as error:
                    text = str(error)
                    assert 'public Telegram group post' in text
                    assert 'public channel' in text
                    assert 'https://t.me/<channel>/<message_id>' in text


def main() -> None:
    test_public_telegram_post_policy()
    test_other_social_hosts_are_explicitly_disabled()
    test_stage_a_uses_no_social_session_or_youtube_runtime()
    test_telegram_command_uses_best_streams_without_cookie_or_login()
    test_mtproto_parallel_transfer_is_bounded_and_has_fallback()
    test_mtproto_secret_gate_requires_complete_credentials()
    test_group_post_without_exposed_media_has_actionable_error()
    print('Telegram-only social intake tests passed')


if __name__ == '__main__':
    main()
