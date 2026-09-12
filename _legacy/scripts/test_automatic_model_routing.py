#!/usr/bin/env python3
"""Regression checks for direct-Gemini Automatic Mode model routing."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = (ROOT / "scripts" / "automatic_analysis.py").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "stage-a.yml").read_text(encoding="utf-8")

assert 'DEFAULT_PRIMARY_MODEL = "gemini-3.7-flash"' in RUNNER
assert 'DEFAULT_FALLBACK_MODELS = ("gemini-3.6-flash",)' in RUNNER
assert '"gemini-2.5-flash"' not in RUNNER, "retired Gemini 2.5 Flash fallback must not be routed"
assert "claude-opus" not in RUNNER.lower()
assert "anthropic/claude" not in RUNNER.lower()
assert "from google import genai" in RUNNER
assert "FunctionDeclaration" in RUNNER
assert "FunctionResponseBlob" in RUNNER
assert "GEMINI_API_KEYS" in WORKFLOW
assert "Run bounded Gemini Automatic Mode analysis" in WORKFLOW

for forbidden in (
    "Puter",
    "puter",
    "PUTER_AUTH_TOKENS",
    "puter_browser_bridge",
    "playwright install",
    "api.puter.com",
    "openai/v1",
):
    assert forbidden not in RUNNER, f"superseded provider marker remained in Automatic Mode runner: {forbidden}"
assert "PUTER_AUTH_TOKENS" not in WORKFLOW
assert "Install headless Chromium for Puter.js Automatic Mode" not in WORKFLOW

for page in ("index.html", "new-task.html", "settings.html", "task.html", "automatic.html"):
    text = (ROOT / page).read_text(encoding="utf-8")
    assert "Puter auth token" not in text
    assert "PUTER_AUTH_TOKENS" not in text
    assert "Claude-first" not in text

print("PASS: Automatic Mode uses direct current Gemini Flash routing, native function/image responses, the shared Gemini key secret, and no Puter or browser bridge path")
