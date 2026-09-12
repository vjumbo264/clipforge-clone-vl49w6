#!/usr/bin/env python3
"""Offline tests for persistent Zernio queue state and IANA-timezone scheduling."""
from __future__ import annotations

from datetime import UTC, datetime

from zernio_state import (
    active_accounts,
    default_queue,
    plan_smart_schedule,
    remove_queue_item,
    upsert_queue_item,
)


SETTINGS = {
    "smart_schedule": {
        "timezone": "Europe/London",
        "interval_hours": 48,
        "preferred_time": "19:30",
        "queue_depth": 4,
        "start_mode": "next_available",
        "custom_start": "",
    }
}


def test_next_available_slot_uses_local_iana_time() -> None:
    planned = plan_smart_schedule(
        SETTINGS,
        default_queue(),
        now=datetime(2026, 8, 22, 10, 0, tzinfo=UTC),
    )
    assert planned["timezone"] == "Europe/London"
    assert planned["scheduled_for"] == "2026-08-22T19:30:00"
    assert planned["scheduled_at_utc"].endswith("Z")


def test_queue_and_provider_slots_delay_without_collision() -> None:
    queue = upsert_queue_item(default_queue(), {
        "job_id": "job-1", "status": "scheduled",
        "scheduled_for": "2026-08-22T19:30:00", "timezone": "Europe/London",
    })
    planned = plan_smart_schedule(
        SETTINGS,
        queue,
        external_posts=[{
            "status": "scheduled", "scheduled_for": "2026-08-24T19:30:00", "timezone": "Europe/London"
        }],
        now=datetime(2026, 8, 22, 9, 0, tzinfo=UTC),
    )
    # job-1 reserves the configured 48-hour cadence; the provider's external
    # post occupies that first next slot, so the next valid local slot is later.
    assert planned["scheduled_for"] == "2026-08-26T19:30:00"


def test_one_hour_cadence_advances_after_queue_and_provider_collisions() -> None:
    settings = {"smart_schedule": dict(SETTINGS["smart_schedule"], interval_hours=1)}
    queue = upsert_queue_item(default_queue(), {
        "job_id": "hourly-1", "status": "scheduled",
        "scheduled_for": "2026-08-22T19:30:00", "timezone": "Europe/London",
    })
    planned = plan_smart_schedule(
        settings,
        queue,
        external_posts=[{
            "status": "scheduled", "scheduled_for": "2026-08-22T20:30:00", "timezone": "Europe/London",
        }],
        now=datetime(2026, 8, 22, 18, 31, tzinfo=UTC),
    )
    assert planned["scheduled_for"] == "2026-08-22T21:30:00"


def test_legacy_interval_days_migrates_without_changing_cadence() -> None:
    legacy = {"smart_schedule": dict(SETTINGS["smart_schedule"], interval_days=2)}
    legacy["smart_schedule"].pop("interval_hours")
    queue = upsert_queue_item(default_queue(), {
        "job_id": "legacy", "status": "scheduled",
        "scheduled_for": "2026-08-22T19:30:00", "timezone": "Europe/London",
    })
    planned = plan_smart_schedule(legacy, queue, now=datetime(2026, 8, 22, 9, 0, tzinfo=UTC))
    assert planned["scheduled_for"] == "2026-08-24T19:30:00"


def test_custom_start_and_queue_upsert_are_job_isolated() -> None:
    settings = {"smart_schedule": dict(SETTINGS["smart_schedule"], start_mode="custom", custom_start="2026-08-23T08:15")}
    queue = upsert_queue_item(default_queue(), {"job_id": "one", "status": "scheduled", "scheduled_for": "2026-08-20T19:30:00", "timezone": "Europe/London"})
    queue = upsert_queue_item(queue, {"job_id": "two", "status": "scheduled", "scheduled_for": "2026-08-22T19:30:00", "timezone": "Europe/London"})
    assert len(queue["items"]) == 2
    queue = upsert_queue_item(queue, {"job_id": "one", "status": "published"})
    assert len(queue["items"]) == 2
    assert [item for item in queue["items"] if item["job_id"] == "one"][0]["status"] == "published"
    planned = plan_smart_schedule(settings, remove_queue_item(queue, "two"), now=datetime(2026, 8, 22, 8, 0, tzinfo=UTC))
    assert planned["scheduled_for"] == "2026-08-23T08:15:00"


def test_only_active_connected_accounts_are_selectable() -> None:
    accounts = active_accounts({"accounts": [
        {"_id": "tt-live", "platform": "tiktok", "isActive": True, "enabled": True},
        {"_id": "yt-disabled", "platform": "youtube", "isActive": True, "enabled": False},
        {"_id": "tt-reconnect", "platform": "tiktok", "isActive": True, "needsReconnection": True},
        {"_id": "yt-live", "platform": "youtube", "isActive": True},
    ]})
    assert [a["id"] for a in accounts["tiktok"]] == ["tt-live"]
    assert [a["id"] for a in accounts["youtube"]] == ["yt-live"]


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"zernio state tests passed ({len(tests)} tests)")
