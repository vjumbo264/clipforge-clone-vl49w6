/**
 * bug-60: shared animation / liveliness helpers for the whole bot UI.
 *
 * Prior sweeps fixed isolated indicators (bug-26 stage-dispatch ack, bug-35
 * the persistent task progress bar, bug-42 the plan-upload receiving
 * indicator). This module lifts the *pattern* into one reusable design
 * language so every screen animates the same way instead of hand-rolling
 * ad-hoc spinners per handler:
 *
 *   • pulseFrames      — rotating activity glyph (⏳ ⌛ 🔄 ✨) for headers
 *   • loadingFrames    — ". .. ..." trailing-dot beats for a quiet "working"
 *   • dotsBar          — animated indeterminate bar with a moving window
 *   • statusEmoji      — one canonical glyph per job state
 *   • pulseTaskLines   — decorate task-list rows with a rotating pulse
 *   • toggleFeedback   — instant ✓/✗ on/off echo for settings toggles
 *   • saveConfirmFlash — prepend a "✅ Saved" flash line to a rendered screen
 *   • flashThenView    — show a transient beat, then settle on the real view
 *
 * Rate-limit contract (hard): every helper here performs AT MOST TWO message
 * writes — one transient beat and one final settle. No helper loops edits.
 * The only multi-edit animator in the codebase remains the self-limiting
 * spinner burst in progress.js (bug-35), which is unchanged. All edits route
 * through views.renderInteractiveView, so Telegram's ~1 edit/sec/chat limit
 * is respected even under concurrent users.
 *
 * This module is IMPORT-LIGHT on purpose: it must not pull in views.js /
 * storage.js (circular). Only pure helpers live here plus the Telegram client.
 */

import { editMessage, sendMessage } from './telegram.js';

/** Rotating activity glyphs for headers while work is in flight. */
export const PULSE_GLYPHS = ['⏳', '⌛', '🔄', '✨'];

/** Return the pulse glyph for a beat index (monotonic tick). */
export function pulseFrames(tick = 0) {
  const i = Math.abs(Math.trunc(Number(tick) || 0)) % PULSE_GLYPHS.length;
  return PULSE_GLYPHS[i];
}

/**
 * Trailing-dot loading beats: beat 0 -> "Working", 1 -> "Working.", 2 ->
 * "Working..", 3 -> "Working...". Quiet, readable, and under the edit limit
 * because callers show exactly one beat before settling.
 */
export function loadingFrames(label = 'Working', beat = 0) {
  const dots = ['', '.', '..', '...'][Math.abs(Math.trunc(Number(beat) || 0)) % 4];
  return `${label}${dots}`;
}

/**
 * Animated indeterminate bar with a moving filled window. Unlike the static
 * indeterminateBar() in progress.js, this takes a frame index so a transient
 * beat looks alive; the window wraps around the track.
 */
export function dotsBar(frame = 0, width = 12, window = 3) {
  const w = Math.max(4, Math.trunc(Number(width) || 12));
  const win = Math.max(1, Math.min(Math.trunc(Number(window) || 3), w));
  const span = w - win + 1;
  const start = Math.abs(Math.trunc(Number(frame) || 0)) % span;
  return '▏' +
    '·'.repeat(start) +
    '▓'.repeat(win) +
    '·'.repeat(Math.max(0, w - start - win)) +
    '▕';
}

/**
 * One canonical status glyph per job state — used consistently by the task
 * list, the task detail header, and the completed list so the bot reads as a
 * single design language.
 */
export function statusEmoji(state, { unreadable = false } = {}) {
  if (unreadable) return '❔';
  switch (String(state || '')) {
    case 'complete': return '✅';
    case 'error': return '⚠️';
    case 'cancelled': return '⛔';
    case 'queued': return '🕓';
    case 'awaiting_torrent_selection': return '📂';
    case 'awaiting_plan': return '📝';
    case 'stage_a_running': return '⚙️';
    case 'stage_b_queued': return '🕓';
    case 'stage_b_running': return '🎬';
    default: return '⏳';
  }
}

/** True when the job state represents active (non-terminal) work. */
export function isActiveState(state) {
  return ['queued', 'stage_a_running', 'awaiting_torrent_selection',
    'awaiting_plan', 'stage_b_queued',
    'stage_b_running'].includes(String(state || ''));
}

/**
 * Decorate a task-list row's text prefix with a rotating pulse for active
 * jobs, or the canonical status glyph for terminal/unreadable ones. Purely
 * presentational: returns a short string to prepend to the row label.
 */
export function pulseTaskLines(entry, beat = 0) {
  const state = entry && entry.status ? String(entry.status.state) : '';
  if (!entry || !entry.status) return statusEmoji('', { unreadable: true });
  if (isActiveState(state)) return pulseFrames(beat);
  return statusEmoji(state);
}

/**
 * Instant feedback text for a boolean settings toggle the user just flipped.
 * Reads as a one-line confirmation that can be prepended to the re-rendered
 * settings screen (see saveConfirmFlash).
 */
export function toggleFeedback(label, enabled) {
  const mark = enabled ? '✓' : '✗';
  const word = enabled ? 'on' : 'off';
  return `${mark} ${label} turned ${word}`;
}

/**
 * Prepend a transient "✅ Saved — <detail>" flash line to a rendered screen's
 * body. `detail` is usually a toggleFeedback() line or a short save summary.
 * Returns the decorated body text.
 */
export function saveConfirmFlash(body, detail = '') {
  const flash = detail ? `✅ <b>Saved</b> — ${detail}` : '✅ <b>Saved</b>';
  return `${flash}\n\n${body}`;
}

/**
 * Show a one-beat transient loading frame on the active view, then hand off
 * to the caller's real render. This is the shared "brief ... before resolving
 * to final content" pattern: exactly ONE edit for the beat, then the caller's
 * own render settles the view (a second write). Never loops.
 *
 * `renderFinal` is an async function (messageId) => result that renders the
 * resolved screen via renderInteractiveView and returns its result. The beat
 * uses renderInteractiveView so it targets the chat's single active view and
 * the final render edits the SAME message in place.
 *
 * Failures of the beat are swallowed — the final render is what matters.
 */
export async function flashThenView(views, env, chatId, messageId, beatText, renderFinal) {
  try {
    const sent = await views.renderInteractiveView(env, chatId, beatText, {}, messageId);
    const id = Number(messageId || (sent && sent.message_id)) || null;
    return await renderFinal(id);
  } catch (error) {
    // If even the beat failed, still try the final render on the original id.
    return renderFinal(messageId);
  }
}
