"""Regression guard for bug-72: prompt.py must tell the planning AI that a
cut's raw footage running LONGER than its narration is expected and desirable,
because Stage B's ``reconcile_cuts`` automatically retimes the cut's FULL
``start_seconds..end_seconds`` range to match voiceover duration exactly via a
stretch factor (speed-up when footage > narration, slow-down when < narration).
It never trims or re-bounds a cut.

Without this signal, the AI shortens ``end_seconds`` to bring the word budget
closer to a natural narration length, which is the exact opposite of what the
operator wants: the entire visual moment must be preserved even when that means
the resulting footage is meaningfully longer than what the narration needs.

These tests pin the fix so it cannot be silently reverted or diluted later.
They follow the same pattern as ``test_prompt_word_budget_ordering.py``
(subprocess-render the prompt with the module CLI, then assert on the generated
text). They also confirm the new guidance sits adjacent to the existing
NARRATION DURATION CONTRACT so a linear-reading model encounters it in the
right place, and that the total narration-budget band is NOT weakened.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPT_PY = ROOT / "pipeline" / "plan" / "prompt.py"

# Distinctive anchors from the bug-72 addition.
HEADER = "FOOTAGE MAY RUN LONGER THAN NARRATION"
AUTO_RETIME = "AUTOMATICALLY retimes"
FULL_RANGE = "cut's full, complete `start_seconds..end_seconds` range"
NEVER_TRIMMED = "NEVER trimmed, shortened, or re-bounded by the pipeline"
KEEP_FULL_CUT = "KEEP THE FULL CUT"
SPED_UP_GOOD = "sped-up moment is a completely normal"
TRIM_IS_THE_MISTAKE = "trimming the cut to avoid it is the actual mistake"

# Anchors from the existing (must-not-be-weakened) NARRATION DURATION CONTRACT.
FLOOR_PERCENT = "90%-115% of this budget"
FLOOR_RATE = "3.133"
CONTRACT_HEADER = "NARRATION DURATION CONTRACT"
CONSTRAINTS_HEADER = "CONSTRAINTS"


# Anchors from the Part B reinforcement block (extreme-ratio worked example).
EXTREME_HEADER = 'NO STRETCH RATIO IS "TOO EXTREME"'
EXTREME_EXAMPLE = "requires 60 seconds of raw"
EXTREME_BOUND = "up to a full order of magnitude"
EXTREME_HESITATION = "this stretch ratio seems too extreme"
EXTREME_DISCONNECT = "does not make the final video shorter"

# The new block must sit inside the NARRATION DURATION CONTRACT area, before
# the CONSTRAINTS section, so it lives alongside the existing (correct)
# guidance rather than as an orphaned appendix.
ADJACENCY_CHARS = 3500


class TestFootageLongerThanNarration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        out = Path(cls._tmp.name) / "prompt.txt"
        subprocess.run(
            [
                sys.executable,
                str(PROMPT_PY),
                "300",
                "50",
                str(out),
                "--target-duration",
                "120",
            ],
            check=True,
            cwd=str(ROOT),
        )
        cls.text = out.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_footage_longer_header_present(self) -> None:
        self.assertIn(HEADER, self.text)

    def test_automatic_retiming_explained(self) -> None:
        self.assertIn(AUTO_RETIME, self.text)
        self.assertIn(FULL_RANGE, self.text)

    def test_cut_never_trimmed_by_pipeline(self) -> None:
        self.assertIn(NEVER_TRIMMED, self.text)

    def test_keep_full_cut_directive_present(self) -> None:
        self.assertIn(KEEP_FULL_CUT, self.text)

    def test_speed_up_framed_as_good_outcome(self) -> None:
        self.assertIn(SPED_UP_GOOD, self.text)

    def test_trimming_named_as_the_mistake(self) -> None:
        self.assertIn(TRIM_IS_THE_MISTAKE, self.text)

    def test_word_budget_floor_not_weakened(self) -> None:
        # The total-budget band must remain fully in force: TOTAL words across
        # all cuts land at 90%-115% of `target * (188/60)`. That band is what
        # converges final runtime to the operator's target, since Stage B
        # retimes footage to narration length exactly.
        self.assertIn(FLOOR_PERCENT, self.text)
        self.assertIn(FLOOR_RATE, self.text)

    def test_new_block_lives_inside_narration_duration_contract(self) -> None:
        # The new guidance must sit between the NARRATION DURATION CONTRACT
        # header and the CONSTRAINTS section, so it is read alongside the
        # existing correct-direction guidance, not stranded elsewhere.
        contract_idx = self.text.index(CONTRACT_HEADER)
        header_idx = self.text.index(HEADER)
        constraints_idx = self.text.index("\n" + CONSTRAINTS_HEADER)
        self.assertLess(contract_idx, header_idx)
        self.assertLess(header_idx, constraints_idx)

    def test_new_block_adjacent_to_word_budget_floor(self) -> None:
        # The "footage may run longer" guidance must sit close to the existing
        # word-budget-floor language so a linear reader sees both together and
        # cannot mistake this addition for permission to under-narrate.
        floor_idx = self.text.index(FLOOR_PERCENT)
        header_idx = self.text.index(HEADER)
        self.assertLess(
            abs(header_idx - floor_idx),
            ADJACENCY_CHARS,
            "the 'footage may run longer than narration' block drifted away "
            "from the 90% word-budget floor; the two must stay adjacent so "
            "the AI reads them together and does not treat the new guidance "
            "as license to write sparse narration for a long cut",
        )

    def test_cross_references_picking_end_seconds(self) -> None:
        # The addition must cross-reference "PICKING end_seconds" rather than
        # re-derive its rules, per the bug-72 spec.
        # Find the header's position and check a nearby PICKING end_seconds
        # reference exists within the addition.
        header_idx = self.text.index(HEADER)
        constraints_idx = self.text.index("\n" + CONSTRAINTS_HEADER)
        addition = self.text[header_idx:constraints_idx]
        self.assertIn("PICKING", addition)
        self.assertIn("end_seconds", addition)

    def test_extreme_ratio_header_present(self) -> None:
        self.assertIn(EXTREME_HEADER, self.text)

    def test_extreme_ratio_worked_example_present(self) -> None:
        self.assertIn(EXTREME_EXAMPLE, self.text)
        self.assertIn(EXTREME_BOUND, self.text)

    def test_extreme_ratio_hesitation_named(self) -> None:
        self.assertIn(EXTREME_HESITATION, self.text)

    def test_extreme_ratio_causal_disconnect_stated(self) -> None:
        self.assertIn(EXTREME_DISCONNECT, self.text)

    def test_extreme_ratio_adjacent_to_footage_section(self) -> None:
        # The Part B reinforcement must sit immediately after the existing
        # FOOTAGE MAY RUN LONGER THAN NARRATION block and before CONSTRAINTS,
        # so a linear reader meets it while the footage-vs-narration guidance
        # is still in view.
        footage_idx = self.text.index(HEADER)
        extreme_idx = self.text.index(EXTREME_HEADER)
        constraints_idx = self.text.index("\n" + CONSTRAINTS_HEADER)
        self.assertLess(footage_idx, extreme_idx)
        self.assertLess(extreme_idx, constraints_idx)


if __name__ == "__main__":
    unittest.main()
