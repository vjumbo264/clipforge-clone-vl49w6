"""Offline unit tests for the Stage A ingest + analyze modules.

No network, no ffmpeg, no faster-whisper — everything here exercises the pure
validators, parsers, scorers, and bundle/manifest assembly with temp files.
"""
from __future__ import annotations

import json
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.stage_a import ingest, scenes, bundle  # noqa: E402


# --------------------------------------------------------------------------- #
# Host / URL classification                                                     #
# --------------------------------------------------------------------------- #

def test_telegram_public_post_url_accepts_channel_post():
    assert ingest.telegram_public_post_url("https://t.me/somechannel/123") == "https://t.me/somechannel/123"
    assert ingest.telegram_public_post_url("https://t.me/s/somechannel/7") == "https://t.me/somechannel/7"


def test_telegram_public_post_url_rejects_non_posts():
    assert ingest.telegram_public_post_url("https://t.me/somechannel") is None
    assert ingest.telegram_public_post_url("https://t.me/+abcdef") is None
    assert ingest.telegram_public_post_url("https://t.me/c/123/456") is None
    assert ingest.telegram_public_post_url("https://example.com/somechannel/1") is None


def test_disabled_social_hosts():
    assert ingest.disabled_social_host("https://youtu.be/abc") == "youtu.be"
    assert ingest.disabled_social_host("https://www.tiktok.com/@x/video/1") == "tiktok.com"
    assert ingest.disabled_social_host("https://cdn.example.com/v.mp4") is None


def test_extract_file_id():
    assert ingest.extract_file_id("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrSt/view") == "1AbCdEfGhIjKlMnOpQrSt"
    assert ingest.extract_file_id("https://drive.google.com/open?id=1AbCdEfGhIjKlMnOpQrSt") == "1AbCdEfGhIjKlMnOpQrSt"
    assert ingest.extract_file_id("1AbCdEfGhIjKlMnOpQrSt12") == "1AbCdEfGhIjKlMnOpQrSt12"
    with pytest.raises(ingest.IngestError):
        ingest.extract_file_id("https://example.com/no-id-here")


# --------------------------------------------------------------------------- #
# Magnet validation                                                             #
# --------------------------------------------------------------------------- #

def test_magnet_hex_and_base32_infohash():
    hex40 = "0123456789abcdef0123456789abcdef01234567"
    info = ingest.inspect_magnet(f"magnet:?xt=urn:btih:{hex40}&dn=test")
    assert info["infohash_v1"] == hex40.upper()
    assert info["display_name"] == "test"
    # base32 form of the same 20 bytes decodes to the same hex
    import base64
    b32 = base64.b32encode(bytes.fromhex(hex40)).decode()
    info2 = ingest.inspect_magnet(f"magnet:?xt=urn:btih:{b32}")
    assert info2["infohash_v1"] == hex40.upper()


def test_magnet_rejects_bad_input():
    with pytest.raises(ingest.MagnetError):
        ingest.inspect_magnet("magnet:?dn=nothing")  # no xt
    with pytest.raises(ingest.MagnetError):
        ingest.inspect_magnet("magnet:?xt=urn:btih:aaa&xt=urn:btih:bbb")  # two xt
    with pytest.raises(ingest.MagnetError):
        ingest.inspect_magnet("https://example.com/?xt=urn:btih:" + "a" * 40)  # not magnet:
    with pytest.raises(ingest.MagnetError):
        ingest.inspect_magnet("magnet:?xt=urn:btih:" + "a" * 40 + "&tr=http://user:pw@t.example/announce")
    with pytest.raises(ingest.MagnetError):
        ingest.inspect_magnet("magnet:?xt=urn:btih:" + "a" * 39)  # wrong length


# --------------------------------------------------------------------------- #
# Torrent manifest parsing                                                      #
# --------------------------------------------------------------------------- #

def _make_torrent(tmp_path: Path, files: list[tuple[str, int]]) -> Path:
    info = {b"name": b"pack", b"piece length": 16384, b"pieces": b"x" * 20,
            b"files": [{b"length": n, b"path": [p.encode()]} for p, n in files]}
    raw = ingest._encode_bencode({b"info": info})
    path = tmp_path / "pack.torrent"
    path.write_bytes(raw)
    return path


def test_inspect_torrent_and_candidates(tmp_path):
    path = _make_torrent(tmp_path, [("movie.mp4", 1000), ("notes.txt", 10), ("extra.mkv", 500)])
    meta = ingest.inspect_torrent(path)
    assert meta["name"] == "pack"
    assert meta["file_count"] == 3
    assert [c["path"] for c in meta["video_candidates"]] == ["movie.mp4", "extra.mkv"]
    chosen = ingest.select_torrent_video(meta, 1)
    assert chosen["path"] == "movie.mp4"
    with pytest.raises(ingest.BencodeError):
        ingest.select_torrent_video(meta, 2)  # notes.txt is not a video
    assert len(ingest.torrent_infohash_v1(path)) == 40


def test_inspect_torrent_rejects_traversal_and_no_video(tmp_path):
    evil = _make_torrent(tmp_path, [("../escape.mp4", 5)])
    with pytest.raises(ingest.BencodeError):
        ingest.inspect_torrent(evil)
    novideo = _make_torrent(tmp_path, [("readme.txt", 5)])
    with pytest.raises(ingest.BencodeError):
        ingest.inspect_torrent(novideo)


def test_select_video_matches_single_payload(tmp_path):
    root = tmp_path / "dl"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "movie.mp4").write_bytes(b"\0" * 4)
    assert ingest.select_video(root, "sub/movie.mp4").name == "movie.mp4"
    with pytest.raises(FileNotFoundError):
        ingest.select_video(root, "sub/other.mp4")


# --------------------------------------------------------------------------- #
# Preserved-subsystem gates fail closed                                         #
# --------------------------------------------------------------------------- #

def _write_request(jobs_root: Path, job_id: str, kind: str, value: str) -> None:
    d = jobs_root / job_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "stage-a-request.json").write_text(json.dumps({
        "version": 2, "job_id": job_id,
        "source": {"kind": kind, "value": value},
        "options": {"whisper_model": "base", "language": "auto",
                    "target_duration_seconds": 120, "focus": "", "enable_vision_assist": True},
        "mode": "manual",
        "series": {"enabled": False, "series_id": "", "source_job_id": "",
                   "part": 0, "start_seconds": 0, "context": ""},
        "music": {"ref": "", "source": "none"},
        "saved_at_epoch": int(time.time()),
    }), encoding="utf-8")


def test_telegram_relay_fails_closed_without_relay_metadata(tmp_path, monkeypatch):
    """§9.2: a relay source without the workflow-written relay block is refused."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    kind = "telegram_relay"
    _write_request(tmp_path, f"job-{kind}", kind, "relay:private")
    with pytest.raises(ingest.IngestError, match="relay metadata"):
        ingest.ingest(f"job-{kind}", str(tmp_path / "work"), root=tmp_path)


def _relay_request(tmp_path, job_id, relay):
    _write_request(tmp_path, job_id, "telegram_relay", "relay:private")
    req_path = tmp_path / job_id / "stage-a-request.json"
    doc = json.loads(req_path.read_text())
    doc["source"]["relay"] = relay
    req_path.write_text(json.dumps(doc), encoding="utf-8")


def test_telegram_relay_rejects_bad_tag(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "motionssalt/clipforge")
    monkeypatch.setenv("GH_TOKEN", "test-token")
    _relay_request(tmp_path, "job-r1", {
        "release_tag": "evil-tag", "expected_size_bytes": "100",
        "sha256": "a" * 64,
    })
    with pytest.raises(ingest.IngestError, match="release tag"):
        ingest.ingest("job-r1", str(tmp_path / "work"), root=tmp_path)


def test_telegram_relay_rejects_oversize(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "motionssalt/clipforge")
    monkeypatch.setenv("GH_TOKEN", "test-token")
    _relay_request(tmp_path, "job-r2", {
        "release_tag": "clipforge-relay-input-job-r2",
        "expected_size_bytes": str(1800 * 1024 * 1024 + 1),
        "sha256": "a" * 64,
    })
    with pytest.raises(ingest.IngestError, match="source size"):
        ingest.ingest("job-r2", str(tmp_path / "work"), root=tmp_path)


def test_telegram_relay_rejects_bad_checksum_format(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "motionssalt/clipforge")
    monkeypatch.setenv("GH_TOKEN", "test-token")
    _relay_request(tmp_path, "job-r3", {
        "release_tag": "clipforge-relay-input-job-r3",
        "expected_size_bytes": "100",
        "sha256": "zzzz",
    })
    with pytest.raises(ingest.IngestError, match="checksum"):
        ingest.ingest("job-r3", str(tmp_path / "work"), root=tmp_path)


def test_telegram_relay_download_and_integrity_verification(tmp_path, monkeypatch):
    """Happy path + tamper path, with the GitHub API stubbed offline."""
    import hashlib as _hl
    monkeypatch.setenv("GITHUB_REPOSITORY", "motionssalt/clipforge")
    monkeypatch.setenv("GH_TOKEN", "test-token")
    payload = b"\x1a\x45\xdf\xa3" + b"relay-video-bytes" * 64
    digest = _hl.sha256(payload).hexdigest()

    class FakeResponse:
        def __init__(self, status=200, body=None, raw=None):
            self.status_code = status
            self._body = body or {}
            self._raw = raw or b""
        def json(self):
            return self._body
        def raise_for_status(self):
            if self.status_code >= 400:
                raise ingest.requests.HTTPError(f"http {self.status_code}")
        def iter_content(self, size):
            yield self._raw

    def fake_get(url, headers=None, stream=False, timeout=None):
        if url.endswith("/releases/tags/clipforge-relay-input-job-r4"):
            return FakeResponse(200, {"assets": [{"name": "source_input.bin", "url": "https://api.github.com/asset/1"}]})
        if url == "https://api.github.com/asset/1":
            assert headers.get("Accept") == "application/octet-stream"
            return FakeResponse(200, raw=payload)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(ingest.requests, "get", fake_get)
    monkeypatch.setattr(ingest, "detect_container_ext", lambda path: "mkv")

    _relay_request(tmp_path, "job-r4", {
        "release_tag": "clipforge-relay-input-job-r4",
        "expected_size_bytes": str(len(payload)),
        "sha256": digest,
    })
    record = ingest.ingest("job-r4", str(tmp_path / "work"), root=tmp_path)
    assert record["source_kind"] == "telegram_relay"
    assert record["size_bytes"] == len(payload)

    # Tampered checksum must fail closed and remove the partial file.
    _relay_request(tmp_path, "job-r5", {
        "release_tag": "clipforge-relay-input-job-r4",
        "expected_size_bytes": str(len(payload)),
        "sha256": "0" * 64,
    })
    with pytest.raises(ingest.IngestError, match="integrity"):
        ingest.ingest("job-r5", str(tmp_path / "work"), root=tmp_path)
    assert not (tmp_path / "work" / "source_input.bin").exists()


def test_telegram_relay_missing_release_404(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "motionssalt/clipforge")
    monkeypatch.setenv("GH_TOKEN", "test-token")

    class FakeResponse:
        status_code = 404
        def raise_for_status(self):
            raise ingest.requests.HTTPError("404")
        def json(self):
            return {}

    monkeypatch.setattr(ingest.requests, "get", lambda *a, **k: FakeResponse())
    _relay_request(tmp_path, "job-r6", {
        "release_tag": "clipforge-relay-input-job-r6",
        "expected_size_bytes": "100",
        "sha256": "a" * 64,
    })
    with pytest.raises(ingest.IngestError, match="not found"):
        ingest.ingest("job-r6", str(tmp_path / "work"), root=tmp_path)


def test_telegram_channel_kind_fails_closed_off_original_repo(tmp_path, monkeypatch):
    """§9.1 layer 1: the wired-in channel download refuses a non-original repo."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "someuser/clone")
    for name in ("CLIPFORGE_TELEGRAM_API_ID", "CLIPFORGE_TELEGRAM_API_HASH", "CLIPFORGE_TELEGRAM_SESSION"):
        monkeypatch.setenv(name, "present")
    _write_request(tmp_path, "job-tg", "telegram_channel", "https://t.me/somechannel/1")
    with pytest.raises(ingest.IngestError, match="original ClipForge"):
        ingest.ingest("job-tg", str(tmp_path / "work"), root=tmp_path)


def test_telegram_channel_kind_fails_closed_without_secrets(tmp_path, monkeypatch):
    """§9.1 layer 2: even on the original repo, missing MTProto secrets fail closed."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "motionssalt/clipforge")
    for name in ("CLIPFORGE_TELEGRAM_API_ID", "CLIPFORGE_TELEGRAM_API_HASH", "CLIPFORGE_TELEGRAM_SESSION"):
        monkeypatch.delenv(name, raising=False)
    _write_request(tmp_path, "job-tg2", "telegram_channel", "https://t.me/somechannel/1")
    with pytest.raises(ingest.IngestError, match="MTProto"):
        ingest.ingest("job-tg2", str(tmp_path / "work"), root=tmp_path)


def test_telegram_channel_kind_rejects_non_post_link(tmp_path, monkeypatch):
    """Channel kind rejects groups/private/non-post links before any network I/O."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "motionssalt/clipforge")
    for name in ("CLIPFORGE_TELEGRAM_API_ID", "CLIPFORGE_TELEGRAM_API_HASH", "CLIPFORGE_TELEGRAM_SESSION"):
        monkeypatch.setenv(name, "present")
    _write_request(tmp_path, "job-tg3", "telegram_channel", "https://t.me/c/123/456")
    with pytest.raises(ingest.IngestError, match="public Telegram channel post link"):
        ingest.ingest("job-tg3", str(tmp_path / "work"), root=tmp_path)


def test_telegram_channel_kind_routes_to_mtproto_download(tmp_path, monkeypatch):
    """With both gates satisfied, the kind delegates to telegram_channel.download_channel_post."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "motionssalt/clipforge")
    for name in ("CLIPFORGE_TELEGRAM_API_ID", "CLIPFORGE_TELEGRAM_API_HASH", "CLIPFORGE_TELEGRAM_SESSION"):
        monkeypatch.setenv(name, "present")
    called = {}

    def fake_download(url, output_path, *, environ=None):
        called["url"] = url
        from pipeline.stage_a import telegram_channel as tc
        canonical = tc.download_channel_post.__wrapped__ if hasattr(tc.download_channel_post, "__wrapped__") else None
        # Simulate the download producing a tiny mkv-looking payload.
        import pathlib
        pathlib.Path(output_path).write_bytes(b"\x1a\x45\xdf\xa3" + b"0" * 32)
        return "https://t.me/somechannel/1"

    monkeypatch.setattr(
        "pipeline.stage_a.telegram_channel.download_channel_post", fake_download
    )
    monkeypatch.setattr(ingest, "detect_container_ext", lambda path: "mkv")
    _write_request(tmp_path, "job-tg4", "telegram_channel", "https://t.me/somechannel/1")
    record = ingest.ingest("job-tg4", str(tmp_path / "work"), root=tmp_path)
    assert called["url"] == "https://t.me/somechannel/1"
    assert record["source_kind"] == "telegram_channel"
    assert record["container"] == "mkv"
    assert record["size_bytes"] > 0


def test_url_kind_telegram_post_routes_to_channel_download(tmp_path, monkeypatch):
    """A t.me post link handed in as a plain URL follows the §9.1 path (legacy semantics)."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "motionssalt/clipforge")
    for name in ("CLIPFORGE_TELEGRAM_API_ID", "CLIPFORGE_TELEGRAM_API_HASH", "CLIPFORGE_TELEGRAM_SESSION"):
        monkeypatch.setenv(name, "present")
    called = {}

    def fake_download(url, output_path, *, environ=None):
        called["url"] = url
        import pathlib
        pathlib.Path(output_path).write_bytes(b"\x1a\x45\xdf\xa3" + b"0" * 32)
        return "https://t.me/somechannel/7"

    monkeypatch.setattr(
        "pipeline.stage_a.telegram_channel.download_channel_post", fake_download
    )
    monkeypatch.setattr(ingest, "detect_container_ext", lambda path: "mp4")
    _write_request(tmp_path, "job-tg5", "url", "https://t.me/s/somechannel/7")
    record = ingest.ingest("job-tg5", str(tmp_path / "work"), root=tmp_path)
    assert called["url"] == "https://t.me/s/somechannel/7"
    assert record["source_kind"] == "telegram_channel"
    assert record["container"] == "mp4"


def test_load_request_rejects_unknown_kind(tmp_path):
    _write_request(tmp_path, "job-x", "ftp", "ftp://x")
    with pytest.raises(ingest.IngestError):
        ingest.load_request("job-x", root=tmp_path)


# --------------------------------------------------------------------------- #
# Scenes: shot index + key moments (pure logic)                                 #
# --------------------------------------------------------------------------- #

def test_build_shot_index_folds_flickers():
    shots = scenes.build_shot_index([10.0, 10.1, 25.0], 40.0)
    # 10.0->10.1 is a 0.1s flicker: folded by EXTENDING the previous shot's
    # end to 10.1 (preserved legacy behavior), not by creating a new shot.
    assert len(shots) == 3
    assert shots[0]["cause"] == "video_start"
    assert shots[0]["start_seconds"] == 0.0 and shots[0]["end_seconds"] == 10.1
    assert shots[1]["start_seconds"] == 10.1 and shots[1]["end_seconds"] == 25.0
    assert shots[-1]["end_seconds"] == 40.0


def test_score_emotional():
    assert scenes.score_emotional("") == 0.0
    assert scenes.score_emotional("the weather is nice today") == 0.0
    assert scenes.score_emotional("he will kill him, i swear he will die, dead, murder, blood") == 1.0


def test_transcript_between_density():
    segs = [{"start": 0.0, "end": 5.0, "text": "hello"}, {"start": 5.0, "end": 10.0, "text": "world"}]
    text, density = scenes.transcript_between(segs, 0.0, 10.0)
    assert text == "hello world"
    assert density == 1.0
    _, density = scenes.transcript_between(segs, 0.0, 20.0)
    assert density == 0.5


def test_compute_visual_tail_bounds():
    shots = [
        {"shot_id": 1, "start_seconds": 0.0, "end_seconds": 10.0},
        {"shot_id": 2, "start_seconds": 10.0, "end_seconds": 12.0},  # 2s next shot
        {"shot_id": 3, "start_seconds": 12.0, "end_seconds": 30.0},
    ]
    # tail capped at half the next (2s) shot -> 1.0s, below min but no room
    assert scenes.compute_visual_tail(shots, 0, 30.0) == 1.0
    # last shot: capped by video end
    assert scenes.compute_visual_tail(shots, 2, 30.0) == 0.0
    # roomy middle case
    shots2 = [
        {"shot_id": 1, "start_seconds": 0.0, "end_seconds": 10.0},
        {"shot_id": 2, "start_seconds": 10.0, "end_seconds": 30.0},
    ]
    assert scenes.compute_visual_tail(shots2, 0, 30.0) == 3.5


# --------------------------------------------------------------------------- #
# Bundle assembly                                                               #
# --------------------------------------------------------------------------- #

def _stage_work(tmp_path: Path, with_events: bool = True) -> Path:
    work = tmp_path / "work"
    (work / "analysis").mkdir(parents=True)
    (work / "screenshots").mkdir()
    (work / "original.mp4").write_bytes(b"\0" * 32)
    # bug-68: analysis_720p.mp4 is deliberately absent — bundle assembly must
    # succeed without it since nothing downstream ever consumed it.
    (work / "transcript.json").write_text(json.dumps({
        "audio_duration_seconds": 42.0, "segments": []}), encoding="utf-8")
    (work / "scene_index.json").write_text(json.dumps({
        "video_duration_seconds": 42.0, "shot_count": 1,
        "shots": [{"shot_id": 1, "start_seconds": 0.0, "end_seconds": 42.0,
                   "keyframe_seconds": 21.0, "cause": "video_start"}]}), encoding="utf-8")
    (work / "key_moments.json").write_text(json.dumps({
        "video_duration_seconds": 42.0, "moment_count": 0, "moments": []}), encoding="utf-8")
    (work / "screenshots" / "frame_000000.jpg").write_bytes(b"\xff\xd8\xff")  # jpeg magic
    (work / "screenshots" / "frame_000006.jpg").write_bytes(b"\xff\xd8\xff")
    if with_events:
        (work / "screenshots" / "event_000021000.jpg").write_bytes(b"\xff\xd8\xff")
    return work


def test_run_bundle_produces_contract_assets(tmp_path, monkeypatch):
    jobs_root = tmp_path / "jobs"
    _write_request(jobs_root, "job-bundle", "url", "https://example.com/v.mp4")
    work = _stage_work(tmp_path)
    monkeypatch.chdir(tmp_path.parents[0] if False else Path.cwd())  # keep cwd; prompt runs via -m

    manifest = bundle.run_bundle("job-bundle", str(work), "clipforge-job-bundle",
                                 jobs_root=str(jobs_root))
    bdir = work / "bundle"
    names = {a["name"] for a in manifest["assets"]}
    assert {"source_input.bin", "transcript.json",
            "screenshots.zip", "event_composites.zip", "scene_index.json",
            "key_moments.json", "00_READ_THIS_FIRST.txt", "manifest.json"} <= names
    # bug-68: the dead 720p analysis copy must NOT be bundled anymore
    assert "analysis_720p.mp4" not in names
    assert not (bdir / "analysis_720p.mp4").exists()
    assert "analysis_copy_720p" not in {a["purpose"] for a in manifest["assets"]}
    assert manifest["release_tag"] == "clipforge-job-bundle"
    # schema-required fields on hashed assets
    src = next(a for a in manifest["assets"] if a["name"] == "source_input.bin")
    assert src["purpose"] == "source_full_quality"
    assert src["size_bytes"] == 32 and len(src["sha256"]) == 64
    # prompt generated and mentions the job
    prompt_text = (bdir / "00_READ_THIS_FIRST.txt").read_text(encoding="utf-8")
    assert "job-bundle" in prompt_text and "production.json" in prompt_text
    # screenshots.zip holds both kinds; event_composites.zip only events
    with zipfile.ZipFile(bdir / "screenshots.zip") as zf:
        zipped = set(zf.namelist())
    assert {"frame_000000.jpg", "frame_000006.jpg", "event_000021000.jpg"} <= zipped
    with zipfile.ZipFile(bdir / "event_composites.zip") as zf:
        assert set(zf.namelist()) == {"event_000021000.jpg"}


def test_run_bundle_omits_event_zip_when_no_events(tmp_path):
    jobs_root = tmp_path / "jobs"
    _write_request(jobs_root, "job-noev", "url", "https://example.com/v.mp4")
    work = _stage_work(tmp_path, with_events=False)
    manifest = bundle.run_bundle("job-noev", str(work), "clipforge-job-noev",
                                 jobs_root=str(jobs_root))
    names = {a["name"] for a in manifest["assets"]}
    assert "event_composites.zip" not in names
    with zipfile.ZipFile(work / "bundle" / "screenshots.zip") as zf:
        assert set(zf.namelist()) == {"frame_000000.jpg", "frame_000006.jpg"}


def test_run_bundle_requires_core_inputs(tmp_path):
    jobs_root = tmp_path / "jobs"
    _write_request(jobs_root, "job-missing", "url", "https://example.com/v.mp4")
    work = _stage_work(tmp_path)
    (work / "transcript.json").unlink()
    with pytest.raises(bundle.BundleError):
        bundle.run_bundle("job-missing", str(work), "clipforge-job-missing",
                          jobs_root=str(jobs_root))
