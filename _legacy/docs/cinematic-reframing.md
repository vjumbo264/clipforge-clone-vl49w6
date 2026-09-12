# Cinematic Scene Reframing

## Purpose

Cinematic mode renders a bare `1080×1200` vertical frame. Its source is first scaled to fill that frame at the existing effective zoom level, then cropped. Batch D makes the **crop position** scene-aware while deliberately keeping the scale unchanged. It never creates a continuous pan, zoom, or tracked camera move.

## Scene boundaries

`cinematic_reframe.py` reuses `scene_index.detect_shots()`, the repository's established local ffmpeg scene-change detector. It runs against the merged Stage B input because that is the exact timeline that will be rendered. The resulting crop plan partitions the video into static scenes. ffmpeg trims, crops, and concatenates those scene segments, so an offset can change only at a detected boundary.

## Prominent-character heuristic

For every scene, the local CPU `InsightFace` `buffalo_sc` detector samples up to three frames away from cut edges using the existing `character_index.py` frame sampler. It keeps the most prominent face from each sample, where prominence is:

> **detection confidence × square root of face-area fraction**

Face area is the principal signal because a large close-up is generally the subject that should survive a narrow crop. Detection confidence filters uncertain boxes, while the square root prevents a single extreme close-up from dominating the other representative samples. The selected face positions are aggregated using the same prominence weight. That aggregate centre is converted to a clamped crop-window offset.

The crop plan records the source and metrics for every scene, including `prominent_face`, `fallback_center_no_confident_face`, or `fallback_center_detector_unavailable`.

## Safety and fallback

If a face is not confidently found in a particular scene, that scene remains at the original centred crop. If OpenCV, InsightFace, ONNX Runtime, or the local model cannot initialize, the whole video renders with centered crops rather than failing Stage B. A single frame-extraction or detector error is also non-fatal.

The detector runs locally on CPU. The pinned dependencies are `opencv-python-headless` and `insightface`, with the existing `onnxruntime` backend. Operators must ensure that the chosen local model pack is licensed appropriately for their use case; the implementation intentionally degrades safely when a model cannot be used.

## Verification fixture

The Batch D end-to-end fixture uses a two-scene `1920×1080` sample with a real face positioned far right in the first scene and centred in the second. The test detected two scenes, selected a rightward crop offset of `1.0000` for the first scene, and retained a near-centred `0.4591` offset for the second. Both resulting frames are `1080×1200` and are visually checked.
