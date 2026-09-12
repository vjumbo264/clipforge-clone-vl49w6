/**
 * ClipForge Bot A — Zernio publishing helpers (pure, no I/O).
 *
 * Port of the legacy telegram-bot Zernio logic (settings normalization,
 * validators, active-account filtering, target derivation, publishing
 * summary, request-id reuse, keyboards) onto the new architecture:
 *
 * - settings live at branding/zernio_settings.json (legacy shape preserved,
 *   incl. legacy interval_days -> interval_hours carry-over);
 * - the account snapshot lives at branding/zernio_accounts.json;
 * - the §6.2 status.publishing block is exactly { status, posts,
 *   idempotency_key } — this module only ever reads/derives from it, and the
 *   mode/schedule details it needs come from the branding/zernio_queue.json
 *   ledger (not schema-bound).
 *
 * Everything here is deterministic and unit-tested in bot/test/zernio.test.mjs.
 */

import { escapeHtml } from './constants.js';
import { buttons } from './telegram.js';

export const ZERNIO_PLATFORMS = ['tiktok', 'youtube', 'instagram'];
export const ZERNIO_PLATFORM_LABELS = { tiktok: 'TikTok', youtube: 'YouTube', instagram: 'Instagram' };

/**
 * bug-52: Instagram publishing requires a Business or Creator account
 * (personal accounts cannot post via the API) and every Instagram publish
 * automatically posts BOTH a Reel and a Story — no content-type selection.
 * Zernio also cannot fetch Instagram media from Google Drive / Dropbox /
 * OneDrive / iCloud share links; ClipForge always uploads the final MP4 via
 * Zernio's media presign endpoint and passes the direct CDN publicUrl, which
 * satisfies this requirement.
 */
export const INSTAGRAM_ACCOUNT_GUIDANCE =
  'Instagram publishing requires an Instagram <b>Business or Creator</b> account connected to Zernio (personal accounts cannot post via the API). Each Instagram publish automatically posts both a Reel and a Story.';
export const ZERNIO_MODES = ['publish_now', 'manual_schedule', 'smart_schedule'];
export const POST_ID_PATTERN = /^[A-Za-z0-9._-]{3,200}$/;
export const REQUEST_ID_PATTERN = /^[A-Za-z0-9._:-]{8,200}$/;

// ------------------------------------------------------------------------ //
// Text helpers                                                              //
// ------------------------------------------------------------------------ //

/** Truncate a button label to a Telegram-safe byte budget (legacy port). */
export function telegramButtonText(value, maximumBytes = 60) {
  const text = String(value ?? '').replace(/[\r\n]+/g, ' ').trim() || '—';
  const encoder = new TextEncoder();
  if (encoder.encode(text).length <= maximumBytes) return text;
  const suffix = '…';
  let output = '';
  for (const character of text) {
    if (encoder.encode(output + character + suffix).length > maximumBytes) break;
    output += character;
  }
  return `${output || '—'}${suffix}`;
}

// ------------------------------------------------------------------------ //
// Settings document (legacy shape, branding/zernio_settings.json)           //
// ------------------------------------------------------------------------ //

export function defaultZernioSettings() {
  return {
    version: 1,
    enabled: false,
    auto_publish: false,
    automatic_mode: 'smart_schedule',
    target_accounts: { tiktok: [], youtube: [], instagram: [] },
    smart_schedule: {
      timezone: 'UTC',
      interval_hours: 24,
      preferred_time: '19:30',
      queue_depth: 4,
      start_mode: 'next_available',
      custom_start: ''
    }
  };
}

/** Legacy carry-over: interval_days: 2 means 48 hours, not 2. */
export function zernioIntervalHours(smart) {
  const explicit = Number(smart && smart.interval_hours);
  if (Number.isInteger(explicit) && explicit >= 1 && explicit <= 8760) return explicit;
  const legacyDays = Number(smart && smart.interval_days);
  if (Number.isInteger(legacyDays) && legacyDays >= 1 && legacyDays <= 365) return legacyDays * 24;
  return 24;
}

/** Coerce any stored/missing/corrupt value into the canonical settings doc. */
export function zernioSettingsOrDefault(value) {
  const current = value && typeof value === 'object' ? value : {};
  const smart = current.smart_schedule && typeof current.smart_schedule === 'object' ? current.smart_schedule : {};
  return {
    version: 1,
    enabled: current.enabled === true,
    auto_publish: current.auto_publish === true,
    automatic_mode: current.automatic_mode === 'publish_now' ? 'publish_now' : 'smart_schedule',
    target_accounts: {
      tiktok: Array.isArray(current.target_accounts && current.target_accounts.tiktok)
        ? current.target_accounts.tiktok.map(String) : [],
      youtube: Array.isArray(current.target_accounts && current.target_accounts.youtube)
        ? current.target_accounts.youtube.map(String) : [],
      instagram: Array.isArray(current.target_accounts && current.target_accounts.instagram)
        ? current.target_accounts.instagram.map(String) : []
    },
    smart_schedule: {
      timezone: String(smart.timezone || 'UTC'),
      interval_hours: zernioIntervalHours(smart),
      preferred_time: /^\d\d:\d\d$/.test(String(smart.preferred_time || '')) ? String(smart.preferred_time) : '19:30',
      queue_depth: Number.isInteger(Number(smart.queue_depth)) ? Number(smart.queue_depth) : 4,
      start_mode: smart.start_mode === 'custom' ? 'custom' : 'next_available',
      custom_start: String(smart.custom_start || '')
    }
  };
}

// ------------------------------------------------------------------------ //
// Field validators (used by the settings text-input flows)                  //
// ------------------------------------------------------------------------ //

export function validZernioTimezone(value) {
  return /^(?:UTC|[A-Za-z_]+(?:\/[A-Za-z_+\-]+)+)$/.test(String(value || '').trim());
}

export function validZernioTime(value) {
  return /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(String(value || '').trim());
}

export function validZernioDateTime(value) {
  return /^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$/.test(String(value || '').trim());
}

/**
 * Apply one smart-schedule field edit (the `zsch:*` flows). Throws Error with
 * a user-safe message on invalid input. Returns the mutated settings doc.
 */
export function applySmartScheduleField(settings, field, rawValue) {
  const value = String(rawValue || '').trim();
  const smart = settings.smart_schedule;
  if (field === 'timezone') {
    if (!validZernioTimezone(value)) throw new Error('Enter a safe IANA timezone such as Europe/London, America/New_York, or UTC.');
    smart.timezone = value;
  } else if (field === 'interval') {
    const number = Number(value);
    if (!Number.isInteger(number) || number < 1 || number > 8760) throw new Error('Cadence must be a whole number from 1 to 8760 hours.');
    smart.interval_hours = number;
    delete smart.interval_days;
  } else if (field === 'time') {
    if (!validZernioTime(value)) throw new Error('Preferred time must use HH:MM in 24-hour format.');
    smart.preferred_time = value;
  } else if (field === 'depth') {
    const number = Number(value);
    if (!Number.isInteger(number) || number < 1 || number > 100) throw new Error('Queue depth must be a whole number from 1 to 100.');
    smart.queue_depth = number;
  } else if (field === 'custom_start') {
    if (!validZernioDateTime(value)) throw new Error('Send the first local slot as YYYY-MM-DDTHH:MM.');
    smart.start_mode = 'custom';
    smart.custom_start = value;
  } else {
    throw new Error('Unknown smart-schedule field.');
  }
  return settings;
}

// ------------------------------------------------------------------------ //
// Accounts + targets                                                        //
// ------------------------------------------------------------------------ //

/** Active, selectable accounts from the committed snapshot, per platform. */
export function activeZernioAccounts(accounts) {
  const out = { tiktok: [], youtube: [], instagram: [] };
  for (const account of Array.isArray(accounts) ? accounts : []) {
    const platform = String(account && account.platform || '').toLowerCase();
    const id = String(account && (account.id || account._id) || '').trim();
    if (!out[platform] || !id) continue;
    if (account.isActive === false || account.enabled === false || account.needsReconnection === true) continue;
    out[platform].push({
      id,
      platform,
      username: String(account.username || ''),
      displayName: String(account.displayName || '')
    });
  }
  return out;
}

/** publish.yml `targets_json` shape: [{ platform, account_ids: [...] }]. */
export function zernioTargets(settings, accounts) {
  const active = activeZernioAccounts(accounts);
  return ZERNIO_PLATFORMS.flatMap((platform) => {
    const selected = new Set(settings.target_accounts[platform] || []);
    const accountIds = active[platform].filter((account) => selected.has(account.id)).map((account) => account.id);
    return accountIds.length ? [{ platform, account_ids: accountIds }] : [];
  });
}

/** Toggle one account selection; refuses unavailable accounts. */
export function toggleZernioTarget(settings, accounts, platform, accountId) {
  if (!ZERNIO_PLATFORMS.includes(platform)) throw new Error('That Zernio account selection is invalid.');
  if (!POST_ID_PATTERN.test(String(accountId || ''))) throw new Error('That Zernio account selection is invalid.');
  const active = activeZernioAccounts(accounts)[platform];
  if (!active.some((account) => account.id === accountId)) {
    throw new Error('That Zernio account is unavailable or requires reconnection.');
  }
  const selected = new Set(settings.target_accounts[platform] || []);
  if (selected.has(accountId)) selected.delete(accountId);
  else selected.add(accountId);
  settings.target_accounts[platform] = [...selected];
  return settings;
}

// ------------------------------------------------------------------------ //
// status.publishing summary + per-post actions (§6.2 shape)                 //
// ------------------------------------------------------------------------ //

export function zernioPostId(post) {
  return String(post && (post.id || post.post_id || post._id) || '').trim();
}

/** One human line summarizing the §6.2 publishing block. */
export function zernioPublishingSummary(publishing) {
  const status = String(publishing && publishing.status || 'not_requested').toLowerCase();
  const label = status === 'published' ? 'published'
    : status === 'scheduled' ? 'scheduled'
    : status === 'publishing' ? 'publishing'
    : status === 'partial' ? 'partially published'
    : status === 'failed' ? 'failed'
    : status === 'cancelled' ? 'cancelled'
    : 'not requested';
  const posts = Array.isArray(publishing && publishing.posts) ? publishing.posts : [];
  return `Zernio: ${label}${posts.length ? ` · ${posts.length} post record(s)` : ''}`;
}

/**
 * Idempotency key for a new publish: reuse the prior key when the last
 * attempt failed (same logical publish), otherwise mint a fresh one.
 */
export function zernioRequestId(jobId, publishing) {
  const prior = String(publishing && publishing.status || '').toLowerCase() === 'failed'
    ? String(publishing.idempotency_key || '') : '';
  if (REQUEST_ID_PATTERN.test(prior)) return prior;
  return `clipforge-${jobId}-${Date.now().toString(36)}`;
}

// ------------------------------------------------------------------------ //
// Keyboards (markup only — routing lives in index.js)                       //
// ------------------------------------------------------------------------ //

export function zernioSettingsKeyboard(config) {
  const settings = config.settings;
  return buttons([
    [
      { text: config.secretConfigured ? 'Replace Zernio API key' : 'Save Zernio API key', callback_data: 'set:zernio:key' },
      { text: 'Refresh accounts', callback_data: 'zernio:refresh' }
    ],
    [
      { text: settings.enabled ? 'Disable publishing controls' : 'Enable publishing controls', callback_data: 'zernio:toggle_enabled' },
      { text: settings.auto_publish ? 'Turn automatic publish off' : 'Turn automatic publish on', callback_data: 'zernio:toggle_auto' }
    ],
    [
      { text: settings.automatic_mode === 'publish_now' ? 'Automatic: publish now' : 'Automatic: smart schedule', callback_data: 'zernio:mode' },
      { text: 'Select target accounts', callback_data: 'zernio:targets' }
    ],
    [
      { text: 'Smart schedule', callback_data: 'zernio:schedule' },
      { text: 'Remove API key', callback_data: 'zernio:clear_prompt' }
    ],
    [{ text: '← Settings', callback_data: 'menu:settings' }]
  ]);
}

export function zernioScheduleKeyboard(settings) {
  const smart = settings.smart_schedule;
  return buttons([
    [
      { text: `Timezone: ${telegramButtonText(smart.timezone, 42)}`, callback_data: 'zsch:timezone' },
      { text: `Cadence: ${smart.interval_hours} hour(s)`, callback_data: 'zsch:interval' }
    ],
    [
      { text: `Preferred time: ${smart.preferred_time}`, callback_data: 'zsch:time' },
      { text: `Queue depth: ${smart.queue_depth}`, callback_data: 'zsch:depth' }
    ],
    [{
      text: smart.start_mode === 'custom'
        ? `Custom start: ${telegramButtonText(smart.custom_start || 'missing', 40)}`
        : 'Start: next available',
      callback_data: 'zsch:start'
    }],
    [{ text: '← Zernio settings', callback_data: 'set:zernio' }]
  ]);
}

export function zernioTargetsKeyboard(settings, accounts) {
  const rows = [];
  for (const account of Array.isArray(accounts) ? accounts : []) {
    const platform = String(account && account.platform || '').toLowerCase();
    const id = String(account && (account.id || account._id) || '').trim();
    if (!ZERNIO_PLATFORMS.includes(platform) || !id) continue;
    const usable = account.isActive !== false && account.enabled !== false && account.needsReconnection !== true;
    const selected = (settings.target_accounts[platform] || []).includes(id);
    const title = `${ZERNIO_PLATFORM_LABELS[platform] || platform} · ${account.displayName || account.username || id}`;
    rows.push([{
      text: `${selected ? '✓ ' : ''}${telegramButtonText(title, 50)}${usable ? '' : ' (unavailable)'}`,
      callback_data: usable ? `ztarget:${platform}:${id}` : 'ztarget:noop'
    }]);
  }
  if (!rows.length) rows.push([{ text: 'No saved accounts — refresh first', callback_data: 'zernio:refresh' }]);
  rows.push([
    { text: 'Refresh accounts', callback_data: 'zernio:refresh' },
    { text: '← Zernio settings', callback_data: 'set:zernio' }
  ]);
  return buttons(rows);
}

/**
 * Per-task publish menu rows for a complete job. `config` is
 * { settings, accounts, secretConfigured }; `publishing` is the §6.2 block.
 * Post-action buttons are only offered for states where they are meaningful.
 */
export function zernioPublishKeyboard(config, publishing, label) {
  const rows = [];
  const targets = zernioTargets(config.settings, config.accounts);
  if (config.secretConfigured && config.settings.enabled && targets.length) {
    rows.push([
      { text: 'Publish now', callback_data: `task:pubnow:${label}` },
      { text: 'Smart schedule', callback_data: `task:pubsmart:${label}` }
    ]);
    rows.push([{ text: 'Choose date and time', callback_data: `task:pubmanual:${label}` }]);
  }
  const posts = Array.isArray(publishing && publishing.posts) ? publishing.posts : [];
  for (const post of posts.slice(0, 6)) {
    const postId = zernioPostId(post);
    if (!POST_ID_PATTERN.test(postId)) continue;
    const state = String(post.status || post.state || '').toLowerCase();
    const platform = String(post.platform || 'post');
    if (['failed', 'error', 'partial'].includes(state)) {
      rows.push([{ text: `Retry ${telegramButtonText(platform, 20)}`, callback_data: `task:pubretry:${label}:${postId}` }]);
    }
    if (['scheduled', 'requested', 'publishing', 'partial', 'failed', 'error'].includes(state)) {
      rows.push([
        { text: `Publish ${telegramButtonText(platform, 16)} now`, callback_data: `task:pubpostnow:${label}:${postId}` },
        { text: `Reschedule ${telegramButtonText(platform, 16)}`, callback_data: `task:pubpostmanual:${label}:${postId}` }
      ]);
      rows.push([{ text: `Cancel ${telegramButtonText(platform, 20)}`, callback_data: `task:pubcancel:${label}:${postId}` }]);
    }
  }
  // bug-47: Zernio CONFIGURATION lives only in the main Settings menu — the
  // completed-task publish view keeps its publish actions but no longer
  // carries a duplicate settings shortcut.
  rows.push([
    { text: `← Task ${label}`, callback_data: `task:open:${label}` }
  ]);
  return buttons(rows);
}

/**
 * bug-62: the per-task publish affordance must reflect the §6.2 publishing
 * state. Once a task has already been published — automatically or manually —
 * the raw '📣 Publish (Zernio)' call-to-action is redundant and misleading, so
 * it is replaced with a status-view affordance. Both open the SAME task:pub
 * menu (which carries the publishing summary plus per-post retry/cancel
 * actions), so nothing is lost. not_requested/failed/cancelled keep the
 * original publish button (a failed publish stays retryable).
 */
export function zernioTaskPublishButton(publishing, label) {
  const status = String(publishing && publishing.status || 'not_requested').toLowerCase();
  if (status === 'published' || status === 'partial') {
    return { text: '✅ View publish status', callback_data: `task:pub:${label}` };
  }
  if (status === 'publishing' || status === 'scheduled') {
    return { text: '⏳ View publish status', callback_data: `task:pub:${label}` };
  }
  return { text: '📣 Publish (Zernio)', callback_data: `task:pub:${label}` };
}

/** Body text for the Zernio settings screen. */
export function zernioSettingsText(config) {
  const settings = config.settings;
  const active = activeZernioAccounts(config.accounts);
  const selected = zernioTargets(settings, config.accounts);
  const lines = ['<b>Zernio publishing</b>'];
  lines.push(`API key: ${config.secretConfigured ? 'secured in GitHub Actions (opaque)' : 'not configured'}`);
  lines.push(`Controls: ${settings.enabled ? 'enabled' : 'disabled'}`);
  lines.push(`Automatic publishing: ${settings.auto_publish ? (settings.automatic_mode === 'publish_now' ? 'publish now' : 'smart schedule') : 'off'}`);
  lines.push(`Targets: ${selected.length ? selected.map((group) => `${group.platform}: ${group.account_ids.length}`).join(' · ') : 'none selected'}`);
  lines.push(`Accounts: ${active.tiktok.length} TikTok · ${active.youtube.length} YouTube · ${active.instagram.length} Instagram active`);
  // bug-52: surface the Business/Creator requirement wherever connection
  // guidance is shown for Instagram.
  lines.push(INSTAGRAM_ACCOUNT_GUIDANCE);
  lines.push(`Smart schedule: ${escapeHtml(settings.smart_schedule.timezone)} · every ${settings.smart_schedule.interval_hours} hour(s) · preferred time ${settings.smart_schedule.preferred_time} · queue ${settings.smart_schedule.queue_depth}`);
  if (settings.smart_schedule.start_mode === 'custom') {
    lines.push(`First slot: ${escapeHtml(settings.smart_schedule.custom_start || 'missing')}`);
  }
  lines.push('The API key is encrypted into <code>ZERNIO_API_KEY</code>, never committed or displayed. Preferences and account snapshots stay inside this clone.');
  return lines.join('\n');
}

/** Body text for the per-task publish screen. */
export function zernioPublishText(config, publishing, label, jobId) {
  const targets = zernioTargets(config.settings, config.accounts);
  const lines = [
    `<b>Task ${escapeHtml(label)} — Zernio publishing</b>`,
    `<code>${escapeHtml(jobId)}</code>`,
    zernioPublishingSummary(publishing)
  ];
  if (!config.secretConfigured) lines.push('Save a Zernio API key in settings before submitting a request.');
  else if (!config.settings.enabled) lines.push('Enable Zernio publishing controls in settings before submitting a request.');
  else if (!targets.length) lines.push('Select at least one active TikTok, YouTube, or Instagram target account in settings.');
  else {
    lines.push(`Targets: ${targets.map((group) => `${group.platform} (${group.account_ids.length})`).join(' · ')}`);
    lines.push(`Timezone: ${escapeHtml(config.settings.smart_schedule.timezone)}`);
  }
  return lines.join('\n');
}
