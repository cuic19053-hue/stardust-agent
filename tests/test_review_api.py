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
