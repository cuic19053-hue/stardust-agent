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
