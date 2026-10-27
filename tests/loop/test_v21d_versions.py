"""v2.1d dependency version policy, loop / orchestrator side (docs/INTERFACES.md "v2.1d"): the "Dependency versions"
facts reach the fix prompt (`FixContext.library_hints`), every applied try is read back for major upgrades /
library replacements (the guide: the answer's `<verify>` → one `verify_guide` LLM call → sbmig's synthesized one),
the error list's tries carry them, the structure review's commit too, and finalize turns what is still in the tree
into `report.operator_checks`. Every call into `sbmig.build.depchanges` / `repo_versions` / `jakarta_libraries` is
faked here (agent V owns them) — and a raising one must never change what the loop does."""
from __future__ import annotations

import json
import logging
from typing import ClassVar

import pytest

from sbmig.core.jakarta_libraries import JakartaLibrary
from sbmig.core.models import (
    GAV,
    BuildError,
    DependencyChange,
    ErrorCluster,
    ErrorKind,
    Phase,
    RepositoryVersions,
    ReviewIssue,
    StructureReview,
    VersionCandidate,
)
from sbmig.core.stage import install_stage_filter
from sbmig.loop import BuildFixLoop, LoopBudget
from sbmig.loop.entries import RunErrors
from sbmig.triage import Triager

from .fakes import (
    FakeBuildRunner,
    FakeEditApplier,
    FakeErrorList,
    FakeFixer,
    FakeKB,
    FakeReviewer,
    FakeScriptRunner,
    FakeVerifyFixer,
    answer,
    bad_build,
    case_of,
    journal_events,
    make_profile,
    ok_build,
    write_edit,
    write_script,
)

FOO = "src/main/java/com/x/Foo.java"
FIXED = "class X { ok }\n"
L2 = "compile|cannot_find_symbol|PageFilter"
CASE = case_of(L2)
SITEMESH2 = GAV(group_id="opensymphony", artifact_id="sitemesh", version="2.4.2")
NEW_POM = "<project><artifactId>demo</artifactId><!-- org.sitemesh:sitemesh 3.2.1 --></project>\n"
MAJOR = DependencyChange(path="pom.xml", key="org.sitemesh:sitemesh", before="2.4.2", after="3.2.1", kind="major")
ENTRY = JakartaLibrary(key="opensymphony:sitemesh", jakarta_since="3.0.0", version="2.6.0", note="same major first")


def sitemesh_error(file: str = FOO, artifact: GAV | None = SITEMESH2) -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=file, module="demo",
                      signature=f"compile|PageFilter|{file}", signature_generic=L2, artifact=artifact)


def foo_build(artifact: GAV | None = SITEMESH2):
    """Foo.java broken → one compile error (carrying `artifact`) until a fix rewrites it."""
    def fn(ws, call):
        return bad_build(sitemesh_error(artifact=artifact)) if "broken" in ws.read(FOO) else ok_build()
    return fn


def make_loop(cfg, ws, fixer, *, build=None, kb=None, errors=None, runner=None, **kw) -> BuildFixLoop:
    return BuildFixLoop(cfg, ws, FakeBuildRunner(ws, fn=build or foo_build()), Triager(cfg), fixer,
                        kb if kb is not None else FakeKB(), make_profile(), budget=LoopBudget.from_config(cfg),
                        script_runner=runner or FakeScriptRunner(), edit_applier=FakeEditApplier(),
                        errors=RunErrors(errors) if errors is not None else None, **kw)


@pytest.fixture()
def logs(caplog):
    install_stage_filter(caplog.handler)
    with caplog.at_level(logging.DEBUG, logger="sbmig"):
        yield caplog


def messages(caplog, level=logging.INFO) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= level and r.name.startswith("sbmig")]


class FakeLookup:
    """RepositoryLookup stand-in: `rv` per key (or raises `raises` from candidates); records the calls."""
    made: ClassVar[list[FakeLookup]] = []

    def __init__(self, cfg=None, *, fetch=None, rv=None, raises=None):
        self.rv, self.raises, self.asked = rv, raises, []
        FakeLookup.made.append(self)

    def candidates(self, key, current):
        self.asked.append((key, current))
        if self.raises is not None:
            raise self.raises
        return (self.rv or RepositoryVersions(key=key, current=current)).model_copy(update={"key": key,
                                                                                           "current": current})


SITEMESH_RV = RepositoryVersions(key="opensymphony:sitemesh", current="2.4.2", major=2,
                                 same_major=[VersionCandidate(version="2.5.0"),
                                             VersionCandidate(version="2.6.0", jakarta=True)],
                                 higher=[VersionCandidate(version="3.3.0", jakarta=True)])


@pytest.fixture()
def table(monkeypatch, tmp_path):
    """The library table: ENTRY for opensymphony:sitemesh (the company file at tmp_path); returns the calls."""
    calls: list[tuple] = []

    def entry_for(key, path=None, company_path=None):
        calls.append((key, company_path))
        return ENTRY if key == ENTRY.key else None

    monkeypatch.setattr("sbmig.core.jakarta_libraries.jakarta_library_for", entry_for)
    monkeypatch.setattr("sbmig.core.jakarta_libraries.company_libraries_file",
                        lambda cfg: tmp_path / "kb" / "libraries.yml", raising=False)
    monkeypatch.setattr("sbmig.core.jakarta_libraries.jakarta_libraries",
                        lambda path=None, company_path=None: (ENTRY,))
    monkeypatch.setattr("sbmig.core.jakarta_libraries.library_hint_line",
                        lambda entry, current: f"approved: raise {entry.key} {current} → {entry.version}",
                        raising=False)
    return calls


def use_lookup(monkeypatch, **kw) -> None:
    FakeLookup.made = []
    monkeypatch.setattr("sbmig.build.repo_versions.RepositoryLookup", lambda cfg, fetch=None: FakeLookup(cfg, **kw))


def fix_foo(verify: str = "", pom: bool = False):
    edits = [write_edit(FOO, FIXED)] + ([write_edit("pom.xml", NEW_POM)] if pom else [])
    return answer(edits=edits, analysis="PageFilter moved.", solution="raise sitemesh.", verify=verify)


# ------------------------------------------------------------------------------------------ (a) facts for the prompt
def test_library_hints_reach_the_prompt_and_are_logged(cfg, ws, logs, table, monkeypatch):
    use_lookup(monkeypatch, rv=SITEMESH_RV)
    seen = []
    monkeypatch.setattr("sbmig.build.repo_versions.hint_lines",
                        lambda rv, entry: seen.append((rv.key, entry)) or ["table: raise to 2.6.0",
                                                                            "same major 2.x: 2.5.0, 2.6.0 (jakarta ✓)"])
    fixer = FakeFixer(scripts=[fix_foo()])
    pr = make_loop(cfg, ws, fixer).run(Phase.VERIFY, "package", 3)
    assert pr.ok
    [ctx] = fixer.contexts
    assert ctx.library_hints == ["table: raise to 2.6.0", "same major 2.x: 2.5.0, 2.6.0 (jakarta ✓)"]
    assert seen == [("opensymphony:sitemesh", ENTRY)]
    [lookup] = FakeLookup.made
    assert lookup.asked == [("opensymphony:sitemesh", "2.4.2")]
    assert table[0][0] == "opensymphony:sitemesh" and str(table[0][1]).endswith("libraries.yml")
    out = messages(logs)
    assert "표 항목: opensymphony:sitemesh → 2.6.0 (approved)" in out
    assert ("의존성 버전 조회: opensymphony:sitemesh 2.4.2 → 같은 메이저 2.5.0, 2.6.0 (jakarta ✓) / 상위 3.3.0 "
            "(jakarta ✓)") in out
    stage = next(r.stage for r in logs.records if r.getMessage().startswith("의존성 버전 조회:"))
    assert stage == f"fix r1 {CASE}"
    [ev] = [e for e in journal_events(ws) if e["event"] == "library_hints"]
    assert ev["case"] == CASE and ev["keys"] == ["opensymphony:sitemesh"] and len(ev["lines"]) == 2


def test_a_failed_lookup_is_one_line_and_the_table_entry_still_reaches_the_prompt(cfg, ws, logs, table,
                                                                                    monkeypatch):
    use_lookup(monkeypatch, rv=RepositoryVersions(key="x", error="HTTP 404 maven-metadata.xml"))
    monkeypatch.setattr("sbmig.build.repo_versions.hint_lines", lambda rv, entry: [])
    fixer = FakeFixer(scripts=[fix_foo()])
    make_loop(cfg, ws, fixer).run(Phase.VERIFY, "package", 3)
    assert fixer.contexts[0].library_hints == ["approved: raise opensymphony:sitemesh 2.4.2 → 2.6.0"]
    assert "의존성 버전 조회 실패: opensymphony:sitemesh — HTTP 404 maven-metadata.xml" in messages(logs)


def test_lookup_off_builds_no_repository_lookup(cfg, ws, table, monkeypatch):
    cfg.libraries.lookup = False
    use_lookup(monkeypatch, rv=SITEMESH_RV)
    fixer = FakeFixer(scripts=[fix_foo()])
    assert make_loop(cfg, ws, fixer).run(Phase.VERIFY, "package", 3).ok
    assert FakeLookup.made == []
    assert fixer.contexts[0].library_hints == ["approved: raise opensymphony:sitemesh 2.4.2 → 2.6.0"]


def test_a_lookup_that_cannot_start_is_off_for_the_run_with_one_warning(cfg, ws, logs, monkeypatch):
    def boom(cfg, fetch=None):
        raise NotImplementedError("stub")

    monkeypatch.setattr("sbmig.build.repo_versions.RepositoryLookup", boom)
    monkeypatch.setattr("sbmig.core.jakarta_libraries.jakarta_library_for",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("table broken")))
    fixer = FakeFixer(scripts=[fix_foo()])
    loop = make_loop(cfg, ws, fixer)
    assert loop.run(Phase.VERIFY, "package", 3).ok
    assert fixer.contexts[0].library_hints == []
    c = ErrorCluster(id="case:other", kind=ErrorKind.COMPILE,
                     errors=[sitemesh_error(artifact=GAV(group_id="org.other", artifact_id="lib", version="1.0"))])
    assert loop._library_hints(c) == []                       # off for the rest of the run: no second warning
    warn = [m for m in messages(logs, logging.WARNING) if m.startswith("의존성 버전 조회 끔")]
    assert len(warn) == 1 and "NotImplementedError" in warn[0]


def test_a_raising_candidates_call_gives_no_hints_and_no_crash(cfg, ws, monkeypatch):
    use_lookup(monkeypatch, raises=TimeoutError("slow nexus"))
    monkeypatch.setattr("sbmig.core.jakarta_libraries.jakarta_library_for", lambda *a, **k: None)
    fixer = FakeFixer(scripts=[fix_foo()])
    assert make_loop(cfg, ws, fixer).run(Phase.VERIFY, "package", 3).ok
    assert fixer.contexts[0].library_hints == []


def test_hints_are_cached_per_library_and_capped(cfg, ws, table, monkeypatch):
    use_lookup(monkeypatch, rv=SITEMESH_RV)
    monkeypatch.setattr("sbmig.build.repo_versions.hint_lines", lambda rv, entry: [f"facts {rv.key}"])
    cfg.libraries.max_artifacts_per_case = 2
    loop = make_loop(cfg, ws, FakeFixer())
    libs = [GAV(group_id="g", artifact_id=f"a{i}", version="1.0") for i in range(4)]
    c = ErrorCluster(id="case:c", kind=ErrorKind.COMPILE,
                     errors=[sitemesh_error(file=f"src/F{i}.java", artifact=g) for i, g in enumerate(libs)]
                     + [sitemesh_error(file="src/G.java", artifact=libs[0])])
    assert loop._library_hints(c) == ["facts g:a0", "facts g:a1"]
    assert loop._library_hints(c) == ["facts g:a0", "facts g:a1"]
    [lookup] = FakeLookup.made
    assert lookup.asked == [("g:a0", "1.0"), ("g:a1", "1.0")]              # once per library and run


class FakeLocator:
    """sbmig.build.classpath stand-in: the owner class of an error and where it lives."""

    def __init__(self, where):
        self.where, self.asked = where, []

    def owner_fqcn(self, e):
        return "com.opensymphony.module.sitemesh.filter.PageFilter"

    def type_mismatch(self, e):
        return None, None

    def locate_class(self, ws, cfg, fqcn, module=None):
        self.asked.append((fqcn, module))
        return self.where.get(fqcn)


def test_the_owner_class_on_the_classpath_names_the_library(cfg, ws, table, monkeypatch):
    from sbmig.build.classpath import ClassLocation
    use_lookup(monkeypatch, rv=SITEMESH_RV)
    monkeypatch.setattr("sbmig.build.repo_versions.hint_lines", lambda rv, entry: [f"facts {rv.key} {rv.current}"])
    fq = "com.opensymphony.module.sitemesh.filter.PageFilter"
    loc = FakeLocator({fq: ClassLocation(artifact=SITEMESH2, jar="/repo/sitemesh-2.4.2.jar", fqcn=fq)})
    loop = make_loop(cfg, ws, FakeFixer(), locator=loc)
    c = ErrorCluster(id="case:c", kind=ErrorKind.COMPILE, module="demo", errors=[sitemesh_error(artifact=None)])
    assert loop._library_hints(c) == ["facts opensymphony:sitemesh 2.4.2"]
    assert loc.asked == [(fq, "demo")]
    # a project class is not a library
    loop2 = make_loop(cfg, ws, FakeFixer(),
                      locator=FakeLocator({fq: ClassLocation(in_project=True, fqcn=fq, source_file=FOO)}))
    assert loop2._library_hints(c) == []


# ------------------------------------------------------------------------------------------ (b) read-back
@pytest.fixture()
def readback(monkeypatch):
    """changes_in_commit → MAJOR for a commit that touched the pom (recorded); apply_table a no-op."""
    calls: list[tuple] = []

    def changes(ws, pre, sha):
        calls.append((pre, sha))
        return [MAJOR.model_copy()]

    monkeypatch.setattr("sbmig.build.depchanges.changes_in_commit", changes)
    monkeypatch.setattr("sbmig.build.depchanges.apply_table", lambda changes, libraries: None)
    monkeypatch.setattr("sbmig.build.depchanges.synthesized_guide",
                        lambda changes, lang="ko": f"synthesized ({lang}): check {changes[0].key}")
    return calls


def test_a_major_upgrade_is_read_back_with_the_answers_verify(cfg, ws, logs, readback):
    el = FakeErrorList()
    guide = "- web.xml 의 sitemesh 필터 등록을 확인\n- decorators.xml → sitemesh3.xml 변환을 확인"
    fixer = FakeVerifyFixer(scripts=[fix_foo(verify=guide, pom=True)], verify_guides=["never asked"])
    loop = make_loop(cfg, ws, fixer, errors=el)
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert pr.ok
    [a] = pr.attempts
    assert a.outcome == "build_ok" and a.applied                  # nothing reverted because of the major upgrade
    assert a.major_upgrades == [MAJOR] and a.verify == guide and a.verify_source == "llm"
    assert fixer.verify_calls == [] and loop.budget.llm_calls == 1
    assert readback == [(a.base_commit, a.commit)]
    # the error list's try got them (finish_try kwargs, then the FixTry itself)
    kws = [k for _a, k in el.of("finish_try")]
    assert all(k["major_upgrades"] == [MAJOR] and k["verify"] == guide for k in kws)
    [t] = el.tries_of(CASE)
    assert t.major_upgrades == [MAJOR] and t.verify == guide
    warn = messages(logs, logging.WARNING)
    assert ("메이저 업그레이드: org.sitemesh:sitemesh 2.4.2 → 3.2.1 (pom.xml) — 운영자 확인 필요") in warn
    out = messages(logs)
    assert "운영자 확인: web.xml 의 sitemesh 필터 등록을 확인" in out
    assert "운영자 확인: decorators.xml → sitemesh3.xml 변환을 확인" in out
    [ev] = [e for e in journal_events(ws) if e["event"] == "major_upgrade"]
    assert ev["attempt"] == a.id and ev["case"] == CASE and ev["guide_source"] == "llm"
    assert ev["change"]["key"] == "org.sitemesh:sitemesh" and ev["change"]["after"] == "3.2.1"


def test_without_verify_the_fixer_writes_the_guide_in_one_counted_llm_call(cfg, ws, readback):
    el = FakeErrorList()
    fixer = FakeVerifyFixer(scripts=[fix_foo(pom=True)], verify_guides=["LLM guide: check the filter"])
    loop = make_loop(cfg, ws, fixer, errors=el)
    [a] = loop.run(Phase.VERIFY, "package", 3).attempts
    assert (a.verify, a.verify_source) == ("LLM guide: check the filter", "llm")
    [(_cluster, ctx, changes, diff)] = fixer.verify_calls
    assert changes == [MAJOR] and "3.2.1" in diff and ctx.case_id == CASE and ctx.phase == Phase.VERIFY
    assert loop.budget.llm_calls == 2 and a.llm_usage.calls == 2
    assert el.tries_of(CASE)[0].llm_calls == 2


def test_an_empty_verify_guide_falls_back_to_the_synthesized_one(cfg, ws, readback):
    fixer = FakeVerifyFixer(scripts=[fix_foo(pom=True)], verify_guides=[""])
    loop = make_loop(cfg, ws, fixer)
    [a] = loop.run(Phase.VERIFY, "package", 3).attempts
    assert (a.verify, a.verify_source) == ("synthesized (en): check org.sitemesh:sitemesh", "sbmig")
    assert len(fixer.verify_calls) == 1 and loop.budget.llm_calls == 2       # the call was still made


def test_a_fixer_without_verify_guide_gets_the_synthesized_guide(cfg, ws, logs, readback):
    cfg.report.language = "ko"
    fixer = FakeFixer(scripts=[fix_foo(pom=True)])
    assert not hasattr(fixer, "verify_guide")
    loop = make_loop(cfg, ws, fixer)
    [a] = loop.run(Phase.VERIFY, "package", 3).attempts
    assert (a.verify, a.verify_source) == ("synthesized (ko): check org.sitemesh:sitemesh", "sbmig")
    assert loop.budget.llm_calls == 1
    assert "운영자 확인: synthesized (ko): check org.sitemesh:sitemesh" in messages(logs)


def test_verify_guide_llm_off_or_no_llm_budget_means_synthesized(cfg, ws, readback):
    cfg.libraries.verify_guide_llm = False
    fixer = FakeVerifyFixer(scripts=[fix_foo(pom=True)], verify_guides=["x"])
    [a] = make_loop(cfg, ws, fixer).run(Phase.VERIFY, "package", 3).attempts
    assert a.verify_source == "sbmig" and fixer.verify_calls == []


def test_a_broken_synthesized_guide_still_leaves_a_line_per_change(cfg, ws, readback, monkeypatch):
    monkeypatch.setattr("sbmig.build.depchanges.synthesized_guide",
                        lambda *a, **k: (_ for _ in ()).throw(NotImplementedError()))
    [a] = make_loop(cfg, ws, FakeFixer(scripts=[fix_foo(pom=True)])).run(Phase.VERIFY, "package", 3).attempts
    assert a.verify_source == "sbmig" and a.verify.startswith("org.sitemesh:sitemesh 2.4.2 → 3.2.1 (pom.xml): ")


def test_a_reused_stored_script_is_read_back_without_an_llm_call(cfg, ws, readback):
    kb = FakeKB()
    kb.cases.seed(CASE, write_script({FOO: FIXED, "pom.xml": NEW_POM}).code)
    fixer = FakeVerifyFixer(verify_guides=["never"])
    loop = make_loop(cfg, ws, fixer, kb=kb)
    [a] = loop.run(Phase.VERIFY, "package", 3).attempts
    assert a.source == "kb-script" and a.outcome == "build_ok"
    assert a.major_upgrades == [MAJOR] and a.verify_source == "sbmig" and fixer.verify_calls == []
    assert loop.budget.llm_calls == 0


def test_no_pom_in_the_commit_no_read_back(cfg, ws, readback):
    [a] = make_loop(cfg, ws, FakeFixer(scripts=[fix_foo()])).run(Phase.VERIFY, "package", 3).attempts
    assert readback == [] and a.major_upgrades == [] and a.verify == ""


def test_minor_changes_are_not_operator_checks_and_a_raising_read_back_changes_nothing(cfg, ws, monkeypatch):
    minor = MAJOR.model_copy(update={"kind": "minor", "after": "2.6.0"})
    monkeypatch.setattr("sbmig.build.depchanges.changes_in_commit", lambda ws, pre, sha: [minor])
    [a] = make_loop(cfg, ws, FakeFixer(scripts=[fix_foo(pom=True)])).run(Phase.VERIFY, "package", 3).attempts
    assert a.major_upgrades == [] and a.outcome == "build_ok"
    monkeypatch.setattr("sbmig.build.depchanges.changes_in_commit",
                        lambda ws, pre, sha: (_ for _ in ()).throw(NotImplementedError()))
    ws.write(FOO, "class Foo { broken }\n")
    ws.commit("break again")
    [b] = make_loop(cfg, ws, FakeFixer(scripts=[fix_foo(pom=True)])).run(Phase.VERIFY, "package", 3).attempts
    assert b.major_upgrades == [] and b.outcome == "build_ok"


def test_the_warning_names_the_approval_or_the_same_major_offer(cfg, ws, logs, table, monkeypatch):
    use_lookup(monkeypatch, rv=SITEMESH_RV)
    monkeypatch.setattr("sbmig.build.repo_versions.hint_lines", lambda rv, entry: ["facts"])
    monkeypatch.setattr("sbmig.build.depchanges.apply_table", lambda changes, libraries: None)
    monkeypatch.setattr("sbmig.build.depchanges.synthesized_guide", lambda changes, lang="ko": "g")
    upgrade = MAJOR.model_copy(update={"key": "opensymphony:sitemesh"})
    approved = MAJOR.model_copy(update={"key": "org.x:lib", "approved": True})
    monkeypatch.setattr("sbmig.build.depchanges.changes_in_commit", lambda ws, pre, sha: [upgrade, approved])
    make_loop(cfg, ws, FakeFixer(scripts=[fix_foo(pom=True)])).run(Phase.VERIFY, "package", 3)
    warn = messages(logs, logging.WARNING)
    assert ("메이저 업그레이드: opensymphony:sitemesh 2.4.2 → 3.2.1 (pom.xml) — 운영자 확인 필요 "
            "[같은 메이저 jakarta 빌드 있음: 2.6.0]") in warn
    assert "메이저 업그레이드: org.x:lib 2.4.2 → 3.2.1 (pom.xml) — 운영자 확인 필요 [승인: libraries.yml]" in warn


# ------------------------------------------------------------------------------------------ orchestrator
def _subject(ws, sha) -> str:
    return ws._git("log", "-1", "--format=%s", sha).stdout.strip()


REVIEW_CHANGE = DependencyChange(path="pom.xml", key="org.tuckey:urlrewritefilter", before="4.0.4", after="5.1.3",
                                 kind="major", section="dependencies")
KEPT = MAJOR
DROPPED = DependencyChange(path="pom.xml", key="opensymphony:oscache", before="2.4.1", after="3.0.0", kind="major")
REPLACED = DependencyChange(path="pom.xml", key="opensymphony:sitemesh", before="2.4.2", after="3.2.1",
                            kind="replaced", replaced_by="org.sitemesh:sitemesh", approved=True)


@pytest.fixture()
def orch_readback(monkeypatch):
    """The review commit → REVIEW_CHANGE; a fix commit → KEPT + DROPPED + REPLACED; still_applied drops DROPPED."""
    def changes(ws, pre, sha):
        subj = _subject(ws, sha)
        if subj.startswith("review:"):
            return [REVIEW_CHANGE.model_copy()]
        if subj.startswith("fix("):
            return [KEPT.model_copy(), DROPPED.model_copy(), REPLACED.model_copy()]
        return []

    monkeypatch.setattr("sbmig.build.depchanges.changes_in_commit", changes)
    monkeypatch.setattr("sbmig.build.depchanges.apply_table", lambda changes, libraries: None)
    monkeypatch.setattr("sbmig.build.depchanges.synthesized_guide",
                        lambda changes, lang="ko": "\n".join(f"sbmig: check {c.key}" for c in changes))
    monkeypatch.setattr("sbmig.build.depchanges.still_applied", lambda ws, change: change.key != DROPPED.key)


def _orch_fix(w, c, ctx):
    return answer(edits=[write_edit(FOO, FIXED), write_edit("pom.xml", NEW_POM)], verify="LLM: check the filter")


@pytest.mark.parametrize("engine", ["graph", "inline"])
def test_finalize_builds_operator_checks_from_the_tries_and_the_review(cfg, ws, orch_readback, engine):
    from .test_v2_orchestrator import make_v2
    cfg.loop.engine = engine
    rv = StructureReview(verdict="issues", issues=[ReviewIssue(severity="critical", file="pom.xml",
                                                               problem="old urlrewrite")])
    reviewer = FakeReviewer(review=rv, files={"pom.xml": "<project><artifactId>demo</artifactId>"
                                                         "<!-- urlrewrite 5.1.3 --></project>\n"})
    build = FakeBuildRunner(ws, fn=lambda w, call: bad_build(sitemesh_error(artifact=None))
                            if "broken" in w.read(FOO) else ok_build())
    report = make_v2(cfg, ws, reviewer=reviewer, build=build, fixer=FakeFixer(script_fn=_orch_fix)).run()
    assert report.success
    srv = report.structure_review
    assert srv.major_upgrades == [REVIEW_CHANGE] and srv.answer.verify == "sbmig: check org.tuckey:urlrewritefilter"
    checks = report.operator_checks
    assert [(c.source, c.change.key, c.kind) for c in checks] == [
        ("review", "org.tuckey:urlrewritefilter", "major_upgrade"),
        ("fix r1 try 1", "org.sitemesh:sitemesh", "major_upgrade"),
        ("fix r1 try 1", "opensymphony:sitemesh", "library_replaced")]          # DROPPED is no longer in the tree
    review_check, fix_check, replaced = checks
    assert review_check.commit == srv.commit and review_check.guide_source == "sbmig" and review_check.case_id == ""
    [att] = [a for pr in report.phases for a in pr.attempts]
    assert fix_check.commit == att.commit and fix_check.case_id == CASE
    assert fix_check.guide == "LLM: check the filter" and fix_check.guide_source == "llm"
    assert sorted(fix_check.files) == sorted([FOO, "pom.xml"]) and replaced.change.approved
    assert "major-upgrade" in report.flags
    md = (ws.run_dir / "report.md").read_text(encoding="utf-8")
    assert "### Operator checks (3 major upgrades)" in md
    assert md.index("### Operator checks") < md.index("### Versions replaced")
    assert "1. operator checks: 3 major upgrade(s) — see \"Operator checks\" above" in md
    assert " major=3" in md
    data = json.loads((ws.run_dir / "report.json").read_text(encoding="utf-8"))
    assert [c["change"]["key"] for c in data["operator_checks"]] == [c.change.key for c in checks]
    # the error list carries the try's changes (errors.jsonl), the review's try too
    entries = [json.loads(x) for x in (ws.run_dir / "errors.jsonl").read_text(encoding="utf-8").splitlines()]
    fix_tries = [t for e in entries for t in e["tries"] if t["source"] != "review"]
    assert fix_tries and all(len(t["major_upgrades"]) == 3 and t["verify"] == "LLM: check the filter"
                             for t in fix_tries)
    review_tries = [t for e in entries for t in e["tries"] if t["source"] == "review"]
    assert review_tries and review_tries[0]["major_upgrades"][0]["key"] == "org.tuckey:urlrewritefilter"
    events = [e for e in journal_events(ws) if e["event"] in ("major_upgrade", "operator_checks")]
    assert any(e["event"] == "major_upgrade" and e.get("step") == "review" for e in events)
    assert any(e["event"] == "operator_checks" and e["count"] == 3 for e in events)


def test_finalize_without_major_upgrades_has_no_checks_and_no_token(cfg, ws, monkeypatch):
    from sbmig.report.result_line import report_result_line

    from .test_v2_orchestrator import make_v2
    monkeypatch.setattr("sbmig.build.depchanges.changes_in_commit", lambda ws, pre, sha: [])
    report = make_v2(cfg, ws).run()
    assert report.operator_checks == [] and "major-upgrade" not in report.flags
    assert " major=" not in report_result_line(report)


def test_operator_checks_survive_broken_depchanges(cfg, ws, monkeypatch):
    """The stubs raising (agent V not merged / a bug): the run is exactly as before, no checks."""
    from .test_v2_orchestrator import make_v2
    for name in ("changes_in_commit", "apply_table", "still_applied", "synthesized_guide"):
        monkeypatch.setattr(f"sbmig.build.depchanges.{name}",
                            lambda *a, **k: (_ for _ in ()).throw(NotImplementedError()))
    reviewer = FakeReviewer(review=StructureReview(verdict="issues", issues=[ReviewIssue(
        severity="critical", file="pom.xml", problem="x")]), files={"pom.xml": NEW_POM})
    build = FakeBuildRunner(ws, fn=lambda w, call: bad_build(sitemesh_error(artifact=None))
                            if "broken" in w.read(FOO) else ok_build())
    report = make_v2(cfg, ws, reviewer=reviewer, build=build, fixer=FakeFixer(script_fn=_orch_fix)).run()
    assert report.success and report.operator_checks == [] and report.structure_review.major_upgrades == []
