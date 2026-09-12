/**
 * kv-minimization phase 5 step 5.8 — the chat-state machinery is gone.
 *
 * These tests pin the removal contract:
 *   1. storage.js no longer exports getState/putState/clearFlow (the
 *      `user:<id>:state` KV key has no writer left anywhere).
 *   2. /cancel is conversational only: it acknowledges and shows home,
 *      and writes NO KV key at all — there is no stored flow to clear
 *      (flows are stateless, ARCHITECTURE.md §8.9).
 *   3. Cancelling with stored credentials leaves the credentials record
 *      untouched and adds no state key.
 *
 * Harness mirrors tasks.test.mjs: Map-backed KV stub, node:sqlite D1
 * harness, stubbed global fetch (GitHub reads 404, Telegram calls recorded).
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { handleCancel } from '../src/commands/cancel.js';
import { putCredentials } from '../src/storage.js';
import { makeD1 } from './helpers/d1.mjs';

const CHAT = 5150;
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

function makeEnv(kv) {
  return {
    CLIPFORGE_BOT_KV: kv,
    CLIPFORGE_BOT_D1: makeD1(),
    KV_ENCRYPTION_KEY: TEST_KEY,
    TELEGRAM_BOT_TOKEN: 'test-token',
  };
}

// Stub fetch: every GitHub read 404s (the home path is fully defensive —
// snapshot defaults, no news, no clone-sync); every Telegram call is
// recorded into `sent`.
function installFetch({ sent = [] } = {}) {
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    if (u.startsWith('https://api.github.com/')) {
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

test('kv-min phase 5 step 5.8: storage.js no longer exports chat-state helpers', async () => {
  const storage = await import('../src/storage.js');
  assert.equal(typeof storage.getState, 'undefined', 'getState must be deleted');
  assert.equal(typeof storage.putState, 'undefined', 'putState must be deleted');
  assert.equal(typeof storage.clearFlow, 'undefined', 'clearFlow must be deleted');
});

test('kv-min phase 5 step 5.8: /cancel writes no KV key — acknowledgement + onboarding home only', async () => {
  const kv = makeKv();
  const env = makeEnv(kv);
  const sent = [];
  const restore = installFetch({ sent });
  try {
    await handleCancel(env, CHAT);
  } finally {
    restore();
  }
  assert.ok(sent.length >= 2, 'the acknowledgement and the home screen must both be sent');
  assert.equal(String(sent[0].payload.text || ''), 'Cancelled. Nothing was started.',
    'first message is the cancellation acknowledgement');
  assert.match(String(sent[sent.length - 1].payload.text || ''), /ClipForge/,
    'the last message is the home/onboarding screen');
  // THE point of step 5.8: cancelling must not persist anything — the old
  // clearFlow wrote a state row; the stateless cancel writes no key at all.
  assert.equal(kv._map.size, 0, 'cancel must not write any KV key');
});

test('kv-min phase 5 step 5.8: /cancel keeps credentials and adds no state key', async () => {
  const kv = makeKv();
  const env = makeEnv(kv);
  await putCredentials(env, CHAT, { githubPat: 'pat-not-real', repo: 'motionssalt/clipforge', geminiKeys: [] });
  const sent = [];
  const restore = installFetch({ sent });
  try {
    await handleCancel(env, CHAT);
  } finally {
    restore();
  }
  // Only the encrypted credentials envelope remains — no user:<id>:state key.
  assert.equal(kv._map.size, 1, 'only the credentials key may exist after cancel');
  for (const k of kv._map.keys()) {
    assert.equal(k, `user:${CHAT}:credentials`, 'no state key may be written');
  }
  // Connected home screen rendered for the chat.
  const home = String(sent[sent.length - 1].payload.text || '');
  assert.match(home, /Connected to: <code>motionssalt\/clipforge<\/code>/,
    'connected home screen is rendered after cancel');
});
