#!/usr/bin/env python3
"""
Delete every ClipForge job artifact older than 12 hours.

Scope covered:
  1. GitHub Releases tagged 'clipforge-<job-id>' or with a marker in the
     release body -> delete Release + all assets + the underlying git tag.
  2. jobs/<job-id>/ folders on the default branch -> remove via a single
     commit at the end.
  3. Refs (branches) named 'clipforge-job/<job-id>' if any -> delete.

'Older than 12 hours' means the job's created_at_epoch (from status.json)
is more than 12 hours ago. If status.json is missing/unreadable, fall back
to the Release's created_at.

Env:
    GITHUB_TOKEN     required
    GITHUB_REPOSITORY  required (owner/repo)
    CLIPFORGE_TTL_SECONDS   optional override (default 43200)

Usage:
    python cleanup.py
"""
import json
import os
import sys
import time
from typing import Any

import requests


API = "https://api.github.com"
JOB_TAG_PREFIX = "clipforge-"
RELAY_TAG_PREFIX = "clipforge-relay-input-"
JOB_BRANCH_PREFIX = "clipforge-job/"
JOBS_DIR = "jobs"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _paged(url: str, headers: dict) -> list[dict]:
    out: list[dict] = []
    while url:
        r = requests.get(url, headers=headers, params={"per_page": 100})
        r.raise_for_status()
        out.extend(r.json())
        nxt = r.links.get("next", {}).get("url")
        url = nxt
    return out


def list_releases(owner_repo: str, headers: dict) -> list[dict]:
    return _paged(f"{API}/repos/{owner_repo}/releases", headers)


def delete_release(owner_repo: str, rel: dict, headers: dict) -> None:
    rid = rel["id"]
    tag = rel.get("tag_name", "")
    print(f"  deleting release id={rid} tag={tag}", flush=True)
    r = requests.delete(f"{API}/repos/{owner_repo}/releases/{rid}", headers=headers)
    if r.status_code not in (204, 404):
        print(f"    WARN release delete status={r.status_code} body={r.text[:200]}", flush=True)
    if tag:
        r = requests.delete(f"{API}/repos/{owner_repo}/git/refs/tags/{tag}", headers=headers)
        if r.status_code not in (204, 404, 422):
            print(f"    WARN tag delete status={r.status_code} body={r.text[:200]}", flush=True)


def list_branches(owner_repo: str, headers: dict) -> list[dict]:
    return _paged(f"{API}/repos/{owner_repo}/branches", headers)


def delete_branch(owner_repo: str, name: str, headers: dict) -> None:
    print(f"  deleting branch {name}", flush=True)
    r = requests.delete(f"{API}/repos/{owner_repo}/git/refs/heads/{name}", headers=headers)
    if r.status_code not in (204, 404, 422):
        print(f"    WARN branch delete status={r.status_code} body={r.text[:200]}", flush=True)


def read_job_created_at(job_id: str) -> int | None:
    path = os.path.join(JOBS_DIR, job_id, "status.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        v = data.get("created_at_epoch")
        return int(v) if v is not None else None
    except Exception:
        return None


def list_job_ids_from_disk() -> list[str]:
    if not os.path.isdir(JOBS_DIR):
        return []
    return [
        name for name in os.listdir(JOBS_DIR)
        if os.path.isdir(os.path.join(JOBS_DIR, name)) and not name.startswith(".")
    ]


def job_id_from_release(rel: dict) -> str | None:
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


def parse_iso8601(s: str) -> int:
    # e.g. '2024-01-01T00:00:00Z'
    import calendar, datetime
    try:
        dt = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
        return calendar.timegm(dt.timetuple())
    except Exception:
        return 0


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN")
    owner_repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not owner_repo:
        print("GITHUB_TOKEN and GITHUB_REPOSITORY are required", file=sys.stderr)
        sys.exit(2)

    ttl = int(os.environ.get("CLIPFORGE_TTL_SECONDS", str(12 * 3600)))
    now = int(time.time())
    cutoff = now - ttl
    print(f"Cleanup start: now={now} cutoff={cutoff} ttl={ttl}s", flush=True)

    headers = _headers(token)

    # 1) Releases
    releases = list_releases(owner_repo, headers)
    expired_job_ids: set[str] = set()
    print(f"Found {len(releases)} releases in repo", flush=True)
    for rel in releases:
        job_id = job_id_from_release(rel)
        if not job_id:
            continue

        created = read_job_created_at(job_id)
        if created is None:
            created = parse_iso8601(rel.get("created_at", "")) or now
        age = now - created

        if created <= cutoff:
            print(f"EXPIRED job={job_id} age={age}s tag={rel.get('tag_name')}", flush=True)
            delete_release(owner_repo, rel, headers)
            expired_job_ids.add(job_id)
        else:
            print(f"keep    job={job_id} age={age}s (< ttl {ttl}s)", flush=True)

    # 2) Also expire jobs that have a folder but no matching release (already
    #    partially cleaned, or Stage A failed before uploading a release).
    for jid in list_job_ids_from_disk():
        if jid in expired_job_ids:
            continue
        created = read_job_created_at(jid)
        if created is None:
            # No status.json -> treat as expired.
            print(f"EXPIRED job={jid} (no status.json)", flush=True)
            expired_job_ids.add(jid)
            continue
        if created <= cutoff:
            print(f"EXPIRED job={jid} folder age={now - created}s", flush=True)
            expired_job_ids.add(jid)

    # 3) Delete any per-job branches whose id is expired (or that don't
    #    correspond to a live job at all).
    live_ids = {jid for jid in list_job_ids_from_disk() if jid not in expired_job_ids}
    for br in list_branches(owner_repo, headers):
        name = br.get("name", "")
        if not name.startswith(JOB_BRANCH_PREFIX):
            continue
        bjid = name[len(JOB_BRANCH_PREFIX):]
        if bjid in expired_job_ids or bjid not in live_ids:
            delete_branch(owner_repo, name, headers)

    # 4) Remove folders from disk. The workflow will commit these deletions.
    removed_dirs: list[str] = []
    for jid in expired_job_ids:
        d = os.path.join(JOBS_DIR, jid)
        if os.path.isdir(d):
            import shutil
            shutil.rmtree(d, ignore_errors=True)
            removed_dirs.append(d)
            print(f"  removed folder {d}", flush=True)

    print(f"Done. expired={len(expired_job_ids)} folders_removed={len(removed_dirs)}", flush=True)


if __name__ == "__main__":
    main()
