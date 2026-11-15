"""`sbmig.restructure.scope_restore`: the as-is `provided` scope restore without the restructure context (the reduced
run of a framework project calls it after each `transform.rewrite`; `_lc_restore_scopes` delegates to it)."""
from __future__ import annotations

from sbmig.core.models import GAV, PomInfo
from sbmig.core.workspace import Workspace
from sbmig.restructure.scope_restore import (asis_provided_keys, project_pom_paths, restore_asis_scopes,
                                             scope_note)

SUCC = {"javax.servlet:javax.servlet-api": "jakarta.servlet:jakarta.servlet-api"}


def pom(*deps: str) -> str:
    return ('<project xmlns="http://maven.apache.org/POM/4.0.0">\n  <modelVersion>4.0.0</modelVersion>\n'
            "  <groupId>com.x</groupId>\n  <artifactId>demo</artifactId>\n  <version>1</version>\n"
            "  <dependencies>\n" + "".join(deps) + "  </dependencies>\n</project>\n")


def dep(g: str, a: str, v: str, scope: str | None = None) -> str:
    s = f"      <scope>{scope}</scope>\n" if scope else ""
    return (f"    <dependency>\n      <groupId>{g}</groupId>\n      <artifactId>{a}</artifactId>\n"
            f"      <version>{v}</version>\n{s}    </dependency>\n")


def asis(*deps: GAV, path: str = "pom.xml") -> PomInfo:
    return PomInfo(path=path, artifact_id="demo", dependencies=list(deps))


def make_ws(tmp_path, files: dict[str, str]) -> Workspace:
    src = tmp_path / "src" / "demo"
    for rel, text in files.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    w = Workspace.from_input(src, tmp_path / "out", run_id="t")
    w.git_init()
    return w


SERVLET_PROVIDED = GAV(group_id="javax.servlet", artifact_id="javax.servlet-api", version="3.1.0", scope="provided")


def test_jakarta_successor_gets_provided_back_and_is_idempotent(tmp_path):
    ws = make_ws(tmp_path, {"pom.xml": pom(dep("jakarta.servlet", "jakarta.servlet-api", "5.0.0", "compile"),
                                           dep("com.x", "lib", "1"))})
    out = restore_asis_scopes(ws, [asis(SERVLET_PROVIDED)], project_pom_paths(ws), SUCC)
    assert out.restored == ["jakarta.servlet:jakarta.servlet-api: compile → provided (as-is scope, pom.xml)"]
    assert out.changed == ["pom.xml"] and out.followups == []
    text = ws.read("pom.xml")
    assert "<scope>provided</scope>" in text and "<artifactId>lib</artifactId>\n      <version>1</version>\n    </dep" in text
    again = restore_asis_scopes(ws, [asis(SERVLET_PROVIDED)], project_pom_paths(ws), SUCC)
    assert again.restored == [] and again.changed == [] and ws.read("pom.xml") == text
    assert scope_note(out.restored) == ("as-is provided scopes restored after the recipes: "
                                        "jakarta.servlet:jakarta.servlet-api: compile → provided (as-is scope, pom.xml)")


def test_switch_off_writes_follow_ups_only(tmp_path):
    text = pom(dep("jakarta.servlet", "jakarta.servlet-api", "5.0.0"))
    ws = make_ws(tmp_path, {"pom.xml": text})
    out = restore_asis_scopes(ws, [asis(SERVLET_PROVIDED)], ["pom.xml"], SUCC, on=False)
    assert out.restored == [] and out.changed == [] and ws.read("pom.xml") == text
    assert out.followups == ["pom.xml: jakarta.servlet:jakarta.servlet-api is compile although the as-is declaration "
                             "was provided (layout.legacy_context.restore_asis_scopes: false) — the container provides it"]


def test_a_compile_declaration_elsewhere_wins():
    compile_ = GAV(group_id="javax.servlet", artifact_id="javax.servlet-api", version="3.1.0")
    assert asis_provided_keys([asis(SERVLET_PROVIDED), asis(compile_, path="b/pom.xml")], SUCC) == {}
    assert asis_provided_keys([asis(SERVLET_PROVIDED)], SUCC) == {
        "javax.servlet:javax.servlet-api": "pom.xml", "jakarta.servlet:jakarta.servlet-api": "pom.xml"}


def test_project_pom_paths_root_first_without_src(tmp_path):
    ws = make_ws(tmp_path, {"pom.xml": pom(), "b/pom.xml": pom(), "a/pom.xml": pom(),
                            "a/src/test/resources/pom.xml": pom()})
    paths = project_pom_paths(ws)
    assert paths[0] == "pom.xml" and sorted(paths[1:]) == ["a/pom.xml", "b/pom.xml"]
    assert project_pom_paths(ws, "a") == ["a/pom.xml"]
