"""v2.1o (orchestrator side): every as-is property the restructure removed because it overrides the Spring Boot BOM's
(`RestructureResult.bom_properties_removed`) is a pom-verify row the next build verifies."""
from __future__ import annotations

import json

from sbmig.core.interfaces import RestructureResult

from .fakes import FakeErrorList
from .test_v2_orchestrator import make_v2

BACK = "shop-back/pom.xml"


class _BomRestructurer:
    def apply(self, ws, profile, cfg, *, mode, params=None):
        ws.write(BACK, "<project/>\n")
        return RestructureResult(ok=True, changed_files=[BACK],
                                 bom_properties_removed=[f"{BACK}: spring-security.version=5.7.11",
                                                         "pom.xml: spring-batch.version=4.3.10",
                                                         f"{BACK}: spring-boot.version=2.5.15"],
                                 version_properties_removed=[f"{BACK}: spring.version=5.3.31"],
                                 recipe_versions_dropped=[
                                     f"org.springframework:spring-context 7.0.10 (as-is 6.2.12, managed 7.0.8, {BACK})",
                                     f"org.example:x 2.0.0 (as-is -, managed 1.5.0, {BACK})"])


def test_each_removed_property_is_a_pom_verify_row(cfg, ws):
    el = FakeErrorList()
    make_v2(cfg, ws, restructurer=_BomRestructurer(), error_list=lambda w, c, p: el).run()
    added = [args for args, _ in el.of("add_pom_verify")]
    boot = cfg.target.spring_boot_version
    why = f"removed: it overrides the Spring Boot {boot} BOM's"
    sec = f"property spring-security.version=5.7.11 {why} spring-security.version"
    batch = f"property spring-batch.version=4.3.10 {why} spring-batch.version"
    assert (BACK, sec) in added and ("pom.xml", batch) in added

    dead = ("version property spring.version=5.3.31 removed: no dependency references it after the parent-managed "
            "versions were dropped")
    assert (BACK, dead) in added
    events = [json.loads(x) for x in ws.journal_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    rs = [e for e in events if e.get("event") == "restructure"]
    assert rs[0]["version_properties_removed"] == [f"{BACK}: spring.version=5.3.31"]
    assert rs[0]["bom_properties_removed"][0] == f"{BACK}: spring-security.version=5.7.11"
    parent = "property spring-boot.version=2.5.15 removed: it overrides the in-house parent's spring-boot.version"
    assert (BACK, parent) in added
    recipe = ("explicit version dropped: org.springframework:spring-context 7.0.10 → managed 7.0.8 (raised by a "
              "recipe from the as-is 6.2.12)")
    assert (BACK, recipe) in added
    added_row = "explicit version dropped: org.example:x 2.0.0 → managed 1.5.0 (added by a recipe: not in the as-is build)"
    assert (BACK, added_row) in added
    assert rs[0]["recipe_versions_dropped"][0].startswith("org.springframework:spring-context 7.0.10")
