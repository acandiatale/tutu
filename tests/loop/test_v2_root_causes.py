"""v2 LLM-call cost (2026-10-26 fixture probe): one root cause splits into many L2 cases in the same files. The cases of
a round come root causes first (`CAUSE_RANK`) and a case whose error files are all covered by a case whose script ran
earlier in the round is deferred (SKIP, no attempt) until the rebuild — never a reuse, never counted in any limit.
PLAN-v2 step 4 (e2e open problem 1): once a script of the round CHANGED a pom.xml, the later compile cases of that
pom's module (`nearest_pom`; the root pom: every module) are deferred as well."""
from __future__ import annotations

from sbmig.cli import run_exit_code
from sbmig.core.models import BlockReason, BuildError, DecisionKind, ErrorKind, Phase, ScriptProposal
from sbmig.triage import CAUSE_RANK, cause_rank, cluster_cases

from .fakes import (FakeBuildRunner, FakeFixer, FakeKB, bad_build, case_of, journal_events, ok_build, script,
                    write_script)
from .test_orchestrator import make_orch
from .test_v2_script_loop import make_loop

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"
DEP = "jakarta.xml.bind-api"


def jerr(file: str, cls: str, key: str, line: int = 1, location: str = "class com.x.Foo",
         msg: str = "") -> BuildError:
    l2 = f"compile|{cls}|{key}"
    return BuildError(kind=ErrorKind.COMPILE, file=file, line=line, module="demo", location=location,
                      message=msg or cls.replace("_", " "), signature=f"{l2}|{file}:{line}", signature_generic=l2)


def jaxb_build(ws, call):
    """pure-java: a missing jakarta.xml.bind-api → 10 cases in 2 files (2 packages + 8 symbols)."""
    if DEP in ws.read("pom.xml"):
        return ok_build()
    errs = [jerr(FOO, "package_does_not_exist", "jakarta.xml.bind"),
            jerr(BAR, "package_does_not_exist", "jakarta.xml.bind.annotation", location="class com.x.Bar")]
    for i, sym in enumerate(("JAXBContext", "JAXBException", "Marshaller", "Unmarshaller")):
        errs += [jerr(FOO, "cannot_find_symbol", sym, line=10 + i), jerr(FOO, "cannot_find_symbol", sym, line=20 + i)]
    for i, sym in enumerate(("XmlElement", "XmlRootElement", "XmlAttribute", "XmlType")):
        errs.append(jerr(BAR, "cannot_find_symbol", sym, line=10 + i, location="class com.x.Bar"))
    return bad_build(*errs)


ADD_DEP = (f"t = p.read('pom.xml')\nif {DEP!r} not in t:\n"
           f"    p.write('pom.xml', t.replace('</project>', '<dependencies><dependency>{DEP}</dependency>"
           "</dependencies></project>'))")


# ------------------------------------------------------------------------------------------ ordering
def test_root_cause_order_table():
    assert CAUSE_RANK["package_does_not_exist"] < CAUSE_RANK["cannot_access"] < CAUSE_RANK["cannot_find_symbol@import"] \
        < CAUSE_RANK["cannot_find_symbol"] < CAUSE_RANK["incompatible_types"] < CAUSE_RANK["method_does_not_override"]
    body = jerr(FOO, "cannot_find_symbol", "B", line=5)
    imp = jerr(FOO, "cannot_find_symbol", "A", line=1, location="package org.legacy")
    cs = cluster_cases([body, body.model_copy(update={"line": 6}), imp])
    assert [c.case_id for c in cs] == [case_of("compile|cannot_find_symbol|A"), case_of("compile|cannot_find_symbol|B")]
    assert [cause_rank(c) for c in cs] == [2, 3]                  # the import before the body usages, despite 2 > 1


def test_spring5_removed_class_comes_before_its_consequences(cfg, ws):
    """WebMvcConfigurerAdapter removed: the cannot_find_symbol case goes first although the override cases have more
    errors; the rest of Foo's cases wait for the rebuild."""
    adapter = "WebMvcConfigurerAdapter"

    def build(w, call):
        if adapter not in w.read(FOO):
            return ok_build()
        return bad_build(jerr(FOO, "method_does_not_override", "addInterceptors", line=20),
                         jerr(FOO, "method_does_not_override", "addInterceptors", line=30),
                         jerr(FOO, "method_does_not_override", "addInterceptors", line=40),
                         jerr(FOO, "incompatible_types", "HandlerInterceptor", line=25),
                         jerr(FOO, "cannot_find_symbol", adapter, line=5))
    ws.write(FOO, f"class Foo extends {adapter} {{ }}\n")
    ws.commit("adapter")
    fixer = FakeFixer(scripts=[write_script({FOO: "class Foo implements WebMvcConfigurer { }\n"})])
    pr = make_loop(cfg, ws, build, fixer).run(Phase.VERIFY, "package", 3)
    assert pr.ok and len(fixer.script_calls) == 1
    assert fixer.script_calls[0][0].case_id == case_of(f"compile|cannot_find_symbol|{adapter}")
    skips = [d for d in pr.iteration_records[0].decisions if d.kind == DecisionKind.SKIP]
    assert len(skips) == 2 and all(d.rationale.startswith("deferred: its files are being fixed by case ") for d in skips)


# ------------------------------------------------------------------------------------------ deferral
def test_pure_java_ten_cases_cost_one_llm_call(cfg, ws):
    """The JAXB pom script of case 1 changes the (root) pom: the other package case (Bar) and the 8 symbol cases wait
    for the rebuild — 1 LLM call (before the pom rule: 2, the second script found the pom fixed)."""
    fixer = FakeFixer(script_fn=lambda w, c, ctx: script(ADD_DEP, analysis="add jakarta.xml.bind-api"))
    loop = make_loop(cfg, ws, jaxb_build, fixer)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert pr.ok and loop.budget.llm_calls == 1
    cause = case_of("compile|package_does_not_exist|jakarta.xml.bind")
    assert [c.case_id for c, _ctx in fixer.script_calls] == [cause]
    assert [a.outcome for a in pr.attempts] == ["build_ok"]
    decisions = pr.iteration_records[0].decisions
    deferred = [d for d in decisions if d.kind == DecisionKind.SKIP and d.rationale.startswith("deferred:")]
    assert len(deferred) == 9 and len(pr.iteration_records) == 2               # the rebuild cleared the 9
    by_pom = [d for d in deferred if d.rationale.startswith("deferred: its module's pom was changed by case ")]
    by_files = [d for d in deferred if d.rationale.startswith("deferred: its files are being fixed by case ")]
    assert len(by_files) == 4 and len(by_pom) == 5                             # Foo's 4 / Bar's package + 4
    assert all(cause in d.rationale for d in deferred)
    assert any(d.cluster_id == f"case:{case_of('compile|package_does_not_exist|jakarta.xml.bind.annotation')}"
               for d in by_pom)
    ev = [e for e in journal_events(ws) if e["event"] == "decision" and e["kind"] == "skip"]
    assert len(ev) == 9 and len({d.cluster_id for d in deferred}) == 9


def test_a_reuse_is_never_deferred(cfg, ws):
    kb = FakeKB()
    sym = case_of("compile|cannot_find_symbol|JAXBContext")
    kb.scripts.seed(sym, script("p.log('stored JAXBContext fix')\n" + ADD_DEP).code)
    fixer = FakeFixer(script_fn=lambda w, c, ctx: script("p.log('looked')"))      # the LLM's scripts change nothing
    pr = make_loop(cfg, ws, jaxb_build, fixer, kb=kb).run(Phase.VERIFY, "package", 4)
    assert pr.ok
    modes = [(a.script_case_id, a.script_mode) for a in pr.attempts]
    assert (sym, "reuse") in modes                                 # Foo was covered by the package case: still ran
    assert pr.attempts[-1].script_mode == "reuse" and pr.attempts[-1].outcome == "build_ok"


# ------------------------------------------------------------------------------------------ pom deferral (modules)
MOD_A, MOD_B = "mod-a/pom.xml", "mod-b/pom.xml"
A1, A2 = "mod-a/src/main/java/a/A.java", "mod-a/src/main/java/a/A2.java"
B1 = "mod-b/src/main/java/b/B.java"


def two_modules(ws):
    ws.write("pom.xml", "<project><artifactId>demo</artifactId><modules><module>mod-a</module>"
                        "<module>mod-b</module></modules></project>\n")
    for pom in (MOD_A, MOD_B):
        ws.write(pom, f"<project><artifactId>{pom.split('/')[0]}</artifactId></project>\n")
    ws.write(A1, "class A { }\n")
    ws.write(A2, "class A2 { }\n")
    ws.write(B1, "class B { broken }\n")
    ws.commit("two modules")


def module_build(dep_pom):
    def build(w, call):
        errs = []
        if DEP not in w.read(dep_pom):
            errs += [jerr(A1, "package_does_not_exist", "jakarta.xml.bind", location="class a.A"),
                     jerr(A2, "cannot_find_symbol", "JAXBContext", location="class a.A2")]
        if "broken" in w.read(B1):
            errs.append(jerr(B1, "cannot_find_symbol", "Helper", location="class b.B"))
        return bad_build(*errs) if errs else ok_build()
    return build


def module_fixer(dep_pom):
    def fix(w, c, ctx):
        if c.case_id == case_of("compile|package_does_not_exist|jakarta.xml.bind"):
            return script(ADD_DEP.replace("'pom.xml'", repr(dep_pom)), analysis="add the API to the pom")
        return write_script({e.file: "class Fixed { }\n" for e in c.errors})
    return FakeFixer(script_fn=fix)


def test_a_module_pom_defers_its_module_not_the_other_module(cfg, ws):
    two_modules(ws)
    fixer = module_fixer(MOD_A)
    pr = make_loop(cfg, ws, module_build(MOD_A), fixer).run(Phase.VERIFY, "package", 4)
    assert pr.ok and len(pr.iteration_records) == 2
    helper, jaxb = case_of("compile|cannot_find_symbol|Helper"), case_of("compile|cannot_find_symbol|JAXBContext")
    assert [c.case_id for c, _ in fixer.script_calls] == [case_of("compile|package_does_not_exist|jakarta.xml.bind"),
                                                          helper]          # B (mod-b) in the same round
    assert {a.iteration for a in pr.attempts} == {1}
    skip = [d for d in pr.iteration_records[0].decisions if d.kind == DecisionKind.SKIP]
    assert [d.cluster_id for d in skip] == [f"case:{jaxb}"]                # A2 (mod-a, its own file) waited
    assert skip[0].rationale.startswith("deferred: its module's pom was changed by case ")


def test_a_root_pom_change_defers_every_module(cfg, ws):
    two_modules(ws)
    fixer = module_fixer("pom.xml")
    pr = make_loop(cfg, ws, module_build("pom.xml"), fixer).run(Phase.VERIFY, "package", 4)
    assert pr.ok
    r1 = pr.iteration_records[0].decisions
    assert [d.kind for d in r1].count(DecisionKind.SKIP) == 2               # A2 and B waited for the rebuild
    helper = case_of("compile|cannot_find_symbol|Helper")
    assert [(a.script_case_id, a.iteration) for a in pr.attempts][-1] == (helper, 2)   # B came back in round 2
    assert len(fixer.script_calls) == 2


def test_a_reuse_is_never_deferred_by_a_pom_change(cfg, ws):
    kb = FakeKB()
    other = case_of("compile|package_does_not_exist|jakarta.xml.bind.annotation")
    kb.scripts.seed(other, script("p.log('stored annotation fix')\n" + ADD_DEP).code)
    fixer = FakeFixer(script_fn=lambda w, c, ctx: script(ADD_DEP, analysis="add jakarta.xml.bind-api"))
    loop = make_loop(cfg, ws, jaxb_build, fixer, kb=kb)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert pr.ok and loop.budget.llm_calls == 1
    r1 = [(a.script_case_id, a.script_mode) for a in pr.attempts if a.iteration == 1]
    assert r1 == [(case_of("compile|package_does_not_exist|jakarta.xml.bind"), "new"), (other, "reuse")]
    assert pr.attempts[1].apply_errors == ["no file changed"]               # it ran (the pom was fixed already)


def test_a_deferred_case_is_attempted_in_the_next_round(cfg, ws):
    a_l2, b_l2 = "compile|package_does_not_exist|org.legacy", "compile|cannot_find_symbol|Helper"

    def build(w, call):
        text = w.read(FOO)
        errs = ([jerr(FOO, "package_does_not_exist", "org.legacy")] if "legacy" in text else []) + \
               ([jerr(FOO, "cannot_find_symbol", "Helper", line=9)] if "Helper" in text else [])
        return bad_build(*errs) if errs else ok_build()
    ws.write(FOO, "import org.legacy.*; class Foo { Helper h; }\n")
    ws.commit("foo")

    def fix(w, c, ctx):
        if c.case_id == case_of(a_l2):
            return write_script({FOO: "class Foo { Helper h; }\n"})         # fixes the import, not Helper
        return write_script({FOO: "class Foo { }\n"})
    fixer = FakeFixer(script_fn=fix)
    pr = make_loop(cfg, ws, build, fixer).run(Phase.VERIFY, "package", 4)
    assert pr.ok
    assert [c.case_id for c, _ in fixer.script_calls] == [case_of(a_l2), case_of(b_l2)]
    assert [a.iteration for a in pr.attempts] == [1, 2]
    r1 = pr.iteration_records[0].decisions
    assert [d.kind for d in r1] == [DecisionKind.SCRIPT, DecisionKind.SKIP]


def test_deferral_counts_in_no_limit_and_does_not_use_the_round_cap(cfg, ws):
    """The cause never gets fixed: its consequence (same file) is deferred every round until the cause is blocked,
    then gets a fresh ladder (`new`, no previous attempts). A deferred case does not use a slot of
    max_clusters_per_iteration: the case in another file is attempted in round 1."""
    cfg.script.max_augments_per_case = 1
    cfg.loop.max_clusters_per_iteration = 2
    BAZ = "src/main/java/com/x/Baz.java"
    ws.write(BAZ, "class Baz { broken }\n")
    ws.commit("baz")

    def build(w, call):
        errs = [jerr(FOO, "package_does_not_exist", "org.gone"), jerr(FOO, "cannot_find_symbol", "Gone", line=7)]
        if "broken" in w.read(BAZ):
            errs.append(jerr(BAZ, "incompatible_types", "x", location="class com.x.Baz"))
        return bad_build(*errs)
    n = {"i": 0}

    def fix(w, c, ctx):
        n["i"] += 1
        if c.errors[0].file == BAZ:
            return write_script({BAZ: "class Baz { ok }\n"})
        return write_script({"notes.txt": f"try {n['i']}\n"})              # changes something, fixes nothing
    fixer = FakeFixer(script_fn=fix)
    pr = make_loop(cfg, ws, build, fixer).run(Phase.VERIFY, "package", 6)
    r1 = [a for a in pr.attempts if a.iteration == 1]
    assert {a.cluster_errors[0].file for a in r1} == {FOO, BAZ}               # cap 2: the deferred case used none
    gone = case_of("compile|cannot_find_symbol|Gone")
    cause = case_of("compile|package_does_not_exist|org.gone")
    calls = [(c.case_id, ctx.script_mode, len(ctx.previous_attempts)) for c, ctx in fixer.script_calls]
    first_gone = next(i for i, x in enumerate(calls) if x[0] == gone)
    assert calls[first_gone][1:] == ("new", 0)                               # deferral was no attempt
    assert [x for x in calls[:first_gone] if x[0] == cause] == [(cause, "new", 0), (cause, "augment", 1)]
    assert {b.reason for b in pr.blocked} == {BlockReason.REPEATED_FAILURE}
    assert not any(a.script_case_id == gone and a.iteration < 3 for a in pr.attempts)


def test_file_less_cases_are_never_deferred(cfg, ws):
    pom = BuildError(kind=ErrorKind.POM_INVALID, module="demo", message="'dependencies.dependency.version' is missing",
                     signature="pom_invalid|missing_version|g:a", signature_generic="pom_invalid|missing_version")
    calls: list[str] = []

    def fix(w, c, ctx):
        calls.append(c.kind.value)
        return script("p.log('x')")
    make_loop(cfg, ws, lambda w, call: bad_build(pom, jerr(FOO, "cannot_find_symbol", "X")),
              FakeFixer(script_fn=fix)).run(Phase.VERIFY, "package", 1)
    assert calls[0] == "pom_invalid"                                           # attempted, never deferred


# ------------------------------------------------------------------------------------------ an LLM that never answers
def test_a_run_whose_llm_never_answers_ends_with_blocked_items_only(cfg, ws):
    """The fixture probe without LLM answers: every case ends BLOCKED, no exception, nothing 'unresolved', the final
    build ran (twice: before and after the loop)."""
    def build(w, call):
        errs = [jerr(f, "cannot_find_symbol", f.rsplit("/", 1)[-1], location="class X") for f in (FOO, BAR)
                if "broken" in w.read(f)]
        return bad_build(*errs) if errs else ok_build()
    fixer = FakeFixer(script_fn=lambda w, c, ctx: ScriptProposal(error="no answer", model="m"))
    b = FakeBuildRunner(ws, fn=build)
    report = make_orch(cfg, ws, build=b, fixer=fixer).run()
    assert not report.success and "aborted" not in report.flags and run_exit_code(report) == 2
    assert report.unresolved == [] and {x.reason for x in report.blocked} == {BlockReason.REPEATED_FAILURE}
    assert sorted(f.split(":")[0] for x in report.blocked for f in x.files) == sorted([BAR, FOO])
    assert [c["label"] for c in b.calls].count("verify") == 2
    assert all(a.outcome == "apply_failed" for p in report.phases for a in p.attempts)


def test_a_run_whose_llm_is_down_ends_with_llm_unavailable_items_only(cfg, ws):
    cfg.llm.cooldown_seconds = 0
    down = ScriptProposal(analysis="llm-error: LLM request failed (status=None, cause=ConnectionError: refused)",
                          error="transport", model="m")
    report = make_orch(cfg, ws, fixer=FakeFixer(script_fn=lambda w, c, ctx: down)).run()
    assert not report.success and "aborted" not in report.flags and report.unresolved == []
    assert {x.reason for x in report.blocked} == {BlockReason.LLM_UNAVAILABLE} and "infra:llm" in report.flags
