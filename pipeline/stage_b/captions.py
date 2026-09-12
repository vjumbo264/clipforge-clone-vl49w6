"""Stage B caption step — CINEMATIC MODE (ClipForge's sole caption renderer).

Runs AFTER the merged render (and after the optional enhance pass), normalizes
the source into the bare 1080x1200 (9:10) cinematic frame via scene-level
reframing, and burns sentence-level caption cards plus a one-time title banner
into that frame.

---------------------------------------------------------------------------
CINEMATIC CAPTION BEHAVIOR
---------------------------------------------------------------------------
  * GRANULARITY — text is grouped and timed as complete sentence cards (long
    sentences split into ≤6-word cards at nearby clause breaks).
  * TIMING — each card appears fully at its first aligned word. Consecutive
    close-together cards swap directly with no fade/overlap/animation. Only a
    long otherwise-empty gap causes the completed card to fade away first.
  * STYLE — compact all-caps Coolvetica text, centred in the frame, with a
    one-pixel dark keyline and a soft gray-black Gaussian drop shadow.
  * KEYWORD COLORING — production.json may mark noteworthy words with an
    author-selected literal #RRGGBB; the renderer applies that exact color and
    never classifies tone/sentiment itself.
  * TITLE BANNER — a one-time intro: a white full-width banner carrying the
    video's title drops in from off-screen top, holds, then drops back out and
    never returns. Banner graphic and title text move as ONE unit (the title is
    rendered INTO the banner image with Pillow).

---------------------------------------------------------------------------
AUTHORITATIVE TEXT vs. TRANSCRIPTION
---------------------------------------------------------------------------
The transcription supplies per-word TIMING ONLY; the words displayed on screen
come from the ORIGINAL script (``voiceover_text`` per cut, verbatim — legacy
``raw_narration`` accepted).

Ported from ``_legacy/scripts/generate_subtitles_cinematic.py`` and
``_legacy/scripts/subtitle_common.py``.
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import re
import subprocess
import sys

from pipeline.stage_b import common, reframe

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont


# ---------- Cinematic styling ----------
CIN_FONT_FILE = str(common.COOLVETICA_FONT)
CIN_FONT = "Coolvetica Rg"
CIN_FONT_FRACTION_OF_HEIGHT = 0.032
CIN_ASS_SHADOW_COLOR = "&H10000000"
CIN_ASS_OUTLINE_COLOR = "&H00181818"
CIN_ASS_OUTLINE_WIDTH = 1
CIN_ASS_SHADOW_OFFSET_X_FRACTION = 0.0020
CIN_ASS_SHADOW_OFFSET_Y_FRACTION = 0.0035

# Production captions are rasterized with Pillow as three deliberate layers:
# a compact near-black high-opacity Gaussian shadow, a subtle one-pixel dark
# keyline, and literal per-word foreground fills.
CIN_RASTER_FPS = 24
CIN_RASTER_MAX_WIDTH_FRACTION = 0.86
CIN_RASTER_Y_FRACTION = 0.55
CIN_RASTER_SHADOW_RGB = (0, 0, 0)
CIN_RASTER_SHADOW_ALPHA = 0.94
CIN_RASTER_SHADOW_X = 4
CIN_RASTER_SHADOW_Y = 6
CIN_RASTER_SHADOW_BLUR_RADIUS = 5
CIN_RASTER_STROKE_RGB = (24, 24, 24)
CIN_RASTER_STROKE_WIDTH = 1

CIN_FRAME_WIDTH = reframe.CIN_FRAME_WIDTH
CIN_FRAME_HEIGHT = reframe.CIN_FRAME_HEIGHT

# ---------- Title banner ----------
CIN_BANNER_FONT_FILE = CIN_FONT_FILE
CIN_BANNER_FONT_FALLBACK = "DejaVuSans-Bold.ttf"
CIN_BANNER_HEIGHT_FRACTION = 0.11
CIN_BANNER_TOP_FRACTION = 0.0
CIN_BANNER_TEXT_WIDTH_FRACTION = 0.90
CIN_BANNER_IN_SECONDS = 2.0
CIN_BANNER_HOLD_SECONDS = 15.0
CIN_BANNER_OUT_SECONDS = 2.0
CIN_BANNER_MIN_FONT_PX = 20

CIN_CAPTION_LONG_GAP_SECONDS = 3.0
CIN_CAPTION_GAP_FADE_SECONDS = 1.0
CIN_CAPTION_MAX_TIMELINE_GAP_SECONDS = CIN_CAPTION_LONG_GAP_SECONDS
CIN_CAPTION_EDGE_TOLERANCE_SECONDS = 2.0

# ---------- Word timing (from subtitle_common) ----------
MIN_WORD_SECONDS = 0.08
MIN_DISPLAY_SECONDS = 0.12
_WORD_RE = re.compile(r"\S+")

_HEX_COLOR_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")
_SENT_END_RE = re.compile(r"[.!?…][\"')\]]*$")
_CLAUSE_BREAK_RE = re.compile(r"[,;:—–][\"')\]]*$")
MAX_CAPTION_WORDS = 6
_MIN_TRAILING_CAPTION_WORDS = 2


def _normalise_hex_color(value: object) -> str | None:
    match = _HEX_COLOR_RE.fullmatch(str(value or "").strip())
    return f"#{match.group(1).upper()}" if match else None


def _hex_rgb(color: str | None) -> tuple[int, int, int]:
    normalized = _normalise_hex_color(color)
    if not normalized:
        return (255, 255, 255)
    return tuple(int(normalized[index:index + 2], 16) for index in (1, 3, 5))


def _hex_ass(color: str) -> str:
    red, green, blue = _hex_rgb(color)
    return f"&H{blue:02X}{green:02X}{red:02X}"


def _norm_token(text: str) -> str:
    """Normalise a token for alignment and metadata lookup, never display."""
    return re.sub(r"[^0-9a-z]+", "", text.lower())


def _ass_time(seconds: float) -> str:
    """Format an ASS timestamp as H:MM:SS.cc."""
    if seconds < 0:
        seconds = 0.0
    centiseconds = int(round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds_part, fraction = divmod(remainder, 100)
    return f"{hours:d}:{minutes:02d}:{seconds_part:02d}.{fraction:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


# --------------------------------------------------------------------------- #
# Transcription (timing source only)                                           #
# --------------------------------------------------------------------------- #

def transcribe_words(voiceover_wav: str, model: str, lang: str, work_dir: str) -> list[dict]:
    """Transcribe the merged voiceover into word-level timing events only."""
    from faster_whisper import WhisperModel  # delayed optional dependency

    print(f"Transcribing merged voiceover for word timings: {voiceover_wav}", flush=True)
    wm = WhisperModel(model, device="cpu", compute_type="int8")
    vad_parameters = {
        "threshold": 0.5,
        "min_speech_duration_ms": 250,
        "min_silence_duration_ms": 1000,
        "speech_pad_ms": 200,
        "max_speech_duration_s": 30.0,
    }
    segments, info = wm.transcribe(
        voiceover_wav,
        language=None if lang == "auto" else lang,
        beam_size=5,
        vad_filter=True,
        vad_parameters=vad_parameters,
        word_timestamps=True,
        condition_on_previous_text=False,
    )
    print(
        f"Detected language: {info.language} (prob={info.language_probability:.2f})",
        flush=True,
    )

    words: list[dict] = []
    for seg in segments:
        for word in (getattr(seg, "words", None) or []):
            text = (word.word or "").strip()
            if not text:
                continue
            start = float(word.start)
            end = max(float(word.end), start + MIN_WORD_SECONDS)
            words.append({"start": round(start, 3), "end": round(end, 3), "word": text})
    if not words:
        raise common.StageBError(
            "transcription produced zero words. The merged voiceover WAV is either "
            "silent or unreadable — check upstream steps."
        )
    print(f"Transcribed {len(words)} words ({words[0]['start']:.2f}s .. {words[-1]['end']:.2f}s)", flush=True)

    transcript_path = os.path.join(work_dir, "voiceover_words.json")
    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(
            {"backend": "faster-whisper", "model": model, "language": info.language, "words": words},
            f, ensure_ascii=False, indent=2,
        )
    print(f"Word transcript written: {transcript_path}", flush=True)
    return words


def align_words_to_script(timed_words: list[dict], script_texts: list[str],
                          cut_durations: list[float] | None = None) -> list[dict]:
    """Map authoritative script wording onto the transcription timing line.

    When ``cut_durations`` (the exact per-cut output durations persisted to
    ``cut_timing.json`` by render.py) is provided, timed words are partitioned
    by the CUMULATIVE real cut boundaries in the merged-voiceover timeline
    instead of proportional word-count quotas. Either way, timed words are
    matched within each cut with a monotone sequence alignment. Display wording
    is always the original script, verbatim.
    """
    script_cuts = [_WORD_RE.findall(text) for text in script_texts]
    total_script = sum(len(cut) for cut in script_cuts)
    if total_script == 0:
        raise common.StageBError("the original script contains no words at all.")

    timed = list(timed_words)
    n_timed = len(timed)

    use_real_boundaries = (
        cut_durations is not None
        and len(cut_durations) == len(script_cuts)
        and all(float(duration) > 0 for duration in cut_durations)
    )
    if cut_durations is not None and not use_real_boundaries:
        print(
            "NOTE: supplied per-cut durations do not match the script's cut count "
            "(or contain a non-positive duration); falling back to proportional "
            "word-count partitioning.",
            flush=True,
        )

    quotas: list[int]
    if use_real_boundaries:
        # A word belongs to the cut containing its midpoint; the final boundary
        # is inclusive so nothing falls off the end.
        boundaries: list[float] = []
        cumulative = 0.0
        for duration in cut_durations or []:
            cumulative += float(duration)
            boundaries.append(cumulative)
        quotas = [0] * len(script_cuts)
        cut_index = 0
        for word in timed:
            midpoint = (float(word["start"]) + float(word["end"])) / 2.0
            while cut_index < len(boundaries) - 1 and midpoint >= boundaries[cut_index]:
                cut_index += 1
            quotas[cut_index] += 1
        print(
            f"NOTE: partitioned {n_timed} timed word(s) by the exact per-cut "
            f"durations from cut_timing.json (boundaries at "
            f"{[round(boundary, 3) for boundary in boundaries]}s).",
            flush=True,
        )
    elif n_timed < total_script:
        print(
            f"NOTE: transcription produced {n_timed} timed words but the script "
            f"has {total_script}. Scaling per-cut timing quotas proportionally.",
            flush=True,
        )
        quotas = []
        assigned = 0
        remaining_timed = n_timed
        remaining_script = total_script
        for cut in script_cuts:
            quota = min(len(cut), int(round(remaining_timed * len(cut) / max(remaining_script, 1))))
            quotas.append(quota)
            assigned += quota
            remaining_timed -= quota
            remaining_script -= len(cut)
        if quotas:
            quotas[-1] += n_timed - assigned
    else:
        quotas = [len(cut) for cut in script_cuts]
        leftover = n_timed - total_script
        if leftover > 0:
            weighted = [leftover * len(cut) / total_script for cut in script_cuts]
            extras = [int(math.floor(value)) for value in weighted]
            remainder = leftover - sum(extras)
            ranked = sorted(
                range(len(script_cuts)),
                key=lambda index: (weighted[index] - extras[index], -index),
                reverse=True,
            )
            for index in ranked[:remainder]:
                extras[index] += 1
            quotas = [quota + extra for quota, extra in zip(quotas, extras)]
            print(
                f"NOTE: transcription produced {leftover} more word(s) than the "
                "script contains; extras are timing-only and were distributed "
                "proportionally across script cuts.",
                flush=True,
            )

    display_events: list[dict] = []
    cursor = 0
    previous_end = 0.0
    for cut_i, script_words in enumerate(script_cuts):
        quota = quotas[cut_i]
        window = timed[cursor:cursor + quota]
        cursor += quota
        if not window:
            current = previous_end
            for word in script_words:
                display_events.append(
                    {"start": round(current, 3), "end": round(current + MIN_DISPLAY_SECONDS, 3), "word": word}
                )
                current += MIN_DISPLAY_SECONDS
            previous_end = current
            continue

        window_start = window[0]["start"]
        window_end = window[-1]["end"]
        script_tokens = [_norm_token(word) for word in script_words]
        timed_tokens = [_norm_token(word["word"]) for word in window]
        matcher = difflib.SequenceMatcher(a=script_tokens, b=timed_tokens, autojunk=False)

        starts: list[float | None] = [None] * len(script_words)
        ends: list[float | None] = [None] * len(script_words)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag not in ("equal", "replace"):
                continue
            script_span = i2 - i1
            timed_span = j2 - j1
            for k in range(script_span):
                fraction_start = k / script_span
                fraction_end = (k + 1) / script_span
                timed_start = j1 + int(fraction_start * timed_span)
                timed_end = j1 + max(int(fraction_end * timed_span), int(fraction_start * timed_span) + 1)
                timed_end = min(timed_end, j2)
                starts[i1 + k] = window[timed_start]["start"]
                ends[i1 + k] = window[max(timed_start, timed_end - 1)]["end"]

        # ``delete`` opcodes (script words Whisper did not return) are
        # allocated over the bounded interval between neighbouring timed
        # anchors instead of inheriting the preceding timestamp.
        known_indices = [index for index, start in enumerate(starts) if start is not None]
        boundary_idx = [-1, *known_indices, len(script_words)]
        for left_index, right_index in zip(boundary_idx, boundary_idx[1:]):
            missing = right_index - left_index - 1
            if missing <= 0:
                continue
            left_time = (
                float(ends[left_index])
                if left_index >= 0 and ends[left_index] is not None
                else window_start
            )
            right_time = (
                float(starts[right_index])
                if right_index < len(script_words) and starts[right_index] is not None
                else window_end
            )
            right_time = max(right_time, left_time)
            span = right_time - left_time
            for offset, index in enumerate(range(left_index + 1, right_index)):
                starts[index] = left_time + span * offset / missing
                ends[index] = left_time + span * (offset + 1) / missing

        last_end = window_start
        for index in range(len(script_words)):
            start = float(starts[index] if starts[index] is not None else last_end)
            end = float(ends[index] if ends[index] is not None else start)
            start = max(start, last_end)
            if start > window_end:
                start = window_end
            end = max(end, start + MIN_DISPLAY_SECONDS)
            starts[index] = start
            ends[index] = end
            last_end = end

        for index, word in enumerate(script_words):
            display_events.append(
                {"start": round(starts[index], 3), "end": round(ends[index], 3), "word": word}  # type: ignore[arg-type]
            )
        previous_end = display_events[-1]["end"]

    assert cursor == n_timed, "timed-word partition bookkeeping drifted"
    print(
        f"Aligned {sum(len(cut) for cut in script_cuts)} script word(s) from "
        f"{len(script_cuts)} cut(s) onto {n_timed} transcribed timing events. "
        "Subtitle wording = ORIGINAL SCRIPT (verbatim).",
        flush=True,
    )
    return display_events


# --------------------------------------------------------------------------- #
# Sentence cards                                                               #
# --------------------------------------------------------------------------- #

def _caption_card_chunks(words: list[dict]) -> list[list[dict]]:
    """Split one sentence into short cards, preferring nearby clause breaks."""
    cards: list[list[dict]] = []
    remaining = list(words)
    while len(remaining) > MAX_CAPTION_WORDS:
        upper = min(MAX_CAPTION_WORDS, len(remaining) - _MIN_TRAILING_CAPTION_WORDS)
        lower = max(3, upper - 2)
        clause_positions = [
            pos for pos in range(lower, upper + 1)
            if _CLAUSE_BREAK_RE.search(remaining[pos - 1]["word"])
        ]
        split_at = clause_positions[-1] if clause_positions else upper
        cards.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining:
        cards.append(remaining)
    return cards


def split_sentences(events: list[dict]) -> list[dict]:
    """Return static voice-aligned caption cards from timed script words."""
    sentences: list[list[dict]] = []
    current: list[dict] = []
    for event in events:
        current.append(event)
        if _SENT_END_RE.search(event["word"]):
            sentences.append(current)
            current = []
    if current:
        if sentences and len(current) < 2:
            sentences[-1].extend(current)
        else:
            sentences.append(current)

    cards: list[dict] = []
    for sentence_words in sentences:
        for card_words in _caption_card_chunks(sentence_words):
            cards.append(
                {
                    "words": card_words,
                    "start": card_words[0]["start"],
                    "speak_end": card_words[-1]["end"],
                }
            )
    print(
        f"Grouped {len(events)} words into {len(cards)} caption card(s) "
        f"(max {MAX_CAPTION_WORDS} words each).",
        flush=True,
    )
    return cards


def script_texts_and_keywords(plan: dict) -> tuple[list[str], dict]:
    """Extract the original per-cut script plus author keyword colors from a
    normalized plan. Keyword shapes per cut (all optional, additive only):
    ``"keywords": [{"word": "betrayal", "color": "#FF5C5C"}, ...]`` or
    ``"keywords": {"betrayal": "#FF5C5C", ...}``."""
    texts: list[str] = []
    keyword_map: dict[str, str] = {}
    for i, c in enumerate(plan.get("cuts") or []):
        text = str(c.get("voiceover_text") or "").strip()
        if not text:
            raise common.StageBError(
                f"cut #{i} has no voiceover_text — the original script is the "
                "authoritative subtitle source."
            )
        texts.append(text)

        raw_kw = c.get("keywords")
        if isinstance(raw_kw, dict):
            items = [
                {"word": word, "color": value.get("color") if isinstance(value, dict) else value}
                for word, value in raw_kw.items()
            ]
        elif isinstance(raw_kw, list):
            items = [k for k in raw_kw if isinstance(k, dict)]
        else:
            items = []
        for k in items:
            word = _norm_token(str(k.get("word") or ""))
            color = _normalise_hex_color(k.get("color"))
            if not word:
                continue
            if color:
                keyword_map[word] = color
            elif k.get("color") is not None:
                raise common.StageBError(
                    f"Invalid keyword color for {word!r}: {k.get('color')!r}. Use a #RRGGBB literal."
                )
    if keyword_map:
        print(f"Loaded {len(keyword_map)} author-selected keyword color(s): {keyword_map}", flush=True)
    return texts, keyword_map


# --------------------------------------------------------------------------- #
# Title banner                                                                 #
# --------------------------------------------------------------------------- #

_banner_font_warned = False


def _load_banner_font(size: int):
    global _banner_font_warned
    if os.path.isfile(CIN_BANNER_FONT_FILE):
        return ImageFont.truetype(CIN_BANNER_FONT_FILE, size)
    if not _banner_font_warned:
        _banner_font_warned = True
        print(
            f"WARNING: vendored banner font missing at {CIN_BANNER_FONT_FILE} — "
            f"falling back to {CIN_BANNER_FONT_FALLBACK}.",
            file=sys.stderr, flush=True,
        )
    try:
        return ImageFont.truetype(CIN_BANNER_FONT_FALLBACK, size)
    except OSError:
        return ImageFont.load_default()


def build_banner_png(title: str, width: int, height: int, out_png: str) -> int:
    """Render the title banner as a single PNG: a full-width white strip with
    the title centred on it in Coolvetica (auto-fit, never truncated)."""
    banner_h = max(48, int(round(height * CIN_BANNER_HEIGHT_FRACTION)))
    banner_title = title.upper()
    img = Image.new("RGB", (width, banner_h), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    max_text_w = int(round(width * CIN_BANNER_TEXT_WIDTH_FRACTION))
    size = max(CIN_BANNER_MIN_FONT_PX, int(round(banner_h * 0.52)))
    font = _load_banner_font(size)
    while size > CIN_BANNER_MIN_FONT_PX and draw.textlength(banner_title, font=font) > max_text_w:
        size -= 2
        font = _load_banner_font(size)

    bbox = draw.textbbox((0, 0), banner_title, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (width - text_w) // 2 - bbox[0]
    y = (banner_h - text_h) // 2 - bbox[1]
    draw.text((x, y), banner_title, font=font, fill=(18, 18, 18))

    img.save(out_png)
    print(
        f"Title banner rendered: {out_png} ({width}x{banner_h}, "
        f"{os.path.basename(CIN_BANNER_FONT_FILE)} {size}px, title={banner_title!r})",
        flush=True,
    )
    return banner_h


def _banner_y_expr(rest_top: int) -> str:
    """Y position of the banner's TOP edge at time t — drop-in / hold /
    drop-out in one piecewise ffmpeg if() expression with cubic easing."""
    t_in = CIN_BANNER_IN_SECONDS
    t_hold = t_in + CIN_BANNER_HOLD_SECONDS
    t_out = t_hold + CIN_BANNER_OUT_SECONDS
    in_progress = f"t/{t_in}"
    out_progress = f"(t-{t_hold})/{CIN_BANNER_OUT_SECONDS}"
    in_ease = f"(1-pow(1-({in_progress}),3))"
    out_ease = f"(1-pow(1-({out_progress}),3))"
    return (
        f"if(lt(t,{t_in}),"
        f"-overlay_h+({rest_top}+overlay_h)*{in_ease},"
        f"if(lt(t,{t_hold}),"
        f"{rest_top},"
        f"if(lt(t,{t_out}),"
        f"{rest_top}-({rest_top}+overlay_h)*{out_ease},"
        f"-overlay_h)))"
    )


# --------------------------------------------------------------------------- #
# Timeline validation                                                          #
# --------------------------------------------------------------------------- #

def normalize_caption_edge_coverage(sentences: list[dict], narration_start: float, narration_end: float) -> None:
    """Extend only the edge cards over detected narration silence."""
    if not sentences:
        return
    if math.isfinite(narration_start) and narration_start > 0:
        sentences[0]["start"] = 0.0
    if math.isfinite(narration_end) and narration_end > 0:
        sentences[-1]["speak_end"] = max(float(sentences[-1]["speak_end"]), narration_end)


def normalize_caption_internal_coverage(sentences: list[dict]) -> None:
    """Hold a caption card across large timing gaps between adjacent cards
    (Whisper occasionally skips a phrase; authored narration is continuous)."""
    normalized = 0
    for current, following in zip(sentences, sentences[1:]):
        current_end = float(current["speak_end"])
        following_start = float(following["start"])
        gap = following_start - current_end
        if math.isfinite(gap) and gap > CIN_CAPTION_MAX_TIMELINE_GAP_SECONDS:
            current["speak_end"] = following_start
            normalized += 1
    if normalized:
        print(
            f"Held {normalized} caption card(s) across large transcription timing "
            "gap(s) to preserve continuous subtitle coverage.",
            flush=True,
        )


def validate_caption_timeline(sentences: list[dict], video_duration: float) -> None:
    """Fail closed when caption cards cannot continuously cover narration."""
    if not sentences:
        raise common.StageBError("Cinematic subtitle timeline has no caption cards.")
    if not math.isfinite(video_duration) or video_duration <= 0:
        raise common.StageBError(f"Invalid final-video duration for caption timeline: {video_duration!r}")

    previous_end = 0.0
    for index, sentence in enumerate(sentences, start=1):
        start = float(sentence["start"])
        end = float(sentence["speak_end"])
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            raise common.StageBError(f"Caption card #{index} has invalid timing [{start!r}, {end!r}].")
        if start < -CIN_CAPTION_EDGE_TOLERANCE_SECONDS or end > video_duration + CIN_CAPTION_EDGE_TOLERANCE_SECONDS:
            raise common.StageBError(
                f"Caption card #{index} spans [{start:.3f}s, {end:.3f}s] outside the "
                f"final video timeline [0.000s, {video_duration:.3f}s]. Refusing to "
                "burn captions with a mismatched timing source."
            )
        if index == 1:
            if start > CIN_CAPTION_EDGE_TOLERANCE_SECONDS:
                raise common.StageBError(
                    f"First caption starts at {start:.3f}s, leaving an unexpected opening subtitle gap."
                )
        else:
            gap = start - previous_end
            if gap > CIN_CAPTION_MAX_TIMELINE_GAP_SECONDS:
                raise common.StageBError(
                    f"Caption cards #{index - 1} and #{index} contain a {gap:.3f}s "
                    "internal blank gap. This would fade captions out while narration "
                    "continues, so Stage B is refusing the release."
                )
        previous_end = max(previous_end, end)

    trailing_gap = video_duration - previous_end
    if trailing_gap > CIN_CAPTION_EDGE_TOLERANCE_SECONDS:
        raise common.StageBError(
            f"Last caption ends at {previous_end:.3f}s but the final video lasts "
            f"{video_duration:.3f}s, leaving a {trailing_gap:.3f}s uncaptained tail."
        )
    print(
        f"Caption timeline validated: {len(sentences)} card(s) cover "
        f"{sentences[0]['start']:.3f}s..{previous_end:.3f}s of the "
        f"{video_duration:.3f}s final video without long blank gaps.",
        flush=True,
    )


# --------------------------------------------------------------------------- #
# ASS diagnostic sidecar                                                       #
# --------------------------------------------------------------------------- #

def write_cinematic_ass(sentences: list[dict], keyword_map: dict,
                        width: int, height: int, out_ass: str) -> None:
    """Write a human-readable timing/debug ASS sidecar (the production image
    uses the Pillow raster compositor, not libass)."""
    font_size = max(24, int(round(height * CIN_FONT_FRACTION_OF_HEIGHT)))
    shadow_x = max(1, int(round(width * CIN_ASS_SHADOW_OFFSET_X_FRACTION)))
    shadow_y = max(2, int(round(height * CIN_ASS_SHADOW_OFFSET_Y_FRACTION)))
    margin = int(round(width * 0.06))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CinShadow,{CIN_FONT},{font_size},{CIN_ASS_SHADOW_COLOR},{CIN_ASS_SHADOW_COLOR},{CIN_ASS_SHADOW_COLOR},{CIN_ASS_SHADOW_COLOR},0,0,0,0,100,100,0,0,1,0,0,5,{margin},{margin},0,1
Style: CinText,{CIN_FONT},{font_size},&H00FFFFFF,&H00FFFFFF,{CIN_ASS_OUTLINE_COLOR},&H00000000,0,0,0,0,100,100,0,0,1,{CIN_ASS_OUTLINE_WIDTH},0,5,{margin},{margin},0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for sent in sentences:
        words = sent["words"]
        start_ts = _ass_time(sent["start"])
        end_ts = _ass_time(sent["speak_end"])
        shadow_parts: list[str] = []
        text_parts: list[str] = []
        for word in words:
            display_word = str(word["word"]).upper()
            color = keyword_map.get(_norm_token(word["word"]))
            shadow_parts.append("{\\c&H000000&}" + _ass_escape(display_word))
            text_tags = f"{{\\c{_hex_ass(color)}&}}" if color else ""
            text_parts.append(text_tags + _ass_escape(display_word))

        lines.append(
            f"Dialogue: 0,{start_ts},{end_ts},CinShadow,,0,0,0,,"
            f"{{\\pos({width // 2 + shadow_x},{height // 2 + shadow_y})}}{' '.join(shadow_parts)}\n"
        )
        lines.append(f"Dialogue: 1,{start_ts},{end_ts},CinText,,0,0,0,,{' '.join(text_parts)}\n")

    with open(out_ass, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"ASS cinematic subtitle sidecar written: {out_ass} ({len(sentences)} card stack(s))", flush=True)


# --------------------------------------------------------------------------- #
# Pillow raster overlay                                                        #
# --------------------------------------------------------------------------- #

def _caption_font(size: int) -> ImageFont.FreeTypeFont:
    if not os.path.isfile(CIN_FONT_FILE):
        raise common.StageBError(f"Missing cinematic font: {CIN_FONT_FILE}")
    return ImageFont.truetype(CIN_FONT_FILE, size)


def _caption_layout(sentence: dict, font, width: int, height: int) -> list[dict]:
    """Lay out a complete static sentence as centred, word-addressable runs."""
    max_width = int(round(width * CIN_RASTER_MAX_WIDTH_FRACTION))
    metrics = ImageDraw.Draw(Image.new("L", (1, 1)))
    lines: list[list[dict]] = []
    current: list[dict] = []
    for word in sentence["words"]:
        item = {"source": word, "display": str(word["word"]).upper()}
        trial = current + [item]
        trial_text = " ".join(x["display"] for x in trial)
        if current and metrics.textlength(trial_text, font=font) > max_width:
            lines.append(current)
            current = [item]
        else:
            current = trial
    if current:
        lines.append(current)

    line_height = int(round(font.size * 1.12))
    top = int(round(height * CIN_RASTER_Y_FRACTION - line_height * len(lines) / 2))
    runs: list[dict] = []
    for line_index, line in enumerate(lines):
        line_text = " ".join(item["display"] for item in line)
        x = (width - metrics.textlength(line_text, font=font)) / 2.0
        y = top + line_index * line_height
        for item in line:
            item["x"] = x
            item["y"] = y
            runs.append(item)
            x += metrics.textlength(item["display"] + " ", font=font)
    return runs


def _caption_card_opacity(sentences: list[dict], index: int, time_s: float) -> float:
    """Return a static card's gap-aware opacity without permitting overlap."""
    sentence = sentences[index]
    start = float(sentence["start"])
    spoken_end = float(sentence["speak_end"])
    next_start = float(sentences[index + 1]["start"]) if index + 1 < len(sentences) else spoken_end

    if time_s < start or time_s >= next_start:
        return 0.0
    gap = next_start - spoken_end
    if gap < CIN_CAPTION_LONG_GAP_SECONDS or time_s < spoken_end:
        return 1.0

    fade_start = min(spoken_end + CIN_CAPTION_LONG_GAP_SECONDS, next_start - CIN_CAPTION_GAP_FADE_SECONDS)
    fade_end = min(fade_start + CIN_CAPTION_GAP_FADE_SECONDS, next_start)
    if time_s < fade_start:
        return 1.0
    if time_s >= fade_end:
        return 0.0
    return max(0.0, min(1.0, (fade_end - time_s) / max(fade_end - fade_start, 0.001)))


def _apply_mask(canvas: Image.Image, mask: Image.Image,
                rgb: tuple[int, int, int], opacity: float = 1.0,
                offset: tuple[int, int] = (0, 0)) -> None:
    """Composite an L-mask onto an RGBA canvas using a precise alpha scale."""
    if offset != (0, 0):
        shifted = Image.new("L", mask.size, 0)
        shifted.paste(mask, offset)
        mask = shifted
    if opacity != 1.0:
        mask = mask.point(lambda value: int(round(value * opacity)))
    if not mask.getbbox():
        return
    layer = Image.new("RGBA", canvas.size, (*rgb, 0))
    layer.putalpha(mask)
    canvas.alpha_composite(layer)


def _raster_caption_layers(sentences: list[dict], keyword_map: dict,
                           width: int, height: int, time_s: float,
                           font) -> Image.Image:
    """Return all-caps caption glyphs over a thin keyline and soft shadow."""
    foreground = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for sentence_index, sentence in enumerate(sentences):
        card_opacity = _caption_card_opacity(sentences, sentence_index, time_s)
        if card_opacity <= 0.0:
            continue
        masks: dict[tuple[int, int, int], Image.Image] = {}
        union = Image.new("L", (width, height), 0)
        stroke_union = Image.new("L", (width, height), 0)
        stroke_draw = ImageDraw.Draw(stroke_union)
        draw_by_color: dict[tuple[int, int, int], ImageDraw.ImageDraw] = {}
        for run in _caption_layout(sentence, font, width, height):
            word = run["source"]
            color = _hex_rgb(keyword_map.get(_norm_token(word["word"])))
            if color not in masks:
                masks[color] = Image.new("L", (width, height), 0)
                draw_by_color[color] = ImageDraw.Draw(masks[color])
            draw_by_color[color].text((run["x"], run["y"]), run["display"], font=font, fill=255)
            stroke_draw.text(
                (run["x"], run["y"]), run["display"], font=font,
                fill=255, stroke_width=CIN_RASTER_STROKE_WIDTH, stroke_fill=255,
            )
        for mask in masks.values():
            union = ImageChops.lighter(union, mask)
        if not union.getbbox():
            continue
        soft_shadow = union.filter(ImageFilter.GaussianBlur(radius=CIN_RASTER_SHADOW_BLUR_RADIUS))
        _apply_mask(foreground, soft_shadow, CIN_RASTER_SHADOW_RGB,
                    CIN_RASTER_SHADOW_ALPHA * card_opacity,
                    (CIN_RASTER_SHADOW_X, CIN_RASTER_SHADOW_Y))
        _apply_mask(foreground, stroke_union, CIN_RASTER_STROKE_RGB, card_opacity)
        for color, mask in masks.items():
            _apply_mask(foreground, mask, color, card_opacity)
    return foreground


def render_cinematic_overlays(sentences: list[dict], keyword_map: dict,
                              width: int, height: int, duration: float,
                              out_foreground_mov: str) -> None:
    """Encode the single soft-drop-shadow caption overlay stream (qtrle)."""
    font_size = max(24, int(round(height * CIN_FONT_FRACTION_OF_HEIGHT)))
    font = _caption_font(font_size)
    frame_count = max(1, int(math.ceil(duration * CIN_RASTER_FPS)))
    cmd = [
        "ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
        "-s", f"{width}x{height}", "-r", str(CIN_RASTER_FPS), "-i", "-",
        "-an", "-c:v", "qtrle", "-pix_fmt", "argb", out_foreground_mov,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    print(
        f"Rendering {frame_count} soft-drop-shadow caption frame(s) at "
        f"{CIN_RASTER_FPS}fps -> {out_foreground_mov}",
        flush=True,
    )
    try:
        for frame_index in range(frame_count):
            foreground = _raster_caption_layers(
                sentences, keyword_map, width, height, frame_index / CIN_RASTER_FPS, font,
            )
            assert proc.stdin is not None
            proc.stdin.write(foreground.tobytes())
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
    if proc.wait() != 0:
        raise common.StageBError("ffmpeg failed while encoding flat-shadow caption stream")


# --------------------------------------------------------------------------- #
# Burn-in composite                                                            #
# --------------------------------------------------------------------------- #

def burn_subtitles(video: str, caption_foreground_mov: str, dst: str,
                   banner: dict | None = None, crop_plan: dict | None = None) -> None:
    """Apply scene-static crops, then composite caption overlay and banner."""
    inputs = ["-i", video, "-i", caption_foreground_mov]
    if crop_plan is None:
        crop_plan = {
            "target_width": CIN_FRAME_WIDTH,
            "target_height": CIN_FRAME_HEIGHT,
            "scenes": [
                {
                    "start_seconds": 0.0,
                    "end_seconds": common.probe_duration_seconds(video),
                    "crop_offset_x": 0.5,
                    "crop_offset_y": 0.5,
                }
            ],
        }
    crop_filters, crop_label = reframe.scene_crop_filter(crop_plan, "0:v")
    filters = [
        *crop_filters,
        f"[{crop_label}]format=gbrp[base]",
        "[1:v]setpts=PTS-STARTPTS,format=rgba[foreground]",
        "[base][foreground]overlay=eof_action=pass:repeatlast=0[v0]",
    ]
    out_label = "v0"
    if banner is not None:
        rest_top = int(round(CIN_FRAME_HEIGHT * CIN_BANNER_TOP_FRACTION))
        inputs += ["-loop", "1", "-i", banner["png"]]
        y_expr = _banner_y_expr(rest_top)
        filters.append("[2:v]setpts=PTS-STARTPTS[bimg]")
        filters.append(
            f"[v0][bimg]overlay=x=0:y='{y_expr}':eval=frame"
            f":eof_action=pass:repeatlast=0:shortest=1[v1]"
        )
        out_label = "v1"
    cmd = [
        "ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters),
        "-map", f"[{out_label}]", "-map", "0:a:0", "-c:v", "libx264",
        "-profile:v", "high", "-level:v", "4.0", "-preset", "veryfast",
        "-crf", "18", "-pix_fmt", "yuv420p", "-bf", "0", "-g", "60",
        "-keyint_min", "60", "-sc_threshold", "0",
        "-x264-params", "force-cfr=1", "-video_track_timescale", "15360",
        "-c:a", "copy", "-movflags", "+faststart", "-use_editlist", "0",
        "-brand", "mp42", "-map_metadata", "-1", "-map_chapters", "-1",
        "-sn", "-dn", "-ignore_unknown", "-fflags", "+genpts",
        "-max_muxing_queue_size", "9999", "-f", "mp4", dst,
    ]
    common.sh(cmd)


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #

def render_captions(
    merged_video_mp4: str,
    voiceover_wav: str,
    plan: dict,
    out_video_mp4: str,
    *,
    model: str = "base",
    lang: str = "en",
    work_dir: str | None = None,
    scene_threshold: float = reframe.DEFAULT_SCENE_THRESHOLD,
    title: str | None = None,
    enable_face_detection: bool = True,
) -> dict:
    """Full caption step: transcribe → align → validate → reframe → banner →
    rasterize → burn into the cinematic frame. ``plan`` is the normalized,
    re-validated production plan."""
    for req in (merged_video_mp4, voiceover_wav):
        if not os.path.exists(req):
            raise common.StageBError(f"Missing required input: {req}")

    out_dir = os.path.dirname(os.path.abspath(out_video_mp4)) or "."
    os.makedirs(out_dir, exist_ok=True)
    work_dir = work_dir or os.path.join(out_dir, "subtitle_work_cinematic")
    os.makedirs(work_dir, exist_ok=True)

    video_duration = common.probe_duration_seconds(merged_video_mp4)
    timed_words = transcribe_words(voiceover_wav, model, lang, work_dir)

    # Exact per-cut durations written by render.py next to the merged voiceover.
    cut_durations: list[float] | None = None
    cut_timing_path = os.path.join(os.path.dirname(os.path.abspath(voiceover_wav)) or ".", "cut_timing.json")
    if os.path.exists(cut_timing_path):
        try:
            with open(cut_timing_path, "r", encoding="utf-8") as f:
                timing_payload = json.load(f)
            durations = [float(entry["video_seconds"]) for entry in (timing_payload.get("cuts") or [])]
            if not durations or not all(duration > 0 for duration in durations):
                raise ValueError
            cut_durations = durations
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            print(
                f"WARNING: {cut_timing_path} is unreadable/invalid; falling back "
                "to proportional word-count partitioning.",
                file=sys.stderr, flush=True,
            )

    script_texts, keyword_map = script_texts_and_keywords(plan)
    words = align_words_to_script(timed_words, script_texts, cut_durations)

    sentences = split_sentences(words)
    narration_start = min((float(event["start"]) for event in timed_words), default=0.0)
    narration_end = max((float(event["end"]) for event in timed_words), default=0.0)
    normalize_caption_edge_coverage(sentences, narration_start, narration_end)
    normalize_caption_internal_coverage(sentences)
    validate_caption_timeline(sentences, video_duration)

    source_width, source_height = common.probe_video_size(merged_video_mp4)
    width, height = CIN_FRAME_WIDTH, CIN_FRAME_HEIGHT
    crop_plan = reframe.build_scene_crop_plan(
        merged_video_mp4,
        duration_seconds=video_duration,
        threshold=scene_threshold,
        enable_face_detection=enable_face_detection,
    )
    crop_plan_path = os.path.join(work_dir, "cinematic_crop_plan.json")
    reframe.write_crop_plan(crop_plan, crop_plan_path)
    print(
        f"Source video resolution: {source_width}x{source_height}; cinematic output "
        f"uses {crop_plan['scene_count']} static scene crop(s) in the bare "
        f"{width}x{height} (9:10) frame with face-centered positions when a "
        "confident face is detected.",
        flush=True,
    )

    banner = None
    banner_title = (title or plan.get("title") or "").strip()
    if banner_title:
        banner_png = os.path.join(work_dir, "title_banner.png")
        banner_h = build_banner_png(banner_title, width, height, banner_png)
        banner = {"png": banner_png, "height": banner_h}
    else:
        print(
            "WARNING: no video title available — rendering WITHOUT the title "
            "banner; captions are unaffected.",
            file=sys.stderr, flush=True,
        )

    ass_path = os.path.join(work_dir, "subtitles_cinematic.ass")
    write_cinematic_ass(sentences, keyword_map, width, height, ass_path)
    foreground_mov = os.path.join(work_dir, "cinematic_caption_flat_shadow.mov")
    render_cinematic_overlays(sentences, keyword_map, width, height, video_duration, foreground_mov)

    print(
        f"Compositing flat-3D-shadow cinematic captions{' + title banner' if banner else ''} "
        f"into {out_video_mp4} ...",
        flush=True,
    )
    burn_subtitles(merged_video_mp4, foreground_mov, out_video_mp4, banner=banner, crop_plan=crop_plan)
    print(f"Final cinematically-subtitled video written: {out_video_mp4}", flush=True)

    return {
        "captioned_mp4": out_video_mp4,
        "crop_plan_json": crop_plan_path,
        "ass_sidecar": ass_path,
        "caption_card_count": len(sentences),
        "banner_applied": banner is not None,
        "video_duration_seconds": video_duration,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage B cinematic captions.")
    ap.add_argument("merged_video_mp4")
    ap.add_argument("voiceover_wav")
    ap.add_argument("production_json")
    ap.add_argument("out_video_mp4")
    ap.add_argument("--model", default="base", choices=["tiny", "base", "small"])
    ap.add_argument("--lang", default="en")
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--scene-threshold", type=float, default=reframe.DEFAULT_SCENE_THRESHOLD)
    ap.add_argument("--title", default=None)
    args = ap.parse_args(argv)

    plan = common.load_production_plan(args.production_json)
    render_captions(
        args.merged_video_mp4,
        args.voiceover_wav,
        plan,
        args.out_video_mp4,
        model=args.model,
        lang=args.lang,
        work_dir=args.work_dir,
        scene_threshold=args.scene_threshold,
        title=args.title,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except common.StageBError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(3)
