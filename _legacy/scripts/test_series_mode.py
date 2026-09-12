#!/usr/bin/env python3
import json
import subprocess
import tempfile
from pathlib import Path
from production_plan_contract import validate_production_plan

ROOT = Path(__file__).resolve().parents[1]


def plan(start=0, end=120, final=False):
    return {
        "video_duration_seconds": 600, "target_total_duration_seconds": 120,
        "series_id": "series-demo", "series_part": 1,
        "series_start_seconds": start, "series_end_seconds": end,
        "series_final": final, "series_summary": "A compact cliffhanger summary.",
        "cuts": [{"start_seconds": start, "end_seconds": min(end, start + 20), "voiceover_text": "A grounded narration line."}],
    }


def main():
    assert not validate_production_plan(plan())
    assert any("precedes series_start_seconds" in error for error in validate_production_plan(plan(100, 180, False) | {"cuts": [{"start_seconds": 99, "end_seconds": 120, "voiceover_text": "bad"}]}))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); job = root / "jobs" / "series-demo"; job.mkdir(parents=True)
        (job / "production.json").write_text(json.dumps(plan()), encoding="utf-8")
        (job / "stage_a_request.json").write_text(json.dumps({"video_url":"https://example.test/source","automatic_mode":"false","focus":"legacy editorial focus must not continue","series_source_job_id":"series-demo"}), encoding="utf-8")
        out = subprocess.check_output(["python3", str(ROOT / "scripts" / "series_state.py"), str(root), "series-demo"], text=True)
        continuation = json.loads(out)
        assert continuation["continue"] is True and continuation["job_id"] == "series-demo-p2"
        assert continuation["series_start_seconds"] == "120" and "Part 1:" in continuation["series_context"]
        assert continuation["focus"] == ""
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "prompt.txt"
        env = {"SERIES_CONTEXT": "Part 1: private continuity only."}
        result = subprocess.run(["python3", str(ROOT / "scripts" / "generate_analysis_prompt.py"), "600", "100", str(output), "--series-part", "2", "--series-start-seconds", "120", "--series-context-env", "SERIES_CONTEXT"], env={**__import__('os').environ, **env}, check=True, capture_output=True, text=True)
        text = output.read_text(encoding="utf-8")
        assert "SERIES MODE — PART 2" in text and "previously on" in text and "120s onward" in text
    print("series mode tests passed")

if __name__ == "__main__":
    main()
