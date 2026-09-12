# MASTER_PROMPT.md — RETIRED

This file used to be the prompt handed to a SECOND external AI agent:
`cut_and_concat.py` bundled it with per-scene `raw_narration` notes into
`output.txt`, and a human pasted that into another AI session to get a
cleaned, chunked narration script back.

That step no longer exists. The analysis agent that writes
`production.json` (see `scripts/generate_analysis_prompt.py`) now writes
the FINAL, ready-to-speak voiceover line for each cut directly
(`voiceover_text`), absorbing everything this prompt used to do
(script cleaning, continuity bridging, per-cut chunking). Stage B then
synthesizes that text with Edge TTS (`scripts/generate_voiceover.py`)
and mixes it into the video automatically — no external agent, no manual
copy-paste, no `output.txt`.

The full old prompt text remains in git history if it is ever needed as
reference material for tuning the analysis prompt.
