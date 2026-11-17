"""Counterexamples for concrete retrieval and conservative semantic gates."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from server.proposal.review_evidence import claim_type, extract_claims, retrieve_claim_evidence
from server.proposal.review_graph import NoEmbedding, SnapshotStore
from server.proposal.review_verification import budget_sum, evidence_checks, verify_model_issue


def snapshot(project, documents):
    docs, chunks = [], []
    for i, d in enumerate(documents):
        docs.append({"id": str(i), "project_id": "p", "original_name": d.get("name", "资料"),
            "kind": d.get("kind", "source"), "confirmed": d.get("confirmed", True),
            "confirmation_origin": "user", "extracted_text": d["text"]})
        chunks.append({"id": str(i), "document_id": str(i), "project_id": "p", "source_name": docs[-1]["original_name"],
                       "source_ref": "片段 1", "content": d["text"]})
    return {"project": {"id": "p", **project}, "documents": docs, "chunks": chunks}


def checks(snap):
    claims, evidence = retrieve_claim_evidence(snap, SnapshotStore(snap), NoEmbedding())
    return evidence_checks(snap, claims, evidence)


def test_retrieve_uses_actual_assertion_in_late_chapter_among_generic_decoys():
    p = {"outline": [{"id": str(i), "title": "研究方法"} for i in range(20)],
         "sections": {str(i): "研究方法计划随后确定。" for i in range(20)}}
    p["sections"]["19"] = "星载稀疏矩阵加速器已完成928条轨道遥测的验证。"
    snap = snapshot(p, [{"text": "学校申报要求研究方法经费预算字数。"} for _ in range(16)] +
                    [{"text": "星载稀疏矩阵加速器928条轨道遥测验证记录尚未完成。"}])
    claims, evidence = retrieve_claim_evidence(snap, SnapshotStore(snap), NoEmbedding())
    assert len(evidence) <= 12
    concrete = next(c for c in claims if "928" in c["quote"])
    assert "928" in concrete["query"] and "16" in concrete["evidence_ids"]
    assert concrete["id"] in next(e for e in evidence if e["id"] == "16")["claim_ids"]


@pytest.mark.parametrize("text,expected", [
    ("计划获得授权后再开展实验，目标准确率98%。", "plan"),
    ("实验尚未开展，没有实测指标。", "not_completed"),
    ("研究目标是不预设已达到的效果。", "plan"),
    ("尚未整理记录，但已完成1000条报修记录实验。", "completed_result"),
    ("本项目已完成200个实测点的采集。", "completed_result"),
])
def test_plans_and_completed_results_are_classified_separately(text, expected):
    assert claim_type(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("预算合计为3000元，测试费1200元，材料费1800元。", (3000, 3000)),
    ("预算合计为8000元，测试费5000元，材料费2000元。", (8000, 7000)),
    ("预算合计为1万元，测试费5000元，材料费0.5万元。", (10000, 10000)),
    ("预算合计为8000元，其中测试费5000元，材料费2000元。", None),
])
def test_budget_arithmetic_uses_values_units_and_complete_item_lists(text, expected):
    assert budget_sum(text) == expected


def test_existing_pilot_and_new_regressions_keep_clean_plans_and_detect_actual_defects():
    root = Path(__file__).resolve().parents[1] / "server/evals"
    expected = {"unsupported_results": "result_evidence", "source_injection": "policy_source",
                "wrong_school_notice": "policy_source", "arithmetic_error": "budget_arithmetic",
                "measured_claim_conflict": "result_evidence", "invented_school_rule": "policy_source"}
    for name in ("review_cases.json", "review_holdout_cases.json"):
        dataset = json.loads((root / name).read_text())
        for case in dataset["cases"]:
            project = deepcopy(dataset["base_project"])
            project["sections"].update(case.get("sections", {}))
            issues = checks(snapshot(project, dataset["base_documents"] + case.get("documents", [])))
            if case["id"] in {"planned_results", "future_plan_ok"}:
                assert issues == [], (case["id"], issues)
            if case["id"] in expected:
                assert expected[case["id"]] in {i["check_code"] for i in issues}, case["id"]


def opinion(quote, **kwargs):
    return {"section_id": "s", "draft_quote": quote, "problem": "需要核对", "suggestion": "请核对",
            "kind": "needs_evidence", "citations": [], **kwargs}


def test_literal_quote_does_not_make_a_project_estimate_a_school_requirement():
    quote = "本项目预算合计为3000元。"
    p = {"school": "大学甲", "outline": [{"id": "s"}], "sections": {"s": quote}}
    hit = {"id": "c", "source_kind": "source", "source_name": "费用估算", "source_ref": "1", "content": quote}
    result, _ = verify_model_issue(p, opinion(quote, kind="school_requirement", problem="学校要求必须先批准预算",
        citations=[{"id": "c", "quote": quote}]), {"c": hit})
    assert result is None  # An invented rule must not create work for the student.


def test_correct_arithmetic_and_future_plan_accusations_are_filtered_but_real_error_survives():
    quote = "预算合计为3000元，测试费1200元，材料费1800元。"
    p = {"outline": [{"id": "s"}], "sections": {"s": quote}}
    result, _ = verify_model_issue(p, opinion(quote, problem="分项相加错误", kind="contradiction"), {})
    assert result is None
    p["sections"]["s"] = quote.replace("1800", "1700")
    result, _ = verify_model_issue(p, opinion(p["sections"]["s"], problem="分项相加错误", kind="contradiction"), {})
    assert result is not None
    plan = "计划获得授权后再开展实验。"
    p["sections"]["s"] = plan
    result, _ = verify_model_issue(p, opinion(plan, problem="与尚未开展的实测结果矛盾", kind="contradiction"), {})
    assert result is None


def test_exact_but_unrelated_quote_is_explicitly_not_semantic_support():
    quote = "已完成1000条报修实验。"
    p = {"outline": [{"id": "s"}], "sections": {"s": quote}}
    hit = {"id": "c", "source_name": "预算", "source_ref": "1", "content": "预算估算2000元"}
    result, _ = verify_model_issue(p, opinion(quote, kind="contradiction", problem="和预算内容矛盾",
        citations=[{"id": "c", "quote": "预算估算2000元"}]), {"c": hit})
    assert result["support_status"] == "insufficient"
    assert result["citations"][0]["support_status"] == "quote_only"
    assert "和预算内容矛盾" not in result["problem"]
