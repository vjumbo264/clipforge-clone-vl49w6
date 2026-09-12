#!/usr/bin/env python3
"""ClipForge's server-side Zernio publishing helper.

The helper creates one Zernio post per platform family so YouTube-only metadata
never reaches TikTok. It contains no credentials in files, stdout, or persisted
job state; the API key is supplied only through the runner environment.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API_BASE = os.environ.get("ZERNIO_API_BASE", "https://zernio.com/api/v1").rstrip("/")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PLATFORMS = {"tiktok", "youtube"}
MODES = {"publish_now", "manual_schedule", "smart_schedule"}


class ZernioError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.details = details or {}


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
    """Derive the job state from the actual per-platform post states."""
    statuses = [str(post.get("status") or "unknown").strip().lower()
                for post in posts if isinstance(post, dict)]
    if not statuses:
        return "unknown"
    active = {"requested", "publishing"}
    terminal_failures = {"failed", "cancelled", "error", "not_requested"}
    if any(value in active for value in statuses):
        return "publishing"
    if any(value == "scheduled" for value in statuses):
        return "scheduled"
    if all(value == "published" for value in statuses):
        return "published"
    if any(value == "published" for value in statuses) and any(value in terminal_failures for value in statuses):
        return "partial"
    if all(value in terminal_failures for value in statuses):
        return "failed"
    return "partial" if any(value == "partial" for value in statuses) else statuses[0]


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
    if platform not in PLATFORMS:
        raise ZernioError("Unsupported Zernio platform; choose TikTok or YouTube.")
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
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,200}", raw):
        raise ZernioError("Invalid idempotency request id.")
    return raw


def caption_with_visible_hashtags(caption: str, hashtags: list[str]) -> str:
    """Return provider-visible caption text with normalized tags appended once."""
    base = str(caption or '').strip()
    visible_tags = ' '.join(str(tag).strip() for tag in hashtags if str(tag).strip())
    if not visible_tags:
        return base
    return f'{base}\n\n{visible_tags}' if base else visible_tags


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
    payload: dict[str, Any] = {
        "content": caption_with_visible_hashtags(meta["caption"], meta["hashtags"]),
        "platforms": [{"platform": platform, "accountId": account_id} for account_id in ids],
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
    if not account_id or platform not in PLATFORMS:
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


TERMINAL_POST_STATUSES = {"published", "failed", "cancelled"}


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
        "provider": "zernio",
        "status": overall,
        "mode": mode,
        "scheduled_for": scheduled_for or None,
        "timezone": timezone or None,
        "idempotency_key": logical_request_id,
        "updated_at_epoch": int(time.time()),
        "media": {"filename": source.name, "size_bytes": source.stat().st_size, "public_url_present": True},
        "metadata": metadata,
        "posts": posts,
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
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", str(post_id or "")):
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
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", str(post_id or "")):
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
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", str(post_id or "")):
        raise ZernioError("Invalid Zernio post id.")
    request_json("DELETE", "/posts/" + quote(post_id, safe=""), api_key, opener=opener)
    return {"post_id": post_id, "status": "cancelled"}


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


def main() -> None:
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
    args = ap.parse_args()
    if args.command == "metadata":
        print(json.dumps(production_metadata(load_production(args.production)), ensure_ascii=False, indent=2))
    elif args.command == "publish":
        try:
            targets = json.loads(args.targets_json)
        except json.JSONDecodeError as err:
            raise ZernioError("targets_json must be valid JSON.") from err
        if not isinstance(targets, list):
            raise ZernioError("targets_json must be a JSON array.")
        print(json.dumps(publish_video(args.api_key, args.production, args.video, targets, mode=args.mode, scheduled_for=args.scheduled_for, timezone=args.timezone, request_id=args.request_id), ensure_ascii=False, indent=2))
    elif args.command == "discover":
        print(json.dumps({"accounts": discover_accounts(args.api_key, args.profile_id), "updated_at_epoch": int(time.time())}, ensure_ascii=False, indent=2))
    elif args.command == "scheduled":
        print(json.dumps(list_scheduled_posts(args.api_key), ensure_ascii=False, indent=2))
    elif args.command == "state":
        try:
            state_doc = json.loads(args.state_json)
        except json.JSONDecodeError as err:
            raise ZernioError("state_json must be valid JSON.") from err
        if not isinstance(state_doc, dict):
            raise ZernioError("state_json must be a JSON object.")
        write_publishing_state(args.path, state_doc)
    elif args.command == "retry":
        print(json.dumps(retry_post(args.api_key, args.post_id), ensure_ascii=False, indent=2))
    elif args.command == "update":
        print(json.dumps(update_post(args.api_key, args.post_id, mode=args.mode, scheduled_for=args.scheduled_for, timezone=args.timezone), ensure_ascii=False, indent=2))
    elif args.command == "cancel":
        print(json.dumps(cancel_post(args.api_key, args.post_id), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(build_platform_payload(load_production(args.production), args.platform, args.account_ids.split(","), args.media_url, mode=args.mode, scheduled_for=args.scheduled_for, timezone=args.timezone), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except ZernioError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
