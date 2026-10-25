from __future__ import annotations

import json

from sbmig.core.models import DecisionKind, FileChange, KBRecipe, Phase, PhaseResult
from sbmig.kb import FileKnowledgeBase, sanitize_case
from sbmig.kb.harvest import changes_as_diff, looks_mechanical, redact

from .helpers import (
    L1,
    L2,
    VALID_RECIPE,
    FakeLLM,
    make_attempt,
    make_case,
    make_error,
    recipe_yaml,
    with_kb,
)


def _phase(*attempts) -> PhaseResult:
    return PhaseResult(phase=Phase.SPRING, attempts=list(attempts))


def test_harvest_only_successful_llm_fixes(kb, ws, profile):
    good = make_attempt("good")
    build_ok = make_attempt("ok2", outcome="build_ok", changes=[FileChange(path="pom.xml", op="modify",
                                                                           search="<a/>", replace="<b/>")])
    no_change = make_attempt("nc", outcome="no_change")
    not_applied = make_attempt("na", applied=False)
    unresolved = make_attempt("ur", resolved=False)
    resolved_unknown = make_attempt("ru", resolved=None, changes=[FileChange(path="x.txt", op="delete")])
    empty = make_attempt("empty", changes=[])
    handler = make_attempt("h", kind=DecisionKind.HANDLER)
    res = kb.harvest(_phase(good, build_ok, no_change, not_applied, unresolved, resolved_unknown, empty, handler),
                     profile, ws)
    assert len(res.cases_created) == 3
    stored = [kb.get_case(c) for c in res.cases_created]
    assert {c.tags[0] for c in stored} == {"attempt:good", "attempt:ok2", "attempt:ru"}
    c = next(c for c in stored if c.tags[0] == "attempt:good")
    assert c.summary == "Removed WebSecurityConfigurerAdapter base class."
    assert c.signatures == [L1] and c.signatures_generic == [L2]
    assert c.project_fingerprint == "fp-demo" and c.case_type.value == "spring_boot"
    assert c.phase == Phase.SPRING and c.model == "fake-model" and c.fix_kind == "llm_patch"
    assert "recipe candidate generation skipped: no LLM client" in res.notes


def test_harvest_dedupe(kb, ws, profile):
    r1 = kb.harvest(_phase(make_attempt("a")), profile, ws)
    r2 = kb.harvest(_phase(make_attempt("b")), profile, ws)
    assert len(r1.cases_created) == 1 and r2.cases_created == []
    assert any("duplicate of case" in n for n in r2.notes)
    assert kb.get_case(r1.cases_created[0]).reuse_count == 1


def test_harvest_disabled(kb_cfg, ws, profile):
    kb = FileKnowledgeBase(with_kb(kb_cfg, harvest=False))
    res = kb.harvest(_phase(make_attempt()), profile, ws)
    assert res.cases_created == [] and "disabled" in res.notes[0]


def test_sanitization(ws, profile):
    root = str(ws.root)
    err = make_error(file=f"{root}/src/main/java/com/example/config/SecurityConfig.java",
                     raw=f"[ERROR] {root}/src/A.java:[3,1] password=hunter2 " + "x" * 3000)
    err = err.model_copy(update={"message": f"cannot find symbol in {root}/src/A.java token: abc123",
                                 "location": f"class at {root}/x"})
    changes = [
        FileChange(path=f"{root}/src/main/resources/application.yml", op="modify",
                   search="spring.datasource.password=s3cr3t", replace="api_key: ABCDEF\nother: 1"),
        FileChange(path="./src/New.java", op="create", content='String secret = "x"; // secret=zzz'),
        FileChange(path="/elsewhere/abs/Thing.java", op="rename", new_path=f"{root}\\win\\Path.java"),
    ]
    a = make_attempt(errors=[err], changes=changes, analysis="")
    case = sanitize_case(a, profile, ws)
    assert [c.path for c in case.changes] == ["src/main/resources/application.yml", "src/New.java", "Thing.java"]
    assert case.changes[0].search == "spring.datasource.password=***"
    assert case.changes[0].replace == "api_key: ***\nother: 1"
    assert "zzz" not in case.changes[1].content and "secret=***" in case.changes[1].content
    e = case.errors[0]
    assert e.file == "src/main/java/com/example/config/SecurityConfig.java"
    assert root not in e.raw and "hunter2" not in e.raw and len(e.raw) <= 2000 + len("…[truncated]")
    assert root not in e.message and "abc123" not in e.message
    assert root not in e.location
    assert case.summary == f"{L2} fixed by editing application.yml, New.java, Thing.java"
    dumped = case.model_dump_json()
    assert root not in dumped and "s3cr3t" not in dumped and "ABCDEF" not in dumped


def test_sanitize_without_cluster_errors_uses_signatures(profile):
    a = make_attempt(errors=[])
    a = a.model_copy(update={"cluster_signatures": [L1, "weird-signature"]})
    case = sanitize_case(a, profile)
    assert case.signatures == [L1, "weird-signature"] and case.signatures_generic == []
    assert [e.kind.value for e in case.errors] == ["compile", "unknown"]
    assert case.summary.startswith("Removed")
    nocase = sanitize_case(make_attempt(errors=[], analysis="").model_copy(update={"cluster_signatures": []}))
    assert nocase.summary.startswith("build error fixed by editing") and nocase.project_fingerprint == ""


def test_redact_none():
    assert redact(None) is None
    assert redact("TOKEN = abc") == "TOKEN = ***"


def test_credit_recipes_and_patches(kb, ws, profile):
    r = kb.add_recipe(recipe_yaml(), KBRecipe(id="RenameFooToBar", path="", signatures=[L1]))
    cid = kb.record_case(make_case())
    attempts = [
        make_attempt("r1", kind=DecisionKind.KB_RECIPE, recipe_ids=[r.id, "org.openrewrite.Builtin"], changes=[]),
        make_attempt("r2", kind=DecisionKind.KB_RECIPE, recipe_ids=[r.id], outcome="no_change", changes=[]),
        make_attempt("p1", kind=DecisionKind.KB_PATCH, kb_case_ids=[cid, "20200101-missing00"]),
        make_attempt("p2", kind=DecisionKind.KB_PATCH, kb_case_ids=[cid], outcome="regressed"),
    ]
    res = kb.harvest(_phase(*attempts), profile, ws)
    assert res.recipes_credited == [r.id]
    cur = kb.get_recipe(r.id)
    assert cur.success_count == 1 and cur.fail_count == 1 and cur.projects == ["fp-demo"]
    c = kb.get_case(cid)
    assert c.reuse_count == 1 and c.reuse_projects == ["fp-demo"]
    assert any("not found" in n for n in res.notes)
    assert res.cases_created == []


def test_recipe_candidate_valid_yaml(kb, ws, profile):
    llm = FakeLLM(VALID_RECIPE)
    res = kb.harvest(_phase(make_attempt()), profile, ws, llm=llm)
    assert res.candidates_created == ["RenameFooToBar"]
    rec = kb.get_recipe("RenameFooToBar")
    assert rec.status == "candidate"
    assert rec.signatures == [L1, L2] and rec.source_case_ids == res.cases_created
    assert rec.description == "Foo moved to Bar in library 2.0."
    case = kb.get_case(res.cases_created[0])
    assert case.recipe_candidate_id == rec.id
    idx = json.loads((kb.root / "index.json").read_text())
    assert idx["recipes"][L2] == [rec.id]
    # prompt content
    user = llm.calls[0][1].content
    assert "NONE" in user and L1 in user and "-public class SecurityConfig extends" in user
    assert "{{" not in user


def test_recipe_candidate_none(kb, ws, profile):
    res = kb.harvest(_phase(make_attempt()), profile, ws, llm=FakeLLM("NONE"))
    assert res.candidates_created == [] and kb.list_recipes() == []
    assert any("no mechanical recipe" in n for n in res.notes)
    assert kb.get_case(res.cases_created[0]).recipe_candidate_id is None


def test_recipe_candidate_disallowed_type_rejected(kb, ws, profile):
    bad = VALID_RECIPE.replace("org.openrewrite.java.ChangeType", "org.openrewrite.java.ExecuteShell")
    res = kb.harvest(_phase(make_attempt()), profile, ws, llm=FakeLLM(bad))
    assert res.candidates_created == [] and kb.list_recipes() == []
    assert any("recipe candidate rejected" in n and "not allowed" in n for n in res.notes)


def test_recipe_candidate_fenced_yaml(kb, ws, profile):
    res = kb.harvest(_phase(make_attempt()), profile, ws, llm=FakeLLM("```yaml\n" + VALID_RECIPE + "```"))
    assert res.candidates_created == ["RenameFooToBar"]
    text = (kb.root / "recipes" / "candidates" / "RenameFooToBar.yml").read_text()
    assert "```" not in text and text.startswith("type: specs.openrewrite.org")


def test_recipe_candidate_existing_recipe_is_linked(kb, ws, profile):
    kb.add_recipe(recipe_yaml(), KBRecipe(id="RenameFooToBar", status="active", path="",
                                          signatures=["compile|old"]))
    res = kb.harvest(_phase(make_attempt()), profile, ws, llm=FakeLLM(VALID_RECIPE))
    assert res.candidates_created == []
    rec = kb.get_recipe("RenameFooToBar")
    assert rec.status == "active" and L1 in rec.signatures and res.cases_created[0] in rec.source_case_ids
    assert kb.get_case(res.cases_created[0]).recipe_candidate_id == rec.id


def test_recipe_candidate_legacy_prefix_from_llm_is_dropped(kb, ws, profile):
    answer = VALID_RECIPE.replace("name: RenameFooToBar", "name: com.company.kb.RenameFooToBar")
    res = kb.harvest(_phase(make_attempt()), profile, ws, llm=FakeLLM(answer))
    assert res.candidates_created == ["RenameFooToBar"]
    text = (kb.root / "recipes" / "candidates" / "RenameFooToBar.yml").read_text()
    assert "name: RenameFooToBar\n" in text and "com.company.kb" not in text


def test_recipe_candidate_links_to_legacy_named_recipe(kb, ws, profile):
    # a KB written before 2026-10-25 holds com.company.kb.RenameFooToBar: the same fix links to it, no twin recipe
    kb.add_recipe(recipe_yaml("com.company.kb.RenameFooToBar"),
                  KBRecipe(id="com.company.kb.RenameFooToBar", status="active", path="", signatures=["compile|old"]))
    res = kb.harvest(_phase(make_attempt()), profile, ws, llm=FakeLLM(VALID_RECIPE))
    assert res.candidates_created == [] and [r.id for r in kb.list_recipes()] == ["com.company.kb.RenameFooToBar"]
    assert kb.get_recipe("RenameFooToBar").id == "com.company.kb.RenameFooToBar"
    assert kb.get_recipe("com.company.kb.RenameFooToBar").id == "com.company.kb.RenameFooToBar"
    assert kb.get_case(res.cases_created[0]).recipe_candidate_id == "com.company.kb.RenameFooToBar"


def test_recipe_candidates_skipped_when_not_mechanical_or_disabled(kb_cfg, ws, profile):
    big = FileChange(path="src/A.java", op="modify", search="\n".join(f"a{i}" for i in range(30)),
                     replace="\n".join(f"b{i}" for i in range(30)))
    llm = FakeLLM(VALID_RECIPE)
    kb = FileKnowledgeBase(kb_cfg)
    res = kb.harvest(_phase(make_attempt(changes=[big])), profile, ws, llm=llm)
    assert len(res.cases_created) == 1 and llm.calls == []
    kb2 = FileKnowledgeBase(with_kb(kb_cfg, generate_recipe_candidates=False, dir=str(ws.root.parent / "kb2")))
    kb2.harvest(_phase(make_attempt()), profile, ws, llm=llm)
    assert llm.calls == []


def test_harvest_never_raises(kb, ws, profile, monkeypatch):
    class BoomLLM:
        def complete(self, *a, **k):
            raise RuntimeError("llm down")

    res = kb.harvest(_phase(make_attempt()), profile, ws, llm=BoomLLM())
    assert len(res.cases_created) == 1
    assert any("llm down" in n for n in res.notes)

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(kb, "record_case_ex", boom)
    monkeypatch.setattr(kb, "credit_recipe", boom)
    monkeypatch.setattr(kb, "get_recipe", lambda rid: object())
    res = kb.harvest(_phase(make_attempt("x"), make_attempt("y", kind=DecisionKind.KB_RECIPE, recipe_ids=["r"])),
                     profile, ws)
    assert any("harvest failed: disk" in n for n in res.notes)
    assert any("credit failed: disk" in n for n in res.notes)
    res = kb.harvest(None, profile, ws)  # type: ignore[arg-type]
    assert any(n.startswith("harvest failed") for n in res.notes)


def test_looks_mechanical_and_diff():
    pom = make_case(changes=[FileChange(path="a/pom.xml", op="create", content="<project/>")])
    assert looks_mechanical(pom)
    assert looks_mechanical(make_case())
    assert not looks_mechanical(make_case(changes=[]))
    assert not looks_mechanical(make_case(changes=[FileChange(path="A.java", op="create", content="x")]))
    diff = changes_as_diff([
        FileChange(path="A.java", op="modify", search="a\nb", replace="a\nc"),
        FileChange(path="B.java", op="modify", search="same", replace="same"),
        FileChange(path="C.java", op="create", content="x\ny"),
        FileChange(path="D.java", op="delete"),
        FileChange(path="E.java", op="rename", new_path="F.java"),
    ])
    assert "-b" in diff and "+c" in diff and "no textual change" in diff
    assert "=== CREATE C.java\n+x\n+y" in diff and "=== DELETE D.java" in diff and "E.java -> F.java" in diff
