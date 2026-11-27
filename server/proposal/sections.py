"""Chapter edits against an exact project snapshot."""
from typing import Any

from .materials import MaterialConflict

MAX_MANUAL_SECTION_LENGTH = 30_000
SECTION_LENGTH_NOTICE = f"每章最多保存 {MAX_MANUAL_SECTION_LENGTH:,} 个字符，请缩短或分章后再保存。"


class SectionProjectMissing(LookupError):
    """The project was deleted while saving this chapter."""


def check_section_revision(project: dict[str, Any], section_id: str, expected_revision: int | None = None,
                           base_content: str | None = None) -> None:
    if expected_revision is not None and expected_revision != project["material_revision"]:
        raise MaterialConflict("项目已变化，请核对最新版本后再保存。")
    if base_content is not None and project["sections"].get(section_id, "") != base_content:
        raise MaterialConflict("该章节已在其他页面修改，请核对版本，未覆盖您的输入。")


def write_section(store, project: dict[str, Any], section_id: str, content: str) -> int:
    saved = store.update_project(project["id"], expected_revision=project["material_revision"],
                                 sections={**project["sections"], section_id: content})
    if saved is None:
        raise SectionProjectMissing("找不到这个项目。")
    return saved["material_revision"]


def save_manual_section(store, project: dict[str, Any], section_id: str, content: str,
                        expected_revision: int | None = None, base_content: str | None = None) -> dict[str, Any]:
    if not isinstance(section_id, str) or not section_id.strip() or len(section_id) > 80:
        raise ValueError("章节标识无效。")
    if not isinstance(content, str):
        raise ValueError("章节正文须为文本。")
    if len(content) > MAX_MANUAL_SECTION_LENGTH:
        raise ValueError(SECTION_LENGTH_NOTICE)
    check_section_revision(project, section_id, expected_revision, base_content)
    revision = write_section(store, project, section_id, content)
    return {"success": True, "section_id": section_id, "character_count": len(content),
            "material_revision": revision}


def visible_section_text(project: dict[str, Any]) -> dict[str, str]:
    """Exclude chapters deliberately retired from the directory, while preserving legacy direct saves."""
    sections = project.get("sections") or {}
    archived = {item["id"] for item in project.get("state", {}).get("_archived_outline", [])}
    if not archived:
        return sections
    active = {item["id"] for item in project.get("outline", [])}
    return {key: text for key, text in sections.items() if key not in archived or key in active}
