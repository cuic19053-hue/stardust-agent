import hashlib

import pytest
from fastapi.testclient import TestClient

import server.app as app
from proposal.agent import OllamaAgent
from proposal.claim_audit import audit_claims, eligible_documents, scoped_official_sources
from proposal.retrieval import ProjectRetriever
from proposal.storage import Store
from proposal.tools import ProposalTools
from proposal.workflow import AgenticWorkflowEngine


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    store = Store(tmp_path)
    p = store.create_project("观测", school="河南工业大学", level="校级")
    project = store.update_project(p["id"], state={"skill_id": "innovation-research", "outline_confirmed": True,
        "confirmed_facts": {"application_year": 2024}}, outline=[{"id": "foundation", "title": "观测基础"}],
        sections={"foundation": "学生原稿"})
    retriever = ProjectRetriever(store, embedder=object())
    tools = ProposalTools(store, retriever)
    monkeypatch.setattr(app, "store", store)
    monkeypatch.setattr(app, "tools", tools)
    monkeypatch.setattr(app, "CLOUD_MODE", False)
    return store, tools, project


def source(store, project, text, name="记录.txt"):
    doc = store.add_document(project["id"], name, "", "source", "text/plain")
    return store.apply_document_review(doc["id"], 0, text, "ready", True, [])


def test_generation_and_saved_export_flag_a_valid_marker_on_an_unsupported_claim(workspace, monkeypatch):
    store, tools, project = workspace
    doc = source(store, project, "[行 1] 准备开展观测。\n[行 2] 耗材预算为1200元。")
    sid = "foundation"
    body = "已完成观测。[来源：记录.txt，行 1]耗材预算为1200元[来源：记录.txt，行 2]。"
    class Draft:
        content = body
        def model_dump(self):
            return {"content": self.content, "status": "passed"}
    monkeypatch.setattr(AgenticWorkflowEngine, "run_workflow", lambda *a, **k: Draft())
    monkeypatch.setattr(tools, "search_project_materials", lambda *a, **k: {"results": [
        {"document_id": doc["id"], "source_name": "记录.txt", "source_ref": f"行 {n}",
         "text": text, "chunk_id": str(n)} for n, text in enumerate(doc["extracted_text"].splitlines(), 1)]})
    result = OllamaAgent(store, tools).generate_section(project["id"], sid)
    assert result["review_incomplete"] and result["alternative_version_id"]
    assert store.get_project(project["id"])["sections"][sid] == "学生原稿"
    wrong, correct = result["citation_check"]["claims"]
    assert wrong["status"] == "needs_review" and wrong["cited_passage"]["quote"] == "准备开展观测。"
    assert correct["status"] == "matched_original" and correct["evidence"]["source_ref"] == "行 2"
    assert correct["evidence"]["source_sha256"] == hashlib.sha256(doc["extracted_text"].encode()).hexdigest()
    assert correct["evidence"]["review_revision"] == 1
    pid = project["id"]
    store.add_message(pid, "assistant", f'<WritingBlock variant="document">## 观测基础\n{body}</WritingBlock>')
    with TestClient(app.app) as client:
        preview = client.get(f"/api/projects/{pid}/chat-drafts").json()
        item, = preview["items"]
        assert item["requires_review"] and "已完成观测" in "".join(item["warnings"])
        assert item["fact_checks"][0]["cited_passage"]["document_id"] == doc["id"]
        response = client.post(f"/api/projects/{pid}/chat-drafts/confirm", json={"snapshot": preview["snapshot"],
            "items": [{"id": item["id"], "replace_existing": True}]})
        assert response.status_code == 200
        exported = client.post(f"/api/projects/{pid}/export", json={"request_id": "a"*32}).json()
        assert not exported["validation"]["passed"] and any("已完成观测" in w for w in exported["warnings"])
        receipt = client.get(f"/api/projects/{pid}/export/status/" + "a"*32).json()
        assert receipt["warnings"] == exported["warnings"]
        from proposal.material_export import export_material
        monkeypatch.setenv("MATERIAL_PIPELINE_ENABLED", "true")
        worker = export_material(store, pid, store.files_dir / pid / "worker-export")
        assert any("已完成观测" in w for w in worker["warnings"])


def test_source_boundaries_wrong_version_or_wrong_document_do_not_support(workspace):
    store, _, project = workspace
    d = source(store, project, "[第 1 页]\n假设已完成观测。\n[第 2 页]\n已完成观测。")
    def status(claim, docs):
        return audit_claims(project, {"s": claim}, eligible_documents(project, docs, []))["claims"][0]["status"]
    assert status("已完成观测[来源：记录.txt，第 1 页]。", [d]) == "needs_review"
    assert status("已完成观测[来源：记录.txt，第 2 页]。", [d]) == "matched_original"
    assert status("已完成观测[来源：记录.txt，第 2 页]。", [d, {**d, "id": "duplicate"}]) == "needs_review"
    assert status("已完成观测", [{**d, "project_id": "another"}]) == "needs_review"
    assert status("已完成观测", [{**d, "confirmation_origin": "automatic"}]) == "needs_review"
    assert eligible_documents(project, [d], [{"role": "user", "content": "不要使用上传的材料"}]) == []
    updated = store.apply_document_review(d["id"], 1, "准备开展观测。", "ready", True, [])
    assert status("已完成观测", [updated]) == "needs_review"
    for claim in ["差旅费为2万元。", "耗材预算为200元。"]:
        assert status(claim, [{**d, "extracted_text": "耗材预算为2万元。"}]) == "needs_review"


def test_official_clauses_are_versioned_and_scoped(workspace):
    _, _, project = workspace
    notice, = scoped_official_sources(project)
    def review(p, wording, **kwargs):
        return audit_claims(p, {"s": wording}, [], **kwargs)["claims"][0]
    for c in notice["chunks"]:
        row = review(project, c["content"] + f'[来源：{notice["title"]}，{c["source_ref"]}]')
        assert row["status"] == "matched_original" and row["evidence"]["quote"] == c["original_quote"]
        assert row["evidence"]["source_url"] == notice["source_url"] and row["evidence"]["application_year"] == 2024
        for wrong in [{"school": "另一所大学"}, {"level": "省级"}, {"state": {"confirmed_facts": {"application_year": 2026}}},
                      {"state": {**project["state"], "skill_id": "entrepreneurship-practice"}}]:
            assert review({**project, **wrong}, c["content"])["status"] == "needs_review"
        assert review(project, c["content"], public_hits=[])["status"] == "needs_review"
    assert review(project, notice["chunks"][1]["content"].replace("指导教师", "学生"))["status"] == "needs_review"
    assert review(project, "我符合申报条件。")["status"] == "needs_review"
    audit = audit_claims(project, {"s": "总预算为3000元。" * 81}, [])
    assert audit["truncated"] and len(audit["claims"]) == 80


def test_retained_version_detects_even_an_edit_that_leaves_the_quoted_line(workspace):
    store, _, project = workspace
    doc = source(store, project, "[行 1] 已完成观测。")
    store.add_message(project["id"], "assistant", '<WritingBlock variant="document">## 观测基础\n已完成观测。</WritingBlock>')
    with TestClient(app.app) as client:
        base = "/api/projects/" + project["id"]
        preview = client.get(base + "/chat-drafts").json()
        item, = preview["items"]
        saved = client.post(base + "/chat-drafts/confirm", json={"snapshot": preview["snapshot"],
            "items": [{"id": item["id"], "replace_existing": True, "content": "耗材预算为1200元。已完成观测。"}]})
        checked = saved.json()["claim_audit"]["claims"]
        assert [r["status"] for r in checked] == ["needs_review", "matched_original"]
        store.apply_document_review(doc["id"], 1, "[行 1] 已完成观测。\n[行 2] 记录已补正。", "ready", True, [])
        result = client.post(base + "/validate").json()["claim_audit"]
        assert result["claims"][1]["status"] == "needs_review"
        assert "版本" in result["claims"][1]["warning"]


def test_validation_cannot_restore_a_revoked_public_clause(workspace, monkeypatch):
    _, _, project = workspace
    notice, = scoped_official_sources(project)
    class Revoked:
        def search_public_knowledge(self, *args):
            return []
    monkeypatch.setenv("RAG_PUBLIC_DB_ENABLED", "true")
    row, = audit_claims(project, {"s": notice["chunks"][0]["content"]}, [], store=Revoked())["claims"]
    assert row["status"] == "needs_review"


def test_budget_and_progress_in_one_sentence_checked_independently(workspace):
    store, _, project = workspace
    doc = source(store, project, "[行 1] 准备开展观测。\n[行 2] 耗材预算为1200元。")
    checks = audit_claims(project, {"s": "已完成观测，耗材预算为1200元。"}, [doc])
    assert [(c["claim"], c["status"]) for c in checks["claims"]] == [
        ("已完成观测", "needs_review"), ("耗材预算为1200元", "matched_original")]


@pytest.mark.parametrize("claim,pending", [("已完成观测", True), ("耗材预算为1200元", False)])
def test_chat_rechecks_the_passage_actually_returned_by_the_tool(workspace, monkeypatch, claim, pending):
    store, tools, project = workspace
    doc = source(store, project, "[行 1] 准备开展观测。耗材预算为1200元。")
    tools.retriever.index_document(doc["id"], doc["extracted_text"])
    agent = OllamaAgent(store, tools)
    reply = f'<WritingBlock variant="document">\n## 观测基础\n{claim}[来源：记录.txt，行 1]。\n</WritingBlock>'
    responses = iter([{"tool_calls": [{"function": {"name": "search_project_materials",
                                                    "arguments": {"query": "开展观测 耗材预算"}}}]}, {"content": reply}])
    monkeypatch.setattr(agent, "_policy_evidence_reply", lambda *_: None)
    monkeypatch.setattr(agent, "_post", lambda *a, **kw: {"message": next(responses)})
    result = agent.chat(project["id"], "请根据确认的材料直接写创新训练申报书的观测基础正文。")
    assert "search_project_materials" in result["tool_calls"]
    assert ("已完成成果待核对" in result["message"]) == pending
    assert reply in result["message"]


def test_worker_hydrates_the_confirmed_source_revision(workspace, tmp_path):
    from server.material_worker import hydrate

    store, _, project = workspace
    doc = source(store, project, "[行 1] 准备开展观测。")
    doc = store.apply_document_review(doc["id"], 1, "[行 1] 已完成观测。", "ready", True, [])
    project = store.update_project(project["id"], sections={"foundation": "已完成观测。"})
    job = {"project_id": project["id"], "payload": {"project": project, "documents": [doc]}}
    replica, _ = hydrate(job, tmp_path / "worker")
    claim, = ProposalTools(replica).validate_project(project["id"])["claim_audit"]["claims"]
    assert claim["evidence"]["review_revision"] == doc["review_revision"] == 2
