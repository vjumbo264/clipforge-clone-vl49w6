/**
 * /settings — the single settings screen (§8.6). Sub-screens and input flows
 * live in index.js; this module owns the top-level summary.
 */

import { getCredentials } from '../storage.js';
import { renderInteractiveView, settingsKeyboard, settingsText } from '../views.js';
import { loadSnapshot } from './start.js';

export async function showSettings(env, chatId, messageId = null) {
  const credentials = await getCredentials(env, chatId);
  const snapshot = await loadSnapshot(credentials);
  return renderInteractiveView(env, chatId, settingsText(snapshot), { replyMarkup: settingsKeyboard() }, messageId);
}

export const handleSettings = showSettings;
