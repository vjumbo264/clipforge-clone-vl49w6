"""Offline unit tests for the bug-69 Stage A release-outcome gate.

The gate's job is to answer ONE question after the full-bundle release attempt
fails: is this specifically GitHub's 2 GiB per-asset limit on
source_input.bin (retry without the source), or anything else (re-fail)?

These tests pin all four failure shapes the workflow can encounter:

  * oversized source + full pre-source prefix present  -> RETRY (the target)
  * oversized source + a pre-source asset missing      -> RE-FAIL (died early
    for another reason; must not silently ship source-less)
  * within-limit source (any failure)                  -> RE-FAIL
  * release never created at all (oversized source)    -> RE-FAIL when the
    prefix is therefore missing (nothing landed); RETRY only if the prefix
    somehow still landed (paranoid, but cheap to pin)

Plus: event_composites.zip joins the required prefix only when actually
produced, and a within-limit oversized-looking failure with the prefix
present still re-fails (size is the primary discriminator).

No network: ``fetch_assets`` is injected with canned release states, and the
bundle dir is fabricated in tmp_path. Sizes are faked by writing sparse files
(seek + write one byte) so no 2 GiB of real disk is touched.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.stage_a import release_gate  # noqa: E402

OVER = release_gate.LIMIT_BYTES + 1
UNDER = release_gate.LIMIT_BYTES - 1

# Sorted work/bundle/* order, for reference:
#   00_READ_THIS_FIRST.txt, event_composites.zip?, key_moments.json,
#   scene_index.json, screenshots.zip, source_input.bin, transcript.json,
#   manifest.json  (manifest is staged last by bundle.py? see note below)
FULL_PREFIX = {"00_READ_THIS_FIRST.txt", "key_moments.json",
               "scene_index.json", "screenshots.zip"}


def _make_bundle(tmp_path: Path, *, source_size: int,
                 event_composites: bool = False) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in ("00_READ_THIS_FIRST.txt", "key_moments.json",
                 "scene_index.json", "screenshots.zip", "transcript.json",
                 "manifest.json"):
        (bundle / name).write_bytes(b"x")
    if event_composites:
        (bundle / "event_composites.zip").write_bytes(b"x")
    src = bundle / "source_input.bin"
    with open(src, "wb") as fh:
        fh.seek(source_size - 1)
        fh.write(b"\0")
    assert os.path.getsize(src) == source_size
    return bundle


def _fetcher(names):
    return lambda tag, repo: set(names)


class TestOversizedSourceRetries:
    def test_oversized_with_full_prefix_retries(self, tmp_path):
        bundle = _make_bundle(tmp_path, source_size=OVER)
        # First attempt landed everything up to the source, then died on it.
        assert release_gate.classify(
            "clipforge-job-x", repo="o/r", bundle_dir=str(bundle),
            fetch_assets=_fetcher(FULL_PREFIX)) is True

    def test_oversized_with_prefix_plus_extra_retries(self, tmp_path):
        bundle = _make_bundle(tmp_path, source_size=OVER)
        # transcript.json also made it (sorts after the source — allowed).
        names = FULL_PREFIX | {"transcript.json"}
        assert release_gate.classify(
            "clipforge-job-x", repo="o/r", bundle_dir=str(bundle),
            fetch_assets=_fetcher(names)) is True

    def test_oversized_with_event_composites_requires_it(self, tmp_path):
        bundle = _make_bundle(tmp_path, source_size=OVER, event_composites=True)
        # event_composites.zip sorts before key_moments.json: when produced,
        # it must have landed too.
        assert release_gate.classify(
            "clipforge-job-x", repo="o/r", bundle_dir=str(bundle),
            fetch_assets=_fetcher(FULL_PREFIX)) is False
        assert release_gate.classify(
            "clipforge-job-x", repo="o/r", bundle_dir=str(bundle),
            fetch_assets=_fetcher(FULL_PREFIX | {"event_composites.zip"})) is True


class TestNonSizeFailuresRefuse:
    def test_oversized_but_prefix_asset_missing_refails(self, tmp_path):
        bundle = _make_bundle(tmp_path, source_size=OVER)
        # screenshots.zip (a pre-source asset) never landed — the upload died
        # BEFORE reaching the source, for some other reason. Must NOT drop
        # the source over this.
        names = FULL_PREFIX - {"screenshots.zip"}
        assert release_gate.classify(
            "clipforge-job-x", repo="o/r", bundle_dir=str(bundle),
            fetch_assets=_fetcher(names)) is False

    def test_within_limit_source_always_refails(self, tmp_path):
        bundle = _make_bundle(tmp_path, source_size=UNDER)
        # Even with the full prefix present (some other asset failed), a
        # within-limit source is never the size-limit path.
        assert release_gate.classify(
            "clipforge-job-x", repo="o/r", bundle_dir=str(bundle),
            fetch_assets=_fetcher(FULL_PREFIX | {"transcript.json"})) is False

    def test_release_never_created_refails(self, tmp_path):
        bundle = _make_bundle(tmp_path, source_size=OVER)
        # Action crashed before creating the release: prefix can't be
        # verified as landed, so this is not a confirmed size-limit shape.
        assert release_gate.classify(
            "clipforge-job-x", repo="o/r", bundle_dir=str(bundle),
            fetch_assets=_fetcher(set())) is False

    def test_missing_source_file_refails(self, tmp_path):
        bundle = _make_bundle(tmp_path, source_size=UNDER)
        (bundle / "source_input.bin").unlink()
        assert release_gate.classify(
            "clipforge-job-x", repo="o/r", bundle_dir=str(bundle),
            fetch_assets=_fetcher(FULL_PREFIX)) is False


class TestCliContract:
    def test_cli_exit_codes(self, tmp_path, monkeypatch):
        bundle = _make_bundle(tmp_path, source_size=OVER)
        monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
        monkeypatch.setattr(release_gate, "fetch_release_asset_names",
                            _fetcher(FULL_PREFIX))
        with pytest.raises(SystemExit) as exc:
            sys.argv = ["release_gate", "clipforge-job-x", "--bundle-dir", str(bundle)]
            release_gate.main()
        assert exc.value.code == 0

        monkeypatch.setattr(release_gate, "fetch_release_asset_names",
                            _fetcher(set()))
        with pytest.raises(SystemExit) as exc2:
            release_gate.main()
        assert exc2.value.code == 1
