"""report.md / report.json of a remediation (sbmig/remediations/report.py)."""
from __future__ import annotations

import json
import re

from sbmig.remediations import graph, report
from sbmig.remediations.models import (
    D_PENDING,
    FAILED,
    INCOMPLETE,
    SUCCEEDED,
    BoundaryFinding,
    Residual,
    RoundPlan,
)

from .fakes import (
    FakeIntakeFns,
    FakeMaven,
    FakeScanner,
    Recorder,
    ScriptedPlanner,
    finding,
    make_cfg,
    make_components,
    make_run,
    proposal,
    scan_result,
)


def _run(tmp_path, **kw):
    state, paths = make_run(tmp_path)
    cfg = kw.pop("cfg", None) or make_cfg()
    comps = make_components(**kw)
    rec = Recorder()
    out = graph.run_remediation(state, paths, cfg, comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    return out, paths


def _fixing(**kw):
    return dict(scan=FakeScanner({"baseline": [scan_result(finding())], "final-r1": [scan_result()]}),
                plan=ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()])]),
                **kw)


def test_succeeded_report(tmp_path):
    out, paths = _run(tmp_path, **_fixing())
    assert out.status == SUCCEEDED
    md = paths.report_md.read_text(encoding="utf-8")
    lines = md.splitlines()
    assert lines[0].startswith("# 취약점 조치 보고서")
    assert lines[2].startswith("> ✅ **성공** (`SUCCEEDED`) — ") and "## 다음에 할 일" in md
    assert "### 라운드 1" in md and "`example:lib`" in md and "1.0 → 1.1" in md and "자동 적용" in md
    assert "저장소 nexus.invalid" in md and "repository:nexus.invalid" not in md
    assert "## 고친 것" in md and "| `example:lib` | 1.0 → 1.1 | 직접 선언 버전 · pom.xml:" in md
    assert f"sha256: `{out.delivery.sha256}`" in md
    assert "최종 검사한 ZIP 과 sha256 이 같은가: 예" in md
    assert "## 바꾸지 않은 것" in md
    data = json.loads(paths.report_json.read_text(encoding="utf-8"))
    assert data["schema"] == report.REPORT_SCHEMA and data["state"]["status"] == SUCCEEDED
    assert data["delivery_matches_scanned_zip"] is True
    assert data["final_scan"]["complete"] is True and data["baseline_scan"]["findings"] == 1
    assert data["rounds"][0]["plan"]["proposals"][0]["to"] == "1.1"


def test_baseline_failure_report(tmp_path):
    out, paths = _run(tmp_path, maven=FakeMaven(outcomes=[False]))
    assert out.status == FAILED
    md = paths.report_md.read_text(encoding="utf-8")
    assert "> ❌ **실패** (`FAILED`)" in md and "`baseline_build_failed`" in md
    assert "기준 빌드 실패" in md and "baseline/build.log" in md
    assert "### 전달 ZIP" in md and "없습니다" in md and "| 결과 ZIP | ❌ 없음 |" in md


def test_incomplete_report_lists_residuals_and_boundary(tmp_path):
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()],
                                      residuals=[Residual(ga="example:vendored", version="0.9", origin="vendored",
                                                          reason="manual_review", detail="WEB-INF/lib|jar")])])
    b = [BoundaryFinding(code="maven_wrapper", path="mvnw", action="ignore", detail="not executed")]
    out, paths = _run(tmp_path, **{**_fixing(), "plan": plan, "intake": FakeIntakeFns(boundary=b)})
    assert out.status == INCOMPLETE
    md = paths.report_md.read_text(encoding="utf-8")
    assert "(`INCOMPLETE`)" in md and "`manual_review`" in md
    assert "`example:vendored`" in md and "WEB-INF/lib\\|jar" in md
    assert "`maven_wrapper` `mvnw` → 기록만" in md


def test_report_is_deterministic_and_has_no_absolute_path(tmp_path):
    out, paths = _run(tmp_path, **_fixing())
    first_md, first_json = paths.report_md.read_bytes(), paths.report_json.read_bytes()
    report.write_report(out, paths)
    assert paths.report_md.read_bytes() == first_md and paths.report_json.read_bytes() == first_json
    for text in (first_md.decode(), first_json.decode()):
        assert str(tmp_path) not in text
        assert str(tmp_path.resolve()) not in text


def test_absolute_paths_in_messages_are_relativized(tmp_path):
    out, paths = _run(tmp_path, **_fixing())
    out.message = f"see {paths.root}/rounds/1/build.log"
    report.write_report(out, paths)
    md = paths.report_md.read_text(encoding="utf-8")
    assert "see rounds/1/build.log" in md and str(paths.root) not in md


def test_summary_is_small_and_json(tmp_path):
    out, _ = _run(tmp_path, **_fixing())
    s = report.summary(out)
    assert s["status"] == SUCCEEDED and s["delivery"]["sha256"] == out.delivery.sha256
    assert set(s) >= {"id", "name", "status", "reason", "reasons", "counts", "round", "next_node"}
    json.dumps(s)


def test_pending_proposal_decision_label(tmp_path):
    scan = FakeScanner({"baseline": [scan_result(finding())]})
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="",
                                      proposals=[proposal(decision=D_PENDING, risks=["major"])])])
    out, paths = _run(tmp_path, scan=scan, plan=plan)
    assert not paths.report_md.exists()          # a waiting status writes no report
    report.write_report(out, paths)               # the service may still render one on demand
    md = paths.report_md.read_text(encoding="utf-8")
    assert "승인 대기" in md and "메이저 변경" in md and "`pending_approval`" in md
    assert "## 승인을 기다리는 것" in md and "체크박스" in md and "| 승인을 기다리는 것 | 제안 1건 |" in md


def test_report_scrubs_secret_env_values_without_the_output_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("IVAS_PASSWORD", "s3cr3t-report-value")
    state, paths = make_run(tmp_path)
    state.status, state.reason, state.message = FAILED, "internal_error", "boom s3cr3t-report-value"
    state.residuals = [Residual(ga="example:lib", version="1", origin="unmapped", reason="manual_review",
                                detail="https://u:s3cr3t-report-value@nexus.invalid/x")]
    report.write_report(state, paths)
    text = paths.report_md.read_text(encoding="utf-8") + paths.report_json.read_text(encoding="utf-8")
    assert "s3cr3t-report-value" not in text and "nexus.invalid" in text


def test_failed_candidate_included_report_says_so_first(tmp_path):
    out, paths = _run(tmp_path, **_fixing(maven=FakeMaven(outcomes=[True, False])))
    assert out.status == FAILED and out.delivery is not None and not out.delivery.build_ok
    md = paths.report_md.read_text(encoding="utf-8")
    lines = md.splitlines()
    assert lines[0].startswith("# 취약점 조치 보고서")
    head = "\n".join(lines[1:8])              # the result line, then the warning block right under it
    assert lines[2].startswith("> ❌ **실패** (`FAILED`)") and lines[4].startswith("> **주의: 결과 ZIP")
    assert "빌드가 실패한 수정본" in head and "라운드 1" in head and "p-1-1" in head
    assert "rounds/1/build.log" in head and "재검사를 하지 않았습니다" in head and "source.zip" in head
    assert "빌드 성공(build_ok): 아니오" in md and "재검사했는가(scanned): 아니오" in md
    assert "결과 ZIP 은 없고" not in md
    data = json.loads(paths.report_json.read_text(encoding="utf-8"))
    assert data["delivery"]["build_ok"] is False and data["delivery"]["scanned"] is False
    assert data["verified_delivery"] is None
    s = report.summary(out)
    assert s["delivery"]["build_ok"] is False and s["delivery"]["scanned"] is False


def test_failed_candidate_discarded_report_has_no_warning_block(tmp_path):
    out, paths = _run(tmp_path, cfg=make_cfg(failed_candidate="discard"),
                      **_fixing(maven=FakeMaven(outcomes=[True, False])))
    assert out.status == FAILED and out.delivery is None
    md = paths.report_md.read_text(encoding="utf-8")
    assert "빌드가 실패한 수정본" not in md and "결과 ZIP 은 없고" in md
    assert json.loads(paths.report_json.read_text(encoding="utf-8"))["delivery"] is None


def test_succeeded_report_shows_build_ok_and_scanned(tmp_path):
    _out, paths = _run(tmp_path, **_fixing())
    md = paths.report_md.read_text(encoding="utf-8")
    assert "빌드 성공(build_ok): 예" in md and "재검사했는가(scanned): 예" in md
    data = json.loads(paths.report_json.read_text(encoding="utf-8"))
    assert data["delivery"]["build_ok"] is True and data["delivery"]["scanned"] is True


def test_proposal_note_reaches_the_report(tmp_path):
    """A proposal's note (property governs other artifacts / the override must be removed later) is shown under the table."""
    from sbmig.remediations.models import (
        K_MANAGED_OVERRIDE,
        Proposal,
        RemediationState,
        RoundPlan,
        RoundState,
        RunPaths,
        write_json_atomic,
    )
    from sbmig.remediations.report import build_report_json, build_report_md
    paths = RunPaths(tmp_path)
    prop = Proposal(id="p-1-1", round=1, ga="example:lib", from_version="1.0.0", to_version="1.0.1", kind=K_MANAGED_OVERRIDE,
                    decision="approved", note="the owners must remove the override when the parent catches up")
    write_json_atomic(paths.plan(1), RoundPlan(round=1, source_tree_hash="t", policy_hash="p", proposals=[prop]).to_dict())
    state = RemediationState(id="r1", status="INCOMPLETE", rounds=[RoundState(round=1, applied=["p-1-1"])])
    assert "the owners must remove the override" in build_report_md(state, paths)
    plans = build_report_json(state, paths)
    assert "the owners must remove the override" in str(plans)


def test_ignored_findings_table_uses_the_given_codes(tmp_path):
    ignored = finding(ga="example:patched", cve="CVE-2026-0009")
    ignored.action = "PC"
    scan = FakeScanner({"baseline": [scan_result(finding(), ignored)], "final-r1": [scan_result(ignored)]})
    out, paths = _run(tmp_path, **{**_fixing(), "scan": scan, "cfg": make_cfg(ignore_actions=["PC", "IG"])})
    assert out.status == SUCCEEDED and out.counts["final_ignored"] == 1
    md = paths.report_md.read_text(encoding="utf-8")
    assert "조치 대상 1건, 조치 대상에서 뺀 항목 1건" in md
    assert "| CVE-2026-0009 | 라이브러리 | `example:patched` | 1.0 | PC | 패치 완료 |" in md
    data = report.build_report_json(out, paths, ignore_actions=["PC", "IG"])
    assert data["ignored_findings"][0]["meaning"] == "패치 완료"
    # without codes (the service renders report.json without the cfg) the default is the empty list: nothing ignored
    data = report.build_report_json(out, paths)
    assert data["ignored_findings"] == [] and data["final_scan"]["actionable"] == 1 and data["ignore_actions"] == []


def test_chain_budget_reason_has_a_korean_text():
    meaning, step = report._REASON_KO["chain_budget"]
    assert "chain_budget_minutes" in meaning and "재개" in step


def test_scoped_proposal_lists_its_modules_and_a_legacy_one_does_not(tmp_path):
    scoped = proposal()
    scoped.modules, scoped.also_modules = ["app-domain"], ["app-service", "app-store"]
    out, paths = _run(tmp_path, **{**_fixing(), "plan": ScriptedPlanner([RoundPlan(round=1, source_tree_hash="",
                                                                                   policy_hash="", proposals=[scoped])])})
    md = paths.report_md.read_text(encoding="utf-8")
    assert "- 다스리는 모듈 `p-1-1`: app-domain" in md
    assert "- 이 수정이 닿지 않을 수 있는 모듈 `p-1-1`: app-service, app-store" in md
    fixed = md.split("## 고친 것", 1)[1].split("\n## ", 1)[0]
    assert "  - 다스리는 모듈: app-domain" in fixed
    assert "  - 이 수정이 닿지 않을 수 있는 모듈(같은 버전이 다른 길로 들어옴): app-service, app-store" in fixed
    data = json.loads(paths.report_json.read_text(encoding="utf-8"))
    pj = data["rounds"][0]["plan"]["proposals"][0]
    assert pj["modules"] == ["app-domain"] and pj["also_modules"] == ["app-service", "app-store"]

    legacy_out, legacy_paths = _run(tmp_path / "legacy", **_fixing())
    md = legacy_paths.report_md.read_text(encoding="utf-8")
    assert "다스리는 모듈" not in md and "닿지 않을 수 있는 모듈" not in md
    pj = json.loads(legacy_paths.report_json.read_text(encoding="utf-8"))["rounds"][0]["plan"]["proposals"][0]
    assert "modules" not in pj and "also_modules" not in pj


# ---------------------------------------------------------------------------- redesigned Korean report (2026-11-12)
CORE, WEB, COMP = "com.example.inhouse:inhouse-core", "com.example.inhouse:inhouse-web", "com.example.inhouse:comp-jar"
NOT_INHERIT = ("module(s) {} do not inherit from the root pom.xml: an override entry there would not reach them")


def _company(tmp_path, *, legacy: bool = False, counts: dict | None = None):
    """A company-shaped finished state built by hand: one committed proposal, in-house residuals (library + parent
    shapes, one library under two artifacts), manual_review residuals sharing one English detail, a classifier variant."""
    from sbmig.remediations.models import (
        BuildOutcome,
        DeliveryInfo,
        Finding,
        Proposal,
        RemediationState,
        RoundState,
        RunPaths,
        ScanResult,
        TextEdit,
        write_json_atomic,
    )
    paths = RunPaths(tmp_path / "run")
    res, finds = [], []

    def add(ga, v, reason, detail, cve, *, origin="transitive", modules=(), via=(), inhouse=(), score=7.5):
        r = Residual(ga=ga, version=v, origin=origin, reason=reason, detail=detail, finding_ids=[cve])
        if not legacy:
            r.modules, r.via, r.inhouse = list(modules), list(via), list(inhouse)
        res.append(r)
        finds.append(Finding(id=cve, kind="library", identifier=ga, version=v, action="NE", score=score))

    via_core = f"brought in by the in-house artifact(s) {CORE}: only their owner can change it"
    add("org.example:json", "2.0", "inhouse_artifact", f"brought in by the in-house artifact(s) {CORE}, {WEB}: only their "
        "owner can change it", "CVE-2026-0101", modules=["app-domain"], via=[f"{CORE}:4.0", "org.example:json:2.0"],
        inhouse=[CORE, WEB], score=9.8)
    add("org.example:yaml", "1.3", "inhouse_artifact", via_core, "CVE-2026-0102", modules=["app-domain"],
        via=[f"{CORE}:4.0", "org.example:yaml:1.3"], inhouse=[CORE])
    add("org.example:spring-web", "6.0", "inhouse_artifact", f"brought in by the in-house artifact(s) {COMP}: only their "
        "owner can change it", "CVE-2026-0103", origin="parent_managed", modules=["app-store"], inhouse=[COMP])
    mods = [["app-a", "app-b"], ["app-c"], ["app-a", "app-b", "app-c", "app-d", "app-e"]]
    for i, ms in enumerate(mods):
        add(f"org.example:m{i}", "1.0", "manual_review", NOT_INHERIT.format(", ".join(ms)), f"CVE-2026-020{i}", modules=ms,
            inhouse=[CORE] if i == 1 else [])
    add("org.example:netty", "4.1", "manual_review", "transitive dependency org.example:netty resolves as a non-jar type "
        "or with a classifier (type/classifier variant (jar/linux)); a plain override entry would not govern it",
        "CVE-2026-0300", modules=["app-a"])
    finds.append(Finding(id="CVE-2026-0001", kind="library", identifier="org.example:log", version="2.25.4", action="NE"))
    prop = Proposal(id="p-1-1", round=1, ga="org.example:log", from_version="2.25.4", to_version="2.25.5",
                    kind="direct_version", decision="auto", evidence="repository:nexus.invalid", finding_ids=["CVE-2026-0001"],
                    edits=[TextEdit(pom="app-boot/pom.xml", line=41, old="2.25.4", new="2.25.5")],
                    note=("also resolved in app-domain, app-service, app-store through another origin; this edit may not "
                          "govern them — whatever stays at 2.25.4 is planned in the next round"))
    if not legacy:
        prop.modules, prop.also_modules = ["app-boot"], ["app-domain", "app-service", "app-store", "app-x", "app-y"]
    write_json_atomic(paths.plan(1), RoundPlan(round=1, source_tree_hash="t", policy_hash="p", proposals=[prop],
                                               residuals=res).to_dict())
    rest = [f for f in finds if f.identifier != "org.example:log"]
    write_json_atomic(paths.scan("baseline"), ScanResult(label="baseline", zip_sha256="a" * 64, complete=True,
                                                         findings=finds).to_dict())
    write_json_atomic(paths.scan("final-r1"), ScanResult(label="final-r1", zip_sha256="b" * 64, complete=True,
                                                         findings=rest).to_dict())
    state = RemediationState(
        id="rem-1", name="app.zip", status=INCOMPLETE, reason="manual_review", reasons=["manual_review", "residual_findings"],
        baseline_scan="scans/baseline.json", final_scan="scans/final-r1.json",
        baseline_build=BuildOutcome(ok=True, exit_code=0, log="baseline/build.log"),
        rounds=[RoundState(round=1, applied=["p-1-1"], build=BuildOutcome(ok=True), committed=True, scan_label="final-r1",
                           verify_problems=[("info: org.example:log in module app-domain still resolves at 2.25.4 — not "
                                            "governed by the edited version text (p-1-1); left for the next round")])],
        delivery=DeliveryInfo(path="delivery/result.zip", sha256="b" * 64, size=10, files=3, tree_hash="x", round=1,
                              scanned=True),
        residuals=res, message="final scan incomplete: page_missing" if legacy else "", counts=dict(counts or {}))
    return state, paths


def _section(md: str, title: str) -> str:
    return md.split(f"\n## {title}\n", 1)[1].split("\n## ", 1)[0]


def test_section_order_and_result_line(tmp_path):
    state, paths = _company(tmp_path)
    md = report.build_report_md(state, paths)
    heads = [ln for ln in md.splitlines() if ln.startswith("## ")]
    assert heads == ["## 한눈에 보기", "## 다음에 할 일", "## 고친 것", "## 사내 라이브러리에서 온 취약점", "## 사람이 확인할 것",
                     "## 조치 대상에서 뺀 항목", "## 진행 기록", "## 바꾸지 않은 것"]
    line = md.splitlines()[2]
    assert line == ("> ⚠️ **미완료(성공 아님)** (`INCOMPLETE`) — 취약점 8건 중 1건을 고쳤고, 3건은 사내 라이브러리 담당자에게, "
                    "4건은 사람 검토로 넘깁니다.")
    glance = _section(md, "한눈에 보기")
    assert "| 사내 라이브러리 경유(예외 처리 가능) | 라이브러리 3개 (취약점 3건) |" in glance
    assert "| 사람이 확인할 것 | 라이브러리 4개 (취약점 4건) |" in glance
    assert "| 결과 ZIP | ✅ 있음 — 빌드 성공, 재검사함 |" in glance and "| 최종 검사에 남은 것 | 7건 (🔴 심각 1건 · 🟠 높음 6건) |" in glance
    assert "| 위험도별 | 🔴 심각 1건 · 🟠 높음 6건 · 표시 없음 1건 |" in glance
    steps = _section(md, "다음에 할 일")
    assert "아키텍처 담당" in steps and "사람 검토 (`manual_review`)" in steps
    numbered = [ln for ln in steps.splitlines() if re.match(r"\d+\. ", ln)]
    assert len(numbered) == 2                                   # manual_review + residual_findings = ONE step
    assert "아키텍처 담당" in numbered[0] and "최종 검사 표" in numbered[1] and "사람이 확인할 것" in numbered[1]


def test_company_case_groups_the_repeated_detail_once(tmp_path):
    state, paths = _company(tmp_path)
    people = _section(report.build_report_md(state, paths), "사람이 확인할 것")
    assert people.count("### ") == 2
    assert "### 사람 검토 · 루트 pom 을 상속하지 않는 모듈 — 3개" in people
    assert people.count("이 모듈들은 프로젝트의 루트 pom 을 상속하지 않습니다") == 1
    assert "do not inherit" not in people and "override entry" not in people
    assert "| 라이브러리 | 버전 | 모듈 | 취약점 |" in people                     # origin lifted: the same on every row
    assert "- 모든 항목 공통 — 출처: 간접 의존성" in people
    assert "| `org.example:m2` | 1.0 | app-a, app-b, app-c 외 2개 |" in people        # long module list cut
    assert "`org.example:m1` · 일부 모듈은 사내 라이브러리 경유" in people
    assert "`org.example:m0` · 일부" not in people
    variant = people.split("분류자·다른 종류로 들어옴", 1)[1]
    assert "모든 항목 공통" not in variant                     # a one-row group lifts nothing
    assert "- 내용: 간접 의존성 org.example:netty 는 jar 가 아닌 종류이거나 분류자가 붙어 들어옵니다(jar/linux)." in variant
    assert "| 위험도 | 라이브러리 | 버전 | 출처 | 모듈 | 취약점 |" in variant
    assert "| 🟠 7.5 | `org.example:netty` | 4.1 | 간접 의존성 | app-a | 🟠 CVE-2026-0300 7.5 |" in variant


def test_inhouse_section_groups_by_artifact(tmp_path):
    state, paths = _company(tmp_path)
    md = report.build_report_md(state, paths)
    sec = _section(md, "사내 라이브러리에서 온 취약점")
    assert sec.lstrip().startswith("> 해당 아티팩트는 예외처리가 가능합니다. 담당자에게 연락 부탁드립니다.")
    assert "회사 부모 pom" in sec and "`inhouse_artifact`" in sec
    assert f"### `{CORE}` — 2개" in sec and f"### `{WEB}` — 1개" in sec and f"### `{COMP}` — 1개" in sec
    assert sec.count("`org.example:json`") == 2                       # one library under two artifacts
    assert "| `org.example:spring-web` | 6.0 | 부모 pom 이 관리 | app-store |" in sec
    assert f"{CORE}:4.0 → org.example:json:2.0" in sec and "최고" not in sec
    assert "| 🟠 7.5 | `org.example:spring-web` | 6.0 | 부모 pom 이 관리 | app-store | 🟠 CVE-2026-0103 7.5 |" in sec
    core = sec.split(f"### `{CORE}`", 1)[1].split("### ", 1)[0]
    assert core.index("`org.example:json`") < core.index("`org.example:yaml`")     # 9.8 before 7.5
    assert "org.example:json" not in _section(md, "사람이 확인할 것")
    custom = report.build_report_md(state, paths, inhouse_message="담당 팀 문의 바랍니다.")
    assert "> 담당 팀 문의 바랍니다." in custom


def test_no_untranslated_known_template_and_deterministic(tmp_path):
    from sbmig.remediations import i18n
    state, paths = _company(tmp_path)
    md = report.build_report_md(state, paths)
    assert md == report.build_report_md(state, paths)
    for key, phrase in i18n.KEY_PHRASES.items():
        assert phrase not in md, key
    assert str(tmp_path) not in md


def test_old_state_shape_renders(tmp_path):
    state, paths = _company(tmp_path, legacy=True)
    md = report.build_report_md(state, paths)
    sec = _section(md, "사내 라이브러리에서 온 취약점")
    assert f"### `{CORE}` — 2개" in sec and f"### `{WEB}` — 1개" in sec      # artifacts read from the old detail
    assert "| `org.example:yaml` | 1.3 | 간접 의존성 | - |" in sec
    assert "다스리는 모듈" not in md
    assert "최종 검사가 완료되지 않았습니다: page_missing" in md               # state.message translated


def test_report_json_lists_inhouse_without_korean(tmp_path):
    state, paths = _company(tmp_path)
    report.write_report(state, paths, inhouse_message="msg")
    data = json.loads(paths.report_json.read_text(encoding="utf-8"))
    inh = data["inhouse"]
    assert inh["message"] == "msg" and [a["ga"] for a in inh["artifacts"]] == sorted([CORE, WEB, COMP])
    core = next(a for a in inh["artifacts"] if a["ga"] == CORE)
    assert [lib["ga"] for lib in core["libraries"]] == ["org.example:json", "org.example:yaml"]
    assert set(core["libraries"][0]) == {"ga", "version", "modules", "via", "finding_ids"}
    assert data["state"]["residuals"][0]["detail"].startswith("brought in by")    # English kept in the contract
    default = report.build_report_json(state, paths)["inhouse"]["message"]
    assert default == "해당 아티팩트는 예외처리가 가능합니다. 담당자에게 연락 부탁드립니다."
    assert paths.report_md.read_text(encoding="utf-8").count("> msg") == 1


def test_counts_and_categories_have_korean_labels_and_long_tables_fold(tmp_path):
    from sbmig.remediations.models import Finding, ScanResult, write_json_atomic
    state, paths = _company(tmp_path, counts={"rounds": 1, "residual": 7, "residual_inhouse": 3, "baseline_findings": 8,
                                              "final_findings": 7, "fixed": 1, "brand_new_key": 2})
    base = ScanResult.from_dict(json.loads(paths.scan("baseline").read_text(encoding="utf-8")))
    base.counts = {"library": 8}
    write_json_atomic(paths.scan("baseline"), base.to_dict())
    md = report.build_report_md(state, paths)
    assert ("- 집계: 기준 검사 조치 대상 8, 고친 취약점 1, 최종 검사 조치 대상 7, 라운드 1, 남긴 항목 7, "
            "사내 라이브러리 경유 항목 3, brand_new_key 2") in md
    assert "- 범주별: 라이브러리 8건" in md and "library=8" not in md
    lines = md.splitlines()
    i = lines.index("<details><summary>남은 취약점 전체 목록 — 7건</summary>")
    assert lines[i + 1] == "" and lines[i + 2].startswith("| 위험도 | 취약점 | 종류 |") and "</details>" in lines[i + 3:i + 15]
    end = lines.index("</details>", i)
    assert lines[end - 1] == ""

    # the ignored-findings table folds only above 10 rows
    ignored = [Finding(id=f"CVE-2026-9{n:03d}", kind="library", identifier="org.example:patched", version="1.0",
                       action="PC") for n in range(11)]
    final = ScanResult.from_dict(json.loads(paths.scan("final-r1").read_text(encoding="utf-8")))
    final.findings += ignored
    write_json_atomic(paths.scan("final-r1"), final.to_dict())
    md = report.build_report_md(state, paths, ignore_actions=["PC"])
    assert "<details><summary>조치 대상에서 뺀 항목 전체 — 11건</summary>" in md
    final.findings = final.findings[:-10]
    write_json_atomic(paths.scan("final-r1"), final.to_dict())
    md = report.build_report_md(state, paths, ignore_actions=["PC"])
    assert "조치 대상에서 뺀 항목 전체" not in md and "| CVE-2026-9000 | 라이브러리 |" in md


def test_next_steps_order_approvals_first():
    from sbmig.remediations.models import RemediationState
    st = RemediationState(id="r", status="INCOMPLETE",
                          reasons=["residual_findings", "no_candidate_version", "pending_approval", "manual_review"],
                          residuals=[Residual(ga="g:a", version="1", origin="transitive", reason="inhouse_artifact")])
    steps = report._next_steps(st)
    assert steps[0] == report._REASON_KO["pending_approval"][1]
    assert steps[1] == report._REASON_KO["inhouse_artifact"][1]
    assert steps[2] == report._REVIEW_BOTH
    assert steps[3] == report._REASON_KO["no_candidate_version"][1] and len(steps) == 4
    st.reasons = ["manual_review"]
    assert report._next_steps(st)[1] == report._REASON_KO["manual_review"][1]



# ---------------------------------------------------------------------------- severity marks (2026-11-12)
def test_severity_boundaries_and_odd_thresholds():
    from sbmig.remediations import i18n
    th = i18n.severity_thresholds(None)
    assert th == (9.0, 7.0, 4.0)
    assert i18n.severity_of(9.0, th) == ("red", "🔴", "심각")
    assert i18n.severity_of(8.9, th) == ("orange", "🟠", "높음")
    assert i18n.severity_of(7.0, th)[0] == "orange" and i18n.severity_of(6.9, th)[0] == "yellow"
    assert i18n.severity_of(4.0, th) == ("yellow", "🟡", "보통")
    assert i18n.severity_of(3.9, th) == ("none", "", "") and i18n.severity_of(None, th) == ("none", "", "")
    assert i18n.severity_thresholds((4, 9, 7)) == (9.0, 7.0, 4.0)            # not descending: sorted, no error
    assert i18n.severity_thresholds((-1, 5, 2)) == (5.0, 2.0, -1.0)
    assert i18n.severity_thresholds(("x", 1, 2)) == (9.0, 7.0, 4.0)          # unreadable: defaults
    assert i18n.severity_thresholds((1, 2)) == (9.0, 7.0, 4.0)


def _scored(tmp_path, scores):
    """One manual_review residual per (ga, [(cve, score)…]) with a complete baseline scan and no final scan."""
    from sbmig.remediations.models import Finding, RemediationState, RunPaths, ScanResult, write_json_atomic
    paths = RunPaths(tmp_path / "run")
    finds, res = [], []
    for ga, items in scores:
        for cve, sc in items:
            finds.append(Finding(id=cve, kind="library", identifier=ga, version="1.0", action="NE", score=sc))
        res.append(Residual(ga=ga, version="1.0", origin="transitive", reason="manual_review", detail="inventory incomplete",
                            finding_ids=[c for c, _ in items]))
    write_json_atomic(paths.scan("baseline"), ScanResult(label="baseline", zip_sha256="a", complete=True,
                                                         findings=finds).to_dict())
    state = RemediationState(id="r", name="a.zip", status=INCOMPLETE, reason="manual_review",
                             baseline_scan="scans/baseline.json", residuals=res)
    return state, paths


def test_rows_sorted_by_severity_with_per_cve_marks_and_cut(tmp_path):
    state, paths = _scored(tmp_path, [
        ("org.example:low", [("CVE-2026-0010", 3.0)]),
        ("org.example:none", [("CVE-2026-0020", None)]),
        ("org.example:many", [("CVE-2026-0001", 5.0), ("CVE-2026-0002", 9.8), ("CVE-2026-0003", None),
                              ("CVE-2026-0004", 8.1), ("CVE-2026-0005", 2.0)]),
        ("org.example:mid", [("CVE-2026-0030", 7.0)]),
    ])
    md = report.build_report_md(state, paths)
    people = _section(md, "사람이 확인할 것")
    rows = [ln for ln in people.splitlines() if ln.startswith("| ") and "`org.example:" in ln]
    assert [r.split("`")[1] for r in rows] == ["org.example:many", "org.example:mid", "org.example:low", "org.example:none"]
    assert rows[0].startswith("| 🔴 9.8 |") and rows[2].startswith("| 3.0 |") and rows[3].startswith("| - |")
    assert "| 🔴 CVE-2026-0002 9.8, 🟠 CVE-2026-0004 8.1, 🟡 CVE-2026-0001 5.0 외 2건 |" in rows[0]
    assert "| CVE-2026-0010 3.0 |" in rows[2] and "| CVE-2026-0020 |" in rows[3]
    glance = _section(md, "한눈에 보기")
    assert "| 위험도별 | 🔴 심각 1건 · 🟠 높음 2건 · 🟡 보통 1건 · 표시 없음 4건 |" in glance
    assert "위험도 표시: 🔴 9.0 이상 · 🟠 7.0 이상 · 🟡 4.0 이상 (IVAS 점수 기준" in glance
    assert md == report.build_report_md(state, paths)

    custom = report.build_report_md(state, paths, severity=(5.0, 3.0, 1.0))
    assert "위험도 표시: 🔴 5.0 이상 · 🟠 3.0 이상 · 🟡 1.0 이상" in custom
    assert "| 🟠 3.0 | `org.example:low`" in custom and "| 위험도별 | 🔴 심각 4건 · 🟠 높음 1건 · 🟡 보통 1건 · 표시 없음 2건 |" in custom


def test_no_scores_anywhere_gives_no_marks(tmp_path):
    state, paths = _scored(tmp_path, [("org.example:a", [("CVE-2026-0001", None)]), ("org.example:b", [])])
    md = report.build_report_md(state, paths)
    body = md.split("점수가 없으면 표시 없음)", 1)[1]                    # everything after the legend line
    assert not any(m in body for m in ("🔴", "🟠", "🟡"))
    assert "| 위험도별 | 표시 없음 1건 |" in md and "| - | `org.example:b` |" in md
    data = report.build_report_json(state, paths)
    assert data["severity"]["baseline"] == {"red": 0, "orange": 0, "yellow": 0, "none": 1}


def test_report_json_severity_and_marked_tables(tmp_path):
    state, paths = _company(tmp_path)
    report.write_report(state, paths, severity=(9.5, 8.0, 7.5))
    data = json.loads(paths.report_json.read_text(encoding="utf-8"))
    sev = data["severity"]
    assert sev["thresholds"] == {"red": 9.5, "orange": 8.0, "yellow": 7.5}
    assert sev["baseline"] == {"red": 1, "orange": 0, "yellow": 6, "none": 1}
    assert sev["final"] == {"red": 1, "orange": 0, "yellow": 6, "none": 0}
    assert "🔴" not in paths.report_json.read_text(encoding="utf-8")
    md = paths.report_md.read_text(encoding="utf-8")
    folded = md.split("<details><summary>남은 취약점 전체 목록", 1)[1].split("</details>", 1)[0]
    rows = [ln for ln in folded.splitlines() if ln.startswith("| ") and "CVE" in ln]
    assert rows[0].startswith("| 🔴 9.8 | CVE-2026-0101 |") and rows[1].startswith("| 🟡 7.5 |")
    fixed = _section(md, "고친 것")
    assert "| 위험도 | 라이브러리 |" in fixed and "| - | `org.example:log` |" in fixed   # no score in any scan


def test_graph_passes_the_configured_thresholds(tmp_path):
    out, paths = _run(tmp_path, cfg=make_cfg(severity_red=9.5, severity_orange=8.5, severity_yellow=5.5), **_fixing())
    md = paths.report_md.read_text(encoding="utf-8")
    assert "위험도 표시: 🔴 9.5 이상 · 🟠 8.5 이상 · 🟡 5.5 이상" in md
    data = json.loads(paths.report_json.read_text(encoding="utf-8"))
    assert data["severity"]["thresholds"] == {"red": 9.5, "orange": 8.5, "yellow": 5.5}
