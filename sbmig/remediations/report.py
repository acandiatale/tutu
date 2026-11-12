"""취약점 조치 보고서 — ``report.json`` (기계용: 상태 전부 + 검사 요약 + 라운드별 계획 요약) 과 ``report.md`` (운영자용, 한국어).
흐름상 위치: 그래프의 마지막 노드 ``report`` 가 끝 상태(SUCCEEDED / INCOMPLETE / FAILED)에서만 ``write_report`` 를 부른다
(대기 상태에서는 쓰지 않는다). API 목록은 ``summary`` 의 작은 dict 를 쓴다.
지켜야 할 것: 비밀 값·토큰·서버 절대 경로를 쓰지 않는다(실행 폴더 기준 상대 경로만); 같은 입력이면 같은 글자(시각을 새로
찍지 않는다, 정렬 고정); 맨 앞에 결과 한 줄·한눈에 보기·다음에 할 일. report.md 는 사람용 한국어다: 조치 코드가 만든 영어 문장은
``i18n.ko_text`` 로 바꿔 보여 주고, 같은 설명의 잔여는 묶어서 설명을 한 번만 쓴다(report.json 은 영어 문장·코드 그대로 — 계약).
사내 아티팩트(사내 라이브러리·회사 부모 pom)를 거쳐 온 잔여(``inhouse_artifact``)는 아티팩트별로 따로 묶고
``remediation.inhouse_message`` 를 붙인다. 결과 ZIP 이 빌드가 실패한 수정본이면(``delivery.build_ok`` 거짓,
``remediation.failed_candidate: include``) 제목 바로 아래 첫 줄들에 그 사실·라운드·제안·빌드 로그·재검사 없음·원본 보존을 쓴다.
``remediation.ignore_actions`` 코드(기본은 없음; 예 PC 패치 완료 · IG 조치 예외)의 항목은 "조치 대상에서 뺀 항목" 표에 코드의 뜻(``ivas.ACTION_MEANINGS``)과
함께 따로 쓰고, 발견·잔여 개수에는 넣지 않는다.

Remediation report: `report.json` (the state, the scan summaries, per-round plan summaries) and `report.md` (Korean, for
operators: a result line, an overview and next steps first; English sentences shown translated, residuals grouped by
explanation, in-house residuals grouped per in-house artifact). Deterministic for the same input; no secret, no absolute server path.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from sbmig.core import scrub as _scrub

from . import i18n
from .i18n import (
    COUNT_KO,
    DECISION_KO,
    FINDING_KIND_KO,
    KIND_KO,
    ORIGIN_KO,
    REASON_KO,
    RISK_KO,
    evidence_ko,
    severity_of,
    severity_thresholds,
    ko_text,
    label,
)
from .models import (
    AWAITING_APPROVAL,
    AWAITING_CONFIRMATION,
    D_PENDING,
    E_INSERT,
    FAILED,
    INCOMPLETE,
    INTERRUPTED,
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
    "residual_findings": ("최종 검사에 취약점이 남았습니다.", "'사람이 확인할 것' 과 진행 기록의 최종 검사 표를 보고 담당 팀이 조치하세요."),
    "manual_review": ("자동으로 바꿀 수 없는 항목(프로젝트에 든 파일·빌드 플러그인·출처 불명·적용하지 못한 제안 등)이 있습니다.",
                      "'사람이 확인할 것' 의 항목을 사람이 확인하세요."),
    "no_candidate_version": ("승인된 저장소·정책에 쓸 수 있는 새 버전이 없는 항목이 있습니다.", "서버 정책(pins) 또는 저장소를 확인하세요."),
    "rounds_exhausted": ("반복 라운드 한도(max_rounds)에 닿았습니다.", "남은 항목을 사람이 확인하세요."),
    "stop_requested": ("중지 요청으로 멈췄습니다. 진행 중이던 원격 IVAS 분석은 IVAS 에서 계속 진행됩니다.",
                       "재개하면 멈춘 노드부터 이어서 합니다."),
    "internal_error": ("도구 내부 오류로 멈췄습니다.", "remediation.log 를 운영자에게 전달하세요."),
    R_INHOUSE_ARTIFACT: (("사내 아티팩트(사내 라이브러리·회사 부모 pom)를 거쳐 들어온 취약점이 있습니다. 그 아티팩트의 pom 은 "
                          "담당자만 고칠 수 있어 프로젝트에서는 바꾸지 않았습니다."),
                         "'사내 라이브러리에서 온 취약점' 의 아티팩트별 담당자(아키텍처 담당)에게 연락하세요."),
    "chain_budget": (("이관 빌드 파드에서 이어서 돈 조치가 시간 한도(remediation.chain_budget_minutes)를 다 써서 멈췄습니다. "
                      "진행 중이던 원격 IVAS 분석은 IVAS 에서 계속 진행됩니다."),
                     "재개하면 새 빌드 파드에서 멈춘 노드부터 이어서 합니다(검사 중이었다면 다시 올리지 않고 같은 분석을 이어서 조회)."),
}
# 정책 include 로 실패한 수정본을 결과 ZIP 으로 냈을 때의 candidate_build_failed 문구
_FAILED_INCLUDED_KO = (("버전을 바꾼 후보가 빌드되지 않았습니다. 결과 ZIP(delivery/result.zip)은 빌드가 실패한 그 수정본이며 IVAS "
                        "재검사는 하지 않았습니다. 원본은 original/source.zip 으로 그대로 보존됩니다."),
                       ("rounds/<n>/build.log 와 candidate.patch 를 보고 빌드를 고치거나 버전을 사람이 정하세요. 실패한 수정본을 "
                        "그대로 배포하지 마세요."))
_DECISION_KO = DECISION_KO          # 예전 이름(다른 곳이 import 해도 깨지지 않게)


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
                      ignore_actions: Iterable[str] | None = None,
                      inhouse_message: str | None = None,
                      severity: tuple[float, float, float] | None = None) -> dict[str, Any]:
    """``report.json`` 의 내용(dict): 상태 전부, 기준·최종 검사 요약(조치 대상 / 뺀 항목 개수), 라운드별 계획 요약, 전달 ZIP
    일치 여부, 조치 대상에서 뺀 항목(``ignored_findings``), 사내 아티팩트별 잔여(``inhouse``: 안내 문구 + 아티팩트마다의
    라이브러리). ``ignore_actions`` 가 None 이면 기본값(빈 목록), ``inhouse_message`` 가 None 이면 설정 기본 문구.
    한국어 번역은 넣지 않는다(도구가 읽는 계약).

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
    """발견 항목들의 위험도 수준별 개수(red / orange / yellow / none)."""
    out = {"red": 0, "orange": 0, "yellow": 0, "none": 0}
    for f in findings:
        out[severity_of(f.score, th)[0]] += 1
    return out


def _severity_json(base: ScanResult | None, final: ScanResult | None, ignore: frozenset[str],
                   th: tuple[float, float, float]) -> dict[str, Any]:
    """report.json 의 ``severity``: 경계와 기준·최종(완료일 때만) 검사의 조치 대상 수준별 개수. 표시·한국어는 넣지 않는다."""
    return {"thresholds": {"red": th[0], "orange": th[1], "yellow": th[2]},
            "baseline": _sev_counts(_split(base, ignore)[0], th) if base is not None else None,
            "final": _sev_counts(_split(final, ignore)[0], th) if final is not None and final.complete else None}


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


def _codes(state: RemediationState) -> list[str]:
    return list(state.reasons or ([state.reason] if state.reason else []))


def _next_steps(state: RemediationState) -> list[str]:
    inhouse = [r for r in state.residuals if r.reason == R_INHOUSE_ARTIFACT]
    if state.status == SUCCEEDED and not inhouse:
        return ["전달 ZIP(delivery/result.zip)을 받아 쓰면 됩니다. 최종 검사는 이 ZIP 과 같은 바이트로 했습니다."]
    codes = [c for c in _codes(state) if c in _REASON_KO]
    # 운영자가 먼저 할 일부터: 승인 → 사내 아티팩트 담당자 연락 → 사람 검토 → 나머지(이유 코드 순서)
    out: list[str] = []
    if "pending_approval" in codes:
        out.append(_reason_text(state, "pending_approval")[1])
    if inhouse or R_INHOUSE_ARTIFACT in codes:
        # 왜: 사내 아티팩트 항목은 evaluate 의 이유 코드에 없어도(다른 이유가 앞설 때) 담당자 연락이 늘 할 일이다
        out.append(_REASON_KO[R_INHOUSE_ARTIFACT][1])
    review = [c for c in ("manual_review", "residual_findings") if c in codes]
    if len(review) == 2:
        # 두 이유의 할 일은 같은 표를 본다 — 한 줄로 합친다
        out.append(_REVIEW_BOTH)
    elif review:
        out.append(_reason_text(state, review[0])[1])
    out += [_reason_text(state, c)[1] for c in codes
            if c not in ("pending_approval", R_INHOUSE_ARTIFACT, "manual_review", "residual_findings")]
    if state.status == SUCCEEDED:
        out.insert(0, "전달 ZIP(delivery/result.zip)을 받아 쓰면 됩니다. 최종 검사는 이 ZIP 과 같은 바이트로 했습니다.")
    return list(dict.fromkeys(out)) or ["보고서와 remediation.log 를 확인하세요."]


_REVIEW_BOTH = ("'사람이 확인할 것' 의 항목을 사람이 확인하고, 진행 기록의 최종 검사 표에 남은 취약점은 담당 팀이 "
                "조치하세요.")


def _default_inhouse_message() -> str:
    from sbmig.core.config import RemediationConfig
    return RemediationConfig().inhouse_message


def _inhouse_of(r: Residual) -> list[str]:
    """사내 아티팩트 잔여를 묶을 g:a 들: 잔여의 ``inhouse`` → (예전 상태 파일) 설명 문장의 g:a → 자기 g:a."""
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
    """긴 목록은 앞의 몇 개 + "외 N개"(같은 입력이면 같은 글자)."""
    xs = [str(x) for x in items if str(x)]
    if not xs:
        return "-"
    if len(xs) > limit:
        return ", ".join(xs[:show]) + f" 외 {len(xs) - show}{unit}"
    return ", ".join(xs)


def _mods(mods: Iterable[str]) -> str:
    return _cut(sorted(set(mods)))


def _key(f: Any) -> tuple[str, str]:
    return (f.identifier, f.version)


class _Facts:
    """report.md 가 여러 절에서 같이 쓰는 사실들(한 번만 계산한다)."""

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
        # 취약점 ID 의 점수: 완료된 최종 검사 먼저, 그다음 기준 검사(고친 취약점은 최종 검사에 없다). 같은 ID 가 여러
        # 라이브러리에 보고됐으면 (ID, 대상, 버전)이 맞는 것, 아니면 그 ID 의 가장 높은 점수
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

    def matching(self, residuals: list[Residual]) -> int:
        """이 잔여들에 해당하는 기준 검사의 조치 대상 발견 항목 수(기준 검사가 없으면 잔여의 취약점 ID 수)."""
        keys = {(r.ga, r.version) for r in residuals}
        if self.base is not None:
            return sum(1 for f in self.b_act if _key(f) in keys)
        return sum(len(set(r.finding_ids)) for r in residuals)

    def fixed(self) -> int | None:
        """기준 검사의 조치 대상 중 최종 검사(완료)에 없는 것의 수. 최종 검사가 없거나 미완료면 None(모른다)."""
        if self.base is None or not self.final_ok:
            return None
        left = {f.key for f in self.f_act}
        return sum(1 for f in self.b_act if f.key not in left)

    def cve_score(self, cve: str, ga: str, version: str) -> float | None:
        """취약점 하나의 점수(검사에 없으면 None)."""
        for exact in self._exact:
            if (cve, ga, version) in exact:
                return exact[(cve, ga, version)]
        for by_id in self._by_id:
            if cve in by_id:
                return by_id[cve]
        return None

    def row_score(self, ids: Iterable[str], ga: str, version: str) -> float | None:
        """행(라이브러리 하나)의 가장 높은 점수."""
        got = [x for x in (self.cve_score(i, ga, version) for i in set(ids)) if x is not None]
        return max(got) if got else None

    def sev_cell(self, score: float | None) -> str:
        """위험도 칸: "🔴 9.8" (표시 없는 점수는 숫자만, 점수가 없으면 "-")."""
        if score is None:
            return "-"
        mark = severity_of(score, self.th)[1]
        return f"{mark} {score:.1f}" if mark else f"{score:.1f}"

    def cve_cell(self, ga: str, version: str, ids: Iterable[str]) -> str:
        """취약점 칸: 점수가 높은 것부터 표시·ID·점수, 셋까지 + "외 N건"."""
        scored = [(i, self.cve_score(i, ga, version)) for i in sorted(set(ids))]
        if not scored:
            return "-"
        scored.sort(key=lambda x: (x[1] is None, -(x[1] or 0.0), x[0]))
        parts = []
        for cve, sc in scored[:3]:
            mark = severity_of(sc, self.th)[1]
            parts.append(" ".join(x for x in (mark, cve, f"{sc:.1f}" if sc is not None else "") if x))
        return ", ".join(parts) + (f" 외 {len(scored) - 3}건" if len(scored) > 3 else "")

    def order(self, score: float | None, ga: str, version: str) -> tuple:
        """행 순서: 점수 높은 것 먼저(점수 없음은 맨 뒤), 그다음 g:a, 버전."""
        return (score is None, -(score or 0.0), ga, version)

    def breakdown(self, findings: Iterable[Any]) -> str:
        """위험도별 개수 글: "🔴 심각 3건 · 🟠 높음 5건 · 표시 없음 1건"(0 인 수준은 뺀다)."""
        c = _sev_counts(findings, self.th)
        parts = [f"{mark} {name} {c[key]}건" for key, mark, name in i18n.SEVERITY_LEVELS if c[key]]
        if c["none"]:
            parts.append(f"표시 없음 {c['none']}건")
        return " · ".join(parts) or "0건"


def _result_sentence(state: RemediationState, fx: _Facts) -> str:
    codes = _codes(state)
    if state.status == FAILED or fx.base is None or not fx.base.complete:
        if codes and codes[0] in _REASON_KO:
            return _reason_text(state, codes[0])[0]
        return ko_text(state.message) or _STATUS_KO.get(state.status, state.status)
    total = len(fx.b_act)
    if total == 0:
        return "기준 검사에서 조치할 취약점이 없었습니다."
    fixed = fx.fixed()
    head = f"취약점 {total}건 중 {fixed}건을 고쳤" if fixed is not None else f"취약점 {total}건을 찾았"
    hand = []
    n_in, n_man = fx.matching(fx.inhouse), fx.matching(fx.manual)
    if n_in:
        hand.append(f"{n_in}건은 사내 라이브러리 담당자에게")
    if n_man:
        hand.append(f"{n_man}건은 사람 검토로")
    text = head + ("고, " + ", ".join(hand) + " 넘깁니다." if hand else "습니다.")
    if fx.pending:
        text += f" 승인을 기다리는 제안이 {len(fx.pending)}건 있습니다."
    return text


def _folded(L: list[str], table: list[str], summary: str) -> None:
    """표를 그대로 쓰거나(``summary`` 가 빈 글자), 접어 둔다(``<details>``). 미리보기가 표를 그리도록 앞뒤에 빈 줄을 둔다."""
    L.append("")
    if summary:
        L.append(f"<details><summary>{summary}</summary>")
        L.append("")
    L.extend(table)
    if summary:
        L.append("")
        L.append("</details>")


def _counts_text(counts: dict[str, int]) -> str:
    """집계 dict → "라운드 1, 남긴 항목 12, …"(아는 키는 정해진 순서, 모르는 키는 이름 그대로 뒤에 정렬)."""
    known = [k for k in COUNT_KO if k in counts]
    rest = sorted(k for k in counts if k not in COUNT_KO)
    return ", ".join([f"{COUNT_KO[k]} {counts[k]}" for k in known] + [f"{k} {counts[k]}" for k in rest])


def _mark(state: RemediationState) -> str:
    return {SUCCEEDED: "✅", FAILED: "❌"}.get(state.status, "⚠️")


def _place(p: Proposal) -> str:
    """제안이 바꾼 곳: 한국어 종류 + pom:줄(넣는 항목은 "줄 앞에 추가")."""
    where = []
    for e in p.edits:
        where.append(f"{e.pom}:{e.line} 앞에 추가" if e.op == E_INSERT else f"{e.pom}:{e.line}")
    return label(KIND_KO, p.kind) + (" · " + ", ".join(where) if where else "")


def _proposal_rows(L: list[str], fx: _Facts, props: list[Proposal], *, risks: bool) -> None:
    head = ("| 위험도 | 라이브러리 | 버전(전 → 후) | 바꾼 곳 | 근거 |" + (" 위험 부류 |" if risks else "")
            + " 해소 대상 취약점 |")
    L.append(head)
    L.append("|" + "---|" * (7 if risks else 6))
    scores = {p.id: fx.row_score(p.finding_ids, p.ga, p.from_version) for p in props}
    props = sorted(props, key=lambda p: (*fx.order(scores[p.id], p.ga, p.from_version), p.id))
    for p in props:
        cells = [fx.sev_cell(scores[p.id]), f"`{_md_cell(p.ga)}`", f"{_md_cell(p.from_version)} → {_md_cell(p.to_version)}", _md_cell(_place(p)),
                 _md_cell(evidence_ko(p.evidence))]
        if risks:
            cells.append(_md_cell(", ".join(label(RISK_KO, r) for r in sorted(p.risks)) or "-"))
        cells.append(_md_cell(fx.cve_cell(p.ga, p.from_version, p.finding_ids)))
        L.append("| " + " | ".join(cells) + " |")
    details = [p for p in props if p.note or p.modules]
    if details:
        L.append("")
        for p in details:
            L.append(f"- `{_md_cell(p.id)}` `{_md_cell(p.ga)}`")
            if p.note:
                L.append(f"  - 메모: {_md_cell(ko_text(p.note))}")
            if p.modules:
                L.append(f"  - 다스리는 모듈: {_md_cell(_mods(p.modules))}")
                if p.also_modules:
                    L.append("  - 이 수정이 닿지 않을 수 있는 모듈(같은 버전이 다른 길로 들어옴): "
                             f"{_md_cell(_mods(p.also_modules))}")


def _chain(via: list[str]) -> str:
    """의존성 사슬 a → b → c. 길면(셋 이상) artifactId:version 만."""
    if len(via) > 2:
        return " → ".join(":".join(x.split(":")[1:]) if x.count(":") >= 2 else x for x in via)
    return " → ".join(via)


def _came(r: Residual, owner: str) -> str:
    """사내 아티팩트 표의 '들어온 길': 사슬이 있으면 사슬, 없으면 출처 이름표(부모 pom 이 관리 등)."""
    if r.via:
        return _chain(r.via)
    if r.ga == owner:
        return "사내 아티팩트 자체"
    return label(ORIGIN_KO, r.origin)


def _inhouse_section(L: list[str], fx: _Facts, message: str) -> None:
    L.append("## 사내 라이브러리에서 온 취약점")
    L.append("")
    L.append(f"> {message}")
    L.append("")
    L.append("아래 라이브러리는 사내 라이브러리를 거쳐 들어오거나 회사 부모 pom 에서 버전이 정해집니다. 그 아티팩트의 pom 은 "
             "담당자(아키텍처 담당)만 고칠 수 있어 이 프로젝트에서는 바꾸지 않았습니다(이유 코드 "
             f"`{R_INHOUSE_ARTIFACT}`).")
    groups: dict[str, list[Residual]] = {}
    for r in fx.inhouse:
        for ga in _inhouse_of(r):
            groups.setdefault(ga, []).append(r)
    for ga in sorted(groups, key=lambda g: (-len(groups[g]), g)):
        rows = sorted(groups[ga], key=lambda r: fx.order(fx.row_score(r.finding_ids, r.ga, r.version), r.ga, r.version))
        L.append("")
        L.append(f"### `{_md_cell(ga)}` — {len(rows)}개")
        L.append("")
        L.append("| 위험도 | 취약 라이브러리 | 버전 | 들어온 길 | 모듈 | 취약점 |")
        L.append("|---|---|---|---|---|---|")
        for r in rows:
            L.append(f"| {fx.sev_cell(fx.row_score(r.finding_ids, r.ga, r.version))} | `{_md_cell(r.ga)}` | {_md_cell(r.version)} | {_md_cell(_came(r, ga))} | {_md_cell(_mods(r.modules))} "
                     f"| {_md_cell(fx.cve_cell(r.ga, r.version, r.finding_ids))} |")


def _people_section(L: list[str], fx: _Facts) -> None:
    L.append("## 사람이 확인할 것")
    if not fx.manual:
        L.append("")
        L.append("- 없음")
        return
    groups: dict[tuple[str, str], list[Residual]] = {}
    texts: dict[tuple[str, str], str] = {}
    for r in fx.manual:
        key, text = i18n.detail_group(r.detail)
        groups.setdefault((r.reason, key), []).append(r)
        texts[(r.reason, key)] = text
    per_reason: dict[str, int] = {}
    for reason, _k in groups:
        per_reason[reason] = per_reason.get(reason, 0) + 1
    seen_reason: set[str] = set()
    unknown_n = 0
    for gk in sorted(groups, key=lambda k: (-len(groups[k]), k[0], k[1])):
        reason, key = gk
        rows = sorted(groups[gk], key=lambda r: (*fx.order(fx.row_score(r.finding_ids, r.ga, r.version), r.ga, r.version),
                                                 r.origin))
        title, memo = i18n.group_meta(key)
        short = label(REASON_KO, reason)
        if per_reason[reason] > 1:
            if not title:
                unknown_n += 1
                title = f"그 밖의 사유 {unknown_n}"
            head = f"### {short} · {title} — {len(rows)}개"
        else:
            head = f"### {short} — {len(rows)}개"
        L.append("")
        L.append(head)
        L.append("")
        expl = texts[gk] or _md_cell("-")
        if reason not in seen_reason:
            expl += f" (이유 코드 `{reason}`)"
            seen_reason.add(reason)
        L.append(_md_cell(expl))
        # 모든 행이 같은 값인 칸은 표에서 빼고 한 줄로 올린다(같은 글자를 행마다 되풀이하지 않는다)
        origins = {label(ORIGIN_KO, r.origin) for r in rows}
        modules = {_mods(r.modules) for r in rows}
        lift_origin = len(rows) > 1 and len(origins) == 1
        lift_mods = len(rows) > 1 and len(modules) == 1
        memos = [ko_text(r.detail) or "-" for r in rows]
        memo_col = memo and len(set(memos)) > 1
        common = []
        if lift_origin:
            common.append(f"출처: {next(iter(origins))}")
        if lift_mods:
            common.append(f"모듈: {next(iter(modules))}")
        if memo and not memo_col and memos[0] != "-" and len(rows) > 1:
            common.append(f"내용: {memos[0]}")
        if common:
            L.append("")
            for c in common:
                L.append(f"- 모든 항목 공통 — {_md_cell(c)}")
        elif memo and len(rows) == 1 and memos[0] != "-":
            # 한 행뿐인 묶음은 '공통' 이라 하지 않는다 — 그 행의 내용으로 쓴다
            L.append("")
            L.append(f"- 내용: {_md_cell(memos[0])}")
        cols = ["위험도", "라이브러리", "버전"] + ([] if lift_origin else ["출처"]) + ([] if lift_mods else ["모듈"]) + ["취약점"]
        cols += ["메모"] if memo_col else []
        L.append("")
        L.append("| " + " | ".join(cols) + " |")
        L.append("|" + "---|" * len(cols))
        for r, m in zip(rows, memos):
            lib = f"`{_md_cell(r.ga)}`"
            if r.inhouse:
                lib += " · 일부 모듈은 사내 라이브러리 경유"
            cells = [fx.sev_cell(fx.row_score(r.finding_ids, r.ga, r.version)), lib, _md_cell(r.version)]
            if not lift_origin:
                cells.append(_md_cell(label(ORIGIN_KO, r.origin)))
            if not lift_mods:
                cells.append(_md_cell(_mods(r.modules)))
            cells.append(_md_cell(fx.cve_cell(r.ga, r.version, r.finding_ids)))
            if memo_col:
                cells.append(_md_cell(m))
            L.append("| " + " | ".join(cells) + " |")


def _round_block(L: list[str], state: RemediationState, fx: _Facts, rs: RoundState) -> None:
    L.append(f"### 라운드 {rs.round}")
    L.append("")
    plan = fx.plans.get(rs.round)
    if plan is None or not plan.proposals:
        L.append("- 제안 없음")
    else:
        L.append("| 제안 | 라이브러리 | 버전 | 종류 | 위험 | 결정 | 근거 |")
        L.append("|---|---|---|---|---|---|---|")
        for p in plan.proposals:
            L.append(f"| {_md_cell(p.id)} | `{_md_cell(p.ga)}` | {_md_cell(p.from_version)} → {_md_cell(p.to_version)} "
                     f"| {_md_cell(label(KIND_KO, p.kind))} "
                     f"| {_md_cell(', '.join(label(RISK_KO, r) for r in sorted(p.risks)))} "
                     f"| {_md_cell(label(DECISION_KO, p.decision))} | {_md_cell(evidence_ko(p.evidence))} |")
        L.append("")
        # 감사 기록이므로 모듈 목록을 줄이지 않는다
        for p in plan.proposals:
            if p.note:
                L.append(f"- 메모 `{_md_cell(p.id)}`: {_md_cell(ko_text(p.note))}")
            if p.modules:
                L.append(f"- 다스리는 모듈 `{_md_cell(p.id)}`: {_md_cell(', '.join(p.modules))}")
                if p.also_modules:
                    L.append(f"- 이 수정이 닿지 않을 수 있는 모듈 `{_md_cell(p.id)}`: {_md_cell(', '.join(p.also_modules))}")
    if plan is not None:
        for n in plan.notes:
            L.append(f"- 계획 메모: {_md_cell(ko_text(n))}")
    L.append(f"- 적용한 제안: {', '.join(rs.applied) if rs.applied else '없음'}")
    if rs.build is not None:
        L.append(f"- 후보 빌드: {'성공' if rs.build.ok else '실패'}" + (f" (로그 `{rs.build.log}`)" if rs.build.log else ""))
        if rs.build.summary and not rs.build.ok:
            L.append(f"  - 요약: {rs.build.summary}")
    L.append(f"- 확정(accepted 로 반영): {'예' if rs.committed else '아니오'}")
    if rs.verify_problems:
        L.append("- 검증 메모:")
        for v in rs.verify_problems:
            L.append(f"  - {ko_text(v)}")
    if rs.scan_label:
        L.append(f"- 재검사: `{rs.scan_label}`")
    L.append("")


def build_report_md(state: RemediationState, paths: RunPaths, *, ignore_actions: Iterable[str] | None = None,
                    inhouse_message: str | None = None, severity: tuple[float, float, float] | None = None) -> str:
    """``report.md`` 의 글(한국어, 훑어보기 좋게). 순서: 제목 + 결과 한 줄(+ 실패한 수정본 경고) → 한눈에 보기 → 다음에 할 일
    → 고친 것 → 승인을 기다리는 것 → 사내 라이브러리에서 온 취약점 → 사람이 확인할 것 → 조치 대상에서 뺀 항목 → 진행 기록 →
    바꾸지 않은 것. 영어 문장은 ``i18n.ko_text`` 로 바꿔 보여 주고(저장된 자료는 그대로), 같은 설명은 묶음마다 한 번만 쓴다.
    ``ignore_actions`` 가 None 이면 기본값(빈 목록), ``inhouse_message`` 가 None 이면 설정 기본 문구, ``severity`` (빨강·주황·
    노랑의 아래 경계)가 None 이면 설정 기본값 — 취약점 옆에 🔴 / 🟠 / 🟡 를 붙이고 행을 점수 높은 것부터 놓는다.

    The Korean `report.md` text, built to be scanned; English sentences are shown translated, grouped explanations once."""
    ignore = _ignore(ignore_actions)
    roots = _roots(paths)
    message = inhouse_message if inhouse_message else _default_inhouse_message()
    fx = _Facts(state, paths, ignore, severity)
    base, final = fx.base, fx.final
    codes = _codes(state)
    L: list[str] = []

    # 1. 제목과 결과 한 줄
    L.append(f"# 취약점 조치 보고서 — {state.name or state.id}")
    L.append("")
    L.append(f"> {_mark(state)} **{_STATUS_KO.get(state.status, state.status)}** (`{state.status}`) — "
             f"{_md_cell(_result_sentence(state, fx))}")
    if _failed_delivery(state):
        d = state.delivery
        rs = next((x for x in state.rounds if x.round == d.round), None)
        ids = ", ".join(rs.applied) if rs and rs.applied else "-"
        blog = (rs.build.log if rs and rs.build and rs.build.log else f"rounds/{d.round}/build.log")
        L.append("")
        L.append(f"> **주의: 결과 ZIP(`{d.path}`)은 빌드가 실패한 수정본입니다.** 라운드 {d.round} 의 후보 빌드가 실패했고, "
                 f"그 수정본(적용한 제안: {ids})을 그대로 담았습니다. 빌드 로그: `{blog}`.")
        L.append("> 이 ZIP 은 IVAS 재검사를 하지 않았습니다. 올린 원본은 `original/source.zip` (내려받기 이름 source.zip)으로 "
                 "그대로 보존됩니다.")
        kept = _verified_kept(paths)
        if kept:
            L.append(f"> 앞 라운드 {kept['round']} 에서 빌드·재검사를 마친 ZIP 은 `{kept['path']}` "
                     "(내려받기 이름 result-verified.zip)으로 남아 있습니다.")
    L.append("")

    # 2. 한눈에 보기 — 모든 줄을 늘 쓴다(없으면 0)
    L.append("## 한눈에 보기")
    L.append("")
    L.append("| 항목 | 내용 |")
    L.append("|---|---|")
    if base is None:
        found = "검사하지 않음"
    else:
        found = (f"{len(fx.b_act)}건 (라이브러리 {len({_key(f) for f in fx.b_act})}개)"
                 + ("" if base.complete else " — 검사 미완료"))
    L.append(f"| 기준 검사에서 찾은 취약점 | {found} |")
    if base is not None:
        L.append(f"| 위험도별 | {fx.breakdown(fx.b_act)} |")
    fixed = fx.fixed()
    fixed_ids = {i for p in fx.committed for i in p.finding_ids}
    L.append(f"| 고친 것 | 라이브러리 {len({p.ga for p in fx.committed})}개 "
             f"(취약점 {fixed if fixed is not None else len(fixed_ids)}건) |")
    L.append(f"| 승인을 기다리는 것 | 제안 {len(fx.pending)}건 |")
    L.append(f"| 사내 라이브러리 경유(예외 처리 가능) | 라이브러리 {len(fx.inhouse)}개 (취약점 {fx.matching(fx.inhouse)}건) |")
    L.append(f"| 사람이 확인할 것 | 라이브러리 {len(fx.manual)}개 (취약점 {fx.matching(fx.manual)}건) |")
    if final is None:
        left = "최종 검사 없음"
    elif not final.complete:
        left = "최종 검사 미완료(0건이 아닙니다)"
    else:
        left = f"{len(fx.f_act)}건" + (f" ({fx.breakdown(fx.f_act)})" if fx.f_act else "")
    L.append(f"| 최종 검사에 남은 것 | {left} |")
    d = state.delivery
    if d is None:
        zip_text = "❌ 없음"
    elif not d.build_ok:
        zip_text = "⚠️ 있음 — 빌드가 실패한 수정본, 재검사 안 함"
    elif d.scanned and _delivery_matches(state, final):
        zip_text = "✅ 있음 — 빌드 성공, 재검사함"
    else:
        zip_text = "⚠️ 있음 — 빌드 성공, " + ("재검사함" if d.scanned else "재검사 안 함")
    L.append(f"| 결과 ZIP | {zip_text} |")
    L.append("")
    red, orange, yellow = fx.th
    L.append(f"위험도 표시: 🔴 {red:.1f} 이상 · 🟠 {orange:.1f} 이상 · 🟡 {yellow:.1f} 이상 (IVAS 점수 기준, 그보다 낮거나 점수가 "
             "없으면 표시 없음)")
    L.append("")

    # 3. 다음에 할 일
    L.append("## 다음에 할 일")
    L.append("")
    for i, s in enumerate(_next_steps(state), 1):
        L.append(f"{i}. {s}")
    if codes or state.message:
        L.append("")
        L.append("이 상태가 된 이유:")
        L.append("")
        for c in codes:
            meaning = _reason_text(state, c)[0] if c in _REASON_KO else ""
            L.append(f"- {label(REASON_KO, c)} (`{c}`)" + (f": {meaning}" if meaning else ""))
        if state.message:
            L.append(f"- 실행 메시지: {ko_text(state.message)}")
    L.append("")

    # 4. 고친 것
    L.append("## 고친 것")
    L.append("")
    if fx.committed:
        _proposal_rows(L, fx, fx.committed, risks=False)
    else:
        L.append("- 없음 (확정한 변경이 없습니다)")
    L.append("")

    # 5. 승인을 기다리는 것
    if fx.pending:
        L.append("## 승인을 기다리는 것")
        L.append("")
        L.append("승인은 조치 화면의 체크박스 목록에서 합니다(선택한 항목 적용). 승인하면 이어서 진행합니다.")
        L.append("")
        _proposal_rows(L, fx, fx.pending, risks=True)
        L.append("")

    # 6. 사내 라이브러리에서 온 취약점
    if fx.inhouse:
        _inhouse_section(L, fx, message)
        L.append("")

    # 7. 사람이 확인할 것
    _people_section(L, fx)
    L.append("")

    # 8. 조치 대상에서 뺀 항목
    from .ivas import unknown_actions
    shown = final if final is not None else base
    L.append("## 조치 대상에서 뺀 항목")
    L.append("")
    L.append(f"- 뺀 조치 코드(`remediation.ignore_actions`): {', '.join(sorted(ignore)) or '없음'} — 이 항목들은 검사 파일과 "
             "이 표에 남지만 계획하지 않고 남은 취약점으로 세지 않습니다.")
    rows = _ignored_rows(shown, ignore)
    if not rows:
        L.append("- 뺀 항목 없음")
    else:
        L.append(f"- 기준: `{state.final_scan if final is not None else state.baseline_scan}`")
        table = ["| 위험도 | 취약점 | 종류 | 대상 | 버전 | 조치 코드 | 뜻 |", "|---|---|---|---|---|---|---|"]
        ign_scores: dict[tuple[str, str, str, str], float] = {}
        for f in _split(shown, ignore)[1]:
            if f.score is not None:
                k = (f.id, f.kind, f.identifier, f.version)
                ign_scores[k] = max(ign_scores.get(k, f.score), f.score)
        keyed = [(ign_scores.get((r["id"], r["kind"], r["identifier"], r["version"])), r) for r in rows]
        keyed.sort(key=lambda x: (*fx.order(x[0], x[1]["identifier"], x[1]["version"]), x[1]["id"], x[1]["kind"]))
        for sc, r in keyed:
            table.append(f"| {fx.sev_cell(sc)} | {_md_cell(r['id'])} | {_md_cell(label(FINDING_KIND_KO, r['kind']))} "
                         f"| `{_md_cell(r['identifier'])}` | {_md_cell(r['version'])} | {_md_cell(r['action'])} "
                         f"| {_md_cell(r['meaning'])} |")
        _folded(L, table, f"조치 대상에서 뺀 항목 전체 — {len(rows)}건" if len(rows) > 10 else "")
    unknown = unknown_actions(list(shown.findings)) if shown is not None else []
    if unknown:
        L.append(f"- 알 수 없는 조치 코드(조치 대상으로 다룸): {', '.join(unknown)}")
    L.append("")

    # 9. 진행 기록
    L.append("## 진행 기록")
    L.append("")
    L.append(f"- 작업 ID: `{state.id}` · 라운드: {len(state.rounds)}")
    if state.counts:
        L.append("- 집계: " + _counts_text(state.counts))
    L.append("")
    L.append("### 기준 빌드")
    L.append("")
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
    L.append("### 기준 검사 (IVAS)")
    L.append("")
    if base is None:
        L.append("- 검사하지 않았습니다(또는 결과가 없습니다).")
    else:
        L.append(f"- 완료: {'예' if base.complete else '아니오 — 미완료 검사는 0건이 아닙니다'}")
        L.append(f"- 발견 항목: {len(base.findings)}건 (조치 대상 {len(fx.b_act)}건, 조치 대상에서 뺀 항목 {len(fx.b_ign)}건) · "
                 f"검사한 ZIP sha256: `{base.zip_sha256}`")
        if base.incomplete_reasons:
            L.append("- 미완료 이유: " + ", ".join(base.incomplete_reasons))
        if base.counts:
            L.append("- 범주별: " + ", ".join(f"{label(FINDING_KIND_KO, k)} {v}건" for k, v in sorted(base.counts.items())))
    L.append("")
    for rs in state.rounds:
        _round_block(L, state, fx, rs)

    L.append("### 전달 ZIP")
    L.append("")
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
        L.append(f"- 최종 검사: `{state.final_scan}` · 완료 {'예' if final.complete else '아니오'} · 발견 {len(final.findings)}건"
                 f" (조치 대상 {len(fx.f_act)}건, 조치 대상에서 뺀 항목 {len(fx.f_ign)}건)")
        if _failed_delivery(state):
            L.append("  - 이 검사는 앞 라운드의 ZIP 을 검사한 것이며, 빌드가 실패한 지금의 결과 ZIP 을 검사한 것이 아닙니다.")
        if fx.f_act:
            table = ["| 위험도 | 취약점 | 종류 | 대상 | 버전 | 조치 코드 |", "|---|---|---|---|---|---|"]
            for f in sorted(fx.f_act, key=lambda x: (*fx.order(x.score, x.identifier, x.version), x.id, x.kind)):
                table.append(f"| {fx.sev_cell(f.score)} | {_md_cell(f.id)} | {_md_cell(label(FINDING_KIND_KO, f.kind))} | `{_md_cell(f.identifier)}` "
                             f"| {_md_cell(f.version)} | {_md_cell(f.action)} |")
            _folded(L, table, f"남은 취약점 전체 목록 — {len(fx.f_act)}건")
    L.append("")
    L.append("### 실행 경계 점검")
    L.append("")
    if not state.boundary:
        L.append("- 발견 없음")
    else:
        for b in state.boundary:
            act = "거부" if b.action == "refuse" else "기록만"
            L.append(f"- `{b.code}` `{b.path}` → {act}" + (f" — {ko_text(b.detail)}" if b.detail else ""))
    L.append("")

    # 10. 바꾸지 않은 것
    L.append("## 바꾸지 않은 것")
    L.append("")
    L.append("- 애플리케이션 소스, 프로젝트 구조(파일 목록·폴더)는 바꾸지 않았습니다. 전달 ZIP 의 파일 집합은 원본과 같고,")
    L.append("  다른 것은 승인·적용된 pom.xml 안의 버전 글자(와 승인된 관리 버전 항목)뿐입니다.")
    L.append("- 원본 ZIP(`original/source.zip`)은 그대로 보존합니다.")
    L.append("")
    return "\n".join(_clean(x, roots) for x in L)


def write_report(state: RemediationState, paths: RunPaths, *, ignore_actions: Iterable[str] | None = None,
                 inhouse_message: str | None = None, severity: tuple[float, float, float] | None = None) -> None:
    """``report.json`` 과 ``report.md`` 를 실행 폴더에 쓴다(원자적 교체). 같은 입력이면 같은 내용. ``ignore_actions`` 는
    그래프가 설정 값을 넘긴다(None = 기본값, 빈 목록).

    Write `report.json` and `report.md` into the run directory."""
    write_json_atomic(paths.report_json, build_report_json(state, paths, ignore_actions=ignore_actions,
                                                           inhouse_message=inhouse_message, severity=severity))
    text = build_report_md(state, paths, ignore_actions=ignore_actions, inhouse_message=inhouse_message,
                           severity=severity)
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
