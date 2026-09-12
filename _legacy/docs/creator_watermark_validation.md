# Creator Watermark Validation

## Root cause and correction

The creator watermark’s black rectangle was caused by the **shadow layer**, not by the foreground blend mode. The prior implementation applied `ImageFilter.MaxFilter(19)` to a full-word glyph mask. That morphological dilation expanded neighboring letters until their forms merged into a hard, opaque word-sized block before the foreground text was composited.

The compositor now uses a **2-pixel Gaussian drop shadow** at **74% alpha**, offset five pixels below the text. This preserves the silhouette and separation of individual letters while providing a visible dark shadow. The foreground remains 63% alpha and uses FFmpeg’s `screen` blend with white; an actual render comparison retained Screen because it produces the intended light, semi-transparent text treatment without affecting the shadow’s letter-shape behavior.

## Visual and media checks

The exported shadow layer was inspected independently over a neutral light backing. It shows recognizable separate letterforms for `Fubara`, rather than a filled rectangle. The resulting Stage B-equivalent render was also inspected on dark, bright, and high-detail busy backgrounds with a caption card above the watermark. The mark remains bottom-centered with its safe margin, readable as text with a soft letter-shaped shadow, and does not interfere with captions.

The final media test preserves the 1080×1200 `yuv420p` H.264 video stream and copied AAC audio. A blank creator name remains an explicit no-op: the source file is copied unchanged.

The terminal delivery compression test reduced the six-second validation artifact from 526,393 bytes to 424,763 bytes (19.3%) while retaining the 1080×1200 `yuv420p` H.264 stream and copied AAC audio. A post-compression bright-background frame was inspected and retained the light Screen-blended name with the separate soft letter-shaped shadow; no solid black word box reappeared.
