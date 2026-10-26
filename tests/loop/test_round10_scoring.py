"""Round 10 (2026-10-26, review fixes A1–A3, A5) under v2 fix scripts: one case across files gets one script; the
per-case limit counts only that case's scored attempts; "not visible" is not "resolved" (inconclusive / structural
revert); partial progress (v2: rolled back, so each augment is a complete script); new errors in a touched file are
counted by (file, signature) occurrences.

The errors here carry real, file-less L1 signatures (`compile|cannot_find_symbol|class Gone`) — `fakes.compile_error`
puts the file into the signature, which hid the cross-file bugs. Their L2 (the case) is per file unless a test passes
one shared L2."""
from __future__ import annotations

from typing import Optional

from sbmig.core.models import (GAV, BlockReason, BuildError, BuildResult, DecisionKind, ErrorKind, FileChange,
                               FixProposal, LLMUsage, Phase)

from .fakes import FakeBuildRunner, FakeFixer, FakeKB, bad_build, ok_build, write_script
from .test_fixloop import make_loop

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"
GONE = "compile|cannot_find_symbol|class Gone"


def l1(file: str, sig: str = GONE, msg: str = "cannot find symbol", line: int = 3,
       kind: ErrorKind = ErrorKind.COMPILE, l2: Optional[str] = None) -> BuildError:
    """A compile error with a file-less L1 signature, like the real parser produces; L2 (the case) per file unless
    given."""
    stem = file.rsplit("/", 1)[-1]
    return BuildError(kind=kind, message=msg, file=file, line=line, module="demo", symbol=sig.rsplit("|", 1)[-1],
                      signature=sig, signature_generic=l2 or ("|".join(sig.split("|")[:2]) + f"|{stem}"))


def dep_error(artifact: str = "com.bogus:bogus-artifact:1.0", module: str = "demo",
              msg: Optional[str] = None) -> BuildError:
    gav = GAV.parse(artifact)
    return BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, module=module, artifact=gav,
                      message=msg or f"Could not find artifact {gav.group_id}:{gav.artifact_id}:jar:{gav.version}",
                      signature=f"dependency_resolution|{gav.key}", signature_generic="dependency_resolution")


def llm(changes: list[FileChange], analysis: str = "fix") -> FixProposal:
    return FixProposal(changes=changes, analysis=analysis, model="m", llm_usage=LLMUsage(model="m", calls=1))


def create(path: str, content: str) -> FileChange:
    return FileChange(path=path, op="create", content=content)


def first_file(c) -> str:
    return c.errors[0].file


# ------------------------------------------------------------------------------------------------ A1 (v2: one case)
def test_same_l1_in_five_files_is_one_case_and_one_script(cfg, ws, profile):
    files = [f"src/main/java/com/x/C{i}.java" for i in range(5)]
    for f in files:
        ws.write(f, "class C { broken }\n")
    ws.commit("five files")
    calls: list = []

    def build(w, call):
        errs = [l1(f, l2="compile|cannot_find_symbol|Gone") for f in files if "broken" in w.read(f)]
        return bad_build(*errs) if errs else ok_build()

    def fix(w, c, ctx):
        calls.append(sorted(e.file for e in c.errors))
        return write_script({e.file: "class C { ok }\n" for e in c.errors})

    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(script_fn=fix))
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert pr.ok and not pr.blocked
    assert calls == [sorted(files)]                              # one LLM call for the five files
    assert [a.outcome for a in pr.attempts] == ["build_ok"]
    assert [d.kind for d in pr.iteration_records[0].decisions] == [DecisionKind.SCRIPT]


def test_repeated_failure_of_the_same_case_still_blocks(cfg, ws, profile):
    def build(w, call):
        return bad_build(l1(FOO)) if "broken" in w.read(FOO) else ok_build()

    n = {"i": 0}

    def fix(w, c, ctx):                     # a different, still wrong script every time → no_change
        n["i"] += 1
        return llm([create(FOO, f"class Foo {{ broken {n['i']} }}\n")])

    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(fn=fix))
    pr = loop.run(Phase.VERIFY, "package", 8)
    assert n["i"] == 1 + cfg.script.max_augments_per_case       # new + the augments, then BLOCKED
    assert [b.reason for b in pr.blocked] == [BlockReason.REPEATED_FAILURE]
    assert {a.outcome for a in pr.attempts} == {"no_change"}


def test_the_same_case_in_another_service_is_hint_material(cfg, ws, profile):
    """MSA: svc-a's loop tried the case; svc-b's loop (its own scope) gets that attempt as a hint ("this run: …"),
    never as one of its own previous attempts."""
    A, B = "svc-a/src/A.java", "svc-b/src/B.java"
    ws.write(A, "class A { broken }\n")
    ws.write(B, "class B { broken }\n")
    ws.commit("services")
    l2 = "compile|cannot_find_symbol|Gone"

    def build(w, call):
        files = [f for f in (A, B) if f.startswith(f"{call['module']}/")] if call["module"] else [A, B]
        errs = [l1(f, l2=l2) for f in files if "broken" in w.read(f)]
        return bad_build(*errs) if errs else ok_build()

    seen: list = []

    def fix(w, c, ctx):
        seen.append((c.id, list(ctx.kb_hints), [a.id for a in ctx.previous_attempts]))
        return write_script({e.file: "class X { broken still }\n" for e in c.errors})
    cfg.script.max_augments_per_case = 1
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(script_fn=fix))
    loop.run(Phase.VERIFY, "package", 3, target="svc-a")
    loop.run(Phase.VERIFY, "package", 3, target="svc-b")
    (a_id, _a_hints, _), _a2, (b_id, b_hints, b_prev) = seen[:3]
    assert a_id.endswith("@svc-a") and b_id.endswith("@svc-b") and b_prev == []
    assert any(h.startswith("this run: attempt") and A in h and GONE in h and "no_change" in h
               for h in b_hints), b_hints


# ------------------------------------------------------------------------------------------------ A2
def test_made_up_dependency_is_reverted_not_improved(cfg, ws, profile):
    """repro_mask A: the LLM 'fixes' Foo by adding a dependency that does not exist; the next build stops at
    dependency resolution and every compile error vanishes from it — reverted, never improved / credited a success."""
    kb = FakeKB()

    def build(w, call):
        if "bogus-artifact" in w.read("pom.xml"):
            return bad_build(dep_error())
        errs = [l1(f) for f in (FOO, BAR) if "broken" in w.read(f)]
        return bad_build(*errs) if errs else ok_build()

    def fix(w, c, ctx):
        if first_file(c) == FOO:
            return llm([FileChange(path="pom.xml", op="modify", search="<artifactId>demo</artifactId>",
                                   replace="<artifactId>demo</artifactId><dependencies><dependency><groupId>com.bogus"
                                           "</groupId><artifactId>bogus-artifact</artifactId><version>1.0</version>"
                                           "</dependency></dependencies>")])
        return llm([create(BAR, "class Bar { ok }\n")])

    cfg.script.max_augments_per_case = 0
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(fn=fix), kb=kb)
    pr = loop.run(Phase.VERIFY, "package", 2)
    first = next(a for a in pr.attempts if a.cluster_errors[0].file == FOO)
    assert first.outcome == "reverted" and first.cluster_resolved is not True
    assert "bogus-artifact" not in ws.read("pom.xml")
    assert first.introduced_errors and first.introduced_errors[0].kind == ErrorKind.DEPENDENCY_RESOLUTION
    assert (first.script_case_id, first.script_version, "reverted") in [c[:3] for c in kb.scripts.credits]


def test_new_dependency_error_of_another_artifact_after_a_pom_edit_is_reverted(cfg, ws, profile):
    """A dependency_resolution error existed before (another module): the kind is not new, but the attempt's pom
    edit added a NEW earlier-stage error that hides its targeted compile error → structural regression."""
    def build(w, call):
        errs = [dep_error("com.other:missing:2.0", module="other")]
        if "bogus-artifact" in w.read("pom.xml"):
            return bad_build(*errs, dep_error())
        if "broken" in w.read(FOO):
            errs.append(l1(FOO))
        return bad_build(*errs)

    def fix(w, c, ctx):
        return llm([FileChange(path="pom.xml", op="modify", search="<artifactId>demo</artifactId>",
                               replace="<artifactId>demo</artifactId><!-- com.bogus:bogus-artifact -->")])

    cfg.loop.blocked_policy["artifact_not_found"] = 0     # the other module's missing artifact stays blocked (v2: 2)
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(fn=fix))
    pr = loop.run(Phase.VERIFY, "package", 1)
    a = next(a for a in pr.attempts if a.decision.kind == DecisionKind.SCRIPT)
    assert a.outcome == "reverted"
    assert any("hide the targeted errors" in n for n in a.notes)
    assert "bogus-artifact" not in ws.read("pom.xml")


def test_timed_out_build_scores_inconclusive(cfg, ws, profile):
    """repro_timeout: the build after a fix round is killed by build.timeout_minutes — nothing is 'improved'."""
    kb = FakeKB()
    n = {"b": 0}

    def build(w, call):
        n["b"] += 1
        if n["b"] == 2:
            return BuildResult(ok=False, goal="compile", command=["mvn"], exit_code=-1, duration_s=1,
                               log_path="/tmp/x", timed_out=True,
                               errors=[BuildError(kind=ErrorKind.UNKNOWN,
                                                  message="build timed out after 30.0 minutes (goal compile)")])
        errs = [l1(f) for f in (FOO, BAR) if "broken" in w.read(f)]
        return bad_build(*errs) if errs else ok_build()

    fixer = FakeFixer(fn=lambda w, c, ctx: llm([create(first_file(c), "class X { broken still }\n")]))
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=fixer, kb=kb)
    pr = loop.run(Phase.VERIFY, "package", 1)
    first = [a for a in pr.attempts if a.iteration == 1]
    assert len(first) == 2 and {a.outcome for a in first} == {"inconclusive"}
    assert all(a.cluster_resolved is None for a in first)
    assert any("timed out" in n for n in first[0].notes)
    assert {c[2] for c in kb.scripts.credits} <= {"inconclusive"}     # no success, no failure


def test_failed_build_without_parsed_errors_scores_inconclusive(cfg, ws, profile):
    n = {"b": 0}

    def build(w, call):
        n["b"] += 1
        if n["b"] >= 2:
            return bad_build()                      # mvn failed, nothing parseable
        return bad_build(l1(FOO))

    fixer = FakeFixer(fn=lambda w, c, ctx: llm([create(FOO, "class Foo { maybe }\n")]))
    pr = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=fixer).run(Phase.VERIFY, "package", 2)
    assert pr.attempts[0].outcome == "inconclusive"
    assert any("without parseable errors" in n for n in pr.attempts[0].notes)


def test_earlier_stage_the_attempt_did_not_cause_is_inconclusive(cfg, ws, profile):
    """A Java-only attempt, then the build stops at dependency resolution (a repository flake): unknown, kept."""
    n = {"b": 0}

    def build(w, call):
        n["b"] += 1
        if n["b"] == 2:
            return bad_build(dep_error("org.x:lib:1.0", msg="Could not transfer artifact org.x:lib:pom:1.0 from/to "
                                                            "nexus: Connection refused"))
        return bad_build(l1(FOO)) if "broken" in w.read(FOO) else ok_build()

    fixer = FakeFixer(fn=lambda w, c, ctx: llm([create(FOO, "class Foo { ok }\n")]))
    pr = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=fixer).run(Phase.VERIFY, "package", 1)
    a = pr.attempts[0]
    assert a.outcome == "inconclusive" and a.cluster_resolved is None
    assert any("stopped before the stage" in n for n in a.notes)
    assert ws.read(FOO) == "class Foo { ok }\n"                 # not reverted


def test_inconclusive_attempt_is_scored_again_after_a_revert(cfg, ws, profile):
    """One round: a pom edit (Foo's cluster) adds a bogus dependency, a Java edit fixes Bar. The bogus dependency
    hides Bar's error → Bar inconclusive; after the structural revert the rebuild shows Bar fixed → improved."""
    def build(w, call):
        if "bogus-artifact" in w.read("pom.xml"):
            return bad_build(dep_error())
        errs = [l1(f) for f in (FOO, BAR) if "broken" in w.read(f)]
        return bad_build(*errs) if errs else ok_build()

    def fix(w, c, ctx):
        if first_file(c) == FOO:
            return llm([FileChange(path="pom.xml", op="modify", search="<artifactId>demo</artifactId>",
                                   replace="<artifactId>demo</artifactId><dependencies><dependency><groupId>com.bogus"
                                           "</groupId><artifactId>bogus-artifact</artifactId><version>1.0</version>"
                                           "</dependency></dependencies>")])
        return llm([create(BAR, "class Bar { ok }\n")])

    pr = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(fn=fix)).run(
        Phase.VERIFY, "package", 1)
    bar = next(a for a in pr.attempts if a.cluster_errors[0].file == BAR)
    foo = next(a for a in pr.attempts if a.cluster_errors[0].file == FOO)
    assert foo.outcome == "reverted"
    assert bar.outcome == "improved" and bar.cluster_resolved is True
    assert any("inconclusive" in n for n in bar.notes) and any("scored again" in n for n in bar.notes)


# ------------------------------------------------------------------------------------------------ A3
SIGS = [f"compile|cannot_find_symbol|class Gone{i}" for i in range(4)]


def test_partial_progress_is_not_a_failure(cfg, ws, profile):
    """Four targeted errors of one case; each attempt (rolled back before the next: a complete script every time)
    fixes one more: partial, partial, partial, then build_ok — new + max_augments_per_case attempts, never blocked,
    each version credited as it scored."""
    ws.write(FOO, "class Foo { " + " ".join(f"e{i}" for i in range(4)) + " }\n")
    ws.commit("four errors")
    kb = FakeKB()

    def build(w, call):
        text = w.read(FOO)
        errs = [l1(FOO, SIGS[i], line=3 + i, l2="compile|cannot_find_symbol|Gone") for i in range(4)
                if f"e{i}" in text]
        return bad_build(*errs) if errs else ok_build()

    n = {"k": 0}

    def fix(w, c, ctx):
        n["k"] += 1
        text = w.read(FOO)
        assert all(f"e{i}" in text for i in range(4))           # the previous partial attempt was rolled back
        for i in range(n["k"]):
            text = text.replace(f"e{i}", "ok")
        return write_script({FOO: text})

    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(script_fn=fix), kb=kb)
    pr = loop.run(Phase.VERIFY, "package", 6)
    assert pr.ok and not pr.blocked
    assert [a.outcome for a in pr.attempts] == ["partial", "partial", "partial", "build_ok"]
    assert "partial: 1 of 4 targeted error(s) gone, 3 remain" in pr.attempts[0].notes
    assert "partial: 2 of 4 targeted error(s) gone, 2 remain" in pr.attempts[1].notes
    assert len(pr.attempts) == 1 + cfg.script.max_augments_per_case
    assert [c[2] for c in kb.scripts.credits] == ["partial", "partial", "partial", "build_ok"]


# ------------------------------------------------------------------------------------------------ A5
def test_new_errors_in_a_touched_file_count_by_file_and_signature(cfg, ws, profile):
    """repro_b2: Foo's attempt keeps its own error and adds three `class Gone` errors — an L1 that Bar already had.
    By signature alone they were not new (no_change); by (file, signature) they are → regressed."""
    def build(w, call):
        errs = []
        if "broken" in w.read(BAR):
            errs.append(l1(BAR))
        foo = w.read(FOO)
        if "broken" in foo or "WORSE" in foo:
            errs.append(l1(FOO, "compile|incompatible_types|-", msg="incompatible types"))
        if "WORSE" in foo:
            errs += [l1(FOO, line=10 + i) for i in range(3)]
        return bad_build(*errs) if errs else ok_build()

    def fix(w, c, ctx):
        if first_file(c) == FOO:
            return llm([create(FOO, "class Foo { WORSE }\n")])
        return llm([], analysis="nothing")

    pr = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(fn=fix)).run(
        Phase.VERIFY, "package", 1)
    foo = next(a for a in pr.attempts if a.cluster_errors[0].file == FOO)
    assert foo.outcome in ("regressed", "reverted")
    assert len(foo.introduced_errors) == 3 and all(e.file == FOO for e in foo.introduced_errors)
    assert "WORSE" not in ws.read(FOO)                          # its commit was the last one → reverted
