/**
 * Bot A GitHub API layer (new architecture).
 *
 * Ported from _legacy/telegram-bot/src/github.js with two deliberate
 * adaptations to the new contracts:
 *
 *  1. saveStageARequest() writes the ARCHITECTURE.md §7.1 NESTED request shape
 *     (source/options/mode/series/music blocks per
 *     schemas/stage_a_request.schema.json), not the legacy flat v2 shape. The
 *     new stage-a.yml consumes only the nested shape.
 *  2. The separate automatic_music.json writer is gone — the music choice is
 *     carried inside stage-a-request.json and resolved by pipeline/plan/music.py.
 *
 * Everything else (shadow-clone copy with excludes, libsodium sealed Actions
 * secrets, release/job cleanup, dispatch helpers, branding readers/writers) is
 * preserved semantics. Secret VALUES never appear in logs or repo files — only
 * masked fingerprints are committed (branding/gemini_keys.json).
 */

import nacl from 'tweetnacl';
import sealedbox from 'tweetnacl-sealedbox-js';
import {
  API_VERSION,
  DEFAULT_BRANCH,
  GEMINI_KEYS_META_PATH,
  GEMINI_SECRET_NAME,
  MUSIC_DEFAULT_PATH,
  PRODUCTION_PATH,
  SERIES_SETTINGS_PATH,
  STAGE_A_REQUEST_PATH,
  STATUS_PATH,
  TTS_SETTINGS_PATH,
  WATERMARK_PATH,
  ZERNIO_ACCOUNTS_PATH,
  ZERNIO_SECRET_NAME,
  ZERNIO_SETTINGS_PATH,
  WHISPER_MODELS,
} from './constants.js';

void nacl; // sealedbox depends on the tweetnacl module being present; keep the import explicit for bundlers.

const encoder = new TextEncoder();
const decoder = new TextDecoder();
// libsodium's crypto_box_seal adds one ephemeral Curve25519 public key plus
// crypto_box authentication overhead (32 + 16 bytes). Keep this explicit
// instead of reading a CommonJS package property: Workers' ESM interop exposes
// `seal` but not `overheadLength` on the default export, which previously made
// the length check compare against NaN and reject every secret.
const SEALED_BOX_OVERHEAD_BYTES = 48;

export const SHADOW_CLONE_SOURCE = 'motionssalt/clipforge';
// bug-63: Shadow Clone creation no longer performs the bulk file copy inside
// the single Worker request (see beginShadowCloneCreation's header comment).
// The bootstrap commit ships this one-time workflow into the new repository;
// the bot dispatches it and polls CLONE_STATUS_PATH for progress.
export const CLONE_COPY_WORKFLOW = 'clone-copy.yml';
export const CLONE_STATUS_PATH = '.clipforge-clone-status.json';
// bug-51 poll budget (milliseconds). The budgets live in module scope because
// they are enforced in TWO separate processes: the webhook request that
// dispatches the copy workflow waits at most CLONE_COPY_FIRST_WAIT_MS inline
// (fast path only), after which the Worker's CRON TRIGGER
// (scheduled handler in index.js) resumes the job from KV and applies the
// stall/deadline checks below — no single Worker request is ever at risk of
// hitting the Cloudflare execution-time limit while waiting on GitHub (the
// killed-mid-loop scenario that made Shadow Clone creation stall forever at
// "Preparing repository…" with neither a success nor an error message).
export const CLONE_COPY_POLL_MS = 2000;      // time between status reads (inline fast-path only)
export const CLONE_COPY_FIRST_WAIT_MS = 30000; // inline wait for very fast runs; then cron takes over
export const CLONE_COPY_START_MS = 120000;   // workflow never wrote any status => never started
// Live-verified (bug-63): the paced publish step heartbeats every ~25 blob
// POSTs (~7s), so 6 minutes of silence means the run is genuinely dead.
export const CLONE_COPY_STALL_MS = 360000;   // status file stopped advancing => dead run
export const CLONE_COPY_DEADLINE_MS = 600000; // absolute ceiling for the copy
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

/**
 * bug-53: normalize any repo reference ("owner/name", mixed case, pasted
 * github.com URL, trailing ".git"/slash, surrounding whitespace) to the
 * canonical lowercase "owner/name" slug used by owner-identity comparisons.
 * Returns '' when the value cannot be a repo reference at all.
 */
export function normalizeRepoSlug(value) {
  let text = String(value || '').trim().toLowerCase();
  if (!text) return '';
  text = text.replace(/^https?:\/\/(?:www\.)?github\.com\//, '').replace(/^git@github\.com:/, '');
  text = text.split(/[?#]/)[0].replace(/\/+$/, '').replace(/\.git$/, '');
  const match = /^([a-z0-9_.-]+)\/([a-z0-9_.-]+)$/.exec(text);
  return match ? `${match[1]}/${match[2]}` : '';
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
  // bug-53: persist the API-canonical full_name (exact owner/name casing)
  // rather than the user-typed string, so the stored repo always matches the
  // owner-identity gate — same source of truth as the clone-creation path.
  const canonical = repository.full_name ? String(repository.full_name) : `${spec.owner}/${spec.name}`;
  return { login: user.login || '', repo: canonical, private: !!repository.private };
}

/**
 * bug-45: pick a repository name automatically when the user did not supply
 * one. Generates `clipforge-clone-<suffix>` candidates and returns the first
 * one that does not already exist on the account.
 */
async function autoCloneRepositoryName(credentials, login) {
  for (let attempt = 0; attempt < 6; attempt += 1) {
    const suffix = Math.random().toString(36).slice(2, 8);
    const candidate = `clipforge-clone-${suffix}`;
    try {
      await githubRequest(credentials, `/repos/${encodeURIComponent(login)}/${encodeURIComponent(candidate)}`);
      // 200 => name taken, try the next candidate.
    } catch (error) {
      if (error instanceof GitHubError && error.status === 404) return candidate;
      throw error;
    }
  }
  throw new Error('Could not find a free repository name automatically. Send a name yourself instead.');
}

// bug-51: Shadow Clone creation is split across THREE entry points so that no
// single Worker request ever waits on the (multi-minute) GitHub Actions copy
// run — awaiting that run inside the webhook was the mechanism behind the
// reported "stuck forever at Preparing repository…" symptom: Cloudflare kills
// a Workers invocation that outlives its wall-clock budget, which terminates
// the fetch handler BEFORE either the success message or the catch-path error
// message in handleClonePatMessage could be sent (and the onProgress edits
// stop with it — a kill is not a rejection, so no catch anywhere can run).
//   beginShadowCloneCreation  — webhook request: create repo, bootstrap the
//                               sync marker + one-time copy workflow, dispatch
//                               it, then wait at most CLONE_COPY_FIRST_WAIT_MS
//                               inline for a very fast run. On timeout it
//                               returns { pending: true } and the caller
//                               stages the job in KV (index.js#stageCloneJob).
//   pollShadowCloneJob        — cron tick: read .clipforge-clone-status.json
//                               once, apply the stall/deadline guards, and on
//                               completion run finalizeShadowClone (below).
//   finalizeShadowClone       — the post-copy tail the workflow cannot do with
//                               its GITHUB_TOKEN (copy .github/workflows/* via
//                               the Contents API with the user's PAT, delete
//                               the one-time workflow, normalize the default
//                               branch, verify the tree). Same work the bug-63
//                               inline tail did, just callable from cron.
//   cancelShadowCloneRun      — best-effort cancel of a dead/stalled copy run
//                               so a failed clone never keeps an Actions run
//                               (and its minutes) burning after we give up.
export async function beginShadowCloneCreation(pat, requestedName, options = {}) {
  // bug-48: optional progress reporter ({stage, done, total}) so the bot can
  // keep a live status message while the clone is being built. Reporting is
  // strictly best-effort: a failing callback must never break the clone.
  const report = async (stage, done, total) => {
    if (typeof options.onProgress !== 'function') return;
    try { await options.onProgress({ stage, done: Number(done) || 0, total: Number(total) || 0 }); }
    catch { /* progress display is best-effort */ }
  };
  const identity = await getGitHubIdentity(pat);
  const credentials = { githubPat: String(pat), repo: '', geminiKeys: [] };
  // bug-45: an empty/blank requestedName means "choose for me" (zero-setup
  // cloning). Resolve a free name BEFORE creating anything so the flow never
  // leaves a half-created repo behind on a naming collision.
  const name = String(requestedName || '').trim()
    ? cloneRepositoryName(requestedName)
    : await autoCloneRepositoryName(credentials, identity.login);
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
  await report('source', 0, files.length);

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
    // bug-45: repo creation is gated on the PAT's Administration permission.
    // A 403/404 here almost always means the token cannot create repositories
    // — surface that plainly instead of the raw API message.
    if (error instanceof GitHubError && (error.status === 403 || error.status === 404)) {
      throw new Error('The token could not create a repository on your account. A classic PAT needs the “repo” scope; a fine-grained PAT needs “Administration” (write) access.');
    }
    throw error;
  }
  const repo = target && target.full_name ? String(target.full_name) : `${identity.login}/${name}`;
  const targetCredentials = { ...credentials, repo };

  // bug-46: GitHub's Git Data API (POST .../git/blobs, .../git/trees,
  // .../git/commits) returns HTTP 409 "Git Repository is empty." for every
  // call against a repo that has zero refs. auto_init:false + a straight
  // blobs/trees/commits/refs sequence is therefore unreachable on a freshly
  // created target — the very first POST /git/blobs fails, and the raw
  // GitHub message was previously bubbled to the user by handleClonePatMessage
  // as a misleading "check your PAT scopes" hint. The Contents API
  // (PUT .../contents/<path>) DOES work on an empty repo and creates the
  // first commit + refs/heads/<branch> in one call, so we use it to
  // bootstrap the ref with the sync marker before switching to the Git Data
  // API (which is faster for the bulk file copy).
  //
  // bug-47: the bootstrap PUT must land on the branch that will actually be
  // the repo's DEFAULT branch. POST /user/repos returns `default_branch` (the
  // branch GitHub will set when the first commit arrives), but an
  // account-level "default branch name" preference can change between repo
  // creation and the bootstrap commit, so the authoritative branch is read
  // back from the repo AFTER the bootstrap commit exists. Hard-coding
  // DEFAULT_BRANCH here previously let the bootstrap commit land on one ref
  // (e.g. refs/heads/master, because that account prefers "master") while the
  // final PATCH fast-forwarded another (refs/heads/main) — GitHub then shows
  // the bootstrap-only ref as the repo's default view, which is exactly the
  // reported symptom: "the clone contains only .clipforge-sync.json".
  const initialBranch = target && target.default_branch ? String(target.default_branch) : DEFAULT_BRANCH;
  const sync = { source: SHADOW_CLONE_SOURCE, synced_sha: sourceCommitSha, synced_at: new Date().toISOString() };
  const syncPayload = `${JSON.stringify(sync, null, 2)}\n`;
  const cloneCopyWorkflowPayload = `${buildCloneCopyWorkflowYaml()}\n`;
  let bootstrap;
  try {
    bootstrap = await githubRequest(targetCredentials, `/repos/${encodeURIComponent(identity.login)}/${encodeURIComponent(name)}/contents/.clipforge-sync.json`, {
      method: 'PUT',
      body: {
        message: `Initialize Shadow Clone from ${SHADOW_CLONE_SOURCE}@${sourceCommitSha.slice(0, 7)}`,
        content: b64encode(syncPayload),
        branch: initialBranch
      }
    });
    // bug-63: ship the one-time copy workflow alongside the sync marker in the
    // bootstrap commit. The Contents API PUT above uses the single-file form,
    // which rejects a `tree` array, so the workflow is a second Contents PUT on
    // the same ref — still one or two round-trips total, and both land on the
    // branch POST /user/repos announced (the live default branch is resolved
    // below, after the ref exists). If this second PUT fails, the repo already
    // exists; surface the failure so the user can pick a new name instead of
    // leaving a copy-less clone behind.
    await githubRequest(targetCredentials, `/repos/${encodeURIComponent(identity.login)}/${encodeURIComponent(name)}/contents/.github/workflows/${encodeURIComponent(CLONE_COPY_WORKFLOW)}`, {
      method: 'PUT',
      body: {
        message: 'clipforge: install one-time Shadow Clone copy workflow',
        content: b64encode(cloneCopyWorkflowPayload),
        branch: initialBranch
      }
    });
  } catch (error) {
    if (error instanceof GitHubError) {
      throw new Error(`GitHub could not initialize the new repository: ${error.message}`);
    }
    throw error;
  }
  // bug-47: resolve the branch that actually became the repo's default from
  // the live repo object, not from our assumption. Immediately after the
  // bootstrap commit GitHub can still report the PLANNED default branch
  // (observed live: default_branch said "main" while only refs/heads/master
  // existed — it settled to "master" ~1s later), so the reported branch name
  // is only trusted once the matching ref actually exists. Every subsequent
  // ref/contents operation in this function uses the resolved value.
  let targetBranch = initialBranch;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    let liveBranch = targetBranch;
    try {
      const liveRepo = await githubRequest(targetCredentials, `/repos/${encodeURIComponent(identity.login)}/${encodeURIComponent(name)}`);
      if (liveRepo && liveRepo.default_branch) liveBranch = String(liveRepo.default_branch);
      const head = await githubRequest(targetCredentials, `/repos/${encodeURIComponent(identity.login)}/${encodeURIComponent(name)}/git/ref/heads/${encodeURIComponent(liveBranch)}`);
      if (head && head.object && head.object.sha) {
        targetBranch = liveBranch;
        break;
      }
    } catch {
      // Reported branch has no ref yet — GitHub is still settling; retry.
    }
    targetBranch = liveBranch;
    if (attempt < 4) await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  const bootstrapCommitSha = bootstrap && bootstrap.commit && bootstrap.commit.sha;
  if (!bootstrapCommitSha) throw new Error('GitHub did not return a bootstrap commit for the new repository.');
  await report('copy', 0, files.length);

  // bug-63: the bulk copy is executed by the one-time Actions workflow that
  // was committed into the new repository above — NOT by this Worker request.
  // The previous implementation looped here: for every source file it fetched
  // the source blob and created a target blob (two fully-awaited GitHub API
  // round-trips per file, in chunks of 4), followed by tree creation, commit
  // creation, and a ref PATCH. For a repo of this size that is hundreds of
  // sequential requests inside ONE Worker invocation, which exceeds the
  // Cloudflare Workers execution budget — the Worker was killed mid-loop,
  // which is exactly why affected clones ended up containing ONLY
  // .clipforge-sync.json (the bootstrap commit is self-contained and survives
  // the kill) and why the progress message never advanced past 'Preparing
  // repository...'. The workflow performs the identical Git Data API
  // copy/tree/commit/PATCH sequence (via `git clone --filter=blob:none` of
  // the public source, so source blobs never pass through the API at all),
  // records progress into .clipforge-clone-status.json, and self-deletes on
  // success. The poll below reads that status file — the same
  // commit-a-status-file + bot-reads-it pattern the job pipeline already uses
  // (ARCHITECTURE.md §6). A clone that still sits on 'copying' for longer
  // than CLONE_COPY_STALL_MS with no status update is treated as a dead
  // run and surfaced as an error instead of spinning forever.
  // GitHub indexes new workflow files slightly after the push that creates
  // them; dispatching immediately can 404 with 'Workflow not found'. Retry a
  // few times before treating that as fatal.
  let dispatched = false;
  let dispatchError = null;
  for (let attempt = 0; attempt < 6 && !dispatched; attempt += 1) {
    try {
      await githubRequest(targetCredentials, `/repos/${encodeURIComponent(identity.login)}/${encodeURIComponent(name)}/actions/workflows/${encodeURIComponent(CLONE_COPY_WORKFLOW)}/dispatches`, {
        method: 'POST',
        body: {
          ref: targetBranch,
          inputs: {
            source_sha: sourceCommitSha,
            bootstrap_commit: bootstrapCommitSha,
            expected_files: String(files.length)
          }
        }
      });
      dispatched = true;
    } catch (error) {
      dispatchError = error;
      if (!(error instanceof GitHubError) || (error.status !== 404 && error.status !== 422)) throw error;
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  }
  if (!dispatched) {
    const detail = dispatchError && dispatchError.message ? ` (${dispatchError.message})` : '';
    throw new Error(`GitHub could not start the clone copy workflow${detail}. Connect to the repository anyway (Settings → GitHub clone → Connect existing clone) and use Sync from source to fill in the missing files.`);
  }
  // bug-51: bounded inline wait — fast path only. A copy run normally takes
  // minutes (runner startup alone is ~30-60s), so most creations leave this
  // loop via the `pending` return below; the cron handler then owns all
  // further waiting. The stall/deadline guards that used to live in this loop
  // moved to pollShadowCloneJob, which can actually outlive the copy run.
  const startedAt = Date.now();
  let lastProgressKey = '';
  try {
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, CLONE_COPY_POLL_MS));
      const status = await readCloneCopyStatus(targetCredentials, targetBranch);
      if (status) {
        const progressKey = `${status.state}:${status.done}:${status.total}`;
        if (progressKey !== lastProgressKey) {
          lastProgressKey = progressKey;
          if (status.state === 'copying') {
            await report('copy', status.done, status.total);
          } else if (status.state === 'finalizing') {
            await report('finalize', status.done || files.length, status.total || files.length);
          }
        }
        if (status.state === 'failed') {
          throw new Error(`The clone copy workflow failed${status.error ? `: ${status.error}` : '.'} Connect to the repository anyway (Settings → GitHub clone → Connect existing clone) and use Sync from source to fill in the missing files.`);
        }
        if (status.state === 'complete') break;
      }
      if (Date.now() - startedAt > CLONE_COPY_FIRST_WAIT_MS) {
        // Hand off to the cron trigger (index.js#scheduled). Everything the
        // cron-side poller needs to resume — including the workflow-run
        // discovery branch when the run-id lookup raced the dispatch — is
        // derivable from this returned record plus the stored PAT.
        return {
          pending: true,
          repo,
          login: identity.login,
          name,
          branch: targetBranch,
          sourceSha: sourceCommitSha,
          bootstrapCommitSha,
          totalFiles: files.length,
          startedAt,
          lastAdvanceAt: startedAt
        };
      }
    }
  } catch (error) {
    if (error instanceof GitHubError) {
      throw new Error(`GitHub rejected a write to the new repository (${error.status || 'unknown'}): ${error.message}`);
    }
    throw error;
  }
  return await finalizeShadowClone({
    githubPat: String(pat),
    repo,
    login: identity.login,
    name,
    branch: targetBranch,
    sourceSha: sourceCommitSha,
    bootstrapCommitSha,
    totalFiles: files.length,
    onProgress: options.onProgress
  });
}

/**
 * bug-51 (cron side, 1/3): ONE poll tick for an in-flight Shadow Clone
 * creation. `job` is the record beginShadowCloneCreation returned with
 * pending:true (stored in KV by index.js#stageCloneJob). Reads the copy
 * workflow's status file exactly once, so a cron invocation handles every
 * staged clone in bounded time.
 *
 * Returns:
 *   { status: 'running', job }   — still copying; `job` carries updated
 *                                  lastAdvanceAt/lastStatusKey and must be
 *                                  re-stored by the caller.
 *   { status: 'complete', job }  — copy run finished; job.runId is resolved.
 *   { status: 'failed', job, error } — terminal failure (workflow reported
 *                                  'failed', never started, stalled, or hit
 *                                  the absolute deadline). `job.runId` is
 *                                  set whenever a run could be identified so
 *                                  the caller can cancelShadowCloneRun it.
 */
export async function pollShadowCloneJob(job) {
  const credentials = { githubPat: String(job.githubPat), repo: String(job.repo) };
  const nextJob = { ...job };
  const status = await readCloneCopyStatus(credentials, String(job.branch));
  const now = Date.now();
  if (status) {
    const progressKey = `${status.state}:${status.done}:${status.total}`;
    if (progressKey !== String(job.lastStatusKey || '')) {
      nextJob.lastStatusKey = progressKey;
      nextJob.lastAdvanceAt = now;
    }
    if (status.state === 'failed') {
      nextJob.runId = await findCloneCopyRunId(credentials);
      return {
        status: 'failed', job: nextJob,
        error: new Error(`The clone copy workflow failed${status.error ? `: ${status.error}` : '.'}`)
      };
    }
    if (status.state === 'complete') {
      nextJob.runId = await findCloneCopyRunId(credentials);
      return { status: 'complete', job: nextJob };
    }
    // The absolute deadline precedes the stall check: a run whose status file
    // keeps "advancing" past the ceiling is still terminated (the old inline
    // loop checked the deadline outside the status branch for this reason).
    if (now - Number(job.startedAt || now) > CLONE_COPY_DEADLINE_MS) {
      nextJob.runId = await findCloneCopyRunId(credentials);
      return {
        status: 'failed', job: nextJob,
        error: new Error('The clone copy workflow took too long.')
      };
    }
    if (now - Number(job.lastAdvanceAt || job.startedAt || now) > CLONE_COPY_STALL_MS) {
      nextJob.runId = await findCloneCopyRunId(credentials);
      return {
        status: 'failed', job: nextJob,
        error: new Error('The clone copy workflow stopped reporting progress (the Actions run may have failed).')
      };
    }
    return { status: 'running', job: nextJob };
  }
  if (now - Number(job.startedAt || now) > CLONE_COPY_START_MS) {
    nextJob.runId = await findCloneCopyRunId(credentials);
    return {
      status: 'failed', job: nextJob,
      error: new Error('The clone copy workflow never started.')
    };
  }
  if (now - Number(job.startedAt || now) > CLONE_COPY_DEADLINE_MS) {
    nextJob.runId = await findCloneCopyRunId(credentials);
    return {
      status: 'failed', job: nextJob,
      error: new Error('The clone copy workflow took too long.')
    };
  }
  return { status: 'running', job: nextJob };
}

/**
 * bug-51: locate the one-time copy workflow's newest run on the clone, for
 * cancellation after a terminal failure. The dispatch response carries no run
 * id, and no list endpoint filters by workflow until GitHub has indexed the
 * brand-new workflow file (observed live on freshly created repos), so the
 * primary read is the repo-wide run list — the clone has exactly one workflow
 * at this point, so its newest run IS the copy run. The per-workflow list is
 * kept as a fallback. Returns null when nothing can be identified yet; the
 * caller simply retries on the next cron tick.
 */
async function findCloneCopyRunId(credentials) {
  const { owner, name } = parseRepo(credentials.repo);
  const readNewest = (body) => {
    const runs = body && Array.isArray(body.workflow_runs) ? body.workflow_runs : [];
    const run = runs.find((entry) => entry && entry.id && entry.event === 'workflow_dispatch') || runs[0];
    return run && run.id ? run.id : null;
  };
  try {
    const body = await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/actions/runs?per_page=10`);
    const runId = readNewest(body);
    if (runId) return runId;
  } catch { /* fall through to the per-workflow list */ }
  try {
    const body = await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/actions/workflows/${encodeURIComponent(CLONE_COPY_WORKFLOW)}/runs?per_page=10`);
    return readNewest(body);
  } catch {
    return null;
  }
}

/**
 * bug-51 (cron side, 2/3): cancel the copy workflow's run, best-effort. Used
 * when a staged clone is declared dead (failed/stalled/deadline) so a queued
 * or still-running Actions run does not keep burning minutes — or, worse,
 * complete LATER and mutate a repository the user was already told failed.
 * Never throws.
 */
export async function cancelShadowCloneRun(job) {
  if (!job || !job.runId || !job.githubPat || !job.repo) return;
  try {
    await cancelWorkflowRun({ githubPat: String(job.githubPat), repo: String(job.repo) }, String(job.repo), job.runId);
  } catch { /* best-effort */ }
}
/**
 * bug-51 (cron side, 3/3): everything that must happen AFTER the copy
 * workflow reports 'complete'. Called from the webhook fast-path (when the
 * copy finished inside CLONE_COPY_FIRST_WAIT_MS) and from the cron handler
 * for staged jobs. `job` needs: githubPat, repo, login, name, branch,
 * sourceSha, bootstrapCommitSha, totalFiles; onProgress is the same
 * best-effort reporter contract as beginShadowCloneCreation.
 */
export async function finalizeShadowClone(job) {
  const credentials = { githubPat: String(job.githubPat), repo: '' };
  const targetCredentials = { githubPat: String(job.githubPat), repo: String(job.repo) };
  const login = String(job.login);
  const name = String(job.name);
  const [sourceOwner, sourceName] = SHADOW_CLONE_SOURCE.split('/');
  let targetBranch = String(job.branch || DEFAULT_BRANCH);
  const bootstrapCommitSha = String(job.bootstrapCommitSha);
  const sourceCommitSha = String(job.sourceSha);
  const report = async (stage, done, total) => {
    if (typeof job.onProgress !== 'function') return;
    try { await job.onProgress({ stage, done: Number(done) || 0, total: Number(total) || 0 }); }
    catch { /* progress display is best-effort */ }
  };
  // Re-enumerate the source tree at the recorded revision: the file list is
  // far too large to store in the KV job record, and re-deriving it here is
  // exactly what the in-request implementation did with its `files` array.
  const sourceCommit = await githubRequest(credentials, `/repos/${encodeURIComponent(sourceOwner)}/${encodeURIComponent(sourceName)}/git/commits/${encodeURIComponent(sourceCommitSha)}`);
  const sourceTreeSha = sourceCommit && sourceCommit.tree && sourceCommit.tree.sha;
  if (!sourceTreeSha) throw new Error('Could not resolve the ClipForge source file tree.');
  const sourceTree = await githubRequest(credentials, `/repos/${encodeURIComponent(sourceOwner)}/${encodeURIComponent(sourceName)}/git/trees/${encodeURIComponent(sourceTreeSha)}?recursive=1`);
  const files = Array.isArray(sourceTree && sourceTree.tree)
    ? sourceTree.tree.filter((entry) => entry && entry.type === 'blob' && sourcePathAllowed(entry.path))
    : [];
  // bug-63: the run's GITHUB_TOKEN may not write .github/workflows/* by any
  // mechanism (push remote-rejected, Contents API 403 — both verified live),
  // so the copy workflow deliberately excludes the source's own workflow
  // files and never deletes itself. The BOT finishes both jobs here with the
  // user's PAT, whose workflow scope the onboarding prompt already requires —
  // the same authority the original in-Worker copy used for every file.
  const workflowFiles = files.filter((file) => /^\.github\/workflows\//.test(file.path) && file.path !== `.github/workflows/${CLONE_COPY_WORKFLOW}`);
  for (const file of workflowFiles) {
    const sourceBlob = await githubRequest(credentials, `/repos/${encodeURIComponent(sourceOwner)}/${encodeURIComponent(sourceName)}/git/blobs/${encodeURIComponent(file.sha)}`);
    if (!sourceBlob || sourceBlob.encoding !== 'base64' || typeof sourceBlob.content !== 'string') throw new Error(`Could not read source file ${file.path}.`);
    let existingSha = null;
    try {
      const existing = await githubRequest(targetCredentials, `/repos/${encodeURIComponent(login)}/${encodeURIComponent(name)}/contents/${encodePath(file.path)}?ref=${encodeURIComponent(targetBranch)}`);
      existingSha = existing && existing.sha ? existing.sha : null;
    } catch (error) {
      if (!(error instanceof GitHubError) || error.status !== 404) throw error;
    }
    await githubRequest(targetCredentials, `/repos/${encodeURIComponent(login)}/${encodeURIComponent(name)}/contents/${encodePath(file.path)}`, {
      method: 'PUT',
      body: {
        message: `clipforge: copy workflow file ${file.path}`,
        content: sourceBlob.content.replace(/\n/g, ''),
        branch: targetBranch,
        ...(existingSha ? { sha: existingSha } : {})
      }
    });
  }
  try {
    const workflowPath = `.github/workflows/${CLONE_COPY_WORKFLOW}`;
    const existing = await githubRequest(targetCredentials, `/repos/${encodeURIComponent(login)}/${encodeURIComponent(name)}/contents/${encodePath(workflowPath)}?ref=${encodeURIComponent(targetBranch)}`);
    if (existing && existing.sha) {
      await githubRequest(targetCredentials, `/repos/${encodeURIComponent(login)}/${encodeURIComponent(name)}/contents/${encodePath(workflowPath)}`, {
        method: 'DELETE',
        body: { message: 'clipforge: remove one-time clone copy workflow', sha: existing.sha, branch: targetBranch }
      });
    }
  } catch (error) {
    // Best-effort: a lingering one-time workflow is inert (workflow_dispatch
    // only, requires the exact bootstrap inputs) and must not fail an
    // otherwise complete clone.
    console.warn('clone copy workflow self-delete skipped:', String(error && error.message || error));
  }
  // The workflow already built the final tree on top of the bootstrap commit
  // and fast-forwarded the live default branch; the PAT writes above layered
  // the source workflow files on top. Read the branch head back for the
  // normalization + verification steps below.
  const headAfterCopy = await githubRequest(targetCredentials, `/repos/${encodeURIComponent(login)}/${encodeURIComponent(name)}/git/ref/heads/${encodeURIComponent(targetBranch)}`);
  const finalCommitSha = headAfterCopy && headAfterCopy.object && headAfterCopy.object.sha;
  if (!finalCommitSha || finalCommitSha === bootstrapCommitSha) {
    throw new Error('Shadow Clone verification failed: the copy workflow reported completion but the repository head did not advance past the bootstrap commit.');
  }
  const finalCommit = await githubRequest(targetCredentials, `/repos/${encodeURIComponent(login)}/${encodeURIComponent(name)}/git/commits/${encodeURIComponent(finalCommitSha)}`);
  const finalTreeSha = finalCommit && finalCommit.tree && finalCommit.tree.sha;
  if (!finalTreeSha) throw new Error('Shadow Clone verification failed: GitHub did not return the copied file tree.');
  await report('finalize', files.length, files.length);
  const commit = { sha: finalCommitSha };
  const tree = { sha: finalTreeSha };
  try {
    // bug-47: the rest of this module (and the workflows' assumptions about
    // the clone layout) address the clone via DEFAULT_BRANCH. If the account's
    // default-branch preference made the first branch something else (e.g.
    // "master"), normalize to DEFAULT_BRANCH with the explicit
    // create-ref -> PATCH default_branch -> delete-old-ref sequence. This is
    // synchronous, unlike POST /branches/<b>/rename (verified live: rename
    // moves the refs instantly but leaves default_branch pointing at the old
    // name for 1-3s — the user could open the repo right after the success
    // message and still see the pre-normalization state).
    if (targetBranch !== DEFAULT_BRANCH) {
      await githubRequest(targetCredentials, `/repos/${encodeURIComponent(login)}/${encodeURIComponent(name)}/git/refs`, {
        method: 'POST', body: { ref: `refs/heads/${DEFAULT_BRANCH}`, sha: commit.sha }
      });
      await githubRequest(targetCredentials, `/repos/${encodeURIComponent(login)}/${encodeURIComponent(name)}`, {
        method: 'PATCH', body: { default_branch: DEFAULT_BRANCH }
      });
      await githubRequest(targetCredentials, `/repos/${encodeURIComponent(login)}/${encodeURIComponent(name)}/git/refs/heads/${encodeURIComponent(targetBranch)}`, {
        method: 'DELETE'
      });
      targetBranch = DEFAULT_BRANCH;
    }
    // bug-47 post-conditions: the clone is only "created" when the repo's
    // DEFAULT branch head is the commit carrying the full copied tree. Check
    // against the live API and fail loudly instead of reporting success on a
    // repo GitHub would display as sync-marker-only (the exact symptom this
    // bug is about — the prior sweep's verification checked the function's
    // own return value, which was correct even when the visible repo was not).
    const verifyRepo = await githubRequest(targetCredentials, `/repos/${encodeURIComponent(login)}/${encodeURIComponent(name)}`);
    const verifyBranch = verifyRepo && verifyRepo.default_branch ? String(verifyRepo.default_branch) : targetBranch;
    const verifyRef = await githubRequest(targetCredentials, `/repos/${encodeURIComponent(login)}/${encodeURIComponent(name)}/git/ref/heads/${encodeURIComponent(verifyBranch)}`);
    const verifySha = verifyRef && verifyRef.object && verifyRef.object.sha;
    if (verifySha !== commit.sha) {
      throw new Error(`Shadow Clone verification failed: the repository's default branch (${verifyBranch}) does not point at the copied file tree.`);
    }
    const verifyTree = await githubRequest(targetCredentials, `/repos/${encodeURIComponent(login)}/${encodeURIComponent(name)}/git/trees/${encodeURIComponent(tree.sha)}?recursive=1`);
    const verifyBlobs = Array.isArray(verifyTree && verifyTree.tree) ? verifyTree.tree.filter((entry) => entry && entry.type === 'blob').length : 0;
    if (!verifyTree || verifyTree.truncated || verifyBlobs < files.length) {
      throw new Error(`Shadow Clone verification failed: the pushed file tree holds ${verifyBlobs} files, expected at least ${files.length}.`);
    }
  } catch (error) {
    // bug-46: replace the raw GitHub API message with something that is
    // accurate for the actual failure mode (Git Data API write against a
    // freshly-created repo) so handleClonePatMessage's catch-all does not
    // misdirect users to re-check PAT scopes when the underlying problem is
    // repo-side and not permission-related.
    if (error instanceof GitHubError) {
      throw new Error(`GitHub rejected a write to the new repository (${error.status || 'unknown'}): ${error.message}`);
    }
    throw error;
  }
  return { repo: String(job.repo), login, sourceSha: sourceCommitSha, copiedFiles: files.length, branch: targetBranch };
}

/**
 * bug-63: best-effort read of the one-time copy workflow's status file from
 * the (possibly not yet normalized) bootstrap branch. Returns null while the
 * file does not exist yet or cannot be parsed — the caller keeps polling.
 */
async function readCloneCopyStatus(credentials, branch) {
  const { owner, name } = parseRepo(credentials.repo);
  try {
    const file = await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/contents/${encodePath(CLONE_STATUS_PATH)}?ref=${encodeURIComponent(branch)}`);
    if (!file || typeof file.content !== 'string') return null;
    const parsed = JSON.parse(b64decode(file.content));
    if (!parsed || typeof parsed.state !== 'string') return null;
    return {
      state: parsed.state,
      done: Number(parsed.done) || 0,
      total: Number(parsed.total) || 0,
      error: typeof parsed.error === 'string' ? parsed.error.slice(0, 300) : ''
    };
  } catch {
    return null;
  }
}

/**
 * bug-63: the one-time copy workflow committed into a brand-new Shadow Clone.
 * It mirrors exactly what beginShadowCloneCreation used to do in the Worker —
 * copy every cloneable source file, build the final tree on top of the
 * bootstrap commit, fast-forward the default branch — but on an Actions
 * runner, whose execution budget is hours instead of a single Worker request
 * lifetime. Progress is written to .clipforge-clone-status.json (the bot
 * polls it, same commit-a-status-file pattern as the job pipeline). The
 * workflow self-deletes in the final commit on success and always leaves the
 * status file behind for diagnosis. Kept as a JS template (not a repo YAML
 * file) so the token never lives in the source repository's workflow
 * directory, where it would run on every clone push.
 */
function buildCloneCopyWorkflowYaml() {
  return `name: Shadow Clone — one-time file copy

# bug-63: created by the ClipForge bot's Shadow Clone onboarding and
# DISPATCHED ONCE with the source revision + bootstrap commit as inputs. It
# performs the bulk source-file copy that previously ran inside the
# Cloudflare Worker and was killed mid-loop by the Worker's execution-time
# limit (leaving clones with only .clipforge-sync.json). Self-deletes on
# success; permanent failures (missing inputs, non-fast-forward, empty tree)
# trap the workflow so it can never silently fire again on a later push.

on:
  workflow_dispatch:
    inputs:
      source_sha:
        description: "Source commit SHA to copy from (must be an ancestor of motionssalt/clipforge main)"
        required: true
        type: string
      bootstrap_commit:
        description: "Bootstrap commit SHA the final tree is built on top of"
        required: true
        type: string
      expected_files:
        description: "Number of cloneable files the bot enumerated at dispatch time"
        required: true
        type: string

permissions:
  contents: write

concurrency:
  group: clipforge-clone-copy
  cancel-in-progress: false

jobs:
  copy:
    name: Copy source files
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - name: Validate inputs
        shell: bash
        run: |
          set -euo pipefail
          printf '%s' "\${{ inputs.source_sha }}" | grep -qE '^[0-9a-f]{40}$'
          printf '%s' "\${{ inputs.bootstrap_commit }}" | grep -qE '^[0-9a-f]{40}$'
          printf '%s' "\${{ inputs.expected_files }}" | grep -qE '^[0-9]+$'

      - name: Check out the clone repository
        uses: actions/checkout@v4
        with:
          ref: "\${{ github.ref_name }}"
          fetch-depth: 0

      - name: Record start status
        shell: bash
        run: |
          set -euo pipefail
          git config user.name  "clipforge-bot"
          git config user.email "clipforge-bot@users.noreply.github.com"
          TOTAL=$(printf '%s' "\${{ inputs.expected_files }}")
          printf '{"version":1,"state":"copying","done":0,"total":%s,"updated_at":"%s"}\\n' "$TOTAL" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > .clipforge-clone-status.json
          git add .clipforge-clone-status.json
          git commit -m "clipforge: clone copy started" -q
          git push -q origin "HEAD:\${{ github.ref_name }}"

      - name: Fetch the source tree
        shell: bash
        run: |
          set -euo pipefail
          rm -rf /tmp/clipforge-src
          git clone --filter=blob:none --no-checkout --depth 50 https://github.com/${SHADOW_CLONE_SOURCE}.git /tmp/clipforge-src
          cd /tmp/clipforge-src
          git cat-file -e "\${{ inputs.source_sha }}^{commit}" || { echo "source_sha not reachable from ${SHADOW_CLONE_SOURCE} main"; exit 1; }
          git checkout -q "\${{ inputs.source_sha }}"

      - name: Copy cloneable files and report progress
        id: copy
        shell: bash
        run: |
          set -euo pipefail
          copy_one() {
            local p="$1"
            if git -C /tmp/clipforge-src cat-file -e "\${{ inputs.source_sha }}:$p" 2>/dev/null; then
              mkdir -p "$(dirname "$p")"
              git -C /tmp/clipforge-src show "\${{ inputs.source_sha }}:$p" > "$p"
            fi
          }
          git -C /tmp/clipforge-src ls-tree -r --name-only "\${{ inputs.source_sha }}" \
            | grep -Ev '^(branding/|jobs/|audio-library/)' \
            | grep -Eiv 'keys|accounts|queue' \
            > /tmp/clipforge-all.txt
          # Workflow files are NOT copied into the working tree: a push from
          # this run must not touch .github/workflows/* (GITHUB_TOKEN pushes
          # carrying workflow changes are remote-rejected — verified live,
          # bug-63; Contents-API PUTs for them are 403-forbidden too). The
          # source's own workflow files are written afterwards by the BOT via
          # the Contents API using the user's PAT, whose workflow scope the
          # onboarding prompt already requires — the same authority the
          # original in-Worker copy relied on. The workflow list is still
          # written so the bot-side follow-up can enumerate exactly which
          # source paths exist.
          grep -v '^\.github/workflows/' /tmp/clipforge-all.txt > /tmp/clipforge-files.txt
          grep '^\.github/workflows/' /tmp/clipforge-all.txt > /tmp/clipforge-workflows.txt || true
          # Mode manifest for the exec-bit restore below (the source ships one
          # 100755 script; exec bits must survive the copy — the old in-Worker
          # copy carried file.mode through for this reason).
          git -C /tmp/clipforge-src ls-tree -r "\${{ inputs.source_sha }}" \
            | awk -F'\t' '{
                p = $2;
                # Portable string ops only — mawk (Ubuntu default) mis-parses
                # escaped slashes inside awk regex constants (verified live).
                if (substr(p,1,9) == "branding/" || substr(p,1,5) == "jobs/" || substr(p,1,14) == "audio-library/") next;
                tl = tolower(p);
                if (index(tl,"keys") || index(tl,"accounts") || index(tl,"queue")) next;
                split($1, m, " ");
                print m[1] " " p;
              }' > /tmp/clipforge-modes.txt
          TOTAL=$(wc -l < /tmp/clipforge-all.txt | tr -d ' ')
          PUSHABLE=$(wc -l < /tmp/clipforge-files.txt | tr -d ' ')
          if [ "$TOTAL" -eq 0 ]; then echo "no cloneable files found"; exit 1; fi
          if [ "$TOTAL" -ne "\${{ inputs.expected_files }}" ]; then
            echo "warning: enumerated $TOTAL files, bot expected \${{ inputs.expected_files }}"
          fi
          echo "total=$TOTAL" >> "$GITHUB_OUTPUT"
          CHUNK=25
          COUNT=0
          NEXT=$CHUNK
          # Live-verified (bug-63): the file list MUST be redirected into the
          # loop — GitHub Actions run-steps have no interactive stdin, so a
          # bare read loop exits after zero iterations and reports success
          # with 0 files copied.
          while IFS= read -r p; do
            copy_one "$p"
            COUNT=$((COUNT + 1))
            if [ "$COUNT" -ge "$NEXT" ] || [ "$COUNT" -eq "$PUSHABLE" ]; then
              printf '{"version":1,"state":"copying","done":%s,"total":%s,"updated_at":"%s"}\\n' "$COUNT" "$PUSHABLE" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > .clipforge-clone-status.json
              git add .clipforge-clone-status.json
              git commit -q -m "clipforge: clone copy progress $COUNT/$PUSHABLE"
              git push -q origin "HEAD:\${{ github.ref_name }}"
              NEXT=$((COUNT + CHUNK))
            fi
          done < /tmp/clipforge-files.txt
          if [ "$COUNT" -ne "$PUSHABLE" ]; then echo "copied $COUNT of $PUSHABLE files"; exit 1; fi
          # Restore exec bits: git-show-redirect materializes every file as
          # 644 regardless of the source mode, and git add records what it
          # sees. chmod from the modes manifest (path = text after first
          # space, so space-containing paths survive).
          awk '$1 == "100755" { print substr($0, index($0, " ") + 1) }' /tmp/clipforge-modes.txt | while IFS= read -r p; do
            if [ -f "$p" ]; then chmod +x "$p"; fi
          done
          echo "copied=$COUNT" >> "$GITHUB_OUTPUT"

      - name: Publish the full tree
        # Live-verified (bug-63): ONE fast-forward git push carries the whole
        # copied tree EXCEPT .github/workflows/* (the copy step deliberately
        # leaves those out of the working tree). This is allowed because the
        # commit chain touches no workflow path, and it needs ZERO REST
        # mutations — the earlier designs of ~330 paced REST blob writes and
        # of GITHUB_TOKEN Contents-API workflow PUTs both hit 403s live
        # (secondary rate limit on the former, the workflow-file restriction
        # on the latter). The push is built on top of the bootstrap commit,
        # so .clipforge-sync.json is preserved (the old in-Worker copy's
        # base_tree behaviour) and exec bits restored by the copy step's
        # chmod survive (git add records what it sees).
        shell: bash
        env:
          COPIED: \${{ steps.copy.outputs.copied }}
          TOTAL: \${{ steps.copy.outputs.total }}
        run: |
          set -euo pipefail
          git config user.name  "clipforge-bot"
          git config user.email "clipforge-bot@users.noreply.github.com"
          printf '{"version":1,"state":"finalizing","done":%s,"total":%s,"updated_at":"%s"}\\n' "$COPIED" "$TOTAL" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > .clipforge-clone-status.json
          git add -A
          git commit -q -m "clipforge: copy files from ${SHADOW_CLONE_SOURCE}@\${{ inputs.source_sha }} ($COPIED files)"
          # Fast-forward only: refuse to clobber a branch that moved.
          git push -q origin "HEAD:\${{ github.ref_name }}"
          # Server-side post-condition: the pushed head must descend from the
          # bootstrap commit and its tree must hold at least TOTAL minus the
          # source-workflow count blobs (those arrive via the bot's PAT after
          # the run reports 'complete').
          git fetch -q origin "\${{ github.ref_name }}"
          git merge-base --is-ancestor "\${{ inputs.bootstrap_commit }}" "origin/\${{ github.ref_name }}" || { echo "bootstrap commit is not an ancestor of the pushed head"; exit 1; }
          WFCOUNT=$(wc -l < /tmp/clipforge-workflows.txt | tr -d ' ')
          FLOOR=$((TOTAL - WFCOUNT))
          BLOBS=$(git ls-tree -r "origin/\${{ github.ref_name }}" | grep -c ' blob ')
          if [ "$BLOBS" -lt "$FLOOR" ]; then echo "published tree holds $BLOBS blobs, expected at least $FLOOR"; exit 1; fi
          echo "published tree verified: $BLOBS blobs (source workflows pending via the bot)"

      - name: Record completion
        shell: bash
        env:
          TOTAL: \${{ steps.copy.outputs.total }}
          COPIED: \${{ steps.copy.outputs.copied }}
        run: |
          set -euo pipefail
          git config user.name  "clipforge-bot"
          git config user.email "clipforge-bot@users.noreply.github.com"
          # The publish step's push advanced the ref; resync before the final
          # status commit. The one-time workflow file itself is NOT deleted
          # here — a GITHUB_TOKEN push may not touch .github/workflows/* even
          # to delete (verified live, bug-63). The bot removes it via the
          # Contents API with the user's PAT right after this status lands.
          git fetch -q origin "\${{ github.ref_name }}"
          git reset -q --hard "origin/\${{ github.ref_name }}"
          printf '{"version":1,"state":"complete","done":%s,"total":%s,"updated_at":"%s"}\\n' "$COPIED" "$TOTAL" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > .clipforge-clone-status.json
          git add .clipforge-clone-status.json
          git commit -q -m "clipforge: clone copy complete"
          git push -q origin "HEAD:\${{ github.ref_name }}"

      - name: Record failure
        if: failure()
        shell: bash
        env:
          TOTAL: \${{ steps.copy.outputs.total }}
        run: |
          set +e
          git config user.name  "clipforge-bot"
          git config user.email "clipforge-bot@users.noreply.github.com"
          git fetch -q origin "\${{ github.ref_name }}"
          git reset -q --hard "origin/\${{ github.ref_name }}"
          printf '{"version":1,"state":"failed","done":0,"total":%s,"error":"copy workflow step failed — see the Actions run log","updated_at":"%s"}\\n' "\${TOTAL:-0}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > .clipforge-clone-status.json
          git add .clipforge-clone-status.json
          git commit -q -m "clipforge: clone copy failed" || true
          git push -q origin "HEAD:\${{ github.ref_name }}" || true
          exit 1
`;
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
    // A push race can leave git conflict markers inside a status file. Recover
    // the union so the bot can still read job state; the next workflow write
    // overwrites the file with a clean copy.
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

/**
 * bug-22: find one asset on the release for a tag, with its size and
 * browser_download_url. Returns null when the release or asset is absent.
 * `asset.state === 'uploaded'` mirrors the Stage B (bug-20) acceptance rule:
 * a starter/queued asset is not yet downloadable.
 */
export async function findReleaseAsset(credentials, repo, tag, assetName) {
  const { owner, name } = parseRepo(repo);
  const release = await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/releases/tags/${encodeURIComponent(tag)}`);
  const assets = release && Array.isArray(release.assets) ? release.assets : [];
  const asset = assets.find((entry) => entry && entry.name === assetName && (entry.state === 'uploaded' || entry.state === undefined));
  if (!asset) return null;
  return {
    name: asset.name,
    size: Number(asset.size) || 0,
    url: String(asset.browser_download_url || ''),
    releaseUrl: String(release.html_url || '')
  };
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
  return /^audio-library\/[^/\\ -]+$/.test(String(path || ''));
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
  return putTextFile(credentials, repo, MUSIC_DEFAULT_PATH, `${JSON.stringify(document, null, 2)}\n`, 'clipforge: save default background music');
}

export async function deleteAudioLibraryTrack(credentials, repo, trackPath) {
  if (!safeAudioLibraryPath(trackPath)) throw new Error('Only a safe audio-library track may be deleted.');
  return deleteRepositoryFile(credentials, repo, trackPath, `clipforge: remove ${trackPath.slice('audio-library/'.length)} from audio library`);
}

export async function clearMusicDefaultIfTrack(credentials, repo, trackPath) {
  if (!safeAudioLibraryPath(trackPath)) throw new Error('Only a safe audio-library track may clear the default.');
  const current = await tryGetJsonFile(credentials, repo, MUSIC_DEFAULT_PATH);
  if (!current || !current.document || current.document.library_track_path !== trackPath) return false;
  await deleteRepositoryFile(credentials, repo, MUSIC_DEFAULT_PATH, 'clipforge: clear deleted default background music');
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

/**
 * Dispatch a workflow. ``codeRef`` (the freshly resolved default-branch SHA)
 * is passed through as the workflow's ``code_ref`` input whenever the target
 * workflow declares one — restarts never re-run stale code (§8.5). The
 * dispatch ref itself stays the default branch (GitHub requires a branch/tag
 * for workflow_dispatch).
 */
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

/**
 * bug-68: list the most recent completed runs of one workflow file. The bot
 * polls the CONNECTED repo's own "Deploy Bots" runs with the owner's PAT (the
 * same authority every other Actions read here uses) so a failed deploy on a
 * clone can be surfaced to the clone owner on Telegram — the workflow-side
 * alert step self-skips when BOTB_MTPROTO_BOT_TOKEN is absent, which is every
 * clone (nothing provisions that secret for them).
 * Returns an array of { id, headSha, conclusion, htmlUrl, createdAt }.
 */
export async function listWorkflowRuns(credentials, repo, workflowFile, perPage = 10) {
  const { owner, name } = parseRepo(repo);
  const body = await githubRequest(credentials,
    `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/actions/workflows/${encodeURIComponent(workflowFile)}/runs?per_page=${encodeURIComponent(perPage)}`);
  const runs = body && Array.isArray(body.workflow_runs) ? body.workflow_runs : [];
  return runs.map((run) => ({
    id: run && run.id,
    headSha: String(run && run.head_sha || ''),
    status: String(run && run.status || ''),
    conclusion: String(run && run.conclusion || ''),
    event: String(run && run.event || ''),
    htmlUrl: String(run && run.html_url || ''),
    createdAt: String(run && run.created_at || '')
  }));
}

export async function currentBranchSha(credentials, repo) {
  const { owner, name } = parseRepo(repo);
  const branch = await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/branches/${encodeURIComponent(DEFAULT_BRANCH)}`);
  const sha = branch && branch.commit && branch.commit.sha;
  if (!sha) throw new Error(`Could not resolve the current ${DEFAULT_BRANCH} commit.`);
  return sha;
}

/**
 * bug-49: read the connected repository's current visibility. Returns
 * { private: boolean, defaultBranch, fullName } straight from the repo
 * object so the settings screen can render the toggle's current state and
 * the flip lands exactly where GitHub reports it afterwards.
 */
export async function getRepositoryVisibility(credentials, repo) {
  const { owner, name } = parseRepo(repo);
  const repository = await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`);
  if (!repository || typeof repository.private !== 'boolean') throw new Error('GitHub did not report the repository visibility.');
  return {
    private: repository.private,
    defaultBranch: String(repository.default_branch || DEFAULT_BRANCH),
    fullName: String(repository.full_name || `${owner}/${name}`)
  };
}

/**
 * bug-49: flip the connected repository between private and public.
 * `makePrivate: false` publicises, `makePrivate: true` re-privatises.
 * Reads the repository back after the PATCH and returns the settled
 * visibility so callers confirm what actually landed instead of trusting
 * the request (matches the bug-47 read-back discipline).
 */
export async function setRepositoryVisibility(credentials, repo, makePrivate) {
  const { owner, name } = parseRepo(repo);
  await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`, {
    method: 'PATCH',
    body: { private: Boolean(makePrivate) }
  });
  return getRepositoryVisibility(credentials, repo);
}

/**
 * bug-50: permanently delete the connected repository (DELETE
 * /repos/{owner}/{repo}). Requires delete_repo on classic PATs or
 * Administration (write) on fine-grained PATs — GitHub answers 403/404 when
 * the token lacks it, and callers surface that with plain scope guidance.
 * Returns true only when GitHub actually accepted the deletion (204).
 */
export async function deleteRepository(credentials, repo) {
  const { owner, name } = parseRepo(repo);
  await githubRequest(credentials, `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`, { method: 'DELETE' });
  return true;
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
  const result = await tryGetJsonFile(credentials, repo, SERIES_SETTINGS_PATH);
  return result ? result.document : null;
}

export async function saveSeriesSettings(credentials, repo, enabled) {
  const document = { version: 1, enabled: enabled === true, updated_at_epoch: Math.floor(Date.now() / 1000) };
  return putTextFile(credentials, repo, SERIES_SETTINGS_PATH, `${JSON.stringify(document, null, 2)}\n`, 'clipforge: update Series Mode setting');
}

export async function saveNarrator(credentials, repo, voice, label) {
  const document = { version: 1, engine: 'edge-tts', voice, voice_label: label, rate: '+20%', volume: '+0%', pitch: '+0Hz', updated_at_epoch: Math.floor(Date.now() / 1000) };
  return putTextFile(credentials, repo, TTS_SETTINGS_PATH, `${JSON.stringify(document, null, 2)}\n`, 'clipforge: save Edge TTS narrator');
}

export async function saveWatermark(credentials, repo, creatorName) {
  const document = { version: 1, creator_name: creatorName, updated_at_epoch: Math.floor(Date.now() / 1000) };
  return putTextFile(credentials, repo, WATERMARK_PATH, `${JSON.stringify(document, null, 2)}\n`, 'clipforge: update creator watermark');
}

const SOURCE_KINDS = new Set(['url', 'drive', 'magnet', 'torrent_file', 'telegram_channel', 'telegram_relay']);
const MUSIC_SOURCES = new Set(['none', 'default', 'explicit_library', 'job_upload']);

/**
 * Write jobs/<job_id>/stage-a-request.json in the ARCHITECTURE.md §7.1 nested
 * shape (schemas/stage_a_request.schema.json). This is the ONLY writer of that
 * contract; stage-a.yml refuses to run without it.
 *
 * ``request`` shape:
 *   {
 *     source:  { kind, value, relay?, torrent_file_index? },
 *     options: { whisper_model?, language?, task?, target_duration_seconds?, focus?,
 *                enable_vision_assist? },
 *     mode:    'manual' | 'automatic',
 *     series:  { enabled, series_id, source_job_id, part, start_seconds, context },
 *     music:   { ref, source }
 *   }
 */
export function buildStageARequest(jobId, request) {
  if (!safeJobId(jobId)) throw new Error('That task identifier is invalid.');
  const source = request && typeof request.source === 'object' && request.source ? request.source : {};
  const kind = String(source.kind || '');
  if (!SOURCE_KINDS.has(kind)) throw new Error(`Unknown source kind: ${kind || 'missing'}.`);
  const options = request && typeof request.options === 'object' && request.options ? request.options : {};
  const series = request && typeof request.series === 'object' && request.series ? request.series : {};
  const music = request && typeof request.music === 'object' && request.music ? request.music : {};
  // bug-30: manual is the only task-creation mode; any legacy/injected
  // 'automatic' request is normalized to manual so no job can resurrect the
  // removed Gemini path.
  const mode = 'manual';
  const whisperModel = WHISPER_MODELS.has(options.whisper_model) ? options.whisper_model : 'base';
  const targetDuration = Math.max(1, Math.floor(Number(options.target_duration_seconds) || 120));
  const musicSource = MUSIC_SOURCES.has(music.source) ? music.source : 'none';
  const context = String(series.context || '').slice(0, 8000);

  const document = {
    version: 2,
    job_id: jobId,
    source: {
      kind,
      value: String(source.value || ''),
      ...(source.relay && typeof source.relay === 'object' ? {
        relay: {
          release_tag: String(source.relay.release_tag || ''),
          expected_size_bytes: String(source.relay.expected_size_bytes || ''),
          sha256: String(source.relay.sha256 || '')
        }
      } : {}),
      ...(source.torrent_file_index !== undefined && source.torrent_file_index !== ''
        ? { torrent_file_index: String(source.torrent_file_index) }
        : {})
    },
    options: {
      whisper_model: whisperModel,
      // bug-65: default is auto-detect ("auto") so Whisper identifies the real
      // source language; the translate task then renders any non-English audio
      // into English. Never force "en" — that would suppress detection of
      // genuinely non-English audio. task="transcribe" is the explicit opt-out
      // that preserves the original language.
      language: String(options.language || 'auto'),
      task: options.task === 'transcribe' ? 'transcribe' : 'translate_to_english',
      target_duration_seconds: targetDuration,
      focus: String(options.focus || ''),
      enable_vision_assist: options.enable_vision_assist !== false
    },
    mode,
    series: {
      enabled: series.enabled === true,
      series_id: String(series.series_id || ''),
      source_job_id: String(series.source_job_id || ''),
      part: Math.max(0, Math.floor(Number(series.part) || 0)),
      start_seconds: Math.max(0, Math.floor(Number(series.start_seconds) || 0)),
      context
    },
    music: {
      ref: musicSource === 'none' ? '' : String(music.ref || ''),
      source: musicSource
    },
    saved_at_epoch: Math.floor(Date.now() / 1000)
  };
  return document;
}

export async function saveStageARequest(credentials, repo, jobId, request) {
  const document = buildStageARequest(jobId, request);
  return putTextFile(credentials, repo, STAGE_A_REQUEST_PATH(jobId), `${JSON.stringify(document, null, 2)}\n`, `clipforge: save Stage A request for job ${jobId}`);
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
  // bug-30: all jobs are manual now; keep the mode arg for call-site compat.
  void mode;
  return `manual-${Number(now)}`;
}

export { b64decode, b64encode, cloneRepositoryName, parseJsonDocument, parseRepo, safeAudioLibraryPath, safeJobId, sourcePathAllowed };
