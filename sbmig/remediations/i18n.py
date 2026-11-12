"""취약점 조치의 사람용 한국어 표시: 코드(출처·변경 종류·주의·결정·이유·검사 결과 종류)의 한국어 이름표와, 조치 코드가 만든 영어
문장(잔여 설명·인벤토리 사유·제안 메모·검증 메모·상태 메시지·준비 문제·빌드 설정 점검)을 한국어 문장으로 바꾸는 규칙표.
흐름상 위치: ``report.build_report_md`` (report.md)와 ``service.proposals`` (화면의 ``*_ko`` 필드)가 부른다. 저장되는 자료
(state.json · plan.json · report.json · HTTP API 의 기존 필드)는 영어 문장과 코드 그대로다 — 그것이 계약이고, 이 모듈은 보여 줄
때만 바꾼다.
문체(사용자 요청 2026-11-12, "기계번역처럼 읽힌다"): 합니다체, 한 문장에 사실 하나, 국내 개발 조직이 실제로 쓰는 말(조치·적용·
반영·제외·의존 경로·간접 의존성·버전 지정·담당자). pom · BOM · classifier · dependencyManagement · property · scope · jar 는
영어 그대로. 의인화·공간 비유(다스리다·닿다·들어온 길·글자)는 쓰지 않는다. 용어표는 ``agent-doc/12-remediation-report.md``.
지켜야 할 것: ``ko_text`` 는 규칙(정규식)이 모르는 문장을 **그대로** 돌려준다(정보를 버리거나 추측하지 않는다). 보고서용
``ko_display`` 는 모르는 조각을 "원문: `…`" 으로 감싸 깨진 한국어처럼 보이지 않게 한다. g:a · 버전 · 모듈 이름 · 파일 경로
· 제안 ID 는 글자 그대로 남긴다(pom 의 줄 번호는 "41행"). Maven 자체의 출력(빌드 요약)은 번역하지 않는다. "; " 또는 " | " 로
이어진 문장은 조각마다 바꾸되, 한 규칙이 "; " 를 품은 문장 전체를 말하면(가장 긴 조각부터 맞춰 본다) 그 규칙이 이긴다.

Korean presentation for the remediation: labels for the codes and pattern rules that turn the English sentences the
remediation code produces into Korean. Stored data keeps its English text and codes (the contract); `ko_text` returns a
sentence no rule knows unchanged, `ko_display` (the report) shows such a piece as "원문: `…`"; identifiers (g:a, versions,
modules, paths, proposal ids) are kept verbatim.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------- 이름표 / labels
ORIGIN_KO: dict[str, str] = {
    "transitive": "간접 의존성", "direct_literal": "직접 의존성 (pom에 버전 표기)",
    "direct_property": "직접 의존성 (property로 버전 지정)", "own_management": "프로젝트의 dependencyManagement",
    "parent_managed": "부모 pom에서 버전 지정", "bom_managed": "BOM에서 버전 지정", "plugin": "빌드 플러그인",
    "vendored": "프로젝트에 포함된 jar 파일", "unmapped": "의존성 트리에서 찾지 못함", "proposal": "적용하지 못한 제안",
}
# 묶음의 모든 행이 같은 출처일 때 표 위에 한 번 쓰는 문장(출처 칸을 빼고)
ORIGIN_ALL_KO: dict[str, str] = {
    "transitive": "아래 항목은 모두 간접 의존성입니다.",
    "direct_literal": "아래 항목은 모두 pom에 버전을 적은 직접 의존성입니다.",
    "direct_property": "아래 항목은 모두 property로 버전을 지정한 직접 의존성입니다.",
    "own_management": "아래 항목은 모두 프로젝트의 dependencyManagement에서 버전을 지정합니다.",
    "parent_managed": "아래 항목은 모두 부모 pom에서 버전을 지정합니다.",
    "bom_managed": "아래 항목은 모두 BOM에서 버전을 지정합니다.",
    "plugin": "아래 항목은 모두 빌드 플러그인입니다.",
    "vendored": "아래 항목은 모두 프로젝트에 포함된 jar 파일입니다.",
    "unmapped": "아래 항목은 모두 의존성 트리에서 찾지 못한 라이브러리입니다.",
    "proposal": "아래 항목은 모두 적용하지 못한 제안입니다.",
}
KIND_KO: dict[str, str] = {
    "direct_version": "pom의 버전 표기 수정", "property_version": "property 값 수정",
    "own_management_version": "dependencyManagement의 버전 수정", "managed_override": "dependencyManagement에 버전 지정 추가",
    "parent_version": "부모 pom 버전 변경", "bom_version": "BOM 버전 변경",
}
RISK_KO: dict[str, str] = {
    "major": "메이저 버전 변경", "parent": "부모 pom 변경", "bom": "BOM 변경", "managed_override": "dependencyManagement 항목 추가",
}
DECISION_KO: dict[str, str] = {"auto": "자동 적용", "pending": "승인 대기", "approved": "승인 후 적용", "rejected": "적용 안 함"}
# 이유 코드(models.R_* + chain_budget) → 짧은 이름표(표·묶음 제목·화면 칩). 무슨 일이 있었고 무엇을 할지는 report._REASON_KO
REASON_KO: dict[str, str] = {
    "baseline_build_failed": "조치 전 빌드 실패", "candidate_build_failed": "수정본 빌드 실패", "scan_incomplete": "검사 미완료",
    "upload_uncertain": "업로드 결과 확인 불가", "zip_hash_mismatch": "검사한 파일 불일치",
    "text_only_violation": "버전 표기 외 변경 감지", "resolution_mismatch": "적용 결과가 계획과 다름",
    "execution_boundary_refused": "빌드 설정 점검에서 중단", "not_ready": "실행 환경 미비", "pending_approval": "승인 대기",
    "rejected_proposals": "적용하지 않은 변경", "residual_findings": "남은 취약점", "manual_review": "직접 확인 필요",
    "no_candidate_version": "올릴 수 있는 버전 없음", "rounds_exhausted": "반복 횟수 한도 도달", "stop_requested": "중지 요청",
    "internal_error": "내부 오류", "policy_denied": "정책상 허용되지 않음", "unknown_provenance": "버전을 지정한 곳을 찾지 못함",
    "stale_location": "pom이 그 사이 변경됨", "inventory_incomplete": "의존성 목록을 다 읽지 못함",
    "inhouse_artifact": "사내 라이브러리 (담당자 요청)", "chain_budget": "제한 시간 초과",
}
# graph.fill_counts 가 쓰는 집계 키 → 한국어(이 순서로 보여 준다; 모르는 키는 이름 그대로 뒤에)
COUNT_KO: dict[str, str] = {
    "baseline_findings": "조치 전 취약점", "baseline_ignored": "조치 전 제외", "fixed": "조치한 취약점",
    "final_findings": "조치 후 남은 취약점", "final_ignored": "조치 후 제외", "rounds": "라운드", "proposals": "제안",
    "proposals_applied": "적용한 제안", "proposals_pending": "승인 대기 제안", "proposals_rejected": "적용하지 않은 제안",
    "residual": "조치하지 못한 항목", "residual_inhouse": "사내 라이브러리 항목",
}
FINDING_KIND_KO: dict[str, str] = {
    "library": "라이브러리", "file_component": "파일 컴포넌트", "file_component_potential": "파일 컴포넌트(추정)",
    "sw": "소프트웨어(지원하지 않음)",
}
# IVAS 검사가 끝나지 않은 까닭 코드(ivas._Collector.bad) → 한국어
SCAN_REASON_KO: dict[str, str] = {
    "analysis_failed": "IVAS 분석 실패", "analysis_timeout": "분석 대기 시간 초과", "duplicate_result": "같은 결과가 두 번 옴",
    "http_error": "IVAS 응답 오류", "id_mismatch": "분석 ID 불일치", "page_missing": "결과 페이지 누락",
    "stop_requested": "중지 요청", "summary_mismatch": "요약과 상세 결과의 개수가 다름", "total_mismatch": "전체 개수가 바뀜",
    "unsupported_sw": "지원하지 않는 SW 취약점", "zip_hash_mismatch": "분석한 파일 불일치",
}
# 빌드 설정 점검(intake.check_boundary) 코드 → 한국어
BOUNDARY_KO: dict[str, str] = {
    "symlink": "심볼릭 링크", "settings_in_project": "프로젝트 안의 settings.xml", "project_maven_config": ".mvn 설정 파일",
    "maven_extensions": "Maven extension 설정", "maven_wrapper": "Maven wrapper", "foreign_repository": "승인되지 않은 저장소",
    "foreign_plugin_repository": "승인되지 않은 플러그인 저장소", "unparsable_pom": "읽을 수 없는 pom",
}


# 위험도 표시(사용자 요청 2026-11-12: 위험한 취약점에 눈이 가게): (표시, 이름) — 빨강·주황·노랑 순서. Markdown 에는 색이 없어
# 편집기 미리보기에서 보이는 그림 글자를 쓴다
SEVERITY_LEVELS: tuple[tuple[str, str, str], ...] = (("red", "🔴", "심각"), ("orange", "🟠", "높음"), ("yellow", "🟡", "보통"))
SEVERITY_NONE_KO = "낮음·점수 없음"


def severity_thresholds(value: Any = None) -> tuple[float, float, float]:
    """위험도 경계(빨강·주황·노랑의 아래 경계). None 이면 설정 기본값(``RemediationConfig``). 순서가 뒤집히거나 음수여도
    예외를 내지 않는다 — 큰 것부터 정렬해 쓴다. 읽을 수 없는 값이면 기본값.

    The (red, orange, yellow) lower bounds; None = the config defaults; sorted descending, never raises."""
    from sbmig.core.config import RemediationConfig
    d = RemediationConfig()
    default = (float(d.severity_red), float(d.severity_orange), float(d.severity_yellow))
    if value is None:
        return default
    try:
        vals = sorted((float(x) for x in list(value)[:3]), reverse=True)
    except (TypeError, ValueError):
        return default
    if len(vals) != 3:
        return default
    return vals[0], vals[1], vals[2]


def severity_of(score: float | None, thresholds: tuple[float, float, float]) -> tuple[str, str, str]:
    """점수 → (수준 키 red / orange / yellow / none, 표시 🔴 / 🟠 / 🟡 / "", 이름 심각 / 높음 / 보통 / ""). 점수 ≥ 경계면 그
    수준; 노랑 경계보다 낮거나 점수가 없으면 표시 없음.

    Score → (level key, mark, Korean name); below yellow or no score → ("none", "", "")."""
    if score is not None:
        try:
            sc = round(float(score), 1)      # 보고서가 보여 주는 소수 한 자리 값으로 판정한다(8.96 → 9.0 은 빨강)
        except (TypeError, ValueError):
            sc = None
        if sc is not None:
            for (key, mark, name), bound in zip(SEVERITY_LEVELS, thresholds):
                if sc >= bound:
                    return key, mark, name
    return "none", "", ""


def label(table: dict[str, str], code: Any) -> str:
    """이름표 표에서 코드의 한국어 이름(모르는 코드·빈 값은 그대로).

    The Korean label of `code` in `table`; unknown codes are returned as they are."""
    s = "" if code is None else str(code)
    return table.get(s, s)


def evidence_ko(evidence: str) -> str:
    """제안의 근거 글자 → 한국어("policy:pins" → 서버 정책에 지정한 버전, "repository:<호스트>" → 저장소 <호스트>).

    The Korean form of a proposal's evidence text."""
    s = str(evidence or "")
    if s == "policy:pins":
        return "서버 정책에 지정한 버전"
    if s.startswith("policy:"):
        return "서버 정책 " + s[len("policy:"):]
    if s.startswith("repository:"):
        return "저장소 " + s[len("repository:"):]
    return s


def loc_ko(loc: str) -> str:
    """"app/pom.xml:41" → "app/pom.xml 41행" (줄 번호가 없으면 그대로).

    "pom:line" → "pom N행"."""
    m = re.fullmatch(r"(?P<p>.+):(?P<n>\d+)", str(loc or ""))
    return f"{m['p']} {m['n']}행" if m else str(loc or "")


# ---------------------------------------------------------------------------- 값 변환 도우미
def _none(x: str | None) -> str:
    """도구가 '없음' 자리에 쓰는 영어 낱말 → 없음."""
    x = (x or "").strip()
    return "없음" if x in ("", "nothing", "none", "absent", "no version") else x


_MORE = re.compile(r" \(\+(\d+) more\)")


def _vlist(x: str | None) -> str:
    """버전 목록 글자의 꼬리표만 한국어로(버전 글자는 그대로)."""
    s = _none(x)
    s = _MORE.sub(r" 외 \1개", s)
    s = s.replace(" (deny_versions)", " (서버 정책에서 금지)").replace(" (prerelease)", " (시험판)")
    s = s.replace("(< patched hint ", "(패치 버전 미만: ")
    return s


_KW = {"parent": "부모 pom", "BOM": "BOM", "transitive dependency": "간접 의존성",
       "managed version": "버전이 지정된 의존성"}


def _kw(x: str | None) -> str:
    return _KW.get(x or "", x or "")


def _origin(x: str | None) -> str:
    return label(ORIGIN_KO, x)


def _places(x: str) -> str:
    """루트 pom 관리 항목 자리 목록("top level, profile dev") → 한국어."""
    return ", ".join("최상위" if p.strip() == "top level" else re.sub(r"^profile (.+)$", r"\1 프로필", p.strip())
                     for p in x.split(", "))


def _what(x: str) -> str:
    """"parent g:a:v, BOM g:a:v" → "부모 pom g:a:v, BOM g:a:v"."""
    return re.sub(r"(^|, )parent ", r"\1부모 pom ", x)


def _desc(x: str) -> str:
    """인벤토리의 "<origin>[ by <관리자>][ via <사슬>]" → 한국어."""
    m = re.fullmatch(r"(?P<o>[a-z_]+)(?: by (?P<by>.+?))?(?: via (?P<via>.+))?", x or "")
    if not m:
        return x
    out = _origin(m["o"])
    if m["by"]:
        out += f", 버전 지정: {m['by']}"
    if m["via"]:
        out += f", 의존 경로: {m['via']}"
    return out


def _left(x: str) -> str:
    s = x.replace("target/ build output", "target/ 빌드 결과물")
    return s.replace("ignored folders ", "접수할 때 제외한 폴더 ")


def _where(x: str) -> str:
    x = (x or "").strip()
    if x == "(at the project root)":
        return "(프로젝트 루트)"
    m = re.fullmatch(r"\(build targets: (.+)\)", x)
    return f"(빌드 대상: {m[1]})" if m else x


def _inc(x: str | None) -> str:
    return " Maven 의존성 분석은 완전하지 않습니다." if x else ""


def _raw(x: str | None) -> str:
    """번역하지 않는 안쪽 글자(도구 내부 오류·Maven 메시지)를 원문 표시로 감싼다."""
    s = re.sub(r"\s+", " ", str(x or "")).replace("`", "'").strip()
    if len(s) > 500:
        s = s[:500] + "…"
    return f"원문: `{s}`"


_STATE_KO = {"QUEUED": "대기 중", "RUNNING": "진행 중", "AWAITING_APPROVAL": "승인 대기",
             "AWAITING_CONFIRMATION": "업로드 확인 필요", "STOPPED": "중지됨", "INTERRUPTED": "중단됨"}


def _state(x: str) -> str:
    """상태 코드 → 한국어(모르는 코드는 그대로)."""
    return _STATE_KO.get(x, x)


def _scan_reasons(x: str) -> str:
    """검사 미완료 사유 코드 목록("page_missing, total_mismatch") → 한국어(모르는 코드는 그대로)."""
    return ", ".join(label(SCAN_REASON_KO, c.strip()) for c in (x or "").split(",") if c.strip())


# ---------------------------------------------------------------------------- 규칙표 / rules
# 변수 조각: "; " 를 품지 않는 글자들. 문장 끝을 알 수 없는 ``.+`` 를 그대로 쓰면 뒤에 이어 붙은 다른 문장까지 삼킨다
_A = r"(?:(?!; ).)+?"


@dataclass(frozen=True)
class _Group:
    """잔여 설명 묶음 하나: 짧은 제목, 행마다의 변수 없이 한 번만 쓰는 한국어 설명, 메모 칸이 필요한가(행마다 다른 사실이 있다)."""
    title: str
    text: str
    memo: bool = True


@dataclass(frozen=True)
class _Rule:
    key: str
    rx: re.Pattern[str]
    ko: Callable[[dict[str, str]], str]
    group: str = ""


GROUPS: dict[str, _Group] = {
    "not_auto_origin": _Group("자동 수정 대상이 아님",
                              "이 항목은 자동으로 수정하지 않습니다. 담당 팀에서 직접 확인해 주세요.", False),
    "policy_deny": _Group("서버 정책에서 변경 금지", "서버 정책에서 변경을 금지한 라이브러리라 자동으로 수정하지 않습니다.", False),
    "unknown_origin": _Group("버전 지정 위치를 알 수 없음", "버전을 어디서 지정하는지 알 수 없어 자동으로 수정하지 않습니다.", False),
    "no_location": _Group("버전을 지정한 곳을 찾지 못함", "프로젝트 pom에서 버전을 지정한 곳을 찾지 못했습니다.", False),
    "not_plain": _Group("일반 버전 형식이 아님",
                        "pom의 버전 표기나 Maven이 사용하는 버전이 일반 버전 형식이 아니어서(식, 범위 등) 자동으로 "
                        "수정하지 않습니다."),
    "stale": _Group("pom이 그 사이 변경됨",
                    "계획을 세운 뒤 pom이 변경되어 버전 표기를 다시 찾지 못했습니다. 다시 실행하면 새로 계획합니다."),
    "pin_unusable": _Group("서버 정책에 지정한 버전을 쓸 수 없음", "서버 정책에 지정한 버전을 사용할 수 없습니다."),
    "no_hint": _Group("패치 버전 정보 없음",
                      "IVAS가 패치 버전을 알려 주지 않았고 서버 정책에 지정한 버전도 없어, 올릴 버전을 정하지 못했습니다.",
                      False),
    "hint_not_plain": _Group("패치 버전이 일반 버전 형식이 아님",
                             "IVAS가 알려 준 패치 버전이 일반 버전 형식이 아니어서(범위 등) 자동으로 고르지 않았습니다."),
    "only_major": _Group("메이저 버전 변경만 가능",
                         "패치된 버전이 다음 메이저 버전에만 있고, 서버 정책이 메이저 버전 변경을 허용하지 않습니다."),
    "no_repo_version": _Group("저장소에 올릴 수 있는 버전 없음", "승인된 저장소에 패치 버전 조건을 만족하는 버전이 없습니다."),
    "no_evidence": _Group("저장소에서 버전 확인 실패", "올릴 버전은 정했지만 승인된 저장소에서 그 버전을 확인하지 못했습니다."),
    "parent_pin_no_text": _Group("서버 정책의 부모 pom·BOM 버전을 적용할 곳 없음",
                                 "서버 정책이 부모 pom 또는 BOM의 버전을 지정했지만, 프로젝트 pom에 그 버전 표기가 없습니다."),
    "parent_deny": _Group("부모 pom·BOM 변경 금지", "버전을 지정하는 부모 pom 또는 BOM을 서버 정책에서 변경 금지했습니다."),
    "root_entry_exists": _Group("루트 pom에 버전 지정이 이미 있음",
                                "루트 pom의 dependencyManagement에 이 라이브러리 항목이 이미 있어 새로 추가하지 않습니다."),
    "variant": _Group("classifier가 붙은 의존성",
                      "jar가 아닌 형식이거나 classifier가 붙은 의존성은 버전을 지정하는 방식으로 조치할 수 없습니다."),
    "root_unreadable": _Group("루트 pom을 읽을 수 없음",
                              "루트 pom.xml을 읽을 수 없어 dependencyManagement에 버전 지정을 추가하지 못했습니다.", False),
    "cannot_place": _Group("버전 지정을 추가할 위치를 정하지 못함",
                           "dependencyManagement에 버전 지정을 추가할 위치를 정하지 못했습니다."),
    "not_inheriting_root": _Group("루트 pom을 상속하지 않는 모듈",
                                  "이 모듈들은 프로젝트의 루트 pom이 아니라 회사 부모 pom을 상속합니다. 루트 pom에서 버전을 "
                                  "지정해도 이 모듈들에는 적용되지 않아 자동으로 조치하지 못했습니다.", False),
    "declared_wins": _Group("모듈 pom에서 버전을 직접 지정",
                            "모듈 pom에서 버전을 직접 지정하고 있어, 루트 pom의 dependencyManagement 로는 바뀌지 않습니다."),
    "rejected_earlier": _Group("이전에 적용하지 않기로 한 변경", "이전에 적용하지 않기로 한 변경이라 다시 제안하지 않습니다."),
    "inventory_incomplete": _Group("의존성 목록을 다 읽지 못함",
                                   "의존성 목록을 다 읽지 못해 조치 계획을 세우지 않았습니다.", False),
    "inhouse_self": _Group("사내 라이브러리",
                           "사내 라이브러리 자체의 취약점입니다. 조치된 버전은 그 라이브러리 담당자가 배포해야 합니다.", False),
    "inhouse_via": _Group("사내 라이브러리가 가져오는 의존성",
                          "사내 라이브러리나 회사 부모 pom을 통해 가져오는 의존성입니다. 버전은 그 담당자만 변경할 수 있습니다.",
                          False),
    "not_applied": _Group("적용하지 못한 제안", "제안을 적용하지 못했습니다. 그 사이 파일이 변경되었을 수 있습니다."),
    "external_manager": _Group("프로젝트 밖에서 버전 지정", "프로젝트 밖의 부모 pom 이나 BOM에서 버전을 지정합니다."),
    "ambiguous": _Group("버전 지정 위치가 분명하지 않음", "어느 부모 pom 또는 BOM에서 버전을 지정하는지 분명하지 않습니다."),
    "no_manager": _Group("버전 선언도 지정하는 곳도 없음",
                         "프로젝트에 버전 선언이 없고, 버전을 지정하는 부모 pom 이나 BOM도 없습니다.", False),
    "text_not_located": _Group("버전 표기를 한 줄로 찾지 못함", "pom에서 버전 표기를 한 줄로 찾지 못했습니다."),
    "declared_vs_resolved": _Group("선언한 버전과 실제 버전이 다름",
                                   "pom에 선언한 버전과 Maven이 실제로 사용하는 버전이 다릅니다."),
    "external_property": _Group("버전 property가 프로젝트 밖에 있음", "버전 property가 프로젝트 pom 밖에서 정의되어 있습니다."),
    "builtin_expr": _Group("Maven 기본 property·식으로 지정한 버전",
                           "버전이 Maven 기본 property 나 식으로 지정되어 자동으로 수정하지 않습니다."),
    "also_resolved": _Group("다른 모듈은 다른 경로로 사용", "다른 모듈에서도 같은 버전을 다른 경로로 가져옵니다."),
    "pom_unreadable": _Group("모듈 pom을 읽을 수 없음", "모듈의 pom을 읽을 수 없습니다."),
    "vendored": _Group("프로젝트에 포함된 jar 파일",
                       "프로젝트에 직접 포함된 파일(jar, js 등)이라 자동으로 수정하지 않습니다. 담당 팀에서 파일을 교체해 "
                       "주세요.", False),
    "kind_unsupported": _Group("자동 조치 대상이 아닌 검사 결과", "자동 조치에서 다루지 않는 종류의 검사 결과입니다."),
    "ident_not_ga": _Group("Maven 좌표가 아닌 식별자", "IVAS 식별자를 Maven 좌표(groupId:artifactId)로 읽을 수 없습니다."),
    "plugin": _Group("빌드 플러그인",
                     "빌드 플러그인이나 플러그인의 의존성이라 자동으로 수정하지 않습니다. 담당 팀에서 플러그인 버전을 올려 "
                     "주세요."),
    "ivas_vs_maven": _Group("IVAS 버전과 Maven 버전이 다름",
                            "IVAS가 보고한 버전을 Maven이 실제로 사용하는 버전과 연결하지 못했습니다."),
}


def _r(key: str, pattern: str, ko: Callable[[dict[str, str]], str], group: str = "") -> _Rule:
    return _Rule(key, re.compile(pattern.replace("{A}", _A)), ko, group)


_VARIANT_PAREN = re.compile(r"type/classifier variant \((?P<v>[^()]*)\)")
_VARIANT_IN = re.compile(r"type/classifier variant in (?P<m>.+)")


def _variant_parts(why: str) -> tuple[list[str], list[str]]:
    """변종 사유 조각들 → (변종 사실 "jar/tests" · "<모듈> 모듈", 그 밖의 조각 원문)."""
    facts, others = [], []
    for piece in (why or "").split("; "):
        m = _VARIANT_PAREN.fullmatch(piece)
        n = _VARIANT_IN.fullmatch(piece)
        if m:
            facts.append(m["v"])
        elif n:
            facts.append(f"{n['m']} 모듈")
        elif piece:
            others.append(piece)
    return facts, others


def _variant_sentence(kw: str, ga: str, why: str) -> str:
    """버전 지정을 막은 변종 사유를 한 번만 말한다: 변종 사실은 괄호에, 그 밖의 사유 조각은 번역해 뒤 문장으로."""
    facts, others = _variant_parts(why)
    head = f"{_kw(kw)} {ga}의 형식이 jar가 아니거나 classifier가 붙어 있습니다"
    head += f"({', '.join(facts)})." if facts else "."
    return " ".join([head, *(ko_display(o) for o in others), "dependencyManagement의 버전 지정으로는 조치할 수 없습니다."])


def _why(x: str | None) -> str:
    """안쪽 문장(이유)을 다시 번역해 앞 문장 뒤에 붙일 글: " 이유: …" (+ 모르는 조각은 " 원문: `…`")."""
    ko, raw = split_known(x or "")
    out = (" 이유: " + " ".join(ko)) if ko else ""
    if raw:
        out += " " + _raw("; ".join(raw))
    return out


# 순서가 중요하다: 같은 문장에 둘이 맞으면 앞의 것이 이긴다(예: root_unreadable 이 cannot_place 보다 앞)
RULES: list[_Rule] = [
    # ---- planner: 잔여 설명
    _r("not_changed_auto", r"(?P<origin>[a-z_]+): not changed automatically",
       lambda g: f"{_origin(g['origin'])} 항목은 자동으로 수정하지 않습니다.", "not_auto_origin"),
    _r("policy_deny", r"(?P<ga>\S+) matches policy\.deny",
       lambda g: f"{g['ga']} 라이브러리는 서버 정책에서 변경을 금지했습니다.", "policy_deny"),
    _r("unknown_origin", r"unknown origin '(?P<origin>[^']*)'",
       lambda g: f"출처 코드('{g['origin']}')를 알 수 없어 자동으로 수정하지 않습니다.", "unknown_origin"),
    _r("no_location", r"no version location in the project poms",
       lambda g: "프로젝트 pom에서 버전을 지정한 곳을 찾지 못했습니다.", "no_location"),
    _r("not_plain_text", r"(?P<loc>\S+:\d+) version text '(?P<t>{A})' is not a plain version",
       lambda g: f"{loc_ko(g['loc'])}의 버전 표기('{g['t']}')는 일반 버전 형식이 아닙니다.", "not_plain"),
    _r("parent_not_plain", r"(?P<loc>\S+:\d+) (?P<kw>parent|BOM) version '(?P<t>{A})' is not plain",
       lambda g: f"{loc_ko(g['loc'])}의 {_kw(g['kw'])} 버전('{g['t']}')은 일반 버전 형식이 아닙니다.", "not_plain"),
    _r("resolved_not_plain", r"resolved version '(?P<v>{A})' is not a plain version",
       lambda g: f"Maven이 사용하는 버전('{g['v']}')은 일반 버전 형식이 아닙니다.", "not_plain"),
    _r("stale_gone", r"(?P<loc>\S+:\d+) no longer exists or cannot be read",
       lambda g: f"{loc_ko(g['loc'])}이 없거나 읽을 수 없습니다. 계획을 세운 뒤 파일이 변경된 것으로 보입니다.", "stale"),
    _r("stale_count", r"(?P<loc>\S+:\d+) contains '(?P<t>{A})' (?P<n>\d+) time\(s\), expected exactly once \(file changed\?\)",
       lambda g: (f"{loc_ko(g['loc'])}에 '{g['t']}' 표기가 {g['n']}번 있습니다. 한 번만 있어야 합니다. 계획을 세운 뒤 파일이 "
                  "변경된 것으로 보입니다."), "stale"),
    _r("different_texts", r"(?P<loc>\S+:\d+): inventory names different version texts for one line",
       lambda g: f"{loc_ko(g['loc'])}의 버전 표기를 의존성 목록에서 서로 다르게 읽었습니다.", "stale"),
    _r("pin_not_newer", r"policy pin (?P<pin>\S+) is not newer than the current (?P<cur>\S+)",
       lambda g: f"서버 정책에 지정한 {g['pin']} 버전이 현재 버전({g['cur']})보다 높지 않습니다.", "pin_unusable"),
    _r("pin_denied", r"policy pin (?P<pin>\S+) is also listed in policy\.deny_versions",
       lambda g: f"서버 정책에 지정한 {g['pin']} 버전이 금지 버전 목록에도 있습니다.", "pin_unusable"),
    _r("pin_major", r"policy pin (?P<pin>\S+) is a major change and policy\.allow_major is false",
       lambda g: (f"서버 정책에 지정한 {g['pin']} 버전은 메이저 버전 변경입니다. 서버 정책이 메이저 버전 변경을 허용하지 "
                  "않습니다."), "pin_unusable"),
    _r("pin_for_not_newer", r"policy pin (?P<pin>\S+) for (?P<mga>\S+) is not newer than (?P<cur>\S+)",
       lambda g: f"서버 정책에 지정한 {g['mga']} 버전({g['pin']})이 현재 버전({g['cur']})보다 높지 않습니다.", "pin_unusable"),
    _r("pin_for_major", r"policy pin (?P<pin>\S+) for (?P<mga>\S+) is a major change and policy\.allow_major is false",
       lambda g: (f"서버 정책에 지정한 {g['mga']} 버전({g['pin']})은 메이저 버전 변경입니다. 서버 정책이 메이저 버전 변경을 "
                  "허용하지 않습니다."), "pin_unusable"),
    _r("pin_below_hint", r"policy pin (?P<pin>\S+) is below the IVAS patched hint (?P<hint>.+)",
       lambda g: f"서버 정책에 지정한 버전({g['pin']})이 IVAS가 알려 준 패치 버전({g['hint']})보다 낮습니다."),
    _r("no_hint", r"no patched version known \(IVAS gave none\) and no policy pin",
       lambda g: "IVAS가 패치 버전을 알려 주지 않았고, 서버 정책에 지정한 버전도 없습니다.", "no_hint"),
    _r("hint_not_plain", r"IVAS patched version hint\(s\) (?P<h>.+) are not plain versions; not chosen automatically",
       lambda g: f"IVAS가 알려 준 패치 버전({g['h']})이 일반 버전 형식이 아니어서 자동으로 고르지 않았습니다.",
       "hint_not_plain"),
    _r("only_major", r"only major upgrades are available \((?P<vs>.+)\) and policy\.allow_major is false",
       lambda g: (f"메이저 버전 변경({_vlist(g['vs'])})만 가능합니다. 서버 정책이 메이저 버전 변경을 허용하지 않습니다."),
       "only_major"),
    _r("repo_listed",
       r"repository listed (?P<listed>.+?); excluded: (?P<excl>.+?); needed > (?P<cur>\S+) and ≥ patched hint (?P<hint>.+?)"
       r"(?:; the hinted (?P<miss>.+) (?:is|are) not in the repository)?",
       lambda g: (f"저장소에 있는 버전: {_vlist(g['listed'])}. 제외한 버전: {_vlist(g['excl'])}. 필요한 버전: {g['cur']} 보다 "
                  f"높고 패치 버전 {g['hint']} 이상."
                  + (f" IVAS가 알려 준 버전 중 {g['miss']} 버전은 저장소에 없습니다." if g.get("miss") else "")), "no_repo_version"),
    _r("no_evidence", r"the repository gave no evidence for (?P<t>\S+)",
       lambda g: f"승인된 저장소에서 {g['t']} 버전을 확인하지 못했습니다.", "no_evidence"),
    _r("parent_pin_no_text",
       r"policy pins the (?P<kw>parent|BOM) (?P<mga>\S+) at (?P<pin>\S+), but its version text is not in the project poms",
       lambda g: (f"서버 정책이 {_kw(g['kw'])} {g['mga']}의 버전을 {g['pin']} 버전으로 지정했지만, 프로젝트 pom에 그 버전 "
                  "표기가 없습니다."), "parent_pin_no_text"),
    _r("parent_denied", r"the (?P<kw>parent|BOM) (?P<mga>\S+) matches policy\.deny",
       lambda g: f"서버 정책에서 {_kw(g['kw'])} {g['mga']}의 변경을 금지했습니다.", "parent_deny"),
    _r("root_entries_many",
       r"the root pom\.xml already has <dependencyManagement> entries for (?P<ga>\S+) \((?P<places>{A})\); "
       r"a second entry is never added",
       lambda g: (f"루트 pom.xml의 dependencyManagement에 {g['ga']} 항목이 이미 여러 곳에 있습니다"
                  f"({_places(g['places'])}). 항목을 더 추가하지 않습니다."), "root_entry_exists"),
    _r("root_entry_mismatch",
       r"the root pom\.xml already has a <dependencyManagement> entry for (?P<ga>\S+) whose version \((?P<v>{A})\) "
       r"is not the resolved (?P<res>\S+) as plain text; a second entry is never added",
       lambda g: (f"루트 pom.xml의 dependencyManagement에 {g['ga']} 항목이 이미 있습니다. 그 버전({_none(g['v'])})은 Maven이 "
                  f"사용하는 버전({g['res']})과 같은 일반 버전 표기가 아닙니다. 항목을 더 추가하지 않습니다."),
       "root_entry_exists"),
    _r("variant",
       r"(?P<kw>transitive dependency|managed version) (?P<ga>\S+) resolves as a non-jar type or with a classifier "
       r"\((?P<why>.+)\); a plain override entry would not govern it",
       lambda g: _variant_sentence(g["kw"], g["ga"], g["why"]), "variant"),
    _r("root_unreadable", r"cannot place an override entry safely: the root pom\.xml is not readable",
       lambda g: "루트 pom.xml을 읽을 수 없어 dependencyManagement에 버전 지정을 추가하지 못했습니다.", "root_unreadable"),
    # 안쪽 오류(apply.EditError)는 도구 내부 문장이라 원문으로 둔다
    _r("cannot_place", r"cannot place an override entry safely: (?P<e>.+)",
       lambda g: f"dependencyManagement에 버전 지정을 추가할 위치를 정하지 못했습니다. {_raw(g['e'])}", "cannot_place"),
    _r("not_inheriting_root",
       r"module\(s\) (?P<mods>{A}) do not inherit from the root pom\.xml: an override entry there would not reach them",
       lambda g: (f"{g['mods']} 모듈은 루트 pom.xml을 상속하지 않습니다. 루트 pom에서 버전을 지정해도 이 모듈들에는 "
                  "적용되지 않습니다."), "not_inheriting_root"),
    _r("declared_wins",
       r"(?P<ga>\S+) is declared with a version in (?P<places>{A}): a declared version wins over <dependencyManagement>, "
       r"so an override entry would not change it",
       lambda g: (f"{g['places']}에서 {g['ga']}의 버전을 직접 지정합니다. 직접 지정한 버전이 dependencyManagement보다 "
                  "우선하므로, dependencyManagement의 버전 지정으로는 바뀌지 않습니다."), "declared_wins"),
    _r("rejected_earlier", r"(?P<ga>\S+) → (?P<v>\S+) was rejected in an earlier decision; not proposed again",
       lambda g: f"{g['ga']} → {g['v']} 변경은 이전에 적용하지 않기로 한 것이라 다시 제안하지 않습니다.", "rejected_earlier"),
    _r("inventory_incomplete_plan", r"inventory incomplete: nothing planned \((?P<n>.*)\)",
       lambda g: "의존성 목록을 다 읽지 못해 조치 계획을 세우지 않았습니다." + _why(g["n"])),
    _r("inventory_incomplete", r"inventory incomplete",
       lambda g: "의존성 목록을 다 읽지 못했습니다.", "inventory_incomplete"),
    _r("inventory_incomplete_msg", r"inventory incomplete: (?P<n>.+)",
       lambda g: "의존성 목록을 다 읽지 못했습니다." + _why(g["n"])),
    _r("inhouse_self", r"(?P<ga>\S+) is an in-house artifact: its owner publishes the fixed version",
       lambda g: f"{g['ga']} 라이브러리는 사내 라이브러리입니다. 조치된 버전은 그 라이브러리 담당자가 배포해야 합니다.", "inhouse_self"),
    _r("inhouse_via", r"brought in by the in-house artifact\(s\) (?P<gas>{A}): only their owner can change it",
       lambda g: f"사내 라이브러리({g['gas']})에서 가져오는 의존성입니다. 버전은 그 라이브러리 담당자만 변경할 수 있습니다.",
       "inhouse_via"),
    # 안쪽 이유(apply.EditError)는 도구 내부 문장이라 원문으로 둔다
    _r("not_applied", r"proposal (?P<pid>\S+) not applied: (?P<why>.+)",
       lambda g: f"제안({g['pid']})을 적용하지 못했습니다. {_raw(g['why'])}", "not_applied"),
    # ---- planner: 제안 메모 / plan notes
    _r("prop_governs", r"property \$\{(?P<p>[^}]*)\} may govern other artifacts",
       lambda g: f"property ${{{g['p']}}}에서 다른 라이브러리의 버전도 지정할 수 있습니다."),
    _r("also_resolved_edit",
       r"also resolved in (?P<mods>{A}) through another origin; this edit may not govern them — "
       r"whatever stays at (?P<cur>\S+) is planned in the next round",
       lambda g: (f"{g['mods']} 모듈은 같은 라이브러리를 다른 경로로 가져옵니다. 이 모듈들에 {g['cur']} 버전이 남아 있으면 다음 "
                  "라운드에서 다시 조치합니다.")),
    _r("raises_parent",
       r"raises the (?P<kw>parent|BOM) (?P<mga>\S+) (?P<cur>\S+) → (?P<pin>\S+) \(server pin\) for (?P<gav>\S+); "
       r"the versions it manages may change too",
       lambda g: (f"{g['gav']} 취약점을 조치하려고 {_kw(g['kw'])} {g['mga']}의 버전을 {g['cur']}에서 {g['pin']} 버전으로 올립니다(서버 정책에 "
                  f"지정한 버전). 이 {_kw(g['kw'])}에서 버전을 지정하는 다른 라이브러리도 함께 바뀔 수 있습니다.")),
    _r("uses_existing", r"uses the existing <dependencyManagement> entry of the root pom\.xml",
       lambda g: "루트 pom.xml의 dependencyManagement에 이미 있는 항목의 버전을 수정합니다."),
    _r("override_transitive",
       r"adds a <dependencyManagement> entry (?P<gav>\S+) to the root pom\.xml: pins the transitive dependency brought "
       r"by (?P<chain>{A}) \((?P<a>\S+) → (?P<b>\S+)\)",
       lambda g: (f"루트 pom.xml의 dependencyManagement에 {g['gav']} 항목을 추가해 간접 의존성의 버전을 지정합니다"
                  f"({g['a']} → {g['b']}). 의존 경로: "
                  f"{'다른 의존성' if g['chain'] == 'another dependency' else g['chain']}.")),
    _r("override_managed",
       r"adds a <dependencyManagement> entry (?P<gav>\S+) to the root pom\.xml: overrides the version managed by "
       r"(?P<mgr>{A}) \((?P<a>\S+) → (?P<b>\S+)\)",
       lambda g: (f"루트 pom.xml의 dependencyManagement에 {g['gav']} 항목을 추가해, "
                  f"{'프로젝트 밖의 부모 pom 또는 BOM' if g['mgr'] == 'a parent / BOM outside the project' else g['mgr']}"
                  f"에서 지정한 버전 대신 사용합니다({g['a']} → {g['b']}).")),
    _r("override_warning",
       r"later upgrades of the parent / BOM / direct dependency will NOT move this version any more — "
       r"the owners must remove the override when the parent / BOM / direct dependency catches up",
       lambda g: ("이 항목을 추가하면 나중에 부모 pom, BOM, 직접 의존성의 버전을 올려도 이 버전은 바뀌지 않습니다. 부모 pom, "
                  "BOM, 직접 의존성이 이 버전 이상으로 올라가면 담당자가 이 항목을 삭제해야 합니다.")),
    _r("override_approval",
       r"always needs approval: an inserted <dependencyManagement> entry is never applied automatically",
       lambda g: "dependencyManagement에 추가하는 항목은 자동으로 적용하지 않습니다. 항상 승인이 필요합니다."),
    _r("merge_covers", r"one (?P<w>override entry|edit) also covers (?P<others>{A}) \((?:same artifact|same version line)\)",
       lambda g: (f"추가하는 항목 하나로 {g['others']}도 함께 조치합니다(같은 라이브러리)." if g["w"] == "override entry"
                  else f"한 번의 수정으로 {g['others']}도 함께 조치합니다(같은 버전 표기).")),
    _r("merge_target", r"target (?P<v>\S+) chosen for (?P<ga>\S+) \(highest of the merged targets\)",
       lambda g: f"{g['ga']}의 목표 버전은 함께 조치하는 대상 가운데 가장 높은 {g['v']}입니다."),
    _r("pending_note", r"(?P<n>\d+) proposal\(s\) wait for approval of their exact diff",
       lambda g: f"제안 {g['n']}건이 승인을 기다립니다."),
    # ---- inventory: 사유
    _r("managed_by_direct", r"managed by (?P<f>\S+), which the project poms do not declare directly",
       lambda g: f"{g['f']}에서 버전을 지정합니다. 프로젝트 pom에는 직접 선언되어 있지 않습니다.", "external_manager"),
    _r("managed_by", r"managed by (?P<f>\S+)",
       lambda g: f"{g['f']}에서 버전을 지정합니다.", "external_manager"),
    _r("ambiguous", r"ambiguous provenance: version may come from (?P<w>{A})",
       lambda g: f"버전을 어디서 지정하는지 분명하지 않습니다. 후보: {_what(g['w'])}", "ambiguous"),
    _r("no_manager", r"no version declared in the project and no parent / BOM to manage it",
       lambda g: "프로젝트에 버전 선언이 없고, 버전을 지정하는 부모 pom 이나 BOM도 없습니다.", "no_manager"),
    _r("text_not_located", r"version text of (?P<ga>\S+) in (?P<pom>\S+) could not be located on one line",
       lambda g: f"{g['pom']}에서 {g['ga']}의 버전 표기를 한 줄로 찾지 못했습니다.", "text_not_located"),
    _r("declares_vs", r"(?P<pom>\S+) declares (?P<ga>\S+) (?P<v>\S+), Maven resolves (?P<res>\S+)",
       lambda g: f"{g['pom']}에는 {g['ga']}의 버전이 {g['v']} 버전으로 선언되어 있지만, Maven은 {g['res']} 버전을 사용합니다.",
       "declared_vs_resolved"),
    _r("prop_not_located", r"property (?P<p>\S+) in (?P<pom>\S+) could not be located on one line",
       lambda g: f"{g['pom']}에서 property {g['p']}의 위치를 한 줄로 찾지 못했습니다.", "text_not_located"),
    _r("prop_value_vs", r"property (?P<p>\S+) in (?P<pom>\S+) is (?P<v>\S+), Maven resolves (?P<res>\S+)",
       lambda g: f"{g['pom']}의 property {g['p']} 값은 {g['v']}이지만, Maven은 {g['res']} 버전을 사용합니다.",
       "declared_vs_resolved"),
    _r("prop_not_defined", r"version property (?P<n>{A}) is not defined in the project poms",
       lambda g: f"버전 property {g['n']}의 정의가 프로젝트 pom에 없습니다.", "external_property"),
    _r("builtin", r"version of (?P<ga>\S+) comes from the built-in property (?P<n>{A})",
       lambda g: f"{g['ga']}의 버전은 Maven 기본 property {g['n']}에서 정해집니다.", "builtin_expr"),
    _r("expression", r"version of (?P<ga>\S+) is the expression (?P<e>.+); manual review",
       lambda g: f"{g['ga']}의 버전은 식으로 지정되어 있습니다(식: {g['e']}). 직접 확인이 필요합니다.", "builtin_expr"),
    _r("also_resolved_inv", r"also resolved in (?P<mods>{A}) \((?P<d>[a-z_]+(?: by {A})?(?: via {A})?)\)",
       lambda g: f"{g['mods']} 모듈에서도 같은 버전을 사용합니다({_desc(g['d'])}).", "also_resolved"),
    _r("variant_paren", r"type/classifier variant \((?P<v>{A})\)",
       lambda g: f"jar가 아닌 형식이거나 classifier가 붙은 의존성으로도 사용됩니다({g['v']}).", "variant"),
    _r("variant_in", r"type/classifier variant in (?P<mods>{A})",
       lambda g: f"{g['mods']} 모듈에서는 jar가 아닌 형식이거나 classifier가 붙은 의존성으로 사용됩니다.", "variant"),
    _r("module_pom_unreadable", r"module (?P<m>\S+): pom not readable \((?P<e>{A})\)",
       lambda g: f"{g['m']} 모듈의 pom을 읽을 수 없습니다({g['e']}).", "pom_unreadable"),
    _r("vendored_dep", r"file in the tree \(not a Maven dependency\); not changed automatically",
       lambda g: "프로젝트에 포함된 파일입니다(Maven 의존성이 아님). 자동으로 수정하지 않습니다.", "vendored"),
    _r("vendored_unresolved", r"file in the tree \(not resolved by Maven\); not changed automatically",
       lambda g: "프로젝트에 포함된 파일입니다(Maven이 사용하지 않음). 자동으로 수정하지 않습니다.", "vendored"),
    _r("file_component", r"file component in the tree; not changed automatically",
       lambda g: "프로젝트에 포함된 파일 컴포넌트입니다. 자동으로 수정하지 않습니다.", "vendored"),
    _r("kind_unsupported", r"finding kind (?P<k>{A}) is not supported by the remediation",
       lambda g: f"{g['k']} 종류의 검사 결과는 자동 조치 대상이 아닙니다.", "kind_unsupported"),
    _r("ident_not_ga", r"identifier '(?P<i>.*)' is not group:artifact",
       lambda g: f"식별자('{g['i']}')는 Maven 좌표(groupId:artifactId) 형식이 아닙니다.", "ident_not_ga"),
    _r("plugin",
       r"build plugin or plugin dependency(?: \(declared (?P<v>{A})\))?; plugin versions are declared, not resolved; "
       r"not changed automatically",
       lambda g: ("빌드 플러그인 또는 플러그인의 의존성입니다" + (f"(선언된 버전 {g['v']})" if g.get("v") else "")
                  + ". 플러그인 버전은 pom에 선언된 값만 확인할 수 있어 자동으로 수정하지 않습니다."), "plugin"),
    _r("ivas_not_resolved",
       r"IVAS reports (?P<ga>\S+) (?P<v>.+?), Maven does not resolve (?P<ga2>\S+) in any module"
       r"(?P<inc> \(resolution incomplete\))?",
       lambda g: (f"IVAS는 {g['ga']} {_none(g['v'])} 버전을 보고했지만, 어느 모듈에서도 Maven이 {g['ga2']} 라이브러리를 사용하지 "
                  "않습니다." + _inc(g.get("inc"))), "ivas_vs_maven"),
    _r("ivas_no_version",
       r"IVAS reports (?P<ga>\S+) without a version(?:; Maven resolves (?P<seen>{A}))?(?P<inc> \(resolution incomplete\))?",
       lambda g: (f"IVAS가 {g['ga']} 라이브러리를 버전 없이 보고했습니다."
                  + (f" Maven이 사용하는 버전: {g['seen']}." if g.get("seen") else "") + _inc(g.get("inc"))),
       "ivas_vs_maven"),
    _r("ivas_vs_maven", r"IVAS reports (?P<v>{A}), Maven resolves (?P<seen>{A})(?P<inc> \(resolution incomplete\))?",
       lambda g: f"IVAS는 {g['v']} 버전을 보고했지만, Maven은 {g['seen']} 버전을 사용합니다." + _inc(g.get("inc")),
       "ivas_vs_maven"),
    _r("cannot_read", r"cannot read (?P<p>\S+): (?P<e>{A})", lambda g: f"{g['p']} 파일을 읽을 수 없습니다({g['e']})."),
    _r("cannot_parse", r"cannot parse (?P<p>\S+): (?P<e>.+)",
       lambda g: f"{g['p']} 파일을 분석할 수 없습니다. {_raw(g['e'])}"),
    _r("target_no_pom", r"build target (?P<t>\S+): no pom\.xml", lambda g: f"빌드 대상 {g['t']}에 pom.xml이 없습니다."),
    _r("module_other_pom", r"(?P<pom>\S+): module '(?P<m>.*)' names a pom file other than pom\.xml — not read",
       lambda g: f"{g['pom']}의 모듈('{g['m']}')이 pom.xml이 아닌 파일을 가리켜 읽지 않았습니다."),
    _r("module_outside", r"(?P<pom>\S+): module '(?P<m>.*)' points outside the project — not read",
       lambda g: f"{g['pom']}의 모듈('{g['m']}')이 프로젝트 밖을 가리켜 읽지 않았습니다."),
    _r("module_no_pom", r"(?P<pom>\S+): module '(?P<m>.*)' has no pom\.xml",
       lambda g: f"{g['pom']}의 모듈 '{g['m']}'에 pom.xml이 없습니다."),
    _r("maven_incomplete_note", r"Maven resolution is incomplete", lambda g: "Maven 의존성 분석이 완전하지 않습니다."),
    # ---- maven: 의존성 분석 메모
    _r("no_root_pom", r"no readable root pom\.xml", lambda g: "읽을 수 있는 루트 pom.xml이 없습니다."),
    _r("no_target_pom", r"no readable pom\.xml in the build targets",
       lambda g: "빌드 대상에 읽을 수 있는 pom.xml이 없습니다."),
    _r("deptree_not_run", r"dependency:tree not run: (?P<e>.+)",
       lambda g: "dependency:tree를 실행하지 않았습니다." + _why(g["e"])),
    _r("deptree_failed", r"dependency:tree failed(?: for build target (?P<t>\S+))? \(see (?P<log>\S+)\)",
       lambda g: ((f"빌드 대상 {g['t']}의 " if g.get("t") else "") + "dependency:tree 실행에 실패했습니다. "
                  f"로그: {g['log']}")),
    _r("deptree_unreadable", r"dependency:tree output unreadable: (?P<e>\S+)",
       lambda g: f"dependency:tree 결과 파일을 읽을 수 없습니다({g['e']})."),
    _r("module_no_tree", r"module (?P<d>\S+) \((?P<ga>[^()\s]+)\): no dependency tree",
       lambda g: f"{g['d']} 모듈({g['ga']})의 의존성 트리가 없습니다."),
    _r("plugins_declared", r"plugin versions are declared in the poms, not resolved",
       lambda g: "플러그인 버전은 pom에 선언된 값만 확인합니다."),
    # ---- maven: 실행 환경 점검(readiness)
    _r("ready_mvnw", r"build\.mvn points at a Maven wrapper \(mvnw\); the server Maven installation is required",
       lambda g: "서버 설정의 Maven 실행 파일이 Maven wrapper(mvnw)로 되어 있습니다. 서버에 설치된 Maven을 지정해야 합니다."),
    _r("ready_mvnw_refused", r"build\.mvn points at a Maven wrapper \(mvnw\); refused",
       lambda g: "서버 설정의 Maven 실행 파일이 Maven wrapper(mvnw)로 되어 있어 실행하지 않았습니다."),
    _r("ready_no_maven", r"Maven not available \((?P<e>.*)\)",
       lambda g: f"서버에서 Maven을 실행할 수 없습니다. {_raw(g['e'])}"),
    _r("ready_no_jdk",
       r"JDK (?P<n>\d+) not found \(build\.java_home / JAVA_HOME has no release file and Maven did not report a Java "
       r"version\)",
       lambda g: f"서버에서 JDK {g['n']} 버전을 찾지 못했습니다. 서버 설정의 JAVA_HOME을 확인해야 합니다."),
    _r("ready_jdk_wrong", r"JDK (?P<n>\d+) required, found Java (?P<m>\S+)",
       lambda g: f"JDK {g['n']} 버전이 필요하지만 서버의 Java 버전은 {g['m']}입니다."),
    _r("ready_unsafe_args",
       r"build\.extra_args contains argument\(s\) the remediation never passes to Maven: (?P<a>.+?) \(insecure TLS / "
       r"settings / pom overrides\) — the migration's builds use them, so the remediation would not build like the "
       r"migration; remove them from build\.extra_args \(put the repository certificate into the JDK trust store "
       r"instead\)",
       lambda g: (f"서버 설정의 Maven 추가 인자에 조치에서 사용하지 않는 인자가 있습니다: {g['a']}. 이 상태로는 이관 빌드와 "
                  "같은 조건으로 빌드할 수 없습니다. 서버 설정에서 이 인자를 제거하고, 저장소 인증서는 JDK 신뢰 저장소에 "
                  "등록해야 합니다.")),
    _r("ready_settings_unset", r"server settings\.xml not configured \(build\.settings_file\)",
       lambda g: "서버의 settings.xml이 설정되어 있지 않습니다."),
    _r("ready_settings_missing", r"server settings\.xml not found \((?P<p>.+)\)",
       lambda g: f"서버의 settings.xml을 찾을 수 없습니다({g['p']})."),
    _r("ready_settings_unreadable", r"server settings\.xml cannot be read \((?P<p>.+)\)",
       lambda g: f"서버의 settings.xml을 읽을 수 없습니다({g['p']})."),
    _r("ready_no_hosts",
       r"no approved repository host \(remediation\.allowed_repository_hosts is empty and the server settings\.xml "
       r"declares no mirror / repository\)",
       lambda g: ("승인된 저장소가 없습니다. 서버 설정에 승인 저장소가 없고, 서버의 settings.xml에도 mirror 나 "
                  "repository가 없습니다.")),
    _r("components_unbuilt", r"remediation components cannot be built \((?P<e>[^()]*)\)",
       lambda g: f"조치 구성 요소를 준비하지 못했습니다({g['e']})."),
    # ---- intake: 빌드 설정 점검의 설명
    _r("boundary_symlink", r"symbolic link in the project", lambda g: "프로젝트 안의 심볼릭 링크입니다."),
    _r("boundary_settings", r"the server's settings\.xml is the only one used",
       lambda g: "서버의 settings.xml만 사용합니다."),
    _r("boundary_wrapper", r"the server's Maven is used; the wrapper is never run",
       lambda g: "서버의 Maven을 사용하며 wrapper는 실행하지 않습니다."),
    _r("boundary_host", r"host (?P<h>\S+) not approved: (?P<u>\S+)",
       lambda g: f"승인되지 않은 저장소 호스트입니다: {g['h']} ({g['u']})"),
    _r("boundary_unresolved", r"unresolved property in url (?P<u>\S+)",
       lambda g: f"저장소 URL의 property를 해석하지 못했습니다: {g['u']}"),
    _r("boundary_non_https", r"non-https \((?P<s>[^()]*)\)",
       lambda g: f"https가 아닙니다({'프로토콜 없음' if g['s'] == 'no scheme' else g['s']})."),
    _r("boundary_no_url", r"no url", lambda g: "URL이 없습니다."),
    _r("boundary_bad_url", r"unparsable url", lambda g: "URL을 해석할 수 없습니다."),
    _r("boundary_repo_id", r"id=(?P<i>\S+)", lambda g: f"저장소 id {g['i']}."),
    # ---- apply.verify_resolution: 검증 메모
    _r("res_incomplete", r"resolution (?P<w>after|before) the change is incomplete: (?P<n>.+)",
       lambda g: f"변경 {'후' if g['w'] == 'after' else '전'} Maven 의존성 분석이 완전하지 않습니다." + _why(g["n"])),
    _r("still_resolves", r"(?P<ga>\S+) still resolves at (?P<v>\S+) \((?P<pid>[^()\s]+)\)",
       lambda g: f"{g['ga']}의 버전은 아직 {g['v']}입니다({g['pid']})."),
    _r("module_missing", r"module (?P<m>\S+) is missing from the resolution after the change \((?P<pid>[^()\s]+)\)",
       lambda g: f"변경 후 의존성 분석 결과에 {g['m']} 모듈이 없습니다({g['pid']})."),
    _r("resolves_at", r"(?P<ga>\S+) in module (?P<m>\S+) resolves at (?P<got>{A}), planned (?P<to>\S+) \((?P<pid>[^()\s]+)\)",
       lambda g: ((f"{g['m']} 모듈에서 {g['ga']} 라이브러리를 찾을 수 없습니다." if _none(g["got"]) == "없음"
                   else f"{g['m']} 모듈의 {g['ga']} 버전은 {g['got']}입니다.")
                  + f" 계획한 버전은 {g['to']} 입니다({g['pid']}).")),
    _r("info_not_governed",
       r"info: (?P<ga>\S+) in module (?P<m>\S+) still resolves at (?P<v>\S+) — not governed by the edited version text "
       r"\((?P<pid>[^()\s]+)\); left for the next round",
       lambda g: (f"참고: {g['m']} 모듈의 {g['ga']} 버전은 아직 {g['v']}입니다. 이번 수정이 적용되지 않는 모듈이므로 다음 "
                  f"라운드에서 조치합니다({g['pid']}).")),
    _r("changed",
       r"(?P<info>info: )?(?P<ga>\S+) in module (?P<m>\S+) changed (?P<was>{A}) → (?P<now>{A})"
       r"(?P<tail> \(transitive\)| \(moved with a property / parent / BOM change\)| without a proposal covering it)",
       lambda g: (("참고: " if g.get("info") else "")
                  + f"{g['m']} 모듈의 {g['ga']} 버전이 바뀌었습니다({_none(g['was'])} → {_none(g['now'])}"
                  + {" (transitive)": ", 간접 의존성).",
                     " (moved with a property / parent / BOM change)": ", property·부모 pom·BOM 변경에 따름).",
                     " without a proposal covering it": "). 이 변경에 해당하는 제안이 없습니다."}[g["tail"]])),
    _r("info_packages",
       r"info: (?P<art>\S+) still packages (?P<e>\S+) \((?P<w>{A})\) — its module is not governed by (?P<pid>\S+)",
       lambda g: f"참고: {g['art']}에 아직 {g['e']}({g['w']})가 들어 있습니다. 이 모듈은 {g['pid']}의 적용 범위가 아닙니다."),
    _r("packages", r"(?P<art>\S+) still packages (?P<e>\S+) \((?P<w>{A}), (?P<pid>[^,()\s]+)\)",
       lambda g: f"{g['art']}에 아직 {g['e']}({g['w']})가 들어 있습니다({g['pid']})."),
    # ---- apply.verify_text_only: 버전 표기만 바꿨는지 검사
    _r("edited_not_listed", r"edited file (?P<p>\S+) is not in the original file list",
       lambda g: f"수정한 파일({g['p']})이 원본 파일 목록에 없습니다."),
    _r("missing_original", r"(?P<p>\S+): missing in the original tree", lambda g: f"{g['p']}: 원본에 없는 파일입니다."),
    _r("missing_candidate", r"(?P<p>\S+): missing in the candidate", lambda g: f"{g['p']}: 수정본에 없는 파일입니다."),
    _r("unreadable", r"(?P<p>\S+): unreadable \((?P<e>\w+)\)", lambda g: f"{g['p']}: 읽을 수 없습니다({g['e']})."),
    _r("changed_unplanned", r"(?P<p>\S+): changed but no planned edit names it",
       lambda g: f"{g['p']}: 계획에 없는 파일이 변경되었습니다."),
    _r("edits_not_apply", r"(?P<p>\S+): planned edits do not apply to the original \((?P<e>.+)\)",
       lambda g: f"{g['p']}: 계획한 수정을 원본에 적용할 수 없습니다. {_raw(g['e'])}"),
    _r("differs_more",
       r"(?P<p>\S+): differs from the original by more than the planned version replacements(?P<o> and override entries)?",
       lambda g: (f"{g['p']}: 계획한 버전 표기 수정{'과 dependencyManagement 항목 추가' if g.get('o') else ''} 외에 원본과 "
                  "다른 부분이 있습니다.")),
    _r("override_not_one", r"(?P<p>\S+): the planned override entries are not in one top-level <dependencyManagement>",
       lambda g: f"{g['p']}: 계획한 dependencyManagement 항목이 최상위 dependencyManagement 한 곳에 모여 있지 않습니다."),
    _r("override_presence", r"(?P<p>\S+): <dependencyManagement> presence before / after does not match the planned override",
       lambda g: f"{g['p']}: 변경 전후의 dependencyManagement 유무가 계획과 맞지 않습니다."),
    _r("outside_differs", r"a file outside the project folder differs from the accepted tree",
       lambda g: "프로젝트 폴더 밖의 파일이 작업 사본과 다릅니다."),
    # ---- graph / chain / service: 상태 메시지
    _r("upload_unknown",
       r"the upload of scan '(?P<l>[^']+)' was sent but its result is unknown; check IVAS and confirm before resuming",
       lambda g: (f"검사 '{g['l']}'의 파일을 IVAS에 올렸지만 접수 여부를 확인하지 못했습니다. IVAS에서 확인한 뒤 확인 "
                  "결과를 선택해 주세요.")),
    _r("upload_unknown_e", r"the upload of scan '(?P<l>[^']+)' has an unknown result: (?P<e>.+)",
       lambda g: f"검사 '{g['l']}'의 IVAS 업로드 결과를 확인하지 못했습니다. {_raw(g['e'])}"),
    _r("stop_scan",
       r"stop requested during scan '(?P<l>[^']+)'; the remote IVAS analysis keeps running and resume continues "
       r"polling it without uploading again",
       lambda g: (f"검사 '{g['l']}' 중에 중지 요청을 받았습니다. IVAS 분석은 계속 진행됩니다. 이어서 진행하면 파일을 다시 "
                  "올리지 않고 같은 분석 결과를 조회합니다.")),
    _r("stop_before_scan", r"stop requested before scan '(?P<l>[^']+)' uploaded anything",
       lambda g: f"검사 '{g['l']}'의 파일을 올리기 전에 중지 요청을 받았습니다."),
    _r("stop_baseline", r"stop requested during the baseline build", lambda g: "조치 전 빌드 중에 중지 요청을 받았습니다."),
    _r("stop_round", r"stop requested during the round (?P<r>\d+) candidate build",
       lambda g: f"라운드 {g['r']} 수정본 빌드 중에 중지 요청을 받았습니다."),
    _r("stop_node", r"stop requested before node (?P<n>\S+)",
       lambda g: f"'{g['n']}' 단계를 시작하기 전에 중지 요청을 받았습니다."),
    _r("zip_changed_intake", r"the original ZIP no longer has the sha256 recorded at intake",
       lambda g: "원본 ZIP의 sha256이 접수할 때 기록한 값과 다릅니다."),
    _r("ivas_bytes_differ", r"IVAS analysed bytes whose sha256 differs from the original ZIP",
       lambda g: "IVAS가 분석한 파일의 sha256이 원본 ZIP과 다릅니다."),
    _r("ivas_scanned_orig", r"IVAS scanned a ZIP whose sha256 differs from the original",
       lambda g: "IVAS가 검사한 ZIP의 sha256이 원본 ZIP과 다릅니다."),
    _r("ivas_scanned_delivery", r"IVAS scanned a ZIP whose sha256 differs from the delivery ZIP",
       lambda g: "IVAS가 검사한 ZIP의 sha256이 결과 ZIP과 다릅니다."),
    _r("scan_incomplete", r"(?P<w>baseline|final) scan incomplete: (?P<r>.+)",
       lambda g: f"{'조치 전' if g['w'] == 'baseline' else '조치 후'} 검사가 끝나지 않았습니다(사유: {_scan_reasons(g['r'])})."),
    _r("maven_incomplete", r"Maven resolution incomplete: (?P<n>.*)",
       lambda g: "Maven 의존성 분석이 완전하지 않습니다." + _why(g["n"])),
    _r("scan_missing", r"the scan this round plans from is missing or incomplete",
       lambda g: "이번 라운드의 계획에 쓸 검사 결과가 없거나 검사가 끝나지 않았습니다."),
    _r("pending_halt", r"(?P<n>\d+) proposal\(s\) of round (?P<r>\d+) wait for approval",
       lambda g: f"라운드 {g['r']}의 제안 {g['n']}건이 승인을 기다립니다."),
    _r("failed_included",
       r"delivery/result\.zip contains the round (?P<r>\d+) change set whose candidate build FAILED "
       r"\(proposals: (?P<ids>.*?); build log (?P<log>\S+)\); it was not rescanned; the original is preserved as "
       r"original/source\.zip(?:; the verified delivery of an earlier round is kept as (?P<kept>\S+))?",
       lambda g: (f"결과 ZIP(delivery/result.zip)에는 수정본 빌드에 실패한 라운드 {g['r']}의 변경이 들어 있습니다(제안 "
                  f"{g['ids']}, 빌드 로그 {g['log']}). 이 ZIP은 재검사하지 않았습니다. 원본은 original/source.zip에 "
                  "그대로 보관합니다."
                  + (f" 이전 라운드에서 검증한 결과 ZIP은 {g['kept']}에 있습니다." if g.get("kept") else ""))),
    _r("discarded",
       r"round (?P<r>\d+) candidate discarded; delivery/result\.zip is the verified state of round (?P<k>\d+), "
       r"without the failed change",
       lambda g: (f"라운드 {g['r']}의 수정본은 버렸습니다. 결과 ZIP(delivery/result.zip)은 라운드 {g['k']}에서 검증한 "
                  "상태이며, 실패한 변경은 들어 있지 않습니다.")),
    _r("delivery_missing", r"the delivery ZIP is missing", lambda g: "결과 ZIP이 없습니다."),
    _r("delivery_changed", r"the delivery ZIP changed after it was frozen",
       lambda g: "결과 ZIP이 확정된 뒤에 변경되었습니다."),
    _r("chain_budget",
       r"the chained remediation used its time budget of (?P<m>[\d.]+) minute\(s\) in this pod; resume continues from "
       r"the stopped node(?P<poll> \(the remote IVAS analysis keeps running and is polled again without a new upload\))?",
       lambda g: (f"이관에 이어 실행한 조치가 쓸 수 있는 시간({g['m']}분)을 다 썼습니다. 이어서 진행하면 "
                  "멈춘 단계부터 다시 시작합니다."
                  + (" IVAS 분석은 계속 진행되며, 파일을 다시 올리지 않고 같은 분석 결과를 조회합니다."
                     if g.get("poll") else ""))),
    _r("baseline_differs",
       r"the migration's final build passed on this same tree, but the remediation's baseline build failed(?P<where>.*?) "
       r"with the same Maven command base, settings\.xml and local repository \(only IVAS_\* / LLM_\* variables are "
       r"removed from its environment\) — compare remediation/baseline/build\.log with the migration's final build log "
       r"\(logs/\*-build-verify\*\.log\); the remediation builds a copy \(remediation/baseline/tree\) without (?P<left>.+)",
       lambda g: (f"이관의 최종 빌드는 같은 소스로 성공했지만, 조치 전 빌드는 실패했습니다{_where(g['where'])}. Maven 명령, "
                  "settings.xml, 로컬 저장소는 같고, 환경 변수에서 IVAS_*, LLM_* 만 제외했습니다. "
                  "remediation/baseline/build.log 파일과 이관의 최종 빌드 로그(logs/*-build-verify*.log)를 비교해 주세요. "
                  f"조치는 다음을 제외한 사본(remediation/baseline/tree)에서 빌드합니다: {_left(g['left'])}.")),
    _r("restarted",
       r"the server restarted while this remediation was (?P<s>\S+); nothing is re-sent automatically — check the state "
       r"and resume it explicitly",
       lambda g: (f"조치가 {_state(g['s'])} 상태일 때 서버가 다시 시작되었습니다. 자동으로 다시 실행하지 않습니다. 상태를 확인한 뒤 "
                  "직접 이어서 진행해 주세요.")),
    _r("shutdown",
       r"the server shut down while this remediation was running; nothing is re-sent automatically — resume it explicitly",
       lambda g: "조치를 실행하는 중에 서버가 종료되었습니다. 자동으로 다시 실행하지 않습니다. 직접 이어서 진행해 주세요."),
    _r("stale_run",
       r"the migration run has ended while this remediation was (?P<s>\S+) \(its build pod is gone\); nothing is re-sent "
       r"automatically",
       lambda g: f"이관 실행이 끝나면서 조치가 {_state(g['s'])} 상태로 남았습니다. 자동으로 다시 실행하지 않습니다."),
    _r("cannot_start", r"cannot start: (?P<e>.+)", lambda g: f"조치를 시작하지 못했습니다. {_raw(g['e'])}"),
    _r("report_not_written", r"report not written: (?P<e>.+)",
       lambda g: f"조치 결과 보고서를 쓰지 못했습니다. {_raw(g['e'])}"),
]


# 시험·보고서 점검용: 규칙마다 영어 원문의 고정 구절(변수 없는 부분). 번역되지 않은 원문이 보고서에 남았는지 찾는 데 쓴다
def _literal_runs(pattern: str) -> list[str]:
    """정규식의 맨 바깥 단계에서 이어진 글자(LITERAL) 덩어리들."""
    try:
        from re import _parser as sre_parse  # type: ignore[attr-defined]
    except ImportError:  # pragma: no cover - Python < 3.11
        import sre_parse  # type: ignore[no-redef]
    runs, cur = [], ""
    for op, arg in sre_parse.parse(pattern):
        if str(op) == "LITERAL":
            cur += chr(arg)
        else:
            if cur:
                runs.append(cur)
            cur = ""
    if cur:
        runs.append(cur)
    return runs


KEY_PHRASES: dict[str, str] = {}
for _rule in RULES:
    _best = max(_literal_runs(_rule.rx.pattern), key=len, default="").strip()
    if len(_best) >= 16:
        KEY_PHRASES[_rule.key] = _best


def _match(text: str) -> tuple[_Rule, str] | None:
    for rule in RULES:
        m = rule.rx.fullmatch(text)
        if m:
            g = {k: (v if v is not None else "") for k, v in m.groupdict().items()}
            return rule, rule.ko(g)
    return None


def _pieces(text: str) -> list[tuple[str, _Rule | None, str]]:
    """"; " 로 나눈 조각들을 가장 긴 것부터 규칙에 맞춘다: (원문 조각, 맞은 규칙 또는 None, 한국어 또는 원문)."""
    parts = text.split("; ")
    out: list[tuple[str, _Rule | None, str]] = []
    i = 0
    while i < len(parts):
        for j in range(len(parts), i, -1):
            cand = "; ".join(parts[i:j])
            got = _match(cand)
            if got is not None:
                out.append((cand, got[0], got[1]))
                i = j
                break
        else:
            out.append((parts[i], None, parts[i]))
            i += 1
    return out


def _join(pieces: list[tuple[str, _Rule | None, str]]) -> str:
    s = ""
    prev: tuple[str, _Rule | None, str] | None = None
    for p in pieces:
        if prev is None:
            s = p[2]
        else:
            # 번역한 문장 뒤는 띄어쓰기로, 원문이 남은 자리는 원래 구분자로 잇는다
            s += (" " if prev[1] is not None and p[1] is not None else "; ") + p[2]
        prev = p
    return s


def ko_text(text: Any) -> Any:
    """조치 코드가 만든 영어 문장 → 한국어. " | " 와 "; " 로 이어진 문장은 조각마다 바꾸고, 모르는 조각은 그대로 둔다.
    문자열이 아니면 그대로 돌려준다.

    Translate a sentence the remediation code produced into Korean, piece by piece; unknown pieces stay unchanged."""
    if not isinstance(text, str) or not text:
        return text
    return " | ".join(_join(_pieces(part)) for part in text.split(" | "))


def ko_display(text: Any) -> str:
    """보고서용 한국어: ``ko_text`` 와 같이 조각마다 바꾸되, 규칙이 모르는 조각은 "원문: `…`" 으로 감싸 뒤에 붙인다(깨진 한국어처럼
    보이지 않게). 빈 값은 "".

    Korean for the report: like `ko_text`, but pieces no rule knows are shown once as "원문: `…`" after the Korean."""
    ko, raw = split_known(text)
    out = " ".join(ko)
    if raw:
        out = (out + " " if out else "") + _raw("; ".join(raw))
    return out


def split_known(text: Any) -> tuple[list[str], list[str]]:
    """문장 → (규칙이 아는 조각의 한국어들, 규칙이 모르는 조각의 원문들). " | " 와 "; " 로 나눈다.

    (Korean of the known pieces, raw text of the unknown pieces)."""
    ko: list[str] = []
    raw: list[str] = []
    if not isinstance(text, str):
        return ko, raw
    for part in text.split(" | "):
        for cand, rule, k in _pieces(part):
            if rule is not None:
                ko.append(k)
            elif cand.strip():
                raw.append(cand.strip())
    return ko, raw


def rule_keys(text: Any) -> list[str]:
    """문장의 조각들이 맞은 규칙 키(순서대로, 모르는 조각은 빠진다).

    The rule keys the pieces of `text` matched."""
    if not isinstance(text, str) or not text:
        return []
    return [r.key for part in text.split(" | ") for _c, r, _k in _pieces(part) if r is not None]


def detail_group(text: str) -> tuple[str, str]:
    """잔여 설명 한 줄 → (묶음 키, 행마다의 변수 없는 한국어 설명 한 문장). 모든 조각이 알려진 규칙이면 키는 규칙 묶음 이름들
    ("+" 로 이음), 아니면 키는 원문 그대로이고 설명은 ``ko_display(원문)``.

    A residual detail → (stable group key, one Korean explanation without the per-row variables); the raw text is the
    key when any piece is unknown."""
    text = str(text or "")
    if not text:
        return "", ""
    pieces = _pieces(text)
    if all(r is not None and r.group for _c, r, _k in pieces):
        keys = list(dict.fromkeys(r.group for _c, r, _k in pieces if r is not None))
        return "+".join(keys), " ".join(GROUPS[k].text for k in keys)
    return text, ko_display(text)


def group_meta(key: str) -> tuple[str, bool]:
    """묶음 키 → (짧은 제목, 메모 칸이 필요한가). 모르는 키(원문 묶음)는 ("", False) — 행마다 같은 원문이다.

    Group key → (short title, whether rows need a memo column)."""
    keys = [k for k in (key or "").split("+") if k in GROUPS]
    if not keys or len(keys) != len((key or "").split("+")):
        return "", False
    return " · ".join(GROUPS[k].title for k in keys), any(GROUPS[k].memo for k in keys)


def variant_label(detail: str) -> str:
    """잔여 설명에 든 jar 가 아닌 형식 · classifier 표시 → "linux-x86_64" · "test-jar, tests" (없으면 "").

    The type / classifier variant a residual detail names, shown after the coordinates ("" when none)."""
    found: list[str] = []
    for v in re.findall(r"type/classifier variant \(([^()]*)\)", str(detail or "")):
        for one in v.split(", "):
            typ, _sep, cls = one.partition("/")
            text = cls if typ == "jar" and cls else (f"{typ}, {cls}" if cls else typ)
            if text and text not in found:
                found.append(text)
    return ", ".join(found)


def row_memo(detail: str) -> str:
    """표의 '내용' 칸: 잔여 설명의 한국어. 변종 사유는 라이브러리 칸에 따로 쓰므로 그 사실은 빼고 다른 조각만 남긴다.

    The memo cell of a residual row (variant facts left out: they are shown next to the coordinates)."""
    text = str(detail or "")
    if not text:
        return ""
    out: list[str] = []
    raw: list[str] = []
    for part in text.split(" | "):
        for cand, rule, k in _pieces(part):
            if rule is None:
                if cand.strip():
                    raw.append(cand.strip())
            elif rule.key == "variant":
                m = rule.rx.fullmatch(cand)
                others = _variant_parts(m["why"])[1] if m else []
                out += [ko_display(o) for o in others]
            elif rule.key not in ("variant_paren", "variant_in"):
                out.append(k)
    s = " ".join(x for x in out if x)
    if raw:
        s = (s + " " if s else "") + _raw("; ".join(raw))
    return s


def inhouse_gas(detail: str) -> list[str]:
    """예전 상태 파일(잔여에 ``inhouse`` 가 없음)용: 사내 아티팩트 설명 문장에서 g:a 들을 읽는다. 아니면 빈 목록.

    The in-house g:a's named by an in-house residual detail (for state files without `Residual.inhouse`)."""
    for _c, r, _k in _pieces(str(detail or "")):
        if r is None:
            continue
        m = r.rx.fullmatch(_c)
        if r.key == "inhouse_self" and m:
            return [m["ga"]]
        if r.key == "inhouse_via" and m:
            return [x.strip() for x in m["gas"].split(",") if x.strip()]
    return []


__all__ = ["SEVERITY_LEVELS", "SEVERITY_NONE_KO", "severity_of", "severity_thresholds", "BOUNDARY_KO", "COUNT_KO",
           "DECISION_KO", "FINDING_KIND_KO", "GROUPS", "KEY_PHRASES", "KIND_KO", "ORIGIN_ALL_KO", "ORIGIN_KO", "REASON_KO",
           "RISK_KO", "RULES", "SCAN_REASON_KO", "detail_group", "evidence_ko", "group_meta", "inhouse_gas", "ko_display",
           "ko_text", "label", "loc_ko", "row_memo", "rule_keys", "split_known", "variant_label"]
