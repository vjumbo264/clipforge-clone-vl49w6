// Unit tests for the /tasks list (bot/src/commands/tasks.js).
//
// Bug 1 fix coverage: a task whose status document cannot be read must
// surface as a distinct "status unavailable" row, not silently fold into the
// active list as a plain "queued" task.
//
// Uses node's built-in test runner with a Map-backed CLIPFORGE_BOT_KV stub
// (credentials still live in KV), a node:sqlite-backed CLIPFORGE_BOT_D1
// harness (task labels/options moved to D1 in the kv-minimization
// migration), and a stubbed global fetch standing in for the GitHub +
// Telegram HTTP calls (the same pattern as relay.test.mjs).
import test from 'node:test';
import assert from 'node:assert/strict';

import { loadTaskList, showTasks } from '../src/commands/tasks.js';
import { describeTaskState } from '../src/constants.js';
import { ensureTaskLabel, putCredentials } from '../src/storage.js';
import { makeD1 } from './helpers/d1.mjs';

const CHAT = 4242;
const TEST_KEY = Buffer.alloc(32, 7).toString('base64');

function makeKv() {
  const map = new Map();
  return {
    get: async (k) => (map.has(k) ? map.get(k) : null),
    put: async (k, v) => { map.set(k, String(v)); },
    delete: async (k) => { map.delete(k); },
    _map: map,
  };
}

function makeStatus(jobId, state, created) {
  return {
    version: 2,
    job_id: jobId,
    mode: 'manual',
    series: { enabled: false, series_id: '', part: 0, start_seconds: 0, is_final: false },
    state,
    message: '',
    created_at_epoch: created,
    updated_at_epoch: created,
    expires_at_epoch: created + 43200,
    release_tag: '',
    release_url: '',
    assets: {},
    run: { workflow_run_id: 0, workflow_run_url: '', code_ref: '' },
    publishing: { status: 'not_requested', posts: [], idempotency_key: '' },
  };
}

// Stub fetch: answers GitHub contents reads from `files` (path -> doc; absent
// key => 404), records every Telegram call's JSON payload into `sent`.
function installFetch({ files = {}, sent = [] } = {}) {
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

async function makeEnv(kv) {
  const env = { CLIPFORGE_BOT_KV: kv, CLIPFORGE_BOT_D1: makeD1(), KV_ENCRYPTION_KEY: TEST_KEY, TELEGRAM_BOT_TOKEN: 'test-token' };
  await putCredentials(env, CHAT, { githubPat: 'pat-not-real', repo: 'owner/repo', geminiKeys: [] });
  return env;
}

// Minimal env for tests that only exercise label/options storage: KV is not
// touched by the D1-backed functions, but keeping the stub makes accidental
// KV access fail loudly instead of silently passing.
function makeStorageEnv() {
  return { CLIPFORGE_BOT_D1: makeD1(), KV_ENCRYPTION_KEY: TEST_KEY, TELEGRAM_BOT_TOKEN: 't' };
}

test('describeTaskState renders awaiting_torrent_selection in plain language', () => {
  assert.equal(describeTaskState(makeStatus('j-1', 'awaiting_torrent_selection', 1)),
    'waiting for your file selection');
  assert.equal(describeTaskState(makeStatus('j-1', 'stage_a_running', 1)), 'stage_a_running');
  assert.equal(describeTaskState(null), 'queued');
  assert.match(describeTaskState(null, { unreadable: true }), /status unavailable/);
});

test('loadTaskList keeps unreadable statuses as null entries (not dropped, not fabricated)', async () => {
  const kv = makeKv();
  const env = await makeEnv(kv);
  await ensureTaskLabel(env, CHAT, 'job-dead');
  await ensureTaskLabel(env, CHAT, 'job-live');
  const restore = installFetch({
    files: { 'jobs/job-live/status.json': makeStatus('job-live', 'stage_a_running', 500) },
  });
  try {
    const { credentials, entries } = await loadTaskList(env, CHAT);
    assert.ok(credentials);
    assert.equal(entries.length, 2);
    const dead = entries.find((e) => e.jobId === 'job-dead');
    const live = entries.find((e) => e.jobId === 'job-live');
    assert.equal(dead.status, null, 'unreadable status must stay null (distinct from queued)');
    assert.equal(live.status.state, 'stage_a_running');
  } finally {
    restore();
  }
});

test('showTasks renders an unreadable status as "status unavailable", not "queued"', async () => {
  const kv = makeKv();
  const env = await makeEnv(kv);
  await ensureTaskLabel(env, CHAT, 'job-dead');
  await ensureTaskLabel(env, CHAT, 'job-live');
  const sent = [];
  const restore = installFetch({
    files: { 'jobs/job-live/status.json': makeStatus('job-live', 'stage_a_running', 500) },
    sent,
  });
  try {
    await showTasks(env, CHAT);
  } finally {
    restore();
  }
  assert.equal(sent.length, 1, 'expected exactly one Telegram message');
  const text = String(sent[0].payload.text || '');
  assert.match(text, /<b>Tasks<\/b>/);
  // The dead job (404 status) must NOT read as a plain "queued" row…
  const deadLine = text.split('\n').find((l) => l.includes('A</b> —'));
  assert.ok(deadLine, 'expected a row for label A');
  assert.match(deadLine, /status unavailable/);
  assert.doesNotMatch(deadLine, /queued/);
  // …while the genuinely running job keeps its real state.
  const liveLine = text.split('\n').find((l) => l.includes('B</b> —'));
  assert.match(liveLine, /stage_a_running/);
});

// ---------------------------------------------------------------------------
// Bug 2 fix coverage: freed labels are reused, the list sorts by creation
// time (not label order), and every /tasks row offers a delete button.
// ---------------------------------------------------------------------------

test('a freed label is reused by the next new task (lowest free letter wins)', async () => {
  const { ensureTaskLabel, removeTask } = await import('../src/storage.js');
  const env = makeStorageEnv();
  assert.equal(await ensureTaskLabel(env, CHAT, 'job-1'), 'A');
  assert.equal(await ensureTaskLabel(env, CHAT, 'job-2'), 'B');
  assert.equal(await ensureTaskLabel(env, CHAT, 'job-3'), 'C');
  assert.equal(await removeTask(env, CHAT, 'B', 'job-2'), true);
  assert.equal(await ensureTaskLabel(env, CHAT, 'job-4'), 'B', 'freed label B must be reused');
  assert.equal(await ensureTaskLabel(env, CHAT, 'job-5'), 'D');
  assert.equal(await ensureTaskLabel(env, CHAT, 'job-4'), 'B', 'same job keeps its label');
});

test('deleting the earliest label frees A for reuse', async () => {
  const { ensureTaskLabel, removeTask } = await import('../src/storage.js');
  const env = makeStorageEnv();
  await ensureTaskLabel(env, CHAT, 'job-1');
  await ensureTaskLabel(env, CHAT, 'job-2');
  await removeTask(env, CHAT, 'A', 'job-1');
  assert.equal(await ensureTaskLabel(env, CHAT, 'job-9'), 'A');
});

test('sortTaskEntries orders by creation time, newest first (label order ignored)', async () => {
  const { sortTaskEntries } = await import('../src/commands/tasks.js');
  const sorted = sortTaskEntries([
    { label: 'B', jobId: 'j-b', status: makeStatus('j-b', 'queued', 100) },
    { label: 'A', jobId: 'j-a', status: makeStatus('j-a', 'queued', 300) },
    { label: 'C', jobId: 'j-c', status: makeStatus('j-c', 'queued', 200) },
    { label: 'D', jobId: 'j-d', status: null },
  ]);
  assert.deepEqual(sorted.map((e) => e.label), ['A', 'C', 'B', 'D']);
});

test('showTasks sorts rows by creation time and offers a delete button per row', async () => {
  const kv = makeKv();
  const env = await makeEnv(kv);
  await ensureTaskLabel(env, CHAT, 'job-old');
  await ensureTaskLabel(env, CHAT, 'job-new');
  await ensureTaskLabel(env, CHAT, 'job-mid');
  const sent = [];
  const restore = installFetch({
    files: {
      'jobs/job-old/status.json': makeStatus('job-old', 'stage_a_running', 100),
      'jobs/job-new/status.json': makeStatus('job-new', 'stage_a_running', 300),
      'jobs/job-mid/status.json': makeStatus('job-mid', 'stage_a_running', 200),
    },
    sent,
  });
  try { await showTasks(env, CHAT); } finally { restore(); }
  const text = String(sent[0].payload.text || '');
  const order = ['B</b> —', 'C</b> —', 'A</b> —'].map((needle) => text.indexOf(needle));
  assert.ok(order[0] !== -1 && order[0] < order[1] && order[1] < order[2], `rows newest-first, got ${order}`);
  const callbacks = JSON.stringify(sent[0].payload.reply_markup || {});
  for (const label of ['A', 'B', 'C']) assert.match(callbacks, new RegExp(`task:del:${label}`));
});

// ---------------------------------------------------------------------------
// Bug 3 fix coverage: a job in awaiting_torrent_selection must surface a
// clear, actionable file-selection prompt — never the raw state string.
// (The pipeline-side state transition was verified intact: stage_a/ingest.py
// writes torrent-selection.json + sets awaiting_torrent_selection via
// pipeline.status for multi-file torrents, exactly like the legacy flow.)
// ---------------------------------------------------------------------------

test('Bug 3: /tasks renders the torrent-selection state as plain language', async () => {
  const kv = makeKv();
  const env = await makeEnv(kv);
  await ensureTaskLabel(env, CHAT, 'job-tor');
  const sent = [];
  const restore = installFetch({
    files: { 'jobs/job-tor/status.json': makeStatus('job-tor', 'awaiting_torrent_selection', 100) },
    sent,
  });
  try { await showTasks(env, CHAT); } finally { restore(); }
  const text = String(sent[0].payload.text || '');
  assert.match(text, /waiting for your file selection/i);
  assert.doesNotMatch(text, /awaiting_torrent_selection/);
  const kb = JSON.stringify(sent[0].payload.reply_markup || {});
  assert.match(kb, /waiting for your file selection/i);
});

test('Bug 3: the task keyboard offers the torrent chooser while selection is pending', async () => {
  const { taskKeyboard } = await import('../src/runtime.js');
  const kb = JSON.stringify(taskKeyboard(makeStatus('j-1', 'awaiting_torrent_selection', 1), 'A'));
  assert.match(kb, /Choose video file/);
  assert.match(kb, /task:tsel:A:0/);
});

test('Bug 3: the task view tells the operator plainly to pick a file', async () => {
  const { showTask } = await import('../src/runtime.js');
  const kv = makeKv();
  const env = await makeEnv(kv);
  await ensureTaskLabel(env, CHAT, 'job-tor2');
  const sent = [];
  const restore = installFetch({
    files: { 'jobs/job-tor2/status.json': makeStatus('job-tor2', 'awaiting_torrent_selection', 100) },
    sent,
  });
  try { await showTask(env, CHAT, 'A'); } finally { restore(); }
  assert.equal(sent.length, 1, 'expected the task view to render');
  const text = String(sent[0].payload.text || '');
  assert.match(text, /waiting for your file selection/i);
  assert.match(text, /Choose video file/);
  const kb = JSON.stringify(sent[0].payload.reply_markup || {});
  assert.match(kb, /task:tsel:A:0/, 'chooser button must be wired to the selection screen');
});

// ---------------------------------------------------------------------------
// kv-minimization phase 4: the Feature 4 unseen-task marker (🆕) is removed
// entirely — no storage, no UI branch. The assertions below pin the
// post-removal rendering: rows show only the canonical state glyph.
// ---------------------------------------------------------------------------

test('kv-min phase 4: /tasks rows carry no 🆕 marker (feature removed)', async () => {
  const kv = makeKv();
  const env = await makeEnv(kv);
  await ensureTaskLabel(env, CHAT, 'job-a');
  await ensureTaskLabel(env, CHAT, 'job-b');
  const sent = [];
  const restore = installFetch({
    files: {
      'jobs/job-a/status.json': makeStatus('job-a', 'stage_a_running', 200),
      'jobs/job-b/status.json': makeStatus('job-b', 'stage_a_running', 100),
    },
    sent,
  });
  try { await showTasks(env, CHAT); } finally { restore(); }
  const text = String(sent[0].payload.text || '');
  assert.doesNotMatch(text, /🆕/, 'no row may carry the removed unseen marker');
  assert.match(text, /<b>⚙️ A<\/b>/);
  assert.match(text, /<b>⚙️ B<\/b>/);
});

test('bug-60: /tasks shows a live working bar while any job is active, and per-state glyphs', async () => {
  const kv = makeKv();
  const env = await makeEnv(kv);
  await ensureTaskLabel(env, CHAT, 'job-active');
  await ensureTaskLabel(env, CHAT, 'job-fin');
  const sent = [];
  const restore = installFetch({
    files: {
      'jobs/job-active/status.json': makeStatus('job-active', 'stage_b_running', 200),
      'jobs/job-fin/status.json': makeStatus('job-fin', 'complete', 100),
    },
    sent,
  });
  try { await showTasks(env, CHAT); } finally { restore(); }
  const text = String(sent[0].payload.text || '');
  // The live hint line uses the shared indeterminate bar from progress.js.
  assert.match(text, /working — open a task for its live progress/, 'active list must carry the live hint line');
  // Canonical per-state glyphs: stage B renders 🎬, complete renders ✅.
  assert.match(text, /<b>🎬 A<\/b>/, 'stage_b_running row must carry the 🎬 glyph');
  assert.match(text, /<b>✅ B<\/b>/, 'complete row must carry the ✅ glyph');
});

test('kv-min phase 4: storage.js no longer exports task-seen helpers', async () => {
  const storage = await import('../src/storage.js');
  assert.equal(typeof storage.markTaskSeen, 'undefined', 'markTaskSeen must be deleted');
  assert.equal(typeof storage.isTaskSeen, 'undefined', 'isTaskSeen must be deleted');
});
