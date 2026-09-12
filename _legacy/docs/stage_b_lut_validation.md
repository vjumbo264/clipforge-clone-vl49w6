# Stage B LUT Validation

**Date:** 22 August 2026  
**Scope:** Replacement of the Stage B color LUT only. The test ran the existing `scripts/enhance_scenes.py` processing path, including its existing denoise, sharpening, debanding, encoding, and audio-preservation behavior.

## Test method

The supplied 512×512 8×8 tiled 64³ LUT grid was retained at `assets/anime_reference_lut_grid.png` and deterministically converted into the FFmpeg Hald level-8 lookup image used by `haldclut`. The test generated a short H.264/AAC video from each public animation frame, copied it to `work/out/final.mp4`, and ran the exact Stage B enhancement command.

| Source frame | Public source | Purpose |
| --- | --- | --- |
| Big Buck Bunny outdoor frame | [Big Buck Bunny Gallery][1] | Validate bright skies, foliage, white character surfaces, edge detail, and highlight preservation. |
| Sintel character frame | [Sintel Open Movie][2] | Validate a human character’s face and hands, warm lighting, deep shadows, and fine facial/creature detail. |

## Measured results

| Test | Mean absolute RGB change | Edge-energy ratio | Highlight clipping, before → after | Shadow clipping, before → after | Visual result |
| --- | ---: | ---: | ---: | ---: | --- |
| Big Buck Bunny | 18.622 | 2.331 | 0.165% → 0.146% | 0.000% → 0.031% | The transform creates a distinct but coherent cyan/teal environmental grade. Cloud highlights remain controlled, the white character remains neutral, and foliage/ink-edge detail remains clear. |
| Sintel | 9.939 | 1.672 | 0.000% → 0.000% | 21.347% → 0.287% | The character’s face and hands remain warm and recognizable, dark scene detail becomes more usable, and the grade remains intentional rather than washed out or blurred. |

The visual comparisons were inspected side by side. Both outputs retained usable highlights and shadows, showed a meaningful color transformation, and preserved detail rather than introducing destructive blur. The required FFmpeg command log explicitly contained `haldclut=shortest=1` and showed the supplied PNG as the second filter input.

[1]: https://www.bigbuckbunny.org/ "Big Buck Bunny"
[2]: https://studio.blender.org/films/sintel/ "Sintel — Blender Studio"
