#!/usr/bin/env python3
"""
Plan and render scene-level cinematic crop windows.

The cinematic renderer crops its merged Stage B video to a bare 1080x1200
(9:10 / project-labelled 10:9) frame. This module deliberately reuses
scene_index.detect_shots(), the established local ffmpeg scene-change detector,
to partition the merged video into static crop scenes. Each scene owns one
normalized crop centre; no crop position changes within a scene.

Batch D is introduced in two layers. The planning and ffmpeg wiring live here;
character detection supplies non-default centres later. Until a reliable face
centre is provided, each scene's centre remains the safe 0.5 / 0.5 fallback.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scene_index  # noqa: E402

CIN_FRAME_WIDTH = 1080
CIN_FRAME_HEIGHT = 1200
DEFAULT_SCENE_THRESHOLD = 0.35
FACE_SAMPLES_PER_SCENE = 3
MIN_FACE_CONFIDENCE = 0.55


def probe_duration(video_path: str) -> float:
    """Return a finite positive video duration in seconds."""
    raw = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", video_path,
    ], text=True).strip()
    duration = float(raw)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"Invalid duration for {video_path}: {raw!r}")
    return duration


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


class FaceCenterEngine:
    """Local CPU face detector used only to choose one static scene focus."""

    backend = "insightface/buffalo_sc:detection"

    def __init__(self) -> None:
        import cv2  # noqa: F401
        from insightface.app import FaceAnalysis  # type: ignore

        # Detection only: no identity embeddings or cross-scene tracking are
        # needed for a single static crop choice. ctx_id=-1 enforces CPU.
        self.app = FaceAnalysis(name="buffalo_sc", allowed_modules=["detection"])
        self.app.prepare(ctx_id=-1, det_size=(640, 640))

    def analyze(self, image_path: str) -> list[dict]:
        import cv2

        image = cv2.imread(image_path)
        if image is None:
            return []
        height, width = image.shape[:2]
        if height <= 0 or width <= 0:
            return []
        detections: list[dict] = []
        for face in self.app.get(image):
            confidence = float(getattr(face, "det_score", 0.0) or 0.0)
            if confidence < MIN_FACE_CONFIDENCE:
                continue
            x1, y1, x2, y2 = [float(v) for v in face.bbox.tolist()]
            box_width = max(0.0, x2 - x1)
            box_height = max(0.0, y2 - y1)
            area_fraction = (box_width * box_height) / float(width * height)
            if area_fraction <= 0:
                continue
            # Face area is the principal prominence signal. Confidence gates
            # bad boxes; sqrt keeps a close-up decisive without making a single
            # extreme frame overwhelm the scene's other samples.
            prominence = confidence * math.sqrt(area_fraction)
            detections.append({
                "center_x": clamp01((x1 + x2) / (2.0 * width)),
                "center_y": clamp01((y1 + y2) / (2.0 * height)),
                "confidence": confidence,
                "area_fraction": area_fraction,
                "prominence": prominence,
            })
        return detections


def _crop_offset_for_subject(
    subject_x: float,
    subject_y: float,
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
) -> tuple[float, float]:
    """Map a normalized subject centre to clamped crop-window offsets."""
    scale = max(target_width / source_width, target_height / source_height)
    scaled_width = source_width * scale
    scaled_height = source_height * scale
    excess_x = max(0.0, scaled_width - target_width)
    excess_y = max(0.0, scaled_height - target_height)
    desired_left = subject_x * scaled_width - target_width / 2.0
    desired_top = subject_y * scaled_height - target_height / 2.0
    offset_x = 0.5 if excess_x <= 0 else clamp01(desired_left / excess_x)
    offset_y = 0.5 if excess_y <= 0 else clamp01(desired_top / excess_y)
    return offset_x, offset_y


def _scene_face_focus(video_path: str, scene: dict, engine: FaceCenterEngine,
                      tmp_dir: str) -> dict | None:
    """Aggregate the prominent-face position from a few in-scene samples."""
    # Reuse the repository's established frame sampler: it avoids cut edges
    # and limits CPU decoding to 1–3 representative images per shot.
    from character_index import sample_frames_for_shot

    shot = {
        "shot_id": scene["scene_id"],
        "start_seconds": scene["start_seconds"],
        "end_seconds": scene["end_seconds"],
    }
    observations: list[dict] = []
    for _time_s, image_path in sample_frames_for_shot(
            video_path, shot, tmp_dir, FACE_SAMPLES_PER_SCENE):
        try:
            faces = engine.analyze(image_path)
        except Exception as error:
            print(f"  face analysis failed for scene {scene['scene_id']}: {error}",
                  flush=True)
            continue
        if faces:
            # One candidate per sampled frame: the face with the greatest
            # confidence-weighted area is the scene's visual subject there.
            observations.append(max(faces, key=lambda face: face["prominence"]))
    if not observations:
        return None
    weight = sum(obs["prominence"] for obs in observations)
    if weight <= 0:
        return None
    return {
        "subject_center_x": sum(obs["center_x"] * obs["prominence"] for obs in observations) / weight,
        "subject_center_y": sum(obs["center_y"] * obs["prominence"] for obs in observations) / weight,
        "sample_count": len(observations),
        "mean_confidence": sum(obs["confidence"] for obs in observations) / len(observations),
        "mean_face_area_fraction": sum(obs["area_fraction"] for obs in observations) / len(observations),
    }


def apply_face_centers(plan: dict, video_path: str,
                       engine: FaceCenterEngine | None = None) -> dict:
    """Update scene plans in place; detector problems leave safe centre crops."""
    source_size = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0", video_path,
    ], text=True).strip().split(",")
    source_width, source_height = (int(source_size[0]), int(source_size[1]))
    try:
        active_engine = engine or FaceCenterEngine()
    except Exception as error:
        plan["face_detector"] = {
            "backend": "unavailable",
            "reason": f"{type(error).__name__}: {error}",
        }
        for scene in plan["scenes"]:
            scene["position_source"] = "fallback_center_detector_unavailable"
        return plan

    import tempfile
    plan["face_detector"] = {"backend": active_engine.backend}
    focused = 0
    with tempfile.TemporaryDirectory(prefix="clipforge_cinematic_faces_") as tmp_dir:
        for scene in plan["scenes"]:
            focus = _scene_face_focus(video_path, scene, active_engine, tmp_dir)
            if focus is None:
                scene["position_source"] = "fallback_center_no_confident_face"
                continue
            offset_x, offset_y = _crop_offset_for_subject(
                focus["subject_center_x"], focus["subject_center_y"],
                source_width, source_height,
                int(plan["target_width"]), int(plan["target_height"]),
            )
            scene.update(focus)
            scene["crop_offset_x"] = offset_x
            scene["crop_offset_y"] = offset_y
            scene["position_source"] = "prominent_face"
            focused += 1
    plan["face_detector"]["focused_scene_count"] = focused
    return plan


def build_scene_crop_plan(
    video_path: str,
    duration_seconds: float | None = None,
    threshold: float = DEFAULT_SCENE_THRESHOLD,
    detect_shots: Callable[[str, float], list[float]] | None = None,
    face_engine: FaceCenterEngine | None = None,
    enable_face_detection: bool = True,
) -> dict:
    """Build one static fallback crop centre for each existing detected shot."""
    if not os.path.isfile(video_path):
        raise FileNotFoundError(video_path)
    duration = float(duration_seconds if duration_seconds is not None else probe_duration(video_path))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"Invalid duration: {duration!r}")
    detector = detect_shots or scene_index.detect_shots
    cut_times = detector(video_path, threshold)
    shots = scene_index.build_shot_index(cut_times, duration)
    scenes = []
    for shot in shots:
        scenes.append({
            "scene_id": int(shot["shot_id"]),
            "start_seconds": float(shot["start_seconds"]),
            "end_seconds": float(shot["end_seconds"]),
            "keyframe_seconds": float(shot["keyframe_seconds"]),
            "cause": shot["cause"],
            "crop_offset_x": 0.5,
            "crop_offset_y": 0.5,
            "position_source": "fallback_center",
        })
    plan = {
        "version": 2,
        "video_duration_seconds": duration,
        "target_width": CIN_FRAME_WIDTH,
        "target_height": CIN_FRAME_HEIGHT,
        "scene_detector": "scene_index.detect_shots",
        "scene_threshold": threshold,
        "scene_count": len(scenes),
        "scenes": scenes,
    }
    if enable_face_detection:
        return apply_face_centers(plan, video_path, face_engine)
    plan["face_detector"] = {"backend": "disabled"}
    return plan


def write_crop_plan(plan: dict, output_json: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(output_json)) or ".", exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as handle:
        json.dump(plan, handle, indent=2)
        handle.write("\n")


def scene_crop_filter(plan: dict, input_label: str = "0:v") -> tuple[list[str], str]:
    """Return an ffmpeg graph that applies one fixed crop window per scene.

    Source content is scaled once to fill the target frame without changing
    zoom. Each trimmed scene then crops the scaled image at its own static
    normalized x/y offset before concat reassembles the original timeline.
    """
    width = int(plan.get("target_width", CIN_FRAME_WIDTH))
    height = int(plan.get("target_height", CIN_FRAME_HEIGHT))
    scenes = list(plan.get("scenes") or [])
    if not scenes:
        raise ValueError("Crop plan contains no scenes")

    if len(scenes) == 1:
        filters = [
            f"[{input_label}]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"setsar=1[scaled_0]"
        ]
    else:
        split_labels = "".join(f"[scaled_{i}]" for i in range(len(scenes)))
        filters = [
            f"[{input_label}]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"setsar=1,split={len(scenes)}{split_labels}"
        ]
    labels: list[str] = []
    for index, scene in enumerate(scenes):
        start = max(0.0, float(scene["start_seconds"]))
        end = max(start + 0.001, float(scene["end_seconds"]))
        x = clamp01(scene.get("crop_offset_x", 0.5))
        y = clamp01(scene.get("crop_offset_y", 0.5))
        label = f"crop_scene_{index}"
        filters.append(
            f"[scaled_{index}]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,"
            f"crop={width}:{height}:x='(iw-ow)*{x:.6f}':y='(ih-oh)*{y:.6f}'[{label}]"
        )
        labels.append(f"[{label}]")
    if len(labels) == 1:
        filters.append(f"{labels[0]}null[reframed]")
    else:
        filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[reframed]")
    return filters, "reframed"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Build a scene-level cinematic crop plan")
    parser.add_argument("video_path")
    parser.add_argument("output_json")
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--threshold", type=float, default=DEFAULT_SCENE_THRESHOLD)
    args = parser.parse_args()
    plan = build_scene_crop_plan(args.video_path, args.duration, args.threshold)
    write_crop_plan(plan, args.output_json)
    print(
        f"Wrote {plan['scene_count']} static cinematic crop scene(s) to {args.output_json}",
        flush=True,
    )


if __name__ == "__main__":
    main()
