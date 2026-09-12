/* ============================================================================
 * ClipForge Shadow Clone
 *
 * Client-side only. The source is fixed to motionssalt/clipforge. The visitor's
 * separately namespaced token is used only for their target repository.
 * ==========================================================================*/
(function () {
  'use strict';

  if (!document.body || document.body.getAttribute('data-page') !== 'settings') return;

  var API = 'https://api.github.com';
  var API_VERSION = '2022-11-28';
  const SOURCE_OWNER = 'motionssalt';
  const SOURCE_REPO = 'clipforge';
  const SOURCE_BRANCH = 'main';
  const SYNC_PATH = '.clipforge-sync.json';
  const EXCLUDE_PATTERNS = [
    /^branding\//,
    /^jobs\//,
    /^audio-library\//,
    /keys/i,
    /accounts/i,
    /queue/i
  ];
  var LS = {
    token: 'shadow_clone_token',
    owner: 'shadow_clone_owner',
    repo: 'shadow_clone_repo',
    syncedSha: 'shadow_clone_synced_sha',
    autoCheck: 'shadow_clone_auto_check'
  };

  function $(id) { return document.getElementById(id); }

  var el = {
    section: $('clone-section'),
    toggle: $('clone-toggle'),
    body: $('clone-body'),
    state: $('clone-state'),
    form: $('clone-form'),
    owner: $('clone-owner-input'),
    repo: $('clone-repo-input'),
    token: $('clone-token-input'),
    reveal: $('clone-token-reveal'),
    repoNote: $('clone-repo-note'),
    overwriteRow: $('clone-overwrite-row'),
    overwrite: $('clone-overwrite-input'),
    start: $('clone-start'),
    message: $('clone-msg'),
    summary: $('clone-summary'),
    repoLink: $('clone-repo-link'),
    lastSynced: $('clone-last-synced'),
    check: $('clone-check-updates'),
    autoCheck: $('clone-auto-check-input'),
    updates: $('clone-updates'),
    updateList: $('clone-update-list'),
    apply: $('clone-apply-updates')
  };

  if (!el.form) return;

  var state = {
    busy: false,
    token: '',
    owner: '',
    repo: '',
    user: null,
    target: null,
    preparedSignature: '',
    readyToClone: false,
    pendingChanges: [],
    syncedSha: '',
    syncedAt: '',
    pendingSyncedAt: '',
    sourceSnapshot: null
  };

  function isHidden(node) { return node && node.classList.contains('is-hidden'); }
  function show(node) { if (node) node.classList.remove('is-hidden'); }
  function hide(node) { if (node) node.classList.add('is-hidden'); }
  function text(node, value) { if (node) node.textContent = value; }

  function setMessage(message, kind) {
    if (!el.message) return;
    el.message.textContent = message || '';
    el.message.className = 'inline-msg' + (kind ? ' ' + kind : '');
  }

  function setState(label, ok) {
    text(el.state, label);
    el.state.classList.toggle('ok', !!ok);
  }

  function setBusy(busy, label) {
    state.busy = busy;
    el.owner.disabled = busy;
    el.repo.disabled = busy;
    el.token.disabled = busy;
    el.reveal.disabled = busy;
    el.overwrite.disabled = busy;
    el.check.disabled = busy;
    el.autoCheck.disabled = busy;
    el.apply.disabled = busy;
    refreshStartButton();
    if (busy && label) {
      el.start.textContent = label;
    } else if (!busy) {
      el.start.textContent = 'Clone ClipForge';
    }
  }

  function cloneSignature() {
    return [el.owner.value.trim(), el.repo.value.trim(), el.token.value.trim()].join('\u0000');
  }

  function hasDraft() {
    return !!(el.owner.value.trim() && el.repo.value.trim() && el.token.value.trim());
  }

  function invalidatePreparation() {
    state.preparedSignature = '';
    state.readyToClone = false;
    state.target = null;
    text(el.repoNote, '');
    el.overwrite.checked = false;
    hide(el.overwriteRow);
  }

  function refreshStartButton() {
    var signatureMatches = state.preparedSignature === cloneSignature();
    var needsOverwriteConfirmation = state.target && state.target.exists && state.target.nonEmpty;
    el.start.disabled = state.busy || !hasDraft() || (signatureMatches && needsOverwriteConfirmation && !el.overwrite.checked);
  }

  function b64decodeUtf8(value) {
    var binary = atob(String(value || '').replace(/\n/g, ''));
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new TextDecoder('utf-8').decode(bytes);
  }

  function b64encodeUtf8(value) {
    var bytes = new TextEncoder().encode(value);
    return bytesToBase64(bytes);
  }

  function bytesToBase64(bytes) {
    var binary = '';
    var chunk = 0x8000;
    for (var i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(binary);
  }

  function isExcluded(path) {
    return EXCLUDE_PATTERNS.some(function (pattern) { return pattern.test(path); });
  }

  function pathParts(path) {
    return String(path || '').split('/').map(encodeURIComponent).join('/');
  }

  function apiError(status, raw, data) {
    var error = new Error(raw || ('GitHub API error ' + status));
    error.name = 'GitHubApiError';
    error.status = status;
    error.raw = raw || ('GitHub API error ' + status);
    error.data = data || null;
    return error;
  }

  async function request(path, options) {
    options = options || {};
    var url = path.indexOf('http') === 0 ? path : API + path;
    var headers = {
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': API_VERSION
    };
    if (options.auth) headers.Authorization = 'Bearer ' + state.token;
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';

    var response;
    try {
      response = await fetch(url, {
        method: options.method || 'GET',
        headers: headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        cache: 'no-store'
      });
    } catch (error) {
      throw apiError(0, error && error.message ? error.message : String(error));
    }

    var raw = '';
    var data = null;
    try {
      raw = await response.text();
      data = raw ? JSON.parse(raw) : null;
    } catch (ignored) {
      data = null;
    }

    if (!response.ok) {
      var message = data && data.message ? data.message : (raw || ('GitHub API error ' + response.status));
      throw apiError(response.status, message, data);
    }
    return { data: data, response: response };
  }

  function scopeError(error) {
    if (error && (error.status === 401 || error.status === 403)) {
      return 'Token needs `repo` scope, or fine-grained Administration: write and Contents: write. ' + error.raw;
    }
    return error && error.raw ? error.raw : String(error || 'Unknown error');
  }

  async function validateToken() {
    var result;
    try {
      result = await request('/user', { auth: true });
    } catch (error) {
      if (error.status === 401) throw apiError(401, 'Invalid token. ' + error.raw, error.data);
      throw error;
    }
    var scopes = String(result.response.headers.get('x-oauth-scopes') || '').split(',').map(function (value) {
      return value.trim();
    }).filter(Boolean);
    if (scopes.length && scopes.indexOf('repo') === -1) {
      throw apiError(403, 'Token needs `repo` scope. GitHub reported scopes: ' + scopes.join(', ') + '.', result.data);
    }
    return result.data;
  }

  async function targetRepo(owner, repo) {
    try {
      var repository = await request('/repos/' + encodeURIComponent(owner) + '/' + encodeURIComponent(repo), { auth: true });
      var nonEmpty = false;
      try {
        var contents = await request('/repos/' + encodeURIComponent(owner) + '/' + encodeURIComponent(repo) + '/contents/', { auth: true });
        nonEmpty = Array.isArray(contents.data) ? contents.data.length > 0 : !!contents.data;
      } catch (error) {
        if (error.status !== 404) throw error;
      }
      return { exists: true, nonEmpty: nonEmpty, repository: repository.data };
    } catch (error) {
      if (error.status === 404) return { exists: false, nonEmpty: false, repository: null };
      throw error;
    }
  }

  async function createTargetRepo(owner, repo, user) {
    var endpoint = String(owner).toLowerCase() === String(user.login || '').toLowerCase()
      ? '/user/repos'
      : '/orgs/' + encodeURIComponent(owner) + '/repos';
    var created = await request(endpoint, {
      method: 'POST',
      auth: true,
      body: { name: repo, private: false }
    });
    return created.data;
  }

  async function sourceSnapshot() {
    var ref = await request('/repos/' + SOURCE_OWNER + '/' + SOURCE_REPO + '/git/refs/heads/' + SOURCE_BRANCH);
    var sha = ref.data && ref.data.object && ref.data.object.sha;
    if (!sha) throw apiError(0, 'GitHub source ref response did not include a commit SHA.');
    var commit = await request('/repos/' + SOURCE_OWNER + '/' + SOURCE_REPO + '/git/commits/' + sha);
    var treeSha = commit.data && commit.data.tree && commit.data.tree.sha;
    if (!treeSha) throw apiError(0, 'GitHub source commit response did not include a tree SHA.');
    var tree = await request('/repos/' + SOURCE_OWNER + '/' + SOURCE_REPO + '/git/trees/' + treeSha + '?recursive=1');
    var files = (tree.data && tree.data.tree ? tree.data.tree : []).filter(function (entry) {
      return entry && entry.type === 'blob' && entry.path && !isExcluded(entry.path);
    });
    if (tree.data && tree.data.truncated) throw apiError(0, 'Source tree response was truncated; Shadow Clone cannot safely continue.');
    return { sha: sha, treeSha: treeSha, files: files };
  }

  async function sourceBlobBase64(sha) {
    var result = await request('/repos/' + SOURCE_OWNER + '/' + SOURCE_REPO + '/git/blobs/' + sha);
    if (!result.data || result.data.encoding !== 'base64' || !result.data.content) {
      throw apiError(0, 'Source blob response was not base64 encoded.');
    }
    return String(result.data.content).replace(/\n/g, '');
  }

  async function createTargetBlob(base64) {
    var result = await request('/repos/' + encodeURIComponent(state.owner) + '/' + encodeURIComponent(state.repo) + '/git/blobs', {
      method: 'POST',
      auth: true,
      body: { content: base64, encoding: 'base64' }
    });
    if (!result.data || !result.data.sha) throw apiError(0, 'Target blob response did not include a SHA.');
    return result.data.sha;
  }

  async function targetMainRef() {
    try {
      var ref = await request('/repos/' + encodeURIComponent(state.owner) + '/' + encodeURIComponent(state.repo) + '/git/refs/heads/main', { auth: true });
      return ref.data;
    } catch (error) {
      if (error.status === 404) return null;
      throw error;
    }
  }

  async function targetBaseTree(ref) {
    if (!ref || !ref.object || !ref.object.sha) return null;
    var commit = await request('/repos/' + encodeURIComponent(state.owner) + '/' + encodeURIComponent(state.repo) + '/git/commits/' + ref.object.sha, { auth: true });
    return commit.data && commit.data.tree ? commit.data.tree.sha : null;
  }

  async function writeTreeCommit(entries, message, parentRef) {
    var body = { tree: entries };
    var baseTree = await targetBaseTree(parentRef);
    if (baseTree) body.base_tree = baseTree;
    var tree = await request('/repos/' + encodeURIComponent(state.owner) + '/' + encodeURIComponent(state.repo) + '/git/trees', {
      method: 'POST', auth: true, body: body
    });
    if (!tree.data || !tree.data.sha) throw apiError(0, 'Target tree response did not include a SHA.');

    var commitBody = { message: message, tree: tree.data.sha, parents: parentRef && parentRef.object && parentRef.object.sha ? [parentRef.object.sha] : [] };
    var commit = await request('/repos/' + encodeURIComponent(state.owner) + '/' + encodeURIComponent(state.repo) + '/git/commits', {
      method: 'POST', auth: true, body: commitBody
    });
    if (!commit.data || !commit.data.sha) throw apiError(0, 'Target commit response did not include a SHA.');

    if (parentRef) {
      await request('/repos/' + encodeURIComponent(state.owner) + '/' + encodeURIComponent(state.repo) + '/git/refs/heads/main', {
        method: 'PATCH', auth: true, body: { sha: commit.data.sha }
      });
    } else {
      await request('/repos/' + encodeURIComponent(state.owner) + '/' + encodeURIComponent(state.repo) + '/git/refs', {
        method: 'POST', auth: true, body: { ref: 'refs/heads/main', sha: commit.data.sha }
      });
    }
    return commit.data.sha;
  }

  function syncFileEntry(sourceSha) {
    var payload = {
      source: SOURCE_OWNER + '/' + SOURCE_REPO,
      synced_sha: sourceSha,
      synced_at: new Date().toISOString()
    };
    state.pendingSyncedAt = payload.synced_at;
    return createTargetBlob(b64encodeUtf8(JSON.stringify(payload, null, 2) + '\n')).then(function (sha) {
      return { path: SYNC_PATH, mode: '100644', type: 'blob', sha: sha };
    });
  }

  async function readSyncState() {
    var remoteSha = '';
    var remoteSyncedAt = '';
    try {
      var file = await request('/repos/' + encodeURIComponent(state.owner) + '/' + encodeURIComponent(state.repo) + '/contents/' + SYNC_PATH + '?ref=main', { auth: true });
      var parsed = JSON.parse(b64decodeUtf8(file.data && file.data.content));
      if (parsed && parsed.source === SOURCE_OWNER + '/' + SOURCE_REPO && typeof parsed.synced_sha === 'string') {
        remoteSha = parsed.synced_sha;
        remoteSyncedAt = typeof parsed.synced_at === 'string' ? parsed.synced_at : '';
      }
    } catch (error) {
      if (error.status !== 404) {
        // The local copy remains a deliberate fallback when remote sync state is unavailable.
        remoteSha = '';
      }
    }
    var localSha = localStorage.getItem(LS.syncedSha) || '';
    var selected = remoteSha || localSha;
    if (selected) localStorage.setItem(LS.syncedSha, selected);
    state.syncedSha = selected;
    state.syncedAt = remoteSyncedAt || state.syncedAt;
    return selected;
  }

  function persistSuccessfulClone(sourceSha) {
    localStorage.setItem(LS.token, state.token);
    localStorage.setItem(LS.owner, state.owner);
    localStorage.setItem(LS.repo, state.repo);
    localStorage.setItem(LS.syncedSha, sourceSha);
    state.syncedSha = sourceSha;
    state.syncedAt = state.pendingSyncedAt || new Date().toISOString();
  }

  function formatDate(value) {
    if (!value) return '—';
    var date = new Date(value);
    return isNaN(date.getTime()) ? '—' : date.toLocaleString();
  }

  async function prepareClone() {
    setBusy(true, 'Validating…');
    setMessage('Validating token…');
    try {
      state.token = el.token.value.trim();
      state.owner = el.owner.value.trim();
      state.repo = el.repo.value.trim();
      state.user = await validateToken();
      var target = await targetRepo(state.owner, state.repo);
      state.target = target;
      state.preparedSignature = cloneSignature();
      state.readyToClone = false;
      if (!target.exists) {
        text(el.repoNote, "This repo doesn't exist yet — it will be created (public).");
        setMessage('Target validated. Review the note, then click Clone ClipForge again.', 'ok');
      } else if (target.nonEmpty) {
        text(el.repoNote, 'This repo already has files. Confirm the overwrite warning before cloning.');
        show(el.overwriteRow);
        setMessage('Target validated. Confirmation is required before cloning.', 'bad');
      } else {
        text(el.repoNote, 'This empty repo is ready for Shadow Clone.');
        setMessage('Target validated. Click Clone ClipForge again to continue.', 'ok');
      }
      state.readyToClone = true;
    } catch (error) {
      state.preparedSignature = '';
      state.readyToClone = false;
      setMessage(scopeError(error), 'bad');
    } finally {
      setBusy(false);
    }
  }

  async function cloneSource() {
    setBusy(true, 'Cloning…');
    try {
      var target = state.target;
      if (!target || state.preparedSignature !== cloneSignature()) {
        throw apiError(0, 'Target settings changed. Validate the target again before cloning.');
      }
      if (target.exists && target.nonEmpty && !el.overwrite.checked) {
        throw apiError(0, 'This repo already has files — confirm the overwrite warning before cloning.');
      }
      if (!target.exists) {
        text(el.repoNote, 'Creating public target repository…');
        target.repository = await createTargetRepo(state.owner, state.repo, state.user);
        target.exists = true;
      }

      text(el.repoNote, 'Reading source files…');
      var snapshot = await sourceSnapshot();
      state.sourceSnapshot = snapshot;
      var entries = [];
      for (var i = 0; i < snapshot.files.length; i++) {
        var sourceFile = snapshot.files[i];
        text(el.repoNote, 'Writing to your repo… ' + (i + 1) + '/' + snapshot.files.length);
        var base64 = await sourceBlobBase64(sourceFile.sha);
        var targetBlobSha = await createTargetBlob(base64);
        entries.push({ path: sourceFile.path, mode: sourceFile.mode || '100644', type: 'blob', sha: targetBlobSha });
      }
      entries.push(await syncFileEntry(snapshot.sha));
      var parentRef = await targetMainRef();
      await writeTreeCommit(entries, 'Shadow clone from ' + SOURCE_OWNER + '/' + SOURCE_REPO + '@' + snapshot.sha.slice(0, 7), parentRef);
      persistSuccessfulClone(snapshot.sha);
      renderConfigured();
      setMessage('Shadow Clone completed successfully.', 'ok');
    } catch (error) {
      setMessage(scopeError(error), 'bad');
    } finally {
      setBusy(false);
    }
  }

  function filterChanges(files) {
    return (files || []).filter(function (file) {
      return file && file.filename && !isExcluded(file.filename) && !(file.previous_filename && isExcluded(file.previous_filename));
    }).map(function (file) {
      var status = file.status === 'renamed' ? 'modified' : file.status;
      return {
        path: file.filename,
        status: status,
        previousPath: file.status === 'renamed' ? file.previous_filename : null
      };
    });
  }

  function renderChanges(changes) {
    el.updateList.innerHTML = '';
    changes.forEach(function (change) {
      var item = document.createElement('li');
      var pill = document.createElement('span');
      pill.className = 'pill';
      pill.textContent = change.status;
      var path = document.createElement('code');
      path.textContent = change.path;
      item.appendChild(pill);
      item.appendChild(document.createTextNode(' '));
      item.appendChild(path);
      el.updateList.appendChild(item);
    });
  }

  async function checkForUpdates(options) {
    options = options || {};
    if (!state.token || !state.owner || !state.repo) return;
    state.pendingChanges = [];
    hide(el.updates);
    el.check.disabled = true;
    el.check.textContent = 'Checking…';
    setMessage('Checking for updates…');
    try {
      var syncedSha = await readSyncState();
      if (!syncedSha) throw apiError(0, 'No saved sync SHA was found in this browser or in ' + SYNC_PATH + '. Clone ClipForge first.');
      var snapshot = await sourceSnapshot();
      state.sourceSnapshot = snapshot;
      if (snapshot.sha === syncedSha) {
        setMessage("You're up to date", 'ok');
        return;
      }
      var comparison = await request('/repos/' + SOURCE_OWNER + '/' + SOURCE_REPO + '/compare/' + encodeURIComponent(syncedSha) + '...' + encodeURIComponent(snapshot.sha));
      var changes = filterChanges(comparison.data && comparison.data.files);
      if (!changes.length) {
        localStorage.setItem(LS.syncedSha, snapshot.sha);
        state.syncedSha = snapshot.sha;
        setMessage("You're up to date", 'ok');
        return;
      }
      state.pendingChanges = changes;
      renderChanges(changes);
      show(el.updates);
      setMessage(changes.length + ' update' + (changes.length === 1 ? '' : 's') + ' available. Review and apply when ready.', 'ok');
    } catch (error) {
      setMessage(scopeError(error), 'bad');
    } finally {
      el.check.disabled = state.busy;
      el.check.textContent = 'Check for updates';
    }
  }

  async function applyUpdates() {
    if (!state.pendingChanges.length || !state.sourceSnapshot) return;
    setBusy(true);
    el.apply.textContent = 'Applying update…';
    setMessage('Applying update…');
    try {
      var snapshot = state.sourceSnapshot;
      var sourceByPath = {};
      snapshot.files.forEach(function (file) { sourceByPath[file.path] = file; });
      var entries = [];
      for (var i = 0; i < state.pendingChanges.length; i++) {
        var change = state.pendingChanges[i];
        setMessage('Applying update… ' + (i + 1) + '/' + state.pendingChanges.length);
        if (change.previousPath) entries.push({ path: change.previousPath, mode: '100644', type: 'blob', sha: null });
        if (change.status === 'removed') {
          entries.push({ path: change.path, mode: '100644', type: 'blob', sha: null });
          continue;
        }
        var sourceFile = sourceByPath[change.path];
        if (!sourceFile) throw apiError(0, 'Updated source file was not found in the current source tree: ' + change.path);
        var base64 = await sourceBlobBase64(sourceFile.sha);
        var targetBlobSha = await createTargetBlob(base64);
        entries.push({ path: change.path, mode: sourceFile.mode || '100644', type: 'blob', sha: targetBlobSha });
      }
      entries.push(await syncFileEntry(snapshot.sha));
      var parentRef = await targetMainRef();
      if (!parentRef) throw apiError(0, 'Target repository main branch is missing; Shadow Clone cannot apply an update.');
      await writeTreeCommit(entries, 'Update from ' + SOURCE_OWNER + '/' + SOURCE_REPO + '@' + snapshot.sha.slice(0, 7), parentRef);
      localStorage.setItem(LS.syncedSha, snapshot.sha);
      state.syncedSha = snapshot.sha;
      state.syncedAt = state.pendingSyncedAt || new Date().toISOString();
      state.pendingChanges = [];
      hide(el.updates);
      renderConfigured();
      setMessage('Update applied successfully.', 'ok');
    } catch (error) {
      setMessage(scopeError(error), 'bad');
    } finally {
      el.apply.textContent = 'Apply update';
      setBusy(false);
    }
  }

  function renderConfigured() {
    var hasClone = !!(state.token && state.owner && state.repo);
    if (!hasClone) {
      setState('not set up', false);
      show(el.form);
      hide(el.summary);
      return;
    }
    setState('cloned', true);
    hide(el.form);
    show(el.summary);
    var href = 'https://github.com/' + encodeURIComponent(state.owner) + '/' + encodeURIComponent(state.repo);
    el.repoLink.href = href;
    el.repoLink.textContent = state.owner + '/' + state.repo;
    text(el.lastSynced, 'Last synced: ' + (state.syncedAt ? formatDate(state.syncedAt) : '—'));
    el.autoCheck.checked = localStorage.getItem(LS.autoCheck) === 'true';
  }

  async function loadStoredState() {
    state.token = localStorage.getItem(LS.token) || '';
    state.owner = localStorage.getItem(LS.owner) || '';
    state.repo = localStorage.getItem(LS.repo) || '';
    state.syncedSha = localStorage.getItem(LS.syncedSha) || '';
    state.syncedAt = '';
    el.token.value = state.token;
    el.owner.value = state.owner;
    el.repo.value = state.repo;
    renderConfigured();
    if (state.token && state.owner && state.repo) {
      try {
        await readSyncState();
        renderConfigured();
        if (localStorage.getItem(LS.autoCheck) === 'true') await checkForUpdates({ automatic: true });
      } catch (error) {
        setMessage(scopeError(error), 'bad');
      }
    }
    refreshStartButton();
  }

  el.toggle.addEventListener('click', function () {
    var open = el.toggle.getAttribute('aria-expanded') === 'true';
    el.toggle.setAttribute('aria-expanded', open ? 'false' : 'true');
    if (open) hide(el.body); else show(el.body);
  });

  el.reveal.addEventListener('click', function () {
    var shown = el.token.type === 'text';
    el.token.type = shown ? 'password' : 'text';
    el.reveal.textContent = shown ? 'Show' : 'Hide';
    el.reveal.setAttribute('aria-pressed', shown ? 'false' : 'true');
  });

  [el.owner, el.repo, el.token].forEach(function (input) {
    input.addEventListener('input', function () {
      invalidatePreparation();
      refreshStartButton();
    });
  });

  el.overwrite.addEventListener('change', refreshStartButton);

  el.form.addEventListener('submit', async function (event) {
    event.preventDefault();
    if (!hasDraft() || state.busy) return;
    if (!state.readyToClone || state.preparedSignature !== cloneSignature()) {
      await prepareClone();
      return;
    }
    await cloneSource();
  });

  el.check.addEventListener('click', function () { checkForUpdates(); });
  el.apply.addEventListener('click', function () { applyUpdates(); });
  el.autoCheck.addEventListener('change', function () {
    localStorage.setItem(LS.autoCheck, el.autoCheck.checked ? 'true' : 'false');
  });

  loadStoredState();
}());
