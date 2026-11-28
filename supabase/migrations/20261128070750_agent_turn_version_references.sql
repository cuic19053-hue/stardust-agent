-- V2 stores version references. Existing in-flight v1 turns keep their history comparison.
create or replace function agent_private.api(p_secret text,p_action text,p_args jsonb) returns jsonb
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
  if p_action='recent' then
    return (select coalesce(jsonb_agg(to_jsonb(q) order by q.created_at desc,q.id desc),'[]'::jsonb)
      from (select id,case when status='running' and lease_until<=now() then 'interrupted' else status end as status,
        left(message,80) as preview,extract(epoch from created_at) as created_at
        from agent_private.turns where project_id=pid and owner_id=uid order by created_at desc,id desc
        limit 20 offset greatest(0,(p_args->>'offset')::integer)) q);
  end if;
  if p_action='detail' then
    return (select jsonb_build_object('id',id,'message',message,'status',
      case when status='running' and lease_until<=now() then 'interrupted' else status end,
      'answer',case when status='completed' then result->>'message' end)
      from agent_private.turns where id=p_args->>'id' and project_id=pid and owner_id=uid);
  end if;
  if p_action='resolve' then
    return (select jsonb_build_object('key_hash',key_hash) from agent_private.turns
      where id=p_args->>'id' and project_id=pid and owner_id=uid);
  end if;
  if p_action='lookup' then
    select * into t from agent_private.turns where project_id=pid and owner_id=uid and key_hash=p_args->>'key_hash';
    if not found then return null; end if;
    if t.request_hash is distinct from p_args->>'request_hash' then
      return jsonb_build_object('conflict','同一请求标识已用于不同内容或模型配置。'); end if;
    return jsonb_build_object('id',t.id,'status',t.status,'snapshot_version',coalesce(t.snapshot->'version','1'::jsonb),'result',case when t.status='completed' then t.result else null end);
  end if;
  if coalesce(p_args->>'lease','') !~ '^[a-f0-9]{64}$' then
    raise exception 'invalid turn lease' using errcode='22023'; end if;
  if p_action in ('open','open_v2') then
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
        (t.snapshot->'history')||jsonb_build_array(case when t.snapshot->>'version'='2'
          then jsonb_build_object('role','user','sha256',encode(sha256(convert_to(t.message,'UTF8')),'hex'))
          else jsonb_build_object('role','user','content',t.message) end)) with ordinality
        where ordinality>greatest(jsonb_array_length(t.snapshot->'history')+1-200,0);
      if t.input_digest<>p_args->>'input_digest' or expected_history is distinct from p_args#>'{snapshot,history}' then
        return jsonb_build_object('conflict','项目、材料或对话已变化，请保留原结果并发起新一轮。'); end if;
      rid:=t.id; snap:=t.snapshot;
    else
      if p_action='open_v2' then
        select coalesce(jsonb_agg(jsonb_build_object('role',m.role,'sha256',
          encode(sha256(convert_to(m.content,'UTF8')),'hex')) order by m.created_at,m.id),'[]'::jsonb)
          into expected_history from (select id,role,content,created_at from public.messages
          where project_id=pid order by created_at desc,id desc limit 200) m;
        if expected_history is distinct from p_args#>'{snapshot,history}' then
          return jsonb_build_object('conflict','对话历史已变化，请重新发起。'); end if;
      end if;
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
