-- After material authority hardening, the separately provisioned backup role
-- still needs its invoker SELECT. Its RLS policy is installed by backup ops.
-- Normal owners use the scoped jobs RPC and must not read internal job fields.
begin;
drop policy if exists material_jobs_owner on public.material_jobs;
do $$
begin
  if not exists (select 1 from information_schema.columns
                 where table_schema='public' and table_name='material_jobs'
                   and column_name='trusted_submission') then
    raise exception 'material authority hardening must be installed first';
  end if;
  if exists (select 1 from pg_policies where schemaname='public'
             and tablename='material_jobs' and policyname='backup_read_jobs'
             and cmd='SELECT' and roles = array['authenticated']::name[]) then
    grant select on public.material_jobs to authenticated;
  end if;
end $$;
commit;
