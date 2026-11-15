"""`sbmig.restructure.source_encoding`: the source-encoding step without the restructure context (the reduced run of a
framework project calls it after each `transform.rewrite`; `_lc_source_encodings` delegates to it), including the kept
external parent's value read from the as-is effective pom capture."""
from __future__ import annotations

import pytest

from sbmig.core.workspace import Workspace
from sbmig.restructure.source_encoding import write_source_encodings

PARENT = ("  <parent>\n    <groupId>com.example.fw</groupId>\n    <artifactId>example-parent</artifactId>\n"
          "    <version>1.0</version>\n  </parent>\n")


def pom(parent: bool = True, props: str = "") -> str:
    p = f"  <properties>\n{props}  </properties>\n" if props else ""
    return ('<project xmlns="http://maven.apache.org/POM/4.0.0">\n  <modelVersion>4.0.0</modelVersion>\n'
            + (PARENT if parent else "") + "  <groupId>com.x</groupId>\n  <artifactId>demo</artifactId>\n"
            "  <version>1</version>\n" + p + "</project>\n")


def make_ws(tmp_path, pom_text: str, *, mixed: bool = False) -> Workspace:
    src = tmp_path / "src" / "demo"
    java = src / "src/main/java/com/x"
    java.mkdir(parents=True)
    (src / "pom.xml").write_text(pom_text)
    (java / "Greeting.java").write_bytes('package com.x;\n/** 인사 */\nclass Greeting { String s = "안녕하세요"; }\n'
                                         .encode("cp949"))
    if mixed:
        (java / "B.java").write_text('package com.x;\nclass B { String s = "라마바"; }\n', encoding="utf-8")
    w = Workspace.from_input(src, tmp_path / "out", run_id="t")
    w.git_init()
    return w


LINE = "pom.xml: project.build.sourceEncoding=MS949 (1 of 1 sampled .java files cp949)"


def capture(value: str | None, parent: str = "com.example.fw:example-parent:1.0") -> dict:
    return {"properties": {"project.build.sourceEncoding": value} if value else {}, "parent": parent,
            "managed": {}, "direct": {}, "by_module": {}}


def test_writes_the_property_and_is_idempotent(tmp_path, cfg):
    ws = make_ws(tmp_path, pom())
    out = write_source_encodings(ws, cfg)
    assert out.written == [LINE] and out.notes == ["source encoding written: " + LINE] and out.changed == ["pom.xml"]
    assert "<project.build.sourceEncoding>MS949</project.build.sourceEncoding>" in ws.read("pom.xml")
    again = write_source_encodings(ws, cfg)
    assert again.written == [] and again.changed == [] and again.followups == []


def test_switch_off_and_mixed_modules_are_sentences_only(tmp_path, cfg):
    ws = make_ws(tmp_path, pom())
    out = write_source_encodings(ws, cfg, on=False)
    assert out.written == [] and "no project.build.sourceEncoding says so" in out.followups[0]
    ws2 = make_ws(tmp_path / "m", pom(), mixed=True)
    out2 = write_source_encodings(ws2, cfg)
    assert out2.written == [] and "the sources mix MS949 and UTF-8 files" in out2.followups[0]


@pytest.mark.parametrize("value", ["MS949", "EUC-KR", "x-windows-949"])
def test_a_matching_value_of_the_kept_parent_is_respected(tmp_path, cfg, value):
    text = pom()
    ws = make_ws(tmp_path, text)
    out = write_source_encodings(ws, cfg, capture=capture(value))
    assert out.written == [] and out.followups == [] and ws.read("pom.xml") == text


def test_a_conflicting_value_of_the_kept_parent_is_reported_not_overridden(tmp_path, cfg):
    text = pom()
    ws = make_ws(tmp_path, text)
    out = write_source_encodings(ws, cfg, capture=capture("UTF-8"))
    assert out.written == [] and ws.read("pom.xml") == text
    assert out.followups == ["pom.xml: the sources are MS949 but the parent com.example.fw:example-parent:1.0 sets "
                             "project.build.sourceEncoding=UTF-8 — set it to MS949 in pom.xml; sbmig keeps the "
                             "parent's value"]


def test_capture_without_the_property_or_another_parent_keeps_the_in_project_rule(tmp_path, cfg):
    ws = make_ws(tmp_path, pom())
    assert write_source_encodings(ws, cfg, capture=capture(None)).written == [LINE]
    ws2 = make_ws(tmp_path / "o", pom())
    assert write_source_encodings(ws2, cfg, capture=capture("UTF-8", parent="com.other:p:1")).written == [LINE]
    ws3 = make_ws(tmp_path / "n", pom(parent=False))
    assert write_source_encodings(ws3, cfg, capture=capture("UTF-8")).written == [LINE]
