"""Bug 1 regression tests: Stage A failures must always leave a readable
``status.json`` with ``state: error`` — including failures BEFORE the
``Resolve job + read the Stage A request`` step (``id: req``) has produced a
usable ``job_id`` output (checkout, environment setup, input validation).

Two layers are pinned:

1. The workflow structure itself (parsed with PyYAML): the request step must
   emit ``job_id`` unconditionally (validation failures exit 0 with
   ``job_valid=false`` and the hard stop lives in a dedicated later step), and
   the ``if: failure()`` handler must NOT be able to exit 0 after failing to
   persist the error status (no trailing ``|| true`` / bare ``true``).
2. The runtime behavior: ``pipeline.status`` invoked with the job id the
   handler now resolves writes a readable error record for a job that has NO
   prior status file at all (the early-failure case).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml  # noqa: E402 — available in CI (deploy gate) and the dev sandbox

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

WORKFLOW = ROOT / ".github" / "workflows" / "stage-a.yml"


def _load_workflow() -> dict:
    # PyYAML parses the key ``on`` as boolean True (YAML 1.1).
    with WORKFLOW.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class StageAFailureWorkflowShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = _load_workflow()
        steps = self.doc["jobs"]["stage_a"]["steps"]
        self.by_id = {s.get("id"): s for s in steps if s.get("id")}
        self.by_name = {s.get("name"): s for s in steps}

    def test_request_step_emits_job_id_before_any_possible_failure(self) -> None:
        run = self.by_id["req"]["run"]
        echo_pos = run.find('echo "job_id=$job_id" >> "$GITHUB_OUTPUT"')
        self.assertNotEqual(echo_pos, -1, "req step must write the job_id output")
        # The validation exit must come AFTER the job_id echo, and must not
        # hard-fail the step (exit 0 + job_valid=false instead).
        first_exit1 = run.find("exit 1")
        self.assertTrue(first_exit1 == -1 or echo_pos < first_exit1,
                        "job_id output must be written before the step can fail")
        self.assertIn('echo "job_valid=false" >> "$GITHUB_OUTPUT"', run)
        self.assertIn("exit 0", run)

    def test_hard_stop_is_a_dedicated_later_step(self) -> None:
        validate = self.by_name.get("Validate resolved job")
        self.assertIsNotNone(validate, "expected a 'Validate resolved job' step")
        self.assertIn("job_valid != 'true'", str(validate.get("if", "")))
        self.assertIn("exit 1", validate["run"])

    def test_failure_handler_cannot_succeed_without_persisting(self) -> None:
        handler = self.by_name.get("On failure, write error status")
        self.assertIsNotNone(handler)
        self.assertEqual(str(handler.get("if", "")), "failure()")
        run = handler["run"]
        self.assertIn("exit 1", run, "handler must fail loudly when it cannot persist")
        stripped = "\n".join(
            line for line in run.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
        lines = [l.strip() for l in stripped.splitlines()]
        self.assertNotEqual(lines[-1], "true", "handler must not end in a bare `true`")
        self.assertNotIn("|| true\n          true", run)

    def test_failure_handler_guards_on_unusable_job_id(self) -> None:
        run = self.by_name["On failure, write error status"]["run"]
        self.assertIn("FATAL: Stage A failed before a usable job_id was resolved", run)


class EarlyFailureStatusWriteTests(unittest.TestCase):
    """The runtime half: with NO existing jobs/<id>/status.json (the job died
    before its first status write), the exact CLI call the failure handler
    makes must still produce a readable error record."""

    def test_status_cli_writes_error_for_brand_new_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "jobs"
            job_id = "manual-early-fail"
            result = subprocess.run(
                [sys.executable, "-m", "pipeline.status", job_id,
                 "--state", "error",
                 "--message", "Stage A failed. See workflow run for logs.",
                 "--release-tag", f"clipforge-{job_id}",
                 "--out-dir", str(out_dir)],
                cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            status_file = out_dir / job_id / "status.json"
            self.assertTrue(status_file.exists(), "error status file must exist")
            record = json.loads(status_file.read_text(encoding="utf-8"))
            self.assertEqual(record["state"], "error")
            self.assertEqual(record["job_id"], job_id)
            self.assertTrue(record["message"], "error record must carry a real message")
            self.assertEqual(record["release_tag"], f"clipforge-{job_id}")
            self.assertGreater(record["expires_at_epoch"], record["created_at_epoch"])

    def test_status_cli_rejects_an_unusable_job_id_loudly(self) -> None:
        # Mirrors the guard in the failure handler: an empty/garbage id must
        # fail (non-zero exit), never be swallowed into a silent no-op.
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-m", "pipeline.status", "bad id with spaces",
                 "--state", "error", "--message", "x", "--out-dir", str(Path(tmp) / "jobs")],
                cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
