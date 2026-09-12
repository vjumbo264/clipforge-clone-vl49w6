#!/usr/bin/env python3
"""Plan one durable ClipForge smart-schedule slot from repo-backed state."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from zernio_state import default_queue, plan_smart_schedule, read_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("settings")
    parser.add_argument("queue")
    parser.add_argument("--external-posts", default="", help="Optional JSON array of existing native Zernio scheduled posts")
    args = parser.parse_args()
    settings = read_json(args.settings, {})
    queue = read_json(args.queue, default_queue())
    external = read_json(args.external_posts, []) if args.external_posts else []
    if not isinstance(external, list):
        external = []
    print(json.dumps(plan_smart_schedule(settings, queue, external_posts=external), ensure_ascii=False))


if __name__ == "__main__":
    main()
