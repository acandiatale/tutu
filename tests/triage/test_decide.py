"""v1 decision helpers that stay until PLAN-v2 step 4 (the v2 decision engine: test_v2_cases.py)."""
from __future__ import annotations

from sbmig.core.models import KBMatch
from sbmig.triage import HandlerRegistry, Triager, recipe_refs_for


def test_recipe_refs_fallbacks(cfg):
    t = Triager(cfg, registry=HandlerRegistry())
    refs = recipe_refs_for(["org.openrewrite.java.testing.junit5.JUnit4to5Migration", "x.Y", "x.Y"],
                           KBMatch(recipe_ids=["kb.Z"]), t.rules)
    assert refs[0].artifact == "org.openrewrite.recipe:rewrite-testing-frameworks"
    assert refs[1].id == "x.Y" and refs[1].source == "builtin" and len(refs) == 2
    assert recipe_refs_for(["kb.Z"], KBMatch(recipe_ids=["kb.Z"], recipe_files=[]), t.rules)[0].file is None
