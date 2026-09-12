#!/usr/bin/env python3
"""Resolve a job's background-music choice for Stage B dispatch.

Adapted port of the legacy music reader. The legacy tool
read a separate music-selection JSON written by the browser before Stage A
dispatch; in the new architecture the bot writes the music choice directly
into ``jobs/<job_id>/stage-a-request.json`` (``music.ref`` / ``music.source``,
ARCHITECTURE.md §7.1), so this module validates THAT record instead and falls
back to the shared saved default (``branding/music_default.json``) only when
the request's source is ``default``.

Deliberately narrow: it permits only an empty music reference, a permanent
audio-library path, or that same job's one-off ``music.mp3`` path. It prints
GitHub Actions output only; it never logs file content or secrets.

Usage (workflow dispatch)::

    python -m pipeline.plan.music <job_id> jobs/<job_id>/stage-a-request.json

Writes ``music_ref=`` and ``music_source=`` to ``$GITHUB_OUTPUT`` (or prints
the ref on stdout when no GITHUB_OUTPUT is set, for local checks).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


LIBRARY_REF_PREFIX = "path:audio-library/"
DEFAULT_MUSIC_PATH = Path("branding/music_default.json")

# stage-a-request music.source values (schemas/stage_a_request.schema.json)
# mapped onto the legacy source vocabulary Stage B already understands.
_REQUEST_SOURCE_TO_LEGACY = {
    "none": "none",
    "default": "saved_library_default",
    "explicit_library": "explicit_library",
    "job_upload": "one_off_upload",
}


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
    """Load a JSON selection document, repairing only the known legacy suffix.

    An early browser writer appended the two literal characters ``\\n`` after
    otherwise valid JSON. Accept that exact, terminal legacy form so a restart
    can recover; all other malformed documents still fail closed.
    """
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        if not raw.endswith("\\n"):
            raise
        data = json.loads(raw[:-2])
    if not isinstance(data, dict):
        raise ValueError("music selection must be a JSON object")
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
        raise ValueError("music_ref must be a string")
    if is_safe_library_ref(value):
        return value
    if value == f"path:jobs/{job_id}/music.mp3":
        return value
    raise ValueError("music_ref is not an allowed library or same-job one-off path")


def resolve_from_request(request: dict[str, object], job_id: str, *, default_music_path: Path = DEFAULT_MUSIC_PATH) -> tuple[str, str]:
    """Return ``(music_ref, music_source)`` for a job.

    ``request`` is the parsed stage-a-request.json. A present ``music`` block
    is deliberate, including an explicit empty choice — it is never silently
    overridden by the shared default. ``source: default`` resolves through the
    shared saved default; anything else validates its own ref.
    """
    music = request.get("music")
    if not isinstance(music, dict):
        return "", "none"
    raw_source = music.get("source")
    source = _REQUEST_SOURCE_TO_LEGACY.get(raw_source if isinstance(raw_source, str) else "", "none")
    if raw_source == "default":
        ref = load_persistent_default_music(default_music_path)
        return (ref, "saved_library_default") if ref else ("", "none")
    return valid_music_ref(music.get("ref"), job_id), source


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve a job's music choice for Stage B dispatch.")
    parser.add_argument("job_id")
    parser.add_argument("request_path", type=Path, help="jobs/<job_id>/stage-a-request.json")
    parser.add_argument("--default-music-path", type=Path, default=DEFAULT_MUSIC_PATH)
    args = parser.parse_args()

    if args.request_path.exists():
        music_ref, source = resolve_from_request(
            load_selection(args.request_path), args.job_id,
            default_music_path=args.default_music_path,
        )
    else:
        # No request record (defensive): fall back to the shared default only.
        music_ref = load_persistent_default_music(args.default_music_path)
        source = "saved_library_default" if music_ref else "none"

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("music_ref=" + music_ref + "\n")
            handle.write("music_source=" + source + "\n")
    else:
        print(music_ref)


if __name__ == "__main__":
    main()
