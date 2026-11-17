"""Frozen real outputs must match original inputs; no inference is permitted."""
from copy import deepcopy
import json

import pytest

from server.evals.review_guard_replay import DEFAULT_ARCHIVE, replay_archive, replay_case


def archive():
    return json.loads(DEFAULT_ARCHIVE.read_text())


def test_frozen_real_outputs_preserve_targets_and_keep_unverified_findings_separate(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx.Client, "post", lambda *_args, **_kwargs: pytest.fail("Replay must not send a model request"))
    data = archive()
    replay = replay_archive(data)
    assert replay["new_model_calls"] == 0
    assert replay["runs"] == data["guard_replay"]["runs"]
    found = 0
    for record, run in zip(data["records"], replay["runs"]):
        report = run["report"]
        assert report["complete"] and run["input_matches"]
        required = [i for i in report["issues"] if i["origin"] == "rule"]
        assert len(required) == len(record["targets"])
        found += len(required)
        hypotheses = {h["problem"] for h in report["unverified_hypotheses"]}
        assert not hypotheses & {i["problem"] for i in report["issues"]}
        assert not hypotheses & {f["reason"] for f in report["follow_up"]}
    assert found == 8 and len(replay["runs"]) == 10
    assert data["guard_review"]["independent_human_gold"] is False


@pytest.mark.parametrize("change", ["input", "schema", "response"])
def test_replay_rejects_changed_inputs_schemas_or_outputs(change):
    record = deepcopy(archive()["records"][0])
    if change == "input":
        record["request"]["messages"][-1]["content"] += "changed"
    elif change == "schema":
        record["request"]["format"]["title"] = "ChangedSchema"
    else:
        record["response"]["message"]["content"] = '{"issues": []}'
    with pytest.raises(ValueError):
        replay_case(record)
