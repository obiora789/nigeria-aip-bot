-- Short-lived conversation context so Vannie can (a) treat a bare "Lagos" as the
-- answer to its own "which aerodrome?" question, and (b) resolve a follow-up like
-- "what about the ILS?" against the last aerodrome. One row per chat, TTL-bounded.
-- Deliberately minimal and short-lived: carried context is a convenience, never a
-- source of truth, and it must expire before it can bleed into an unrelated query.

create table if not exists conversation_context (
    chat_hash  text primary key,
    last_icao  text,           -- last aerodrome resolved, for follow-ups
    pending    jsonb,          -- a request awaiting an aerodrome: {intent,proc,runway,raw}
    updated_at timestamptz not null default now(),
    expires_at timestamptz not null
);

create index if not exists idx_ctx_expires on conversation_context (expires_at);
