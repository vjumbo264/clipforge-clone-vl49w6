#!/usr/bin/env python3
"""Regression coverage for resumable Stage A torrent-video selection."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
writer = ROOT / "scripts" / "write_torrent_selection.py"
status_writer = ROOT / "scripts" / "write_status.py"


def bencode(value: Any) -> bytes:
    """Build a compact valid torrent fixture without a network dependency."""
    if isinstance(value, int):
        return f"i{value}e".encode("ascii")
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        chunks = []
        for key in sorted(value):
            chunks.append(bencode(key))
            chunks.append(bencode(value[key]))
        return b"d" + b"".join(chunks) + b"e"
    raise TypeError(f"unsupported fixture type: {type(value)!r}")


with tempfile.TemporaryDirectory(prefix="clipforge_torrent_resume_") as tmp:
    root = Path(tmp)
    torrent_path = root / "resume-fixture.torrent"
    torrent_path.write_bytes(bencode({
        b"announce": b"udp://tracker.example.invalid:6969/announce",
        b"info": {b"name": b"resume-check.mkv", b"length": 4096},
    }))
    record_path = root / "jobs" / "resume-check" / "torrent-selection.json"
    subprocess.run([
        "python3", str(writer), str(torrent_path), str(record_path),
        "--job-id", "resume-check", "--whisper-model", "base",
        "--language", "auto", "--target-duration-seconds", "120",
        "--focus", "the opening scene",
    ], check=True)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["job_id"] == "resume-check"
    assert record["selected_index"] is None
    assert len(record["video_candidates"]) == 1
    candidate = record["video_candidates"][0]
    assert candidate["index"] == 1
    assert candidate["path"] == "resume-check.mkv"
    assert candidate["length"] == 4096
    assert record["stage_a_inputs"]["focus"] == "the opening scene"

    subprocess.run([
        "python3", str(status_writer), "resume-check", "awaiting_torrent_selection",
        "--out-dir", str(root / "jobs"),
        "--message", "Choose a video from this torrent to begin Stage A.",
        "--release-tag", "clipforge-resume-check",
        "--extra", "torrent_selection_path=jobs/resume-check/torrent-selection.json",
    ], check=True)
    status = json.loads((root / "jobs" / "resume-check" / "status.json").read_text(encoding="utf-8"))
    assert status["stage"] == "awaiting_torrent_selection"
    assert status["extra"]["torrent_selection_path"].endswith("torrent-selection.json")
    assert status["expires_at_epoch"] > status["created_at_epoch"]

workflow = (ROOT / ".github" / "workflows" / "stage-a.yml").read_text(encoding="utf-8")
app = (ROOT / "app.js").read_text(encoding="utf-8")
html = (ROOT / "index.html").read_text(encoding="utf-8")

assert "torrent_selection:" in workflow
assert "stage_a_ready" in workflow
assert "awaiting_torrent_selection" in workflow
assert "write_torrent_selection.py" in workflow
assert "Re-attach dispatch branch for status persistence" in workflow
assert 'git checkout -B "$GITHUB_REF_NAME" "origin/$GITHUB_REF_NAME"' in workflow
assert "needs.torrent_selection.outputs.stage_a_ready == 'true'" in workflow
assert "selected_index=\"${{ github.event.inputs.torrent_file_index }}\"" in workflow
assert "createPendingTorrentSelection" in app
assert "dispatchPendingTorrentSelection" in app
assert "loadPendingTorrentSelection" in app
assert "awaiting_torrent_selection" in app
assert "torrent-selection.json" in app
assert "torrent-selection-block" in html
assert "start-torrent-stage-a" in html

print("PASS: Stage A torrent selection is persisted, reloadable, and blocks execution until one valid video is confirmed")
