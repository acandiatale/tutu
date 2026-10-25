from __future__ import annotations

import pytest

from sbmig.kb import WHITELIST, validate_recipe_yaml
from sbmig.kb.recipes import (
    is_kb_recipe_name,
    is_none_answer,
    recipe_names_in_yaml,
    short_recipe_name,
    strip_fences,
    with_recipe_name,
)

from .helpers import VALID_RECIPE


def _recipe(entries: str, name: str = "Fix", extra: str = "") -> str:
    return (f"type: specs.openrewrite.org/v1beta/recipe\nname: {name}\ndisplayName: Fix\n"
            f"description: Does a fix.\n{extra}recipeList:\n{entries}")


def test_valid_recipe():
    ok, errors, parsed = validate_recipe_yaml(VALID_RECIPE)
    assert ok, errors
    assert parsed["name"] == "RenameFooToBar"


def test_every_whitelisted_recipe_with_required_options_is_valid():
    for rid, (required, _opt) in WHITELIST.items():
        if required:
            opts = "".join(f"      {k}: v{i}\n" for i, k in enumerate(required))
            if rid.endswith("ChangePluginGroupIdAndArtifactId"):
                opts += "      newArtifactId: x\n"
            entry = f"  - {rid}:\n{opts}"
        else:
            entry = f"  - {rid}\n"
        ok, errors, _ = validate_recipe_yaml(_recipe(entry))
        assert ok, (rid, errors)


@pytest.mark.parametrize("entries,needle", [
    ("  - org.openrewrite.java.RemoveUnusedImports\n", "not allowed"),
    ("  - org.openrewrite.java.ChangeType:\n      oldFullyQualifiedTypeName: a.B\n", "missing required"),
    ("  - org.openrewrite.java.ChangeType:\n      oldFullyQualifiedTypeName: a.B\n"
     "      newFullyQualifiedTypeName: a.C\n      bogus: 1\n", "unknown option"),
    ("  - org.openrewrite.java.ChangeType:\n      oldFullyQualifiedTypeName: [a, b]\n"
     "      newFullyQualifiedTypeName: a.C\n", "scalar"),
    ("  - org.openrewrite.java.ChangeType:\n      oldFullyQualifiedTypeName: 'a.B; rm -rf /'\n"
     "      newFullyQualifiedTypeName: a.C\n", "suspicious"),
    ("  - org.openrewrite.java.ChangeType:\n      oldFullyQualifiedTypeName: '  '\n"
     "      newFullyQualifiedTypeName: a.C\n", "empty value"),
    ("  - org.openrewrite.java.ChangeType:\n      oldFullyQualifiedTypeName: \"a\\nb\"\n"
     "      newFullyQualifiedTypeName: a.C\n", "multi-line"),
    ("  - org.openrewrite.java.ChangeType:\n      oldFullyQualifiedTypeName: " + "a" * 400 + "\n"
     "      newFullyQualifiedTypeName: a.C\n", "too long"),
    ("  - org.openrewrite.java.ChangeType: notamapping\n", "options must be a mapping"),
    ("  - {a: 1, b: 2}\n", "single-key mapping"),
    ("  - org.openrewrite.maven.ChangePluginGroupIdAndArtifactId:\n      oldGroupId: g\n      oldArtifactId: a\n",
     "newGroupId and/or newArtifactId"),
    ("  - org.openrewrite.maven.ChangePropertyValue:\n      key: k\n      newValue: ${evil}\n", "'$'"),
    ("  - org.openrewrite.maven.ChangePropertyValue:\n      key: k\n      newValue: `id`\n", "backticks"),
])
def test_invalid_entries(entries, needle):
    ok, errors, _ = validate_recipe_yaml(_recipe(entries))
    assert not ok
    assert any(needle in e for e in errors), errors


def test_too_many_entries():
    entry = "  - org.openrewrite.maven.RemoveProperty:\n      propertyName: p\n"
    ok, errors, _ = validate_recipe_yaml(_recipe(entry * 13))
    assert not ok and any("max 12" in e for e in errors)
    assert validate_recipe_yaml(_recipe(entry * 12))[0]


@pytest.mark.parametrize("text,needle", [
    ("", "empty"),
    ("a: [b", "invalid YAML"),
    (VALID_RECIPE + "---\n" + VALID_RECIPE, "exactly one"),
    ("- a\n- b\n", "mapping"),
    (VALID_RECIPE.replace("RenameFooToBar", "org.evil.Recipe"), "PascalCase"),
    (VALID_RECIPE.replace("RenameFooToBar", "org.openrewrite.java.ChangeType"), "PascalCase"),
    (VALID_RECIPE.replace("RenameFooToBar", "com.company.Fix"), "PascalCase"),
    (VALID_RECIPE.replace("RenameFooToBar", "lower.case"), "PascalCase"),
    (VALID_RECIPE.replace("specs.openrewrite.org/v1beta/recipe", "specs.openrewrite.org/v1beta/style"), "type must"),
    (VALID_RECIPE.replace("displayName: Rename Foo to Bar\n", ""), "displayName"),
    (VALID_RECIPE.replace("description: Foo moved to Bar in library 2.0.\n", ""), "description"),
    (VALID_RECIPE + "preconditions: []\n", "unknown top-level"),
    (VALID_RECIPE + "tags: notalist\n", "tags"),
    ("type: specs.openrewrite.org/v1beta/recipe\nname: X\ndisplayName: X\ndescription: d\n"
     "recipeList: []\n", "non-empty"),
    (VALID_RECIPE.replace("Foo moved", "#!/bin/sh Foo moved"), "shell"),
])
def test_invalid_documents(text, needle):
    ok, errors, _ = validate_recipe_yaml(text)
    assert not ok
    assert any(needle in e for e in errors), errors


def test_tags_and_bool_options_allowed():
    text = _recipe("  - org.openrewrite.java.dependencies.UpgradeDependencyVersion:\n      groupId: g\n"
                   "      artifactId: a\n      newVersion: 1.2.3\n      overrideManagedVersion: true\n",
                   extra="tags: [kb, migration]\n")
    assert validate_recipe_yaml(text)[0]


def test_fences_are_stripped():
    assert validate_recipe_yaml("```yaml\n" + VALID_RECIPE + "```\n")[0]
    assert strip_fences("Here you go:\n```yml\nname: x\n```\nthanks") == "name: x"
    assert strip_fences("plain") == "plain"


def test_none_answers():
    assert is_none_answer("NONE")
    assert is_none_answer("none.")
    assert is_none_answer("```\nNONE\n```")
    assert is_none_answer("NONE\nbecause the fix adds logic")
    assert is_none_answer("")
    assert not is_none_answer(VALID_RECIPE)


def test_recipe_names_in_yaml():
    assert recipe_names_in_yaml(VALID_RECIPE + "---\nname: Two\n") == [
        "RenameFooToBar", "Two"]
    assert recipe_names_in_yaml("a: [") == []
    assert recipe_names_in_yaml("type: specs.openrewrite.org/v1beta/style\nname: x\n") == []


def test_kb_recipe_names_are_one_pascal_word_and_legacy_prefix_stays_valid():
    # 2026-10-25: KB recipes are named `RenameFooToBar`; a KB written before keeps `com.company.kb.RenameFooToBar`
    assert is_kb_recipe_name("RenameFooToBar") and is_kb_recipe_name("com.company.kb.RenameFooToBar")
    for bad in ("org.openrewrite.java.ChangeType", "com.company.migrate.Run", "com.company.kb.lower", "rename", ""):
        assert not is_kb_recipe_name(bad)
    legacy = VALID_RECIPE.replace("name: RenameFooToBar", "name: com.company.kb.RenameFooToBar")
    assert validate_recipe_yaml(legacy)[0]
    assert short_recipe_name("com.company.kb.RenameFooToBar") == "RenameFooToBar"
    assert short_recipe_name("org.openrewrite.java.ChangeType") == "org.openrewrite.java.ChangeType"
    renamed = with_recipe_name(legacy, "RenameFooToBar")
    assert renamed == VALID_RECIPE                         # only the top-level name line changes
    assert "      oldFullyQualifiedTypeName: com.lib.Foo" in renamed
