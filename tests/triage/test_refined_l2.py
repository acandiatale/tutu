"""Refined case keys for catch-all compile L2s (sbmig.triage.cluster.refine_signature, PLAN-v2 step 4 / e2e open
problem 2): a file-less `Compilation failure` summary, a `compile|other|…` message and an L2 without a key segment
must not give unrelated failures one case id. The errors below are the ones the 2026-10-27 e2e runs recorded
(message / raw as the log parser produced them; L1 / L2 from `fill_signatures`)."""
from __future__ import annotations

from sbmig.build.signatures import fill_signatures
from sbmig.core.models import BuildError, ErrorKind
from sbmig.kb.catalog import case_id_for
from sbmig.triage import Triager, case_id_of, cluster_cases, is_coarse_l2, refine_signature, refine_signatures

WERROR_RAW = ("[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.14.0:compile "
              "(default-compile) on project pricing-core: Compilation failure\n"
              "[ERROR] src/main/java/com/example/pricing/PriceCalculator.java: warnings found and -Werror specified")
BOOTCP_RAW = ("[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.16.0:compile "
              "(default-compile) on project ledger-utils-comp: Compilation failure: Compilation failure: \n"
              "[ERROR] error: option --boot-class-path cannot be used together with --release\n"
              "[ERROR] Usage: javac <options> <source files>\n"
              "[ERROR] use --help for a list of possible options")


def werror() -> BuildError:
    return fill_signatures(BuildError(kind=ErrorKind.COMPILE, message="Compilation failure", module="pricing-core",
                                      raw=WERROR_RAW))


def bootcp() -> BuildError:
    return fill_signatures(BuildError(kind=ErrorKind.COMPILE, message="Compilation failure: Compilation failure:",
                                      module="ledger-utils-comp", raw=BOOTCP_RAW))


def symbol(file: str = "src/main/java/a/Foo.java", line: int = 3) -> BuildError:
    return fill_signatures(BuildError(
        kind=ErrorKind.COMPILE, message="cannot find symbol", file=file, line=line, module="demo",
        symbol="class JAXBContext", location="class a.Foo",
        raw=f"[ERROR] /abs/{file}:[{line},5] cannot find symbol\n  symbol:   class JAXBContext\n  location: class a.Foo"))


def test_the_two_javac_summaries_get_different_case_ids():
    a, b = werror(), bootcp()
    assert a.signature_generic == "compile|build|compilation failure"            # the parser's catch-all keys
    assert b.signature_generic == "compile|build|compilation failure: compilation failure"
    assert is_coarse_l2(a) and is_coarse_l2(b)
    refine_signatures([a, b])
    assert a.signature_generic == "compile|build|warnings found and -werror specified"
    assert b.signature_generic == "compile|build|option --boot-class-path cannot be used together with --release"
    assert a.signature == a.signature_generic and b.signature == b.signature_generic   # L1 said no more than L2
    assert case_id_of(a) != case_id_of(b)
    assert case_id_of(a) == case_id_for(a.signature_generic)                     # the catalog's id of the same key


def test_the_same_failure_in_two_modules_is_one_case():
    a = werror()
    b = fill_signatures(BuildError(kind=ErrorKind.COMPILE, message="Compilation failure", module="pricing-api",
                                   raw=WERROR_RAW.replace("pricing-core", "pricing-api")
                                   .replace("pricing/PriceCalculator.java", "api/Rates.java")))
    cs = cluster_cases([a, b])
    assert len(cs) == 1 and len(cs[0].errors) == 2


def test_cluster_cases_and_the_triager_refine_before_grouping(cfg):
    cs = cluster_cases([werror(), bootcp(), symbol()])
    assert len(cs) == 3
    assert {c.case_id for c in cs} == {case_id_for("compile|build|warnings found and -werror specified"),
                                       case_id_for("compile|build|option --boot-class-path cannot be used together "
                                                   "with --release"),
                                       case_id_for("compile|cannot_find_symbol|JAXBContext")}
    assert len(Triager(cfg).cluster([werror(), bootcp()])) == 2


def test_a_normal_case_is_unchanged():
    e = symbol()
    before = (e.signature, e.signature_generic)
    assert e.signature_generic == "compile|cannot_find_symbol|JAXBContext" and not is_coarse_l2(e)
    refine_signature(e)
    assert (e.signature, e.signature_generic) == before
    assert case_id_of(e) == case_id_for("compile|cannot_find_symbol|JAXBContext")


def test_other_class_gets_the_whole_first_line_and_keeps_its_l1():
    e = fill_signatures(BuildError(
        kind=ErrorKind.COMPILE, file="module/comp-jar/src/main/java/com/example/ops/util/TokenCodec.java", line=24,
        message="exception java.io.IOException is never thrown in body of corresponding try statement",
        raw="[ERROR] module/comp-jar/src/main/java/com/example/ops/util/TokenCodec.java:[24,11] exception "
            "java.io.IOException is never thrown in body of corresponding try statement"))
    assert e.signature_generic == "compile|other|exception java.io.ioexception is never thrown in"
    l1 = e.signature
    refine_signature(e)
    assert e.signature_generic == ("compile|other|exception java.io.ioexception is never thrown in body of "
                                   "corresponding try statement")
    assert e.signature == l1                                                     # the L1 was already specific


def test_refinement_is_idempotent_and_needs_a_meaningful_raw_line():
    a = werror()
    refine_signature(a)
    once = (a.signature, a.signature_generic)
    refine_signature(refine_signature(a))
    assert (a.signature, a.signature_generic) == once
    bare = fill_signatures(BuildError(kind=ErrorKind.COMPILE, message="Compilation failure", module="x",
                                      raw="[ERROR] Failed to execute goal g:maven-compiler-plugin:3.14.0:compile "
                                          "(default-compile) on project x: Compilation failure\n[ERROR] -> [Help 1]"))
    none = fill_signatures(BuildError(kind=ErrorKind.COMPILE, message="Compilation failure", module="x"))
    for e in (bare, none):
        refine_signature(e)
        assert e.signature_generic == "compile|build|compilation failure"        # nothing better to key on


def test_an_l2_without_a_key_segment_takes_the_raw_detail():
    e = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file="src/main/java/a/Foo.java", line=9,
                   signature="compile|cannot_find_symbol|src/main/java/a/Foo.java:9",
                   signature_generic="compile|cannot_find_symbol",
                   raw="[ERROR] /abs/src/main/java/a/Foo.java:[9,5] cannot find symbol\n  symbol:   variable clientIp")
    assert is_coarse_l2(e)
    refine_signature(e)
    assert e.signature_generic == "compile|cannot_find_symbol|symbol: variable clientip"     # the bare phrase skipped
    assert e.signature == "compile|cannot_find_symbol|src/main/java/a/Foo.java:9"


def test_other_kinds_are_never_refined():
    for e in (BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="x", signature="dependency_resolution|g:a:1",
                         signature_generic="dependency_resolution|g:a", raw="[ERROR] Failed to execute goal x"),
              BuildError(kind=ErrorKind.ENFORCER, message="RequireJavaVersion: x",
                         signature="enforcer|requirejavaversion: x", signature_generic="enforcer|requirejavaversion: x",
                         raw="[ERROR] Rule 0: RequireJavaVersion failed")):
        before = (e.signature, e.signature_generic)
        assert not is_coarse_l2(e)
        refine_signature(e)
        assert (e.signature, e.signature_generic) == before
