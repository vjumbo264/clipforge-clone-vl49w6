// Unit tests for the shared bot-wide animation helpers (bot/src/anim.js)
// added in bug-60. These cover the pure design-language primitives and the
// rate-limit contract of flashThenView (at most one transient beat, then the
// final render — no edit loops).
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  PULSE_GLYPHS, dotsBar, flashThenView, isActiveState, loadingFrames,
  pulseFrames, pulseTaskLines, saveConfirmFlash, statusEmoji, toggleFeedback
} from '../src/anim.js';

test('pulseFrames rotates through the glyph set and wraps', () => {
  assert.equal(pulseFrames(0), PULSE_GLYPHS[0]);
  assert.equal(pulseFrames(1), PULSE_GLYPHS[1]);
  assert.equal(pulseFrames(PULSE_GLYPHS.length), PULSE_GLYPHS[0], 'wraps around');
  // Negative/garbage input never throws and always returns a valid glyph.
  assert.ok(PULSE_GLYPHS.includes(pulseFrames(-1)), 'negative input still yields a valid glyph');
  assert.ok(PULSE_GLYPHS.includes(pulseFrames(NaN)), 'NaN input still yields a valid glyph');
  assert.ok(PULSE_GLYPHS.includes(pulseFrames('x')), 'non-numeric input still yields a valid glyph');
});

test('loadingFrames produces trailing-dot beats', () => {
  assert.equal(loadingFrames('Working', 0), 'Working');
  assert.equal(loadingFrames('Working', 1), 'Working.');
  assert.equal(loadingFrames('Working', 2), 'Working..');
  assert.equal(loadingFrames('Working', 3), 'Working...');
  assert.equal(loadingFrames('Working', 4), 'Working', 'wraps after three dots');
});

test('dotsBar renders a fixed-width bar with a moving window', () => {
  const b0 = dotsBar(0, 12, 3);
  const b1 = dotsBar(1, 12, 3);
  assert.equal(b0.startsWith('▏') && b0.endsWith('▕'), true, 'bar is bracketed');
  // Inner track width is exactly `width`.
  assert.equal([...b0].length - 2, 12);
  assert.notEqual(b0, b1, 'window advances with the frame');
  // Window wraps within the track.
  assert.equal(dotsBar(0, 12, 3), dotsBar(10, 12, 3), 'frame wraps over the span');
});

test('statusEmoji maps every known state to one canonical glyph', () => {
  assert.equal(statusEmoji('complete'), '✅');
  assert.equal(statusEmoji('error'), '⚠️');
  assert.equal(statusEmoji('cancelled'), '⛔');
  assert.equal(statusEmoji('stage_a_running'), '⚙️');
  assert.equal(statusEmoji('stage_b_running'), '🎬');
  assert.equal(statusEmoji('awaiting_torrent_selection'), '📂');
  assert.equal(statusEmoji('', { unreadable: true }), '❔');
  assert.equal(statusEmoji('some_future_state'), '⏳', 'unknown states fall back to a neutral glyph');
});

test('isActiveState distinguishes working from terminal states', () => {
  for (const s of ['queued', 'stage_a_running', 'awaiting_torrent_selection', 'awaiting_plan', 'stage_b_queued', 'stage_b_running']) {
    assert.equal(isActiveState(s), true, s);
  }
  for (const s of ['complete', 'error', 'cancelled']) {
    assert.equal(isActiveState(s), false, s);
  }
});

test('pulseTaskLines pulses active jobs and glyph-marks terminal/unreadable ones', () => {
  const active = pulseTaskLines({ status: { state: 'stage_a_running' } }, 0);
  assert.ok(PULSE_GLYPHS.includes(active), 'active job pulses');
  assert.equal(pulseTaskLines({ status: { state: 'complete' } }), '✅');
  assert.equal(pulseTaskLines({ status: null }), '❔', 'unreadable status is explicit');
});

test('toggleFeedback reads as a clear on/off echo', () => {
  assert.equal(toggleFeedback('Series Mode', true), '✓ Series Mode turned on');
  assert.equal(toggleFeedback('Zernio publishing', false), '✗ Zernio publishing turned off');
});

test('saveConfirmFlash prepends a Saved flash line', () => {
  const out = saveConfirmFlash('<b>Settings</b>', 'voice set to <b>Ava</b>');
  assert.match(out, /^✅ <b>Saved<\/b> — voice set to <b>Ava<\/b>\n\n<b>Settings<\/b>$/);
});

test('flashThenView shows one transient beat then the final view (no edit loop)', async () => {
  const writes = [];
  const views = {
    renderInteractiveView: async (env, chatId, text, options, messageId) => {
      writes.push({ text, messageId: messageId ?? null });
      return { message_id: 900 + writes.length };
    }
  };
  const result = await flashThenView(views, {}, 7, 41, '⏳ beat', async (id) => {
    // The beat resolves to a message id the final render edits in place.
    assert.equal(id, 41, 'existing message id is threaded through');
    await views.renderInteractiveView({}, 7, 'final', {}, id);
    return 'done';
  });
  assert.equal(result, 'done');
  assert.equal(writes.length, 2, 'exactly one beat + one final render — no loop');
  assert.equal(writes[0].text, '⏳ beat');
  assert.equal(writes[1].text, 'final');
});

test('flashThenView still resolves the final view when the beat fails', async () => {
  const views = {
    renderInteractiveView: async (env, chatId, text) => {
      if (text === 'beat') throw new Error('edit failed');
      return { message_id: 55 };
    }
  };
  const result = await flashThenView(views, {}, 7, null, 'beat', async (id) => `resolved:${id}`);
  assert.equal(result, 'resolved:null', 'final render runs with the original id even if the beat throws');
});
