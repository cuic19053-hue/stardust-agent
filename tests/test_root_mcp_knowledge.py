from __future__ import annotations

import importlib.util
from pathlib import Path
import pytest

if importlib.util.find_spec("mcp") is None:
    pytest.skip("mcp not installed", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("root_student_mcp_test", ROOT / "mcp_server.py")
assert spec and spec.loader
mcp_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp_server)


def test_mcp_route_abstains_and_public_policy_is_scope_bound():
    route = mcp_server.route_student_skill("想参加挑战杯或者互联网+")
    assert route["selected"] is None
    assert route["needs_clarification"] is True
    missing = mcp_server.search_student_knowledge("single-scholarship", "单项奖学金申请条件")
    assert missing["official_only"] and missing["results"] == []
    valid = mcp_server.search_student_knowledge(
        "single_scholarship", "单项奖学金申请条件", school="河南大学",
        application_year=2026, education_level="本科",
    )
    assert valid["official_evidence_found"]
    assert all(item["school"] == "河南大学" for item in valid["results"])
    assert "调用者提供" in valid["scope_notice"]


def test_mcp_rejects_unindexed_skill_paths():
    assert mcp_server.get_skill_requirements("../../outside").get("error")
    assert mcp_server.generate_student_doc("../../outside", demo=True).get("error")
    assert mcp_server.search_student_knowledge("../../outside", "项目").get("error")


def test_default_doc_paths_preserve_each_applicant(tmp_path, monkeypatch):
    from docx import Document

    (tmp_path / "subskills").symlink_to(ROOT / "subskills", target_is_directory=True)
    monkeypatch.setattr(mcp_server, "PROJECT_DIR", tmp_path)
    paths = []
    for name in ("模拟甲", "模拟乙"):
        result = mcp_server.generate_student_doc("national-scholarship", data={
            "applicant_name": name, "paragraphs": ["申请正文"],
        })
        assert result["success"]
        paths.append(Path(result["output_path"]))
    assert paths[0] != paths[1]
    assert [Document(path).paragraphs[-1].text.splitlines()[0] for path in paths] == ["模拟甲", "模拟乙"]
