"""Stuck-series grace-window coverage for pipeline.cleanup.expired.

Operator report (revert-and-cleanup-fix session): series tasks from 3+ days
ago never expired. Every stuck series on disk had ALL parts terminal but NO
part marked is_final, so the bug-66 guard in series_is_complete() protected
them forever. The fix adds a 24h inactivity grace: a fully-terminal series
with no is_final part becomes reapable once its newest part's own expiry is
at least DEFAULT_SERIES_GRACE_SECONDS in the past — while any genuinely
active series (a part still running, or one whose expiry is recent) stays
protected exactly as bug-66 intended.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from pipeline.cleanup.expired import (
    DEFAULT_SERIES_GRACE_SECONDS,
    series_is_complete,
)

NOW = int(time.time())
HOUR = 3600
TTL = 12 * HOUR  # mirrors pipeline/status.py DEFAULT_TTL_SECONDS


def _write_part(root, job_id, *, series_id, part, is_final, state, created):
    folder = Path(root) / "jobs" / job_id
    folder.mkdir(parents=True)
    (folder / "status.json").write_text(
        json.dumps({
            "job_id": job_id,
            "state": state,
            "created_at_epoch": created,
            "expires_at_epoch": created + TTL,
            "series": {
                "enabled": True,
                "series_id": series_id,
                "part": part,
                "start_seconds": 0,
                "is_final": is_final,
            },
        }),
        encoding="utf-8",
    )


class TestStuckSeriesGrace(unittest.TestCase):
    def test_fully_terminal_no_final_long_quiet_is_complete(self):
        """The operator's stuck series: every part terminal, none is_final,
        newest part expired days ago -> the series may finally be reaped."""
        with tempfile.TemporaryDirectory() as td:
            created = NOW - 3 * 24 * HOUR
            _write_part(td, "series-s-p1", series_id="series-s", part=1,
                        is_final=False, state="complete", created=created)
            _write_part(td, "series-s-p2", series_id="series-s", part=2,
                        is_final=False, state="complete", created=created + HOUR)
            self.assertTrue(series_is_complete(td, "series-s"))

    def test_fully_terminal_no_final_but_recent_is_incomplete(self):
        """bug-66 protection intact: a series whose newest part expired only
        an hour ago (< grace) may still be mid-publishing -> protected."""
        with tempfile.TemporaryDirectory() as td:
            created = NOW - TTL - HOUR  # newest part expired one hour ago
            _write_part(td, "series-r-p1", series_id="series-r", part=1,
                        is_final=False, state="complete", created=created - HOUR)
            _write_part(td, "series-r-p2", series_id="series-r", part=2,
                        is_final=False, state="complete", created=created)
            self.assertFalse(series_is_complete(td, "series-r"))

    def test_terminal_final_part_completes_immediately(self):
        """An is_final terminal part makes the series reapable with no wait."""
        with tempfile.TemporaryDirectory() as td:
            _write_part(td, "series-f-p1", series_id="series-f", part=1,
                        is_final=True, state="complete", created=NOW - HOUR)
            self.assertTrue(series_is_complete(td, "series-f"))

    def test_nonterminal_sibling_always_incomplete(self):
        """Even a long-quiet series is protected while any part is still
        alive (e.g. awaiting_plan) — the grace never overrides bug-49."""
        with tempfile.TemporaryDirectory() as td:
            created = NOW - 5 * 24 * HOUR
            _write_part(td, "series-a-p1", series_id="series-a", part=1,
                        is_final=False, state="complete", created=created)
            _write_part(td, "series-a-p2", series_id="series-a", part=2,
                        is_final=False, state="awaiting_plan", created=created)
            self.assertFalse(series_is_complete(td, "series-a"))

    def test_grace_boundary(self):
        """Exactly at newest-expiry + grace the series flips to complete;
        one second earlier it is still protected."""
        with tempfile.TemporaryDirectory() as td:
            created = NOW - TTL - DEFAULT_SERIES_GRACE_SECONDS
            _write_part(td, "series-g-p1", series_id="series-g", part=1,
                        is_final=False, state="complete", created=created)
            self.assertTrue(series_is_complete(td, "series-g", now=NOW))
            self.assertFalse(series_is_complete(td, "series-g", now=NOW - 1))


if __name__ == "__main__":
    unittest.main()
