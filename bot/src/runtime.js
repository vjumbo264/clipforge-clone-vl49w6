/**
 * Shared UI runtime for Bot A — helpers used by both the command modules
 * (bot/src/commands/*) and the callback router (bot/src/index.js):
 * error mapping, the credentials gate, and the §8.5 task status screen.
 */

import { GitHubError, readStatus, readProductionPlan, readStageARequest } from './github.js';
import { getCredentials, getJobIdForLabel } from './storage.js';
import { isTerminal } from './jobs.js';
import { buttons } from './telegram.js';
import { escapeHtml, redact, describeTaskState } from './constants.js';
import { ONBOARDING_TEXT, onboardingKeyboard, renderInteractiveView } from './views.js';
import { extractPlanSeries, manualSeriesContinuation, nextPartJobId } from './series.js';
import { progressLine } from './progress.js';
// bug-60: canonical per-state glyph for the task detail header.
import { statusEmoji } from './anim.js';
// bug-62: the completed-task publish affordance reflects the §6.2 publishing
// state instead of always offering a raw Publish button.
import { zernioPublishingSummary, zernioTaskPublishButton } from './zernio.js';

/** Map internal errors to short, secret-free user text (§13 invariant #1). */
export function userError(error) {
  const raw = error && error.message ? error.message : 'The request could not be completed.';
  if (error instanceof GitHubError && error.status === 401) return 'GitHub rejected the stored token. Reconnect it in Settings → GitHub clone.';
  if (error instanceof GitHubError && error.status === 403) return 'GitHub denied this operation. Confirm the token has repository contents, Actions, and workflow permissions.';
  if (error instanceof GitHubError && error.status === 404) return 'GitHub could not find that repository, task, or workflow.';
  return redact(raw).slice(0, 900);
}

/**
 * Credentials gate. Renders the onboarding screen and returns null when the
 * chat is not yet bound to a clone.
 */
export async function requireCredentials(env, chatId, messageId = null) {
  const credentials = await getCredentials(env, chatId);
  if (!credentials || !credentials.githubPat || !credentials.repo) {
    await renderInteractiveView(env, chatId, `${ONBOARDING_TEXT}\n\n<b>Set up your GitHub clone first</b> — it takes about a minute.`, { replyMarkup: onboardingKeyboard() }, messageId);
    return null;
  }
  return credentials;
}

/**
 * next-part-button-hide: shared derivation of the next-part job id that
 * `startNextSeriesPart` (bot/src/index.js) uses when it fires a series
 * continuation. Both the reactive dispatch guard there AND the proactive
 * render-time guard here read the same nextId — if this ever diverges the
 * button gate would silently disagree with the dispatch guard about which
 * job counts as "the next part." Returns null when the current job is not
 * a continuable manual series part.
 */
export function deriveNextPartId(status, request, plan) {
  const continuation = manualSeriesContinuation(status, request, plan);
  if (!continuation) return null;
  try {
    return nextPartJobId(continuation);
  } catch {
    return null;
  }
}

/**
 * next-part-button-hide: async existence probe called from render sites
 * (currently only showTask). Runs ONLY when the completed-manual-series
 * button would otherwise be shown, so tasks that never reach that branch
 * pay no extra GitHub round-trips. Mirrors the reactive guard inside
 * `startNextSeriesPart` (bot/src/index.js ~L1470-1472): if either the
 * next part's stage-a-request or status exists in the repo, the next
 * part has already been dispatched and the button must be hidden. The
 * existence check is live per-render (not cached) so a subsequent
 * `deleteClipforgeJob` — which removes every jobs/<nextId>/* blob from
 * the default branch, see bot/src/github.js — naturally re-reveals the
 * button on the previous part's card without any invalidation step.
 */
export async function nextPartAlreadyExists(credentials, status, request, plan) {
  const state = status && status.state ? String(status.state) : '';
  if (state !== 'complete') return false;
  if (!status || status.mode !== 'manual') return false;
  const series = status.series && typeof status.series === 'object' ? status.series : {};
  if (series.enabled !== true) return false;
  const planSeries = extractPlanSeries(plan);
  if (planSeries.is_final === true || series.is_final === true) return false;
  const nextId = deriveNextPartId(status, request, plan);
  if (!nextId) return false;
  const [existingRequest, existingStatus] = await Promise.all([
    readStageARequest(credentials, credentials.repo, nextId).catch(() => null),
    readStatus(credentials, credentials.repo, nextId).catch(() => null),
  ]);
  return Boolean(existingRequest || existingStatus);
}

/** §8.5 — contextual buttons: only actions valid in the job's current state. */
export function taskKeyboard(status, label, plan = null, nextPartExists = false) {
  const state = status && status.state ? String(status.state) : 'queued';
  const series = status.series && typeof status.series === 'object' ? status.series : {};
  const rows = [];
  if (state === 'awaiting_torrent_selection') {
    rows.push([{ text: '📂 Choose video file', callback_data: `task:tsel:${label}:0` }]);
  }
  if (state === 'awaiting_plan') {
    rows.push([
      { text: '🤖 Get agent prompt', callback_data: `task:prompt:${label}` },
      { text: '⬆️ Upload production.json', callback_data: `task:upload:${label}` }
    ]);
  }
  if (state === 'stage_b_queued' || state === 'stage_b_running') {
    rows.push([{ text: '⛔ Cancel Stage B', callback_data: `task:cancelb:${label}` }]);
  }
  if (state === 'error' || state === 'cancelled') {
    // bug-15: only offer restarts for stages that actually RAN. status.message
    // tells us where the failure happened ("Stage A failed." vs "Stage B
    // failed."); when it is uninformative, any recorded stage progress
    // (awaiting_plan / stage_b_*) implies Stage A completed, so Stage B restart
    // is meaningful. A task that died inside Stage A (Stage B never began) must
    // NOT show a "Restart Stage B" button.
    const message = String((status && status.message) || '');
    const stageBStarted = /stage b/i.test(message);
    const restartRow = [{ text: '↻ Restart Stage A', callback_data: `task:restarta:${label}` }];
    if (stageBStarted) {
      restartRow.push({ text: '↻ Restart Stage B', callback_data: `task:restartb:${label}` });
    }
    rows.push(restartRow);
  }
  if (state === 'complete') {
    rows.push([
      { text: '📥 Download', callback_data: `task:dl:${label}` },
      // bug-62: once a publish already happened (automatically or manually)
      // the raw Publish CTA is replaced with a status-view affordance.
      zernioTaskPublishButton(status.publishing, label)
    ]);
    // bug-48: a completed series part gets the SAME copy-prompt action
    // non-series tasks have, plus a next-part action gated on the PLAN's
    // is_final flag — not the status series block, which pipeline/status.py
    // always normalizes to is_final:false (why the old button never showed).
    if (status.mode === 'manual' && series.enabled === true) {
      const planSeries = extractPlanSeries(plan);
      rows.push([{ text: '📋 Copy prompt', callback_data: `task:prompt:${label}` }]);
      // bug-51: the deploy-blocking test failure. taskKeyboard is also called
      // WITHOUT a plan (unit tests, §8.5 state-validity contract) — there the
      // plan falls back to {} and planSeries.is_final is undefined, which used
      // to make the next-part button render even when the STATUS already said
      // is_final:true. Gate defensively: hide the button when EITHER the plan
      // or the status reports the series is final. In production statuses the
      // status flag is normalized to false, so the plan flag still governs
      // (preserving the bug-48 fix); status-level is_final:true only ever
      // further restricts, never wrongly reveals, the action.
      // next-part-button-hide: the button is additionally suppressed once
      // the next part has already been dispatched (its stage-a-request.json
      // or status.json exists in the repo). nextPartExists is computed by
      // the caller — see nextPartAlreadyExists — so taskKeyboard itself
      // remains synchronous and unit-testable. When nextPartExists is
      // left at its default (false), the previous gate is unchanged, so
      // callers that legitimately can't run the existence probe (e.g. the
      // §8.5 state-validity unit test) keep their pre-fix behavior.
      if (planSeries.is_final !== true && series.is_final !== true && nextPartExists !== true) {
        rows.push([{ text: '▶ Start next part', callback_data: `task:next:${label}` }]);
      }
      rows.push([{ text: '📚 Series parts', callback_data: `task:parts:${label}` }]);
    }
    // bug-22: on-demand delivery — send the finished video into this chat
    // when it fits under Telegram's 50 MB bot limit, otherwise hand back the
    // GitHub release link. Works for single tasks and series Stage B parts.
    rows.push([{ text: '📩 Send video to chat', callback_data: `task:sendvideo:${label}` }]);
  }
  rows.push([{ text: '🔄 Refresh', callback_data: `task:open:${label}` }]);
  // Bug 2 fix: deletion is offered for terminal tasks AND for tasks whose
  // status is unreadable (status == null) — a stuck task must be clearable
  // so its letter is reclaimed.
  // bug-02: from the task detail view we still want a full-page confirmation
  // card (there is no list of rows to toggle inline here), so route through
  // task:delfrom which invokes confirmDeleteTask. task:del is reserved for
  // the tasks-list inline toggle.
  if (isTerminal(state) || !status) rows.push([{ text: '🗑 Delete task', callback_data: `task:delfrom:${label}` }]);
  rows.push([{ text: '← Tasks', callback_data: 'menu:tasks' }]);
  return buttons(rows);
}

/** §8.5 task status screen: state, message, links, contextual actions. */
export async function showTask(env, chatId, label, messageId = null) {
  const credentials = await requireCredentials(env, chatId, messageId);
  if (!credentials) return;
  const jobId = await getJobIdForLabel(env, chatId, label);
  // kv-minimization phase 4: the unseen/seen marker feature is removed —
  // opening a task no longer writes anything to storage.
  if (!jobId) {
    return renderInteractiveView(env, chatId, `Unknown task <b>${escapeHtml(label)}</b>.`, { replyMarkup: buttons([[{ text: '← Tasks', callback_data: 'menu:tasks' }]]) }, messageId);
  }
  // bug-48: read the production plan alongside the status so the completed
  // series affordances can key off the plan's real is_final / summary.
  // next-part-button-hide: the stage-a-request is fetched in the same batch
  // so the render-time next-part existence probe can reuse the SAME
  // (status, request, plan) triple that startNextSeriesPart consumes when
  // it derives the continuation — the two code paths cannot disagree about
  // which job id counts as "the next part."
  const [status, plan, request] = await Promise.all([
    readStatus(credentials, credentials.repo, jobId),
    readProductionPlan(credentials, credentials.repo, jobId).catch(() => null),
    readStageARequest(credentials, credentials.repo, jobId).catch(() => null)
  ]);
  if (!status) {
    return renderInteractiveView(env, chatId,
      `<b>Task ${escapeHtml(label)}</b> · <code>${escapeHtml(jobId)}</code>\n\n<b>Status unavailable</b> — the job record could not be read. It may have failed before Stage A could report in, or it may have expired. Try Refresh, or delete this task if it is stale.`,
      { replyMarkup: buttons([[{ text: '🔄 Refresh', callback_data: `task:open:${label}` }], [{ text: '🗑 Delete task', callback_data: `task:delfrom:${label}` }], [{ text: '← Tasks', callback_data: 'menu:tasks' }]]) },
      messageId);
  }
  const state = String(status.state || 'queued');
  const lines = [
    `<b>Task ${escapeHtml(label)}</b> · <code>${escapeHtml(jobId)}</code>`,
    // bug-60: the canonical state glyph leads the State line so the detail
    // view uses the same visual language as the task list rows.
    `State: ${statusEmoji(state)} <b>${escapeHtml(state)}</b>`,
    // Bug 3 fix: the torrent-selection state reads as an explicit call to
    // action on the task view itself — the operator is told plainly what the
    // job is waiting for and which button resolves it, not just shown a raw
    // state name.
    state === 'awaiting_torrent_selection'
      ? `⏳ <b>${escapeHtml(describeTaskState(status))}</b> — tap <b>📂 Choose video file</b> below and pick the file to process.`
      : '',
    escapeHtml(status.message || ''),
    // bug-35: a live progress bar reflects the current stage so the operator
    // sees forward motion at a glance instead of a bare state name. It
    // updates each time the task view is (re)opened/refreshed.
    progressLine(status),
    // bug-62: surface the §6.2 publishing state on the task view so a
    // completed+published task reads as published (the keyboard affordance
    // above already swaps the raw Publish CTA for a status view).
    status.publishing && String(status.publishing.status || 'not_requested') !== 'not_requested'
      ? zernioPublishingSummary(status.publishing)
      : ''
  ];
  const series = status.series && typeof status.series === 'object' ? status.series : {};
  if (series.enabled === true) lines.push(`Series: part ${Number(series.part) || 1}${series.is_final === true ? ' (final)' : ''}`);
  const linkRows = [];
  const links = [];
  if (status.release_url) links.push({ text: 'Open release', url: status.release_url });
  if (status.run && status.run.workflow_run_url) links.push({ text: 'Workflow run', url: status.run.workflow_run_url });
  if (links.length) linkRows.push(links);
  // next-part-button-hide: only probe next-part existence when the
  // completed-manual-series button would otherwise render at all. The
  // guard inside nextPartAlreadyExists short-circuits on every other
  // state, so tasks that will never show this row pay no extra API call.
  const nextPartExists = await nextPartAlreadyExists(credentials, status, request, plan);
  const keyboard = taskKeyboard(status, label, plan, nextPartExists);
  keyboard.inline_keyboard = [...linkRows, ...keyboard.inline_keyboard];
  return renderInteractiveView(env, chatId, lines.filter(Boolean).join('\n'), { replyMarkup: keyboard }, messageId);
}
