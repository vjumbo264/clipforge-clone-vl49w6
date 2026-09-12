#!/usr/bin/env python3
"""
Stage B — per-scene branding orchestrator.

Runs AFTER cut_and_produce.py (and after enhance_scenes.py when that
step is enabled) and BEFORE the ffprobe validation step in
stage-b.yml. It accepts either the current single merged final.mp4 or
the legacy per-scene scene_*.mp4 layout in the scenes directory. For
each input MP4 it composites the video into the branded 1080x1200 (10:9)
template via brand_scene.py and rewrites the original file in place,
using the exact atomic-swap pattern established by enhance_scenes.py:

    <name>.mp4  --brand-->  <name>.branded.mp4  --validate-->  os.replace()

The swap only happens once the branded file exists AND has passed
brand_scene.py's own mobile-safe validation (1080x1200, H.264 High@L4.0,
yuv420p, has_b_frames=0, start_time ~0, exactly 1 video + 1 audio
stream, valid ftyp). A failed scene therefore leaves the original
untouched and fails the whole run — a half-branded output set can never
ship. No *.branded.mp4 temp files remain after a successful run: every
temp is consumed by os.replace() (or removed in the failure cleanup
below), which the "no leftover temp files" check at the end of this
script asserts explicitly.

Three behavior paths (all intentional, all exit 0):

  1. Branding DISABLED via --no-enabled
     ---------------------------------
     The workflow's `brand` dispatch input maps 1:1 to the site's
     on/off toggle (same contract as the `enhance` input). 'false'
     short-circuits this script to a no-op: no scene files are read or
     written, and the rest of the workflow stays structurally identical
     whether branding is on or off.

  2. Branding ENABLED but NO username configured
     -------------------------------------------
     Branding/branding.json is optional — a channel that has never
     saved its branding in the site Settings panel has no username on
     disk. In that case an unconfigured channel ships UNBRANDED
     (identical to the pre-branding pipeline) rather than failing: the
     script prints a line, exits 0, and the scene files pass through
     unchanged. Stage B's status.json reports branding_applied=false
     for this path (it requires toggle on AND a configured username).

  3. Branding ENABLED + username configured
     --------------------------------------
     Full compositor run: each scene_*.mp4 is rendered into the 10:9
     branded canvas with the persistent channel identity (username /
     display name / profile picture) and the per-job title, then
     atomically swapped in place.

Why this is its OWN script (and its own workflow step):
-------------------------------------------------------
Mirrors the enhance_scenes.py separation of concerns:

  * brand_scene.py  — brands ONE scene (no pipeline knowledge).
  * brand_template.py — renders the chrome PNG (no ffmpeg knowledge).
  * brand_scenes.py — THIS file: pipeline glue. Resolves the persistent
    branding record from branding/branding.json (written by the site's
    Settings panel via the contents API, living OUTSIDE jobs/ so the
    12-hour cleanup never deletes it), loops the scene directory, and
    owns the toggle / no-username no-op contracts.

Usage:
    python brand_scenes.py <scenes_dir> [--enabled | --no-enabled] \\
        [--title "Job title"] [--username HANDLE] \\
        [--display-name NAME] [--profile-picture PATH] \\
        [--badge COMMENTARY]

When the --username / --display-name / --profile-picture flags are
omitted they are resolved from branding/branding.json in the repo root
(the default branch checkout the workflow runs in). Explicit flags
override the file — useful for local testing.

Exit codes:
    0  success (including both no-op paths)
    2  bad inputs (scenes dir missing/empty, branding.json unreadable
       when it exists, explicit profile picture path missing)
    3  a per-scene brand/validate step failed, or leftover temp files
       were found after the swap loop
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Local — brand_scene / brand_template are sibling modules in scripts/.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from brand_scene import brand_scene  # noqa: E402
from brand_template import Branding  # noqa: E402

# branding/branding.json lives at the repo root (the workflow runs from
# the checkout root, and this script's parent dir is scripts/).
REPO_ROOT = Path(__file__).resolve().parent.parent
BRANDING_JSON = REPO_ROOT / "branding" / "branding.json"


def load_branding_file() -> dict:
    """
    Read branding/branding.json. Missing file -> {} (the no-username
    no-op path). A file that EXISTS but doesn't parse fails hard (exit
    2): silently shipping unbranded when the user explicitly saved
    branding would be the wrong failure mode — that's a broken config
    the user should hear about, unlike the never-configured case.
    """
    try:
        with open(BRANDING_JSON, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        print(f"{BRANDING_JSON} not found — no branding saved yet.")
        return {}
    except Exception as e:
        print(
            f"ERROR: {BRANDING_JSON} exists but is unreadable ({e}).\n"
            f"Fix or delete the file, or run with --no-enabled.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not isinstance(doc, dict):
        print(
            f"ERROR: {BRANDING_JSON} must contain a JSON object.",
            file=sys.stderr,
        )
        sys.exit(2)
    return doc


def resolve_branding(args) -> Branding | None:
    """
    Merge explicit CLI flags over branding/branding.json and return the
    effective Branding, or None when no username is configured (the
    ship-unbranded no-op path).

    The profile picture is dropped (with a note) when the configured
    path doesn't exist on disk — the renderer draws a graceful
    placeholder disc in that case, so a missing avatar must not fail
    the run. An EXPLICITLY passed --profile-picture that is missing
    fails hard instead (exit 2): an explicit path that doesn't exist
    is a caller bug, not a config gap.
    """
    doc = load_branding_file()

    username = (args.username or "").strip() or (doc.get("username") or "").strip()
    display_name = (
        (args.display_name or "").strip()
        or (doc.get("display_name") or "").strip()
        or username
    )
    picture = (
        (args.profile_picture or "").strip()
        or (doc.get("profile_picture") or "").strip()
    )

    if not username:
        print(
            "Branding ENABLED but no username is configured "
            "(branding/branding.json missing or has an empty username) —\n"
            "scenes ship UNBRANDED, unchanged from the pre-branding "
            "pipeline. Save channel branding in the site Settings panel "
            "to enable it.",
            flush=True,
        )
        return None

    if picture:
        pic_path = Path(picture)
        if not pic_path.is_absolute():
            pic_path = REPO_ROOT / pic_path
        if pic_path.is_file():
            picture = str(pic_path)
        elif (args.profile_picture or "").strip():
            print(
                f"ERROR: --profile-picture path does not exist: {picture}",
                file=sys.stderr,
            )
            sys.exit(2)
        else:
            print(
                f"Branding picture listed in branding.json but missing on "
                f"disk: {picture} — rendering with the placeholder avatar."
            )
            picture = ""

    return Branding(
        username=username,
        display_name=display_name,
        profile_picture=picture,
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Composite each cut scene MP4 into the branded 10:9 template "
            "in place (atomic .branded.mp4 swap per scene), using the "
            "persistent channel branding from branding/branding.json."
        )
    )
    ap.add_argument(
        "scenes_dir",
        help="Directory of scene_XX.mp4 files (or the merged final.mp4)",
    )
    ap.add_argument(
        "--enabled", dest="enabled", action="store_true", default=True,
        help="Apply the branding template (default).",
    )
    ap.add_argument(
        "--no-enabled", dest="enabled", action="store_false",
        help=(
            "Skip branding entirely — no scenes are read, no scenes are "
            "rewritten, exits 0. Wire this to the site's on/off toggle."
        ),
    )
    ap.add_argument(
        "--title", default=os.environ.get("JOB_TITLE", ""),
        help=(
            "Per-job title rendered into the template's title block "
            "(defaults to the JOB_TITLE env var, which stage-b.yml "
            "exports from production.json['title'])."
        ),
    )
    ap.add_argument("--username", default="",
                    help="Override branding.json username.")
    ap.add_argument("--display-name", default="",
                    help="Override branding.json display name.")
    ap.add_argument("--profile-picture", default="",
                    help="Override branding.json profile picture path.")
    ap.add_argument("--badge", default="COMMENTARY",
                    help="Category badge text shown at top-right.")
    args = ap.parse_args()

    # ---- Path 1: toggle off -------------------------------------------------
    if not args.enabled:
        print(
            "Branding DISABLED via --no-enabled — scene files left untouched.",
            flush=True,
        )
        return

    scenes_dir = Path(args.scenes_dir)
    if not scenes_dir.is_dir():
        print(f"ERROR: scenes dir does not exist: {scenes_dir}",
              file=sys.stderr)
        sys.exit(2)

    # Accept both the legacy per-scene layout (scene_*.mp4) and the new
    # single merged file (final.mp4) produced by cut_and_produce.py.
    scenes = sorted(scenes_dir.glob("scene_*.mp4"))
    merged = scenes_dir / "final.mp4"
    if merged.is_file():
        scenes.append(merged)
    if not scenes:
        print(f"ERROR: no scene_*.mp4 / final.mp4 files found in {scenes_dir}",
              file=sys.stderr)
        sys.exit(2)

    # ---- Path 2: toggle on, no username configured --------------------------
    branding = resolve_branding(args)
    if branding is None:
        return

    # ---- Path 3: full compositor run ----------------------------------------
    print(
        f"Branding {len(scenes)} scene(s) into the 1080x1200 (10:9) template:\n"
        f"  channel  = {branding.display_name!r} (@{branding.username})\n"
        f"  avatar   = {branding.profile_picture or 'placeholder disc'}\n"
        f"  title    = {args.title!r}\n"
        f"  badge    = {args.badge!r}\n"
        f"  swap     = <name>.branded.mp4 -> os.replace -> <name>.mp4 "
        f"(only after mobile-safe validation passes).",
        flush=True,
    )

    total_before = 0
    total_after = 0
    failed = []
    for src in scenes:
        tmp = src.with_suffix(".branded.mp4")
        before = src.stat().st_size
        total_before += before
        print(f"\n--- Branding {src.name} ({before/1024/1024:.2f} MB)",
              flush=True)
        try:
            # brand_scene() renders the chrome, runs the ffmpeg overlay
            # pass, and validates the output (exit 3 inside a subprocess-
            # free call — it sys.exits on failure, so catch SystemExit
            # to keep the loop's bookkeeping intact and report ALL
            # failures, not just the first).
            brand_scene(
                scene_path=str(src),
                out_path=str(tmp),
                branding=branding,
                title=args.title,
                badge=args.badge,
            )
        except SystemExit as e:
            failed.append((src.name, e.code))
            # Never leave a half-written temp behind on the failure path.
            if tmp.exists():
                tmp.unlink()
            continue
        after = tmp.stat().st_size
        total_after += after
        pct = (after - before) / before * 100.0 if before else 0.0
        print(
            f"  size: {before/1024/1024:.2f} MB -> {after/1024/1024:.2f} MB "
            f"({pct:+.1f}%)",
            flush=True,
        )
        # Atomic swap: the original is only replaced once the branded
        # file exists AND has passed validation inside brand_scene().
        os.replace(tmp, src)

    if failed:
        names = ", ".join(n for n, _ in failed)
        print(
            f"\nERROR: {len(failed)}/{len(scenes)} scene(s) failed to "
            f"brand: {names}\nOriginals for those scenes were left "
            f"untouched; branded scenes already swapped stay swapped. "
            f"Re-run after fixing the underlying ffmpeg/validation "
            f"failure.",
            file=sys.stderr,
        )
        sys.exit(3)

    # Final sanity check: the swap loop must leave NO temp files behind.
    # os.replace() consumes each .branded.mp4 by construction; this
    # assert turns a bookkeeping bug into a loud failure instead of
    # stray files silently leaking into the release zip step.
    leftovers = sorted(scenes_dir.glob("*.branded.mp4"))
    if leftovers:
        names = ", ".join(p.name for p in leftovers)
        print(
            f"ERROR: leftover temp file(s) after the atomic swap: {names}",
            file=sys.stderr,
        )
        sys.exit(3)
    print("\nNo leftover .branded.mp4 temp files — atomic swap clean.",
          flush=True)

    if total_before:
        overall = (total_after - total_before) / total_before * 100.0
        print(
            f"Done. Aggregate size delta across {len(scenes)} scene(s): "
            f"{total_before/1024/1024:.2f} MB -> "
            f"{total_after/1024/1024:.2f} MB ({overall:+.1f}%).",
            flush=True,
        )


if __name__ == "__main__":
    main()
