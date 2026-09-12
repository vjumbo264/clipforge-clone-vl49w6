#!/usr/bin/env python3
"""Deterministic checks for the active Edge TTS voiceover configuration."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("voice", ROOT / "generate_voiceover.py")
assert spec and spec.loader
voice = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = voice
spec.loader.exec_module(voice)

assert voice.TTS_PRESET_NAME == "edge_commentary_clear_neutral"
assert voice.DEFAULT_TTS_VOICE == "en-US-AndrewNeural"
assert voice.SAMPLE_RATE_HZ == 24000
assert voice.SAMPLE_WIDTH_BYTES == 2
assert voice.CHANNELS == 1
assert voice.VOICE_CLARITY_PRESET_NAME == "speech_clarity_v1"
assert voice.VOICE_CLARITY_TARGET_I_LUFS == -16.0
assert voice.VOICE_CLARITY_TARGET_TP_DBTP == -1.5
assert len(voice.TTS_VOICE_CATALOG) == 12
assert set(voice.TTS_VOICE_CATALOG) == {
    "en-US-AndrewNeural", "en-US-BrianNeural", "en-US-ChristopherNeural",
    "en-US-EricNeural", "en-US-GuyNeural", "en-US-RogerNeural",
    "en-US-AvaNeural", "en-US-AriaNeural", "en-US-JennyNeural",
    "en-US-MichelleNeural", "en-NG-AbeoNeural", "en-NG-EzinneNeural",
}
assert voice.TTS_VOICE_CATALOG["en-NG-AbeoNeural"]["label"] == "Abeo"
assert voice.TTS_VOICE_CATALOG["en-NG-EzinneNeural"]["label"] == "Ezinne"

with tempfile.TemporaryDirectory() as temp_dir:
    settings_path = Path(temp_dir) / "tts_settings.json"
    assert voice.load_tts_settings(settings_path).voice == voice.DEFAULT_TTS_VOICE
    settings_path.write_text(json.dumps({"version": 1, "voice": "en-US-AvaNeural"}), encoding="utf-8")
    assert voice.load_tts_settings(settings_path).voice == "en-US-AvaNeural"
    settings_path.write_text(json.dumps({"version": 1, "voice": "not-supported"}), encoding="utf-8")
    assert voice.load_tts_settings(settings_path).voice == voice.DEFAULT_TTS_VOICE

captured: dict[str, object] = {}

class FakeCommunicate:
    def __init__(self, **kwargs: object) -> None:
        captured.update(kwargs)

    async def save(self, destination: str) -> None:
        Path(destination).write_bytes(b"edge-mp3-transport")

voice.edge_tts.Communicate = FakeCommunicate
with tempfile.TemporaryDirectory() as temp_dir:
    output = Path(temp_dir) / "voice.mp3"
    settings = voice.TtsSettings("en-US-AvaNeural")
    asyncio.run(voice._save_edge_mp3("Clear narration.", settings, output))
    assert output.read_bytes() == b"edge-mp3-transport"
    assert captured == {
        "text": "Clear narration.",
        "voice": "en-US-AvaNeural",
        "rate": "+20%",
        "volume": "+0%",
        "pitch": "+0Hz",
    }

assert voice.TtsSettings("en-US-AndrewNeural").metadata == {
    "engine": "edge-tts",
    "voice": "en-US-AndrewNeural",
    "voice_label": "Andrew",
    "rate": "+20%",
    "volume": "+0%",
    "pitch": "+0Hz",
    "preset": "edge_commentary_clear_neutral",
}

print("PASS: Edge TTS narrator catalog, persisted settings, 24 kHz PCM contract, and speech-clarity mastering configuration")

# Stage B invokes cut_and_produce.py from the repository root. The manifest must
# therefore contain the resolved `wav` path, not merely a display filename.
with tempfile.TemporaryDirectory() as temp_dir:
    workspace = Path(temp_dir)
    production_path = workspace / "production.json"
    output_dir = workspace / "voiceover"
    production_path.write_text(json.dumps({
        "cuts": [{"start_seconds": 0, "end_seconds": 1, "voiceover_text": "Contract check."}]
    }), encoding="utf-8")
    original_argv = sys.argv[:]
    original_synthesize = voice.synthesize_edge_tts_to_wav
    original_post_process = voice.post_process_voiceover_wav

    def fake_synthesize(_text: str, _settings: object, destination: Path) -> None:
        voice.write_wav(destination, b"\x00\x00" * voice.SAMPLE_RATE_HZ)

    def fake_post_process(source: Path, destination: Path) -> dict[str, object]:
        destination.write_bytes(source.read_bytes())
        return {"preset": "test"}

    try:
        voice.synthesize_edge_tts_to_wav = fake_synthesize
        voice.post_process_voiceover_wav = fake_post_process
        sys.argv = ["generate_voiceover.py", str(production_path), str(output_dir)]
        voice.main()
    finally:
        sys.argv = original_argv
        voice.synthesize_edge_tts_to_wav = original_synthesize
        voice.post_process_voiceover_wav = original_post_process

    manifest = json.loads((output_dir / "voiceover_manifest.json").read_text(encoding="utf-8"))
    entry = manifest["cuts"][0]
    assert entry["wav"] == str((output_dir / "voiceover_01.wav").resolve())
    assert Path(entry["wav"]).is_file()
    assert entry["duration_frames"] == voice.SAMPLE_RATE_HZ

print("PASS: generated manifests preserve the resolved `wav` path required by Stage B reconciliation")
