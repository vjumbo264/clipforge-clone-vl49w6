#!/usr/bin/env python3
"""
Transcribe an audio (or video) file to a timestamped transcript.json using
faster-whisper running locally on CPU. No external APIs, no API keys.

The transcription is behind a small `Transcriber` protocol so a different
backend (e.g. a hosted API) could be dropped in later, but the default and
only shipped implementation is local faster-whisper.

Usage:
    python transcribe.py <input_audio_or_video> <output_json>
                         [--model base|small|tiny] [--lang auto|en|ja|...]
"""
import argparse
import json
import os
import sys
import time
from typing import Iterable, Protocol


class Segment(dict):
    """Just a typed dict-ish shape: {id, start, end, text}."""


class Transcriber(Protocol):
    def transcribe(self, path: str) -> Iterable[Segment]: ...


# ---------------------------------------------------------------------------
# Segment-boundary tuning
# ---------------------------------------------------------------------------
#
# faster-whisper's VAD splits the audio into speech chunks, but Whisper's
# decoder still assigns segment "end" timestamps on the ORIGINAL timeline.
# When a segment happens to land next to a long non-speech gap (music,
# opening credits, silence), the decoder's end-timestamp can drift far past
# when speech actually stopped and effectively snap to the onset of the
# NEXT speech chunk. Downstream cut logic then treats that gap as part of
# the previous segment and produces cuts that are wildly too long.
#
# To keep segment "end" timestamps honest we:
#   1. Tighten Silero VAD so long non-speech regions are actually excluded
#      (the previous 500ms threshold was too aggressive at merging real
#      silence into speech, and no upper bound existed on merged chunks).
#   2. Ask faster-whisper for word_timestamps so each segment carries its
#      own per-word timing.
#   3. Post-process every segment: clip "end" to the last word's end time
#      (plus a small tail pad), and hard-cap the segment duration so a
#      swallowed silence gap can never survive into the transcript.

# Max amount (seconds) we allow segment "end" to sit past the last word's
# end. Anything larger is treated as absorbed non-speech and trimmed off.
MAX_TRAILING_SILENCE_S = 0.75

# Absolute cap on any single segment's duration. Real spoken sentences
# essentially never exceed this; a value larger than this is a strong
# signal that a silence/music gap has been absorbed into the segment.
MAX_SEGMENT_DURATION_S = 30.0

# If we have no word timestamps to lean on, estimate a plausible spoken
# duration from the text length and clamp to that. ~15 chars/sec is a
# comfortable upper bound for natural speech; we add a fixed floor so
# very short words still get a sensible window.
FALLBACK_CHARS_PER_SEC = 15.0
FALLBACK_MIN_DURATION_S = 1.5


def _clip_segment_end(start: float, end: float, text: str, words) -> float:
    """
    Return a corrected segment end time that reflects when speech actually
    stopped, not when the next speech began.

    - If word-level timestamps are available, snap `end` to the last word's
      end (plus a small tail pad).
    - Otherwise fall back to a text-length-based estimate.
    - In either case, enforce MAX_SEGMENT_DURATION_S as a hard ceiling.
    """
    corrected = end

    if words:
        last_word_end = None
        for w in words:
            we = getattr(w, "end", None)
            if we is None and isinstance(w, dict):
                we = w.get("end")
            if we is not None:
                last_word_end = float(we)
        if last_word_end is not None:
            # Clip trailing silence: never let `end` sit more than
            # MAX_TRAILING_SILENCE_S past the last spoken word.
            padded = last_word_end + MAX_TRAILING_SILENCE_S
            if corrected > padded:
                corrected = padded
            # And never let it be earlier than the last word actually ends.
            if corrected < last_word_end:
                corrected = last_word_end
    else:
        # No word timestamps -> estimate from text length.
        est = max(
            FALLBACK_MIN_DURATION_S,
            len((text or "").strip()) / FALLBACK_CHARS_PER_SEC,
        )
        est_end = start + est + MAX_TRAILING_SILENCE_S
        if corrected > est_end:
            corrected = est_end

    # Hard ceiling on segment duration.
    if corrected - start > MAX_SEGMENT_DURATION_S:
        corrected = start + MAX_SEGMENT_DURATION_S

    # Sanity: end must be strictly after start.
    if corrected <= start:
        corrected = start + FALLBACK_MIN_DURATION_S

    return corrected


def _verify_onnxruntime_importable() -> None:
    """
    Eagerly import onnxruntime and surface the REAL failure if it can't be
    loaded.

    Why this exists
    ---------------
    faster-whisper's VAD filter path (SileroVADModel.__init__ in
    faster_whisper/vad.py) does:

        try:
            import onnxruntime
        except ImportError as e:
            raise RuntimeError(
                "Applying the VAD filter requires the onnxruntime package"
            ) from e

    That message is technically accurate but heavily misleading in practice.
    In this project onnxruntime is ALWAYS installed (faster-whisper 1.0.3
    pulls it in, and scripts/requirements.txt pins it explicitly). So if
    the VAD filter reports it "requires the onnxruntime package", the
    real underlying cause is virtually always one of:

      - onnxruntime's C extension fails to import because it was built
        against a different NumPy major than the one resolved into the
        environment (ABI mismatch on the numpy dtype struct);
      - a manylinux tag mismatch that only lets pip pick a broken wheel.

    Doing the import here, BEFORE we hand off to faster-whisper, means the
    original traceback (e.g. "numpy.dtype size changed, may indicate binary
    incompatibility") shows up in the run log verbatim, instead of getting
    swallowed and re-raised as the vague "requires the onnxruntime package"
    message that already burned a Stage A run once.
    """
    try:
        import onnxruntime  # noqa: F401
    except Exception as e:  # noqa: BLE001 — we want to see EVERY failure mode
        # Re-raise with a message that pins the blame where it belongs, and
        # keep the original exception chained so the traceback survives.
        raise RuntimeError(
            "onnxruntime failed to import at transcription time. faster-whisper's "
            "VAD filter will hit this same failure and hide it behind a "
            "'requires the onnxruntime package' message. The underlying error "
            f"is: {type(e).__name__}: {e}. Check scripts/requirements.txt — "
            "this almost always means an ABI mismatch between the installed "
            "onnxruntime wheel and the resolved NumPy version."
        ) from e


class FasterWhisperTranscriber:
    """
    Default (and currently only) transcriber.

    Uses the CTranslate2-based faster-whisper reimplementation of OpenAI
    Whisper. Runs CPU-only in the Actions runner. Model size is
    intentionally capped at 'small' — 'large' is too slow on CPU.
    """

    ALLOWED_SIZES = {"tiny", "base", "small"}

    def __init__(self, model_size: str = "base", language: str = "auto"):
        if model_size not in self.ALLOWED_SIZES:
            raise ValueError(
                f"model_size must be one of {sorted(self.ALLOWED_SIZES)} (CPU perf cap), got {model_size!r}"
            )
        # Import lazily so the module is importable without the dep for tests.
        from faster_whisper import WhisperModel  # type: ignore

        # Verify onnxruntime is actually usable BEFORE we build the model.
        # If it isn't, we want the real traceback here, not the misleading
        # "VAD filter requires the onnxruntime package" that would surface
        # a few lines later inside faster-whisper.
        _verify_onnxruntime_importable()

        print(f"Loading faster-whisper model: {model_size} (CPU, int8)", flush=True)
        t0 = time.time()
        self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
        print(f"Model loaded in {time.time() - t0:.1f}s", flush=True)
        self.language = None if language == "auto" else language

    def transcribe(self, path: str) -> Iterable[Segment]:
        print(f"Transcribing {path} (language={self.language or 'auto-detect'})", flush=True)

        # VAD parameters tuned to keep long non-speech gaps OUT of segments.
        #
        # - min_silence_duration_ms: 1000ms (up from 500ms). 500ms treated
        #   natural inter-sentence pauses as "still speech" and let the
        #   decoder merge across them; 1000ms is closer to the library
        #   default (2000ms) but keeps sensitivity for tight dialogue.
        # - speech_pad_ms: 200ms (down from the library default 400ms).
        #   Less padding means less silence tacked onto each speech chunk.
        # - max_speech_duration_s: 30s. Prevents VAD from merging a long
        #   run of quasi-speech into one huge chunk that the decoder
        #   would then have to segment on its own.
        # - min_speech_duration_ms: 250ms. Drops sub-quarter-second blips
        #   that are almost always non-speech noise.
        # - threshold: 0.5 (Silero default) — explicit for clarity.
        vad_parameters = {
            "threshold": 0.5,
            "min_speech_duration_ms": 250,
            "min_silence_duration_ms": 1000,
            "speech_pad_ms": 200,
            "max_speech_duration_s": 30.0,
        }

        segments, info = self.model.transcribe(
            path,
            language=self.language,
            beam_size=5,
            vad_filter=True,
            vad_parameters=vad_parameters,
            # word_timestamps=True gives us per-word timing we can use to
            # clip the segment "end" back to the last actual spoken word,
            # instead of trusting the decoder's silence-swallowing end.
            word_timestamps=True,
            # Suppress "trailing silence hallucinations" that Whisper is
            # known to emit — those are exactly the kind of ghost content
            # that made bad ends look plausible.
            condition_on_previous_text=False,
        )
        print(
            f"Detected language: {info.language} (prob={info.language_probability:.2f}), "
            f"duration={info.duration:.1f}s",
            flush=True,
        )

        i = 0
        for seg in segments:
            raw_start = float(seg.start)
            raw_end = float(seg.end)
            text = (seg.text or "").strip()
            words = getattr(seg, "words", None)

            corrected_end = _clip_segment_end(raw_start, raw_end, text, words)

            if corrected_end < raw_end - 0.01:
                # Log corrections so bad-boundary regressions are visible
                # in the run log without needing to re-diff transcripts.
                print(
                    f"  [vad-fix] seg {i}: end {raw_end:.2f}s -> {corrected_end:.2f}s "
                    f"(text={text[:40]!r})",
                    flush=True,
                )

            yield Segment(
                id=i,
                start=round(raw_start, 3),
                end=round(corrected_end, 3),
                text=text,
            )
            i += 1


def build_default_transcriber(model_size: str, language: str) -> Transcriber:
    """Single choke point for choosing the backend. Swap here if ever needed."""
    return FasterWhisperTranscriber(model_size=model_size, language=language)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input_path")
    ap.add_argument("output_json")
    ap.add_argument("--model", default="base", choices=sorted(FasterWhisperTranscriber.ALLOWED_SIZES))
    ap.add_argument("--lang", default="auto")
    args = ap.parse_args()

    if not os.path.exists(args.input_path):
        print(f"Input not found: {args.input_path}", file=sys.stderr)
        sys.exit(2)

    tx = build_default_transcriber(args.model, args.lang)
    segs: list[Segment] = []
    t0 = time.time()
    for seg in tx.transcribe(args.input_path):
        segs.append(seg)
        if seg["id"] % 25 == 0:
            print(f"  [{seg['start']:8.2f}s] {seg['text'][:80]}", flush=True)
    elapsed = time.time() - t0

    duration = segs[-1]["end"] if segs else 0.0
    payload = {
        "backend": "faster-whisper",
        "model": args.model,
        "language_hint": args.lang,
        "audio_duration_seconds": duration,
        "generated_at_epoch": int(time.time()),
        "segments": segs,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.output_json)) or ".", exist_ok=True)
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(
        f"Wrote {len(segs)} segments spanning {duration:.1f}s to {args.output_json} "
        f"in {elapsed:.1f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
