# Stage B playback fix — root cause and what changed

> **Note (current pipeline):** this is a historical bug-fix log. The
> scripts it names — `scripts/cut_scenes.py` and
> `scripts/cut_and_concat.py` — no longer exist in the repo, and Stage B
> no longer produces per-scene `scene_XX.mp4` files. The encode-flags
> knowledge documented below (the `-map` / `-sn -dn` / `-bf 0` /
> `force-cfr` / `-video_track_timescale` policy and the strict
> 1-video + 1-audio validation bar) now lives in
> `scripts/cut_and_produce.py`, which produces the single merged
> `final.mp4`. Read this file for the *why*; edit `cut_and_produce.py`
> for the *how*.

## Symptom
scene_XX.mp4 files from Stage B would not play properly on phones OR on PC.

## Root cause (confirmed on the real output, not a guess)
The previous "always re-encode to H.264/AAC" fix WAS in place, but the ffmpeg
command in scripts/cut_scenes.py still produced a non-portable MP4. A real
broken output (scene_01.mp4 from the 2026-08-06 job, source = an x265/HEVC
10-bit MKV with an embedded subtitle) showed:

1. THREE streams: h264 + aac + a third `bin_data`/`text` "SubtitleHandler"
   stream. ffmpeg's DEFAULT stream mapping copied the source's embedded
   subtitle into the MP4. Strict players (phones, WhatsApp, many PC players)
   reject/choke on an MP4 carrying an extra data track.
2. `has_b_frames=2` and `start_time=0.066667` (video PTS offset of 2 frames
   vs DTS 0). That CTS offset desyncs A/V at the head of every cut and breaks
   strict decoders.
3. `-x264-params nal-hrd=cbr:force-cfr=1` wrote a CBR HRD timing model into
   the bitstream while running pure CRF with NO VBV (-maxrate/-bufsize) —
   an inconsistent combination.

## Fix (scripts/cut_scenes.py; same encode flags mirrored in scripts/cut_and_concat.py)
- `-map 0:v:0 -map 0:a:0`  -> only the primary video + audio are muxed.
- `-sn -dn -ignore_unknown -map_metadata -1 -map_chapters -1` -> no subtitle/
  data/chapter track and no inherited global metadata reach the output.
- `-bf 0`                  -> no B-frames, so PTS==DTS and start_time==0.
- `-x264-params force-cfr=1` (dropped `nal-hrd=cbr`).
- `-video_track_timescale 15360` for a clean phone-friendly timebase.
- Video-only sources now get a synthesized silent stereo AAC track (via lavfi
  anullsrc + `-shortest`) so every scene is always a valid A/V MP4.

## Validation hardened
scripts/cut_scenes.py validate_mp4() now hard-fails (exit 3) if a scene has
anything other than EXACTLY 1 video + 1 audio stream, if video has_b_frames!=0,
or if container/video start_time > 0.05s — so a bad scene can never ship.

Verified end-to-end: subtitle-bearing MKV and video-only sources both produce
2-stream, has_b_frames=0, start_time=0, High@L4.0 yuv420p + AAC-LC MP4s that
decode cleanly.
