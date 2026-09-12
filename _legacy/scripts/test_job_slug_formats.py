#!/usr/bin/env python3
"""Regression coverage for generated ClipForge Stage A job-ID formats."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.js").read_text(encoding="utf-8")

EXPECTED = (
    "slug = (automaticMode ? 'automatic-' : "
    "(torrentFile ? 'torrent-' : (isMagnetLink ? 'magnet-' : 'manual-'))) + Date.now();"
)
LEGACY_BUG = (
    "slug = automaticMode ? 'automatic-' : "
    "(torrentFile ? 'torrent-' : (isMagnetLink ? 'magnet-' : 'manual-')) + Date.now();"
)

assert EXPECTED in APP, "generated Stage A slugs must append Date.now() after choosing every prefix"
assert LEGACY_BUG not in APP, "Automatic Mode must not retain the precedence bug"
assert APP.count("'automatic-'") == 1, "there must be one generated Automatic Mode prefix path"

# Document the four outcome contracts represented by the shared expression.
def generated_slug(automatic: bool, torrent: bool, magnet: bool, now: int) -> str:
    prefix = "automatic-" if automatic else ("torrent-" if torrent else ("magnet-" if magnet else "manual-"))
    return prefix + str(now)

now = 1787551490123
assert generated_slug(True, False, False, now) == "automatic-1787551490123"
assert generated_slug(False, True, False, now) == "torrent-1787551490123"
assert generated_slug(False, False, True, now) == "magnet-1787551490123"
assert generated_slug(False, False, False, now) == "manual-1787551490123"

print("PASS: generated Automatic, torrent, magnet, and manual Stage A IDs all retain their timestamped formats")
