"""HTTP queue lifecycle, opt-in boundary, and project isolation."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("langgraph.checkpoint.sqlite")

import server.app as app
from proposal.review_worker import ReviewWorker
from proposal.storage import Store


@pytest.fixture
def client_store(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(app, "store", store)
    monkeypatch.setattr(app, "CLOUD_MODE", False)
    monkeypatch.setenv("LOCAL_REVIEW_ENABLED", "true")
    monkeypatch.delenv("VERCEL", raising=False)
    with TestClient(app.app) as client:
        yield client, store


def ready_project(store):
    project = store.create_project("合成预审项目")
    return store.update_project(project["id"], state={"skill_id": "innovation-research"},
        outline=[{"id": "method", "title": "研究方法"}], sections={"method": "研究方法待补充。"})


def test_disabled_by_default_and_never_initializes_cloud_checkpoint_tables(client_store, monkeypatch):
    client, store = client_store
    project = ready_project(store)
    monkeypatch.delenv("LOCAL_REVIEW_ENABLED")
    assert client.get("/api/reviews/capabilities").json()["enabled"] is False
    assert client.get(f"/api/projects/{project['id']}/reviews").status_code == 404
    with store.connect() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='review_runs'").fetchone() is None


def test_serverless_cannot_enable_local_review_even_with_flag(client_store, monkeypatch):
    client, store = client_store
    project = ready_project(store)
    monkeypatch.setenv("VERCEL", "1")
    assert client.get("/api/reviews/capabilities").json()["enabled"] is False
    assert client.get(f"/api/projects/{project['id']}/reviews").status_code == 404


def test_cloud_mode_cannot_enable_review(client_store, monkeypatch):
    _client, _store = client_store
    monkeypatch.setattr(app, "CLOUD_MODE", True)
    route = next(route for route in app.app.routes if getattr(route, "path", "") == "/api/reviews/capabilities")
    assert route.endpoint()["enabled"] is False


def test_cloud_project_review_routes_still_require_login(client_store, monkeypatch):
    client, store = client_store
    project = ready_project(store)
    monkeypatch.setattr(app, "CLOUD_MODE", True)
    assert client.get(f"/api/projects/{project['id']}/reviews").status_code == 401
    assert client.post(f"/api/projects/{project['id']}/reviews", json={
        "fingerprint": "a" * 64, "idempotency_key": "click", "mode": "model"}).status_code == 401


def test_http_submit_poll_reopen_and_duplicate_request(client_store):
    client, store = client_store
    project = ready_project(store)
    path = f"/api/projects/{project['id']}/reviews"
    current = client.get(path).json()
    assert len(current["current_fingerprint"]) == 64
    body = {"idempotency_key": "one-key", "fingerprint": current["current_fingerprint"], "mode": "rules"}
    first = client.post(path, json=body)
    assert first.status_code == 202
    run = first.json()
    assert run["status"] == "pending"
    assert "snapshot" not in run and "key_hash" not in run
    assert client.post(path, json=body).json()["id"] == run["id"]
    with ReviewWorker(store) as worker:
        worker.run_once()
    result = client.get(path + "/" + run["id"]).json()
    assert result["status"] == "completed"
    assert result["report"]["fingerprint"] == current["current_fingerprint"]
    assert len(client.get(path).json()["items"]) == 1
    assert client.post(path, json={**body, "mode": "model"}).status_code == 409
    other = ready_project(store)
    assert client.get(f"/api/projects/{other['id']}/reviews/{run['id']}").status_code == 404


def test_unsaved_or_unsupported_project_cannot_submit(client_store):
    client, store = client_store
    project = store.create_project()
    result = client.get(f"/api/projects/{project['id']}/reviews").json()
    assert result["current_fingerprint"] is None
    assert "保存" in result["unavailable_reason"]
    store.update_project(project["id"], state={"skill_id": "national-scholarship"}, sections={"x": "正文"})
    result = client.get(f"/api/projects/{project['id']}/reviews").json()
    assert "仅支持" in result["unavailable_reason"]


def test_html_serves_versioned_review_script(client_store):
    client, _store = client_store
    html = client.get("/").text
    assert f'/static/review.js?v={app.ASSET_VERSIONS["review.js"]}' in html
    assert 'id="draftReviewBtn"' in html


def test_follow_up_requires_new_input_preserves_parent_and_survives_reload(client_store):
    client, store = client_store
    project = ready_project(store)
    path = f"/api/projects/{project['id']}/reviews"
    fp = client.get(path).json()["current_fingerprint"]
    parent = client.post(path, json={"idempotency_key": "first", "fingerprint": fp}).json()
    with ReviewWorker(store) as worker:
        worker.run_once()
    report = client.get(path + "/" + parent["id"]).json()["report"]
    assert report["follow_up"] and report["workflow_stage"] == "awaiting_input"
    body = {"idempotency_key": "continue", "fingerprint": fp, "parent_id": parent["id"]}
    assert client.post(path, json=body).status_code == 409
    store.update_project(project["id"], sections={"method": "研究方法拟开展对比实验，获得授权后再进行采集。"})
    body["fingerprint"] = client.get(path).json()["current_fingerprint"]
    child = client.post(path, json=body)
    assert child.status_code == 202
    assert child.json()["parent_id"] == parent["id"]
    assert client.post(path, json=body).json()["id"] == child.json()["id"]
    assert client.get(path + "/" + parent["id"]).json()["report"] == report
    with ReviewWorker(Store(store.data_dir)) as worker:
        result = worker.run_once()
    assert result["report"]["parent_id"] == parent["id"]
    assert result["fingerprint"] != parent["fingerprint"]


def test_confirmed_chat_draft_automatically_queues_free_review_once(client_store):
    client, store = client_store
    project = ready_project(store)
    pid = project["id"]
    store.add_message(pid, "assistant", '<WritingBlock variant="document">## 研究方法\n\n'
                      '研究方法拟在获得授权后进行实验，按原始记录评价。\n</WritingBlock>')
    path = f"/api/projects/{pid}/chat-drafts"
    preview = client.get(path).json()
    assert preview["items"]
    saved = client.post(path + "/confirm", json={"snapshot": preview["snapshot"], "items": [
        {"id": preview["items"][0]["id"], "replace_existing": True}]})
    assert saved.status_code == 200
    run_id = saved.json()["review"]["run_id"]
    runs = client.get(f"/api/projects/{pid}/reviews").json()["items"]
    assert len(runs) == 1 and runs[0]["id"] == run_id
    assert runs[0]["mode"] == "rules" and runs[0]["origin"] == "chat_draft"
    from proposal.review_workflow import after_draft_saved
    assert after_draft_saved(store, pid, "chat_draft")["run_id"] == run_id


def test_section_generation_and_rules_share_the_saved_snapshot(client_store, monkeypatch):
    client, store = client_store
    project = ready_project(store)
    monkeypatch.setattr(app, "_metered_invoke", lambda *_: {"content": "研究方法拟在获得授权后开展实验。"})
    saved = client.post(f"/api/projects/{project['id']}/sections/method/generate", json={})
    assert saved.status_code == 200
    run = client.get(f"/api/projects/{project['id']}/reviews/" + saved.json()["review"]["run_id"]).json()
    assert run["origin"] == "section_generation" and run["mode"] == "rules"
    with ReviewWorker(store) as worker:
        report = worker.run_once()["report"]
    assert report["project_revision"] == saved.json()["material_revision"]


@pytest.mark.parametrize("stream", [False, True])
def test_real_chat_endpoint_duplicate_key_does_not_call_model_again(client_store, monkeypatch, stream):
    from proposal.agent import OllamaAgent
    from proposal.tools import ProposalTools
    from proposal.retrieval import ProjectRetriever
    client, store = client_store
    project = ready_project(store)
    agent = OllamaAgent(store, ProposalTools(store, ProjectRetriever(store)))
    monkeypatch.setattr(app, "agent", agent)
    monkeypatch.setattr(agent, "_policy_evidence_reply", lambda *_: None)
    calls = []
    response = {"role": "assistant", "content": "先明确一个可验证的研究问题。"}
    monkeypatch.setattr(agent, "_post", lambda *_: calls.append(1) or {"message": response})
    def events(_payload):
        calls.append(1)
        yield {"type": "delta", "text": response["content"]}
        yield {"type": "final", "message": response}
    monkeypatch.setattr(agent, "_stream_post", events)
    body = {"message": "分析一下项目的下一步", "idempotency_key": "chat-one", "stream": stream}
    first = client.post(f"/api/projects/{project['id']}/chat", json=body)
    retry = client.post(f"/api/projects/{project['id']}/chat", json=body)
    assert first.status_code == retry.status_code == 200
    if stream:
        assert "event: done" in first.text and '"replayed": true' in retry.text
    else:
        assert first.json()["turn_id"] == retry.json()["turn_id"]
        assert retry.json()["replayed"] is True
    assert calls == [1]
    assert len(store.messages(project["id"])) == 2
