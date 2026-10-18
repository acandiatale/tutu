from __future__ import annotations

from sbmig.core.models import AdvisoryFinding, CaseType, ErrorKind, Phase, RunReport, utcnow
from sbmig.kb import FileKnowledgeBase
from sbmig.kb.ledger import FIXED_RESOLUTIONS, Ledger, advisory_record, render_learning_md


def finding(aid="springfox", outcome="replaced", **kw) -> AdvisoryFinding:
    base = dict(advisory_id=aid, library=aid, status="eol", title=f"{aid} is discontinued", policy="recipe",
                recipes=["org.openrewrite.java.springdoc.SwaggerToSpringDoc"], outcome=outcome,
                replacement="org.springdoc:springdoc-openapi-starter-webmvc-ui", followup="check the UI path " * 40)
    base.update(kw)
    return AdvisoryFinding(**base)


def run_report(*findings, flags=None) -> RunReport:
    return RunReport(run_id="t-run", project_name="demo", case_type=CaseType.SPRING_BOOT, started_at=utcnow(),
                     advisories=list(findings), flags=list(flags or []))


def test_advisory_record(ws, profile):
    r = advisory_record(finding(), profile, ws)
    assert r.resolution == "advisory" and r.phase == Phase.ANALYZE and r.error_kind == ErrorKind.UNKNOWN
    assert r.symbol == "springfox" and r.resolver_id == "recipe:org.openrewrite.java.springdoc.SwaggerToSpringDoc"
    assert r.replacement == "springfox → org.springdoc:springdoc-openapi-starter-webmvc-ui"
    assert r.outcome == "replaced" and r.fix_labels == ["dependency-replace"]
    assert r.recipe_id == "org.openrewrite.java.springdoc.SwaggerToSpringDoc"
    assert len(r.notes[0]) == 300 and r.notes[1:] == ["advisory:springfox", "status:eol"]
    g = advisory_record(finding("hystrix", "still-present", policy="guide", recipes=[], replacement=None), profile,
                        ws)
    assert g.resolver_id == "guide:-" and g.fix_labels == [] and g.recipe_id is None
    assert g.replacement == "hystrix → ?" and g.project == "demo" and g.run_id == "t-run"
    assert "advisory" not in FIXED_RESOLUTIONS


def test_record_run_writes_advisories_once(kb, ws, profile):
    rep = run_report(finding(), finding("hystrix", "still-present", policy="guide", recipes=[]))
    assert kb.record_run(rep, profile, ws).ledger_entries == 2
    assert kb.record_run(rep, profile, ws).ledger_entries == 0           # dedupe per run_id + advisory id
    rows = list(kb.ledger().iter(resolution="^advisory$"))
    assert [r.symbol for r in rows] == ["springfox", "hystrix"]


def test_record_run_respects_config(kb_cfg, ws, profile):
    off = kb_cfg.model_copy(update={"advisories": kb_cfg.advisories.model_copy(update={"ledger": False})})
    kb = FileKnowledgeBase(off)
    assert kb.record_run(run_report(finding()), profile, ws).ledger_entries == 0
    kb2 = FileKnowledgeBase(kb_cfg)
    assert kb2.record_run(run_report(finding(), flags=["dry-run"]), profile, ws).ledger_entries == 0


def test_summary_and_learning_section(tmp_path, ws, profile):
    led = Ledger(tmp_path / "ledger.jsonl")
    other = profile.model_copy(update={"name": "other", "fingerprint": "fp-other"})
    third = profile.model_copy(update={"name": "third", "fingerprint": "fp-third"})
    led.append([advisory_record(finding(), profile, ws),
                advisory_record(finding(outcome="still-present"), other, ws),
                advisory_record(finding("hystrix", "still-present", policy="guide", recipes=[]), third, ws)])
    s = led.summary()
    assert s["by_resolution"] == {"advisory": 3}
    assert s["llm_symbols"] == [] and s["kb_fired"] == [] and s["fixed_by_label"] == {}   # not a fix
    sf, hy = s["advisories"]
    assert sf == {"id": "springfox", "library": "springfox", "status": "eol", "policy": "recipe", "count": 2,
                  "replaced": 1, "still_present": 1,
                  "replacement": "springfox → org.springdoc:springdoc-openapi-starter-webmvc-ui",
                  "projects": 2, "project_names": ["demo", "other"]}
    assert hy["id"] == "hystrix" and hy["projects"] == 1 and hy["policy"] == "guide"
    md = render_learning_md(s)
    assert "## Libraries needing owner action (advisories)" in md
    assert "| springfox | springfox | eol | recipe | 2 | 1 | 1 |" in md
    empty = render_learning_md(Ledger(tmp_path / "none.jsonl").summary())
    assert "## Libraries needing owner action (advisories)\n\n_none_" in empty
