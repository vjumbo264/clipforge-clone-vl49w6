#!/usr/bin/env python3
"""Regression coverage for planned-versus-reconciled duration collapse checks."""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from cut_and_produce import (  # noqa: E402
    MIN_RECONCILED_TO_PLANNED_RATIO,
    assert_reconciled_duration_coverage,
)


assert MIN_RECONCILED_TO_PLANNED_RATIO == 0.75

cuts = [
    {"start_seconds": 0, "end_seconds": 75},
    {"start_seconds": 75, "end_seconds": 150},
]

# 90%-plus narration coverage passes the independent original-plan guard.
covered_plan = [
    {"video_seconds": 70.0},
    {"video_seconds": 71.0},
]
assert_reconciled_duration_coverage(cuts, covered_plan)

# The old tautological reconciled-video versus voiceover comparison would pass
# this: each reconciled duration already equals the voiceover duration. The
# new guard must instead reject it against the original 150-second plan.
collapsed_plan = [
    {"video_seconds": 11.0},
    {"video_seconds": 36.0},
]
err = io.StringIO()
try:
    with contextlib.redirect_stderr(err):
        assert_reconciled_duration_coverage(cuts, collapsed_plan)
    raise AssertionError("collapsed narration plan was accepted")
except SystemExit as exc:
    assert exc.code == 3
message = err.getvalue()
assert "narration duration collapse" in message
assert "planned 75.00s" in message
assert "cut #1" in message and "cut #2" in message

print("PASS: duration collapse guard rejects short reconciled narration while accepting covered plans")
