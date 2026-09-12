-- remove-paste-feature: the multi-bubble production.json paste-reassembly
-- mechanism was deliberately removed (the uplb opcode, the receiving
-- indicator, keepPlanPasteAlive, looksLikePartialJson, and the storage.js
-- buffer accessors). This table held the in-flight paste fragments; nothing
-- reads or writes it anymore. The upload step is now file-upload-only: any
-- UTF-8-decodable text file, validated by parseAndValidateProductionPlan.
-- Migration 0003 stays in history (D1 migrations are applied sequentially
-- and migration files are never deleted); this drop is the forward path.
DROP TABLE IF EXISTS plan_upload_buffer;
