// Standalone JS-side smoke tests for bot/src/plan.js. The authoritative
// cross-language equivalence check lives in
// pipeline/tests/test_plan_cross_validation.py — this file just gives the
// bot repo a self-contained `node --test` run.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateProductionPlan, parseAndValidateProductionPlan } from '../src/plan.js';

test('accepts a minimal valid plan', () => {
  const errors = validateProductionPlan({
    video_duration_seconds: 120,
    target_total_duration_seconds: 30,
    cuts: [
      { start_seconds: 0, end_seconds: 10, voiceover_text: 'a' },
      { start_seconds: 20, end_seconds: 30, voiceover_text: 'b' },
    ],
  });
  assert.deepEqual(errors, []);
});

test('rejects non-object', () => {
  assert.deepEqual(validateProductionPlan([1, 2, 3]), ['Top level must be a JSON object.']);
});

test('rejects overlapping cuts', () => {
  const errors = validateProductionPlan({
    video_duration_seconds: 100,
    target_total_duration_seconds: 30,
    cuts: [
      { start_seconds: 0, end_seconds: 30, voiceover_text: 'x' },
      { start_seconds: 20, end_seconds: 40, voiceover_text: 'y' },
    ],
  });
  assert.ok(errors.some((e) => e.includes('overlaps or precedes')));
});

test('accepts both nested and flat series shapes', () => {
  const nested = validateProductionPlan({
    video_duration_seconds: 300,
    target_total_duration_seconds: 60,
    series: {
      series_id: 'abc', part: 2, start_seconds: 100, end_seconds: 200,
      is_final: false, summary: 'ok',
    },
    cuts: [{ start_seconds: 110, end_seconds: 150, voiceover_text: 'x' }],
  });
  assert.deepEqual(nested, []);

  const flat = validateProductionPlan({
    video_duration_seconds: 300,
    target_total_duration_seconds: 60,
    series_id: 'abc', series_part: 2, series_start_seconds: 100, series_end_seconds: 200,
    series_final: false, series_summary: 'ok',
    cuts: [{ start_seconds: 110, end_seconds: 150, voiceover_text: 'x' }],
  });
  assert.deepEqual(flat, []);
});

test('parseAndValidateProductionPlan reports JSON parse errors', () => {
  const { document, errors } = parseAndValidateProductionPlan('{not-json');
  assert.equal(document, null);
  assert.ok(errors.length >= 1);
  assert.ok(errors[0].startsWith('Not valid JSON:'));
});
