"""Fenced, per-turn operation journal for interactive chat (SQLite / Supabase).

Completed model/tool steps can replay. An in-flight operation with an unknown
outcome cannot. Credentials and provider headers never enter this journal.
"""
from __future__ import annotations

from contextvars import ContextVar
from copy import copy, deepcopy
from datetime import datetime, timezone
import json
import os
import secrets
import time
import uuid

from .review_runs import digest

active_journal = ContextVar("active_agent_journal", default=None)


class JournalConflict(ValueError):
    pass


def input_snapshot(store, project_id, runtime_chunks=None):
    project = store.get_project(project_id)
    documents = [{k: v for k, v in d.items() if k in {"id", "project_id", "original_name", "kind", "content_type",
                  "extracted_text", "extraction_status", "confirmed", "confirmation_origin", "extraction_warning", "low_confidence_count"}}
                 for d in store.list_documents(project_id)]
    raw_chunks = store.chunks_for_project(project_id)
    if runtime_chunks is not None:
        runtime_chunks.extend(raw_chunks)
    chunks = [{k: v for k, v in c.items() if not k.startswith("embedding")} for c in raw_chunks]
    # A stored hint is display metadata, never needed to execute the turn.
    project = deepcopy(project)
    project.get("state", {}).pop("last_model_config", None)
    snapshot = {"project": project, "documents": documents, "chunks": chunks,
                "history": store.messages(project_id, limit=200)}
    if len(json.dumps(snapshot, ensure_ascii=False).encode()) > 2_000_000:
        raise JournalConflict("本轮资料过大，请减少材料后继续。")
    return snapshot


def material_digest(snapshot):
    # Cloud's existing project trigger also increments revision for a chat's
    # updated_at touch. Compare actual input data, not that incidental counter.
    project = {k: v for k, v in snapshot["project"].items() if k not in {"updated_at", "created_at", "material_revision"}}
    return digest({"project": project, "documents": snapshot["documents"], "chunks": snapshot["chunks"]})


class AgentJournal:
    def __init__(self, store, project_id, key, profile, message):
        self.store, self.project_id = store, project_id
        self.index, self.lease, self.task_id = 0, secrets.token_hex(32), ""
        self.cloud = hasattr(store, "_rest")
        if os.environ.get("VERCEL") and not self.cloud:
            raise RuntimeError("云端对话必须使用持久化存储，不能回退临时 SQLite。")
        if not self.cloud:
            with store.connect() as db:
                db.executescript("""
                  CREATE TABLE IF NOT EXISTS agent_turns(
                    id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    key_hash TEXT NOT NULL,request_hash TEXT NOT NULL,snapshot TEXT NOT NULL,
                    input_digest TEXT NOT NULL,message TEXT NOT NULL,status TEXT NOT NULL,
                    lease TEXT,lease_until REAL,result TEXT,created_at REAL NOT NULL,
                    UNIQUE(project_id,key_hash));
                  CREATE TABLE IF NOT EXISTS agent_operations(
                    turn_id TEXT REFERENCES agent_turns(id) ON DELETE CASCADE,idx INTEGER NOT NULL,
                    kind TEXT NOT NULL,request_hash TEXT NOT NULL,status TEXT NOT NULL,result TEXT,
                    PRIMARY KEY(turn_id,idx));
                """)
        self.request = {"key_hash": digest(key), "request_hash": digest({"message": message, "profile": profile}),
                        "message": message}

    def rpc(self, action, **args):
        payload = {"p_action": action, "p_secret": os.environ.get("WORKBENCH_BACKEND_SECRET", ""),
                   "p_args": {"project_id": self.project_id, "id": self.task_id, "lease": self.lease, **args}}
        result = self.store._rest("rpc/proposal_agent_turn", "POST", payload=payload)
        if result and result.get("conflict"):
            raise JournalConflict(result["conflict"])
        return result

    def open(self):
        if self.cloud:
            known = self.rpc("lookup", **self.request)
        else:
            with self.store.connect() as db:
                row = db.execute("SELECT * FROM agent_turns WHERE project_id=? AND key_hash=?",
                                 (self.project_id, self.request["key_hash"])).fetchone()
                known = dict(row) if row else None
                if known and known["request_hash"] != self.request["request_hash"]:
                    raise JournalConflict("同一请求标识已用于不同内容或模型配置。")
                if known and known["status"] == "completed":
                    known["result"] = json.loads(known["result"])
        if known and known["status"] == "completed":
            return known
        self.runtime_chunks = []
        snapshot = input_snapshot(self.store, self.project_id, self.runtime_chunks)
        if self.cloud:
            task = self.rpc("open", **self.request, snapshot=snapshot, input_digest=material_digest(snapshot))
        else:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM agent_turns WHERE project_id=? AND key_hash=?",
                                 (self.project_id, self.request["key_hash"])).fetchone()
                if row:
                    task = dict(row)
                    if task["request_hash"] != self.request["request_hash"]:
                        raise JournalConflict("同一请求标识已用于不同内容或模型配置。")
                    if task["status"] == "completed":
                        task["result"] = json.loads(task["result"])
                        return task
                    if task["status"] == "needs_review":
                        raise JournalConflict("上次调用结果尚未确认，不会自动重发。请查看已收到的内容和用量记录。")
                    if task["lease_until"] and task["lease_until"] > time.time():
                        raise JournalConflict("该轮对话仍在执行，请稍后用原请求恢复。")
                    unknown = db.execute("SELECT 1 FROM agent_operations WHERE turn_id=? AND status='started'", (task["id"],)).fetchone()
                    if unknown:
                        db.execute("UPDATE agent_turns SET status='needs_review' WHERE id=?", (task["id"],))
                        # Commit before reporting the uncertain result.
                        task["status"] = "needs_review"
                    else:
                        old = json.loads(task["snapshot"])
                        if task["input_digest"] != material_digest(snapshot) or snapshot["history"] != (old["history"] + [{"role": "user", "content": task["message"]}])[-200:]:
                            raise JournalConflict("项目、材料或对话已变化，请保留原结果并发起新一轮。")
                else:
                    task = {"id": uuid.uuid4().hex, "snapshot": json.dumps(snapshot, ensure_ascii=False), "status": "running"}
                    db.execute("""INSERT INTO agent_turns(id,project_id,key_hash,request_hash,snapshot,input_digest,message,status,created_at)
                        VALUES(?,?,?,?,?,?,?,'running',?)""", (task["id"], self.project_id, self.request["key_hash"],
                        self.request["request_hash"], task["snapshot"], material_digest(snapshot), self.request["message"], time.time()))
                    db.execute("INSERT INTO messages(id,project_id,role,content,created_at) VALUES(?,?,'user',?,?)",
                               (uuid.uuid4().hex, self.project_id, self.request["message"], datetime.now(timezone.utc).isoformat()))
                    db.execute("UPDATE projects SET updated_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), self.project_id))
                if task["status"] != "needs_review":
                    active = db.execute("SELECT 1 FROM agent_turns WHERE project_id=? AND id<>? AND status='running' AND lease_until>?",
                                        (self.project_id, task["id"], time.time())).fetchone()
                    if active:
                        raise JournalConflict("项目还有一轮对话在执行，请稍后继续。")
                    db.execute("UPDATE agent_turns SET status='running',lease=?,lease_until=? WHERE id=?",
                               (self.lease, time.time() + 120, task["id"]))
        if task["status"] == "needs_review":
            raise JournalConflict("上次调用结果尚未确认，不会自动重发。请查看用量记录。")
        if task["status"] == "completed":
            return task
        self.task_id = task["id"]
        self.snapshot = json.loads(task["snapshot"]) if isinstance(task["snapshot"], str) else task["snapshot"]
        return task

    def _fence(self, db):
        row = db.execute("SELECT * FROM agent_turns WHERE id=? AND project_id=? AND status='running' AND lease=? AND lease_until>?",
                         (self.task_id, self.project_id, self.lease, time.time())).fetchone()
        if row is None:
            raise JournalConflict("本轮执行租约已失效，请查看原任务。")
        db.execute("UPDATE agent_turns SET lease_until=? WHERE id=?", (time.time() + 120, self.task_id))

    def begin(self, kind, request):
        idx, self.index = self.index, self.index + 1
        fingerprint = digest(request)
        if self.cloud:
            return idx, self.rpc("begin", idx=idx, kind=kind, request_hash=fingerprint)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._fence(db)
            row = db.execute("SELECT * FROM agent_operations WHERE turn_id=? AND idx=?", (self.task_id, idx)).fetchone()
            if row:
                if row["request_hash"] != fingerprint or row["kind"] != kind:
                    raise JournalConflict("恢复步骤与原请求不一致，已停止执行。")
                if row["status"] != "completed":
                    raise JournalConflict("这一步的执行结果尚未确认，不会自动重发。")
                return idx, {"cached": True, "result": json.loads(row["result"])}
            db.execute("INSERT INTO agent_operations(turn_id,idx,kind,request_hash,status) VALUES(?,?,?,?,'started')",
                       (self.task_id, idx, kind, fingerprint))
            return idx, {"cached": False}

    def record(self, idx, result):
        encoded = json.dumps(result, ensure_ascii=False)
        if len(encoded.encode()) > 1_000_000:
            raise JournalConflict("单步结果过大，已停止自动恢复。")
        if self.cloud:
            self.rpc("record", idx=idx, result=result)
            return
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._fence(db)
            if not db.execute("UPDATE agent_operations SET status='completed',result=? WHERE turn_id=? AND idx=? AND status='started'",
                              (encoded, self.task_id, idx)).rowcount:
                raise JournalConflict("步骤结果已被其他执行记录。")

    def execute(self, kind, request, call):
        idx, operation = self.begin(kind, request)
        if operation["cached"]:
            return operation["result"]
        result = call()
        self.record(idx, result)
        return result

    def complete(self, result):
        if self.cloud:
            self.rpc("complete", result=result)
            return
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._fence(db)
            if db.execute("SELECT 1 FROM agent_operations WHERE turn_id=? AND status='started'", (self.task_id,)).fetchone():
                raise JournalConflict("仍有调用结果未确认，本轮尚不能标为完成。")
            db.execute("INSERT INTO messages(id,project_id,role,content,created_at) VALUES(?,?,'assistant',?,?)",
                       (uuid.uuid4().hex, self.project_id, result["message"], datetime.now(timezone.utc).isoformat()))
            db.execute("UPDATE projects SET updated_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), self.project_id))
            db.execute("UPDATE agent_turns SET status='completed',result=?,lease=NULL,lease_until=NULL WHERE id=?",
                       (json.dumps(result, ensure_ascii=False), self.task_id))

    def suspend(self):
        if self.cloud:
            self.rpc("suspend")
            return
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._fence(db)
            unknown = db.execute("SELECT 1 FROM agent_operations WHERE turn_id=? AND status='started'", (self.task_id,)).fetchone()
            db.execute("UPDATE agent_turns SET status=?,lease=NULL,lease_until=NULL WHERE id=?",
                       ("needs_review" if unknown else "suspended", self.task_id))


class TurnStore:
    """Frozen input reads; conversation writes commit once with task completion."""
    def __init__(self, journal):
        self.journal, self.user_added = journal, False
        # Rank only captured text. Existing vectors remain ephemeral and retain
        # their embedding revision/consent checks in ProjectRetriever.
        self.search_project_chunks = None

    def __getattr__(self, name):
        return getattr(self.journal.store, name)

    def get_project(self, project_id):
        return deepcopy(self.journal.snapshot["project"]) if project_id == self.journal.project_id else None

    def messages(self, project_id, limit=30):
        history = deepcopy(self.journal.snapshot["history"])
        if self.user_added:
            history.append({"role": "user", "content": self.journal.request["message"]})
        return history[-limit:] if project_id == self.journal.project_id else []

    def add_message(self, project_id, role, content):
        if project_id != self.journal.project_id:
            raise JournalConflict("对话项目不匹配。")
        if role == "user":
            self.user_added = True

    def list_documents(self, project_id):
        return deepcopy(self.journal.snapshot["documents"]) if project_id == self.journal.project_id else []

    def chunks_for_project(self, project_id):
        if project_id != self.journal.project_id:
            return []
        runtime = {c["id"]: c for c in self.journal.runtime_chunks}
        return [{**deepcopy(c), **{k: v for k, v in runtime.get(c["id"], {}).items()
                                 if k.startswith("embedding") and runtime[c["id"]]["content"] == c["content"]}}
                for c in self.journal.snapshot["chunks"]]


def bind_runtime(agent):
    journal = active_journal.get()
    if not journal:
        return agent
    bound = copy(agent)
    bound.store = TurnStore(journal)
    bound.tools = copy(agent.tools)
    bound.tools.store = bound.store
    retriever = getattr(bound.tools, "retriever", None)
    if retriever:
        bound.tools.retriever = copy(retriever)
        bound.tools.retriever.store = bound.store
    return bound
