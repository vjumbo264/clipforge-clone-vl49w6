#!/usr/bin/env python3
"""Structural checks for ClipForge’s GitHub Actions Zernio integration."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ROOT / ".github" / "workflows" / "zernio-publish.yml"
STAGE_B = ROOT / ".github" / "workflows" / "stage-b.yml"
APP = ROOT / "app.js"


def load(path: Path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{path.name} must parse as a YAML mapping"
    return doc


def test_publish_workflow_contract() -> None:
    doc = load(PUBLISH)
    dispatch = doc[True]["workflow_dispatch"]
    inputs = dispatch["inputs"]
    assert {"discover", "publish", "retry", "update", "cancel"} <= set(inputs["action"]["options"])
    assert {"publish_now", "manual_schedule", "smart_schedule"} <= set(inputs["mode"]["options"])
    assert "request_id" in inputs
    assert doc["permissions"]["contents"] == "write"
    assert doc["concurrency"]["group"] == "clipforge-zernio-publishing"
    rendered = PUBLISH.read_text(encoding="utf-8")
    assert "zernio_schedule.py" in rendered
    assert "external_scheduled.json" in rendered
    assert "stage') != 'complete'" in rendered
    assert "status['publishing']" in rendered
    assert "branding/zernio_queue.json" in rendered
    assert "ref: ${{ github.event.repository.default_branch }}" in rendered
    assert "read_json_object_safely" in rendered
    assert "publishing_error_state, read_json_object_safely" in rendered
    assert "from scripts.zernio_publish import aggregate_publishing_status" in rendered
    assert "publishing['status'] = aggregate_publishing_status(updated)" in rendered
    assert "publishing = publishing_error_state(prior)" in rendered


def test_recovery_uses_latest_code_but_original_job_data() -> None:
    rendered = PUBLISH.read_text(encoding="utf-8")
    # State B code is checked out from the default branch. State A job data is
    # addressed only by the preserved workflow input and release tag pattern.
    assert "Checkout latest publishing implementation" in rendered
    assert "ref: ${{ github.event.repository.default_branch }}" in rendered
    assert 'status_path = Path(\'jobs\') / jid / \'status.json\'' in rendered
    assert '"jobs/$JOB_ID/production.json"' in rendered
    assert 'gh release download "clipforge-${{ inputs.job_id }}"' in rendered
    stage_b = STAGE_B.read_text(encoding="utf-8")
    assert '--ref "${{ github.event.repository.default_branch }}"' in stage_b
    app = APP.read_text(encoding="utf-8")
    assert "var REF = 'main';" in app
    assert "function zernioRequestIdForCurrentJob()" in app
    assert "request_id: zernioRequestIdForCurrentJob()" in app
    assert "publishing.idempotency_key" in app


def test_stage_b_dispatch_is_best_effort_after_completion() -> None:
    doc = load(STAGE_B)
    assert doc["permissions"]["contents"] == "write"
    assert doc["permissions"]["actions"] == "write"
    rendered = STAGE_B.read_text(encoding="utf-8")
    complete_at = rendered.index("Write complete status")
    auto_at = rendered.index("Optionally dispatch automatic Zernio publishing")
    failure_at = rendered.index("On failure, write error status")
    assert complete_at < auto_at < failure_at
    auto_section = rendered[auto_at:failure_at]
    assert "if: success()" in auto_section
    assert "|| echo \"WARN: automatic Zernio dispatch failed" in auto_section
    assert "zernio_targets.py automatic-fields" in auto_section
    assert "zernio_targets.py decode" in auto_section
    assert "TARGETS_JSON_B64" in auto_section
    assert "source work/zernio_auto.env" not in auto_section
    assert "--ref \"${{ github.event.repository.default_branch }}\"" in auto_section


def test_frontend_blocks_duplicate_full_publish_after_terminal_outcomes() -> None:
    rendered = APP.read_text(encoding="utf-8")
    assert "var repeatBlocked = posts.length > 0" in rendered
    assert "state.zernioBusy || active || repeatBlocked || !targets.length" in rendered
    assert "Already processed by Zernio:" in rendered
    assert "Retry failed target for an individual failed platform" in rendered


def test_frontend_derives_publish_state_from_per_platform_posts() -> None:
    rendered = APP.read_text(encoding="utf-8")
    assert "function aggregateZernioStatus(publishing)" in rendered
    assert "statuses.some(function (value) { return value === 'requested' || value === 'publishing'; })" in rendered
    assert "var meta = zernioStatusMeta(aggregateStatus);" in rendered
    assert "el['zernio-publish-job'].disabled = state.zernioBusy || active || repeatBlocked || !targets.length;" in rendered
    assert "The Zernio key could not be confirmed from this browser." in rendered


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"zernio workflow tests passed ({len(tests)} tests)")
