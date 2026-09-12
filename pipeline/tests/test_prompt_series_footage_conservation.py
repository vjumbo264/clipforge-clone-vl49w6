"""Regression guard for the Part C clarification: SERIES_DIRECTIVE must tell
the planning AI that series-mode story pacing (WHERE a non-final part's arc
stops — rules 4 and 9) never licenses shortening the footage of any INCLUDED
cut, and that footage must never be conserved/rationed across parts.

Pattern follows test_prompt_footage_longer_than_narration.py: render the
prompt via the module CLI with --series flags, then assert on the text.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPT_PY = ROOT / "pipeline" / "plan" / "prompt.py"

SERIES_HEADER = "SERIES MODE"
RULE9_ANCHOR = "stops at the most"
RULE10_ANCHOR = "SELF-CHECK BEFORE RETURNING"
CLAUSE_HEADER = "CUT LENGTH IS NOT A SERIES-PACING DECISION"
CLAUSE_SEPARATION = "ZERO influence on"  # phrase wraps across lines in the directive
CLAUSE_NO_CONSERVE = "Do not shorten a cut's footage because you're trying to"
CLAUSE_TWO_PARTS = "two well-realized parts, two parts is correct"
CLAUSE_PARALLEL = "whether or not this production is part of a series"


class TestSeriesFootageConservation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        out = Path(cls._tmp.name) / "prompt.txt"
        subprocess.run(
            [
                sys.executable,
                str(PROMPT_PY),
                "600",
                "100",
                str(out),
                "--target-duration",
                "120",
                "--series-part",
                "1",
                "--series-start-seconds",
                "0",
                "--series-id",
                "series-test-1",
            ],
            check=True,
            cwd=str(ROOT),
        )
        cls.text = out.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_series_directive_renders(self) -> None:
        self.assertIn(SERIES_HEADER, self.text)

    def test_clause_present(self) -> None:
        self.assertIn(CLAUSE_HEADER, self.text)

    def test_clause_separates_arc_stop_from_cut_length(self) -> None:
        self.assertIn(CLAUSE_SEPARATION, self.text)

    def test_clause_forbids_footage_conservation(self) -> None:
        self.assertIn(CLAUSE_NO_CONSERVE, self.text)
        self.assertIn(CLAUSE_TWO_PARTS, self.text)

    def test_clause_draws_parallel_to_general_rule(self) -> None:
        self.assertIn(CLAUSE_PARALLEL, self.text)

    def test_clause_adjacent_to_rules_9_and_10(self) -> None:
        rule9_idx = self.text.index(RULE9_ANCHOR)
        clause_idx = self.text.index(CLAUSE_HEADER)
        rule10_idx = self.text.index(RULE10_ANCHOR)
        self.assertLess(rule9_idx, clause_idx)
        self.assertLess(clause_idx, rule10_idx)


if __name__ == "__main__":
    unittest.main()
