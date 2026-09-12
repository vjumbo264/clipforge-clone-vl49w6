"""Cross-language equivalence tests for the production.json validator.

Loads the language-neutral fixtures in ``fixtures/plan_cases.json``, runs each
case through the Python validator, and then re-runs the *same* documents
through the JS validator via a tiny Node.js shim. Both implementations must
produce the same accept/reject decision AND the same error strings (as a set)
for the same input.

Fixture post-processing: entries whose ``post_process`` block asks for
``expand_summary_to_length: N`` have their nested ``series.summary`` string
replaced with an N-character block so we can express "longer than 1200" without
bloating the JSON file itself.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.plan.schema import validate_production_plan  # noqa: E402


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "plan_cases.json"
JS_SHIM_PATH = Path(__file__).resolve().parent / "run_js_validator.mjs"


def _apply_post_process(document: Any, post: dict[str, Any]) -> Any:
    if not isinstance(document, dict):
        return document
    if "expand_summary_to_length" in post:
        length = int(post["expand_summary_to_length"])
        series = document.get("series")
        if isinstance(series, dict):
            series["summary"] = "x" * length
        if "series_summary" in document:
            document["series_summary"] = "x" * length
    return document


def _load_cases() -> list[dict[str, Any]]:
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    cases = data["cases"]
    for case in cases:
        if "post_process" in case:
            case["document"] = _apply_post_process(case["document"], case["post_process"])
    return cases


def _run_js_validator(documents: list[Any]) -> list[list[str]]:
    """Batch-run the JS validator on ``documents`` and return per-doc error lists."""
    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("node not available on PATH — JS-side cross-validation skipped")
    payload = json.dumps(documents)
    result = subprocess.run(
        [node, str(JS_SHIM_PATH)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(
            "JS validator shim failed rc=" + str(result.returncode)
            + "\nstdout=" + result.stdout
            + "\nstderr=" + result.stderr
        )
    return json.loads(result.stdout)


class PythonValidatorTests(unittest.TestCase):
    def test_all_fixtures(self) -> None:
        cases = _load_cases()
        self.assertGreater(len(cases), 10, "fixture file should contain many cases")
        for case in cases:
            with self.subTest(case=case["name"]):
                errors = validate_production_plan(case["document"])
                if case["valid"]:
                    self.assertEqual(errors, [], f"expected valid but got: {errors}")
                else:
                    self.assertNotEqual(errors, [], "expected errors but got none")
                    if "expected_errors" in case:
                        self.assertEqual(sorted(set(errors)), sorted(set(case["expected_errors"])))


class CrossLanguageEquivalenceTests(unittest.TestCase):
    def test_js_matches_python(self) -> None:
        cases = _load_cases()
        js_results = _run_js_validator([c["document"] for c in cases])
        self.assertEqual(len(js_results), len(cases))
        for case, js_errors in zip(cases, js_results):
            with self.subTest(case=case["name"]):
                py_errors = validate_production_plan(case["document"])
                self.assertEqual(
                    sorted(set(py_errors)),
                    sorted(set(js_errors)),
                    "\nPython errors: " + repr(py_errors)
                    + "\nJS errors:     " + repr(js_errors),
                )


if __name__ == "__main__":
    unittest.main()
