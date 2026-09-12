// In-memory CLIPFORGE_BOT_D1 harness for unit tests.
//
// kv-minimization migration: storage.js now talks to a Cloudflare D1
// database instead of KV for task labels/options, clone jobs, and
// announcement markers. The bot's tests run under plain `node --test` (no
// workerd/miniflare), so this helper implements the exact Workers D1 API
// surface the code uses — prepare(sql).bind(...).{all,first,run}() and
// batch([...]) — on top of node:sqlite's DatabaseSync, with the REAL
// migration files (bot/migrations/*.sql, applied in filename order) as the
// schema. Tests therefore exercise the same SQL the worker executes, not a
// re-imagining of it. (restore-bare-send-recognition: the helper originally
// loaded ONLY 0001_initial.sql; it now loads the whole migrations directory
// so the 0002_awaiting_input.sql table exists in tests too.)

import { DatabaseSync } from 'node:sqlite';
import { readdirSync, readFileSync } from 'node:fs';

const MIGRATIONS_DIR = new URL('../../migrations/', import.meta.url);

export function makeD1() {
  const db = new DatabaseSync(':memory:');
  const migrationFiles = readdirSync(MIGRATIONS_DIR).filter((name) => name.endsWith('.sql')).sort();
  for (const file of migrationFiles) {
    db.exec(readFileSync(new URL(`../../migrations/${file}`, import.meta.url), 'utf8'));
  }

  function prepare(sql) {
    const stmt = db.prepare(sql);
    let bound = [];
    const api = {
      bind(...values) { bound = values; return api; },
      async all() {
        const rows = stmt.all(...bound);
        return { results: rows, success: true, meta: { changes: 0 } };
      },
      async first() {
        const row = stmt.get(...bound);
        return row === undefined ? null : row;
      },
      async run() {
        const info = stmt.run(...bound);
        return { success: true, meta: { changes: Number(info.changes), last_row_id: Number(info.lastInsertRowid) } };
      },
    };
    return api;
  }

  return {
    prepare,
    async batch(statements) {
      db.exec('BEGIN');
      const results = [];
      try {
        for (const statement of statements) results.push(await statement.run());
        db.exec('COMMIT');
      } catch (error) {
        db.exec('ROLLBACK');
        throw error;
      }
      return results;
    },
    // Escape hatch for assertions: run a read query directly.
    _query(sql, ...params) { return db.prepare(sql).all(...params); },
  };
}
