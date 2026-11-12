"""취약점 조치 보고서 — ``report.json`` (기계용: 상태 전부 + 검사 요약 + 라운드별 계획 요약) 과 ``report.md`` (운영자용, 한국어).
흐름상 위치: 그래프의 마지막 노드 ``report`` 가 끝 상태(SUCCEEDED / INCOMPLETE / FAILED)에서만 ``write_report`` 를 부른다
(대기 상태에서는 쓰지 않는다). API 목록은 ``summary`` 의 작은 dict 를 쓴다.
지켜야 할 것: 비밀 값·토큰·서버 절대 경로를 쓰지 않는다(실행 폴더 기준 상대 경로만); 같은 입력이면 같은 글자(시각을 새로
찍지 않는다, 정렬 고정); 맨 앞 줄에 상태·이유·다음에 할 일. 결과 ZIP 이 빌드가 실패한 수정본이면(``delivery.build_ok`` 거짓,
``remediation.failed_candidate: include``) 제목 바로 아래 첫 줄들에 그 사실·라운드·제안·빌드 로그·재검사 없음·원본 보존을 쓴다.
``remediation.ignore_actions`` 코드(기본은 없음; 예 PC 패치 완료 · IG 조치 예외)의 항목은 "조치 대상에서 뺀 항목" 표에 코드의 뜻(``ivas.ACTION_MEANINGS``)과
함께 따로 쓰고, 발견·잔여 개수에는 넣지 않는다.

Remediation report: `report.json` (the state, the scan summaries, per-round plan summaries) and `report.md` (Korean, for
operators: status, reasons and next steps first). Deterministic for the same input; no secret, no absolute server path.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from sbmig.core import scrub as _scrub

from .models import (
    AWAITING_APPROVAL,
    AWAITING_CONFIRMATION,
    D_APPROVED,
    D_AUTO,
    D_PENDING,
    D_REJECTED,
    FAILED,
    INCOMPLETE,
    INTERRUPTED,
    QUEUED,
    RUNNING,
    SCHEMA,
    STOPPED,
    SUCCEEDED,
    RemediationState,
    RoundPlan,
    RunPaths,
    ScanResult,
    read_json,
    write_json_atomic,
)

log = logging.getLogger("sbmig.remediations.report")

REPORT_SCHEMA = "sbmig.remediation.report/1"

_STATUS_KO = {
    SUCCEEDED: "성공", INCOMPLETE: "미완료(성공 아님)", FAILED: "실패", AWAITING_APPROVAL: "승인 대기",
    AWAITING_CONFIRMATION: "업로드 확인 대기", STOPPED: "중지됨", INTERRUPTED: "서버 재시작으로 중단됨",
    QUEUED: "대기열", RUNNING: "실행 중",
}
# 이유 코드 → (뜻, 다음에 할 일)
_REASON_KO: dict[str, tuple[str, str]] = {
    "baseline_build_failed": ("올린 프로젝트가 그대로는 빌드되지 않았습니다(기준 빌드 실패). 검사·계획은 하지 않았습니다.",
                              "baseline/build.log 를 보고 프로젝트 담당 팀이 빌드를 고친 뒤 다시 올리세요."),
    "candidate_build_failed": ("버전을 바꾼 후보가 빌드되지 않아 버렸습니다(정책 discard). 결과 ZIP 은 없고 원본은 그대로입니다.",
                               "rounds/<n>/build.log 와 candidate.patch 를 보고 버전을 사람이 정하세요."),
    "scan_incomplete": ("IVAS 검사가 끝까지 완료되지 않았습니다. 미완료 검사는 0건이 아닙니다.",
                        "IVAS 상태를 확인한 뒤 다시 실행하세요."),
    "upload_uncertain": ("IVAS 업로드를 보냈지만 결과를 모릅니다. 자동으로 다시 올리지 않습니다.",
                         "IVAS 에서 업로드 여부를 확인한 뒤 확인 결과와 함께 재개하세요."),
    "zip_hash_mismatch": ("검사한 ZIP 의 sha256 이 전달(또는 원본) ZIP 과 다릅니다.",
                          "전달 ZIP 을 쓰지 말고 운영자에게 알리세요."),
    "text_only_violation": ("후보에 버전 글자 밖의 변경이 있어 버렸습니다.", "rounds/<n>/candidate.patch 를 확인하세요(도구 오류)."),
    "resolution_mismatch": ("후보의 Maven 해석·패키징 결과가 계획과 다릅니다. 후보를 버렸습니다.",
                            "rounds/<n>/resolved.json 과 검증 메모를 보고 사람이 판단하세요."),
    "execution_boundary_refused": ("프로젝트가 서버 Maven 실행 경계를 우회할 수 있는 요소를 가졌습니다(작업 거부).",
                                   "아래 '실행 경계 점검' 항목을 프로젝트에서 없앤 뒤 다시 올리세요."),
    "not_ready": ("서버 준비 조건(IVAS 설정, settings.xml, JDK 21, Maven)이 없습니다.", "운영자가 서버 설정을 확인하세요."),
    "pending_approval": ("승인을 기다리는 제안이 있습니다.", "제안의 diff 를 검토해 승인·거절한 뒤 재개하세요."),
    "rejected_proposals": ("거절된 제안이 있어 해당 취약점은 남았습니다.", "거절한 항목을 담당 팀이 직접 조치하세요."),
    "residual_findings": ("최종 검사에 취약점이 남았습니다.", "아래 잔여 표를 보고 담당 팀이 조치하세요."),
    "manual_review": ("자동으로 바꿀 수 없는 항목(vendored / plugin / 매핑 불가 / 출처 불명 / 적용 안 된 제안)이 있습니다.",
                      "잔여·수동 검토 표의 항목을 사람이 확인하세요."),
    "no_candidate_version": ("승인된 저장소·정책에 쓸 수 있는 새 버전이 없는 항목이 있습니다.", "서버 정책(pins) 또는 저장소를 확인하세요."),
    "rounds_exhausted": ("반복 라운드 한도(max_rounds)에 닿았습니다.", "남은 항목을 사람이 확인하세요."),
    "stop_requested": ("중지 요청으로 멈췄습니다. 진행 중이던 원격 IVAS 분석은 IVAS 에서 계속 진행됩니다.",
                       "재개하면 멈춘 노드부터 이어서 합니다."),
    "internal_error": ("도구 내부 오류로 멈췄습니다.", "remediation.log 를 운영자에게 전달하세요."),
    "chain_budget": (("이관 빌드 파드에서 이어서 돈 조치가 시간 한도(remediation.chain_budget_minutes)를 다 써서 멈췄습니다. "
                      "진행 중이던 원격 IVAS 분석은 IVAS 에서 계속 진행됩니다."),
                     "재개하면 새 빌드 파드에서 멈춘 노드부터 이어서 합니다(검사 중이었다면 다시 올리지 않고 같은 분석을 이어서 조회)."),
}
# 정책 include 로 실패한 수정본을 결과 ZIP 으로 냈을 때의 candidate_build_failed 문구
_FAILED_INCLUDED_KO = (("버전을 바꾼 후보가 빌드되지 않았습니다. 결과 ZIP(delivery/result.zip)은 빌드가 실패한 그 수정본이며 IVAS "
                        "재검사는 하지 않았습니다. 원본은 original/source.zip 으로 그대로 보존됩니다."),
                       ("rounds/<n>/build.log 와 candidate.patch 를 보고 빌드를 고치거나 버전을 사람이 정하세요. 실패한 수정본을 "
                        "그대로 배포하지 마세요."))
_DECISION_KO = {D_AUTO: "자동", D_PENDING: "승인 대기", D_APPROVED: "승인됨", D_REJECTED: "거절됨"}


def _clean(text: Any, roots: list[str]) -> Any:
    """문자열이면 비밀 환경 변수 값·URL 자격증명(``graph.scrub_message`` 와 같은 규칙)과 실행 폴더의 절대 경로를 지우고
    비밀 정제 훅을 통과시킨다. 정제 훅은 출력 정제(``SBMIG_REDACT_OUTPUT``)가 켜졌을 때만 걸리므로 그것만 믿지 않는다."""
    if not isinstance(text, str):
        return text
    from .graph import scrub_message  # graph 가 이 모듈을 늦게 부른다 — 여기서도 늦게 불러 순환을 피한다
    for r in roots:
        text = text.replace(r + "/", "").replace(r, ".")
    return _scrub.scrub(scrub_message(text, None, len(text) + 1))


def _clean_json(obj: Any, roots: list[str]) -> Any:
    if isinstance(obj, dict):
        return {k: _clean_json(v, roots) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_json(v, roots) for v in obj]
    return _clean(obj, roots)


def _roots(paths: RunPaths) -> list[str]:
    out = {str(paths.root)}
    try:
        out.add(str(paths.root.resolve()))
    except OSError:
        pass
    return sorted(out, key=len, reverse=True)


def _load_scan(paths: RunPaths, rel: str) -> ScanResult | None:
    if not rel:
        return None
    data = read_json(paths.root / rel)
    return ScanResult.from_dict(data) if isinstance(data, dict) else None


def _load_plan(paths: RunPaths, n: int) -> RoundPlan | None:
    data = read_json(paths.plan(n))
    return RoundPlan.from_dict(data) if isinstance(data, dict) else None


def _ignore(ignore_actions: Iterable[str] | None) -> frozenset[str]:
    from .ivas import ignore_set
    return ignore_set(None if ignore_actions is None else list(ignore_actions))


def _split(scan: ScanResult | None, ignore: frozenset[str]) -> tuple[list[Any], list[Any]]:
    if scan is None:
        return [], []
    from .ivas import split_findings
    return split_findings(list(scan.findings), ignore)


def _scan_summary(scan: ScanResult | None, rel: str, ignore: frozenset[str] = frozenset()) -> dict[str, Any] | None:
    if scan is None:
        return None
    from .ivas import unknown_actions
    act, ign = _split(scan, ignore)
    return {"file": rel, "label": scan.label, "complete": scan.complete, "zip_sha256": scan.zip_sha256,
            "findings": len(scan.findings), "actionable": len(act), "ignored": len(ign),
            "unknown_actions": unknown_actions(list(scan.findings)), "counts": dict(sorted(scan.counts.items())),
            "incomplete_reasons": list(scan.incomplete_reasons), "status_code": scan.status_code}


def _ignored_rows(scan: ScanResult | None, ignore: frozenset[str]) -> list[dict[str, Any]]:
    """조치 대상에서 뺀 항목들(정렬 고정): 취약점·종류·대상·버전·코드·뜻."""
    from .ivas import action_meaning
    _act, ign = _split(scan, ignore)
    return [{"id": f.id, "kind": f.kind, "identifier": f.identifier, "version": f.version, "action": f.action,
             "meaning": action_meaning(f.action)}
            for f in sorted(ign, key=lambda x: (x.identifier, x.version, x.id, x.kind))]


def _plan_summary(plan: RoundPlan | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {"round": plan.round, "source_tree_hash": plan.source_tree_hash, "policy_hash": plan.policy_hash,
            "proposals": [{"id": p.id, "ga": p.ga, "from": p.from_version, "to": p.to_version, "kind": p.kind,
                           "risks": sorted(p.risks), "decision": p.decision, "evidence": p.evidence, "note": p.note,
                           "finding_ids": list(p.finding_ids), "proposal_hash": p.proposal_hash,
                           **({"modules": list(p.modules), "also_modules": list(p.also_modules)} if p.modules else {})}
                          for p in plan.proposals],
            "residuals": [r.to_dict() for r in plan.residuals], "notes": list(plan.notes)}


def _delivery_matches(state: RemediationState, final: ScanResult | None) -> bool:
    return bool(state.delivery and final is not None and final.zip_sha256 == state.delivery.sha256)


def _failed_delivery(state: RemediationState) -> bool:
    """결과 ZIP 이 빌드가 실패한 수정본인가(정책 include)."""
    return bool(state.delivery is not None and not state.delivery.build_ok)


def _verified_kept(paths: RunPaths) -> dict[str, Any] | None:
    """실패한 수정본에 result.zip 자리를 내준 앞 라운드의 검증된 ZIP(``delivery/result-r<k>-verified.zip``) 정보."""
    from .graph import find_verified_zip  # graph 가 이 모듈을 늦게 부른다 — 여기서도 늦게 불러 순환을 피한다
    z = find_verified_zip(paths)
    if z is None:
        return None
    data = read_json(z.with_suffix(".json"))
    data = data if isinstance(data, dict) else {}
    try:
        rel = paths.rel(z)
    except ValueError:
        rel = z.name
    return {"path": rel, "sha256": data.get("sha256", ""), "round": data.get("round", 0), "size": data.get("size", 0)}


def _delivery_dict(state: RemediationState) -> dict[str, Any] | None:
    d = state.delivery
    if d is None:
        return None
    return {"path": d.path, "sha256": d.sha256, "size": d.size, "round": d.round, "build_ok": d.build_ok,
            "scanned": d.scanned}


def build_report_json(state: RemediationState, paths: RunPaths, *,
                      ignore_actions: Iterable[str] | None = None) -> dict[str, Any]:
    """``report.json`` 의 내용(dict): 상태 전부, 기준·최종 검사 요약(조치 대상 / 뺀 항목 개수), 라운드별 계획 요약, 전달 ZIP
    일치 여부, 조치 대상에서 뺀 항목(``ignored_findings``). ``ignore_actions`` 가 None 이면 기본값(빈 목록).

    The content of `report.json`; `ignore_actions` None = the default codes."""
    ignore = _ignore(ignore_actions)
    base = _load_scan(paths, state.baseline_scan)
    final = _load_scan(paths, state.final_scan)
    rounds = []
    for rs in state.rounds:
        rounds.append({"round": rs.round, "plan": _plan_summary(_load_plan(paths, rs.round)),
                       "applied": list(rs.applied), "build": rs.build.to_dict() if rs.build else None,
                       "verify": list(rs.verify_problems), "committed": rs.committed, "scan_label": rs.scan_label})
    data = {"schema": REPORT_SCHEMA, "state_schema": SCHEMA, "state": state.to_dict(),
            "baseline_scan": _scan_summary(base, state.baseline_scan, ignore),
            "final_scan": _scan_summary(final, state.final_scan, ignore),
            "ignore_actions": sorted(ignore),
            "ignored_findings": _ignored_rows(final if final is not None else base, ignore),
            "delivery_matches_scanned_zip": _delivery_matches(state, final), "delivery": _delivery_dict(state),
            "verified_delivery": _verified_kept(paths), "rounds": rounds}
    return _clean_json(data, _roots(paths))


def _md_cell(text: Any) -> str:
    return str(text if text not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")


def _earlier_delivery(state: RemediationState) -> bool:
    """실패로 끝났지만 앞 라운드에서 검증을 마친 전달 ZIP 이 남아 있는가(실패한 라운드의 후보만 버렸다)."""
    last = max((rs.round for rs in state.rounds), default=0)
    return bool(state.status == FAILED and state.delivery is not None and state.delivery.round < last)


def _reason_text(state: RemediationState, code: str) -> tuple[str, str]:
    meaning, step = _REASON_KO[code]
    if code == "candidate_build_failed" and _failed_delivery(state):
        return _FAILED_INCLUDED_KO
    if _earlier_delivery(state) and code in ("candidate_build_failed", "text_only_violation", "resolution_mismatch"):
        d = state.delivery
        meaning = (meaning.split(".")[0] + ". 실패한 라운드의 후보는 버렸습니다. 전달 ZIP(delivery/result.zip)은 라운드 "
                   f"{d.round} 에서 빌드·재검사를 마친 상태이며 실패한 변경은 들어 있지 않습니다.")
    return meaning, step


def _next_steps(state: RemediationState) -> list[str]:
    if state.status == SUCCEEDED:
        return ["전달 ZIP(delivery/result.zip)을 받아 쓰면 됩니다. 최종 검사는 이 ZIP 과 같은 바이트로 했습니다."]
    codes = state.reasons or ([state.reason] if state.reason else [])
    out = [_reason_text(state, c)[1] for c in codes if c in _REASON_KO]
    return list(dict.fromkeys(out)) or ["보고서와 remediation.log 를 확인하세요."]


def build_report_md(state: RemediationState, paths: RunPaths, *, ignore_actions: Iterable[str] | None = None) -> str:
    """``report.md`` 의 글(한국어). 맨 앞: 상태·이유·다음에 할 일 → 기준 빌드 → 기준 검사 → 라운드별 → 전달 ZIP →
    조치 대상에서 뺀 항목 → 잔여 → 실행 경계 → 바꾸지 않은 것. ``ignore_actions`` 가 None 이면 기본값(빈 목록).

    The Korean `report.md` text."""
    ignore = _ignore(ignore_actions)
    roots = _roots(paths)
    base = _load_scan(paths, state.baseline_scan)
    final = _load_scan(paths, state.final_scan)
    L: list[str] = []
    L.append(f"# 취약점 조치 보고서 — {state.name or state.id}")
    L.append("")
    if _failed_delivery(state):
        d = state.delivery
        rs = next((x for x in state.rounds if x.round == d.round), None)
        ids = ", ".join(rs.applied) if rs and rs.applied else "-"
        blog = (rs.build.log if rs and rs.build and rs.build.log else f"rounds/{d.round}/build.log")
        L.append(f"> **주의: 결과 ZIP(`{d.path}`)은 빌드가 실패한 수정본입니다.** 라운드 {d.round} 의 후보 빌드가 실패했고, "
                 f"그 수정본(적용한 제안: {ids})을 그대로 담았습니다. 빌드 로그: `{blog}`.")
        L.append("> 이 ZIP 은 IVAS 재검사를 하지 않았습니다. 올린 원본은 `original/source.zip` (내려받기 이름 source.zip)으로 "
                 "그대로 보존됩니다.")
        kept = _verified_kept(paths)
        if kept:
            L.append(f"> 앞 라운드 {kept['round']} 에서 빌드·재검사를 마친 ZIP 은 `{kept['path']}` "
                     "(내려받기 이름 result-verified.zip)으로 남아 있습니다.")
        L.append("")
    L.append(f"- 상태: **{state.status}** ({_STATUS_KO.get(state.status, state.status)})")
    codes = state.reasons or ([state.reason] if state.reason else [])
    if codes:
        L.append("- 이유: " + ", ".join(f"`{c}`" for c in codes))
        for c in codes:
            if c in _REASON_KO:
                L.append(f"  - `{c}`: {_reason_text(state, c)[0]}")
    if state.message:
        L.append(f"- 메시지: {state.message}")
    L.append("- 다음에 할 일:")
    for s in _next_steps(state):
        L.append(f"  - {s}")
    L.append(f"- 작업 ID: `{state.id}` · 라운드: {len(state.rounds)}")
    if state.counts:
        L.append("- 집계: " + ", ".join(f"{k}={v}" for k, v in sorted(state.counts.items())))
    L.append("")

    L.append("## 기준 빌드")
    bb = state.baseline_build
    if bb is None:
        L.append("- 실행하지 않았습니다.")
    else:
        L.append(f"- 결과: {'성공' if bb.ok else '실패'} (종료 코드 {bb.exit_code}, {bb.seconds:.0f}초"
                 f"{', 시간 초과' if bb.timed_out else ''})")
        if bb.log:
            L.append(f"- 로그: `{bb.log}`")
        if bb.summary and not bb.ok:
            L.append(f"- 요약: {bb.summary}")
    L.append("")

    L.append("## 기준 검사 (IVAS)")
    if base is None:
        L.append("- 검사하지 않았습니다(또는 결과가 없습니다).")
    else:
        L.append(f"- 완료: {'예' if base.complete else '아니오 — 미완료 검사는 0건이 아닙니다'}")
        b_act, b_ign = _split(base, ignore)
        L.append(f"- 발견 항목: {len(base.findings)}건 (조치 대상 {len(b_act)}건, 조치 대상에서 뺀 항목 {len(b_ign)}건) · "
                 f"검사한 ZIP sha256: `{base.zip_sha256}`")
        if base.incomplete_reasons:
            L.append("- 미완료 이유: " + ", ".join(base.incomplete_reasons))
        if base.counts:
            L.append("- 범주별: " + ", ".join(f"{k}={v}" for k, v in sorted(base.counts.items())))
    L.append("")

    for rs in state.rounds:
        L.append(f"## 라운드 {rs.round}")
        plan = _load_plan(paths, rs.round)
        if plan is None or not plan.proposals:
            L.append("- 제안 없음")
        else:
            L.append("| 제안 | 라이브러리 | 버전 | 종류 | 위험 | 결정 | 근거 |")
            L.append("|---|---|---|---|---|---|---|")
            for p in plan.proposals:
                L.append(f"| {_md_cell(p.id)} | `{_md_cell(p.ga)}` | {_md_cell(p.from_version)} → {_md_cell(p.to_version)} "
                         f"| {_md_cell(p.kind)} | {_md_cell(', '.join(sorted(p.risks)))} "
                         f"| {_md_cell(_DECISION_KO.get(p.decision, p.decision))} | {_md_cell(p.evidence)} |")
            # 제안의 주의 문장(note)은 표 아래에 그대로: 속성 하나가 다른 라이브러리도 정한다는 사실, override 항목은 부모·BOM 이
            # 따라잡으면 담당자가 지워야 한다는 안내가 여기로 온다 — 표 칸에 넣기에는 길다
            for p in plan.proposals:
                if p.note:
                    L.append(f"- 주의 `{_md_cell(p.id)}`: {_md_cell(p.note)}")
            # 제안이 다스리는 모듈(검증이 from → to 를 요구하는 곳)과, 같은 버전이 다른 길로 해석돼 이 수정이 닿지 않을 수
            # 있는 모듈 — 범위가 없는 예전 제안(부모·BOM 버전 포함)에는 적지 않는다
            for p in plan.proposals:
                if p.modules:
                    L.append(f"- 다스리는 모듈 `{_md_cell(p.id)}`: {_md_cell(', '.join(p.modules))}")
                    if p.also_modules:
                        L.append(f"- 같은 버전이 다른 길로 해석된 모듈(이 수정이 닿지 않을 수 있음) `{_md_cell(p.id)}`: "
                                 f"{_md_cell(', '.join(p.also_modules))}")
        L.append(f"- 적용한 제안: {', '.join(rs.applied) if rs.applied else '없음'}")
        if rs.build is not None:
            L.append(f"- 후보 빌드: {'성공' if rs.build.ok else '실패'}" + (f" (로그 `{rs.build.log}`)" if rs.build.log else ""))
            if rs.build.summary and not rs.build.ok:
                L.append(f"  - 요약: {rs.build.summary}")
        L.append(f"- 확정(accepted 로 반영): {'예' if rs.committed else '아니오'}")
        if rs.verify_problems:
            L.append("- 검증 메모:")
            for v in rs.verify_problems:
                L.append(f"  - {v}")
        if rs.scan_label:
            L.append(f"- 재검사: `{rs.scan_label}`")
        L.append("")

    L.append("## 전달 ZIP")
    d = state.delivery
    if d is None:
        L.append("- 없습니다" + (" (실패한 후보는 결과 ZIP 에 넣지 않습니다 — 원본은 그대로)." if state.status == FAILED else "."))
    else:
        L.append(f"- 파일: `{d.path}` ({d.size} 바이트, 파일 {d.files}개, 라운드 {d.round})")
        L.append(f"- sha256: `{d.sha256}`")
        L.append(f"- 빌드 성공(build_ok): {'예' if d.build_ok else '아니오 — 빌드가 실패한 수정본입니다'}")
        L.append(f"- 이 ZIP 을 재검사했는가(scanned): {'예' if d.scanned else '아니오'}")
        L.append(f"- 최종 검사한 ZIP 과 sha256 이 같은가: {'예' if _delivery_matches(state, final) else '아니오'}")
        kept = _verified_kept(paths)
        if kept:
            L.append(f"- 앞 라운드 {kept['round']} 의 검증된 ZIP: `{kept['path']}` (sha256 `{kept['sha256']}`)")
        if _earlier_delivery(state):
            last = max(rs.round for rs in state.rounds)
            L.append(f"- 주의: 이 ZIP 은 라운드 {d.round} 에서 빌드·재검사를 마친 상태입니다. 라운드 {last} 의 실패한 변경은 "
                     "들어 있지 않습니다(그 변경은 rounds/<n>/candidate.patch 에만 남습니다).")
    if final is not None:
        f_act, f_ign = _split(final, ignore)
        L.append(f"- 최종 검사: `{state.final_scan}` · 완료 {'예' if final.complete else '아니오'} · 발견 {len(final.findings)}건"
                 f" (조치 대상 {len(f_act)}건, 조치 대상에서 뺀 항목 {len(f_ign)}건)")
        if _failed_delivery(state):
            L.append("  - 이 검사는 앞 라운드의 ZIP 을 검사한 것이며, 빌드가 실패한 지금의 결과 ZIP 을 검사한 것이 아닙니다.")
        if f_act:
            L.append("")
            L.append("| 취약점 | 종류 | 대상 | 버전 | 조치 코드 |")
            L.append("|---|---|---|---|---|")
            for f in sorted(f_act, key=lambda x: (x.identifier, x.version, x.id, x.kind)):
                L.append(f"| {_md_cell(f.id)} | {_md_cell(f.kind)} | `{_md_cell(f.identifier)}` | {_md_cell(f.version)} "
                         f"| {_md_cell(f.action)} |")
    L.append("")

    from .ivas import unknown_actions
    shown = final if final is not None else base
    L.append("## 조치 대상에서 뺀 항목")
    L.append(f"- 뺀 조치 코드(`remediation.ignore_actions`): {', '.join(sorted(ignore)) or '없음'} — 이 항목들은 검사 파일과 "
             "이 표에 남지만 계획하지 않고 남은 취약점으로 세지 않습니다.")
    rows = _ignored_rows(shown, ignore)
    if not rows:
        L.append("- 없음")
    else:
        L.append(f"- 기준: `{state.final_scan if final is not None else state.baseline_scan}`")
        L.append("")
        L.append("| 취약점 | 종류 | 대상 | 버전 | 조치 코드 | 뜻 |")
        L.append("|---|---|---|---|---|---|")
        for r in rows:
            L.append(f"| {_md_cell(r['id'])} | {_md_cell(r['kind'])} | `{_md_cell(r['identifier'])}` | {_md_cell(r['version'])} "
                     f"| {_md_cell(r['action'])} | {_md_cell(r['meaning'])} |")
    unknown = unknown_actions(list(shown.findings)) if shown is not None else []
    if unknown:
        L.append(f"- 알 수 없는 조치 코드(조치 대상으로 다룸): {', '.join(unknown)}")
    L.append("")

    L.append("## 잔여·수동 검토")
    if not state.residuals:
        L.append("- 없음")
    else:
        L.append("| 라이브러리 | 버전 | 출처 | 이유 | 설명 |")
        L.append("|---|---|---|---|---|")
        for r in state.residuals:
            L.append(f"| `{_md_cell(r.ga)}` | {_md_cell(r.version)} | {_md_cell(r.origin)} | `{_md_cell(r.reason)}` "
                     f"| {_md_cell(r.detail)} |")
    L.append("")

    L.append("## 실행 경계 점검")
    if not state.boundary:
        L.append("- 발견 없음")
    else:
        for b in state.boundary:
            act = "거부" if b.action == "refuse" else "기록만"
            L.append(f"- `{b.code}` `{b.path}` → {act}" + (f" — {b.detail}" if b.detail else ""))
    L.append("")

    L.append("## 바꾸지 않은 것")
    L.append("- 애플리케이션 소스, 프로젝트 구조(파일 목록·폴더)는 바꾸지 않았습니다. 전달 ZIP 의 파일 집합은 원본과 같고,")
    L.append("  다른 것은 승인·적용된 pom.xml 안의 버전 글자뿐입니다.")
    L.append("- 원본 ZIP(`original/source.zip`)은 그대로 보존합니다.")
    L.append("")
    return "\n".join(_clean(x, roots) for x in L)


def write_report(state: RemediationState, paths: RunPaths, *, ignore_actions: Iterable[str] | None = None) -> None:
    """``report.json`` 과 ``report.md`` 를 실행 폴더에 쓴다(원자적 교체). 같은 입력이면 같은 내용. ``ignore_actions`` 는
    그래프가 설정 값을 넘긴다(None = 기본값, 빈 목록).

    Write `report.json` and `report.md` into the run directory."""
    write_json_atomic(paths.report_json, build_report_json(state, paths, ignore_actions=ignore_actions))
    text = build_report_md(state, paths, ignore_actions=ignore_actions)
    tmp = paths.report_md.with_name(paths.report_md.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(paths.report_md)


def summary(state: RemediationState) -> dict[str, Any]:
    """API 목록용 작은 요약(dict).

    A small dict for the API list."""
    d = state.delivery
    return {"id": state.id, "name": state.name, "status": state.status, "reason": state.reason,
            "reasons": list(state.reasons), "node": state.node, "next_node": state.next_node, "round": state.round,
            "created": state.created, "updated": state.updated, "counts": dict(sorted(state.counts.items())),
            "require_approval": state.require_approval,
            "delivery": {"sha256": d.sha256, "size": d.size, "round": d.round, "build_ok": d.build_ok,
                         "scanned": d.scanned} if d else None}


__all__ = ["build_report_json", "build_report_md", "summary", "write_report"]
