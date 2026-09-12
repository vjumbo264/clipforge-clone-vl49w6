#!/usr/bin/env python3
"""
Write / update jobs/<job-id>/status.json for a given job.

Usage:
    python write_status.py <job-id> <stage> [--message MSG]
                           [--release-tag TAG]
                           [--release-url URL]
                           [--asset name=url ...]
                           [--extra key=value ...]
                           [--out-dir jobs]

Stages recognized by the frontend contract:
    queued
    awaiting_torrent_selection
    stage_a_running
    automatic_analysis_running
    awaiting_json_upload
    stage_b_queued
    stage_b_running
    stage_b_cancelling
    cancelled
    complete
    error
"""
import argparse
import json
import os
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("job_id")
    ap.add_argument("stage")
    ap.add_argument("--message", default="")
    ap.add_argument("--release-tag", default="")
    ap.add_argument("--release-url", default="")
    ap.add_argument("--asset", action="append", default=[], help="name=url")
    ap.add_argument("--extra", action="append", default=[], help="key=value")
    ap.add_argument("--out-dir", default="jobs")
    args = ap.parse_args()

    assets = {}
    for a in args.asset:
        if "=" not in a:
            continue
        k, v = a.split("=", 1)
        assets[k.strip()] = v.strip()

    extra = {}
    for e in args.extra:
        if "=" not in e:
            continue
        k, v = e.split("=", 1)
        extra[k.strip()] = v.strip()

    job_dir = os.path.join(args.out_dir, args.job_id)
    os.makedirs(job_dir, exist_ok=True)
    path = os.path.join(job_dir, "status.json")

    prior = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                prior = json.load(f)
        except Exception:
            prior = {}

    created_at = prior.get("created_at_epoch") or int(time.time())

    payload = {
        "job_id": args.job_id,
        "stage": args.stage,
        "message": args.message,
        "release_tag": args.release_tag or prior.get("release_tag", ""),
        "release_url": args.release_url or prior.get("release_url", ""),
        "assets": {**prior.get("assets", {}), **assets},
        "created_at_epoch": created_at,
        "updated_at_epoch": int(time.time()),
        "expires_at_epoch": created_at + 12 * 3600,
        **prior.get("extra", {}),
        **extra,
    }
    # Move any extra-only keys under 'extra' for cleanliness.
    known = {"job_id", "stage", "message", "release_tag", "release_url", "assets",
             "created_at_epoch", "updated_at_epoch", "expires_at_epoch", "extra"}
    payload_extra = {k: v for k, v in payload.items() if k not in known}
    for k in list(payload_extra.keys()):
        del payload[k]
    payload["extra"] = payload_extra

    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Wrote {path}: stage={args.stage}")


if __name__ == "__main__":
    main()
