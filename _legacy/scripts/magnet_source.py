#!/usr/bin/env python3
"""Validate BitTorrent magnet URIs before Stage A contacts the swarm.

The validator is deliberately offline: it parses only the supplied URI and
never resolves DNS, connects to a tracker, opens DHT, or starts a download.
Stage A uses the normalized v1 infohash to locate the `.torrent` metadata that
aria2 saves after its bounded metadata-only retrieval step.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit


MAX_MAGNET_URI_CHARS = 8_192
MAX_TRACKERS = 30
MAX_TRACKER_URI_CHARS = 512
MAX_METADATA_SOURCES = 3
MAX_METADATA_SOURCE_URI_CHARS = 1_024
MAX_DISPLAY_NAME_CHARS = 255
_ALLOWED_TRACKER_SCHEMES = {"udp", "http", "https"}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_HEX_INFOHASH_RE = re.compile(r"[0-9a-fA-F]{40}\Z")
_BASE32_INFOHASH_RE = re.compile(r"[A-Z2-7a-z2-7]{32}\Z")


class MagnetError(ValueError):
    """Raised when a magnet URI is malformed or exceeds Stage A limits."""


def _reject_controls(value: str, field: str) -> str:
    if _CONTROL_RE.search(value):
        raise MagnetError(f"{field} contains a control character")
    return value


def _canonical_v1_infohash(value: str) -> str:
    """Convert a BEP-9 v1 BTIH token (40 hex or 32 base32) to uppercase hex."""
    if _HEX_INFOHASH_RE.fullmatch(value):
        return value.upper()
    if _BASE32_INFOHASH_RE.fullmatch(value):
        try:
            decoded = base64.b32decode(value.upper())
        except (ValueError, base64.binascii.Error) as exc:
            raise MagnetError("xt contains invalid base32 infohash data") from exc
        if len(decoded) != 20:
            raise MagnetError("xt does not contain a v1 20-byte infohash")
        return decoded.hex().upper()
    raise MagnetError("xt must contain one v1 urn:btih 40-hex or 32-base32 infohash")


def _validate_http_source(value: str, field: str) -> str:
    """Accept an anonymous HTTP(S) metadata source, never a local path."""
    if len(value) > MAX_METADATA_SOURCE_URI_CHARS:
        raise MagnetError(f"{field} URI exceeds length limit")
    _reject_controls(value, f"{field} URI")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise MagnetError(f"{field} URI must use http or https with a host")
    if parsed.username is not None or parsed.password is not None:
        raise MagnetError(f"{field} URI must not embed credentials")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise MagnetError(f"{field} URI has an invalid port") from exc
    return value


def _validate_tracker(value: str) -> str:
    """Accept only ordinary UDP/HTTP(S) tracker announce endpoints."""
    if len(value) > MAX_TRACKER_URI_CHARS:
        raise MagnetError("tracker URI exceeds length limit")
    _reject_controls(value, "tracker URI")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in _ALLOWED_TRACKER_SCHEMES or not parsed.netloc:
        raise MagnetError("tracker URI must use udp, http, or https with a host")
    if parsed.username is not None or parsed.password is not None:
        raise MagnetError("tracker URI must not embed credentials")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise MagnetError("tracker URI has an invalid port") from exc
    return value


def inspect_magnet(magnet_uri: str) -> dict[str, Any]:
    """Return a safe, normalized summary of one v1 BitTorrent magnet URI."""
    if not isinstance(magnet_uri, str):
        raise MagnetError("magnet URI must be text")
    value = magnet_uri.strip()
    if not value:
        raise MagnetError("magnet URI is empty")
    if len(value) > MAX_MAGNET_URI_CHARS:
        raise MagnetError(f"magnet URI exceeds {MAX_MAGNET_URI_CHARS} character limit")
    _reject_controls(value, "magnet URI")
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "magnet" or parsed.netloc or parsed.path:
        raise MagnetError("source must be a magnet:? URI")
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise MagnetError("magnet URI has malformed query parameters") from exc

    xt_values: list[str] = []
    display_names: list[str] = []
    trackers: list[str] = []
    metadata_sources: list[str] = []
    for key, raw_value in pairs:
        _reject_controls(key, "magnet parameter name")
        _reject_controls(raw_value, "magnet parameter value")
        if key == "xt":
            if raw_value.lower().startswith("urn:btih:"):
                xt_values.append(raw_value[9:])
        elif key == "dn":
            if len(raw_value) > MAX_DISPLAY_NAME_CHARS:
                raise MagnetError("display name exceeds length limit")
            display_names.append(raw_value)
        elif key == "tr":
            trackers.append(_validate_tracker(raw_value))
        elif key == "xs":
            metadata_sources.append(_validate_http_source(raw_value, "exact source"))

    if len(xt_values) != 1:
        raise MagnetError("magnet URI must include exactly one v1 xt=urn:btih value")
    if len(display_names) > 1:
        raise MagnetError("magnet URI must not include multiple display names")
    if len(trackers) > MAX_TRACKERS:
        raise MagnetError(f"magnet URI has more than {MAX_TRACKERS} trackers")
    if len(metadata_sources) > MAX_METADATA_SOURCES:
        raise MagnetError(f"magnet URI has more than {MAX_METADATA_SOURCES} exact sources")

    return {
        "version": 1,
        "infohash_v1": _canonical_v1_infohash(xt_values[0]),
        "display_name": display_names[0] if display_names else "",
        "tracker_count": len(trackers),
        "trackers": trackers,
        "metadata_sources": metadata_sources,
    }


def find_saved_metadata(directory: Path, infohash_v1: str) -> Path:
    """Return aria2's single saved metadata file for the expected infohash."""
    expected = _canonical_v1_infohash(infohash_v1)
    if not directory.is_dir():
        raise FileNotFoundError("magnet metadata directory is missing")
    matches = [
        path for path in directory.iterdir()
        if path.is_file()
        and path.suffix.lower() == ".torrent"
        and path.stem.upper() == expected
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError("aria2 did not save metadata for the requested magnet infohash")
    raise FileNotFoundError("aria2 saved more than one matching magnet metadata file")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="print normalized safe magnet metadata as JSON")
    validate.add_argument("magnet_uri")
    saved = sub.add_parser("saved-metadata", help="print aria2's expected saved .torrent path")
    saved.add_argument("directory", type=Path)
    saved.add_argument("infohash_v1")
    infohash = sub.add_parser("infohash", help="print the normalized v1 infohash")
    infohash.add_argument("magnet_uri")
    metadata_source = sub.add_parser("metadata-source", help="print the first verified HTTP(S) exact source, if present")
    metadata_source.add_argument("magnet_uri")
    args = parser.parse_args()

    try:
        if args.command == "validate":
            print(json.dumps(inspect_magnet(args.magnet_uri), ensure_ascii=False, indent=2))
        elif args.command == "saved-metadata":
            print(find_saved_metadata(args.directory, args.infohash_v1))
        elif args.command == "infohash":
            print(inspect_magnet(args.magnet_uri)["infohash_v1"])
        else:
            sources = inspect_magnet(args.magnet_uri)["metadata_sources"]
            if sources:
                print(sources[0])
    except (MagnetError, FileNotFoundError, OSError) as exc:
        print(f"magnet source error: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
