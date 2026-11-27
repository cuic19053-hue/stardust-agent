"""Shared handling for drafts whose review could not finish.

An incomplete review must not silently overwrite existing project content.
This check does not establish factual correctness when a review completes.
"""
from __future__ import annotations

import logging
from typing import Any

from .materials import MaterialConflict
from .sections import write_section

logger = logging.getLogger(__name__)


def review_incomplete(result: dict[str, Any]) -> bool:
    card = result.get("review_card")
    return (
        bool(result.get("review_incomplete"))
        or bool(result.get("grounding_issues"))
        or bool(result.get("revision_incomplete"))
        or result.get("status") == "review_failed"
        or (isinstance(card, dict) and card.get("review_status") == "failed")
    )


def mark_review_alternative(result: dict[str, Any], version_id: str | None = None) -> None:
    result["review_incomplete"] = True
    result["conflict_status"] = "user_edit_preserved"
    if result.get("grounding_issues"):
        subject = "项目数据、成果或来源" if result.get("grounding_kind") == "project" else "部分个人数字"
        result["notice"] = (f"草稿中的{subject}需要你核对，已保留为备选，未覆盖当前正文。"
                            if version_id is not None else f"草稿中的{subject}需要你核对，未覆盖当前正文。")
    elif result.get("revision_incomplete"):
        result["notice"] = ("后续修改未完成，已保留上一版草稿为备选，未覆盖当前正文。"
                            if version_id is not None else "后续修改未完成，未覆盖当前正文。")
    else:
        result["notice"] = ("本轮检查未完成，草稿已保留为备选，未覆盖当前正文。"
                            if version_id is not None else "本轮检查未完成，未覆盖当前正文。")
    if version_id is not None:
        result["alternative_version_id"] = version_id


def mark_review_save_uncertain(result: dict[str, Any]) -> None:
    """Keep the returned content visible when storing its alternate failed."""
    result["review_incomplete"] = True
    result["result_save_uncertain"] = True
    result["conflict_status"] = "user_edit_preserved"
    if result.get("grounding_issues"):
        subject = "项目数据、成果或来源" if result.get("grounding_kind") == "project" else "部分个人数字"
        result["notice"] = f"草稿中的{subject}需要你核对；草稿保存状态不明，请先复制或放入编辑器，不必重复生成。"
    elif result.get("revision_incomplete"):
        result["notice"] = "后续修改未完成，上一版草稿保存状态不明，请先复制或放入编辑器，不必重复生成。"
    else:
        result["notice"] = "本轮检查未完成，草稿保存状态不明；请先复制或放入编辑器，不必重复生成。"


def persist_generated_section(store, project, section_id, result, *, section_changed=False):
    """Apply a finished draft once, or retain it for review with the original text intact."""
    current = project["sections"].get(section_id, "")
    if result.get("requires_student_input") and current.strip() and not section_changed:
        return
    content = result.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("未生成可保存的章节正文。")

    incomplete = review_incomplete(result)
    alternative = incomplete or section_changed
    if not alternative:
        try:
            revision = write_section(store, project, section_id, content)
        except MaterialConflict:
            alternative = True
        except Exception:
            # A transport error can occur after commit. Do not reissue a write
            # or turn the already-finished model call into a regeneration request.
            logger.warning("Could not confirm the generated chapter write")
            result.update(conflict_status="user_edit_preserved", result_save_uncertain=True,
                          notice="草稿保存结果待确认，请先核对最新正文。本次生成的内容可复制保留。")
            return
        else:
            result.update(material_applied=True, material_revision=revision)

    try:
        version = store.save_section_version(
            project["id"], section_id, content, source="ai_generated_alternative" if alternative else "ai_generated",
            review_card=result.get("review_card"),
        )
    except Exception:
        logger.warning("Could not record the generated chapter in version history")
        if incomplete:
            mark_review_save_uncertain(result)
        elif alternative:
            result.update(conflict_status="user_edit_preserved", result_save_uncertain=True,
                          notice="正文已有更新，备选版本保存状态不明，请先复制本次生成的草稿。")
        else:
            result["notice"] = "正文已保存，历史版本暂未保存。"
    else:
        if incomplete:
            mark_review_alternative(result, str(version["id"]))
        elif alternative:
            result.update(conflict_status="user_edit_preserved", alternative_version_id=version["id"],
                          notice="项目在生成期间发生变化，生成草稿已保留在历史版本中。")
