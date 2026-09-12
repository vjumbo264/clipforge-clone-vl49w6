"""ClipForge job status writer — the single Python entry point for updating
``jobs/<job_id>/status.json``.

Every stage that runs in GitHub Actions imports this module (or invokes it as
a script) to write status *before* starting its risky work and *after*
finishing it, so a crashed run always leaves a resumable record. See
ARCHITECTURE.md §6.1 (states) and §6.2 (schema).

The schema is small and stable. This module is intentionally free of external
deps: it must run in a bare Actions job that has installed nothing yet.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable


STATUS_VERSION = 2

# States permitted by the schema. Ordered as in ARCHITECTURE.md §6.1.
VALID_STATES: tuple[str, ...] = (
    "queued",
    "stage_a_running",
    "awaiting_torrent_selection",
    "awaiting_plan",
    "stage_b_queued",
    "stage_b_running",
    "complete",
    "error",
    "cancelled",
)

TERMINAL_STATES: frozenset[str] = frozenset({"complete", "error", "cancelled"})

VALID_MODES: tuple[str, ...] = ("manual",)

VALID_PUBLISHING_STATUSES: tuple[str, ...] = (
    "not_requested",
    "publishing",
    "scheduled",
    "published",
    "partial",
    "failed",
    "cancelled",
)

# Job-id character set matches ARCHITECTURE.md §6.3.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_JOB_ID_MAX_LEN = 120

# Default TTL for a job's small state — the FALLBACK only, used when the
# operator has not configured one. The operator-facing TTL lives in ONE place:
# the repo Actions variable CLIPFORGE_TTL_SECONDS (currently 172800 = 48h),
# which .github/workflows/stage-a.yml and .github/workflows/cleanup.yml both
# pass into the code as the CLIPFORGE_TTL_SECONDS environment variable, and
# bot/wrangler.*.jsonc mirrors into the Telegram bot as env var
# CLIPFORGE_JOB_TTL_SECONDS (bot/src/jobs.js). ALL of these must agree — this
# constant is only the last-resort default when none of them is set (e.g.
# local runs, unit tests). See ARCHITECTURE.md §12 and TTL_FIX_PROGRESS.json.
DEFAULT_TTL_SECONDS = 12 * 3600

TTL_ENV_VAR = "CLIPFORGE_TTL_SECONDS"


def configured_ttl_seconds(env: dict[str, str] | None = None) -> int:
    """The TTL for NEWLY created job status records, in seconds.

    Reads ``CLIPFORGE_TTL_SECONDS`` from the environment (set by
    stage-a.yml / cleanup.yml from the single repo Actions variable of the
    same name); falls back to ``DEFAULT_TTL_SECONDS`` when unset, and to the
    default on unparsable/non-positive values (a bad override must never
    crash a job's very first status write).
    """
    source = os.environ if env is None else env
    try:
        value = int(str(source.get(TTL_ENV_VAR, "")).strip())
    except (TypeError, ValueError):
        return DEFAULT_TTL_SECONDS
    return value if value > 0 else DEFAULT_TTL_SECONDS


# --------------------------------------------------------------------------- #
# Validation & construction                                                    #
# --------------------------------------------------------------------------- #

def is_valid_job_id(job_id: Any) -> bool:
    return (
        isinstance(job_id, str)
        and 1 <= len(job_id) <= _JOB_ID_MAX_LEN
        and _JOB_ID_RE.match(job_id) is not None
    )


def new_status(
    *,
    job_id: str,
    mode: str,
    state: str = "queued",
    message: str = "",
    now_epoch: int | None = None,
    ttl_seconds: int | None = None,
    series: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a fresh, schema-conformant status record.

    ``ttl_seconds`` None (the default) resolves through
    ``configured_ttl_seconds()`` — i.e. the CLIPFORGE_TTL_SECONDS environment
    variable set from the single repo Actions variable, falling back to
    ``DEFAULT_TTL_SECONDS``. Callers should NOT hardcode a TTL.
    """
    ttl = configured_ttl_seconds() if ttl_seconds is None else int(ttl_seconds)
    if not is_valid_job_id(job_id):
        raise ValueError(f"invalid job_id: {job_id!r}")
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode: {mode!r}")
    if state not in VALID_STATES:
        raise ValueError(f"invalid state: {state!r}")

    now = int(now_epoch if now_epoch is not None else time.time())
    return {
        "version": STATUS_VERSION,
        "job_id": job_id,
        "mode": mode,
        "series": _normalize_series(series),
        "state": state,
        "message": message,
        "created_at_epoch": now,
        "updated_at_epoch": now,
        "expires_at_epoch": now + ttl,
        "release_tag": "",
        "release_url": "",
        "assets": {},
        "run": {
            "workflow_run_id": 0,
            "workflow_run_url": "",
            "code_ref": "",
        },
        "publishing": {
            "status": "not_requested",
            "posts": [],
            "idempotency_key": "",
        },
    }


def _normalize_series(series: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(series, dict):
        return {
            "enabled": False,
            "series_id": "",
            "part": 0,
            "start_seconds": 0,
            "is_final": False,
        }
    return {
        "enabled": bool(series.get("enabled", False)),
        "series_id": str(series.get("series_id", "")),
        "part": int(series.get("part", 0)),
        "start_seconds": int(series.get("start_seconds", 0)),
        "is_final": bool(series.get("is_final", False)),
    }


# --------------------------------------------------------------------------- #
# I/O                                                                          #
# --------------------------------------------------------------------------- #

def status_path(job_id: str, *, root: os.PathLike[str] | str = "jobs") -> Path:
    if not is_valid_job_id(job_id):
        raise ValueError(f"invalid job_id: {job_id!r}")
    return Path(root) / job_id / "status.json"


def read_status(job_id: str, *, root: os.PathLike[str] | str = "jobs") -> dict[str, Any] | None:
    path = status_path(job_id, root=root)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_status(
    job_id: str,
    *,
    state: str | None = None,
    message: str | None = None,
    mode: str | None = None,
    release_tag: str | None = None,
    release_url: str | None = None,
    assets: dict[str, str] | None = None,
    run: dict[str, Any] | None = None,
    publishing: dict[str, Any] | None = None,
    series: dict[str, Any] | None = None,
    expires_at_epoch: int | None = None,
    ttl_seconds: int | None = None,
    now_epoch: int | None = None,
    root: os.PathLike[str] | str = "jobs",
) -> dict[str, Any]:
    """Merge the given fields into ``jobs/<job_id>/status.json``.

    Missing fields are preserved from the prior record. If no record exists
    yet, a fresh one is created (mode defaults to ``"manual"`` if not passed
    on first write — callers should always pass ``mode`` on the first write).
    Returns the new record.
    """
    if not is_valid_job_id(job_id):
        raise ValueError(f"invalid job_id: {job_id!r}")

    prior = read_status(job_id, root=root) or {}
    now = int(now_epoch if now_epoch is not None else time.time())

    if not prior:
        record = new_status(
            job_id=job_id,
            mode=(mode or "manual"),
            state=(state or "queued"),
            message=(message or ""),
            now_epoch=now,
            ttl_seconds=ttl_seconds,
            series=series,
        )
    else:
        record = dict(prior)
        record.setdefault("version", STATUS_VERSION)
        record.setdefault("job_id", job_id)
        record.setdefault("assets", {})
        record.setdefault("run", {"workflow_run_id": 0, "workflow_run_url": "", "code_ref": ""})
        record.setdefault("publishing", {"status": "not_requested", "posts": [], "idempotency_key": ""})
        record.setdefault("series", _normalize_series(None))
        record.setdefault("release_tag", "")
        record.setdefault("release_url", "")
        record.setdefault("message", "")

    if mode is not None:
        if mode not in VALID_MODES:
            raise ValueError(f"invalid mode: {mode!r}")
        record["mode"] = mode

    if state is not None:
        if state not in VALID_STATES:
            raise ValueError(f"invalid state: {state!r}")
        # Refuse to transition out of a terminal state except into another
        # terminal state (which is idempotent for cleanup). This mirrors the
        # single-source-of-truth rule from ARCHITECTURE.md §6.
        prior_state = record.get("state")
        if prior_state in TERMINAL_STATES and state != prior_state and state not in TERMINAL_STATES:
            # bug-13/15: a deliberate Restart Stage A/B tap RE-OPENS a terminal job.
            # The restart dispatches a fresh workflow run whose first status write
            # (stage_a_running / stage_b_running) must not be refused here —
            # previously the write crashed the restarted run at its very first
            # step ("cannot transition terminal state 'error' -> 'stage_a_running'").
            # Terminal -> same-terminal stays idempotent for cleanup; only
            # complete -> non-running states remains refused (a finished render
            # cannot silently slip back to a queued/awaiting state).
            running = {"stage_a_running", "stage_b_running"}
            if not (state in running or (prior_state == "error" and state == "queued")):
                raise ValueError(
                    f"cannot transition terminal state {prior_state!r} -> {state!r}; "
                    "terminal jobs are done (restart via a Stage A/B re-run)"
                )
        record["state"] = state

    if message is not None:
        record["message"] = str(message)

    if release_tag is not None:
        record["release_tag"] = str(release_tag)
    if release_url is not None:
        record["release_url"] = str(release_url)

    if assets:
        merged_assets = dict(record.get("assets") or {})
        for k, v in assets.items():
            merged_assets[str(k)] = str(v)
        record["assets"] = merged_assets

    if run:
        merged_run = dict(record.get("run") or {})
        for k in ("workflow_run_id", "workflow_run_url", "code_ref"):
            if k in run:
                merged_run[k] = run[k]
        record["run"] = merged_run

    if publishing:
        merged_pub = dict(record.get("publishing") or {"status": "not_requested", "posts": [], "idempotency_key": ""})
        if "status" in publishing:
            if publishing["status"] not in VALID_PUBLISHING_STATUSES:
                raise ValueError(f"invalid publishing.status: {publishing['status']!r}")
            merged_pub["status"] = publishing["status"]
        if "posts" in publishing:
            merged_pub["posts"] = list(publishing["posts"])
        if "idempotency_key" in publishing:
            merged_pub["idempotency_key"] = str(publishing["idempotency_key"])
        record["publishing"] = merged_pub

    if series is not None:
        record["series"] = _normalize_series(series)

    if expires_at_epoch is not None:
        record["expires_at_epoch"] = int(expires_at_epoch)
    elif "expires_at_epoch" not in record:
        # Same resolution order as new_status(): the configured env TTL wins;
        # DEFAULT_TTL_SECONDS is only the no-config fallback.
        record["expires_at_epoch"] = int(record.get("created_at_epoch", now)) + configured_ttl_seconds()

    record["updated_at_epoch"] = now

    path = status_path(job_id, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)
    return record


# --------------------------------------------------------------------------- #
# CLI (used by GitHub Actions steps)                                           #
# --------------------------------------------------------------------------- #

def _parse_kv_list(items: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write jobs/<job_id>/status.json")
    ap.add_argument("job_id")
    ap.add_argument("--state", required=True, choices=list(VALID_STATES))
    ap.add_argument("--mode", choices=list(VALID_MODES))
    ap.add_argument("--message", default="")
    ap.add_argument("--release-tag", default=None)
    ap.add_argument("--release-url", default=None)
    ap.add_argument("--asset", action="append", default=[], metavar="name=url")
    ap.add_argument("--workflow-run-id", type=int, default=None)
    ap.add_argument("--workflow-run-url", default=None)
    ap.add_argument("--code-ref", default=None)
    ap.add_argument("--series-json", default=None, metavar="JSON",
                    help="bug-57: sync the status series block from this JSON object "
                         "(e.g. the request's series block); any subset of "
                         "enabled/series_id/part/start_seconds/is_final")
    ap.add_argument("--ttl-seconds", type=int, default=None,
                    help="TTL for a NEWLY created job's expires_at_epoch. "
                         "stage-a.yml passes $CLIPFORGE_TTL_SECONDS (sourced from "
                         "the single repo Actions variable CLIPFORGE_TTL_SECONDS); "
                         "omit to resolve from the env var, then DEFAULT_TTL_SECONDS.")
    ap.add_argument("--out-dir", default="jobs")
    args = ap.parse_args(argv)

    series_updates: dict[str, Any] | None = None
    if args.series_json is not None:
        try:
            parsed = json.loads(args.series_json)
        except json.JSONDecodeError:
            raise SystemExit(f"--series-json is not valid JSON: {args.series_json!r}")
        if not isinstance(parsed, dict):
            raise SystemExit("--series-json must be a JSON object")
        series_updates = parsed

    assets = _parse_kv_list(args.asset)
    run_updates: dict[str, Any] = {}
    if args.workflow_run_id is not None:
        run_updates["workflow_run_id"] = args.workflow_run_id
    if args.workflow_run_url is not None:
        run_updates["workflow_run_url"] = args.workflow_run_url
    if args.code_ref is not None:
        run_updates["code_ref"] = args.code_ref

    record = write_status(
        args.job_id,
        state=args.state,
        message=args.message,
        mode=args.mode,
        release_tag=args.release_tag,
        release_url=args.release_url,
        assets=assets or None,
        run=run_updates or None,
        series=series_updates,
        ttl_seconds=args.ttl_seconds,
        root=args.out_dir,
    )
    print(f"wrote {status_path(args.job_id, root=args.out_dir)} state={record['state']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "STATUS_VERSION",
    "VALID_STATES",
    "TERMINAL_STATES",
    "VALID_MODES",
    "VALID_PUBLISHING_STATUSES",
    "DEFAULT_TTL_SECONDS",
    "TTL_ENV_VAR",
    "configured_ttl_seconds",
    "is_valid_job_id",
    "new_status",
    "status_path",
    "read_status",
    "write_status",
]
