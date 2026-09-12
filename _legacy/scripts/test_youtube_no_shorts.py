#!/usr/bin/env python3
"""Protect the no-Shorts-specific YouTube configuration contract."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    ROOT / "app.js",
    ROOT / "index.html",
    ROOT / "scripts" / "zernio_publish.py",
    ROOT / ".github" / "workflows" / "zernio-publish.yml",
    ROOT / "docs" / "zernio_api_verification.md",
]

for target in TARGETS:
    content = target.read_text(encoding="utf-8").lower()
    assert "isshort" not in content, f"Shorts-specific API field returned in {target}"
    assert "videotype" not in content and "video_type" not in content, f"video-type selector returned in {target}"
    assert "classify as shorts" not in content, f"Shorts classification rule returned in {target}"
    assert "youtube shorts" not in content, f"YouTube Shorts setting returned in {target}"

print("PASS: YouTube publishing has no Shorts-specific setting, selector, or classification rule")
