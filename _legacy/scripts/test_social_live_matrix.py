#!/usr/bin/env python3
"""Manual live verification for ClipForge's public Telegram-post intake.

The public URL below comes from the current yt-dlp Telegram extractor fixture.
The probe downloads one actual public channel-post video through the exact Stage
A downloader, validates its media container with ffprobe, records no account
state, and removes the downloaded media afterwards.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOWNLOADER = ROOT / 'scripts' / 'download_drive.py'
RESULT_PATH = ROOT / 'work' / 'telegram_public_post_check.json'
PUBLIC_TELEGRAM_POST = 'https://t.me/europa_press/613'
TIMEOUT_SECONDS = 180


def ffprobe(path: Path) -> float | None:
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        return None
    return duration if result.returncode == 0 and duration > 0 else None


def main() -> None:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='clipforge-telegram-live-') as temp:
        output = Path(temp) / 'telegram-post.media'
        try:
            run = subprocess.run(
                [sys.executable, str(DOWNLOADER), PUBLIC_TELEGRAM_POST, str(output)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )
            exists = output.is_file() and output.stat().st_size > 0
            duration = ffprobe(output) if exists else None
            result = {
                'source_type': 'public_telegram_channel_post',
                'url': PUBLIC_TELEGRAM_POST,
                'status': 'passed' if run.returncode == 0 and duration else 'failed',
                'returncode': run.returncode,
                'bytes': output.stat().st_size if exists else 0,
                'duration_seconds': duration or 0,
                'seconds': round(time.monotonic() - started, 1),
                'output_tail': run.stdout[-1200:],
            }
        except subprocess.TimeoutExpired as error:
            result = {
                'source_type': 'public_telegram_channel_post',
                'url': PUBLIC_TELEGRAM_POST,
                'status': 'timed_out',
                'returncode': None,
                'bytes': output.stat().st_size if output.is_file() else 0,
                'duration_seconds': 0,
                'seconds': round(time.monotonic() - started, 1),
                'output_tail': str(error.stdout or '')[-1200:],
            }
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(f"Telegram public post: {result['status']} ({result['seconds']}s)", flush=True)
    print(f'Results written to {RESULT_PATH}')
    if result['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
