#!/usr/bin/env python3
"""Paired local-model review evaluation with auditable, separate semantic review."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import fcntl
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import platform
import re
import statistics
import sys
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from server.proposal.review_graph import ModelReview
from server.proposal.review_runs import GRAPH_VERSION, PROMPT_VERSION, ReviewRuns, capture_snapshot, digest
from server.proposal.review_worker import LocalReviewModel, ReviewWorker
from server.proposal.storage import Store
from server.evals.review_prompt_variants import CALIBRATION_VERSION, prompt_for_variant

CASES_PATH = Path(__file__).with_name("review_cases.json")
ARMS = ("rules", "direct", "graph")
SOURCES = (
    "server/evals/review_benchmark.py", "server/evals/review_cases.json",
    "server/evals/review_prompt_variants.py", "server/evals/review_holdout_cases.json",
    "server/proposal/review_graph.py", "server/proposal/review_prompt.py",
    "server/proposal/review_runs.py", "server/proposal/review_worker.py",
    "server/proposal/retrieval.py", "server/proposal/tools.py",
    "server/proposal/completeness.py", "server/proposal/storage.py",
)


def write_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_dataset(dataset):
    ids = [case["id"] for case in dataset["cases"]]
    if len(ids) != len(set(ids)) or any(not i.replace("_", "").isalnum() for i in ids):
        raise ValueError("重复或不安全的用例ID")
    for case in dataset["cases"]:
        targets = [target["id"] for target in case["targets"]]
        if len(targets) != len(set(targets)):
            raise ValueError("重复的问题标签")
    return dataset


def load_dataset(path=CASES_PATH):
    return validate_dataset(read_json(path))


def source_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def model_identity(base_url, model):
    # Reuse the production local review URL guard; no cloud key or user DB.
    local = LocalReviewModel(base_url)
    with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as client:
        response = client.get(local.base_url + "/api/tags")
        response.raise_for_status()
        tags = response.json()
    for item in tags.get("models", []):
        if item.get("name") in {model, model + ":latest"}:
            if not re.fullmatch(r"[0-9a-f]{64}", item.get("digest", "")):
                raise ValueError("模型缺少有效摘要")
            return {"name": model, "digest": item["digest"], "details": item.get("details", {})}
    raise ValueError("本机未安装指定模型")


class MeasuredModel(LocalReviewModel):
    """Same system prompt/schema/settings for both model arms; one HTTP attempt."""
    def __init__(self, config, directory):
        super().__init__(config["base_url"])
        self.config, self.directory = config, Path(directory)
        self.system_prompt = prompt_for_variant(config.get("prompt_variant", "production"))
        self.observation = {"calls": 0, "metrics": None, "content": None, "evidence": []}

    def messages(self, content):
        return [{"role": "system", "content": self.system_prompt},
                {"role": "user", "content": json.dumps(content, ensure_ascii=False)}]

    def __call__(self, run, content, schema):
        if self.observation["calls"]:
            raise RuntimeError("一次评测最多调用模型一次")
        if not self.accepts_input(content, schema):
            raise ValueError("输入超出模型预算，不截断或扩大上下文")
        if model_identity(self.base_url, run["model"]) != self.config["model_identity"]:
            raise ValueError("模型摘要已变化，未发送推理请求")
        request = {"model": run["model"], "stream": False, "format": schema,
                   "messages": self.messages(content), "options": {
                       "temperature": 0, "seed": self.config["seed"],
                       "num_ctx": self.context_size, "num_predict": self.output_budget}}
        self.observation.update(calls=1, evidence=content["evidence"])
        write_json(self.directory / "request.json", request)
        started = time.monotonic()
        with httpx.Client(timeout=self.config["timeout"], follow_redirects=False, trust_env=False) as client:
            response = client.post(self.base_url + "/api/chat", json=request)
            if response.status_code != 200 or len(response.content) > 100_000:
                raise ValueError("模型响应未确认")
            body = response.json()
        write_json(self.directory / "response.json", body)
        metrics = {"latency_ms": round((time.monotonic() - started) * 1000)}
        for key in ("prompt_eval_count", "eval_count"):
            value = body.get(key)
            metrics[key] = value if type(value) is int and value >= 0 else None
        text = body.get("message", {}).get("content")
        self.observation.update(metrics=metrics, content=text, done_reason=body.get("done_reason"))
        if model_identity(self.base_url, run["model"]) != self.config["model_identity"]:
            raise ValueError("推理期间模型摘要发生变化")
        if not isinstance(text, str) or body.get("done_reason") == "length":
            raise ValueError("模型响应缺失或截断")
        return {"content": text, "metrics": metrics}


def seed_project(dataset, case, directory):
    store = Store(directory)
    base = dataset["base_project"]
    project = store.create_project(base["title"], school=base["school"])
    store.update_project(project["id"], state=base["state"], outline=base["outline"],
                         sections={**base["sections"], **case["sections"]})
    for item in dataset["base_documents"] + case.get("documents", []):
        doc_id = uuid.uuid5(uuid.NAMESPACE_URL, "review-benchmark:" + case["id"] + ":" + item["id"]).hex
        doc = store.add_document(project["id"], item["name"], "synthetic-not-read-from-disk",
                                 item["kind"], "text/plain", document_id=doc_id)
        store.update_document_extraction(doc["id"], item["text"], "completed",
                                         confirmed=item["confirmed"], confirmed_by_user=item["confirmed"])
        if item["confirmed"]:
            store.replace_chunks(doc["id"], [{"content": item["text"], "source_ref": "合成资料第1段"}], expected_text=item["text"])
            # Stabilize IDs included in model input across fresh reproductions.
            with store.connect() as db:
                db.execute("UPDATE chunks SET id=? WHERE document_id=?", (uuid.uuid5(uuid.NAMESPACE_URL, doc_id).hex, doc_id))
    return store, project["id"]


def location_counts(issues, snapshot, evidence):
    sections = snapshot["project"]["sections"]
    sources = {item["id"]: item for item in evidence}
    counts = Counter()
    for issue in issues:
        if issue.get("origin") == "rule":
            continue  # Structural rule outputs do not claim quoted model spans.
        counts["model_issues"] += 1
        if issue.get("draft_quote") and issue["draft_quote"] in sections.get(issue.get("section_id"), ""):
            counts["located_issues"] += 1
        for citation in issue.get("citations", []):
            counts["citations"] += 1
            source = sources.get(citation.get("id"))
            if source and citation.get("quote") and citation["quote"] in source["content"]:
                counts["matched_citations"] += 1
    return {key: counts[key] for key in ("model_issues", "located_issues", "citations", "matched_citations")}


def execute_trial(store, project_id, arm, caller, directory):
    runs = ReviewRuns(store)
    if arm == "direct":
        with store.connect() as db:
            db.execute("BEGIN")
            snapshot = capture_snapshot(store, db, project_id)
        run = {"model": caller.config["model"], "fingerprint": digest(snapshot)}
    else:
        run = runs.submit(project_id, directory.name, runs.current(project_id),
                          "rules" if arm == "rules" else "model", caller.config["model"] if arm != "rules" else "")
        snapshot = json.loads(runs.raw(run["id"])["snapshot"])
    started = time.monotonic()
    status, error, issues, complete, events = "completed", None, [], False, []
    try:
        if arm == "direct":
            content = {key: snapshot["project"][key] for key in ("sections", "outline", "school")}
            content["evidence"] = [{"id": c["id"], "document_id": c["document_id"],
                                    "source_name": c["source_name"], "source_ref": c["source_ref"],
                                    "content": c["content"]} for c in snapshot["chunks"]]
            raw = caller(run, content, ModelReview.model_json_schema())
            issues = [{**issue.model_dump(), "origin": "model"} for issue in ModelReview.model_validate_json(raw["content"]).issues]
            complete = True
        else:
            with ReviewWorker(store, model_call=caller) as worker:
                result = worker.run_once()
            status, error, events = result["status"], result["error"], result["events"]
            report = result["report"] or {}
            issues, complete = report.get("issues", []), report.get("complete", False)
    except Exception as exc:
        status, error = "failed", type(exc).__name__
    # Check project state and facts, not review-owned rows/checkpoints/timestamps.
    after = store.get_project(project_id)
    immutable = all(after[key] == snapshot["project"][key] for key in ("sections", "state", "outline", "material_revision"))
    observation = caller.observation
    evidence = observation["evidence"]
    raw_issues, raw_valid = [], None
    if observation["content"] is not None:
        try:
            raw_issues = [i.model_dump() for i in ModelReview.model_validate_json(observation["content"]).issues]
            raw_valid = True
        except (ValueError, TypeError):
            raw_valid = False
    return {"status": status, "error": error, "complete": complete, "issues": issues,
            "duration_ms": round((time.monotonic() - started) * 1000), "events": events,
            "draft_unchanged": immutable, "model_calls": observation["calls"],
            "metrics": observation["metrics"], "raw_schema_valid": raw_valid,
            "raw_locations": location_counts(raw_issues, snapshot, evidence),
            "final_locations": location_counts(issues, snapshot, evidence),
            "evidence_document_ids": sorted({item["document_id"] for item in evidence}),
            "input_fingerprint": run["fingerprint"]}


def ratio(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def semantic_score(result, case, review, reviewer_kind):
    """Metrics require an explicit, evidence-backed decision for every issue."""
    targets = {target["id"] for target in case["targets"]}
    if review is None or reviewer_kind not in {"agent", "human"}:
        return None
    if review.get("result_sha256") != digest(result):
        raise ValueError("复核记录不匹配当前输出")
    decisions = review.get("issues", [])
    if len(decisions) != len(result["issues"]) or [d.get("index") for d in decisions] != list(range(len(decisions))):
        raise ValueError("必须逐项复核，不能漏掉模型意见")
    matched, false_positive, outside = set(), 0, 0
    for decision, issue in zip(decisions, result["issues"]):
        verdict, expected = decision.get("verdict"), decision.get("target_id")
        if verdict == "pending":
            return None
        if not decision.get("reason", "").strip() or decision.get("evidence") != issue["problem"]:
            raise ValueError("每项复核需要实际问题原文及理由")
        if verdict == "matched":
            if expected not in targets:
                raise ValueError("复核引用了不存在的问题标签")
            if expected in matched:
                false_positive += 1  # Duplicating an issue does not add recall.
            else:
                matched.add(expected)
        elif verdict == "spurious":
            false_positive += 1
        elif verdict == "out_of_scope":
            outside += 1
        else:
            raise ValueError("复核决定无效")
    citation_decisions = review.get("citations", [])
    citations = [(i, j, c) for i, issue in enumerate(result["issues"]) for j, c in enumerate(issue.get("citations", []))]
    support = None
    if citation_decisions:
        if [(d.get("issue_index"), d.get("citation_index")) for d in citation_decisions] != [(i, j) for i, j, _ in citations]:
            raise ValueError("引用语义支持率需要逐条复核所有引用")
        for decision, (_, _, citation) in zip(citation_decisions, citations):
            if decision.get("quote") != citation["quote"] or (decision.get("verdict") != "pending" and not decision.get("reason")):
                raise ValueError("引用复核缺少实际引文或理由")
            if decision.get("verdict") not in {"supports", "does_not_support", "pending"}:
                raise ValueError("引用复核决定无效")
        if all(d["verdict"] != "pending" for d in citation_decisions):
            support = {"supported": sum(d["verdict"] == "supports" for d in citation_decisions), "total": len(citations)}
    return {"true_positive": len(matched), "false_positive": false_positive,
            "false_negative": len(targets - matched), "out_of_scope": outside,
            "missed_target_ids": sorted(targets - matched), "citation_support": support}


def build_report(output, dataset, reviews=None):
    output = Path(output)
    manifest = read_json(output / "manifest.json")
    if digest(dataset) != manifest["dataset_digest"]:
        raise ValueError("评测资料与运行时保存的版本不一致")
    cases = {case["id"]: case for case in dataset["cases"]}
    rows, template = [], {"reviewer_kind": "", "reviewer_id": "", "runs": {}}
    if reviews and (reviews.get("reviewer_kind") not in {"human", "agent"} or not reviews.get("reviewer_id", "").strip()):
        raise ValueError("请明确复核者类型和标识；Agent复核不能记为人工")
    for trial in manifest["trials"]:
        path = output / "runs" / trial["id"] / "result.json"
        if not path.exists():
            continue
        result = read_json(path)
        row = {**trial, **result}
        review = (reviews or {}).get("runs", {}).get(trial["id"])
        row["semantic"] = semantic_score(result, cases[trial["case_id"]], review, (reviews or {}).get("reviewer_kind"))
        rows.append(row)
        template["runs"][trial["id"]] = {"result_sha256": digest(result), "targets": cases[trial["case_id"]]["targets"],
            "issues": [{"index": i, "verdict": "pending", "target_id": None, "evidence": issue["problem"], "reason": ""}
                       for i, issue in enumerate(result["issues"])],
            "citations": [{"issue_index": i, "citation_index": j, "quote": c["quote"], "verdict": "pending", "reason": ""}
                          for i, issue in enumerate(result["issues"]) for j, c in enumerate(issue.get("citations", []))]}
    arms = {}
    for arm in manifest["config"]["arms"]:
        selected = [row for row in rows if row["arm"] == arm]
        counts = Counter()
        for row in selected:
            counts.update(row["final_locations"])
        semantics = [row["semantic"] for row in selected]
        planned = sum(t["arm"] == arm for t in manifest["trials"])
        fully_reviewed = bool(selected) and len(selected) == planned and all(item is not None for item in semantics)
        quality = None
        if fully_reviewed:
            tp, fp, fn, outside = (sum(item[key] for item in semantics) for key in ("true_positive", "false_positive", "false_negative", "out_of_scope"))
            supports = [s["citation_support"] for s in semantics if s["citation_support"] is not None]
            all_citations_reviewed = sum(s["total"] for s in supports) == counts["citations"]
            quality = {"precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
                       "true_positive": tp, "false_positive": fp, "false_negative": fn, "out_of_scope": outside,
                       "citation_support_rate": ratio(sum(s["supported"] for s in supports), counts["citations"]) if all_citations_reviewed else None,
                       "reviewer_kind": reviews["reviewer_kind"]}
        latencies = [row["duration_ms"] for row in selected if row["status"] != "interrupted"]
        token_metrics = [row["metrics"] for row in selected if row["metrics"]]
        arms[arm] = {"planned": planned, "finished": len(selected),
                     "complete": sum(row["complete"] for row in selected), "failed_or_incomplete": sum(not row["complete"] for row in selected),
                     "median_duration_ms": round(statistics.median(latencies)) if latencies else None,
                     "model_calls": sum(row["model_calls"] for row in selected),
                     "reported_prompt_tokens": sum(m["prompt_eval_count"] for m in token_metrics if m.get("prompt_eval_count") is not None),
                     "reported_output_tokens": sum(m["eval_count"] for m in token_metrics if m.get("eval_count") is not None),
                     "unknown_usage_runs": sum(row["model_calls"] > 0 and (row["metrics"] is None or any(row["metrics"].get(k) is None for k in ("prompt_eval_count", "eval_count"))) for row in selected),
                     "quote_location_rate": ratio(counts["located_issues"], counts["model_issues"]),
                     "citation_text_match_rate": ratio(counts["matched_citations"], counts["citations"]),
                     "semantic": quality}
    report = {"suite": dataset["name"], "curation": dataset["curation"], "manifest": manifest,
              "reviewer_kind": (reviews or {}).get("reviewer_kind"), "reviewer_id": (reviews or {}).get("reviewer_id"),
              "human_review_complete": bool(rows) and len(rows) == len(manifest["trials"]) and bool(reviews) and reviews.get("reviewer_kind") == "human" and all(row["semantic"] is not None and (row["final_locations"]["citations"] == 0 or row["semantic"]["citation_support"] is not None) for row in rows),
              "arms": arms, "runs": rows, "cost": {"provider_api_micro_cny": 0, "hardware_and_electricity_micro_cny": None,
              "scope": "local Ollama; no platform credits or external model API"}}
    write_json(output / "report.json", report)
    write_json(output / "review-template.json", template)
    lines = ["# 草稿预审对比试点", "", "样例由 Agent 编写；逐字引用匹配不等于语义支持。独立人工复核：" + ("已完成" if report["human_review_complete"] else "未完成"), "",
             "提示词版本：" + manifest["prompt_version"] + "；资料摘要：" + manifest["dataset_digest"], "",
             "| 路径 | 完整/计划 | 中位耗时(ms) | 原文定位率 | 引文匹配率 | 问题精确率 | 问题召回率 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for arm, value in arms.items():
        quality = value["semantic"] or {}
        values = [arm, f"{value['complete']}/{value['planned']}", value["median_duration_ms"], value["quote_location_rate"], value["citation_text_match_rate"], quality.get("precision"), quality.get("recall")]
        lines.append("| " + " | ".join("待复核/无分母" if v is None else str(v) for v in values) + " |")
    lines.extend(["", "语义指标复核者：" + str(report["reviewer_kind"] or "未复核") + "。失败与不完整运行保留在结果中，不用成功重试替换。",
                  "本机模型无按次供应商API费用；硬件和电力成本未测量。Token仅汇总已报告值。", "",
                  "## 实际运行", ""])
    lines.extend(f"- `{row['id']}`：{row['status']}，完整={row['complete']}，问题数={len(row['issues'])}，错误={row['error']}" for row in rows)
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    csv_file = io.StringIO()
    writer = csv.DictWriter(csv_file, fieldnames=["id", "case_id", "arm", "status", "complete", "duration_ms", "model_calls"])
    writer.writeheader()
    writer.writerows({key: row[key] for key in writer.fieldnames} for row in rows)
    (output / "results.csv").write_text(csv_file.getvalue(), encoding="utf-8")
    return report


def run_benchmark(output, config, identity, *, resume=False, factory=MeasuredModel, dataset=None):
    dataset, output = validate_dataset(dataset) if dataset is not None else load_dataset(), Path(output).resolve()
    variant = config.get("prompt_variant", "production")
    effective_prompt = prompt_for_variant(variant)
    selected = [case for case in dataset["cases"] if case["id"] in config["cases"]]
    if (len(selected) != len(config["cases"]) or not selected or not config["arms"]
            or len(set(config["arms"])) != len(config["arms"]) or set(config["arms"]) - set(ARMS)):
        raise ValueError("无效的用例或对比路径")
    manifest = {"version": 1, "config": config, "model": identity, "source_hashes": source_hashes(), "dataset_digest": digest(dataset),
                "runtime": {"python": platform.python_version(), "machine": platform.machine(),
                            "packages": {name: importlib.metadata.version(name) for name in ("langgraph", "langgraph-checkpoint-sqlite", "httpx", "pydantic")}},
                "graph_version": GRAPH_VERSION,
                "prompt_version": PROMPT_VERSION if variant == "production" else CALIBRATION_VERSION,
                "prompt_sha256": hashlib.sha256(effective_prompt.encode()).hexdigest(), "trials": []}
    for repeat in range(config["repeats"]):
        for index, case in enumerate(selected):
            order = list(config["arms"])
            if (index + repeat) % 2:
                order.reverse()  # Counterbalance cold-start/order effects.
            for arm in order:
                manifest["trials"].append({"id": f"{case['id']}-r{repeat + 1}-{arm}", "case_id": case["id"], "arm": arm, "repeat": repeat + 1})
    if resume:
        if read_json(output / "manifest.json") != manifest:
            raise ValueError("代码、模型、数据或配置变化，不能混入已有评测")
    else:
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / "manifest.json", manifest)
        write_json(output / "dataset.json", dataset)
    with (output / ".run.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for trial in manifest["trials"]:
            if source_hashes() != manifest["source_hashes"]:
                raise ValueError("执行期间源码发生变化，请保留结果并新建评测目录")
            directory = output / "runs" / trial["id"]
            directory.mkdir(parents=True, exist_ok=True)
            if (directory / "result.json").exists():
                continue
            case = next(case for case in selected if case["id"] == trial["case_id"])
            if (directory / "attempt.json").exists():
                result = {"status": "interrupted", "error": "attempt_outcome_unknown_not_replayed", "complete": False, "issues": [],
                          "duration_ms": None, "events": [], "draft_unchanged": None, "model_calls": int(trial["arm"] != "rules"),
                          "metrics": None, "raw_schema_valid": None, "raw_locations": location_counts([], {"project": {"sections": {}}}, []),
                          "final_locations": location_counts([], {"project": {"sections": {}}}, []), "evidence_document_ids": [], "input_fingerprint": None}
            else:
                case_dir = output / "inputs" / trial["case_id"]
                marker = case_dir / "project.json"
                if marker.exists():
                    store, project_id = Store(case_dir / "store"), read_json(marker)["id"]
                else:
                    case_dir.mkdir(parents=True, exist_ok=True)
                    store, project_id = seed_project(dataset, case, case_dir / "store")
                    write_json(marker, {"id": project_id})
                    runs = ReviewRuns(store)
                    with store.connect() as db:
                        db.execute("BEGIN")
                        write_json(case_dir / "snapshot.json", capture_snapshot(store, db, project_id))
                write_json(directory / "attempt.json", trial)
                caller = factory({**config, "model_identity": identity, "seed": config["seed"] + trial["repeat"] - 1}, directory)
                result = execute_trial(store, project_id, trial["arm"], caller, directory)
            write_json(directory / "result.json", result)
            build_report(output, dataset)
            print(f"{trial['id']}: {result['status']} complete={result['complete']}", flush=True)
    return build_report(output, dataset)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cases")
    parser.add_argument("--dataset", type=Path, default=CASES_PATH)
    parser.add_argument("--prompt-variant", choices=("production", "calibrated"), default="production")
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--repeats", type=int, choices=range(1, 11), default=1)
    parser.add_argument("--timeout", type=int, choices=range(1, 121), default=120)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--reviews", type=Path)
    args = parser.parse_args(argv)
    if args.report_only:
        build_report(args.output, read_json(args.output / "dataset.json"), read_json(args.reviews) if args.reviews else None)
        return 0
    if args.reviews:
        parser.error("--reviews 只用于 --report-only，不能触发模型重跑")
    dataset = load_dataset(args.dataset)
    config = {key: getattr(args, key) for key in ("model", "base_url", "seed", "repeats", "timeout", "prompt_variant")}
    config.update(cases=args.cases.split(",") if args.cases else [c["id"] for c in dataset["cases"]], arms=args.arms.split(","))
    report = run_benchmark(args.output, config, model_identity(args.base_url, args.model), resume=args.resume, dataset=dataset)
    return 0 if all(row["complete"] for row in report["runs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
