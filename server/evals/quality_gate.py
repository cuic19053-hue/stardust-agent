"""Measure synthetic regressions with real loopback inference and a frozen review replay."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re

import httpx

from server.evals.review_guard_replay import DEFAULT_ARCHIVE, replay_archive
from server.evals.student_intake_baseline import DATASET as INTAKE_DATA
from server.evals.student_intake_baseline import load_cases, score, sha, write
from server.proposal.student_intake import IntakeCandidate, extraction_messages
from server.proposal.student_intake_review import model_turns, verified_review
from server.proposal.workflow import AgenticWorkflowEngine

MODEL = "qwen2.5:1.5b"
MODEL_DIGEST = "65ec06548149b04c096a120e4a6da9d4017ea809c91734ea5631e89f96ddc57b"
OLLAMA_VERSION = "0.34.1"
INTAKE_IDS = (
    "basic_identity", "gpa_scale_explicit", "unresolved_conflict", "third_party",
    "planned_not_completed", "budget_conversion", "national_award_privacy", "fabrication_request",
)
HERE = Path(__file__).parent
DRAFT_DATA = HERE / "fixtures/quality_draft_cases.json"
BASELINE = HERE / "baselines/ci_quality.json"


class Inference:
    def __init__(self, client, output):
        self.client, self.output = client, output
        self.calls = 0
        self.errors = 0

    def post(self, endpoint, payload, timeout=120):
        self.calls += 1
        path = self.output / f"call-{self.calls:02d}"
        options = {**payload.get("options", {}), "temperature": 0, "seed": 42,
                   "num_ctx": 8192, "num_thread": 4, "num_gpu": 0}
        options.setdefault("num_predict", 1536)
        payload = {**payload, "options": options}
        write(path.with_suffix(".request.json"), payload)
        try:
            response = self.client.post(endpoint, json=payload, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            write(path.with_suffix(".response.json"), result)
            if result.get("done") is not True or result.get("done_reason") != "stop":
                raise ValueError(f"unfinished inference in {path.name}: {result.get('done_reason')}")
        except (httpx.HTTPError, ValueError) as exc:
            self.errors += 1
            write(path.with_suffix(".error.json"), {"error": str(exc)})
            raise RuntimeError(f"{path.name}: inference failed") from exc
        return result

    def draft_or_critic(self, messages, options=None):
        options = dict(options or {})
        schema = options.pop("response_schema", None)
        payload = {"model": MODEL, "messages": messages, "stream": False, "options": options}
        if schema is not None:
            payload["format"] = schema
        return self.post("/api/chat", payload, timeout=180)["message"]["content"]


def checked_json(client, path):
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def verify_model(client):
    version = checked_json(client, "/api/version")["version"]
    models = {row["name"]: row["digest"] for row in checked_json(client, "/api/tags")["models"]}
    if version != OLLAMA_VERSION or models.get(MODEL) != MODEL_DIGEST:
        raise ValueError(f"Expected Ollama {OLLAMA_VERSION} and {MODEL}@{MODEL_DIGEST}")


def measure_intake(inference):
    cases = {case["id"]: case for case in load_cases(INTAKE_DATA)["cases"]}
    results = {}
    for case_id in INTAKE_IDS:
        case = cases[case_id]
        turns = case["turns"]
        visible = model_turns(turns)
        if visible:
            response = inference.post("/api/chat", {
                "model": MODEL, "stream": False, "messages": extraction_messages(visible),
                "format": IntakeCandidate.model_json_schema(), "options": {"num_predict": 2048},
            })
            candidate, decision = verified_review(response["message"]["content"], visible, turns)
        else:
            candidate = IntakeCandidate(schema_version="student-intake-v1", facts=[], follow_up_fields=[])
            decision = "pending_student_confirmation"
        result = score(case, candidate.model_dump_json() if candidate else "")
        results[case_id] = {**result, "decision": decision,
                            "candidate": candidate.model_dump() if candidate else None}
    totals = Counter()
    for result in results.values():
        totals.update({key: result[key] for key in (
            "tp", "fp", "fn", "case_exact", "evidence_supported_matches",
            "forbidden_field_count",
        )})
    return results, {
        "field_f1": 2 * totals["tp"] / (2 * totals["tp"] + totals["fp"] + totals["fn"]),
        "exact_cases": totals["case_exact"],
        "grounded_matches": totals["evidence_supported_matches"],
        "forbidden_fields": totals["forbidden_field_count"],
    }


def measure_drafts(inference):
    results = {}
    engine = AgenticWorkflowEngine(draft_caller=inference.draft_or_critic,
                                   critic_caller=inference.draft_or_critic,
                                   max_retries=0, extended_grounding=True)
    for case in json.loads(DRAFT_DATA.read_text(encoding="utf-8"))["cases"]:
        draft = engine.run_workflow(
            project_context={"project_name": case["title"], "confirmed_facts": case["facts"],
                             "planned_items": case["plans"]},
            section_id=case["section"]["id"], section_title=case["section"]["title"],
            skill_guidance=case["guidance"], evidence_results=[],
        )
        content = draft.content
        echoed_fields = [f"**{key}**" for key in case["facts"] | case["plans"]]
        body = "\n".join(line for line in content.splitlines() if not any(k in line for k in echoed_fields))
        results[case["id"]] = {
            "present": [p for p in case["required"] if re.search(p, body)],
            "missing": [p for p in case["required"] if not re.search(p, body)],
            "forbidden": [p for p in case["forbidden"] if re.search(p, content)],
            "content": content,
        }
    return results, {
        "fact_and_plan_hits": sum(len(x["present"]) for x in results.values()),
        "exact_cases": sum(not (x["missing"] or x["forbidden"]) for x in results.values()),
        "forbidden_claims": sum(len(x["forbidden"]) for x in results.values()),
    }


def finding_key(issue):
    return tuple(issue.get(key) for key in ("section_id", "origin", "check_code", "problem", "verification"))


def measure_review():
    archive = json.loads(DEFAULT_ARCHIVE.read_text(encoding="utf-8"))
    reviewed = {row["case_id"]: row for row in archive["guard_review"]["runs"]}
    frozen = {row["case_id"]: row["report"] for row in archive["guard_replay"]["runs"]}
    replay = replay_archive(archive)
    results = {}
    for record, run in zip(archive["records"], replay["runs"], strict=True):
        case_id = record["case_id"]
        if not run["report"]["complete"]:
            raise ValueError(f"{case_id}: incomplete review replay")
        reference_issues = frozen[case_id]["issues"]
        labels = {}
        for label in reviewed[case_id]["issues"]:
            original = reference_issues[label["index"]]
            if original["problem"] != label["problem"]:
                raise ValueError(f"{case_id}: review label no longer matches its original finding")
            labels[finding_key(original)] = label
        targets = {target["id"] for target in record["targets"]}
        found, unknown, false_positives = set(), [], []
        for issue in run["report"]["issues"]:
            label = labels.get(finding_key(issue))
            if not label:
                unknown.append(issue["problem"])
            elif label["verdict"] == "matched" and label["target_id"] in targets - found:
                found.add(label["target_id"])
            elif label["verdict"] != "out_of_scope":
                false_positives.append(issue["problem"])
        results[case_id] = {"matched": sorted(found), "missed": sorted(targets - found),
                            "unreviewed": unknown, "false_positives": false_positives}
    return results, {
        "target_hits": sum(len(x["matched"]) for x in results.values()),
        "missed_targets": sum(len(x["missed"]) for x in results.values()),
        "false_positives": sum(len(x["false_positives"]) for x in results.values()),
        "unreviewed_findings": sum(len(x["unreviewed"]) for x in results.values()),
    }


def regressions(metrics, drafts, baseline):
    failures = []
    for direction, bounds in (("minimum", baseline["minimum"]), ("maximum", baseline["maximum"])):
        for group, limits in bounds.items():
            for name, limit in limits.items():
                value = metrics[group][name]
                if (direction == "minimum" and value < limit) or (direction == "maximum" and value > limit):
                    failures.append(f"{group}.{name} = {value}, {direction} = {limit}")
    for case_id, expected in baseline["draft_cases"].items():
        draft = drafts[case_id]
        missing = sorted(set(expected["present"]) - set(draft["present"]))
        added = sorted(set(draft["forbidden"]) - set(expected["known_forbidden"]))
        if missing or added:
            failures.append(f"draft.{case_id}: lost checks {missing}, new forbidden checks {added}")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=11434)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--measure-only", action="store_true", help="Record a run without applying thresholds.")
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    inputs = {file.name: sha(file) for file in (INTAKE_DATA, DRAFT_DATA, DEFAULT_ARCHIVE)}
    baseline = None if args.measure_only else json.loads(BASELINE.read_text(encoding="utf-8"))
    if baseline and inputs != baseline["fixture_sha256"]:
        raise ValueError("Regression fixtures changed. Measure and explicitly review the new baseline.")

    details, metrics = {}, {}
    with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", trust_env=False) as client:
        verify_model(client)
        inference = Inference(client, args.output)
        details["intake"], metrics["intake"] = measure_intake(inference)
        details["draft"], metrics["draft"] = measure_drafts(inference)
        verify_model(client)
    details["review_replay"], metrics["review_replay"] = measure_review()
    failures = regressions(metrics, details["draft"], baseline) if baseline else []
    if inference.errors:
        failures.append(f"{inference.errors} live model calls failed")
    report = {"model": MODEL, "model_digest": MODEL_DIGEST, "ollama_version": OLLAMA_VERSION,
              "fixture_sha256": inputs, "live_calls": inference.calls, "metrics": metrics,
              "cases": details, "failures": failures}
    write(args.output / "report.json", report)
    print(json.dumps({"metrics": metrics, "failures": failures}, ensure_ascii=False, indent=2))
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
