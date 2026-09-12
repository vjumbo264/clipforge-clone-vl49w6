/**
 * Bot A view layer — the single self-editing status message
 * (ARCHITECTURE.md §8.1 principle 1) plus the §8.3 home screen and §8.6
 * settings screen composers.
 *
 * kv-minimization phase 5: the activeViewId that used to be persisted in
 * the per-chat KV state record is gone. The edit target is now IMPLICIT in
 * the update: button-press renders edit callback.message.message_id (the
 * caller passes it in), and renders answering a user-sent message always
 * start a fresh message (bug-01 behavior, unchanged). No navigation state
 * is written to any datastore.
 */

import { buttons, editMessage, sendMessage } from './telegram.js';
import { DEFAULT_VOICE, VOICES, escapeHtml } from './constants.js';

/**
 * bug-23: canonical end-to-end user guide (Telegraph). Linked from /help
 * and the onboarding screen.
 */
export const HELP_GUIDE_URL = 'https://telegra.ph/ClipForge-Bot--Setup--User-Guide-08-27';

export function callbackMessageId(callback) {
  const value = callback && callback.message && callback.message.message_id;
  return Number.isInteger(Number(value)) && Number(value) > 0 ? Number(value) : null;
}

/**
 * Render a view into the chat's single active message: edit when we have one,
 * send a fresh one otherwise. Ported verbatim from the legacy bot.
 */
export async function renderInteractiveView(env, chatId, text, options = {}, messageId = null) {
  const targetId = Number(messageId || 0);
  if (targetId > 0) {
    try {
      await editMessage(env, chatId, targetId, text, options);
      return { message_id: targetId, edited: true };
    } catch (error) {
      if (/message is not modified/i.test(String(error && error.message || ''))) {
        return { message_id: targetId, edited: true };
      }
    }
  }
  return sendMessage(env, chatId, text, options);
}

/**
 * Used right after the human supplied free text or a document: their message
 * sits between the old menu and the next screen, so the next screen starts a
 * fresh message instead of editing a stale one. Preserved from legacy.
 */
export async function renderFreshView(env, chatId, text, options = {}) {
  return renderInteractiveView(env, chatId, text, options, null);
}

export function onboardingKeyboard() {
  return buttons([
    [{ text: 'Create private Shadow Clone', callback_data: 'clone:new' }],
    [{ text: 'Connect existing clone', callback_data: 'clone:connect' }],
    [{ text: '🎬 Help tutorial (video)', callback_data: 'menu:helpvideo' }],
    [{ text: '📖 How ClipForge works (guide)', url: HELP_GUIDE_URL }]
  ]);
}

export function homeKeyboard() {
  return buttons([
    [{ text: '🎬 New video', callback_data: 'menu:new' }],
    [{ text: '📋 Tasks', callback_data: 'menu:tasks' }, { text: '✅ Completed', callback_data: 'menu:done' }],
    [{ text: '⚙️ Settings', callback_data: 'menu:settings' }],
    // bug-67: video help tutorial, available to every user (main or clone).
    [{ text: '🎬 Help tutorial (video)', callback_data: 'menu:helpvideo' }]
  ]);
}

/**
 * §8.3 home screen. `snapshot` is { repo, narratorVoice, seriesEnabled,
 * zernioEnabled }. (bug-30: Gemini removed.)
 */
export function homeText(snapshot) {
  const voice = VOICES[snapshot.narratorVoice] ? snapshot.narratorVoice : DEFAULT_VOICE;
  const lines = [
    '<b>ClipForge</b>',
    `Connected to: <code>${escapeHtml(snapshot.repo)}</code>   ·   Narrator: ${escapeHtml(VOICES[voice].label)}   ·   Series: ${snapshot.seriesEnabled ? 'on' : 'off'}`,
    `Zernio: ${snapshot.zernioEnabled ? 'on' : 'off'}`
  ];
  return lines.join('\n');
}

// bug-23: onboarding points at the full Telegraph guide.
export const ONBOARDING_TEXT = '<b>ClipForge</b>\n\nThis shared bot turns a source video into a short, narrated, captioned vertical clip. Your private chat operates only the GitHub clone connected to it.\n\nCreate your own private Shadow Clone, or connect a clone you already have.';

export function helpKeyboard() {
  // bug-29: the guide is text-only (ASCII diagrams, no screenshots).
  return buttons([
    [{ text: '🎬 Help tutorial (video)', callback_data: 'menu:helpvideo' }],
    [{ text: '📖 Full user guide', url: HELP_GUIDE_URL }]
  ]);
}

export const HELP_TEXT = [
  '<b>ClipForge commands</b>',
  '',
  '/new — start the new-video wizard (manual or series mode)',
  '/tasks — list active tasks; finished and errored tasks stay visible here',
  '/done — list completed tasks',
  '/settings — clone settings (GitHub clone, narrator voice, watermark, music, Zernio)',
  '/cancel — cancel the current setup or input flow',
  '/start, /help — this screen / the home menu',
  '',
  'Inside /new you can send: a direct https:// video URL, a Google Drive share link,',
  'a magnet URI, a .torrent file (≤ 1 MB), a public t.me channel-post link,',
  'or forward/upload the video itself.',
  '',
  'Removed legacy commands: /status is folded into /tasks.'
].join('\n');

export function settingsKeyboard() {
  return buttons([
    [{ text: 'GitHub clone', callback_data: 'set:github' }, { text: 'Narrator', callback_data: 'set:voice' }],
    [{ text: 'Music library', callback_data: 'set:music' }, { text: 'Watermark', callback_data: 'set:watermark' }],
    [{ text: 'Series Mode', callback_data: 'set:series' }, { text: 'Zernio publishing', callback_data: 'set:zernio' }],
    [{ text: 'Back to menu', callback_data: 'menu:home' }]
  ]);
}

/**
 * §8.6 settings summary lines. `snapshot` is { repo, narratorVoice,
 * seriesEnabled, watermarkName, musicDefaultPath, zernioEnabled }. (bug-30: Gemini removed.)
 * bug-49: when the clone's visibility is known (`snapshot.repoPrivate` boolean),
 * the GitHub line reports public/private at a glance so the visibility toggle's
 * current state is visible without opening the sub-screen.
 */
export function settingsText(snapshot) {
  const voice = VOICES[snapshot.narratorVoice] ? snapshot.narratorVoice : DEFAULT_VOICE;
  const visibility = snapshot.repo && typeof snapshot.repoPrivate === 'boolean'
    ? ` (${snapshot.repoPrivate ? 'private' : '🌐 public'})`
    : '';
  return [
    '<b>Settings</b>',
    `GitHub clone: ${snapshot.repo ? `<code>${escapeHtml(snapshot.repo)}</code>${visibility}` : 'not connected'}`,
    `Narrator: ${escapeHtml(VOICES[voice].label)} (Edge TTS)`,
    `Music default: ${snapshot.musicDefaultPath ? `<code>${escapeHtml(String(snapshot.musicDefaultPath).replace(/^audio-library\//, ''))}</code>` : 'not set'}`,
    `Watermark: ${snapshot.watermarkName ? escapeHtml(snapshot.watermarkName) : 'not set'}`,
    `Series Mode default: ${snapshot.seriesEnabled ? 'on' : 'off'}`,
    `Zernio publishing: ${snapshot.zernioEnabled ? 'on' : 'off'}`
  ].join('\n');
}
