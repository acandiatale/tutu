from __future__ import annotations

from sbmig.core.models import ErrorKind, PomInfo
from sbmig.triage import Triager, cluster_errors, cluster_id, plan_iteration
from sbmig.triage.cluster import cluster_label, module_index

from .conftest import make_profile
from .fakes import err

PROFILE = make_profile(poms=[PomInfo(path="pom.xml", artifact_id="parent"),
                             PomInfo(path="core/pom.xml", artifact_id="core"),
                             PomInfo(path="web/pom.xml", artifact_id="web")])


def test_compile_errors_cluster_by_file_and_priority_by_reactor():
    errors = [
        err(file="web/src/main/java/W.java", module="web", message="cannot find symbol", sig="s1"),
        err(file="core/src/main/java/C.java", module="core", message="cannot find symbol", sig="s2"),
        err(file="web/src/main/java/W.java", module="web", message="incompatible types", sig="s3"),
        err(file="web/src/main/java/W.java", module="web", message="cannot find symbol", sig="s1"),
    ]
    cl = cluster_errors(errors, PROFILE)
    assert [c.file for c in cl] == ["core/src/main/java/C.java", "web/src/main/java/W.java"]
    assert cl[0].priority == 51 and cl[1].priority == 52
    assert cl[1].signatures == ["s1", "s3"]          # unique, ordered
    assert cl[1].id == cluster_id(ErrorKind.COMPILE, "web/src/main/java/W.java")
    assert cl[1].id.startswith("compile:") and len(cl[1].id.split(":")[1]) == 8


def test_resolution_clusters_by_artifact_and_kind_priorities():
    errors = [
        err(ErrorKind.TEST_COMPILE, file="core/src/test/java/T.java", module="core"),
        err(ErrorKind.DEPENDENCY_RESOLUTION, "could not resolve", artifact="a:b:1", module="core"),
        err(ErrorKind.DEPENDENCY_RESOLUTION, "could not resolve", artifact="a:b:2", module="web"),
        err(ErrorKind.PARENT_RESOLUTION, "non-resolvable parent", artifact="p:q:1"),
        err(ErrorKind.POM_INVALID, "'dependencies.dependency.version' for x:y:jar is missing", module="core"),
        err(ErrorKind.PLUGIN_RESOLUTION, "plugin", artifact="org.x:plug:1"),
        err(ErrorKind.ENFORCER, "enforcer", module="core"),
        err(ErrorKind.PLUGIN_EXECUTION, "boom", module="web"),
        err(ErrorKind.UNKNOWN, "??"),
        err(ErrorKind.TEST_FAILURE, "failed", module="core"),
        err(ErrorKind.REWRITE, "rw"),
    ]
    cl = cluster_errors(errors, PROFILE)
    kinds = [c.kind for c in cl]
    assert kinds == [ErrorKind.PARENT_RESOLUTION, ErrorKind.POM_INVALID, ErrorKind.DEPENDENCY_RESOLUTION,
                     ErrorKind.PLUGIN_RESOLUTION, ErrorKind.ENFORCER, ErrorKind.REWRITE, ErrorKind.TEST_COMPILE,
                     ErrorKind.PLUGIN_EXECUTION, ErrorKind.TEST_FAILURE, ErrorKind.UNKNOWN]
    dep = [c for c in cl if c.kind == ErrorKind.DEPENDENCY_RESOLUTION]
    assert len(dep) == 1 and len(dep[0].errors) == 2          # same g:a, different versions
    tc = next(c for c in cl if c.kind == ErrorKind.TEST_COMPILE)
    assert tc.priority == 81
    assert "a:b" in cluster_label(dep[0])


def test_unknown_module_and_tie_break_by_count():
    errors = [err(file="x/A.java", module="ghost", sig="a"),
              err(file="x/B.java", module="ghost", sig="b"), err(file="x/B.java", module="ghost", sig="c")]
    cl = cluster_errors(errors, make_profile(poms=[PomInfo(path="other/pom.xml", artifact_id="other")]))
    assert all(c.priority == 90 for c in cl)
    assert cl[0].file == "x/B.java"                     # more errors first
    assert module_index(None, "x") is None
    assert module_index(PROFILE, None, "web/src/X.java") == 2
    # file without module falls back to module key
    c2 = cluster_errors([err(file=None, module="core")], PROFILE)
    assert c2[0].priority == 51


def test_masking_same_module_only():
    errors = [err(ErrorKind.DEPENDENCY_RESOLUTION, "nope", artifact="a:b:1", module="core"),
              err(file="core/A.java", module="core"), err(file="web/W.java", module="web")]
    items = plan_iteration(cluster_errors(errors, PROFILE))
    skipped = [i for i in items if i.skip is not None]
    assert len(skipped) == 1 and skipped[0].cluster.module == "core"
    assert "masked" in skipped[0].skip.rationale and skipped[0].masked_by
    # module-less resolution error masks everything
    items = plan_iteration(cluster_errors([err(ErrorKind.PARENT_RESOLUTION, "p", artifact="p:q:1"),
                                           err(file="web/W.java", module="web")], PROFILE))
    assert sum(1 for i in items if i.skip) == 1


def test_triager_cluster_delegates(cfg):
    t = Triager(cfg)
    assert t.cluster([err(file="A.java")], PROFILE)[0].kind == ErrorKind.COMPILE
    assert t.plan_iteration([])== []
