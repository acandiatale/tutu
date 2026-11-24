"""v2.1z-cc (orchestrator side): a version the restructure wrote into an annotation processor path is a
pom-verify row of the pom it was written to, worded as what it is (not "carried over from the as-is build")."""
from __future__ import annotations

from sbmig.core.interfaces import RestructureResult

from .fakes import FakeErrorList
from .test_v2_orchestrator import make_v2


class _PathRestructurer:
    def apply(self, ws, profile, cfg, *, mode, params=None):
        ws.write("shop-api/pom.xml", "<project/>\n")
        own = "org.mapstruct:mapstruct-processor: 1.6.3 ← the pom's own dependency (annotationProcessorPaths, shop-api/pom.xml)"
        asis = ("org.apache.commons:commons-text: 1.10.0 ← as-is managed by com.company.legacy:legacy-parent:1.0.0 "
                "(annotationProcessorPaths, shop-api/pom.xml)")
        dep = ("org.apache.commons:commons-text: 1.10.0 ← as-is managed by com.company.legacy:legacy-parent:1.0.0 "
               "(shop-api/pom.xml)")
        return RestructureResult(ok=True, changed_files=["shop-api/pom.xml"], versions_pinned=[own, asis, dep])


def test_a_processor_path_version_is_a_row_of_its_pom_with_its_own_wording(cfg, ws):
    el = FakeErrorList()
    make_v2(cfg, ws, restructurer=_PathRestructurer(), error_list=lambda w, c, p: el).run()
    added = [args for args, _ in el.of("add_pom_verify")]
    assert ("shop-api/pom.xml", "annotation processor path version written: org.mapstruct:mapstruct-processor 1.6.3 "
            "(the pom's own dependency)") in added
    assert ("shop-api/pom.xml", "annotation processor path version written: org.apache.commons:commons-text 1.10.0 "
            "(as-is managed by com.company.legacy:legacy-parent:1.0.0)") in added
    # the dependency pin of the same artifact keeps its row and its file
    assert ("shop-api/pom.xml", "version carried over from the as-is build: org.apache.commons:commons-text 1.10.0") in added
    assert not [a for a in added if "annotationProcessorPaths," in a[0]]
