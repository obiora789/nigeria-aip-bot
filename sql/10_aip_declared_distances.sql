-- Structured per-runway declared distances (AD 2.13), populated by
-- extract_declared_distances.py. Attribution is resolved once at ingestion
-- (with count-aligned validation), so query-time lookups are exact and can
-- never misattribute one runway end's value to another. Aerodromes whose table
-- doesn't parse cleanly are simply absent here -> the bot falls back to the
-- refuse-to-source guard for those.
create table if not exists aip_declared_distances (
    icao   text not null,
    runway text not null,          -- designator, e.g. '18L', '04', '02'
    tora   text,                   -- text preserves decimals ('893.1') exactly
    toda   text,
    asda   text,
    lda    text,
    primary key (icao, runway)
);

create index if not exists idx_declared_icao on aip_declared_distances (icao);
