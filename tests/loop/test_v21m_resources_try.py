"""v2.1m binary files under Maven resource filtering, fix-loop side (docs/INTERFACES.md "v2.1m", "Fix loop"): a
maven-resources-plugin filtering failure is first tried WITHOUT the LLM (`sbmig-resources`: the extensions of
`binary_resource_plan(errors=…)` written by `apply_non_filtered_extensions`, committed `fix(sbmig-resources): …`),
once per (pom, extension set) per run, after the round's deferral; nothing to add → the ladder; the plan's files lead
the prompt facts when the try could not apply."""
from __future__ import annotations

from pathlib import Path

from sbmig.build.signatures import fill_signatures
from sbmig.core.models import BuildError, ErrorKind, Phase, PhaseResult
from sbmig.core.workspace import Workspace
from sbmig.loop import BuildFixLoop, LoopBudget
from sbmig.loop.fixloop import SBMIG_RESOURCES_SOURCE, _Round
from sbmig.restructure.pom_editor import PomEditor
from sbmig.triage import Triager

from .fakes import (
    FakeBuildRunner,
    FakeEditApplier,
    FakeFixer,
    FakeKB,
    answer,
    bad_build,
    journal_events,
    make_profile,
    ok_build,
)

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
NUL_BLOB = b"SQLite\x00\xff\xfe\x00\x81" * 20          # binary content, unknown extension
ITEMS = "src/main/resources/data/items.dmdb"
MESSAGE = (f"filtering {ITEMS} to target/classes/data/items.dmdb failed with MalformedInputException: "
           "Input length = 1")
POM = (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
       "  <groupId>com.x</groupId>\n  <artifactId>demo</artifactId>\n  <version>1.0.0</version>\n"
       "  <build>\n    <resources>\n      <resource>\n        <directory>src/main/resources</directory>\n"
       "        <filtering>true</filtering>\n      </resource>\n    </resources>\n  </build>\n</project>\n")
LINE = "pom.xml: dmdb → maven-resources-plugin"


def _ws(tmp_path: Path, extra: dict[str, bytes] | None = None, *, items: bool = True) -> Workspace:
    src = tmp_path / "src" / "demo"
    files: dict[str, bytes] = {"pom.xml": POM.encode(),
                               "src/main/resources/application.properties": b"v=@project.version@\n",
                               **({ITEMS: NUL_BLOB} if items else {}), **(extra or {})}
    for rel, data in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


def filtering_error(message: str = MESSAGE) -> BuildError:
    return fill_signatures(BuildError(kind=ErrorKind.PLUGIN_EXECUTION, goal="resources:resources", module="demo",
                                      message=message))


def listed(ws: Workspace, rel: str = "pom.xml") -> list[str]:
    ed = PomEditor(ws.abs(rel))
    out: list[str] = []
    for el in ed.plugin_elements("build"):
        if ed.plugin_key(el).endswith(":maven-resources-plugin"):
            conf = ed.find_in(el, "configuration/nonFilteredFileExtensions")
            out += [(x.text or "").strip() for x in ed.children(conf, "nonFilteredFileExtension")] if conf is not None \
                else []
    return out


def resources_build(always: bool = False, message: str = MESSAGE, fixed_by: str | None = None):
    """Fails with the filtering error until the pom lists `dmdb` (`always`: whatever the pom says; `fixed_by`: until
    that file says "fixed")."""
    def fn(ws, call):
        if fixed_by is not None:
            done = ws.exists(fixed_by) and "fixed" in ws.read(fixed_by)
            return ok_build() if done else bad_build(filtering_error(message))
        if always or "dmdb" not in listed(ws):
            return bad_build(filtering_error(message))
        return ok_build()
    return fn


def make_loop(cfg, ws, fixer, build) -> BuildFixLoop:
    return BuildFixLoop(cfg, ws, FakeBuildRunner(ws, fn=build), Triager(cfg), fixer, FakeKB(), make_profile(),
                        budget=LoopBudget.from_config(cfg), edit_applier=FakeEditApplier())


def subjects(ws: Workspace) -> list[str]:
    return ws._git("log", "--format=%s").stdout.splitlines()


def test_a_filtering_failure_is_fixed_without_the_llm(cfg, tmp_path):
    ws = _ws(tmp_path, {"src/main/resources/data/BLOB": b"\x00\x01\x02"})   # a binary without an extension
    fixer = FakeFixer()
    loop = make_loop(cfg, ws, fixer, resources_build())
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert pr.ok and fixer.contexts == [] and loop.budget.llm_calls == 0     # zero LLM calls
    assert listed(ws) == ["dmdb"]
    [att] = pr.attempts
    assert (att.source, att.script_mode, att.script_version, att.outcome) == (SBMIG_RESOURCES_SOURCE, "new", 0,
                                                                              "build_ok")
    assert att.llm_usage.calls == 0 and [ch.path for ch in att.changes] == ["pom.xml"]
    assert subjects(ws)[0] == f"fix(sbmig-resources): {att.cluster_id}" and len(subjects(ws)) == 2
    assert f"nonFilteredFileExtensions: {LINE}" in att.notes
    assert att.decision.rationale.startswith("sbmig-resources:")
    # the extension-less binary: a note on the try and a Handover follow-up
    assert any("BLOB" in n for n in att.notes) and any("BLOB" in f for f in loop.followups)
    [ev] = [e for e in journal_events(ws) if e["event"] == "resources_try"]
    assert ev["lines"] == [LINE] and ITEMS in ev["files"]


def test_the_same_case_is_not_tried_again(cfg, tmp_path):
    ws = _ws(tmp_path)
    fixer = FakeFixer()                                   # its answers apply nothing
    pr = make_loop(cfg, ws, fixer, resources_build(always=True)).run(Phase.VERIFY, "package", 4)
    assert not pr.ok
    tries = [a for a in pr.attempts if a.source == SBMIG_RESOURCES_SOURCE]
    assert len(tries) == 1 and tries[0].outcome == "no_change"
    assert len(fixer.contexts) >= 2                       # the ladder went on; the pre-try did not use up an LLM try
    assert all(a.source != SBMIG_RESOURCES_SOURCE for a in pr.attempts[1:])


def test_nothing_to_add_goes_to_the_ladder(cfg, tmp_path):
    props = "src/main/resources/messages.properties"
    ws = _ws(tmp_path, {props: b"k=v\n"}, items=False)   # no binary under filtering: the plan is empty
    message = f"filtering {props} to target/classes/messages.properties failed with MalformedInputException: " \
              "Input length = 1"                          # a text file: never an extension to add
    fixer = FakeFixer(scripts=[answer(edits=[{"op": "write", "path": props, "content": "k=fixed\n"}])])
    pr = make_loop(cfg, ws, fixer, resources_build(message=message, fixed_by=props)).run(Phase.VERIFY, "package", 3)
    assert pr.ok and len(fixer.contexts) == 1
    assert [a.source for a in pr.attempts] == ["llm-edit"] and listed(ws) == []
    [ev] = [e for e in journal_events(ws) if e["event"] == "resources_try"]   # looked at once, nothing to add
    assert ev["lines"] == []


def test_the_resources_try_waits_when_another_case_changed_the_pom_this_round(cfg, tmp_path):
    ws = _ws(tmp_path)
    loop = make_loop(cfg, ws, FakeFixer(), resources_build())
    build = bad_build(filtering_error())
    [c] = loop._cluster(build.errors, None)
    head = ws.head()
    st = _Round(phase=Phase.VERIFY, iteration=1, build=build, target=None, stall_level=0, others={},
                poms={"pom.xml": "case:other"})
    pr = PhaseResult(phase=Phase.VERIFY)
    assert loop._handle_cluster(pr, st, c) == "deferred"
    assert pr.attempts == [] and ws.head() == head and listed(ws) == []
    assert st.decisions[-1].rationale.startswith("deferred: its module's pom was changed by case other")
    st = _Round(phase=Phase.VERIFY, iteration=1, build=build, target=None, stall_level=0, others={})
    pr = PhaseResult(phase=Phase.VERIFY)
    assert loop._handle_cluster(pr, st, c) == "attempted"            # a deferral did not use the try up
    assert [a.source for a in pr.attempts] == [SBMIG_RESOURCES_SOURCE] and listed(ws) == ["dmdb"]


def test_when_the_try_cannot_apply_the_llm_gets_the_plans_files_in_the_same_round(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("sbmig.restructure.resources_filtering.apply_non_filtered_extensions",
                        lambda ws, plan, *, poms=None: [])     # nothing written (an unwritable pom)
    ws = _ws(tmp_path)
    fixer = FakeFixer()
    pr = make_loop(cfg, ws, fixer, resources_build()).run(Phase.VERIFY, "package", 1)
    assert [(a.source, a.outcome) for a in pr.attempts][:2] == [(SBMIG_RESOURCES_SOURCE, "apply_failed"),
                                                                ("llm-new", "apply_failed")]
    assert pr.attempts[0].iteration == pr.attempts[1].iteration
    hints = fixer.contexts[0].library_hints
    assert hints[:2] == [f"binary resources under filtering: {LINE} (nonFilteredFileExtensions)",
                         f"binary file under resource filtering: {ITEMS}"]


def test_a_compile_error_is_not_a_resources_case(cfg, tmp_path):
    ws = _ws(tmp_path)
    loop = make_loop(cfg, ws, FakeFixer(), resources_build())
    javac = BuildError(kind=ErrorKind.COMPILE, file="src/main/java/A.java", module="demo",
                       message="unmappable character (0xEB) for encoding UTF-8", goal="compiler:compile",
                       signature="compile|x", signature_generic="compile|unmappable")
    [c] = loop._cluster([javac], None)
    assert loop._resources_targets(c) is None and loop._resources_facts(c) == []
