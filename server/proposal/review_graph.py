"""Read-only LangGraph review over one immutable local snapshot."""
from __future__ import annotations

import json
import time
from copy import deepcopy
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from .review_evidence import retrieve_claim_evidence
from .review_verification import evidence_checks, follow_up_items, verify_model_issue
from .review_runs import GRAPH_VERSION, PROMPT_VERSION, ReviewRuns
from .tools import ProposalTools


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=4, max_length=600)


class ModelIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_id: str = Field(min_length=1, max_length=100)
    draft_quote: str = Field(min_length=4, max_length=240)
    problem: str = Field(min_length=1, max_length=240)
    suggestion: str = Field(min_length=1, max_length=240)
    kind: Literal["writing_suggestion", "needs_evidence", "school_requirement", "contradiction"]
    citations: list[Citation] = Field(default_factory=list, max_length=2)


class ModelReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issues: list[ModelIssue] = Field(max_length=6)


class ReviewState(TypedDict, total=False):
    run_id: str
    snapshot: dict
    evidence: list[dict]
    claims: list[dict]
    rule_issues: list[dict]
    model_issues: list[dict]
    issues: list[dict]
    unverified_hypotheses: list[dict]
    model_status: str
    warnings: list[str]
    metrics: dict
    report: dict
    mode: str


class SnapshotStore:
    """Expose only reads used by existing validation and keyword retrieval."""
    def __init__(self, snapshot):
        self.snapshot = deepcopy(snapshot)

    def get_project(self, project_id):
        project = self.snapshot["project"]
        return deepcopy(project) if project["id"] == project_id else None

    def list_documents(self, project_id):
        return deepcopy(self.snapshot["documents"]) if self.get_project(project_id) else []

    def chunks_for_project(self, project_id):
        return deepcopy(self.snapshot["chunks"]) if self.get_project(project_id) else []


class NoEmbedding:
    def revision(self):
        return ""


def build_review_graph(runs: ReviewRuns, checkpointer, model_call=None, *, graph_version=GRAPH_VERSION,
                       interrupt_after=None):
    def load_snapshot(state):
        run = runs.raw(state["run_id"])
        if run["graph_version"] != graph_version:
            raise ValueError("预审执行版本不匹配")
        return {"snapshot": json.loads(run["snapshot"]), "warnings": [], "metrics": {},
                "mode": run["mode"], "model_status": "not_requested", "model_issues": []}

    def retrieve(state):
        snapshot = state["snapshot"]
        claims, evidence = retrieve_claim_evidence(snapshot, SnapshotStore(snapshot), NoEmbedding())
        return {"claims": claims, "evidence": evidence}

    def rules(state):
        project = state["snapshot"]["project"]
        result = ProposalTools(SnapshotStore(state["snapshot"])).validate_project(project["id"])
        issues = []

        def add(section_id, problem, suggestion):
            issues.append({"section_id": section_id, "problem": problem, "suggestion": suggestion,
                           "origin": "rule", "verification": "rule_result", "citations": []})

        for section in project["outline"]:
            title = section.get("title", section["id"])
            if title in result["missing_sections"]:
                add(section["id"], "该章节尚未填写", "按学校模板补充该章节；不适用时注明原因。")
            if title in result["placeholder_sections"]:
                add(section["id"], "该章节仍含待补充内容", "核对真实资料后补全占位内容。")
        for item in result["missing_facts"]:
            add("", item["label"], "补充项目的真实信息后重新检查。")
        for warning in result["consistency_warnings"]:
            if warning in {f"章节“{title}”仍有待补充内容。" for title in result["placeholder_sections"]}:
                continue  # Already reported with a concrete section_id above.
            if warning.startswith("草稿可考虑补充："):
                requested = warning.split("：", 1)[1].split("。", 1)[0].split("、")
                remaining = [title for title in requested if title not in result["missing_sections"]]
                if not remaining:
                    continue
                warning = "草稿可考虑补充：" + "、".join(remaining) + "。这是通用内容提示，具体以学校通知和模板为准。"
            add("", warning, "结合草稿及已确认材料核对；通用提示以学校要求为准。")
            if warning.startswith("预算信息可能不一致"):
                issues[-1]["section_id"] = next((s["id"] for s in project["outline"] if "预算" in s.get("title", "") or "budget" in s["id"]), "")
                issues[-1]["check_code"] = "budget_total"
        for name in result["unconfirmed_documents"]:
            add("", f"材料“{name}”尚未确认", "先核对识别文字；未确认文字不会作为模型检查依据。")
        return {"rule_issues": issues}

    def semantic(state):
        run = runs.raw(state["run_id"])
        if run["mode"] == "rules":
            return {"model_issues": [], "model_status": "not_requested"}
        project = state["snapshot"]["project"]
        content = {"sections": project["sections"], "outline": project["outline"],
                   "school": project["school"], "facts": project.get("state", {}),
                   "priority_claims": [{"section_id": c["section_id"], "quote": c["quote"], "type": c["type"]}
                                       for c in state["claims"][:12]],
                   "evidence": state["evidence"]}
        if len(json.dumps(content, ensure_ascii=False)) > 26_000:
            return {"model_issues": [], "model_status": "input_limit", "warnings": ["材料超过首版模型检查范围；本报告仅含规则检查。"]}
        if model_call is None:
            raise ValueError("本地模型检查服务未配置")
        schema = ModelReview.model_json_schema()
        accepts_input = getattr(model_call, "accepts_input", None)
        if callable(accepts_input) and not accepts_input(content, schema):
            return {"model_issues": [], "model_status": "input_limit", "warnings": ["材料超过本地模型上下文预算；本报告仅含规则检查。"]}
        response = runs.model_result(state["run_id"], lambda: model_call(run, content, schema))
        if response.get("skip") == "insufficient_credits":
            return {"model_issues": [], "model_status": "insufficient_credits", "warnings": ["积分不足，模型未调用；本报告仅含规则检查。"]}
        try:
            reviewed = ModelReview.model_validate_json(response["content"])
        except (ValueError, TypeError, KeyError):
            return {"model_issues": [], "model_status": "invalid_response", "warnings": ["模型未返回有效检查意见；本报告仅含规则检查。"],
                    "metrics": response.get("metrics", {})}
        return {"model_issues": [item.model_dump() for item in reviewed.issues], "model_status": "completed",
                "metrics": response.get("metrics", {})}

    def verify(state):
        project = state["snapshot"]["project"]
        evidence = {item["id"]: item for item in state["evidence"]}
        issues = [*state["rule_issues"], *evidence_checks(state["snapshot"], state["claims"], state["evidence"])]
        warnings = list(state["warnings"])
        hypotheses = []
        for issue in state["model_issues"]:
            verified, notices = verify_model_issue(project, issue, evidence)
            warnings.extend(notices)
            if verified:
                duplicate = next((i for i in issues if i.get("section_id") == verified["section_id"] and i.get("check_code")
                                  and i.get("check_code") == verified.get("check_code")), None)
                if duplicate:
                    existing = {(c["id"], c["quote"]) for c in duplicate["citations"]}
                    duplicate["citations"] = (duplicate["citations"] + [c for c in verified["citations"]
                        if (c["id"], c["quote"]) not in existing])[:2]
                    continue
                # A literal citation/lexical overlap is not an entailment proof.
                # Keep unproven factual judgments outside discovered problems.
                if verified["kind"] != "writing_suggestion" or verified["support_status"] == "insufficient":
                    hypotheses.append(verified)
                    continue
                issues.append(verified)
        if not evidence:
            warnings.append("未检索到匹配的已确认材料，学校要求和事实依据仍需核对。")
        return {"issues": issues, "unverified_hypotheses": hypotheses, "warnings": list(dict.fromkeys(warnings))}

    def save(state):
        run = runs.raw(state["run_id"])
        project = state["snapshot"]["project"]
        report = {"run_id": run["id"], "fingerprint": run["fingerprint"],
                  "parent_id": run.get("parent_id"), "origin": run.get("origin", "manual"),
                  "graph_version": graph_version, "prompt_version": PROMPT_VERSION,
                  "project_revision": project["material_revision"],
                  "section_titles": {item["id"]: item.get("title", item["id"]) for item in project["outline"]},
                  "model": run["model"], "model_status": state["model_status"],
                  "complete": state["model_status"] in {"not_requested", "completed"},
                  "issues": state["issues"], "warnings": state["warnings"], "metrics": state["metrics"],
                  "follow_up": follow_up_items(state["issues"]),
                  "unverified_hypotheses": state["unverified_hypotheses"],
                  "claims": state["claims"],
                  "workflow_stage": ("awaiting_input" if follow_up_items(state["issues"]) else
                                     "awaiting_verification" if state["unverified_hypotheses"] else "reviewed"),
                  "notice": "本报告检查指定草稿快照。规则结果属于基础检查；模型意见及来源适用性待人工核对。"}
        runs.update(run["id"], report=report)
        return {"report": report}

    def instrument(name, function):
        def node(state):
            runs.update(state["run_id"], step=name)
            started = time.monotonic()
            result = function(state)
            runs.event(state["run_id"], name, int((time.monotonic() - started) * 1000))
            return result
        return node

    graph = StateGraph(ReviewState)
    steps = [("load_snapshot", load_snapshot), ("retrieve", retrieve), ("rules", rules),
             ("semantic", semantic), ("verify", verify), ("save_report", save)]
    for name, function in steps:
        graph.add_node(name, instrument(name, function))
    graph.add_edge(START, steps[0][0])
    graph.add_edge("load_snapshot", "retrieve")
    graph.add_edge("retrieve", "rules")
    graph.add_conditional_edges("rules", lambda state: state["mode"],
                                {"rules": "verify", "model": "semantic"})
    graph.add_edge("semantic", "verify")
    graph.add_edge("verify", "save_report")
    graph.add_edge(steps[-1][0], END)
    return graph.compile(checkpointer=checkpointer, interrupt_after=interrupt_after)
