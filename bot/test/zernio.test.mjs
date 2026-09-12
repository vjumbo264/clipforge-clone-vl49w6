import test from 'node:test';
import assert from 'node:assert/strict';

import {
  activeZernioAccounts,
  applySmartScheduleField,
  defaultZernioSettings,
  telegramButtonText,
  toggleZernioTarget,
  validZernioDateTime,
  validZernioTime,
  validZernioTimezone,
  zernioIntervalHours,
  zernioPublishKeyboard,
  zernioPublishText,
  zernioPublishingSummary,
  zernioRequestId,
  zernioScheduleKeyboard,
  zernioSettingsKeyboard,
  zernioSettingsOrDefault,
  zernioSettingsText,
  zernioTargets,
  zernioTargetsKeyboard,
  zernioTaskPublishButton,
} from '../src/zernio.js';

// ------------------------------------------------------------------------ //
// Settings normalization                                                    //
// ------------------------------------------------------------------------ //

test('defaultZernioSettings matches the legacy shape', () => {
  const settings = defaultZernioSettings();
  assert.equal(settings.version, 1);
  assert.equal(settings.enabled, false);
  assert.equal(settings.auto_publish, false);
  assert.equal(settings.automatic_mode, 'smart_schedule');
  assert.deepEqual(settings.target_accounts, { tiktok: [], youtube: [], instagram: [] });
  assert.deepEqual(settings.smart_schedule, {
    timezone: 'UTC', interval_hours: 24, preferred_time: '19:30',
    queue_depth: 4, start_mode: 'next_available', custom_start: ''
  });
});

test('zernioSettingsOrDefault tolerates missing/corrupt input', () => {
  assert.deepEqual(zernioSettingsOrDefault(null), defaultZernioSettings());
  assert.deepEqual(zernioSettingsOrDefault('junk'), defaultZernioSettings());
  assert.deepEqual(zernioSettingsOrDefault(42), defaultZernioSettings());
});

test('zernioSettingsOrDefault preserves explicit values and coerces types', () => {
  const settings = zernioSettingsOrDefault({
    enabled: true,
    auto_publish: true,
    automatic_mode: 'publish_now',
    target_accounts: { tiktok: ['A1', 7], youtube: 'oops', instagram: ['IG9'] },
    smart_schedule: {
      timezone: 'Europe/London', interval_hours: 6, preferred_time: '07:45',
      queue_depth: '9', start_mode: 'custom', custom_start: '2026-09-01T08:00'
    }
  });
  assert.equal(settings.enabled, true);
  assert.equal(settings.auto_publish, true);
  assert.equal(settings.automatic_mode, 'publish_now');
  assert.deepEqual(settings.target_accounts, { tiktok: ['A1', '7'], youtube: [], instagram: ['IG9'] });
  assert.equal(settings.smart_schedule.timezone, 'Europe/London');
  assert.equal(settings.smart_schedule.interval_hours, 6);
  assert.equal(settings.smart_schedule.preferred_time, '07:45');
  assert.equal(settings.smart_schedule.queue_depth, 9);
  assert.equal(settings.smart_schedule.start_mode, 'custom');
  assert.equal(settings.smart_schedule.custom_start, '2026-09-01T08:00');
});

test('zernioIntervalHours honors legacy interval_days carry-over', () => {
  assert.equal(zernioIntervalHours({ interval_hours: 12 }), 12);
  assert.equal(zernioIntervalHours({ interval_days: 2 }), 48);
  assert.equal(zernioIntervalHours({ interval_days: 365 }), 8760);
  assert.equal(zernioIntervalHours({ interval_days: 400 }), 24);
  assert.equal(zernioIntervalHours({ interval_hours: 0, interval_days: 3 }), 72);
  assert.equal(zernioIntervalHours({}), 24);
});

test('automatic_mode falls back to smart_schedule for unknown values', () => {
  assert.equal(zernioSettingsOrDefault({ automatic_mode: 'weird' }).automatic_mode, 'smart_schedule');
  assert.equal(zernioSettingsOrDefault({ automatic_mode: 'publish_now' }).automatic_mode, 'publish_now');
});

// ------------------------------------------------------------------------ //
// Field validators                                                          //
// ------------------------------------------------------------------------ //

test('validZernioTimezone accepts IANA names and UTC only', () => {
  assert.equal(validZernioTimezone('UTC'), true);
  assert.equal(validZernioTimezone('Europe/London'), true);
  assert.equal(validZernioTimezone('America/New_York'), true);
  assert.equal(validZernioTimezone('not/a/zone/;rm -rf /'), false);
  assert.equal(validZernioTimezone(''), false);
  assert.equal(validZernioTimezone('UTC; DROP'), false);
});

test('validZernioTime enforces 24h HH:MM', () => {
  assert.equal(validZernioTime('19:30'), true);
  assert.equal(validZernioTime('00:00'), true);
  assert.equal(validZernioTime('23:59'), true);
  assert.equal(validZernioTime('24:00'), false);
  assert.equal(validZernioTime('7:30'), false);
  assert.equal(validZernioTime('19:60'), false);
});

test('validZernioDateTime enforces local ISO without timezone suffix', () => {
  assert.equal(validZernioDateTime('2026-09-01T08:00'), true);
  assert.equal(validZernioDateTime('2026-09-01T08:00:30'), true);
  assert.equal(validZernioDateTime('2026-09-01 08:00'), false);
  assert.equal(validZernioDateTime('2026-09-01T08:00Z'), false);
  assert.equal(validZernioDateTime('tomorrow'), false);
});

test('applySmartScheduleField mutates the right field and validates', () => {
  const settings = defaultZernioSettings();
  applySmartScheduleField(settings, 'timezone', 'Europe/Berlin');
  assert.equal(settings.smart_schedule.timezone, 'Europe/Berlin');
  applySmartScheduleField(settings, 'interval', '48');
  assert.equal(settings.smart_schedule.interval_hours, 48);
  applySmartScheduleField(settings, 'time', '06:15');
  assert.equal(settings.smart_schedule.preferred_time, '06:15');
  applySmartScheduleField(settings, 'depth', '12');
  assert.equal(settings.smart_schedule.queue_depth, 12);
  applySmartScheduleField(settings, 'custom_start', '2026-09-01T08:00');
  assert.equal(settings.smart_schedule.start_mode, 'custom');
  assert.equal(settings.smart_schedule.custom_start, '2026-09-01T08:00');
});

test('applySmartScheduleField drops legacy interval_days on explicit edit', () => {
  const settings = defaultZernioSettings();
  settings.smart_schedule.interval_days = 2;
  applySmartScheduleField(settings, 'interval', '10');
  assert.equal(settings.smart_schedule.interval_hours, 10);
  assert.equal('interval_days' in settings.smart_schedule, false);
});

test('applySmartScheduleField rejects invalid input with user-safe errors', () => {
  const settings = defaultZernioSettings();
  assert.throws(() => applySmartScheduleField(settings, 'timezone', 'x;y'), /IANA timezone/);
  assert.throws(() => applySmartScheduleField(settings, 'interval', '0'), /1 to 8760/);
  assert.throws(() => applySmartScheduleField(settings, 'interval', '3.5'), /whole number/);
  assert.throws(() => applySmartScheduleField(settings, 'time', '25:00'), /HH:MM/);
  assert.throws(() => applySmartScheduleField(settings, 'depth', '0'), /1 to 100/);
  assert.throws(() => applySmartScheduleField(settings, 'custom_start', 'soon'), /YYYY-MM-DDTHH:MM/);
  assert.throws(() => applySmartScheduleField(settings, 'nope', 'x'), /Unknown smart-schedule field/);
});

// ------------------------------------------------------------------------ //
// Accounts + targets                                                        //
// ------------------------------------------------------------------------ //

const ACCOUNTS = [
  { platform: 'tiktok', id: 'TT1', username: 'tt_one', isActive: true },
  { platform: 'tiktok', id: 'TT2', displayName: 'Second', isActive: false },
  { platform: 'youtube', _id: 'YT1', username: 'yt_one' },
  { platform: 'youtube', id: 'YT2', needsReconnection: true },
  { platform: 'instagram', id: 'IG1' },
  { platform: 'tiktok', id: '' },
];

test('activeZernioAccounts filters to active tiktok/youtube/instagram only', () => {
  const active = activeZernioAccounts(ACCOUNTS);
  assert.deepEqual(active.tiktok.map((a) => a.id), ['TT1']);
  assert.deepEqual(active.youtube.map((a) => a.id), ['YT1']);
  // bug-52: Instagram accounts are now selectable.
  assert.deepEqual(active.instagram.map((a) => a.id), ['IG1']);
  assert.deepEqual(activeZernioAccounts(null), { tiktok: [], youtube: [], instagram: [] });
});

test('zernioTargets builds the publish.yml targets_json shape', () => {
  const settings = defaultZernioSettings();
  settings.target_accounts = { tiktok: ['TT1', 'TT2'], youtube: ['YT1', 'YT2'] };
  const targets = zernioTargets(settings, ACCOUNTS);
  assert.deepEqual(targets, [
    { platform: 'tiktok', account_ids: ['TT1'] },
    { platform: 'youtube', account_ids: ['YT1'] }
  ]);
});

test('zernioTargets returns [] when nothing usable is selected', () => {
  const settings = defaultZernioSettings();
  settings.target_accounts = { tiktok: ['TT2'], youtube: [] };
  assert.deepEqual(zernioTargets(settings, ACCOUNTS), []);
});

test('toggleZernioTarget adds and removes selections', () => {
  const settings = defaultZernioSettings();
  toggleZernioTarget(settings, ACCOUNTS, 'tiktok', 'TT1');
  assert.deepEqual(settings.target_accounts.tiktok, ['TT1']);
  toggleZernioTarget(settings, ACCOUNTS, 'tiktok', 'TT1');
  assert.deepEqual(settings.target_accounts.tiktok, []);
});

test('toggleZernioTarget refuses unavailable accounts and bad ids', () => {
  const settings = defaultZernioSettings();
  assert.throws(() => toggleZernioTarget(settings, ACCOUNTS, 'tiktok', 'TT2'), /unavailable or requires reconnection/);
  assert.throws(() => toggleZernioTarget(settings, ACCOUNTS, 'tiktok', 'ab'), /invalid/);
  // bug-52: Instagram accounts are toggleable when active...
  toggleZernioTarget(settings, ACCOUNTS, 'instagram', 'IG1');
  assert.deepEqual(settings.target_accounts.instagram, ['IG1']);
  // ...but unknown platforms and unknown instagram ids are still refused.
  assert.throws(() => toggleZernioTarget(settings, ACCOUNTS, 'facebook', 'FB1'), /invalid/);
  assert.throws(() => toggleZernioTarget(settings, ACCOUNTS, 'instagram', 'NOPE'), /unavailable or requires reconnection/);
});

// ------------------------------------------------------------------------ //
// Publishing summary + request id (§6.2 shape)                              //
// ------------------------------------------------------------------------ //

test('zernioTaskPublishButton swaps the Publish CTA for a status view once published', () => {
  // bug-62: published/partial -> status view (publish already happened);
  // publishing/scheduled -> in-flight status view; not_requested/failed/
  // cancelled keep the raw Publish button (a failed publish stays retryable).
  for (const [status, expected] of [
    [null, '📣 Publish (Zernio)'],
    [{ status: 'not_requested' }, '📣 Publish (Zernio)'],
    [{ status: 'failed' }, '📣 Publish (Zernio)'],
    [{ status: 'cancelled' }, '📣 Publish (Zernio)'],
    [{ status: 'published' }, '✅ View publish status'],
    [{ status: 'partial' }, '✅ View publish status'],
    [{ status: 'publishing' }, '⏳ View publish status'],
    [{ status: 'scheduled' }, '⏳ View publish status'],
  ]) {
    const btn = zernioTaskPublishButton(status, 'A');
    assert.equal(btn.text, expected, `publishing=${JSON.stringify(status)}`);
    // Every variant still opens the same task:pub menu (records + actions).
    assert.equal(btn.callback_data, 'task:pub:A');
  }
});

test('zernioPublishingSummary labels every §6.2 status', () => {
  assert.equal(zernioPublishingSummary(null), 'Zernio: not requested');
  assert.equal(zernioPublishingSummary({ status: 'not_requested', posts: [] }), 'Zernio: not requested');
  assert.equal(zernioPublishingSummary({ status: 'publishing', posts: [{}] }), 'Zernio: publishing · 1 post record(s)');
  assert.equal(zernioPublishingSummary({ status: 'scheduled', posts: [{}, {}] }), 'Zernio: scheduled · 2 post record(s)');
  assert.equal(zernioPublishingSummary({ status: 'published', posts: [{}] }), 'Zernio: published · 1 post record(s)');
  assert.equal(zernioPublishingSummary({ status: 'partial', posts: [{}, {}] }), 'Zernio: partially published · 2 post record(s)');
  assert.equal(zernioPublishingSummary({ status: 'failed', posts: [{}] }), 'Zernio: failed · 1 post record(s)');
  assert.equal(zernioPublishingSummary({ status: 'cancelled', posts: [{}] }), 'Zernio: cancelled · 1 post record(s)');
});

test('zernioRequestId reuses the key of a failed attempt only', () => {
  const failed = { status: 'failed', idempotency_key: 'clipforge-job-1-abc123' };
  assert.equal(zernioRequestId('job-1', failed), 'clipforge-job-1-abc123');
  const fresh = zernioRequestId('job-1', { status: 'published', idempotency_key: 'clipforge-job-1-abc123' });
  assert.match(fresh, /^clipforge-job-1-[a-z0-9]+$/);
  assert.notEqual(fresh, 'clipforge-job-1-abc123');
  const malformed = zernioRequestId('job-1', { status: 'failed', idempotency_key: 'bad key!' });
  assert.match(malformed, /^clipforge-job-1-[a-z0-9]+$/);
});

// ------------------------------------------------------------------------ //
// Keyboards + text                                                          //
// ------------------------------------------------------------------------ //

function makeConfig(overrides = {}) {
  const settings = defaultZernioSettings();
  return { settings, accounts: ACCOUNTS, secretConfigured: false, ...overrides };
}

test('zernioSettingsKeyboard reflects key + toggle state', () => {
  const off = zernioSettingsKeyboard(makeConfig());
  const flat = JSON.stringify(off);
  assert.match(flat, /Save Zernio API key/);
  assert.match(flat, /Enable publishing controls/);
  assert.match(flat, /Turn automatic publish on/);
  assert.match(flat, /set:zernio:key/);
  assert.match(flat, /zernio:refresh/);
  assert.match(flat, /zernio:targets/);
  assert.match(flat, /zernio:schedule/);
  assert.match(flat, /zernio:clear_prompt/);
  assert.match(flat, /menu:settings/);

  const settings = defaultZernioSettings();
  settings.enabled = true;
  settings.auto_publish = true;
  settings.automatic_mode = 'publish_now';
  const on = JSON.stringify(zernioSettingsKeyboard(makeConfig({ settings, secretConfigured: true })));
  assert.match(on, /Replace Zernio API key/);
  assert.match(on, /Disable publishing controls/);
  assert.match(on, /Turn automatic publish off/);
  assert.match(on, /Automatic: publish now/);
});

test('zernioScheduleKeyboard exposes all smart-schedule fields', () => {
  const settings = defaultZernioSettings();
  settings.smart_schedule.timezone = 'Europe/London';
  const keyboard = JSON.stringify(zernioScheduleKeyboard(settings));
  for (const action of ['zsch:timezone', 'zsch:interval', 'zsch:time', 'zsch:depth', 'zsch:start']) {
    assert.match(keyboard, new RegExp(action));
  }
  assert.match(keyboard, /Europe\/London/);
  assert.match(keyboard, /Start: next available/);
  settings.smart_schedule.start_mode = 'custom';
  settings.smart_schedule.custom_start = '2026-09-01T08:00';
  assert.match(JSON.stringify(zernioScheduleKeyboard(settings)), /Custom start: 2026-09-01T08:00/);
});

test('zernioTargetsKeyboard marks selection and unusable accounts', () => {
  const settings = defaultZernioSettings();
  settings.target_accounts.tiktok = ['TT1'];
  const keyboard = zernioTargetsKeyboard(settings, ACCOUNTS);
  const rows = keyboard.inline_keyboard;
  const t1 = rows.flat().find((b) => b.callback_data === 'ztarget:tiktok:TT1');
  assert.ok(t1.text.startsWith('✓ '));
  const t2 = rows.flat().find((b) => b.callback_data === 'ztarget:noop');
  assert.match(t2.text, /\(unavailable\)$/);
  assert.match(JSON.stringify(keyboard), /zernio:refresh/);
});

test('zernioTargetsKeyboard offers refresh when no accounts exist', () => {
  const keyboard = zernioTargetsKeyboard(defaultZernioSettings(), []);
  assert.equal(keyboard.inline_keyboard[0][0].text, 'No saved accounts — refresh first');
});

test('zernioPublishKeyboard gates new publishes on key+enabled+targets', () => {
  const label = 'A';
  // Not configured: no publish buttons, only navigation.
  const gated = zernioPublishKeyboard(makeConfig(), null, label);
  assert.doesNotMatch(JSON.stringify(gated), /task:pubnow/);

  // Fully configured: all three mode buttons.
  const settings = defaultZernioSettings();
  settings.enabled = true;
  settings.target_accounts.tiktok = ['TT1'];
  const config = makeConfig({ settings, secretConfigured: true });
  const open = JSON.stringify(zernioPublishKeyboard(config, { status: 'not_requested', posts: [] }, label));
  assert.match(open, /task:pubnow:A/);
  assert.match(open, /task:pubsmart:A/);
  assert.match(open, /task:pubmanual:A/);
});

test('zernioPublishKeyboard renders per-post actions by post state', () => {
  const settings = defaultZernioSettings();
  settings.enabled = true;
  settings.target_accounts.tiktok = ['TT1'];
  const config = makeConfig({ settings, secretConfigured: true });
  const publishing = {
    status: 'partial',
    posts: [
      { post_id: 'PFAILED1', platform: 'tiktok', status: 'failed' },
      { post_id: 'PSCHED01', platform: 'youtube', status: 'scheduled' },
      { post_id: 'PDONE001', platform: 'tiktok', status: 'published' },
      { post_id: 'bad id!', platform: 'tiktok', status: 'failed' },
    ]
  };
  const keyboard = JSON.stringify(zernioPublishKeyboard(config, publishing, 'B'));
  assert.match(keyboard, /task:pubretry:B:PFAILED1/);
  assert.match(keyboard, /task:pubpostnow:B:PFAILED1/);
  assert.match(keyboard, /task:pubcancel:B:PFAILED1/);
  assert.match(keyboard, /task:pubpostmanual:B:PSCHED01/);
  // Published post: no actions. Malformed id: skipped entirely.
  assert.doesNotMatch(keyboard, /PDONE001/);
  assert.doesNotMatch(keyboard, /bad id/);
});

test('zernioSettingsText and zernioPublishText never leak secrets and escape HTML', () => {
  const settings = defaultZernioSettings();
  settings.enabled = true;
  settings.auto_publish = true;
  settings.smart_schedule.timezone = 'Europe/<script>';
  const config = makeConfig({ settings, secretConfigured: true });
  const text = zernioSettingsText(config);
  assert.match(text, /secured in GitHub Actions \(opaque\)/);
  assert.match(text, /Europe\/&lt;script&gt;/);
  assert.doesNotMatch(text, /ZERNIO_API_KEY=[^\s]*/);
  // bug-52: the settings screen surfaces the Instagram Business/Creator
  // account requirement and the automatic Reel + Story behavior.
  assert.match(text, /Business or Creator/);
  assert.match(text, /Reel and a Story/);

  const publishText = zernioPublishText(config, { status: 'scheduled', posts: [{}] }, 'A</b>', 'job-1');
  assert.match(publishText, /A&lt;\/b&gt;/);
  assert.match(publishText, /Zernio: scheduled · 1 post record\(s\)/);
});

test('zernioPublishText explains each gating state', () => {
  const label = 'C';
  assert.match(zernioPublishText(makeConfig(), null, label, 'j'), /Save a Zernio API key/);

  const noControls = makeConfig({ secretConfigured: true });
  assert.match(zernioPublishText(noControls, null, label, 'j'), /Enable Zernio publishing controls/);

  const noTargets = makeConfig({ secretConfigured: true });
  noTargets.settings.enabled = true;
  assert.match(zernioPublishText(noTargets, null, label, 'j'), /Select at least one active TikTok, YouTube, or Instagram/);

  const ready = makeConfig({ secretConfigured: true });
  ready.settings.enabled = true;
  ready.settings.target_accounts.tiktok = ['TT1'];
  assert.match(zernioPublishText(ready, null, label, 'j'), /Targets: tiktok \(1\)/);
});

// ------------------------------------------------------------------------ //
// Button text truncation                                                    //
// ------------------------------------------------------------------------ //

test('telegramButtonText respects the byte budget incl. multibyte', () => {
  assert.equal(telegramButtonText('short'), 'short');
  const long = 'x'.repeat(200);
  const out = telegramButtonText(long, 60);
  assert.ok(new TextEncoder().encode(out).length <= 60);
  assert.ok(out.endsWith('…'));
  const multibyte = telegramButtonText('🎬'.repeat(40), 60);
  assert.ok(new TextEncoder().encode(multibyte).length <= 60);
  assert.equal(telegramButtonText(''), '—');
});
