"""bug-60 regression tests for the enhance filter chain.

The 2x upscale/sharpen/rescale-back scaffold was removed: it stretched every
non-square source (per-axis caps made the upscale non-uniform; the flat /2
rescale-back assumed uniform 2x) and its final lanczos downscale low-pass-
blurred away most of the sharpening. These tests make sure the scaffold
cannot silently return and that the surviving invariants hold.
"""
from __future__ import annotations

from pipeline.stage_b import enhance


def test_no_upscale_scaffold_in_filter_chain():
    chain = enhance.FILTER_CHAIN
    for marker in ("2*iw", "2*ih", "min(2160", "min(3840", "trunc(iw/2", "trunc(ih/2"):
        assert marker not in chain, f"upscale scaffold marker {marker!r} found in FILTER_CHAIN"
    assert "scale" not in chain, "no scaling stage at all should remain in FILTER_CHAIN"


def test_no_upscale_scaffold_in_filtergraph():
    # The -filter_complex graph is built inside enhance_one; make sure the
    # module no longer defines or references the removed stages anywhere.
    src = open(enhance.__file__, encoding="utf-8").read()
    assert "UPSCALE =" not in src, "UPSCALE constant must not be redefined"
    assert "RESCALE_BACK" not in src, "RESCALE_BACK constant must not exist"
    assert "2*iw" not in src and "2*ih" not in src


def test_sharpeners_run_at_native_resolution():
    chain = enhance.FILTER_CHAIN
    # denoise -> LUT -> sharpeners -> deband -> format/setsar, in that order.
    assert chain.index("hqdn3d") < chain.index("haldclut") < chain.index("cas") < chain.index(
        "unsharp"
    ) < chain.index("gradfun")


def test_setsar_and_yuv420p_still_applied():
    chain = enhance.FILTER_CHAIN
    assert chain.endswith("format=yuv420p,setsar=1"), chain
    assert "setsar=1" in chain


def test_mobile_safe_contract_unchanged():
    assert enhance.TARGET_FPS == 30
    assert enhance.TARGET_PIX_FMT == "yuv420p"
    assert enhance.X264_PROFILE == "high"
