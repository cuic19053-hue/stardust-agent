#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apply_skill_fixes.py — 一次性重放 2026-11-15 审查确定的 SKILL.md 修复。

背景
----
本轮改造的修复项分散在 35 个 SKILL.md 中，其中一部分由
`inject_writing_style.py` 批量注入（写作规范 + 合规底线块），
另一部分是个别文件的行级修订。为了让整套修复**可重放、可审计**，
把它们收敛到这个脚本里，而不是散落在一次性的 shell 命令中。

修复分组
--------
F1 领导人表述      「最高领导人」→「习近平总书记重要讲话」（含 references）
F2 政策文号        不可核验文号 → 真实文号或占位
F3 无来源统计      规模断言补出处要求
F4 公文误用        以公文条例作申请书格式依据 → 加不适用说明
F5 政策金额时效    插入文档级「金额与政策时效」声明
F6 跨引用金额      奖学金互相引用处补年度
F7 英文检索锚点    description 追加 `（EN: ...）`，YAML 安全写法
F8 触发词去重      skill 内重复 triggers 只保留首次
F9 写作规范+合规   调用 inject_writing_style.py 批量注入

用法
----
    python3 scripts/apply_skill_fixes.py --dry-run
    python3 scripts/apply_skill_fixes.py
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parent.parent
SUBSKILLS = ROOT / "subskills"

# --- F1 领导人表述 ---------------------------------------------------------
LEADER_FIXES: List[Tuple[str, str, str]] = [
    ("challenge-cup/SKILL.md",
     "5. **领导人讲话**：应包含最高领导人讲话（如适用），如习近平讲话 2 次",
     "5. **领导人讲话**：应包含习近平总书记重要讲话（如适用），如 2 次引用习近平总书记重要讲话"),
    ("entrepreneurship-training/SKILL.md",
     "### 4.3 最高领导人讲话引用", "### 4.3 习近平总书记重要讲话引用"),
    ("entrepreneurship-training/SKILL.md",
     "| 无最高领导人讲话 | 至少引用 1 次（如适用） |",
     "| 无习近平总书记重要讲话 | 至少引用 1 次（如适用） |"),
    ("innovation-research/SKILL.md",
     "- 应包含最高领导人讲话（如适用）", "- 应包含习近平总书记重要讲话（如适用）"),
    ("innovation-research/SKILL.md",
     "❌ 只列 8 条政策文件，无最高领导人讲话", "❌ 只列 8 条政策文件，无习近平总书记重要讲话"),
    ("innovation-research/SKILL.md",
     "19. 政策引用含最高领导人讲话", "19. 政策引用含习近平总书记重要讲话"),
    ("internet-plus/SKILL.md",
     "4. 应包含**最高领导人讲话**（如适用）", "4. 应包含**习近平总书记重要讲话**（如适用）"),
    ("challenge-cup/references/intake-checklist.md",
     "5. 应包含最高领导人讲话（如适用）", "5. 应包含习近平总书记重要讲话（如适用）"),
    ("entrepreneurship-training/references/intake-checklist.md",
     "| leader_speeches | 最高领导人讲话引用 | ≥ 1 条 |",
     "| leader_speeches | 习近平总书记重要讲话引用 | ≥ 1 条 |"),
    ("entrepreneurship-training/references/intake-checklist.md",
     "4. 应包含最高领导人讲话（如适用）", "4. 应包含习近平总书记重要讲话（如适用）"),
    ("innovation-research/references/case-spec.md",
     "5. **最高领导人讲话**：应包含最高领导人讲话（如适用）",
     "5. **习近平总书记重要讲话**：应包含习近平总书记重要讲话（如适用）"),
    ("innovation-research/references/case-spec.md",
     "**最高领导人讲话**：", "**习近平总书记重要讲话**："),
    ("innovation-research/references/intake-checklist.md",
     "| 国家政策引用 | ≥8 条政策文件 | policy_citations | 时序 5 年+，含最高领导人讲话，按时间倒序 |",
     "| 国家政策引用 | ≥8 条政策文件 | policy_citations | 时序 5 年+，含习近平总书记重要讲话，按时间倒序 |"),
    ("internet-plus/references/intake-checklist.md",
     "- 必须含最高领导人讲话（如适用，如习近平总书记关于创新创业/应急救援/乡村振兴的讲话）",
     "- 必须含习近平总书记重要讲话（如适用，如关于创新创业/应急救援/乡村振兴的讲话）"),
]

# --- F2 政策文号 -----------------------------------------------------------
DOCNO_FIXES: List[Tuple[str, str, str]] = [
    # 应急体系规划真实文号为国发〔2021〕36 号
    ("challenge-cup/SKILL.md", "（国发〔2021〕109 号）", "（国发〔2021〕36 号）"),
    ("challenge-cup/build.py", '"doc_no": "国发〔2021〕109 号"', '"doc_no": "国发〔2021〕36 号"'),
    ("challenge-cup/references/intake-checklist.md",
     "国发〔2021〕109 号", "国发〔2021〕36 号"),
    # 《中国制造 2025》真实文号为国发〔2015〕28 号
    ("internet-plus/SKILL.md",
     "| 制造强国 | 国发〔2025〕XX 号《中国制造 2025》 | 智能制造项目 |",
     "| 制造强国 | 国发〔2015〕28 号《中国制造 2025》 | 智能制造项目 |"),
    # 未来年份文号 → 占位
    ("challenge-cup/build.py",
     '"doc_no": "国办发〔2026〕12 号"', '"doc_no": "国办发〔20XX〕XX 号"'),
    ("innovation-research/build.py",
     '"doc_no": "国办发〔2026〕8 号"', '"doc_no": "国办发〔20XX〕XX 号"'),
    ("innovation-research/references/case-spec.md",
     '"doc_no": "国办发〔2026〕8 号"', '"doc_no": "国办发〔20XX〕XX 号"'),
]

# --- F3 无来源统计 ---------------------------------------------------------
STAT_FIXES: List[Tuple[str, str, str]] = [
    ("military-enlistment/SKILL.md",
     "截至 2024 年，全国累计已有 200 余万名大学生参军入伍，大学生新兵占比超过 60%。",
     "据教育部、国防部征兵办公室历年公布的口径，大学生已成为新兵征集的主体"
     "（具体人数与占比请引用你所在年度官方发布，本仓库不提供未标注出处的数字）。"),
    ("western-plan/SKILL.md",
     "**实施年份**：2003 年至今（截至 2025 年累计选派 50 余万名志愿者）。",
     "**实施年份**：2003 年至今（累计选派规模请引用团中央最新年度公布口径，"
     "本仓库不提供未标注出处的数字）。"),
    ("western-plan/SKILL.md",
     "深入了解西部计划自 2003 年实施以来累计选派 50 余万名志愿者的项目历程",
     "深入了解西部计划自 2003 年实施以来的项目历程"),
    ("western-plan/SKILL.md",
     '"西部计划是团中央、教育部、财政部、人社部四部委联合实施的国家级志愿服务项目，'
     '自 2003 年实施以来累计选派 50 余万名志愿者。"',
     '"西部计划是团中央、教育部、财政部、人社部四部委联合实施的国家级志愿服务项目，'
     '自 2003 年起持续选派高校毕业生赴西部基层服务（历年选派规模以团中央公布数据为准）。"'),
    ("western-plan/references/json-schema.md",
     "深入了解西部计划自 2003 年实施以来累计选派 50 余万名志愿者的项目历程",
     "深入了解西部计划自 2003 年实施以来的项目历程"),
    ("youth-league-application/SKILL.md",
     "**推荐表述**：截至 2023 年底，全国共有共青团员 7415.1 万名，团组织 432.6 万个。",
     "**推荐表述**：截至 2023 年底，全国共有共青团员 7415.1 万名，团组织 432.6 万个"
     "（数据来源：共青团中央 2024 年 5 月发布的团内统计公报；使用前请核对最新年度公报）。"),
    ("youth-league-application/SKILL.md",
     "| Q21 | 政治表述时效 | 提及团的十九大精神（不引十七大、十八大）；团员人数用 7415.1 万（不引 7300 万、7358 万） |",
     "| Q21 | 政治表述时效 | 提及团的十九大精神（不引十七大、十八大）；"
     "团员人数用 7415.1 万（2023 年底团中央公报口径，以最新年度公报为准） |"),
    ("major-transfer/references/intake-checklist.md",
     "| career_research | 行业调研报告（职业型可选） | 医疗 AI 行业人才缺口 30 万 |",
     "| career_research | 行业调研报告（职业型可选） | `[行业]` 人才缺口 `[N]` 万"
     "（须引用可核验的行业报告，不填无出处数字） |"),
]

# --- F4 公文适用范围 -------------------------------------------------------
OFFICIAL_FIXES: List[Tuple[str, str, str]] = [
    ("selected-graduate/SKILL.md",
     "| 《党政机关公文处理工作条例》 | 中共中央、国务院 | 2012 年 | 申请书格式参考 |",
     "| 《党政机关公文处理工作条例》 | 中共中央、国务院 | 2012 年 | "
     "仅参考行文严谨性；申请书属个人申请材料，不适用公文格式 |"),
    ("selected-graduate/SKILL.md",
     "| 6 | 《党政机关公文处理工作条例》 | 中共中央、国务院 | 2012 | 公文规范 |",
     "| 6 | 《党政机关公文处理工作条例》 | 中共中央、国务院 | 2012 | "
     "行文严谨性参考（非公文格式依据） |"),
    ("summary-report/SKILL.md",
     "docx 生成 → 政治与公文规范质检", "docx 生成 → 政治表述与书信体格式质检"),
    ("national-scholarship/SKILL.md",
     "本 skill 只协助规范政治类公文的**格式与事实表述**",
     "本 skill 只协助规范政治类申请材料的**格式与事实表述**"),
]

# --- F6 跨引用金额时效 -----------------------------------------------------
MONEY_FIXES: List[Tuple[str, str, str]] = [
    ("single-scholarship/SKILL.md",
     "国奖（10000 元）、励志（6000 元）",
     "国奖（10000 元）、励志（6000 元）——金额为 2024 年起标准，以当年通知为准——"),
    ("university-scholarship/SKILL.md",
     "国家奖学金（10000 元）与国家励志奖学金（6000 元）",
     "国家奖学金（10000 元）与国家励志奖学金（6000 元，均为 2024 年起标准，以当年通知为准）"),
    ("military-enlistment/SKILL.md",
     "学费补偿代偿：本专科生每年最高 16000 元，研究生每年最高 20000 元",
     "学费补偿代偿：本专科生每年最高 16000 元，研究生每年最高 20000 元"
     "（现行标准，须以你入学/入伍当年财政部、教育部、中央军委相关通知为准）"),
    ("military-enlistment/SKILL.md",
     "对应征入伍的大学生，在校期间缴纳的学费实行一次性补偿或获得国家助学贷款代偿。"
     "本专科生每年最高 16000 元，研究生每年最高 20000 元。",
     "对应征入伍的大学生，在校期间缴纳的学费实行一次性补偿或获得国家助学贷款代偿。"
     "本专科生每年最高 16000 元，研究生每年最高 20000 元"
     "（该额度按现行政策执行，每年可能调整，须核对当年通知）。"),
    ("military-enlistment/SKILL.md",
     "本人在校期间学费将获得每年最高 16000 元的补偿代偿；",
     "本人在校期间学费将获得每年最高 `[按当年政策填写]` 元的补偿代偿"
     "（现行标准为 16000 元，须核对当年通知）；"),
    ("military-enlistment/SKILL.md",
     "- 学费补偿代偿（每年最高 16000 元，本专科；20000 元，研究生）",
     "- 学费补偿代偿（每年最高 16000 元，本专科；20000 元，研究生；以当年通知为准）"),
    ("military-enlistment/SKILL.md",
     "1. **学费补偿代偿**：本专科生每年最高 16000 元，研究生每年最高 20000 元",
     "1. **学费补偿代偿**：本专科生每年最高 16000 元，研究生每年最高 20000 元（以当年通知为准）"),
    ("military-enlistment/SKILL.md",
     '- 必须含具体金额或条款（如"每年最高 16000 元"）',
     '- 必须含具体金额或条款（如"每年最高 16000 元，以当年通知为准"）'),
    ("csc-scholarship/SKILL.md",
     "（按国家与城市分档，例如美国一类地区约 1900 美元/月，英国约 1200~1500 英镑/月，德国约 1200 欧元/月）",
     "（按国家与城市分档，例如美国一类地区约 1900 美元/月，英国约 1200~1500 英镑/月，"
     "德国约 1200 欧元/月；具体额度以当年《国家留学基金资助出国留学人员选派指南》为准）"),
    ("csc-scholarship/SKILL.md",
     "| 安置费 | ✅（一次性 1000 元人民币） | ✅ | ✅ | ✅ |",
     "| 安置费 | ✅（一次性 1000 元人民币，以当年选派指南为准） | ✅ | ✅ | ✅ |"),
    ("csc-scholarship/SKILL.md",
     "35. CSC 资助生活费按城市分档（美国一类 1900 美元/月，二类 1700 美元/月；英国 1200~1500 英镑/月）？",
     "35. CSC 资助生活费按城市分档（美国一类 1900 美元/月，二类 1700 美元/月；"
     "英国 1200~1500 英镑/月；以当年选派指南为准）？"),
]

# F5 需要插入文档级时效声明的 skill
CURRENCY_NOTICE_SKILLS = [
    "military-enlistment", "national-scholarship", "motivation-scholarship",
    "university-scholarship", "single-scholarship", "enterprise-scholarship",
    "western-plan", "entrepreneurship-practice", "national-project-eval",
    "csc-scholarship",
]
CURRENCY_NOTICE = (
    "> **金额与政策时效**：本文出现的奖助金额、补贴标准、名额与比例均为写作时的政策口径，"
    "实际执行以你所在学校当年官方通知为准；未拿到通知时留空或写「待核对」，不按往年数字推算。\n\n"
)

# --- F7 英文检索锚点 -------------------------------------------------------
# 精简版 description：原描述把栏目清单塞进预加载字段（违反官方最佳实践 D5），
# 这里压到「做什么 + 何时用 + 关键区分」三要素，栏目细节留给正文。
DESC_COMPACT = {
    "internet-plus":
        "生成「中国国际大学生创新大赛（原互联网+）」商业计划书 docx。"
        "适用于高教主赛道、红色之旅赛道与职教赛道；按当年赛事通知的评审维度组织章节，"
        "输出可提交的 Word 文档。",
    "major-transfer":
        "生成「转专业申请书」docx。适用于大一/大二学生申请转入其他专业；"
        "按个人情况、现专业学习、转专业原因、目标专业认知、转后规划五段组织，"
        "禁止贬低原专业，生成前须核对学校转专业政策。",
    "youth-league-application":
        "生成「入团申请书」docx。适用于中学/大学未入团青年申请加入共青团；"
        "书信体四段结构，须含共青团性质、团章、团史、五四精神等规范表述，"
        "字数按学校类型分档，最终递交须本人手写。",
}

EN_ANCHOR = {
    "challenge-cup": "Challenge Cup academic competition proposal",
    "civilized-student": "civic conduct award application",
    "class-collective": "outstanding class collective application",
    "college-research": "college-level research project proposal",
    "csc-scholarship": "CSC government scholarship application",
    "enterprise-scholarship": "corporate scholarship application",
    "entrepreneurship-practice": "startup practice project proposal",
    "entrepreneurship-training": "entrepreneurship training project proposal",
    "exchange-program": "exchange program application",
    "graduate-recommendation": "graduate school recommendation personal statement",
    "grant-application": "national financial aid grant application",
    "innovation-research": "undergraduate innovation research proposal",
    "internet-plus": "China International College Students Innovation Competition business plan",
    "internet-plus-red-tour": "Red Tour track business plan",
    "major-transfer": "major transfer application",
    "military-enlistment": "military enlistment application",
    "motivation-scholarship": "national inspirational scholarship application",
    "national-project-eval": "national project proposal review",
    "national-scholarship": "national scholarship application",
    "outstanding-cadre": "outstanding student cadre application",
    "outstanding-graduate": "outstanding graduate application",
    "outstanding-student": "outstanding student application",
    "outstanding-thesis": "outstanding thesis award application",
    "policy-lecture": "policy lecture social practice proposal",
    "selected-graduate": "selected graduate cadre application",
    "single-scholarship": "single-category scholarship application",
    "social-survey": "social survey practice proposal",
    "summary-report": "progress report and thought report",
    "tech-service": "rural technology service practice proposal",
    "university-research": "university-level research project proposal",
    "university-scholarship": "university-level scholarship application",
    "volunteer-teaching": "volunteer teaching practice proposal",
    "western-plan": "Go West volunteer program application",
    "youth-league-application": "Youth League membership application",
    "youth-league-conversion": "probationary Party member conversion application",
}


def _apply_pairs(pairs, dry_run: bool) -> Tuple[int, List[str]]:
    changed = 0
    misses: List[str] = []
    for rel, old, new in pairs:
        p = SUBSKILLS / rel
        if not p.exists():
            p = ROOT / rel
        if not p.exists():
            misses.append(f"{rel} 文件不存在")
            continue
        text = p.read_text(encoding="utf-8")
        if old not in text:
            if new in text:
                continue  # 已是目标状态，幂等
            misses.append(f"{rel}: {old[:40]}")
            continue
        if not dry_run:
            updated = text.replace(old, new)
            # 幂等保护：若 new 以 old 为前缀（即在原句后追加说明），
            # 且文件中已存在 new，说明此前已追加过，不再重复叠加。
            if new.startswith(old) and new != old and text.count(new) > 0:
                continue
            p.write_text(updated, encoding="utf-8")
        changed += 1
    return changed, misses


def _insert_currency_notice(dry_run: bool) -> int:
    changed = 0
    for sid in CURRENCY_NOTICE_SKILLS:
        p = SUBSKILLS / sid / "SKILL.md"
        text = p.read_text(encoding="utf-8")
        if "金额与政策时效" in text:
            continue
        m = re.search(r"^## ", text, re.M)
        if not m:
            continue
        if not dry_run:
            p.write_text(text[:m.start()] + CURRENCY_NOTICE + text[m.start():],
                         encoding="utf-8")
        changed += 1
    return changed


def _add_english_anchor(dry_run: bool) -> Tuple[int, List[str]]:
    """给 description 追加英文检索锚点。

    YAML 安全：含 `: ` 或 `#` 的描述一律用双引号包裹并转义，
    避免 `mapping values are not allowed here` 解析失败。
    """
    import yaml

    changed = 0
    problems: List[str] = []
    for md in sorted(SUBSKILLS.glob("*/SKILL.md")):
        sid = md.parent.name
        anchor = EN_ANCHOR.get(sid)
        if not anchor:
            problems.append(f"{sid}: 未定义英文锚点")
            continue
        text = md.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        if not m:
            problems.append(f"{sid}: 缺 frontmatter")
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as exc:
            problems.append(f"{sid}: frontmatter 解析失败 {exc}")
            continue
        desc = str(fm.get("description", "") or "")
        if anchor.lower() in desc.lower():
            continue
        # 超长描述先精简，再追加锚点
        base = DESC_COMPACT.get(sid, desc)
        new_desc = f"{base.rstrip('。')}。（EN: {anchor}）"
        if len(new_desc) > 300:
            problems.append(f"{sid}: 加锚点后 {len(new_desc)} 字符，超 300 上限")
            continue
        # YAML 安全序列化：需要引号时用双引号并转义
        if any(ch in new_desc for ch in (":", "#", '"', "'")) or new_desc != new_desc.strip():
            yaml_value = '"' + new_desc.replace("\\", "\\\\").replace('"', '\\"') + '"'
        else:
            yaml_value = new_desc
        new_fm = re.sub(r"^description:.*?(?=\nmetadata:|\Z)",
                        f"description: {yaml_value}", m.group(1),
                        count=1, flags=re.S | re.M)
        new_text = text[:m.start(1)] + new_fm + text[m.end(1):]
        # 校验重写后仍可解析
        try:
            yaml.safe_load(re.match(r"^---\n(.*?)\n---\n", new_text, re.S).group(1))
        except yaml.YAMLError as exc:
            problems.append(f"{sid}: 重写后 YAML 非法 {exc}")
            continue
        if not dry_run:
            md.write_text(new_text, encoding="utf-8")
        changed += 1
    return changed, problems


def _dedupe_triggers(dry_run: bool) -> int:
    changed = 0
    for md in sorted(SUBSKILLS.glob("*/SKILL.md")):
        text = md.read_text(encoding="utf-8")
        m = re.search(
            r"(^metadata:\s*\n(?:.*\n)*?\s*triggers:\s*\n)((?:[ \t]*-[ \t]*.+\n)+)",
            text, re.M)
        if not m:
            continue
        head, body = m.group(1), m.group(2)
        seen = set()
        out = []
        for line in body.splitlines(keepends=True):
            key = line.strip().lstrip("-").strip()
            if key in seen:
                continue
            seen.add(key)
            out.append(line)
        new_body = "".join(out)
        if new_body == body:
            continue
        if not dry_run:
            md.write_text(text[:m.start()] + head + new_body + text[m.end():],
                          encoding="utf-8")
        changed += 1
    return changed


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="重放 SKILL.md 审查修复")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-inject", action="store_true",
                    help="不调用 inject_writing_style.py")
    args = ap.parse_args(argv)

    total_misses: List[str] = []

    for label, pairs in (
        ("F1 领导人表述", LEADER_FIXES),
        ("F2 政策文号", DOCNO_FIXES),
        ("F3 无来源统计", STAT_FIXES),
        ("F4 公文误用", OFFICIAL_FIXES),
        ("F6 跨引用金额", MONEY_FIXES),
    ):
        n, misses = _apply_pairs(pairs, args.dry_run)
        print(f"{label}: 修改 {n} 处" + (f"，未命中 {len(misses)}" if misses else ""))
        total_misses.extend(misses)

    n = _insert_currency_notice(args.dry_run)
    print(f"F5 文档级时效声明: 新增 {n} 个 skill")

    n, probs = _add_english_anchor(args.dry_run)
    print(f"F7 英文检索锚点: 更新 {n} 个 description"
          + (f"，跳过 {len(probs)}" if probs else ""))
    total_misses.extend(probs)

    n = _dedupe_triggers(args.dry_run)
    print(f"F8 触发词去重: 修正 {n} 个 skill")

    if not args.skip_inject and not args.dry_run:
        rc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "inject_writing_style.py")],
            capture_output=True, text=True)
        tail = (rc.stdout or "").strip().splitlines()
        print(f"F9 写作规范+合规块: {tail[0] if tail else 'ok'}")
        if rc.returncode != 0:
            print(rc.stderr)

    if total_misses:
        print("\n⚠️  未命中（可能已修复或原文有变）：")
        for m in total_misses[:20]:
            print("   -", m)

    if args.dry_run:
        print("\n（--dry-run，未写盘）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
