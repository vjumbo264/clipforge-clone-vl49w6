"""Shared helpers for ClipForge Stage B modules.

This module deliberately contains no step-specific logic. It provides:

* ffmpeg / ffprobe wrappers with consistent, diagnosable failure reporting;
* the Stage B boundary contract — loading a ``production.json``, re-validating
  it against the single source of truth (``pipeline.plan.schema``), and
  normalizing it into the flat internal shape every step consumes;
* readers for the non-secret branding JSON files (narrator, creator watermark,
  music default) that fail safe to neutral defaults.

Nothing here imports from ``_legacy``; logic is ported and adapted.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from pipeline.plan import schema as plan_schema


# --------------------------------------------------------------------------- #
# bug-57: status.series sync from production.json                             #
# --------------------------------------------------------------------------- #

def plan_series_block(document: dict[str, Any]) -> dict[str, Any]:
    """Derive the status.json ``series`` block from a production.json document.

    Accepts the raw (pre-validation) production.json so callers can sync
    ``jobs/<id>/status.json`` even when the plan later fails boundary
    validation — the bot needs ``series.enabled`` to render the task header
    and series affordances regardless of whether the render itself proceeds.

    A plan counts as a series plan whenever it declares series data in either
    documented shape: the nested §7.3 ``series`` object OR any legacy flat
    ``series_*`` sibling (same test ``pipeline.plan.schema._extract_series``
    uses). All status fields are derived from the plan with the exact
    per-field nested-wins precedence of ``normalize_plan`` — never left at
    their zeroed defaults. Non-series plans return the all-off block, and
    individual values are defensively coerced (bool/str/int) so a malformed
    plan can never crash a status write.
    """
    if not isinstance(document, dict):
        return {"enabled": False, "series_id": "", "part": 0,
                "start_seconds": 0, "is_final": False}
    # bug-70: a present-but-EMPTY nested series dict must NOT count as
    # "declares series data" — normalize_plan previously always emitted
    # series={} for a non-series plan (now fixed to emit series=None), but
    # this function must stay correct on its own regardless of what shape any
    # caller hands it, since it's also called directly against a raw,
    # pre-normalization document elsewhere in this file. An empty dict has no
    # actual series content and must be treated the same as it being absent.
    nested_series = document.get("series")
    has_nested = isinstance(nested_series, dict) and bool(nested_series)
    declares = has_nested or any(
        flat_key in document for flat_key in plan_schema._NESTED_TO_FLAT.values()
    )
    if not declares:
        return {"enabled": False, "series_id": "", "part": 0,
                "start_seconds": 0, "is_final": False}
    # Nested series object first, legacy flat ``series_*`` siblings fill any
    # field the nested object did not define (same per-field nested-wins
    # precedence as ``normalize_plan``). Deliberately self-contained so it
    # also works on a raw document whose non-series fields are unvalidated.
    series: dict[str, Any] = {}
    nested = document.get("series")
    if isinstance(nested, dict):
        series = dict(nested)
    for flat_key, nested_key in (
        ("series_id", "series_id"),
        ("series_part", "part"),
        ("series_start_seconds", "start_seconds"),
        ("series_final", "is_final"),
    ):
        if nested_key not in series and flat_key in document:
            series[nested_key] = document[flat_key]
    return {
        "enabled": True,
        "series_id": str(series.get("series_id") or ""),
        "part": _as_nonneg_int(series.get("part")),
        "start_seconds": _as_nonneg_int(series.get("start_seconds")),
        "is_final": bool(series.get("is_final", False)),
    }


def _as_nonneg_int(value: Any) -> int:
    """Best-effort non-negative int for series part/start_seconds."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# Repository-relative locations                                                #
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parents[2]
ASSETS_DIR = REPO_ROOT / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
BRANDING_DIR = REPO_ROOT / "branding"

COOLVETICA_FONT = FONTS_DIR / "Coolvetica.ttf"
# bug-59: the grade is now the "vibrant multi-hue glow" look derived from the
# 960x540 8x8 gradient-tile reference image. That reference is a mood board,
# NOT a valid HALD CLUT identity (wrong dimensions/layout), so it is not
# applied literally — pipeline/stage_b/build_glow_lut.py synthesises a real
# 64^3 3D LUT encoding its character (warm S-curve contrast, ~1.5x vibrance
# saturation, +18deg saturation-gated hue shift) and writes both assets below.
LUT_GRID_SOURCE = ASSETS_DIR / "vibrant_glow_grade.cube"
LUT_HALD_ASSET = ASSETS_DIR / "vibrant_glow_color_cube_l8.png"

TTS_SETTINGS_PATH = BRANDING_DIR / "tts_settings.json"
CREATOR_WATERMARK_PATH = BRANDING_DIR / "creator_watermark.json"
MUSIC_DEFAULT_PATH = BRANDING_DIR / "music_default.json"


# --------------------------------------------------------------------------- #
# Process helpers                                                              #
# --------------------------------------------------------------------------- #

class StageBError(RuntimeError):
    """A user-safe Stage B failure (no secrets, no stack noise)."""


def run(cmd: list[str], description: str) -> subprocess.CompletedProcess[str]:
    """Run ``cmd``, echoing it, and raise ``StageBError`` with a stderr tail."""
    print(f"$ {' '.join(cmd)}", flush=True)
    try:
        return subprocess.run(
            cmd,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise StageBError(f"{description}: required tool not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or "")[-1500:] if exc.stderr else "no diagnostic output"
        raise StageBError(f"{description} failed: {tail}") from exc


def sh(cmd: list[str]) -> None:
    """Run a command streaming output (for long ffmpeg renders)."""
    print(f"$ {' '.join(cmd)}", flush=True)
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:
        raise StageBError(f"required tool not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise StageBError(f"command failed (exit {exc.returncode}): {cmd[0]}") from exc


def probe_duration_seconds(path: str | Path) -> float:
    """Return a finite positive media duration in seconds."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise StageBError(f"ffprobe failed on {path}: {out.stderr[-800:]}")
    raw = out.stdout.strip()
    try:
        duration = float(raw)
    except ValueError as exc:
        raise StageBError(f"invalid duration for {path}: {raw!r}") from exc
    if not (duration == duration) or duration <= 0:  # NaN / non-positive guard
        raise StageBError(f"invalid duration for {path}: {raw!r}")
    return duration


def probe_video_size(path: str | Path) -> tuple[int, int]:
    """Return ``(width, height)`` of the first video stream."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise StageBError(f"ffprobe failed on {path}: {out.stderr[-800:]}")
    try:
        streams = json.loads(out.stdout).get("streams", [])
        width = int(streams[0]["width"])
        height = int(streams[0]["height"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StageBError(f"no video stream in {path}") from exc
    return width, height


def probe_json(path: str | Path) -> dict:
    """Return the full ffprobe JSON for a media file."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries",
            "stream=codec_type,codec_name,profile,level,pix_fmt,width,height,"
            "sample_rate,channels,r_frame_rate,has_b_frames,start_time,sample_aspect_ratio,time_base,"
            "avg_frame_rate"
            ,
            "-show_entries", "format=format_name,format_long_name,duration,bit_rate,start_time,size",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise StageBError(f"ffprobe failed on {path}: {out.stderr[-800:]}")
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        raise StageBError(f"ffprobe returned unparseable JSON for {path}") from exc


# --------------------------------------------------------------------------- #
# Production plan loading + boundary validation                                #
# --------------------------------------------------------------------------- #

#: Internal normalized per-cut shape produced by :func:`load_production_plan`.
#: ``voiceover_text`` is always populated (legacy ``raw_narration`` accepted).
Cut = dict[str, Any]


def normalize_plan(document: dict[str, Any]) -> dict[str, Any]:
    """Normalize a validated production plan into the flat Stage B shape.

    Accepts both documented series shapes (nested ``series`` object OR flat
    ``series_*`` siblings; nested wins per field) and both narration field
    names (``voiceover_text`` OR legacy ``raw_narration``), mirroring
    ``pipeline/plan/schema.py``. Returns a NEW dict; the input is untouched.

    Callers must have already run ``validate_production_plan`` — this function
    assumes the document is valid and only restructures it.
    """
    nested = document.get("series")
    flat_map = {
        "series_id": "series_id",
        "series_part": "part",
        "series_start_seconds": "start_seconds",
        "series_end_seconds": "end_seconds",
        "series_final": "is_final",
        "series_summary": "summary",
    }
    # bug-70: a plan that declares NO series data at all (nested absent/null,
    # no flat series_* siblings) must normalize to series=None, not series={}.
    # An empty-but-present dict is indistinguishable from "this plan IS a
    # series plan with everything defaulted" to any downstream reader that
    # only checks isinstance(..., dict) (see plan_series_block below, and the
    # historical schema._extract_series bug-49/bug-56 precedent) — that
    # ambiguity caused a plain one-off task's status.json to be written with
    # series.enabled=True (and the bot to render a bogus "Series: part 1"
    # line on a task the operator explicitly created with series OFF).
    is_series_plan = isinstance(nested, dict) or any(
        flat_key in document for flat_key in flat_map
    )
    series: dict[str, Any] = {}
    if isinstance(nested, dict):
        series = dict(nested)
    # Flat legacy siblings fill any field the nested object did not define.
    for flat_key, nested_key in flat_map.items():
        if nested_key not in series and flat_key in document:
            series[nested_key] = document[flat_key]

    cuts: list[Cut] = []
    for raw_cut in document.get("cuts") or []:
        cut = dict(raw_cut)
        narration = cut.get("voiceover_text")
        if not (isinstance(narration, str) and narration.strip()):
            narration = cut.get("raw_narration")
        cut["voiceover_text"] = str(narration).strip()
        cut["start_seconds"] = int(cut["start_seconds"])
        cut["end_seconds"] = int(cut["end_seconds"])
        cuts.append(cut)
    cuts.sort(key=lambda c: c["start_seconds"])

    return {
        "version": document.get("version", 2),
        "job_id": document.get("job_id", ""),
        "title": str(document.get("title") or "").strip(),
        "video_duration_seconds": int(document["video_duration_seconds"]),
        "target_total_duration_seconds": int(document["target_total_duration_seconds"]),
        "cuts": cuts,
        "hashtags": list(document.get("hashtags") or []),
        "youtube_tags": list(document.get("youtube_tags") or []),
        "series": series if is_series_plan else None,
        # Pass-through of any unknown top-level fields (forward compatibility).
        "_extra": {
            k: v
            for k, v in document.items()
            if k
            not in {
                "version", "job_id", "title", "video_duration_seconds",
                "target_total_duration_seconds", "cuts", "hashtags",
                "youtube_tags", "series",
                *flat_map.keys(),
            }
        },
    }


def load_production_plan(path: str | Path) -> dict[str, Any]:
    """Read, RE-VALIDATE (§13 invariant #5), and normalize a production.json.

    Raises ``StageBError`` listing every validation error when the document is
    invalid — Stage B must refuse to render untrusted plans.
    """
    plan_path = Path(path)
    if not plan_path.is_file():
        raise StageBError(f"production.json not found: {plan_path}")
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StageBError(f"could not read production.json: {exc}") from exc

    document, errors = plan_schema.parse_and_validate_production_plan(text)
    if errors:
        raise StageBError(
            "production.json failed Stage B boundary validation:\n- "
            + "\n- ".join(errors)
        )
    assert document is not None  # guaranteed when errors is empty
    return normalize_plan(document)


# --------------------------------------------------------------------------- #
# Branding readers (non-secret, fail-safe)                                     #
# --------------------------------------------------------------------------- #

def read_branding_json(path: Path) -> dict[str, Any]:
    """Best-effort read of a branding JSON file; ``{}`` on any problem."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def load_creator_watermark_name(path: Path = CREATOR_WATERMARK_PATH) -> str:
    """Return the normalized creator watermark name, or ``""`` when absent.

    An empty result is an explicit no-op for the watermark step — it never
    attempts to render empty text.
    """
    doc = read_branding_json(path)
    raw = doc.get("creator_name", "")
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.split())[:64]


def load_music_default(path: Path = MUSIC_DEFAULT_PATH) -> dict[str, Any]:
    """Return the saved default-music record, or ``{}`` when none is saved."""
    return read_branding_json(path)


def resolve_music_ref(ref: str, *, job_id: str, work_dir: Path) -> Path | None:
    """Resolve a ``path:``/``url:``/``asset:``-less music ref to a local file.

    Only ``path:<repo-relative>`` is resolved locally here; ``url:`` and
    ``asset:`` forms are resolved by the workflow before this step runs and
    handed in as a local path. Returns ``None`` for an empty ref (no music).
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    ref = ref.replace("<JOB_ID>", job_id)
    if ref.startswith("path:"):
        src = REPO_ROOT / ref[len("path:"):]
        if not src.is_file():
            raise StageBError(f"music file not found at repo path: {src}")
        dest = work_dir / "music_input"
        dest = dest.with_suffix(src.suffix or ".mp3")
        shutil.copyfile(src, dest)
        return dest
    # A pre-downloaded local file path.
    candidate = Path(ref)
    if candidate.is_file():
        return candidate
    raise StageBError(f"unsupported or missing music_ref: {ref}")


# --------------------------------------------------------------------------- #
# Small validators used across steps                                           #
# --------------------------------------------------------------------------- #

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def sanitize_job_id(raw: str) -> str:
    """Normalize a workflow-supplied job id to the §6.3 character set."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", str(raw)).strip("-")
    if not cleaned or len(cleaned) > 120 or not _JOB_ID_RE.match(cleaned):
        raise StageBError(f"invalid job_id: {raw!r}")
    return cleaned
