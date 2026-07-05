-- Vannie query log — one row per pilot query, for observability & triage.
-- The point: failures (not-found, abstain, chart-not-found, error) flag
-- themselves for review, so problems surface in a queue instead of being
-- discovered by chance in production.

create table if not exists vannie_query_log (
    id           bigint generated always as identity primary key,
    created_at   timestamptz  not null default now(),
    chat_hash    text,                       -- salted hash, not the real chat id
    query        text         not null,
    intent       text,                       -- extracted intent
    icao         text,                       -- resolved aerodrome, if any
    path         text,                       -- terminal path (grounded/chart/…)
    similarity   real,                       -- max retrieval similarity, if searched
    charts       int          default 0,     -- charts returned
    needs_review boolean      default false, -- true for non-confident outcomes
    airac        text
);

create index if not exists idx_qlog_created on vannie_query_log (created_at desc);
create index if not exists idx_qlog_review  on vannie_query_log (created_at desc)
    where needs_review;
create index if not exists idx_qlog_path    on vannie_query_log (path);

-- Weekly triage helper: the review queue, newest first.
--   select created_at, icao, path, query
--   from vannie_query_log
--   where needs_review and created_at > now() - interval '7 days'
--   order by created_at desc;
