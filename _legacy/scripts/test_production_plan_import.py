#!/usr/bin/env python3
"""Regression checks for importing a production.json plan into ClipForge."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "index.html").read_text(encoding="utf-8")


# Production plans are parsed from text, so their extension must not limit an
# otherwise-valid JSON document selected through the browser's file picker.
assert 'id="cuts-file-input"' in HTML
assert 'accept="application/json,.json,text/plain,.txt"' in HTML
assert "reader.readAsText(file)" in APP
assert "function importProductionPlanText(raw)" in APP
assert "parsed = JSON.parse(raw)" in APP
assert "var errors = validateCuts(parsed)" in APP
assert "state.validatedCuts = raw" in APP
assert "el['start-stage-b'].disabled = false" in APP
assert "importProductionPlanText(reader.result)" in APP

# Pasting is an alternative input surface, not an independent validator.
assert 'id="cuts-paste-toggle"' in HTML
assert '>Paste from clipboard<' in HTML
assert 'id="cuts-paste-input"' in HTML
assert 'id="cuts-paste-import"' in HTML
assert '>Validate typed JSON<' in HTML
assert "function showProductionPlanPasteField(focus)" in APP
assert "async function importProductionPlanFromClipboard()" in APP
assert "navigator.clipboard.readText()" in APP
assert "el['cuts-paste-input'].value = raw" in APP
assert "return importProductionPlanText(raw)" in APP
assert "function showClipboardImportFallback(message)" in APP
assert "Do not leave a previous successful plan armed after a clipboard error." in APP
assert "Clipboard access is unavailable here." in APP
assert "Could not read your clipboard." in APP
assert "el['cuts-paste-toggle'].addEventListener('click'" in APP
assert "importProductionPlanFromClipboard();" in APP
assert "el['cuts-paste-import'].addEventListener('click'" in APP
assert "importProductionPlanText(el['cuts-paste-input'].value)" in APP

print("PASS: .json, .txt, clipboard, and typed production plans share one validation and Stage B enablement path")
