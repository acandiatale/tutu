from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sbmig.core.models import FileChange, KBRecipe
from sbmig.kb import FileKnowledgeBase

from .helpers import L1, L2, make_case, make_cluster, make_error, recipe_yaml, security_change, with_kb


def test_match_empty_kb(kb, ws, profile):
    m = kb.match(make_cluster(), ws, profile)
    assert m.cases == [] and m.recipe_ids == [] and m.hints == []


def test_match_by_l1_and_l2(kb, ws, profile):
    l1_case = kb.record_case(make_case(summary="exact"))
    l2_case = kb.record_case(make_case(signatures=["compile|cannot find symbol|class X|other"],
                                       signatures_generic=[L2], summary="generic"))
    unrelated = kb.record_case(make_case(signatures=["compile|zzz"], signatures_generic=["compile|zzz"]))
    m = kb.match(make_cluster(), ws, profile)
    ids = [c.id for c in m.cases]
    assert set(ids) == {l1_case, l2_case} and unrelated not in ids
    # only L2 on the cluster side
    m2 = kb.match(make_cluster(make_error(l1="compile|novel|x|y")), ws, profile)
    assert {c.id for c in m2.cases} == {l1_case, l2_case}


def test_match_sort_reuse_then_newest_and_cap(kb, ws, profile):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ids = []
    for i in range(12):
        ids.append(kb.record_case(make_case(signatures=[f"compile|m|{i}|l"], signatures_generic=[L2],
                                            created_at=base + timedelta(days=i), summary=f"c{i}")))
    popular = ids[0]
    kb.bump_case_reuse(popular)
    kb.bump_case_reuse(popular)
    m = kb.match(make_cluster(), ws, profile)
    assert len(m.cases) == 10
    assert m.cases[0].id == popular
    assert m.cases[1].id == ids[11] and m.cases[2].id == ids[10]


def test_hints_format_and_cap(kb_cfg, ws, profile):
    kb = FileKnowledgeBase(with_kb(kb_cfg, max_hints=2))
    pom_change = FileChange(path="pom.xml", op="modify", search="a", replace="b")
    cid = kb.record_case(make_case(changes=[security_change(), pom_change], summary="drop adapter"))
    for i in range(3):
        kb.record_case(make_case(signatures=[f"compile|m|{i}|l"], summary=f"s{i}"))
    kb.bump_case_reuse(cid)
    m = kb.match(make_cluster(), ws, profile)
    assert len(m.hints) == 2
    assert m.hints[0] == f"[case {cid}, reused 1×] drop adapter — changed: SecurityConfig.java, pom.xml"


def test_hints_disabled(kb_cfg, ws, profile):
    kb = FileKnowledgeBase(with_kb(kb_cfg, use_hints=False))
    kb.record_case(make_case())
    m = kb.match(make_cluster(), ws, profile)
    assert m.hints == [] and len(m.cases) == 1


# ---------------------------------------------------------------- replay
def test_replay_when_search_present_once(kb, ws, profile):
    cid = kb.record_case(make_case())
    m = kb.match(make_cluster(), ws, profile)
    assert [c.id for c in m.replayable] == [cid]
    assert m.replayable[0].changes[0].path == "src/main/java/com/example/config/SecurityConfig.java"


def test_no_replay_when_search_absent(kb, ws, profile):
    kb.record_case(make_case(changes=[FileChange(path="src/main/java/com/example/config/SecurityConfig.java",
                                                 op="modify", search="not in file", replace="x")]))
    assert kb.match(make_cluster(), ws, profile).replayable == []


def test_no_replay_when_search_ambiguous(kb, ws, profile):
    p = "src/main/java/com/example/config/SecurityConfig.java"
    ws.write(p, ws.read(p) + "\n// public class SecurityConfig extends WebSecurityConfigurerAdapter {\n")
    kb.record_case(make_case())
    assert kb.match(make_cluster(), ws, profile).replayable == []


def test_replay_basename_fallback_unique(kb, ws, profile):
    kb.record_case(make_case(changes=[security_change("module/a/src/main/java/x/SecurityConfig.java")]))
    m = kb.match(make_cluster(), ws, profile)
    assert len(m.replayable) == 1
    assert m.replayable[0].changes[0].path == "src/main/java/com/example/config/SecurityConfig.java"


def test_replay_basename_fallback_ambiguous(kb, ws, profile):
    ws.write("other/SecurityConfig.java", ws.read("src/main/java/com/example/config/SecurityConfig.java"))
    kb.record_case(make_case(changes=[security_change("module/a/SecurityConfig.java")]))
    assert kb.match(make_cluster(), ws, profile).replayable == []


def test_replay_create_delete_rename_rules(kb, ws, profile):
    create_new = FileChange(path="src/main/java/New.java", op="create", content="class New {}")
    create_existing = FileChange(path="pom.xml", op="create", content="x")
    ok = kb.record_case(make_case(changes=[security_change(), create_new], summary="ok"))
    kb.record_case(make_case(changes=[security_change(), create_existing], summary="bad"))
    delete_ok = kb.record_case(make_case(signatures=[L1, "d"], changes=[FileChange(path="pom.xml", op="delete")]))
    kb.record_case(make_case(signatures=[L1, "e"], changes=[FileChange(path="gone.xml", op="delete")]))
    rename_ok = kb.record_case(make_case(signatures=[L1, "r"], changes=[
        FileChange(path="pom.xml", op="rename", new_path="pom2.xml")]))
    kb.record_case(make_case(signatures=[L1, "r2"], changes=[
        FileChange(path="pom.xml", op="rename", new_path="src/main/java/com/example/config/SecurityConfig.java")]))
    kb.record_case(make_case(signatures=[L1, "m"], changes=[FileChange(path="pom.xml", op="modify")]))
    kb.record_case(make_case(signatures=[L1, "none"], changes=[]))
    kb.record_case(make_case(signatures=[L1, "h"], fix_kind="handler"))
    m = kb.match(make_cluster(), ws, profile)
    assert {c.id for c in m.replayable} == {ok, delete_ok, rename_ok}


def test_replay_disabled(kb_cfg, ws, profile):
    kb = FileKnowledgeBase(with_kb(kb_cfg, use_patch_replay=False))
    kb.record_case(make_case())
    assert kb.match(make_cluster(), ws, profile).replayable == []


# ---------------------------------------------------------------- recipes / patterns
def _add(kb, name, status, sigs):
    return kb.add_recipe(recipe_yaml(name), KBRecipe(id=name, status=status, path="", signatures=sigs))


def test_match_active_and_candidate_recipes(kb, ws, profile):
    a = _add(kb, "com.company.kb.ActiveOne", "active", [L1])
    c = _add(kb, "com.company.kb.CandOne", "candidate", [L2])
    _add(kb, "com.company.kb.Rejected", "rejected", [L1])
    _add(kb, "com.company.kb.Other", "active", ["compile|other"])
    m = kb.match(make_cluster(), ws, profile)
    assert m.recipe_ids == [a.id] and m.recipe_files == [a.path]
    assert m.candidate_recipe_ids == [c.id] and m.candidate_recipe_files == [c.path]


def test_match_recipe_meta_edited_without_reindex(kb, ws, profile):
    import json
    r = _add(kb, "com.company.kb.Edited", "active", ["compile|nothing"])
    meta = kb._meta_path(__import__("pathlib").Path(r.path))
    data = json.loads(meta.read_text())
    data["signatures"] = [L2]
    meta.write_text(json.dumps(data))
    assert kb.match(make_cluster(), ws, profile).recipe_ids == [r.id]


def test_active_recipes_disabled(kb_cfg, ws, profile):
    kb = FileKnowledgeBase(with_kb(kb_cfg, use_active_recipes=False))
    _add(kb, "com.company.kb.ActiveOne", "active", [L1])
    m = kb.match(make_cluster(), ws, profile)
    assert m.recipe_ids == [] and kb.active_recipe_files() == [] and kb.active_recipe_ids() == []


def test_match_patterns(kb, ws, profile):
    kb.ensure_layout()
    (kb.root / "patterns" / "security.yml").write_text(
        "id: wsca\n"
        "match: {signature_regex: 'WebSecurityConfigurerAdapter'}\n"
        "recipes: [org.openrewrite.java.spring.boot2.WebSecurityConfigurerAdapter]\n"
        "handler_ids: [security-filter-chain]\n"
        "hints: ['Replace the adapter with a SecurityFilterChain @Bean']\n")
    (kb.root / "patterns" / "multi.yaml").write_text(
        "- match: {signature_regex: 'nomatch'}\n  hints: [never]\n"
        "- match: {signature_regex: '^compile\\|cannot_find_symbol'}\n  hints: [generic]\n"
        "---\n"
        "match: {signature_regex: '('}\n"
        "---\n"
        "id: no-regex\n"
        "---\n"
        "just a string\n")
    (kb.root / "patterns" / "broken.yml").write_text("a: [unclosed")
    m = kb.match(make_cluster(), ws, profile)
    assert m.recipe_ids == ["org.openrewrite.java.spring.boot2.WebSecurityConfigurerAdapter"]
    assert m.handler_ids == ["security-filter-chain"]
    assert "[pattern multi] generic" in m.hints
    assert "[pattern wsca] Replace the adapter with a SecurityFilterChain @Bean" in m.hints
    assert not any("never" in h for h in m.hints)
    assert kb.stats()["patterns"] == 3
