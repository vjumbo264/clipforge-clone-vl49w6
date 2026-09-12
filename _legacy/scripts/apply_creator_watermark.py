#!/usr/bin/env python3
"""Burn a persistent creator watermark into the final Stage B video.

The compositor deliberately creates two transparent full-frame overlays:

* a **soft, semi-transparent black drop shadow** that follows the
  individual letterforms; and
* a partially transparent letter mask whose pixels are sampled from an
  FFmpeg ``screen`` blend of the current frame and white, producing the
  intended light overlay treatment on both dark and bright footage.

The result is a real frame-level watermark, not metadata or a sidecar. It is
run after cinematic captions and before the terminal delivery compression.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FONT_PATH = ROOT / "assets" / "fonts" / "Coolvetica.ttf"
FONT_CONDENSE_RATIO = 0.76
FONT_HEIGHT_FRACTION = 0.055
MIN_FONT_SIZE = 38
MAX_FONT_SIZE = 76
BOTTOM_SAFE_FRACTION = 0.062
SHADOW_BLUR_RADIUS_PX = 2.0
SHADOW_OPACITY = 0.74
SHADOW_OFFSET_Y_PX = 5
TEXT_MASK_OPACITY = 0.63


def probe(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    return int(stream["width"]), int(stream["height"])


def fit_text_mask(name: str, width: int, height: int) -> tuple[Image.Image, tuple[int, int]]:
    """Create a centered, horizontally condensed antialiased text mask."""
    if not FONT_PATH.is_file():
        raise FileNotFoundError(f"Bundled watermark font is missing: {FONT_PATH}")
    font_size = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, round(height * FONT_HEIGHT_FRACTION)))
    font = ImageFont.truetype(str(FONT_PATH), size=font_size)
    scratch = Image.new("L", (1, 1))
    box = scratch.getbbox()
    del box
    from PIL import ImageDraw  # imported here to keep font-layout code local
    draw = ImageDraw.Draw(scratch)
    left, top, right, bottom = draw.textbbox((0, 0), name, font=font, stroke_width=0)
    natural_width = max(1, right - left)
    natural_height = max(1, bottom - top)
    # Reserve blur falloff and the offset so no part of the letter-shaped
    # shadow can be clipped at the safe-area boundary.
    pad = int(SHADOW_BLUR_RADIUS_PX * 3) + SHADOW_OFFSET_Y_PX + 4
    glyph = Image.new("L", (natural_width + pad * 2, natural_height + pad * 2), 0)
    glyph_draw = ImageDraw.Draw(glyph)
    glyph_draw.text((pad - left, pad - top), name, font=font, fill=255)

    condensed_width = max(1, round(glyph.width * FONT_CONDENSE_RATIO))
    glyph = glyph.resize((condensed_width, glyph.height), Image.Resampling.LANCZOS)
    max_width = round(width * 0.72)
    if glyph.width > max_width:
        scale = max_width / glyph.width
        glyph = glyph.resize((max_width, max(1, round(glyph.height * scale))), Image.Resampling.LANCZOS)

    x = (width - glyph.width) // 2
    y = height - round(height * BOTTOM_SAFE_FRACTION) - glyph.height
    return glyph, (x, y)


def build_layers(name: str, width: int, height: int, directory: Path) -> tuple[Path, Path]:
    glyph, (x, y) = fit_text_mask(name, width, height)
    text_mask = Image.new("L", (width, height), 0)
    text_mask.paste(glyph, (x, y))
    # The foreground alpha intentionally remains below 1.0: its actual RGB
    # arrives from FFmpeg's Screen blend rather than an opaque caption color.
    text_mask = text_mask.point(lambda value: round(value * TEXT_MASK_OPACITY))

    # A Gaussian drop shadow keeps the silhouette of each letter. The former
    # wide MaxFilter acted as morphological dilation: neighboring glyphs merged
    # into a hard, opaque word-sized rectangle before the text was composited.
    shadow_source = Image.new("L", (width, height), 0)
    shadow_source.paste(glyph, (x, y + SHADOW_OFFSET_Y_PX))
    shadow_alpha = shadow_source.filter(ImageFilter.GaussianBlur(SHADOW_BLUR_RADIUS_PX))
    shadow_alpha = shadow_alpha.point(lambda value: round(value * SHADOW_OPACITY))
    shadow_rgba = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow_rgba.putalpha(shadow_alpha)

    shadow_path = directory / "watermark_shadow.png"
    mask_path = directory / "watermark_mask.png"
    shadow_rgba.save(shadow_path)
    text_mask.save(mask_path)
    return shadow_path, mask_path


def apply_watermark(source: Path, destination: Path, name: str) -> None:
    name = " ".join(name.split())
    if not name:
        shutil.copyfile(source, destination)
        print("Creator watermark empty — copied video without watermark.")
        return
    width, height = probe(source)
    with tempfile.TemporaryDirectory(prefix="clipforge_watermark_") as tmp:
        shadow_path, mask_path = build_layers(name, width, height, Path(tmp))
        graph = (
            "[0:v]setpts=PTS-STARTPTS,format=rgba[base];"
            "[1:v]setpts=PTS-STARTPTS,format=rgba[shadow];"
            "[base][shadow]overlay=shortest=1:format=auto[shadowed];"
            "[shadowed]split[composite_base][blend_base];"
            f"color=c=white:s={width}x{height}:r=30,format=rgb24[white];"
            "[blend_base][white]blend=all_mode=screen:shortest=1[screen_text];"
            "[2:v]setpts=PTS-STARTPTS,format=gray[text_mask];"
            "[screen_text][text_mask]alphamerge[watermark_text];"
            "[composite_base][watermark_text]overlay=shortest=1:format=auto[watermarked]"
        )
        command = [
            "ffmpeg", "-y", "-i", str(source),
            "-loop", "1", "-framerate", "30", "-i", str(shadow_path),
            "-loop", "1", "-framerate", "30", "-i", str(mask_path),
            "-filter_complex", graph,
            "-map", "[watermarked]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart", "-map_metadata", "-1", "-map_chapters", "-1",
            "-shortest", str(destination),
        ]
        print("Applying creator watermark: screen-blended condensed text + soft letter-shaped shadow")
        print("$ " + " ".join(command), flush=True)
        subprocess.run(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--name", required=True, help="Creator watermark text; empty copies source unchanged")
    args = parser.parse_args()
    apply_watermark(args.source, args.destination, args.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
