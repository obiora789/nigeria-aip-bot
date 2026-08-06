-- sql/12_scope_generalisation.sql
--
-- Generalise aip_facts from AERODROME-ONLY to ANY AIP ENTITY.
--
-- WHY
-- ---
-- A pilot asked "Where is TEMSA?" and Vannie answered "I don't have 'TEMSA' in
-- the Nigerian AIP." TEMSA is in the AIP, on seven pages: an en-route
-- significant point at 06°49'12"N 002°44'42"E on airway UT467. The refusal was
-- not a bug in retrieval — it is structural. Every layer of this pipeline is
-- keyed on icao_code:
--
--     aip_facts.icao_code            NOT NULL
--     uq_aip_facts_key               (icao_code, subsection, entity, label)
--     match_aip_facts(p_icao ...)    scoped to one aerodrome at the DB boundary
--     resolver.resolve()             matches names against 40 AERODROMES
--
-- So no waypoint, airway or danger area can be stored, retrieved or named,
-- however well it is extracted. 152 of the AIP's 1,073 pages are ENR, holding
-- 212 waypoints, 39 airways and 57 prohibited/restricted/danger areas — over
-- 300 entities that cannot be asked about.
--
-- WHAT CHANGES
-- ------------
-- The key becomes (scope_kind, scope_id) instead of icao_code:
--
--     scope_kind      scope_id        example
--     AD              ICAO code       DNMM
--     ENR_AREA        area id         DND45
--     ENR_POINT       waypoint name   TEMSA
--     ENR_ROUTE       airway          UT467
--     ENR_AIRSPACE    FIR/TMA name    KANO FIR
--
-- Nothing else in the design changes, because nothing else actually cares that
-- a scope is an aerodrome — the safety property is "retrieval is confined to
-- ONE entity", and that holds for a waypoint exactly as it does for a runway.
--
-- ADDITIVE AND REVERSIBLE, DELIBERATELY
-- -------------------------------------
-- 5,289 live rows are in this table and the bot serves from it. So:
--   * new columns are added and BACKFILLED, never renamed
--   * icao_code is KEPT and kept in sync by trigger, so any code still reading
--     it continues to work unchanged during the transition
--   * the old 4-column unique index is kept until the new one is proven, so
--     build_fact_index.py's existing on_conflict target stays valid
--   * match_aip_facts keeps its exact current signature; a NEW overload takes
--     the scope pair
-- Every step is safe to run twice. Roll back by dropping the new objects; the
-- original columns and indexes are untouched.
--
-- RUN ORDER
--   1. this file            (schema + backfill + new RPC)
--   2. verify block at foot (must print 0 unscoped rows)
--   3. deploy code that writes scope_kind/scope_id
--   4. python build_fact_index.py --force        (rebuild AD on the new key)
--   5. 13_scope_cutover.sql (drops the legacy index)
--   6. python build_fact_index.py --enr 5.1      (load ENR)
--
-- STEP 5 MUST PRECEDE STEP 6, and that ordering was got wrong once. The
-- legacy index uq_aip_facts_key is (icao_code, subsection, entity, label).
-- Every ENR row has icao_code = its scope marker and entity = '', so DNP1's
-- "Name" and DNP2's "Name" collide under it. A real load produced:
--     wrote 3/313 facts
--     duplicate key value violates unique constraint "uq_aip_facts_key"
-- The constraint was working correctly — it simply encodes the assumption
-- that a fact belongs to an aerodrome, which is what this migration retires.

begin;

-- ---------------------------------------------------------------------------
-- 1. Columns
-- ---------------------------------------------------------------------------
alter table aip_facts add column if not exists scope_kind text;
alter table aip_facts add column if not exists scope_id   text;

-- ---------------------------------------------------------------------------
-- 2. Backfill every existing row as an aerodrome fact
-- ---------------------------------------------------------------------------
update aip_facts
   set scope_kind = 'AD',
       scope_id   = icao_code
 where scope_kind is null
    or scope_id   is null;

-- ---------------------------------------------------------------------------
-- 3. Constraints, only after the backfill has run
-- ---------------------------------------------------------------------------
alter table aip_facts alter column scope_kind set not null;
alter table aip_facts alter column scope_id   set not null;

-- The permitted scopes are enumerated, not free text. An unrecognised scope
-- would silently create a partition of facts that nothing can retrieve — the
-- same class of invisible gap that made ENR unreachable in the first place.
alter table aip_facts drop constraint if exists ck_aip_facts_scope_kind;
alter table aip_facts add  constraint ck_aip_facts_scope_kind
    check (scope_kind in ('AD', 'ENR_AREA', 'ENR_POINT',
                          'ENR_ROUTE', 'ENR_AIRSPACE', 'GEN'));

-- An AD-scoped fact must still carry its ICAO, and it must agree with
-- scope_id. Without this a row could claim scope ('AD','DNMM') while
-- icao_code said DNAA, and the two retrieval paths would disagree about which
-- aerodrome it belongs to — a misattribution introduced by the migration
-- itself.
alter table aip_facts drop constraint if exists ck_aip_facts_ad_icao;
alter table aip_facts add  constraint ck_aip_facts_ad_icao
    check (scope_kind <> 'AD' or (icao_code is not null and icao_code = scope_id));

-- ---------------------------------------------------------------------------
-- 4. Keep icao_code in sync, so code that has not migrated keeps working
-- ---------------------------------------------------------------------------
-- icao_code is NOT NULL, so a non-aerodrome row cannot leave it empty. It is
-- set to the scope_id for AD rows and to the scope_kind marker otherwise,
-- which keeps the column meaningful for AD queries and obviously non-ICAO for
-- everything else (no code will mistake 'ENR_AREA' for an aerodrome).
create or replace function aip_facts_sync_scope() returns trigger as $$
begin
    if new.scope_kind is null and new.icao_code is not null then
        new.scope_kind := 'AD';
        new.scope_id   := new.icao_code;
    end if;
    if new.scope_kind = 'AD' then
        new.icao_code := coalesce(new.icao_code, new.scope_id);
        new.scope_id  := coalesce(new.scope_id, new.icao_code);
    else
        new.icao_code := coalesce(new.icao_code, new.scope_kind);
    end if;
    return new;
end;
$$ language plpgsql;

drop trigger if exists trg_aip_facts_sync_scope on aip_facts;
create trigger trg_aip_facts_sync_scope
    before insert or update on aip_facts
    for each row execute function aip_facts_sync_scope();

-- ---------------------------------------------------------------------------
-- 5. Indexes
-- ---------------------------------------------------------------------------
-- The new uniqueness key. PLAIN index on exactly these five columns: a
-- functional index cannot satisfy PostgREST's on_conflict=, which is why
-- entity is NOT NULL DEFAULT '' rather than nullable (see 11_aip_facts.sql —
-- the first version of that table got this wrong and every upsert failed).
create unique index if not exists uq_aip_facts_scope_key
    on aip_facts (scope_kind, scope_id, subsection, entity, label);

create index if not exists ix_aip_facts_scope
    on aip_facts (scope_kind, scope_id);
create index if not exists ix_aip_facts_scope_sub
    on aip_facts (scope_kind, scope_id, subsection);

-- uq_aip_facts_key (the old 4-column index) is deliberately NOT dropped here.
-- build_fact_index.py still targets it in on_conflict; dropping it in the same
-- migration would break ingestion the moment this runs. 13_scope_cutover.sql
-- removes it once the writer is deployed.

-- ---------------------------------------------------------------------------
-- 6. Retrieval
-- ---------------------------------------------------------------------------
-- NEW overload. The original match_aip_facts(query_embedding, p_icao, ...) is
-- left exactly as it is, so database.search_facts() keeps working untouched
-- until it is migrated.
--
-- The safety guarantee is unchanged and still enforced at the DB boundary:
-- results are confined to ONE scope, so a fact can never be returned for a
-- different entity. Only the definition of "entity" widens.
create or replace function match_aip_facts_scoped(
    query_embedding vector(1536),
    p_scope_kind    text,
    p_scope_id      text,
    p_subsection    text default null,
    match_limit     int  default 8
)
returns table (
    scope_kind text,
    scope_id   text,
    subsection text,
    entity     text,
    label      text,
    fact_value text,
    fact_text  text,
    similarity float
)
language sql stable
as $$
    select f.scope_kind,
           f.scope_id,
           f.subsection,
           f.entity,
           f.label,
           f.fact_value,
           f.fact_text,
           1 - (f.embedding <=> query_embedding) as similarity
      from aip_facts f
     where f.scope_kind = p_scope_kind
       and f.scope_id   = p_scope_id
       and (p_subsection is null or f.subsection = p_subsection)
       and f.embedding is not null
     order by f.embedding <=> query_embedding
     limit greatest(1, least(match_limit, 50));
$$;

-- Name lookup: "Where is TEMSA?" / "What is DND45?". Deterministic, exact, no
-- embedding involved — the same shape as resolver.AERODROMES, which is the one
-- part of this system that has never misrouted. A name either is a published
-- entity or it is not.
create or replace function find_aip_scope(p_name text)
returns table (scope_kind text, scope_id text, fact_count bigint)
language sql stable
as $$
    select f.scope_kind, f.scope_id, count(*) as fact_count
      from aip_facts f
     where upper(regexp_replace(f.scope_id, '\s+', '', 'g'))
         = upper(regexp_replace(coalesce(p_name, ''), '\s+', '', 'g'))
     group by f.scope_kind, f.scope_id;
$$;

commit;


-- ===========================================================================
-- VERIFY — run these after the migration. All three must hold.
-- ===========================================================================
-- 1) every row is scoped, and the AD rows still agree with icao_code
--    expect: 0
--
-- select count(*) from aip_facts
--  where scope_kind is null
--     or scope_id is null
--     or (scope_kind = 'AD' and icao_code is distinct from scope_id);
--
-- 2) the population is unchanged and entirely AD before any ENR is loaded
--    expect: AD | 36 | 5289   (or whatever your current fact count is)
--
-- select scope_kind, count(distinct scope_id) as scopes, count(*) as facts
--   from aip_facts group by scope_kind order by scope_kind;
--
-- 3) the legacy RPC still answers, so nothing in the running bot has changed
--    expect: rows, exactly as before this migration
--
-- select subsection, entity, label, left(fact_value, 40)
--   from match_aip_facts(
--        (select embedding from aip_facts where icao_code = 'DNMM' limit 1),
--        'DNMM', '2.17', 5);
--
-- 4) after ENR 5.1 is loaded, this should return the danger area:
--
-- select * from find_aip_scope('DND45');
