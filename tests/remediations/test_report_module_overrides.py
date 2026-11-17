"""report.md for override proposals in a module's own pom, chosen by the owners (v2.1z-an): 승인 대기 grouped per
library with one line per pom, the not-selected changes as the owners' choice (sbmig/remediations/report.py)."""
from __future__ import annotations

from sbmig.remediations import i18n, report
from sbmig.remediations.models import (
    AWAITING_APPROVAL,
    D_PENDING,
    D_REJECTED,
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


def _override(pid, pom, modules, *, ga="org.example:json", frm="2.0", to="2.1", classifier="", decision=D_PENDING,
              sources=None, cve="CVE-2026-0101"):
    g, a = ga.split(":")
    entry = {"groupId": g, "artifactId": a, "version": to, **({"classifier": classifier} if classifier else {})}
    return Proposal(id=pid, round=1, ga=ga, from_version=frm, to_version=to, kind="managed_override", decision=decision,
                    risks=["managed_override"], evidence="repository:nexus.invalid", finding_ids=[cve],
                    edits=[TextEdit(pom=pom, line=30, old="", new="<dependency/>", op="insert", anchor="</dependencies>",
                                    override=entry)],
                    modules=list(modules), requires_approval=True,
                    sources=sources if sources is not None else [
                        ModuleSource(module=m, how="transitive", via=[WEB_CORE]) for m in modules])


def _proposals(decision=D_PENDING):
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


def _state(tmp_path, decision=D_PENDING):
    state, paths = _company(tmp_path)
    base = ScanResult.from_dict(__import__("json").loads(paths.scan("baseline").read_text(encoding="utf-8")))
    base.findings.append(Finding(id="CVE-2026-0950", kind="library", identifier="org.example:xml", version="1.4",
                                 action="NE", score=8.6))
    write_json_atomic(paths.scan("baseline"), base.to_dict())
    props = _proposals(decision)
    if decision == D_PENDING:
        state.status, state.reason, state.reasons = AWAITING_APPROVAL, "pending_approval", ["pending_approval"]
        state.final_scan, state.delivery = "", None
        state.rounds = [RoundState(round=1)]
    else:
        state.reasons = list(state.reasons) + ["rejected_proposals"]
        state.rounds = [RoundState(round=1, applied=[], committed=False)]
    write_json_atomic(paths.plan(1), RoundPlan(round=1, source_tree_hash="t", policy_hash="p", proposals=props,
                                               residuals=state.residuals).to_dict())
    return state, paths


def test_pending_module_overrides_are_grouped_per_library_with_one_line_per_pom(tmp_path):
    state, paths = _state(tmp_path)
    md = report.build_report_md(state, paths)
    sec = _section(md, "승인 대기")
    assert md.count("## 승인 대기 — 제안 5건") == 1
    rows = [ln for ln in sec.splitlines() if ln.startswith("| ") and "`org.example:" in ln]
    assert len(rows) == 2                                      # one row per library, not per pom
    json_row = next(r for r in rows if "`org.example:json`" in r)
    assert "`app-back/pom.xml`, `app-front/pom.xml`, 최상위 pom.xml" in json_row
    assert "- **`json` 2.0 → 2.1** (제안 4건)" in sec and "  - 수정할 pom:" in sec
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


def test_waiting_tells_the_owner_to_choose_on_the_page_first(tmp_path):
    state, paths = _state(tmp_path)
    md = report.build_report_md(state, paths)
    lines = md.splitlines()
    assert "적용 버튼(「선택한 N건 적용」)을 누르면 이어서 진행합니다." in lines[3] and "검토한 뒤에 골라도 됩니다." in lines[3]
    steps = _section(md, "해야 할 일")
    first = next(ln for ln in steps.splitlines() if ln.startswith("1. "))
    assert first.startswith("1. **적용할 변경 고르기") and "적용 버튼(「선택한 N건 적용」)을 눌러 주세요." in first
    assert "고른 변경만 적용하고" in _section(md, "승인 대기")


def test_not_selected_changes_read_as_the_owners_choice_with_the_facts(tmp_path):
    state, paths = _state(tmp_path, D_REJECTED)
    md = report.build_report_md(state, paths)
    heads = [ln for ln in md.splitlines() if ln.startswith("## ")]
    assert "## 선택하지 않아 적용하지 않은 변경 — 제안 5건" in heads and not any(h.startswith("## 승인 대기") for h in heads)
    sec = _section(md, "선택하지 않아 적용하지 않은 변경")
    assert sec.lstrip().startswith("조치 화면에서 선택하지 않은 변경입니다. 이번 실행에서는 다시 제안하지 않습니다.")
    assert "아래 내용을 해당 pom.xml에 직접 추가할 수 있습니다." in sec
    assert "| 위험도 | 라이브러리 | 버전 변경 | 수정할 곳 | 해당 취약점 |" in sec
    assert "`app-back/pom.xml`에 버전 지정 추가 (제안 `p-1-1`) · 항목 `org.example:json:2.1`" in sec
    assert "실패" not in sec
    assert "| 선택하지 않은 변경 | 2건 | 2개 |" in _section(md, "요약")
    assert "**선택하지 않은 변경 검토하기 (2건)**" in _section(md, "해야 할 일")
    assert "선택하지 않음" in md.split("## 처리 기록", 1)[1]         # the decision column of the record
    assert _detached(md) == [], _detached(md)
    for word in BANNED_WORDS:
        assert word not in md.split("## 처리 기록", 1)[0], word


def test_rejected_earlier_says_this_run_only():
    out = i18n.ko_text("org.example:json → 2.1 was rejected in an earlier decision; not proposed again")
    assert out == "org.example:json의 2.1 버전 변경은 담당자가 선택하지 않은 것이라 이번 실행에서는 다시 제안하지 않습니다."
    assert "조치를 다시 실행하면 다시 제안합니다." in i18n.GROUPS["rejected_earlier"].text
    assert i18n.label(i18n.DECISION_KO, "rejected") == "선택하지 않음"


def test_a_root_override_of_an_older_state_keeps_its_form(tmp_path):
    state, paths = _state(tmp_path)
    plan = RoundPlan.from_dict(__import__("json").loads(paths.plan(1).read_text(encoding="utf-8")))
    for p in plan.proposals:
        p.sources = []
    plan.proposals = [p for p in plan.proposals if p.edits[0].pom == "pom.xml"]
    write_json_atomic(paths.plan(1), plan.to_dict())
    sec = _section(report.build_report_md(state, paths), "승인 대기")
    assert "| `pom.xml` 30행 앞에 추가 |" in sec and "수정할 pom" not in sec and "최상위 pom.xml에" not in sec


def test_an_ineffective_entry_is_neither_applied_nor_not_selected(tmp_path):
    """An inserted entry that did not change the resolved version is removed again (`decision: "ineffective"`, not in
    `rounds[r].applied`); the owners did select it, so it is not "선택하지 않은 변경" either. Its residual sits under
    직접 확인 with its path."""
    from sbmig.remediations.models import BuildOutcome, Residual
    state, paths = _company(tmp_path)
    props = _proposals("approved")
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
