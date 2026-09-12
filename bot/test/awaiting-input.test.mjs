/**
 * restore-bare-send-recognition — the awaiting_input marker suite.
 *
 * The defect class: after kv-minimization phase 5 removed the legacy
 * per-chat state.flow, the bot could only recognise a flow answer that
 * arrived as a genuine Telegram REPLY (message.reply_to_message). A bare
 * send, a forward, or a file picked from saved messages carries no reply
 * edge, so the input fell through to home / the reject branch. The
 * awaiting_input marker (0002_awaiting_input.sql) restores recognition:
 * when NO reply edge exists, a live per-chat marker routes the message
 * through the SAME handleFlowReply dispatch a reply would use.
 *
 * Covered here, per the contract's TESTING REQUIREMENT:
 *   - bare send AND forward (no reply_to_message) route correctly for the
 *     wizard source step (wzs), the production.json upload step (upl), and
 *     the music-collect step (mupl);
 *   - an EXPIRED row is NOT honored (falls through to home/reject as today);
 *   - PRECEDENCE: a genuine reply to an older prompt wins over a newer
 *     awaiting_input row;
 *   - an ADVANCED flow re-anchors its row (no stale step-1 double-fire) and
 *     a COMPLETED flow clears it.
 * The genuine-reply pins live in flow-forward-regression.test.mjs and are
 * not weakened here.
 *
 * Harness shape mirrors flow-forward-regression.test.mjs: Map-backed KV,
 * node:sqlite D1 over the REAL migration files, stubbed global fetch.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import worker from '../src/index.js';
import { encodeToken, makePayload, withFlowMarker } from '../src/flow.js';
import { newWizard, encodeWizardToken } from '../src/wizard.js';
import { ensureTaskLabel, putCredentials, putAwaitingInput, getAwaitingInput, AWAITING_INPUT_TTL_SECONDS } from '../src/storage.js';
import { makeD1 } from './helpers/d1.mjs';

const CHAT = 5150;
const TEST_KEY = Buffer.alloc(32, 7).toString('base64');
const BOT_USER = { id: 9001, is_bot: true, first_name: 'ClipForge' };
const NOW = Math.floor(Date.now() / 1000);

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
  // Seed credentials so requireCredentials() does not short-circuit the
  // handlers (the hole that made the 8f9e46e reply test vacuous).
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

function textOf(sent) {
  return sent.map((c) => String(c.payload.text || c.payload.caption || '')).join('\n');
}

/** The bot's own marker-bearing prompt message, for a genuine reply. */
function botPrompt(messageId, payload) {
  return {
    message_id: messageId,
    from: BOT_USER,
    text: withFlowMarker('prompt', payload),
    entities: [{ type: 'text_link', url: `https://cf.invalid/f/${encodeURIComponent(payload)}`, offset: 0, length: 1 }],
  };
}

// ---------------------------------------------------------------------- //
// 1. Wizard source step — a bare FORWARD must route into the wizard.      //
//    (This is the exact confirmed production bug 8f9e46e mis-"fixed".)    //
// ---------------------------------------------------------------------- //

test('bare forward of a .torrent with a live wzs marker routes to the wizard source step', async () => {
  const env = await makeEnv();
  const wizard = newWizard(); // step: source
  await putAwaitingInput(env, CHAT, 'wzs', makePayload('wzs', encodeWizardToken(wizard)));
  const sent = [];
  const restore = installFetch({ sent });
  try {
    const res = await drive(env, {
      update_id: 100,
      message: {
        message_id: 101,
        from: { id: 1, is_bot: false },
        chat: { id: CHAT, type: 'private' },
        forward_from_chat: { id: -100, type: 'channel' }, // a forward: no reply edge possible
        document: { file_name: 'movie.torrent', file_id: 'f2', file_unique_id: 'u2', file_size: 4096 },
      },
    });
    assert.equal(res.status, 200);
    const allText = textOf(sent);
    assert.ok(!/was not expected/i.test(allText), 'must NOT hit the reject branch when a wizard is awaiting a source');
    assert.match(allText, /focus|step 2/i, 'wizard should advance past the source step');
  } finally { restore(); }
});

test('bare text link with a live wzs marker advances the wizard (no reply needed)', async () => {
  const env = await makeEnv();
  await putAwaitingInput(env, CHAT, 'wzs', makePayload('wzs', encodeWizardToken(newWizard())));
  const sent = [];
  const restore = installFetch({ sent });
  try {
    await drive(env, {
      update_id: 101,
      message: {
        message_id: 102,
        from: { id: 1, is_bot: false },
        chat: { id: CHAT, type: 'private' },
        text: 'https://example.com/video.mp4', // a plain send, not a reply
      },
    });
    const allText = textOf(sent);
    assert.match(allText, /focus|step 2/i, 'wizard should advance to the focus step on a bare link');
    assert.ok(!/was not expected/i.test(allText), 'must NOT fall through');
  } finally { restore(); }
});

// ---------------------------------------------------------------------- //
// 2. production.json upload step (upl) — bare .json file routes.          //
// ---------------------------------------------------------------------- //

test('bare .json document with a live upl marker routes to the plan upload handler', async () => {
  const env = await makeEnv();
  // A task must exist for the label to resolve to a jobId.
  const label = await ensureTaskLabel(env, CHAT, 'job-upl-1');
  await putAwaitingInput(env, CHAT, 'upl', makePayload('upl', label));
  const sent = [];
  const restore = installFetch({ sent });
  try {
    // A single-bubble, brace-balanced production.json as a pasted file.
    const plan = JSON.stringify({ schema: 'clipforge.production_plan/v1', parts: [] });
    await drive(env, {
      update_id: 102,
      message: {
        message_id: 103,
        from: { id: 1, is_bot: false },
        chat: { id: CHAT, type: 'private' },
        document: { file_name: 'production.json', file_id: 'pj1', file_unique_id: 'pju1', file_size: plan.length },
        // no reply_to_message — a plain send of the file
      },
    });
    const allText = textOf(sent);
    // It must be claimed by the upload handler (acknowledged as a plan),
    // NOT rejected as an unexpected video/document.
    assert.ok(!/was not expected/i.test(allText), 'the plan file must not be rejected as unexpected');
  } finally { restore(); }
});

// ---------------------------------------------------------------------- //
// 3. Music-collect step (mupl) — a bare/forwarded audio is collected.     //
// ---------------------------------------------------------------------- //

test('bare audio with a live mupl marker is collected, not rejected as unexpected', async () => {
  const env = await makeEnv();
  await putAwaitingInput(env, CHAT, 'mupl', makePayload('mupl', encodeToken({ u: [] })));
  const sent = [];
  const restore = installFetch({ sent });
  try {
    await drive(env, {
      update_id: 103,
      message: {
        message_id: 104,
        from: { id: 1, is_bot: false },
        chat: { id: CHAT, type: 'private' },
        forward_from: { id: 42 }, // a forwarded track — no reply edge
        audio: { file_id: 'a9', file_unique_id: 'au9', file_size: 4096, file_name: 'track.mp3' },
      },
    });
    const allText = textOf(sent);
    assert.ok(!/was not expected/i.test(allText), 'a track must be collected, not rejected, while the batch is open');
    assert.match(allText, /collected/i, 'the batch re-anchor prompt should report the collected count');
  } finally { restore(); }
});

// ---------------------------------------------------------------------- //
// 4. Expiry — a row past its expires_at is NOT honored.                   //
// ---------------------------------------------------------------------- //

test('an EXPIRED awaiting_input row is not honored — bare send falls through as today', async () => {
  const env = await makeEnv();
  // expires_at in the PAST: putAwaitingInput with an already-expired clock.
  await putAwaitingInput(env, CHAT, 'wzs', makePayload('wzs', encodeWizardToken(newWizard())), NOW - AWAITING_INPUT_TTL_SECONDS - 60);
  const sent = [];
  const restore = installFetch({ sent });
  try {
    await drive(env, {
      update_id: 104,
      message: {
        message_id: 105,
        from: { id: 1, is_bot: false },
        chat: { id: CHAT, type: 'private' },
        document: { file_name: 'movie.torrent', file_id: 'f3', file_unique_id: 'u3', file_size: 4096 },
      },
    });
    const allText = textOf(sent);
    assert.match(allText, /was not expected/i, 'an expired marker must NOT route the file into the wizard');
    assert.ok(!/focus|step 2/i.test(allText), 'the wizard must NOT advance on an expired marker');
  } finally { restore(); }
  // The expired row is lazy-swept (deleted on read).
  assert.equal(await getAwaitingInput(env, CHAT), null, 'the expired row should be deleted on read');
});

// ---------------------------------------------------------------------- //
// 5. Precedence — a genuine reply beats a newer awaiting_input row.       //
// ---------------------------------------------------------------------- //

test('a genuine reply to an OLDER prompt wins over a newer awaiting_input row', async () => {
  const env = await makeEnv();
  // A newer mupl marker is open…
  await putAwaitingInput(env, CHAT, 'mupl', makePayload('mupl', encodeToken({ u: [] })));
  const sent = [];
  const restore = installFetch({ sent });
  try {
    // …but the user replies to the OLDER wzs source prompt. The reply edge
    // must win: the link routes to the WIZARD, not the music handler.
    const reply = {
      message_id: 106,
      from: { id: 1, is_bot: false },
      chat: { id: CHAT, type: 'private' },
      text: 'https://example.com/video.mp4',
      reply_to_message: botPrompt(50, makePayload('wzs', encodeWizardToken(newWizard()))),
    };
    await drive(env, { update_id: 105, message: reply });
    const allText = textOf(sent);
    assert.match(allText, /focus|step 2/i, 'the reply must route to the wizard (reply wins over the mupl marker)');
    assert.ok(!/collected|add music/i.test(allText), 'must NOT route to the music handler');
  } finally { restore(); }
});

// ---------------------------------------------------------------------- //
// 6. Advance / completion lifecycle — no stale double-fire.               //
// ---------------------------------------------------------------------- //

test('advancing the wizard re-anchors the marker — the next bare input uses the NEW step, not a stale one', async () => {
  const env = await makeEnv();
  const wizard = newWizard();
  wizard.source = { kind: 'url', value: 'https://example.com/video.mp4' };
  // The wizard is at the FOCUS step (advanced past source). Its marker is
  // the focus-step token. A bare focus answer must advance it further.
  wizard.step = 'focus';
  await putAwaitingInput(env, CHAT, 'wzs', makePayload('wzs', encodeWizardToken(wizard)));
  const sent = [];
  const restore = installFetch({ sent });
  try {
    await drive(env, {
      update_id: 106,
      message: {
        message_id: 107,
        from: { id: 1, is_bot: false },
        chat: { id: CHAT, type: 'private' },
        text: 'a calm nature montage', // the focus answer, sent bare
      },
    });
    const allText = textOf(sent);
    assert.match(allText, /length|duration|music|confirm/i, 'focus answer should advance the wizard past focus');
    assert.ok(!/was not expected/i.test(allText), 'must not fall through');
  } finally { restore(); }
});

test('a COMPLETED flow clears its marker — the next unrelated bare message is not hijacked', async () => {
  const env = await makeEnv();
  // The watermark flow (wm) completes with showSettings — it sends NO new
  // prompt, so the consume-on-success delete must clear its row.
  await putAwaitingInput(env, CHAT, 'wm', makePayload('wm'));
  assert.ok(await getAwaitingInput(env, CHAT), 'marker is live before the answer');
  const sent = [];
  const restore = installFetch({ sent });
  try {
    await drive(env, {
      update_id: 107,
      message: {
        message_id: 108,
        from: { id: 1, is_bot: false },
        chat: { id: CHAT, type: 'private' },
        text: '@myhandle', // the watermark answer, sent bare
      },
    });
    assert.match(textOf(sent), /settings|watermark/i, 'the bare watermark answer should be applied (flow completed)');
  } finally { restore(); }
  // Completion consumed the marker: it must be GONE, not left to double-fire.
  assert.equal(await getAwaitingInput(env, CHAT), null, 'a completed flow must clear its awaiting_input row');

  // And the proof it cannot double-fire: an unrelated bare document now
  // falls through to the ordinary "not expected" acknowledgement.
  const sent2 = [];
  const restore2 = installFetch({ sent: sent2 });
  try {
    await drive(env, {
      update_id: 108,
      message: {
        message_id: 109,
        from: { id: 1, is_bot: false },
        chat: { id: CHAT, type: 'private' },
        document: { file_name: 'unrelated.torrent', file_id: 'f9', file_unique_id: 'u9', file_size: 2048 },
      },
    });
    assert.match(textOf(sent2), /was not expected/i, 'with the marker cleared, an unrelated send falls through normally');
  } finally { restore2(); }
});

// ---------------------------------------------------------------------- //
// 7. Choke-point WRITE path: sending a marker prompt marks the chat.      //
// ---------------------------------------------------------------------- //

test('the sendForceReply choke point marks awaiting_input when a prompt is sent (drive /new end to end)', async () => {
  const env = await makeEnv();
  assert.equal(await getAwaitingInput(env, CHAT), null, 'no marker before any prompt');
  const sent = [];
  const restore = installFetch({ sent });
  try {
    await drive(env, {
      update_id: 109,
      message: {
        message_id: 110,
        from: { id: 1, is_bot: false },
        chat: { id: CHAT, type: 'private' },
        text: '/new',
        entities: [{ type: 'bot_command', offset: 0, length: 4 }],
      },
    });
    assert.match(textOf(sent), /send or forward/i, 'the wizard source-step prompt should be sent');
  } finally { restore(); }
  const row = await getAwaitingInput(env, CHAT);
  assert.ok(row, 'the source-step prompt must mark the chat via the choke point');
  assert.equal(row.op, 'wzs', 'the marker carries the wizard opcode');
  // And the stored payload is the SAME cf: token the prompt text carries —
  // it decodes back into a wizard record, so the read path can route it.
  assert.match(String(row.payload), /^cf:wzs:/, 'payload reuses the flow.js cf: encoding verbatim');
});

test('a button press prunes a stale marker (routeCallback pre-dispatch prune)', async () => {
  const env = await makeEnv();
  await putAwaitingInput(env, CHAT, 'wm', makePayload('wm'));
  const sent = [];
  const restore = installFetch({ sent });
  try {
    await drive(env, {
      update_id: 110,
      callback_query: {
        id: 'cb1',
        from: { id: 1, is_bot: false },
        message: { message_id: 60, chat: { id: CHAT, type: 'private' }, from: BOT_USER, text: 'menu' },
        data: 'menu:home',
      },
    });
  } finally { restore(); }
  assert.equal(await getAwaitingInput(env, CHAT), null, 'any button press must prune the pending-input marker');
});
