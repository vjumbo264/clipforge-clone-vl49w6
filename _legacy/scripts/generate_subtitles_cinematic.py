#!/usr/bin/env python3
"""
Stage B subtitle step — CINEMATIC MODE.

This is ClipForge's sole caption renderer. Stage B always calls this
script to produce a bare 1080x1200 (10:9) cinematic frame with sentence-
level captions, a one-time title banner, and scene-level reframing.

Placement in the pipeline is intentionally distinct from the legacy
renderer: it runs AFTER cut_and_produce.py (and after the optional
enhance pass), normalises the source into the bare 1080x1200 (10:9)
cinematic frame, and burns captions plus the one-time title banner into
that frame. The Stage B workflow never runs brand_scenes.py for this
mode, so cinematic output cannot acquire legacy channel chrome.

---------------------------------------------------------------------------
CINEMATIC CAPTION BEHAVIOR
---------------------------------------------------------------------------
  * GRANULARITY — text is grouped and timed as complete sentence cards.
  * TIMING — each card appears fully at its first aligned word. Consecutive
    close-together cards swap directly with no fade, overlap, scale, tracking,
    or character-level animation. Only a long otherwise-empty gap causes the
    completed card to fade away before the next card arrives.
  * STYLE — compact all-caps Coolvetica text, centred in the frame both
    horizontally and vertically (Alignment=5), with a one-pixel subtle dark
    stroke for edge separation and the existing soft gray-black drop shadow
    immediately down and right of the glyphs. The narrow condensed caption
    font and bottom-of-frame placement of template mode are deliberately NOT
    carried over; this mode has no halo or separate glow layer.
  * KEYWORD COLORING — production.json may mark noteworthy words with an
    author-selected literal hex color (see load_script_with_keywords).
    The renderer applies that exact color and does not classify tone,
    sentiment, or emotional weight itself.
  * TITLE BANNER — a one-time intro element: a white full-width banner
    carrying the video's title drops in from off-screen top for two seconds,
    remains fully visible for at least fifteen seconds, then takes two seconds
    to drop back out (up, off the top of the frame) and is absent for the rest
    of the video. Banner graphic and title text move as ONE unit: the title is
    rendered INTO the banner image (Pillow) and the same image is animated as
    one overlay, so text and banner can never drift apart. The title
    typeface is Coolvetica, vendored at assets/fonts/Coolvetica.ttf —
    it replaces the narrow title font of old template mode INSIDE
    cinematic mode only; template mode itself is untouched here.

---------------------------------------------------------------------------
AUTHORITATIVE TEXT vs. TRANSCRIPTION
---------------------------------------------------------------------------
Same contract as template mode: the transcription supplies per-word
TIMING ONLY; the words displayed on screen come from the ORIGINAL
script (`voiceover_text` per cut, verbatim). Word timing comes from
the shared transcribe_words()/align_words_to_script() functions in
subtitle_common.py, which isolates timing policy from caption styling.

Usage:
    python generate_subtitles_cinematic.py <merged_video_mp4> <voiceover_wav>
                                 <out_video_mp4>
                                 [--script-json <production_or_manifest>]
                                 [--model base|small|tiny] [--lang auto|en|...]
                                 [--work-dir <path>]

Exit codes match the rest of the repo: 2 = bad input, 3 = validation
failure, subprocess failures propagate from sh().
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys

# Shared timing, alignment, ASS escaping, probing, and command helpers
# are styling-neutral so the sole cinematic renderer has no dependency on a
# retired subtitle mode.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import subtitle_common  # noqa: E402
import cinematic_reframe  # noqa: E402

# Pillow renders the title banner image (text composited into the white
# banner graphic before ffmpeg ever sees it). Already a pipeline
# dependency (brand_scene.py / brand_scenes.py).
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont  # noqa: E402


# ---------- Cinematic styling ----------
# Caption and banner typography share the vendored Coolvetica family.
# The explicit font file is also passed to libass through its fontsdir option
# below, so CI does not depend on a system-wide font installation.
CIN_FONT_FILE = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "assets", "fonts", "Coolvetica.ttf"))
CIN_FONT = "Coolvetica Rg"
CIN_FONT_FALLBACKS = ["DejaVu Sans", "Liberation Sans", "sans-serif"]
# Static caption cards use a deliberately small 3.2%-of-frame type scale so
# they read cleanly without dominating the live footage.
CIN_FONT_FRACTION_OF_HEIGHT = 0.032
# The timing/debug ASS sidecar approximates the production drop shadow with a
# near-black, high-opacity offset. The rendered video is authored by the Pillow raster
# compositor below, which supplies the actual Gaussian-softened edge.
CIN_ASS_SHADOW_COLOR = "&H10000000"
CIN_ASS_OUTLINE_COLOR = "&H00181818"
CIN_ASS_OUTLINE_WIDTH = 1
CIN_ASS_SHADOW_OFFSET_X_FRACTION = 0.0020
CIN_ASS_SHADOW_OFFSET_Y_FRACTION = 0.0035

# Production captions are rasterized with Pillow as three deliberate layers:
# a compact near-black high-opacity Gaussian shadow, a subtle one-pixel dark
# keyline, and literal per-word foreground fills. This is not a separate glow treatment.
CIN_RASTER_FPS = 24
CIN_RASTER_MAX_WIDTH_FRACTION = 0.86
CIN_RASTER_Y_FRACTION = 0.55
CIN_RASTER_SHADOW_RGB = (0, 0, 0)
CIN_RASTER_SHADOW_ALPHA = 0.94
CIN_RASTER_SHADOW_X = 4
CIN_RASTER_SHADOW_Y = 6
CIN_RASTER_SHADOW_BLUR_RADIUS = 5
# A narrow dark keyline preserves readability over pale, high-detail footage.
# It sits above the existing blur shadow and below the literal per-word fills.
CIN_RASTER_STROKE_RGB = (24, 24, 24)
CIN_RASTER_STROKE_WIDTH = 1
# ---------- Cinematic output frame ----------
# Cinematic output is always a bare 10:9 frame. Source material fills the
# canvas with a centred crop; no template, header, lower title block, or CTA
# is ever part of this renderer's filter graph.
CIN_FRAME_WIDTH = 1080
CIN_FRAME_HEIGHT = 1200

# ---------- Title banner (Batch 2) ----------
# One-time intro element: a white full-width banner drops in from the
# top of the frame at t=0, holds, then drops back out the same way and
# never returns. The banner graphic and its title text move as ONE unit
# — the text is rendered into the banner PNG and libass moves the whole
# image (DrawingModePictures dialogue), not a separate text event.
#
# Title face: Coolvetica, vendored next to the Batch 1 subtitle font.
# It replaces the narrow title font of old template mode WITHIN the
# cinematic banner only; template mode is untouched (its removal is a
# later batch). If the vendored file is ever missing we fall back to
# DejaVu Sans Bold with a warning rather than failing the render.
CIN_BANNER_FONT_FILE = CIN_FONT_FILE
CIN_BANNER_FONT_FALLBACK = "DejaVuSans-Bold.ttf"  # system fontconfig name
CIN_BANNER_HEIGHT_FRACTION = 0.11      # compact banner height vs frame height
CIN_BANNER_TOP_FRACTION = 0.0          # resting top edge: flush with y=0
CIN_BANNER_TEXT_WIDTH_FRACTION = 0.90  # max title width vs frame width
CIN_BANNER_IN_SECONDS = 2.0            # eased drop-in duration
CIN_BANNER_HOLD_SECONDS = 15.0         # fully-visible hold (minimum requested)
CIN_BANNER_OUT_SECONDS = 2.0           # eased drop-out duration (same motion, reversed)
CIN_BANNER_LAYER = 8                   # above the caption stacks (0-7)
CIN_BANNER_MIN_FONT_PX = 20            # autofit floor for long titles

# Caption cards remain static for ordinary close-together dialogue. When the
# next card is at least this far away, the completed card fades only within the
# otherwise-empty gap rather than lingering awkwardly or cutting abruptly.
CIN_CAPTION_LONG_GAP_SECONDS = 3.0
CIN_CAPTION_GAP_FADE_SECONDS = 1.0
# Edge TTS narration is concatenated without editorial silence. A gap this
# large in the aligned card timeline is therefore a timing failure, not a
# legitimate pause to render blank; fail before shipping missing captions.
CIN_CAPTION_MAX_TIMELINE_GAP_SECONDS = CIN_CAPTION_LONG_GAP_SECONDS
CIN_CAPTION_EDGE_TOLERANCE_SECONDS = 2.0

# ---------- Author-controlled keyword colors ----------
# production.json optionally carries literal #RRGGBB values chosen by its
# authoring process. This renderer only validates and applies them; it never
# maps tone or sentiment labels to color.
_HEX_COLOR_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")


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

_SENT_END_RE = re.compile(r"[.!?…][\"')\]]*$")
_CLAUSE_BREAK_RE = re.compile(r"[,;:—–][\"')\]]*$")
MAX_CAPTION_WORDS = 6
_MIN_TRAILING_CAPTION_WORDS = 2


def _caption_card_chunks(words: list[dict]) -> list[list[dict]]:
    """Split one sentence into short cards, preferring nearby clause breaks."""
    cards: list[list[dict]] = []
    remaining = list(words)
    while len(remaining) > MAX_CAPTION_WORDS:
        # Leave at least two words for the next card when possible. Among the
        # final few legal positions, prefer the latest natural clause break;
        # otherwise use the longest comfortable card.
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
    """Return static voice-aligned caption cards from timed script words.

    Sentence-final punctuation defines the initial grouping. Long sentences
    are then divided into cards of at most six words, preferring a nearby
    comma, semicolon, colon, or dash; each card keeps the exact start/end
    timing of its own words. A one-word trailing fragment is avoided when a
    shorter preceding card can leave a readable two-word final card.
    """
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
            cards.append({
                "words": card_words,
                "start": card_words[0]["start"],
                "speak_end": card_words[-1]["end"],
            })
    print(f"Grouped {len(events)} words into {len(cards)} caption card(s) "
          f"(max {MAX_CAPTION_WORDS} words each).", flush=True)
    return cards


def load_script_with_keywords(script_json: str) -> tuple[list[str], dict]:
    """
    Load the ORIGINAL script (one voiceover_text per cut, in cut order)
    plus additive, author-selected keyword-color metadata.

    Accepted keyword shapes per cut (all optional, additive only):
        "keywords": [{"word": "betrayal", "color": "#FF5C5C"}, ...]
        "keywords": {"betrayal": "#FF5C5C", ...}

    Returns (texts, keyword_map) where keyword_map maps a normalised token to
    an exact #RRGGBB literal. Tone-only legacy entries are deliberately
    ignored with a warning: there is no renderer-side tone palette.
    """
    with open(script_json, "r", encoding="utf-8") as f:
        payload = json.load(f)
    cuts = payload.get("cuts") or []
    if not cuts:
        print(f"{script_json} contains no cuts", file=sys.stderr)
        sys.exit(2)
    if all(isinstance(c, dict) and "index" in c for c in cuts):
        cuts = sorted(cuts, key=lambda c: int(c["index"]))
    else:
        cuts = sorted(cuts, key=lambda c: float(c.get("start_seconds", 0)))

    texts: list[str] = []
    keyword_map: dict[str, str] = {}
    for i, c in enumerate(cuts):
        text = (c.get("voiceover_text") or c.get("raw_narration") or "").strip()
        if not text:
            print(
                f"{script_json} cut #{i} has no voiceover_text (and no "
                f"legacy raw_narration fallback) — the original script is "
                f"the authoritative subtitle source, so a cut without "
                f"script text cannot be subtitled correctly. Fix the "
                f"production.json and re-run.",
                file=sys.stderr,
            )
            sys.exit(2)
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
            word = subtitle_common._norm_token(str(k.get("word") or ""))
            color = _normalise_hex_color(k.get("color"))
            if not word:
                continue
            if color:
                keyword_map[word] = color
            elif k.get("tone"):
                print(
                    f"WARNING: ignoring legacy tone-only keyword {word!r}; "
                    "production.json must provide a literal #RRGGBB color.",
                    file=sys.stderr,
                )
            elif k.get("color") is not None:
                raise ValueError(
                    f"Invalid keyword color for {word!r}: {k.get('color')!r}. "
                    "Use a #RRGGBB literal.")
    if keyword_map:
        print(f"Loaded {len(keyword_map)} author-selected keyword color(s): "
              f"{keyword_map}", flush=True)
    return texts, keyword_map


def load_banner_title(script_json: str) -> str:
    """Read the video title (production.json's top-level "title").

    Additive and forgiving: a missing/empty title yields "" and the
    caller renders without a banner instead of failing — the banner is
    decoration, the captions are the contract. The script file itself
    was already validated by load_script_with_keywords().
    """
    try:
        with open(script_json, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("title") or "").strip()


_banner_font_warned = False


def _load_banner_font(size: int):
    """Coolvetica (vendored) if present, else DejaVu Sans Bold."""
    global _banner_font_warned
    if os.path.isfile(CIN_BANNER_FONT_FILE):
        return ImageFont.truetype(CIN_BANNER_FONT_FILE, size)
    if not _banner_font_warned:
        _banner_font_warned = True
        print(
            f"WARNING: vendored banner font missing at "
            f"{CIN_BANNER_FONT_FILE} — falling back to "
            f"{CIN_BANNER_FONT_FALLBACK}.",
            file=sys.stderr, flush=True,
        )
    try:
        return ImageFont.truetype(CIN_BANNER_FONT_FALLBACK, size)
    except OSError:
        return ImageFont.load_default()


def build_banner_png(title: str, width: int, height: int,
                     out_png: str) -> int:
    """
    Render the title banner as a single PNG: a full-width white strip
    with the title centred on it in Coolvetica. Returning the banner
    height lets the caller place the ASS \\move anchors precisely.

    Because the title is composited INTO the image here, the ffmpeg
    side only ever animates one opaque rectangle — banner and text are
    one unit by construction (they can never be animated separately).

    The title auto-fits: it starts large (about half the banner height)
    and shrinks until it sits inside CIN_BANNER_TEXT_WIDTH_FRACTION of
    the frame width, with a floor at CIN_BANNER_MIN_FONT_PX. Long
    titles are never truncated — they just set smaller.
    """
    banner_h = max(48, int(round(height * CIN_BANNER_HEIGHT_FRACTION)))
    # The banner is the final rendering boundary for its text, so enforce
    # uppercase here rather than relying on the source title or a caller.
    banner_title = title.upper()
    img = Image.new("RGB", (width, banner_h), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    max_text_w = int(round(width * CIN_BANNER_TEXT_WIDTH_FRACTION))
    size = max(CIN_BANNER_MIN_FONT_PX, int(round(banner_h * 0.52)))
    font = _load_banner_font(size)
    while size > CIN_BANNER_MIN_FONT_PX and \
            draw.textlength(banner_title, font=font) > max_text_w:
        size -= 2
        font = _load_banner_font(size)

    bbox = draw.textbbox((0, 0), banner_title, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    # Centre optically: shift by the bbox origin so ascender/descender
    # metrics don't push the glyphs off-centre.
    x = (width - text_w) // 2 - bbox[0]
    y = (banner_h - text_h) // 2 - bbox[1]
    draw.text((x, y), banner_title, font=font, fill=(18, 18, 18))

    img.save(out_png)
    print(
        f"Title banner rendered: {out_png} ({width}x{banner_h}, "
        f"{os.path.basename(CIN_BANNER_FONT_FILE)} {size}px, "
        f"title={banner_title!r})",
        flush=True,
    )
    return banner_h


def write_cinematic_ass(sentences: list[dict], keyword_map: dict,
                        width: int, height: int, out_ass: str) -> None:
    """
    One sentence = TWO static Dialogue events sharing the same voice-aligned
    timespan: a close, low-opacity diagnostic shadow and crisp foreground
    text. The production compositor uses Pillow to add the true soft Gaussian
    drop shadow; these centred (Alignment=5) events are a timing sidecar only.
    """
    font_size = max(24, int(round(height * CIN_FONT_FRACTION_OF_HEIGHT)))
    shadow_x = max(1, int(round(width * CIN_ASS_SHADOW_OFFSET_X_FRACTION)))
    shadow_y = max(2, int(round(height * CIN_ASS_SHADOW_OFFSET_Y_FRACTION)))
    margin = int(round(width * 0.06))  # side clearance for wrapped lines

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
        start_ts = subtitle_common._ass_time(sent["start"])
        end_ts = subtitle_common._ass_time(sent["speak_end"])
        shadow_parts: list[str] = []
        text_parts: list[str] = []
        for word in words:
            # Source wording remains authoritative for timing and keyword
            # lookup, while the on-screen contract deliberately uppercases
            # every caption run (including the ASS diagnostic sidecar).
            display_word = str(word["word"]).upper()
            color = keyword_map.get(subtitle_common._norm_token(word["word"]))
            shadow_parts.append("{\\c&H000000&}" + subtitle_common._ass_escape(display_word))
            text_tags = f"{{\\c{_hex_ass(color)}&}}" if color else ""
            text_parts.append(text_tags + subtitle_common._ass_escape(display_word))

        shadow_text = " ".join(shadow_parts)
        main_text = " ".join(text_parts)
        lines.append(
            f"Dialogue: 0,{start_ts},{end_ts},CinShadow,,0,0,0,,"
            f"{{\\pos({width // 2 + shadow_x},{height // 2 + shadow_y})}}{shadow_text}\n"
        )
        lines.append(
            f"Dialogue: 1,{start_ts},{end_ts},CinText,,0,0,0,,{main_text}\n"
        )

    with open(out_ass, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(
        f"ASS cinematic subtitle file written: {out_ass} "
        f"({len(sentences)} sentence event stack(s), font {CIN_FONT} Regular "
        f"{font_size}px, all-caps foreground + {CIN_ASS_OUTLINE_WIDTH}px dark "
        f"outline + compact soft-gray shadow diagnostic offset {shadow_x}px right / "
        f"{shadow_y}px down — CENTRED in "
        f"the {width}x{height} video frame; static complete-sentence "
        f"events aligned to the voiceover, no caption animation; rendered "
        f"into the bare 1080x1200 cinematic frame "
        f"with no downstream branded compositor)",
        flush=True,
    )


def _banner_y_expr(rest_top: int) -> str:
    """
    Y position of the banner's TOP edge at time t — the drop-in / hold /
    drop-out schedule in one piecewise expression (ffmpeg if()).

      drop-in : t in [0, in)          — the whole banner slides DOWN
                from fully off-screen top (top edge -overlay_h, i.e. the banner
                just above the frame) to its resting slot with cubic easing;
      hold    : t in [in, in+hold)    — parked at rest_top;
      drop-out: t in [in+hold, out)   — the SAME move reversed (slides
                back UP, top edge from rest_top to -overlay_h);
      after   : top edge stays pinned at -overlay_h. The overlay source remains
                alive for the complete base-video duration, so this final
                branch is visibly evaluated rather than relying on an input
                trim or an enable-boundary to hide the exit animation.

    `overlay_h` is the banner image's own height in the overlay filter.
    Use the explicit FFmpeg variable rather than `H`, which denotes the main
    video height and would make a compact banner travel too far too late.
    """
    t_in = CIN_BANNER_IN_SECONDS
    t_hold = t_in + CIN_BANNER_HOLD_SECONDS
    t_out = t_hold + CIN_BANNER_OUT_SECONDS
    # cubic ease-out: fast initial travel with a smooth settle at the
    # visible slot. Use the mirrored curve for the exit so both moves feel
    # designed rather than mechanically linear.
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


def _probe_video_duration(video: str) -> float:
    """Return the input duration in seconds for frame-exact overlays."""
    raw = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", video,
    ], text=True).strip()
    duration = float(raw)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"Invalid video duration reported for {video}: {raw!r}")
    return duration


def _caption_font(size: int) -> ImageFont.FreeTypeFont:
    if not os.path.isfile(CIN_FONT_FILE):
        raise FileNotFoundError(f"Missing cinematic font: {CIN_FONT_FILE}")
    return ImageFont.truetype(CIN_FONT_FILE, size)


def _caption_color(color: str | None) -> tuple[int, int, int]:
    """Return the literal author-selected RGB value, or white when absent."""
    return _hex_rgb(color)


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


def normalize_caption_edge_coverage(sentences: list[dict], narration_start: float, narration_end: float) -> None:
    """Extend only the edge cards over detected narration silence.

    VAD-backed transcription deliberately omits leading and trailing silence.
    The first/last visual caption card therefore needs to cover that silence so
    an opening or closing pause cannot trigger a false timeline failure. This
    does not alter any displayed word or conceal interior timing gaps.
    """
    if not sentences:
        return
    if math.isfinite(narration_start) and narration_start > 0:
        sentences[0]["start"] = 0.0
    if math.isfinite(narration_end) and narration_end > 0:
        sentences[-1]["speak_end"] = max(float(sentences[-1]["speak_end"]), narration_end)


def normalize_caption_internal_coverage(sentences: list[dict]) -> None:
    """Hold a caption card across large timing gaps between adjacent cards.

    The rendered text is taken verbatim from the original script, whereas
    Whisper is used only to estimate word timing. When Whisper skips a phrase,
    the next reliable anchor can be several seconds later even though the
    authored narration is continuous. Keeping the preceding card visible until
    that anchor prevents the renderer's long-gap fade from creating an empty
    subtitle interval. Invalid or out-of-order cards remain the validator's
    responsibility and are never silently repaired here.
    """
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
            f"Held {normalized} caption card(s) across large transcription "
            "timing gap(s) to preserve continuous subtitle coverage.",
            flush=True,
        )


def validate_caption_timeline(sentences: list[dict], video_duration: float) -> None:
    """Fail closed when caption cards cannot continuously cover narration.

    The authoritative script is spoken by Edge TTS cut-by-cut and the merged
    voiceover is concatenated without editorial silence. Caption cards should
    therefore begin near the video origin, end near its final narration, and
    never contain an internal blank interval large enough for the renderer's
    long-gap fade. This catches clock-rate errors and malformed word alignment
    before a visually plausible but incomplete subtitle overlay is released.
    """
    if not sentences:
        raise ValueError("Cinematic subtitle timeline has no caption cards.")
    if not math.isfinite(video_duration) or video_duration <= 0:
        raise ValueError(f"Invalid final-video duration for caption timeline: {video_duration!r}")

    previous_end = 0.0
    for index, sentence in enumerate(sentences, start=1):
        start = float(sentence["start"])
        end = float(sentence["speak_end"])
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            raise ValueError(
                f"Caption card #{index} has invalid timing [{start!r}, {end!r}]."
            )
        if start < -CIN_CAPTION_EDGE_TOLERANCE_SECONDS or \
                end > video_duration + CIN_CAPTION_EDGE_TOLERANCE_SECONDS:
            raise ValueError(
                f"Caption card #{index} spans [{start:.3f}s, {end:.3f}s] outside "
                f"the final video timeline [0.000s, {video_duration:.3f}s]. "
                "Refusing to burn captions with a mismatched timing source."
            )
        if index == 1:
            if start > CIN_CAPTION_EDGE_TOLERANCE_SECONDS:
                raise ValueError(
                    f"First caption starts at {start:.3f}s, leaving an unexpected "
                    "opening subtitle gap."
                )
        else:
            gap = start - previous_end
            if gap > CIN_CAPTION_MAX_TIMELINE_GAP_SECONDS:
                raise ValueError(
                    f"Caption cards #{index - 1} and #{index} contain a {gap:.3f}s "
                    "internal blank gap. This would fade captions out while narration "
                    "continues, so Stage B is refusing the release."
                )
        previous_end = max(previous_end, end)

    trailing_gap = video_duration - previous_end
    if trailing_gap > CIN_CAPTION_EDGE_TOLERANCE_SECONDS:
        raise ValueError(
            f"Last caption ends at {previous_end:.3f}s but the final video lasts "
            f"{video_duration:.3f}s, leaving a {trailing_gap:.3f}s uncaptained tail."
        )
    print(
        f"Caption timeline validated: {len(sentences)} card(s) cover "
        f"{sentences[0]['start']:.3f}s..{previous_end:.3f}s of the "
        f"{video_duration:.3f}s final video without long blank gaps.",
        flush=True,
    )


def _caption_card_opacity(sentences: list[dict], index: int, time_s: float) -> float:
    """Return a static card's gap-aware opacity without permitting overlap."""
    sentence = sentences[index]
    start = float(sentence["start"])
    spoken_end = float(sentence["speak_end"])
    next_start = (float(sentences[index + 1]["start"])
                  if index + 1 < len(sentences) else spoken_end)

    if time_s < start or time_s >= next_start:
        return 0.0
    # The next card is not yet ready. Short pauses retain the completed card
    # at full opacity and swap directly when the next card starts.
    gap = next_start - spoken_end
    if gap < CIN_CAPTION_LONG_GAP_SECONDS or time_s < spoken_end:
        return 1.0

    # In a long otherwise-empty pause, wait up to the threshold then use a
    # one-second fade. If the gap is only barely long enough, pull the fade
    # earlier so it can finish cleanly before the incoming card cuts in.
    fade_start = min(spoken_end + CIN_CAPTION_LONG_GAP_SECONDS,
                     next_start - CIN_CAPTION_GAP_FADE_SECONDS)
    fade_end = min(fade_start + CIN_CAPTION_GAP_FADE_SECONDS, next_start)
    if time_s < fade_start:
        return 1.0
    if time_s >= fade_end:
        return 0.0
    return max(0.0, min(1.0, (fade_end - time_s) /
                        max(fade_end - fade_start, 0.001)))


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
        # Cards are static for close dialogue. A completed card may only fade
        # during a long otherwise-empty gap and is always cut at the incoming
        # card's exact start, so the renderer never produces an overlap.
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
            color = _caption_color(keyword_map.get(subtitle_common._norm_token(word["word"])))
            if color not in masks:
                masks[color] = Image.new("L", (width, height), 0)
                draw_by_color[color] = ImageDraw.Draw(masks[color])
            draw_by_color[color].text((run["x"], run["y"]), run["display"],
                                      font=font, fill=255)
            stroke_draw.text((run["x"], run["y"]), run["display"], font=font,
                             fill=255, stroke_width=CIN_RASTER_STROKE_WIDTH,
                             stroke_fill=255)
        for mask in masks.values():
            union = ImageChops.lighter(union, mask)
        if not union.getbbox():
            continue
        soft_shadow = union.filter(
            ImageFilter.GaussianBlur(radius=CIN_RASTER_SHADOW_BLUR_RADIUS))
        _apply_mask(foreground, soft_shadow, CIN_RASTER_SHADOW_RGB,
                    CIN_RASTER_SHADOW_ALPHA * card_opacity,
                    (CIN_RASTER_SHADOW_X, CIN_RASTER_SHADOW_Y))
        _apply_mask(foreground, stroke_union, CIN_RASTER_STROKE_RGB,
                    card_opacity)
        for color, mask in masks.items():
            _apply_mask(foreground, mask, color, card_opacity)
    return foreground


def render_cinematic_overlays(sentences: list[dict], keyword_map: dict,
                              width: int, height: int, duration: float,
                              out_foreground_mov: str) -> None:
    """Encode the single soft-drop-shadow caption overlay stream."""
    font_size = max(24, int(round(height * CIN_FONT_FRACTION_OF_HEIGHT)))
    font = _caption_font(font_size)
    frame_count = max(1, int(math.ceil(duration * CIN_RASTER_FPS)))
    cmd = [
        "ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
        "-s", f"{width}x{height}", "-r", str(CIN_RASTER_FPS), "-i", "-",
        "-an", "-c:v", "qtrle", "-pix_fmt", "argb", out_foreground_mov,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    print(f"Rendering {frame_count} soft-drop-shadow caption frame(s) at "
          f"{CIN_RASTER_FPS}fps -> {out_foreground_mov}", flush=True)
    try:
        for frame_index in range(frame_count):
            foreground = _raster_caption_layers(
                sentences, keyword_map, width, height,
                frame_index / CIN_RASTER_FPS, font)
            assert proc.stdin is not None
            proc.stdin.write(foreground.tobytes())
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed while encoding flat-shadow caption stream")


def burn_subtitles(video: str, caption_foreground_mov: str, dst: str,
                   banner: dict | None = None,
                   crop_plan: dict | None = None) -> None:
    """Apply scene-static crops, then composite flat 3D-shadow captions and banner."""
    inputs = ["-i", video, "-i", caption_foreground_mov]
    if crop_plan is None:
        # Defensive default for direct callers: a single centre crop preserves
        # pre-Batch-D behavior while the normal CLI always supplies a plan.
        crop_plan = {
            "target_width": CIN_FRAME_WIDTH,
            "target_height": CIN_FRAME_HEIGHT,
            "scenes": [{"start_seconds": 0.0, "end_seconds": _probe_video_duration(video),
                        "crop_center_x": 0.5, "crop_center_y": 0.5}],
        }
    crop_filters, crop_label = cinematic_reframe.scene_crop_filter(crop_plan, "0:v")
    filters = [
        *crop_filters,
        f"[{crop_label}]format=gbrp[base]",
        "[1:v]setpts=PTS-STARTPTS,format=rgba[foreground]",
        "[base][foreground]overlay=eof_action=pass:repeatlast=0[v0]",
    ]
    out_label = "v0"
    if banner is not None:
        rest_top = int(round(CIN_FRAME_HEIGHT * CIN_BANNER_TOP_FRACTION))
        # Keep the image stream alive for the whole base-video timeline. The
        # y-expression itself owns every state: two-second entry, fifteen-
        # second hold, two-second exit, then off-screen. In particular, do not
        # trim the source or gate it with enable= at the exit boundary: both
        # approaches can prevent the final animated frames from reaching the
        # compositor.
        inputs += ["-loop", "1", "-i", banner["png"]]
        y_expr = _banner_y_expr(rest_top)
        filters.append("[2:v]setpts=PTS-STARTPTS[bimg]")
        filters.append(
            f"[v0][bimg]overlay=x=0:y='{y_expr}':eval=frame"
            f":eof_action=pass:repeatlast=0:shortest=1[v1]")
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
    subtitle_common.sh(cmd)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("merged_video_mp4")
    ap.add_argument("voiceover_wav")
    ap.add_argument("out_video_mp4")
    ap.add_argument("--script-json", default=None,
                    help="production.json / cuts.json / "
                         "voiceover_manifest.json carrying the ORIGINAL "
                         "script (voiceover_text per cut) plus optional "
                         "literal keyword colors. Strongly recommended: "
                         "without it the transcription's wording is used "
                         "as a legacy fallback.")
    ap.add_argument("--model", default="base", choices=["tiny", "base", "small"])
    ap.add_argument("--lang", default="auto")
    ap.add_argument("--work-dir", default=None,
                    help="scratch dir for transcript, ASS debug data, and raster caption overlay (default: "
                         "<out dir>/subtitle_work_cinematic)")
    ap.add_argument("--scene-threshold", type=float,
                    default=cinematic_reframe.DEFAULT_SCENE_THRESHOLD,
                    help="ffmpeg scene-score threshold used for static cinematic crop planning (default: 0.35)")
    ap.add_argument("--title", default=None,
                    help="video title for the cinematic intro banner. "
                         "Default: production.json's top-level 'title' "
                         "(--script-json). If neither is available the "
                         "banner is skipped with a warning (captions "
                         "still render).")
    args = ap.parse_args()

    for req in (args.merged_video_mp4, args.voiceover_wav):
        if not os.path.exists(req):
            print(f"Missing required input: {req}", file=sys.stderr)
            sys.exit(2)
    if args.script_json and not os.path.exists(args.script_json):
        print(f"Missing required input: {args.script_json}", file=sys.stderr)
        sys.exit(2)

    out_dir = os.path.dirname(os.path.abspath(args.out_video_mp4)) or "."
    os.makedirs(out_dir, exist_ok=True)
    work_dir = args.work_dir or os.path.join(out_dir, "subtitle_work_cinematic")
    os.makedirs(work_dir, exist_ok=True)

    # Transcription = TIMING SOURCE ONLY (shared with template mode).
    # Probe once and use the exact final-video duration for all subtitle timing
    # validation, crop planning, and raster overlay generation.
    video_duration = _probe_video_duration(args.merged_video_mp4)
    timed_words = subtitle_common.transcribe_words(args.voiceover_wav, args.model,
                                          args.lang, work_dir)

    # Exact per-cut durations written by cut_and_produce.py next to the
    # merged voiceover WAV. When present they replace the proportional
    # word-count quota partitioning inside align_words_to_script() with the
    # real cut boundaries the voiceover was concatenated from.
    cut_durations: list[float] | None = None
    cut_timing_path = os.path.join(
        os.path.dirname(os.path.abspath(args.voiceover_wav)) or ".",
        "cut_timing.json",
    )
    if os.path.exists(cut_timing_path):
        try:
            with open(cut_timing_path, "r", encoding="utf-8") as f:
                timing_payload = json.load(f)
            timing_cuts = timing_payload.get("cuts") or []
            durations = [float(entry["video_seconds"]) for entry in timing_cuts]
            if not durations or not all(duration > 0 for duration in durations):
                raise ValueError("sidecar contains no positive durations")
            cut_durations = durations
            print(
                f"Loaded exact per-cut timing from {cut_timing_path} "
                f"({len(durations)} cut(s), {sum(durations):.2f}s total).",
                flush=True,
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(
                f"WARNING: could not use {cut_timing_path} ({exc}); caption "
                "timing is approximate for this job (proportional word-count "
                "partitioning).",
                file=sys.stderr, flush=True,
            )
    else:
        print(
            "NOTE: no cut_timing.json next to the voiceover WAV (this job "
            "predates exact per-cut timing) — caption timing is approximate "
            "for this job (proportional word-count partitioning).",
            flush=True,
        )

    keyword_map: dict[str, str] = {}
    if args.script_json:
        # Original script = AUTHORITATIVE WORDING; keywords = additive
        # literal author-selected keyword-color metadata.
        script_texts, keyword_map = load_script_with_keywords(args.script_json)
        words = subtitle_common.align_words_to_script(timed_words, script_texts, cut_durations=cut_durations)
    else:
        print(
            "WARNING: no --script-json given — falling back to the "
            "transcription's own words for display. The original script is "
            "the intended authoritative subtitle source; pass "
            "production.json (or the voiceover manifest) via --script-json.",
            file=sys.stderr,
            flush=True,
        )
        words = timed_words

    sentences = split_sentences(words)
    narration_start = min((float(event["start"]) for event in timed_words), default=0.0)
    narration_end = max((float(event["end"]) for event in timed_words), default=0.0)
    normalize_caption_edge_coverage(sentences, narration_start, narration_end)
    normalize_caption_internal_coverage(sentences)
    validate_caption_timeline(sentences, video_duration)

    source_width, source_height = subtitle_common.probe_video_size(args.merged_video_mp4)
    width, height = CIN_FRAME_WIDTH, CIN_FRAME_HEIGHT
    crop_plan = cinematic_reframe.build_scene_crop_plan(
        args.merged_video_mp4,
        duration_seconds=video_duration,
        threshold=args.scene_threshold,
        enable_face_detection=True,
    )
    crop_plan_path = os.path.join(work_dir, "cinematic_crop_plan.json")
    cinematic_reframe.write_crop_plan(crop_plan, crop_plan_path)
    print(
        f"Source video resolution: {source_width}x{source_height}; "
        f"cinematic output uses {crop_plan['scene_count']} static scene crop(s) "
        f"in the bare {width}x{height} (10:9) frame with face-centered positions "
        f"when a confident face is detected.",
        flush=True,
    )

    # ---- Title banner (Batch 2): white drop-in intro banner ----
    banner = None
    title = (args.title or "").strip()
    if not title and args.script_json:
        title = load_banner_title(args.script_json)
    if title:
        banner_png = os.path.join(work_dir, "title_banner.png")
        banner_h = build_banner_png(title, width, height, banner_png)
        banner = {"png": banner_png, "height": banner_h}
    else:
        print(
            "WARNING: no video title available (production.json['title'] "
            "empty and no --title given) — rendering WITHOUT the title "
            "banner; captions are unaffected.",
            file=sys.stderr, flush=True,
        )

    # Retain the ASS as a human-readable timing/debug artefact, while the
    # production image uses the Pillow-rendered compact soft-drop-shadow treatment.
    ass_path = os.path.join(work_dir, "subtitles_cinematic.ass")
    write_cinematic_ass(sentences, keyword_map, width, height, ass_path)
    foreground_mov = os.path.join(work_dir, "cinematic_caption_flat_shadow.mov")
    render_cinematic_overlays(
        sentences, keyword_map, width, height,
        video_duration, foreground_mov)

    print(f"Compositing flat-3D-shadow cinematic captions"
          f"{' + title banner' if banner else ''} into "
          f"{args.out_video_mp4} ...", flush=True)
    burn_subtitles(args.merged_video_mp4, foreground_mov,
                   args.out_video_mp4, banner=banner, crop_plan=crop_plan)
    print(f"Final cinematically-subtitled video written: "
          f"{args.out_video_mp4}", flush=True)


if __name__ == "__main__":
    main()
