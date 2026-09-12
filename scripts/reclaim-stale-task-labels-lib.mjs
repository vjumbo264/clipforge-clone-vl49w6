/**
 * Pure decision logic for scripts/reclaim-stale-task-labels.mjs, extracted so
 * it can be unit-tested (bot/test/reclaim-stale-task-labels.test.mjs) without
 * executing the CLI's top-level env checks, KV reads, or GitHub API calls.
 *
 * classify() decides whether a labelled job's task label may be reclaimed:
 *   'stale:<reason>'  — label is freed
 *   'active'          — label is kept
 *   'unknown:<error>' — label is skipped (undecidable, kept to be safe)
 *
 * Terminal states (complete/error/cancelled) and the series guard mirror
 * pipeline/cleanup/expired.py — see series_is_complete() there.
 */

export const TERMINAL_STATES = ['complete', 'error', 'cancelled'];

// Inactivity grace for the series guard, mirroring expired.py's
// DEFAULT_SERIES_GRACE_SECONDS: a fully-terminal series with no is_final
// part (bug-66) keeps its labels only while its newest part's own expiry is
// less than this far in the past. Without it, an abandoned series pins its
// labels forever — the operator's 'tasks never expire' report.
export const DEFAULT_SERIES_GRACE_SECONDS = 24 * 3600;

/**
 * bug-49 + bug-66 + bug-67: build a seriesIncomplete(seriesId) predicate over
 * a caller-supplied job lister. A series is INCOMPLETE (parts must keep their
 * labels) unless BOTH:
 *   (a) every known part is in a terminal state, AND
 *   (b) at least one part is marked series.is_final === true.
 * Rule (b) is the bug-66 rule: a series whose existing parts all finished but
 * none is marked final has not actually finished producing parts (the next
 * part simply hasn't been started/rendered yet), so it is INCOMPLETE.
 * A part with no readable status counts as INCOMPLETE (mirrors expired.py's
 * "missing/unreadable sibling status counts as INCOMPLETE"), so a protected
 * part is never reaped while any sibling is unknown.
 *
 * listJobDocs: async () => iterable of { jobId, doc|null } — doc is the parsed
 * status.json, or null when it is missing/unreadable.
 */
export function makeSeriesIncomplete(listJobDocs, { now = Math.floor(Date.now() / 1000), grace = DEFAULT_SERIES_GRACE_SECONDS } = {}) {
  const cache = new Map();
  return async function seriesIncomplete(seriesId) {
    const id = String(seriesId || '');
    if (!id) return false;
    if (cache.has(id)) return cache.get(id);
    let incomplete = false;
    try {
      let sawFinal = false;
      let lastActivity = 0;
      for (const item of await listJobDocs()) {
        if (!item || !item.doc) continue; // unreadable sibling: not evidence of completion
        const doc = item.doc;
        const ser = doc.series && typeof doc.series === 'object' ? doc.series : {};
        if (String(ser.series_id || '') !== id) continue;
        if (!TERMINAL_STATES.includes(String(doc.state))) { incomplete = true; break; }
        // bug-66: an unreadable/ambiguous is_final flag simply doesn't count;
        // only an explicit true marks the series as having produced its last part.
        if (ser.is_final === true) sawFinal = true;
        // Series activity signal (mirrors expired.py): newest part timing.
        for (const k of ['created_at_epoch', 'expires_at_epoch']) {
          const v = Number(doc[k]);
          if (Number.isFinite(v) && v > lastActivity) lastActivity = v;
        }
      }
      // bug-66: zero-final series is not finished — UNLESS (stuck-series
      // grace) every part is terminal and the newest part's timing is at
      // least `grace` seconds in the past. A live series always has a part
      // whose expiry is recent, so the grace never reaps anything active.
      if (!incomplete && !sawFinal && !(lastActivity > 0 && lastActivity + grace <= now)) incomplete = true;
    } catch { incomplete = false; }
    cache.set(id, incomplete);
    return incomplete;
  };
}

/**
 * bug-67: the series guard MUST run before the terminal-state short-circuit.
 * The old ordering returned `stale:terminal-*` for any terminal job first,
 * making the bug-49 series protection unreachable dead code for exactly the
 * case it exists for — a finished non-final part of a still-incomplete series
 * (job manual-1788023189426: state=complete, part 1, is_final=false, part 2
 * never started; its label was reclaimed while the series was still active).
 *
 * Order:
 *   1. no readable status        -> stale (mirrors expired.py)
 *   2. fetch/transport error     -> unknown (skipped, label kept to be safe)
 *   3. enabled, incomplete series -> active (regardless of this part's state)
 *   4. terminal state            -> stale
 *   5. expires_at_epoch in past  -> stale
 *   6. otherwise                 -> active
 */
export async function classify(result, { now, seriesIncomplete }) {
  // Mirrors pipeline.cleanup.expired: no readable status => expired.
  if (result.missing || result.error === 'unparseable') return 'stale:no-readable-status';
  if (result.error) return `unknown:${result.error}`;
  const doc = result.doc || {};
  const ser = doc.series && typeof doc.series === 'object' ? doc.series : {};
  if (ser.enabled === true && String(ser.series_id || '') && await seriesIncomplete(String(ser.series_id))) {
    return 'active'; // bug-49/bug-67: series still running — keep the label.
  }
  if (TERMINAL_STATES.includes(String(doc.state))) return `stale:terminal-${doc.state}`;
  const expires = Number(doc.expires_at_epoch);
  if (Number.isFinite(expires) && expires > 0 && expires < now) return 'stale:ttl-expired';
  return 'active';
}
