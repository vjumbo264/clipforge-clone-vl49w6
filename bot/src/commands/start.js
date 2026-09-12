/**
 * /start and /help — the §8.3 home screen (same as tapping "Menu").
 * Also owns the settings snapshot loader shared with commands/settings.js.
 */

import { getCredentials, getAnnouncedNews, setAnnouncedNews, getAnnouncedUpdate, setAnnouncedUpdate, getAnnouncedDeployFailure, setAnnouncedDeployFailure } from '../storage.js';
import {
  getRepositoryFileBytes, getRepositoryVisibility, listWorkflowRuns, readSeriesSettings, readZernioSettings, tryGetJsonFile
} from '../github.js';
import { isOriginalRepo } from '../identity.js';
import { checkCloneUpdates, applyCloneUpdates } from '../clone-sync.js';
import { MUSIC_DEFAULT_PATH, TTS_SETTINGS_PATH, WATERMARK_PATH } from '../constants.js';
import { sendMessage, sendVideoBytes } from '../telegram.js';
import { escapeHtml } from '../constants.js';

// bug-46: news broadcasts are published by the main account into the SOURCE
// repo at this path; every clone announces a new publication once per chat.
const NEWS_PATH = 'docs/news.json';
const NEWS_SOURCE_REPO = 'motionssalt/clipforge';
// bug-65: the changelog file the main account publishes into the SOURCE repo;
// its published_at marker is what tells a clone a new update has arrived.
const UPDATE_NOTICE_PATH = 'docs/update_notice.json';
// bug-68: the workflow whose failed runs the clone owner must hear about.
const DEPLOY_BOTS_WORKFLOW_FILE = 'deploy-bots.yml';

/** Announce the latest main-account news broadcast once per publication. */
async function announceNews(env, chatId, credentials) {
  try {
    const result = await tryGetJsonFile(credentials, NEWS_SOURCE_REPO, NEWS_PATH).catch(() => null);
    const doc = result && result.document && typeof result.document === 'object' ? result.document : null;
    const message = doc && String(doc.message || '').trim();
    const marker = doc && String(doc.published_at || '');
    if (!message || !marker) return;
    const announced = await getAnnouncedNews(env, chatId);
    if (announced === marker) return;
    await setAnnouncedNews(env, chatId, marker);
    await sendMessage(env, chatId, `\ud83d\udcf0 <b>News from ClipForge</b>\n\n${escapeHtml(message)}`);
  } catch { /* news is best-effort — never block the home screen */ }
}
import {
  ONBOARDING_TEXT, homeKeyboard, homeText, onboardingKeyboard, renderInteractiveView
} from '../views.js';

async function safeDoc(credentials, path) {
  try {
    const result = await tryGetJsonFile(credentials, credentials.repo, path);
    return result && result.document ? result.document : null;
  } catch {
    return null;
  }
}


// bug-65: updates the main account publishes from the source repo apply to a
// clone AUTOMATICALLY — there is no clone-side "Apply update" button anymore.
// The main account keeps its manual publish control (it decides WHEN an update
// goes out); the clone side applies it on receipt. Detection piggybacks on the
// home screen (the same passive touchpoint that announces news broadcasts): the
// source repo's docs/update_notice.json carries a published_at marker; when it
// advances past the marker stored in this chat, the pending update is applied
// immediately and the owner gets a passive after-the-fact changelog. Best-effort
// throughout — a sync failure never blocks the home screen; the marker is only
// stored after a successful apply, so a failed attempt retries on the next home
// screen render instead of being silently swallowed.
async function autoSyncFromSource(env, chatId, credentials) {
  try {
    const result = await tryGetJsonFile(credentials, NEWS_SOURCE_REPO, UPDATE_NOTICE_PATH).catch(() => null);
    const doc = result && result.document && typeof result.document === 'object' ? result.document : null;
    const marker = doc && String(doc.published_at || doc.version || '');
    if (!marker) return;
    const announced = await getAnnouncedUpdate(env, chatId);
    if (announced === marker) return;
    const plan = await checkCloneUpdates(credentials);
    if (plan.upToDate) {
      await setAnnouncedUpdate(env, chatId, marker);
      return;
    }
    const result2 = await applyCloneUpdates(credentials, plan);
    await setAnnouncedUpdate(env, chatId, marker);
    const previewLimit = 10;
    const preview = plan.changes.slice(0, previewLimit)
      .map((c) => `\u2022 <code>${escapeHtml(c.path)}</code> <i>${escapeHtml(c.status)}</i>`).join('\n');
    const more = plan.changes.length > previewLimit ? `\n\u2026and ${plan.changes.length - previewLimit} more` : '';
    const summary = doc && String(doc.summary || '').trim();
    await sendMessage(env, chatId,
      `\u2b06\ufe0f <b>Your clone was updated automatically</b>\n\n` +
      `From the main ClipForge account @ <code>${escapeHtml(plan.sourceSha.slice(0, 7))}</code>` +
      ` \u2014 commit <code>${escapeHtml((result2.commitSha || '').slice(0, 7))}</code>.\n` +
      `${result2.applied} file${result2.applied === 1 ? '' : 's'} updated:\n\n${preview}${more}\n\n` +
      (summary ? `${escapeHtml(summary)}\n\n` : '') +
      `Your per-clone paths (branding/, jobs/, audio-library/, key/account files) were excluded and preserved.`);
  } catch { /* auto-sync is best-effort — never block the home screen */ }
}

/**
 * bug-68: notify the clone owner when their own Deploy Bots run fails. The
 * workflow-side alert step (bug-58) posts from the runner with the central
 * BOTB_MTPROTO_BOT_TOKEN secret — but that secret exists ONLY on the main
 * account's repo (telegram-relay.yml is a preserved central-only subsystem,
 * ARCHITECTURE.md §9.2), so on every clone the alert step self-skips and a
 * blocked deploy is visible only in the Actions UI. Clones have no bot token
 * of their own (§10: one shared Bot A serves all users), so the fix cannot be
 * another repo secret. Instead the shared Bot A — which already DMs each
 * clone owner — polls the CONNECTED repo's Deploy Bots runs with the owner's
 * own PAT on the home-screen touchpoint (same passive-pull pattern as the
 * bug-46 news broadcast and bug-65 auto-update; the worker has no cron
 * trigger and the central account cannot push into a private clone). Each
 * failed run is announced exactly once, keyed on the run id so a rerun that
 * fails again still notifies. Marker is stored only after a successful send,
 * so a transient failure retries on the next home render instead of being
 * swallowed. Best-effort throughout — never blocks the home screen.
 */
async function announceDeployFailure(env, chatId, credentials) {
  try {
    const runs = await listWorkflowRuns(credentials, credentials.repo, DEPLOY_BOTS_WORKFLOW_FILE, 10);
    const failed = runs.filter((run) => run.status === 'completed' && run.conclusion === 'failure' && run.id);
    if (!failed.length) return;
    const announced = String(await getAnnouncedDeployFailure(env, chatId) || '');
    const unannounced = failed.filter((run) => String(run.id) !== announced);
    if (!unannounced.length) return;
    // Newest first (the API returns newest first): announce the latest failure.
    const run = unannounced[0];
    const shortSha = run.headSha ? run.headSha.slice(0, 7) : 'unknown';
    const url = run.htmlUrl || `https://github.com/${credentials.repo}/actions`;
    await sendMessage(env, chatId,
      `\u26a0\ufe0f <b>Deploy blocked on your clone</b>\n\n` +
      `The <b>Deploy Bots</b> run for commit <code>${escapeHtml(shortSha)}</code> failed; the Cloudflare Workers were NOT updated. ` +
      `This is expected on a brand-new clone until Cloudflare credentials are configured \u2014 otherwise check the run for the failing step:\n${escapeHtml(url)}`);
    await setAnnouncedDeployFailure(env, chatId, String(run.id));
  } catch { /* deploy-failure notice is best-effort — never block the home screen */ }
}

/** One batched read of the small branding/ JSONs behind the home + settings screens. */
export async function loadSnapshot(credentials) {
  const empty = {
    repo: '', narratorVoice: '', seriesEnabled: false,
    watermarkName: '', musicDefaultPath: '', zernioEnabled: false, repoPrivate: null
  };
  if (!credentials || !credentials.repo) return empty;
  // bug-49: repo visibility rides the same batched read so settingsText() can
  // show public/private; a failed read leaves repoPrivate null and the line
  // simply omits the visibility suffix.
  const [narrator, watermark, musicDefault, series, zernio, visibility] = await Promise.all([
    safeDoc(credentials, TTS_SETTINGS_PATH),
    safeDoc(credentials, WATERMARK_PATH),
    safeDoc(credentials, MUSIC_DEFAULT_PATH),
    readSeriesSettings(credentials, credentials.repo).catch(() => null),
    readZernioSettings(credentials, credentials.repo).catch(() => null),
    getRepositoryVisibility(credentials, credentials.repo).catch(() => null)
  ]);
  return {
    repo: credentials.repo,
    narratorVoice: narrator && narrator.voice ? String(narrator.voice) : '',
    seriesEnabled: Boolean(series && (series.enabled === true || (series.document && series.document.enabled === true))),
    watermarkName: watermark && watermark.creator_name ? String(watermark.creator_name) : '',
    musicDefaultPath: musicDefault && musicDefault.library_track_path ? String(musicDefault.library_track_path) : '',
    zernioEnabled: Boolean(zernio && (zernio.enabled === true || (zernio.document && zernio.document.enabled === true))),
    repoPrivate: visibility && typeof visibility.private === 'boolean' ? visibility.private : null
  };
}

export async function showHome(env, chatId, messageId = null) {
  const credentials = await getCredentials(env, chatId);
  if (!credentials || !credentials.githubPat || !credentials.repo) {
    return renderInteractiveView(env, chatId, ONBOARDING_TEXT, { replyMarkup: onboardingKeyboard() }, messageId);
  }
  const snapshot = await loadSnapshot(credentials);
  const view = await renderInteractiveView(env, chatId, homeText(snapshot), { replyMarkup: homeKeyboard() }, messageId);
  await announceNews(env, chatId, credentials);
  // bug-65: only a connected CLONE auto-syncs; the main account (connected to
  // the source repo itself) is already on the latest code and must never
  // rewrite its own repo from this path.
  if (!isOriginalRepo(env, credentials.repo)) {
    await autoSyncFromSource(env, chatId, credentials);
    // bug-68: clones are exactly where the workflow-side alert self-skips
    // (no BOTB_MTPROTO_BOT_TOKEN), so the owner poll lives on this branch.
    await announceDeployFailure(env, chatId, credentials);
  }
  return view;
}

/** /start is the home screen. */
export async function handleStart(env, chatId, messageId = null) {
  return showHome(env, chatId, messageId);
}

/**
 * /help — the in-app command reference (bug-06). The command surface had
 * drifted from the legacy bot with no current reference anywhere; /help now
 * answers with the actual current command list instead of the home screen.
 * It is intentionally a fresh message, never an edit of a prior view.
 */
export async function handleHelp(env, chatId) {
  const { HELP_TEXT, helpKeyboard, renderInteractiveView } = await import('../views.js');
  // bug-23: the in-app command reference now carries a link to the full
  // Telegraph user guide.
  return renderInteractiveView(env, chatId, HELP_TEXT, { replyMarkup: helpKeyboard() }, null);
}

// bug-67: the help tutorial video lives in the repository itself so every
// deployment (main account AND every Shadow Clone) serves the same file
// without any owner gating. The actual help.mp4 is added to assets/help/ by
// the maintainer; until then the button answers with a graceful fallback.
// NOTE: Telegram Bot API sendVideo is limited to 50 MB — help.mp4 must stay
// under that (we cap the read just below it).
const HELP_VIDEO_PATH = 'assets/help/help.mp4';
const HELP_VIDEO_MAX_BYTES = 49 * 1024 * 1024; // < Telegram's 50 MB sendVideo limit

export async function handleHelpVideo(env, chatId) {
  const credentials = await getCredentials(env, chatId).catch(() => null);
  const repo = credentials && credentials.githubPat && credentials.repo ? credentials.repo : null;
  if (repo) {
    try {
      const bytes = await getRepositoryFileBytes(credentials, repo, HELP_VIDEO_PATH, HELP_VIDEO_MAX_BYTES);
      return await sendVideoBytes(env, chatId, bytes, 'help.mp4',
        '\u{1F3AC} <b>ClipForge help tutorial</b>\n\nFull text guide: /help');
    } catch { /* fall through to the not-available message below */ }
  }
  return sendMessage(env, chatId,
    '\u{1F3AC} <b>Help tutorial</b>\n\nThe tutorial video is not available yet — it will be added as <code>assets/help/help.mp4</code> (max 50 MB for Telegram). In the meantime, use /help for the full written guide.',
    { replyMarkup: null });
}
