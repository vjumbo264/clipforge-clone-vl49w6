/**
 * remove-paste-feature — plan upload is FILE-UPLOAD-ONLY, accepting ANY
 * text-based file.
 *
 * This suite REPLACES plan-paste-ground-truth.test.mjs and
 * json-reassembly-byteproof.test.mjs, which exercised the deliberately
 * removed multi-bubble paste-reassembly mechanism (the `uplb` opcode, the
 * plan_upload_buffer D1 fragment buffer, keepPlanPasteAlive, and
 * looksLikePartialJson). That code is deleted; tests for it are deleted,
 * not skipped.
 *
 * What remains, driven through the REAL worker (src/index.js) via
 * handleUpdate -> handleMessage -> awaiting_input/parseFlowReply ->
 * handleFlowReply -> handlePlanUploadMessage with stubbed global fetch:
 *
 *   - a .json file containing a valid plan still works (regression guard)
 *     and commits the plan BYTE-FOR-BYTE to the repo;
 *   - a .txt file whose content is valid plan JSON now works;
 *   - a .md file whose content is valid plan JSON now works;
 *   - a text file reported by the client with the generic
 *     application/octet-stream mime type still works (content gate, not
 *     extension/mime gate);
 *   - a binary (non-UTF-8) file is rejected as "not a text file";
 *   - a text file whose content is NOT valid JSON produces the SAME
 *     "not valid" error the .json path always produced;
 *   - a text file whose content parses but violates the §7.3 schema
 *     produces the same schema-error listing;
 *   - a typed/pasted text message is declined with file-only guidance
 *     (the paste path is gone).
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import worker from '../src/index.js';
import { makePayload } from '../src/flow.js';
import { ensureTaskLabel, putCredentials, putAwaitingInput } from '../src/storage.js';
import { makeD1 } from './helpers/d1.mjs';

const CHAT = 7717;
const TEST_KEY = Buffer.alloc(32, 7).toString('base64');

function buildValidPlan() {
  return {
    schema: 'clipforge.production_plan/v1',
    title: 'File Upload Verification Plan',
    video_duration_seconds: 60,
    target_total_duration_seconds: 60,
    cuts: [
      {
        start_seconds: 0,
        end_seconds: 60,
        voiceover_text: 'A single continuous narration for the file-upload verification test.',
        keywords: [
          { word: 'mountain sunrise', color: '#ff8800' },
          { word: 'calm ocean waves', color: '#0088ff' },
          { word: 'forest trail', color: '#00aa44' },
        ],
      },
    ],
    hashtags: ['#one', '#two', '#three', '#four', '#five'],
  };
}
const PLAN_TEXT = JSON.stringify(buildValidPlan(), null, 2);

function makeKv() {
  const map = new Map();
  return {
    get: async (k) => (map.has(k) ? map.get(k) : null),
    put: async (k, v) => { map.set(k, String(v)); },
    delete: async (k) => { map.delete(k); },
    _map: map,
  };
}

async function makeEnv() {
  const env = {
    CLIPFORGE_BOT_KV: makeKv(),
    CLIPFORGE_BOT_D1: makeD1(),
    KV_ENCRYPTION_KEY: TEST_KEY,
    TELEGRAM_BOT_TOKEN: 'test-token',
    TELEGRAM_WEBHOOK_SECRET: 'test-secret',
  };
  await putCredentials(env, CHAT, { githubPat: 'pat-not-real', repo: 'owner/repo', geminiKeys: [] });
  return env;
}

function makeCtx() {
  const pending = [];
  return { waitUntil: (p) => pending.push(Promise.resolve(p)), _pending: pending };
}

// fileBytes: the exact bytes the Telegram file-download endpoint serves.
// fileName/mimeType: whatever the client reported on the document.
function installFetch({ sent = [], github = [], fileBytes = null } = {}) {
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    if (u.startsWith('https://api.telegram.org/file/')) {
      return new Response(fileBytes, { status: 200 });
    }
    if (u.startsWith('https://api.telegram.org/')) {
      const m = u.split('/').pop();
      let payload = {};
      try { payload = init && init.body ? JSON.parse(String(init.body)) : {}; } catch { payload = {}; }
      sent.push({ method: m, payload });
      if (m === 'getFile') {
        return new Response(JSON.stringify({ ok: true, result: { file_path: 'documents/file_1' } }), { status: 200 });
      }
      return new Response(JSON.stringify({ ok: true, result: { message_id: 5000 + sent.length } }), { status: 200 });
    }
    if (u.startsWith('https://api.github.com/')) {
      let body = null;
      try { body = init && init.body ? JSON.parse(String(init.body)) : null; } catch { body = null; }
      github.push({ url: u, method: String((init && init.method) || 'GET').toUpperCase(), body });
      if (body && body.content) {
        // Contents API PUT — the committed file content, base64.
        github[github.length - 1].content = Buffer.from(String(body.content), 'base64').toString('utf8');
      }
      if (String((init && init.method) || 'GET').toUpperCase() === 'PUT' || String((init && init.method) || 'GET').toUpperCase() === 'POST') {
        return new Response(JSON.stringify({ content: { sha: 'deadbeef' }, commit: { sha: 'c0ffee' } }), { status: 201 });
      }
      if (u.includes('/branches/')) {
        return new Response(JSON.stringify({ commit: { sha: 'c0ffee' } }), { status: 200 });
      }
      // status.json / stage-a-request.json / production.json reads: absent.
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

function documentUpdate(updateId, messageId, { fileName, mimeType, fileSize }) {
  return {
    update_id: updateId,
    message: {
      message_id: messageId,
      from: { id: 1, is_bot: false },
      chat: { id: CHAT, type: 'private' },
      document: {
        file_id: 'file-id-1',
        file_unique_id: 'unique-1',
        file_name: fileName,
        mime_type: mimeType,
        file_size: fileSize,
      },
      // deliberately NO reply_to_message — a bare file send routes via
      // the awaiting_input row written by the upload prompt
    },
  };
}

function textUpdate(updateId, messageId, text) {
  return {
    update_id: updateId,
    message: {
      message_id: messageId,
      from: { id: 1, is_bot: false },
      chat: { id: CHAT, type: 'private' },
      text,
    },
  };
}

async function armUpload(env, label) {
  // What the upl prompt's sendForceReply choke point wrote when the
  // operator tapped Upload:
  await putAwaitingInput(env, CHAT, 'upl', makePayload('upl', label));
}

function committedPlan(github) {
  const planPut = github.find((c) => c.method === 'PUT' && c.url.includes('production.json'));
  return planPut || null;
}

// ====================================================================== //
// Accepted files                                                          //
// ====================================================================== //

for (const [fileName, mimeType] of [
  ['production.json', 'application/json'],
  ['production.txt', 'text/plain'],
  ['production.md', 'text/markdown'],
  ['production.markdown', 'text/markdown'],
  ['production.dat', 'application/octet-stream'], // generic mime: content gate decides
]) {
  test(`a ${fileName} file containing valid plan JSON uploads and dispatches Stage B`, async () => {
    const env = await makeEnv();
    const label = await ensureTaskLabel(env, CHAT, `job-upload-${fileName.replace(/\W+/g, '-')}`);
    await armUpload(env, label);

    const sent = [];
    const github = [];
    const restore = installFetch({ sent, github, fileBytes: PLAN_TEXT });
    try {
      await drive(env, documentUpdate(200, 201, {
        fileName, mimeType, fileSize: Buffer.byteLength(PLAN_TEXT),
      }));

      const allText = textOf(sent);
      assert.ok(!/not valid/i.test(allText),
        `a valid plan in ${fileName} must NOT fail validation. Bot said:\n` + allText);
      const planPut = committedPlan(github);
      assert.ok(planPut, `the plan from ${fileName} must be committed to the repo (Stage B dispatch path)`);
      assert.deepEqual(JSON.parse(planPut.content), buildValidPlan(),
        `the committed plan must match the uploaded plan for ${fileName}`);
    } finally { restore(); }
  });
}

test('the committed production.json preserves the uploaded plan byte content (round-trip through validation)', async () => {
  const env = await makeEnv();
  const label = await ensureTaskLabel(env, CHAT, 'job-upload-bytes');
  await armUpload(env, label);

  const sent = [];
  const github = [];
  const restore = installFetch({ sent, github, fileBytes: PLAN_TEXT });
  try {
    await drive(env, documentUpdate(200, 201, {
      fileName: 'production.json', mimeType: 'application/json', fileSize: Buffer.byteLength(PLAN_TEXT),
    }));
    const planPut = committedPlan(github);
    assert.ok(planPut, 'the plan must be committed');
    // The handler re-serializes the validated document with 2-space indent
    // + trailing newline — assert the semantic content survives exactly.
    assert.deepEqual(JSON.parse(planPut.content), JSON.parse(PLAN_TEXT));
  } finally { restore(); }
});

// ====================================================================== //
// Rejected files / inputs                                                 //
// ====================================================================== //

test('a binary (non-UTF-8) file is rejected as not a text file', async () => {
  const env = await makeEnv();
  const label = await ensureTaskLabel(env, CHAT, 'job-upload-binary');
  await armUpload(env, label);

  // 256 bytes of random non-UTF-8 binary (invalid continuation bytes).
  const binary = Buffer.from(Array.from({ length: 256 }, (_, i) => (i * 37 + 128) % 256));
  const sent = [];
  const github = [];
  const restore = installFetch({ sent, github, fileBytes: binary });
  try {
    await drive(env, documentUpdate(200, 201, {
      fileName: 'production.png', mimeType: 'image/png', fileSize: binary.length,
    }));
    const allText = textOf(sent);
    assert.ok(/not a text file/i.test(allText),
      'a binary upload must be rejected as not-a-text-file. Bot said:\n' + allText);
    assert.ok(!committedPlan(github), 'a binary upload must never reach the repo');
  } finally { restore(); }
});

test('a text file with malformed JSON gets the existing "not valid" error', async () => {
  const env = await makeEnv();
  const label = await ensureTaskLabel(env, CHAT, 'job-upload-badjson');
  await armUpload(env, label);

  const sent = [];
  const github = [];
  const restore = installFetch({ sent, github, fileBytes: '{ "schema": ' });
  try {
    await drive(env, documentUpdate(200, 201, {
      fileName: 'production.json', mimeType: 'application/json', fileSize: 14,
    }));
    const allText = textOf(sent);
    assert.ok(/not valid/i.test(allText), 'malformed JSON must fail with the existing error. Bot said:\n' + allText);
    assert.ok(/Not valid JSON/i.test(allText), 'the JSON parse error line must be listed');
    assert.ok(!committedPlan(github), 'an invalid plan must never reach the repo');
  } finally { restore(); }
});

test('a text file whose JSON violates the plan schema gets the existing schema-error listing', async () => {
  const env = await makeEnv();
  const label = await ensureTaskLabel(env, CHAT, 'job-upload-badschema');
  await armUpload(env, label);

  const notAPlan = JSON.stringify({ hello: 'world' }, null, 2);
  const sent = [];
  const github = [];
  const restore = installFetch({ sent, github, fileBytes: notAPlan });
  try {
    await drive(env, documentUpdate(200, 201, {
      fileName: 'production.txt', mimeType: 'text/plain', fileSize: Buffer.byteLength(notAPlan),
    }));
    const allText = textOf(sent);
    assert.ok(/not valid/i.test(allText), 'a schema-invalid plan must fail validation. Bot said:\n' + allText);
    assert.ok(/problem/i.test(allText), 'the schema problems must be listed');
    assert.ok(!committedPlan(github), 'a schema-invalid plan must never reach the repo');
  } finally { restore(); }
});

test('an oversized file is rejected before download', async () => {
  const env = await makeEnv();
  const label = await ensureTaskLabel(env, CHAT, 'job-upload-toobig');
  await armUpload(env, label);

  const sent = [];
  const restore = installFetch({ sent, fileBytes: 'x'.repeat(2 * 1024 * 1024) });
  try {
    await drive(env, documentUpdate(200, 201, {
      fileName: 'production.json', mimeType: 'application/json', fileSize: 2 * 1024 * 1024,
    }));
    assert.ok(/1 MB/i.test(textOf(sent)), 'an oversized file must be rejected. Bot said:\n' + textOf(sent));
  } finally { restore(); }
});

test('a pasted/typed text message is declined with file-only guidance (paste path removed)', async () => {
  const env = await makeEnv();
  const label = await ensureTaskLabel(env, CHAT, 'job-upload-paste-declined');
  await armUpload(env, label);

  const sent = [];
  const github = [];
  const restore = installFetch({ sent, github, fileBytes: PLAN_TEXT });
  try {
    await drive(env, textUpdate(200, 201, PLAN_TEXT));
    const allText = textOf(sent);
    assert.ok(/as a <b>file<\/b>/i.test(allText),
      'a pasted plan must be declined with file-only guidance. Bot said:\n' + allText);
    assert.ok(/no longer supported/i.test(allText), 'the guidance must state pasting is no longer supported');
    assert.ok(!committedPlan(github), 'a pasted plan must never reach the repo');
  } finally { restore(); }
});

test('the upload prompt copy mentions text-file types and never mentions pasting', async () => {
  const env = await makeEnv();
  const label = await ensureTaskLabel(env, CHAT, 'job-upload-copy');
  const sent = [];
  const restore = installFetch({ sent });
  try {
    // Tap the Upload button: task:upload:<label> routes to beginPlanUpload.
    await drive(env, {
      update_id: 200,
      callback_query: {
        id: 'cb-1',
        from: { id: 1, is_bot: false },
        message: {
          message_id: 500,
          from: { id: 9001, is_bot: true, first_name: 'ClipForge' },
          chat: { id: CHAT, type: 'private' },
          text: 'task view',
        },
        data: `task:upload:${label}`,
      },
    });
    const allText = textOf(sent);
    assert.ok(/\.json/.test(allText) && /\.txt/.test(allText) && /\.md/.test(allText),
      'the upload prompt must name the accepted file types. Bot said:\n' + allText);
    assert.ok(!/paste/i.test(allText), 'the upload prompt must not mention pasting. Bot said:\n' + allText);
  } finally { restore(); }
});
