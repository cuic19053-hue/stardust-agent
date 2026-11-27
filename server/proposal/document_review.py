"""Publish reviewed text together with the index built from that text."""
from .materials import MaterialConflict


class DocumentReviewUnavailable(RuntimeError):
    """The store cannot yet publish reviewed documents atomically."""


def review_document(store, retriever, document, text, confirmed, expected_revision=None):
    if document["kind"] == "template":
        raise ValueError("Word/PDF 模板不作为项目事实材料索引。")
    if confirmed and not text.strip():
        raise ValueError("识别文字为空，请先补充再确认。")
    if len(text) > 200_000:
        raise ValueError("材料校对文字最多支持 200,000 个字符。")
    revision = document.get("review_revision")
    if revision is None:
        raise DocumentReviewUnavailable("材料校对暂时不可用，请稍后重试。")
    if expected_revision is not None and revision != expected_revision:
        raise MaterialConflict("材料已在其他页面更新，请核对最新文字后再提交。")

    status = "ready" if confirmed else document["extraction_status"]
    chunks, mode = [], "not_indexed"
    if confirmed:
        try:
            chunks, mode = retriever.prepare_document_index(document["project_id"], text)
        except MaterialConflict:
            raise
        except Exception:
            # Retain the reviewed text for retry and revoke the old index only if this review still owns the document.
            store.apply_document_review(document["id"], revision, text, status, False, [])
            raise
        if not chunks:
            raise ValueError("材料中没有可索引的正文，请核对后再确认。")
    saved = store.apply_document_review(document["id"], revision, text, status, confirmed, chunks)
    return saved, len(chunks), mode
