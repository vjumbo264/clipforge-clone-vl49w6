#!/usr/bin/env python3
"""
Generate the 00_READ_THIS_FIRST.txt file that Stage A ships alongside the
transcript + screenshots + local vision-assist indexes. This file
instructs the downstream AI agent how to select cuts and what production.json
shape to return.

The duration, frame filename convention, and window size are substituted
from the actual Stage A run.

The optional --focus / --focus-env argument narrows the entire analysis
to one specific scene/moment/plot thread in the source video. When
provided, a strong "FOCUS DIRECTIVE" block is prepended to the top of
the prompt and the working-order steps are rewritten to explicitly
constrain the agent to that focus. When absent, the prompt behaves
exactly as before (consider the whole video).

Usage:
    python generate_analysis_prompt.py <duration_seconds> <total_frames>
                                       <output_txt_path>
                                       [--target-duration 120]
                                       [--job-id JOB_ID]
                                       [--window-seconds 6]
                                       [--shot-count 0]
                                       [--key-moment-count 0]
                                       [--event-frame-count 0]
                                       [--focus "the confrontation between X and Y"]
                                       [--focus-env FOCUS]
"""
import argparse
import os


# ---------------------------------------------------------------------------
# Focus directive.
#
# Prepended verbatim (with the user's focus text substituted) at the very
# top of 00_READ_THIS_FIRST.txt, BEFORE the general instructions, whenever
# the operator supplied a focus value in the Stage A form. Placed at the
# top on purpose — this is a hard task-level constraint, not a preference,
# and every subsequent instruction below (transcript reading, index use,
# screenshot viewing, cut selection, narration) has to be understood
# through this lens.
#
# The wording is intentionally emphatic and repeats the constraint several
# times: real-world agents skim these prompts, and a single mention of
# "focus on X" tends to get overridden by the very detailed
# "consider the whole video" machinery that follows.
# ---------------------------------------------------------------------------
FOCUS_DIRECTIVE = """\
################################################################################
##  FOCUS DIRECTIVE — READ AND OBEY BEFORE ANYTHING ELSE BELOW
################################################################################

The operator has narrowed this run to ONE specific focus inside the source
video. This is a HARD, TASK-LEVEL CONSTRAINT — not a preference, not a
hint, and not something to balance against the rest of the instructions
below. It overrides the general "consider the whole video" framing you
will read in the sections that follow.

  >>> FOCUS: {focus_text}

What this means, concretely:

  1. Your entire analysis — transcript reading, index inspection,
     screenshot viewing, character identification, cut selection, and
     voiceover_text writing — must be NARROWED to the focus stated above.

  2. Treat every other plot thread, subplot, side character, running gag,
     cold open, recap, preview, or independently interesting moment in
     this video as OUT OF SCOPE. Even if a non-focus moment is
     objectively strong (high emotional score, high priority in
     key_moments.json, a clear shot boundary with punchy dialogue),
     SKIP IT if it does not directly serve the focus above.

  3. The candidate pool for production.json is NOT "the best beats in the
     whole video". It is "the best beats that build the specific
     throughline named in the focus above". A ~2-minute cut sampled
     evenly across an entire episode is exactly the diluted,
     bouncing-around result the focus directive exists to prevent.

  4. When reading transcript.json in STEP 1 (see working order below):
     build your story map around the FOCUS. Locate where the focus
     begins in the timeline, where it ends, and how it develops. You
     may — and should — ignore stretches of the transcript that
     clearly belong to unrelated plot threads.

  5. When reading key_moments.json in STEP 2: filter the `moments`
     array down to only those moments that belong to the focus. Do NOT
     pick a moment just because its priority is high; a high-priority
     moment from an unrelated subplot is worse for this run than a
     medium-priority moment inside the focus.

  6. When opening screenshots in STEP 3: only open composites that
     cover time ranges where the focus is on screen (or where its
     immediate setup / payoff lives). Do not spend vision budget on
     unrelated stretches.

  7. When writing voiceover_text in STEP 4: every cut must contribute
     to the ONE story defined by the focus above. The concatenated
     narration should read as a single tight arc about that focus,
     from earliest setup to final payoff — not as a highlight reel of
     the whole video with the focus mixed in.

  8. The narration-length target still applies (~{target_duration}s of
     SPOKEN NARRATION across all cuts), but it is subordinate to the
     focus and to each cut's visual completeness. It paces how much you
     write per cut; it is NOT a video length and sets no bound on any
     cut's footage or on `end_seconds`, which is governed solely by the
     PICKING end_seconds rules in STEP 3. If the focus genuinely has
     less material than the narration target can cover, prefer tighter
     narration over padding with unrelated moments; if it has more,
     keep every essential beat and let the narration run a little long
     rather than dropping or truncating footage.

  9. If — after honestly reading the transcript and indexes — you
     cannot locate the focus in this source video at all, say so
     explicitly in a single-line comment at the top of your response
     BEFORE returning production.json, and then return the best production.json
     you can that still respects the focus intent (e.g. its closest
     analog); do NOT silently fall back to a whole-video highlight
     reel.

Everything below this box is the general ClipForge Stage A guidance.
Read it through the lens of the FOCUS above: wherever the general
guidance says "consider the whole video", "the video's story", "the
overall throughline", or similar, substitute "the focus stated above"
in your head. Where the general guidance and the focus directive
conflict, the FOCUS DIRECTIVE wins.

################################################################################

"""


NO_FOCUS_DIRECTIVE = """\
################################################################################
##  NO OPERATOR FOCUS — SELF-SELECT ONE COMPELLING STORY THREAD FIRST
################################################################################

No focus was supplied by the operator. This is NOT permission to make a broad,
evenly sampled episode summary or a whole-video highlight reel. Before you
open any screenshot composite, you must create your own narrow editorial focus
from the evidence in the supplied artifacts.

Follow this order without shortcuts:

  1. Read transcript.json in timeline order. Identify concrete candidate
     scenes, exchanges, reversals, confrontations, reveals, decisions, or
     turning points with a beginning, development, and payoff.

  2. Read key_moments.json before using vision. Use its existing
     `emotional_score`, `priority`, dialogue, and candidate ranking only as
     evidence to compare those transcript-supported candidates. Do not invent
     a new criterion or select a beat merely because it is isolated spectacle.

  3. Choose EXACTLY ONE strongest supported scene, exchange, or story thread.
     It can span several adjacent moments when they form one coherent arc, but
     it must be nameable as one throughline. State that self-selected thread
     in one short line at the top of your response before production.json.

  4. Only after that choice, inspect scene_index.json and selectively open the
     composites that cover this thread's setup, development, and payoff. Do
     not open unrelated windows just to represent other parts of the video.

From the selection onward, treat your self-selected thread exactly like a hard
operator focus:

  - Every screenshot opened, cut selected, and voiceover_text line must serve
    that one thread and advance its single emotional/story arc.
  - Reject the tempting alternative of choosing the strongest beat from each
    act or evenly sampling the source from beginning to end. That produces a
    diluted highlight reel and is a failure for this run.
  - When the source has several independently strong subplots, choose only the
    best-supported one; do not balance them or pad to the target duration with
    unrelated material. A shorter coherent cut is better than a wider one.
  - Continue to use only grounded transcript/index evidence. Do not invent
    connective events, motives, dialogue, or visual details.

The narration-length target remains about {target_duration}s of SPOKEN
NARRATION in total, and it is subordinate to the one self-selected story
thread. It paces narration only — it is not a video length, bounds no
cut's footage, and has no bearing on `end_seconds`, which is governed
solely by the PICKING end_seconds rules in STEP 3. Everything below this
box is general ClipForge guidance; read it through that thread, not
through the whole video.

################################################################################

"""


SERIES_DIRECTIVE = """\
################################################################################
## SERIES MODE — PART {series_part}
################################################################################
This is Part {series_part} of a sequential ClipForge series. Use only source
footage from {series_start_seconds}s onward. Do not recap, say "previously on",
or replay earlier material. Prior-part continuity notes are private guidance:
{series_context}

Start with a strong standalone hook. For every non-final part, choose an honest
cliffhanger rather than forcing a target timestamp. In production.json include
series_id, series_part, series_start_seconds ({series_start_seconds}),
series_end_seconds, series_final (boolean), and a concise non-empty
series_summary. Put "Part {series_part}" directly in title.
################################################################################

"""


TEMPLATE = """\
{series_block}{focus_block}================================================================================
  READ THIS FIRST — Source-video cut-selection instructions for the AI agent
================================================================================

You have been given SIX artifacts from a Stage A run of the ClipForge
pipeline:

  1. transcript.json          — timestamped transcript of the video's audio.
  2. scene_index.json         — locally-computed list of every shot
                                boundary in the video (via ffmpeg
                                scene-change scores). Deterministic,
                                zero-guesswork. See "USING THE INDEXES"
                                below.
  3. key_moments.json         — a locally-ranked SHORTLIST of the
                                high-signal moments in the video (shot
                                cuts, emotionally loaded dialogue).
                                Each moment carries a `why` field. Use
                                this shortlist to decide which stretches
                                to open screenshots for, BEFORE opening
                                any.
  4. screenshots.zip          — a zip archive containing composite JPEG
                                images of the (compressed) source video.
                                Two kinds of image live inside:
                                   • frame_NNNNNN.jpg   (baseline)
                                   • event_NNNNNNNNN.jpg (dense event)
                                See "ABOUT THE SCREENSHOTS ARCHIVE" below.
  5. (the original video)     — attached to the same Release. You do not
                                need to open it; Stage B uses it later.
  6. This file                — your instructions.

Your job: choose the most engaging moments from this video{focus_scope_clause} and output a
`production.json` file (schema at the bottom of this document) that ClipForge's
Stage B will use to slice the ORIGINAL full-quality video and stitch a
short-form commentary base.

The narration ClipForge produces from your `voiceover_text` field is meant
to explain the video to someone who cannot see it. That means your
`voiceover_text` has to describe what is VISUALLY happening on screen —
actions, reactions, expressions, visual gags, scene changes — not just
paraphrase the dialogue. The transcript alone is almost never enough for
this; the indexes plus the screenshots are how you actually see the video.

WRITE IT TO BE EMOTION-FIRST, NOT JUST EVENT-ACCURATE.
An accurate event list still makes viewers scroll. Organize every cut around
what the moment FEELS like and what is emotionally at stake for the people in
it; use the visible plot action as the proof and vehicle for that feeling.
The bar is not "did I list what happened" — it is "does the viewer feel the
pressure, shock, hope, humiliation, relief, or triumph that makes them need
the next beat?" The emotional throughline is the story spine. Concretely:
  - LEAD WITH THE EMOTIONAL TURN. For each beat, first identify the clearest
    source-supported emotional shift: confidence becoming panic, safety
    becoming dread, embarrassment becoming defiance, despair becoming relief,
    or anticipation becoming triumph. Let that shift organize the wording;
    actions explain WHY it lands, rather than competing as a flat event list.
  - NAME CLEAR EMOTIONS DIRECTLY. Plain, punchy emotional statements are
    encouraged when the footage, performance, dialogue, or outcome supports
    them: "She is shocked." "He is terrified." "That humiliation lands."
    "He is triumphant." Give that feeling its own short beat so a hook or
    payoff can land. A frozen stare, flinch, retreat, or grin can be the
    evidence for the emotion — not a substitute for saying it.
  - STAY EVIDENCE-GROUNDED, NOT MIND-READING. Do not invent elaborate hidden
    thoughts, motives, memories, or intentions the source cannot establish
    (for example, "he wonders whether his mother is proud"). That rule does
    NOT ban a direct emotion that is clearly visible, audible, or strongly
    conveyed by the situation. Say "he is devastated" when the performance
    and outcome support it; do not fabricate a private backstory for why.
  - SHORT SENTENCES. One emotional or visual beat, one idea, one sentence. A
    cut with three things happening in it is three short sentences, not one
    sentence stitched together with "while" / "as" / "and". Short lines let
    tension, reaction, and payoff land before the next beat starts.
  - SET UP, THEN SUBVERT. When the source gives an expectation followed by a
    reversal, frame both the event and its emotional consequence: "He thinks
    he has won. Then the door opens. He is horrified." Do not flatten the
    twist into one matter-of-fact clause.
  - DO NOT SKIP THE LOW POINT. Keep the doubt, loss, or apparent failure that
    makes the later relief or triumph satisfying. Going straight from action
    to resolution erases the emotional pressure that earns the payoff.

COMMENTARY RHYTHM — emotion-first narration still moves with crisp, declarative momentum.
  - Make the emotional throughline the connective tissue across cuts. Each
    new action should raise, reverse, confirm, or release a feeling or stake;
    do not merely advance plot mechanics.
  - State source-backed actions and emotional facts plainly. Prefer "The lock
    snaps shut. She is trapped." to hedged framing such as "It looks like she
    may be unable to leave." Do not pad a beat with "seems," "starts to,"
    "kind of," or "maybe" unless the uncertainty is genuinely visible and
    is itself the emotional point.
  - Use specific source-supported details — a number, object, location,
    action, expression, line of dialogue, or consequence — to make the
    emotional claim earned rather than generic hype.
  - Keep most sentences short and complete. One visual beat, direct emotion,
    or emotional consequence per sentence. Let a very short sentence land a
    shock, threat, reversal, humiliation, relief, or triumph.
  - Keep the overall story frame in past tense when required, but use
    present-tense immediacy for the play-by-play inside each beat: "He slips
    inside" and "he takes the bait" feel alive where "he had gone inside"
    and "he later took the bait" flatten the moment.
  - Build short cause-and-effect chains in which the effect is often an
    emotional consequence: action → consequence → fear, relief, rage, or
    resolve. Give each link its own compact sentence instead of one
    clause-heavy line.
  - Before a twist or reveal, withhold the explanation for one beat: signal
    the danger, hope, or pressure first, then resolve what it means in the
    next short sentence. Keep stakes active with brief recurring language
    such as "his suspicion is confirmed" or "it is already too late" rather
    than re-explaining plot mechanics.
  - EMOTION-FIRST EXAMPLE. If visible footage shows a competitor miss, stare
    at the result, then lift a trophy after a reversal, write: "For a second,
    it looks like he has lost. Then the result changes. He is stunned — and
    triumphant." This is stronger than a neutral list such as "He misses,
    looks at the result, and lifts the trophy" because the same supported
    events are organized around the viewer's emotional experience.

WORD CHOICE & PERSONALITY — change the phrasing, NOT the voice delivery.
  - Sound like a sharp human commentator talking naturally, not a polished
    corporate recap. Keep the same clear, concise structure above, but allow
    casual turns of phrase, playful understatement, teasing, blunt reactions,
    and light slang when the moment earns them.
  - Let the source drive the attitude. A disastrous plan can be "cooked"; a
    clear fumble can be "a brutal sell"; an obvious reversal can get a dry
    "well, that went badly." These are examples of natural phrasing, not
    catchphrases to force into every cut.
  - Mild profanity is allowed very occasionally when it genuinely sharpens a
    shock, frustration, or comic beat (for example: "damn," "hell," or
    "crap"). Never use it as filler, pile it into consecutive lines, or make
    the narration sound aggressively vulgar.
  - Tease a visible choice, plan, or consequence — never a protected trait,
    a real person's identity, or someone in genuine distress. If the source is
    painful, vulnerable, or serious, stay direct and humane instead of trying
    to score a joke.
  - Keep every line source-backed. Do not invent motives, insults, or
    off-screen context merely to sound edgy. The personality should feel
    spontaneous because the wording fits the visible beat, not because it
    tries too hard.
  - This is a LANGUAGE-ONLY instruction. Do not add performance directions,
    filler words, ad-libs, sound effects, or text outside `voiceover_text`.

CHARACTER IDENTIFICATION IS YOUR JOB.
The pipeline no longer runs a local face-clustering step. That step was
misidentifying people (splitting the same character into two labels, or
merging distinct characters together, or missing brief appearances) and
its errors were poisoning downstream selection and narration. You are
the vision model — you can see the screenshots and read the transcript,
so you identify who is who directly from those two sources. Do it the
way any careful viewer would: infer names from dialogue (people
addressing each other, self-introductions, third-person references,
on-screen text), pick a consistent descriptor when no name is spoken,
and use the SAME label for the same character across every cut.

--------------------------------------------------------------------------------
CRITICAL WORKING ORDER (read this before doing anything else)
--------------------------------------------------------------------------------

The order below is the single most important thing in this document.

  STEP 1: Read `transcript.json` end-to-end. Do not open any composite
     screenshots yet. Reconstruct the story from the dialogue. Write
     down (mentally or in a scratch buffer) what the video appears to
     be about, who the recurring speakers seem to be, and where the
     beats and turning points sit on the timeline. Note every proper
     name that appears — someone being addressed by name, someone
     introducing themselves, a third-person reference — and keep those
     names next to the timestamp where they were said, so you can bind
     each name to a visible character when you open the screenshots
     later.{step1_focus_note}

  STEP 2: Open `scene_index.json` and `key_moments.json`.
     - `scene_index.json` gives you EXACT shot boundaries — where the
       camera cuts. You no longer have to infer cuts by comparing
       panels; the boundaries are enumerated.
     - `key_moments.json` gives you a locally-ranked shortlist of the
       most promising cut candidates, each annotated with
       `transcript_excerpt`, `signals` (emotional_score,
       dialogue_density, priority), and a `why` field. Read the whole
       `moments` array. This is your candidate pool. Cross-check it
       against your Step 1 story map and pick the moments that
       genuinely serve the throughline of the video, not just the
       highest-priority ones in isolation.{step2_focus_note}

  STEP 3: For each candidate moment/cut range from Step 2, OPEN
     screenshots to fill in what the transcript and indexes still can't
     tell you — including WHO is on screen. See "ABOUT THE SCREENSHOTS
     ARCHIVE" and "HOW TO SELECT CUTS" below for the viewing rules —
     chronological order, adjacent windows, dense event composites for
     boundary-crossing beats. As you view screenshots for the first
     few cuts, lock in a consistent label for each recurring
     character (their real name if the transcript names them, or a
     short descriptive tag if it doesn't) and reuse that same label
     for the rest of the run.

  STEP 4: Assemble production.json using the voiceover_text contract
     described near the bottom of this file, and write the single
     `title` field described in the OUTPUT SCHEMA — ONE title for the
     whole job, decided AFTER your cuts are final (it must describe
     the story your selected cuts tell, not the source video in
     general). ALSO write the `hashtags` and `youtube_tags` arrays
     described in the POSTING PACKAGE METADATA section — the same
     one-per-job social-media metadata that ships alongside the
     finished video.

--------------------------------------------------------------------------------
VIDEO METADATA (substituted by Stage A)
--------------------------------------------------------------------------------

  Job ID:                  {job_id}
  Full video duration:     {duration_seconds} seconds ({duration_hms})
  Focus (this run):        {focus_metadata}
  Baseline cadence:        one composite JPEG per {window_seconds}-second
                           window of source video. Each composite is a
                           3x2 grid of 6 panels, and each panel is one
                           frame from one of the 6 seconds inside that
                           window (panel 1 = second 1 of the window,
                           panel 6 = second 6 of the window).
  Baseline screenshots:    frame_000000.jpg .. frame_{last_window_start:06d}.jpg
                           ({total_frames} files total). The number in
                           each filename is the WINDOW START SECOND,
                           zero-padded to 6 digits. Consecutive
                           filenames step by {window_seconds}. File
                           frame_SSSSSS.jpg covers source seconds
                           [SSSSSS, SSSSSS+{window_seconds}).
  Dense event composites:  {event_frame_count} additional images named
                           event_NNNNNNNNN.jpg (9-digit MILLISECOND
                           timestamp of the CENTER of the event window,
                           zero-padded). Each event composite is also a
                           3x2 grid, but sampled ~1.5 fps across a
                           tighter 4-second window centered on a
                           high-signal moment (a shot boundary or a
                           high-priority beat). Use these to see fast
                           beats the 6-second baseline may only
                           partially capture.
  Local scene index:       {shot_count} shot(s) enumerated in
                           scene_index.json.
  Local key-moments list:  {key_moment_count} moment(s) in
                           key_moments.json.
  Narration length target: ~{target_duration} seconds of SPOKEN
                           NARRATION in total (user-selected in the Stage
                           A form; approximate — favor engagement over
                           hitting the number exactly). This is NOT the
                           output video's length: it bounds no cut's
                           footage and has no bearing on `end_seconds`,
                           which is governed solely by the PICKING
                           end_seconds rules in STEP 3.

--------------------------------------------------------------------------------
USING THE INDEXES
--------------------------------------------------------------------------------

The two JSON indexes are the cheap-vision layer. They were produced
locally on the runner using ffmpeg — no LLM vision was spent building
them. That means you can lean on them heavily WITHOUT paying vision
tokens for the coverage they provide.

  scene_index.json
    - The definitive list of shot boundaries. Every entry is `{{shot_id,
      start_seconds, end_seconds, keyframe_seconds, cause}}`.
    - When you write voiceover_text for a cut that spans multiple
      shot_ids, you already know exactly where the camera cut inside
      it — describe those transitions accurately ("the shot cuts to a
      wide of the arena", "we cut back to the boy") instead of
      guessing.
    - `keyframe_seconds` is the midpoint of a shot — the single most
      representative timestamp. If you want a single frame from a
      shot, that's the one to look at.

  key_moments.json
    - Your candidate shortlist. Every moment has:
        * `start_seconds` — the shot's start (a real shot boundary).
        * `shot_end_seconds` — the raw shot boundary, i.e. the frame
          BEFORE the camera cuts away from this shot.
        * `end_seconds` — `shot_end_seconds` PLUS a small
          `visual_tail_seconds` extension into the next shot, so the
          moment's window actually contains the beat's on-screen
          resolution (reaction shot, cutaway, aftermath), not just its
          last spoken word. Prefer `end_seconds` over
          `shot_end_seconds` when you use this moment as a cut-end
          anchor.
        * `visual_tail_seconds` — how much tail was added past the
          raw shot boundary. See `visual_tail_policy` at the top of
          the file.
        * `transcript_excerpt` — the dialogue during the window.
        * `signals` — is_shot_boundary, emotional_score,
          dialogue_density, priority.
        * `why` — human-readable list of the exact reasons this
          moment was flagged (and, when the tail was extended, why).
    - Priority is a HINT, not a mandate. A high priority means
      "worth a look", not "must be a cut". The best cut list is the
      one that tells one connected story, not the one that greedily
      picks the top-N priorities.
    - Moments are already sorted chronologically in the file.{indexes_focus_note}

--------------------------------------------------------------------------------
ABOUT THE SCREENSHOTS ARCHIVE — read this carefully
--------------------------------------------------------------------------------

The screenshots ship as a single zip file (`screenshots.zip`) purely for
transport efficiency. Treat it as a normal working input:

  • EXTRACTING screenshots.zip is EXPECTED and REQUIRED. Do it up front,
    the same way you would unzip any other input archive. Extraction is
    a cheap local file operation — it does NOT consume vision tokens and
    does NOT count as "looking at" the images.

  • What is expensive is VIEWING / LOADING / FEEDING-TO-VISION every
    image inside the archive. The thing to avoid is indiscriminately
    piping the entire folder into your vision context in one shot. Do
    NOT do that.

  • After extraction you will have a `screenshots/` directory containing:
      - `frame_NNNNNN.jpg` — baseline 6-second-window composites.
      - `event_NNNNNNNNN.jpg` — dense 4-second-window composites, one
        per high-signal moment. Sampled at ~1.5 fps so beats that fell
        between baseline panels are resolvable here.

  • Do NOT rely on the transcript alone. In a lot of shorts the visuals
    carry information that the transcript literally cannot: physical
    actions, facial reactions, sight gags, on-screen text, cutaways,
    and scene changes. View enough windows to reconstruct the visual
    sequence of events for each cut you plan to keep.

  • Each frame_SSSSSS.jpg is a 3x2 grid composite that represents a
    {window_seconds}-SECOND WINDOW of the source video, starting at the
    second `SSSSSS` in the filename. The 6 panels are sampled ONE PER
    SECOND across those {window_seconds} seconds. Reading order is
    English reading order (left-to-right, then top-to-bottom):

        [ s+0s   s+1s   s+2s ]   <- one frame each from seconds 1, 2, 3 of the window
        [ s+3s   s+4s   s+5s ]   <- one frame each from seconds 4, 5, 6 of the window

    where `s` is the window's start second (the number in the filename).

  • Each event_NNNNNNNNN.jpg is a 3x2 grid composite of a 4-SECOND
    window CENTERED on the millisecond timestamp encoded in the
    filename (divide by 1000 to get the center in seconds). Panels are
    sampled at ~1.5 fps, so consecutive panels are ~0.66s apart.
    Reading order is the same left-to-right, top-to-bottom. Prefer
    event composites for beats you know are on a shot boundary (i.e.
    anything on the key_moments.json shortlist with
    `is_shot_boundary=true`) — they resolve fast beats that the
    baseline can miss.

  • When you need finer-grained temporal information than one baseline
    file can give you, open the ADJACENT baseline files
    (frame_<S>.jpg, frame_<S+{window_seconds}>.jpg,
    frame_<S+{two_windows}>.jpg, ...). Each successive file continues
    the sequence into the next {window_seconds}-second window.

In short:
    Extract archive         → ALWAYS do this. Cheap. Expected.
    Load a baseline
      composite             → Deliberately, as needed to understand a
                              specific candidate cut.
    Load an event composite → Prefer over the baseline for any beat
                              flagged in key_moments.json — same 1-image
                              cost, ~4x the temporal resolution at the
                              important spot.
    Load every image
      in bulk               → DON'T. Be intentional, not exhaustive.

--------------------------------------------------------------------------------
HOW TO SELECT CUTS AND DESCRIBE THEM
--------------------------------------------------------------------------------

STEP 1 (indexes-first). You have already read transcript.json,
        scene_index.json, and key_moments.json (Steps 1-2 above). Your
        candidate cut pool is the `moments` array of key_moments.json,
        filtered against your story map from transcript.json.{cut_step1_focus_note}

        Prioritize keeping moments that:
          - Sit on strong emotional dialogue (`emotional_score` >= 0.4).
          - Advance the story (a new confrontation, a reveal, a
            reaction beat).
          - Are the payoff of a setup earlier in the video (check the
            transcript, not just the individual moment).

        Skip moments that:
          - Are shot boundaries with no dialogue signal (usually
            establishing shots or filler).
          - Repeat information a previous cut already delivered.
          - Land in intro / outro / recap / preview sections.{cut_step1_skip_focus_note}

STEP 2 (open screenshots — in chronological order — to fill visuals).

        Frame filename convention (baseline):

            frame_<window_start_seconds>.jpg    (zero-padded to 6 digits)

        Each such file covers the half-open interval
        [window_start, window_start + {window_seconds}) seconds. To
        find the baseline file that covers a specific moment T (in
        seconds), floor T to the nearest multiple of {window_seconds}:

            window_start = (T // {window_seconds}) * {window_seconds}

        e.g. for a moment around 4 minutes 32 seconds in (T = 272s):
        272 // {window_seconds} = {example_div}, so the covering file is
        `screenshots/frame_{example_window:06d}.jpg`, which covers
        seconds [{example_window}, {example_window_end}).

        Event filename convention (dense, for high-signal beats):

            event_<center_milliseconds>.jpg    (zero-padded to 9 digits)

        The number is the CENTER of the 4-second event window in
        milliseconds. To find an event composite near a moment M
        seconds, look for a file whose center is within ~2 seconds of
        M (i.e. abs(center_ms/1000 - M) <= 2.0). If one exists, prefer
        it over the baseline for that beat.

        Every high-signal moment in key_moments.json emits TWO event
        composites: one centered on its `start_seconds` (the onset of
        the beat) AND one centered on its `end_seconds` (the beat's
        on-screen resolution — already pre-padded past the raw shot
        boundary by `visual_tail_seconds`). This is deliberate: the
        tail composite is what lets you verify that the described
        action has actually finished on screen before you commit an
        `end_seconds` value. ALWAYS open the tail composite before
        finalizing `end_seconds` for a cut that anchors on this
        moment — see "PICKING end_seconds" in STEP 3 below.

        VIEWING ORDER — this is a hard rule, not a suggestion:

          - Within a single candidate range, open composites in
            ASCENDING timestamp order (canonical / chronological).
            Never jump around inside the range out of order.
          - Across candidates, work through them in the order they
            occur in the video (earliest start_seconds first).
          - Do NOT interleave windows from unrelated parts of the
            video. Out-of-order viewing is a major cause of
            inaccurate voiceover_text.
          - If you realize mid-way that you need a window from an
            EARLIER candidate you already left, go back and view it
            in ascending order before returning.

        How many windows to open per candidate:
          - Aim for enough coverage that you could confidently write a
            plain-prose description of what a viewer sees during the
            cut, including any visual beat that isn't spoken.
          - For most beats: ONE event composite (if available) OR one
            baseline composite is enough for the specific moment,
            plus the adjacent baseline composite on either side for
            surrounding context.
          - Sample MORE composites when the shot is action-heavy or
            has visual gags the transcript won't capture.
          - Sample FEWER when the shot is a static talking head and
            the transcript already describes the content well.
          - If after your first pass you still don't understand what
            is happening, open MORE composites for the SAME candidate
            (still in ascending order). Do not wander off.

STEP 3 (assemble cuts).

        - Order cuts chronologically by `start_seconds`.
        - Each cut should be self-contained enough that a viewer landing
          on it makes sense — favor cutting at natural sentence / beat
          boundaries from the transcript, not mid-word.
        - Target the sum of (end - start) across all cuts at roughly
          {target_duration} seconds. It does NOT need to be exact.
        - `start_seconds` and `end_seconds` are still expressed in
          SOURCE-VIDEO seconds (integers, second-precision). They do
          NOT need to align with window boundaries.

        --------------------------------------------------------------
        PICKING end_seconds — read this before writing any cut
        --------------------------------------------------------------

        This is the single most common failure mode of production.json:
        `end_seconds` chosen at the point where the NARRATION or
        DIALOGUE ends, one or two seconds BEFORE the described
        on-screen action actually finishes happening. The result is a
        scene_XX.mp4 that references an event ("his eye flashes red",
        "the storm crashes down", "she hooks her pinky around his",
        "he stalks toward the door") and then cuts off right before
        the viewer can see that event on screen. Do not do this.

        The rule is simple and non-negotiable:

          If your voiceover_text for this cut describes an action, a
          reaction, a cutaway, or a visible result, then `end_seconds`
          MUST sit AFTER that action / reaction / result is visibly
          complete on screen. Not at the last spoken word. Not at the
          shot boundary just before the payoff. AFTER the payoff.

        Concretely:

          (a) Ignore "where the transcript segment ends" as an anchor
              for `end_seconds`. Whisper's segment ends are aligned to
              the last SPOKEN WORD (± a small pad) — the visual beat
              a viewer would describe as "the moment" almost always
              lands AFTER the last word, in the next 1–4 seconds of
              screen time.

          (b) Do NOT anchor `end_seconds` to the shot's
              `shot_end_seconds` from scene_index.json / key_moments.json.
              Short-form narrative beats routinely resolve ACROSS the
              cut: the reaction shot, the cutaway to what was just
              referenced, the aftermath of the punch, the flash of
              someone's eye as they close it — those live in the NEXT
              shot, not the current one. If the described beat's
              payoff clearly lives in the next shot, extend
              `end_seconds` into that next shot until the payoff is
              on screen.

          (c) The `end_seconds` value in each key_moments.json entry
              is already pre-padded past `shot_end_seconds` by a
              small `visual_tail_seconds` for exactly this reason.
              Use that as your MINIMUM. Only shrink it back below
              that value if you have specifically verified from the
              screenshots that the described action truly completed
              earlier; and even then, keep at least ~1s of visual
              tail after the last described beat.

          (d) Verify visually. Open the event composite centered on
              your candidate `end_seconds` (there is one for every
              key_moments.json moment — same event_<ms>.jpg naming as
              the start-of-moment composite) and confirm the described
              action's resolution is visible in one of its panels. If
              the payoff is still in progress at the last panel, push
              `end_seconds` forward by another 1–2 seconds and check
              again. If it already completed by the first panel, you
              may pull `end_seconds` back — but keep at least ~1s of
              tail after the described beat.

          (e) Tail budget. A visual tail of roughly 1–4 seconds past
              the on-screen end of the described beat is the right
              range for essentially every cut. Less than 1s reads as
              a cut-off; more than 4s starts dragging.

          (f) Self-check before finalizing each cut: re-read your
              voiceover_text and, for every distinct beat it mentions,
              confirm you have visual evidence (from the screenshots
              you opened) that the beat is INSIDE
              [`start_seconds`, `end_seconds`]. If any described beat
              lands at or past `end_seconds`, extend `end_seconds`.
              This check is more important than hitting the target
              total duration exactly.

        - For each cut, write a `voiceover_text` field as an engaging,
          emotion-first account of WHAT HAPPENS in that segment. Before
          drafting, identify what shifts emotionally for the people involved
          and what is at stake; then use the visual sequence of events
          (actions, reactions, expressions, scene changes, on-screen text,
          visual gags) and essential dialogue as the evidence that makes that
          emotional throughline believable. Do not write a neutral chronology
          with one decorative feeling word inserted.
        - Write the FINAL `voiceover_text` in a tight, emotion-first
          commentary rhythm: short declarative sentences, concrete
          source-backed details, minimal filler, and steady forward pull.
          Keep the story frame in past tense where appropriate, but narrate
          the visible moment in present tense for immediacy. Give an action,
          its consequence, and its emotional effect separate short sentences.
          Before a twist, withhold its meaning for one beat, then reveal it;
          reinforce the emotional stakes with concise recurring stakes-language
          instead of re-explaining plot. Name a clearly supported reaction
          directly and plainly — "she is terrified," "he is humiliated," "they
          are relieved" — rather than hiding it behind an action-only account.
          Do not invent an unsupported private monologue, motive, or backstory.
          Use one sentence for setup and another for the reversal, feeling, or
          payoff. Do not hedge, recap the same beat twice, or turn a simple
          visual fact into a long, winding sentence. A short blunt sentence is
          welcome when the scene lands a shock, threat, loss, relief, or triumph.
        - Give the wording a little lived-in personality when it fits: casual
          phrasing, dry teasing, light slang, and an occasional mild curse
          word are welcome only when grounded in the visible moment. Keep it
          playful rather than forced, cruel, repetitive, or overly vulgar.
          Do not change narration pace, delivery, or audio direction; this
          affects the words on the page only.
        - Keep clear setup-then-payoff structure with the emotional shift as
          its own beat — see "WRITE IT TO BE EMOTION-FIRST, NOT JUST
          EVENT-ACCURATE" above. Do not default to one long clause-stacked
          sentence per beat.

        OPENING HOOK — FIRST-CUT WRITING:

        The first 1–2 sentences of the FIRST cut have a higher standard than
        ordinary beat narration. They are the viewer's reason to continue,
        not merely the chronological start of a summary. Lead with the
        strongest source-supported emotional pressure point — the fear,
        shock, humiliation, hope, relief, desire, or triumph that makes the
        event matter — then use the event as proof. Before drafting that cut,
        identify the strongest fact genuinely supported by the selected story
        and screenshots: the most curiosity-inducing, surprising, dangerous,
        absurd, emotional, ironic, or high-stakes truth.

        Do NOT begin by mechanically summarizing the first chronological event
        when a later consequence, reversal, impossible situation, visible
        mistake, unanswered question, or contrast creates a better honest way
        into the story. You may tease that source-supported consequence first,
        then give the minimum context needed to make the following beats clear.
        Tease; do not falsely reveal or spoil the whole payoff unless the
        revealed outcome itself is clearly the strongest truthful hook.

        Choose the hook mechanism that fits THIS footage, not a reusable
        formula: curiosity, consequence, stakes, irony, mystery, shock,
        character mistake, impossible situation, emotional tension,
        absurdity, or contradiction. Vary the mechanism naturally across
        stories. Do not force drama into a quiet scene and do not make every
        story sound like the same kind of viral clip.

        Write several candidate opening lines internally, then select the
        most compact, natural one that satisfies at least two of these when
        the source supports them: curiosity, stakes, an implied consequence,
        an unanswered question, a surprising fact, tension, or irony. The
        chosen line must be immediately understandable when spoken, leave a
        meaningful question unanswered, and flow naturally into the next line.
        Aim for roughly 7–14 words in the first sentence; do not exceed about
        16 words unless a source-backed name or fact genuinely needs the room.
        Reject a candidate that merely begins with a generic person doing the
        first action (for example, "a man picks up...", "he walks in...",
        "a teenager steps into...", or "she goes to confront...") when the
        real hook can instead front-load the pressure point, unusual object,
        consequence, or reversal. This is a quality guard, not a mechanical
        ban: use a person-first line only when it is truly the most compelling
        source-backed entry point. If a later source-backed disruption makes an
        ordinary entrance dangerous, awkward, absurd, or consequential, frame
        that disruption before explaining who walked where.

        For a mystery or reveal, default to withholding the hidden answer in
        the opening. Do not resolve the mystery by naming the contained item,
        culprit, final identity, or last beat when a truthful unanswered
        question can carry the hook. Let the viewer discover the payoff through
        the footage and following narration. Reveal it early only when the
        outcome itself is plainly the strongest honest reason to continue —
        not merely because it is available in the source notes.

        Use HOOK → minimal context → escalation → payoff where the footage
        supports that arc. Give each sentence forward pull: an event should
        imply the next consequence or question, not become a flat list of
        actions. Avoid spending the opening on generic framing such as a bare
        chronological setup, "in this scene," "basically," or "after that"
        unless it is genuinely the strongest source-backed wording.

        OPENING QUALITY GATE — before returning production.json, internally
        test the first 1–2 sentences. Would a viewer who knows nothing about
        the source want the next sentence? Is this stronger than merely
        describing the first event? Does it create genuine curiosity, tension,
        stakes, surprise, irony, or an unanswered question? Is every claim
        supported by the source? Does it sound natural in the existing spoken
        commentary voice? Does it avoid forced slang, generic viral phrasing,
        and an unnecessary payoff spoiler? Does it pull naturally into the
        next sentence? If the answer to any applicable question is no, rewrite
        the opening internally before producing the final JSON.

        HIGHER CURIOSITY WITHOUT LOWERING ACCURACY: do not invent events,
        motivations, dialogue, reactions, relationships, or consequences.
        Do not manufacture suspense unsupported by the screenshots,
        transcript, indexes, or selected cuts. Do not force slang, fake
        excitement, or generic viral phrasing. The language itself, not the
        TTS, must make the opening stronger; preserve the existing natural,
        confident commentary personality and all existing delivery behavior.

        CHARACTER LABELS IN voiceover_text:
        - Identify each recurring character yourself from the
          screenshots and transcript, and refer to them by the SAME
          label in every cut. Label priority:
            (a) REAL NAME — if the transcript makes a name clear
                (someone addressed by name, a self-introduction, a
                third-person reference that lines up with who is on
                screen at that timestamp), use the real name (e.g.
                "Killua", "Gon"). On first use you may pair the name
                with one short descriptor to anchor the visual
                ("Killua, the white-haired boy, …"), then use the
                name alone (or a pronoun clearly referring back to
                him) from then on.
            (b) DESCRIPTIVE TAG — if no name evidence exists, pick a
                short natural descriptor and keep it CONSISTENT
                across every cut (e.g. always "the silver-haired
                boy").
        - Once a real name is established, use it for the rest of the
          output. Do NOT acknowledge the name once and then fall back
          to "the white-haired boy" in later cuts — a resolved name
          stays resolved. Never switch labels for the same character
          between cuts — that reads as a highlight reel of strangers.
        - When unsure whether two on-screen faces are the same person,
          look at the screenshots — hair, outfit, and context are
          usually enough to decide. If you truly cannot tell, keep the
          descriptions generic ("a boy in a white shirt") rather than
          committing to a wrong identity.

        NARRATE THE CUTS AS ONE CONNECTED STORY:

        The voiceover_text fields, in the order the cuts appear in
        production.json, are the FINAL script. There is no second
        commentary-writing pass: each field is synthesized to speech
        verbatim (text-to-speech) and played over its own cut, in
        order, as the narration of ONE video. Write every
        voiceover_text as finished, speakable prose — no brackets,
        no stage directions, no [uncertain name] placeholders, no
        notes-to-self; if a name is uncertain, use a plain descriptor
        instead. If each voiceover_text reads like a
        standalone description of a good moment, the final voiceover
        ends up as a disconnected highlight reel — no throughline,
        no cause and effect. Viewers stay engaged when they can
        follow a story from setup to payoff, not when they are shown
        a list of separately interesting clips.

        Treat the sequence of cuts as ONE story you are narrating in
        order, and write each voiceover_text with awareness of what
        came before it in the source video:

          - Before writing voiceover_text for the first cut, briefly
            note what the overall story of the source video is (from
            your Step 1 story map) and what arc the cuts you have
            chosen trace through it — who the subject is, what they
            want or are up against, and how the chosen beats
            progress from beginning to end.{narration_focus_note}

          - Between one cut and the next in the source there is
            usually a gap of footage you deliberately did NOT select.
            Sometimes the two cuts still follow naturally (same
            scene continuing, same subject, obvious next beat) and
            need no bridge. But often there IS a gap the viewer will
            feel — a change of location, a time skip, a new subject
            appearing, an unexplained new state, or a payoff whose
            setup lives in the un-selected footage in between. When
            that gap exists, OPEN the next cut's voiceover_text with
            the SHORTEST possible connective phrase or sentence that
            carries the viewer across it. Keep it terse — the point
            is linkage, not exposition.

          - The connective lead-in must only summarize / frame what
            the source material actually implies (from the
            transcript, indexes, and screenshots you viewed). Do
            NOT invent new events, new dialogue, new characters, or
            new motivations to bridge the gap.

          - When two adjacent cuts are linked as setup → payoff, or
            cause → effect, make that relationship legible in the
            wording of the second cut's voiceover_text ("Because of
            that, …", "This is what he was warned about at the
            start: …", "The device she rigged earlier now …").

          - Refer to recurring people/entities the SAME way across
            every cut. Whichever label you chose for a character in
            their first cut stays fixed through the entire
            production.json. Don't reset labels between cuts.

          - Each individual cut's voiceover_text is still primarily
            a plain, matter-of-fact description of what happens
            visually inside THAT cut. The connective tissue is a
            lead-in of at most one short sentence or clause, not a
            paragraph of recap.

          - The first cut's voiceover_text opens the story. It does
            not need a connective lead-in. Apply the OPENING HOOK rule:
            lead with the strongest compact, source-supported reason to
            keep watching rather than automatically orienting who / what /
            where in the first sentence. Supply that essential context in
            the next short sentence or as soon as the story needs it.

          - The last cut's voiceover_text ends the story. Land on the
            final beat from the source; do not add a wrap-up.

--------------------------------------------------------------------------------
JOB TITLE — ONE title for the entire job (top-level `title` field)
--------------------------------------------------------------------------------

Every scene clip Stage B cuts from this production.json belongs to ONE posting
package, so they all share ONE title. You generate that title ONCE per
job, here, as a top-level `title` field — NOT per cut, NOT inside any
cut object.

Rules for the title:

  - Decide it AFTER your cuts are final. It must hook the specific story
    your selected cuts tell — not the source video in general, and not
    a beat you ended up leaving out.
  - ONE single line of plain text. No emoji, no leading/trailing
    quotation marks, no markdown, no hashtags, no episode numbers, no
    trailing punctuation.
  - Catchy and attention-grabbing for short-form feeds: front-load the
    most arresting element (the twist, the stakes, the impossible
    thing), keep it specific to what actually happens in the cuts, and
    keep it under roughly 90 characters.
  - Keep it to ONE short sentence or phrase, roughly 5-8 words. A little
    flexibility is fine for natural phrasing, but this is a simple title,
    not a description, plot summary, or sentence packed with context.
  - Never invent events the cuts do not show. A title that promises a
    payoff the viewer never sees is worse than a plain one.

--------------------------------------------------------------------------------
POSTING PACKAGE METADATA — hashtags + YouTube tags (top-level fields)
--------------------------------------------------------------------------------

Alongside the one-per-job `title`, every production.json also carries the
social-media metadata the finished video will be posted with:

  - `hashtags`   — a JSON array of hashtag strings.
  - `youtube_tags` — a JSON array of YouTube keyword-tag strings.

These are generated ONCE per job (like the title), from the SAME story your
selected cuts tell, and shipped inside production.json so Stage B can drop
them straight into the final artifact's metadata.txt (no second agent, no
second pass).

Rules for `hashtags`:

  - JSON array of 5-8 strings.
  - Each string is ONE hashtag INCLUDING the leading `#`
    (e.g. `"#anime"`, `"#hunterxhunter"`). No spaces inside a hashtag,
    no punctuation other than the leading `#`, no emoji.
  - Mix broad-reach tags (the genre, the format) with niche tags (the
    specific show / subject / character / trope your cuts are about),
    ordered most-relevant-first.
  - Tie them to what the cuts actually show — not the source video in
    general and not a beat you left out. If a hashtag would only make
    sense to someone who saw an un-selected part of the source,
    drop it.
  - No duplicates. No brand/handle hashtags for accounts you don't
    control. No hashtag stuffing beyond 8.

Rules for `youtube_tags`:

  - JSON array of 10-20 strings.
  - Each string is one YouTube keyword tag — plain lowercase words or
    short phrases, NO leading `#`, NO commas inside a single tag, NO
    quotes, NO emoji. Multi-word phrases are fine (`"hunter x hunter
    hanzo fight"`).
  - Mix broad + niche + long-tail, ordered most-relevant-first, so the
    concatenated list stays under YouTube's 500-character total limit.
  - Same content discipline as hashtags: describe the STORY the cuts
    actually tell (subject, characters, key beats, genre, format),
    not the whole source video.
  - No duplicates. No repeating the title verbatim as a tag.

--------------------------------------------------------------------------------
OUTPUT SCHEMA — production.json  (return EXACTLY this shape, no extra keys)
--------------------------------------------------------------------------------

{{
  "video_duration_seconds": {duration_seconds},
  "title": "ONE catchy, attention-grabbing title for this whole job — a single 5-8 word sentence or phrase, not a description (see JOB TITLE above)",
  "hashtags": [
    "#hashtag1",
    "#hashtag2"
    // ... 5-8 hashtags total, most-relevant-first, each including the leading '#'
    // (see POSTING PACKAGE METADATA above)
  ],
  "youtube_tags": [
    "keyword tag 1",
    "keyword tag 2"
    // ... 10-20 YouTube keyword tags total, most-relevant-first, no '#', no commas
    // inside a single tag (see POSTING PACKAGE METADATA above)
  ],
  "cuts": [
    {{
      "start_seconds": 142,
      "end_seconds": 170,
      "voiceover_text": "the FINAL, ready-to-speak voiceover line for this cut. This exact text is synthesized to speech by the pipeline and heard VERBATIM in the finished video — write it as spoken narration, not as notes about the video. Describe what is visually happening (actions, reactions, scene changes, essential dialogue), in chronological order, using SHORT sentences (one beat per sentence, not clause-stacked run-ons), a confident third person present tense by default, an emotion-first throughline: use visible plot action to prove what the character stands to feel, then state every clearly supported emotional shift directly in its own short beat before moving to the next event. Written as one scene of a continuous story: when there is a real gap between this cut and the previous one, open with a short connective phrase (e.g. 'Later, …', 'After the fight, …') so the moment lands as part of the same throughline, not as a fresh unrelated clip. Do not invent events to bridge the gap. Refer to recurring characters with the SAME descriptor or name you used in earlier cuts.",
      "keywords": [
        {{"word": "one genuinely noteworthy word from this cut's voiceover_text", "color": "#FF5C5C"}}
        // optional: choose only the words worth emphasizing and assign each exact #RRGGBB color yourself
      ]
    }}
    // ... more cuts, ordered chronologically ...
  ],
  "target_total_duration_seconds": {target_duration}
}}

NARRATION DURATION CONTRACT — REQUIRED FOR EVERY CUT
  Edge TTS is currently configured for about 188 words per minute (3.133
  spoken words per second). Before returning JSON, calculate EACH cut's
  voiceover word budget as `(end_seconds - start_seconds) * (188 / 60)`.
  Write enough final spoken words to cover that full budget. A cut MUST NOT
  fall below 90% of this budget (2.82 words per second), so narration covers
  at least 90% of the planned cut duration at the real TTS pace. This is a
  hard delivery requirement, not a suggestion: do not return a sparse summary
  for a long cut. For example, a 75-second cut targets about 235 words and
  requires at least 212 spoken words. Count the actual words in every
  `voiceover_text` before returning the plan; add source-grounded chronological
  beats, reactions, and connective detail until every cut meets its minimum.

CONSTRAINTS
  - `start_seconds` and `end_seconds` are integers, in seconds since the
    start of the video. `end_seconds > start_seconds`. Both must lie
    within [0, {duration_seconds}].
  - Cuts MUST NOT overlap.
  - Cuts MUST be sorted ascending by `start_seconds`.
  - `end_seconds` must sit AFTER the visual resolution of every event
    the cut's `voiceover_text` describes. It is NOT the last spoken
    word, and it is NOT the raw shot boundary at the end of the
    current shot — both routinely land BEFORE the described action's
    on-screen payoff. See the "PICKING end_seconds" block in STEP 3
    for the full rule and self-check. When in doubt, err on the side
    of 1–2 extra seconds of tail; a slightly long cut is fine, a cut
    that ends before its described beat is on screen is broken.
  - `title` is a single non-empty string: one catchy title for the WHOLE
    job (all cuts share it), roughly 5-8 words as one short sentence or
    phrase. One line, plain text, no emoji/quotes/markdown, and not a plot
    description or summary.
  - `hashtags` is a JSON array of 5-8 non-empty strings, each beginning
    with `#`, no spaces inside a hashtag, no duplicates. See the
    POSTING PACKAGE METADATA section above.
  - `youtube_tags` is a JSON array of 10-20 non-empty strings, no leading
    `#`, no commas inside a single tag, no duplicates, joined length under
    500 characters. See the POSTING PACKAGE METADATA section above.
  - `voiceover_text` is the final spoken line for its cut: engaging
    prose meant to be READ ALOUD, not a flat matter-of-fact summary.
    Its word count is REQUIRED to meet the NARRATION DURATION CONTRACT above:
    target `(end_seconds - start_seconds) * (188 / 60)` spoken words and never
    return fewer than 90% of that target. Long cuts need correspondingly rich,
    source-grounded narration; do not compress a long cut into a one-sentence
    summary. No markdown, no timestamps inside it, no brackets, no
    parentheticals, no directions or commentary ABOUT the video —
    everything in it is heard by the viewer verbatim. Meet the hard
    word-budget floor above while keeping the delivery content-rich rather
    than padded or breathless. Write in a crisp,
    declarative commentary style: concrete source-backed facts, direct
    action verbs, minimal filler, and steady forward momentum. Favor
    several short sentences over one long compound sentence — short
    sentences read faster, hit harder, and give the narration a pulse
    instead of a monotone. When the beat has a reversal, expectation, or twist,
    write the setup and the payoff as distinct sentences rather than
    folding both into one clause. State a character's emotional
    reaction as its own short beat where the source material supports
    it. no name guessing when unsure — but when the transcript
    provides clear name evidence for a character, using that name is
    NOT a guess: use the real name, consistently, in every cut. Use
    the SAME label (real name, or a descriptor only when no name
    evidence exists) across every cut for the same character. Describe
    what is visually happening, not just what is said.
  - `keywords` is optional and belongs inside an individual cut. When you
    use it, select only words in that cut's `voiceover_text` that are truly
    noteworthy to a viewer, then assign each one a literal `#RRGGBB` color
    that fits its emotional weight. You make both editorial decisions: which
    words deserve emphasis and which exact color they receive. Use
    `[{{"word": "exact script word", "color": "#RRGGBB"}}]` (or omit the
    field). Never emit a `tone`, sentiment label, color family, or other
    abstract classification; ClipForge applies literal colors and performs no
    tone-to-color interpretation.
  - Across cuts, `voiceover_text` fields must read as consecutive scenes
    of ONE continuous story, not as independent highlight descriptions.
    Where there is a real gap between adjacent cuts (location change,
    time skip, unexplained new state, setup→payoff), open the later
    cut's `voiceover_text` with a short connective lead-in that carries
    the viewer across the gap. Never invent events to fill a gap.{constraints_focus_note}
  - Return ONLY the JSON, no surrounding prose, no code fences. It will
    be uploaded verbatim to ClipForge Stage B.

================================================================================
"""


def hms(sec: int) -> str:
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h:d}h{m:02d}m{s:02d}s"
    return f"{m:d}m{s:02d}s"


def _resolve_focus(args) -> str:
    """
    Resolve the focus string from either --focus (literal argv) or
    --focus-env (env var name). Env-var form is preferred by stage-a.yml
    because a free-form user string is safer piped through the
    environment than through argv. Returns "" when no focus is set /
    only whitespace was given.
    """
    raw = ""
    if args.focus_env:
        raw = os.environ.get(args.focus_env, "") or ""
    elif args.focus is not None:
        raw = args.focus
    return (raw or "").strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("duration_seconds", type=int)
    ap.add_argument("total_frames", type=int)
    ap.add_argument("output_txt")
    ap.add_argument("--target-duration", type=int, default=120)
    ap.add_argument("--job-id", default="(unknown)")
    ap.add_argument(
        "--window-seconds",
        type=int,
        default=6,
        help="Seconds of source video covered by each baseline composite "
             "JPEG (must match the ffmpeg extraction step in stage-a.yml).",
    )
    ap.add_argument("--shot-count", type=int, default=0)
    ap.add_argument("--key-moment-count", type=int, default=0)
    ap.add_argument("--event-frame-count", type=int, default=0)
    ap.add_argument(
        "--focus",
        default=None,
        help="Optional literal focus string. Narrows the entire analysis "
             "to one specific scene/moment/plot thread in the source video. "
             "Prefer --focus-env for values coming from workflow_dispatch "
             "so we don't have to shell-quote arbitrary user text on argv.",
    )
    ap.add_argument(
        "--focus-env",
        default=None,
        help="Name of the environment variable that carries the focus string.",
    )
    ap.add_argument("--series-part", type=int, default=None)
    ap.add_argument("--series-start-seconds", type=int, default=None)
    ap.add_argument("--series-context-env", default=None)
    args = ap.parse_args()

    window_seconds = max(int(args.window_seconds), 1)
    total_frames = max(int(args.total_frames), 0)
    last_window_start = max(total_frames - 1, 0) * window_seconds

    example_t = 272
    example_div = example_t // window_seconds
    example_window = example_div * window_seconds
    example_window_end = example_window + window_seconds

    focus_text = _resolve_focus(args)
    has_focus = bool(focus_text)
    series_enabled = args.series_part is not None or args.series_start_seconds is not None
    if series_enabled and (args.series_part is None or args.series_part < 1 or args.series_start_seconds is None or args.series_start_seconds < 0):
        ap.error("Series prompts require a positive part number and a non-negative start timestamp.")
    series_context = (os.environ.get(args.series_context_env, "") if args.series_context_env else "").strip() or "(Part 1: no prior parts.)"
    series_block = SERIES_DIRECTIVE.format(series_part=args.series_part, series_start_seconds=args.series_start_seconds, series_context=series_context) if series_enabled else ""

    # Compose the focus-conditional blocks. Supplied focus preserves its
    # operator-controlled directive. Empty focus gets a distinct directive that
    # requires a transcript + key_moments-grounded, self-selected SINGLE story
    # thread before vision or cut selection; it never falls back to a broad
    # whole-video/highlight-reel interpretation.
    if has_focus:
        focus_block = FOCUS_DIRECTIVE.format(
            focus_text=focus_text,
            target_duration=args.target_duration,
        )
        focus_scope_clause = (
            " — narrowed strictly to the FOCUS stated at the top of this file"
        )
        focus_metadata = focus_text
        step1_focus_note = (
            "\n     FOCUS-MODE ADDITION: while reading, build your story map "
            "specifically around the FOCUS stated at the top of this file. "
            "Note where the focus enters the timeline, where it develops, "
            "and where it resolves. You may skim or skip stretches of the "
            "transcript that clearly belong to unrelated plot threads."
        )
        step2_focus_note = (
            "\n     FOCUS-MODE ADDITION: filter key_moments.json down to only "
            "the moments that belong to the FOCUS. A high-priority moment "
            "outside the focus is out of scope for this run — do NOT pick "
            "it just because its priority is high."
        )
        indexes_focus_note = (
            "\n\n  FOCUS-MODE ADDITION (both indexes):\n"
            "    - When applying the indexes, treat any shot or moment that "
            "does not belong to the FOCUS stated at the top of this file as "
            "out of scope. High shot activity or high `priority` outside "
            "the focus is not a reason to include it."
        )
        cut_step1_focus_note = (
            "\n\n        FOCUS-MODE ADDITION: your candidate cut pool for "
            "this run is not `moments` in general — it is the subset of "
            "`moments` that belongs to the FOCUS stated at the top of "
            "this file. Discard the rest before you start ranking."
        )
        cut_step1_skip_focus_note = (
            "\n          - Belong to any plot thread, subplot, side "
            "character, or running gag OTHER than the FOCUS stated at the "
            "top of this file, even if they are independently strong."
        )
        narration_focus_note = (
            "\n            FOCUS-MODE ADDITION: the \"overall story\" you "
            "narrate is the FOCUS stated at the top of this file, NOT the "
            "whole source video. The arc should trace that focus from its "
            "earliest visible setup in the source to its final payoff."
        )
        constraints_focus_note = (
            "\n  - Every cut in this production.json must serve the FOCUS stated "
            "at the top of the accompanying 00_READ_THIS_FIRST.txt. Cuts "
            "belonging to unrelated plot threads are a violation of this "
            "run's constraint, regardless of how strong they are in "
            "isolation."
        )
    else:
        focus_block = NO_FOCUS_DIRECTIVE.format(
            target_duration=args.target_duration,
        )
        focus_scope_clause = " — after selecting one self-supported story thread first"
        focus_metadata = "(none supplied — agent must self-select one compelling supported story thread)"
        step1_focus_note = (
            "\n     NO-FOCUS ADDITION: before inspecting screenshots, map transcript "
            "candidates and choose one concrete scene/exchange/turning point with "
            "a coherent setup, development, and payoff. This choice becomes the "
            "run's hard throughline."
        )
        step2_focus_note = (
            "\n     NO-FOCUS ADDITION: use `emotional_score`, `priority`, dialogue, "
            "and candidate ranking to compare transcript-supported candidates, then "
            "commit to exactly ONE. Do not collect one high-priority beat from each "
            "subplot or act."
        )
        indexes_focus_note = (
            "\n\n  NO-FOCUS ADDITION (both indexes):\n"
            "    - After choosing one supported thread from transcript + key_moments, "
            "open and use only the shots/moments that build its setup, development, "
            "or payoff. Do not use indexes to assemble a whole-video montage."
        )
        cut_step1_focus_note = (
            "\n\n        NO-FOCUS ADDITION: candidate cuts must be the subset that "
            "serves your one self-selected supported story thread, not the "
            "independently strongest beats across the video."
        )
        cut_step1_skip_focus_note = (
            "\n          - Belong to another subplot, side character, running gag, "
            "or isolated spectacle outside the one self-selected thread, even if "
            "they are independently strong."
        )
        narration_focus_note = (
            "\n            NO-FOCUS ADDITION: narrate only the one self-selected "
            "thread's arc from setup to payoff. A broad recap or evenly sampled "
            "highlight reel is prohibited."
        )
        constraints_focus_note = (
            "\n  - Every cut in this production.json must serve the one self-selected "
            "thread declared at the top of the response. Do not broaden into an "
            "evenly sampled whole-video/episode highlight reel."
        )

    content = TEMPLATE.format(
        series_block=series_block,
        focus_block=focus_block,
        focus_scope_clause=focus_scope_clause,
        focus_metadata=focus_metadata,
        step1_focus_note=step1_focus_note,
        step2_focus_note=step2_focus_note,
        indexes_focus_note=indexes_focus_note,
        cut_step1_focus_note=cut_step1_focus_note,
        cut_step1_skip_focus_note=cut_step1_skip_focus_note,
        narration_focus_note=narration_focus_note,
        constraints_focus_note=constraints_focus_note,
        job_id=args.job_id,
        duration_seconds=args.duration_seconds,
        duration_hms=hms(args.duration_seconds),
        total_frames=total_frames,
        window_seconds=window_seconds,
        two_windows=window_seconds * 2,
        three_windows=window_seconds * 3,
        last_window_start=last_window_start,
        target_duration=args.target_duration,
        example_div=example_div,
        example_window=example_window,
        example_window_end=example_window_end,
        shot_count=args.shot_count,
        key_moment_count=args.key_moment_count,
        event_frame_count=args.event_frame_count,
    )

    os.makedirs(os.path.dirname(os.path.abspath(args.output_txt)) or ".", exist_ok=True)
    with open(args.output_txt, "w", encoding="utf-8") as f:
        f.write(content)
    focus_report = f"focus={focus_text!r}" if has_focus else "focus=<none>"
    print(f"Wrote analysis prompt to {args.output_txt} ({len(content)} bytes; {focus_report})")


if __name__ == "__main__":
    main()
