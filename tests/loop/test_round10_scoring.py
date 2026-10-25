"""Round 10 (2026-10-26, review fixes A1–A3, A5): per-cluster limits count only scored failures of the SAME cluster;
"not visible" is not "resolved" (inconclusive / structural revert); partial progress; new errors in a touched file are
counted by (file, signature) occurrences.

The errors here carry real, file-less L1 signatures (`compile|cannot_find_symbol|class Gone`) — `fakes.compile_error`
puts the file into the signature, which hid the cross-file bugs."""
from __future__ import annotations

from typing import Optional

from sbmig.core.models import (GAV, BlockReason, BuildError, BuildResult, DecisionKind, ErrorKind, FileChange,
                               FixProposal, KBMatch, LLMUsage, Phase)

from .fakes import FakeBuildRunner, FakeFixer, FakeKB, FakeRewriteRunner, bad_build, journal_events, ok_build
from .test_fixloop import make_loop

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"
GONE = "compile|cannot_find_symbol|class Gone"


def l1(file: str, sig: str = GONE, msg: str = "cannot find symbol", line: int = 3,
       kind: ErrorKind = ErrorKind.COMPILE) -> BuildError:
    """A compile error with a file-less L1 signature, like the real parser produces."""
    return BuildError(kind=kind, message=msg, file=file, line=line, module="demo", symbol=sig.rsplit("|", 1)[-1],
                      signature=sig, signature_generic="|".join(sig.split("|")[:2]))


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


# ------------------------------------------------------------------------------------------------ A1
def test_same_l1_in_five_files_each_gets_its_own_llm_attempts(cfg, ws, profile):
    files = [f"src/main/java/com/x/C{i}.java" for i in range(5)]
    for f in files:
        ws.write(f, "class C { broken }\n")
    ws.commit("five files")
    calls: list[str] = []

    def build(w, call):
        errs = [l1(f) for f in files if "broken" in w.read(f)]
        return bad_build(*errs) if errs else ok_build()

    def fix(w, c, ctx):
        calls.append(c.file)
        assert ctx.previous_attempts == []          # other files' (pending) attempts are not this cluster's
        return llm([create(c.file, "class C { ok }\n")])

    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(fn=fix))
    pr = loop.run(Phase.SPRING, "compile", 3)
    assert pr.ok and not pr.blocked
    assert sorted(calls) == sorted(files)                       # all five in round 1, none blocked
    assert {a.outcome for a in pr.attempts} == {"build_ok"}
    assert [d.kind for d in pr.iteration_records[0].decisions] == [DecisionKind.LLM_FIX] * 5


def test_repeated_failure_of_the_same_cluster_still_blocks(cfg, ws, profile):
    def build(w, call):
        return bad_build(l1(FOO)) if "broken" in w.read(FOO) else ok_build()

    n = {"i": 0}

    def fix(w, c, ctx):                     # a different, still wrong patch every time → no_change
        n["i"] += 1
        return llm([create(FOO, f"class Foo {{ broken {n['i']} }}\n")])

    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(fn=fix))
    pr = loop.run(Phase.SPRING, "compile", 8)
    limit = max(cfg.loop.max_llm_attempts_per_cluster, cfg.loop.stall_limit + 1)
    assert n["i"] == limit
    assert [b.reason for b in pr.blocked] == [BlockReason.REPEATED_FAILURE]
    assert {a.outcome for a in pr.attempts} == {"no_change"}


def test_sibling_files_are_hint_material(cfg, ws, profile):
    """Foo and Bar share an L1 error and both fail in round 1. In round 2 each prompt lists only its own previous
    attempt; the sibling's attempt comes as a hint ("this run: …") — hint material, never a limit."""
    def build(w, call):
        errs = [l1(f) for f in (FOO, BAR) if "broken" in w.read(f)]
        return bad_build(*errs) if errs else ok_build()

    seen: list = []

    def fix(w, c, ctx):
        seen.append((c.file, list(ctx.kb_hints), [a.cluster_errors[0].file for a in ctx.previous_attempts]))
        n = sum(1 for s in seen if s[0] == c.file)
        return llm([create(c.file, f"class X {{ broken {n} }}\n")])

    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(fn=fix))
    loop.run(Phase.SPRING, "compile", 2)
    second = {f: (hints, prev) for f, hints, prev in seen[2:4]}
    assert set(second) == {FOO, BAR}
    for f, other in ((FOO, BAR), (BAR, FOO)):
        hints, prev = second[f]
        assert prev == [f]                                       # only its own attempt counts
        assert any(h.startswith("this run: attempt") and other in h and GONE in h and "no_change" in h
                   for h in hints), hints


# ------------------------------------------------------------------------------------------------ A2
def test_made_up_dependency_is_reverted_not_improved(cfg, ws, profile):
    """repro_mask A: the LLM 'fixes' Foo by adding a dependency that does not exist; the next build stops at
    dependency resolution and every compile error vanishes from it — reverted, never improved / harvested."""
    kb = FakeKB()

    def build(w, call):
        if "bogus-artifact" in w.read("pom.xml"):
            return bad_build(dep_error())
        errs = [l1(f) for f in (FOO, BAR) if "broken" in w.read(f)]
        return bad_build(*errs) if errs else ok_build()

    def fix(w, c, ctx):
        if c.file == FOO:
            return llm([FileChange(path="pom.xml", op="modify", search="<artifactId>demo</artifactId>",
                                   replace="<artifactId>demo</artifactId><dependencies><dependency><groupId>com.bogus"
                                           "</groupId><artifactId>bogus-artifact</artifactId><version>1.0</version>"
                                           "</dependency></dependencies>")])
        return llm([create(BAR, "class Bar { ok }\n")])

    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(fn=fix), kb=kb)
    pr = loop.run(Phase.SPRING, "compile", 2)
    first = next(a for a in pr.attempts if a.cluster_errors[0].file == FOO)
    assert first.outcome == "reverted" and first.cluster_resolved is not True
    assert "bogus-artifact" not in ws.read("pom.xml")
    assert first.introduced_errors and first.introduced_errors[0].kind == ErrorKind.DEPENDENCY_RESOLUTION
    assert "case-" + first.id not in kb.harvest(pr, profile, ws).cases_created


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

    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(fn=fix))
    pr = loop.run(Phase.SPRING, "compile", 1)
    a = next(a for a in pr.attempts if a.decision.kind == DecisionKind.LLM_FIX)
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

    fixer = FakeFixer(fn=lambda w, c, ctx: llm([create(c.file, "class X { broken still }\n")]))
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=fixer, kb=kb)
    pr = loop.run(Phase.SPRING, "compile", 1)
    first = [a for a in pr.attempts if a.iteration == 1]
    assert len(first) == 2 and {a.outcome for a in first} == {"inconclusive"}
    assert all(a.cluster_resolved is None for a in first)
    assert any("timed out" in n for n in first[0].notes)
    assert kb.harvest(pr, profile, ws).cases_created == []


def test_failed_build_without_parsed_errors_scores_inconclusive(cfg, ws, profile):
    n = {"b": 0}

    def build(w, call):
        n["b"] += 1
        if n["b"] >= 2:
            return bad_build()                      # mvn failed, nothing parseable
        return bad_build(l1(FOO))

    fixer = FakeFixer(fn=lambda w, c, ctx: llm([create(FOO, "class Foo { maybe }\n")]))
    pr = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=fixer).run(Phase.SPRING, "compile", 2)
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
    pr = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=fixer).run(Phase.SPRING, "compile", 1)
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
        if c.file == FOO:
            return llm([FileChange(path="pom.xml", op="modify", search="<artifactId>demo</artifactId>",
                                   replace="<artifactId>demo</artifactId><dependencies><dependency><groupId>com.bogus"
                                           "</groupId><artifactId>bogus-artifact</artifactId><version>1.0</version>"
                                           "</dependency></dependencies>")])
        return llm([create(BAR, "class Bar { ok }\n")])

    pr = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(fn=fix)).run(
        Phase.SPRING, "compile", 1)
    bar = next(a for a in pr.attempts if a.cluster_errors[0].file == BAR)
    foo = next(a for a in pr.attempts if a.cluster_errors[0].file == FOO)
    assert foo.outcome == "reverted"
    assert bar.outcome == "improved" and bar.cluster_resolved is True
    assert any("inconclusive" in n for n in bar.notes) and any("scored again" in n for n in bar.notes)


# ------------------------------------------------------------------------------------------------ A3
SIGS = [f"compile|cannot_find_symbol|class Gone{i}" for i in range(4)]


def test_partial_progress_is_not_a_failure(cfg, ws, profile):
    """Four targeted errors, one gone per attempt: partial, partial, partial, then build_ok — more attempts than the
    repeated-failure limit, never blocked."""
    ws.write(FOO, "class Foo { " + " ".join(f"e{i}" for i in range(4)) + " }\n")
    ws.commit("four errors")

    def build(w, call):
        text = w.read(FOO)
        errs = [l1(FOO, SIGS[i], line=3 + i) for i in range(4) if f"e{i}" in text]
        return bad_build(*errs) if errs else ok_build()

    def fix(w, c, ctx):
        text = w.read(FOO)
        first = next(i for i in range(4) if f"e{i}" in text)
        return llm([create(FOO, text.replace(f"e{first}", "ok"))])

    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(fn=fix))
    pr = loop.run(Phase.SPRING, "compile", 6)
    assert pr.ok and not pr.blocked
    assert [a.outcome for a in pr.attempts] == ["partial", "partial", "partial", "build_ok"]
    assert "partial: 1 of 4 targeted error(s) gone, 3 remain" in pr.attempts[0].notes
    assert 4 > max(cfg.loop.max_llm_attempts_per_cluster, cfg.loop.stall_limit + 1)


def test_kb_recipe_is_not_credited_for_partial_progress(cfg, ws, profile):
    ws.write(FOO, "class Foo { e0 e1 }\n")
    ws.commit("two errors")

    def build(w, call):
        text = w.read(FOO)
        errs = [l1(FOO, SIGS[i], line=3 + i) for i in range(2) if f"e{i}" in text]
        return bad_build(*errs) if errs else ok_build()

    def effect(w, refs):
        w.write(FOO, w.read(FOO).replace("e0", "ok"))
        return [FOO]

    kb = FakeKB(match=lambda c: KBMatch(recipe_ids=["kb.Half"], recipe_files=["/kb/kb.Half.yml"]))
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), kb=kb,
                     rewrite=FakeRewriteRunner(ws, effect=effect))
    pr = loop.run(Phase.SPRING, "compile", 1)
    rec = next(a for a in pr.attempts if a.decision.kind == DecisionKind.KB_RECIPE)
    assert rec.outcome == "partial"
    assert ("kb.Half", True) not in kb.credits and ("kb.Half", False) not in kb.credits
    ev = [e for e in journal_events(ws) if e["event"] == "kb_credit_skipped"]
    assert ev and ev[0]["reason"] == "partial" and ev[0]["recipes"] == ["kb.Half"]


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
        if c.file == FOO:
            return llm([create(FOO, "class Foo { WORSE }\n")])
        return llm([], analysis="nothing")

    pr = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(fn=fix)).run(
        Phase.SPRING, "compile", 1)
    foo = next(a for a in pr.attempts if a.cluster_errors[0].file == FOO)
    assert foo.outcome in ("regressed", "reverted")
    assert len(foo.introduced_errors) == 3 and all(e.file == FOO for e in foo.introduced_errors)
    assert "WORSE" not in ws.read(FOO)                          # its commit was the last one → reverted
