/**
 * Bot A storage layer, split by backend (kv-minimization migration):
 *
 *   KV (CLIPFORGE_BOT_KV) — per-chat encrypted credentials + Bot A → Bot B
 *       relay staging records, and nothing else. kv-minimization phase 6
 *       removed the telegram:update:{id} webhook dedup marker: Bot A's
 *       webhook handlers are idempotent by construction instead, so a
 *       redelivered update is a safe no-op (see the phase-6 audit in
 *       MIGRATION_PROGRESS.json).
 *   D1 (CLIPFORGE_BOT_D1) — task labels/options, Shadow Clone job records
 *       (bug-51), and announcement markers.
 *
 * The legacy per-chat `state` record (flow/pending/currentTask/activeViewId)
 * is GONE (kv-minimization phase 5): menus and input flows are stateless
 * (ARCHITECTURE.md §8.9) — the current step rides the bot's own messages
 * as invisible markers, never a datastore row.
 */

import { decryptCredentials, encryptCredentials } from './crypto.js';

const RELAY_TTL_SECONDS = 12 * 60 * 60;

function key(chatId, suffix) {
  return `user:${String(chatId)}:${suffix}`;
}

export async function getCredentials(env, chatId) {
  const raw = await env.CLIPFORGE_BOT_KV.get(key(chatId, 'credentials'));
  return decryptCredentials(raw, chatId, env.KV_ENCRYPTION_KEY);
}

export async function putCredentials(env, chatId, credentials) {
  // bug-30: geminiKeys/pendingGeminiKey dropped — the Gemini mode is removed.
  const safe = {
    version: 1,
    githubPat: String(credentials.githubPat || ''),
    repo: String(credentials.repo || ''),
    pendingGithubPat: String(credentials.pendingGithubPat || '')
  };
  const encrypted = await encryptCredentials(safe, chatId, env.KV_ENCRYPTION_KEY);
  await env.CLIPFORGE_BOT_KV.put(key(chatId, 'credentials'), encrypted);
}

export async function deleteCredentials(env, chatId) {
  await env.CLIPFORGE_BOT_KV.delete(key(chatId, 'credentials'));
}

export async function putRelayJob(env, chatId, jobId, record) {
  const keyName = `relay:${String(chatId)}:${String(jobId)}`;
  const payload = { version: 1, job_id: String(jobId), chat_id: Number(chatId), ...(record || {}) };
  await env.CLIPFORGE_BOT_KV.put(keyName, JSON.stringify(payload), { expirationTtl: RELAY_TTL_SECONDS });
  return payload;
}

export async function getRelayJob(env, chatId, jobId) {
  const raw = await env.CLIPFORGE_BOT_KV.get(`relay:${String(chatId)}:${String(jobId)}`);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && parsed.version === 1 && parsed.job_id === String(jobId) && Number(parsed.chat_id) === Number(chatId) ? parsed : null;
  } catch {
    return null;
  }
}

// ------------------------------------------------------------------------ //
// Task labels + options (kv-minimization phase 1: D1, was user:<id>:tasks KV) //
// ------------------------------------------------------------------------ //
//
// The legacy monolithic tasks document (labels + options + the `next`
// high-water hint in one KV blob) is replaced by two D1 tables:
//   task_labels(chat_id, label, job_id)      PRIMARY KEY (chat_id, label)
//   task_options(chat_id, job_id, options_json) PRIMARY KEY (chat_id, job_id)
// Caller-visible behavior is preserved exactly; only the backend changes.

function nextLabel(index) {
  let n = index;
  let out = '';
  do {
    out = String.fromCharCode(65 + (n % 26)) + out;
    n = Math.floor(n / 26) - 1;
  } while (n >= 0);
  return out;
}

// Bug 2 fix: labels are no longer allocated from a monotonically increasing
// counter that never shrinks. The lowest currently-unused label is chosen, so
// a letter freed by a manual delete (or reclamation) is reused by the next new
// task instead of labels only ever growing forward. The legacy `next`
// high-water hint is gone with the KV blob — correctness never depended on
// it, and D1 makes the used-set query cheap and exact.
function lowestFreeLabelIndex(usedLabels) {
  const used = usedLabels instanceof Set ? usedLabels : new Set(usedLabels || []);
  let index = 0;
  while (used.has(nextLabel(index))) index += 1;
  return index;
}

export async function ensureTaskLabel(env, chatId, jobId) {
  const existing = await env.CLIPFORGE_BOT_D1.prepare(
    'SELECT label FROM task_labels WHERE chat_id = ? AND job_id = ?'
  ).bind(Number(chatId), String(jobId)).first();
  if (existing) return existing.label;
  const { results: rows } = await env.CLIPFORGE_BOT_D1.prepare(
    'SELECT label FROM task_labels WHERE chat_id = ?'
  ).bind(Number(chatId)).all();
  const label = nextLabel(lowestFreeLabelIndex(new Set(rows.map((row) => row.label))));
  await env.CLIPFORGE_BOT_D1.prepare(
    'INSERT INTO task_labels (chat_id, label, job_id) VALUES (?, ?, ?)'
  ).bind(Number(chatId), label, String(jobId)).run();
  return label;
}

export async function setTaskOptions(env, chatId, jobId, options) {
  const current = await getTaskOptions(env, chatId, jobId);
  const merged = { ...current, ...options };
  await env.CLIPFORGE_BOT_D1.prepare(
    `INSERT INTO task_options (chat_id, job_id, options_json) VALUES (?, ?, ?)
     ON CONFLICT (chat_id, job_id) DO UPDATE SET options_json = excluded.options_json`
  ).bind(Number(chatId), String(jobId), JSON.stringify(merged)).run();
}

export async function getTaskOptions(env, chatId, jobId) {
  const row = await env.CLIPFORGE_BOT_D1.prepare(
    'SELECT options_json FROM task_options WHERE chat_id = ? AND job_id = ?'
  ).bind(Number(chatId), String(jobId)).first();
  if (!row) return {};
  try {
    const parsed = JSON.parse(row.options_json);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

export async function getJobIdForLabel(env, chatId, label) {
  const row = await env.CLIPFORGE_BOT_D1.prepare(
    'SELECT job_id FROM task_labels WHERE chat_id = ? AND label = ?'
  ).bind(Number(chatId), String(label || '').toUpperCase()).first();
  return row ? row.job_id : null;
}

export async function removeTask(env, chatId, label, jobId) {
  const normalizedLabel = String(label || '').toUpperCase();
  if (!normalizedLabel) return false;
  const existing = await env.CLIPFORGE_BOT_D1.prepare(
    'SELECT job_id FROM task_labels WHERE chat_id = ? AND label = ?'
  ).bind(Number(chatId), normalizedLabel).first();
  if (!existing || existing.job_id !== jobId) return false;
  await env.CLIPFORGE_BOT_D1.batch([
    env.CLIPFORGE_BOT_D1.prepare('DELETE FROM task_labels WHERE chat_id = ? AND label = ?').bind(Number(chatId), normalizedLabel),
    env.CLIPFORGE_BOT_D1.prepare('DELETE FROM task_options WHERE chat_id = ? AND job_id = ?').bind(Number(chatId), String(jobId)),
  ]);
  return true;
}

// kv-minimization phase 4: the unseen-task marker (Feature 4: markTaskSeen/
// isTaskSeen and the `seen` field in task options) is DELETED as a feature,
// not just as storage. There is intentionally no seen tracking anywhere now.

export async function taskLabels(env, chatId) {
  const { results: rows } = await env.CLIPFORGE_BOT_D1.prepare(
    'SELECT label, job_id FROM task_labels WHERE chat_id = ?'
  ).bind(Number(chatId)).all();
  return rows.map((row) => ({ label: row.label, jobId: row.job_id }));
}

// ------------------------------------------------------------------------ //
// Announcement markers (kv-minimization phase 3: D1, was per-kind KV keys)    //
// ------------------------------------------------------------------------ //
//
// One row per (chat_id, kind) in the announcements table, upsert on write —
// the same "announced exactly once per marker" semantics as the KV keys they
// replace. Kinds: 'update_notice' (bug-31), 'deploy_failure' (bug-68),
// 'news_notice' (bug-46). Getters return null when no row exists, matching
// the old KV.get() null-on-miss contract callers already rely on.

async function getAnnouncement(env, chatId, kind) {
  const row = await env.CLIPFORGE_BOT_D1.prepare(
    'SELECT marker FROM announcements WHERE chat_id = ? AND kind = ?'
  ).bind(Number(chatId), kind).first();
  return row ? row.marker : null;
}

async function setAnnouncement(env, chatId, kind, marker) {
  await env.CLIPFORGE_BOT_D1.prepare(
    `INSERT INTO announcements (chat_id, kind, marker) VALUES (?, ?, ?)
     ON CONFLICT (chat_id, kind) DO UPDATE SET marker = excluded.marker`
  ).bind(Number(chatId), kind, String(marker || '')).run();
}

// bug-31: the last announced clone-update marker (docs/update_notice.json's
// published_at). Stored per chat so a pushed update is announced exactly once.
export async function getAnnouncedUpdate(env, chatId) {
  return getAnnouncement(env, chatId, 'update_notice');
}

export async function setAnnouncedUpdate(env, chatId, marker) {
  await setAnnouncement(env, chatId, 'update_notice', marker);
}

// bug-68: the last deploy-failure marker announced to this chat. The marker
// is the failed Deploy Bots run's id, so a new failed run is announced even
// if it lands on the same commit as a previously announced one (rerun after
// fixing the cause must re-notify if it fails again).
export async function getAnnouncedDeployFailure(env, chatId) {
  return getAnnouncement(env, chatId, 'deploy_failure');
}

export async function setAnnouncedDeployFailure(env, chatId, marker) {
  await setAnnouncement(env, chatId, 'deploy_failure', marker);
}

// bug-46: the last announced news marker (docs/news.json's published_at) from
// the main account. Stored per chat so a pushed news message is announced
// exactly once per publication.
export async function getAnnouncedNews(env, chatId) {
  return getAnnouncement(env, chatId, 'news_notice');
}

export async function setAnnouncedNews(env, chatId, marker) {
  await setAnnouncement(env, chatId, 'news_notice', marker);
}

// ------------------------------------------------------------------------ //
// Shadow Clone job records (bug-51) — kv-minimization phase 2: D1-backed     //
// ------------------------------------------------------------------------ //

// bug-51: Shadow Clone creation can outlive any single Worker invocation
// (the copy is a GitHub Actions run; Cloudflare kills long-lived requests —
// which is exactly how the old flow stalled forever at "Preparing
// repository…" with no terminal message). The webhook therefore stages the
// in-flight creation as a job record and the cron trigger in index.js
// resumes it: poll the copy status, finalize on completion, or fail the job
// with a user-visible message.
//
// kv-minimization phase 2: the record moved from two KV keys
// (user:<chatId>:clonejob + the global clipforge:clone-jobs:index scan set)
// to one row in the D1 clone_jobs table. The PAT envelope is still AES-256-GCM
// encrypted with the chat's AAD via crypto.js — the encryption algorithm,
// envelope shape, and AAD are UNCHANGED; only the storage location moved.
// A row's existence IS the cron sweep's scan set now, so the separate index
// key and its maintenance are gone entirely: listCloneJobChatIds is a plain
// SELECT, and a lost delete can no longer wedge the sweep (there is nothing
// extra to lose).

const CLONE_JOB_COLUMNS =
  'chat_id, pat_envelope, repo, login, name, branch, source_sha, bootstrap_commit_sha, ' +
  'total_files, started_at, last_advance_at, last_status_key, run_id, finalize_failed_at';

export async function getCloneJob(env, chatId) {
  const record = await env.CLIPFORGE_BOT_D1.prepare(
    `SELECT ${CLONE_JOB_COLUMNS} FROM clone_jobs WHERE chat_id = ?`
  ).bind(Number(chatId)).first();
  if (!record) return null;
  const patEnvelope = typeof record.pat_envelope === 'string' ? record.pat_envelope : '';
  let pat = null;
  try { pat = patEnvelope ? await decryptCredentials(patEnvelope, chatId, env.KV_ENCRYPTION_KEY) : null; }
  catch { return null; } // corrupt/undecryptable record: treat as stale, the sweep prunes it
  const githubPat = pat && typeof pat.githubPat === 'string' ? pat.githubPat : '';
  if (!githubPat) return null;
  return {
    version: 1,
    chatId: Number(record.chat_id) || Number(chatId),
    githubPat,
    repo: String(record.repo || ''),
    login: String(record.login || ''),
    name: String(record.name || ''),
    branch: String(record.branch || 'main'),
    sourceSha: String(record.source_sha || ''),
    bootstrapCommitSha: String(record.bootstrap_commit_sha || ''),
    totalFiles: Number(record.total_files) || 0,
    startedAt: Number(record.started_at) || Date.now(),
    lastAdvanceAt: Number(record.last_advance_at) || Number(record.started_at) || Date.now(),
    lastStatusKey: String(record.last_status_key || ''),
    runId: record.run_id || null,
    finalizeFailedAt: Number(record.finalize_failed_at) || 0
  };
}

export async function putCloneJob(env, chatId, job) {
  const patEnvelope = await encryptCredentials({ version: 1, githubPat: String(job.githubPat || ''), repo: '', pendingGithubPat: '' }, chatId, env.KV_ENCRYPTION_KEY);
  const startedAt = Number(job.startedAt) || Date.now();
  await env.CLIPFORGE_BOT_D1.prepare(
    `INSERT INTO clone_jobs (${CLONE_JOB_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT (chat_id) DO UPDATE SET
       pat_envelope = excluded.pat_envelope,
       repo = excluded.repo,
       login = excluded.login,
       name = excluded.name,
       branch = excluded.branch,
       source_sha = excluded.source_sha,
       bootstrap_commit_sha = excluded.bootstrap_commit_sha,
       total_files = excluded.total_files,
       started_at = excluded.started_at,
       last_advance_at = excluded.last_advance_at,
       last_status_key = excluded.last_status_key,
       run_id = excluded.run_id,
       finalize_failed_at = excluded.finalize_failed_at`
  ).bind(
    Number(chatId),
    patEnvelope,
    String(job.repo || ''),
    String(job.login || ''),
    String(job.name || ''),
    String(job.branch || 'main'),
    String(job.sourceSha || ''),
    String(job.bootstrapCommitSha || ''),
    Number(job.totalFiles) || 0,
    startedAt,
    Number(job.lastAdvanceAt) || startedAt,
    String(job.lastStatusKey || ''),
    job.runId == null ? null : String(job.runId),
    Number(job.finalizeFailedAt) || 0
  ).run();
}

export async function deleteCloneJob(env, chatId) {
  await env.CLIPFORGE_BOT_D1.prepare(
    'DELETE FROM clone_jobs WHERE chat_id = ?'
  ).bind(Number(chatId)).run();
}

/** All chat ids with a staged Shadow Clone job (the cron sweep's scan set). */
export async function listCloneJobChatIds(env) {
  const { results: rows } = await env.CLIPFORGE_BOT_D1.prepare(
    'SELECT chat_id FROM clone_jobs'
  ).all();
  return rows.map((row) => Number(row.chat_id)).filter((id) => Number.isSafeInteger(id));
}

// ------------------------------------------------------------------------ //
// Awaiting-input marker (restore-bare-send-recognition)                      //
// ------------------------------------------------------------------------ //
// A MINIMAL, SCOPED, EXPIRING per-chat marker — deliberately NOT the legacy
// state.flow/pending blob. One row per chat, written ONLY when the bot sends
// a force_reply/flow-marker prompt (one write per prompt-sent — never per
// keystroke or per menu navigation, so this does not reintroduce the
// write-volume problem the kv-minimization migration solved). It lets a bare
// send or a forward at an input step route exactly like a genuine reply:
// parseFlowReply (the reply_to_message edge) remains the PRIMARY path and
// always wins when both signals exist; this table is the fallback only.
//
// op/payload reuse the flow.js marker vocabulary and encoding VERBATIM — the
// payload column stores the same 'cf:<op>:<arg…>' token the prompt message
// itself carries. This table is NOT an update-dedup mechanism (phase 6 stays
// removed) and must never serve double duty as one.

// 15 minutes. RELAY_TTL_SECONDS (12 h) is a relay-handoff convention — far
// too long for an input prompt. 15 min matches the implicit window the
// pre-migration session state (and every force_reply prompt) assumed: long
// enough for a user to find and forward a file, short enough that a stale
// row cannot hijack an unrelated message hours later.
export const AWAITING_INPUT_TTL_SECONDS = 15 * 60;

/** Upsert the chat's awaiting-input marker (one row per chat). */
export async function putAwaitingInput(env, chatId, op, payload, nowSeconds = Math.floor(Date.now() / 1000)) {
  await env.CLIPFORGE_BOT_D1.prepare(
    'INSERT OR REPLACE INTO awaiting_input (chat_id, op, payload, expires_at) VALUES (?, ?, ?, ?)'
  ).bind(
    Number(chatId),
    String(op || ''),
    payload == null ? null : String(payload),
    nowSeconds + AWAITING_INPUT_TTL_SECONDS
  ).run();
}

/**
 * Read the chat's LIVE awaiting-input marker, or null. An expired row is
 * deleted here (lazy sweep) and treated as absent — it is never honored.
 */
export async function getAwaitingInput(env, chatId, nowSeconds = Math.floor(Date.now() / 1000)) {
  const row = await env.CLIPFORGE_BOT_D1.prepare(
    'SELECT op, payload, expires_at FROM awaiting_input WHERE chat_id = ?'
  ).bind(Number(chatId)).first();
  if (!row) return null;
  if (Number(row.expires_at) <= nowSeconds) {
    await deleteAwaitingInput(env, chatId);
    return null;
  }
  return { op: String(row.op || ''), payload: row.payload == null ? null : String(row.payload) };
}

/** Clear the chat's awaiting-input marker (flow completed or cancelled). */
export async function deleteAwaitingInput(env, chatId) {
  await env.CLIPFORGE_BOT_D1.prepare(
    'DELETE FROM awaiting_input WHERE chat_id = ?'
  ).bind(Number(chatId)).run();
}

/**
 * Consume-on-success delete used by the awaiting_input READ path: removes
 * the row ONLY when it still holds the exact payload that was just handled.
 * When the handler advanced the flow, the sendForceReply choke point already
 * re-anchored the row to the next step's payload BEFORE this delete runs —
 * the condition then fails, the new row survives, and the flow's marker
 * stays live. When the handler completed without a new prompt, the payload
 * still matches and the stale row is deleted so it cannot double-fire.
 */
export async function deleteAwaitingInputIfUnconsumed(env, chatId, expectedPayload) {
  await env.CLIPFORGE_BOT_D1.prepare(
    'DELETE FROM awaiting_input WHERE chat_id = ? AND (payload IS ? OR payload = ?)'
  ).bind(Number(chatId), expectedPayload == null ? null : String(expectedPayload), expectedPayload == null ? null : String(expectedPayload)).run();
}

// --- plan upload fragment buffer — REMOVED (remove-paste-feature) -------- //
//
// The multi-bubble production.json paste-reassembly mechanism (and its D1
// fragment buffer, formerly migration 0003's plan_upload_buffer table) was
// deliberately deleted: the operator abandoned pasted-JSON reassembly after
// repeated fix rounds. The upload step is now file-upload-only (any
// UTF-8-decodable text file), validated by the unchanged
// parseAndValidateProductionPlan. Migration 0004 drops the table.
