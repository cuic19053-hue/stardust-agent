#!/usr/bin/env python3
"""Compare prompt-only and schema-constrained extraction on synthetic dialogues.

Uses installed Ollama models only; no app store, .env, credentials or cloud API.
Raw responses, errors and all trial scores are retained; no repair or retry.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from server.proposal.student_intake import (IntakeCandidate, SYSTEM_PROMPT,
    evidence_errors, extraction_messages, follow_up_questions)

DATASET = ROOT / "server/evals/fixtures/student_intake/v1/cases.json"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_cases(path=DATASET):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("synthetic") is not True or data.get("annotation_status") != "agent_authored_not_human_reviewed":
        raise ValueError("Only explicitly labelled synthetic pilot data supported")
    ids = [x["id"] for x in data["cases"]]
    if len(ids) != len(set(ids)) or not ids:
        raise ValueError("duplicate or empty case IDs")
    for case in data["cases"]:
        extraction_messages(case["turns"])
        gold = IntakeCandidate.model_validate({"schema_version": "student-intake-v1",
            "facts": [{k: v for k, v in fact.items() if k != "aliases"} for fact in case["gold"]],
            "follow_up_fields": case["required_follow_up"]})
        if evidence_errors(gold, case["turns"]):
            raise ValueError("invalid gold evidence: " + case["id"])
    return data


def score(case, raw):
    result = {"json_valid": False, "schema_valid": False, "evidence_valid": False,
              "tp": 0, "fp": 0, "fn": len(case["gold"]), "predicted": 0,
              "evidence_supported_matches": 0, "required_questions": len(case["required_follow_up"]),
              "question_hits": 0, "duplicate_facts": 0, "forbidden_field_count": 0,
              "withheld_question_count": 0, "case_exact": False}
    try:
        parsed = json.loads(raw)
        result["json_valid"] = True
        candidate = IntakeCandidate.model_validate(parsed)
        result["schema_valid"] = True
    except (ValueError, TypeError) as exc:
        result["validation_error"] = str(exc)[:1500]
        return result
    errors = evidence_errors(candidate, case["turns"])
    result["evidence_errors"] = errors
    result["evidence_valid"] = not errors
    result["predicted"] = len(candidate.facts)
    unmatched = list(case["gold"])
    seen = Counter((f.field, f.record_id) for f in candidate.facts)
    result["duplicate_facts"] = sum(n - 1 for n in seen.values())
    for fact in candidate.facts:
        hit = next((g for g in unmatched if g["field"] == fact.field
                    and g["record_id"] == fact.record_id and g["status"] == fact.status
                    and fact.value in [g["value"], *g.get("aliases", [])]), None)
        if hit is None:
            result["fp"] += 1
            continue
        result["tp"] += 1
        unmatched.remove(hit)
        # An anchor includes subject/uncertainty/negation/correction context;
        # a mere numeric substring is not enough. Conflict needs both anchors.
        if all(any(e.turn_id == anchor["turn_id"] and anchor["quote"] in e.quote
                   for e in fact.evidence) for anchor in hit["evidence"]):
            result["evidence_supported_matches"] += 1
    result["fn"] = len(unmatched)
    result["missing"] = [{k: x[k] for k in ("field", "record_id", "value", "status")} for x in unmatched]
    result["question_hits"] = len(set(case["required_follow_up"]) & set(candidate.follow_up_fields))
    result["forbidden_field_count"] = sum(f.field in case["forbidden_fields"] for f in candidate.facts)
    withheld = {g["field"] for g in case["gold"] if g["status"] == "withheld"}
    result["withheld_question_count"] = len(withheld & set(candidate.follow_up_fields))
    result["questions"] = follow_up_questions(candidate)
    result["case_exact"] = (not result["fp"] and not result["fn"] and not errors
        and result["evidence_supported_matches"] == len(case["gold"])
        and result["question_hits"] == result["required_questions"]
        and not result["withheld_question_count"] and not result["duplicate_facts"])
    return result


def summarize(rows):
    def group(items):
        n = len(items)
        totals = {k: sum(r["score"][k] for r in items) for k in (
            "tp", "fp", "fn", "predicted", "evidence_supported_matches", "required_questions",
            "question_hits", "forbidden_field_count", "withheld_question_count", "duplicate_facts")}
        tp, fp, fn = (totals[k] for k in ("tp", "fp", "fn"))
        times = sorted(r["wall_seconds"] for r in items)
        return {"trials": n, "errors": sum(bool(r.get("error")) for r in items),
            "json_valid_rate": sum(r["score"]["json_valid"] for r in items) / n,
            "schema_valid_rate": sum(r["score"]["schema_valid"] for r in items) / n,
            "evidence_valid_rate": sum(r["score"]["evidence_valid"] for r in items) / n,
            "fact_precision": tp / (tp + fp) if tp + fp else None,
            "fact_recall": tp / (tp + fn) if tp + fn else None,
            "fact_f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
            "evidence_supported_recall": totals["evidence_supported_matches"] / (tp + fn) if tp + fn else None,
            "required_question_recall": totals["question_hits"] / totals["required_questions"] if totals["required_questions"] else None,
            "exact_cases": sum(r["score"]["case_exact"] for r in items),
            "p50_seconds": statistics.median(times),
            "p95_seconds": times[math.ceil(n * .95) - 1],
            "reported_output_tokens": sum(r.get("usage", {}).get("eval_count", 0) for r in items),
            "counts": totals}
    modes = sorted({r["mode"] for r in rows})
    return {mode: {"overall": group([r for r in rows if r["mode"] == mode]),
                  "by_tag": {tag: group([r for r in rows if r["mode"] == mode and tag in r["tags"]])
                             for tag in sorted({t for r in rows for t in r["tags"]})
                             if any(r["mode"] == mode and tag in r["tags"] for r in rows)}} for mode in modes}


def request(path, payload=None, timeout=5):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request("http://127.0.0.1:11434" + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--mode", choices=("prompt", "schema", "both"), default="both")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 10 or not 0 < args.timeout <= 300:
        parser.error("repeat must be 1..10; timeout must be (0, 300]")
    data = load_cases(args.dataset)
    cases = [c for c in data["cases"] if not args.cases or c["id"] in args.cases]
    if not cases or (args.cases and set(args.cases) - {c["id"] for c in cases}):
        parser.error("unknown/empty case selection")
    installed = request("/api/tags")["models"]
    model = next((m for m in installed if m["name"] in {args.model, args.model + ":latest"}), None)
    if not model:
        parser.error("requested model is not installed; no automatic download")
    args.output.mkdir(parents=True, exist_ok=False)
    schema = IntakeCandidate.model_json_schema()
    files = [Path(__file__), ROOT / "server/proposal/student_intake.py", args.dataset.resolve()]
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "model": model["name"],
        "model_digest": model["digest"], "endpoint": "http://127.0.0.1:11434",
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_sha256": {str(p.relative_to(ROOT)): sha(p) for p in files},
        "dataset_id": data["dataset_id"], "annotation_status": data["annotation_status"],
        "case_ids": [c["id"] for c in cases], "mode": args.mode, "repeat": args.repeat,
        "options": {"temperature": 0, "seed": 42, "num_ctx": 8192, "num_predict": 2048},
        "timeout_seconds": args.timeout, "retries": 0,
        "scope": "offline extraction pilot, not product end-to-end or human gold evaluation"}
    write(args.output / "manifest.json", manifest)
    write(args.output / "schema.json", schema)
    (args.output / "system_prompt.txt").write_text(SYSTEM_PROMPT, encoding="utf-8")
    (args.output / "cases.json").write_bytes(args.dataset.read_bytes())
    for path in files:
        target = args.output / "execution-source" / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    modes = ["prompt", "schema"] if args.mode == "both" else [args.mode]
    rows = []
    for case in cases:
        for mode in modes:
            for repeat in range(1, args.repeat + 1):
                payload = {"model": model["name"], "stream": False,
                    "messages": extraction_messages(case["turns"]), "options": manifest["options"]}
                if mode == "schema":
                    payload["format"] = schema
                trial = {"case_id": case["id"], "tags": case["tags"], "mode": mode, "repeat": repeat}
                name = f"{case['id']}-{mode}-r{repeat}"
                write(args.output / (name + ".request.json"), payload)
                start = time.perf_counter()
                raw = ""
                try:
                    response = request("/api/chat", payload, timeout=args.timeout)
                    write(args.output / (name + ".response.json"), response)
                    raw = response.get("message", {}).get("content", "")
                    trial["usage"] = {k: response[k] for k in ("prompt_eval_count", "eval_count") if k in response}
                    if response.get("done") is not True or response.get("done_reason") == "length":
                        trial["error"] = "incomplete_or_truncated_response"
                except Exception as exc:
                    trial["error"] = f"{type(exc).__name__}: {exc}"
                trial["wall_seconds"] = round(time.perf_counter() - start, 3)
                trial["raw"] = raw
                trial["score"] = score(case, "" if trial.get("error") else raw)
                rows.append(trial)
                with (args.output / "trials.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(trial, ensure_ascii=False) + "\n")
                write(args.output / "summary.json", summarize(rows))
                print(f"{name}: tp={trial['score']['tp']} fp={trial['score']['fp']} fn={trial['score']['fn']} "
                      f"exact={trial['score']['case_exact']} seconds={trial['wall_seconds']} "
                      f"error={trial.get('error', '')}", flush=True)
    current = next((m for m in request("/api/tags")["models"] if m["name"] == model["name"]), {})
    manifest["model_digest_unchanged"] = current.get("digest") == model["digest"]
    manifest["completed_trials"] = len(rows)
    write(args.output / "manifest.json", manifest)


if __name__ == "__main__":
    main()
