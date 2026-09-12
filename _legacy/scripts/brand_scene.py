#!/usr/bin/env python3
"""
Branded 9:16 compositor (Part 2 of the branding stack).

Given ONE scene MP4 (a legacy per-scene scene_XX.mp4, or the merged
final.mp4 produced by cut_and_produce.py — optionally enhanced by
enhance_scenes.py), a branding record, and a job title, this
script composites the scene into a professional-looking branded 10:9
vertical template and writes the result back as a mobile-safe MP4.

Design contract (mirrors the same "phone/WhatsApp/TikTok has to accept
this file" bar the cut/enhance stages already satisfy):

  * Canvas: 1080×1200 RGBA (10:9 near-square vertical), full-bleed.
  * Source clip: kept at its NATIVE aspect ratio. It is letterboxed /
    pillarboxed into a fixed 1080×608 slot inside the canvas — never
    stretched, never cropped. A 16:9 clip fills the slot exactly; a
    4:3 clip leaves black pillarboxes inside the slot; a taller clip
    leaves black letterboxes inside the slot. All three cases look
    intentional because the slot itself sits inside a designed frame.
  * Chrome: rendered ONCE per scene by brand_template.py as a
    transparent RGBA PNG that matches the canvas pixel-for-pixel with
    the slot area cut out (alpha=0). ffmpeg overlays this PNG on top of
    the letterboxed video, so the clip shows through the hole.
  * Encoding: identical to cut_and_produce.py — H.264 High@L4.0, yuv420p,
    CFR 30, no B-frames, CRF 23 + VBV ceiling, +faststart, mp42 brand,
    no edit lists, exactly one video + one audio stream. Audio is
    stream-copied from the source scene because branding is a purely
    visual pass — re-encoding the AAC would only add generation loss.
  * Validation: after writing the branded MP4, the same mobile-safe
    checks cut_and_produce.py enforces (exactly 1 video + 1 audio stream,
    yuv420p, has_b_frames=0, start_time ~0, valid MP4 ftyp) are re-run.
    A failure fails the whole invocation with exit 3 — a bad branded
    scene must never silently ship.

This script has NO knowledge of how the pipeline resolves branding or
where jobs live in the workflow. Wiring this into stage-b.yml is a
separate addition. All inputs are passed on the command line.

Usage:
    python brand_scene.py <in_scene.mp4> <out_branded.mp4> \\
        --title "Job title string" \\
        --username "channelhandle" \\
        --display-name "Display Name" \\
        --profile-picture path/to/avatar.png \\
        [--badge COMMENTARY] \\
        [--keep-overlay path/to/overlay.png]

Exit codes:
    0  success
    2  bad inputs (missing scene file, missing overlay dependencies)
    3  an ffmpeg / ffprobe / validation step failed
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Local — brand_template is a sibling module in scripts/.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from brand_template import Branding, build_template  # noqa: E402


# =============================================================================
# ENCODING PROFILE — MUST match cut_and_produce.py / enhance_scenes.py
# =============================================================================
# If any of these are changed, they must be changed in all three files
# together and the mobile-playback checks re-verified. See the docstring
# at the top of cut_and_produce.py for the full rationale on each value.
TARGET_FPS = 30
TARGET_PIX_FMT = "yuv420p"
X264_PROFILE = "high"
X264_LEVEL = "4.0"
X264_PRESET = "medium"
X264_CRF = "23"
X264_MAXRATE = "8M"
X264_BUFSIZE = "16M"

# 10:9 near-square vertical canvas — see brand_template.py for the rationale.
CANVAS_W = 1080
CANVAS_H = 1200


def sh(cmd: list[str]) -> None:
    """Run a command, streaming stdout/stderr live for debuggability."""
    print(f"$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True)


def probe_json(path: str) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries",
            "stream=codec_type,codec_name,profile,pix_fmt,width,height,"
            "has_b_frames,start_time,r_frame_rate,sample_rate,channels",
            "-show_entries", "format=format_name,duration,bit_rate,start_time",
            "-of", "json",
            path,
        ],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def probe_has_audio(path: str) -> bool:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            path,
        ],
        capture_output=True, text=True,
    )
    return "audio" in out.stdout


def composite(
    scene_path: str,
    overlay_png: str,
    out_path: str,
    slot_x: int,
    slot_y: int,
    slot_w: int,
    slot_h: int,
) -> None:
    """
    Build the one-shot ffmpeg filter graph that:
      1) scales the source clip so it fits INSIDE the slot at NATIVE AR
         (never stretched, never cropped) via
         `scale=slot_w:slot_h:force_original_aspect_ratio=decrease`;
      2) pads it to exactly slot_w×slot_h with black bars so the slot is
         always fully covered;
      3) pads that scaled+padded clip up to the full 1080×1200 canvas
         with the slot positioned at (slot_x, slot_y);
      4) overlays the pre-rendered branded chrome PNG on top.

    The overlay PNG has the slot area carved to fully transparent, so
    the clip shows through. Everything outside the slot is dominated by
    the overlay (background, header, title, CTA).

    Why this specific filter order:
      * `scale=…:force_original_aspect_ratio=decrease` guarantees the
        output NEVER exceeds slot_w×slot_h and NEVER changes AR — the
        exact requirement in the addition brief.
      * `pad=slot_w:slot_h:(slot_w-iw)/2:(slot_h-ih)/2:color=black`
        centres the scaled clip inside the slot with black
        letter/pillar bars WHERE NEEDED. On a 16:9 source these bars
        are zero pixels wide because scale hits slot dims exactly.
      * `pad=CANVAS_W:CANVAS_H:slot_x:slot_y:color=black` positions
        that slot-sized block at the correct canvas coordinates and
        fills the rest of the canvas with black. The overlay PNG then
        paints the branded chrome ON TOP of that black, so no black
        actually reaches the viewer outside the slot.
      * `overlay=0:0` composites the RGBA chrome at (0,0). Because the
        chrome PNG is the exact canvas size and the slot region has
        alpha=0, this is a single-pass alpha composite — hardware-decoder
        friendly and much cheaper than per-frame drawtext.

    Audio: stream-copied from the source scene. Branding is a purely
    visual pass, so re-encoding the AAC would only add generation loss.
    If the source has no audio (should not happen post-cut_and_produce.py, but
    guarded anyway) a silent stereo AAC track is synthesised so the
    branded MP4 remains a valid 2-stream A/V file.
    """
    has_audio = probe_has_audio(scene_path)

    # Filter graph: single [0:v] chain, no complex splits needed because
    # the overlay is a still image on input [1].
    filter_complex = (
        f"[0:v]"
        f"scale={slot_w}:{slot_h}:force_original_aspect_ratio=decrease,"
        f"pad={slot_w}:{slot_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"pad={CANVAS_W}:{CANVAS_H}:{slot_x}:{slot_y}:color=black,"
        f"setsar=1[bg];"
        f"[bg][1:v]overlay=0:0:format=auto,"
        f"fps={TARGET_FPS},format={TARGET_PIX_FMT},setsar=1[outv]"
    )

    cmd: list[str] = [
        "ffmpeg", "-y",
        "-i", scene_path,
        # Overlay PNG as a still image — `-loop 1` means the same PNG is
        # applied to every video frame instead of appearing once and then
        # disappearing.
        "-loop", "1",
        "-i", overlay_png,
    ]

    if not has_audio:
        # Guard rail — cut_and_produce.py always writes an audio stream, but
        # if somebody feeds this compositor a video-only file we still
        # produce a valid A/V MP4 rather than crashing ffmpeg's -map.
        cmd += [
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        ]

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[outv]",
    ]
    cmd += ["-map", "0:a:0"] if has_audio else ["-map", "2:a:0"]

    cmd += [
        # Video: same phone-safe H.264 profile as cut_and_produce.py.
        "-c:v", "libx264",
        "-profile:v", X264_PROFILE,
        "-level:v", X264_LEVEL,
        "-preset", X264_PRESET,
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

        # Audio: stream-copy from source (or re-encode the lavfi silent
        # track if we synthesised one). The `-c:a` and `-shortest` pair
        # below handle both cases.
    ]

    if has_audio:
        cmd += ["-c:a", "copy"]
    else:
        cmd += [
            "-c:a", "aac",
            "-profile:a", "aac_low",
            "-b:a", "128k",
            "-ar", "48000",
            "-ac", "2",
            "-shortest",
        ]

    # `-shortest` is also required to stop encoding when the FINITE video
    # input ends — otherwise the looped image input would run forever.
    # (Adding it twice is harmless.)
    cmd += ["-shortest"]

    cmd += [
        "-movflags", "+faststart",
        "-use_editlist", "0",
        "-brand", "mp42",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-sn", "-dn", "-ignore_unknown",
        "-fflags", "+genpts",
        "-max_muxing_queue_size", "9999",
        "-f", "mp4",
        out_path,
    ]

    sh(cmd)


def validate(path: str) -> None:
    """
    Re-enforce the exact mobile-safety bar from cut_and_produce.py. Any
    deviation fails with exit 3 so a bad branded scene never ships.
    """
    if not os.path.isfile(path):
        print(f"ERROR: {path} does not exist", file=sys.stderr)
        sys.exit(3)

    with open(path, "rb") as f:
        head = f.read(12)
    if len(head) < 12 or head[4:8] != b"ftyp":
        print(f"ERROR: {path} missing ftyp box", file=sys.stderr)
        sys.exit(3)

    data = probe_json(path)
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    v = [s for s in streams if s.get("codec_type") == "video"]
    a = [s for s in streams if s.get("codec_type") == "audio"]

    if len(v) != 1 or len(a) != 1 or len(streams) != 2:
        kinds = [f"{s.get('codec_type')}:{s.get('codec_name')}" for s in streams]
        print(
            f"ERROR: branded {path} must be 1 video + 1 audio, got {kinds}",
            file=sys.stderr,
        )
        sys.exit(3)

    vs = v[0]
    if vs.get("codec_name") != "h264":
        print(f"ERROR: video codec {vs.get('codec_name')!r}, expected h264",
              file=sys.stderr)
        sys.exit(3)
    if vs.get("pix_fmt") != TARGET_PIX_FMT:
        print(
            f"ERROR: pix_fmt {vs.get('pix_fmt')!r}, expected {TARGET_PIX_FMT!r}",
            file=sys.stderr,
        )
        sys.exit(3)
    if vs.get("has_b_frames") not in (0, "0"):
        print(f"ERROR: has_b_frames={vs.get('has_b_frames')!r}, expected 0",
              file=sys.stderr)
        sys.exit(3)
    if int(vs.get("width") or 0) != CANVAS_W or int(vs.get("height") or 0) != CANVAS_H:
        print(
            f"ERROR: branded video is {vs.get('width')}x{vs.get('height')}, "
            f"expected {CANVAS_W}x{CANVAS_H} (10:9 near-square vertical canvas)",
            file=sys.stderr,
        )
        sys.exit(3)

    try:
        if float(fmt.get("start_time", "0")) > 0.05:
            print(f"ERROR: container start_time={fmt.get('start_time')}",
                  file=sys.stderr)
            sys.exit(3)
        if float(vs.get("start_time", "0")) > 0.05:
            print(f"ERROR: video start_time={vs.get('start_time')}",
                  file=sys.stderr)
            sys.exit(3)
    except (TypeError, ValueError):
        pass

    print(f"  OK: {path} is a mobile-safe 10:9 branded MP4.", flush=True)


def brand_scene(
    scene_path: str,
    out_path: str,
    branding: Branding,
    title: str,
    badge: str,
    keep_overlay_path: str = "",
) -> None:
    """
    Full one-scene composite: render the chrome PNG, run the ffmpeg
    overlay pass, validate the output. `keep_overlay_path`, when set,
    writes the intermediate overlay PNG to that location for visual
    inspection (used by the built-in smoke test).
    """
    if not os.path.isfile(scene_path):
        print(f"ERROR: input scene not found: {scene_path}", file=sys.stderr)
        sys.exit(2)

    # 1. Render the branded chrome.
    print(
        f"Rendering branded chrome PNG "
        f"(title chars={len(title)}, username={branding.username!r}, "
        f"picture={'yes' if branding.profile_picture else 'no'})…",
        flush=True,
    )
    img, slot = build_template(branding, title, badge_text=badge)

    # 2. Persist it to a tmp path so ffmpeg can read it as a still input.
    #    (ffmpeg can't accept a Pillow Image directly.) The tmp file
    #    lives inside a NamedTemporaryFile so it's cleaned up on exit.
    with tempfile.TemporaryDirectory(prefix="clipforge_brand_") as td:
        overlay_png = os.path.join(td, "overlay.png")
        img.save(overlay_png, "PNG")
        print(f"  overlay -> {overlay_png} ({img.size[0]}×{img.size[1]})",
              flush=True)

        if keep_overlay_path:
            Path(keep_overlay_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(overlay_png, keep_overlay_path)
            print(f"  kept overlay copy at {keep_overlay_path}", flush=True)

        # 3. Composite.
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".",
                    exist_ok=True)
        composite(scene_path, overlay_png, out_path,
                  slot.x, slot.y, slot.w, slot.h)

    # 4. Validate — hard fail if the output isn't mobile-safe.
    validate(out_path)


def _cli() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Composite one cut scene MP4 into the branded 9:16 template "
            "using persistent channel branding + per-job title."
        )
    )
    ap.add_argument("in_scene",
                    help="Input scene MP4 — a legacy scene_XX.mp4 or the "
                         "merged final.mp4 (from cut_and_produce.py)")
    ap.add_argument("out_scene", help="Output branded MP4 path")
    ap.add_argument("--title", default="",
                    help="Per-job title (from production.json['title']).")
    ap.add_argument("--username", default="")
    ap.add_argument("--display-name", default="")
    ap.add_argument("--profile-picture", default="",
                    help="Local filesystem path to the channel avatar image.")
    ap.add_argument("--badge", default="COMMENTARY",
                    help="Category badge text shown at top-right.")
    ap.add_argument(
        "--keep-overlay",
        default="",
        help=(
            "If set, copy the intermediate branded chrome PNG to this path "
            "so it can be inspected visually. Not required in production."
        ),
    )
    args = ap.parse_args()

    branding = Branding(
        username=args.username,
        display_name=args.display_name,
        profile_picture=args.profile_picture,
    )
    brand_scene(
        scene_path=args.in_scene,
        out_path=args.out_scene,
        branding=branding,
        title=args.title,
        badge=args.badge,
        keep_overlay_path=args.keep_overlay,
    )


if __name__ == "__main__":
    _cli()
