#!/usr/bin/env python3
"""Offline regression tests for production.json -> Zernio metadata mapping."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from zernio_publish import (
    build_platform_payload,
    load_production,
    normalize_hashtags,
    normalize_tags,
    aggregate_publishing_status,
    caption_with_visible_hashtags,
    post_summary,
    production_metadata,
    publish_video,
    reconcile_post_status,
    write_publishing_state,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "scripts" / "fixtures" / "zernio_production.json"


def test_tracked_production_fixture() -> None:
    doc = load_production(EXAMPLE)
    meta = production_metadata(doc)
    assert meta["title"] == "Morty Enters the Fear Hole Alone and It Knows Why"
    assert meta["caption"] == meta["title"]  # no caption field exists in the actual schema
    assert meta["hashtags"] == doc["hashtags"]
    assert meta["tags"] == doc["youtube_tags"]
    assert all(tag.startswith("#") and not tag.startswith("##") for tag in meta["hashtags"])
    assert all(not tag.startswith("#") and "," not in tag for tag in meta["tags"])


def test_aggregate_status_comes_from_per_platform_posts() -> None:
    assert aggregate_publishing_status([{"status": "published"}, {"status": "publishing"}]) == "publishing"
    assert aggregate_publishing_status([{"status": "published"}, {"status": "failed"}]) == "partial"
    assert aggregate_publishing_status([{"status": "published"}, {"status": "published"}]) == "published"
    assert aggregate_publishing_status([{"status": "failed"}, {"status": "cancelled"}]) == "failed"


def test_normalization_does_not_duplicate_hashes_or_tags() -> None:
    assert normalize_hashtags(["#One", "##one", "Two", " #THREE "]) == ["#One", "#Two", "#THREE"]
    assert normalize_tags(["#one", "one", "two, words", "x" * 101]) == ["one", "two words"]


def test_visible_hashtags_are_appended_once() -> None:
    visible = caption_with_visible_hashtags("A caption", ["#one", "#two"])
    assert visible == "A caption\n\n#one #two"
    assert caption_with_visible_hashtags("", ["#one"]) == "#one"
    assert caption_with_visible_hashtags("A caption", []) == "A caption"


def test_platform_payloads_are_separate() -> None:
    doc = load_production(EXAMPLE)
    tiktok = build_platform_payload(doc, "tiktok", ["tt-1"], "https://media.example/final.mp4")
    youtube = build_platform_payload(doc, "youtube", ["yt-1"], "https://media.example/final.mp4")
    expected_content = doc["title"] + "\n\n" + " ".join(doc["hashtags"])
    assert tiktok["content"] == expected_content
    assert tiktok["hashtags"] == doc["hashtags"]
    assert "title" not in tiktok
    assert "tags" not in tiktok
    assert youtube["title"] == doc["title"]
    assert youtube["tags"] == doc["youtube_tags"]
    assert youtube["hashtags"] == doc["hashtags"]
    assert youtube["content"] == expected_content
    assert all(entry["platform"] == "tiktok" for entry in tiktok["platforms"])
    assert all(entry["platform"] == "youtube" for entry in youtube["platforms"])


def test_optional_caption_and_tags_aliases_are_only_used_when_present() -> None:
    doc = {
        "title": "A title",
        "caption": "The generated caption.",
        "description": "A different description.",
        "hashtags": ["#one", "##two"],
        "tags": ["#keyword", "keyword", "plain"],
        "video_duration_seconds": 10,
        "target_total_duration_seconds": 5,
        "cuts": [{"start_seconds": 0, "end_seconds": 5, "voiceover_text": "Hello."}],
    }
    meta = production_metadata(doc)
    assert meta["caption"] == "The generated caption."
    assert meta["tags"] == ["keyword", "plain"]
    assert meta["metadata_source"]["caption"] == "production.json.caption"
    assert meta["metadata_source"]["tags"] == "production.json.tags"


def test_idempotent_existing_post_response_is_preserved() -> None:
    summary = post_summary({"existingPost": {
        "_id": "existing-1", "status": "scheduled", "scheduledFor": "2026-08-23T19:30:00",
        "timezone": "Europe/London", "platforms": [{"platform": "youtube", "accountId": "yt-1"}],
    }})
    assert summary["post_id"] == "existing-1"
    assert summary["status"] == "scheduled"
    assert summary["timezone"] == "Europe/London"


class _Response:
    def __init__(self, payload, status=200):
        self.payload = json.dumps(payload).encode("utf-8") if isinstance(payload, dict) else payload
        self.status = status
    def read(self, *_args):
        return self.payload
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return False


def test_reconcile_refreshes_tiktok_until_terminal() -> None:
    responses = iter([
        {"post": {"_id": "tt-async", "status": "published", "platforms": [
            {"platform": "tiktok", "accountId": "tt-1", "status": "published", "platformPostId": "live-1"},
        ]}},
    ])
    requests = []
    def opener(req, timeout=0):
        requests.append((req.get_method(), req.full_url))
        return _Response(next(responses))
    initial = {"post_id": "tt-async", "status": "publishing", "platforms": [
        {"platform": "tiktok", "accountId": "tt-1", "status": "publishing"},
    ]}
    sleeps = []
    result = reconcile_post_status(
        "sk_test_key_not_logged", initial, mode="publish_now", opener=opener,
        sleep=sleeps.append, poll_interval_seconds=0, max_attempts=3,
    )
    assert result["status"] == "published"
    assert result["platforms"][0]["status"] == "published"
    assert requests == [("GET", "https://zernio.com/api/v1/posts/tt-async")]
    assert sleeps == [0]


def test_publish_flow_isolated_per_platform_and_idempotent() -> None:
    requests = []
    def opener(req, timeout=0):
        requests.append((req.get_method(), req.full_url, req.headers.get("X-request-id")))
        if req.full_url.endswith("/media/presign"):
            return _Response({"uploadUrl": "https://upload.example/object", "publicUrl": "https://media.example/final.mp4"})
        if req.full_url == "https://upload.example/object":
            return _Response(b"", 200)
        if req.full_url.endswith("/posts"):
            body = json.loads(req.data.decode("utf-8"))
            platform = body["platforms"][0]["platform"]
            if platform == "youtube":
                return _Response({"existingPost": {"_id": "yt-existing", "status": "scheduled", "platforms": body["platforms"]}})
            return _Response({"post": {"_id": "tt-new", "status": "scheduled", "platforms": body["platforms"]}})
        raise AssertionError(req.full_url)
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / "final.mp4"
        video.write_bytes(b"not-a-real-video-but-a-nonempty-fixture")
        result = publish_video(
            "sk_test_key_not_logged", EXAMPLE, video,
            [{"platform": "tiktok", "account_ids": ["tt-1"]}, {"platform": "youtube", "account_ids": ["yt-1"]}],
            mode="manual_schedule", scheduled_for="2026-08-23T19:30:00", timezone="Europe/London",
            request_id="clipforge-test-request", opener=opener,
        )
    assert result["status"] == "scheduled"
    assert [post["post_id"] for post in result["posts"]] == ["tt-new", "yt-existing"]
    assert result["posts"][1]["request_id"] == "clipforge-test-request-youtube"
    assert any(method == "PUT" and url == "https://upload.example/object" for method, url, _ in requests)
    assert all("sk_test_key_not_logged" not in str(item) for item in requests)


def test_metadata_survives_publishing_state_merge() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        status = Path(tmp) / "status.json"
        status.write_text(json.dumps({"stage": "complete", "extra": {"title": "unchanged"}}))
        state = {"provider": "zernio", "status": "scheduled", "metadata": {"title": "A", "hashtags": ["#a"], "tags": ["tag"]}}
        write_publishing_state(status, state)
        saved = json.loads(status.read_text())
        assert saved["stage"] == "complete"
        assert saved["extra"]["title"] == "unchanged"
        assert saved["publishing"]["metadata"]["hashtags"] == ["#a"]
        assert saved["publishing"]["metadata"]["tags"] == ["tag"]


def test_cli_uses_the_actual_production_file() -> None:
    output = subprocess.check_output(["python3", str(ROOT / "scripts" / "zernio_publish.py"), "metadata", str(EXAMPLE)], text=True)
    meta = json.loads(output)
    assert meta["metadata_source"]["hashtags"] == "production.json.hashtags"
    assert meta["metadata_source"]["tags"] == "production.json.youtube_tags"


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"zernio metadata tests passed ({len(tests)} tests)")
