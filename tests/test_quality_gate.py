import json

import pytest

from server.evals.quality_gate import BASELINE, regressions


@pytest.fixture()
def scored_baseline():
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    metrics = {}
    for bound in (baseline["minimum"], baseline["maximum"]):
        for group, values in bound.items():
            metrics.setdefault(group, {}).update(values)
    drafts = {case_id: {"present": list(case["present"]), "forbidden": list(case["known_forbidden"])}
              for case_id, case in baseline["draft_cases"].items()}
    assert regressions(metrics, drafts, baseline) == []
    return metrics, drafts, baseline


@pytest.mark.parametrize(("group", "key", "delta"), [
    ("intake", "field_f1", -0.01),
    ("review_replay", "unreviewed_findings", 1),
])
def test_quality_gate_rejects_metric_regressions(scored_baseline, group, key, delta):
    metrics, drafts, baseline = scored_baseline
    metrics[group][key] += delta
    assert regressions(metrics, drafts, baseline)[0].startswith(f"{group}.{key} = ")


def test_draft_cannot_trade_a_known_issue_for_a_new_one(scored_baseline):
    metrics, drafts, baseline = scored_baseline
    drafts["research_foundation"]["forbidden"] = ["虚构准确率"]
    assert "new forbidden checks ['虚构准确率']" in regressions(metrics, drafts, baseline)[0]
