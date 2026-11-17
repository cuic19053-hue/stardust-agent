"""Single-process local worker with durable LangGraph checkpoints and input."""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from urllib.parse import urlsplit

import httpx
from langgraph.checkpoint.sqlite import SqliteSaver
from langsmith import tracing_context

from .review_graph import build_review_graph
from .review_runs import GRAPH_VERSION, ReviewNeedsReview, ReviewRuns
from .review_prompt import SYSTEM_PROMPT

class LocalReviewModel:
    context_size = 16384
    output_budget = 1400

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")).rstrip("/")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.path or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("本地预审模型必须使用本机回环地址")

    @staticmethod
    def messages(content):
        return [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(content, ensure_ascii=False)}]

    def accepts_input(self, content, schema):
        # UTF-8 bytes conservatively upper-bound byte-tokenizer input, including
        # schema text if a provider inserts it. Leave room for chat wrappers.
        input_bytes = sum(len(json.dumps(value, ensure_ascii=False).encode())
                          for value in (self.messages(content), schema))
        return input_bytes + self.output_budget + 1024 <= self.context_size

    def __call__(self, run, content, schema):
        if not self.accepts_input(content, schema):
            raise ValueError("输入超过本地模型上下文预算")
        started = time.monotonic()
        # Ignore proxy variables; never follow even a loopback server's redirect.
        with httpx.Client(timeout=120, follow_redirects=False, trust_env=False) as client:
            response = client.post(self.base_url + "/api/chat", json={
                "model": run["model"], "stream": False, "format": schema,
                "messages": self.messages(content),
                "options": {"temperature": 0, "num_ctx": self.context_size, "num_predict": self.output_budget},
            })
            if response.status_code != 200 or len(response.content) > 100_000:
                raise ReviewNeedsReview("模型响应未确认，需核对后重新检查")
            body = response.json()
        text = body.get("message", {}).get("content")
        if not isinstance(text, str):
            raise ReviewNeedsReview("模型响应未确认，需核对后重新检查")
        metrics = {"latency_ms": int((time.monotonic() - started) * 1000)}
        for key in ("prompt_eval_count", "eval_count"):
            value = body.get(key)
            if type(value) is int and value >= 0:
                metrics[key] = value
        return {"content": text, "metrics": metrics}


class ReviewWorker:
    """The OS releases our exclusive lock after exit, including SIGKILL.

    A replacement worker resumes pending graph steps from disk. A model call
    with an unrecorded outcome is quarantined instead of replayed.
    """
    def __init__(self, store, *, model_call=None, after_node=None):
        self.store = store
        self.runs = ReviewRuns(store)
        self.model_call = model_call
        self.after_node = after_node  # deterministic fault injection, never an API input
        self.lock = None

    def __enter__(self):
        handle = (self.store.data_dir / "review-worker.lock").open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("此数据目录已有预审 worker") from None
        self.lock = handle
        return self

    def __exit__(self, *_):
        if self.lock:
            self.lock.close()
            self.lock = None

    def run_once(self) -> dict | None:
        if self.lock is None:
            raise RuntimeError("必须在 worker 独占锁内执行")
        run = self.runs.claim()
        if run is None:
            return None
        run_id, project_id = run["id"], run["project_id"]
        if run["graph_version"] not in {GRAPH_VERSION, "proposal-review-v1"}:
            self.runs.update(run_id, status="needs_review", error="执行版本已变化，请核对后新建检查")
            return self.runs.get(project_id, run_id)
        conn = sqlite3.connect(self.store.db_path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            saver = SqliteSaver(conn)
            builder = build_review_graph
            if run["graph_version"] == "proposal-review-v1":
                from .review_graph_v1 import build_review_graph as builder
            graph = builder(self.runs, saver, self.model_call, graph_version=run["graph_version"])
            config = {"configurable": {"thread_id": run_id}, "recursion_limit": 15}
            previous = graph.get_state(config)
            inputs = None if previous.values else {"run_id": run_id}
            # No ambient external tracing of private drafts, even if another
            # local app exported LANGSMITH_TRACING=true into this shell.
            with tracing_context(enabled=False):
                for update in graph.stream(inputs, config, stream_mode="updates", durability="sync"):
                    if self.after_node:
                        self.after_node(next(iter(update)))
            final = graph.get_state(config).values
            if not final.get("report"):
                raise ValueError("缺少检查报告")
            self.runs.update(run_id, status="completed", step="completed", report=final["report"])
        except Exception:
            # A raw exception may include private input. Do not return/log it.
            try:
                with self.store.connect() as db:
                    started = db.execute("SELECT status FROM review_model_calls WHERE run_id=?", (run_id,)).fetchone()
                uncertain = started and started["status"] == "started"
                self.runs.update(run_id, status="needs_review" if uncertain else "failed",
                                 error="模型调用结果未确认，需核对后新建检查" if uncertain else "检查未完成，请核对后新建检查")
            except KeyError:
                pass  # Project deletion owns cascading cleanup.
        finally:
            conn.close()
        return self.runs.get(project_id, run_id)
