-- Durable, project-scoped LangGraph reviews. No provider key or user JWT is stored.
-- Deployed disabled; the operator configures a dedicated capability and scheduler.
create schema if not exists review_private;
revoke all on schema review_private from public;
grant usage on schema review_private to anon, authenticated;

create table review_private.config (
  singleton boolean primary key default true check(singleton),
  enabled boolean not null default false,
  worker_hash bytea check(octet_length(worker_hash)=32)
);
insert into review_private.config(singleton) values(true);
create table review_private.runs (
  id text primary key check(id ~ '^[a-f0-9]{32}$'),
  project_id text not null references public.projects(id) on delete cascade,
  owner_id uuid not null references auth.users(id) on delete cascade,
  key_hash text not null, request_hash text not null,
  fingerprint text not null, snapshot jsonb not null check(octet_length(snapshot::text)<=1000000),
  mode text not null check(mode in ('rules','model')),
  model text not null, graph_version text not null,
  status text not null default 'pending' check(status in ('pending','running','completed','failed','needs_review')),
  step text not null default 'queued', report jsonb, error text not null default '',
  lease text, lease_until timestamptz, notified_until timestamptz,
  attempts integer not null default 0,
  model_state text not null default 'not_started' check(model_state in ('not_started','started','completed')),
  model_result jsonb, usage_request_id text unique,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  unique(project_id,key_hash)
);
create index review_runs_owner_project on review_private.runs(owner_id,project_id,created_at desc);
create index review_runs_pending on review_private.runs(created_at) where status='pending';
create index review_runs_expired on review_private.runs(lease_until) where status='running';
create table review_private.checkpoints (
  run_id text not null references review_private.runs(id) on delete cascade,
  id text not null, parent_id text, body jsonb not null check(octet_length(body::text)<=1800000),
  primary key(run_id,id)
);
create table review_private.writes (
  run_id text not null references review_private.runs(id) on delete cascade,
  checkpoint_id text not null, task_id text not null, idx integer not null,
  channel text not null, value jsonb not null, primary key(run_id,checkpoint_id,task_id,idx)
);
create table review_private.events (
  id bigint generated always as identity primary key,
  run_id text not null references review_private.runs(id) on delete cascade,
  node text not null, duration_ms integer not null check(duration_ms between 0 and 120000),
  created_at timestamptz not null default now()
);
create index review_events_run on review_private.events(run_id,id);
create table review_private.deliveries (
  id uuid primary key default gen_random_uuid(),
  run_id text not null references review_private.runs(id) on delete cascade,
  expires_at timestamptz not null default now()+interval '90 seconds',
  consumed boolean not null default false
);
create index review_deliveries_run on review_private.deliveries(run_id);

alter table review_private.config enable row level security;
alter table review_private.runs enable row level security;
alter table review_private.checkpoints enable row level security;
alter table review_private.writes enable row level security;
alter table review_private.events enable row level security;
alter table review_private.deliveries enable row level security;
revoke all on all tables in schema review_private from public,anon,authenticated;
revoke all on all sequences in schema review_private from public,anon,authenticated;

create function review_private.check_worker(p_secret text) returns void
language plpgsql security definer set search_path='' as $$
begin
  if p_secret is null or octet_length(p_secret)<>64 or not exists (
    select 1 from review_private.config where singleton
      and worker_hash=pg_catalog.sha256(pg_catalog.convert_to(p_secret,'UTF8'))
  ) then raise exception 'review capability rejected' using errcode='42501'; end if;
end $$;

create function review_private.snapshot(p_project text,p_owner uuid) returns jsonb
language sql security definer set search_path='' as $$
  select jsonb_build_object('project',jsonb_build_object(
    'id',p.id,'title',p.title,'school',p.school,'level',p.level,'discipline',p.discipline,
    'material_revision',p.material_revision,'outline',p.outline_json,'sections',p.sections_json,
    'state',(select coalesce(jsonb_object_agg(key,value),'{}'::jsonb) from jsonb_each(p.state_json)
      where key in ('skill_id','problem','target_user','method','foundation','budget',
                    'confirmed_facts','references','official_sources'))),
    'documents',(select coalesce(jsonb_agg(jsonb_build_object(
      'id',d.id,'project_id',d.project_id,'original_name',d.original_name,'kind',d.kind,
      'extracted_text',d.extracted_text,'extraction_status',d.extraction_status,
      'confirmed',d.confirmed and d.confirmation_origin='user',
      'confirmation_origin',d.confirmation_origin,'low_confidence_count',d.low_confidence_count)
      order by d.id),'[]'::jsonb) from public.documents d where d.project_id=p.id),
    'chunks',(select coalesce(jsonb_agg(jsonb_build_object(
      'id',c.id,'project_id',c.project_id,'document_id',c.document_id,
      'source_name',c.source_name,'source_ref',c.source_ref,'content',c.content) order by c.id),'[]'::jsonb)
      from public.chunks c join public.documents d on d.id=c.document_id and d.project_id=c.project_id
      where c.project_id=p.id and d.confirmed and d.confirmation_origin='user'
        and d.kind<>'template' and strpos(d.extracted_text,c.content)>0))
  from public.projects p where p.id=p_project and p.owner_id=p_owner
$$;

create function review_private.public_run(p_id text) returns jsonb
language sql security definer set search_path='' as $$
  select jsonb_build_object('id',r.id,'project_id',r.project_id,'fingerprint',r.fingerprint,
    'mode',r.mode,'model',r.model,'graph_version',r.graph_version,'status',r.status,
    'step',r.step,'report',r.report,'error',r.error,'created_at',extract(epoch from r.created_at),
    'updated_at',extract(epoch from r.updated_at),
    'billing',jsonb_build_object('request_id',r.usage_request_id,
      'status',coalesce(u.status,case when r.mode='rules' then 'not_applicable' else 'not_started' end),
      'reserved_micro_cny',u.reserved_micro_cny,'actual_micro_cny',u.actual_micro_cny))
  from review_private.runs r left join public.workbench_usage_events u on u.request_id=r.usage_request_id
  where r.id=p_id
$$;

-- A notification contains only a one-use signed delivery, never a durable key or private draft.
create function review_private.notify(p_run text) returns void
language plpgsql security definer set search_path='' as $$
declare d uuid; stamp text; secret text;
begin
  if not exists(select 1 from review_private.config where enabled) then return; end if;
  select decrypted_secret into secret from vault.decrypted_secrets where name='stardust_review_worker';
  if secret is null then return; end if;
  update review_private.runs set notified_until=clock_timestamp()+interval '90 seconds'
    where id=p_run and status='pending' and (notified_until is null or notified_until<clock_timestamp());
  if not found then return; end if;
  insert into review_private.deliveries(run_id) values(p_run) returning id into d;
  stamp := extract(epoch from clock_timestamp())::bigint::text;
  perform net.http_post(url:='https://agent.stardust.website/api/reviews/internal/tick',
    body:=jsonb_build_object('delivery_id',d::text),
    headers:=jsonb_build_object('Content-Type','application/json',
      'x-review-timestamp',stamp,'x-review-signature',
      encode(extensions.hmac('stardust-review:'||d::text||':'||stamp,secret,'sha256'),'hex')),
    timeout_milliseconds:=55000);
end $$;

create function review_private.dispatch() returns integer
language plpgsql security definer set search_path='' as $$
declare r record; n integer:=0;
begin
  if not exists(select 1 from review_private.config where enabled) then return 0; end if;
  -- An unknown model outcome never re-enters the queue.
  update review_private.runs set status=case when model_state='started' then 'needs_review' else 'pending' end,
    error=case when model_state='started' then '模型调用结果未确认，请核对账单；不会自动重发。' else '' end,
    lease=null,lease_until=null,notified_until=null,updated_at=clock_timestamp()
    where status='running' and lease_until<clock_timestamp();
  update review_private.runs set status='failed',error='检查恢复次数超过限制，请核对后重新检查。'
    where status='pending' and attempts>=20;
  delete from review_private.deliveries where expires_at<clock_timestamp()-interval '1 day';
  for r in select id from review_private.runs where status='pending' and attempts<20
      and (notified_until is null or notified_until<clock_timestamp()) order by created_at limit 5
  loop perform review_private.notify(r.id); n:=n+1; end loop;
  return n;
end $$;

create function review_private.user_api(p_action text,p_args jsonb,p_secret text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare uid uuid:=auth.uid(); pid text:=p_args->>'project_id'; snap jsonb; fp text;
  req text; old review_private.runs%rowtype; rid text; mode text; reason text:=''; items jsonb;
begin
  if uid is null or not exists(select 1 from public.projects where id=pid and owner_id=uid) then
    raise exception 'project access denied' using errcode='42501';
  end if;
  if p_action='get' then
    select id into rid from review_private.runs where id=p_args->>'id' and project_id=pid and owner_id=uid;
    if rid is null then return null; end if;
    return review_private.public_run(rid)||jsonb_build_object('events',(
      select coalesce(jsonb_agg(jsonb_build_object('node',node,'duration_ms',duration_ms) order by id),'[]')
      from review_private.events where run_id=rid));
  end if;
  snap:=review_private.snapshot(pid,uid);
  if coalesce(snap#>>'{project,state,skill_id}','innovation-research')<>'innovation-research' then
    reason:='首版草稿预审仅支持大创创新训练项目';
  elsif not exists(select 1 from jsonb_each_text(snap#>'{project,sections}') where length(btrim(value))>0) then
    reason:='请先保存至少一章草稿';
  elsif octet_length(snap::text)>1000000 then reason:='预审材料超过 1 MB，请减少材料后重试';
  else fp:=encode(pg_catalog.sha256(pg_catalog.convert_to(snap::text,'UTF8')),'hex'); end if;
  if p_action='list' then
    select coalesce(jsonb_agg(review_private.public_run(id) order by created_at desc),'[]') into items
      from (select id,created_at from review_private.runs where project_id=pid and owner_id=uid
        order by created_at desc limit 20) q;
    return jsonb_build_object('items',items,'current_fingerprint',fp,'unavailable_reason',reason);
  end if;
  if p_action<>'submit' then raise exception 'invalid action' using errcode='22023'; end if;
  perform public.workbench_check_backend(p_secret);
  if not exists(select 1 from review_private.config where enabled) then
    return jsonb_build_object('unavailable',true); end if;
  mode:=p_args->>'mode';
  if mode is null or mode not in ('rules','model') or length(coalesce(p_args->>'key','')) not between 1 and 160
      or coalesce(p_args->>'fingerprint','') !~ '^[a-f0-9]{64}$' then
    raise exception 'invalid review request' using errcode='22023'; end if;
  req:=encode(pg_catalog.sha256(pg_catalog.convert_to((p_args->>'fingerprint')||':'||mode||':proposal-review-cloud-v1','UTF8')),'hex');
  -- Serializes submission keys even before their first row exists.
  perform pg_advisory_xact_lock(hashtextextended(pid||':'||(p_args->>'key'),0));
  select * into old from review_private.runs where project_id=pid
    and key_hash=encode(pg_catalog.sha256(pg_catalog.convert_to(p_args->>'key','UTF8')),'hex');
  if found then
    if old.request_hash<>req then return jsonb_build_object('conflict',true); end if;
    return review_private.public_run(old.id);
  end if;
  if reason<>'' then return jsonb_build_object('invalid_input',reason); end if;
  if fp is distinct from p_args->>'fingerprint' then return jsonb_build_object('conflict',true); end if;
  if mode='model' and coalesce((select balance_micro_cny from public.workbench_credit_wallets where user_id=uid),0)<200000 then
    return jsonb_build_object('insufficient_credits',true); end if;
  rid:=replace(gen_random_uuid()::text,'-','');
  insert into review_private.runs(id,project_id,owner_id,key_hash,request_hash,fingerprint,snapshot,mode,model,graph_version)
    values(rid,pid,uid,encode(pg_catalog.sha256(pg_catalog.convert_to(p_args->>'key','UTF8')),'hex'),req,fp,snap,
      mode,case when mode='model' then 'deepseek-v4-flash' else '' end,'proposal-review-cloud-v1');
  perform review_private.notify(rid);
  return review_private.public_run(rid);
end $$;

create function review_private.worker_api(p_secret text,p_action text,p_args jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare r review_private.runs%rowtype; d review_private.deliveries%rowtype; result jsonb;
  v jsonb; write_index integer; cp text;
begin
  perform review_private.check_worker(p_secret);
  if p_action='health' then
    return jsonb_build_object('enabled',(select enabled from review_private.config where singleton));
  end if;
  if not exists(select 1 from review_private.config where enabled) then return null; end if;
  if p_action='claim' then
    update review_private.deliveries set consumed=true where id=(p_args->>'delivery_id')::uuid
      and not consumed and expires_at>clock_timestamp() returning * into d;
    if not found then return null; end if;
    select * into r from review_private.runs where id=d.run_id and status='pending' and attempts<20
      for update skip locked;
    if not found then return null; end if;
    update review_private.runs set status='running',lease=replace(gen_random_uuid()::text,'-',''),
      lease_until=clock_timestamp()+interval '120 seconds',attempts=attempts+1,updated_at=clock_timestamp()
      where id=r.id returning * into r;
    return to_jsonb(r);
  end if;
  select * into r from review_private.runs where id=p_args->>'id' and status='running'
    and lease=p_args->>'lease' and lease_until>=clock_timestamp() for update;
  if not found then raise exception 'review lease invalid' using errcode='42501'; end if;
  if p_action='context' then return to_jsonb(r); end if;
  if p_action='checkpoint_get' then
    select id into cp from review_private.checkpoints where run_id=r.id
      and (p_args->>'checkpoint_id' is null or id=p_args->>'checkpoint_id') order by id desc limit 1;
    if cp is null then return null; end if;
    select body into result from review_private.checkpoints where run_id=r.id and id=cp;
    return result||jsonb_build_object('writes',(select coalesce(jsonb_agg(jsonb_build_object(
      'task_id',task_id,'channel',channel,'value',value) order by task_id,review_private.writes.idx),'[]')
      from review_private.writes where run_id=r.id and checkpoint_id=cp));
  elsif p_action='checkpoint_put' then
    insert into review_private.checkpoints(run_id,id,parent_id,body)
      values(r.id,p_args#>>'{body,id}',p_args#>>'{body,parent_id}',p_args->'body')
      on conflict(run_id,id) do update set body=excluded.body;
    delete from review_private.checkpoints where run_id=r.id and id not in (
      select id from review_private.checkpoints where run_id=r.id order by id desc limit 3);
    delete from review_private.writes where run_id=r.id and checkpoint_id not in (
      select id from review_private.checkpoints where run_id=r.id);
  elsif p_action='checkpoint_writes' then
    if jsonb_array_length(p_args->'writes')>100 then raise exception 'too many writes'; end if;
    for v in select value from jsonb_array_elements(p_args->'writes') loop
      write_index:=(v->>'idx')::integer;
      insert into review_private.writes(run_id,checkpoint_id,task_id,idx,channel,value)
        values(r.id,p_args->>'checkpoint_id',p_args->>'task_id',write_index,v->>'channel',v->'value')
        on conflict(run_id,checkpoint_id,task_id,idx) do update set
          value=case when excluded.idx<0 then excluded.value else review_private.writes.value end;
    end loop;
  elsif p_action='progress' then
    if p_args->>'step' not in ('load_snapshot','retrieve','rules','semantic','verify','save_report') then
      raise exception 'invalid step'; end if;
    update review_private.runs set step=p_args->>'step',updated_at=clock_timestamp() where id=r.id;
  elsif p_action='event' then
    if (select count(*) from review_private.events where run_id=r.id)<100 then
      insert into review_private.events(run_id,node,duration_ms) values(r.id,p_args->>'node',(p_args->>'duration_ms')::integer);
    end if;
  elsif p_action='model_begin' then
    if r.mode<>'model' then raise exception 'model not authorized' using errcode='42501'; end if;
    if r.model_state='completed' then return jsonb_build_object('cached',r.model_result); end if;
    if r.model_state='started' then return jsonb_build_object('unknown',true); end if;
    update review_private.runs set model_state='started' where id=r.id;
    return jsonb_build_object('execute',true);
  elsif p_action='model_result' then
    if r.model_state not in ('started','completed') or octet_length((p_args->'result')::text)>200000 then raise exception 'invalid model result'; end if;
    update review_private.runs set model_state='completed',model_result=p_args->'result' where id=r.id;
    if r.usage_request_id is not null and p_args#>'{result,metrics}' is not null then
      insert into public.token_usages(id,project_id,task_type,model,prompt_tokens,completion_tokens,total_tokens,latency_ms,key_fingerprint)
        values(r.id,r.project_id,'proposal_review',r.model,
          greatest(0,coalesce((p_args#>>'{result,metrics,prompt_eval_count}')::int,0)),
          greatest(0,coalesce((p_args#>>'{result,metrics,eval_count}')::int,0)),
          greatest(0,coalesce((p_args#>>'{result,metrics,prompt_eval_count}')::int,0))+
          greatest(0,coalesce((p_args#>>'{result,metrics,eval_count}')::int,0)),
          greatest(0,coalesce((p_args#>>'{result,metrics,latency_ms}')::int,0)),'platform_review')
        on conflict(id) do nothing;
    end if;
  elsif p_action='reserve' then
    if r.mode<>'model' or r.model_state<>'started' or r.usage_request_id is not null
      or p_args->>'model' is distinct from r.model
      or coalesce(p_args->>'request_id','') !~ '^o2x1_[a-f0-9]{32}$' then
      raise exception 'review inference already reserved or not authorized' using errcode='42501'; end if;
    begin
      result:=public.workbench_reserve_usage(r.owner_id,p_args->>'request_id',r.model,200000,(p_args->>'api_key_id')::bigint);
    exception when check_violation then
      return jsonb_build_object('insufficient_credits',true);
    end;
    update review_private.runs set usage_request_id=p_args->>'request_id' where id=r.id;
    return result;
  elsif p_action in ('settle','refund','interaction') then
    if r.usage_request_id is null or r.usage_request_id is distinct from p_args->>'request_id' then
      raise exception 'review usage identity mismatch' using errcode='42501'; end if;
    if p_action='settle' then
      return public.workbench_settle_usage(r.usage_request_id,(p_args->>'amount')::bigint,p_args->>'provider_id');
    elsif p_action='refund' then
      result:=public.workbench_refund_usage(r.usage_request_id,'review_provider_rejected');
      update review_private.runs set model_state='completed',model_result='{"skip":"provider_rejected"}' where id=r.id;
      return result;
    end if;
    if p_args->>'status' not in ('started','completed','failed','unknown')
       or coalesce(length(p_args->>'input_text'),0)>200000 or coalesce(length(p_args->>'output_text'),0)>200000 then
      raise exception 'invalid interaction'; end if;
    insert into public.workbench_model_interactions(request_id,user_id,project_id,model,input_text,output_text,status)
      values(r.usage_request_id,r.owner_id,r.project_id,r.model,coalesce(p_args->>'input_text',''),coalesce(p_args->>'output_text',''),p_args->>'status')
      on conflict(request_id) do update set
        input_text=case when excluded.input_text<>'' then excluded.input_text else workbench_model_interactions.input_text end,
        output_text=case when excluded.output_text<>'' then excluded.output_text else workbench_model_interactions.output_text end,
        status=excluded.status,updated_at=clock_timestamp()
      where workbench_model_interactions.user_id=r.owner_id and workbench_model_interactions.project_id=r.project_id;
    if not found then raise exception 'interaction identity mismatch' using errcode='42501'; end if;
    if p_args->>'status'='completed' and length(coalesce(p_args->>'output_text',''))>0 then
      -- Cache the known provider answer before optional settlement lookups.
      -- A restart can finish the graph without invoking or charging again.
      update review_private.runs set model_state='completed',
        model_result=jsonb_build_object('content',p_args->>'output_text','metrics','{}'::jsonb) where id=r.id;
    end if;
  elsif p_action='save_report' then
    if p_args#>>'{report,fingerprint}' is distinct from r.fingerprint
       or p_args#>>'{report,run_id}' is distinct from r.id
       or octet_length((p_args->'report')::text)>200000 then raise exception 'report version mismatch'; end if;
    update review_private.runs set report=p_args->'report' where id=r.id;
  elsif p_action='yield' then
    update review_private.runs set status='pending',lease=null,lease_until=null,notified_until=null,updated_at=clock_timestamp() where id=r.id;
    perform review_private.notify(r.id);
  elsif p_action='finish' then
    if r.report is null then raise exception 'report missing'; end if;
    update review_private.runs set status='completed',step='completed',lease=null,lease_until=null,updated_at=clock_timestamp() where id=r.id;
  elsif p_action='recover' then
    update review_private.runs set status=case when model_state='started' then 'needs_review' else 'pending' end,
      error=case when model_state='started' then '模型调用结果未确认，请核对账单；不会自动重发。' else '' end,
      lease=null,lease_until=null,notified_until=clock_timestamp()+interval '30 seconds',updated_at=clock_timestamp()
      where id=r.id;
  elsif p_action='fail' then
    update review_private.runs set status=case when model_state='started' then 'needs_review' else 'failed' end,
      error=case when model_state='started' then '模型调用结果未确认，请核对账单；不会自动重发。' else '检查未完成，请核对后重新检查。' end,
      lease=null,lease_until=null,updated_at=clock_timestamp() where id=r.id;
  else raise exception 'invalid worker action' using errcode='22023'; end if;
  return jsonb_build_object('ok',true);
end $$;

create function review_private.configure(p_backend text,p_worker text,p_enabled boolean) returns jsonb
language plpgsql security definer set search_path='' as $$
declare sid uuid; jid bigint;
begin
  perform public.workbench_check_backend(p_backend);
  if p_worker is null or octet_length(p_worker)<>64 then raise exception 'invalid capability'; end if;
  update review_private.config set worker_hash=pg_catalog.sha256(pg_catalog.convert_to(p_worker,'UTF8')),enabled=p_enabled;
  if not p_enabled then
    update review_private.runs set status=case when model_state='started' then 'needs_review' else 'pending' end,
      lease=null,lease_until=null,notified_until=null,updated_at=clock_timestamp()
      where status='running';
  end if;
  select id into sid from vault.secrets where name='stardust_review_worker';
  if sid is null then perform vault.create_secret(p_worker,'stardust_review_worker','Signs one-use review deliveries');
  else perform vault.update_secret(sid,p_worker,'stardust_review_worker','Signs one-use review deliveries'); end if;
  select cron.schedule('stardust-proposal-review','* * * * *','select review_private.dispatch()') into jid;
  return jsonb_build_object('enabled',p_enabled,'job_id',jid);
end $$;

revoke all on all functions in schema review_private from public,anon,authenticated;
grant execute on function review_private.user_api(text,jsonb,text) to authenticated;
grant execute on function review_private.worker_api(text,text,jsonb),review_private.configure(text,text,boolean) to anon,authenticated;

create function public.proposal_review_api(p_action text,p_args jsonb,p_secret text default null) returns jsonb
language sql security invoker set search_path='' as $$ select review_private.user_api(p_action,p_args,p_secret) $$;
revoke all on function public.proposal_review_api(text,jsonb,text) from public,anon;
grant execute on function public.proposal_review_api(text,jsonb,text) to authenticated;
create function public.proposal_review_worker(p_secret text,p_action text,p_args jsonb) returns jsonb
language sql security invoker set search_path='' as $$ select review_private.worker_api(p_secret,p_action,p_args) $$;
revoke all on function public.proposal_review_worker(text,text,jsonb) from public;
grant execute on function public.proposal_review_worker(text,text,jsonb) to anon,authenticated;
create function public.proposal_review_configure(p_backend text,p_worker text,p_enabled boolean) returns jsonb
language sql security invoker set search_path='' as $$ select review_private.configure(p_backend,p_worker,p_enabled) $$;
revoke all on function public.proposal_review_configure(text,text,boolean) from public;
grant execute on function public.proposal_review_configure(text,text,boolean) to anon,authenticated;
