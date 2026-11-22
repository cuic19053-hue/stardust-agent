"""Exported data tables retain their header and pagination through every path."""
import io

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm
from fastapi.testclient import TestClient

import server.app as app
from proposal.exporter import export_project
from proposal.material_export import export_material
from proposal.storage import Store
from proposal.tools import ProposalTools
from utils.docx_common import harden_table


@pytest.fixture(params=["direct", "web", "worker"])
def export_path(request, tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    monkeypatch.setenv("MATERIAL_PIPELINE_ENABLED", "true")
    monkeypatch.setattr(app, "CLOUD_MODE", False)
    monkeypatch.setattr(app, "store", store)
    monkeypatch.setattr(app, "tools", ProposalTools(store))

    def export(project_id, *, allow_layout_failure=False):
        if request.param == "web":
            with TestClient(app.app) as client:
                response = client.post(f"/api/projects/{project_id}/export")
                assert response.status_code == 200, response.text
                if not allow_layout_failure:
                    assert response.json()["layout"]["score"] is not None
                download = client.get(response.json()["download_url"])
                assert download.status_code == 200
                return Document(io.BytesIO(download.content))
        if request.param == "worker":
            report = export_material(store, project_id, tmp_path / "worker")
            if not allow_layout_failure:
                assert not any("排版优化未完成" in warning for warning in report["warnings"])
            return Document(tmp_path / "worker" / "proposal.docx")
        target = tmp_path / "direct.docx"
        assert export_project(store, project_id, target)["success"]
        return Document(target)

    return store, export


def project_with_table(store, content):
    project = store.create_project(title="表格分页回归")
    return store.update_project(
        project["id"], state={"skill_id": "innovation-research", "outline_confirmed": True},
        outline=[{"id": "budget", "title": "经费预算"}], sections={"budget": content},
    )["id"]


def flag(row, name):
    return row._tr.trPr is not None and row._tr.trPr.find(qn(f"w:{name}")) is not None


def assert_data_table(table, expected):
    assert [[cell.text for cell in row.cells] for row in table.rows] == expected
    assert all(flag(row, "cantSplit") for row in table.rows)
    assert flag(table.rows[0], "tblHeader")
    assert not any(flag(row, "tblHeader") for row in table.rows[1:])
    assert all(p.paragraph_format.keep_with_next for c in table.rows[0].cells for p in c.paragraphs)
    # A few tall rows can span pages, so only the header stays with its successor.
    assert not any(p.paragraph_format.keep_with_next for row in table.rows[1:]
                   for cell in row.cells for p in cell.paragraphs)


@pytest.mark.parametrize("body_rows", [2, 24])
def test_short_and_long_tables_repeat_header_without_binding_all_rows(export_path, body_rows):
    store, export = export_path
    expected = [["开支科目", "预算经费（元）", "估算依据"]]
    expected += [[f"计算测试费{i}", str(100 + i), "仅供软件回归的模拟估算"] for i in range(body_rows)]
    lines = ["|" + "|".join(row) + "|" for row in expected]
    lines.insert(1, "|---|---:|---|")
    pid = project_with_table(store, "\n".join(lines))
    document = export(pid)
    table = next(t for t in document.tables if t.cell(0, 0).text == "开支科目")
    assert_data_table(table, expected)
    cover = document.tables[0]
    assert not any(flag(row, "tblHeader") for row in cover.rows)
    assert store.get_project(pid)["sections"]["budget"] == "\n".join(lines)


def test_headerless_tables_do_not_repeat_the_first_record(export_path):
    store, export = export_path
    content = "\n".join(f"|记录{i}|{i}|" for i in range(10))
    document = export(project_with_table(store, content))
    table = next(t for t in document.tables if t.cell(0, 0).text == "记录0")
    assert len(table.rows) == 10
    assert all(flag(row, "cantSplit") for row in table.rows)
    assert not any(flag(row, "tblHeader") for row in table.rows)


def test_placeholder_and_empty_cells_survive_word_export(export_path):
    store, export = export_path
    content = "|项目|状态|\n|---|---|\n|---|---|\n| | |\n|预算|待定|\n||未定|\n|试剂||"
    document = export(project_with_table(store, content))
    table = next(t for t in document.tables if t.cell(0, 0).text == "项目")
    assert [[cell.text for cell in row.cells] for row in table.rows] == [
        ["项目", "状态"], ["---", "---"], ["", ""], ["预算", "待定"],
        ["", "未定"], ["试剂", ""],
    ]


def test_school_table_pagination_and_page_setup_are_preserved(export_path, tmp_path):
    store, export = export_path
    template = Document()
    template.sections[0].page_width = Cm(19)
    template.sections[0].left_margin = Cm(3.2)
    table = template.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "学校原有栏目"
    table.cell(0, 1).text = "固定填写说明"
    table.cell(1, 0).text = "长篇申请理由"
    table.cell(1, 1).text = "学校保留的内容"
    table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
    original_xml = table._tbl.xml
    path = tmp_path / "school.docx"
    template.save(path)
    original_bytes = path.read_bytes()
    pid = project_with_table(store, "|开支科目|金额|\n|---|---|\n|计算测试费|1200|")
    uploaded = store.add_document(pid, "school.docx", str(path), "template",
                                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    store.set_template_document(pid, uploaded["id"])
    document = export(pid)
    assert document.tables[0]._tbl.xml == original_xml
    assert document.sections[0].page_width == template.sections[0].page_width
    assert document.sections[0].left_margin == template.sections[0].left_margin
    assert path.read_bytes() == original_bytes
    generated = next(t for t in document.tables if t.cell(0, 0).text == "开支科目")
    assert_data_table(generated, [["开支科目", "金额"], ["计算测试费", "1200"]])


def test_inline_protection_survives_optional_layout_failure(export_path, monkeypatch):
    import utils.docx_layout_fix as layout

    def fail(*args, **kwargs):
        raise OSError("simulated optional layout failure")

    monkeypatch.setattr(layout, "fix", fail)
    store, export = export_path
    document = export(project_with_table(store, "|开支科目|金额|\n|---|---|\n|测试费|1200|"),
                      allow_layout_failure=True)
    table = next(t for t in document.tables if t.cell(0, 0).text == "开支科目")
    assert_data_table(table, [["开支科目", "金额"], ["测试费", "1200"]])


def test_explicit_table_protection_is_idempotent_and_handles_multiple_header_rows():
    table = Document().add_table(rows=5, cols=2)
    for name in ("cantSplit", "tblHeader"):
        disabled = OxmlElement(f"w:{name}")
        disabled.set(qn("w:val"), "0")
        table.rows[0]._tr.get_or_add_trPr().append(disabled)
    for _ in range(2):
        harden_table(table, keep_together=False, header_rows=2)
    for index, row in enumerate(table.rows):
        assert len(row._tr.trPr.findall(qn("w:cantSplit"))) == 1
        assert row._tr.trPr.find(qn("w:cantSplit")).get(qn("w:val")) == "1"
        assert len(row._tr.trPr.findall(qn("w:tblHeader"))) == (1 if index < 2 else 0)
        assert all(bool(p.paragraph_format.keep_with_next) == (index < 2)
                   for cell in row.cells for p in cell.paragraphs)
