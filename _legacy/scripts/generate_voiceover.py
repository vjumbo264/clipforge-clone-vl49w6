"""Render ClipForge Stage B narration through Microsoft Edge TTS.

The generator consumes ``production.json`` and writes one 24 kHz mono PCM WAV
per cut plus ``voiceover_manifest.json``. Its public command-line contract and
post-processing remain stable for ``cut_and_produce.py`` and the cinematic
subtitle renderer. The persisted, non-secret narrator selection is read from
``branding/tts_settings.json``; a safe neutral U.S. default is used until an
operator saves a preference in Settings.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import edge_tts

# Stable Stage B audio contract. The downstream reconciler and subtitle pass
# rely on this exact layout, not on Edge TTS's MP3 transport format.
SAMPLE_RATE_HZ = 24000
SAMPLE_WIDTH_BYTES = 2
CHANNELS = 1

# The existing speech-specific master is retained unchanged so swapping TTS
# engines does not also change loudness, intelligibility, or mixing behavior.
VOICE_CLARITY_PRESET_NAME = "speech_clarity_v1"
VOICE_CLARITY_TARGET_I_LUFS = -16.0
VOICE_CLARITY_TARGET_LRA_LU = 7.0
VOICE_CLARITY_TARGET_TP_DBTP = -1.5
VOICE_CLARITY_PRE_FILTERS = (
    "highpass=f=70:p=2,"
    "equalizer=f=3000:t=q:w=1.1:g=1.5,"
    "acompressor=threshold=0.125:ratio=1.5:attack=15:release=120:makeup=1.0"
)
VOICE_CLARITY_FINAL_LIMITER = "alimiter=limit=0.84:attack=5:release=50:level=0"

# Curated in Settings. The names are Edge's public neural voice identifiers;
# production accepts only this fixed set, never a browser-supplied arbitrary
# endpoint or SSML payload.
TTS_VOICE_CATALOG: dict[str, dict[str, str]] = {
    "en-US-AndrewNeural": {
        "label": "Andrew", "gender": "Male", "style": "Warm, confident, conversational",
    },
    "en-US-BrianNeural": {
        "label": "Brian", "gender": "Male", "style": "Approachable, casual, sincere",
    },
    "en-US-ChristopherNeural": {
        "label": "Christopher", "gender": "Male", "style": "Reliable, authoritative narrator",
    },
    "en-US-EricNeural": {
        "label": "Eric", "gender": "Male", "style": "Rational, measured narrator",
    },
    "en-US-GuyNeural": {
        "label": "Guy", "gender": "Male", "style": "Energetic news-style narrator",
    },
    "en-US-RogerNeural": {
        "label": "Roger", "gender": "Male", "style": "Lively narrator",
    },
    "en-US-AvaNeural": {
        "label": "Ava", "gender": "Female", "style": "Expressive, caring, conversational",
    },
    "en-US-AriaNeural": {
        "label": "Aria", "gender": "Female", "style": "Positive, confident narrator",
    },
    "en-US-JennyNeural": {
        "label": "Jenny", "gender": "Female", "style": "Friendly, considerate narrator",
    },
    "en-US-MichelleNeural": {
        "label": "Michelle", "gender": "Female", "style": "Friendly, polished narrator",
    },
    "en-NG-AbeoNeural": {
        "label": "Abeo", "gender": "Male", "style": "Nigerian English, friendly and positive",
    },
    "en-NG-EzinneNeural": {
        "label": "Ezinne", "gender": "Female", "style": "Nigerian English, friendly and positive",
    },
}
DEFAULT_TTS_VOICE = "en-US-AndrewNeural"
TTS_SETTINGS_PATH = Path("branding/tts_settings.json")
TTS_PRESET_NAME = "edge_commentary_clear_neutral"
# ClipForge's existing narrator direction was about 188 WPM / 1.2x normal.
# Edge's public prosody control expresses that same intended brisk pacing.
TTS_RATE = "+20%"
TTS_VOLUME = "+0%"
TTS_PITCH = "+0Hz"
EDGE_TTS_MAX_ATTEMPTS = 3
EDGE_TTS_BACKOFF_S = 2.0


@dataclass(frozen=True)
class TtsSettings:
    voice: str
    rate: str = TTS_RATE
    volume: str = TTS_VOLUME
    pitch: str = TTS_PITCH

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "engine": "edge-tts",
            "voice": self.voice,
            "voice_label": TTS_VOICE_CATALOG[self.voice]["label"],
            "rate": self.rate,
            "volume": self.volume,
            "pitch": self.pitch,
            "preset": TTS_PRESET_NAME,
        }


def _run_ffmpeg(command: list[str], description: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required for ClipForge voiceover processing.") from exc
    except subprocess.CalledProcessError as exc:
        tail = exc.stderr[-1200:] if exc.stderr else "no ffmpeg diagnostic"
        raise RuntimeError(f"ffmpeg failed during {description}: {tail}") from exc


def load_tts_settings(path: Path = TTS_SETTINGS_PATH) -> TtsSettings:
    """Load the non-secret saved narrator preference, failing safely to Andrew."""
    if not path.is_file():
        print(f"No saved Edge TTS preference at {path}; using default {DEFAULT_TTS_VOICE}.", flush=True)
        return TtsSettings(DEFAULT_TTS_VOICE)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Invalid Edge TTS preference at {path}: {exc}; using default {DEFAULT_TTS_VOICE}.", flush=True)
        return TtsSettings(DEFAULT_TTS_VOICE)
    voice = document.get("voice") if isinstance(document, dict) else None
    if voice not in TTS_VOICE_CATALOG:
        print(f"Unsupported saved Edge TTS voice; using default {DEFAULT_TTS_VOICE}.", flush=True)
        return TtsSettings(DEFAULT_TTS_VOICE)
    return TtsSettings(voice)


async def _save_edge_mp3(text: str, settings: TtsSettings, destination: Path) -> None:
    communicator = edge_tts.Communicate(
        text=text,
        voice=settings.voice,
        rate=settings.rate,
        volume=settings.volume,
        pitch=settings.pitch,
    )
    await communicator.save(str(destination))


def synthesize_edge_tts_to_wav(text: str, settings: TtsSettings, output_path: Path) -> None:
    """Synthesize one narration line and normalize it to the stable WAV contract."""
    spoken_text = str(text or "").strip()
    if not spoken_text:
        raise RuntimeError("Voiceover text is empty.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    with tempfile.TemporaryDirectory(prefix="clipforge_edge_tts_") as temp_dir:
        mp3_path = Path(temp_dir) / "voice.mp3"
        for attempt in range(1, EDGE_TTS_MAX_ATTEMPTS + 1):
            try:
                if mp3_path.exists():
                    mp3_path.unlink()
                asyncio.run(_save_edge_mp3(spoken_text, settings, mp3_path))
                if not mp3_path.is_file() or mp3_path.stat().st_size == 0:
                    raise RuntimeError("Edge TTS returned an empty audio response.")
                _run_ffmpeg(
                    [
                        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(mp3_path),
                        "-ar", str(SAMPLE_RATE_HZ), "-ac", str(CHANNELS),
                        "-c:a", "pcm_s16le", str(output_path),
                    ],
                    "Edge TTS WAV normalization",
                )
                if not output_path.is_file() or output_path.stat().st_size <= 44:
                    raise RuntimeError("Edge TTS WAV normalization produced no audio.")
                return
            except Exception as error:  # Provider and transport errors vary by version.
                last_error = error
                if attempt < EDGE_TTS_MAX_ATTEMPTS:
                    print(
                        f"Edge TTS attempt {attempt}/{EDGE_TTS_MAX_ATTEMPTS} failed for "
                        f"{settings.voice}; retrying in {EDGE_TTS_BACKOFF_S:.0f}s.",
                        flush=True,
                    )
                    time.sleep(EDGE_TTS_BACKOFF_S * attempt)
        raise RuntimeError(
            f"Edge TTS failed after {EDGE_TTS_MAX_ATTEMPTS} attempts for {settings.voice}: {last_error}"
        ) from last_error


# ---------------------------------------------------------------------------
# WAV writing and speech-clarity post-processing
# ---------------------------------------------------------------------------
def write_wav(path: Path, pcm_bytes: bytes) -> float:
    """Wrap raw 24 kHz mono s16le PCM in a RIFF WAV and return its duration."""
    if len(pcm_bytes) % (SAMPLE_WIDTH_BYTES * CHANNELS) != 0:
        pcm_bytes = pcm_bytes[: len(pcm_bytes) - (len(pcm_bytes) % (SAMPLE_WIDTH_BYTES * CHANNELS))]
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav.setframerate(SAMPLE_RATE_HZ)
        wav.writeframes(pcm_bytes)
    return (len(pcm_bytes) // (SAMPLE_WIDTH_BYTES * CHANNELS)) / SAMPLE_RATE_HZ


def wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        if wav.getframerate() <= 0:
            raise RuntimeError(f"Invalid WAV sample rate in {path}.")
        return wav.getnframes() / wav.getframerate()


def wav_frame_count(path: Path) -> int:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes()


def _measure_loudness_after_preprocessing(input_path: Path) -> dict[str, float]:
    filter_chain = (
        f"{VOICE_CLARITY_PRE_FILTERS},"
        f"loudnorm=I={VOICE_CLARITY_TARGET_I_LUFS}:"
        f"LRA={VOICE_CLARITY_TARGET_LRA_LU}:"
        f"TP={VOICE_CLARITY_TARGET_TP_DBTP}:print_format=json"
    )
    result = _run_ffmpeg(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(input_path), "-af", filter_chain, "-f", "null", "-"],
        "voiceover loudness measurement",
    )
    matches = re.findall(r"\{\s*\"input_i\".*?\n\}", result.stderr, flags=re.DOTALL)
    if not matches:
        raise RuntimeError("ffmpeg loudnorm did not return parseable JSON measurement data.")
    try:
        measured = json.loads(matches[-1])
        return {
            "measured_I": float(measured["input_i"]),
            "measured_LRA": float(measured["input_lra"]),
            "measured_TP": float(measured["input_tp"]),
            "measured_thresh": float(measured["input_thresh"]),
            "offset": float(measured["target_offset"]),
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("ffmpeg loudnorm returned incomplete measurement data.") from exc


def post_process_voiceover_wav(input_path: Path, output_path: Path) -> dict[str, object]:
    """Apply ClipForge's calibrated speech-clarity and loudness pass."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required for voiceover clarity processing.")
    measurement = _measure_loudness_after_preprocessing(input_path)
    loudnorm = (
        f"loudnorm=I={VOICE_CLARITY_TARGET_I_LUFS}:"
        f"LRA={VOICE_CLARITY_TARGET_LRA_LU}:"
        f"TP={VOICE_CLARITY_TARGET_TP_DBTP}:"
        f"measured_I={measurement['measured_I']}:"
        f"measured_LRA={measurement['measured_LRA']}:"
        f"measured_TP={measurement['measured_TP']}:"
        f"measured_thresh={measurement['measured_thresh']}:"
        f"offset={measurement['offset']}:linear=true:print_format=summary"
    )
    filter_chain = f"{VOICE_CLARITY_PRE_FILTERS},{loudnorm},{VOICE_CLARITY_FINAL_LIMITER}"
    temporary_path = output_path.with_name(f".{output_path.stem}.processed.tmp.wav")
    try:
        _run_ffmpeg(
            [
                "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(input_path),
                "-af", filter_chain, "-ar", str(SAMPLE_RATE_HZ), "-ac", str(CHANNELS),
                "-c:a", "pcm_s16le", str(temporary_path),
            ],
            "voiceover clarity processing",
        )
        if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg completed without producing processed voiceover audio.")
        temporary_path.replace(output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return {
        "preset": VOICE_CLARITY_PRESET_NAME,
        "target_integrated_lufs": VOICE_CLARITY_TARGET_I_LUFS,
        "target_lra_lu": VOICE_CLARITY_TARGET_LRA_LU,
        "target_true_peak_dbtp": VOICE_CLARITY_TARGET_TP_DBTP,
        "filters": [
            "highpass 70 Hz, 2 poles",
            "equalizer +1.5 dB at 3 kHz (Q 1.1)",
            "acompressor 1.5:1 above -18 dBFS",
            "two-pass EBU R128 loudnorm",
            "true-peak safety limiter at -1.5 dBFS equivalent",
        ],
        "measurement": measurement,
    }


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: generate_voiceover.py <production.json> <out_dir>", file=sys.stderr)
        raise SystemExit(2)
    production_path = Path(sys.argv[1]).resolve()
    output_dir = Path(sys.argv[2]).resolve()
    if not production_path.is_file():
        print(f"FATAL: {production_path} not found.", file=sys.stderr)
        raise SystemExit(2)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        production = json.loads(production_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"FATAL: could not read production.json: {exc}")
    cuts = production.get("cuts") if isinstance(production, dict) else None
    if not isinstance(cuts, list) or not cuts:
        raise SystemExit("FATAL: production.json has no cuts.")

    settings = load_tts_settings()
    print(
        f"Rendering {len(cuts)} voiceover clip(s) with Edge TTS "
        f"voice={settings.voice} rate={settings.rate}.",
        flush=True,
    )
    manifest: dict[str, Any] = {
        "version": 2,
        **settings.metadata,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "channels": CHANNELS,
        "sample_width_bytes": SAMPLE_WIDTH_BYTES,
        "post_processing_preset": VOICE_CLARITY_PRESET_NAME,
        "cuts": [],
    }
    for index, cut in enumerate(cuts, start=1):
        if not isinstance(cut, dict):
            raise SystemExit(f"FATAL: cut {index} is not an object.")
        narration = cut.get("voiceover_text")
        if not isinstance(narration, str) or not narration.strip():
            raise SystemExit(f"FATAL: cut {index} has no voiceover_text.")
        raw_path = output_dir / f".voiceover_{index:02d}.raw.wav"
        final_path = output_dir / f"voiceover_{index:02d}.wav"
        print(f"  [cut {index:02d}] Edge TTS synthesis", flush=True)
        try:
            synthesize_edge_tts_to_wav(narration, settings, raw_path)
            mastering = post_process_voiceover_wav(raw_path, final_path)
            duration = wav_duration_seconds(final_path)
            frames = wav_frame_count(final_path)
        finally:
            if raw_path.exists():
                raw_path.unlink()
        manifest["cuts"].append(
            {
                "index": index,
                # This resolved path is the stable Stage B contract consumed by
                # cut_and_produce.py, which runs from the repository root.
                "wav": str(final_path),
                "duration_seconds": duration,
                "duration_frames": frames,
                "voiceover_text": narration,
                "mastering": mastering,
            }
        )
    manifest_path = output_dir / "voiceover_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {manifest_path}.", flush=True)


if __name__ == "__main__":
    main()
