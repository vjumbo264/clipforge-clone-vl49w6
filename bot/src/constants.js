/**
 * ClipForge Bot A constants (new architecture).
 *
 * State names come from ARCHITECTURE.md §6.1 and are shared with
 * bot/src/jobs.js and pipeline/status.py — do not fork them here.
 */

export const API_VERSION = '2022-11-28';
export const DEFAULT_BRANCH = 'main';

export const GEMINI_SECRET_NAME = 'GEMINI_API_KEYS';
export const GEMINI_KEYS_META_PATH = 'branding/gemini_keys.json';
export const WATERMARK_PATH = 'branding/creator_watermark.json';
export const TTS_SETTINGS_PATH = 'branding/tts_settings.json';
export const MUSIC_DEFAULT_PATH = 'branding/music_default.json';
export const SERIES_SETTINGS_PATH = 'branding/series_settings.json';
export const ZERNIO_SECRET_NAME = 'ZERNIO_API_KEY';
export const ZERNIO_SETTINGS_PATH = 'branding/zernio_settings.json';
export const ZERNIO_ACCOUNTS_PATH = 'branding/zernio_accounts.json';

export const PRODUCTION_PATH = (jobId) => `jobs/${jobId}/production.json`;
export const STATUS_PATH = (jobId) => `jobs/${jobId}/status.json`;
export const STAGE_A_REQUEST_PATH = (jobId) => `jobs/${jobId}/stage-a-request.json`;

export const STAGE_A_WORKFLOW = 'stage-a.yml';
export const STAGE_B_WORKFLOW = 'stage-b.yml';
export const PUBLISH_WORKFLOW = 'publish.yml';

export const TARGET_DURATIONS = [30, 60, 120, 180, 300];
export const WHISPER_MODELS = new Set(['tiny', 'base', 'small']);

export const VOICES = {
  'en-US-AndrewNeural': { label: 'Andrew', gender: 'Male', style: 'Warm, confident, conversational' },
  'en-US-BrianNeural': { label: 'Brian', gender: 'Male', style: 'Approachable, casual, sincere' },
  'en-US-ChristopherNeural': { label: 'Christopher', gender: 'Male', style: 'Reliable, authoritative narrator' },
  'en-US-EricNeural': { label: 'Eric', gender: 'Male', style: 'Rational, measured narrator' },
  'en-US-GuyNeural': { label: 'Guy', gender: 'Male', style: 'Energetic news-style narrator' },
  'en-US-RogerNeural': { label: 'Roger', gender: 'Male', style: 'Lively narrator' },
  'en-US-AvaNeural': { label: 'Ava', gender: 'Female', style: 'Expressive, caring, conversational' },
  'en-US-AriaNeural': { label: 'Aria', gender: 'Female', style: 'Positive, confident, conversational' },
  'en-US-JennyNeural': { label: 'Jenny', gender: 'Female', style: 'Friendly, considerate narrator' },
  'en-US-MichelleNeural': { label: 'Michelle', gender: 'Female', style: 'Friendly, polished narrator' },
  'en-NG-AbeoNeural': { label: 'Abeo', gender: 'Male', style: 'Nigerian English, friendly and positive' },
  'en-NG-EzinneNeural': { label: 'Ezinne', gender: 'Female', style: 'Nigerian English, friendly and positive' }
};

export const DEFAULT_VOICE = 'en-US-AndrewNeural';

// §8.2: the entire command surface. /status is folded into /tasks.
export const COMMANDS = new Set(['/start', '/help', '/new', '/tasks', '/done', '/settings', '/cancel']);

/**
 * Plain-language rendering for task states an operator must recognize at a
 * glance (Bug 1/Bug 3 fix). Anything not listed falls back to the raw state
 * name. `unreadable` marks a task whose status document could not be read at
 * all — rendered distinctly instead of being folded into "queued".
 */
export function describeTaskState(status, { unreadable = false } = {}) {
  if (unreadable) return 'status unavailable — could not read the job record';
  const state = status && status.state ? String(status.state) : 'queued';
  if (state === 'awaiting_torrent_selection') return 'waiting for your file selection';
  return state;
}

export function escapeHtml(value) {
  return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

export function redact(value) {
  // Never echo token-shaped strings back to Telegram (§13 invariant #1).
  return String(value ?? '')
    .replace(/github_pat_[A-Za-z0-9_]+/g, 'github_pat_[REDACTED]')
    .replace(/ghp_[A-Za-z0-9]+/g, 'ghp_[REDACTED]')
    .replace(/AIza[A-Za-z0-9_-]+/g, 'AIza[REDACTED]');
}
