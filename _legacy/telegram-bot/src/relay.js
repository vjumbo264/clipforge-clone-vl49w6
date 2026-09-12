const SAFE_JOB_RE = /^[A-Za-z0-9._-]{1,120}$/;
const SAFE_CHAT_RE = /^-?[1-9][0-9]{0,18}$/;
const SAFE_FILENAME_RE = /^[^/\\\u0000-\u001f\u007f]{1,240}$/;

// The handoff has to fit beneath GitHub Release's <2 GiB per-asset limit and
// leave useful processing headroom on standard hosted runners.
export const MAX_RELAY_VIDEO_BYTES = 1800 * 1024 * 1024;
export const RELAY_SOURCE_TYPE = 'telegram_bot_forward';
const CAPTION_PREFIX = 'CFRELAY1';
const READY_PREFIX = 'CFRELAY_READY1';

function requireJobId(value) {
  const jobId = String(value || '').trim();
  if (!SAFE_JOB_RE.test(jobId)) throw new Error('The relay job identifier is invalid.');
  return jobId;
}

function requireChatId(value) {
  const chatId = String(value || '').trim();
  if (!SAFE_CHAT_RE.test(chatId)) throw new Error('The relay chat identifier is invalid.');
  return chatId;
}

function maybeFilename(value, fallback) {
  const candidate = String(value || '').trim();
  return SAFE_FILENAME_RE.test(candidate) ? candidate : fallback;
}

function videoLikeDocument(document) {
  const mime = String(document && document.mime_type || '').toLowerCase();
  const filename = String(document && document.file_name || '').toLowerCase();
  return mime.startsWith('video/') || /\.(?:mp4|m4v|mov|mkv|webm|avi|mpeg|mpg)$/i.test(filename);
}

export function relayVideoMetadata(message) {
  const video = message && message.video;
  const document = message && message.document;
  const media = video || (videoLikeDocument(document) ? document : null);
  if (!media) return null;
  const size = Number(media.file_size || 0);
  if (!Number.isSafeInteger(size) || size <= 0) throw new Error('Telegram did not declare a valid video size. Send the video again as a normal video or video document.');
  if (size > MAX_RELAY_VIDEO_BYTES) {
    throw new Error(`This video is ${formatBytes(size)}, above ClipForge’s ${formatBytes(MAX_RELAY_VIDEO_BYTES)} direct-forward safety limit. Use a smaller source for now.`);
  }
  const fileId = String(media.file_id || '');
  const uniqueId = String(media.file_unique_id || '');
  if (!fileId || !uniqueId) throw new Error('Telegram did not provide a reusable media identifier. Send the video again.');
  const mime = String(media.mime_type || (video ? 'video/mp4' : 'application/octet-stream')).toLowerCase();
  return {
    source_type: RELAY_SOURCE_TYPE,
    media_kind: video ? 'video' : 'document_video',
    file_id: fileId,
    file_unique_id: uniqueId,
    file_size: size,
    mime_type: mime,
    file_name: maybeFilename(document && document.file_name, video ? 'telegram-video.mp4' : 'telegram-video.bin'),
    source_message_id: Number(message.message_id || 0),
  };
}

export function relayCaption(jobId, sourceChatId) {
  return `${CAPTION_PREFIX}:${requireJobId(jobId)}:${requireChatId(sourceChatId)}`;
}

export function parseRelayCaption(value) {
  const match = new RegExp(`^${CAPTION_PREFIX}:([A-Za-z0-9._-]{1,120}):(-?[1-9][0-9]{0,18})$`).exec(String(value || '').trim());
  return match ? { jobId: match[1], sourceChatId: Number(match[2]) } : null;
}

export function relayReadyMarker(jobId, sourceChatId, groupMessageId) {
  const groupId = Number(groupMessageId || 0);
  if (!Number.isSafeInteger(groupId) || groupId <= 0) throw new Error('The internal relay message identifier is invalid.');
  return `${READY_PREFIX}:${requireJobId(jobId)}:${requireChatId(sourceChatId)}:${groupId}`;
}

export function parseRelayReadyMarker(value) {
  const match = new RegExp(`^${READY_PREFIX}:([A-Za-z0-9._-]{1,120}):(-?[1-9][0-9]{0,18}):([1-9][0-9]{0,18})$`).exec(String(value || '').trim());
  return match ? { jobId: match[1], sourceChatId: Number(match[2]), groupMessageId: Number(match[3]) } : null;
}

// A command mention is deliberately supported as a privacy-mode-safe Bot A →
// Bot B signal. Telegram delivers a bot-to-bot command addressed to Bot B even
// if a delayed BotFather privacy change has not yet propagated to getMe().
export function parseRelayReadySignal(value) {
  const raw = String(value || '').trim();
  return parseRelayReadyMarker(raw) || parseRelayReadyMarker((/^\/relay@[A-Za-z0-9_]{5,64}\s+(.+)$/i.exec(raw) || [])[1]);
}

export function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return 'Unknown size';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} MiB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}

export const __test = { parseRelayCaption, parseRelayReadyMarker, parseRelayReadySignal, relayCaption, relayReadyMarker, relayVideoMetadata, videoLikeDocument };
