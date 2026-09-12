#!/usr/bin/env python3
"""Shared ClipForge production.json contract loader and validator.

The declarative rule source is ``schemas/production_plan_contract.json``. This
module deliberately preserves the manual browser validator's compatibility
rules: optional metadata stays optional, legacy ``raw_narration`` remains an
accepted fallback, extra properties are allowed, and cuts must be sorted and
non-overlapping.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "schemas" / "production_plan_contract.json"


@lru_cache(maxsize=1)
def load_contract() -> dict[str, Any]:
    """Load and sanity-check the portable validation contract."""
    with CONTRACT_PATH.open(encoding="utf-8") as handle:
        contract = json.load(handle)
    if contract.get("version") != 1:
        raise RuntimeError("Unsupported production-plan contract version.")
    required_sections = {"top_level", "scalar_fields", "string_arrays", "cuts"}
    missing = sorted(required_sections - set(contract))
    if missing:
        raise RuntimeError("Production-plan contract is missing: " + ", ".join(missing))
    return contract


def is_int(value: Any) -> bool:
    """Match JavaScript's finite integer behavior without accepting booleans."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and int(value) == value


def validate_production_plan(document: Any, contract: dict[str, Any] | None = None) -> list[str]:
    """Return browser-equivalent validation errors for a parsed production plan."""
    rules = contract or load_contract()
    errors: list[str] = []

    if not isinstance(document, dict):
        return ["Top level must be a JSON object."]

    title_rules = rules.get("title", {})
    if title_rules.get("optional") and "title" in document:
        if not isinstance(document["title"], str) or not document["title"].strip():
            errors.append("`title` must be a non-empty string when present (or omit it entirely).")

    for key, array_rules in rules["string_arrays"].items():
        if array_rules.get("optional") and key not in document:
            continue
        value = document.get(key)
        if not isinstance(value, list):
            errors.append(f"`{key}` must be a JSON array of strings when present (or omit it entirely).")
            continue
        minimum = int(array_rules["minimum_items"])
        maximum = int(array_rules["maximum_items"])
        if len(value) < minimum or len(value) > maximum:
            errors.append(f"`{key}` must contain between {minimum} and {maximum} entries when present (got {len(value)}).")
        seen: dict[str, bool] = {}
        for index, entry in enumerate(value):
            at = f"`{key}[{index}]`"
            if not isinstance(entry, str) or not entry.strip():
                errors.append(f"{at} must be a non-empty string.")
                continue
            trimmed = entry.strip()
            required_prefix = array_rules.get("require_prefix")
            if required_prefix and not trimmed.startswith(str(required_prefix)):
                errors.append(f"{at} must start with `{required_prefix}` (got {json.dumps(entry)}).")
            if array_rules.get("forbid_whitespace") and any(char.isspace() for char in trimmed):
                errors.append(f"{at} must not contain whitespace inside a hashtag.")
            forbidden_prefix = array_rules.get("forbid_prefix")
            if forbidden_prefix and trimmed.startswith(str(forbidden_prefix)):
                errors.append(f"{at} must not start with `{forbidden_prefix}` (YouTube tags are plain keywords).")
            forbidden_substring = array_rules.get("forbid_substring")
            if forbidden_substring and str(forbidden_substring) in trimmed:
                errors.append(f"{at} must not contain a comma inside a single tag.")
            comparison = trimmed.lower() if array_rules.get("case_insensitive_unique") else trimmed
            if comparison in seen:
                errors.append(f"{at} duplicates an earlier entry ({json.dumps(entry)}).")
            else:
                seen[comparison] = True

    scalars = rules["scalar_fields"]
    for key, scalar_rules in scalars.items():
        value = document.get(key)
        if scalar_rules.get("required") and (not is_int(value) or value <= 0):
            errors.append(f"`{key}` must be a positive integer.")

    series_rules = rules.get("series", {})
    series_id = document.get(series_rules.get("id_field", "series_id"))
    series_start = None
    series_end = None
    if series_id is not None:
        id_field = series_rules.get("id_field", "series_id")
        part_field = series_rules.get("part_field", "series_part")
        start_key = series_rules.get("start_field", "series_start_seconds")
        end_key = series_rules.get("end_field", "series_end_seconds")
        final_key = series_rules.get("final_field", "series_final")
        summary_key = series_rules.get("summary_field", "series_summary")
        if not isinstance(series_id, str) or not series_id.strip(): errors.append(f"`{id_field}` must be a non-empty string for a series production plan.")
        if not is_int(document.get(part_field)) or document.get(part_field) <= 0: errors.append(f"`{part_field}` must be a positive integer for a series production plan.")
        if not is_int(document.get(start_key)) or document.get(start_key) < 0: errors.append(f"`{start_key}` must be a non-negative integer for a series production plan.")
        else: series_start = int(document[start_key])
        if not is_int(document.get(end_key)) or document.get(end_key) < 0: errors.append(f"`{end_key}` must be a non-negative integer for a series production plan.")
        else: series_end = int(document[end_key])
        if series_start is not None and series_end is not None and series_end <= series_start: errors.append(f"`{end_key}` must be greater than `{start_key}` for a series production plan.")
        if not isinstance(document.get(final_key), bool): errors.append(f"`{final_key}` must be boolean for a series production plan.")
        summary = document.get(summary_key)
        if not isinstance(summary, str) or not summary.strip(): errors.append(f"`{summary_key}` must be a non-empty string for a series production plan.")
        elif len(summary.strip()) > int(series_rules.get("summary_max_length", 1200)): errors.append(f"`{summary_key}` exceeds the maximum allowed length.")

    cut_rules = rules["cuts"]
    cuts = document.get("cuts")
    if not isinstance(cuts, list):
        errors.append("`cuts` must be an array.")
        return errors
    if len(cuts) < int(cut_rules["minimum_items"]):
        errors.append("`cuts` is empty — at least one cut is required.")
        return errors

    duration = document.get("video_duration_seconds")
    valid_duration = duration if is_int(duration) else None
    previous_end: int | float | None = None
    start_field = cut_rules["start_field"]
    end_field = cut_rules["end_field"]
    voiceover_field = cut_rules["voiceover_field"]
    legacy_voiceover_field = cut_rules["legacy_voiceover_field"]

    for index, cut in enumerate(cuts):
        at = f"cuts[{index}]"
        if not isinstance(cut, dict):
            errors.append(f"{at} must be an object.")
            continue
        start = cut.get(start_field)
        end = cut.get(end_field)
        if not is_int(start):
            errors.append(f"{at}.{start_field} must be an integer.")
        if not is_int(end):
            errors.append(f"{at}.{end_field} must be an integer.")
        voiceover = cut.get(voiceover_field)
        if not isinstance(voiceover, str) or not voiceover.strip():
            voiceover = cut.get(legacy_voiceover_field)
        if not isinstance(voiceover, str) or not voiceover.strip():
            errors.append(f"{at}.{voiceover_field} must be a non-empty string (legacy {legacy_voiceover_field} accepted).")
        if not is_int(start) or not is_int(end):
            continue
        if end <= start:
            errors.append(f"{at}: end_seconds ({end}) must be greater than start_seconds ({start}).")
        if start < int(cut_rules["start_minimum"]):
            errors.append(f"{at}: start_seconds ({start}) is below 0.")
        if series_start is not None and start < series_start: errors.append(f"{at}: start_seconds ({start}) precedes series_start_seconds ({series_start}).")
        if series_end is not None and end > series_end: errors.append(f"{at}: end_seconds ({end}) exceeds series_end_seconds ({series_end}).")
        if valid_duration is not None and end > valid_duration:
            errors.append(f"{at}: end_seconds ({end}) exceeds video_duration_seconds ({valid_duration}).")
        if previous_end is not None and start < previous_end:
            errors.append(
                f"{at}: starts at {start} which overlaps or precedes the previous cut ending at {previous_end} "
                "— cuts must not overlap and must be sorted ascending."
            )
        previous_end = end

    return errors


def parse_and_validate_production_plan(text: str, contract: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, list[str]]:
    """Parse JSON text and return the document plus browser-compatible errors."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [f"Not valid JSON: {exc.msg}."]
    return document, validate_production_plan(document, contract)
