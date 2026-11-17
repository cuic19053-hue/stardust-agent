"""Opt-in local review API. Cloud must never fall back to temporary SQLite."""
from __future__ import annotations

import importlib.util
import os
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .review_runs import ReviewConflict, ReviewRuns
from .storage import Store
from .storage import InsufficientCreditsError


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=160)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    mode: Literal["rules", "model"] = "rules"
    parent_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


def register_review_api(app, get_store, is_cloud, platform_ready=lambda: False):
    def enabled():
        if is_cloud():
            from .cloud_review import cloud_review_enabled
            return cloud_review_enabled()
        return (os.name == "posix" and not is_cloud() and not os.environ.get("VERCEL")
                and os.environ.get("LOCAL_REVIEW_ENABLED", "").lower() == "true"
                and importlib.util.find_spec("langgraph") is not None
                and importlib.util.find_spec("langgraph.checkpoint.sqlite") is not None)

    def service(project_id):
        if not enabled():
            raise HTTPException(404, "草稿预审尚未启用")
        store = get_store()
        if is_cloud() and hasattr(store, "_rest"):
            from .cloud_review import CloudReviewRuns
            if store.get_project(project_id) is None:
                raise HTTPException(404, "项目不存在")
            return CloudReviewRuns(store)
        if not isinstance(store, Store) or hasattr(store, "_rest"):
            raise HTTPException(404, "草稿预审尚未启用")
        if store.get_project(project_id) is None:
            raise HTTPException(404, "项目不存在")
        return ReviewRuns(store)

    @app.get("/api/reviews/capabilities")
    def capabilities():
        cloud = bool(is_cloud())
        ready = bool(enabled())
        if cloud and ready:
            from .cloud_review import ReviewRPC
            store = get_store()
            try:
                health = ReviewRPC(store.project_url, store.publishable_key, os.environ["REVIEW_WORKER_SECRET"]).call("health")
                ready = bool(health and health.get("enabled"))
            except RuntimeError:
                ready = False
        return {"enabled": ready, "execution": "cloud_queue" if cloud else "local_worker",
                "ai_available": bool(platform_ready()) if cloud else True,
                "ai_label": "平台 AI 预审（预留 2 积分，按实际用量结算）" if cloud else "同时检查论证与事实依据（使用本地模型）"}

    @app.get("/api/projects/{project_id}/reviews")
    def list_reviews(project_id: str):
        runs = service(project_id)
        if hasattr(runs, "listing"):
            try:
                return runs.listing(project_id)
            except RuntimeError:
                raise HTTPException(503, "云端预审暂不可用，请稍后重试。") from None
        try:
            fingerprint, reason = runs.current(project_id), ""
        except ValueError as exc:
            fingerprint, reason = None, str(exc)
        except KeyError:
            raise HTTPException(404, "项目不存在") from None
        return {"items": runs.list(project_id), "current_fingerprint": fingerprint, "unavailable_reason": reason}

    @app.post("/api/projects/{project_id}/reviews", status_code=202)
    def submit_review(project_id: str, payload: ReviewInput):
        runs = service(project_id)
        if is_cloud() and payload.mode == "model" and not platform_ready():
            raise HTTPException(503, "平台 AI 预审暂不可用，可以先运行规则检查。")
        model = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b") if payload.mode == "model" else ""
        if (payload.mode == "model" and not model) or len(model) > 160 or any(char.isspace() for char in model):
            raise HTTPException(503, "本地预审模型配置不正确")
        try:
            return runs.submit(project_id, payload.idempotency_key, payload.fingerprint, payload.mode, model,
                               parent_id=payload.parent_id)
        except ReviewConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except InsufficientCreditsError as exc:
            raise HTTPException(402, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except KeyError:
            raise HTTPException(404, "项目不存在") from None
        except RuntimeError:
            raise HTTPException(503, "预审提交结果未确认，请稍后查看原任务。") from None

    @app.get("/api/projects/{project_id}/reviews/{run_id}")
    def get_review(project_id: str, run_id: str):
        runs = service(project_id)
        try:
            result = runs.get(project_id, run_id)
        except RuntimeError:
            raise HTTPException(503, "云端预审状态暂不可用。") from None
        if result is None:
            raise HTTPException(404, "检查任务不存在")
        return result

    @app.post("/api/reviews/internal/tick")
    def review_tick(payload: dict, request: Request):
        if not is_cloud() or not enabled():
            raise HTTPException(404, "云端检查服务尚未启用")
        from .cloud_review import run_delivery, valid_delivery
        delivery = str(payload.get("delivery_id") or "")
        if not valid_delivery(delivery, request.headers.get("x-review-timestamp", ""),
                              request.headers.get("x-review-signature", ""), os.environ["REVIEW_WORKER_SECRET"]):
            raise HTTPException(403, "检查调度签名无效")
        try:
            return run_delivery(get_store(), delivery)
        except RuntimeError:
            raise HTTPException(503, "检查调度暂不可用") from None

    @app.post("/api/admin/configure-review-scheduler")
    def configure_review_scheduler(payload: dict, request: Request):
        import hmac
        expected = os.environ.get("REVIEW_CONFIGURE_SECRET", "")
        if not is_cloud() or len(expected) != 64 or not hmac.compare_digest(request.headers.get("x-review-ops-token", ""), expected):
            raise HTTPException(403, "无权配置检查调度")
        if type(payload.get("enabled")) is not bool:
            raise HTTPException(422, "请指定调度是否启用")
        from .cloud_review import configure_scheduler
        try:
            return configure_scheduler(get_store(), payload["enabled"])
        except RuntimeError:
            raise HTTPException(503, "检查调度配置失败") from None
