# Automatic Mode Failure Diagnosis

**Evidence source:** GitHub Actions log archive supplied for run `97555232783`, analyzed on 24 August 2026.

## What failed

The five repeated failures are not caused by a missing secret, the Stage A media preparation, the transcript, screenshots, or the Edge TTS voiceover system. The supplied run passed through Stage A artifact generation, release creation, and the initial native Gemini evidence calls. The error sequence occurred only while Automatic Mode was making live Gemini requests.

| Evidence from the supplied run | Meaning |
| --- | --- |
| `GEMINI_API_KEYS` was populated | The Action received the required secret. This was not a missing-key configuration failure. |
| `read_transcript`, `read_scene_index`, `read_key_moments`, and `open_composite` ran | The direct Gemini tool contract and released evidence bundle were working before the provider failure. |
| Key index 0 returned `503 UNAVAILABLE` | Gemini reported temporary high demand. This is a provider-capacity failure, not a permanently invalid key. |
| Key indexes 0, 1, 2, and 3 returned `429 RESOURCE_EXHAUSTED` | The relevant model/project quota or rate-limit buckets were unavailable. The log does not retain the specific RPM, TPM, or daily quota metric. |
| Key index 4 later returned `503 UNAVAILABLE` | Another temporary provider-capacity failure occurred. |
| Final fallback returned `404 NOT_FOUND` for `gemini-2.5-flash` | This was a deterministic code defect: Gemini explicitly said that model is no longer available to new users and directed callers to `gemini-3.6-flash`. |

> The repeatable terminal defect was the routing of a fallback request to retired `gemini-2.5-flash`. That model has now been removed from the local Automatic Mode configuration.

## Corrected code behavior

The local code now uses `gemini-3.7-flash` as the primary route and `gemini-3.6-flash` as the only fallback. The test suite asserts that the retired 2.5 model cannot be reintroduced. The previously prepared bounded retry behavior for temporary `503`, network, and malformed-provider failures remains in the local code; it retries the same request twice before changing routes. It does not blindly retry `429` errors, because a depleted rate/quota bucket requires either a reset window or different provider availability.

## What the code fix can and cannot solve

Removing the retired fallback eliminates the avoidable `404`. It cannot create Gemini capacity during a live `503`, and it cannot replenish a project/model rate or quota bucket that returns `429`.

Because you confirmed that each API key belongs to a different Google project, the repeated `429` responses are not explained by several keys sharing one project quota. Instead, the run shows that multiple individual project/model allocations were unavailable during the same request burst. Open the AI Studio rate-limit view for each project and inspect the active allocation for the models used by Automatic Mode, especially `gemini-3.7-flash` and `gemini-3.6-flash`. Google documents distinct request, input-token, and daily limits, whose values depend on project/model access. [1]

## Immediate next steps

The corrected code cannot reach the live workflow until it is published. The connected GitHub account still receives HTTP 403 when attempting to push to `motionssalt/clipforge`, so the remote workflow is still running the earlier version that lacks the temporary-failure retry and retains the retired fallback. Restore repository write access for the connected account, or push the current local commits from an authorized account. Then retry only after verifying the model-specific quota state for each project.

The failure is therefore a combination of **one code-routing defect** and **live Gemini availability/quota constraints**. It is not an Edge TTS problem, and it is not evidence that all keys are permanently invalid.

## Reference

[1]: https://ai.google.dev/gemini-api/docs/rate-limits "Google Gemini API rate limits"
