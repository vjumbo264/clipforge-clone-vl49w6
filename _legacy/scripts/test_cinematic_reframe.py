#!/usr/bin/env python3
"""Deterministic checks for scene-level cinematic crop planning."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("reframe", ROOT / "cinematic_reframe.py")
assert spec and spec.loader
reframe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = reframe
spec.loader.exec_module(reframe)

with tempfile.NamedTemporaryFile(suffix=".mp4") as placeholder:
    calls = []
    def fake_detect(video, threshold):
        calls.append((video, threshold))
        return [2.0, 5.0]

    plan = reframe.build_scene_crop_plan(
        placeholder.name, duration_seconds=8.0, threshold=0.35,
        detect_shots=fake_detect, enable_face_detection=False,
    )

assert calls == [(placeholder.name, 0.35)]
assert plan["scene_detector"] == "scene_index.detect_shots"
assert plan["scene_count"] == 3
assert [(s["start_seconds"], s["end_seconds"]) for s in plan["scenes"]] == [
    (0.0, 2.0), (2.0, 5.0), (5.0, 8.0),
]
assert plan["face_detector"]["backend"] == "disabled"
assert all(s["crop_offset_x"] == 0.5 and s["crop_offset_y"] == 0.5
           and s["position_source"] == "fallback_center" for s in plan["scenes"])

filters, label = reframe.scene_crop_filter(plan)
joined = ";".join(filters)
assert label == "reframed"
assert "scale=1080:1200:force_original_aspect_ratio=increase" in joined
assert joined.count("crop=1080:1200") == 3
assert joined.count("(iw-ow)*0.500000") == 3
assert "split=3[scaled_0][scaled_1][scaled_2]" in joined
assert "concat=n=3:v=1:a=0[reframed]" in joined
# 16:9 source -> 9:10 target: only horizontal excess exists. An off-right
# subject moves the crop right, while a centred subject leaves it centred.
center_x, center_y = reframe._crop_offset_for_subject(0.5, 0.5, 1920, 1080, 1080, 1200)
right_x, right_y = reframe._crop_offset_for_subject(0.90, 0.5, 1920, 1080, 1080, 1200)
assert (center_x, center_y) == (0.5, 0.5)
assert 0.5 < right_x <= 1.0 and right_y == 0.5

focused_plan = {
    "target_width": 1080, "target_height": 1200,
    "scenes": [
        {"start_seconds": 0, "end_seconds": 1, "crop_offset_x": 0.0, "crop_offset_y": 0.5},
        {"start_seconds": 1, "end_seconds": 2, "crop_offset_x": 1.0, "crop_offset_y": 0.5},
    ],
}
focused_filters, _ = reframe.scene_crop_filter(focused_plan)
focused_graph = ";".join(focused_filters)
assert "(iw-ow)*0.000000" in focused_graph
assert "(iw-ow)*1.000000" in focused_graph

# Face observations alter exactly one scene-level crop position. The sampler
# is mocked here; image extraction and the actual local detector are exercised
# separately in the integration sample.
class FakeEngine:
    backend = "fake-local-face"

real_check_output = reframe.subprocess.check_output
real_scene_focus = reframe._scene_face_focus
reframe.subprocess.check_output = lambda *args, **kwargs: "1920,1080\n"
reframe._scene_face_focus = lambda *args, **kwargs: {
    "subject_center_x": 0.90, "subject_center_y": 0.50,
    "sample_count": 2, "mean_confidence": 0.93, "mean_face_area_fraction": 0.18,
}
face_plan = {
    "target_width": 1080, "target_height": 1200,
    "scenes": [{"scene_id": 1, "start_seconds": 0, "end_seconds": 2,
                "crop_offset_x": 0.5, "crop_offset_y": 0.5,
                "position_source": "fallback_center"}],
}
reframe.apply_face_centers(face_plan, "placeholder.mp4", FakeEngine())
assert face_plan["scenes"][0]["position_source"] == "prominent_face"
assert face_plan["scenes"][0]["crop_offset_x"] > 0.5
assert face_plan["scenes"][0]["crop_offset_y"] == 0.5
assert face_plan["face_detector"]["focused_scene_count"] == 1

# A missing local runtime must never fail a video; it retains a centred crop.
reframe._scene_face_focus = real_scene_focus
unavailable_plan = {
    "target_width": 1080, "target_height": 1200,
    "scenes": [{"scene_id": 1, "start_seconds": 0, "end_seconds": 1,
                "crop_offset_x": 0.5, "crop_offset_y": 0.5,
                "position_source": "fallback_center"}],
}
class BrokenEngine:
    def __init__(self):
        raise RuntimeError("model unavailable")
reframe.FaceCenterEngine = BrokenEngine
reframe.subprocess.check_output = lambda *args, **kwargs: "1920,1080\n"
reframe.apply_face_centers(unavailable_plan, "placeholder.mp4")
reframe.subprocess.check_output = real_check_output
assert unavailable_plan["face_detector"]["backend"] == "unavailable"
assert unavailable_plan["scenes"][0]["position_source"] == "fallback_center_detector_unavailable"

print("PASS: scene-level face focus shifts the crop and unavailable detector falls back safely")
