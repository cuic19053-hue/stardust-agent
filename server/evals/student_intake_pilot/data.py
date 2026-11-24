"""Freeze and verify the local, fictional intake experiment before fitting."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import itertools
import json
from pathlib import Path
import re
import unicodedata

from server.evals.student_intake_baseline import DATASET, load_cases
from server.proposal.student_intake import FIELDS, IntakeCandidate, evidence_errors, extraction_messages

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "datasets/student_intake/v2"
SPLITS = ("train", "validation", "test")


def digest(path):
    with Path(path).open("rb") as reader:
        return hashlib.file_digest(reader, "sha256").hexdigest()


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8") as writer:
        json.dump(value, writer, ensure_ascii=False, indent=2)
        writer.write("\n")


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as reader:
        return [json.loads(line) for line in reader if line.strip()]


def target(case):
    return {"schema_version": "student-intake-v1", "facts": case["gold"],
            "follow_up_fields": case["required_follow_up"]}


def split_cases(split):
    if split not in SPLITS:
        raise ValueError(split)
    manifest = json.loads((DATA / "manifest.json").read_text())
    path = DATA / f"{split}.jsonl"
    if digest(path) != manifest["splits"][split]["sha256"]:
        raise ValueError(f"{split} has changed since sealing")
    return read_jsonl(path)


def normalized(case):
    text = "".join(t["content"] for t in case["turns"])
    text = re.sub(r"[零〇一二两三四五六七八九十百千万\d.]+", "#", text)
    return "".join(c for c in unicodedata.normalize("NFKC", text) if c.isalnum() or c == "#")


def seal():
    splits = {s: read_jsonl(DATA / f"{s}.jsonl") for s in SPLITS}
    legacy = load_cases()["cases"]
    groups = {k: {} for k in ("id", "student_id", "session_id", "family_id", "normalized_dialogue")}
    grams = {}
    metrics = {}
    for split, cases in {"observed_v1": legacy, **splits}.items():
        fields, states, tags = Counter(), Counter(), Counter()
        for case in cases:
            norm = normalized(case)
            for group, seen in groups.items():
                key = norm if group == "normalized_dialogue" else case.get(group)
                if key is None and split == "observed_v1":
                    continue
                if not key or (key in seen and (seen[key] != split or group in {"id", "normalized_dialogue"})):
                    raise ValueError(f"split leak/duplicate: {group}/{case['id']}")
                seen[key] = split
            grams[(split, case["id"])] = {norm[i:i+3] for i in range(len(norm)-2)}
            if split == "observed_v1":
                continue
            if (case["synthetic"] is not True or case["source"] != "codex-original-synthetic-v2"
                or case["authorization"] != "user-request-local-training-and-evaluation"):
                raise ValueError(f"unapproved provenance: {case['id']}")
            extraction_messages(case["turns"])
            label = IntakeCandidate.model_validate(target(case))
            if evidence_errors(label, case["turns"]):
                raise ValueError(f"reference not spoken by the student: {case['id']}")
            if re.search(r"(?<!\d)(?:1[3-9]\d{9}|\d{15,18}[\dXx]?)(?!\d)|[\w.+-]+@[\w.-]+\.[A-Za-z]+", str(case["turns"])):
                raise ValueError(f"identifier in synthetic text: {case['id']}")
            if set(case["forbidden_fields"]) & {f.field for f in label.facts}:
                raise ValueError(f"contradictory exclusion: {case['id']}")
            blocked = set(case["forbidden_follow_up"]) | {f.field for f in label.facts if f.status == "withheld"}
            if blocked & set(label.follow_up_fields):
                raise ValueError(f"forbidden follow-up in annotations: {case['id']}")
            for fact in label.facts:
                if fact.status == "conflict" and len({(e.turn_id, e.quote) for e in fact.evidence}) < 2:
                    raise ValueError(f"missing independent conflict evidence: {case['id']}")
                fields[fact.field] += 1
                states[fact.status] += 1
            tags.update(case["tags"])
        if split != "observed_v1":
            metrics[split] = {"sha256": digest(DATA / f"{split}.jsonl"), "cases": len(cases),
                "students": len({c["student_id"] for c in cases}), "sessions": len({c["session_id"] for c in cases}),
                "families": len({c["family_id"] for c in cases}), "fields": dict(sorted(fields.items())),
                "statuses": dict(states), "tags": dict(tags)}
    proximity = sorted(((len(a & b) / len(a | b), x, y)
                        for (x, a), (y, b) in itertools.combinations(grams.items(), 2)
                        if x[0] != y[0]), reverse=True)
    if proximity[0][0] >= 0.55:
        raise ValueError(f"inspect near duplicates before sealing: {proximity[:3]}")
    return {"version": "student-intake-synthetic-v2", "sealed_utc": datetime.now(timezone.utc).isoformat(),
            "source": "Original Chinese fictional dialogues and labels written for the user's local experiment.",
            "authorization": "User explicitly requested preparation and local training/evaluation on labelled synthetic data.",
            "review": "Codex semantic second pass and deterministic validation; no independent human annotation.",
            "split_policy": "Scenario families authored for one split, distinct fictional student and session identifiers; v1 held out of every split.",
            "observed_v1_sha256": digest(DATASET), "splits": metrics,
            "cross_split_top_trigram_jaccard_after_normalization": proximity[:8],
            "design_field_count": len(FIELDS)}


if __name__ == "__main__":
    write_new(DATA / "manifest.json", seal())
