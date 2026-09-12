-- paste-fix session 3 (fix-json-reassembly-definitively): the accumulated
-- production.json fragment buffer for an in-flight multi-bubble paste used to
-- ride INSIDE the uplb flow marker as base64url JSON
-- (cf:uplb:<label>:<b64token>). That marker lives in the (receiving)
-- indicator's Telegram message text, which is capped at 4096 UTF-16 units --
-- a multi-KB paste pushed the marker past the cap, Telegram rejected the
-- indicator send/edit ('message is too long'), the awaiting_input row was
-- never re-anchored, and the next bare bubble re-entered with an EMPTY
-- buffer, so the trailing fragment was parsed alone and the operator saw
-- 'Unexpected token ...' on a valid plan. The buffer now lives in D1; the
-- marker is a constant-size routing token (cf:uplb:<label>).
CREATE TABLE IF NOT EXISTS plan_upload_buffer (
  chat_id INTEGER PRIMARY KEY NOT NULL,
  fragments TEXT NOT NULL,   -- JSON array of raw fragment strings, arrival order
  bubble_ids TEXT NOT NULL,  -- JSON array of Telegram message ids (bug-27/38 cleanup)
  updated_at INTEGER NOT NULL -- unix seconds; diagnostic only (no TTL logic)
);
