"""Score one downloaded Word document against a frozen rubric and independent human review."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from docx import Document
from pydantic import BaseModel, ConfigDict, Field

Text = Annotated[str, Field(min_length=1)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Index = Annotated[int, Field(ge=0)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Artifact(Record):
    path: Text
    sha256: Digest

    def checked(self, folder):
        file = folder / self.path
        if hashlib.sha256(file.read_bytes()).hexdigest() != self.sha256:
            raise ValueError(f"文件版本有变化，请重新核对 {file.name}")
        return file


class RequiredField(Record):
    # One zero-based paragraph index or table / row / column.
    location: list[Index]
    expected_fragment: Text


class Rubric(Record):
    template: Artifact
    sources: dict[Text, Artifact]  # reviewed UTF-8 transcripts, including school policies
    fields: dict[Text, RequiredField]
    layout_checks: dict[Text, Text]


class Fact(Record):
    draft_quote: Text
    verdict: Literal["correct", "error", "unresolved"]
    source_id: str = ""
    original_quote: str = ""


class Receipt(Record):
    provider_request_id: Text
    actual_micro_cny: Index


class Review(Record):
    rubric_sha256: Digest
    output: Artifact
    run_id: Text
    deployment: Text
    model: Text
    reviewer: Text
    facts: list[Fact]
    layout: dict[Text, Literal["pass", "fail", "unreviewed"]]
    # Keys are the billable calls in this run's journal; pending receipts stay null.
    receipts: dict[Text, Receipt | None]
    wallet_spent_micro_cny: Index | None


def field_text(doc, coords):
    if len(coords) == 1:
        return doc.paragraphs[coords[0]].text
    if len(coords) == 3:
        table, row, col = coords
        return doc.tables[table].rows[row].cells[col].text
    raise ValueError("栏目位置须为段落序号或表、行、列三个序号。")


def ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else None}


def evaluate(rubric_path: Path, review_path: Path):
    raw = rubric_path.read_bytes()
    rubric = Rubric.model_validate_json(raw)
    review = Review.model_validate_json(review_path.read_bytes())
    if review.rubric_sha256 != hashlib.sha256(raw).hexdigest():
        raise ValueError("标注对应的评分表版本不一致。")
    template = Document(rubric.template.checked(rubric_path.parent))
    doc = Document(review.output.checked(review_path.parent))
    sources = {key: artifact.checked(rubric_path.parent).read_text(encoding="utf-8")
               for key, artifact in rubric.sources.items()}
    # Keep paragraph boundaries so concatenation across cells cannot invent a quote.
    paragraphs = [p.text for p in doc.paragraphs]
    paragraphs.extend(p.text for table in doc.tables for row in table.rows for cell in row.cells for p in cell.paragraphs)
    seen = set()
    for fact in review.facts:
        if fact.draft_quote in seen or not any(fact.draft_quote in p for p in paragraphs):
            raise ValueError("事实标注重复或引句未在下载的正文中找到。")
        seen.add(fact.draft_quote)
        if fact.verdict != "unresolved" and (not fact.original_quote or fact.source_id not in sources
                                             or fact.original_quote not in sources[fact.source_id]):
            raise ValueError("可判定的事实需要能在指定版本材料中定位的原句。")
    missing = []
    for field, target in rubric.fields.items():
        if target.expected_fragment in field_text(template, target.location):
            raise ValueError(f"栏目 {field} 的预期答案已存在于空表，请核对标注。")
        try:
            text = field_text(doc, target.location)
        except IndexError:
            text = ""  # Output lost the required destination.
        if target.expected_fragment not in text:
            missing.append(field)
    if set(review.layout) != set(rubric.layout_checks):
        raise ValueError("逐页排版判定须覆盖评分表的每个检查点，未检查项填 unreviewed。")
    costs = [r for r in review.receipts.values() if r is not None]
    if len({r.provider_request_id for r in costs}) != len(costs):
        raise ValueError("同一供应商收据重复归入多个调用。")
    settled = (len(costs) == len(review.receipts) and bool(costs)
               and sum(r.actual_micro_cny for r in costs) == review.wallet_spent_micro_cny)
    unresolved = sum(f.verdict == "unresolved" for f in review.facts)
    layout_pending = sum(v == "unreviewed" for v in review.layout.values())
    return {"deployment": review.deployment, "model": review.model, "run_id": review.run_id,
            "output_sha256": review.output.sha256, "rubric_sha256": review.rubric_sha256,
            "fact_error": ratio(sum(f.verdict == "error" for f in review.facts), len(review.facts) - unresolved),
            "facts_unresolved": unresolved, "required_field_missing": ratio(len(missing), len(rubric.fields)),
            "missing_fields": missing, "layout_pass": ratio(sum(v == "pass" for v in review.layout.values()),
                                                            len(review.layout) - layout_pending),
            "layout_unreviewed": layout_pending, "delivery_cost_micro_cny": review.wallet_spent_micro_cny if settled else None,
            "cost_status": "reconciled" if settled else "unreconciled"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rubric", type=Path)
    parser.add_argument("review", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(evaluate(args.rubric, args.review), ensure_ascii=False, indent=2))
    except (OSError, ValueError, IndexError) as exc:
        parser.exit(1, f"评测未完成：{exc}\n")
