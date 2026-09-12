import test from 'node:test';
import assert from 'node:assert/strict';
import { decryptCredentials, encryptCredentials } from '../src/crypto.js';
import { validateProductionPlan } from '../src/production.js';
import { stageLabel } from '../src/constants.js';
import { ensureTaskLabel, getJobIdForLabel, getTasks, getState, putState, removeTask } from '../src/storage.js';
import worker, { __test } from '../src/index.js';
import nacl from 'tweetnacl';
import sealedbox from 'tweetnacl-sealedbox-js';
import { downloadTelegramFileBytes, sendAudioBytes, sendDocumentBytes } from '../src/telegram.js';
import { cloneRepositoryName, createPrivateShadowClone, deleteClipforgeJob, parseJsonDocument, putBinaryFile, sourcePathAllowed, updateZernioSecret } from '../src/github.js';

class MemoryKv {
  constructor() { this.values = new Map(); this.putCalls = 0; }
  async get(key) { return this.values.get(key) ?? null; }
  async put(key, value) { this.putCalls += 1; this.values.set(key, value); }
  async delete(key) { this.values.delete(key); }
}

const keyBytes = new Uint8Array(32);
for (let i = 0; i < keyBytes.length; i += 1) keyBytes[i] = i + 1;
const encryptionSecret = btoa(String.fromCharCode(...keyBytes));

function env() { return { CLIPFORGE_BOT_KV: new MemoryKv(), KV_ENCRYPTION_KEY: encryptionSecret }; }

test('sealed-box dependency produces a GitHub-secret compatible ciphertext envelope', () => {
  const pair = nacl.box.keyPair();
  const message = new TextEncoder().encode('gemini-key-material');
  const sealed = sealedbox.seal(message, pair.publicKey);
  assert.equal(sealed.length, message.length + sealedbox.overheadLength);
  assert.deepEqual(sealedbox.open(sealed, pair.publicKey, pair.secretKey), message);
});

test('credentials are encrypted, bound to a chat, and round-trip without plaintext KV storage', async () => {
  const value = { githubPat: 'github_pat_example_123456789', repo: 'owner/repo', geminiKeys: ['AIza-example-key-123456'] };
  const encrypted = await encryptCredentials(value, 101, encryptionSecret);
  assert.equal(encrypted.includes(value.githubPat), false);
  assert.deepEqual(await decryptCredentials(encrypted, 101, encryptionSecret), value);
  await assert.rejects(() => decryptCredentials(encrypted, 102, encryptionSecret));
});

test('Zernio key saving seals a GitHub Actions secret with the required 48-byte envelope overhead', async () => {
  const pair = nacl.box.keyPair();
  const publicKey = btoa(String.fromCharCode(...pair.publicKey));
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null });
    if (calls.length === 1) return new Response(JSON.stringify({ key: publicKey, key_id: 'test-key-id' }), { headers: { 'content-type': 'application/json' } });
    return new Response(null, { status: 204 });
  };
  try {
    const zernioKey = 'sk_example_zernio_key_for_regression';
    await updateZernioSecret({ githubPat: 'test-token' }, 'owner/repo', zernioKey);
    assert.equal(calls.length, 2);
    assert.match(calls[0].url, /\/actions\/secrets\/public-key$/);
    assert.match(calls[1].url, /\/actions\/secrets\/ZERNIO_API_KEY$/);
    assert.equal(calls[1].method, 'PUT');
    assert.equal(calls[1].body.key_id, 'test-key-id');
    assert.equal(atob(calls[1].body.encrypted_value).length, new TextEncoder().encode(zernioKey).length + 48);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('task labels are stable and isolated per Telegram chat', async () => {
  const workerEnv = env();
  assert.equal(await ensureTaskLabel(workerEnv, 1, 'manual-1'), 'A');
  assert.equal(await ensureTaskLabel(workerEnv, 1, 'automatic-2'), 'B');
  assert.equal(await ensureTaskLabel(workerEnv, 1, 'manual-1'), 'A');
  assert.equal(await ensureTaskLabel(workerEnv, 2, 'other-user-job'), 'A');
  assert.equal(await getJobIdForLabel(workerEnv, 1, 'A'), 'manual-1');
  assert.equal(await getJobIdForLabel(workerEnv, 2, 'B'), null);
  assert.equal((await getTasks(workerEnv, 1)).labels.B, 'automatic-2');
});

test('task removal clears only the requested chat label and its job options', async () => {
  const workerEnv = env();
  await ensureTaskLabel(workerEnv, 1, 'manual-delete');
  await ensureTaskLabel(workerEnv, 2, 'other-chat-job');
  assert.equal(await removeTask(workerEnv, 1, 'A', 'manual-delete'), true);
  assert.equal(await getJobIdForLabel(workerEnv, 1, 'A'), null);
  assert.equal(await getJobIdForLabel(workerEnv, 2, 'A'), 'other-chat-job');
  assert.equal(await removeTask(workerEnv, 1, 'A', 'manual-delete'), false);
});

test('confirmed task cleanup deletes only the selected job release, tag, and job files', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    const parsed = new URL(String(url));
    const path = `${parsed.pathname}${parsed.search}`;
    const method = init.method || 'GET';
    calls.push({ path, method, body: init.body ? JSON.parse(init.body) : null });
    const reply = (value, status = 200) => new Response(value === null ? null : JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
    if (path === '/repos/owner/repo/git/ref/heads/main') return reply({ object: { sha: 'branch-sha' } });
    if (path === '/repos/owner/repo/git/commits/branch-sha') return reply({ tree: { sha: 'tree-sha' } });
    if (path === '/repos/owner/repo/git/trees/tree-sha?recursive=1') return reply({ truncated: false, tree: [
      { type: 'blob', path: 'jobs/manual-delete/status.json' },
      { type: 'blob', path: 'jobs/manual-delete/production.json' },
      { type: 'blob', path: 'jobs/other-job/status.json' },
      { type: 'blob', path: 'README.md' }
    ] });
    if (path === '/repos/owner/repo/releases/tags/clipforge-manual-delete') return reply({ id: 42 });
    if (path === '/repos/owner/repo/releases/42' && method === 'DELETE') return reply(null, 204);
    if (path === '/repos/owner/repo/git/refs/tags/clipforge-manual-delete' && method === 'DELETE') return reply(null, 204);
    if (path === '/repos/owner/repo/releases/tags/clipforge-relay-input-manual-delete') return reply({ id: 43 });
    if (path === '/repos/owner/repo/releases/43' && method === 'DELETE') return reply(null, 204);
    if (path === '/repos/owner/repo/git/refs/tags/clipforge-relay-input-manual-delete' && method === 'DELETE') return reply(null, 204);
    if (path === '/repos/owner/repo/contents/jobs/manual-delete/status.json?ref=main') return reply({ sha: 'status-sha' });
    if (path === '/repos/owner/repo/contents/jobs/manual-delete/production.json?ref=main') return reply({ sha: 'production-sha' });
    if (path === '/repos/owner/repo/contents/jobs/manual-delete/status.json' && method === 'DELETE') return reply({});
    if (path === '/repos/owner/repo/contents/jobs/manual-delete/production.json' && method === 'DELETE') return reply({});
    throw new Error(`Unexpected GitHub request: ${method} ${path}`);
  };
  try {
    const result = await deleteClipforgeJob({ githubPat: 'test-token' }, 'owner/repo', 'manual-delete');
    assert.equal(result.deletedFiles, 2);
    assert.equal(calls.some((call) => call.path.includes('jobs/other-job')), false);
    assert.equal(calls.some((call) => call.path.includes('README.md')), false);
    assert.equal(calls.some((call) => call.path === '/repos/owner/repo/releases/42' && call.method === 'DELETE'), true);
    assert.equal(calls.some((call) => call.path === '/repos/owner/repo/git/refs/tags/clipforge-manual-delete' && call.method === 'DELETE'), true);
    assert.equal(calls.some((call) => call.path === '/repos/owner/repo/releases/43' && call.method === 'DELETE'), true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('task cleanup tolerates a missing release and GitHub’s 422 missing-tag response', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    const parsed = new URL(String(url));
    const path = `${parsed.pathname}${parsed.search}`;
    const method = init.method || 'GET';
    const reply = (value, status = 200) => new Response(value === null ? null : JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
    if (path === '/repos/owner/repo/git/ref/heads/main') return reply({ object: { sha: 'branch-sha' } });
    if (path === '/repos/owner/repo/git/commits/branch-sha') return reply({ tree: { sha: 'tree-sha' } });
    if (path === '/repos/owner/repo/git/trees/tree-sha?recursive=1') return reply({ truncated: false, tree: [{ type: 'blob', path: 'jobs/manual-no-release/status.json' }] });
    if (path === '/repos/owner/repo/releases/tags/clipforge-manual-no-release') return reply({ message: 'Not Found' }, 404);
    if (path === '/repos/owner/repo/git/refs/tags/clipforge-manual-no-release' && method === 'DELETE') return reply({ message: 'Reference does not exist' }, 422);
    if (path === '/repos/owner/repo/releases/tags/clipforge-relay-input-manual-no-release') return reply({ message: 'Not Found' }, 404);
    if (path === '/repos/owner/repo/git/refs/tags/clipforge-relay-input-manual-no-release' && method === 'DELETE') return reply({ message: 'Reference does not exist' }, 422);
    if (path === '/repos/owner/repo/contents/jobs/manual-no-release/status.json?ref=main') return reply({ sha: 'status-sha' });
    if (path === '/repos/owner/repo/contents/jobs/manual-no-release/status.json' && method === 'DELETE') return reply({});
    throw new Error(`Unexpected GitHub request: ${method} ${path}`);
  };
  try {
    const result = await deleteClipforgeJob({ githubPat: 'test-token' }, 'owner/repo', 'manual-no-release');
    assert.equal(result.deletedFiles, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('conversation state never adopts another chat state', async () => {
  const workerEnv = env();
  await putState(workerEnv, 1, { flow: 'manual_source', pending: { mode: 'manual' }, currentTask: 'manual-1', activeViewId: 77 });
  assert.equal((await getState(workerEnv, 1)).currentTask, 'manual-1');
  assert.equal((await getState(workerEnv, 1)).activeViewId, 77);
  assert.equal((await getState(workerEnv, 2)).currentTask, null);
  assert.equal((await getState(workerEnv, 2)).activeViewId, null);
});

test('state persistence does not rewrite an unchanged normalized record', async () => {
  const workerEnv = env();
  const state = { flow: 'manual_source', pending: { mode: 'manual' }, currentTask: null, activeViewId: 12 };
  assert.equal(await putState(workerEnv, 10, state), true);
  assert.equal(await putState(workerEnv, 10, { ...state, pending: { mode: 'manual' } }), false);
  assert.equal(workerEnv.CLIPFORGE_BOT_KV.putCalls, 1);
});

test('interactive views edit the chat-scoped active message before creating a new one', async () => {
  const workerEnv = { ...env(), TELEGRAM_BOT_TOKEN: 'test-token' };
  await putState(workerEnv, 19, { activeViewId: 84 });
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), body: JSON.parse(init.body) });
    return new Response(JSON.stringify({ ok: true, result: { message_id: 84 } }), { headers: { 'content-type': 'application/json' } });
  };
  try {
    const result = await __test.renderInteractiveView(workerEnv, 19, '<b>Updated</b>', { replyMarkup: __test.mainMenu() });
    assert.equal(result.edited, true);
    assert.equal(calls.length, 1);
    assert.match(calls[0].url, /\/editMessageText$/);
    assert.equal(calls[0].body.message_id, 84);
    assert.equal(calls[0].body.chat_id, 19);
    assert.equal(calls[0].body.text, '<b>Updated</b>');
    assert.equal(calls[0].body.reply_markup.inline_keyboard.length > 0, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('interactive views safely create a new view only when no editable message exists', async () => {
  const workerEnv = { ...env(), TELEGRAM_BOT_TOKEN: 'test-token' };
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), body: JSON.parse(init.body) });
    return new Response(JSON.stringify({ ok: true, result: { message_id: 101 } }), { headers: { 'content-type': 'application/json' } });
  };
  try {
    const result = await __test.renderInteractiveView(workerEnv, 20, 'First control view');
    assert.equal(result.message_id, 101);
    assert.equal(calls.length, 1);
    assert.match(calls[0].url, /\/sendMessage$/);
    assert.equal((await getState(workerEnv, 20)).activeViewId, 101);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('typed slash commands create a new control message rather than editing the prior view', async () => {
  const workerEnv = { ...env(), TELEGRAM_BOT_TOKEN: 'test-token', TELEGRAM_WEBHOOK_SECRET: 'expected-secret' };
  await putState(workerEnv, 29, { activeViewId: 444 });
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), body: JSON.parse(init.body) });
    return new Response(JSON.stringify({ ok: true, result: { message_id: 445 } }), { headers: { 'content-type': 'application/json' } });
  };
  try {
    const response = await worker.fetch(new Request('https://worker.example/webhook', {
      method: 'POST',
      headers: { 'X-Telegram-Bot-Api-Secret-Token': 'expected-secret', 'content-type': 'application/json' },
      body: JSON.stringify({ update_id: 4001, message: { message_id: 10, text: '/start', chat: { id: 29, type: 'private' } } })
    }), workerEnv);
    assert.equal(response.status, 200);
    assert.equal(calls.length, 1);
    assert.match(calls[0].url, /\/sendMessage$/);
    assert.equal(calls[0].body.chat_id, 29);
    assert.equal((await getState(workerEnv, 29)).activeViewId, 445);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('health is public but webhook updates require the configured Telegram secret', async () => {
  const workerEnv = { ...env(), TELEGRAM_WEBHOOK_SECRET: 'expected-secret', TELEGRAM_BOT_TOKEN: 'test-token' };
  const health = await worker.fetch(new Request('https://worker.example/health'), workerEnv);
  assert.equal(health.status, 200);
  const rejected = await worker.fetch(new Request('https://worker.example/webhook', { method: 'POST', body: '{}' }), workerEnv);
  assert.equal(rejected.status, 401);
});

test('voice previews and full agent prompts are delivered as safe multipart Telegram files', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), body: init.body });
    return new Response(JSON.stringify({ ok: true, result: { message_id: calls.length } }), { headers: { 'content-type': 'application/json' } });
  };
  try {
    const workerEnv = { TELEGRAM_BOT_TOKEN: 'test-token' };
    await sendAudioBytes(workerEnv, 9, new Uint8Array([1, 2, 3]), 'en-US-AvaNeural.mp3', 'Preview');
    await sendDocumentBytes(workerEnv, 9, new TextEncoder().encode('full agent prompt'), 'manual-1-agent-prompt.txt', 'Prompt');
    assert.equal(calls[0].url.endsWith('/sendAudio'), true);
    assert.equal(calls[0].body.get('audio').name, 'en-US-AvaNeural.mp3');
    assert.equal(calls[1].url.endsWith('/sendDocument'), true);
    assert.equal(calls[1].body.get('document').name, 'manual-1-agent-prompt.txt');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('shared-bot onboarding offers private Shadow Clone creation or existing-clone connection', () => {
  const callbacks = __test.cloneOnboardingMenu().inline_keyboard.flat().map((button) => button.callback_data);
  assert.ok(callbacks.includes('clone:new'));
  assert.ok(callbacks.includes('clone:connect'));
  assert.equal(cloneRepositoryName('my-clipforge_2026'), 'my-clipforge_2026');
  assert.throws(() => cloneRepositoryName('../unsafe'), /Shadow Clone repository name/);
  assert.equal(sourcePathAllowed('scripts/generate_voiceover.py'), true);
  assert.equal(sourcePathAllowed('jobs/other-user/status.json'), false);
  assert.equal(sourcePathAllowed('branding/gemini_keys.json'), false);
  assert.equal(sourcePathAllowed('audio-library/private-track.mp3'), false);
});

test('private Shadow Clone creation copies shared source while excluding all user state', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    const parsed = new URL(String(url));
    const path = `${parsed.pathname}${parsed.search}`;
    calls.push({ path, method: init.method || 'GET', headers: init.headers, body: init.body ? JSON.parse(init.body) : null });
    const reply = (value) => new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } });
    if (path === '/user') return reply({ login: 'alice' });
    if (path === '/repos/motionssalt/clipforge/git/ref/heads/main') return reply({ object: { sha: 'source-commit' } });
    if (path === '/repos/motionssalt/clipforge/git/commits/source-commit') return reply({ tree: { sha: 'source-tree' } });
    if (path === '/repos/motionssalt/clipforge/git/trees/source-tree?recursive=1') return reply({ truncated: false, tree: [
      { type: 'blob', path: 'README.md', sha: 'readme-blob', mode: '100644' },
      { type: 'blob', path: 'jobs/another-user/status.json', sha: 'job-blob', mode: '100644' },
      { type: 'blob', path: 'branding/tts_settings.json', sha: 'brand-blob', mode: '100644' }
    ] });
    if (path === '/user/repos' && init.method === 'POST') return reply({ full_name: 'alice/my-clipforge' });
    if (path === '/repos/motionssalt/clipforge/git/blobs/readme-blob') return reply({ encoding: 'base64', content: 'cmVhZG1l' });
    if (path === '/repos/alice/my-clipforge/git/blobs' && init.method === 'POST') return reply({ sha: calls.filter((call) => call.path === path && call.method === 'POST').length === 1 ? 'copied-readme' : 'sync-blob' });
    if (path === '/repos/alice/my-clipforge/git/trees' && init.method === 'POST') return reply({ sha: 'target-tree' });
    if (path === '/repos/alice/my-clipforge/git/commits' && init.method === 'POST') return reply({ sha: 'target-commit' });
    if (path === '/repos/alice/my-clipforge/git/refs' && init.method === 'POST') return reply({ ref: 'refs/heads/main' });
    throw new Error(`Unexpected GitHub request: ${init.method || 'GET'} ${path}`);
  };
  try {
    const result = await createPrivateShadowClone('token-value', 'my-clipforge');
    assert.deepEqual(result.repo, 'alice/my-clipforge');
    assert.equal(result.copiedFiles, 1);
    const treeRequest = calls.find((call) => call.path === '/repos/alice/my-clipforge/git/trees');
    assert.deepEqual(treeRequest.body.tree.map((entry) => entry.path).sort(), ['.clipforge-sync.json', 'README.md']);
    assert.equal(calls.some((call) => call.path.includes('job-blob')), false);
    assert.equal(calls.some((call) => call.path.includes('brand-blob')), false);
    assert.equal(calls.every((call) => call.headers['User-Agent'] === 'ClipForge-Telegram-Bot/1.0'), true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('source and command helpers enforce Telegram-only social intake while preserving other source paths', () => {
  assert.equal(__test.commandOf('/automatic'), '/automatic');
  assert.equal(__test.commandOf('/automatic@ClipForgeBot'), '/automatic');
  assert.equal(__test.commandOf('/unknown'), null);
  assert.equal(__test.validateSource('https://example.com/video.mp4'), 'https://example.com/video.mp4');
  assert.equal(__test.validateSource('https://t.me/public_channel/123'), 'https://t.me/public_channel/123');
  assert.equal(__test.validateSource('magnet:?xt=urn:btih:abcdef'), 'magnet:?xt=urn:btih:abcdef');
  assert.equal(__test.isPublicTelegramPost('https://t.me/public_channel/123'), true);
  assert.equal(__test.isPublicTelegramPost('https://t.me/c/123/456'), false);
  assert.equal(__test.disabledSocialSourceHost('https://www.youtube.com/watch?v=x'), 'youtube.com');
  assert.throws(() => __test.validateSource('https://www.youtube.com/watch?v=x'), /disabled/);
  assert.throws(() => __test.validateSource('https://t.me/c/123/456'), /public Telegram channel post/);
  assert.throws(() => __test.validateSource('file:///etc/passwd'));
  assert.equal(__test.normalizeFocus('  the  opening scene  '), 'the opening scene');
  assert.equal(__test.normalizeFocus('-'), '');
});

test('Telegram source preflight blocks public groups but permits public channels before Stage A dispatch', async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response('<a>View In Group</a>', { status: 200 });
    await assert.rejects(__test.preflightTelegramChannelPost('https://t.me/public_group/123'), /public Telegram group link/);
    globalThis.fetch = async () => new Response('<a>View In Channel</a>', { status: 200 });
    await __test.preflightTelegramChannelPost('https://t.me/public_channel/123');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Music Library exposes safe library controls while task deletion is limited to terminal task states', () => {
  const settings = __test.settingsMenu().inline_keyboard.flat().map((button) => button.callback_data);
  assert.ok(settings.includes('set:music_library'));
  assert.equal(settings.includes('set:music_default'), false);
  assert.equal(__test.isSafeAudioLibraryPath('audio-library/LoFi Night.m4a'), true);
  assert.equal(__test.isSafeAudioLibraryPath('jobs/manual-1/music.mp3'), false);
  assert.equal(__test.isSafeAudioLibraryPath('audio-library/../escape.mp3'), false);
  assert.equal(__test.taskCanBeDeleted({ stage: 'complete' }), true);
  assert.equal(__test.taskCanBeDeleted({ stage: 'awaiting_json_upload' }), true);
  assert.equal(__test.taskCanBeDeleted({ stage: 'stage_a_running' }), false);
  const terminalCallbacks = __test.taskButtons('A', { stage: 'complete' }).inline_keyboard.flat().map((button) => button.callback_data);
  assert.ok(terminalCallbacks.includes('task:delete:A'));
  const runningCallbacks = __test.taskButtons('A', { stage: 'stage_a_running' }).inline_keyboard.flat().map((button) => button.callback_data);
  assert.equal(runningCallbacks.includes('task:delete:A'), false);
});

test('Series Mode proceeds from source directly to duration and retains normal focus setup for ordinary tasks', () => {
  assert.equal(__test.taskSetupFlowAfterSource({ mode: 'manual', seriesMode: true }), 'manual_duration');
  assert.equal(__test.taskSetupFlowAfterSource({ mode: 'automatic', seriesMode: true }), 'automatic_duration');
  assert.equal(__test.taskSetupFlowAfterSource({ mode: 'manual', seriesMode: false }), 'manual_focus');
  assert.equal(__test.taskSetupFlowAfterSource({ mode: 'automatic' }), 'automatic_focus');
  assert.equal(__test.taskSetupFlowAfterSource({ mode: 'unsupported', seriesMode: true }), null);
});

test('a Series Mode source message creates a duration prompt rather than asking for editorial focus', async () => {
  const workerEnv = { ...env(), TELEGRAM_BOT_TOKEN: 'test-token', TELEGRAM_WEBHOOK_SECRET: 'expected-secret' };
  await putState(workerEnv, 61, { flow: 'manual_source', pending: { mode: 'manual', seriesMode: true }, activeViewId: 901 });
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), body: JSON.parse(init.body) });
    return new Response(JSON.stringify({ ok: true, result: { message_id: 902 } }), { headers: { 'content-type': 'application/json' } });
  };
  try {
    const response = await worker.fetch(new Request('https://worker.example/webhook', {
      method: 'POST',
      headers: { 'X-Telegram-Bot-Api-Secret-Token': 'expected-secret', 'content-type': 'application/json' },
      body: JSON.stringify({ update_id: 4061, message: { message_id: 61, text: 'https://example.com/source.mp4', chat: { id: 61, type: 'private' } } })
    }), workerEnv);
    assert.equal(response.status, 200);
    assert.equal(calls.length, 1);
    assert.match(calls[0].url, /\/sendMessage$/);
    assert.match(calls[0].body.text, /Series Mode does not use editorial focus/);
    assert.equal(calls[0].body.text.includes('Send an optional editorial focus'), false);
    assert.equal((await getState(workerEnv, 61)).flow, 'manual_duration');
    assert.equal((await getState(workerEnv, 61)).pending.focus, undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('music and torrent inline-button labels are bounded by UTF-8 bytes', () => {
  const label = __test.telegramButtonText('𝐒𝐀𝐃 𝐅𝐔𝐍𝐊 (𝐒𝐔𝐏𝐄𝐑 𝐒𝐋𝐎𝐖𝐄𝐃) 𝐗 𝐘𝐔𝐓𝐀 𝐎𝐊𝐊𝐎𝐓𝐒𝐔.m4a');
  assert.ok(new TextEncoder().encode(label).length <= 60);
  assert.match(label, /…$/);
  assert.equal(__test.telegramButtonText('CRY_FOR_ME_FUNK.m4a'), 'CRY_FOR_ME_FUNK.m4a');
  const video = __test.torrentCandidateButtonText('[Anime Time] Tokyo Ghoul/[Anime Time] Tokyo Ghoul Episode 12 END 1080p.mkv');
  assert.ok(new TextEncoder().encode(video).length <= 48);
  assert.match(video, /1080p\.mkv$/);
  assert.equal(__test.formatBytes(2_097_152), '2.0 MiB');
});

test('status JSON with workflow merge markers is recovered for display without changing non-status JSON rules', () => {
  const conflicted = '{\n  "job_id": "automatic-example",\n  "stage": "stage_a_running",\n<<<<<<< Updated upstream\n  "updated_at_epoch": 10,\n=======\n  "updated_at_epoch": 20,\n>>>>>>> Stashed changes\n}\n';
  const parsed = parseJsonDocument(conflicted, 'jobs/automatic-example/status.json');
  assert.equal(parsed.job_id, 'automatic-example');
  assert.equal(parsed.stage, 'stage_a_running');
  assert.equal(parsed.updated_at_epoch, 20);
  assert.throws(() => parseJsonDocument(conflicted, 'jobs/automatic-example/production.json'), /not valid JSON/);
});

test('a staged torrent task remains resumable after returning to the home menu and is not presented as dispatched', () => {
  const staged = { flow: 'manual_focus', pending: { mode: 'manual', source: 'path:jobs/manual-example/source.torrent', jobId: 'manual-example' } };
  assert.equal(__test.hasResumablePendingTask(staged), true);
  assert.equal(__test.hasResumablePendingTask({ flow: 'manual_focus', pending: { mode: 'manual', source: 'path:jobs/manual-example/source.torrent' } }), false);
  const callbacks = __test.homeMenu(staged).inline_keyboard.flat().map((button) => button.callback_data);
  assert.ok(callbacks.includes('resume:task'));
  assert.equal(stageLabel('starting'), 'Starting');
});

test('existing masked Gemini metadata is recognised without materialising an opaque site secret', () => {
  assert.equal(__test.existingGeminiLabel([], [{ fingerprint: 'masked-one' }, { fingerprint: 'masked-two' }]), 'Gemini keys: 2 existing site keys already configured in GitHub Actions');
  assert.match(__test.existingGeminiLabel(['AIza-local-secret'], [{ fingerprint: 'masked-one' }]), /stored in this bot chat/);
  assert.equal(__test.existingGeminiLabel([], []), 'Gemini keys: not configured');
});

test('the copied agent handoff contains the exact release URL', () => {
  const releaseUrl = 'https://github.com/motionssalt/clipforge/releases/tag/clipforge-manual-test';
  const prompt = __test.buildAgentHandoffPrompt(releaseUrl);
  assert.ok(prompt.includes(releaseUrl));
  assert.ok(prompt.includes('00_READ_THIS_FIRST.txt'));
  assert.ok(new TextEncoder().encode(prompt).length <= 256);
});

test('Zernio settings preserve the site schema and only target active selected accounts', () => {
  const defaults = __test.defaultZernioSettings();
  assert.equal(defaults.automatic_mode, 'smart_schedule');
  assert.equal(defaults.smart_schedule.timezone, 'UTC');
  assert.equal(__test.validZernioTimezone('America/New_York'), true);
  assert.equal(__test.validZernioTimezone('bad timezone'), false);
  assert.equal(__test.validZernioTime('19:30'), true);
  assert.equal(__test.validZernioTime('25:99'), false);
  assert.equal(__test.validZernioDateTime('2026-08-25T19:30'), true);
  assert.equal(__test.validZernioDateTime('2026/08/25 19:30'), false);
  const accounts = [
    { id: 'tik-active', platform: 'tiktok', displayName: 'TikTok active', isActive: true, enabled: true },
    { id: 'yt-active', platform: 'youtube', username: 'youtube-active', isActive: true, enabled: true },
    { id: 'tik-reconnect', platform: 'tiktok', needsReconnection: true }
  ];
  const settings = __test.zernioSettingsOrDefault({ enabled: true, auto_publish: true, automatic_mode: 'publish_now', target_accounts: { tiktok: ['tik-active', 'tik-reconnect'], youtube: ['yt-active'] }, smart_schedule: { timezone: 'Europe/London', interval_days: 3, preferred_time: '08:15', queue_depth: 7 } });
  assert.equal(settings.smart_schedule.interval_hours, 72);
  const hourly = __test.zernioSettingsOrDefault({ smart_schedule: { interval_hours: 1 } });
  assert.equal(hourly.smart_schedule.interval_hours, 1);
  assert.deepEqual(__test.activeZernioAccounts(accounts).tiktok.map((account) => account.id), ['tik-active']);
  assert.deepEqual(__test.zernioTargets(settings, accounts), [{ platform: 'tiktok', account_ids: ['tik-active'] }, { platform: 'youtube', account_ids: ['yt-active'] }]);
  const callbacks = __test.zernioSettingsMenu({ settings, secretConfigured: true }).inline_keyboard.flat().map((button) => button.callback_data);
  for (const callback of ['set:zernio_key', 'zernio:refresh', 'zernio:targets', 'zernio:schedule', 'zernio:clear_prompt']) assert.ok(callbacks.includes(callback));
});

test('manual Stage A status exposes the agent-prompt control before production upload', () => {
  const markup = __test.taskButtons('A', { stage: 'awaiting_json_upload' });
  const callbacks = markup.inline_keyboard.flat().map((button) => button.callback_data);
  assert.ok(callbacks.includes('agent:A'));
  assert.ok(callbacks.includes('plan:A'));
  assert.equal(__test.taskButtons('A', { stage: 'stage_a_running' }).inline_keyboard.flat().some((button) => button.callback_data === 'agent:A'), false);
  assert.ok(__test.taskButtons('B', { stage: 'awaiting_torrent_selection' }).inline_keyboard.flat().some((button) => button.callback_data === 'torrent:B'));
});

test('torrent manifest downloads are bounded and retained as raw bytes', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(new Uint8Array([0x64, 0x33, 0x3a, 0x66, 0x6f, 0x6f]), { headers: { 'content-length': '6' } });
  try {
    const bytes = await downloadTelegramFileBytes({ TELEGRAM_BOT_TOKEN: 'test-token' }, 'documents/example.torrent', 6);
    assert.deepEqual([...bytes], [0x64, 0x33, 0x3a, 0x66, 0x6f, 0x6f]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('torrent manifest uploads use only the selected job source.torrent path and base64 bytes', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    const parsed = new URL(String(url));
    calls.push({ path: `${parsed.pathname}${parsed.search}`, method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null });
    if ((init.method || 'GET') === 'GET') return new Response(JSON.stringify({ message: 'Not Found' }), { status: 404, headers: { 'content-type': 'application/json' } });
    return new Response(JSON.stringify({ content: { path: 'jobs/manual-safe/source.torrent' } }), { headers: { 'content-type': 'application/json' } });
  };
  try {
    await putBinaryFile({ githubPat: 'test-token' }, 'owner/repo', 'jobs/manual-safe/source.torrent', new Uint8Array([0x64, 0x33, 0x3a]), 'upload manifest');
    const put = calls.find((call) => call.method === 'PUT');
    assert.equal(put.path, '/repos/owner/repo/contents/jobs/manual-safe/source.torrent');
    assert.equal(calls[0].path, '/repos/owner/repo/contents/jobs/manual-safe/source.torrent?ref=main');
    assert.equal(put.body.content, 'ZDM6');
    assert.equal(put.body.branch, 'main');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('manual production-plan validation preserves ClipForge’s timing and narration contract', () => {
  const valid = {
    video_duration_seconds: 90,
    target_total_duration_seconds: 30,
    cuts: [{ start_seconds: 0, end_seconds: 30, voiceover_text: 'A supported narration.' }]
  };
  assert.deepEqual(validateProductionPlan(valid), []);
  const invalid = {
    video_duration_seconds: 10,
    target_total_duration_seconds: 30,
    cuts: [{ start_seconds: 8, end_seconds: 7, raw_narration: '' }]
  };
  assert.ok(validateProductionPlan(invalid).length >= 2);
});

import { getRelayJob, putCredentials } from '../src/storage.js';
import { encryptRelayPayload } from '../src/crypto.js';
import { MAX_RELAY_VIDEO_BYTES, parseRelayCaption, parseRelayReadyMarker, relayCaption, relayReadyMarker, relayVideoMetadata } from '../src/relay.js';
import relayWorker, { __test as relayWorkerTest } from '../src/relay-worker.js';

test('direct video source is copied into the internal group without calling getFile or downloading media bytes', async () => {
  const workerEnv = {
    ...env(), TELEGRAM_BOT_TOKEN: 'test-token', TELEGRAM_WEBHOOK_SECRET: 'expected-secret',
    INTERNAL_RELAY_GROUP_CHAT_ID: '-1001234567890', BOT_B_TELEGRAM_USERNAME: 'Clipforgedl_bot',
    RELAY_ENCRYPTION_KEY: encryptionSecret
  };
  await putCredentials(workerEnv, 77, { githubPat: 'test-clone-pat', repo: 'example/clone', geminiKeys: [] });
  await putState(workerEnv, 77, { flow: 'manual_source', pending: { mode: 'manual', seriesMode: false }, activeViewId: null });
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    const body = JSON.parse(init.body);
    calls.push({ url: String(url), body });
    const result = /\/copyMessage$/.test(String(url)) ? { message_id: 700 } : { message_id: 701 };
    return new Response(JSON.stringify({ ok: true, result }), { headers: { 'content-type': 'application/json' } });
  };
  try {
    const response = await worker.fetch(new Request('https://worker.example/webhook', {
      method: 'POST', headers: { 'X-Telegram-Bot-Api-Secret-Token': 'expected-secret', 'content-type': 'application/json' },
      body: JSON.stringify({ update_id: 7011, message: {
        message_id: 701, chat: { id: 77, type: 'private' },
        video: { file_id: 'video-id', file_unique_id: 'unique-video', file_size: 123456, mime_type: 'video/mp4' }
      } })
    }), workerEnv);
    assert.equal(response.status, 200);
    assert.equal(calls.length, 2);
    assert.match(calls[0].url, /\/copyMessage$/);
    assert.equal(calls[0].body.chat_id, -1001234567890);
    assert.equal(calls[0].body.from_chat_id, 77);
    assert.equal(calls[0].body.message_id, 701);
    assert.match(calls[0].body.caption, /^CFRELAY1:manual-/);
    assert.match(calls[1].url, /\/sendMessage$/);
    assert.match(calls[1].body.text, /Video securely staged/);
    assert.equal(calls.some((call) => /\/getFile$|\/file\/bot/.test(call.url)), false);
    const state = await getState(workerEnv, 77);
    assert.equal(state.pending.source, 'telegram_bot_forward');
    assert.equal(state.pending.relay.internal_group_message_id, 700);
    assert.equal(state.flow, 'manual_focus');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('direct relay metadata rejects non-video, missing, and oversized files before any handoff', () => {
  assert.equal(relayVideoMetadata({ message_id: 1, document: { file_name: 'notes.txt', file_size: 10 } }), null);
  assert.throws(() => relayVideoMetadata({ message_id: 1, video: { file_id: 'a', file_unique_id: 'b', file_size: MAX_RELAY_VIDEO_BYTES + 1 } }), /safety limit/);
  assert.throws(() => relayVideoMetadata({ message_id: 1, video: { file_id: '', file_unique_id: 'b', file_size: 100 } }), /reusable media identifier/);
});

test('relay captions and ready markers bind one job, source chat, and copied group message', () => {
  const caption = relayCaption('manual-123', -10022);
  assert.deepEqual(parseRelayCaption(caption), { jobId: 'manual-123', sourceChatId: -10022 });
  const marker = relayReadyMarker('manual-123', -10022, 91);
  assert.deepEqual(parseRelayReadyMarker(marker), { jobId: 'manual-123', sourceChatId: -10022, groupMessageId: 91 });
  assert.deepEqual(relayWorkerTest.parseRelayReadySignal(`/relay@Clipforgedl_bot ${marker}`), { jobId: 'manual-123', sourceChatId: -10022, groupMessageId: 91 });
  assert.equal(parseRelayCaption('CFRELAY1:bad id:-12'), null);
  assert.equal(parseRelayReadyMarker('CFRELAY_READY1:manual-123:-10022:0'), null);
});

test('deduplication keys are allocated only for processable Bot A and Bot B updates', async () => {
  const botAEnv = { ...env(), TELEGRAM_BOT_TOKEN: 'test-token', TELEGRAM_WEBHOOK_SECRET: 'expected-secret' };
  const botAResponse = await worker.fetch(new Request('https://worker.example/webhook', {
    method: 'POST', headers: { 'X-Telegram-Bot-Api-Secret-Token': 'expected-secret', 'content-type': 'application/json' },
    body: JSON.stringify({ update_id: 8001, message: { message_id: 1, text: 'group noise', chat: { id: -1001, type: 'group' } } })
  }), botAEnv);
  assert.equal(botAResponse.status, 200);
  assert.equal(botAEnv.CLIPFORGE_BOT_KV.values.size, 0);

  const botBEnv = {
    ...env(), BOT_B_TELEGRAM_WEBHOOK_SECRET: 'bot-b-secret', INTERNAL_RELAY_GROUP_CHAT_ID: '-10022', BOT_A_TELEGRAM_ID: '1234'
  };
  const botBResponse = await relayWorker.fetch(new Request('https://worker.example/webhook', {
    method: 'POST', headers: { 'X-Telegram-Bot-Api-Secret-Token': 'bot-b-secret', 'content-type': 'application/json' },
    body: JSON.stringify({ update_id: 8002, message: { message_id: 2, text: 'unrelated group noise', chat: { id: -10022, type: 'group' }, from: { id: 9999, is_bot: true } } })
  }), botBEnv);
  assert.equal(botBResponse.status, 200);
  assert.equal(botBEnv.CLIPFORGE_BOT_KV.values.size, 0);

  const copiedMedia = { chat: { id: -10022 }, from: { id: 1234, is_bot: true }, caption: relayCaption('manual-123', 17), video: { file_id: 'media-id' } };
  assert.equal(relayWorkerTest.isRelevantRelayUpdate(botBEnv, copiedMedia), true);
  assert.equal(relayWorkerTest.isRelevantRelayUpdate(botBEnv, { ...copiedMedia, from: { id: 5678, is_bot: true } }), false);
});

test('legacy authenticated public Telegram intake is authorized only for the configured original repository', () => {
  assert.equal(__test.permitsLegacyTelegramMtproto({ ORIGINAL_CLIPFORGE_REPOSITORY: 'motionssalt/clipforge' }, { repo: 'motionssalt/clipforge' }), true);
  assert.equal(__test.permitsLegacyTelegramMtproto({ ORIGINAL_CLIPFORGE_REPOSITORY: 'motionssalt/clipforge' }, { repo: 'other-user/clipforge-shadow' }), false);
});

test('Bot B routes only a matching opaque Bot A payload and needs no per-user credential decryption', async () => {
  const payload = { version: 1, job_id: 'manual-51', target_repo: 'owner/clone', target_github_pat: 'clone-pat', stage_a_inputs: {}, telegram: { group_message_id: 43 } };
  const sealed = await encryptRelayPayload(payload, 'manual-51', encryptionSecret);
  const record = {
    state: 'ready', repo: 'owner/clone', sealed_payload: sealed,
    relay: { internal_group_chat_id: -10022, internal_group_message_id: 43 }
  };
  assert.equal(relayWorkerTest.sealedRelayPayload(record, { jobId: 'manual-51', sourceChatId: 17, groupMessageId: 43 }), sealed);
  assert.equal(sealed.includes('clone-pat'), false);
  assert.throws(() => relayWorkerTest.sealedRelayPayload(record, { jobId: 'manual-51', sourceChatId: 17, groupMessageId: 44 }), /does not match/);
  assert.throws(() => relayWorkerTest.sealedRelayPayload({ ...record, sealed_payload: 'short' }, { jobId: 'manual-51', sourceChatId: 17, groupMessageId: 43 }), /sealed payload/);
});
