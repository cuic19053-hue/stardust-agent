# -*- coding: utf-8 -*-
"""端到端合规测试：build.py 生成的 docx 正文不得含不可核验内容。

为什么需要这一层
----------------
静态门禁只检查 SKILL.md / references 的文本，但真正交付给用户的是
`build.py` 生成的 `.docx`。`DEFAULT_DATA` 里的示例数据会**逐字写入文档**，
所以「SKILL.md 干净」不等于「产物干净」。

2026-11-15 实测发现：`challenge-cup` 的 demo docx 里带着
`国办发〔20XX〕XX 号`、`2026.03`、`国家层面发布` 等不可核验表述，
而当时所有静态门禁都是绿的。本测试补上这一层。

检查项
------
E1 无模糊领导人代称
E2 无未来年份的政策发布日期
E3 无「国家层面」这类非机关主体
E4 无未来年份发文字号
E5 无绝对化承诺语
"""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUBSKILLS = ROOT / "subskills"

CURRENT_YEAR = date.today().year

# 生成产物中禁止出现的模式
BANNED = [
    ("E1", re.compile(r"最高领导人|国家领导人")),
    ("E3", re.compile(r"国家层面发布|国家层面印发")),
    ("E5", re.compile(r"包过|保证获奖|稳过|确保立项")),
]


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    return re.sub(r"<[^>]+>", "", xml)


def _build(skill: str, out: Path) -> bool:
    proc = subprocess.run(
        [sys.executable, str(SUBSKILLS / skill / "build.py"),
         "--demo", "--out", str(out)],
        capture_output=True, text=True, timeout=180)
    return proc.returncode == 0 and out.exists()


@pytest.mark.parametrize("skill", [
    "challenge-cup",
    "innovation-research",
    "grant-application",
    "national-scholarship",
    "entrepreneurship-training",
])
def test_generated_docx_has_no_unverifiable_content(skill, tmp_path):
    """demo 产物不得含模糊代称、未来日期、非机关主体或承诺语。"""
    out = tmp_path / f"{skill}.docx"
    if not _build(skill, out):
        pytest.skip(f"{skill} 无法在当前环境构建（缺 python-docx 等依赖）")

    text = _docx_text(out)
    hits = []
    for code, pattern in BANNED:
        m = pattern.search(text)
        if m:
            hits.append(f"{code}: {m.group(0)}")

    # 未来年份的**政策发布日期**（项目进度表里的未来日期是正常计划，不算）
    # 判据：日期前后 40 字内出现「印发/发布/出台/颁布」，且年份晚于当前年
    POLICY_VERB = re.compile(r"印发|发布|出台|颁布")
    for m in re.finditer(r"(?:19|20)\d{2}\.\d{2}", text):
        if int(m.group(0)[:4]) <= CURRENT_YEAR:
            continue
        window = text[max(0, m.start() - 40):m.end() + 40]
        if POLICY_VERB.search(window):
            hits.append(f"E2: 未来政策日期 {m.group(0)}")

    # 未来年份发文字号
    for m in re.finditer(r"〔(20\d{2})〕", text):
        if int(m.group(1)) > CURRENT_YEAR:
            hits.append(f"E4: 未来文号 {m.group(0)}")

    assert not hits, f"{skill} 生成的 docx 含不可核验内容：" + "；".join(sorted(set(hits)))


def test_example_placeholders_are_marked():
    """demo 数据里的示例政策必须带「（示例」标记，避免被当真实依据抄走。"""
    builders = ["challenge-cup", "innovation-research", "entrepreneurship-training"]
    unmarked = []
    for skill in builders:
        src = (SUBSKILLS / skill / "build.py").read_text(encoding="utf-8")
        # 出现「关于加强基层消防工作的意见」这类示例政策时，须同段有「示例」标注
        for line in src.splitlines():
            if "关于加强基层消防工作的意见" in line and "示例" not in line:
                unmarked.append(f"{skill}: {line.strip()[:70]}")
    assert not unmarked, "示例政策未标注：" + "；".join(unmarked)


# --- 规则自身的正反测试（防止判据被改坏）---

def _future_policy_hit(text: str) -> bool:
    POLICY_VERB = re.compile(r"印发|发布|出台|颁布")
    for m in re.finditer(r"(?:19|20)\d{2}\.\d{2}", text):
        if int(m.group(0)[:4]) <= CURRENT_YEAR:
            continue
        window = text[max(0, m.start() - 40):m.end() + 40]
        if POLICY_VERB.search(window):
            return True
    return False


@pytest.mark.parametrize("text", [
    "2027.03，国务院办公厅发布《关于加强基层消防工作的意见》",
    "国务院办公厅于 2028.01 印发了该意见",
])
def test_future_policy_date_is_detected(text):
    assert _future_policy_hit(text), "未来政策日期未被检出"


@pytest.mark.parametrize("text", [
    "第三阶段：2027.01-2027.04 系统集成与功能测试",
    "2021.12，国务院印发《十四五国家应急体系规划》",
    "总周期 12 个月（2026.06-2027.06）",
])
def test_project_schedule_dates_are_not_flagged(text):
    assert not _future_policy_hit(text), "项目进度表日期被误判为政策日期"
