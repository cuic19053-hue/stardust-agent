"""Supabase queue, scoped checkpoints and bounded serverless LangGraph steps."""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP
from langsmith import tracing_context

from .agent import OllamaAgent
from .review_graph import build_review_graph
from .review_runs import ReviewConflict, ReviewNeedsReview
from .storage import InsufficientCreditsError
from .supabase_http import supabase_urlopen
from .workbench_metering import WorkbenchMeter

CLOUD_GRAPH_VERSION = "proposal-review-cloud-v1"
NODES = ["load_snapshot", "retrieve", "rules", "semantic", "verify", "save_report"]
logger = logging.getLogger(__name__)


def cloud_review_enabled():
    return (os.environ.get("CLOUD_REVIEW_ENABLED", "").lower() == "true"
            and len(os.environ.get("REVIEW_WORKER_SECRET", "")) == 64
            and len(os.environ.get("WORKBENCH_BACKEND_SECRET", "")) == 64)


class CloudReviewRuns:
    """User-facing operations retain the existing request JWT / owner RLS."""
    def __init__(self, store):
        self.store = store

    def call(self, action, project_id, **args):
        payload = {"p_action": action, "p_args": {"project_id": project_id, **args}}
        if action == "submit":
            payload["p_secret"] = os.environ.get("WORKBENCH_BACKEND_SECRET", "")
        return self.store._rest("rpc/proposal_review_api", "POST", payload=payload)

    def listing(self, project_id):
        return self.call("list", project_id)

    def get(self, project_id, run_id):
        return self.call("get", project_id, id=run_id)

    def submit(self, project_id, key, fingerprint, mode, _model=""):
        result = self.call("submit", project_id, key=key, fingerprint=fingerprint, mode=mode)
        if result.get("conflict"):
            raise ReviewConflict("正文、材料或本次提交的输入已变化，请刷新后重新检查")
        if result.get("insufficient_credits"):
            raise InsufficientCreditsError("AI 预审需预留 2 积分，余额不足；可以先运行规则检查。")
        if result.get("invalid_input"):
            raise ValueError(result["invalid_input"])
        if result.get("unavailable"):
            raise RuntimeError("云端预审暂不可用")
        return result


class ReviewRPC:
    """Server-only capability; every post-claim action also requires its lease."""
    def __init__(self, project_url, publishable_key, secret, job=None):
        self.url, self.key, self.secret = project_url.rstrip("/"), publishable_key, secret
        self.job = job

    def call(self, action, **args):
        if self.job:
            args = {"id": self.job["id"], "lease": self.job["lease"], **args}
        return self.post("proposal_review_worker", {"p_secret": self.secret, "p_action": action, "p_args": args})

    def post(self, function, payload):
        if function not in {"proposal_review_worker", "proposal_review_configure"}:
            raise ValueError("检查接口不支持")
        request = urllib.request.Request(self.url + "/rest/v1/rpc/" + function,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"apikey": self.key, "Authorization": "Bearer " + self.key, "Content-Type": "application/json"},
            method="POST")
        try:
            with supabase_urlopen(request, timeout=8) as response:
                raw = response.read(6_000_001)
            if len(raw) > 6_000_000:
                raise ValueError
            return json.loads(raw) if raw else None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise RuntimeError("云端预审状态未确认，请稍后查看原任务。") from None


def configure_scheduler(store, enabled):
    worker = os.environ.get("REVIEW_WORKER_SECRET", "")
    backend = os.environ.get("WORKBENCH_BACKEND_SECRET", "")
    if len(worker) != 64 or len(backend) != 64:
        raise RuntimeError("检查服务能力未配置")
    rpc = ReviewRPC(store.project_url, store.publishable_key, worker)
    return rpc.post("proposal_review_configure", {"p_backend": backend, "p_worker": worker, "p_enabled": enabled})


class LeasedReviewRuns:
    def __init__(self, rpc):
        self.rpc = rpc
        self.job = rpc.job

    def raw(self, run_id):
        if run_id != self.job["id"]:
            raise PermissionError("检查任务范围不匹配")
        return {**deepcopy(self.job), "snapshot": json.dumps(self.job["snapshot"], ensure_ascii=False)}

    def update(self, run_id, *, status="running", step=None, report=None, error=""):
        if run_id != self.job["id"] or status != "running":
            raise PermissionError("检查任务范围不匹配")
        if step:
            self.rpc.call("progress", step=step)
        if report is not None:
            self.rpc.call("save_report", report=report)

    def event(self, run_id, node, duration_ms):
        if run_id != self.job["id"]:
            raise PermissionError("检查任务范围不匹配")
        self.rpc.call("event", node=node, duration_ms=min(duration_ms, 120000))

    def model_result(self, run_id, call):
        if run_id != self.job["id"]:
            raise PermissionError("检查任务范围不匹配")
        result = self.rpc.call("model_begin")
        if "cached" in result:
            return result["cached"]
        if result.get("unknown"):
            raise ReviewNeedsReview("模型调用结果待核对")
        if result.get("execute") is not True:
            raise RuntimeError("模型检查未获授权")
        response = call()
        self.rpc.call("model_result", result=response)
        return response


class CloudReviewCheckpointer(BaseCheckpointSaver):
    """No local filesystem, provider credentials or request identity in config."""
    def __init__(self, rpc):
        super().__init__()
        self.rpc = rpc

    def _check(self, config):
        cfg = config["configurable"]
        if cfg.get("thread_id") != self.rpc.job["id"] or cfg.get("checkpoint_ns", ""):
            raise PermissionError("检查点范围不匹配")
        return cfg

    def _dump(self, value):
        kind, data = self.serde.dumps_typed(value)
        return {"type": kind, "data": base64.b64encode(data).decode("ascii")}

    def _load(self, value):
        return self.serde.loads_typed((value["type"], base64.b64decode(value["data"], validate=True)))

    def get_tuple(self, config):
        cfg = self._check(config)
        result = self.rpc.call("checkpoint_get", checkpoint_id=cfg.get("checkpoint_id"))
        if not result:
            return None
        def pointer(identifier):
            return {"configurable": {"thread_id": self.rpc.job["id"], "checkpoint_ns": "", "checkpoint_id": identifier}}
        return CheckpointTuple(pointer(result["id"]), self._load(result["checkpoint"]), result["metadata"],
            pointer(result["parent_id"]) if result.get("parent_id") else None,
            [(row["task_id"], row["channel"], self._load(row["value"])) for row in result["writes"]])

    def put(self, config, checkpoint, metadata, new_versions):
        cfg = self._check(config)
        self.rpc.call("checkpoint_put", body={"id": checkpoint["id"], "parent_id": cfg.get("checkpoint_id"),
            "checkpoint": self._dump(checkpoint), "metadata": metadata})
        return {"configurable": {"thread_id": cfg["thread_id"], "checkpoint_ns": "", "checkpoint_id": checkpoint["id"]}}

    def put_writes(self, config, writes, task_id, task_path=""):
        cfg = self._check(config)
        if writes:
            self.rpc.call("checkpoint_writes", checkpoint_id=cfg["checkpoint_id"], task_id=task_id,
                writes=[{"idx": WRITES_IDX_MAP.get(channel, index), "channel": channel, "value": self._dump(value)}
                        for index, (channel, value) in enumerate(writes)])


class ReviewMeterStore:
    def __init__(self, rpc):
        self.rpc = rpc

    def reserve_workbench_usage(self, user_id, request_id, model, amount, api_key_id=None):
        result = self.rpc.call("reserve", request_id=request_id, model=model, api_key_id=api_key_id)
        if result.get("insufficient_credits"):
            raise InsufficientCreditsError("AI 预审积分不足")
        return result

    def settle_workbench_usage(self, request_id, amount, provider_id):
        return self.rpc.call("settle", request_id=request_id, amount=amount, provider_id=provider_id)

    def refund_workbench_usage(self, request_id, reason="provider_error"):
        return self.rpc.call("refund", request_id=request_id)

    def record_workbench_interaction(self, user_id, project_id, request_id, model, *,
                                     input_text="", output_text="", status="started", error_code=None):
        return self.rpc.call("interaction", request_id=request_id, status=status,
                             input_text=input_text, output_text=output_text)


class CloudReviewModel:
    def __init__(self, rpc):
        self.rpc = rpc

    def accepts_input(self, content, schema):
        return len(json.dumps(content, ensure_ascii=False).encode()) + len(json.dumps(schema).encode()) < 60000

    def __call__(self, run, content, schema):
        from .review_prompt import SYSTEM_PROMPT
        meter = WorkbenchMeter(ReviewMeterStore(self.rpc), run["owner_id"],
                               int(os.environ["WOPALLY_WORKBENCH_API_KEY_ID"]), run["project_id"])
        meter.background_reconciliation = True
        # Bound settlement lookup without changing the site's billing policy.
        from .wopally_usage import WopallyUsage
        meter.usage = WopallyUsage(timeout=2, max_pages=1)
        candidate = OllamaAgent(None, None, base_url=os.environ["WOPALLY_MODEL_BASE_URL"],
            model=run["model"], provider="openai_compatible", api_key=os.environ["WOPALLY_WORKBENCH_API_KEY"],
            meter=meter, platform_transport=True)
        # Leave the rest of the 60s function for ledger and checkpoint writes.
        candidate._execution_deadline = time.monotonic() + 45
        try:
            response = candidate._post("/api/chat", {"model": run["model"], "format": "json",
                "messages": [{"role": "system", "content": SYSTEM_PROMPT + "\n输出结构：" + json.dumps(schema, ensure_ascii=False)},
                             {"role": "user", "content": json.dumps(content, ensure_ascii=False)}],
                "options": {"temperature": 0, "num_predict": 1400}}, timeout=42)
        except InsufficientCreditsError:
            return {"skip": "insufficient_credits"}
        usage = response.get("usage", {})
        return {"content": response["message"]["content"], "metrics": {
            "prompt_eval_count": usage.get("prompt_tokens", 0), "eval_count": usage.get("completion_tokens", 0),
            "latency_ms": usage.get("latency_ms", 0)}}


def valid_delivery(delivery_id, timestamp, signature, secret, *, now=None):
    if (len(secret) != 64 or not re.fullmatch(r"[a-f0-9-]{36}", delivery_id)
            or not re.fullmatch(r"[0-9]{10,12}", timestamp)
            or not re.fullmatch(r"[a-f0-9]{64}", signature)):
        return False
    if abs((time.time() if now is None else now) - int(timestamp)) > 90:
        return False
    expected = hmac.new(secret.encode(), f"stardust-review:{delivery_id}:{timestamp}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def run_delivery(store, delivery_id, *, rpc_factory=ReviewRPC, model_factory=CloudReviewModel):
    rpc = rpc_factory(store.project_url, store.publishable_key, os.environ["REVIEW_WORKER_SECRET"])
    job = rpc.call("claim", delivery_id=delivery_id)
    if not job:
        return {"status": "idle"}
    rpc.job = job
    if job["graph_version"] != CLOUD_GRAPH_VERSION:
        rpc.call("fail")
        return {"status": "failed"}
    try:
        graph = build_review_graph(LeasedReviewRuns(rpc), CloudReviewCheckpointer(rpc), model_factory(rpc),
            graph_version=CLOUD_GRAPH_VERSION, interrupt_after=NODES)
        config = {"configurable": {"thread_id": job["id"]}, "recursion_limit": 15}
        with tracing_context(enabled=False):
            previous = graph.get_state(config)
            inputs = None if previous.values else {"run_id": job["id"]}
            for _ in graph.stream(inputs, config, stream_mode="updates", durability="sync"):
                pass
            result = graph.get_state(config)
        rpc.call("yield" if result.next else "finish")
        return {"status": "pending" if result.next else "completed"}
    except Exception as exc:
        cause = exc.__cause__
        # Never log exception messages/tracebacks: upstream errors can contain
        # prompts, response bodies or credential-bearing URLs.
        logger.warning("Cloud review step failed; run_id=%s error=%s cause=%s http_status=%s",
                       job["id"], type(exc).__name__, type(cause).__name__,
                       getattr(cause, "code", None) if isinstance(cause, urllib.error.HTTPError) else None)
        # Failure can follow a committed response/checkpoint. Never invoke again
        # here; expiration and the persisted inference marker decide recovery.
        try:
            rpc.call("recover")
        except RuntimeError:
            pass
        return {"status": "needs_review"}
