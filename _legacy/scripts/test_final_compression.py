#!/usr/bin/env python3
"""Integration smoke test for the final delivery compression pass."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compress_final_video as delivery  # noqa: E402

WORK = "/tmp/clipforge_delivery_compression_test"
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK, exist_ok=True)
source = os.path.join(WORK, "fully_composited_source.mp4")
delivery_out = os.path.join(WORK, "delivery.mp4")

# This stands in for the already-composited final: H.264 CRF 18 video with an
# AAC audio track, 1080x1200 dimensions, and a fixed frame rate.
subprocess.run([
    "ffmpeg", "-y", "-loglevel", "error",
    "-f", "lavfi", "-i", "testsrc2=size=1080x1200:rate=24:duration=6",
    "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=6",
    "-map", "0:v:0", "-map", "1:a:0",
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
    "-pix_fmt", "yuv420p", "-bf", "0", "-g", "60", "-keyint_min", "60",
    "-sc_threshold", "0", "-x264-params", "force-cfr=1",
    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", source,
], check=True)

before, after, reduced = delivery.compress(
    delivery.Path(source).resolve(), delivery.Path(delivery_out).resolve())
assert reduced, f"expected a smaller delivery output, got {before} -> {after} bytes"
assert after < before, (before, after)

probe = json.loads(subprocess.check_output([
    "ffprobe", "-v", "error", "-show_entries",
    "stream=codec_type,codec_name,width,height,r_frame_rate,sample_aspect_ratio:format=duration",
    "-of", "json", delivery_out,
], text=True))
video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
audio = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
assert (video["width"], video["height"]) == (1080, 1200), video
assert video["r_frame_rate"] == "24/1", video
assert audio["codec_name"] == "aac", audio
assert abs(float(probe["format"]["duration"]) - 6.0) < 0.1, probe
print(f"PASS: final delivery compression {before:,} -> {after:,} bytes "
      f"({(1 - after / before) * 100:.1f}% reduction), "
      "1080x1200/24fps/AAC/duration preserved")
