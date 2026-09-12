# Subtitle font

`BebasNeue-Regular.ttf` — **Bebas Neue**, the condensed all-caps display
face used for the burned-in word-by-word subtitles in
`scripts/generate_subtitles.py`.

- Source: google/fonts repository (`ofl/bebasneue/BebasNeue-Regular.ttf`)
- Author: Ryoichi Tsunekawa / Dharma Type
- License: SIL Open Font License 1.1 (OFL) — free for commercial use,
  embedding, and redistribution; the font is vendored here so subtitle
  rendering is identical on every GitHub Actions runner regardless of
  what system fonts are installed. OFL 1.1 full text:
  https://openfontlicense.org/

The file is passed to ffmpeg's libass `subtitles=` filter via its
`fontsdir` option; the ASS style references the family name
`Bebas Neue`.

# Title-banner font (cinematic mode)

`Coolvetica.ttf` — **Coolvetica** Regular, the display face used for
the one-time intro title banner in cinematic mode
(`scripts/generate_subtitles_cinematic.py`). It replaces the narrow
template-mode title face WITHIN the cinematic banner only; template
mode's own font handling is untouched.

- Source: coolvetica by Typodermic Fonts (free-for-commercial-use
  release), vendored so banner rendering is identical on every GitHub
  Actions runner with no external fetch. Upstream:
  https://typodermicfonts.com/coolvetica/
- Family name in the file: `Coolvetica` (Regular).

The banner script renders the title into a white banner PNG with
Pillow using this file directly (`ImageFont.truetype`); if the file
is ever missing it falls back to DejaVu Sans Bold with a warning
rather than failing the render.
