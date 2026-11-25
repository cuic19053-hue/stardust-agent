"""Public replay, transport privacy and explicit, project-bound oral filing."""
import hashlib
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
    # The public replay records the answers and checks from the published experiment.
    frozen = json.dumps(data["examples"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(frozen).hexdigest() == "ab478627dfd8fd23c5761709e16750334e898bc124a6f354e132e8b747b7c7d4"
    for example in data["examples"]:
        for condition in ("base", "tuned"):
            row = example[condition]
            assert row["finish"] == "complete" and row["output_tokens"] > 0
            archived = intake.IntakeCandidate.model_validate(row["review"]["candidate"])
            assert not intake.evidence_errors(archived, example["turns"])
    assert client.post(f"/api/projects/{pid}/intake/propose").status_code == 401
    assert client.post(f"/api/projects/{pid}/intake/confirm", json={}).status_code == 401
    model._post.assert_not_called()


def test_label_in_student_quote_selects_one_field_and_binds_confirmation(api):
    client, store, pid, model = api
    store.add_message(pid, "user", "我的专业是文物保护。")
    wrong = fact("identity.college", "stated", "文物保护", "文物保护")
    model._post.return_value = response(wrong, {**wrong, "field": "identity.name"}, wrong)
    url = f"/api/projects/{pid}/intake"
    preview = client.post(url + "/propose").json()
    row, = preview["facts"]
    assert row["fact"] == fact("identity.major", "stated", "文物保护", "我的专业是文物保护")
    assert not store.get_project(pid)["state"].get(intake.ARCHIVE)
    payload = body(preview)
    # A ticket for an old, incorrectly labelled candidate no longer passes source review.
    old_fact = intake.IntakeFact.model_validate(wrong)
    old_ticket = intake.confirmation_ticket(payload["source_revision"], old_fact, payload["model"])
    assert client.post(url + "/confirm", json={**payload, "fact": wrong, "ticket": old_ticket}).status_code == 422
    assert client.post(url + "/confirm", json=payload).status_code == 200
    assert model._post.call_count == 1


def test_one_sided_budget_conflict_is_rebuilt_and_follow_up_names_both_claims(api):
    client, store, pid, model = api
    budget = "project.budget_yuan"
    store.add_message(pid, "user", "我的项目预算是4100元。")
    store.add_message(pid, "user", "申报表上项目预算是6900元。")
    model._post.return_value = response(fact(budget, "conflict", None, "我的项目预算是4100元"))
    url = f"/api/projects/{pid}/intake"
    result = client.post(url + "/propose").json()
    row, = result["facts"]
    assert row["fact"]["status"] == "conflict" and row["fact"]["value"] is None
    assert row["fact"]["evidence"] == [
        {"turn_id": "u1", "quote": "我的项目预算是4100元"},
        {"turn_id": "u2", "quote": "申报表上项目预算是6900元"},
    ]
    question, = result["questions"]
    assert "4100元" in question and "6900元" in question
    assert not row["can_confirm"] and client.post(url + "/confirm", json=body(result)).status_code == 422
    assert not store.get_project(pid)["state"].get(intake.ARCHIVE)


@pytest.mark.parametrize("second", [
    "还需要找表核实。", "同学的项目预算是6900元。", "另一个项目预算是6900元。", "上学期项目预算是6900元。",
    "另一个项目，项目预算是6900元。", "上学期，项目预算是6900元。", "项目预算是6900元吗？",
])
def test_unrelated_or_missing_second_claim_cannot_complete_conflict(api, second):
    client, store, pid, model = api
    store.add_message(pid, "user", "我的项目预算是4100元。")
    store.add_message(pid, "user", second)
    model._post.return_value = response(fact("project.budget_yuan", "conflict", None, "我的项目预算是4100元"))
    result = client.post(f"/api/projects/{pid}/intake/propose").json()
    assert result["decision"] == "needs_manual_review" and result["facts"] == result["questions"] == []


def test_explicit_refusal_overrides_a_guessed_scale_without_losing_gpa(api):
    client, store, pid, model = api
    store.add_message(pid, "user", "我的绩点是3.51。满分我不想说。")
    model._post.return_value = response(
        fact("academic.gpa", "stated", "3.51", "我的绩点是3.51"),
        fact("academic.gpa_scale", "unknown", "5", "满分我不想说"),
    )
    result = client.post(f"/api/projects/{pid}/intake/propose").json()
    assert result["decision"] == "pending_student_confirmation"
    assert [row["fact"]["field"] for row in result["facts"]] == ["academic.gpa", "academic.gpa_scale"]
    assert result["facts"][1]["fact"] == fact("academic.gpa_scale", "withheld", None, "满分我不想说")
    assert not result["facts"][1]["can_confirm"]
    assert result["questions"] == [] and result["saved"] is False


def test_partial_review_cannot_release_another_value_for_a_damaged_field(api):
    client, store, pid, model = api
    store.add_message(pid, "user", "我的绩点是3.51。我的专业是城市规划。")
    model._post.return_value = response(
        fact("academic.gpa", "stated", "3.51", "我的绩点是3.51"),
        fact("academic.gpa", "conflict", None, "我的绩点是3.51"),
        fact("identity.major", "stated", "城市规划", "我的专业是城市规划"),
    )
    result = client.post(f"/api/projects/{pid}/intake/propose").json()
    assert result["decision"] == "partial_review"
    assert [r["fact"]["field"] for r in result["facts"]] == ["identity.major"]
    model._post.return_value["message"]["finish_reason"] = "length"
    truncated = client.post(f"/api/projects/{pid}/intake/propose").json()
    assert truncated["decision"] == "needs_manual_review" and not truncated["facts"]


def test_speaker_denies_having_an_advisor_next_to_a_template(api):
    client, store, pid, model = api
    store.add_message(pid, "user", "范文里的导师是侯老师。我本人还没有导师。")
    model._post.return_value = response(fact("research.experience", "unknown", None, "我本人还没有导师"))
    result = client.post(f"/api/projects/{pid}/intake/propose").json()
    assert [r["fact"] for r in result["facts"]] == [fact("project.advisor", "negated", None, "我本人还没有导师")]
    assert not result["questions"]


@pytest.mark.parametrize(("field", "status", "value"), [
    ("project.name", "stated", "潮汐模型"), ("project.name", "planned", None),
    ("project.stage", "stated", "一个潮汐模型"),
])
def test_planned_artifact_keeps_its_source_and_can_only_be_confirmed_as_planned(api, field, status, value):
    client, store, pid, model = api
    text = "我们计划制作一个潮汐模型，还没开始测潮。"
    store.add_message(pid, "user", text)
    model._post.return_value = response(fact(field, status, value, text))
    url = f"/api/projects/{pid}/intake"
    result = client.post(url + "/propose").json()
    row, = result["facts"]
    assert row["fact"] == fact("project.expected_output", "planned", "一个潮汐模型", "我们计划制作一个潮汐模型")
    payload = body(result)
    assert client.post(url + "/confirm", json={**payload, "fact": {**payload["fact"], "status": "stated"}}).status_code == 422
    assert client.post(url + "/confirm", json=payload).status_code == 200


def test_reconcile_two_real_budget_values_even_when_the_second_claim_is_malformed(api):
    client, store, pid, model = api
    for v in ("4800", "7600"):
        store.add_message(pid, "user", f"项目预算是{v}元。")
    field = "project.budget_yuan"
    model._post.return_value = response(fact(field, "stated", "4800", "项目预算是4800元"),
                                        fact(field, "negated", "7600", "项目预算是7600元", "u2"))
    result = client.post(f"/api/projects/{pid}/intake/propose").json()
    row, = result["facts"]
    assert row["fact"]["status"] == "conflict" and row["fact"]["value"] is None
    assert [e["turn_id"] for e in row["fact"]["evidence"]] == ["u1", "u2"]
    assert result["questions"] and not row["can_confirm"]
