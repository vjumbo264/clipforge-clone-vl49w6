#!/usr/bin/env python3
"""
event_frames.py — Emit DENSE composite screenshots for only the moments
that key_moments.json flagged as high-signal.

Why this exists
---------------
The baseline screenshot cadence (one 3x2 composite per 6 seconds of
source, one panel per second) is fine for slow / talking-head footage
but too coarse for fast beats — a cut or a punch lands in a single
frame that the baseline may or may not sample. The downstream vision
agent then either misses the beat entirely or has to guess from a
single ambiguous panel.

Rather than doubling the baseline (which would double vision-token
cost across the whole video), we spend the extra frame budget ONLY on
the moments we already know are important — the ones on the
`key_moments.json` shortlist that have `is_shot_boundary=true` OR a
priority above the configured minimum.

For each such moment we emit TWO extra 3x2 composites:

  1. START composite — 3x2 tile of a 4-second window centered on the
     moment's `start_seconds`. Captures the shot boundary itself and
     the entry into the described beat.

  2. TAIL composite — 3x2 tile of a 4-second window centered on the
     moment's `end_seconds` (which by build_key_moments.py's
     visual-tail policy already sits a bit PAST the raw shot
     boundary). This is the fix for a real recurring bug: cuts were
     ending before the described on-screen action actually finished
     happening. The baseline cadence gives the agent 1 fps of visual
     info at the END of a candidate scene and none across the
     boundary — so it could not verify whether the described payoff
     (reaction shot, cutaway, result of the action) had actually
     landed before its chosen `end_seconds`. Sampling densely across
     the tail restores symmetry: the agent now sees ~1.5 fps at BOTH
     ends of every candidate, and can push `end_seconds` out until
     the described action is visibly complete.

Each composite is a 3x2 tile sampled at ~1.5 fps across a 4-second
window (consecutive panels ~0.66s apart), same layout the agent
already understands from the baseline.

Total extra frames are ~= 2 * (# high-signal moments) which for a
typical video is <20% of the baseline frame count. Cost stays flat,
resolution at the important moments jumps ~4x AND now covers the
action's visual tail, not just its onset.

Output filenames are:

    event_<center_ms>.jpg     # zero-padded to 9 digits: event_000042500.jpg

The number is the CENTER of the window in milliseconds. Start and tail
composites are distinguished purely by their timestamp — a moment
with start_seconds=42.5 and end_seconds=61.5 emits event_000042500.jpg
and event_000061500.jpg. Files placed alongside the baseline
`frame_NNNNNN.jpg` files in the same screenshots directory. The agent
is told about them in 00_READ_THIS_FIRST.txt.

Usage
-----
    python event_frames.py <compressed_video> <key_moments_json>
                           <screenshots_out_dir>
                           [--window-seconds 4]
                           [--samples 6]
                           [--panel-width 640]
                           [--max-events 30]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


def emit_event_composite(
    video_path: str,
    center_seconds: float,
    dst_path: str,
    window_seconds: float,
    samples: int,
    panel_width: int,
) -> bool:
    """
    Extract `samples` frames evenly spaced across a `window_seconds`
    window centered on `center_seconds`, tile them into a 3x2 composite,
    and write to `dst_path`. Returns True on success.
    """
    half = window_seconds / 2.0
    start = max(0.0, center_seconds - half)
    # samples evenly spaced across the window
    fps = samples / window_seconds

    # 3x2 tile requires exactly 6 input frames per tile. If the user
    # requests a non-multiple-of-6 sample count we still tile 3x2 and
    # drop the excess — this keeps layout consistent with the baseline.
    tile_layout = "3x2"

    vf = (
        f"fps={fps:.6f},"
        f"scale='min({panel_width},iw)':'-2',"
        f"tile={tile_layout}:padding=2:margin=2:color=black"
    )
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-t", f"{window_seconds:.3f}",
        "-i", video_path,
        "-vf", vf,
        "-frames:v", "1",
        "-qscale:v", "5",
        dst_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0
    except subprocess.CalledProcessError as e:
        print(
            f"  event-composite ffmpeg failed @ {center_seconds:.2f}s: "
            f"{e.stderr.decode(errors='replace')[-400:]}",
            file=sys.stderr,
        )
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("compressed_video")
    ap.add_argument("key_moments_json")
    ap.add_argument("screenshots_out_dir")
    ap.add_argument("--window-seconds", type=float, default=4.0)
    ap.add_argument("--samples", type=int, default=6)
    ap.add_argument("--panel-width", type=int, default=640)
    ap.add_argument(
        "--max-events",
        type=int,
        default=30,
        help="Hard cap on the number of high-signal MOMENTS we emit dense "
             "composites for. Each selected moment produces up to two "
             "composites (start and tail), so the actual file count is up "
             "to 2x this cap.",
    )
    ap.add_argument(
        "--min-priority",
        type=float,
        default=0.35,
        help="Only moments with priority >= this get a dense composite.",
    )
    ap.add_argument(
        "--tail-composites",
        default="true",
        help="If 'true' (default), also emit an event composite centered on "
             "each moment's end_seconds so the agent has dense visual "
             "coverage of the described beat's on-screen resolution, not "
             "just its onset. Set to 'false' to emit start composites only.",
    )
    args = ap.parse_args()
    emit_tails = str(args.tail_composites).strip().lower() not in ("false", "0", "no", "off")

    if not os.path.exists(args.compressed_video):
        print(f"Input video not found: {args.compressed_video}", file=sys.stderr)
        sys.exit(2)
    if not os.path.exists(args.key_moments_json):
        # No key moments file -> nothing to do, but this is not an error.
        print(
            f"No key_moments file at {args.key_moments_json}; "
            f"no event composites will be emitted.",
            flush=True,
        )
        return

    with open(args.key_moments_json, "r", encoding="utf-8") as f:
        km = json.load(f)

    moments = km.get("moments", [])
    if not moments:
        print("key_moments has no moments; nothing to do.", flush=True)
        return

    # Select high-signal moments. Priority-ranked, then chronological.
    high = [
        m for m in moments
        if m.get("signals", {}).get("is_shot_boundary")
        or m.get("signals", {}).get("priority", 0.0) >= args.min_priority
    ]
    high.sort(key=lambda m: m["signals"].get("priority", 0.0), reverse=True)
    high = high[: args.max_events]
    high.sort(key=lambda m: m["start_seconds"])

    os.makedirs(args.screenshots_out_dir, exist_ok=True)

    # Deduplicate composite CENTERS across all moments — adjacent moments
    # frequently share a boundary (moment N's end ≈ moment N+1's start),
    # and we don't want to render the same window twice.
    emitted_centers_ms: set[int] = set()

    def _emit_one(center: float, label: str) -> bool:
        """
        Render a single 4-second event composite centered on `center` (in
        source-video seconds). Returns True if a NEW file was written
        (False if the center collides with something we already emitted
        or if ffmpeg failed).
        """
        if center < 0.0:
            return False
        # Round to whole milliseconds for the filename AND for dedup.
        center_ms = int(round(center * 1000))
        if center_ms in emitted_centers_ms:
            return False
        # Also treat centers within ~0.5s of an already-emitted center as
        # duplicates — they render essentially identical 4-second windows.
        for existing in emitted_centers_ms:
            if abs(existing - center_ms) < 500:
                return False

        fname = f"event_{center_ms:09d}.jpg"
        dst = os.path.join(args.screenshots_out_dir, fname)
        ok = emit_event_composite(
            args.compressed_video,
            center,
            dst,
            args.window_seconds,
            args.samples,
            args.panel_width,
        )
        if ok:
            emitted_centers_ms.add(center_ms)
            print(f"  event composite [{label}] @ {center:.2f}s -> {fname}", flush=True)
            return True
        return False

    written = 0
    for m in high:
        start_c = float(m["start_seconds"])
        if _emit_one(start_c, "start"):
            written += 1

        if not emit_tails:
            continue

        # Tail composite: center on end_seconds, which by
        # build_key_moments.py's visual-tail policy already sits a bit
        # PAST the raw shot boundary. If the moment for some reason has
        # no distinct end (very short shot, missing field), fall back to
        # shot_end_seconds and finally to a small pad past start.
        end_c = m.get("end_seconds")
        if end_c is None:
            end_c = m.get("shot_end_seconds", start_c + 3.0)
        end_c = float(end_c)
        # If end is essentially the same as start (a sub-second shot),
        # skip — the start composite already covers it.
        if end_c - start_c < args.window_seconds / 2.0:
            continue
        if _emit_one(end_c, "tail"):
            written += 1

    print(f"Wrote {written} event composite(s) to {args.screenshots_out_dir}", flush=True)


if __name__ == "__main__":
    main()
