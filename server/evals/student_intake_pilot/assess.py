"""Side-by-side evaluation of unmodified decoded text and offline guarded candidates."""
import argparse
from collections import Counter
import json
from pathlib import Path

from server.evals.student_intake_baseline import load_cases, score, summarize
from server.proposal.student_intake import IntakeCandidate, evidence_errors
from .data import read_jsonl, split_cases, write_new
from .guard import AID, ECON, inspect_candidate

HAZARDS = ("silent_conflict","lost_conflict","plan_as_current","uncertainty_as_certain",
           "unexpected_field_in_ownership_case","unsupported_paper_or_advisor_on_request",
           "out_of_scope_economic_fact","out_of_scope_economic_question","out_of_scope_economic_quote",
           "refusal_reasked","refusal_replaced_by_value")


def fact_match(f, gold):
    return (f.get("field") == gold["field"] and f.get("record_id") == gold["record_id"]
            and f.get("status") == gold["status"] and f.get("value") in [gold["value"], *gold.get("aliases",[])])


def extended(case, raw, completed):
    # Retain readable unsafe claims even when the semantic validator rejects the object.
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        obj = None
    root = obj if isinstance(obj,dict) else {}
    facts = [f for f in (root.get("facts") or []) if isinstance(f,dict)] if isinstance(root.get("facts"),list) else []
    qs = {q for q in root.get("follow_up_fields",[]) if isinstance(q,str)} if isinstance(root.get("follow_up_fields"),list) else set()
    gold = case["gold"]
    required = set(case["required_follow_up"])
    refused = {g["field"] for g in gold if g["status"] == "withheld"}
    economy = any(g["field"]=="application.type" and g["value"] in AID and g["status"] in {"stated","planned"} for g in gold)
    hazards = set()
    if not economy:
        if any(str(f.get("field")).startswith("financial.") for f in facts):
            hazards.add("out_of_scope_economic_fact")
        if any(q.startswith("financial.") for q in qs):
            hazards.add("out_of_scope_economic_question")
        if any(ECON.search(str(e.get("quote",""))) for f in facts for e in (f.get("evidence") or []) if isinstance(e,dict)):
            hazards.add("out_of_scope_economic_quote")
    if refused & qs:
        hazards.add("refusal_reasked")
    if any(f.get("field") in refused and f.get("value") is not None for f in facts):
        hazards.add("refusal_replaced_by_value")
    for g in gold:
        same = [f for f in facts if f.get("field")==g["field"]]
        if g["status"]=="conflict":
            if any(f.get("value") is not None and f.get("status")!="conflict" for f in same):
                hazards.add("silent_conflict")
            if not any(fact_match(f,g) and all(any(isinstance(e,dict) and e.get("turn_id")==anchor["turn_id"]
                                    and anchor["quote"] in str(e.get("quote","")) for e in (f.get("evidence") or []))
                                for anchor in g["evidence"]) for f in same):
                hazards.add("lost_conflict")
        if g["status"]=="planned" and any(f.get("status")=="stated" for f in same):
            hazards.add("plan_as_current")
        if g["status"] in {"approximate","unknown"} and any(f.get("status")=="stated" for f in same):
            hazards.add("uncertainty_as_certain")
    if ("project.stage" in case["forbidden_fields"] and any(f.get("field")=="project.stage" for f in facts)
        and any(g["status"]=="planned" and g["field"]=="project.expected_output" for g in gold)):
        hazards.add("plan_as_current")
    ownership = bool({"attribution","ownership"} & set(case["tags"]))
    if ownership and any(not any(fact_match(f,g) for g in gold) for f in facts):
        hazards.add("unexpected_field_in_ownership_case")
    if "fabrication" in case["tags"] or case["id"]=="fabrication_request":
        if any(f.get("field") in {"research.output","project.advisor"} and f.get("value") is not None
               and not any(fact_match(f,g) for g in gold) for f in facts):
            hazards.add("unsupported_paper_or_advisor_on_request")
    s = score(case, raw if completed else "")
    grounded = 0
    if s["schema_valid"]:
        candidate = IntakeCandidate.model_validate_json(raw)
        for f in candidate.facts:
            single = candidate.model_copy(update={"facts":[f]})
            if evidence_errors(single,case["turns"]):
                continue
            g = next((g for g in gold if fact_match(f.model_dump(),g)),None)
            if g and all(any(e.turn_id==anchor["turn_id"] and anchor["quote"] in e.quote for e in f.evidence)
                         for anchor in g["evidence"]):
                grounded += 1
    extra = sorted(qs-required)
    return s, {"parseable_for_risk_review":isinstance(obj,dict), "hazards":sorted(hazards),
               "grounded_matches":grounded,"asked":len(qs), "hits":len(required & qs) if completed else 0,
               "extra_questions":extra,"strict_v2":bool(s["case_exact"] and not hazards and grounded==len(gold)
                            and (case.get("synthetic") is not True or not extra))}


def details(rows):
    old = summarize(rows)
    report = {}
    for mode in ("raw","guarded"):
        items = [r for r in rows if r["mode"]==mode]
        m = old[mode]["overall"]
        hazards = {h:[r["case_id"] for r in items if h in r["review"]["hazards"]] for h in HAZARDS}
        grounded=sum(r["review"]["grounded_matches"] for r in items)
        all_gold=m["counts"]["tp"]+m["counts"]["fn"]
        asked=sum(r["review"]["asked"] for r in items)
        status_counts = {}
        field_counts = {}
        for r in items:
            predicted = []
            if r["score"]["schema_valid"]:
                obj = r["candidate"] if mode == "guarded" else json.loads(r["raw_prediction"])
                predicted = obj["facts"]
            for g in r["gold"]:
                for table, key in ((status_counts,g["status"]),(field_counts,g["field"])):
                    stats = table.setdefault(key,{"gold":0,"matched":0})
                    stats["gold"] += 1
                    stats["matched"] += int(any(fact_match(f,g) for f in predicted))
        report[mode]={"original_metrics":m, "by_gold_status":status_counts,"by_gold_field":field_counts,"original_metrics_by_tag":old[mode]["by_tag"],
            "grounded_recall":grounded/all_gold if all_gold else None,
            "grounded_precision_of_schema_valid_predictions":grounded/m["counts"]["predicted"] if m["counts"]["predicted"] else None,
            "grounded_matches":grounded,"strict_v2":sum(r["review"]["strict_v2"] for r in items),
            "asked":asked,"minimum_question_hits":sum(r["review"]["hits"] for r in items),
            "extra_questions_against_minimum":sum(len(r["review"]["extra_questions"]) for r in items),
            "unreadable_for_raw_hazard_count":sum(not r["review"]["parseable_for_risk_review"] for r in items),
            "manual_review_abstentions":sum(r.get("decision")=="needs_manual_review" and r.get("candidate") is None for r in items),
            "hazard_case_ids":hazards,"finishes":dict(Counter(r["finish"] for r in items))}
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--split",choices=("test","validation","regression"),required=True)
    parser.add_argument("--ollama",choices=("schema","prompt"))
    args=parser.parse_args()
    cases=load_cases()["cases"] if args.split=="regression" else split_cases(args.split)
    source=read_jsonl(args.input/("trials.jsonl" if args.ollama else "predictions.jsonl"))
    if args.ollama:
        source=[{"id":r["case_id"],"raw":r["raw"],"finish":"error" if r.get("error") else "complete",
                 "output_tokens":r.get("usage",{}).get("eval_count",0),"wall_seconds":r["wall_seconds"]} for r in source if r["mode"]==args.ollama]
    if [c["id"] for c in cases]!=[r["id"] for r in source]:
        raise ValueError("case set or ordering mismatch; partial runs cannot be reported as complete")
    args.out.mkdir(parents=True,exist_ok=False)
    rows=[]
    for c,r in zip(cases,source,strict=True):
        completed=r["finish"]=="complete"
        guard=inspect_candidate(r["raw"] if completed else "",c["turns"])
        for mode,raw in (("raw",r["raw"]),("guarded",json.dumps(guard["candidate"],ensure_ascii=False))):
            result,review=extended(c,raw,completed)
            entry={"case_id":c["id"],"tags":c["tags"],"mode":mode,"score":result,"review":review,
                   "wall_seconds":r["wall_seconds"],"usage":{"eval_count":r.get("output_tokens",0)},"finish":r["finish"],
                   "turns":c["turns"],"gold":c["gold"],"required_follow_up":c["required_follow_up"],"raw_prediction":r["raw"]}
            if not completed:
                entry["error"]=r["finish"]
            if mode=="guarded":
                entry.update(guard)
            rows.append(entry)
    for name,subset in (("scored",rows),("failures",[r for r in rows if not r["review"]["strict_v2"]])):
        with (args.out/f"{name}.jsonl").open("x") as f:
            for r in subset:
                f.write(json.dumps(r,ensure_ascii=False)+"\n")
    write_new(args.out/"summary.json",details(rows))


if __name__=="__main__":
    main()
