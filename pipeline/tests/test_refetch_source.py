"""Offline unit tests for the bug-69 source re-fetch module.

No network and no aria2/swarm contact — every downloader is monkeypatched so
these tests exercise ONLY the re-fetch orchestration: source-kind dispatch,
the exact torrent_file_index reuse (with the interactive parking flow never
re-triggered), the series Part 1 source-manifest resolution, and the
clearly-labelled re-fetch failure message.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.stage_a import ingest, refetch_source  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

def _write_request(jobs_root: Path, job_id: str, source: dict) -> None:
    d = jobs_root / job_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "stage-a-request.json").write_text(json.dumps({
        "version": 2, "job_id": job_id,
        "source": source,
        "options": {"whisper_model": "base", "language": "auto",
                    "target_duration_seconds": 120, "focus": "", "enable_vision_assist": True},
        "mode": "manual",
        "series": {"enabled": False, "series_id": "", "source_job_id": "",
                   "part": 0, "start_seconds": 0, "context": ""},
        "music": {"ref": "", "source": "none"},
        "saved_at_epoch": int(time.time()),
    }), encoding="utf-8")


def _patch_container_ext(monkeypatch):
    # detect_container_ext sniffs magic bytes; our stub payloads are plain, so
    # force a deterministic container for the record/original.<ext> naming.
    monkeypatch.setattr(ingest, "detect_container_ext", lambda _p: "mp4")


# --------------------------------------------------------------------------- #
# Direct URL (non-torrent happy path)                                          #
# --------------------------------------------------------------------------- #

def test_refetch_url_uses_download_direct(tmp_path, monkeypatch):
    _patch_container_ext(monkeypatch)
    jobs_root = tmp_path / "jobs"
    _write_request(jobs_root, "job-url", {"kind": "url", "value": "https://example.com/v.mp4"})
    calls = {}

    def fake_download_direct(url, out):
        calls["url"] = url
        Path(out).write_bytes(b"\x00" * 64)
        return 64

    monkeypatch.setattr(ingest, "download_direct", fake_download_direct)
    rec = refetch_source.refetch_source("job-url", str(tmp_path / "work"), root=str(jobs_root))

    assert calls["url"] == "https://example.com/v.mp4"
    assert rec["source_kind"] == "url"
    assert rec["refetch"] is True
    assert "2 GiB" in rec["refetch_reason"]
    assert Path(rec["original_path"]).name == "original.mp4"
    assert Path(rec["original_path"]).is_file()


def test_refetch_url_telegram_post_routes_to_channel_path(tmp_path, monkeypatch):
    _patch_container_ext(monkeypatch)
    jobs_root = tmp_path / "jobs"
    _write_request(jobs_root, "job-tgurl",
                   {"kind": "url", "value": "https://t.me/somechannel/123"})
    calls = {}

    def fake_channel(value, out):
        calls["value"] = value
        Path(out).write_bytes(b"\x00" * 8)
        return value

    def boom_direct(url, out):  # a t.me post URL must NOT go through plain direct
        raise AssertionError("telegram post URL must not use download_direct")

    monkeypatch.setattr(ingest, "_download_telegram_channel", fake_channel)
    monkeypatch.setattr(ingest, "download_direct", boom_direct)
    rec = refetch_source.refetch_source("job-tgurl", str(tmp_path / "work"), root=str(jobs_root))
    assert calls["value"] == "https://t.me/somechannel/123"
    assert rec["source_kind"] == "url"


# --------------------------------------------------------------------------- #
# Drive                                                                        #
# --------------------------------------------------------------------------- #

def test_refetch_drive_uses_saved_file_id(tmp_path, monkeypatch):
    _patch_container_ext(monkeypatch)
    jobs_root = tmp_path / "jobs"
    _write_request(jobs_root, "job-drive",
                   {"kind": "drive", "value": "https://drive.google.com/file/d/AbCdEfGh123/view"})
    calls = {}

    def fake_drive(file_id, out):
        calls["file_id"] = file_id
        Path(out).write_bytes(b"\x00" * 8)
        return 8

    monkeypatch.setattr(ingest, "download_drive", fake_drive)
    rec = refetch_source.refetch_source("job-drive", str(tmp_path / "work"), root=str(jobs_root))
    assert calls["file_id"] == "AbCdEfGh123"
    assert rec["source_kind"] == "drive"


# --------------------------------------------------------------------------- #
# Torrent (magnet) — exact index reuse, no interactive parking                 #
# --------------------------------------------------------------------------- #

def _fake_torrent_common(monkeypatch, captured, candidates=None):
    if candidates is None:
        candidates = [{"index": 3, "path": "a.mp4"}]
    monkeypatch.setattr(ingest, "inspect_magnet",
                        lambda v: {"infohash_v1": "AB" * 20, "display_name": "t"})
    monkeypatch.setattr(ingest, "_resolve_magnet_metadata",
                        lambda v, info, mdir, wdir: Path(str(mdir)) / "x.torrent")
    inspect_calls = []

    def fake_inspect_torrent(p):
        inspect_calls.append(str(p))
        return {"video_candidates": list(candidates)}

    monkeypatch.setattr(ingest, "inspect_torrent", fake_inspect_torrent)
    captured["inspect_calls"] = inspect_calls
    monkeypatch.setattr(ingest, "select_torrent_video",
                        lambda md, idx: {"index": idx, "path": "a.mp4"})

    def fake_payload(torrent_path, out_dir, selected_index):
        captured["selected_index"] = selected_index
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "a.mp4").write_bytes(b"\x00" * 32)

    monkeypatch.setattr(ingest, "_download_torrent_payload", fake_payload)
    monkeypatch.setattr(ingest, "select_video",
                        lambda root, rel: Path(root) / rel)
    # Hard guard: the interactive parking flow must never run during re-fetch.
    def no_parking(*a, **k):
        raise AssertionError("interactive torrent-selection parking re-triggered")
    monkeypatch.setattr(ingest, "write_torrent_selection", no_parking)


def test_refetch_magnet_reuses_exact_saved_index_no_parking(tmp_path, monkeypatch):
    _patch_container_ext(monkeypatch)
    captured = {}
    _fake_torrent_common(monkeypatch, captured)
    jobs_root = tmp_path / "jobs"
    _write_request(jobs_root, "job-magnet",
                   {"kind": "magnet", "value": "magnet:?xt=urn:btih:" + "ab" * 20,
                    "torrent_file_index": "4"})
    rec = refetch_source.refetch_source("job-magnet", str(tmp_path / "work"), root=str(jobs_root))
    assert captured["selected_index"] == 4          # the EXACT saved index
    assert rec["source_kind"] == "magnet"
    assert Path(rec["original_path"]).is_file()


def test_refetch_magnet_without_saved_index_fails_clearly(tmp_path, monkeypatch):
    """GENUINE ambiguity: empty torrent_file_index AND 2+ video candidates.
    A fully automated re-fetch cannot guess which file among several, so the
    fatal error must still fire — with the actual candidate count stated."""
    _patch_container_ext(monkeypatch)
    captured = {}
    _fake_torrent_common(monkeypatch, captured,
                         candidates=[{"index": 1, "path": "a.mp4"},
                                     {"index": 2, "path": "b.mp4"}])
    jobs_root = tmp_path / "jobs"
    _write_request(jobs_root, "job-noidx",
                   {"kind": "magnet", "value": "magnet:?xt=urn:btih:" + "ab" * 20})
    with pytest.raises(ingest.IngestError) as exc:
        refetch_source.refetch_source("job-noidx", str(tmp_path / "work"), root=str(jobs_root))
    msg = str(exc.value)
    assert "re-fetch" in msg and "torrent_file_index" in msg
    assert "2 video candidates" in msg  # the ACTUAL candidate count
    assert "among several" in msg       # framed as genuine multi-file ambiguity
    assert "selected_index" not in captured  # never reached the swarm


# --------------------------------------------------------------------------- #
# Empty torrent_file_index fallbacks (single-file ingest fix)                  #
# --------------------------------------------------------------------------- #

def test_refetch_magnet_empty_index_single_candidate_auto_selects(tmp_path, monkeypatch):
    """A single-file torrent legitimately has NO saved torrent_file_index: the
    original ingest auto-selected the sole video without ever showing a picker,
    so nothing was persisted. The re-fetch must mirror that fallback (the exact
    operator-reported failure on job manual-1788116412985)."""
    _patch_container_ext(monkeypatch)
    captured = {}
    _fake_torrent_common(monkeypatch, captured)  # exactly ONE candidate, index 3
    jobs_root = tmp_path / "jobs"
    _write_request(jobs_root, "job-single",
                   {"kind": "magnet", "value": "magnet:?xt=urn:btih:" + "ab" * 20})
    rec = refetch_source.refetch_source("job-single", str(tmp_path / "work"), root=str(jobs_root))
    assert captured["selected_index"] == 3   # the sole candidate, auto-selected
    assert rec["source_kind"] == "magnet"
    assert Path(rec["original_path"]).is_file()
    # The candidate-count check must reuse the fetch's own inspection — never
    # inspect the torrent metadata twice.
    assert len(captured["inspect_calls"]) == 1


def test_refetch_torrent_file_empty_index_single_candidate_auto_selects(tmp_path, monkeypatch):
    """Same fallback through the torrent_file branch (job-local manifest)."""
    _patch_container_ext(monkeypatch)
    captured = {}
    monkeypatch.setattr(ingest, "inspect_torrent",
                        lambda p: {"video_candidates": [{"index": 3, "path": "a.mp4"}]})
    monkeypatch.setattr(ingest, "select_torrent_video",
                        lambda md, idx: {"index": idx, "path": "a.mp4"})

    def fake_payload(torrent_path, out_dir, selected_index):
        captured["selected_index"] = selected_index
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "a.mp4").write_bytes(b"\x00" * 32)

    monkeypatch.setattr(ingest, "_download_torrent_payload", fake_payload)
    monkeypatch.setattr(ingest, "select_video", lambda root, rel: Path(root) / rel)
    monkeypatch.setattr(ingest, "write_torrent_selection",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("parking re-triggered")))

    jobs_root = tmp_path / "jobs"
    job_dir = jobs_root / "job-tfile"
    job_dir.mkdir(parents=True)
    (job_dir / "source.torrent").write_bytes(b"d8:announce0:e")
    _write_request(jobs_root, "job-tfile",
                   {"kind": "torrent_file",
                    "value": f"path:{job_dir}/source.torrent"})  # no index saved
    rec = refetch_source.refetch_source("job-tfile", str(tmp_path / "work"), root=str(jobs_root))
    assert captured["selected_index"] == 3
    assert rec["source_kind"] == "torrent_file"


def test_refetch_magnet_empty_index_zero_candidates_distinct_error(tmp_path, monkeypatch):
    """A genuinely empty/broken torrent (ZERO video candidates) is neither the
    auto-select case nor the multi-file ambiguity case — it gets its own
    distinct error."""
    _patch_container_ext(monkeypatch)
    captured = {}
    _fake_torrent_common(monkeypatch, captured, candidates=[])
    jobs_root = tmp_path / "jobs"
    _write_request(jobs_root, "job-empty",
                   {"kind": "magnet", "value": "magnet:?xt=urn:btih:" + "ab" * 20})
    with pytest.raises(ingest.IngestError) as exc:
        refetch_source.refetch_source("job-empty", str(tmp_path / "work"), root=str(jobs_root))
    msg = str(exc.value)
    assert "re-fetch" in msg
    assert "no supported video candidates" in msg
    assert "among several" not in msg  # must NOT be misclassified as ambiguity
    assert "selected_index" not in captured


def test_refetch_magnet_saved_index_multi_candidate_unchanged(tmp_path, monkeypatch):
    """Regression guard: a multi-file torrent WITH a persisted selection must
    behave exactly as before — the saved index is used verbatim, no fallback
    logic interferes."""
    _patch_container_ext(monkeypatch)
    captured = {}
    _fake_torrent_common(monkeypatch, captured,
                         candidates=[{"index": 1, "path": "a.mp4"},
                                     {"index": 2, "path": "b.mp4"}])
    jobs_root = tmp_path / "jobs"
    _write_request(jobs_root, "job-picked",
                   {"kind": "magnet", "value": "magnet:?xt=urn:btih:" + "ab" * 20,
                    "torrent_file_index": "2"})
    rec = refetch_source.refetch_source("job-picked", str(tmp_path / "work"), root=str(jobs_root))
    assert captured["selected_index"] == 2  # the EXACT saved index
    assert rec["source_kind"] == "magnet"
    assert Path(rec["original_path"]).is_file()


# --------------------------------------------------------------------------- #
# Torrent (torrent_file) — series Part 1 manifest resolution                   #
# --------------------------------------------------------------------------- #

def test_refetch_torrent_file_resolves_part1_manifest(tmp_path, monkeypatch):
    """A series continuation's saved source points at Part 1's job folder; the
    re-fetch must resolve that exact manifest, not re-derive it from the
    current (part-2) job id."""
    _patch_container_ext(monkeypatch)
    captured = {}
    monkeypatch.setattr(ingest, "inspect_torrent",
                        lambda p: {"video_candidates": [{"index": 3, "path": "a.mp4"}]})
    monkeypatch.setattr(ingest, "select_torrent_video",
                        lambda md, idx: {"index": idx, "path": "a.mp4"})

    def fake_payload(torrent_path, out_dir, selected_index):
        captured["torrent_path"] = str(torrent_path)
        captured["selected_index"] = selected_index
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "a.mp4").write_bytes(b"\x00" * 32)

    monkeypatch.setattr(ingest, "_download_torrent_payload", fake_payload)
    monkeypatch.setattr(ingest, "select_video", lambda root, rel: Path(root) / rel)
    monkeypatch.setattr(ingest, "write_torrent_selection",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("parking re-triggered")))

    jobs_root = tmp_path / "jobs"
    # Part 1 owns the .torrent manifest on disk.
    part1 = jobs_root / "manual-part1"
    part1.mkdir(parents=True)
    (part1 / "source.torrent").write_bytes(b"d8:announce0:e")
    # The continuation's request persists the ORIGINAL source (Part 1's path+index).
    _write_request(jobs_root, "series-x-p2",
                   {"kind": "torrent_file",
                    "value": f"path:{part1}/source.torrent",
                    "torrent_file_index": "3"})

    rec = refetch_source.refetch_source(
        "series-x-p2", str(tmp_path / "work"),
        root=str(jobs_root), source_job="manual-part1")
    assert captured["torrent_path"].endswith("manual-part1/source.torrent")
    assert captured["selected_index"] == 3
    assert rec["source_kind"] == "torrent_file"
    assert rec["refetched_from_job"] == "series-x-p2"  # its own request held the source


# --------------------------------------------------------------------------- #
# telegram_relay — uses the saved relay block                                  #
# --------------------------------------------------------------------------- #

def test_refetch_telegram_relay_uses_relay_block(tmp_path, monkeypatch):
    _patch_container_ext(monkeypatch)
    jobs_root = tmp_path / "jobs"
    relay = {"release_tag": "clipforge-relay-input-job-relay",
             "expected_size_bytes": 8, "sha256": "ab" * 32}
    _write_request(jobs_root, "job-relay",
                   {"kind": "telegram_relay", "value": "relay:private", "relay": relay})
    calls = {}

    def fake_relay(job_id, source, out):
        calls["job_id"] = job_id
        calls["relay"] = source.get("relay")
        Path(out).write_bytes(b"\x00" * 8)

    monkeypatch.setattr(ingest, "_download_relay_asset", fake_relay)
    rec = refetch_source.refetch_source("job-relay", str(tmp_path / "work"), root=str(jobs_root))
    assert calls["relay"] == relay
    assert rec["source_kind"] == "telegram_relay"


# --------------------------------------------------------------------------- #
# Failure labelling + missing request                                          #
# --------------------------------------------------------------------------- #

def test_refetch_failure_is_labelled_as_refetch(tmp_path, monkeypatch):
    _patch_container_ext(monkeypatch)
    jobs_root = tmp_path / "jobs"
    _write_request(jobs_root, "job-fail", {"kind": "url", "value": "https://example.com/v.mp4"})

    def boom(url, out):
        raise ingest.IngestError("direct download failed: HTTP 404")

    monkeypatch.setattr(ingest, "download_direct", boom)
    with pytest.raises(ingest.IngestError) as exc:
        refetch_source.refetch_source("job-fail", str(tmp_path / "work"), root=str(jobs_root))
    # IngestError propagates (already a precise message); the module only wraps
    # non-IngestError surprises. Either way the message must not be swallowed.
    assert "404" in str(exc.value)


def test_refetch_wraps_unexpected_error_as_refetch(tmp_path, monkeypatch):
    _patch_container_ext(monkeypatch)
    jobs_root = tmp_path / "jobs"
    _write_request(jobs_root, "job-weird", {"kind": "url", "value": "https://example.com/v.mp4"})

    def boom(url, out):
        raise RuntimeError("socket exploded")

    monkeypatch.setattr(ingest, "download_direct", boom)
    with pytest.raises(ingest.IngestError) as exc:
        refetch_source.refetch_source("job-weird", str(tmp_path / "work"), root=str(jobs_root))
    msg = str(exc.value)
    assert "re-fetch" in msg and "2 GiB" in msg and "socket exploded" in msg


def test_refetch_missing_request_fails_clearly(tmp_path):
    jobs_root = tmp_path / "jobs"
    (jobs_root).mkdir(parents=True)
    with pytest.raises(ingest.IngestError) as exc:
        refetch_source.refetch_source("ghost", str(tmp_path / "work"), root=str(jobs_root))
    assert "stage-a-request.json" in str(exc.value)


def test_refetch_unsupported_kind_fails(tmp_path):
    jobs_root = tmp_path / "jobs"
    _write_request(jobs_root, "job-bad", {"kind": "carrier_pigeon", "value": "x"})
    with pytest.raises(ingest.IngestError) as exc:
        refetch_source.refetch_source("job-bad", str(tmp_path / "work"), root=str(jobs_root))
    assert "unsupported source kind" in str(exc.value)
