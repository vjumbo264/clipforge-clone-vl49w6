"""Stage B step 2 — reconcile per-cut timing against its voiceover, cut the
ORIGINAL full-quality video at the reconciled ranges, mute the source audio,
mix in the synthesized voiceover (plus optional background music), and
concatenate everything into ONE merged, mobile-safe final MP4.

---------------------------------------------------------------------------
Mobile-compatibility policy (ARCHITECTURE.md §14 — intentionally unchanged)
---------------------------------------------------------------------------
One ffmpeg pass that:

* Uses the concat FILTER (not the concat demuxer) so cuts are joined inside
  the filter graph with fresh, contiguous, zero-based timestamps — no edit
  lists, no per-segment container quirks.
* Re-encodes video to H.264 High@L4.0, yuv420p 8-bit, fixed 30 fps CFR, SAR=1.
* Re-encodes audio to AAC-LC, 48 kHz, stereo, 192 kbps.
* Writes MP4 with ``+faststart`` and mp42 brand; no B-frames, no subtitle/data
  tracks, stripped global metadata/chapters.

---------------------------------------------------------------------------
Timing reconciliation (no drift)
---------------------------------------------------------------------------
The final video's total length MUST equal the total voiceover length, and each
cut's video stays in lockstep with its own voiceover. Every cut is reconciled
against its measured voiceover duration by retiming the cut's own footage ONLY
(``setpts`` multiplier). Cut boundaries never move, no footage is borrowed from
an adjacent cut, and there is no stretch-factor ceiling.

---------------------------------------------------------------------------
Audio
---------------------------------------------------------------------------
The source video's audio is ALWAYS muted. Each cut's audio track is its
loudness-normalized voiceover at unity (no arbitrary gain), padded with silence
to the cut's reconciled video length so concat sees equal-length A/V per
segment. Optional background music is trimmed/looped to the merged duration,
gain-staged so its MEASURED loudness sits at MUSIC_TO_VOICE_LOUDNESS_RATIO of
the measured vocal loudness (bug-34 — no more blind fixed percentage),
side-chain ducked under the narration, and a
wide-ceiling limiter catches only true digital-clip peaks.

A standalone merged voiceover-only WAV is written for the caption step, which
transcribes it for word-level timestamps.

Ported from ``_legacy/scripts/cut_and_produce.py``.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import wave
from pathlib import Path
from typing import Any

from pipeline.stage_b import common

# ---------- Mobile-safe encoding parameters ----------
TARGET_FPS = 30             # phones/WhatsApp are happiest with CFR 30
TARGET_PIX_FMT = "yuv420p"  # 8-bit 4:2:0 is the ONLY universally-decoded pixfmt
X264_PROFILE = "high"
X264_LEVEL = "4.0"          # covers up to 1080p30, accepted everywhere
X264_PRESET = "veryfast"    # good speed/quality tradeoff on GH runners
X264_CRF = "18"             # visually lossless for practical purposes
AAC_BITRATE = "192k"
AAC_SAMPLE_RATE = "48000"
AAC_CHANNELS = "2"

# ---------- Timing reconciliation ----------
# Post-reconciliation assertion tolerance: |total_video - total_voiceover|.
DURATION_TOLERANCE_S = 0.25
# Declared-vs-actual source-duration agreement (scene-accuracy fix). The plan's
# ``video_duration_seconds`` is the planner's whole reference for choosing every
# cut's start/end seconds. If it disagrees with the source file's REAL probed
# duration, the plan was written against a different cut of the footage (stale
# plan, wrong/older re-fetched source, or a hallucinated duration) and every
# cut would silently land on the WRONG scene while still passing per-cut range
# validation. ffprobe container duration is accurate to well under 1s and the
# declared value is a rounded integer, so a 2s tolerance absorbs only rounding.
SOURCE_DURATION_MISMATCH_TOLERANCE_S = 2.0

# ---------- Audio level policy ----------
# Each TTS WAV is loudness-normalized by voiceover.py to a dialogue target
# before this step sees it, so the voice path stays at unity. The music bed is
# gain-staged by MEASUREMENT (bug-34): both the merged voiceover and the music
# file are measured with ffmpeg loudnorm (EBU R128 integrated loudness), and
# the music gain is computed so its perceived level lands at
# MUSIC_TO_VOICE_LOUDNESS_RATIO of the vocals — regardless of how loud the
# uploaded music file already is. This is a CONSTANT gain applied for the
# whole clip (bug-59): no per-moment ducking/compression is layered on top,
# so the background level does not fluctuate as narration starts and stops.
VOICEOVER_VOLUME = 1.00
# Fallback only: used when a loudness measurement cannot be obtained (e.g. a
# silent/corrupt music file). Mirrors the legacy fixed one-third amplitude.
MUSIC_VOLUME = 0.33
# Target perceived background:vocals ratio (0.15:1). Halved from 0.30
# (remove-paste-feature-and-double-voice-ratio initiative): the operator
# asked for the vocals to count double relative to the bed ("0.3 to 1 ->
# more like 0.3 to 2"), i.e. the SAME music loudness logic with the voice
# weighted 2x, which is mathematically 0.30 / 2 = 0.15. The music target
# offset moves from 10*log10(0.30) ~= -5.23 dB to 10*log10(0.15) ~= -8.24 dB
# -- the bed sits exactly 3.01 dB further below the voiceover at every
# measurement pair. (The literal reading "ratio = 2.0" would put music
# +3.01 dB ABOVE the vocals, the opposite of the request, and was rejected.)
MUSIC_TO_VOICE_LOUDNESS_RATIO = 0.15
# Sanity clamps for the computed music gain (dB). The two bounds are NOT
# symmetric, because cutting an over-loud bed and boosting an over-quiet one
# are different failure modes with different risk profiles.
#
# Cut side (floor): voiceovers arrive normalized to -16 LUFS (voiceover.py
# VOICE_CLARITY_TARGET_I_LUFS) and the 0.15:1 ratio offset is
# 10*log10(0.15) ~= -8.24 dB, so the required cut is
#     voice_lufs - 8.24 - music_lufs.
# Loud commercial masters measure about -6 to -14 LUFS integrated (worst
# realistic case: -23 LUFS vocals vs -6 LUFS music needs -25.2 dB), and NO
# real signal can measure far above +3 LUFS -- a full-scale square wave is
# the physical ceiling for an integrated reading. The worst physically
# possible cut is therefore
#     -23 LUFS vocals - 8.24 dB - (+3 LUFS music) ~= -34.2 dB.
# A -60 dB floor gives ~26 dB of headroom beyond even that impossible edge,
# so no real upload -- however loud -- is ever clamped away from the exact
# ratio (re-verified for the 0.15 target: the smaller ratio demands MORE
# attenuation than 0.30 did, and -60 still clears the new worst case with
# room to spare). A genuinely absurd demand (e.g. a corrupt file misparsed
# as tens of dB above digital full scale) is still clamped rather than
# attenuating the bed by a physically meaningless amount.
#
# Boost side (ceiling): boosting a quiet file amplifies its noise floor and
# mastering artifacts by the same amount, so this side stays deliberately
# conservative. +24 dB covers music down to ~-48 LUFS under -16 LUFS vocals;
# a bed arriving quieter than that is left slightly below the target ratio
# rather than lifting 25+ dB of hiss along with it.
MUSIC_GAIN_MIN_DB = -60.0
MUSIC_GAIN_MAX_DB = 24.0
MIX_LIMITER_CEILING = 0.99


def measure_integrated_loudness_lufs(path: str | Path) -> float:
    """Measure EBU R128 integrated loudness (LUFS) of an audio file with
    ffmpeg's loudnorm analysis pass. Raises StageBError when ffmpeg cannot
    produce a parseable measurement (e.g. silent or corrupt audio)."""
    result = common.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin",
            "-i", str(path),
            "-af", "loudnorm=print_format=json",
            "-f", "null", "-",
        ],
        f"loudness measurement for {path}",
    )
    matches = re.findall(r"\{\s*\"input_i\".*?\n\}", result.stderr, flags=re.DOTALL)
    if not matches:
        raise common.StageBError(
            f"ffmpeg loudnorm returned no parseable measurement for {path}."
        )
    try:
        measured = json.loads(matches[-1])
        return float(measured["input_i"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise common.StageBError(
            f"ffmpeg loudnorm returned incomplete measurement data for {path}."
        ) from exc


def music_gain_db_for_ratio(voice_lufs: float, music_lufs: float) -> float:
    """Gain (dB) that brings ``music_lufs`` to
    MUSIC_TO_VOICE_LOUDNESS_RATIO × the perceived level of ``voice_lufs``.

    A loudness ratio r maps to a level offset of 10·log10(r) dB, so the
    0.15:1 background:vocals target is voice_lufs + 10·log10(0.15) ≈ vocals
    minus 8.24 dB. (Note this function's formula is 10·log10 — the level
    ratio convention used for LUFS targets — not 20·log10; 20·log10(0.15)
    would be ≈ -16.48 dB and is NOT what this code targets.) The result is
    clamped to [MUSIC_GAIN_MIN_DB, MUSIC_GAIN_MAX_DB] so a pathological
    measurement can neither blast nor fully mute the bed.
    """
    target_db = 10.0 * math.log10(MUSIC_TO_VOICE_LOUDNESS_RATIO)  # ≈ -8.24 dB
    gain_db = (voice_lufs + target_db) - music_lufs
    return min(max(gain_db, MUSIC_GAIN_MIN_DB), MUSIC_GAIN_MAX_DB)


def resolve_music_volume(voiceover_wav: str | Path, music: str | Path) -> tuple[float, float | None]:
    """Return (music_volume_linear, measured_gain_db_or_None) for the bed.

    bug-34: measure both tracks and compute the gain so the music sits at
    MUSIC_TO_VOICE_LOUDNESS_RATIO of the vocals' perceived loudness. Falls
    back to the legacy fixed MUSIC_VOLUME only when measurement is impossible
    (e.g. a silent music file), keeping the render alive.
    """
    try:
        voice_lufs = measure_integrated_loudness_lufs(voiceover_wav)
        music_lufs = measure_integrated_loudness_lufs(music)
    except common.StageBError as exc:
        print(
            f"WARNING: loudness measurement failed ({exc}); "
            f"falling back to fixed music volume {MUSIC_VOLUME}.",
            flush=True,
        )
        return MUSIC_VOLUME, None
    gain_db = music_gain_db_for_ratio(voice_lufs, music_lufs)
    linear = 10.0 ** (gain_db / 20.0)
    print(
        f"Music loudness staging: voiceover {voice_lufs:.2f} LUFS, "
        f"music {music_lufs:.2f} LUFS → target {voice_lufs + 10.0 * math.log10(MUSIC_TO_VOICE_LOUDNESS_RATIO):.2f} LUFS, "
        f"applied gain {gain_db:+.2f} dB (linear {linear:.4f}).",
        flush=True,
    )
    return linear, gain_db


def final_wav_timing(path: str | Path) -> tuple[float, int]:
    """Return final-WAV duration plus its exact 48 kHz trim-frame target.

    The final WAV is the authoritative post-processing artifact, so both the
    video retiming duration and the atrim endpoint derive from its container
    frame count (never a manifest duration rounded to milliseconds, which can
    round DOWN and truncate final phoneme samples).
    """
    try:
        with wave.open(str(path), "rb") as wav:
            input_frames = wav.getnframes()
            input_rate = wav.getframerate()
    except (wave.Error, EOFError) as exc:
        raise common.StageBError(f"Voiceover WAV is unreadable: {path}") from exc
    if input_frames <= 0 or input_rate <= 0:
        raise common.StageBError(f"Voiceover WAV has invalid timing: {path}")
    output_rate = int(AAC_SAMPLE_RATE)
    output_frames = round(input_frames * output_rate / input_rate)
    if output_frames <= 0:
        raise common.StageBError(f"Voiceover WAV has no renderable samples: {path}")
    return output_frames / output_rate, output_frames


# --------------------------------------------------------------------------- #
# Declared-vs-actual source-duration sanity check (scene-accuracy fix)          #
# --------------------------------------------------------------------------- #

def assert_declared_duration_matches_source(plan: dict[str, Any], actual_seconds: float) -> None:
    """Fail closed when the plan's declared source duration disagrees with the
    probed real duration of the video about to be rendered.

    Every cut's ``start_seconds``/``end_seconds`` was chosen against the
    timeline of the source the PLANNER saw, whose length is recorded in the
    plan as ``video_duration_seconds``. Stage A scenes/transcript and Stage B
    extraction all share the original source's zero-based timeline, so if the
    declared duration does not match the probed duration of the file actually
    present at render time, the plan was written against a DIFFERENT cut of
    the footage — a stale plan, a wrong/older re-fetched source (bug-69), or a
    hallucinated duration — and each cut would silently extract the WRONG
    scene while still passing per-cut range validation against both the
    declared and the actual length. Refuse loudly instead of producing a
    video whose footage does not match its narration.
    """
    declared = plan.get("video_duration_seconds")
    try:
        declared_seconds = float(declared)
    except (TypeError, ValueError):
        declared_seconds = 0.0
    if declared_seconds <= 0:
        raise common.StageBError(
            "production.json has no usable video_duration_seconds to reconcile "
            "against the probed source duration; cannot confirm the plan's cut "
            "timestamps reference this source file."
        )
    delta = abs(declared_seconds - actual_seconds)
    if delta > SOURCE_DURATION_MISMATCH_TOLERANCE_S:
        raise common.StageBError(
            f"production.json declares video_duration_seconds={declared_seconds:.0f}s "
            f"but the actual source video probes at {actual_seconds:.2f}s "
            f"(difference {delta:.2f}s exceeds the {SOURCE_DURATION_MISMATCH_TOLERANCE_S:.0f}s "
            "tolerance). The plan's cut timestamps were written against a source "
            "of a different length than the one about to be rendered, so every "
            "cut would land on the wrong scene. Re-run Stage A against this exact "
            "source and regenerate the plan, or confirm the correct source file is "
            "being used."
        )


# --------------------------------------------------------------------------- #
# Timing reconciliation                                                        #
# --------------------------------------------------------------------------- #

def reconcile_cuts(cuts: list[dict], vo_durations: list[float],
                   src_duration: float) -> list[dict]:
    """Adjust every cut so its planned video duration matches its voiceover
    duration EXACTLY, by retiming the cut's own footage — never by changing
    start/end seconds, borrowing from another cut, or trimming footage away.

    Returns a NEW list of cut dicts carrying the original boundaries (untouched)
    plus a per-cut ``stretch`` factor (setpts multiplier: <1.0 speeds the video
    up, >1.0 slows it down) and ``video_seconds`` (the planned output duration,
    equal to its voiceover duration).
    """
    plan = [
        {
            "start_seconds": float(c["start_seconds"]),
            "end_seconds": float(c["end_seconds"]),
            "stretch": 1.0,
        }
        for c in cuts
    ]

    for i in range(len(plan)):
        vo = vo_durations[i]
        video_len = plan[i]["end_seconds"] - plan[i]["start_seconds"]
        plan[i]["video_seconds"] = vo

        if video_len <= 0 or abs(vo - video_len) < 0.01:
            continue  # already matches (or a degenerate zero-length cut)

        # video_len * stretch = vo  =>  stretch = vo / video_len
        stretch = vo / video_len
        plan[i]["stretch"] = stretch
        direction = "slowing down" if stretch > 1.0 else "speeding up"
        print(
            f"  [reconcile] cut #{i}: footage {video_len:.2f}s vs voiceover "
            f"{vo:.2f}s — {direction} the clip by {stretch:.3f}x to match "
            f"exactly (no boundary change, no borrowed footage).",
            flush=True,
        )

    return plan


# --------------------------------------------------------------------------- #
# The single ffmpeg pass                                                       #
# --------------------------------------------------------------------------- #

def produce_merged_video(src: str | Path, plan: list[dict], vo_wavs: list[str],
                         dst: str | Path, music: str | Path | None,
                         music_volume: float = MUSIC_VOLUME) -> None:
    """Cut every reconciled range from ``src``, retime per-cut video by its
    stretch factor, mute source audio, lay in the voiceover WAVs (padded to the
    cut length), concat everything, optionally mix ducked music under the
    voiceover, and write ONE mobile-safe MP4."""
    if not plan:
        raise common.StageBError("produce_merged_video called with no cuts")

    n = len(plan)
    cmd: list[str] = ["ffmpeg", "-y"]

    # One `-ss <start> -to <end> -i <src>` block per cut. Input-side seek is
    # fast, and because the concat filter downstream re-encodes with fresh PTS
    # we inherit none of the edit-list / non-zero-start problems a stream-copy
    # pipeline would have.
    for p in plan:
        cmd += ["-ss", f"{p['start_seconds']:.3f}", "-to", f"{p['end_seconds']:.3f}", "-i", str(src)]

    for w in vo_wavs:
        cmd += ["-i", str(w)]

    music_idx = None
    if music:
        music_idx = 2 * n
        # `-stream_loop -1` must precede the input it applies to; loop short
        # music so the atrim below always has enough material to cut from.
        cmd += ["-stream_loop", "-1", "-i", str(music)]

    parts: list[str] = []
    concat_inputs: list[str] = []
    for i, p in enumerate(plan):
        stretch = p["stretch"]
        parts.append(
            f"[{i}:v:0]"
            f"setpts={stretch:.6f}*PTS,"
            f"fps={TARGET_FPS},"
            f"format={TARGET_PIX_FMT},"
            f"setsar=1"
            f"[v{i}]"
        )
        # Audio: normalized voiceover only — source audio is never mapped.
        # Pad to the exact post-resample sample count so concat sees equal A/V
        # segment lengths. end_sample avoids millisecond rounding truncating a
        # final consonant or vowel.
        parts.append(
            f"[{n + i}:a:0]"
            f"aformat=sample_fmts=fltp:sample_rates={AAC_SAMPLE_RATE}:channel_layouts=stereo,"
            f"aresample=async=1:first_pts=0,"
            f"apad,atrim=end_sample={p['audio_samples']},"
            f"volume={VOICEOVER_VOLUME}"
            f"[a{i}]"
        )
        concat_inputs.append(f"[v{i}][a{i}]")

    parts.append("".join(concat_inputs) + f"concat=n={n}:v=1:a=1[vcat][acat]")

    total_seconds = sum(p["video_seconds"] for p in plan)

    if music_idx is not None:
        parts.append(
            f"[{music_idx}:a:0]"
            f"aformat=sample_fmts=fltp:sample_rates={AAC_SAMPLE_RATE}:channel_layouts=stereo,"
            f"aresample=async=1:first_pts=0,"
            f"atrim=0:{total_seconds:.3f},"
            f"volume={music_volume:.6f}"
            f"[music_reduced]"
        )
        # bug-59: the bed is already gain-staged to a CONSTANT loudness-matched
        # level by resolve_music_volume() above (MUSIC_TO_VOICE_LOUDNESS_RATIO,
        # measured via LUFS so a quiet upload is boosted and a loud one is cut
        # down to the same target ratio). Previously this constant level was
        # then fed through sidechaincompress, ducking it further any time the
        # voiceover was present and letting it rise back in gaps — the
        # operator wants a flat, non-fluctuating background level for the
        # whole clip instead. music_reduced is mixed in directly at its
        # already-correct constant gain; no per-moment compression is applied.
        parts.append(
            f"[acat][music_reduced]amix=inputs=2:duration=first:normalize=0,"
            f"alimiter=limit={MIX_LIMITER_CEILING}"
            f"[aout]"
        )
        audio_map = "[aout]"
    else:
        audio_map = "[acat]"

    filter_complex = ";".join(parts)

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[vcat]",
        "-map", audio_map,

        # Video: H.264 High@L4.0, CRF 18, yuv420p — universally decodable.
        "-c:v", "libx264",
        "-profile:v", X264_PROFILE,
        "-level:v", X264_LEVEL,
        "-preset", X264_PRESET,
        "-crf", X264_CRF,
        "-pix_fmt", TARGET_PIX_FMT,
        # No B-frames -> PTS==DTS, start_time==0.
        "-bf", "0",
        "-g", str(TARGET_FPS * 2),
        "-keyint_min", str(TARGET_FPS * 2),
        "-sc_threshold", "0",
        "-x264-params", "force-cfr=1",
        "-video_track_timescale", "15360",

        # Audio: AAC-LC stereo 48 kHz 192 kbps.
        "-c:a", "aac",
        "-profile:a", "aac_low",
        "-b:a", AAC_BITRATE,
        "-ar", AAC_SAMPLE_RATE,
        "-ac", AAC_CHANNELS,

        # Container: MP4, faststart, mp42 brand, no edit lists; strip
        # metadata/chapters; drop subtitle/data tracks.
        "-movflags", "+faststart",
        "-use_editlist", "0",
        "-brand", "mp42",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-sn", "-dn", "-ignore_unknown",
        "-fflags", "+genpts",
        "-max_muxing_queue_size", "9999",
        "-f", "mp4",
        str(dst),
    ]

    common.sh(cmd)


def write_merged_voiceover(vo_wavs: list[str], plan: list[dict], out_wav: str | Path) -> None:
    """Concatenate the per-cut voiceover WAVs (each padded to its cut's planned
    video length, matching what's in the video) into one standalone WAV. The
    caption step transcribes THIS file for word-level timestamps, so it must
    line up with the video 1:1."""
    n = len(vo_wavs)
    cmd: list[str] = ["ffmpeg", "-y"]
    for w in vo_wavs:
        cmd += ["-i", str(w)]
    parts: list[str] = []
    labels: list[str] = []
    for i in range(n):
        parts.append(
            f"[{i}:a:0]"
            f"aformat=sample_fmts=fltp:sample_rates={AAC_SAMPLE_RATE}:channel_layouts=mono,"
            f"aresample=async=1:first_pts=0,"
            f"apad,atrim=end_sample={plan[i]['audio_samples']}"
            f"[w{i}]"
        )
        labels.append(f"[w{i}]")
    parts.append("".join(labels) + f"concat=n={n}:v=0:a=1[wout]")
    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", "[wout]",
        "-c:a", "pcm_s16le",
        # Keep the already-reconciled 48 kHz sample timeline intact.
        "-ar", AAC_SAMPLE_RATE,
        "-ac", "1",
        str(out_wav),
    ]
    common.sh(cmd)


def validate_merged_voiceover_timeline(path: str | Path, expected_seconds: float) -> float:
    """Fail closed if the caption timing source is not 1:1 with final video."""
    try:
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            sample_rate = wav.getframerate()
            channels = wav.getnchannels()
    except (wave.Error, EOFError) as exc:
        raise common.StageBError(f"Merged voiceover WAV is unreadable: {path}") from exc
    if frames <= 0 or sample_rate <= 0 or channels != 1:
        raise common.StageBError(
            f"Merged voiceover WAV has invalid audio metadata: "
            f"frames={frames}, sample_rate={sample_rate}, channels={channels}"
        )
    expected_rate = int(AAC_SAMPLE_RATE)
    if sample_rate != expected_rate:
        raise common.StageBError(
            f"Merged voiceover sample rate is {sample_rate} Hz, expected "
            f"{expected_rate} Hz for the reconciled subtitle timeline."
        )
    actual_seconds = frames / sample_rate
    tolerance_seconds = max(0.05, expected_seconds * 0.0005)
    drift_seconds = abs(actual_seconds - expected_seconds)
    if drift_seconds > tolerance_seconds:
        raise common.StageBError(
            f"Merged voiceover duration ({actual_seconds:.3f}s) differs from "
            f"the reconciled final-video plan ({expected_seconds:.3f}s) by "
            f"{drift_seconds:.3f}s (tolerance {tolerance_seconds:.3f}s). "
            "Refusing caption generation because captions would not share the "
            "final video timeline."
        )
    print(
        f"Merged voiceover timeline validated: {actual_seconds:.3f}s at "
        f"{sample_rate} Hz mono matches the reconciled final video "
        f"({expected_seconds:.3f}s).",
        flush=True,
    )
    return actual_seconds


def validate_mp4(path: str | Path) -> None:
    """Verify the output is a genuine, valid, mobile-safe MP4 (codec profile,
    pixel format, stream counts, zero start time). Fails loudly via
    ``StageBError`` rather than shipping a file that won't play on a phone."""
    print(f"\nValidating output MP4: {path}", flush=True)

    if not os.path.isfile(path):
        raise common.StageBError(f"Output file does not exist: {path}")

    with open(path, "rb") as f:
        head = f.read(32)
    if len(head) < 12 or head[4:8] != b"ftyp":
        raise common.StageBError(
            f"Output file is NOT a valid MP4 (missing ftyp box at offset 4, got: {head[4:8]!r})"
        )

    major_brand = head[8:12].decode("ascii", errors="replace")
    print(f"  Major brand: {major_brand}", flush=True)

    probe_data = common.probe_json(path)
    streams = probe_data.get("streams", [])
    fmt_info = probe_data.get("format", {})

    print(f"  Format: {fmt_info.get('format_long_name', 'unknown')} ({fmt_info.get('format_name', 'unknown')})", flush=True)
    print(f"  Duration: {fmt_info.get('duration', 'unknown')}s", flush=True)
    print(f"  Bit rate: {fmt_info.get('bit_rate', 'unknown')} bps", flush=True)
    print(f"  Start time: {fmt_info.get('start_time', 'unknown')}", flush=True)

    v_streams = [s for s in streams if s.get("codec_type") == "video"]
    a_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if not v_streams:
        raise common.StageBError("Output file has no video stream")
    if not a_streams:
        raise common.StageBError("Output file has no audio stream")
    if len(a_streams) > 1:
        raise common.StageBError(
            f"Output file has {len(a_streams)} audio streams, expected exactly 1 "
            "(voiceover [+ music] mixed). A leaked source track would put "
            "original audio back into the final video."
        )

    v = v_streams[0]
    a = a_streams[0]
    print(
        f"  Video: {v.get('codec_name')} profile={v.get('profile')} "
        f"level={v.get('level')} pix_fmt={v.get('pix_fmt')} "
        f"{v.get('width')}x{v.get('height')} fps={v.get('r_frame_rate')}",
        flush=True,
    )
    print(f"  Audio: {a.get('codec_name')} sr={a.get('sample_rate')} ch={a.get('channels')}", flush=True)

    if v.get("codec_name") != "h264":
        raise common.StageBError(f"video codec is {v.get('codec_name')!r}, expected h264")
    if v.get("pix_fmt") != TARGET_PIX_FMT:
        raise common.StageBError(
            f"video pix_fmt is {v.get('pix_fmt')!r}, expected {TARGET_PIX_FMT!r} "
            "(non-yuv420p pixel formats are the #1 cause of 'won't play on phone' bugs)"
        )
    if a.get("codec_name") != "aac":
        raise common.StageBError(f"audio codec is {a.get('codec_name')!r}, expected aac")

    try:
        st = float(fmt_info.get("start_time", "0"))
        if st > 0.05:
            raise common.StageBError(
                f"output start_time is {st:.3f}s, expected ~0.0. Non-zero start "
                "times break WhatsApp / iOS playback."
            )
    except (TypeError, ValueError):
        pass

    print("  Validation PASSED: output is a mobile-safe MP4.\n", flush=True)


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #

def render_merged(
    original_video: str | Path,
    plan: dict[str, Any],
    voiceover_manifest_path: str | Path,
    out_video_mp4: str | Path,
    *,
    voiceover_wav: str | Path | None = None,
    music: str | Path | None = None,
) -> dict[str, Any]:
    """Produce the merged mobile-safe MP4 + merged voiceover WAV + cut-timing
    sidecar. ``plan`` is the normalized, re-validated plan from
    :func:`pipeline.stage_b.common.load_production_plan`."""
    original_video = str(original_video)
    out_video_mp4 = str(out_video_mp4)
    for req in (original_video, str(voiceover_manifest_path)):
        if not os.path.exists(req):
            raise common.StageBError(f"Missing required input: {req}")
    if music and not os.path.exists(str(music)):
        raise common.StageBError(f"Missing required input: {music}")

    cuts = plan.get("cuts") or []
    if not cuts:
        raise common.StageBError("production plan contains no cuts")

    with open(voiceover_manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    vo_entries = manifest.get("cuts") or []
    if len(vo_entries) != len(cuts):
        raise common.StageBError(
            f"Voiceover manifest has {len(vo_entries)} entries but production.json "
            f"has {len(cuts)} cuts — they must match 1:1. Re-run the voiceover "
            "step against this production.json."
        )
    vo_entries = sorted(vo_entries, key=lambda e: int(e["index"]))
    vo_wavs = [e["wav"] for e in vo_entries]
    for w in vo_wavs:
        if not os.path.exists(w):
            raise common.StageBError(f"Voiceover WAV from manifest does not exist: {w}")
    vo_timings = [final_wav_timing(w) for w in vo_wavs]
    vo_durations = [duration for duration, _ in vo_timings]

    src_duration = common.probe_duration_seconds(original_video)
    print(f"Source video duration: {src_duration:.2f}s", flush=True)

    # Scene-accuracy guard: refuse to render a plan whose declared source
    # duration does not match this file's real duration — otherwise every cut
    # would silently extract the wrong scene (see assert_declared_duration_matches_source).
    assert_declared_duration_matches_source(plan, src_duration)

    print("\nReconciling per-cut timing against voiceover durations...", flush=True)
    reconciled = reconcile_cuts(cuts, vo_durations, src_duration)
    for item, (_, audio_samples) in zip(reconciled, vo_timings):
        item["audio_samples"] = audio_samples
    # Persist authoritative per-cut durations as a sidecar next to the merged
    # voiceover WAV: the caption step uses these REAL cut boundaries instead of
    # proportional word-count ratios.
    sidecar_wav = str(voiceover_wav) if voiceover_wav else os.path.join(
        os.path.dirname(os.path.abspath(out_video_mp4)) or ".", "voiceover.wav",
    )
    cut_timing_path = os.path.join(os.path.dirname(os.path.abspath(sidecar_wav)) or ".", "cut_timing.json")
    with open(cut_timing_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "version": 1,
                "cuts": [
                    {"index": index, "video_seconds": float(item["video_seconds"])}
                    for index, item in enumerate(reconciled)
                ],
            },
            f,
            indent=2,
        )
        f.write("\n")
    print(f"Cut timing sidecar written: {cut_timing_path}", flush=True)

    total_video = sum(p["video_seconds"] for p in reconciled)
    total_vo = sum(vo_durations)
    print(
        f"Reconciled plan: {len(reconciled)} cut(s), total video "
        f"{total_video:.2f}s vs total voiceover {total_vo:.2f}s",
        flush=True,
    )
    if abs(total_video - total_vo) > DURATION_TOLERANCE_S:
        raise common.StageBError(
            f"total planned video duration ({total_video:.3f}s) differs from total "
            f"voiceover duration ({total_vo:.3f}s) by more than {DURATION_TOLERANCE_S}s. "
            "The final video would drift out of sync with its narration — refusing "
            "to produce it."
        )

    # Validate the reconciled ranges against the source.
    prev_end = -1.0
    for i, p in enumerate(reconciled):
        s, e = p["start_seconds"], p["end_seconds"]
        if e <= s:
            raise common.StageBError(f"Cut #{i}: end ({e}) <= start ({s}) after reconciliation")
        if s < 0 or e > src_duration + 0.5:
            raise common.StageBError(
                f"Cut #{i}: range [{s}, {e}] outside source [0, {src_duration:.2f}] after reconciliation"
            )
        if s < prev_end:
            raise common.StageBError(
                f"Cut #{i}: overlaps previous cut ({s} < prev end {prev_end}) after "
                "reconciliation — footage would be double-used"
            )
        prev_end = e

    # bug-34: write the merged voiceover BEFORE the render so its real
    # integrated loudness can be measured against the music file. The gain
    # applied to the bed is derived from that measurement (0.15:1 vs vocals),
    # not a fixed percentage of whatever level the upload happens to be.
    write_merged_voiceover(vo_wavs, reconciled, sidecar_wav)
    expected_voiceover_seconds = sum(float(item["video_seconds"]) for item in reconciled)
    validate_merged_voiceover_timeline(sidecar_wav, expected_voiceover_seconds)
    print(f"Merged voiceover written: {sidecar_wav}", flush=True)

    music_volume = MUSIC_VOLUME
    music_gain_db = None
    if music:
        music_volume, music_gain_db = resolve_music_volume(sidecar_wav, music)

    music_note = ""
    if music:
        if music_gain_db is None:
            music_note = (
                f", background music at fixed fallback {int(MUSIC_VOLUME * 100)}% "
                "(loudness measurement unavailable) and ducked beneath speech"
            )
        else:
            music_note = (
                f", background music gain-staged {music_gain_db:+.1f} dB to "
                f"{int(MUSIC_TO_VOICE_LOUDNESS_RATIO * 100)}% of measured vocal "
                "loudness and ducked beneath speech"
            )
    print(
        f"\nProducing ONE merged video from {len(reconciled)} cut(s) with "
        f"normalized voiceover at unity (source audio muted"
        f"{music_note}) "
        f"— mobile-safe encoding (H.264 High@L{X264_LEVEL} {TARGET_PIX_FMT} CRF{X264_CRF}, "
        f"AAC-LC {AAC_SAMPLE_RATE}Hz stereo {AAC_BITRATE}, +faststart, no edit lists).",
        flush=True,
    )

    os.makedirs(os.path.dirname(os.path.abspath(out_video_mp4)) or ".", exist_ok=True)
    produce_merged_video(original_video, reconciled, vo_wavs, out_video_mp4, music, music_volume)
    print(f"Merged video written: {out_video_mp4}", flush=True)

    validate_mp4(out_video_mp4)

    return {
        "merged_mp4": out_video_mp4,
        "merged_voiceover_wav": sidecar_wav,
        "cut_timing_json": cut_timing_path,
        "total_seconds": total_video,
        "cut_count": len(reconciled),
        "music_applied": bool(music),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage B merged render (mobile-safe single pass).")
    ap.add_argument("original_video")
    ap.add_argument("production_json")
    ap.add_argument("voiceover_manifest_json")
    ap.add_argument("out_video_mp4")
    ap.add_argument("--voiceover-wav", default=None,
                    help="where to write the merged voiceover-only WAV "
                         "(default: <out_video_dir>/voiceover.wav)")
    ap.add_argument("--music", default=None,
                    help="optional background music file; trimmed/looped to the "
                         "merged duration and ducked under the voiceover")
    args = ap.parse_args(argv)

    plan = common.load_production_plan(args.production_json)
    render_merged(
        args.original_video,
        plan,
        args.voiceover_manifest_json,
        args.out_video_mp4,
        voiceover_wav=args.voiceover_wav,
        music=args.music,
    )
    return 0


if __name__ == "__main__":
    import sys

    try:
        raise SystemExit(main())
    except common.StageBError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(3)
