-- Feedback: a 👍/👎 on an answer is ground-truth signal. We tag each logged
-- query with a short qid, put that qid in the feedback buttons, and when a pilot
-- taps 👎 we flag that exact query for review — real wrong answers surface
-- themselves into the triage queue.

alter table vannie_query_log
    add column if not exists qid      text,
    add column if not exists feedback text;   -- 'up' | 'down' | null

create index if not exists idx_qlog_qid on vannie_query_log (qid);
