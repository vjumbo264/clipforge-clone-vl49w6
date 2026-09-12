"""Tests for pipeline.stage_b.series_reconcile (bug-56).

Covers every branch the reconciler needs to be trustworthy inside
.github/workflows/stage-b.yml:

* a series job whose production.json has the wrong part number auto-corrects;
* the same for a wrong series_id or wrong start_seconds;
* a completely correct series production.json is unchanged;
* a non-series job is never touched, no matter what the plan looks like;
* fields the AI legitimately owns (end_seconds, is_final, summary, cuts) are
  never touched, only the three durable-request-derived fields;
* legacy flat ``series_*`` sibling fields also get corrected when they were
  the only shape the AI submitted;
* malformed / missing request or plan is a no-op, not a crash.
"""
from __future__ import annotations

import copy
import unittest

from pipeline.stage_b.series_reconcile import (
    ReconciliationResult,
    format_log_line,
    reconcile_series_metadata,
)


def _request(**overrides):
    doc = {
        "version": 2,
        "job_id": "series-123-p2",
        "mode": "manual",
        "series": {
            "enabled": True,
            "series_id": "series-123",
            "source_job_id": "series-123-p1",
            "part": 2,
            "start_seconds": 120,
            "context": "Prior events (Part 1): The setup.",
        },
    }
    if "series" in overrides:
        doc["series"] = {**doc["series"], **overrides["series"]}
    for k, v in overrides.items():
        if k != "series":
            doc[k] = v
    return doc


def _plan_nested(**series_overrides):
    plan = {
        "version": 2,
        "job_id": "series-123-p2",
        "video_duration_seconds": 600,
        "target_total_duration_seconds": 120,
        "cuts": [{"start_seconds": 120, "end_seconds": 240, "voiceover_text": "Hello."}],
        "series": {
            "series_id": "series-123",
            "part": 2,
            "start_seconds": 120,
            "end_seconds": 240,
            "is_final": False,
            "summary": "Part 2 recap.",
        },
    }
    plan["series"].update(series_overrides)
    return plan


def _plan_flat(**overrides):
    plan = {
        "version": 2,
        "job_id": "series-123-p2",
        "video_duration_seconds": 600,
        "target_total_duration_seconds": 120,
        "cuts": [{"start_seconds": 120, "end_seconds": 240, "voiceover_text": "Hello."}],
        "series_id": "series-123",
        "series_part": 2,
        "series_start_seconds": 120,
        "series_end_seconds": 240,
        "series_final": False,
        "series_summary": "Part 2 recap.",
    }
    plan.update(overrides)
    return plan


class WrongPartNumberAutoCorrects(unittest.TestCase):
    """The reported incident: Part 2 job submitted with part: 1."""

    def test_nested_shape_part_autocorrected(self):
        plan = _plan_nested(part=1, start_seconds=120)  # wrong part, right start
        result = reconcile_series_metadata(plan, _request())
        self.assertIsInstance(result, ReconciliationResult)
        self.assertEqual(len(result.changes), 1)
        self.assertEqual(result.changes[0], ("part", 1, 2))
        self.assertEqual(plan["series"]["part"], 2)
        # untouched fields remain the AI's choice
        self.assertEqual(plan["series"]["end_seconds"], 240)
        self.assertEqual(plan["series"]["summary"], "Part 2 recap.")

    def test_wrong_series_id_autocorrected(self):
        plan = _plan_nested(series_id="series-999")
        result = reconcile_series_metadata(plan, _request())
        self.assertEqual(len(result.changes), 1)
        self.assertEqual(result.changes[0], ("series_id", "series-999", "series-123"))
        self.assertEqual(plan["series"]["series_id"], "series-123")

    def test_wrong_start_seconds_autocorrected(self):
        plan = _plan_nested(start_seconds=0)
        result = reconcile_series_metadata(plan, _request())
        self.assertEqual(len(result.changes), 1)
        self.assertEqual(result.changes[0], ("start_seconds", 0, 120))
        self.assertEqual(plan["series"]["start_seconds"], 120)

    def test_all_three_wrong_at_once(self):
        plan = _plan_nested(series_id="series-999", part=1, start_seconds=0)
        result = reconcile_series_metadata(plan, _request())
        # order is the module's declared safe-field order
        self.assertEqual(
            [c[0] for c in result.changes],
            ["series_id", "part", "start_seconds"],
        )
        self.assertEqual(plan["series"]["series_id"], "series-123")
        self.assertEqual(plan["series"]["part"], 2)
        self.assertEqual(plan["series"]["start_seconds"], 120)


class NoChangesWhenCorrect(unittest.TestCase):
    def test_correct_plan_untouched(self):
        plan = _plan_nested()
        before = copy.deepcopy(plan)
        result = reconcile_series_metadata(plan, _request())
        self.assertEqual(result.changes, [])
        self.assertEqual(plan, before)


class NonSeriesJobUntouched(unittest.TestCase):
    def test_non_series_request_never_reconciles(self):
        # A non-series request must not cause the reconciler to touch the
        # plan even when it happens to contain a series block from some
        # unrelated authoring mistake.
        request = {"version": 2, "job_id": "one-off", "mode": "manual", "series": {"enabled": False}}
        plan = _plan_nested(part=99, series_id="totally-wrong")
        before = copy.deepcopy(plan)
        result = reconcile_series_metadata(plan, request)
        self.assertEqual(result.changes, [])
        self.assertEqual(plan, before)

    def test_missing_request_never_reconciles(self):
        plan = _plan_nested(part=1)
        before = copy.deepcopy(plan)
        result = reconcile_series_metadata(plan, None)
        self.assertEqual(result.changes, [])
        self.assertEqual(plan, before)

    def test_missing_series_key_never_reconciles(self):
        request = {"version": 2, "job_id": "one-off"}
        plan = _plan_nested(part=1)
        before = copy.deepcopy(plan)
        result = reconcile_series_metadata(plan, request)
        self.assertEqual(result.changes, [])
        self.assertEqual(plan, before)


class AiOwnedFieldsNeverTouched(unittest.TestCase):
    """end_seconds / is_final / summary / cuts are the AI's creative choices."""

    def test_end_seconds_mismatch_not_reconciled(self):
        # The reconciler must not silently normalize end_seconds even if the
        # request happened to have one — end_seconds is the AI's choice.
        request = _request()
        request["series"]["end_seconds"] = 999
        plan = _plan_nested()  # end_seconds=240
        result = reconcile_series_metadata(plan, request)
        self.assertEqual(result.changes, [])
        self.assertEqual(plan["series"]["end_seconds"], 240)

    def test_summary_and_cuts_untouched(self):
        plan = _plan_nested(part=1)
        result = reconcile_series_metadata(plan, _request())
        self.assertEqual(result.changes[0][0], "part")
        # everything the AI owns still exactly as authored
        self.assertEqual(plan["series"]["summary"], "Part 2 recap.")
        self.assertEqual(plan["series"]["is_final"], False)
        self.assertEqual(plan["series"]["end_seconds"], 240)
        self.assertEqual(plan["cuts"], [{"start_seconds": 120, "end_seconds": 240, "voiceover_text": "Hello."}])
        self.assertEqual(plan["target_total_duration_seconds"], 120)


class LegacyFlatShapeReconciled(unittest.TestCase):
    def test_flat_only_plan_gets_both_shapes_corrected(self):
        plan = _plan_flat(series_part=1)  # wrong flat part number, no nested block
        result = reconcile_series_metadata(plan, _request())
        self.assertEqual(len(result.changes), 1)
        self.assertEqual(result.changes[0], ("part", 1, 2))
        # nested shape gets created (canonical), flat sibling also rewritten
        self.assertEqual(plan["series"]["part"], 2)
        self.assertEqual(plan["series_part"], 2)

    def test_mixed_shape_flat_sibling_updated_when_wrong(self):
        # nested wins for reads, but if a flat sibling ALSO existed and was
        # wrong, the reconciler must not leave a stale flat value behind that
        # a downstream tool consuming the flat shape would then trust.
        plan = _plan_nested(part=1)
        plan["series_part"] = 1  # legacy flat sibling also wrong
        result = reconcile_series_metadata(plan, _request())
        self.assertEqual(result.changes[0], ("part", 1, 2))
        self.assertEqual(plan["series"]["part"], 2)
        self.assertEqual(plan["series_part"], 2)


class MalformedInputsAreSafeNoOps(unittest.TestCase):
    def test_non_dict_plan_returns_empty_result(self):
        result = reconcile_series_metadata("not a plan", _request())  # type: ignore[arg-type]
        self.assertEqual(result.changes, [])

    def test_request_with_bogus_part_is_skipped(self):
        request = _request(series={"enabled": True, "series_id": "series-123",
                                    "source_job_id": "series-123-p1",
                                    "part": "not-a-number", "start_seconds": 120})
        plan = _plan_nested(part=99)
        before = copy.deepcopy(plan)
        result = reconcile_series_metadata(plan, request)
        # bogus expected part -> reconciler declines to autocorrect; caller's
        # normal mismatch check still fires.
        self.assertEqual(result.changes, [])
        self.assertEqual(plan, before)

    def test_request_with_missing_series_id_is_skipped(self):
        request = _request(series={"enabled": True, "series_id": "",
                                    "source_job_id": "series-123-p1",
                                    "part": 2, "start_seconds": 120})
        plan = _plan_nested(part=1)
        before = copy.deepcopy(plan)
        result = reconcile_series_metadata(plan, request)
        self.assertEqual(result.changes, [])
        self.assertEqual(plan, before)


class LogLineFormat(unittest.TestCase):
    def test_no_changes_line(self):
        plan = _plan_nested()
        result = reconcile_series_metadata(plan, _request())
        line = format_log_line(result)
        self.assertIn("no changes", line)
        self.assertNotIn("auto-corrected", line)

    def test_changes_line_names_every_field(self):
        plan = _plan_nested(series_id="wrong", part=1, start_seconds=0)
        result = reconcile_series_metadata(plan, _request())
        line = format_log_line(result)
        self.assertIn("auto-corrected 3", line)
        for name in ("series_id", "part", "start_seconds"):
            self.assertIn(name, line)


if __name__ == "__main__":
    unittest.main()
