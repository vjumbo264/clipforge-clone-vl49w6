#!/usr/bin/env python3
"""Regression tests for safe Zernio workflow transport and failure persistence."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from zernio_targets import TargetValidationError, automatic_fields, decode_targets, encode_targets, serialize_targets
from zernio_workflow_state import publishing_error_state, read_json_object_safely


MULTI_TARGETS = [
    {"platform": "tiktok", "account_ids": ["tt-one", "tt-two"]},
    {"platform": "youtube", "account_ids": ["yt-one", "yt-two", "yt-three"]},
]


def test_target_serialization_preserves_json_quotes_and_multiple_accounts() -> None:
    serialized = serialize_targets(MULTI_TARGETS)
    assert serialized == (
        '[{"platform":"tiktok","account_ids":["tt-one","tt-two"]},'
        '{"platform":"youtube","account_ids":["yt-one","yt-two","yt-three"]}]'
    )
    assert json.loads(serialized) == MULTI_TARGETS


def test_target_transport_round_trip_preserves_tiktok_and_youtube_groups() -> None:
    encoded = encode_targets(MULTI_TARGETS)
    assert '"' not in encoded
    assert decode_targets(encoded) == MULTI_TARGETS


def test_empty_and_malformed_targets_are_rejected_at_the_transport_boundary() -> None:
    for value in ([], [{"platform": "tiktok", "account_ids": []}], [{"platform": "other", "account_ids": ["x"]}]):
        try:
            serialize_targets(value)
        except TargetValidationError:
            pass
        else:
            raise AssertionError(f"expected target validation to reject {value!r}")


def test_automatic_fields_keep_valid_json_through_shell_safe_transport() -> None:
    settings = {
        "enabled": True,
        "auto_publish": True,
        "automatic_mode": "smart_schedule",
        "target_accounts": {"tiktok": ["tt-one", "tt-two"], "youtube": ["yt-one"]},
        "smart_schedule": {"timezone": "Europe/London"},
    }
    skip, mode, timezone, encoded = automatic_fields(settings)
    assert (skip, mode, timezone) == ("false", "smart_schedule", "Europe/London")
    assert decode_targets(encoded) == [
        {"platform": "tiktok", "account_ids": ["tt-one", "tt-two"]},
        {"platform": "youtube", "account_ids": ["yt-one"]},
    ]


def test_unavailable_result_records_clean_error_without_losing_prior_context() -> None:
    prior = {"post_id": "preserve-me", "mode": "smart_schedule", "status": "requested"}
    error = publishing_error_state(prior)
    assert error["status"] == "error"
    assert error["post_id"] == "preserve-me"
    assert error["mode"] == "smart_schedule"
    assert error["result_available"] is False
    assert "earlier publishing step" in error["error"]


def test_result_reader_treats_missing_empty_malformed_and_non_object_as_unavailable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert read_json_object_safely(root / "missing.json") is None
        empty = root / "empty.json"; empty.write_text("\n", encoding="utf-8")
        malformed = root / "malformed.json"; malformed.write_text("{", encoding="utf-8")
        array = root / "array.json"; array.write_text("[]", encoding="utf-8")
        valid = root / "valid.json"; valid.write_text('{"status":"scheduled"}', encoding="utf-8")
        assert read_json_object_safely(empty) is None
        assert read_json_object_safely(malformed) is None
        assert read_json_object_safely(array) is None
        assert read_json_object_safely(valid) == {"status": "scheduled"}


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"zernio recovery tests passed ({len(tests)} tests)")
