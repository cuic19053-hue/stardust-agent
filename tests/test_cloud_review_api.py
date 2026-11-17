from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.proposal import cloud_review
from server.proposal.review_api import register_review_api

SECRET = "b" * 64
FINGERPRINT = "c" * 64


@pytest.fixture
def cloud_client(monkeypatch):
    monkeypatch.setenv("CLOUD_REVIEW_ENABLED", "true")
    monkeypatch.setenv("WORKBENCH_BACKEND_SECRET", "a" * 64)
    monkeypatch.setenv("REVIEW_WORKER_SECRET", SECRET)
    monkeypatch.setenv("REVIEW_CONFIGURE_SECRET", "d" * 64)
    calls = []
    class Store:
        project_url = "https://synthetic.supabase.co"
        publishable_key = "synthetic"
        result = {"id": "a" * 32, "status": "pending", "fingerprint": FINGERPRINT}
        def get_project(self, project_id):
            return {"id": project_id} if project_id == "p" else None
        def _rest(self, path, method, payload):
            calls.append(payload)
            action = payload["p_action"]
            if action == "list":
                return {"items": [], "current_fingerprint": FINGERPRINT}
            return self.result
    store = Store()
    monkeypatch.setattr(cloud_review.ReviewRPC, "call", lambda _self, action, **args: {"enabled": True})
    app = FastAPI()
    register_review_api(app, lambda: store, lambda: True, lambda: True)
    with TestClient(app) as client:
        yield client, store, calls


def test_cloud_capabilities_and_submission_never_accept_browser_model_or_credentials(cloud_client):
    client, _store, calls = cloud_client
    capabilities = client.get("/api/reviews/capabilities").json()
    assert capabilities["enabled"] and capabilities["execution"] == "cloud_queue"
    assert "2 积分" in capabilities["ai_label"]
    body = {"fingerprint": FINGERPRINT, "idempotency_key": "click", "mode": "model"}
    result = client.post("/api/projects/p/reviews", json=body)
    assert result.status_code == 202
    assert calls[-1]["p_args"] == {"project_id": "p", "key": "click", "fingerprint": FINGERPRINT, "mode": "model",
                                  "parent_id": None, "origin": "manual", "graph_version": "proposal-review-cloud-v2"}
    assert calls[-1]["p_secret"] == "a" * 64
    assert client.post("/api/projects/p/reviews", json={**body, "model": "untrusted"}).status_code == 422
    assert client.post("/api/projects/other/reviews", json=body).status_code == 404


def test_cloud_credit_failure_is_explicit_402(cloud_client):
    client, store, _calls = cloud_client
    store.result = {"insufficient_credits": True}
    response = client.post("/api/projects/p/reviews", json={"fingerprint": FINGERPRINT, "idempotency_key": "click", "mode": "model"})
    assert response.status_code == 402
    assert "2 积分" in response.json()["detail"]


def test_forged_scheduler_tick_never_claims_work(cloud_client, monkeypatch):
    client, _store, _calls = cloud_client
    claimed = []
    monkeypatch.setattr(cloud_review, "run_delivery", lambda *_: claimed.append(1))
    result = client.post("/api/reviews/internal/tick", json={"delivery_id": str(uuid.uuid4())})
    assert result.status_code == 403 and claimed == []


def test_valid_signed_delivery_can_run_without_user_jwt(cloud_client, monkeypatch):
    client, _store, _calls = cloud_client
    claimed = []
    delivery, stamp = str(uuid.uuid4()), str(int(time.time()))
    signature = hmac.new(SECRET.encode(), f"stardust-review:{delivery}:{stamp}".encode(), hashlib.sha256).hexdigest()
    monkeypatch.setattr(cloud_review, "run_delivery", lambda _store, token: claimed.append(token) or {"status": "pending"})
    result = client.post("/api/reviews/internal/tick", json={"delivery_id": delivery},
                         headers={"x-review-timestamp": stamp, "x-review-signature": signature})
    assert result.status_code == 200 and claimed == [delivery]
    assert not cloud_review.valid_delivery(delivery, str(int(stamp) - 91), signature, SECRET, now=int(stamp))
    assert not cloud_review.valid_delivery(str(uuid.uuid4()), stamp, signature, SECRET)


def test_scheduler_configuration_requires_its_narrow_operator_capability(cloud_client, monkeypatch):
    client, _store, _calls = cloud_client
    configured = []
    monkeypatch.setattr(cloud_review, "configure_scheduler", lambda _store, enabled: configured.append(enabled) or {"enabled": enabled})
    path = "/api/admin/configure-review-scheduler"
    assert client.post(path, json={"enabled": True}).status_code == 403
    assert client.post(path, json={"enabled": True}, headers={"x-admin-token": "existing-admin"}).status_code == 403
    assert configured == []
    response = client.post(path, json={"enabled": True}, headers={"x-review-ops-token": "d" * 64})
    assert response.status_code == 200 and configured == [True]


def test_unavailable_database_keeps_the_cloud_entry_hidden(cloud_client, monkeypatch):
    client, _store, _calls = cloud_client
    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic transport failure")
    monkeypatch.setattr(cloud_review.ReviewRPC, "call", fail)
    assert client.get("/api/reviews/capabilities").json()["enabled"] is False


def test_cloud_model_timeout_is_one_paid_attempt_with_a_bounded_budget(monkeypatch):
    from server.proposal import agent
    monkeypatch.setenv("WOPALLY_WORKBENCH_API_KEY_ID", "692")
    monkeypatch.setenv("WOPALLY_WORKBENCH_API_KEY", "synthetic-secret")
    monkeypatch.setenv("WOPALLY_MODEL_BASE_URL", "https://synthetic-provider.test/v1")
    requests, actions = [], []
    class RPC:
        def call(self, action, **args):
            actions.append(action)
            return {"status": "reserved"} if action == "reserve" else {"ok": True}
    def timeout(request, *, timeout):
        requests.append((request, timeout))
        raise TimeoutError("synthetic upstream timeout")
    monkeypatch.setattr(agent, "wopally_urlopen", timeout)
    model = cloud_review.CloudReviewModel(RPC())
    with pytest.raises(agent.ModelUnavailable):
        model({"owner_id": "owner", "project_id": "project", "model": "deepseek-v4-flash"},
              {"sections": {"method": "合成测试"}}, {"type": "object"})
    assert len(requests) == 1
    assert 30 < requests[0][1] < 50
    assert json.loads(requests[0][0].data)["thinking"] == {"type": "disabled"}
    assert actions == ["reserve", "interaction"]  # Unknown outcomes retain their hold.


def test_cloud_failure_logs_only_safe_error_metadata(monkeypatch, caplog):
    monkeypatch.setenv("REVIEW_WORKER_SECRET", SECRET)
    actions = []
    class RPC:
        job = None
        def call(self, action, **args):
            actions.append(action)
            if action == "claim":
                return {"id": "a" * 32, "graph_version": cloud_review.CLOUD_GRAPH_VERSION}
            return {"ok": True}
    def fail(*_args, **_kwargs):
        cause = urllib.error.HTTPError("https://provider.test/?key=private-value", 502,
                                       "private-prompt-value", None, None)
        raise RuntimeError("private-response-value") from cause
    monkeypatch.setattr(cloud_review, "build_review_graph", fail)
    store = type("Store", (), {"project_url": "synthetic", "publishable_key": "synthetic"})()
    result = cloud_review.run_delivery(store, str(uuid.uuid4()), rpc_factory=lambda *_: RPC(), model_factory=lambda _: None)
    assert result["status"] == "needs_review" and actions == ["claim", "recover"]
    assert "http_status=502" in caplog.text and "error=RuntimeError" in caplog.text
    assert "private-value" not in caplog.text
    assert "private-prompt-value" not in caplog.text
    assert "private-response-value" not in caplog.text
