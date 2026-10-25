from __future__ import annotations

import json

import pytest

from sbmig.core.models import (
    GAV,
    BlockedItem,
    BlockReason,
    BuildError,
    CaseType,
    DecisionKind,
    ErrorKind,
    FixAttempt,
    FleetState,
    HarvestResult,
    LLMUsage,
    Phase,
    PhaseResult,
    RewriteResult,
    RunReport,
    TriageDecision,
    utcnow,
)
from sbmig.fleet import FleetRunner

from .conftest import make_project


def report(
    name: str, *, success=True, aborted=False, blocked=0, llm=0, harvest=None, phases=None
) -> RunReport:
    r = RunReport(
        run_id="x",
        project_name=name,
        case_type=CaseType.SPRING_BOOT,
        started_at=utcnow(),
        success=success and not aborted,
        llm_usage=LLMUsage(calls=llm, input_tokens=10 * llm),
        harvest=harvest,
        phases=phases or [],
    )
    if aborted:
        r.flags.append("aborted")
        r.manual_followups.append("run aborted: no pom")
    for i in range(blocked):
        r.blocked.append(
            BlockedItem(
                id=f"b{i}",
                reason=BlockReason.EXTERNAL_JAVAX_API,
                phase=Phase.SPRING,
                cluster_id="c",
                artifact=GAV(group_id="com.co", artifact_id="legacy"),
            )
        )
    if not success and not aborted:
        r.unresolved.append(BuildError(kind=ErrorKind.COMPILE, message="x"))
    return r


class FakeRun:
    """run_project stand-in: behaviour per project name; records (name, max_llm_calls, run_id)."""

    def __init__(self, **behaviour):
        self.behaviour = behaviour
        self.calls: list[tuple[str, int, str]] = []

    def __call__(self, project, cfg, out_dir, run_id):
        self.calls.append((project.name, cfg.loop.max_llm_calls, run_id))
        b = self.behaviour.get(project.name, {})
        if callable(b):
            return b(project, cfg)
        if b == "crash":
            raise RuntimeError("boom")
        return report(project.name, **b)


def _state(out) -> FleetState:
    return FleetState.model_validate_json((out / "fleet" / "state.json").read_text())


def test_runs_all_and_writes_summaries(fleet_cfg, projects_dir, tmp_path):
    out = tmp_path / "out"
    att = FixAttempt(
        id="a",
        phase=Phase.SPRING,
        iteration=1,
        cluster_id="c",
        applied=True,
        outcome="improved",
        decision=TriageDecision(
            cluster_id="c",
            kind=DecisionKind.KB_RECIPE,
            recipe_ids=["Foo", "com.company.kb.Legacy", "org.openrewrite.Builtin"],
        ),
    )
    patch = att.model_copy(
        update={
            "decision": TriageDecision(
                cluster_id="c", kind=DecisionKind.KB_PATCH, kb_case_ids=["20260101-aaa"]
            )
        }
    )
    rw = RewriteResult(
        ok=True,
        recipes=[],
        recipes_applied={"Bar": ["A.java"], "org.openrewrite.X": ["B.java"]},
    )
    phase = PhaseResult(phase=Phase.SPRING, attempts=[att, patch], rewrite_results=[rw], build_ok=True)
    fake = FakeRun(
        alpha={
            "llm": 2,
            "harvest": HarvestResult(
                cases_created=["c1"], candidates_created=["r1"], recipes_promoted=["r0"]
            ),
        },
        beta={"success": False, "blocked": 1, "phases": [phase]},
    )
    runner = FleetRunner(fleet_cfg, projects_dir, out, run_project=fake)
    state = runner.run()
    assert [c[0] for c in fake.calls] == ["alpha", "beta", "gamma"]
    assert [p.status for p in state.projects] == ["success", "incomplete", "success"]
    assert runner.exit_code == 2 and state.finished_at is not None
    a, b, _ = state.projects
    assert a.llm_calls == 2 and a.llm_input_tokens == 20 and a.kb_cases_created == ["c1"]
    assert a.kb_candidates_created == ["r1"] and a.kb_recipes_promoted == ["r0"] and a.exit_code == 0
    assert b.blocked == 1 and b.blocked_reasons == ["external_javax_api:com.co:legacy"] and b.unresolved == 1
    assert b.kb_recipes_used == ["Foo", "com.company.kb.Legacy", "case:20260101-aaa", "Bar"]
    assert state.llm_calls_total == 2
    assert fake.calls[0][2] == f"{state.fleet_id}-001-alpha" and a.run_id == fake.calls[0][2]
    fleet = out / "fleet"
    for f in ("state.json", "summary.md", "summary.csv", "summary.json", "learning.md"):
        assert (fleet / f).exists(), f
    md = (fleet / "summary.md").read_text()
    assert "| 2 | beta | spring_boot | incomplete | 2 |" in md and "external_javax_api:com.co:legacy" in md
    raw_csv = (fleet / "summary.csv").read_bytes()
    assert raw_csv.startswith(b"\xef\xbb\xbf")                        # UTF-8 BOM: Excel opens Korean paths right
    csv_lines = raw_csv.decode("utf-8-sig").splitlines()
    assert csv_lines[0].startswith("#,name,case,status") and len(csv_lines) == 4
    assert csv_lines[0].endswith(",error,attention,input")
    assert ",unresolved:1;blocked:" not in csv_lines[2] and "blocked:external_javax_api;unresolved:1" in csv_lines[2]
    assert json.loads((fleet / "summary.json").read_text())["fleet_id"] == state.fleet_id
    assert "what the KB learned" in (fleet / "learning.md").read_text()


def test_resume_skips_successes_and_retry_failed(fleet_cfg, projects_dir, tmp_path):
    out = tmp_path / "out"
    fake = FakeRun(beta={"success": False}, gamma={"aborted": True})
    s1 = FleetRunner(fleet_cfg, projects_dir, out, run_project=fake).run()
    assert [p.status for p in s1.projects] == ["success", "incomplete", "aborted"]
    assert s1.projects[2].exit_code == 3 and s1.projects[2].error == "run aborted: no pom"
    fake2 = FakeRun()
    s2 = FleetRunner(fleet_cfg, projects_dir, out, run_project=fake2).run()
    assert fake2.calls == [] and s2.fleet_id == s1.fleet_id  # resume: nothing to do
    retry_cfg = fleet_cfg.model_copy(
        update={"fleet": fleet_cfg.fleet.model_copy(update={"retry_failed": True})}
    )
    s3 = FleetRunner(retry_cfg, projects_dir, out, run_project=fake2).run()
    assert [c[0] for c in fake2.calls] == ["beta", "gamma"]
    assert [p.status for p in s3.projects] == ["success"] * 3
    assert s3.projects[1].attempt == 2 and s3.projects[1].run_id.endswith("-002-beta-r2")
    assert FleetRunner(retry_cfg, projects_dir, out, run_project=fake2).run().projects[0].attempt == 1


def test_new_projects_in_manifest_are_appended_and_running_is_rerun(fleet_cfg, projects_dir, tmp_path):
    out = tmp_path / "out"
    FleetRunner(fleet_cfg, projects_dir, out, run_project=FakeRun()).run()
    st = _state(out)
    st.projects[1].status = "running"  # fleet process died during beta
    (out / "fleet" / "state.json").write_text(st.model_dump_json())
    make_project(projects_dir, "delta")
    fake = FakeRun()
    s = FleetRunner(fleet_cfg, projects_dir, out, run_project=fake).run()
    assert [c[0] for c in fake.calls] == ["beta", "delta"]
    assert [p.name for p in s.projects] == ["alpha", "beta", "gamma", "delta"]
    assert s.projects[1].attempt == 2


def test_stop_on_abort(fleet_cfg, projects_dir, tmp_path):
    cfg = fleet_cfg.model_copy(update={"fleet": fleet_cfg.fleet.model_copy(update={"stop_on_abort": True})})
    fake = FakeRun(alpha={"aborted": True})
    runner = FleetRunner(cfg, projects_dir, tmp_path / "out", run_project=fake)
    state = runner.run()
    assert [c[0] for c in fake.calls] == ["alpha"] and runner.exit_code == 3
    assert [p.status for p in state.projects] == ["aborted", "pending", "pending"]
    assert any("stopped on abort" in n for n in state.notes) and state.finished_at is None


def test_unsupported_build_tool_is_a_skip_not_an_abort(fleet_cfg, projects_dir, tmp_path):
    """Round 9: a Gradle / Ant project rejected at intake (exit 3, flag unsupported-build-tool:<tool>) is `skipped`:
    stop_on_abort does not stop the fleet, retry_failed does not re-run it, the reason is in the row."""
    def gradle(project, cfg):
        r = report(project.name, aborted=True)
        r.flags.append("unsupported-build-tool:gradle")
        r.manual_followups[0] = "run aborted: unsupported build tool gradle (build.gradle); only Maven projects"
        return r

    cfg = fleet_cfg.model_copy(update={"fleet": fleet_cfg.fleet.model_copy(
        update={"stop_on_abort": True, "retry_failed": True})})
    fake = FakeRun(alpha=gradle)
    runner = FleetRunner(cfg, projects_dir, tmp_path / "out", run_project=fake)
    state = runner.run()
    assert [c[0] for c in fake.calls] == ["alpha", "beta", "gamma"]          # the fleet went on
    a = state.projects[0]
    assert a.status == "skipped" and a.exit_code == 3 and a.error.startswith("unsupported build tool: gradle")
    assert [p.status for p in state.projects[1:]] == ["success", "success"]
    assert runner.exit_code == 2 and not runner.stopped_on_abort and state.finished_at is not None
    # resume with retry_failed: the skipped project is not re-run
    fake2 = FakeRun(alpha=gradle)
    state2 = FleetRunner(cfg, projects_dir, tmp_path / "out", run_project=fake2).run()
    assert fake2.calls == [] and state2.projects[0].status == "skipped"


def test_max_projects(fleet_cfg, projects_dir, tmp_path):
    cfg = fleet_cfg.model_copy(update={"fleet": fleet_cfg.fleet.model_copy(update={"max_projects": 2})})
    fake = FakeRun()
    runner = FleetRunner(cfg, projects_dir, tmp_path / "out", run_project=fake)
    state = runner.run()
    assert [c[0] for c in fake.calls] == ["alpha", "beta"] and runner.exit_code == 2
    assert state.projects[2].status == "pending" and any("max_projects=2" in n for n in state.notes)
    assert "progress: 2 of 3 done" in (tmp_path / "out" / "fleet" / "summary.md").read_text()
    runner2 = FleetRunner(cfg, projects_dir, tmp_path / "out", run_project=fake)
    runner2.run()
    assert [c[0] for c in fake.calls][-1] == "gamma" and runner2.exit_code == 0


def test_llm_budget_threading(fleet_cfg, projects_dir, tmp_path):
    loop = fleet_cfg.loop.model_copy(update={"max_llm_calls": 10})
    cfg = fleet_cfg.model_copy(
        update={"loop": loop, "fleet": fleet_cfg.fleet.model_copy(update={"max_llm_calls_total": 12})}
    )
    fake = FakeRun(alpha={"llm": 7}, beta={"llm": 5})
    state = FleetRunner(cfg, projects_dir, tmp_path / "out", run_project=fake).run()
    assert [c[1] for c in fake.calls] == [10, 5, 0]  # min(per-project, remaining)
    assert state.llm_calls_total == 12 and "llm-budget-exhausted:gamma" in state.notes
    unlimited = FakeRun()
    FleetRunner(fleet_cfg, projects_dir, tmp_path / "out2", run_project=unlimited).run()
    assert {c[1] for c in unlimited.calls} == {fleet_cfg.loop.max_llm_calls}


def test_crash_is_aborted_and_fleet_continues(fleet_cfg, projects_dir, tmp_path):
    fake = FakeRun(alpha="crash")
    runner = FleetRunner(fleet_cfg, projects_dir, tmp_path / "out", run_project=fake)
    state = runner.run()
    assert [c[0] for c in fake.calls] == ["alpha", "beta", "gamma"]
    a = state.projects[0]
    assert a.status == "aborted" and a.exit_code == 3 and "RuntimeError: boom" in a.error
    assert [p.status for p in state.projects[1:]] == ["success", "success"] and runner.exit_code == 2


def test_missing_input_skipped(fleet_cfg, tmp_path):
    make_project(tmp_path / "src", "ok")
    m = tmp_path / "list.txt"
    m.write_text("src/ok\nsrc/gone\n")
    fake = FakeRun()
    state = FleetRunner(fleet_cfg, m, tmp_path / "out", run_project=fake).run()
    assert [c[0] for c in fake.calls] == ["ok"]
    assert state.projects[1].status == "skipped" and "input not found" in state.projects[1].error
    make_project(tmp_path / "src", "gone")  # appears later → runs on resume
    FleetRunner(fleet_cfg, m, tmp_path / "out", run_project=fake).run()
    assert fake.calls[-1][0] == "gone"


def test_simple_first_order_and_per_project_config(fleet_cfg, projects_dir, tmp_path):
    kinds = {"alpha": CaseType.MSA, "beta": CaseType.PURE_JAVA, "gamma": CaseType.SPRING_BOOT}
    seen = []

    def analyze(p, cfg):
        seen.append(p.name)
        return kinds[p.name]

    extra = tmp_path / "extra.yml"
    extra.write_text("loop: {max_llm_calls: 7}\nkb: {dir: /elsewhere}\n")
    m = tmp_path / "fleet.yml"
    m.write_text(f"""projects:
  - {{input: {projects_dir / "alpha"}}}
  - {{input: {projects_dir / "beta"}, configs: [{extra}]}}
  - {{input: {projects_dir / "gamma"}, overrides: {{loop: {{max_llm_calls: 3}}}}}}
""")
    cfg = fleet_cfg.model_copy(update={"fleet": fleet_cfg.fleet.model_copy(update={"order": "simple-first"})})
    cfgs = {}

    def run(p, c):
        cfgs[p.name] = c
        return report(p.name)

    fake = FakeRun(alpha=run, beta=run, gamma=run)
    runner = FleetRunner(cfg, m, tmp_path / "out", run_project=fake, analyze=analyze)
    runner.run()
    assert [c[0] for c in fake.calls] == ["beta", "gamma", "alpha"]
    assert [c[1] for c in fake.calls] == [7, 3, fleet_cfg.loop.max_llm_calls]
    assert cfgs["beta"].kb.dir == fleet_cfg.kb.dir  # the KB stays fleet-wide
    # case types are cached in the state: no re-analysis on resume
    seen.clear()
    FleetRunner(cfg, m, tmp_path / "out", run_project=fake, analyze=analyze).run()
    assert seen == []


def test_dry_run_runs_nothing(fleet_cfg, projects_dir, tmp_path):
    fake = FakeRun()
    runner = FleetRunner(fleet_cfg, projects_dir, tmp_path / "out", run_project=fake, dry_run=True)
    runner.run()
    assert fake.calls == [] and not (tmp_path / "out" / "fleet" / "state.json").exists()
    assert [(i, r.name, a) for i, r, a in runner.planned] == [
        (1, "alpha", "run"),
        (2, "beta", "run"),
        (3, "gamma", "run"),
    ]


def test_reindex_every_and_shared_kb(fleet_cfg, projects_dir, tmp_path):
    class KB:
        def __init__(self):
            self.reindexed = 0

        def reindex(self):
            self.reindexed += 1
            return 0

    kb = KB()
    cfg = fleet_cfg.model_copy(update={"fleet": fleet_cfg.fleet.model_copy(update={"reindex_every": 2})})
    FleetRunner(cfg, projects_dir, tmp_path / "out", run_project=FakeRun(), kb=kb).run()
    assert kb.reindexed == 1


def test_keyboard_interrupt_marks_pending(fleet_cfg, projects_dir, tmp_path):
    def interrupt(p, c):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        FleetRunner(fleet_cfg, projects_dir, tmp_path / "out", run_project=FakeRun(beta=interrupt)).run()
    st = _state(tmp_path / "out")
    assert [p.status for p in st.projects] == ["success", "pending", "pending"]
    assert st.projects[1].error == "interrupted"


def test_advisories_and_carried_over_columns(fleet_cfg, projects_dir, tmp_path):
    from sbmig.core.models import AdvisoryFinding, HandoverSummary, VersionChange

    def with_advisories(project, cfg):
        r = report(project.name)
        r.advisories = [
            AdvisoryFinding(advisory_id="springfox", library="springfox", status="eol", title="t", policy="recipe",
                            outcome="replaced"),
            AdvisoryFinding(advisory_id="hystrix", library="hystrix", status="eol", title="t", policy="guide",
                            outcome="still-present"),
        ]
        co = VersionChange(key="a:b", before="1", after="1", before_source="managed", carried_over=True)
        r.handover = HandoverSummary(changes=[co], carried_over=[co])
        return r

    def hystrix_only(project, cfg):
        r = report(project.name)
        r.advisories = [AdvisoryFinding(advisory_id="hystrix", library="hystrix", status="eol", title="t",
                                        policy="guide", outcome="still-present")]
        return r

    out = tmp_path / "out"
    fake = FakeRun(alpha=with_advisories, beta=hystrix_only)
    state = FleetRunner(fleet_cfg, projects_dir, out, run_project=fake).run()
    a, b, c = state.projects
    assert a.advisories == ["springfox:replaced", "hystrix:still-present"] and a.carried_over == 1
    assert b.advisories == ["hystrix:still-present"] and b.carried_over == 0
    assert c.advisories == [] and c.carried_over == 0
    fleet = out / "fleet"
    head = (fleet / "summary.csv").read_text(encoding="utf-8-sig").splitlines()[0].split(",")
    assert head.index("advisories") == head.index("unresolved") + 1 and "carried_over" in head
    md = (fleet / "summary.md").read_text()
    assert "| advisories | carried_over |" in md
    assert "## Libraries flagged across the fleet" in md
    assert "| hystrix | 2 | 0 | 2 | 0 | alpha, beta |" in md and "| springfox | 1 | 1 | 0 | 0 | alpha |" in md
    assert "alpha (1)" in md
    # state.json round-trips the new fields; older state files without them still load
    st = _state(out)
    assert st.projects[0].advisories == a.advisories
    raw = json.loads((fleet / "state.json").read_text())
    for p in raw["projects"]:
        p.pop("advisories"), p.pop("carried_over")
    assert FleetState.model_validate(raw).projects[0].carried_over == 0
