#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync_version_meta.py — 把 version.json 的体量元数据同步到实际状态。

为什么需要
----------
`version.json` 里的 `total_lines` 与 `categories_breakdown[*].lines` 曾长期
停留在 35,227 / 2026-11-08，实际已漂移数十行。这类「软元数据」不会让测试
失败，但会让外部读者拿到错误数字，属于典型的静默失真。

本脚本重算这些字段；`check_doc_consistency.py` 会校验它们是否一致。

同步字段
--------
- total_lines          35 个 SKILL.md 的行数总和
- categories_breakdown[*].lines   按 index.json 的 category 分组统计
- categories_breakdown[*].count   分组内 skill 数
- total_skills / skills           与 subskills/ 目录一致
- generated_at         同步日期（仅在内容实际变化时更新）

不改动：project_version / description（版本语义由维护者决定）。

用法
----
    python3 scripts/sync_version_meta.py --dry-run
    python3 scripts/sync_version_meta.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
SUBSKILLS = ROOT / "subskills"
VERSION = ROOT / "version.json"
INDEX = ROOT / "index.json"


def _lines(md: Path) -> int:
    return len(md.read_text(encoding="utf-8").splitlines())


def compute() -> Dict:
    dirs = sorted(d.name for d in SUBSKILLS.iterdir() if d.is_dir())
    idx = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else {}
    cat_of = {s["name"]: s.get("category") for s in idx.get("skills", [])}
    display_of = {s["name"]: s.get("display_name", s.get("category"))
                  for s in idx.get("skills", [])}

    per_cat_lines: Dict[str, int] = {}
    per_cat_count: Dict[str, int] = {}
    total_lines = 0
    for name in dirs:
        md = SUBSKILLS / name / "SKILL.md"
        if not md.exists():
            continue
        n = _lines(md)
        total_lines += n
        cat = cat_of.get(name) or "other"
        per_cat_lines[cat] = per_cat_lines.get(cat, 0) + n
        per_cat_count[cat] = per_cat_count.get(cat, 0) + 1

    return {
        "dirs": dirs,
        "total_lines": total_lines,
        "per_cat_lines": per_cat_lines,
        "per_cat_count": per_cat_count,
        "display_of": display_of,
    }


def sync(dry_run: bool = False) -> Tuple[List[str], Dict]:
    ver = json.loads(VERSION.read_text(encoding="utf-8"))
    data = compute()
    changes: List[str] = []

    if ver.get("total_lines") != data["total_lines"]:
        changes.append(f"total_lines: {ver.get('total_lines')} → {data['total_lines']}")
        ver["total_lines"] = data["total_lines"]

    if ver.get("total_skills") != len(data["dirs"]):
        changes.append(f"total_skills: {ver.get('total_skills')} → {len(data['dirs'])}")
        ver["total_skills"] = len(data["dirs"])

    if sorted(ver.get("skills") or []) != sorted(data["dirs"]):
        changes.append(f"skills 列表：{len(ver.get('skills') or [])} → {len(data['dirs'])} 条")
        ver["skills"] = sorted(data["dirs"])

    breakdown = ver.setdefault("categories_breakdown", {})
    for cat, lines in sorted(data["per_cat_lines"].items()):
        entry = breakdown.setdefault(cat, {})
        if entry.get("lines") != lines:
            changes.append(f"{cat}.lines: {entry.get('lines')} → {lines}")
            entry["lines"] = lines
        cnt = data["per_cat_count"][cat]
        if entry.get("count") != cnt:
            changes.append(f"{cat}.count: {entry.get('count')} → {cnt}")
            entry["count"] = cnt
        if "display" not in entry:
            entry["display"] = cat

    # 清理已不存在分类的条目
    for cat in list(breakdown):
        if cat not in data["per_cat_lines"]:
            changes.append(f"移除已不存在分类：{cat}")
            del breakdown[cat]

    if ver.get("categories_count") != len(data["per_cat_lines"]):
        changes.append(
            f"categories_count: {ver.get('categories_count')} → {len(data['per_cat_lines'])}")
        ver["categories_count"] = len(data["per_cat_lines"])

    if changes and ver.get("generated_at") != date.today().isoformat():
        changes.append(f"generated_at: {ver.get('generated_at')} → {date.today().isoformat()}")
        ver["generated_at"] = date.today().isoformat()

    if changes and not dry_run:
        VERSION.write_text(
            json.dumps(ver, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return changes, data


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同步 version.json 体量元数据")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    changes, data = sync(dry_run=args.dry_run)
    if not changes:
        print(f"✅ version.json 已是最新（{len(data['dirs'])} 个 skill，"
              f"{data['total_lines']} 行）")
        return 0

    verb = "将更新" if args.dry_run else "已更新"
    print(f"{verb} version.json：")
    for c in changes:
        print("  -", c)
    if args.dry_run:
        print("\n（--dry-run，未写盘）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
