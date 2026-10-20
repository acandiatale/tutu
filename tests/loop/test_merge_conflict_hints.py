"""Round 9: first-wins module-merge conflicts become fix-prompt hints — a note per relevant conflict and the dropped
copy as read-only reference text (FixContext.reference_notes / reference_files), never a patch target."""
from __future__ import annotations

from sbmig.core.interfaces import FixContext
from sbmig.core.models import (
    BuildError,
    ErrorCluster,
    ErrorKind,
    FileChange,
    FixProposal,
    LLMUsage,
    MergeConflict,
    Phase,
)
from sbmig.fix import ContextPacker
from sbmig.fix.fixer import LLMFixer
from sbmig.loop import BuildFixLoop, LoopBudget
from sbmig.triage import HandlerRegistry, Triager

from .fakes import FakeBuildRunner, FakeFixer, FakeKB, FakeRewriteRunner, bad_build, journal_events, ok_build

KEPT = "module/comp-jar/src/main/java/com/x/Shared.java"
USER = "module/comp-jar/src/main/java/com/x/Foo.java"
SAVED = "merge-conflicts/web/src/main/java/com/x/Shared.java"
KEPT_SRC = "package com.x;\npublic class Shared {\n    public void a() {}\n}\n"
DROPPED_SRC = "package com.x;\npublic class Shared {\n    public void a() {}\n    public int extra() { return 1; }\n}\n"
REF_TITLE = "Reference files (read-only, NOT in the build — never write SEARCH/REPLACE blocks for these paths)"
NOTES_TITLE = "Module merge notes (first-wins; the dropped copy is below as reference)"


def conflict(**kw) -> MergeConflict:
    base = {"path": "src/main/java/com/x/Shared.java", "kept_from": "core", "dropped_from": ["web"], "saved_copies": [SAVED]}
    base.update(kw)
    return MergeConflict(**base)


def err(file: str, message: str = "cannot find symbol", symbol: str | None = None, location: str | None = None,
        sig: str = "s1") -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message=message, file=file, line=3, symbol=symbol, location=location,
                      module="comp-jar", signature=sig, signature_generic="compile|x")


def setup_tree(ws, dropped: str = DROPPED_SRC) -> None:
    ws.write(KEPT, KEPT_SRC)
    ws.write(USER, "package com.x;\nclass Foo { int x = new Shared().extra(); }\n")
    ws.commit("restructured")
    p = ws.run_dir / SAVED
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dropped, encoding="utf-8")


def make_loop(cfg, ws, profile, *, build, fixer, conflicts):
    t = Triager(cfg, registry=HandlerRegistry())
    loop = BuildFixLoop(cfg, ws, build, t, t.registry, fixer, FakeKB(), FakeRewriteRunner(ws), profile,
                        budget=LoopBudget.from_config(cfg))
    assert loop.merge_conflicts == []                                 # default: no hints
    loop.merge_conflicts = list(conflicts)
    return loop


def run_once(cfg, ws, profile, errors, conflicts, proposal=None):
    fixer = FakeFixer(proposals=[proposal] if proposal else None)
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, script=[bad_build(*errors)]), fixer=fixer,
                     conflicts=conflicts)
    pr = loop.run(Phase.RESTRUCTURE, "package", 1)
    return fixer, pr


def test_hint_by_file(cfg, ws, profile):
    setup_tree(ws)
    fixer, pr = run_once(cfg, ws, profile, [err("./" + KEPT, message="incompatible types")], [conflict()])
    ctx = fixer.contexts[0]
    assert len(ctx.reference_notes) == 1
    note = ctx.reference_notes[0]
    assert note.startswith(f"{KEPT} was merged first-wins from module `core`; the copy from `web` was dropped")
    assert "do not re-create a second module" in note
    assert ctx.reference_files == {SAVED: DROPPED_SRC}
    assert f"merge-conflict hint: {KEPT}" in pr.attempts[0].notes
    ev = [e for e in journal_events(ws) if e["event"] == "merge_conflict_hints"]
    assert ev and ev[0]["reference_files"] == [SAVED]


def test_hint_by_symbol_backslash_paths(cfg, ws, profile):
    setup_tree(ws)
    e = err(USER.replace("/", "\\"), symbol="method extra()", location="class com.x.Shared")
    fixer, _pr = run_once(cfg, ws, profile, [e], [conflict()])
    ctx = fixer.contexts[0]
    assert len(ctx.reference_notes) == 1 and SAVED in ctx.reference_files
    e2 = err(USER, message="method a(int) in class Shared cannot be applied to given types;", sig="s2")
    fixer, _ = run_once(cfg, ws, profile, [e2], [conflict()])
    assert fixer.contexts[0].reference_notes


def test_unrelated_identical_and_union_conflicts_are_ignored(cfg, ws, profile):
    setup_tree(ws)
    unrelated = err(USER, symbol="class SharedThing", location="class com.x.Foo")      # SharedThing ≠ Shared
    fixer, pr = run_once(cfg, ws, profile, [unrelated], [conflict()])
    assert fixer.contexts[0].reference_notes == [] and fixer.contexts[0].reference_files == {}
    assert not any(n.startswith("merge-conflict hint") for n in pr.attempts[0].notes)
    on_file = err(KEPT)
    for c in (conflict(identical=True, saved_copies=[]), conflict(identical=True),
              conflict(union=True, path="src/main/resources/META-INF/services/x.Y")):
        fixer, _ = run_once(cfg, ws, profile, [on_file], [c])
        assert fixer.contexts[0].reference_notes == [] and fixer.contexts[0].reference_files == {}


def test_union_registry_never_hinted_by_file(cfg, ws, profile):
    setup_tree(ws)
    reg = "module/comp-jar/src/main/resources/META-INF/spring.factories"
    ws.write(reg, "a=b\n")
    ws.commit("reg")
    c = conflict(path="src/main/resources/META-INF/spring.factories", union=True)
    fixer, _ = run_once(cfg, ws, profile, [err(reg, message="bad")], [c])
    assert fixer.contexts[0].reference_notes == []


def test_large_copy_is_truncated_and_unreadable_copy_noted(cfg, ws, profile):
    big = DROPPED_SRC + "// " + "x" * 20_000 + "\n"
    setup_tree(ws, dropped=big)
    fixer, _ = run_once(cfg, ws, profile, [err(KEPT)], [conflict()])
    text = fixer.contexts[0].reference_files[SAVED]
    assert text.endswith("… (truncated)") and len(text) <= 12_000 and text.startswith("package com.x;")
    fixer, _pr = run_once(cfg, ws, profile, [err(KEPT)], [conflict(saved_copies=["merge-conflicts/web/Missing.java"])])
    ctx = fixer.contexts[0]
    assert ctx.reference_files == {}
    assert any("could not be read" in n for n in ctx.reference_notes) and len(ctx.reference_notes) == 2


def test_patch_aimed_at_a_reference_copy_is_dropped(cfg, ws, profile):
    setup_tree(ws)
    fixed = KEPT_SRC.replace("public void a() {}", "public void a() {}\n    public int extra() { return 1; }")
    prop = FixProposal(changes=[FileChange(path=SAVED, op="create", content="junk"),
                                FileChange(path=KEPT, op="create", content=fixed)],
                       analysis="ported extra()", model="m", llm_usage=LLMUsage(calls=1))
    fixer = FakeFixer(proposals=[prop])
    build = FakeBuildRunner(ws, fn=lambda w, call: ok_build() if "extra" in w.read(KEPT) else bad_build(err(KEPT)))
    loop = make_loop(cfg, ws, profile, build=build, fixer=fixer, conflicts=[conflict()])
    pr = loop.run(Phase.RESTRUCTURE, "package", 2)
    assert pr.build_ok
    assert [c.path for c in fixer.applied[0]] == [KEPT]                # the reference path never reaches apply
    assert not ws.exists(SAVED)
    assert any("read-only reference copy" in n for n in pr.attempts[0].notes)


# ---------------------------------------------------------------- packer / prompt
def _cluster() -> ErrorCluster:
    e = err(KEPT, symbol="method extra()", location="class com.x.Shared")
    return ErrorCluster(id="c1", kind=ErrorKind.COMPILE, file=KEPT, errors=[e], signatures=[e.signature])


def _ctx(**kw) -> FixContext:
    return FixContext(phase=Phase.RESTRUCTURE, iteration=1, **kw)


def test_packer_sections_order_and_prompt_text(cfg, ws):
    setup_tree(ws)
    c = _ctx(advisory_hints=["hystrix → resilience4j"], kb_hints=["use X"],
             reference_notes=[f"{KEPT} was merged first-wins from module `core`"],
             reference_files={SAVED: DROPPED_SRC})
    p = ContextPacker(cfg).pack(ws, _cluster(), c, "case=spring_boot")
    titles = [t for t, _ in p.sections]
    src = titles.index("Source (exact text; copy SEARCH blocks from here)")
    assert titles[src + 1] == REF_TITLE
    adv = titles.index(ContextPacker.ADVISORY_TITLE)
    assert titles[adv + 1] == NOTES_TITLE and titles[adv + 2].startswith("Knowledge-base hints")
    body = dict(p.sections)[REF_TITLE]
    assert f"// reference copy (read-only, not in the build): {SAVED}\n```java\n" in body and "extra()" in body
    assert SAVED not in p.files_included
    assert SAVED not in dict(p.sections)["Source (exact text; copy SEARCH blocks from here)"]
    user = LLMFixer(cfg, client=None).build_messages(p)[1].content
    assert REF_TITLE in user and NOTES_TITLE in user
    # the reference path appears only inside the reference section, after its read-only warning
    i_ref = user.index(REF_TITLE)
    assert user.find(SAVED) > i_ref and user.count(SAVED) == 1
    # no reference sections when the context has none
    p = ContextPacker(cfg).pack(ws, _cluster(), _ctx(), "case=spring_boot")
    assert REF_TITLE not in [t for t, _ in p.sections] and NOTES_TITLE not in [t for t, _ in p.sections]


def test_reference_sections_shrink_before_files(cfg, ws):
    order = ContextPacker.TRUNCATE_ORDER
    assert order.index("reference_files") < order.index("files")
    assert order.index("reference_notes") < order.index("files")
    assert order.index("kb_hints") < order.index("reference_files")
    setup_tree(ws)
    c = _ctx(reference_notes=["n" * 200], reference_files={SAVED: "r" * 5000})
    packer = ContextPacker(cfg)
    full = packer.pack(ws, _cluster(), c, "case=x", budget=10**6)
    notes_chars = sum(len(t) + len(b) + 6 for t, b in full.sections if t.startswith("Migration note"))
    p = packer.pack(ws, _cluster(), c, "case=x", budget=full.chars - notes_chars - 2000)
    kinds = [t.split(" ")[0] for t in p.truncated]
    assert "reference_files" in kinds and "files" not in kinds
    assert KEPT_SRC.strip() in dict(p.sections)["Source (exact text; copy SEARCH blocks from here)"]
