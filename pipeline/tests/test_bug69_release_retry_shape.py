"""Bug-69 workflow-shape regression tests (parsed YAML, no network).

Pins the structural contract of the oversized-source release retry + Stage B
re-fetch so a future edit can't silently re-break it:

Stage A (stage-a.yml):
  * the primary softprops attempt uploads the FULL bundle (work/bundle/*) and
    is continue-on-error so the gate can classify the failure;
  * the gate step calls the unit-tested pipeline.stage_a.release_gate module
    (not a drifting inline copy) and emits the source_omitted output;
  * the retry uploads raw bytes via ``gh api --input`` (NOT ``-F data=@file``,
    which JSON-encodes and corrupts the asset — verified empirically);
  * the retry deletes a same-named leftover asset before re-POSTing
    (idempotency against a partially-succeeded first attempt);
  * the omitted-source body + manifest record the intentional omission
    (``source-omitted: true`` / ``source_omitted``), gated on
    ``release_gate.outputs.source_omitted == 'true'``.

Stage B (stage-b.yml):
  * relmeta records source_found instead of hard-failing on a missing source;
  * the download step runs only when source_found == 'true' (common path
    zero-change) and the re-fetch step only when it == 'false';
  * the re-fetch step calls pipeline.stage_a.refetch_source and exports
    ORIGINAL_PATH for the downstream render steps.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load(name: str) -> dict:
    with (ROOT / ".github" / "workflows" / name).open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _steps(doc: dict) -> list:
    """Every step (many wiring steps have no id — index by list, search by name)."""
    out = []
    for job in doc["jobs"].values():
        out.extend(job.get("steps", []))
    return out


def _by_id(steps: list, sid: str) -> dict:
    return next(s for s in steps if s.get("id") == sid)


def _by_name(steps: list, needle: str) -> dict:
    return next(s for s in steps if needle in (s.get("name") or ""))


class StageARetryShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.steps = _steps(_load("stage-a.yml"))

    def test_primary_attempt_is_full_bundle_and_continue_on_error(self) -> None:
        step = _by_id(self.steps, "release_full")
        self.assertTrue(step.get("continue-on-error"),
                        "primary attempt must be continue-on-error so the gate can classify")
        self.assertEqual(step["with"]["files"], "work/bundle/*",
                         "primary attempt must upload the FULL bundle including source_input.bin")
        self.assertEqual(step["uses"], "softprops/action-gh-release@v2")

    def test_gate_uses_tested_module_and_emits_output(self) -> None:
        run = _by_id(self.steps, "release_gate")["run"]
        self.assertIn("python -m pipeline.stage_a.release_gate", run,
                      "gate must call the unit-tested module, not an inline copy")
        self.assertIn('source_omitted=true', run)
        self.assertIn('source_omitted=false', run)
        self.assertIn("steps.release_full.outcome", run)

    def test_retry_uploads_raw_bytes_not_json_encoded(self) -> None:
        run = _by_id(self.steps, "release_gate")["run"]
        self.assertIn('"--input", path', run,
                      "retry must upload via --input (raw bytes)")
        self.assertNotIn('"-F", f"data=@{path}"', run,
                         "-F data=@file JSON-encodes the payload — asset would be corrupted")

    def test_retry_deletes_leftover_asset_before_repost(self) -> None:
        run = _by_id(self.steps, "release_gate")["run"]
        self.assertIn('"--method", "DELETE"', run)
        self.assertIn("releases/assets/", run,
                      "retry must delete a same-named leftover before re-POSTing (idempotency)")

    def test_omission_recorded_in_body_and_manifest(self) -> None:
        gate = _by_id(self.steps, "release_gate")["run"]
        self.assertIn("source-omitted: true", gate,
                      "omitted-source body must carry the unambiguous marker")
        manifest_step = _by_name(self.steps, "Record an omitted source in the manifest")
        self.assertEqual(manifest_step.get("if"),
                         "${{ steps.release_gate.outputs.source_omitted == 'true' }}",
                         "manifest rewrite must run only on the omitted path")
        mrun = manifest_step["run"]
        self.assertIn('m["source_omitted"] = True', mrun)
        self.assertIn('source_input.bin', mrun,
                      "manifest must drop the source_input.bin asset entry")
        self.assertIn("--input work/bundle/manifest.json", mrun,
                      "manifest re-upload must use --input (raw bytes), not -F data=@")


class StageBRefetchShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = _load("stage-b.yml")
        cls.steps = _steps(cls.doc)

    def test_relmeta_records_source_found_instead_of_failing(self) -> None:
        run = _by_id(self.steps, "relmeta")["run"]
        self.assertIn("source_found=true", run)
        self.assertIn("source_found=false", run)

    def test_download_step_only_when_source_present(self) -> None:
        download = _by_name(self.steps, "Download source video from release")
        self.assertEqual(download.get("if"),
                         "${{ steps.relmeta.outputs.source_found == 'true' }}")

    def test_refetch_step_only_when_source_absent(self) -> None:
        refetch = _by_name(self.steps, "Re-fetch omitted source")
        self.assertEqual(refetch.get("if"),
                         "${{ steps.relmeta.outputs.source_found == 'false' }}")
        self.assertIn("python -m pipeline.stage_a.refetch_source", refetch["run"])
        self.assertIn("ORIGINAL_PATH=", refetch["run"],
                      "re-fetch must export ORIGINAL_PATH for downstream render steps")

    def test_download_and_refetch_are_mutually_exclusive(self) -> None:
        download = _by_name(self.steps, "Download source video from release")
        refetch = _by_name(self.steps, "Re-fetch omitted source")
        self.assertNotEqual(download["if"], refetch["if"])
        self.assertIn("source_found == 'true'", download["if"])
        self.assertIn("source_found == 'false'", refetch["if"])


if __name__ == "__main__":
    unittest.main()
