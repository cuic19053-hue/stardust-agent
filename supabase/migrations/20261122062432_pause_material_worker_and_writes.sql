-- Emergency containment only. Keep historical jobs and the backup SELECT policy.
-- The independent worker must be stopped at its host before re-enabling it.
-- An in-flight model request must be reconciled, never blindly replayed.
begin;
lock table public.material_jobs in access exclusive mode;
do $$
begin
  if exists (select 1 from public.material_jobs
             where status in ('pending', 'running') or lease_until > now()) then
    raise exception 'material jobs still active: drain and reconcile before pausing';
  end if;
end $$;

-- The old invoker RPC depends on these table privileges too; retaining SELECT
-- avoids breaking the separately managed, RLS-restricted backup account.
revoke insert, update, delete on public.material_jobs from public, anon, authenticated;

-- Block the running old worker at both the public wrapper and its private
-- function. Revocation does not stop the host process or in-flight requests.
revoke execute on function public.material_worker_rpc(text,text,jsonb)
  from public, anon, authenticated;
revoke execute on function material_private.worker_rpc(text,text,jsonb)
  from public, anon, authenticated;
commit;
