"""The source-encoding sampler with its total budget used up before the last module (review P1): no crash, the
module without a sample is reported as not sampled, and the modules sampled before it keep their result."""
from __future__ import annotations

import pytest

from sbmig.analyze.pom_parser import parse_poms
from sbmig.core.workspace import Workspace
from sbmig.restructure.source_encoding import write_source_encodings
from sbmig.review import inputs


def _pom(aid: str, packaging: str = "jar", modules: str = "") -> str:
    return ('<project xmlns="http://maven.apache.org/POM/4.0.0">\n  <modelVersion>4.0.0</modelVersion>\n'
            f"  <groupId>com.x</groupId>\n  <artifactId>{aid}</artifactId>\n  <version>1</version>\n"
            f"  <packaging>{packaging}</packaging>\n{modules}</project>\n")


def make_ws(tmp_path) -> Workspace:
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    mods = "".join(f"    <module>{m}</module>\n" for m in ("a", "b", "c"))
    (src / "pom.xml").write_text(_pom("demo", "pom", f"  <modules>\n{mods}  </modules>\n"))
    for m in ("a", "b", "c"):
        d = src / m / "src/main/java/com/x"
        d.mkdir(parents=True)
        (src / m / "pom.xml").write_text(_pom(m))
    for i in range(2):   # module a: MS949 sources
        (src / "a/src/main/java/com/x" / f"A{i}.java").write_bytes(
            f'package com.x;\nclass A{i} {{ String s = "안녕하세요"; }}\n'.encode("cp949"))
        (src / "b/src/main/java/com/x" / f"B{i}.java").write_text(f"package com.x;\nclass B{i} {{}}\n")
    (src / "c/src/main/java/com/x/C.java").write_text("package com.x;\nclass C {}\n")
    w = Workspace.from_input(src, tmp_path / "out", run_id="t")
    w.git_init()
    return w


@pytest.fixture
def small_budget(monkeypatch):
    monkeypatch.setattr(inputs, "ENCODING_SAMPLE_TOTAL", 4)
    monkeypatch.setattr(inputs, "ENCODING_SAMPLE_PER_MODULE", 2)


def test_spread_with_no_share_is_empty():
    assert inputs._spread(["x.java", "y.java"], 0) == []
    assert inputs._spread(["x.java"], -1) == []


def test_a_module_after_the_budget_is_not_sampled_and_the_others_keep_their_result(tmp_path, small_budget):
    ws = make_ws(tmp_path)
    poms, _w = parse_poms(ws)
    encs = {m.module: m for m in inputs.source_encodings(ws, poms)}
    assert encs["a"].counts == {"cp949": 2} and encs["a"].mismatch == ["cp949"]
    assert encs["b"].sampled == 2
    assert encs["c"].files == 1 and encs["c"].sampled == 0 and encs["c"].counts == {}
    line = encs["c"].line("the company parent")
    assert line == "- `c` (`c/pom.xml`): 1 .java file(s) not sampled (the sample limit was reached before this module)"


def test_the_ms949_module_still_gets_its_encoding_written(tmp_path, cfg, small_budget):
    ws = make_ws(tmp_path)
    out = write_source_encodings(ws, cfg)
    assert out.written == ["a/pom.xml: project.build.sourceEncoding=MS949 (2 of 2 sampled .java files cp949)"]
    assert "<project.build.sourceEncoding>MS949</project.build.sourceEncoding>" in ws.read("a/pom.xml")
    assert "sourceEncoding" not in ws.read("c/pom.xml")
