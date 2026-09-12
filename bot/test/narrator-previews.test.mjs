import test from 'node:test';
import assert from 'node:assert/strict';
import { access, stat } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { VOICES } from '../src/constants.js';
import { sourcePathAllowed } from '../src/github.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

test('every Bot A narrator preview is committed and inherited by private Shadow Clones', async () => {
  const voiceIds = Object.keys(VOICES).sort();
  assert.equal(voiceIds.length, 12);
  for (const voiceId of voiceIds) {
    const sourcePath = `assets/tts-previews/${voiceId}.mp3`;
    const previewPath = path.join(root, sourcePath);
    await access(previewPath);
    const preview = await stat(previewPath);
    assert.ok(preview.size > 0, `${sourcePath} must not be empty`);
    assert.equal(sourcePathAllowed(sourcePath), true, `${sourcePath} must be copied to private Shadow Clones`);
  }
});
