from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from proposal.chat_drafts import require_export_content
from proposal.exporter import ensure_grant_income_artifact_safe, export_input_fingerprint, export_project
from proposal.materials import MaterialConflict


logger = logging.getLogger(__name__)
WORD_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def word_digest(sha256: bytes) -> str:
    return f"sha-256=:{base64.b64encode(sha256).decode('ascii')}:"


class ExportInput(BaseModel):
    allow_empty_draft: bool = False


def _require_current_export(
    store, project: dict[str, Any], current: dict[str, Any], content: bytes,
) -> None:
    state_data = project["state"]
    if (not state_data.get("exported_docx_fingerprint")
            or state_data["exported_docx_fingerprint"] != export_input_fingerprint(store, current)
            or state_data.get("exported_docx_sha256") != hashlib.sha256(content).hexdigest()):
        raise HTTPException(409, "项目内容已更新或导出版本无法确认，请保存最新修改后重新导出。")


def register_export_api(app: FastAPI, get_store, load_project, validate_project) -> None:
    @app.post("/api/projects/{project_id}/export")
    def export_docx(project_id: str, payload: ExportInput | None = None) -> dict[str, Any]:
        store = get_store()
        project = load_project(project_id)
        source_fingerprint = export_input_fingerprint(store, project)
        source_revision = project.get("material_revision", 0)
        try:
            require_export_content(project, bool(payload and payload.allow_empty_draft))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        preflight = validate_project(project_id)
        output_path = store.files_dir / project_id / f"export-{secrets.token_hex(16)}.docx"
        try:
            result = export_project(store, project_id, output_path)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Word 草稿生成失败：{exc}") from exc
        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error", "Word 导出失败"))
        if result.get("template_status") == "needs_review":
            preflight = dict(preflight)
            preflight["passed"] = False
            preflight["consistency_warnings"] = [*preflight.get("consistency_warnings", []),
                "学校模板尚有未填写内容或无法自动映射，请在 Word 中检查并补齐。"]

        # 排版硬化 + 质检：35 个子 skill 各自手写 docx，表格防拆/图注绑定/页码/大纲
        # 的覆盖率是 0，在这一层统一补齐，用户拿到的每一份都过同一把尺子。
        try:
            from utils.docx_layout_fix import fix as _harden
            from utils.layout_audit import audit as _audit

            # Generic hardening changes pagination/styles; keep school-provided
            # templates intact and only audit their generated copy.
            if not result.get("used_original_template"):
                import shutil
                import tempfile
                from docx import Document
                # Harden a separate copy. A crash, broken output, or failed write
                # must not destroy the Word document we already generated.
                with tempfile.TemporaryDirectory(prefix="layout-", dir=output_path.parent) as temp_dir:
                    candidate = Path(temp_dir) / "proposal.docx"
                    shutil.copyfile(output_path, candidate)
                    _harden(candidate)
                    Document(candidate)  # Reject an unreadable copy before replacing the original.
                    os.replace(candidate, output_path)
            _qa = _audit(Path(output_path), render=False)
            result["layout"] = {
                "score": _qa.score,
                "fails": [{"rule": i.rule, "detail": i.detail, "fix": i.fix} for i in _qa.fails],
                "warns": [{"rule": i.rule, "detail": i.detail} for i in _qa.warns],
            }
        except Exception as exc:  # 质检失败不影响导出
            logger.warning("排版硬化失败：%s", exc)
            result["layout"] = {"score": None, "error": str(exc)[:200]}

        current = load_project(project_id)
        if (current.get("material_revision", 0) != source_revision
                or result.get("source_fingerprint") != source_fingerprint
                or export_input_fingerprint(store, current) != source_fingerprint):
            output_path.unlink(missing_ok=True)
            raise HTTPException(409, "导出期间项目内容已更新，请保存最新修改后重新导出。")
        state_data = dict(current["state"])
        state_data.update(exported_docx_fingerprint=source_fingerprint,
                          exported_docx_sha256=hashlib.sha256(output_path.read_bytes()).hexdigest(),
                          exported_docx_local_name=output_path.name)
        storage_path = None
        if hasattr(store, "persist_generated_file"):
            storage_path = store.persist_generated_file(
                project_id,
                output_path.name,
                output_path,
                WORD_CONTENT_TYPE,
            )
            state_data["exported_docx_storage_path"] = storage_path
        def discard_unpublished() -> None:
            try:
                output_path.unlink(missing_ok=True)
                if storage_path and hasattr(store, "discard_generated_export"):
                    store.discard_generated_export(project_id, storage_path)
            except (OSError, RuntimeError):
                logger.warning("Could not remove unpublished Word export; project_id=%s", project_id)

        try:
            saved = store.update_project(project_id, expected_revision=current.get("material_revision", 0), state=state_data)
        except MaterialConflict:
            # Only clean up a rejected publication. A lost acknowledgement may conceal a successful write.
            discard_unpublished()
            raise
        if saved is None:
            discard_unpublished()
            raise HTTPException(404, "项目不存在")
        previous_name = current["state"].get("exported_docx_local_name", "")
        previous_storage = current["state"].get("exported_docx_storage_path", "")
        try:
            if re.fullmatch(r"export-[a-f0-9]{32}\.docx", previous_name) and previous_name != output_path.name:
                (output_path.parent / previous_name).unlink(missing_ok=True)
            if (previous_storage and previous_storage != state_data.get("exported_docx_storage_path")
                    and hasattr(store, "discard_generated_export")):
                store.discard_generated_export(project_id, previous_storage)
        except (OSError, RuntimeError):
            # Publishing succeeded; old-file cleanup must not hide the new result.
            logger.warning("Previous Word export cleanup deferred; project_id=%s", project_id)
        result["download_url"] = f"/api/projects/{project_id}/download"
        result["download_digest"] = word_digest(bytes.fromhex(state_data["exported_docx_sha256"]))
        result["validation"] = preflight
        return result

    @app.get("/api/projects/{project_id}/download")
    def download_docx(project_id: str) -> Response:
        store = get_store()
        project = load_project(project_id)
        download_name = f"{project.get('state', {}).get('skill_name') or '申报材料'}草稿.docx"
        state_data = project["state"]
        storage_path = state_data.get("exported_docx_storage_path")
        if storage_path and hasattr(store, "read_generated_file"):
            content = store.read_generated_file(storage_path)
            try:
                ensure_grant_income_artifact_safe(store, project, content)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=f"{exc} 请处理后重新导出。") from None
            _require_current_export(store, project, load_project(project["id"]), content)
            return Response(
                content=content,
                media_type=WORD_CONTENT_TYPE,
                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(download_name)}",
                         "Repr-Digest": word_digest(bytes.fromhex(state_data["exported_docx_sha256"])),
                         "Cache-Control": "private, no-store"},
            )
        name = state_data.get("exported_docx_local_name", "")
        if name and not re.fullmatch(r"export-[a-f0-9]{32}\.docx", name):
            raise HTTPException(409, "导出文件版本无法确认，请重新导出。")
        path = store.files_dir / project_id / (name or "申报书草稿.docx")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="请先导出 Word 草稿")
        try:
            ensure_grant_income_artifact_safe(store, project, path)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=f"{exc} 请处理后重新导出。") from None
        content = path.read_bytes()
        _require_current_export(store, project, load_project(project["id"]), content)
        return Response(
            content=content,
            media_type=WORD_CONTENT_TYPE,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(download_name)}",
                     "Repr-Digest": word_digest(bytes.fromhex(state_data["exported_docx_sha256"])),
                     "Cache-Control": "private, no-store"},
        )
