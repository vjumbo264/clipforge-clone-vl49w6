#!/usr/bin/env python3
"""ClipForge TTL cleanup — port of _legacy/scripts/cleanup.py (ARCHITECTURE.md §12).

Deletes every ClipForge job artifact whose TTL has passed:

  1. GitHub Releases tagged ``clipforge-<job-id>`` (or relay input
     ``clipforge-relay-input-<job-id>``, or carrying a
     ``clipforge-job-id: <id>`` marker line in the body) -> delete the
     release, its assets, and the underlying git tag.
  2. ``jobs/<job-id>/`` folders on the default branch -> removed from disk;
     the calling workflow commits the deletions.
  3. Refs (branches) named ``clipforge-job/<job-id>`` -> deleted when their
     job is expired or no longer exists.

Expiry rule (§12, new §6.2 schema): a job expires when its
``expires_at_epoch`` (written by ``pipeline.status``) is in the past. The
``CLIPFORGE_TTL_SECONDS`` override replaces the computed expiry as
``created_at_epoch + ttl`` — matching the legacy "older than N seconds"
behavior. Legacy fallback when neither field is readable: the release's
``created_at``. A job folder with no readable status.json is treated as
expired (preserved legacy behavior).

Stdlib-only (urllib) — no third-party deps, so the cleanup job installs
nothing before running this module.

Env:
    GITHUB_TOKEN            required
    GITHUB_REPOSITORY       required (owner/repo)
    CLIPFORGE_TTL_SECONDS   optional override (default 43200 = 12h)

Usage:
    python -m pipeline.cleanup.expired
"""
from __future__ import annotations

import calendar
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API = "https://api.github.com"
JOB_TAG_PREFIX = "clipforge-"
RELAY_TAG_PREFIX = "clipforge-relay-input-"
JOB_BRANCH_PREFIX = "clipforge-job/"
JOBS_DIR = "jobs"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# Inactivity grace (seconds) for the series-protection rule below. A series
# whose parts are ALL terminal but NONE is is_final-marked stays protected
# (bug-66) only while its newest part's own expiry is less than this far in
# the past. 24h is far longer than any realistic publish/queue lag, so a
# genuinely active series is never reaped — but an abandoned one no longer
# pins its expired parts (and the operator's task list) forever.
DEFAULT_SERIES_GRACE_SECONDS = 24 * 3600


# --------------------------------------------------------------------------- #
# GitHub API transport (urllib; injectable for tests)                          #
# --------------------------------------------------------------------------- #


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _next_link(link_header: str) -> str:
    """Extract the rel="next" URL from an RFC 5988 Link header."""
    for part in (link_header or "").split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        url = segments[0].strip().strip("<>")
        for attr in segments[1:]:
            if attr.strip() == 'rel="next"':
                return url
    return ""


def _read_limited(stream) -> bytes:
    data = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise RuntimeError("GitHub API response exceeded the safe response-size limit.")
    return data


def _request(
    method: str,
    url: str,
    token: str,
    *,
    opener: Callable[..., Any] = urlopen,
) -> tuple[int, dict[str, str], Any]:
    """One GitHub API call -> (status, headers, parsed-json-or-None)."""
    req = Request(url, method=method, headers=_headers(token))
    try:
        with opener(req) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            raw = _read_limited(resp)
    except HTTPError as exc:
        status = exc.code
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        raw = exc.read(MAX_RESPONSE_BYTES + 1) if exc.fp is not None else b""
    parsed: Any = None
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
    return status, headers, parsed


def _paged(
    url: str,
    token: str,
    *,
    opener: Callable[..., Any] = urlopen,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}per_page=100"
    while url:
        status, headers, parsed = _request("GET", url, token, opener=opener)
        if status != 200 or not isinstance(parsed, list):
            raise RuntimeError(f"GitHub API list failed (HTTP {status}) for {url}")
        out.extend(parsed)
        url = _next_link(headers.get("link", ""))
    return out


def list_releases(owner_repo: str, token: str, *, opener: Callable[..., Any] = urlopen) -> list[dict[str, Any]]:
    return _paged(f"{API}/repos/{owner_repo}/releases", token, opener=opener)


def list_branches(owner_repo: str, token: str, *, opener: Callable[..., Any] = urlopen) -> list[dict[str, Any]]:
    return _paged(f"{API}/repos/{owner_repo}/branches", token, opener=opener)


def delete_release(
    owner_repo: str,
    rel: dict[str, Any],
    token: str,
    *,
    opener: Callable[..., Any] = urlopen,
) -> None:
    rid = rel["id"]
    tag = rel.get("tag_name", "")
    print(f"  deleting release id={rid} tag={tag}", flush=True)
    status, _h, body = _request("DELETE", f"{API}/repos/{owner_repo}/releases/{rid}", token, opener=opener)
    if status not in (204, 404):
        print(f"    WARN release delete status={status} body={json.dumps(body)[:200] if body else ''}", flush=True)
    if tag:
        status, _h, body = _request(
            "DELETE", f"{API}/repos/{owner_repo}/git/refs/tags/{quote(tag, safe='')}", token, opener=opener,
        )
        if status not in (204, 404, 422):
            print(f"    WARN tag delete status={status} body={json.dumps(body)[:200] if body else ''}", flush=True)


def delete_branch(
    owner_repo: str,
    name: str,
    token: str,
    *,
    opener: Callable[..., Any] = urlopen,
) -> None:
    print(f"  deleting branch {name}", flush=True)
    status, _h, body = _request(
        "DELETE", f"{API}/repos/{owner_repo}/git/refs/heads/{quote(name, safe='')}", token, opener=opener,
    )
    if status not in (204, 404, 422):
        print(f"    WARN branch delete status={status} body={json.dumps(body)[:200] if body else ''}", flush=True)


# --------------------------------------------------------------------------- #
# Job expiry logic                                                              #
# --------------------------------------------------------------------------- #


def read_job_timing(root: str | Path, job_id: str) -> dict[str, int | None]:
    """Read {created_at_epoch, expires_at_epoch} from a job's status.json.

    Missing/unreadable fields come back as None; a missing folder returns both
    None. Mirrors the legacy read_job_created_at fallback contract while
    surfacing the new §6.2 expires_at_epoch field.
    """
    path = Path(root) / JOBS_DIR / job_id / "status.json"
    timing: dict[str, Any] = {"created_at_epoch": None, "expires_at_epoch": None, "series_id": "", "series_final": False}
    if not path.exists():
        return timing
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return timing
    if not isinstance(data, dict):
        return timing
    for key in ("created_at_epoch", "expires_at_epoch"):
        value = data.get(key)
        try:
            timing[key] = int(value) if value is not None else None
        except (TypeError, ValueError):
            timing[key] = None
    # bug-49: carry the series identity so the sweep can protect a part whose
    # series still has unfinished parts.
    series = data.get("series")
    if isinstance(series, dict) and series.get("enabled") is True:
        timing["series_id"] = str(series.get("series_id") or "")
        timing["series_final"] = bool(series.get("is_final", False))
    return timing


def job_is_expired(
    timing: dict[str, int | None],
    *,
    now: int,
    ttl: int,
    release_created_at: int | None = None,
) -> tuple[bool, str]:
    """Decide expiry. Returns (expired, reason).

    Order: explicit expires_at_epoch wins (§6.2/§12); otherwise
    created_at_epoch + ttl (legacy "older than TTL" rule, where
    CLIPFORGE_TTL_SECONDS overrides the default 12h); otherwise the release's
    created_at fallback; otherwise (nothing readable) expired.
    """
    expires = timing.get("expires_at_epoch")
    created = timing.get("created_at_epoch")
    if expires is not None:
        return (expires <= now, f"expires_at={expires}")
    if created is not None:
        return (created + ttl <= now, f"created_at={created}+ttl={ttl}")
    if release_created_at:
        return (release_created_at + ttl <= now, f"release_created_at={release_created_at}+ttl={ttl}")
    return True, "no readable timing (treated as expired)"


def list_job_ids_from_disk(root: str | Path) -> list[str]:
    base = Path(root) / JOBS_DIR
    if not base.is_dir():
        return []
    return [
        entry.name for entry in base.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    ]


def job_id_from_release(rel: dict[str, Any]) -> str | None:
    tag = rel.get("tag_name") or ""
    if tag.startswith(RELAY_TAG_PREFIX):
        return tag[len(RELAY_TAG_PREFIX):]
    if tag.startswith(JOB_TAG_PREFIX):
        return tag[len(JOB_TAG_PREFIX):]
    body = rel.get("body") or ""
    # Fallback: parse marker line 'clipforge-job-id: <id>'
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("clipforge-job-id:"):
            return line.split(":", 1)[1].strip()
    return None


def parse_iso8601(value: str) -> int:
    """Parse GitHub's '2024-01-01T00:00:00Z' timestamps; 0 when unparsable."""
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return calendar.timegm(dt.timetuple())
    except (TypeError, ValueError):
        return 0


def _part_is_terminal(root: str | Path, job_id: str) -> bool:
    """bug-49: True when a part's status.json reads a terminal state."""
    try:
        data = json.loads((Path(root) / JOBS_DIR / job_id / "status.json").read_text(encoding="utf-8"))
        return str(data.get("state")) in ("complete", "error", "cancelled")
    except Exception:
        return False


def _series_last_activity_epoch(root: str | Path, series_id: str) -> int | None:
    """Newest timing signal across a series' on-disk parts.

    Parts are created over the series' lifetime, so the newest part's
    created/expires timestamps are the best available proxy for 'this series
    was still producing work recently' (status.json persists no updated_at
    field, so expires_at_epoch = creation + TTL is the freshest per-part
    signal).
    """
    last: int | None = None
    for jid in list_job_ids_from_disk(root):
        info = read_job_timing(root, jid)
        if info.get("series_id") != series_id:
            continue
        for key in ("created_at_epoch", "expires_at_epoch"):
            value = info.get(key)
            if value is not None:
                last = value if last is None else max(last, value)
    return last


def series_is_complete(root: str | Path, series_id: str, *, now: int | None = None,
                       grace: int = DEFAULT_SERIES_GRACE_SECONDS) -> bool:
    """bug-49 + bug-66: a series is complete only when EVERY known part is
    terminal AND at least one on-disk part is marked series_final/is_final.

    bug-66: read_job_timing() already surfaced the is_final flag, but nothing
    consumed it — so a mid-series job whose existing parts had all finished
    (later parts not yet rendered, e.g. the series-1787970477573 incident in
    cleanup commit 1d6ad63) looked "complete" and every part was reaped while
    publishing was still scheduled. Require a final-marked part: a series with
    zero parts marked is_final has not actually finished producing parts, so
    it is INCOMPLETE regardless of how many existing parts are terminal.

    A missing/unreadable sibling status counts as INCOMPLETE, so a protected
    part is never reaped while any sibling is still alive or unknown.

    Stuck-series escape hatch (operator report: series tasks from 3+ days ago
    never expired — every stuck series on disk had all-terminal parts but NO
    is_final part, so the bug-66 guard above protected them forever): when no
    part is marked is_final but EVERY part is terminal AND the newest part's
    own expiry is at least `grace` seconds (default 24h) in the past, the
    series is treated as complete anyway. A live series always has a part
    whose expiry is at most TTL old — far inside the grace window — so the
    bug-66 protection is fully intact for anything genuinely active."""
    if not series_id:
        return True
    saw_final = False
    for jid in list_job_ids_from_disk(root):
        info = read_job_timing(root, jid)
        if info.get("series_id") != series_id:
            continue
        if not _part_is_terminal(root, jid):
            return False
        if info.get("series_final"):
            saw_final = True
    if saw_final:
        return True
    # No is_final-marked part: reap only once the whole series has been
    # terminal AND quiet for the full grace window.
    if now is None:
        now = int(time.time())
    last_activity = _series_last_activity_epoch(root, series_id)
    return last_activity is not None and last_activity + grace <= now


# --------------------------------------------------------------------------- #
# Main                                                                          #
# --------------------------------------------------------------------------- #


def main(
    *,
    env: dict[str, str] | None = None,
    root: str | Path = ".",
    opener: Callable[..., Any] = urlopen,
    log: Callable[..., None] = print,
) -> int:
    env = dict(os.environ if env is None else env)
    token = env.get("GITHUB_TOKEN", "")
    owner_repo = env.get("GITHUB_REPOSITORY", "")
    if not token or not owner_repo:
        print("GITHUB_TOKEN and GITHUB_REPOSITORY are required", file=sys.stderr)
        return 2

    try:
        ttl = int(env.get("CLIPFORGE_TTL_SECONDS", str(12 * 3600)))
    except ValueError:
        print("CLIPFORGE_TTL_SECONDS must be an integer number of seconds", file=sys.stderr)
        return 2
    if ttl <= 0:
        print("CLIPFORGE_TTL_SECONDS must be positive", file=sys.stderr)
        return 2

    now = int(time.time())
    log(f"Cleanup start: now={now} ttl={ttl}s", flush=True)

    # 1) Releases.
    releases = list_releases(owner_repo, token, opener=opener)
    expired_job_ids: set[str] = set()
    log(f"Found {len(releases)} releases in repo", flush=True)
    for rel in releases:
        job_id = job_id_from_release(rel)
        if not job_id:
            continue
        timing = read_job_timing(root, job_id)
        release_created = parse_iso8601(rel.get("created_at", "")) or None
        expired, reason = job_is_expired(timing, now=now, ttl=ttl, release_created_at=release_created)
        # bug-49: never reap a series part while its series is incomplete.
        if expired and timing.get("series_id") and not series_is_complete(root, timing["series_id"]):
            log(f"PROTECT job={job_id} — series {timing['series_id']} incomplete ({reason})", flush=True)
            expired = False
        if expired:
            log(f"EXPIRED job={job_id} ({reason}) tag={rel.get('tag_name')}", flush=True)
            delete_release(owner_repo, rel, token, opener=opener)
            expired_job_ids.add(job_id)
        else:
            log(f"keep    job={job_id} ({reason})", flush=True)

    # 2) Jobs with a folder but no matching release (already partially
    #    cleaned, or Stage A failed before uploading a release).
    for jid in list_job_ids_from_disk(root):
        if jid in expired_job_ids:
            continue
        timing = read_job_timing(root, jid)
        expired, reason = job_is_expired(timing, now=now, ttl=ttl)
        # bug-49: never reap a series part while its series is incomplete.
        if expired and timing.get("series_id") and not series_is_complete(root, timing["series_id"]):
            log(f"PROTECT job={jid} folder — series {timing['series_id']} incomplete ({reason})", flush=True)
            expired = False
        if expired:
            log(f"EXPIRED job={jid} folder ({reason})", flush=True)
            expired_job_ids.add(jid)

    # 3) Delete per-job branches whose id is expired (or that don't
    #    correspond to a live job at all).
    live_ids = {jid for jid in list_job_ids_from_disk(root) if jid not in expired_job_ids}
    for branch in list_branches(owner_repo, token, opener=opener):
        name = branch.get("name", "")
        if not name.startswith(JOB_BRANCH_PREFIX):
            continue
        branch_job = name[len(JOB_BRANCH_PREFIX):]
        if branch_job in expired_job_ids or branch_job not in live_ids:
            delete_branch(owner_repo, name, token, opener=opener)

    # 4) Remove folders from disk. The calling workflow commits the deletions.
    removed_dirs: list[str] = []
    for jid in sorted(expired_job_ids):
        folder = Path(root) / JOBS_DIR / jid
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
            removed_dirs.append(str(folder))
            log(f"  removed folder {folder}", flush=True)

    log(f"Done. expired={len(expired_job_ids)} folders_removed={len(removed_dirs)}", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except (HTTPError, URLError, RuntimeError) as exc:
        print(f"cleanup failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
