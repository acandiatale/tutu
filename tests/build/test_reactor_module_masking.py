"""v2.1z: every reactor artifactId the log shows is masked as <module> in signatures (cross-project case keys)."""
from __future__ import annotations

import pytest

from sbmig.build.log_parser import parse_maven_log, reactor_artifact_ids
from sbmig.build.signatures import (
    MODULE_NAME_STOPLIST,
    normalize_message,
    reactor_modules,
    set_reactor_modules,
    signature_for,
)
from sbmig.core.models import BuildError, ErrorKind


@pytest.fixture(autouse=True)
def _empty_registry():
    set_reactor_modules(())
    yield
    set_reactor_modules(())


ENFORCER_LOG = """\
[INFO] Scanning for projects...
[INFO] ------------------------------------------------------------------------
[INFO] Reactor Build Order:
[INFO]
[INFO] pricing-core                                                       [pom]
[INFO] pricing-core-comp                                                  [jar]
[INFO]
[INFO] ---------------------< com.acme.pricing:pricing-core >---------------------
[INFO] Building pricing-core 1.0.0                                        [1/2]
[INFO]   from pom.xml
[INFO] --------------------------------[ pom ]---------------------------------
[INFO]
[INFO] --- clean:3.2.0:clean (default-clean) @ pricing-core ---
[INFO]
[INFO] ------------------< com.acme.pricing:pricing-core-comp >------------------
[INFO] Building pricing-core-comp 1.0.0                                   [2/2]
[INFO]   from pricing-core-comp/pom.xml
[INFO] --------------------------------[ jar ]---------------------------------
[INFO]
[INFO] --- enforcer:3.5.0:enforce (enforce-java) @ pricing-core-comp ---
[WARNING] Rule 0: org.apache.maven.enforcer.rules.version.RequireJavaVersion failed with message:
[WARNING] pricing-core must be built with JDK 8
[INFO] ------------------------------------------------------------------------
[INFO] Reactor Summary for pricing-core 1.0.0:
[INFO]
[INFO] pricing-core ....................................... SUCCESS [  0.100 s]
[INFO] pricing-core-comp .................................. FAILURE [  0.200 s]
[INFO] ------------------------------------------------------------------------
[INFO] BUILD FAILURE
[INFO] ------------------------------------------------------------------------
[ERROR] Failed to execute goal org.apache.maven.plugins:maven-enforcer-plugin:3.5.0:enforce (enforce-java) on project pricing-core-comp: Some Enforcer rules have failed. Look above for specific messages explaining why the rule failed. -> [Help 1]
"""


def test_reactor_artifact_ids_from_headers_markers_and_failed_goal():
    assert reactor_artifact_ids(ENFORCER_LOG) == ["pricing-core", "pricing-core-comp"]
    log = "[ERROR] Failed to execute goal x:y:1:z (id) on project orders-svc: boom\n"
    assert reactor_artifact_ids(log) == ["orders-svc"]
    assert reactor_artifact_ids("") == []


def test_enforcer_root_artifact_id_is_masked_in_l2():
    errs = parse_maven_log(ENFORCER_LOG)
    enf = [e for e in errs if e.kind == ErrorKind.ENFORCER]
    assert enf, errs
    e = enf[0]
    assert e.module == "pricing-core-comp"
    assert "pricing-core" in e.message
    assert e.signature_generic == "enforcer|requirejavaversion: <module> must be built with"
    assert "pricing" not in e.signature
    assert reactor_modules() == frozenset({"pricing-core", "pricing-core-comp"})


def test_same_case_key_for_another_project():
    other = ENFORCER_LOG.replace("pricing-core", "billing-api")
    a = parse_maven_log(ENFORCER_LOG)
    b = parse_maven_log(other)
    assert sorted(e.signature_generic for e in a) == sorted(e.signature_generic for e in b)


def test_longest_name_first_and_boundaries():
    set_reactor_modules(["pricing-core", "pricing-core-comp"])
    out = normalize_message("pricing-core-comp needs pricing-core but not pricing-corex or com.pricing-core.x")
    assert out == "<module> needs <module> but not pricing-corex or com.pricing-core.x"


def test_stoplisted_and_short_names_are_not_masked():
    set_reactor_modules(["web", "app", "ab", "core"])
    assert normalize_message("Error assembling WAR: webxml attribute is required") == \
        "error assembling war: webxml attribute is required"
    assert normalize_message("the web module needs app core") == "the web module needs app core"
    assert normalize_message("ab failed") == "ab failed"
    assert {"web", "core", "boot", "front", "back"} <= MODULE_NAME_STOPLIST
    # the error's OWN module is always masked, stoplist or not (the pre-v2.1z rule; review R2)
    assert normalize_message("the web module", module="web") == "the <module> module"


def test_plain_lowercase_registry_names_are_not_masked_but_artifact_like_ones_are():
    """Review R2: reactor names that are ordinary words (`user`, `config`) must not eat message tokens; names that look
    like artifactIds (a separator, a digit or an uppercase letter) are masked; the own module always is."""
    set_reactor_modules(["user", "config", "pricing-core", "app_web", "svc2", "DEV"])
    assert normalize_message("package user.model does not exist") == "package user.model does not exist"
    assert normalize_message("cannot find symbol: variable config") == "cannot find symbol: variable config"
    assert normalize_message("pricing-core must be built with JDK 8") == "<module> must be built with jdk #"
    assert normalize_message("app_web and svc2 and DEV failed") == "<module> and <module> and <module> failed"
    assert normalize_message("cannot find symbol: variable config", module="config") == "cannot find symbol: variable <module>"


def test_masking_is_case_sensitive():
    set_reactor_modules(["Pricing-Core"])
    assert normalize_message("pricing-core and Pricing-Core") == "pricing-core and <module>"


def test_registry_is_replaced_by_the_next_parse():
    parse_maven_log(ENFORCER_LOG)
    assert "pricing-core" in reactor_modules()
    parse_maven_log(ENFORCER_LOG.replace("pricing-core", "billing-api"))
    assert reactor_modules() == frozenset({"billing-api", "billing-api-comp"})
    assert normalize_message("pricing-core must") == "pricing-core must"
    parse_maven_log("[INFO] BUILD SUCCESS\n")
    assert reactor_modules() == frozenset()


def test_hand_built_error_with_empty_registry_unchanged():
    assert reactor_modules() == frozenset()
    err = BuildError(kind=ErrorKind.ENFORCER, message="RequireJavaVersion: foo-svc must be built with JDK 8",
                     module="foo-svc")
    l1, l2 = signature_for(err)
    assert l2 == "enforcer|requirejavaversion: <module> must be built with"
    assert l1 == "enforcer|requirejavaversion: <module> must be built with jdk #"
    err2 = BuildError(kind=ErrorKind.ENFORCER, message="RequireJavaVersion: bar-svc must be built with JDK 8",
                      module="foo-svc")
    assert "bar-svc" in signature_for(err2)[1]


def test_union_of_own_module_and_registry():
    set_reactor_modules(["orders-root"])
    out = normalize_message("orders-svc depends on orders-root", module="orders-svc")
    assert out == "<module> depends on <module>"
