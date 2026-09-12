#!/usr/bin/env python3
"""Final delivery compression for an already-rendered ClipForge MP4.

This is deliberately a separate terminal pass: it changes neither captions,
banners, crops, timing, nor audio. It applies CRF-based x264 compression to the
fully composited video, stream-copies the audio, verifies the output preserves
core media properties, and writes measured before/after size information.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# CRF 23 is the repository's established delivery-quality setting (also used by
# the prior branded and enhancement encodes). It is materially leaner than the
# CRF 18 final compositor while remaining visually clean for mobile/social use.
DELIVERY_CRF = "23"
DELIVERY_PRESET = "medium"
X264_PROFILE = "high"
X264_LEVEL = "4.0"


def _run(command: list[str]) -> None:
    print("$ " + " ".join(command), flush=True)
    subprocess.run(command, check=True)


def _probe(path: Path) -> dict:
    raw = subprocess.check_output(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration,size:stream=index,codec_type,codec_name,width,height,"
            "r_frame_rate,avg_frame_rate,sample_aspect_ratio,time_base",
            "-of", "json", str(path),
        ],
        text=True,
    )
    return json.loads(raw)


def _stream(probe: dict, stream_type: str) -> dict:
    matches = [stream for stream in probe.get("streams", [])
               if stream.get("codec_type") == stream_type]
    if not matches:
        raise RuntimeError(f"{stream_type} stream missing")
    return matches[0]


def _size_label(size_bytes: int) -> str:
    return f"{size_bytes / (1024 * 1024):.2f} MiB ({size_bytes:,} bytes)"


def _duration(probe: dict) -> float:
    return float((probe.get("format") or {}).get("duration") or 0.0)


def _validate(source: Path, candidate: Path) -> None:
    before = _probe(source)
    after = _probe(candidate)
    before_video = _stream(before, "video")
    after_video = _stream(after, "video")

    # r_frame_rate is the exact nominal playback rate. MP4 may represent
    # avg_frame_rate with a slightly different rational after remuxing even
    # when every frame’s cadence is unchanged, so it is logged but not used as
    # a byte-for-byte invariant.
    invariant_fields = ("width", "height", "r_frame_rate", "sample_aspect_ratio")
    mismatches = [
        f"{field}: {before_video.get(field)} -> {after_video.get(field)}"
        for field in invariant_fields
        if before_video.get(field) != after_video.get(field)
    ]
    if mismatches:
        raise RuntimeError("Final compression changed video invariants: " +
                           "; ".join(mismatches))

    before_duration = _duration(before)
    after_duration = _duration(after)
    if abs(before_duration - after_duration) > 0.10:
        raise RuntimeError(
            "Final compression changed duration: "
            f"{before_duration:.3f}s -> {after_duration:.3f}s"
        )

    source_audio = [stream for stream in before.get("streams", [])
                    if stream.get("codec_type") == "audio"]
    candidate_audio = [stream for stream in after.get("streams", [])
                       if stream.get("codec_type") == "audio"]
    if len(source_audio) != len(candidate_audio):
        raise RuntimeError(
            "Final compression changed audio stream count: "
            f"{len(source_audio)} -> {len(candidate_audio)}"
        )
    for index, (original, copied) in enumerate(zip(source_audio, candidate_audio), 1):
        if original.get("codec_name") != copied.get("codec_name"):
            raise RuntimeError(
                f"Final compression re-encoded audio stream {index}: "
                f"{original.get('codec_name')} -> {copied.get('codec_name')}"
            )


def compress(source: Path, destination: Path) -> tuple[int, int, bool]:
    """Write a quality-compressed delivery MP4 and return size metrics.

    If a pathological source does not shrink under constant-quality encoding,
    the source is copied to the requested destination rather than making the
    delivered asset larger. Either result is fully probe-validated and logged.
    """
    if not source.is_file():
        raise FileNotFoundError(f"Final video not found: {source}")
    if source.resolve() == destination.resolve():
        raise ValueError("Source and destination must be different paths")

    destination.parent.mkdir(parents=True, exist_ok=True)
    candidate = destination.with_name(destination.name + ".compressing.mp4")
    candidate.unlink(missing_ok=True)
    destination.unlink(missing_ok=True)
    before_bytes = source.stat().st_size

    command = [
        "ffmpeg", "-y", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-profile:v", X264_PROFILE,
        "-level:v", X264_LEVEL, "-preset", DELIVERY_PRESET,
        "-crf", DELIVERY_CRF, "-pix_fmt", "yuv420p",
        # Preserve ClipForge's mobile-safe no-B-frame final-video contract.
        "-bf", "0", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-x264-params", "force-cfr=1", "-video_track_timescale", "15360",
        # Audio is copied exactly; no additional generation loss or size churn.
        "-c:a", "copy",
        "-movflags", "+faststart", "-use_editlist", "0", "-brand", "mp42",
        "-map_metadata", "-1", "-map_chapters", "-1", "-sn", "-dn",
        "-ignore_unknown", "-fflags", "+genpts", "-max_muxing_queue_size", "9999",
        "-f", "mp4", str(candidate),
    ]
    _run(command)
    _validate(source, candidate)

    candidate_bytes = candidate.stat().st_size
    reduced = candidate_bytes < before_bytes
    if reduced:
        os.replace(candidate, destination)
    else:
        print(
            "WARNING: CRF delivery candidate was not smaller; preserving the "
            "original completed asset rather than increasing delivery size.",
            flush=True,
        )
        shutil.copy2(source, destination)

    final_bytes = destination.stat().st_size
    reduction = (1.0 - (final_bytes / before_bytes)) * 100.0 if before_bytes else 0.0
    print("Final delivery compression complete:", flush=True)
    print(f"  before: {_size_label(before_bytes)}", flush=True)
    print(f"  candidate: {_size_label(candidate_bytes)}", flush=True)
    print(f"  after: {_size_label(final_bytes)}", flush=True)
    print(f"  reduction: {reduction:.1f}% ({'compressed' if reduced else 'original retained'})",
          flush=True)
    return before_bytes, final_bytes, reduced


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: compress_final_video.py <input_final.mp4> <output_final.mp4>",
              file=sys.stderr)
        raise SystemExit(2)
    compress(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())


if __name__ == "__main__":
    main()
