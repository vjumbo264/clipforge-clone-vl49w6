// Unit tests for the bug-51 Shadow Clone job lifecycle (bot/src/storage.js
// clone-job records + bot/src/github.js pollShadowCloneJob state machine).
//
// Bug-51 coverage: Shadow Clone creation previously stalled forever at
// "Preparing repository…" because the webhook request awaited a multi-minute
// GitHub Actions copy run and Cloudflare killed the invocation mid-wait — no
// success message, no error message (a kill is not a rejection, so no catch
// could run). The fix stages the in-flight creation in KV and resumes it
// from the cron trigger. These tests pin the two halves of that guarantee:
//   1. A staged job survives process death (KV round-trip, PAT re-encrypted
//      under the chat's credential AAD, scan-set index maintained).
//   2. pollShadowCloneJob maps every copy-status outcome — running, complete,
//      reported-failed, never-started, stalled, deadline — to exactly one
//      decision, so the cron tick can always drive a job to a terminal
//      user-facing message.
//
// Uses node's built-in test runner with a node:sqlite-backed
// CLIPFORGE_BOT_D1 harness (clone-job records moved from KV to D1 in the
// kv-minimization migration) and a stubbed global fetch standing in for the
// GitHub HTTP calls (the same pattern as tasks.test.mjs / relay.test.mjs).
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  CLONE_COPY_DEADLINE_MS, CLONE_COPY_START_MS, CLONE_COPY_STALL_MS, pollShadowCloneJob
} from '../src/github.js';
import { getCloneJob, putCloneJob, deleteCloneJob, listCloneJobChatIds } from '../src/storage.js';
import { makeD1 } from './helpers/d1.mjs';

const CHAT = 4242;
const TEST_KEY = Buffer.alloc(32, 7).toString('base64');
const NOW = Date.now();

// The D1-backed clone-job functions never touch KV; providing only the D1
// harness makes any accidental KV access fail loudly.
function makeEnv() {
  return { CLIPFORGE_BOT_D1: makeD1(), KV_ENCRYPTION_KEY: TEST_KEY };
}

function makeJob(overrides = {}) {
  return {
    chatId: CHAT,
    githubPat: 'pat-not-real',
    repo: 'owner/clipforge-clone-test',
    login: 'owner',
    name: 'clipforge-clone-test',
    branch: 'main',
    sourceSha: 'a'.repeat(40),
    bootstrapCommitSha: 'b'.repeat(40),
    totalFiles: 100,
    startedAt: NOW,
    lastAdvanceAt: NOW,
    lastStatusKey: '',
    runId: null,
    finalizeFailedAt: 0,
    ...overrides,
  };
}

// Stub fetch: answers the clone-status contents read from `status` (null =>
// 404, i.e. the status file does not exist yet) and the Actions run-list
// reads from `runIds`. Records every call URL into `calls`.
function installFetch({ status = null, runIds = [], calls = [] } = {}) {
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    calls.push(u);
    if (u.includes('/contents/.clipforge-clone-status.json')) {
      if (status === null) {
        return new Response(JSON.stringify({ message: 'Not Found' }), { status: 404 });
      }
      return new Response(JSON.stringify({
        content: Buffer.from(JSON.stringify(status), 'utf8').toString('base64'),
        sha: 'abc',
      }), { status: 200 });
    }
    if (u.includes('/actions/runs') || u.includes('/actions/workflows/')) {
      return new Response(JSON.stringify({
        workflow_runs: runIds.map((id) => ({ id, event: 'workflow_dispatch' })),
      }), { status: 200 });
    }
    throw new Error('unexpected fetch: ' + u);
  };
  return () => { globalThis.fetch = original; };
}

test('clone job D1 round-trip preserves the job and re-encrypts the PAT', async () => {
  const env = makeEnv();
  await putCloneJob(env, CHAT, makeJob());
  const stored = env.CLIPFORGE_BOT_D1._query('SELECT pat_envelope FROM clone_jobs WHERE chat_id = ?', CHAT)[0];
  assert.ok(stored, 'job record stored');
  assert.ok(!stored.pat_envelope.includes('pat-not-real'), 'raw PAT never appears in the stored envelope');
  const job = await getCloneJob(env, CHAT);
  assert.equal(job.githubPat, 'pat-not-real', 'PAT decrypts back');
  assert.equal(job.repo, 'owner/clipforge-clone-test');
  assert.equal(job.totalFiles, 100);
  assert.equal(job.startedAt, NOW);
  assert.deepEqual(await listCloneJobChatIds(env), [CHAT], 'chat id indexed for the cron sweep');
});

test('clone job delete removes the record; the table itself is the scan set', async () => {
  const env = makeEnv();
  await putCloneJob(env, CHAT, makeJob());
  await putCloneJob(env, 9999, makeJob({ chatId: 9999 }));
  assert.deepEqual((await listCloneJobChatIds(env)).sort(), [CHAT, 9999].sort());
  await deleteCloneJob(env, CHAT);
  assert.equal(await getCloneJob(env, CHAT), null);
  assert.deepEqual(await listCloneJobChatIds(env), [9999]);
});

test('getCloneJob returns null for a record whose PAT envelope cannot be decrypted', async () => {
  const env = makeEnv();
  env.CLIPFORGE_BOT_D1.prepare(
    `INSERT INTO clone_jobs (chat_id, pat_envelope, repo, login, name, branch, source_sha,
       bootstrap_commit_sha, total_files, started_at, last_advance_at, last_status_key, run_id, finalize_failed_at)
     VALUES (?, 'not-json', 'o/r', 'o', 'r', 'main', '', '', 0, 1, 1, '', NULL, 0)`
  ).bind(CHAT).run();
  assert.equal(await getCloneJob(env, CHAT), null);
});

test('putCloneJob upserts: a re-staged job replaces the previous row in place', async () => {
  const env = makeEnv();
  await putCloneJob(env, CHAT, makeJob({ runId: null, lastStatusKey: 'copying:1:100' }));
  await putCloneJob(env, CHAT, makeJob({ runId: 4242, lastStatusKey: 'copying:50:100', finalizeFailedAt: 123 }));
  assert.equal(env.CLIPFORGE_BOT_D1._query('SELECT chat_id FROM clone_jobs WHERE chat_id = ?', CHAT).length, 1, 'one row per chat');
  const job = await getCloneJob(env, CHAT);
  assert.equal(job.runId, '4242');
  assert.equal(job.lastStatusKey, 'copying:50:100');
  assert.equal(job.finalizeFailedAt, 123);
});

test('pollShadowCloneJob: copying status stays running and advances the stall clock', async () => {
  const restore = installFetch({ status: { state: 'copying', done: 40, total: 100 } });
  try {
    const outcome = await pollShadowCloneJob(makeJob({ startedAt: NOW - 60000, lastAdvanceAt: NOW - 60000 }));
    assert.equal(outcome.status, 'running');
    assert.equal(outcome.job.lastStatusKey, 'copying:40:100');
    assert.ok(outcome.job.lastAdvanceAt > NOW - 60000, 'lastAdvanceAt advanced');
  } finally { restore(); }
});

test('pollShadowCloneJob: complete resolves the run id for finalization', async () => {
  const restore = installFetch({ status: { state: 'complete', done: 100, total: 100 }, runIds: [555] });
  try {
    const outcome = await pollShadowCloneJob(makeJob());
    assert.equal(outcome.status, 'complete');
    assert.equal(outcome.job.runId, 555);
  } finally { restore(); }
});

test('pollShadowCloneJob: workflow-reported failure is terminal with the workflow error text', async () => {
  const restore = installFetch({ status: { state: 'failed', done: 40, total: 100, error: 'push rejected' }, runIds: [777] });
  try {
    const outcome = await pollShadowCloneJob(makeJob());
    assert.equal(outcome.status, 'failed');
    assert.match(String(outcome.error.message), /copy workflow failed: push rejected/);
    assert.equal(outcome.job.runId, 777, 'run id resolved so the dead run can be cancelled');
  } finally { restore(); }
});

test('pollShadowCloneJob: no status file past the start budget means the run never started', async () => {
  const restore = installFetch({ status: null, runIds: [] });
  try {
    const running = await pollShadowCloneJob(makeJob({ startedAt: NOW - (CLONE_COPY_START_MS - 1000) }));
    assert.equal(running.status, 'running', 'still within the start budget');
    const failed = await pollShadowCloneJob(makeJob({ startedAt: NOW - CLONE_COPY_START_MS - 1000 }));
    assert.equal(failed.status, 'failed');
    assert.match(String(failed.error.message), /never started/);
  } finally { restore(); }
});

test('pollShadowCloneJob: an unchanging status past the stall budget is a dead run', async () => {
  const restore = installFetch({ status: { state: 'copying', done: 40, total: 100 }, runIds: [42] });
  try {
    const outcome = await pollShadowCloneJob(makeJob({
      startedAt: NOW - CLONE_COPY_STALL_MS - 60000,
      lastAdvanceAt: NOW - CLONE_COPY_STALL_MS - 1000,
      lastStatusKey: 'copying:40:100',
    }));
    assert.equal(outcome.status, 'failed');
    assert.match(String(outcome.error.message), /stopped reporting progress/);
    assert.equal(outcome.job.runId, 42);
  } finally { restore(); }
});

test('pollShadowCloneJob: the absolute deadline terminates even a freshly advancing run', async () => {
  const restore = installFetch({ status: { state: 'copying', done: 99, total: 100 }, runIds: [] });
  try {
    const outcome = await pollShadowCloneJob(makeJob({ startedAt: NOW - CLONE_COPY_DEADLINE_MS - 1000 }));
    assert.equal(outcome.status, 'failed');
    assert.match(String(outcome.error.message), /took too long/);
  } finally { restore(); }
});
