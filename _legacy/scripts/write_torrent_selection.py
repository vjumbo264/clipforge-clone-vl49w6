#!/usr/bin/env python3
"""Create or update the durable Stage A torrent-video selection record.

The record is intentionally metadata-only: it never contacts trackers or peers.
It lets the browser render a multi-video choice after a reload, while preserving
the original Stage A settings until the user selects the one video to retrieve.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import torrent_source


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("torrent", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--whisper-model", default="base")
    ap.add_argument("--language", default="auto")
    ap.add_argument("--target-duration-seconds", default="120")
    ap.add_argument("--focus", default="")
    ap.add_argument("--selected-index", default="")
    args = ap.parse_args()

    metadata = torrent_source.inspect_torrent(args.torrent)
    candidates = metadata["video_candidates"]
    if not candidates:
        raise SystemExit("Torrent contains no supported video candidates")

    selected_index = None
    if args.selected_index:
        selected_index = torrent_source.select_torrent_video(
            metadata, int(args.selected_index)
        )["index"]

    payload = {
        "version": 1,
        "job_id": args.job_id,
        "torrent_name": metadata["name"],
        "video_candidates": candidates,
        "selected_index": selected_index,
        "stage_a_inputs": {
            "whisper_model": args.whisper_model,
            "language": args.language,
            "target_duration_seconds": str(args.target_duration_seconds),
            "focus": args.focus,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(f"Wrote {args.output}: {len(candidates)} candidate(s), "
          f"selected_index={selected_index!r}")


if __name__ == "__main__":
    main()
