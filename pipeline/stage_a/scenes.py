"""ClipForge Stage A — local vision-assist layer (zero AI-vision tokens).

Ported (semantics preserved) from the legacy trio into one module:

  * ``_legacy/scripts/scene_index.py``       — ffmpeg shot-boundary detection.
  * ``_legacy/scripts/build_key_moments.py`` — scene+transcript key-moment fusion.
  * ``_legacy/scripts/event_frames.py``      — dense composites for flagged beats.
  * baseline 6-second composite extraction   — the ffmpeg tile pass that used
    to live inline in the legacy ``stage-a.yml`` shell.

Everything here is pure local CPU work (ffmpeg + JSON stitching); no model
downloads, no network, no AI-vision tokens. Outputs per ARCHITECTURE.md §7.2:

  * ``scene_index.json``    — shot boundaries.
  * ``key_moments.json``    — ranked high-signal shortlist (with visual tails).
  * ``screenshots/``        — ``frame_NNNNNN.jpg`` baseline + ``event_*.jpg``.

CLI subcommands mirror the legacy scripts so the workflow can drive them
independently::

    python -m pipeline.stage_a.scenes shots <video> <duration> <out.json> [--threshold 0.35]
    python -m pipeline.stage_a.scenes moments <scene.json> <transcript.json> <out.json> [--max-moments 60]
    python -m pipeline.stage_a.scenes events <video> <key_moments.json> <out_dir> [options]
    python -m pipeline.stage_a.scenes baseline <video> <out_dir> [--window-seconds 6] [--panel-width 640]
    python -m pipeline.stage_a.scenes all <video> <transcript.json> <duration> <work_dir> [options]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


# --------------------------------------------------------------------------- #
# Shot boundaries (from scene_index.py)                                        #
# --------------------------------------------------------------------------- #

SCENE_LINE_RE = re.compile(r"pts_time:(?P<t>[0-9]+(?:\.[0-9]+)?)")


def detect_shots(video_path: str, threshold: float) -> list[float]:
    """Shot-change timestamps from ffmpeg's built-in scene-change score.

    Runs ``select='gt(scene,T)',showinfo`` and parses ``pts_time:`` fields
    from ffmpeg's stderr. Sorted + deduplicated (ffmpeg sometimes
    double-reports on frame boundaries).
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i", video_path,
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null",
        "-",
    ]
    print(f"$ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr[-2000:], file=sys.stderr)
        raise RuntimeError(f"ffmpeg scene-detect failed (exit {proc.returncode})")

    times: list[float] = []
    for line in proc.stderr.splitlines():
        m = SCENE_LINE_RE.search(line)
        if m:
            try:
                times.append(float(m.group("t")))
            except ValueError:
                pass

    uniq: list[float] = []
    for t in sorted(times):
        if not uniq or (t - uniq[-1]) > 0.05:
            uniq.append(t)
    return uniq


def build_shot_index(cut_times: list[float], total_duration: float) -> list[dict]:
    """Turn sorted cut timestamps into shot dicts covering [0, total_duration].

    Sub-quarter-second flickers (flashes, subtitle pop-ins) are folded into
    the previous shot. Each shot's ``keyframe_seconds`` is its midpoint — a
    stable representative-frame anchor for the downstream vision agent.
    """
    boundaries = [0.0] + [t for t in cut_times if 0.0 < t < total_duration]
    boundaries.append(float(total_duration))

    shots: list[dict] = []
    for i in range(len(boundaries) - 1):
        s = round(boundaries[i], 3)
        e = round(boundaries[i + 1], 3)
        if e - s < 0.25:
            if shots:
                shots[-1]["end_seconds"] = e
                shots[-1]["keyframe_seconds"] = round(
                    (shots[-1]["start_seconds"] + e) / 2.0, 3
                )
            continue
        shots.append(
            {
                "shot_id": len(shots) + 1,
                "start_seconds": s,
                "end_seconds": e,
                "keyframe_seconds": round((s + e) / 2.0, 3),
                "cause": "video_start" if i == 0 else "cut",
            }
        )
    return shots


def write_scene_index(
    video_path: str,
    duration_seconds: float,
    output_json: str,
    *,
    threshold: float = 0.35,
) -> dict:
    """Detect shots in the (compressed) analysis copy and write scene_index.json."""
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"input not found: {video_path}")

    cut_times = detect_shots(video_path, threshold)
    print(f"Detected {len(cut_times)} raw scene-change candidate(s)", flush=True)

    shots = build_shot_index(cut_times, duration_seconds)
    print(f"Consolidated into {len(shots)} shot(s)", flush=True)

    payload = {
        "video_duration_seconds": float(duration_seconds),
        "threshold": threshold,
        "shot_count": len(shots),
        "shots": shots,
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_json)) or ".", exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Wrote scene index to {output_json}", flush=True)
    return payload


# --------------------------------------------------------------------------- #
# Key-moment shortlist (from build_key_moments.py)                             #
# --------------------------------------------------------------------------- #

# Words that tend to sit on top of a real narrative beat in short-form
# commentary content. Small and language-neutral-ish (matches on lowercased
# ASCII stems); a HINT for the agent, not a hard filter.
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
    """Cheap heuristic emotional-word density in [0, 1] (~5 stem hits = strong)."""
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
    return min(1.0, hits / 5.0)


def overlap_seconds(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def transcript_between(segments: list[dict], t_start: float, t_end: float) -> tuple[str, float]:
    """Return (concatenated_text, dialogue_density) for [t_start, t_end]."""
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
    """Seconds to extend a moment's ``end_seconds`` PAST the raw shot boundary.

    Short-form narrative beats routinely resolve ACROSS the cut (reaction
    shot, cutaway, aftermath). Padding into the next shot(s) — bounded by
    ``max_tail``, half the next shot's duration, and the video end — keeps
    the described payoff inside the candidate window. Preserved exactly from
    the legacy build_key_moments.py policy.
    """
    if shot_index < 0 or shot_index >= len(shots):
        return 0.0
    shot = shots[shot_index]
    shot_end = float(shot["end_seconds"])
    room_to_video_end = max(0.0, duration - shot_end)
    if room_to_video_end <= 0.0:
        return 0.0

    if shot_index + 1 < len(shots):
        next_shot = shots[shot_index + 1]
        next_duration = max(
            0.0, float(next_shot["end_seconds"]) - float(next_shot["start_seconds"])
        )
        cap_from_next = next_duration / 2.0
    else:
        cap_from_next = room_to_video_end

    tail = min(max_tail, cap_from_next, room_to_video_end)
    if tail < min_tail and room_to_video_end >= min_tail and cap_from_next >= min_tail:
        tail = min_tail
    return round(tail, 3)


def write_key_moments(
    scene_index_json: str,
    transcript_json: str,
    output_json: str,
    *,
    max_moments: int = 60,
    visual_tail_min_seconds: float = 1.5,
    visual_tail_max_seconds: float = 3.5,
) -> dict:
    """Fuse scene_index.json + transcript.json into key_moments.json.

    Character identification is deliberately NOT done here — the legacy local
    face-clustering step introduced identity errors that poisoned downstream
    selection; the vision agent identifies characters directly instead.
    """
    for p in (scene_index_json, transcript_json):
        if not os.path.exists(p):
            raise FileNotFoundError(f"input not found: {p}")

    with open(scene_index_json, "r", encoding="utf-8") as f:
        scene_data = json.load(f)
    with open(transcript_json, "r", encoding="utf-8") as f:
        tx_data = json.load(f)

    duration = float(scene_data.get("video_duration_seconds", 0.0))
    shots = scene_data.get("shots", [])
    tx_segments = tx_data.get("segments", [])

    candidates: list[dict] = []
    for shot_idx, shot in enumerate(shots):
        sid = shot["shot_id"]
        s = float(shot["start_seconds"])
        shot_end = float(shot["end_seconds"])

        transcript_excerpt, dialogue_density = transcript_between(tx_segments, s, shot_end)
        emotional = score_emotional(transcript_excerpt)

        priority = 0.0
        why: list[str] = []
        priority += 0.55 * emotional
        if emotional >= 0.4:
            why.append("high emotional-word density in dialogue")
        priority += 0.25 * dialogue_density
        priority += 0.05
        why.append(f"shot boundary at {s:.1f}s ({shot.get('cause', 'cut')})")

        tail = compute_visual_tail(
            shots,
            shot_idx,
            duration,
            min_tail=max(0.0, visual_tail_min_seconds),
            max_tail=max(0.0, visual_tail_max_seconds),
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

    candidates.sort(key=lambda m: m["signals"]["priority"], reverse=True)
    top = candidates[:max_moments]
    top.sort(key=lambda m: m["start_seconds"])
    for i, m in enumerate(top, start=1):
        m["moment_id"] = i

    payload = {
        "video_duration_seconds": duration,
        "moment_count": len(top),
        "visual_tail_policy": {
            "min_seconds": visual_tail_min_seconds,
            "max_seconds": visual_tail_max_seconds,
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

    os.makedirs(os.path.dirname(os.path.abspath(output_json)) or ".", exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(
        f"Wrote key_moments index: {len(top)} moment(s) from "
        f"{len(candidates)} shot(s) -> {output_json}",
        flush=True,
    )
    return payload


# --------------------------------------------------------------------------- #
# Composite screenshots (baseline cadence + dense event composites)            #
# --------------------------------------------------------------------------- #

def emit_event_composite(
    video_path: str,
    center_seconds: float,
    dst_path: str,
    window_seconds: float,
    samples: int,
    panel_width: int,
) -> bool:
    """Tile ``samples`` frames across a window centered on ``center_seconds``
    into one 3x2 JPEG. Returns True on success."""
    half = window_seconds / 2.0
    start = max(0.0, center_seconds - half)
    fps = samples / window_seconds

    tile_layout = "3x2"
    vf = (
        f"fps={fps:.6f},"
        f"scale='min({panel_width},iw)':'-2',"
        f"tile={tile_layout}:padding=2:margin=2:color=black"
    )
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-t", f"{window_seconds:.3f}",
        "-i", video_path,
        "-vf", vf,
        "-frames:v", "1",
        "-qscale:v", "5",
        dst_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0
    except subprocess.CalledProcessError as e:
        print(
            f"  event-composite ffmpeg failed @ {center_seconds:.2f}s: "
            f"{e.stderr.decode(errors='replace')[-400:]}",
            file=sys.stderr,
        )
        return False


def emit_event_composites(
    compressed_video: str,
    key_moments_json: str,
    screenshots_out_dir: str,
    *,
    window_seconds: float = 4.0,
    samples: int = 6,
    panel_width: int = 640,
    max_events: int = 30,
    min_priority: float = 0.35,
    tail_composites: bool = True,
) -> int:
    """Emit dense 4-second composites for high-signal key moments only.

    Returns the number of NEW event composites written. A missing or empty
    key_moments file is not an error (nothing to do). Composite centers are
    deduplicated (and ~0.5s-collapsed) across adjacent moments.
    """
    if not os.path.exists(compressed_video):
        raise FileNotFoundError(f"input video not found: {compressed_video}")
    if not os.path.exists(key_moments_json):
        print(
            f"No key_moments file at {key_moments_json}; "
            "no event composites will be emitted.",
            flush=True,
        )
        return 0

    with open(key_moments_json, "r", encoding="utf-8") as f:
        km = json.load(f)

    moments = km.get("moments", [])
    if not moments:
        print("key_moments has no moments; nothing to do.", flush=True)
        return 0

    high = [
        m for m in moments
        if m.get("signals", {}).get("is_shot_boundary")
        or m.get("signals", {}).get("priority", 0.0) >= min_priority
    ]
    high.sort(key=lambda m: m["signals"].get("priority", 0.0), reverse=True)
    high = high[:max_events]
    high.sort(key=lambda m: m["start_seconds"])

    os.makedirs(screenshots_out_dir, exist_ok=True)

    emitted_centers_ms: set[int] = set()

    def _emit_one(center: float, label: str) -> bool:
        if center < 0.0:
            return False
        center_ms = int(round(center * 1000))
        if center_ms in emitted_centers_ms:
            return False
        for existing in emitted_centers_ms:
            if abs(existing - center_ms) < 500:
                return False

        fname = f"event_{center_ms:09d}.jpg"
        dst = os.path.join(screenshots_out_dir, fname)
        ok = emit_event_composite(
            compressed_video,
            center,
            dst,
            window_seconds,
            samples,
            panel_width,
        )
        if ok:
            emitted_centers_ms.add(center_ms)
            print(f"  event composite [{label}] @ {center:.2f}s -> {fname}", flush=True)
            return True
        return False

    written = 0
    for m in high:
        start_c = float(m["start_seconds"])
        if _emit_one(start_c, "start"):
            written += 1

        if not tail_composites:
            continue

        end_c = m.get("end_seconds")
        if end_c is None:
            end_c = m.get("shot_end_seconds", start_c + 3.0)
        end_c = float(end_c)
        if end_c - start_c < window_seconds / 2.0:
            continue
        if _emit_one(end_c, "tail"):
            written += 1

    print(f"Wrote {written} event composite(s) to {screenshots_out_dir}", flush=True)
    return written


def extract_baseline_composites(
    compressed_video: str,
    screenshots_out_dir: str,
    *,
    window_seconds: int = 6,
    panel_width: int = 640,
) -> int:
    """Baseline cadence: one 3x2 composite JPEG per ``window_seconds`` window.

    Preserved from the legacy stage-a.yml inline step: ``fps=1`` gives one
    frame per source second; ``tile=3x2`` packs every 6 consecutive seconds
    into one JPEG; output image N covers source seconds
    [N*window, N*window+window). Files are renamed to
    ``frame_<window_start_seconds>.jpg`` (zero-padded to 6 digits).
    Returns the number of baseline composites written.
    """
    if not os.path.exists(compressed_video):
        raise FileNotFoundError(f"input video not found: {compressed_video}")

    out_dir = Path(screenshots_out_dir)
    tmp_dir = out_dir.parent / (out_dir.name + "_tmp")
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-i", compressed_video,
        "-vf", f"fps=1,scale='min({panel_width},iw)':'-2',tile=3x2:padding=2:margin=2:color=black",
        "-vsync", "vfr",
        "-qscale:v", "5",
        "-start_number", "0",
        str(tmp_dir / "grid_%05d.jpg"),
    ]
    print(f"$ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr[-2000:], file=sys.stderr)
        raise RuntimeError(f"ffmpeg baseline composite extraction failed (exit {proc.returncode})")

    count = 0
    for f in sorted(tmp_dir.glob("grid_*.jpg")):
        seq = f.stem.split("_", 1)[1]
        seq_num = int(seq)
        start_sec = seq_num * window_seconds
        dst = out_dir / f"frame_{start_sec:06d}.jpg"
        os.replace(f, dst)
        count += 1
    try:
        tmp_dir.rmdir()
    except OSError:
        pass

    print(
        f"Extracted {count} baseline composite screenshots "
        f"({window_seconds}-second windows, 1 frame per second, 3x2 grid)",
        flush=True,
    )
    return count


# --------------------------------------------------------------------------- #
# Convenience: run the whole local vision-assist stack                         #
# --------------------------------------------------------------------------- #

def run_all(
    compressed_video: str,
    transcript_json: str,
    duration_seconds: float,
    work_dir: str,
    *,
    threshold: float = 0.35,
    max_moments: int = 60,
    window_seconds: int = 6,
    enable_event_composites: bool = True,
) -> dict:
    """Run baseline composites → shot index → key moments → event composites.

    Returns a stats dict (counts + paths) the orchestrator/workflow uses to
    fill the analysis prompt and manifest.
    """
    work = Path(work_dir)
    screenshots_dir = work / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    baseline_count = extract_baseline_composites(
        compressed_video, str(screenshots_dir), window_seconds=window_seconds)

    scene_payload = write_scene_index(
        compressed_video, duration_seconds, str(work / "scene_index.json"),
        threshold=threshold)

    moments_payload = write_key_moments(
        str(work / "scene_index.json"), transcript_json, str(work / "key_moments.json"),
        max_moments=max_moments)

    event_count = 0
    if enable_event_composites:
        event_count = emit_event_composites(
            compressed_video, str(work / "key_moments.json"), str(screenshots_dir))

    return {
        "screenshot_count": baseline_count,
        "screenshot_window_seconds": window_seconds,
        "shot_count": scene_payload.get("shot_count", 0),
        "key_moment_count": moments_payload.get("moment_count", 0),
        "event_frame_count": event_count,
        "screenshots_dir": str(screenshots_dir),
        "scene_index_json": str(work / "scene_index.json"),
        "key_moments_json": str(work / "key_moments.json"),
    }


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(description="ClipForge Stage A local vision-assist")
    sub = ap.add_subparsers(dest="command", required=True)

    p_shots = sub.add_parser("shots", help="detect shot boundaries -> scene_index.json")
    p_shots.add_argument("video_path")
    p_shots.add_argument("duration_seconds", type=float)
    p_shots.add_argument("output_json")
    p_shots.add_argument("--threshold", type=float, default=0.35)

    p_moments = sub.add_parser("moments", help="build key_moments.json shortlist")
    p_moments.add_argument("scene_index_json")
    p_moments.add_argument("transcript_json")
    p_moments.add_argument("output_json")
    p_moments.add_argument("--max-moments", type=int, default=60)
    p_moments.add_argument("--visual-tail-min-seconds", type=float, default=1.5)
    p_moments.add_argument("--visual-tail-max-seconds", type=float, default=3.5)

    p_events = sub.add_parser("events", help="emit dense event composites")
    p_events.add_argument("compressed_video")
    p_events.add_argument("key_moments_json")
    p_events.add_argument("screenshots_out_dir")
    p_events.add_argument("--window-seconds", type=float, default=4.0)
    p_events.add_argument("--samples", type=int, default=6)
    p_events.add_argument("--panel-width", type=int, default=640)
    p_events.add_argument("--max-events", type=int, default=30)
    p_events.add_argument("--min-priority", type=float, default=0.35)
    p_events.add_argument("--tail-composites", default="true")

    p_base = sub.add_parser("baseline", help="extract baseline 6s composites")
    p_base.add_argument("compressed_video")
    p_base.add_argument("screenshots_out_dir")
    p_base.add_argument("--window-seconds", type=int, default=6)
    p_base.add_argument("--panel-width", type=int, default=640)

    p_all = sub.add_parser("all", help="run the whole local vision-assist stack")
    p_all.add_argument("compressed_video")
    p_all.add_argument("transcript_json")
    p_all.add_argument("duration_seconds", type=float)
    p_all.add_argument("work_dir")
    p_all.add_argument("--threshold", type=float, default=0.35)
    p_all.add_argument("--max-moments", type=int, default=60)
    p_all.add_argument("--window-seconds", type=int, default=6)
    p_all.add_argument("--no-event-composites", action="store_true")

    args = ap.parse_args()

    try:
        if args.command == "shots":
            write_scene_index(args.video_path, args.duration_seconds, args.output_json,
                              threshold=args.threshold)
        elif args.command == "moments":
            write_key_moments(args.scene_index_json, args.transcript_json, args.output_json,
                              max_moments=args.max_moments,
                              visual_tail_min_seconds=args.visual_tail_min_seconds,
                              visual_tail_max_seconds=args.visual_tail_max_seconds)
        elif args.command == "events":
            emit_event_composites(
                args.compressed_video, args.key_moments_json, args.screenshots_out_dir,
                window_seconds=args.window_seconds, samples=args.samples,
                panel_width=args.panel_width, max_events=args.max_events,
                min_priority=args.min_priority,
                tail_composites=str(args.tail_composites).strip().lower() not in ("false", "0", "no", "off"))
        elif args.command == "baseline":
            extract_baseline_composites(args.compressed_video, args.screenshots_out_dir,
                                        window_seconds=args.window_seconds,
                                        panel_width=args.panel_width)
        else:  # all
            stats = run_all(
                args.compressed_video, args.transcript_json, args.duration_seconds,
                args.work_dir, threshold=args.threshold, max_moments=args.max_moments,
                window_seconds=args.window_seconds,
                enable_event_composites=not args.no_event_composites)
            print(json.dumps(stats, ensure_ascii=False, indent=2))
    except (RuntimeError, FileNotFoundError, OSError) as exc:
        print(f"scenes error: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()


__all__ = [
    "detect_shots", "build_shot_index", "write_scene_index",
    "score_emotional", "overlap_seconds", "transcript_between",
    "compute_visual_tail", "write_key_moments",
    "emit_event_composite", "emit_event_composites",
    "extract_baseline_composites", "run_all",
    "EMOTIONAL_STEMS",
]
