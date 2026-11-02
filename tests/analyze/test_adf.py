"""v2.1z-d (2026-11-02): Oracle ADF (JDeveloper) detection — four independent evidence families (A `bc4j` BC4J XML, B `java`
oracle.jbo / adf / binding imports, C `model` adfm.xml / DataBindings.cpx / pageDef, D `faces` ADF Faces pages / adfc-config /
bounded flows); **two or more families reject** ("ADF 프로젝트라 변환이 불가능합니다"), one family is only a trace, `.jpr` names
and `adf-config.xml` are hints. No directory name is interpreted; `target/` and IDE / VCS dirs are ignored."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze.adf import (
    ADF_MESSAGE_KO,
    ADF_PREFIX,
    TRACES_PREFIX,
    AdfInventory,
    adf_abort_reason,
    adf_followup,
    adf_sizing_lines,
    detect_adf,
    traces_followup,
)
from sbmig.analyze.analyzer import PathView
from sbmig.report.i18n import tr_line

BC4J = 'xmlns="http://xmlns.oracle.com/bc4j"'
AF = 'xmlns:af="http://xmlns.oracle.com/adf/faces/rich"'
CTRL = 'xmlns="http://xmlns.oracle.com/adf/controller"'


def _entity(name: str, table: str) -> str:
    return (f"<?xml version='1.0' encoding='UTF-8'?>\n<!DOCTYPE Entity SYSTEM \"jbo_03_01.dtd\">\n"
            f'<Entity {BC4J} Name="{name}" Version="12.2.1.4.0" DBObjectType="table" DBObjectName="{table}">'
            f'<Attribute Name="Id" ColumnName="ID" SQLType="NUMERIC" Type="oracle.jbo.domain.Number" PrimaryKey="true"/>'
            "</Entity>\n")


def _vo(name: str) -> str:
    return (f'<?xml version="1.0"?><ViewObject {BC4J} Name="{name}" Version="12.2.1.4.0" CustomQuery="true">'
            "<SQLQuery><![CDATA[SELECT 1 FROM DUAL]]></SQLQuery></ViewObject>")


def _am(name: str) -> str:
    return f'<?xml version="1.0"?><AppModule {BC4J} Name="{name}" Version="12.2.1.4.0"/>'


FAMILY_A = {"DEV/s10/src/model/Emp.xml": _entity("Emp", "EMP"), "DEV/s10/src/model/Dept.xml": _entity("Dept", "DEPT"),
            "DEV/s10/src/model/EmpView.xml": _vo("EmpView"), "DEV/s10/src/model/HRModule.xml": _am("HRModule")}
FAMILY_B = {"DEV/s10/src/model/EmpImpl.java": "package model;\nimport oracle.jbo.server.EntityImpl;\npublic class EmpImpl extends EntityImpl {}\n",
            "DEV/s10/src/view/EmpBean.java": "package view;\n\nimport java.util.List;\nimport oracle.adf.model.BindingContext;\npublic class EmpBean {}\n"}
FAMILY_C = {"DEV/s10/adfmsrc/model/adfm.xml": '<MetadataDirectory xmlns="http://xmlns.oracle.com/adfm/metainf" version="11.1.1.0.0"/>',
            "DEV/s10/adfmsrc/model/DataBindings.cpx": '<Application xmlns="http://xmlns.oracle.com/adfm/application" id="DataBindings"/>',
            "DEV/s10/adfmsrc/view/mainPageDef.xml": '<pageDefinition xmlns="http://xmlns.oracle.com/adfm/uimodel" id="mainPageDef"/>'}
FAMILY_D = {"DEV/s10/public_html/WEB-INF/adfc-config.xml": f'<adfc-config {CTRL} version="1.2">'
                                                           '<view id="main"><page>/main.jspx</page></view>'
                                                           '<view id="detail"><page>/detail.jspx</page></view></adfc-config>',
            "DEV/s10/public_html/WEB-INF/emp-flow.xml": f'<adfc-config {CTRL} version="1.2"><task-flow-definition id="emp-flow">'
                                                        '<view id="edit"><page>/edit.jsff</page></view></task-flow-definition></adfc-config>',
            "DEV/s10/public_html/main.jspx": f'<jsp:root xmlns:jsp="http://java.sun.com/JSP/Page" {AF}/>',
            "DEV/s10/public_html/detail.jspx": f'<jsp:root xmlns:jsp="http://java.sun.com/JSP/Page" {AF}/>',
            "DEV/s10/public_html/edit.jsff": f'<jsp:root {AF}/>'}
COMMON = {"APP/pom.xml": '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
                         "<groupId>g</groupId><artifactId>app</artifactId><version>1</version><packaging>pom</packaging></project>",
          "DEV/s10/s10.jpr": '<?xml version = "1.0"?><jpr:project xmlns:jpr="http://xmlns.oracle.com/ide/project">'
                             '<list n="technologyScope"><string v="ADF_FACES"/><string v="ADFbc"/></list></jpr:project>',
          "DEV/s10/src/view/Formats.java": "package view;\npublic class Formats {}\n",
          "STATIC/css/app.css": "body{}",
          "DEV/s10/target/classes/model/Emp.xml": _entity("Emp", "EMP"),        # build output: pruned, never counted
          ".git/objects/x/Emp.xml": _entity("Emp", "EMP")}                        # VCS metadata: pruned


def _tree(tmp_path: Path, *parts: dict[str, str]) -> PathView:
    for files in parts:
        for rel, text in files.items():
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
    return PathView(tmp_path)


def test_full_workspace_all_four_families_is_rejected_and_sized(tmp_path):
    inv = detect_adf(_tree(tmp_path, COMMON, FAMILY_A, FAMILY_B, FAMILY_C, FAMILY_D))
    assert inv.is_adf and not inv.traces and not inv.error
    assert inv.present == ["bc4j", "java", "model", "faces"]
    assert (inv.entities, inv.view_objects, inv.app_modules, inv.associations, inv.view_links) == (2, 1, 1, 0, 0)
    assert sorted(inv.tables) == ["DEPT", "EMP"]
    assert inv.page_defs == 1 and inv.pages == 3                       # 2 jspx + 1 jsff, all with the af namespace
    assert inv.task_flows == 2 and inv.flow_views == 3                 # adfc-config (2 views) + the bounded flow (1 view)
    assert (inv.java_total, inv.java_adf) == (3, 2)                     # Formats.java imports nothing from oracle.*
    assert inv.hints == ["DEV/s10/s10.jpr"]                            # a .jpr technology name is a hint, not evidence
    assert "DEV/s10/adfmsrc/model/adfm.xml" in inv.families["model"] and "DEV/s10/src/model/EmpImpl.java" in inv.families["java"]
    assert "DEV/s10/public_html/WEB-INF/emp-flow.xml" in inv.families["faces"]
    assert not any("target/" in p or ".git/" in p for v in inv.families.values() for p in v)
    assert inv.by_dir == {"DEV": {"java": 3, "java_adf": 2, "bc4j": 4, "pages": 3, "page_defs": 1}}
    s = inv.summary()
    assert s.is_adf and s.families == inv.present and s.tables == ["DEPT", "EMP"] and s.tables_total == 2
    assert s.evidence_counts == {"bc4j": 4, "java": 2, "model": 3, "faces": 5} and s.evidence_total == 14
    assert all(len(v) <= 8 for v in s.evidence.values())


def test_family_a_alone_is_a_trace_not_a_rejection(tmp_path):
    inv = detect_adf(_tree(tmp_path, COMMON, FAMILY_A))
    assert inv.present == ["bc4j"] and inv.traces and not inv.is_adf
    assert inv.entities == 2 and inv.java_adf == 0


def test_family_d_alone_is_a_trace_not_a_rejection(tmp_path):
    inv = detect_adf(_tree(tmp_path, COMMON, FAMILY_D))
    assert inv.present == ["faces"] and inv.traces and not inv.is_adf
    assert inv.pages == 3 and inv.task_flows == 2


def test_families_a_and_b_are_rejected(tmp_path):
    inv = detect_adf(_tree(tmp_path, COMMON, FAMILY_A, FAMILY_B))
    assert inv.present == ["bc4j", "java"] and inv.is_adf


def test_families_c_and_d_are_rejected(tmp_path):
    inv = detect_adf(_tree(tmp_path, COMMON, FAMILY_C, FAMILY_D))
    assert inv.present == ["model", "faces"] and inv.is_adf and inv.entities == 0


def test_plain_servlet_project_with_a_stray_adf_config_is_nothing(tmp_path):
    ws = _tree(tmp_path, {"pom.xml": "<project/>", "src/main/resources/adf-config.xml": "<adf-config/>",
                          "src/main/java/a/Hello.java": "package a;\nimport javax.servlet.http.HttpServlet;\npublic class Hello extends HttpServlet {}\n",
                          "src/main/webapp/index.jsp": "<%= 1 %>", "src/main/webapp/plain.jspx": "<jsp:root/>",
                          "tools/tools.jpr": '<jpr:project><string v="ADF_FACES"/></jpr:project>'})
    inv = detect_adf(ws)
    assert inv.present == [] and not inv.is_adf and not inv.traces
    assert inv.hints == ["src/main/resources/adf-config.xml", "tools/tools.jpr"]
    assert inv.pages == 0 and inv.java_total == 1 and inv.java_adf == 0      # a plain JSPX is not an ADF page


def test_bc4j_xml_needs_the_namespace_and_a_known_root(tmp_path):
    ws = _tree(tmp_path, {"m/Emp.xml": '<Entity Name="Emp" DBObjectName="EMP"/>',               # no bc4j namespace
                          "m/Other.xml": f'<Thing {BC4J} Name="x"/>',                              # namespace, unknown root
                          "m/FooPageDef.xml": "<something/>",                                      # name only: not a pageDef
                          "m/x-flow.xml": '<adfc-config version="1.2"><task-flow-definition id="x"/></adfc-config>'})  # no controller ns
    inv = detect_adf(ws)
    assert inv.present == [] and inv.entities == 0 and inv.page_defs == 0 and inv.task_flows == 0


def test_missing_root_and_oracle_import_deep_in_a_file_do_not_break(tmp_path):
    assert not detect_adf(PathView(tmp_path / "nowhere")).present
    body = "package a;\n" + "// filler\n" * 200 + "import oracle.jbo.Row;\npublic class Late {}\n"
    inv = detect_adf(_tree(tmp_path, {"a/Late.java": body}))
    assert inv.java_total == 1 and inv.java_adf == 0        # imports are read from the file head only (cheap by design)
    assert not detect_adf(str(tmp_path)).error                # a plain path works too (never PurePath.root)


def test_followup_lines_and_their_korean_rendering():
    inv = AdfInventory(entities=12, view_objects=20, app_modules=2, page_defs=30, task_flows=5, flow_views=40, pages=34,
                       java_total=1546, java_adf=900, tables=[f"T{i}" for i in range(11)])
    inv.families = {"bc4j": ["m/Emp.xml"], "java": ["m/EmpImpl.java"], "model": [], "faces": []}
    s = inv.summary()
    line = adf_followup(s)
    assert line.startswith(ADF_PREFIX + " " + ADF_MESSAGE_KO + " (evidence: bc4j, java; 12 entities / 11 tables, 20 view objects")
    assert line.endswith("900 of 1546 java files on ADF)") and "rewrite" not in line
    ko = tr_line("ko", line)
    assert ko.startswith("ADF 프로젝트라 변환이 불가능합니다 (근거: bc4j, java; 엔티티 12개 / 테이블 11개") and ko.endswith("java 1546개 중 900개가 ADF)")
    cont = adf_followup(s, "continue")
    assert cont.endswith(" — intake.adf_policy: continue (report only, the run goes on; not recommended)")
    assert tr_line("ko", cont).startswith("ADF 프로젝트라 변환이 불가능합니다") and tr_line("ko", cont) != cont
    assert adf_abort_reason().startswith("ADF project — conversion is not possible (" + ADF_MESSAGE_KO)
    inv.families = {"bc4j": [], "java": [], "model": [], "faces": ["p/main.jspx", "p/WEB-INF/adfc-config.xml"]}
    t = inv.summary()
    note = traces_followup(t)
    assert note.startswith(TRACES_PREFIX + " (one evidence family only — faces: p/main.jspx, p/WEB-INF/adfc-config.xml)")
    assert "ADF 흔적: 변환은 계속하되 확인 필요" in note
    assert tr_line("ko", note).startswith("ADF 흔적(근거 계열 하나 — faces: p/main.jspx, p/WEB-INF/adfc-config.xml) — 변환은 계속하되 확인 필요 (")
    sizing = adf_sizing_lines(s)
    assert sizing[0].startswith("ADF: ADF project — conversion is not possible; families: bc4j, java; 12 entities / 11 tables")
    assert any(l.strip().startswith("evidence: bc4j: m/Emp.xml; java: m/EmpImpl.java") for l in sizing)
    assert adf_sizing_lines(t)[0].startswith("ADF: ADF traces (one evidence family) — not rejected, check needed; families: faces")
