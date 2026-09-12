#!/usr/bin/env python3
"""Regression coverage for analysis-prompt template interpolation."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "generate_analysis_prompt.py"
FOCUS = "When they were trapped in the cave and gon carries everyone out."
LITERAL_KEYWORD_EXAMPLE = '[{"word": "exact script word", "color": "#RRGGBB"}]'


with tempfile.TemporaryDirectory(prefix="clipforge_prompt_test_") as temp_dir:
    output = Path(temp_dir) / "00_READ_THIS_FIRST.txt"
    subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "600",
            "100",
            str(output),
            "--target-duration",
            "180",
            "--job-id",
            "20260822-051719-32553947636",
            "--focus",
            FOCUS,
        ],
        check=True,
    )
    rendered = output.read_text(encoding="utf-8")

assert LITERAL_KEYWORD_EXAMPLE in rendered, "literal JSON keyword example was not rendered"
assert FOCUS in rendered, "focus interpolation was lost"
assert '"target_total_duration_seconds": 180' in rendered, "target duration was not rendered"
for required_phrase in (
    "WRITE IT TO BE EMOTION-FIRST, NOT JUST EVENT-ACCURATE.",
    "The emotional throughline is the story spine.",
    "LEAD WITH THE EMOTIONAL TURN.",
    "NAME CLEAR EMOTIONS DIRECTLY.",
    "STAY EVIDENCE-GROUNDED, NOT MIND-READING.",
    '"She is shocked." "He is terrified."',
    "COMMENTARY RHYTHM — emotion-first narration still moves with crisp, declarative momentum.",
    "present-tense immediacy",
    "Build short cause-and-effect chains in which the effect is often an\n    emotional consequence",
    "Before a twist or reveal, withhold the explanation for one beat",
    "EMOTION-FIRST EXAMPLE.",
    "He is stunned — and\n    triumphant.",
    "same supported\n    events are organized around the viewer's emotional experience",
    "emotion-first account of WHAT HAPPENS in that segment",
    '"she is terrified," "he is humiliated," "they\n          are relieved"',
    "WORD CHOICE & PERSONALITY — change the phrasing, NOT the voice delivery",
    "casual turns of phrase, playful understatement, teasing, blunt reactions",
    "Mild profanity is allowed very occasionally",
    "Tease a visible choice, plan, or consequence",
    "LANGUAGE-ONLY instruction",
    "Do not change narration pace, delivery, or audio direction",
    "NARRATION DURATION CONTRACT — REQUIRED FOR EVERY CUT",
    "188 words per minute (3.133\n  spoken words per second)",
    "(end_seconds - start_seconds) * (188 / 60)",
    "fall below 90% of this budget (2.82 words per second)",
    "Count the actual words in every\n  `voiceover_text` before returning the plan",
):
    assert required_phrase in rendered, f"missing narration-style guidance: {required_phrase}"

# The prompt's explicit shock/reversal/triumph example is the concrete
# acceptance sample: emotions lead, while visible events prove them.
sample = 'write: "For a second,\n    it looks like he has lost. Then the result changes. He is stunned — and\n    triumphant."'
assert sample in rendered
assert "He misses,\n    looks at the result, and lifts the trophy" in rendered
assert "about 2.5 words per\n    second" not in rendered, "obsolete soft pacing guidance must not conflict with the hard TTS contract"

# A missing focus is deliberately not historic whole-video mode. It must issue
# an independent, evidence-first editorial constraint before any screenshots
# are opened and must explicitly prohibit a broad highlight reel.
with tempfile.TemporaryDirectory(prefix="clipforge_no_focus_prompt_test_") as temp_dir:
    no_focus_output = Path(temp_dir) / "00_READ_THIS_FIRST.txt"
    subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "600",
            "100",
            str(no_focus_output),
            "--target-duration",
            "180",
            "--job-id",
            "20260824-no-focus",
        ],
        check=True,
    )
    no_focus_rendered = no_focus_output.read_text(encoding="utf-8")

for required_phrase in (
    "NO OPERATOR FOCUS — SELF-SELECT ONE COMPELLING STORY THREAD FIRST",
    "Read transcript.json in timeline order.",
    "Read key_moments.json before using vision.",
    "`emotional_score`, `priority`, dialogue, and candidate ranking",
    "Choose EXACTLY ONE strongest supported scene, exchange, or story thread.",
    "State that self-selected thread",
    "open any screenshot composite, you must create your own narrow editorial focus",
    "Every screenshot opened, cut selected, and voiceover_text line must serve",
    "evenly sampled episode summary or a whole-video highlight reel",
    "Do not collect one high-priority beat from each subplot or act.",
    "A broad recap or evenly sampled highlight reel is prohibited.",
):
    assert required_phrase in no_focus_rendered, f"missing no-focus single-thread guidance: {required_phrase}"

assert "FOCUS DIRECTIVE — READ AND OBEY BEFORE ANYTHING ELSE BELOW" not in no_focus_rendered
assert "(none — whole video considered)" not in no_focus_rendered
print("PASS: analysis prompt preserves emotion-first guidance, enforces a 188-WPM narration budget, and requires one self-selected thread in no-focus mode")
