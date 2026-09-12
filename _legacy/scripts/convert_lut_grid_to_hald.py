#!/usr/bin/env python3
"""Convert a tiled 64³ RGB LUT grid into FFmpeg's Hald CLUT level-8 PNG.

The source grid has 8×8 tiles of 64×64 pixels. Each tile is one blue slice;
red advances across each tile and green advances down each tile. FFmpeg's
``haldclut`` expects the same 64³ lookup table in a 512×512 Hald level-8
ordering, where the flattened index is R + G*64 + B*64².
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

TILE_GRID = 8
CUBE_SIZE = TILE_GRID * TILE_GRID
IMAGE_SIZE = CUBE_SIZE * TILE_GRID


def assert_tiled_grid(image: Image.Image, source: Path) -> None:
    if image.size != (IMAGE_SIZE, IMAGE_SIZE):
        raise ValueError(
            f"{source} is {image.width}×{image.height}; expected a "
            f"{IMAGE_SIZE}×{IMAGE_SIZE} 8×8 tiled 64³ LUT grid."
        )


def source_coordinate(red: int, green: int, blue: int) -> tuple[int, int]:
    """Return the tiled-grid coordinate for a 64³ RGB input coordinate."""
    tile_x = blue % TILE_GRID
    tile_y = blue // TILE_GRID
    return tile_x * CUBE_SIZE + red, tile_y * CUBE_SIZE + green


def hald_coordinate(red: int, green: int, blue: int) -> tuple[int, int]:
    """Return FFmpeg Hald level-8 coordinate for a 64³ RGB input coordinate."""
    index = red + green * CUBE_SIZE + blue * CUBE_SIZE * CUBE_SIZE
    return index % IMAGE_SIZE, index // IMAGE_SIZE


def convert(source: Path, destination: Path) -> None:
    image = Image.open(source).convert("RGB")
    assert_tiled_grid(image, source)
    output = Image.new("RGB", (IMAGE_SIZE, IMAGE_SIZE))
    src = image.load()
    dst = output.load()
    for blue in range(CUBE_SIZE):
        for green in range(CUBE_SIZE):
            for red in range(CUBE_SIZE):
                sx, sy = source_coordinate(red, green, blue)
                dx, dy = hald_coordinate(red, green, blue)
                dst[dx, dy] = src[sx, sy]
    destination.parent.mkdir(parents=True, exist_ok=True)
    output.save(destination, format="PNG", optimize=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="512×512 tiled 64³ LUT PNG")
    parser.add_argument("destination", type=Path, help="FFmpeg Hald level-8 PNG")
    args = parser.parse_args()
    convert(args.source, args.destination)
    print(
        f"Converted {args.source} -> {args.destination} "
        f"({IMAGE_SIZE}×{IMAGE_SIZE}, 64³, FFmpeg Hald level-8)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
