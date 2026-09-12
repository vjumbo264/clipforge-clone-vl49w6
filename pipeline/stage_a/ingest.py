"""ClipForge Stage A — source ingest (the four non-preserved source kinds).

Resolves a ``jobs/<job_id>/stage-a-request.json`` ``source`` object into a
single local original-quality video file plus a small ingest record. Ported
from the legacy scripts (``_legacy/scripts/download_drive.py``'s url/drive
parts, ``magnet_source.py``, ``torrent_source.py``) into the new
``pipeline/stage_a/`` layout per ARCHITECTURE.md §5 and §7.1.

Supported here (all clones):

  * ``url``          — a direct public video-file URL (http/https).
  * ``drive``        — a public Google Drive share link or file id
                       (confirm-token handling preserved).
  * ``magnet``       — a BitTorrent magnet URI. Validated offline, resolved
                       via aria2 metadata fetch, then either auto-selected
                       (single video) or parked for the user's pick
                       (``awaiting_torrent_selection``).
  * ``torrent_file`` — an uploaded ``.torrent`` manifest, same selection flow.

Both PRESERVED subsystems are now wired in:

  * ``telegram_channel`` (§9.1) — original-repo-only MTProto public channel
    download, delegated to ``pipeline/stage_a/telegram_channel.py`` (which
    enforces the original-repo gate + fail-closed MTProto secrets itself).
  * ``telegram_relay`` (§9.2) — Bot A → group → Bot B relay. The central
    relay workflow rewrites the request with real ``relay.release_tag`` /
    ``expected_size_bytes`` / ``sha256`` before dispatching Stage A; here we
    fetch the temporary prerelease asset and verify size + SHA-256.

``NotBuiltGate`` is retained for API compatibility but no source kind routes
to it anymore.

This module is import-safe with no heavy third-party deps: magnet/torrent
inspection is pure-python; url/drive download uses ``requests``; the actual
aria2/torrent swarm transfer is driven by the workflow shell, not here, so
this file never contacts trackers/peers itself.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from .. import status as job_status

# --------------------------------------------------------------------------- #
# Constants (preserved from the legacy sources)                                #
# --------------------------------------------------------------------------- #

CHUNK_SIZE = 1024 * 1024  # 1 MiB
REQUEST_TIMEOUT = (30, 60)  # (connect, read) seconds
MAX_TELEGRAM_MEDIA_BYTES = 5 * 1024 * 1024 * 1024

TELEGRAM_PUBLIC_HOSTS = ("t.me", "telegram.me")
DISABLED_SOCIAL_HOSTS = (
    "youtube-nocookie.com", "youtu.be", "youtube.com",
    "vm.tiktok.com", "vt.tiktok.com", "tiktok.com",
    "fb.watch", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "vimeo.com", "redd.it", "reddit.com",
)
PUBLIC_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{5,64}$")

# Torrent / magnet safety limits (preserved from the legacy sources).
MAX_TORRENT_BYTES = 1 * 1024 * 1024
MAX_VIDEO_BYTES = 12 * 1024 * 1024 * 1024
MAX_MAGNET_URI_CHARS = 8_192
MAX_TRACKERS = 30
MAX_TRACKER_URI_CHARS = 512
MAX_METADATA_SOURCES = 3
MAX_METADATA_SOURCE_URI_CHARS = 1_024
MAX_DISPLAY_NAME_CHARS = 255
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".mov", ".webm", ".avi", ".ts", ".m2ts",
}
_ALLOWED_TRACKER_SCHEMES = {"udp", "http", "https"}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_HEX_INFOHASH_RE = re.compile(r"[0-9a-fA-F]{40}\Z")
_BASE32_INFOHASH_RE = re.compile(r"[A-Z2-7a-z2-7]{32}\Z")

# Source kinds this module fully resolves itself.
_SELF_SERVE_KINDS = ("url", "drive", "magnet", "torrent_file")
# Preserved subsystems (§9.1 / §9.2), wired to their own download paths.
_PRESERVED_KINDS = ("telegram_channel", "telegram_relay")
ALL_KINDS = _SELF_SERVE_KINDS + _PRESERVED_KINDS

# §9.2 relay-asset contract (mirrors relay/telegram_relay.py write-back and
# the legacy stage-a.yml fetch checks).
RELAY_RELEASE_TAG_RE = re.compile(r"^clipforge-relay-input-[A-Za-z0-9._-]+$")
RELAY_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
MAX_RELAY_SOURCE_BYTES = 1800 * 1024 * 1024  # 1800 MiB (preserved cap)
GITHUB_API = "https://api.github.com"


class IngestError(RuntimeError):
    """A user-facing ingest failure (bad input, unavailable source, gate)."""


class MagnetError(ValueError):
    """A magnet URI is malformed or exceeds Stage A limits."""


class BencodeError(ValueError):
    """A torrent manifest is malformed or exceeds safe limits."""


class NotBuiltGate(IngestError):
    """The source kind is reserved for a preserved subsystem not yet built."""


# --------------------------------------------------------------------------- #
# Request loading / validation                                                 #
# --------------------------------------------------------------------------- #

def request_path(job_id: str, *, root: os.PathLike[str] | str = "jobs") -> Path:
    return Path(root) / job_id / "stage-a-request.json"


def load_request(job_id: str, *, root: os.PathLike[str] | str = "jobs") -> dict[str, Any]:
    """Load and minimally sanity-check ``jobs/<job_id>/stage-a-request.json``."""
    path = request_path(job_id, root=root)
    if not path.exists():
        raise IngestError(f"missing Stage A request: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IngestError(f"unreadable Stage A request {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise IngestError(f"Stage A request {path} is not a JSON object")
    source = data.get("source")
    if not isinstance(source, dict):
        raise IngestError(f"Stage A request {path} has no 'source' object")
    kind = source.get("kind")
    if kind not in ALL_KINDS:
        raise IngestError(
            f"unsupported source kind {kind!r}; expected one of {', '.join(ALL_KINDS)}"
        )
    if not isinstance(source.get("value"), str):
        raise IngestError(f"Stage A request {path} source.value must be a string")
    return data


# --------------------------------------------------------------------------- #
# Host classification (preserved semantics)                                    #
# --------------------------------------------------------------------------- #

def recognised_host(url: str, allowed_hosts: tuple[str, ...]) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    for allowed in allowed_hosts:
        if host == allowed or host.endswith(f".{allowed}"):
            return allowed
    return None


def disabled_social_host(url: str) -> str | None:
    return recognised_host(url, DISABLED_SOCIAL_HOSTS)


def telegram_public_post_url(url: str) -> str | None:
    """Canonical public Telegram channel post URL, or None (fail closed)."""
    if not recognised_host(url, TELEGRAM_PUBLIC_HOSTS):
        return None
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parts[:1] == ["s"]:
        parts = parts[1:]
    if len(parts) != 2:
        return None
    channel, message_id = parts
    if not PUBLIC_CHANNEL_RE.fullmatch(channel) or not message_id.isdecimal() or int(message_id) < 1:
        return None
    return f"https://t.me/{channel}/{int(message_id)}"


# --------------------------------------------------------------------------- #
# Google Drive (confirm-token download, preserved)                             #
# --------------------------------------------------------------------------- #

def extract_file_id(link: str) -> str:
    link = link.strip()
    if not link:
        raise IngestError("empty drive link")
    if "/" not in link and "?" not in link and " " not in link and len(link) >= 20:
        return link
    for pattern in (r"/file/d/([a-zA-Z0-9_-]+)", r"[?&]id=([a-zA-Z0-9_-]+)", r"/d/([a-zA-Z0-9_-]+)"):
        match = re.search(pattern, link)
        if match:
            return match.group(1)
    raise IngestError(f"could not extract a Google Drive file id from: {link}")


def _stream_to_file(resp: requests.Response, output_path: str, label: str) -> int:
    total = int(resp.headers.get("Content-Length", 0))
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    written = 0
    last_report = time.time()
    with open(output_path, "wb") as handle:
        for chunk in resp.iter_content(CHUNK_SIZE):
            if not chunk:
                continue
            handle.write(chunk)
            written += len(chunk)
            now = time.time()
            if now - last_report >= 5:
                if total:
                    print(f"  ...{written / 1e6:.1f} MB / {total / 1e6:.1f} MB ({written * 100.0 / total:.1f}%)", flush=True)
                else:
                    print(f"  ...{written / 1e6:.1f} MB", flush=True)
                last_report = now
    if written == 0:
        raise IngestError(f"downloaded 0 bytes from {label} — download failed")
    print(f"Done. Wrote {written} bytes to {output_path}", flush=True)
    return written


def download_drive(file_id: str, output_path: str) -> int:
    """Download a public Drive item with Google confirm-token handling."""
    base_url = "https://docs.google.com/uc?export=download"
    session = requests.Session()
    response = session.get(base_url, params={"id": file_id}, stream=True, allow_redirects=True, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    token = next((value for name, value in response.cookies.items() if name.startswith("download_warning")), None)
    if token is None and "text/html" in response.headers.get("Content-Type", ""):
        body = response.text
        match = re.search(r'name="confirm"\s+value="([^"]+)"', body) or re.search(r"confirm=([0-9A-Za-z_-]+)", body)
        if match:
            token = match.group(1)
    if token:
        response = session.get(base_url, params={"id": file_id, "confirm": token, "export": "download"}, stream=True, allow_redirects=True, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    if "text/html" in response.headers.get("Content-Type", ""):
        response = session.get("https://drive.usercontent.google.com/download", params={"id": file_id, "export": "download", "confirm": token or "t"}, stream=True, allow_redirects=True, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        if "text/html" in response.headers.get("Content-Type", ""):
            raise IngestError("Google Drive returned HTML instead of a file — the link may not be publicly shared, may require sign-in, or may be rate limited.")
    return _stream_to_file(response, output_path, f"Drive file id={file_id}")


def download_direct(url: str, output_path: str) -> int:
    """Download an ordinary direct public file URL."""
    response = requests.get(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; clipforge-downloader/1.0)",
        "Accept": "*/*",
    }, stream=True, allow_redirects=True, timeout=REQUEST_TIMEOUT)
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        code = error.response.status_code if error.response is not None else "error"
        raise IngestError(f"direct download failed: {url} returned HTTP {code} — check that the URL is correct and publicly accessible.") from error
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" in content_type or content_type.startswith("text/"):
        raise IngestError(f"the URL returned {content_type or 'a text response'} instead of a file — this does not look like a direct video download link.")
    return _stream_to_file(response, output_path, f"URL={url}")


# --------------------------------------------------------------------------- #
# Magnet validation (offline; preserved from magnet_source.py)                 #
# --------------------------------------------------------------------------- #

def _reject_controls(value: str, field: str) -> str:
    if _CONTROL_RE.search(value):
        raise MagnetError(f"{field} contains a control character")
    return value


def _canonical_v1_infohash(value: str) -> str:
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
    if len(value) > MAX_METADATA_SOURCE_URI_CHARS:
        raise MagnetError(f"{field} URI exceeds length limit")
    _reject_controls(value, f"{field} URI")
    parsed = urllib.parse.urlsplit(value)
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
    if len(value) > MAX_TRACKER_URI_CHARS:
        raise MagnetError("tracker URI exceeds length limit")
    _reject_controls(value, "tracker URI")
    parsed = urllib.parse.urlsplit(value)
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
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() != "magnet" or parsed.netloc or parsed.path:
        raise MagnetError("source must be a magnet:? URI")
    try:
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
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
    """Return aria2's single saved ``.torrent`` metadata file for the infohash."""
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


# --------------------------------------------------------------------------- #
# Torrent manifest inspection (preserved from torrent_source.py)               #
# --------------------------------------------------------------------------- #

def _decode_bencode(data: bytes) -> Any:
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
    for item in torrent_video_candidates(metadata):
        if item["index"] == selected_index:
            return item
    raise BencodeError("selected torrent file is not an eligible video candidate")


def select_video(root: Path, expected_relative_path: str) -> Path:
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


def write_torrent_selection(
    torrent_path: Path,
    job_id: str,
    request: dict[str, Any],
    output: Path,
    selected_index: int | None = None,
) -> dict[str, Any]:
    """Write ``jobs/<job_id>/torrent-selection.json`` (metadata-only)."""
    metadata = inspect_torrent(torrent_path)
    candidates = metadata["video_candidates"]
    if not candidates:
        raise IngestError("torrent contains no supported video candidates")

    chosen = None
    if selected_index is not None:
        chosen = select_torrent_video(metadata, int(selected_index))["index"]

    options = request.get("options") or {}
    payload = {
        "version": 1,
        "job_id": job_id,
        "torrent_name": metadata["name"],
        "video_candidates": candidates,
        "selected_index": chosen,
        "stage_a_inputs": {
            "whisper_model": options.get("whisper_model", "base"),
            # bug-65: default is auto-detect ("auto"), matching the new
            # pipeline default (translate_to_english + auto-detect). A forced
            # "en" here would mislabel auto-detected non-English sources.
            "language": options.get("language", "auto"),
            "target_duration_seconds": str(options.get("target_duration_seconds", 120)),
            "focus": options.get("focus", ""),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {output}: {len(candidates)} candidate(s), selected_index={chosen!r}")
    return payload


# --------------------------------------------------------------------------- #
# Preserved-subsystem gates (fail closed until their own phases land)          #
# --------------------------------------------------------------------------- #

ORIGINAL_CLIPFORGE_REPOSITORY = "motionssalt/clipforge"


def _download_telegram_channel(value: str, output_path: str) -> str:
    """§9.1 preserved subsystem: public Telegram channel-post download.

    Delegates to pipeline/stage_a/telegram_channel.py, which enforces both
    restriction layers itself (original-repo check + fail-closed MTProto
    secrets) and performs the download. Returns the canonical post URL.
    """
    from . import telegram_channel
    return telegram_channel.download_channel_post(value, output_path)


def _relay_asset_metadata(source: dict[str, Any]) -> tuple[str, int, str]:
    """Validate the §9.2 relay block and return (release_tag, size, sha256).

    Mirrors the legacy stage-a.yml checks exactly: tag shape, positive size
    bounded by the preserved 1800 MiB cap, and a 64-hex lowercase checksum.
    Fails closed with a user-facing error on any deviation.
    """
    relay = source.get("relay")
    if not isinstance(relay, dict):
        raise IngestError(
            "telegram_relay source is missing its relay metadata block — the central "
            "relay workflow must write source.relay.{release_tag,expected_size_bytes,sha256} "
            "before dispatching Stage A."
        )
    tag = str(relay.get("release_tag") or "")
    if not RELAY_RELEASE_TAG_RE.fullmatch(tag):
        raise IngestError("Invalid temporary relay release tag")
    try:
        expected_size = int(relay.get("expected_size_bytes"))
    except (TypeError, ValueError):
        raise IngestError("Invalid temporary relay source size")
    if expected_size < 1 or expected_size > MAX_RELAY_SOURCE_BYTES:
        raise IngestError("Invalid temporary relay source size")
    digest = str(relay.get("sha256") or "").strip().lower()
    if not RELAY_SHA256_RE.fullmatch(digest):
        raise IngestError("Invalid temporary relay source checksum")
    return tag, expected_size, digest


def _github_api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "clipforge-stage-a/1.0",
    }


def _download_relay_asset(job_id: str, source: dict[str, Any], output_path: str) -> None:
    """Fetch the temporary prerelease relay asset and verify its integrity.

    Ported from the legacy stage-a.yml ``telegram_bot_forward`` fetch block:
    validate the relay metadata, preflight disk space, stream the asset from
    the job's ``clipforge-relay-input-<job_id>`` release (private repos
    require the asset API endpoint with an octet-stream Accept), then verify
    the exact size and SHA-256. Deletes the partial file on any mismatch.
    """
    tag, expected_size, expected_sha = _relay_asset_metadata(source)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise IngestError(
            "telegram_relay source needs GH_TOKEN to fetch its temporary release asset."
        )
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        raise IngestError("telegram_relay source needs GITHUB_REPOSITORY to locate its release.")

    # Disk preflight (preserved: 2x expected + 1 GiB headroom).
    out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    free_bytes = shutil.disk_usage(out_dir).free
    required_bytes = expected_size * 2 + 1024 ** 3
    if free_bytes < required_bytes:
        raise IngestError("Insufficient runner disk space for the private relay source")

    headers = _github_api_headers(token)
    base = f"{GITHUB_API}/repos/{repo}"
    try:
        release = requests.get(f"{base}/releases/tags/{tag}", headers=headers, timeout=REQUEST_TIMEOUT)
        if release.status_code == 404:
            raise IngestError(
                f"relay release {tag} not found — the central relay workflow has not "
                "delivered the source asset (or already cleaned it up)."
            )
        release.raise_for_status()
        assets = release.json().get("assets") or []
        asset = next((a for a in assets if a.get("name") == "source_input.bin"), None)
        if asset is None:
            raise IngestError(f"relay release {tag} has no source_input.bin asset")
        asset_url = asset["url"]
        download = requests.get(
            asset_url,
            headers={**headers, "Accept": "application/octet-stream"},
            stream=True, timeout=REQUEST_TIMEOUT,
        )
        download.raise_for_status()
        written = 0
        digest = hashlib.sha256()
        with open(output_path, "wb") as handle:
            for chunk in download.iter_content(CHUNK_SIZE):
                if not chunk:
                    continue
                handle.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                if written > expected_size:
                    raise IngestError("relay source exceeded its declared size")
    except IngestError:
        if os.path.exists(output_path):
            os.unlink(output_path)
        raise
    except requests.RequestException as exc:
        if os.path.exists(output_path):
            os.unlink(output_path)
        raise IngestError(f"relay asset download failed: {exc}") from exc

    actual_size = os.path.getsize(output_path)
    actual_sha = digest.hexdigest()
    if actual_size != expected_size or actual_sha != expected_sha:
        os.unlink(output_path)
        raise IngestError("Temporary relay source integrity validation failed")
    print(
        f"Relay asset verified: {tag} source_input.bin "
        f"({actual_size} bytes, sha256 {actual_sha[:12]}…)",
        flush=True,
    )


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #

def detect_container_ext(source_file: str) -> str:
    """Detect the real container from bytes; fall back to ffprobe → mkv."""
    import subprocess
    try:
        mime = subprocess.run(
            ["file", "-b", "--mime-type", source_file],
            capture_output=True, text=True,
        ).stdout.strip()
    except OSError:
        mime = ""
    if mime in {"video/x-matroska", "application/x-matroska"}:
        return "mkv"
    if mime in {"video/mp4", "application/mp4"}:
        return "mp4"
    try:
        probe = subprocess.run(["ffprobe", "-v", "error", source_file],
                               capture_output=True, text=True)
        if probe.returncode == 0:
            return "mkv"
    except OSError:
        pass
    raise IngestError("downloaded file is not a recognized video container")


def ingest(job_id: str, work_dir: str, *, root: os.PathLike[str] | str = "jobs") -> dict[str, Any]:
    """Resolve the job's source into ``<work_dir>/original.<ext>``.

    Returns an ingest record describing the produced artifact and the source
    kind. Writes job status around the risky download step. For multi-file
    torrent sources with no selection yet, writes ``torrent-selection.json``,
    sets state ``awaiting_torrent_selection``, and raises ``SystemExit(3)`` so
    the workflow can stop cleanly (the bot re-dispatches with a pick).
    """
    request = load_request(job_id, root=root)
    source = request["source"]
    kind = source["kind"]
    value = source["value"]
    mode = request.get("mode", "manual")

    Path(work_dir).mkdir(parents=True, exist_ok=True)

    job_status.write_status(
        job_id, state="stage_a_running",
        message=f"Downloading source video ({kind})",
        mode=mode, root=root,
    )

    tmp_source = os.path.join(work_dir, "source_input.bin")

    if kind == "url":
        raw = value
        if "%" in raw and "http" in raw:
            raw = urllib.parse.unquote(raw)
        # A Telegram public-post link handed in as a plain URL is still the
        # §9.1 preserved-subsystem path (legacy dispatch semantics preserved:
        # download_drive.py's main() routes any t.me post URL to the MTProto
        # path regardless of how the source was classified).
        if telegram_public_post_url(raw):
            canonical = _download_telegram_channel(raw, tmp_source)
            print(f"Telegram channel post ingested via §9.1 path: {canonical}")
            ext = detect_container_ext(tmp_source)
            original_path = os.path.join(work_dir, f"original.{ext}")
            shutil.copyfile(tmp_source, original_path)
            size_bytes = os.path.getsize(original_path)
            record = {
                "version": 1,
                "job_id": job_id,
                "source_kind": "telegram_channel",
                "original_path": original_path,
                "original_asset_name": f"original.{ext}",
                "size_bytes": size_bytes,
                "container": ext,
                "ingested_at_epoch": int(time.time()),
            }
            (Path(root) / job_id).mkdir(parents=True, exist_ok=True)
            (Path(root) / job_id / "ingest.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Ingested telegram_channel source -> {original_path} ({size_bytes} bytes)")
            return record
        disabled = disabled_social_host(raw)
        if disabled:
            raise IngestError(
                f"{disabled} social links are disabled. Forward or upload the video to a "
                "public Telegram channel, then use its public post link instead."
            )
        if recognised_host(raw, TELEGRAM_PUBLIC_HOSTS):
            raise IngestError(
                "Use a public Telegram channel post link in the form "
                "https://t.me/<channel>/<message_id>. Private and non-post Telegram "
                "links are not supported."
            )
        download_direct(raw, tmp_source)

    elif kind == "drive":
        file_id = extract_file_id(value)
        download_drive(file_id, tmp_source)

    elif kind == "telegram_channel":
        canonical = _download_telegram_channel(value, tmp_source)
        print(f"Telegram channel post ingested via §9.1 path: {canonical}")

    elif kind == "telegram_relay":
        _download_relay_asset(job_id, source, tmp_source)

    elif kind in ("magnet", "torrent_file"):
        # Resolve to a .torrent manifest, then decide single vs multi-file.
        if kind == "magnet":
            info = inspect_magnet(value)
            metadata_dir = Path(work_dir) / "magnet-metadata"
            metadata_dir.mkdir(parents=True, exist_ok=True)
            torrent_path = _resolve_magnet_metadata(value, info, metadata_dir, work_dir)
        else:
            # torrent_file: value is a job-local path the bot wrote.
            expected_path = str(Path(root) / job_id / "source.torrent")
            torrent_value = value
            if torrent_value.startswith("path:"):
                torrent_value = torrent_value[len("path:"):]
            if torrent_value != expected_path or not os.path.isfile(torrent_value):
                raise IngestError(
                    f"invalid or missing job-local torrent manifest (expected {expected_path})"
                )
            torrent_path = Path(torrent_value)
            inspect_torrent(torrent_path)  # validate before any swarm contact

        metadata = inspect_torrent(torrent_path)
        candidates = metadata["video_candidates"]
        selection_path = Path(root) / job_id / "torrent-selection.json"

        # Determine the user-selected index, if any.
        selected_index_raw = source.get("torrent_file_index", "")
        selected_index: int | None = None
        if selected_index_raw not in ("", None):
            try:
                selected_index = int(selected_index_raw)
            except (TypeError, ValueError):
                raise IngestError("a valid selected torrent video index is required")

        if selected_index is None:
            if len(candidates) == 1:
                selected_index = candidates[0]["index"]
            else:
                # Multi-file torrent with no pick yet: park for the user's choice.
                write_torrent_selection(torrent_path, job_id, request, selection_path)
                job_status.write_status(
                    job_id, state="awaiting_torrent_selection",
                    message=f"Source has {len(candidates)} video files — pick one to continue.",
                    mode=mode, root=root,
                )
                raise SystemExit(3)

        chosen = select_torrent_video(metadata, selected_index)
        torrent_dir = Path(work_dir) / "torrent"
        _download_torrent_payload(torrent_path, torrent_dir, chosen["index"])
        downloaded = select_video(torrent_dir, chosen["path"])
        shutil.copyfile(str(downloaded), tmp_source)
        print(f"Selected torrent video: {downloaded}")

    ext = detect_container_ext(tmp_source)
    original_path = os.path.join(work_dir, f"original.{ext}")
    shutil.copyfile(tmp_source, original_path)
    size_bytes = os.path.getsize(original_path)

    record = {
        "version": 1,
        "job_id": job_id,
        "source_kind": kind,
        "original_path": original_path,
        "original_asset_name": f"original.{ext}",
        "size_bytes": size_bytes,
        "container": ext,
        "ingested_at_epoch": int(time.time()),
    }
    (Path(root) / job_id).mkdir(parents=True, exist_ok=True)
    (Path(root) / job_id / "ingest.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Ingested {kind} source -> {original_path} ({size_bytes} bytes)")
    return record


def _resolve_magnet_metadata(magnet_uri: str, info: dict[str, Any], metadata_dir: Path, work_dir: str) -> Path:
    """Obtain the ``.torrent`` for a magnet — from an ``xs=`` source if present,
    else via aria2's bounded metadata-only fetch."""
    import subprocess
    for source in info.get("metadata_sources", []):
        try:
            candidate = metadata_dir / "exact-source.torrent"
            download_direct(source, str(candidate))
            resolved = torrent_infohash_v1(candidate)
            if resolved == info["infohash_v1"]:
                print("Magnet metadata retrieved from its exact-source URL.")
                return candidate
            candidate.unlink(missing_ok=True)
        except (IngestError, BencodeError, OSError) as exc:
            print(f"magnet exact-source failed ({exc}); falling back to aria2", flush=True)

    print("Fetching magnet metadata via aria2 (metadata-only, bounded).", flush=True)
    # Regression fix (bug-04): legacy used --bt-stop-timeout=120 --seed-ratio=0
    # --max-tries=1 and, crucially, did NOT require aria2 to exit 0 -- aria2's
    # metadata-only mode saves the .torrent manifest and then exits non-zero
    # while waiting to stop, so the real success signal is "was the metadata
    # file saved", not the exit code. The new port used check=True with
    # --max-tries=3 and no stop timeout, so every successful metadata fetch was
    # raised as IngestError and the torrent method always failed.
    # Peer-discovery fix (bug-64): this invocation previously enabled neither
    # DHT nor peer exchange, so the only route to peers was whatever trackers
    # happened to be embedded in the magnet URI itself. Magnets with few,
    # stale, or dead embedded trackers then produced a characteristic stall:
    # one tracker-sourced connection (CN:1), byte count frozen at a fixed
    # value, 0% for the entire run until --bt-stop-timeout fired. DHT/PEX give
    # aria2 tracker-independent peer discovery and metadata exchange; verified
    # live against a tracker-less well-seeded magnet (Sintel test torrent) in
    # a fresh environment: CN:0 -> CN:29 within ~15s and the .torrent saved in
    # ~23s, with no prior DHT routing table -- aria2's built-in bootstrap is
    # sufficient, so no --dht-file-path warm-up file is needed in CI.
    # --bt-tracker= is ADDITIVE in aria2 (it merges with, never replaces, the
    # magnet's embedded announce entries; confirmed empirically via debug log
    # showing both embedded and flag trackers active), so it only widens the
    # net. --bt-stop-timeout raised 120 -> 180: DHT discovery has a real
    # warm-up latency tracker-only fetching does not, and the stalled-single-
    # connection failure mode now has a genuine fallback path that needs that
    # extra time; the outer `timeout 600` / subprocess timeout=620 still bound
    # a truly dead swarm, so user-facing failure latency is unchanged in the
    # worst case. Success detection is unchanged: the saved-metadata-file
    # check below (bug-04) remains the only success signal.
    try:
        subprocess.run(
            ["timeout", "600", "aria2c", f"--dir={metadata_dir}",
             "--bt-metadata-only=true", "--bt-save-metadata=true",
             "--seed-time=0", "--seed-ratio=0", "--bt-stop-timeout=180",
             "--connect-timeout=60", "--timeout=60",
             "--enable-dht=true", "--enable-dht6=true",
             "--enable-peer-exchange=true",
             "--bt-tracker=udp://tracker.opentrackr.org:1337/announce,"
             "udp://open.stealth.si:80/announce,"
             "udp://exodus.desync.com:6969/announce,"
             "udp://tracker.torrent.eu.org:451/announce",
             "--max-tries=1", "--file-allocation=none", magnet_uri],
            check=False, timeout=620,
        )
    except subprocess.TimeoutExpired as exc:
        raise IngestError(f"aria2 could not retrieve the magnet metadata: {exc}") from exc
    except OSError as exc:
        raise IngestError(f"aria2 is unavailable for magnet metadata fetch: {exc}") from exc
    try:
        return find_saved_metadata(metadata_dir, info["infohash_v1"])
    except FileNotFoundError as exc:
        raise IngestError(f"aria2 could not retrieve the magnet metadata: {exc}") from exc


def _download_torrent_payload(torrent_path: Path, out_dir: Path, selected_index: int) -> None:
    """Retrieve only the selected media entry via aria2 (never seeds)."""
    import subprocess
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["aria2c", f"--dir={out_dir}", f"--select-file={selected_index}",
             "--file-allocation=none", "--continue=true", "--auto-file-renaming=false",
             "--seed-time=0", "--max-tries=10",
             "--enable-dht=true", "--enable-peer-exchange=true",
             "--bt-enable-lpd=true", "--bt-max-peers=100",
             "--bt-request-peer-speed-limit=50K",
             "--bt-tracker-connect-timeout=10", "--bt-tracker-timeout=10",
             "--bt-tracker-interval=30",
             "--bt-tracker=udp://tracker.opentrackr.org:1337/announce,"
             "udp://open.stealth.si:80/announce,"
             "udp://exodus.desync.com:6969/announce,"
             "udp://tracker.torrent.eu.org:451/announce",
             "--connect-timeout=30", "--timeout=30", str(torrent_path)],
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise IngestError(f"aria2 torrent download failed: {exc}") from exc


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(description="ClipForge Stage A ingest")
    ap.add_argument("job_id")
    ap.add_argument("work_dir")
    ap.add_argument("--jobs-root", default="jobs")
    args = ap.parse_args()
    try:
        record = ingest(args.job_id, args.work_dir, root=args.jobs_root)
    except NotBuiltGate as exc:
        print(f"ingest gate: {exc}", file=sys.stderr)
        raise SystemExit(4)
    except (IngestError, MagnetError, BencodeError, FileNotFoundError) as exc:
        print(f"ingest error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()


__all__ = [
    "IngestError", "MagnetError", "BencodeError", "NotBuiltGate",
    "ALL_KINDS", "load_request", "request_path",
    "recognised_host", "disabled_social_host", "telegram_public_post_url",
    "extract_file_id", "download_drive", "download_direct",
    "inspect_magnet", "find_saved_metadata",
    "inspect_torrent", "torrent_infohash_v1", "torrent_video_candidates",
    "select_torrent_video", "select_video", "write_torrent_selection",
    "detect_container_ext", "ingest",
]
