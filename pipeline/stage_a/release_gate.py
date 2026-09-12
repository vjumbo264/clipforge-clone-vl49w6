"""ClipForge — Stage A release-outcome classification (bug-69).

After the full-bundle ``softprops/action-gh-release`` attempt fails, the
workflow must decide whether the failure is specifically GitHub's 2 GiB
per-asset limit on ``source_input.bin`` (the ONLY failure allowed to drop the
source and retry without it) or any other failure (which must re-fail rather
than silently ship a source-less release).

The rule (see ``.github/workflows/stage-a.yml``): treat it as the size limit
ONLY when BOTH hold —

  1. ``source_input.bin`` on disk exceeds 2 GiB, AND
  2. every asset that sorts BEFORE it in the upload order is already present
     in the release. ``work/bundle/*`` uploads in sorted order and
     ``source_input.bin`` sorts second-to-last, so the pre-source prefix
     (``00_READ_THIS_FIRST.txt`` .. ``screenshots.zip``, plus
     ``event_composites.zip`` when present) MUST have landed before the
     source failed. ``transcript.json`` (and ``manifest.json`` when the
     action crashed before creating the release) sort AFTER the source and
     are allowed to be missing — the idempotent retry fills them in.

A missing pre-source asset with an oversized source is NOT this path (it
means the upload died earlier for a different reason), and a failure with a
within-limit source is obviously not this path. Both re-fail.

Exit codes (CLI): 0 = classified as the oversized-source size-limit failure
(proceed with the source-less retry); 1 = not this failure (re-fail).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

#: GitHub's hard per-file release-asset limit.
LIMIT_BYTES = 2 * 1024 ** 3  # 2 GiB

#: Assets that sort BEFORE source_input.bin in the work/bundle/* glob order.
#: event_composites.zip is added dynamically when it was actually produced.
PREFIX_ASSETS = frozenset({
    "00_READ_THIS_FIRST.txt",
    "key_moments.json",
    "scene_index.json",
    "screenshots.zip",
})


def fetch_release_asset_names(tag: str, repo: str) -> set[str]:
    """Return the asset names currently on release ``tag`` (empty if the
    release was never created — the action can crash on the source before
    creating it)."""
    try:
        rel: dict[str, Any] = json.loads(subprocess.run(
            ["gh", "api", f"repos/{repo}/releases/tags/{tag}"],
            capture_output=True, text=True, check=True).stdout)
    except subprocess.CalledProcessError:
        return set()
    return {a.get("name") for a in rel.get("assets", [])}


def classify(tag: str, *, repo: str, bundle_dir: str = "work/bundle",
             fetch_assets=None) -> bool:
    """True iff the failed full-bundle upload is the 2 GiB size-limit path."""
    # Resolve at call time (not def time) so tests can monkeypatch
    # fetch_release_asset_names without injecting fetch_assets explicitly.
    if fetch_assets is None:
        fetch_assets = fetch_release_asset_names
    src = os.path.join(bundle_dir, "source_input.bin")
    src_size = os.path.getsize(src) if os.path.exists(src) else 0
    present = set(fetch_assets(tag, repo))
    prefix = set(PREFIX_ASSETS)
    if os.path.exists(os.path.join(bundle_dir, "event_composites.zip")):
        prefix.add("event_composites.zip")
    missing_prefix = prefix - present
    print(f"source_input.bin size: {src_size} bytes (2 GiB limit: {LIMIT_BYTES})")
    print(f"published assets: {sorted(present) or 'none'}")
    if src_size > LIMIT_BYTES and not missing_prefix:
        print("CLASSIFIED: oversized-source release failure — retrying WITHOUT source_input.bin.")
        return True
    print("NOT a size-limit failure "
          f"(source_oversized={src_size > LIMIT_BYTES}, missing_prefix={sorted(missing_prefix)}). "
          "Refusing to drop the source; re-failing.", file=sys.stderr)
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="ClipForge Stage A release-outcome gate (bug-69)")
    ap.add_argument("tag")
    ap.add_argument("--bundle-dir", default="work/bundle")
    args = ap.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]
    ok = classify(args.tag, repo=repo, bundle_dir=args.bundle_dir)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()


__all__ = ["classify", "fetch_release_asset_names", "LIMIT_BYTES", "PREFIX_ASSETS"]
