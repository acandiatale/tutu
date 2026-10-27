"""v2.1 direct edits (sbmig.script.edits): the LLM's `<edits>` → a synthesized `def fix(p)` run through the real
ScriptRunner (`python -I` child) — every op, the not-found failure, refusals, the pom-call whitelist, validation."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.config import Config
from sbmig.core.models import Edit, ScriptRunResult
from sbmig.core.workspace import Workspace
from sbmig.script import ScriptRunner, apply_edits, check_script, edits_script
from sbmig.script.api import PomHelper
from sbmig.script.edits import (EDIT_OPS, POM_CALLS, edit_error, edit_from_item, edit_problems, edits_from_json,
                                pom_call_args, pom_calls, summarize_edits)

POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.x</groupId>
  <artifactId>demo</artifactId>
  <version>1.0</version>
  <dependencies>
    <dependency>
      <groupId>javax.xml.bind</groupId>
      <artifactId>jaxb-api</artifactId>
      <version>2.3.1</version>
    </dependency>
  </dependencies>
</project>
"""


def make_cfg(**script) -> Config:
    cfg = Config(artifacts={"parent": {"group_id": "g", "artifact_id": "p"}})
    for k, v in script.items():
        setattr(cfg.script, k, v)
    return cfg


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    src = tmp_path / "src-project"
    (src / "src/main/java/com/x").mkdir(parents=True)
    (src / "pom.xml").write_text(POM)
    (src / "src/main/java/com/x/A.java").write_bytes(
        "import javax.annotation.PostConstruct;\r\n// 한글\r\nclass A {}\r\n".encode("cp949"))
    (src / "src/main/java/com/x/B.java").write_text("import javax.annotation.PreDestroy;\nclass B {}\n")
    (src / "old.txt").write_text("bye\n")
    (src / "move.txt").write_text("moving\n")
    w = Workspace.from_input(src, tmp_path / "out", run_id="edits-test")
    w.git_init()
    return w


def E(**kw) -> Edit:
    return Edit(**kw)


def test_every_op_through_the_real_runner(ws):
    cfg = make_cfg()
    edits = [
        E(op="replace", path="src/main/java/com/x/A.java", old="import javax.annotation.PostConstruct;",
          new="import jakarta.annotation.PostConstruct;"),
        E(op="write", path="src/main/java/com/x/B.java", content="import jakarta.annotation.PreDestroy;\nclass B {}\n"),
        E(op="create", path="src/main/resources/app.properties", content="a=한글\n"),
        E(op="delete", path="old.txt"),
        E(op="move", path="move.txt", to="docs/move.txt"),
        E(op="pom", path="pom.xml", pom={"add_dependency": {"group_id": "jakarta.xml.bind",
                                                            "artifact_id": "jakarta.xml.bind-api", "version": None},
                                         "remove_dependency": {"groupId": "javax.xml.bind", "artifactId": "jaxb-api"},
                                         "set_property": {"name": "java.version", "value": "21"}}),
    ]
    res = apply_edits(ws, edits, cfg=cfg)
    assert isinstance(res, ScriptRunResult) and res.status == "ok", res.error
    assert res.changed_files == sorted(["src/main/java/com/x/A.java", "src/main/java/com/x/B.java",
                                        "src/main/resources/app.properties", "old.txt", "move.txt", "docs/move.txt",
                                        "pom.xml"])
    a = ws.abs("src/main/java/com/x/A.java").read_bytes()
    assert a == "import jakarta.annotation.PostConstruct;\r\n// 한글\r\nclass A {}\r\n".encode("cp949")   # cp949 + CRLF kept
    assert ws.read("src/main/java/com/x/B.java").startswith("import jakarta.annotation.PreDestroy;")
    assert ws.abs("src/main/resources/app.properties").read_text(encoding="utf-8") == "a=한글\n"
    assert not ws.exists("old.txt") and not ws.exists("move.txt") and ws.read("docs/move.txt") == "moving\n"
    pom = ws.read("pom.xml")
    assert "<artifactId>jakarta.xml.bind-api</artifactId>" in pom and "jaxb-api" not in pom
    assert "<java.version>21</java.version>" in pom
    assert res.log[0] == "edit 1: src/main/java/com/x/A.java: replaced 1 occurrence(s)"
    assert any(ln.startswith("edit 6: pom.xml: add_dependency jakarta.xml.bind:jakarta.xml.bind-api") for ln in res.log)
    # a second run of the same edits changes nothing more (already applied / already gone / already moved)
    again = apply_edits(ws, [e for e in edits if e.op != "create"], cfg=cfg)
    assert again.status == "ok" and again.changed_files == []
    assert "edit 1: src/main/java/com/x/A.java: already applied (new text present)" in again.log
    assert "edit 3: old.txt: already gone" in again.log and "edit 4: move.txt: already moved to docs/move.txt" in again.log
    assert "edit 5: pom.xml: nothing to change (already as asked)" in again.log


def test_text_not_found_fails_and_names_the_edit_and_file(ws):
    """Not atomic: edit 1 is on disk when edit 2 fails — the run reports it in changed_files and the caller restores
    the tree (git), exactly as after a failed fix script."""
    edits = [E(op="replace", path="src/main/java/com/x/B.java", old="PreDestroy", new="PostConstruct"),
             E(op="replace", path="pom.xml", old="<artifactId>nope</artifactId>", new="<artifactId>x</artifactId>")]
    res = apply_edits(ws, edits, cfg=make_cfg())
    assert res.status == "error" and res.error == "edit 2: text not found in pom.xml"
    assert res.changed_files == ["src/main/java/com/x/B.java"]
    ws.discard_uncommitted()
    assert "PreDestroy" in ws.read("src/main/java/com/x/B.java")
    missing = apply_edits(ws, [E(op="replace", path="no/such.java", old="a", new="b")], cfg=make_cfg())
    assert missing.status == "error" and missing.error == "edit 1: file not found: no/such.java"


@pytest.mark.parametrize("edit, words", [
    (E(op="create", path="old.txt", content="other\n"), "edit 1: old.txt already exists (use op write to replace it)"),
    (E(op="move", path="nothing.txt", to="x.txt"), "edit 1: nothing to move at nothing.txt"),
    (E(op="move", path="move.txt", to="old.txt"), "edit 1: cannot move move.txt: old.txt already exists"),
    (E(op="pom", path="sub/pom.xml", pom={"set_property": {"name": "a", "value": "b"}}), "edit 1: pom not found: sub/pom.xml"),
    (E(op="write", path="target/classes/x.txt", content="x"), "edit 1: write target/classes/x.txt: path "),
])
def test_runtime_failures(ws, edit, words):
    res = apply_edits(ws, [edit], cfg=make_cfg())
    assert res.status == "error" and res.error.startswith(words), res.error
    assert "\n" not in res.error and "Traceback" not in res.error


def test_create_of_an_identical_file_is_already_there(ws):
    res = apply_edits(ws, [E(op="create", path="old.txt", content="bye\n")], cfg=make_cfg())
    assert res.status == "ok" and res.changed_files == [] and res.log == ["edit 1: old.txt: already there"]


@pytest.mark.parametrize("edit, words", [
    (E(op="replace", path="/etc/passwd", old="a", new="b"), "absolute path"),
    (E(op="replace", path="C:\\x\\A.java", old="a", new="b"), "absolute path"),
    (E(op="delete", path="a/../../up.txt"), "leaves the project folder"),
    (E(op="write", path=".git/config", content=""), "under .git/"),
    (E(op="replace", path="A.java", old="", new="b"), "exact current text"),
    (E(op="replace", path="A.java", old="same", new="same"), "changes nothing"),
    (E(op="move", path="a.txt"), "destination in 'to'"),
    (E(op="move", path="a.txt", to="../b.txt"), "to: path"),
    (E(op="pom", path="pom.xml", pom={"add_plugin": {"g": "x"}}), "unknown pom call 'add_plugin'"),
    (E(op="pom", path="pom.xml", pom={"save": {}}), "no editor call"),
    (E(op="pom", path="pom.xml", pom={"add_dependency": {"group_id": "g"}}), "'artifact_id' is missing"),
    (E(op="pom", path="pom.xml", pom={"add_dependency": {"group_id": "g", "artifact_id": "a", "colour": "x"}}),
     "unknown argument 'colour'"),
    (E(op="pom", path="src/A.java", pom={"set_property": {"name": "a", "value": "b"}}), "pom.xml path"),
    (E(op="pom", path="pom.xml", pom={"set_property": {"name": "a"}}), "'value' is missing"),
])
def test_refused_before_running(ws, edit, words):
    probs = edit_problems([edit])
    assert probs and probs[0].startswith("edit 1: ") and words in probs[0], probs
    res = apply_edits(ws, [edit], cfg=make_cfg())
    assert res.status == "rejected" and words in res.error and res.problems == probs
    assert not ws.is_dirty()
    with pytest.raises(ValueError):
        edits_script([edit])


def test_pom_call_whitelist_matches_the_editor():
    for name in POM_CALLS:
        assert callable(getattr(PomHelper, name, None)), name
    for read_only in ("text", "save", "dependencies", "has_dependency", "get_property"):
        assert read_only not in POM_CALLS
    assert set(EDIT_OPS) == {"replace", "write", "create", "delete", "move", "pom"}


def test_pom_argument_spellings():
    kw = {"g": "g1", "a": "a1", "version": "1.0"}
    assert pom_call_args("add_dependency", {"groupId": "g1", "artifactId": "a1", "version": "1.0"}) == ([kw], [])
    assert pom_call_args("add_dependency", {"group_id": "g1", "artifact_id": "a1", "version": "1.0"}) == ([kw], [])
    assert pom_call_args("add_dependency", "g1:a1:1.0") == ([kw], [])
    assert pom_call_args("add_dependency", ["g1", "a1", "1.0"]) == ([kw], [])
    two, probs = pom_call_args("add_dependency", [{"g": "g1", "a": "a1"}, {"g": "g2", "a": "a2", "scope": "test"}])
    assert probs == [] and [k["a"] for k in two] == ["a1", "a2"] and two[1]["scope"] == "test"
    excl, _ = pom_call_args("add_exclusion", {"group_id": "g", "artifact_id": "a",
                                              "exclusion": {"group_id": "eg", "artifact_id": "ea"}})
    assert excl == [{"g": "g", "a": "a", "ex_g": "eg", "ex_a": "ea"}]
    assert pom_call_args("set_dependency_version", {"group_id": "g", "artifact_id": "a"})[0] == \
        [{"g": "g", "a": "a", "version": None}]                       # absent / null = drop <version>
    assert pom_call_args("set_plugin_version", {"group_id": "", "artifact_id": "maven-war-plugin",
                                                "version": "3.4.0"})[1] == []      # "" = org.apache.maven.plugins
    assert pom_call_args("remove_property", "old.prop") == ([{"key": "old.prop"}], [])
    assert pom_call_args("set_property", {"name": "flag", "value": True})[0] == [{"key": "flag", "value": "true"}]
    calls, _ = pom_calls({"call": "remove_dependency", "args": {"group_id": "g", "artifact_id": "a"}})
    assert calls == [("remove_dependency", {"g": "g", "a": "a"})]


def test_edits_from_json_is_lenient_about_keys_and_strict_about_ops():
    edits, probs = edits_from_json([
        {"op": "REPLACE", "file": "./a/B.java", "search": "x", "replace": "y", "count": "1"},
        {"action": "move", "path": "a\\c.txt", "dst": "d.txt"},
        {"op": "pom", "path": "pom.xml", "add_dependency": {"group_id": "g", "artifact_id": "a"}, "reason": "why"},
        {"op": "create", "path": "n.txt"},
    ])
    assert probs == []
    assert edits[0] == Edit(op="replace", path="a/B.java", old="x", new="y", count=1)
    assert edits[1].to == "d.txt" and edits[1].path == "a/c.txt"
    assert edits[2].pom == {"add_dependency": {"group_id": "g", "artifact_id": "a"}}
    assert edits[3].content == ""
    assert edits_from_json({"edits": [{"op": "delete", "path": "x"}]})[0] == [Edit(op="delete", path="x")]
    assert edits_from_json({"op": "delete", "path": "x"})[0] == [Edit(op="delete", path="x")]
    bad, probs = edits_from_json([{"op": "delete", "path": "ok"}, {"op": "patch", "path": "x"}, "text",
                                  {"path": "x"}, {"op": "write", "path": "w"}])
    assert bad == []                                                  # one bad item refuses the whole list
    assert probs == ["edit 2: unknown op 'patch' (allowed: replace, write, create, delete, move, pom)",
                     "edit 3: an edit must be an object, got str",
                     "edit 4: no 'op' (one of: replace, write, create, delete, move, pom)",
                     "edit 5: write needs the complete new text in 'content'"]
    assert edits_from_json("nope")[1] == ["<edits> must be a JSON list of edit objects, got str"]
    e, probs = edit_from_item({"op": "replace", "path": "a", "old": "x"})
    assert e is None and "'new'" in probs[0]


def test_synthesized_script_passes_the_static_check_and_embeds_literals():
    tricky = "a \"quoted\" 'text' with {braces} \\ backslash, 한글, tab\t and\nnewline"
    edits = [E(op="replace", path="d/{x}.txt", old=tricky, new=tricky + "!"),
             E(op="write", path="w.txt", content="x" * 40000),
             E(op="pom", path="pom.xml", pom={"add_dependency": {"group_id": "g'\"", "artifact_id": "a"}})]
    code = edits_script(edits)
    assert check_script(code, max_chars=0) == []
    assert repr(tricky) in code and "def fix(p):" in code and "# edit 1: replace d/{x}.txt" in code
    assert check_script(code) != []                       # longer than script.max_code_chars …
    assert edits_script(edits) == code                    # deterministic
    assert summarize_edits(edits) == "replace d/{x}.txt; write w.txt; pom pom.xml add_dependency"


def test_long_edits_run_without_the_code_size_cap(ws):
    res = apply_edits(ws, [E(op="write", path="big.txt", content="line\n" * 8000)], cfg=make_cfg())
    assert res.status == "ok" and res.changed_files == ["big.txt"] and ws.read("big.txt").count("line") == 8000


def test_an_injected_runner_is_used(ws):
    class Fake:
        def __init__(self):
            self.codes: list[str] = []

        def run(self, ws, code, *, errors=(), timeout_s=None):
            self.codes.append(code)
            return ScriptRunResult(status="error", error="EditFailed: edit 1: text not found in a (line 9)\n"
                                                         "script traceback:\n  line 9, in fail: raise …")

    fake = Fake()
    res = apply_edits(ws, [E(op="replace", path="a", old="x", new="y")], cfg=make_cfg(), runner=fake)
    assert fake.codes and "def fix(p):" in fake.codes[0] and res.error == "edit 1: text not found in a"
    assert apply_edits(ws, [], cfg=make_cfg(), runner=fake).status == "ok" and len(fake.codes) == 1


def test_edit_error_keeps_other_errors():
    assert edit_error("timed out after 60s (process tree killed)") == "timed out after 60s (process tree killed)"
    assert edit_error("EditFailed: edit 3: pom not found: x/pom.xml") == "edit 3: pom not found: x/pom.xml"
    assert edit_error("") == ""


def test_runner_logs_edits_as_edits(ws, caplog):
    caplog.set_level("INFO", logger="sbmig.script")
    apply_edits(ws, [E(op="delete", path="old.txt")], cfg=make_cfg())
    assert any(r.getMessage().startswith("edits ok in ") for r in caplog.records)
    assert ScriptRunner.label == "fix script"
