"""Local end-to-end smoke test for the Stage B render chain (NOT a unit test —
skipped by unittest discovery because of the ``smoke_`` prefix).

Exercises the REAL ffmpeg paths of render/enhance/captions/watermark/compress
against a synthetic source video. Edge TTS synthesis and faster-whisper
transcription are the only stubbed pieces: the voiceover WAVs are generated
with ffmpeg's speech-like tone and ``captions.transcribe_words`` is monkeypatched
to return deterministic word timings (network/model downloads are unavailable
in this environment).

Run manually:  python pipeline/tests/smoke_stage_b_local.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.stage_b import captions, common, compress, enhance, render, run as stage_b_run, watermark  # noqa: E402


def sh(cmd: list[str]) -> None:
    print("$ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="clipforge_stageb_smoke_"))
    print(f"smoke workdir: {tmp}", flush=True)

    # ---- synthetic 12s source video (with audio) -------------------------- #
    src = tmp / "source.mp4"
    sh([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=12",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(src),
    ])

    # ---- production.json --------------------------------------------------- #
    plan_doc = {
        "version": 2,
        "job_id": "manual-smoke",
        "title": "Smoke Test Clip",
        "video_duration_seconds": 12,
        "target_total_duration_seconds": 8,
        "cuts": [
            {"start_seconds": 1, "end_seconds": 5, "voiceover_text": "This is the first cut. It has narration."},
            {"start_seconds": 6, "end_seconds": 10, "voiceover_text": "Second cut here, with a clause. And the ending."},
        ],
        "hashtags": ["#one", "#two", "#three", "#four", "#five"],
        "youtube_tags": [f"tag{i}" for i in range(10)],
    }
    production_json = tmp / "production.json"
    production_json.write_text(json.dumps(plan_doc), encoding="utf-8")
    plan = common.load_production_plan(production_json)
    print("plan validated + normalized OK", flush=True)

    # ---- synthetic voiceover WAVs (stand-in for Edge TTS output) ---------- #
    vo_dir = tmp / "voiceover"
    vo_dir.mkdir()
    manifest_cuts = []
    for index, seconds in ((1, 3.0), (2, 3.0)):
        wav_path = vo_dir / f"voiceover_{index:02d}.wav"
        sh([
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"sine=frequency={300 + index * 100}:sample_rate=24000:duration={seconds}",
            "-ac", "1", "-c:a", "pcm_s16le", str(wav_path),
        ])
        frames = int(seconds * 24000)
        manifest_cuts.append({
            "index": index,
            "wav": str(wav_path),
            "duration_seconds": seconds,
            "duration_frames": frames,
            "voiceover_text": plan_doc["cuts"][index - 1]["voiceover_text"],
            "mastering": {"preset": "smoke"},
        })
    manifest_path = vo_dir / "voiceover_manifest.json"
    manifest_path.write_text(json.dumps({"version": 2, "cuts": manifest_cuts}), encoding="utf-8")

    out_dir = tmp / "out"
    out_dir.mkdir()
    merged = out_dir / "final.mp4"
    merged_vo = out_dir / "voiceover.wav"

    # ---- 1. render ---------------------------------------------------------- #
    summary = render.render_merged(src, plan, manifest_path, merged, voiceover_wav=merged_vo)
    print("render OK:", summary["total_seconds"], "s", flush=True)

    # ---- 2. enhance ---------------------------------------------------------- #
    applied = enhance.enhance_video(merged, enabled=True)
    print("enhance OK (applied:", applied, ")", flush=True)

    # ---- 3. captions (transcribe_words stubbed) ------------------------------ #
    def fake_transcribe(wav_path: str, model: str, lang: str, work_dir: str) -> list[dict]:
        words: list[dict] = []
        t = 0.1
        for text in (
            "This is the first cut It has narration".split()
            + "Second cut here with a clause And the ending".split()
        ):
            words.append({"start": round(t, 3), "end": round(t + 0.3, 3), "word": text})
            t += 0.36
        return words

    captions.transcribe_words = fake_transcribe  # type: ignore[assignment]
    captioned = out_dir / "final_subtitled.mp4"
    cap_summary = captions.render_captions(
        str(merged), str(merged_vo), plan, str(captioned),
        enable_face_detection=False,
    )
    print("captions OK:", cap_summary["caption_card_count"], "cards", flush=True)
    captioned.replace(merged)

    # ---- 4. watermark -------------------------------------------------------- #
    watermarked = out_dir / "final_watermarked.mp4"
    did = watermark.apply_watermark(merged, watermarked, "Smoke Tester")
    assert did
    watermarked.replace(merged)
    print("watermark OK", flush=True)

    # ---- 5. compress ---------------------------------------------------------- #
    delivery = out_dir / "final_delivery.mp4"
    before, after, reduced = compress.compress(merged, delivery)
    delivery.replace(merged)
    print(f"compress OK: {before} -> {after} (reduced={reduced})", flush=True)

    # ---- 6. final validation + packaging ------------------------------------- #
    render.validate_mp4(merged)
    metadata = out_dir / "metadata.txt"
    stage_b_run.write_metadata_txt(plan, metadata)
    final_zip = out_dir / "clipforge-manual-smoke-final.zip"
    stage_b_run.package_final_zip(merged, metadata, production_json, final_zip)

    probe = common.probe_json(merged)
    v = [s for s in probe["streams"] if s.get("codec_type") == "video"][0]
    print(
        f"FINAL: {v['width']}x{v['height']} {v['codec_name']}/{v['pix_fmt']} "
        f"fps={v['r_frame_rate']} duration={probe['format']['duration']}s",
        flush=True,
    )
    print("SMOKE TEST PASSED", flush=True)

    if "--keep" in sys.argv:
        print(f"artifacts kept at: {tmp}", flush=True)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
