#!/usr/bin/env bash
set -euo pipefail

out_dir="assets/tts-previews"
mkdir -p "$out_dir"
text="This is a preview of the selected ClipForge narrator. Clear, natural commentary at a steady pace, ready for your next video."

voices=(
  "en-US-AndrewNeural"
  "en-US-BrianNeural"
  "en-US-ChristopherNeural"
  "en-US-EricNeural"
  "en-US-GuyNeural"
  "en-US-RogerNeural"
  "en-US-AvaNeural"
  "en-US-AriaNeural"
  "en-US-JennyNeural"
  "en-US-MichelleNeural"
)

for voice in "${voices[@]}"; do
  target="$out_dir/$voice.mp3"
  echo "Generating $target"
  edge-tts --voice "$voice" --rate=+20% --volume=+0% --pitch=+0Hz \
    --text "$text" --write-media "$target"
done
