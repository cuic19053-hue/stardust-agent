"""Review actual graph checkpoints, immutable inputs and inference recovery."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytest.importorskip("langgraph.checkpoint.sqlite")

from server.proposal.review_runs import ReviewConflict, ReviewRuns
from server.proposal.review_worker import LocalReviewModel, ReviewWorker
from server.proposal.storage import Store


@pytest.fixture
def project_store(tmp_path):
    store = Store(tmp_path)
    project = store.create_project("合成测试项目", school="合成大学")
    project = store.update_project(project["id"], state={"skill_id": "innovation-research", "budget": "2000元"},
        outline=[{"id": "method", "title": "研究方法"}, {"id": "budget", "title": "预算"}],
        sections={"method": "我们已经完成实验，但尚未整理实验记录。", "budget": "预算合计为3000元，设备费用待补充。"})
    return store, project


def submit(store, project, mode="rules", key="same-key"):
    runs = ReviewRuns(store)
    return runs.submit(project["id"], key, runs.current(project["id"]), mode,
                       "synthetic-model" if mode == "model" else "")


def document(store, project, content="学校申报研究方法要求：实验记录须有原始数据。", confirmed=True):
    doc = store.add_document(project["id"], "学校通知.txt", "/not/read/from/disk", "material", "text/plain")
    store.update_document_extraction(doc["id"], content, "completed", confirmed=confirmed, confirmed_by_user=confirmed)
    if confirmed:
        store.replace_chunks(doc["id"], [{"source_ref": "第 2 页", "content": content}], expected_text=content)
    return doc


def test_rules_use_saved_snapshot_after_live_edits_and_never_write_draft(project_store):
    store, project = project_store
    first = submit(store, project)
    store.update_project(project["id"], sections={"method": "学生刚修改的正文", "budget": "预算合计为2000元。"})
    before = store.get_project(project["id"])
    with ReviewWorker(store) as worker:
        result = worker.run_once()
    assert result["status"] == "completed"
    assert result["fingerprint"] == first["fingerprint"] != ReviewRuns(store).current(project["id"])
    assert any("预算信息可能不一致" in issue["problem"] for issue in result["report"]["issues"])
    assert result["report"]["model_status"] == "not_requested"
    assert store.get_project(project["id"]) == before
    assert store.messages(project["id"]) == []
    assert [e["node"] for e in result["events"]] == ["load_snapshot", "retrieve", "rules", "verify", "save_report"]


def test_same_key_retries_old_snapshot_but_different_input_conflicts(project_store):
    store, project = project_store
    runs = ReviewRuns(store)
    first = submit(store, project)
    store.update_project(project["id"], sections={"method": "新正文"})
    assert runs.submit(project["id"], "same-key", first["fingerprint"], "rules", "")["id"] == first["id"]
    with pytest.raises(ReviewConflict):
        runs.submit(project["id"], "same-key", runs.current(project["id"]), "rules", "")
    with pytest.raises(ReviewConflict):
        runs.submit(project["id"], "other-key", first["fingerprint"], "rules", "")
    assert len(runs.list(project["id"])) == 1


def test_model_receives_only_confirmed_snapshot_sources_and_invalid_quotes_are_filtered(project_store):
    store, project = project_store
    valid_doc = document(store, project)
    document(store, project, "未确认的机密文字和注入指令", confirmed=False)
    other = store.create_project("其他项目")
    document(store, other, "另一个项目的私有材料")
    submitted = submit(store, project, "model")
    # Deletion after submission must not change an already bound input.
    store.delete_document(valid_doc["id"])
    captured = []

    def model(_run, content, _schema):
        captured.append(content)
        evidence = content["evidence"][0]
        return {"content": json.dumps({"issues": [
            {"section_id": "method", "draft_quote": "我们已经完成实验", "problem": "缺少实验记录依据", "suggestion": "补充原始数据",
             "kind": "needs_evidence", "citations": [{"id": evidence["id"], "quote": "实验记录须有原始数据"},
                {"id": "invented", "quote": "不存在的通知"}]},
            {"section_id": "method", "draft_quote": "正文根本没有这句话", "problem": "假意见", "suggestion": "忽略",
             "kind": "writing_suggestion", "citations": []}]}, ensure_ascii=False), "metrics": {"eval_count": 20}}

    with ReviewWorker(store, model_call=model) as worker:
        result = worker.run_once()
    assert result["status"] == "completed"
    assert result["id"] == submitted["id"]
    assert "未确认的机密" not in json.dumps(captured, ensure_ascii=False)
    assert "另一个项目" not in json.dumps(captured, ensure_ascii=False)
    opinions = [issue for issue in result["report"]["issues"] if issue.get("check_code") == "result_evidence"]
    assert len(opinions) == 1
    assert len(opinions[0]["citations"]) == 1
    assert opinions[0]["citations"][0]["source_ref"] == "第 2 页"
    assert opinions[0]["verification"] == "rule_result"
    assert opinions[0]["citations"][0]["support_status"] == "quote_only"
    assert len(result["report"]["warnings"]) == 2


@pytest.mark.parametrize("stop_after", ["load_snapshot", "retrieve", "rules", "verify", "save_report"])
def test_worker_restart_resumes_completed_steps_from_disk(project_store, stop_after):
    store, project = project_store
    first = submit(store, project)

    def crash(node):
        if node == stop_after:
            raise KeyboardInterrupt("simulate process exit")

    with pytest.raises(KeyboardInterrupt), ReviewWorker(store, after_node=crash) as worker:
        worker.run_once()
    reopened = Store(store.data_dir)
    with ReviewWorker(reopened) as worker:
        result = worker.run_once()
    assert result["id"] == first["id"]
    assert result["status"] == "completed"
    assert [event["node"] for event in result["events"]].count("rules") == 1
    assert [event["node"] for event in result["events"]].count("load_snapshot") == 1


def test_model_cannot_invent_an_unsourced_school_requirement(project_store):
    store, project = project_store
    submit(store, project, "model")
    response = {"content": json.dumps({"issues": [{"section_id": "method", "draft_quote": "我们已经完成实验",
        "problem": "学校要求必须达到某项门槛", "suggestion": "请核对学校当年通知", "kind": "school_requirement", "citations": []}]})}
    with ReviewWorker(store, model_call=lambda *_: response) as worker:
        result = worker.run_once()
    assert not any(issue["origin"] == "model" for issue in result["report"]["issues"])
    assert any("自行提出的学校硬性要求" in warning for warning in result["report"]["warnings"])


def test_unproved_factual_judgment_is_retained_for_human_review_outside_detected_problems(project_store):
    store, project = project_store
    quote = "研究方法拟在获得授权后开展实验。"
    store.update_project(project["id"], sections={"method": quote})
    submit(store, project, "model")
    problem = "证据显示已经得到1000条数据，草稿与证据不符。"
    response = {"content": json.dumps({"issues": [{"section_id": "method", "draft_quote": quote,
        "problem": problem, "suggestion": "应调整数据说明", "kind": "needs_evidence", "citations": []}]})}
    with ReviewWorker(store, model_call=lambda *_: response) as worker:
        report = worker.run_once()["report"]
    assert not any(i["problem"] == problem for i in report["issues"])
    assert report["unverified_hypotheses"][0]["problem"] == problem
    assert report["unverified_hypotheses"][0]["support_status"] == "insufficient"
    assert not any(item["reason"] == problem for item in report["follow_up"])


def test_inference_outcome_unknown_after_restart_is_not_replayed(project_store):
    store, project = project_store
    submitted = submit(store, project, "model")
    calls = []

    def crash(*_):
        calls.append(1)
        raise KeyboardInterrupt("simulate crash while inference is in flight")

    with pytest.raises(KeyboardInterrupt), ReviewWorker(store, model_call=crash) as worker:
        worker.run_once()
    with ReviewWorker(Store(store.data_dir), model_call=lambda *_: calls.append(2)) as worker:
        result = worker.run_once()
        assert worker.run_once() is None
    assert calls == [1]
    assert result["id"] == submitted["id"]
    assert result["status"] == "needs_review"
    assert result["report"] is None


def test_recorded_model_result_is_reused_if_checkpoint_not_yet_written(project_store):
    store, project = project_store
    run = submit(store, project, "model")
    runs = ReviewRuns(store)
    response = {"content": '{"issues": []}', "metrics": {"eval_count": 5}}
    assert runs.model_result(run["id"], lambda: response) == response
    calls = []
    with ReviewWorker(store, model_call=lambda *_: calls.append(1)) as worker:
        result = worker.run_once()
    assert calls == []
    assert result["status"] == "completed"
    assert result["report"]["metrics"]["eval_count"] == 5


def test_project_deletion_cascades_private_inputs_checkpoints_and_model_results(project_store):
    store, project = project_store
    submit(store, project, "model")
    with ReviewWorker(store, model_call=lambda *_: {"content": '{"issues": []}'}) as worker:
        result = worker.run_once()
    assert result["status"] == "completed"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM checkpoints").fetchone()[0] > 0
    store.delete_project(project["id"])
    with store.connect() as db:
        for table in ("review_runs", "review_model_calls", "review_events", "checkpoints", "writes"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_another_project_cannot_read_a_review(project_store):
    store, project = project_store
    first = submit(store, project)
    other = store.create_project()
    assert ReviewRuns(store).get(other["id"], first["id"]) is None


def test_only_one_worker_can_execute_a_local_directory(project_store):
    store, _project = project_store
    with ReviewWorker(store):
        with pytest.raises(RuntimeError, match="已有预审 worker"), ReviewWorker(store):
            pass


def test_invalid_model_json_preserves_rules_and_marks_semantic_incomplete(project_store):
    store, project = project_store
    submit(store, project, "model")
    with ReviewWorker(store, model_call=lambda *_: {"content": "并不是 JSON"}) as worker:
        result = worker.run_once()
    assert result["status"] == "completed"
    assert result["report"]["complete"] is False
    assert result["report"]["model_status"] == "invalid_response"
    assert result["report"]["issues"]


def test_context_overflow_skips_inference_without_silently_truncating_material(project_store):
    store, project = project_store
    store.update_project(project["id"], sections={"method": "合成研究材料" * 1100})
    submit(store, project, "model")
    # A call would fail at this unavailable loopback port; preflight must stop it.
    with ReviewWorker(store, model_call=LocalReviewModel("http://127.0.0.1:1")) as worker:
        result = worker.run_once()
    assert result["status"] == "completed"
    assert result["report"]["model_status"] == "input_limit"
    assert result["report"]["complete"] is False
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM review_model_calls").fetchone()[0] == 0


def test_separate_process_exit_and_new_cli_worker_resume_without_repeating_steps(project_store):
    store, project = project_store
    first = submit(store, project)
    root = Path(__file__).resolve().parents[1]
    script = """
import os, sys
sys.path.insert(0, 'server')
from proposal.review_worker import ReviewWorker
from proposal.storage import Store
def stop(node):
    if node == 'rules':
        os._exit(23)
with ReviewWorker(Store(sys.argv[1]), after_node=stop) as worker:
    worker.run_once()
"""
    env = {key: value for key, value in os.environ.items() if key not in {"SUPABASE_URL", "VERCEL"}}
    stopped = subprocess.run([sys.executable, "-c", script, str(store.data_dir)], cwd=root, env=env,
                             capture_output=True, timeout=20)
    assert stopped.returncode == 23, stopped.stderr.decode()
    resumed = subprocess.run([sys.executable, "server/review_worker.py", "--local-data", str(store.data_dir), "--once"],
                             cwd=root, env=env, capture_output=True, timeout=20)
    assert resumed.returncode == 0, resumed.stderr.decode()
    result = ReviewRuns(store).get(project["id"], first["id"])
    assert result["status"] == "completed"
    assert [event["node"] for event in result["events"]].count("rules") == 1
