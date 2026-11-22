-- Restore the guarded Worker interaction RPC used by platform model tasks.
-- The public invoker also needs EXECUTE on this capability-checked private
-- function. Preserve the public wrapper ACL so an emergency pause stays paused.
begin;
create or replace function material_private.record_model_interaction(
  p_secret text,
  p_job_id text,
  p_lease text,
  p_request_id text,
  p_model text,
  p_input_text text,
  p_output_text text,
  p_status text,
  p_error_code text
) returns boolean language plpgsql security definer set search_path='' as $$
declare j public.material_jobs%rowtype;
begin
  if not exists(select 1 from material_private.worker_capability
    where secret_hash=sha256(convert_to(p_secret,'UTF8'))) then
    raise exception 'worker capability rejected' using errcode='42501';
  end if;
  select * into j from public.material_jobs
  where id=p_job_id and status='running' and trusted_submission and lease=p_lease and lease_until>=now()
  for update;
  if not found or not j.calls ? p_request_id then
    raise exception 'worker lease or request invalid' using errcode='42501';
  end if;
  if p_model not in ('deepseek-v4-flash','gpt-image-2')
     or (j.kind in ('section','outline','plan') and p_model<>'deepseek-v4-flash')
     or (j.kind='figure' and p_model<>'gpt-image-2')
     or p_request_id !~ '^(o2x1_|o90v1_|m12_)[a-f0-9]{32}$'
     or p_status not in ('started','completed','failed','unknown')
     or coalesce(length(p_input_text),0)>200000
     or coalesce(length(p_output_text),0)>200000
     or (p_error_code is not null and p_error_code !~ '^[A-Z_]{1,80}$') then
    raise exception 'invalid model interaction' using errcode='22023';
  end if;
  insert into public.workbench_model_interactions(
    request_id,user_id,project_id,model,input_text,output_text,status,error_code
  ) values (
    p_request_id,j.owner_id,j.project_id,p_model,coalesce(p_input_text,''),
    coalesce(p_output_text,''),p_status,p_error_code
  ) on conflict(request_id) do update set
    input_text=case when excluded.input_text<>'' then excluded.input_text
                    else workbench_model_interactions.input_text end,
    output_text=case when excluded.output_text<>'' then excluded.output_text
                     else workbench_model_interactions.output_text end,
    status=excluded.status,error_code=excluded.error_code,updated_at=now()
  where workbench_model_interactions.user_id=excluded.user_id
    and workbench_model_interactions.project_id=excluded.project_id
    and workbench_model_interactions.model=excluded.model;
  if not found then
    raise exception 'model interaction identity mismatch' using errcode='42501';
  end if;
  return true;
end $$;
revoke all on function material_private.record_model_interaction(
  text,text,text,text,text,text,text,text,text
) from public, anon, authenticated;
grant execute on function material_private.record_model_interaction(
  text,text,text,text,text,text,text,text,text
) to anon, authenticated;

-- All leased inference paths must enforce the trusted-submission boundary.
-- Refuse to create either function from scratch: CREATE OR REPLACE must retain
-- their existing ACLs, including any emergency operational pause.
do $$
begin
  if to_regprocedure('material_private.worker_context(text,text,text)') is null
     or to_regprocedure('material_private.authorize_byok_call(text,text,text,text,text)') is null then
    raise exception 'existing material worker inference functions are required';
  end if;
end $$;
create or replace function material_private.worker_context(
  p_secret text,
  p_job_id text,
  p_lease text
) returns jsonb
language plpgsql
security definer
set search_path=''
as $$
declare
  j public.material_jobs%rowtype;
begin
  if p_job_id !~ '^[a-f0-9]{32}$' or p_lease !~ '^[a-f0-9]{32}$' then
    raise exception 'invalid worker lease' using errcode='42501';
  end if;
  if not exists (
    select 1
    from material_private.worker_capability
    where secret_hash=sha256(convert_to(p_secret,'UTF8'))
  ) then
    raise exception 'worker capability rejected' using errcode='42501';
  end if;
  select * into j
  from public.material_jobs
  where id=p_job_id
  for update;
  if not found or not j.trusted_submission or j.status<>'running' or j.lease is distinct from p_lease
      or j.lease_until<now() then
    raise exception 'lease expired or job cancelled' using errcode='42501';
  end if;
  if not coalesce((j.payload->>'paid')::boolean,false) then
    raise exception 'model inference not confirmed' using errcode='42501';
  end if;
  return jsonb_build_object(
    'id', j.id,
    'project_id', j.project_id,
    'owner_id', j.owner_id,
    'kind', j.kind,
    'target', j.target,
    'payload', jsonb_build_object(
      'paid', j.payload->'paid',
      'platform', j.payload->'platform',
      'config', j.payload->'config',
      'config_ciphertext', j.payload->'config_ciphertext',
      'spec', j.payload->'spec',
      'image_path', j.payload->'image_path',
      'image_upload_url', j.payload->'image_upload_url',
      'image_download_url', j.payload->'image_download_url'
    )
  );
end
$$;


create or replace function material_private.authorize_byok_call(
  p_secret text,
  p_job_id text,
  p_lease text,
  p_call_id text,
  p_operation text
) returns jsonb
language plpgsql
security definer
set search_path=''
as $$
declare
  j public.material_jobs%rowtype;
  max_calls integer;
  call_marker text;
begin
  if p_job_id !~ '^[a-f0-9]{32}$'
      or p_lease !~ '^[a-f0-9]{32}$'
      or p_call_id !~ '^[a-f0-9]{32}$' then
    raise exception 'invalid worker request' using errcode='42501';
  end if;
  if not exists (
    select 1
    from material_private.worker_capability
    where secret_hash=sha256(convert_to(p_secret,'UTF8'))
  ) then
    raise exception 'worker capability rejected' using errcode='42501';
  end if;
  select * into j
  from public.material_jobs
  where id=p_job_id
  for update;
  if not found or not j.trusted_submission or j.status<>'running' or j.lease is distinct from p_lease
      or j.lease_until<now() then
    raise exception 'lease expired or job cancelled' using errcode='42501';
  end if;
  if not coalesce((j.payload->>'paid')::boolean,false)
      or coalesce((j.payload->>'platform')::boolean,false) then
    raise exception 'not a BYOK paid job' using errcode='42501';
  end if;
  if p_operation='text' and j.kind not in ('section','outline','plan') then
    raise exception 'text operation not allowed' using errcode='42501';
  elsif p_operation='image' and not (
      j.kind='figure' and j.payload->'spec'->>'renderer'='gpt'
  ) then
    raise exception 'image operation not allowed' using errcode='42501';
  elsif p_operation not in ('text','image') then
    raise exception 'invalid operation' using errcode='42501';
  end if;
  max_calls:=case when j.kind='section' then 6 else 1 end;
  call_marker:='byok:'||p_call_id;
  if j.calls @> jsonb_build_array(call_marker)
      or jsonb_array_length(j.calls)>=max_calls then
    raise exception 'BYOK call budget exceeded' using errcode='42501';
  end if;
  update public.material_jobs
  set calls=calls||jsonb_build_array(call_marker),updated_at=now()
  where id=j.id;
  return jsonb_build_object('ok',true);
end
$$;

commit;
