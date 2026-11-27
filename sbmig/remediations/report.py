"""취약점 조치 결과를 ``report.json``과 담당자가 읽는 한국어 ``report.md``로 만드는 모듈.

The graph's ``report`` node calls ``write_report`` on a final state; the API list uses ``summary``. No token
is written either, and paths are relative to the run folder. No current time goes in and the sort order is
fixed. report.md shows stored English sentences through ``i18n.ko_display``. ``ignore_actions`` items are not
counted as vulnerabilities.

Remediation report: `report.json` (the state, the scan summaries, per-round plan summaries; English sentences
and codes as stored) and `report.md` (Korean, for the project owners). Deterministic for the same input; no
secret, no absolute server path.

report.md order, empty sections left out: result block, summary, next steps, fixed, build fix, in-house
libraries, manual review (grouped by explanation), excluded items (`remediation.ignore_actions`), project
files (file components under `remediation.file_components: report`, counted nowhere else), unchanged, the
folded processing record (the only place with status / reason codes). `inhouse_artifact` residuals are grouped
per in-house artifact under `remediation.inhouse_message`; the in-house and manual-review tables print the
path each module got the library by."""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from sbmig.core import scrub as _scrub

from . import fsops, i18n
from .inhouse import FRAMEWORK_GROUP
from .matching import finding_key, ga_key
from .i18n import (
    BOUNDARY_KO,
    COUNT_KO,
    DECISION_KO,
    FINDING_KIND_KO,
    INHOUSE_GROUP_KO,
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
    AWAITING_CONFIRMATION,
    D_AUTO,
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
    SUCCEEDED: "조치 완료", INCOMPLETE: "일부만 조치됨", FAILED: "조치 실패",
    AWAITING_CONFIRMATION: "업로드 확인 필요", STOPPED: "중지됨", INTERRUPTED: "중단됨", QUEUED: "대기 중", RUNNING: "진행 중",
}
# 결과 블록 첫 줄에 굵게 쓰는 문장
_VERDICT = {
    SUCCEEDED: "✅ **조치를 마쳤습니다.**", INCOMPLETE: "⚠️ **일부만 조치되었습니다.**", FAILED: "❌ **조치하지 못했습니다.**",
    AWAITING_CONFIRMATION: "⏸ **확인이 필요합니다.**",
    STOPPED: "⏹ **조치를 멈췄습니다.**", INTERRUPTED: "⏹ **조치가 중단되었습니다.**",
    QUEUED: "⏳ **조치를 기다리고 있습니다.**", RUNNING: "⏳ **조치를 진행하고 있습니다.**",
}
# 사유 코드마다 (결과 줄에 쓸 설명, 해야 할 일)이다. "<n>"은 마지막 라운드 번호로 바뀐다
# 사내, 직접 확인처럼 개수가 붙는 할 일은 _next_steps가 개수를 넣는다
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
# 빌드에 실패한 수정본을 결과 ZIP으로 냈을 때(include 정책)의 candidate_build_failed 문장
_FAILED_INCLUDED_KO = ("버전을 바꾼 수정본이 빌드에 실패했습니다.",
                       "**빌드 오류 확인하기** — 빌드에 실패한 결과 ZIP은 배포하지 말고, `rounds/<n>/build.log` 파일과 "
                       "`rounds/<n>/candidate.patch` 파일을 확인해 빌드 오류를 고치거나 올릴 버전을 다시 정해 주세요.")
_SUCCEEDED_STEP = "**결과 ZIP 받기** — 재검사를 마친 결과 ZIP을 받아 사용해 주세요."
# 바꾼 것이 없어 원본을 그대로 낸 결과 ZIP(라운드 0)은 조치 전 검사만 거쳤다
_SUCCEEDED_STEP_ROUND0 = "**결과 ZIP 받기** — 검사를 마친 결과 ZIP을 받아 사용해 주세요."
# 조치 전 빌드가 실패해도 이어 간 실행(state.baseline_tolerated)의 baseline_build_failed 문장과 할 일
_TOLERATED_KO = (("이 프로젝트는 조치 전에도 빌드되지 않았습니다. 남은 빌드 오류는 프로젝트 담당자와 "
                  "프레임워크 담당자가 고쳐야 합니다. 취약점 검사는 오픈소스 "
                  "라이브러리를 보는 것이므로 빌드와 상관없이 진행했습니다."),
                 ("**빌드 오류 고치기** — 이관 보고서(`migration-report.md`)의 「변환 뒤 남은 빌드 오류」에 정리된 오류를 "
                  "프로젝트 담당자와 프레임워크 담당자가 고쳐 주세요."))
_TOLERATED_BUILD_STEP = ("**바꾼 버전으로 한 번 빌드하기** — 빌드 오류를 고친 뒤 결과 ZIP의 pom.xml 버전을 "
                         "그대로 두고 한 번 빌드해 주세요. 빌드가 통과해야 바꾼 버전을 그대로 쓸 수 있습니다.")
# 빌드 설정을 점검하다 읽을 수 없는 pom이 나와 멈췄을 때의 결과 줄 설명과 할 일
_POM_REFUSED_WHAT = "pom 파일을 읽지 못해 작업을 중단했습니다."
_POM_REFUSED_STEP = ("**pom 파일 고치기** — 위에 적힌 pom 파일을 열어 문법 오류나 파일 권한을 확인하고, Maven으로 빌드되는 "
                     "상태로 고친 뒤 다시 올려 주세요.")


def _clean(text: Any, roots: list[str]) -> Any:
    """문자열에서 비밀 환경 변수 값, URL 속 계정 정보, 실행 폴더 절대 경로를 지우고 마스킹 훅을 거친다.
    마스킹 훅은 ``SBMIG_REDACT_OUTPUT``이 켜졌을 때만 동작한다. 그래서 훅에만 맡기지 않고 여기서 먼저 지운다."""
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


def _load_scan(paths: RunPaths, rel: str, mode: str = "report") -> ScanResult | None:
    if not rel:
        return None
    data = read_json(paths.root / rel)
    if not isinstance(data, dict):
        return None
    from .ivas import count_view
    # count 모드면 파일 목록을 다 읽지 못한 검사를 미완료로 본다. 그래프와 같은 규칙이다
    return count_view(ScanResult.from_dict(data), mode)


def _load_plan(paths: RunPaths, n: int) -> RoundPlan | None:
    data = read_json(paths.plan(n))
    return RoundPlan.from_dict(data) if isinstance(data, dict) else None


def _ignore(ignore_actions: Iterable[str] | None) -> frozenset[str]:
    from .ivas import ignore_set
    return ignore_set(None if ignore_actions is None else list(ignore_actions))


def _mode(file_components: str | None) -> str:
    """``remediation.file_components`` 값을 report나 count로 정한다. None이면 report다."""
    from .ivas import files_reported
    return "report" if files_reported(file_components) else "count"


def _split(scan: ScanResult | None, ignore: frozenset[str], mode: str = "report") -> tuple[list[Any], list[Any]]:
    """(조치 대상, 제외 항목)을 반환한다. report 모드의 파일 컴포넌트는 어느 쪽에도 넣지 않는다."""
    if scan is None:
        return [], []
    from .ivas import split_files, split_findings
    return split_findings(split_files(list(scan.findings), mode)[0], ignore)


def _files(scan: ScanResult | None, mode: str) -> list[Any]:
    """report 모드에서 따로 싣는 파일 컴포넌트 항목을 반환한다. count 모드면 빈 목록을 반환한다."""
    if scan is None:
        return []
    from .ivas import split_files
    return split_files(list(scan.findings), mode)[1]


def _scan_summary(scan: ScanResult | None, rel: str, ignore: frozenset[str] = frozenset(),
                  mode: str = "report") -> dict[str, Any] | None:
    if scan is None:
        return None
    from .ivas import unknown_actions
    act, ign = _split(scan, ignore, mode)
    out = {"file": rel, "label": scan.label, "complete": scan.complete, "zip_sha256": scan.zip_sha256,
           "findings": len(scan.findings), "actionable": len(act), "ignored": len(ign),
           "unknown_actions": unknown_actions(list(scan.findings)), "counts": dict(sorted(scan.counts.items())),
           "incomplete_reasons": list(scan.incomplete_reasons), "status_code": scan.status_code}
    if mode == "report":
        # count 모드에서는 파일 컴포넌트 필드를 넣지 않는다
        out["file_findings"] = len(_files(scan, mode))
        out["file_complete"] = bool(scan.file_complete)
    return out


def _ignored_rows(scan: ScanResult | None, ignore: frozenset[str], mode: str = "report") -> list[dict[str, Any]]:
    """조치 대상에서 뺀 항목을 표의 행 목록(취약점, 종류, 대상, 버전, 코드, 뜻)으로 만든다. 정렬 순서는 고정한다."""
    from .ivas import action_meaning
    _act, ign = _split(scan, ignore, mode)
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
    """결과 ZIP이 빌드에 실패한 수정본이면 True를 반환한다(include 정책)."""
    return bool(state.delivery is not None and not state.delivery.build_ok and not state.delivery.build_tolerated)


def _tolerated(state: RemediationState) -> bool:
    """조치 전 빌드가 실패했는데도 검사와 조치를 이어 간 실행이면 True를 반환한다."""
    b = state.baseline_build
    return bool(state.baseline_tolerated and b is not None and not b.ok)


def _nothing_changed(state: RemediationState) -> bool:
    """확정한 라운드에 적용한 제안이 하나도 없으면 True를 반환한다.
    이때 결과 ZIP은 원본이라 빌드로 확인할 변경이 없다."""
    return not any(rs.committed and rs.applied for rs in state.rounds)


def build_unconfirmed(summary: Mapping[str, Any] | None) -> bool:
    """고칠 것은 다 고쳤고 빌드 확인만 남은 실행인지 조치 요약(``chain.summary``)으로 확인한다.
    조치 전 빌드 실패를 받아들인 실행이고, INCOMPLETE이고, 사유가 ``baseline_build_failed`` 하나뿐일 때만 True다.

    Another reason present, or no `reasons` list, is the ordinary "partly" case."""
    if not isinstance(summary, Mapping):
        return False
    reasons = summary.get("reasons")
    return bool(summary.get("baseline_tolerated") is True and summary.get("status") == INCOMPLETE
                and isinstance(reasons, list) and reasons == ["baseline_build_failed"])


# 조치하지 못한 것은 없고 빌드로 확인만 하지 못한 실행의 결과 문장이다(build_unconfirmed)
UNCONFIRMED_VERDICT_KO = "조치했지만 빌드로 확인하지 못했습니다"
# 바꾼 버전이 없어 빌드로 확인할 것이 없는 실행(조치 전 빌드 실패를 받아들인 실행)의 한 문장이다
NOTHING_TO_CONFIRM_KO = "이 프로젝트는 조치 전에도 빌드되지 않았지만, 바꾼 라이브러리 버전이 없어 빌드로 확인할 것은 없습니다."


def _tolerated_compare(state: RemediationState) -> dict[str, Any]:
    """조치 전 빌드와 같은 오류로만 실패해 확정한 마지막 라운드의 비교 결과를 반환한다. 없으면 빈 dict다."""
    cmps = [rs.build_compare for rs in sorted(state.rounds, key=lambda x: x.round)
            if rs.committed and rs.build_tolerated and rs.build_compare]
    return cmps[-1] if cmps else {}


def _verified_kept(paths: RunPaths) -> dict[str, Any] | None:
    """앞 라운드에서 검증을 마치고 보관해 둔 ZIP의 정보를 반환한다. 없으면 None이다.
    수정본이 실패하면 이 ZIP(``delivery/result-r<k>-verified.zip``)을 남겨 둔다."""
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
    return {"path": rel, "sha256": data.get("sha256", ""), "round": data.get("round", 0), "size": data.get("size", 0),
            # 조치 전 빌드 실패를 받아들인 실행에서 재검사만 하고 빌드로 확인하지 못한 결과 ZIP이다
            "unconfirmed": bool(data.get("build_tolerated") and not data.get("build_ok"))}


def _delivery_dict(state: RemediationState) -> dict[str, Any] | None:
    d = state.delivery
    if d is None:
        return None
    return {"path": d.path, "sha256": d.sha256, "size": d.size, "round": d.round, "build_ok": d.build_ok,
            "scanned": d.scanned, "build_tolerated": d.build_tolerated}


def build_report_json(state: RemediationState, paths: RunPaths, *,
                      ignore_actions: Iterable[str] | None = None,
                      inhouse_message: str | None = None,
                      severity: tuple[float, float, float] | None = None,
                      inhouse: Mapping[str, str] | None = None,
                      file_components: str | None = None) -> dict[str, Any]:
    """``report.json``에 쓸 내용을 만든다. 도구가 읽는 파일이라 한국어 번역은 넣지 않는다.
    state 전체, 검사 요약, 라운드별 계획 요약, 결과 ZIP이 검사한 ZIP과 같은지, 제외 항목, 사내 아티팩트별 항목을 담는다.
    ``ignore_actions``, ``inhouse_message``가 None이면 설정 기본값을 쓴다.
    ``inhouse``(``inhouse.inhouse_keys``)를 주면 사내 프레임워크 묶음에 ``group``을 넣는다.

    The content of `report.json`; `ignore_actions` None = the default codes. With `inhouse` an in-house artifact group
    of the `framework` group carries `"group": "framework"` (other groups unchanged). `file_components` None = `report`:
    then `file_findings` lists the file-component rows of the scan the report shows and `file_findings_complete` says
    whether its file part was read; under `count` neither key is written."""
    ignore = _ignore(ignore_actions)
    mode = _mode(file_components)
    base = _load_scan(paths, state.baseline_scan, mode)
    final = _load_scan(paths, state.final_scan, mode)
    rounds = []
    for rs in state.rounds:
        row = {"round": rs.round, "plan": _plan_summary(_load_plan(paths, rs.round)),
               "applied": list(rs.applied), "build": rs.build.to_dict() if rs.build else None,
               "verify": list(rs.verify_problems), "committed": rs.committed, "scan_label": rs.scan_label}
        fix = _fix_of(rs)
        if fix is not None:
            # 빌드 오류 수정 루프를 실행한 라운드에만 넣는다
            row["fix"] = fix.to_dict() if hasattr(fix, "to_dict") else dict(fix)
        dropped = _discarded_of(rs)
        if dropped:
            row["fix_discarded"] = [f.to_dict() if hasattr(f, "to_dict") else dict(f) for f in dropped]
        rounds.append(row)
    data = {"schema": REPORT_SCHEMA, "state_schema": SCHEMA, "state": state.to_dict(),
            "baseline_scan": _scan_summary(base, state.baseline_scan, ignore, mode),
            "final_scan": _scan_summary(final, state.final_scan, ignore, mode),
            "ignore_actions": sorted(ignore),
            "ignored_findings": _ignored_rows(final if final is not None else base, ignore, mode),
            "delivery_matches_scanned_zip": _delivery_matches(state, final), "delivery": _delivery_dict(state),
            "verified_delivery": _verified_kept(paths), "rounds": rounds,
            "inhouse": _inhouse_json(state, inhouse_message or _default_inhouse_message(), inhouse),
            "severity": _severity_json(base, final, ignore, severity_thresholds(severity), mode)}
    if mode == "report":
        shown, _from_base = _files_scan(base, final)
        data["file_components"] = mode
        data["file_findings"] = [{"id": f.id, "kind": f.kind, "identifier": f.identifier, "version": f.version,
                                  "patched_version": f.patched_version, "files": list(f.locations),
                                  "files_unread": bool(f.files_unread), "score": f.score, "action": f.action}
                                 for f in sorted(_files(shown, mode), key=lambda x: (x.identifier, x.version, x.id, x.kind))]
        data["file_findings_complete"] = bool(shown.file_complete) if shown is not None else None
    return _clean_json(data, _roots(paths))


def _files_scan(base: ScanResult | None, final: ScanResult | None) -> tuple[ScanResult | None, bool]:
    """파일 컴포넌트 절에 쓸 검사를 고른다. 끝난 검사 가운데 조치 후 검사가 먼저다.
    조치 후 검사의 파일 부분을 읽지 못했으면 조치 전 검사를 쓴다. 두 번째 값은 그렇게 조치 전 검사로 바꿨는지다.

    The scan whose file findings the report lists: the complete final scan, else the complete baseline; a final scan
    whose file part was not read gives way to a baseline whose file part was (the remediation changes no file)."""
    done = [x for x in (final, base) if x is not None and x.complete]
    if not done:
        return None, False
    read = [x for x in done if x.file_complete]
    pick = read[0] if read else done[0]
    return pick, pick is base and final is not None and final.complete


def _sev_counts(findings: Iterable[Any], th: tuple[float, float, float]) -> dict[str, int]:
    """발견 항목을 위험도(red / orange / yellow / none)별로 센다."""
    out = {"red": 0, "orange": 0, "yellow": 0, "none": 0}
    for f in findings:
        out[severity_of(f.score, th)[0]] += 1
    return out


def _severity_json(base: ScanResult | None, final: ScanResult | None, ignore: frozenset[str],
                   th: tuple[float, float, float], mode: str = "report") -> dict[str, Any]:
    """report.json의 ``severity``를 만든다. 위험도 경계값과 조치 전, 조치 후 검사의 위험도별 개수를 담는다.
    조치 후 검사가 끝나지 않았으면 ``final``은 None이다."""
    return {"thresholds": {"red": th[0], "orange": th[1], "yellow": th[2]},
            "baseline": _sev_counts(_split(base, ignore, mode)[0], th) if base is not None else None,
            "final": (_sev_counts(_split(final, ignore, mode)[0], th) if final is not None and final.complete
                      else None)}


def _md_cell(text: Any) -> str:
    return str(text if text not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")


def _earlier_delivery(state: RemediationState) -> bool:
    """작업은 실패로 끝났지만 앞 라운드에서 검증한 결과 ZIP이 남아 있으면 True를 반환한다."""
    last = max((rs.round for rs in state.rounds), default=0)
    return bool(state.status == FAILED and state.delivery is not None and state.delivery.round < last)


def _last_round(state: RemediationState) -> int:
    return max((rs.round for rs in state.rounds), default=1)


def _reason_text(state: RemediationState, code: str) -> tuple[str, str]:
    """사유 코드에 맞는 (결과 줄 설명, 해야 할 일)을 반환한다.
    실패한 수정본을 냈거나 앞 라운드의 결과 ZIP이 남아 있으면 문장을 바꾼다."""
    what, step = _REASON_KO[code]
    if code == "execution_boundary_refused":
        what, step = _boundary_text(state, what, step)
    if code == "baseline_build_failed" and _tolerated(state):
        what, step = _TOLERATED_KO
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


def _boundary_text(state: RemediationState, what: str, step: str) -> tuple[str, str]:
    """읽을 수 없는 pom이 있으면 어느 파일이 왜 안 읽히는지 결과 줄에 넣는다.
    중단한 이유가 pom뿐이면 문장과 할 일도 pom에 맞게 바꾼다."""
    refused = [b for b in state.boundary if b.action == "refuse"]
    poms = [b for b in refused if b.code == "unparsable_pom"]
    if not poms:
        return what, step
    where = " ".join(f"{b.path}: {i18n.pom_why_ko(b.detail) or label(BOUNDARY_KO, b.code)}." for b in poms)
    if len(poms) == len(refused):
        return f"{_POM_REFUSED_WHAT} {where}", _POM_REFUSED_STEP
    return f"{what} 읽지 못한 pom 파일도 있습니다. {where}", step


def _codes(state: RemediationState) -> list[str]:
    return list(state.reasons or ([state.reason] if state.reason else []))


RETRY_PASSED_KO = "처음 빌드는 실패했지만 다시 빌드하니 통과해 소스는 고치지 않았습니다."
INTERRUPTED_FIX_REASON = "interrupted: the run ended during the loop"      # graph가 fix_discarded에 남기는 사유
USAGE_UNKNOWN_SUFFIX = " (usage unknown)"
BUILD_CHANGED_SHOWN = 5


def _build_changed_line(rs: RoundState) -> str:
    """빌드가 바꿔서 원래대로 되돌려 둔 파일을 처리 기록 한 줄로 만든다. 없으면 빈 문자열이다.

    The record line for `RoundState.build_changed` (read defensively: older states have no such field)."""
    paths = [str(x) for x in (getattr(rs, "build_changed", None) or []) if str(x)]
    if not paths:
        return ""
    shown = ", ".join(f"`{_md_cell(x)}`" for x in paths[:BUILD_CHANGED_SHOWN])
    more = f" 외 {_num(len(paths) - BUILD_CHANGED_SHOWN)}개" if len(paths) > BUILD_CHANGED_SHOWN else ""
    return f"- 빌드가 바꾼 파일 {_num(len(paths))}개는 결과에 넣지 않고 원래대로 두었습니다. {shown}{more}"


def _fix_of(rs: RoundState) -> Any:
    """라운드의 빌드 오류 수정 결과(``RoundState.fix``)를 반환한다. 없거나 이 필드가 없는 이전 상태면 None이다."""
    return getattr(rs, "fix", None)


def _changed_fix(fix: Any) -> bool:
    """자동 수정이 소스를 실제로 바꿨으면 True를 반환한다.
    다시 빌드해서 통과한 경우(``retry_passed``)는 바꾼 파일이 없다."""
    return bool(fix is not None and getattr(fix, "fixed", False) and (getattr(fix, "changed", None) or []))


def _retry_passed(fix: Any) -> bool:
    """수정 루프의 첫 빌드가 고치지 않고 통과했으면 True를 반환한다.
    ``retry_passed``가 없는 이전 결과도 바꾼 파일이 없으면 통과한 것으로 본다."""
    if fix is None or not getattr(fix, "fixed", False):
        return False
    return bool(getattr(fix, "retry_passed", False)) or not (getattr(fix, "changed", None) or [])


def _discarded_of(rs: RoundState) -> list[Any]:
    """수정본을 다시 만들면서 버린 자동 수정 목록(``RoundState.fix_discarded``)을 반환한다. 없으면 빈 목록이다."""
    return list(getattr(rs, "fix_discarded", None) or [])


def _fixed_rounds(state: RemediationState) -> list[RoundState]:
    """빌드 오류를 고쳐서 확정한 라운드를 번호순으로 반환한다. 결과에 들어가는 소스 수정은 이 라운드의 것뿐이다.
    소스를 바꾸지 않은 라운드(다시 빌드해 통과)는 넣지 않는다.

    Rounds that are committed with `fix.fixed` and a non-empty `fix.changed`, by number: the only rounds whose fix is in
    the result, so the only ones the report, the page digest and the files route show."""
    return [rs for rs in sorted(state.rounds, key=lambda x: x.round) if rs.committed and _changed_fix(_fix_of(rs))]


def served_fix_round(state: RemediationState) -> RoundState | None:
    """결과에 들어간 마지막 자동 수정 라운드를 반환한다. 없으면 None이다.

    The last of `_fixed_rounds`, or None. The files route serves the combined `fix.patch` (`write_fix_patch`) only
    when this is not None."""
    rounds = _fixed_rounds(state)
    return rounds[-1] if rounds else None


FIX_PATCH_NAME = "fix.patch"


def fix_patch_path(paths: RunPaths) -> Path:
    """결과에 들어간 자동 수정을 모두 모은 ``fix.patch``의 경로를 반환한다. ``report.md`` 옆에 둔다.

    The files route serves it as `fix.patch`."""
    return paths.root / FIX_PATCH_NAME


def write_fix_patch(state: RemediationState, paths: RunPaths) -> Path | None:
    """확정한 자동 수정 라운드의 ``rounds/<r>/fix.patch``를 라운드 순서대로 모아 파일 하나로 원자적으로 쓴다.
    라운드마다 앞에 ``# round <r>`` 줄을 붙인다. 파일이 없는 라운드는 경고를 남기고 ``# round <r>: patch file missing`` 줄만 쓴다.
    그런 라운드가 없으면 이전에 쓴 파일을 지우고 None을 반환한다.

    The rounds are those of `_fixed_rounds`. Bytes are copied as they are (the per-round patches are UTF-8,
    INTERFACES "v2.1z-bk")."""
    target = fix_patch_path(paths)
    rounds = _fixed_rounds(state)
    if not rounds:
        if target.is_file():
            target.unlink()
        return None
    parts: list[bytes] = []
    for rs in rounds:
        src = paths.round_dir(rs.round) / FIX_PATCH_NAME
        try:
            body = src.read_bytes()
        except OSError as e:
            log.warning("fix.patch of round %s missing (%s): %s", rs.round, paths.rel(src), type(e).__name__)
            parts.append(f"# round {rs.round}: patch file missing\n".encode())
            continue
        if body and not body.endswith(b"\n"):
            body += b"\n"
        parts.append(f"# round {rs.round}\n".encode() + body)
    tmp = target.with_name(target.name + ".tmp")
    # Windows에서도 되도록 쓰기 핸들 하나로 쓰고 fsync한 뒤 바꾼다
    with open(tmp, "wb") as f:
        f.write(b"".join(parts))
        f.flush()
        os.fsync(f.fileno())
    fsops.replace(tmp, target)
    return target


def _fixed_files(state: RemediationState) -> list[str]:
    """확정한 라운드에서 빌드 오류를 고치느라 바꾼 소스 파일을 겹치지 않게 반환한다."""
    out: dict[str, None] = {}
    for rs in _fixed_rounds(state):
        for p in getattr(_fix_of(rs), "changed", None) or []:
            out[str(p)] = None
    return sorted(out)


def _unfixed_line(state: RemediationState) -> str:
    """자동 수정이 마지막 라운드의 빌드 실패를 고치지 못했으면 그 사실과 이유를 한 문장으로 만든다.
    그런 경우가 아니면 빈 문자열이다."""
    last = max(state.rounds, key=lambda x: x.round, default=None)
    fix = _fix_of(last) if last is not None else None
    if fix is None or getattr(fix, "fixed", False) or last.committed:
        return ""
    if not getattr(fix, "attempted", False):
        # 시작하지 않은 경우다. 시도했다고도, 소스 수정을 결과에서 뺐다고도 쓰지 않는다
        return i18n.fix_not_started_ko(getattr(fix, "reason", ""))
    from .graph import fix_made_no_attempt  # 순환 import를 피하려고 함수 안에서 import한다
    if fix_made_no_attempt(fix):
        # 루프는 시작했지만 시도가 하나도 없었다. 시도한 소스 수정이 있었다고 쓰지 않는다
        return i18n.fix_no_attempt_ko(getattr(fix, "reason", ""))
    return i18n.fix_failed_ko(getattr(fix, "reason", ""))


def _with_count(step: str, count: int | None, unit: str = "건") -> str:
    """할 일 제목 뒤에 "(N건)"을 붙인다. 개수를 모르거나 0이면 제목을 그대로 반환한다."""
    if not count:
        return step
    head, sep, rest = step.partition("** — ")
    return f"{head} ({count}{unit}){sep}{rest}" if sep else step


def _next_steps(state: RemediationState, fx: "_Facts | None" = None) -> list[str]:
    """현재 상태에서 담당자가 할 일 목록을 만든다. ``fx``가 있으면 할 일에 개수를 붙인다.
    순서는 자동으로 고친 소스 확인 -> 결과 ZIP 받기 -> 실패 원인 해결 -> 자동으로 적용한 변경 확인 ->
    사내 라이브러리 담당자에게 요청 -> 직접 확인 -> 나머지 사유다."""
    inhouse = [r for r in state.residuals if r.reason == R_INHOUSE_ARTIFACT]
    manual = [r for r in state.residuals if r.reason != R_INHOUSE_ARTIFACT]
    codes = [c for c in _codes(state) if c in _REASON_KO]
    out: list[str] = []
    fixed_files = _fixed_files(state)
    if fixed_files:
        # 도구가 고친 소스는 다른 할 일보다 먼저 확인을 요청한다. 내려받는 파일은 라운드를 모은 fix.patch 하나다
        out.append(f"**자동으로 고친 소스 확인하기** — 자동으로 고친 소스 {_num(len(fixed_files))}개를 확인해 주세요. "
                   f"바꾼 내용은 「빌드 오류 수정」과 `{FIX_PATCH_NAME}` 파일에 있습니다.")
    if state.status == SUCCEEDED:
        out.append(_SUCCEEDED_STEP_ROUND0 if state.delivery is not None and state.delivery.round == 0
                   else _SUCCEEDED_STEP)
    if _tolerated(state) and state.status != FAILED and not _nothing_changed(state):
        # 빌드 오류를 고치고 바꾼 버전으로 빌드해야 결과를 쓸 수 있다. 그래서 앞쪽에 둔다
        out.append(_TOLERATED_KO[1])
        if state.delivery is not None and state.delivery.build_tolerated and state.delivery.round > 0:
            out.append(_TOLERATED_BUILD_STEP)
        codes = [c for c in codes if c != "baseline_build_failed"]
    others = [_reason_text(state, c)[1] for c in codes
              if c not in (R_INHOUSE_ARTIFACT, "manual_review", "residual_findings")]
    if state.status in (FAILED, STOPPED, INTERRUPTED, AWAITING_CONFIRMATION):
        # 실패, 중단, 확인 대기는 원인부터 해결해야 다음 단계로 갈 수 있다. 그래서 앞에 둔다
        out += others
        others = []
    if fx is not None:
        auto = _auto_risky_step(fx.committed)
        if auto:
            out.append(auto)
    if inhouse or R_INHOUSE_ARTIFACT in codes:
        # 사유 코드에 빠져 있어도 사내 아티팩트 항목이 있으면 항상 담당자 연락이 필요하다
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
    if _tolerated(state) and state.status != FAILED and _nothing_changed(state):
        # 바꾼 버전이 없으면 결과와 상관없는 안내다. 그래서 맨 뒤에 둔다
        out.append(_TOLERATED_KO[1])
    return list(dict.fromkeys(out)) or ["**처리 기록 확인하기** — 「처리 기록」과 `remediation.log` 파일을 확인해 주세요."]


_RISK_ORDER = ("major", "parent", "bom", "managed_override")


def _auto_risky_step(committed: list[Proposal]) -> str:
    """적용한 변경 가운데 주의 항목이 있으면 확인을 요청하는 할 일을 만든다. 없으면 빈 문자열을 반환한다.

    The step for committed proposals with decision `auto` and a risk flag; one count per proposal, one per class."""
    props = [p for p in committed if p.decision == D_AUTO and p.risks]
    if not props:
        return ""
    kinds = [r for r in _RISK_ORDER if any(r in p.risks for p in props)]
    kinds += sorted({r for p in props for r in p.risks} - set(_RISK_ORDER))
    counts = ", ".join(f"{label(RISK_KO, r)} {sum(1 for p in props if r in p.risks)}건" for r in kinds)
    return (f"**자동으로 적용한 변경 확인하기 ({len(props)}건)** — 적용한 변경 가운데 주의가 필요한 "
            f"항목입니다({counts}). 「조치 완료」 표의 「주의」 열에서 해당 라이브러리를 확인하고 애플리케이션 동작을 "
            "점검해 주세요.")


def _default_inhouse_message() -> str:
    from sbmig.core.config import RemediationConfig
    return RemediationConfig().inhouse_message


def _inhouse_of(r: Residual) -> list[str]:
    """사내 아티팩트 항목을 묶을 g:a 목록을 반환한다.
    항목의 ``inhouse``가 먼저다. 없으면 설명 문장 속 g:a(이전 형식의 state)를, 그것도 없으면 항목의 g:a를 쓴다."""
    return sorted(set(r.inhouse or i18n.inhouse_gas(r.detail) or [r.ga]))


def _framework_ga(ga: str, inhouse: Mapping[str, str] | None) -> bool:
    """g:a가 사내 프레임워크(``framework`` 그룹) 아티팩트인지 확인한다. 목록이 없으면 False를 반환한다."""
    if not inhouse:
        return False
    try:
        return inhouse.get(ga) == FRAMEWORK_GROUP
    except Exception:  # noqa: BLE001 - 표시에만 쓴다. 판단하지 못하면 표시하지 않는다
        return False


def _inhouse_json(state: RemediationState, message: str, inhouse: Mapping[str, str] | None = None) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in state.residuals:
        if r.reason != R_INHOUSE_ARTIFACT:
            continue
        for ga in _inhouse_of(r):
            groups.setdefault(ga, []).append({"ga": r.ga, "version": r.version, "modules": list(r.modules),
                                              "via": list(r.via), "finding_ids": list(r.finding_ids)})
    arts: list[dict[str, Any]] = []
    for ga, libs in sorted(groups.items()):
        entry: dict[str, Any] = {"ga": ga, "libraries": sorted(libs, key=lambda x: (x["ga"], x["version"]))}
        # 다른 그룹의 출력이 바뀌지 않도록 프레임워크 묶음에만 group을 넣는다
        if _framework_ga(ga, inhouse):
            entry["group"] = FRAMEWORK_GROUP
        arts.append(entry)
    return {"message": message, "artifacts": arts}


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


def _fold(ga: Any, version: Any) -> tuple[str, str]:
    """(g:a, 버전)을 대소문자 구분 없이 비교할 키로 만든다.

    IVAS may spell a version in another case than Maven (`2.2.0.release` / `2.2.0.RELEASE`); rows carry Maven's
    spelling, findings IVAS's, so the report counts and scores by the case-folded pair."""
    return (str(ga or "").casefold(), str(version or "").casefold())


class _Facts:
    """report.md의 여러 절이 함께 쓰는 값을 한 번만 계산해 두는 클래스."""

    def __init__(self, state: RemediationState, paths: RunPaths, ignore: frozenset[str],
                 th: tuple[float, float, float] | None = None, mode: str = "report"):
        self.th = severity_thresholds(th)
        self.mode = mode
        self.base = _load_scan(paths, state.baseline_scan, mode)
        self.final = _load_scan(paths, state.final_scan, mode)
        self.b_act, self.b_ign = _split(self.base, ignore, mode)
        self.f_act, self.f_ign = _split(self.final, ignore, mode)
        # report 모드에서 따로 싣는 파일 컴포넌트다. 조치는 파일을 바꾸지 않아서 끝난 검사 하나에서 가져온다
        self.files_scan, self.files_from_base = _files_scan(self.base, self.final) if mode == "report" else (None, False)
        self.files = _files(self.files_scan, mode)
        self.plans = {rs.round: _load_plan(paths, rs.round) for rs in state.rounds}
        self.committed = []
        for rs in sorted(state.rounds, key=lambda x: x.round):
            pl = self.plans.get(rs.round)
            if rs.committed and pl is not None:
                ids = set(rs.applied)
                self.committed += [p for p in pl.proposals if p.id in ids]
        self.inhouse = [r for r in state.residuals if r.reason == R_INHOUSE_ARTIFACT]
        self.manual = [r for r in state.residuals if r.reason != R_INHOUSE_ARTIFACT]
        self.final_ok = self.final is not None and self.final.complete
        # 완료된 조치 후 검사에 남은 취약점이다. 버전과 상관없이 같은 라이브러리에 남아 있으면 조치하지 않은 것으로 본다
        self._left = {finding_key(f.kind, f.id, f.identifier) for f in self.f_act} if self.final_ok else set()
        self._left_ids = ({(f.id, ga_key(":".join(str(f.identifier or "").split(":")[:2]))) for f in self.f_act}
                          if self.final_ok else set())
        # 점수는 완료된 조치 후 검사에서 먼저 찾고, 없으면 조치 전 검사에서 찾는다. 조치한 취약점은 조치 후 검사에 없다
        # 같은 ID가 여러 라이브러리에 있으면 (ID, 대상, 버전)이 맞는 점수를 쓴다. 맞는 것이 없으면 그 ID의 최고 점수다
        self._exact: list[dict[tuple[str, str, str], float]] = []
        self._by_id: list[dict[str, float]] = []
        for scan in ([self.final] if self.final_ok else []) + ([self.base] if self.base is not None else []):
            exact: dict[tuple[str, str, str], float] = {}
            by_id: dict[str, float] = {}
            for f in scan.findings:
                if f.score is None:
                    continue
                k = (f.id, *_fold(f.identifier, f.version))
                exact[k] = max(exact.get(k, f.score), f.score)
                by_id[f.id] = max(by_id.get(f.id, f.score), f.score)
            self._exact.append(exact)
            self._by_id.append(by_id)

    def matching(self, items: list[Any]) -> int:
        """항목이나 제안과 같은 (g:a, 버전)인 조치 전 검사의 조치 대상 취약점 수를 센다.
        조치 전 검사가 없으면 항목에 기록된 취약점 ID 수를 반환한다."""
        keys = self._keys(items)
        if self.base is not None:
            return sum(1 for f in self.b_act if _fold(*_key(f)) in keys)
        return sum(len(set(r.finding_ids)) for r in items)

    @staticmethod
    def _keys(items: list[Any]) -> set[tuple[str, str]]:
        return {_fold(getattr(r, "ga", ""), getattr(r, "version", None) or getattr(r, "from_version", "")) for r in items}

    def overlap(self) -> int:
        """사내 라이브러리 절과 직접 확인 절에 모두 나오는 취약점 수를 센다.
        모듈에 따라 나뉜 항목이면 같은 취약점이 두 절에 모두 있다."""
        if self.base is None or not self.inhouse or not self.manual:
            return 0
        a, b = self._keys(self.inhouse), self._keys(self.manual)
        return sum(1 for f in self.b_act if _fold(*_key(f)) in a and _fold(*_key(f)) in b)

    def fixed(self) -> int | None:
        """조치 전 검사의 조치 대상 중 완료된 조치 후 검사에서 사라진 취약점 수를 센다.
        조치 후 검사가 없거나 완료되지 않았으면 None을 반환한다."""
        if self.base is None or not self.final_ok:
            return None
        # graph.fill_counts와 같은 키로 센다. 버전이 바뀌어도 같은 라이브러리에 남은 취약점은 조치한 것이 아니다
        return len({finding_key(f.kind, f.id, f.identifier) for f in self.b_act} - self._left)

    def resolved_ids(self, ga: str, ids: Iterable[str]) -> list[str]:
        """제안의 취약점 ID 가운데 완료된 조치 후 검사가 그 라이브러리에서 더 이상 보고하지 않는 것만 반환한다.
        버전은 보지 않는다. 조치 후 검사가 없거나 끝나지 않았으면 ID를 그대로 반환한다."""
        if not self.final_ok:
            return list(ids)
        g = ga_key(ga)
        return [i for i in ids if (i, g) not in self._left_ids]

    def cve_score(self, cve: str, ga: str, version: str) -> float | None:
        """취약점 하나의 점수를 반환한다. 검사 결과에 없으면 None을 반환한다."""
        k = (cve, *_fold(ga, version))
        for exact in self._exact:
            if k in exact:
                return exact[k]
        for by_id in self._by_id:
            if cve in by_id:
                return by_id[cve]
        return None

    def row_score(self, ids: Iterable[str], ga: str, version: str) -> float | None:
        """라이브러리 행의 취약점 중 가장 높은 점수를 반환한다."""
        got = [x for x in (self.cve_score(i, ga, version) for i in set(ids)) if x is not None]
        return max(got) if got else None

    def sev_cell(self, score: float | None) -> str:
        """위험도 칸을 "🔴 9.8" 형식으로 만든다. 표시 기준보다 낮으면 숫자만 쓰고, 점수가 없으면 "-"다."""
        if score is None:
            return "-"
        mark = severity_of(score, self.th)[1]
        return f"{mark} {score:.1f}" if mark else f"{score:.1f}"

    def cve_cell(self, ga: str, version: str, ids: Iterable[str]) -> str:
        """취약점 칸의 문자열을 만든다. 점수가 높은 순으로 "ID (점수)"를 세 개까지 쓰고 나머지는 "외 N건"으로 줄인다."""
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


# 결과 줄에 상태 메시지를 넣지 않는 사유 코드. 메시지가 빌드 요약이거나 결과 줄과 같은 내용이다
_MESSAGE_ELSEWHERE = ("baseline_build_failed", "candidate_build_failed", "text_only_violation", "resolution_mismatch")
# 결과 줄에 일반 문장 대신 상태 메시지를 쓰는 사유 코드. 메시지가 더 구체적이다
_MESSAGE_SAYS_IT = ("chain_budget", "upload_uncertain", "stop_requested")
# 결과 줄에 따로 쓰지 않는 사유 코드. 결과 줄의 숫자 문장이 이미 설명한다
_NUMBERS_EXPLAIN = ("manual_review", "residual_findings", R_INHOUSE_ARTIFACT)


def _numbers(state: RemediationState, fx: _Facts) -> list[str]:
    """결과 블록에 쓸 숫자 문장들을 만든다. 조치 전 검사가 끝났을 때만 만든다."""
    if fx.base is None or not fx.base.complete:
        return []
    total = len(fx.b_act)
    if total == 0:
        # 파일에서 찾은 취약점은 아래 절에 따로 있다. 그래서 라이브러리 취약점이 없다고 밝힌다
        return ["조치 전 검사에서 조치할 라이브러리 취약점이 없었습니다." if fx.files
                else "조치 전 검사에서 조치할 취약점이 없었습니다."]
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
    both = fx.overlap()
    if n_in or n_man:
        whole = left is not None and left == n_in + n_man - both
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
        if both:
            # 모듈에 따라 나뉜 항목의 취약점은 두 절에 모두 나온다. 그 건수를 따로 밝힌다
            out.append(f"그중 {_num(both)}건은 모듈에 따라 사내 라이브러리 담당자와 프로젝트 양쪽에서 조치해야 해서 두 절에 "
                       "모두 나옵니다.")
    return out


def _result_lines(state: RemediationState, fx: _Facts) -> list[str]:
    """결과 블록의 줄들("> " 없이)을 만든다. 굵은 문장, 무슨 일이 있었는지, 숫자 순이다."""
    codes = [c for c in _codes(state) if c in _REASON_KO]
    out = [_VERDICT.get(state.status, f"**{_STATUS_KO.get(state.status, state.status)}**")]
    body: list[str] = []
    if _tolerated(state) and state.status in (SUCCEEDED, INCOMPLETE) and not _nothing_changed(state):
        return _tolerated_lines(state, fx, codes)
    if state.status == INTERRUPTED:
        ko, _raw = _known_and_raw(state.message)
        body += ko or ["서버가 다시 시작되면서 조치가 멈췄습니다. 자동으로 다시 실행하지 않습니다."]
        return out + [" ".join(body)]
    elif state.status in (SUCCEEDED, INCOMPLETE):
        body += _numbers(state, fx)
        if _tolerated(state):
            # 바꾼 버전이 없으면 결과 문장 뒤에 빌드로 확인할 것이 없다는 한 문장만 둔다
            body.append(NOTHING_TO_CONFIRM_KO)
        body += [_reason_text(state, c)[0] for c in codes if c not in _NUMBERS_EXPLAIN]
        if state.status == INCOMPLETE and not body:
            body += [_reason_text(state, c)[0] for c in codes]
    else:
        ko, raw = _known_and_raw(state.message)
        if codes[:1] and codes[0] in _MESSAGE_SAYS_IT and ko and not raw:
            # 메시지가 검사 이름이나 시간 한도까지 알려 주므로 일반 문장 대신 쓴다
            return out + [" ".join(ko)]
        body += [_reason_text(state, c)[0] for c in codes[:1]]
        if _tolerated(state):
            body += _new_error_lines(state)
    from .graph import TOLERATED_HEAD  # 순환 import를 피하려고 함수 안에서 import한다
    # 조치 전 빌드 실패 문장과 읽을 수 없는 pom의 이유(_boundary_text)는 위에서 이미 썼다. 상세로 다시 쓰지 않는다
    pom_said = codes[:1] == ["execution_boundary_refused"] and any(b.code == "unparsable_pom" for b in state.boundary)
    if (state.message and not (set(codes) & set(_MESSAGE_ELSEWHERE)) and not pom_said
            and not str(state.message).startswith(TOLERATED_HEAD)):
        ko, _raw = _known_and_raw(state.message)
        if ko:
            body.append("상세: " + " ".join(ko))
    return out + ([" ".join(dict.fromkeys(body))] if body else [])


def _tolerated_lines(state: RemediationState, fx: _Facts, codes: list[str]) -> list[str]:
    """조치 전 빌드가 실패해도 이어 간 실행의 결과 블록 줄들을 만든다.
    순서는 굵은 문장, 빌드 사정, 비교 결과, 숫자다."""
    d = state.delivery
    changed = d is not None and d.round > 0
    cmp = _tolerated_compare(state)
    if changed and d.build_tolerated:
        verdict = "⚠️ **라이브러리 버전을 바꾸고 다시 검사했습니다. 바꾼 버전은 빌드로 확인하지 못했습니다.**"
    elif changed:
        verdict = "⚠️ **라이브러리 버전을 바꾸고 빌드한 뒤 다시 검사했습니다.**"
    else:
        verdict = "⚠️ **빌드되지 않는 상태로 취약점을 검사했습니다. 바꾼 라이브러리 버전은 없습니다.**"
    body = [_TOLERATED_KO[0]]
    if cmp:
        n = cmp.get("candidate_errors")
        body.append("버전을 바꾼 뒤에도 빌드는 조치 전과 같은 오류에서 멈췄습니다"
                    + (f"(오류 {_num(n)}건). " if isinstance(n, int) and n else ". ")
                    + "새로 생긴 오류는 없습니다. 다만 빌드가 오류가 난 곳에서 멈추므로, 그 뒤의 모듈과 코드가 바꾼 버전으로 "
                      "컴파일되는지는 알 수 없습니다.")
    body += _numbers(state, fx)
    body += [_reason_text(state, c)[0] for c in codes
             if c not in _NUMBERS_EXPLAIN and c != "baseline_build_failed"]
    from .graph import TOLERATED_HEAD  # 순환 import를 피하려고 함수 안에서 import한다
    if state.message and not str(state.message).startswith(TOLERATED_HEAD):
        ko, _raw = _known_and_raw(state.message)
        if ko:
            body.append("상세: " + " ".join(ko))
    return [verdict, " ".join(dict.fromkeys(body))]


def _new_error_lines(state: RemediationState) -> list[str]:
    """마지막 라운드의 수정본 빌드가 조치 전에 없던 오류로 실패했으면 그 사실을 알리는 문장을 만든다."""
    last = max(state.rounds, key=lambda x: x.round, default=None)
    cmp = last.build_compare if last is not None else {}
    if not cmp or cmp.get("same"):
        return []
    new = list(cmp.get("new") or [])
    if new:
        return [(f"조치 전에도 빌드 오류가 있었지만, 버전을 바꾼 뒤 조치 전에는 없던 오류가 {_num(len(new))}건 생겼습니다. "
                 f"새 오류: {', '.join(f'`{x}`' for x in new[:3])}")]
    why = i18n.ko_text(f"the candidate build failed differently from the baseline build: {cmp.get('why') or '-'}")
    return [f"조치 전에도 빌드 오류가 있었지만, {why}"]


def _place(p: Proposal) -> str:
    """제안이 수정하는 위치를 "`pom` 41행" 형식으로 만든다. 항목을 추가하는 제안은 "41행 앞에 추가"로 쓴다."""
    where = []
    for e in p.edits:
        where.append(f"`{_md_cell(e.pom)}` {e.line}행" + (" 앞에 추가" if e.op == E_INSERT else ""))
    return ", ".join(where) or "-"


def _note_lines(text: str) -> list[str]:
    """제안 메모를 한국어 문장 목록으로 바꾼다. 규칙 하나가 한 줄이다.
    번역하지 못한 부분은 마지막 줄에 원문으로 둔다."""
    ko, raw = _known_and_raw(text)
    return [k for k in ko if k] + ([f"원문: `{raw.replace('`', chr(39))}`"] if raw else [])


_DONE_HEAD = ["위험도", "라이브러리", "버전 변경", "수정한 곳", "해결된 취약점"]


def _by_pom(p: Proposal) -> bool:
    """pom마다 묶어 보여 줄 버전 지정 추가 제안이면 True를 반환한다.
    모듈별 경로가 없는 이전 형식의 최상위 pom 제안은 False다.

    A `managed_override` proposal shown per pom under its library: one with module sources or one that edits a module
    pom. A root override of an older state (no sources) keeps the one-row-per-proposal form."""
    return p.kind == K_MANAGED_OVERRIDE and bool(p.edits) and (bool(p.sources) or p.edits[0].pom != "pom.xml")


def _pom_words(pom: str) -> str:
    return "최상위 pom.xml" if pom == "pom.xml" else f"`{_md_cell(pom)}`"


def _entry_words(p: Proposal) -> str:
    """추가할 항목을 "`g:a:v` classifier `x`" 형식으로 만든다."""
    o = p.edits[0].override or {} if p.edits else {}
    g, a = o.get("groupId", ""), o.get("artifactId", "")
    coord = f"{g}:{a}" if g and a else p.ga
    text = f"`{_md_cell(coord)}:{_md_cell(o.get('version') or p.to_version)}`"
    variant = _one_variant(f"{o.get('type') or 'jar'}/{o.get('classifier') or ''}")
    return text + (f" {variant}" if variant else "")


def _sources_words(p: Any) -> str:
    """제안이나 항목의 모듈별 들어온 경로를 한 줄로 만든다. 경로가 없으면 적용 범위를 쓴다."""
    groups = _row_sources(p)
    if groups:
        return "; ".join((f"{', '.join(f'`{_md_cell(m)}`' for m in mods)} 모듈: " if mods else "") + text
                         for mods, text in groups)
    mods = getattr(p, "modules", None) or []
    return f"적용 범위 {_mods_code(mods)}" if mods else ""


def _pom_line(p: Proposal) -> str:
    """pom 하나에 버전 지정을 추가하는 제안을 한 줄로 만든다. 수정할 pom, 추가할 항목, 들어온 경로를 담는다."""
    pom = p.edits[0].pom if p.edits else ""
    parts = [f"{_pom_words(pom)}에 버전 지정 추가 (제안 `{_md_cell(p.id)}`)", f"항목 {_entry_words(p)}"]
    src = _sources_words(p)
    if src:
        parts.append(src)
    return " · ".join(parts)


def _proposal_section(L: list[str], fx: _Facts, props: list[Proposal]) -> None:
    """적용한 제안의 표와 제안별 설명을 쓴다. 버전 지정 추가 제안은 라이브러리별로 묶고 pom마다 한 줄로 쓴다.

    Grouping key of the `_by_pom` proposals: (library, from, to); one table row, then one line per pom."""
    # 주의 항목이 있을 때만 조치 완료 표에 주의 열을 넣는다
    risky_done = any(p.risks for p in props)
    head = _DONE_HEAD
    if risky_done:
        head = head[:-1] + ["주의"] + head[-1:]
    L.append("| " + " | ".join(head) + " |")
    L.append("|" + "---|" * len(head))
    units: list[list[Proposal]] = []
    grouped: dict[tuple[str, str, str], list[Proposal]] = {}
    for p in props:
        if _by_pom(p):
            key = (p.ga, p.from_version, p.to_version)
            if key not in grouped:
                grouped[key] = []
                units.append(grouped[key])
            grouped[key].append(p)
        else:
            units.append([p])
    for u in units:
        u.sort(key=lambda p: (p.edits[0].pom if p.edits else "", p.id))

    def ids(u: list[Proposal]) -> list[str]:
        return [i for p in u for i in p.finding_ids]

    scores = {id(u): fx.row_score(ids(u), u[0].ga, u[0].from_version) for u in units}
    units.sort(key=lambda u: (*fx.order(scores[id(u)], u[0].ga, u[0].from_version), u[0].id))
    for u in units:
        p = u[0]
        if _by_pom(p):
            poms = list(dict.fromkeys(_pom_words(x.edits[0].pom) for x in u))
            place = _cut(poms, unit="곳")
        else:
            place = _place(p)
        cells = [fx.sev_cell(scores[id(u)]), f"`{_md_cell(p.ga)}`",
                 f"{_md_cell(p.from_version)} → {_md_cell(p.to_version)}", place]
        if risky_done:
            risks = sorted({r for x in u for r in x.risks})
            cells.append(_md_cell(", ".join(label(RISK_KO, r) for r in risks) or "-"))
        # 조치 완료 표의 해결된 취약점은 완료된 재검사에서 사라진 것만 쓴다
        shown = fx.resolved_ids(p.ga, ids(u))
        cells.append(_md_cell(fx.cve_cell(p.ga, p.from_version, shown)))
        L.append("| " + " | ".join(cells) + " |")
    L.append("")
    for u in units:
        p = u[0]
        if not _by_pom(p):
            _proposal_notes(L, p)
            continue
        ref = f"제안 `{_md_cell(p.id)}`" if len(u) == 1 else f"제안 {len(u)}건"
        L.append(f"- **`{_md_cell(_aid(p.ga))}` {_md_cell(p.from_version)} → {_md_cell(p.to_version)}** ({ref})")
        if p.evidence:
            L.append(f"  - 버전 확인: {_md_cell(evidence_ko(p.evidence))}")
        L.append(f"  - 수정 방식: {_md_cell(label(KIND_KO, p.kind))}")
        if len(u) == 1:
            L.append(f"  - {_md_cell(_pom_line(p))}")
        else:
            # 이미 적용한 변경이라 과거형으로 쓴다
            L.append("  - 수정한 pom:")
            L += [f"    - {_md_cell(_pom_line(x))}" for x in u]
        notes = list(dict.fromkeys(n for x in u for n in (_note_lines(x.note) if x.note else [])))
        if len(notes) == 1:
            L.append(f"  - 참고: {_md_cell(notes[0])}")
        elif notes:
            L.append("  - 참고:")
            L += [f"    - {_md_cell(n)}" for n in notes]


def _proposal_notes(L: list[str], p: Proposal) -> None:
    """제안 하나의 설명 줄(버전 확인, 수정 방식, 적용 범위, 참고)을 쓴다."""
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
    """사내 라이브러리 표의 의존 경로를 만든다. artifactId를 화살표로 잇고 취약한 라이브러리로 끝낸다.
    버전은 버전 칸에 따로 쓴다."""
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
    """이 묶음이 회사 부모 pom에서 왔으면 True를 반환한다.
    의존 경로 없이 부모 pom이나 BOM이 버전을 정하는 행이 하나라도 있으면 그렇게 본다."""
    return any(not r.via and r.origin in ("parent_managed", "bom_managed") for r in rows)


def _common_modules(rows: list[Residual]) -> str | None:
    """모든 행의 모듈 목록이 같으면 그 목록을, 다르면 None을 반환한다. 모듈이 하나도 없으면 빈 문자열이다."""
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


def _version_cell(r: Residual) -> str:
    """버전 칸을 만든다. IVAS 표기가 다르면 Maven 표기 뒤에 IVAS 표기를 한 번 붙인다.

    Example: `2.2.0.RELEASE (IVAS 표기: 2.2.0.release)`."""
    rv = getattr(r, "reported_version", "") or ""
    text = _md_cell(r.version)
    return f"{text} (IVAS 표기: {_md_cell(rv)})" if rv and rv != r.version else text


def _one_variant(part: str) -> str:
    """"<type>/<classifier>" 하나를 말로 바꾼다. 일반 jar("jar/" 또는 빈 값)면 빈 문자열을 반환한다."""
    typ, _sep, cls = part.strip().partition("/")
    words = ([f"형식 `{_md_cell(typ)}`"] if typ and typ != "jar" else []) + \
        ([f"classifier `{_md_cell(cls)}`"] if cls else [])
    return " ".join(words)


def _variant_words(variant: str) -> str:
    """``ModuleSource.variant``("jar/, jar/jakarta"처럼 ", "로 이은 목록)를 읽을 수 있는 말로 바꾼다.
    일반 jar를 뜻하는 "jar/"는 그대로 보여 주지 않는다.

    One readable phrase for a module's variants: "" for a plain jar only; one variant alone as its words
    ("classifier `jakarta`", "형식 `test-jar` classifier `tests`"); several (the plain jar listed as "jar/" first)
    as "사용 형태 N가지(일반 jar, classifier `jakarta`)". The raw "jar/" never shows."""
    parts = [p.strip() for p in variant.split(",") if p.strip()]
    words = [_one_variant(p) or "일반 jar" for p in parts]
    words = list(dict.fromkeys(words))
    if not words or words == ["일반 jar"]:
        return ""
    if len(words) == 1:
        return words[0]
    return f"사용 형태 {len(words)}가지({', '.join(words)})"


def _source_text(s: Any) -> str:
    """모듈 하나에 라이브러리가 들어온 경로를 한 줄로 만든다. 값 뒤에는 바뀌는 조사를 붙이지 않는다.

    One module's source in words: a direct declaration (pom and line), the transitive path (the module's own dependency
    first), the parent / BOM that sets the version, the classifier / type. "" when the source says nothing."""
    parts: list[str] = []
    how = str(getattr(s, "how", "") or "")
    via = [str(x) for x in (getattr(s, "via", None) or []) if x]
    d = getattr(s, "declared", None)
    where = ""
    if d is not None and getattr(d, "pom", ""):
        where = f"`{_md_cell(d.pom)}`" + (f" {d.line}행" if getattr(d, "line", 0) else "")
    if via:
        parts.append(" > ".join(f"`{_md_cell(x)}`" for x in via) + " 경로로 가져옴")
        if where:
            parts.append(f"선언 위치 {where}")
    elif where:
        if how in ("direct_literal", "direct_property"):
            parts.append(f"{where}에 직접 선언")
        elif how == "own_management":
            parts.append(f"{where}의 dependencyManagement에서 버전 지정")
        else:
            parts.append(f"{where}에 선언")
    prop = getattr(d, "property", "") if d is not None else ""
    if prop:
        parts.append(f"버전 property `{_md_cell(prop)}`")
    manager = str(getattr(s, "manager", "") or "")
    if manager:
        parts.append(f"`{_md_cell(manager)}`에서 버전 지정")
    variant = _variant_words(str(getattr(s, "variant", "") or ""))
    if variant:
        parts.append(variant)
    if not parts and how:
        parts.append(label(ORIGIN_KO, how))
    return ", ".join(parts)


def _row_sources(r: Residual) -> list[tuple[list[str], str]]:
    """행의 모듈을 들어온 경로가 같은 것끼리 묶어서 (모듈 목록, 경로 문장) 목록으로 반환한다.
    순서는 ``sources``를 따른다."""
    groups: dict[str, list[str]] = {}
    for s in getattr(r, "sources", None) or []:
        text = _source_text(s)
        if text:
            mods = groups.setdefault(text, [])
            mod = str(getattr(s, "module", "") or "")
            if mod and mod not in mods:
                mods.append(mod)
    return [(mods, text) for text, mods in groups.items()]


def _source_adds(s: Any) -> bool:
    """의존 경로 칸에 없는 사실(선언한 줄, 형식이나 classifier)이 경로에 있으면 True를 반환한다."""
    d = getattr(s, "declared", None)
    return bool((d is not None and getattr(d, "pom", "")) or _variant_words(str(getattr(s, "variant", "") or "")))


def _mixed_origin(r: Residual) -> bool:
    """모듈별 경로의 출처가 행의 출처와 다른 모듈이 있으면 True를 반환한다. 경로가 없으면 False다.

    True when a module source names another origin than the row's (an also-module declaring the library itself)."""
    hows = {str(getattr(s, "how", "") or "") for s in getattr(r, "sources", None) or []} - {""}
    return bool(hows) and hows != {r.origin}


def _sources_block(L: list[str], rows: list[Residual], *, beside_path: bool = False) -> None:
    """표 아래에 행마다 "들어온 경로" 목록을 쓴다. 모든 모듈이 같으면 한 줄, 다르면 모듈별로 쓴다.
    들어온 경로가 있는 행이 없으면(이전 형식의 state) 아무것도 쓰지 않는다."""
    items = [(r, _row_sources(r)) for r in rows]
    items = [(r, g) for r, g in items if g]
    if beside_path:
        # 의존 경로 칸이 이미 경로를 보여 준다. 그래서 모듈마다 다르거나 칸에 없는 사실이 있는 행만 쓴다
        items = [(r, g) for r, g in items if len(g) > 1 or any(_source_adds(x) for x in r.sources)]
    if not items:
        return
    L.append("")
    L.append("들어온 경로:")
    L.append("")
    for r, groups in items:
        head = f"- `{_md_cell(r.ga)}` {_md_cell(r.version)}"
        if len(groups) == 1:
            L.append(f"{head}: {groups[0][1]}")
            continue
        L.append(head)
        for mods, text in groups:
            who = ", ".join(f"`{_md_cell(m)}`" for m in mods)
            L.append(f"  - {who} 모듈: {text}" if who else f"  - {text}")


def _inhouse_section(L: list[str], fx: _Facts, message: str, inhouse: Mapping[str, str] | None = None) -> None:
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
    framework = {ga for ga in groups if _framework_ga(ga, inhouse)}
    if framework:
        L.append("")
        L.append(f"{INHOUSE_GROUP_KO[FRAMEWORK_GROUP]}의 버전은 바꾸지 않습니다. 프레임워크와 프레임워크가 가져오는 "
                 "라이브러리의 취약점은 프레임워크 담당자가 조치해야 합니다.")
    for ga in sorted(groups, key=lambda g: (-len(groups[g]), g)):
        rows = sorted(groups[ga], key=lambda r: fx.order(fx.row_score(r.finding_ids, r.ga, r.version), r.ga, r.version))
        L.append("")
        if ga in framework:
            tag = f" ({INHOUSE_GROUP_KO[FRAMEWORK_GROUP]})"
        else:
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
            cells = [fx.sev_cell(fx.row_score(r.finding_ids, r.ga, r.version)), _lib_cell(r), _version_cell(r),
                     _md_cell(_dep_path(r, ga))]
            if common is None:
                cells.append(_md_cell(_mods(r.modules)))
            cells.append(_md_cell(fx.cve_cell(r.ga, r.version, r.finding_ids)))
            L.append("| " + " | ".join(cells) + " |")
        _sources_block(L, rows, beside_path=True)


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
        # 모듈별 경로가 행의 출처와 다르면 "모두 간접 의존성" 같은 문장이 틀린다. 그래서 표 위로 올리지 않는다
        lift_origin = len(rows) > 1 and len(origins) == 1 and not any(_mixed_origin(r) for r in rows)
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
            cells = [fx.sev_cell(fx.row_score(r.finding_ids, r.ga, r.version)), _lib_cell(r), _version_cell(r)]
            if not lift_origin:
                cells.append("모듈마다 다름 (들어온 경로 참고)" if _mixed_origin(r) else _md_cell(label(ORIGIN_KO, r.origin)))
            if common is None and not no_mods:
                cells.append(_md_cell(_mods(r.modules)))
            cells.append(_md_cell(fx.cve_cell(r.ga, r.version, r.finding_ids)))
            if memo_col:
                cells.append(_md_cell(m))
            L.append("| " + " | ".join(cells) + " |")
        _sources_block(L, rows)


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
    rows = _ignored_rows(shown, ignore, fx.mode)
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
    for f in _split(shown, ignore, fx.mode)[1]:
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


FILES_TITLE = "프로젝트 파일에서 찾은 취약점"
FILES_LEAD = ("IVAS가 프로젝트 안에 들어 있는 파일(정적 파일 등)에서 찾은 취약점입니다. 이번 조치는 pom.xml의 라이브러리 "
              "버전만 바꾸므로 이 항목은 바꾸지 않았고, 위의 조치 결과에도 넣지 않았습니다.")
FILES_UNREAD = "파일 위치를 읽지 못했습니다"


def _files_section(L: list[str], fx: _Facts) -> None:
    """report 모드에서 파일 컴포넌트 항목을 컴포넌트별 표로 쓴다. 쓸 것이 없으면 아무것도 쓰지 않는다.

    The file components of `remediation.file_components: report`: one row per (component, version) with the patched
    version IVAS names, the findings and the files (cut with 외 N개; 「파일 위치를 읽지 못했습니다」 when the matched
    files could not be read). A file part that could not be read at all is one sentence instead of a table."""
    scan = fx.files_scan
    if scan is None or (not fx.files and scan.file_complete):
        return
    if not fx.files:
        L.append(f"## {FILES_TITLE}")
        L.append("")
        L.append("파일에서 찾은 취약점 목록을 IVAS에서 읽지 못했습니다. 이번 조치는 pom.xml의 라이브러리 버전만 바꾸므로 "
                 "위의 조치 결과와는 관계가 없습니다.")
        L.append("")
        return
    L.append(f"## {FILES_TITLE} — {_num(len(fx.files))}건")
    L.append("")
    L.append(FILES_LEAD)
    extra = []
    if fx.files_from_base:
        extra.append("조치 후 검사에서는 이 목록을 읽지 못해 조치 전 검사 결과를 적었습니다.")
    if not scan.file_complete:
        extra.append("IVAS에서 목록을 끝까지 읽지 못해 빠진 항목이 있을 수 있습니다.")
    groups: dict[tuple[str, str], list[Any]] = {}
    for f in fx.files:
        groups.setdefault(_fold(f.identifier, f.version), []).append(f)
    if any(all(f.kind != "file_component" for f in fs) for fs in groups.values()):
        extra.append("「(추정)」은 IVAS가 파일 내용을 보고 그 컴포넌트로 추정한 항목입니다.")
    if extra:
        L.append("")
        L.append(" ".join(extra))
    L.append("")
    rows = []
    for fs in groups.values():
        f0 = fs[0]
        score = max((f.score for f in fs if f.score is not None), default=None)
        patched = sorted({f.patched_version for f in fs if f.patched_version})
        if any(f.files_unread for f in fs):
            where = FILES_UNREAD
        else:
            where = _cut([f"`{x}`" for x in sorted({loc for f in fs for loc in f.locations})])
        name = f"`{_md_cell(f0.identifier)}`" + (" (추정)" if all(f.kind != "file_component" for f in fs) else "")
        cves = fx.cve_cell(f0.identifier, f0.version, [f.id for f in fs])
        rows.append((fx.order(score, f0.identifier, f0.version),
                     f"| {fx.sev_cell(score)} | {name} | {_md_cell(f0.version)} | {_md_cell(', '.join(patched))} "
                     f"| {_md_cell(cves)} | {_md_cell(where)} |"))
    table = ["| 위험도 | 컴포넌트 | 버전 | 패치된 버전 | 취약점 | 파일 위치 |", "|---|---|---|---|---|---|"]
    table += [r for _k, r in sorted(rows, key=lambda x: x[0])]
    _folded(L, table, f"{FILES_TITLE} 전체 — {_num(len(fx.files))}건" if len(rows) > 10 else "")
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
        # 재검사에서 취약점이 하나도 사라지지 않은 변경의 라이브러리는 조치 완료로 세지 않는다
        n_lib = len({p.ga for p in fx.committed if fx.resolved_ids(p.ga, p.finding_ids)})
        L.append(f"| 조치 완료 | {_num(fixed_n)}건 | " + (f"{_num(n_lib)}개 |" if n_lib else "- |"))
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
        if fx.files:
            L.append("")
            L.append(f"{FILES_TITLE} {_num(len(fx.files))}건은 위 개수에 넣지 않고 아래 「{FILES_TITLE}」에 따로 "
                     "적었습니다.")
    L.append("")
    L.append("결과 파일: " + _delivery_text(state, final))
    L.append("")


def _delivery_text(state: RemediationState, final: ScanResult | None) -> str:
    d = state.delivery
    if d is None:
        return "없음 — 원본은 `original/source.zip`에 그대로 있습니다."
    # 담당자는 프로젝트 이름으로 내려받은 ZIP만 본다. 그래서 작업 폴더 안의 경로 대신 결과 ZIP이라고 쓴다
    head = "결과 ZIP — "
    if d.build_tolerated:
        what = "원본 그대로, 빌드되지 않음" if d.round == 0 else "빌드로 확인하지 못함(조치 전과 같은 빌드 오류)"
        return head + what + ", " + ("재검사 완료" if d.scanned and _delivery_matches(state, final) else "재검사하지 않음")
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
    L.append(f"> **주의:** 결과 ZIP에는 라운드 {d.round}에서 빌드에 실패한 수정본(제안 {ids})이 들어 있고, "
             f"재검사는 하지 않았습니다. 빌드 로그는 `{blog}`에 있습니다. 이 ZIP을 그대로 배포하지 마세요.")
    L.append("> 원본은 `original/source.zip`에 그대로 보관합니다(내려받기 이름 source.zip).")
    kept = _verified_kept(paths)
    if kept and kept.get("unconfirmed"):
        L.append(f"> 라운드 {kept['round']}에서 재검사를 마친 결과 ZIP(빌드로는 확인하지 못함)은 `{kept['path']}`에 있습니다"
                 "(내려받기 이름 result-verified.zip).")
    elif kept:
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
        # 빌드 실패 메시지는 Maven 빌드 요약이다. 아래 빌드 단계에 그대로 나오므로 여기서는 쓰지 않는다
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
        L.append(f"**{step}. 조치 전 빌드** — {_build_line(bb)}" + (f" · 로그 `{bb.log}`" if bb.log else "")
                 + (" · 빌드 오류가 있어도 검사를 이어서 진행함" if _tolerated(state) else ""))
        if bb.summary and not bb.ok:
            _code_block(L, bb.summary)
    L.append("")
    step += 1
    if base is None:
        L.append(f"**{step}. 조치 전 검사 (IVAS)** — 검사하지 않음")
    else:
        head = (f"**{step}. 조치 전 검사 (IVAS)** — " + ("검사 완료" if base.complete else "검사 미완료")
                + f" · 취약점 {_num(len(fx.b_act))}건" + (f" (제외 {_num(len(fx.b_ign))}건 별도)" if fx.b_ign else "")
                + (f" (프로젝트 파일 {_num(n_files)}건 별도)" if (n_files := len(_files(base, fx.mode))) else "")
                + f" · 검사한 ZIP sha256 `{base.zip_sha256}`")
        L.append(head)
        if base.incomplete_reasons:
            L.append("- 미완료 사유: " + ", ".join(label(i18n.SCAN_REASON_KO, c) for c in base.incomplete_reasons))
        if fx.mode == "report" and base.complete and not base.file_complete:
            L.append("- 프로젝트 파일 쪽 목록은 IVAS에서 끝까지 읽지 못했습니다. 라이브러리 목록은 모두 읽었습니다.")
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
    return {p.id for p in fx.committed}


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
        # 감사 기록이라 모듈 목록을 줄이지 않는다
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
    if set(rs.applied) != planned or not planned:
        L.append(f"- 적용한 제안: {', '.join(rs.applied) if rs.applied else '없음'}")
    first = getattr(rs, "fix_first_build", None)
    if first is not None:
        # 자동 수정이 고친 라운드다. 아래의 수정본 빌드는 고친 뒤 다시 한 빌드다
        L.append(f"- 처음 수정본 빌드: {_build_line(first)}" + (f" · 로그 `{first.log}`" if first.log else ""))
    _fix_record_line(L, rs)
    if rs.build is not None:
        L.append(f"- 수정본 빌드: {_build_line(rs.build)}" + (f" · 로그 `{rs.build.log}`" if rs.build.log else ""))
        if rs.build_compare:
            c = rs.build_compare
            if c.get("same"):
                L.append(f"- 조치 전 빌드와 비교: 같은 오류에서 멈춤 (오류 {c.get('candidate_errors')}건, 새 오류 없음)")
            elif c.get("new"):
                L.append(f"- 조치 전 빌드와 비교: 새 오류 {len(c['new'])}건 — "
                         + ", ".join(f"`{_md_cell(x)}`" for x in c["new"][:5]))
            else:
                L.append(f"- 조치 전 빌드와 비교: {_md_cell(i18n.ko_text('the candidate build failed differently from the baseline build: ' + str(c.get('why') or '-')))}")
        if rs.build.summary and not rs.build.ok:
            _code_block(L, rs.build.summary)
    changed_by_build = _build_changed_line(rs)
    if changed_by_build:
        L.append(changed_by_build)
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


def _fix_record_line(L: list[str], rs: RoundState) -> None:
    """처리 기록의 라운드에 빌드 오류 자동 수정 결과를 쓴다. 버린 수정은 하나에 한 줄이다.
    수정 루프 결과가 없는 라운드는 버린 수정 줄만 쓴다."""
    for old in _discarded_of(rs):
        _discarded_line(L, old)
    fix = _fix_of(rs)
    if fix is None:
        return

    def n(key: str) -> str:
        return _num(int(getattr(fix, key, 0) or 0))
    if _usage_unknown(fix):
        # 사용량을 읽지 못한 결과다. 호출 수를 정확한 값처럼 쓰지 않는다
        L.append(f"- 빌드 오류 자동 수정: {_fix_outcome_ko(fix)} · {USAGE_UNKNOWN_KO}")
        return
    if _retry_passed(fix):
        L.append(f"- 빌드 오류 자동 수정: {RETRY_PASSED_KO}")
        return
    if getattr(fix, "fixed", False):
        files = _num(len(getattr(fix, 'changed', None) or []))
        if not rs.committed:
            # 채택하지 않은 라운드의 수정은 결과에 없다. 그래서 고쳤다고 쓰지 않고 patch 경로도 쓰지 않는다
            L.append(f"- 빌드 오류 자동 수정: 수정본을 채택하지 않아 결과에 넣지 않음 · 파일 {files}개 · "
                     f"{n('iterations')}회 반복 · LLM 호출 {n('llm_calls')}회")
            return
        patch = getattr(fix, "patch", "") or ""
        L.append(f"- 빌드 오류 자동 수정: 고침 · 파일 {files}개 · {n('iterations')}회 반복 · {_calls_ko(fix)}"
                 + (f" · 바꾼 내용 `{_md_cell(patch)}`" if patch else ""))
        return
    if not getattr(fix, "attempted", False):
        L.append(f"- 빌드 오류 자동 수정: 시작하지 않음({i18n.fix_not_started_reason_ko(getattr(fix, 'reason', ''))})")
        return
    if getattr(fix, "discarded_changed", None):
        # 고쳤지만 확인 빌드가 실패해 버린 수정이다
        _discarded_line(L, fix)
        return
    ko, raw = i18n.fix_reason_ko(getattr(fix, "reason", ""))
    from .graph import fix_made_no_attempt  # 순환 import를 피하려고 함수 안에서 import한다
    if fix_made_no_attempt(fix):
        # 시도가 하나도 없던 루프다. 반복 횟수와 시도한 수정은 쓰지 않는다
        L.append("- 빌드 오류 자동 수정: 하지 못함" + f"({ko or '이유 모름'})" + f" · {_calls_ko(fix)}"
                 + (f" · {_md_cell(raw)}" if raw else ""))
        return
    L.append("- 빌드 오류 자동 수정: 고치지 못함" + (f"({ko})" if ko else "") + f" · {n('iterations')}회 반복 · "
             f"{_calls_ko(fix)} · 시도한 소스 수정은 결과에 넣지 않음" + (f" · {_md_cell(raw)}" if raw else ""))


def _discarded_line(L: list[str], fix: Any) -> None:
    """버린 자동 수정 하나를 처리 기록에 한 줄로 쓴다. 바꿨던 파일 수와 LLM 호출 수를 함께 쓴다."""
    reason = str(getattr(fix, "reason", "") or "")
    ko, raw = i18n.fix_reason_ko(reason)
    if reason.startswith(INTERRUPTED_FIX_REASON):
        # 실행이 중간에 끝나서 멈춘 수정 루프다. 바꾼 파일은 없고 LLM 호출 수만 의미가 있다
        calls = ("LLM 호출 수는 알 수 없음" if reason.endswith(USAGE_UNKNOWN_SUFFIX) or _usage_unknown(fix)
                 else _calls_ko(fix))
        L.append(f"- 빌드 오류 자동 수정: 버린 수정({ko or '실행이 수정 도중에 끝남'}) · {calls}")
        return
    files = getattr(fix, "discarded_changed", None) or getattr(fix, "changed", None) or []
    L.append("- 빌드 오류 자동 수정: 버린 수정" + (f"({ko})" if ko else "") + f" · 파일 {_num(len(files))}개 · "
             f"{_num(int(getattr(fix, 'iterations', 0) or 0))}회 반복 · "
             f"{_calls_ko(fix)}" + (f" · {_md_cell(raw)}" if raw else ""))


USAGE_UNKNOWN_KO = "LLM 사용량 일부는 알 수 없음"


def _usage_unknown(fix: Any) -> bool:
    """수정 루프 결과의 LLM 사용량을 읽지 못했으면 True를 반환한다(``FixResult.usage_unknown``)."""
    return bool(getattr(fix, "usage_unknown", False))


def _tokens_unknown(fix: Any) -> bool:
    """토큰 수를 모르는 호출이 결과에 있으면 True를 반환한다(``FixUsage.tokens_unknown``)."""
    return any(bool(getattr(u, "tokens_unknown", False)) for u in (getattr(fix, "usage", None) or []))


def _calls_ko(fix: Any) -> str:
    """처리 기록에 쓸 LLM 호출 수 문구를 만든다. 토큰 수를 모르는 호출이 있으면 그렇게 덧붙인다."""
    text = f"LLM 호출 {_num(int(getattr(fix, 'llm_calls', 0) or 0))}회"
    return f"{text} · {USAGE_UNKNOWN_KO}" if _tokens_unknown(fix) else text


def _fix_outcome_ko(fix: Any) -> str:
    """사용량을 모르는 수정 루프의 결과를 짧은 말로 반환한다.
    "고침", "시작하지 않음", "고치지 못함(사유)" 가운데 하나다."""
    if getattr(fix, "fixed", False):
        return "고침"
    if not getattr(fix, "attempted", False):
        return "시작하지 않음"
    ko, _raw = i18n.fix_reason_ko(getattr(fix, "reason", ""))
    return "고치지 못함" + (f"({ko})" if ko else "")


# 결과 ZIP 없이 FAILED로 끝난 실행의 「결과 파일」 문장(사유 코드별)
# 상태 메시지로 정하는 두 경우는 _no_delivery_why가 먼저 본다
_NO_DELIVERY_KO = {
    "candidate_build_failed": "빌드에 실패한 수정본은 결과 ZIP으로 만들지 않았습니다.",
    "baseline_build_failed": "조치 전 빌드가 실패해 검사와 조치를 진행하지 않았습니다.",
    "text_only_violation": "버전 표기 외의 변경이 발견된 수정본은 결과 ZIP으로 만들지 않았습니다.",
    "resolution_mismatch": "의존성 버전이 계획과 다르게 적용된 수정본은 결과 ZIP으로 만들지 않았습니다.",
    "zip_hash_mismatch": "검사한 파일과 가진 파일이 같은지 확인하지 못해 결과 ZIP을 만들지 않았습니다.",
    "execution_boundary_refused": "조치를 시작하기 전에 멈춰 결과 ZIP을 만들지 않았습니다.",
    "not_ready": "조치를 시작하기 전에 멈춰 결과 ZIP을 만들지 않았습니다.",
    "internal_error": "도구 내부 오류로 결과 ZIP을 만들지 못했습니다.",
}
_NOT_WRITTEN_ROUND = re.compile(r"the result ZIP of round (\d+) could not be written")


def _no_delivery_why(state: RemediationState) -> str:
    """결과 ZIP 없이 FAILED로 끝난 실행에서 「결과 파일」의 「없음」 뒤에 붙일 이유 문장을 만든다.
    FAILED가 아니면 빈 문자열을 반환한다.

    Order: a candidate that could not be put back after a (possibly passing) build, a result ZIP that could
    not be written, then by reason code (`_NO_DELIVERY_KO`); a code without its own sentence says only that no
    result ZIP was made."""
    if state.status != FAILED:
        return ""
    keys = i18n.rule_keys(state.message)
    if "candidate_not_restored" in keys:
        text = "수정본을 빌드 뒤 원래 상태로 되돌리지 못해 결과 ZIP을 만들지 않았습니다."
    elif "delivery_not_written" in keys:
        m = _NOT_WRITTEN_ROUND.search(str(state.message or ""))
        text = (f"라운드 {m.group(1)}의 " if m else "") + "결과 ZIP을 쓰지 못했습니다."
    else:
        text = _NO_DELIVERY_KO.get(state.reason, "결과 ZIP을 만들지 않았습니다.")
    return f" — {text} 원본은 그대로입니다."


def _delivery_block(L: list[str], state: RemediationState, fx: _Facts, paths: RunPaths, step: int) -> None:
    d, final = state.delivery, fx.final
    if d is None:
        L.append(f"**{step}. 결과 파일** — 없음{_no_delivery_why(state)}")
    else:
        facts = [f"{_mb(d.size)}", f"파일 {_num(d.files)}개"] + ([f"라운드 {d.round}"] if len(state.rounds) > 1 else [])
        built = "빌드 성공" if d.build_ok else "빌드로 확인하지 못함" if d.build_tolerated else "빌드 실패"
        status = [built, "재검사 완료" if d.scanned else "재검사하지 않음"]
        if d.scanned:
            status.append("재검사한 파일과 일치" if _delivery_matches(state, final) else "재검사한 파일과 다름")
        L.append(f"**{step}. 결과 파일** — `{d.path}` ({', '.join(facts)}) · " + " · ".join(status))
        L.append(f"- sha256 `{d.sha256}`")
        kept = _verified_kept(paths)
        if kept:
            what = "재검사한 결과 ZIP(빌드로는 확인하지 못함)" if kept.get("unconfirmed") else "검증한 결과 ZIP"
            L.append(f"- 라운드 {kept['round']}에서 {what}: `{kept['path']}` (sha256 `{kept['sha256']}`)")
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


def _fix_section(L: list[str], state: RemediationState) -> None:
    """빌드 오류를 자동으로 고쳐 확정한 라운드가 있으면 「빌드 오류 수정」 절을 쓴다.
    라운드가 여럿이면 라운드마다 소제목을 둔다.

    Known limits of the fix loop (INTERFACES "v2.1z-bk", not fixed): a project whose build needs its own `.git`
    (git-commit-id) cannot be built in the work copy, so its loop ends "not fixed"; Maven calls of the loop that do
    not go through the build runner (effective pom, dependency tree) keep their own timeouts; the per-attempt guard
    does not see a new file under a path the project's `.gitignore` ignores (the end check does)."""
    rounds = _fixed_rounds(state)
    if not rounds:
        return
    L.append("## 빌드 오류 수정")
    L.append("")
    L.append("버전을 올린 뒤 빌드가 실패해 소스를 고쳤습니다.")
    L.append("")
    for rs in rounds:
        fix = _fix_of(rs)
        if len(rounds) > 1:
            L.append(f"### 라운드 {rs.round}")
            L.append("")
        L.append("| 파일 |")
        L.append("|---|")
        for p in getattr(fix, "changed", None) or []:
            L.append(f"| `{_md_cell(p)}` |")
        L.append("")
        L.append(_fix_calls_sentence(rs, fix))
        L.append("")
    # write_fix_patch가 모든 라운드의 diff를 report.md 옆의 fix.patch 하나에 모은다
    many = " 라운드마다 나누어" if len(rounds) > 1 else ""
    L.append(f"바꾼 내용 전체는 `{FIX_PATCH_NAME}` 파일에{many} 모아 두었습니다.")
    L.append("")


def _fix_calls_sentence(rs: RoundState, fix: Any) -> str:
    """「빌드 오류 수정」의 반복 횟수와 LLM 호출 수 문장을 만든다.
    라운드에 버린 루프가 있으면 라운드 전체 호출 수도 쓴다.

    The fixing loop's own count, and when the round has other loops (`fix_discarded`: discarded, interrupted,
    stopped) the round's total: 「빌드와 수정을 N회 반복했고, LLM을 M회 호출했습니다(버린 시도까지 모두 T회).」.
    A count that is not exact (a loop whose usage could not be read, calls without token counts) says so, and the
    total then reads 「(버린 시도까지 알려진 것만 T회)」."""
    iters = _num(int(getattr(fix, "iterations", 0) or 0))
    own = int(getattr(fix, "llm_calls", 0) or 0)
    others = _discarded_of(rs)
    unknown = any(_usage_unknown(f) or _tokens_unknown(f) for f in [fix, *others])
    head = f"- 빌드와 수정을 {iters}회 반복했고, LLM을 {_num(own)}회 호출했습니다"
    if others:
        total = own + sum(int(getattr(f, "llm_calls", 0) or 0) for f in others)
        # 사용량 일부를 모르면 합계가 정확해 보이지 않게 "알려진 것만"이라고 쓴다
        head += f"(버린 시도까지 {'알려진 것만' if unknown else '모두'} {_num(total)}회)"
    return head + "." + (" LLM 사용량 일부는 알 수 없습니다." if unknown else "")


def _unchanged_section(L: list[str], state: RemediationState, fx: _Facts) -> None:
    L.append("## 변경하지 않은 것")
    L.append("")
    n_fixed = len(_fixed_files(state))
    if n_fixed:
        L.append(f"- 프로젝트 구조(파일·폴더)는 변경하지 않았습니다. 소스는 빌드 오류를 고치느라 {_num(n_fixed)}개 파일을 "
                 "바꿨습니다.")
    else:
        L.append("- 애플리케이션 소스와 프로젝트 구조(파일·폴더)는 변경하지 않았습니다.")
    left_out = bool(state.intake is not None and state.intake.build_output_files)
    # 빌드 산출물을 뺀 업로드인데 검사한 ZIP의 sha256이 원본과 같으면, 이전 버전이 원본 ZIP을 그대로 검사한 실행이다
    original_scanned = bool(left_out and fx.base is not None and state.intake is not None
                            and fx.base.zip_sha256 == state.intake.zip_sha256)
    if state.delivery is not None and state.delivery.round == 0:
        # 바꾼 것이 없어 조치 전에 검사한 ZIP을 그대로 결과 ZIP으로 냈다. 그래서 달라진 버전 표기도 없다
        same_as_upload = state.intake is not None and state.delivery.sha256 == state.intake.zip_sha256
        L.append("- 결과 ZIP은 원본 ZIP에서 빌드 산출물만 뺀 것입니다." if left_out and not same_as_upload
                 else "- 결과 ZIP은 원본 ZIP과 같은 파일입니다.")
    elif state.delivery is not None:
        override = any(p.kind == K_MANAGED_OVERRIDE for p in fx.committed)
        if n_fixed:
            # 조사가 앞 단어의 받침에 따라 바뀐다. 그래서 덧붙일 것이 있으면 쉼표로 잇는다
            extra = (", dependencyManagement에 추가한 버전 지정과" if override else "와") + f" 고친 소스 {_num(n_fixed)}개"
        else:
            extra = "와 dependencyManagement에 추가한 버전 지정" if override else ""
        same = "원본에서 빌드 산출물을 뺀 목록과 같습니다" if left_out else "원본과 같습니다"
        L.append(f"- 결과 ZIP의 파일 목록은 {same}. 달라진 것은 적용된 pom.xml의 버전 표기{extra}뿐입니다.")
    L.append("- 원본 ZIP은 `original/source.zip`에 그대로 보관합니다.")
    # 접수 단계에서 빌드 산출물을 뺐으면 한 번만 알린다. 원본 ZIP을 그대로 검사한 이전 버전의 실행에는 맞지 않는 문장이다
    for n in (state.intake.notes if state.intake is not None and not original_scanned else []):
        if "build_output_left_out" in i18n.rule_keys(n):
            L.append(f"- {_md_cell(ko_display(n))}")
    L.append("")


def build_report_md(state: RemediationState, paths: RunPaths, *, ignore_actions: Iterable[str] | None = None,
                    inhouse_message: str | None = None, severity: tuple[float, float, float] | None = None,
                    inhouse: Mapping[str, str] | None = None, file_components: str | None = None) -> str:
    """한국어 ``report.md`` 본문을 만든다. 절은 아래 번호 주석 순서로 쓴다. 빈 절은 쓰지 않는다.
    - 영어 문장은 ``i18n.ko_display``로 바꿔 보여 준다. 같은 설명은 묶음마다 한 번만 쓴다
    - ``severity``가 None이면 설정 기본값으로 🔴 / 🟠 / 🟡를 붙인다. 점수가 높은 행이 위에 온다
    - ``ignore_actions``, ``inhouse_message``가 None이면 설정 기본값을 쓴다
    - ``inhouse``(``inhouse.inhouse_keys``)를 주면 사내 프레임워크 묶음 제목에 "사내 프레임워크"를 붙인다

    `file_components` None = `report`: file findings get their own section after the library sections and a
    line under the summary."""
    ignore = _ignore(ignore_actions)
    roots = _roots(paths)
    message = inhouse_message if inhouse_message else _default_inhouse_message()
    fx = _Facts(state, paths, ignore, severity, _mode(file_components))
    L: list[str] = []

    # 1. 제목과 결과 블록
    L.append(f"# 취약점 조치 결과 — {state.name or state.id}")
    L.append("")
    for line in _result_lines(state, fx):
        L.append(f"> {_md_cell(line)}")
    if _failed_delivery(state):
        _failed_delivery_block(L, state, paths)
    unfixed = _unfixed_line(state)
    if unfixed:
        # 빌드 실패 안내 바로 아래에 자동 수정의 결과를 한 줄로 둔다
        L.append(">")
        L.append(f"> {_md_cell(unfixed)}")
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
        if any(not fx.resolved_ids(p.ga, p.finding_ids) for p in fx.committed):
            L.append("「해결된 취약점」 칸이 「-」인 변경은 적용했지만 재검사에서 같은 취약점이 아직 보고됩니다. 남은 취약점은 "
                     "「처리 기록」의 남은 취약점 목록에서 확인해 주세요.")
            L.append("")
        _proposal_section(L, fx, fx.committed)
        L.append("")

    # 4-1. 빌드 오류 수정(버전을 올린 뒤 실패한 빌드를 자동으로 고친 라운드)
    _fix_section(L, state)

    # 5. 사내 라이브러리에서 발생한 취약점
    if fx.inhouse:
        _inhouse_section(L, fx, message, inhouse)
        L.append("")

    # 6. 직접 확인이 필요한 취약점
    if fx.manual:
        _manual_section(L, fx)
        L.append("")

    # 7. 조치 대상에서 제외한 항목
    _excluded_section(L, state, fx, ignore)

    # 7-1. 프로젝트 파일에서 찾은 취약점(report 모드)
    _files_section(L, fx)

    # 8. 변경하지 않은 것
    _unchanged_section(L, state, fx)

    # 9. 처리 기록(접힘)
    _record_section(L, state, fx, paths)
    return "\n".join(_clean(x, roots) for x in L)


def write_report(state: RemediationState, paths: RunPaths, *, ignore_actions: Iterable[str] | None = None,
                 inhouse_message: str | None = None, severity: tuple[float, float, float] | None = None,
                 inhouse: Mapping[str, str] | None = None, file_components: str | None = None) -> None:
    """``report.json``, ``report.md``, ``fix.patch``를 실행 폴더에 원자적으로 쓴다.
    ``fix.patch``는 모든 자동 수정을 모은 파일이다.
    ``ignore_actions``가 None이면 기본값(빈 목록)을 쓴다."""
    write_fix_patch(state, paths)
    write_json_atomic(paths.report_json, build_report_json(state, paths, ignore_actions=ignore_actions,
                                                           inhouse_message=inhouse_message, severity=severity,
                                                           inhouse=inhouse, file_components=file_components))
    text = build_report_md(state, paths, ignore_actions=ignore_actions, inhouse_message=inhouse_message,
                           severity=severity, inhouse=inhouse, file_components=file_components)
    tmp = paths.report_md.with_name(paths.report_md.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    fsops.replace(tmp, paths.report_md)


def summary(state: RemediationState) -> dict[str, Any]:
    """API 목록에 보여 줄 요약 dict를 만든다."""
    d = state.delivery
    return {"id": state.id, "name": state.name, "status": state.status, "reason": state.reason,
            "reasons": list(state.reasons), "node": state.node, "next_node": state.next_node, "round": state.round,
            "created": state.created, "updated": state.updated, "counts": dict(sorted(state.counts.items())),
            "baseline_tolerated": bool(state.baseline_tolerated),
            "delivery": {"sha256": d.sha256, "size": d.size, "round": d.round, "build_ok": d.build_ok,
                         "scanned": d.scanned, "build_tolerated": d.build_tolerated} if d else None}


def fix_summary(state: RemediationState) -> dict[str, Any] | None:
    """화면에 보여 줄 빌드 오류 자동 수정 요약을 만든다. 수정 루프를 실행한 라운드가 없으면 None이다.
    ``changed``는 확정한 라운드에서 고친 소스 파일 수다. ``llm_calls``는 모든 라운드의 수정 루프가 호출한 LLM 횟수다.

    The page's digest of the build fix loop: None when no round ran it; `fixed` / `changed` count only rounds committed
    with a fix that changed files (`_fixed_rounds`; a retry that passed changes nothing), `llm_calls` sums every
    loop of every round, the discarded ones (`fix_discarded`) included, `round` = the last such round. `changed` is
    the number of distinct files: a file fixed in two rounds counts once. `llm_usage_unknown: true` (only then): a
    part of that usage is unknown, `llm_calls` counts the known calls."""
    fixes = [f for rs in sorted(state.rounds, key=lambda x: x.round)
             for f in [*_discarded_of(rs), _fix_of(rs)] if f is not None]
    if not fixes:
        return None
    done = _fixed_rounds(state)
    # 버린 수정도 LLM을 호출했으니 함께 센다. 두 번 세지 않도록 라운드의 fix와 fix_discarded만 더한다
    out = {"fixed": bool(done), "changed": len(_fixed_files(state)),
           "llm_calls": sum(int(getattr(f, "llm_calls", 0) or 0) for f in fixes),
           "round": done[-1].round if done else None}
    if any(_usage_unknown(f) or _tokens_unknown(f) for f in fixes):
        # 사용량 일부를 알 수 없다. llm_calls는 아는 만큼의 수다
        out["llm_usage_unknown"] = True
    return out


__all__ = ["FIX_PATCH_NAME", "build_report_json", "build_report_md", "build_unconfirmed", "fix_patch_path", "fix_summary",
           "served_fix_round", "summary", "write_fix_patch", "write_report"]
