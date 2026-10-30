"""v2.1n (orchestrator side): every optional artifact the restructure introduced for a trigger dependency
(`RestructureResult.optional_added`) is a pom-verify row the next build verifies; the restructure journal event
carries both lists."""
from __future__ import annotations

import json

from sbmig.core.interfaces import RestructureResult

from .fakes import FakeErrorList
from .test_v2_orchestrator import make_v2

BACK = "shop-back/pom.xml"
JPA = "com.company.lib:lib-jpa"
HIB = "org.hibernate.orm:hibernate-core"
LANG3 = "org.apache.commons:commons-lang3"
TEXT = "org.apache.commons:commons-text"


class _TriggerRestructurer:
    def apply(self, ws, profile, cfg, *, mode, params=None):
        ws.write(BACK, "<project/>\n")
        return RestructureResult(ok=True, changed_files=[BACK],
                                 optional_added=[f"{JPA} → {BACK} (for {HIB}, org.hibernate:hibernate-core)",
                                                 f"{LANG3} → {BACK} (for {TEXT})"],
                                 optional_replaced=[f"{HIB} removed from {BACK} → {JPA}"])


def test_each_added_optional_is_a_pom_verify_row(cfg, ws):
    el = FakeErrorList()
    make_v2(cfg, ws, restructurer=_TriggerRestructurer(), error_list=lambda w, c, p: el).run()
    added = [args for args, _ in el.of("add_pom_verify")]
    assert (BACK, f"optional {JPA} added for {HIB}, org.hibernate:hibernate-core in {BACK}") in added
    assert (BACK, f"optional {LANG3} added for {TEXT} in {BACK}") in added
    events = [json.loads(x) for x in ws.journal_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    rs = [e for e in events if e.get("event") == "restructure"]
    assert rs and rs[0]["optional_added"][1] == f"{LANG3} → {BACK} (for {TEXT})"
    assert rs[0]["optional_replaced"] == [f"{HIB} removed from {BACK} → {JPA}"]
