#!/usr/bin/env python3
"""
character_index.py — Build a stable, deterministic face/character index
of the source video, so the downstream vision agent stops re-guessing
"who is this?" every time it opens a screenshot.

Purpose
-------
Character identification is the #1 thing the vision agent gets wrong in
this pipeline. It re-identifies each person from scratch on every frame,
so the same character ends up narrated as "a man", then "the boy", then
"the fighter" across three cuts of the same scene. The commentary reads
as a highlight reel of unrelated people instead of one story.

This script fixes tracking (not naming) by running a CPU-only face
pipeline that assigns a *stable* identity to every appearance of every
face in the video:

  1. For each shot in scene_index.json, sample a small number of frames
     from the ALREADY-COMPRESSED 720p analysis copy (typically 1-3 frames
     spread across the shot). No re-decoding of the original 4K/HEVC file.
  2. Run InsightFace's `buffalo_sc` bundle (small ONNX model, downloads
     once, runs on CPU) to get face bounding boxes + 512-d embeddings.
  3. Cluster the embeddings across the whole video with an
     agglomerative-average threshold to group repeat appearances of the
     same face into stable identities: person_A, person_B, ...
  4. Emit `character_index.json` listing every identity, every appearance
     (shot_id, timestamp, bbox), and a `representative_frame` — plus tiny
     face-crop thumbnails written into `people/` under the screenshots
     directory so the agent can see "here is person_A" at a glance.

Zero AI-vision-token cost: the entire pipeline is local CPU. Naming is
still the downstream agent's job (it has the transcript and screenshots
for context); this script only handles the *tracking* — which is exactly
where the free, deterministic tools have a huge accuracy advantage over
an LLM squinting at 6-panel grids.

Graceful degradation
--------------------
If `insightface` / `onnxruntime` is unavailable or the model download
fails, this script writes an EMPTY-but-valid character_index.json and
exits 0. The rest of the pipeline still works; the agent just falls
back to its old descriptive-tag behavior.

Output shape (`character_index.json`)
-------------------------------------
{
  "video_duration_seconds": 1416,
  "identity_count": 4,
  "backend": "insightface/buffalo_sc",
  "clustering": { "threshold": 0.5, "algorithm": "agglomerative_avg" },
  "identities": [
    {
      "person_id": "person_A",
      "appearance_count": 27,
      "screen_time_seconds": 184.3,
      "representative": {
        "shot_id": 3,
        "timestamp_seconds": 42.5,
        "thumbnail_path": "people/person_A.jpg"
      },
      "appearances": [
        {
          "shot_id": 3,
          "timestamp_seconds": 42.5,
          "bbox": [x1, y1, x2, y2],
          "detection_score": 0.92
        },
        ...
      ]
    },
    ...
  ]
}

Usage
-----
    python character_index.py <compressed_video> <scene_index_json>
                              <thumbnails_out_dir> <output_json>
                              [--max-samples-per-shot 3]
                              [--cluster-threshold 0.5]
                              [--min-appearances 2]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import traceback
from typing import Any


# ---------------------------------------------------------------------------
# Sampling: pull a small number of representative frames per shot
# ---------------------------------------------------------------------------

def sample_frames_for_shot(
    video_path: str,
    shot: dict,
    tmp_dir: str,
    max_samples: int,
) -> list[tuple[float, str]]:
    """
    Extract up to `max_samples` JPEG frames spread across the shot's
    duration. Returns [(timestamp_seconds, image_path), ...].

    We deliberately sample from the compressed 720p analysis copy, not
    the original — the face detector doesn't need higher resolution than
    that, and this keeps ffmpeg fast.
    """
    start = float(shot["start_seconds"])
    end = float(shot["end_seconds"])
    dur = max(end - start, 0.05)

    if max_samples <= 1 or dur < 1.0:
        # Very short shot: one anchor frame at the midpoint is enough.
        times = [(start + end) / 2.0]
    else:
        # Evenly spaced points strictly inside the shot, avoiding the
        # first/last 5% (where cut noise / motion blur is most likely).
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
        dst = os.path.join(
            tmp_dir, f"shot{shot['shot_id']:05d}_s{i}_{int(t*1000):09d}.jpg"
        )
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{t:.3f}",
            "-i", video_path,
            "-frames:v", "1",
            "-q:v", "3",
            "-vf", "scale='min(1280,iw)':'-2'",
            dst,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            if os.path.getsize(dst) > 0:
                outputs.append((round(t, 3), dst))
        except subprocess.CalledProcessError:
            # Individual frame-extraction failures are non-fatal — we
            # still have other samples from the same shot.
            continue
    return outputs


# ---------------------------------------------------------------------------
# Face detection + embedding via InsightFace (CPU)
# ---------------------------------------------------------------------------

class FaceEngine:
    """
    Thin wrapper around InsightFace's FaceAnalysis. Lazily imports so the
    module is testable without the heavy deps installed.
    """

    def __init__(self) -> None:
        import cv2  # noqa: F401  (checked here so the error is clear)
        from insightface.app import FaceAnalysis  # type: ignore

        # `buffalo_sc` is the SMALL bundle: SCRFD detector + MobileFaceNet
        # embedder. ~30 MB total download, runs comfortably on CPU. The
        # `sc` variant is what makes this affordable inside a GH Actions
        # ubuntu-latest runner.
        self.app = FaceAnalysis(
            name="buffalo_sc",
            allowed_modules=["detection", "recognition"],
        )
        # ctx_id=-1 forces CPU. det_size=(640, 640) is a good balance of
        # small-face recall and speed on the 720p analysis copy.
        self.app.prepare(ctx_id=-1, det_size=(640, 640))

    def analyze(self, image_path: str) -> list[dict]:
        import cv2
        import numpy as np  # noqa: F401

        img = cv2.imread(image_path)
        if img is None:
            return []
        faces = self.app.get(img)
        out: list[dict] = []
        for f in faces:
            # Skip very low-confidence detections outright.
            score = float(getattr(f, "det_score", 0.0) or 0.0)
            if score < 0.5:
                continue
            bbox = [int(v) for v in f.bbox.astype(int).tolist()]
            emb = f.normed_embedding.astype(float).tolist()
            out.append(
                {
                    "bbox": bbox,
                    "score": round(score, 3),
                    "embedding": emb,
                    "image_path": image_path,
                }
            )
        return out


# ---------------------------------------------------------------------------
# Clustering: group embeddings across the video into stable identities
# ---------------------------------------------------------------------------

def cluster_embeddings(
    embeddings: list[list[float]],
    threshold: float,
) -> list[int]:
    """
    Agglomerative average-link clustering on normalized 512-d embeddings.
    Returns a cluster-id list parallel to `embeddings`.

    We use cosine distance (`1 - dot`) because InsightFace embeddings are
    L2-normalized. `threshold` is the max intra-cluster average distance
    — 0.5 is the InsightFace-recommended sweet spot for buffalo_sc.
    """
    import numpy as np
    from sklearn.cluster import AgglomerativeClustering

    if not embeddings:
        return []

    X = np.array(embeddings, dtype=np.float32)
    if len(X) == 1:
        return [0]

    model = AgglomerativeClustering(
        n_clusters=None,
        metric="cosine",
        linkage="average",
        distance_threshold=threshold,
    )
    labels = model.fit_predict(X)
    return [int(x) for x in labels.tolist()]


# ---------------------------------------------------------------------------
# Assembly + thumbnail writing
# ---------------------------------------------------------------------------

def crop_and_write_thumbnail(
    src_image: str, bbox: list[int], dst: str, pad_ratio: float = 0.25
) -> bool:
    """
    Write a small square-ish thumbnail crop of the face at `bbox` from
    `src_image` to `dst`. Adds a % padding around the box so the
    thumbnail includes hair / context, not just the eyes-nose-mouth region.
    """
    try:
        import cv2

        img = cv2.imread(src_image)
        if img is None:
            return False
        h, w = img.shape[:2]
        x1, y1, x2, y2 = bbox
        bw = x2 - x1
        bh = y2 - y1
        pad_x = int(bw * pad_ratio)
        pad_y = int(bh * pad_ratio)
        cx1 = max(0, x1 - pad_x)
        cy1 = max(0, y1 - pad_y)
        cx2 = min(w, x2 + pad_x)
        cy2 = min(h, y2 + pad_y)
        crop = img[cy1:cy2, cx1:cx2]
        if crop.size == 0:
            return False
        # Resize so the long side is ~256 px — small enough to keep
        # screenshots.zip lean, large enough to recognize a face.
        ch, cw = crop.shape[:2]
        long_side = max(ch, cw)
        if long_side > 256:
            scale = 256.0 / long_side
            crop = cv2.resize(
                crop,
                (max(1, int(cw * scale)), max(1, int(ch * scale))),
                interpolation=cv2.INTER_AREA,
            )
        os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
        return bool(cv2.imwrite(dst, crop, [cv2.IMWRITE_JPEG_QUALITY, 85]))
    except Exception:
        return False


def person_id_for(index: int) -> str:
    """
    Map a 0-based cluster index to a human-friendly id: person_A, ...,
    person_Z, person_AA, person_AB, ... (base-26 letter suffix).
    """
    n = index
    letters = ""
    while True:
        n, r = divmod(n, 26)
        letters = chr(ord("A") + r) + letters
        if n == 0:
            break
        n -= 1
    return f"person_{letters}"


def emit_empty_index(output_json: str, duration: float, reason: str) -> None:
    payload = {
        "video_duration_seconds": float(duration),
        "identity_count": 0,
        "backend": "disabled",
        "reason": reason,
        "clustering": {},
        "identities": [],
    }
    os.makedirs(os.path.dirname(os.path.abspath(output_json)) or ".", exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Wrote EMPTY character index ({reason}) to {output_json}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("compressed_video")
    ap.add_argument("scene_index_json")
    ap.add_argument("thumbnails_out_dir")
    ap.add_argument("output_json")
    ap.add_argument("--max-samples-per-shot", type=int, default=3)
    ap.add_argument("--cluster-threshold", type=float, default=0.5)
    ap.add_argument(
        "--min-appearances",
        type=int,
        default=2,
        help="Drop identities that appear fewer than N times. Prevents "
             "one-frame background extras from polluting the index.",
    )
    args = ap.parse_args()

    for p in (args.compressed_video, args.scene_index_json):
        if not os.path.exists(p):
            print(f"Input not found: {p}", file=sys.stderr)
            sys.exit(2)

    with open(args.scene_index_json, "r", encoding="utf-8") as f:
        scene_data = json.load(f)
    shots = scene_data.get("shots", [])
    duration = float(scene_data.get("video_duration_seconds", 0.0))

    if not shots:
        emit_empty_index(args.output_json, duration, "no shots in scene index")
        return

    # Best-effort init of the face engine. If deps aren't there, degrade.
    try:
        engine = FaceEngine()
    except Exception as e:
        traceback.print_exc()
        emit_empty_index(
            args.output_json,
            duration,
            f"face engine unavailable: {type(e).__name__}: {e}",
        )
        return

    all_detections: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="clipforge_faces_") as tmp:
        for shot in shots:
            samples = sample_frames_for_shot(
                args.compressed_video, shot, tmp, args.max_samples_per_shot
            )
            for t, img_path in samples:
                try:
                    faces = engine.analyze(img_path)
                except Exception as e:
                    print(
                        f"  face analyze failed on shot {shot['shot_id']} @ {t}s: {e}",
                        flush=True,
                    )
                    faces = []
                for face in faces:
                    face["shot_id"] = shot["shot_id"]
                    face["timestamp_seconds"] = t
                    all_detections.append(face)

        print(f"Collected {len(all_detections)} face detection(s) across the video", flush=True)

        if not all_detections:
            emit_empty_index(args.output_json, duration, "no faces detected")
            return

        # Cluster.
        labels = cluster_embeddings(
            [d["embedding"] for d in all_detections],
            args.cluster_threshold,
        )

        # Group detections by cluster label.
        groups: dict[int, list[dict]] = {}
        for det, lbl in zip(all_detections, labels):
            groups.setdefault(lbl, []).append(det)

        # Rank clusters by screen presence (detection count is a good
        # cheap proxy) so person_A is always the most prominent character.
        ranked = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)

        identities: list[dict] = []
        for new_idx, (_lbl, dets) in enumerate(ranked):
            if len(dets) < args.min_appearances:
                continue
            pid = person_id_for(new_idx)

            # Pick the highest-scoring detection as the representative for
            # the thumbnail. Ties broken by earlier timestamp so the ID
            # thumbnail is the first strong view of that character.
            rep = max(
                dets,
                key=lambda d: (d["score"], -d["timestamp_seconds"]),
            )
            thumb_rel = f"people/{pid}.jpg"
            thumb_abs = os.path.join(args.thumbnails_out_dir, thumb_rel)
            crop_and_write_thumbnail(rep["image_path"], rep["bbox"], thumb_abs)

            appearances = [
                {
                    "shot_id": d["shot_id"],
                    "timestamp_seconds": d["timestamp_seconds"],
                    "bbox": d["bbox"],
                    "detection_score": d["score"],
                }
                for d in sorted(
                    dets, key=lambda x: (x["shot_id"], x["timestamp_seconds"])
                )
            ]

            # Screen-time estimate: sum the shot durations that contain at
            # least one detection of this identity.
            shot_ids_with_face = {d["shot_id"] for d in dets}
            shot_dur_by_id = {
                s["shot_id"]: (s["end_seconds"] - s["start_seconds"]) for s in shots
            }
            screen_time = round(
                sum(shot_dur_by_id.get(sid, 0.0) for sid in shot_ids_with_face),
                2,
            )

            identities.append(
                {
                    "person_id": pid,
                    "appearance_count": len(appearances),
                    "screen_time_seconds": screen_time,
                    "representative": {
                        "shot_id": rep["shot_id"],
                        "timestamp_seconds": rep["timestamp_seconds"],
                        "thumbnail_path": thumb_rel,
                    },
                    "appearances": appearances,
                }
            )

    payload = {
        "video_duration_seconds": duration,
        "identity_count": len(identities),
        "backend": "insightface/buffalo_sc",
        "clustering": {
            "threshold": args.cluster_threshold,
            "algorithm": "agglomerative_avg",
            "metric": "cosine",
        },
        "identities": identities,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.output_json)) or ".", exist_ok=True)
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(
        f"Wrote character index with {len(identities)} identity(ies) to {args.output_json}",
        flush=True,
    )


if __name__ == "__main__":
    main()
