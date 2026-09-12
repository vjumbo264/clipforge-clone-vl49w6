"""Stage B — scene-level cinematic crop planning and ffmpeg wiring.

The cinematic renderer crops the merged Stage B video to a bare 1080x1200
(9:10) frame. Shot boundaries are detected locally with ffmpeg's scene-change
score; each scene owns ONE normalized crop centre and no crop position changes
within a scene. When a confident prominent face is found (optional CPU-only
InsightFace pass), the crop centre is biased toward it; otherwise the safe
0.5/0.5 centre is kept — detector problems never fail the render.

Ported from ``_legacy/scripts/cinematic_reframe.py`` (the shot detector from
``_legacy/scripts/scene_index.py`` and the frame sampler from
``_legacy/scripts/character_index.py`` are folded in so this package has no
dependency on legacy import-time path wiring).
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
from typing import Any, Callable

from pipeline.stage_b import common

CIN_FRAME_WIDTH = 1080
CIN_FRAME_HEIGHT = 1200
DEFAULT_SCENE_THRESHOLD = 0.35
FACE_SAMPLES_PER_SCENE = 3
MIN_FACE_CONFIDENCE = 0.55
# bug-36: real faces (InsightFace) AND stylised/anime faces (OpenCV
# animeface cascade) are both detected; whichever sees a face wins. The
# cascade's rectangles carry no confidence, so they use a neutral 0.6 (just
# above MIN_FACE_CONFIDENCE) and compete on size.
_ANIME_CASCADE_CONFIDENCE = 0.6
# bug-40: the anime-face cascade is VENDORED in this repo (MIT-licensed,
# nagadomi/lbpcascade_animeface). opencv-python-headless does NOT ship
# lbpcascade_animeface.xml, so looking it up inside cv2.data.haarcascades
# (as the bug-36 code did) silently degraded "anime faces work" to the
# human frontal haar cascade in production.
_BUNDLED_CASCADE_DIR = os.path.join(os.path.dirname(__file__), "cascades")
_ANIME_CASCADE_SOURCES = (
    # (cascade file name, directory resolver) — first file that exists wins.
    ("lbpcascade_animeface.xml", "bundled"),   # vendored (nagadomi, MIT)
    ("lbpcascade_animeface.xml", "opencv"),    # in case a build ships it
    ("haarcascade_frontalface_default.xml", "opencv"),
)
# bug-40: animal faces. OpenCV ships cat-face cascades; a lazy YOLOv8n
# pass (ultralytics, optional dependency) covers dogs/birds and 13 more
# COCO animal classes. Boxes from animal detectors use the same neutral
# confidence as the anime cascade and compete on size.
_ANIMAL_CASCADE_CONFIDENCE = 0.6
_ANIMAL_CASCADE_FILES = (
    "haarcascade_frontalcatface_extended.xml",
    "haarcascade_frontalcatface.xml",
)
_YOLO_ANIMAL_CLASS_IDS = (14, 15, 16, 17, 18, 19, 20, 21, 22, 23)  # bird..zebra
_YOLO_EXTRA_ANIMAL_NAMES = frozenset({"dog"})  # label-name guard
_YOLO_MIN_CONFIDENCE = 0.4

_SCENE_LINE_RE = re.compile(r"pts_time:(?P<t>[0-9]+(?:\.[0-9]+)?)")


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


# --------------------------------------------------------------------------- #
# Shot detection (ported from _legacy/scripts/scene_index.py)                  #
# --------------------------------------------------------------------------- #

def detect_shots(video_path: str, threshold: float) -> list[float]:
    """Return shot-change timestamps (seconds) from ffmpeg's scene score."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", video_path,
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ]
    print(f"$ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr[-2000:], flush=True)
        raise common.StageBError(f"ffmpeg scene-detect failed (exit {proc.returncode})")

    times: list[float] = []
    for line in proc.stderr.splitlines():
        m = _SCENE_LINE_RE.search(line)
        if m:
            try:
                times.append(float(m.group("t")))
            except ValueError:
                pass

    uniq: list[float] = []
    for t in sorted(times):
        if not uniq or (t - uniq[-1]) > 0.05:
            uniq.append(t)
    return uniq


def build_shot_index(cut_times: list[float], total_duration: float) -> list[dict]:
    """Turn sorted cut timestamps into shot dicts covering [0, total_duration]."""
    shots: list[dict] = []
    boundaries = [0.0, *cut_times, float(total_duration)]
    for index in range(len(boundaries) - 1):
        start = boundaries[index]
        end = boundaries[index + 1]
        shots.append(
            {
                "shot_id": index + 1,
                "start_seconds": start,
                "end_seconds": end,
                "keyframe_seconds": (start + end) / 2.0,
                "cause": "video_start" if index == 0 else "cut",
            }
        )
    return shots


def sample_frames_for_shot(
    video_path: str,
    shot: dict,
    tmp_dir: str,
    max_samples: int,
) -> list[tuple[float, str]]:
    """Extract up to ``max_samples`` JPEG frames spread across the shot.

    Sampling avoids the first/last 5% of the shot where cut noise and motion
    blur are most likely. Returns ``[(timestamp_seconds, image_path), ...]``.
    """
    start = float(shot["start_seconds"])
    end = float(shot["end_seconds"])
    dur = max(end - start, 0.05)

    if max_samples <= 1 or dur < 1.0:
        times = [(start + end) / 2.0]
    else:
        n = min(max_samples, max(1, int(dur // 2)))
        n = max(n, 1)
        pad = dur * 0.05
        if n == 1:
            times = [(start + end) / 2.0]
        else:
            step = (dur - 2 * pad) / (n - 1)
            times = [start + pad + i * step for i in range(n)]

    outputs: list[tuple[float, str]] = []
    for i, t in enumerate(times):
        dst = os.path.join(tmp_dir, f"shot{shot['shot_id']:05d}_s{i}_{int(t * 1000):09d}.jpg")
        common.run(
            [
                "ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", video_path,
                "-frames:v", "1", "-q:v", "3", dst,
            ],
            "shot frame sampling",
        )
        if os.path.isfile(dst):
            outputs.append((t, dst))
    return outputs


# --------------------------------------------------------------------------- #
# Face-centre engine (optional; falls back to centre crop)                     #
# --------------------------------------------------------------------------- #

class FaceCenterEngine:
    """Local CPU subject detector used only to choose one static scene focus.

    Detection tiers, all best-effort (a tier that is unavailable or finds
    nothing simply falls through to the next):
      1. InsightFace buffalo_sc — real human faces (confidence-scored).
      2. OpenCV cascades — stylised/anime faces (vendored
         lbpcascade_animeface.xml) and cat faces (haarcascade_frontalcatface*),
         all shipped with the repo / the wheel so they resolve in CI.
      3. YOLOv8n (optional ultralytics dependency) — COCO animal classes
         (bird/cat/dog/horse/sheep/cow/elephant/bear/zebra/giraffe) for
         non-cat animal subjects.
    """

    backend = "insightface/buffalo_sc + cascades(anime,cat) + yolov8n(animals)"

    def __init__(self) -> None:
        import cv2
        from insightface.app import FaceAnalysis  # type: ignore

        # Detection only; ctx_id=-1 enforces CPU.
        self.app = FaceAnalysis(name="buffalo_sc", allowed_modules=["detection"])
        self.app.prepare(ctx_id=-1, det_size=(640, 640))
        self._cv2 = cv2
        self._stylised_cascades = _load_stylised_cascades(cv2)
        self._animal_cascades = _load_animal_cascades(cv2)
        self._yolo = None          # lazily constructed on first need
        self._yolo_tried = False

    # -- cascade loading helpers are module-level below; engine caches them --

    def _yolo_model(self):
        """bug-40: lazily import ultralytics and load YOLOv8n; returns None
        when the dependency or weights are unavailable (never raises)."""
        if self._yolo_tried:
            return self._yolo
        self._yolo_tried = True
        try:
            from ultralytics import YOLO  # type: ignore

            self._yolo = YOLO("yolov8n.pt")
        except Exception as error:
            print(f"  animal detector (YOLOv8n) unavailable: {type(error).__name__}: {error}", flush=True)
            self._yolo = None
        return self._yolo

    def analyze(self, image_path: str) -> list[dict]:
        cv2 = self._cv2

        image = cv2.imread(image_path)
        if image is None:
            return []
        height, width = image.shape[:2]
        if height <= 0 or width <= 0:
            return []
        detections: list[dict] = []

        def _record(x1, y1, x2, y2, confidence):
            box_width = max(0.0, x2 - x1)
            box_height = max(0.0, y2 - y1)
            area_fraction = (box_width * box_height) / float(width * height)
            if area_fraction <= 0:
                return
            _record.hits += 1
            detections.append(
                {
                    "center_x": clamp01((x1 + x2) / (2.0 * width)),
                    "center_y": clamp01((y1 + y2) / (2.0 * height)),
                    "confidence": confidence,
                    "area_fraction": area_fraction,
                    "prominence": confidence * math.sqrt(area_fraction),
                }
            )
        _record.hits = 0

        for face in self.app.get(image):
            confidence = float(getattr(face, "det_score", 0.0) or 0.0)
            if confidence < MIN_FACE_CONFIDENCE:
                continue
            x1, y1, x2, y2 = [float(v) for v in face.bbox.tolist()]
            _record(x1, y1, x2, y2, confidence)

        # bug-36 + bug-40: if the CNN found nothing, try the stylised/anime
        # and cat-face cascades (shipped with the repo / OpenCV), then a
        # YOLO animal pass for other animal subjects. Fallback tiers never
        # raise and never fail the render.
        if not detections:
            try:
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                gray = cv2.equalizeHist(gray)
                for cascade in (*self._stylised_cascades, *self._animal_cascades):
                    _record.hits = 0
                    for (cx, cy, cw, ch) in cascade.detectMultiScale(
                        gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24)
                    ):
                        _record(float(cx), float(cy), float(cx + cw), float(cy + ch), _ANIME_CASCADE_CONFIDENCE)
                    if detections:
                        break
            except Exception:
                pass

        if not detections:
            model = self._yolo_model()
            if model is not None:
                try:
                    results = model.predict(image, verbose=False)
                    names = getattr(model, "names", {}) or {}
                    for result in results or []:
                        boxes = getattr(result, "boxes", None)
                        if boxes is None:
                            continue
                        for cls_id, conf, xyxy in zip(
                            boxes.cls.tolist(), boxes.conf.tolist(), boxes.xyxy.tolist()
                        ):
                            label = str(names.get(int(cls_id), "")).lower()
                            is_animal = int(cls_id) in _YOLO_ANIMAL_CLASS_IDS or label in _YOLO_EXTRA_ANIMAL_NAMES
                            if not is_animal or float(conf) < _YOLO_MIN_CONFIDENCE:
                                continue
                            x1, y1, x2, y2 = [float(v) for v in xyxy]
                            _record(x1, y1, x2, y2, float(conf))
                except Exception as error:
                    print(f"  animal detector pass failed: {type(error).__name__}: {error}", flush=True)

        return detections


def _cascade_path(cv2, name: str, source: str) -> str | None:
    base = _BUNDLED_CASCADE_DIR if source == "bundled" else cv2.data.haarcascades
    path = os.path.join(base, name)
    return path if os.path.isfile(path) else None


def _load_stylised_cascades(cv2) -> list:
    """bug-36/bug-40: cascades that catch stylised/anime faces the CNN
    detector misses. Best-effort: missing cascade files yield no extras."""
    cascades = []
    for name, source in _ANIME_CASCADE_SOURCES:
        path = _cascade_path(cv2, name, source)
        if path is None:
            continue
        cascade = cv2.CascadeClassifier(path)
        if not cascade.empty():
            cascades.append(cascade)
            break  # first available stylised cascade wins
    return cascades


def _load_animal_cascades(cv2) -> list:
    """bug-40: OpenCV's shipped cat-face cascades. Other animal species are
    covered by the lazy YOLO pass in FaceCenterEngine.analyze."""
    cascades = []
    for name in _ANIMAL_CASCADE_FILES:
        path = _cascade_path(cv2, name, "opencv")
        if path is None:
            continue
        cascade = cv2.CascadeClassifier(path)
        if not cascade.empty():
            cascades.append(cascade)
    return cascades


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
    shot = {
        "shot_id": scene["scene_id"],
        "start_seconds": scene["start_seconds"],
        "end_seconds": scene["end_seconds"],
    }
    observations: list[dict] = []
    for _time_s, image_path in sample_frames_for_shot(video_path, shot, tmp_dir, FACE_SAMPLES_PER_SCENE):
        try:
            faces = engine.analyze(image_path)
        except Exception as error:
            print(f"  face analysis failed for scene {scene['scene_id']}: {error}", flush=True)
            continue
        if faces:
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
    source_width, source_height = common.probe_video_size(video_path)
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
    detect_shots_fn: Callable[[str, float], list[float]] | None = None,
    face_engine: FaceCenterEngine | None = None,
    enable_face_detection: bool = True,
) -> dict:
    """Build one static crop centre per detected shot of the merged video."""
    if not os.path.isfile(video_path):
        raise common.StageBError(f"video not found: {video_path}")
    duration = float(duration_seconds if duration_seconds is not None
                     else common.probe_duration_seconds(video_path))
    if not math.isfinite(duration) or duration <= 0:
        raise common.StageBError(f"Invalid duration: {duration!r}")
    detector = detect_shots_fn or detect_shots
    cut_times = detector(video_path, threshold)
    shots = build_shot_index(cut_times, duration)
    scenes = []
    for shot in shots:
        scenes.append(
            {
                "scene_id": int(shot["shot_id"]),
                "start_seconds": float(shot["start_seconds"]),
                "end_seconds": float(shot["end_seconds"]),
                "keyframe_seconds": float(shot["keyframe_seconds"]),
                "cause": shot["cause"],
                "crop_offset_x": 0.5,
                "crop_offset_y": 0.5,
                "position_source": "fallback_center",
            }
        )
    plan = {
        "version": 2,
        "video_duration_seconds": duration,
        "target_width": CIN_FRAME_WIDTH,
        "target_height": CIN_FRAME_HEIGHT,
        "scene_detector": "stage_b.reframe.detect_shots",
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
        raise common.StageBError("Crop plan contains no scenes")

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


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Build a scene-level cinematic crop plan")
    parser.add_argument("video_path")
    parser.add_argument("output_json")
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--threshold", type=float, default=DEFAULT_SCENE_THRESHOLD)
    args = parser.parse_args(argv)
    plan = build_scene_crop_plan(args.video_path, args.duration, args.threshold)
    write_crop_plan(plan, args.output_json)
    print(
        f"Wrote {plan['scene_count']} static cinematic crop scene(s) to {args.output_json}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except common.StageBError as exc:
        import sys

        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(3)
