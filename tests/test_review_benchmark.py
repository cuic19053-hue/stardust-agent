"""Evaluation accounting must not turn structural checks into model accuracy."""
import copy
import json
from pathlib import Path

import pytest

pytest.importorskip("langgraph.checkpoint.sqlite")

from server.evals import review_benchmark as benchmark


IDENTITY = {"name": "synthetic-model", "digest": "a" * 64, "details": {}}


def config(**changes):
    return {"cases": ["budget_mismatch"], "arms": ["rules", "direct", "graph"],
            "model": "synthetic-model", "base_url": "http://127.0.0.1:11434",
            "seed": 42, "repeats": 1, "timeout": 10, **changes}


class FakeModel:
    calls = []

    def __init__(self, config, directory):
        self.config = config
        self.observation = {"calls": 0, "metrics": None, "content": None, "evidence": []}

    def accepts_input(self, *_):
        return True

    def __call__(self, run, content, schema):
        self.calls.append(copy.deepcopy(content))
        assert "targets" not in json.dumps(content)
        source = content["evidence"][0]
        issues = [{"section_id": "budget", "draft_quote": "预算合计为3000元", "kind": "contradiction",
                   "problem": "预算总额冲突", "suggestion": "核对两处总额", "citations": [
                       {"id": source["id"], "quote": "来源没有的引文"}]}]
        raw = json.dumps({"issues": issues}, ensure_ascii=False)
        metrics = {"prompt_eval_count": 100, "eval_count": 25, "latency_ms": 1}
        self.observation.update(calls=1, content=raw, metrics=metrics, evidence=content["evidence"])
        return {"content": raw, "metrics": metrics}


@pytest.fixture
def trial(tmp_path):
    FakeModel.calls = []
    directory = tmp_path / "benchmark"
    report = benchmark.run_benchmark(directory, config(), IDENTITY, factory=FakeModel)
    return directory, report


def test_paired_paths_use_identical_drafts_and_model_contract_but_keep_raw_failures(trial):
    directory, report = trial
    assert len(FakeModel.calls) == 2
    assert FakeModel.calls[0]["sections"] == FakeModel.calls[1]["sections"]
    assert {e["id"] for e in FakeModel.calls[0]["evidence"]} == {e["id"] for e in FakeModel.calls[1]["evidence"]}
    assert all(row["draft_unchanged"] for row in report["runs"])
    arms = {row["arm"]: row for row in report["runs"]}
    assert arms["direct"]["issues"][0]["citations"]
    assert arms["direct"]["raw_locations"]["matched_citations"] == 0
    assert arms["graph"]["raw_locations"]["citations"] == 1
    assert arms["graph"]["final_locations"]["citations"] == 0
    assert any(issue["origin"] == "rule" for issue in arms["graph"]["issues"])
    assert [e["node"] for e in arms["graph"]["events"]] == ["load_snapshot", "retrieve", "rules", "semantic", "verify", "save_report"]
    assert report["arms"]["rules"]["model_calls"] == 0
    assert report["human_review_complete"] is False
    assert all(value["semantic"] is None for value in report["arms"].values())
    assert (directory / "results.csv").exists()


def test_resume_does_not_replay_and_changed_configuration_is_rejected(trial):
    directory, first = trial
    before = len(FakeModel.calls)
    resumed = benchmark.run_benchmark(directory, config(), IDENTITY, resume=True, factory=FakeModel)
    assert len(FakeModel.calls) == before
    assert resumed == first
    with pytest.raises(ValueError, match="不能混入"):
        benchmark.run_benchmark(directory, config(seed=43), IDENTITY, resume=True, factory=FakeModel)
    with pytest.raises(ValueError, match="不能混入"):
        benchmark.run_benchmark(directory, config(), {**IDENTITY, "digest": "b" * 64}, resume=True, factory=FakeModel)


def test_interrupted_attempt_is_retained_without_a_second_inference(trial):
    directory, _ = trial
    run_dir = directory / "runs/budget_mismatch-r1-direct"
    (run_dir / "result.json").unlink()
    before = len(FakeModel.calls)
    resumed = benchmark.run_benchmark(directory, config(), IDENTITY, resume=True, factory=FakeModel)
    assert len(FakeModel.calls) == before
    row = next(r for r in resumed["runs"] if r["arm"] == "direct")
    assert row["status"] == "interrupted" and row["complete"] is False
    assert resumed["arms"]["direct"]["unknown_usage_runs"] == 1
    assert resumed["arms"]["direct"]["complete"] == 0


def test_failed_model_is_not_excluded_from_quality_denominators(tmp_path):
    class Failed(FakeModel):
        def __call__(self, *_):
            self.observation["calls"] = 1
            raise TimeoutError("synthetic timeout")
    output = tmp_path / "failed"
    result = benchmark.run_benchmark(output, config(arms=["direct"]), IDENTITY, factory=Failed)
    row = result["runs"][0]
    assert row["status"] == "failed" and result["arms"]["direct"]["complete"] == 0
    reviews = benchmark.read_json(output / "review-template.json")
    reviews.update(reviewer_kind="agent", reviewer_id="test-reviewer")
    reviewed = benchmark.build_report(output, benchmark.load_dataset(), reviews)
    assert reviewed["arms"]["direct"]["semantic"]["false_negative"] == 1
    assert reviewed["arms"]["direct"]["semantic"]["recall"] == 0
    assert reviewed["arms"]["direct"]["semantic"]["precision"] is None
    assert reviewed["human_review_complete"] is False


def example_result():
    return {"issues": [{"problem": "预算不一致", "citations": [{"id": "source", "quote": "预算为2000元"}]}]}


def judgement(result, verdict="matched", target="budget_total"):
    return {"result_sha256": benchmark.digest(result), "issues": [
        {"index": 0, "verdict": verdict, "target_id": target, "evidence": "预算不一致", "reason": "核对两处金额"}],
        "citations": [{"issue_index": 0, "citation_index": 0, "quote": "预算为2000元",
                       "verdict": "does_not_support", "reason": "引文不能支持该条具体结论"}]}


def test_semantic_and_literal_citation_scores_are_independent():
    result = example_result()
    case = {"targets": [{"id": "budget_total"}, {"id": "other_issue"}]}
    score = benchmark.semantic_score(result, case, judgement(result), "agent")
    assert score["true_positive"] == 1 and score["false_negative"] == 1
    assert score["citation_support"] == {"supported": 0, "total": 1}
    assert benchmark.semantic_score(result, case, None, "agent") is None
    pending = judgement(result)
    pending["issues"][0]["verdict"] = "pending"
    assert benchmark.semantic_score(result, case, pending, "agent") is None


def test_duplicate_predictions_do_not_inflate_recall():
    result = example_result()
    result["issues"] *= 2
    review = judgement(result)
    review["issues"] *= 2
    review["issues"][1] = {**review["issues"][1], "index": 1}
    review["citations"] = []
    score = benchmark.semantic_score(result, {"targets": [{"id": "budget_total"}]}, review, "human")
    assert score["true_positive"] == 1 and score["false_positive"] == 1


@pytest.mark.parametrize("change", ["stale", "missing_issue", "fake_evidence", "fake_target", "missing_citation"])
def test_invalid_review_evidence_cannot_generate_scores(change):
    result, case = example_result(), {"targets": [{"id": "budget_total"}]}
    review = judgement(result)
    if change == "stale":
        review["result_sha256"] = "stale"
    elif change == "missing_issue":
        review["issues"] = []
    elif change == "fake_evidence":
        review["issues"][0]["evidence"] = "不是实际输出"
    elif change == "fake_target":
        review["issues"][0]["target_id"] = "invented"
    else:
        review["citations"].append({**review["citations"][0], "citation_index": 1})
    with pytest.raises(ValueError):
        benchmark.semantic_score(result, case, review, "agent")


def test_report_only_has_no_model_access_and_checks_reviewer_identity(trial, monkeypatch):
    directory, _ = trial
    monkeypatch.setattr(benchmark, "model_identity", lambda *_: pytest.fail("report-only must not query a model"))
    assert benchmark.main(["--output", str(directory), "--report-only"]) == 0
    with pytest.raises(ValueError, match="复核者"):
        benchmark.build_report(directory, benchmark.load_dataset(), {"reviewer_kind": "automated", "reviewer_id": "x"})
    with pytest.raises(ValueError, match="资料"):
        benchmark.build_report(directory, {**benchmark.load_dataset(), "version": 999})


def test_external_model_urls_and_duplicated_arms_are_rejected(tmp_path):
    for url in ("https://external.test", "http://127.0.0.1:11434/proxy", "http://user:secret@localhost:11434"):
        with pytest.raises(ValueError):
            benchmark.MeasuredModel(config(base_url=url), tmp_path)
    with pytest.raises(ValueError, match="对比路径"):
        benchmark.run_benchmark(tmp_path / "bad", config(arms=["direct", "direct"]), IDENTITY, factory=FakeModel)


def test_pending_citation_review_is_not_a_completed_human_review(trial):
    directory, _ = trial
    reviews = benchmark.read_json(directory / "review-template.json")
    reviews.update(reviewer_kind="human", reviewer_id="synthetic-unit-test-reviewer")
    for run in reviews["runs"].values():
        for item in run["issues"]:
            item.update(verdict="spurious", reason="测试复核完整性，不是实际人工评测")
    result = benchmark.build_report(directory, benchmark.load_dataset(), reviews)
    assert all(row["semantic"] is not None for row in result["runs"])
    assert result["human_review_complete"] is False
    assert result["arms"]["direct"]["semantic"]["citation_support_rate"] is None


def public_baseline():
    return benchmark.read_json(Path(__file__).resolve().parents[1] / "server/evals/baselines/review_pilot_20261117.json")


def test_public_baseline_scores_are_reproducible_without_a_model(tmp_path):
    baseline = public_baseline()
    original = baseline["report"]
    benchmark.write_json(tmp_path / "manifest.json", original["manifest"])
    for row in original["runs"]:
        directory = tmp_path / "runs" / row["id"]
        directory.mkdir(parents=True)
        trial_keys = {"id", "case_id", "arm", "repeat", "semantic"}
        benchmark.write_json(directory / "result.json", {k: v for k, v in row.items() if k not in trial_keys})
    rebuilt = benchmark.build_report(tmp_path, baseline["dataset"], baseline["reviews"])
    assert rebuilt["arms"] == original["arms"]
    assert rebuilt["human_review_complete"] is False


def test_public_baseline_token_and_quote_metrics_match_raw_responses():
    baseline = public_baseline()
    runs = {r["id"]: r for r in baseline["report"]["runs"]}
    exchanges = baseline["model_exchanges"]
    assert len(exchanges) == 12 and len(runs) == 18
    assert {e["run_id"] for e in exchanges} == {r["id"] for r in runs.values() if r["model_calls"]}
    for exchange in exchanges:
        request, response = exchange["request"], exchange["response"]
        row = runs[exchange["run_id"]]
        content = json.loads(request["messages"][1]["content"])
        snapshot = {"project": {"sections": content["sections"]}}
        issues = json.loads(response["message"]["content"])["issues"]
        assert response["done"] is True
        assert request["options"] == {"temperature": 0, "seed": 42, "num_ctx": 16384, "num_predict": 1400}
        assert row["metrics"]["prompt_eval_count"] == response["prompt_eval_count"]
        assert row["metrics"]["eval_count"] == response["eval_count"]
        assert row["raw_locations"] == benchmark.location_counts(issues, snapshot, content["evidence"])
        assert row["final_locations"] == benchmark.location_counts(row["issues"], snapshot, content["evidence"])


def test_prompt_variant_changes_only_system_message_and_is_explicit(tmp_path):
    from server.proposal.review_prompt import SYSTEM_PROMPT
    from server.proposal.review_worker import LocalReviewModel
    content = {"sections": {"method": "合成计划"}, "evidence": [], "outline": [], "school": "合成大学"}
    production = benchmark.MeasuredModel(config(), tmp_path)
    calibrated = benchmark.MeasuredModel(config(prompt_variant="calibrated"), tmp_path)
    assert production.messages(content) == LocalReviewModel.messages(content)
    assert production.messages(content)[1] == calibrated.messages(content)[1]
    assert calibrated.messages(content)[0]["content"].startswith(SYSTEM_PROMPT)
    assert production.messages(content)[0]["content"] != calibrated.messages(content)[0]["content"]
    with pytest.raises(ValueError, match="未知"):
        benchmark.MeasuredModel(config(prompt_variant="unregistered"), tmp_path)


def test_switching_prompt_variant_cannot_resume_an_existing_experiment(tmp_path):
    output = tmp_path / "variant"
    original = benchmark.run_benchmark(output, config(prompt_variant="production"), IDENTITY, factory=FakeModel)
    assert original["manifest"]["prompt_version"] == benchmark.PROMPT_VERSION
    with pytest.raises(ValueError, match="不能混入"):
        benchmark.run_benchmark(output, config(prompt_variant="calibrated"), IDENTITY, resume=True, factory=FakeModel)


def test_additional_dataset_uses_its_own_inputs_and_targets(tmp_path):
    dataset = benchmark.load_dataset(benchmark.CASES_PATH.with_name("review_holdout_cases.json"))
    output = tmp_path / "additional"
    cfg = config(cases=["future_plan_ok"], arms=["rules"], prompt_variant="calibrated")
    result = benchmark.run_benchmark(output, cfg, IDENTITY, factory=FakeModel, dataset=dataset)
    assert result["manifest"]["dataset_digest"] == benchmark.digest(dataset)
    assert result["manifest"]["prompt_version"] == "review-calibration-v1"
    snapshot = benchmark.read_json(output / "inputs/future_plan_ok/snapshot.json")
    assert snapshot["project"]["state"]["budget"] == "8000元"
    assert result["runs"][0]["issues"] == []
