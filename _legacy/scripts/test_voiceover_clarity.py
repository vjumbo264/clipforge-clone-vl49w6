#!/usr/bin/env python3
"""Integration checks for ClipForge Edge TTS defaults and voiceover mastering."""
from __future__ import annotations

import importlib.util
import math
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("voice", ROOT / "generate_voiceover.py")
assert spec and spec.loader
voice = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = voice
spec.loader.exec_module(voice)

cut_spec = importlib.util.spec_from_file_location("cut", ROOT / "cut_and_produce.py")
assert cut_spec and cut_spec.loader
cut = importlib.util.module_from_spec(cut_spec)
sys.modules[cut_spec.name] = cut
cut_spec.loader.exec_module(cut)

# A changing-amplitude speech-like tone is sufficient to exercise every ffmpeg
# filter without requiring a network call or committing generated audio.
frames = []
for index in range(voice.SAMPLE_RATE_HZ * 2):
    t = index / voice.SAMPLE_RATE_HZ
    amplitude = 0.12 if t < 0.7 else (0.42 if t < 1.35 else 0.2)
    value = int(amplitude * 32767 * math.sin(2 * math.pi * 220 * t))
    frames.append(value.to_bytes(2, byteorder="little", signed=True))
pcm = b"".join(frames)

assert voice.DEFAULT_TTS_VOICE == "en-US-AndrewNeural"
assert len(voice.TTS_VOICE_CATALOG) == 12
assert set(voice.TTS_VOICE_CATALOG) == {
    "en-US-AndrewNeural", "en-US-BrianNeural", "en-US-ChristopherNeural",
    "en-US-EricNeural", "en-US-GuyNeural", "en-US-RogerNeural",
    "en-US-AvaNeural", "en-US-AriaNeural", "en-US-JennyNeural", "en-US-MichelleNeural",
    "en-NG-AbeoNeural", "en-NG-EzinneNeural",
}

with tempfile.TemporaryDirectory(prefix="clipforge_clarity_test_") as temp_dir:
    root = Path(temp_dir)
    default_settings = voice.load_tts_settings(root / "missing.json")
    assert default_settings.voice == voice.DEFAULT_TTS_VOICE
    saved_settings = root / "tts_settings.json"
    saved_settings.write_text('{"version": 1, "voice": "en-US-AvaNeural"}', encoding="utf-8")
    assert voice.load_tts_settings(saved_settings).voice == "en-US-AvaNeural"
    saved_settings.write_text('{"version": 1, "voice": "not-a-safe-voice"}', encoding="utf-8")
    assert voice.load_tts_settings(saved_settings).voice == voice.DEFAULT_TTS_VOICE

    raw_path = root / "raw.wav"
    processed_path = Path(temp_dir) / "processed.wav"
    raw_duration = voice.write_wav(raw_path, pcm)
    metadata = voice.post_process_voiceover_wav(raw_path, processed_path)
    processed_duration = voice.wav_duration_seconds(processed_path)
    assert processed_path.is_file() and processed_path.stat().st_size > 44
    assert abs(processed_duration - raw_duration) < 0.03
    assert metadata["preset"] == "speech_clarity_v1"
    assert metadata["target_integrated_lufs"] == -16.0
    assert "two-pass EBU R128 loudnorm" in metadata["filters"]
    with wave.open(str(processed_path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == voice.SAMPLE_RATE_HZ

    # 24,011 samples are 1.000458…s. A millisecond manifest rounds that
    # down to 1.000s, which historically let atrim remove eleven input
    # samples; Stage B must instead retain its exact post-resample tail.
    tail_path = Path(temp_dir) / "sub_millisecond_tail.wav"
    tail_frames = voice.SAMPLE_RATE_HZ + 11
    voice.write_wav(tail_path, b"\0\0" * tail_frames)
    tail_duration, output_frames = cut.final_wav_timing(str(tail_path))
    assert output_frames == tail_frames * 2
    assert tail_duration == output_frames / int(cut.AAC_SAMPLE_RATE)
    assert tail_duration > round(tail_frames / voice.SAMPLE_RATE_HZ, 3)

print("PASS: Edge TTS catalog defaults, voiceover clarity pass, and Stage B preserve exact final-WAV timing")

# The subtitle timing source must share the final video's 48 kHz reconciled
# timeline. A wrong header or a stretched duration used to let cinematic
# captions disappear and later resume without failing Stage B.
def write_timing_wav(path: Path, sample_rate: int, seconds: float) -> None:
    frame_count = int(round(sample_rate * seconds))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\0\0" * frame_count)


with tempfile.TemporaryDirectory(prefix="clipforge_caption_timeline_") as temp_dir:
    root = Path(temp_dir)
    valid = root / "valid_48k.wav"
    write_timing_wav(valid, int(cut.AAC_SAMPLE_RATE), 2.0)
    assert cut.validate_merged_voiceover_timeline(str(valid), 2.0) == 2.0

    wrong_rate = root / "wrong_rate.wav"
    write_timing_wav(wrong_rate, 16000, 2.0)
    try:
        cut.validate_merged_voiceover_timeline(str(wrong_rate), 2.0)
        raise AssertionError("Expected wrong subtitle sample rate to fail")
    except ValueError as exc:
        assert "sample rate" in str(exc)

    stretched = root / "stretched_48k.wav"
    write_timing_wav(stretched, int(cut.AAC_SAMPLE_RATE), 6.0)
    try:
        cut.validate_merged_voiceover_timeline(str(stretched), 2.0)
        raise AssertionError("Expected stretched subtitle timeline to fail")
    except ValueError as exc:
        assert "duration" in str(exc)

print("PASS: subtitle timing WAV must remain 48 kHz and match the reconciled video timeline")

with tempfile.TemporaryDirectory(prefix="clipforge_merged_voiceover_") as temp_dir:
    root = Path(temp_dir)
    first = root / "voiceover_01.wav"
    second = root / "voiceover_02.wav"
    # Input narration is 24 kHz, matching Edge TTS output before Stage B's
    # reconciliation to the 48 kHz final-video timeline.
    voice.write_wav(first, b"\0\0" * voice.SAMPLE_RATE_HZ)
    voice.write_wav(second, b"\0\0" * voice.SAMPLE_RATE_HZ)
    merged = root / "voiceover.wav"
    plan = [
        {"audio_samples": int(cut.AAC_SAMPLE_RATE), "video_seconds": 1.0},
        {"audio_samples": int(cut.AAC_SAMPLE_RATE), "video_seconds": 1.0},
    ]
    cut.write_merged_voiceover([str(first), str(second)], plan, str(merged))
    assert cut.validate_merged_voiceover_timeline(str(merged), 2.0) == 2.0
    with wave.open(str(merged), "rb") as wav:
        assert wav.getframerate() == int(cut.AAC_SAMPLE_RATE)
        assert wav.getnframes() == 2 * int(cut.AAC_SAMPLE_RATE)

print("PASS: merged subtitle voiceover preserves the 48 kHz reconciled frame timeline")
