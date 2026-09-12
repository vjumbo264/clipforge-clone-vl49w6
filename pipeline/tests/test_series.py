"""Offline tests for pipeline/plan/series.py (series continuation derivation).

Mirrors the derivation semantics of the legacy tool and its legacy test
(_legacy/scripts/test_series_mode.py) against the NEW nested §7.1/§7.3
contracts, plus the cases unique to this port (music carry-forward, mixed
plan shapes, eligibility edges).
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pipeline.plan.series import (
    SeriesDerivationError,
    derive_next_part,
    main as series_main,
)


def _request(
    job_id: str,
    *,
    enabled: bool = True,
    series_id: str = "series-123",
    source_job_id: str = "series-123-p1",
    part: int = 1,
    start_seconds: int = 0,
    mode: str = "manual",
    music: dict | None = None,
    source: dict | None = None,
    options: dict | None = None,
) -> dict:
    return {
        "version": 2,
        "job_id": job_id,
        "source": source if source is not None else {"kind": "url", "value": "https://example.com/v"},
        "options": options
        if options is not None
        else {
            "whisper_model": "base",
            "language": "auto",
            "target_duration_seconds": 120,
            "focus": "",
            "enable_vision_assist": True,
        },
        "mode": mode,
        "series": {
            "enabled": enabled,
            "series_id": series_id,
            "source_job_id": source_job_id,
            "part": part,
            "start_seconds": start_seconds,
            "context": "",
        },
        "music": music if music is not None else {"ref": "", "source": "none"},
        "saved_at_epoch": 1,
    }


def _plan_nested(
    job_id: str,
    *,
    series_id: str = "series-123",
    part: int = 1,
    start: int = 0,
    end: int = 120,
    is_final: bool = False,
    summary: str = "The story so far.",
) -> dict:
    return {
        "version": 2,
        "job_id": job_id,
        "video_duration_seconds": 600,
        "target_total_duration_seconds": 120,
        "cuts": [{"start_seconds": start, "end_seconds": end, "voiceover_text": "Hello."}],
        "series": {
            "series_id": series_id,
            "part": part,
            "start_seconds": start,
            "end_seconds": end,
            "is_final": is_final,
            "summary": summary,
        },
    }


def _plan_flat(
    job_id: str,
    *,
    series_id: str = "series-123",
    part: int = 1,
    start: int = 0,
    end: int = 120,
    is_final: bool = False,
    summary: str = "The story so far.",
) -> dict:
    return {
        "version": 2,
        "job_id": job_id,
        "video_duration_seconds": 600,
        "target_total_duration_seconds": 120,
        "cuts": [{"start_seconds": start, "end_seconds": end, "voiceover_text": "Hello."}],
        "series_id": series_id,
        "series_part": part,
        "series_start_seconds": start,
        "series_end_seconds": end,
        "series_final": is_final,
        "series_summary": summary,
    }


def _write_job(root: Path, job_id: str, request: dict | None, plan: dict | None) -> None:
    job_dir = root / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    if request is not None:
        (job_dir / "stage-a-request.json").write_text(json.dumps(request), encoding="utf-8")
    if plan is not None:
        (job_dir / "production.json").write_text(json.dumps(plan), encoding="utf-8")


class DeriveNextPartTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "jobs").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_request_is_not_eligible(self) -> None:
        _write_job(self.root, "series-123-p1", None, _plan_nested("series-123-p1"))
        self.assertEqual(derive_next_part(self.root, "series-123-p1"), {"continue": False})

    def test_missing_plan_is_not_eligible(self) -> None:
        _write_job(self.root, "series-123-p1", _request("series-123-p1"), None)
        self.assertEqual(derive_next_part(self.root, "series-123-p1"), {"continue": False})

    def test_non_series_request_is_not_eligible(self) -> None:
        _write_job(
            self.root,
            "manual-1",
            _request("manual-1", enabled=False, series_id="", source_job_id="", part=0),
            _plan_nested("manual-1", series_id="series-123"),
        )
        self.assertEqual(derive_next_part(self.root, "manual-1"), {"continue": False})

    def test_final_part_stops_the_chain(self) -> None:
        _write_job(
            self.root,
            "series-123-p1",
            _request("series-123-p1"),
            _plan_nested("series-123-p1", is_final=True),
        )
        self.assertEqual(derive_next_part(self.root, "series-123-p1"), {"continue": False})

    def test_plan_without_series_id_is_not_eligible(self) -> None:
        _write_job(
            self.root,
            "series-123-p1",
            _request("series-123-p1"),
            _plan_nested("series-123-p1", series_id=""),
        )
        self.assertEqual(derive_next_part(self.root, "series-123-p1"), {"continue": False})

    def test_invalid_part_metadata_raises(self) -> None:
        plan = _plan_nested("series-123-p1")
        plan["series"]["part"] = "one"
        _write_job(self.root, "series-123-p1", _request("series-123-p1"), plan)
        with self.assertRaises(SeriesDerivationError):
            derive_next_part(self.root, "series-123-p1")

    def test_missing_source_job_id_raises(self) -> None:
        _write_job(
            self.root,
            "series-123-p1",
            _request("series-123-p1", source_job_id=""),
            _plan_nested("series-123-p1"),
        )
        with self.assertRaises(SeriesDerivationError):
            derive_next_part(self.root, "series-123-p1")

    def test_history_gap_raises(self) -> None:
        # Part 2 completed but part 1 has no persisted plan.
        _write_job(
            self.root,
            "series-123-p2",
            _request("series-123-p2", part=2, start_seconds=120),
            _plan_nested("series-123-p2", part=2, start=120, end=240),
        )
        with self.assertRaises(SeriesDerivationError):
            derive_next_part(self.root, "series-123-p2")

    def test_unexpected_later_part_raises(self) -> None:
        _write_job(
            self.root,
            "series-123-p1",
            _request("series-123-p1"),
            _plan_nested("series-123-p1", part=1, end=120),
        )
        _write_job(
            self.root,
            "series-123-p3",
            _request("series-123-p3", part=3, start_seconds=240),
            _plan_nested("series-123-p3", part=3, start=240, end=360),
        )
        with self.assertRaises(SeriesDerivationError):
            derive_next_part(self.root, "series-123-p1")

    def test_continuation_payload_nested_plan(self) -> None:
        _write_job(
            self.root,
            "series-123-p1",
            _request("series-123-p1"),
            _plan_nested("series-123-p1", end=120, summary="Part one recap."),
        )
        payload = derive_next_part(self.root, "series-123-p1")
        self.assertTrue(payload["continue"])
        self.assertEqual(payload["job_id"], "series-123-p2")
        request = payload["request"]
        self.assertEqual(request["mode"], "manual")
        self.assertEqual(request["source"]["kind"], "url")
        self.assertEqual(request["source"]["value"], "https://example.com/v")
        series = request["series"]
        self.assertTrue(series["enabled"])
        self.assertEqual(series["series_id"], "series-123")
        self.assertEqual(series["source_job_id"], "series-123-p1")
        self.assertEqual(series["part"], 2)
        self.assertEqual(series["start_seconds"], 120)
        self.assertEqual(series["context"], "Prior events (Part 1): Part one recap.")
        self.assertEqual(request["options"]["focus"], "")

    def test_continuation_payload_flat_plan(self) -> None:
        # Legacy flat series_* production.json must derive identically.
        _write_job(
            self.root,
            "series-123-p1",
            _request("series-123-p1"),
            _plan_flat("series-123-p1", end=90, summary="Flat recap."),
        )
        payload = derive_next_part(self.root, "series-123-p1")
        self.assertTrue(payload["continue"])
        self.assertEqual(payload["request"]["series"]["start_seconds"], 90)
        self.assertEqual(payload["request"]["series"]["context"], "Prior events (Part 1): Flat recap.")

    def test_context_chains_prior_summaries_in_order(self) -> None:
        _write_job(
            self.root,
            "series-123-p1",
            _request("series-123-p1"),
            _plan_nested("series-123-p1", part=1, end=120, summary="First."),
        )
        _write_job(
            self.root,
            "series-123-p2",
            _request("series-123-p2", part=2, start_seconds=120),
            _plan_nested("series-123-p2", part=2, start=120, end=240, summary="Second."),
        )
        payload = derive_next_part(self.root, "series-123-p2")
        self.assertEqual(payload["job_id"], "series-123-p3")
        self.assertEqual(payload["request"]["series"]["start_seconds"], 240)
        self.assertEqual(
            payload["request"]["series"]["context"],
            "Prior events (Part 1): First.\nPrior events (Part 2): Second.",
        )

    def test_context_capped_at_8000_chars(self) -> None:
        _write_job(
            self.root,
            "series-123-p1",
            _request("series-123-p1"),
            _plan_nested("series-123-p1", end=120, summary="x" * 9000),
        )
        payload = derive_next_part(self.root, "series-123-p1")
        self.assertLessEqual(len(payload["request"]["series"]["context"]), 8000)

    def test_context_falls_back_when_no_summaries(self) -> None:
        _write_job(
            self.root,
            "series-123-p1",
            _request("series-123-p1"),
            _plan_nested("series-123-p1", end=120, summary=""),
        )
        payload = derive_next_part(self.root, "series-123-p1")
        self.assertEqual(payload["request"]["series"]["context"], "(No prior summaries.)")

    def test_long_series_id_rejected(self) -> None:
        long_id = "s" * 119  # + "-p2" would exceed 120
        _write_job(
            self.root,
            f"{long_id}-p1",
            _request(f"{long_id}-p1", series_id=long_id, source_job_id=f"{long_id}-p1"),
            _plan_nested(f"{long_id}-p1", series_id=long_id),
        )
        with self.assertRaises(SeriesDerivationError):
            derive_next_part(self.root, f"{long_id}-p1")

    def test_torrent_file_index_carries_forward(self) -> None:
        _write_job(
            self.root,
            "series-123-p1",
            _request(
                "series-123-p1",
                source={"kind": "torrent_file", "value": "path:jobs/series-123-p1/source.torrent", "torrent_file_index": "3"},
            ),
            _plan_nested("series-123-p1"),
        )
        payload = derive_next_part(self.root, "series-123-p1")
        self.assertEqual(payload["request"]["source"]["torrent_file_index"], "3")

    def test_library_music_carries_forward(self) -> None:
        _write_job(
            self.root,
            "series-123-p1",
            _request(
                "series-123-p1",
                music={"ref": "audio-library/theme.mp3", "source": "explicit_library"},
            ),
            _plan_nested("series-123-p1"),
        )
        payload = derive_next_part(self.root, "series-123-p1")
        self.assertEqual(
            payload["request"]["music"],
            {"ref": "audio-library/theme.mp3", "source": "explicit_library"},
        )

    def test_default_music_carries_forward_as_default(self) -> None:
        _write_job(
            self.root,
            "series-123-p1",
            _request("series-123-p1", music={"ref": "", "source": "default"}),
            _plan_nested("series-123-p1"),
        )
        payload = derive_next_part(self.root, "series-123-p1")
        self.assertEqual(payload["request"]["music"], {"ref": "", "source": "default"})

    def test_one_off_upload_falls_back_to_default(self) -> None:
        _write_job(
            self.root,
            "series-123-p1",
            _request(
                "series-123-p1",
                music={"ref": "jobs/series-123-p1/music.mp3", "source": "job_upload"},
            ),
            _plan_nested("series-123-p1"),
        )
        payload = derive_next_part(self.root, "series-123-p1")
        self.assertEqual(payload["request"]["music"], {"ref": "", "source": "default"})

    def test_manual_mode_carries_forward(self) -> None:
        _write_job(
            self.root,
            "series-123-p1",
            _request("series-123-p1", mode="manual"),
            _plan_nested("series-123-p1"),
        )
        payload = derive_next_part(self.root, "series-123-p1")
        self.assertEqual(payload["request"]["mode"], "manual")

    def test_unparseable_other_plan_is_ignored(self) -> None:
        _write_job(
            self.root,
            "series-123-p1",
            _request("series-123-p1"),
            _plan_nested("series-123-p1"),
        )
        other = self.root / "jobs" / "unrelated"
        other.mkdir()
        (other / "production.json").write_text("{not json", encoding="utf-8")
        payload = derive_next_part(self.root, "series-123-p1")
        self.assertTrue(payload["continue"])

    def test_cli_usage_error(self) -> None:
        self.assertEqual(series_main(["only-one-arg"]), 2)

    def test_cli_success_and_failure(self) -> None:
        _write_job(
            self.root,
            "series-123-p1",
            _request("series-123-p1"),
            _plan_nested("series-123-p1"),
        )
        self.assertEqual(series_main([str(self.root), "series-123-p1"]), 0)
        # A part 2 with NO persisted part 1 -> history gap -> hard error (exit 1).
        _write_job(
            self.root,
            "series-999-p2",
            _request("series-999-p2", series_id="series-999", source_job_id="series-999-p1", part=2, start_seconds=120),
            _plan_nested("series-999-p2", series_id="series-999", part=2, start=120, end=240),
        )
        self.assertEqual(series_main([str(self.root), "series-999-p2"]), 1)


if __name__ == "__main__":
    unittest.main()
