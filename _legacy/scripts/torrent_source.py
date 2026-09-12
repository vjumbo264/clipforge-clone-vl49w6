#!/usr/bin/env python3
"""Safely inspect a .torrent manifest and choose its largest video payload.

This utility never contacts trackers, peers, or DHT. It only parses bencoded
manifest metadata already supplied by the user, and later selects a downloaded
video file from a local directory for Stage A's normal ingest path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


MAX_TORRENT_BYTES = 1 * 1024 * 1024
MAX_VIDEO_BYTES = 12 * 1024 * 1024 * 1024
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".mov", ".webm", ".avi", ".ts", ".m2ts",
}


class BencodeError(ValueError):
    """Raised when a torrent manifest is malformed or exceeds safe limits."""


def _decode_bencode(data: bytes) -> Any:
    """Decode the small bencoded metadata grammar used by torrent manifests."""
    index = 0

    def parse() -> Any:
        nonlocal index
        if index >= len(data):
            raise BencodeError("unexpected end of bencoded data")
        token = data[index:index + 1]
        if token == b"i":
            index += 1
            end = data.find(b"e", index)
            if end < 0:
                raise BencodeError("unterminated integer")
            raw = data[index:end]
            if not raw or raw in (b"-0",) or (raw.startswith(b"0") and len(raw) > 1):
                raise BencodeError("invalid integer")
            try:
                value = int(raw)
            except ValueError as exc:
                raise BencodeError("invalid integer") from exc
            index = end + 1
            return value
        if token == b"l":
            index += 1
            values = []
            while index < len(data) and data[index:index + 1] != b"e":
                values.append(parse())
            if index >= len(data):
                raise BencodeError("unterminated list")
            index += 1
            return values
        if token == b"d":
            index += 1
            values: dict[bytes, Any] = {}
            while index < len(data) and data[index:index + 1] != b"e":
                key = parse()
                if not isinstance(key, bytes):
                    raise BencodeError("dictionary key is not a byte string")
                values[key] = parse()
            if index >= len(data):
                raise BencodeError("unterminated dictionary")
            index += 1
            return values
        if token.isdigit():
            colon = data.find(b":", index)
            if colon < 0:
                raise BencodeError("missing byte-string separator")
            try:
                length = int(data[index:colon])
            except ValueError as exc:
                raise BencodeError("invalid byte-string length") from exc
            if length < 0:
                raise BencodeError("negative byte-string length")
            index = colon + 1
            end = index + length
            if end > len(data):
                raise BencodeError("truncated byte string")
            value = data[index:end]
            index = end
            return value
        raise BencodeError("unknown bencode token")

    value = parse()
    if index != len(data):
        raise BencodeError("trailing bencoded data")
    return value


def _encode_bencode(value: Any) -> bytes:
    """Encode parsed torrent metadata canonically for BEP-3 v1 infohashing."""
    if isinstance(value, int):
        return f"i{value}e".encode("ascii")
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(_encode_bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        if not all(isinstance(key, bytes) for key in value):
            raise BencodeError("torrent dictionary has a non-byte key")
        payload = []
        for key in sorted(value):
            payload.append(_encode_bencode(key))
            payload.append(_encode_bencode(value[key]))
        return b"d" + b"".join(payload) + b"e"
    raise BencodeError("torrent contains an unsupported bencode value")


def torrent_infohash_v1(torrent_path: Path) -> str:
    """Return the uppercase SHA-1 of a valid torrent's canonical info dictionary."""
    raw = torrent_path.read_bytes()
    if not raw or len(raw) > MAX_TORRENT_BYTES:
        raise BencodeError("torrent file is empty or exceeds the allowed size")
    root = _decode_bencode(raw)
    if not isinstance(root, dict) or not isinstance(root.get(b"info"), dict):
        raise BencodeError("torrent has no info dictionary")
    return hashlib.sha1(_encode_bencode(root[b"info"])).hexdigest().upper()


def _text(value: Any, field: str) -> str:
    if not isinstance(value, bytes):
        raise BencodeError(f"{field} is missing or invalid")
    return value.decode("utf-8", errors="replace")


def _safe_path_part(value: str, field: str) -> str:
    if (not value or value in {".", ".."} or "\x00" in value or
            "/" in value or "\\" in value):
        raise BencodeError(f"{field} contains an unsafe path component")
    return value


def inspect_torrent(torrent_path: Path) -> dict[str, Any]:
    """Return safe display metadata for a single- or multi-file torrent."""
    raw = torrent_path.read_bytes()
    if not raw:
        raise BencodeError("torrent file is empty")
    if len(raw) > MAX_TORRENT_BYTES:
        raise BencodeError(f"torrent file exceeds {MAX_TORRENT_BYTES} byte limit")
    root = _decode_bencode(raw)
    if not isinstance(root, dict):
        raise BencodeError("torrent root must be a dictionary")
    info = root.get(b"info")
    if not isinstance(info, dict):
        raise BencodeError("torrent has no info dictionary")

    name = _safe_path_part(_text(info.get(b"name"), "info.name"), "info.name")
    files: list[dict[str, Any]] = []
    if b"files" in info:
        raw_files = info[b"files"]
        if not isinstance(raw_files, list) or not raw_files:
            raise BencodeError("info.files is invalid")
        for entry in raw_files:
            if not isinstance(entry, dict) or not isinstance(entry.get(b"length"), int):
                raise BencodeError("torrent contains an invalid file entry")
            parts = entry.get(b"path")
            if not isinstance(parts, list) or not parts:
                raise BencodeError("torrent file entry has no path")
            clean_parts = [
                _safe_path_part(_text(part, "file path"), "file path")
                for part in parts
            ]
            files.append({
                "index": len(files) + 1,
                "path": "/".join(clean_parts),
                "length": entry[b"length"],
            })
    else:
        length = info.get(b"length")
        if not isinstance(length, int):
            raise BencodeError("single-file torrent has no valid length")
        files.append({"index": 1, "path": name, "length": length})

    metadata = {
        "name": name,
        "file_count": len(files),
        "total_bytes": sum(max(0, item["length"]) for item in files),
        "files": files,
    }
    metadata["video_candidates"] = torrent_video_candidates(metadata)
    return metadata


def torrent_video_candidates(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all supported manifest video entries in original torrent order."""
    candidates = [
        item for item in metadata["files"]
        if Path(item["path"]).suffix.lower() in VIDEO_EXTENSIONS and item["length"] > 0
    ]
    for item in candidates:
        if item["length"] > MAX_VIDEO_BYTES:
            raise BencodeError(
                f"video candidate {item['index']} exceeds {MAX_VIDEO_BYTES} byte Stage A safety limit"
            )
    if not candidates:
        wanted = ", ".join(sorted(VIDEO_EXTENSIONS))
        raise BencodeError(f"torrent has no supported video file ({wanted})")
    return candidates


def select_torrent_video(metadata: dict[str, Any], selected_index: int) -> dict[str, Any]:
    """Validate and return the exact user-selected manifest video entry."""
    for item in torrent_video_candidates(metadata):
        if item["index"] == selected_index:
            return item
    raise BencodeError("selected torrent file is not an eligible video candidate")


def select_video(root: Path, expected_relative_path: str) -> Path:
    """Return only the downloaded file matching the user-selected manifest path."""
    expected = expected_relative_path.replace("\\", "/").lstrip("/")
    matches = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        relative = path.relative_to(root).as_posix()
        if relative == expected or relative.endswith("/" + expected):
            matches.append(path)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError("torrent completed without the selected video payload")
    raise FileNotFoundError("torrent produced more than one path matching the selected video payload")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    inspect_parser = sub.add_parser("inspect", help="print safe torrent metadata as JSON")
    inspect_parser.add_argument("torrent", type=Path)
    select_parser = sub.add_parser("select-video", help="print the user-selected downloaded video path")
    select_parser.add_argument("directory", type=Path)
    select_parser.add_argument("selected_relative_path")
    index_parser = sub.add_parser("select-index", help="validate and print a selected manifest file index")
    index_parser.add_argument("torrent", type=Path)
    index_parser.add_argument("selected_index", type=int)
    path_parser = sub.add_parser("select-path", help="validate and print a selected manifest video path")
    path_parser.add_argument("torrent", type=Path)
    path_parser.add_argument("selected_index", type=int)
    infohash_parser = sub.add_parser("infohash", help="print the manifest's normalized v1 infohash")
    infohash_parser.add_argument("torrent", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "inspect":
            print(json.dumps(inspect_torrent(args.torrent), ensure_ascii=False, indent=2))
        elif args.command == "select-index":
            metadata = inspect_torrent(args.torrent)
            print(select_torrent_video(metadata, args.selected_index)["index"])
        elif args.command == "select-path":
            metadata = inspect_torrent(args.torrent)
            print(select_torrent_video(metadata, args.selected_index)["path"])
        elif args.command == "infohash":
            print(torrent_infohash_v1(args.torrent))
        else:
            print(select_video(args.directory, args.selected_relative_path))
    except (BencodeError, FileNotFoundError, OSError) as exc:
        print(f"torrent source error: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
