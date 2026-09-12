#!/usr/bin/env python3
"""Deprecated: the anime Hald CLUT is now a shipped asset, not procedurally generated.

Previously this script built ``assets/anime_reference_color_cube_l8.png`` from a
deterministic in-repo procedural grade (a filmic luma S-curve, warm-hue
protection band, cool-highlight tilt, etc.).

The LUT shipped with the repository is now a hand-authored grade converted
from an external tile-grid source LUT into the canonical ffmpeg Hald level-8
layout. Re-generating it procedurally would silently regress the shipped
asset, so this script is intentionally reduced to a no-op stub that prints a
migration notice and exits without touching disk.

If you need to regenerate the LUT, do it out-of-tree from the original tile-grid
source and copy the result into ``assets/anime_reference_color_cube_l8.png`` after
review. Do not restore the old procedural generator here.
"""
from __future__ import annotations

import sys
from pathlib import Path

ASSET = Path(__file__).resolve().parents[1] / "assets" / "anime_reference_color_cube_l8.png"


def main() -> int:
    print(
        "generate_anime_color-cube filter.py is a no-op stub.\n"
        "The anime Hald CLUT is now a shipped asset at:\n"
        f"  {ASSET}\n"
        "It was authored externally from a tile-grid source LUT and remapped to the\n"
        "canonical ffmpeg Hald level-8 layout (512×512, R on fast-x, G on fast-y,\n"
        "B on the outer tile grid). This script does NOT overwrite it — regenerating\n"
        "procedurally would silently regress the shipped grade.\n\n"
        "If you must rebuild the LUT, do it out-of-tree from the original source and\n"
        "copy the result into place after visual review. See scripts/enhance_scenes.py\n"
        "for how the LUT is consumed (ffmpeg `color-cube filter` filter).",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
