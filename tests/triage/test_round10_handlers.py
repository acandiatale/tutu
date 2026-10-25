"""Round 10 deterministic handlers: jdk-base64-shim, boot4-type-relocation-sweep, security-path-pattern-matcher,
spring-data-specification-unrestricted, remove-tools-jar; add-known-dependency rows / scope; Undertow relocation."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.models import GAV, BuildError, CaseType, ErrorCluster, ErrorKind, PomInfo
from sbmig.core.workspace import Workspace
from sbmig.triage import HandlerRegistry
from sbmig.triage.handlers.jdk_compat import SHIM_SOURCES, JdkBase64Shim, point_to_shims, source_root
from sbmig.triage.handlers.pom_handlers import AddKnownDependency, known_package_table, relocation_table
from sbmig.triage.handlers.spring7_sources import (
    Boot4TypeRelocationSweep,
    SpringDataSpecificationUnrestricted,
    SpringSecurityPathPatternMatcher,
    boot4_type_table,
    boot_below_4_declared,
    pathpattern_problem,
    relocate_types,
    rewrite_request_matchers,
    rewrite_specification_where_null,
)

from .conftest import make_profile


def _ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    return Workspace(root, tmp_path / "run", "t")


def _write(ws: Workspace, rel: str, text: str) -> None:
    p = ws.root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _err(message: str, file: str = "src/main/java/a/A.java", raw: str | None = None,
         kind: ErrorKind = ErrorKind.COMPILE) -> BuildError:
    return BuildError(kind=kind, message=message, raw=raw or message, file=file, line=3)


def _cluster(*errs: BuildError, kind: ErrorKind = ErrorKind.COMPILE) -> ErrorCluster:
    return ErrorCluster(id="c1", kind=kind, file=errs[0].file, errors=list(errs))


# ---------------------------------------------------------------------------------------------------------------------
# D5 jdk-base64-shim
# ---------------------------------------------------------------------------------------------------------------------
CODEC = """package com.example.util;

import java.io.IOException;

import sun.misc.BASE64Decoder;
import sun.misc.BASE64Encoder;

public final class Codec {
    static String enc(byte[] b) { return new BASE64Encoder().encode(b); }
    static byte[] dec(String s) {
        try { return new BASE64Decoder().decodeBuffer(s); } catch (IOException e) { return null; }
    }
    static String fq(byte[] b) { return new sun.misc.BASE64Encoder().encodeBuffer(b); }  // sun.misc.BASE64Encoder
    static final String NAME = "sun.misc.BASE64Decoder";
}
"""


def test_point_to_shims_rewrites_code_but_not_literals_or_comments():
    new, needed = point_to_shims(CODEC)
    assert needed == {"BASE64Encoder", "BASE64Decoder"}
    assert "import sbmig.compat.BASE64Decoder;" in new and "import sbmig.compat.BASE64Encoder;" in new
    assert "new sbmig.compat.BASE64Encoder().encodeBuffer(b)" in new
    assert "// sun.misc.BASE64Encoder" in new and '"sun.misc.BASE64Decoder"' in new
    assert "catch (IOException e)" in new                    # call sites unchanged: decodeBuffer still throws it
    assert point_to_shims(new)[0] == new                       # idempotent


def test_point_to_shims_wildcards_and_name_clash():
    sun_wild = "package a;\nimport sun.misc.*;\nclass W { String e(byte[] b) { return new BASE64Encoder().encode(b); } Unsafe u; }\n"
    new, needed = point_to_shims(sun_wild)
    assert "import sun.misc.*;" in new and "import sbmig.compat.BASE64Encoder;" in new and needed == {"BASE64Encoder"}
    xerces = ("package a;\nimport com.sun.org.apache.xerces.internal.impl.dv.util.*;\n"
              "class X { String e(byte[] b) { return Base64.encode(b); } }\n")
    new, needed = point_to_shims(xerces)
    assert "xerces" not in new and "import sbmig.compat.Base64;" in new and needed == {"Base64"}
    # java.util.Base64 imported by name already decides what `Base64` means: no clashing shim import
    clash = ("package a;\nimport java.util.Base64;\nimport com.sun.org.apache.xerces.internal.impl.dv.util.*;\n"
             "class X { String e(byte[] b) { return Base64.getEncoder().encodeToString(b); } }\n")
    new, _ = point_to_shims(clash)
    assert "import sbmig.compat.Base64;" not in new and "import java.util.Base64;" in new


def test_source_root():
    assert source_root("m/src/main/java/com/example/util/Codec.java", CODEC) == "m/src/main/java"
    assert source_root("APP/src/com/example/util/Codec.java", CODEC) == "APP/src"
    assert source_root("com/example/util/Codec.java", CODEC) == ""
    assert source_root("src/main/java/wrong/Codec.java", CODEC) is None


def test_shim_handler_places_one_shim_set_per_module_and_is_idempotent(tmp_path):
    ws = _ws(tmp_path)
    _write(ws, "pom.xml", "<project/>")
    _write(ws, "src/main/java/com/example/util/Codec.java", CODEC)
    _write(ws, "src/test/java/com/example/util/CodecTest.java",
           "package com.example.util;\nimport sun.misc.BASE64Decoder;\nclass CodecTest { "
           "byte[] t(String s) throws Exception { return new BASE64Decoder().decodeBuffer(s); } }\n")
    _write(ws, "web/src/test/java/com/example/web/T.java",
           "package com.example.web;\nclass T { String x = com.sun.org.apache.xerces.internal.impl.dv.util.Base64"
           ".encode(new byte[0]); }\n")
    h = JdkBase64Shim()
    paths = sorted(c.path for c in h.apply(ws, None, make_profile(), None))
    assert "src/main/java/sbmig/compat/BASE64Encoder.java" in paths
    assert "src/main/java/sbmig/compat/BASE64Decoder.java" in paths
    assert not any(p.startswith("src/test/java/sbmig/") for p in paths)        # main root of the module serves tests
    assert "web/src/test/java/sbmig/compat/Base64.java" in paths               # test-only module: its own copy
    assert h.flags == ["jdk-base64-shimmed"] and "76-character" in h.notes[0]
    assert ws.read("src/main/java/sbmig/compat/BASE64Encoder.java") == SHIM_SOURCES["BASE64Encoder"]
    assert "sbmig.compat.Base64.encode" in ws.read("web/src/test/java/com/example/web/T.java")
    assert h.apply(ws, None, make_profile(), None) == []                       # idempotent, shims never overwritten


def test_shim_handler_reactive_trigger_and_registry(tmp_path):
    reg = HandlerRegistry.default(None)
    c = _cluster(_err("cannot find symbol", raw="error: cannot find symbol\n  symbol:   class BASE64Encoder\n"
                                                 "  location: package sun.misc"))
    assert "jdk-base64-shim" in [h.id for h in reg.matching(c, make_profile(), None)]
    assert JdkBase64Shim().applicable(None, make_profile(libs=(("jdk-removed-apis", None),)), None)
    assert not JdkBase64Shim().applicable(None, make_profile(), None)


def test_shim_sources_keep_the_jdk8_contract():
    enc, dec, x = SHIM_SOURCES["BASE64Encoder"], SHIM_SOURCES["BASE64Decoder"], SHIM_SOURCES["Base64"]
    assert "getMimeEncoder(76" in enc and "System.lineSeparator()" in enc and "BYTES_PER_LINE = 57" in enc
    assert dec.count("throws IOException") >= 5 and "getMimeDecoder()" in dec
    assert "return null;" in x and "len % 4 != 0" in x and "'\\t'" in x
    assert all(s.splitlines()[1] == "package sbmig.compat;" for s in SHIM_SOURCES.values())


# ---------------------------------------------------------------------------------------------------------------------
# D4 boot4-type-relocation-sweep
# ---------------------------------------------------------------------------------------------------------------------
def test_boot4_table_matches_the_project_recipe():
    """boot4_types.yml and com.company.migrate.Boot4TypeRelocations list the same moves."""
    import yaml

    moves, gone = boot4_type_table()
    doc = next(d for d in yaml.safe_load_all((Path(__file__).resolve().parents[2] / "knowledge" / "recipes" / "base"
                                              / "spring-7-leftovers.yml").read_text(encoding="utf-8"))
               if d and d.get("name") == "com.company.migrate.Boot4TypeRelocations")
    pairs = {(r["org.openrewrite.java.ChangeType"]["oldFullyQualifiedTypeName"],
              r["org.openrewrite.java.ChangeType"]["newFullyQualifiedTypeName"]) for r in doc["recipeList"]}
    assert pairs == {(m.from_, m.to) for m in moves} and len(moves) == 25
    assert "org.springframework.boot.web.servlet.error" in gone and "org.springframework.boot.web.servlet" not in gone


def test_relocate_types_imports_fqn_wildcards_literals():
    moves, gone = boot4_type_table()
    src = ("package a;\nimport org.springframework.boot.web.servlet.*;\nimport org.springframework.boot.web.servlet.error.*;\n"
           "import org.springframework.boot.autoconfigure.web.ServerProperties;\n"
           "class A implements ErrorController { MultipartConfigFactory f; FilterRegistrationBean<?> r; ServerProperties p;\n"
           "  String s = \"org.springframework.boot.web.server.ErrorPage\";\n"
           "  org.springframework.boot.web.server.ErrorPage e; ServerProperties.Servlet sv; }\n")
    res = relocate_types(src, moves, gone)
    t = res.text
    assert "import org.springframework.boot.web.servlet.*;" in t                   # live package: wildcard kept
    assert "import org.springframework.boot.web.servlet.error.*;" not in t        # gone package: wildcard dropped
    assert "import org.springframework.boot.webmvc.error.ErrorController;" in t
    assert "import org.springframework.boot.servlet.MultipartConfigFactory;" in t
    assert "import org.springframework.boot.web.server.autoconfigure.ServerProperties;" in t
    assert "org.springframework.boot.web.error.ErrorPage e;" in t
    assert '"org.springframework.boot.web.server.ErrorPage"' in t                  # literal untouched
    assert "FilterRegistrationBean" not in "".join(res.imports_added)            # still in web.servlet
    assert relocate_types(t, moves, gone).text == t                               # idempotent


def test_boot4_sweep_preemptive_guard_and_reactive(tmp_path):
    ws = _ws(tmp_path)
    _write(ws, "pom.xml", "<project><parent><groupId>org.springframework.boot</groupId>"
                          "<artifactId>spring-boot-starter-parent</artifactId><version>${boot.version}</version></parent>"
                          "<properties><boot.version>2.7.18</boot.version></properties></project>")
    _write(ws, "src/main/java/a/E.java",
           "package a;\nimport org.springframework.boot.web.servlet.error.ErrorController;\nclass E implements ErrorController {}\n")
    prof = make_profile(boot="2.7.18", case=CaseType.SPRING_BOOT)
    h = Boot4TypeRelocationSweep()
    assert boot_below_4_declared(ws) == "pom.xml: 2.7.18"
    assert h.apply(ws, None, prof, None) == []                                   # Boot 2 still pinned: not yet
    c = _cluster(_err("package org.springframework.boot.web.servlet.error does not exist", file="src/main/java/a/E.java"))
    assert h.triggered(c)
    assert [x.path for x in h.apply(ws, c, prof, None)] == ["src/main/java/a/E.java"]
    assert "org.springframework.boot.webmvc.error.ErrorController" in ws.read("src/main/java/a/E.java")
    assert h.flags == ["boot4-types-relocated"]
    _write(ws, "pom.xml", "<project><parent><groupId>org.springframework.boot</groupId>"
                          "<artifactId>spring-boot-starter-parent</artifactId><version>4.1.0</version></parent></project>")
    assert boot_below_4_declared(ws) is None
    assert not h.applicable(None, make_profile(boot=None, case=CaseType.SPRING_LEGACY), None)   # non-Boot: never


def test_boot4_sweep_live_package_trigger():
    raw = "error: cannot find symbol\n  symbol:   class MultipartConfigFactory\n  location: class a.WebConfig"
    assert Boot4TypeRelocationSweep().triggered(_cluster(_err("cannot find symbol", raw=raw)))


# ---------------------------------------------------------------------------------------------------------------------
# D6 security-path-pattern-matcher
# ---------------------------------------------------------------------------------------------------------------------
MATCHERS = """package com.example.security;

import java.util.ArrayList;
import java.util.List;
import org.springframework.security.web.util.matcher.AntPathRequestMatcher;
import org.springframework.security.web.util.matcher.RequestMatcher;

public class UrlMatchers {
    private final List<AntPathRequestMatcher> matchers = new ArrayList<>();

    void load(String url, String method) {
        matchers.add(new AntPathRequestMatcher(url));
        matchers.add(new AntPathRequestMatcher("/admin/**", "POST"));
        matchers.add(new AntPathRequestMatcher(url, method));
        matchers.add(new AntPathRequestMatcher("/**/*.do", null));
        RequestMatcher m = AntPathRequestMatcher.antMatcher("/api/(x),y/**");
        // new AntPathRequestMatcher("/comment")
        String s = "new AntPathRequestMatcher(\\"x\\")";
    }
}
"""


def test_rewrite_request_matchers():
    r = rewrite_request_matchers(MATCHERS)
    t = r.text
    assert r.rewritten == 5 and r.left == [] and r.todos == ["/**/*.do"]
    assert "import org.springframework.security.web.util.matcher.AntPathRequestMatcher;" not in t
    assert "import org.springframework.security.web.servlet.util.matcher.PathPatternRequestMatcher;" in t
    assert "import org.springframework.http.HttpMethod;" in t
    assert "List<RequestMatcher> matchers" in t
    assert "PathPatternRequestMatcher.pathPattern(url)" in t
    assert 'PathPatternRequestMatcher.pathPattern(HttpMethod.POST, "/admin/**")' in t
    assert ("(org.springframework.util.StringUtils.hasText(method) ? PathPatternRequestMatcher.pathPattern("
            "HttpMethod.valueOf(method), url) : PathPatternRequestMatcher.pathPattern(url))") in t
    assert 'PathPatternRequestMatcher.pathPattern("/api/(x),y/**")' in t          # comma / parens inside the literal
    todo = t.index("// TODO sbmig: PathPattern allows ** only as the last path segment")
    assert todo < t.index('pathPattern("/**/*.do")')
    assert '// new AntPathRequestMatcher("/comment")' in t and 'String s = "new AntPathRequestMatcher(' in t
    assert rewrite_request_matchers(t).text == t


def test_rewrite_request_matchers_leaves_what_it_cannot_prove():
    src = ("package a;\nimport org.springframework.security.web.util.matcher.AntPathRequestMatcher;\n"
           "class B { Object m = new AntPathRequestMatcher(\"/x\", \"GET\", false); }\n")
    r = rewrite_request_matchers(src)
    assert r.text == src and r.rewritten == 0 and r.left
    mvc = ("package a;\nimport org.springframework.security.web.servlet.util.matcher.MvcRequestMatcher;\n"
           "class C { Object m = new MvcRequestMatcher(introspector, \"/x/**\"); }\n")
    t = rewrite_request_matchers(mvc).text
    assert 'PathPatternRequestMatcher.pathPattern("/x/**")' in t and "MvcRequestMatcher" not in t


@pytest.mark.parametrize("pattern,bad", [("/**", False), ("/api/**", False), ("**", False), ("/a/*.do", False),
                                         ("/**/*.do", True), ("/api/**/list", True), ("/a**", True),
                                         ("/a/**/b/**", True)])
def test_pathpattern_problem(pattern, bad):
    assert pathpattern_problem(pattern) is bad


def test_security_matcher_handler_is_reactive(tmp_path):
    ws = _ws(tmp_path)
    _write(ws, "src/main/java/com/example/security/UrlMatchers.java", MATCHERS)
    h = SpringSecurityPathPatternMatcher()
    assert h.apply(ws, None, make_profile(), None) == []
    c = _cluster(_err("cannot find symbol", raw="symbol:   class AntPathRequestMatcher\n  location: package "
                      "org.springframework.security.web.util.matcher"))
    assert h.matches(c, make_profile(), None)
    assert [x.path for x in h.apply(ws, c, make_profile(), None)] == ["src/main/java/com/example/security/UrlMatchers.java"]
    assert h.flags == ["security-path-matchers-rewritten"] and any("TODO sbmig" in n for n in h.notes)


# ---------------------------------------------------------------------------------------------------------------------
# D10 spring-data-specification-unrestricted
# ---------------------------------------------------------------------------------------------------------------------
def test_specification_where_null():
    src = ("import org.springframework.data.jpa.domain.Specification;\nclass A {\n"
           "  Specification<U> s = Specification.where(null);\n"
           "  Specification<U> t = Specification.<U>where(null).and(x);\n"
           "  Specification<U> k = Specification.where(spec);\n"
           "  String z = \"Specification.where(null)\";\n}\n")
    new, n = rewrite_specification_where_null(src)
    assert n == 2
    assert "Specification<U> s = Specification.unrestricted();" in new
    assert "Specification.<U>unrestricted().and(x)" in new and "Specification.where(spec)" in new
    assert '"Specification.where(null)"' in new
    static = ("import static org.springframework.data.jpa.domain.Specification.where;\nclass B {\n"
              "  Object s = where(null);\n}\n")
    new, n = rewrite_specification_where_null(static)
    assert n == 1 and "Object s = Specification.unrestricted();" in new
    assert "import org.springframework.data.jpa.domain.Specification;" in new


def test_specification_handler(tmp_path):
    ws = _ws(tmp_path)
    _write(ws, "src/main/java/a/Repo.java", "package a;\nimport org.springframework.data.jpa.domain.Specification;\n"
                                            "class Repo { Specification<String> s = Specification.where(null); }\n")
    raw = ("error: reference to where is ambiguous\n  both method <T#1>where(Specification<T#1>) in Specification and "
           "method <T#2>where(PredicateSpecification<T#2>) in Specification match")
    c = _cluster(_err("reference to where is ambiguous", file="src/main/java/a/Repo.java", raw=raw))
    h = SpringDataSpecificationUnrestricted()
    assert h.matches(c, make_profile(), None) and h.apply(ws, None, make_profile(), None) == []
    assert [x.path for x in h.apply(ws, c, make_profile(), None)] == ["src/main/java/a/Repo.java"]
    assert "Specification.unrestricted()" in ws.read("src/main/java/a/Repo.java")
    assert not h.matches(_cluster(_err("reference to foo is ambiguous")), make_profile(), None)


# ---------------------------------------------------------------------------------------------------------------------
# D12 remove-tools-jar
# ---------------------------------------------------------------------------------------------------------------------
TOOLS_POM = """<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>a</groupId><artifactId>b</artifactId><version>1</version>
  <dependencies>
    <dependency>
      <groupId>com.sun</groupId>
      <artifactId>tools</artifactId>
      <version>1.8</version>
      <scope>system</scope>
      <systemPath>${java.home}/../lib/tools.jar</systemPath>
    </dependency>
    <dependency>
      <groupId>org.apache.hadoop</groupId>
      <artifactId>hadoop-common</artifactId>
      <version>2.7.3</version>
      <exclusions><exclusion><groupId>jdk.tools</groupId><artifactId>jdk.tools</artifactId></exclusion></exclusions>
    </dependency>
    <!-- <dependency><groupId>jdk.tools</groupId><artifactId>jdk.tools</artifactId></dependency> -->
  </dependencies>
  <profiles><profile><id>jdk8</id><dependencies>
    <dependency><groupId>jdk.tools</groupId><artifactId>jdk.tools</artifactId><version>1.8</version>
      <scope>system</scope><systemPath>${JAVA_HOME}/lib/tools.jar</systemPath></dependency>
  </dependencies></profile></profiles>
</project>
"""


def test_remove_tools_jar(kit):
    (kit.root / "pom.xml").write_text(TOOLS_POM, encoding="utf-8")
    h = kit.handler("remove-tools-jar")
    c = _cluster(_err("'dependencies.dependency.systemPath' for com.sun:tools:jar refers to a non-existing file "
                      "/opt/jdk21/../lib/tools.jar", file="pom.xml", kind=ErrorKind.POM_INVALID),
                 kind=ErrorKind.POM_INVALID)
    assert h.matches(c, make_profile(), None)
    assert [x.path for x in h.apply(kit.ws, c, make_profile(), None)] == ["pom.xml"]
    text = kit.text()
    assert "<artifactId>tools</artifactId>" not in text and "${JAVA_HOME}/lib/tools.jar" not in text
    assert "<artifactId>hadoop-common</artifactId>" in text and "<exclusion><groupId>jdk.tools</groupId>" in text
    assert "<!-- <dependency><groupId>jdk.tools</groupId>" in text                  # comments untouched
    assert h.flags == ["tools-jar-removed"]
    assert h.apply(kit.ws, None, make_profile(), None) == []


def test_tools_jar_preemptive_reason(cfg):
    from sbmig.plan import pre_handlers_for

    prof = make_profile(poms=[PomInfo(path="pom.xml", artifact_id="b", dependencies=[
        GAV(group_id="com.sun", artifact_id="tools", version="1.8", scope="system")])])
    assert "remove-tools-jar" in pre_handlers_for(prof, cfg)


# ---------------------------------------------------------------------------------------------------------------------
# D3 / D14 known packages, D13 scope, D11 relocation
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("ref,sym,artifact", [
    ("org.springframework.security.access", "AccessDecisionManager", "org.springframework.security:spring-security-access"),
    ("org.springframework.security.access", "ConfigAttribute", "org.springframework.security:spring-security-access"),
    ("org.springframework.security.access.vote", "", "org.springframework.security:spring-security-access"),
    ("org.springframework.security.access.vote.AffirmativeBased", "", "org.springframework.security:spring-security-access"),
    ("org.springframework.security.web.access.intercept", "FilterSecurityInterceptor",
     "org.springframework.security:spring-security-access"),
    ("org.springframework.security.web.access.expression", "WebExpressionVoter",
     "org.springframework.security:spring-security-access"),
    ("org.springframework.security.access", "AccessDeniedException", None),          # still in spring-security-core
    ("org.springframework.security.web.access.intercept", "AuthorizationFilter", None),   # still in -web
    ("org.apache.log4j", "Logger", "org.apache.logging.log4j:log4j-1.2-api"),
    ("org.springframework.boot.webmvc.error", "ErrorController", "org.springframework.boot:spring-boot-webmvc"),
    ("org.springframework.boot.webmvc.test.autoconfigure", "", "org.springframework.boot:spring-boot-starter-webmvc-test"),
])
def test_round10_known_packages(ref, sym, artifact):
    known_package_table.cache_clear()
    row = AddKnownDependency._lookup(ref, sym)
    assert (row.artifact if row else None) == artifact
    if row is not None and artifact.startswith("org.springframework.security"):
        assert row.managed and row.version == "7.1.0" and row.scope is None


def test_test_library_in_main_code_gets_compile_scope(kit):
    kit.pom()
    h = kit.handler("add-known-dependency")
    main = _cluster(_err("package org.junit.experimental.categories does not exist",
                         file="src/main/java/a/TestSupport.java"))
    h.apply(kit.ws, main, make_profile(), None)
    dep = kit.dep("junit:junit")
    assert dep is not None and dep.scope in (None, "compile")
    test = _cluster(_err("package org.mockito does not exist", file="src/test/java/a/ATest.java",
                         kind=ErrorKind.TEST_COMPILE), kind=ErrorKind.TEST_COMPILE)
    h.apply(kit.ws, test, make_profile(), None)
    assert kit.dep("org.mockito:mockito-core").scope == "test"
    helper = _cluster(_err("package org.hamcrest does not exist", file="src/test/java/a/BTest.java"))   # compile goal
    h.apply(kit.ws, helper, make_profile(), None)
    assert kit.dep("org.hamcrest:hamcrest").scope == "test"


def test_undertow_relocation_row():
    rows = {r.from_: r for r in relocation_table()}
    r = rows["org.springframework.boot:spring-boot-starter-undertow"]
    assert r.to == "org.springframework.boot:spring-boot-starter-tomcat" and r.min_target_boot == 4 and not r.packages


def test_registry_order_round10():
    ids = HandlerRegistry.default(None).ids()
    for hid in ("jdk-base64-shim", "boot4-type-relocation-sweep", "security-path-pattern-matcher",
                "spring-data-specification-unrestricted"):
        assert ids.index("spring-data-crud-repository") < ids.index(hid) < ids.index("add-known-dependency")
    assert ids.index("remove-tools-jar") < ids.index("systempath-relocate")
