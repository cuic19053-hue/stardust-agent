"""Join confirmed chat drafts and chapter generation to the same review lifecycle."""
from __future__ import annotations

import logging
import os

from .review_runs import ReviewRuns, digest

logger = logging.getLogger(__name__)


def after_draft_saved(store, project_id, origin):
    """Queue a free rule check once per saved snapshot; paid review stays opt-in.

    Saving text is already committed. A queue outage must not pretend that save
    failed (which could cause the caller to repeat a replacement).
    """
    try:
        project = store.get_project(project_id)
        if not project or (project.get("state") or {}).get("skill_id", "innovation-research") != "innovation-research":
            return {"status": "disabled"}
        if hasattr(store, "_rest"):
            from .cloud_review import CloudReviewRuns, cloud_review_enabled
            if not cloud_review_enabled():
                return {"status": "disabled"}
            runs = CloudReviewRuns(store)
            listing = runs.listing(project_id)
            fingerprint, items = listing.get("current_fingerprint"), listing.get("items", [])
        else:
            if os.environ.get("LOCAL_REVIEW_ENABLED", "").lower() != "true":
                return {"status": "disabled"}
            runs = ReviewRuns(store)
            fingerprint, items = runs.current(project_id), runs.list(project_id)
        if not fingerprint:
            return {"status": "unavailable"}
        matching = next((r for r in items if r["fingerprint"] == fingerprint and r["mode"] == "rules"
                         and r["status"] in {"pending", "running", "completed"}), None)
        if matching:
            return {"status": matching["status"], "run_id": matching["id"]}
        parent = next((r for r in items if r["status"] == "completed" and r["fingerprint"] != fingerprint), None)
        run = runs.submit(project_id, "saved:" + digest({"fingerprint": fingerprint, "origin": origin}),
                          fingerprint, "rules", "", parent_id=parent["id"] if parent else None, origin=origin)
        return {"status": run["status"], "run_id": run["id"]}
    except (ValueError, KeyError, RuntimeError):
        logger.warning("Post-save rule review unavailable", extra={"origin": origin})
        return {"status": "unavailable", "notice": "正文已保存，自动检查暂不可用，可稍后在预审面板重试。"}
