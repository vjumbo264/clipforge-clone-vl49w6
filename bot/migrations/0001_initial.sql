-- kv-minimization migration, phase 0: D1 schema replacing CLIPFORGE_BOT_KV for
-- task labels/options, Shadow Clone job resume state, and announcement markers.
-- KV remains ONLY for credentials, relay handoff records, and Bot B relay dedup.
--
-- task_labels: one row per (chat, label); labels are the user-facing A, B, C…
-- letters that map to job ids (bug-2 lowest-free-label reuse is implemented in
-- storage.js over this table's rows, unchanged semantics, new backend).
CREATE TABLE IF NOT EXISTS task_labels (
  chat_id INTEGER NOT NULL,
  label TEXT NOT NULL,
  job_id TEXT NOT NULL,
  PRIMARY KEY (chat_id, label)
);

-- task_options: per (chat, job) options blob. NOTE: the legacy `seen` field is
-- intentionally NOT carried into D1 — it is deleted as a feature in phase 4.
CREATE TABLE IF NOT EXISTS task_options (
  chat_id INTEGER NOT NULL,
  job_id TEXT NOT NULL,
  options_json TEXT NOT NULL,
  PRIMARY KEY (chat_id, job_id)
);

-- clone_jobs: Shadow Clone job resume state (bug-51). pat_envelope is the same
-- AES-256-GCM envelope crypto.js produces for KV (AAD per chat) — only the
-- storage location changes, not the encryption. A row's existence IS the cron
-- sweep's scan set; the old CLONE_JOB_INDEX_KEY KV set is dropped entirely.
CREATE TABLE IF NOT EXISTS clone_jobs (
  chat_id INTEGER PRIMARY KEY,
  pat_envelope TEXT NOT NULL,
  repo TEXT NOT NULL,
  login TEXT NOT NULL,
  name TEXT NOT NULL,
  branch TEXT NOT NULL,
  source_sha TEXT NOT NULL,
  bootstrap_commit_sha TEXT NOT NULL,
  total_files INTEGER NOT NULL,
  started_at INTEGER NOT NULL,
  last_advance_at INTEGER NOT NULL,
  last_status_key TEXT NOT NULL,
  run_id TEXT,
  finalize_failed_at INTEGER NOT NULL
);

-- announcements: one row per (chat, kind); upsert on write. kind is one of
-- 'update_notice' (bug-31), 'deploy_failure' (bug-68), 'news_notice' (bug-46).
CREATE TABLE IF NOT EXISTS announcements (
  chat_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('update_notice', 'deploy_failure', 'news_notice')),
  marker TEXT NOT NULL,
  PRIMARY KEY (chat_id, kind)
);
