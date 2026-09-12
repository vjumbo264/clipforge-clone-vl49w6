#!/usr/bin/env node
/* Deterministic contract tests for Stage B restart music persistence. */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const workflow = fs.readFileSync(
  path.join(root, '.github', 'workflows', 'stage-b.yml'), 'utf8');

const match = app.match(/  function persistedMusicRef\(status\) \{[\s\S]*?\n  \}\n\n  async function restartStageB/);
assert(match, 'persistedMusicRef helper was not found');
const helperSource = match[0].replace(/\n\n  async function restartStageB$/, '');
const persistedMusicRef = vm.runInNewContext(`(${helperSource.trim()})`);

// Old jobs have no metadata and intentionally use the existing music.mp3
// lookup. An explicit empty selection is distinct from old missing metadata.
assert.strictEqual(persistedMusicRef(null), null);
assert.strictEqual(persistedMusicRef({ extra: {} }), null);
assert.strictEqual(
  persistedMusicRef({ extra: { music_ref: 'path:audio-library/steady-bed.mp3' } }),
  'path:audio-library/steady-bed.mp3'
);
assert.strictEqual(
  persistedMusicRef({ extra: { music_ref: ' path:jobs/abc/music.mp3 ' } }),
  'path:jobs/abc/music.mp3'
);
assert.strictEqual(persistedMusicRef({ extra: { music_ref: '' } }), '');
assert.strictEqual(persistedMusicRef({ extra: { music_ref: null } }), '');

assert(
  workflow.includes('--extra "music_ref=${{ github.event.inputs.music_ref }}"'),
  'Stage B must record its incoming music_ref in status metadata'
);
assert(
  app.includes("var musicRef = savedMusicRef;") &&
  app.includes("if (musicRef === null) {") &&
  app.includes("if (!state.musicDefaultLoaded) await loadMusicDefault();") &&
  app.includes("musicRef = resolvedLibraryMusicRef();") &&
  app.includes("await gh(base + '/music.mp3?ref=' + REF + '&_=' + Date.now());") &&
  app.includes('music_ref: musicRef,'),
  'restart must prefer persisted selection, then saved library default, then legacy job upload fallback'
);

console.log('PASS: restart retains library music, saved defaults, job uploads, and explicit no-music selection');
