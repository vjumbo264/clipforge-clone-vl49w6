#!/usr/bin/env python3
"""Regression checks for the persistent final-stage creator watermark."""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "apply_creator_watermark.py"
COMPOSITOR = MODULE_PATH.read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "stage-b.yml").read_text(encoding="utf-8")
APP = (ROOT / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "index.html").read_text(encoding="utf-8")

spec = importlib.util.spec_from_file_location("apply_creator_watermark", MODULE_PATH)
assert spec and spec.loader
watermark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watermark)


def test_layers_are_condensed_centered_and_safe() -> None:
    with tempfile.TemporaryDirectory(prefix="creator_watermark_") as directory:
        shadow_path, mask_path = watermark.build_layers("Fubara", 1080, 1200, Path(directory))
        shadow = Image.open(shadow_path).convert("RGBA")
        mask = Image.open(mask_path).convert("L")
        alpha = shadow.getchannel("A")
        shadow_box = alpha.getbbox()
        text_box = mask.getbbox()
        assert shadow_box and text_box
        assert 0 < alpha.getextrema()[1] < 255, "shadow must be visibly present but not fully opaque"
        assert 120 < text_box[2] - text_box[0] < 600, "name should be visibly condensed, not full-width"
        center = (text_box[0] + text_box[2]) / 2
        assert abs(center - 540) <= 2, "name should be bottom-centered"
        assert text_box[3] <= 1200 - round(1200 * watermark.BOTTOM_SAFE_FRACTION) + 2
        assert mask.getextrema()[1] < 255, "foreground must stay non-opaque for blended treatment"


def test_shadow_uses_soft_letter_shaped_drop_shadow_not_dilation() -> None:
    assert "ImageFilter.GaussianBlur" in COMPOSITOR
    assert "ImageFilter.MaxFilter" not in COMPOSITOR
    assert "SHADOW_OPACITY = 0.74" in COMPOSITOR
    assert "hard, opaque word-sized rectangle" in COMPOSITOR


def test_compositor_uses_light_screen_blend_for_foreground() -> None:
    assert "blend=all_mode=screen" in COMPOSITOR
    assert "blend=all_mode=overlay" not in COMPOSITOR
    assert "screen-blended condensed text" in COMPOSITOR


def test_empty_name_is_an_explicit_no_watermark_copy() -> None:
    with tempfile.TemporaryDirectory(prefix="creator_watermark_") as directory:
        source = Path(directory) / "source.bin"
        destination = Path(directory) / "destination.bin"
        source.write_bytes(b"unchanged-video-bytes")
        watermark.apply_watermark(source, destination, "   \n")
        assert destination.read_bytes() == source.read_bytes()


def test_stage_b_burns_watermark_after_captions_and_before_delivery_compression() -> None:
    captions = WORKFLOW.index("- name: Burn cinematic subtitles into the final video")
    watermark_step = WORKFLOW.index("- name: Burn creator watermark into final video")
    compress = WORKFLOW.index("- name: Compress final video for delivery")
    assert captions < watermark_step < compress
    assert "scripts/apply_creator_watermark.py" in WORKFLOW
    assert "CREATOR_WATERMARK_APPLIED" in WORKFLOW
    assert "creator_watermark.json" in WORKFLOW
    assert "inputs.brand" not in WORKFLOW
    assert "brand_scenes.py work/out" not in WORKFLOW
    assert "BRANDING_" not in WORKFLOW


def test_profile_ui_contains_only_creator_name_controls() -> None:
    assert "creator_watermark.json" in APP
    assert "watermark-name-input" in APP and "watermark-name-input" in HTML
    assert "branding-username-input" not in APP and "branding-username-input" not in HTML
    assert "branding-avatar-input" not in APP and "branding-avatar-input" not in HTML
    assert "loadWatermark" in APP and "saveWatermark" in APP
    assert "watermarkSha" in APP and "putRepoFile(WATERMARK_JSON_PATH" in APP


def test_frontend_has_a_safe_repository_file_writer() -> None:
    assert "async function putRepoFile(repoPath, content, message)" in APP
    assert "current = await gh(endpoint + '?ref=' + encodeURIComponent(REF))" in APP
    assert "if (current && current.sha) body.sha = current.sha" in APP
    assert "return gh(endpoint, { method: 'PUT', body: body })" in APP
    assert "Invalid repository file path." in APP
    # The redesigned static workspace deliberately uses a plain same-origin
    # app.js reference; cache-busted historic builds remain acceptable too.
    assert 'src="app.js"' in HTML or 'src="app.js?v=' in HTML


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"Creator watermark tests passed ({len(tests)} tests)")
