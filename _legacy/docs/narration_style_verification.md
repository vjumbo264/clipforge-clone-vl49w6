# Narration Style Verification

The tightened prompt was rendered by `scripts/generate_analysis_prompt.py` and exercised by `scripts/test_generate_analysis_prompt.py`. A separate live model run then produced `work/narration_style_sample.json` using the same production JSON voiceover contract: short declarative sentences, concrete visible facts, direct actions, and an explicit payoff.

> Nia slips into the abandoned observatory. She grabs a data drive from a console. A ceiling alarm screams and the exit seals. She spots a rusted maintenance cable. She climbs into the rafters and threads the cable across. She detaches the lock mechanism and pulls it open. The floor collapses below as she reaches the opposite platform. She catches herself. She clamps the drive to her chest and opens the restored door.

The live sample contains 72 words for a 42-second cut, leaving normal room for delivery. It establishes the situation, advances one visible beat at a time, names concrete objects and consequences, and gives the surprise its own short sentence. This verifies that production JSON narration authored under the new contract follows the intended tighter commentary pattern without borrowing material from the supplied reference examples.
