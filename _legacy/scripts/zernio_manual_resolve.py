#!/usr/bin/env python3
"""Resolve the correct Zernio dispatch for a manual, on-demand publish.

Given a completed ClipForge job id, decide whether zernio-publish.yml should
be invoked with action=retry (there is already a queue entry with at least
one post id for this job) or action=publish (no attempt has ever been queued
for this job, e.g. the automatic Stage B dispatch never fired or failed
before creating any post). This mirrors exactly what Stage B's automatic
dispatch step already computes for a fresh publish, so a manual run behaves
identically to what would have happened automatically.

Prints one tab-separated line: ACTION\tMODE\tTIMEZONE\tTARGETS_JSON_B64\tPOST_ID
POST_ID is empty for a fresh publish.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from zernio_targets import automatic_fields, TargetValidationError  # noqa: E402


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: zernio_manual_resolve.py <repo-root> <job-id> <settings-path>")
    root = Path(sys.argv[1]).resolve()
    job_id = sys.argv[2]
    settings_path = Path(sys.argv[3])

    if not (root / "jobs" / job_id / "production.json").exists():
        raise SystemExit(f"No completed job found at jobs/{job_id}/production.json")

    queue_path = root / "branding" / "zernio_queue.json"
    existing_post_id = ""
    if queue_path.exists():
        try:
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Could not read {queue_path}: {exc}") from exc
        for item in queue.get("items", []) if isinstance(queue, dict) else []:
            if isinstance(item, dict) and str(item.get("job_id") or "") == job_id:
                post_ids = item.get("post_ids")
                if isinstance(post_ids, list) and post_ids:
                    existing_post_id = str(post_ids[0])
                break

    if existing_post_id:
        # A prior attempt exists for this job; retry that same post rather
        # than creating a duplicate one.
        print(f"retry\t\t\t\t{existing_post_id}")
        return

    if not settings_path.exists():
        raise SystemExit("Zernio is not configured (branding/zernio_settings.json is missing).")
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        skip, mode, timezone, targets_b64 = automatic_fields(settings)
    except (OSError, json.JSONDecodeError, TargetValidationError) as exc:
        raise SystemExit(f"Could not resolve Zernio settings: {exc}") from exc
    if skip == "true" or not mode or not targets_b64:
        raise SystemExit(
            "Zernio automatic publishing is disabled or has no target accounts configured; "
            "nothing to publish to."
        )
    print(f"publish\t{mode}\t{timezone}\t{targets_b64}\t")


if __name__ == "__main__":
    main()
