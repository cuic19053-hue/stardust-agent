-- Operator-only final step after the compatible Worker and API are ready.
-- The capability secrets themselves are provisioned separately and never
-- appear in a migration or in a Worker/API error response.
begin;
lock table public.material_jobs in access exclusive mode;
do $$
declare definition text;
begin
  if not exists (select 1 from material_private.submit_capability) or
     not exists (select 1 from material_private.worker_capability) then
    raise exception 'material submission and worker capabilities must be provisioned';
  end if;
  if exists (select 1 from material_private.submit_capability s
             join material_private.worker_capability w on s.secret_hash=w.secret_hash) then
    raise exception 'submission and worker capabilities must be independent';
  end if;
  if has_table_privilege('authenticated','public.material_jobs','INSERT') or
     has_table_privilege('authenticated','public.material_jobs','UPDATE') or
     has_table_privilege('authenticated','public.material_jobs','DELETE') or
     has_column_privilege('authenticated','public.material_revisions','job_id','INSERT') then
    raise exception 'material task write permissions remain unsafe';
  end if;
  select pg_get_functiondef('material_private.worker_rpc(text,text,jsonb)'::regprocedure)
    into definition;
  if position('and trusted_submission' in lower(definition))=0 or
     position('not j.trusted_submission' in lower(definition))=0 then
    raise exception 'trusted Worker guard is missing';
  end if;
  if exists (select 1 from public.material_jobs where status in ('pending','running')
             or lease_until>now()) then
    raise exception 'drain and reconcile existing material tasks before resuming';
  end if;
end $$;
grant execute on function material_private.worker_rpc(text,text,jsonb) to anon, authenticated;
grant execute on function public.material_worker_rpc(text,text,jsonb) to anon, authenticated;
commit;
