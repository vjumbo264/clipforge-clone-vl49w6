#!/usr/bin/env python3
"""Static checks for Stage B's single cinematic subtitle renderer."""
from pathlib import Path

workflow = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "stage-b.yml"
).read_text(encoding="utf-8")

assert "subtitle_mode:" not in workflow
assert "github.event.inputs.subtitle_mode" not in workflow
assert "generate_subtitles.py" not in workflow
assert "case \"$SUBTITLE_MODE\" in" not in workflow
assert "scripts/generate_subtitles_cinematic.py" in workflow
assert "- name: Burn cinematic subtitles into the final video" in workflow
print("PASS: Stage B has one direct cinematic subtitle renderer and no legacy selector")
