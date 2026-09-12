/**
 * ClipForge production.json validator — JS port paired with
 * ``pipeline/plan/schema.py``. The two implementations MUST return the same
 * accept/reject decision and the same error strings for the same input.
 * That equivalence is enforced by
 * ``pipeline/tests/test_plan_cross_validation.py``.
 *
 * See ARCHITECTURE.md §7.3 for the rules. Notes for future sessions:
 *
 * - ARCHITECTURE.md §7.3's nested ``series`` shape is canonical (operator
 *   decision, 2026-08-26 — see resolved concern ``series-shape-nested-vs-flat``
 *   in ``BUILD_PROGRESS.json``). Legacy flat ``series_*`` sibling fields are
 *   accepted as input only, for plans already in flight and legacy tooling —
 *   never emitted by current prompts. Do not remove flat-input acceptance
 *   without a separate operator call. Mirrors the ``raw_narration`` alongside
 *   ``voiceover_text`` precedent.
 * - Unknown top-level fields are permitted.
 * - No external dependencies. Runs inside the Cloudflare Worker.
 */

'use strict';

// --------------------------------------------------------------------------- //
// Low-level type helpers                                                       //
// --------------------------------------------------------------------------- //

function isInteger(value) {
  return (
    typeof value === 'number' &&
    Number.isFinite(value) &&
    Math.floor(value) === value
  );
}

function isNonemptyString(value) {
  return typeof value === 'string' && value.trim() !== '';
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

// --------------------------------------------------------------------------- //
// Series-shape normalization                                                   //
// --------------------------------------------------------------------------- //

const NESTED_TO_FLAT = {
  series_id: 'series_id',
  part: 'series_part',
  start_seconds: 'series_start_seconds',
  end_seconds: 'series_end_seconds',
  is_final: 'series_final',
  summary: 'series_summary',
};

function extractSeries(document) {
  const nested = document.series;
  const values = {};
  let hasFlat = false;
  const hasNested = isPlainObject(nested);

  for (const nestedKey of Object.keys(NESTED_TO_FLAT)) {
    const flatKey = NESTED_TO_FLAT[nestedKey];
    if (Object.prototype.hasOwnProperty.call(document, flatKey)) {
      hasFlat = true;
      values[nestedKey] = document[flatKey];
    }
    if (hasNested && Object.prototype.hasOwnProperty.call(nested, nestedKey)) {
      values[nestedKey] = nested[nestedKey];
    }
  }

  return { isSeriesPlan: hasFlat || hasNested, values };
}

// --------------------------------------------------------------------------- //
// Public API                                                                   //
// --------------------------------------------------------------------------- //

export function validateProductionPlan(document) {
  const errors = [];

  if (!isPlainObject(document)) {
    return ['Top level must be a JSON object.'];
  }

  // -- Optional title ------------------------------------------------------ //
  if (Object.prototype.hasOwnProperty.call(document, 'title')) {
    if (!isNonemptyString(document.title)) {
      errors.push('`title` must be a non-empty string when present (or omit it entirely).');
    }
  }

  // -- Required positive-integer scalars ---------------------------------- //
  for (const key of ['video_duration_seconds', 'target_total_duration_seconds']) {
    const value = document[key];
    if (!isInteger(value) || value <= 0) {
      errors.push('`' + key + '` must be a positive integer.');
    }
  }

  // -- Optional tag arrays ------------------------------------------------- //
  validateStringArray(document.hashtags, {
    name: 'hashtags',
    present: Object.prototype.hasOwnProperty.call(document, 'hashtags'),
    minimum: 5,
    maximum: 8,
    requirePrefix: '#',
    forbidWhitespace: true,
  }, errors);

  validateStringArray(document.youtube_tags, {
    name: 'youtube_tags',
    present: Object.prototype.hasOwnProperty.call(document, 'youtube_tags'),
    minimum: 10,
    maximum: 20,
    forbidPrefix: '#',
    forbidSubstring: ',',
  }, errors);

  // -- Series (optional) --------------------------------------------------- //
  const { isSeriesPlan, values: seriesValues } = extractSeries(document);
  let seriesStart = null;
  let seriesEnd = null;

  if (isSeriesPlan) {
    if (!isNonemptyString(seriesValues.series_id)) {
      errors.push('`series_id` must be a non-empty string for a series production plan.');
    }

    const part = seriesValues.part;
    if (!isInteger(part) || part <= 0) {
      errors.push('`series_part` must be a positive integer for a series production plan.');
    }

    const startVal = seriesValues.start_seconds;
    if (!isInteger(startVal) || startVal < 0) {
      errors.push('`series_start_seconds` must be a non-negative integer for a series production plan.');
    } else {
      seriesStart = startVal;
    }

    const endVal = seriesValues.end_seconds;
    if (!isInteger(endVal) || endVal < 0) {
      errors.push('`series_end_seconds` must be a non-negative integer for a series production plan.');
    } else {
      seriesEnd = endVal;
    }

    if (seriesStart !== null && seriesEnd !== null && seriesEnd <= seriesStart) {
      errors.push('`series_end_seconds` must be greater than `series_start_seconds`.');
    }

    if (typeof seriesValues.is_final !== 'boolean') {
      errors.push('`series_final` must be boolean for a series production plan.');
    }

    const summary = seriesValues.summary;
    if (!isNonemptyString(summary)) {
      errors.push('`series_summary` must be a non-empty string for a series production plan.');
    } else if (summary.trim().length > 1200) {
      errors.push('`series_summary` exceeds the maximum allowed length.');
    }
  }

  // -- Cuts ---------------------------------------------------------------- //
  const cuts = document.cuts;
  if (!Array.isArray(cuts)) {
    errors.push('`cuts` must be an array.');
    return errors;
  }
  if (cuts.length < 1) {
    errors.push('`cuts` is empty — at least one cut is required.');
    return errors;
  }

  const duration = document.video_duration_seconds;
  const validDuration = isInteger(duration) ? duration : null;
  let previousEnd = null;

  for (let index = 0; index < cuts.length; index += 1) {
    const cut = cuts[index];
    const at = 'cuts[' + index + ']';

    if (!isPlainObject(cut)) {
      errors.push(at + ' must be an object.');
      continue;
    }

    const start = cut.start_seconds;
    const end = cut.end_seconds;

    if (!isInteger(start)) errors.push(at + '.start_seconds must be an integer.');
    if (!isInteger(end)) errors.push(at + '.end_seconds must be an integer.');

    let narration = cut.voiceover_text;
    if (!isNonemptyString(narration)) narration = cut.raw_narration;
    if (!isNonemptyString(narration)) {
      errors.push(at + '.voiceover_text must be a non-empty string (legacy raw_narration accepted).');
    }

    if (!isInteger(start) || !isInteger(end)) continue;

    if (start < 0) errors.push(at + '.start_seconds must be at least 0.');
    if (seriesStart !== null && start < seriesStart) {
      errors.push(at + '.start_seconds precedes series_start_seconds.');
    }
    if (seriesEnd !== null && end > seriesEnd) {
      errors.push(at + '.end_seconds exceeds series_end_seconds.');
    }
    if (end <= start) errors.push(at + '.end_seconds must be greater than start_seconds.');
    if (validDuration !== null && end > validDuration) {
      errors.push(at + '.end_seconds exceeds video_duration_seconds.');
    }
    if (previousEnd !== null && start < previousEnd) {
      errors.push(at + ' overlaps or precedes the prior cut.');
    }
    previousEnd = end;
  }

  return errors;
}

export function parseAndValidateProductionPlan(text) {
  let document;
  try {
    document = JSON.parse(text);
  } catch (err) {
    // Mirror the Python "Not valid JSON: <msg>." shape while remaining
    // resilient to the fact that browser/V8 JSON error messages differ from
    // Python's. The cross-validator tests do not exercise this path with
    // free-form messages — they only assert that JSON errors are reported
    // as a single-line error, not the exact wording.
    const msg = err && err.message ? String(err.message) : 'parse error';
    return { document: null, errors: ['Not valid JSON: ' + msg + '.'] };
  }
  return { document, errors: validateProductionPlan(document) };
}

// --------------------------------------------------------------------------- //
// Internal helpers                                                             //
// --------------------------------------------------------------------------- //

function validateStringArray(value, options, errors) {
  if (!options.present) return;

  if (!Array.isArray(value)) {
    errors.push('`' + options.name + '` must be an array of strings when present.');
    return;
  }

  if (value.length < options.minimum || value.length > options.maximum) {
    errors.push(
      '`' + options.name + '` must contain between ' +
      options.minimum + ' and ' + options.maximum + ' entries.'
    );
  }

  const seen = new Set();
  for (let index = 0; index < value.length; index += 1) {
    const entry = value[index];
    const at = options.name + '[' + index + ']';

    if (!isNonemptyString(entry)) {
      errors.push(at + ' must be a non-empty string.');
      continue;
    }
    const cleaned = entry.trim();
    if (options.requirePrefix && !cleaned.startsWith(options.requirePrefix)) {
      errors.push(at + ' must start with ' + options.requirePrefix + '.');
    }
    if (options.forbidWhitespace && /\s/.test(cleaned)) {
      errors.push(at + ' must not contain whitespace.');
    }
    if (options.forbidPrefix && cleaned.startsWith(options.forbidPrefix)) {
      errors.push(at + ' must not start with ' + options.forbidPrefix + '.');
    }
    if (options.forbidSubstring && cleaned.indexOf(options.forbidSubstring) !== -1) {
      errors.push(at + ' must not contain ' + options.forbidSubstring + '.');
    }
    const comparable = cleaned.toLowerCase();
    if (seen.has(comparable)) {
      errors.push(at + ' duplicates an earlier entry.');
    } else {
      seen.add(comparable);
    }
  }
}

export default { validateProductionPlan, parseAndValidateProductionPlan };
