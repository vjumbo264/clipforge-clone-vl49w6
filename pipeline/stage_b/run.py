"""Stage B orchestrator — runs the full render pipeline for one job.

Order of operations (each risky step is bracketed by
``pipeline.status.write_status`` so a crashed run leaves a resumable record):

    validate production.json (§13 invariant #5)
      → voiceover (Edge TTS, 24 kHz mono PCM)
      → render (mobile-safe single-pass merge; muted source; VO [+ music])
      → enhance (optional, mobile-safe filter chain)
      → captions (cinematic reframe + sentence cards + title banner)
      → watermark (optional creator watermark)
      → compress (terminal delivery CRF pass)
      → final.mp4 validated; caller packages final.zip + uploads to release

This module is runnable standalone for testing::

    python -m pipeline.stage_b.run \
        --job-id test-1 --production-json production.json \
        --original-video source.mp4 --work-dir work \
        [--music path:jobs/test-1/music.mp3] [--no-enhance] [--no-status]

``--no-status`` skips status.json writes (useful for local smoke tests where
no job record exists). In GitHub Actions the workflow calls this WITHOUT
``--no-status`` so every step transition is persisted.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

from pipeline import status as status_mod
from pipeline.stage_b import (
    captions,
    common,
    compress,
    enhance,
    render,
    voiceover,
    watermark,
)


def _write(job_id: str, *, enabled: bool, state: str, message: str, **kwargs) -> None:
    """Thin status-write wrapper so every step bracket is one line."""
    if not enabled:
        return
    status_mod.write_status(job_id, state=state, message=message, **kwargs)


def write_metadata_txt(plan: dict, out_path: Path) -> None:
    """Write the posting-package metadata.txt (title + hashtags + YouTube tags)
    sourced verbatim from production.json. Optional fields write blank sections
    so the file structure stays predictable."""
    title = str(plan.get("title") or "").strip()

    def clean_str_list(value):
        if not isinstance(value, list):
            return []
        return [s.strip() for s in value if isinstance(s, str) and s.strip()]

    hashtag_line = " ".join(clean_str_list(plan.get("hashtags")))
    youtube_tag_line = ", ".join(clean_str_list(plan.get("youtube_tags")))
    lines = [
        "TITLE:", title, "",
        "HASHTAGS:", hashtag_line, "",
        "YOUTUBE TAGS:", youtube_tag_line, "",
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")


def package_final_zip(final_mp4: Path, metadata_txt: Path, production_json: Path,
                      out_zip: Path) -> None:
    """Zip final.mp4 + metadata.txt + production.json into final.zip and verify
    every expected member is present (§7.4)."""
    if not final_mp4.is_file():
        raise common.StageBError(f"final.mp4 missing: {final_mp4}")
    if not metadata_txt.is_file():
        raise common.StageBError(f"metadata.txt missing: {metadata_txt}")
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(final_mp4, "final.mp4")
        zf.write(metadata_txt, "metadata.txt")
        if production_json.is_file():
            zf.write(production_json, "production.json")
    with zipfile.ZipFile(out_zip, "r") as zf:
        names = set(zf.namelist())
    for required in ("final.mp4", "metadata.txt"):
        if required not in names:
            raise common.StageBError(f"{required} missing from final ZIP")
    print(f"Packaged {out_zip} ({out_zip.stat().st_size / 1024 / 1024:.2f} MB)", flush=True)


def run_stage_b(
    *,
    job_id: str,
    production_json: Path,
    original_video: Path,
    work_dir: Path,
    music_ref: str = "",
    enhance_enabled: bool = True,
    whisper_model: str = "base",
    whisper_lang: str = "en",
    enable_face_detection: bool = True,
    write_status: bool = True,
    release_tag: str = "",
    release_url: str = "",
    run_info: dict | None = None,
) -> dict:
    """Execute the full Stage B pipeline. Returns a summary dict with the final
    artifact paths. Raises ``common.StageBError`` on any user-safe failure."""
    job_id = common.sanitize_job_id(job_id)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    out_dir = work_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    status_kwargs = dict(
        release_tag=release_tag or None,
        release_url=release_url or None,
        run=run_info,
    )

    # ---- bug-57: sync status.series from the plan BEFORE any render step --- #
    # The task header, the Zernio-publish suppression, and the series
    # next-part affordance in the bot all read status.series — which used to
    # stay at its zeroed defaults (enabled:false, part:0) because nothing ever
    # copied the plan's series data into status.json. Sync it as the very
    # first status write of the run, from the RAW document (best-effort:
    # pre-validation), so the bot reflects the series even when the plan
    # subsequently fails boundary validation. After successful validation the
    # block is re-synced from the validated/normalized plan below, so a
    # malformed-but-parseable document can never leave a corrupt series block.
    try:
        raw_document = json.loads(Path(production_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw_document = None
    if isinstance(raw_document, dict):
        _write(job_id, enabled=write_status, state="stage_b_running",
               message="Stage B: validating production.json",
               series=common.plan_series_block(raw_document), **status_kwargs)
    else:
        _write(job_id, enabled=write_status, state="stage_b_running",
               message="Stage B: validating production.json", **status_kwargs)

    # ---- Boundary validation (§13 invariant #5) ---------------------------- #
    plan = common.load_production_plan(production_json)

    # bug-57 (authoritative sync): re-write status.series from the VALIDATED,
    # normalized plan so the block is guaranteed schema-correct from here on.
    _write(job_id, enabled=write_status, state="stage_b_running",
           message="Stage B: validating production.json",
           series=common.plan_series_block(plan), **status_kwargs)

    # ---- Optional music ----------------------------------------------------- #
    music_path = common.resolve_music_ref(music_ref, job_id=job_id, work_dir=work_dir) \
        if music_ref else None

    # ---- 1. Voiceover -------------------------------------------------------- #
    _write(job_id, enabled=write_status, state="stage_b_running",
           message="Stage B: synthesizing voiceover (Edge TTS)", **status_kwargs)
    vo_dir = work_dir / "voiceover"
    voiceover.generate_voiceovers(plan, vo_dir)
    manifest_path = vo_dir / "voiceover_manifest.json"

    # ---- 2. Render (merge) --------------------------------------------------- #
    _write(job_id, enabled=write_status, state="stage_b_running",
           message="Stage B: cutting + mixing merged video (mobile-safe)", **status_kwargs)
    merged_mp4 = out_dir / "final.mp4"
    merged_vo_wav = out_dir / "voiceover.wav"
    render.render_merged(
        original_video,
        plan,
        manifest_path,
        merged_mp4,
        voiceover_wav=merged_vo_wav,
        music=music_path,
    )

    # ---- 3. Enhance (optional) ---------------------------------------------- #
    _write(job_id, enabled=write_status, state="stage_b_running",
           message="Stage B: quality enhancement pass" if enhance_enabled
           else "Stage B: enhancement disabled", **status_kwargs)
    enhance.enhance_video(merged_mp4, enabled=enhance_enabled)

    # ---- 4. Captions (cinematic) --------------------------------------------- #
    _write(job_id, enabled=write_status, state="stage_b_running",
           message="Stage B: burning cinematic captions + title banner", **status_kwargs)
    captioned_mp4 = out_dir / "final_subtitled.mp4"
    captions.render_captions(
        str(merged_mp4),
        str(merged_vo_wav),
        plan,
        str(captioned_mp4),
        model=whisper_model,
        lang=whisper_lang,
        enable_face_detection=enable_face_detection,
    )
    os.replace(captioned_mp4, merged_mp4)

    # ---- 5. Watermark (optional) --------------------------------------------- #
    creator_name = common.load_creator_watermark_name()
    _write(job_id, enabled=write_status, state="stage_b_running",
           message=("Stage B: applying creator watermark" if creator_name
                    else "Stage B: no creator watermark configured"), **status_kwargs)
    if creator_name:
        watermarked_mp4 = out_dir / "final_watermarked.mp4"
        watermark.apply_watermark(merged_mp4, watermarked_mp4, creator_name)
        os.replace(watermarked_mp4, merged_mp4)

    # ---- 6. Compress (terminal delivery) -------------------------------------- #
    _write(job_id, enabled=write_status, state="stage_b_running",
           message="Stage B: final delivery compression", **status_kwargs)
    delivery_mp4 = out_dir / "final_delivery.mp4"
    before_bytes, final_bytes, reduced = compress.compress(merged_mp4, delivery_mp4)
    os.replace(delivery_mp4, merged_mp4)

    # Final validation of the shipped MP4.
    render.validate_mp4(merged_mp4)

    # ---- 7. Package ----------------------------------------------------------- #
    _write(job_id, enabled=write_status, state="stage_b_running",
           message="Stage B: packaging final.zip", **status_kwargs)
    metadata_txt = out_dir / "metadata.txt"
    write_metadata_txt(plan, metadata_txt)
    final_zip = out_dir / f"clipforge-{job_id}-final.zip"
    package_final_zip(merged_mp4, metadata_txt, Path(production_json), final_zip)

    return {
        "job_id": job_id,
        "final_mp4": str(merged_mp4),
        "final_zip": str(final_zip),
        "metadata_txt": str(metadata_txt),
        "title": plan.get("title", ""),
        "cut_count": len(plan.get("cuts") or []),
        "enhanced": enhance_enabled,
        "creator_watermark_applied": bool(creator_name),
        "music_applied": bool(music_path),
        "compressed_from_bytes": before_bytes,
        "compressed_to_bytes": final_bytes,
        "compression_reduced": reduced,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the full ClipForge Stage B pipeline.")
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--production-json", required=True)
    ap.add_argument("--original-video", required=True)
    ap.add_argument("--work-dir", default="work")
    ap.add_argument("--music", default="", help="music_ref (path:<repo-path> or a local file)")
    ap.add_argument("--enhance", dest="enhance", action="store_true", default=True)
    ap.add_argument("--no-enhance", dest="enhance", action="store_false")
    ap.add_argument("--whisper-model", default="base", choices=["tiny", "base", "small"])
    ap.add_argument("--whisper-lang", default="en")
    ap.add_argument("--no-face-detection", dest="face_detection", action="store_false", default=True)
    ap.add_argument("--no-status", dest="write_status", action="store_false", default=True,
                    help="skip jobs/<id>/status.json writes (local smoke tests)")
    ap.add_argument("--release-tag", default="")
    ap.add_argument("--release-url", default="")
    args = ap.parse_args(argv)

    run_info = {
        "workflow_run_id": int(os.environ.get("GITHUB_RUN_ID", "0") or 0),
        "workflow_run_url": os.environ.get("GITHUB_WORKFLOW_RUN_URL", ""),
        "code_ref": os.environ.get("GITHUB_SHA", ""),
    }

    try:
        summary = run_stage_b(
            job_id=args.job_id,
            production_json=Path(args.production_json),
            original_video=Path(args.original_video),
            work_dir=Path(args.work_dir),
            music_ref=args.music,
            enhance_enabled=args.enhance,
            whisper_model=args.whisper_model,
            whisper_lang=args.whisper_lang,
            enable_face_detection=args.face_detection,
            write_status=args.write_status,
            release_tag=args.release_tag,
            release_url=args.release_url,
            run_info=run_info,
        )
    except common.StageBError as exc:
        if args.write_status:
            try:
                status_mod.write_status(
                    common.sanitize_job_id(args.job_id),
                    state="error",
                    message=f"Stage B failed: {exc}",
                    release_tag=args.release_tag or None,
                )
            except Exception:
                pass
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 3

    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
