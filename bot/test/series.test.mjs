import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildSeriesContext,
  extractPlanSeries,
  manualSeriesContinuation,
  nextPartJobId,
  nextPartRequestBody,
} from '../src/series.js';

function makeRequest(overrides = {}) {
  return {
    version: 2,
    job_id: 'series-1-p1',
    source: { kind: 'url', value: 'https://example.com/v' },
    options: {
      whisper_model: 'base',
      language: 'auto',
      target_duration_seconds: 120,
      focus: '',
      enable_vision_assist: true,
    },
    mode: 'manual',
    series: {
      enabled: true,
      series_id: 'series-1',
      source_job_id: 'series-1-p1',
      part: 1,
      start_seconds: 0,
      context: '',
    },
    music: { ref: '', source: 'none' },
    saved_at_epoch: 1,
    ...overrides,
  };
}

function makePlanNested(overrides = {}) {
  return {
    version: 2,
    job_id: 'series-1-p1',
    video_duration_seconds: 600,
    target_total_duration_seconds: 120,
    cuts: [{ start_seconds: 0, end_seconds: 120, voiceover_text: 'Hi.' }],
    series: {
      series_id: 'series-1',
      part: 1,
      start_seconds: 0,
      end_seconds: 120,
      is_final: false,
      summary: 'Recap one.',
      ...overrides,
    },
  };
}

function makePlanFlat(overrides = {}) {
  return {
    version: 2,
    job_id: 'series-1-p1',
    video_duration_seconds: 600,
    target_total_duration_seconds: 120,
    cuts: [{ start_seconds: 0, end_seconds: 120, voiceover_text: 'Hi.' }],
    series_id: 'series-1',
    series_part: 1,
    series_start_seconds: 0,
    series_end_seconds: 120,
    series_final: false,
    series_summary: 'Recap one.',
    ...overrides,
  };
}

const completeStatus = { state: 'complete', mode: 'manual', series: { enabled: true, series_id: 'series-1', part: 1, start_seconds: 0, is_final: false } };

test('extractPlanSeries reads the nested shape', () => {
  const values = extractPlanSeries(makePlanNested());
  assert.equal(values.series_id, 'series-1');
  assert.equal(values.part, 1);
  assert.equal(values.end_seconds, 120);
  assert.equal(values.is_final, false);
  assert.equal(values.summary, 'Recap one.');
});

test('extractPlanSeries reads the legacy flat shape', () => {
  const values = extractPlanSeries(makePlanFlat());
  assert.equal(values.series_id, 'series-1');
  assert.equal(values.part, 1);
  assert.equal(values.end_seconds, 120);
});

test('extractPlanSeries nested wins per-field over flat', () => {
  const plan = { ...makePlanFlat(), series: { end_seconds: 222 } };
  const values = extractPlanSeries(plan);
  assert.equal(values.end_seconds, 222); // nested wins
  assert.equal(values.series_id, 'series-1'); // flat fallback
});

test('manualSeriesContinuation requires the complete state', () => {
  assert.equal(manualSeriesContinuation(null, makeRequest(), makePlanNested()), null);
  assert.equal(manualSeriesContinuation({ state: 'stage_b_running' }, makeRequest(), makePlanNested()), null);
  assert.equal(manualSeriesContinuation(completeStatus, makeRequest(), makePlanNested()) !== null, true);
});

test('manualSeriesContinuation requires a series request', () => {
  assert.equal(manualSeriesContinuation(completeStatus, null, makePlanNested()), null);
  // not a series request
  assert.equal(
    manualSeriesContinuation(completeStatus, makeRequest({ series: { enabled: false, series_id: '', source_job_id: '', part: 0, start_seconds: 0, context: '' } }), makePlanNested()),
    null,
  );
  // mode no longer gates continuation — every job is manual now, and a
  // historical request persisted with a legacy mode value continues the same
  assert.notEqual(manualSeriesContinuation(completeStatus, makeRequest({ mode: 'automatic' }), makePlanNested()), null);
  // missing series id
  assert.equal(
    manualSeriesContinuation(completeStatus, makeRequest({ series: { enabled: true, series_id: '', source_job_id: 'x', part: 1, start_seconds: 0, context: '' } }), makePlanNested()),
    null,
  );
  // invalid part
  assert.equal(
    manualSeriesContinuation(completeStatus, makeRequest({ series: { enabled: true, series_id: 's', source_job_id: 'x', part: 0, start_seconds: 0, context: '' } }), makePlanNested()),
    null,
  );
});

test('manualSeriesContinuation requires a non-final plan with a valid end', () => {
  assert.equal(manualSeriesContinuation(completeStatus, makeRequest(), null), null);
  assert.equal(manualSeriesContinuation(completeStatus, makeRequest(), makePlanNested({ is_final: true })), null);
  assert.equal(manualSeriesContinuation(completeStatus, makeRequest(), makePlanFlat({ series_final: true })), null);
  assert.equal(manualSeriesContinuation(completeStatus, makeRequest(), makePlanNested({ end_seconds: 'later' })), null);
});

test('manualSeriesContinuation returns the next part coordinates (nested + flat)', () => {
  const nested = manualSeriesContinuation(completeStatus, makeRequest(), makePlanNested({ end_seconds: 137 }));
  assert.deepEqual(nested, { seriesId: 'series-1', part: 2, startSeconds: 137 });
  const flat = manualSeriesContinuation(completeStatus, makeRequest(), makePlanFlat({ series_end_seconds: 90 }));
  assert.deepEqual(flat, { seriesId: 'series-1', part: 2, startSeconds: 90 });
});

test('buildSeriesContext orders, skips empty summaries, and caps at 8000 chars', () => {
  assert.equal(buildSeriesContext([]), '(No prior summaries.)');
  // bug-56: each line is prefixed with "Prior events (Part N):" rather than a
  // bare "Part N:" — an AI reading the resulting prompt used to be able to
  // copy that bare Part number into its own production.json series.part.
  assert.equal(
    buildSeriesContext([{ part: 2, summary: 'Second.' }, { part: 1, summary: 'First.' }, { part: 3, summary: '  ' }]),
    'Prior events (Part 1): First.\nPrior events (Part 2): Second.',
  );
  const long = buildSeriesContext([{ part: 1, summary: 'x'.repeat(9000) }]);
  assert.ok(long.length <= 8000);
});

test('nextPartRequestBody mirrors the pipeline continuation with mode manual', () => {
  const request = makeRequest({
    source: { kind: 'torrent_file', value: 'path:jobs/series-1-p1/source.torrent', torrent_file_index: '3' },
    music: { ref: 'audio-library/theme.mp3', source: 'explicit_library' },
  });
  const body = nextPartRequestBody(request, { seriesId: 'series-1', part: 2, startSeconds: 120 }, 'Prior events (Part 1): Recap one.');
  assert.equal(body.mode, 'manual');
  assert.equal(body.source.kind, 'torrent_file');
  assert.equal(body.source.torrent_file_index, '3');
  assert.equal(body.options.focus, '');
  assert.deepEqual(body.series, {
    enabled: true,
    series_id: 'series-1',
    source_job_id: 'series-1-p1',
    part: 2,
    start_seconds: 120,
    context: 'Prior events (Part 1): Recap one.',
  });
  assert.deepEqual(body.music, { ref: 'audio-library/theme.mp3', source: 'explicit_library' });
});

test('nextPartRequestBody falls back to the completing job id when source_job_id is missing (bug-64)', () => {
  // bug-64: a pre-fix Part 1 request with a blank source_job_id must resolve
  // to the completing part's OWN job id (its release provably exists), never
  // the series_id — series_id is series-<ts>, not a job id, so
  // clipforge-series-<ts> is the "release not found" the bug is about.
  const request = makeRequest({ series: { enabled: true, series_id: 'series-1', source_job_id: '', part: 1, start_seconds: 0, context: '' } });
  const body = nextPartRequestBody(request, { seriesId: 'series-1', part: 2, startSeconds: 120 }, '', 'manual-111');
  assert.equal(body.series.source_job_id, 'manual-111');
  // ...and a request whose source_job_id is a series id (the bug-61 shape) is
  // passed through unchanged — the stage-a.yml reuse step treats any
  // 'series-'-prefixed source_job_id as broken legacy data and re-points it
  // at the current job id there.
  const legacy = nextPartRequestBody(
    makeRequest({ series: { enabled: true, series_id: 'series-1', source_job_id: 'series-1', part: 1, start_seconds: 0, context: '' } }),
    { seriesId: 'series-1', part: 2, startSeconds: 120 }, '', 'manual-111');
  assert.equal(legacy.series.source_job_id, 'series-1');
});

test('nextPartJobId enforces the §6.3 identity rule', () => {
  assert.equal(nextPartJobId({ seriesId: 'series-1', part: 2 }), 'series-1-p2');
  assert.throws(() => nextPartJobId({ seriesId: 'bad id!', part: 2 }), /unsafe/);
  assert.throws(() => nextPartJobId({ seriesId: 's'.repeat(119), part: 2 }), /unsafe/);
});
