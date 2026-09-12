export const API_VERSION = '2022-11-28';
export const DEFAULT_BRANCH = 'main';
export const GEMINI_SECRET_NAME = 'GEMINI_API_KEYS';
export const GEMINI_KEYS_META_PATH = 'branding/gemini_keys.json';
export const WATERMARK_PATH = 'branding/creator_watermark.json';
export const TTS_SETTINGS_PATH = 'branding/tts_settings.json';
export const MUSIC_DEFAULT_PATH = 'branding/music_default.json';
export const ZERNIO_SECRET_NAME = 'ZERNIO_API_KEY';
export const ZERNIO_SETTINGS_PATH = 'branding/zernio_settings.json';
export const ZERNIO_ACCOUNTS_PATH = 'branding/zernio_accounts.json';
export const ZERNIO_QUEUE_PATH = 'branding/zernio_queue.json';
export const ZERNIO_WORKFLOW = 'zernio-publish.yml';
export const AUTOMATIC_MUSIC_PATH = (jobId) => `jobs/${jobId}/automatic_music.json`;
export const PRODUCTION_PATH = (jobId) => `jobs/${jobId}/production.json`;
export const STATUS_PATH = (jobId) => `jobs/${jobId}/status.json`;
export const STAGE_A_REQUEST_PATH = (jobId) => `jobs/${jobId}/stage-a-request.json`;

export const STAGES = new Set([
  'queued',
  'awaiting_torrent_selection',
  'stage_a_running',
  'automatic_analysis_running',
  'awaiting_json_upload',
  'stage_b_queued',
  'stage_b_running',
  'stage_b_cancelling',
  'cancelled',
  'complete',
  'error'
]);

export const STAGE_LABELS = {
  starting: 'Starting',
  queued: 'Queued',
  awaiting_torrent_selection: 'Needs torrent selection',
  stage_a_running: 'Stage A running',
  automatic_analysis_running: 'Automatic analysis running',
  awaiting_json_upload: 'Awaiting production plan',
  stage_b_queued: 'Stage B queued',
  stage_b_running: 'Stage B running',
  stage_b_cancelling: 'Stage B cancelling',
  cancelled: 'Cancelled',
  complete: 'Complete',
  error: 'Error'
};

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

export const COMMANDS = new Set([
  '/start', '/settings', '/tasks', '/status', '/manual', '/automatic', '/done', '/cancel', '/help'
]);

export function stageLabel(stage) {
  return STAGE_LABELS[stage] || `Unknown (${String(stage || 'missing')})`;
}

export function isTerminalStage(stage) {
  return ['complete', 'error', 'cancelled'].includes(stage);
}
