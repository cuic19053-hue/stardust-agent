"""Public replay, transport privacy and explicit, project-bound oral filing."""
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import server.app as app
from proposal import student_intake_workbench as intake
from proposal.storage import Store
from proposal.workbench_metering import WorkbenchMeter

TEXT = "我准备提交一份巡河记录。"


def fact(field, status, value, quote, turn="u1"):
    return {"field": field, "status": status, "value": value, "record_id": "self",
            "evidence": [{"turn_id": turn, "quote": quote}]}


def response(*facts):
    return {"message": {"content": json.dumps({
        "schema_version": "student-intake-v1", "facts": facts, "follow_up_fields": [],
    }, ensure_ascii=False)}}


@pytest.fixture
def api(tmp_path, monkeypatch):
    store = Store(tmp_path)
    pid = store.create_project()["id"]
    model = Mock(model="deepseek-v4-flash", provider="openai_compatible", meter=None)
    model._post.return_value = response(fact("project.expected_output", "planned", "巡河记录", TEXT))
    monkeypatch.setattr(app, "store", store)
    monkeypatch.setattr(app, "_agent_for_config", lambda *args: model)
    monkeypatch.setattr(app, "CLOUD_MODE", False)
    with TestClient(app.app) as client:
        yield client, store, pid, model


def body(result):
    row = result["facts"][0]
    return {"confirmed": True, "source_revision": result["source_revision"],
            "model": result["model"], "fact": row["fact"], "ticket": row["ticket"]}


def test_preview_confirm_retry_and_mismatched_context(api):
    client, store, pid, model = api
    store.add_message(pid, "user", TEXT)
    url = f"/api/projects/{pid}/intake"
    result = client.post(url + "/propose").json()
    assert result["model"] == "deepseek-v4-flash" and result["saved"] is False
    assert intake.ARCHIVE not in store.get_project(pid)["state"]
    assert model._post.call_args.args[1]["format"] == "json"
    payload = body(result)
    for changed in ({**payload, "confirmed": False}, {**payload, "model": "other"}):
        assert client.post(url + "/confirm", json=changed).status_code == 422
    forged = deepcopy(payload)
    forged["fact"]["status"] = "stated"
    assert client.post(url + "/confirm", json=forged).status_code == 422
    for _ in range(2):
        assert client.post(url + "/confirm", json=payload).status_code == 200
    state = store.get_project(pid)["state"]
    record, = state[intake.ARCHIVE]
    assert record["fact"] == payload["fact"] and record["confirmed_at"]
    assert not state.get("confirmed_facts")
    compact = app.OllamaAgent._compact_project(store.get_project(pid))
    assert intake.ARCHIVE not in compact["confirmed_state"]
    other = store.create_project()["id"]
    store.add_message(other, "user", TEXT)
    assert client.post(f"/api/projects/{other}/intake/confirm", json=payload).status_code == 409
    store.add_message(pid, "user", "我还要核对这个计划。")
    assert client.post(url + "/confirm", json=payload).status_code == 409


def test_preview_withdraws_conflict_and_shows_both_quotes(api):
    client, store, pid, model = api
    store.add_message(pid, "user", "我的绩点是3.11。")
    store.add_message(pid, "user", "我的绩点是3.73。")
    model._post.return_value = response(fact("academic.gpa", "stated", "3.73", "我的绩点是3.73", "u2"))
    url = f"/api/projects/{pid}/intake"
    result = client.post(url + "/propose").json()
    row, = result["facts"]
    assert row["fact"]["status"] == "conflict" and not row["can_confirm"]
    assert len(row["fact"]["evidence"]) == 2 and result["questions"]
    assert client.post(url + "/confirm", json=body(result)).status_code == 422


def test_transport_strips_out_of_scope_private_and_other_speakers(api):
    client, store, pid, model = api
    for role, content in [("user", "我正在申请国家奖学金。我家收入六万元。"),
                          ("assistant", "这是助手写的范文。"),
                          ("user", "同学的绩点是3.91。我的学号是2026000123。"),
                          ("user", "我准备申请图书志愿者。")]:
        store.add_message(pid, role, content)
    model._post.return_value = response(fact("financial.annual_income_yuan", "stated", "60000", "我家收入六万元"))
    result = client.post(f"/api/projects/{pid}/intake/propose").json()
    content = model._post.call_args.args[1]["messages"][-1]["content"]
    assert "图书志愿者" in content
    assert all(s not in content for s in ("六万元", "助手写的", "3.91", "2026000123"))
    assert result["facts"] == result["questions"] == []


def test_manual_review_and_history_bound_do_not_write(api):
    client, store, pid, model = api
    store.add_message(pid, "user", TEXT)
    model._post.return_value = {"message": {"content": "broken"}}
    url = f"/api/projects/{pid}/intake/propose"
    assert client.post(url).json()["decision"] == "needs_manual_review"
    for _ in range(intake.MAX_MESSAGES):
        store.add_message(pid, "user", TEXT)
    assert client.post(url).status_code == 422
    assert model._post.call_count == 1
    assert intake.ARCHIVE not in store.get_project(pid)["state"]


def test_intake_meter_keeps_billing_but_discards_content(api):
    client, store, pid, model = api
    ledger = Mock()
    model.meter = WorkbenchMeter(ledger, "owner", 12, pid)
    store.add_message(pid, "user", TEXT)
    assert client.post(f"/api/projects/{pid}/intake/propose").status_code == 200
    usage_id = model.meter.reserve(model.model, input_text=TEXT)
    model.meter.complete_interaction(usage_id, model.model, "private output")
    ledger.reserve_workbench_usage.assert_called_once()
    assert ledger.record_workbench_interaction.call_args_list[0].kwargs["input_text"] == ""
    assert ledger.record_workbench_interaction.call_args_list[1].kwargs["output_text"] == ""


def test_public_replay_and_private_routes_in_cloud(api, monkeypatch):
    client, _, pid, model = api
    monkeypatch.setattr(app, "CLOUD_MODE", True)
    page = client.get("/student-intake")
    assert page.status_code == 200 and "同底座 MLX LoRA" in page.text
    data = client.get("/static/student-intake-demo.json").json()
    assert data["synthetic"] and len(data["examples"]) == 5
    for example in data["examples"]:
        for condition in ("base", "tuned"):
            row = example[condition]
            assert row["finish"] == "complete" and row["output_tokens"] > 0
            checked = intake.verified(row["raw"], example["turns"], example["turns"])
            assert (checked.model_dump() if checked else None) == row["review"]["candidate"]
    assert client.post(f"/api/projects/{pid}/intake/propose").status_code == 401
    assert client.post(f"/api/projects/{pid}/intake/confirm", json={}).status_code == 401
    model._post.assert_not_called()
