"""Round 7b: Gradle / Ant / no-build projects are rejected at intake (before the effective-pom capture, before
analyze); `_step_restructure` carries the layout step's merge conflicts into the report."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.cli import run_exit_code
from sbmig.core.models import MergeConflict
from sbmig.core.workspace import Workspace

from .fakes import FakeBuildRunner, FakeRestructurer, journal_events, make_project
from .test_orchestrator import files_build, make_orch


def _boom_analyzer(ws, cfg):
    raise AssertionError("analyzer must not run for a non-Maven project")


def _ws_from(src: Path, tmp_path: Path) -> Workspace:
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


def _project(tmp_path: Path, files: dict[str, str]) -> Path:
    src = tmp_path / "src" / "proj"
    src.mkdir(parents=True)
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return src


CASES = [
    ("gradle", {"build.gradle": "plugins { id 'java' }\n", "src/main/java/A.java": "class A {}\n"}, "Gradle build"),
    ("gradle", {"settings.gradle.kts": "", "app/build.gradle.kts": ""}, "Gradle build"),
    ("ant", {"build.xml": "<project/>\n", "src/A.java": "class A {}\n"}, "Ant build.xml"),
    ("none", {"README.txt": "nothing to build\n"}, "no pom.xml found"),
    ("none", {}, "no pom.xml found"),
]


@pytest.mark.parametrize("engine", ["inline", "graph"])
@pytest.mark.parametrize("tool,files,why", CASES)
def test_non_maven_aborts_at_intake(cfg, tmp_path, engine, tool, files, why):
    cfg = cfg.model_copy(deep=True)
    cfg.loop.engine = engine
    ws = _ws_from(_project(tmp_path, files), tmp_path)
    build = FakeBuildRunner(ws, fn=files_build)
    captured: list[bool] = []
    orch = make_orch(cfg, ws, build=build, analyzer=_boom_analyzer)
    orch._capture_effective = lambda report: captured.append(True)        # must not be reached
    report = orch.run()
    assert f"unsupported-build-tool:{tool}" in report.flags and "aborted" in report.flags
    assert run_exit_code(report) == 3
    assert report.manual_followups[0].startswith(f"run aborted: {why}")
    assert "only Maven projects are supported" in report.manual_followups[0]
    assert build.calls == [] and not captured
    assert report.profile is None and report.final_build is None
    ev = [e for e in journal_events(ws) if e.get("event") == "build_tool"]
    assert ev and ev[0]["tool"] == tool and ev[0]["ok"] is False and why in ev[0]["reason"]
    assert not any(e.get("event") == "analyze" for e in journal_events(ws))
    assert (ws.run_dir / "report.json").exists()
    assert why in (ws.run_dir / "report.md").read_text()


def test_maven_project_passes_intake(cfg, ws):
    build = FakeBuildRunner(ws, fn=files_build)
    report = make_orch(cfg, ws, build=build).run()
    assert not any(f.startswith("unsupported-build-tool") for f in report.flags)
    ev = [e for e in journal_events(ws) if e.get("event") == "build_tool"]
    assert ev == [{**ev[0], "event": "build_tool", "tool": "maven", "ok": True}]
    assert report.success and build.calls[-1]["label"] == "verify"


def test_profile_fallback_still_aborts_in_analyze(cfg, tmp_path):
    """A pom.xml exists (intake passes) but the analyzer still reports a non-Maven build: step_analyze aborts."""
    from .fakes import make_profile
    ws = _ws_from(make_project(tmp_path), tmp_path)
    report = make_orch(cfg, ws, profile=make_profile(build_tool="ant")).run()
    assert "aborted" in report.flags and "'ant'" in report.manual_followups[0]
    assert not any(f.startswith("unsupported-build-tool") for f in report.flags)


# ------------------------------------------------------------------ merge conflicts → report
CONFLICTS = [
    MergeConflict(path="src/main/resources/messages.properties", kept_from="core", dropped_from=["api"],
                  identical=False, saved_copies=["merge-conflicts/api/src/main/resources/messages.properties"]),
    MergeConflict(path="src/main/resources/logback.xml", kept_from="core", dropped_from=["api", "web"],
                  identical=True),
]


class MergingRestructurer(FakeRestructurer):
    def apply(self, ws, profile, cfg, *, mode, params=None):
        res = super().apply(ws, profile, cfg, mode=mode, params=params)
        if mode != "layout":
            return res
        return res.model_copy(update={
            "merged_modules": ["core", "api"], "kept_boot_module": "module/boot-jar",
            "merge_conflicts": [c.model_copy() for c in CONFLICTS],
            "manual_followups": res.manual_followups + ["1 merge conflict: review the dropped copy"]})


class OldStyleResult:
    """A result object without the round-7b fields (older plugin)."""
    ok = True
    changed_files: list = []
    generated_files: list = []
    moved_files = 0
    mandatory_added: list = []
    optional_bumped: list = []
    manual_followups: list = []
    notes: list = []
    error = None


class OldRestructurer(FakeRestructurer):
    def apply(self, ws, profile, cfg, *, mode, params=None):
        self.calls.append(mode)
        return OldStyleResult()


def test_restructure_copies_merge_conflicts(cfg, ws):
    rs = MergingRestructurer()
    report = make_orch(cfg, ws, restructurer=rs).run()
    assert "layout" in rs.calls
    assert [(c.path, c.kept_from) for c in report.merge_conflicts] == [(c.path, c.kept_from) for c in CONFLICTS]
    ev = [e for e in journal_events(ws) if e.get("event") == "restructure" and e.get("mode") == "layout"]
    assert ev and ev[0]["merged_modules"] == ["core", "api"] and ev[0]["kept_boot_module"] == "module/boot-jar"
    assert ev[0]["merge_conflicts"] == 2
    notes = [n for pr in report.phases for n in pr.notes]
    assert any("merged modules: core, api" in n and "kept boot module: module/boot-jar" in n for n in notes)
    assert "1 merge conflict: review the dropped copy" in report.manual_followups
    h = report.handover
    assert h is not None and any("kept the copy from core, dropped api" in x for x in h.merge_conflicts)
    assert any("review the dropped copy" in x for x in h.from_here)
    md = (ws.run_dir / "report.md").read_text()
    assert "**Merge conflicts**" in md and md.index("**Merge conflicts**") < md.index("From here on")


def test_restructure_dedupes_conflicts_across_steps(cfg, ws):
    from sbmig.core.models import Phase, PhaseResult
    orch = make_orch(cfg, ws, restructurer=MergingRestructurer())
    report = orch.run()
    assert len(report.merge_conflicts) == 2
    step = next(s for s in report.plan.steps if s.id == "restructure.apply")
    orch.report = report
    assert orch._step_restructure(PhaseResult(phase=Phase.RESTRUCTURE, step_ids=[step.id]), step)
    assert len(report.merge_conflicts) == 2


def test_restructure_result_without_merge_fields(cfg, ws):
    report = make_orch(cfg, ws, restructurer=OldRestructurer()).run()
    assert report.merge_conflicts == []
    ev = [e for e in journal_events(ws) if e.get("event") == "restructure"]
    assert ev and all(e["merge_conflicts"] == 0 and e["merged_modules"] == [] for e in ev)
