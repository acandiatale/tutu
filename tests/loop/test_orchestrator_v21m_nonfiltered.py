"""v2.1m (orchestrator side): every pom the restructure gave `<nonFilteredFileExtensions>` entries
(`RestructureResult.non_filtered_extensions`) is a pom-verify row the next build verifies, like the v2.1l pins."""
from __future__ import annotations

from sbmig.core.interfaces import RestructureResult

from .fakes import FakeErrorList
from .test_v2_orchestrator import make_v2

BACK = "shop-back/pom.xml"
FRONT = "shop-front/pom.xml"


class _FilteringRestructurer:
    def apply(self, ws, profile, cfg, *, mode, params=None):
        ws.write(BACK, "<project/>\n")
        followup = ("binary file without an extension under a filtered resource dir: shop-back/src/main/resources/"
                    "data/BLOB — exclude it from filtering (a second <resource> with <filtering>false</filtering>) "
                    "or rename it")
        return RestructureResult(ok=True, changed_files=[BACK],
                                 non_filtered_extensions=[f"{BACK}: dmdb, woff2 → maven-resources-plugin",
                                                          f"{FRONT}: ttf → maven-resources-plugin, maven-war-plugin"],
                                 manual_followups=[followup])


def test_each_written_pom_is_a_pom_verify_row(cfg, ws):
    el = FakeErrorList()
    report = make_v2(cfg, ws, restructurer=_FilteringRestructurer(), error_list=lambda w, c, p: el).run()
    added = [args for args, _ in el.of("add_pom_verify")]
    assert (BACK, f"nonFilteredFileExtensions added to maven-resources-plugin in {BACK}: dmdb, woff2") in added
    war_row = f"nonFilteredFileExtensions added to maven-resources-plugin, maven-war-plugin in {FRONT}: ttf"
    assert (FRONT, war_row) in added
    assert any("data/BLOB" in f for f in report.manual_followups)


def test_no_extensions_no_row(cfg, ws):
    el = FakeErrorList()

    class _Plain:
        def apply(self, ws, profile, cfg, *, mode, params=None):
            return RestructureResult(ok=True)

    make_v2(cfg, ws, restructurer=_Plain(), error_list=lambda w, c, p: el).run()
    assert not any("nonFilteredFileExtensions" in args[1] for args, _ in el.of("add_pom_verify"))
