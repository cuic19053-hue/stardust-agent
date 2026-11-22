#!/usr/bin/env python3
"""Aggregate project-notice spreadsheets into a PII-free statistical baseline.

Design constraints (enforced in code, not by convention):
- Personal data columns are dropped before aggregation and never logged.
- Only aggregate statistics are written; no project titles are emitted.
- Any group with fewer than MIN_GROUP records is suppressed (k-anonymity).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import statistics as st
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
import xml.etree.ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
MIN_GROUP = 5

# Columns that must never reach the output or the logs.
PII_COLUMNS = (
    "负责人", "成员", "姓名", "学号", "导师", "指导教师", "电话", "手机",
    "邮箱", "身份证", "证件", "联系方式", "编号", "序号",
)

FIELD_ALIASES = {
    "title": ("项目名称", "项目名", "课题名称", "作品名称"),
    "level": ("级别", "项目级别", "立项级别"),
    "type": ("项目类型", "类型", "项目类别"),
    "college": ("学院名称", "学院", "所在学院", "院系"),
}

TITLE_PATTERNS = {
    "基于式": r"^基于|基于",
    "研究型": r"研究",
    "系统平台模型": r"系统|平台|模型|架构",
    "智能AI": r"智能|AI|人工智能|算法",
    "数字数据": r"数字|数据",
    "乡村振兴": r"乡村|农村|振兴|农户",
    "文化非遗": r"文化|非遗|传统",
    "设计开发": r"设计|开发|构建",
}


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def col_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref)
    if not letters:
        return 0
    n = 0
    for ch in letters.group(1):
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(path: Path) -> list[list[str]]:
    try:
        book = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        die(f"{path.name} 不是有效的 xlsx，请确认下载的是表格而不是验证码页")

    names = book.namelist()
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(book.read("xl/sharedStrings.xml"))
        for item in root.findall(f"{NS}si"):
            shared.append("".join(t.text or "" for t in item.iter(f"{NS}t")))

    rows: list[list[str]] = []
    sheets = sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    for sheet in sheets:
        root = ET.fromstring(book.read(sheet))
        for row in root.findall(f".//{NS}row"):
            cells: dict[int, str] = {}
            for cell in row.findall(f"{NS}c"):
                value = cell.find(f"{NS}v")
                inline = cell.find(f"{NS}is")
                if value is None and inline is None:
                    continue
                if inline is not None:
                    text = "".join(t.text or "" for t in inline.iter(f"{NS}t"))
                else:
                    text = value.text or ""
                if cell.get("t") == "s" and text.isdigit():
                    idx = int(text)
                    text = shared[idx] if idx < len(shared) else text
                cells[col_index(cell.get("r", "A1"))] = text
            if cells:
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
    return rows


def is_pii(name: str) -> bool:
    return any(token in name for token in PII_COLUMNS)


def find_header(rows: list[list[str]]) -> tuple[int, dict[str, int]]:
    for index, row in enumerate(rows[:40]):
        joined = "|".join(str(c) for c in row)
        if "项目名称" not in joined and "项目名" not in joined:
            continue
        mapping: dict[str, int] = {}
        for key, aliases in FIELD_ALIASES.items():
            for alias in aliases:
                hit = next((i for i, c in enumerate(row) if str(c).strip() == alias), None)
                if hit is not None:
                    mapping[key] = hit
                    break
        if "title" in mapping:
            return index, mapping
    die("未找到包含「项目名称」的表头行")
    return 0, {}


def extract(path: Path) -> list[dict[str, str]]:
    rows = read_xlsx(path)
    header_at, mapping = find_header(rows)
    header = rows[header_at]

    # Fail loudly if a PII column is being read by mistake.
    for key, idx in mapping.items():
        column = str(header[idx]).strip() if idx < len(header) else ""
        if is_pii(column):
            die(f"字段映射命中个人信息列「{column}」，已中止")

    records: list[dict[str, str]] = []
    for row in rows[header_at + 1:]:
        def take(key: str) -> str:
            idx = mapping.get(key)
            if idx is None or idx >= len(row):
                return ""
            return str(row[idx]).strip()

        title = take("title")
        if not title or title in ("项目名称", "合计", "备注"):
            continue
        records.append({
            "title": title,
            "level": take("level"),
            "type": take("type"),
            "college": take("college"),
        })
    return records


def summarize(group: list[dict[str, str]]) -> dict[str, Any]:
    lengths = sorted(len(r["title"]) for r in group)
    total = len(lengths)
    p25 = lengths[total // 4]
    p75 = lengths[(3 * total) // 4]
    patterns = {}
    for name, pattern in TITLE_PATTERNS.items():
        hits = sum(1 for r in group if re.search(pattern, r["title"]))
        patterns[name] = round(hits / total, 3)
    return {
        "n": total,
        "title_len": {
            "median": int(st.median(lengths)),
            "mean": round(st.mean(lengths), 1),
            "p25": p25,
            "p75": p75,
            "min": lengths[0],
            "max": lengths[-1],
        },
        "patterns": patterns,
        "types": dict(Counter(r["type"] for r in group if r["type"]).most_common()),
    }


def build(manifest: dict[str, Any], root: Path) -> dict[str, Any]:
    buckets: dict[tuple[str, str], list[dict[str, str]]] = {}
    coverage: list[dict[str, Any]] = []

    for source in manifest.get("sources", []):
        path = (root / source["file"]).resolve()
        if not path.is_file():
            print(f"warn: 跳过不存在的文件 {source['file']}", file=sys.stderr)
            continue
        records = extract(path)
        for record in records:
            key = (source.get("track", ""), record["level"] or "未标注")
            buckets.setdefault(key, []).append(record)
        coverage.append({
            "school": source.get("school", ""),
            "year": source.get("year"),
            "track": source.get("track", ""),
            "source_url": source.get("source_url", ""),
            "file": source["file"],
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "records": len(records),
        })

    stats: dict[str, Any] = {}
    suppressed: list[str] = []
    for (track, level), group in sorted(buckets.items()):
        if len(group) < MIN_GROUP:
            suppressed.append(f"{track}/{level}(n={len(group)})")
            continue
        stats.setdefault(track or "未标注", {})[level] = summarize(group)

    schools = {c["school"] for c in coverage if c["school"]}
    years = {c["year"] for c in coverage if c["year"]}
    return {
        "version": 1,
        "generated_from": "official public notices (aggregate statistics only)",
        "privacy": {
            "pii_columns_dropped": list(PII_COLUMNS),
            "min_group": MIN_GROUP,
            "suppressed_groups": suppressed,
            "note": "不包含任何单条项目名称、姓名、学号或导师信息。",
        },
        "coverage": {
            "sources": len(coverage),
            "schools": sorted(schools),
            "years": sorted(str(y) for y in years),
            "total_records": sum(c["records"] for c in coverage),
            "files": coverage,
        },
        "limitations": [
            "仅覆盖立项名单的命名与元数据，不代表申报书正文特征。",
            "样本来自少数学校与年度，不可外推为全国结论。",
            "省级与国家级分别统计，不合并推断。",
        ],
        "baselines": stats,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="contest/manifest.json")
    parser.add_argument("--out", default="contest/baseline_stats.json")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.is_file():
        die(f"找不到 manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    result = build(manifest, manifest_path.parent)
    out = Path(args.out).resolve()
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    cov = result["coverage"]
    print(f"来源 {cov['sources']} 个 / 学校 {len(cov['schools'])} 所 / 年度 {len(cov['years'])} 个 / 记录 {cov['total_records']} 条")
    for track, levels in result["baselines"].items():
        for level, stat in levels.items():
            print(f"  {track}/{level}: n={stat['n']} 标题中位 {stat['title_len']['median']} 字")
    if result["privacy"]["suppressed_groups"]:
        print("  已抑制小样本组: " + ", ".join(result["privacy"]["suppressed_groups"]))
    print(f"已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
