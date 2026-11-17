#!/usr/bin/env python3
"""Compare two reviewed prompt experiments while checking their actual inputs."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from server.evals.review_benchmark import read_json, write_json

CORE_SOURCES = (
    "server/proposal/review_graph.py", "server/proposal/review_prompt.py",
    "server/proposal/review_evidence.py", "server/proposal/review_verification.py",
    "server/proposal/review_runs.py", "server/proposal/review_worker.py",
    "server/proposal/retrieval.py", "server/proposal/tools.py",
    "server/proposal/completeness.py", "server/proposal/storage.py",
)


def compare_experiments(baseline, candidate, arm="graph"):
    left, right = baseline["report"], candidate["report"]
    a, b = left["manifest"], right["manifest"]
    if a["dataset_digest"] != b["dataset_digest"]:
        raise ValueError("数据集不同，不能作为提示词对照")
    if a["model"]["digest"] != b["model"]["digest"]:
        raise ValueError("模型摘要不同")
    if a["runtime"] != b["runtime"]:
        raise ValueError("执行环境不同")
    if a["graph_version"] != b["graph_version"]:
        raise ValueError("图版本不同")
    if any(a["source_hashes"].get(name) != b["source_hashes"].get(name) for name in CORE_SOURCES):
        raise ValueError("生产流程源码不同，不能将差异归因于提示词")
    def index(report):
        rows = [r for r in report["runs"] if r["arm"] == arm]
        keys = [(r["case_id"], r["repeat"]) for r in rows]
        if len(keys) != len(set(keys)):
            raise ValueError("重复评测运行")
        return dict(zip(keys, rows))
    old_rows, new_rows = index(left), index(right)
    if not old_rows or set(old_rows) != set(new_rows):
        raise ValueError("样例或重复次数不同")
    selected = [t for t in a["trials"] if t["arm"] == arm]
    other = [t for t in b["trials"] if t["arm"] == arm]
    if (len(selected) != len(old_rows) or len(other) != len(new_rows)
            or {(t["case_id"], t["repeat"]) for t in selected} != set(old_rows)
            or {(t["case_id"], t["repeat"]) for t in other} != set(new_rows)):
        raise ValueError("比较必须包含全部计划运行，不能只挑已完成样例")
    exchanges = []
    for bundle in (baseline, candidate):
        items = bundle.get("model_exchanges", [])
        ids = [e["run_id"] for e in items]
        if len(ids) != len(set(ids)):
            raise ValueError("重复模型记录")
        exchanges.append(dict(zip(ids, items)))
    checked = 0
    prompt_hashes = [set(), set()]
    for key in old_rows:
        old, new = old_rows[key], new_rows[key]
        first, second = exchanges[0].get(old["id"]), exchanges[1].get(new["id"])
        if not first or not second:
            raise ValueError("缺少实际请求，不能证明比较输入一致")
        one, two = copy.deepcopy(first["request"]), copy.deepcopy(second["request"])
        for i, request in enumerate((one, two)):
            if len(request.get("messages", [])) != 2 or request["messages"][0].get("role") != "system":
                raise ValueError("模型消息结构不符")
            prompt_hashes[i].add(hashlib.sha256(request["messages"][0]["content"].encode()).hexdigest())
            request["messages"][0]["content"] = "<controlled-system-prompt>"
        if one != two:
            raise ValueError("实际草稿、证据、schema或生成参数不同")
        checked += 1
    if any(len(values) != 1 for values in prompt_hashes):
        raise ValueError("同一实验存在不同提示词，不能当作固定版本比较")
    actual_hashes = [next(iter(values)) for values in prompt_hashes]
    for manifest, actual in zip((a, b), actual_hashes):
        if manifest.get("prompt_sha256") not in (None, actual):
            raise ValueError("提示词摘要与实际请求不一致")
    before, after = left["arms"][arm], right["arms"][arm]
    outcome = {
        "version": 1, "arm": arm, "pairs": checked, "actual_inputs_matched": True,
        "baseline_prompt_version": a["prompt_version"], "candidate_prompt_version": b["prompt_version"],
        "baseline_prompt_sha256": actual_hashes[0], "candidate_prompt_sha256": actual_hashes[1],
        "baseline": before, "candidate": after,
        "scope": "fixed synthetic regression comparison; reviewer kind must be reported; not production validation",
        "reviewer_kind": {"baseline": left.get("reviewer_kind"), "candidate": right.get("reviewer_kind")},
    }
    if before["complete"] != len(old_rows) or after["complete"] != len(new_rows):
        outcome.update(status="incomplete", meets_comparison_criteria=False)
        return outcome
    old, new = before.get("semantic"), after.get("semantic")
    if old is None or new is None:
        outcome.update(status="pending_review", meets_comparison_criteria=False)
        return outcome
    if left.get("reviewer_kind") != right.get("reviewer_kind") or left.get("reviewer_kind") not in {"agent", "human"}:
        raise ValueError("复核者类型不同，不能按同一口径比较")
    keys = ("true_positive", "false_positive", "false_negative")
    if any(type(value.get(k)) is not int or value[k] < 0 for value in (old, new) for k in keys):
        raise ValueError("问题计数无效")
    if old["true_positive"] + old["false_negative"] != new["true_positive"] + new["false_negative"]:
        raise ValueError("目标问题数量不同")
    newly_missed, recovered = [], []
    for key, before_row in old_rows.items():
        after_row = new_rows[key]
        if before_row.get("semantic") is None or after_row.get("semantic") is None:
            raise ValueError("缺少逐运行复核结果")
        old_misses = set(before_row["semantic"]["missed_target_ids"])
        new_misses = set(after_row["semantic"]["missed_target_ids"])
        newly_missed.extend({"case_id": key[0], "repeat": key[1], "target_id": target} for target in sorted(new_misses - old_misses))
        recovered.extend({"case_id": key[0], "repeat": key[1], "target_id": target} for target in sorted(old_misses - new_misses))
    better_fp = new["false_positive"] < old["false_positive"]
    better_fn = new["false_negative"] < old["false_negative"]
    worse_fp = new["false_positive"] > old["false_positive"]
    worse_fn = new["false_negative"] > old["false_negative"]
    if (better_fp or better_fn) and not (worse_fp or worse_fn):
        status = "improved"
    elif (better_fp or better_fn) and (worse_fp or worse_fn):
        status = "tradeoff"
    elif worse_fp or worse_fn:
        status = "regressed"
    else:
        status = "unchanged"
    if newly_missed and status in {"improved", "unchanged"}:
        status = "tradeoff"
    outcome.update(status=status, meets_comparison_criteria=status == "improved",
                   delta_false_positive=new["false_positive"] - old["false_positive"],
                   delta_false_negative=new["false_negative"] - old["false_negative"],
                   newly_missed_targets=newly_missed, recovered_targets=recovered)
    return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--baseline-experiment")
    parser.add_argument("--candidate-experiment")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("请使用新的比较输出文件，保留已有记录")
    def select(path, experiment):
        value = read_json(path)
        if experiment:
            value = value["experiments"][experiment]
        if "report" not in value:
            raise ValueError("请指定组合记录中的实验名称")
        return value
    result = compare_experiments(select(args.baseline, args.baseline_experiment), select(args.candidate, args.candidate_experiment))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, result)
    print(json.dumps({k: result[k] for k in ("status", "pairs", "meets_comparison_criteria")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
