#!/usr/bin/env python3
"""Regression checks for the supplied Stage B LUT grid and FFmpeg Hald usage."""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
CONVERTER = ROOT / "scripts" / "convert_lut_grid_to_hald.py"
GRID = ROOT / "assets" / "anime_reference_lut_grid.png"
HALD = ROOT / "assets" / "anime_reference_color_cube_l8.png"
ENHANCE = ROOT / "scripts" / "enhance_scenes.py"
STAGE_B = ROOT / ".github" / "workflows" / "stage-b.yml"

spec = importlib.util.spec_from_file_location("convert_lut_grid_to_hald", CONVERTER)
assert spec and spec.loader
converter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(converter)


def test_supplied_grid_is_real_64_cubed_tiled_lut() -> None:
    image = Image.open(GRID)
    assert image.size == (512, 512)
    assert converter.CUBE_SIZE == 64
    assert converter.IMAGE_SIZE == 512


def test_hald_asset_is_exactly_rebuilt_from_supplied_grid() -> None:
    with tempfile.TemporaryDirectory(prefix="clipforge_lut_") as directory:
        rebuilt = Path(directory) / "rebuilt_hald.png"
        converter.convert(GRID, rebuilt)
        expected = Image.open(HALD).convert("RGB")
        actual = Image.open(rebuilt).convert("RGB")
        assert expected.size == (512, 512)
        assert ImageChops.difference(expected, actual).getbbox() is None


def test_sampled_lookup_coordinates_preserve_the_grid_mapping() -> None:
    source = Image.open(GRID).convert("RGB")
    hald = Image.open(HALD).convert("RGB")
    for red, green, blue in ((0, 0, 0), (63, 0, 0), (0, 63, 0), (0, 0, 63), (11, 29, 47), (63, 63, 63)):
        sx, sy = converter.source_coordinate(red, green, blue)
        dx, dy = converter.hald_coordinate(red, green, blue)
        assert source.getpixel((sx, sy)) == hald.getpixel((dx, dy))


def test_stage_b_uses_the_converted_hald_asset_as_a_real_filter_input() -> None:
    source = ENHANCE.read_text(encoding="utf-8")
    workflow = STAGE_B.read_text(encoding="utf-8")
    assert 'LUT_ASSET = Path(__file__).resolve().parents[1] / "assets" / "anime_reference_color_cube_l8.png"' in source
    assert 'LUT_FILTER = "haldclut=shortest=1"' in source
    assert 'f"[denoised][1:v]{LUT_FILTER}' in source
    assert 'python scripts/enhance_scenes.py work/out "$enable_arg"' in workflow


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"Stage B LUT tests passed ({len(tests)} tests)")
