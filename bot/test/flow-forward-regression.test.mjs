/**
 * restore-bare-send-recognition — regression suite for the input-recognition
 * defect class: "the bot cannot tell that a chat is mid-flow, so a bare send
 * or a forward at an input step falls through to home / the reject branch".
 *
 * HISTORY (important, do not re-litigate):
 * - Commit 8f9e46e responded to the confirmed production bug by rewriting
 *   prompt COPY to demand the user manually reply. That was the WRONG fix:
 *   Telegram only attaches `message.reply_to_message` when the user uses the
 *   reply-compose UI, so a forward / a file picked from saved messages / a
 *   plain send never carries it no matter what the prompt says.
 * - This suite is the rewritten form of that commit's tests. The assertions
 *   that pinned the reply-only copy are inverted to pin the RESTORED
 *   send-or-forward copy; the harness bugs they hid are fixed (see below).
 *
 * HARNESS BUG FIXED HERE: the 8f9e46e version of the genuine-reply test
 * seeded NO credentials, so handleTorrentDocument's requireCredentials()
 * returned falsy, handleFlowReply returned undefined, and the message fell
 * through to the reject branch — the test passed only because the old reject
 * copy happened not to contain the string it asserted against ("not
 * expected"). It never actually verified reply routing. Credentials are now
 * seeded (as tasks.test.mjs does) and the GitHub contents PUT is stubbed, so
 * the reply path is genuinely exercised end to end.
 *
 * Harness shape mirrors stateless-cancel.test.mjs / tasks.test.mjs: Map-backed
 * KV stub, node:sqlite D1 harness over the REAL migration files, stubbed
 * global fetch (GitHub reads 404 / writes 200, Telegram calls recorded).
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import worker from '../src/index.js';
import { makePayload, withFlowMarker } from '../src/flow.js';
import { newWizard, encodeWizardToken, stepPrompt } from '../src/wizard.js';
import { putCredentials } from '../src/storage.js';
import { makeD1 } from './helpers/d1.mjs';

const CHAT = 5150;
const TEST_KEY = Buffer.alloc(32, 7).toString('base64');
const BOT_USER = { id: 9001, is_bot: true, first_name: 'ClipForge' };

function makeKv() {
  const map = new Map();
  return {
    get: async (k) => (map.has(k) ? map.get(k) : null),
    put: async (k, v) => { map.set(k, String(v)); },
    delete: async (k) => { map.delete(k); },
    _map: map,
  };
}

async function makeEnv(kv = makeKv()) {
  const env = {
    CLIPFORGE_BOT_KV: kv,
    CLIPFORGE_BOT_D1: makeD1(),
    KV_ENCRYPTION_KEY: TEST_KEY,
    TELEGRAM_BOT_TOKEN: 'test-token',
    TELEGRAM_WEBHOOK_SECRET: 'test-secret',
  };
  // Without this the wizard's requireCredentials() short-circuits every
  // media handler and the flow silently falls through — the exact hole that
  // made the 8f9e46e reply test vacuous.
  await putCredentials(env, CHAT, { githubPat: 'pat-not-real', repo: 'owner/repo', geminiKeys: [] });
  return env;
}

function makeCtx() {
  const pending = [];
  return { waitUntil: (p) => pending.push(Promise.resolve(p)), _pending: pending };
}

function installFetch({ sent = [] } = {}) {
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    const method = String((init && init.method) || 'GET').toUpperCase();
    // Telegram file download (getFile -> file/bot<token>/<path>) for .torrent.
    if (u.startsWith('https://api.telegram.org/file/bot')) {
      return new Response(new Uint8Array([100, 56, 58, 97, 110, 110, 111]), { status: 200 });
    }
    if (u.startsWith('https://api.telegram.org/')) {
      const m = u.split('/').pop();
      let payload = {};
      try { payload = init && init.body ? JSON.parse(String(init.body)) : {}; } catch { payload = {}; }
      sent.push({ method: m, payload });
      if (m === 'getFile') {
        return new Response(JSON.stringify({ ok: true, result: { file_id: 'f1', file_path: 'documents/source.torrent' } }), { status: 200 });
      }
      return new Response(JSON.stringify({ ok: true, result: { message_id: sent.length } }), { status: 200 });
    }
    if (u.startsWith('https://api.github.com/')) {
      // Reads 404 (nothing staged yet); writes succeed so the wizard advances.
      if (method === 'PUT' || method === 'POST') {
        return new Response(JSON.stringify({ content: { sha: 'deadbeef' }, commit: { sha: 'c0ffee' } }), { status: 201 });
      }
      return new Response(JSON.stringify({ message: 'Not Found' }), { status: 404 });
    }
    throw new Error('unexpected fetch: ' + u);
  };
  return () => { globalThis.fetch = original; };
}

async function drive(env, update) {
  const req = new Request('https://bot.test/', {
    method: 'POST',
    headers: { 'X-Telegram-Bot-Api-Secret-Token': 'test-secret' },
    body: JSON.stringify(update),
  });
  const ctx = makeCtx();
  const res = await worker.fetch(req, env, ctx);
  await Promise.all(ctx._pending);
  return res;
}

/** A genuine reply to the bot's own wzs source-step prompt. */
function wizardPromptReply(userMessage, wizard) {
  const payload = makePayload('wzs', encodeWizardToken(wizard));
  const url = `https://cf.invalid/f/${encodeURIComponent(payload)}`;
  const wizardPrompt = {
    message_id: 50,
    from: BOT_USER,
    text: withFlowMarker('prompt', payload),
    // Telegram encodes the marker as a text_link entity; reconstruct it so
    // extractFlowPayload can read the payload out of the replied-to message.
    entities: [{ type: 'text_link', url, offset: 0, length: 1 }],
  };
  return { ...userMessage, reply_to_message: wizardPrompt };
}

function textOf(sent) {
  return sent.map((c) => String(c.payload.text || c.payload.caption || '')).join('\n');
}

// ---------------------------------------------------------------------- //
// Copy contract: the reply-only wording from 8f9e46e must stay reverted.  //
// ---------------------------------------------------------------------- //

test('source-step prompt copy invites a bare send OR forward (8f9e46e reply-only copy stays reverted)', () => {
  const text = stepPrompt(newWizard()).text;
  assert.match(text, /send or forward/i, 'must invite a plain send or a forward');
  assert.doesNotMatch(text, /Reply to this message/i,
    'must NOT demand a manual reply — the awaiting_input marker makes that unnecessary');
  assert.doesNotMatch(text, /cannot be matched to this wizard/i,
    'the 8f9e46e "forwards do not work" warning must be gone');
});

// ---------------------------------------------------------------------- //
// The genuine-reply path must keep working EXACTLY as before.             //
// ---------------------------------------------------------------------- //

test('a .torrent REPLY to the wzs source-step prompt routes to the wizard (not the reject branch)', async () => {
  const env = await makeEnv();
  const sent = [];
  const restore = installFetch({ sent });
  try {
    const reply = wizardPromptReply({
      message_id: 51,
      from: { id: 1, is_bot: false },
      chat: { id: CHAT, type: 'private' },
      document: { file_name: 'source.torrent', file_id: 'f1', file_unique_id: 'u1', file_size: 2048 },
    }, newWizard());
    const res = await drive(env, { update_id: 1, message: reply });
    assert.equal(res.status, 200);
    const allText = textOf(sent);
    assert.ok(!/was not expected/i.test(allText), 'must NOT hit the reject branch');
    assert.match(allText, /step 2\/5|focus/i, 'wizard should advance past the source step');
  } finally { restore(); }
});

test('a text REPLY carrying a link routes to the wizard and advances it', async () => {
  const env = await makeEnv();
  const sent = [];
  const restore = installFetch({ sent });
  try {
    const reply = wizardPromptReply({
      message_id: 52,
      from: { id: 1, is_bot: false },
      chat: { id: CHAT, type: 'private' },
      text: 'https://example.com/video.mp4',
    }, newWizard());
    await drive(env, { update_id: 2, message: reply });
    const allText = textOf(sent);
    assert.ok(!/was not expected/i.test(allText), 'a genuine reply must never hit the reject branch');
    assert.match(allText, /step 2\/5|focus/i, 'wizard should advance to the focus step');
  } finally { restore(); }
});

// ---------------------------------------------------------------------- //
// With NO flow open, a bare send/forward still gets the legacy rejection. //
// (The awaiting_input steps add the mid-flow acceptance path on top.)     //
// ---------------------------------------------------------------------- //

test('a forwarded .torrent with NO flow open gets the legacy "not expected" acknowledgement, not silence', async () => {
  const env = await makeEnv();
  const sent = [];
  const restore = installFetch({ sent });
  try {
    const forwarded = {
      message_id: 60,
      from: { id: 1, is_bot: false },
      chat: { id: CHAT, type: 'private' },
      forward_from_chat: { id: -100, type: 'channel' },
      document: { file_name: 'movie.torrent', file_id: 'f2', file_unique_id: 'u2', file_size: 4096 },
      // NOTE: no reply_to_message — and no awaiting_input row is open either.
    };
    const res = await drive(env, { update_id: 3, message: forwarded });
    assert.equal(res.status, 200);
    const allText = textOf(sent);
    assert.match(allText, /was not expected/i, 'must acknowledge explicitly rather than fall silent');
    assert.match(allText, /send or forward the video/i, 'legacy send-or-forward framing is restored');
    assert.doesNotMatch(allText, /reply to the step-1 prompt/i, '8f9e46e reply-only guidance must be gone');
  } finally { restore(); }
});

test('bare (non-reply) audio with no music batch open is still acknowledged loudly, without reply-only guidance', async () => {
  const env = await makeEnv();
  const sent = [];
  const restore = installFetch({ sent });
  try {
    await drive(env, {
      update_id: 4,
      message: {
        message_id: 61,
        from: { id: 1, is_bot: false },
        chat: { id: CHAT, type: 'private' },
        audio: { file_id: 'a1', file_unique_id: 'au1', file_size: 4096, file_name: 'track.mp3' },
      },
    });
    const allText = textOf(sent);
    assert.match(allText, /was not expected/i, 'the loud branch from 8f9e46e is KEPT (silence was a real defect)');
    assert.doesNotMatch(allText, /reply to that prompt/i, 'but its reply-only guidance is reverted');
  } finally { restore(); }
});

test('reply-less text with no flow open falls through to home (unchanged)', async () => {
  const env = await makeEnv();
  const sent = [];
  const restore = installFetch({ sent });
  try {
    await drive(env, {
      update_id: 5,
      message: {
        message_id: 70,
        from: { id: 1, is_bot: false },
        chat: { id: CHAT, type: 'private' },
        text: 'https://example.com/video.mp4',
      },
    });
    const allText = textOf(sent);
    assert.ok(!/step 2\/5|focus/i.test(allText), 'text with no flow open must NOT advance the wizard');
  } finally { restore(); }
});
