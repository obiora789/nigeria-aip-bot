-- Add a review-completion flag so weekly triage CONVERGES: once you've acted on
-- a failing query you mark it reviewed, and it drops out of the open queue. Only
-- new failures show up next week, instead of re-seeing everything.

alter table vannie_query_log
    add column if not exists reviewed      boolean not null default false,
    add column if not exists reviewed_at   timestamptz,
    add column if not exists reviewed_note text;

-- The "open" queue: flagged for review and not yet handled.
create index if not exists idx_qlog_open on vannie_query_log (created_at desc)
    where needs_review and not reviewed;

-- Open queue query:
--   select id, created_at, icao, path, query
--   from vannie_query_log
--   where needs_review and not reviewed
--   order by created_at desc;
