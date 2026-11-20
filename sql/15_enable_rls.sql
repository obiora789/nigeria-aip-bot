-- 15_enable_rls.sql — lock the tables against the public "anon" key (2026-11-19).
--
-- WHY: no table had row-level security, and the search function was granted
-- to anon. Supabase treats the anon key as public. Without RLS, anyone holding
-- it can read the query log (pilots' questions and Vannie's answers) and WRITE
-- aip_facts — a forged "TWR 118.9" row would be shown word for word.
--
-- BEFORE RUNNING — CHECK WHICH KEY VANNIE USES. RLS does not apply to the
-- service_role key, so the bot keeps working only if SUPABASE_KEY is that key.
-- On your Mac, in the repo folder:
--
--   python -c "import os,json,base64;from dotenv import load_dotenv;load_dotenv();p=os.environ['SUPABASE_KEY'].split('.')[1];print(json.loads(base64.urlsafe_b64decode(p+'='*(-len(p)%4)))['role'])"
--
-- It must print: service_role. If it prints anon, STOP — do not run this file;
-- switch Render's SUPABASE_KEY to the service_role key first.
--
-- Safe to run more than once. No policies are added on purpose: with RLS on
-- and no policy, anon and authenticated can do nothing; service_role is
-- unaffected.

alter table if exists aip_knowledge_base     enable row level security;
alter table if exists aip_facts              enable row level security;
alter table if exists aip_structured         enable row level security;
alter table if exists aip_charts             enable row level security;
alter table if exists aip_declared_distances enable row level security;
alter table if exists vannie_query_log       enable row level security;
alter table if exists conversation_context   enable row level security;

-- Functions: only the server may call them. Each is granted to service_role
-- explicitly first, so revoking the PUBLIC default cannot lock the bot out.
do $$
declare f record;
begin
  for f in select p.oid::regprocedure as sig
           from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = 'public'
             and p.proname in ('match_aip_text_advanced', 'match_aip_facts',
                               'match_aip_facts_scoped', 'find_aip_scope',
                               'get_aip_charts')
  loop
    execute format('grant execute on function %s to service_role', f.sig);
    execute format('revoke execute on function %s from anon, authenticated, public', f.sig);
  end loop;
end $$;

-- Check: every table should show rls = true.
select relname as table_name, relrowsecurity as rls
from pg_class
where relname in ('aip_knowledge_base','aip_facts','aip_structured','aip_charts',
                  'aip_declared_distances','vannie_query_log','conversation_context')
order by relname;
