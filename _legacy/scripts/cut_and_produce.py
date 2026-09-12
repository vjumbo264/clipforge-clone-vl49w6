#!/usr/bin/env python3
"""
Stage B step 2 of 3: reconcile per-cut timing against its voiceover, cut
the ORIGINAL full-quality video at the reconciled ranges, mute the source
audio, mix in the synthesized voiceover (plus optional background music),
and concatenate everything into ONE merged, mobile-safe final MP4.

Replaces cut_and_concat.py. There are no more per-scene scene_NN.mp4
outputs and no more output.txt for an external commentary agent — the
voiceover_text in production.json IS the final script, synthesized by
generate_voiceover.py immediately before this step runs.

---------------------------------------------------------------------------
Mobile-compatibility policy (carried over from cut_and_concat.py — the
lessons below are hard-won, do not relitigate them)
---------------------------------------------------------------------------
Earlier versions tried to be clever: stream-copy when the source codecs
"looked" MP4-compatible, re-encode otherwise. In practice that produced
files that opened on desktop players but refused to play natively on
phones (iOS Photos, Android Gallery, WhatsApp) — the user was forced to
re-convert on-device before they'd play.

The specific problems that path introduced:

  1. Stream-copying with `-ss` before `-i` snaps to the nearest prior
     keyframe and writes `edts`/`elst` (edit list) atoms into each
     segment to hide the pre-roll. Mobile hardware decoders and
     WhatsApp routinely ignore edit lists, which desyncs A/V or shows
     black frames at the head of every cut.

  2. Even when the source is labelled H.264 + AAC, the actual sub-flavor
     (High 10 / 10-bit / 4:2:2, Main 10, level 5.1+, 48 kHz surround
     AAC, unusual SAR / anamorphic, negative CTS offsets) is regularly
     rejected by phone hardware decoders while still parsing fine in
     ffprobe and VLC.

  3. Concat-demuxer + stream-copy of per-segment MP4s inherits every
     one of those problems and additionally leaves timestamps that
     don't start at 0, tripping strict players (WhatsApp in particular
     refuses videos whose first PTS is not zero).

  4. Embedded SUBTITLE/data tracks in the source can leak into the MP4
     as a third `bin_data`/`text` stream, and enabled B-frames surface
     as `start_time=0.0667` / `has_b_frames=2`. Both break phones AND
     strict PC players. Hence: `-bf 0` (no B-frames, PTS==DTS,
     start_time==0), no `nal-hrd=cbr` (invalid without VBV), stripped
     global metadata/chapters, and `-sn -dn -ignore_unknown` so no
     subtitle/data track reaches the MP4.

The fix: forget the branching. Do ONE ffmpeg pass that:

  - Uses the concat filter (not the concat demuxer) so cuts are joined
    inside the filter graph with fresh, contiguous, zero-based
    timestamps — no edit lists, no per-segment container quirks.
  - Re-encodes video to H.264 High@L4.0, yuv420p 8-bit, with a fixed
    30 fps CFR and SAR=1 — the exact profile every phone / WhatsApp /
    Instagram / TikTok hardware decoder is guaranteed to accept.
  - Re-encodes audio to AAC-LC, 48 kHz, stereo, 192 kbps — the safe
    audio flavor for all mobile players.
  - Writes an MP4 with `+faststart` (moov at the front, for instant
    playback while streaming) and mp42 brand.
  - CRF 18 keeps the result visually indistinguishable from the source
    while still guaranteeing a phone-playable file.

---------------------------------------------------------------------------
Timing reconciliation (no drift)
---------------------------------------------------------------------------
The final video's total length MUST equal the total voiceover length, and
each cut's video must stay in lockstep with its own voiceover. Before any
ffmpeg work, every cut is reconciled against its measured voiceover
duration by retiming the cut's own footage ONLY:

  - Voiceover longer than the cut's footage: slow the cut's video down
    (video-only PTS retime via setpts) so its duration exactly matches
    the voiceover.
  - Voiceover shorter than the cut's footage: speed the cut's video up
    (same mechanism, factor > 1) so its duration exactly matches the
    voiceover.

The cut's `start_seconds`/`end_seconds` boundaries are NEVER moved and no
footage is ever borrowed from an adjacent cut or trimmed away — every
frame of the originally selected clip stays in the final video, and the
full voiceover is always heard, because retiming (not reselecting
footage) is the only mechanism used to match durations. There is no
stretch-factor ceiling: however far the durations differ, the video is
slowed or sped up by exactly that amount.

After reconciliation the script asserts, loudly, that the total planned
video duration matches the total voiceover duration within tolerance.

---------------------------------------------------------------------------
Audio
---------------------------------------------------------------------------
The source video's audio is ALWAYS muted. Each cut's audio track is its
synthesized voiceover, amplified by VOICEOVER_VOLUME (4.00x, ~+12 dB) at
the per-cut node so the boost survives concat and the music amix, and
padded with silence to the cut's (reconciled) video length so the concat
filter sees equal-length A/V per segment. If a background music file was
uploaded for the job it is trimmed (or looped) to exactly the merged
video duration, scaled DOWN by MUSIC_VOLUME (0.33x, ~-9.6 dB, i.e.
roughly one third of its uploaded level, inside the requested 30-35%
range), and mixed UNDER the (already-amplified) voiceover with amix
normalize=0. A final wide-ceiling alimiter (MIX_LIMITER_CEILING =
0.99 full-scale) catches only true digital-clip peaks on loud overlaps;
it does NOT act as a compressor and does NOT undo either track's gain.
The final MP4 has exactly one audio track: voiceover + music, no source
audio.

A standalone merged voiceover-only WAV is also written for the sole
cinematic subtitle step, which transcribes it for word-level timestamps.

Usage:
    python cut_and_produce.py <original_video> <production_json>
                              <voiceover_manifest_json> <out_video_mp4>
                              [--voiceover-wav <merged_voiceover.wav>]
                              [--music <music_file>]
"""
import argparse
import json
import os
import subprocess
import sys
import wave


# ---------- Mobile-safe encoding parameters ----------
# Kept as module-level constants so they're easy to audit / tune in one place.
TARGET_FPS = 30            # phones/WhatsApp are happiest with CFR 30
TARGET_PIX_FMT = "yuv420p" # 8-bit 4:2:0 is the ONLY universally-decoded pixfmt
X264_PROFILE = "high"
X264_LEVEL = "4.0"         # covers up to 1080p30, accepted everywhere
X264_PRESET = "veryfast"   # good speed/quality tradeoff on GH runners
X264_CRF = "18"            # visually lossless for practical purposes
AAC_BITRATE = "192k"
AAC_SAMPLE_RATE = "48000"
AAC_CHANNELS = "2"

# ---------- Timing reconciliation ----------
# Every cut is retimed (slowed or sped up) to exactly match its voiceover
# duration. There is no borrowing from adjacent cuts, no boundary
# trimming, and no stretch-factor ceiling — see the module docstring.
# Post-reconciliation assertion tolerance: |total_video - total_voiceover|.
DURATION_TOLERANCE_S = 0.25
# Defense-in-depth lower bound for a reconciled edit relative to the original
# production-plan timeline. The prompt asks for at least 90% narration
# coverage at the configured TTS pace; 75% leaves normal synthesis variation
# headroom while refusing a dramatic collapse such as 47s delivered for 187s
# planned. This is deliberately independent of the exact-match guard above.
MIN_RECONCILED_TO_PLANNED_RATIO = 0.75

# ---------- Audio level policy ----------
# Each TTS WAV is loudness-normalized by generate_voiceover.py to a dialogue
# target before this script sees it.  Therefore the voice path stays at unity:
# no arbitrary voice gain is used to fight the music.
#
# The music bed is reduced to one third of its uploaded amplitude and is then
# side-chain ducked only while narration is present.  The static 0.33 gain is
# the requested 30–35% baseline; ducking is deliberately on the MUSIC branch
# (not the dialogue branch), preventing a loud master from masking speech.
# When there is no narration, music remains at the requested baseline.
VOICEOVER_VOLUME = 1.00
MUSIC_VOLUME = 0.33
MUSIC_DUCK_THRESHOLD = 0.015
MUSIC_DUCK_RATIO = 10
MUSIC_DUCK_ATTACK_MS = 20
MUSIC_DUCK_RELEASE_MS = 350
MIX_LIMITER_CEILING = 0.99


def sh(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True)


def probe_duration(path: str) -> float:
    out = subprocess.check_output(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        text=True,
    ).strip()
    return float(out)


def final_wav_timing(path: str) -> tuple[float, int]:
    """Return final-WAV duration plus its exact 48 kHz trim-frame target.

    Stage B has historically trusted a manifest duration rounded to milliseconds.
    That can round *down* and make atrim remove final phoneme samples. The final
    WAV is the authoritative post-processing artifact, so derive both the video
    retiming duration and the atrim endpoint from its container frame count.
    """
    try:
        with wave.open(path, "rb") as wav:
            input_frames = wav.getnframes()
            input_rate = wav.getframerate()
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"Voiceover WAV is unreadable: {path}") from exc
    if input_frames <= 0 or input_rate <= 0:
        raise ValueError(f"Voiceover WAV has invalid timing: {path}")
    output_rate = int(AAC_SAMPLE_RATE)
    output_frames = round(input_frames * output_rate / input_rate)
    if output_frames <= 0:
        raise ValueError(f"Voiceover WAV has no renderable samples: {path}")
    return output_frames / output_rate, output_frames


# ---------------------------------------------------------------------------
# Timing reconciliation
# ---------------------------------------------------------------------------
def reconcile_cuts(cuts: list[dict], vo_durations: list[float],
                   src_duration: float) -> list[dict]:
    """
    Adjust every cut so its planned video duration matches its voiceover
    duration EXACTLY, by retiming the cut's own footage — never by
    changing start_seconds/end_seconds, borrowing from another cut, or
    trimming footage away. Returns a NEW list of cut dicts carrying the
    original start_seconds/end_seconds (untouched) plus a per-cut
    `stretch` factor (setpts multiplier: <1.0 speeds the video up, >1.0
    slows it down, 1.0 = already an exact match) and `video_seconds`
    (the planned output duration for that cut, equal to its voiceover
    duration).

    `src_duration` is accepted for interface compatibility with callers
    but is no longer used: boundaries never move, so there is nothing to
    clamp against the source video's total length.
    """
    n = len(cuts)
    plan = [
        {
            "start_seconds": float(c["start_seconds"]),
            "end_seconds": float(c["end_seconds"]),
            "stretch": 1.0,
        }
        for c in cuts
    ]

    for i in range(n):
        vo = vo_durations[i]
        video_len = plan[i]["end_seconds"] - plan[i]["start_seconds"]
        plan[i]["video_seconds"] = vo

        if video_len <= 0 or abs(vo - video_len) < 0.01:
            # Already matches (or a degenerate zero-length cut, left
            # alone rather than dividing by zero).
            continue

        # setpts={stretch}*PTS makes the clip's own duration become
        # video_len * stretch. To land exactly on the voiceover length:
        #   video_len * stretch = vo   =>   stretch = vo / video_len
        # stretch > 1.0 spreads the same frames over MORE time (slows
        # the clip down); stretch < 1.0 compresses them into LESS time
        # (speeds the clip up). Applied unconditionally, with no cap.
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


def assert_reconciled_duration_coverage(cuts: list[dict], plan: list[dict]) -> None:
    """Reject narration-driven duration collapse before rendering output.

    ``reconcile_cuts`` intentionally makes each output video duration equal
    its voiceover duration, so comparing those two reconciled totals alone is
    tautological. This guard instead compares every reconciled duration and
    the total reconciled duration against the original production.json ranges.
    """
    if len(cuts) != len(plan):
        raise ValueError("planned cuts and reconciled plan must match 1:1")

    planned_durations = [
        float(cut["end_seconds"]) - float(cut["start_seconds"])
        for cut in cuts
    ]
    reconciled_durations = [float(item["video_seconds"]) for item in plan]
    planned_total = sum(planned_durations)
    reconciled_total = sum(reconciled_durations)
    if planned_total <= 0:
        raise ValueError("production plan has no positive planned duration")

    total_ratio = reconciled_total / planned_total
    collapsed = []
    for index, (cut, planned, reconciled) in enumerate(
        zip(cuts, planned_durations, reconciled_durations), start=1
    ):
        ratio = reconciled / planned if planned > 0 else 0.0
        if planned <= 0 or ratio < MIN_RECONCILED_TO_PLANNED_RATIO:
            collapsed.append(
                f"cut #{index} [{float(cut['start_seconds']):.2f}-{float(cut['end_seconds']):.2f}s]: "
                f"planned {planned:.2f}s, voiceover/reconciled {reconciled:.2f}s ({ratio:.1%})"
            )

    if total_ratio < MIN_RECONCILED_TO_PLANNED_RATIO or collapsed:
        details = "; ".join(collapsed) if collapsed else "no individual cut below threshold"
        print(
            "ERROR: narration duration collapse: original production plan totals "
            f"{planned_total:.2f}s but reconciled voiceover-driven output totals "
            f"{reconciled_total:.2f}s ({total_ratio:.1%}); minimum allowed is "
            f"{MIN_RECONCILED_TO_PLANNED_RATIO:.0%}. Undersized cuts: {details}. "
            "Refusing to produce a truncated final video; expand the affected "
            "voiceover_text and regenerate narration.",
            file=sys.stderr,
        )
        sys.exit(3)


# ---------------------------------------------------------------------------
# The single ffmpeg pass
# ---------------------------------------------------------------------------
def produce_merged_video(src: str, plan: list[dict], vo_wavs: list[str],
                         dst: str, music: str | None) -> None:
    """
    Cut every reconciled range from `src`, retime per-cut video by its
    stretch factor, mute source audio, lay in the voiceover WAVs (padded
    to the cut length), concat everything, optionally mix ducked music
    under the voiceover, and write ONE mobile-safe MP4. See the module
    docstring for why the encoding parameters look the way they do.
    """
    if not plan:
        raise ValueError("produce_merged_video called with no cuts")

    n = len(plan)
    cmd: list[str] = ["ffmpeg", "-y"]

    # One `-ss <start> -to <end> -i <src>` block per cut. Input-side seek
    # is fast, and because the concat filter downstream re-encodes with
    # fresh PTS we inherit none of the edit-list / non-zero-start problems
    # a stream-copy pipeline would have.
    for p in plan:
        cmd += [
            "-ss", f"{p['start_seconds']:.3f}",
            "-to", f"{p['end_seconds']:.3f}",
            "-i", src,
        ]

    # One voiceover WAV input per cut.
    for w in vo_wavs:
        cmd += ["-i", w]

    # Optional music input (last).
    music_idx = None
    if music:
        music_idx = 2 * n
        cmd += ["-i", music]

    parts: list[str] = []
    concat_inputs: list[str] = []
    for i, p in enumerate(plan):
        # Video: normalize THEN retime. setpts scales presentation
        # timestamps by the stretch factor; combined with fps=TARGET_FPS
        # afterwards the frame count is resampled to the new duration, so
        # the retime is a true slow-down/speed-up, not dropped/dup frames
        # at the original duration.
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
        # final consonant or vowel; the video retime uses the same sample count.
        parts.append(
            f"[{n + i}:a:0]"
            f"aformat=sample_fmts=fltp:sample_rates={AAC_SAMPLE_RATE}:channel_layouts=stereo,"
            f"aresample=async=1:first_pts=0,"
            f"apad,atrim=end_sample={p['audio_samples']},"
            f"volume={VOICEOVER_VOLUME}"
            f"[a{i}]"
        )
        concat_inputs.append(f"[v{i}][a{i}]")

    parts.append(
        "".join(concat_inputs) + f"concat=n={n}:v=1:a=1[vcat][acat]"
    )

    total_seconds = sum(p["video_seconds"] for p in plan)

    if music_idx is not None:
        # Trim/loop music to the merged duration, reduce it to 33% of the
        # uploaded amplitude, then duck THAT already-reduced music whenever
        # normalized narration is active.  `sidechaincompress` receives music
        # as its first input and narration as its detector; its output is the
        # processed music stream that is actually mapped into the final MP4.
        # This prevents later mix stages from restoring the background level.
        parts.append(
            f"[{music_idx}:a:0]"
            f"aformat=sample_fmts=fltp:sample_rates={AAC_SAMPLE_RATE}:channel_layouts=stereo,"
            f"aresample=async=1:first_pts=0,"
            f"atrim=0:{total_seconds:.3f},"
            f"volume={MUSIC_VOLUME}"
            f"[music_reduced]"
        )
        parts.append("[acat]asplit=2[voice_mix][voice_sidechain]")
        parts.append(
            f"[music_reduced][voice_sidechain]"
            f"sidechaincompress=threshold={MUSIC_DUCK_THRESHOLD}:"
            f"ratio={MUSIC_DUCK_RATIO}:attack={MUSIC_DUCK_ATTACK_MS}:"
            f"release={MUSIC_DUCK_RELEASE_MS}:makeup=1"
            f"[music_ducked]"
        )
        parts.append(
            f"[voice_mix][music_ducked]amix=inputs=2:duration=first:normalize=0,"
            f"alimiter=limit={MIX_LIMITER_CEILING}"
            f"[aout]"
        )
        audio_map = "[aout]"
    else:
        audio_map = "[acat]"

    filter_complex = ";".join(parts)

    # Loop short music via input option rather than a filter so the atrim
    # above always has enough material to cut from.
    if music_idx is not None:
        # Insert `-stream_loop -1` before the music `-i` (it must precede
        # the input it applies to). cmd currently ends with ["-i", music].
        cmd = cmd[:-2] + ["-stream_loop", "-1", "-i", music]

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
        # No B-frames -> PTS==DTS, start_time==0 (see module docstring).
        "-bf", "0",
        # Force a keyframe every 2s and disable scene-cut extras.
        "-g", str(TARGET_FPS * 2),
        "-keyint_min", str(TARGET_FPS * 2),
        "-sc_threshold", "0",
        # force-cfr only; nal-hrd=cbr removed (invalid without VBV).
        "-x264-params", "force-cfr=1",
        # Pin a clean, phone-friendly video timebase.
        "-video_track_timescale", "15360",

        # Audio: AAC-LC stereo 48 kHz 192 kbps — the phone-safe combo.
        "-c:a", "aac",
        "-profile:a", "aac_low",
        "-b:a", AAC_BITRATE,
        "-ar", AAC_SAMPLE_RATE,
        "-ac", AAC_CHANNELS,

        # Container: MP4, faststart (moov at front), mp42 brand, no edit
        # lists. Strip global metadata + chapters; drop subtitle/data tracks.
        "-movflags", "+faststart",
        "-use_editlist", "0",
        "-brand", "mp42",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-sn", "-dn", "-ignore_unknown",

        # Sanity: fresh, zero-based timestamps.
        "-fflags", "+genpts",
        "-max_muxing_queue_size", "9999",
        "-f", "mp4",
        dst,
    ]

    sh(cmd)


def write_merged_voiceover(vo_wavs: list[str], plan: list[dict],
                           out_wav: str) -> None:
    """
    Concatenate the per-cut voiceover WAVs (each padded to its cut's
    planned video length, matching what's in the video) into one
    standalone WAV. generate_subtitles_cinematic.py transcribes THIS file
    for word-level timestamps, so it must line up with the video 1:1.
    """
    n = len(vo_wavs)
    cmd: list[str] = ["ffmpeg", "-y"]
    for w in vo_wavs:
        cmd += ["-i", w]
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
        # Keep the already-reconciled 48 kHz sample timeline intact. Whisper
        # decodes/resamples internally; writing a 16 kHz header over concat
        # frames would stretch this WAV's apparent duration and make subtitle
        # timestamps drift far beyond the final video.
        "-ar", AAC_SAMPLE_RATE,
        "-ac", "1",
        out_wav,
    ]
    sh(cmd)


def validate_merged_voiceover_timeline(path: str, expected_seconds: float) -> float:
    """Fail closed if the subtitle timing source is not 1:1 with final video.

    The subtitle renderer transcribes this WAV and uses its timestamps directly
    on the final MP4. A sample-rate/header mismatch can make a valid-looking WAV
    several times longer than the reconciled video, which presents as caption
    gaps, late resumption, or a silent cutoff. Do not allow that artifact to
    reach the renderer.
    """
    try:
        with wave.open(path, "rb") as wav:
            frames = wav.getnframes()
            sample_rate = wav.getframerate()
            channels = wav.getnchannels()
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"Merged voiceover WAV is unreadable: {path}") from exc
    if frames <= 0 or sample_rate <= 0 or channels != 1:
        raise ValueError(
            f"Merged voiceover WAV has invalid audio metadata: "
            f"frames={frames}, sample_rate={sample_rate}, channels={channels}"
        )
    expected_rate = int(AAC_SAMPLE_RATE)
    if sample_rate != expected_rate:
        raise ValueError(
            f"Merged voiceover sample rate is {sample_rate} Hz, expected "
            f"{expected_rate} Hz for the reconciled subtitle timeline."
        )
    actual_seconds = frames / sample_rate
    tolerance_seconds = max(0.05, expected_seconds * 0.0005)
    drift_seconds = abs(actual_seconds - expected_seconds)
    if drift_seconds > tolerance_seconds:
        raise ValueError(
            f"Merged voiceover duration ({actual_seconds:.3f}s) differs from "
            f"the reconciled final-video plan ({expected_seconds:.3f}s) by "
            f"{drift_seconds:.3f}s (tolerance {tolerance_seconds:.3f}s). "
            "Refusing subtitle generation because captions would not share the "
            "final video timeline."
        )
    print(
        f"Merged voiceover timeline validated: {actual_seconds:.3f}s at "
        f"{sample_rate} Hz mono matches the reconciled final video "
        f"({expected_seconds:.3f}s).",
        flush=True,
    )
    return actual_seconds


def validate_mp4(path: str) -> None:
    """
    Verify that the output file is a genuine, valid MP4 file AND that
    it uses the mobile-safe codec profile we asked for. If any of
    these checks fail, something has gone wrong upstream and shipping
    the file would risk the "won't play on phone" bug returning.
    """
    print(f"\nValidating output MP4: {path}", flush=True)

    if not os.path.isfile(path):
        print(f"ERROR: Output file does not exist: {path}", file=sys.stderr)
        sys.exit(3)

    # File signature: MP4 files have an ftyp box near the start.
    with open(path, "rb") as f:
        head = f.read(32)
    if len(head) < 12 or head[4:8] != b"ftyp":
        print(
            f"ERROR: Output file is NOT a valid MP4 "
            f"(missing ftyp box at offset 4, got: {head[4:8]!r})",
            file=sys.stderr,
        )
        sys.exit(3)

    major_brand = head[8:12].decode("ascii", errors="replace")
    print(f"  Major brand: {major_brand}", flush=True)

    probe_out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries",
            "stream=codec_type,codec_name,profile,level,pix_fmt,width,height,"
            "sample_rate,channels,r_frame_rate",
            "-show_entries", "format=format_name,format_long_name,duration,bit_rate,start_time",
            "-of", "json",
            path,
        ],
        capture_output=True,
        text=True,
    )
    if probe_out.returncode != 0:
        print(
            f"ERROR: ffprobe failed on output file:\n{probe_out.stderr}",
            file=sys.stderr,
        )
        sys.exit(3)

    probe_data = json.loads(probe_out.stdout)
    streams = probe_data.get("streams", [])
    fmt_info = probe_data.get("format", {})
    fmt_name = fmt_info.get("format_name", "unknown")

    print(f"  Format: {fmt_info.get('format_long_name', 'unknown')} ({fmt_name})", flush=True)
    print(f"  Duration: {fmt_info.get('duration', 'unknown')}s", flush=True)
    print(f"  Bit rate: {fmt_info.get('bit_rate', 'unknown')} bps", flush=True)
    print(f"  Start time: {fmt_info.get('start_time', 'unknown')}", flush=True)

    v_streams = [s for s in streams if s.get("codec_type") == "video"]
    a_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if not v_streams:
        print("ERROR: Output file has no video stream", file=sys.stderr)
        sys.exit(3)
    if not a_streams:
        print("ERROR: Output file has no audio stream", file=sys.stderr)
        sys.exit(3)
    if len(a_streams) > 1:
        print(
            f"ERROR: Output file has {len(a_streams)} audio streams, expected "
            f"exactly 1 (voiceover [+ music] mixed). A leaked source track "
            f"would put original audio back into the final video.",
            file=sys.stderr,
        )
        sys.exit(3)

    v = v_streams[0]
    a = a_streams[0]
    print(
        f"  Video: {v.get('codec_name')} "
        f"profile={v.get('profile')} "
        f"level={v.get('level')} "
        f"pix_fmt={v.get('pix_fmt')} "
        f"{v.get('width')}x{v.get('height')} "
        f"fps={v.get('r_frame_rate')}",
        flush=True,
    )
    print(
        f"  Audio: {a.get('codec_name')} "
        f"sr={a.get('sample_rate')} "
        f"ch={a.get('channels')}",
        flush=True,
    )

    # Hard checks: the codec profile MUST match what we asked for. If
    # ffmpeg silently downgraded (missing encoder, filter error, etc.)
    # we want to fail loudly rather than ship a file that won't play
    # on the user's phone.
    if v.get("codec_name") != "h264":
        print(
            f"ERROR: video codec is {v.get('codec_name')!r}, expected h264",
            file=sys.stderr,
        )
        sys.exit(3)
    if v.get("pix_fmt") != TARGET_PIX_FMT:
        print(
            f"ERROR: video pix_fmt is {v.get('pix_fmt')!r}, expected {TARGET_PIX_FMT!r} "
            f"(non-yuv420p pixel formats are the #1 cause of "
            f"'won't play on phone' bugs)",
            file=sys.stderr,
        )
        sys.exit(3)
    if a.get("codec_name") != "aac":
        print(
            f"ERROR: audio codec is {a.get('codec_name')!r}, expected aac",
            file=sys.stderr,
        )
        sys.exit(3)

    # Start-time should be at or very near zero. WhatsApp specifically
    # rejects files whose first PTS is meaningfully greater than zero.
    try:
        st = float(fmt_info.get("start_time", "0"))
        if st > 0.05:
            print(
                f"ERROR: output start_time is {st:.3f}s, expected ~0.0. "
                f"Non-zero start times break WhatsApp / iOS playback.",
                file=sys.stderr,
            )
            sys.exit(3)
    except (TypeError, ValueError):
        pass

    print("  Validation PASSED: output is a mobile-safe MP4.\n", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("original_video")
    ap.add_argument("production_json")
    ap.add_argument("voiceover_manifest_json")
    ap.add_argument("out_video_mp4")
    ap.add_argument("--voiceover-wav", default=None,
                    help="where to write the merged voiceover-only WAV "
                         "(default: <out_video_dir>/voiceover.wav)")
    ap.add_argument("--music", default=None,
                    help="optional background music file; trimmed/looped to "
                         "the merged duration and ducked to MUSIC_VOLUME")
    args = ap.parse_args()

    for req in (args.original_video, args.production_json,
                args.voiceover_manifest_json):
        if not os.path.exists(req):
            print(f"Missing required input: {req}", file=sys.stderr)
            sys.exit(2)
    if args.music and not os.path.exists(args.music):
        print(f"Missing required input: {args.music}", file=sys.stderr)
        sys.exit(2)

    with open(args.production_json, "r", encoding="utf-8") as f:
        payload = json.load(f)
    cuts = payload.get("cuts") or []
    if not cuts:
        print(f"{args.production_json} contains no cuts", file=sys.stderr)
        sys.exit(2)
    cuts = sorted(cuts, key=lambda c: float(c["start_seconds"]))

    with open(args.voiceover_manifest_json, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    vo_entries = manifest.get("cuts") or []
    if len(vo_entries) != len(cuts):
        print(
            f"Voiceover manifest has {len(vo_entries)} entries but "
            f"production.json has {len(cuts)} cuts — they must match 1:1. "
            f"Re-run generate_voiceover.py against this production.json.",
            file=sys.stderr,
        )
        sys.exit(2)
    vo_entries = sorted(vo_entries, key=lambda e: int(e["index"]))
    vo_wavs = [e["wav"] for e in vo_entries]
    for w in vo_wavs:
        if not os.path.exists(w):
            print(f"Voiceover WAV from manifest does not exist: {w}",
                  file=sys.stderr)
            sys.exit(2)
    try:
        vo_timings = [final_wav_timing(w) for w in vo_wavs]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    # The final WAV is authoritative. This supports existing manifests while
    # eliminating any stale or rounded duration value before atrim executes.
    vo_durations = [duration for duration, _ in vo_timings]

    src_duration = probe_duration(args.original_video)
    print(f"Source video duration: {src_duration:.2f}s", flush=True)

    # ---- Timing reconciliation (no drift) ----
    print("\nReconciling per-cut timing against voiceover durations...",
          flush=True)
    plan = reconcile_cuts(cuts, vo_durations, src_duration)
    for item, (_, audio_samples) in zip(plan, vo_timings):
        item["audio_samples"] = audio_samples
    assert_reconciled_duration_coverage(cuts, plan)

    # Persist the authoritative per-cut durations as a sidecar next to the
    # merged voiceover WAV. The subtitle step runs as a separate process and
    # otherwise has to re-derive cut boundaries from proportional word-count
    # ratios, which drifts whenever a cut's real spoken duration does not
    # match its word-count share. The merged voiceover is concatenated in
    # this exact order with each cut padded/trimmed to exactly these
    # durations, so their cumulative sums ARE the real cut boundaries of the
    # WAV the subtitle step transcribes. Pure addition: nothing here feeds
    # back into the ffmpeg/produce/validation flow.
    sidecar_wav = args.voiceover_wav or os.path.join(
        os.path.dirname(os.path.abspath(args.out_video_mp4)) or ".",
        "voiceover.wav",
    )
    cut_timing_path = os.path.join(
        os.path.dirname(os.path.abspath(sidecar_wav)) or ".",
        "cut_timing.json",
    )
    with open(cut_timing_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "version": 1,
                "cuts": [
                    {"index": index, "video_seconds": float(item["video_seconds"])}
                    for index, item in enumerate(plan)
                ],
            },
            f,
            indent=2,
        )
        f.write("\n")
    print(f"Cut timing sidecar written: {cut_timing_path}", flush=True)

    total_video = sum(p["video_seconds"] for p in plan)
    total_vo = sum(vo_durations)
    print(
        f"Reconciled plan: {len(plan)} cut(s), total video "
        f"{total_video:.2f}s vs total voiceover {total_vo:.2f}s",
        flush=True,
    )
    if abs(total_video - total_vo) > DURATION_TOLERANCE_S:
        print(
            f"ERROR: total planned video duration ({total_video:.3f}s) "
            f"differs from total voiceover duration ({total_vo:.3f}s) by "
            f"more than {DURATION_TOLERANCE_S}s. The final video would "
            f"drift out of sync with its narration — refusing to produce "
            f"it. This indicates a bug in reconcile_cuts; do not ship.",
            file=sys.stderr,
        )
        sys.exit(3)

    # Validate the reconciled ranges against the source.
    prev_end = -1.0
    for i, p in enumerate(plan):
        s, e = p["start_seconds"], p["end_seconds"]
        if e <= s:
            print(f"Cut #{i}: end ({e}) <= start ({s}) after reconciliation",
                  file=sys.stderr)
            sys.exit(2)
        if s < 0 or e > src_duration + 0.5:
            print(
                f"Cut #{i}: range [{s}, {e}] outside source "
                f"[0, {src_duration:.2f}] after reconciliation",
                file=sys.stderr,
            )
            sys.exit(2)
        if s < prev_end:
            print(
                f"Cut #{i}: overlaps previous cut ({s} < prev end "
                f"{prev_end}) after reconciliation — footage would be "
                f"double-used",
                file=sys.stderr,
            )
            sys.exit(2)
        prev_end = e

    print(
        f"\nProducing ONE merged video from {len(plan)} cut(s) with "
        f"normalized voiceover at unity (source audio muted"
        f"{', background music at ' + str(int(MUSIC_VOLUME * 100)) + '% and ducked beneath speech' if args.music else ''}) "
        f"— mobile-safe encoding "
        f"(H.264 High@L{X264_LEVEL} {TARGET_PIX_FMT} CRF{X264_CRF}, "
        f"AAC-LC {AAC_SAMPLE_RATE}Hz stereo {AAC_BITRATE}, "
        f"+faststart, no edit lists).",
        flush=True,
    )

    os.makedirs(os.path.dirname(os.path.abspath(args.out_video_mp4)) or ".",
                exist_ok=True)
    produce_merged_video(args.original_video, plan, vo_wavs,
                         args.out_video_mp4, args.music)
    print(f"Merged video written: {args.out_video_mp4}", flush=True)

    # Validate the output is a genuine, mobile-safe MP4.
    validate_mp4(args.out_video_mp4)

    # Standalone merged voiceover for the subtitle step.
    vo_wav = args.voiceover_wav or os.path.join(
        os.path.dirname(os.path.abspath(args.out_video_mp4)) or ".",
        "voiceover.wav",
    )
    write_merged_voiceover(vo_wavs, plan, vo_wav)
    expected_voiceover_seconds = sum(float(item["video_seconds"]) for item in plan)
    validate_merged_voiceover_timeline(vo_wav, expected_voiceover_seconds)
    print(f"Merged voiceover written: {vo_wav}", flush=True)


if __name__ == "__main__":
    main()
