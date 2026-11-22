-- A worker may claim or modify only tasks with a verified server-side submission.
-- CREATE OR REPLACE preserves the existing EXECUTE ACL, including an operational pause.
begin;
create or replace function material_private.worker_rpc(p_secret text,p_action text,p_args jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare j public.material_jobs%rowtype; request_id text; model text; r jsonb;
begin
 if not exists(select 1 from material_private.worker_capability
   where secret_hash=sha256(convert_to(p_secret,'UTF8'))) then
  raise exception 'worker capability rejected' using errcode='42501'; end if;
 if p_action='claim' then
  update public.material_jobs set status=case when coalesce((payload->>'paid')::boolean,false) then 'needs_review' else 'pending' end,
    error=case when coalesce((payload->>'paid')::boolean,false) then '租约中断，禁止盲目重复调用，请核对结果与费用' else '' end,
    payload=case when coalesce((payload->>'paid')::boolean,false) then payload-'config_ciphertext' else payload end,
    updated_at=now() where status='running' and trusted_submission and lease_until<now();
  select * into j from public.material_jobs where status='pending' and trusted_submission order by created_at for update skip locked limit 1;
  if not found then return null; end if;
  update public.material_jobs set status='running',lease=replace(gen_random_uuid()::text,'-',''),
    lease_until=now()+interval '120 seconds',updated_at=now() where id=j.id returning * into j;
  return to_jsonb(j);
 end if;
 select * into j from public.material_jobs where id=p_args->>'id' for update;
 if not found or not j.trusted_submission or j.status<>'running' or j.lease is distinct from p_args->>'lease' or j.lease_until<now() then
  raise exception 'lease expired or job cancelled' using errcode='42501'; end if;
 if p_action='heartbeat' or p_action='checkpoint' then
  update public.material_jobs set lease_until=now()+interval '120 seconds',
    step=case when p_action='checkpoint' then left(p_args->>'step',100) else step end,
    result=result||coalesce(p_args->'partial','{}'::jsonb),updated_at=now() where id=j.id;
  return jsonb_build_object('ok',true);
 elsif p_action='finish' then
  if p_args->>'status' not in ('completed','failed','needs_review') then raise exception 'invalid status'; end if;
  update public.material_jobs set status=p_args->>'status',result=result||coalesce(p_args->'result','{}'),
   error=left(coalesce(p_args->>'error',''),400),payload=payload-'config_ciphertext',updated_at=now() where id=j.id;
  return jsonb_build_object('ok',true);
 elsif p_action in ('reserve','settle','refund') then
  -- Restore identity only within this guarded transaction, never mint a JWT.
  perform set_config('request.jwt.claim.sub',j.owner_id::text,true);
  perform set_config('request.jwt.claims',jsonb_build_object('sub',j.owner_id::text,'role','authenticated')::text,true);
  request_id:=p_args->>'request_id';
  if p_action='reserve' then
   model:=p_args->>'model';
   if j.kind not in ('section','figure','outline','plan') or not coalesce((j.payload->>'platform')::boolean,false)
     or model not in ('deepseek-v4-flash','gpt-image-2')
     or (j.kind in ('section','outline','plan') and model<>'deepseek-v4-flash')
     or (j.kind='figure' and model<>'gpt-image-2')
     or jsonb_array_length(j.calls)>=(case when j.kind='section' then 6 else 1 end) then raise exception 'call budget exceeded'; end if;
   r:=public.workbench_reserve_usage(j.owner_id,request_id,model,
     case when model='gpt-image-2' then 2000000 else 200000 end,(p_args->>'api_key_id')::bigint);
   update public.material_jobs set calls=calls||jsonb_build_array(request_id) where id=j.id;
   return r;
  end if;
  if not j.calls ? request_id then raise exception 'request not owned by job' using errcode='42501'; end if;
  if p_action='settle' then return public.workbench_settle_usage(request_id,(p_args->>'amount')::bigint,p_args->>'provider_id'); end if;
  return public.workbench_refund_usage(request_id,left(p_args->>'reason',120));
 end if;
 raise exception 'invalid worker action';
end $$;
commit;
