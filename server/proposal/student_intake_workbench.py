"""Source-grounded oral intake previews and explicit, per-record confirmation."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import secrets
import re

from pydantic import Field
from typing import Literal

from .materials import MaterialConflict
from .skills import redact_source_contacts
from .student_intake import (
    FIELDS, IntakeCandidate, IntakeFact, StrictModel, evidence_errors, extraction_messages,
    follow_up_questions,
)
from .student_intake_guard import ECON, NUM, PRIVACY_FIELDS, REFUSE, clauses, financial_scope, inspect_candidate

MAX_MESSAGES = 40
MAX_CHARS = 14000
ARCHIVE = "_student_intake_confirmed"
ARCHIVABLE = {"stated", "approximate", "planned", "negated"}
STATUS_LABELS = {"stated": "自述", "approximate": "模糊", "planned": "计划", "negated": "否定",
                 "unknown": "未知", "withheld": "拒答", "conflict": "冲突"}
PRIVATE = re.compile(r"身份证|学号|银行卡|住址|家庭地址|家庭住址|手机号|电话号码|详细地址|(?<!\d)\d{13,19}(?!\d)")
ECONOMIC = re.compile(r"年挣|年赚|家庭.{0,4}(?:工资|薪水|几口|低保)|(?:我家|家里).{0,5}(?:挣|赚|低保|几口)")
MASK = "【已略去】"
_LOCAL_KEY = secrets.token_bytes(32)


class IntakeConfirmation(StrictModel):
    confirmed: Literal[True]
    source_revision: str = Field(pattern="^[0-9a-f]{64}$")
    fact: IntakeFact
    model: str = Field(min_length=1, max_length=160)
    ticket: str = Field(pattern="^[0-9a-f]{64}$")


def confirmation_ticket(revision, fact, model):
    # The revision already binds the complete source and project; the HMAC
    # also binds the exact field, status, quotes and producing model.
    secret = os.environ.get("WORKBENCH_BACKEND_SECRET")
    key = secret.encode() if secret else _LOCAL_KEY
    body = json.dumps(["intake-confirm-v1", revision, fact.model_dump(), model],
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hmac.digest(key, body.encode(), "sha256").hex()


def read_sources(store, project_id):
    # Read the whole bounded history. A suffix could lose an earlier objection
    # or opposing claim. Ordinals start at the beginning of the conversation.
    messages = store.messages(project_id, limit=MAX_MESSAGES + 1)
    if len(messages) > MAX_MESSAGES:
        raise ValueError("项目对话超过 40 条，无法完整核对前文。请在事实面板逐项填写。")
    turns = [{"turn_id": f"u{i}", "role": "user", "content": m["content"]}
             for i, m in enumerate(messages, 1) if m["role"] == "user" and m["content"].strip()]
    if sum(len(t["content"]) for t in turns) > MAX_CHARS:
        raise ValueError("本次口述过长，无法完整核对。请在事实面板逐项填写。")
    snapshot = json.dumps([project_id, turns], ensure_ascii=False, separators=(",", ":"))
    revision = hashlib.sha256(snapshot.encode()).hexdigest()
    return turns, revision


def model_turns(turns):
    """Minimize the source before transport and preserve literal quote boundaries."""
    parts = clauses(turns)
    allowed = financial_scope(parts)
    refused = {field for field, marker in PRIVACY_FIELDS.items() if any(
        not p.external and marker.search(p.text) and REFUSE.search(p.text) for p in parts
    )}
    visible = []
    for turn in turns:
        content = turn["content"]
        spans = []
        for p in parts:
            if p.turn != turn["turn_id"]:
                continue
            financial = ECON.search(p.text) or ECONOMIC.search(p.text)
            privacy = PRIVATE.search(p.text) or redact_source_contacts(p.text) != p.text
            declined = any(PRIVACY_FIELDS[field].search(p.text) and NUM.search(p.text) for field in refused)
            if p.external or privacy or (financial and not allowed) or declined:
                spans.append((p.start, p.end))
        for start, end in reversed(spans):
            content = content[:start] + MASK + content[end:]
        if re.search(r"[\w]", content.replace(MASK, "")):
            visible.append({**turn, "content": content})
    return visible


def verified_review(raw, visible, original):
    review = inspect_candidate(raw, visible)
    if review["candidate"] is None:
        return None, "needs_manual_review"
    result = IntakeCandidate.model_validate(review["candidate"])
    # Synthetic masking tokens or joined-up redacted spans cannot be evidence.
    if evidence_errors(result, original) or any(MASK in e.quote for f in result.facts for e in f.evidence):
        return None, "needs_manual_review"
    return result, review["decision"]


def verified(raw, visible, original):
    return verified_review(raw, visible, original)[0]


def preview(model, turns, revision):
    visible = model_turns(turns)
    candidate = IntakeCandidate(schema_version="student-intake-v1", facts=[], follow_up_fields=[])
    decision = "pending_student_confirmation"
    if visible:
        # The platform uses DeepSeek JSON output mode. Local Ollama accepts
        # the full Pydantic schema as a constrained generation grammar.
        response = model._post("/api/chat", {
            "model": model.model, "stream": False, "messages": extraction_messages(visible),
            "format": "json" if model.provider == "openai_compatible" else IntakeCandidate.model_json_schema(),
            "options": {"temperature": 0, "num_predict": 2048},
        })
        message = response.get("message") or {}
        if message.get("finish_reason") in {"length", "content_filter"}:
            candidate, decision = None, "needs_manual_review"
        else:
            candidate, decision = verified_review(message.get("content", ""), visible, turns)
    return {
        "model": model.model if visible else None, "saved": False, "source_revision": revision,
        "decision": decision,
        "facts": [{**present(f), "ticket": confirmation_ticket(revision, f, model.model)}
                  for f in candidate.facts] if candidate else [],
        "questions": follow_up_questions(candidate) if candidate else [],
    }


def present(fact):
    return {"fact": fact.model_dump(), "label": FIELDS[fact.field][0],
            "status_label": STATUS_LABELS[fact.status], "can_confirm": fact.status in ARCHIVABLE}


def confirm(store, project, request):
    turns, revision = read_sources(store, project["id"])
    if revision != request.source_revision:
        raise MaterialConflict("对话已经更新，请重新提取并核对原话。")
    fact = request.fact
    if not hmac.compare_digest(request.ticket, confirmation_ticket(revision, fact, request.model)):
        raise ValueError("这项候选已变更，请重新提取。")
    raw = IntakeCandidate(schema_version="student-intake-v1", facts=[fact], follow_up_fields=[])
    checked = verified(raw.model_dump_json(), model_turns(turns), turns)
    if fact.status not in ARCHIVABLE or checked is None or fact not in checked.facts:
        raise ValueError("原话、状态或申请范围无法核实，请在对话中补充后再提取。")
    state = dict(project["state"])
    records = list(state.get(ARCHIVE, []))
    item = {**present(fact), "source_revision": revision, "model": request.model,
            "confirmed_at": datetime.now(timezone.utc).isoformat()}
    for row in records:
        if row["fact"] == item["fact"]:
            return {"confirmed": True, "record": row}
    state[ARCHIVE] = [*records, item]
    updated = store.update_project(project["id"], state=state, expected_revision=project["material_revision"])
    if not updated:
        raise MaterialConflict("项目已经变更，请刷新后核对。")
    return {"confirmed": True, "record": item}
