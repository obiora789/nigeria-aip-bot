-- sql/13_scope_cutover.sql
--
-- Retire the legacy aerodrome-only unique index.
--
-- WHY THIS IS A SEPARATE FILE FROM 12
-- -----------------------------------
-- 12_scope_generalisation.sql deliberately kept uq_aip_facts_key so that a
-- build_fact_index.py still targeting the old on_conflict string would keep
-- working the moment the schema changed. That ordering is right for the AD
-- path: schema first, code second, index drop third.
--
-- It is WRONG for ENR, and this was found the hard way on the first real load:
--
--     wrote 3/313 facts
--     duplicate key value violates unique constraint "uq_aip_facts_key"
--
-- uq_aip_facts_key is (icao_code, subsection, entity, label). Every ENR row
-- carries icao_code = its scope_kind marker ('ENR_AREA') and entity = '',
-- because a danger area has no aerodrome and no sub-entity. So DNP1's "Name"
-- and DNP2's "Name" are identical under that key and Postgres rejected all but
-- the first — correctly. The constraint was doing its job; it just encodes an
-- assumption ("a fact belongs to an aerodrome") that is no longer true.
--
-- Postgres refusing the batch is the GOOD outcome here. Had the index not
-- existed, 57 areas would have silently overwritten one another down to a
-- handful of rows, and the run would have reported success.
--
-- PREREQUISITES — do not run this until both are true:
--   1. 12_scope_generalisation.sql has run and its verify query 1 returns 0
--   2. the deployed build_fact_index.py upserts with
--      on_conflict="scope_kind,scope_id,subsection,entity,label"
--      (check: grep on_conflict build_fact_index.py)
-- Dropping this index while an older writer is live would let duplicate AD
-- facts through unchecked.

begin;

-- Fail loudly rather than silently leaving the table unprotected if the new
-- key is somehow absent. A window with NEITHER unique index is the one state
-- in which silent overwrite becomes possible.
do $$
begin
    if not exists (
        select 1 from pg_indexes
         where tablename = 'aip_facts'
           and indexname = 'uq_aip_facts_scope_key'
    ) then
        raise exception
            'uq_aip_facts_scope_key is missing — run 12_scope_generalisation.sql '
            'first. Refusing to drop the only unique index on aip_facts.';
    end if;
end $$;

drop index if exists uq_aip_facts_key;

-- The old lookup indexes stay: icao_code is still populated for AD rows and
-- still queried by the un-migrated search_facts() path.
--   ix_aip_facts_icao, ix_aip_facts_sub — deliberately retained.

commit;


-- ===========================================================================
-- VERIFY
-- ===========================================================================
-- 1) exactly one unique index remains, and it is the scoped one
--    expect: uq_aip_facts_scope_key
--
-- select indexname from pg_indexes
--  where tablename = 'aip_facts' and indexdef like '%UNIQUE%';
--
-- 2) reload ENR 5.1 — expect "wrote 313/313 facts"
--
--    python build_fact_index.py --enr 5.1 --pdf Complete_AIP2026.pdf
--
-- 3) the areas are there and complete
--    expect: 57 areas, 313 facts
--
-- select count(distinct scope_id) as areas, count(*) as facts
--   from aip_facts where scope_kind = 'ENR_AREA';
--
-- 4) one area, in full
--
-- select label, fact_value from aip_facts
--  where scope_kind = 'ENR_AREA' and scope_id = 'DND45' order by label;
--
-- 5) AD facts are untouched by the drop
--    expect: 36 aerodromes, your existing fact count
--
-- select count(distinct scope_id) as aerodromes, count(*) as facts
--   from aip_facts where scope_kind = 'AD';
