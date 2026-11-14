# -*- coding: utf-8 -*-
"""合规门禁的测试：锁住 2026-11-15 全量审查发现的问题类型不再回归。

覆盖：领导人表述、未来文号、占位文号、无来源统计、绝对化承诺、
诚信底线、政策金额时效、公文适用范围误用。
"""

from __future__ import annotations

import sys
from pathlib import Path

import re

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import check_compliance as cc  # noqa: E402


def _codes(path: Path) -> set[str]:
    return {i.code for i in cc.check_file(path)}


def _write(tmp_path: Path, text: str, name: str = "SKILL.md") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# --- C1 领导人表述 ---

def test_banned_leader_term_is_error(tmp_path):
    p = _write(tmp_path, "本 skill 应包含最高领导人讲话（如适用）\n")
    issues = cc.check_file(p)
    assert any(i.code == "C1" and i.severity == "error" for i in issues)


def test_rule_self_reference_is_not_flagged(tmp_path):
    """「不使用「最高领导人」这类代称」是规则自述，不应报错。"""
    p = _write(tmp_path, "政治表述规范：不使用「最高领导人」这类模糊代称。\n")
    assert "C1" not in _codes(p)


def test_compliant_leader_term_passes(tmp_path):
    p = _write(tmp_path, "应包含习近平总书记重要讲话（如适用）。\n")
    assert "C1" not in _codes(p)


# --- C2 / C3 文号 ---

def test_future_doc_number_is_error(tmp_path):
    p = _write(tmp_path, "国办发〔2099〕12 号《关于加强基层消防工作的意见》\n")
    issues = cc.check_file(p)
    assert any(i.code == "C2" and i.severity == "error" for i in issues)


def test_placeholder_doc_number_without_hint_warns(tmp_path):
    p = _write(tmp_path, "依据国办发〔20XX〕XX 号文件执行。\n")
    issues = cc.check_file(p)
    assert any(i.code == "C3" for i in issues)


def test_placeholder_doc_number_with_hint_passes(tmp_path):
    p = _write(tmp_path, "国办发〔20XX〕XX 号是占位，须替换为真实文号。\n")
    assert "C3" not in _codes(p)


def test_real_doc_number_passes(tmp_path):
    p = _write(tmp_path, "《2030 年前碳达峰行动方案》（国发〔2021〕23 号）。\n")
    codes = _codes(p)
    assert "C2" not in codes and "C3" not in codes


# --- C4 无来源统计 ---

def test_unsourced_scale_claim_warns(tmp_path):
    p = _write(tmp_path, "截至 2024 年，全国累计已有 200 余万名大学生参军入伍。\n")
    issues = cc.check_file(p)
    assert any(i.code == "C4" for i in issues)


def test_sourced_scale_claim_passes(tmp_path):
    p = _write(tmp_path, "据团中央 2024 年 5 月公布，全国共有共青团员 7415.1 万名。\n")
    assert "C4" not in _codes(p)


def test_budget_numbers_are_not_scale_claims(tmp_path):
    """用户自有预算/经费不是对外部世界的规模断言，不应误报。"""
    p = _write(tmp_path, "| 经费 | 500~2000 元 | 5 千~2 万元 |\n")
    assert "C4" not in _codes(p)


# --- C5 绝对化承诺 ---

@pytest.mark.parametrize("phrase", ["包过", "保证获奖", "稳过", "确保立项"])
def test_promise_phrase_is_error(tmp_path, phrase):
    p = _write(tmp_path, f"本模板{phrase}，放心使用。\n")
    issues = cc.check_file(p)
    assert any(i.code == "C5" and i.severity == "error" for i in issues)


def test_normal_percentage_is_not_promise(tmp_path):
    p = _write(tmp_path, "| 合计正文 | 约 8000 字 | 100% |\n")
    assert "C5" not in _codes(p)


# --- C6 诚信底线 ---

def test_missing_integrity_clause_is_error(tmp_path):
    sub = tmp_path / "subskills" / "demo-skill"
    sub.mkdir(parents=True)
    p = sub / "SKILL.md"
    p.write_text("---\nname: demo-skill\ndescription: 演示\n---\n\n## 质检清单\n- 检查\n",
                 encoding="utf-8")
    issues = cc.check_file(p)
    assert any(i.code == "C6" and i.severity == "error" for i in issues)


def test_integrity_clause_passes(tmp_path):
    sub = tmp_path / "subskills" / "demo-skill"
    sub.mkdir(parents=True)
    p = sub / "SKILL.md"
    p.write_text(
        "---\nname: demo-skill\ndescription: 演示\n---\n\n"
        "## 合规底线\n不代写、不伪造，产出为草稿底稿。\n\n## 质检清单\n- 检查\n",
        encoding="utf-8")
    assert "C6" not in _codes(p)


# --- C7 政策金额时效 ---

def test_policy_amount_without_year_warns(tmp_path):
    p = _write(tmp_path, "国家奖学金奖励标准为每生每年 10000 元。\n")
    issues = cc.check_file(p)
    assert any(i.code == "C7" for i in issues)


def test_policy_amount_with_year_passes(tmp_path):
    p = _write(tmp_path, "自 2024 年起，国家奖学金标准提高到每生每年 10000 元。\n")
    assert "C7" not in _codes(p)


def test_document_level_notice_suppresses_later_warnings(tmp_path):
    p = _write(
        tmp_path,
        "> **金额与政策时效**：以你所在学校当年官方通知为准。\n\n"
        "国家奖学金奖励标准为每生每年 10000 元。\n",
    )
    assert "C7" not in _codes(p)


def test_user_budget_amount_is_not_policy_amount(tmp_path):
    p = _write(tmp_path, "团队暂领基本生活费 3000 元/月。\n")
    assert "C7" not in _codes(p)


# --- C8 公文适用范围误用 ---

def test_official_doc_as_format_basis_is_error(tmp_path):
    p = _write(tmp_path, "| 《党政机关公文处理工作条例》 | 中共中央、国务院 | 2012 年 | 申请书格式参考 |\n")
    issues = cc.check_file(p)
    assert any(i.code == "C8" and i.severity == "error" for i in issues)


def test_official_doc_with_disclaimer_passes(tmp_path):
    p = _write(tmp_path, "《党政机关公文处理工作条例》：仅参考行文严谨性，不适用公文格式。\n")
    assert "C8" not in _codes(p)


# --- 代码块与 frontmatter 不参与自然语言检查 ---

def test_code_block_is_skipped(tmp_path):
    p = _write(tmp_path, "```python\nprint('最高领导人')\n```\n")
    assert "C1" not in _codes(p)


def test_frontmatter_triggers_are_skipped(tmp_path):
    p = _write(
        tmp_path,
        "---\nname: demo\ndescription: 演示\nmetadata:\n  triggers:\n  - 10000元奖学金\n---\n\n正文。\n",
    )
    assert "C7" not in _codes(p)


# --- 全仓门禁必须通过 ---

def test_repository_passes_compliance_gate():
    """仓库当前状态必须 0 错误 0 警告，防止新增内容引入合规问题。"""
    issues = []
    for f in cc._iter_files():
        issues.extend(cc.check_file(f))
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    detail = "；".join(f"{i.file}:{i.line} {i.code}" for i in issues[:10])
    assert not errors, f"合规错误 {len(errors)} 项：{detail}"
    assert not warnings, f"合规警告 {len(warnings)} 项：{detail}"


# --- 修复脚本的幂等性 ---

def test_apply_skill_fixes_is_idempotent():
    """重复运行修复脚本不得产生任何改动（防止时效后缀被叠加多次）。"""
    import subprocess

    script = ROOT / "scripts" / "apply_skill_fixes.py"
    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT,
        capture_output=True, text=True).stdout
    proc = subprocess.run(
        [sys.executable, str(script), "--skip-inject"],
        capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT,
        capture_output=True, text=True).stdout
    assert before == after, "apply_skill_fixes.py 非幂等，工作树被重复修改"


def test_no_duplicated_currency_suffix():
    """时效后缀不得重复叠加（历史上出现过「（以当年通知为准）（以当年通知为准）」）。"""
    dup_patterns = [
        "（以当年通知为准）（以当年通知为准）",
        "（现行标准，须以你入学/入伍当年财政部、教育部、中央军委相关通知为准）"
        "（现行标准，须以你入学/入伍当年财政部、教育部、中央军委相关通知为准）",
    ]
    offenders = []
    for md in sorted((ROOT / "subskills").glob("*/SKILL.md")):
        text = md.read_text(encoding="utf-8")
        for pat in dup_patterns:
            if pat in text:
                offenders.append(md.parent.name)
    assert not offenders, f"时效后缀重复叠加：{offenders}"


# --- 核心章节命名（I7）---

def test_core_sections_present_in_all_skills():
    """35 个 skill 必须含 4 个核心 H2，允许业务括注。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_skill_integrity import CORE_H2, H2_RE

    missing = []
    for md in sorted((ROOT / "subskills").glob("*/SKILL.md")):
        titles = [t.strip() for t in H2_RE.findall(md.read_text(encoding="utf-8"))]
        for core in CORE_H2:
            if not any(core in t for t in titles):
                missing.append(f"{md.parent.name}: {core}")
    assert not missing, "缺少核心章节：" + "；".join(missing)


def test_business_section_names_keep_core_word_first():
    """业务化命名须保留核心词（如「工作流程总览：5 段结构」而非「5 段结构」）。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_skill_integrity import H2_RE

    bad = []
    for md in sorted((ROOT / "subskills").glob("*/SKILL.md")):
        for title in H2_RE.findall(md.read_text(encoding="utf-8")):
            # 「§N 5 段结构」「N 段结构」这类纯业务命名应已改为「工作流程总览：…」
            if re.match(r"^§?\d*\s*\d+\s*段结构", title.strip()):
                bad.append(f"{md.parent.name}: {title.strip()}")
    assert not bad, "业务化章节名未前置核心词：" + "；".join(bad)


# --- 根级 references 不得闲置（I8）---

def test_root_references_are_all_indexed():
    """根目录 references/*.md 必须至少被 1 个 skill 索引，否则是死知识。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_skill_integrity import check_root_references

    problems = check_root_references()
    assert not problems, "；".join(p.detail for p in problems)


def test_root_references_idle_detection_works(tmp_path, monkeypatch):
    """反向测试：新增一个无人引用的根级 reference，I8 必须报出来。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    import verify_skill_integrity as v

    probe = v.ROOT / "references" / "_probe_idle_test.md"
    probe.write_text("# probe\n", encoding="utf-8")
    try:
        problems = v.check_root_references()
        assert any("_probe_idle_test.md" in p.detail for p in problems)
    finally:
        probe.unlink()


def test_all_reference_links_resolve():
    """所有 references 指针（含 ../../references/ 根级）必须可达。"""
    bad = []
    for md in sorted((ROOT / "subskills").glob("*/SKILL.md")):
        text = md.read_text(encoding="utf-8")
        for rel in re.findall(r"\]\((\.\./\.\./references/[a-z_]+\.md)\)", text):
            if not (md.parent / rel).resolve().exists():
                bad.append(f"{md.parent.name}: {rel}")
        for rel in re.findall(r"\]\(references/([a-z0-9_.-]+\.md)\)", text):
            if not (md.parent / "references" / rel).exists():
                bad.append(f"{md.parent.name}: references/{rel}")
    assert not bad, "断链：" + "；".join(bad[:10])


# --- 英文检索锚点（分层语言策略的可验证部分）---

EN_ANCHOR_QUERIES = {
    "challenge cup": "challenge-cup",
    "national scholarship": "national-scholarship",
    "csc government scholarship": "csc-scholarship",
    "innovation research proposal": "innovation-research",
    "graduate recommendation personal statement": "graduate-recommendation",
    "china international college students innovation competition": "internet-plus",
    "volunteer teaching": "volunteer-teaching",
    "military enlistment": "military-enlistment",
    "major transfer": "major-transfer",
    "red tour": "internet-plus-red-tour",
    "youth league": "youth-league-application",
    "western plan": "western-plan",
}


def _descriptions() -> dict:
    import yaml

    out = {}
    for md in sorted((ROOT / "subskills").glob("*/SKILL.md")):
        text = md.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        fm = yaml.safe_load(m.group(1)) or {}
        out[md.parent.name] = str(fm.get("description", "") or "")
    return out


def test_every_description_has_english_anchor():
    """每条 description 末尾必须有 `（EN: ...）` 锚点，补齐英文 query 召回。"""
    missing = [sid for sid, d in _descriptions().items() if "（EN: " not in d]
    assert not missing, f"缺少英文锚点：{missing}"


def test_english_anchor_queries_hit_expected_skill():
    """英文核心词必须能字面命中对应 skill（英文用户的可发现性）。"""
    descs = _descriptions()
    misses = []
    for query, expected in EN_ANCHOR_QUERIES.items():
        hits = [sid for sid, d in descs.items() if query in d.lower()]
        if expected not in hits:
            misses.append(f"{query!r} → 期望 {expected}，实际 {hits[:3]}")
    assert not misses, "英文锚点召回失败：" + "；".join(misses)


def test_anchor_stays_within_length_budget():
    """加锚点后 description 仍须 ≤300 字符。"""
    over = [(sid, len(d)) for sid, d in _descriptions().items() if len(d) > 300]
    assert not over, f"超 300 字符：{over}"


# --- 触发词质量 ---

def _triggers() -> dict:
    import yaml

    out = {}
    for md in sorted((ROOT / "subskills").glob("*/SKILL.md")):
        text = md.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        fm = yaml.safe_load(m.group(1)) or {}
        meta = fm.get("metadata") or {}
        out[md.parent.name] = list(meta.get("triggers") or [])
    return out


def test_each_skill_has_unique_triggers():
    """每个 skill 至少 3 个不被其他 skill 共用的触发词，保证可区分。

    共享的上位词（三下乡/大创/立项申报书）是用户真实会输入的分类词，
    保留它们以维持类目召回；只要独有触发词足够，就不会互相抢占。
    """
    import collections

    trig = _triggers()
    owners = collections.defaultdict(list)
    for sid, ts in trig.items():
        for t in ts:
            owners[t].append(sid)
    weak = []
    for sid, ts in trig.items():
        uniq = [t for t in ts if len(owners[t]) == 1]
        if len(uniq) < 3:
            weak.append(f"{sid}(独有 {len(uniq)}/{len(ts)})")
    assert not weak, "独有触发词不足 3 个：" + "；".join(weak)


def test_no_duplicate_triggers_within_skill():
    """同一 skill 内不得有重复触发词。"""
    import collections

    dups = []
    for sid, ts in _triggers().items():
        rep = [k for k, v in collections.Counter(ts).items() if v > 1]
        if rep:
            dups.append(f"{sid}: {rep}")
    assert not dups, "skill 内重复触发词：" + "；".join(dups)


def test_triggers_minimum_count():
    """每个 skill 至少 3 个触发词（官方建议）。"""
    few = [(sid, len(ts)) for sid, ts in _triggers().items() if len(ts) < 3]
    assert not few, f"触发词过少：{few}"


# --- version.json 元数据不得静默漂移 ---

def test_version_json_metadata_matches_actual():
    """version.json 的 total_lines / 分类行数必须与实际一致。"""
    import json

    ver = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
    dirs = sorted(p.name for p in (ROOT / "subskills").iterdir() if p.is_dir())
    actual = sum(
        len((ROOT / "subskills" / n / "SKILL.md").read_text(encoding="utf-8").splitlines())
        for n in dirs)
    assert ver.get("total_lines") == actual, (
        f"total_lines={ver.get('total_lines')}，实际 {actual}；"
        "运行 python3 scripts/sync_version_meta.py")
    assert ver.get("total_skills") == len(dirs)
    assert sorted(ver.get("skills") or []) == dirs


def test_sync_version_meta_is_idempotent():
    """同步脚本重复运行不应产生改动。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    from sync_version_meta import sync

    changes, _ = sync(dry_run=True)
    assert not changes, "version.json 元数据漂移：" + "；".join(changes)


# --- 已核实政策文号不得被改回错误值 ---

# 2026-11-15 通过中国政府网 / 教育部官网逐条核实
VERIFIED_DOC_NUMBERS = {
    "《\"十四五\"国家应急体系规划》": "国发〔2021〕36 号",
    "《中国制造 2025》": "国发〔2015〕28 号",
    "《2030 年前碳达峰行动方案》": "国发〔2021〕23 号",
    "《新一代人工智能发展规划》": "国发〔2017〕35 号",
    "《\"十四五\"数字经济发展规划》": "国发〔2021〕29 号",
    "《关于进一步支持大学生创新创业的指导意见》": "国办发〔2021〕35 号",
    "《国家级大学生创新创业训练计划管理办法》": "教高函〔2019〕13 号",
    "《国家公派出国留学研究生管理规定（试行）》": "教外留〔2007〕46 号",
    "《\"十四五\"推进农业农村现代化规划》": "国发〔2021〕25 号",
    "《\"十四五\"医药工业发展规划》": "工信部联规〔2021〕217 号",
    "《新能源汽车产业发展规划（2021—2035 年）》": "国办发〔2020〕39 号",
}

# 已确认的错误值，出现即失败
KNOWN_WRONG_NUMBERS = {
    "国发〔2021〕109 号": "应急体系规划应为 36 号",
    "国发〔2025〕XX 号": "中国制造 2025 应为 2015 年 28 号",
    "国办发〔2026〕12 号": "未来年份虚构文号",
    "国办发〔2026〕8 号": "未来年份虚构文号",
    "留金发〔2007〕3010 号": "公派研究生管理规定应为教外留〔2007〕46 号",
    "工信部联规〔2021〕208 号": "医药工业发展规划应为 217 号",
    "国发〔2022〕6 号": "农业农村现代化规划应为 2021 年 25 号",
    "教高〔2024〕3 号": "国创计划管理办法应为教高函〔2019〕13 号",
}


def _all_skill_text() -> str:
    """所有面向用户/维护者的内容：skill 正文、references、根级参考、docs。"""
    parts = []
    for p in sorted((ROOT / "subskills").glob("*/SKILL.md")):
        parts.append(p.read_text(encoding="utf-8"))
    for p in sorted((ROOT / "subskills").glob("*/references/*.md")):
        parts.append(p.read_text(encoding="utf-8"))
    for p in sorted((ROOT / "references").glob("*.md")):
        parts.append(p.read_text(encoding="utf-8"))
    for p in sorted((ROOT / "docs").rglob("*.md")):
        parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


# 允许提及错误文号的上下文：修订记录 / 测试 / 待办清单
WRONG_NUMBER_ALLOWLIST = ("ISSUES.md", "DELIVERY_", "VERSION_DIFF_", "SKILL_LANGUAGE")


def test_verified_doc_numbers_stay_correct():
    """已核实的政策文号必须保持正确（防止被改回错误值）。"""
    text = _all_skill_text()
    wrong = []
    for title, doc_no in VERIFIED_DOC_NUMBERS.items():
        if title in text and doc_no not in text:
            wrong.append(f"{title} 缺少正确文号 {doc_no}")
    assert not wrong, "；".join(wrong)


def test_known_wrong_doc_numbers_absent():
    """已确认的错误文号不得再次出现在「会被人当作正确知识读」的文件里。

    修订记录、交付报告、本测试文件本身需要引用错误值做对照，按文件名排除。
    """
    offenders = []
    for path in sorted(list((ROOT / "subskills").rglob("*.md"))
                       + list((ROOT / "references").glob("*.md"))
                       + list((ROOT / "docs").rglob("*.md"))):
        if any(a in path.name for a in WRONG_NUMBER_ALLOWLIST):
            continue
        text = path.read_text(encoding="utf-8")
        for bad, why in KNOWN_WRONG_NUMBERS.items():
            if bad in text:
                offenders.append(f"{path.name}: {bad}（{why}）")
    assert not offenders, "出现已知错误文号：" + "；".join(offenders)


def test_verified_doc_numbers_documented():
    """已核实文号清单必须写入合规文档，供贡献者查阅。"""
    doc = (ROOT / "docs" / "design" / "SKILL_LANGUAGE_AND_COMPLIANCE.md").read_text(
        encoding="utf-8")
    missing = [d for d in VERIFIED_DOC_NUMBERS.values() if d not in doc]
    assert not missing, f"合规文档缺少已核实文号：{missing}"


# --- 奖助金额不得串位（国奖 10000 / 励志 6000）---

AMOUNT_RULES = {
    "国家奖学金": {"10000"},
    "国家励志奖学金": {"6000"},
}


def test_scholarship_amounts_not_crossed():
    """同一句内，国家奖学金只能配 10000，国家励志奖学金只能配 6000。

    两个奖项常在同一句并列出现（「国家奖学金（10000 元）与国家励志奖学金（6000 元）」），
    所以按句子切分后，含两个奖项的并列句跳过，只校验单奖项句。
    """
    import re as _re

    wrong = []
    for md in sorted((ROOT / "subskills").glob("*/SKILL.md")):
        text = md.read_text(encoding="utf-8")
        for sent in _re.split(r"[。；\n]", text):
            has_nat = "国家奖学金" in sent
            has_mot = "国家励志奖学金" in sent
            if has_nat and has_mot:
                continue
            for name, allowed in AMOUNT_RULES.items():
                if name not in sent:
                    continue
                # 跳过「非本 skill 处理」的指引句（那是分流说明，不陈述标准）
                m = _re.search(r"(\d{4,5})\s*元", sent)
                if m and m.group(1) not in allowed:
                    wrong.append(f"{md.parent.name}: {name} 配 {m.group(1)} 元")
    assert not wrong, "金额串位：" + "；".join(wrong)


def test_cross_skill_doc_number_consistency():
    """同一文件名在不同 skill 中不得对应不同文号。"""
    import collections
    import re as _re

    pairs = collections.defaultdict(set)
    pat = _re.compile(
        r"《([^》]{4,60})》[^。；\n]{0,12}?([\u4e00-\u9fa5]{2,8}〔20\d{2}〕\d+\s*号)")
    for md in sorted((ROOT / "subskills").glob("*/SKILL.md")):
        for m in pat.finditer(md.read_text(encoding="utf-8")):
            pairs[m.group(1)].add(m.group(2))
    conflicts = {k: v for k, v in pairs.items() if len(v) > 1}
    assert not conflicts, f"跨 skill 文号冲突：{conflicts}"


# --- description 锚点唯一性（历史上出现过叠加两个 EN 锚点）---

def test_exactly_one_english_anchor_per_description():
    """每条 description 有且仅有一个英文锚点，不得叠加。"""
    import yaml

    bad = []
    for md in sorted((ROOT / "subskills").glob("*/SKILL.md")):
        text = md.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        fm = yaml.safe_load(m.group(1)) or {}
        desc = str(fm.get("description", "") or "")
        n = desc.count("（EN: ")
        if n != 1:
            bad.append(f"{md.parent.name}: {n} 个锚点")
    assert not bad, "锚点数量异常：" + "；".join(bad)


def test_description_has_no_double_period():
    """description 不得出现「。 。」这类标点冗余（重复替换的痕迹）。"""
    import yaml

    bad = []
    for md in sorted((ROOT / "subskills").glob("*/SKILL.md")):
        text = md.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        fm = yaml.safe_load(m.group(1)) or {}
        desc = str(fm.get("description", "") or "")
        if "。 。" in desc or "。。" in desc:
            bad.append(md.parent.name)
    assert not bad, f"description 标点冗余：{bad}"


# --- 每个 skill 都要说明与相邻 skill 的区别，防止误路由 ---

def test_each_skill_states_disambiguation():
    """35 个 skill 必须说明「不适用/区别/请走其他」，避免路由歧义。"""
    missing = []
    for md in sorted((ROOT / "subskills").glob("*/SKILL.md")):
        text = md.read_text(encoding="utf-8")
        if not re.search(r"不适用|区别|区分|走其他|请走|不触发", text):
            missing.append(md.parent.name)
    assert not missing, f"缺少区分说明：{missing}"
