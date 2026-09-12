// kv-minimization phase 6: Bot A's own Telegram update dedup is REMOVED.
// The webhook must be idempotent by construction instead of gated by a
// stored telegram:update:{id} marker. These pins guard the removal and the
// untouched Bot B relay dedup (explicitly out of scope for this phase).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as storage from '../src/storage.js';

test('phase 6: storage.js no longer exports markUpdateSeen', () => {
  assert.equal(typeof storage.markUpdateSeen, 'undefined');
});

test('phase 6: index.js webhook path contains no update-dedup call', () => {
  const src = readFileSync(new URL('../src/index.js', import.meta.url), 'utf8');
  assert.ok(!src.includes('markUpdateSeen'), 'index.js must not reference markUpdateSeen');
});

test('phase 6: relay-worker.js relay dedup is untouched (out of scope)', () => {
  const src = readFileSync(new URL('../src/relay-worker.js', import.meta.url), 'utf8');
  assert.ok(src.includes('markRelayUpdateSeen') && src.includes('alreadySeenRelayUpdate'),
    'Bot B relay dedup must remain');
});
