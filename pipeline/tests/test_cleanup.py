"""Offline tests for pipeline/cleanup/expired.py (background TTL cleanup).

Covers the expiry decision matrix (expires_at_epoch / created_at+ttl / release
fallback / unreadable), release tag -> job id mapping, the RFC 5988 pagination
parser, and a full mocked-API end-to-end run. No test makes real network
calls: all HTTP goes through a fake opener.
"""

from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request

from pipeline.cleanup.expired import (
    _next_link,
    job_id_from_release,
    job_is_expired,
    list_job_ids_from_disk,
    main,
    parse_iso8601,
    read_job_timing,
    series_is_complete,
)


class _FakeResponse:
    def __init__(self, status: int = 200, payload=None, headers: dict | None = None):
        self.status = status
        self.headers = headers or {}
        self._payload = json.dumps(payload).encode("utf-8") if payload is not None else b""

    def read(self, _limit: int = -1) -> bytes:
        return self._payload

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeGitHub:
    """Scriptable GitHub API double. Records every call; serves canned data."""

    def __init__(self, *, releases=None, branches=None):
        self.releases = list(releases or [])
        self.branches = list(branches or [])
        self.calls: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def opener(self, req: Request) -> _FakeResponse:
        method = req.get_method()
        url = req.full_url
        self.calls.append((method, url))
        if method == "GET" and "/releases" in url:
            return _FakeResponse(200, self.releases)
        if method == "GET" and "/branches" in url:
            return _FakeResponse(200, self.branches)
        if method == "DELETE":
            self.deleted.append(url)
            return _FakeResponse(204, None)
        raise AssertionError(f"unexpected request: {method} {url}")


class NextLink(unittest.TestCase):
    def test_parses_next(self):
        header = (
            '<https://api.github.com/repos/o/r/releases?per_page=100&page=2>; rel="next", '
            '<https://api.github.com/repos/o/r/releases?per_page=100&page=3>; rel="last"'
        )
        self.assertEqual(
            _next_link(header),
            "https://api.github.com/repos/o/r/releases?per_page=100&page=2",
        )

    def test_absent(self):
        self.assertEqual(_next_link('<https://x>; rel="last"'), "")
        self.assertEqual(_next_link(""), "")


class JobIdFromRelease(unittest.TestCase):
    def test_job_tag(self):
        self.assertEqual(job_id_from_release({"tag_name": "clipforge-manual-123"}), "manual-123")

    def test_relay_tag(self):
        self.assertEqual(job_id_from_release({"tag_name": "clipforge-relay-input-manual-9"}), "manual-9")

    def test_body_marker(self):
        rel = {"tag_name": "other", "body": "notes\nclipforge-job-id: automatic-42\nmore"}
        self.assertEqual(job_id_from_release(rel), "automatic-42")

    def test_unrelated_release(self):
        self.assertIsNone(job_id_from_release({"tag_name": "v1.0.0", "body": "release notes"}))


class JobTiming(unittest.TestCase):
    def test_reads_both_fields(self):
        with TemporaryDirectory() as td:
            job = Path(td) / "jobs" / "job-1"
            job.mkdir(parents=True)
            (job / "status.json").write_text(json.dumps({
                "state": "complete", "created_at_epoch": 100, "expires_at_epoch": 200,
            }), encoding="utf-8")
            self.assertEqual(read_job_timing(td, "job-1"),
                             {"created_at_epoch": 100, "expires_at_epoch": 200,
                              "series_id": "", "series_final": False})

    def test_missing_folder(self):
        with TemporaryDirectory() as td:
            self.assertEqual(read_job_timing(td, "nope"),
                             {"created_at_epoch": None, "expires_at_epoch": None,
                              "series_id": "", "series_final": False})

    def test_unreadable_json(self):
        with TemporaryDirectory() as td:
            job = Path(td) / "jobs" / "job-2"
            job.mkdir(parents=True)
            (job / "status.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(read_job_timing(td, "job-2"),
                             {"created_at_epoch": None, "expires_at_epoch": None,
                              "series_id": "", "series_final": False})

    def test_reads_series_fields(self):
        """bug-49/bug-66: series_id and the is_final flag are surfaced."""
        with TemporaryDirectory() as td:
            job = Path(td) / "jobs" / "series-x-p2"
            job.mkdir(parents=True)
            (job / "status.json").write_text(json.dumps({
                "state": "complete", "created_at_epoch": 100, "expires_at_epoch": 200,
                "series": {"enabled": True, "series_id": "series-x", "part": 2,
                           "is_final": True},
            }), encoding="utf-8")
            self.assertEqual(read_job_timing(td, "series-x-p2"),
                             {"created_at_epoch": 100, "expires_at_epoch": 200,
                              "series_id": "series-x", "series_final": True})

    def test_list_job_ids_skips_hidden(self):
        with TemporaryDirectory() as td:
            base = Path(td) / "jobs"
            (base / "a").mkdir(parents=True)
            (base / ".hidden").mkdir()
            (base / "file.txt").write_text("x", encoding="utf-8")
            self.assertEqual(sorted(list_job_ids_from_disk(td)), ["a"])


class ExpiryDecision(unittest.TestCase):
    NOW = 100_000

    def test_expires_at_epoch_wins(self):
        timing = {"created_at_epoch": 1, "expires_at_epoch": self.NOW - 1}
        expired, reason = job_is_expired(timing, now=self.NOW, ttl=86_400_000)
        self.assertTrue(expired)
        self.assertIn("expires_at", reason)

    def test_expires_at_epoch_future_keeps(self):
        timing = {"created_at_epoch": 1, "expires_at_epoch": self.NOW + 3600}
        expired, _ = job_is_expired(timing, now=self.NOW, ttl=10)
        self.assertFalse(expired)

    def test_created_plus_ttl(self):
        timing = {"created_at_epoch": self.NOW - 500, "expires_at_epoch": None}
        expired, reason = job_is_expired(timing, now=self.NOW, ttl=400)
        self.assertTrue(expired)
        self.assertIn("ttl=400", reason)
        expired, _ = job_is_expired(timing, now=self.NOW, ttl=600)
        self.assertFalse(expired)

    def test_release_fallback(self):
        timing = {"created_at_epoch": None, "expires_at_epoch": None}
        expired, reason = job_is_expired(
            timing, now=self.NOW, ttl=100, release_created_at=self.NOW - 200)
        self.assertTrue(expired)
        self.assertIn("release_created_at", reason)

    def test_nothing_readable_is_expired(self):
        timing = {"created_at_epoch": None, "expires_at_epoch": None}
        expired, reason = job_is_expired(timing, now=self.NOW, ttl=100)
        self.assertTrue(expired)
        self.assertIn("no readable timing", reason)


class SeriesCompleteness(unittest.TestCase):
    """bug-66 regression coverage: series_is_complete must require BOTH every
    existing part terminal AND at least one part actually marked is_final —
    OR (stuck-series grace, revert-and-cleanup-fix-session-1) every part
    terminal with the newest part's expiry at least 24h in the past.

    Incident replay (cleanup commit 1d6ad63): series-1787970477573 had parts 1
    and 2 both terminal but NEITHER marked is_final (part 3 never rendered).
    The pre-fix code treated the series as complete and reaped both parts
    while publishing was still scheduled. The sibling series-1787978275321 had
    a p2 genuinely marked is_final: true, so reaping its finished p1 was
    correct and must keep working. Those incident fixtures expire only
    ~seconds-to-an-hour before self.NOW — far inside the 24h grace window —
    so they stay protected exactly as bug-66 intended; the grace (whose own
    tests live in test_cleanup_series_grace.py) applies only to series that
    have been fully terminal AND quiet for more than a day.
    """

    NOW = 1_800_000_000

    def _write_part(self, td, job_id, *, series_id, part, is_final, state,
                    expired=True):
        job = Path(td) / "jobs" / job_id
        job.mkdir(parents=True, exist_ok=True)
        payload = {
            "state": state,
            "created_at_epoch": self.NOW - 1000,
            "expires_at_epoch": self.NOW - 1 if expired else self.NOW + 3600,
            "series": {"enabled": True, "series_id": series_id, "part": part,
                       "is_final": is_final},
        }
        (job / "status.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_mid_series_no_final_part_is_incomplete(self):
        """Incident case: every existing part terminal, NONE marked is_final
        -> series is INCOMPLETE (later parts still expected)."""
        with TemporaryDirectory() as td:
            self._write_part(td, "series-a-p1", series_id="series-a", part=1,
                             is_final=False, state="complete")
            self._write_part(td, "series-a-p2", series_id="series-a", part=2,
                             is_final=False, state="complete")
            self.assertFalse(series_is_complete(td, "series-a"))

    def test_terminal_final_part_makes_series_complete(self):
        """Sibling-series correct behavior: a terminal, is_final-marked final
        part means the series really is complete and may be reaped."""
        with TemporaryDirectory() as td:
            self._write_part(td, "series-b-p1", series_id="series-b", part=1,
                             is_final=False, state="complete")
            self._write_part(td, "series-b-p2", series_id="series-b", part=2,
                             is_final=True, state="complete")
            self.assertTrue(series_is_complete(td, "series-b"))

    def test_live_sibling_still_protects_series(self):
        """bug-49 unchanged: a non-terminal sibling keeps the series
        incomplete even when another part IS marked is_final."""
        with TemporaryDirectory() as td:
            self._write_part(td, "series-c-p1", series_id="series-c", part=1,
                             is_final=True, state="complete")
            self._write_part(td, "series-c-p2", series_id="series-c", part=2,
                             is_final=False, state="stage_b_running",
                             expired=False)
            self.assertFalse(series_is_complete(td, "series-c"))

    def test_end_to_end_incident_replay(self):
        """Full mocked cleanup run replaying 1d6ad63: expired mid-series jobs
        with no is_final part must be PROTECTED (folders and releases kept);
        the sibling series with a true is_final p2 must reap its finished p1
        exactly as the real run legitimately did."""
        import time as time_mod

        with TemporaryDirectory() as td:
            # Incident series: p1 + p2 terminal, neither is_final.
            self._write_part(td, "manual-incident-p1", series_id="series-inc",
                             part=1, is_final=False, state="complete")
            self._write_part(td, "series-inc-p2", series_id="series-inc",
                             part=2, is_final=False, state="complete")
            # Correctly-reaped sibling series: p1 terminal, p2 terminal+final.
            self._write_part(td, "manual-final-p1", series_id="series-fin",
                             part=1, is_final=False, state="complete")
            self._write_part(td, "series-fin-p2", series_id="series-fin",
                             part=2, is_final=True, state="complete")

            fake = _FakeGitHub(
                releases=[
                    {"id": 31, "tag_name": "clipforge-manual-incident-p1",
                     "created_at": "", "body": ""},
                    {"id": 32, "tag_name": "clipforge-series-inc-p2",
                     "created_at": "", "body": ""},
                    {"id": 33, "tag_name": "clipforge-manual-final-p1",
                     "created_at": "", "body": ""},
                    {"id": 34, "tag_name": "clipforge-series-fin-p2",
                     "created_at": "", "body": ""},
                ],
                branches=[],
            )
            real_time = time_mod.time
            time_mod.time = lambda: self.NOW
            try:
                rc = main(env={
                    "GITHUB_TOKEN": "test-token",
                    "GITHUB_REPOSITORY": "owner/repo",
                    "CLIPFORGE_TTL_SECONDS": "43200",
                }, root=td, opener=fake.opener, log=lambda *a, **k: None)
            finally:
                time_mod.time = real_time

            self.assertEqual(rc, 0)
            deleted = "\n".join(fake.deleted)
            # Incident series fully protected: nothing deleted, folders kept.
            self.assertNotIn("/releases/31", deleted)
            self.assertNotIn("/releases/32", deleted)
            self.assertTrue((Path(td) / "jobs" / "manual-incident-p1").exists())
            self.assertTrue((Path(td) / "jobs" / "series-inc-p2").exists())
            # Finished series reaped normally: both parts' releases deleted.
            self.assertIn("/releases/33", deleted)
            self.assertIn("/releases/34", deleted)
            self.assertFalse((Path(td) / "jobs" / "manual-final-p1").exists())
            self.assertFalse((Path(td) / "jobs" / "series-fin-p2").exists())


class SeriesTTLOperatorScenarios(unittest.TestCase):
    """Series-TTL initiative regression coverage: every scenario traced during
    the cascade investigation (see SERIES_TTL_PROGRESS.json) is pinned here.

    Evidence recap: the operator-observed cascades (cleanup commits 1d6ad63 and
    343446f, workflow runs 33268138682/33259122992, 2026-08-29) were produced
    by the PRE-bug-66 code; the bug-66 fix (e81ac72, requiring an is_final
    part) landed after them and every post-fix sweep reaped only legitimately
    finished series. These tests lock the protective behavior so any future
    regression of the guard fails the suite.
    """

    NOW = 1_800_000_000

    def _write_part(self, td, job_id, *, series_id, part, is_final, state,
                    expired=True, enabled=True):
        job = Path(td) / "jobs" / job_id
        job.mkdir(parents=True, exist_ok=True)
        payload = {
            "state": state,
            "created_at_epoch": self.NOW - 1000,
            "expires_at_epoch": self.NOW - 1 if expired else self.NOW + 3600,
            "series": {"enabled": enabled, "series_id": series_id, "part": part,
                       "start_seconds": 0, "is_final": is_final},
        }
        (job / "status.json").write_text(json.dumps(payload), encoding="utf-8")

    def _run_main(self, td, releases, branches=None):
        import time as time_mod

        fake = _FakeGitHub(releases=releases, branches=branches or [])
        real_time = time_mod.time
        time_mod.time = lambda: self.NOW
        try:
            rc = main(env={
                "GITHUB_TOKEN": "test-token",
                "GITHUB_REPOSITORY": "owner/repo",
                "CLIPFORGE_TTL_SECONDS": "43200",
            }, root=td, opener=fake.opener, log=lambda *a, **k: None)
        finally:
            time_mod.time = real_time
        self.assertEqual(rc, 0)
        return fake

    # --- Direct series_is_complete traces (investigation points 2/3/5). ---

    def test_point2_three_part_series_p1_expired_is_protected(self):
        """3-part series: p1 complete+expired (is_final=false), p2 complete
        not expired, p3 still queued (non-terminal, plan not yet populated).
        The expired part 1 must be protected."""
        with TemporaryDirectory() as td:
            self._write_part(td, "t3-p1", series_id="t3", part=1,
                             is_final=False, state="complete", expired=True)
            self._write_part(td, "t3-p2", series_id="t3", part=2,
                             is_final=False, state="complete", expired=False)
            self._write_part(td, "t3-p3", series_id="t3", part=3,
                             is_final=False, state="queued", expired=False)
            self.assertFalse(series_is_complete(td, "t3"))

    def test_point3_single_part_pending_next_is_protected(self):
        """Operator's most plausible real scenario: ONLY part 1 exists on disk
        (complete, expired, is_final=false); part 2 was never dispatched
        (operator has not clicked 'Start next part'). The lone part must be
        protected even though it is the only part the sweep can see."""
        with TemporaryDirectory() as td:
            self._write_part(td, "t1-p1", series_id="t1", part=1,
                             is_final=False, state="complete", expired=True)
            self.assertFalse(series_is_complete(td, "t1"))

    def test_point5_premature_is_final_running_sibling_protects(self):
        """is_final=true written at Stage B plan-sync time while the final
        part is still rendering (stage_b_running). The running sibling keeps
        the series incomplete even though a final-marked part exists —
        is_final alone must never reap an in-flight series."""
        with TemporaryDirectory() as td:
            self._write_part(td, "t5-p1", series_id="t5", part=1,
                             is_final=False, state="complete", expired=True)
            self._write_part(td, "t5-p2", series_id="t5", part=2,
                             is_final=True, state="stage_b_running",
                             expired=False)
            self.assertFalse(series_is_complete(td, "t5"))

    def test_point5b_corrupt_sibling_status_protects(self):
        """Missing/unknown = protect: a sibling whose status.json is
        unreadable counts as non-terminal, so the series is incomplete and
        the expired part is protected."""
        with TemporaryDirectory() as td:
            self._write_part(td, "t6-p1", series_id="t6", part=1,
                             is_final=False, state="complete", expired=True)
            bad = Path(td) / "jobs" / "t6-p2"
            bad.mkdir(parents=True)
            (bad / "status.json").write_text("{not json", encoding="utf-8")
            self.assertFalse(series_is_complete(td, "t6"))

    def test_control_finished_series_is_reaped(self):
        """Legitimate reap stays intact: every part terminal AND one marked
        is_final -> series complete -> expired parts are reaped."""
        with TemporaryDirectory() as td:
            self._write_part(td, "t7-p1", series_id="t7", part=1,
                             is_final=False, state="complete", expired=True)
            self._write_part(td, "t7-p2", series_id="t7", part=2,
                             is_final=True, state="complete", expired=True)
            self.assertTrue(series_is_complete(td, "t7"))

    # --- End-to-end main() traces (sections 1 and 2 both honor the guard). ---

    def test_e2e_single_part_series_with_release_is_protected(self):
        """Section-1 guard: the lone expired part of a pending-next series
        keeps BOTH its release and its folder."""
        with TemporaryDirectory() as td:
            self._write_part(td, "e1-p1", series_id="e1", part=1,
                             is_final=False, state="complete", expired=True)
            fake = self._run_main(td, releases=[
                {"id": 41, "tag_name": "clipforge-e1-p1",
                 "created_at": "", "body": ""},
            ])
            self.assertNotIn("/releases/41", "\n".join(fake.deleted))
            self.assertTrue((Path(td) / "jobs" / "e1-p1").exists())

    def test_e2e_folder_only_section2_guard_matches_section1(self):
        """Section-2 guard: an expired series part whose folder exists but
        whose release is already gone (partial prior cleanup) must be
        protected exactly as in section 1 — the two guards cannot disagree
        for the same job."""
        with TemporaryDirectory() as td:
            self._write_part(td, "e2-p1", series_id="e2", part=1,
                             is_final=False, state="complete", expired=True)
            self._write_part(td, "e2-p2", series_id="e2", part=2,
                             is_final=False, state="queued", expired=False)
            fake = self._run_main(td, releases=[
                {"id": 42, "tag_name": "clipforge-e2-p2",
                 "created_at": "", "body": ""},
            ])
            # p2 not expired -> its release is kept; p1 (no release) folder kept.
            self.assertNotIn("/releases/42", "\n".join(fake.deleted))
            self.assertTrue((Path(td) / "jobs" / "e2-p1").exists())
            self.assertTrue((Path(td) / "jobs" / "e2-p2").exists())

    def test_e2e_finished_series_reaps_all_parts_together(self):
        """When the series genuinely finished (terminal is_final sibling),
        the sweep reaps every expired part in one run — matching the
        post-bug-66 production sweeps (e.g. commit 0b956eb)."""
        with TemporaryDirectory() as td:
            self._write_part(td, "e3-p1", series_id="e3", part=1,
                             is_final=False, state="complete", expired=True)
            self._write_part(td, "e3-p2", series_id="e3", part=2,
                             is_final=True, state="complete", expired=True)
            fake = self._run_main(td, releases=[
                {"id": 43, "tag_name": "clipforge-e3-p1",
                 "created_at": "", "body": ""},
                {"id": 44, "tag_name": "clipforge-e3-p2",
                 "created_at": "", "body": ""},
            ])
            deleted = "\n".join(fake.deleted)
            self.assertIn("/releases/43", deleted)
            self.assertIn("/releases/44", deleted)
            self.assertFalse((Path(td) / "jobs" / "e3-p1").exists())
            self.assertFalse((Path(td) / "jobs" / "e3-p2").exists())


class ParseIso(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_iso8601("1970-01-01T00:01:00Z"), 60)

    def test_invalid(self):
        self.assertEqual(parse_iso8601("not-a-date"), 0)
        self.assertEqual(parse_iso8601(""), 0)


class EndToEnd(unittest.TestCase):
    NOW = 1_800_000_000

    def _env(self) -> dict[str, str]:
        return {
            "GITHUB_TOKEN": "test-token",  # fake; never leaves the process
            "GITHUB_REPOSITORY": "owner/repo",
            "CLIPFORGE_TTL_SECONDS": "43200",
        }

    def test_requires_credentials(self):
        self.assertEqual(main(env={}, root=".", opener=_FakeGitHub().opener, log=lambda *a, **k: None), 2)

    def test_rejects_bad_ttl(self):
        env = self._env()
        env["CLIPFORGE_TTL_SECONDS"] = "abc"
        self.assertEqual(main(env=env, root=".", opener=_FakeGitHub().opener, log=lambda *a, **k: None), 2)
        env["CLIPFORGE_TTL_SECONDS"] = "0"
        self.assertEqual(main(env=env, root=".", opener=_FakeGitHub().opener, log=lambda *a, **k: None), 2)

    def test_full_cleanup_run(self):
        import time as time_mod

        with TemporaryDirectory() as td:
            # Expired job (created long ago, no expires_at -> ttl applies).
            old = Path(td) / "jobs" / "manual-old"
            old.mkdir(parents=True)
            (old / "status.json").write_text(json.dumps({
                "state": "complete", "created_at_epoch": self.NOW - 90000,
            }), encoding="utf-8")
            # Live job (recent).
            live = Path(td) / "jobs" / "manual-live"
            live.mkdir(parents=True)
            (live / "status.json").write_text(json.dumps({
                "state": "stage_b_running", "created_at_epoch": self.NOW - 60,
            }), encoding="utf-8")
            # Folder with no status.json -> expired per legacy rule.
            orphan = Path(td) / "jobs" / "manual-orphan"
            orphan.mkdir(parents=True)

            fake = _FakeGitHub(
                releases=[
                    {"id": 11, "tag_name": "clipforge-manual-old",
                     "created_at": "2027-01-01T00:00:00Z", "body": ""},
                    {"id": 12, "tag_name": "clipforge-manual-live",
                     "created_at": "2027-01-01T00:00:00Z", "body": ""},
                    {"id": 13, "tag_name": "unrelated", "created_at": "", "body": ""},
                ],
                branches=[
                    {"name": "main"},
                    {"name": "clipforge-job/manual-old"},
                    {"name": "clipforge-job/manual-live"},
                    {"name": "clipforge-job/manual-ghost"},
                ],
            )

            real_time = time_mod.time
            time_mod.time = lambda: self.NOW
            try:
                rc = main(env=self._env(), root=td, opener=fake.opener,
                          log=lambda *a, **k: None)
            finally:
                time_mod.time = real_time

            self.assertEqual(rc, 0)
            deleted = "\n".join(fake.deleted)
            # Expired release + its tag deleted; live release untouched.
            self.assertIn("/releases/11", deleted)
            self.assertIn("/git/refs/tags/clipforge-manual-old", deleted)
            self.assertNotIn("/releases/12", deleted)
            # Expired + ghost branches deleted; live branch and main kept.
            self.assertIn("clipforge-job%2Fmanual-old", deleted.replace("/", "%2F"))
            self.assertIn("manual-ghost", deleted)
            self.assertNotIn("manual-live", deleted)
            self.assertNotIn("/main", deleted)
            # Expired folders removed from disk; live folder remains.
            self.assertFalse(old.exists())
            self.assertFalse(orphan.exists())
            self.assertTrue(live.exists())

    def test_expires_at_epoch_drives_expiry(self):
        import time as time_mod

        with TemporaryDirectory() as td:
            job = Path(td) / "jobs" / "manual-exp"
            job.mkdir(parents=True)
            # Recently created but explicitly expired (short operator TTL).
            (job / "status.json").write_text(json.dumps({
                "state": "complete",
                "created_at_epoch": self.NOW - 60,
                "expires_at_epoch": self.NOW - 1,
            }), encoding="utf-8")
            fake = _FakeGitHub(
                releases=[{"id": 21, "tag_name": "clipforge-manual-exp",
                           "created_at": "", "body": ""}],
                branches=[],
            )
            real_time = time_mod.time
            time_mod.time = lambda: self.NOW
            try:
                rc = main(env=self._env(), root=td, opener=fake.opener,
                          log=lambda *a, **k: None)
            finally:
                time_mod.time = real_time
            self.assertEqual(rc, 0)
            self.assertIn("/releases/21", "\n".join(fake.deleted))
            self.assertFalse(job.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
