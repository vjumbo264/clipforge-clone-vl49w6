"""Stage B series-metadata reconciler (bug-56).

The Stage B boundary validation in ``.github/workflows/stage-b.yml`` used to
hard-fail whenever an AI-authored production.json carried the wrong
``series.series_id`` / ``series.part`` / ``series.start_seconds`` compared to
the durable Stage A request (``jobs/<jid>/stage-a-request.json``). That safety
check is REAL and cannot be deleted — a silent wrong render would corrupt the
series far worse than a loud failure. But those three fields are also entirely
derived from the durable request; the operator does not retype them, and they
do not describe any content the AI actually generates (footage cuts,
narration, captions). It is therefore safe — and desirable — to silently
overwrite an AI-submitted wrong value with the known correct one, letting the
render proceed instead of forcing a manual redo of production.json.

This module contains the PURE reconciliation logic so it can be unit tested
offline. The workflow calls :func:`reconcile_series_metadata` after the plan
validates (see stage-b.yml, "Resolve production.json" step); the caller writes
the corrected plan back to ``work/production.json`` before proceeding.

DELIBERATE NON-GOALS
--------------------

* This module NEVER touches fields the AI legitimately owns:
  ``end_seconds`` (the AI's creative choice of where to end this part),
  ``is_final`` (an editorial signal), ``summary``, ``title``, ``cuts``,
  ``hashtags``, ``youtube_tags``, ``video_duration_seconds``,
  ``target_total_duration_seconds`` — every one of those still hard-fails
  if it disagrees with what the plan/request expects (see stage-b.yml and
  ``pipeline/plan/schema.validate_production_plan``).

* This module NEVER runs on a non-series job. A plan with no series block
  and a request with ``series.enabled`` not True is passed through
  unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pipeline.plan import schema as plan_schema


# Fields the reconciler is willing to silently overwrite. Each one is derived
# from the durable stage-a-request.json and does not describe any content the
# AI author actually generated. Ordering here is the same as the log output.
_SAFE_FIELDS: tuple[str, ...] = ("series_id", "part", "start_seconds")


@dataclass
class ReconciliationResult:
    """Outcome of a reconciliation pass.

    Attributes
    ----------
    plan:
        The (possibly mutated) plan document. Always the same identity as
        the input dict — the reconciler mutates in place so the caller can
        write it back without worrying about which reference it is holding.
    changes:
        Ordered list of ``(field_name, submitted_value, corrected_value)``
        tuples for every field the reconciler overwrote. Empty when the
        plan was already correct or when the job was not a series job.
    reason:
        Short human-readable summary of what happened. Suitable for
        printing to the GitHub Actions log.
    """

    plan: dict[str, Any]
    changes: list[tuple[str, Any, Any]] = field(default_factory=list)
    reason: str = ""


def _extract_plan_series(plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(nested_series_obj_or_new, flat_shadow)``.

    ``nested_series_obj_or_new`` is the plan's existing nested ``series``
    dict if present, otherwise a fresh one attached to the plan under
    ``series`` (so the caller's write-back captures the correction).
    ``flat_shadow`` is a dict view of any legacy ``series_*`` sibling fields
    on the plan document itself, so the reconciler can rewrite both shapes
    when both are present (nested-wins for reads, both-writes for writes —
    the shared shape).
    """
    nested = plan.get("series")
    if not isinstance(nested, dict):
        nested = {}
        plan["series"] = nested
    flat_shadow: dict[str, Any] = {}
    for nested_key, flat_key in plan_schema._NESTED_TO_FLAT.items():
        if flat_key in plan:
            flat_shadow[nested_key] = flat_key
    return nested, flat_shadow


def _coerce_nonneg_int(value: Any) -> int | None:
    """Return ``value`` as a non-negative int if possible, else None."""
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    if result < 0:
        return None
    return result


def _expected_from_request(request: dict[str, Any]) -> dict[str, Any] | None:
    """Return the authoritative series metadata from a Stage A request.

    Returns None when the request is not a series request. Coerces the two
    integer fields defensively so a malformed request cannot crash the
    reconciler — a request with unusable expected values simply means no
    reconciliation is attempted and the caller's downstream mismatch check
    fires normally.
    """
    if not isinstance(request, dict):
        return None
    series_req = request.get("series")
    if not isinstance(series_req, dict) or series_req.get("enabled") is not True:
        return None
    series_id = series_req.get("series_id")
    if not isinstance(series_id, str) or series_id.strip() == "":
        return None
    part = _coerce_nonneg_int(series_req.get("part"))
    if part is None or part < 1:
        return None
    start_seconds = _coerce_nonneg_int(series_req.get("start_seconds"))
    if start_seconds is None:
        return None
    return {
        "series_id": series_id,
        "part": part,
        "start_seconds": start_seconds,
    }


def reconcile_series_metadata(
    plan: dict[str, Any],
    request: dict[str, Any] | None,
) -> ReconciliationResult:
    """Silently auto-correct safe series metadata on ``plan`` in place.

    Parameters
    ----------
    plan:
        The parsed production.json document. MUTATED IN PLACE when any of
        the safe fields (``series_id``, ``part``, ``start_seconds``)
        disagree with the durable request. The caller is expected to write
        the resulting plan back to ``work/production.json`` before proceed-
        ing to render.
    request:
        The parsed ``jobs/<jid>/stage-a-request.json`` document, or None
        when the durable request is missing. When the request is missing
        or not a valid series request, the plan is returned unchanged and
        no reconciliation is attempted.

    Returns
    -------
    ReconciliationResult
        See the dataclass docstring. Never raises; malformed input yields
        an empty-changes result so the caller's normal validation path
        stays authoritative.
    """
    if not isinstance(plan, dict):
        # Defensive: schema validation runs before this and would already have
        # rejected a non-dict plan; returning an empty result keeps the shape
        # of the return value predictable.
        return ReconciliationResult(plan={}, reason="plan is not a JSON object; skipping reconciliation")

    expected = _expected_from_request(request or {})
    if expected is None:
        return ReconciliationResult(plan=plan, reason="job is not a series part; skipping reconciliation")

    # Read the plan's currently-declared series metadata using the same
    # per-field nested-wins precedence the rest of the pipeline uses.
    _is_series_plan, current = plan_schema._extract_series(plan)
    nested, flat_shadow = _extract_plan_series(plan)

    changes: list[tuple[str, Any, Any]] = []
    for field_name in _SAFE_FIELDS:
        expected_value = expected[field_name]
        submitted_value = current.get(field_name)
        # Compare defensively so a str/int mismatch on part/start_seconds
        # is treated as a mismatch, not a spurious pass.
        if field_name == "series_id":
            submitted_norm: Any = submitted_value if isinstance(submitted_value, str) else None
            match = submitted_norm == expected_value
        else:
            submitted_norm = _coerce_nonneg_int(submitted_value)
            match = submitted_norm == expected_value
        if match:
            continue
        # Overwrite BOTH shapes: nested (canonical) and any flat sibling
        # that was present on the input plan, so a plan that used the
        # legacy flat shape still writes back correctly.
        nested[field_name] = expected_value
        if field_name in flat_shadow:
            plan[flat_shadow[field_name]] = expected_value
        changes.append((field_name, submitted_value, expected_value))

    if not changes:
        return ReconciliationResult(plan=plan, reason="series metadata already matches the durable Stage A request")

    reason_parts = [
        f"{name}: {submitted!r} -> {corrected!r}"
        for name, submitted, corrected in changes
    ]
    return ReconciliationResult(
        plan=plan,
        changes=changes,
        reason="series metadata auto-corrected from stage-a-request.json (" + "; ".join(reason_parts) + ")",
    )


def format_log_line(result: ReconciliationResult) -> str:
    """Return the single-line workflow log message for ``result``.

    Kept as its own function so the workflow YAML has a stable one-liner
    to emit (and so the unit tests can pin its format).
    """
    if not result.changes:
        return f"Series reconciliation: no changes ({result.reason})."
    return (
        "Series reconciliation: auto-corrected "
        + str(len(result.changes))
        + " field(s) from the durable Stage A request. "
        + result.reason
    )


__all__ = [
    "ReconciliationResult",
    "reconcile_series_metadata",
    "format_log_line",
]
