from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sbmig.core.config import TargetConfig
from sbmig.core.models import RecipeRef
from sbmig.rewrite import COMPOSITE_RECIPE_ID, RecipeCompositionError, compose_rewrite_yaml, recipe_documents
from sbmig.rewrite.compose import recipe_list_ids, split_documents

PIN = ("type: specs.openrewrite.org/v1beta/recipe\nname: com.acme.BootPin\ndisplayName: pin\nrecipeList:\n"
       "  - org.openrewrite.maven.UpgradeParentVersion:\n      groupId: org.springframework.boot\n"
       "      artifactId: spring-boot-starter-parent\n      newVersion: \"{{SPRING_BOOT_VERSION}}\"\n")


def docs(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if d]


def test_planned_run_is_the_composite_alone():
    """v2 plans OpenRewrite recipes only: no project / KB recipe files are concatenated."""
    text = compose_rewrite_yaml([RecipeRef(id="org.openrewrite.java.migrate.UpgradeToJava21")])
    assert list(recipe_documents(text)) == [COMPOSITE_RECIPE_ID]
    assert recipe_documents(text)[COMPOSITE_RECIPE_ID]["recipeList"] == ["org.openrewrite.java.migrate.UpgradeToJava21"]
    assert "knowledge/recipes/base" not in text


def test_templating_of_recipe_files(tmp_path):
    own = tmp_path / "pin.yml"
    own.write_text(PIN)
    text = compose_rewrite_yaml([RecipeRef(id="com.acme.BootPin")], extra_files=[own],
                                target=TargetConfig(spring_boot_version="4.1.3"))
    assert "{{" not in text
    d = recipe_documents(text)["com.acme.BootPin"]
    assert {list(item.values())[0].get("newVersion") for item in d["recipeList"]} == {"4.1.3"}
    default = compose_rewrite_yaml([], extra_files=[own])
    assert 'newVersion: "4.1.0"' in default


def test_composite_is_last_and_keeps_requested_order(tmp_path):
    extra = tmp_path / "extra.yml"
    extra.write_text("type: specs.openrewrite.org/v1beta/recipe\nname: com.x.Java\ndisplayName: j\n"
                     "recipeList:\n  - org.openrewrite.java.migrate.UpgradeJavaVersion:\n"
                     "      version: {{JAVA_VERSION}}\n  - org.openrewrite.Foo:\n      fw: '{{ SPRING_FRAMEWORK_VERSION }}'\n")
    recipes = [RecipeRef(id="org.openrewrite.java.migrate.UpgradeToJava21"),
               RecipeRef(id="com.x.Java"),
               RecipeRef(id="org.openrewrite.java.migrate.UpgradeToJava21")]
    text = compose_rewrite_yaml(recipes, extra_files=[extra])
    all_docs = docs(text)
    assert all_docs[-1]["name"] == COMPOSITE_RECIPE_ID
    assert all_docs[-1]["recipeList"] == ["org.openrewrite.java.migrate.UpgradeToJava21", "com.x.Java"]
    java = recipe_documents(text)["com.x.Java"]
    assert java["recipeList"][0]["org.openrewrite.java.migrate.UpgradeJavaVersion"]["version"] == 21
    assert java["recipeList"][1]["org.openrewrite.Foo"]["fw"] == "7.0.x"
    assert recipe_list_ids(java) == ["org.openrewrite.java.migrate.UpgradeJavaVersion", "org.openrewrite.Foo"]
    assert str(extra) in text                                    # sources listed in the header comment


def test_later_file_overrides_and_recipe_file_is_included(tmp_path):
    first = tmp_path / "first.yml"
    first.write_text(PIN)
    override = tmp_path / "override.yml"
    override.write_text("---\ntype: specs.openrewrite.org/v1beta/recipe\nname: com.acme.BootPin\n"
                        "displayName: overridden\nrecipeList: []\n---\n# only a comment\n")
    own = tmp_path / "own.yaml"
    own.write_text("type: specs.openrewrite.org/v1beta/recipe\nname: com.acme.Own\nrecipeList:\n"
                   "  - org.openrewrite.java.ChangeType:\n      oldFullyQualifiedTypeName: a.B\n"
                   "      newFullyQualifiedTypeName: c.D\n---\ntype: specs.openrewrite.org/v1beta/style\n"
                   "name: com.acme.Style\nstyleConfigs: []\n")
    text = compose_rewrite_yaml([RecipeRef(id="com.acme.Own")], extra_files=[first, override, own])
    d = recipe_documents(text)
    assert d["com.acme.BootPin"]["displayName"] == "overridden"
    assert "com.acme.Own" in d
    assert any(x.get("type") == "specs.openrewrite.org/v1beta/style" for x in docs(text))
    assert text.count("name: com.acme.BootPin") == 1


def test_errors_for_missing_file_invalid_yaml_and_reserved_id(tmp_path, caplog):
    with pytest.raises(FileNotFoundError):
        compose_rewrite_yaml([], extra_files=[tmp_path / "nope.yml"])
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


def test_extra_files_keep_working_without_a_base_dir(tmp_path: Path):
    """The old `base_dir` / `kb_files` keywords are gone."""
    with pytest.raises(TypeError):
        compose_rewrite_yaml([], base_dir=tmp_path)                          # type: ignore[call-arg]
    with pytest.raises(TypeError):
        compose_rewrite_yaml([], kb_files=[])                                # type: ignore[call-arg]
