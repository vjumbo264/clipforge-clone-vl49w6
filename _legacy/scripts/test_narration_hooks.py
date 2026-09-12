#!/usr/bin/env python3
"""Regression coverage for Stage A's language-only opening-hook guidance."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPT = ROOT / "scripts" / "generate_analysis_prompt.py"
FIXTURES = ROOT / "scripts" / "fixtures" / "narration_hook_cases.json"
RHYTHM_SAMPLE = ROOT / "scripts" / "fixtures" / "commentary_rhythm_sample.json"
TITLE_SAMPLE = ROOT / "scripts" / "fixtures" / "title_constraint_sample.json"
TTS = ROOT / "scripts" / "generate_voiceover.py"
SUBTITLES = ROOT / "scripts" / "generate_subtitles_cinematic.py"
STAGE_B = ROOT / ".github" / "workflows" / "stage-b.yml"


def test_hook_fixture_cases_are_diverse_and_source_backed() -> None:
    cases = json.loads(FIXTURES.read_text(encoding="utf-8"))["cases"]
    assert len(cases) >= 7
    assert len({case["hook_mechanism"] for case in cases}) == len(cases)
    for case in cases:
        before = case["before_opening"].lower()
        after = case["after_opening"].lower()
        assert before != after, case["story_type"]
        assert len(after.split()) <= 16, case["story_type"]
        assert not after.startswith(("he walks", "she walks", "then", "after that", "this is", "in this scene", "basically", "first", "meanwhile", "at this point", "that's when")), case["story_type"]
        for term in case["required_source_terms"]:
            assert term.lower() in after, (case["story_type"], term)


def test_prompt_contains_dedicated_source_grounded_opening_hook_strategy() -> None:
    rendered = PROMPT.read_text(encoding="utf-8")
    for phrase in (
        "OPENING HOOK — FIRST-CUT WRITING",
        "curiosity-inducing, surprising,",
        "dangerous,\n        absurd, emotional, ironic, or high-stakes truth.",
        "Do NOT begin by mechanically summarizing the first chronological event",
        "HOOK → minimal context → escalation → payoff",
        "Write several candidate opening lines internally",
        "Aim for roughly 7–14 words in the first sentence",
        "Reject a candidate that merely begins with a generic person doing the",
        '"a teenager steps into...", or "she goes to confront..."',
        "ordinary entrance dangerous, awkward, absurd, or consequential, frame",
        "that disruption before explaining who walked where.",
        "For a mystery or reveal, default to withholding the hidden answer in",
        "Do not resolve the mystery by naming the contained item",
        "OPENING QUALITY GATE — before returning production.json, internally",
        "If the answer to any applicable question is no, rewrite",
        "HIGHER CURIOSITY WITHOUT LOWERING ACCURACY",
        "do not invent events,",
        "motivations, dialogue, reactions, relationships, or consequences.",
        "Do not force slang, fake",
        "excitement, or generic viral phrasing.",
        "Do not change narration pace, delivery, or audio direction",
    ):
        assert phrase in rendered, f"missing hook instruction: {phrase}"


def test_title_guidance_has_a_concrete_short_word_target() -> None:
    rendered = PROMPT.read_text(encoding="utf-8")
    assert "roughly 5-8 words" in rendered
    assert "not a description, plot summary" in rendered
    sample = json.loads(TITLE_SAMPLE.read_text(encoding="utf-8"))
    assert 5 <= len(sample["title"].split()) <= 8
    assert sample["word_count"] == len(sample["title"].split())


def test_commentary_rhythm_techniques_are_integrated_and_demonstrated() -> None:
    rendered = PROMPT.read_text(encoding="utf-8")
    for phrase in (
        "present-tense immediacy",
        "short cause-and-effect chains",
        "withhold the explanation for one beat",
        "recurring stakes-language",
        "Do not invent an unsupported private monologue, motive, or backstory.",
    ):
        assert phrase in rendered, f"missing commentary technique guidance: {phrase}"

    sample = json.loads(RHYTHM_SAMPLE.read_text(encoding="utf-8"))
    assert len(sample["generated_after_guidance"].split(". ")) >= 6
    assert all(sample["evidence"].values())
    assert "The alarm is already live." in sample["generated_after_guidance"]
    assert "Then the door locks behind him." in sample["generated_after_guidance"]


def test_tts_and_caption_pipeline_files_are_not_prompt_targets() -> None:
    prompt = PROMPT.read_text(encoding="utf-8")
    assert "voiceover_text" in prompt
    assert "The language itself, not the" in prompt
    assert "TTS, must make the opening stronger" in prompt
    # The production boundary stays intact: the TTS and subtitle consumers
    # receive voiceover_text verbatim and no settings are changed here.
    tts = TTS.read_text(encoding="utf-8").lower()
    subtitles = SUBTITLES.read_text(encoding="utf-8").lower()
    assert "voiceover_text" in tts and "production.json" in tts
    assert "voiceover_text" in subtitles and "production.json" in subtitles
    assert "generate_voiceover.py" in STAGE_B.read_text(encoding="utf-8")


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"narration hook tests passed ({len(tests)} tests)")
