/**
 * ClipForge Bot A — main-owner identity helpers (pure, no I/O).
 *
 * bug-53: the owner gates historically compared the stored connected repo
 * against the original repo with a raw lowercase equality check. The stored
 * value comes from whatever the user typed (or, on the clone-creation path,
 * from the API's canonical full_name) — so any drift (case, surrounding
 * whitespace, a pasted github.com URL, a trailing ".git") silently demoted
 * the ONE main-owner account to a clone, hiding main-only controls such as
 * "📰 Push news to clones" and "📣 Publish update to clones". These helpers
 * centralize the comparison behind slug normalization (same bug class as
 * bug-39/bug-51).
 */

import { normalizeRepoSlug } from './github.js';

/** The one main-owner repository, normalized ("motionssalt/clipforge" by default). */
export function originalRepoFromEnv(env) {
  return normalizeRepoSlug((env && env.ORIGINAL_CLIPFORGE_REPOSITORY) || 'motionssalt/clipforge');
}

/**
 * True when the connected repo IS the original ClipForge repository — the
 * single main-owner account. Both sides are normalized before comparison so
 * trivially-malformed stored values still recognize the main account.
 */
export function isOriginalRepo(env, repo) {
  const normalized = normalizeRepoSlug(repo);
  return Boolean(normalized) && normalized === originalRepoFromEnv(env);
}
