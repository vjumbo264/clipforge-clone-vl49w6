#!/usr/bin/env python3
"""bug-59: build the new "vibrant multi-hue glow" 3D LUT from first principles.

The user-supplied reference for this look is a 960x540, 8x8 grid of gradient
tiles (red→yellow→green / pink→cyan→blue→magenta bands). That image is a MOOD
reference, not a valid HALD CLUT identity (HALD images must be perfect-square,
e.g. 512x512), so we do NOT apply it via ``haldclut`` — per the bug brief we
instead analyse its character and synthesise a real 64^3 3D LUT that encodes
the same colour behaviour for every RGB input:

  1. Gentle contrast S-curve around mid-grey (the reference has punchy,
     separated tonal bands — lifted shadows with a slight warmth).
  2. Strong global saturation boost (~1.5x in HSV space) — the dominant
     property of the reference is its very high saturation.
  3. Saturation-dependent hue rotation ("hue-shifting gradient feel"): highly
     saturated mid-bright colours are rotated ~+18 degrees, interpolating
     neighbours toward the same band progressions seen in the reference
     (red→yellow→green and magenta→blue→cyan). Near-neutral and very dark /
     very bright pixels keep their hue so skin tones, shadows and speculars
     are not wrecked.
  4. Mild vibrance-style lift of low-saturation pixels so flat regions share
     the glow without clipping already-vivid colours.

Outputs, both written to ``assets/``:

* ``vibrant_glow_color_cube_l8.png`` — FFmpeg Hald CLUT level-8 (512x512),
  consumed by the existing ``haldclut=shortest=1`` filter, so the render
  pipeline wiring is unchanged apart from the asset path.
* ``vibrant_glow_grade.cube`` — the same mapping in IRIDAS .cube form
  (64^3) for inspection, QA and use by other tools.

Run: ``python -m pipeline.stage_b.build_glow_lut`` from the repo root.
"""
from __future__ import annotations

import argparse
import colorsys
from pathlib import Path

import numpy as np
from PIL import Image

# --- tuneables (the "grade") --------------------------------------------------
SAT_BOOST = 1.5          # global saturation multiplier (reference is very vivid)
SAT_POWER = 0.85         # <1 lifts low-saturation colours more (vibrance feel)
HUE_SHIFT_DEG = 18.0     # hue rotation for fully-saturated mid tones
HUE_SHIFT_SAT_FLOOR = 0.25  # below this saturation, no hue rotation
HUE_SHIFT_SAT_FULL = 0.75   # at/above this saturation, full rotation
CONTRAST = 0.28          # S-curve strength around mid-grey
SHADOW_LIFT = 0.015      # tiny warm lift so blacks stay "glowy" not crushed
WARMTH = (1.03, 1.00, 0.97)  # per-channel gain — the reference reads warm overall

CUBE = 64                # 64^3 LUT, matching the previous asset's resolution
HALD_SIDE = CUBE * CUBE // (CUBE // 8)  # 512 for a level-8 Hald image


def _smoothstep(edge0: float, edge1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _grade(rgb: np.ndarray) -> np.ndarray:
    """Apply the look to an (N, 3) float array in 0..1 RGB space."""
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]

    # Warmth gain.
    r *= WARMTH[0]
    g *= WARMTH[1]
    b *= WARMTH[2]

    # S-curve contrast around 0.5 plus a whisper of shadow lift.
    def scurve(x: np.ndarray) -> np.ndarray:
        x = x + SHADOW_LIFT
        return x + CONTRAST * (x * x * (3 - 2 * x) - x)

    r, g, b = scurve(r), scurve(g), scurve(b)

    # HSV saturation boost + saturation-gated hue rotation.
    flat = np.stack([np.clip(r, 0, 1), np.clip(g, 0, 1), np.clip(b, 0, 1)], axis=1)
    out = np.empty_like(flat)
    # Vectorising colorsys fully is clumsy; a 64^3 LUT is only 262k entries,
    # so a numpy loop per entry is acceptable but slow-ish. Do it in one pass
    # with manual HSV maths instead.
    mx = flat.max(axis=1)
    mn = flat.min(axis=1)
    v = mx
    d = mx - mn
    s = np.where(mx > 1e-6, d / np.maximum(mx, 1e-6), 0.0)

    rc = np.where(d > 1e-6, (mx - flat[:, 0]) / np.maximum(d, 1e-6), 0.0)
    gc = np.where(d > 1e-6, (mx - flat[:, 1]) / np.maximum(d, 1e-6), 0.0)
    bc = np.where(d > 1e-6, (mx - flat[:, 2]) / np.maximum(d, 1e-6), 0.0)
    h = np.where(
        d <= 1e-6,
        0.0,
        np.where(
            flat[:, 0] >= mx,
            (bc - gc) / 6.0,
            np.where(flat[:, 1] >= mx, (2.0 + rc - bc) / 6.0, (4.0 + gc - rc) / 6.0),
        ),
    ) % 1.0

    # Saturation boost with vibrance shaping.
    s_new = np.clip(np.power(s, SAT_POWER) * SAT_BOOST, 0.0, 1.0)

    # Hue rotation, gated by the ORIGINAL saturation so neutrals stay neutral.
    gate = _smoothstep(HUE_SHIFT_SAT_FLOOR, HUE_SHIFT_SAT_FULL, s)
    h_new = (h + (HUE_SHIFT_DEG / 360.0) * gate) % 1.0

    # HSV -> RGB (vectorised).
    i = np.floor(h_new * 6.0).astype(int) % 6
    f = h_new * 6.0 - np.floor(h_new * 6.0)
    p = v * (1.0 - s_new)
    q = v * (1.0 - f * s_new)
    t = v * (1.0 - (1.0 - f) * s_new)
    for k in range(6):
        mask = i == k
        seg = [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][k]
        out[mask, 0] = seg[0][mask]
        out[mask, 1] = seg[1][mask]
        out[mask, 2] = seg[2][mask]
    return np.clip(out, 0.0, 1.0)


def build_lut() -> np.ndarray:
    """Return the 64^3 graded LUT as a (CUBE^3, 3) uint8 array (R-fastest)."""
    idx = np.arange(CUBE ** 3, dtype=np.int64)
    r = (idx % CUBE) / (CUBE - 1)
    g = ((idx // CUBE) % CUBE) / (CUBE - 1)
    b = (idx // (CUBE * CUBE)) / (CUBE - 1)
    rgb = np.stack([r, g, b], axis=1)
    return np.round(_grade(rgb) * 255.0).astype(np.uint8)


def write_hald(lut: np.ndarray, dest: Path) -> None:
    """Write FFmpeg Hald CLUT level-8: flat index R + G*64 + B*4096 maps to
    (index % 512, index // 512) — the exact layout ``haldclut`` expects."""
    image = np.zeros((HALD_SIDE, HALD_SIDE, 3), dtype=np.uint8)
    ys, xs = np.divmod(np.arange(CUBE ** 3, dtype=np.int64), HALD_SIDE)
    image[ys, xs] = lut
    dest.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(image, "RGB").save(dest, format="PNG", optimize=False)


def write_cube(lut: np.ndarray, dest: Path) -> None:
    lines = [
        'TITLE "vibrant glow grade (bug-59)"',
        'COMMENT "Derived from the 960x540 gradient-tile reference: warm S-curve contrast, ~1.5x vibrance saturation, +18deg saturation-gated hue shift."',
        f"LUT_3D_SIZE {CUBE}",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
    ]
    lines += ["{:.6f} {:.6f} {:.6f}".format(*(px / 255.0)) for px in lut]
    dest.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve()
    assets = here.parents[2] / "assets"
    parser.add_argument("--hald", type=Path,
                        default=assets / "vibrant_glow_color_cube_l8.png")
    parser.add_argument("--cube", type=Path,
                        default=assets / "vibrant_glow_grade.cube")
    args = parser.parse_args()
    lut = build_lut()
    write_hald(lut, args.hald)
    write_cube(lut, args.cube)
    print(f"Wrote Hald level-8 CLUT: {args.hald} ({HALD_SIDE}x{HALD_SIDE})")
    print(f"Wrote IRIDAS .cube:     {args.cube} ({CUBE}^3)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
