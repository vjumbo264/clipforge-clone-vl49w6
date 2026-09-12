// Unit tests for bot/src/jobs.js. Uses node's built-in test runner (node --test).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  STATUS_VERSION,
  VALID_STATES,
  TERMINAL_STATES,
  isValidJobId,
  isTerminal,
  canTransition,
  newStatus,
  mergeStatus,
  serializeStatus,
  statusPath,
} from '../src/jobs.js';

test('isValidJobId accepts and rejects the right shapes', () => {
  for (const good of ['manual-1', 'series-abc-p2', 'a', 'A_B.c-1']) {
    assert.equal(isValidJobId(good), true, good);
  }
  for (const bad of ['', 'has space', 'slash/inside', 123, null, 'x'.repeat(121)]) {
    assert.equal(isValidJobId(bad), false, JSON.stringify(bad));
  }
});

test('isTerminal covers the three terminal states', () => {
  for (const s of ['complete', 'error', 'cancelled']) assert.equal(isTerminal(s), true, s);
  for (const s of ['queued', 'stage_a_running', 'awaiting_plan']) assert.equal(isTerminal(s), false, s);
});

test('newStatus builds a schema-conformant record', () => {
  const rec = newStatus({ job_id: 'manual-1', mode: 'manual', nowEpoch: 1000 });
  assert.equal(rec.version, STATUS_VERSION);
  assert.equal(rec.state, 'queued');
  assert.equal(rec.created_at_epoch, 1000);
  assert.equal(rec.expires_at_epoch, 1000 + 12 * 3600);
  assert.deepEqual(rec.publishing, { status: 'not_requested', posts: [], idempotency_key: '' });
  assert.equal(rec.series.enabled, false);
});

test('newStatus rejects bad mode / state / job_id', () => {
  assert.throws(() => newStatus({ job_id: 'j-1', mode: 'oops' }));
  assert.throws(() => newStatus({ job_id: 'j-1', mode: 'manual', state: 'no-such' }));
  assert.throws(() => newStatus({ job_id: 'bad space', mode: 'manual' }));
});

test('canTransition permits legal edges and blocks illegal ones', () => {
  assert.equal(canTransition('queued', 'stage_a_running'), true);
  assert.equal(canTransition('stage_a_running', 'awaiting_plan'), true);
  assert.equal(canTransition('awaiting_plan', 'stage_b_queued'), true);
  assert.equal(canTransition('stage_b_running', 'complete'), true);

  // Same-state is idempotent.
  assert.equal(canTransition('queued', 'queued'), true);

  // No leaving terminal states.
  assert.equal(canTransition('complete', 'stage_b_running'), false);
  assert.equal(canTransition('cancelled', 'stage_b_queued'), false);

  // No skipping stages.
  assert.equal(canTransition('queued', 'complete'), false);
  assert.equal(canTransition('awaiting_plan', 'stage_b_running'), false);
});

test('mergeStatus enforces the state machine', () => {
  const rec = newStatus({ job_id: 'j-1', mode: 'manual', nowEpoch: 100 });
  const running = mergeStatus(rec, { state: 'stage_a_running', message: 'go' }, { nowEpoch: 200 });
  assert.equal(running.state, 'stage_a_running');
  assert.equal(running.created_at_epoch, 100);
  assert.equal(running.updated_at_epoch, 200);
  assert.throws(() => mergeStatus(running, { state: 'complete' }, { nowEpoch: 300 }));

  const done = mergeStatus(
    mergeStatus(running, { state: 'awaiting_plan' }, { nowEpoch: 210 }),
    { state: 'stage_b_queued' },
    { nowEpoch: 220 },
  );
  const rendered = mergeStatus(
    mergeStatus(done, { state: 'stage_b_running' }, { nowEpoch: 230 }),
    { state: 'complete' },
    { nowEpoch: 240 },
  );
  assert.equal(rendered.state, 'complete');
  // Terminal -> non-terminal is refused.
  assert.throws(() => mergeStatus(rendered, { state: 'stage_b_running' }));
});

test('mergeStatus merges assets, run, publishing without dropping prior keys', () => {
  const base = newStatus({ job_id: 'j-1', mode: 'manual', nowEpoch: 100 });
  const withAssets = mergeStatus(
    base,
    { assets: { analysis_bundle_url: 'https://a' }, run: { workflow_run_id: 42 } },
    { nowEpoch: 110 },
  );
  const next = mergeStatus(
    withAssets,
    { assets: { final_mp4: 'https://b' }, run: { code_ref: 'deadbeef' }, publishing: { status: 'publishing' } },
    { nowEpoch: 120 },
  );
  assert.deepEqual(next.assets, { analysis_bundle_url: 'https://a', final_mp4: 'https://b' });
  assert.equal(next.run.workflow_run_id, 42);
  assert.equal(next.run.code_ref, 'deadbeef');
  assert.equal(next.publishing.status, 'publishing');
});

test('mergeStatus rejects invalid publishing.status', () => {
  const base = newStatus({ job_id: 'j-1', mode: 'manual', nowEpoch: 100 });
  assert.throws(() => mergeStatus(base, { publishing: { status: 'not-a-thing' } }));
});

test('serializeStatus / statusPath produce stable output', () => {
  const rec = newStatus({ job_id: 'j-1', mode: 'manual', nowEpoch: 100 });
  const text = serializeStatus(rec);
  assert.ok(text.endsWith('\n'));
  assert.equal(JSON.parse(text).job_id, 'j-1');
  assert.equal(statusPath('j-1'), 'jobs/j-1/status.json');
  assert.throws(() => statusPath('bad space'));
});

test('VALID_STATES covers the nine states from ARCHITECTURE.md §6.1', () => {
  const expected = [
    'queued',
    'stage_a_running',
    'awaiting_torrent_selection',
    'awaiting_plan',
    'stage_b_queued',
    'stage_b_running',
    'complete',
    'error',
    'cancelled',
  ];
  assert.deepEqual([...VALID_STATES], expected);
  assert.deepEqual([...TERMINAL_STATES].sort(), ['cancelled', 'complete', 'error']);
});

// fix-ttl-config-disconnect (TTL_FIX_PROGRESS.json, fix #1): a newly created
// job's expires_at_epoch must reflect the CONFIGURED TTL (the worker env var
// CLIPFORGE_JOB_TTL_SECONDS from wrangler vars, mirroring the single repo
// Actions variable CLIPFORGE_TTL_SECONDS), never a hardcoded 12h constant.
import { configuredJobTtlSeconds, DEFAULT_TTL_SECONDS } from '../src/jobs.js';

test('configuredJobTtlSeconds: env var wins, safe fallback otherwise', () => {
  assert.equal(configuredJobTtlSeconds({ CLIPFORGE_JOB_TTL_SECONDS: '172800' }), 172800);
  assert.equal(configuredJobTtlSeconds({}), DEFAULT_TTL_SECONDS);
  assert.equal(configuredJobTtlSeconds(undefined), DEFAULT_TTL_SECONDS);
  for (const bad of ['', 'abc', '0', '-3600', '  ']) {
    assert.equal(configuredJobTtlSeconds({ CLIPFORGE_JOB_TTL_SECONDS: bad }), DEFAULT_TTL_SECONDS, bad);
  }
});

test('newStatus uses the configured job TTL (48h), not the hardcoded 12h', () => {
  const env = { CLIPFORGE_JOB_TTL_SECONDS: '172800' };
  const rec = newStatus({ job_id: 'manual-1', mode: 'manual', nowEpoch: 1000, env });
  assert.equal(rec.expires_at_epoch, 1000 + 172800);
});

test('newStatus: explicit ttlSeconds beats env; no env falls back to 12h default', () => {
  const rec = newStatus({ job_id: 'manual-1', mode: 'manual', nowEpoch: 1000, ttlSeconds: 600 });
  assert.equal(rec.expires_at_epoch, 1600);
  const noEnv = newStatus({ job_id: 'manual-1', mode: 'manual', nowEpoch: 1000 });
  assert.equal(noEnv.expires_at_epoch, 1000 + DEFAULT_TTL_SECONDS);
});
