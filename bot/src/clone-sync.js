/**
 * Shadow-clone update helper (bug-21).
 *
 * Any user connected to a clone can pull the latest motionssalt/clipforge
 * source into their own clone from Settings → GitHub clone → Sync from source.
 * This is the mechanism the *main* account uses to push updates from the
 * source repo to their own deployment clone — but it works identically for
 * anyone who has cloned the source, since GitHub-side "push updates to
 * someone else's private repo" is impossible with per-user PATs. The
 * "main-account-visible" wording in the bug is satisfied by the same button
 * being visible to every connected clone (including the main account's).
 *
 * Ported semantically from _legacy/shadow-clone.js (the old browser Settings
 * page had check-updates + apply-updates buttons). Same excludes, same sync
 * file (.clipforge-sync.json), same GitHub Git-Data commit sequence.
 */

import {
  SHADOW_CLONE_SOURCE,
  githubRequest,
  sourcePathAllowed,
  parseRepo,
  b64encode,
} from './github.js';
import { DEFAULT_BRANCH } from './constants.js';

const SYNC_PATH = '.clipforge-sync.json';

function encodeSeg(value) {
  return encodeURIComponent(String(value));
}

async function sourceHead(credentials) {
  const [owner, name] = SHADOW_CLONE_SOURCE.split('/');
  const ref = await githubRequest(
    credentials,
    `/repos/${encodeSeg(owner)}/${encodeSeg(name)}/git/ref/heads/${encodeSeg(DEFAULT_BRANCH)}`
  );
  const sha = ref && ref.object && ref.object.sha;
  if (!sha) throw new Error('Could not resolve the current ClipForge source revision.');
  return sha;
}

async function readSyncFile(credentials, repo) {
  const { owner, name } = parseRepo(repo);
  try {
    const file = await githubRequest(
      credentials,
      `/repos/${encodeSeg(owner)}/${encodeSeg(name)}/contents/${SYNC_PATH}?ref=${encodeSeg(DEFAULT_BRANCH)}`
    );
    if (!file || typeof file.content !== 'string') return null;
    const decoded = atob(file.content.replace(/\n/g, ''));
    const parsed = JSON.parse(decoded);
    if (parsed && parsed.source === SHADOW_CLONE_SOURCE && typeof parsed.synced_sha === 'string') {
      return { sha: parsed.synced_sha, at: typeof parsed.synced_at === 'string' ? parsed.synced_at : '' };
    }
    return null;
  } catch (error) {
    if (error && error.status === 404) return null;
    throw error;
  }
}

/**
 * Check whether the connected clone is behind the source. Returns
 * { upToDate, sourceSha, syncedSha, syncedAt, changes: [{path, status, previousPath}] }
 * When upToDate is true, changes is empty. Uses GitHub's compare API so we
 * only fetch the diff, not the whole tree.
 */
export async function checkCloneUpdates(credentials) {
  const repo = credentials && credentials.repo;
  if (!repo) throw new Error('This chat is not connected to a clone.');
  const sourceSha = await sourceHead(credentials);
  const synced = await readSyncFile(credentials, repo);
  if (!synced || !synced.sha) {
    // No sync marker: everything on source is treated as an update. Fall back
    // to a snapshot listing.
    const files = await sourceSnapshot(credentials, sourceSha);
    return {
      upToDate: false,
      sourceSha,
      syncedSha: '',
      syncedAt: '',
      changes: files.map((f) => ({ path: f.path, status: 'modified', previousPath: null })),
      bootstrap: true,
    };
  }
  if (synced.sha === sourceSha) {
    return { upToDate: true, sourceSha, syncedSha: synced.sha, syncedAt: synced.at, changes: [] };
  }
  const [owner, name] = SHADOW_CLONE_SOURCE.split('/');
  const comparison = await githubRequest(
    credentials,
    `/repos/${encodeSeg(owner)}/${encodeSeg(name)}/compare/${encodeSeg(synced.sha)}...${encodeSeg(sourceSha)}`
  );
  const rawFiles = Array.isArray(comparison && comparison.files) ? comparison.files : [];
  const changes = rawFiles
    .filter((f) => f && f.filename && sourcePathAllowed(f.filename) && !(f.previous_filename && !sourcePathAllowed(f.previous_filename)))
    .map((f) => ({
      path: f.filename,
      status: f.status === 'renamed' ? 'modified' : (f.status || 'modified'),
      previousPath: f.status === 'renamed' ? f.previous_filename : null,
    }));
  if (!changes.length) {
    return { upToDate: true, sourceSha, syncedSha: synced.sha, syncedAt: synced.at, changes: [] };
  }
  return { upToDate: false, sourceSha, syncedSha: synced.sha, syncedAt: synced.at, changes };
}

async function sourceSnapshot(credentials, sha) {
  const [owner, name] = SHADOW_CLONE_SOURCE.split('/');
  const commit = await githubRequest(
    credentials,
    `/repos/${encodeSeg(owner)}/${encodeSeg(name)}/git/commits/${encodeSeg(sha)}`
  );
  const treeSha = commit && commit.tree && commit.tree.sha;
  if (!treeSha) throw new Error('Could not resolve the ClipForge source file tree.');
  const tree = await githubRequest(
    credentials,
    `/repos/${encodeSeg(owner)}/${encodeSeg(name)}/git/trees/${encodeSeg(treeSha)}?recursive=1`
  );
  if (tree && tree.truncated) throw new Error('The ClipForge source tree is too large to sync safely.');
  return Array.isArray(tree && tree.tree)
    ? tree.tree.filter((entry) => entry && entry.type === 'blob' && sourcePathAllowed(entry.path))
    : [];
}

async function copyBlob(credentials, repo, sourceOwner, sourceName, blobSha) {
  const sourceBlob = await githubRequest(
    credentials,
    `/repos/${encodeSeg(sourceOwner)}/${encodeSeg(sourceName)}/git/blobs/${encodeSeg(blobSha)}`
  );
  if (!sourceBlob || sourceBlob.encoding !== 'base64' || typeof sourceBlob.content !== 'string') {
    throw new Error('Could not read a source blob during clone sync.');
  }
  const { owner, name } = parseRepo(repo);
  const targetBlob = await githubRequest(
    credentials,
    `/repos/${encodeSeg(owner)}/${encodeSeg(name)}/git/blobs`,
    { method: 'POST', body: { content: sourceBlob.content.replace(/\n/g, ''), encoding: 'base64' } }
  );
  if (!targetBlob || !targetBlob.sha) throw new Error('Could not write a blob into the clone during sync.');
  return targetBlob.sha;
}

/**
 * Apply the changeset returned by checkCloneUpdates to the clone. Uses a
 * single git tree/commit built against the clone's current main HEAD as
 * base_tree, so untouched clone-local paths (branding/, jobs/, etc.) are
 * preserved automatically.
 */
export async function applyCloneUpdates(credentials, plan) {
  const repo = credentials && credentials.repo;
  if (!repo) throw new Error('This chat is not connected to a clone.');
  if (!plan || plan.upToDate || !Array.isArray(plan.changes) || !plan.changes.length) {
    return { applied: 0, sourceSha: (plan && plan.sourceSha) || '' };
  }
  const [sourceOwner, sourceName] = SHADOW_CLONE_SOURCE.split('/');
  const snapshot = await sourceSnapshot(credentials, plan.sourceSha);
  const byPath = new Map(snapshot.map((entry) => [entry.path, entry]));

  const entries = [];
  for (const change of plan.changes) {
    if (change.previousPath) {
      entries.push({ path: change.previousPath, mode: '100644', type: 'blob', sha: null });
    }
    if (change.status === 'removed') {
      entries.push({ path: change.path, mode: '100644', type: 'blob', sha: null });
      continue;
    }
    const sourceFile = byPath.get(change.path);
    if (!sourceFile) throw new Error(`Updated source file was not found in the current source tree: ${change.path}`);
    const blobSha = await copyBlob(credentials, repo, sourceOwner, sourceName, sourceFile.sha);
    entries.push({ path: change.path, mode: sourceFile.mode || '100644', type: 'blob', sha: blobSha });
  }

  // Refresh the sync marker.
  const syncPayload = {
    source: SHADOW_CLONE_SOURCE,
    synced_sha: plan.sourceSha,
    synced_at: new Date().toISOString(),
  };
  const { owner, name } = parseRepo(repo);
  const syncBlob = await githubRequest(
    credentials,
    `/repos/${encodeSeg(owner)}/${encodeSeg(name)}/git/blobs`,
    { method: 'POST', body: { content: b64encode(`${JSON.stringify(syncPayload, null, 2)}\n`), encoding: 'base64' } }
  );
  if (!syncBlob || !syncBlob.sha) throw new Error('Could not write the clone sync marker.');
  entries.push({ path: SYNC_PATH, mode: '100644', type: 'blob', sha: syncBlob.sha });

  // Base against current main head so untouched paths persist.
  const headRef = await githubRequest(
    credentials,
    `/repos/${encodeSeg(owner)}/${encodeSeg(name)}/git/ref/heads/${encodeSeg(DEFAULT_BRANCH)}`
  );
  const parentSha = headRef && headRef.object && headRef.object.sha;
  if (!parentSha) throw new Error('The clone is missing its main branch head; sync cannot proceed.');
  const parentCommit = await githubRequest(
    credentials,
    `/repos/${encodeSeg(owner)}/${encodeSeg(name)}/git/commits/${encodeSeg(parentSha)}`
  );
  const baseTreeSha = parentCommit && parentCommit.tree && parentCommit.tree.sha;
  if (!baseTreeSha) throw new Error('Could not read the clone base tree during sync.');

  const treeBody = { tree: entries, base_tree: baseTreeSha };
  const tree = await githubRequest(
    credentials,
    `/repos/${encodeSeg(owner)}/${encodeSeg(name)}/git/trees`,
    { method: 'POST', body: treeBody }
  );
  if (!tree || !tree.sha) throw new Error('Could not create the sync tree in the clone.');

  const commit = await githubRequest(
    credentials,
    `/repos/${encodeSeg(owner)}/${encodeSeg(name)}/git/commits`,
    {
      method: 'POST',
      body: {
        message: `Sync from ${SHADOW_CLONE_SOURCE}@${plan.sourceSha.slice(0, 7)} (${plan.changes.length} file${plan.changes.length === 1 ? '' : 's'})`,
        tree: tree.sha,
        parents: [parentSha],
      },
    }
  );
  if (!commit || !commit.sha) throw new Error('Could not commit the sync into the clone.');

  await githubRequest(
    credentials,
    `/repos/${encodeSeg(owner)}/${encodeSeg(name)}/git/refs/heads/${encodeSeg(DEFAULT_BRANCH)}`,
    { method: 'PATCH', body: { sha: commit.sha } }
  );

  return { applied: plan.changes.length, sourceSha: plan.sourceSha, commitSha: commit.sha };
}
