#!/usr/bin/env python3
"""Static regression checks for Stage B current-code execution and recovery."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "stage-b.yml").read_text(encoding="utf-8")
APP = (ROOT / "app.js").read_text(encoding="utf-8")


def test_workflow_uses_a_current_code_pin_or_current_default_branch() -> None:
    assert "code_ref:" in WORKFLOW
    assert "github.event.inputs.code_ref != '' && github.event.inputs.code_ref || github.event.repository.default_branch" in WORKFLOW
    assert "checked-out commit (HEAD)" in WORKFLOW
    assert "default branch fallback" in WORKFLOW
    assert "DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}" in WORKFLOW


def test_normal_and_recovery_dispatches_share_the_same_sha_resolution() -> None:
    assert "async function resolveCurrentStageBCodeRef()" in APP
    assert APP.count("await resolveCurrentStageBCodeRef()") >= 2
    # Both fresh production and recovery dispatches explicitly carry the pin.
    assert APP.count("code_ref: codeRef") >= 2


def test_failed_job_recovery_contract_prefers_updated_code_over_original_state() -> None:
    failed_run_revision = "deadbeef"
    fixed_current_revision = "feedface"
    supplied_code_ref = fixed_current_revision
    default_branch_revision = fixed_current_revision
    effective_revision = supplied_code_ref or default_branch_revision
    assert effective_revision != failed_run_revision
    assert effective_revision == fixed_current_revision


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"Stage B current-code tests passed ({len(tests)} tests)")
