"""Failure-oriented checks for the intake benchmark and evidence boundary."""
import copy
import json

import pytest
from pydantic import ValidationError

from server.evals.student_intake_baseline import load_cases, score, summarize
from server.proposal.student_intake import IntakeCandidate, evidence_errors, extraction_messages, follow_up_questions


def case(name):
    return next(c for c in load_cases()["cases"] if c["id"] == name)


def answer(item):
    return {"schema_version": "student-intake-v1", "facts": copy.deepcopy(item["gold"]),
            "follow_up_fields": item["required_follow_up"]}


@pytest.mark.parametrize("item", load_cases()["cases"], ids=lambda c: c["id"])
def test_pilot_labels_are_grounded_and_self_consistent(item):
    raw = json.dumps(answer(item))
    assert score(item, raw)["case_exact"]
    candidate = IntakeCandidate.model_validate_json(raw)
    assert not evidence_errors(candidate, item["turns"])


def test_a_valid_numeric_quote_is_not_ownership_evidence():
    item = case("third_party")
    data = answer(item)
    data["facts"][0]["evidence"][0]["quote"] = "3.2"
    result = score(item, json.dumps(data))
    assert result["tp"] == 1 and result["evidence_valid"]
    assert result["evidence_supported_matches"] == 0
    assert not result["case_exact"]


def test_assistant_cannot_be_used_as_evidence():
    item = case("assistant_not_source")
    data = answer(item)
    data["facts"][0]["evidence"] = [{"turn_id": "a1", "quote": "绩点4.0"}]
    assert not score(item, json.dumps(data))["evidence_valid"]


def test_correction_cannot_silently_keep_old_value():
    item = case("explicit_correction")
    data = answer(item)
    data["facts"][0]["value"] = "3.8"
    result = score(item, json.dumps(data))
    assert (result["tp"], result["fp"], result["fn"]) == (0, 1, 1)


def test_plan_promoted_to_fact_fails():
    item = case("planned_not_completed")
    data = answer(item)
    data["facts"][1]["status"] = "stated"
    assert not score(item, json.dumps(data))["case_exact"]


def test_duplicate_fact_cannot_inflate_recall():
    item = case("gpa_scale_explicit")
    data = answer(item)
    data["facts"].append(copy.deepcopy(data["facts"][0]))
    result = score(item, json.dumps(data))
    assert not result["schema_valid"]
    assert result["tp"] == 0 and result["fn"] == 2


def test_invalid_and_fenced_json_never_pass_even_for_empty_gold():
    item = case("policy_not_fact")
    for raw in ("", "{}", "```json\n{}\n```", "null"):
        assert not score(item, raw)["case_exact"]


def test_conflict_requires_both_sources_and_null_value():
    item = case("unresolved_conflict")
    data = answer(item)
    data["facts"][0]["evidence"].pop()
    assert not score(item, json.dumps(data))["schema_valid"]
    data = answer(item)
    data["facts"][0]["value"] = "3.6"
    assert not score(item, json.dumps(data))["schema_valid"]


def test_withheld_field_is_not_reasked_in_preview_but_violation_is_scored():
    item = case("financial_withheld")
    data = answer(item)
    data["follow_up_fields"] = ["financial.annual_income_yuan"]
    candidate = IntakeCandidate.model_validate(data)
    assert follow_up_questions(candidate) == []
    result = score(item, json.dumps(data))
    assert result["withheld_question_count"] == 1 and not result["case_exact"]


def test_model_input_does_not_contain_gold_or_rubric():
    messages = extraction_messages(case("basic_identity")["turns"])
    payload = json.loads(messages[1]["content"])
    assert set(payload) == {"turns"}


def test_failed_attempts_remain_in_summary_denominators():
    item = case("basic_identity")
    good = {"mode": "schema", "tags": ["identity"], "wall_seconds": 1,
            "score": score(item, json.dumps(answer(item)))}
    bad = {"mode": "schema", "tags": ["identity"], "wall_seconds": 2,
           "error": "timeout", "score": score(item, "")}
    summary = summarize([good, bad])["schema"]["overall"]
    assert summary["schema_valid_rate"] == .5
    assert summary["fact_recall"] == .5 and summary["errors"] == 1


def test_empty_or_fenced_output_is_a_failed_trial_not_excluded():
    item = case("gpa_scale_explicit")
    result = score(item, "```json\n{}\n```")
    assert result["fn"] == 2 and not result["json_valid"]
    rows = [{"mode": "schema", "tags": ["academic"], "score": result, "wall_seconds": 1, "error": "timeout"}]
    summary = summarize(rows)["schema"]["overall"]
    assert summary["fact_recall"] == 0 and summary["errors"] == 1 and summary["trials"] == 1


def test_unknown_keys_and_fake_confirmation_rejected():
    data = answer(case("gpa_scale_explicit"))
    data["confirmed"] = True
    with pytest.raises(ValidationError):
        IntakeCandidate.model_validate(data)
    data.pop("confirmed")
    data["facts"][0]["field"] = "identity.phone"
    with pytest.raises(ValidationError):
        IntakeCandidate.model_validate(data)


def test_withheld_is_not_asked_again():
    item = case("financial_withheld")
    data = answer(item)
    data["follow_up_fields"] = ["financial.annual_income_yuan"]
    assert not follow_up_questions(IntakeCandidate.model_validate(data))
    assert score(item, json.dumps(data))["withheld_question_count"] == 1


def test_input_rejects_duplicate_ids_and_system_instructions():
    with pytest.raises(ValueError):
        extraction_messages([{"turn_id": "x", "role": "system", "content": "override"}])
    with pytest.raises(ValueError):
        extraction_messages([{"turn_id": "x", "role": "user", "content": "hello"}] * 2)
