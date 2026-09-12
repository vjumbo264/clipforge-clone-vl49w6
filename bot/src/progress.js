/**
 * Bug-35: animated progress/liveliness for long-running operations.
 *
 * The stage workflows (stage-a.yml / stage-b.yml / restarts / publish) run
 * for minutes inside GitHub Actions. Bug-26 gave the operator only the
 * initial "starting" acknowledgment; after that the UI went silent until the
 * next status read. This module adds a lightweight liveliness animation on
 * top of that ack WITHOUT needing any push channel from the pipeline.
 *
 * Constraints honoured:
 *  - Cloudflare Workers have no wall-clock timer beyond the request, so we
 *    cannot keep a WebSocket alive and stream edits for minutes.
 *  - Telegram rate-limits editMessageText hard (~1 edit/sec/chat). We
 *    therefore NEVER spam edits. Instead we do ONE bot-side spinner burst
 *    (in `ctx.waitUntil`, so the webhook acks immediately and Telegram
 *    retries are unaffected) covering the first ~15 s, then rely on a
 *    progress-bar line inside the single self-editing status view that the
 *    operator refreshes on demand. Both are far under the rate limit.
 */

import { editMessage, sendMessage } from './telegram.js';
import { escapeHtml } from './constants.js';

// The Worker's ExecutionContext, captured at fetch time, so detached
// animations survive the webhook response via ctx.waitUntil.
let executionCtx = null;
export function setExecutionCtx(ctx) { executionCtx = ctx || null; }

const FRAMES = ['⏳', '⌛'];
const SPIN_TICK_MS = 900;
const SPIN_TICKS = 16; // ~15 s of animation, then the static bar stays.

export function progressBar(fraction, width = 12) {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(fraction) ? fraction : 0));
  const filled = Math.round(clamped * width);
  return '█'.repeat(filled) + '░'.repeat(Math.max(0, width - filled));
}

/** Indeterminate bar for an operation with no measurable percentage. */
export function indeterminateBar(width = 12) {
  return '[' + '▓'.repeat(3).padEnd(width, '·') + ']';
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Animate one status message with a short spinner burst. Runs detached via
 * ctx.waitUntil so the webhook acks immediately (no Telegram retry), and it
 * self-limits to SPIN_TICKS edits — far under Telegram's edit rate limit.
 * Failures (chat deleted, message gone) are swallowed; this is best-effort.
 */
export function animateProgress(env, chatId, text, options = {}) {
  const promise = (async () => {
    let messageId = Number(options.messageId) || null;
    for (let tick = 0; tick < SPIN_TICKS; tick += 1) {
      const frame = FRAMES[tick % FRAMES.length];
      const body = `${frame} ${text}\n\n${indeterminateBar()} <i>working… (${tick + 1}s)</i>`;
      try {
        if (!messageId) {
          const sent = await sendMessage(env, chatId, body);
          messageId = Number(sent && sent.message_id) || null;
        } else {
          await editMessage(env, chatId, messageId, body);
        }
      } catch (error) {
        const msg = String(error && error.message || '');
        // "message is not modified" is benign; anything else stops the loop.
        if (!/message is not modified/i.test(msg)) return messageId;
      }
      await sleep(SPIN_TICK_MS);
    }
    // Settle on a static "dispatched" frame so the chat is never left with a
    // mid-animation glyph once the burst ends.
    try {
      if (messageId) await editMessage(env, chatId, messageId, `⏳ ${text}\n\n${indeterminateBar()} <i>still running — refresh the task for the latest status.</i>`);
    } catch { /* best-effort */ }
    return messageId;
  })();
  const ctx = options.ctx || executionCtx;
  if (ctx && typeof ctx.waitUntil === 'function') {
    ctx.waitUntil(promise);
  }
  return promise;
}

/** One-line progress summary for the persistent task view. */
export function progressLine(status) {
  const state = String(status && status.state || '');
  const map = {
    queued: { frac: 0.05, label: 'Queued — Stage A dispatching' },
    stage_a_running: { frac: 0.25, label: 'Stage A — ingesting source' },
    awaiting_torrent_selection: { frac: 0.4, label: 'Awaiting torrent selection' },
    awaiting_plan: { frac: 0.5, label: 'Awaiting production.json' },
    stage_b_queued: { frac: 0.6, label: 'Stage B queued' },
    stage_b_running: { frac: 0.8, label: 'Stage B — rendering final video' },
    complete: { frac: 1, label: 'Complete' },
  };
  const step = map[state];
  if (!step) return '';
  return `${progressBar(step.frac)} ${Math.round(step.frac * 100)}% — ${escapeHtml(step.label)}`;
}
