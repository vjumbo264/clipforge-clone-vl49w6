-- restore-bare-send-recognition: a MINIMAL, SCOPED, EXPIRING per-chat input
-- marker. This is deliberately NOT the legacy state.flow/pending blob: one
-- row per chat, written only when the bot sends a force_reply/flow-marker
-- prompt (one write per prompt-sent, never per keystroke or per menu
-- navigation), so that a bare send or a forward at an input step can be
-- routed exactly the way a genuine reply is. The reply_to_message edge
-- (parseFlowReply in flow.js) remains the PRIMARY path; this table is
-- consulted only when no reply edge exists, and genuine replies always win
-- when both signals are present.
--
-- awaiting_input: one row per chat (upserted).
--   op         — the SAME flow opcode vocabulary flow.js markers use
--                (wzs, upl, uplb, clname, patnew, patc, repo, gemkey, wm,
--                news, zkey, zsch, tpm, tppm, mupl) — no parallel vocabulary.
--   payload    — the SAME encoded 'cf:<op>:<arg…>' token flow.js embeds in
--                the prompt message — no new encoding.
--   expires_at — unix seconds; AWAITING_INPUT_TTL_SECONDS in storage.js
--                (15 min — RELAY_TTL_SECONDS = 12 h is a relay-handoff
--                convention, far too long for an input prompt). Expired rows
--                are deleted on read and are never honored.
-- This table is NOT an update-dedup mechanism (phase 6 stays removed) and
-- must never serve double duty as one.
CREATE TABLE IF NOT EXISTS awaiting_input (
  chat_id INTEGER PRIMARY KEY,
  op TEXT NOT NULL,
  payload TEXT,
  expires_at INTEGER NOT NULL
);
