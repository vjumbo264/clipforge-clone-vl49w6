#!/usr/bin/env python3
"""
scene_index.py — Detect shot boundaries in the compressed analysis copy
and emit `scene_index.json`.

Purpose
-------
The downstream vision agent currently has no reliable way to know WHERE
in the source video the camera actually cuts. It has to compare panels
across composite screenshots and guess — which is exactly where scene
detection and character re-identification go wrong.

This script runs a single ffmpeg pass with the built-in `select='gt(scene,T)'`
filter over the ALREADY-COMPRESSED 720p analysis copy (so no extra
decode cost from the original), extracts the timestamp of each detected
shot change, and writes a compact JSON index. Zero AI-vision tokens
are spent; this is pure local CPU work using only ffmpeg (which the
Stage A workflow already installs).

Output shape (`scene_index.json`)
---------------------------------
{
  "video_duration_seconds": 1416,
  "threshold": 0.35,
  "shot_count": 47,
  "shots": [
    {
      "shot_id": 1,
      "start_seconds": 0.0,
      "end_seconds": 12.3,
      "keyframe_seconds": 6.0,
      "cause": "video_start"
    },
    {
      "shot_id": 2,
      "start_seconds": 12.3,
      "end_seconds": 28.9,
      "keyframe_seconds": 20.5,
      "cause": "cut"
    },
    ...
  ]
}

Cost profile
------------
- One ffmpeg pass on the 720p compressed copy: seconds of CPU.
- No network, no model download, no AI-vision tokens.
- Output is a small JSON file (~a few KB even for long videos).

Usage
-----
    python scene_index.py <compressed_video> <duration_seconds> <output_json>
                          [--threshold 0.35]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys


SCENE_LINE_RE = re.compile(
    r"pts_time:(?P<t>[0-9]+(?:\.[0-9]+)?)"
)


def detect_shots(video_path: str, threshold: float) -> list[float]:
    """
    Return the list of shot-change timestamps (in seconds) detected by
    ffmpeg's built-in scene-change score.

    We run the filter chain `select='gt(scene,T)',showinfo` and parse the
    `pts_time:` fields from ffmpeg's stderr. Only frames whose scene
    score exceeds the threshold are kept, so the parsed timestamps are
    exactly the shot-change points.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i", video_path,
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null",
        "-",
    ]
    print(f"$ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # ffmpeg still exits 0 on a successful null-mux; a real failure is unusual.
        print(proc.stderr[-2000:], file=sys.stderr)
        raise RuntimeError(f"ffmpeg scene-detect failed (exit {proc.returncode})")

    times: list[float] = []
    for line in proc.stderr.splitlines():
        m = SCENE_LINE_RE.search(line)
        if m:
            try:
                times.append(float(m.group("t")))
            except ValueError:
                pass

    # Sort + deduplicate (ffmpeg sometimes double-reports on frame boundaries).
    uniq: list[float] = []
    for t in sorted(times):
        if not uniq or (t - uniq[-1]) > 0.05:
            uniq.append(t)
    return uniq


def build_shot_index(cut_times: list[float], total_duration: float) -> list[dict]:
    """
    Turn a sorted list of cut timestamps into a list of shot dicts covering
    [0, total_duration]. Each shot's `keyframe_seconds` is its midpoint —
    a stable single-representative-frame anchor that character indexing
    and the vision agent can point at.
    """
    boundaries = [0.0] + [t for t in cut_times if 0.0 < t < total_duration]
    boundaries.append(float(total_duration))

    shots: list[dict] = []
    for i in range(len(boundaries) - 1):
        s = round(boundaries[i], 3)
        e = round(boundaries[i + 1], 3)
        if e - s < 0.25:
            # Tiny sub-quarter-second flickers are almost always
            # false-positives (a flash, a subtitle pop-in). Fold them
            # into the previous shot.
            if shots:
                shots[-1]["end_seconds"] = e
                shots[-1]["keyframe_seconds"] = round(
                    (shots[-1]["start_seconds"] + e) / 2.0, 3
                )
            continue
        shots.append(
            {
                "shot_id": len(shots) + 1,
                "start_seconds": s,
                "end_seconds": e,
                "keyframe_seconds": round((s + e) / 2.0, 3),
                "cause": "video_start" if i == 0 else "cut",
            }
        )
    return shots


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video_path")
    ap.add_argument("duration_seconds", type=float)
    ap.add_argument("output_json")
    ap.add_argument(
        "--threshold",
        type=float,
        default=0.35,
        help="Scene-score threshold (0..1). 0.35 catches typical narrative "
             "cuts without spamming on high-motion within a single shot.",
    )
    args = ap.parse_args()

    if not os.path.exists(args.video_path):
        print(f"Input not found: {args.video_path}", file=sys.stderr)
        sys.exit(2)

    cut_times = detect_shots(args.video_path, args.threshold)
    print(f"Detected {len(cut_times)} raw scene-change candidate(s)", flush=True)

    shots = build_shot_index(cut_times, args.duration_seconds)
    print(f"Consolidated into {len(shots)} shot(s)", flush=True)

    payload = {
        "video_duration_seconds": float(args.duration_seconds),
        "threshold": args.threshold,
        "shot_count": len(shots),
        "shots": shots,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.output_json)) or ".", exist_ok=True)
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Wrote scene index to {args.output_json}", flush=True)


if __name__ == "__main__":
    main()
