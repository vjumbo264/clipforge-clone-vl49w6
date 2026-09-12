#!/usr/bin/env python3
"""Persistent, log-safe state helpers for ClipForge's optional Zernio layer.

The repository is the durable store.  Global configuration and the publishing
queue live under branding/, while each job keeps a publishing-request.json and
status.json sibling.  No function in this module handles API secrets.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PLATFORMS = {"tiktok", "youtube"}
PUBLISHING_MODES = {"publish_now", "manual_schedule", "smart_schedule"}
TERMINAL_QUEUE_STATUSES = {"published", "failed", "cancelled", "not_requested"}
ACTIVE_QUEUE_STATUSES = {"requested", "queued", "scheduled", "publishing", "partial"}
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


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


def normalise_platforms(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value:
        platform = str(raw or "").strip().lower()
        if platform in PLATFORMS and platform not in result:
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
    return datetime.combine(day, time(hour=hour, minute=minute), tzinfo=zone)


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
    result: dict[str, list[dict[str, str]]] = {"tiktok": [], "youtube": []}
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
