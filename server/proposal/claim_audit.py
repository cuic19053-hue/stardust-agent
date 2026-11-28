"""Bind high-risk draft statements to confirmed passages or scoped policy clauses."""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict

from .documents import split_into_chunks
from .knowledge import PublicKnowledgeRetriever, matching_sources
from .review_evidence import claim_type

CITATION = re.compile(r"\[来源[：:]\s*([^\]\n]+)\]")
LOCATOR = re.compile(r"^\[(第\s*\d+\s*页|段落\s*\d+|表格\s*\d+\s*第\s*\d+\s*行|行\s*\d+|图片 OCR)\]\s*")
MONEY = re.compile(r"(?:\d[\d,.]{0,24}|[一二三四五六七八九十百千万两]{1,24})\s*(?:万|千|百)?元")
EXPENSE = re.compile(r"预算|经费|耗材|材料费|设备费|差旅费|支出|报销|资助")
ELIGIBILITY = re.compile(r"申报资格|申请资格|符合.{0,6}(?:申报|申请)条件|(?:可以|可|不得|不能|有资格)(?:申报|申请)|"
                         r"(?:申报|申请)(?:条件|对象|人须|人应|人需|人为)|不在申报之列")
DEADLINE = re.compile(r"截止|(?:申报|申请|报送|提交|报名).{0,8}截至|申报时间|报送时间|\d日.{0,12}(?:前|之前).{0,12}(?:报送|提交|申报)")
RESULT = re.compile(r"(?:已|已经)(?:经)?(?:开展|发表|获批|建成|验证|观测|测得)|(?:完成了|取得了)(?:观测|实验|测试|调研|成果)")
LABEL = {"budget": "预算", "completed": "已完成成果", "eligibility": "申报资格", "deadline": "截止日期"}
POLICY = {"eligibility", "deadline"}
LIMIT = 80


def _types(text):
    kinds = []
    if EXPENSE.search(text) and MONEY.search(text):
        kinds.append("budget")
    if ELIGIBILITY.search(text):
        kinds.append("eligibility")
    if DEADLINE.search(text):
        kinds.append("deadline")
    if not set(kinds) & POLICY:
        if claim_type(text) == "completed_result" or RESULT.search(text):
            kinds.append("completed")
    return kinds


def _key(text):
    # Preserve qualifiers, numbering, subjects, amount units and all interior punctuation.
    return text.strip().rstrip("。！？；;").strip()


def _separate_facts(claim):
    # Preserve conditions and policy exceptions. Split two independent project facts
    # only when each clause has its own subject matter and can be checked on its own.
    if "，" in claim and not re.search(r"如果|若|假设|示例|例如|除非|只要|据称|条件|才会", claim):
        clauses = claim.split("，", 2)
        if len(clauses) == 2 and {tuple(_types(c)) for c in clauses} == {("budget",), ("completed",)}:
            return [c.strip() for c in clauses]
    return [claim]


def _statements(text):
    # Postfix markers after a full stop still belong to the preceding statement.
    text = re.sub(r"([。！？；;])[^\S\n]*((?:\[来源[：:][^\]\n]+\][ \t]*)+)", r"\2\1", text)
    for line in text.splitlines():
        for match in re.finditer(r"(?:\[来源[：:][^\]\n]*\]|[^。！？；;])+[。！？；;]?", line):
            statement = match.group()
            refs = [tuple(p.strip() for p in re.split("[，,]", value, maxsplit=1))
                    for value in CITATION.findall(statement)]
            claim = _key(CITATION.sub("", statement))
            claim = re.sub(r"^(?:[-*] |#{1,6} )", "", claim).replace("**", "").strip()
            for fact in _separate_facts(claim):
                kinds = _types(fact)
                if kinds:
                    yield fact, kinds, refs


def scoped_official_sources(project, store=None, public_hits=None):
    state = project.get("state") or {}
    facts = state.get("confirmed_facts") or {}
    year = str(facts.get("application_year", ""))
    if not project.get("school") or not re.fullmatch(r"\d{4}", year):
        return []
    skill_id = str(state.get("skill_id", "innovation-research"))
    variant = state.get("skill_variant", "")
    try:
        sources = matching_sources(skill_id, project["school"], int(year),
                                   variant=variant, filters=facts, official_only=True)
    except (OSError, ValueError, UnicodeError):
        return []
    if (public_hits is None and os.getenv("RAG_PUBLIC_DB_ENABLED", "").lower() == "true"
            and callable(getattr(store, "search_public_knowledge", None))):
        # A fixed public query checks publication without sending any draft or private facts.
        public_hits = PublicKnowledgeRetriever(store).search(
            skill_id, "申报资格 截止日期 系统申报时间", project["school"], int(year),
            variant=variant, filters={k: facts.get(k, "") for k in ("education_level", "college", "political_stage")},
            top_k=10)["results"]
    result = []
    for source in sources:
        if source.get("project_levels") and project.get("level") not in source["project_levels"]:
            continue
        chunks = [c for c in source["chunks"] if public_hits is None or any(
            h.get("id") == c["id"] and h.get("source_sha256") == source["source_sha256"] for h in public_hits)]
        if chunks:
            result.append({**source, "chunks": chunks})
    return result


def eligible_documents(project, documents, history):
    from .personal_source_filter import filter_personal_evidence
    from .project_writing import project_sources_allowed
    from .skills import registry
    if not project_sources_allowed(history, ""):
        return []
    skill = registry.get((project.get("state") or {}).get("skill_id", "innovation-research"))
    excluded = filter_personal_evidence(skill, project, history, documents, {})[1] if skill else []
    return [d for d in documents if d.get("id") not in excluded and d.get("project_id") == project["id"]
            and d.get("kind") in {"source", "photo"} and d.get("confirmed")
            and d.get("confirmation_origin") == "user" and not d.get("low_confidence_count")]


def _private_passages(doc, wanted, cited_locations):
    lines = [line.strip() for line in doc["extracted_text"].splitlines() if line.strip()]
    chunk_refs = {ref for ref in cited_locations if re.fullmatch(r"片段 [1-9]\d*", ref)}
    bounds = []
    if chunk_refs:
        normalized = "\n".join(lines)
        for n, chunk in enumerate(split_into_chunks(normalized), 1):
            ref = f"片段 {n}"
            if ref not in chunk_refs or any(LOCATOR.match(line) for line in chunk.splitlines()):
                continue
            start = normalized.find(chunk)
            if start >= 0 and normalized.find(chunk, start + 1) == -1:
                bounds.append((start, start + len(chunk), ref))
    page = ""
    offset = 0
    for line_no, line in enumerate(lines, 1):
        start = offset
        offset += len(line) + 1
        location = LOCATOR.match(line)
        if location:
            page = location[1]
            start += location.end()
            line = line[location.end():]
        ref = page or f"行 {line_no}"
        for match in re.finditer(r"[^。！？；;]+[。！？；;]?", line):
            quote = match[0].strip()
            key = _key(quote)
            refs = {ref}
            refs.update(label for left, right, label in bounds
                        if left <= start + match.start() and start + match.end() <= right)
            for fact in _separate_facts(key):
                if fact in wanted or refs & cited_locations:
                    yield fact, ref, refs, quote


def source_version(evidence):
    return {k: evidence[k] for k in ("document_id", "source_url", "source_sha256", "source_ref", "review_revision",
                                     "application_year", "scope", "skill_id", "school")
            if k in evidence}


def _private_evidence(rows, documents, retrieved):
    wanted = {r["claim"] for r in rows if not set(r["kinds"]) & POLICY and len(r["claim"]) <= 600}
    requested_refs = {ref for row in rows for ref in row["refs"]}
    by_claim = defaultdict(list)
    names = Counter(d["original_name"] for d in documents)
    comparisons = {}
    if not wanted:
        return by_claim, comparisons, names
    for doc in sorted(documents, key=lambda d: d["id"]):
        hits = None if retrieved is None else [h for h in retrieved if h.get("document_id") == doc["id"]]
        if hits == []:
            continue
        name = doc["original_name"]
        locations = {ref[1] for ref in requested_refs if len(ref) == 2 and ref[0] == name}
        seen = set()
        matched_here = set()
        digest = None
        for key, ref, locators, quote in _private_passages(doc, wanted, locations):
            aliases = {(name, locator) for locator in locators}
            cited = aliases & requested_refs
            if hits is not None and not any(quote in h.get("text", "") for h in hits):
                continue
            if key not in wanted and (not cited or all(r in comparisons for r in cited)):
                continue
            if (not cited and key in matched_here) or (key, ref) in seen:
                continue
            if digest is None:
                digest = hashlib.sha256(doc["extracted_text"].encode()).hexdigest()
            evidence = {"document_id": doc["id"], "source_name": name, "source_ref": ref,
                        "quote": quote[:600], "source_sha256": digest, "review_revision": doc.get("review_revision"),
                        "project_id": doc["project_id"]}
            if key in wanted:
                matched_here.add(key)
                seen.add((key, ref))
                by_claim[key].append((aliases, evidence))
            if names[name] == 1:
                for marker in cited:
                    comparisons.setdefault(marker, evidence)

    return by_claim, comparisons, names


def _policy_evidence(project, store, public_hits):
    policies = {}
    for source in scoped_official_sources(project, store, public_hits):
        for chunk in source["chunks"]:
            if not chunk.get("original_quote"):
                continue
            evidence = {k: source[k] for k in ("source_url", "source_sha256", "school", "application_year", "scope", "skill_id")}
            evidence.update(source_name=source["title"], source_ref=chunk["source_ref"], quote=chunk["original_quote"])
            for wording in (chunk["content"], chunk["original_quote"]):
                policies.setdefault(_key(wording), []).append(({(source["title"], chunk["source_ref"])}, evidence))

    return policies


def audit_claims(project, sections, documents, *, retrieved=None, public_hits=None, store=None, saved=False):
    """Inspect the exact saved text. A generated claim must also use evidence seen on that run."""
    rows = []
    truncated = False
    for sid, content in sections.items():
        for claim, kinds, refs in _statements(str(content)):
            if len(rows) == LIMIT:
                truncated = True
                break
            rows.append({"section_id": sid, "claim": claim, "kinds": kinds, "refs": refs})
        if truncated:
            break

    by_claim, comparisons, names = _private_evidence(rows, documents, retrieved)
    policies = _policy_evidence(project, store, public_hits) if any(set(r["kinds"]) & POLICY for r in rows) else {}

    pinned = {}
    if saved:
        history = (project.get("state") or {}).get("_chat_draft_sources", {})
        for sid, content in sections.items():
            origin = history.get(sid) or {}
            if origin.get("body_sha256") == hashlib.sha256(str(content).encode()).hexdigest():
                pinned[sid] = origin.get("fact_origins", {})
    retrieved_refs = {(h.get("source_name"), h.get("source_ref")) for h in retrieved or []}
    warnings = []
    for row in rows:
        claim = row["claim"]
        refs = set(row.pop("refs"))
        is_policy = bool(set(row["kinds"]) & POLICY)
        options = (policies if is_policy else by_claim).get(claim, [])
        if refs and retrieved is not None and not is_policy and not refs <= retrieved_refs:
            options = []
        row["claim_id"] = hashlib.sha256(json.dumps([claim, sorted(refs)], ensure_ascii=False).encode()).hexdigest()
        previous = pinned.get(row["section_id"], {}).get(row["claim_id"])
        candidates = [ev for aliases, ev in options if not refs or
                      (refs <= aliases and (is_policy or names[ev["source_name"]] == 1))]
        evidence = next((ev for ev in candidates if previous and source_version(ev) == previous),
                        next(iter(candidates), None))
        changed = previous is not None and (not evidence or source_version(evidence) != previous)
        row["status"] = "matched_original" if evidence and not changed else "needs_review"
        row["claim"] = claim[:240]
        if changed:
            row["warning"] = f"保存时绑定的原文版本或位置已变化，『{claim[:100]}』待核对。"
            if evidence:
                row["cited_passage"] = evidence
            warnings.append(row["warning"])
        elif evidence:
            row["evidence"] = evidence
        else:
            subject = "、".join(LABEL[k] for k in row["kinds"])
            reason = "缺少适用于本校、本年度和该赛道的正式条款" if is_policy else "需与已确认材料的对应原句核对"
            row["warning"] = f"{subject}待核对：『{claim[:100]}』，{reason}。"
            if len(refs) == 1 and not is_policy:
                comparison = comparisons.get(next(iter(refs)))
                if comparison:
                    row["cited_passage"] = comparison
            warnings.append(row["warning"])
    if truncated:
        warnings.append(f"本次已检查前 {LIMIT} 条关键断言，其余内容待核对。")
    return {"claims": rows, "warnings": warnings, "truncated": truncated}
