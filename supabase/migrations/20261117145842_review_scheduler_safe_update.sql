-- Production uses safeupdate: even a singleton update must identify its row.
create or replace function review_private.configure(p_backend text,p_worker text,p_enabled boolean) returns jsonb
language plpgsql security definer set search_path='' as $$
declare sid uuid; jid bigint;
begin
  perform public.workbench_check_backend(p_backend);
  if p_worker is null or octet_length(p_worker)<>64 then raise exception 'invalid capability'; end if;
  update review_private.config set worker_hash=pg_catalog.sha256(pg_catalog.convert_to(p_worker,'UTF8')),
    enabled=p_enabled where singleton;
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
