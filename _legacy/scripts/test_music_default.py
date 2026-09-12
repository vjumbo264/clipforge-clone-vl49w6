#!/usr/bin/env python3
"""Regression coverage for ClipForge's persisted library-music default."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from read_automatic_music import (
    is_safe_library_ref,
    load_persistent_default_music,
    load_selection,
    valid_music_ref,
)


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.js").read_text(encoding="utf-8")
AUTO = (ROOT / "automatic.html").read_text(encoding="utf-8")
TASK = (ROOT / "task.html").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "stage-a.yml").read_text(encoding="utf-8")
default_raw = (ROOT / "branding" / "music_default.json").read_text(encoding="utf-8")
if default_raw.endswith("\\n"):
    default_raw = default_raw[:-2]
DEFAULT = json.loads(default_raw)


assert DEFAULT["version"] == 1
stored_default = DEFAULT["library_track_path"]
assert stored_default is None or is_safe_library_ref("path:" + stored_default)
assert "One-off job uploads" in DEFAULT["note"]

# The only persistent path allowed is an audio-library basename. Traversal,
# external refs, and a different job's music file all fail closed.
assert valid_music_ref("path:audio-library/steady-bed.mp3", "job-1") == "path:audio-library/steady-bed.mp3"
unicode_track = "path:audio-library/𝐒𝐀𝐃 𝐅𝐔𝐍𝐊 (𝐒𝐔𝐏𝐄𝐑 𝐒𝐋𝐎𝐖𝐄𝐃) 𝐗 𝐘𝐔𝐓𝐀 𝐎𝐊𝐊𝐎𝐓𝐒𝐔.m4a"
assert is_safe_library_ref(unicode_track)
assert valid_music_ref(unicode_track, "job-1") == unicode_track
assert valid_music_ref("path:jobs/job-1/music.mp3", "job-1") == "path:jobs/job-1/music.mp3"
for bad in (
    "path:audio-library/../secret.mp3",
    "path:audio-library/nested/track.mp3",
    "path:audio-library/..\\secret.mp3",
    "path:audio-library/track\x00.mp3",
    "https://example.invalid/track.mp3",
    "path:jobs/other-job/music.mp3",
):
    try:
        valid_music_ref(bad, "job-1")
        raise AssertionError("unsafe music ref accepted: " + bad)
    except ValueError:
        pass

for required in (
    "var MUSIC_DEFAULT_PATH = 'branding/music_default.json';",
    "async function loadMusicDefault()",
    "async function saveMusicDefault(trackPath)",
    "function resolvedLibraryMusicRef()",
    "async function prepareAutomaticMusicChoice(jobId)",
    "saved_library_default",
    "one_off_upload",
    "if (!state.musicDefaultLoaded) await loadMusicDefault();",
    "musicRef = resolvedLibraryMusicRef();",
    "basename.indexOf('/') === -1",
    "basename.indexOf('\\\\') === -1",
):
    assert required in APP, f"missing music-default controller behavior: {required}"

assert "[A-Za-z0-9._-]+" not in APP, "frontend library validation must not reject Unicode track filenames"
# The legacy browser writer appended a literal backslash-n after otherwise
# valid JSON. Recover only that exact suffix so old failed jobs can restart.
with tempfile.TemporaryDirectory() as temp_dir:
    path = Path(temp_dir) / "automatic_music.json"
    payload = {"version": 1, "music_ref": unicode_track, "source": "explicit_library"}
    valid = json.dumps(payload, ensure_ascii=False)
    path.write_text(valid + "\\n", encoding="utf-8")
    assert load_selection(path) == payload
    path.write_text(valid + " extra", encoding="utf-8")
    try:
        load_selection(path)
        raise AssertionError("arbitrary malformed automatic music JSON was accepted")
    except json.JSONDecodeError:
        pass
assert "JSON.stringify(selection, null, 2) + '\\n'" in APP
assert "JSON.stringify(selection, null, 2) + '\\\\n'" not in APP
assert "function parseJsonWithLegacyTrailingNewline(raw)" in APP

# A missing per-job Automatic Mode selection is the normal fallback path: it
# must resolve to the persisted library default, while an invalid shared file
# must still fail closed rather than accept a job-external path.
assert load_persistent_default_music(ROOT / "branding" / "music_default.json") == "path:" + stored_default
with tempfile.TemporaryDirectory() as temp_dir:
    root = Path(temp_dir)
    selection = root / "missing_automatic_music.json"
    default_path = root / "music_default.json"
    output_path = root / "github_output.txt"
    default_path.write_text(json.dumps({"version": 1, "library_track_path": unicode_track.removeprefix("path:")}), encoding="utf-8")
    env = os.environ.copy()
    env["GITHUB_OUTPUT"] = str(output_path)
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "read_automatic_music.py"), "job-without-selection", str(selection), "--default-music-path", str(default_path)],
        check=True,
        env=env,
    )
    resolved = output_path.read_text(encoding="utf-8")
    assert "music_ref=" + unicode_track in resolved
    assert "music_source=saved_library_default" in resolved
    default_path.write_text(json.dumps({"version": 1, "library_track_path": "audio-library/../unsafe.mp3"}), encoding="utf-8")
    assert load_persistent_default_music(default_path) == ""
# Manual and automatic UI each show the current default. Automatic Mode has
# one (and only one) forward picker in its launch form, not a later handoff.
assert AUTO.count('id="audio-library-list"') == 1
assert AUTO.count('id="music-file-input"') == 1
assert 'id="audio-library-default"' in AUTO
assert "shared last-selected default" in AUTO
assert 'id="audio-library-default"' in TASK
assert "saved library default is used" in TASK

for required in (
    "Resolve Automatic Mode music selection",
    "python scripts/read_automatic_music.py",
    '"jobs/${{ steps.jid.outputs.job_id }}/automatic_music.json"',
    '--extra "music_ref=${{ steps.automatic_music.outputs.music_ref }}"',
    '-f "music_ref=${{ steps.automatic_music.outputs.music_ref }}',
    '--extra "music_source=${{ steps.automatic_music.outputs.music_source }}"',
):
    assert required in WORKFLOW, f"Automatic Mode workflow does not forward music: {required}"

print("PASS: manual and Automatic Mode use visible persisted library defaults, including no-selection backend fallback, while one-off uploads remain job-scoped")
