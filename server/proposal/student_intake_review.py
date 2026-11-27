"""Source filtering and candidate review shared by the workbench and browser runtime."""
import re

from .source_contacts import redact_source_contacts
from .student_intake import FIELDS, IntakeCandidate, evidence_errors
from .student_intake_guard import (
    ECON, MASK, PRIVACY_FIELDS, REFUSE, SENTENCE_BREAK, clauses, contains_disclosed_number, financial_scope, inspect_candidate,
)

ARCHIVABLE = {"stated", "approximate", "planned", "negated"}
STATUS_LABELS = {"stated": "自述", "approximate": "模糊", "planned": "计划", "negated": "否定",
                 "unknown": "未知", "withheld": "拒答", "conflict": "冲突"}
PRIVATE = re.compile(r"身份证|学号|银行卡|住址|家庭地址|家庭住址|手机号|电话号码|详细地址|(?<!\d)\d{13,19}(?!\d)")
ECONOMIC = re.compile(r"年挣|年赚|家庭.{0,4}(?:工资|薪水|几口|低保)|(?:我家|家里).{0,5}(?:挣|赚|低保|几口)")
INTAKE_TOPIC = re.compile(
    r"^(?:我(?:本人|自己|们)?(?:的|正在|计划|准备|打算|想|要)?\s*)?"
    r"(?:申请|申报|办理|绩点|GPA|专业|学校|学院|排名|项目|团队|志愿时长)", re.I,
)


def model_turns(turns):
    """Minimize the source before transport and preserve literal quote boundaries."""
    parts = clauses(turns)
    allowed = financial_scope(parts)
    refused = {field for field, marker in PRIVACY_FIELDS.items() if any(
        not p.external and marker.search(p.text) and REFUSE.search(p.text) for p in parts
    )}
    visible = []
    withheld_context = False
    for turn in turns:
        content = turn["content"]
        spans = []
        # Keep comma-separated descriptions and their amounts together. A new
        # sentence or an explicitly named intake topic starts a separate group.
        group = []
        groups = []
        for p in parts:
            if p.turn != turn["turn_id"]:
                continue
            starts_topic = INTAKE_TOPIC.match(p.text.strip())
            if group and (SENTENCE_BREAK.search(content[group[-1].end:p.start]) or starts_topic
                          or INTAKE_TOPIC.match(group[-1].text.strip())):
                groups.append(group)
                group = []
            group.append(p)
        if group:
            groups.append(group)
        for group in groups:
            phrase = content[group[0].start:group[-1].end]
            financial = ECON.search(phrase) or ECONOMIC.search(phrase)
            refused_topic = any(PRIVACY_FIELDS[field].search(phrase) for field in refused)
            has_value = contains_disclosed_number(phrase)
            # A bare answer can follow the field label after a pause or in the next turn.
            continuation = withheld_context and has_value and not INTAKE_TOPIC.match(phrase.strip())
            withheld_context = (bool(financial) and not allowed) or refused_topic or continuation
            if (financial and not allowed) or (refused_topic and has_value) or continuation:
                spans.append((group[0].start, group[-1].end))
                continue
            for p in group:
                if p.external or PRIVATE.search(p.text) or redact_source_contacts(p.text) != p.text:
                    spans.append((p.start, p.end))
        for start, end in reversed(spans):
            content = content[:start] + MASK + content[end:]
        if re.search(r"[\w]", content.replace(MASK, "")):
            visible.append({**turn, "content": content})
    return visible


def verified_review(raw, visible, original):
    review = inspect_candidate(raw, visible, original)
    if review["candidate"] is None:
        return None, "needs_manual_review"
    result = IntakeCandidate.model_validate(review["candidate"])
    # Synthetic masking tokens or joined-up redacted spans cannot be evidence.
    if evidence_errors(result, original) or any(MASK in e.quote for f in result.facts for e in f.evidence):
        return None, "needs_manual_review"
    return result, review["decision"]


def verified(raw, visible, original):
    return verified_review(raw, visible, original)[0]


def present(fact):
    return {"fact": fact.model_dump(), "label": FIELDS[fact.field][0],
            "status_label": STATUS_LABELS[fact.status], "can_confirm": fact.status in ARCHIVABLE}
