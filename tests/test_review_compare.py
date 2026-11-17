"""A shorter list of opinions cannot hide more missed problems."""
import pytest
from pathlib import Path

pytest.importorskip("langgraph.checkpoint.sqlite")

from server.evals.review_compare import CORE_SOURCES, compare_experiments, main
from server.evals.review_benchmark import build_report, read_json, write_json, location_counts


def bundle(fp=2, fn=1):
    semantic = {"true_positive": 3 - fn, "false_positive": fp, "false_negative": fn}
    request = {"model": "synthetic", "format": {"type": "object"}, "options": {"seed": 42},
               "messages": [{"role": "system", "content": "fixed"}, {"role": "user", "content": "same draft"}]}
    return {"report": {
        "manifest": {"dataset_digest": "data", "model": {"digest": "model"}, "runtime": {"python": "3.12"},
                     "graph_version": "v1", "prompt_version": "v1", "source_hashes": {k: "code" for k in CORE_SOURCES},
                     "trials": [{"id": "c-r1-graph", "case_id": "c", "repeat": 1, "arm": "graph"}]},
        "reviewer_kind": "agent", "runs": [{"id": "c-r1-graph", "case_id": "c", "repeat": 1, "arm": "graph",
                                             "semantic": {"missed_target_ids": [f"target{i}" for i in range(fn)]}}],
        "arms": {"graph": {"complete": 1, "semantic": semantic}}},
        "model_exchanges": [{"run_id": "c-r1-graph", "request": request}]}


@pytest.mark.parametrize("fp,fn,status", [
    (1, 1, "improved"), (2, 0, "improved"), (1, 2, "tradeoff"),
    (3, 1, "regressed"), (2, 2, "regressed"), (2, 1, "unchanged")])
def test_both_false_positives_and_misses_are_compared(fp, fn, status):
    candidate = bundle(fp, fn)
    candidate["model_exchanges"][0]["request"]["messages"][0]["content"] = "changed system"
    result = compare_experiments(bundle(), candidate)
    assert result["status"] == status
    assert result["meets_comparison_criteria"] == (status == "improved")
    assert result["reviewer_kind"] == {"baseline": "agent", "candidate": "agent"}


@pytest.mark.parametrize("field", ["data", "model", "code", "runtime", "request", "missing_run", "missing_exchange"])
def test_uncontrolled_experiment_cannot_be_compared(field):
    candidate = bundle()
    manifest = candidate["report"]["manifest"]
    if field == "data":
        manifest["dataset_digest"] = "other"
    elif field == "model":
        manifest["model"]["digest"] = "other"
    elif field == "code":
        manifest["source_hashes"][CORE_SOURCES[0]] = "other"
    elif field == "runtime":
        manifest["runtime"]["python"] = "other"
    elif field == "request":
        candidate["model_exchanges"][0]["request"]["messages"][1]["content"] = "different draft"
    elif field == "missing_run":
        candidate["report"]["runs"] = []
    else:
        candidate["model_exchanges"] = []
    with pytest.raises(ValueError):
        compare_experiments(bundle(), candidate)


def test_missing_reviews_and_incomplete_runs_are_not_improvements():
    candidate = bundle()
    candidate["report"]["arms"]["graph"]["semantic"] = None
    assert compare_experiments(bundle(), candidate)["status"] == "pending_review"
    candidate = bundle(fp=0, fn=0)
    candidate["report"]["arms"]["graph"]["complete"] = 0
    result = compare_experiments(bundle(), candidate)
    assert result["status"] == "incomplete" and result["meets_comparison_criteria"] is False


def test_different_review_methods_are_not_the_same_measurement():
    candidate = bundle()
    candidate["report"]["reviewer_kind"] = "human"
    with pytest.raises(ValueError, match="复核者"):
        compare_experiments(bundle(), candidate)


def test_equal_recall_does_not_hide_a_newly_missed_problem():
    candidate = bundle(fp=1, fn=1)
    candidate["report"]["runs"][0]["semantic"]["missed_target_ids"] = ["target1"]
    result = compare_experiments(bundle(), candidate)
    assert result["status"] == "tradeoff"
    assert result["meets_comparison_criteria"] is False
    assert result["newly_missed_targets"][0]["target_id"] == "target1"
    assert result["recovered_targets"][0]["target_id"] == "target0"


def test_prompt_manifest_must_match_the_actual_request():
    candidate = bundle()
    candidate["report"]["manifest"]["prompt_sha256"] = "invented"
    with pytest.raises(ValueError, match="提示词摘要"):
        compare_experiments(bundle(), candidate)


def test_published_calibration_is_reproducible_and_keeps_the_regression(tmp_path):
    root = Path(__file__).resolve().parents[1]
    archive = root / "server/evals/baselines/review_calibration_20261118.json"
    baseline = read_json(root / "server/evals/baselines/review_pilot_20261117.json")
    record = read_json(archive)
    for name, experiment in record["experiments"].items():
        original = experiment["report"]
        directory = tmp_path / name
        directory.mkdir()
        write_json(directory / "manifest.json", original["manifest"])
        for row in original["runs"]:
            run_dir = directory / "runs" / row["id"]
            run_dir.mkdir(parents=True)
            result = {k: v for k, v in row.items() if k not in {"id", "case_id", "arm", "repeat", "semantic"}}
            write_json(run_dir / "result.json", result)
        rebuilt = build_report(directory, experiment["dataset"], experiment["reviews"])
        assert rebuilt["arms"] == original["arms"]
        assert rebuilt["human_review_complete"] is False
    experiments = record["experiments"]
    assert compare_experiments(baseline, experiments["development_calibrated"]) == record["comparisons"]["development"]
    assert compare_experiments(experiments["additional_production"], experiments["additional_calibrated"]) == record["comparisons"]["additional"]
    assert all(x["status"] == "regressed" for x in record["comparisons"].values())
    output = tmp_path / "cli-comparison.json"
    assert main(["--baseline", str(archive), "--baseline-experiment", "additional_production",
                 "--candidate", str(archive), "--candidate-experiment", "additional_calibrated",
                 "--output", str(output)]) == 0
    assert read_json(output) == record["comparisons"]["additional"]


def test_calibration_records_match_actual_prompt_tokens_and_quotes():
    import hashlib
    import json
    record = read_json(Path(__file__).resolve().parents[1] / "server/evals/baselines/review_calibration_20261118.json")
    calls = 0
    for experiment in record["experiments"].values():
        rows = {r["id"]: r for r in experiment["report"]["runs"]}
        for exchange in experiment["model_exchanges"]:
            request, response = exchange["request"], exchange["response"]
            row = rows[exchange["run_id"]]
            calls += 1
            content = json.loads(request["messages"][1]["content"])
            snapshot = {"project": {"sections": content["sections"]}}
            assert response["done"] is True
            assert hashlib.sha256(request["messages"][0]["content"].encode()).hexdigest() == experiment["report"]["manifest"]["prompt_sha256"]
            assert row["metrics"]["prompt_eval_count"] == response["prompt_eval_count"]
            assert row["metrics"]["eval_count"] == response["eval_count"]
            assert row["raw_locations"] == location_counts(json.loads(response["message"]["content"])["issues"], snapshot, content["evidence"])
    assert calls == 14
