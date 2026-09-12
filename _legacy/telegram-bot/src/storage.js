import { decryptCredentials, encryptCredentials } from './crypto.js';

const STATE_TTL_SECONDS = 7 * 24 * 60 * 60;
const UPDATE_TTL_SECONDS = 24 * 60 * 60;
const RELAY_TTL_SECONDS = 12 * 60 * 60;

function key(chatId, suffix) {
  return `user:${String(chatId)}:${suffix}`;
}

export async function getState(env, chatId) {
  const raw = await env.CLIPFORGE_BOT_KV.get(key(chatId, 'state'));
  if (!raw) return { version: 1, flow: null, pending: {}, currentTask: null, activeViewId: null };
  try {
    const state = JSON.parse(raw);
    if (!state || state.version !== 1 || typeof state !== 'object') throw new Error('bad state');
    return {
      version: 1,
      flow: typeof state.flow === 'string' ? state.flow : null,
      pending: state.pending && typeof state.pending === 'object' ? state.pending : {},
      currentTask: typeof state.currentTask === 'string' ? state.currentTask : null,
      activeViewId: Number.isInteger(Number(state.activeViewId)) && Number(state.activeViewId) > 0 ? Number(state.activeViewId) : null
    };
  } catch {
    return { version: 1, flow: null, pending: {}, currentTask: null, activeViewId: null };
  }
}

export async function putState(env, chatId, state) {
  const stateKey = key(chatId, 'state');
  const serialized = JSON.stringify({
    version: 1,
    flow: state.flow || null,
    pending: state.pending || {},
    currentTask: state.currentTask || null,
    activeViewId: Number.isInteger(Number(state.activeViewId)) && Number(state.activeViewId) > 0 ? Number(state.activeViewId) : null
  });
  // View refreshes can revisit the exact same state. A read is far cheaper
  // than consuming a daily KV write quota, so persist only a real change.
  if (await env.CLIPFORGE_BOT_KV.get(stateKey) === serialized) return false;
  await env.CLIPFORGE_BOT_KV.put(stateKey, serialized, { expirationTtl: STATE_TTL_SECONDS });
  return true;
}

export async function clearFlow(env, chatId) {
  const state = await getState(env, chatId);
  state.flow = null;
  state.pending = {};
  await putState(env, chatId, state);
  return state;
}

export async function getCredentials(env, chatId) {
  const raw = await env.CLIPFORGE_BOT_KV.get(key(chatId, 'credentials'));
  return decryptCredentials(raw, chatId, env.KV_ENCRYPTION_KEY);
}

export async function putCredentials(env, chatId, credentials) {
  const safe = {
    version: 1,
    githubPat: String(credentials.githubPat || ''),
    repo: String(credentials.repo || ''),
    geminiKeys: Array.isArray(credentials.geminiKeys) ? credentials.geminiKeys.map(String) : [],
    pendingGithubPat: String(credentials.pendingGithubPat || ''),
    pendingGeminiKey: String(credentials.pendingGeminiKey || '')
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

export async function getTasks(env, chatId) {
  const raw = await env.CLIPFORGE_BOT_KV.get(key(chatId, 'tasks'));
  if (!raw) return { version: 1, next: 0, labels: {}, options: {} };
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || parsed.version !== 1 || !parsed.labels || typeof parsed.labels !== 'object') throw new Error('bad tasks');
    return { version: 1, next: Number(parsed.next) || 0, labels: parsed.labels, options: parsed.options && typeof parsed.options === 'object' ? parsed.options : {} };
  } catch {
    return { version: 1, next: 0, labels: {}, options: {} };
  }
}

function nextLabel(index) {
  let n = index;
  let out = '';
  do {
    out = String.fromCharCode(65 + (n % 26)) + out;
    n = Math.floor(n / 26) - 1;
  } while (n >= 0);
  return out;
}

export async function ensureTaskLabel(env, chatId, jobId) {
  const tasks = await getTasks(env, chatId);
  for (const [label, knownJobId] of Object.entries(tasks.labels)) {
    if (knownJobId === jobId) return label;
  }
  const label = nextLabel(tasks.next);
  tasks.next += 1;
  tasks.labels[label] = jobId;
  await env.CLIPFORGE_BOT_KV.put(key(chatId, 'tasks'), JSON.stringify(tasks));
  return label;
}

export async function setTaskOptions(env, chatId, jobId, options) {
  const tasks = await getTasks(env, chatId);
  tasks.options[jobId] = { ...(tasks.options[jobId] || {}), ...options };
  await env.CLIPFORGE_BOT_KV.put(key(chatId, 'tasks'), JSON.stringify(tasks));
}

export async function getTaskOptions(env, chatId, jobId) {
  const tasks = await getTasks(env, chatId);
  return tasks.options[jobId] || {};
}

export async function getJobIdForLabel(env, chatId, label) {
  const tasks = await getTasks(env, chatId);
  return tasks.labels[String(label || '').toUpperCase()] || null;
}

export async function removeTask(env, chatId, label, jobId) {
  const tasks = await getTasks(env, chatId);
  const normalizedLabel = String(label || '').toUpperCase();
  if (!normalizedLabel || tasks.labels[normalizedLabel] !== jobId) return false;
  delete tasks.labels[normalizedLabel];
  delete tasks.options[jobId];
  await env.CLIPFORGE_BOT_KV.put(key(chatId, 'tasks'), JSON.stringify(tasks));
  return true;
}

export async function taskLabels(env, chatId) {
  const tasks = await getTasks(env, chatId);
  return Object.entries(tasks.labels).map(([label, jobId]) => ({ label, jobId }));
}

export async function markUpdateSeen(env, updateId) {
  if (!Number.isInteger(updateId)) return false;
  const updateKey = `telegram:update:${updateId}`;
  const exists = await env.CLIPFORGE_BOT_KV.get(updateKey);
  if (exists) return true;
  await env.CLIPFORGE_BOT_KV.put(updateKey, '1', { expirationTtl: UPDATE_TTL_SECONDS });
  return false;
}
