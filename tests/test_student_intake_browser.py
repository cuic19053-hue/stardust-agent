import hashlib
import json
import re

import pytest
from fastapi.testclient import TestClient

from server import app as server_app
from server.proposal import student_intake_browser as browser


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
    nonce = re.search(r'<script nonce="([^"]+)" src="/static/student-intake-local.js', html.text).group(1)
    policy = html.headers["Content-Security-Policy"]
    assert f"script-src 'nonce-{nonce}'" in policy and "connect-src 'none'" in policy
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
    layers = [f for f in manifest["files"].values() if "patch" in f]
    assert len(layers) == 3
    shards = [manifest["files"][f"params_shard_{i}.bin"]["sha256"] for i in range(8)]
    assert hashlib.sha256("".join(shards).encode()).hexdigest() == "8dd9796e3d89e7097ea08b83d15a4c223b91a73683ebecfbba4a11f52b8c2da4"
    for asset in [*manifest["files"].values(), *(f["patch"] for f in layers)]:
        if asset.get("url", "").startswith("./"):
            data = (folder / asset["url"]).read_bytes()
            assert len(data) == asset["bytes"] and hashlib.sha256(data).hexdigest() == asset["sha256"]
