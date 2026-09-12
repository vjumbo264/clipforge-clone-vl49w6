/**
 * /cancel — abort the current wizard/input flow (§8.2). Never touches task
 * state. kv-minimization phase 5 step 5.8: there is no stored flow to clear
 * (menus/flows are stateless, ARCHITECTURE.md §8.9) — cancelling is purely
 * conversational: acknowledge, then show home.
 *
 * restore-bare-send-recognition amendment: the awaiting_input marker IS a
 * stored (scoped, expiring) input expectation, so /cancel clears it —
 * otherwise a bare send after /cancel would wrongly re-enter the cancelled
 * flow. This is the marker's only conversational-cancellation surface.
 */

import { showHome } from './start.js';
import { sendMessage } from '../telegram.js';
import { deleteAwaitingInput } from '../storage.js';

export async function handleCancel(env, chatId, messageId = null) {
  await deleteAwaitingInput(env, chatId).catch(() => {});
  await sendMessage(env, chatId, 'Cancelled. Nothing was started.');
  return showHome(env, chatId, messageId);
}
