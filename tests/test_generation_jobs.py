# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from contextlib import contextmanager

import pytest
from server.proposal.storage import Store
from server.proposal.jobs import GenerationJobRunner


def test_job_runner_lifecycle_and_completion(tmp_path):
    store = Store(tmp_path / "test.db")
    runner = GenerationJobRunner(store, max_workers=2)
    project = store.create_project(title="测试大创项目")

    calls = []
    def fake_invoke():
        calls.append(1)
        return {"content": "这是生成的章节正文", "review_card": {"score": 85.0}}

    job = runner.submit_job(
        project_id=project["id"],
        section_id="background",
        invoke_fn=fake_invoke,
        current_section_content="",
    )
    assert job["status"] in {"pending", "running"}

    # 等待完成
    for _ in range(50):
        time.sleep(0.05)
        st = runner.get_job_status(job["id"])
        if st and st["status"] == "completed":
            break

    final_job = runner.get_job_status(job["id"])
    assert final_job["status"] == "completed"
    assert final_job["step"] == "finished"
    assert final_job["result"]["content"] == "这是生成的章节正文"
    assert len(calls) == 1

    # 验证章节内容已被保存到项目中
    proj_after = store.get_project(project["id"])
    assert proj_after["sections"]["background"] == "这是生成的章节正文"

    # 验证版本历史记录已保存
    versions = store.list_section_versions(project["id"], "background")
    assert len(versions) == 1
    assert versions[0]["source"] == "ai_generated"


def test_job_runner_idempotency_key(tmp_path):
    store = Store(tmp_path / "test.db")
    runner = GenerationJobRunner(store, max_workers=2)
    project = store.create_project(title="测试大创项目")

    calls = []
    def fake_invoke():
        calls.append(1)
        time.sleep(0.1)
        return {"content": "幂等结果"}

    job1 = runner.submit_job(
        project_id=project["id"],
        section_id="method",
        invoke_fn=fake_invoke,
        idempotency_key="idemp_key_123",
    )
    # 重复使用相同 key 提交
    job2 = runner.submit_job(
        project_id=project["id"],
        section_id="method",
        invoke_fn=fake_invoke,
        idempotency_key="idemp_key_123",
    )

    assert job1["id"] == job2["id"]

    for _ in range(50):
        time.sleep(0.05)
        if runner.get_job_status(job1["id"])["status"] == "completed":
            break

    # 确保只执行了一次实际生成
    assert len(calls) == 1


def test_job_runner_active_job_mutex(tmp_path):
    store = Store(tmp_path / "test.db")
    runner = GenerationJobRunner(store, max_workers=2)
    project = store.create_project(title="测试大创项目")

    def slow_invoke():
        time.sleep(0.2)
        return {"content": "慢生成"}

    job1 = runner.submit_job(project["id"], "summary", slow_invoke)
    # 在 job1 运行期间再次提交同一章节
    job2 = runner.submit_job(project["id"], "summary", slow_invoke)

    assert job1["id"] == job2["id"]


def test_job_runner_version_conflict_protection(tmp_path):
    store = Store(tmp_path / "test.db")
    runner = GenerationJobRunner(store, max_workers=2)
    project = store.create_project(title="测试大创项目")

    initial_text = "用户初始写的两句话"
    store.update_project(project["id"], sections={"background": initial_text})

    def fake_gen():
        # 模拟模型生成较慢，用户在此期间手动编辑了输入框
        time.sleep(0.1)
        store.update_project(project["id"], sections={"background": "用户在生成期间修改保存的内容！"})
        return {"content": "AI 生成的大段内容"}

    job = runner.submit_job(
        project_id=project["id"],
        section_id="background",
        invoke_fn=fake_gen,
        current_section_content=initial_text,
    )

    for _ in range(50):
        time.sleep(0.05)
        if runner.get_job_status(job["id"])["status"] == "completed":
            break

    final_job = runner.get_job_status(job["id"])
    assert final_job["status"] == "completed"
    # 验证冲突保护生效
    assert final_job["result"]["conflict_status"] == "user_edit_preserved"

    # 验证项目正文没有被 AI 强制覆盖，保留了用户编辑
    proj_after = store.get_project(project["id"])
    assert proj_after["sections"]["background"] == "用户在生成期间修改保存的内容！"

    # 验证 AI 产物存入了备选版本
    versions = store.list_section_versions(project["id"], "background")
    assert any(v["source"] == "ai_generated_alternative" for v in versions)


def test_job_runner_cancellation(tmp_path):
    store = Store(tmp_path / "test.db")
    runner = GenerationJobRunner(store, max_workers=2)
    project = store.create_project(title="测试大创项目")

    def slow_task():
        time.sleep(0.5)
        return {"content": "本不该完成的内容"}

    job = runner.submit_job(project["id"], "cancel_sec", slow_task)
    success = runner.cancel_job(job["id"])
    assert success is True

    st = runner.get_job_status(job["id"])
    assert st["status"] == "cancelled"


@pytest.mark.parametrize("edited_section", ["background", "method"])
def test_edit_between_job_read_and_write_survives(tmp_path, monkeypatch, edited_section):
    store = Store(tmp_path / "race.db")
    project_id = store.create_project(title="写入竞态")["id"]
    runner = GenerationJobRunner(store)
    update = store.update_project
    generated = "生成结果"

    def update_with_student_edit(pid, **fields):
        if fields.get("sections", {}).get("background") == generated:
            update(pid, sections={edited_section: "学生刚保存的正文"})
        return update(pid, **fields)

    monkeypatch.setattr(store, "update_project", update_with_student_edit)
    job = runner.submit_job(project_id, "background", lambda: {"content": generated})
    runner.executor.shutdown(wait=True)
    completed = runner.get_job_status(job["id"])
    assert completed["status"] == "completed"
    assert store.get_project(project_id)["sections"] == {edited_section: "学生刚保存的正文"}
    assert completed["result"]["conflict_status"] == "user_edit_preserved"
    candidate, = store.list_section_versions(project_id, "background")
    assert candidate["source"] == "ai_generated_alternative" and candidate["content"] == generated


@pytest.mark.parametrize("changed", [True, False])
def test_history_failure_preserves_finished_job_text(tmp_path, monkeypatch, changed):
    store = Store(tmp_path / "history.db")
    project_id = store.create_project(title="版本保存中断")["id"]
    runner = GenerationJobRunner(store)
    store.update_project(project_id, sections={"s": "原文"})

    def invoke():
        if changed:
            store.update_project(project_id, sections={"s": "原文\n"})
        return {"content": "生成文本"}

    def unavailable(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(store, "save_section_version", unavailable)
    job = runner.submit_job(project_id, "s", invoke, current_section_content="原文")
    runner.executor.shutdown(wait=True)
    completed = runner.get_job_status(job["id"])
    assert completed["status"] == "completed"
    result = completed["result"]
    assert result["content"] == "生成文本"
    actual = store.get_project(project_id)
    if changed:
        assert actual["sections"]["s"] == "原文\n"
        assert result["conflict_status"] == "user_edit_preserved" and result["result_save_uncertain"]
    else:
        assert actual["sections"]["s"] == "生成文本"
        assert result["material_applied"] and result["material_revision"] == actual["material_revision"]


def test_save_acknowledges_its_own_revision_if_another_writer_commits_immediately(tmp_path, monkeypatch):
    store = Store(tmp_path / "ack.db")
    other = Store(store.data_dir)
    project = store.create_project(title="保存响应")
    connect = store.connect

    @contextmanager
    def racing_connect():
        with connect() as db:
            yield db
            wrote = bool(db.total_changes)
        if wrote:
            other.update_project(project["id"], sections={"s": "第二份正文"})

    monkeypatch.setattr(store, "connect", racing_connect)
    saved = store.update_project(project["id"], sections={"s": "第一份正文"})
    latest = other.get_project(project["id"])
    assert saved["sections"]["s"] == "第一份正文"
    assert latest["sections"]["s"] == "第二份正文"
    assert saved["material_revision"] + 1 == latest["material_revision"]
