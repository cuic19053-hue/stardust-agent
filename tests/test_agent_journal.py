"""Exercise actual chat execution, disk recovery, deduplication and unknown calls."""
import json

import pytest
import server.app
from proposal.agent import OllamaAgent
from proposal.agent_journal import AgentJournal, JournalConflict, active_journal
from proposal.retrieval import ProjectRetriever
from proposal.storage import Store
from proposal.tools import ProposalTools


@pytest.fixture
def chat(tmp_path, monkeypatch):
    store = Store(tmp_path)
    project = store.create_project("合成聊天恢复项目")
    tools = ProposalTools(store, ProjectRetriever(store))
    agent = OllamaAgent(store, tools)
    monkeypatch.setattr(agent, "_policy_evidence_reply", lambda *_: None)
    return store, project["id"], agent


def open_turn(store, pid, key="request", message="请分析这个项目"):
    journal = AgentJournal(store, pid, key, "model-profile-hash", message)
    task = journal.open()
    return journal, task


def invoke(journal, agent, pid, stream=False):
    token = active_journal.set(journal)
    try:
        if stream:
            return list(agent.chat_stream(pid, "请分析这个项目"))[-1]
        return agent.chat(pid, "请分析这个项目")
    finally:
        active_journal.reset(token)


def test_completed_turn_replays_without_model_call_or_duplicate_messages(chat, monkeypatch):
    store, pid, agent = chat
    calls = []
    monkeypatch.setattr(agent, "_post", lambda *_: calls.append(1) or {"message": {"role": "assistant", "content": "请先明确研究问题。"}})
    journal, _ = open_turn(store, pid)
    result = invoke(journal, agent, pid)
    assert store.messages(pid) == [{"role": "user", "content": "请分析这个项目"}]
    journal.complete(result)
    _, reopened = open_turn(Store(store.data_dir), pid)
    assert reopened["status"] == "completed" and reopened["result"]["message"] == result["message"]
    assert calls == [1]
    assert [m["role"] for m in store.messages(pid)] == ["user", "assistant"]


@pytest.mark.parametrize("stream,resume_stream", [(False, False), (True, True), (False, True), (True, False)])
def test_restart_after_known_model_and_tool_results_does_not_repeat_them(chat, monkeypatch, stream, resume_stream):
    store, pid, agent = chat
    calls, tools_called = [], []
    tool = {"function": {"name": "get_project_state", "arguments": {"project_id": pid}}}
    responses = [{"message": {"role": "assistant", "content": "", "tool_calls": [tool]}},
                 {"message": {"role": "assistant", "content": "这是依据项目状态给出的分析。"}}]
    def model(*_):
        calls.append(1)
        return responses[len(calls) - 1]
    monkeypatch.setattr(agent, "_post", model)
    def streaming(_payload):
        response = model()
        yield {"type": "final", "message": response["message"]}
    monkeypatch.setattr(agent, "_stream_post", streaming)
    original = agent.tools.invoke
    monkeypatch.setattr(agent.tools, "invoke", lambda name, args: tools_called.append(name) or original(name, args))
    journal, _ = open_turn(store, pid)
    record = journal.record
    def crash_after_tool(idx, result):
        record(idx, result)
        if idx == 1:
            raise KeyboardInterrupt("process exited after durable tool result")
    monkeypatch.setattr(journal, "record", crash_after_tool)
    with pytest.raises(KeyboardInterrupt):
        invoke(journal, agent, pid, stream)
    with store.connect() as db:
        db.execute("UPDATE agent_turns SET lease_until=0")
    resumed, _ = open_turn(Store(store.data_dir), pid)
    result = invoke(resumed, agent, pid, resume_stream)
    resumed.complete(result)
    assert calls == [1, 1]  # first inference replayed; only the final inference is new
    assert tools_called == ["get_project_state"]
    assert len(store.messages(pid)) == 2


def test_unknown_inference_is_terminal_and_never_replayed(chat, monkeypatch):
    store, pid, agent = chat
    calls = []
    def crash(*_):
        calls.append(1)
        raise KeyboardInterrupt("in flight")
    monkeypatch.setattr(agent, "_post", crash)
    journal, _ = open_turn(store, pid)
    with pytest.raises(KeyboardInterrupt):
        invoke(journal, agent, pid)
    with store.connect() as db:
        db.execute("UPDATE agent_turns SET lease_until=0")
    with pytest.raises(JournalConflict, match="未确认"):
        open_turn(Store(store.data_dir), pid)
    assert calls == [1] and len(store.messages(pid)) == 1
    with store.connect() as db:
        assert db.execute("SELECT status FROM agent_turns").fetchone()[0] == "needs_review"


def test_request_conflict_fencing_changed_input_and_project_deletion(chat):
    store, pid, _ = chat
    journal, _ = open_turn(store, pid)
    with pytest.raises(JournalConflict):
        open_turn(store, pid, message="不同指令")
    with pytest.raises(JournalConflict, match="仍在执行"):
        open_turn(store, pid)
    journal.suspend()
    resumed, _ = open_turn(store, pid)
    with pytest.raises(JournalConflict, match="租约"):
        journal.begin("model", {})
    resumed.suspend()
    store.update_project(pid, title="学生已改项目")
    with pytest.raises(JournalConflict, match="已变化"):
        open_turn(store, pid)
    store.delete_project(pid)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM agent_turns").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM agent_operations").fetchone()[0] == 0


def test_trial_allocation_and_tool_results_are_cached_once(chat):
    store, pid, _ = chat
    journal, _ = open_turn(store, pid)
    calls = []
    assert journal.execute("trial", {}, lambda: calls.append(1) or 2) == 2
    journal.suspend()
    resumed, _ = open_turn(store, pid)
    assert resumed.execute("trial", {}, lambda: calls.append(2)) == 2
    assert calls == [1]
    # A different task cannot see or reuse this project's private task.
    other = store.create_project("其他项目")
    separate, _ = open_turn(store, other["id"])
    assert separate.task_id != resumed.task_id


def test_no_credentials_or_paths_persist_in_operation_inputs(chat):
    store, pid, _ = chat
    journal, _ = open_turn(store, pid)
    journal.execute("model", {"api_key": "synthetic-secret-must-never-be-stored"}, lambda: {"message": "合成结果"})
    with store.connect() as db:
        data = json.dumps([dict(r) for r in db.execute("SELECT * FROM agent_operations")])
    assert "synthetic-secret" not in data


def test_serverless_never_falls_back_to_a_temporary_local_turn_ledger(chat, monkeypatch):
    store, pid, _ = chat
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(RuntimeError, match="持久化存储"):
        open_turn(store, pid)
    with store.connect() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='agent_turns'").fetchone() is None


def test_material_versions_and_history_survive_restart_without_copying_bodies(chat, monkeypatch):
    from proposal.agent_journal import TurnStore
    store, pid, _ = chat
    raw = "准备开展现场观测。" * 10_000
    doc = store.add_document(pid, "记录.txt", "private-path", "source", "text/plain")
    store.apply_document_review(doc["id"], 0, raw, "ready", True, [{"content": raw[:800], "source_ref": "段落 1"}])
    for i in range(200):
        store.add_message(pid, "user", f"历史 {i} " + "测" * 3650)
    original = store.messages(pid, limit=200)
    with monkeypatch.context() as patch:
        for name in ("list_documents", "chunks_for_project", "get_document"):
            patch.setattr(store, name, lambda *_a, **_kw: pytest.fail("unexpected eager body read"))
        journal, _ = open_turn(store, pid)
        assert TurnStore(journal).document_headers(pid)[0]["review_revision"] == 1
    with store.connect() as db:
        snapshot = db.execute("SELECT snapshot FROM agent_turns").fetchone()[0]
    assert len(snapshot.encode()) < 25_000
    assert "准备开展" not in snapshot and "历史" not in snapshot and "private-path" not in snapshot
    journal.suspend()
    resumed, _ = open_turn(Store(store.data_dir), pid)
    bound = TurnStore(resumed)
    assert bound.messages(pid, 200) == original
    assert bound.list_documents(pid)[0]["extracted_text"] == raw
    result = ProjectRetriever(bound, embedder=object()).search(pid, "现场观测")
    assert result["results"][0]["source_ref"] == "段落 1"
    assert not bound.chunks_for_project("different-project")
    store.apply_document_review(doc["id"], 1, "材料已撤回", "ready", False, [])
    with pytest.raises(JournalConflict, match="变化"):
        bound.chunks_for_project(pid)
    resumed.suspend()
    with pytest.raises(JournalConflict, match="变化"):
        open_turn(Store(store.data_dir), pid)


def test_existing_v1_turn_recovers_with_its_original_inputs(chat):
    from proposal.agent_journal import TurnStore, legacy_snapshot, material_digest
    store, pid, _ = chat
    store.add_message(pid, "user", "上轮消息")
    old = legacy_snapshot(store, pid, [])
    journal, _ = open_turn(store, pid)
    journal.execute("tool", {"x": 1}, lambda: "已记录")
    journal.suspend()
    with store.connect() as db:
        db.execute("UPDATE agent_turns SET snapshot=?,input_digest=?",
                   (json.dumps(old), material_digest(old)))
    resumed, _ = open_turn(Store(store.data_dir), pid)
    assert TurnStore(resumed).messages(pid) == old["history"]
    assert resumed.execute("tool", {"x": 1}, lambda: pytest.fail("repeated tool")) == "已记录"


def test_cloud_reference_scan_pages_only_headers_and_chunk_ids(tmp_path, monkeypatch):
    from proposal.supabase_store import SupabaseStore
    store = SupabaseStore("https://example.test", "unused", data_dir=tmp_path)
    requests = []
    rows = [{"id": str(i), "kind": "source", "confirmed": True, "confirmation_origin": "user"}
            for i in range(3)]

    def rest(table, *, params):
        requests.append((table, params))
        offset = int(params["offset"])
        # Simulate a deployment with a cap smaller than the requested page size.
        return rows[offset:offset + 1]

    monkeypatch.setattr(store, "_rest", rest)
    assert len(store.document_headers("project-one")) == 3
    assert store.confirmed_chunk_ids("project-one") == ["0", "1", "2"]
    for table, params in requests:
        assert params["project_id"] == "eq.project-one"
        assert "extracted_text" not in params["select"] and "*" not in params["select"]
        if table == "chunks":
            assert params["select"].startswith("id,") and params["documents.confirmation_origin"] == "eq.user"
            assert params["documents.project_id"] == "eq.project-one"
