/**
 * New-video wizard — the single linear task-creation flow
 * (ARCHITECTURE.md §8.4). This module is PURE: no env, no I/O, no Telegram.
 * That keeps every transition unit-testable offline.
 *
 * Step order is fixed: source → focus → length → music → confirm.
 * bug-33: there is no mode step anymore — manual is the only path and /new
 * goes straight to the source step with mode pre-set to 'manual'.
 * The focus step is skipped when the series toggle is on (§8.4 step 3:
 * "series has no editorial focus").
 */

import { TARGET_DURATIONS } from './constants.js';
import { encodeToken, decodeToken } from './flow.js';

export const STEPS = ['source', 'focus', 'length', 'music', 'confirm'];

// §5 "Deliberately disabled" hosts — rejected at intake with a helpful
// message (ported verbatim from the legacy bot).
export const DISABLED_SOCIAL_HOSTS = [
  'youtube-nocookie.com', 'youtu.be', 'youtube.com',
  'vm.tiktok.com', 'vt.tiktok.com', 'tiktok.com',
  'fb.watch', 'facebook.com', 'instagram.com',
  'twitter.com', 'x.com', 'vimeo.com', 'redd.it', 'reddit.com'
];

export const TELEGRAM_PUBLIC_POST_RE = /^https?:\/\/(?:t\.me|telegram\.me)\/(?:s\/)?[A-Za-z0-9_]{5,64}\/[1-9][0-9]*(?:[/?#]|$)/i;
const DRIVE_RE = /^https?:\/\/(?:drive|docs)\.google\.com\//i;
const URL_RE = /^https?:\/\//i;
const MAGNET_RE = /^magnet:\?/i;

export const MAX_TORRENT_BYTES = 1024 * 1024; // §5: .torrent uploads ≤ 1 MB

export function newWizard() {
  // bug-33: no mode choice — manual is the only mode. Start directly at the
  // source step.
  return {
    step: 'source',
    jobId: `manual-${Date.now()}`, // assigned up-front (needed for torrent upload paths)
    mode: 'manual',
    series: false,
    source: null, // { kind, value } | { kind: 'torrent_file', value, fileName }
    focus: '',
    duration: null,
    music: null // { ref, source } — source: none | default | explicit_library
  };
}

/** Steps that actually apply to this wizard (focus skipped for series). */
export function stepsFor(wizard) {
  return STEPS.filter((step) => !(step === 'focus' && wizard && wizard.series === true));
}

export function nextStep(wizard) {
  const order = stepsFor(wizard);
  const index = order.indexOf(wizard.step);
  return order[Math.min(index + 1, order.length - 1)];
}

export function previousStep(wizard) {
  const order = stepsFor(wizard);
  const index = order.indexOf(wizard.step);
  return order[Math.max(index - 1, 0)];
}

function hostOf(url) {
  try { return new URL(url).hostname.toLowerCase(); } catch { return ''; }
}

/**
 * Classify a text message sent at the source step into a §5 source kind.
 * Returns { kind, value } or { error }.
 */
export function classifySourceText(text) {
  const value = String(text || '').trim();
  if (!value) return { error: 'Send a direct link, a magnet URI, a .torrent file, or a video.' };
  if (MAGNET_RE.test(value)) return { kind: 'magnet', value };
  if (TELEGRAM_PUBLIC_POST_RE.test(value)) return { kind: 'telegram_channel', value };
  if (URL_RE.test(value)) {
    const host = hostOf(value);
    const blocked = DISABLED_SOCIAL_HOSTS.find((entry) => host === entry || host.endsWith(`.${entry}`));
    if (blocked) {
      return {
        error: `Links from ${blocked} are not supported. Put the video on a public Telegram channel and send the t.me link, or send the video file directly.`
      };
    }
    if (DRIVE_RE.test(value)) return { kind: 'drive', value };
    return { kind: 'url', value };
  }
  return {
    error: 'That does not look like a supported source. Send a direct video URL (https://…), a Google Drive link, a magnet URI, a .torrent file, a public t.me channel-post link, or the video itself.'
  };
}

/** Human-readable one-liner for the confirm screen. */
export function describeSource(source) {
  if (!source) return '—';
  switch (source.kind) {
    case 'url': return `Direct link: ${source.value}`;
    case 'drive': return `Google Drive: ${source.value}`;
    case 'magnet': return 'Magnet URI';
    case 'torrent_file': return `.torrent file: ${source.fileName || 'source.torrent'}`;
    case 'telegram_channel': return `Telegram channel post: ${source.value}`;
    case 'telegram_relay': return 'Video sent directly to the bot';
    default: return String(source.kind || '—');
  }
}

export function describeMusic(music) {
  if (!music || music.source === 'none') return 'No music';
  if (music.source === 'default') return 'Saved default track';
  if (music.source === 'explicit_library') return `Library track: ${String(music.ref || '').replace(/^audio-library\//, '')}`;
  return '—';
}

export function describeMode(wizard) {
  // bug-30/33: manual is the only mode — it is not shown as a choice.
  const base = 'Manual (your external AI writes the plan)';
  return wizard.series ? `${base} · Series on` : base;
}

/** True when every wizard choice has been made and ▶ Start may run. */
export function wizardComplete(wizard) {
  return Boolean(
    wizard &&
    wizard.mode &&
    wizard.source &&
    wizard.duration &&
    wizard.music &&
    wizard.jobId
  );
}

/** Summary lines for the confirm screen (§8.4 step 6). */
export function wizardSummaryLines(wizard) {
  const lines = [
    `Mode: ${describeMode(wizard)}`,
    `Source: ${describeSource(wizard.source)}`
  ];
  if (!wizard.series) lines.push(`Focus: ${wizard.focus ? wizard.focus : 'whole video'}`);
  lines.push(`Length: ${wizard.duration}s`);
  lines.push(`Music: ${describeMusic(wizard.music)}`);
  return lines;
}

/**
 * Project the wizard into the spec consumed by github.buildStageARequest()
 * (which emits the §7.1 nested stage-a-request.json shape).
 */
export function wizardToRequest(wizard, seriesId) {
  const series = wizard.series === true;
  return {
    source: {
      kind: wizard.source.kind,
      value: wizard.source.value,
      ...(wizard.source.torrentFileIndex !== undefined && wizard.source.torrentFileIndex !== ''
        ? { torrent_file_index: String(wizard.source.torrentFileIndex) }
        : {})
    },
    options: {
      target_duration_seconds: wizard.duration,
      focus: series ? '' : String(wizard.focus || '')
    },
    mode: 'manual', // manual is the only mode
    series: {
      enabled: series,
      series_id: series ? String(seriesId || '') : '',
      // bug-64 (the real bug-61 follow-up): Part 1 is its own Stage A evidence
      // source, and its release is published under its JOB id — so
      // source_job_id must be wizard.jobId, NOT the series id. bug-61 seeded
      // series_id here on the mistaken assumption that series_id == Part 1's
      // job id; in reality series_id is series-<Date.now()> (index.js) while
      // the job id is manual-<Date.now()> (a different timestamp), so Part 2's
      // reuse step downloaded "clipforge-series-<ts>" — a release that never
      // exists — and died with "release not found".
      source_job_id: series ? String(wizard.jobId || '') : '',
      part: series ? 1 : 0,
      start_seconds: 0,
      context: ''
    },
    music: wizard.music && wizard.music.source !== 'none'
      ? { ref: String(wizard.music.ref || ''), source: wizard.music.source }
      : { ref: '', source: 'none' }
  };
}

// --- keyboards (pure) --------------------------------------------------- //

function nav(extra = []) {
  return [...extra, [{ text: 'Back', callback_data: 'wz:back' }, { text: 'Cancel', callback_data: 'wz:cancel' }]];
}

export function seriesKeyboard(wizard) {
  void wizard;
  // bug-50: the per-task series toggle is removed from /new. Series mode is
  // a default owned by main Settings; the source step only offers Cancel.
  return [
    [{ text: 'Cancel', callback_data: 'wz:cancel' }]
  ];
}

export function lengthKeyboard() {
  const label = (seconds) => (seconds === 60 ? '1 min' : seconds === 180 ? '3 min' : seconds === 300 ? '5 min' : `${seconds}s`);
  return [
    TARGET_DURATIONS.slice(0, 3).map((seconds) => ({ text: label(seconds), callback_data: `wz:dur:${seconds}` })),
    TARGET_DURATIONS.slice(3).map((seconds) => ({ text: label(seconds), callback_data: `wz:dur:${seconds}` })),
    [{ text: 'Back', callback_data: 'wz:back' }, { text: 'Cancel', callback_data: 'wz:cancel' }]
  ];
}

export function musicKeyboard() {
  return nav([
    [{ text: 'No music', callback_data: 'wz:music:none' }, { text: 'Use saved default', callback_data: 'wz:music:default' }],
    [{ text: 'Choose library track', callback_data: 'wz:music:library' }]
  ]);
}

export const LIBRARY_TRACKS_PER_PAGE = 6;

/** Paginated library picker for the wizard music step. */
export function libraryKeyboard(tracks, pageValue) {
  const totalPages = Math.max(1, Math.ceil(tracks.length / LIBRARY_TRACKS_PER_PAGE));
  const requested = Number(pageValue);
  const page = Number.isInteger(requested) ? Math.min(Math.max(requested, 0), totalPages - 1) : 0;
  const rows = [];
  for (let index = page * LIBRARY_TRACKS_PER_PAGE; index < Math.min(tracks.length, (page + 1) * LIBRARY_TRACKS_PER_PAGE); index += 1) {
    rows.push([{ text: `🎵 ${tracks[index].name}`.slice(0, 60), callback_data: `wz:lib:${index}` }]);
  }
  if (!tracks.length) rows.push([{ text: 'Library is empty — add tracks in Settings → Music library', callback_data: 'wz:music:library' }]);
  if (totalPages > 1) {
    const navigation = [];
    if (page > 0) navigation.push({ text: '◀ Prev', callback_data: `wz:libpage:${page - 1}` });
    if (page < totalPages - 1) navigation.push({ text: 'Next ▶', callback_data: `wz:libpage:${page + 1}` });
    rows.push(navigation);
  }
  rows.push([{ text: 'Back', callback_data: 'wz:music:menu' }, { text: 'Cancel', callback_data: 'wz:cancel' }]);
  return { rows, page };
}

export function confirmKeyboard() {
  return nav([[{ text: '▶ Start', callback_data: 'wz:confirm' }]]);
}

/** The prompt text + keyboard for a wizard step (text steps use no keyboard). */
export function stepPrompt(wizard, options = {}) {
  switch (wizard.step) {
    case 'source':
      return {
        // restore-bare-send-recognition: the reply-only copy shipped by 8f9e46e
        // is reverted. A bare send / forward is a legitimate answer again — the
        // awaiting_input marker (bot/migrations/0002_awaiting_input.sql) lets
        // handleMessage recognise it with no reply_to_message edge, exactly as
        // the legacy server-side state.flow did before kv-minimization.
        text: '<b>New video — step 1/5: source</b>\n\nSend or forward the video, or paste a direct link / Google Drive link / magnet URI / public t.me channel-post link, or upload a <code>.torrent</code> file (≤ 1 MB).',
        keyboard: seriesKeyboard(wizard)
      };
    case 'focus':
      return {
        text: '<b>New video — step 2/5: focus</b>\n\nOptionally narrow the analysis to one thread (e.g. <i>the trial cross-examination</i>), or send <code>-</code> for the whole video.',
        keyboard: nav([])
      };
    case 'length':
      return { text: '<b>New video — step 3/5: length</b>\n\nPick the target spoken-narration length (pacing only — footage runs as long as each scene needs).', keyboard: lengthKeyboard() };
    case 'music':
      return { text: '<b>New video — step 4/5: music</b>\n\nBackground music for the final render.', keyboard: musicKeyboard() };
    case 'confirm':
      return {
        text: `<b>New video — step 5/5: confirm</b>\n\n${wizardSummaryLines(wizard).join('\n')}`,
        keyboard: confirmKeyboard()
      };
    default:
      return { text: '<b>New video</b>', keyboard: nav([]) };
  }
}

// --- stateless wizard token (kv-minimization phase 5, ARCHITECTURE.md §8.9) --
//
// The full wizard record no longer lives in a stored state.pending.wizard.
// It rides inside the bot's OWN wizard messages as the invisible `wzs` flow
// marker (encodeToken JSON), so a button callback re-reads its wizard from
// callback.message.text and a force_reply answer re-reads it from
// message.reply_to_message. Markers are user-visible/user-replayable, so the
// decoder shape-validates every field before any handler may trust it.

const WIZARD_STEPS = new Set([...STEPS]);
const SOURCE_KINDS = new Set(['url', 'drive', 'magnet', 'torrent_file', 'telegram_channel', 'telegram_relay']);
const MUSIC_SOURCES = new Set(['none', 'default', 'explicit_library']);

/** wizard -> opaque base64url token for the `wzs` flow marker. */
export function encodeWizardToken(wizard) {
  return encodeToken({ v: 1, wizard });
}

/**
 * `wzs` marker arg -> wizard record, or null when the token is malformed or
 * fails shape validation. Tolerates older/newer records by keeping only the
 * fields the current step machine understands; anything unexpected drops the
 * whole token (the caller falls back to the "wizard expired, /new" path).
 */
export function decodeWizardToken(token) {
  const decoded = decodeToken(token);
  if (!decoded || decoded.v !== 1 || !decoded.wizard || typeof decoded.wizard !== 'object') return null;
  const source = decoded.wizard;
  const wizard = newWizard();

  if (typeof source.jobId === 'string' && /^manual-\d{6,20}$/.test(source.jobId)) wizard.jobId = source.jobId;
  if (typeof source.step === 'string' && WIZARD_STEPS.has(source.step)) wizard.step = source.step;
  if (source.series === true) wizard.series = true;

  if (source.source !== null && source.source !== undefined) {
    if (typeof source.source !== 'object') return null;
    const kind = String(source.source.kind || '');
    const value = String(source.source.value || '');
    if (!SOURCE_KINDS.has(kind) || !value) return null;
    wizard.source = { kind, value };
    if (typeof source.source.fileName === 'string' && source.source.fileName) {
      wizard.source.fileName = source.source.fileName.slice(0, 200);
    }
    if (source.source.torrentFileIndex !== undefined && source.source.torrentFileIndex !== null) {
      wizard.source.torrentFileIndex = String(source.source.torrentFileIndex).slice(0, 20);
    }
  }

  if (typeof source.focus === 'string') wizard.focus = source.focus.slice(0, 500);

  if (source.duration !== null && source.duration !== undefined) {
    const duration = Number(source.duration);
    if (!TARGET_DURATIONS.includes(duration)) return null;
    wizard.duration = duration;
  }

  if (source.music !== null && source.music !== undefined) {
    if (typeof source.music !== 'object') return null;
    const musicSource = String(source.music.source || '');
    if (!MUSIC_SOURCES.has(musicSource)) return null;
    wizard.music = { ref: String(source.music.ref || '').slice(0, 500), source: musicSource };
  }

  if (source.relay !== null && source.relay !== undefined) {
    if (typeof source.relay !== 'object') return null;
    // The §9.2 staging record: only the fields startRelayJob re-validates and
    // consumes (all re-checked for integer-ness there before any dispatch).
    wizard.relay = {
      file_unique_id: String(source.relay.file_unique_id || '').slice(0, 200),
      mime_type: String(source.relay.mime_type || '').slice(0, 200),
      file_size: Number(source.relay.file_size || 0),
      file_name: String(source.relay.file_name || '').slice(0, 200),
      duration: Number(source.relay.duration || 0),
      internal_group_chat_id: Number(source.relay.internal_group_chat_id || 0),
      internal_group_message_id: Number(source.relay.internal_group_message_id || 0)
    };
  }

  return wizard;
}
