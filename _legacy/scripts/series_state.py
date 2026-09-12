#!/usr/bin/env python3
"""Derive a safe, sequential Stage A continuation from persisted series files."""
from __future__ import annotations
import json
import sys
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: series_state.py <repo-root> <completed-job-id>")
    root = Path(sys.argv[1]).resolve()
    job_id = sys.argv[2]
    plan = load(root / "jobs" / job_id / "production.json")
    request_path = root / "jobs" / job_id / "stage_a_request.json"
    if not request_path.exists():
        # Jobs dispatched outside the normal Stage A trigger (e.g. manual
        # jobs) have no persisted original request, so there is nothing to
        # derive a continuation from. Treat as not eligible rather than
        # crashing the workflow.
        print(json.dumps({"continue": False}))
        return
    request = load(request_path)
    series_id = str(plan.get("series_id") or "").strip()
    if not series_id or plan.get("series_final") is True:
        print(json.dumps({"continue": False}))
        return
    part = plan.get("series_part")
    end = plan.get("series_end_seconds")
    if not isinstance(part, int) or part < 1 or not isinstance(end, int) or end < 0:
        raise SystemExit("Completed series production.json has invalid part metadata.")
    source = str(request.get("series_source_job_id") or "").strip()
    if not source:
        raise SystemExit("Series request is missing its original source job id.")
    prior = []
    for candidate in (root / "jobs").glob("*/production.json"):
        try:
            document = load(candidate)
        except Exception:
            continue
        if document.get("series_id") == series_id and isinstance(document.get("series_part"), int):
            prior.append((document["series_part"], candidate.parent.name, str(document.get("series_summary") or "").strip()))
    prior.sort()
    if not prior or prior[-1][0] != part:
        raise SystemExit("Series history is incomplete or contains an unexpected later part.")
    context = "\n".join(f"Part {number}: {summary}" for number, _, summary in prior if summary) or "(No prior summaries.)"
    next_id = f"{series_id}-p{part + 1}"
    if len(next_id) > 120:
        raise SystemExit("Series job id would exceed the safe limit.")
    out = {
        "continue": True,
        "job_id": next_id,
        "video_url": str(request.get("video_url") or ""),
        "torrent_file_index": str(request.get("torrent_file_index") or ""),
        "whisper_model": str(request.get("whisper_model") or "base"),
        "language": str(request.get("language") or "auto"),
        "target_duration_seconds": str(request.get("target_duration_seconds") or "120"),
        # Series Mode has no editorial-focus override. Each part is bounded by
        # its persisted source window and continuity context instead.
        "focus": "",
        "automatic_mode": str(request.get("automatic_mode") or "false"),
        "series_mode": "true",
        "series_id": series_id,
        "series_source_job_id": source,
        "series_part": str(part + 1),
        "series_start_seconds": str(end),
        "series_context": context[:8000],
    }
    print(json.dumps(out, separators=(",", ":")))


if __name__ == "__main__":
    main()
