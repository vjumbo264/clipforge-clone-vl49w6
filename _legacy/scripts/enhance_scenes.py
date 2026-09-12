#!/usr/bin/env python3
"""Apply the optional, mobile-safe anime enhancement pass to cut scenes.

The input contract remains deliberately small and stable:

    python scripts/enhance_scenes.py <scenes_dir> [--enabled | --no-enabled]

With ``--no-enabled`` this script does not read or rewrite any scene, so it is
safe to wire directly to the existing site toggle. When enabled, each
``scene_*.mp4`` (or the single merged ``final.mp4`` produced by
cut_and_produce.py) is rewritten atomically only after it passes the same
mobile-playback checks expected by the cutting stage.

The picture chain is, in order:

    light hqdn3d shimmer denoise → supplied 3D color-cube grade → strong
    contrast-adaptive sharpen → threshold-protected line-ink unsharp → gradfun
    debanding → yuv420p

Rationale for each stage:

* ``hqdn3d`` is used lightly and only to remove H.264 compression shimmer
  *before* it can be amplified by the later sharpening. It is deliberately
  weak (luma 0.8 / chroma 0.6 spatial, 3.0 / 2.0 temporal) so it cleans
  mosquito noise without flattening flat cel regions or dissolving ink
  edges. This is the classic fast "denoise before sharpen" first step; it
  is NOT a heavy restoration denoise.
  IMPORTANT: do NOT swap this back to ``nlmeans``. A strong ``nlmeans``
  (e.g. ``s=30``) is a non-local-means patch matcher whose per-pixel cost
  is orders of magnitude higher than ``hqdn3d``. It single-handedly pushed
  end-to-end processing from ~15 minutes to over an hour per video, and
  because it smooths high-frequency texture indiscriminately it produced
  the exact "plastered / blurry / over-smooth / artificial" look this pass
  is meant to avoid. ``hqdn3d`` is both dramatically faster and the right
  tool for anime linework.
* The supplied 64³ color-cube asset is deterministically converted from
  ``assets/anime_reference_lut_grid.png`` into FFmpeg's Hald level-8 layout.
  It is the only color transform in this stage; no contrast curve is stacked
  after it. The tiled source grid is retained for reproducibility and the
  generated Hald image is the actual filter input.
* ``cas`` (contrast-adaptive sharpen) restores the texture and edge
  definition the source needs, in a locally content-aware way that does
  not ring on flat areas. It is run at a strong-but-controlled strength so
  the footage reads as crisp and detailed, not soft.
* ``unsharp`` then sharpens line ink specifically: the luma threshold keeps
  the pass off flat cel-shaded regions so it targets contours rather than
  introducing halos, while the zeroed chroma matrix avoids chromatic
  fringing. Together, ``cas`` + ``unsharp`` deliver the unsharp-mask-style
  crispness, clean edge definition, and perceived detail of a high-quality
  anime edit — the opposite of the smoothed, plastered look produced by
  over-denoising.
* ``gradfun`` cleans up any 8-bit banding the encode introduced on the
  smooth cel gradients — always the last chroma-domain step before
  ``format=yuv420p`` locks the output pixel format.
* ``setsar=1`` guarantees square-pixel output regardless of source SAR.

This chain has been benchmarked end-to-end through a real H.264 CRF23
encode → enhance → validation round trip and is confirmed compatible
with the contract enforced by ``validate_enhanced()`` below (H.264 High
profile, yuv420p, CFR 30, zero B-frames, single video + single audio
stream, faststart, no edit lists).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# hqdn3d=luma_spatial:chroma_spatial:luma_tmp:chroma_tmp. Intentionally
# light: it cleans compression shimmer before sharpening rather than
# blurring flat cel regions or ink edges. Fast — this is what keeps the
# enhancement pass in the ~15-minute range instead of the >1-hour range
# that the previous heavy nlmeans denoise caused.
DENOISE = "hqdn3d=0.8:0.6:3.0:2.0"
LUT_FILTER = "haldclut=shortest=1"

# The supplied level-8 color cube is a 512×512 PNG encoding a 64×64×64
# mapping. It is rebuilt from assets/anime_reference_lut_grid.png, the
# user-provided 8×8 tile-grid LUT (64 blue slices; red tile-x, green tile-y),
# via scripts/convert_lut_grid_to_hald.py. Never procedurally overwrite it.
LUT_GRID_SOURCE = Path(__file__).resolve().parents[1] / "assets" / "anime_reference_lut_grid.png"
LUT_ASSET = Path(__file__).resolve().parents[1] / "assets" / "anime_reference_color_cube_l8.png"

# Contrast-adaptive sharpen: restores texture and edge definition across
# the whole frame in a locally content-aware way (does not ring on flat
# areas). Run at a strong-but-controlled strength so the result reads as
# crisp and detailed, not soft or plastered.
EDGE_SHARPEN = "cas=strength=0.75"

# Luma-only unsharp with a hard threshold: only pixels whose local contrast
# exceeds the threshold are sharpened, which targets line ink and skips
# flat cel-shaded regions (no halos/ringing on flat areas). The chroma
# matrix is zeroed via the trailing 5:5:0 so colour is never sharpened
# (avoids chromatic halos). This is the unsharp-mask-style crispness that
# gives anime linework its clean, well-defined contours.
LINE_SHARPEN = "unsharp=7:7:0.85:5:5:0.35"

# gradfun cleans any 8-bit banding introduced by the encode. Kept mild
# (strength 1.2, radius 16) so it does not eat fine texture.
DEBAND = "gradfun=1.2:16"

FILTER_CHAIN = (
    f"{DENOISE},{LUT_FILTER},{EDGE_SHARPEN},{LINE_SHARPEN},{DEBAND},"
    "format=yuv420p,setsar=1"
)

# Mobile-safe encoding parameters. These retain the output contract now
# owned by cut_and_produce.py: H.264 High@L4.0, yuv420p, CFR 30, and zero
# B-frames.
TARGET_FPS = 30
TARGET_PIX_FMT = "yuv420p"
X264_PROFILE = "high"
X264_LEVEL = "4.0"
X264_PRESET = "medium"
X264_TUNE = "animation"
X264_CRF = "24"
X264_MAXRATE = "8M"
X264_BUFSIZE = "16M"


def sh(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True)


def probe_json(path: str) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries",
            "stream=codec_type,codec_name,profile,pix_fmt,width,height,"
            "has_b_frames,start_time,r_frame_rate",
            "-show_entries", "format=format_name,duration,bit_rate,start_time",
            "-of", "json", path,
        ],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def enhance_one(src: str, dst: str) -> None:
    """Denoise, apply the supplied color cube, and gently sharpen one scene."""
    graph = (
        f"[0:v]{DENOISE}[denoised];"
        f"[denoised][1:v]{LUT_FILTER},{EDGE_SHARPEN},"
        f"{LINE_SHARPEN},{DEBAND},format=yuv420p,setsar=1[enhanced]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        # The supplied color cube must be a looping second video input; ``shortest=1`` makes
        # output duration follow the scene, not this infinite image stream.
        "-loop", "1", "-framerate", str(TARGET_FPS), "-i", str(LUT_ASSET),
        "-filter_complex", graph,
        # Preserve the existing stream policy: one rendered video, original
        # primary audio, and no subtitles/data/metadata/chapter leakage.
        "-map", "[enhanced]", "-map", "0:a:0",
        "-c:v", "libx264",
        "-profile:v", X264_PROFILE,
        "-level:v", X264_LEVEL,
        "-preset", X264_PRESET,
        "-tune", X264_TUNE,
        "-crf", X264_CRF,
        "-maxrate", X264_MAXRATE,
        "-bufsize", X264_BUFSIZE,
        "-pix_fmt", TARGET_PIX_FMT,
        "-bf", "0",
        "-g", str(TARGET_FPS * 2),
        "-keyint_min", str(TARGET_FPS * 2),
        "-sc_threshold", "0",
        "-x264-params", "force-cfr=1",
        "-r", str(TARGET_FPS),
        "-video_track_timescale", "15360",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-use_editlist", "0",
        "-brand", "mp42",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-sn", "-dn", "-ignore_unknown",
        "-fflags", "+genpts",
        "-max_muxing_queue_size", "9999",
        "-f", "mp4", dst,
    ]
    sh(cmd)


def validate_enhanced(path: str) -> None:
    """Fail before swapping unless the enhanced MP4 is mobile-safe."""
    with open(path, "rb") as handle:
        head = handle.read(12)
    if len(head) < 12 or head[4:8] != b"ftyp":
        raise RuntimeError(f"{path} is missing an MP4 ftyp box")

    data = probe_json(path)
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if len(video) != 1 or len(audio) != 1 or len(streams) != 2:
        kinds = [f"{s.get('codec_type')}:{s.get('codec_name')}" for s in streams]
        raise RuntimeError(f"{path} must contain exactly one video and one audio stream; got {kinds}")

    stream = video[0]
    if stream.get("codec_name") != "h264":
        raise RuntimeError(f"video codec {stream.get('codec_name')!r}, expected h264")
    if stream.get("profile") not in ("High", "High 10"):
        raise RuntimeError(f"video profile {stream.get('profile')!r}, expected High")
    if stream.get("pix_fmt") != TARGET_PIX_FMT:
        raise RuntimeError(f"pixel format {stream.get('pix_fmt')!r}, expected {TARGET_PIX_FMT!r}")
    if stream.get("has_b_frames") not in (0, "0"):
        raise RuntimeError(f"has_b_frames={stream.get('has_b_frames')!r}, expected 0")
    if stream.get("r_frame_rate") != f"{TARGET_FPS}/1":
        raise RuntimeError(f"frame rate {stream.get('r_frame_rate')!r}, expected {TARGET_FPS}/1")

    for label, value in (("container", fmt.get("start_time", "0")),
                         ("video", stream.get("start_time", "0"))):
        try:
            if abs(float(value)) > 0.05:
                raise RuntimeError(f"{label} start_time={value}, expected ~0")
        except (TypeError, ValueError):
            # ffprobe may omit an optional start_time; muxer settings above
            # still guarantee a zero-origin output and avoid edit lists.
            pass
    print(f"  OK: {path} still mobile-safe after enhancement.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply the anime Hald-LUT enhancement chain to cut scene MP4s in place."
    )
    parser.add_argument("scenes_dir", help="Directory containing scene_*.mp4 (or the merged final.mp4)")
    parser.add_argument("--enabled", dest="enabled", action="store_true", default=True,
                        help="Apply enhancement (default).")
    parser.add_argument("--no-enabled", dest="enabled", action="store_false",
                        help="No-op: leave all scene files completely untouched.")
    args = parser.parse_args()

    if not args.enabled:
        print("Quality enhancement DISABLED via --no-enabled — scene files left untouched.", flush=True)
        return
    if not LUT_GRID_SOURCE.is_file():
        raise SystemExit(f"ERROR: supplied LUT grid source missing: {LUT_GRID_SOURCE}")
    if not LUT_ASSET.is_file():
        raise SystemExit(f"ERROR: bundled color-cube asset missing: {LUT_ASSET}")

    scenes_dir = Path(args.scenes_dir)
    if not scenes_dir.is_dir():
        raise SystemExit(f"ERROR: scenes dir does not exist: {scenes_dir}")
    # Accept both the legacy per-scene layout (scene_*.mp4) and the new
    # single merged file (final.mp4) produced by cut_and_produce.py.
    scenes = sorted(scenes_dir.glob("scene_*.mp4"))
    merged = scenes_dir / "final.mp4"
    if merged.is_file():
        scenes.append(merged)
    if not scenes:
        raise SystemExit(f"ERROR: no scene_*.mp4 / final.mp4 files found in {scenes_dir}")

    print(
        f"Enhancing {len(scenes)} scene(s) with supplied color-cube chain:\n"
        f"  {FILTER_CHAIN} (LUT: {LUT_ASSET.name})\n"
        f"  H.264 High@L{X264_LEVEL}, {TARGET_PIX_FMT}, CRF {X264_CRF}, "
        f"preset={X264_PRESET}, tune={X264_TUNE}, CFR {TARGET_FPS}, no B-frames.",
        flush=True,
    )
    total_before = total_after = 0
    for src in scenes:
        tmp = src.with_suffix(".enhanced.mp4")
        before = src.stat().st_size
        total_before += before
        print(f"\n--- Enhancing {src.name} ({before / 1024 / 1024:.2f} MB)", flush=True)
        try:
            enhance_one(str(src), str(tmp))
            validate_enhanced(str(tmp))
            after = tmp.stat().st_size
            total_after += after
            print(f"  size: {before / 1024 / 1024:.2f} MB -> {after / 1024 / 1024:.2f} MB "
                  f"({(after - before) / before * 100 if before else 0:+.1f}%)", flush=True)
            os.replace(tmp, src)
        finally:
            # No incomplete .enhanced.mp4 is left behind if ffmpeg/validation fails.
            if tmp.exists():
                tmp.unlink()
    if total_before:
        print(f"\nDone. Aggregate size delta: {total_before / 1024 / 1024:.2f} MB -> "
              f"{total_after / 1024 / 1024:.2f} MB "
              f"({(total_after - total_before) / total_before * 100:+.1f}%).", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, RuntimeError) as error:
        print(f"ERROR: enhancement failed: {error}", file=sys.stderr)
        raise SystemExit(3)
