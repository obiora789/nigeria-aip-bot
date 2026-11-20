-- 14_audit_trail.sql — audit trail for every logged question (2026-10-27).
--
-- Run once in Supabase (SQL editor). Safe to run again.
--
-- With the AIRAC cycle (already logged) these let any reply be explained and
-- reproduced later: what Vannie said, which models said it, which prompts and
-- output schemas they were given, and which deployed commit was running.
-- Until this has run, observability.log_query keeps logging without these
-- columns and the logs say "run sql/14_audit_trail.sql". Restart after running.

alter table vannie_query_log
    add column if not exists reply          text,   -- the text Vannie sent (first 8000 chars)
    add column if not exists models         text,   -- e.g. extract=gpt-4o-mini-2024-07-18;synth=...;embed=...
    add column if not exists prompt_version text,   -- fingerprint of all prompts + output schemas
    add column if not exists app_version    text;   -- deployed git commit (RENDER_GIT_COMMIT)

create index if not exists idx_qlog_prompt_version on vannie_query_log (prompt_version);

-- /deletemydata deletes by chat_hash. (The daily retention pass deletes by
-- created_at, which sql/05_query_log.sql already indexes.)
create index if not exists idx_qlog_chat_hash on vannie_query_log (chat_hash);
