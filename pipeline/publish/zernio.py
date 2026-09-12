#!/usr/bin/env python3
"""ClipForge Zernio publishing subsystem — port of legacy zernio_*.py.

This module collapses the previous six-file layout
(_legacy/scripts/zernio_publish.py + zernio_state.py + zernio_targets.py +
zernio_schedule.py + zernio_workflow_state.py + zernio_manual_resolve.py)
into a single package module under ``pipeline/publish/`` per ARCHITECTURE.md
§4 and §10. Behavior and error semantics are preserved verbatim except:

* imports are relative to this package;
* the CLI subcommand set is unified so
  ``.github/workflows/publish.yml`` can invoke every operation via
  ``python -m pipeline.publish.zernio <subcommand> ...`` (no callers need to
  reach across sibling modules like the legacy scripts did);
* ``publishing`` is metadata attached to ``jobs/<id>/status.json`` per
  ARCHITECTURE.md §6.1 ("Publishing is not a job state"); this module never
  moves a Stage B job out of ``complete``.

No function here accepts, logs, or persists an API key. The Zernio API key is
supplied to the CLI via ``ZERNIO_API_KEY`` (set by the GitHub Actions runner
from an Actions secret) and is only used in-process for outbound HTTP.
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import mimetypes
import os
import re
import sys
import time
import uuid
from datetime import UTC, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# --------------------------------------------------------------------------- #
# Shared constants                                                             #
# --------------------------------------------------------------------------- #

API_BASE = os.environ.get("ZERNIO_API_BASE", "https://zernio.com/api/v1").rstrip("/")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PLATFORMS: tuple[str, ...] = ("tiktok", "youtube", "instagram")
PLATFORM_SET = set(PLATFORMS)
# Instagram publishes BOTH a Reel and a Story for every publish (bug-52):
# per docs.zernio.com/platforms/instagram, omitting contentType publishes a
# single video as a Reel automatically; contentType="story" targets Stories.
INSTAGRAM_CONTENT_TYPES: tuple[str, ...] = ("reel", "story")
MODES: set[str] = {"publish_now", "manual_schedule", "smart_schedule"}
PUBLISHING_MODES = MODES  # legacy alias

TERMINAL_QUEUE_STATUSES: set[str] = {"published", "failed", "cancelled", "not_requested"}
ACTIVE_QUEUE_STATUSES: set[str] = {"requested", "queued", "scheduled", "publishing", "partial"}
TERMINAL_POST_STATUSES: set[str] = {"published", "failed", "cancelled"}

TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
TIMEZONE_RE = re.compile(r"^(?:UTC|[A-Za-z_]+(?:/[A-Za-z_+\-]+)+)$")
POST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}")
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{8,200}")


# --------------------------------------------------------------------------- #
# Exceptions                                                                   #
# --------------------------------------------------------------------------- #


class ZernioError(RuntimeError):
    """Any Zernio API / client-side failure. Never contains an API key."""

    def __init__(self, message: str, *, status: int | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.details = details or {}


class TargetValidationError(ValueError):
    """Raised by target-normalization helpers on bad input."""


# --------------------------------------------------------------------------- #
# JSON on-disk helpers                                                          #
# --------------------------------------------------------------------------- #


def read_json(path: str | Path, default: Any) -> Any:
    target = Path(path)
    if not target.exists():
        return copy.deepcopy(default)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {target}: {exc}") from exc
    return value


def write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json_object_safely(path: str | Path) -> dict[str, Any] | None:
    """Return a JSON object only when a workflow result file is usable.

    A publisher command can fail before writing stdout, leaving the redirected
    result file absent or empty. Treat every unreadable, blank, malformed, or
    non-object result as unavailable so the workflow can record the original
    publishing failure rather than raise a secondary JSONDecodeError.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


# --------------------------------------------------------------------------- #
# Time / timezone helpers                                                       #
# --------------------------------------------------------------------------- #


def normalise_platforms(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value:
        platform = str(raw or "").strip().lower()
        if platform in PLATFORM_SET and platform not in result:
            result.append(platform)
    return result


def validate_timezone(name: str) -> str:
    value = str(name or "").strip() or "UTC"
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {value}") from exc
    return value


def validate_hhmm(value: str) -> str:
    text = str(value or "").strip()
    if not TIME_RE.fullmatch(text):
        raise ValueError("Preferred posting time must use HH:MM (24-hour) format.")
    return text


def parse_local_datetime(value: str, timezone_name: str) -> datetime:
    """Interpret a user-supplied local ISO datetime in a specific IANA timezone.

    Users select a local wall-clock time.  The native Zernio API accepts that
    local timestamp plus its IANA timezone, so this intentionally does not do
    date arithmetic in UTC.
    """
    zone = ZoneInfo(validate_timezone(timezone_name))
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("A local scheduled datetime is required.")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Scheduled datetime must be ISO-8601.") from exc
    if parsed.tzinfo is not None:
        return parsed.astimezone(zone)
    return parsed.replace(tzinfo=zone)


def _wall_clock_for(day, preferred_time: str, zone: ZoneInfo) -> datetime:
    hour, minute = (int(part) for part in validate_hhmm(preferred_time).split(":"))
    # Construct in the chosen IANA zone first. This preserves the requested
    # local posting hour through DST transitions instead of adding UTC days.
    return datetime.combine(day, dtime(hour=hour, minute=minute), tzinfo=zone)


def smart_interval_hours(smart: dict[str, Any]) -> int:
    """Return a positive whole hourly cadence, migrating legacy day settings.

    New settings persist ``interval_hours``. Existing clones can retain their
    old ``interval_days`` document unchanged until their next settings save;
    converting it here prevents a legacy two-day schedule from unexpectedly
    becoming two hours.
    """
    raw = smart.get("interval_hours")
    if raw is None:
        try:
            raw = int(smart.get("interval_days", 1)) * 24
        except (TypeError, ValueError) as exc:
            raise ValueError("Posting interval must be a positive whole number of hours.") from exc
    try:
        hours = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Posting interval must be a positive whole number of hours.") from exc
    if hours < 1 or hours > 8760:
        raise ValueError("Posting interval must be between 1 and 8760 hours.")
    return hours


def _advance_slot(slot: datetime, interval_hours: int, preferred_time: str, zone: ZoneInfo) -> datetime:
    """Advance one cadence interval, retaining old wall-clock DST behavior.

    A 24-hour multiple represents an existing daily-style schedule, so it
    advances by local calendar dates at the preferred local time. Shorter and
    non-day-multiple intervals advance by exact elapsed hours, which permits
    one-hour cadence without date rounding.
    """
    if interval_hours % 24 == 0:
        return _wall_clock_for(slot.date() + timedelta(days=interval_hours // 24), preferred_time, zone)
    return (slot + timedelta(hours=interval_hours)).astimezone(zone)


def _format_local(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat(timespec="seconds")


def _slot_from_record(record: Any, default_timezone: str) -> datetime | None:
    if not isinstance(record, dict):
        return None
    raw = record.get("scheduled_for") or record.get("scheduledFor")
    if not raw:
        return None
    zone_name = str(record.get("timezone") or default_timezone)
    try:
        return parse_local_datetime(str(raw), zone_name)
    except ValueError:
        return None


def _active_slots(queue: dict[str, Any], external_posts: Iterable[Any], timezone_name: str) -> list[datetime]:
    slots: list[datetime] = []
    for item in queue.get("items", []) if isinstance(queue, dict) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").lower() not in ACTIVE_QUEUE_STATUSES:
            continue
        slot = _slot_from_record(item, timezone_name)
        if slot is not None:
            slots.append(slot)
    for post in external_posts:
        slot = _slot_from_record(post, timezone_name)
        if slot is not None:
            slots.append(slot)
    return slots


def plan_smart_schedule(
    settings: dict[str, Any],
    queue: dict[str, Any],
    *,
    external_posts: Iterable[Any] = (),
    now: datetime | None = None,
) -> dict[str, str]:
    """Calculate a collision-free native Zernio scheduledFor timestamp.

    Existing ClipForge queue entries reserve their configured whole-hour
    interval. Existing Zernio scheduled posts are treated as occupied times.
    The result is a local timestamp plus IANA timezone for Zernio's native
    scheduler.
    """
    smart = settings.get("smart_schedule") if isinstance(settings, dict) else {}
    if not isinstance(smart, dict):
        smart = {}
    timezone_name = validate_timezone(str(smart.get("timezone") or "UTC"))
    preferred_time = validate_hhmm(str(smart.get("preferred_time") or "19:30"))
    interval_hours = smart_interval_hours(smart)

    zone = ZoneInfo(timezone_name)
    current = (now or datetime.now(UTC)).astimezone(zone).replace(second=0, microsecond=0)
    start_mode = str(smart.get("start_mode") or "next_available")
    custom_start = str(smart.get("custom_start") or "").strip()
    if start_mode == "custom" and custom_start:
        candidate = parse_local_datetime(custom_start, timezone_name).replace(second=0, microsecond=0)
    else:
        candidate = _wall_clock_for(current.date(), preferred_time, zone)
        # Daily-style cadences preserve the previous preferred local wall-clock
        # behavior. Hourly and other non-day cadences use the same HH:MM value
        # as an anchor and advance by real elapsed hours until the next slot.
        if interval_hours % 24 == 0:
            if candidate <= current:
                # Keep the historical behavior for day-based schedules: the
                # first available preferred wall-clock slot is tomorrow, while
                # queued-slot spacing still uses the configured cadence.
                candidate = _wall_clock_for(current.date() + timedelta(days=1), preferred_time, zone)
        else:
            while candidate <= current:
                candidate = _advance_slot(candidate, interval_hours, preferred_time, zone)

    slots = _active_slots(queue, external_posts, timezone_name)
    own_slots = _active_slots(queue, (), timezone_name)
    # A later item already assigned by ClipForge defines the next interval
    # anchor. This prevents two Stage B completions from claiming the same
    # hourly cadence slot.
    if own_slots:
        latest = max(own_slots)
        after_latest = _advance_slot(latest, interval_hours, preferred_time, zone)
        if candidate < after_latest:
            candidate = after_latest

    # Exact timestamps are collision keys. Work entirely in the account's local
    # IANA timezone, then send that local timestamp and timezone to Zernio.
    occupied = {_format_local(slot.astimezone(zone)) for slot in slots}
    guard = 0
    while _format_local(candidate) in occupied or candidate <= current:
        candidate = _advance_slot(candidate, interval_hours, preferred_time, zone)
        guard += 1
        if guard > 10000:
            raise ValueError("Could not find an available smart-scheduling slot.")

    return {
        "scheduled_for": candidate.strftime("%Y-%m-%dT%H:%M:%S"),
        "timezone": timezone_name,
        "scheduled_at_utc": candidate.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


# --------------------------------------------------------------------------- #
# Queue helpers                                                                 #
# --------------------------------------------------------------------------- #


def default_queue() -> dict[str, Any]:
    return {"version": 1, "provider": "zernio", "items": []}


def queue_count(queue: dict[str, Any]) -> int:
    return sum(
        1 for item in queue.get("items", [])
        if isinstance(item, dict) and str(item.get("status") or "").lower() in ACTIVE_QUEUE_STATUSES
    )


def upsert_queue_item(queue: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(queue if isinstance(queue, dict) else default_queue())
    result.setdefault("version", 1)
    result.setdefault("provider", "zernio")
    items = result.setdefault("items", [])
    if not isinstance(items, list):
        items = result["items"] = []
    job_id = str(item.get("job_id") or "")
    replacement = copy.deepcopy(item)
    for index, existing in enumerate(items):
        if isinstance(existing, dict) and str(existing.get("job_id") or "") == job_id:
            items[index] = replacement
            break
    else:
        items.append(replacement)
    return result


def remove_queue_item(queue: dict[str, Any], job_id: str) -> dict[str, Any]:
    result = copy.deepcopy(queue if isinstance(queue, dict) else default_queue())
    result["items"] = [
        item for item in result.get("items", [])
        if not isinstance(item, dict) or str(item.get("job_id") or "") != str(job_id)
    ]
    return result


def active_accounts(accounts_doc: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {platform: [] for platform in PLATFORMS}
    for account in accounts_doc.get("accounts", []) if isinstance(accounts_doc, dict) else []:
        if not isinstance(account, dict):
            continue
        platform = str(account.get("platform") or "").lower()
        account_id = str(account.get("id") or account.get("_id") or "").strip()
        if platform not in result or not account_id:
            continue
        if account.get("isActive") is False or account.get("enabled") is False or account.get("needsReconnection") is True:
            continue
        result[platform].append({
            "id": account_id,
            "platform": platform,
            "username": str(account.get("username") or ""),
            "displayName": str(account.get("displayName") or ""),
            "profileId": str(account.get("profileId") or ""),
        })
    return result


def safe_fingerprint(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= 8:
        return "…"
    return text[:4] + "…" + text[-4:]


def now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


# --------------------------------------------------------------------------- #
# Target validation (settings.target_accounts -> transport JSON)                #
# --------------------------------------------------------------------------- #


def normalize_targets(value: Any, *, allow_empty: bool = False) -> list[dict[str, Any]]:
    """Accept only non-empty TikTok/YouTube/Instagram groups with string account IDs."""
    if not isinstance(value, list):
        raise TargetValidationError("targets must be a JSON array")
    out: list[dict[str, Any]] = []
    seen_platforms: set[str] = set()
    for target in value:
        if not isinstance(target, dict):
            raise TargetValidationError("each target must be a JSON object")
        platform = target.get("platform")
        ids = target.get("account_ids")
        if platform not in PLATFORM_SET:
            raise TargetValidationError("target platform must be tiktok, youtube, or instagram")
        if platform in seen_platforms:
            raise TargetValidationError("each platform may appear only once")
        if not isinstance(ids, list) or not ids:
            raise TargetValidationError("each target must include one or more account_ids")
        normalized_ids: list[str] = []
        seen_ids: set[str] = set()
        for account_id in ids:
            item = str(account_id).strip()
            if not item or item in seen_ids:
                continue
            seen_ids.add(item)
            normalized_ids.append(item)
        if not normalized_ids:
            raise TargetValidationError("each target must include non-empty account_ids")
        seen_platforms.add(platform)
        out.append({"platform": platform, "account_ids": normalized_ids})
    if not out and not allow_empty:
        raise TargetValidationError("targets must contain at least one account group")
    return out


def targets_from_settings(settings: dict[str, Any]) -> list[dict[str, Any]]:
    groups = settings.get("target_accounts") if isinstance(settings.get("target_accounts"), dict) else {}
    raw = [{"platform": platform, "account_ids": groups.get(platform, [])} for platform in PLATFORMS if groups.get(platform)]
    return normalize_targets(raw, allow_empty=True)


def serialize_targets(targets: Any) -> str:
    """Produce the exact compact JSON expected by workflow_dispatch inputs."""
    return json.dumps(normalize_targets(targets), separators=(",", ":"), ensure_ascii=False)


def encode_targets(targets: Any) -> str:
    return base64.b64encode(serialize_targets(targets).encode("utf-8")).decode("ascii")


def decode_targets(encoded: str) -> list[dict[str, Any]]:
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
        return normalize_targets(json.loads(raw))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, TargetValidationError) as exc:
        raise TargetValidationError("invalid base64 JSON target transport") from exc


AUTOMATIC_MODES = {"publish_now", "smart_schedule"}


def automatic_fields(settings: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return tab-safe fields for Stage B: skip, mode, IANA timezone, JSON b64."""
    enabled = settings.get("enabled") is True
    automatic = settings.get("auto_publish") is True
    mode = settings.get("automatic_mode")
    schedule = settings.get("smart_schedule") if isinstance(settings.get("smart_schedule"), dict) else {}
    timezone = str(schedule.get("timezone") or "UTC")
    targets = targets_from_settings(settings)
    if not (enabled and automatic and mode in AUTOMATIC_MODES and targets):
        return "true", "", "", ""
    if not TIMEZONE_RE.fullmatch(timezone):
        raise TargetValidationError("automatic publishing requires a safe IANA timezone")
    return "false", mode, timezone, encode_targets(targets)


# --------------------------------------------------------------------------- #
# HTTP transport                                                                #
# --------------------------------------------------------------------------- #


def _read_limited(stream) -> bytes:
    data = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ZernioError("Zernio response exceeded the safe response-size limit.")
    return data


def _json_bytes(data: bytes) -> Any:
    if not data:
        return {}
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _safe_error_message(payload: Any, status: int | None) -> str:
    if isinstance(payload, dict):
        message = payload.get("error") or payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:300]
    return f"Zernio request failed (HTTP {status or 'unknown'})."


def _sanitize_error(payload: Any) -> dict[str, Any]:
    """Keep durable diagnostic fields but never preserve provider raw payloads."""
    if not isinstance(payload, dict):
        return {}
    allowed = {"type", "code", "param", "platform"}
    out = {key: str(payload[key])[:300] for key in allowed if payload.get(key) is not None}
    details = payload.get("details")
    if isinstance(details, dict):
        safe_detail_keys = {
            "retryAfterSeconds", "currentCount", "limit", "reason", "field",
            "existingPostId", "accountId", "platform",
        }
        clean = {str(k): str(v)[:300] for k, v in details.items() if k in safe_detail_keys}
        if clean:
            out["details"] = clean
    return out


def request_json(
    method: str,
    path: str,
    api_key: str,
    *,
    body: dict[str, Any] | None = None,
    request_id: str | None = None,
    opener: Callable[..., Any] = urlopen,
) -> Any:
    if not api_key or not api_key.strip():
        raise ZernioError("Zernio API key is not configured.")
    url = path if path.startswith(("http://", "https://")) else API_BASE + "/" + path.lstrip("/")
    headers = {
        "Authorization": "Bearer " + api_key.strip(),
        "Accept": "application/json",
        "User-Agent": "ClipForge-Zernio/1.1",
    }
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if request_id:
        headers["x-request-id"] = request_id
    req = Request(url, data=data, headers=headers, method=method.upper())
    try:
        with opener(req, timeout=90) as response:
            payload = _json_bytes(_read_limited(response))
            if 200 <= response.status < 300:
                return payload
            raise ZernioError(_safe_error_message(payload, response.status), status=response.status, details=_sanitize_error(payload))
    except HTTPError as err:
        payload = _json_bytes(_read_limited(err))
        raise ZernioError(_safe_error_message(payload, err.code), status=err.code, details=_sanitize_error(payload)) from err
    except (URLError, TimeoutError) as err:
        raise ZernioError("Could not reach Zernio.", details={"type": "network_error"}) from err


# --------------------------------------------------------------------------- #
# Metadata helpers                                                              #
# --------------------------------------------------------------------------- #


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def normalize_hashtags(value: Any) -> list[str]:
    """Return one # per hashtag, preserving order and removing duplicates."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in _strings(value):
        token = re.sub(r"^#+", "", raw).strip()
        if not token or re.search(r"\s", token):
            continue
        tag = "#" + token
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            result.append(tag)
    return result


def normalize_tags(value: Any) -> list[str]:
    """Return plain YouTube keywords within the documented per-tag/total limits."""
    result: list[str] = []
    seen: set[str] = set()
    total = 0
    for raw in _strings(value):
        tag = raw.lstrip("#").replace(",", " ").strip()
        tag = re.sub(r"\s+", " ", tag)
        if not tag or len(tag) > 100:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        if total + len(tag) + (1 if result else 0) > 500:
            break
        seen.add(key)
        result.append(tag)
        total += len(tag) + (1 if len(result) > 1 else 0)
    return result


def load_production(path: str | Path) -> dict[str, Any]:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise ZernioError(f"Could not read production.json: {err}") from err
    if not isinstance(doc, dict):
        raise ZernioError("production.json must contain a JSON object.")
    title = doc.get("title")
    if title is not None and not isinstance(title, str):
        raise ZernioError("production.json title must be a string when present.")
    for key in ("hashtags", "youtube_tags", "tags"):
        if key in doc and doc[key] is not None and not isinstance(doc[key], list):
            raise ZernioError(f"production.json {key} must be an array when present.")
    return doc


def aggregate_publishing_status(posts: list[dict[str, Any]]) -> str:
    """Derive the job state from the actual per-platform post states.

    The return value must stay within the §6.2 publishing-status enum
    (schemas/job_status.schema.json): not_requested | publishing | scheduled |
    published | partial | failed | cancelled. Provider-side per-post states
    like "requested" or "error" are mapped onto that enum here.
    """
    statuses = [str(post.get("status") or "").strip().lower()
                for post in posts if isinstance(post, dict)]
    statuses = [value for value in statuses if value]
    if not statuses:
        return "not_requested"
    active = {"requested", "publishing"}
    terminal_failures = {"failed", "cancelled", "error", "not_requested", "unknown"}
    if any(value in active for value in statuses):
        return "publishing"
    if any(value == "scheduled" for value in statuses):
        return "scheduled"
    if all(value == "published" for value in statuses):
        return "published"
    if any(value == "published" for value in statuses) and any(value in terminal_failures for value in statuses):
        return "partial"
    if all(value == "cancelled" for value in statuses):
        return "cancelled"
    if all(value in terminal_failures for value in statuses):
        return "failed"
    return "partial"


def production_metadata(doc: dict[str, Any]) -> dict[str, Any]:
    """Resolve only fields that ClipForge actually stores in production.json."""
    title = doc.get("title") if isinstance(doc.get("title"), str) else ""
    caption = ""
    for key in ("caption", "description"):
        if isinstance(doc.get(key), str) and doc[key].strip():
            caption = doc[key].strip()
            break
    if not caption:
        caption = title.strip()
    hashtags = normalize_hashtags(doc.get("hashtags", []))
    tags_source = doc.get("youtube_tags", doc.get("tags", []))
    return {
        "title": title.strip(),
        "caption": caption,
        "hashtags": hashtags,
        "tags": normalize_tags(tags_source),
        "metadata_source": {
            "title": "production.json.title" if title.strip() else None,
            "caption": "production.json.caption" if doc.get("caption") else ("production.json.description" if doc.get("description") else ("production.json.title" if title.strip() else None)),
            "hashtags": "production.json.hashtags",
            "tags": "production.json.youtube_tags" if "youtube_tags" in doc else ("production.json.tags" if "tags" in doc else None),
        },
    }


def validate_platform(value: str) -> str:
    platform = str(value or "").strip().lower()
    if platform not in PLATFORM_SET:
        raise ZernioError("Unsupported Zernio platform; choose TikTok, YouTube, or Instagram.")
    return platform


def validate_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode not in MODES:
        raise ZernioError("Unsupported publishing mode.")
    return mode


def validate_request_id(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return str(uuid.uuid4())
    if not REQUEST_ID_RE.fullmatch(raw):
        raise ZernioError("Invalid idempotency request id.")
    return raw


def caption_with_visible_hashtags(caption: str, hashtags: list[str]) -> str:
    """Return provider-visible caption text with normalized tags appended once."""
    base = str(caption or "").strip()
    visible_tags = " ".join(str(tag).strip() for tag in hashtags if str(tag).strip())
    if not visible_tags:
        return base
    return f"{base}\n\n{visible_tags}" if base else visible_tags


def build_platform_payload(
    production: dict[str, Any],
    platform: str,
    account_ids: list[str],
    media_url: str,
    *,
    mode: str = "publish_now",
    scheduled_for: str = "",
    timezone: str = "UTC",
) -> dict[str, Any]:
    platform = validate_platform(platform)
    mode = validate_mode(mode)
    ids = [str(value).strip() for value in account_ids if str(value).strip()]
    if not ids:
        raise ZernioError(f"No {platform} account was selected.")
    if not media_url or not media_url.startswith(("https://", "http://")):
        raise ZernioError("Zernio media URL must be an absolute HTTP(S) URL.")
    meta = production_metadata(production)
    platforms: list[dict[str, Any]] = []
    for account_id in ids:
        if platform == "instagram":
            # bug-52: every Instagram publish posts BOTH a Reel and a Story —
            # no user selection. Per docs.zernio.com/platforms/instagram a
            # single video without contentType publishes as a Reel; adding
            # contentType="story" publishes the same video to Stories. The
            # platformPostUrl/status come back per platform entry, so the two
            # entries stay individually trackable. The media URL must be a
            # direct CDN URL (Zernio presign publicUrl) — Drive/Dropbox/
            # OneDrive/iCloud share links return HTML Instagram cannot fetch.
            platforms.append({"platform": platform, "accountId": account_id})
            platforms.append({"platform": platform, "accountId": account_id, "contentType": "story"})
        else:
            platforms.append({"platform": platform, "accountId": account_id})
    payload: dict[str, Any] = {
        "content": caption_with_visible_hashtags(meta["caption"], meta["hashtags"]),
        "platforms": platforms,
        "mediaItems": [{"type": "video", "url": media_url}],
        "hashtags": meta["hashtags"],
        "metadata": {"source": "clipforge"},
    }
    if platform == "youtube":
        if meta["title"]:
            payload["title"] = meta["title"][:100]
        payload["tags"] = meta["tags"]
    if mode == "publish_now":
        payload["publishNow"] = True
    else:
        if not scheduled_for:
            raise ZernioError("A scheduled-for timestamp is required for scheduling.")
        payload["scheduledFor"] = scheduled_for
        payload["timezone"] = timezone or "UTC"
    return payload


def sanitize_account(account: Any) -> dict[str, Any] | None:
    if not isinstance(account, dict):
        return None
    account_id = account.get("_id") or account.get("id")
    platform = str(account.get("platform") or "").lower()
    if not account_id or platform not in PLATFORM_SET:
        return None
    return {
        "id": str(account_id),
        "platform": platform,
        "username": str(account.get("username") or ""),
        "displayName": str(account.get("displayName") or ""),
        "profileId": str(account.get("profileId") or ""),
        "isActive": account.get("isActive") is not False,
        "enabled": account.get("enabled") is not False,
        "needsReconnection": account.get("needsReconnection") is True,
    }


def accounts_from_response(payload: Any) -> list[dict[str, Any]]:
    values = payload.get("accounts", []) if isinstance(payload, dict) else []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        clean = sanitize_account(value)
        if clean and clean["id"] not in seen:
            seen.add(clean["id"])
            result.append(clean)
    return result


# --------------------------------------------------------------------------- #
# Media upload + publish flow                                                   #
# --------------------------------------------------------------------------- #


def presign_video(
    api_key: str,
    filename: str,
    size: int,
    content_type: str = "video/mp4",
    *,
    request_id: str | None = None,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    payload = request_json(
        "POST", "/media/presign", api_key,
        body={"filename": filename, "contentType": content_type, "size": int(size)},
        request_id=request_id, opener=opener,
    )
    if not isinstance(payload, dict) or not payload.get("uploadUrl") or not payload.get("publicUrl"):
        raise ZernioError("Zernio did not return both uploadUrl and publicUrl.")
    return {"uploadUrl": str(payload["uploadUrl"]), "publicUrl": str(payload["publicUrl"])}


def upload_binary(
    upload_url: str,
    path: str | Path,
    content_type: str = "video/mp4",
    *,
    opener: Callable[..., Any] = urlopen,
) -> None:
    """Upload directly from disk rather than reading a multi-GB video into RAM."""
    source = Path(path)
    size = source.stat().st_size
    headers = {"Content-Type": content_type, "Content-Length": str(size)}
    try:
        with source.open("rb") as stream:
            req = Request(upload_url, data=stream, headers=headers, method="PUT")
            with opener(req, timeout=900) as response:
                if response.status < 200 or response.status >= 300:
                    raise ZernioError(f"Zernio media upload failed (HTTP {response.status}).", status=response.status)
    except HTTPError as err:
        raise ZernioError(f"Zernio media upload failed (HTTP {err.code}).", status=err.code) from err
    except (OSError, URLError, TimeoutError) as err:
        raise ZernioError("Could not upload media to Zernio.", details={"type": "network_error"}) from err


def post_summary(payload: Any) -> dict[str, Any]:
    # Same x-request-id retries return the original record under existingPost;
    # normal create/retry/update responses use post. Both are authoritative.
    post = (payload.get("post") or payload.get("existingPost") or payload) if isinstance(payload, dict) else {}
    if not isinstance(post, dict):
        return {"status": "unknown"}
    platforms = []
    for target in post.get("platforms", []) if isinstance(post.get("platforms"), list) else []:
        if not isinstance(target, dict):
            continue
        item = {
            key: target.get(key)
            for key in ("platform", "accountId", "status", "platformPostId", "platformPostUrl")
            if target.get(key) is not None
        }
        if target.get("error"):
            item["error"] = str(target["error"])[:300]
        platforms.append(item)
    return {
        "post_id": str(post.get("_id") or post.get("id") or ""),
        "status": str(post.get("status") or "unknown").lower(),
        "scheduled_for": post.get("scheduledFor"),
        "timezone": post.get("timezone"),
        "platforms": platforms,
    }


def refresh_post_status(api_key: str, post_id: str, *, opener: Callable[..., Any] = urlopen) -> dict[str, Any]:
    """Read the authoritative current status for one Zernio post."""
    return post_summary(request_json(
        "GET", "/posts/" + quote(str(post_id), safe=""), api_key, opener=opener,
    ))


def reconcile_post_status(
    api_key: str,
    summary: dict[str, Any],
    *,
    mode: str,
    opener: Callable[..., Any] = urlopen,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval_seconds: float = 5.0,
    max_attempts: int = 12,
) -> dict[str, Any]:
    """Refresh an immediate post until every platform target is terminal."""
    if mode != "publish_now" or not summary.get("post_id"):
        return summary
    current = summary
    for attempt in range(max(1, int(max_attempts))):
        targets = current.get("platforms") if isinstance(current.get("platforms"), list) else []
        statuses = [str(target.get("status") or "unknown").lower()
                    for target in targets if isinstance(target, dict)]
        if statuses and all(status in TERMINAL_POST_STATUSES for status in statuses):
            return current
        if attempt + 1 >= max(1, int(max_attempts)):
            break
        sleep(max(0.0, float(poll_interval_seconds)))
        try:
            current = refresh_post_status(api_key, str(summary["post_id"]), opener=opener)
        except ZernioError as error:
            current = dict(current)
            current["refresh_error"] = str(error)[:300]
    return current


def _existing_post_on_duplicate(api_key: str, error: ZernioError, *, opener: Callable[..., Any]) -> dict[str, Any] | None:
    existing_id = ((error.details.get("details") or {}).get("existingPostId") if isinstance(error.details, dict) else None)
    if error.status != 409 or not existing_id:
        return None
    response = request_json("GET", "/posts/" + quote(str(existing_id), safe=""), api_key, opener=opener)
    summary = post_summary(response)
    summary["deduplicated"] = True
    return summary


def publish_video(
    api_key: str,
    production_path: str | Path,
    video_path: str | Path,
    targets: list[dict[str, Any]],
    *,
    mode: str = "publish_now",
    scheduled_for: str = "",
    timezone: str = "UTC",
    request_id: str | None = None,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Upload one final MP4 and create/recover one post per platform family."""
    mode = validate_mode(mode)
    production = load_production(production_path)
    metadata = production_metadata(production)
    logical_request_id = validate_request_id(request_id)
    source = Path(video_path)
    if not source.is_file() or source.stat().st_size <= 0:
        raise ZernioError("Final MP4 is missing or empty.")
    content_type = mimetypes.guess_type(source.name)[0] or "video/mp4"
    if content_type != "video/mp4":
        content_type = "video/mp4"
    upload = presign_video(
        api_key, source.name, source.stat().st_size, content_type,
        request_id=logical_request_id + "-media", opener=opener,
    )
    upload_binary(upload["uploadUrl"], source, content_type, opener=opener)
    posts: list[dict[str, Any]] = []
    for target in targets:
        platform = validate_platform(str(target.get("platform", "")))
        ids = target.get("account_ids") or target.get("accountIds") or []
        payload = build_platform_payload(
            production, platform, list(ids), upload["publicUrl"],
            mode=mode, scheduled_for=scheduled_for, timezone=timezone,
        )
        per_platform_id = logical_request_id + "-" + platform
        try:
            response = request_json("POST", "/posts", api_key, body=payload, request_id=per_platform_id, opener=opener)
            summary = post_summary(response)
            summary = reconcile_post_status(
                api_key, summary, mode=mode, opener=opener,
            )
        except ZernioError as err:
            summary = _existing_post_on_duplicate(api_key, err, opener=opener)
            if summary is not None:
                summary = reconcile_post_status(
                    api_key, summary, mode=mode, opener=opener,
                )
            if summary is None:
                # Preserve the failure beside any successful platform rather
                # than abandoning the entire multi-platform publishing state.
                summary = {
                    "post_id": "",
                    "status": "failed",
                    "error": str(err)[:300],
                    "error_details": err.details,
                }
        summary["platform"] = platform
        summary["request_id"] = per_platform_id
        posts.append(summary)
    overall = aggregate_publishing_status(posts)
    return {
        "status": overall,
        "posts": posts,
        "idempotency_key": logical_request_id,
    }


def discover_accounts(api_key: str, profile_id: str = "", *, opener: Callable[..., Any] = urlopen) -> list[dict[str, Any]]:
    query = ("?profileId=" + quote(profile_id, safe="")) if profile_id else ""
    return accounts_from_response(request_json("GET", "/accounts" + query, api_key, opener=opener))


def list_scheduled_posts(api_key: str, *, opener: Callable[..., Any] = urlopen) -> list[dict[str, Any]]:
    """Return the provider's current scheduled posts for collision-aware planning."""
    payload = request_json("GET", "/posts?status=scheduled&limit=100", api_key, opener=opener)
    values = payload.get("posts", []) if isinstance(payload, dict) else []
    result: list[dict[str, Any]] = []
    for post in values:
        if not isinstance(post, dict):
            continue
        when = post.get("scheduledFor")
        if not when:
            continue
        result.append({
            "post_id": str(post.get("_id") or post.get("id") or ""),
            "scheduled_for": when,
            "timezone": str(post.get("timezone") or "UTC"),
            "status": str(post.get("status") or "scheduled").lower(),
        })
    return result


def retry_post(api_key: str, post_id: str, *, opener: Callable[..., Any] = urlopen) -> dict[str, Any]:
    if not POST_ID_RE.fullmatch(str(post_id or "")):
        raise ZernioError("Invalid Zernio post id.")
    return post_summary(request_json("POST", "/posts/" + quote(post_id, safe="") + "/retry", api_key, opener=opener))


def update_post(
    api_key: str,
    post_id: str,
    *,
    mode: str,
    scheduled_for: str = "",
    timezone: str = "UTC",
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    if not POST_ID_RE.fullmatch(str(post_id or "")):
        raise ZernioError("Invalid Zernio post id.")
    mode = validate_mode(mode)
    body: dict[str, Any] = {"isDraft": False}
    if mode == "publish_now":
        body["publishNow"] = True
    else:
        if not scheduled_for:
            raise ZernioError("A scheduled-for timestamp is required when changing a schedule.")
        body["scheduledFor"] = scheduled_for
        body["timezone"] = timezone or "UTC"
    return post_summary(request_json("PUT", "/posts/" + quote(post_id, safe=""), api_key, body=body, opener=opener))


def cancel_post(api_key: str, post_id: str, *, opener: Callable[..., Any] = urlopen) -> dict[str, Any]:
    if not POST_ID_RE.fullmatch(str(post_id or "")):
        raise ZernioError("Invalid Zernio post id.")
    request_json("DELETE", "/posts/" + quote(post_id, safe=""), api_key, opener=opener)
    return {"post_id": post_id, "status": "cancelled"}


# --------------------------------------------------------------------------- #
# Job status.publishing helpers                                                 #
# --------------------------------------------------------------------------- #


def write_publishing_state(path: str | Path, publishing: dict[str, Any]) -> None:
    """Merge a log-safe publishing sibling into status.json without touching Stage B."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except json.JSONDecodeError:
            pass
    existing["publishing"] = publishing
    existing["updated_at_epoch"] = int(time.time())
    target.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def publishing_error_state(prior: dict[str, Any] | None = None) -> dict[str, Any]:
    """Preserve safe prior details while recording the original publisher failure."""
    publishing = dict(prior) if isinstance(prior, dict) else {}
    posts = publishing.get("posts")
    # §6.2: a publisher failure maps to "failed" (there is no "error" status).
    publishing["status"] = "failed"
    publishing["posts"] = posts if isinstance(posts, list) else []
    publishing["idempotency_key"] = str(publishing.get("idempotency_key") or "")
    return publishing


# --------------------------------------------------------------------------- #
# Manual-resolve (retry-vs-fresh) for publish.yml                               #
# --------------------------------------------------------------------------- #


def resolve_manual_dispatch(root: str | Path, job_id: str, settings_path: str | Path) -> tuple[str, str, str, str, str]:
    """Return (action, mode, timezone, targets_b64, post_id) for a manual publish.

    * action == "retry" when a prior queue entry for the job has at least one
      post_id; only ``post_id`` is populated in that case.
    * action == "publish" when no attempt has ever been recorded; ``mode``,
      ``timezone``, and ``targets_b64`` are populated from settings.
    * Raises ``SystemExit`` when the job or settings are missing/invalid — this
      mirrors the legacy CLI shape used by publish.yml.
    """
    root = Path(root).resolve()
    if not (root / "jobs" / job_id / "production.json").exists():
        raise SystemExit(f"No completed job found at jobs/{job_id}/production.json")

    queue_path = root / "branding" / "zernio_queue.json"
    existing_post_id = ""
    if queue_path.exists():
        try:
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Could not read {queue_path}: {exc}") from exc
        for item in queue.get("items", []) if isinstance(queue, dict) else []:
            if isinstance(item, dict) and str(item.get("job_id") or "") == job_id:
                post_ids = item.get("post_ids")
                if isinstance(post_ids, list) and post_ids:
                    existing_post_id = str(post_ids[0])
                break

    if existing_post_id:
        return "retry", "", "", "", existing_post_id

    settings_path = Path(settings_path)
    if not settings_path.exists():
        raise SystemExit("Zernio is not configured (branding/zernio_settings.json is missing).")
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        skip, mode, timezone, targets_b64 = automatic_fields(settings)
    except (OSError, json.JSONDecodeError, TargetValidationError) as exc:
        raise SystemExit(f"Could not resolve Zernio settings: {exc}") from exc
    if skip == "true" or not mode or not targets_b64:
        raise SystemExit(
            "Zernio automatic publishing is disabled or has no target accounts configured; "
            "nothing to publish to."
        )
    return "publish", mode, timezone, targets_b64, ""


# --------------------------------------------------------------------------- #
# Stage B automatic-publish dispatch (ARCHITECTURE.md §11 "optional auto-     #
# Publish"). Stage B's final step evaluates these committed files after the   #
# job's terminal `complete` status has been pushed and, when publishing is    #
# armed, dispatches publish.yml action=publish. A skip is ALWAYS a no-op      #
# success — auto-publish must never fail a completed Stage B run.             #
# --------------------------------------------------------------------------- #


def automatic_dispatch(
    root: str | Path,
    job_id: str,
    *,
    settings_path: str | Path | None = None,
    accounts_path: str | Path | None = None,
    secret_configured: bool = False,
) -> tuple[str, str, str, str, str, str]:
    """Return (dispatch, mode, timezone, targets_json, request_id, reason).

    * ``dispatch == "true"`` only when ALL gates pass; otherwise every output
      field except ``reason`` is empty and the caller must do nothing.
    * Gates (in order): the job's status.json exists and is ``complete``;
      settings exist with ``enabled && auto_publish`` and a valid
      ``automatic_mode``; ``ZERNIO_API_KEY`` is configured (flag only — the
      secret value is never read here); at least one selected target account
      is still active in the committed accounts snapshot.
    * ``request_id`` mirrors the bot's ``zernioRequestId`` reuse-on-failure
      rule: when the job's last publishing attempt failed, its recorded
      ``idempotency_key`` is reused so a re-render of the same job recovers
      the same logical publish; otherwise a fresh key is minted.
    """
    root = Path(root)
    settings_path = Path(settings_path) if settings_path else root / "branding" / "zernio_settings.json"
    accounts_path = Path(accounts_path) if accounts_path else root / "branding" / "zernio_accounts.json"

    def skip(reason: str) -> tuple[str, str, str, str, str, str]:
        return "false", "", "", "", "", reason

    status_path = root / "jobs" / job_id / "status.json"
    if not status_path.exists():
        return skip("job status.json is missing")
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return skip(f"job status.json is unreadable ({exc})")
    if not isinstance(status, dict) or status.get("state") != "complete":
        return skip("Stage B did not reach state=complete")

    if not settings_path.exists():
        return skip("branding/zernio_settings.json is missing")
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return skip(f"zernio_settings.json is unreadable ({exc})")
    if not isinstance(settings, dict):
        return skip("zernio_settings.json is not a JSON object")

    try:
        skip_flag, mode, timezone, targets_b64 = automatic_fields(settings)
    except TargetValidationError as exc:
        return skip(f"automatic settings are invalid ({exc})")
    if skip_flag == "true":
        return skip("Zernio automatic publishing is disabled or has no selected targets")

    if not secret_configured:
        return skip("ZERNIO_API_KEY is not configured in repository secrets")

    # Targets must still resolve against ACTIVE accounts from the committed
    # snapshot — a selected account that has since disconnected never
    # publishes (same rule the bot's zernioTargets applies).
    accounts_doc: dict[str, Any] = {}
    if accounts_path.exists():
        try:
            loaded = json.loads(accounts_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return skip(f"zernio_accounts.json is unreadable ({exc})")
        if isinstance(loaded, dict):
            accounts_doc = loaded
    active = active_accounts(accounts_doc)
    selected = normalize_targets(decode_targets(targets_b64))
    resolved: list[dict[str, Any]] = []
    for target in selected:
        platform = str(target.get("platform") or "")
        wanted = set(target.get("account_ids") or [])
        ids = [account["id"] for account in active.get(platform, []) if account["id"] in wanted]
        if ids:
            resolved.append({"platform": platform, "account_ids": ids})
    if not resolved:
        return skip("no selected target account is active in the accounts snapshot")
    targets_json = serialize_targets(resolved)

    # Reuse-on-failure idempotency, mirroring bot/src/zernio.js zernioRequestId.
    publishing = status.get("publishing") if isinstance(status.get("publishing"), dict) else {}
    prior = str(publishing.get("idempotency_key") or "") if str(publishing.get("status") or "").lower() == "failed" else ""
    if REQUEST_ID_RE.fullmatch(prior):
        request_id = prior
    else:
        request_id = f"clipforge-auto-{job_id}-{uuid.uuid4()}"

    return "true", mode, timezone, targets_json, request_id, "automatic publish armed"


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    meta = sub.add_parser("metadata")
    meta.add_argument("production")

    publish = sub.add_parser("publish")
    publish.add_argument("production")
    publish.add_argument("video")
    publish.add_argument("targets_json", help="JSON array of {platform, account_ids}")
    publish.add_argument("--api-key", default=os.environ.get("ZERNIO_API_KEY", ""))
    publish.add_argument("--mode", default="publish_now", choices=sorted(MODES))
    publish.add_argument("--scheduled-for", default="")
    publish.add_argument("--timezone", default="UTC")
    publish.add_argument("--request-id", default="")

    discover = sub.add_parser("discover")
    discover.add_argument("--profile-id", default="")
    discover.add_argument("--api-key", default=os.environ.get("ZERNIO_API_KEY", ""))

    scheduled = sub.add_parser("scheduled")
    scheduled.add_argument("--api-key", default=os.environ.get("ZERNIO_API_KEY", ""))

    state = sub.add_parser("state")
    state.add_argument("path")
    state.add_argument("state_json")

    retry = sub.add_parser("retry")
    retry.add_argument("post_id")
    retry.add_argument("--api-key", default=os.environ.get("ZERNIO_API_KEY", ""))

    update = sub.add_parser("update")
    update.add_argument("post_id")
    update.add_argument("--api-key", default=os.environ.get("ZERNIO_API_KEY", ""))
    update.add_argument("--mode", required=True, choices=sorted(MODES))
    update.add_argument("--scheduled-for", default="")
    update.add_argument("--timezone", default="UTC")

    cancel = sub.add_parser("cancel")
    cancel.add_argument("post_id")
    cancel.add_argument("--api-key", default=os.environ.get("ZERNIO_API_KEY", ""))

    payload = sub.add_parser("payload")
    payload.add_argument("production")
    payload.add_argument("platform", choices=sorted(PLATFORMS))
    payload.add_argument("account_ids", help="comma-separated Zernio account ids")
    payload.add_argument("media_url")
    payload.add_argument("--mode", default="publish_now", choices=sorted(MODES))
    payload.add_argument("--scheduled-for", default="")
    payload.add_argument("--timezone", default="UTC")

    schedule = sub.add_parser("schedule")
    schedule.add_argument("settings")
    schedule.add_argument("queue")
    schedule.add_argument("--external-posts", default="",
                          help="Optional JSON file with existing native Zernio scheduled posts")

    targets_auto = sub.add_parser("automatic-fields")
    targets_auto.add_argument("settings")

    encode = sub.add_parser("encode")
    encode.add_argument("targets_json")

    decode = sub.add_parser("decode")
    decode.add_argument("targets_b64")

    resolve = sub.add_parser("resolve-manual")
    resolve.add_argument("root")
    resolve.add_argument("job_id")
    resolve.add_argument("settings_path")

    autodispatch = sub.add_parser("automatic-dispatch")
    autodispatch.add_argument("root")
    autodispatch.add_argument("job_id")
    autodispatch.add_argument("--secret-configured", default="false",
                              help="'true' when ZERNIO_API_KEY exists in repository secrets; the value itself is never read")

    args = ap.parse_args(argv)

    if args.command == "metadata":
        _print(production_metadata(load_production(args.production)))
    elif args.command == "publish":
        try:
            targets = json.loads(args.targets_json)
        except json.JSONDecodeError as err:
            raise ZernioError("targets_json must be valid JSON.") from err
        if not isinstance(targets, list):
            raise ZernioError("targets_json must be a JSON array.")
        _print(publish_video(
            args.api_key, args.production, args.video, targets,
            mode=args.mode, scheduled_for=args.scheduled_for, timezone=args.timezone,
            request_id=args.request_id,
        ))
    elif args.command == "discover":
        _print({
            "accounts": discover_accounts(args.api_key, args.profile_id),
            "updated_at_epoch": int(time.time()),
        })
    elif args.command == "scheduled":
        _print(list_scheduled_posts(args.api_key))
    elif args.command == "state":
        try:
            state_doc = json.loads(args.state_json)
        except json.JSONDecodeError as err:
            raise ZernioError("state_json must be valid JSON.") from err
        if not isinstance(state_doc, dict):
            raise ZernioError("state_json must be a JSON object.")
        write_publishing_state(args.path, state_doc)
    elif args.command == "retry":
        _print(retry_post(args.api_key, args.post_id))
    elif args.command == "update":
        _print(update_post(
            args.api_key, args.post_id,
            mode=args.mode, scheduled_for=args.scheduled_for, timezone=args.timezone,
        ))
    elif args.command == "cancel":
        _print(cancel_post(args.api_key, args.post_id))
    elif args.command == "payload":
        _print(build_platform_payload(
            load_production(args.production), args.platform,
            args.account_ids.split(","), args.media_url,
            mode=args.mode, scheduled_for=args.scheduled_for, timezone=args.timezone,
        ))
    elif args.command == "schedule":
        settings = read_json(args.settings, {})
        queue = read_json(args.queue, default_queue())
        external = read_json(args.external_posts, []) if args.external_posts else []
        if not isinstance(external, list):
            external = []
        print(json.dumps(plan_smart_schedule(settings, queue, external_posts=external), ensure_ascii=False))
    elif args.command == "automatic-fields":
        data = json.loads(Path(args.settings).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TargetValidationError("settings must contain a JSON object")
        print("\t".join(automatic_fields(data)))
    elif args.command == "encode":
        print(encode_targets(json.loads(args.targets_json)))
    elif args.command == "decode":
        print(serialize_targets(decode_targets(args.targets_b64)))
    elif args.command == "resolve-manual":
        action, mode, timezone, targets_b64, post_id = resolve_manual_dispatch(
            args.root, args.job_id, args.settings_path,
        )
        print(f"{action}\t{mode}\t{timezone}\t{targets_b64}\t{post_id}")
    elif args.command == "automatic-dispatch":
        dispatch, mode, timezone, targets_json, request_id, reason = automatic_dispatch(
            args.root, args.job_id,
            secret_configured=str(args.secret_configured).strip().lower() == "true",
        )
        # Tab-safe transport: targets_json is compact JSON without tabs/newlines.
        print(f"{dispatch}\t{mode}\t{timezone}\t{targets_json}\t{request_id}\t{reason}")
    else:  # pragma: no cover — argparse guarantees an above branch
        raise SystemExit(f"Unknown zernio command: {args.command}")


if __name__ == "__main__":  # pragma: no cover
    try:
        main()
    except (ZernioError, TargetValidationError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
