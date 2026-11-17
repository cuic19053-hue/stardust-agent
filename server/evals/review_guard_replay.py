"""Replay frozen real model outputs through the graph; never sends an inference."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import InMemorySaver
from langsmith import tracing_context
from server.proposal.review_graph import build_review_graph
from server.proposal.review_runs import GRAPH_VERSION, digest
from server.proposal.review_worker import LocalReviewModel

DEFAULT_ARCHIVE = Path(__file__).with_name("baselines") / "review_evidence_v2_20261118.json"


def replay_case(record):
    if digest(record["response"]) != record["response_sha256"]:
        raise ValueError("Frozen model response checksum mismatch")
    class Runs:
        report = None

        def raw(self, _run_id):
            return {"id": record["id"], "fingerprint": digest(record["snapshot"]), "mode": "model",
                    "graph_version": GRAPH_VERSION, "model": record["request"]["model"],
                    "snapshot": json.dumps(record["snapshot"], ensure_ascii=False)}

        def update(self, _run_id, **fields):
            if "report" in fields:
                self.report = fields["report"]

        def event(self, *_args):
            pass

        def model_result(self, _run_id, call):
            return call()

    calls = []
    def frozen_output(_run, content, schema):
        # Reject drift before returning an old output for a changed input.
        if record["request"]["messages"] != LocalReviewModel.messages(content):
            raise ValueError("Replay input differs from the original model request")
        if record["request"]["format"] != schema:
            raise ValueError("Replay schema differs from the original model request")
        calls.append(1)
        return {"content": record["response"]["message"]["content"], "metrics": {"source_output_replayed": True}}

    runs = Runs()
    with tracing_context(enabled=False):
        build_review_graph(runs, InMemorySaver(), frozen_output).invoke(
            {"run_id": record["id"]}, {"configurable": {"thread_id": record["id"]}, "recursion_limit": 15})
    if calls != [1] or runs.report is None:
        raise ValueError("The replay did not consume exactly one frozen output")
    return {"case_id": record["case_id"], "dataset": record["dataset"], "input_matches": True,
            "source_response_sha256": digest(record["response"]), "report": runs.report}


def replay_archive(archive):
    files = ("review_graph.py", "review_evidence.py", "review_verification.py", "review_runs.py", "retrieval.py", "tools.py")
    import hashlib
    return {"execution_kind": "frozen_real_model_response_replay", "new_model_calls": 0,
            "reviewer_kind": "agent", "independent_human_gold": False,
            "source_hashes": {f"server/proposal/{f}": hashlib.sha256((ROOT / "server/proposal" / f).read_bytes()).hexdigest() for f in files},
            "runs": [replay_case(record) for record in archive["records"]]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = replay_archive(json.loads(args.archive.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"Replayed {len(result['runs'])} frozen outputs; new model calls: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
