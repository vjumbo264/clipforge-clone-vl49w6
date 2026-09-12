import test from 'node:test';
import assert from 'node:assert/strict';

import {
  MAX_RELAY_VIDEO_BYTES, RELAY_SOURCE_TYPE, parseRelayCaption, parseRelayReadyMarker,
  parseRelayReadySignal, relayCaption, relayReadyMarker, relayVideoMetadata
} from '../src/relay.js';
import { getRelayJob, putRelayJob } from '../src/storage.js';
import { __test as relayWorker } from '../src/relay-worker.js';

test('relay caption round-trips job id and chat id', () => {
  const caption = relayCaption('manual-1787692652625', 123456789);
  assert.equal(caption, 'CFRELAY1:manual-1787692652625:123456789');
  assert.deepEqual(parseRelayCaption(caption), { jobId: 'manual-1787692652625', sourceChatId: 123456789 });
});

test('relay caption rejects unsafe identifiers', () => {
  assert.throws(() => relayCaption('bad job!', 1));
  assert.throws(() => relayCaption('manual-1', 'not-a-chat'));
  assert.equal(parseRelayCaption('CFRELAY1:'), null);
  assert.equal(parseRelayCaption('something else entirely'), null);
});

test('ready marker round-trips and parses from the /relay@bot command form', () => {
  const marker = relayReadyMarker('manual-9', 555, 777);
  assert.equal(marker, 'CFRELAY_READY1:manual-9:555:777');
  assert.deepEqual(parseRelayReadyMarker(marker), { jobId: 'manual-9', sourceChatId: 555, groupMessageId: 777 });
  const viaCommand = parseRelayReadySignal(`/relay@Clipforgedl_bot ${marker}`);
  assert.deepEqual(viaCommand, { jobId: 'manual-9', sourceChatId: 555, groupMessageId: 777 });
  assert.equal(parseRelayReadySignal('/relay@Clipforgedl_bot nonsense'), null);
  assert.throws(() => relayReadyMarker('manual-9', 555, 0));
});

test('relay video metadata accepts videos and video documents, rejects the rest', () => {
  const video = relayVideoMetadata({ message_id: 5, video: { file_id: 'f', file_unique_id: 'u', file_size: 1024, mime_type: 'video/mp4' } });
  assert.equal(video.media_kind, 'video');
  assert.equal(video.file_size, 1024);
  const doc = relayVideoMetadata({ message_id: 6, document: { file_id: 'f', file_unique_id: 'u', file_size: 2048, mime_type: 'video/quicktime', file_name: 'clip.MOV' } });
  assert.equal(doc.media_kind, 'document_video');
  assert.equal(doc.file_name, 'clip.MOV');
  assert.equal(relayVideoMetadata({ message_id: 7, document: { file_id: 'f', file_unique_id: 'u', file_size: 10, mime_type: 'application/pdf', file_name: 'x.pdf' } }), null);
  assert.equal(relayVideoMetadata({ message_id: 8, text: 'hello' }), null);
});

test('relay video metadata enforces the 1800 MiB safety cap', () => {
  assert.equal(MAX_RELAY_VIDEO_BYTES, 1800 * 1024 * 1024);
  assert.throws(() => relayVideoMetadata({ message_id: 1, video: { file_id: 'f', file_unique_id: 'u', file_size: MAX_RELAY_VIDEO_BYTES + 1 } }), /safety limit/);
  assert.throws(() => relayVideoMetadata({ message_id: 1, video: { file_id: 'f', file_unique_id: 'u', file_size: 0 } }));
});

test('Bot B relevance gate: right group + Bot A + relay shape, before any KV access', () => {
  const env = { INTERNAL_RELAY_GROUP_CHAT_ID: '-5405387856', BOT_A_TELEGRAM_ID: '8670100252' };
  const base = { chat: { id: -5405387856 }, from: { id: 8670100252, is_bot: true } };
  assert.equal(relayWorker.isRelevantRelayUpdate(env, { ...base, caption: 'CFRELAY1:manual-1:123', video: { file_id: 'x' } }), true);
  assert.equal(relayWorker.isRelevantRelayUpdate(env, { ...base, text: '/relay@Clipforgedl_bot CFRELAY_READY1:manual-1:123:9' }), true);
  // wrong group
  assert.equal(relayWorker.isRelevantRelayUpdate(env, { ...base, chat: { id: -1 }, caption: 'CFRELAY1:manual-1:123', video: { file_id: 'x' } }), false);
  // not from Bot A
  assert.equal(relayWorker.isRelevantRelayUpdate(env, { ...base, from: { id: 42, is_bot: true }, caption: 'CFRELAY1:manual-1:123', video: { file_id: 'x' } }), false);
  // not a bot at all
  assert.equal(relayWorker.isRelevantRelayUpdate(env, { ...base, from: { id: 8670100252, is_bot: false }, caption: 'CFRELAY1:manual-1:123', video: { file_id: 'x' } }), false);
  // relay-shaped text from Bot A but neither media caption nor ready signal
  assert.equal(relayWorker.isRelevantRelayUpdate(env, { ...base, text: 'hello group' }), false);
  // missing config fails closed
  assert.equal(relayWorker.isRelevantRelayUpdate({}, { ...base, caption: 'CFRELAY1:manual-1:123', video: { file_id: 'x' } }), false);
});

test('Bot B sealed-payload gate requires ready state and matching media message', () => {
  const sealed = 'x'.repeat(100);
  const record = { state: 'ready', relay: { internal_group_message_id: 777 }, sealed_payload: sealed };
  assert.equal(relayWorker.sealedRelayPayload(record, { groupMessageId: 777 }), sealed);
  assert.throws(() => relayWorker.sealedRelayPayload({ ...record, state: 'staged' }, { groupMessageId: 777 }));
  assert.throws(() => relayWorker.sealedRelayPayload(record, { groupMessageId: 778 }));
  assert.throws(() => relayWorker.sealedRelayPayload({ ...record, sealed_payload: 'short' }, { groupMessageId: 777 }));
});

test('relay source type constant matches the sealed-payload semantics', () => {
  assert.equal(RELAY_SOURCE_TYPE, 'telegram_bot_forward');
});

// Map-backed CLIPFORGE_BOT_KV stub plus the Bot B routing config Bot B's
// gates require. Only the storage surface relay code touches is implemented.
function mockRelayEnv() {
  const kv = new Map();
  return {
    INTERNAL_RELAY_GROUP_CHAT_ID: '-5405387856',
    BOT_A_TELEGRAM_ID: '8670100252',
    CLIPFORGE_BOT_KV: {
      async get(name) { return kv.has(name) ? kv.get(name) : null; },
      async put(name, value) { kv.set(name, String(value)); },
      async delete(name) { kv.delete(name); }
    }
  };
}

function copiedMediaMessage(caption, fileId) {
  return {
    chat: { id: -5405387856 },
    from: { id: 8670100252, is_bot: true },
    caption,
    video: { file_id: fileId }
  };
}

test('race closed: staging-time record lets Bot B attach copied media before any confirm-time fields exist', async () => {
  const env = mockRelayEnv();
  const chatId = 123456789;
  const jobId = 'manual-1787692652625';
  // Exactly what handleWizardRelayVideo now writes at staging time: media
  // coordinates and the group chat id, but no internal_group_message_id yet
  // and none of the confirm-time fields (state 'ready', sealed_payload).
  await putRelayJob(env, chatId, jobId, {
    state: 'staged',
    repo: 'owner/clone',
    mode: 'manual',
    relay: {
      source_type: 'telegram_bot_forward', media_kind: 'video', file_id: 'f', file_unique_id: 'u',
      file_size: 1024, mime_type: 'video/mp4', file_name: 'telegram-video.mp4', source_message_id: 5,
      internal_group_chat_id: -5405387856
    }
  });
  // Bot B observes the group copy in the window between staging and confirm.
  await relayWorker.rememberCopiedMedia(env, copiedMediaMessage(relayCaption(jobId, chatId), 'botb-file-id'));
  const record = await getRelayJob(env, chatId, jobId);
  assert.equal(record.relay.bot_b_file_id, 'botb-file-id');
  assert.equal(record.state, 'staged'); // confirm-time upgrade has not run yet
  assert.equal(record.sealed_payload, undefined);
});

test('pre-fix behavior documented: with no staging record the copied-media update is silently dropped', async () => {
  const env = mockRelayEnv();
  await relayWorker.rememberCopiedMedia(env, copiedMediaMessage(relayCaption('manual-1', 555), 'botb-file-id'));
  assert.equal(await getRelayJob(env, 555, 'manual-1'), null);
});

test('rememberCopiedMedia still refuses malformed or out-of-scope updates', async () => {
  const env = mockRelayEnv();
  await putRelayJob(env, 555, 'manual-1', { state: 'staged', relay: { internal_group_chat_id: -5405387856 } });
  // wrong group
  await relayWorker.rememberCopiedMedia(env, { ...copiedMediaMessage(relayCaption('manual-1', 555), 'x'), chat: { id: -1 } });
  // not from Bot A
  await relayWorker.rememberCopiedMedia(env, { ...copiedMediaMessage(relayCaption('manual-1', 555), 'x'), from: { id: 42, is_bot: true } });
  // no relay caption
  await relayWorker.rememberCopiedMedia(env, copiedMediaMessage('hello group', 'x'));
  const record = await getRelayJob(env, 555, 'manual-1');
  assert.equal(record.relay.bot_b_file_id, undefined);
});

// ------------------------------------------------------------------------ //
// bug-52: cross-repo (multi-tenant) relay through the central Bot A/Bot B   //
// ------------------------------------------------------------------------ //
//
// bug-52 asked whether Shadow Clone owners can use the central Bot A/Bot B
// relay instead of standing up their own. Audit result (see FIX_STATE.json):
// they already DO — ARCHITECTURE.md §10 has ONE shared Bot A deployment
// serving every user (each chat bound to its own clone repo), the relay group
// id is a public var on that deployment, and RELAY_ENCRYPTION_KEY lives as a
// secret on the same shared Worker. The sealed envelope already carries
// target_repo + the user's own PAT, so telegram-relay.yml writes into the
// OWNING user's clone. None of the 7 isOriginalRepo gates touch the relay
// path (they gate §9.1 channel downloads and main-owner news/update controls).
//
// What was missing is TEST COVERAGE for the multi-tenant guarantee itself:
// with several clone owners' jobs staged in the SAME shared KV namespace and
// the SAME relay group, Bot B must only ever act on the payload matching the
// job it was told to expect — never one user's video delivered to another
// user's clone. These tests pin exactly that.

test('multi-tenant: two clone owners staged in the same group stay isolated', async () => {
  const env = mockRelayEnv();
  const chatA = 111111111; // owner of clone-a
  const chatB = 222222222; // owner of clone-b
  await putRelayJob(env, chatA, 'job-a', {
    state: 'ready', repo: 'usera/clipforge-clone-a', mode: 'manual',
    relay: { internal_group_chat_id: -5405387856, internal_group_message_id: 1001 },
    sealed_payload: 'sealed-for-clone-a'.padEnd(80, 'x')
  });
  await putRelayJob(env, chatB, 'job-b', {
    state: 'ready', repo: 'userb/clipforge-clone-b', mode: 'manual',
    relay: { internal_group_chat_id: -5405387856, internal_group_message_id: 2002 },
    sealed_payload: 'sealed-for-clone-b'.padEnd(80, 'y')
  });
  // A copied-media update carrying owner B's caption attaches ONLY to B's job.
  await relayWorker.rememberCopiedMedia(env, copiedMediaMessage(relayCaption('job-b', chatB), 'file-of-b'));
  const recordA = await getRelayJob(env, chatA, 'job-a');
  const recordB = await getRelayJob(env, chatB, 'job-b');
  assert.equal(recordA.relay.bot_b_file_id, undefined, 'owner A job untouched by owner B media');
  assert.equal(recordB.relay.bot_b_file_id, 'file-of-b');
  // And the sealed-payload gate returns each job's OWN payload only for its
  // OWN message id — a marker quoting job-b never releases job-a's payload.
  assert.equal(relayWorker.sealedRelayPayload(recordA, { groupMessageId: 1001 }), recordA.sealed_payload);
  assert.throws(() => relayWorker.sealedRelayPayload(recordA, { groupMessageId: 2002 }));
  assert.throws(() => relayWorker.sealedRelayPayload(recordB, { groupMessageId: 1001 }));
});

test('multi-tenant: same job id under two different chats cannot cross over', async () => {
  const env = mockRelayEnv();
  // Two users running identically-named jobs (job ids are per-chat scoped in
  // KV — relay:<chatId>:<jobId> — so identical job ids across tenants must
  // never alias).
  await putRelayJob(env, 111111111, 'manual-1', {
    state: 'ready', relay: { internal_group_message_id: 500 }, sealed_payload: 'a'.repeat(80)
  });
  await putRelayJob(env, 222222222, 'manual-1', {
    state: 'ready', relay: { internal_group_message_id: 600 }, sealed_payload: 'b'.repeat(80)
  });
  await relayWorker.rememberCopiedMedia(env, copiedMediaMessage(relayCaption('manual-1', 222222222), 'media-b'));
  assert.equal((await getRelayJob(env, 111111111, 'manual-1')).relay.bot_b_file_id, undefined);
  assert.equal((await getRelayJob(env, 222222222, 'manual-1')).relay.bot_b_file_id, 'media-b');
});

test('multi-tenant: Bot B relevance gate is identical for clone and main-account traffic', () => {
  const env = { INTERNAL_RELAY_GROUP_CHAT_ID: '-5405387856', BOT_A_TELEGRAM_ID: '8670100252' };
  const base = { chat: { id: -5405387856 }, from: { id: 8670100252, is_bot: true } };
  // The gate keys on (group, Bot A id, caption/marker shape) only — there is
  // deliberately NO repo/tenant check here, because tenancy is enforced by
  // the (chatId, jobId) KV key and the message-id-matched sealed payload.
  for (const chatId of [111111111, 222222222, 333333333]) {
    assert.equal(relayWorker.isRelevantRelayUpdate(env, { ...base, caption: relayCaption('job-x', chatId), video: { file_id: 'f' } }), true);
    assert.equal(relayWorker.isRelevantRelayUpdate(env, { ...base, text: relayReadyMarker('job-x', chatId, 42) }), true);
  }
});
