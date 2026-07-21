-- sql/11_aip_facts.sql
--
-- Field-level retrieval index for AD 2.x.
--
-- WHY: aip_knowledge_base holds ONE vector per (aerodrome, subsection).
-- DNMM's AD 2.22 is 79,871 characters in a single embedding — an average of
-- holding procedures, minima tables, radar procedures, VFR rules and PBN
-- coding. An average of everything is close to nothing, which is measurably
-- why retrieval failed: "lateral limit for lagos ctr" -> ENR 3.1 @ 59%,
-- "Abuja runway" -> AD 2.22 minima table @ 55%.
--
-- This table stores one row per FIELD instead. Each row is atomic and
-- self-describing: it names its aerodrome, subsection, entity (runway end,
-- service, navaid) and label. Retrieval matches a query against a sentence
-- that is nearly the query itself, so no keyword routing is needed to
-- compensate for a blurry vector.
--
-- Misattribution stays impossible: the entity is PART of the retrieved unit,
-- not reconstructed afterwards, so one runway's PCN can never be served as
-- another's.

create extension if not exists vector;

create table if not exists aip_facts (
    id           bigserial primary key,
    icao_code    text        not null,
    subsection   text        not null,          -- '2.12', '2.17', ...
    entity       text        not null default '',  -- 'RWY 18L', 'Lagos Tower', ''
    label        text        not null,          -- 'Strength (PCN) and surface'
    fact_value   text        not null,          -- the answer itself
    fact_text    text        not null,          -- the embedded sentence
    embedding    vector(1536),
    created_at   timestamptz not null default now()
);

-- One row per (aerodrome, subsection, entity, label) — makes re-runs
-- idempotent and lets build_fact_index.py upsert safely.
--
-- NOTE: this must be a PLAIN unique index on exactly these four columns.
-- A functional index (e.g. coalesce(entity,'')) would not satisfy
-- PostgREST's on_conflict=..., and every upsert would fail. That is why
-- `entity` is NOT NULL DEFAULT '' rather than nullable.
create unique index if not exists uq_aip_facts_key
    on aip_facts (icao_code, subsection, entity, label);

create index if not exists ix_aip_facts_icao on aip_facts (icao_code);
create index if not exists ix_aip_facts_sub  on aip_facts (icao_code, subsection);

-- ANN index. Lists ~ sqrt(rows); ~8k facts -> 100 is comfortable.
create index if not exists ix_aip_facts_embedding
    on aip_facts using ivfflat (embedding vector_cosine_ops) with (lists = 100);


-- Retrieval RPC. Always scoped to ONE aerodrome, so a fact can never be
-- returned for a different airport — the same guarantee the rest of the
-- pipeline enforces, kept at the database boundary.
create or replace function match_aip_facts(
    query_embedding vector(1536),
    p_icao          text,
    p_subsection    text default null,
    match_limit     int  default 8
)
returns table (
    subsection text,
    entity     text,
    label      text,
    fact_value text,
    fact_text  text,
    similarity float
)
language sql stable
as $$
    select f.subsection, f.entity, f.label, f.fact_value, f.fact_text,
           1 - (f.embedding <=> query_embedding) as similarity
    from aip_facts f
    where f.icao_code = p_icao
      and (p_subsection is null or f.subsection = p_subsection)
    order by f.embedding <=> query_embedding
    limit match_limit;
$$;
