"""ClipForge Stage B — render pipeline (voiceover, render, reframe, captions,
watermark, enhance, compress).

Every module in this package obeys the ARCHITECTURE.md §7.4 contract:

* The incoming ``production.json`` is ALWAYS treated as untrusted and is
  re-validated at the Stage B boundary via
  ``pipeline.plan.schema.validate_production_plan`` (§13 invariant #5) — even
  though the bot already validated it at upload.
* Both accepted plan shapes are handled (nested ``series`` object OR flat
  ``series_*`` siblings; ``voiceover_text`` OR legacy ``raw_narration``) using
  the same normalization rules as ``pipeline/plan/schema.py``.
* The caller (``.github/workflows/stage-b.yml`` or ``python -m
  pipeline.stage_b.run``) is responsible for writing
  ``jobs/<job_id>/status.json`` via ``pipeline.status.write_status`` before and
  after each risky step so a crashed run leaves a resumable state.
* Intermediates (per-cut voiceover WAVs, crop plans, overlay streams) stay in
  the runner workspace only — only ``final.mp4`` and ``final.zip`` ship to the
  release.
"""
