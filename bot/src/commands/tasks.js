/**
 * /tasks — list active (non-terminal) tasks with their per-chat labels (§6.3,
 * §8.2). Each row opens the task status screen.
 *
 * Bug 1 fix: a task whose status document cannot be read is no longer folded
 * into the active list as a plain "queued" row (indistinguishable from a
 * task that is genuinely still working). An unreadable status now surfaces
 * explicitly as a distinct "status unavailable" row.
 *
 * Bug 2 fix: the list is sorted by actual creation time (most recent first)
 * instead of label order — label order stopped correlating with recency the
 * moment labels became reusable — and every row carries a delete button that
 * routes through the existing confirm-delete flow, so any stale or stuck
 * task can be cleared (and its letter reclaimed) by the operator.
 *
 * bug-02 (fix-sweep): tapping the delete button no longer switches to a
 * separate confirmation view. Instead it edits the row's own label to
 * "Confirm Delete?" in place, in the same list. Tapping the confirm button
 * again on that row actually deletes.
 */

import { readStatus } from '../github.js';
import { taskLabels } from '../storage.js';
import { buttons } from '../telegram.js';
import { escapeHtml, describeTaskState } from '../constants.js';
import { requireCredentials } from '../runtime.js';
import { renderInteractiveView } from '../views.js';
// bug-60: canonical per-state glyphs + the shared indeterminate bar so the
// list reads as part of the bot-wide animation design language.
import { isActiveState, statusEmoji } from '../anim.js';
import { indeterminateBar } from '../progress.js';

const MAX_LISTED = 15;

/** Sort a loaded task list by creation time, most recent first. */
export function sortTaskEntries(entries) {
  return entries.slice().sort((a, b) => {
    const aCreated = Number(a.status && a.status.created_at_epoch) || 0;
    const bCreated = Number(b.status && b.status.created_at_epoch) || 0;
    if (aCreated !== bCreated) return bCreated - aCreated; // newest first
    return a.label < b.label ? -1 : a.label > b.label ? 1 : 0;
  });
}

/** Load statuses for the chat's labels; returns sorted [{ label, jobId, status }]. */
export async function loadTaskList(env, chatId) {
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return { credentials: null, entries: [] };
  const labels = (await taskLabels(env, chatId)).slice(0, MAX_LISTED);
  const entries = await Promise.all(labels.map(async ({ label, jobId }) => {
    const status = await readStatus(credentials, credentials.repo, jobId).catch(() => null);
    // kv-minimization phase 4: the 🆕 unseen marker is gone — no per-task
    // options are read for list rendering at all anymore.
    return { label, jobId, status };
  }));
  return { credentials, entries: sortTaskEntries(entries) };
}

/**
 * Render the active-task list. `pendingDeleteLabel` marks a single row as
 * awaiting delete confirmation: its label reads "Confirm Delete?" and its
 * trash button becomes the executing confirm button (bug-02).
 */
export async function showTasks(env, chatId, messageId = null, pendingDeleteLabel = '') {
  const { credentials, entries } = await loadTaskList(env, chatId);
  if (!credentials) return;
  // A task is "active" unless we can positively read a terminal state. An
  // unreadable status is NOT silently treated as "queued" (pre-fix behavior):
  // it is listed with an explicit unavailable marker so a job that died
  // before persisting its error status is visible to the operator.
  // bug-07 fix: terminal tasks (complete / error / cancelled) stay visible
  // in the task list with their terminal status instead of vanishing.
  // Pre-fix this filter dropped them from /tasks entirely, so a task that
  // finished or errored disappeared from the operator's main list and was
  // only reachable via /done.
  const active = entries;
  const lines = ['<b>Tasks</b>'];
  const rows = [];
  // bug-60: when any job is actively working, a live hint line carries the
  // shared indeterminate bar so the list signals forward motion at a glance
  // (and the per-task view's progress bar animates on every open/refresh).
  const anyActive = active.some((entry) => entry.status && isActiveState(entry.status.state));
  if (anyActive) lines.push(`${indeterminateBar()} <i>working — open a task for its live progress</i>`);
  if (!active.length) {
    lines.push('No tasks yet. Start one with 🎬 New video.');
  } else {
    for (const entry of active) {
      const unreadable = !entry.status;
      const stateText = describeTaskState(entry.status, { unreadable });
      const termState = entry.status ? String(entry.status.state) : '';
      // bug-60: the canonical statusEmoji set replaces the three hand-picked
      // terminal marks, so active states now show a distinct glyph too
      // (⚙️ stage A, 🎬 stage B, 📂 awaiting file, …) instead of nothing.
      const stateMark = `${statusEmoji(termState, { unreadable })} `;
      const marker = stateMark;
      const isPendingDelete = pendingDeleteLabel && entry.label === pendingDeleteLabel;
      if (isPendingDelete) {
        lines.push(`<b>${marker}${escapeHtml(entry.label)}</b> — <b>Confirm Delete?</b>`);
        rows.push([
          { text: `⚠️ ${entry.label} — Confirm Delete?`, callback_data: `task:delconfirm:${entry.label}` },
          { text: `✖ Cancel`, callback_data: `menu:tasks` }
        ]);
      } else {
        lines.push(`<b>${marker}${escapeHtml(entry.label)}</b> — ${escapeHtml(stateText)}`);
        rows.push([
          { text: `${marker}Open ${entry.label} · ${stateText}`, callback_data: `task:open:${entry.label}` },
          { text: `🗑 ${entry.label}`, callback_data: `task:del:${entry.label}` }
        ]);
      }
    }
  }
  rows.push([{ text: '← Menu', callback_data: 'menu:home' }]);
  return renderInteractiveView(env, chatId, lines.join('\n'), { replyMarkup: buttons(rows) }, messageId);
}

export const handleTasks = showTasks;
