"""Hosted-style RPC tests in the network-isolated PostgreSQL fixture only."""
import json

from cloud_review_postgres_cases import BACKEND, UID, OTHER, literal, user, advance


def turn(db, action, args=None, *, owner=UID, secret=BACKEND, ok=True):
    payload = {"project_id": "p", "lease": "c" * 64, **(args or {})}
    raw = db(f"set role authenticated; set request.jwt.claim.sub='{owner}'; "
        f"select public.proposal_agent_turn({literal(secret)},{literal(action)},{literal(payload)});", ok=ok)
    return json.loads(raw) if ok and raw else None


def request(db, key="d", history=None):
    return {"key_hash": key * 64, "request_hash": "e" * 64, "input_digest": "f" * 64,
        "message": "合成测试输入", "snapshot": {"project": {"id": "p"}, "documents": [], "chunks": [],
        "history": history or []}}


def test_agent_rpc_private_owner_and_backend_boundaries(cloud_db):
    db = cloud_db
    turn(db, "open", request(db), secret="wrong", ok=False)
    turn(db, "open", request(db), owner=OTHER, ok=False)
    assert db("set role authenticated; select * from agent_private.turns", ok=False) == ""
    assert db("set role anon; select public.proposal_agent_turn('bad','list','{}')", ok=False) == ""
    task = turn(db, "open", request(db))
    assert task["status"] == "running"
    assert db("select count(*) from messages where project_id='p' and role='user'") == "1"
    assert "conflict" in turn(db, "open", request(db))
    assert "snapshot" not in turn(db, "list")[0]


def test_agent_known_steps_resume_once_and_completion_is_atomic(cloud_db):
    db = cloud_db
    args = request(db)
    task = turn(db, "open", args)
    op = {"id": task["id"], "idx": 0, "kind": "model", "request_hash": "a" * 64}
    assert turn(db, "begin", op) == {"cached": False}
    turn(db, "record", {"id": task["id"], "idx": 0, "result": {"type": "response", "message": {"content": "已保存的模型结果"}}})
    db(f"update agent_private.turns set lease_until=now()-interval '1 second' where id='{task['id']}'")
    args.update(lease="b" * 64)
    args["snapshot"]["history"] = [{"role": "user", "content": "合成测试输入"}]
    resumed = turn(db, "open", args)
    assert resumed["id"] == task["id"]
    cached = turn(db, "begin", {**op, "lease": "b" * 64})
    assert cached["cached"] and cached["result"]["message"]["content"] == "已保存的模型结果"
    assert "conflict" in turn(db, "record", {"id": task["id"], "idx": 0, "result": {"tampered": True}})
    result = {"message": "已完成的合成结果", "tool_calls": []}
    turn(db, "complete", {"id": task["id"], "lease": "b" * 64, "result": result})
    assert turn(db, "open", args)["result"] == result
    assert turn(db, "lookup", args)["result"] == result
    assert db("select count(*) from messages where project_id='p' and role='assistant'") == "1"
    assert db("select count(*) from workbench_usage_events") == "0"
    db("delete from projects where id='p'")
    assert db("select count(*) from agent_private.turns") == "0"
    assert db("select count(*) from agent_private.operations") == "0"


def test_agent_unknown_call_and_changed_inputs_cannot_be_reexecuted(cloud_db):
    db = cloud_db
    args = request(db)
    task = turn(db, "open", args)
    turn(db, "begin", {"id": task["id"], "idx": 0, "kind": "model", "request_hash": "a" * 64})
    db(f"update agent_private.turns set lease_until=now()-interval '1 second' where id='{task['id']}'")
    assert "conflict" in turn(db, "open", args)
    assert db(f"select status from agent_private.turns where id='{task['id']}'") == "needs_review"
    assert "conflict" in turn(db, "open", args)
    assert db("select count(*) from agent_private.operations") == "1"
    fresh = request(db, key="b", history=[{"role": "user", "content": "合成测试输入"}])
    new = turn(db, "open", fresh)
    turn(db, "suspend", {"id": new["id"]})
    fresh["input_digest"] = "a" * 64
    assert "conflict" in turn(db, "open", fresh)


def test_cloud_follow_up_uses_new_snapshot_and_preserves_original_report(cloud_db):
    db = cloud_db
    fp = user(db, "list")["current_fingerprint"]
    parent = user(db, "submit", {"key": "parent", "mode": "rules", "fingerprint": fp})
    advance(db, lambda *_: {})
    saved = user(db, "get", {"id": parent["id"]})
    assert saved["report"]["workflow_stage"] == "awaiting_input"
    body = {"key": "child", "mode": "rules", "fingerprint": fp, "parent_id": parent["id"], "origin": "chat_draft"}
    assert user(db, "submit", body)["conflict"]
    db("update projects set sections_json=" + literal({"method": "研究方法拟获得授权后开展实验。"}) + " where id='p'")
    body["fingerprint"] = user(db, "list")["current_fingerprint"]
    child = user(db, "submit", body)
    assert child["parent_id"] == parent["id"] and child["origin"] == "chat_draft"
    assert user(db, "submit", body)["id"] == child["id"]
    assert user(db, "submit", {**body, "parent_id": "0" * 32})["conflict"]
    assert user(db, "get", {"id": parent["id"]})["report"] == saved["report"]
