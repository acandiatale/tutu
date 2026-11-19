"""report.md for override proposals in a module's own pom (v2.1z-an): applied changes grouped per library with one line
per pom, the caution column and step of risk labels (sbmig/remediations/report.py). The approval sections (승인 대기,
선택하지 않은 변경) went with the approval step."""
from __future__ import annotations

from sbmig.remediations import report
from sbmig.remediations.models import (
    D_AUTO,
    BuildOutcome,
    Finding,
    ModuleSource,
    PomLocation,
    Proposal,
    RoundPlan,
    RoundState,
    ScanResult,
    TextEdit,
    write_json_atomic,
)

from .test_report import BANNED_WORDS, _company, _detached, _section

WEB_CORE = "org.example.web:web-core:2.1"


def _override(pid, pom, modules, *, ga="org.example:json", frm="2.0", to="2.1", classifier="", decision=D_AUTO,
              sources=None, cve="CVE-2026-0101"):
    g, a = ga.split(":")
    entry = {"groupId": g, "artifactId": a, "version": to, **({"classifier": classifier} if classifier else {})}
    return Proposal(id=pid, round=1, ga=ga, from_version=frm, to_version=to, kind="managed_override", decision=decision,
                    risks=["managed_override"], evidence="repository:nexus.invalid", finding_ids=[cve],
                    edits=[TextEdit(pom=pom, line=30, old="", new="<dependency/>", op="insert", anchor="</dependencies>",
                                    override=entry)],
                    modules=list(modules),
                    sources=sources if sources is not None else [
                        ModuleSource(module=m, how="transitive", via=[WEB_CORE]) for m in modules])


def _proposals(decision=D_AUTO):
    return [
        _override("p-1-1", "app-back/pom.xml", ["app-back"], decision=decision),
        _override("p-1-2", "app-front/pom.xml", ["app-front"], decision=decision,
                  sources=[ModuleSource(module="app-front", how="parent_managed", manager="com.example:comp-jar:4.0.0",
                                        declared=PomLocation(pom="app-front/pom.xml", line=22, text=""))]),
        _override("p-1-3", "app-back/pom.xml", ["app-back"], classifier="jakarta", decision=decision,
                  sources=[ModuleSource(module="app-back", how="transitive", via=[WEB_CORE], variant="jar/, jar/jakarta")]),
        _override("p-1-4", "pom.xml", ["app-boot"], decision=decision),
        _override("p-1-5", "app-back/pom.xml", ["app-back"], ga="org.example:xml", frm="1.4", to="1.5",
                  decision=decision, cve="CVE-2026-0950"),
    ]


def _state(tmp_path, decision=D_AUTO):
    state, paths = _company(tmp_path)
    base = ScanResult.from_dict(__import__("json").loads(paths.scan("baseline").read_text(encoding="utf-8")))
    base.findings.append(Finding(id="CVE-2026-0950", kind="library", identifier="org.example:xml", version="1.4",
                                 action="NE", score=8.6))
    write_json_atomic(paths.scan("baseline"), base.to_dict())
    props = _proposals(decision)
    state.rounds = [RoundState(round=1, applied=[p.id for p in props], build=BuildOutcome(ok=True), committed=True,
                               scan_label="final-r1")]
    write_json_atomic(paths.plan(1), RoundPlan(round=1, source_tree_hash="t", policy_hash="p", proposals=props,
                                               residuals=state.residuals).to_dict())
    return state, paths


def test_applied_module_overrides_are_grouped_per_library_with_one_line_per_pom(tmp_path):
    state, paths = _state(tmp_path)
    md = report.build_report_md(state, paths)
    sec = _section(md, "조치 완료")
    assert "## 승인 대기" not in md
    rows = [ln for ln in sec.splitlines() if ln.startswith("| ") and "`org.example:" in ln]
    assert len(rows) == 2                                      # one row per library, not per pom
    json_row = next(r for r in rows if "`org.example:json`" in r)
    assert "`app-back/pom.xml`, `app-front/pom.xml`, 최상위 pom.xml" in json_row
    assert "- **`json` 2.0 → 2.1** (제안 4건)" in sec and "  - 수정한 pom:" in sec
    assert (f"    - `app-back/pom.xml`에 버전 지정 추가 (제안 `p-1-1`) · 항목 `org.example:json:2.1` · "
            f"`app-back` 모듈: `{WEB_CORE}` 경로로 가져옴") in sec
    assert ("    - `app-back/pom.xml`에 버전 지정 추가 (제안 `p-1-3`) · 항목 `org.example:json:2.1` classifier `jakarta` · "
            f"`app-back` 모듈: `{WEB_CORE}` 경로로 가져옴, 사용 형태 2가지(일반 jar, classifier `jakarta`)") in sec
    assert ("    - `app-front/pom.xml`에 버전 지정 추가 (제안 `p-1-2`) · 항목 `org.example:json:2.1` · `app-front` 모듈: "
            "`app-front/pom.xml` 22행에 선언, `com.example:comp-jar:4.0.0`에서 버전 지정") in sec
    assert "    - 최상위 pom.xml에 버전 지정 추가 (제안 `p-1-4`)" in sec
    # one proposal for a library: no sub-list
    assert ("- **`xml` 1.4 → 1.5** (제안 `p-1-5`)\n  - 버전 확인: 저장소 nexus.invalid\n"
            "  - 수정 방식: dependencyManagement에 버전 지정 추가\n  - `app-back/pom.xml`에 버전 지정 추가") in sec


def test_a_root_override_of_an_older_state_keeps_its_form(tmp_path):
    state, paths = _state(tmp_path)
    plan = RoundPlan.from_dict(__import__("json").loads(paths.plan(1).read_text(encoding="utf-8")))
    for p in plan.proposals:
        p.sources = []
    plan.proposals = [p for p in plan.proposals if p.edits[0].pom == "pom.xml"]
    write_json_atomic(paths.plan(1), plan.to_dict())
    state.rounds[0].applied = ["p-1-4"]
    sec = _section(report.build_report_md(state, paths), "조치 완료")
    assert "| `pom.xml` 30행 앞에 추가 |" in sec and "수정한 pom" not in sec and "최상위 pom.xml에" not in sec


def test_an_ineffective_entry_is_not_applied_and_sits_under_manual_review(tmp_path):
    """An inserted entry that did not change the resolved version is removed again (`decision: "ineffective"`, not in
    `rounds[r].applied`). Its residual sits under 직접 확인 with its path."""
    from sbmig.remediations.models import Residual
    state, paths = _company(tmp_path)
    props = _proposals()
    props[0].decision = "ineffective"                     # p-1-1, app-back/pom.xml
    write_json_atomic(paths.plan(1), RoundPlan(round=1, source_tree_hash="t", policy_hash="p", proposals=props,
                                               residuals=state.residuals).to_dict())
    state.rounds = [RoundState(round=1, applied=[p.id for p in props[1:]], build=BuildOutcome(ok=True), committed=True,
                               scan_label="final-r1")]
    state.residuals.append(Residual(
        ga="org.example:json", version="2.0", origin="transitive", reason="manual_review",
        detail=("the override entry in app-back/pom.xml did not change the resolved version of org.example:json (2.0 "
                "stays): a dependency declared with a version, here or in a parent, wins over <dependencyManagement>; "
                "the entry was removed again"), finding_ids=["CVE-2026-0101"], modules=["app-back"],
        sources=[ModuleSource(module="app-back", how="transitive", via=[WEB_CORE],
                              declared=PomLocation(pom="app-back/pom.xml", line=44, text="2.0"))]))
    md = report.build_report_md(state, paths)
    assert "선택하지 않아 적용하지 않은 변경" not in md
    done = _section(md, "조치 완료")
    assert "(제안 `p-1-1`)" not in done and "(제안 `p-1-2`)" in done
    people = _section(md, "직접 확인이 필요한 취약점")
    group = people.split("### 버전 지정을 넣어도 바뀌지 않는 버전", 1)[1].split("### ", 1)[0]
    assert "다시 뺐습니다" in group and "선언 위치 `app-back/pom.xml` 44행" in group
    record = md.split("## 처리 기록", 1)[1]
    assert "| p-1-1 |" in record and "효과가 없어 다시 뺌" in record
    assert "- 적용한 제안: p-1-2, p-1-3, p-1-4, p-1-5" in record
    assert _detached(md) == []


# ---------------------------------------------------------------------------- applied without approval (v2.1z-ar)
DONE_HEAD_TODAY = "| 위험도 | 라이브러리 | 버전 변경 | 수정한 곳 | 해결된 취약점 |"
DONE_HEAD_RISKY = "| 위험도 | 라이브러리 | 버전 변경 | 수정한 곳 | 주의 | 해결된 취약점 |"


def _auto_props(decision="auto"):
    """One same-major direct change, one major direct change, one root-pom override entry, two module-pom override
    entries (same library, so one grouped row). Built by hand: independent of the planner's approval default."""
    same = Proposal(id="p-1-1", round=1, ga="org.example:log", from_version="2.25.4", to_version="2.25.5",
                    kind="direct_version", decision=decision, evidence="repository:nexus.invalid",
                    finding_ids=["CVE-2026-0001"], edits=[TextEdit(pom="app-boot/pom.xml", line=41, old="2.25.4",
                                                                   new="2.25.5")], modules=["app-boot"])
    major = Proposal(id="p-1-2", round=1, ga="org.example:http", from_version="1.9", to_version="2.0",
                     kind="direct_version", decision=decision, risks=["major"], evidence="repository:nexus.invalid",
                     finding_ids=["CVE-2026-0002"], edits=[TextEdit(pom="app-back/pom.xml", line=52, old="1.9",
                                                                    new="2.0")], modules=["app-back"])
    root = _override("p-1-3", "pom.xml", ["app-boot"], ga="org.example:xml", frm="1.4", to="1.5", decision=decision,
                     cve="CVE-2026-0950")
    mods = [_override("p-1-4", "app-back/pom.xml", ["app-back"], decision=decision),
            _override("p-1-5", "app-front/pom.xml", ["app-front"], decision=decision)]
    mods[1].risks = ["major", "managed_override"]          # the grouped row shows the union
    return [same, major, root, *mods]


def _auto_state(tmp_path, decision="auto", *, props=None):
    from sbmig.remediations.models import SUCCEEDED, BuildOutcome
    state, paths = _company(tmp_path)
    props = props if props is not None else _auto_props(decision)
    finds = [Finding(id=i, kind="library", identifier=p.ga, version=p.from_version, action="NE", score=s)
             for p, s in zip(props, (7.5, 9.1, 8.6, 6.2, 6.2)) for i in p.finding_ids]
    finds = list({(f.id, f.identifier): f for f in finds}.values())
    write_json_atomic(paths.scan("baseline"), ScanResult(label="baseline", zip_sha256="a" * 64, complete=True,
                                                         findings=finds).to_dict())
    write_json_atomic(paths.scan("final-r1"), ScanResult(label="final-r1", zip_sha256="b" * 64, complete=True,
                                                         findings=[]).to_dict())
    write_json_atomic(paths.plan(1), RoundPlan(round=1, source_tree_hash="t", policy_hash="p", proposals=props,
                                               residuals=[]).to_dict())
    state.status, state.reason, state.reasons, state.residuals, state.message = SUCCEEDED, "", [], [], ""
    state.rounds = [RoundState(round=1, applied=[p.id for p in props], build=BuildOutcome(ok=True), committed=True,
                               scan_label="final-r1")]
    return state, paths


def _table(sec):
    return [ln for ln in sec.splitlines() if ln.startswith("|")]


def test_done_table_gets_a_caution_column_when_an_applied_change_has_a_flag(tmp_path):
    state, paths = _auto_state(tmp_path)
    md = report.build_report_md(state, paths)
    rows = _table(_section(md, "조치 완료"))
    assert rows[0] == DONE_HEAD_RISKY and rows[1] == "|" + "---|" * 6
    by_ga = {r.split(" | ")[1]: r for r in rows[2:]}
    assert len(by_ga) == 4 and all(r.count(" | ") == 5 for r in rows[2:])
    assert " | 메이저 버전 변경 | " in by_ga["`org.example:http`"]
    assert " | dependencyManagement 항목 추가 | " in by_ga["`org.example:xml`"]
    assert " | 2.25.4 → 2.25.5 | `app-boot/pom.xml` 41행 | - | " in by_ga["`org.example:log`"]
    # two module-pom entries of one library: one row, the union of their flags
    assert " | 메이저 버전 변경, dependencyManagement 항목 추가 | " in by_ga["`org.example:json`"]
    # an applied change is past: the list under a grouped library reads "수정한 pom", not "수정할 pom"
    sec = _section(md, "조치 완료")
    assert "  - 수정한 pom:" in sec and "수정할 pom" not in sec
    assert _detached(md) == []
    for word in BANNED_WORDS:
        assert word not in md.split("## 처리 기록", 1)[0], word


def test_done_table_without_any_flag_is_unchanged(tmp_path):
    state, paths = _company(tmp_path)                 # one committed direct change, no flag
    sec = _section(report.build_report_md(state, paths), "조치 완료")
    rows = _table(sec)
    assert rows == [DONE_HEAD_TODAY, "|---|---|---|---|---|",
                    "| - | `org.example:log` | 2.25.4 → 2.25.5 | `app-boot/pom.xml` 41행 | CVE-2026-0001 |"]
    assert "주의" not in sec
    # the same proposals without flags: the old five columns, no step
    props = _auto_props()
    for p in props:
        p.risks = []
    state, paths = _auto_state(tmp_path / "plain", props=props)
    md = report.build_report_md(state, paths)
    rows = _table(_section(md, "조치 완료"))
    assert rows[0] == DONE_HEAD_TODAY and all(r.count(" | ") == 4 for r in rows[2:])
    assert "자동으로 적용한 변경 확인하기" not in md


def test_auto_applied_risky_changes_get_a_step_after_the_result_zip(tmp_path):
    state, paths = _auto_state(tmp_path)
    md = report.build_report_md(state, paths)
    steps = [ln for ln in _section(md, "해야 할 일").splitlines() if ln[:1].isdigit()]
    assert steps[0].startswith("1. **결과 ZIP 받기**")
    assert steps[1] == ("2. **자동으로 적용한 변경 확인하기 (4건)** — 적용한 변경 가운데 주의가 필요한 "
                        "항목입니다(메이저 버전 변경 2건, dependencyManagement 항목 추가 3건). 「조치 완료」 표의 「주의」 열에서 "
                        "해당 라이브러리를 확인하고 애플리케이션 동작을 점검해 주세요.")
    assert len(steps) == 2
    assert "## 승인 대기" not in md and "승인을 기다리" not in md


def test_auto_step_leaves_out_a_class_without_proposals():
    props = _auto_props()
    step = report._auto_risky_step([props[1]])
    assert "(1건)" in step and "(메이저 버전 변경 1건)" in step and "dependencyManagement" not in step


def test_auto_step_comes_after_a_failure_step_and_before_manual_review(tmp_path):
    from sbmig.remediations.models import FAILED, BuildOutcome, DeliveryInfo, Residual
    state, paths = _auto_state(tmp_path)
    write_json_atomic(paths.plan(2), RoundPlan(round=2, source_tree_hash="t2", policy_hash="p", proposals=[
        Proposal(id="p-2-1", round=2, ga="org.example:mail", from_version="1.0", to_version="1.1",
                 kind="direct_version", decision="auto", risks=["major"], finding_ids=["CVE-2026-0700"],
                 edits=[TextEdit(pom="pom.xml", line=9, old="1.0", new="1.1")])]).to_dict())
    state.rounds.append(RoundState(round=2, applied=["p-2-1"], build=BuildOutcome(ok=False), committed=False))
    state.status, state.reason, state.reasons = FAILED, "candidate_build_failed", ["candidate_build_failed",
                                                                                     "manual_review"]
    state.delivery = DeliveryInfo(path="delivery/result.zip", sha256="b" * 64, size=10, files=3, tree_hash="x",
                                  round=1, scanned=True)
    state.residuals = [Residual(ga="org.example:vendored", version="0.9", origin="vendored", reason="manual_review",
                                detail="WEB-INF/lib|jar", finding_ids=["CVE-2026-0800"])]
    steps = report._next_steps(state, report._Facts(state, paths, frozenset()))
    assert steps[0] == report._reason_text(state, "candidate_build_failed")[1]
    assert steps[1].startswith("**자동으로 적용한 변경 확인하기 (4건)**")     # round 2 was not committed
    assert steps[2].startswith(report._REASON_KO["manual_review"][1].split("** — ")[0])
    assert len(steps) == 3
    assert not any("자동으로 적용한" in s for s in report._next_steps(state))   # no facts, no step


def test_approved_risky_changes_of_an_older_plan_get_no_step_but_keep_the_column(tmp_path):
    """A plan written while the approval step existed may hold `approved` proposals: a person reviewed them, so they
    are applied (`RoundPlan.to_apply`) and shown, but not counted in the step about automatically applied changes."""
    state, paths = _auto_state(tmp_path, "approved")
    md = report.build_report_md(state, paths)
    assert "자동으로 적용한 변경 확인하기" not in md
    assert _table(_section(md, "조치 완료"))[0] == DONE_HEAD_RISKY
    # only the auto ones are counted
    props = _auto_props()
    props[1].decision = "approved"
    state, paths = _auto_state(tmp_path / "mixed", props=props)
    md = report.build_report_md(state, paths)
    assert ("**자동으로 적용한 변경 확인하기 (3건)** — 적용한 변경 가운데 주의가 필요한 항목입니다"
            "(메이저 버전 변경 1건, dependencyManagement 항목 추가 3건).") in md


def test_report_json_is_unchanged_by_the_caution_column(tmp_path):
    state, paths = _auto_state(tmp_path)
    data = report.build_report_json(state, paths)
    props = data["rounds"][0]["plan"]["proposals"]
    assert [p["decision"] for p in props] == ["auto"] * 5
    assert "주의" not in __import__("json").dumps(data, ensure_ascii=False)
