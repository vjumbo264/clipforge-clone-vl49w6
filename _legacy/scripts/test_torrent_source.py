#!/usr/bin/env python3
"""Offline regression coverage for Stage A uploaded-torrent ingestion."""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "scripts" / "torrent_source.py"
spec = importlib.util.spec_from_file_location("torrent_source", HELPER_PATH)
assert spec and spec.loader
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)


def bencode(value: Any) -> bytes:
    """Build small deterministic torrent fixtures without network files."""
    if isinstance(value, int):
        return f"i{value}e".encode("ascii")
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        items = []
        for key in sorted(value):
            items.append(bencode(key))
            items.append(bencode(value[key]))
        return b"d" + b"".join(items) + b"e"
    raise TypeError(f"unsupported bencode fixture type: {type(value)!r}")


multi_video_manifest = {
    b"announce": b"udp://tracker.example.invalid:6969/announce",
    b"info": {
        b"name": b"fixture-episodes",
        b"files": [
            {b"length": 2_000, b"path": [b"episodes", b"episode-01.mkv"]},
            {b"length": 1_900, b"path": [b"episodes", b"episode-02.mkv"]},
            {b"length": 100, b"path": [b"extras", b"trailer.mp4"]},
            {b"length": 10, b"path": [b"notes", b"readme.txt"]},
        ],
    },
}
single_video_manifest = {
    b"announce": b"udp://tracker.example.invalid:6969/announce",
    b"info": {b"name": b"single-video.webm", b"length": 3_000},
}

with tempfile.TemporaryDirectory(prefix="clipforge_torrent_fixture_") as temp_dir:
    fixture_dir = Path(temp_dir)
    multi_path = fixture_dir / "multi.torrent"
    single_path = fixture_dir / "single.torrent"
    multi_path.write_bytes(bencode(multi_video_manifest))
    single_path.write_bytes(bencode(single_video_manifest))

    multi_metadata = source.inspect_torrent(multi_path)
    assert multi_metadata["file_count"] == 4
    assert [item["index"] for item in multi_metadata["video_candidates"]] == [1, 2, 3]
    assert source.select_torrent_video(multi_metadata, 2)["path"] == "episodes/episode-02.mkv"
    assert source.select_torrent_video(multi_metadata, 1)["path"].endswith(".mkv")

    single_metadata = source.inspect_torrent(single_path)
    assert single_metadata["file_count"] == 1
    assert [item["index"] for item in single_metadata["video_candidates"]] == [1]
    assert source.select_torrent_video(single_metadata, 1)["path"] == "single-video.webm"

    try:
        source.select_torrent_video(multi_metadata, 4)
        raise AssertionError("non-video torrent selection was accepted")
    except source.BencodeError:
        pass

    root = fixture_dir / "download"
    (root / "readme.txt").parent.mkdir()
    (root / "readme.txt").write_text("not video", encoding="utf-8")
    selected = root / "episodes" / "episode-02.mkv"
    decoy = root / "episodes" / "episode-01.mkv"
    selected.parent.mkdir()
    decoy.write_bytes(b"0" * 32)
    selected.write_bytes(b"1" * 128)
    assert source.select_video(root, "episodes/episode-02.mkv") == selected
    try:
        source.select_video(root, "extras/trailer.mp4")
        raise AssertionError("missing selected payload was accepted")
    except FileNotFoundError:
        pass

workflow = (ROOT / ".github" / "workflows" / "stage-a.yml").read_text(encoding="utf-8")
app = (ROOT / "app.js").read_text(encoding="utf-8")
html = (ROOT / "index.html").read_text(encoding="utf-8")
assert "source.torrent" in workflow and "aria2c" in workflow
assert "--select-file=\"$selected_index\"" in workflow
assert "--seed-time=0" in workflow
assert "torrent_file_index" in workflow and "select-path" in workflow
assert "expected_path=\"jobs/${{ steps.jid.outputs.job_id }}/source.torrent\"" in workflow
assert "torrent-file-input" in app and "createPendingTorrentSelection" in app
assert "dispatchPendingTorrentSelection" in app and "torrent-selection.json" in app
assert "torrentVideoCandidates" in app and "awaiting_torrent_selection" in app
assert "torrent-video-select" in html and 'accept=".torrent,application/x-bittorrent"' in html

print("PASS: persisted multi-video torrent selection routes only the chosen safe payload through Stage A")
