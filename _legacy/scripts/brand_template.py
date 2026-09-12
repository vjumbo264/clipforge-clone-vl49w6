#!/usr/bin/env python3
"""
Branded 10:9 template renderer (compositor Part 2 of the branding stack).

This module renders the STATIC chrome around a scene clip — the tinted
vertical canvas, the ringed profile picture, the username / display name
row, the category badge, the auto-fitted title block, the follow button,
and the like/comment/share engagement prompt — as a single transparent
RGBA PNG that matches the final output canvas pixel-for-pixel (1080×1200).

It also computes the exact rectangle (`slot`) inside that canvas where the
source scene video must be pasted, in native aspect ratio, letterboxed —
never stretched, never cropped. The companion `brand_scene.py` uses that
rectangle to build the ffmpeg filter graph that letterboxes each source
clip into the slot and then overlays this template PNG on top.

Why a Pillow overlay instead of pure ffmpeg drawtext / drawbox:
  * drawtext cannot auto-wrap or auto-shrink a title of unknown length —
    a 6-word title and a 22-word title need very different type sizes and
    line counts to look correct. Doing that in ffmpeg means measuring
    text through a subprocess loop; doing it in Pillow is one function.
  * Rounded corners, ring strokes, shadows, and translucent panels all
    exist as one-liners in Pillow but are painful (or impossible) to
    express in the drawbox filter cleanly.
  * The overlay is generated ONCE per scene and reused for every frame,
    so per-frame ffmpeg cost is just a single `overlay=` composite over
    the letterboxed video — cheap and hardware-decoder-friendly.

This file has NO knowledge of ffmpeg, video codecs, or the Stage B
workflow. It renders a PNG and reports geometry; nothing else. That
keeps it composable with, and independent of, the mobile-safe encode
stage in cut_and_produce.py / enhance_scenes.py, and with the (separate)
wiring step that plugs this compositor into stage-b.yml.

Design bar (must match or exceed the previously-approved prototype):
  * Dark tinted vertical canvas — not black; a subtly-graded charcoal
    with an accent-tinted glow so the frame reads as designed, not raw.
  * Ringed profile picture: circular avatar clipped to a disc, an
    accent-colored 6 px ring, and a thin outer separator so it pops off
    any avatar color.
  * Category badge: small, uppercase, tracked, accent-colored pill.
  * Typography hierarchy: display name (bold, bright) > @username
    (muted) > title (very bold, biggest) > CTA row.
  * Accent color used consistently across ring, badge, title underline,
    and follow button — a single visual signature that ties the frame
    together instead of scattered spot colors.
  * Rounded follow button + separate engagement prompt (Like / Comment /
    Share) so the call-to-action is not JUST "follow".
  * Long titles wrap and shrink to fit; short titles are centered and
    stay large. The title block never overflows its allotted region.

The colors, sizes, and paddings are ALL kept as module-level constants
at the top of the file so they can be audited or retuned in one place
without spelunking through the render code.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont


# =============================================================================
# CANVAS / LAYOUT CONSTANTS
# =============================================================================
# 10:9 near-square vertical (taller than wide, but not the 9:16 full-vertical
# of old template mode): cinematic mode's output aspect ratio. Width 1080 keeps
# the 1080p encoding base; 1080 / 10 * 9 = 972 is even, so no odd-dimension
# rounding anywhere, and 1080x1200 stays well inside the mobile-safe H.264
# High@L4.0 macroblock budget the rest of the pipeline encodes to.
CANVAS_W = 1080
CANVAS_H = 1200

# Video slot geometry (max box the source clip is letterboxed into).
# * Width 1080 -> full-bleed left/right so the clip is the visual anchor.
# * Height 608 -> a 16:9 clip lands EXACTLY at 1080x608 (16:9 = 1.777…);
#   4:3 sources leave black pillarboxes inside the slot; taller-than-16:9
#   sources leave black letterboxes above/below inside the slot. In every
#   case the source aspect ratio is preserved — never stretched, never
#   cropped.
# * Y=268 centres the slot in the chrome-free band of the shorter 10:9
#   canvas: the header row (avatar / name / badge) ends around y=208 and
#   the title block starts at TITLE_TOP, so the clip keeps generous
#   vertical room on both sides without crowding either chrome block.
SLOT_X = 0
SLOT_Y = 268
SLOT_W = 1080
SLOT_H = 608

# Colours. Chosen for high-contrast readability on a phone screen at
# arm's length and for a single-accent visual identity across the frame.
ACCENT = (255, 59, 92, 255)          # #FF3B5C — commentary-channel accent
ACCENT_SOFT = (255, 59, 92, 90)      # translucent accent for glows/rings
BG_TOP = (17, 19, 25, 255)           # #111319 — canvas top
BG_BOTTOM = (28, 31, 40, 255)        # #1C1F28 — canvas bottom
TEXT_BRIGHT = (245, 246, 250, 255)   # near-white
TEXT_MUTED = (170, 176, 190, 255)    # cool grey for @handle / secondary
TEXT_ON_ACCENT = (255, 255, 255, 255)
SLOT_BORDER = (255, 59, 92, 220)     # thin accent-tinted frame around clip
SLOT_SHADOW = (0, 0, 0, 140)         # soft drop shadow behind clip

# Font search list. On GitHub ubuntu-latest runners DejaVu Sans is always
# installed (bundled with the base image); Liberation Sans and Noto Sans
# are the standard fallbacks. If none of these are found the code falls
# back to Pillow's default bitmap font — the render will still work, it
# just won't look as clean.
FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
]
FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
]

# Header row geometry (top of frame — avatar + name + handle + badge).
HEADER_Y = 72
AVATAR_D = 128                       # diameter in px
AVATAR_X = 60
AVATAR_RING_W = 6                    # accent ring thickness
AVATAR_SEP_W = 2                     # thin dark separator between ring & disc

# Category badge shown to the right of the name row.
BADGE_TEXT_DEFAULT = "COMMENTARY"

# Title block sits between the video slot and the CTA row. The 10:9 canvas
# is 720 px shorter than the old 9:16 one, so the block is compressed
# accordingly (the auto-fit shrink logic absorbs the reduced height).
TITLE_TOP = SLOT_Y + SLOT_H + 36     # breathing room below clip
TITLE_BOTTOM = 1046                  # hard bottom before the CTA row
TITLE_PAD_X = 60                     # left/right inset for wrapping

# CTA row anchors at the bottom of the frame.
CTA_BTN_Y = 1058
CTA_BTN_H = 92
CTA_BTN_W = 460
CTA_BTN_RADIUS = 46
ENGAGEMENT_Y = 1158                  # centered text under the button


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class Branding:
    """
    Persistent channel branding, as stored in branding/branding.json.
    profile_picture is a filesystem path (may be empty / absent — the
    renderer draws a graceful placeholder disc in that case).
    """
    username: str = ""
    display_name: str = ""
    profile_picture: str = ""


@dataclass
class SlotGeometry:
    """
    Where inside the 1080×1200 branded canvas the source clip must land,
    letterboxed at native aspect ratio. `brand_scene.py` consumes this to
    build the ffmpeg filter graph (scale + pad + overlay). Kept as a
    small dataclass so the (compositor <-> caller) contract is explicit.
    """
    canvas_w: int
    canvas_h: int
    x: int
    y: int
    w: int
    h: int


# =============================================================================
# FONT HELPERS
# =============================================================================

def _load_font(paths: list[str], size: int) -> ImageFont.FreeTypeFont:
    """
    Try each candidate font path in order; fall back to Pillow's default.
    We deliberately try FreeType first — the default bitmap font can't be
    resized, so a fallback title would look tiny at 1080p.
    """
    for p in paths:
        try:
            return ImageFont.truetype(p, size=size)
        except (OSError, IOError):
            continue
    # Pillow default — ugly at large sizes, but the code stays functional.
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    """
    Return the (width, height) of `text` in `font`. Uses textbbox because
    textsize was removed in Pillow 10+. Returns (0, 0) for empty text.
    """
    if not text:
        return (0, 0)
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    return (r - l, b - t)


# =============================================================================
# BACKGROUND / DECOR
# =============================================================================

def _draw_background(canvas: Image.Image) -> None:
    """
    Fill the canvas with a subtle vertical gradient (BG_TOP -> BG_BOTTOM)
    plus a soft accent glow behind the video slot. The glow gives the
    frame a designed look without being noisy — a pure flat charcoal
    would read as a placeholder, not a template.
    """
    w, h = canvas.size
    px = canvas.load()
    # Vertical gradient, computed per row.
    for y in range(h):
        t = y / max(h - 1, 1)
        r = int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
        g = int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
        b = int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
        for x in range(w):
            px[x, y] = (r, g, b, 255)

    # Soft accent glow behind where the clip will sit, so the video slot
    # visually pops even before the clip is composited under it.
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cx = SLOT_X + SLOT_W // 2
    cy = SLOT_Y + SLOT_H // 2
    # Draw three concentric translucent ellipses and blur the whole layer.
    for radius, alpha in [(720, 30), (520, 40), (360, 55)]:
        gd.ellipse(
            [cx - radius, cy - radius // 2, cx + radius, cy + radius // 2],
            fill=(ACCENT[0], ACCENT[1], ACCENT[2], alpha),
        )
    glow = glow.filter(ImageFilter.GaussianBlur(radius=90))
    canvas.alpha_composite(glow)


def _rounded_rect_mask(size: tuple[int, int], radius: int) -> Image.Image:
    """L-mode mask with rounded corners, for clipping other layers."""
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255
    )
    return mask


def _draw_slot_frame(canvas: Image.Image) -> None:
    """
    Draw a soft drop-shadow beneath the video slot and a thin accent-
    colored frame around it. The frame is drawn on the TEMPLATE layer,
    which sits above the source clip in the ffmpeg overlay, so the border
    reads clearly against the video's own colors.

    The shadow is intentionally offset a few pixels DOWN so the clip
    feels lifted off the canvas without being cartoonish.
    """
    # Shadow.
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rectangle(
        [SLOT_X - 6, SLOT_Y + 10, SLOT_X + SLOT_W + 6, SLOT_Y + SLOT_H + 30],
        fill=SLOT_SHADOW,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=18))
    canvas.alpha_composite(shadow)

    # Thin accent frame around the video slot (drawn on canvas directly
    # so it sits ABOVE the clip once the overlay is composited over it).
    frame = ImageDraw.Draw(canvas)
    frame.rectangle(
        [SLOT_X, SLOT_Y, SLOT_X + SLOT_W - 1, SLOT_Y + SLOT_H - 1],
        outline=SLOT_BORDER,
        width=2,
    )


# =============================================================================
# AVATAR (ringed profile picture)
# =============================================================================

def _draw_avatar(canvas: Image.Image, picture_path: str) -> None:
    """
    Draw a circular avatar with an accent ring + thin dark inner separator
    at (AVATAR_X, HEADER_Y). If `picture_path` is empty or unreadable, a
    graceful placeholder (charcoal disc with a subtle accent initial-less
    fallback) is drawn instead so the layout always looks intentional.
    """
    total_d = AVATAR_D + 2 * (AVATAR_RING_W + AVATAR_SEP_W)
    cx = AVATAR_X
    cy = HEADER_Y

    # Outer accent ring.
    ring_layer = Image.new("RGBA", (total_d, total_d), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring_layer)
    rd.ellipse([0, 0, total_d - 1, total_d - 1], fill=ACCENT)
    # Thin dark separator inside the ring — makes the ring readable no
    # matter what colours the avatar itself contains.
    inset = AVATAR_RING_W
    rd.ellipse(
        [inset, inset, total_d - 1 - inset, total_d - 1 - inset],
        fill=(0, 0, 0, 255),
    )
    canvas.alpha_composite(ring_layer, dest=(cx, cy))

    # Avatar image (or placeholder).
    avatar = None
    if picture_path and os.path.isfile(picture_path):
        try:
            src = Image.open(picture_path).convert("RGBA")
            # Center-crop to a square, then resize to AVATAR_D.
            w, h = src.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            src = src.crop((left, top, left + side, top + side)).resize(
                (AVATAR_D, AVATAR_D), Image.LANCZOS
            )
            avatar = src
        except Exception:
            # Any decode failure -> fall through to placeholder.
            avatar = None

    if avatar is None:
        # Placeholder: charcoal disc with a faint accent tint (never a
        # broken-image icon; the rest of the frame should still look
        # correct even if the user hasn't uploaded a profile picture).
        avatar = Image.new("RGBA", (AVATAR_D, AVATAR_D), (40, 44, 55, 255))
        ad = ImageDraw.Draw(avatar)
        ad.ellipse(
            [AVATAR_D // 4, AVATAR_D // 4,
             AVATAR_D - AVATAR_D // 4, AVATAR_D - AVATAR_D // 4],
            fill=(60, 66, 82, 255),
        )

    # Clip to circle.
    mask = Image.new("L", (AVATAR_D, AVATAR_D), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, AVATAR_D - 1, AVATAR_D - 1], fill=255)
    canvas.paste(
        avatar,
        (cx + AVATAR_RING_W + AVATAR_SEP_W, cy + AVATAR_RING_W + AVATAR_SEP_W),
        mask,
    )


# =============================================================================
# HEADER TEXT ROW (display name / @handle / category badge)
# =============================================================================

def _draw_header_text(
    canvas: Image.Image,
    display_name: str,
    username: str,
    badge_text: str,
) -> None:
    """
    Draw the display name (bold, bright), @username (muted), and a small
    uppercase category badge to the right of the avatar. All three fields
    tolerate empty strings — an unbranded run just shows the badge.
    """
    draw = ImageDraw.Draw(canvas)

    text_x = AVATAR_X + AVATAR_D + 2 * (AVATAR_RING_W + AVATAR_SEP_W) + 28

    name_font = _load_font(FONT_CANDIDATES_BOLD, 48)
    handle_font = _load_font(FONT_CANDIDATES_REGULAR, 34)

    # Fall back to a sensible visible label if the user hasn't set anything
    # yet — the header still needs SOME text so the layout balances.
    shown_name = display_name.strip() or username.strip() or "Your channel"
    shown_handle = f"@{username.strip()}" if username.strip() else ""

    # Display name.
    name_y = HEADER_Y + 20
    draw.text((text_x, name_y), shown_name, font=name_font, fill=TEXT_BRIGHT)

    # @username directly under, in muted grey.
    if shown_handle:
        _, name_h = _text_size(draw, shown_name, name_font)
        draw.text(
            (text_x, name_y + name_h + 6),
            shown_handle,
            font=handle_font,
            fill=TEXT_MUTED,
        )

    # Category badge — small pill on the right side.
    if badge_text:
        _draw_badge(canvas, badge_text)


def _draw_badge(canvas: Image.Image, text: str) -> None:
    """
    Uppercase, letter-spaced accent pill on the top right. This is the
    "category" element from the prototype — signals what kind of content
    the channel makes at a glance.
    """
    draw = ImageDraw.Draw(canvas)
    badge_font = _load_font(FONT_CANDIDATES_BOLD, 30)
    label = text.upper().strip()
    # Letter-space the badge label by inserting hair-spaces between chars,
    # so it reads as a designed pill rather than as regular text.
    spaced = " ".join(list(label))
    tw, th = _text_size(draw, spaced, badge_font)
    pad_x = 26
    pad_y = 12
    bw = tw + 2 * pad_x
    bh = th + 2 * pad_y
    bx = CANVAS_W - 60 - bw
    by = HEADER_Y + 36
    draw.rounded_rectangle(
        [bx, by, bx + bw, by + bh],
        radius=bh // 2,
        fill=ACCENT,
    )
    # Vertically center the glyphs inside the pill using a fresh bbox
    # measurement — Pillow's font ascender/descender offset means the
    # naive `(bh - th)//2` is a couple of px too low on most fonts.
    l, t, r, b = draw.textbbox((0, 0), spaced, font=badge_font)
    draw.text(
        (bx + (bw - (r - l)) // 2 - l,
         by + (bh - (b - t)) // 2 - t),
        spaced,
        font=badge_font,
        fill=TEXT_ON_ACCENT,
    )


# =============================================================================
# TITLE BLOCK (auto-fit)
# =============================================================================

def _wrap_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """
    Greedy word-wrap. Breaks a title into as few lines as possible that
    each fit inside `max_width` at the given font size. Words longer than
    max_width by themselves are kept on their own line rather than being
    hard-broken — a rare case for real titles, and preserving the word
    reads better than a mid-word cut.
    """
    words = text.split()
    lines: list[str] = []
    current = ""
    for w in words:
        trial = w if not current else f"{current} {w}"
        tw, _ = _text_size(draw, trial, font)
        if tw <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def _fit_title(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_height: int,
) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """
    Find the largest bold font size at which `text` word-wraps into
    lines that fit inside (max_width × max_height). Returns
    (font, lines, line_height).

    Search strategy: start at the ideal punchy display size (88 px) and
    step down by 4 px until the wrapped block fits. This means a short
    title stays big and bold; a long title shrinks just enough to fit
    without ever overflowing the reserved area. We cap the minimum size
    at 40 px — below that the title stops reading as a title.

    IMPORTANT: we do NOT truncate long titles with ellipses. The brief
    explicitly requires "correctly readable regardless of how long or
    short it is", so shrinking is the correct trade — a full readable
    3-line title is more useful than a truncated big one.
    """
    # Empty title -> nothing to render (title block is optional).
    if not text.strip():
        f = _load_font(FONT_CANDIDATES_BOLD, 40)
        return (f, [], 0)

    for size in range(88, 36, -4):
        f = _load_font(FONT_CANDIDATES_BOLD, size)
        lines = _wrap_to_width(draw, text.strip(), f, max_width)
        # Line height approximation: bbox height of a tall glyph + 8 px
        # extra leading so descender-heavy titles ("gpj") don't kiss.
        _, lh = _text_size(draw, "Ay", f)
        line_h = lh + 10
        block_h = line_h * len(lines)
        if block_h <= max_height:
            return (f, lines, line_h)

    # Absolute floor — even the smallest size didn't fit (a truly
    # extreme title). Emit at 40 px with whatever line count it takes;
    # the CTA row still renders below unaffected. This is a soft-fail
    # rather than a crash so a weird title never breaks the pipeline.
    f = _load_font(FONT_CANDIDATES_BOLD, 40)
    lines = _wrap_to_width(draw, text.strip(), f, max_width)
    _, lh = _text_size(draw, "Ay", f)
    return (f, lines, lh + 10)


def _draw_title_block(canvas: Image.Image, title: str) -> None:
    """
    Draw the title in the reserved region between the video slot and the
    CTA row, auto-fitted to size, with a short accent-colored underline
    bar above it that ties the block into the channel accent color.
    """
    if not title.strip():
        return

    draw = ImageDraw.Draw(canvas)
    max_w = CANVAS_W - 2 * TITLE_PAD_X
    max_h = TITLE_BOTTOM - TITLE_TOP - 30  # leave 30 px above accent bar

    font, lines, line_h = _fit_title(draw, title, max_w, max_h)
    if not lines:
        return

    block_h = line_h * len(lines)
    region_h = TITLE_BOTTOM - TITLE_TOP
    # Vertically center the wrapped block inside its region.
    y0 = TITLE_TOP + (region_h - block_h) // 2

    # Small accent bar above the first line — visual signature of the
    # brand accent, ties the title block to the ring/badge/button.
    bar_w = 80
    bar_h = 6
    bar_x = (CANVAS_W - bar_w) // 2
    bar_y = y0 - 26
    draw.rounded_rectangle(
        [bar_x, bar_y, bar_x + bar_w, bar_y + bar_h],
        radius=3,
        fill=ACCENT,
    )

    # Center each wrapped line horizontally.
    y = y0
    for line in lines:
        lw, _ = _text_size(draw, line, font)
        x = (CANVAS_W - lw) // 2
        # Subtle dark shadow behind the title so it stays readable if the
        # accent glow happens to sit directly behind it. Two-pass offset
        # blur costs almost nothing at this scale and lifts the type.
        draw.text((x + 2, y + 3), line, font=font, fill=(0, 0, 0, 180))
        draw.text((x, y), line, font=font, fill=TEXT_BRIGHT)
        y += line_h


# =============================================================================
# CTA ROW (follow button + like/comment/share prompt)
# =============================================================================

def _draw_cta_button(canvas: Image.Image) -> None:
    """
    Rounded accent-colored FOLLOW pill, centered horizontally, with a
    little play-triangle glyph and bold letter-spaced text. This is the
    primary call-to-action and is drawn in the strongest visual weight
    (solid accent fill) so the eye lands on it after reading the title.
    """
    draw = ImageDraw.Draw(canvas)
    bx = (CANVAS_W - CTA_BTN_W) // 2
    by = CTA_BTN_Y

    # Outer soft glow — lifts the button off the canvas without a hard
    # drop shadow (which would fight the video slot's own shadow).
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(
        [bx - 20, by - 20, bx + CTA_BTN_W + 20, by + CTA_BTN_H + 20],
        radius=CTA_BTN_RADIUS + 20,
        fill=ACCENT_SOFT,
    )
    glow = glow.filter(ImageFilter.GaussianBlur(radius=22))
    canvas.alpha_composite(glow)

    # Main button.
    draw.rounded_rectangle(
        [bx, by, bx + CTA_BTN_W, by + CTA_BTN_H],
        radius=CTA_BTN_RADIUS,
        fill=ACCENT,
    )

    # Play triangle + text, both centered as a single group.
    label_font = _load_font(FONT_CANDIDATES_BOLD, 50)
    label = "F O L L O W"
    lw, lh = _text_size(draw, label, label_font)
    tri_w = 26
    gap = 20
    group_w = tri_w + gap + lw
    gx = bx + (CTA_BTN_W - group_w) // 2
    gy = by + (CTA_BTN_H - lh) // 2

    # Triangle: a right-pointing play glyph, drawn as a filled polygon.
    tri_top = by + CTA_BTN_H // 2 - 22
    tri_bot = by + CTA_BTN_H // 2 + 22
    draw.polygon(
        [
            (gx, tri_top),
            (gx, tri_bot),
            (gx + tri_w, by + CTA_BTN_H // 2),
        ],
        fill=TEXT_ON_ACCENT,
    )

    # Text — bbox-anchored so the pill glyph metrics don't push it low.
    l, t, r, b = draw.textbbox((0, 0), label, font=label_font)
    draw.text(
        (gx + tri_w + gap - l,
         by + (CTA_BTN_H - (b - t)) // 2 - t),
        label,
        font=label_font,
        fill=TEXT_ON_ACCENT,
    )


def _draw_heart_icon(draw: ImageDraw.ImageDraw, x: float, y: float, size: float, fill) -> None:
    """Draw a vector heart icon using Pillow primitives."""
    half = size / 2
    draw.ellipse([x, y, x + half * 1.15, y + half * 1.15], fill=fill)
    draw.ellipse([x + half * 0.85, y, x + size, y + half * 1.15], fill=fill)
    draw.polygon([
        (x + size * 0.05, y + half * 0.6),
        (x + size * 0.95, y + half * 0.6),
        (x + half, y + size),
    ], fill=fill)


def _draw_bubble_icon(draw: ImageDraw.ImageDraw, x: float, y: float, size: float, fill) -> None:
    """Draw a vector speech bubble icon using Pillow primitives."""
    bw = size
    bh = size * 0.82
    by = y + (size - bh) / 2
    draw.rounded_rectangle([x, by, x + bw, by + bh * 0.85], radius=size * 0.25, fill=fill)
    tx = x + bw * 0.25
    ty = by + bh * 0.85
    draw.polygon([
        (tx, ty - 1),
        (tx + bw * 0.3, ty - 1),
        (tx + bw * 0.05, ty + bh * 0.25),
    ], fill=fill)


def _draw_share_icon(draw: ImageDraw.ImageDraw, x: float, y: float, size: float, fill) -> None:
    """Draw a vector share arrow icon (up-right arrow) using Pillow primitives."""
    ax1, ay1 = x + size * 0.2, y + size * 0.8
    ax2, ay2 = x + size * 0.8, y + size * 0.2
    thick = max(3, int(size * 0.12))
    draw.line([(ax1, ay1 + thick / 2), (ax2 - thick / 2, ay2)], fill=fill, width=thick)
    hw = size * 0.38
    draw.polygon([
        (ax2, ay2),
        (ax2 - hw, ay2),
        (ax2, ay2 + hw),
    ], fill=fill)


def _draw_engagement_prompt(canvas: Image.Image) -> None:
    """
    Secondary call-to-action row directly below the follow button —
    encourages likes / comments / shares. Drawn with clean vector icons
    (heart, speech bubble, share arrow) and text labels instead of
    unreliable font/emoji glyphs.
    """
    draw = ImageDraw.Draw(canvas)
    font = _load_font(FONT_CANDIDATES_BOLD, 34)

    labels = ["LIKE", "COMMENT", "SHARE"]
    icon_size = 32
    icon_text_gap = 12
    item_gap = 48

    text_metrics = []
    for label in labels:
        tw, th = _text_size(draw, label, font)
        text_metrics.append((tw, th))

    total_w = 0
    for i, label in enumerate(labels):
        tw, th = text_metrics[i]
        item_w = icon_size + icon_text_gap + tw
        total_w += item_w
        if i < len(labels) - 1:
            total_w += item_gap

    y = ENGAGEMENT_Y
    pad_x = 36
    pad_y = 14
    max_h = max(icon_size, max(th for _, th in text_metrics))
    start_x = (CANVAS_W - total_w) // 2

    bg = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(bg)
    bd.rounded_rectangle(
        [start_x - pad_x, y - pad_y, start_x + total_w + pad_x, y + max_h + pad_y],
        radius=(max_h + 2 * pad_y) // 2,
        fill=(255, 255, 255, 18),
    )
    canvas.alpha_composite(bg)

    cur_x = start_x
    for i, label in enumerate(labels):
        tw, th = text_metrics[i]
        icon_y = y + (max_h - icon_size) // 2
        text_y = y + (max_h - th) // 2 - 2

        if i == 0:
            _draw_heart_icon(draw, cur_x, icon_y, icon_size, TEXT_BRIGHT)
        elif i == 1:
            _draw_bubble_icon(draw, cur_x, icon_y, icon_size, TEXT_BRIGHT)
        elif i == 2:
            _draw_share_icon(draw, cur_x, icon_y, icon_size, TEXT_BRIGHT)

        text_x = cur_x + icon_size + icon_text_gap
        draw.text((text_x + 2, text_y + 2), label, font=font, fill=(0, 0, 0, 180))
        draw.text((text_x, text_y), label, font=font, fill=TEXT_BRIGHT)

        cur_x += icon_size + icon_text_gap + tw
        if i < len(labels) - 1:
            sep_x = cur_x + item_gap // 2
            sep_w, sep_h = _text_size(draw, "•", font)
            sep_y = y + (max_h - sep_h) // 2 - 2
            draw.text(
                (sep_x - sep_w // 2, sep_y),
                "•",
                font=font,
                fill=(TEXT_MUTED[0], TEXT_MUTED[1], TEXT_MUTED[2], 160),
            )
            cur_x += item_gap


# =============================================================================
# PUBLIC API
# =============================================================================

def build_template(
    branding: Branding,
    title: str,
    badge_text: str = BADGE_TEXT_DEFAULT,
) -> tuple[Image.Image, SlotGeometry]:
    """
    Render the full 1080×1200 branded chrome as an RGBA image and return
    (image, slot_geometry). The image has the video slot region CUT OUT
    (fully transparent inside the slot rectangle) so the compositor can
    letterbox the source clip UNDER the overlay and have it show through.

    Draw order:
        1. gradient background + accent glow
        2. drop shadow behind the video slot (glow-blurred, so it sits
           just below the slot rectangle before the cut-out is applied)
        3. header row (avatar + name + handle + badge)
        4. title block (auto-fit)
        5. CTA button + engagement prompt
        6. carve the video slot rectangle to transparent (this is what
           makes the source clip visible under the overlay)
        7. thin accent-colored frame around the slot, drawn LAST so it
           overlays the source clip cleanly

    The frame drawn in step 7 is only 2 px, so it doesn't obscure the
    video meaningfully but it does anchor the slot visually against the
    canvas.
    """
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))

    _draw_background(canvas)
    # The shadow layer is drawn BEFORE we carve the slot transparent, so
    # the shadow spills OUT past the slot edges (which is what we want).
    _draw_slot_frame_shadow_only(canvas)

    _draw_avatar(canvas, branding.profile_picture)
    _draw_header_text(canvas, branding.display_name, branding.username, badge_text)

    _draw_title_block(canvas, title)

    _draw_cta_button(canvas)
    _draw_engagement_prompt(canvas)

    # Carve the video slot rectangle to alpha=0 so the clip shows through
    # when this overlay is composited on top of the letterboxed video.
    # Using putalpha on a mask is the cleanest way to preserve every
    # other pixel exactly as drawn.
    _carve_slot(canvas)

    # Draw the thin accent frame LAST — it must sit on top of the clip
    # once the overlay is composited, so the source video doesn't cover
    # its own border. It's drawn just OUTSIDE the transparent hole (on
    # opaque pixels along the slot perimeter), 2 px wide, so it survives.
    _draw_slot_border(canvas)

    slot = SlotGeometry(
        canvas_w=CANVAS_W,
        canvas_h=CANVAS_H,
        x=SLOT_X,
        y=SLOT_Y,
        w=SLOT_W,
        h=SLOT_H,
    )
    return canvas, slot


def _draw_slot_frame_shadow_only(canvas: Image.Image) -> None:
    """Shadow-only variant of _draw_slot_frame (the border is drawn LAST)."""
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rectangle(
        [SLOT_X - 6, SLOT_Y + 10, SLOT_X + SLOT_W + 6, SLOT_Y + SLOT_H + 30],
        fill=SLOT_SHADOW,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=18))
    canvas.alpha_composite(shadow)


def _carve_slot(canvas: Image.Image) -> None:
    """
    Set alpha=0 for every pixel inside the video slot rectangle so the
    source clip shows through when the overlay is composited on top.
    Everything drawn OUTSIDE the rectangle (background, avatar, title,
    CTA) is preserved exactly.
    """
    alpha = canvas.split()[3]
    d = ImageDraw.Draw(alpha)
    d.rectangle(
        [SLOT_X, SLOT_Y, SLOT_X + SLOT_W - 1, SLOT_Y + SLOT_H - 1],
        fill=0,
    )
    canvas.putalpha(alpha)


def _draw_slot_border(canvas: Image.Image) -> None:
    """
    2 px accent-colored frame around the slot. Drawn AFTER _carve_slot so
    it sits on the opaque perimeter pixels and remains visible above the
    clip once composited.
    """
    d = ImageDraw.Draw(canvas)
    d.rectangle(
        [SLOT_X, SLOT_Y, SLOT_X + SLOT_W - 1, SLOT_Y + SLOT_H - 1],
        outline=SLOT_BORDER,
        width=2,
    )


# =============================================================================
# CLI
# =============================================================================

def _cli() -> None:
    """
    Command-line entry point. Primarily useful for ad-hoc rendering and
    for the integration tests in brand_scene.py — the real pipeline calls
    `build_template()` directly.

    Args:
        --out              Output PNG path (1080×1200, RGBA).
        --title            Job title string.
        --username         Channel @username (part 1 data).
        --display-name     Channel display name (part 1 data).
        --profile-picture  Local path to the channel avatar (part 1 data).
        --badge            Optional category badge text.
        --geometry-json    Optional path to also emit slot geometry JSON.
    """
    ap = argparse.ArgumentParser(description="Render the branded 9:16 chrome PNG.")
    ap.add_argument("--out", required=True, help="Output PNG path.")
    ap.add_argument("--title", default="", help="Job title.")
    ap.add_argument("--username", default="")
    ap.add_argument("--display-name", default="")
    ap.add_argument("--profile-picture", default="")
    ap.add_argument("--badge", default=BADGE_TEXT_DEFAULT)
    ap.add_argument(
        "--geometry-json",
        default="",
        help="If set, also write the slot geometry as JSON to this path.",
    )
    args = ap.parse_args()

    branding = Branding(
        username=args.username,
        display_name=args.display_name,
        profile_picture=args.profile_picture,
    )
    img, slot = build_template(branding, args.title, badge_text=args.badge)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    print(f"Wrote {out} ({img.size[0]}×{img.size[1]} RGBA).", flush=True)

    if args.geometry_json:
        gp = Path(args.geometry_json)
        gp.parent.mkdir(parents=True, exist_ok=True)
        gp.write_text(json.dumps(asdict(slot), indent=2))
        print(f"Wrote {gp} (slot geometry).", flush=True)


if __name__ == "__main__":
    _cli()
