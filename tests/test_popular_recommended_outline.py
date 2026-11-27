"""Student-facing starting outlines stay scoped to the selected track."""
import pytest
from fastapi.testclient import TestClient

import server.app as app
from proposal.agent import ModelUnavailable
from proposal.outline_fallback import recommended_outline
from proposal.storage import Store


@pytest.mark.parametrize(("skill_id", "expected", "unrelated"), [
    ("innovation-research", "研究方法与技术路线", "商业模式与运营计划"),
    ("internet-plus", "商业模式与运营计划", "调查结果与分析"),
    ("internet-plus-red-tour", "社会价值与成效", "调查结果与分析"),
    ("social-survey", "调研内容与方法", "商业模式与运营计划"),
    ("graduate-recommendation", "申请动机与专业匹配", "经费预算"),
])
def test_popular_track_outline_is_relevant_and_does_not_guess_results(skill_id, expected, unrelated):
    outline, notice = recommended_outline({"state": {"skill_id": skill_id}})
    titles = [item["title"] for item in outline]
    assert expected in titles and unrelated not in titles
    assert "学校" in notice and len({item["id"] for item in outline}) == len(outline)


def test_recommendation_is_a_read_only_preview_and_never_calls_a_model(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(app, "store", store)
    monkeypatch.setattr(app, "CLOUD_MODE", False)
    monkeypatch.setattr(app, "_metered_invoke", lambda *_a, **_k: pytest.fail("No model call for a recommendation"))
    project_id = store.create_project()["id"]
    store.update_project(project_id, state={"skill_id": "challenge-cup", "initial_request": "挑战杯社会调查"},
                         outline=[{"id": "previous", "title": "原目录"}], sections={"previous": "学生原文"})
    before = store.get_project(project_id)
    with TestClient(app.app) as client:
        response = client.get(f"/api/projects/{project_id}/outline/recommend")
        assert response.status_code == 200
        assert "调查设计与研究方法" in [item["title"] for item in response.json()["outline"]]
        assert "学校" in response.json()["warning"]
        assert client.get(f"/api/projects/{'f' * 32}/outline/recommend").status_code == 404
    after = store.get_project(project_id)
    assert after["outline"] == before["outline"] and after["sections"] == before["sections"]
    assert after["material_revision"] == before["material_revision"]


def test_model_failure_uses_challenge_outline_instead_of_innovation_template(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(app, "store", store)
    monkeypatch.setattr(app, "CLOUD_MODE", False)
    def unavailable(*_args, **_kwargs):
        raise ModelUnavailable("test offline")
    monkeypatch.setattr(app, "_metered_invoke", unavailable)
    project_id = store.create_project()["id"]
    store.update_project(project_id, state={"skill_id": "challenge-cup", "initial_request": "挑战杯科技发明制作"})
    with TestClient(app.app) as client:
        response = client.post(f"/api/projects/{project_id}/outline/generate")
    assert response.status_code == 200
    assert "设计方案与制作过程" in [item["title"] for item in response.json()["outline"]]
    assert "模型暂不可用" in response.json()["warning"]


def test_saving_stale_outline_does_not_overwrite_other_page_changes(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(app, "store", store)
    monkeypatch.setattr(app, "CLOUD_MODE", False)
    pid = store.create_project()["id"]
    saved = store.update_project(pid, state={"skill_id": "social-survey"},
                                 outline=[{"id": "draft", "title": "调研背景"}], sections={"draft": "原正文"})
    stale_revision = saved["material_revision"]
    store.update_project(pid, sections={"draft": "其他页面刚保存的正文"})
    with TestClient(app.app) as client:
        response = client.put(f"/api/projects/{pid}/outline", json={
            "outline": [{"id": "new", "title": "调研方法"}], "expected_revision": stale_revision,
        })
    assert response.status_code == 409
    project = store.get_project(pid)
    assert project["outline"] == [{"id": "draft", "title": "调研背景"}]
    assert project["sections"]["draft"] == "其他页面刚保存的正文"


def test_generated_candidate_preserves_confirmed_outline_and_a_concurrent_fact(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(app, "store", store)
    monkeypatch.setattr(app, "CLOUD_MODE", False)
    pid = store.create_project()["id"]
    saved = store.update_project(pid, state={"outline_confirmed": True, "skill_id": "social-survey"},
                                 outline=[{"id": "draft", "title": "已确认目录"}], sections={"draft": "学生原稿"})

    def model_result(*_args, **_kwargs):
        store.update_project(pid, state={**saved["state"], "confirmed_facts": {"method": "学生并发确认的事实"}})
        return [{"id": "draft", "title": "候选目录"}]

    monkeypatch.setattr(app, "_metered_invoke", model_result)
    with TestClient(app.app) as client:
        response = client.post(f"/api/projects/{pid}/outline/generate")
    assert response.status_code == 200
    assert response.json()["outline"][0]["title"] == "候选目录"
    current = store.get_project(pid)
    assert current["outline"] == saved["outline"] and current["sections"] == saved["sections"]
    assert current["state"]["outline_confirmed"] is True
    assert current["state"]["confirmed_facts"] == {"method": "学生并发确认的事实"}
    assert current["material_revision"] == saved["material_revision"] + 1


def test_outline_precondition_catches_directory_edits_but_accepts_unrelated_changes(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(app, "store", store)
    monkeypatch.setattr(app, "CLOUD_MODE", False)
    pid = store.create_project()["id"]
    base = [{"id": "draft", "title": "初始目录"}]
    local = [{"id": "draft", "title": "本页目录"}]
    remote = [{"id": "draft", "title": "另一页已保存的目录"}]
    store.update_project(pid, outline=base, sections={"draft": "正文"})
    store.update_project(pid, sections={"draft": "另一页只修改了正文"})
    with TestClient(app.app) as client:
        url = f"/api/projects/{pid}/outline"
        assert client.put(url, json={"outline": local, "base_outline": base}).status_code == 200
        assert store.get_project(pid)["sections"]["draft"] == "另一页只修改了正文"
        store.update_project(pid, outline=remote)
        response = client.put(url, json={"outline": local, "base_outline": local})
    assert response.status_code == 409
    assert store.get_project(pid)["outline"] == remote


def test_retired_chapter_can_be_restored_and_stays_out_of_both_word_renderers(tmp_path, monkeypatch):
    import io
    from docx import Document
    from proposal.material_export import export_material
    from proposal.tools import ProposalTools

    monkeypatch.setenv("MATERIAL_PIPELINE_ENABLED", "true")
    store = Store(tmp_path)
    monkeypatch.setattr(app, "store", store)
    monkeypatch.setattr(app, "tools", ProposalTools(store))
    monkeypatch.setattr(app, "CLOUD_MODE", False)
    pid = store.create_project(title="目录恢复测试")["id"]
    first = {"id": "first", "title": "保留的章节"}
    retired = {"id": "retired", "title": "移出的章节"}
    note = "这份正文仅在恢复后可以导出。"
    template = Document()
    template.add_paragraph("{{section:retired}}")
    path = tmp_path / "school-template.docx"
    template.save(path)
    doc = store.add_document(pid, path.name, str(path), "template",
                             "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    store.update_project(pid, outline=[first, retired], sections={"first": "可提交的正文。", "retired": note},
                         state={"outline_confirmed": True}, template_document_id=doc["id"])
    root = f"/api/projects/{pid}"

    def document_text(doc):
        return "\n".join([*(p.text for p in doc.paragraphs),
                           *(cell.text for table in doc.tables for row in table.rows for cell in row.cells)])

    with TestClient(app.app) as client:
        saved = client.put(root + "/outline", json={"outline": [first], "base_outline": [first, retired]})
        saved.raise_for_status()
        assert saved.json()["archived_outline"] == [retired]
        assert client.get(root).json()["sections"]["retired"] == note
        client.post(root + "/export").raise_for_status()
        assert note not in document_text(Document(io.BytesIO(client.get(root + "/download").content)))
        worker_dir = tmp_path / "worker"
        export_material(store, pid, worker_dir)
        assert note not in document_text(Document(worker_dir / "proposal.docx"))

        restored = client.put(root + "/outline", json={"outline": [first, retired], "base_outline": [first]})
        restored.raise_for_status()
        assert restored.json()["archived_outline"] == []
        client.post(root + "/export").raise_for_status()
        assert note in document_text(Document(io.BytesIO(client.get(root + "/download").content)))
