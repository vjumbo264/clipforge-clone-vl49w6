// Unit tests for the "hide 'Start next part' when next part already exists"
// fix (initiative: hide-next-part-button-when-dispatched).
//
// The reactive dispatch guard in bot/src/index.js's startNextSeriesPart uses
//   nextId = nextPartJobId(manualSeriesContinuation(status, request, plan))
// then checks readStageARequest(...) / readStatus(...) — non-null on either
// means the next part is already dispatched, so refuse to re-dispatch.
//
// The proactive render-time guard added in bot/src/runtime.js
// (nextPartAlreadyExists + a new nextPartExists parameter on taskKeyboard)
// must use the SAME derivation and probe the SAME two files, so the
// button is simply absent when the reactive guard would have blocked a
// click. These tests pin both halves:
//
//   1. taskKeyboard's gate: nextPartExists:false keeps the button
//      (regression guard for the pre-fix behavior); nextPartExists:true
//      removes it (the new behavior).
//   2. nextPartAlreadyExists derives the exact same nextId
//      startNextSeriesPart derives, against a stubbed GitHub fetch,
//      and reports true iff the stage-a-request OR the status of that
//      next id exists — a regression guard against the two code paths
//      silently diverging on which job id counts as "the next part."
//   3. showTask ends up rendering WITHOUT the "Start next part" button
//      when the next part already exists in the repo, and WITH the
//      button once the next part's files are removed (mirroring what
//      deleteClipforgeJob does to the underlying GitHub blobs) — proving
//      the delete-then-button-reappears exception works end to end.

import test from 'node:test';
import assert from 'node:assert/strict';

import { taskKeyboard, nextPartAlreadyExists, deriveNextPartId } from '../src/runtime.js';
import { manualSeriesContinuation, nextPartJobId } from '../src/series.js';
import { ensureTaskLabel, putCredentials } from '../src/storage.js';
import { makeD1 } from './helpers/d1.mjs';

const CHAT = 8181;
const TEST_KEY = Buffer.alloc(32, 9).toString('base64');

// ---------- shared fixtures ------------------------------------------------

function makeCompleteSeriesStatus() {
  // A completed manual series Part 1 whose plan says the series is not final:
  // this is the exact state under which taskKeyboard is asked to render the
  // "Start next part" row.
  return {
    version: 2,
    job_id: 'series-abc-p1',
    mode: 'manual',
    state: 'complete',
    message: 'Complete.',
    series: { enabled: true, series_id: 'series-abc', part: 1, start_seconds: 0, is_final: false },
    created_at_epoch: 1000,
    updated_at_epoch: 2000,
    expires_at_epoch: 44000,
    release_tag: 'clipforge-series-abc-p1',
    release_url: 'https://example.invalid/r',
    assets: {},
    run: { workflow_run_id: 0, workflow_run_url: '', code_ref: '' },
    publishing: { status: 'not_requested', posts: [], idempotency_key: '' },
  };
}

function makeStageARequest() {
  return {
    version: 2,
    job_id: 'series-abc-p1',
    source: { kind: 'url', value: 'https://example.invalid/v' },
    options: { whisper_model: 'base', language: 'auto', target_duration_seconds: 120, focus: '', enable_vision_assist: true },
    mode: 'manual',
    series: { enabled: true, series_id: 'series-abc', source_job_id: 'series-abc-p1', part: 1, start_seconds: 0, context: '' },
    music: { ref: '', source: 'none' },
    saved_at_epoch: 1,
  };
}

function makeNonFinalPlan() {
  return {
    version: 2,
    job_id: 'series-abc-p1',
    video_duration_seconds: 600,
    target_total_duration_seconds: 120,
    cuts: [{ start_seconds: 0, end_seconds: 120, voiceover_text: 'Hi.' }],
    series: { series_id: 'series-abc', part: 1, start_seconds: 0, end_seconds: 137, is_final: false, summary: 'Recap.' },
  };
}

// ---------- (1) taskKeyboard render-gate contract -------------------------

test('taskKeyboard shows Start next part when nextPartExists is false (pre-fix behavior preserved)', () => {
  const status = makeCompleteSeriesStatus();
  const plan = makeNonFinalPlan();
  const kb = taskKeyboard(status, 'A', plan, false);
  const callbacks = kb.inline_keyboard.flat().map((b) => b.callback_data || '');
  assert.ok(callbacks.includes('task:next:A'), 'button must be present when next part has NOT been dispatched');
});

test('taskKeyboard hides Start next part when nextPartExists is true (new behavior)', () => {
  const status = makeCompleteSeriesStatus();
  const plan = makeNonFinalPlan();
  const kb = taskKeyboard(status, 'A', plan, true);
  const callbacks = kb.inline_keyboard.flat().map((b) => b.callback_data || '');
  assert.ok(!callbacks.includes('task:next:A'), 'button must be ABSENT once the next part already exists');
  // Sibling actions must still render — the fix only removes ONE row.
  assert.ok(callbacks.includes('task:dl:A'), 'download must still render');
  assert.ok(callbacks.includes('task:parts:A'), 'series-parts must still render');
  assert.ok(callbacks.includes('task:prompt:A'), 'copy-prompt must still render');
});

test('taskKeyboard still hides Start next part when the plan says is_final=true, regardless of nextPartExists', () => {
  const status = makeCompleteSeriesStatus();
  const finalPlan = { ...makeNonFinalPlan(), series: { ...makeNonFinalPlan().series, is_final: true } };
  for (const nextPartExists of [false, true]) {
    const kb = taskKeyboard(status, 'A', finalPlan, nextPartExists);
    const callbacks = kb.inline_keyboard.flat().map((b) => b.callback_data || '');
    assert.ok(!callbacks.includes('task:next:A'),
      `final part must never show the button (nextPartExists=${nextPartExists})`);
  }
});

test('taskKeyboard defaults nextPartExists to false so legacy call sites keep pre-fix behavior', () => {
  // The §8.5 state-validity contract in wizard.test.mjs and every other
  // synchronous caller of taskKeyboard(status, label) MUST keep working
  // without touching the network. The default must therefore be false.
  const status = makeCompleteSeriesStatus();
  const plan = makeNonFinalPlan();
  const kb = taskKeyboard(status, 'A', plan); // no fourth argument
  const callbacks = kb.inline_keyboard.flat().map((b) => b.callback_data || '');
  assert.ok(callbacks.includes('task:next:A'), 'without the flag, button must still render');
});

// ---------- (2) caller-level derivation regression guard ------------------

test('deriveNextPartId matches nextPartJobId(manualSeriesContinuation(...)) — the exact derivation startNextSeriesPart uses', () => {
  // If these two code paths ever compute different job ids for "the next
  // part", the render-time hide and the reactive dispatch guard would
  // silently disagree. Pin them to the same expression.
  const status = makeCompleteSeriesStatus();
  const request = makeStageARequest();
  const plan = makeNonFinalPlan();
  const continuation = manualSeriesContinuation(status, request, plan);
  assert.ok(continuation, 'sanity: continuation should be non-null in the completed non-final case');
  const expected = nextPartJobId(continuation); // === 'series-abc-p2'
  assert.equal(deriveNextPartId(status, request, plan), expected);
  assert.equal(expected, 'series-abc-p2');
});

test('deriveNextPartId returns null when the current job is not a continuable part', () => {
  const status = makeCompleteSeriesStatus();
  const request = makeStageARequest();
  // Final part -> not continuable.
  assert.equal(deriveNextPartId(status, request, { ...makeNonFinalPlan(), series: { ...makeNonFinalPlan().series, is_final: true } }), null);
  // Not yet complete -> not continuable.
  assert.equal(deriveNextPartId({ ...status, state: 'stage_b_running' }, request, makeNonFinalPlan()), null);
  // Not a series request -> not continuable.
  const nonSeriesRequest = { ...request, series: { enabled: false, series_id: '', source_job_id: '', part: 0, start_seconds: 0, context: '' } };
  assert.equal(deriveNextPartId(status, nonSeriesRequest, makeNonFinalPlan()), null);
});

// ---------- (2b) nextPartAlreadyExists against stubbed GitHub -------------

function stubFetchWithFiles(files) {
  // Same shape as bot/test/tasks.test.mjs installFetch(): answer GitHub
  // /contents/<path> reads with a base64 JSON blob when files[<path>]
  // exists, 404 otherwise. Only /contents/ URLs are used here.
  const original = globalThis.fetch;
  const observed = [];
  globalThis.fetch = async (url) => {
    const u = String(url);
    observed.push(u);
    if (u.startsWith('https://api.github.com/')) {
      const m = u.match(/\/contents\/(.+?)(?:\?|$)/);
      const path = m ? decodeURIComponent(m[1]) : '';
      if (Object.prototype.hasOwnProperty.call(files, path) && files[path] !== null) {
        return new Response(JSON.stringify({
          content: Buffer.from(JSON.stringify(files[path]), 'utf8').toString('base64'),
          sha: 'abc',
        }), { status: 200 });
      }
      return new Response(JSON.stringify({ message: 'Not Found' }), { status: 404 });
    }
    throw new Error('unexpected fetch: ' + u);
  };
  return { restore: () => { globalThis.fetch = original; }, observed };
}

const CREDS = { githubPat: 'pat-not-real', repo: 'owner/repo', geminiKeys: [] };

test('nextPartAlreadyExists returns true when the next part status.json exists', async () => {
  const status = makeCompleteSeriesStatus();
  const request = makeStageARequest();
  const plan = makeNonFinalPlan();
  // The proactive guard must probe THE EXACT SAME job id
  // startNextSeriesPart probes — series-abc-p2 in this fixture.
  const nextId = nextPartJobId(manualSeriesContinuation(status, request, plan));
  const { restore, observed } = stubFetchWithFiles({
    [`jobs/${nextId}/status.json`]: { state: 'queued', job_id: nextId },
  });
  try {
    const exists = await nextPartAlreadyExists(CREDS, status, request, plan);
    assert.equal(exists, true);
    // Also verify we probed the correct paths — regression guard against
    // the derivation diverging from startNextSeriesPart's.
    const probed = observed.filter((u) => u.includes(`/contents/jobs/${nextId}/`));
    assert.ok(probed.length >= 1, 'nextPartAlreadyExists must probe jobs/<nextId>/ files');
  } finally {
    restore();
  }
});

test('nextPartAlreadyExists returns true when only the next part stage-a-request.json exists', async () => {
  // Between save and status write, only stage-a-request is present. The
  // reactive guard treats this as "already dispatched"; the proactive
  // guard must agree.
  const status = makeCompleteSeriesStatus();
  const request = makeStageARequest();
  const plan = makeNonFinalPlan();
  const nextId = nextPartJobId(manualSeriesContinuation(status, request, plan));
  const { restore } = stubFetchWithFiles({
    [`jobs/${nextId}/stage-a-request.json`]: { job_id: nextId, mode: 'manual' },
  });
  try {
    assert.equal(await nextPartAlreadyExists(CREDS, status, request, plan), true);
  } finally {
    restore();
  }
});

test('nextPartAlreadyExists returns false when NEITHER next-part file exists', async () => {
  const status = makeCompleteSeriesStatus();
  const request = makeStageARequest();
  const plan = makeNonFinalPlan();
  const { restore } = stubFetchWithFiles({}); // every read 404s
  try {
    assert.equal(await nextPartAlreadyExists(CREDS, status, request, plan), false);
  } finally {
    restore();
  }
});

test('nextPartAlreadyExists short-circuits (NO GitHub call) on states where the button would never render', async () => {
  // If a task is not complete, or not a series, or the final part, the
  // button row would not be shown anyway — the probe must not spend an
  // extra GitHub round-trip for those cards.
  const request = makeStageARequest();
  const plan = makeNonFinalPlan();
  const { restore, observed } = stubFetchWithFiles({});
  try {
    // not complete
    assert.equal(await nextPartAlreadyExists(CREDS, { ...makeCompleteSeriesStatus(), state: 'stage_b_running' }, request, plan), false);
    // not a series
    assert.equal(await nextPartAlreadyExists(CREDS, { ...makeCompleteSeriesStatus(), series: { enabled: false } }, request, plan), false);
    // final part
    const finalPlan = { ...plan, series: { ...plan.series, is_final: true } };
    assert.equal(await nextPartAlreadyExists(CREDS, makeCompleteSeriesStatus(), request, finalPlan), false);
    // No GitHub reads should have happened for any of the above.
    assert.equal(observed.filter((u) => u.startsWith('https://api.github.com/')).length, 0,
      'no /contents/ probes may fire when the button row wouldn\'t render anyway');
  } finally {
    restore();
  }
});

// ---------- (3) showTask end-to-end: hide, then reappear after delete -----

function stubFetchForShowTask({ files, sent }) {
  // Broader stub: answers GitHub /contents/ reads AND records Telegram
  // POSTs so we can inspect the rendered keyboard. Mirrors tasks.test.mjs.
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    if (u.startsWith('https://api.github.com/')) {
      const m = u.match(/\/contents\/(.+?)(?:\?|$)/);
      const path = m ? decodeURIComponent(m[1]) : '';
      if (Object.prototype.hasOwnProperty.call(files, path) && files[path] !== null) {
        return new Response(JSON.stringify({
          content: Buffer.from(JSON.stringify(files[path]), 'utf8').toString('base64'),
          sha: 'abc',
        }), { status: 200 });
      }
      return new Response(JSON.stringify({ message: 'Not Found' }), { status: 404 });
    }
    if (u.startsWith('https://api.telegram.org/')) {
      const method = u.split('/').pop();
      let payload = {};
      try { payload = init && init.body ? JSON.parse(String(init.body)) : {}; } catch { payload = {}; }
      sent.push({ method, payload });
      return new Response(JSON.stringify({ ok: true, result: { message_id: sent.length } }), { status: 200 });
    }
    throw new Error('unexpected fetch: ' + u);
  };
  return () => { globalThis.fetch = original; };
}

async function makeEnv() {
  const kv = new Map();
  const kvStub = {
    get: async (k) => (kv.has(k) ? kv.get(k) : null),
    put: async (k, v) => { kv.set(k, String(v)); },
    delete: async (k) => { kv.delete(k); },
  };
  const env = { CLIPFORGE_BOT_KV: kvStub, CLIPFORGE_BOT_D1: makeD1(), KV_ENCRYPTION_KEY: TEST_KEY, TELEGRAM_BOT_TOKEN: 'test-token' };
  await putCredentials(env, CHAT, { githubPat: 'pat-not-real', repo: 'owner/repo', geminiKeys: [] });
  return env;
}

test('showTask HIDES Start next part when the next part already exists in the repo', async () => {
  const env = await makeEnv();
  await ensureTaskLabel(env, CHAT, 'series-abc-p1');
  const sent = [];
  const files = {
    'jobs/series-abc-p1/status.json': makeCompleteSeriesStatus(),
    'jobs/series-abc-p1/production.json': makeNonFinalPlan(),
    'jobs/series-abc-p1/stage-a-request.json': makeStageARequest(),
    // The next part exists — this is what the fix must observe.
    'jobs/series-abc-p2/status.json': { state: 'queued', job_id: 'series-abc-p2' },
  };
  const restore = stubFetchForShowTask({ files, sent });
  try {
    const { showTask } = await import('../src/runtime.js');
    await showTask(env, CHAT, 'A');
  } finally {
    restore();
  }
  assert.equal(sent.length, 1, 'the task view must render exactly once');
  const kb = JSON.stringify(sent[0].payload.reply_markup || {});
  assert.doesNotMatch(kb, /task:next:A/, 'Start next part must be ABSENT when the next part already exists');
  // Sibling rows for a completed manual series part must still render.
  assert.match(kb, /task:dl:A/, 'download row must still render');
  assert.match(kb, /task:parts:A/, 'series-parts row must still render');
});

test('showTask reveals Start next part again when the next part\'s files are removed (delete-then-reappears)', async () => {
  // bot/src/github.js deleteClipforgeJob() removes every jobs/<jobId>/*
  // blob from the default branch. This test models that exact effect by
  // starting with the next part present, showing the card (button
  // hidden), then removing its files and re-rendering the card — the
  // button must reappear because the probe reads live per render.
  const env = await makeEnv();
  await ensureTaskLabel(env, CHAT, 'series-abc-p1');
  const sent = [];
  const files = {
    'jobs/series-abc-p1/status.json': makeCompleteSeriesStatus(),
    'jobs/series-abc-p1/production.json': makeNonFinalPlan(),
    'jobs/series-abc-p1/stage-a-request.json': makeStageARequest(),
    'jobs/series-abc-p2/status.json': { state: 'queued', job_id: 'series-abc-p2' },
  };
  const restore = stubFetchForShowTask({ files, sent });
  try {
    const { showTask } = await import('../src/runtime.js');
    await showTask(env, CHAT, 'A');
    const kbBefore = JSON.stringify(sent[0].payload.reply_markup || {});
    assert.doesNotMatch(kbBefore, /task:next:A/, 'baseline: button hidden while next part exists');

    // Simulate deleteClipforgeJob wiping the next part's tree. In real
    // production this removes BOTH status.json and stage-a-request.json
    // (and any other blobs under jobs/series-abc-p2/) from the default
    // branch of GitHub — see bot/src/github.js. Here we just drop them
    // from the stubbed content map.
    delete files['jobs/series-abc-p2/status.json'];
    delete files['jobs/series-abc-p2/stage-a-request.json'];

    await showTask(env, CHAT, 'A');
    const kbAfter = JSON.stringify(sent[sent.length - 1].payload.reply_markup || {});
    assert.match(kbAfter, /task:next:A/, 'after delete, the button must reappear on the previous part\'s card');
  } finally {
    restore();
  }
});
