/**
 * /new — start the New-video wizard (§8.4). Owns the wizard step renderer,
 * which the callback router in index.js reuses for every wizard transition.
 *
 * kv-minimization phase 5: the wizard record no longer lives in a stored
 * state.pending.wizard. It travels INSIDE the wizard's own prompt/keyboard
 * message as the invisible `wzs` flow marker (ARCHITECTURE.md §8.9):
 * button callbacks re-read it from callback.message.text, and free-text
 * answers arrive as force_reply replies whose reply_to_message carries it.
 * No chat-state record is written for navigation.
 */

import { buttons, sendForceReply } from '../telegram.js';
import { requireCredentials } from '../runtime.js';
import { renderInteractiveView } from '../views.js';
import { makePayload, withFlowMarker } from '../flow.js';
import { encodeWizardToken, newWizard, stepPrompt } from '../wizard.js';
import { readSeriesSettings } from '../github.js';

const EXPIRED_TEXT = 'That wizard has expired. Start again with /new.';

/** The fallback card when a marker-less (very old) wizard message is used. */
export function renderWizardExpired(env, chatId, messageId = null) {
  return renderInteractiveView(env, chatId, EXPIRED_TEXT, {
    replyMarkup: buttons([[{ text: '🎬 New video', callback_data: 'menu:new' }]])
  }, messageId);
}

/**
 * Render one wizard step. The wizard record is embedded in the message as
 * the `wzs` marker; text steps (source/focus) additionally go out as a
 * force_reply prompt so the answer arrives as a reply to THIS message.
 */
export async function renderWizardStep(env, chatId, wizard, messageId = null, extra = '') {
  if (!wizard) return renderWizardExpired(env, chatId, messageId);
  const prompt = stepPrompt(wizard);
  const text = withFlowMarker(extra ? `${prompt.text}\n\n${extra}` : prompt.text, makePayload('wzs', encodeWizardToken(wizard)));
  if (wizard.step === 'source' || wizard.step === 'focus') {
    // Free-text steps: the reply edge to THIS prompt is how the next update
    // is matched back to the wizard — no stored pending record exists.
    return sendForceReply(env, chatId, text, { inlineRows: prompt.keyboard });
  }
  return renderInteractiveView(env, chatId, text, { replyMarkup: buttons(prompt.keyboard) }, messageId);
}

/** /new — always starts a fresh wizard (§8.1: one way to make a video). */
export async function handleNew(env, chatId, messageId = null) {
  const credentials = await requireCredentials(env, chatId, messageId);
  if (!credentials) return;
  // bug-50: the wizard inherits the Settings series default — no per-task toggle.
  const seriesSettings = await readSeriesSettings(credentials, credentials.repo).catch(() => null);
  const wizard = newWizard();
  wizard.series = Boolean(seriesSettings && seriesSettings.enabled === true);
  // Nothing is persisted: the fresh wizard lives only in the prompt's marker.
  return renderWizardStep(env, chatId, wizard, messageId);
}
