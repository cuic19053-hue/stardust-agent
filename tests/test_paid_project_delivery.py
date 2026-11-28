"""A simulated provider receipt, with real upload, journal, wallet and Word routes."""
import hashlib
import io
import json
import os
from pathlib import Path

import pytest

from docx import Document
from fastapi.testclient import TestClient

import server.app as app
from proposal.agent_journal import AgentJournal
from proposal.retrieval import ProjectRetriever
from proposal.storage import Store
from proposal.tools import ProposalTools


def test_large_sources_paid_turn_restart_and_second_device_delivery(tmp_path, monkeypatch):
    exercise_delivery(tmp_path, monkeypatch)


@pytest.mark.skipif(not os.getenv("HAUT_DELIVERY_CASE"), reason="等待河工大正式空表和独立标注的原表栏位")
def test_haut_innovation_form_delivery(tmp_path, monkeypatch):
    # The template digest and destination are pinned by the reviewer BEFORE
    # rendering. Merely appending the saved chapter would pass a text search.
    case_file = Path(os.environ["HAUT_DELIVERY_CASE"])
    case = json.loads(case_file.read_text(encoding="utf-8"))
    template = case_file.parent / case["template"]
    assert hashlib.sha256(template.read_bytes()).hexdigest() == case["sha256"]
    output, fact = exercise_delivery(tmp_path, monkeypatch, template=template)
    original = Document(template)
    assert len(output.tables) == len(original.tables)
    for before, after in zip(original.tables, output.tables):
        assert len(before.rows) == len(after.rows)
        assert [col.width for col in before.columns] == [col.width for col in after.columns]
    target = case["research_field"]

    def field(doc):
        if "paragraph" in target:
            return doc.paragraphs[target["paragraph"]].text
        return doc.tables[target["table"]].cell(target["row"], target["column"]).text

    assert fact not in field(original)
    assert fact in field(output)


def exercise_delivery(tmp_path, monkeypatch, *, template=None):
    store = Store(tmp_path)
    pid = store.create_project("校园观测方案", school="河南工业大学")["id"]
    store.update_project(pid, state={"skill_id": "innovation-research"})
    store.grant_workbench_payment_credits("local_user", "synthetic-payment", 1_000_000)
    retriever = ProjectRetriever(store, embedder=object())
    monkeypatch.setattr(app, "store", store)
    monkeypatch.setattr(app, "retriever", retriever)
    monkeypatch.setattr(app, "tools", ProposalTools(store, retriever))
    monkeypatch.setattr(app, "CLOUD_MODE", False)
    monkeypatch.setattr(app, "_platform_bundle_ready", lambda: True)
    monkeypatch.setenv("WOPALLY_MODEL_BASE_URL", "https://provider.example.test/v1")
    monkeypatch.setenv("WOPALLY_WORKBENCH_API_KEY_ID", "123")
    monkeypatch.setenv("WOPALLY_WORKBENCH_API_KEY", "synthetic")
    monkeypatch.setattr("proposal.wopally_usage.WopallyUsage.find",
                        lambda self, request_id, *_: (10_000, "receipt-" + request_id))
    source_fact = "准备开展校园观测，耗材预算为1200元。"
    draft = ("<WritingBlock variant=\"document\">\n## 研究方案\n" + source_fact +
             "团队拟先核对记录表，再安排观测。数据将在观测后整理。\n</WritingBlock>")
    calls = []

    def provider(request, **kwargs):
        messages = json.loads(request.data)["messages"]
        calls.append(1)
        if len(calls) == 1:
            message = {"tool_calls": [{"id": "source-1", "type": "function", "function": {
                "name": "search_project_materials", "arguments": json.dumps({"query": "校园观测 耗材预算"}),
            }}]}
        else:
            assert len(calls) == 2 and any(m["role"] == "tool" and source_fact in m["content"] for m in messages)
            message = {"content": draft}
        return io.BytesIO(json.dumps({"choices": [{"message": message}]}).encode())

    monkeypatch.setattr("proposal.agent.wopally_urlopen", provider)
    url = f"/api/projects/{pid}"
    payload = {"message": "根据我已确认的材料撰写研究方案。", "idempotency_key": "delivery-acceptance",
               "llm_config": {"provider": "preset"}}
    with TestClient(app.app) as first:
        if template is not None:
            uploaded = first.post(url + "/documents?kind=template", files={
                "files": (template.name, template.read_bytes(),
                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
            assert uploaded.status_code == 200, uploaded.text
        for i in range(4):
            raw = (source_fact + "\n" + ("这是待核对的现场记录。"*9000)[:90_000]).encode()
            upload = first.post(url + "/documents", files={"files": (f"记录{i}.txt", raw, "text/plain")})
            assert upload.status_code == 200, upload.text
            doc = upload.json()["documents"][0]["document"]
            review = first.patch(f"/api/documents/{doc['id']}/confirm", json={
                "confirmed": True, "expected_revision": doc["review_revision"]})
            assert review.status_code == 200 and review.json()["indexed_chunks"] > 0
        # Lose the worker after both paid responses are journaled, before the
        # final message is committed or the client receives a success response.
        def lose_worker(*_):
            raise RuntimeError("worker disconnected")
        with monkeypatch.context() as patch:
            patch.setattr(AgentJournal, "complete", lose_worker)
            response = first.post(url + "/chat", json=payload)
        assert response.status_code == 503, response.text
        assert calls == [1, 1]

    restarted = Store(tmp_path)
    monkeypatch.setattr(app, "store", restarted)
    monkeypatch.setattr(app, "tools", ProposalTools(restarted, ProjectRetriever(restarted, embedder=object())))
    with restarted.connect() as db:
        db.execute("UPDATE agent_turns SET lease_until=0")
        snapshot = db.execute("SELECT snapshot FROM agent_turns").fetchone()[0]
        assert len(snapshot.encode()) < 64_000 and source_fact not in snapshot
    with TestClient(app.app) as second:
        run, = second.get(url + "/chat-runs").json()["runs"]
        assert run["status"] == "interrupted"
        second_device = {k: v for k, v in payload.items() if k != "idempotency_key"}
        second_device["resume_turn_id"] = run["id"]
        assert second.post(url + "/chat", json={**second_device, "message": "另一条指令"}).status_code == 409
        restored = second.post(url + "/chat", json=second_device)
        assert restored.status_code == 200, restored.text
        assert second.post(url + "/chat", json=payload).json()["replayed"]
        # A fresh device can read the saved answer with just the project.
        run, = second.get(url + "/chat-runs").json()["runs"]
        detail = second.get(url + "/chat-runs/" + run["id"]).json()
        assert detail["status"] == "completed" and source_fact in detail["answer"]
        other = restarted.create_project("其他项目")["id"]
        assert second.get(f"/api/projects/{other}/chat-runs/{run['id']}").status_code == 404
        preview = second.get(url + "/chat-drafts").json()
        item = next(i for i in preview["items"] if source_fact in i["content"])
        budget, = item["fact_checks"]
        assert budget["status"] == "matched_original" and budget["evidence"]["quote"] == source_fact
        assert budget["evidence"]["source_ref"] == "行 1" and budget["evidence"]["review_revision"] > 0
        assert second.post(url + "/chat-drafts/confirm", json={"snapshot": preview["snapshot"],
                           "items": [{"id": item["id"]}]}).status_code == 200
        exported = second.post(url + "/export")
        assert exported.status_code == 200, exported.text
        if template is not None:
            assert exported.json()["used_original_template"] is True
        word = second.get(exported.json()["download_url"])
        assert word.status_code == 200
        docx = Document(io.BytesIO(word.content))
        text = "\n".join([*(p.text for p in docx.paragraphs),
                          *(cell.text for table in docx.tables for row in table.rows for cell in row.cells)])
        assert source_fact in text and "已完成观测" not in text
        assert calls == [1, 1]
        wallet = restarted.get_workbench_credit_balance("local_user")
        assert wallet["total_spent_micro_cny"] == 20_000 and wallet["balance_micro_cny"] == 980_000
        assert restarted.pending_workbench_usage("local_user") == []
    return docx, source_fact
