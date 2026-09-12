#!/usr/bin/env python3
"""Validate Zernio target groups and transport them safely through shell inputs."""
from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path
from typing import Any

PLATFORMS = ("tiktok", "youtube")
MODES = {"publish_now", "smart_schedule"}
TIMEZONE_RE = re.compile(r"^(?:UTC|[A-Za-z_]+(?:/[A-Za-z_+\-]+)+)$")


class TargetValidationError(ValueError):
    pass


def normalize_targets(value: Any, *, allow_empty: bool = False) -> list[dict[str, Any]]:
    """Accept only non-empty TikTok/YouTube groups with string account IDs."""
    if not isinstance(value, list):
        raise TargetValidationError("targets must be a JSON array")
    out: list[dict[str, Any]] = []
    seen_platforms: set[str] = set()
    for target in value:
        if not isinstance(target, dict):
            raise TargetValidationError("each target must be a JSON object")
        platform = target.get("platform")
        ids = target.get("account_ids")
        if platform not in PLATFORMS:
            raise TargetValidationError("target platform must be tiktok or youtube")
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


def automatic_fields(settings: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return tab-safe fields for Stage B: skip, mode, IANA timezone, JSON b64."""
    enabled = settings.get("enabled") is True
    automatic = settings.get("auto_publish") is True
    mode = settings.get("automatic_mode")
    schedule = settings.get("smart_schedule") if isinstance(settings.get("smart_schedule"), dict) else {}
    timezone = str(schedule.get("timezone") or "UTC")
    targets = targets_from_settings(settings)
    if not (enabled and automatic and mode in MODES and targets):
        return "true", "", "", ""
    if not TIMEZONE_RE.fullmatch(timezone):
        raise TargetValidationError("automatic publishing requires a safe IANA timezone")
    return "false", mode, timezone, encode_targets(targets)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    auto = sub.add_parser("automatic-fields")
    auto.add_argument("settings")
    encode = sub.add_parser("encode")
    encode.add_argument("targets_json")
    decode = sub.add_parser("decode")
    decode.add_argument("targets_b64")
    args = parser.parse_args()
    if args.command == "automatic-fields":
        data = json.loads(Path(args.settings).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TargetValidationError("settings must contain a JSON object")
        print("\t".join(automatic_fields(data)))
    elif args.command == "encode":
        print(encode_targets(json.loads(args.targets_json)))
    else:
        print(serialize_targets(decode_targets(args.targets_b64)))


if __name__ == "__main__":
    try:
        main()
    except (OSError, json.JSONDecodeError, TargetValidationError) as exc:
        raise SystemExit(str(exc)) from exc
