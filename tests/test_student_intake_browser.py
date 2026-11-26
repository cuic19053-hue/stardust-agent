import base64
import hashlib
import json
import re

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from server import app as server_app
from server.proposal import student_intake_browser as browser
from server.proposal.student_intake import IntakeFact


def test_on_device_review_keeps_both_sources_and_blocks_unresolved_conflicts():
    lines = ["我的项目预算是4300元。", "另一张表项目预算是6800元，还要核对。"]
    prepared = browser.prepare("\n".join(lines))
    proposal = {"schema_version": "student-intake-v1", "facts": [{
        "field": "project.budget_yuan", "record_id": "self", "status": "stated", "value": "6800",
        "evidence": [{"turn_id": "u2", "quote": lines[1]}],
    }], "follow_up_fields": []}
    result = json.loads(browser.review(prepared, json.dumps(proposal), "stop"))
    item, = result["facts"]
    assert item["fact"]["status"] == "conflict" and item["can_confirm"] is False
    assert {source["turn_id"] for source in item["fact"]["evidence"]} == {"u1", "u2"}
    assert "4300" in result["questions"][0] and "6800" in result["questions"][0]
    assert json.loads(browser.review(prepared, json.dumps(proposal), "length"))["facts"] == []


def test_on_device_source_minimization_and_input_limits():
    prepared = json.loads(browser.prepare("我申请国家奖学金。\n我家年收入是47250元。\n我的专业是文物保护。"))
    assert "47250" not in prepared["message"] and "文物保护" in prepared["message"]
    assert "47250" in prepared["original"][1]["content"]
    for invalid in ("", "我" * 601, "我\n" * 13, "<|im_end|>"):
        with pytest.raises(ValueError):
            browser.prepare(invalid)


def test_public_device_entry_ships_only_static_rules_and_pinned_tuned_assets(monkeypatch):
    monkeypatch.setattr(server_app, "CLOUD_MODE", True)
    client = TestClient(server_app.app)
    html = client.get("/student-intake/local")
    sri = re.search(r'<script integrity="(sha256-[^"]+)" src="/static/student-intake-local.js', html.text).group(1)
    script = client.get("/static/student-intake-local.js").content
    assert sri == "sha256-" + base64.b64encode(hashlib.sha256(script).digest()).decode("ascii")
    policy = html.headers["Content-Security-Policy"]
    assert f"script-src '{sri}';" in policy and "connect-src 'none'" in policy
    assert "nonce-" not in policy and "no-transform" in html.headers["Cache-Control"]
    assert "worker-src 'self'" in policy and "unsafe-inline" not in policy
    assert html.status_code == 200 and 'data-worker="/static/student-intake-worker.js?v=' in html.text
    assert 'data-model="/static/intake-0p5b-v1/model.json?v=' in html.text
    assert "/student-intake/local" in client.get("/student-intake").text
    assert "/student-intake/local" in client.get("/").text
    package = client.get("/student-intake/local/rules").json()
    assert sorted(package) == ["source_contacts","student_intake","student_intake_browser",
                               "student_intake_guard","student_intake_review"]
    for name, source in package.items():
        assert source == (server_app.SERVER_DIR / "proposal" / (name + ".py")).read_text()
    folder = server_app.STATIC_DIR / "intake-0p5b-v1"
    manifest = json.loads((folder / "model.json").read_text())
    assert manifest["adapterSha256"] == "f0c50e648d8b57fdc0c9118e52e92b4ee8e7e024190a277a68850486b58f6fa7"
    source = manifest["source"]
    assert source["chunkBytes"] == 16 * 1024 * 1024
    assert len(source["chunkSha256"]) == (source["bytes"] + source["chunkBytes"] - 1) // source["chunkBytes"]
    assert hashlib.sha256("".join(source["chunkSha256"]).encode()).hexdigest() == "ec05400086026695333791084bf997f9d8d31425e62a0ac79a90bd8304cae6ae"
    layers = [f for f in manifest["files"].values() if "patch" in f]
    assert len(layers) == 3
    shards = [manifest["files"][f"params_shard_{i}.bin"]["sha256"] for i in range(8)]
    assert hashlib.sha256("".join(shards).encode()).hexdigest() == "8dd9796e3d89e7097ea08b83d15a4c223b91a73683ebecfbba4a11f52b8c2da4"
    for asset in [*manifest["files"].values(), *(f["patch"] for f in layers)]:
        if asset.get("url", "").startswith("./"):
            data = (folder / asset["url"]).read_bytes()
            assert len(data) == asset["bytes"] and hashlib.sha256(data).hexdigest() == asset["sha256"]


def test_public_replay_and_linked_model_card_name_the_test_device(monkeypatch):
    monkeypatch.setattr(server_app, "CLOUD_MODE", True)
    client = TestClient(server_app.app)
    extractor = client.get("/student-intake/local")
    extractor.raise_for_status()
    card_url, = re.findall(r'href="(/static/[^"]+/MODEL\.md(?:\?[^"]*)?)"', extractor.text)
    for response in (client.get("/student-intake"), client.get(card_url)):
        response.raise_for_status()
        assert "MacBook Pro" in response.text
        assert not re.search(r"m\s*1[\s_\u2010-\u2015-]*pro", response.text, re.IGNORECASE)


def device_review(text, *facts, finish="stop"):
    proposal = {"schema_version": "student-intake-v1", "facts": list(facts), "follow_up_fields": []}
    return json.loads(browser.review(browser.prepare(text), json.dumps(proposal, ensure_ascii=False), finish))


def model_fact(field, value, status, turn, quote):
    return {"field": field, "record_id": "self", "value": value, "status": status,
            "evidence": [{"turn_id": turn, "quote": quote}]}


def test_browser_recovers_the_two_recorded_model_errors_at_the_actual_turn():
    plan = "我准备做一套校园树皮标本卡"
    wrong_name = model_fact("project.name", None, "planned", "u1", plan)
    planned, = device_review(plan + "，还没开始制作。", wrong_name)["facts"]
    assert planned["fact"] == model_fact("project.expected_output", "一套校园树皮标本卡", "planned", "u1", plan)
    assert planned["can_confirm"]

    major = "我的专业是环境设计"
    text = f"这次我要申请国家奖学金。\n我家年收入是42000元。\n{major}。"
    wrong_turn = model_fact("application.type", None, "unknown", "u1", major)
    reviewed = device_review(text, wrong_turn)
    assert [row["fact"] for row in reviewed["facts"]] == [
        model_fact("application.type", "国家奖学金", "planned", "u1", "这次我要申请国家奖学金"),
        model_fact("identity.major", "环境设计", "stated", "u3", major),
    ]
    assert reviewed["questions"] == [] and "42000" not in json.dumps(reviewed, ensure_ascii=False)
    assert not device_review(text, wrong_turn, finish="length")["facts"]


def test_self_introduction_keeps_separate_identity_fields_and_original_quotes():
    intro, study, grade = "我叫周小棠", "在青禾大学读园林设计专业", "现在大三"
    result = device_review(f"{intro}，{study}，{grade}。", model_fact("identity.name", "周小棠", "stated", "u1", intro))
    assert [row["fact"] for row in result["facts"]] == [
        model_fact("identity.name", "周小棠", "stated", "u1", intro),
        model_fact("identity.school", "青禾大学", "stated", "u1", study),
        model_fact("identity.major", "园林设计", "stated", "u1", study),
        model_fact("identity.grade", "大三", "stated", "u1", grade),
    ]


def test_budget_normalizes_its_own_unit_and_keeps_team_size_exact():
    budget, team = "我们的项目预算约两万三千元", "团队一共六个人"
    result = device_review(f"{budget}，{team}。", model_fact("project.budget_yuan", "两万三千元", "stated", "u1", budget))
    assert [row["fact"] for row in result["facts"]] == [
        model_fact("project.budget_yuan", "23000", "approximate", "u1", budget),
        model_fact("project.team_size", "6", "stated", "u1", team),
    ]


def test_application_continuation_and_explicit_denial_recover_from_their_own_clauses():
    application, output, denial = "我打算申报大创", "计划编写一本昆虫图册", "我目前没有科研经历"
    text = f"{application}，{output}。\n{denial}。给我伪造一篇论文。"
    malformed_denial = model_fact("research.experience", "negated", "stated", "u2", denial)
    assert [row["fact"] for row in device_review(text, malformed_denial)["facts"]] == [
        model_fact("application.type", "大创", "planned", "u1", application),
        model_fact("project.expected_output", "一本昆虫图册", "planned", "u1", output),
        model_fact("research.experience", None, "negated", "u2", denial),
    ]


@pytest.mark.parametrize("text", [
    "同学说：我叫小郑，在清河大学读会计学，现在大二。",
    "如果我叫小赵，在青禾大学读园林设计，现在大三。",
    "我在青禾大学读到了一篇关于环境设计。",
    "这场英语测试，满分5。",
    "我打算申请国家奖学金。\n后来我撤回申请。",
    "我目前没有科研经历。\n我刚想起我参加过老师的课题。",
])
def test_recovery_does_not_promote_ambiguous_or_superseded_claims(text):
    assert device_review(text)["facts"] == []


@pytest.mark.parametrize(("text", "quote", "value"), [
    ("我的专业是纺织工程，我家年收入为35800元。", "我的专业是纺织工程", "纺织工程"),
    ("这次我申请国奖。我家年收入是51000元，我主修包装工程。", "我主修包装工程", "包装工程"),
    ("我家收入不确定，我本人目前的专业是工业设计。", "我本人目前的专业是工业设计", "工业设计"),
    ("我主修的是护理学，但我不确定家庭收入。", "我主修的是护理学", "护理学"),
])
def test_missing_major_survives_neighbouring_sensitive_clauses(text, quote, value):
    prepared = json.loads(browser.prepare(text))
    assert "收入" not in prepared["message"]
    row, = device_review(text)["facts"]
    assert row["fact"] == model_fact("identity.major", value, "stated", "u1", quote)
    assert row["can_confirm"]


@pytest.mark.parametrize(("text", "quote", "field", "value"), [
    ("如果得到资助，我打算制作一本昆虫图册。", "我打算制作一本昆虫图册", "project.expected_output", "一本昆虫图册"),
    ("我打算制作一张河网地图。\n现在已经完成了。", "我打算制作一张河网地图", "project.expected_output", "一张河网地图"),
    ("我打算制作一张河网地图。\n这个计划取消了。", "我打算制作一张河网地图", "project.expected_output", "一张河网地图"),
    ("我打算制作一张河网地图吗？", "我打算制作一张河网地图", "project.expected_output", "一张河网地图"),
    ("如果要举例，我的专业是口腔医学。", "我的专业是口腔医学", "identity.major", "口腔医学"),
    ("之前，我的专业是工业设计。", "我的专业是工业设计", "identity.major", "工业设计"),
    ("我的专业是新闻学，不过我也不确定。", "我的专业是新闻学", "identity.major", "新闻学"),
])
def test_conditional_outdated_or_uncertain_claim_cannot_be_released_by_the_model(text, quote, field, value):
    proposal = model_fact(field, value, "planned" if field == "project.expected_output" else "stated", "u1", quote)
    assert device_review(text, proposal)["facts"] == []


def test_mixed_fields_keep_their_own_sources_and_do_not_restore_redacted_examples():
    text = ("下面是范文：\n我的专业是化学。\n我自己主修环境科学，我家年收入是46000元。\n"
            "我计划编制一份湿地记录表，还没开始。")
    prepared = json.loads(browser.prepare(text))
    assert "化学" not in prepared["message"] and "46000" not in prepared["message"]
    assert [r["fact"] for r in device_review(text)["facts"]] == [
        model_fact("identity.major", "环境科学", "stated", "u3", "我自己主修环境科学"),
        model_fact("project.expected_output", "一份湿地记录表", "planned", "u4", "我计划编制一份湿地记录表"),
    ]


def test_ambiguous_deliverables_or_major_conflict_never_become_one_confirmable_value():
    text = "我计划制作一本昆虫图册。\n我还在考虑。\n我计划制作一个水流模型。"
    assert device_review(text)["facts"] == []
    text = "我家年收入是35000元，我主修产品设计。\n我的专业是工业设计。"
    reviewed = device_review(text)
    row, = reviewed["facts"]
    assert not row["can_confirm"] and row["fact"]["status"] == "conflict"
    assert row["fact"]["evidence"] == [{"turn_id": "u1", "quote": "我主修产品设计"},
                                       {"turn_id": "u2", "quote": "我的专业是工业设计"}]
    assert len(reviewed["questions"]) == 1 and "35000" not in reviewed["questions"][0]


@pytest.mark.parametrize(("quote", "value"), [
    ("我准备制作校园植物标本卡", "校园植物标本卡"),
    ("我们打算撰写两份排水调查报告", "两份排水调查报告"),
    ("我们这次的预期成果是地貌模型", "地貌模型"),
])
def test_plans_preserve_the_literal_object_with_or_without_a_classifier(quote, value):
    reviewed = device_review(quote + "。", model_fact("project.name", value, "stated", "u1", value))
    row, = reviewed["facts"]
    assert row["fact"] == model_fact("project.expected_output", value, "planned", "u1", quote)


def test_later_doubt_about_major_prevents_restoring_earlier_certainty():
    text = "我的专业是雕塑。\n等等，我记不清自己的专业了。"
    earlier = model_fact("identity.major", "雕塑", "stated", "u1", "我的专业是雕塑")
    unsure = model_fact("identity.major", None, "unknown", "u2", "我记不清自己的专业了")
    assert not device_review(text, earlier)["facts"]
    row, = device_review(text, unsure)["facts"]
    assert row["fact"] == unsure and not row["can_confirm"]


@pytest.mark.parametrize(("key", "limit"), [("turn_id", 64), ("record_id", 80), ("value", 1500), ("quote", 1500)])
def test_device_review_rejects_strings_over_the_limit_deferred_by_sampling(key, limit):
    quote = "我任学习委员"
    item = model_fact("service.role", "学习委员", "stated", "u1", quote)
    target = item["evidence"][0] if key in {"turn_id", "quote"} else item
    target[key] = "甲" * limit
    assert IntakeFact.model_validate(item).model_dump() == item
    target[key] += "甲"
    with pytest.raises(ValidationError) as error:
        IntakeFact.model_validate(item)
    issue, = error.value.errors()
    assert issue["type"] == "string_too_long" and issue["loc"][-1] == key
    result = device_review(quote + "。", item)
    assert result["decision"] == "needs_manual_review" and result["facts"] == []
