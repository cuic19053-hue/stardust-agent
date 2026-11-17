"""Conservative evidence gates. Unknown entailment stays unknown, never 'verified'."""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal

from .retrieval import _tokens
from .review_evidence import PLAN, POLICY, claim_type

MONEY = re.compile(r"([\d,]+(?:\.\d+)?)\s*(万元|万|元)")
TOTAL = re.compile(r"(?:预算合计|预算总额|总预算|总经费|经费合计|申请经费|合计|总额)(?:为|是|[：:])?\s*([\d,]+(?:\.\d+)?)\s*(万元|万|元)")
INJECTION = re.compile(r"忽略.{0,20}指令|返回空问题|请在.{0,20}审批|本段文字就是.{0,12}凭据")


def amount(match):
    return Decimal(match.group(1).replace(",", "")) * (10000 if "万" in match.group(2) else 1)


def budget_sum(text):
    """Only calculate an explicit total followed by at least two named fee items."""
    total = TOTAL.search(text)
    if not total:
        return None
    items = list(re.finditer(r"[\u4e00-\u9fffA-Za-z]{1,16}(?:费|费用|支出)\s*([\d,]+(?:\.\d+)?)\s*(万元|万|元)", text))
    items = [item for item in items if item.start() >= total.end()]
    if len(items) < 2 or re.search(r"其中|例如|包括但不限|部分|另有|预留|等费用", text):
        return None  # A partial list cannot establish an arithmetic error.
    return amount(total), sum((amount(item) for item in items), Decimal(0))


def policy_applicable(project, hit, quote):
    # 'User confirmed' establishes transcription, not the authority of a policy.
    if hit.get("source_kind") != "official" or INJECTION.search(hit["content"]):
        return False
    school = str(project.get("school") or "").strip()
    if not school or school not in hit["content"]:
        return False
    years = set(re.findall(r"20\d{2}年?", quote))
    source_years = {y.rstrip("年") for y in re.findall(r"20\d{2}年?", hit["content"])}
    if not years or not {y.rstrip("年") for y in years} <= source_years:
        return False
    urls = re.findall(r"\[来源链接\]\s*(https://\S+)", hit["content"])
    sources = (project.get("state") or {}).get("official_sources", [])
    return any(isinstance(s, dict) and s.get("url") in urls for s in sources)


def related(a, b):
    # Numeric overlap alone never proves that two statements concern the same thing.
    words = {t for t in _tokens(a) if not re.fullmatch(r"[\d.]+", t)}
    ignored = _tokens("本项目学校研究计划已经完成尚未开展没有需要要求")
    return len((words - ignored) & (_tokens(b) - ignored)) >= 2


def citation(hit, quote=None):
    return {"id": hit["id"], "source_name": hit["source_name"], "source_ref": hit["source_ref"],
            "quote": quote or hit["content"][:600]}


def evidence_checks(snapshot, claims, evidence):
    """Emit independently reproducible checks even if the model omits a problem."""
    project = snapshot["project"]
    by_id = {e["id"]: e for e in evidence}
    issues = []
    for claim in claims:
        quote, sid = claim["quote"], claim["section_id"]
        hits = [by_id[i] for i in claim.get("evidence_ids", []) if i in by_id]
        if claim["type"] == "policy" and not any(policy_applicable(project, h, quote) for h in hits):
            # Only challenge an assertion of an applicable rule/approval, not a future task.
            if re.search(r"根据.{0,35}通知|必须|不得申报|(?:已|已经).{0,12}(?:同意|审批|资格)|同意立项", quote) and not re.search(r"(?:拟|计划|待|将).{0,8}(?:申请|办理|取得)", quote):
                issues.append(_issue(sid, quote, "policy_source", "该学校规定、资格或审批结论缺少本校适用年度的正式依据。",
                    "补充本校当年通知及对应条款；审批结论须提供真实审批记录，不能由材料中的指令代填。", [], "insufficient"))
        if (claim["type"] == "completed_result" and re.search(r"实验|测试|实测|采集|样本|准确率|召回率|F1", quote)) or (re.search(r"(?:准确率|召回率|F1).{0,8}(?:达到|达|为).{0,4}\d", quote) and not PLAN.search(quote)):
            relevant = [h for h in hits if related(quote, h["content"]) and not INJECTION.search(h["content"])]
            denials = [h for h in relevant if re.search(r"尚未.{0,15}(?:实验|采集|授权)|未开展.{0,12}实验|没有实测", h["content"])]
            numbers = set(re.findall(r"\d+(?:\.\d+)?", quote))
            support = [h for h in relevant if not denials and (quote in h["content"] or
                (numbers and numbers <= set(re.findall(r"\d+(?:\.\d+)?", h["content"]))
                 and re.search(r"(?:已|已经).{0,15}(?:完成|采集|实验|实测)|(?:准确率|召回率|F1).{0,8}(?:达到|为)", h["content"])
                 and not PLAN.search(h["content"]))) ]
            if not support:
                issues.append(_issue(sid, quote, "result_evidence", "已完成的实验或实测结果尚缺可核对依据。" + ("已确认进展仍记载尚未开展相关工作。" if denials else ""),
                    "补充同一实验的原始记录、样本与指标计算；尚属计划的内容请改为计划或预期。", [citation(h) for h in denials[:2]], "insufficient"))
    for section in project.get("outline", []):
        sid = section["id"]
        text = str(project.get("sections", {}).get(sid) or "")
        if "预算" not in section.get("title", "") and "budget" not in sid:
            continue
        sums = budget_sum(text)
        if sums and sums[0] != sums[1]:
            issues.append(_issue(sid, text[:240], "budget_arithmetic", f"预算分项合计为{sums[1]:g}元，与所列总额{sums[0]:g}元不一致。",
                "核对分项或总额，保留真实估算依据。", [], "calculated"))
    merged = {}
    for issue in issues:
        key = (issue["section_id"], issue["check_code"])
        if key in merged:
            old = merged[key]
            combined = old["draft_quote"] + issue["draft_quote"]
            if issue["draft_quote"] not in old["draft_quote"] and len(combined) <= 240 and combined in project["sections"].get(issue["section_id"], ""):
                old["draft_quote"] = combined
        else:
            merged[key] = issue
    return list(merged.values())


def _issue(sid, quote, code, problem, suggestion, citations, support):
    return {"section_id": sid, "draft_quote": quote, "check_code": code,
            "kind": "needs_evidence" if support == "insufficient" else "contradiction",
            "problem": problem, "suggestion": suggestion, "citations": citations,
            "origin": "rule", "verification": "rule_result", "support_status": support}


def verify_model_issue(project, issue, evidence):
    """Return a safely worded issue and warnings; never launder an unsupported rule."""
    section = issue["section_id"]
    body = str(project.get("sections", {}).get(section) or "")
    if section not in {s["id"] for s in project["outline"]} or issue["draft_quote"] not in body:
        return None, ["已过滤无法定位到指定草稿的模型意见。"]
    quote = issue["draft_quote"]
    warnings, valid = [], []
    for c in issue["citations"]:
        hit = evidence.get(c["id"])
        if hit is None or c["quote"] not in hit["content"]:
            warnings.append("已过滤不存在或引文不匹配的模型来源。")
        else:
            valid.append((hit, c["quote"]))
    opinion = issue["problem"] + issue["suggestion"]
    status = claim_type(quote)
    # A model can quote just the first sentence and omit the following denial.
    if status == "fact" and claim_type(body) in {"plan", "not_completed"}:
        status = claim_type(body)
    if status in {"plan", "not_completed"} and re.search(r"缺乏|缺少|未提供|没有|应包括|应提供|需提供", opinion) and re.search(r"已完成.{0,20}(?:实验|结果|采集)|实测结果|实测数据采集的证据|没有完成的对比实验", opinion):
        return None, [*warnings, "未将明确计划或尚未开展的工作，判为缺少已完成成果。"]
    if issue["kind"] == "school_requirement" or re.search(r"学校.{0,15}(?:必须|规定|要求|批准)|(?:必须|应当).{0,12}学校批准", opinion):
        valid_policy = [(h, q) for h, q in valid if policy_applicable(project, h, quote) and related(quote, q)]
        if not valid_policy:
            if not POLICY.search(quote):
                return None, [*warnings, "已过滤没有适用来源、且由模型自行提出的学校硬性要求。"]
            return {**issue, "kind": "needs_evidence", "check_code": "policy_source",
                "problem": "学校要求待核对：现有来源不能确认该条规定适用于本校本次申报。",
                "suggestion": "请补充本校适用年度的正式通知及对应条款后再判断。", "citations": [],
                "origin": "model", "verification": "needs_human_review", "support_status": "insufficient"}, [*warnings, "模型提出的学校要求缺少有效来源，已改为资料核对项。"]
    sums = budget_sum(body)
    if re.search(r"加总|相加|之和|算术|计算错误|分项合计", opinion) and sums and sums[0] == sums[1]:
        return None, [*warnings, "已过滤与实际预算加总结果不符的模型意见。"]
    if claim_type(quote) == "plan" and re.search(r"虚构|伪造|已完成|实测结果", opinion):
        return None, [*warnings, "已过滤将明确计划或未完成状态当作已完成成果的意见。"]
    if claim_type(quote) in {"plan", "not_completed"} and re.search(r"不一致|不符|矛盾", opinion):
        progress = [q for _, q in valid if re.search(r"尚未.{0,15}(?:授权|实验|采集)|未开展.{0,12}实验|没有实测", q)]
        if progress:
            return None, [*warnings, "未来计划或未完成状态与尚未开展的进展可以同时成立，已过滤据此提出的冲突意见。"]
    semantic = [(h, q) for h, q in valid if related(quote, q) and not INJECTION.search(q)]
    # Exact quotation is observable. General semantic entailment is not asserted.
    result = {**issue, "citations": [{**citation(h, q), "support_status": "related_unverified" if (h, q) in semantic else "quote_only"}
                                    for h, q in valid], "origin": "model",
              "verification": "needs_human_review", "support_status": "unverified"}
    if re.search(r"矛盾|不符|不一致|不(?:完全)?对应|证据显示", opinion):
        result["support_status"] = "insufficient"
    if "预算" in quote and re.search(r"预算|总额|分项", opinion):
        result["check_code"] = "budget_total"
    if claim_type(quote) == "completed_result" and re.search(r"实验|测试|实测|采集|样本|准确率|召回率|F1", quote) and re.search(r"缺|没有|不符|不一致|矛盾|未验证|依据", opinion):
        result["check_code"] = "result_evidence"
    if issue["kind"] == "contradiction":
        result.update(kind="needs_evidence", problem="待核对的差异：" + issue["problem"], support_status="insufficient")
        if not semantic:
            result["problem"] = "该处与材料是否一致尚缺对应依据，请核对。"
    return result, warnings


def follow_up_items(issues):
    items, seen = [], set()
    for issue in issues:
        key = (issue.get("section_id", ""), issue.get("check_code") or issue["problem"])
        if key in seen:
            continue
        seen.add(key)
        if issue.get("kind") == "writing_suggestion":
            continue
        items.append({"id": hashlib.sha256(repr(key).encode()).hexdigest()[:20],
            "section_id": key[0], "reason": issue["problem"], "request": issue["suggestion"],
            "actions": ["edit_draft", "confirm_material"], "status": "awaiting_input"})
    return items
