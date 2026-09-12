"""ClipForge — deterministic source re-fetch (bug-69).

When Stage A's release had to omit ``source_input.bin`` because the source
exceeded GitHub's 2 GiB per-asset limit, nothing downstream can download the
source from the release. This module re-fetches the source DIRECTLY from the
original source reference already durably committed in
``jobs/<job_id>/stage-a-request.json`` (ARCHITECTURE.md §7.1), reusing
``pipeline/stage_a/ingest.py``'s existing resolution logic — no new download
code, and no re-prompting of the operator:

  * ``url``               -> :func:`ingest.download_direct` (or the §9.1
                             Telegram public-post path for t.me post URLs).
  * ``drive``             -> :func:`ingest.download_drive` on the saved file id.
  * ``telegram_channel``  -> :func:`ingest._download_telegram_channel` on the
                             saved canonical post URL.
  * ``telegram_relay``    -> :func:`ingest._download_relay_asset` on the saved
                             ``source.relay`` block (tag + size + sha256).
  * ``magnet``            -> metadata re-resolution, then
                             :func:`ingest._download_torrent_payload` with the
                             EXACT saved ``torrent_file_index``.
  * ``torrent_file``      -> :func:`ingest._download_torrent_payload` on the
                             job-local ``source.torrent`` manifest, again with
                             the EXACT saved ``torrent_file_index``.

For torrent kinds the previously-made selection is reused silently — the
interactive multi-file parking flow (``awaiting_torrent_selection``) is NEVER
re-triggered: the file was already chosen once and that choice is durably
saved in ``torrent_file_index``.

Series continuations: a later part shares Part 1's underlying source, and its
``stage-a-request.json`` already persists that ORIGINAL source object (the
``value`` path for ``torrent_file`` points at Part 1's ``jobs/<part1>/
source.torrent``). This module therefore resolves the request via the
``--source-job`` (Part 1's job id) when given, falling back to the job
itself — so a series continuation re-fetches from the ORIGINAL saved source
reference even when its own release (or Part 1's release) never contained
``source_input.bin``.

Failure convention: any re-fetch failure raises :class:`ingest.IngestError`
with a message that plainly states (a) this is a RE-FETCH (the release
intentionally omitted the source), (b) why it failed when known, so the
workflow's status/error handling surfaces it via the normal
``jobs/<id>/status.json`` + bot messaging path — not confused with a
first-time ingest failure.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import ingest
from .ingest import IngestError


def _fail(reason: str) -> IngestError:
    return IngestError(
        "Stage B could not re-obtain the source video. This is a re-fetch: the "
        "Stage A release intentionally omitted source_input.bin because it "
        "exceeded GitHub's 2 GiB per-asset limit, so the source is being "
        "re-fetched from the original source reference saved in "
        "stage-a-request.json. Reason: " + reason
    )


def _resolve_request_path(job_id: str, source_job: str, root: Path) -> tuple[Path, str]:
    """Pick the request file to re-fetch from.

    Prefer the explicit ``source_job`` (a series Part 1), else fall back to
    the job's own request. Returns (request_path, owner_job_id).
    """
    candidates: list[tuple[str, Path]] = []
    if source_job:
        candidates.append((source_job, root / source_job / "stage-a-request.json"))
    candidates.append((job_id, root / job_id / "stage-a-request.json"))
    for owner, path in candidates:
        if path.is_file():
            return path, owner
    raise _fail(
        "no stage-a-request.json found for the job"
        + (f" or its series source job '{source_job}'" if source_job else "")
        + " — cannot re-fetch without the saved source reference."
    )


def _resolve_torrent_manifest(value: str, owner_job: str, root: Path) -> Path:
    """Locate the job-local ``source.torrent`` for a ``torrent_file`` source.

    ``ingest()`` enforces that the manifest lives at ``jobs/<job>/source.torrent``
    for the job being ingested. For a re-fetch the manifest may belong to a
    DIFFERENT (earlier) job — a series Part 1 — because parts share one source
    and the persisted ``value`` already points at the owning job's folder. We
    honour the persisted path as-is instead of re-deriving it from the current
    job id.
    """
    raw = value[len("path:"):] if value.startswith("path:") else value
    p = Path(raw)
    if not p.is_file():
        raise _fail(
            f"the saved torrent manifest '{raw}' is not present in the checkout "
            f"(expected under jobs/ for job '{owner_job}')."
        )
    return p


def refetch_source(
    job_id: str,
    work_dir: str,
    *,
    root: os.PathLike[str] | str = "jobs",
    source_job: str = "",
) -> dict[str, Any]:
    """Re-fetch the job's source into ``<work_dir>/original.<ext>``.

    Returns an ingest-style record. Raises :class:`IngestError` on any failure.
    Never triggers the interactive torrent-selection parking flow.
    """
    root_path = Path(root)
    req_path, owner_job = _resolve_request_path(job_id, source_job, root_path)
    try:
        request = json.loads(req_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _fail(f"could not read {req_path}: {exc}") from exc

    source = request.get("source") or {}
    kind = source.get("kind")
    value = source.get("value")
    if not kind or value is None:
        raise _fail(f"{req_path} has no usable source.kind/source.value.")

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    tmp_source = work / "source_input.bin"

    try:
        if kind == "url":
            raw = str(value)
            if "%" in raw and "http" in raw:
                import urllib.parse
                raw = urllib.parse.unquote(raw)
            if ingest.telegram_public_post_url(raw):
                ingest._download_telegram_channel(raw, str(tmp_source))
            else:
                disabled = ingest.disabled_social_host(raw)
                if disabled:
                    raise _fail(f"{disabled} social links are disabled sources.")
                ingest.download_direct(raw, str(tmp_source))

        elif kind == "drive":
            ingest.download_drive(ingest.extract_file_id(str(value)), str(tmp_source))

        elif kind == "telegram_channel":
            ingest._download_telegram_channel(str(value), str(tmp_source))

        elif kind == "telegram_relay":
            ingest._download_relay_asset(owner_job, source, str(tmp_source))

        elif kind in ("magnet", "torrent_file"):
            selected_raw = source.get("torrent_file_index", "")
            selected_index: int | None = None
            if selected_raw not in ("", None):
                try:
                    selected_index = int(selected_raw)
                except (TypeError, ValueError):
                    raise _fail(f"invalid saved torrent_file_index '{selected_raw}'.")

            if kind == "magnet":
                info = ingest.inspect_magnet(str(value))
                metadata_dir = work / "magnet-metadata"
                metadata_dir.mkdir(parents=True, exist_ok=True)
                torrent_path = ingest._resolve_magnet_metadata(
                    str(value), info, metadata_dir, str(work))
            else:
                torrent_path = _resolve_torrent_manifest(str(value), owner_job, root_path)

            # Single inspection, reused both for the candidate-count check
            # below and for the actual fetch — never inspect twice.
            metadata = ingest.inspect_torrent(torrent_path)

            if selected_index is None:
                # Mirror ingest.py's fallback: an empty torrent_file_index is
                # EXPECTED when the torrent had exactly one video file — the
                # original ingest auto-selected it without showing a picker,
                # so there was nothing to persist. Only GENUINE multi-file
                # ambiguity is fatal for an automated re-fetch.
                candidates = metadata.get("video_candidates", [])
                if not candidates:
                    raise _fail(
                        "the torrent contains no supported video candidates — "
                        "there is nothing to re-fetch."
                    )
                if len(candidates) == 1:
                    selected_index = candidates[0]["index"]
                else:
                    raise _fail(
                        f"the saved torrent source has no torrent_file_index and "
                        f"the torrent has {len(candidates)} video candidates — an "
                        f"automated re-fetch cannot pick one file among several "
                        f"without re-prompting. Re-ingest the source in Stage A "
                        f"and select a file."
                    )

            chosen = ingest.select_torrent_video(metadata, selected_index)
            torrent_dir = work / "torrent"
            ingest._download_torrent_payload(torrent_path, torrent_dir, chosen["index"])
            downloaded = ingest.select_video(torrent_dir, chosen["path"])
            shutil.copyfile(str(downloaded), str(tmp_source))

        else:
            raise _fail(f"unsupported source kind '{kind}' for re-fetch.")
    except IngestError:
        raise
    except Exception as exc:  # surface anything unexpected as a diagnosable re-fetch failure
        raise _fail(f"{type(exc).__name__}: {exc}") from exc

    if not tmp_source.is_file() or tmp_source.stat().st_size == 0:
        raise _fail("the re-fetch produced no usable file.")

    ext = ingest.detect_container_ext(str(tmp_source))
    original_path = work / f"original.{ext}"
    shutil.copyfile(str(tmp_source), str(original_path))
    size_bytes = os.path.getsize(original_path)

    record = {
        "version": 1,
        "job_id": job_id,
        "source_kind": kind,
        "refetch": True,
        "refetch_reason": "source_input.bin omitted from release (2 GiB per-asset limit)",
        "refetched_from_job": owner_job,
        "original_path": str(original_path),
        "original_asset_name": f"original.{ext}",
        "size_bytes": size_bytes,
        "container": ext,
        "ingested_at_epoch": int(time.time()),
    }
    print(f"Re-fetched {kind} source -> {original_path} ({size_bytes} bytes)", flush=True)
    return record


def main() -> None:
    ap = argparse.ArgumentParser(description="ClipForge source re-fetch (bug-69)")
    ap.add_argument("job_id")
    ap.add_argument("work_dir")
    ap.add_argument("--jobs-root", default="jobs")
    ap.add_argument("--source-job", default="",
                    help="Series Part 1 job id whose original source reference to use.")
    args = ap.parse_args()
    try:
        record = refetch_source(
            args.job_id, args.work_dir,
            root=args.jobs_root, source_job=args.source_job,
        )
    except IngestError as exc:
        print(f"source re-fetch error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()


__all__ = ["refetch_source"]
