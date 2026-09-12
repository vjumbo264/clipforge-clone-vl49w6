#!/usr/bin/env python3
"""
build_key_moments.py — Fuse scene_index.json + transcript.json into a
compact `key_moments.json` that tells the downstream vision agent
*which* moments are worth investigating.

Why this exists
---------------
The agent currently has to reconstruct the story of the video from
scratch every time. It sees:
  - A dialogue-only transcript with no speaker labels.
  - A wall of 6-second composite screenshots with no "important vs
    filler" signal.
So it wastes vision-token budget on filler footage and still misses the
turning points because none of them are marked.

`key_moments.json` is the shortlist. It flags:
  - Shot boundaries (from scene_index).
  - Transcript stretches with a high density of emotional/decision
    language.
Each moment carries a small `signals` block explaining WHY it was
flagged, so the agent can decide at a glance whether to keep it as a
cut candidate before it opens any screenshots.

Character identification is deliberately NOT done here. Locally
clustering faces was doing more harm than good — it introduced
identity errors (splits, merges, missed brief appearances) that then
poisoned the "introduces_person" / "cast_change" signals and locked
the downstream vision agent into wrong labels. The AI vision agent is
now trusted to identify characters directly from the transcript and
screenshots.

Zero AI-vision-token cost. This is pure JSON stitching.

Output shape (`key_moments.json`)
---------------------------------
{
  "video_duration_seconds": 1416,
  "moment_count": 42,
  "moments": [
    {
      "moment_id": 1,
      "start_seconds": 42.5,
      "shot_end_seconds": 61.2,
      "end_seconds": 63.7,
      "visual_tail_seconds": 2.5,
      "shot_ids": [3],
      "transcript_excerpt": "...",
      "signals": {
        "is_shot_boundary": true,
        "emotional_score": 0.42,
        "dialogue_density": 0.71,
        "priority": 0.51
      },
      "why": [
        "shot boundary at 42.5s (cut)",
        "high emotional-word density in dialogue",
        "end_seconds extended 2.5s past shot boundary so the beat's",
        "visual payoff (reaction / cutaway / result on screen) is",
        "actually contained inside the moment window"
      ]
    },
    ...
  ]
}

Usage
-----
    python build_key_moments.py <scene_index_json> <transcript_json>
                                <output_json>
                                [--max-moments 60]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys


# Words that tend to sit on top of a real narrative beat in short-form
# commentary content. Kept small and language-neutral-ish (matches on
# lowercased ASCII stems); it's a HINT for the agent, not a hard filter,
# so a few missed matches on non-English content don't hurt.
EMOTIONAL_STEMS = [
    "kill", "die", "dead", "death", "murder", "blood",
    "love", "hate", "fear", "angry", "furious", "scared", "terrified",
    "friend", "enemy", "betray", "trust", "lie", "truth", "secret",
    "save", "protect", "escape", "trapped", "alone", "help",
    "cry", "scream", "shout", "whisper", "silent",
    "reveal", "confess", "admit", "promise",
    "win", "lose", "defeat", "surrender", "beg",
    "please", "sorry", "never", "always", "forever",
    "why", "how could", "what if",
]

TOKEN_RE = re.compile(r"[A-Za-z']+")


def score_emotional(text: str) -> float:
    """
    Very cheap heuristic emotional-word density in [0, 1]. Counts hits
    against EMOTIONAL_STEMS and normalizes by token count. It's not
    trying to be a sentiment model — just a "does this stretch of
    dialogue sound like a beat, or like exposition?" hint.
    """
    if not text:
        return 0.0
    text_l = text.lower()
    tokens = TOKEN_RE.findall(text_l)
    if not tokens:
        return 0.0
    hits = 0
    for stem in EMOTIONAL_STEMS:
        if stem in text_l:
            hits += 1
    # Squash: ~5 stem hits = strong beat.
    return min(1.0, hits / 5.0)


def overlap_seconds(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def transcript_between(segments: list[dict], t_start: float, t_end: float) -> tuple[str, float]:
    """
    Return (concatenated_text, dialogue_density). Dialogue density is
    the fraction of [t_start, t_end] covered by transcript segments.
    """
    if t_end <= t_start:
        return "", 0.0
    pieces: list[str] = []
    covered = 0.0
    for seg in segments:
        s = float(seg.get("start", 0.0))
        e = float(seg.get("end", 0.0))
        ov = overlap_seconds(t_start, t_end, s, e)
        if ov > 0:
            txt = (seg.get("text") or "").strip()
            if txt:
                pieces.append(txt)
            covered += ov
    dur = t_end - t_start
    return " ".join(pieces), min(1.0, covered / dur) if dur > 0 else 0.0


def compute_visual_tail(
    shots: list[dict],
    shot_index: int,
    duration: float,
    min_tail: float = 1.5,
    max_tail: float = 3.5,
) -> float:
    """
    Return how many seconds to extend a moment's `end_seconds` PAST the
    raw shot boundary, so the visual payoff of the described action is
    actually contained inside the moment window.

    Why any tail at all
    -------------------
    The raw shot boundary (`shot.end_seconds`) is the frame BEFORE the
    camera cuts away. On short-form narrative content the beat that a
    viewer would describe as "the moment" almost always resolves ACROSS
    the cut — the reaction shot, the cutaway to the object that was
    just referenced, the aftermath of the punch, the flash of
    someone's eye as they close it. If we hand the downstream vision
    agent a moment window that ends at the raw shot boundary, and the
    agent then anchors its cut's `end_seconds` to the window it was
    given, the resulting scene_XX.mp4 lands right before the described
    action's visual payoff is on screen.

    Padding by a small, bounded amount into the NEXT shot(s) gives the
    agent visual coverage of the payoff. It never invents new content —
    the next shot is real footage that already exists in the source; we
    just include it in the candidate window so the agent can see it and
    decide whether to keep it.

    Bounds
    ------
    - Never longer than `max_tail` seconds (default 3.5s) — beyond that
      we would start swallowing the next shot's own moment.
    - Never longer than half of the NEXT shot's duration, for the same
      reason: don't cannibalize the next moment's window.
    - Never past `duration` (end of the video).
    - Always at least `min_tail` seconds when there is room, so even a
      tiny follow-up shot gets a meaningful chunk of tail.
    """
    if shot_index < 0 or shot_index >= len(shots):
        return 0.0
    shot = shots[shot_index]
    shot_end = float(shot["end_seconds"])
    # Room until the end of the video.
    room_to_video_end = max(0.0, duration - shot_end)
    if room_to_video_end <= 0.0:
        return 0.0

    if shot_index + 1 < len(shots):
        next_shot = shots[shot_index + 1]
        next_duration = max(
            0.0, float(next_shot["end_seconds"]) - float(next_shot["start_seconds"])
        )
        # Half the next shot's duration, so we never eat more than half
        # of the following beat's own visual real estate.
        cap_from_next = next_duration / 2.0
    else:
        # This is the last shot: only the video-end cap applies.
        cap_from_next = room_to_video_end

    tail = min(max_tail, cap_from_next, room_to_video_end)
    # Floor tail at min_tail when there is at least that much room; if
    # the next shot is genuinely tiny (<3s), we accept the small cap.
    if tail < min_tail and room_to_video_end >= min_tail and cap_from_next >= min_tail:
        tail = min_tail
    return round(tail, 3)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("scene_index_json")
    ap.add_argument("transcript_json")
    ap.add_argument("output_json")
    ap.add_argument(
        "--max-moments",
        type=int,
        default=60,
        help="Hard cap on the number of moments emitted. The agent should "
             "still open its own screenshots; this is a shortlist, not a "
             "final cut list.",
    )
    ap.add_argument(
        "--visual-tail-min-seconds",
        type=float,
        default=1.5,
        help="Minimum seconds to extend end_seconds past the raw shot "
             "boundary so the described beat's visual payoff (reaction / "
             "cutaway / result) sits inside the moment window, not one "
             "frame past it. See compute_visual_tail() docstring.",
    )
    ap.add_argument(
        "--visual-tail-max-seconds",
        type=float,
        default=3.5,
        help="Hard cap on the visual-tail extension. Larger values would "
             "start eating the following moment's own window.",
    )
    args = ap.parse_args()

    for p in (args.scene_index_json, args.transcript_json):
        if not os.path.exists(p):
            print(f"Input not found: {p}", file=sys.stderr)
            sys.exit(2)

    with open(args.scene_index_json, "r", encoding="utf-8") as f:
        scene_data = json.load(f)
    with open(args.transcript_json, "r", encoding="utf-8") as f:
        tx_data = json.load(f)

    duration = float(scene_data.get("video_duration_seconds", 0.0))
    shots = scene_data.get("shots", [])
    tx_segments = tx_data.get("segments", [])

    # Score every shot as a candidate moment.
    #
    # A moment's `end_seconds` is NOT the raw shot boundary — it is the
    # shot boundary plus a small `visual_tail_seconds` extension into the
    # next shot. That extension is what stops the downstream vision agent
    # from picking cut end_seconds that land one frame before the described
    # action's visual payoff (the reaction shot, the cutaway, the result
    # of the just-executed action). See compute_visual_tail() above.
    candidates: list[dict] = []
    for shot_idx, shot in enumerate(shots):
        sid = shot["shot_id"]
        s = float(shot["start_seconds"])
        shot_end = float(shot["end_seconds"])

        # Score against the RAW shot window (the emotional/dialogue signal
        # belongs to what was said inside the current shot, not to the
        # tail we're about to bolt onto its end).
        transcript_excerpt, dialogue_density = transcript_between(tx_segments, s, shot_end)
        emotional = score_emotional(transcript_excerpt)

        # Composite priority. Weights tuned so:
        #   - a shot whose dialogue is emotionally loaded is kept,
        #   - a shot with strong dialogue coverage nudges up,
        #   - a shot with neither is dropped even if the camera cut.
        priority = 0.0
        why: list[str] = []
        priority += 0.55 * emotional
        if emotional >= 0.4:
            why.append("high emotional-word density in dialogue")
        priority += 0.25 * dialogue_density
        # Shot cuts alone are cheap — the boundary itself is always noted,
        # but on its own it barely nudges priority.
        priority += 0.05
        why.append(f"shot boundary at {s:.1f}s ({shot.get('cause', 'cut')})")

        tail = compute_visual_tail(
            shots,
            shot_idx,
            duration,
            min_tail=max(0.0, args.visual_tail_min_seconds),
            max_tail=max(0.0, args.visual_tail_max_seconds),
        )
        moment_end = round(min(duration, shot_end + tail), 3)
        if tail > 0.0:
            why.append(
                f"end_seconds extended +{tail:.2f}s past the shot boundary so "
                f"the beat's visual payoff (reaction / cutaway / result on "
                f"screen) is inside the moment window, not one frame past it"
            )

        candidates.append(
            {
                "start_seconds": s,
                # `shot_end_seconds` is preserved as the raw shot boundary
                # so downstream code (and the agent) can tell the two apart.
                "shot_end_seconds": round(shot_end, 3),
                "end_seconds": moment_end,
                "visual_tail_seconds": tail,
                "shot_ids": [sid],
                "transcript_excerpt": transcript_excerpt[:500],
                "signals": {
                    "is_shot_boundary": True,
                    "emotional_score": round(emotional, 3),
                    "dialogue_density": round(dialogue_density, 3),
                    "priority": round(min(1.0, priority), 3),
                },
                "why": why,
            }
        )

    # Rank by priority and keep the top N. Then re-sort chronologically
    # so the agent reads them in playback order.
    candidates.sort(key=lambda m: m["signals"]["priority"], reverse=True)
    top = candidates[: args.max_moments]
    top.sort(key=lambda m: m["start_seconds"])
    for i, m in enumerate(top, start=1):
        m["moment_id"] = i

    payload = {
        "video_duration_seconds": duration,
        "moment_count": len(top),
        "visual_tail_policy": {
            "min_seconds": args.visual_tail_min_seconds,
            "max_seconds": args.visual_tail_max_seconds,
            "description": (
                "Each moment's `end_seconds` is deliberately extended past "
                "the raw shot boundary (`shot_end_seconds`) by "
                "`visual_tail_seconds`, so the on-screen payoff of the "
                "described beat (reaction shot / cutaway / result of the "
                "action) sits INSIDE the moment window. Do not treat "
                "`shot_end_seconds` as a cut-end candidate — use "
                "`end_seconds` (or push further if the visible action "
                "clearly runs past even that)."
            ),
        },
        "notes": (
            "This file is a SHORTLIST of high-signal moments produced "
            "locally at zero AI-vision cost. It is a hint, not a mandate: "
            "the agent should still verify each moment by opening the "
            "corresponding screenshots. Every moment carries a `why` "
            "field explaining what triggered its inclusion."
        ),
        "moments": top,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.output_json)) or ".", exist_ok=True)
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(
        f"Wrote key_moments index: {len(top)} moment(s) from "
        f"{len(candidates)} shot(s) -> {args.output_json}",
        flush=True,
    )


if __name__ == "__main__":
    main()
