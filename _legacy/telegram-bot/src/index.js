import {
  COMMANDS, DEFAULT_VOICE, GEMINI_KEYS_META_PATH, MUSIC_DEFAULT_PATH, PRODUCTION_PATH, STAGE_LABELS, TARGET_DURATIONS, TTS_SETTINGS_PATH, WATERMARK_PATH,
  VOICES, WHISPER_MODELS, ZERNIO_SECRET_NAME, ZERNIO_WORKFLOW, isTerminalStage, stageLabel
} from './constants.js';
import { encryptRelayPayload, maskSecret } from './crypto.js';
import {
  GitHubError, actionsSecretExists, cancelWorkflowRun, clearMusicDefaultIfTrack, createPrivateShadowClone, currentBranchSha, deleteAudioLibraryTrack, deleteClipforgeJob, deleteZernioSecret, dispatchWorkflow, geminiFingerprint, getJsonFile,
  getRepositoryFileBytes, listAudioLibrary, listJobIds, readGeminiMetadata, readProductionPlan, readSeriesSettings, readStageARequest, readStatus, readZernioAccounts, readZernioSettings, saveAutomaticMusicChoice,
  putBinaryFile, saveMusicDefault, saveNarrator, saveProductionPlan, saveSeriesSettings, saveStageARequest, saveWatermark, saveZernioSettings, tryGetJsonFile, updateGeminiSecret, updateZernioSecret, validateConnection,
  writeGeminiMetadata, zernioFingerprint
} from './github.js';
import { validateProductionPlan } from './production.js';
import {
  clearFlow, ensureTaskLabel, getCredentials, getJobIdForLabel, getState, getTaskOptions, markUpdateSeen,
  putCredentials, putRelayJob, putState, removeTask, setTaskOptions, taskLabels
} from './storage.js';
import { answerCallback, buttons, copyMessage, deleteMessage, downloadTelegramFile, downloadTelegramFileBytes, editMessage, getTelegramFile, sendAudioBytes, sendDocumentBytes, sendMessage, TelegramError } from './telegram.js';
import { RELAY_SOURCE_TYPE, relayCaption, relayReadyMarker, relayVideoMetadata } from './relay.js';

const SOURCE_RE = /^(https?:\/\/|magnet:\?)/i;
const SAFE_JOB_RE = /^[A-Za-z0-9._-]+$/;
const MAX_TASKS = 12;
const MAX_TORRENT_BYTES = 1024 * 1024;
const MAX_LIBRARY_TRACK_BYTES = 8 * 1024 * 1024;
const MUSIC_LIBRARY_TRACKS_PER_PAGE = 6;
const SAFE_AUDIO_FILENAME_RE = /^[^/\\\u0000-\u001f\u007f]+\.(?:mp3|m4a|aac|wav|ogg|opus|flac)$/i;
const TELEGRAM_PUBLIC_POST_RE = /^https?:\/\/(?:t\.me|telegram\.me)\/(?:s\/)?[A-Za-z0-9_]{5,64}\/[1-9][0-9]*(?:[/?#]|$)/i;
const DISABLED_SOCIAL_SOURCE_HOSTS = ['youtube-nocookie.com', 'youtu.be', 'youtube.com', 'vm.tiktok.com', 'vt.tiktok.com', 'tiktok.com', 'fb.watch', 'facebook.com', 'instagram.com', 'twitter.com', 'x.com', 'vimeo.com', 'redd.it', 'reddit.com'];
const TORRENT_CANDIDATES_PER_PAGE = 8;

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}

function normalizedButtonText(value, fallback = 'Unnamed track') {
  return String(value ?? '').replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim() || fallback;
}

function telegramButtonText(value, maximumBytes = 60) {
  const text = normalizedButtonText(value);
  const encoder = new TextEncoder();
  if (encoder.encode(text).length <= maximumBytes) return text;
  const suffix = '…';
  let output = '';
  for (const character of text) {
    if (encoder.encode(output + character + suffix).length > maximumBytes) break;
    output += character;
  }
  return `${output || 'Track'}${suffix}`;
}

function torrentCandidateButtonText(value, maximumBytes = 48) {
  const text = normalizedButtonText(value, 'Unnamed video');
  const encoder = new TextEncoder();
  if (encoder.encode(text).length <= maximumBytes) return text;
  const suffix = '…';
  const contentBudget = maximumBytes - encoder.encode(suffix).length;
  const prefixBudget = Math.floor(contentBudget / 2);
  const suffixBudget = contentBudget - prefixBudget;
  let prefix = '';
  for (const character of text) {
    if (encoder.encode(prefix + character).length > prefixBudget) break;
    prefix += character;
  }
  let ending = '';
  for (const character of Array.from(text).reverse()) {
    if (encoder.encode(character + ending).length > suffixBudget) break;
    ending = character + ending;
  }
  return `${prefix || 'Video'}${suffix}${ending}`;
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return 'Unknown size';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}

function redact(value) {
  return String(value || '')
    .replace(/(?:ghp|github_pat)_[A-Za-z0-9_]{12,}/g, '[redacted]')
    .replace(/AIza[A-Za-z0-9_-]{12,}/g, '[redacted]')
    .replace(/AQ\.[A-Za-z0-9._-]{12,}/g, '[redacted]');
}

function userError(error) {
  const raw = error && error.message ? error.message : 'The request could not be completed.';
  if (error instanceof GitHubError && error.status === 401) return 'GitHub rejected the stored token. Reconnect it from /settings.';
  if (error instanceof GitHubError && error.status === 403) return 'GitHub denied this operation. Confirm the token has repository contents, Actions, and workflow permissions.';
  if (error instanceof GitHubError && error.status === 404) return 'GitHub could not find that repository, task, or workflow.';
  return redact(raw).slice(0, 900);
}

function mainMenu() {
  return buttons([
    [{ text: 'Automatic', callback_data: 'menu:auto' }, { text: 'Manual', callback_data: 'menu:manual' }],
    [{ text: 'Tasks', callback_data: 'menu:tasks' }],
    [{ text: 'Completed', callback_data: 'menu:done' }, { text: 'Settings', callback_data: 'menu:settings' }]
  ]);
}

function taskSetupFlowAfterSource(pending) {
  if (!pending || !['manual', 'automatic'].includes(pending.mode)) return null;
  return `${pending.mode}_${pending.seriesMode === true ? 'duration' : 'focus'}`;
}

function hasResumablePendingTask(state) {
  const pending = state && state.pending;
  return Boolean(pending && pending.mode && pending.source && pending.jobId && [
    'manual_focus', 'automatic_focus', 'manual_duration', 'automatic_duration', 'manual_music', 'automatic_music'
  ].includes(state.flow));
}

function homeMenu(state) {
  const rows = [...mainMenu().inline_keyboard];
  if (hasResumablePendingTask(state)) rows.push([{ text: `Resume pending ${state.pending.mode} task`, callback_data: 'resume:task' }]);
  return buttons(rows);
}

function settingsMenu() {
  return buttons([
    [{ text: 'GitHub clone', callback_data: 'set:github' }, { text: 'Gemini API key', callback_data: 'set:gemini' }],
    [{ text: 'Narrator', callback_data: 'set:voice' }, { text: 'Music library', callback_data: 'set:music_library' }],
    [{ text: 'Watermark', callback_data: 'set:watermark' }, { text: 'Zernio publishing', callback_data: 'set:zernio' }],
    [{ text: 'Series Mode', callback_data: 'set:series' }, { text: 'Back to menu', callback_data: 'menu:home' }]
  ]);
}

function defaultZernioSettings() {
  return {
    version: 1,
    enabled: false,
    auto_publish: false,
    automatic_mode: 'smart_schedule',
    target_accounts: { tiktok: [], youtube: [] },
    smart_schedule: { timezone: 'UTC', interval_hours: 24, preferred_time: '19:30', queue_depth: 4, start_mode: 'next_available', custom_start: '' }
  };
}

function zernioIntervalHours(smart) {
  const explicit = Number(smart && smart.interval_hours);
  if (Number.isInteger(explicit) && explicit >= 1 && explicit <= 8760) return explicit;
  // Preserve legacy clone settings: interval_days: 2 remains 48 hours, not 2.
  const legacyDays = Number(smart && smart.interval_days);
  if (Number.isInteger(legacyDays) && legacyDays >= 1 && legacyDays <= 365) return legacyDays * 24;
  return 24;
}

function zernioSettingsOrDefault(value) {
  const current = value && typeof value === 'object' ? value : {};
  const smart = current.smart_schedule && typeof current.smart_schedule === 'object' ? current.smart_schedule : {};
  return {
    version: 1,
    enabled: current.enabled === true,
    auto_publish: current.auto_publish === true,
    automatic_mode: current.automatic_mode === 'publish_now' ? 'publish_now' : 'smart_schedule',
    target_accounts: {
      tiktok: Array.isArray(current.target_accounts && current.target_accounts.tiktok) ? current.target_accounts.tiktok.map(String) : [],
      youtube: Array.isArray(current.target_accounts && current.target_accounts.youtube) ? current.target_accounts.youtube.map(String) : []
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

function validZernioTimezone(value) {
  return /^(?:UTC|[A-Za-z_]+(?:\/[A-Za-z_+\-]+)+)$/.test(String(value || '').trim());
}

function validZernioTime(value) {
  return /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(String(value || '').trim());
}

function validZernioDateTime(value) {
  return /^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$/.test(String(value || '').trim());
}

function activeZernioAccounts(accounts) {
  const out = { tiktok: [], youtube: [] };
  for (const account of Array.isArray(accounts) ? accounts : []) {
    const platform = String(account && account.platform || '').toLowerCase();
    const id = String(account && (account.id || account._id) || '').trim();
    if (!out[platform] || !id || account.isActive === false || account.enabled === false || account.needsReconnection === true) continue;
    out[platform].push({ id, platform, username: String(account.username || ''), displayName: String(account.displayName || '') });
  }
  return out;
}

function zernioTargets(settings, accounts) {
  const active = activeZernioAccounts(accounts);
  return ['tiktok', 'youtube'].flatMap((platform) => {
    const selected = new Set(settings.target_accounts[platform] || []);
    const accountIds = active[platform].filter((account) => selected.has(account.id)).map((account) => account.id);
    return accountIds.length ? [{ platform, account_ids: accountIds }] : [];
  });
}

function cloneOnboardingMenu() {
  return buttons([
    [{ text: 'Create private Shadow Clone', callback_data: 'clone:new' }],
    [{ text: 'Connect existing clone', callback_data: 'clone:connect' }],
    [{ text: 'Cancel', callback_data: 'flow:cancel' }]
  ]);
}

function musicMenu() {
  return buttons([
    [{ text: 'No music', callback_data: 'music:none' }, { text: 'Use saved default', callback_data: 'music:default' }],
    [{ text: 'Choose library track', callback_data: 'music:library' }],
    [{ text: 'Back', callback_data: 'setup:back' }, { text: 'Cancel', callback_data: 'flow:cancel' }]
  ]);
}

function isSafeAudioLibraryPath(path) {
  return /^audio-library\/[^/\\\u0000-\u001f\u007f]+$/.test(String(path || ''));
}

async function readMusicDefaultPath(credentials) {
  try {
    const { document } = await getJsonFile(credentials, credentials.repo, MUSIC_DEFAULT_PATH);
    const path = document && document.library_track_path;
    return isSafeAudioLibraryPath(path) ? path : null;
  } catch (error) {
    if (error instanceof GitHubError && error.status === 404) return null;
    throw error;
  }
}

async function resolveMusicTrackByIndex(credentials, indexValue) {
  const index = Number(indexValue);
  if (!Number.isInteger(index) || index < 0) throw new Error('That music-library selection is invalid.');
  const tracks = await listAudioLibrary(credentials, credentials.repo);
  const track = tracks[index];
  if (!track || !isSafeAudioLibraryPath(track.path)) throw new Error('That music-library track is no longer available. Refresh the library and try again.');
  return track;
}

async function resolveMusicTrackBySha(credentials, sha) {
  if (!/^[A-Fa-f0-9]{40}$/.test(String(sha || ''))) throw new Error('That music-library deletion request is invalid.');
  const tracks = await listAudioLibrary(credentials, credentials.repo);
  const track = tracks.find((entry) => entry.sha === sha);
  if (!track || !isSafeAudioLibraryPath(track.path)) throw new Error('That music-library track is no longer available. Refresh the library and try again.');
  return track;
}

async function showMusicLibrary(env, chatId, pageValue = '0', messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  const [tracks, defaultPath] = await Promise.all([
    listAudioLibrary(credentials, credentials.repo),
    readMusicDefaultPath(credentials)
  ]);
  const requested = Number(pageValue);
  const totalPages = Math.max(1, Math.ceil(tracks.length / MUSIC_LIBRARY_TRACKS_PER_PAGE));
  const page = Number.isInteger(requested) ? Math.min(Math.max(requested, 0), totalPages - 1) : 0;
  const lines = ['<b>Music library</b>'];
  lines.push(`Tracks in this Shadow Clone: <b>${tracks.length}</b>`);
  lines.push(defaultPath ? `Saved default: <code>${escapeHtml(defaultPath.replace(/^audio-library\//, ''))}</code>` : 'Saved default: not set');
  lines.push('Preview, set the default, or delete library tracks below. Uploads are clone-scoped; one-off task music is never added here.');
  const rows = [];
  for (let index = page * MUSIC_LIBRARY_TRACKS_PER_PAGE; index < Math.min(tracks.length, (page + 1) * MUSIC_LIBRARY_TRACKS_PER_PAGE); index += 1) {
    const track = tracks[index];
    const defaultMark = track.path === defaultPath ? ' ✓ default' : '';
    rows.push([{ text: `▶ ${telegramButtonText(`${track.name} · ${formatBytes(track.size)}${defaultMark}`, 58)}`, callback_data: `ml:preview:${index}` }]);
    rows.push([
      { text: track.path === defaultPath ? 'Default ✓' : 'Set default', callback_data: `ml:default:${index}` },
      { text: 'Delete', callback_data: `ml:delete:${track.sha}` }
    ]);
  }
  if (!tracks.length) rows.push([{ text: 'No tracks yet — add one', callback_data: 'ml:upload' }]);
  if (totalPages > 1) {
    const navigation = [];
    if (page > 0) navigation.push({ text: 'Previous', callback_data: `ml:page:${page - 1}` });
    navigation.push({ text: `Page ${page + 1}/${totalPages}`, callback_data: `ml:page:${page}` });
    if (page < totalPages - 1) navigation.push({ text: 'Next', callback_data: `ml:page:${page + 1}` });
    rows.push(navigation);
  }
  rows.push([{ text: 'Upload track', callback_data: 'ml:upload' }, { text: 'Refresh library', callback_data: `ml:page:${page}` }]);
  rows.push([{ text: 'Back to settings', callback_data: 'menu:settings' }]);
  return renderInteractiveView(env, chatId, lines.join('\n'), { replyMarkup: buttons(rows) }, messageId);
}

async function previewMusicLibraryTrack(env, chatId, indexValue) {
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  const track = await resolveMusicTrackByIndex(credentials, indexValue);
  const bytes = await getRepositoryFileBytes(credentials, credentials.repo, track.path, MAX_LIBRARY_TRACK_BYTES);
  await sendAudioBytes(env, chatId, bytes, track.name, `<b>${escapeHtml(track.name)}</b>\n${escapeHtml(formatBytes(track.size))} · preview from this Shadow Clone music library`);
}

async function setMusicLibraryDefault(env, chatId, indexValue, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  const track = await resolveMusicTrackByIndex(credentials, indexValue);
  await saveMusicDefault(credentials, credentials.repo, track.path);
  return showMusicLibrary(env, chatId, String(Math.floor(Number(indexValue) / MUSIC_LIBRARY_TRACKS_PER_PAGE)), messageId);
}

async function promptMusicLibraryDelete(env, chatId, sha, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  const track = await resolveMusicTrackBySha(credentials, sha);
  return renderInteractiveView(env, chatId, `<b>Delete music track?</b>\n\n<code>${escapeHtml(track.name)}</code>\n${escapeHtml(formatBytes(track.size))}\n\nThis permanently removes the track from this Shadow Clone. If it is the saved default, the default will be cleared. Existing job-local music files are not touched.`, { replyMarkup: buttons([[{ text: 'Delete track', callback_data: `ml:deleteconfirm:${track.sha}` }, { text: 'Keep track', callback_data: 'set:music_library' }]]) }, messageId);
}

async function deleteMusicLibraryTrack(env, chatId, sha, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  const track = await resolveMusicTrackBySha(credentials, sha);
  await deleteAudioLibraryTrack(credentials, credentials.repo, track.path);
  await clearMusicDefaultIfTrack(credentials, credentials.repo, track.path);
  return showMusicLibrary(env, chatId, '0', messageId);
}

async function promptMusicLibraryUpload(env, chatId, messageId = null) {
  if (!await requireCredentials(env, chatId)) return;
  const state = await getState(env, chatId);
  state.flow = 'settings_music_upload';
  state.pending = {};
  await putState(env, chatId, state);
  return renderInteractiveView(env, chatId, `Send one audio document for this Shadow Clone music library. Allowed formats: <code>.mp3</code>, <code>.m4a</code>, <code>.aac</code>, <code>.wav</code>, <code>.ogg</code>, <code>.opus</code>, or <code>.flac</code>. Maximum size: ${formatBytes(MAX_LIBRARY_TRACK_BYTES)}.`, { replyMarkup: buttons([[{ text: 'Cancel', callback_data: 'flow:cancel' }]]) }, messageId);
}

async function handleMusicLibraryUpload(env, chatId, document) {
  const filename = String(document && document.file_name || '').trim();
  const size = Number(document && document.file_size || 0);
  if (!SAFE_AUDIO_FILENAME_RE.test(filename)) throw new Error('Upload an audio file with an allowed audio extension and a safe filename.');
  if (!Number.isFinite(size) || size <= 0 || size > MAX_LIBRARY_TRACK_BYTES) throw new Error(`Music-library uploads must be non-empty and no larger than ${formatBytes(MAX_LIBRARY_TRACK_BYTES)}.`);
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  const file = await getTelegramFile(env, document.file_id);
  if (!file || !file.file_path) throw new Error('Telegram did not return the uploaded audio file.');
  const bytes = await downloadTelegramFileBytes(env, file.file_path, MAX_LIBRARY_TRACK_BYTES);
  await putBinaryFile(credentials, credentials.repo, `audio-library/${filename}`, bytes, `clipforge: add ${filename} to audio library`);
  await clearFlow(env, chatId);
  await renderTaskInputResponse(env, chatId, `<b>Music track added.</b>\n<code>${escapeHtml(filename)}</code>\n${escapeHtml(formatBytes(bytes.length))}\n\nYou can preview it, set it as the default, or delete it from the library.`, { replyMarkup: buttons([[{ text: 'Open music library', callback_data: 'set:music_library' }, { text: 'Back to settings', callback_data: 'menu:settings' }]]) });
}

function durationMenu() {
  return buttons([
    TARGET_DURATIONS.slice(0, 3).map((seconds) => ({ text: seconds === 60 ? '1 min' : `${seconds}s`, callback_data: `dur:${seconds}` })),
    TARGET_DURATIONS.slice(3).map((seconds) => ({ text: seconds === 180 ? '3 min' : '5 min', callback_data: `dur:${seconds}` })),
    [{ text: 'Back', callback_data: 'setup:back' }, { text: 'Cancel', callback_data: 'flow:cancel' }]
  ]);
}

function commandOf(text) {
  const first = String(text || '').trim().split(/\s+/)[0].toLowerCase().replace(/@[^\s]+$/, '');
  return COMMANDS.has(first) ? first : null;
}

async function requireCredentials(env, chatId) {
  const credentials = await getCredentials(env, chatId);
  if (!credentials || !credentials.githubPat || !credentials.repo) {
    await renderInteractiveView(env, chatId, 'Set up your private GitHub clone first. Send <b>/start</b> and choose <b>Create private Shadow Clone</b> or <b>Connect existing clone</b>.', { replyMarkup: cloneOnboardingMenu() });
    return null;
  }
  return credentials;
}

async function renderTaskInputResponse(env, chatId, text, options = {}) {
  const state = await getState(env, chatId);
  // A human just supplied text or a document. Preserve the prior menu and
  // deliberately start a fresh control message for the next task-setup step.
  state.activeViewId = null;
  await putState(env, chatId, state);
  return renderInteractiveView(env, chatId, text, options, null);
}

async function renderInteractiveView(env, chatId, text, options = {}, messageId = null) {
  const state = await getState(env, chatId);
  const targetId = Number(messageId || state.activeViewId || 0);
  if (targetId > 0) {
    try {
      await editMessage(env, chatId, targetId, text, options);
      state.activeViewId = targetId;
      await putState(env, chatId, state);
      return { message_id: targetId, edited: true };
    } catch (error) {
      if (/message is not modified/i.test(String(error && error.message || ''))) return { message_id: targetId, edited: true };
    }
  }
  const sent = await sendMessage(env, chatId, text, options);
  state.activeViewId = Number(sent && sent.message_id) || null;
  await putState(env, chatId, state);
  return sent;
}

function callbackMessageId(callback) {
  const value = callback && callback.message && callback.message.message_id;
  return Number.isInteger(Number(value)) && Number(value) > 0 ? Number(value) : null;
}

async function showHome(env, chatId, messageId = null) {
  const credentials = await getCredentials(env, chatId);
  const configured = credentials && credentials.githubPat && credentials.repo;
  const existing = await readExistingSettings(credentials);
  const gemini = credentials && credentials.geminiKeys && credentials.geminiKeys.length;
  if (!configured) {
    await renderInteractiveView(env, chatId,
      '<b>ClipForge Telegram operator</b>\n\nThis is one shared bot. Your private Telegram chat operates only the GitHub clone connected to your chat. Create your own private Shadow Clone, or connect a clone you already have.',
      { replyMarkup: cloneOnboardingMenu() }, messageId);
    return;
  }
  const state = await getState(env, chatId);
  const pendingNotice = hasResumablePendingTask(state)
    ? `\n\n<b>Pending task setup:</b> the source is staged but Stage A has not been dispatched. Resume it to choose focus, duration, and music.`
    : '';
  await renderInteractiveView(env, chatId,
    `<b>ClipForge Telegram operator</b>\n\nConnected to <code>${escapeHtml(credentials.repo)}</code>. This shared bot keeps tasks and credentials isolated to this chat.\n${existingGeminiLabel(gemini ? credentials.geminiKeys : [], existing.geminiMeta)}.${pendingNotice}`,
    { replyMarkup: homeMenu(state) }, messageId);
}

async function readExistingSettings(credentials) {
  if (!credentials || !credentials.repo) return { geminiMeta: [], narrator: null, watermark: null, musicDefault: null, series: null };
  const safeJson = async (path) => {
    try { return await tryGetJsonFile(credentials, credentials.repo, path); }
    catch { return null; }
  };
  const [geminiMeta, narrator, watermark, musicDefault, series] = await Promise.all([
    readGeminiMetadata(credentials, credentials.repo).catch(() => []),
    safeJson(TTS_SETTINGS_PATH),
    safeJson(WATERMARK_PATH),
    safeJson(MUSIC_DEFAULT_PATH),
    readSeriesSettings(credentials, credentials.repo).catch(() => null)
  ]);
  return { geminiMeta, narrator: narrator && narrator.document, watermark: watermark && watermark.document, musicDefault: musicDefault && musicDefault.document, series };
}

function existingGeminiLabel(localKeys, metadata) {
  if (localKeys && localKeys.length) return `Gemini keys: ${localKeys.map(maskSecret).join(', ')} stored in this bot chat`;
  if (metadata && metadata.length) return `Gemini keys: ${metadata.length} existing site key${metadata.length === 1 ? '' : 's'} already configured in GitHub Actions`;
  return 'Gemini keys: not configured';
}

async function showSettings(env, chatId, messageId = null) {
  const credentials = await getCredentials(env, chatId);
  const existing = await readExistingSettings(credentials);
  const zernio = credentials && credentials.repo ? await loadZernioConfig(credentials).catch(() => null) : null;
  const lines = ['<b>Settings</b>'];
  lines.push(credentials && credentials.repo ? `GitHub clone: <code>${escapeHtml(credentials.repo)}</code>` : 'GitHub clone: not connected');
  lines.push(existingGeminiLabel(credentials && credentials.geminiKeys, existing.geminiMeta));
  const voice = existing.narrator && VOICES[existing.narrator.voice] ? existing.narrator.voice : DEFAULT_VOICE;
  lines.push(`Narrator: ${escapeHtml(VOICES[voice].label)} (Edge TTS)`);
  lines.push(`Watermark: ${existing.watermark && existing.watermark.creator_name ? escapeHtml(existing.watermark.creator_name) : 'not set'}`);
  lines.push(`Music default: ${existing.musicDefault && existing.musicDefault.library_track_path ? escapeHtml(String(existing.musicDefault.library_track_path).replace(/^audio-library\//, '')) : 'not set'}`);
  lines.push(`Series Mode: ${existing.series && existing.series.enabled === true ? 'on — new tasks chain parts sequentially' : 'off'}`);
  if (zernio) lines.push(`Zernio: ${zernio.secretConfigured ? zernio.settings.enabled ? zernio.settings.auto_publish ? `enabled · automatic ${zernio.settings.automatic_mode === 'publish_now' ? 'publish now' : 'smart schedule'}` : 'enabled · automatic off' : 'controls disabled' : 'API key not configured'}`);
  lines.push('Existing Gemini and Zernio secrets remain opaque: the bot can use them but will never display or copy raw values.');
  await renderInteractiveView(env, chatId, lines.join('\n'), { replyMarkup: settingsMenu() }, messageId);
}

async function loadZernioConfig(credentials) {
  const [rawSettings, accounts, secretConfigured] = await Promise.all([
    readZernioSettings(credentials, credentials.repo).catch(() => null),
    readZernioAccounts(credentials, credentials.repo).catch(() => []),
    actionsSecretExists(credentials, credentials.repo, ZERNIO_SECRET_NAME).catch(() => false)
  ]);
  return { settings: zernioSettingsOrDefault(rawSettings), accounts, secretConfigured };
}

async function persistZernioSettings(credentials, settings) {
  const document = { ...settings, version: 1, updated_at_epoch: Math.floor(Date.now() / 1000) };
  await saveZernioSettings(credentials, credentials.repo, document);
  return document;
}

function zernioSettingsMenu(config) {
  const settings = config.settings;
  return buttons([
    [{ text: config.secretConfigured ? 'Replace Zernio API key' : 'Save Zernio API key', callback_data: 'set:zernio_key' }, { text: 'Refresh accounts', callback_data: 'zernio:refresh' }],
    [{ text: settings.enabled ? 'Disable publishing controls' : 'Enable publishing controls', callback_data: 'zernio:toggle_enabled' }, { text: settings.auto_publish ? 'Turn automatic publish off' : 'Turn automatic publish on', callback_data: 'zernio:toggle_auto' }],
    [{ text: settings.automatic_mode === 'publish_now' ? 'Automatic: publish now' : 'Automatic: smart schedule', callback_data: 'zernio:mode' }, { text: 'Select target accounts', callback_data: 'zernio:targets' }],
    [{ text: 'Smart schedule', callback_data: 'zernio:schedule' }, { text: 'Remove API key', callback_data: 'zernio:clear_prompt' }],
    [{ text: 'Back to settings', callback_data: 'menu:settings' }]
  ]);
}

function zernioScheduleMenu(settings) {
  const smart = settings.smart_schedule;
  return buttons([
    [{ text: `Timezone: ${telegramButtonText(smart.timezone, 42)}`, callback_data: 'zsch:timezone' }, { text: `Cadence: ${smart.interval_hours} hour(s)`, callback_data: 'zsch:interval' }],
    [{ text: `Preferred time: ${smart.preferred_time}`, callback_data: 'zsch:time' }, { text: `Queue depth: ${smart.queue_depth}`, callback_data: 'zsch:depth' }],
    [{ text: smart.start_mode === 'custom' ? `Custom start: ${telegramButtonText(smart.custom_start || 'missing', 40)}` : 'Start: next available', callback_data: 'zsch:start' }],
    [{ text: 'Back to Zernio settings', callback_data: 'set:zernio' }]
  ]);
}

async function showZernioSettings(env, chatId, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  const config = await loadZernioConfig(credentials);
  const settings = config.settings;
  const active = activeZernioAccounts(config.accounts);
  const selected = zernioTargets(settings, config.accounts);
  const lines = ['<b>Zernio publishing</b>'];
  lines.push(`API key: ${config.secretConfigured ? 'secured in GitHub Actions (opaque)' : 'not configured'}`);
  lines.push(`Controls: ${settings.enabled ? 'enabled' : 'disabled'}`);
  lines.push(`Automatic publishing: ${settings.auto_publish ? settings.automatic_mode === 'publish_now' ? 'publish now' : 'smart schedule' : 'off'}`);
  lines.push(`Targets: ${selected.length ? selected.map((group) => `${group.platform}: ${group.account_ids.length}`).join(' · ') : 'none selected'}`);
  lines.push(`Accounts: ${active.tiktok.length} TikTok · ${active.youtube.length} YouTube active`);
  lines.push(`Smart schedule: ${escapeHtml(settings.smart_schedule.timezone)} · every ${settings.smart_schedule.interval_hours} hour(s) · preferred time ${settings.smart_schedule.preferred_time} · queue ${settings.smart_schedule.queue_depth}`);
  if (settings.smart_schedule.start_mode === 'custom') lines.push(`First slot: ${escapeHtml(settings.smart_schedule.custom_start || 'missing')}`);
  lines.push('The API key is encrypted into <code>ZERNIO_API_KEY</code>, never committed or displayed. Preferences and account snapshots stay inside this clone.');
  await renderInteractiveView(env, chatId, lines.join('\n'), { replyMarkup: zernioSettingsMenu(config) }, messageId);
}

async function showZernioTargets(env, chatId, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  const config = await loadZernioConfig(credentials);
  const settings = config.settings;
  const rows = [];
  for (const account of config.accounts) {
    const platform = String(account && account.platform || '').toLowerCase();
    const id = String(account && (account.id || account._id) || '').trim();
    if (!['tiktok', 'youtube'].includes(platform) || !id) continue;
    const usable = account.isActive !== false && account.enabled !== false && account.needsReconnection !== true;
    const selected = (settings.target_accounts[platform] || []).includes(id);
    const title = `${platform === 'tiktok' ? 'TikTok' : 'YouTube'} · ${account.displayName || account.username || id}`;
    rows.push([{ text: `${selected ? '✓ ' : ''}${telegramButtonText(title, 50)}${usable ? '' : ' (unavailable)'}`, callback_data: usable ? `ztarget:${platform}:${id}` : 'noop:account' }]);
  }
  if (!rows.length) rows.push([{ text: 'No saved accounts — refresh first', callback_data: 'zernio:refresh' }]);
  rows.push([{ text: 'Refresh accounts', callback_data: 'zernio:refresh' }, { text: 'Back to Zernio settings', callback_data: 'set:zernio' }]);
  await renderInteractiveView(env, chatId, '<b>Zernio target accounts</b>\n\nSelect active TikTok and YouTube accounts. Changes save immediately. Unavailable, disabled, or reconnect-required accounts cannot be selected.', { replyMarkup: buttons(rows) }, messageId);
}

async function updateZernioSetting(env, chatId, mutate) {
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return null;
  const config = await loadZernioConfig(credentials);
  const settings = config.settings;
  mutate(settings, config.accounts);
  const saved = await persistZernioSettings(credentials, settings);
  return { credentials, config: { ...config, settings: saved } };
}

async function refreshZernioAccounts(env, chatId, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  const configured = await actionsSecretExists(credentials, credentials.repo, ZERNIO_SECRET_NAME);
  if (!configured) throw new Error('Save a Zernio API key before refreshing accounts.');
  await dispatchWorkflow(credentials, credentials.repo, ZERNIO_WORKFLOW, { action: 'discover' });
  await renderInteractiveView(env, chatId, 'Account refresh was dispatched. Zernio will save the active TikTok and YouTube snapshot to this clone. Use Refresh accounts again after the workflow finishes.', { replyMarkup: buttons([[{ text: 'Refresh accounts', callback_data: 'zernio:refresh' }, { text: 'Back to Zernio settings', callback_data: 'set:zernio' }]]) }, messageId);
}

function normalizeFocus(value) {
  const normalized = String(value || '').replace(/\s+/g, ' ').trim();
  if (normalized === '-' || normalized.toLowerCase() === 'none') return '';
  if (normalized.length > 500) throw new Error('Editorial focus must be 500 characters or fewer.');
  return normalized;
}

function sourceHost(value) {
  try { return new URL(value).hostname.toLowerCase().replace(/\.$/, ''); }
  catch { return ''; }
}

function hostMatches(host, allowed) {
  return host === allowed || host.endsWith(`.${allowed}`);
}

function disabledSocialSourceHost(value) {
  const host = sourceHost(value);
  return DISABLED_SOCIAL_SOURCE_HOSTS.find((allowed) => hostMatches(host, allowed)) || null;
}

function isPublicTelegramPost(value) {
  return TELEGRAM_PUBLIC_POST_RE.test(String(value || ''));
}

function originalRepositoryForLegacyMtproto(env) {
  return String(env.ORIGINAL_CLIPFORGE_REPOSITORY || 'motionssalt/clipforge').trim().toLowerCase();
}

function permitsLegacyTelegramMtproto(env, credentials) {
  return Boolean(credentials && String(credentials.repo || '').trim().toLowerCase() === originalRepositoryForLegacyMtproto(env));
}

function telegramPageIsGroup(page) {
  return /view\s+in\s+group/i.test(String(page || ''));
}

async function preflightTelegramChannelPost(source) {
  if (!isPublicTelegramPost(source)) return;
  try {
    const response = await fetch(source, {
      headers: { Accept: 'text/html,application/xhtml+xml', 'User-Agent': 'ClipForge Telegram intake preflight' },
      redirect: 'follow',
      signal: AbortSignal.timeout(8_000),
    });
    if (!response.ok) return;
    if (telegramPageIsGroup(await response.text())) {
      throw new Error('This is a public Telegram group link. ClipForge downloads social video only from a public Telegram channel post. Create a public channel—not a group—forward or upload the video there, then send that channel post link.');
    }
  } catch (error) {
    if (/public Telegram group link/i.test(String(error && error.message || ''))) throw error;
    // A transient Telegram page check must not prevent a valid public channel
    // post from reaching the independently verified Stage A downloader.
  }
}

function validateSource(value) {
  const source = String(value || '').trim();
  if (!SOURCE_RE.test(source)) throw new Error('Send a public Telegram channel post link (not a group), a direct https:// video-file URL, an anyone-with-link Google Drive URL, or a magnet URI. To use a social video, first forward or upload it to a public Telegram channel—not a group—and send that post link.');
  if (source.length > 4000) throw new Error('The source URL is too long.');
  if (/^https?:\/\//i.test(source)) {
    const blockedHost = disabledSocialSourceHost(source);
    if (blockedHost) throw new Error(`${blockedHost} social links are disabled. Forward or upload the video to a public Telegram channel and send its public post link instead.`);
    const host = sourceHost(source);
    if ((host === 't.me' || host === 'telegram.me') && !isPublicTelegramPost(source)) throw new Error('Send a public Telegram channel post link in the form https://t.me/<channel>/<message_id>. Private and non-post Telegram links are not supported.');
  }
  return source;
}

function startTaskJobId(mode) {
  return `${mode === 'automatic' ? 'automatic' : 'manual'}-${Date.now()}`;
}

function formatStatus(label, jobId, status) {
  if (!status) return `<b>Task ${escapeHtml(label)}</b>\n<code>${escapeHtml(jobId)}</code>\nStatus has not been written yet. The workflow may still be starting.`;
  const lines = [`<b>Task ${escapeHtml(label)} — ${escapeHtml(stageLabel(status.stage))}</b>`, `<code>${escapeHtml(jobId)}</code>`];
  if (status.message) lines.push(escapeHtml(status.message));
  if (status.expires_at_epoch) lines.push(`Expires: ${escapeHtml(new Date(Number(status.expires_at_epoch) * 1000).toLocaleString())}`);
  const runUrl = status.extra && status.extra.workflow_run_url;
  if (runUrl && /^https:\/\//.test(runUrl)) lines.push(`<a href="${escapeHtml(runUrl)}">Open GitHub workflow</a>`);
  if (status.release_url && /^https:\/\//.test(status.release_url)) lines.push(`<a href="${escapeHtml(status.release_url)}">Open release</a>`);
  return lines.join('\n');
}

function taskCanBeDeleted(status) {
  return Boolean(status && ['error', 'cancelled', 'complete', 'awaiting_json_upload', 'awaiting_torrent_selection'].includes(status.stage));
}

async function readJobRequest(credentials, jobId) {
  try { return await readStageARequest(credentials, credentials.repo, jobId); } catch (error) { if (error instanceof GitHubError && error.status === 404) return null; throw error; }
}

// Manual Series Mode has no workflow-driven continuation: when a non-final
// MANUAL series part completes, the user needs a way to start the next part
// (automatic mode gets this from stage-b.yml + scripts/series_state.py).
// Returns the next part's coordinates, or null when the completed job is not
// a continuable manual series part.
async function manualSeriesContinuation(credentials, jobId, status) {
  if (!status || status.stage !== 'complete') return null;
  const request = await readJobRequest(credentials, jobId);
  if (!request || request.series_mode !== 'true' || request.automatic_mode === 'true') return null;
  const seriesId = String(request.series_id || '').trim();
  const part = Number(request.series_part || 0);
  if (!seriesId || !Number.isInteger(part) || part < 1) return null;
  const plan = await readProductionPlan(credentials, credentials.repo, jobId);
  if (!plan || plan.series_final === true) return null;
  const end = Number(plan.series_end_seconds);
  if (!Number.isInteger(end) || end < 0) return null;
  return { seriesId, part: part + 1, startSeconds: end };
}

function taskButtons(label, status, seriesContinuation = null) {
  const rows = [[{ text: 'Refresh', callback_data: `status:${label}` }]];
  if (!status) return buttons(rows);
  if (['error', 'cancelled'].includes(status.stage)) rows.push([{ text: 'Restart Stage A', callback_data: `retry:a:${label}` }]);
  if (status.stage === 'awaiting_torrent_selection') rows.push([{ text: 'Choose torrent video', callback_data: `torrent:${label}` }]);
  if (status.stage === 'awaiting_json_upload') rows.push([{ text: 'Get agent prompt', callback_data: `agent:${label}` }, { text: 'Upload production.json', callback_data: `plan:${label}` }]);
  if (['error', 'cancelled', 'complete'].includes(status.stage)) rows.push([{ text: 'Restart Stage B', callback_data: `retry:b:${label}` }]);
  if (['stage_b_queued', 'stage_b_running'].includes(status.stage)) rows.push([{ text: 'Cancel Stage B', callback_data: `cancel:${label}` }]);
  if (status.stage === 'complete') rows.push([{ text: 'Zernio publishing', callback_data: `zpub:menu:${label}` }]);
  if (seriesContinuation) rows.push([{ text: `Start next part (Part ${seriesContinuation.part})`, callback_data: `series:next:${label}` }]);
  if (taskCanBeDeleted(status)) rows.push([{ text: 'Delete task', callback_data: `task:delete:${label}` }]);
  return buttons(rows);
}

async function listTasks(env, chatId, completedOnly = false, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  const ids = await listJobIds(credentials, credentials.repo);
  if (!ids.length) { await renderInteractiveView(env, chatId, 'No ClipForge tasks exist in this clone yet.', { replyMarkup: mainMenu() }, messageId); return; }
  const entries = [];
  for (const jobId of ids) {
    const status = await readStatus(credentials, credentials.repo, jobId);
    let request = null;
    try { request = await readStageARequest(credentials, credentials.repo, jobId); } catch (error) { if (!(error instanceof GitHubError && error.status === 404)) throw error; }
    const dispatched = Boolean(status || request);
    if (!dispatched) continue; // A source.torrent alone is staged task input, not a dispatched task.
    if (completedOnly && (!status || status.stage !== 'complete')) continue;
    const label = await ensureTaskLabel(env, chatId, jobId);
    const seriesId = request && request.series_mode === 'true' ? String(request.series_id || '') : '';
    const seriesPart = request && request.series_mode === 'true' ? Number(request.series_part || 0) : 0;
    entries.push({ label, jobId, status, seriesId, seriesPart });
    if (entries.length >= MAX_TASKS) break;
  }
  if (!entries.length) { await renderInteractiveView(env, chatId, completedOnly ? 'No completed tasks were found.' : 'No dispatched tasks were found.', { replyMarkup: mainMenu() }, messageId); return; }
  const text = entries.map(({ label, jobId, status, seriesId, seriesPart }) => `${seriesId ? `<b>Series ${escapeHtml(seriesId)} · Part ${seriesPart || '?'}</b>\n` : ''}<b>${label}</b> · ${escapeHtml(stageLabel(status ? status.stage : 'starting'))}\n<code>${escapeHtml(jobId)}</code>`).join('\n\n');
  const rows = entries.map(({ label }) => [{ text: `Task ${label}`, callback_data: completedOnly ? `done:${label}` : `status:${label}` }]);
  await renderInteractiveView(env, chatId, `<b>${completedOnly ? 'Completed tasks' : 'Tasks'}</b>\n\n${text}`, { replyMarkup: buttons(rows) }, messageId);
}

async function showTaskStatus(env, chatId, label, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  const jobId = await getJobIdForLabel(env, chatId, label);
  if (!credentials || !jobId) { if (!jobId) await renderInteractiveView(env, chatId, 'That task label is no longer available. Use /tasks to refresh it.', { replyMarkup: mainMenu() }, messageId); return; }
  const status = await readStatus(credentials, credentials.repo, jobId);
  const seriesContinuation = await manualSeriesContinuation(credentials, jobId, status);
  const state = await getState(env, chatId);
  state.currentTask = jobId;
  await putState(env, chatId, state);
  await renderInteractiveView(env, chatId, formatStatus(label, jobId, status), { replyMarkup: taskButtons(label, status, seriesContinuation) }, messageId);
}

function zernioPostId(post) {
  return String(post && (post.id || post.post_id || post._id) || '').trim();
}

function zernioPublishingSummary(publishing) {
  const status = String(publishing && publishing.status || 'not_requested').toLowerCase();
  const label = status === 'published' ? 'published' : status === 'scheduled' ? 'scheduled' : ['publishing', 'requested'].includes(status) ? 'publishing' : status === 'partial' ? 'partial' : ['failed', 'error'].includes(status) ? 'failed' : status === 'cancelled' ? 'cancelled' : 'not requested';
  const posts = Array.isArray(publishing && publishing.posts) ? publishing.posts : [];
  return `Zernio: ${label}${posts.length ? ` · ${posts.length} post record(s)` : ''}`;
}

function zernioRequestId(jobId, publishing) {
  const prior = String(publishing && publishing.status || '').toLowerCase() === 'error' ? String(publishing.idempotency_key || '') : '';
  if (/^[A-Za-z0-9._:-]{8,200}$/.test(prior)) return prior;
  return `clipforge-${jobId}-${Date.now().toString(36)}`;
}

async function showZernioPublishMenu(env, chatId, label, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  const jobId = await getJobIdForLabel(env, chatId, label);
  if (!credentials || !jobId) return;
  const status = await readStatus(credentials, credentials.repo, jobId);
  if (!status || status.stage !== 'complete') throw new Error('Zernio publishing is available after Stage B completes.');
  const config = await loadZernioConfig(credentials);
  const targets = zernioTargets(config.settings, config.accounts);
  const publishing = status.publishing && typeof status.publishing === 'object' ? status.publishing : {};
  const lines = [`<b>Task ${escapeHtml(label)} — Zernio publishing</b>`, `<code>${escapeHtml(jobId)}</code>`, zernioPublishingSummary(publishing)];
  if (!config.secretConfigured) lines.push('Save a Zernio API key in settings before submitting a request.');
  else if (!config.settings.enabled) lines.push('Enable Zernio publishing controls in settings before submitting a request.');
  else if (!targets.length) lines.push('Select at least one active TikTok or YouTube target account in settings.');
  else lines.push(`Targets: ${targets.map((group) => `${group.platform} (${group.account_ids.length})`).join(' · ')}\nTimezone: ${escapeHtml(config.settings.smart_schedule.timezone)}`);
  const rows = [];
  if (config.secretConfigured && config.settings.enabled && targets.length) {
    rows.push([{ text: 'Publish now', callback_data: `zpub:now:${label}` }, { text: 'Smart schedule', callback_data: `zpub:smart:${label}` }]);
    rows.push([{ text: 'Choose date and time', callback_data: `zpub:manual:${label}` }]);
  }
  const posts = Array.isArray(publishing.posts) ? publishing.posts : [];
  for (const post of posts.slice(0, 6)) {
    const postId = zernioPostId(post);
    if (!/^[A-Za-z0-9._-]{3,200}$/.test(postId)) continue;
    const state = String(post.status || post.state || '').toLowerCase();
    const platform = String(post.platform || 'post');
    if (['failed', 'error', 'partial'].includes(state)) rows.push([{ text: `Retry ${telegramButtonText(platform, 20)}`, callback_data: `zpost:retry:${label}:${postId}` }]);
    if (['scheduled', 'requested', 'publishing', 'partial', 'failed', 'error'].includes(state)) {
      rows.push([{ text: `Publish ${telegramButtonText(platform, 16)} now`, callback_data: `zpost:now:${label}:${postId}` }, { text: `Reschedule ${telegramButtonText(platform, 16)}`, callback_data: `zpost:manual:${label}:${postId}` }]);
      rows.push([{ text: `Cancel ${telegramButtonText(platform, 20)}`, callback_data: `zpost:cancel:${label}:${postId}` }]);
    }
  }
  rows.push([{ text: 'Zernio settings', callback_data: 'set:zernio' }, { text: 'Back to completed task', callback_data: `done:${label}` }]);
  await renderInteractiveView(env, chatId, lines.join('\n'), { replyMarkup: buttons(rows) }, messageId);
}

async function dispatchZernioPublish(env, chatId, label, mode, scheduledFor = '', messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  const jobId = await getJobIdForLabel(env, chatId, label);
  if (!credentials || !jobId) return;
  const [status, config] = await Promise.all([readStatus(credentials, credentials.repo, jobId), loadZernioConfig(credentials)]);
  if (!status || status.stage !== 'complete') throw new Error('Zernio publishing is available after Stage B completes.');
  if (!config.secretConfigured) throw new Error('Save a Zernio API key in settings before publishing.');
  if (!config.settings.enabled) throw new Error('Enable Zernio publishing controls in settings before publishing.');
  const targets = zernioTargets(config.settings, config.accounts);
  if (!targets.length) throw new Error('Select at least one active TikTok or YouTube account in Zernio settings.');
  if (!['publish_now', 'smart_schedule', 'manual_schedule'].includes(mode)) throw new Error('That publishing mode is unavailable.');
  if (mode === 'manual_schedule' && !validZernioDateTime(scheduledFor)) throw new Error('Send a local time in YYYY-MM-DDTHH:MM format.');
  await dispatchWorkflow(credentials, credentials.repo, ZERNIO_WORKFLOW, {
    action: 'publish', job_id: jobId, mode, scheduled_for: scheduledFor, timezone: config.settings.smart_schedule.timezone,
    targets_json: JSON.stringify(targets), request_id: zernioRequestId(jobId, status.publishing)
  });
  await renderInteractiveView(env, chatId, `Zernio ${mode === 'publish_now' ? 'publish-now' : mode === 'smart_schedule' ? 'smart-schedule' : 'scheduled'} request dispatched for task <b>${escapeHtml(label)}</b>. Stage B remains complete while Zernio processes the request.`, { replyMarkup: buttons([[{ text: 'Refresh completed task', callback_data: `done:${label}` }]]) }, messageId);
}

async function dispatchZernioPostAction(env, chatId, label, postId, action, mode = '', scheduledFor = '', messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  const jobId = await getJobIdForLabel(env, chatId, label);
  if (!credentials || !jobId) return;
  if (!/^[A-Za-z0-9._-]{3,200}$/.test(postId) || !['retry', 'update', 'cancel'].includes(action)) throw new Error('That Zernio post action is invalid.');
  const config = await loadZernioConfig(credentials);
  if (!config.secretConfigured) throw new Error('Save a Zernio API key in settings before managing posts.');
  if (action === 'update' && mode === 'manual_schedule' && !validZernioDateTime(scheduledFor)) throw new Error('Send a local time in YYYY-MM-DDTHH:MM format.');
  await dispatchWorkflow(credentials, credentials.repo, ZERNIO_WORKFLOW, { action, job_id: jobId, post_id: postId, mode, scheduled_for: scheduledFor, timezone: config.settings.smart_schedule.timezone });
  await renderInteractiveView(env, chatId, `Zernio ${action} request dispatched for task <b>${escapeHtml(label)}</b>. Refresh the completed task after the workflow finishes.`, { replyMarkup: buttons([[{ text: 'Refresh completed task', callback_data: `done:${label}` }]]) }, messageId);
}

async function showCompleted(env, chatId, label, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  const jobId = await getJobIdForLabel(env, chatId, label);
  if (!credentials || !jobId) return;
  const status = await readStatus(credentials, credentials.repo, jobId);
  if (!status || status.stage !== 'complete') { await renderInteractiveView(env, chatId, 'That task is not complete yet.', { replyMarkup: mainMenu() }, messageId); return; }
  const assets = status.assets || {};
  const lines = [`<b>Task ${escapeHtml(label)} is complete.</b>`, `<code>${escapeHtml(jobId)}</code>`];
  if (assets.final_mp4) lines.push(`<a href="${escapeHtml(assets.final_mp4)}">Download final.mp4</a>`);
  if (assets.final_zip) lines.push(`<a href="${escapeHtml(assets.final_zip)}">Download final ZIP</a>`);
  if (!assets.final_mp4 && !assets.final_zip) lines.push('The completion status has no final asset URL. Open the release from /status.');
  if (status.publishing) lines.push(zernioPublishingSummary(status.publishing));
  const seriesContinuation = await manualSeriesContinuation(credentials, jobId, status);
  await renderInteractiveView(env, chatId, lines.join('\n'), { replyMarkup: taskButtons(label, status, seriesContinuation) }, messageId);
}

async function resumePendingTask(env, chatId, messageId = null) {
  const state = await getState(env, chatId);
  if (!hasResumablePendingTask(state)) { await renderInteractiveView(env, chatId, 'There is no staged task setup to resume.', { replyMarkup: mainMenu() }, messageId); return; }
  if (state.flow.endsWith('_focus')) {
    if (state.pending && state.pending.seriesMode === true) {
      delete state.pending.focus;
      state.flow = `${state.pending.mode}_duration`;
      await putState(env, chatId, state);
      return renderInteractiveView(env, chatId, '<b>Series Mode does not use editorial focus.</b> Choose the target output length for each part.', { replyMarkup: durationMenu() }, messageId);
    }
    return renderInteractiveView(env, chatId, 'Resume task setup: send an optional editorial focus, or send <code>-</code> to consider the whole video.', { replyMarkup: buttons([[{ text: 'Cancel', callback_data: 'flow:cancel' }]]) }, messageId);
  }
  if (state.flow.endsWith('_duration')) return renderInteractiveView(env, chatId, 'Resume task setup: choose the target output length.', { replyMarkup: durationMenu() }, messageId);
  return renderInteractiveView(env, chatId, 'Resume task setup: choose optional background music.', { replyMarkup: musicMenu() }, messageId);
}

async function beginTask(env, chatId, mode, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  if (mode === 'automatic' && !(credentials.geminiKeys || []).length) {
    const metadata = await readGeminiMetadata(credentials, credentials.repo).catch(() => []);
    if (!metadata.length) {
      await renderInteractiveView(env, chatId, 'Automatic Mode needs at least one Gemini API key. Add it from /settings before starting.', { replyMarkup: buttons([[{ text: 'Open settings', callback_data: 'menu:settings' }]]) }, messageId);
      return;
    }
  }
  const series = await readSeriesSettings(credentials, credentials.repo).catch(() => null);
  const state = await getState(env, chatId);
  state.flow = `${mode}_source`;
  state.pending = { mode, seriesMode: Boolean(series && series.enabled === true) };
  await putState(env, chatId, state);
  await renderInteractiveView(env, chatId, `<b>${mode === 'automatic' ? 'Automatic' : 'Manual'} task</b>\nSend or forward one video directly to ClipForge, send a public Telegram channel post link (not a group), a direct video-file URL, a Google Drive anyone-with-link URL, a magnet URI, or upload a non-empty <code>.torrent</code> document up to 1 MB. Send /cancel to stop.`, { replyMarkup: buttons([[{ text: 'Back to menu', callback_data: 'setup:back' }, { text: 'Cancel', callback_data: 'flow:cancel' }]]) }, messageId);
}

async function setupBack(env, chatId, messageId = null) {
  const state = await getState(env, chatId);
  const pending = state.pending || {};
  const mode = pending.mode;
  if (!mode || !state.flow || !state.flow.startsWith(`${mode}_`)) {
    await clearFlow(env, chatId);
    return renderInteractiveView(env, chatId, 'Task setup is no longer active.', { replyMarkup: mainMenu() }, messageId);
  }
  if (state.flow.endsWith('_music')) {
    delete pending.duration; delete pending.musicRef; delete pending.musicSource; delete pending.musicOptions;
    state.flow = `${mode}_duration`;
    state.pending = pending;
    await putState(env, chatId, state);
    return renderInteractiveView(env, chatId, 'Choose the target output length.', { replyMarkup: durationMenu() }, messageId);
  }
  if (state.flow.endsWith('_duration')) {
    delete pending.duration; delete pending.focus;
    if (pending.seriesMode === true) {
      delete pending.source; delete pending.jobId;
      state.flow = `${mode}_source`;
      state.pending = pending;
      await putState(env, chatId, state);
      return renderInteractiveView(env, chatId, `<b>${mode === 'automatic' ? 'Automatic' : 'Manual'} Series task</b>\nSend or forward one video directly to ClipForge, send a public Telegram channel post link (not a group), a direct video-file URL, a Google Drive anyone-with-link URL, a magnet URI, or upload a non-empty <code>.torrent</code> document up to 1 MB. Series Mode does not request editorial focus.`, { replyMarkup: buttons([[{ text: 'Back to menu', callback_data: 'setup:back' }, { text: 'Cancel', callback_data: 'flow:cancel' }]]) }, messageId);
    }
    state.flow = `${mode}_focus`;
    state.pending = pending;
    await putState(env, chatId, state);
    return renderInteractiveView(env, chatId, 'Send an optional editorial focus. Send <code>-</code> to consider the whole video.', { replyMarkup: buttons([[{ text: 'Back', callback_data: 'setup:back' }, { text: 'Cancel', callback_data: 'flow:cancel' }]]) }, messageId);
  }
  if (state.flow.endsWith('_focus')) {
    if (pending.seriesMode === true) {
      delete pending.focus;
      state.flow = `${mode}_duration`;
      state.pending = pending;
      await putState(env, chatId, state);
      return renderInteractiveView(env, chatId, '<b>Series Mode does not use editorial focus.</b> Choose the target output length for each part.', { replyMarkup: durationMenu() }, messageId);
    }
    delete pending.source; delete pending.jobId; delete pending.focus;
    state.flow = `${mode}_source`;
    state.pending = pending;
    await putState(env, chatId, state);
    return renderInteractiveView(env, chatId, `<b>${mode === 'automatic' ? 'Automatic' : 'Manual'} task</b>\nSend or forward one video directly to ClipForge, send a public Telegram channel post link (not a group), a direct video-file URL, a Google Drive anyone-with-link URL, a magnet URI, or upload a non-empty <code>.torrent</code> document up to 1 MB.`, { replyMarkup: buttons([[{ text: 'Back to menu', callback_data: 'setup:back' }, { text: 'Cancel', callback_data: 'flow:cancel' }]]) }, messageId);
  }
  await clearFlow(env, chatId);
  return renderInteractiveView(env, chatId, 'Task setup closed. Start a new task whenever you are ready.', { replyMarkup: mainMenu() }, messageId);
}

async function finishTaskLaunch(env, chatId, messageId = null) {
  const state = await getState(env, chatId);
  const pending = state.pending || {};
  const credentials = await requireCredentials(env, chatId);
  if (!credentials || !pending.mode || !pending.source || !TARGET_DURATIONS.includes(Number(pending.duration))) return;
  const jobId = pending.jobId || startTaskJobId(pending.mode);
  const isRelaySource = pending.source === RELAY_SOURCE_TYPE;
  const inputs = {
    video_url: isRelaySource ? 'relay:private' : pending.source,
    source_type: isRelaySource ? RELAY_SOURCE_TYPE : 'url',
    relay_release_tag: '',
    relay_expected_size: '',
    relay_sha256: '',
    torrent_file_index: '',
    job_id: jobId,
    whisper_model: 'base',
    language: 'auto',
    target_duration_seconds: String(pending.duration),
    focus: pending.seriesMode === true ? '' : (pending.focus || ''),
    automatic_mode: pending.mode === 'automatic' ? 'true' : 'false',
    series_mode: pending.seriesMode === true ? 'true' : '', series_id: pending.seriesMode === true ? jobId : '',
    series_source_job_id: pending.seriesMode === true ? jobId : '', series_part: pending.seriesMode === true ? '1' : '',
    series_start_seconds: pending.seriesMode === true ? '0' : '', series_context: ''
  };
  await saveStageARequest(credentials, credentials.repo, jobId, inputs);
  if (pending.mode === 'automatic' && pending.musicSource !== 'default') {
    await saveAutomaticMusicChoice(credentials, credentials.repo, jobId, pending.musicRef || '', pending.musicSource || 'none');
  }
  if (isRelaySource) {
    const relay = pending.relay || {};
    if (!Number.isSafeInteger(Number(relay.internal_group_chat_id)) || !Number.isSafeInteger(Number(relay.internal_group_message_id))) {
      throw new Error('The private Telegram relay metadata is incomplete. Send the video again.');
    }
    const normalizedRelay = { ...relay, internal_group_chat_id: Number(relay.internal_group_chat_id), internal_group_message_id: Number(relay.internal_group_message_id) };
    if (!env.RELAY_ENCRYPTION_KEY) throw new Error('The private Telegram relay is not configured yet. Use a supported link source for now.');
    const sealedPayload = await encryptRelayPayload({
      version: 1, job_id: jobId, target_repo: credentials.repo, target_github_pat: credentials.githubPat,
      stage_a_inputs: inputs,
      telegram: {
        group_chat_id: normalizedRelay.internal_group_chat_id, group_message_id: normalizedRelay.internal_group_message_id,
        declared_size: Number(normalizedRelay.file_size || 0), expected_file_unique_id: String(normalizedRelay.file_unique_id || ''),
        expected_mime_type: String(normalizedRelay.mime_type || ''), source_name: String(normalizedRelay.file_name || 'telegram-video.bin')
      }
    }, jobId, env.RELAY_ENCRYPTION_KEY);
    await putRelayJob(env, chatId, jobId, {
      state: 'ready', repo: credentials.repo, mode: pending.mode, relay: normalizedRelay, sealed_payload: sealedPayload
    });
    const botBUsername = String(env.BOT_B_TELEGRAM_USERNAME || '').trim();
    if (!/^[A-Za-z0-9_]{5,64}$/.test(botBUsername)) throw new Error('The private Telegram relay recipient is not configured yet.');
    await sendMessage(env, Number(relay.internal_group_chat_id), `/relay@${botBUsername} ${relayReadyMarker(jobId, chatId, Number(relay.internal_group_message_id))}`, { parseMode: 'HTML' });
  } else {
    await dispatchWorkflow(credentials, credentials.repo, 'stage-a.yml', inputs);
  }
  const label = await ensureTaskLabel(env, chatId, jobId);
  await setTaskOptions(env, chatId, jobId, { musicRef: pending.musicRef || '', mode: pending.mode });
  state.flow = null;
  state.pending = {};
  state.currentTask = jobId;
  await putState(env, chatId, state);
  const dispatchedText = isRelaySource
    ? `Task <b>${label}</b> was queued for private Telegram relay.\n<code>${escapeHtml(jobId)}</code>\nClipForge will start Stage A after the temporary relay has safely delivered the source to this clone.`
    : `Task <b>${label}</b> was dispatched.\n<code>${escapeHtml(jobId)}</code>\nUse the button below for progress.`;
  await renderInteractiveView(env, chatId, dispatchedText, { replyMarkup: buttons([[{ text: `View Task ${label}`, callback_data: `status:${label}` }, { text: 'Main menu', callback_data: 'menu:home' }]]) }, messageId);
}

async function chooseLibrary(env, chatId, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  const tracks = (await listAudioLibrary(credentials, credentials.repo)).slice(0, 10);
  if (!tracks.length) { await renderInteractiveView(env, chatId, 'No saved audio-library tracks exist in this clone. Choose no music or the saved default.', { replyMarkup: musicMenu() }, messageId); return; }
  const state = await getState(env, chatId);
  state.pending.musicOptions = tracks;
  await putState(env, chatId, state);
  await renderInteractiveView(env, chatId, 'Choose a library track for this task.', { replyMarkup: buttons([...tracks.map((track, index) => [{ text: telegramButtonText(track.name), callback_data: `music:track:${index}` }]), [{ text: 'Back to music options', callback_data: 'music:back' }]]) }, messageId);
}

async function selectMusic(env, chatId, choice, messageId = null) {
  const state = await getState(env, chatId);
  if (!state.pending || !state.pending.mode) { await renderInteractiveView(env, chatId, 'Start a new task with /manual or /automatic first.', { replyMarkup: mainMenu() }, messageId); return; }
  if (choice === 'none') { state.pending.musicRef = ''; state.pending.musicSource = 'none'; }
  else if (choice === 'default') {
    state.pending.musicRef = '';
    state.pending.musicSource = 'default';
    if (state.pending.mode === 'manual') {
      const credentials = await requireCredentials(env, chatId);
      if (!credentials) return;
      try {
        const { document } = await getJsonFile(credentials, credentials.repo, 'branding/music_default.json');
        const path = document && document.library_track_path;
        if (typeof path === 'string' && /^audio-library\/[^/\\\u0000-\u001f\u007f]+$/.test(path)) state.pending.musicRef = `path:${path}`;
      } catch (error) {
        if (!(error instanceof GitHubError && error.status === 404)) throw error;
      }
    }
  }
  else if (choice.startsWith('track:')) {
    const index = Number(choice.slice('track:'.length));
    const track = state.pending.musicOptions && state.pending.musicOptions[index];
    if (!track || !/^audio-library\/[^/\\\u0000-\u001f\u007f]+$/.test(track.path)) throw new Error('That library track is no longer available.');
    state.pending.musicRef = `path:${track.path}`;
    state.pending.musicSource = 'explicit_library';
  } else return;
  delete state.pending.musicOptions;
  await putState(env, chatId, state);
  await finishTaskLaunch(env, chatId, messageId);
}

function buildAgentHandoffPrompt(releaseUrl) {
  return `Open this GitHub Release: ${releaseUrl}\nRead 00_READ_THIS_FIRST.txt first, inspect the release assets and original video, create production.json, and return only that file.`;
}

async function sendManualAgentPrompt(env, chatId, label, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  const jobId = await getJobIdForLabel(env, chatId, label);
  if (!credentials || !jobId) return;
  const status = await readStatus(credentials, credentials.repo, jobId);
  if (!status || status.stage !== 'awaiting_json_upload') {
    await renderInteractiveView(env, chatId, 'The agent prompt is available only after a manual Stage A task reaches “Awaiting production plan”.', { replyMarkup: buttons([[{ text: 'Refresh task', callback_data: `status:${label}` }]]) }, messageId);
    return;
  }
  const releaseUrl = status.release_url;
  if (typeof releaseUrl !== 'string' || !releaseUrl.startsWith('https://')) throw new Error('The Stage A release link is unavailable. Open the release from this task’s status and retry after the release completes.');
  const prompt = buildAgentHandoffPrompt(releaseUrl);
  const replyMarkup = { inline_keyboard: [[
    { text: 'Open GitHub Release', url: releaseUrl },
    { text: 'Copy agent prompt', copy_text: { text: prompt } }
  ]] };
  await renderInteractiveView(
    env,
    chatId,
    `<b>Task ${escapeHtml(label)} agent handoff</b>\n\n${escapeHtml(prompt)}\n\nUse <b>Open GitHub Release</b> to give your agent the complete Stage A files and instructions. Then use <b>Upload production.json</b> here to start Stage B.`,
    { replyMarkup: { inline_keyboard: [...replyMarkup.inline_keyboard, [{ text: 'Upload production.json', callback_data: `plan:${label}` }, { text: 'Back to task', callback_data: `status:${label}` }]] } },
    messageId
  );
}

// Prior-part summaries become the private continuity context for the next
// part's analysis prompt — the same derivation scripts/series_state.py
// performs for the automatic continuation path.
async function seriesContextSummaries(credentials, seriesId) {
  const entries = [];
  for (const id of await listJobIds(credentials, credentials.repo)) {
    const request = await readJobRequest(credentials, id);
    if (!request || request.series_mode !== 'true' || String(request.series_id || '') !== seriesId) continue;
    const plan = await readProductionPlan(credentials, credentials.repo, id);
    const part = plan && Number(plan.series_part);
    const summary = plan && String(plan.series_summary || '').trim();
    if (Number.isInteger(part) && summary) entries.push([part, summary]);
  }
  entries.sort((a, b) => a[0] - b[0]);
  const text = entries.map(([part, summary]) => `Part ${part}: ${summary}`).join('\n');
  return (text || '(No prior summaries.)').slice(0, 8000);
}

// Manual equivalent of stage-b.yml's "Continue the next validated Series Mode
// part" step: persist the next part's Stage A request (same continuation
// payload series_state.py derives for automatic mode, with automatic_mode
// forced off) and dispatch Stage A. When that run reaches
// awaiting_json_upload, the EXISTING "Get agent prompt" / "Upload
// production.json" buttons handle the human/agent handoff unchanged.
async function startNextSeriesPart(env, chatId, label, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  const jobId = await getJobIdForLabel(env, chatId, label);
  if (!credentials || !jobId) return;
  const status = await readStatus(credentials, credentials.repo, jobId);
  const continuation = await manualSeriesContinuation(credentials, jobId, status);
  if (!continuation) {
    await renderInteractiveView(env, chatId, 'That completed task has no next Series Mode part to start (it is not a manual series part, or it was the final part).', { replyMarkup: buttons([[{ text: 'Refresh task', callback_data: `status:${label}` }]]) }, messageId);
    return;
  }
  const request = await readJobRequest(credentials, jobId);
  const nextId = `${continuation.seriesId}-p${continuation.part}`;
  if (!SAFE_JOB_RE.test(nextId) || nextId.length > 120) throw new Error('The next series part job id would be unsafe.');
  // Duplicate-dispatch guard, mirroring the request_path.exists() check in
  // stage-b.yml's automatic continuation step.
  if ((await readJobRequest(credentials, nextId)) || (await readStatus(credentials, credentials.repo, nextId))) {
    const existingLabel = await ensureTaskLabel(env, chatId, nextId);
    await renderInteractiveView(env, chatId, `Part ${continuation.part} of this series already exists as task <b>${escapeHtml(existingLabel)}</b>.\n<code>${escapeHtml(nextId)}</code>`, { replyMarkup: buttons([[{ text: `View Task ${existingLabel}`, callback_data: `status:${existingLabel}` }]]) }, messageId);
    return;
  }
  const inputs = {
    video_url: String(request.video_url || ''),
    source_type: String(request.source_type || 'url'),
    relay_release_tag: '', relay_expected_size: '', relay_sha256: '',
    torrent_file_index: String(request.torrent_file_index || ''),
    job_id: nextId,
    whisper_model: WHISPER_MODELS.has(request.whisper_model) ? request.whisper_model : 'base',
    language: String(request.language || 'auto'),
    target_duration_seconds: String(request.target_duration_seconds || '120'),
    focus: '',
    automatic_mode: 'false',
    series_mode: 'true', series_id: continuation.seriesId,
    series_source_job_id: String(request.series_source_job_id || continuation.seriesId),
    series_part: String(continuation.part),
    series_start_seconds: String(continuation.startSeconds),
    series_context: await seriesContextSummaries(credentials, continuation.seriesId)
  };
  await saveStageARequest(credentials, credentials.repo, nextId, inputs);
  await dispatchWorkflow(credentials, credentials.repo, 'stage-a.yml', inputs);
  const nextLabel = await ensureTaskLabel(env, chatId, nextId);
  await renderInteractiveView(env, chatId, `Series Part ${continuation.part} was dispatched as task <b>${escapeHtml(nextLabel)}</b>.\n<code>${escapeHtml(nextId)}</code>\nIt continues from ${continuation.startSeconds}s of the original source. When Stage A reaches \u201cAwaiting production plan\u201d, use <b>Get agent prompt</b> and <b>Upload production.json</b> exactly as you did for the previous part.`, { replyMarkup: buttons([[{ text: `View Task ${nextLabel}`, callback_data: `status:${nextLabel}` }, { text: 'Main menu', callback_data: 'menu:home' }]]) }, messageId);
}

async function sendVoicePreview(env, chatId, voice) {
  const meta = VOICES[voice];
  if (!meta) throw new Error('That narrator preview is unavailable.');
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  const bytes = await getRepositoryFileBytes(credentials, credentials.repo, `assets/tts-previews/${voice}.mp3`, 10 * 1024 * 1024);
  await sendAudioBytes(env, chatId, bytes, `${voice}.mp3`, `<b>${escapeHtml(meta.label)}</b> — ${escapeHtml(meta.style)}\nThis is the committed Edge TTS preview for the same narrator available in Settings.`);
}

async function startPlanFlow(env, chatId, label, messageId = null) {
  const jobId = await getJobIdForLabel(env, chatId, label);
  if (!jobId) { await renderInteractiveView(env, chatId, 'Use /tasks to refresh task labels.', { replyMarkup: mainMenu() }, messageId); return; }
  const state = await getState(env, chatId);
  state.flow = 'manual_plan';
  state.pending = { jobId, label };
  await putState(env, chatId, state);
  await renderInteractiveView(env, chatId, `Send the production.json document for task <b>${escapeHtml(label)}</b>, or paste the complete JSON as a message. It will be validated before anything is committed.`, { replyMarkup: buttons([[{ text: 'Back to task', callback_data: `status:${label}` }, { text: 'Cancel', callback_data: 'flow:cancel' }]]) }, messageId);
}

async function submitPlan(env, chatId, jsonText) {
  const state = await getState(env, chatId);
  const { jobId, label } = state.pending || {};
  if (!SAFE_JOB_RE.test(String(jobId || ''))) throw new Error('No valid task is awaiting a production plan.');
  let document;
  try { document = JSON.parse(jsonText); } catch { throw new Error('The submitted production plan is not valid JSON.'); }
  const errors = validateProductionPlan(document);
  if (errors.length) {
    await renderTaskInputResponse(env, chatId, `<b>Production plan not accepted</b>\n${errors.slice(0, 10).map(escapeHtml).join('\n')}\n\nCorrect the plan and send it again.`, { replyMarkup: buttons([[{ text: 'Back to task', callback_data: `status:${label}` }, { text: 'Cancel', callback_data: 'flow:cancel' }]]) });
    return;
  }
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  await saveProductionPlan(credentials, credentials.repo, jobId, `${JSON.stringify(document, null, 2)}\n`);
  const options = await getTaskOptions(env, chatId, jobId);
  const codeRef = await currentBranchSha(credentials, credentials.repo);
  await dispatchWorkflow(credentials, credentials.repo, 'stage-b.yml', {
    job_id: jobId, production_ref: `path:${PRODUCTION_PATH(jobId)}`, music_ref: options.musicRef || '', code_ref: codeRef
  });
  state.flow = null;
  state.pending = {};
  await putState(env, chatId, state);
  await renderTaskInputResponse(env, chatId, `Validated production.json and dispatched Stage B for task <b>${escapeHtml(label)}</b>.`, { replyMarkup: buttons([[{ text: `View Task ${label}`, callback_data: `status:${label}` }]]) });
}

async function handleFlowText(env, chatId, text) {
  const state = await getState(env, chatId);
  if (!state.flow) return false;
  if (state.flow === 'settings_music_upload') {
    throw new Error('Use Telegram’s attachment control to send an allowed audio document for the music library.');
  }
  if (state.flow === 'settings_github_pat' || state.flow === 'settings_shadow_pat') {
    const token = String(text || '').trim();
    if (token.length < 20 || /\s/.test(token)) throw new Error('That does not look like a valid GitHub token. Send the token again without spaces.');
    const credentials = (await getCredentials(env, chatId)) || { githubPat: '', repo: '', geminiKeys: [] };
    credentials.pendingGithubPat = token;
    await putCredentials(env, chatId, credentials);
    const creatingClone = state.flow === 'settings_shadow_pat';
    state.flow = creatingClone ? 'settings_shadow_name' : 'settings_github_repo';
    state.pending = {};
    await putState(env, chatId, state);
    await renderInteractiveView(env, chatId, creatingClone
      ? 'Now send a short name for your new <b>private</b> Shadow Clone, for example <code>my-clipforge</code>.'
      : 'Now send the clone repository in the exact form <code>owner/repository</code>.', { replyMarkup: buttons([[{ text: 'Cancel', callback_data: 'flow:cancel' }]]) });
    return true;
  }
  if (state.flow === 'settings_github_repo') {
    const repo = String(text || '').trim();
    const credentials = await getCredentials(env, chatId);
    const pat = credentials && credentials.pendingGithubPat;
    if (!pat) throw new Error('The pending GitHub token expired. Start GitHub connection again from /settings.');
    const result = await validateConnection(pat, repo);
    await putCredentials(env, chatId, { githubPat: pat, repo: result.repo, geminiKeys: credentials.geminiKeys || [] });
    state.flow = null;
    state.pending = {};
    await putState(env, chatId, state);
    await renderInteractiveView(env, chatId, `GitHub connection saved for <code>${escapeHtml(result.repo)}</code>.`, { replyMarkup: mainMenu() });
    return true;
  }
  if (state.flow === 'settings_shadow_name') {
    const name = String(text || '').trim();
    const credentials = await getCredentials(env, chatId);
    const pat = credentials && credentials.pendingGithubPat;
    if (!pat) throw new Error('The pending GitHub token expired. Start clone setup again from /start.');
    await renderInteractiveView(env, chatId, 'Creating your isolated private Shadow Clone. This can take a moment.');
    const result = await createPrivateShadowClone(pat, name);
    await putCredentials(env, chatId, { githubPat: pat, repo: result.repo, geminiKeys: [] });
    state.flow = null;
    state.pending = {};
    await putState(env, chatId, state);
    await renderInteractiveView(env, chatId, `Private Shadow Clone created: <code>${escapeHtml(result.repo)}</code>\n${result.copiedFiles} shared source files copied. Your tasks, settings, Gemini keys, voice selection, and future job artifacts stay in this clone only.`, { replyMarkup: mainMenu() });
    return true;
  }
  if (state.flow === 'settings_gemini') {
    const key = String(text || '').trim();
    if (key.length < 20 || /\s/.test(key)) throw new Error('That does not look like a Gemini API key. Send it again without spaces.');
    await saveGeminiKey(env, chatId, key, Boolean(state.pending && state.pending.replaceExisting));
    return true;
  }
  if (state.flow === 'settings_zernio_key') {
    const key = String(text || '').trim();
    if (key.length < 20 || /\s/.test(key)) throw new Error('Enter a valid Zernio API key with no whitespace.');
    const credentials = await requireCredentials(env, chatId);
    if (!credentials) return true;
    await updateZernioSecret(credentials, credentials.repo, key);
    await clearFlow(env, chatId);
    await renderInteractiveView(env, chatId, `Zernio API key ${escapeHtml(zernioFingerprint(key))} was encrypted and saved as a GitHub Actions secret.`, { replyMarkup: buttons([[{ text: 'Open Zernio settings', callback_data: 'set:zernio' }]]) });
    return true;
  }
  if (['settings_zernio_timezone', 'settings_zernio_interval', 'settings_zernio_time', 'settings_zernio_depth', 'settings_zernio_custom_start'].includes(state.flow)) {
    const value = String(text || '').trim();
    const saved = await updateZernioSetting(env, chatId, (settings) => {
      const smart = settings.smart_schedule;
      if (state.flow === 'settings_zernio_timezone') {
        if (!validZernioTimezone(value)) throw new Error('Enter a safe IANA timezone such as Europe/London, America/New_York, or UTC.');
        smart.timezone = value;
      } else if (state.flow === 'settings_zernio_interval') {
        const number = Number(value); if (!Number.isInteger(number) || number < 1 || number > 8760) throw new Error('Cadence must be a whole number from 1 to 8760 hours.');
        smart.interval_hours = number;
        delete smart.interval_days;
      } else if (state.flow === 'settings_zernio_time') {
        if (!validZernioTime(value)) throw new Error('Preferred time must use HH:MM in 24-hour format.');
        smart.preferred_time = value;
      } else if (state.flow === 'settings_zernio_depth') {
        const number = Number(value); if (!Number.isInteger(number) || number < 1 || number > 100) throw new Error('Queue depth must be a whole number from 1 to 100.');
        smart.queue_depth = number;
      } else {
        if (!validZernioDateTime(value)) throw new Error('Send the first local slot as YYYY-MM-DDTHH:MM.');
        smart.start_mode = 'custom'; smart.custom_start = value;
      }
    });
    await clearFlow(env, chatId);
    await renderInteractiveView(env, chatId, 'Zernio smart-schedule preference saved.', { replyMarkup: zernioScheduleMenu(saved.config.settings) });
    return true;
  }
  if (state.flow === 'zernio_manual_schedule' || state.flow === 'zernio_post_schedule') {
    const value = String(text || '').trim();
    if (!validZernioDateTime(value)) throw new Error('Send a local scheduled time as YYYY-MM-DDTHH:MM.');
    const pending = state.pending || {};
    await clearFlow(env, chatId);
    if (state.flow === 'zernio_manual_schedule') await dispatchZernioPublish(env, chatId, pending.label, 'manual_schedule', value);
    else await dispatchZernioPostAction(env, chatId, pending.label, pending.postId, 'update', 'manual_schedule', value);
    return true;
  }
  if (state.flow === 'settings_watermark') {
    const value = String(text || '').replace(/\s+/g, ' ').trim();
    if (value.length > 64) throw new Error('Watermark text must be 64 characters or fewer.');
    const credentials = await requireCredentials(env, chatId);
    if (!credentials) return true;
    await saveWatermark(credentials, credentials.repo, value.toLowerCase() === 'clear' ? '' : value);
    await clearFlow(env, chatId);
    await renderInteractiveView(env, chatId, value.toLowerCase() === 'clear' ? 'Creator watermark cleared.' : 'Creator watermark saved for future Stage B videos.', { replyMarkup: buttons([[{ text: 'Back to settings', callback_data: 'menu:settings' }]]) });
    return true;
  }
  if (state.flow === 'manual_source' || state.flow === 'automatic_source') {
    const source = validateSource(text);
    if (isPublicTelegramPost(source)) {
      const credentials = await requireCredentials(env, chatId);
      if (!credentials) return true;
      if (!permitsLegacyTelegramMtproto(env, credentials)) {
        throw new Error('Public Telegram channel-link intake is reserved for the original ClipForge repository. This Shadow Clone accepts direct videos sent or forwarded to ClipForge, plus Drive, direct-file, magnet, and .torrent sources.');
      }
    }
    state.pending.source = source;
    await preflightTelegramChannelPost(state.pending.source);
    state.flow = taskSetupFlowAfterSource(state.pending);
    await putState(env, chatId, state);
    if (state.pending.seriesMode === true) {
      await renderTaskInputResponse(env, chatId, '<b>Series Mode does not use editorial focus.</b> Choose the target output length for each part.', { replyMarkup: durationMenu() });
    } else {
      await renderTaskInputResponse(env, chatId, 'Send an optional editorial focus. Send <code>-</code> to consider the whole video.', { replyMarkup: buttons([[{ text: 'Back', callback_data: 'setup:back' }, { text: 'Cancel', callback_data: 'flow:cancel' }]]) });
    }
    return true;
  }
  if (state.flow === 'manual_focus' || state.flow === 'automatic_focus') {
    if (state.pending.seriesMode === true) {
      delete state.pending.focus;
      state.flow = `${state.pending.mode}_duration`;
      await putState(env, chatId, state);
      await renderTaskInputResponse(env, chatId, '<b>Series Mode ignores editorial focus.</b> Choose the target output length for each part.', { replyMarkup: durationMenu() });
      return true;
    }
    state.pending.focus = normalizeFocus(text);
    state.flow = `${state.pending.mode}_duration`;
    await putState(env, chatId, state);
    await renderTaskInputResponse(env, chatId, 'Choose the target output length.', { replyMarkup: durationMenu() });
    return true;
  }
  if (state.flow === 'manual_plan') { await submitPlan(env, chatId, text); return true; }
  return false;
}

async function saveGeminiKey(env, chatId, key, replacingLegacy, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  const existing = credentials.geminiKeys || [];
  const fingerprint = geminiFingerprint(key);
  if (existing.some((entry) => geminiFingerprint(entry) === fingerprint)) throw new Error('That Gemini key is already stored.');
  const metadata = await readGeminiMetadata(credentials, credentials.repo);
  if (!replacingLegacy && !existing.length && metadata.length) {
    credentials.pendingGeminiKey = key;
    await putCredentials(env, chatId, credentials);
    const state = await getState(env, chatId);
    state.flow = 'settings_gemini_replace_confirm';
    state.pending = {};
    await putState(env, chatId, state);
    await renderInteractiveView(env, chatId, 'This clone has Gemini key fingerprints from another interface session. GitHub cannot reveal their raw values, so adding this key would replace that secret. Continue only if you want to replace it.', { replyMarkup: buttons([[{ text: 'Replace existing key set', callback_data: 'set:gemini_replace' }, { text: 'Cancel', callback_data: 'flow:cancel' }]]) }, messageId);
    return;
  }
  const keys = [...existing, key];
  await updateGeminiSecret(credentials, credentials.repo, keys);
  const nextMetadata = [...metadata.filter((entry) => entry && entry.fingerprint !== fingerprint), { fingerprint, added_at_epoch: Math.floor(Date.now() / 1000) }];
  await writeGeminiMetadata(credentials, credentials.repo, nextMetadata);
  await putCredentials(env, chatId, { ...credentials, geminiKeys: keys, pendingGeminiKey: '' });
  await clearFlow(env, chatId);
  await renderInteractiveView(env, chatId, `Gemini key ${escapeHtml(fingerprint)} was encrypted and saved to the GitHub Actions secret.`, { replyMarkup: buttons([[{ text: 'Back to settings', callback_data: 'menu:settings' }]]) }, messageId);
}

async function handleTorrentUpload(env, chatId, document, state) {
  const filename = String(document && document.file_name || '');
  if (!/\.torrent$/i.test(filename) || !document.file_size || document.file_size > MAX_TORRENT_BYTES) {
    throw new Error('Upload a non-empty .torrent file no larger than 1 MB.');
  }
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  const file = await getTelegramFile(env, document.file_id);
  if (!file || !file.file_path) throw new Error('Telegram did not return the uploaded torrent.');
  const bytes = await downloadTelegramFileBytes(env, file.file_path, MAX_TORRENT_BYTES);
  if (bytes[0] !== 0x64) throw new Error('The uploaded file is not a valid bencoded torrent manifest.');
  const jobId = startTaskJobId(state.pending.mode);
  await putBinaryFile(credentials, credentials.repo, `jobs/${jobId}/source.torrent`, bytes, `clipforge: upload source.torrent for job ${jobId}`);
  state.pending.source = `path:jobs/${jobId}/source.torrent`;
  state.pending.jobId = jobId;
  state.flow = taskSetupFlowAfterSource(state.pending);
  await putState(env, chatId, state);
  if (state.pending.seriesMode === true) {
    await renderTaskInputResponse(env, chatId, `<b>Torrent source staged — setup is not complete yet.</b>\n<code>${escapeHtml(jobId)}</code>\n\nSeries Mode does not use editorial focus. Choose output length and music. Only after those choices will Stage A dispatch and ask you to choose the video file inside the torrent.`, { replyMarkup: durationMenu() });
  } else {
    await renderTaskInputResponse(env, chatId, `<b>Torrent source staged — setup is not complete yet.</b>\n<code>${escapeHtml(jobId)}</code>\n\nNext, send an optional editorial focus, or send <code>-</code> to consider the whole video. Then choose output length and music. Only after those choices will Stage A dispatch and ask you to choose the video file inside the torrent.`, { replyMarkup: buttons([[{ text: 'Back', callback_data: 'setup:back' }, { text: 'Cancel', callback_data: 'flow:cancel' }]]) });
  }
}

async function handleRelayVideo(env, chatId, message) {
  const state = await getState(env, chatId);
  if (!['manual_source', 'automatic_source'].includes(state.flow)) {
    await renderTaskInputResponse(env, chatId, 'This video was not expected. Start Manual or Automatic Mode first, then send or forward one video directly to ClipForge.', { replyMarkup: mainMenu() });
    return;
  }
  const relayGroup = String(env.INTERNAL_RELAY_GROUP_CHAT_ID || '').trim();
  if (!/^-?[1-9][0-9]{0,18}$/.test(relayGroup)) throw new Error('Direct Telegram forwarding is not configured yet. Use a public channel post link for now.');
  const media = relayVideoMetadata(message);
  if (!media) throw new Error('Send a video or a video document. Other file types are not supported as a direct source.');
  const credentials = await requireCredentials(env, chatId);
  if (!credentials) return;
  const jobId = startTaskJobId(state.pending.mode);
  let resolvedRelayGroup = Number(relayGroup);
  let copied;
  try {
    copied = await copyMessage(env, resolvedRelayGroup, chatId, message.message_id, relayCaption(jobId, chatId));
  } catch (error) {
    if (!(error instanceof TelegramError) || !Number.isSafeInteger(error.migrateToChatId)) throw error;
    resolvedRelayGroup = error.migrateToChatId;
    copied = await copyMessage(env, resolvedRelayGroup, chatId, message.message_id, relayCaption(jobId, chatId));
  }
  const internalMessageId = Number(copied && copied.message_id || 0);
  if (!Number.isSafeInteger(internalMessageId) || internalMessageId <= 0) throw new Error('Telegram copied the video without returning an internal relay message identifier.');
  state.pending.source = RELAY_SOURCE_TYPE;
  state.pending.relay = {
    ...media,
    internal_group_chat_id: resolvedRelayGroup,
    internal_group_message_id: internalMessageId,
  };
  state.pending.jobId = jobId;
  state.flow = taskSetupFlowAfterSource(state.pending);
  await putState(env, chatId, state);
  const next = state.pending.seriesMode === true
    ? `<b>Video securely staged — setup is not complete yet.</b>\n<code>${escapeHtml(jobId)}</code>\n\nSeries Mode does not use editorial focus. Choose output length and music. ClipForge will then begin the private relay and Stage A.`
    : `<b>Video securely staged — setup is not complete yet.</b>\n<code>${escapeHtml(jobId)}</code>\n\nNext, send an optional editorial focus, or send <code>-</code> to consider the whole video. Then choose output length and music. ClipForge will begin the private relay only after your task setup is complete.`;
  await renderTaskInputResponse(env, chatId, next, { replyMarkup: state.pending.seriesMode === true ? durationMenu() : buttons([[{ text: 'Back', callback_data: 'setup:back' }, { text: 'Cancel', callback_data: 'flow:cancel' }]]) });
}

async function handleDocument(env, chatId, document, message = null) {
  const state = await getState(env, chatId);
  if (message && ['manual_source', 'automatic_source'].includes(state.flow) && relayVideoMetadata(message)) {
    return handleRelayVideo(env, chatId, message);
  }
  if (state.flow === 'settings_music_upload') {
    await handleMusicLibraryUpload(env, chatId, document);
    return;
  }
  if (state.flow === 'manual_source' || state.flow === 'automatic_source') {
    await handleTorrentUpload(env, chatId, document, state);
    return;
  }
  if (state.flow !== 'manual_plan') { await renderTaskInputResponse(env, chatId, 'This document was not expected. Start Manual or Automatic Mode first to upload a .torrent file, or choose a task awaiting production.json before uploading a production plan.', { replyMarkup: mainMenu() }); return; }
  if (!document || document.file_size > 1024 * 1024) throw new Error('production.json must be 1 MB or smaller.');
  const file = await getTelegramFile(env, document.file_id);
  if (!file || !file.file_path) throw new Error('Telegram did not return the uploaded file.');
  const content = await downloadTelegramFile(env, file.file_path);
  await submitPlan(env, chatId, content);
}

async function showTorrentCandidates(env, chatId, label, pageValue = '0', messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  const jobId = await getJobIdForLabel(env, chatId, label);
  if (!credentials || !jobId) return;
  const selection = await tryGetJsonFile(credentials, credentials.repo, `jobs/${jobId}/torrent-selection.json`);
  const candidates = selection && selection.document && Array.isArray(selection.document.video_candidates) ? selection.document.video_candidates : [];
  if (!candidates.length) throw new Error('The torrent video choices are not available yet. Refresh the task status in a moment.');
  const totalPages = Math.ceil(candidates.length / TORRENT_CANDIDATES_PER_PAGE);
  const requestedPage = Number(pageValue);
  const page = Number.isInteger(requestedPage) ? Math.min(Math.max(requestedPage, 0), totalPages - 1) : 0;
  const rows = candidates.slice(page * TORRENT_CANDIDATES_PER_PAGE, (page + 1) * TORRENT_CANDIDATES_PER_PAGE).map((candidate) => {
    const index = Number(candidate && candidate.index);
    if (!Number.isInteger(index) || index < 1) return null;
    const name = torrentCandidateButtonText(candidate.path || `Video ${index}`);
    return [
      { text: `${index}. ${name}`, callback_data: `torrentpick:${label}:${index}` },
      { text: 'Details', callback_data: `torrentdetail:${label}:${index}:${page}` }
    ];
  }).filter(Boolean);
  if (totalPages > 1) {
    const navigation = [];
    if (page > 0) navigation.push({ text: 'Previous', callback_data: `torrentpage:${label}:${page - 1}` });
    navigation.push({ text: `Page ${page + 1}/${totalPages}`, callback_data: `torrentpage:${label}:${page}` });
    if (page < totalPages - 1) navigation.push({ text: 'Next', callback_data: `torrentpage:${label}:${page + 1}` });
    rows.push(navigation);
  }
  const text = `Choose the video file inside this torrent (page ${page + 1} of ${totalPages}). This starts Stage A for the selected file only.`;
  const options = { replyMarkup: buttons(rows) };
  return renderInteractiveView(env, chatId, text, options, messageId);
}

async function showTorrentCandidateDetails(env, chatId, label, indexValue, pageValue = '0', messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  const jobId = await getJobIdForLabel(env, chatId, label);
  const index = Number(indexValue);
  if (!credentials || !jobId) return;
  if (!Number.isInteger(index) || index < 1) throw new Error('That torrent video choice is invalid.');
  const selection = await tryGetJsonFile(credentials, credentials.repo, `jobs/${jobId}/torrent-selection.json`);
  const candidates = selection && selection.document && Array.isArray(selection.document.video_candidates) ? selection.document.video_candidates : [];
  const candidate = candidates.find((entry) => Number(entry && entry.index) === index);
  if (!candidate) throw new Error('That torrent video is no longer available. Refresh the task status.');
  const filename = normalizedButtonText(candidate.path, 'Unnamed video');
  const details = `<b>Torrent video ${index}</b>\n\n<b>Full filename</b>\n<code>${escapeHtml(filename)}</code>\n\n<b>Size</b>\n${escapeHtml(formatBytes(candidate.length))}\n\nTap the numbered filename button when this is the video you want Stage A to process.`;
  const page = Number.isInteger(Number(pageValue)) ? Math.max(0, Number(pageValue)) : 0;
  const detailMarkup = buttons([[{ text: `Select video ${index}`, callback_data: `torrentpick:${label}:${index}` }], [{ text: 'Back to video list', callback_data: `torrentpage:${label}:${page}` }]]);
  if (details.length <= 3500) return renderInteractiveView(env, chatId, details, { replyMarkup: detailMarkup }, messageId);
  return sendDocumentBytes(env, chatId, new TextEncoder().encode(`Torrent video ${index}\n\nFull filename:\n${filename}\n\nSize: ${formatBytes(candidate.length)}\n`), `torrent-video-${index}-details.txt`, 'Full torrent candidate details');
}

async function chooseTorrentCandidate(env, chatId, label, indexValue, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  const jobId = await getJobIdForLabel(env, chatId, label);
  const index = Number(indexValue);
  if (!credentials || !jobId) return;
  if (!Number.isInteger(index) || index < 1) throw new Error('That torrent video choice is invalid.');
  const selection = await tryGetJsonFile(credentials, credentials.repo, `jobs/${jobId}/torrent-selection.json`);
  const candidates = selection && selection.document && Array.isArray(selection.document.video_candidates) ? selection.document.video_candidates : [];
  if (!candidates.some((candidate) => Number(candidate && candidate.index) === index)) throw new Error('That torrent video is no longer available. Refresh the task status.');
  const inputs = await readStageARequest(credentials, credentials.repo, jobId);
  await dispatchWorkflow(credentials, credentials.repo, 'stage-a.yml', {
    video_url: String(inputs.video_url || `path:jobs/${jobId}/source.torrent`), torrent_file_index: String(index), job_id: jobId,
    whisper_model: WHISPER_MODELS.has(inputs.whisper_model) ? inputs.whisper_model : 'base', language: String(inputs.language || 'auto'),
    target_duration_seconds: String(inputs.target_duration_seconds || '120'), focus: String(inputs.focus || ''), automatic_mode: inputs.automatic_mode === 'true' ? 'true' : 'false'
  });
  await renderInteractiveView(env, chatId, `Selected torrent video ${index}. Stage A has started for task <b>${escapeHtml(label)}</b>.`, { replyMarkup: buttons([[{ text: 'Refresh task', callback_data: `status:${label}` }]]) }, messageId);
}

async function restartStageA(env, chatId, label, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  const jobId = await getJobIdForLabel(env, chatId, label);
  if (!credentials || !jobId) return;
  const inputs = await readStageARequest(credentials, credentials.repo, jobId);
  if (inputs.automatic_mode === 'true' && !(credentials.geminiKeys || []).length) {
    const metadata = await readGeminiMetadata(credentials, credentials.repo).catch(() => []);
    if (!metadata.length) throw new Error('Automatic Mode needs a stored Gemini API key before Stage A can restart.');
  }
  await dispatchWorkflow(credentials, credentials.repo, 'stage-a.yml', {
    video_url: String(inputs.video_url || ''), source_type: String(inputs.source_type || 'url'),
    relay_release_tag: String(inputs.relay_release_tag || ''), relay_expected_size: String(inputs.relay_expected_size || ''), relay_sha256: String(inputs.relay_sha256 || ''),
    torrent_file_index: String(inputs.torrent_file_index || ''), job_id: jobId,
    whisper_model: WHISPER_MODELS.has(inputs.whisper_model) ? inputs.whisper_model : 'base', language: String(inputs.language || 'auto'),
    target_duration_seconds: String(inputs.target_duration_seconds || '120'), focus: String(inputs.focus || ''), automatic_mode: inputs.automatic_mode === 'true' ? 'true' : 'false'
  });
  await renderInteractiveView(env, chatId, `Restarted Stage A for task <b>${escapeHtml(label)}</b>.`, { replyMarkup: buttons([[{ text: 'Refresh task', callback_data: `status:${label}` }]]) }, messageId);
}

async function restartStageB(env, chatId, label, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  const jobId = await getJobIdForLabel(env, chatId, label);
  if (!credentials || !jobId) return;
  const codeRef = await currentBranchSha(credentials, credentials.repo);
  const options = await getTaskOptions(env, chatId, jobId);
  await dispatchWorkflow(credentials, credentials.repo, 'stage-b.yml', { job_id: jobId, production_ref: `path:${PRODUCTION_PATH(jobId)}`, music_ref: options.musicRef || '', code_ref: codeRef });
  await renderInteractiveView(env, chatId, `Restarted Stage B for task <b>${escapeHtml(label)}</b> using the current default-branch code.`, { replyMarkup: buttons([[{ text: 'Refresh task', callback_data: `status:${label}` }]]) }, messageId);
}

async function cancelStageBPrompt(env, chatId, label, messageId = null) {
  await renderInteractiveView(env, chatId, `Cancel Stage B for task <b>${escapeHtml(label)}</b>? The current GitHub Actions run will be cancelled.`, { replyMarkup: buttons([[{ text: 'Cancel Stage B', callback_data: `cancel:confirm:${label}` }, { text: 'Keep running', callback_data: `status:${label}` }]]) }, messageId);
}

async function cancelStageB(env, chatId, label, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  const jobId = await getJobIdForLabel(env, chatId, label);
  if (!credentials || !jobId) return;
  const status = await readStatus(credentials, credentials.repo, jobId);
  const runId = status && status.extra && Number(status.extra.workflow_run_id);
  if (!['stage_b_queued', 'stage_b_running'].includes(status && status.stage) || !Number.isFinite(runId) || runId <= 0) throw new Error('This task does not currently expose an active Stage B run to cancel.');
  await cancelWorkflowRun(credentials, credentials.repo, runId);
  await renderInteractiveView(env, chatId, `Cancellation requested for Stage B task <b>${escapeHtml(label)}</b>.`, { replyMarkup: buttons([[{ text: 'Refresh task', callback_data: `status:${label}` }]]) }, messageId);
}

async function promptTaskDelete(env, chatId, label, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  const jobId = await getJobIdForLabel(env, chatId, label);
  if (!credentials || !jobId) return;
  const status = await readStatus(credentials, credentials.repo, jobId);
  if (!taskCanBeDeleted(status)) {
    return renderInteractiveView(env, chatId, 'This task is still active or has not written a terminal status. Refresh it after the run finishes or fails before deleting it.', { replyMarkup: buttons([[{ text: 'Refresh task', callback_data: `status:${label}` }]]) }, messageId);
  }
  const publishingWarning = status.publishing ? '\n\nThis does not withdraw any Zernio post that may already have been submitted or published.' : '';
  return renderInteractiveView(env, chatId, `<b>Delete task ${escapeHtml(label)}?</b>\n\nThis permanently removes the task’s job files, release assets, release tag, and this chat’s task record from the connected Shadow Clone. It cannot be undone.${publishingWarning}`, { replyMarkup: buttons([[{ text: 'Delete task permanently', callback_data: `task:confirm:${label}` }, { text: 'Keep task', callback_data: `status:${label}` }]]) }, messageId);
}

async function deleteTask(env, chatId, label, messageId = null) {
  const credentials = await requireCredentials(env, chatId);
  const jobId = await getJobIdForLabel(env, chatId, label);
  if (!credentials || !jobId) return;
  const status = await readStatus(credentials, credentials.repo, jobId);
  if (!taskCanBeDeleted(status)) throw new Error('This task is still active and cannot be deleted yet. Refresh its status after it stops.');
  const removed = await deleteClipforgeJob(credentials, credentials.repo, jobId);
  const localRemoved = await removeTask(env, chatId, label, jobId);
  if (!localRemoved) throw new Error('The task mapping changed before deletion completed. Refresh the task list.');
  const state = await getState(env, chatId);
  if (state.currentTask === jobId) state.currentTask = null;
  if (state.pending && state.pending.jobId === jobId) { state.flow = null; state.pending = {}; }
  await putState(env, chatId, state);
  return renderInteractiveView(env, chatId, `<b>Task ${escapeHtml(label)} deleted.</b>\n\nRemoved ${removed.deletedFiles} job file${removed.deletedFiles === 1 ? '' : 's'} and the associated ClipForge release when present. Other tasks and all other chats remain unchanged.`, { replyMarkup: buttons([[{ text: 'View tasks', callback_data: 'menu:tasks' }, { text: 'Main menu', callback_data: 'menu:home' }]]) }, messageId);
}

async function handleCommand(env, chatId, command) {
  // Commands are typed into the chat, so their response must be a new bot
  // message. Only callback-driven navigation is allowed to replace a view.
  const state = await getState(env, chatId);
  state.activeViewId = null;
  await putState(env, chatId, state);
  if (command === '/start' || command === '/help') return showHome(env, chatId);
  if (command === '/settings') return showSettings(env, chatId);
  if (command === '/tasks') return listTasks(env, chatId, false);
  if (command === '/status') return listTasks(env, chatId, false);
  if (command === '/done') return listTasks(env, chatId, true);
  if (command === '/manual') return beginTask(env, chatId, 'manual');
  if (command === '/automatic') return beginTask(env, chatId, 'automatic');
  if (command === '/cancel') { await clearFlow(env, chatId); return renderInteractiveView(env, chatId, 'Current setup or task-input flow cancelled.', { replyMarkup: mainMenu() }); }
}

async function handleCallback(env, callback) {
  const chatId = callback.message && callback.message.chat && callback.message.chat.id;
  if (!chatId || !callback.data || callback.message.chat.type !== 'private') return;
  await answerCallback(env, callback.id);
  const [kind, ...rest] = callback.data.split(':');
  const value = rest.join(':');
  const viewId = callbackMessageId(callback);
  // Always bind the current callback to the exact view the user tapped. This
  // also makes nested helpers and typed follow-up flows edit that same screen.
  if (viewId) {
    const state = await getState(env, chatId);
    state.activeViewId = viewId;
    await putState(env, chatId, state);
  }
  if (kind === 'resume' && value === 'task') return resumePendingTask(env, chatId, viewId);
  if (kind === 'menu') {
    if (value === 'home') return showHome(env, chatId, viewId);
    if (value === 'settings') return showSettings(env, chatId, viewId);
    if (value === 'tasks' || value === 'status') return listTasks(env, chatId, false, viewId);
    if (value === 'done') return listTasks(env, chatId, true, viewId);
    if (value === 'manual' || value === 'auto') return beginTask(env, chatId, value === 'auto' ? 'automatic' : 'manual', viewId);
  }
  if (kind === 'set') {
    if (value === 'github') {
      return renderInteractiveView(env, chatId, 'This shared bot can operate an existing ClipForge clone or create a separate private Shadow Clone for this chat. Choose one option.', { replyMarkup: cloneOnboardingMenu() }, viewId);
    }
    if (value === 'gemini') {
      const credentials = await requireCredentials(env, chatId);
      if (!credentials) return;
      const metadata = await readGeminiMetadata(credentials, credentials.repo).catch(() => []);
      if (!(credentials.geminiKeys || []).length && metadata.length) {
        return renderInteractiveView(env, chatId, `This clone already has ${metadata.length} Gemini key${metadata.length === 1 ? '' : 's'} configured by the ClipForge site. Automatic Mode will use that existing GitHub Actions secret. You do not need to add the key again.`, { replyMarkup: buttons([[{ text: 'Replace existing key set', callback_data: 'set:gemini_replace_start' }], [{ text: 'Keep existing settings', callback_data: 'flow:cancel' }]]) }, viewId);
      }
      const state = await getState(env, chatId); state.flow = 'settings_gemini'; state.pending = {}; await putState(env, chatId, state);
      return renderInteractiveView(env, chatId, 'Send one Gemini API key. It will be encrypted, stored in GitHub Actions as <code>GEMINI_API_KEYS</code>, and never committed to the repository.', { replyMarkup: buttons([[{ text: 'Cancel', callback_data: 'flow:cancel' }]]) }, viewId);
    }
    if (value === 'gemini_replace_start') {
      const credentials = await requireCredentials(env, chatId);
      if (!credentials) return;
      const state = await getState(env, chatId); state.flow = 'settings_gemini'; state.pending = { replaceExisting: true }; await putState(env, chatId, state);
      return renderInteractiveView(env, chatId, 'You are about to replace the existing site-managed Gemini key set. Send the replacement Gemini API key. This cannot recover or merge the opaque existing key set.', { replyMarkup: buttons([[{ text: 'Cancel', callback_data: 'flow:cancel' }]]) }, viewId);
    }
    if (value === 'gemini_replace') {
      const credentials = await requireCredentials(env, chatId);
      if (!credentials || !credentials.pendingGeminiKey) throw new Error('No pending Gemini key was found. Start the Gemini setup flow again.');
      return saveGeminiKey(env, chatId, credentials.pendingGeminiKey, true, viewId);
    }
    if (value === 'voice') {
      const rows = Object.entries(VOICES).map(([id, meta]) => [
        { text: `Use ${meta.label} — ${meta.gender}`, callback_data: `voice:${id}` },
        { text: `Preview ${meta.label}`, callback_data: `preview:${id}` }
      ]);
      return renderInteractiveView(env, chatId, 'Choose the Edge TTS default narrator for future Stage B jobs, or preview any of the ten committed voice samples first.', { replyMarkup: buttons(rows) }, viewId);
    }
    if (value === 'music_library' || value === 'music_default') return showMusicLibrary(env, chatId, '0', viewId);
    if (value === 'watermark') {
      if (!await requireCredentials(env, chatId)) return;
      const state = await getState(env, chatId); state.flow = 'settings_watermark'; state.pending = {}; await putState(env, chatId, state);
      return renderInteractiveView(env, chatId, 'Send the creator watermark text (up to 64 characters), or send <code>clear</code> to remove it.', { replyMarkup: buttons([[{ text: 'Cancel', callback_data: 'flow:cancel' }]]) }, viewId);
    }
    if (value === 'series') {
      const credentials = await requireCredentials(env, chatId); if (!credentials) return;
      const current = await readSeriesSettings(credentials, credentials.repo).catch(() => null);
      const enabled = !(current && current.enabled === true);
      await saveSeriesSettings(credentials, credentials.repo, enabled);
      return renderInteractiveView(env, chatId, enabled
        ? '<b>Series Mode is on.</b> New tasks begin at Part 1. Later parts reuse the original Stage A evidence and begin after the prior part\'s claimed source timestamp.'
        : '<b>Series Mode is off.</b> New tasks use the ordinary one-video flow. Existing series continue unchanged.', { replyMarkup: settingsMenu() }, viewId);
    }
    if (value === 'zernio') return showZernioSettings(env, chatId, viewId);
    if (value === 'zernio_key') {
      if (!await requireCredentials(env, chatId)) return;
      const state = await getState(env, chatId); state.flow = 'settings_zernio_key'; state.pending = {}; await putState(env, chatId, state);
      return renderInteractiveView(env, chatId, 'Send the Zernio API key. It will be encrypted into the <code>ZERNIO_API_KEY</code> GitHub Actions secret, never committed, and never shown again.', { replyMarkup: buttons([[{ text: 'Cancel', callback_data: 'flow:cancel' }]]) }, viewId);
    }
  }
  if (kind === 'ml') {
    const [action, argument] = value.split(':');
    if (action === 'page') return showMusicLibrary(env, chatId, argument, viewId);
    if (action === 'preview') return previewMusicLibraryTrack(env, chatId, argument);
    if (action === 'default') return setMusicLibraryDefault(env, chatId, argument, viewId);
    if (action === 'delete') return promptMusicLibraryDelete(env, chatId, argument, viewId);
    if (action === 'deleteconfirm') return deleteMusicLibraryTrack(env, chatId, argument, viewId);
    if (action === 'upload') return promptMusicLibraryUpload(env, chatId, viewId);
    return;
  }
  if (kind === 'series') {
    const [action, label] = value.split(':');
    if (action === 'next') return startNextSeriesPart(env, chatId, label, viewId);
    return;
  }
  if (kind === 'task') {
    const [action, label] = value.split(':');
    if (action === 'delete') return promptTaskDelete(env, chatId, label, viewId);
    if (action === 'confirm') return deleteTask(env, chatId, label, viewId);
    return;
  }
  if (kind === 'zernio') {
    if (value === 'refresh') return refreshZernioAccounts(env, chatId, viewId);
    if (value === 'targets') return showZernioTargets(env, chatId, viewId);
    if (value === 'schedule') {
      const credentials = await requireCredentials(env, chatId); if (!credentials) return;
      const config = await loadZernioConfig(credentials);
      return renderInteractiveView(env, chatId, '<b>Zernio smart schedule</b>\nSet the account timezone, posting cadence, preferred local time, maximum queue depth, and first-slot policy. Every change saves to this clone.', { replyMarkup: zernioScheduleMenu(config.settings) }, viewId);
    }
    if (value === 'toggle_enabled' || value === 'toggle_auto' || value === 'mode') {
      const saved = await updateZernioSetting(env, chatId, (settings) => {
        if (value === 'toggle_enabled') settings.enabled = !settings.enabled;
        else if (value === 'toggle_auto') settings.auto_publish = !settings.auto_publish;
        else settings.automatic_mode = settings.automatic_mode === 'publish_now' ? 'smart_schedule' : 'publish_now';
      });
      return renderInteractiveView(env, chatId, 'Zernio publishing preference saved.', { replyMarkup: zernioSettingsMenu(saved.config) }, viewId);
    }
    if (value === 'clear_prompt') return renderInteractiveView(env, chatId, 'Remove the stored <code>ZERNIO_API_KEY</code>? Existing Zernio post records remain, but new requests will fail until another key is saved.', { replyMarkup: buttons([[{ text: 'Remove API key', callback_data: 'zernio:clear_confirm' }, { text: 'Keep API key', callback_data: 'set:zernio' }]]) }, viewId);
    if (value === 'clear_confirm') {
      const credentials = await requireCredentials(env, chatId); if (!credentials) return;
      await deleteZernioSecret(credentials, credentials.repo);
      return renderInteractiveView(env, chatId, 'The stored Zernio API key was removed. Publishing preferences and records were not changed.', { replyMarkup: buttons([[{ text: 'Open Zernio settings', callback_data: 'set:zernio' }]]) }, viewId);
    }
  }
  if (kind === 'ztarget') {
    const [platform, accountId] = value.split(':');
    if (!['tiktok', 'youtube'].includes(platform) || !/^[A-Za-z0-9._-]{3,200}$/.test(accountId || '')) throw new Error('That Zernio account selection is invalid.');
    const saved = await updateZernioSetting(env, chatId, (settings, accounts) => {
      const active = activeZernioAccounts(accounts)[platform];
      if (!active.some((account) => account.id === accountId)) throw new Error('That Zernio account is unavailable or requires reconnection.');
      const selected = new Set(settings.target_accounts[platform] || []);
      if (selected.has(accountId)) selected.delete(accountId); else selected.add(accountId);
      settings.target_accounts[platform] = [...selected];
    });
    return showZernioTargets(env, chatId, viewId);
  }
  if (kind === 'zsch') {
    const credentials = await requireCredentials(env, chatId); if (!credentials) return;
    const config = await loadZernioConfig(credentials);
    if (value === 'start') return renderInteractiveView(env, chatId, 'Choose when the smart schedule should begin.', { replyMarkup: buttons([[{ text: 'Next available slot', callback_data: 'zsch:startnext' }, { text: 'Custom local date', callback_data: 'zsch:startcustom' }], [{ text: 'Back', callback_data: 'zernio:schedule' }]]) }, viewId);
    if (value === 'startnext') {
      const saved = await updateZernioSetting(env, chatId, (settings) => { settings.smart_schedule.start_mode = 'next_available'; settings.smart_schedule.custom_start = ''; });
      return renderInteractiveView(env, chatId, 'Smart scheduling will use the next available slot.', { replyMarkup: zernioScheduleMenu(saved.config.settings) }, viewId);
    }
    const field = { timezone: 'settings_zernio_timezone', interval: 'settings_zernio_interval', time: 'settings_zernio_time', depth: 'settings_zernio_depth', startcustom: 'settings_zernio_custom_start' }[value];
    if (!field) return;
    const state = await getState(env, chatId); state.flow = field; state.pending = {}; await putState(env, chatId, state);
    const instruction = value === 'timezone' ? 'Send an IANA timezone, for example <code>Europe/London</code>, <code>America/New_York</code>, or <code>UTC</code>.' : value === 'interval' ? 'Send the smart-schedule cadence in whole hours (1–8760). Send <code>1</code> to publish at one-hour intervals.' : value === 'time' ? 'Send the preferred local posting time in <code>HH:MM</code> 24-hour format. It anchors the first slot; hourly cadence advances from there.' : value === 'depth' ? 'Send the maximum active Zernio queue depth (1–100).' : 'Send the first local smart-schedule slot as <code>YYYY-MM-DDTHH:MM</code>.';
    return renderInteractiveView(env, chatId, instruction, { replyMarkup: buttons([[{ text: 'Cancel', callback_data: 'flow:cancel' }]]) }, viewId);
  }
  if (kind === 'zpub') {
    const [action, label] = value.split(':');
    if (action === 'menu') return showZernioPublishMenu(env, chatId, label, viewId);
    if (action === 'now') return dispatchZernioPublish(env, chatId, label, 'publish_now', '', viewId);
    if (action === 'smart') return dispatchZernioPublish(env, chatId, label, 'smart_schedule', '', viewId);
    if (action === 'manual') {
      const state = await getState(env, chatId); state.flow = 'zernio_manual_schedule'; state.pending = { label }; await putState(env, chatId, state);
      return renderInteractiveView(env, chatId, 'Send the local scheduled time as <code>YYYY-MM-DDTHH:MM</code>. The configured Zernio timezone will be used.', { replyMarkup: buttons([[{ text: 'Cancel', callback_data: 'flow:cancel' }]]) }, viewId);
    }
  }
  if (kind === 'zpost') {
    const [action, label, postId] = value.split(':');
    if (action === 'retry') return dispatchZernioPostAction(env, chatId, label, postId, 'retry', '', '', viewId);
    if (action === 'now') return dispatchZernioPostAction(env, chatId, label, postId, 'update', 'publish_now', '', viewId);
    if (action === 'cancel') return dispatchZernioPostAction(env, chatId, label, postId, 'cancel', '', '', viewId);
    if (action === 'manual') {
      const state = await getState(env, chatId); state.flow = 'zernio_post_schedule'; state.pending = { label, postId }; await putState(env, chatId, state);
      return renderInteractiveView(env, chatId, 'Send the new local scheduled time as <code>YYYY-MM-DDTHH:MM</code>. The configured Zernio timezone will be used.', { replyMarkup: buttons([[{ text: 'Cancel', callback_data: 'flow:cancel' }]]) }, viewId);
    }
  }
  if (kind === 'clone') {
    const state = await getState(env, chatId);
    state.flow = value === 'new' ? 'settings_shadow_pat' : 'settings_github_pat';
    state.pending = {};
    await putState(env, chatId, state);
    return renderInteractiveView(env, chatId, value === 'new'
      ? 'Send a GitHub PAT for your account. The bot will use it once to create and connect a new <b>private</b> Shadow Clone. It needs repository contents, Actions secrets, workflow-dispatch, and repository-creation permission. The token is encrypted before storage.'
      : 'Send a GitHub PAT for the existing clone. It needs repository contents, Actions secrets, and workflow-dispatch permission. The token will be encrypted before storage.', { replyMarkup: buttons([[{ text: 'Cancel', callback_data: 'flow:cancel' }]]) }, viewId);
  }
  if (kind === 'preview') return sendVoicePreview(env, chatId, value);
  if (kind === 'voice') {
    const voice = value;
    const meta = VOICES[voice];
    if (!meta) throw new Error('That narrator choice is unavailable.');
    const credentials = await requireCredentials(env, chatId); if (!credentials) return;
    await saveNarrator(credentials, credentials.repo, voice, meta.label);
    return renderInteractiveView(env, chatId, `${escapeHtml(meta.label)} is now the default Edge TTS narrator.`, { replyMarkup: buttons([[{ text: 'Back to settings', callback_data: 'menu:settings' }]]) }, viewId);
  }
  if (kind === 'dur') {
    const duration = Number(value); if (!TARGET_DURATIONS.includes(duration)) throw new Error('That output length is unavailable.');
    const state = await getState(env, chatId); if (!state.flow || !state.flow.endsWith('_duration')) throw new Error('Start a new task first.');
    state.pending.duration = duration; state.flow = `${state.pending.mode}_music`; await putState(env, chatId, state);
    return renderInteractiveView(env, chatId, 'Choose optional background music.', { replyMarkup: musicMenu() }, viewId);
  }
  if (kind === 'setup' && value === 'back') return setupBack(env, chatId, viewId);
  if (kind === 'music') {
    if (value === 'library') return chooseLibrary(env, chatId, viewId);
    if (value === 'back') return renderInteractiveView(env, chatId, 'Choose optional background music.', { replyMarkup: musicMenu() }, viewId);
    return selectMusic(env, chatId, value, viewId);
  }
  if (kind === 'status') return showTaskStatus(env, chatId, value, viewId);
  if (kind === 'done') return showCompleted(env, chatId, value, viewId);
  if (kind === 'torrent') return showTorrentCandidates(env, chatId, value, '0', viewId);
  if (kind === 'torrentpage') { const [label, page] = value.split(':'); return showTorrentCandidates(env, chatId, label, page, viewId); }
  if (kind === 'torrentdetail') { const [label, index, page] = value.split(':'); return showTorrentCandidateDetails(env, chatId, label, index, page, viewId); }
  if (kind === 'torrentpick') { const [label, index] = value.split(':'); return chooseTorrentCandidate(env, chatId, label, index, viewId); }
  if (kind === 'agent') return sendManualAgentPrompt(env, chatId, value, viewId);
  if (kind === 'plan') return startPlanFlow(env, chatId, value, viewId);
  if (kind === 'retry') { const [stage, label] = value.split(':'); return stage === 'a' ? restartStageA(env, chatId, label, viewId) : restartStageB(env, chatId, label, viewId); }
  if (kind === 'cancel') { if (value === 'abort') return renderInteractiveView(env, chatId, 'Stage B will continue.', { replyMarkup: mainMenu() }, viewId); if (value.startsWith('confirm:')) return cancelStageB(env, chatId, value.slice('confirm:'.length), viewId); return cancelStageBPrompt(env, chatId, value, viewId); }
  if (kind === 'flow' && value === 'cancel') { await clearFlow(env, chatId); return renderInteractiveView(env, chatId, 'Current flow cancelled.', { replyMarkup: mainMenu() }, viewId); }
}

async function handleUpdate(env, update) {
  if (!update || typeof update !== 'object') return;
  const callback = update.callback_query;
  const message = update.message;
  const chat = callback && callback.message && callback.message.chat || message && message.chat;
  const chatId = chat && chat.id;
  // Ignore non-private and structurally unsupported updates before allocating
  // a 24-hour deduplication key. These updates cannot enter the bot flow.
  if (!chatId || chat.type !== 'private') return;
  if (await markUpdateSeen(env, update.update_id)) return;
  if (callback) return handleCallback(env, callback);
  const command = commandOf(message.text);
  if (command) return handleCommand(env, chatId, command);
  if (message.video) return handleRelayVideo(env, chatId, message);
  if (message.document) return handleDocument(env, chatId, message.document, message);
  const priorState = await getState(env, chatId);
  if (message.text && await handleFlowText(env, chatId, message.text)) {
    if (['settings_github_pat', 'settings_shadow_pat', 'settings_gemini', 'settings_zernio_key'].includes(priorState.flow)) {
      await deleteMessage(env, chatId, message.message_id).catch(() => undefined);
    }
    return;
  }
  await renderInteractiveView(env, chatId, 'Use /start to open the ClipForge menu, or /cancel to leave the current flow.', { replyMarkup: mainMenu() });
}

function validWebhook(request, env) {
  const header = request.headers.get('X-Telegram-Bot-Api-Secret-Token');
  return Boolean(env.TELEGRAM_WEBHOOK_SECRET) && header === env.TELEGRAM_WEBHOOK_SECRET;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === 'GET' && url.pathname === '/health') return new Response('ok', { status: 200 });
    if (request.method !== 'POST' || url.pathname !== '/webhook') return new Response('Not found', { status: 404 });
    if (!validWebhook(request, env)) return new Response('Unauthorized', { status: 401 });
    let update;
    try { update = await request.json(); } catch { return new Response('Bad request', { status: 400 }); }
    try { await handleUpdate(env, update); }
    catch (error) {
      const chatId = update && ((update.message && update.message.chat && update.message.chat.id) || (update.callback_query && update.callback_query.message && update.callback_query.message.chat && update.callback_query.message.chat.id));
      if (chatId) {
        const respond = update && update.message ? renderTaskInputResponse : renderInteractiveView;
        await respond(env, chatId, `<b>ClipForge could not complete that step.</b>\n${escapeHtml(userError(error))}`, { replyMarkup: mainMenu() }).catch(() => undefined);
      }
    }
    return new Response('ok', { status: 200 });
  }
};

export const __test = { activeZernioAccounts, buildAgentHandoffPrompt, callbackMessageId, cloneOnboardingMenu, commandOf, defaultZernioSettings, disabledSocialSourceHost, existingGeminiLabel, formatBytes, formatStatus, hasResumablePendingTask, homeMenu, isPublicTelegramPost, isSafeAudioLibraryPath, permitsLegacyTelegramMtproto, preflightTelegramChannelPost, renderInteractiveView, renderTaskInputResponse, settingsMenu, taskCanBeDeleted, taskSetupFlowAfterSource, telegramButtonText, telegramPageIsGroup, torrentCandidateButtonText, validZernioDateTime, validZernioTime, validZernioTimezone, validateSource, normalizeFocus, mainMenu, durationMenu, taskButtons, userError, zernioSettingsMenu, zernioSettingsOrDefault, zernioTargets };
