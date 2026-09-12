#!/usr/bin/env python3
"""Derive a safe, sequential Series Mode continuation from persisted jobs.

Adapted port of ``_legacy/scripts/series_state.py`` to the new contracts:

* Requests are read from ``jobs/<job_id>/stage-a-request.json`` (nested §7.1
  shape) instead of the legacy flat v1 request.
* Plans are read from ``jobs/<job_id>/production.json``; BOTH the nested
  ``series`` object (ARCHITECTURE.md §7.3) and the legacy flat ``series_*``
  sibling fields are accepted, with nested winning per-field — the exact
  normalization rule shared by ``pipeline/plan/schema.py`` and
  ``bot/src/plan.js``.
* The emitted continuation payload is the nested §7.1 request document body
  (without ``version``/``job_id``/``saved_at_epoch`` — the caller stamps
  those), NOT the legacy flat input list. ``.github/workflows/stage-b.yml``
  persists it as the next part's ``stage-a-request.json`` and dispatches
  Stage A with the new workflow's only inputs (``job_id``, ``code_ref``).

Derivation semantics are preserved from the legacy tool:

* a job without a persisted request is not eligible (``continue: false``);
* a plan without a ``series_id`` or with ``series_final is true`` stops the
  chain;
* ``series_part``/``series_end_seconds`` must be valid integers;
* the request must name its original ``series.source_job_id``;
* the persisted history must be complete and end exactly at the completed
  part (a gap or an unexpected later part is a hard error, not a guess);
* the context string is ``Prior events (Part N): <summary>`` lines joined
  with newlines, capped at 8000 chars (bug-56: the leading token is
  deliberately not a bare ``Part N:`` so an AI author cannot copy that
  number into its own production.json ``series.part`` field);
* the next job id ``<series_id>-p<N+1>`` must satisfy the §6.3 identity rule
  (character set ``[A-Za-z0-9._-]``, max 120 chars);
* music carries forward only for the shared default or a permanent
  audio-library track — a part-1 one-off upload (``path:jobs/<part1>/…``)
  cannot be reused by another job and falls back to the shared default.

Usage (stage-b.yml continuation step)::

    python -m pipeline.plan.series <repo-root> <completed-job-id>

Prints the JSON payload on stdout. ``{\"continue\": false}`` means no
continuation should be dispatched.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# §6.3 job identity rule (shared with bot/src/jobs.js and pipeline/status.py).
JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")

# Mirrors pipeline/plan/music.py — a permanent library track may carry forward
# to the next part; a one-off per-job upload may not.
LIBRARY_REF_PREFIX = "path:audio-library/"
WHISPER_MODELS = {"tiny", "base", "small"}
MAX_JOB_ID_LENGTH = 120
MAX_CONTEXT_CHARS = 8000

# Nested series keys -> legacy flat sibling names (same mapping as
# schema.py's _SERIES_NESTED_TO_FLAT — keep in sync).
_SERIES_NESTED_TO_FLAT = {
    "series_id": "series_id",
    "part": "series_part",
    "start_seconds": "series_start_seconds",
    "end_seconds": "series_end_seconds",
    "is_final": "series_final",
    "summary": "series_summary",
}


class SeriesDerivationError(Exception):
    """Raised when persisted series state is inconsistent (hard error)."""


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_series(document: dict[str, Any]) -> dict[str, Any]:
    """Normalize series metadata from a production.json document.

    Accepts the nested §7.3 object and the legacy flat ``series_*`` siblings;
    nested wins per-field when both are present (same rule as schema.py).
    """
    nested = document.get("series")
    nested = nested if isinstance(nested, dict) else {}
    values: dict[str, Any] = {}
    for nested_key, flat_key in _SERIES_NESTED_TO_FLAT.items():
        value = nested.get(nested_key)
        if value is None:
            value = document.get(flat_key)
        values[nested_key] = value
    return values


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def derive_next_part(root: Path, job_id: str) -> dict[str, Any]:
    """Derive the continuation payload for the job that just completed.

    Returns ``{"continue": False}`` when the job is not eligible; raises
    ``SeriesDerivationError`` when the persisted series state is corrupt.
    """
    root = Path(root).resolve()
    jobs_dir = root / "jobs"

    plan_path = jobs_dir / job_id / "production.json"
    if not plan_path.exists():
        # Stage B writes production.json into the repo before rendering; its
        # absence means there is nothing trustworthy to derive from.
        return {"continue": False}
    plan = _load(plan_path)

    request_path = jobs_dir / job_id / "stage-a-request.json"
    if not request_path.exists():
        # Jobs dispatched outside the normal Stage A trigger have no persisted
        # original request, so there is nothing to derive a continuation from.
        # Treat as not eligible rather than crashing the workflow.
        return {"continue": False}
    request = _load(request_path)

    req_series = request.get("series") if isinstance(request.get("series"), dict) else {}
    req_options = request.get("options") if isinstance(request.get("options"), dict) else {}
    req_source = request.get("source") if isinstance(request.get("source"), dict) else {}
    req_music = request.get("music") if isinstance(request.get("music"), dict) else {}

    if req_series.get("enabled") is not True:
        return {"continue": False}

    plan_series = _extract_series(plan)
    series_id = str(plan_series.get("series_id") or "").strip()
    if not series_id or plan_series.get("is_final") is True:
        return {"continue": False}

    part = plan_series.get("part")
    end = plan_series.get("end_seconds")
    if not _is_int(part) or part < 1 or not _is_int(end) or end < 0:
        raise SeriesDerivationError(
            "Completed series production.json has invalid part metadata."
        )

    source_job = str(req_series.get("source_job_id") or "").strip()
    if not source_job:
        raise SeriesDerivationError(
            "Series request is missing its original source job id."
        )

    # Walk every persisted plan for this series. The history must be complete
    # and must end exactly at the part that just completed — otherwise the
    # derived start window would silently overlap or skip source footage.
    prior: list[tuple[int, str, str]] = []
    for candidate in sorted(jobs_dir.glob("*/production.json")):
        try:
            document = _load(candidate)
        except Exception:
            continue
        values = _extract_series(document)
        if values.get("series_id") == series_id and _is_int(values.get("part")):
            prior.append(
                (
                    values["part"],
                    candidate.parent.name,
                    str(values.get("summary") or "").strip(),
                )
            )
    prior.sort(key=lambda entry: entry[0])
    if (
        not prior
        or prior[-1][0] != part
        # A gap anywhere in the chain means a part's plan was never persisted;
        # deriving from incomplete history would silently skip source footage.
        or [number for number, _, _ in prior] != list(range(1, part + 1))
    ):
        raise SeriesDerivationError(
            "Series history is incomplete or contains an unexpected later part."
        )

    # bug-56: prefix every summary with ``Prior events (Part N):`` rather
    # than a bare ``Part N:``. The bare prefix inside the continuity block
    # was mistakable by an AI author for the current-part number stated
    # elsewhere in the prompt, and correlated with production.json being
    # submitted with the wrong series.part value.
    context = "\n".join(
        f"Prior events (Part {number}): {summary}"
        for number, _, summary in prior
        if summary
    ) or "(No prior summaries.)"

    next_part = part + 1
    next_id = f"{series_id}-p{next_part}"
    if not JOB_ID_RE.match(next_id) or len(next_id) > MAX_JOB_ID_LENGTH:
        raise SeriesDerivationError("Series job id would exceed the safe limit.")

    # Music carry-forward: the shared default is resolved at dispatch time
    # (source 'default' with an empty ref), and a permanent audio-library ref
    # is valid for every job. A one-off job upload (path:jobs/<part>/…) can
    # never be reused by another job, so it falls back to the shared default —
    # the same policy the legacy stage-b.yml continuation step enforced.
    music_source = str(req_music.get("source") or "none")
    music_ref = str(req_music.get("ref") or "")
    if music_source not in {"none", "default", "explicit_library", "job_upload"}:
        music_source, music_ref = "none", ""
    if music_source == "job_upload":
        music_source, music_ref = "default", ""
    elif music_source == "explicit_library" and not music_ref.startswith(
        "audio-library/"
    ):
        # buildStageARequest stores the library-track REPO path in music.ref
        # (the 'path:' prefix is added at dispatch time); anything else is not
        # a reusable library track.
        music_source, music_ref = "default", ""

    whisper_model = str(req_options.get("whisper_model") or "base")
    if whisper_model not in WHISPER_MODELS:
        whisper_model = "base"
    target_duration = req_options.get("target_duration_seconds")
    if not _is_int(target_duration) or target_duration < 1:
        target_duration = 120
    start_seconds = req_series.get("start_seconds")
    if not _is_int(start_seconds) or start_seconds < 0:
        start_seconds = 0

    return {
        "continue": True,
        "job_id": next_id,
        "request": {
            "source": {
                "kind": str(req_source.get("kind") or "url"),
                "value": str(req_source.get("value") or ""),
                **(
                    {"torrent_file_index": req_source["torrent_file_index"]}
                    if req_source.get("torrent_file_index") not in (None, "")
                    else {}
                ),
            },
            "options": {
                "whisper_model": whisper_model,
                # bug-65: default is auto-detect ("auto"), matching the new
                # pipeline default (translate_to_english + auto-detect).
                "language": str(req_options.get("language") or "auto"),
                "target_duration_seconds": target_duration,
                # Series Mode has no editorial-focus override. Each part is
                # bounded by its persisted source window and continuity
                # context instead.
                "focus": "",
                "enable_vision_assist": req_options.get("enable_vision_assist") is not False,
            },
            "mode": "manual",
            "series": {
                "enabled": True,
                "series_id": series_id,
                "source_job_id": source_job,
                "part": next_part,
                "start_seconds": end,
                "context": context[:MAX_CONTEXT_CHARS],
            },
            "music": {"ref": music_ref, "source": music_source},
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: python -m pipeline.plan.series <repo-root> <completed-job-id>", file=sys.stderr)
        return 2
    try:
        payload = derive_next_part(Path(args[0]), args[1])
    except SeriesDerivationError as exc:
        print(f"series continuation error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
