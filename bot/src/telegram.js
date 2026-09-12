/**
 * Thin Telegram Bot API client for Bot A (Worker-safe: fetch + Web Crypto
 * only). Ported essentially verbatim from _legacy/telegram-bot/src/telegram.js.
 */

// restore-bare-send-recognition: the single choke point for awaiting_input
// writes. flow.js is PURE (no I/O) and storage.js imports only crypto.js,
// so these imports introduce no cycle (commands/* import telegram.js; the
// reverse never happens).
import { extractFlowPayload, parsePayload } from './flow.js';
import { putAwaitingInput } from './storage.js';

function telegramUrl(token, method) {
  return `https://api.telegram.org/bot${token}/${method}`;
}

export class TelegramError extends Error {
  constructor(description, body = null) {
    super(description);
    this.name = 'TelegramError';
    const migrated = body && body.parameters && Number(body.parameters.migrate_to_chat_id);
    this.migrateToChatId = Number.isSafeInteger(migrated) ? migrated : null;
  }
}

export async function telegram(env, method, payload) {
  const response = await fetch(telegramUrl(env.TELEGRAM_BOT_TOKEN, method), {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
  });
  const body = await response.json().catch(() => null);
  if (!response.ok || !body || body.ok !== true) {
    const description = body && body.description ? `Telegram could not complete that request: ${body.description}` : 'Telegram could not complete that request.';
    throw new TelegramError(description, body);
  }
  return body.result;
}

export async function sendMessage(env, chatId, text, options = {}) {
  return telegram(env, 'sendMessage', {
    chat_id: chatId,
    text,
    parse_mode: options.parseMode || 'HTML',
    disable_web_page_preview: options.disablePreview !== false,
    ...(options.replyMarkup ? { reply_markup: options.replyMarkup } : {})
  });
}

export async function editMessage(env, chatId, messageId, text, options = {}) {
  return telegram(env, 'editMessageText', {
    chat_id: chatId,
    message_id: messageId,
    text,
    parse_mode: options.parseMode || 'HTML',
    disable_web_page_preview: options.disablePreview !== false,
    ...(options.replyMarkup ? { reply_markup: options.replyMarkup } : {})
  });
}

export async function deleteMessage(env, chatId, messageId) {
  return telegram(env, 'deleteMessage', { chat_id: chatId, message_id: messageId });
}

/**
 * kv-minimization phase 5: send an input prompt as a ForceReply so the
 * user's answer arrives as message.reply_to_message pointing back at the
 * prompt — that reply edge, plus the invisible flow marker in the prompt
 * text (src/flow.js), replaces the old stored state.flow/state.pending.
 * `options.inlineRows` attaches a normal inline keyboard to the SAME
 * message (e.g. a Cancel button); Telegram allows both markups together.
 */
export async function sendForceReply(env, chatId, text, options = {}) {
  const result = await telegram(env, 'sendMessage', {
    chat_id: chatId,
    text,
    parse_mode: options.parseMode || 'HTML',
    disable_web_page_preview: options.disablePreview !== false,
    reply_markup: options.inlineRows
      ? { inline_keyboard: options.inlineRows, force_reply: true, selective: true }
      : { force_reply: true, selective: true }
  });
  // restore-bare-send-recognition (single choke point): if this prompt
  // carries a flow marker, record it in awaiting_input so a BARE send or a
  // forward (which never carries reply_to_message) routes exactly like a
  // genuine reply. The marker appended by withFlowMarker always sits at the
  // END of the text ('\n<a href="...cf:<op>:…">ZWSP</a>'), so its entity
  // offset/length are known arithmetically — no HTML parsing needed. The
  // upsert is ONE write per prompt-sent. A D1 hiccup must never break prompt
  // delivery, so a write failure is swallowed (the reply path still works).
  const marker = flowMarkerHtmlOf(text);
  if (marker) {
    const parsed = parsePayload(marker.payload);
    if (parsed) {
      try {
        await putAwaitingInput(env, chatId, parsed.op, marker.payload);
      } catch (error) {
        console.warn('awaiting_input upsert failed', error && error.message ? error.message : error);
      }
    }
  }
  return result;
}

/**
 * Re-derive the flow marker embedded by flow.js withFlowMarker from an
 * outgoing prompt's text: returns { payload } or null. The entity fed to
 * extractFlowPayload uses the marker's known trailing position/length, so
 * the SAME parser that reads replies validates the stored value.
 */
function flowMarkerHtmlOf(text) {
  const raw = String(text || '');
  const newline = raw.lastIndexOf('\n');
  if (newline < 0) return null;
  const markerHtml = raw.slice(newline + 1);
  const hrefStart = markerHtml.indexOf('href="');
  const hrefEnd = hrefStart < 0 ? -1 : markerHtml.indexOf('"', hrefStart + 6);
  if (hrefStart < 0 || hrefEnd < 0) return null;
  const url = markerHtml.slice(hrefStart + 6, hrefEnd);
  const payload = extractFlowPayload({
    entities: [{ type: 'text_link', url, offset: newline + 1, length: markerHtml.length }]
  });
  return payload ? { payload } : null;
}

export async function answerCallback(env, callbackId, text = '') {
  return telegram(env, 'answerCallbackQuery', { callback_query_id: callbackId, ...(text ? { text } : {}) });
}

// Copies Telegram-side media without downloading bytes into the Worker. A new
// caption binds the copy to one ClipForge job without exposing the sender.
export async function copyMessage(env, toChatId, fromChatId, messageId, caption) {
  return telegram(env, 'copyMessage', {
    chat_id: toChatId,
    from_chat_id: fromChatId,
    message_id: messageId,
    caption,
    disable_notification: true,
  });
}

async function telegramMultipart(env, method, fields, fileField, bytes, fileName, mimeType) {
  const form = new FormData();
  for (const [name, value] of Object.entries(fields)) {
    if (value !== undefined && value !== null) form.set(name, typeof value === 'string' ? value : JSON.stringify(value));
  }
  form.set(fileField, new Blob([bytes], { type: mimeType }), fileName);
  const response = await fetch(telegramUrl(env.TELEGRAM_BOT_TOKEN, method), { method: 'POST', body: form });
  const body = await response.json().catch(() => null);
  if (!response.ok || !body || body.ok !== true) throw new Error('Telegram could not deliver the requested file.');
  return body.result;
}

function audioMimeType(filename) {
  const extension = String(filename || '').split('.').pop().toLowerCase();
  return ({ mp3: 'audio/mpeg', m4a: 'audio/mp4', aac: 'audio/aac', wav: 'audio/wav', ogg: 'audio/ogg', opus: 'audio/ogg', flac: 'audio/flac' })[extension] || 'application/octet-stream';
}

export async function sendAudioBytes(env, chatId, bytes, filename, caption) {
  return telegramMultipart(env, 'sendAudio', {
    chat_id: chatId,
    caption,
    parse_mode: 'HTML',
    title: filename.replace(/\.[^.]+$/i, ''),
  }, 'audio', bytes, filename, audioMimeType(filename));
}

// bug-22: on-demand delivery of a finished video (< 50 MB) straight into the
// user's chat. supports_streaming keeps it playable inline in the client.
export async function sendVideoBytes(env, chatId, bytes, filename, caption, replyMarkup = null) {
  return telegramMultipart(env, 'sendVideo', {
    chat_id: chatId,
    caption,
    parse_mode: 'HTML',
    supports_streaming: true,
    ...(replyMarkup ? { reply_markup: replyMarkup } : {})
  }, 'video', bytes, filename, 'video/mp4');
}

export async function sendDocumentBytes(env, chatId, bytes, filename, caption, replyMarkup = null) {
  return telegramMultipart(env, 'sendDocument', {
    chat_id: chatId,
    caption,
    parse_mode: 'HTML',
    ...(replyMarkup ? { reply_markup: replyMarkup } : {})
  }, 'document', bytes, filename, 'text/plain; charset=utf-8');
}

export async function getTelegramFile(env, fileId) {
  return telegram(env, 'getFile', { file_id: fileId });
}

export async function downloadTelegramFile(env, filePath) {
  const response = await fetch(`https://api.telegram.org/file/bot${env.TELEGRAM_BOT_TOKEN}/${filePath}`);
  if (!response.ok) throw new Error('Telegram could not provide the uploaded file.');
  return response.text();
}

export async function downloadTelegramFileBytes(env, filePath, maximumBytes) {
  const response = await fetch(`https://api.telegram.org/file/bot${env.TELEGRAM_BOT_TOKEN}/${filePath}`);
  if (!response.ok) throw new Error('Telegram could not provide the uploaded file.');
  const headerLength = Number(response.headers.get('content-length') || '0');
  if (headerLength && headerLength > maximumBytes) throw new Error('The uploaded torrent exceeds the 1 MB limit.');
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (!bytes.length || bytes.length > maximumBytes) throw new Error('The uploaded torrent must be non-empty and no larger than 1 MB.');
  return bytes;
}

export function buttons(rows) {
  return { inline_keyboard: rows };
}
