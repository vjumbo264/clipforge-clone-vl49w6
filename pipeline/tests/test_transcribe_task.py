"""bug-65: unit tests for the transcribe/translate task plumbing.

No real faster-whisper model is downloaded — a stub ``faster_whisper`` module
is installed into sys.modules so FasterWhisperTranscriber can be constructed
and its argument-forwarding verified directly.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _install_fake_faster_whisper(captured: dict, detected_language: str = "ja",
                                 detected_probability: float = 0.97):
    """Install a stub faster_whisper module; returns nothing.

    The stub WhisperModel records every kwarg of transcribe() into ``captured``
    and yields one fixed segment plus an info object with the given detected
    language / probability.
    """
    fw = types.ModuleType("faster_whisper")

    class FakeSegment:
        start = 0.0
        end = 1.0
        text = "hello world"
        words = None

    class FakeInfo:
        language = detected_language
        language_probability = detected_probability
        duration = 1.0

    class FakeWhisperModel:
        def __init__(self, size, device=None, compute_type=None):
            captured["model_size"] = size

        def transcribe(self, path, **kwargs):
            captured["transcribe_kwargs"] = kwargs
            return iter([FakeSegment()]), FakeInfo()

    fw.WhisperModel = FakeWhisperModel
    return fw


@pytest.fixture()
def stub_fw():
    captured: dict = {}
    fw = _install_fake_faster_whisper(captured)
    # onnxruntime is imported eagerly by transcribe._verify_onnxruntime_importable;
    # stub it too so the test never depends on the real wheel.
    onnx = types.ModuleType("onnxruntime")
    with mock.patch.dict(sys.modules, {"faster_whisper": fw, "onnxruntime": onnx}):
        # Re-import fresh so the class picks up the stubbed modules.
        sys.modules.pop("pipeline.stage_a.transcribe", None)
        from pipeline.stage_a import transcribe
        yield transcribe, captured
    sys.modules.pop("pipeline.stage_a.transcribe", None)


def test_translate_default_forces_autodetect(stub_fw):
    """Default task is translate_to_english and language is forced to None
    (auto-detect) even when the caller passes a forced hint like 'en' — a
    forced 'en' would tell Whisper the audio already IS English and there
    would be nothing to translate FROM (bug-65)."""
    transcribe, captured = stub_fw
    tx = transcribe.FasterWhisperTranscriber(model_size="tiny", language="en")
    list(tx.transcribe("dummy.wav"))
    kwargs = captured["transcribe_kwargs"]
    assert kwargs["task"] == "translate"
    assert kwargs["language"] is None


def test_transcribe_task_honours_language_hint(stub_fw):
    """task='transcribe' keeps the caller's language hint and forwards
    faster-whisper's literal 'transcribe' value."""
    transcribe, captured = stub_fw
    tx = transcribe.FasterWhisperTranscriber(model_size="tiny", language="ja", task="transcribe")
    list(tx.transcribe("dummy.wav"))
    kwargs = captured["transcribe_kwargs"]
    assert kwargs["task"] == "transcribe"
    assert kwargs["language"] == "ja"


def test_transcribe_task_auto_maps_to_none(stub_fw):
    """The codebase's 'auto' literal maps to faster-whisper language=None."""
    transcribe, captured = stub_fw
    tx = transcribe.FasterWhisperTranscriber(model_size="tiny", language="auto", task="transcribe")
    list(tx.transcribe("dummy.wav"))
    assert captured["transcribe_kwargs"]["language"] is None


def test_unknown_task_rejected(stub_fw):
    transcribe, _ = stub_fw
    with pytest.raises(ValueError, match="task"):
        transcribe.FasterWhisperTranscriber(model_size="tiny", task="summarize")


def test_payload_records_task_and_detected_language(stub_fw, tmp_path):
    """transcript.json must record the task that actually ran AND the detected
    source language/probability, so 'English because source was English' is
    distinguishable from 'English translation of originally-Japanese audio'."""
    transcribe, captured = stub_fw
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"\0" * 4)
    out = tmp_path / "transcript.json"
    payload = transcribe.transcribe_to_json(str(audio), str(out), model="tiny")
    assert payload["task"] == "translate_to_english"
    assert payload["language_hint"] == "auto"  # the default hint
    assert payload["detected_language"] == "ja"  # stub detects Japanese
    assert payload["detected_language_probability"] == pytest.approx(0.97)
    # Persisted copy matches the returned payload.
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert on_disk["task"] == "translate_to_english"
    assert on_disk["detected_language"] == "ja"


def test_payload_transcribe_opt_out_records_task(stub_fw, tmp_path):
    transcribe, _ = stub_fw
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"\0" * 4)
    payload = transcribe.transcribe_to_json(
        str(audio), str(tmp_path / "t.json"), model="tiny",
        language="ja", task="transcribe",
    )
    assert payload["task"] == "transcribe"
    assert payload["language_hint"] == "ja"


def test_cli_defaults_to_translate_and_auto(stub_fw, tmp_path, monkeypatch):
    """The CLI's own defaults must be --task translate_to_english / --lang auto."""
    transcribe, _ = stub_fw
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"\0" * 4)
    out = tmp_path / "t.json"
    monkeypatch.setattr(sys, "argv", ["transcribe", str(audio), str(out), "--model", "tiny"])
    transcribe.main()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["task"] == "translate_to_english"
    assert payload["language_hint"] == "auto"
