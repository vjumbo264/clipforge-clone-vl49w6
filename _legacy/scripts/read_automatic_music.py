#!/usr/bin/env python3
"""Validate and expose a pre-dispatch Automatic Mode music choice.

The browser writes jobs/<job>/automatic_music.json before dispatching Stage A.
This script is deliberately narrow: it permits only an empty music reference,
a permanent audio-library path, or that same job's one-off music.mp3 path.
It prints GitHub Actions output only; it never logs file content or secrets.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


LIBRARY_REF_PREFIX = "path:audio-library/"
DEFAULT_MUSIC_PATH = Path("branding/music_default.json")


def is_safe_library_ref(value: str) -> bool:
    """Accept one ordinary repository basename, including Unicode names.

    The selection is later opened only from the checked-out repository. This
    validator still rejects empty names, traversal, nested paths, backslashes,
    and control characters before the workflow receives it.
    """
    if not value.startswith(LIBRARY_REF_PREFIX):
        return False
    basename = value[len(LIBRARY_REF_PREFIX):]
    return (
        bool(basename)
        and basename not in {".", ".."}
        and "/" not in basename
        and "\\" not in basename
        and not any(ord(char) < 32 or ord(char) == 127 for char in basename)
    )


def load_selection(path: Path) -> dict[str, object]:
    """Load the job selection and repair only the known legacy suffix.

    An early browser writer appended the two literal characters ``\\n`` after
    otherwise valid JSON. Accept that exact, terminal legacy form so a Stage A
    restart can recover; all other malformed documents still fail closed.
    """
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        if not raw.endswith("\\n"):
            raise
        data = json.loads(raw[:-2])
    if not isinstance(data, dict):
        raise ValueError("automatic music selection must be a JSON object")
    return data


def load_persistent_default_music(path: Path) -> str:
    """Return a safe library ref from the shared default, or an empty ref.

    A default is intentionally limited to a repository audio-library basename;
    job-local uploads are one-off and must never become a cross-job fallback.
    Invalid or unavailable settings fail closed to no music.
    """
    if not path.exists():
        return ""
    try:
        data = load_selection(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    if data.get("version") != 1:
        return ""
    raw_path = data.get("library_track_path")
    if not isinstance(raw_path, str):
        return ""
    music_ref = "path:" + raw_path
    return music_ref if is_safe_library_ref(music_ref) else ""


def valid_music_ref(value: object, job_id: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValueError("automatic music_ref must be a string")
    if is_safe_library_ref(value):
        return value
    if value == f"path:jobs/{job_id}/music.mp3":
        return value
    raise ValueError("automatic music_ref is not an allowed library or same-job one-off path")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("selection_path", type=Path)
    parser.add_argument("--default-music-path", type=Path,
                        default=DEFAULT_MUSIC_PATH)
    args = parser.parse_args()

    music_ref = ""
    source = "none"
    if args.selection_path.exists():
        # A present selection is deliberate, including an explicit empty choice.
        # Do not silently override it with the shared default.
        data = load_selection(args.selection_path)
        if data.get("version") != 1:
            raise ValueError("automatic music selection has an unsupported format")
        music_ref = valid_music_ref(data.get("music_ref"), args.job_id)
        raw_source = data.get("source")
        if isinstance(raw_source, str) and raw_source in {
            "explicit_library", "saved_library_default", "one_off_upload", "none"
        }:
            source = raw_source
    else:
        music_ref = load_persistent_default_music(args.default_music_path)
        if music_ref:
            source = "saved_library_default"

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("music_ref=" + music_ref + "\n")
            handle.write("music_source=" + source + "\n")
    else:
        print(music_ref)


if __name__ == "__main__":
    main()
