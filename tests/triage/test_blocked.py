"""Blocked-cluster classification (X1–X6) on realistic Maven output + the decision policy (blocked_policy)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.build import parse_maven_log
from sbmig.core.interfaces import TriageContext
from sbmig.core.models import (BlockReason, DecisionKind, ErrorKind, FixAttempt, Phase, SourceStats,
                               TriageDecision)
from sbmig.core.workspace import Workspace
from sbmig.loop.blocked_registry import BlockedRegistry
from sbmig.triage import HandlerRegistry, Triager, classify_blocked, cluster_errors, extract_owner, is_project_class

from .conftest import make_profile

ROOT = "/work/proj"
COMPILE_HDR = ("[INFO] --- maven-compiler-plugin:3.13.0:compile (default-compile) @ demo ---\n"
               "[ERROR] COMPILATION ERROR : \n")
COMPILE_FTR = ("[INFO] 1 error\n[INFO] BUILD FAILURE\n"
               "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.13.0:compile "
               "(default-compile) on project demo: Compilation failure\n")

CTOR_JAVAX = (
    "[ERROR] /work/proj/src/main/java/com/acme/web/Foo.java:[12,9] constructor LegacyRequestHelper in class "
    "com.company.legacy.web.LegacyRequestHelper cannot be applied to given types;\n"
    "  required: javax.servlet.http.HttpServletRequest\n"
    "  found:    jakarta.servlet.http.HttpServletRequest\n"
    "  reason: argument mismatch; jakarta.servlet.http.HttpServletRequest cannot be converted to "
    "javax.servlet.http.HttpServletRequest\n")
INCOMPATIBLE = ("[ERROR] /work/proj/src/main/java/com/acme/web/Bar.java:[20,30] incompatible types: "
                "jakarta.servlet.http.HttpServletRequest cannot be converted to javax.servlet.http.HttpServletRequest\n")
METHOD_JAVAX = (
    "[ERROR] /work/proj/src/main/java/com/acme/web/Ctl.java:[33,15] method audit in class "
    "com.company.legacy.audit.AuditLogger<T> cannot be applied to given types;\n"
    "  required: javax.servlet.http.HttpServletRequest,java.lang.String\n"
    "  found:    jakarta.servlet.http.HttpServletRequest,java.lang.String\n"
    "  reason: argument mismatch; jakarta.servlet.http.HttpServletRequest cannot be converted to "
    "javax.servlet.http.HttpServletRequest\n")
NO_SUITABLE = (
    "[ERROR] /work/proj/src/main/java/com/acme/web/Q2.java:[9,13] no suitable constructor found for "
    "Wrapper(jakarta.servlet.http.HttpServletRequest)\n"
    "    constructor com.company.legacy.Wrapper.Wrapper(javax.servlet.http.HttpServletRequest) is not applicable\n"
    "      (argument mismatch; jakarta.servlet.http.HttpServletRequest cannot be converted to "
    "javax.servlet.http.HttpServletRequest)\n"
    "    constructor com.company.legacy.Wrapper.Wrapper() is not applicable\n"
    "      (actual and formal argument lists differ in length)\n")
OVERRIDE = ("[ERROR] /work/proj/src/main/java/com/acme/web/MyFilter.java:[8,8] com.acme.web.MyFilter is not abstract "
            "and does not override abstract method doFilter(javax.servlet.ServletRequest,javax.servlet.ServletResponse,"
            "javax.servlet.FilterChain) in com.company.legacy.web.LegacyFilter\n")
CANNOT_ACCESS = ("[ERROR] /work/proj/src/main/java/com/acme/web/C.java:[14,20] cannot access "
                 "javax.servlet.http.HttpServletRequest\n"
                 "  class file for javax.servlet.http.HttpServletRequest not found\n")
PKG_MISSING = "[ERROR] /work/proj/src/main/java/com/acme/web/D.java:[3,22] package javax.servlet.http does not exist\n"
SYMBOL_JAVAX = ("[ERROR] /work/proj/src/main/java/com/acme/web/E.java:[10,5] cannot find symbol\n"
                "  symbol:   class HttpServletRequest\n  location: class com.acme.web.E\n")
LIB_SYMBOL = ("[ERROR] /work/proj/src/main/java/com/acme/svc/Svc.java:[7,13] cannot find symbol\n"
              "  symbol:   method oldMethod(java.lang.String)\n  location: class com.company.lib.Util\n")
LIB_VAR_SYMBOL = ("[ERROR] /work/proj/src/main/java/com/acme/svc/Svc2.java:[7,13] cannot find symbol\n"
                  "  symbol:   method legacyFind(long)\n  location: variable client of type com.vendor.sdk.Client\n")
SPRING_SYMBOL = ("[ERROR] /work/proj/src/main/java/com/acme/svc/Sec.java:[7,13] cannot find symbol\n"
                 "  symbol:   method antMatchers(java.lang.String)\n"
                 "  location: class org.springframework.security.config.annotation.web.builders.HttpSecurity\n")
LIB_APPLIED = ("[ERROR] /work/proj/src/main/java/com/acme/svc/Svc3.java:[21,15] method send in class "
               "com.vendor.mq.Producer cannot be applied to given types;\n"
               "  required: byte[]\n  found:    java.lang.String\n  reason: argument mismatch\n")
REVERSE = ("[ERROR] /work/proj/src/main/java/com/acme/web/R.java:[5,20] incompatible types: "
           "javax.servlet.http.HttpServletRequest cannot be converted to jakarta.servlet.http.HttpServletRequest\n")


def parse(*blocks: str):
    return parse_maven_log(COMPILE_HDR + "".join(blocks) + COMPILE_FTR, ROOT)


def one(*blocks: str):
    clusters = cluster_errors(parse(*blocks))
    assert len(clusters) == 1, clusters
    return clusters[0]


@pytest.fixture()
def proj(tmp_path: Path) -> Workspace:
    root = tmp_path / "p"
    (root / "src/main/java/com/acme/web").mkdir(parents=True)
    (root / "src/main/java/com/acme/web/Foo.java").write_text(
        "package com.acme.web;\nimport jakarta.servlet.http.HttpServletRequest;\nclass Foo {}\n")
    return Workspace(root, tmp_path / "run", "t")


# --------------------------------------------------------------------------------------------- X1
@pytest.mark.parametrize("block,owner,required", [
    (CTOR_JAVAX, "com.company.legacy.web.LegacyRequestHelper", "javax.servlet.http.HttpServletRequest"),
    (METHOD_JAVAX, "com.company.legacy.audit.AuditLogger", "javax.servlet.http.HttpServletRequest"),
    (NO_SUITABLE, "com.company.legacy.Wrapper", "javax.servlet.http.HttpServletRequest"),
    (OVERRIDE, "com.company.legacy.web.LegacyFilter", "javax.servlet.ServletRequest"),
    (INCOMPATIBLE, None, "javax.servlet.http.HttpServletRequest"),
    (CANNOT_ACCESS, None, "javax.servlet.http.HttpServletRequest"),
])
def test_x1_external_javax_api(block, owner, required, proj):
    c = one(block)
    for ws in (None, proj):
        b = classify_blocked(c, make_profile(), None, ws=ws)
        assert b is not None and b.reason == BlockReason.EXTERNAL_JAVAX_API
        assert b.owner_class == owner
        assert b.required_type == required
        assert b.found_type and b.found_type.startswith("jakarta.")
        assert b.errors == c.errors and b.evidence


def test_x1_detail_text():
    b = classify_blocked(one(CTOR_JAVAX), make_profile(), None)
    assert b.detail().startswith("com.company.legacy.web.LegacyRequestHelper requires "
                                 "javax.servlet.http.HttpServletRequest")


def test_x1_not_blocked_when_owner_is_a_project_class(tmp_path):
    root = tmp_path / "p"
    d = root / "module/comp-jar/src/main/java/com/company/legacy/web"
    d.mkdir(parents=True)
    (d / "LegacyRequestHelper.java").write_text("package com.company.legacy.web; class LegacyRequestHelper {}\n")
    ws = Workspace(root, tmp_path / "run", "t")
    assert classify_blocked(one(CTOR_JAVAX), make_profile(), None, ws=ws) is None
    # without a workspace the profile's packages decide
    prof = make_profile()
    prof.source = SourceStats(packages=["com.company.legacy.web"])
    assert classify_blocked(one(CTOR_JAVAX), prof, None) is None
    prof.source = SourceStats(base_package="com.company.legacy")
    assert classify_blocked(one(CTOR_JAVAX), prof, None) is None


def test_x1_not_blocked_when_project_still_imports_javax(proj):
    """The recipe did not migrate some file: the project itself still hands javax around → fixable here."""
    proj.write("src/main/java/com/acme/web/Old.java",
               "package com.acme.web;\nimport javax.servlet.http.HttpServletRequest;\nclass Old {}\n")
    assert classify_blocked(one(INCOMPATIBLE), make_profile(), None, ws=proj) is None


@pytest.mark.parametrize("block", [PKG_MISSING, SYMBOL_JAVAX])
def test_missing_javax_is_the_recipes_job(block, proj):
    for ws in (None, proj):
        assert classify_blocked(one(block), make_profile(), None, ws=ws) is None


def test_x1_reverse_direction_needs_proof_from_the_workspace(proj):
    c = one(REVERSE)
    assert classify_blocked(c, make_profile(), None) is None            # without ws: maybe the project's own javax
    b = classify_blocked(c, make_profile(), None, ws=proj)              # project has no javax import → a library
    assert b is not None and b.required_type.startswith("jakarta.") and b.found_type.startswith("javax.")
    proj.write("src/main/java/com/acme/web/Old.java", "import javax.servlet.http.HttpServletRequest;\n")
    assert classify_blocked(c, make_profile(), None, ws=proj) is None


def test_x1_partial_cluster_covers_only_the_javax_error():
    errs = parse(CTOR_JAVAX, "[ERROR] /work/proj/src/main/java/com/acme/web/Foo.java:[30,5] cannot find symbol\n"
                             "  symbol:   class Missing\n  location: class com.acme.web.Foo\n")
    c = cluster_errors(errs)[0]
    assert len(c.errors) == 2
    b = classify_blocked(c, make_profile(), None)
    assert b is not None and len(b.errors) == 1 and "LegacyRequestHelper" in b.errors[0].message


# --------------------------------------------------------------------------------------------- X2
@pytest.mark.parametrize("block,owner", [(LIB_SYMBOL, "com.company.lib.Util"), (LIB_VAR_SYMBOL, "com.vendor.sdk.Client"),
                                         (LIB_APPLIED, "com.vendor.mq.Producer")])
def test_x2_external_api_incompatible(block, owner, proj):
    b = classify_blocked(one(block), make_profile(), None, ws=proj)
    assert b is not None and b.reason == BlockReason.EXTERNAL_API_INCOMPATIBLE and b.owner_class == owner


def test_x2_negative_cases(proj):
    # migration-target APIs (Spring, JDK, …) are what the recipes and the LLM are for
    assert classify_blocked(one(SPRING_SYMBOL), make_profile(), None, ws=proj) is None
    # project-owned owner
    proj.write("src/main/java/com/company/lib/Util.java", "package com.company.lib; class Util {}\n")
    assert classify_blocked(one(LIB_SYMBOL), make_profile(), None, ws=proj) is None
    # owner inside the project's own package (generated class not in src) counts as project too
    own = ("[ERROR] /work/proj/src/main/java/com/acme/web/Foo.java:[7,13] cannot find symbol\n"
           "  symbol:   method gen()\n  location: class com.acme.web.Generated\n")
    assert classify_blocked(one(own), make_profile(), None, ws=proj) is None


def test_extract_owner_and_is_project_class(proj):
    errs = parse(CTOR_JAVAX, LIB_VAR_SYMBOL)
    assert extract_owner(errs[0]) == "com.company.legacy.web.LegacyRequestHelper"
    assert extract_owner(errs[1]) == "com.vendor.sdk.Client"
    assert is_project_class(proj, None, "com.acme.web.Foo")
    assert is_project_class(proj, None, "com.acme.web.Foo.Inner")
    assert is_project_class(proj, None, "com.acme.web.Foo<java.lang.String>")
    assert not is_project_class(proj, None, "com.company.legacy.web.LegacyRequestHelper")
    assert not is_project_class(proj, None, "Foo") and not is_project_class(None, None, None)
    prof = make_profile()
    prof.source = SourceStats(base_package="com.acme")
    assert is_project_class(None, prof, "com.acme.x.Y") and not is_project_class(None, prof, "com.acmex.Y")


# --------------------------------------------------------------------------------------------- X3–X6
def test_x3_artifact_not_found(repo_root):
    log = (repo_root / "tests/build/logs/real-missing-dependency.log").read_text()
    clusters = [c for c in cluster_errors(parse_maven_log(log, ROOT)) if c.kind == ErrorKind.DEPENDENCY_RESOLUTION]
    assert clusters
    b = classify_blocked(clusters[0], make_profile(), None)
    assert b.reason == BlockReason.ARTIFACT_NOT_FOUND
    assert b.repositories == ["central (https://repo.maven.apache.org/maven2)"]
    log = (repo_root / "tests/build/logs/real-plugin-resolution.log").read_text()
    c = next(c for c in cluster_errors(parse_maven_log(log, ROOT)) if c.kind == ErrorKind.PLUGIN_RESOLUTION)
    assert classify_blocked(c, make_profile(), None).reason == BlockReason.ARTIFACT_NOT_FOUND


def test_x3_needs_a_not_found_message():
    from .fakes import err
    c = cluster_errors([err(ErrorKind.DEPENDENCY_RESOLUTION, "dependencies.dependency.version is weird",
                            artifact="a:b:1")])[0]
    assert classify_blocked(c, make_profile(), None) is None


def test_x4_parent_not_found(repo_root):
    log = (repo_root / "tests/build/logs/real-bad-parent.log").read_text()
    c = next(c for c in cluster_errors(parse_maven_log(log, ROOT)) if c.kind == ErrorKind.PARENT_RESOLUTION)
    b = classify_blocked(c, make_profile(), None)
    assert b.reason == BlockReason.PARENT_NOT_FOUND and "company-parent" in b.detail()
    assert b.repositories and "repo.maven.apache.org" in b.repositories[0]


@pytest.mark.parametrize("msg,plugin", [
    ("Execution default of goal org.example:gen-maven-plugin:1.2:generate failed: Unsupported class file major "
     "version 65", "org.example:gen-maven-plugin:1.2"),
    ("Execution default of goal org.example:x-maven-plugin:0.9:run failed: An API incompatibility was encountered "
     "while executing org.example:x-maven-plugin:0.9:run: java.lang.NoSuchMethodError: 'void com.sun.tools.javac.Foo'",
     "org.example:x-maven-plugin:0.9"),
    ("Execution default of goal org.example:apt-maven-plugin:1.1:process failed: java.lang.IllegalAccessError: class "
     "lombok.javac.apt.LombokProcessor cannot access class com.sun.tools.javac.processing.JavacProcessingEnvironment",
     "org.example:apt-maven-plugin:1.1"),
])
def test_x5_plugin_incompatible(msg, plugin):
    from .fakes import err
    c = cluster_errors([err(ErrorKind.PLUGIN_EXECUTION, msg, module="demo", raw="[ERROR] Failed to execute " + msg)])[0]
    b = classify_blocked(c, make_profile(), None)
    assert b.reason == BlockReason.PLUGIN_INCOMPATIBLE and b.plugin == plugin
    # a plugin failing for another reason (resources filtering) is not a JDK 21 incompatibility
    other = cluster_errors([err(ErrorKind.PLUGIN_EXECUTION, "filtering a.properties failed with MalformedInputException",
                                module="demo")])[0]
    assert classify_blocked(other, make_profile(), None) is None


def test_x6_unsupported_source(repo_root):
    log = (repo_root / "tests/build/logs/synthetic-kotlin.log").read_text()
    clusters = cluster_errors(parse_maven_log(log, ROOT))
    assert clusters
    for c in clusters:
        b = classify_blocked(c, make_profile(), None)
        assert b.reason == BlockReason.UNSUPPORTED_SOURCE and "Kotlin" in b.detail()
    from .fakes import err
    gen = cluster_errors([err(ErrorKind.PLUGIN_EXECUTION, "Execution default of goal org.xolstice.maven.plugins:"
                              "protobuf-maven-plugin:0.6.1:compile failed: protoc did not exit cleanly", module="demo")])[0]
    b = classify_blocked(gen, make_profile(), None)
    assert b.reason == BlockReason.UNSUPPORTED_SOURCE and b.plugin == "org.xolstice.maven.plugins:protobuf-maven-plugin:0.6.1"


def test_plain_compile_error_is_not_blocked(proj):
    own = ("[ERROR] /work/proj/src/main/java/com/acme/web/Foo.java:[3,5] cannot find symbol\n"
           "  symbol:   class Missing\n  location: class com.acme.web.Foo\n")
    assert classify_blocked(one(own), make_profile(), None, ws=proj) is None
    assert classify_blocked(one(own).model_copy(update={"errors": []}), make_profile(), None) is None


# --------------------------------------------------------------------------------------------- decide policy (v2)
def ctx(**kw):
    return TriageContext(phase=kw.pop("phase", Phase.VERIFY), iteration=1, **kw)


def script_attempt(cluster, outcome="no_change", mode="new"):
    return FixAttempt(id="a", phase=Phase.VERIFY, iteration=1, cluster_id=cluster.id,
                      decision=TriageDecision(cluster_id=cluster.id, kind=DecisionKind.SCRIPT), outcome=outcome,
                      cluster_signatures=cluster.signatures, script_mode=mode, script_version=1,
                      script_code="def fix(p):\n    pass\n")


def cases(text: str):
    from sbmig.triage import cluster_cases
    return cluster_cases(parse(text))[0]


def test_policy_zero_blocks_immediately_without_llm(cfg, profile):
    cfg.loop.blocked_policy["external_javax_api"] = 0          # v2 default 2 (a script may swap the library)
    t = Triager(cfg, registry=HandlerRegistry())
    c = cases(CTOR_JAVAX)
    d = t.decide(c, None, ctx(), profile)
    assert d.kind == DecisionKind.BLOCKED
    assert d.rationale.startswith("blocked: external_javax_api — com.company.legacy.web.LegacyRequestHelper requires "
                                  "javax.servlet.http.HttpServletRequest")
    assert t.blocked_candidate(c.id).reason == BlockReason.EXTERNAL_JAVAX_API


def test_policy_counts_llm_script_attempts(cfg, profile):
    t = Triager(cfg, registry=HandlerRegistry())
    c = cases(LIB_SYMBOL)
    assert cfg.loop.blocked_policy["external_api_incompatible"] == 2     # v2 default
    cfg.loop.blocked_policy["external_api_incompatible"] = 1
    d = t.decide(c, None, ctx(), profile)
    assert d.kind == DecisionKind.SCRIPT and "external_api_incompatible" in d.rationale and "1/1" in d.rationale
    d = t.decide(c, None, ctx(previous_attempts=[script_attempt(c)]), profile)
    assert d.kind == DecisionKind.BLOCKED and "after 1 LLM script attempt" in d.rationale
    cfg.loop.blocked_policy["external_javax_api"] = 2
    c1 = cases(CTOR_JAVAX)
    assert t.decide(c1, None, ctx(previous_attempts=[script_attempt(c1)]), profile).kind == DecisionKind.SCRIPT
    two = [script_attempt(c1), script_attempt(c1, mode="augment")]
    assert t.decide(c1, None, ctx(previous_attempts=two), profile).kind == DecisionKind.BLOCKED


def test_registry_llm_unavailable_and_garbage(cfg, profile):
    t = Triager(cfg, registry=HandlerRegistry())
    reg = BlockedRegistry()
    t.attach_run(blocked_registry=reg)
    c = cases(LIB_SYMBOL)
    reg.block(c, BlockReason.EXTERNAL_API_INCOMPATIBLE, Phase.VERIFY)
    d = t.decide(c, None, ctx(), profile)          # already blocked → no script
    assert d.kind == DecisionKind.BLOCKED and "already blocked" in d.rationale
    # LLM unavailable → cases that need the LLM are blocked
    t3 = Triager(cfg, registry=HandlerRegistry())
    plain = cases("[ERROR] /work/proj/src/main/java/com/acme/web/Foo.java:[3,5] cannot find symbol\n"
                  "  symbol:   class Missing\n  location: class com.acme.web.Foo\n")
    t3.set_llm_unavailable("HTTP 503")
    d = t3.decide(plain, None, ctx(), profile)
    assert d.kind == DecisionKind.BLOCKED and "llm_unavailable" in d.rationale
    t3.set_llm_unavailable(None)
    # answers without a script: `new` again, up to 1 + script.max_augments_per_case requests
    garbage = [script_attempt(plain, "apply_failed").model_copy(update={"script_code": "", "script_version": 0})]
    assert t3.decide(plain, None, ctx(previous_attempts=garbage), profile).kind == DecisionKind.SCRIPT
    n = 1 + cfg.script.max_augments_per_case
    d = t3.decide(plain, None, ctx(previous_attempts=garbage * n), profile)
    assert d.kind == DecisionKind.BLOCKED and "repeated_failure" in d.rationale and "no usable script" in d.rationale
