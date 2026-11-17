"""Local, durable, immutable-input review runs; never used by a cloud store."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any

from .storage import Store

GRAPH_VERSION = "proposal-review-v2"
PROMPT_VERSION = "proposal-review-v2"
MAX_SNAPSHOT_BYTES = 1_000_000


class ReviewConflict(ValueError):
    pass


class ReviewNeedsReview(RuntimeError):
    pass


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def capture_snapshot(store: Store, db: sqlite3.Connection, project_id: str) -> dict:
    """One SQLite read transaction, including documents and searchable chunks."""
    row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise KeyError("项目不存在")
    project = store._project_row(row)
    if project.get("state", {}).get("skill_id", "innovation-research") != "innovation-research":
        raise ValueError("首版草稿预审仅支持大创创新训练项目")
    project = {key: project.get(key) for key in
               ("id", "title", "school", "level", "discipline", "state", "outline", "sections", "material_revision")}
    project["state"] = {key: value for key, value in project["state"].items() if key in {
        "skill_id", "problem", "target_user", "method", "foundation", "budget",
        "confirmed_facts", "references", "official_sources"}}
    if not any(str(value).strip() for value in project["sections"].values()):
        raise ValueError("请先保存至少一章草稿")
    documents = []
    for row in db.execute("SELECT * FROM documents WHERE project_id=? ORDER BY id", (project_id,)):
        document = store._document_row(row)
        # Paths, uploaded bytes and embeddings never enter a checkpoint.
        documents.append({key: document.get(key) for key in (
            "id", "project_id", "original_name", "kind", "extracted_text", "extraction_status",
            "confirmed", "confirmation_origin", "low_confidence_count")})
    chunks = [dict(row) for row in db.execute("""SELECT c.id,c.project_id,c.document_id,
        c.source_name,c.source_ref,c.content FROM chunks c
        JOIN documents d ON d.id=c.document_id AND d.project_id=c.project_id
        WHERE c.project_id=? AND d.confirmed=1 AND d.confirmation_origin='user'
          AND d.kind!='template' AND instr(d.extracted_text,c.content)>0 ORDER BY c.id""", (project_id,))]
    value = {"project": project, "documents": documents, "chunks": chunks}
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_SNAPSHOT_BYTES:
        raise ValueError("本地预审材料超过 1 MB，请减少材料后重试")
    return value


class ReviewRuns:
    def __init__(self, store: Store):
        if not isinstance(store, Store) or hasattr(store, "_rest"):
            raise ValueError("ReviewRuns requires a local Store")
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS review_runs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    key_hash TEXT NOT NULL, request_hash TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, snapshot TEXT NOT NULL, mode TEXT NOT NULL,
                    graph_version TEXT NOT NULL, model TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', step TEXT NOT NULL DEFAULT 'queued',
                    report TEXT, error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(project_id,key_hash)
                );
                CREATE INDEX IF NOT EXISTS review_runs_project ON review_runs(project_id,created_at DESC);
                CREATE TABLE IF NOT EXISTS review_model_calls (
                    run_id TEXT PRIMARY KEY REFERENCES review_runs(id) ON DELETE CASCADE,
                    status TEXT NOT NULL, result TEXT
                );
                CREATE TABLE IF NOT EXISTS review_events (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
                    node TEXT NOT NULL, duration_ms INTEGER NOT NULL, created_at REAL NOT NULL
                );
                /* SqliteSaver 3.1.3 schema, with run-owned cascading deletion.
                   All checkpoint connections enable foreign_keys, so a deleted
                   project cannot be recreated by an in-flight checkpoint write. */
                CREATE TABLE IF NOT EXISTS checkpoints (
                    thread_id TEXT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
                    checkpoint_ns TEXT NOT NULL DEFAULT '', checkpoint_id TEXT NOT NULL,
                    parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB,
                    PRIMARY KEY(thread_id,checkpoint_ns,checkpoint_id)
                );
                CREATE TABLE IF NOT EXISTS writes (
                    thread_id TEXT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
                    checkpoint_ns TEXT NOT NULL DEFAULT '', checkpoint_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, task_path TEXT NOT NULL DEFAULT '', idx INTEGER NOT NULL,
                    channel TEXT NOT NULL, type TEXT, value BLOB,
                    PRIMARY KEY(thread_id,checkpoint_ns,checkpoint_id,task_id,idx)
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(review_runs)")}
            if "parent_id" not in columns:
                db.execute("ALTER TABLE review_runs ADD COLUMN parent_id TEXT REFERENCES review_runs(id) ON DELETE SET NULL")
                db.execute("ALTER TABLE review_runs ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'")

    def current(self, project_id: str) -> str:
        with self.store.connect() as db:
            db.execute("BEGIN")
            return digest(capture_snapshot(self.store, db, project_id))

    def submit(self, project_id: str, key: str, fingerprint: str, mode: str, model: str,
               *, parent_id: str | None = None, origin: str = "manual") -> dict:
        if mode not in {"rules", "model"} or not key or len(key) > 160:
            raise ValueError("预审参数不正确")
        request_hash = digest({"fingerprint": fingerprint, "mode": mode,
                               "graph_version": GRAPH_VERSION, "model": model,
                               "parent_id": parent_id, "origin": origin})
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM review_runs WHERE project_id=? AND key_hash=?",
                                  (project_id, digest(key))).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise ReviewConflict("同一次提交的输入已变化，请重新检查")
                return self.public(existing)
            if origin not in {"manual", "chat_draft", "section_generation", "material_confirmation"}:
                raise ValueError("检查来源不正确")
            if parent_id:
                parent = db.execute("SELECT * FROM review_runs WHERE id=? AND project_id=?", (parent_id, project_id)).fetchone()
                if not parent or parent["status"] != "completed":
                    raise ReviewConflict("原检查尚未完成或不存在")
                if parent["fingerprint"] == fingerprint:
                    raise ReviewConflict("请先修改正文或补充并确认资料，再继续检查新版本")
            snapshot = capture_snapshot(self.store, db, project_id)
            if digest(snapshot) != fingerprint:
                raise ReviewConflict("正文或材料已变化，请刷新后重新检查")
            now, run_id = time.time(), uuid.uuid4().hex
            db.execute("""INSERT INTO review_runs
                (id,project_id,key_hash,request_hash,fingerprint,snapshot,mode,graph_version,model,created_at,updated_at,parent_id,origin)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (run_id, project_id, digest(key), request_hash,
                fingerprint, json.dumps(snapshot, ensure_ascii=False), mode, GRAPH_VERSION, model, now, now, parent_id, origin))
            return self.public(db.execute("SELECT * FROM review_runs WHERE id=?", (run_id,)).fetchone())

    @staticmethod
    def public(row) -> dict | None:
        if row is None:
            return None
        result = {key: row[key] for key in ("id", "project_id", "fingerprint", "mode", "graph_version",
                   "model", "status", "step", "error", "created_at", "updated_at", "parent_id", "origin")}
        result["report"] = json.loads(row["report"]) if row["report"] else None
        return result

    def get(self, project_id: str, run_id: str) -> dict | None:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM review_runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone()
            result = self.public(row)
            if result:
                result["events"] = [dict(event) for event in db.execute(
                    "SELECT node,duration_ms FROM review_events WHERE run_id=? ORDER BY id", (run_id,))]
            return result

    def list(self, project_id: str) -> list[dict]:
        with self.store.connect() as db:
            return [self.public(row) for row in db.execute(
                "SELECT * FROM review_runs WHERE project_id=? ORDER BY created_at DESC LIMIT 20", (project_id,))]

    def raw(self, run_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM review_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("检查任务不存在")
        return dict(row)

    def claim(self) -> dict | None:
        # Caller holds the one-process OS lock throughout the worker lifetime.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM review_runs WHERE status IN ('pending','running') ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE review_runs SET status='running',updated_at=? WHERE id=?", (time.time(), row["id"]))
            return dict(row)

    def update(self, run_id: str, *, status: str = "running", step: str | None = None,
               report: dict | None = None, error: str = "") -> None:
        with self.store.connect() as db:
            changed = db.execute("""UPDATE review_runs SET status=?,step=coalesce(?,step),
                report=coalesce(?,report),error=?,updated_at=? WHERE id=? AND status='running'""",
                (status, step, json.dumps(report, ensure_ascii=False) if report is not None else None,
                 error, time.time(), run_id)).rowcount
            if not changed:
                raise KeyError("检查任务不存在或已结束")

    def event(self, run_id: str, node: str, duration_ms: int) -> None:
        with self.store.connect() as db:
            db.execute("INSERT INTO review_events(run_id,node,duration_ms,created_at) VALUES (?,?,?,?)",
                       (run_id, node, duration_ms, time.time()))

    def model_result(self, run_id: str, call) -> dict:
        """Never replay an inference whose result was not durably recorded."""
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM review_model_calls WHERE run_id=?", (run_id,)).fetchone()
            if row:
                if row["status"] == "completed":
                    return json.loads(row["result"])
                raise ReviewNeedsReview("模型调用结果未确认，需核对后新建检查")
            db.execute("INSERT INTO review_model_calls(run_id,status) VALUES (?,'started')", (run_id,))
        result = call()
        with self.store.connect() as db:
            db.execute("UPDATE review_model_calls SET status='completed',result=? WHERE run_id=?",
                       (json.dumps(result, ensure_ascii=False), run_id))
        return result
