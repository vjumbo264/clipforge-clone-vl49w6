// Unit tests for the announcement markers (bot/src/storage.js), covering the
// kv-minimization phase-3 move from per-kind KV keys to the announcements D1
// table: one row per (chat_id, kind), upsert on write, null-on-miss getters —
// the exact "announced exactly once per marker" semantics callers rely on:
//   update_notice   (bug-31: clone-update push announced once)
//   deploy_failure  (bug-68: marker is the failed run id — reruns re-notify)
//   news_notice     (bug-46: docs/news.json published_at announced once)
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  getAnnouncedUpdate, setAnnouncedUpdate,
  getAnnouncedDeployFailure, setAnnouncedDeployFailure,
  getAnnouncedNews, setAnnouncedNews,
} from '../src/storage.js';
import { makeD1 } from './helpers/d1.mjs';

const CHAT = 4242;

function makeEnv() {
  return { CLIPFORGE_BOT_D1: makeD1() };
}

test('announcement getters return null before anything is announced', async () => {
  const env = makeEnv();
  assert.equal(await getAnnouncedUpdate(env, CHAT), null);
  assert.equal(await getAnnouncedDeployFailure(env, CHAT), null);
  assert.equal(await getAnnouncedNews(env, CHAT), null);
});

test('each kind round-trips its marker per chat', async () => {
  const env = makeEnv();
  await setAnnouncedUpdate(env, CHAT, '2026-08-30T00:00:00Z');
  await setAnnouncedDeployFailure(env, CHAT, '1122334455');
  await setAnnouncedNews(env, CHAT, '2026-08-29T12:00:00Z');
  assert.equal(await getAnnouncedUpdate(env, CHAT), '2026-08-30T00:00:00Z');
  assert.equal(await getAnnouncedDeployFailure(env, CHAT), '1122334455');
  assert.equal(await getAnnouncedNews(env, CHAT), '2026-08-29T12:00:00Z');
});

test('set is an upsert: a newer marker replaces the old one for the same kind', async () => {
  const env = makeEnv();
  await setAnnouncedUpdate(env, CHAT, 'old-marker');
  await setAnnouncedUpdate(env, CHAT, 'new-marker');
  assert.equal(await getAnnouncedUpdate(env, CHAT), 'new-marker');
  assert.equal(
    env.CLIPFORGE_BOT_D1._query('SELECT marker FROM announcements WHERE chat_id = ? AND kind = ?', CHAT, 'update_notice').length,
    1,
    'still exactly one row for (chat, kind)',
  );
});

test('kinds and chats are independent namespaces', async () => {
  const env = makeEnv();
  await setAnnouncedUpdate(env, CHAT, 'u-1');
  await setAnnouncedNews(env, 7777, 'n-7');
  assert.equal(await getAnnouncedUpdate(env, 7777), null, 'other chat has no update marker');
  assert.equal(await getAnnouncedNews(env, CHAT), null, 'other kind has no marker');
  assert.equal(await getAnnouncedUpdate(env, CHAT), 'u-1');
  assert.equal(await getAnnouncedNews(env, 7777), 'n-7');
});

test('empty marker stores as empty string (matches legacy String(marker || "") behavior)', async () => {
  const env = makeEnv();
  await setAnnouncedDeployFailure(env, CHAT, null);
  assert.equal(await getAnnouncedDeployFailure(env, CHAT), '');
});
