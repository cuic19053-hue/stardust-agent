"""Regression cases shared by the project MCP, HTTP edits, and version restore."""
import pytest
from fastapi.testclient import TestClient

import server.app as app
from proposal.agent import OllamaAgent
from proposal.storage import Store
from proposal.tools import ProposalTools


@pytest.fixture
def chapter(tmp_path, monkeypatch):
    store = Store(tmp_path)
    pid = store.create_project(title="章节保存验收")["id"]
    project = store.update_project(pid, outline=[{"id": "project_info", "title": "项目概况"}],
                                 state={"skill_id": "innovation-research", "outline_confirmed": True},
                                 sections={"project_info": "学生原文", "method": "旧方法"})
    tools = ProposalTools(store)
    monkeypatch.setattr(app, "store", store)
    monkeypatch.setattr(app, "tools", tools)
    monkeypatch.setattr(app, "CLOUD_MODE", False)
    return store, tools, project


def test_tool_accepts_boundary_but_never_truncates(chapter):
    store, tools, p = chapter
    content = "甲🚀" * 15_000
    saved = tools.save_section(p["id"], "project_info", content)
    assert saved["character_count"] == 30_000
    too_long = tools.save_section(p["id"], "project_info", content + "尾")
    assert "error" in too_long
    actual = store.get_project(p["id"])
    assert actual["sections"]["project_info"] == content
    assert actual["material_revision"] == saved["material_revision"]


@pytest.mark.parametrize("channel", ["tool", "http"])
def test_interleaved_sibling_edit_survives_even_without_client_revision(chapter, monkeypatch, channel):
    store, tools, p = chapter
    update = store.update_project
    def intervening(pid, **fields):
        update(pid, sections={**p["sections"], "method": "刚保存的新方法"})
        return update(pid, **fields)
    monkeypatch.setattr(store, "update_project", intervening)
    if channel == "tool":
        assert "error" in tools.save_section(p["id"], "project_info", "旧页面提交")
    else:
        with TestClient(app.app) as client:
            response = client.put(f"/api/projects/{p['id']}/sections/project_info", json={"content": "旧页面提交"})
        assert response.status_code == 409
    assert store.get_project(p["id"])["sections"] == {"method": "刚保存的新方法", "project_info": "学生原文"}


@pytest.mark.parametrize("guard", [{"base_content": ""}, {"expected_revision": 0}])
def test_tool_can_save_against_the_version_it_read(chapter, guard):
    store, tools, p = chapter
    result = tools.save_section(p["id"], "project_info", "过期草稿", **guard)
    assert "error" in result
    assert store.get_project(p["id"])["sections"] == p["sections"]


def test_restore_is_reversible_and_a_repeated_request_is_a_noop(chapter):
    store, _, p = chapter
    pid = p["id"]
    version = store.save_section_version(pid, "project_info", "指定历史稿")
    url = f"/api/projects/{pid}/sections/project_info/versions/{version['id']}/restore"
    with TestClient(app.app) as client:
        assert client.post(url).status_code == 200
        revision = store.get_project(pid)["material_revision"]
        history = store.list_section_versions(pid, "project_info")
        assert any(v["content"] == "学生原文" for v in history)
        assert client.post(url).status_code == 200
        assert store.get_project(pid)["material_revision"] == revision
        assert len(store.list_section_versions(pid, "project_info")) == len(history)


def test_restore_rejects_an_outdated_client_revision(chapter):
    store, _, p = chapter
    pid = p["id"]
    version = store.save_section_version(pid, "project_info", "指定历史稿")
    store.update_project(pid, sections={**p["sections"], "project_info": "新的学生正文"})
    with TestClient(app.app) as client:
        response = client.post(f"/api/projects/{pid}/sections/project_info/versions/{version['id']}/restore",
                               json={"expected_revision": p["material_revision"]})
    assert response.status_code == 409
    assert store.get_project(pid)["sections"]["project_info"] == "新的学生正文"


def test_oversized_historical_chapter_can_be_restored_and_shortened(chapter):
    store, _, p = chapter
    pid = p["id"]
    content = "历史稿" * 10_001
    version = store.save_section_version(pid, "project_info", content)
    url = f"/api/projects/{pid}/sections/project_info"
    with TestClient(app.app) as client:
        assert client.post(f"{url}/versions/{version['id']}/restore").status_code == 200
        actual = store.get_project(pid)
        assert actual["sections"]["project_info"] == content
        response = client.put(url, json={"content": "修订完成", "base_content": content,
                                         "expected_revision": actual["material_revision"]})
        assert response.status_code == 200, response.text


def test_direct_generator_keeps_an_edit_made_after_reading_the_project(chapter, monkeypatch):
    store, tools, p = chapter
    pid = p["id"]
    def search(*_args, **_kwargs):
        store.update_project(pid, sections={**p["sections"], "project_info": "刚刚保存的手稿"})
        return {"results": []}
    monkeypatch.setattr(tools, "search_project_materials", search)
    result = OllamaAgent(store, tools).generate_section(pid, "project_info")
    assert store.get_project(pid)["sections"]["project_info"] == "刚刚保存的手稿"
    assert result["conflict_status"] == "user_edit_preserved"
    assert store.get_section_version(result["alternative_version_id"])["content"] == result["content"]


@pytest.mark.parametrize("failed_source", ["before_restore", "restore"])
def test_restore_distinguishes_backup_failure_from_a_lost_history_receipt(chapter, monkeypatch, failed_source):
    store, _, p = chapter
    pid = p["id"]
    version = store.save_section_version(pid, "project_info", "目标稿")
    save_version = store.save_section_version

    def fail_selected(*args, source, **kwargs):
        if source == failed_source:
            raise OSError("unavailable")
        return save_version(*args, source=source, **kwargs)

    monkeypatch.setattr(store, "save_section_version", fail_selected)
    with TestClient(app.app) as client:
        response = client.post(f"/api/projects/{pid}/sections/project_info/versions/{version['id']}/restore")
    if failed_source == "before_restore":
        assert response.status_code == 503
        assert store.get_project(pid)["sections"]["project_info"] == "学生原文"
    else:
        assert response.status_code == 200 and "已恢复" in response.json()["notice"]
        assert store.get_project(pid)["sections"]["project_info"] == "目标稿"
        assert any(v["content"] == "学生原文" for v in store.list_section_versions(pid, "project_info"))


def test_generation_acknowledgement_lost_after_commit_preserves_the_model_result(chapter, monkeypatch):
    store, _, p = chapter
    update = store.update_project
    writes = []

    def commit_and_fail(pid, **fields):
        writes.append(pid)
        update(pid, **fields)
        raise ConnectionError("lost response")

    monkeypatch.setattr(store, "update_project", commit_and_fail)
    monkeypatch.setattr(app, "_metered_invoke", lambda *_: {"content": "完整模型结果"})
    with TestClient(app.app) as client:
        response = client.post(f"/api/projects/{p['id']}/sections/project_info/generate")
    assert response.status_code == 200
    result = response.json()
    assert result["result_save_uncertain"] and result["content"] == "完整模型结果"
    assert not result.get("material_applied")
    assert writes == [p["id"]] and store.get_project(p["id"])["sections"]["project_info"] == result["content"]


def test_local_version_history_orders_timestamps_that_share_a_second(chapter, monkeypatch):
    from proposal import storage
    monkeypatch.setattr(storage, "utc_now", lambda: "2026-11-27T00:00:00+00:00")
    store, _, p = chapter
    for text in ("旧稿", "恢复前的手稿", "最新正文"):
        store.save_section_version(p["id"], "project_info", text)
    versions = store.list_section_versions(p["id"], "project_info", limit=2)
    assert [v["content"] for v in versions] == ["最新正文", "恢复前的手稿"]


def test_http_length_error_names_the_limit_without_echoing_the_draft(chapter):
    store, _, p = chapter
    with TestClient(app.app) as client:
        response = client.put(f"/api/projects/{p['id']}/sections/project_info", json={"content": "私" * 30_001})
    assert response.status_code == 422
    assert "30,000" in response.json()["detail"] and "私" not in response.text
    assert store.get_project(p["id"])["sections"] == p["sections"]


def test_blank_generated_text_cannot_clear_a_chapter(chapter, monkeypatch):
    store, _, p = chapter
    monkeypatch.setattr(app, "_metered_invoke", lambda *_: {"content": "  \n"})
    with TestClient(app.app) as client:
        response = client.post(f"/api/projects/{p['id']}/sections/project_info/generate")
    assert response.status_code == 400
    assert store.get_project(p["id"])["sections"] == p["sections"]


def test_explicitly_incomplete_review_keeps_the_generated_text_as_a_candidate(chapter, monkeypatch):
    store, _, p = chapter
    candidate = "评审尚未完成的初稿"
    monkeypatch.setattr(app, "_metered_invoke", lambda *_: {"content": candidate, "review_incomplete": True})
    with TestClient(app.app) as client:
        response = client.post(f"/api/projects/{p['id']}/sections/project_info/generate")
    assert response.status_code == 200
    result = response.json()
    assert result["conflict_status"] == "user_edit_preserved"
    assert store.get_project(p["id"])["sections"] == p["sections"]
    assert store.get_section_version(str(result["alternative_version_id"]))["content"] == candidate


def test_restore_cannot_overwrite_a_save_that_lands_after_its_backup(chapter, monkeypatch):
    store, _, p = chapter
    pid = p["id"]
    version = store.save_section_version(pid, "project_info", "目标稿")
    save_version = store.save_section_version

    def backup_then_interleave(*args, source, **kwargs):
        backup = save_version(*args, source=source, **kwargs)
        if source == "before_restore":
            store.update_project(pid, sections={**p["sections"], "project_info": "另一页面的新正文"})
        return backup

    monkeypatch.setattr(store, "save_section_version", backup_then_interleave)
    with TestClient(app.app) as client:
        response = client.post(f"/api/projects/{pid}/sections/project_info/versions/{version['id']}/restore",
                               json={"expected_revision": p["material_revision"]})
    assert response.status_code == 409
    assert store.get_project(pid)["sections"]["project_info"] == "另一页面的新正文"
