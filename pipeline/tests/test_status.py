"""Unit tests for pipeline.status (jobs/<id>/status.json writer + state machine)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline import status  # noqa: E402


class JobIdTests(unittest.TestCase):
    def test_accepts_valid(self) -> None:
        for jid in ["manual-1", "series-abc-p2", "a", "A_B.c-1"]:
            self.assertTrue(status.is_valid_job_id(jid), jid)

    def test_rejects_invalid(self) -> None:
        for jid in ["", "has space", "slash/inside", 123, None, "x" * 121]:
            self.assertFalse(status.is_valid_job_id(jid), repr(jid))


class NewStatusTests(unittest.TestCase):
    def test_shape(self) -> None:
        rec = status.new_status(job_id="manual-1", mode="manual", now_epoch=1000)
        self.assertEqual(rec["version"], 2)
        self.assertEqual(rec["state"], "queued")
        self.assertEqual(rec["created_at_epoch"], 1000)
        self.assertEqual(rec["expires_at_epoch"], 1000 + status.DEFAULT_TTL_SECONDS)
        self.assertEqual(rec["publishing"]["status"], "not_requested")
        self.assertEqual(rec["series"]["enabled"], False)

    def test_rejects_bad_mode(self) -> None:
        with self.assertRaises(ValueError):
            status.new_status(job_id="j-1", mode="nope")

    def test_rejects_bad_state(self) -> None:
        with self.assertRaises(ValueError):
            status.new_status(job_id="j-1", mode="manual", state="bogus")

    def test_series_normalization(self) -> None:
        rec = status.new_status(
            job_id="j-1",
            mode="manual",
            series={"enabled": True, "series_id": "abc", "part": 2, "start_seconds": 30, "is_final": False},
        )
        self.assertEqual(rec["series"]["series_id"], "abc")
        self.assertEqual(rec["series"]["part"], 2)


class WriteStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_first_write_creates_file(self) -> None:
        rec = status.write_status(
            "manual-1",
            state="queued",
            mode="manual",
            message="starting",
            root=self.root,
            now_epoch=2000,
        )
        path = self.root / "manual-1" / "status.json"
        self.assertTrue(path.exists())
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored, rec)
        self.assertEqual(rec["state"], "queued")
        self.assertEqual(rec["created_at_epoch"], 2000)

    def test_merge_preserves_prior_fields(self) -> None:
        status.write_status(
            "manual-1",
            state="queued",
            mode="manual",
            release_tag="clipforge-manual-1",
            release_url="https://example/release",
            root=self.root,
            now_epoch=2000,
        )
        rec = status.write_status(
            "manual-1",
            state="stage_a_running",
            message="ingesting",
            root=self.root,
            now_epoch=2100,
        )
        self.assertEqual(rec["release_tag"], "clipforge-manual-1")
        self.assertEqual(rec["release_url"], "https://example/release")
        self.assertEqual(rec["state"], "stage_a_running")
        self.assertEqual(rec["created_at_epoch"], 2000)
        self.assertEqual(rec["updated_at_epoch"], 2100)

    def test_assets_merge_not_replace(self) -> None:
        status.write_status(
            "manual-1", state="queued", mode="manual",
            assets={"analysis_bundle_url": "u1"}, root=self.root, now_epoch=2000,
        )
        rec = status.write_status(
            "manual-1", state="stage_a_running",
            assets={"final_mp4": "u2"}, root=self.root, now_epoch=2100,
        )
        self.assertEqual(rec["assets"], {"analysis_bundle_url": "u1", "final_mp4": "u2"})

    def test_refuses_to_leave_terminal_state(self) -> None:
        status.write_status("manual-1", state="queued", mode="manual", root=self.root, now_epoch=2000)
        status.write_status("manual-1", state="complete", root=self.root, now_epoch=2100)
        with self.assertRaises(ValueError):
            status.write_status("manual-1", state="stage_b_running", root=self.root, now_epoch=2200)

    def test_terminal_to_terminal_idempotent(self) -> None:
        status.write_status("manual-1", state="queued", mode="manual", root=self.root, now_epoch=2000)
        status.write_status("manual-1", state="complete", root=self.root, now_epoch=2100)
        # complete -> complete is idempotent; error / cancelled are also permitted as
        # a terminal-to-terminal transition (used by cleanup jobs).
        status.write_status("manual-1", state="complete", root=self.root, now_epoch=2150)
        status.write_status("manual-1", state="cancelled", root=self.root, now_epoch=2200)

    def test_atomic_write_leaves_no_temp(self) -> None:
        status.write_status("j-1", state="queued", mode="manual", root=self.root, now_epoch=2000)
        job_dir = self.root / "j-1"
        # No temp files should be lingering after a successful write.
        for entry in job_dir.iterdir():
            self.assertFalse(entry.name.endswith(".tmp"))


class SchemaConformanceTests(unittest.TestCase):
    """The record produced by new_status must conform to the JSON schema."""

    def test_new_status_conforms_to_json_schema(self) -> None:
        try:
            import jsonschema  # type: ignore
        except ImportError:
            self.skipTest("jsonschema not installed")
        schema = json.loads((ROOT / "schemas" / "job_status.schema.json").read_text(encoding="utf-8"))
        rec = status.new_status(job_id="j-1", mode="manual")
        jsonschema.validate(rec, schema)


class PlanSeriesSyncTests(unittest.TestCase):
    """bug-57: status.series must be synced from production.json, not left
    at zeroed defaults — that desync was the root cause of the task header
    showing "manual-<id>", the Zernio publish button staying visible on
    series parts, and the missing next-part flow."""

    def setUp(self) -> None:
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, series_block: dict) -> None:
        status.write_status(
            "job-1", state="queued", mode="manual", series=series_block,
            root=self.tmp,
        )

    def test_plan_series_block_nested_shape(self) -> None:
        from pipeline.stage_b.common import plan_series_block
        block = plan_series_block({
            "title": "x",
            "series": {
                "series_id": "series-1", "part": 2, "start_seconds": 628,
                "end_seconds": 900, "is_final": False, "summary": "…",
            },
        })
        self.assertEqual(block, {
            "enabled": True, "series_id": "series-1", "part": 2,
            "start_seconds": 628, "is_final": False,
        })

    def test_plan_series_block_flat_shape(self) -> None:
        from pipeline.stage_b.common import plan_series_block
        block = plan_series_block({
            "series_id": "s-1", "series_part": 3,
            "series_start_seconds": 10, "series_final": True,
        })
        self.assertEqual(block, {
            "enabled": True, "series_id": "s-1", "part": 3,
            "start_seconds": 10, "is_final": True,
        })

    def test_plan_series_block_non_series_and_garbage(self) -> None:
        from pipeline.stage_b.common import plan_series_block
        off = {"enabled": False, "series_id": "", "part": 0,
               "start_seconds": 0, "is_final": False}
        self.assertEqual(plan_series_block({"title": "x"}), off)
        self.assertEqual(plan_series_block(None), off)
        self.assertEqual(plan_series_block({}), off)
        # Garbage in a declared series never crashes; values coerce safely.
        block = plan_series_block({"series_part": "x", "series_final": "yes"})
        self.assertTrue(block["enabled"])
        self.assertEqual(block["part"], 0)
        self.assertIs(block["is_final"], True)

    def test_write_status_syncs_series_from_plan(self) -> None:
        """The exact bug-57 scenario: a job whose status.series is zeroed
        gets its series block restored from the plan via write_status."""
        from pipeline.stage_b.common import plan_series_block
        self._seed({})  # zeroed series block, as the broken job had
        doc = {
            "series": {"series_id": "series-9", "part": 1,
                       "start_seconds": 0, "is_final": False},
        }
        rec = status.write_status("job-1", series=plan_series_block(doc), root=self.tmp)
        self.assertTrue(rec["series"]["enabled"])
        self.assertEqual(rec["series"]["series_id"], "series-9")
        self.assertEqual(rec["series"]["part"], 1)


if __name__ == "__main__":
    unittest.main()


class TtlConfigurationTests(unittest.TestCase):
    """fix-ttl-config-disconnect (TTL_FIX_PROGRESS.json, fix #1) regression
    coverage: a newly created job's expires_at_epoch must reflect the
    CONFIGURED TTL (env CLIPFORGE_TTL_SECONDS — the single repo Actions
    variable threaded through stage-a.yml/cleanup.yml), never a hardcoded
    constant that can drift from cleanup.yml's sweep TTL.
    """

    def setUp(self) -> None:
        import os
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._saved_env = os.environ.pop(status.TTL_ENV_VAR, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        import os
        if self._saved_env is not None:
            os.environ[status.TTL_ENV_VAR] = self._saved_env
        else:
            os.environ.pop(status.TTL_ENV_VAR, None)

    def test_configured_ttl_env_wins_for_new_status(self) -> None:
        import os
        os.environ[status.TTL_ENV_VAR] = "172800"  # 48h, the operator's setting
        rec = status.new_status(job_id="manual-1", mode="manual", now_epoch=1000)
        self.assertEqual(rec["expires_at_epoch"], 1000 + 172800)

    def test_fallback_default_when_no_env(self) -> None:
        rec = status.new_status(job_id="manual-1", mode="manual", now_epoch=1000)
        self.assertEqual(rec["expires_at_epoch"], 1000 + status.DEFAULT_TTL_SECONDS)

    def test_bad_or_nonpositive_env_falls_back_safely(self) -> None:
        import os
        for bad in ("", "abc", "0", "-3600", "  "):
            os.environ[status.TTL_ENV_VAR] = bad
            rec = status.new_status(job_id="manual-1", mode="manual", now_epoch=1000)
            self.assertEqual(
                rec["expires_at_epoch"], 1000 + status.DEFAULT_TTL_SECONDS,
                f"bad env {bad!r} must fall back to DEFAULT_TTL_SECONDS",
            )

    def test_explicit_ttl_seconds_still_wins(self) -> None:
        import os
        os.environ[status.TTL_ENV_VAR] = "172800"
        rec = status.new_status(
            job_id="manual-1", mode="manual", now_epoch=1000, ttl_seconds=600,
        )
        self.assertEqual(rec["expires_at_epoch"], 1600)

    def test_write_status_first_write_uses_configured_ttl(self) -> None:
        import os
        os.environ[status.TTL_ENV_VAR] = "172800"
        rec = status.write_status(
            "manual-1", state="queued", mode="manual", root=self.root, now_epoch=2000,
        )
        self.assertEqual(rec["expires_at_epoch"], 2000 + 172800)
        stored = json.loads((self.root / "manual-1" / "status.json").read_text())
        self.assertEqual(stored["expires_at_epoch"], 2000 + 172800)

    def test_write_status_late_expiry_backfill_uses_configured_ttl(self) -> None:
        """A pre-existing record MISSING expires_at_epoch gets it backfilled
        from the configured TTL, not the hardcoded constant."""
        import os
        os.environ[status.TTL_ENV_VAR] = "172800"
        job = self.root / "manual-1"
        job.mkdir(parents=True)
        (job / "status.json").write_text(json.dumps({
            "version": 2, "job_id": "manual-1", "mode": "manual",
            "state": "queued", "message": "", "created_at_epoch": 2000,
            "updated_at_epoch": 2000, "release_tag": "", "release_url": "",
            "assets": {}, "run": {}, "publishing": {},
            "series": {"enabled": False, "series_id": "", "part": 0,
                       "start_seconds": 0, "is_final": False},
        }))
        rec = status.write_status("manual-1", state="stage_a_running", root=self.root, now_epoch=2100)
        self.assertEqual(rec["expires_at_epoch"], 2000 + 172800)

    def test_write_status_merge_never_rewrites_existing_expiry(self) -> None:
        """Guard against an extend-on-touch regression: a merge write (no
        explicit expires_at_epoch) must PRESERVE the stored expiry exactly,
        even when the configured TTL changes between writes."""
        import os
        os.environ[status.TTL_ENV_VAR] = "43200"
        status.write_status("manual-1", state="queued", mode="manual", root=self.root, now_epoch=2000)
        os.environ[status.TTL_ENV_VAR] = "172800"  # operator bumps TTL later
        rec = status.write_status("manual-1", state="stage_a_running", root=self.root, now_epoch=2100)
        self.assertEqual(rec["expires_at_epoch"], 2000 + 43200,
                         "merge writes must never re-extend expires_at_epoch")


class WorkflowTtlAgreementTests(unittest.TestCase):
    """The 48h-disconnect regression was THREE independently-set TTL numbers
    drifting apart (stage-a.yml / cleanup.yml / pipeline DEFAULT). These tests
    parse the real workflow YAML and assert every TTL-bearing site resolves to
    the SAME configured value, so a future edit that re-introduces drift fails
    the suite instead of the operator's jobs expiring wrongly.
    """

    WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    EXPECTED_TTL = 172800  # 48h — the operator's configured job lifetime

    def _read(self, name: str) -> str:
        return (self.WORKFLOWS / name).read_text(encoding="utf-8")

    def test_stage_a_and_cleanup_share_one_source(self) -> None:
        stage_a = self._read("stage-a.yml")
        cleanup = self._read("cleanup.yml")
        # Both workflows must draw from the SAME single Actions variable.
        self.assertIn("vars.CLIPFORGE_TTL_SECONDS", stage_a)
        self.assertIn("vars.CLIPFORGE_TTL_SECONDS", cleanup)
        # ...with the SAME in-repo fallback literal on both sides.
        shared = "CLIPFORGE_TTL_SECONDS || '172800'"
        self.assertIn(shared, stage_a, "stage-a.yml fallback must be 172800")
        self.assertIn(shared, cleanup, "cleanup.yml fallback must be 172800")

    def test_cleanup_default_is_not_the_old_12h(self) -> None:
        cleanup = self._read("cleanup.yml")
        # The old hardcoded 12h fallback (43200) is exactly the drift bug; it
        # must not survive anywhere in the cleanup workflow's TTL plumbing.
        self.assertNotIn("|| '43200'", cleanup)
        self.assertNotIn("default: \"43200\"", cleanup)

    def test_stage_a_threads_ttl_into_status_writer(self) -> None:
        stage_a = self._read("stage-a.yml")
        # Both job-creation-adjacent status writes must pass --ttl-seconds so
        # a NEW job's expires_at_epoch is computed from the configured value.
        self.assertGreaterEqual(
            stage_a.count('--ttl-seconds "$CLIPFORGE_TTL_SECONDS"'), 2,
            "stage-a.yml must pass --ttl-seconds \"$CLIPFORGE_TTL_SECONDS\" "
            "on the notify-start AND write-initial-status calls",
        )

    def test_status_py_default_documented_as_fallback_only(self) -> None:
        src = (Path(__file__).resolve().parents[2] / "pipeline" / "status.py").read_text(encoding="utf-8")
        self.assertIn("CLIPFORGE_TTL_SECONDS", src)
        self.assertIn("DEFAULT_TTL_SECONDS = 12 * 3600", src)

    def test_bot_ttl_var_documented_and_valued(self) -> None:
        """The bot writes every job's initial status.json, so its TTL env var
        must exist in wrangler config and equal the same 48h value."""
        import re
        cfg = (Path(__file__).resolve().parents[2] / "bot" / "wrangler.bot-a.jsonc").read_text(encoding="utf-8")
        match = re.search(r'"CLIPFORGE_JOB_TTL_SECONDS"\s*:\s*"(\d+)"', cfg)
        self.assertIsNotNone(match, "bot/wrangler.bot-a.jsonc must set CLIPFORGE_JOB_TTL_SECONDS")
        self.assertEqual(int(match.group(1)), self.EXPECTED_TTL,
                         "bot job TTL must agree with the workflow TTLs")
