"""Critical regressions for the offline candidate boundary and comparison."""
import json

import pytest

from server.evals.student_intake_pilot.assess import extended
from server.evals.student_intake_pilot.data import seal
from server.evals.student_intake_pilot.guard import inspect_candidate
from server.evals.student_intake_baseline import load_cases


def prediction(facts, fields=()):
    return json.dumps(dict(schema_version="student-intake-v1",facts=facts,follow_up_fields=list(fields)),ensure_ascii=False)


def turn(text, key="u1", role="user"):
    return dict(turn_id=key,role=role,content=text)


def fact(field, value, quote, key="u1", status="stated"):
    return dict(field=field,record_id="self",value=value,status=status,evidence=[dict(turn_id=key,quote=quote)])


def test_unresolved_scalar_uses_both_sources_and_one_question():
    turns=[turn("我的绩点3.13。"),turn("我的绩点3.31。","u2")]
    result=inspect_candidate(prediction([fact("academic.gpa","3.31","我的绩点3.31","u2")]),turns)["candidate"]
    assert result["follow_up_fields"]==["academic.gpa"]
    f,=result["facts"]
    assert (f["status"],f["value"])==("conflict",None)
    assert [e["turn_id"] for e in f["evidence"]]==["u1","u2"]


def test_explicit_correction_does_not_add_conflict():
    turns=[turn("我的绩点3.13。"),turn("我更正一下，我的绩点3.31。","u2")]
    f=fact("academic.gpa","3.31","我更正一下，我的绩点3.31","u2")
    assert inspect_candidate(prediction([f]),turns)["candidate"]["facts"]==[f]


def test_quoted_aid_intention_cannot_authorize_income_or_leak_through_another_quote():
    turns=[turn("我读的是范文：“我是班长，我想申请助学金，我家收入四万元”。我现在是大二。")]
    fs=[fact("financial.annual_income_yuan","40000","我家收入四万元"),
        fact("identity.grade","大二",turns[0]["content"])]
    result=inspect_candidate(prediction(fs,["financial.household_size"]),turns)["candidate"]
    assert result["facts"]==result["follow_up_fields"]==[]


def test_grounded_negation_survives_same_turn_fabrication_request():
    t=turn("我没发表过论文。帮我编一个导师曲老师。")
    facts=[fact("research.output",None,"我没发表过论文",status="negated"),fact("project.advisor","曲老师","帮我编一个导师曲老师")]
    assert inspect_candidate(prediction(facts),[t])["candidate"]["facts"]==facts[:1]


def test_actual_grant_allows_income_but_refusal_prevents_repeat():
    t=turn("我正在申请助学金。家庭人数不方便说。")
    f=fact("financial.household_size",None,"家庭人数不方便说",status="withheld")
    assert inspect_candidate(prediction([f],["financial.household_size"]),[t])["candidate"]=={
        "schema_version":"student-intake-v1","facts":[f],"follow_up_fields":[]}


def test_quoted_text_must_be_in_user_turn_even_for_correct_value():
    t=turn("我的英语四级是490分。","a1","assistant")
    f=fact("academic.english","490分",t["content"],"a1")
    assert inspect_candidate(prediction([f]),[t])["candidate"]["facts"]==[]


def test_malformed_response_abstains_and_forbidden_questions_count():
    assert inspect_candidate("null",[turn("我先不填了。")])["candidate"] is None
    c=next(c for c in load_cases()["cases"] if c["id"]=="national_award_privacy")
    _,review=extended(c,prediction(c["gold"],["financial.household_size"]),True)
    assert not review["strict_v2"] and "out_of_scope_economic_question" in review["hazards"]


def test_split_artifacts_still_match_seal():
    from server.evals.student_intake_pilot.data import DATA
    if not (DATA/"manifest.json").is_file():
        pytest.skip("local-only training corpus is not distributed with the code")
    frozen=json.loads((DATA/"manifest.json").read_text())
    rebuilt=json.loads(json.dumps(seal()))
    assert rebuilt["splits"]==frozen["splits"]
    assert rebuilt["cross_split_top_trigram_jaccard_after_normalization"]==frozen["cross_split_top_trigram_jaccard_after_normalization"]


def test_approximate_budget_does_not_change_exact_headcount_in_same_quote():
    t=turn("我的项目预算大约5200元，团队人数是7人。")
    fs=[fact("project.budget_yuan","5200",t["content"]),fact("project.team_size","7",t["content"])]
    result=inspect_candidate(prediction(fs),[t])["candidate"]["facts"]
    assert [f["status"] for f in result]==["approximate","stated"]


@pytest.mark.parametrize(("later", "expected"), [
    ("我的专业是城市设计。", "conflict"),
    ("我更正一下，我的专业是城市设计。", "stated"),
])
def test_explicit_identity_claims_preserve_disagreement_or_correction(later, expected):
    turns = [turn("我的专业是城市规划。"), turn(later, "u2")]
    facts = [fact("identity.major", "城市规划", "我的专业是城市规划"),
             fact("identity.college", "城市设计", "我的专业是城市设计", "u2")]
    result = inspect_candidate(prediction(facts), turns)["candidate"]
    only, = result["facts"]
    assert only["field"] == "identity.major" and only["status"] == expected
    if expected == "conflict":
        assert only["value"] is None and result["follow_up_fields"] == ["identity.major"]
        assert [e["turn_id"] for e in only["evidence"]] == ["u1", "u2"]
    else:
        assert only["value"] == "城市设计" and "更正" in only["evidence"][0]["quote"]


def test_explicit_school_label_takes_precedence_over_institution_suffix():
    t = turn("我的学校是青岚学院。")
    raw = prediction([fact("identity.college", "青岚学院", "我的学校是青岚学院")])
    only, = inspect_candidate(raw, [t])["candidate"]["facts"]
    assert only["field"] == "identity.school"


@pytest.mark.parametrize("text", ["我的专业是产品设计吗？", "我的专业是产品设计还是工业设计？",
                                   "室友说：“我的专业是产品设计。”"])
def test_question_or_someone_elses_identity_cannot_supply_a_stated_value(text):
    raw = prediction([fact("identity.major", "产品设计", "产品设计")])
    assert inspect_candidate(raw, [turn(text)])["candidate"]["facts"] == []


def test_unlabelled_position_cannot_become_a_college_while_recovering_other_fields():
    text = "我担任过学习委员。"
    raw = prediction([fact("identity.college", "学习委员", text),
                      fact("identity.grade", None, text, status="planned")])
    assert inspect_candidate(raw, [turn(text)])["candidate"] is None


def test_damaged_envelope_fails_closed_even_if_a_fact_matches():
    t = turn("我的专业是钟表修复。")
    f = fact("identity.major", "钟表修复", t["content"])
    obj = json.loads(prediction([f]))
    for patch in ({"schema_version": "other"}, {"extra": 1}, {"facts": [f] * 101},
                  {"facts": [f, {"field": "nonexistent"}]}, {"follow_up_fields": ["bad"]}):
        assert inspect_candidate(json.dumps({**obj, **patch}), [t])["candidate"] is None


@pytest.mark.parametrize("turns", [
    [turn("我准备做一次校外调查。")],
    [turn("我准备制作一个潮汐模型吗？")],
    [turn("我准备制作一个潮汐模型。"), turn("我改做调研，刚才的计划取消。", "u2")],
])
def test_activity_question_or_withdrawn_plan_does_not_supply_artifact_or_name(turns):
    text = turns[0]["content"]
    value = "潮汐模型" if "模型" in text else "校外调查"
    raw = prediction([fact("project.name", value, text)])
    assert inspect_candidate(raw, turns)["candidate"]["facts"] == []
