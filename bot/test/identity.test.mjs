import test from 'node:test';
import assert from 'node:assert/strict';

import { normalizeRepoSlug } from '../src/github.js';
import { isOriginalRepo, originalRepoFromEnv } from '../src/identity.js';

// bug-53: the main-owner gate must recognize the main account even when the
// stored repo value carries trivial formatting drift (case, whitespace, a
// pasted github.com URL, a trailing ".git"), and must never admit clones.

test('normalizeRepoSlug canonicalizes owner/name variants', () => {
  assert.equal(normalizeRepoSlug('motionssalt/clipforge'), 'motionssalt/clipforge');
  assert.equal(normalizeRepoSlug('Motionssalt/ClipForge'), 'motionssalt/clipforge');
  assert.equal(normalizeRepoSlug('  motionssalt/clipforge  '), 'motionssalt/clipforge');
  assert.equal(normalizeRepoSlug('motionssalt/clipforge.git'), 'motionssalt/clipforge');
  assert.equal(normalizeRepoSlug('motionssalt/clipforge/'), 'motionssalt/clipforge');
  assert.equal(normalizeRepoSlug('https://github.com/motionssalt/clipforge'), 'motionssalt/clipforge');
  assert.equal(normalizeRepoSlug('https://github.com/motionssalt/clipforge.git'), 'motionssalt/clipforge');
  assert.equal(normalizeRepoSlug('https://www.github.com/Motionssalt/ClipForge/'), 'motionssalt/clipforge');
  assert.equal(normalizeRepoSlug('git@github.com:motionssalt/clipforge.git'), 'motionssalt/clipforge');
  // A bare "host/owner/name" without a scheme is not a repo reference — two
  // slashes fail the single-slash owner/name pattern and normalize to ''.
  assert.equal(normalizeRepoSlug('github.com/motionssalt/clipforge'), '');
});

test('normalizeRepoSlug rejects non-repo values', () => {
  assert.equal(normalizeRepoSlug(''), '');
  assert.equal(normalizeRepoSlug(null), '');
  assert.equal(normalizeRepoSlug(undefined), '');
  assert.equal(normalizeRepoSlug('not-a-repo'), '');
  assert.equal(normalizeRepoSlug('owner'), '');
  assert.equal(normalizeRepoSlug('owner/'), '');
  assert.equal(normalizeRepoSlug('/name'), '');
});

test('isOriginalRepo recognizes the main account with drifted stored values', () => {
  const env = {};
  assert.equal(isOriginalRepo(env, 'motionssalt/clipforge'), true);
  assert.equal(isOriginalRepo(env, 'Motionssalt/ClipForge'), true);
  assert.equal(isOriginalRepo(env, ' motionssalt/clipforge '), true);
  assert.equal(isOriginalRepo(env, 'motionssalt/clipforge.git'), true);
  assert.equal(isOriginalRepo(env, 'https://github.com/motionssalt/clipforge'), true);
});

test('isOriginalRepo rejects clones and empty values', () => {
  const env = {};
  assert.equal(isOriginalRepo(env, 'someone/clipforge-clone-abc123'), false);
  assert.equal(isOriginalRepo(env, 'motionssalt/clipforge-fork'), false);
  assert.equal(isOriginalRepo(env, ''), false);
  assert.equal(isOriginalRepo(env, null), false);
  assert.equal(isOriginalRepo(env, undefined), false);
});

test('originalRepoFromEnv honors ORIGINAL_CLIPFORGE_REPOSITORY', () => {
  assert.equal(originalRepoFromEnv({}), 'motionssalt/clipforge');
  assert.equal(originalRepoFromEnv({ ORIGINAL_CLIPFORGE_REPOSITORY: 'Motionssalt/ClipForge' }), 'motionssalt/clipforge');
  const env = { ORIGINAL_CLIPFORGE_REPOSITORY: 'example/source' };
  assert.equal(isOriginalRepo(env, 'example/source'), true);
  assert.equal(isOriginalRepo(env, 'motionssalt/clipforge'), false);
});
