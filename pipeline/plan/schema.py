"""ClipForge production.json validator (single source of truth, Python side).

This module implements the validation rules from ARCHITECTURE.md §7.3. It is
paired with ``bot/src/plan.js`` — the two implementations MUST produce the same
accept/reject decision and the same error strings for the same input. That
equivalence is enforced by the cross-validator tests in
``pipeline/tests/test_plan_cross_validation.py``.

Design notes for future sessions:

* ARCHITECTURE.md §7.3's nested ``series`` object is canonical (operator
  decision, 2026-08-26 — see resolved concern ``series-shape-nested-vs-flat``
  in ``BUILD_PROGRESS.json``). The legacy flat ``series_*`` sibling fields are
  accepted here as input only, for plans already in flight and legacy tooling
  — never emitted by current prompts. Do not remove flat-input acceptance
  without a separate operator call; it's still live back-compat, not dead
  code. Same precedent as accepting ``raw_narration`` alongside
  ``voiceover_text``.
* Unknown top-level fields are permitted (forward compatibility).
* This validator NEVER trusts the producer. Stage B re-runs it before render
  even though the bot ran it at upload time (§13 invariant #5).
"""

from __future__ import annotations

import json
import math
from typing import Any


# --------------------------------------------------------------------------- #
# Low-level type helpers                                                       #
# --------------------------------------------------------------------------- #

def _is_int(value: Any) -> bool:
    """True iff ``value`` is a finite whole number and not a bool.

    Mirrors ``Number.isFinite(v) && Math.floor(v) === v`` in JS while rejecting
    ``bool`` (which is an ``int`` subclass in Python but not treated as an
    integer in JSON semantics).
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value) and float(value).is_integer()
    return False


def _as_int(value: Any) -> int:
    """Return the integer view of a value already known to satisfy ``_is_int``."""
    return int(value)


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


# --------------------------------------------------------------------------- #
# Series-shape normalization                                                   #
# --------------------------------------------------------------------------- #

# Fields the nested-series object exposes -> the flat legacy sibling name.
# Kept explicit so any drift is obvious.
_NESTED_TO_FLAT = {
    "series_id":     "series_id",
    "part":          "series_part",
    "start_seconds": "series_start_seconds",
    "end_seconds":   "series_end_seconds",
    "is_final":      "series_final",
    "summary":       "series_summary",
}


def _extract_series(document: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Return ``(is_series_plan, values)``.

    ``is_series_plan`` is True whenever the document declares series data in
    either shape. ``values`` is a dict keyed by nested-shape field names
    (``series_id``, ``part``, ``start_seconds``, ``end_seconds``, ``is_final``,
    ``summary``). Missing fields are absent from ``values``.

    Precedence when both shapes are present: nested ``series`` wins for each
    field it defines. This lets a producer that only knows the flat shape still
    work, while a mixed document is deterministic.
    """
    nested = document.get("series")
    values: dict[str, Any] = {}
    has_flat = False
    has_nested = isinstance(nested, dict)

    for nested_key, flat_key in _NESTED_TO_FLAT.items():
        if flat_key in document:
            has_flat = True
            values[nested_key] = document[flat_key]
        if has_nested and nested_key in nested:
            values[nested_key] = nested[nested_key]

    is_series_plan = has_flat or has_nested
    return is_series_plan, values


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def validate_production_plan(document: Any) -> list[str]:
    """Return a list of validation error strings.

    An empty list means the document is valid.

    Callers should treat any non-empty return as untrusted and refuse to render
    (see ARCHITECTURE.md §13 invariant #5).
    """
    errors: list[str] = []

    if not isinstance(document, dict):
        return ["Top level must be a JSON object."]

    # -- Optional title ------------------------------------------------------ #
    if "title" in document:
        if not _is_nonempty_string(document["title"]):
            errors.append("`title` must be a non-empty string when present (or omit it entirely).")

    # -- Required positive-integer scalars ---------------------------------- #
    for key in ("video_duration_seconds", "target_total_duration_seconds"):
        value = document.get(key)
        if not _is_int(value) or _as_int(value) <= 0:
            errors.append(f"`{key}` must be a positive integer.")

    # -- Optional tag arrays ------------------------------------------------- #
    _validate_string_array(
        document.get("hashtags"),
        name="hashtags",
        present=("hashtags" in document),
        minimum=5,
        maximum=8,
        require_prefix="#",
        forbid_whitespace=True,
        errors=errors,
    )
    _validate_string_array(
        document.get("youtube_tags"),
        name="youtube_tags",
        present=("youtube_tags" in document),
        minimum=10,
        maximum=20,
        forbid_prefix="#",
        forbid_substring=",",
        errors=errors,
    )

    # -- Series (optional) --------------------------------------------------- #
    is_series_plan, series_values = _extract_series(document)
    series_start: int | None = None
    series_end: int | None = None
    if is_series_plan:
        sid = series_values.get("series_id")
        if not _is_nonempty_string(sid):
            errors.append("`series_id` must be a non-empty string for a series production plan.")

        part = series_values.get("part")
        if not _is_int(part) or _as_int(part) <= 0:
            errors.append("`series_part` must be a positive integer for a series production plan.")

        start_val = series_values.get("start_seconds")
        if not _is_int(start_val) or _as_int(start_val) < 0:
            errors.append("`series_start_seconds` must be a non-negative integer for a series production plan.")
        else:
            series_start = _as_int(start_val)

        end_val = series_values.get("end_seconds")
        if not _is_int(end_val) or _as_int(end_val) < 0:
            errors.append("`series_end_seconds` must be a non-negative integer for a series production plan.")
        else:
            series_end = _as_int(end_val)

        if series_start is not None and series_end is not None and series_end <= series_start:
            errors.append("`series_end_seconds` must be greater than `series_start_seconds`.")

        if not isinstance(series_values.get("is_final"), bool):
            errors.append("`series_final` must be boolean for a series production plan.")

        summary = series_values.get("summary")
        if not _is_nonempty_string(summary):
            errors.append("`series_summary` must be a non-empty string for a series production plan.")
        elif len(summary.strip()) > 1200:  # type: ignore[arg-type]
            errors.append("`series_summary` exceeds the maximum allowed length.")

    # -- Cuts ---------------------------------------------------------------- #
    cuts = document.get("cuts")
    if not isinstance(cuts, list):
        errors.append("`cuts` must be an array.")
        return errors
    if len(cuts) < 1:
        errors.append("`cuts` is empty — at least one cut is required.")
        return errors

    duration = document.get("video_duration_seconds")
    valid_duration = _as_int(duration) if _is_int(duration) else None
    previous_end: int | None = None

    for index, cut in enumerate(cuts):
        at = f"cuts[{index}]"
        if not isinstance(cut, dict):
            errors.append(f"{at} must be an object.")
            continue

        start = cut.get("start_seconds")
        end = cut.get("end_seconds")

        if not _is_int(start):
            errors.append(f"{at}.start_seconds must be an integer.")
        if not _is_int(end):
            errors.append(f"{at}.end_seconds must be an integer.")

        narration = cut.get("voiceover_text")
        if not _is_nonempty_string(narration):
            narration = cut.get("raw_narration")
        if not _is_nonempty_string(narration):
            errors.append(f"{at}.voiceover_text must be a non-empty string (legacy raw_narration accepted).")

        if not _is_int(start) or not _is_int(end):
            continue

        s = _as_int(start)
        e = _as_int(end)

        if s < 0:
            errors.append(f"{at}.start_seconds must be at least 0.")
        if series_start is not None and s < series_start:
            errors.append(f"{at}.start_seconds precedes series_start_seconds.")
        if series_end is not None and e > series_end:
            errors.append(f"{at}.end_seconds exceeds series_end_seconds.")
        if e <= s:
            errors.append(f"{at}.end_seconds must be greater than start_seconds.")
        if valid_duration is not None and e > valid_duration:
            errors.append(f"{at}.end_seconds exceeds video_duration_seconds.")
        if previous_end is not None and s < previous_end:
            errors.append(f"{at} overlaps or precedes the prior cut.")
        previous_end = e

    return errors


def parse_and_validate_production_plan(text: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Parse JSON ``text`` and validate the resulting document.

    Returns ``(document_or_None, errors)``. On JSON parse failure the document
    is ``None`` and errors contains a single "Not valid JSON: …" line.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [f"Not valid JSON: {exc.msg}."]
    return document, validate_production_plan(document)


# --------------------------------------------------------------------------- #
# Internal helpers                                                             #
# --------------------------------------------------------------------------- #

def _validate_string_array(
    value: Any,
    *,
    name: str,
    present: bool,
    minimum: int,
    maximum: int,
    errors: list[str],
    require_prefix: str | None = None,
    forbid_whitespace: bool = False,
    forbid_prefix: str | None = None,
    forbid_substring: str | None = None,
) -> None:
    if not present:
        return
    if not isinstance(value, list):
        errors.append(f"`{name}` must be an array of strings when present.")
        return
    if len(value) < minimum or len(value) > maximum:
        errors.append(f"`{name}` must contain between {minimum} and {maximum} entries.")

    seen: set[str] = set()
    for index, entry in enumerate(value):
        at = f"{name}[{index}]"
        if not _is_nonempty_string(entry):
            errors.append(f"{at} must be a non-empty string.")
            continue
        cleaned = entry.strip()
        if require_prefix is not None and not cleaned.startswith(require_prefix):
            errors.append(f"{at} must start with {require_prefix}.")
        if forbid_whitespace and any(ch.isspace() for ch in cleaned):
            errors.append(f"{at} must not contain whitespace.")
        if forbid_prefix is not None and cleaned.startswith(forbid_prefix):
            errors.append(f"{at} must not start with {forbid_prefix}.")
        if forbid_substring is not None and forbid_substring in cleaned:
            errors.append(f"{at} must not contain {forbid_substring}.")
        comparable = cleaned.lower()
        if comparable in seen:
            errors.append(f"{at} duplicates an earlier entry.")
        else:
            seen.add(comparable)


__all__ = ["validate_production_plan", "parse_and_validate_production_plan"]
