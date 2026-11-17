-- V2 evidence review + immutable follow-up lineage. Old checkpoints keep their v1 graph.
alter table review_private.runs
  add column parent_id text references review_private.runs(id) on delete set null,
  add column origin text not null default 'manual' check(origin in
    ('manual','chat_draft','section_generation','material_confirmation'));
create index review_runs_parent on review_private.runs(parent_id) where parent_id is not null;

create or replace function review_private.public_run(p_id text) returns jsonb
language sql security definer set search_path='' as $$
  select jsonb_build_object('id',r.id,'project_id',r.project_id,'fingerprint',r.fingerprint,
    'parent_id',r.parent_id,'origin',r.origin,'mode',r.mode,'model',r.model,'graph_version',r.graph_version,'status',r.status,
    'step',r.step,'report',r.report,'error',r.error,'created_at',extract(epoch from r.created_at),
    'updated_at',extract(epoch from r.updated_at),
    'billing',jsonb_build_object('request_id',r.usage_request_id,
      'status',coalesce(u.status,case when r.mode='rules' then 'not_applicable' else 'not_started' end),
      'reserved_micro_cny',u.reserved_micro_cny,'actual_micro_cny',u.actual_micro_cny))
  from review_private.runs r left join public.workbench_usage_events u on u.request_id=r.usage_request_id
  where r.id=p_id
$$;

create or replace function review_private.user_api(p_action text,p_args jsonb,p_secret text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare uid uuid:=auth.uid(); pid text:=p_args->>'project_id'; snap jsonb; fp text;
  req text; old review_private.runs%rowtype; rid text; mode text; reason text:=''; items jsonb; parent review_private.runs%rowtype; v_parent_id text:=nullif(p_args->>'parent_id',''); origin text:=coalesce(p_args->>'origin','manual'); version text:=coalesce(p_args->>'graph_version','proposal-review-cloud-v1');
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
  if version not in ('proposal-review-cloud-v1','proposal-review-cloud-v2') or origin not in
      ('manual','chat_draft','section_generation','material_confirmation') then
    raise exception 'invalid review version/origin' using errcode='22023'; end if;
  req:=encode(pg_catalog.sha256(pg_catalog.convert_to((p_args->>'fingerprint')||':'||mode||':'||version||
    case when version='proposal-review-cloud-v1' then '' else ':'||coalesce(v_parent_id,'')||':'||origin end,'UTF8')),'hex');
  -- Serializes submission keys even before their first row exists.
  perform pg_advisory_xact_lock(hashtextextended(pid||':'||(p_args->>'key'),0));
  select * into old from review_private.runs where project_id=pid
    and key_hash=encode(pg_catalog.sha256(pg_catalog.convert_to(p_args->>'key','UTF8')),'hex');
  if found then
    if old.request_hash<>req then return jsonb_build_object('conflict',true); end if;
    return review_private.public_run(old.id);
  end if;
  if v_parent_id is not null then
    select * into parent from review_private.runs where id=v_parent_id and project_id=pid and owner_id=uid;
    if not found or parent.status<>'completed' or parent.fingerprint=p_args->>'fingerprint' then
      return jsonb_build_object('conflict',true); end if;
  end if;
  if reason<>'' then return jsonb_build_object('invalid_input',reason); end if;
  if fp is distinct from p_args->>'fingerprint' then return jsonb_build_object('conflict',true); end if;
  if mode='model' and coalesce((select balance_micro_cny from public.workbench_credit_wallets where user_id=uid),0)<200000 then
    return jsonb_build_object('insufficient_credits',true); end if;
  rid:=replace(gen_random_uuid()::text,'-','');
  insert into review_private.runs(id,project_id,owner_id,key_hash,request_hash,fingerprint,snapshot,mode,model,graph_version,parent_id,origin)
    values(rid,pid,uid,encode(pg_catalog.sha256(pg_catalog.convert_to(p_args->>'key','UTF8')),'hex'),req,fp,snap,
      mode,case when mode='model' then 'deepseek-v4-flash' else '' end,version,v_parent_id,origin);
  perform review_private.notify(rid);
  return review_private.public_run(rid);
end $$;

-- Interactive turns keep only scoped input snapshots, operation hashes and results.
-- A provider key, JWT or request header is never accepted as an execution field.
create schema agent_private;
revoke all on schema agent_private from public;
grant usage on schema agent_private to authenticated;
create table agent_private.turns (
  id text primary key check(id ~ '^[a-f0-9]{32}$'),
  project_id text not null references public.projects(id) on delete cascade,
  owner_id uuid not null references auth.users(id) on delete cascade,
  key_hash text not null check(key_hash ~ '^[a-f0-9]{64}$'),
  request_hash text not null check(request_hash ~ '^[a-f0-9]{64}$'),
  input_digest text not null check(input_digest ~ '^[a-f0-9]{64}$'),
  snapshot jsonb not null check(octet_length(snapshot::text)<=2000000),
  message text not null check(length(message) between 1 and 12000),
  status text not null check(status in ('running','suspended','completed','needs_review')),
  lease_hash bytea, lease_until timestamptz,
  result jsonb check(octet_length(result::text)<=1000000),
  created_at timestamptz not null default now(),
  unique(project_id,key_hash)
);
create index agent_turns_owner_project on agent_private.turns(owner_id,project_id,created_at desc);
create index agent_turns_active on agent_private.turns(project_id,lease_until) where status='running';
create table agent_private.operations (
  turn_id text not null references agent_private.turns(id) on delete cascade,
  idx integer not null check(idx between 0 and 23),
  kind text not null check(kind in ('model','tool','trial')),
  request_hash text not null check(request_hash ~ '^[a-f0-9]{64}$'),
  status text not null check(status in ('started','completed')),
  result jsonb check(octet_length(result::text)<=1000000),
  primary key(turn_id,idx)
);
alter table agent_private.turns enable row level security;
alter table agent_private.operations enable row level security;
revoke all on all tables in schema agent_private from public,anon,authenticated;

create function agent_private.api(p_secret text,p_action text,p_args jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare uid uuid:=auth.uid(); pid text:=p_args->>'project_id'; t agent_private.turns%rowtype;
  op agent_private.operations%rowtype; rid text; snap jsonb; expected_history jsonb; v_result jsonb;
begin
  perform public.workbench_check_backend(p_secret);
  if uid is null or not exists(select 1 from public.projects where id=pid and owner_id=uid) then
    raise exception 'project access denied' using errcode='42501'; end if;
  if p_action='list' then
    return (select coalesce(jsonb_agg(jsonb_build_object('id',id,'status',status,'created_at',extract(epoch from created_at))
      order by created_at desc),'[]'::jsonb) from
      (select id,status,created_at from agent_private.turns where project_id=pid and owner_id=uid order by created_at desc limit 20) q);
  end if;
  if p_action='lookup' then
    select * into t from agent_private.turns where project_id=pid and owner_id=uid and key_hash=p_args->>'key_hash';
    if not found then return null; end if;
    if t.request_hash is distinct from p_args->>'request_hash' then
      return jsonb_build_object('conflict','同一请求标识已用于不同内容或模型配置。'); end if;
    return jsonb_build_object('id',t.id,'status',t.status,'result',case when t.status='completed' then t.result else null end);
  end if;
  if coalesce(p_args->>'lease','') !~ '^[a-f0-9]{64}$' then
    raise exception 'invalid turn lease' using errcode='22023'; end if;
  if p_action='open' then
    if coalesce(p_args->>'key_hash','') !~ '^[a-f0-9]{64}$'
        or coalesce(p_args->>'request_hash','') !~ '^[a-f0-9]{64}$'
        or coalesce(p_args->>'input_digest','') !~ '^[a-f0-9]{64}$'
        or length(coalesce(p_args->>'message','')) not between 1 and 12000
        or jsonb_typeof(p_args->'snapshot') is distinct from 'object'
        or octet_length((p_args->'snapshot')::text)>2000000
        or p_args#>>'{snapshot,project,id}' is distinct from pid then
      raise exception 'invalid turn input' using errcode='22023'; end if;
    perform pg_advisory_xact_lock(hashtextextended('agent-turn:'||pid,0));
    select * into t from agent_private.turns where project_id=pid and owner_id=uid and key_hash=p_args->>'key_hash' for update;
    if found then
      if t.request_hash<>p_args->>'request_hash' then return jsonb_build_object('conflict','同一请求标识已用于不同内容或模型配置。'); end if;
      if t.status='completed' then return jsonb_build_object('id',t.id,'status',t.status,'result',t.result); end if;
      if t.status='needs_review' then return jsonb_build_object('conflict','上次调用结果尚未确认，不会自动重发。请查看已收到的内容和用量记录。'); end if;
      if t.lease_until>clock_timestamp() then return jsonb_build_object('conflict','该轮对话仍在执行，请稍后用原请求恢复。'); end if;
      if exists(select 1 from agent_private.operations where turn_id=t.id and status='started') then
        update agent_private.turns set status='needs_review',lease_hash=null,lease_until=null where id=t.id;
        return jsonb_build_object('conflict','上次调用结果尚未确认，不会自动重发。请查看用量记录。'); end if;
      select jsonb_agg(value order by ordinality) into expected_history from jsonb_array_elements(
        (t.snapshot->'history')||jsonb_build_array(jsonb_build_object('role','user','content',t.message))) with ordinality
        where ordinality>greatest(jsonb_array_length(t.snapshot->'history')+1-200,0);
      if t.input_digest<>p_args->>'input_digest' or expected_history is distinct from p_args#>'{snapshot,history}' then
        return jsonb_build_object('conflict','项目、材料或对话已变化，请保留原结果并发起新一轮。'); end if;
      rid:=t.id; snap:=t.snapshot;
    else
      rid:=replace(gen_random_uuid()::text,'-',''); snap:=p_args->'snapshot';
    end if;
    if exists(select 1 from agent_private.turns where project_id=pid and id<>rid and status='running' and lease_until>clock_timestamp()) then
      return jsonb_build_object('conflict','项目还有一轮对话在执行，请稍后继续。'); end if;
    if t.id is null then
      insert into agent_private.turns(id,project_id,owner_id,key_hash,request_hash,input_digest,snapshot,message,status)
        values(rid,pid,uid,p_args->>'key_hash',p_args->>'request_hash',p_args->>'input_digest',snap,p_args->>'message','running');
      insert into public.messages(id,project_id,role,content) values(replace(gen_random_uuid()::text,'-',''),pid,'user',p_args->>'message');
      update public.projects set updated_at=clock_timestamp() where id=pid and owner_id=uid;
    end if;
    update agent_private.turns set status='running',lease_hash=sha256(convert_to(p_args->>'lease','UTF8')),
      lease_until=clock_timestamp()+interval '120 seconds' where id=rid;
    return jsonb_build_object('id',rid,'status','running','snapshot',snap);
  end if;
  select * into t from agent_private.turns where id=p_args->>'id' and project_id=pid and owner_id=uid
    and status='running' and lease_until>clock_timestamp()
    and lease_hash=sha256(convert_to(p_args->>'lease','UTF8')) for update;
  if not found then return jsonb_build_object('conflict','本轮执行租约已失效，请查看原任务。'); end if;
  update agent_private.turns set lease_until=clock_timestamp()+interval '120 seconds' where id=t.id;
  if p_action='begin' then
    select * into op from agent_private.operations where turn_id=t.id and idx=(p_args->>'idx')::integer;
    if found then
      if op.request_hash<>p_args->>'request_hash' or op.kind<>p_args->>'kind' or op.status<>'completed' then
        return jsonb_build_object('conflict','恢复步骤与原请求不一致或结果未确认，已停止执行。'); end if;
      return jsonb_build_object('cached',true,'result',op.result);
    end if;
    insert into agent_private.operations(turn_id,idx,kind,request_hash,status)
      values(t.id,(p_args->>'idx')::integer,p_args->>'kind',p_args->>'request_hash','started');
    return jsonb_build_object('cached',false);
  elsif p_action='record' then
    update agent_private.operations set status='completed',result=p_args->'result'
      where turn_id=t.id and idx=(p_args->>'idx')::integer and status='started';
    if not found then return jsonb_build_object('conflict','步骤结果已被其他执行记录。'); end if;
  elsif p_action='complete' then
    if exists(select 1 from agent_private.operations where turn_id=t.id and status='started') then
      return jsonb_build_object('conflict','仍有调用结果未确认，本轮尚不能标为完成。'); end if;
    v_result:=p_args->'result';
    if jsonb_typeof(v_result) is distinct from 'object' or length(coalesce(v_result->>'message','')) not between 1 and 200000 then
      raise exception 'invalid turn result' using errcode='22023'; end if;
    insert into public.messages(id,project_id,role,content) values(replace(gen_random_uuid()::text,'-',''),pid,'assistant',v_result->>'message');
    update public.projects set updated_at=clock_timestamp() where id=pid and owner_id=uid;
    update agent_private.turns set status='completed',result=v_result,lease_hash=null,lease_until=null where id=t.id;
  elsif p_action='suspend' then
    update agent_private.turns set status=case when exists(
      select 1 from agent_private.operations where turn_id=t.id and status='started') then 'needs_review' else 'suspended' end,
      lease_hash=null,lease_until=null where id=t.id;
  else raise exception 'invalid action' using errcode='22023'; end if;
  return jsonb_build_object('ok',true);
end $$;

create function public.proposal_agent_turn(p_secret text,p_action text,p_args jsonb default '{}') returns jsonb
language sql security invoker set search_path='' as $$ select agent_private.api(p_secret,p_action,p_args) $$;
revoke all on function agent_private.api(text,text,jsonb) from public,anon;
revoke all on function public.proposal_agent_turn(text,text,jsonb) from public,anon;
grant execute on function agent_private.api(text,text,jsonb),public.proposal_agent_turn(text,text,jsonb) to authenticated;

