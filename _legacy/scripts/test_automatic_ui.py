#!/usr/bin/env python3
"""Regression checks for the Automatic Mode source-to-delivery workspace."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "automatic.html").read_text(encoding="utf-8")
TASK = (ROOT / "task.html").read_text(encoding="utf-8")
APP = (ROOT / "app.js").read_text(encoding="utf-8")
WORKFLOW_A = (ROOT / ".github" / "workflows" / "stage-a.yml").read_text(encoding="utf-8")
WORKFLOW_B = (ROOT / ".github" / "workflows" / "stage-b.yml").read_text(encoding="utf-8")


for required in (
    '<body data-page="automatic">',
    "Automatic Mode",
    'id="stage-a-form"',
    'id="video-url-input"',
    'id="torrent-file-input"',
    'id="focus-input"',
    'id="start-stage-a"',
    "Start Automatic Mode",
    'id="settings-section" class="act" aria-labelledby="settings-heading" hidden aria-hidden="true"',
    'href="settings.html"',
    "configuration belongs on\n         the main Settings page",
    "whole-video highlight reels",
    'id="status-section"',
    'id="tasks-section"',
    'id="handoff-block"',
    'id="complete-block"',
    'src="production-plan-contract.js"',
    'src="app.js"',
    'id="stage-a-controls"',
    'id="restart-stage-a"',
    'Restart Stage A',
):
    assert required in HTML, f"Automatic Mode page missing: {required}"

for required in (
    "if (PAGE === 'automatic' && !state.geminiKeyMeta.length)",
    "var automaticMode = PAGE === 'automatic';",
    "automatic_mode: automaticMode ? 'true' : 'false'",
    "automatic_mode: settings.automatic_mode === 'true' ? 'true' : 'false'",
    "automatic_analysis_running",
    "var page = PAGE === 'automatic' ? 'automatic.html' : 'task.html';",
    "if (PAGE === 'task' || PAGE === 'automatic')",
    "Automatic Mode is reading evidence, selecting one story thread, and validating production.json.",
    "function stageARequestPath(jobId)",
    "async function persistStageARequest(jobId, inputs)",
    "function restartableStageAInputs(raw, jobId)",
    "async function restartStageA()",
    "function renderStageAControls(stage, status)",
):
    assert required in APP, f"Automatic Mode controller missing: {required}"

for required in ('id="stage-a-controls"', 'id="restart-stage-a"', 'Restart Stage A'):
    assert required in TASK, f"Manual task workspace missing Stage A restart UI: {required}"
assert "workflow_phase=stage_a" in WORKFLOW_A
assert "workflow_phase=stage_b" in WORKFLOW_B

for legacy in (
    "state.puterKeyMeta",
    "Puter auth token",
    "Puter",
    "Opus",
    "GPT fallback",
    "non-Opus",
):
    assert legacy not in HTML + APP, f"Automatic Mode must not retain legacy provider gate/copy: {legacy}"
print("PASS: Automatic Mode has a direct-Gemini-only source/focus launch form, shared Gemini key prerequisite, durable Stage A restart controls, unattended dispatch flag, and continuous task-progress route")
