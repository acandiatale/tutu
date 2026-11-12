"""취약점 조치 결과를 도구용 ``report.json``과 담당자용 한국어 ``report.md``로 만드는 모듈.
그래프의 마지막 노드 ``report``가 작업이 끝난 상태(SUCCEEDED / INCOMPLETE / FAILED)일 때만
``write_report``를 호출하고, API 목록은 ``summary``를 쓴다.
- 비밀 값, 토큰, 서버 절대 경로를 넣지 않는다. 경로는 실행 폴더 기준 상대 경로만 쓴다
- 입력이 같으면 내용도 같아야 하므로 현재 시각을 넣지 않고 정렬 순서를 고정한다
- report.md 순서: 결과 블록, 요약, 해야 할 일, 조치 완료, 승인 대기, 사내 라이브러리, 직접 확인, 제외 항목,
  변경하지 않은 것, 처리 기록. 빈 절은 쓰지 않는다
- 상태와 사유 코드는 접힌 처리 기록에만 쓰고, 영어 문장은 ``i18n.ko_display``로 바꿔 보여 준다.
  report.json에는 영어 문장과 코드를 그대로 쓴다
- ``inhouse_artifact`` 항목은 아티팩트별로 묶고 ``remediation.inhouse_message``를 붙인다
- ``remediation.ignore_actions``에 해당하는 항목은 제외 항목 절에만 쓰고 취약점 개수에 넣지 않는다

Remediation report: `report.json` (the state, the scan summaries, per-round plan summaries) and `report.md` (Korean, for
the project owners: a result line, a summary and the actions first; English sentences shown translated, residuals grouped by
explanation, in-house residuals grouped per in-house artifact, codes only in the folded record). Deterministic for the same
input; no secret, no absolute server path.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from sbmig.core import scrub as _scrub

from . import i18n
from .i18n import (
    BOUNDARY_KO,
    COUNT_KO,
    DECISION_KO,
    FINDING_KIND_KO,
    KIND_KO,
    ORIGIN_ALL_KO,
    ORIGIN_KO,
    REASON_KO,
    RISK_KO,
    SEVERITY_NONE_KO,
    evidence_ko,
    ko_display,
    label,
    severity_of,
    severity_thresholds,
)
from .models import (
    AWAITING_APPROVAL,
    AWAITING_CONFIRMATION,
    D_PENDING,
    E_INSERT,
    FAILED,
    INCOMPLETE,
    INTERRUPTED,
    K_DIRECT_VERSION,
    K_MANAGED_OVERRIDE,
    QUEUED,
    R_INHOUSE_ARTIFACT,
    RUNNING,
    SCHEMA,
    STOPPED,
    SUCCEEDED,
    Proposal,
    RemediationState,
    Residual,
    RoundPlan,
    RoundState,
    RunPaths,
    ScanResult,
    read_json,
    write_json_atomic,
)

log = logging.getLogger("sbmig.remediations.report")

REPORT_SCHEMA = "sbmig.remediation.report/1"

# 처리 기록과 다른 화면에서 쓰는 상태 코드별 짧은 한국어 이름
_STATUS_KO = {
    SUCCEEDED: "조치 완료", INCOMPLETE: "일부만 조치됨", FAILED: "조치 실패", AWAITING_APPROVAL: "승인 대기",
    AWAITING_CONFIRMATION: "업로드 확인 필요", STOPPED: "중지됨", INTERRUPTED: "중단됨", QUEUED: "대기 중", RUNNING: "진행 중",
}
# 결과 블록 첫 줄에 굵게 쓰는 문장
_VERDICT = {
    SUCCEEDED: "✅ **조치를 마쳤습니다.**", INCOMPLETE: "⚠️ **일부만 조치되었습니다.**", FAILED: "❌ **조치하지 못했습니다.**",
    AWAITING_APPROVAL: "⏸ **승인을 기다리고 있습니다.**", AWAITING_CONFIRMATION: "⏸ **확인이 필요합니다.**",
    STOPPED: "⏹ **조치를 멈췄습니다.**", INTERRUPTED: "⏹ **조치가 중단되었습니다.**",
    QUEUED: "⏳ **조치를 기다리고 있습니다.**", RUNNING: "⏳ **조치를 진행하고 있습니다.**",
}
# 사유 코드별 (결과 줄에 쓸 설명, 해야 할 일)이다. "<n>"은 마지막 라운드 번호로 바뀐다
# 승인, 사내, 직접 확인처럼 개수가 붙는 할 일은 _next_steps가 개수를 넣는다
_REASON_KO: dict[str, tuple[str, str]] = {
    "baseline_build_failed": ("올린 프로젝트가 조치 전 빌드에서 실패했습니다. 빌드되지 않는 프로젝트는 검사하지 않습니다.",
                              "**빌드 오류 고치기** — `baseline/build.log` 파일을 확인해 빌드 오류를 고친 뒤 프로젝트를 다시 "
                              "올려 주세요."),
    "candidate_build_failed": ("버전을 바꾼 수정본이 빌드에 실패해 수정본을 버렸습니다. 결과 ZIP은 만들지 않았고 원본은 그대로입니다.",
                               "**빌드 로그 확인하기** — `rounds/<n>/build.log` 파일과 `rounds/<n>/candidate.patch` 파일을 확인하고, "
                               "올릴 버전은 담당 팀에서 정해 주세요."),
    "scan_incomplete": ("IVAS 검사가 끝나지 않았습니다. 끝나지 않은 검사는 취약점 0건으로 보지 않습니다.",
                        "**IVAS 검사 상태 확인하기** — IVAS에서 분석 상태를 확인한 뒤 조치를 다시 실행해 주세요."),
    "upload_uncertain": ("IVAS에 파일을 올렸지만 접수 여부를 확인하지 못했습니다. 같은 파일이 두 번 접수되지 않도록 자동으로 다시 "
                         "올리지 않습니다.",
                         "**IVAS 접수 여부 확인하기** — IVAS 화면에서 파일이 접수됐는지 확인하고, 조치 화면에서 확인 결과를 "
                         "선택해 주세요."),
    "zip_hash_mismatch": ("IVAS가 검사한 파일이 결과 ZIP 또는 원본 ZIP과 다릅니다.",
                          "**결과 ZIP 사용 보류하기** — 결과 ZIP을 사용하지 말고 운영 담당자에게 알려 주세요."),
    "text_only_violation": ("수정본에서 버전 표기 외의 변경이 발견되어 수정본을 버렸습니다. 도구 오류일 가능성이 높습니다.",
                            "**운영 담당자에게 알리기** — `rounds/<n>/candidate.patch` 파일을 첨부해 운영 담당자에게 알려 주세요."),
    "resolution_mismatch": ("수정본을 빌드해 보니 의존성 버전이 계획과 다르게 적용되어 수정본을 버렸습니다.",
                            "**적용 결과 확인하기** — 「처리 기록」의 검증 내용과 `rounds/<n>/resolved.json` 파일을 확인하고, "
                            "올릴 버전은 담당 팀에서 정해 주세요."),
    "execution_boundary_refused": ("프로젝트에 서버의 Maven 실행 설정을 바꿀 수 있는 파일이 있어 작업을 중단했습니다.",
                                   "**빌드 설정 파일 정리하기** — 「처리 기록」의 빌드 설정 점검에 나온 파일을 프로젝트에서 "
                                   "제거한 뒤 다시 올려 주세요."),
    "not_ready": ("조치 서버의 실행 환경(IVAS 설정, settings.xml, JDK 21, Maven)이 준비되지 않았습니다.",
                  "**운영 담당자에게 알리기** — 운영 담당자에게 서버 설정 확인을 요청해 주세요."),
    "pending_approval": ("승인을 기다리는 변경이 있습니다.",
                         "**변경 승인하기** — 「승인 대기」의 변경을 확인한 뒤, 조치 화면의 체크박스 목록에서 적용할 변경을 "
                         "골라 「선택한 항목 적용」을 눌러 주세요."),
    "rejected_proposals": ("적용하지 않기로 한 변경이 있어 그 취약점은 남아 있습니다.",
                           "**적용하지 않은 변경 조치하기** — 적용하지 않기로 한 변경의 취약점은 담당 팀에서 직접 조치해 "
                           "주세요."),
    "residual_findings": ("조치 후 검사에서 취약점이 남아 있습니다.",
                          "**남은 취약점 확인하기** — 「처리 기록」의 남은 취약점 전체 목록을 확인하고 담당 팀에서 조치해 "
                          "주세요."),
    "manual_review": ("자동으로 조치할 수 없는 항목이 있습니다.",
                      "**직접 확인하기** — 「직접 확인이 필요한 취약점」의 항목은 자동으로 조치할 수 없습니다. 항목별 사유를 "
                      "확인하고 담당 팀에서 조치해 주세요."),
    "no_candidate_version": ("승인된 저장소나 서버 정책에서 올릴 수 있는 버전을 찾지 못한 항목이 있습니다.",
                             "**올릴 버전 정하기** — 버전을 찾지 못한 항목은 담당 팀에서 버전을 정하거나, 운영 담당자에게 "
                             "서버 정책에 버전 지정을 추가해 달라고 요청해 주세요."),
    "rounds_exhausted": ("반복 횟수 한도에 도달해 조치를 멈췄습니다.",
                         "**남은 취약점 확인하기** — 남은 취약점은 담당 팀에서 직접 확인해 주세요."),
    "stop_requested": ("중지 요청을 받아 조치를 멈췄습니다. 진행 중이던 IVAS 분석은 IVAS에서 계속됩니다.",
                       "**이어서 진행하기** — 계속하려면 조치 화면에서 「이어서 진행」을 눌러 주세요."),
    "internal_error": ("도구 내부 오류로 조치를 멈췄습니다.",
                       "**운영 담당자에게 알리기** — `remediation.log` 파일을 운영 담당자에게 전달해 주세요."),
    R_INHOUSE_ARTIFACT: ("사내 라이브러리에서 발생한 취약점이 있습니다. 사내 라이브러리의 pom은 담당자만 수정할 수 있어 이 "
                         "프로젝트에서는 변경하지 않았습니다.",
                         "**사내 라이브러리 담당자에게 요청하기** — 「사내 라이브러리에서 발생한 취약점」에 아티팩트별로 "
                         "정리했습니다. 각 아티팩트의 아키텍처 담당자에게 연락해 주세요."),
    "chain_budget": ("조치에 쓸 수 있는 시간을 다 써서 멈췄습니다. 진행 중이던 IVAS 분석은 IVAS에서 "
                     "계속됩니다. 이어서 진행하면 멈춘 단계부터 다시 시작하고, 검사 중이었다면 파일을 다시 "
                     "올리지 않고 같은 분석 결과를 조회합니다.",
                     "**이어서 진행하기** — 계속하려면 조치 화면에서 「이어서 진행」을 눌러 주세요."),
}
# include 정책에 따라 빌드에 실패한 수정본을 결과 ZIP으로 냈을 때 쓰는 candidate_build_failed 문장
_FAILED_INCLUDED_KO = ("버전을 바꾼 수정본이 빌드에 실패했습니다.",
                       "**빌드 오류 확인하기** — 빌드에 실패한 결과 ZIP은 배포하지 말고, `rounds/<n>/build.log` 파일과 "
                       "`rounds/<n>/candidate.patch` 파일을 확인해 빌드 오류를 고치거나 올릴 버전을 다시 정해 주세요.")
_SUCCEEDED_STEP = "**결과 ZIP 받기** — 재검사를 마친 `delivery/result.zip` 파일을 받아 사용해 주세요."
_DECISION_KO = DECISION_KO          # 다른 곳에서 이전 이름으로 import해도 깨지지 않게 남겨 둔다


def _clean(text: Any, roots: list[str]) -> Any:
    """문자열에서 비밀 환경 변수 값, URL 속 계정 정보, 실행 폴더 절대 경로를 지우고 마스킹 훅을 거친다.
    마스킹 훅은 ``SBMIG_REDACT_OUTPUT``이 켜졌을 때만 동작하므로 훅에만 의존하지 않는다."""
    if not isinstance(text, str):
        return text
    from .graph import scrub_message  # 순환 import를 피하려고 함수 안에서 import한다
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
    """조치 대상에서 뺀 항목을 표의 행 목록(취약점, 종류, 대상, 버전, 코드, 뜻)으로 만든다.
    정렬 순서는 고정한다."""
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
    """결과 ZIP이 빌드에 실패한 수정본인지(include 정책) 확인한다."""
    return bool(state.delivery is not None and not state.delivery.build_ok)


def _verified_kept(paths: RunPaths) -> dict[str, Any] | None:
    """실패한 수정본 대신 보관해 둔 앞 라운드의 검증된 ZIP(``delivery/result-r<k>-verified.zip``) 정보를 반환한다."""
    from .graph import find_verified_zip  # 순환 import를 피하려고 함수 안에서 import한다
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
                      ignore_actions: Iterable[str] | None = None,
                      inhouse_message: str | None = None,
                      severity: tuple[float, float, float] | None = None) -> dict[str, Any]:
    """``report.json``에 쓸 내용을 만든다. 상태 전체, 검사 요약, 라운드별 계획 요약, 결과 ZIP 일치 여부,
    ``ignored_findings``, 사내 아티팩트별 항목(``inhouse``)을 담는다.
    도구가 읽는 형식이므로 한국어 번역은 넣지 않는다.
    ``ignore_actions``, ``inhouse_message``가 None이면 설정 기본값을 쓴다.

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
            "verified_delivery": _verified_kept(paths), "rounds": rounds,
            "inhouse": _inhouse_json(state, inhouse_message or _default_inhouse_message()),
            "severity": _severity_json(base, final, ignore, severity_thresholds(severity))}
    return _clean_json(data, _roots(paths))


def _sev_counts(findings: Iterable[Any], th: tuple[float, float, float]) -> dict[str, int]:
    """발견 항목을 위험도(red / orange / yellow / none)별로 센다."""
    out = {"red": 0, "orange": 0, "yellow": 0, "none": 0}
    for f in findings:
        out[severity_of(f.score, th)[0]] += 1
    return out


def _severity_json(base: ScanResult | None, final: ScanResult | None, ignore: frozenset[str],
                   th: tuple[float, float, float]) -> dict[str, Any]:
    """report.json의 ``severity``를 만든다.
    위험도 경계값과, 조치 전 검사와 완료된 조치 후 검사의 위험도별 개수를 담는다."""
    return {"thresholds": {"red": th[0], "orange": th[1], "yellow": th[2]},
            "baseline": _sev_counts(_split(base, ignore)[0], th) if base is not None else None,
            "final": _sev_counts(_split(final, ignore)[0], th) if final is not None and final.complete else None}


def _md_cell(text: Any) -> str:
    return str(text if text not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")


def _earlier_delivery(state: RemediationState) -> bool:
    """작업은 실패로 끝났지만 앞 라운드에서 검증한 결과 ZIP이 남아 있는지 확인한다."""
    last = max((rs.round for rs in state.rounds), default=0)
    return bool(state.status == FAILED and state.delivery is not None and state.delivery.round < last)


def _last_round(state: RemediationState) -> int:
    return max((rs.round for rs in state.rounds), default=1)


def _reason_text(state: RemediationState, code: str) -> tuple[str, str]:
    """사유 코드에 맞는 (결과 줄 설명, 해야 할 일)을 반환한다.
    실패한 수정본을 냈거나 앞 라운드의 결과 ZIP이 남아 있으면 문장을 바꾼다."""
    what, step = _REASON_KO[code]
    if code == "candidate_build_failed" and _failed_delivery(state):
        what, step = _FAILED_INCLUDED_KO
    elif _earlier_delivery(state) and code in ("candidate_build_failed", "text_only_violation", "resolution_mismatch"):
        d = state.delivery
        head = {"candidate_build_failed": "수정본이 빌드에 실패했습니다.",
                "text_only_violation": "수정본에서 버전 표기 외의 변경이 발견되었습니다.",
                "resolution_mismatch": "수정본에서 의존성 버전이 계획과 다르게 적용되었습니다."}[code]
        what = (f"라운드 {_last_round(state)}의 {head} 그 라운드의 변경은 버렸습니다. 결과 ZIP은 라운드 {d.round}에서 "
                "빌드와 재검사를 마친 상태이며, 실패한 변경은 들어 있지 않습니다.")
    n = str(_last_round(state))
    return what.replace("<n>", n), step.replace("<n>", n)


def _codes(state: RemediationState) -> list[str]:
    return list(state.reasons or ([state.reason] if state.reason else []))


def _with_count(step: str, count: int | None, unit: str = "건") -> str:
    """할 일 제목 뒤에 "(N건)"을 붙인다. 개수를 모르거나 0이면 제목을 그대로 반환한다."""
    if not count:
        return step
    head, sep, rest = step.partition("** — ")
    return f"{head} ({count}{unit}){sep}{rest}" if sep else step


def _next_steps(state: RemediationState, fx: "_Facts | None" = None) -> list[str]:
    """현재 상태에서 담당자가 할 일 목록을 만든다. ``fx``가 있으면 개수를 붙인다.
    순서: 결과 ZIP 받기 -> 변경 승인 -> 사내 라이브러리 담당자에게 요청 -> 직접 확인 -> 나머지 사유 코드.

    The actions for this state, in a fixed order; counts when the report facts are given."""
    inhouse = [r for r in state.residuals if r.reason == R_INHOUSE_ARTIFACT]
    manual = [r for r in state.residuals if r.reason != R_INHOUSE_ARTIFACT]
    codes = [c for c in _codes(state) if c in _REASON_KO]
    out: list[str] = []
    if state.status == SUCCEEDED:
        out.append(_SUCCEEDED_STEP)
    others = [_reason_text(state, c)[1] for c in codes
              if c not in ("pending_approval", R_INHOUSE_ARTIFACT, "manual_review", "residual_findings")]
    if state.status in (FAILED, STOPPED, INTERRUPTED, AWAITING_CONFIRMATION):
        # 실패, 중단, 확인 대기는 원인을 먼저 해결해야 다음 단계로 갈 수 있으므로 앞에 둔다
        out += others
        others = []
    pending = fx.pending if fx is not None else []
    if "pending_approval" in codes or pending:
        out.append(_with_count(_reason_text(state, "pending_approval")[1], len(pending), "건"))
    if inhouse or R_INHOUSE_ARTIFACT in codes:
        # 다른 사유 때문에 evaluate의 사유 코드에 빠져 있어도 사내 아티팩트 항목은 항상 담당자 연락이 필요하다
        out.append(_with_count(_reason_text(state, R_INHOUSE_ARTIFACT)[1],
                               fx.matching(fx.inhouse) if fx is not None else None))
    if manual or "manual_review" in codes:
        out.append(_with_count(_reason_text(state, "manual_review")[1],
                               fx.matching(fx.manual) if fx is not None else None))
    elif "residual_findings" in codes:
        out.append(_with_count(_reason_text(state, "residual_findings")[1],
                               len(fx.f_act) if fx is not None and fx.final_ok else None))
    out += others
    if state.status == INTERRUPTED and not out:
        out.append("**이어서 진행하기** — 상태를 확인한 뒤 조치 화면에서 「이어서 진행」을 눌러 주세요.")
    return list(dict.fromkeys(out)) or ["**처리 기록 확인하기** — 「처리 기록」과 `remediation.log` 파일을 확인해 주세요."]


def _default_inhouse_message() -> str:
    from sbmig.core.config import RemediationConfig
    return RemediationConfig().inhouse_message


def _inhouse_of(r: Residual) -> list[str]:
    """사내 아티팩트 항목을 묶을 g:a 목록을 반환한다. 항목의 ``inhouse``, 이전 형식의 상태 파일이면 설명
    문장 속 g:a, 그래도 없으면 항목 자신의 g:a 순으로 찾는다."""
    return sorted(set(r.inhouse or i18n.inhouse_gas(r.detail) or [r.ga]))


def _inhouse_json(state: RemediationState, message: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in state.residuals:
        if r.reason != R_INHOUSE_ARTIFACT:
            continue
        for ga in _inhouse_of(r):
            groups.setdefault(ga, []).append({"ga": r.ga, "version": r.version, "modules": list(r.modules),
                                              "via": list(r.via), "finding_ids": list(r.finding_ids)})
    return {"message": message,
            "artifacts": [{"ga": ga, "libraries": sorted(libs, key=lambda x: (x["ga"], x["version"]))}
                          for ga, libs in sorted(groups.items())]}


# ---------------------------------------------------------------------------- report.md 도우미
def _cut(items: Iterable[str], unit: str = "개", limit: int = 4, show: int = 3) -> str:
    """긴 목록을 앞의 몇 개와 "외 N개"로 줄인다."""
    xs = [str(x) for x in items if str(x)]
    if not xs:
        return "-"
    if len(xs) > limit:
        return ", ".join(xs[:show]) + f" 외 {len(xs) - show}{unit}"
    return ", ".join(xs)


def _mods(mods: Iterable[str]) -> str:
    return _cut(sorted(set(mods)))


def _mods_code(mods: Iterable[str]) -> str:
    """본문에 쓸 모듈 목록을 `a`, `b`, `c` 외 N개 형식으로 만든다."""
    xs = sorted(set(m for m in mods if m))
    shown = [f"`{m}`" for m in xs[:3]] if len(xs) > 4 else [f"`{m}`" for m in xs]
    return ", ".join(shown) + (f" 외 {len(xs) - 3}개" if len(xs) > 4 else "") if xs else "-"


def _aid(coord: str) -> str:
    """"g:a[:v]"에서 artifactId를 꺼낸다. 형식이 다르면 그대로 반환한다."""
    parts = str(coord or "").split(":")
    return parts[1] if len(parts) >= 2 and parts[1] else str(coord or "")


def _num(n: int) -> str:
    return f"{n:,}"


def _mb(size: int) -> str:
    mb = (size or 0) / (1024 * 1024)
    return "0.1 MB 미만" if 0 < mb < 0.05 else f"{mb:.1f} MB"


def _key(f: Any) -> tuple[str, str]:
    return (f.identifier, f.version)


class _Facts:
    """report.md의 여러 절이 함께 쓰는 값을 한 번만 계산해 두는 클래스."""

    def __init__(self, state: RemediationState, paths: RunPaths, ignore: frozenset[str],
                 th: tuple[float, float, float] | None = None):
        self.th = severity_thresholds(th)
        self.base = _load_scan(paths, state.baseline_scan)
        self.final = _load_scan(paths, state.final_scan)
        self.b_act, self.b_ign = _split(self.base, ignore)
        self.f_act, self.f_ign = _split(self.final, ignore)
        self.plans = {rs.round: _load_plan(paths, rs.round) for rs in state.rounds}
        self.committed = []
        for rs in sorted(state.rounds, key=lambda x: x.round):
            pl = self.plans.get(rs.round)
            if rs.committed and pl is not None:
                ids = set(rs.applied)
                self.committed += [p for p in pl.proposals if p.id in ids]
        self.pending = [p for n in sorted(self.plans) if self.plans[n] is not None
                        for p in self.plans[n].proposals if p.decision == D_PENDING]
        self.inhouse = [r for r in state.residuals if r.reason == R_INHOUSE_ARTIFACT]
        self.manual = [r for r in state.residuals if r.reason != R_INHOUSE_ARTIFACT]
        self.final_ok = self.final is not None and self.final.complete
        # 조치한 취약점은 조치 후 검사에 없으므로 점수는 완료된 조치 후 검사에서 먼저, 다음에 조치 전 검사에서 찾는다
        # 같은 ID가 여러 라이브러리에 있으면 (ID, 대상, 버전)이 맞는 점수를, 없으면 그 ID의 최고 점수를 쓴다
        self._exact: list[dict[tuple[str, str, str], float]] = []
        self._by_id: list[dict[str, float]] = []
        for scan in ([self.final] if self.final_ok else []) + ([self.base] if self.base is not None else []):
            exact: dict[tuple[str, str, str], float] = {}
            by_id: dict[str, float] = {}
            for f in scan.findings:
                if f.score is None:
                    continue
                k = (f.id, f.identifier, f.version)
                exact[k] = max(exact.get(k, f.score), f.score)
                by_id[f.id] = max(by_id.get(f.id, f.score), f.score)
            self._exact.append(exact)
            self._by_id.append(by_id)

    def matching(self, items: list[Any]) -> int:
        """이 항목이나 제안에 해당하는, 조치 전 검사의 조치 대상 취약점 수를 센다.
        조치 전 검사가 없으면 취약점 ID 수를 반환한다."""
        keys = {(getattr(r, "ga", ""), getattr(r, "version", None) or getattr(r, "from_version", "")) for r in items}
        if self.base is not None:
            return sum(1 for f in self.b_act if _key(f) in keys)
        return sum(len(set(r.finding_ids)) for r in items)

    def fixed(self) -> int | None:
        """조치 전 검사의 조치 대상 중 완료된 조치 후 검사에서 사라진 취약점 수를 센다.
        조치 후 검사가 없거나 완료되지 않았으면 None을 반환한다."""
        if self.base is None or not self.final_ok:
            return None
        left = {f.key for f in self.f_act}
        return sum(1 for f in self.b_act if f.key not in left)

    def cve_score(self, cve: str, ga: str, version: str) -> float | None:
        """취약점 하나의 점수를 반환한다. 검사 결과에 없으면 None을 반환한다."""
        for exact in self._exact:
            if (cve, ga, version) in exact:
                return exact[(cve, ga, version)]
        for by_id in self._by_id:
            if cve in by_id:
                return by_id[cve]
        return None

    def row_score(self, ids: Iterable[str], ga: str, version: str) -> float | None:
        """라이브러리 행의 취약점 중 가장 높은 점수를 반환한다."""
        got = [x for x in (self.cve_score(i, ga, version) for i in set(ids)) if x is not None]
        return max(got) if got else None

    def sev_cell(self, score: float | None) -> str:
        """위험도 칸의 문자열을 "🔴 9.8" 형식으로 만든다.
        표시 기준보다 낮으면 숫자만, 점수가 없으면 "-"를 쓴다."""
        if score is None:
            return "-"
        mark = severity_of(score, self.th)[1]
        return f"{mark} {score:.1f}" if mark else f"{score:.1f}"

    def cve_cell(self, ga: str, version: str, ids: Iterable[str]) -> str:
        """취약점 칸의 문자열을 만든다.
        점수가 높은 순으로 "ID (점수)"를 세 개까지 쓰고 나머지는 "외 N건"으로 줄인다."""
        scored = [(i, self.cve_score(i, ga, version)) for i in sorted(set(ids))]
        if not scored:
            return "-"
        scored.sort(key=lambda x: (x[1] is None, -(x[1] or 0.0), x[0]))
        parts = []
        for cve, sc in scored[:3]:
            mark = severity_of(sc, self.th)[1]
            parts.append(" ".join(x for x in (mark, cve, f"({sc:.1f})" if sc is not None else "") if x))
        return ", ".join(parts) + (f" 외 {len(scored) - 3}건" if len(scored) > 3 else "")

    def order(self, score: float | None, ga: str, version: str) -> tuple:
        """표의 행 정렬 키를 만든다. 점수가 높은 순(점수 없음은 맨 뒤), 그다음 g:a, 버전 순이다."""
        return (score is None, -(score or 0.0), ga, version)

    def sev_counts(self, findings: Iterable[Any]) -> dict[str, int]:
        return _sev_counts(findings, self.th)


def _folded(L: list[str], table: list[str], summary: str) -> None:
    """``summary``가 비어 있으면 표를 그대로 넣고, 아니면 ``<details>``로 접어서 넣는다.
    미리보기에서 표가 그려지도록 앞뒤에 빈 줄을 둔다."""
    L.append("")
    if summary:
        L.append(f"<details><summary>{summary}</summary>")
        L.append("")
    L.extend(table)
    if summary:
        L.append("")
        L.append("</details>")


def _known_and_raw(text: str) -> tuple[list[str], str]:
    """상태 메시지를 (번역된 문장들, 번역 규칙에 없는 부분의 원문)으로 나눈다.
    번역되지 않은 부분이 없으면 원문은 빈 문자열이다."""
    ko, raw = i18n.split_known(str(text or ""))
    return ko, " | ".join(raw)


# 메시지가 빌드 요약이거나 결과 줄과 같은 내용이므로 결과 줄에 넣지 않는 사유 코드
_MESSAGE_ELSEWHERE = ("baseline_build_failed", "candidate_build_failed", "pending_approval", "text_only_violation",
                      "resolution_mismatch")
# 상태 메시지가 더 구체적이므로 결과 줄의 일반 문장 대신 메시지를 쓰는 사유 코드
_MESSAGE_SAYS_IT = ("chain_budget", "upload_uncertain", "stop_requested")
# 결과 줄의 숫자 문장이 이미 설명하므로 따로 쓰지 않는 사유 코드
_NUMBERS_EXPLAIN = ("manual_review", "residual_findings", R_INHOUSE_ARTIFACT, "pending_approval")


def _numbers(state: RemediationState, fx: _Facts) -> list[str]:
    """결과 블록에 쓸 숫자 문장들을 만든다. 조치 전 검사가 끝났을 때만 만든다."""
    if fx.base is None or not fx.base.complete:
        return []
    total = len(fx.b_act)
    if total == 0:
        return ["조치 전 검사에서 조치할 취약점이 없었습니다."]
    fixed = fx.fixed()
    left = len(fx.f_act) if fx.final_ok else None
    if fixed is None:
        out = [f"조치 전 검사에서 취약점 {_num(total)}건을 찾았습니다."]
    elif left == 0:
        out = [f"취약점 {_num(total)}건을 모두 조치했고, 재검사에서 남은 취약점이 없습니다."]
    elif fixed == 0:
        out = [f"취약점 {_num(total)}건 가운데 조치한 취약점은 없고 {_num(left or 0)}건이 남았습니다."]
    else:
        out = [f"취약점 {_num(total)}건 가운데 {_num(fixed)}건을 조치했고 {_num(left or 0)}건이 남았습니다."]
    n_in, n_man = fx.matching(fx.inhouse), fx.matching(fx.manual)
    if n_in or n_man:
        whole = left is not None and left == n_in + n_man
        lead = f"남은 {_num(left or 0)}건" if whole else "남은 취약점"
        to_in = f"{_num(n_in)}건은 사내 라이브러리 담당자에게 요청해야" if n_in else ""
        to_man = f"{_num(n_man)}건은 프로젝트에서 직접 확인해야" if n_man else ""
        if n_in and n_man:
            out.append(f"{lead} 중 {to_in} 하고, {to_man} 합니다.")
        elif whole:
            out.append(f"{lead}은 모두 " + ("사내 라이브러리 담당자에게 요청해야 합니다." if n_in
                                            else "프로젝트에서 직접 확인해야 합니다."))
        else:
            out.append(f"{lead} 중 {to_in or to_man} 합니다.")
    return out


def _result_lines(state: RemediationState, fx: _Facts) -> list[str]:
    """결과 블록의 줄들("> " 없이)을 만든다. 굵은 문장, 무슨 일이 있었는지, 숫자 순이다."""
    codes = [c for c in _codes(state) if c in _REASON_KO]
    out = [_VERDICT.get(state.status, f"**{_STATUS_KO.get(state.status, state.status)}**")]
    body: list[str] = []
    if state.status == AWAITING_APPROVAL:
        n = len(fx.pending)
        body += _numbers(state, fx)[:1]
        body.append((f"제안 {n}건이 승인을 기다립니다." if n else "승인을 기다리는 변경이 있습니다.")
                    + " 조치 화면에서 적용할 변경을 고르면 이어서 진행합니다.")
    elif state.status == INTERRUPTED:
        ko, _raw = _known_and_raw(state.message)
        body += ko or ["서버가 다시 시작되면서 조치가 멈췄습니다. 자동으로 다시 실행하지 않습니다."]
        return out + [" ".join(body)]
    elif state.status in (SUCCEEDED, INCOMPLETE):
        body += _numbers(state, fx)
        body += [_reason_text(state, c)[0] for c in codes if c not in _NUMBERS_EXPLAIN]
        if state.status == INCOMPLETE and not body:
            body += [_reason_text(state, c)[0] for c in codes]
    else:
        ko, raw = _known_and_raw(state.message)
        if codes[:1] and codes[0] in _MESSAGE_SAYS_IT and ko and not raw:
            # 메시지가 검사 이름이나 시간 한도까지 알려 주므로 일반 문장 대신 쓴다
            return out + [" ".join(ko)]
        body += [_reason_text(state, c)[0] for c in codes[:1]]
    if state.message and not (set(codes) & set(_MESSAGE_ELSEWHERE)):
        ko, _raw = _known_and_raw(state.message)
        if ko:
            body.append("상세: " + " ".join(ko))
    if fx.pending and state.status not in (AWAITING_APPROVAL,):
        body.append(f"승인을 기다리는 제안이 {len(fx.pending)}건 있습니다.")
    return out + ([" ".join(dict.fromkeys(body))] if body else [])


def _place(p: Proposal) -> str:
    """제안이 수정하는 위치를 "`pom` 41행" 형식으로 만든다. 항목을 추가하는 제안은 "41행 앞에 추가"로 쓴다."""
    where = []
    for e in p.edits:
        where.append(f"`{_md_cell(e.pom)}` {e.line}행" + (" 앞에 추가" if e.op == E_INSERT else ""))
    return ", ".join(where) or "-"


def _note_lines(text: str) -> list[str]:
    """제안 메모를 한국어 문장으로 바꾼다.
    규칙 하나가 한 줄이 되고, 번역하지 못한 부분은 마지막 줄에 원문으로 둔다."""
    ko, raw = _known_and_raw(text)
    return [k for k in ko if k] + ([f"원문: `{raw.replace('`', chr(39))}`"] if raw else [])


def _proposal_section(L: list[str], fx: _Facts, props: list[Proposal], *, pending: bool) -> None:
    head = (["위험도", "라이브러리", "버전 변경", "수정할 곳", "주의", "해결할 취약점"] if pending
            else ["위험도", "라이브러리", "버전 변경", "수정한 곳", "해결된 취약점"])
    L.append("| " + " | ".join(head) + " |")
    L.append("|" + "---|" * len(head))
    scores = {p.id: fx.row_score(p.finding_ids, p.ga, p.from_version) for p in props}
    props = sorted(props, key=lambda p: (*fx.order(scores[p.id], p.ga, p.from_version), p.id))
    for p in props:
        cells = [fx.sev_cell(scores[p.id]), f"`{_md_cell(p.ga)}`", f"{_md_cell(p.from_version)} → {_md_cell(p.to_version)}",
                 _place(p)]
        if pending:
            cells.append(_md_cell(", ".join(label(RISK_KO, r) for r in sorted(p.risks)) or "-"))
        cells.append(_md_cell(fx.cve_cell(p.ga, p.from_version, p.finding_ids)))
        L.append("| " + " | ".join(cells) + " |")
    L.append("")
    for p in props:
        L.append(f"- **`{_md_cell(_aid(p.ga))}` {_md_cell(p.from_version)} → {_md_cell(p.to_version)}** "
                 f"(제안 `{_md_cell(p.id)}`)")
        if p.evidence:
            L.append(f"  - 버전 확인: {_md_cell(evidence_ko(p.evidence))}")
        if p.kind and p.kind != K_DIRECT_VERSION:
            L.append(f"  - 수정 방식: {_md_cell(label(KIND_KO, p.kind))}")
        if p.modules:
            L.append(f"  - 적용 범위: {_md_cell(_mods_code(p.modules))}")
        notes = _note_lines(p.note) if p.note else []
        if len(notes) == 1:
            L.append(f"  - 참고: {_md_cell(notes[0])}")
        elif notes:
            L.append("  - 참고:")
            L += [f"    - {_md_cell(n)}" for n in notes]
        if p.also_modules and "also_resolved_edit" not in i18n.rule_keys(p.note):
            L.append(f"  - 적용되지 않을 수 있는 모듈: {_md_cell(_mods_code(p.also_modules))} — 같은 라이브러리를 다른 경로로 "
                     "가져옵니다.")


def _dep_path(r: Residual, owner: str) -> str:
    """사내 라이브러리 표의 의존 경로를 만든다.
    artifactId를 화살표로 잇고 취약한 라이브러리로 끝내며, 버전은 버전 칸에 따로 쓴다."""
    if r.via:
        aids = [_aid(x) for x in r.via]
        if aids[-1] != _aid(r.ga):
            aids.append(_aid(r.ga))
        return " → ".join(aids)
    if r.ga == owner:
        return "사내 라이브러리 자체"
    if r.origin in ("parent_managed", "bom_managed"):
        return label(ORIGIN_KO, r.origin)
    return "-"


def _is_parent_group(rows: list[Residual]) -> bool:
    """이 묶음이 회사 부모 pom에서 온 것인지 확인한다.
    의존 경로 없이 부모 pom이나 BOM이 버전을 정하는 행이 있으면 그렇다고 본다."""
    return any(not r.via and r.origin in ("parent_managed", "bom_managed") for r in rows)


def _common_modules(rows: list[Residual]) -> str | None:
    """모든 행의 모듈 목록이 같으면 그 목록을, 다르면 None을 반환한다.
    모듈이 하나도 없으면 빈 문자열을 반환한다."""
    sets = {tuple(sorted(set(r.modules))) for r in rows}
    if len(sets) == 1:
        only = next(iter(sets))
        return _mods_code(only) if only else ""
    return None


def _lib_cell(r: Residual) -> str:
    lib = f"`{_md_cell(r.ga)}`"
    variant = i18n.variant_label(r.detail)
    if variant:
        lib += f" ({_md_cell(variant)})"
    return lib


def _inhouse_section(L: list[str], fx: _Facts, message: str) -> None:
    L.append(f"## 사내 라이브러리에서 발생한 취약점 — {_num(fx.matching(fx.inhouse))}건")
    L.append("")
    L.append(f"> {message}")
    L.append("")
    L.append("사내 라이브러리가 가져오는 의존성이거나, 회사 부모 pom에서 버전을 지정하는 라이브러리입니다. 사내 라이브러리의 "
             "pom은 담당자만 수정할 수 있어 이 프로젝트에서는 변경하지 않았습니다.")
    groups: dict[str, list[Residual]] = {}
    for r in fx.inhouse:
        for ga in _inhouse_of(r):
            groups.setdefault(ga, []).append(r)
    for ga in sorted(groups, key=lambda g: (-len(groups[g]), g)):
        rows = sorted(groups[ga], key=lambda r: fx.order(fx.row_score(r.finding_ids, r.ga, r.version), r.ga, r.version))
        L.append("")
        tag = " (회사 부모 pom)" if _is_parent_group(rows) else ""
        L.append(f"### `{_md_cell(ga)}`{tag} — 라이브러리 {len(rows)}개")
        L.append("")
        common = _common_modules(rows)
        if common:
            L.append(f"사용 모듈: {_md_cell(common)}")
            L.append("")
        cols = ["위험도", "취약한 라이브러리", "버전", "의존 경로"] + ([] if common is not None else ["사용 모듈"]) + ["취약점"]
        L.append("| " + " | ".join(cols) + " |")
        L.append("|" + "---|" * len(cols))
        for r in rows:
            cells = [fx.sev_cell(fx.row_score(r.finding_ids, r.ga, r.version)), _lib_cell(r), _md_cell(r.version),
                     _md_cell(_dep_path(r, ga))]
            if common is None:
                cells.append(_md_cell(_mods(r.modules)))
            cells.append(_md_cell(fx.cve_cell(r.ga, r.version, r.finding_ids)))
            L.append("| " + " | ".join(cells) + " |")


def _manual_section(L: list[str], fx: _Facts) -> None:
    L.append(f"## 직접 확인이 필요한 취약점 — {_num(fx.matching(fx.manual))}건")
    groups: dict[tuple[str, str], list[Residual]] = {}
    texts: dict[tuple[str, str], str] = {}
    for r in fx.manual:
        key, text = i18n.detail_group(r.detail)
        groups.setdefault((r.reason, key), []).append(r)
        texts[(r.reason, key)] = text
    order = sorted(groups, key=lambda k: (-len(groups[k]), k[0], k[1]))
    titles: dict[tuple[str, str], str] = {}
    other_n = 0
    for gk in order:
        title = i18n.group_meta(gk[1])[0]
        if not title:
            if gk[0] != "manual_review" and gk[0] in REASON_KO:
                title = label(REASON_KO, gk[0])
            else:
                other_n += 1
                title = "기타 사유"
        titles[gk] = title
    if other_n > 1:
        n = 0
        for gk in order:
            if titles[gk] == "기타 사유":
                n += 1
                titles[gk] = f"기타 사유 {n}"
    dup = {t for t in titles.values() if list(titles.values()).count(t) > 1}
    for gk in order:
        reason, key = gk
        rows = sorted(groups[gk], key=lambda r: (*fx.order(fx.row_score(r.finding_ids, r.ga, r.version), r.ga, r.version),
                                                 r.origin))
        title = titles[gk] + (f" ({label(REASON_KO, reason)})" if titles[gk] in dup else "")
        memo = i18n.group_meta(key)[1]
        L.append("")
        L.append(f"### {title} — 라이브러리 {len(rows)}개")
        L.append("")
        L.append(_md_cell(texts[gk] or "-"))
        # 모든 행이 같은 값인 칸은 표에서 빼고 표 위에 한 번만 쓴다
        origins = {r.origin for r in rows}
        lift_origin = len(rows) > 1 and len(origins) == 1
        common = _common_modules(rows) if len(rows) > 1 else None
        no_mods = all(not r.modules for r in rows)
        memos = [i18n.row_memo(r.detail) if memo else "" for r in rows]
        memo_col = memo and len(set(memos)) > 1
        above: list[str] = []
        if lift_origin:
            o = next(iter(origins))
            above.append(ORIGIN_ALL_KO.get(o, f"구분: {label(ORIGIN_KO, o)}"))
        if common:
            above.append(f"사용 모듈: {common}")
        if memo and not memo_col and memos and memos[0]:
            above.append(f"내용: {memos[0]}")
        for a in above:
            L.append("")
            L.append(_md_cell(a))
        cols = ["위험도", "라이브러리", "버전"] + ([] if lift_origin else ["구분"])
        cols += [] if (common is not None or no_mods) else ["사용 모듈"]
        cols += ["취약점"] + (["내용"] if memo_col else [])
        L.append("")
        L.append("| " + " | ".join(cols) + " |")
        L.append("|" + "---|" * len(cols))
        for r, m in zip(rows, memos):
            lib = _lib_cell(r)
            if r.inhouse:
                lib += " — 일부 모듈은 사내 라이브러리에서 가져옴"
            cells = [fx.sev_cell(fx.row_score(r.finding_ids, r.ga, r.version)), lib, _md_cell(r.version)]
            if not lift_origin:
                cells.append(_md_cell(label(ORIGIN_KO, r.origin)))
            if common is None and not no_mods:
                cells.append(_md_cell(_mods(r.modules)))
            cells.append(_md_cell(fx.cve_cell(r.ga, r.version, r.finding_ids)))
            if memo_col:
                cells.append(_md_cell(m))
            L.append("| " + " | ".join(cells) + " |")


def _ivas_action(code: str) -> str:
    """IVAS 조치 상태 칸의 문자열을 만든다. 예: "조치 필요 (NE)"."""
    from .ivas import action_meaning
    c = str(code or "")
    return f"{action_meaning(c)} ({c})" if c else action_meaning(c)


def _ident_cell(identifier: str, kind: str) -> str:
    cell = f"`{_md_cell(identifier)}`"
    return cell if kind in ("library", "") else f"{cell} ({_md_cell(label(FINDING_KIND_KO, kind))})"


def _excluded_section(L: list[str], state: RemediationState, fx: _Facts, ignore: frozenset[str]) -> None:
    shown = fx.final if fx.final is not None else fx.base
    rows = _ignored_rows(shown, ignore)
    if not rows:
        return
    from .ivas import action_meaning
    L.append(f"## 조치 대상에서 제외한 항목 — {_num(len(rows))}건")
    L.append("")
    codes = ", ".join(f"{action_meaning(c)}({c})" for c in sorted(ignore))
    L.append(f"IVAS 조치 상태가 {codes} 인 항목은 조치 대상에서 제외했습니다. 검사 결과에는 남아 있지만 남은 취약점으로 세지 "
             "않습니다.")
    table = ["| 위험도 | 취약점 | 라이브러리 | 버전 | IVAS 조치 상태 |", "|---|---|---|---|---|"]
    ign_scores: dict[tuple[str, str, str, str], float] = {}
    for f in _split(shown, ignore)[1]:
        if f.score is not None:
            k = (f.id, f.kind, f.identifier, f.version)
            ign_scores[k] = max(ign_scores.get(k, f.score), f.score)
    keyed = [(ign_scores.get((r["id"], r["kind"], r["identifier"], r["version"])), r) for r in rows]
    keyed.sort(key=lambda x: (*fx.order(x[0], x[1]["identifier"], x[1]["version"]), x[1]["id"], x[1]["kind"]))
    for sc, r in keyed:
        table.append(f"| {fx.sev_cell(sc)} | {_md_cell(r['id'])} | {_ident_cell(r['identifier'], r['kind'])} "
                     f"| {_md_cell(r['version'])} | {_md_cell(_ivas_action(r['action']))} |")
    _folded(L, table, f"제외한 항목 전체 — {_num(len(rows))}건" if len(rows) > 10 else "")
    L.append("")


def _summary_section(L: list[str], state: RemediationState, fx: _Facts, paths: RunPaths) -> None:
    base, final = fx.base, fx.final
    L.append("## 요약")
    L.append("")
    if base is None:
        L.append("조치 전 검사를 하지 않아 취약점 개수를 알 수 없습니다.")
    else:
        libs = len({_key(f) for f in fx.b_act})
        found = f"{_num(len(fx.b_act))}건" + ("" if base.complete else " (검사 미완료)")
        fixed = fx.fixed()
        fixed_n = fixed if fixed is not None else fx.matching(fx.committed)
        L.append("| 구분 | 취약점 | 라이브러리 |")
        L.append("|---|---|---|")
        L.append(f"| 조치 전 | {found} | {_num(libs)}개 |")
        n_lib = len({p.ga for p in fx.committed})
        L.append(f"| 조치 완료 | {_num(fixed_n)}건 | " + (f"{_num(n_lib)}개 |" if n_lib else "- |"))
        n_pend = fx.matching(fx.pending)
        if fx.pending:
            L.append(f"| 승인 대기 | {_num(n_pend)}건 | {_num(len({p.ga for p in fx.pending}))}개 |")
        if fx.inhouse:
            L.append(f"| 사내 라이브러리 담당자 요청 | {_num(fx.matching(fx.inhouse))}건 | {_num(len(fx.inhouse))}개 |")
        if fx.manual:
            L.append(f"| 직접 확인 필요 | {_num(fx.matching(fx.manual))}건 | {_num(len(fx.manual))}개 |")
        n_ign = len(fx.f_ign if final is not None else fx.b_ign)
        if n_ign:
            L.append(f"| 조치 대상에서 제외 | {_num(n_ign)}건 | - |")
        if final is None:
            left = "재검사하지 않음"
        elif not final.complete:
            left = "재검사 미완료"
        else:
            left = f"{_num(len(fx.f_act))}건"
        L.append(f"| 조치 후 남은 취약점 | {left} | - |")
        L.append("")
        red, orange, yellow = fx.th
        bc = fx.sev_counts(fx.b_act)
        fc = fx.sev_counts(fx.f_act) if fx.final_ok else None
        L.append("| 위험도 | 기준 (IVAS 점수) | 조치 전 | 조치 후 |")
        L.append("|---|---|---|---|")
        for (key, mark, name), bound in zip(i18n.SEVERITY_LEVELS, (red, orange, yellow)):
            after = f"{_num(fc[key])}건" if fc is not None else "-"
            L.append(f"| {mark} {name} | {bound:.1f} 이상 | {_num(bc[key])}건 | {after} |")
        after = f"{_num(fc['none'])}건" if fc is not None else "-"
        L.append(f"| {SEVERITY_NONE_KO} | {yellow:.1f} 미만 | {_num(bc['none'])}건 | {after} |")
    L.append("")
    L.append("결과 파일: " + _delivery_text(state, final))
    L.append("")


def _delivery_text(state: RemediationState, final: ScanResult | None) -> str:
    d = state.delivery
    if d is None:
        return "없음 — 원본은 `original/source.zip`에 그대로 있습니다."
    head = f"`{_md_cell(d.path)}` — "
    if not d.build_ok:
        return head + "⚠️ 빌드에 실패한 수정본, 재검사하지 않음"
    if not d.scanned:
        text = head + "빌드 성공, 재검사하지 않음"
    elif _delivery_matches(state, final):
        text = head + "빌드 성공, 재검사 완료"
    else:
        text = head + "빌드 성공, 재검사한 파일과 다름"
    if _earlier_delivery(state):
        text += f" (라운드 {d.round}에서 검증한 상태)"
    return text


def _failed_delivery_block(L: list[str], state: RemediationState, paths: RunPaths) -> None:
    d = state.delivery
    rs = next((x for x in state.rounds if x.round == d.round), None)
    ids = ", ".join(rs.applied) if rs and rs.applied else "-"
    blog = (rs.build.log if rs and rs.build and rs.build.log else f"rounds/{d.round}/build.log")
    L.append(">")
    L.append(f"> **주의:** 결과 ZIP(`{d.path}`)에는 라운드 {d.round}에서 빌드에 실패한 수정본(제안 {ids})이 들어 있고, "
             f"재검사는 하지 않았습니다. 빌드 로그는 `{blog}`에 있습니다. 이 ZIP을 그대로 배포하지 마세요.")
    L.append("> 원본은 `original/source.zip`에 그대로 보관합니다(내려받기 이름 source.zip).")
    kept = _verified_kept(paths)
    if kept:
        L.append(f"> 라운드 {kept['round']}에서 빌드와 재검사를 마친 결과 ZIP은 `{kept['path']}`에 있습니다(내려받기 이름 "
                 "result-verified.zip).")


def _build_line(b: Any) -> str:
    """빌드 한 번의 결과 문구를 만든다. 예: "성공 (212초)", "실패 (종료 코드 1, 30초, 시간 초과)"."""
    if b.ok:
        return f"성공 ({b.seconds:.0f}초)" if b.seconds else "성공"
    parts = [f"종료 코드 {b.exit_code}"] + ([f"{b.seconds:.0f}초"] if b.seconds else []) + (["시간 초과"] if b.timed_out else [])
    return f"실패 ({', '.join(parts)})"


def _code_block(L: list[str], text: str) -> None:
    L.append("")
    L.append("```text")
    L.extend(str(text).replace("```", "'''").splitlines())
    L.append("```")
    L.append("")


def _record_section(L: list[str], state: RemediationState, fx: _Facts, paths: RunPaths) -> None:
    base, final = fx.base, fx.final
    codes = _codes(state)
    L.append("## 처리 기록")
    L.append("")
    L.append("<details><summary>단계별 기록 (문의할 때 필요한 정보)</summary>")
    L.append("")
    L.append(f"- 작업 ID: `{state.id}`")
    L.append(f"- 상태 코드: `{state.status}`" + (" · 사유 코드: " + ", ".join(f"`{c}`" for c in codes) if codes else ""))
    L.append(f"- 진행한 라운드: {len(state.rounds)}회")
    if state.message and not {"baseline_build_failed", "candidate_build_failed"} & set(codes):
        # 빌드 실패 메시지는 Maven 빌드 요약이고 아래 빌드 단계에 그대로 나오므로 여기서는 쓰지 않는다
        _ko, raw = _known_and_raw(state.message)
        if raw:
            L.append(f"- 실행 메시지 원문: `{_md_cell(raw).replace('`', chr(39))}`")
    if state.counts:
        known = [k for k in COUNT_KO if k in state.counts]
        rest = sorted(k for k in state.counts if k not in COUNT_KO)
        L.append("")
        L.append("| 집계 | 값 |")
        L.append("|---|---|")
        for k in known:
            L.append(f"| {COUNT_KO[k]} | {_num(state.counts[k]) if isinstance(state.counts[k], int) else state.counts[k]} |")
        for k in rest:
            L.append(f"| {_md_cell(k)} | {_md_cell(state.counts[k])} |")
    L.append("")
    step = 1
    bb = state.baseline_build
    if bb is None:
        L.append(f"**{step}. 조치 전 빌드** — 실행하지 않음")
    else:
        L.append(f"**{step}. 조치 전 빌드** — {_build_line(bb)}" + (f" · 로그 `{bb.log}`" if bb.log else ""))
        if bb.summary and not bb.ok:
            _code_block(L, bb.summary)
    L.append("")
    step += 1
    if base is None:
        L.append(f"**{step}. 조치 전 검사 (IVAS)** — 검사하지 않음")
    else:
        head = (f"**{step}. 조치 전 검사 (IVAS)** — " + ("검사 완료" if base.complete else "검사 미완료")
                + f" · 취약점 {_num(len(fx.b_act))}건" + (f" (제외 {_num(len(fx.b_ign))}건 별도)" if fx.b_ign else "")
                + f" · 검사한 ZIP sha256 `{base.zip_sha256}`")
        L.append(head)
        if base.incomplete_reasons:
            L.append("- 미완료 사유: " + ", ".join(label(i18n.SCAN_REASON_KO, c) for c in base.incomplete_reasons))
        kinds = dict(sorted(base.counts.items()))
        if kinds and (len(kinds) > 1 or "library" not in kinds):
            L.append("- 종류별: " + ", ".join(f"{label(FINDING_KIND_KO, k)} {_num(v)}건" for k, v in kinds.items()))
    L.append("")
    for rs in sorted(state.rounds, key=lambda x: x.round):
        step += 1
        _round_block(L, state, fx, rs, step)
    step += 1
    _delivery_block(L, state, fx, paths, step)
    step += 1
    L.append(f"**{step}. 빌드 설정 점검** — " + ("이상 없음" if not state.boundary else f"{len(state.boundary)}건"))
    for b in state.boundary:
        act = "작업 중단" if b.action == "refuse" else "기록만 함"
        detail = ko_display(b.detail) if b.detail else ""
        L.append(f"- `{_md_cell(b.path)}` — {_md_cell(label(BOUNDARY_KO, b.code))}: {act}"
                 + (f" — {_md_cell(detail)}" if detail else ""))
    from .ivas import unknown_actions
    shown = final if final is not None else base
    unknown = unknown_actions(list(shown.findings)) if shown is not None else []
    if unknown:
        L.append("")
        shown_codes = ", ".join("빈 값" if c == "(empty)" else c for c in unknown)
        L.append(f"- 알 수 없는 IVAS 조치 상태 코드({shown_codes})는 조치 대상으로 처리했습니다.")
    L.append("")
    L.append("</details>")
    if final is not None and fx.f_act:
        table = ["| 위험도 | 취약점 | 라이브러리 | 버전 | IVAS 조치 상태 |", "|---|---|---|---|---|"]
        for f in sorted(fx.f_act, key=lambda x: (*fx.order(x.score, x.identifier, x.version), x.id, x.kind)):
            table.append(f"| {fx.sev_cell(f.score)} | {_md_cell(f.id)} | {_ident_cell(f.identifier, f.kind)} "
                         f"| {_md_cell(f.version)} | {_md_cell(_ivas_action(f.action))} |")
        _folded(L, table, f"남은 취약점 전체 목록 — {_num(len(fx.f_act))}건")
    L.append("")


def _shown_above(fx: _Facts) -> set[str]:
    return {p.id for p in fx.committed} | {p.id for p in fx.pending}


def _round_block(L: list[str], state: RemediationState, fx: _Facts, rs: RoundState, step: int) -> None:
    L.append(f"**{step}. 라운드 {rs.round}**")
    L.append("")
    plan = fx.plans.get(rs.round)
    above = _shown_above(fx)
    if plan is None or not plan.proposals:
        L.append("- 제안 없음")
    else:
        risky = any(p.risks for p in plan.proposals)
        cols = ["제안", "라이브러리", "버전 변경", "수정 방식", "처리", "버전 확인"] + (["주의"] if risky else [])
        L.append("| " + " | ".join(cols) + " |")
        L.append("|" + "---|" * len(cols))
        for p in plan.proposals:
            cells = [_md_cell(p.id), f"`{_md_cell(p.ga)}`", f"{_md_cell(p.from_version)} → {_md_cell(p.to_version)}",
                     _md_cell(label(KIND_KO, p.kind)), _md_cell(label(DECISION_KO, p.decision)),
                     _md_cell(evidence_ko(p.evidence))]
            if risky:
                cells.append(_md_cell(", ".join(label(RISK_KO, r) for r in sorted(p.risks))))
            L.append("| " + " | ".join(cells) + " |")
        L.append("")
        # 감사 기록이므로 모듈 목록을 줄이지 않는다
        for p in plan.proposals:
            if p.modules:
                L.append(f"- 적용 범위 ({_md_cell(p.id)}): {_md_cell(', '.join(p.modules))}")
                if p.also_modules:
                    L.append(f"- 적용되지 않을 수 있는 모듈 ({_md_cell(p.id)}): {_md_cell(', '.join(p.also_modules))}")
            if p.note and p.id not in above:
                L.append(f"- 참고 ({_md_cell(p.id)}): {_md_cell(' '.join(_note_lines(p.note)))}")
    if plan is not None:
        for n in plan.notes:
            L.append(f"- 계획 참고: {_md_cell(ko_display(n))}")
    planned = {p.id for p in plan.proposals} if plan is not None else set()
    if set(rs.applied) != {p.id for p in (plan.proposals if plan else []) if p.decision != "rejected"} or not planned:
        L.append(f"- 적용한 제안: {', '.join(rs.applied) if rs.applied else '없음'}")
    if rs.build is not None:
        L.append(f"- 수정본 빌드: {_build_line(rs.build)}" + (f" · 로그 `{rs.build.log}`" if rs.build.log else ""))
        if rs.build.summary and not rs.build.ok:
            _code_block(L, rs.build.summary)
    if rs.build is not None or rs.committed:
        L.append(f"- 수정본 채택: {'예' if rs.committed else '아니오'}")
    for v in rs.verify_problems:
        text = ko_display(v)
        if text.startswith("참고: "):
            L.append(f"- 검증 참고: {_md_cell(text[len('참고: '):])}")
        else:
            L.append(f"- 검증 문제: {_md_cell(text)}")
    if rs.scan_label:
        L.append(f"- 재검사: `{rs.scan_label}`")
    L.append("")


def _delivery_block(L: list[str], state: RemediationState, fx: _Facts, paths: RunPaths, step: int) -> None:
    d, final = state.delivery, fx.final
    if d is None:
        why = (" — 빌드에 실패한 수정본은 결과 ZIP으로 만들지 않았습니다. 원본은 그대로입니다."
               if state.status == FAILED else "")
        L.append(f"**{step}. 결과 파일** — 없음{why}")
    else:
        facts = [f"{_mb(d.size)}", f"파일 {_num(d.files)}개"] + ([f"라운드 {d.round}"] if len(state.rounds) > 1 else [])
        status = ["빌드 성공" if d.build_ok else "빌드 실패", "재검사 완료" if d.scanned else "재검사하지 않음"]
        if d.scanned:
            status.append("재검사한 파일과 일치" if _delivery_matches(state, final) else "재검사한 파일과 다름")
        L.append(f"**{step}. 결과 파일** — `{d.path}` ({', '.join(facts)}) · " + " · ".join(status))
        L.append(f"- sha256 `{d.sha256}`")
        kept = _verified_kept(paths)
        if kept:
            L.append(f"- 라운드 {kept['round']}에서 검증한 결과 ZIP: `{kept['path']}` (sha256 `{kept['sha256']}`)")
        if _earlier_delivery(state):
            L.append(f"- 이 ZIP은 라운드 {d.round}에서 빌드와 재검사를 마친 상태입니다. 라운드 {_last_round(state)}의 실패한 "
                     "변경은 들어 있지 않습니다(그 변경은 rounds/<n>/candidate.patch 에만 있습니다).")
    if final is not None:
        L.append(f"- 조치 후 검사: `{state.final_scan}` — " + ("검사 완료" if final.complete else "검사 미완료")
                 + f", {_num(len(fx.f_act))}건" + (f" (제외 {_num(len(fx.f_ign))}건 별도)" if fx.f_ign else ""))
        if final.incomplete_reasons:
            L.append("  - 미완료 사유: " + ", ".join(label(i18n.SCAN_REASON_KO, c) for c in final.incomplete_reasons))
        if _failed_delivery(state):
            L.append("  - 이 검사는 이전 라운드의 결과 ZIP을 검사한 것입니다. 빌드에 실패한 지금의 결과 ZIP은 검사하지 "
                     "않았습니다.")
    L.append("")


def _unchanged_section(L: list[str], state: RemediationState, fx: _Facts) -> None:
    L.append("## 변경하지 않은 것")
    L.append("")
    L.append("- 애플리케이션 소스와 프로젝트 구조(파일·폴더)는 변경하지 않았습니다.")
    if state.delivery is not None:
        extra = ("와 dependencyManagement에 추가한 버전 지정" if any(p.kind == K_MANAGED_OVERRIDE for p in fx.committed)
                 else "")
        L.append(f"- 결과 ZIP의 파일 목록은 원본과 같습니다. 달라진 것은 적용된 pom.xml의 버전 표기{extra}뿐입니다.")
    L.append("- 원본 ZIP은 `original/source.zip`에 그대로 보관합니다.")
    L.append("")


def build_report_md(state: RemediationState, paths: RunPaths, *, ignore_actions: Iterable[str] | None = None,
                    inhouse_message: str | None = None, severity: tuple[float, float, float] | None = None) -> str:
    """한국어 ``report.md`` 본문을 만든다. 절 순서는 모듈 docstring과 같고 빈 절은 쓰지 않는다.
    - 영어 문장은 ``i18n.ko_display``로 바꿔 보여 주고, 같은 설명은 묶음마다 한 번만 쓴다
    - ``severity``가 None이면 설정 기본값으로 🔴 / 🟠 / 🟡를 붙이고 점수가 높은 행부터 놓는다
    - ``ignore_actions``, ``inhouse_message``가 None이면 설정 기본값을 쓴다

    The Korean `report.md` text: the result, a summary and the actions first; codes only in the folded record."""
    ignore = _ignore(ignore_actions)
    roots = _roots(paths)
    message = inhouse_message if inhouse_message else _default_inhouse_message()
    fx = _Facts(state, paths, ignore, severity)
    L: list[str] = []

    # 1. 제목과 결과 블록
    L.append(f"# 취약점 조치 결과 — {state.name or state.id}")
    L.append("")
    for line in _result_lines(state, fx):
        L.append(f"> {_md_cell(line)}")
    if _failed_delivery(state):
        _failed_delivery_block(L, state, paths)
    L.append("")

    # 2. 요약
    _summary_section(L, state, fx, paths)

    # 3. 해야 할 일
    L.append("## 해야 할 일")
    L.append("")
    for i, s in enumerate(_next_steps(state, fx), 1):
        L.append(f"{i}. {s}")
    L.append("")

    # 4. 조치 완료
    if fx.committed:
        fixed = fx.fixed()
        n = fixed if fixed is not None else fx.matching(fx.committed)
        L.append(f"## 조치 완료 — {_num(n)}건")
        L.append("")
        _proposal_section(L, fx, fx.committed, pending=False)
        L.append("")

    # 5. 승인 대기
    if fx.pending:
        L.append(f"## 승인 대기 — 제안 {len(fx.pending)}건")
        L.append("")
        L.append("아래 변경은 승인해야 적용됩니다. 적용하면 빌드와 재검사를 이어서 진행합니다.")
        L.append("")
        _proposal_section(L, fx, fx.pending, pending=True)
        L.append("")

    # 6. 사내 라이브러리에서 발생한 취약점
    if fx.inhouse:
        _inhouse_section(L, fx, message)
        L.append("")

    # 7. 직접 확인이 필요한 취약점
    if fx.manual:
        _manual_section(L, fx)
        L.append("")

    # 8. 조치 대상에서 제외한 항목
    _excluded_section(L, state, fx, ignore)

    # 9. 변경하지 않은 것
    _unchanged_section(L, state, fx)

    # 10. 처리 기록(접힘)
    _record_section(L, state, fx, paths)
    return "\n".join(_clean(x, roots) for x in L)


def write_report(state: RemediationState, paths: RunPaths, *, ignore_actions: Iterable[str] | None = None,
                 inhouse_message: str | None = None, severity: tuple[float, float, float] | None = None) -> None:
    """``report.json``과 ``report.md``를 실행 폴더에 원자적으로 쓴다.
    ``ignore_actions``가 None이면 기본값(빈 목록)을 쓴다.

    Write `report.json` and `report.md` into the run directory."""
    write_json_atomic(paths.report_json, build_report_json(state, paths, ignore_actions=ignore_actions,
                                                           inhouse_message=inhouse_message, severity=severity))
    text = build_report_md(state, paths, ignore_actions=ignore_actions, inhouse_message=inhouse_message,
                           severity=severity)
    tmp = paths.report_md.with_name(paths.report_md.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(paths.report_md)


def summary(state: RemediationState) -> dict[str, Any]:
    """API 목록에 보여 줄 요약 dict를 만든다.

    A small dict for the API list."""
    d = state.delivery
    return {"id": state.id, "name": state.name, "status": state.status, "reason": state.reason,
            "reasons": list(state.reasons), "node": state.node, "next_node": state.next_node, "round": state.round,
            "created": state.created, "updated": state.updated, "counts": dict(sorted(state.counts.items())),
            "require_approval": state.require_approval,
            "delivery": {"sha256": d.sha256, "size": d.size, "round": d.round, "build_ok": d.build_ok,
                         "scanned": d.scanned} if d else None}


__all__ = ["build_report_json", "build_report_md", "summary", "write_report"]
