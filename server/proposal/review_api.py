"""Opt-in local review API. Cloud must never fall back to temporary SQLite."""
from __future__ import annotations

import importlib.util
import os
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .review_runs import ReviewConflict, ReviewRuns
from .storage import Store


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=160)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    mode: Literal["rules", "model"] = "rules"


def register_review_api(app, get_store, is_cloud):
    def enabled():
        return (os.name == "posix" and not is_cloud() and not os.environ.get("VERCEL")
                and os.environ.get("LOCAL_REVIEW_ENABLED", "").lower() == "true"
                and importlib.util.find_spec("langgraph") is not None
                and importlib.util.find_spec("langgraph.checkpoint.sqlite") is not None)

    def service(project_id):
        if not enabled():
            raise HTTPException(404, "草稿预审尚未启用")
        store = get_store()
        if not isinstance(store, Store) or hasattr(store, "_rest"):
            raise HTTPException(404, "草稿预审尚未启用")
        if store.get_project(project_id) is None:
            raise HTTPException(404, "项目不存在")
        return ReviewRuns(store)

    @app.get("/api/reviews/capabilities")
    def capabilities():
        return {"enabled": bool(enabled()), "execution": "local_worker"}

    @app.get("/api/projects/{project_id}/reviews")
    def list_reviews(project_id: str):
        runs = service(project_id)
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
        model = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b") if payload.mode == "model" else ""
        if (payload.mode == "model" and not model) or len(model) > 160 or any(char.isspace() for char in model):
            raise HTTPException(503, "本地预审模型配置不正确")
        try:
            return runs.submit(project_id, payload.idempotency_key, payload.fingerprint, payload.mode, model)
        except ReviewConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except KeyError:
            raise HTTPException(404, "项目不存在") from None

    @app.get("/api/projects/{project_id}/reviews/{run_id}")
    def get_review(project_id: str, run_id: str):
        runs = service(project_id)
        result = runs.get(project_id, run_id)
        if result is None:
            raise HTTPException(404, "检查任务不存在")
        return result
