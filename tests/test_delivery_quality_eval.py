import hashlib
import json

import pytest
from docx import Document

from server.evals.delivery_quality import evaluate


def test_frozen_template_scoring_and_cost_reconciliation(tmp_path):
    def artifact(name):
        return {"path": name, "sha256": hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()}

    doc = Document()
    doc.add_table(1, 2)
    doc.save(tmp_path / "form.docx")
    doc.tables[0].cell(0, 0).text = "已完成观测。"
    doc.add_paragraph("预算1200元。")  # Merely appearing elsewhere does not fill the required cell.
    doc.save(tmp_path / "output.docx")
    (tmp_path / "notes.txt").write_text("准备开展观测。预算1200元。", encoding="utf-8")
    rubric = {"template": artifact("form.docx"), "sources": {"notes": artifact("notes.txt")},
              "fields": {"预算": {"location": [0, 0, 1], "expected_fragment": "1200元"}},
              "layout_checks": {"p1": "第一页签章栏完整", "p2": "分页正确"}}
    pre = tmp_path / "rubric.json"
    pre.write_text(json.dumps(rubric), encoding="utf-8")
    post = tmp_path / "review.json"
    review = {"rubric_sha256": artifact(pre.name)["sha256"], "output": artifact("output.docx"),
              "model": "fixture", "deployment": "local-fixture", "reviewer": "fixture", "run_id": "sample",
              "facts": [{"draft_quote": "已完成观测", "verdict": "error", "source_id": "notes", "original_quote": "准备开展观测"},
                        {"draft_quote": "预算1200元", "verdict": "correct", "source_id": "notes", "original_quote": "预算1200元"}],
              "layout": {"p1": "pass", "p2": "unreviewed"}, "wallet_spent_micro_cny": 1200,
              "receipts": {"call-1": {"provider_request_id": "abc", "actual_micro_cny": 1200}}}

    def score():
        post.write_text(json.dumps(review), encoding="utf-8")
        return evaluate(pre, post)

    report = score()
    assert report["fact_error"]["rate"] == 0.5 and report["required_field_missing"]["rate"] == 1
    assert report["layout_pass"]["denominator"] == 1 and report["layout_unreviewed"] == 1
    assert report["delivery_cost_micro_cny"] == 1200
    review["receipts"]["call-2"] = None
    assert score()["delivery_cost_micro_cny"] is None
    review["receipts"]["call-2"] = review["receipts"]["call-1"]
    with pytest.raises(ValueError, match="重复"):
        score()
    review["receipts"] = {}
    review["facts"][0]["original_quote"] = "不存在的证据"
    with pytest.raises(ValueError, match="原句"):
        score()
    review["facts"] = []
    assert score()["fact_error"]["rate"] is None
    doc.add_paragraph("下载之后又被编辑")
    doc.save(tmp_path / "output.docx")
    with pytest.raises(ValueError, match="版本"):
        score()
