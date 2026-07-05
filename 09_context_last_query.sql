-- Remember the last query text so a bare follow-up ("can you list them?")
-- inherits the previous topic, not just the previous aerodrome.
alter table conversation_context
    add column if not exists last_query text;
