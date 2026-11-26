"""Run each indexed guide's real builder and the workbench's Word export."""
from pathlib import Path
import re
import subprocess
import sys

from docx import Document
import pytest

from server.proposal.material_export import export_material
from server.proposal.outline_fallback import recommended_outline
from server.proposal.skills import registry
from server.proposal.storage import Store


ROOT = Path(__file__).resolve().parents[1]
TRACKS = {track["id"]: track for track in registry.list()}


def test_every_track_has_one_support_level():
    assert len(TRACKS) == len(registry.skills)
    assert set(TRACKS) == {path.parent.name for path in (ROOT / "subskills").glob("*/SKILL.md")}
    assert {name for name, track in TRACKS.items() if track["support_level"] == "specialized_draft"} == {
        "innovation-research",
    }
    assert {track["support_level"] for track in TRACKS.values()} == {"standard_draft", "specialized_draft"}
    catalog = (ROOT / "subskills/README.md").read_text(encoding="utf-8")
    rows = re.findall(r"(?m)^\| \[([^/]+)/\]\(\./[^/]+/SKILL\.md\).*\| (通用草稿|专用草稿) \|$", catalog)
    labels = {"standard_draft": "通用草稿", "specialized_draft": "专用草稿"}
    assert len(rows) == len(TRACKS)
    assert dict(rows) == {name: labels[track["support_level"]] for name, track in TRACKS.items()}


@pytest.mark.parametrize("track_id", sorted(TRACKS))
def test_track_smoke(track_id, tmp_path, monkeypatch):
    track = TRACKS[track_id]
    skill = registry.get(track_id)
    assert skill and registry.model_guidance_page(track_id, 0)["guidance"].strip()
    builder = ROOT / skill["build_py_path"]
    assert builder == ROOT / "subskills" / track_id / "build.py"
    demo = tmp_path / "demo.docx"
    process = subprocess.run(
        [sys.executable, str(builder), "--demo", "--out", str(demo)], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    assert process.returncode == 0 and demo.is_file(), process.stdout + process.stderr
    assert any(text.strip() for text in Document(demo).element.body.xpath(".//w:t/text()"))

    monkeypatch.setenv("MATERIAL_PIPELINE_ENABLED", "true")
    store = Store(tmp_path / "store")
    project_id = store.create_project(title="合成赛道冒烟")["id"]
    state = {"skill_id": track_id, "skill_category": track["category"], "outline_confirmed": True}
    outline, _ = recommended_outline({"state": state})
    assert outline and all(section["id"] and section["title"] for section in outline)
    sample = "本段由测试写入，用于核对导出是否保留项目正文。"
    project = store.update_project(project_id, state=state, outline=outline, sections={outline[0]["id"]: sample})
    specialized = track["support_level"] == "specialized_draft"
    assert project["material"]["pipeline"] == ("innovation-research-v1" if specialized else "material-v1")
    folder = tmp_path / "export"
    report = export_material(store, project_id, folder)
    content = [p.text for p in Document(folder / report["file"]).paragraphs]
    cover = "创新训练项目申报书（草稿）" if specialized else f"{track['name']}（草稿）"
    assert content.count(cover) == 1
    assert content.count(sample) == 1
    assert report["template_status"] == "generic"
