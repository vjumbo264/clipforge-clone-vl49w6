/**
 * ClipForge job state machine helpers (bot side).
 *
 * The bot reads and writes ``jobs/<job_id>/status.json`` via the GitHub API to
 * drive its own UI, and to detect terminal states before offering actions. The
 * *canonical* status writer is Python (``pipeline/status.py``, run inside
 * GitHub Actions). These JS helpers exist so Bot A can construct valid
 * records for the first write, merge updates, and query the state machine
 * without duplicating the constants.
 *
 * See ARCHITECTURE.md §6.1 (states) and §6.2 (schema).
 */

'use strict';

export const STATUS_VERSION = 2;

export const VALID_STATES = Object.freeze([
  'queued',
  'stage_a_running',
  'awaiting_torrent_selection',
  'awaiting_plan',
  'stage_b_queued',
  'stage_b_running',
  'complete',
  'error',
  'cancelled',
]);

export const TERMINAL_STATES = Object.freeze(new Set(['complete', 'error', 'cancelled']));

export const VALID_MODES = Object.freeze(['manual']);

export const VALID_PUBLISHING_STATUSES = Object.freeze([
  'not_requested',
  'publishing',
  'scheduled',
  'published',
  'partial',
  'failed',
  'cancelled',
]);

// FALLBACK ONLY (12h), mirroring pipeline/status.py's DEFAULT_TTL_SECONDS.
// The operator-facing job TTL is configured in ONE place — the repo Actions
// variable CLIPFORGE_TTL_SECONDS (currently 172800 = 48h) — and mirrored into
// this worker via the CLIPFORGE_JOB_TTL_SECONDS var in bot/wrangler.*.jsonc
// (see configuredJobTtlSeconds below). ALL of these must agree with
// .github/workflows/stage-a.yml's and .github/workflows/cleanup.yml's
// CLIPFORGE_TTL_SECONDS env — drift between the job-creation TTL (here) and
// the cleanup sweep TTL was the root cause of the operator's "48h not taking
// effect" report (TTL_FIX_PROGRESS.json, fix #1). This constant is only the
// last-resort default when the var is unset (e.g. unit tests).
export const DEFAULT_TTL_SECONDS = 12 * 3600;

/**
 * The TTL for a NEWLY created job's status record, in seconds. Reads the
 * worker env var CLIPFORGE_JOB_TTL_SECONDS (set in bot/wrangler.*.jsonc,
 * mirroring the repo Actions variable CLIPFORGE_TTL_SECONDS); falls back to
 * DEFAULT_TTL_SECONDS when unset/unparsable/non-positive — a bad value must
 * never crash job creation.
 */
export function configuredJobTtlSeconds(env) {
  const raw = env && env.CLIPFORGE_JOB_TTL_SECONDS;
  const value = Number.parseInt(String(raw === undefined || raw === null ? '' : raw).trim(), 10);
  return Number.isFinite(value) && value > 0 ? value : DEFAULT_TTL_SECONDS;
}

const JOB_ID_RE = /^[A-Za-z0-9._-]+$/;
const JOB_ID_MAX_LEN = 120;

// Allowed forward transitions in the state machine. Terminal states have no
// outgoing edges. Re-entering the same state is always allowed (idempotent
// writes). Torrent selection funnels back into stage_a_running.
const TRANSITIONS = Object.freeze({
  queued: ['stage_a_running', 'error', 'cancelled'],
  stage_a_running: [
    'awaiting_torrent_selection',
    'awaiting_plan',
    'error',
    'cancelled',
  ],
  awaiting_torrent_selection: ['stage_a_running', 'error', 'cancelled'],
  awaiting_plan: ['stage_b_queued', 'error', 'cancelled'],
  stage_b_queued: ['stage_b_running', 'error', 'cancelled'],
  stage_b_running: ['complete', 'error', 'cancelled'],
  complete: [],
  error: [],
  cancelled: [],
});

// --------------------------------------------------------------------------- //
// Validation                                                                   //
// --------------------------------------------------------------------------- //

export function isValidJobId(jobId) {
  return (
    typeof jobId === 'string' &&
    jobId.length >= 1 &&
    jobId.length <= JOB_ID_MAX_LEN &&
    JOB_ID_RE.test(jobId)
  );
}

export function isTerminal(state) {
  return TERMINAL_STATES.has(state);
}

export function canTransition(from, to) {
  if (from === to) return true;
  const allowed = TRANSITIONS[from];
  return Array.isArray(allowed) && allowed.indexOf(to) !== -1;
}

// --------------------------------------------------------------------------- //
// Record construction                                                          //
// --------------------------------------------------------------------------- //

function normalizeSeries(series) {
  if (!series || typeof series !== 'object' || Array.isArray(series)) {
    return { enabled: false, series_id: '', part: 0, start_seconds: 0, is_final: false };
  }
  return {
    enabled: Boolean(series.enabled),
    series_id: String(series.series_id || ''),
    part: Number(series.part || 0),
    start_seconds: Number(series.start_seconds || 0),
    is_final: Boolean(series.is_final),
  };
}

/**
 * Build a fresh, schema-conformant status record.
 * @param {{job_id: string, mode: string, state?: string, message?: string,
 *          nowEpoch?: number, ttlSeconds?: number, series?: object}} args
 */
export function newStatus(args) {
  if (!isValidJobId(args && args.job_id)) {
    throw new Error('invalid job_id: ' + String(args && args.job_id));
  }
  if (VALID_MODES.indexOf(args.mode) === -1) {
    throw new Error('invalid mode: ' + String(args.mode));
  }
  const state = args.state || 'queued';
  if (VALID_STATES.indexOf(state) === -1) {
    throw new Error('invalid state: ' + String(state));
  }
  const now = Number.isFinite(args.nowEpoch) ? Math.floor(args.nowEpoch) : Math.floor(Date.now() / 1000);
  // ttlSeconds: explicit per-call value wins; otherwise the configured env
  // TTL (CLIPFORGE_JOB_TTL_SECONDS from wrangler vars, mirroring the repo
  // Actions variable CLIPFORGE_TTL_SECONDS); otherwise DEFAULT_TTL_SECONDS.
  const ttl = Number.isFinite(args.ttlSeconds) ? Math.floor(args.ttlSeconds) : configuredJobTtlSeconds(args.env);

  return {
    version: STATUS_VERSION,
    job_id: args.job_id,
    mode: args.mode,
    series: normalizeSeries(args.series),
    state,
    message: args.message || '',
    created_at_epoch: now,
    updated_at_epoch: now,
    expires_at_epoch: now + ttl,
    release_tag: '',
    release_url: '',
    assets: {},
    run: { workflow_run_id: 0, workflow_run_url: '', code_ref: '' },
    publishing: { status: 'not_requested', posts: [], idempotency_key: '' },
  };
}

/**
 * Merge ``updates`` into a prior status record and return a new object. The
 * merge is defensive: it enforces the state-transition rules, refuses to move
 * out of a terminal state (except into another terminal state), and bumps
 * ``updated_at_epoch``.
 *
 * @param {object} prior
 * @param {object} updates fields: state, message, mode, release_tag,
 *   release_url, assets, run, publishing, series, expires_at_epoch
 * @param {{nowEpoch?: number}} [opts]
 */
export function mergeStatus(prior, updates, opts) {
  if (!prior || typeof prior !== 'object' || Array.isArray(prior)) {
    throw new Error('prior status must be an object');
  }
  const now = opts && Number.isFinite(opts.nowEpoch)
    ? Math.floor(opts.nowEpoch)
    : Math.floor(Date.now() / 1000);

  const next = Object.assign({}, prior);
  next.version = STATUS_VERSION;
  next.assets = Object.assign({}, prior.assets || {});
  next.run = Object.assign({ workflow_run_id: 0, workflow_run_url: '', code_ref: '' }, prior.run || {});
  next.publishing = Object.assign(
    { status: 'not_requested', posts: [], idempotency_key: '' },
    prior.publishing || {},
  );
  next.series = normalizeSeries(prior.series);

  if (!updates) updates = {};

  if (updates.mode !== undefined) {
    if (VALID_MODES.indexOf(updates.mode) === -1) {
      throw new Error('invalid mode: ' + String(updates.mode));
    }
    next.mode = updates.mode;
  }

  if (updates.state !== undefined) {
    if (VALID_STATES.indexOf(updates.state) === -1) {
      throw new Error('invalid state: ' + String(updates.state));
    }
    const priorState = prior.state;
    if (!canTransition(priorState, updates.state)) {
      throw new Error(
        'cannot transition ' + String(priorState) + ' -> ' + String(updates.state),
      );
    }
    next.state = updates.state;
  }

  if (updates.message !== undefined) next.message = String(updates.message);
  if (updates.release_tag !== undefined) next.release_tag = String(updates.release_tag);
  if (updates.release_url !== undefined) next.release_url = String(updates.release_url);

  if (updates.assets && typeof updates.assets === 'object') {
    for (const k of Object.keys(updates.assets)) {
      next.assets[k] = String(updates.assets[k]);
    }
  }

  if (updates.run && typeof updates.run === 'object') {
    for (const k of ['workflow_run_id', 'workflow_run_url', 'code_ref']) {
      if (k in updates.run) next.run[k] = updates.run[k];
    }
  }

  if (updates.publishing && typeof updates.publishing === 'object') {
    if ('status' in updates.publishing) {
      if (VALID_PUBLISHING_STATUSES.indexOf(updates.publishing.status) === -1) {
        throw new Error('invalid publishing.status: ' + String(updates.publishing.status));
      }
      next.publishing.status = updates.publishing.status;
    }
    if ('posts' in updates.publishing) {
      next.publishing.posts = Array.isArray(updates.publishing.posts)
        ? updates.publishing.posts.slice()
        : [];
    }
    if ('idempotency_key' in updates.publishing) {
      next.publishing.idempotency_key = String(updates.publishing.idempotency_key);
    }
  }

  if (updates.series !== undefined) next.series = normalizeSeries(updates.series);

  if (updates.expires_at_epoch !== undefined) {
    next.expires_at_epoch = Math.floor(Number(updates.expires_at_epoch));
  } else if (typeof next.expires_at_epoch !== 'number') {
    next.expires_at_epoch = Number(next.created_at_epoch || now) + DEFAULT_TTL_SECONDS;
  }

  next.updated_at_epoch = now;
  return next;
}

/**
 * Convenience: build the JSON text a bot commit would write to
 * ``jobs/<job_id>/status.json``. Uses the same indent-2 format as the Python
 * writer and adds a trailing newline so file diffs stay clean across languages.
 */
export function serializeStatus(record) {
  return JSON.stringify(record, null, 2) + '\n';
}

/**
 * Convenience: the canonical path (relative to the repo root) for a job's
 * status file. Consumers give this to their GitHub content-API call.
 */
export function statusPath(jobId) {
  if (!isValidJobId(jobId)) throw new Error('invalid job_id: ' + String(jobId));
  return 'jobs/' + jobId + '/status.json';
}

export default {
  STATUS_VERSION,
  VALID_STATES,
  TERMINAL_STATES,
  VALID_MODES,
  VALID_PUBLISHING_STATUSES,
  DEFAULT_TTL_SECONDS,
  configuredJobTtlSeconds,
  isValidJobId,
  isTerminal,
  canTransition,
  newStatus,
  mergeStatus,
  serializeStatus,
  statusPath,
};
