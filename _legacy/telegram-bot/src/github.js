import nacl from 'tweetnacl';
import sealedbox from 'tweetnacl-sealedbox-js';
import {
  API_VERSION,
  DEFAULT_BRANCH,
  GEMINI_KEYS_META_PATH,
  GEMINI_SECRET_NAME,
  PRODUCTION_PATH,
  STATUS_PATH,
  STAGE_A_REQUEST_PATH,
  TTS_SETTINGS_PATH,
  WATERMARK_PATH,
  ZERNIO_ACCOUNTS_PATH,
  ZERNIO_SECRET_NAME,
  ZERNIO_SETTINGS_PATH
} from './constants.js';

const encoder = new TextEncoder();
const decoder = new TextDecoder();
// libsodium's crypto_box_seal adds one ephemeral Curve25519 public key plus
// crypto_box authentication overhead (32 + 16 bytes). Keep this explicit
// instead of reading a CommonJS package property: Workers' ESM interop exposes
// `seal` but not `overheadLength` on the default export, which previously made
// the length check compare against NaN and reject every secret.
const SEALED_BOX_OVERHEAD_BYTES = 48;

export const SHADOW_CLONE_SOURCE = 'motionssalt/clipforge';
const SHADOW_CLONE_EXCLUDES = [
  /^branding\//,
  /^jobs\//,
  /^audio-library\//,
  /keys/i,
  /accounts/i,
  /queue/i
];

function sourcePathAllowed(path) {
  const value = String(path || '');
  return Boolean(value) && !SHADOW_CLONE_EXCLUDES.some((pattern) => pattern.test(value));
}

function cloneRepositoryName(value) {
  const name = String(value || '').trim();
  if (!/^[A-Za-z0-9_.-]{1,100}$/.test(name) || name.toLowerCase().endsWith('.git')) {
    throw new Error('Shadow Clone repository name may contain letters, numbers, dots, hyphens, and underscores only.');
  }
  return name;
}

export class GitHubError extends Error {
  constructor(status, message, body = null) {
    super(message);
    this.name = 'GitHubError';
    this.status = status;
    this.body = body;
  }
}

function parseRepo(repo) {
  const match = /^([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)$/.exec(String(repo || '').trim());
  if (!match) throw new Error('Repository must use the exact format owner/repository.');
  return { owner: match[1], name: match[2] };
}

function encodePath(path) {
  return String(path).split('/').map(encodeURIComponent).join('/');
}

function b64encode(value) {
  const bytes = encoder.encode(value);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

function b64FromBytes(bytes) {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

function b64decode(value) {
  const binary = atob(String(value || '').replace(/\n/g, ''));
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  return decoder.decode(bytes);
}

function bytesFromB64(value) {
  const binary = atob(String(value || '').replace(/\n/g, ''));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

async function downloadPrivateAsset(credentials, url, maximumBytes, description) {
  if (!/^https:\/\//.test(String(url || ''))) throw new Error(`Invalid ${description} download URL.`);
  const response = await fetch(url, {
    headers: {
      Accept: 'application/octet-stream',
      Authorization: `Bearer ${credentials.githubPat}`,
      'User-Agent': 'ClipForge-Telegram-Bot/1.0',
      'X-GitHub-Api-Version': API_VERSION
    }
  });
  if (!response.ok) throw new GitHubError(response.status, `GitHub could not download the ${description}.`);
  const length = Number(response.headers.get('content-length') || 0);
  if (Number.isFinite(length) && length > maximumBytes) throw new Error(`${description} is too large to send through Telegram.`);
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (!bytes.length || bytes.length > maximumBytes) throw new Error(`${description} is missing or too large to send through Telegram.`);
  return bytes;
}

export async function githubRequest(credentials, path, options = {}) {
  const url = path.startsWith('http') ? path : `https://api.github.com${path}`;
  const response = await fetch(url, {
    method: options.method || 'GET',
    headers: {
      Accept: options.accept || 'application/vnd.github+json',
      Authorization: `Bearer ${credentials.githubPat}`,
      'User-Agent': 'ClipForge-Telegram-Bot/1.0',
      'X-GitHub-Api-Version': API_VERSION,
      ...(options.body === undefined ? {} : { 'Content-Type': 'application/json' }),
      ...(options.headers || {})
    },
    body: options.body === undefined ? undefined : JSON.stringify(options.body)
  });
  if (response.status === 204 || response.status === 205) return null;
  const raw = await response.text();
  let body = null;
  try { body = raw ? JSON.parse(raw) : null; } catch { body = raw; }
  if (!response.ok) {
    const message = body && typeof body === 'object' && body.message ? body.message : `GitHub API returned HTTP ${response.status}.`;
    throw new GitHubError(response.status, message, body);
  }
  return body;
}

export async function getGitHubIdentity(pat) {
  const credentials = { githubPat: String(pat), repo: '', geminiKeys: [] };
  const user = await githubRequest(credentials, '/user');
  if (!user || !user.login) throw new Error('GitHub did not return an account name for this token.');
  return { login: String(user.login) };
}

export async function validateConnection(pat, repo) {
  const credentials = { githubPat: String(pat), repo: String(repo), geminiKeys: [] };
  const user = await githubRequest(credentials, '/user');
  const spec = parseRepo(repo);
  const repository = await githubRequest(credentials, `/repos/${encodeURIComponent(spec.owner)}/${encodeURIComponent(spec.name)}`);
  if (!repository || repository.permissions && repository.permissions.push === false) {
    throw new Error('The GitHub token can read this repository but cannot write to it.');
  }
  return { login: user.login || '', repo: `${spec.owner}/${spec.name}`, private: !!repository.private };
}

export async function createPrivateShadowClone(pat, requestedName) {
  const name = cloneRepositoryName(requestedName);
  const identity = await getGitHubIdentity(pat);
  const credentials = { githubPat: String(pat), repo: '', geminiKeys: [] };
  const [sourceOwner, sourceName] = SHADOW_CLONE_SOURCE.split('/');

  const sourceRef = await githubRequest(credentials, `/repos/${encodeURIComponent(sourceOwner)}/${encodeURIComponent(sourceName)}/git/ref/heads/${encodeURIComponent(DEFAULT_BRANCH)}`);
  const sourceCommitSha = sourceRef && sourceRef.object && sourceRef.object.sha;
  if (!sourceCommitSha) throw new Error('Could not resolve the current ClipForge source revision.');
  const sourceCommit = await githubRequest(credentials, `/repos/${encodeURIComponent(sourceOwner)}/${encodeURIComponent(sourceName)}/git/commits/${encodeURIComponent(sourceCommitSha)}`);
  const sourceTreeSha = sourceCommit && sourceCommit.tree && sourceCommit.tree.sha;
  if (!sourceTreeSha) throw new Error('Could not resolve the ClipForge source file tree.');
  const sourceTree = await githubRequest(credentials, `/repos/${encodeURIComponent(sourceOwner)}/${encodeURIComponent(sourceName)}/git/trees/${encodeURIComponent(sourceTreeSha)}?recursive=1`);
  if (sourceTree && sourceTree.truncated) throw new Error('The ClipForge source tree is too large to clone safely.');
  const files = Array.isArray(sourceTree && sourceTree.tree)
    ? sourceTree.tree.filter((entry) => entry && entry.type === 'blob' && sourcePathAllowed(entry.path))
    : [];
  if (!files.length) throw new Error('The ClipForge source tree did not contain any cloneable files.');

  let target;
  try {
    target = await githubRequest(credentials, '/user/repos', {
      method: 'POST',
      body: {
        name,
        private: true,
        description: 'Private ClipForge Shadow Clone',
        auto_init: false
      }
    });
  } catch (error) {
    if (error instanceof GitHubError && error.status === 422) {
      throw new Error('A repository with that name already exists in your account. Use “Connect existing clone” instead, or choose a new name.');
    }
    throw error;
  }
  const repo = target && target.full_name ? String(target.full_name) : `${identity.login}/${name}`;
  const targetCredentials = { ...credentials, repo };
  const entries = [];
  const copyChunkSize = 4;
  for (let index = 0; index < files.length; index += copyChunkSize) {
    const chunk = files.slice(index, index + copyChunkSize);
    const copied = await Promise.all(chunk.map(async (file) => {
      const sourceBlob = await githubRequest(credentials, `/repos/${encodeURIComponent(sourceOwner)}/${encodeURIComponent(sourceName)}/git/blobs/${encodeURIComponent(file.sha)}`);
      if (!sourceBlob || sourceBlob.encoding !== 'base64' || typeof sourceBlob.content !== 'string') throw new Error(`Could not read source file ${file.path}.`);
      const targetBlob = await githubRequest(targetCredentials, `/repos/${encodeURIComponent(identity.login)}/${encodeURIComponent(name)}/git/blobs`, {
        method: 'POST', body: { content: sourceBlob.content.replace(/\n/g, ''), encoding: 'base64' }
      });
      if (!targetBlob || !targetBlob.sha) throw new Error(`Could not copy source file ${file.path}.`);
      return { path: file.path, mode: file.mode || '100644', type: 'blob', sha: targetBlob.sha };
    }));
    entries.push(...copied);
  }
  const sync = { source: SHADOW_CLONE_SOURCE, synced_sha: sourceCommitSha, synced_at: new Date().toISOString() };
  const syncBlob = await githubRequest(targetCredentials, `/repos/${encodeURIComponent(identity.login)}/${encodeURIComponent(name)}/git/blobs`, {
    method: 'POST', body: { content: b64encode(`${JSON.stringify(sync, null, 2)}\n`), encoding: 'base64' }
  });
  entries.push({ path: '.clipforge-sync.json', mode: '100644', type: 'blob', sha: syncBlob.sha });
  const tree = await githubRequest(targetCredentials, `/repos/${encodeURIComponent(identity.login)}/${encodeURIComponent(name)}/git/trees`, { method: 'POST', body: { tree: entries } });
  if (!tree || !tree.sha) throw new Error('Could not create the Shadow Clone file tree.');
  const commit = await githubRequest(targetCredentials, `/repos/${encodeURIComponent(identity.login)}/${encodeURIComponent(name)}/git/commits`, {
    method: 'POST', body: { message: `Shadow clone from ${SHADOW_CLONE_SOURCE}@${sourceCommitSha.slice(0, 7)}`, tree: tree.sha, parents: [] }
  });
  if (!commit || !commit.sha) throw new Error('Could not create the Shadow Clone commit.');
  await githubRequest(targetCredentials, `/repos/${encodeURIComponent(identity.login)}/${encodeURIComponent(name)}/git/refs`, {
    method: 'POST', body: { ref: `refs/heads/${DEFAULT_BRANCH}`, sha: commit.sha }
  });
  return { repo, login: identity.login, sourceSha: sourceCommitSha, copiedFiles: files.length };
}

export async function getContent(credentials, repo, path) {
  const { owner, name } = parseRepo(repo);
  return githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/contents/${encodePath(path)}?ref=${encodeURIComponent(DEFAULT_BRANCH)}`);
}

function parseJsonDocument(text, path) {
  try { return JSON.parse(text); }
  catch (error) {
    if (!/\/status\.json$/.test(String(path)) || !text.includes('<<<<<<<') || !text.includes('>>>>>>>')) {
      throw new Error(`${path} is not valid JSON.`);
    }
    const cleaned = text.split('\n').filter((line) => !/^(<<<<<<<|=======|>>>>>>>)/.test(line.trim())).join('\n').replace(/,(\s*[}\]])/g, '$1');
    try { return JSON.parse(cleaned); }
    catch { throw new Error(`${path} is not valid JSON.`); }
  }
}

export async function getJsonFile(credentials, repo, path) {
  const file = await getContent(credentials, repo, path);
  if (!file || typeof file.content !== 'string') throw new Error(`GitHub did not return ${path}.`);
  return { document: parseJsonDocument(b64decode(file.content), path), sha: file.sha || null };
}

export async function tryGetJsonFile(credentials, repo, path) {
  try { return await getJsonFile(credentials, repo, path); }
  catch (error) { if (error instanceof GitHubError && error.status === 404) return null; throw error; }
}

export async function putBinaryFile(credentials, repo, path, bytes, message) {
  const { owner, name } = parseRepo(repo);
  if (!(bytes instanceof Uint8Array) || !bytes.length) throw new Error('The binary upload is empty.');
  let sha = null;
  try {
    const existing = await getContent(credentials, repo, path);
    sha = existing && existing.sha ? existing.sha : null;
  } catch (error) {
    if (!(error instanceof GitHubError) || error.status !== 404) throw error;
  }
  return githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/contents/${encodePath(path)}`, {
    method: 'PUT',
    body: { message, content: b64FromBytes(bytes), branch: DEFAULT_BRANCH, ...(sha ? { sha } : {}) }
  });
}

export async function putTextFile(credentials, repo, path, text, message) {
  const { owner, name } = parseRepo(repo);
  let sha = null;
  try {
    const existing = await getContent(credentials, repo, path);
    sha = existing && existing.sha ? existing.sha : null;
  } catch (error) {
    if (!(error instanceof GitHubError) || error.status !== 404) throw error;
  }
  return githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/contents/${encodePath(path)}`, {
    method: 'PUT',
    body: { message, content: b64encode(text), branch: DEFAULT_BRANCH, ...(sha ? { sha } : {}) }
  });
}

export async function getRepositoryFileBytes(credentials, repo, path, maximumBytes = 10 * 1024 * 1024) {
  const file = await getContent(credentials, repo, path);
  if (file && typeof file.content === 'string' && file.encoding === 'base64') {
    const bytes = bytesFromB64(file.content);
    if (!bytes.length || bytes.length > maximumBytes) throw new Error(`${path} is missing or too large for Telegram delivery.`);
    return bytes;
  }
  if (file && typeof file.download_url === 'string') {
    return downloadPrivateAsset(credentials, file.download_url, maximumBytes, path);
  }
  throw new Error(`GitHub did not return binary content for ${path}.`);
}

export async function getReleaseTextAsset(credentials, url, maximumBytes = 1024 * 1024) {
  const bytes = await downloadPrivateAsset(credentials, url, maximumBytes, 'release text asset');
  return decoder.decode(bytes);
}

export async function listJobIds(credentials, repo) {
  const { owner, name } = parseRepo(repo);
  try {
    const listing = await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/contents/jobs?ref=${encodeURIComponent(DEFAULT_BRANCH)}`);
    if (!Array.isArray(listing)) return [];
    return listing.filter((item) => item && item.type === 'dir' && /^[A-Za-z0-9._-]+$/.test(item.name)).map((item) => item.name).sort().reverse();
  } catch (error) {
    if (error instanceof GitHubError && error.status === 404) return [];
    throw error;
  }
}

export async function readStatus(credentials, repo, jobId) {
  const result = await tryGetJsonFile(credentials, repo, STATUS_PATH(jobId));
  return result ? result.document : null;
}

function safeAudioLibraryPath(path) {
  return /^audio-library\/[^/\\\u0000-\u001f\u007f]+$/.test(String(path || ''));
}

function safeJobId(jobId) {
  return /^[A-Za-z0-9._-]{3,200}$/.test(String(jobId || ''));
}

async function deleteRepositoryFile(credentials, repo, path, message) {
  const { owner, name } = parseRepo(repo);
  const existing = await getContent(credentials, repo, path);
  if (!existing || !existing.sha) throw new Error(`GitHub could not resolve ${path} for deletion.`);
  return githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/contents/${encodePath(path)}`, {
    method: 'DELETE', body: { message, sha: existing.sha, branch: DEFAULT_BRANCH }
  });
}

export async function listAudioLibrary(credentials, repo) {
  const { owner, name } = parseRepo(repo);
  try {
    const listing = await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/contents/audio-library?ref=${encodeURIComponent(DEFAULT_BRANCH)}`);
    if (!Array.isArray(listing)) return [];
    return listing
      .filter((item) => item && item.type === 'file' && safeAudioLibraryPath(`audio-library/${item.name}`))
      .map((item) => ({ name: item.name, path: `audio-library/${item.name}`, size: Number(item.size) || 0, sha: String(item.sha || '') }))
      .sort((left, right) => left.name.localeCompare(right.name));
  } catch (error) {
    if (error instanceof GitHubError && error.status === 404) return [];
    throw error;
  }
}

export async function saveMusicDefault(credentials, repo, trackPath) {
  if (!safeAudioLibraryPath(trackPath)) throw new Error('Default music must be a track in audio-library/.');
  const document = {
    version: 1,
    library_track_path: trackPath,
    updated_at_epoch: Math.floor(Date.now() / 1000),
    note: 'Last explicitly selected audio-library track. One-off job uploads are never stored as the default.'
  };
  return putTextFile(credentials, repo, 'branding/music_default.json', `${JSON.stringify(document, null, 2)}\n`, 'clipforge: save default background music');
}

export async function deleteAudioLibraryTrack(credentials, repo, trackPath) {
  if (!safeAudioLibraryPath(trackPath)) throw new Error('Only a safe audio-library track may be deleted.');
  return deleteRepositoryFile(credentials, repo, trackPath, `clipforge: remove ${trackPath.slice('audio-library/'.length)} from audio library`);
}

export async function clearMusicDefaultIfTrack(credentials, repo, trackPath) {
  if (!safeAudioLibraryPath(trackPath)) throw new Error('Only a safe audio-library track may clear the default.');
  const current = await tryGetJsonFile(credentials, repo, 'branding/music_default.json');
  if (!current || !current.document || current.document.library_track_path !== trackPath) return false;
  await deleteRepositoryFile(credentials, repo, 'branding/music_default.json', 'clipforge: clear deleted default background music');
  return true;
}

function missingReleaseArtifact(error) {
  return error instanceof GitHubError && (
    error.status === 404 ||
    (error.status === 422 && /reference does not exist/i.test(String(error.message || '')))
  );
}

async function deleteReleaseAndTag(credentials, repo, tag) {
  const { owner, name } = parseRepo(repo);
  try {
    const release = await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/releases/tags/${encodeURIComponent(tag)}`);
    if (release && Number.isInteger(Number(release.id))) {
      await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/releases/${Number(release.id)}`, { method: 'DELETE' });
    }
  } catch (error) {
    if (!missingReleaseArtifact(error)) throw error;
  }
  try {
    await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/git/refs/tags/${encodeURIComponent(tag)}`, { method: 'DELETE' });
  } catch (error) {
    if (!missingReleaseArtifact(error)) throw error;
  }
}

export async function deleteClipforgeJob(credentials, repo, jobId) {
  if (!safeJobId(jobId)) throw new Error('That task identifier is invalid.');
  const { owner, name } = parseRepo(repo);
  const branch = await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/git/ref/heads/${encodeURIComponent(DEFAULT_BRANCH)}`);
  const commitSha = branch && branch.object && branch.object.sha;
  if (!commitSha) throw new Error('GitHub could not resolve the clone branch before task cleanup.');
  const commit = await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/git/commits/${encodeURIComponent(commitSha)}`);
  const treeSha = commit && commit.tree && commit.tree.sha;
  if (!treeSha) throw new Error('GitHub could not resolve the clone file tree before task cleanup.');
  const tree = await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/git/trees/${encodeURIComponent(treeSha)}?recursive=1`);
  if (tree && tree.truncated) throw new Error('The clone file tree is too large to safely delete this task.');
  const prefix = `jobs/${jobId}/`;
  const paths = Array.isArray(tree && tree.tree)
    ? tree.tree.filter((entry) => entry && entry.type === 'blob' && typeof entry.path === 'string' && entry.path.startsWith(prefix)).map((entry) => entry.path).sort()
    : [];
  await deleteReleaseAndTag(credentials, repo, `clipforge-${jobId}`);
  await deleteReleaseAndTag(credentials, repo, `clipforge-relay-input-${jobId}`);
  for (const path of paths) {
    await deleteRepositoryFile(credentials, repo, path, `clipforge: delete task ${jobId}`);
  }
  return { jobId, deletedFiles: paths.length, releaseTag: `clipforge-${jobId}`, relayReleaseTag: `clipforge-relay-input-${jobId}` };
}

export async function dispatchWorkflow(credentials, repo, workflow, inputs) {
  const { owner, name } = parseRepo(repo);
  await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`, {
    method: 'POST', body: { ref: DEFAULT_BRANCH, inputs }
  });
}

export async function cancelWorkflowRun(credentials, repo, runId) {
  const { owner, name } = parseRepo(repo);
  await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/actions/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' });
}

export async function currentBranchSha(credentials, repo) {
  const { owner, name } = parseRepo(repo);
  const branch = await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/branches/${encodeURIComponent(DEFAULT_BRANCH)}`);
  const sha = branch && branch.commit && branch.commit.sha;
  if (!sha) throw new Error(`Could not resolve the current ${DEFAULT_BRANCH} commit.`);
  return sha;
}

export async function getActionsPublicKey(credentials, repo) {
  const { owner, name } = parseRepo(repo);
  const response = await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/actions/secrets/public-key`);
  if (!response || !response.key || !response.key_id) throw new Error('GitHub did not return an Actions public key.');
  return response;
}

function sealForGitHub(value, publicKeyBase64) {
  const plaintext = encoder.encode(value);
  const publicKey = bytesFromB64(publicKeyBase64);
  const sealed = sealedbox.seal(plaintext, publicKey);
  if (!sealed || sealed.length !== plaintext.length + SEALED_BOX_OVERHEAD_BYTES) throw new Error('Could not encrypt the GitHub Actions secret.');
  return b64FromBytes(sealed);
}

export async function updateActionsSecret(credentials, repo, secretName, value) {
  const { owner, name } = parseRepo(repo);
  const publicKey = await getActionsPublicKey(credentials, repo);
  const encryptedValue = sealForGitHub(String(value || ''), publicKey.key);
  await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/actions/secrets/${encodeURIComponent(secretName)}`, {
    method: 'PUT', body: { encrypted_value: encryptedValue, key_id: publicKey.key_id }
  });
}

export async function actionsSecretExists(credentials, repo, secretName) {
  const { owner, name } = parseRepo(repo);
  try {
    await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/actions/secrets/${encodeURIComponent(secretName)}`);
    return true;
  } catch (error) {
    if (error instanceof GitHubError && error.status === 404) return false;
    throw error;
  }
}

export async function deleteActionsSecret(credentials, repo, secretName) {
  const { owner, name } = parseRepo(repo);
  try {
    await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/actions/secrets/${encodeURIComponent(secretName)}`, { method: 'DELETE' });
  } catch (error) {
    if (!(error instanceof GitHubError && error.status === 404)) throw error;
  }
}

export async function updateZernioSecret(credentials, repo, value) {
  return updateActionsSecret(credentials, repo, ZERNIO_SECRET_NAME, value);
}

export async function deleteZernioSecret(credentials, repo) {
  return deleteActionsSecret(credentials, repo, ZERNIO_SECRET_NAME);
}

export async function updateGeminiSecret(credentials, repo, geminiKeys) {
  return updateActionsSecret(credentials, repo, GEMINI_SECRET_NAME, geminiKeys.join('\n'));
}

export async function readGeminiMetadata(credentials, repo) {
  const result = await tryGetJsonFile(credentials, repo, GEMINI_KEYS_META_PATH);
  return result && Array.isArray(result.document.keys) ? result.document.keys : [];
}

export async function writeGeminiMetadata(credentials, repo, metaEntries) {
  const document = {
    version: 1,
    note: 'Masked fingerprints only. Raw keys live in the GEMINI_API_KEYS repo secret and are never committed.',
    keys: metaEntries,
    updated_at_epoch: Math.floor(Date.now() / 1000)
  };
  return putTextFile(credentials, repo, GEMINI_KEYS_META_PATH, `${JSON.stringify(document, null, 2)}\n`, 'clipforge: update Gemini API key metadata (masked fingerprints only)');
}

export async function readZernioSettings(credentials, repo) {
  const result = await tryGetJsonFile(credentials, repo, ZERNIO_SETTINGS_PATH);
  return result ? result.document : null;
}

export async function readZernioAccounts(credentials, repo) {
  const result = await tryGetJsonFile(credentials, repo, ZERNIO_ACCOUNTS_PATH);
  return result && Array.isArray(result.document.accounts) ? result.document.accounts : [];
}

export async function saveZernioSettings(credentials, repo, document) {
  return putTextFile(credentials, repo, ZERNIO_SETTINGS_PATH, `${JSON.stringify(document, null, 2)}\n`, 'clipforge: update Zernio publishing settings');
}

export function zernioFingerprint(value) {
  const raw = String(value || '').trim();
  return raw.length >= 9 ? `${raw.slice(0, 4)}…${raw.slice(-4)}` : 'invalid';
}

export async function readSeriesSettings(credentials, repo) {
  const result = await tryGetJsonFile(credentials, repo, 'branding/series_settings.json');
  return result ? result.document : null;
}

export async function saveSeriesSettings(credentials, repo, enabled) {
  const document = { version: 1, enabled: enabled === true, updated_at_epoch: Math.floor(Date.now() / 1000) };
  return putTextFile(credentials, repo, 'branding/series_settings.json', `${JSON.stringify(document, null, 2)}\n`, 'clipforge: update Series Mode setting');
}

export async function saveNarrator(credentials, repo, voice, label) {
  const document = { version: 1, engine: 'edge-tts', voice, voice_label: label, rate: '+20%', volume: '+0%', pitch: '+0Hz', updated_at_epoch: Math.floor(Date.now() / 1000) };
  return putTextFile(credentials, repo, TTS_SETTINGS_PATH, `${JSON.stringify(document, null, 2)}\n`, 'clipforge: save Edge TTS narrator');
}

export async function saveWatermark(credentials, repo, creatorName) {
  const document = { version: 1, creator_name: creatorName, updated_at_epoch: Math.floor(Date.now() / 1000) };
  return putTextFile(credentials, repo, WATERMARK_PATH, `${JSON.stringify(document, null, 2)}\n`, 'clipforge: update creator watermark');
}

export async function saveStageARequest(credentials, repo, jobId, inputs) {
  const document = {
    version: 2, job_id: jobId, video_url: String(inputs.video_url || ''),
    source_type: String(inputs.source_type || 'url'), relay_release_tag: String(inputs.relay_release_tag || ''),
    relay_expected_size: String(inputs.relay_expected_size || ''), relay_sha256: String(inputs.relay_sha256 || ''),
    torrent_file_index: String(inputs.torrent_file_index || ''),
    whisper_model: String(inputs.whisper_model || 'base'), language: String(inputs.language || 'auto'),
    target_duration_seconds: String(inputs.target_duration_seconds || '120'), focus: String(inputs.focus || ''),
    automatic_mode: inputs.automatic_mode === 'true' ? 'true' : 'false',
    series_mode: inputs.series_mode === 'true' ? 'true' : 'false', series_id: String(inputs.series_id || ''),
    series_source_job_id: String(inputs.series_source_job_id || ''), series_part: String(inputs.series_part || ''),
    series_start_seconds: String(inputs.series_start_seconds || ''), series_context: String(inputs.series_context || ''),
    saved_at_epoch: Math.floor(Date.now() / 1000)
  };
  return putTextFile(credentials, repo, STAGE_A_REQUEST_PATH(jobId), `${JSON.stringify(document, null, 2)}\n`, `clipforge: save Stage A restart settings for job ${jobId}`);
}

export async function saveAutomaticMusicChoice(credentials, repo, jobId, musicRef, source) {
  const document = { version: 1, job_id: jobId, music_ref: musicRef, source, saved_at_epoch: Math.floor(Date.now() / 1000) };
  return putTextFile(credentials, repo, `jobs/${jobId}/automatic_music.json`, `${JSON.stringify(document, null, 2)}\n`, `clipforge: save Automatic Mode music choice for ${jobId}`);
}

export async function readStageARequest(credentials, repo, jobId) {
  const result = await getJsonFile(credentials, repo, STAGE_A_REQUEST_PATH(jobId));
  return result.document;
}

export async function readProductionPlan(credentials, repo, jobId) {
  // Absence is meaningful (a job without an uploaded plan is not an error),
  // so use the null-on-404 reader instead of the throwing getJsonFile.
  const result = await tryGetJsonFile(credentials, repo, PRODUCTION_PATH(jobId));
  return result ? result.document : null;
}

export async function saveProductionPlan(credentials, repo, jobId, jsonText) {
  return putTextFile(credentials, repo, PRODUCTION_PATH(jobId), jsonText, `clipforge: upload production.json for job ${jobId}`);
}

export function geminiFingerprint(value) {
  const raw = String(value || '').trim();
  return raw.length >= 9 ? `${raw.slice(0, 4)}…${raw.slice(-4)}` : 'invalid';
}

export function makeJobId(mode, now = Date.now()) {
  return `${mode === 'automatic' ? 'automatic' : 'manual'}-${Number(now)}`;
}

export { b64decode, b64encode, cloneRepositoryName, parseJsonDocument, parseRepo, sourcePathAllowed };
