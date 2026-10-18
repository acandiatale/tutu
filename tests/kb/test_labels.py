from __future__ import annotations

import yaml

from sbmig.core.models import GAV, BlockedItem, BlockReason, BuildError, ErrorKind, FileChange, KBCase, Phase
from sbmig.kb.labels import compile_recipe, label_changes, symbol_for_blocked, symbol_for_errors
from sbmig.kb.recipes import validate_recipe_yaml

JAVA = "src/main/java/com/example/A.java"


def mod(search: str, replace: str, path: str = JAVA) -> FileChange:
    return FileChange(path=path, op="modify", search=search, replace=replace)


def dep(g: str, a: str, v: str | None = None, scope: str | None = None) -> str:
    return (
        "<dependency>\n  <groupId>"
        + g
        + "</groupId>\n  <artifactId>"
        + a
        + "</artifactId>\n"
        + (f"  <version>{v}</version>\n" if v else "")
        + (f"  <scope>{scope}</scope>\n" if scope else "")
        + "</dependency>"
    )


def err(
    symbol: str | None = None,
    message: str = "cannot find symbol",
    location: str | None = None,
    kind: ErrorKind = ErrorKind.COMPILE,
    **kw,
) -> BuildError:
    return BuildError(kind=kind, message=message, symbol=symbol, location=location, **kw)


def case_of(changes, errors=None) -> KBCase:
    res = label_changes(changes, errors or [])
    return KBCase(
        id="20260101-abc",
        changes=changes,
        errors=errors or [],
        fix_labels=res.labels,
        replacement=res.replacement,
        symbol=res.symbol,
        signatures_generic=["compile|x|Y"],
    )


# --------------------------------------------------------------------------- java labels
def test_import_rename_only_imports():
    ch = mod("import javax.annotation.PostConstruct;", "import jakarta.annotation.PostConstruct;")
    r = label_changes([ch])
    assert r.labels == ["import-rename"]
    assert r.replacement == "javax.annotation.PostConstruct → jakarta.annotation.PostConstruct"


def test_type_rename_import_plus_code():
    ch = mod(
        "import org.lib.OldThing;\n\nclass A { OldThing t = new OldThing(); }",
        "import org.lib.NewThing;\n\nclass A { NewThing t = new NewThing(); }",
    )
    r = label_changes([ch])
    assert r.labels[0] == "type-rename" and "import-rename" not in r.labels
    assert r.replacement == "org.lib.OldThing → org.lib.NewThing"


def test_annotation_replace_with_import_pair():
    changes = [
        mod(
            "import org.springframework.boot.jackson.JsonComponent;",
            "import org.springframework.boot.jackson.JacksonComponent;",
        ),
        mod("@JsonComponent\npublic class Money {", "@JacksonComponent\npublic class Money {"),
    ]
    r = label_changes(changes, [err("class JsonComponent")])
    assert r.labels == ["annotation-replace", "type-rename"]
    assert r.replacement.endswith("JsonComponent → org.springframework.boot.jackson.JacksonComponent")
    assert r.symbol == "JsonComponent"


def test_annotation_replace_without_import():
    r = label_changes([mod("@Foo\nvoid x() {}", "@Bar\nvoid x() {}")])
    assert r.labels == ["annotation-replace"] and r.replacement == "@Foo → @Bar"


def test_method_rename_with_owner_from_errors():
    ch = mod("    client.doOldThing(a, b);", "    client.doNewThing(a, b);")
    e = err("method doOldThing(int,int)", location="variable client of type org.lib.Client")
    r = label_changes([ch], [e])
    assert r.labels == ["method-rename"]
    assert r.replacement == "Client.doOldThing → doNewThing" and r.symbol == "doOldThing"
    assert label_changes([ch]).replacement == "doOldThing → doNewThing"


def test_package_rename():
    ch = mod(
        "import javax.servlet.Filter;\nimport javax.servlet.http.HttpServletRequest;",
        "import jakarta.servlet.Filter;\nimport jakarta.servlet.http.HttpServletRequest;",
    )
    r = label_changes([ch])
    assert r.labels == ["package-rename"] and r.replacement == "javax.servlet → jakarta.servlet"
    assert r.details["package_rename"]["recursive"] is True


def test_override_signature():
    ch = mod(
        "@Override\npublic void configure(HttpSecurity http) throws Exception {",
        "@Override\npublic SecurityFilterChain configure(HttpSecurity http) {",
    )
    assert label_changes([ch]).labels == ["override-signature"]


def test_method_signature_adapt():
    ch = mod("http.csrf().disable();", "http.csrf(c -> c.disable());")
    assert label_changes([ch]).labels == ["method-signature-adapt"]


def test_remove_and_add_code():
    assert label_changes([mod("a();\nb();\n", "a();\n")]).labels == ["remove-code"]
    assert label_changes([mod("a();\n", "a();\nb();\n")]).labels == ["add-code"]
    assert label_changes([FileChange(path=JAVA, op="delete")]).labels == ["remove-code"]
    assert label_changes([FileChange(path=JAVA, op="create", content="class A {}")]).labels == ["add-code"]


def test_generic_rewrite_and_whitespace_ignored():
    assert label_changes([mod("int x = 1 + 2", 'String y = "z"')]).labels == ["generic-rewrite"]
    r = label_changes([mod("  int x = 1;\nfoo( a );", "int x = 1;\nfoo(a);")])
    assert r.labels == ["generic-rewrite"]  # nothing but whitespace


# --------------------------------------------------------------------------- pom labels
def test_dependency_add_remove_replace_version():
    r = label_changes(
        [
            mod(
                "<dependencies>\n</dependencies>",
                "<dependencies>\n" + dep("org.foo", "bar", "1.0") + "\n</dependencies>",
                "pom.xml",
            )
        ]
    )
    assert r.labels == ["dependency-add"] and r.replacement == "+ org.foo:bar 1.0"
    r = label_changes([mod(dep("org.foo", "bar", "1.0"), "", "pom.xml")])
    assert r.labels == ["dependency-remove"]
    r = label_changes(
        [
            mod(
                dep("javax.servlet", "javax.servlet-api", "4.0.1"),
                dep("jakarta.servlet", "jakarta.servlet-api", "6.1.0"),
                "pom.xml",
            )
        ]
    )
    assert r.labels == ["dependency-replace"]
    assert r.replacement == "javax.servlet:javax.servlet-api → jakarta.servlet:jakarta.servlet-api"
    r = label_changes([mod(dep("com.foo", "bar", "1.0"), dep("com.foo", "bar", "2.0"), "pom.xml")])
    assert r.labels == ["dependency-version"] and r.replacement == "com.foo:bar 1.0 → 2.0"


def test_partial_version_fragment_uses_error_artifact():
    e = BuildError(
        kind=ErrorKind.DEPENDENCY_RESOLUTION,
        message="could not resolve",
        artifact=GAV(group_id="com.foo", artifact_id="bar", version="1.0"),
    )
    r = label_changes([mod("<version>1.0</version>", "<version>2.0</version>", "pom.xml")], [e])
    assert r.labels == ["dependency-version"] and r.replacement == "com.foo:bar 1.0 → 2.0"
    assert r.symbol == "com.foo:bar"


def test_plugin_version_and_pom_property():
    old = "<plugin>\n<artifactId>maven-compiler-plugin</artifactId>\n<version>3.8.1</version>\n</plugin>"
    new = old.replace("3.8.1", "3.13.0")
    r = label_changes([mod(old, new, "pom.xml")])
    assert r.labels == ["plugin-version"]
    assert r.replacement == "org.apache.maven.plugins:maven-compiler-plugin 3.8.1 → 3.13.0"
    r = label_changes(
        [
            mod(
                "<properties>\n<java.version>11</java.version>\n</properties>",
                "<properties>\n<java.version>21</java.version>\n</properties>",
                "pom.xml",
            )
        ]
    )
    assert r.labels == ["pom-property"] and r.replacement == "java.version: 11 → 21"


# --------------------------------------------------------------------------- config labels
def test_spring_property_rename_properties_and_yaml():
    r = label_changes(
        [
            mod(
                "server.max-http-header-size=16KB",
                "server.max-http-request-header-size=16KB",
                "src/main/resources/application.properties",
            )
        ]
    )
    assert r.labels == ["spring-property-rename"]
    assert r.replacement == "server.max-http-header-size → server.max-http-request-header-size"
    r = label_changes(
        [
            mod(
                "spring:\n  redis:\n    host: x\n",
                "spring:\n  data:\n    redis:\n      host: x\n",
                "svc/src/main/resources/application.yml",
            )
        ]
    )
    assert (
        r.labels == ["spring-property-rename"]
        and r.replacement == "spring.redis.host → spring.data.redis.host"
    )


def test_config_value():
    r = label_changes([mod("a.b=1", "a.b=2", "src/test/resources/application.properties")])
    assert r.labels == ["config-value"] and r.replacement == "a.b: 1 → 2"


def test_labels_ordered_most_specific_first():
    changes = [mod("import org.lib.Old;", "import org.lib.New;"), mod("a();\nb();", "a();")]
    r = label_changes(changes)
    assert r.labels == ["import-rename", "remove-code"]


# --------------------------------------------------------------------------- symbol
def test_symbol_extraction():
    assert symbol_for_errors([err("class WebSecurityConfigurerAdapter")]) == "WebSecurityConfigurerAdapter"
    assert symbol_for_errors([err(None, "package javax.servlet does not exist")]) == "package javax.servlet"
    res = BuildError(
        kind=ErrorKind.DEPENDENCY_RESOLUTION,
        message="x",
        artifact=GAV(group_id="com.company", artifact_id="legacy", version="1"),
    )
    assert symbol_for_errors([res]) == "com.company:legacy"
    assert symbol_for_errors([err(None, "x", signature_generic="compile|cannot_access|Foo")]) == "Foo"
    assert symbol_for_errors([]) == ""
    b = BlockedItem(
        id="b",
        reason=BlockReason.EXTERNAL_JAVAX_API,
        phase=Phase.SPRING,
        cluster_id="c",
        owner_class="com.company.web.LegacyFilter",
        artifact=GAV(group_id="com.company", artifact_id="legacy-web"),
    )
    assert symbol_for_blocked(b) == "com.company.web.LegacyFilter"
    assert symbol_for_blocked(b.model_copy(update={"owner_class": None})) == "com.company:legacy-web"


# --------------------------------------------------------------------------- compile_recipe
def _recipe(text: str) -> dict:
    ok, errors, doc = validate_recipe_yaml(text)
    assert ok, errors
    return doc


def test_compile_type_rename():
    changes = [
        mod(
            "import org.springframework.boot.jackson.JsonComponent;",
            "import org.springframework.boot.jackson.JacksonComponent;",
        ),
        mod("@JsonComponent\nclass M {", "@JacksonComponent\nclass M {"),
    ]
    doc = _recipe(compile_recipe(case_of(changes)))
    assert doc["name"] == "com.company.kb.JsonComponentToJacksonComponent"
    assert doc["recipeList"] == [
        {
            "org.openrewrite.java.ChangeType": {
                "oldFullyQualifiedTypeName": "org.springframework.boot.jackson.JsonComponent",
                "newFullyQualifiedTypeName": "org.springframework.boot.jackson.JacksonComponent",
            }
        }
    ]
    assert "no LLM" in doc["description"]


def test_compile_package_rename():
    ch = mod(
        "import javax.servlet.Filter;\nimport javax.servlet.ServletRequest;",
        "import jakarta.servlet.Filter;\nimport jakarta.servlet.ServletRequest;",
    )
    doc = _recipe(compile_recipe(case_of([ch])))
    assert doc["recipeList"] == [
        {
            "org.openrewrite.java.ChangePackage": {
                "oldPackageName": "javax.servlet",
                "newPackageName": "jakarta.servlet",
            }
        }
    ]


def test_compile_dependency_add_and_version_and_replace():
    add = mod(
        "<dependencies>\n</dependencies>",
        "<dependencies>\n" + dep("org.foo", "bar-baz", "1.2", "test") + "\n</dependencies>",
        "pom.xml",
    )
    doc = _recipe(compile_recipe(case_of([add])))
    assert doc["name"] == "com.company.kb.AddBarBaz"
    assert doc["recipeList"] == [
        {
            "org.openrewrite.java.dependencies.AddDependency": {
                "groupId": "org.foo",
                "artifactId": "bar-baz",
                "version": "1.2",
                "scope": "test",
            }
        }
    ]
    ver = mod(dep("com.foo", "bar", "1.0"), dep("com.foo", "bar", "2.0.1"), "pom.xml")
    doc = _recipe(compile_recipe(case_of([ver])))
    assert doc["recipeList"] == [
        {
            "org.openrewrite.java.dependencies.UpgradeDependencyVersion": {
                "groupId": "com.foo",
                "artifactId": "bar",
                "newVersion": "2.0.1",
            }
        }
    ]
    rep = mod(
        dep("javax.servlet", "javax.servlet-api"), dep("jakarta.servlet", "jakarta.servlet-api"), "pom.xml"
    )
    doc = _recipe(compile_recipe(case_of([rep])))
    assert list(doc["recipeList"][0]) == ["org.openrewrite.java.dependencies.ChangeDependency"]


def test_compile_plugin_version_and_property():
    old = "<plugin>\n<artifactId>maven-war-plugin</artifactId>\n<version>2.6</version>\n</plugin>"
    doc = _recipe(compile_recipe(case_of([mod(old, old.replace("2.6", "3.4.0"), "pom.xml")])))
    assert doc["recipeList"][0]["org.openrewrite.maven.UpgradePluginVersion"]["newVersion"] == "3.4.0"
    doc = _recipe(
        compile_recipe(
            case_of(
                [
                    mod(
                        "<lombok.version>1.18.20</lombok.version>",
                        "<lombok.version>1.18.38</lombok.version>",
                        "pom.xml",
                    )
                ]
            )
        )
    )
    assert doc["recipeList"] == [
        {"org.openrewrite.maven.ChangePropertyValue": {"key": "lombok.version", "newValue": "1.18.38"}}
    ]


def test_compile_spring_property_rename():
    ch = mod("spring.redis.host=x", "spring.data.redis.host=x", "src/main/resources/application.properties")
    doc = _recipe(compile_recipe(case_of([ch])))
    assert doc["recipeList"] == [
        {
            "org.openrewrite.java.spring.ChangeSpringPropertyKey": {
                "oldPropertyKey": "spring.redis.host",
                "newPropertyKey": "spring.data.redis.host",
            }
        }
    ]
    assert yaml.safe_load(compile_recipe(case_of([ch])))["type"] == "specs.openrewrite.org/v1beta/recipe"


def test_compile_none_for_project_owned_logic_or_ambiguous():
    own = mod("import com.example.util.OldHelper;", "import com.example.util.NewHelper;")
    assert label_changes([own]).labels == ["import-rename"]
    assert compile_recipe(case_of([own])) is None  # project-owned FQCN
    logic = mod("http.csrf().disable();", "http.csrf(c -> c.disable());")
    assert compile_recipe(case_of([logic])) is None  # logic change
    mixed = [mod("import org.lib.Old;", "import org.lib.New;"), mod("a();\nb();", "a();")]
    assert compile_recipe(case_of(mixed)) is None  # mechanical + removed code
    two = mod("import org.lib.A;\nimport org.other.B;", "import org.lib2.X;\nimport org.other2.Y;")
    assert compile_recipe(case_of([two])) is None  # not 1:1
    managed = mod(
        "<dependencies>\n</dependencies>",
        "<dependencies>\n" + dep("org.foo", "bar") + "\n</dependencies>",
        "pom.xml",
    )
    assert compile_recipe(case_of([managed])) is None  # AddDependency needs a version
    prop = mod("<v>${a}</v>", "<v>${b}</v>", "pom.xml")
    assert compile_recipe(case_of([prop])) is None  # '$' never passes validation
