from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sbmig.core.config import TargetConfig
from sbmig.core.models import RecipeRef
from sbmig.rewrite import COMPOSITE_RECIPE_ID, RecipeCompositionError, compose_rewrite_yaml, recipe_documents
from sbmig.rewrite.compose import recipe_list_ids, split_documents

REPO = Path(__file__).resolve().parents[2]
BASE = REPO / "knowledge" / "recipes" / "base"


def docs(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if d]


def test_templating_of_base_recipes():
    text = compose_rewrite_yaml([RecipeRef(id="com.company.migrate.SpringBoot41Version", source="project")],
                                base_dir=BASE, target=TargetConfig(spring_boot_version="4.1.3"))
    assert "{{" not in text
    d = recipe_documents(text)["com.company.migrate.SpringBoot41Version"]
    versions = {list(item.values())[0].get("newVersion") for item in d["recipeList"]}
    assert versions == {"4.1.3"}
    default = compose_rewrite_yaml([], base_dir=BASE)
    assert 'newVersion: "4.1.0"' in default


def test_composite_is_last_and_keeps_requested_order(tmp_path):
    extra = tmp_path / "extra.yml"
    extra.write_text("type: specs.openrewrite.org/v1beta/recipe\nname: com.x.Java\ndisplayName: j\n"
                     "recipeList:\n  - org.openrewrite.java.migrate.UpgradeJavaVersion:\n"
                     "      version: {{JAVA_VERSION}}\n  - org.openrewrite.Foo:\n      fw: '{{ SPRING_FRAMEWORK_VERSION }}'\n")
    recipes = [RecipeRef(id="org.openrewrite.java.migrate.UpgradeToJava21"),
               RecipeRef(id="com.x.Java", source="project"),
               RecipeRef(id="com.company.migrate.SpringBoot41Version", source="project"),
               RecipeRef(id="org.openrewrite.java.migrate.UpgradeToJava21")]
    text = compose_rewrite_yaml(recipes, base_dir=BASE, extra_files=[extra])
    all_docs = docs(text)
    assert all_docs[-1]["name"] == COMPOSITE_RECIPE_ID
    assert all_docs[-1]["recipeList"] == ["org.openrewrite.java.migrate.UpgradeToJava21", "com.x.Java",
                                          "com.company.migrate.SpringBoot41Version"]
    java = recipe_documents(text)["com.x.Java"]
    assert java["recipeList"][0]["org.openrewrite.java.migrate.UpgradeJavaVersion"]["version"] == 21
    assert java["recipeList"][1]["org.openrewrite.Foo"]["fw"] == "7.0.x"
    assert recipe_list_ids(java) == ["org.openrewrite.java.migrate.UpgradeJavaVersion", "org.openrewrite.Foo"]
    assert str(extra) in text                                    # sources listed in the header comment


def test_missing_project_or_kb_ids_raise_clear_error():
    with pytest.raises(RecipeCompositionError) as ei:
        compose_rewrite_yaml([RecipeRef(id="com.company.kb.Nope", source="kb"),
                              RecipeRef(id="com.company.migrate.Missing", source="project"),
                              RecipeRef(id="org.openrewrite.java.Unknown")], base_dir=BASE)
    assert ei.value.missing == ["com.company.kb.Nope", "com.company.migrate.Missing"]
    assert "com.company.kb.Nope" in str(ei.value) and "spring-boot-41-version.yml" in str(ei.value)


def test_kb_files_override_base_and_recipe_file_is_included(tmp_path):
    kb = tmp_path / "kb.yml"
    kb.write_text("---\ntype: specs.openrewrite.org/v1beta/recipe\nname: com.company.migrate.SpringBoot41Version\n"
                  "displayName: overridden\nrecipeList: []\n---\n# only a comment\n")
    own = tmp_path / "own.yaml"
    own.write_text("type: specs.openrewrite.org/v1beta/recipe\nname: com.company.kb.Own\nrecipeList:\n"
                   "  - org.openrewrite.java.ChangeType:\n      oldFullyQualifiedTypeName: a.B\n"
                   "      newFullyQualifiedTypeName: c.D\n---\ntype: specs.openrewrite.org/v1beta/style\n"
                   "name: com.company.Style\nstyleConfigs: []\n")
    text = compose_rewrite_yaml([RecipeRef(id="com.company.kb.Own", source="kb", file=str(own))],
                                base_dir=BASE, kb_files=[kb])
    d = recipe_documents(text)
    assert d["com.company.migrate.SpringBoot41Version"]["displayName"] == "overridden"
    assert "com.company.kb.Own" in d
    assert any(x.get("type") == "specs.openrewrite.org/v1beta/style" for x in docs(text))
    assert text.count("name: com.company.migrate.SpringBoot41Version") == 1


def test_errors_for_missing_file_invalid_yaml_and_reserved_id(tmp_path, caplog):
    with pytest.raises(FileNotFoundError):
        compose_rewrite_yaml([], base_dir=None, extra_files=[tmp_path / "nope.yml"])
    bad = tmp_path / "bad.yml"
    bad.write_text("name: [unclosed\n")
    with pytest.raises(ValueError, match="invalid YAML"):
        compose_rewrite_yaml([], extra_files=[bad])
    reserved = tmp_path / "reserved.yml"
    reserved.write_text(f"type: specs.openrewrite.org/v1beta/recipe\nname: {COMPOSITE_RECIPE_ID}\n"
                        "recipeList: [x]\nx: '{{{{UNKNOWN_KEY}}}}'\n")
    text = compose_rewrite_yaml([RecipeRef(id="org.openrewrite.java.Foo")], extra_files=[reserved])
    assert recipe_documents(text)[COMPOSITE_RECIPE_ID]["recipeList"] == ["org.openrewrite.java.Foo"]
    assert "reserved composite id" in caplog.text


def test_split_documents_drops_comment_only_chunks():
    assert split_documents("# hi\n---\na: 1\n--- # c\n# only\n---\nb: 2\n") == ["a: 1", "b: 2"]
