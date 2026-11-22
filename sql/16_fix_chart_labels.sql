-- 16_fix_chart_labels.sql — correct 19 mislabelled rows in aip_charts.
--
-- Found 2026-11-21 with diagnose_chart_titles.py on the AMDT 03/2026 PDF:
--   * 17 SID and STAR pages were stored as 'RNAV Approach Chart', because the
--     chart-type rules tried "RNAV ... RWY nn" before SID/STAR (fixed for the
--     next chart load in chart_titles.py). Asking for an RNAV approach plate
--     at these aerodromes could send a SID or STAR.
--   * 2 Abuja pages (p350, p351) are AD 2.22 coding-table TEXT, not charts,
--     stored as RNAV approach plates for RWY 04 and RWY 22.
-- The images are correct; only the labels change. Nothing is uploaded.
--
-- STEP 1 — look before changing (run this SELECT alone first). Open the
-- chart_url of DNPO page 951: its title reads "TRANSITION ALTITUDE 3500FT
-- TRANSITION LEVEL FL50", so confirm it is the SID chart before step 2.
select icao_code, source_page, procedure_type, runway, chart_url
  from aip_charts
 where (icao_code, source_page) in (
   ('DNAA',350),('DNAA',351),('DNAA',361),('DNAA',367),('DNAA',368),
   ('DNBE',482),('DNBE',483),('DNBE',484),('DNBY',516),('DNBY',517),('DNBY',518),
   ('DNKN',768),('DNKN',769),('DNKN',771),('DNKN',772),
   ('DNKS',796),('DNKS',797),('DNPO',951),('DNPO',953))
 order by icao_code, source_page;
-- Expect 19 rows, all 'RNAV Approach Chart'.

-- STEP 2 — the correction, all or nothing.
begin;

update aip_charts set procedure_type = 'SID Chart'
 where (icao_code, source_page) in (
   ('DNAA',367),('DNAA',368),('DNBE',482),('DNBE',483),('DNBY',516),('DNBY',517),
   ('DNKN',768),('DNKN',769),('DNKS',796),('DNPO',951))
   and procedure_type = 'RNAV Approach Chart';                -- expect UPDATE 10

update aip_charts set procedure_type = 'STAR Chart'
 where (icao_code, source_page) in (
   ('DNAA',361),('DNBE',484),('DNBY',518),('DNKN',771),('DNKN',772),
   ('DNKS',797),('DNPO',953))
   and procedure_type = 'RNAV Approach Chart';                -- expect UPDATE 7

delete from aip_charts
 where icao_code = 'DNAA' and source_page in (350, 351)
   and procedure_type = 'RNAV Approach Chart';                -- expect DELETE 2

commit;

-- STEP 3 — check: each affected aerodrome now has only real approaches
-- under 'RNAV Approach Chart' (Abuja 2, Benin 2, Amassoma 2, Kano 4,
-- Kashimbila 2, Port Harcourt 2).
select icao_code, procedure_type, count(*) as pages
  from aip_charts
 where icao_code in ('DNAA','DNBE','DNBY','DNKN','DNKS','DNPO')
 group by 1, 2 order by 1, 2;
