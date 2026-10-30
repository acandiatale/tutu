"""v2.1l (orchestrator side): the restructure gets the as-is intake capture (`params["asis_effective"]`) and
the after-restructure capture (`params["effective_pom"]`) for `layout` / `boot_pom` / `msa_pom`, independent
of `layout.drop_managed_versions`; each version it carried over is a pom-verify row the next build
verifies."""
from __future__ import annotations

import json

import pytest

from sbmig.build.effective import _effective_path
from sbmig.core.interfaces import RestructureResult

from .fakes import FakeErrorList, make_profile
from .test_v2_orchestrator import make_v2


class _Recorder:
    def __init__(self):
        self.params: list[dict] = []

    def apply(self, ws, profile, cfg, *, mode, params=None):
        self.params.append(dict(params or {}))
        return RestructureResult(ok=True)


@pytest.mark.parametrize("drop", ["gte", "off"])
@pytest.mark.parametrize("mode", ["layout", "boot_pom", "msa_pom", "optional_bump"])
def test_pom_modes_get_both_captures(cfg, ws, mode, drop):
    cfg.layout.drop_managed_versions = drop  # type: ignore[assignment]
    rec = _Recorder()
    orch = make_v2(cfg, ws, restructurer=rec)
    orch.profile = make_profile()
    orch._call_restructurer(mode, {})
    got = rec.params[0]
    if mode == "optional_bump":
        assert "asis_effective" not in got and "effective_pom" not in got
    else:
        assert callable(got["asis_effective"]) and callable(got["effective_pom"])


def test_asis_effective_reads_the_intake_capture_and_never_raises(cfg, ws):
    orch = make_v2(cfg, ws)
    assert orch._asis_effective() is None                                   # no capture yet
    path = _effective_path(ws, "depmgmt.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert orch._asis_effective() is None                                   # unreadable → None
    path.write_text(json.dumps({"managed": {}, "direct": {}, "by_module": {}}), encoding="utf-8")
    assert orch._asis_effective() is None                                   # empty shape → None
    path.write_text(json.dumps({"managed": {"g:a": "1.0"}, "parent": "p:p:1"}), encoding="utf-8")
    data = orch._asis_effective()
    assert data is not None and data["managed"] == {"g:a": "1.0"} and data["parent"] == "p:p:1"


class _PinRestructurer:
    def apply(self, ws, profile, cfg, *, mode, params=None):
        ws.write("shop-back/pom.xml", "<project/>\n")
        pinned = ("org.apache.commons:commons-text: 1.10.0 ← as-is managed by "
                  "com.company.legacy:legacy-parent:1.0.0 (shop-back/pom.xml)")
        followup = ("declare a version for org.unknown:mystery in shop-back/pom.xml or manage it in the "
                    "parent — the as-is build did not resolve one")
        repo = ("org.unknown:other: 2.0.0 ← repository newest release (the as-is build did not resolve it) "
                "(shop-back/pom.xml)")
        return RestructureResult(ok=True, changed_files=["shop-back/pom.xml"], versions_pinned=[pinned, repo],
                                 versions_unknown=["org.unknown:mystery (shop-back/pom.xml)"],
                                 manual_followups=[followup])


def test_each_pin_is_a_pom_verify_row(cfg, ws):
    el = FakeErrorList()
    report = make_v2(cfg, ws, restructurer=_PinRestructurer(), error_list=lambda w, c, p: el).run()
    added = [args for args, _ in el.of("add_pom_verify")]
    row = "version carried over from the as-is build: org.apache.commons:commons-text 1.10.0"
    assert ("shop-back/pom.xml", row) in added
    assert ("shop-back/pom.xml", "version chosen from the repository: org.unknown:other 2.0.0") in added
    assert any("declare a version for org.unknown:mystery" in f for f in report.manual_followups)
