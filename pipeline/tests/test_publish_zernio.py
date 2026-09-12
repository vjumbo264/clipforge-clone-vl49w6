"""Offline tests for pipeline/publish/zernio.py (Zernio publishing subsystem).

Covers state/queue helpers, smart-schedule slot search, target normalization
and encoding, metadata normalization, aggregate_publishing_status, HTTP
transport error mapping (via a mock opener), publish_video end-to-end with
mocked network, and resolve_manual_dispatch.

No test makes real network calls. All HTTP is exercised via a mock opener.
"""

from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timedelta, timezone as pytz
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from pipeline.publish import zernio
from pipeline.publish.zernio import (
    TargetValidationError,
    ZernioError,
    active_accounts,
    aggregate_publishing_status,
    automatic_dispatch,
    automatic_fields,
    build_platform_payload,
    caption_with_visible_hashtags,
    cancel_post,
    decode_targets,
    default_queue,
    encode_targets,
    load_production,
    normalize_hashtags,
    normalize_tags,
    normalize_targets,
    plan_smart_schedule,
    post_summary,
    production_metadata,
    publish_video,
    publishing_error_state,
    queue_count,
    read_json_object_safely,
    reconcile_post_status,
    remove_queue_item,
    resolve_manual_dispatch,
    retry_post,
    safe_fingerprint,
    smart_interval_hours,
    targets_from_settings,
    update_post,
    upsert_queue_item,
    validate_hhmm,
    validate_mode,
    validate_platform,
    validate_request_id,
    validate_timezone,
    write_publishing_state,
)


# --------------------------------------------------------------------------- #
# Mock HTTP opener                                                              #
# --------------------------------------------------------------------------- #


class _MockResponse:
    def __init__(self, status: int, body: dict[str, Any] | bytes):
        self.status = status
        if isinstance(body, (bytes, bytearray)):
            self._data = bytes(body)
        else:
            self._data = json.dumps(body).encode("utf-8")

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            data, self._data = self._data, b""
            return data
        data, self._data = self._data[:n], self._data[n:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mock_opener(script):
    """Return an opener callable that plays back a scripted sequence of responses.

    script: list of dicts with keys:
      - match: dict(method=..., path_contains=...) OR callable(request)->bool
      - status: int
      - body: dict OR bytes
      - raise_http: optional int -> raise HTTPError with body/status
      - raise_url: optional bool -> raise URLError
    """
    calls = []
    remaining = list(script)

    def opener(req, timeout=None):
        method = getattr(req, "method", "GET") or "GET"
        url = getattr(req, "full_url", None) or req.get_full_url()
        headers = dict(req.header_items())
        body = None
        if getattr(req, "data", None):
            if isinstance(req.data, bytes):
                try:
                    body = json.loads(req.data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body = req.data
            else:
                body = req.data
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        if not remaining:
            raise AssertionError(f"unexpected extra HTTP call: {method} {url}")
        step = remaining.pop(0)
        matcher = step.get("match")
        if callable(matcher):
            ok = matcher(req)
        elif isinstance(matcher, dict):
            ok = True
            if "method" in matcher and matcher["method"] != method:
                ok = False
            if "path_contains" in matcher and matcher["path_contains"] not in url:
                ok = False
        else:
            ok = True
        if not ok:
            raise AssertionError(
                f"HTTP call did not match expected step: got {method} {url}; expected {matcher!r}",
            )
        if step.get("raise_url"):
            from urllib.error import URLError
            raise URLError("mocked network failure")
        if "raise_http" in step:
            payload = step.get("body") or {}
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            raise HTTPError(url, step["raise_http"], "mock", {}, io.BytesIO(data))
        return _MockResponse(step.get("status", 200), step.get("body", {}))

    opener.calls = calls
    opener.remaining = remaining
    return opener


# --------------------------------------------------------------------------- #
# Validation helpers                                                            #
# --------------------------------------------------------------------------- #


class ValidationHelpers(unittest.TestCase):
    def test_validate_platform(self):
        self.assertEqual(validate_platform("tiktok"), "tiktok")
        self.assertEqual(validate_platform("YouTube"), "youtube")
        # bug-52: Instagram is a first-class Zernio platform now.
        self.assertEqual(validate_platform("Instagram"), "instagram")
        with self.assertRaises(ZernioError):
            validate_platform("facebook")

    def test_validate_mode(self):
        for mode in ("publish_now", "manual_schedule", "smart_schedule"):
            self.assertEqual(validate_mode(mode), mode)
        with self.assertRaises(ZernioError):
            validate_mode("draft")

    def test_validate_hhmm(self):
        self.assertEqual(validate_hhmm("09:00"), "09:00")
        self.assertEqual(validate_hhmm("23:59"), "23:59")
        with self.assertRaises(ValueError):
            validate_hhmm("24:00")
        with self.assertRaises(ValueError):
            validate_hhmm("9:00")

    def test_validate_timezone(self):
        self.assertEqual(validate_timezone("UTC"), "UTC")
        self.assertEqual(validate_timezone("Europe/London"), "Europe/London")
        with self.assertRaises(ValueError):
            validate_timezone("Mars/Central")

    def test_validate_request_id(self):
        generated = validate_request_id(None)
        self.assertRegex(generated, r"[0-9a-f-]{36}")
        self.assertEqual(validate_request_id("clipforge.manual-abc:123"), "clipforge.manual-abc:123")
        with self.assertRaises(ZernioError):
            validate_request_id("short")  # <8 chars
        with self.assertRaises(ZernioError):
            validate_request_id("bad space id here")

    def test_smart_interval_hours_migrates_days(self):
        self.assertEqual(smart_interval_hours({"interval_hours": 6}), 6)
        self.assertEqual(smart_interval_hours({"interval_days": 2}), 48)
        self.assertEqual(smart_interval_hours({}), 24)
        with self.assertRaises(ValueError):
            smart_interval_hours({"interval_hours": 0})
        with self.assertRaises(ValueError):
            smart_interval_hours({"interval_hours": "abc"})
        with self.assertRaises(ValueError):
            smart_interval_hours({"interval_hours": 99999})

    def test_safe_fingerprint(self):
        self.assertEqual(safe_fingerprint("short"), "…")
        self.assertEqual(safe_fingerprint("abcdefgh"), "…")
        fp = safe_fingerprint("abcdefghij1234567890")
        self.assertTrue(fp.startswith("abcd") and fp.endswith("7890") and "…" in fp)


# --------------------------------------------------------------------------- #
# Metadata + hashtag/tag normalization                                          #
# --------------------------------------------------------------------------- #


class Metadata(unittest.TestCase):
    def test_normalize_hashtags_dedupes_and_prefixes(self):
        self.assertEqual(
            normalize_hashtags(["#alpha", "beta", "#Alpha", "gamma  space", "  ", ""]),
            ["#alpha", "#beta"],
        )
        self.assertEqual(normalize_hashtags(None), [])
        self.assertEqual(normalize_hashtags(["#not a tag", "ok"]), ["#ok"])

    def test_normalize_tags_length_and_total_cap(self):
        # length cap 100 per tag
        long = "x" * 101
        self.assertEqual(normalize_tags([long, "ok"]), ["ok"])
        # total 500 cap
        big = ["a" * 90] * 10
        result = normalize_tags(big)
        total_len = sum(len(t) for t in result) + max(0, len(result) - 1)
        self.assertLessEqual(total_len, 500)

    def test_load_production_rejects_non_object(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "production.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ZernioError):
                load_production(path)

    def test_load_production_rejects_wrong_types(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "production.json"
            path.write_text(json.dumps({"title": 123}), encoding="utf-8")
            with self.assertRaises(ZernioError):
                load_production(path)
            path.write_text(json.dumps({"hashtags": "nope"}), encoding="utf-8")
            with self.assertRaises(ZernioError):
                load_production(path)

    def test_production_metadata_prefers_caption_over_description(self):
        meta = production_metadata({
            "title": "  Title  ",
            "caption": "  cap  ",
            "description": "descr",
            "hashtags": ["#a"],
            "youtube_tags": ["kw1", "kw2"],
        })
        self.assertEqual(meta["title"], "Title")
        self.assertEqual(meta["caption"], "cap")
        self.assertEqual(meta["hashtags"], ["#a"])
        self.assertEqual(meta["tags"], ["kw1", "kw2"])
        self.assertEqual(meta["metadata_source"]["tags"], "production.json.youtube_tags")

    def test_production_metadata_falls_back_to_title(self):
        meta = production_metadata({"title": "Only Title"})
        self.assertEqual(meta["caption"], "Only Title")
        self.assertEqual(meta["metadata_source"]["caption"], "production.json.title")

    def test_caption_with_visible_hashtags(self):
        self.assertEqual(caption_with_visible_hashtags("base", ["#a", "#b"]), "base\n\n#a #b")
        self.assertEqual(caption_with_visible_hashtags("", ["#a"]), "#a")
        self.assertEqual(caption_with_visible_hashtags("base", []), "base")


# --------------------------------------------------------------------------- #
# Targets / settings                                                            #
# --------------------------------------------------------------------------- #


class Targets(unittest.TestCase):
    def test_normalize_targets_dedupes_ids(self):
        result = normalize_targets([
            {"platform": "tiktok", "account_ids": ["A", "A", "  ", "B"]},
        ])
        self.assertEqual(result, [{"platform": "tiktok", "account_ids": ["A", "B"]}])

    def test_normalize_targets_rejects_duplicate_platforms(self):
        with self.assertRaises(TargetValidationError):
            normalize_targets([
                {"platform": "tiktok", "account_ids": ["A"]},
                {"platform": "tiktok", "account_ids": ["B"]},
            ])

    def test_normalize_targets_rejects_empty_by_default(self):
        with self.assertRaises(TargetValidationError):
            normalize_targets([])
        # allow_empty is accepted only for the settings->transport path
        self.assertEqual(normalize_targets([], allow_empty=True), [])

    def test_normalize_targets_rejects_unknown_platform(self):
        with self.assertRaises(TargetValidationError):
            normalize_targets([{"platform": "facebook", "account_ids": ["A"]}])
        # bug-52: instagram is accepted.
        self.assertEqual(
            normalize_targets([{"platform": "instagram", "account_ids": ["IG1"]}]),
            [{"platform": "instagram", "account_ids": ["IG1"]}],
        )

    def test_encode_decode_roundtrip(self):
        original = [{"platform": "tiktok", "account_ids": ["a", "b"]}]
        b64 = encode_targets(original)
        self.assertEqual(decode_targets(b64), original)

    def test_decode_rejects_bad_base64(self):
        with self.assertRaises(TargetValidationError):
            decode_targets("not_base64!!!")

    def test_targets_from_settings(self):
        settings = {
            "target_accounts": {"tiktok": ["A"], "youtube": [], "instagram": ["IG1"]},
        }
        self.assertEqual(
            targets_from_settings(settings),
            [{"platform": "tiktok", "account_ids": ["A"]}, {"platform": "instagram", "account_ids": ["IG1"]}],
        )

    def test_automatic_fields_disabled(self):
        skip, mode, tz, b64 = automatic_fields({})
        self.assertEqual((skip, mode, tz, b64), ("true", "", "", ""))

    def test_automatic_fields_enabled(self):
        settings = {
            "enabled": True,
            "auto_publish": True,
            "automatic_mode": "smart_schedule",
            "smart_schedule": {"timezone": "Europe/Berlin"},
            "target_accounts": {"tiktok": ["A"]},
        }
        skip, mode, tz, b64 = automatic_fields(settings)
        self.assertEqual(skip, "false")
        self.assertEqual(mode, "smart_schedule")
        self.assertEqual(tz, "Europe/Berlin")
        self.assertEqual(decode_targets(b64), [{"platform": "tiktok", "account_ids": ["A"]}])

    def test_automatic_fields_unsafe_timezone(self):
        settings = {
            "enabled": True,
            "auto_publish": True,
            "automatic_mode": "publish_now",
            "smart_schedule": {"timezone": "not/a/zone/;rm -rf /"},
            "target_accounts": {"tiktok": ["A"]},
        }
        with self.assertRaises(TargetValidationError):
            automatic_fields(settings)


# --------------------------------------------------------------------------- #
# Queue helpers                                                                 #
# --------------------------------------------------------------------------- #


class Queue(unittest.TestCase):
    def test_default_queue(self):
        q = default_queue()
        self.assertEqual(q, {"version": 1, "provider": "zernio", "items": []})

    def test_upsert_updates_existing(self):
        q = default_queue()
        q = upsert_queue_item(q, {"job_id": "j1", "status": "queued"})
        q = upsert_queue_item(q, {"job_id": "j1", "status": "publishing"})
        self.assertEqual(len(q["items"]), 1)
        self.assertEqual(q["items"][0]["status"], "publishing")

    def test_upsert_appends_new(self):
        q = default_queue()
        q = upsert_queue_item(q, {"job_id": "j1", "status": "queued"})
        q = upsert_queue_item(q, {"job_id": "j2", "status": "scheduled"})
        self.assertEqual(len(q["items"]), 2)

    def test_remove(self):
        q = default_queue()
        q = upsert_queue_item(q, {"job_id": "j1", "status": "queued"})
        q = upsert_queue_item(q, {"job_id": "j2", "status": "scheduled"})
        q = remove_queue_item(q, "j1")
        self.assertEqual([i["job_id"] for i in q["items"]], ["j2"])

    def test_queue_count_only_active(self):
        q = default_queue()
        q = upsert_queue_item(q, {"job_id": "j1", "status": "queued"})
        q = upsert_queue_item(q, {"job_id": "j2", "status": "published"})
        q = upsert_queue_item(q, {"job_id": "j3", "status": "publishing"})
        self.assertEqual(queue_count(q), 2)

    def test_active_accounts_filters_inactive(self):
        accounts_doc = {
            "accounts": [
                {"id": "A1", "platform": "tiktok"},
                {"id": "A2", "platform": "tiktok", "isActive": False},
                {"id": "Y1", "platform": "youtube", "needsReconnection": True},
                {"id": "Y2", "platform": "youtube", "enabled": False},
                {"id": "Y3", "platform": "youtube"},
                {"id": "IG1", "platform": "instagram"},
                {"id": "IG2", "platform": "instagram", "needsReconnection": True},
            ],
        }
        result = active_accounts(accounts_doc)
        self.assertEqual([a["id"] for a in result["tiktok"]], ["A1"])
        self.assertEqual([a["id"] for a in result["youtube"]], ["Y3"])
        self.assertEqual([a["id"] for a in result["instagram"]], ["IG1"])


# --------------------------------------------------------------------------- #
# Smart-schedule slot search                                                    #
# --------------------------------------------------------------------------- #


class SmartSchedule(unittest.TestCase):
    def _now(self, hour: int = 8, tz: str = "Europe/London") -> datetime:
        # A weekday morning, well before the default 19:30 slot.
        return datetime(2026, 6, 15, hour, 0, tzinfo=ZoneInfo(tz))

    def test_daily_default_uses_today_pref_time(self):
        settings = {"smart_schedule": {"timezone": "Europe/London", "preferred_time": "19:30", "interval_hours": 24}}
        result = plan_smart_schedule(settings, default_queue(), now=self._now())
        self.assertEqual(result["timezone"], "Europe/London")
        self.assertTrue(result["scheduled_for"].startswith("2026-06-15T19:30"))

    def test_daily_when_past_pref_shifts_to_tomorrow(self):
        # It's already 21:00 local; must land at 19:30 tomorrow.
        settings = {"smart_schedule": {"timezone": "Europe/London", "preferred_time": "19:30", "interval_hours": 24}}
        result = plan_smart_schedule(settings, default_queue(), now=self._now(hour=21))
        self.assertTrue(result["scheduled_for"].startswith("2026-06-16T19:30"))

    def test_daily_avoids_collision_with_queue(self):
        settings = {"smart_schedule": {"timezone": "Europe/London", "preferred_time": "19:30", "interval_hours": 24}}
        queue = default_queue()
        queue = upsert_queue_item(queue, {
            "job_id": "j1", "status": "scheduled",
            "scheduled_for": "2026-06-15T19:30:00", "timezone": "Europe/London",
        })
        result = plan_smart_schedule(settings, queue, now=self._now())
        # Must skip to the next daily slot at the same wall-clock time.
        self.assertTrue(result["scheduled_for"].startswith("2026-06-16T19:30"))

    def test_hourly_cadence(self):
        settings = {"smart_schedule": {"timezone": "UTC", "preferred_time": "12:00", "interval_hours": 1}}
        now = datetime(2026, 6, 15, 8, 0, tzinfo=pytz.utc)
        result = plan_smart_schedule(settings, default_queue(), now=now)
        # Anchor is today 12:00 which is > now, so first available == 12:00.
        self.assertTrue(result["scheduled_for"].startswith("2026-06-15T12:00"))

    def test_external_scheduled_blocks_slot(self):
        settings = {"smart_schedule": {"timezone": "UTC", "preferred_time": "12:00", "interval_hours": 24}}
        now = datetime(2026, 6, 15, 8, 0, tzinfo=pytz.utc)
        external = [{"scheduled_for": "2026-06-15T12:00:00", "timezone": "UTC"}]
        result = plan_smart_schedule(settings, default_queue(), external_posts=external, now=now)
        self.assertTrue(result["scheduled_for"].startswith("2026-06-16T12:00"))

    def test_custom_start_respected(self):
        settings = {"smart_schedule": {
            "timezone": "UTC",
            "preferred_time": "12:00",
            "interval_hours": 24,
            "start_mode": "custom",
            "custom_start": "2026-07-01T08:00:00",
        }}
        now = datetime(2026, 6, 15, 8, 0, tzinfo=pytz.utc)
        result = plan_smart_schedule(settings, default_queue(), now=now)
        self.assertEqual(result["scheduled_for"], "2026-07-01T08:00:00")


# --------------------------------------------------------------------------- #
# aggregate_publishing_status                                                   #
# --------------------------------------------------------------------------- #


class Aggregate(unittest.TestCase):
    def test_all_published(self):
        self.assertEqual(aggregate_publishing_status([
            {"status": "published"}, {"status": "published"},
        ]), "published")

    def test_active_wins(self):
        self.assertEqual(aggregate_publishing_status([
            {"status": "published"}, {"status": "publishing"},
        ]), "publishing")

    def test_scheduled_reported(self):
        self.assertEqual(aggregate_publishing_status([
            {"status": "scheduled"}, {"status": "scheduled"},
        ]), "scheduled")

    def test_partial_mix(self):
        self.assertEqual(aggregate_publishing_status([
            {"status": "published"}, {"status": "failed"},
        ]), "partial")

    def test_all_failed(self):
        self.assertEqual(aggregate_publishing_status([
            {"status": "failed"}, {"status": "cancelled"},
        ]), "failed")


# --------------------------------------------------------------------------- #
# post_summary and status refresh                                               #
# --------------------------------------------------------------------------- #


class PostSummary(unittest.TestCase):
    def test_post_summary_from_wrapped_body(self):
        body = {"post": {
            "_id": "P1",
            "status": "PUBLISHED",
            "scheduledFor": "2026-06-15T19:30:00",
            "timezone": "UTC",
            "platforms": [
                {"platform": "tiktok", "accountId": "A", "status": "published",
                 "platformPostUrl": "https://tt/x", "error": "e" * 400},
            ],
        }}
        summary = post_summary(body)
        self.assertEqual(summary["post_id"], "P1")
        self.assertEqual(summary["status"], "published")
        self.assertEqual(len(summary["platforms"][0]["error"]), 300)

    def test_post_summary_existing_post_fallback(self):
        body = {"existingPost": {"_id": "P2", "status": "scheduled"}}
        self.assertEqual(post_summary(body)["post_id"], "P2")

    def test_post_summary_bare(self):
        body = {"_id": "P3", "status": "queued"}
        self.assertEqual(post_summary(body)["post_id"], "P3")


class ReconcileStatus(unittest.TestCase):
    def test_reconcile_terminal_returns_immediately(self):
        summary = {"post_id": "P1", "platforms": [{"status": "published"}]}
        opener = _mock_opener([])  # must not call
        result = reconcile_post_status("k", summary, mode="publish_now", opener=opener,
                                       sleep=lambda s: None, max_attempts=3)
        self.assertEqual(result["platforms"][0]["status"], "published")

    def test_reconcile_polls_until_terminal(self):
        summary = {"post_id": "P1", "platforms": [{"status": "publishing"}]}
        opener = _mock_opener([
            {"match": {"method": "GET", "path_contains": "/posts/P1"},
             "status": 200,
             "body": {"post": {"_id": "P1", "status": "published",
                               "platforms": [{"status": "published"}]}}},
        ])
        result = reconcile_post_status("k", summary, mode="publish_now", opener=opener,
                                       sleep=lambda s: None, max_attempts=3)
        self.assertEqual(result["platforms"][0]["status"], "published")

    def test_reconcile_no_op_for_scheduled(self):
        summary = {"post_id": "P1", "status": "scheduled"}
        opener = _mock_opener([])  # must not call
        result = reconcile_post_status("k", summary, mode="smart_schedule", opener=opener)
        self.assertEqual(result, summary)


# --------------------------------------------------------------------------- #
# HTTP transport                                                                #
# --------------------------------------------------------------------------- #


class Transport(unittest.TestCase):
    def test_missing_api_key(self):
        with self.assertRaises(ZernioError):
            zernio.request_json("GET", "/accounts", "", opener=_mock_opener([]))

    def test_error_sanitization_strips_provider_payload(self):
        opener = _mock_opener([{
            "match": {"method": "GET"},
            "raise_http": 429,
            "body": {"error": "rate limited", "details": {"retryAfterSeconds": 12, "secret": "no"}},
        }])
        try:
            zernio.request_json("GET", "/accounts", "k", opener=opener)
        except ZernioError as e:
            self.assertEqual(e.status, 429)
            self.assertIn("rate limited", str(e))
            self.assertIn("details", e.details)
            self.assertNotIn("secret", e.details.get("details", {}))
        else:
            self.fail("expected ZernioError")

    def test_network_error_yields_zernio_error(self):
        opener = _mock_opener([{"match": {"method": "GET"}, "raise_url": True}])
        with self.assertRaises(ZernioError) as cm:
            zernio.request_json("GET", "/accounts", "k", opener=opener)
        self.assertIn("Could not reach Zernio", str(cm.exception))


# --------------------------------------------------------------------------- #
# publish_video end-to-end (mocked)                                             #
# --------------------------------------------------------------------------- #


class PublishVideo(unittest.TestCase):
    def _write_prod_and_video(self, td: Path) -> tuple[Path, Path]:
        prod = td / "production.json"
        prod.write_text(json.dumps({
            "title": "T", "caption": "cap", "hashtags": ["#a"], "youtube_tags": ["kw1"],
        }), encoding="utf-8")
        video = td / "final.mp4"
        video.write_bytes(b"\x00\x00\x00 ftypisom" + b"x" * 128)
        return prod, video

    def test_publish_video_publish_now_success(self):
        with TemporaryDirectory() as td_str:
            td = Path(td_str)
            prod, video = self._write_prod_and_video(td)
            opener = _mock_opener([
                # presign
                {"match": {"method": "POST", "path_contains": "/media/presign"},
                 "status": 200,
                 "body": {"uploadUrl": "https://upload.example/x",
                          "publicUrl": "https://cdn.example/final.mp4"}},
                # upload PUT
                {"match": {"method": "PUT"}, "status": 200, "body": b""},
                # POST /posts (tiktok)
                {"match": {"method": "POST", "path_contains": "/posts"},
                 "status": 200,
                 "body": {"post": {"_id": "P1", "status": "published",
                                   "platforms": [{"platform": "tiktok", "status": "published"}]}}},
            ])
            result = publish_video(
                "api-key-1", prod, video,
                [{"platform": "tiktok", "account_ids": ["A1"]}],
                mode="publish_now", request_id="clipforge-test-req-123",
                opener=opener,
            )
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["idempotency_key"], "clipforge-test-req-123")
        self.assertEqual(len(result["posts"]), 1)
        self.assertEqual(result["posts"][0]["platform"], "tiktok")
        # §6.2 conformance: the result must be storable as status.publishing
        # verbatim (additionalProperties: false, required status/posts/idempotency_key).
        self.assertEqual(sorted(result.keys()), ["idempotency_key", "posts", "status"])

    def test_publish_video_recovers_on_409_duplicate(self):
        with TemporaryDirectory() as td_str:
            td = Path(td_str)
            prod, video = self._write_prod_and_video(td)
            opener = _mock_opener([
                {"match": {"method": "POST", "path_contains": "/media/presign"},
                 "status": 200,
                 "body": {"uploadUrl": "https://upload.example/x",
                          "publicUrl": "https://cdn.example/final.mp4"}},
                {"match": {"method": "PUT"}, "status": 200, "body": b""},
                # POST /posts -> 409 with existingPostId
                {"match": {"method": "POST", "path_contains": "/posts"},
                 "raise_http": 409,
                 "body": {"error": "duplicate", "details": {"existingPostId": "P9"}}},
                # GET /posts/P9
                {"match": {"method": "GET", "path_contains": "/posts/P9"},
                 "status": 200,
                 "body": {"post": {"_id": "P9", "status": "published",
                                   "platforms": [{"platform": "tiktok", "status": "published"}]}}},
            ])
            result = publish_video(
                "api-key-1", prod, video,
                [{"platform": "tiktok", "account_ids": ["A1"]}],
                mode="publish_now", request_id="clipforge-test-req-456",
                opener=opener,
            )
        self.assertEqual(result["status"], "published")
        self.assertTrue(result["posts"][0].get("deduplicated"))

    def test_publish_video_records_per_platform_failure(self):
        with TemporaryDirectory() as td_str:
            td = Path(td_str)
            prod, video = self._write_prod_and_video(td)
            opener = _mock_opener([
                {"match": {"method": "POST", "path_contains": "/media/presign"},
                 "status": 200,
                 "body": {"uploadUrl": "https://upload.example/x",
                          "publicUrl": "https://cdn.example/final.mp4"}},
                {"match": {"method": "PUT"}, "status": 200, "body": b""},
                # tiktok success
                {"match": {"method": "POST", "path_contains": "/posts"},
                 "status": 200,
                 "body": {"post": {"_id": "P1", "status": "published",
                                   "platforms": [{"platform": "tiktok", "status": "published"}]}}},
                # youtube fails
                {"match": {"method": "POST", "path_contains": "/posts"},
                 "raise_http": 500,
                 "body": {"error": "server exploded"}},
            ])
            result = publish_video(
                "api-key-1", prod, video,
                [
                    {"platform": "tiktok", "account_ids": ["A1"]},
                    {"platform": "youtube", "account_ids": ["Y1"]},
                ],
                mode="publish_now", request_id="clipforge-test-req-789",
                opener=opener,
            )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["posts"][0]["status"], "published")
        self.assertEqual(result["posts"][1]["status"], "failed")
        self.assertIn("server exploded", result["posts"][1]["error"])

    def test_publish_video_missing_media(self):
        with TemporaryDirectory() as td_str:
            td = Path(td_str)
            prod = td / "p.json"
            prod.write_text(json.dumps({"title": "T"}), encoding="utf-8")
            missing = td / "no.mp4"
            with self.assertRaises(ZernioError):
                publish_video(
                    "api-key-1", prod, missing,
                    [{"platform": "tiktok", "account_ids": ["A"]}],
                    opener=_mock_opener([]),
                )


# --------------------------------------------------------------------------- #
# retry / update / cancel                                                       #
# --------------------------------------------------------------------------- #


class PostActions(unittest.TestCase):
    def test_retry(self):
        opener = _mock_opener([
            {"match": {"method": "POST", "path_contains": "/posts/P1/retry"},
             "status": 200,
             "body": {"post": {"_id": "P1", "status": "publishing",
                               "platforms": [{"status": "publishing"}]}}},
        ])
        result = retry_post("k", "P1", opener=opener)
        self.assertEqual(result["status"], "publishing")

    def test_retry_rejects_bad_id(self):
        with self.assertRaises(ZernioError):
            retry_post("k", "bad id with spaces", opener=_mock_opener([]))

    def test_update_requires_scheduled_for_for_manual(self):
        with self.assertRaises(ZernioError):
            update_post("k", "P1", mode="manual_schedule", opener=_mock_opener([]))

    def test_update_publish_now(self):
        opener = _mock_opener([
            {"match": {"method": "PUT", "path_contains": "/posts/P1"},
             "status": 200,
             "body": {"post": {"_id": "P1", "status": "publishing"}}},
        ])
        result = update_post("k", "P1", mode="publish_now", opener=opener)
        self.assertEqual(result["post_id"], "P1")

    def test_cancel(self):
        opener = _mock_opener([
            {"match": {"method": "DELETE", "path_contains": "/posts/P1"},
             "status": 200, "body": b""},
        ])
        result = cancel_post("k", "P1", opener=opener)
        self.assertEqual(result, {"post_id": "P1", "status": "cancelled"})


# --------------------------------------------------------------------------- #
# Workflow-side state helpers                                                   #
# --------------------------------------------------------------------------- #


class WorkflowState(unittest.TestCase):
    def test_write_publishing_state_merges(self):
        with TemporaryDirectory() as td_str:
            path = Path(td_str) / "status.json"
            path.write_text(json.dumps({"job_id": "j1", "state": "complete"}), encoding="utf-8")
            write_publishing_state(path, {"provider": "zernio", "status": "requested"})
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(doc["state"], "complete")
            self.assertEqual(doc["publishing"]["status"], "requested")

    def test_publishing_error_state_preserves_prior(self):
        # §6.2: a publisher failure maps to "failed" (there is no "error" status).
        result = publishing_error_state({"posts": [{"post_id": "P1"}]})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["posts"], [{"post_id": "P1"}])
        self.assertEqual(sorted(result.keys()), ["idempotency_key", "posts", "status"])

    def test_publishing_error_state_empty_prior(self):
        result = publishing_error_state(None)
        self.assertEqual(result, {"status": "failed", "posts": [], "idempotency_key": ""})

    def test_read_json_object_safely(self):
        with TemporaryDirectory() as td_str:
            missing = Path(td_str) / "no.json"
            self.assertIsNone(read_json_object_safely(missing))
            blank = Path(td_str) / "b.json"
            blank.write_text("", encoding="utf-8")
            self.assertIsNone(read_json_object_safely(blank))
            arr = Path(td_str) / "a.json"
            arr.write_text("[]", encoding="utf-8")
            self.assertIsNone(read_json_object_safely(arr))
            obj = Path(td_str) / "o.json"
            obj.write_text('{"a":1}', encoding="utf-8")
            self.assertEqual(read_json_object_safely(obj), {"a": 1})


# --------------------------------------------------------------------------- #
# resolve_manual_dispatch                                                       #
# --------------------------------------------------------------------------- #


class ResolveManual(unittest.TestCase):
    def _make_root(self, td: Path, *, with_queue_post_id: str | None = None,
                    settings: dict | None = None) -> Path:
        (td / "jobs" / "job-x").mkdir(parents=True)
        (td / "jobs" / "job-x" / "production.json").write_text('{"title":"T"}', encoding="utf-8")
        if with_queue_post_id is not None:
            (td / "branding").mkdir()
            (td / "branding" / "zernio_queue.json").write_text(json.dumps({
                "version": 1, "provider": "zernio",
                "items": [{"job_id": "job-x", "status": "scheduled",
                           "post_ids": [with_queue_post_id]}],
            }), encoding="utf-8")
        settings_path = td / "branding" / "zernio_settings.json"
        settings_path.parent.mkdir(exist_ok=True)
        if settings is not None:
            settings_path.write_text(json.dumps(settings), encoding="utf-8")
        return settings_path

    def test_retry_when_prior_post_present(self):
        with TemporaryDirectory() as td_str:
            td = Path(td_str)
            settings_path = self._make_root(td, with_queue_post_id="P9")
            action, mode, tz, b64, post_id = resolve_manual_dispatch(td, "job-x", settings_path)
            self.assertEqual((action, post_id), ("retry", "P9"))
            self.assertEqual((mode, tz, b64), ("", "", ""))

    def test_publish_when_no_prior(self):
        with TemporaryDirectory() as td_str:
            td = Path(td_str)
            settings_path = self._make_root(td, settings={
                "enabled": True, "auto_publish": True, "automatic_mode": "publish_now",
                "target_accounts": {"tiktok": ["A"]},
                "smart_schedule": {"timezone": "UTC"},
            })
            action, mode, tz, b64, post_id = resolve_manual_dispatch(td, "job-x", settings_path)
            self.assertEqual(action, "publish")
            self.assertEqual(mode, "publish_now")
            self.assertEqual(tz, "UTC")
            self.assertEqual(post_id, "")
            self.assertEqual(decode_targets(b64), [{"platform": "tiktok", "account_ids": ["A"]}])

    def test_missing_job_errors(self):
        with TemporaryDirectory() as td_str:
            td = Path(td_str)
            (td / "branding").mkdir()
            (td / "branding" / "zernio_settings.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(SystemExit):
                resolve_manual_dispatch(td, "missing", td / "branding" / "zernio_settings.json")

    def test_missing_settings_errors(self):
        with TemporaryDirectory() as td_str:
            td = Path(td_str)
            self._make_root(td)  # no settings written
            with self.assertRaises(SystemExit):
                resolve_manual_dispatch(td, "job-x", td / "branding" / "zernio_settings.json")

    def test_auto_publish_disabled_errors(self):
        with TemporaryDirectory() as td_str:
            td = Path(td_str)
            settings_path = self._make_root(td, settings={
                "enabled": True, "auto_publish": False,
                "target_accounts": {"tiktok": ["A"]},
            })
            with self.assertRaises(SystemExit):
                resolve_manual_dispatch(td, "job-x", settings_path)


# --------------------------------------------------------------------------- #
# Stage B automatic-publish dispatch (automatic_dispatch)                      #
# --------------------------------------------------------------------------- #


class AutomaticDispatch(unittest.TestCase):
    """Gate matrix for the Stage B auto-publish step.

    A skip must ALWAYS be a clean no-op (never raises) so it can never fail a
    completed Stage B run; only a fully armed configuration dispatches.
    """

    ARMED_SETTINGS = {
        "enabled": True,
        "auto_publish": True,
        "automatic_mode": "publish_now",
        "target_accounts": {"tiktok": ["A"]},
        "smart_schedule": {"timezone": "UTC"},
    }

    def _make_root(self, td: Path, *, state: str | None = "complete",
                   publishing: dict | None = None,
                   settings: dict | None = None,
                   accounts: list[dict] | None = None) -> Path:
        if state is not None:
            job = td / "jobs" / "job-y"
            job.mkdir(parents=True)
            status: dict[str, Any] = {"state": state}
            if publishing is not None:
                status["publishing"] = publishing
            (job / "status.json").write_text(json.dumps(status), encoding="utf-8")
        if settings is not None:
            (td / "branding").mkdir(exist_ok=True)
            (td / "branding" / "zernio_settings.json").write_text(json.dumps(settings), encoding="utf-8")
        if accounts is not None:
            (td / "branding").mkdir(exist_ok=True)
            (td / "branding" / "zernio_accounts.json").write_text(
                json.dumps({"version": 1, "provider": "zernio", "accounts": accounts}), encoding="utf-8")
        return td

    def test_missing_status_skips(self):
        with TemporaryDirectory() as td_str:
            td = self._make_root(Path(td_str), state=None,
                                 settings=self.ARMED_SETTINGS,
                                 accounts=[{"platform": "tiktok", "id": "A"}])
            dispatch, *_rest, reason = automatic_dispatch(td, "job-y", secret_configured=True)
            self.assertEqual(dispatch, "false")
            self.assertIn("status.json", reason)

    def test_incomplete_job_skips(self):
        with TemporaryDirectory() as td_str:
            td = self._make_root(Path(td_str), state="stage_b_running",
                                 settings=self.ARMED_SETTINGS,
                                 accounts=[{"platform": "tiktok", "id": "A"}])
            dispatch, *_rest, reason = automatic_dispatch(td, "job-y", secret_configured=True)
            self.assertEqual(dispatch, "false")
            self.assertIn("complete", reason)

    def test_missing_settings_skips(self):
        with TemporaryDirectory() as td_str:
            td = self._make_root(Path(td_str))  # complete job, no settings
            dispatch, *_rest, reason = automatic_dispatch(td, "job-y", secret_configured=True)
            self.assertEqual(dispatch, "false")
            self.assertIn("zernio_settings.json", reason)

    def test_auto_publish_off_skips(self):
        with TemporaryDirectory() as td_str:
            settings = dict(self.ARMED_SETTINGS, auto_publish=False)
            td = self._make_root(Path(td_str), settings=settings,
                                 accounts=[{"platform": "tiktok", "id": "A"}])
            dispatch, *_rest, reason = automatic_dispatch(td, "job-y", secret_configured=True)
            self.assertEqual(dispatch, "false")
            self.assertIn("disabled", reason)

    def test_invalid_automatic_settings_skip_not_raise(self):
        with TemporaryDirectory() as td_str:
            settings = dict(self.ARMED_SETTINGS,
                            smart_schedule={"timezone": "not/a/zone/;rm -rf /"})
            td = self._make_root(Path(td_str), settings=settings,
                                 accounts=[{"platform": "tiktok", "id": "A"}])
            dispatch, *_rest, reason = automatic_dispatch(td, "job-y", secret_configured=True)
            self.assertEqual(dispatch, "false")
            self.assertIn("invalid", reason)

    def test_missing_secret_skips(self):
        with TemporaryDirectory() as td_str:
            td = self._make_root(Path(td_str), settings=self.ARMED_SETTINGS,
                                 accounts=[{"platform": "tiktok", "id": "A"}])
            dispatch, *_rest, reason = automatic_dispatch(td, "job-y", secret_configured=False)
            self.assertEqual(dispatch, "false")
            self.assertIn("ZERNIO_API_KEY", reason)

    def test_no_active_target_skips(self):
        with TemporaryDirectory() as td_str:
            td = self._make_root(Path(td_str), settings=self.ARMED_SETTINGS,
                                 accounts=[{"platform": "tiktok", "id": "A", "isActive": False}])
            dispatch, *_rest, reason = automatic_dispatch(td, "job-y", secret_configured=True)
            self.assertEqual(dispatch, "false")
            self.assertIn("active", reason)

    def test_missing_accounts_snapshot_skips(self):
        with TemporaryDirectory() as td_str:
            td = self._make_root(Path(td_str), settings=self.ARMED_SETTINGS)  # no accounts file
            dispatch, *_rest, reason = automatic_dispatch(td, "job-y", secret_configured=True)
            self.assertEqual(dispatch, "false")
            self.assertIn("active", reason)

    def test_fully_armed_dispatches(self):
        with TemporaryDirectory() as td_str:
            td = self._make_root(Path(td_str), settings=self.ARMED_SETTINGS,
                                 accounts=[{"platform": "tiktok", "id": "A", "username": "u"},
                                           {"platform": "tiktok", "id": "B"}])
            dispatch, mode, tz, targets_json, request_id, reason = automatic_dispatch(
                td, "job-y", secret_configured=True)
            self.assertEqual(dispatch, "true")
            self.assertEqual(mode, "publish_now")
            self.assertEqual(tz, "UTC")
            self.assertEqual(json.loads(targets_json), [{"platform": "tiktok", "account_ids": ["A"]}])
            self.assertTrue(request_id.startswith("clipforge-auto-job-y-"))
            self.assertIn("armed", reason)

    def test_inactive_selection_filtered_out_of_targets(self):
        with TemporaryDirectory() as td_str:
            settings = dict(self.ARMED_SETTINGS,
                            target_accounts={"tiktok": ["A", "B"]})
            td = self._make_root(Path(td_str), settings=settings,
                                 accounts=[{"platform": "tiktok", "id": "A"},
                                           {"platform": "tiktok", "id": "B", "needsReconnection": True}])
            dispatch, _mode, _tz, targets_json, _rid, _reason = automatic_dispatch(
                td, "job-y", secret_configured=True)
            self.assertEqual(dispatch, "true")
            self.assertEqual(json.loads(targets_json), [{"platform": "tiktok", "account_ids": ["A"]}])

    def test_failed_attempt_reuses_idempotency_key(self):
        with TemporaryDirectory() as td_str:
            td = self._make_root(
                Path(td_str), settings=self.ARMED_SETTINGS,
                accounts=[{"platform": "tiktok", "id": "A"}],
                publishing={"status": "failed", "idempotency_key": "clipforge-auto-job-y-abc12345", "posts": []})
            dispatch, _mode, _tz, _targets, request_id, _reason = automatic_dispatch(
                td, "job-y", secret_configured=True)
            self.assertEqual(dispatch, "true")
            self.assertEqual(request_id, "clipforge-auto-job-y-abc12345")

    def test_non_failed_attempt_mints_fresh_key(self):
        with TemporaryDirectory() as td_str:
            td = self._make_root(
                Path(td_str), settings=self.ARMED_SETTINGS,
                accounts=[{"platform": "tiktok", "id": "A"}],
                publishing={"status": "published", "idempotency_key": "clipforge-auto-job-y-abc12345", "posts": []})
            dispatch, _mode, _tz, _targets, request_id, _reason = automatic_dispatch(
                td, "job-y", secret_configured=True)
            self.assertEqual(dispatch, "true")
            self.assertNotEqual(request_id, "clipforge-auto-job-y-abc12345")
            self.assertTrue(request_id.startswith("clipforge-auto-job-y-"))


# --------------------------------------------------------------------------- #
# build_platform_payload                                                        #
# --------------------------------------------------------------------------- #


class Payload(unittest.TestCase):
    def test_youtube_payload_uses_title_and_tags(self):
        production = {"title": "MyTitle", "caption": "cap", "hashtags": ["#a"], "youtube_tags": ["kw1"]}
        payload = build_platform_payload(
            production, "youtube", ["Y1"], "https://cdn/x.mp4",
            mode="publish_now",
        )
        self.assertEqual(payload["title"], "MyTitle")
        self.assertEqual(payload["tags"], ["kw1"])
        self.assertTrue(payload["publishNow"])
        self.assertEqual(payload["platforms"], [{"platform": "youtube", "accountId": "Y1"}])

    def test_tiktok_payload_omits_title(self):
        payload = build_platform_payload(
            {"title": "T", "caption": "cap"}, "tiktok", ["A1"], "https://cdn/x.mp4",
            mode="publish_now",
        )
        self.assertNotIn("title", payload)
        self.assertNotIn("tags", payload)

    def test_instagram_payload_posts_reel_and_story(self):
        # bug-52: every Instagram publish emits BOTH a Reel entry (no
        # contentType — a single video auto-publishes as a Reel per the Zernio
        # docs) and a Story entry (contentType="story"), with no user
        # selection between content types.
        payload = build_platform_payload(
            {"title": "T", "caption": "cap", "hashtags": ["#a"]},
            "instagram", ["IG1", "IG2"], "https://cdn/x.mp4",
            mode="publish_now",
        )
        self.assertNotIn("title", payload)
        self.assertNotIn("tags", payload)
        self.assertTrue(payload["publishNow"])
        self.assertEqual(
            payload["platforms"],
            [
                {"platform": "instagram", "accountId": "IG1"},
                {"platform": "instagram", "accountId": "IG1", "contentType": "story"},
                {"platform": "instagram", "accountId": "IG2"},
                {"platform": "instagram", "accountId": "IG2", "contentType": "story"},
            ],
        )
        # Media stays a direct CDN URL (presign publicUrl) — never a
        # Drive/Dropbox/OneDrive/iCloud share link.
        self.assertEqual(payload["mediaItems"], [{"type": "video", "url": "https://cdn/x.mp4"}])

    def test_instagram_payload_scheduled_carries_both_content_types(self):
        payload = build_platform_payload(
            {"title": "T"}, "instagram", ["IG1"], "https://cdn/x.mp4",
            mode="manual_schedule", scheduled_for="2026-09-01T19:30:00", timezone="UTC",
        )
        self.assertEqual(payload["scheduledFor"], "2026-09-01T19:30:00")
        kinds = [entry.get("contentType", "reel") for entry in payload["platforms"]]
        self.assertEqual(kinds, ["reel", "story"])

    def test_schedule_requires_scheduled_for(self):
        with self.assertRaises(ZernioError):
            build_platform_payload({"title": "T"}, "tiktok", ["A"], "https://cdn/x.mp4",
                                   mode="manual_schedule")

    def test_bad_media_url(self):
        with self.assertRaises(ZernioError):
            build_platform_payload({"title": "T"}, "tiktok", ["A"], "ftp://x")

    def test_empty_account_ids(self):
        with self.assertRaises(ZernioError):
            build_platform_payload({"title": "T"}, "tiktok", ["  "], "https://cdn/x.mp4")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
