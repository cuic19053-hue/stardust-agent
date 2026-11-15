#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_compliance.py — skills 内容的国家规范与合规门禁。

为什么需要
----------
35 个 SKILL.md 与 100+ 个 references 文件由多人多轮迭代，内容层面
（政治表述、政策文号、统计数据、个人信息、适用范围）缺少自动检查。
2026-11-15 的一次全量人工审查发现：模糊代称 15 处、不可核验的未来
文号若干、无出处统计 5 处、公文适用范围误用 4 处，全部靠人眼才发现。

本脚本把这些判据固化成 CI 可跑的规则，避免同类问题回归。

检查项
------
C1 领导人表述   禁止「最高领导人」「国家领导人」等模糊代称，须用规范全称
C2 未来文号     禁止出现晚于当前年份的发文字号（如 2026 年写〔2027〕X 号）
C3 占位文号     形如 〔20XX〕XX 号 的占位必须同段出现「占位/替换/待核」说明
C4 无来源统计   数字 + 万/亿 的规模断言必须同段带出处或「以…公布为准」
C5 绝对化承诺   禁止「包过」「保证获奖」「稳过」「确保立项」等承诺性表述
C6 诚信底线     每个 SKILL.md 必须含「不代写」边界声明
C7 时效标注     出现具体金额（元/美元）的行须带「以…为准」或「现行标准」
C8 公文误用     不得以《党政机关公文处理工作条例》作为申请书格式依据
C9 无依据门槛   不得把无官方出处的数值写成「硬规则」（如「英文文献 ≥60%」，
                与高校形式审查「英文不超过 50%」相反）
C10 已核实文号  已核对的文号不得被改回错误值，已知错误值不得再次出现

用法
----
    python3 scripts/check_compliance.py
    python3 scripts/check_compliance.py --json
    python3 scripts/check_compliance.py --strict     # 警告也算失败
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
SUBSKILLS = ROOT / "subskills"
SCOPE_FILES = [ROOT / "SKILL.md"] + sorted(ROOT.glob("references/*.md"))

CURRENT_YEAR = date.today().year

# C1 模糊代称
BANNED_LEADER_TERMS = ["最高领导人", "国家领导人"]

# C2/C3 文号
DOC_NO_RE = re.compile(r"〔(\d{4}|20XX|20xx|XXXX)〕\s*([0-9Xx]+)?\s*号")
# 占位说明：必须是文号之外的说明词，文号自身的 "XX" 不算
PLACEHOLDER_HINT = ("占位", "替换", "待核", "示例", "须核对", "以真实")

# C4 规模统计：只在「描述外部世界」的断言里检查，预算/经费/融资属用户自有数字
SCALE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:余|多|约)?\s*[万亿]")
SCALE_SUBJECT = ("全国", "累计", "总量", "总规模", "市场规模", "占比", "人数", "人口",
                 "装机", "用户数", "产值", "缺口")
SCALE_EXEMPT = ("元", "预算", "经费", "融资", "估值", "字", "示例", "例如", "如「", "如\"")
SOURCE_HINT = ("据", "来源", "公布", "发布", "为准", "通报", "统计", "年鉴", "白皮书")

# C5 绝对化承诺
PROMISE_RE = re.compile(
    r"包过|保证获奖|必获奖|稳过|稳拿|确保立项|必定获奖|100\s*%\s*通过|一定能(?:获奖|立项)"
)

# C7 政策金额：只查奖助/补贴/补偿等政策口径，用户自填预算不在此列
MONEY_RE = re.compile(r"\d[\d,.]*\s*(?:元|美元|英镑|欧元)")
MONEY_SUBJECT = ("奖助", "助学金", "奖学金", "补贴", "补偿", "资助", "津贴",
                 "安置费", "生活费", "优待金")
# 明确属于用户自有支出的语境，不按政策口径要求时效标注
MONEY_OWN_CONTEXT = ("团队", "本人暂领", "自筹", "预算", "支出", "花费", "自付")
MONEY_HINT = ("以当年", "以你所在", "现行标准", "为准", "待核对", "按当年", "以…为准")
# 同行已给出文号或明确年份，视为已溯源，不再要求重复标注时效
MONEY_SOURCED_RE = re.compile(r"〔\d{4}〕|(?:19|20)\d{2}\s*年")

# C8 公文误用
OFFICIAL_DOC = "《党政机关公文处理工作条例》"

# C9 无依据门槛：这些数值曾被当作「硬规则」，但无官方出处，且部分与
# 高校形式审查标准方向相反。出现在否定语境（反模式/早期版本/不要套用）时放行。
UNGROUNDED_RE = re.compile(
    r"英文\s*[≥>＞]\s*[56]0\s*%"
    r"|(?:文献|参考文献)\s*[≥>＞]\s*30\s*篇"
    r"|(?:≥|不少于)\s*1\.5\s*万字"
    r"|1\.5\s*万字\s*/\s*2[04]\s*个月"
    # 社会经济效益指标数量：无官方依据，且对人文社科选题不成立
    r"|(?:经济效益|社会经济效益)[^\n]{0,12}[≥>＞]\s*1[02]\s*项"
    r"|(?:指标数|指标数量)\s*[≥>＞]\s*1[02]\s*项"
    # 政策引用条数：没有任何高校评分表设有该评分项
    r"|政策(?:引用|数)?[^\n]{0,8}[≥>＞]\s*(?:[89]|1[02])\s*(?:条|项)"
)
UNGROUNDED_EXEMPT = (
    "反模式", "早期版本", "不要套用", "无官方依据", "无依据",
    "不成立", "相反", "曾在本 skill", "早期", "冲突", "不设",
    "UNGROUNDED", "C9",
)

# C10 已核实 / 已知错误文号（与 tests/test_compliance.py 保持同源）
VERIFIED_DOC_NUMBERS = {
    "「十四五」国家应急体系规划": "国发〔2021〕36 号",
    "中国制造 2025": "国发〔2015〕28 号",
    "2030 年前碳达峰行动方案": "国发〔2021〕23 号",
    "新一代人工智能发展规划": "国发〔2017〕35 号",
    "「十四五」数字经济发展规划": "国发〔2021〕29 号",
    "关于进一步支持大学生创新创业的指导意见": "国办发〔2021〕35 号",
    "国家级大学生创新创业训练计划管理办法": "教高函〔2019〕13 号",
    "国家公派出国留学研究生管理规定": "教外留〔2007〕46 号",
    "「十四五」推进农业农村现代化规划": "国发〔2021〕25 号",
    "「十四五」医药工业发展规划": "工信部联规〔2021〕217 号",
    "新能源汽车产业发展规划": "国办发〔2020〕39 号",
    "「十四五」机器人产业发展规划": "工信部联规〔2021〕179 号",
    "安全应急装备产业分类指导目录": "工信厅安全函〔2025〕283 号",
    "通用航空装备创新应用实施方案": "工信部联重装〔2024〕52 号",
    "关于加快应急机器人发展的指导意见": "应急〔2023〕148 号",
}
KNOWN_WRONG_DOC_NUMBERS = {
    "国发〔2021〕109 号": "应急体系规划应为 36 号",
    "留金发〔2007〕3010 号": "公派研究生管理规定应为教外留〔2007〕46 号",
    "工信部联规〔2021〕208 号": "医药工业发展规划应为 217 号",
    "国发〔2022〕6 号": "农业农村现代化规划应为 2021 年 25 号",
    "教高〔2024〕3 号": "国创计划管理办法应为教高函〔2019〕13 号",
    "工信部联重装〔2024〕12 号": "通用航空装备方案应为 52 号",
    "应急〔2023〕116 号": "应急机器人指导意见应为 148 号",
    "工信部联装〔2025〕198 号": "安全应急装备目录应为工信厅安全函〔2025〕283 号",
    "中办发〔2024〕23 号": "韧性城市意见公开文本无文号，属编造",
    "应急〔2022〕15 号": "应急救援力量建设规划公开文本无文号，属编造",
}


@dataclass
class Issue:
    file: str
    line: int
    code: str
    detail: str
    severity: str  # error | warning


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def _iter_files() -> List[Path]:
    files = list(SCOPE_FILES)
    for md in sorted(SUBSKILLS.glob("*/SKILL.md")):
        files.append(md)
    for ref in sorted(SUBSKILLS.glob("*/references/*.md")):
        files.append(ref)
    return [f for f in files if f.is_file()]


def _in_code_block(lines: List[str]) -> List[bool]:
    """标记每行是否位于 ``` 代码块内（代码块里的示例不参与自然语言检查）。"""
    flags: List[bool] = []
    inside = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            flags.append(True)
            inside = not inside
            continue
        flags.append(inside)
    return flags


def _frontmatter_end(lines: List[str]) -> int:
    """返回 YAML 前置块结束行号（0-based，指向第二个 --- 的下一行）。无前置块返回 0。"""
    if not lines or lines[0].strip() != "---":
        return 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return i + 1
    return 0


def check_file(path: Path) -> List[Issue]:
    issues: List[Issue] = []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_code = _in_code_block(lines)
    fm_end = _frontmatter_end(lines)
    rel = _rel(path)

    # 文档级时效声明：出现后，其后的金额不再逐行要求重复标注
    doc_notice_at = next(
        (i for i, ln in enumerate(lines, start=1) if "金额与政策时效" in ln), None)

    # C6 诚信底线（只对 SKILL.md）
    if path.name == "SKILL.md" and path.parent.parent.name == "subskills":
        if "不代写" not in text:
            issues.append(Issue(rel, 0, "C6", "缺少「不代写」边界声明", "error"))

    for i, line in enumerate(lines, start=1):
        # 代码块与 YAML 前置块（triggers 关键词表）不参与自然语言合规检查
        if in_code[i - 1] or i <= fm_end:
            continue

        # C1（「不使用「最高领导人」这类代称」属于规则自述，不算违规）
        if "不使用" not in line and "禁止" not in line and "改为" not in line:
            for term in BANNED_LEADER_TERMS:
                if term in line:
                    issues.append(Issue(
                        rel, i, "C1",
                        f"使用模糊代称「{term}」，应改为规范全称（如「习近平总书记重要讲话」）",
                        "error"))
                    break

        # C2 / C3 文号
        for m in DOC_NO_RE.finditer(line):
            year_part, num_part = m.group(1), m.group(2) or ""
            if year_part.isdigit():
                y = int(year_part)
                if y > CURRENT_YEAR:
                    issues.append(Issue(
                        rel, i, "C2",
                        f"发文字号年份 {y} 晚于当前年份 {CURRENT_YEAR}，属不可核验文号",
                        "error"))
            else:
                if not any(h in line for h in PLACEHOLDER_HINT):
                    issues.append(Issue(
                        rel, i, "C3",
                        "占位文号未在同段注明「占位/替换/待核」",
                        "warning"))

        # C4 无来源统计（只查对外部世界的规模断言）
        if (SCALE_RE.search(line)
                and any(s in line for s in SCALE_SUBJECT)
                and not any(e in line for e in SCALE_EXEMPT)
                and not any(h in line for h in SOURCE_HINT)):
            issues.append(Issue(
                rel, i, "C4",
                "出现万/亿级规模断言但同段无出处或「以…为准」说明",
                "warning"))

        # C5 绝对化承诺
        m = PROMISE_RE.search(line)
        if m:
            issues.append(Issue(
                rel, i, "C5",
                f"出现承诺性表述「{m.group(0)}」",
                "error"))

        # C7 政策金额时效（只查奖助/补贴类政策口径）
        if (MONEY_RE.search(line)
                and any(s in line for s in MONEY_SUBJECT)
                and not any(c in line for c in MONEY_OWN_CONTEXT)
                and not any(h in line for h in MONEY_HINT)
                and not MONEY_SOURCED_RE.search(line)
                and not (doc_notice_at is not None and i > doc_notice_at)):
            issues.append(Issue(
                rel, i, "C7",
                "出现政策类具体金额但未标注「以当年通知为准」类时效说明",
                "warning"))

        # C8 公文误用
        if OFFICIAL_DOC in line:
            if "不适用" not in line and "仅参考" not in line and "非公文" not in line:
                issues.append(Issue(
                    rel, i, "C8",
                    "以《党政机关公文处理工作条例》作为申请书格式依据（适用范围误用）",
                    "error"))

        # C9 无依据门槛：把无官方出处的数值表述成硬规则。
        # 允许出现在「反模式/早期版本/不要套用」等否定语境中。
        if not any(x in line for x in UNGROUNDED_EXEMPT):
            m = UNGROUNDED_RE.search(line)
            if m:
                issues.append(Issue(
                    rel, i, "C9",
                    f"「{m.group(0)}」无官方依据却被当作门槛；"
                    "常见高校形式审查为「英文不超过 50%」等相反口径，"
                    "详见 references/thresholds-and-sources.md",
                    "error"))

    # C10 已核实文号（整份文本级检查）
    # 只判「写错了」，不判「没写」：很多 skill 只提文件名不标文号，属正常写法；
    # 只有该文件已经给出文号、却给了错值时才是错误。
    for bad, why in KNOWN_WRONG_DOC_NUMBERS.items():
        if bad in text:
            issues.append(Issue(
                rel, 0, "C10",
                f"出现已知错误文号 {bad}（{why}）",
                "error"))
    for title, doc_no in VERIFIED_DOC_NUMBERS.items():
        if title not in text or doc_no in text:
            continue
        # 同一文件里给这个标题配了别的文号 → 文号写错
        pat = re.compile(
            re.escape(title) + r"[^\n]{0,40}?〔\d{4}〕\s*\d+\s*号"
            r"|〔\d{4}〕\s*\d+\s*号[^\n]{0,40}?" + re.escape(title))
        m = pat.search(text)
        if m:
            issues.append(Issue(
                rel, 0, "C10",
                f"《{title}》文号应为 {doc_no}，实际写作「{m.group(0)[:40]}」",
                "error"))

    return issues


LABELS = {
    "C1": "领导人表述",
    "C2": "未来文号",
    "C3": "占位文号",
    "C4": "无来源统计",
    "C5": "绝对化承诺",
    "C6": "诚信底线",
    "C7": "时效标注",
    "C8": "公文误用",
    "C9": "无依据门槛",
    "C10": "已核实文号",
}


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="skills 国家规范与合规门禁")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="警告也算失败")
    args = ap.parse_args(argv)

    files = _iter_files()
    if not files:
        print("❌ 未找到待检查文件")
        return 2

    issues: List[Issue] = []
    for f in files:
        issues.extend(check_file(f))

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]

    if args.json:
        print(json.dumps({
            "passed": not errors and (not args.strict or not warnings),
            "file_count": len(files),
            "error_count": len(errors),
            "warning_count": len(warnings),
            "issues": [asdict(i) for i in issues],
        }, ensure_ascii=False, indent=2))
        return 0 if (not errors and (not args.strict or not warnings)) else 1

    print(f"合规检查：{len(files)} 个文件，{len(errors)} 项错误，{len(warnings)} 项警告\n")

    by_code: Dict[str, List[Issue]] = {}
    for i in issues:
        by_code.setdefault(i.code, []).append(i)

    for code in sorted(by_code):
        bucket = by_code[code]
        errs = sum(1 for i in bucket if i.severity == "error")
        mark = "❌" if errs else "⚠️ "
        print(f"{mark} {code} {LABELS[code]}（{len(bucket)} 处）")
        for i in bucket[:12]:
            loc = f"{i.file}:{i.line}" if i.line else i.file
            print(f"     {loc} — {i.detail}")
        if len(bucket) > 12:
            print(f"     … 另有 {len(bucket) - 12} 处")
        print()

    failed = bool(errors) or (args.strict and bool(warnings))
    if failed:
        print("❌ 合规门禁未通过")
        return 1
    print("✅ 合规门禁通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
