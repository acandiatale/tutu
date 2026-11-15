"""`framework.*` (v2.1z-ak): the patterns that name the in-house framework artifacts and the two policy switches."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from sbmig.core.config import FrameworkConfig, load_config
from sbmig.core.models import FrameworkEvidence, FrameworkUse, MigrationPlan


def test_shipped_defaults_switch_the_check_off():
    cfg = load_config("config/artifacts.example.yml")
    assert cfg.framework.artifacts == []
    assert cfg.framework.enabled is False
    assert cfg.framework.scope == "jdk-jakarta"
    assert cfg.framework.transitive == "apply"
    # the remediation treats the framework as an in-house artifact group; without patterns the group holds nothing
    assert cfg.remediation.inhouse_groups == ["mandatory", "optional", "parents", "framework"]


def test_blank_patterns_are_ignored():
    f = FrameworkConfig(artifacts=["", "  ", "example-fw-core "])
    assert f.patterns() == ["example-fw-core"]
    assert f.enabled is True
    assert FrameworkConfig(artifacts=[" "]).enabled is False


@pytest.mark.parametrize("pattern,group,artifact,expected", [
    ("example-fw-core", "com.example.fw", "example-fw-core", True),        # artifactId only: any group
    ("example-fw-core", "other.group", "example-fw-core", True),
    ("example-fw-core", None, "example-fw-core", True),
    ("example-fw-core", "com.example.fw", "example-fw-core-web", False),
    ("example-fw-core", "com.example.fw", "Example-Fw-Core", False),       # case sensitive
    ("com.example.fw:example-fw-core", "com.example.fw", "example-fw-core", True),
    ("com.example.fw:example-fw-core", "other.group", "example-fw-core", False),
    ("com.example.fw:example-fw-*", "com.example.fw", "example-fw-web", True),
    ("com.example.*:example-fw-core", "com.example.fw", "example-fw-core", True),
    ("example-fw-*", "x", "example-fw-biz", True),
    ("example-fw-core", "x", "", False),
    ("example-fw-core", "x", None, False),
])
def test_matches(pattern, group, artifact, expected):
    assert FrameworkConfig(artifacts=[pattern]).matches(group, artifact) is expected


def test_matches_key_takes_ga_and_longer_coordinates():
    f = FrameworkConfig(artifacts=["com.example.fw:example-fw-core"])
    assert f.matches_key("com.example.fw:example-fw-core")
    assert f.matches_key("com.example.fw:example-fw-core:jar:1.2.0:compile")
    assert not f.matches_key("example-fw-core")
    assert not f.matches_key("")
    assert not f.matches_key(None)


@pytest.mark.parametrize("name,expected", [
    ("example-fw-core.jar", True),
    ("example-fw-core-1.2.0.jar", True),
    ("WebContent/WEB-INF/lib/example-fw-core-1.2.0-SNAPSHOT.jar", True),
    ("WEB-INF\\lib\\example-fw-core-2.jar", True),
    ("example-fw-core-web-1.0.jar", False),      # another artifact whose name starts the same
    ("example-fw-corex.jar", False),
    ("other.jar", False),
    ("", False),
    (None, False),
])
def test_matches_jar_by_file_name(name, expected):
    assert FrameworkConfig(artifacts=["com.example.fw:example-fw-core"]).matches_jar(name) is expected


def test_matches_jar_never_matches_on_a_bare_star():
    assert FrameworkConfig(artifacts=["com.example.fw:*"]).matches_jar("anything-1.0.jar") is False
    assert FrameworkConfig(artifacts=["example-fw-*"]).matches_jar("example-fw-biz-3.1.jar") is True


def test_maven_includes():
    f = FrameworkConfig(artifacts=["example-fw-core", "com.example.fw:example-fw-*"])
    assert f.maven_includes() == ["*:example-fw-core", "com.example.fw:example-fw-*"]


def test_unknown_policy_values_are_rejected():
    with pytest.raises(ValidationError):
        FrameworkConfig(scope="jdk")
    with pytest.raises(ValidationError):
        FrameworkConfig(transitive="yes")


def test_framework_use_helpers():
    use = FrameworkUse(patterns=["example-fw-core"], evidence=[
        FrameworkEvidence(source="direct", artifact="com.example.fw:example-fw-core", version="1.2.0", where="pom.xml"),
        FrameworkEvidence(source="direct", artifact="com.example.fw:example-fw-core", version="1.2.0", where="web/pom.xml"),
        FrameworkEvidence(source="bundled", artifact="example-fw-core-1.2.0.jar", where="WEB-INF/lib/example-fw-core-1.2.0.jar"),
    ])
    assert use.found and not use.reduced
    assert use.keys() == ["com.example.fw:example-fw-core"]          # a jar without coordinates has no g:a
    use.scope = "jdk-jakarta"
    assert use.reduced
    assert not FrameworkUse().found


def test_plan_scope_defaults_to_full():
    from sbmig.core.models import CaseType
    plan = MigrationPlan(profile_fingerprint="x", case_type=CaseType.PURE_JAVA, steps=[])
    assert plan.scope == "full"


def test_recipe_sets_default_and_validation():
    assert FrameworkConfig().recipe_sets == ["jdk21", "jakarta"]
    f = FrameworkConfig(recipe_sets=["jdk21", "jakarta", "servlet_jakarta_10", "jakarta"])
    assert f.recipe_sets == ["jdk21", "jakarta", "servlet_jakarta_10"]          # duplicates dropped, order kept
    with pytest.raises(ValidationError, match="unknown recipe set"):
        FrameworkConfig(recipe_sets=["jdk21", "boot4"])
    with pytest.raises(ValidationError, match="unknown recipe set"):
        FrameworkConfig(recipe_sets=["extra"])                                   # a dict of lists, not a set


def test_shipped_recipe_sets():
    assert load_config("config/artifacts.example.yml").framework.recipe_sets == ["jdk21", "jakarta"]
