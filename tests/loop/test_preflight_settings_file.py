"""v2.1z-al: in the cluster Maven's settings file is a plain file the operator copies onto the data volume
(`/data/.m2/settings.xml`), so the preflight checks it: `build.settings_file` set and not a readable file → flag
`settings-not-found`, abort at intake before any Maven call (exit 3), in both run flows. An EMPTY file (created but
never filled) is the same flag with its own reason. Unset = Maven's default
settings, as on a PC: nothing changes."""
from __future__ import annotations

import pytest

from sbmig.cli import run_exit_code
from sbmig.core.models import CaseType, RunReport, utcnow
from sbmig.core.workspace import Workspace
from sbmig.report.i18n import tr_line

from .fakes import FakeBuildRunner, journal_events
from .test_orchestrator import make_orch
from .test_remediation_flow import IVAS_ENV, one_fix, run_flow


def _reason(path) -> str:
    return f"Maven settings file not found: {path} (build.settings_file) — put the settings.xml there and rerun"


def _no_maven(monkeypatch):
    import sbmig.build as b
    calls: list = []
    monkeypatch.setattr(b, "maven_available", lambda *a, **k: calls.append(a) or (True, "Java version: 21.0.1"))
    return calls


def _report() -> RunReport:
    return RunReport(run_id="x", project_name="p", case_type=CaseType.UNKNOWN, started_at=utcnow())


def test_a_missing_settings_file_aborts_at_intake_before_any_maven_call(cfg, ws, tmp_path, monkeypatch):
    missing = tmp_path / "m2" / "settings.xml"
    cfg.build.settings_file = str(missing)
    mvn = _no_maven(monkeypatch)
    build = FakeBuildRunner(ws)
    orch = make_orch(cfg, ws, build=build)
    orch._build_runner = None                       # the real preflight path
    report = orch.run()
    assert {"settings-not-found", "aborted", "infra:maven"} <= set(report.flags) and run_exit_code(report) == 3
    assert mvn == [] and build.calls == [] and report.phases == []
    assert orch.abort_reason == _reason(missing)
    assert f"run aborted: {_reason(missing)}" in report.manual_followups
    events = journal_events(ws)
    assert [e["reason"] for e in events if e["event"] == "abort"] == [_reason(missing)]
    assert next(e for e in events if e["event"] == "preflight")["settings_file"] == f"missing ({missing})"
    assert tr_line("ko", _reason(missing)) == (f"Maven settings.xml을 찾지 못했습니다({missing}). 이 경로에 settings.xml을 "
                                               "넣은 뒤 다시 실행해 주세요.")


def test_a_directory_is_not_a_settings_file(cfg, ws, tmp_path, monkeypatch):
    cfg.build.settings_file = str(tmp_path)
    _no_maven(monkeypatch)
    orch = make_orch(cfg, ws)
    r = _report()
    orch._preflight(r)
    assert "settings-not-found" in r.flags


@pytest.mark.parametrize("value", [None, "", "  ", "null"])
def test_no_settings_file_configured_checks_nothing(cfg, ws, monkeypatch, value):
    cfg.build.settings_file = value
    mvn = _no_maven(monkeypatch)
    orch = make_orch(cfg, ws)
    orch._build_runner = None
    r = _report()
    orch._preflight(r)
    assert "settings-not-found" not in r.flags and len(mvn) == 1


def test_a_present_settings_file_checks_nothing(cfg, ws, tmp_path, monkeypatch):
    f = tmp_path / "settings.xml"
    f.write_text("<settings/>\n", encoding="utf-8")
    cfg.build.settings_file = str(f)
    mvn = _no_maven(monkeypatch)
    orch = make_orch(cfg, ws)
    orch._build_runner = None
    r = _report()
    orch._preflight(r)
    assert "settings-not-found" not in r.flags and len(mvn) == 1
    assert not [e for e in journal_events(ws) if e["event"] == "abort"]


@pytest.mark.parametrize("engine", ["graph", "inline"])
def test_the_remediation_flow_stops_at_intake_before_its_own_readiness_check(cfg, tmp_path, monkeypatch, engine):
    """The remediation readiness says `server settings.xml not found (<path>)` for the same fact; the intake abort
    comes first, so a run shows one reason, the preflight's."""
    for k, v in IVAS_ENV.items():
        monkeypatch.setenv(k, v)
    missing = tmp_path / "nowhere" / "settings.xml"
    c = cfg.model_copy(deep=True)
    c.build.settings_file = str(missing)
    orch, ws, report, _, build = run_flow(c, tmp_path, one_fix(), engine=engine)
    assert isinstance(ws, Workspace)
    assert run_exit_code(report) == 3 and "settings-not-found" in report.flags and "aborted" in report.flags
    assert orch.abort_reason == _reason(missing) and build.calls == []
    events = journal_events(ws)
    assert [e["reason"] for e in events if e["event"] == "abort"] == [_reason(missing)]
    assert not [e for e in events if e["event"] == "remediation_skipped"]
    assert not any("server settings.xml not found" in f for f in report.manual_followups)


def test_an_empty_settings_file_aborts_with_its_own_reason(cfg, ws, tmp_path, monkeypatch):
    empty = tmp_path / "m2" / "settings.xml"
    empty.parent.mkdir()
    empty.write_bytes(b"")
    cfg.build.settings_file = str(empty)
    mvn = _no_maven(monkeypatch)
    build = FakeBuildRunner(ws)
    orch = make_orch(cfg, ws, build=build)
    orch._build_runner = None
    report = orch.run()
    reason = f"Maven settings file is empty: {empty} (build.settings_file) — copy the settings.xml there and rerun"
    assert {"settings-not-found", "aborted"} <= set(report.flags) and run_exit_code(report) == 3
    assert mvn == [] and build.calls == [] and orch.abort_reason == reason
    events = journal_events(ws)
    assert next(e for e in events if e["event"] == "preflight")["settings_file"] == f"empty ({empty})"
    assert tr_line("ko", reason) == (f"Maven settings.xml이 비어 있습니다({empty}). 이 경로에 settings.xml 내용을 넣은 뒤 "
                                     "다시 실행해 주세요.")
