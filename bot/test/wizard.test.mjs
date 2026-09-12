import test from 'node:test';
import assert from 'node:assert/strict';

import {
  MAX_TORRENT_BYTES, TELEGRAM_PUBLIC_POST_RE, classifySourceText, decodeWizardToken, describeMusic,
  describeSource, encodeWizardToken, libraryKeyboard, nextStep, newWizard, previousStep, stepsFor,
  wizardComplete, wizardSummaryLines, wizardToRequest
} from '../src/wizard.js';
import { encodeToken } from '../src/flow.js';
import { taskKeyboard } from '../src/runtime.js';
import { homeText, settingsText } from '../src/views.js';

test('classifySourceText accepts every §5 source kind', () => {
  assert.deepEqual(classifySourceText('https://example.com/v.mp4'), { kind: 'url', value: 'https://example.com/v.mp4' });
  assert.equal(classifySourceText('https://drive.google.com/file/d/abc/view').kind, 'drive');
  assert.equal(classifySourceText('magnet:?xt=urn:btih:abc').kind, 'magnet');
  assert.equal(classifySourceText('https://t.me/somechannel/123').kind, 'telegram_channel');
});

test('classifySourceText rejects disabled social hosts with guidance', () => {
  for (const url of ['https://youtube.com/watch?v=x', 'https://youtu.be/x', 'https://www.tiktok.com/@a/video/1', 'https://x.com/a/status/1', 'https://instagram.com/reel/x']) {
    const result = classifySourceText(url);
    assert.ok(result.error, url);
    assert.match(result.error, /Telegram channel|not supported/);
  }
});

test('classifySourceText rejects junk input', () => {
  assert.ok(classifySourceText('').error);
  assert.ok(classifySourceText('hello world').error);
  assert.ok(classifySourceText('ftp://example.com/v.mp4').error);
});

test('public post regex matches channels, rejects profiles and groups', () => {
  assert.ok(TELEGRAM_PUBLIC_POST_RE.test('https://t.me/channelname/42'));
  assert.ok(TELEGRAM_PUBLIC_POST_RE.test('https://telegram.me/s/channelname/42'));
  assert.ok(!TELEGRAM_PUBLIC_POST_RE.test('https://t.me/channelname'));
  assert.ok(!TELEGRAM_PUBLIC_POST_RE.test('https://t.me/+joinhash'));
});

test('focus step is skipped when the series toggle is on (§8.4)', () => {
  const wizard = newWizard();
  assert.deepEqual(stepsFor(wizard), ['source', 'focus', 'length', 'music', 'confirm']);
  wizard.series = true;
  assert.deepEqual(stepsFor(wizard), ['source', 'length', 'music', 'confirm']);
});

test('bug-33: /new starts at the source step with manual pre-set', () => {
  const wizard = newWizard();
  assert.equal(wizard.step, 'source');
  assert.equal(wizard.mode, 'manual');
  assert.match(wizard.jobId, /^manual-\d+$/);
  assert.ok(!stepsFor(wizard).includes('mode'));
});

test('wizard step navigation walks forward and back', () => {
  const wizard = newWizard();
  assert.equal(wizard.step, 'source');
  assert.equal(nextStep(wizard), 'focus');
  wizard.series = true;
  assert.equal(nextStep(wizard), 'length');
  wizard.step = nextStep(wizard);
  assert.equal(previousStep(wizard), 'source');
  wizard.step = 'source';
  assert.equal(previousStep(wizard), 'source');
});

test('wizardToRequest projects the §7.1 shape', () => {
  const wizard = newWizard();
  wizard.jobId = 'manual-123';
  wizard.mode = 'manual';
  wizard.source = { kind: 'url', value: 'https://example.com/v.mp4' };
  wizard.focus = 'the verdict';
  wizard.duration = 60;
  wizard.music = { ref: 'audio-library/a.mp3', source: 'explicit_library' };
  const request = wizardToRequest(wizard, '');
  assert.equal(request.source.kind, 'url');
  assert.equal(request.options.target_duration_seconds, 60);
  assert.equal(request.options.focus, 'the verdict');
  assert.equal(request.mode, 'manual');
  assert.equal(request.series.enabled, false);
  assert.deepEqual(request.music, { ref: 'audio-library/a.mp3', source: 'explicit_library' });
  assert.ok(wizardComplete(wizard));
});

test('wizardToRequest blanks focus for series and stamps part 1', () => {
  const wizard = newWizard();
  wizard.jobId = 'manual-9';
  wizard.mode = 'manual';
  wizard.series = true;
  wizard.source = { kind: 'magnet', value: 'magnet:?xt=urn:btih:x' };
  wizard.focus = 'should be dropped';
  wizard.duration = 120;
  wizard.music = { ref: '', source: 'none' };
  const request = wizardToRequest(wizard, 'series-42');
  assert.equal(request.series.enabled, true);
  assert.equal(request.series.series_id, 'series-42');
  assert.equal(request.series.part, 1);
  assert.equal(request.options.focus, '');
  assert.deepEqual(request.music, { ref: '', source: 'none' });
  const lines = wizardSummaryLines(wizard);
  assert.ok(!lines.some((line) => line.startsWith('Focus:')));
});

test('wizardComplete requires all choices', () => {
  const wizard = newWizard();
  assert.equal(wizardComplete(wizard), false);
});

test('libraryKeyboard paginates and always offers back/cancel', () => {
  const tracks = Array.from({ length: 14 }, (_, i) => ({ name: `t${i}.mp3`, path: `audio-library/t${i}.mp3` }));
  const first = libraryKeyboard(tracks, 0);
  assert.equal(first.page, 0);
  assert.ok(first.rows.flat().some((b) => b.callback_data === 'wz:lib:0'));
  assert.ok(first.rows.flat().some((b) => b.callback_data === 'wz:libpage:1'));
  const last = libraryKeyboard(tracks, 99);
  assert.equal(last.page, 2);
  assert.ok(last.rows.flat().some((b) => b.callback_data === 'wz:cancel'));
});

test('describeSource / describeMusic render confirm lines', () => {
  assert.match(describeSource({ kind: 'url', value: 'https://x/v.mp4' }), /Direct link/);
  assert.match(describeSource({ kind: 'torrent_file', fileName: 'a.torrent' }), /a\.torrent/);
  assert.equal(describeMusic({ source: 'none' }), 'No music');
  assert.match(describeMusic({ source: 'explicit_library', ref: 'audio-library/x.mp3' }), /x\.mp3/);
});

test('taskKeyboard exposes only state-valid actions (§8.5)', () => {
  const flat = (status, label = 'A') => taskKeyboard(status, label).inline_keyboard.flat().map((b) => b.callback_data || b.text);
  assert.ok(flat({ state: 'awaiting_torrent_selection' }).some((d) => d === 'task:tsel:A:0'));
  const awaiting = flat({ state: 'awaiting_plan' });
  assert.ok(awaiting.includes('task:prompt:A') && awaiting.includes('task:upload:A'));
  assert.ok(flat({ state: 'stage_b_running' }).includes('task:cancelb:A'));
  // bug-15: a task that died inside Stage A (Stage B never began) must NOT
  // offer a Stage B restart; a Stage B failure offers both.
  const errA = flat({ state: 'error', message: 'Stage A failed. See workflow run for logs.' });
  assert.ok(errA.includes('task:restarta:A') && !errA.includes('task:restartb:A') && errA.includes('task:delfrom:A'));
  const errB = flat({ state: 'error', message: 'Stage B failed. See workflow run for logs.' });
  assert.ok(errB.includes('task:restarta:A') && errB.includes('task:restartb:A') && errB.includes('task:delfrom:A'));
  const done = flat({ state: 'complete', mode: 'manual', series: { enabled: true, part: 1, is_final: false } });
  assert.ok(done.includes('task:dl:A') && done.includes('task:pub:A') && done.includes('task:next:A'));
  const doneFinal = flat({ state: 'complete', mode: 'manual', series: { enabled: true, part: 3, is_final: true } });
  assert.ok(!doneFinal.includes('task:next:A'));
  assert.ok(flat({ state: 'queued' }).includes('task:open:A')); // refresh always present
});

test('home and settings screens render the §8.3/§8.6 summary', () => {
  const home = homeText({ repo: 'me/clone', narratorVoice: 'en-US-AvaNeural', seriesEnabled: true, zernioEnabled: false });
  assert.match(home, /me\/clone/);
  assert.match(home, /Narrator: Ava/);
  assert.match(home, /Series: on/);
  const settings = settingsText({ repo: '', narratorVoice: 'unknown', seriesEnabled: false, watermarkName: '', musicDefaultPath: '', zernioEnabled: false });
  assert.match(settings, /not connected/);
  assert.match(settings, /Narrator: Andrew/); // DEFAULT_VOICE fallback
});

test('torrent cap is 1 MB per §5', () => {
  assert.equal(MAX_TORRENT_BYTES, 1024 * 1024);
});

// --- stateless wizard token (kv-minimization phase 5 step 5.2) ----------- //
// The wizard record rides inside the bot's own messages as the `wzs` marker
// (ARCHITECTURE.md §8.9). Markers are user-replayable, so the decoder must
// shape-validate and must never let a hostile token reach a handler.

test('wizard token round-trips every field a live wizard carries', () => {
  const wizard = newWizard();
  wizard.step = 'music';
  wizard.series = true;
  wizard.source = { kind: 'telegram_relay', value: 'relay:private' };
  wizard.relay = {
    source_type: 'telegram_bot_forward', media_kind: 'video', file_id: 'x',
    file_unique_id: 'u1', file_size: 12345, mime_type: 'video/mp4',
    file_name: 'clip.mp4', source_message_id: 44, duration: 9,
    internal_group_chat_id: -100123, internal_group_message_id: 77
  };
  wizard.focus = '';
  wizard.duration = 180;
  wizard.music = { ref: 'audio-library/a b.m4a', source: 'explicit_library' };
  const decoded = decodeWizardToken(encodeWizardToken(wizard));
  assert.equal(decoded.jobId, wizard.jobId);
  assert.equal(decoded.step, 'music');
  assert.equal(decoded.series, true);
  assert.equal(decoded.mode, 'manual'); // constant, not carried
  assert.deepEqual(decoded.source, wizard.source);
  assert.equal(decoded.duration, 180);
  assert.deepEqual(decoded.music, wizard.music);
  // relay keeps exactly the subset startRelayJob re-validates and consumes
  assert.deepEqual(decoded.relay, {
    file_unique_id: 'u1', mime_type: 'video/mp4', file_size: 12345,
    file_name: 'clip.mp4', duration: 9,
    internal_group_chat_id: -100123, internal_group_message_id: 77
  });
});

test('decodeWizardToken rejects malformed or hostile tokens', () => {
  assert.equal(decodeWizardToken(''), null);
  assert.equal(decodeWizardToken('not-a-token'), null);
  assert.equal(decodeWizardToken(encodeToken({ v: 2, wizard: newWizard() })), null); // unknown version
  assert.equal(decodeWizardToken(encodeToken({ v: 1 })), null); // missing wizard
  const bad = (mutate) => {
    const record = { v: 1, wizard: { ...newWizard() } };
    mutate(record.wizard);
    return decodeWizardToken(encodeToken(record));
  };
  // unknown source kind / empty value must not survive
  assert.equal(bad((w) => { w.source = { kind: 'evil', value: 'x' }; }), null);
  assert.equal(bad((w) => { w.source = { kind: 'url', value: '' }; }), null);
  assert.equal(bad((w) => { w.source = 'url:https://x'; }), null);
  // duration outside TARGET_DURATIONS must not survive
  assert.equal(bad((w) => { w.duration = 42; }), null);
  // unknown music source must not survive
  assert.equal(bad((w) => { w.music = { ref: 'x', source: 'pirate' }; }), null);
});

test('decodeWizardToken sanitizes but keeps valid records (defaults applied)', () => {
  const record = { v: 1, wizard: { jobId: 'manual-1234567890', step: 'focus', focus: 'a'.repeat(900) } };
  const decoded = decodeWizardToken(encodeToken(record));
  assert.equal(decoded.jobId, 'manual-1234567890');
  assert.equal(decoded.step, 'focus');
  assert.equal(decoded.focus.length, 500); // clamped like handleWizardMessage
  assert.equal(decoded.series, false); // default, not inherited
  assert.equal(decoded.duration, null);
  assert.equal(decoded.music, null);
});

test('unknown step strings fall back to source, unknown jobIds to a fresh one', () => {
  const record = { v: 1, wizard: { jobId: '../evil', step: 'nope' } };
  const decoded = decodeWizardToken(encodeToken(record));
  assert.match(decoded.jobId, /^manual-\d+$/); // fresh id, hostile one dropped
  assert.equal(decoded.step, 'source');
});
