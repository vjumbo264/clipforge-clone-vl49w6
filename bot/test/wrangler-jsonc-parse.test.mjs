// bug-62 regression test:
// scripts/reclaim-stale-task-labels.mjs's kvNamespaceId() used to strip JSONC
// comments and then call strict JSON.parse — which does NOT tolerate the
// trailing commas that JSONC legitimately allows. bot/wrangler.bot-a.jsonc
// had a { ... }, // comment... } shape that, after the comment lines were
// blanked to whitespace, became `..., }`, and JSON.parse threw:
//   Expected double-quoted property name in JSON at position 534
//   (line 31 column 1)
// — the literal hourly cleanup.yml failure. The parser has been extracted to
// scripts/jsonc.mjs and hardened to also strip trailing commas before } or ].
//
// This test locks in that BOTH real wrangler config files as they exist on
// disk parse successfully via that helper (so a future edit reintroducing an
// invalid trailing comma is caught in CI before it reaches a live cleanup.yml
// run), and that the helper accepts the exact failure shape that caused the
// original outage.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { parseWranglerJsonc, stripJsoncCommentsAndTrailingCommas } from '../../scripts/jsonc.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(HERE, '..', '..');

function readRepoFile(rel) {
  return readFileSync(join(REPO_ROOT, rel), 'utf8');
}

test('bug-62: bot/wrangler.bot-a.jsonc parses via the reclaim script parser', () => {
  const raw = readRepoFile('bot/wrangler.bot-a.jsonc');
  const obj = parseWranglerJsonc(raw);
  assert.equal(obj.name, 'clipforge-bot-a');
  const kv = (obj.kv_namespaces || []).find((b) => b.binding === 'CLIPFORGE_BOT_KV');
  assert.ok(kv, 'CLIPFORGE_BOT_KV binding must be present');
  assert.match(String(kv.id), /^[0-9a-f]{32}$/, 'KV namespace id must be a 32-char hex id');
  // The specific pattern the outage came from — a scheduled trigger — must
  // still round-trip cleanly.
  assert.deepEqual(obj.triggers, { crons: ['* * * * *'] });
});

test('bug-62: bot/wrangler.bot-b.jsonc parses via the reclaim script parser', () => {
  const raw = readRepoFile('bot/wrangler.bot-b.jsonc');
  const obj = parseWranglerJsonc(raw);
  assert.equal(obj.name, 'clipforge-telegram-relay-bot');
  const kv = (obj.kv_namespaces || []).find((b) => b.binding === 'CLIPFORGE_BOT_KV');
  assert.ok(kv, 'CLIPFORGE_BOT_KV binding must be present');
  assert.match(String(kv.id), /^[0-9a-f]{32}$/, 'KV namespace id must be a 32-char hex id');
});

test('bug-62: parser accepts the exact failure shape from the outage', () => {
  // A comma after "triggers": { ... }, followed only by // comment lines and
  // the closing } — the literal shape bot/wrangler.bot-a.jsonc had when
  // cleanup.yml started failing with position 534 line 31 col 1.
  const failing = [
    '{',
    '  "kv_namespaces": [{ "binding": "CLIPFORGE_BOT_KV", "id": "deadbeef" }],',
    '  "triggers": { "crons": ["* * * * *"] },',
    '  // Secret bindings (set via `wrangler secret put`, never committed):',
    '  //   TELEGRAM_BOT_TOKEN — Bot A\'s Telegram bot token',
    '}',
    '',
  ].join('\n');
  // Confirm the pre-fix parser SHAPE (comments-only strip, no trailing-comma
  // strip) really did fail on this — regression protection for the fix.
  const preFixStripped = failing
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^[ \t]*\/\/[^\n]*$/gm, '');
  assert.throws(() => JSON.parse(preFixStripped), /Expected double-quoted property name in JSON/);
  // And confirm the new helper accepts it.
  const obj = parseWranglerJsonc(failing);
  assert.deepEqual(obj.triggers, { crons: ['* * * * *'] });
  assert.equal(obj.kv_namespaces[0].id, 'deadbeef');
});

test('bug-62: parser tolerates trailing commas in nested arrays and objects', () => {
  const src = '{ "a": [1, 2, 3,], "b": { "c": true, }, }';
  assert.deepEqual(parseWranglerJsonc(src), { a: [1, 2, 3], b: { c: true } });
});

test('bug-62: stripping preserves line numbers of downstream errors', () => {
  // If a genuinely-broken file is passed through, the line where the
  // syntax error surfaces should still match the source line (comments and
  // trailing commas are blanked in place, not deleted), which keeps error
  // messages useful. Here line 3 has a genuinely-bad token.
  const src = '{\n  // a comment on line 2\n  not-a-string: 1\n}\n';
  const stripped = stripJsoncCommentsAndTrailingCommas(src);
  // The stripped output must still be 4 lines long (comment line preserved
  // as blank), so a JSON.parse error's line number lines up with the source.
  assert.equal(stripped.split('\n').length, src.split('\n').length);
});
