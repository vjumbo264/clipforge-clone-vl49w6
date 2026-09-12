"""Regression tests for the "wrong scene playing for a cut" initiative.

Two layers of protection, matching the two things that must hold for a cut to
show the footage its narration describes:

1. ``FrameSelectionFidelityTests`` (integration, ffmpeg-gated) — the
   constructed test case from the investigation. It builds a synthetic source
   whose every one-second span is a DIFFERENT solid color (second N == a known
   RGB), runs a cut through Stage B's real ``render_merged``, and asserts the
   OUTPUT frames carry the color of the REQUESTED source seconds — proving the
   ``-ss <start> -to <end> -i <src>`` extraction + setpts retiming select the
   EXACT source frames, not a neighboring/mismatched segment. This would catch
   any timeline-reference mismatch between scene detection / transcript and
   Stage B extraction. It currently PASSES (the timelines are consistent) and
   is kept as a permanent guard against regression.

2. ``DeclaredDurationGuardTests`` (pure logic) — proves the root-cause fix:
   Stage B now refuses to render when the plan's declared
   ``video_duration_seconds`` does not match the source file's real probed
   duration, instead of silently interpreting every cut's seconds against the
   wrong-length timeline.

Run:  python -m unittest pipeline.tests.test_scene_accuracy -v
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.stage_b import common, render  # noqa: E402

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

try:
    from PIL import Image
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

# Distinct, well-separated colors per source second (index = source second).
SECOND_COLORS = [
    (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0),
    (255, 0, 255), (0, 255, 255), (128, 0, 0), (0, 128, 0),
    (0, 0, 128), (128, 128, 0), (128, 0, 128), (0, 128, 128),
]


def _valid_plan_document(**overrides) -> dict:
    doc = {
        "version": 2,
        "job_id": "scene-accuracy-test",
        "title": "Scene Accuracy",
        "video_duration_seconds": 300,
        "target_total_duration_seconds": 60,
        "cuts": [
            {"start_seconds": 10, "end_seconds": 40, "voiceover_text": "Narration for the first cut."},
        ],
        "hashtags": [f"#t{i}" for i in range(5)],
        "youtube_tags": [f"tag{i}" for i in range(10)],
    }
    doc.update(overrides)
    return doc


class DeclaredDurationGuardTests(unittest.TestCase):
    """The root-cause fix: declared-vs-actual source-duration sanity check."""

    def test_matching_duration_passes(self) -> None:
        plan = _valid_plan_document(video_duration_seconds=300)
        render.assert_declared_duration_matches_source(plan, 300.4)

    def test_within_tolerance_rounding_passes(self) -> None:
        # Declared is a rounded integer; a sub-2s probe difference is benign.
        plan = _valid_plan_document(video_duration_seconds=300)
        render.assert_declared_duration_matches_source(plan, 301.9)

    def test_stale_or_wrong_duration_fails_loudly(self) -> None:
        # Plan written against a 9283s source, but the file present at render
        # time is a different (shorter) cut -> every cut would land wrong.
        plan = _valid_plan_document(video_duration_seconds=9283)
        with self.assertRaises(common.StageBError) as ctx:
            render.assert_declared_duration_matches_source(plan, 1379.0)
        message = str(ctx.exception)
        self.assertIn("9283", message)
        self.assertIn("1379.00", message)
        self.assertIn("wrong scene", message.lower())

    def test_shorter_declared_than_actual_fails(self) -> None:
        plan = _valid_plan_document(video_duration_seconds=100)
        with self.assertRaises(common.StageBError):
            render.assert_declared_duration_matches_source(plan, 200.0)

    def test_unusable_declared_duration_fails(self) -> None:
        plan = _valid_plan_document()
        del plan["video_duration_seconds"]
        with self.assertRaises(common.StageBError):
            render.assert_declared_duration_matches_source(plan, 300.0)


@unittest.skipUnless(FFMPEG_AVAILABLE and PIL_AVAILABLE, "ffmpeg/ffprobe/Pillow required")
class FrameSelectionFidelityTests(unittest.TestCase):
    """Constructed test (investigation point 6): a cut's start/end seconds must
    select the EXACT source frames, not a neighboring/mismatched segment."""

    def _make_color_source(self, path: Path, seconds: int = 12, fps: int = 30) -> None:
        """Build a source whose second N is solid SECOND_COLORS[N]."""
        inputs: list[str] = []
        filter_parts: list[str] = []
        for n in range(seconds):
            r, g, b = SECOND_COLORS[n % len(SECOND_COLORS)]
            inputs += ["-f", "lavfi", "-i",
                       f"color=c=0x{r:02x}{g:02x}{b:02x}:size=64x64:rate={fps}:duration=1"]
            filter_parts.append(f"[{n}:v]")
        filter_complex = (
            "".join(filter_parts)
            + f"concat=n={seconds}:v=1:a=0[v]"
        )
        subprocess.run(
            ["ffmpeg", "-y", *inputs, "-filter_complex", filter_complex,
             "-map", "[v]", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(path)],
            check=True, capture_output=True,
        )

    def _dominant_color(self, frame_png: Path) -> tuple[int, int, int]:
        img = Image.open(frame_png).convert("RGB").resize((8, 8))
        pixels = list(img.getdata())
        n = len(pixels)
        r = sum(p[0] for p in pixels) // n
        g = sum(p[1] for p in pixels) // n
        b = sum(p[2] for p in pixels) // n
        return (r, g, b)

    def _nearest_second(self, color: tuple[int, int, int]) -> int:
        best, best_d = 0, float("inf")
        for n, ref in enumerate(SECOND_COLORS):
            d = sum((color[i] - ref[i]) ** 2 for i in range(3))
            if d < best_d:
                best, best_d = n, d
        return best

    def _extract_output_frame(self, video: Path, at_seconds: float, out_png: Path) -> None:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{at_seconds:.3f}", "-i", str(video),
             "-frames:v", "1", str(out_png)],
            check=True, capture_output=True,
        )

    def test_cut_extracts_the_requested_source_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tds:
            td = Path(tds)
            src = td / "source.mp4"
            self._make_color_source(src, seconds=12, fps=30)
            src_dur = common.probe_duration_seconds(src)

            # Cut source seconds [4,7) -> colors 4,5,6 (magenta, cyan, dark-red).
            cut_start, cut_end = 4, 7
            plan_doc = _valid_plan_document(
                video_duration_seconds=int(round(src_dur)),
                cuts=[{"start_seconds": cut_start, "end_seconds": cut_end,
                       "voiceover_text": "A short narration."}],
            )
            plan = common.normalize_plan(plan_doc)

            # 2.4s voiceover vs 3.0s footage = 80% (above the 75% duration-
            # collapse floor) and still exercises the speed-up path.
            vo = td / "vo.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi",
                 "-i", "sine=frequency=440:sample_rate=48000:duration=2.4",
                 "-ac", "1", "-c:a", "pcm_s16le", str(vo)],
                check=True, capture_output=True,
            )
            manifest = td / "manifest.json"
            manifest.write_text(json.dumps({"version": 2, "cuts": [
                {"index": 0, "wav": str(vo), "duration_seconds": 2.4,
                 "duration_frames": 115200, "voiceover_text": "A short narration.",
                 "mastering": {"preset": "test"}},
            ]}), encoding="utf-8")

            out = td / "out.mp4"
            render.render_merged(str(src), plan, str(manifest), str(out))

            # Voiceover 2.4s < footage 3.0s -> footage sped up to exactly 2.4s.
            out_dur = common.probe_duration_seconds(out)
            self.assertAlmostEqual(out_dur, 2.4, delta=0.25)

            # Frame-selection fidelity: the output's FIRST frame must be the
            # source's second-4 color, and a frame near the END must be the
            # source's second-6 color — i.e. the cut spans the requested source
            # range [4,7), not a shifted/neighboring segment. With a 0.8x speed
            # (3s footage -> 2.4s output), output t=0.05s maps to source ~4.06s
            # (second 4) and output t=2.30s maps to source ~6.9s (second 6).
            first_png = td / "first.png"
            self._extract_output_frame(out, 0.05, first_png)
            self.assertEqual(self._nearest_second(self._dominant_color(first_png)), 4)

            last_png = td / "last.png"
            self._extract_output_frame(out, 2.30, last_png)
            self.assertEqual(self._nearest_second(self._dominant_color(last_png)), 6)

    def test_reconcile_preserves_cut_boundaries(self) -> None:
        # Reconciliation must retime footage, never move which source frames
        # are selected.
        cuts = [{"start_seconds": 4, "end_seconds": 7}]
        reconciled = render.reconcile_cuts(cuts, [1.5], 12.0)
        self.assertEqual(reconciled[0]["start_seconds"], 4.0)
        self.assertEqual(reconciled[0]["end_seconds"], 7.0)
        self.assertAlmostEqual(reconciled[0]["stretch"], 0.5)


if __name__ == "__main__":
    unittest.main()
