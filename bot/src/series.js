/**
 * Series Mode — manual continuation helpers (Bot A side).
 *
 * Port of the legacy ``manualSeriesContinuation`` / ``startNextSeriesPart``
 * logic from ``_legacy/telegram-bot/src/index.js`` onto the new contracts:
 *
 * - Requests are the nested §7.1 shape (``series.enabled`` / ``series_id`` /
 *   ``part`` / ``source_job_id`` …, ``mode: 'manual'``).
 * - Plans may carry EITHER the nested §7.3 ``series`` object or the legacy
 *   flat ``series_*`` siblings — extracted with the exact per-field
 *   precedence rule shared by ``bot/src/plan.js`` and
 *   ``pipeline/plan/schema.py`` (nested wins per-field).
 * - The continuation payload mirrors what ``pipeline/plan/series.py``
 *   derives for a series continuation, with the mode forced to ``manual``.
 *
 * Only the pure derivation lives here so it is testable offline; all
 * GitHub/Telegram side effects stay in bot/src/index.js.
 */

import { isValidJobId } from './jobs.js';

export const MAX_CONTEXT_CHARS = 8000;
export const MAX_JOB_ID_LENGTH = 120;

// Nested series keys -> legacy flat sibling names (same mapping as
// plan.js's NESTED_TO_FLAT and schema.py's _SERIES_NESTED_TO_FLAT — keep in sync).
const NESTED_TO_FLAT = {
  series_id: 'series_id',
  part: 'series_part',
  start_seconds: 'series_start_seconds',
  end_seconds: 'series_end_seconds',
  is_final: 'series_final',
  summary: 'series_summary',
};

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isInteger(value) {
  return typeof value === 'number' && Number.isFinite(value) && Math.floor(value) === value;
}

/**
 * Normalize series metadata from a production.json document, accepting the
 * nested §7.3 object and the legacy flat ``series_*`` siblings. Nested wins
 * per-field when both are present (the shared closest-analog rule).
 */
export function extractPlanSeries(document) {
  const source = isPlainObject(document) ? document : {};
  const nested = isPlainObject(source.series) ? source.series : {};
  const values = {};
  for (const nestedKey of Object.keys(NESTED_TO_FLAT)) {
    const flatKey = NESTED_TO_FLAT[nestedKey];
    if (Object.prototype.hasOwnProperty.call(source, flatKey)) {
      values[nestedKey] = source[flatKey];
    }
    if (Object.prototype.hasOwnProperty.call(nested, nestedKey)) {
      values[nestedKey] = nested[nestedKey];
    }
  }
  return values;
}

/**
 * Decide whether a COMPLETED job is a continuable manual series part and, if
 * so, return the next part's coordinates.
 *
 * Ported semantics (legacy manualSeriesContinuation):
 * - job must be in the 'complete' state;
 * - the persisted request must be a series request (``series.enabled``) in
 *   MANUAL mode;
 * - the request must carry a valid ``series_id`` and part ≥ 1;
 * - the plan must exist, must not be the final part, and must carry a valid
 *   ``end_seconds``.
 *
 * @param {object|null} status jobs/<id>/status.json document
 * @param {object|null} request jobs/<id>/stage-a-request.json document
 * @param {object|null} plan jobs/<id>/production.json document
 * @returns {{seriesId: string, part: number, startSeconds: number}|null}
 */
export function manualSeriesContinuation(status, request, plan) {
  if (!status || String(status.state) !== 'complete') return null;
  if (!isPlainObject(request)) return null;
  const reqSeries = isPlainObject(request.series) ? request.series : {};
  if (reqSeries.enabled !== true) return null;
  const seriesId = String(reqSeries.series_id || '').trim();
  const part = Number(reqSeries.part || 0);
  if (!seriesId || !Number.isInteger(part) || part < 1) return null;
  if (!isPlainObject(plan)) return null;
  const planSeries = extractPlanSeries(plan);
  if (planSeries.is_final === true) return null;
  const end = Number(planSeries.end_seconds);
  if (!isInteger(end) || end < 0) return null;
  return { seriesId, part: part + 1, startSeconds: end };
}

/**
 * Build the ``Prior events (Part N): <summary>`` context string for a series
 * (the same derivation pipeline/plan/series.py performs), capped at 8000
 * chars. ``entries`` is an array of {part, summary};
 * unsummarized parts are skipped.
 *
 * bug-56: the leading token is intentionally ``Prior events (Part N):`` rather
 * than a bare ``Part N:`` prefix. An AI reading the resulting prompt used to
 * see a bare ``Part 1:`` inside this continuity block even when it was
 * currently authoring Part 2, and could plausibly copy that number into the
 * production.json ``series.part`` field instead of the current-part value
 * stated elsewhere in the prompt. Prefixing every summary with ``Prior events``
 * makes the passage unmistakably about earlier parts.
 */
export function buildSeriesContext(entries) {
  const sorted = (Array.isArray(entries) ? entries : [])
    .filter((entry) => isInteger(Number(entry && entry.part)) && String(entry && entry.summary || '').trim() !== '')
    .map((entry) => [Number(entry.part), String(entry.summary).trim()])
    .sort((a, b) => a[0] - b[0]);
  const text = sorted
    .map(([part, summary]) => `Prior events (Part ${part}): ${summary}`)
    .join('\n');
  return (text || '(No prior summaries.)').slice(0, MAX_CONTEXT_CHARS);
}

/**
 * Build the wizardToRequest-shaped body for the next manual series part.
 * The result is passed to buildStageARequest/saveStageARequest, which stamps
 * version/job_id/saved_at_epoch and normalizes every field.
 */
export function nextPartRequestBody(request, continuation, context, currentJobId = '') {
  const source = isPlainObject(request.source) ? request.source : {};
  const options = isPlainObject(request.options) ? request.options : {};
  const music = isPlainObject(request.music) ? request.music : {};
  const reqSeries = isPlainObject(request.series) ? request.series : {};
  return {
    source: {
      kind: String(source.kind || ''),
      value: String(source.value || ''),
      ...(isPlainObject(source.relay) ? { relay: source.relay } : {}),
      ...(source.torrent_file_index !== undefined && source.torrent_file_index !== ''
        ? { torrent_file_index: String(source.torrent_file_index) }
        : {}),
    },
    options: {
      whisper_model: options.whisper_model,
      language: options.language,
      // bug-65: carry an explicit task choice (e.g. a "transcribe" opt-out)
      // across series parts; buildStageARequest defaults it to
      // "translate_to_english" when absent.
      task: options.task,
      target_duration_seconds: options.target_duration_seconds,
      // Series Mode has no editorial-focus override. Each part is bounded by
      // its persisted source window and continuity context instead.
      focus: '',
      enable_vision_assist: options.enable_vision_assist,
    },
    mode: 'manual',
    series: {
      enabled: true,
      series_id: continuation.seriesId,
      // The original source job id is carried forward unchanged so every
      // part can reuse Part 1's Stage A evidence. bug-64: when a pre-fix Part
      // 1 request left source_job_id blank, the fallback is the COMPLETING
      // part's own job id (currentJobId) — that job just reached 'complete',
      // so its release provably exists. Never the series_id: it is
      // series-<ts>, not a job id, so clipforge-<series_id> never exists
      // (the exact "release not found" bug-61's series_id fallback caused).
      source_job_id: String(reqSeries.source_job_id || currentJobId || continuation.seriesId),
      part: continuation.part,
      start_seconds: continuation.startSeconds,
      context: String(context || '').slice(0, MAX_CONTEXT_CHARS),
    },
    music: {
      ref: String(music.ref || ''),
      source: String(music.source || 'none'),
    },
  };
}

/** §6.3 identity rule for the derived next-part job id. */
export function nextPartJobId(continuation) {
  const nextId = `${continuation.seriesId}-p${continuation.part}`;
  if (!isValidJobId(nextId) || nextId.length > MAX_JOB_ID_LENGTH) {
    throw new Error('The next series part job id would be unsafe.');
  }
  return nextId;
}
