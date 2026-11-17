"""Real migration, queue, checkpoints and billing in isolated Docker Postgres."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

pytest.importorskip("langgraph")

from server.proposal.cloud_review import ReviewMeterStore, run_delivery
from server.proposal.workbench_metering import WorkbenchMeter

ROOT = Path(__file__).resolve().parents[1]
UID = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"

BACKEND = "a" * 64
WORKER = "b" * 64


def literal(value):
    return "'" + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)).replace("'", "''") + "'"


@pytest.fixture
def cloud_db(sql, monkeypatch):
    sql("""create role service_role bypassrls;
      create schema extensions; create extension pgcrypto with schema extensions;
      create table public.documents(id text primary key,project_id text references projects(id) on delete cascade,
        original_name text,kind text,extracted_text text,extraction_status text,
        confirmed boolean,confirmation_origin text,low_confidence_count int default 0);
      create table public.chunks(id text primary key,project_id text references projects(id) on delete cascade,
        document_id text references documents(id) on delete cascade,source_name text,source_ref text,content text);
      create table public.token_usages(id text primary key,project_id text references projects(id) on delete cascade,
        task_type text,model text,prompt_tokens int,completion_tokens int,total_tokens int,latency_ms int,key_fingerprint text,
        created_at timestamptz default now());
      create schema vault;
      create table vault.secrets(id uuid primary key default gen_random_uuid(),name text unique,secret text);
      create view vault.decrypted_secrets as select id,name,secret as decrypted_secret from vault.secrets;
      create function vault.create_secret(text,text,text) returns uuid language sql as $$
        insert into vault.secrets(secret,name) values($1,$2) returning id $$;
      create function vault.update_secret(uuid,text,text,text) returns void language sql as $$
        update vault.secrets set secret=$2,name=$3 where id=$1 $$;
      create schema cron;
      create table cron.job(jobid bigint generated always as identity,jobname text unique,schedule text,command text);
      create function cron.schedule(text,text,text) returns bigint language sql as $$
        insert into cron.job(jobname,schedule,command) values($1,$2,$3)
        on conflict(jobname) do update set schedule=excluded.schedule,command=excluded.command returning jobid $$;
      create schema net;
      create table net.requests(id bigint generated always as identity,url text,body jsonb,headers jsonb);
      create function net.http_post(url text,body jsonb,params jsonb default '{}',headers jsonb default '{}',
        timeout_milliseconds int default 2000) returns bigint language sql as $$
        insert into net.requests(url,body,headers) values($1,$2,$4) returning id $$;
    """)
    for filename in ("20261011173308_workbench_credit_ledger.sql", "20261012120000_workbench_backend_capability.sql",
                     "20261113090000_workbench_model_interactions.sql", "20261117135553_langgraph_cloud_review.sql"):
        sql((ROOT / "supabase/migrations" / filename).read_text())
    sql(f"""update workbench_backend_capability set secret_hash=sha256(convert_to('{BACKEND}','UTF8'));
      select public.workbench_grant_payment_credits('{UID}','isolated-review-fixture',1000000);
      update projects set state_json='{{"skill_id":"innovation-research","budget":"2000元"}}',
        outline_json='[{{"id":"method","title":"研究方法"}},{{"id":"budget","title":"预算"}}]',
        sections_json='{{"method":"已完成实验，原始记录待补充。","budget":"预算合计为3000元。"}}' where id='p';
      insert into documents values('d','p','合成通知.txt','source','实验记录应保留原始数据。','completed',true,'user',0);
      insert into chunks values('c','p','d','合成通知.txt','第 2 页','实验记录应保留原始数据。');
      select public.proposal_review_configure('{BACKEND}','{WORKER}',true);
    """)
    monkeypatch.setenv("REVIEW_WORKER_SECRET", WORKER)
    return sql


def user(db, action, args=None, *, owner=UID, secret=BACKEND, ok=True):
    payload = {"project_id": "p", **(args or {})}
    result = db(f"set role authenticated; set request.jwt.claim.sub='{owner}'; select public.proposal_review_api("
                f"'{action}',{literal(payload)},{literal(secret)});", ok=ok)
    return json.loads(result) if result and ok else None


def submit(db, *, mode="rules", key="one-click"):
    fingerprint = user(db, "list")["current_fingerprint"]
    return user(db, "submit", {"mode": mode, "key": key, "fingerprint": fingerprint})


def worker(db, action, job=None, *, secret=WORKER, ok=True, **args):
    payload = {**({"id": job["id"], "lease": job["lease"]} if job else {}), **args}
    result = db(f"set role anon; select public.proposal_review_worker({literal(secret)},'{action}',{literal(payload)});", ok=ok)
    return json.loads(result) if result and ok else None


def delivery(db):
    return db("select id from review_private.deliveries where not consumed order by expires_at limit 1")


class SQLRPC:
    def __init__(self, db):
        self.db, self.job = db, None

    def call(self, action, **args):
        return worker(self.db, action, self.job, **args)


def advance(db, model, *, rpc_factory=None):
    rpc = SQLRPC(db)
    store = type("CloudStore", (), {"project_url": "https://synthetic.supabase.co", "publishable_key": "synthetic"})()
    for _ in range(12):
        token = delivery(db)
        if not token:
            break
        result = run_delivery(store, token, rpc_factory=rpc_factory or (lambda *_: rpc), model_factory=lambda _rpc: model(_rpc))
    return result


def test_authority_snapshot_submission_and_signed_notification(cloud_db):
    db = cloud_db
    for role in ("anon", "authenticated"):
        db(f"set role {role}; select * from review_private.runs", ok=False)
        db(f"set role {role}; select review_private.snapshot('p','{UID}')", ok=False)
    user(db, "list", owner=OTHER, ok=False)
    fp = user(db, "list")["current_fingerprint"]
    user(db, "submit", {"key": "x", "fingerprint": fp, "mode": "rules"}, secret="wrong", ok=False)
    first = submit(db)
    assert "snapshot" not in first and "lease" not in first
    assert submit(db)["id"] == first["id"]
    assert user(db, "submit", {"key": "one-click", "fingerprint": fp, "mode": "model"})["conflict"]
    db("update projects set sections_json='{{}}' where id='other'".replace("{{}}", "{}"))
    user(db, "get", {"id": first["id"]}, owner=OTHER, ok=False)
    request = json.loads(db("select jsonb_build_object('headers',headers,'body',body) from net.requests limit 1"))
    assert list(request["body"]) == ["delivery_id"]
    assert WORKER not in json.dumps(request)
    assert BACKEND not in json.dumps(request)
    assert len(request["headers"]["x-review-signature"]) == 64


def test_cloud_graph_checkpoints_resume_and_project_delete_cascades(cloud_db):
    db = cloud_db
    first = submit(db)
    db("update projects set sections_json='{}' where id='p'")
    result = advance(db, lambda _rpc: lambda *_: pytest.fail("rules requested no inference"))
    assert result["status"] == "completed"
    report = user(db, "get", {"id": first["id"]})
    assert report["report"]["fingerprint"] == first["fingerprint"]
    assert any("预算信息可能不一致" in issue["problem"] for issue in report["report"]["issues"])
    assert [e["node"] for e in report["events"]] == ["load_snapshot", "retrieve", "rules", "verify", "save_report"]
    assert db("select sections_json from projects where id='p'") == "{}"
    assert int(db("select count(*) from review_private.checkpoints")) > 0
    db("delete from projects where id='p'")
    for table in ("runs", "checkpoints", "writes", "events", "deliveries"):
        assert db(f"select count(*) from review_private.{table}") == "0"


def billed_model(calls):
    def factory(rpc):
        def call(run, _content, _schema):
            calls.append(1)
            meter = WorkbenchMeter(ReviewMeterStore(rpc), UID, 692, "p")
            meter.usage.find = lambda *_: (12345, "synthetic-receipt")
            request_id = meter.reserve("deepseek-v4-flash")
            meter.complete_interaction(request_id, "deepseek-v4-flash", '{"issues": []}')
            meter.settle(request_id, "deepseek-v4-flash")
            return {"content": '{"issues": []}', "metrics": {"eval_count": 10}}
        return call
    return factory


def test_real_credit_ledger_is_reserved_and_settled_once(cloud_db):
    db, calls = cloud_db, []
    first = submit(db, mode="model")
    assert advance(db, billed_model(calls))["status"] == "completed"
    assert calls == [1]
    assert db(f"select balance_micro_cny from workbench_credit_wallets where user_id='{UID}'") == "987655"
    assert db("select count(*) from workbench_usage_events") == "1"
    assert db("select count(*) from workbench_credit_transactions where kind='usage_reserve'") == "1"
    report = user(db, "get", {"id": first["id"]})
    assert report["billing"]["status"] == "settled"
    assert report["billing"]["actual_micro_cny"] == 12345
    assert submit(db, mode="model")["id"] == first["id"]
    assert db("select count(*) from workbench_model_interactions where status='completed'") == "1"
    assert db("select count(*) from token_usages where task_type='proposal_review'") == "1"


def test_unknown_inference_and_expired_lease_never_replay_or_refund(cloud_db):
    db = cloud_db
    run = submit(db, mode="model")
    job = worker(db, "claim", delivery_id=delivery(db))
    worker(db, "model_begin", job)
    request_id = "o2x1_" + uuid.uuid4().hex
    worker(db, "reserve", job, request_id=request_id, model="deepseek-v4-flash", api_key_id=692)
    worker(db, "reserve", job, request_id="o2x1_" + uuid.uuid4().hex, model="deepseek-v4-flash", api_key_id=692, ok=False)
    worker(db, "settle", job, request_id="o2x1_" + uuid.uuid4().hex, amount=0, provider_id="fake", ok=False)
    db("update review_private.runs set lease_until=now()-interval '1 minute'")
    db("select review_private.dispatch()")
    assert user(db, "get", {"id": run["id"]})["status"] == "needs_review"
    assert db("select count(*) from workbench_usage_events where status='reserved'") == "1"
    assert db("select count(*) from workbench_credit_transactions where kind='usage_refund'") == "0"
    worker(db, "context", job, ok=False)
    worker(db, "claim", delivery_id=str(uuid.uuid4()), secret="wrong", ok=False)


def test_insufficient_credits_never_creates_paid_task(cloud_db):
    db = cloud_db
    db(f"update workbench_credit_wallets set balance_micro_cny=0 where user_id='{UID}'")
    assert submit(db, mode="model")["insufficient_credits"] is True
    assert db("select count(*) from review_private.runs") == "0"
    assert submit(db)["mode"] == "rules"
    assert db("select count(*) from workbench_usage_events") == "0"


def test_commit_then_timeout_reuses_response_and_never_rebills(cloud_db):
    db, calls = cloud_db, []
    run = submit(db, mode="model")
    flaky = SQLRPC(db)
    original = flaky.call
    failed = []

    def call(action, **args):
        result = original(action, **args)
        if action == "model_result" and not failed:
            failed.append(True)
            raise RuntimeError("synthetic lost acknowledgement")
        return result

    flaky.call = call
    advance(db, billed_model(calls), rpc_factory=lambda *_: flaky)
    assert calls == [1]
    db("update review_private.runs set notified_until=now()-interval '1 minute'; select review_private.dispatch()")
    assert advance(db, billed_model(calls))["status"] == "completed"
    assert calls == [1]
    assert user(db, "get", {"id": run["id"]})["billing"]["status"] == "settled"
