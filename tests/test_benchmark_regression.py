# -*- coding: utf-8 -*-
"""对标真实评审依据后的回归测试。

为什么需要这一层
----------------
2026-11 做了一次「真实案例对标」审计：收集真实申报书、高校形式审查标准与
评分表，再让 skill 实际生成一份稿子对照。结论是 skill 里有一批「硬规则」
**没有官方出处，其中两条与官方口径方向相反**：

  - 「英文文献 ≥60%」←→ 多校形式审查要求「英文不超过 50%」
  - 「国家级经费 1~2 万」←→ 教高函〔2019〕13 号要求「平均不低于 2 万元/项」

同时发现 DEFAULT_DATA 的 10 条政策引用里有 5 条文号是编造的，以及
build.py 把「消防无人机」主题硬编码进渲染方法，导致任何非该主题的稿子
都会被写入不相干的消防措辞。

本文件把这些发现固化成测试，防止回归。依据清单见
`subskills/innovation-research/references/thresholds-and-sources.md`。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUBSKILL = ROOT / "subskills" / "innovation-research"
BUILD = SUBSKILL / "build.py"

sys.path.insert(0, str(SUBSKILL))

# 官方口径（教高函〔2019〕13 号第九条（六））
NATIONAL_FUNDING_FLOOR = 20000

# 形式审查常见口径（山东理工《大学生创新训练项目申报书形式审查标准》）
RECENT_LIT_RATIO = 0.80
EN_LIT_RATIO_MAX = 0.50


def _load_default_data():
    """导入 build.py 的 DEFAULT_DATA（不触发 argparse）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_ir_build", BUILD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.DEFAULT_DATA


def _cn_len(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    return re.sub(r"<[^>]+>", "", xml)


# --- C9 类：无依据门槛不得回归 ---

UNGROUNDED_PATTERNS = [
    (re.compile(r"英文\s*[≥>＞]\s*[56]0\s*%"), "英文文献 ≥50% 以上的门槛"),
    (re.compile(r"参考文献?\s*[≥>＞]\s*30\s*篇"), "文献 ≥30 篇的硬门槛"),
    (re.compile(r"[≥>＞]\s*1\.5\s*万字"), "1.5 万字的字数硬门槛"),
]
# 允许出现这些模式的语境：反模式说明、修订记录、依据对照
EXEMPT_CONTEXT = (
    "反模式", "早期版本", "不要套用", "无官方依据", "无依据", "不成立",
    "相反", "冲突", "不设", "早期", "曾在本 skill", "thredholds",
    "thresholds", "已知错误", "错误值", "UNGROUNDED",
)


def _skill_files():
    files = list(ROOT.glob("subskills/*/SKILL.md"))
    files += list(ROOT.glob("subskills/*/references/*.md"))
    files += list(ROOT.glob("references/*.md"))
    return sorted(files)


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: str(p.name))
def test_no_ungrounded_thresholds_regress(path):
    """面向用户的文档不得把无官方出处的数值写成门槛。"""
    text = path.read_text(encoding="utf-8")
    hits = []
    for i, line in enumerate(text.splitlines(), start=1):
        if any(x in line for x in EXEMPT_CONTEXT):
            continue
        for pattern, label in UNGROUNDED_PATTERNS:
            if pattern.search(line):
                hits.append(f"{path.name}:{i} {label}")
    assert not hits, "出现无依据门槛：" + "；".join(hits)


def test_english_ratio_direction_is_correct():
    """英文文献方向必须是「不超过 50%」，不能是「≥60%」。"""
    text = (SUBSKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "英文不超过 50%" in text or "英文占比 ≤50%" in text or "≤50%" in text
    # 不应再出现把英文占比当加分项的正向表述
    assert not re.search(r"英文\s*[≥>＞]\s*60\s*%(?!\s*」)", text) or \
        "早期版本" in text or "不要套用" in text


# --- 经费：不得低于教高函〔2019〕13 号下限 ---

def test_default_data_meets_national_funding_floor():
    """DEFAULT_DATA 是国家级示例，经费不得低于国家规定下限。"""
    data = _load_default_data()
    total = int(data["budget_total"])
    assert total >= NATIONAL_FUNDING_FLOOR, (
        f"示例申请经费 {total} 元低于教高函〔2019〕13 号规定的"
        f"「平均不低于 {NATIONAL_FUNDING_FLOOR} 元/项」")


def test_default_data_budget_items_sum_matches_total():
    data = _load_default_data()
    total = sum(int(b["amount"]) for b in data["budget_items"])
    assert total == int(data["budget_total"]), "预算明细合计与申请经费不一致"


def test_budget_validation_flags_below_floor():
    """低于国家下限时，校验必须报出来。"""
    text = BUILD.read_text(encoding="utf-8")
    assert "教高函〔2019〕13 号第九条（六）" in text
    assert "平均不低于 2 万元/项" in text


# --- 文献指标 ---

def test_default_data_literature_ratios_are_reported_not_hidden():
    """示例文献不满足形式审查时，build.py 必须如实警告而不是静默通过。"""
    out = Path("/tmp") / "_lit_check.docx"
    proc = subprocess.run(
        [sys.executable, str(BUILD), "--demo", "--out", str(out)],
        capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-800:]
    combined = proc.stdout + proc.stderr
    data = _load_default_data()
    refs = data["literature_review"]
    en = sum(1 for r in refs if str(r.get("language", "")).lower() in ("en", "english"))
    if en / len(refs) > EN_LIT_RATIO_MAX:
        assert "英文文献占比" in combined, "英文超限未被警告"
    assert "示例数据仅示范结构" in combined, "示例数据缺少定位说明"


def test_literature_ratio_helpers_agree_with_thresholds():
    """用一组构造数据验证 80% / 50% 两条判据的方向。"""
    def recent_ratio(years, current=2026):
        recent = [y for y in years if y >= current - 5]
        return len(recent) / len(years)

    assert recent_ratio([2024, 2025, 2026]) == 1.0
    assert recent_ratio([2010, 2011, 2012]) == 0.0
    assert recent_ratio([2024, 2010]) == 0.5
    assert recent_ratio([2024, 2010]) < RECENT_LIT_RATIO


# --- 文号：已核实的不得被改回错误值 ---

VERIFIED = {
    "「十四五」国家应急体系规划": "国发〔2021〕36 号",
    "「十四五」机器人产业发展规划": "工信部联规〔2021〕179 号",
    "新一代人工智能发展规划": "国发〔2017〕35 号",
}

KNOWN_WRONG = {
    "工信部联重装〔2024〕12 号": "通用航空装备方案应为 52 号",
    "应急〔2023〕116 号": "应急机器人指导意见应为 148 号",
    "工信部联装〔2025〕198 号": "安全应急装备目录应为工信厅安全函〔2025〕283 号",
    "中办发〔2024〕23 号": "韧性城市意见公开文本无文号",
    "应急〔2022〕15 号": "应急救援力量建设规划公开文本无文号",
}


def test_default_data_has_no_fabricated_doc_numbers():
    """DEFAULT_DATA 不得含已确认编造或错误的文号。"""
    raw = BUILD.read_text(encoding="utf-8")
    offenders = [f"{bad}（{why}）" for bad, why in KNOWN_WRONG.items() if bad in raw]
    assert not offenders, "DEFAULT_DATA 含错误文号：" + "；".join(offenders)


def test_default_data_uses_verified_doc_numbers():
    raw = BUILD.read_text(encoding="utf-8")
    missing = [f"《{t}》应写作 {d}" for t, d in VERIFIED.items()
               if t in raw and d not in raw]
    assert not missing, "；".join(missing)


def test_policy_citations_are_in_reverse_chronological_order():
    """政策引用须按时间倒序（最新在前）。"""
    data = _load_default_data()
    dates = []
    for p in data["policy_citations"]:
        m = re.match(r"^(\d{4})\.(\d{2})$", str(p.get("date", "")))
        if m:
            dates.append((int(m.group(1)), int(m.group(2))))
    assert dates == sorted(dates, reverse=True), f"政策未按时间倒序：{dates}"


def test_policy_citations_have_minimum_count():
    """国家级示例应给出足够条数以示范写法（≥6 条已足以说明问题）。"""
    data = _load_default_data()
    assert len(data["policy_citations"]) >= 6


def test_policy_without_doc_no_is_explicitly_marked():
    """没有公开发文字号的文件必须显式说明，不能留空让人误以为漏填。"""
    data = _load_default_data()
    for p in data["policy_citations"]:
        if not str(p.get("doc_no", "")).strip():
            assert "未标注发文字号" in p.get("key_quote", ""), (
                f"《{p['title']}》无文号却未说明")


# --- 主题污染：非消防主题不得被写入消防措辞 ---

SOCIO_DATA = {
    "project_name": "城市社区居家养老服务供需错配的成因与治理路径研究",
    "project_level": "省级",
    "project_type": "创新训练项目",
    "leader_name": "张三", "leader_id": "202212345", "leader_major": "社会工作",
    "leader_grade": "2022 级", "leader_phone": "138XXXXXXXX",
    "advisor_name": "李教授", "advisor_title": "教授",
    "advisor_research": "社会保障", "college": "社会学院",
    "apply_date": "2026 年 3 月",
    "basic_info": {
        "project_name": "城市社区居家养老服务供需错配的成因与治理路径研究",
        "project_type": "创新训练项目", "project_source": "A 学生自主选题",
        "discipline": "0303 社会学", "duration": "2026.04-2027.03（12 个月）",
        "budget": "8000",
        "leader_info": "张三 / 202212345 / 社会工作 / 大三 / 138XXXXXXXX",
        "team_members": "李四（问卷设计）、王五（访谈执行）",
        "advisor_info": "李教授 / 教授 / 社会保障 / 139XXXXXXXX",
    },
    "abstract": "本项目以某市 3 个街道为样本，通过问卷调查与深度访谈识别"
                "居家养老服务供需错配的成因。",
    "background": ["我国 60 岁以上人口规模持续扩大，居家养老服务供给与"
                   "需求之间的结构性错配日益突出。"],
    "policy_citations": [], "scientific_challenges": [],
    "algorithm_comparison": {}, "formulas": [], "tech_roadmap": [],
    "literature_review": [],
    "research_content": ["梳理居家养老政策演进与地方实践差异"],
    "research_goal": "形成可操作的供需匹配治理方案。",
    "innovations": ["视角创新：从供需双侧匹配而非单侧供给切入。"],
    "team_foundation": "团队修读《社会研究方法》。",
    "advisor_foundation": "指导教师主持省社科基金 1 项。",
    "lab_condition": "学院配备统计软件与访谈室。",
    "budget_items": [{"item": "资料费", "amount": "8000", "basis": "图书与数据库"}],
    "budget_total": "8000",
}

TOPIC_WORDS = ["消防", "无人机", "火场", "YOLO", "多模态", "3D 目标检测",
               "GPU", "应急救援", "卷积神经网络"]


def test_non_cs_topic_has_no_contamination(tmp_path):
    """非工科选题生成的 docx 不得出现消防/无人机等无关措辞。"""
    data_file = tmp_path / "socio.json"
    data_file.write_text(json.dumps(SOCIO_DATA, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "socio.docx"
    proc = subprocess.run(
        [sys.executable, str(BUILD), "--data", str(data_file), "--out", str(out)],
        capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-800:]
    text = _docx_text(out)
    found = [(w, text.count(w)) for w in TOPIC_WORDS if w in text]
    assert not found, f"非工科选题被写入无关技术措辞：{found}"


def test_cs_topic_still_renders_its_own_content(tmp_path):
    """反向验证：消防主题仍然正常输出本主题内容（没被误删）。"""
    out = tmp_path / "demo.docx"
    proc = subprocess.run(
        [sys.executable, str(BUILD), "--demo", "--out", str(out)],
        capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-800:]
    text = _docx_text(out)
    assert "消防" in text or "无人机" in text


# --- 不可核验的「训练声明」不得回归 ---

def test_no_unverifiable_training_claims():
    """不得声称「深度学习 N 篇范文」「输出国奖标准」这类无法核验的能力。"""
    bad_patterns = [
        re.compile(r"已深度学习\s*\d+\s*[+＋]?\s*篇"),
        re.compile(r"深度学习近\s*\d+\s*届"),
        re.compile(r"输出符合国奖标准"),
    ]
    hits = []
    for path in sorted(ROOT.glob("subskills/*/SKILL.md")):
        text = path.read_text(encoding="utf-8")
        for pat in bad_patterns:
            m = pat.search(text)
            if m:
                hits.append(f"{path.parent.name}: {m.group(0)}")
    assert not hits, "出现不可核验的训练声明：" + "；".join(hits)


# --- 死代码不得回归 ---

def test_skill_md_code_blocks_are_runnable_shape():
    """SKILL.md 里给出的脚本不得调用未定义的辅助函数。"""
    text = (SUBSKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "extract_section" not in text, (
        "§12.3 引用了未定义的 extract_section，读者照抄会直接 NameError")


def test_thresholds_reference_document_exists_and_cites_sources():
    """门槛依据文件必须存在，且给出可查的一手来源链接。"""
    doc = SUBSKILL / "references" / "thresholds-and-sources.md"
    assert doc.exists(), "缺少门槛依据文件"
    text = doc.read_text(encoding="utf-8")
    assert "教高函〔2019〕13" in text
    assert "gov.cn" in text, "缺少一手来源链接"
    # 必须写明三类数值的效力区分
    for key in ("国家规定", "高校形式审查", "默认值"):
        assert key in text, f"依据文件缺少「{key}」分类"
