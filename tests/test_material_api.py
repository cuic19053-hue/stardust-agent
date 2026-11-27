import base64
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from proposal.storage import Store
from proposal.tools import ProposalTools
from proposal.agent import OllamaAgent
from proposal.material_jobs import MaterialJobs


@pytest.fixture
def api(monkeypatch, tmp_path):
    import app
    monkeypatch.setenv("MATERIAL_PIPELINE_ENABLED", "true")
    monkeypatch.setenv("MATERIAL_WORKER_READY", "true")
    monkeypatch.setattr(app, "CLOUD_MODE", False)
    store = Store(tmp_path)
    tools = ProposalTools(store)
    monkeypatch.setattr(app, "store", store)
    monkeypatch.setattr(app, "tools", tools)
    monkeypatch.setattr(app, "agent", OllamaAgent(store,tools))
    p = store.create_project(title="大创试点")
    p = store.update_project(p["id"], state={"skill_id":"innovation-research","outline_confirmed":True},
        outline=[{"id":"method","title":"研究方法"}], sections={"method":"原文"})
    with TestClient(app.app) as client:
        yield client,store,p


def test_documented_worker_command_starts_without_a_pythonpath(tmp_path):
    worker = Path(__file__).resolve().parents[1] / "server" / "material_worker.py"
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    process = subprocess.run([sys.executable, str(worker), "--help"], env=env, cwd=tmp_path,
                             capture_output=True, text=True, timeout=15)
    assert process.returncode == 0, process.stderr
    assert "--local-data" in process.stdout


def test_material_api_supports_catalogue_types_and_rejects_unknown(api):
    client,store,p = api
    assert client.get(f"/api/projects/{p['id']}/material").status_code == 200
    store.update_project(p["id"],state={"skill_id":"national-scholarship"})
    assert client.get(f"/api/projects/{p['id']}/material").status_code == 200
    store.update_project(p["id"],state={"skill_id":"not-a-catalogue-skill"})
    assert client.get(f"/api/projects/{p['id']}/material").status_code == 404


@pytest.mark.parametrize("ready", [False, True])
def test_project_detail_exposes_same_worker_readiness_as_material_context(api, monkeypatch, ready):
    client, _, project = api
    url = f"/api/projects/{project['id']}"
    monkeypatch.setenv("MATERIAL_WORKER_READY", str(ready).lower())
    detail = client.get(url).json()
    context = client.get(url + "/material").json()
    assert detail["material"]["enabled"] is True
    assert detail["material"]["worker_ready"] is context["worker_ready"] is ready


def test_non_innovation_export_job_runs_end_to_end(api, monkeypatch):
    import material_worker
    from docx import Document
    client, store, project = api
    project = store.update_project(project["id"], state={
        "skill_id": "national-scholarship", "outline_confirmed": True,
    })
    response = client.post(f"/api/projects/{project['id']}/material/jobs", json={
        "revision": project["material_revision"], "kind": "export",
        "idempotency_key": "national-export",
    })
    assert response.status_code == 202, response.text
    assert material_worker.run_one(local_store=store)
    job = MaterialJobs(store).call("get", project_id=project["id"], id=response.json()["id"])
    assert job["status"] == "completed", job
    path = store.files_dir / project["id"] / "_material" / job["id"] / "proposal.docx"
    assert "国家奖学金" in "\n".join(p.text for p in Document(path).paragraphs)
    download = client.get(f"/api/projects/{project['id']}/material/jobs/{job['id']}/files/proposal.docx")
    download.raise_for_status()
    assert download.content == path.read_bytes()
    digest = base64.b64encode(hashlib.sha256(download.content).digest()).decode("ascii")
    assert download.headers["Repr-Digest"] == f"sha-256=:{digest}:"
    assert download.headers["Content-Type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_worker_export_recovers_exact_request_and_checks_the_current_snapshot(api):
    import material_worker
    from docx import Document
    from io import BytesIO

    client, store, project = api
    pid = project["id"]
    base = f"/api/projects/{pid}/material"
    key = "a" * 32
    lookup = f"{base}/exports/by-request/{key}"
    body = {"revision": project["material_revision"], "kind": "export", "idempotency_key": key}
    assert client.get(lookup).status_code == 404
    receipt = client.post(f"{base}/jobs", json=body)
    assert receipt.status_code == 202, receipt.text
    pending = client.get(lookup)
    assert pending.headers["Cache-Control"] == "private, no-store"
    assert pending.json()["id"] == receipt.json()["id"]
    assert pending.json()["status"] == "pending"
    assert client.post(f"{base}/jobs", json=body).json()["id"] == pending.json()["id"]
    assert len(MaterialJobs(store).call("list", project_id=pid)) == 1

    other = store.create_project(title="另一个项目")
    store.update_project(other["id"], state={"skill_id": "innovation-research"})
    assert client.get(lookup.replace(pid, other["id"])).status_code == 404
    assert client.get(lookup.replace(key, "b" * 32)).status_code == 404
    assert material_worker.run_one(local_store=store)
    completed = client.get(lookup).json()
    assert completed["status"] == "completed"
    assert completed["id"] == pending.json()["id"]
    artifact = f"{base}/jobs/{completed['id']}/files/proposal.docx"
    latest = client.get(artifact + "?current=true")
    latest.raise_for_status()
    assert any("原文" in paragraph.text for paragraph in Document(BytesIO(latest.content)).paragraphs)

    store.add_message(pid, "assistant", "普通助手回复。")
    assert client.get(artifact + "?current=true").status_code == 200
    store.add_message(pid, "user", "这次请用新的申报范围。")
    stale = client.get(artifact + "?current=true")
    assert stale.status_code == 409 and "重新导出" in stale.json()["detail"]
    assert client.get(artifact).content == latest.content


def test_paid_job_lookup_recovers_original_and_replayed_apply_preserves_newer_text(api):
    client, store, project = api
    pid = project["id"]
    base = f"/api/projects/{pid}/material/jobs"
    key = "a" * 32
    body = {"revision": project["material_revision"], "kind": "section", "target": "method",
            "confirm_paid": True, "idempotency_key": key, "llm_config": {"provider": "ollama"}}
    submitted = client.post(base, json=body)
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["id"]
    queue = MaterialJobs(store)
    job = queue.claim_local()
    assert job["id"] == job_id
    queue.worker_local(job, "finish", status="completed", result={"content": "本次生成的正文", "status": "passed"})
    found = client.get(f"{base}/by-request/{key}")
    assert found.headers["Cache-Control"] == "private, no-store"
    assert found.json()["id"] == job_id and found.json()["status"] == "completed"
    assert "payload" not in found.json()
    assert client.post(base, json=body).json()["id"] == job_id
    assert len(queue.call("list", project_id=pid)) == 1
    other = store.create_project(title="另一个项目")
    assert client.get(f"{base}/by-request/{key}".replace(pid, other["id"])).status_code == 404

    apply_url = f"{base}/{job_id}/apply"
    applied = client.post(apply_url)
    applied.raise_for_status()
    assert applied.json()["material_revision"] == store.get_project(pid)["material_revision"]
    assert store.get_project(pid)["sections"]["method"] == "本次生成的正文"
    store.update_project(pid, sections={"method": "学生后来手写的正文"})
    replay = client.post(apply_url)
    replay.raise_for_status()
    assert replay.json()["conflict_status"] == "user_edit_preserved"
    assert store.get_project(pid)["sections"]["method"] == "学生后来手写的正文"
    assert len(store.list_section_versions(pid, "method")) == 1


def test_export_snapshot_preserves_income_optout_after_30_messages(api):
    import material_worker
    client, store, project = api
    pid = project["id"]
    state = {"skill_id": "grant-application", "outline_confirmed": True,
             "confirmed_facts": {"family_income": "2万元"}}
    project = store.update_project(pid, state=state, outline=[{"id": "letter", "title": "申请书"}],
                                   sections={"letter": "尊敬的老师，现申请国家助学金。"})
    store.add_message(pid, "user", "不要把我家的收入放进申请书。")
    for i in range(35):
        store.add_message(pid, "assistant", f"普通对话 {i}")
    url = f"/api/projects/{pid}/material/jobs"
    response = client.post(url, json={"revision": project["material_revision"], "kind": "export",
                                     "idempotency_key": "after-30"})
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    raw = MaterialJobs(store).call("raw", project_id=pid, id=job_id)
    assert any("不要把我家的收入" in item["content"] for item in raw["payload"]["messages"])
    assert material_worker.run_one(local_store=store)
    assert client.get(f"{url}/{job_id}/files/proposal.docx").status_code == 200
    # Other assets are not certified by checking the generated Word file.
    folder = store.files_dir / pid / "_material" / job_id
    (folder / "report.json").write_text('{"info":"示例"}', encoding="utf-8")
    (folder / "figure.png").write_bytes(b"synthetic")
    assert client.get(f"{url}/{job_id}/files/report.json").status_code == 409
    assert client.get(f"{url}/{job_id}/files/figure.png").status_code == 409


def test_running_export_cannot_be_downloaded_after_income_optout(api):
    import material_worker
    client, store, project = api
    pid = project["id"]
    state = {"skill_id": "grant-application", "outline_confirmed": True,
             "confirmed_facts": {"family_income": "2万元"}}
    project = store.update_project(pid, state=state, outline=[{"id": "letter", "title": "申请书"}],
                                   sections={"letter": "我的家庭年收入是2万元。"})
    url = f"/api/projects/{pid}/material/jobs"
    response = client.post(url, json={"revision": project["material_revision"], "kind": "export",
                                     "idempotency_key": "before-optout"})
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    store.add_message(pid, "user", "不要把我家的收入写进申请书。")
    assert material_worker.run_one(local_store=store)
    response = client.get(f"{url}/{job_id}/files/proposal.docx")
    assert response.status_code == 409 and "重新导出" in response.json()["detail"]
    store.add_message(pid, "user", "现在可以写家庭年收入。")
    assert client.get(f"{url}/{job_id}/files/proposal.docx").status_code == 200


def test_worker_submission_rejects_saved_income_after_optout_before_queue(api):
    client, store, project = api
    pid = project["id"]
    project = store.update_project(pid, state={"skill_id": "grant-application", "outline_confirmed": True,
        "confirmed_facts": {"family_income": "2万元"}},
        outline=[{"id": "letter", "title": "申请书"}], sections={"letter": "我家的收入是2万元。"})
    store.add_message(pid, "user", "不要把我家的收入写进申请书。")
    response = client.post(f"/api/projects/{pid}/material/jobs", json={
        "revision": project["material_revision"], "kind": "export", "idempotency_key": "no-income",
    })
    assert response.status_code == 422 and "正文" in response.json()["detail"]
    assert not MaterialJobs(store).call("list", project_id=pid)


def test_cloud_job_snapshots_retrieval_only_with_matching_user_consent(api, monkeypatch):
    import app
    import proposal.material_api as material_api
    client, store, project = api
    saved, queries = [], []

    class FakeQueue:
        cloud = True
        def __init__(self, _store):
            pass
        def find(self, *_args):
            return None
        def submit(self, project_id, kind, target, payload, idempotency):
            saved.append(payload)
            return {"id": payload["job_id"]}

    class FakeRetriever:
        def search(self, project_id, query, top_k=5):
            queries.append((project_id, query, top_k))
            return {"retrieval_mode": "hybrid", "results": [{"text": "已确认资料"}]}

    async def bypass_auth(request, call_next):
        return await call_next(request)

    monkeypatch.setattr(app, "CLOUD_MODE", True)
    monkeypatch.setattr(app, "_require_supabase_session", bypass_auth)
    monkeypatch.setattr(app, "_agent_for_config", lambda *a, **kw: object())
    monkeypatch.setattr(app, "retriever", FakeRetriever())
    monkeypatch.setattr(material_api, "MaterialJobs", FakeQueue)
    monkeypatch.setattr(store, "_identity", lambda: {"user_id": "owner"}, raising=False)
    url = f"/api/projects/{project['id']}/material/jobs"
    body = {"revision": project["material_revision"], "kind": "outline", "idempotency_key": "one",
            "llm_config": {"provider": "ollama"}, "confirm_paid": True}
    assert client.post(url, json=body).status_code == 202
    assert not queries and "private_retrieval" not in saved[-1]
    wrong = {"X-Private-RAG-Consent": "cloudflare-bge-m3-v1", "X-Private-RAG-Project": "a" * 32}
    assert client.post(url, headers=wrong, json=body).status_code == 202
    assert not queries and "private_retrieval" not in saved[-1]
    correct = {**wrong, "X-Private-RAG-Project": project["id"]}
    assert client.post(url, headers=correct, json=body).status_code == 202
    assert queries == [(project["id"], "研究问题 研究内容 方法 创新点", 5)]
    assert saved[-1]["private_retrieval"]["result"]["retrieval_mode"] == "hybrid"


def test_cloud_platform_jobs_fail_closed_until_billing_is_ready(api, monkeypatch):
    import app
    import proposal.material_api as material_api
    client, store, project = api
    accepted = []

    class FakeQueue:
        cloud = True
        def __init__(self, _store):
            pass
        def find(self, *_args):
            return None
        def submit(self, project_id, kind, target, payload, idempotency):
            accepted.append(payload)
            return {"id": payload["job_id"]}

    async def bypass_auth(request, call_next):
        return await call_next(request)

    monkeypatch.setattr(app, "CLOUD_MODE", True)
    monkeypatch.setattr(app, "_require_supabase_session", bypass_auth)
    monkeypatch.setattr(app, "_agent_for_config", lambda *a, **kw: object())
    monkeypatch.setattr(material_api, "MaterialJobs", FakeQueue)
    monkeypatch.setattr(material_api, "encrypt_config", lambda *_args: "test-ciphertext")
    monkeypatch.setattr(store, "_identity", lambda: {"user_id": "owner"}, raising=False)
    monkeypatch.delenv("MATERIAL_PLATFORM_JOBS_READY", raising=False)
    url = f"/api/projects/{project['id']}/material/jobs"
    body = {"revision": project["material_revision"], "kind": "outline", "idempotency_key": "one",
            "llm_config": {"provider": "preset"}, "confirm_paid": True}
    response = client.post(url, json=body)
    assert response.status_code == 503 and "账务" in response.json()["detail"]
    assert not accepted
    body["llm_config"] = {"provider": "openai_compatible", "base_url": "https://example.com/v1",
                          "api_key": "test-key"}
    assert client.post(url, json=body).status_code == 202
    assert accepted[-1]["platform"] is False
    monkeypatch.setenv("MATERIAL_PLATFORM_JOBS_READY", "true")
    body["llm_config"] = {"provider": "preset"}
    assert client.post(url, json=body).status_code == 202
    assert accepted[-1]["platform"] is True


def test_requirements_and_revision_conflict(api):
    client,store,p = api
    body = {"revision":p["material_revision"],"items":[{
        "key":"font","value":"宋体","source_kind":"official","source":"学校通知",
        "scope":"2026 测试大学","confirmed":True,"applicable":True}]}
    url = f"/api/projects/{p['id']}/material/requirements"
    r = client.put(url,json=body)
    assert r.status_code == 200, r.text
    assert r.json()["requirements"]["effective"]["font"]["value"] == "宋体"
    assert client.put(url,json=body).status_code == 409


def test_figure_plan_and_explicit_confirmation(api):
    client,store,p = api
    root = f"/api/projects/{p['id']}/material"
    spec = {"id":"flow","section_id":"method","title":"技术路线","renderer":"svg","nodes":["建模","验证"],"confirmed":False}
    assert client.put(root+"/plans",json={"revision":p["material_revision"],"items":[spec]}).status_code == 200
    p = store.get_project(p["id"])
    request = {"revision":p["material_revision"],"kind":"figure","target":"flow","idempotency_key":"click"}
    assert client.post(root+"/jobs",json=request).status_code == 422
    spec["confirmed"] = True
    assert client.put(root+"/plans",json={"revision":p["material_revision"],"items":[spec]}).status_code == 200
    request["revision"] = store.get_project(p["id"])["material_revision"]
    first = client.post(root+"/jobs",json=request)
    assert first.status_code == 202, first.text
    second = client.post(root+"/jobs",json=request)
    assert first.json()["id"] == second.json()["id"]
    assert "payload" not in first.json()
    other = store.create_project()
    assert client.get(f"/api/projects/{other['id']}/material/jobs/{first.json()['id']}").status_code == 404


def test_text_calls_require_confirmation_and_credentials_not_echoed(api):
    client,store,p = api
    root = f"/api/projects/{p['id']}/material"
    result = client.post(root+"/jobs",json={
        "revision":p["material_revision"],"kind":"section","target":"method",
        "idempotency_key":"text","llm_config":{"provider":"ollama"}})
    assert result.status_code == 422
    assert not MaterialJobs(store).call("list",project_id=p["id"])
    # Invalid model secrets cannot appear in the validation response.
    result = client.post(root+"/jobs",json={
        "revision":p["material_revision"],"kind":"section","target":"method",
        "idempotency_key":"text","confirm_paid":True,
        "llm_config":{"provider":"openai_compatible","base_url":"http://localhost","api_key":"SECRET_MARKER"}})
    assert result.status_code == 422
    assert "SECRET_MARKER" not in result.text


def test_upload_not_silently_confirmed_in_pilot(api):
    client,store,p = api
    result = client.post(f"/api/projects/{p['id']}/documents",files=[
        ("files",("source.txt","预算 20000 元".encode(),"text/plain"))])
    assert result.status_code == 200, result.text
    docs = store.list_documents(p["id"])
    assert not docs[0]["confirmed"]
    client.get(f"/api/projects/{p['id']}")
    assert not store.list_documents(p["id"])[0]["confirmed"]


def test_manual_section_cas(api):
    client,store,p = api
    url = f"/api/projects/{p['id']}/sections/method"
    first = client.put(url,json={"content":"","expected_revision":p["material_revision"]})
    assert first.status_code == 200
    assert client.put(url,json={"content":"覆盖","expected_revision":p["material_revision"]}).status_code == 409
    assert store.get_project(p["id"])["sections"]["method"] == ""


def test_direct_section_generation_reports_atomic_persistence_when_worker_unready(api, monkeypatch):
    import app
    client, store, project = api
    monkeypatch.setenv("MATERIAL_WORKER_READY", "false")
    monkeypatch.setattr(app, "_metered_invoke",
                        lambda *args, **kwargs: {"content": "新的生成草稿"})
    response = client.post(f"/api/projects/{project['id']}/sections/method/generate")
    assert response.status_code == 200
    result = response.json()
    assert result["material_applied"] is True
    assert result["material_revision"] == store.get_project(project["id"])["material_revision"]
    assert store.get_project(project["id"])["sections"]["method"] == "新的生成草稿"


def test_importing_a_completed_outline_job_returns_a_preview_without_replacing_saved_directory(api):
    client, store, project = api
    queue = MaterialJobs(store)
    identifier = "a" * 32
    queue.submit(project["id"], "outline", "", {"job_id": identifier,
        "base_revision": project["material_revision"], "input_snapshot": "snapshot"}, "outline-preview")
    job = queue.claim_local()
    candidate = [{"id": "different", "title": "候选目录"}]
    queue.worker_local(job, "finish", status="completed", result={"outline": candidate})
    url = f"/api/projects/{project['id']}/material/jobs/{identifier}/apply"
    response = client.post(url)
    assert response.status_code == 200 and response.json()["outline"] == candidate
    assert response.json()["applied"] is False
    current = store.get_project(project["id"])
    assert current["material_revision"] == project["material_revision"]
    assert current["outline"] == project["outline"] and current["state"]["outline_confirmed"] is True
