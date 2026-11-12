"""취약점 조치의 사람용 한국어 표시: 코드(출처·변경 종류·위험·결정·이유·발견 종류)의 한국어 이름표와, 조치 코드가 만든 영어 문장
(잔여 설명·인벤토리 사유·제안 메모·검증 메모·상태 메시지)을 한국어 문장으로 바꾸는 규칙표.
흐름상 위치: ``report.build_report_md`` (report.md)와 ``service.proposals`` (화면의 ``*_ko`` 필드)가 부른다. 저장되는 자료
(state.json · plan.json · report.json · HTTP API 의 기존 필드)는 영어 문장과 코드 그대로다 — 그것이 계약이고, 이 모듈은 보여 줄
때만 바꾼다.
지켜야 할 것: 규칙(정규식)이 모르는 문장은 **그대로** 돌려준다(정보를 버리거나 추측하지 않는다). g:a · 버전 · 모듈 이름 · 파일 경로
· 제안 ID 는 글자 그대로 남긴다. Maven 자체의 출력(빌드 요약)은 번역하지 않는다. "; " 또는 " | " 로 이어진 문장은 조각마다
바꾸되, 한 규칙이 "; " 를 품은 문장 전체를 말하면(가장 긴 조각부터 맞춰 본다) 그 규칙이 이긴다.

Korean presentation for the remediation: labels for the codes and pattern rules that turn the English sentences the
remediation code produces into Korean. Stored data keeps its English text and codes (the contract); a sentence no rule
knows is returned unchanged; identifiers (g:a, versions, modules, paths, proposal ids) are kept verbatim.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------- 이름표 / labels
ORIGIN_KO: dict[str, str] = {
    "transitive": "간접 의존성", "direct_literal": "직접 선언(버전 글자)", "direct_property": "직접 선언(속성)",
    "own_management": "프로젝트의 관리 항목", "parent_managed": "부모 pom 이 관리", "bom_managed": "BOM 이 관리",
    "plugin": "빌드 플러그인", "vendored": "프로젝트에 들어 있는 파일", "unmapped": "출처를 찾지 못함",
    "proposal": "적용하지 못한 제안",
}
KIND_KO: dict[str, str] = {
    "direct_version": "직접 선언 버전", "property_version": "속성 값", "own_management_version": "프로젝트 관리 버전",
    "managed_override": "관리 버전 항목 추가(override)", "parent_version": "부모 pom 버전", "bom_version": "BOM 버전",
}
RISK_KO: dict[str, str] = {
    "major": "메이저 변경", "parent": "부모 pom 변경", "bom": "BOM 변경", "managed_override": "override 항목 추가",
}
DECISION_KO: dict[str, str] = {"auto": "자동 적용", "pending": "승인 대기", "approved": "승인됨", "rejected": "거절됨"}
# 이유 코드(models.R_* + chain_budget) → 짧은 이름표(표 머리·묶음 제목용). 긴 뜻과 다음에 할 일은 report._REASON_KO
REASON_KO: dict[str, str] = {
    "baseline_build_failed": "기준 빌드 실패", "candidate_build_failed": "후보 빌드 실패", "scan_incomplete": "검사 미완료",
    "upload_uncertain": "업로드 결과 모름", "zip_hash_mismatch": "ZIP 해시 불일치", "text_only_violation": "버전 글자 밖 변경",
    "resolution_mismatch": "해석 결과가 계획과 다름", "execution_boundary_refused": "실행 경계 거부",
    "not_ready": "서버 준비 안 됨", "pending_approval": "승인 대기", "rejected_proposals": "거절한 제안",
    "residual_findings": "남은 취약점", "manual_review": "사람 검토", "no_candidate_version": "올릴 버전 없음",
    "rounds_exhausted": "라운드 한도", "stop_requested": "중지 요청", "internal_error": "내부 오류",
    "policy_denied": "서버 정책이 막음", "unknown_provenance": "관리 출처 불명", "stale_location": "버전 자리가 바뀜",
    "inventory_incomplete": "의존성 목록 미완성", "inhouse_artifact": "사내 라이브러리 경유", "chain_budget": "시간 한도",
}
# graph.fill_counts 가 쓰는 집계 키 → 한국어(이 순서로 보여 준다; 모르는 키는 이름 그대로 뒤에)
COUNT_KO: dict[str, str] = {
    "baseline_findings": "기준 검사 조치 대상", "baseline_ignored": "기준 검사에서 뺀 항목", "fixed": "고친 취약점",
    "final_findings": "최종 검사 조치 대상", "final_ignored": "최종 검사에서 뺀 항목", "rounds": "라운드",
    "proposals": "제안", "proposals_applied": "적용한 제안", "proposals_pending": "승인 대기 제안",
    "proposals_rejected": "거절한 제안", "residual": "남긴 항목", "residual_inhouse": "사내 라이브러리 경유 항목",
}
FINDING_KIND_KO: dict[str, str] = {
    "library": "라이브러리", "file_component": "파일 컴포넌트", "file_component_potential": "파일 컴포넌트(잠재)",
    "sw": "소프트웨어(지원 안 함)",
}


# 위험도 표시(사용자 요청 2026-11-12: 위험한 취약점에 눈이 가게): (표시, 이름) — 빨강·주황·노랑 순서. Markdown 에는 색이 없어
# 편집기 미리보기에서 보이는 그림 글자를 쓴다
SEVERITY_LEVELS: tuple[tuple[str, str, str], ...] = (("red", "🔴", "심각"), ("orange", "🟠", "높음"), ("yellow", "🟡", "보통"))


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
    """제안의 근거 글자 → 한국어("policy:pins" → 서버 정책 pin, "repository:<호스트>" → 저장소 <호스트>).

    The Korean form of a proposal's evidence text."""
    s = str(evidence or "")
    if s == "policy:pins":
        return "서버 정책 pin"
    if s.startswith("policy:"):
        return "서버 정책 " + s[len("policy:"):]
    if s.startswith("repository:"):
        return "저장소 " + s[len("repository:"):]
    return s


# ---------------------------------------------------------------------------- 값 변환 도우미
def _none(x: str | None) -> str:
    """도구가 '없음' 자리에 쓰는 영어 낱말 → 없음."""
    x = (x or "").strip()
    return "없음" if x in ("", "nothing", "none", "absent", "no version") else x


_MORE = re.compile(r" \(\+(\d+) more\)")


def _vlist(x: str | None) -> str:
    """버전 목록 글자의 꼬리표만 한국어로(버전 글자는 그대로)."""
    s = _none(x)
    s = _MORE.sub(r" (외 \1개)", s)
    s = s.replace(" (deny_versions)", " (deny_versions 에 있음)").replace(" (prerelease)", " (시험판)")
    s = s.replace("(< patched hint ", "(패치 힌트 미만: ")
    return s


_KW = {"parent": "부모 pom", "BOM": "BOM", "transitive dependency": "간접 의존성", "managed version": "관리 버전"}


def _kw(x: str | None) -> str:
    return _KW.get(x or "", x or "")


def _origin(x: str | None) -> str:
    return label(ORIGIN_KO, x)


def _places(x: str) -> str:
    """루트 pom 관리 항목 자리 목록("top level, profile dev") → 한국어."""
    return ", ".join("최상위" if p.strip() == "top level" else re.sub(r"^profile ", "프로필 ", p.strip())
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
        out += f" · 관리: {m['by']}"
    if m["via"]:
        out += f" · 경로: {m['via']}"
    return out


def _left(x: str) -> str:
    s = x.replace("target/ build output", "target/ 빌드 산출물")
    return s.replace("ignored folders ", "접수가 무시한 폴더 ")


def _where(x: str) -> str:
    x = (x or "").strip()
    if x == "(at the project root)":
        return "(프로젝트 루트에서)"
    m = re.fullmatch(r"\(build targets: (.+)\)", x)
    return f"(빌드 대상: {m[1]})" if m else x


def _inc(x: str | None) -> str:
    return " (Maven 해석이 완전하지 않음)" if x else ""


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
    "not_auto_origin": _Group("자동 변경 대상이 아닌 출처",
                              "이 출처의 라이브러리는 자동으로 바꾸지 않습니다. 사람이 확인해야 합니다.", False),
    "policy_deny": _Group("서버 정책이 막음", "서버 정책(policy.deny)이 막은 라이브러리라 자동으로 바꾸지 않습니다.", False),
    "unknown_origin": _Group("출처를 알 수 없음", "출처를 알 수 없어 자동으로 바꾸지 않습니다.", False),
    "no_location": _Group("버전 자리를 찾지 못함", "프로젝트 pom 에서 버전이 적힌 자리를 찾지 못했습니다.", False),
    "not_plain": _Group("단순한 버전이 아님",
                        "pom 에 적힌 버전 글자나 해석된 버전이 단순한 버전이 아니라(식·범위 등) 자동으로 바꾸지 않습니다."),
    "stale": _Group("버전 자리가 바뀜", "계획한 뒤 pom 이 바뀌어 버전 자리를 다시 찾지 못했습니다. 다시 실행하면 새로 계획합니다."),
    "pin_unusable": _Group("서버 정책 pin 을 쓸 수 없음", "서버 정책(pin)이 정한 버전을 쓸 수 없습니다."),
    "no_hint": _Group("패치 버전 정보 없음",
                      "IVAS 가 고쳐진 버전을 알려 주지 않았고 서버 정책(pin)도 없어 올릴 버전을 정하지 못했습니다.", False),
    "hint_not_plain": _Group("패치 버전 힌트가 버전이 아님",
                             "IVAS 가 알려 준 패치 버전이 단순한 버전이 아니라(범위 등) 자동으로 고르지 않았습니다."),
    "only_major": _Group("메이저 변경만 가능",
                         "고쳐진 버전이 다음 메이저에만 있고 서버 정책이 메이저 변경을 허용하지 않습니다(policy.allow_major)."),
    "no_repo_version": _Group("저장소에 맞는 버전 없음",
                              "승인된 저장소에 패치 버전 조건을 만족하는 새 버전이 없습니다."),
    "no_evidence": _Group("저장소 근거 없음",
                          "고를 버전은 있지만 승인된 저장소가 그 버전을 확인해 주지 않았습니다."),
    "parent_pin_no_text": _Group("정책이 정한 부모 pom·BOM 버전 자리가 없음",
                                 "서버 정책이 부모 pom·BOM 버전을 정했지만 그 버전 글자가 프로젝트 pom 에 없습니다."),
    "parent_deny": _Group("관리하는 부모 pom·BOM 을 정책이 막음",
                          "버전을 관리하는 부모 pom·BOM 을 서버 정책(policy.deny)이 막았습니다."),
    "root_entry_exists": _Group("루트 pom 에 관리 항목이 이미 있음",
                                "루트 pom 에 이 라이브러리의 관리 항목이 이미 있어 새 항목을 넣지 않습니다."),
    "variant": _Group("분류자·다른 종류로 들어옴",
                      "jar 가 아닌 종류나 분류자(classifier)로 들어온 라이브러리라 일반 override 항목으로 고칠 수 없습니다."),
    "root_unreadable": _Group("루트 pom 을 읽을 수 없음",
                              "루트 pom.xml 을 읽을 수 없어 override 항목을 넣을 자리를 정하지 못했습니다.", False),
    "cannot_place": _Group("override 자리를 정하지 못함", "override 항목을 안전하게 넣을 자리를 정하지 못했습니다."),
    "not_inheriting_root": _Group("루트 pom 을 상속하지 않는 모듈",
                                  "이 모듈들은 프로젝트의 루트 pom 을 상속하지 않습니다(회사 부모 pom 을 상속). 루트 pom 에 버전을 "
                                  "덮어써도 모듈에 닿지 않아 자동으로 고칠 수 없습니다.", False),
    "declared_wins": _Group("모듈이 버전을 직접 선언함",
                            "모듈 pom 이 버전을 직접 선언해, 루트 pom 의 override 항목으로는 바뀌지 않습니다."),
    "rejected_earlier": _Group("앞에서 거절한 제안", "앞 라운드에서 거절한 제안이라 다시 제안하지 않습니다."),
    "inventory_incomplete": _Group("의존성 목록 미완성",
                                   "의존성 목록(인벤토리)이 완전하지 않아 아무것도 계획하지 않았습니다.", False),
    "inhouse_self": _Group("사내 아티팩트 자체",
                           "사내 아티팩트 자체입니다. 고친 버전은 그 아티팩트의 담당자가 배포해야 합니다.", False),
    "inhouse_via": _Group("사내 아티팩트 경유",
                          "사내 라이브러리나 회사 부모 pom 을 거쳐 들어옵니다. 그 아티팩트의 담당자만 pom 을 고칠 수 있습니다.",
                          False),
    "not_applied": _Group("적용하지 못한 제안", "제안을 적용하지 못했습니다(파일이 바뀌었을 수 있음)."),
    "external_manager": _Group("프로젝트 밖에서 관리됨", "프로젝트 밖의 부모 pom·BOM 이 버전을 관리합니다."),
    "ambiguous": _Group("관리 출처가 분명하지 않음", "어느 부모 pom·BOM 이 버전을 정하는지 분명하지 않습니다."),
    "no_manager": _Group("버전 선언도 관리자도 없음",
                         "프로젝트에 버전 선언이 없고 버전을 관리하는 부모 pom·BOM 도 없습니다.", False),
    "text_not_located": _Group("버전 글자를 한 줄에서 찾지 못함", "pom 에서 버전 글자를 한 줄로 찾지 못했습니다."),
    "declared_vs_resolved": _Group("선언과 해석이 다름", "pom 에 적힌 버전과 Maven 이 해석한 버전이 다릅니다."),
    "external_property": _Group("버전 속성이 프로젝트 밖에 있음", "버전 속성이 프로젝트 pom 밖에서 정해집니다."),
    "builtin_expr": _Group("내장 속성·식으로 정해진 버전",
                           "버전이 Maven 내장 속성이나 식에서 와 자동으로 바꾸지 않습니다."),
    "also_resolved": _Group("다른 모듈은 다른 길로 들어옴", "다른 모듈에서도 같은 버전이 다른 길로 들어옵니다."),
    "pom_unreadable": _Group("모듈 pom 을 읽을 수 없음", "모듈의 pom 을 읽을 수 없습니다."),
    "vendored": _Group("프로젝트에 든 파일",
                       "프로젝트에 직접 들어 있는 파일이라(jar·js 등) 자동으로 바꾸지 않습니다. 파일을 사람이 교체해야 합니다.",
                       False),
    "kind_unsupported": _Group("지원하지 않는 발견 종류", "조치 기능이 다루지 않는 발견 종류입니다."),
    "ident_not_ga": _Group("Maven 좌표가 아닌 식별자", "IVAS 식별자를 Maven 좌표(group:artifact)로 읽을 수 없습니다."),
    "plugin": _Group("빌드 플러그인",
                     "빌드 플러그인(또는 그 의존성)이라 자동으로 바꾸지 않습니다. 플러그인 버전을 사람이 올려야 합니다."),
    "ivas_vs_maven": _Group("IVAS 와 Maven 버전 불일치", "IVAS 가 보고한 버전을 Maven 해석 결과와 연결하지 못했습니다."),
}


def _r(key: str, pattern: str, ko: Callable[[dict[str, str]], str], group: str = "") -> _Rule:
    return _Rule(key, re.compile(pattern.replace("{A}", _A)), ko, group)


_VARIANT_PAREN = re.compile(r"type/classifier variant \((?P<v>[^()]*)\)")
_VARIANT_IN = re.compile(r"type/classifier variant in (?P<m>.+)")


def _variant_sentence(kw: str, ga: str, why: str) -> str:
    """override 를 막은 변종 사유를 한 번만 말한다: 변종 사실(jar/tests, "<모듈> 모듈")은 괄호에, 그 밖의 사유 조각은
    번역해 뒤 문장으로."""
    facts, others = [], []
    for piece in (why or "").split("; "):
        m = _VARIANT_PAREN.fullmatch(piece)
        n = _VARIANT_IN.fullmatch(piece)
        if m:
            facts.append(m["v"])
        elif n:
            facts.append(f"{n['m']} 모듈")
        elif piece:
            others.append(ko_text(piece))
    head = f"{_kw(kw)} {ga} 는 jar 가 아닌 종류이거나 분류자가 붙어 들어옵니다"
    head += f"({', '.join(facts)})." if facts else "."
    return " ".join([head, *others, "일반 override 항목으로는 고칠 수 없습니다."])


def _sub(x: str | None) -> str:
    """안쪽 문장(이유·메모)을 다시 번역한다(괄호 안에 들어가므로 끝의 마침표는 뺀다)."""
    out = ko_text(x or "")
    return out.removesuffix(".")


# 순서가 중요하다: 같은 문장에 둘이 맞으면 앞의 것이 이긴다(예: root_unreadable 이 cannot_place 보다 앞)
RULES: list[_Rule] = [
    # ---- planner: 잔여 설명
    _r("not_changed_auto", r"(?P<origin>[a-z_]+): not changed automatically",
       lambda g: f"{_origin(g['origin'])} 항목이라 자동으로 바꾸지 않습니다.", "not_auto_origin"),
    _r("policy_deny", r"(?P<ga>\S+) matches policy\.deny",
       lambda g: f"{g['ga']} 는 서버 정책(policy.deny)이 막은 라이브러리입니다.", "policy_deny"),
    _r("unknown_origin", r"unknown origin '(?P<origin>[^']*)'",
       lambda g: f"출처 '{g['origin']}' 를 알 수 없어 자동으로 바꾸지 않습니다.", "unknown_origin"),
    _r("no_location", r"no version location in the project poms",
       lambda g: "프로젝트 pom 에서 버전이 적힌 자리를 찾지 못했습니다.", "no_location"),
    _r("not_plain_text", r"(?P<loc>\S+:\d+) version text '(?P<t>{A})' is not a plain version",
       lambda g: f"{g['loc']} 의 버전 글자 '{g['t']}' 가 단순한 버전이 아닙니다.", "not_plain"),
    _r("parent_not_plain", r"(?P<loc>\S+:\d+) (?P<kw>parent|BOM) version '(?P<t>{A})' is not plain",
       lambda g: f"{g['loc']} 의 {_kw(g['kw'])} 버전 '{g['t']}' 가 단순한 버전이 아닙니다.", "not_plain"),
    _r("resolved_not_plain", r"resolved version '(?P<v>{A})' is not a plain version",
       lambda g: f"해석된 버전 '{g['v']}' 가 단순한 버전이 아닙니다.", "not_plain"),
    _r("stale_gone", r"(?P<loc>\S+:\d+) no longer exists or cannot be read",
       lambda g: f"{g['loc']} 줄이 없거나 읽을 수 없습니다(파일이 바뀌었을 수 있음).", "stale"),
    _r("stale_count", r"(?P<loc>\S+:\d+) contains '(?P<t>{A})' (?P<n>\d+) time\(s\), expected exactly once \(file changed\?\)",
       lambda g: f"{g['loc']} 줄에 '{g['t']}' 가 {g['n']}번 있습니다. 정확히 한 번이어야 합니다(파일이 바뀌었을 수 있음).",
       "stale"),
    _r("different_texts", r"(?P<loc>\S+:\d+): inventory names different version texts for one line",
       lambda g: f"{g['loc']} 한 줄에 대해 의존성 목록이 서로 다른 버전 글자를 말합니다.", "stale"),
    _r("pin_not_newer", r"policy pin (?P<pin>\S+) is not newer than the current (?P<cur>\S+)",
       lambda g: f"서버 정책 pin {g['pin']} 이 지금 버전 {g['cur']} 보다 새 버전이 아닙니다.", "pin_unusable"),
    _r("pin_denied", r"policy pin (?P<pin>\S+) is also listed in policy\.deny_versions",
       lambda g: f"서버 정책 pin {g['pin']} 이 policy.deny_versions 에도 있습니다.", "pin_unusable"),
    _r("pin_major", r"policy pin (?P<pin>\S+) is a major change and policy\.allow_major is false",
       lambda g: f"서버 정책 pin {g['pin']} 은 메이저 변경인데 policy.allow_major 가 꺼져 있습니다.", "pin_unusable"),
    _r("pin_for_not_newer", r"policy pin (?P<pin>\S+) for (?P<mga>\S+) is not newer than (?P<cur>\S+)",
       lambda g: f"서버 정책 pin {g['pin']}({g['mga']})이 지금 버전 {g['cur']} 보다 새 버전이 아닙니다.", "pin_unusable"),
    _r("pin_for_major", r"policy pin (?P<pin>\S+) for (?P<mga>\S+) is a major change and policy\.allow_major is false",
       lambda g: f"서버 정책 pin {g['pin']}({g['mga']})은 메이저 변경인데 policy.allow_major 가 꺼져 있습니다.",
       "pin_unusable"),
    _r("pin_below_hint", r"policy pin (?P<pin>\S+) is below the IVAS patched hint (?P<hint>.+)",
       lambda g: f"서버 정책 pin {g['pin']} 이 IVAS 가 알려 준 패치 버전 {g['hint']} 보다 낮습니다."),
    _r("no_hint", r"no patched version known \(IVAS gave none\) and no policy pin",
       lambda g: "IVAS 가 패치 버전을 알려 주지 않았고 서버 정책 pin 도 없습니다.", "no_hint"),
    _r("hint_not_plain", r"IVAS patched version hint\(s\) (?P<h>.+) are not plain versions; not chosen automatically",
       lambda g: f"IVAS 패치 버전 힌트 {g['h']} 가 단순한 버전이 아니라 자동으로 고르지 않았습니다.", "hint_not_plain"),
    _r("only_major", r"only major upgrades are available \((?P<vs>.+)\) and policy\.allow_major is false",
       lambda g: f"메이저 업그레이드({_vlist(g['vs'])})만 있는데 policy.allow_major 가 꺼져 있습니다.", "only_major"),
    _r("repo_listed",
       r"repository listed (?P<listed>.+?); excluded: (?P<excl>.+?); needed > (?P<cur>\S+) and ≥ patched hint (?P<hint>.+?)"
       r"(?:; the hinted (?P<miss>.+) (?:is|are) not in the repository)?",
       lambda g: (f"저장소에 있는 버전: {_vlist(g['listed'])}. 제외한 버전: {_vlist(g['excl'])}. 필요한 버전: {g['cur']} 보다 "
                  f"높고 패치 버전 {g['hint']} 이상."
                  + (f" 힌트의 {g['miss']} 는 저장소에 없습니다." if g.get("miss") else "")), "no_repo_version"),
    _r("no_evidence", r"the repository gave no evidence for (?P<t>\S+)",
       lambda g: f"승인된 저장소가 {g['t']} 를 확인해 주지 않았습니다.", "no_evidence"),
    _r("parent_pin_no_text",
       r"policy pins the (?P<kw>parent|BOM) (?P<mga>\S+) at (?P<pin>\S+), but its version text is not in the project poms",
       lambda g: f"서버 정책이 {_kw(g['kw'])} {g['mga']} 를 {g['pin']} 으로 정했지만 그 버전 글자가 프로젝트 pom 에 없습니다.",
       "parent_pin_no_text"),
    _r("parent_denied", r"the (?P<kw>parent|BOM) (?P<mga>\S+) matches policy\.deny",
       lambda g: f"{_kw(g['kw'])} {g['mga']} 는 서버 정책(policy.deny)이 막았습니다.", "parent_deny"),
    _r("root_entries_many",
       r"the root pom\.xml already has <dependencyManagement> entries for (?P<ga>\S+) \((?P<places>{A})\); "
       r"a second entry is never added",
       lambda g: (f"루트 pom.xml 에 {g['ga']} 의 관리 항목(<dependencyManagement>)이 이미 여러 곳에 있습니다"
                  f"({_places(g['places'])}). 항목을 하나 더 넣지 않습니다."), "root_entry_exists"),
    _r("root_entry_mismatch",
       r"the root pom\.xml already has a <dependencyManagement> entry for (?P<ga>\S+) whose version \((?P<v>{A})\) "
       r"is not the resolved (?P<res>\S+) as plain text; a second entry is never added",
       lambda g: (f"루트 pom.xml 의 {g['ga']} 관리 항목 버전({_none(g['v'])})이 해석된 {g['res']} 와 같은 단순한 글자가 "
                  "아닙니다. 항목을 하나 더 넣지 않습니다."), "root_entry_exists"),
    _r("variant",
       r"(?P<kw>transitive dependency|managed version) (?P<ga>\S+) resolves as a non-jar type or with a classifier "
       r"\((?P<why>.+)\); a plain override entry would not govern it",
       lambda g: _variant_sentence(g["kw"], g["ga"], g["why"]), "variant"),
    _r("root_unreadable", r"cannot place an override entry safely: the root pom\.xml is not readable",
       lambda g: "루트 pom.xml 을 읽을 수 없어 override 항목을 넣을 자리를 정하지 못했습니다.", "root_unreadable"),
    # 안쪽 오류(apply.EditError)는 도구 내부 문장이라 그대로 둔다
    _r("cannot_place", r"cannot place an override entry safely: (?P<e>.+)",
       lambda g: f"override 항목을 안전하게 넣을 자리를 정하지 못했습니다: {g['e']}", "cannot_place"),
    _r("not_inheriting_root",
       r"module\(s\) (?P<mods>{A}) do not inherit from the root pom\.xml: an override entry there would not reach them",
       lambda g: (f"{g['mods']} 모듈은 루트 pom.xml 을 상속하지 않습니다. 루트 pom 에 override 항목을 넣어도 이 모듈들에 "
                  "닿지 않습니다."), "not_inheriting_root"),
    _r("declared_wins",
       r"(?P<ga>\S+) is declared with a version in (?P<places>{A}): a declared version wins over <dependencyManagement>, "
       r"so an override entry would not change it",
       lambda g: (f"{g['ga']} 가 {g['places']} 에 버전과 함께 선언돼 있습니다. 직접 선언한 버전이 <dependencyManagement> "
                  "보다 우선이라 override 항목으로는 바뀌지 않습니다."), "declared_wins"),
    _r("rejected_earlier", r"(?P<ga>\S+) → (?P<v>\S+) was rejected in an earlier decision; not proposed again",
       lambda g: f"{g['ga']} → {g['v']} 는 앞에서 거절한 제안이라 다시 제안하지 않습니다.", "rejected_earlier"),
    _r("inventory_incomplete_plan", r"inventory incomplete: nothing planned \((?P<n>.*)\)",
       lambda g: f"의존성 목록(인벤토리)이 완전하지 않아 아무것도 계획하지 않았습니다. 이유: {ko_text(g['n'])}"),
    _r("inventory_incomplete", r"inventory incomplete",
       lambda g: "의존성 목록(인벤토리)이 완전하지 않습니다.", "inventory_incomplete"),
    _r("inventory_incomplete_msg", r"inventory incomplete: (?P<n>.+)",
       lambda g: f"의존성 목록(인벤토리)이 완전하지 않습니다. 이유: {ko_text(g['n'])}"),
    _r("inhouse_self", r"(?P<ga>\S+) is an in-house artifact: its owner publishes the fixed version",
       lambda g: f"{g['ga']} 는 사내 아티팩트입니다. 고친 버전은 그 담당자가 배포해야 합니다.", "inhouse_self"),
    _r("inhouse_via", r"brought in by the in-house artifact\(s\) (?P<gas>{A}): only their owner can change it",
       lambda g: f"사내 아티팩트 {g['gas']} 를 거쳐 들어옵니다. 그 담당자만 바꿀 수 있습니다.", "inhouse_via"),
    # 안쪽 이유(apply.EditError)는 도구 내부 문장이라 그대로 둔다
    _r("not_applied", r"proposal (?P<pid>\S+) not applied: (?P<why>.+)",
       lambda g: f"제안 {g['pid']} 를 적용하지 못했습니다: {g['why']}", "not_applied"),
    # ---- planner: 제안 메모 / plan notes
    _r("prop_governs", r"property \$\{(?P<p>[^}]*)\} may govern other artifacts",
       lambda g: f"속성 ${{{g['p']}}} 가 다른 라이브러리의 버전도 정할 수 있습니다."),
    _r("also_resolved_edit",
       r"also resolved in (?P<mods>{A}) through another origin; this edit may not govern them — "
       r"whatever stays at (?P<cur>\S+) is planned in the next round",
       lambda g: (f"{g['mods']} 모듈에서는 같은 버전이 다른 길로 들어옵니다. 이 수정이 닿지 않을 수 있고, {g['cur']} 로 "
                  "남는 곳은 다음 라운드에서 다시 계획합니다.")),
    _r("raises_parent",
       r"raises the (?P<kw>parent|BOM) (?P<mga>\S+) (?P<cur>\S+) → (?P<pin>\S+) \(server pin\) for (?P<gav>\S+); "
       r"the versions it manages may change too",
       lambda g: (f"{g['gav']} 를 고치려고 {_kw(g['kw'])} {g['mga']} 를 {g['cur']} → {g['pin']} 으로 올립니다(서버 pin). "
                  f"그 {_kw(g['kw'])} 가 관리하는 다른 버전도 바뀔 수 있습니다.")),
    _r("uses_existing", r"uses the existing <dependencyManagement> entry of the root pom\.xml",
       lambda g: "루트 pom.xml 에 이미 있는 관리 항목(<dependencyManagement>)의 버전을 바꿉니다."),
    _r("override_transitive",
       r"adds a <dependencyManagement> entry (?P<gav>\S+) to the root pom\.xml: pins the transitive dependency brought "
       r"by (?P<chain>{A}) \((?P<a>\S+) → (?P<b>\S+)\)",
       lambda g: (f"루트 pom.xml 의 <dependencyManagement> 에 {g['gav']} 항목을 넣어, "
                  f"{'다른 의존성' if g['chain'] == 'another dependency' else g['chain']} 이(가) 끌고 오는 간접 의존성의 "
                  f"버전을 고정합니다({g['a']} → {g['b']}).")),
    _r("override_managed",
       r"adds a <dependencyManagement> entry (?P<gav>\S+) to the root pom\.xml: overrides the version managed by "
       r"(?P<mgr>{A}) \((?P<a>\S+) → (?P<b>\S+)\)",
       lambda g: (f"루트 pom.xml 의 <dependencyManagement> 에 {g['gav']} 항목을 넣어, "
                  f"{'프로젝트 밖의 부모 pom·BOM' if g['mgr'] == 'a parent / BOM outside the project' else g['mgr']} "
                  f"이(가) 관리하던 버전을 덮어씁니다({g['a']} → {g['b']}).")),
    _r("override_warning",
       r"later upgrades of the parent / BOM / direct dependency will NOT move this version any more — "
       r"the owners must remove the override when the parent / BOM / direct dependency catches up",
       lambda g: ("이 항목을 넣으면 나중에 부모 pom·BOM·직접 의존성을 올려도 이 버전은 따라 움직이지 않습니다. 부모 pom·BOM·"
                  "직접 의존성이 이 버전을 따라잡으면 담당자가 항목을 지워야 합니다.")),
    _r("override_approval",
       r"always needs approval: an inserted <dependencyManagement> entry is never applied automatically",
       lambda g: "늘 승인이 필요합니다. 넣는 <dependencyManagement> 항목은 자동으로 적용하지 않습니다."),
    _r("merge_covers", r"one (?P<w>override entry|edit) also covers (?P<others>{A}) \((?:same artifact|same version line)\)",
       lambda g: (f"override 항목 하나가 {g['others']} 도 함께 고칩니다(같은 아티팩트)." if g["w"] == "override entry"
                  else f"수정 하나가 {g['others']} 도 함께 고칩니다(같은 버전 줄).")),
    _r("merge_target", r"target (?P<v>\S+) chosen for (?P<ga>\S+) \(highest of the merged targets\)",
       lambda g: f"합친 목표 중 가장 높은 {g['v']} 를 골랐습니다({g['ga']} 기준)."),
    _r("pending_note", r"(?P<n>\d+) proposal\(s\) wait for approval of their exact diff",
       lambda g: f"제안 {g['n']}건이 정확한 diff 에 대한 승인을 기다립니다."),
    # ---- inventory: 사유
    _r("managed_by_direct", r"managed by (?P<f>\S+), which the project poms do not declare directly",
       lambda g: f"{g['f']} 가 버전을 관리합니다(프로젝트 pom 에는 직접 선언이 없습니다).", "external_manager"),
    _r("managed_by", r"managed by (?P<f>\S+)",
       lambda g: f"{g['f']} 가 버전을 관리합니다.", "external_manager"),
    _r("ambiguous", r"ambiguous provenance: version may come from (?P<w>{A})",
       lambda g: f"버전이 어디서 오는지 분명하지 않습니다. 후보: {_what(g['w'])}", "ambiguous"),
    _r("no_manager", r"no version declared in the project and no parent / BOM to manage it",
       lambda g: "프로젝트에 버전 선언이 없고 버전을 관리하는 부모 pom·BOM 도 없습니다.", "no_manager"),
    _r("text_not_located", r"version text of (?P<ga>\S+) in (?P<pom>\S+) could not be located on one line",
       lambda g: f"{g['pom']} 에서 {g['ga']} 의 버전 글자를 한 줄에서 찾지 못했습니다.", "text_not_located"),
    _r("declares_vs", r"(?P<pom>\S+) declares (?P<ga>\S+) (?P<v>\S+), Maven resolves (?P<res>\S+)",
       lambda g: f"{g['pom']} 는 {g['ga']} {g['v']} 를 선언했지만 Maven 은 {g['res']} 로 해석합니다.",
       "declared_vs_resolved"),
    _r("prop_not_located", r"property (?P<p>\S+) in (?P<pom>\S+) could not be located on one line",
       lambda g: f"{g['pom']} 의 속성 {g['p']} 를 한 줄에서 찾지 못했습니다.", "text_not_located"),
    _r("prop_value_vs", r"property (?P<p>\S+) in (?P<pom>\S+) is (?P<v>\S+), Maven resolves (?P<res>\S+)",
       lambda g: f"{g['pom']} 의 속성 {g['p']} 는 {g['v']} 인데 Maven 은 {g['res']} 로 해석합니다.",
       "declared_vs_resolved"),
    _r("prop_not_defined", r"version property (?P<n>{A}) is not defined in the project poms",
       lambda g: f"버전 속성 {g['n']} 가 프로젝트 pom 에 정의돼 있지 않습니다.", "external_property"),
    _r("builtin", r"version of (?P<ga>\S+) comes from the built-in property (?P<n>{A})",
       lambda g: f"{g['ga']} 의 버전이 Maven 내장 속성 {g['n']} 에서 옵니다.", "builtin_expr"),
    _r("expression", r"version of (?P<ga>\S+) is the expression (?P<e>.+); manual review",
       lambda g: f"{g['ga']} 의 버전이 식 {g['e']} 입니다. 사람이 확인해야 합니다.", "builtin_expr"),
    _r("also_resolved_inv", r"also resolved in (?P<mods>{A}) \((?P<d>[a-z_]+(?: by {A})?(?: via {A})?)\)",
       lambda g: f"{g['mods']} 모듈에서도 같은 버전으로 해석됩니다({_desc(g['d'])}).", "also_resolved"),
    _r("variant_paren", r"type/classifier variant \((?P<v>{A})\)",
       lambda g: f"jar 가 아닌 종류·분류자로도 해석됩니다({g['v']}).", "variant"),
    _r("variant_in", r"type/classifier variant in (?P<mods>{A})",
       lambda g: f"{g['mods']} 모듈에서 jar 가 아닌 종류·분류자로 해석됩니다.", "variant"),
    _r("module_pom_unreadable", r"module (?P<m>\S+): pom not readable \((?P<e>{A})\)",
       lambda g: f"{g['m']} 모듈의 pom 을 읽을 수 없습니다({g['e']}).", "pom_unreadable"),
    _r("vendored_dep", r"file in the tree \(not a Maven dependency\); not changed automatically",
       lambda g: "프로젝트 트리에 들어 있는 파일입니다(Maven 의존성이 아님). 자동으로 바꾸지 않습니다.", "vendored"),
    _r("vendored_unresolved", r"file in the tree \(not resolved by Maven\); not changed automatically",
       lambda g: "프로젝트 트리에 들어 있는 파일입니다(Maven 이 해석하지 않음). 자동으로 바꾸지 않습니다.", "vendored"),
    _r("file_component", r"file component in the tree; not changed automatically",
       lambda g: "프로젝트 트리에 든 파일 컴포넌트입니다. 자동으로 바꾸지 않습니다.", "vendored"),
    _r("kind_unsupported", r"finding kind (?P<k>{A}) is not supported by the remediation",
       lambda g: f"발견 종류 {g['k']} 는 조치 기능이 다루지 않습니다.", "kind_unsupported"),
    _r("ident_not_ga", r"identifier '(?P<i>.*)' is not group:artifact",
       lambda g: f"식별자 '{g['i']}' 가 group:artifact 모양이 아닙니다.", "ident_not_ga"),
    _r("plugin",
       r"build plugin or plugin dependency(?: \(declared (?P<v>{A})\))?; plugin versions are declared, not resolved; "
       r"not changed automatically",
       lambda g: ("빌드 플러그인 또는 플러그인의 의존성입니다" + (f"(선언된 버전 {g['v']})" if g.get("v") else "")
                  + ". 플러그인 버전은 해석하지 않고 선언만 보므로 자동으로 바꾸지 않습니다."), "plugin"),
    _r("ivas_not_resolved",
       r"IVAS reports (?P<ga>\S+) (?P<v>.+?), Maven does not resolve (?P<ga2>\S+) in any module"
       r"(?P<inc> \(resolution incomplete\))?",
       lambda g: (f"IVAS 는 {g['ga']} {_none(g['v'])} 를 보고했지만 어느 모듈의 Maven 해석에도 {g['ga2']} 가 없습니다."
                  + _inc(g.get("inc"))), "ivas_vs_maven"),
    _r("ivas_no_version",
       r"IVAS reports (?P<ga>\S+) without a version(?:; Maven resolves (?P<seen>{A}))?(?P<inc> \(resolution incomplete\))?",
       lambda g: (f"IVAS 가 {g['ga']} 를 버전 없이 보고했습니다."
                  + (f" Maven 은 {g['seen']} 로 해석합니다." if g.get("seen") else "") + _inc(g.get("inc"))),
       "ivas_vs_maven"),
    _r("ivas_vs_maven", r"IVAS reports (?P<v>{A}), Maven resolves (?P<seen>{A})(?P<inc> \(resolution incomplete\))?",
       lambda g: f"IVAS 는 {g['v']} 를 보고했지만 Maven 은 {g['seen']} 로 해석합니다." + _inc(g.get("inc")),
       "ivas_vs_maven"),
    _r("cannot_read", r"cannot read (?P<p>\S+): (?P<e>{A})", lambda g: f"{g['p']} 를 읽을 수 없습니다: {g['e']}"),
    _r("cannot_parse", r"cannot parse (?P<p>\S+): (?P<e>.+)", lambda g: f"{g['p']} 를 해석할 수 없습니다: {g['e']}"),
    _r("target_no_pom", r"build target (?P<t>\S+): no pom\.xml", lambda g: f"빌드 대상 {g['t']} 에 pom.xml 이 없습니다."),
    _r("module_other_pom", r"(?P<pom>\S+): module '(?P<m>.*)' names a pom file other than pom\.xml — not read",
       lambda g: f"{g['pom']}: 모듈 '{g['m']}' 이 pom.xml 이 아닌 파일을 가리켜 읽지 않았습니다."),
    _r("module_outside", r"(?P<pom>\S+): module '(?P<m>.*)' points outside the project — not read",
       lambda g: f"{g['pom']}: 모듈 '{g['m']}' 이 프로젝트 밖을 가리켜 읽지 않았습니다."),
    _r("module_no_pom", r"(?P<pom>\S+): module '(?P<m>.*)' has no pom\.xml",
       lambda g: f"{g['pom']}: 모듈 '{g['m']}' 에 pom.xml 이 없습니다."),
    _r("maven_incomplete_note", r"Maven resolution is incomplete", lambda g: "Maven 해석이 완전하지 않습니다."),
    # ---- apply.verify_resolution: 검증 메모
    _r("res_incomplete", r"resolution (?P<w>after|before) the change is incomplete: (?P<n>.+)",
       lambda g: f"변경 {'뒤' if g['w'] == 'after' else '전'}의 Maven 해석이 완전하지 않습니다. 이유: {ko_text(g['n'])}"),
    _r("still_resolves", r"(?P<ga>\S+) still resolves at (?P<v>\S+) \((?P<pid>[^()\s]+)\)",
       lambda g: f"{g['ga']} 가 아직 {g['v']} 로 해석됩니다({g['pid']})."),
    _r("module_missing", r"module (?P<m>\S+) is missing from the resolution after the change \((?P<pid>[^()\s]+)\)",
       lambda g: f"변경 뒤 해석 결과에 {g['m']} 모듈이 없습니다({g['pid']})."),
    _r("resolves_at", r"(?P<ga>\S+) in module (?P<m>\S+) resolves at (?P<got>{A}), planned (?P<to>\S+) \((?P<pid>[^()\s]+)\)",
       lambda g: f"{g['m']} 모듈의 {g['ga']} 가 {_none(g['got'])} 로 해석됩니다. 계획은 {g['to']} 였습니다({g['pid']})."),
    _r("info_not_governed",
       r"info: (?P<ga>\S+) in module (?P<m>\S+) still resolves at (?P<v>\S+) — not governed by the edited version text "
       r"\((?P<pid>[^()\s]+)\); left for the next round",
       lambda g: (f"참고: {g['m']} 모듈의 {g['ga']} 는 아직 {g['v']} 입니다. 고친 버전 글자가 다스리지 않는 모듈이라 다음 "
                  f"라운드로 넘깁니다({g['pid']}).")),
    _r("changed",
       r"(?P<info>info: )?(?P<ga>\S+) in module (?P<m>\S+) changed (?P<was>{A}) → (?P<now>{A})"
       r"(?P<tail> \(transitive\)| \(moved with a property / parent / BOM change\)| without a proposal covering it)",
       lambda g: (("참고: " if g.get("info") else "")
                  + f"{g['m']} 모듈의 {g['ga']} 가 {_none(g['was'])} → {_none(g['now'])} 로 바뀌었습니다"
                  + {" (transitive)": "(간접 의존성).",
                     " (moved with a property / parent / BOM change)": "(속성·부모 pom·BOM 변경과 함께 움직임).",
                     " without a proposal covering it": ". 이 변화를 다루는 제안이 없습니다."}[g["tail"]])),
    _r("info_packages",
       r"info: (?P<art>\S+) still packages (?P<e>\S+) \((?P<w>{A})\) — its module is not governed by (?P<pid>\S+)",
       lambda g: f"참고: {g['art']} 에 아직 {g['e']}({g['w']})가 들어 있습니다. 그 모듈은 {g['pid']} 가 다스리지 않습니다."),
    _r("packages", r"(?P<art>\S+) still packages (?P<e>\S+) \((?P<w>{A}), (?P<pid>[^,()\s]+)\)",
       lambda g: f"{g['art']} 에 아직 {g['e']}({g['w']})가 들어 있습니다({g['pid']})."),
    # ---- apply.verify_text_only: 글자만 바꿨는지 검사
    _r("edited_not_listed", r"edited file (?P<p>\S+) is not in the original file list",
       lambda g: f"고친 파일 {g['p']} 이 원본 파일 목록에 없습니다."),
    _r("missing_original", r"(?P<p>\S+): missing in the original tree", lambda g: f"{g['p']}: 원본 트리에 없습니다."),
    _r("missing_candidate", r"(?P<p>\S+): missing in the candidate", lambda g: f"{g['p']}: 후보에 없습니다."),
    _r("unreadable", r"(?P<p>\S+): unreadable \((?P<e>\w+)\)", lambda g: f"{g['p']}: 읽을 수 없습니다({g['e']})."),
    _r("changed_unplanned", r"(?P<p>\S+): changed but no planned edit names it",
       lambda g: f"{g['p']}: 바뀌었지만 계획한 수정에 없는 파일입니다."),
    _r("edits_not_apply", r"(?P<p>\S+): planned edits do not apply to the original \((?P<e>.+)\)",
       lambda g: f"{g['p']}: 계획한 수정이 원본에 맞지 않습니다({g['e']})."),
    _r("differs_more",
       r"(?P<p>\S+): differs from the original by more than the planned version replacements(?P<o> and override entries)?",
       lambda g: f"{g['p']}: 계획한 버전 글자{' 바꾸기와 override 항목' if g.get('o') else ' 바꾸기'}보다 더 많이 원본과 다릅니다."),
    _r("override_not_one", r"(?P<p>\S+): the planned override entries are not in one top-level <dependencyManagement>",
       lambda g: f"{g['p']}: 계획한 override 항목들이 최상위 <dependencyManagement> 하나 안에 있지 않습니다."),
    _r("override_presence", r"(?P<p>\S+): <dependencyManagement> presence before / after does not match the planned override",
       lambda g: f"{g['p']}: 변경 전후의 <dependencyManagement> 유무가 계획한 override 와 맞지 않습니다."),
    _r("outside_differs", r"a file outside the project folder differs from the accepted tree",
       lambda g: "프로젝트 폴더 밖의 파일이 작업 트리와 다릅니다."),
    # ---- graph / chain / service: 상태 메시지
    _r("upload_unknown",
       r"the upload of scan '(?P<l>[^']+)' was sent but its result is unknown; check IVAS and confirm before resuming",
       lambda g: f"검사 '{g['l']}' 의 업로드를 보냈지만 결과를 모릅니다. IVAS 에서 확인한 뒤 확인 결과와 함께 이어서 진행하세요."),
    _r("upload_unknown_e", r"the upload of scan '(?P<l>[^']+)' has an unknown result: (?P<e>.+)",
       lambda g: f"검사 '{g['l']}' 의 업로드 결과를 모릅니다: {g['e']}"),
    _r("stop_scan",
       r"stop requested during scan '(?P<l>[^']+)'; the remote IVAS analysis keeps running and resume continues "
       r"polling it without uploading again",
       lambda g: (f"검사 '{g['l']}' 중에 중지 요청을 받았습니다. IVAS 의 원격 분석은 계속 진행되며, 이어서 진행하면 다시 "
                  "올리지 않고 그 분석을 조회합니다.")),
    _r("stop_before_scan", r"stop requested before scan '(?P<l>[^']+)' uploaded anything",
       lambda g: f"검사 '{g['l']}' 를 올리기 전에 중지 요청을 받았습니다."),
    _r("stop_baseline", r"stop requested during the baseline build", lambda g: "기준 빌드 중에 중지 요청을 받았습니다."),
    _r("stop_round", r"stop requested during the round (?P<r>\d+) candidate build",
       lambda g: f"라운드 {g['r']} 후보 빌드 중에 중지 요청을 받았습니다."),
    _r("stop_node", r"stop requested before node (?P<n>\S+)", lambda g: f"{g['n']} 단계 전에 중지 요청을 받았습니다."),
    _r("zip_changed_intake", r"the original ZIP no longer has the sha256 recorded at intake",
       lambda g: "원본 ZIP 의 sha256 이 접수 때 기록한 값과 다릅니다."),
    _r("ivas_bytes_differ", r"IVAS analysed bytes whose sha256 differs from the original ZIP",
       lambda g: "IVAS 가 분석한 파일의 sha256 이 원본 ZIP 과 다릅니다."),
    _r("ivas_scanned_orig", r"IVAS scanned a ZIP whose sha256 differs from the original",
       lambda g: "IVAS 가 검사한 ZIP 의 sha256 이 원본과 다릅니다."),
    _r("ivas_scanned_delivery", r"IVAS scanned a ZIP whose sha256 differs from the delivery ZIP",
       lambda g: "IVAS 가 검사한 ZIP 의 sha256 이 전달 ZIP 과 다릅니다."),
    _r("scan_incomplete", r"(?P<w>baseline|final) scan incomplete: (?P<r>.+)",
       lambda g: f"{'기준' if g['w'] == 'baseline' else '최종'} 검사가 완료되지 않았습니다: {g['r']}"),
    _r("maven_incomplete", r"Maven resolution incomplete: (?P<n>.*)",
       lambda g: f"Maven 해석이 완전하지 않습니다. 이유: {ko_text(g['n'])}"),
    _r("scan_missing", r"the scan this round plans from is missing or incomplete",
       lambda g: "이번 라운드가 계획에 쓸 검사 결과가 없거나 완료되지 않았습니다."),
    _r("pending_halt", r"(?P<n>\d+) proposal\(s\) of round (?P<r>\d+) wait for approval",
       lambda g: f"라운드 {g['r']} 의 제안 {g['n']}건이 승인을 기다립니다."),
    _r("failed_included",
       r"delivery/result\.zip contains the round (?P<r>\d+) change set whose candidate build FAILED "
       r"\(proposals: (?P<ids>.*?); build log (?P<log>\S+)\); it was not rescanned; the original is preserved as "
       r"original/source\.zip(?:; the verified delivery of an earlier round is kept as (?P<kept>\S+))?",
       lambda g: (f"delivery/result.zip 에는 후보 빌드가 실패한 라운드 {g['r']} 의 변경(제안: {g['ids']}, 빌드 로그 "
                  f"{g['log']})이 들어 있습니다. 이 ZIP 은 재검사하지 않았고, 원본은 original/source.zip 으로 보존됩니다."
                  + (f" 앞 라운드에서 검증한 ZIP 은 {g['kept']} 로 남아 있습니다." if g.get("kept") else ""))),
    _r("discarded",
       r"round (?P<r>\d+) candidate discarded; delivery/result\.zip is the verified state of round (?P<k>\d+), "
       r"without the failed change",
       lambda g: (f"라운드 {g['r']} 후보는 버렸습니다. delivery/result.zip 은 라운드 {g['k']} 에서 검증한 상태이며 실패한 "
                  "변경은 들어 있지 않습니다.")),
    _r("delivery_missing", r"the delivery ZIP is missing", lambda g: "전달 ZIP 이 없습니다."),
    _r("delivery_changed", r"the delivery ZIP changed after it was frozen", lambda g: "전달 ZIP 이 고정한 뒤에 바뀌었습니다."),
    _r("chain_budget",
       r"the chained remediation used its time budget of (?P<m>[\d.]+) minute\(s\) in this pod; resume continues from "
       r"the stopped node(?P<poll> \(the remote IVAS analysis keeps running and is polled again without a new upload\))?",
       lambda g: (f"이관에 이어 돈 조치가 이 파드의 시간 한도 {g['m']}분을 다 썼습니다. 이어서 진행하면 멈춘 단계부터 다시 "
                  "합니다." + (" IVAS 원격 분석은 계속 진행되며, 새로 올리지 않고 다시 조회합니다." if g.get("poll") else ""))),
    _r("baseline_differs",
       r"the migration's final build passed on this same tree, but the remediation's baseline build failed(?P<where>.*?) "
       r"with the same Maven command base, settings\.xml and local repository \(only IVAS_\* / LLM_\* variables are "
       r"removed from its environment\) — compare remediation/baseline/build\.log with the migration's final build log "
       r"\(logs/\*-build-verify\*\.log\); the remediation builds a copy \(remediation/baseline/tree\) without (?P<left>.+)",
       lambda g: (f"이관의 최종 빌드는 같은 트리에서 통과했지만 조치의 기준 빌드는 실패했습니다{_where(g['where'])}. Maven "
                  "명령·settings.xml·로컬 저장소는 같고 환경 변수에서 IVAS_* / LLM_* 만 뺐습니다. "
                  "remediation/baseline/build.log 와 이관의 최종 빌드 로그(logs/*-build-verify*.log)를 비교하세요. 조치는 "
                  f"{_left(g['left'])} 를 뺀 사본(remediation/baseline/tree)에서 빌드합니다.")),
    _r("restarted",
       r"the server restarted while this remediation was (?P<s>\S+); nothing is re-sent automatically — check the state "
       r"and resume it explicitly",
       lambda g: (f"이 조치가 {g['s']} 상태일 때 서버가 다시 시작됐습니다. 자동으로 다시 보내지 않습니다. 상태를 확인한 뒤 "
                  "직접 이어서 진행하세요.")),
    _r("shutdown",
       r"the server shut down while this remediation was running; nothing is re-sent automatically — resume it explicitly",
       lambda g: "조치를 실행하는 중에 서버가 내려갔습니다. 자동으로 다시 보내지 않습니다. 직접 이어서 진행하세요."),
    _r("cannot_start", r"cannot start: (?P<e>.+)", lambda g: f"시작하지 못했습니다: {g['e']}"),
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


def detail_group(text: str) -> tuple[str, str]:
    """잔여 설명 한 줄 → (묶음 키, 행마다의 변수 없는 한국어 설명 한 문장). 모든 조각이 알려진 규칙이면 키는 규칙 묶음 이름들
    ("+" 로 이음), 아니면 키는 원문 그대로이고 설명은 ``ko_text(원문)``.

    A residual detail → (stable group key, one Korean explanation without the per-row variables); the raw text is the
    key when any piece is unknown."""
    text = str(text or "")
    if not text:
        return "", ""
    pieces = _pieces(text)
    if all(r is not None and r.group for _c, r, _k in pieces):
        keys = list(dict.fromkeys(r.group for _c, r, _k in pieces if r is not None))
        return "+".join(keys), " ".join(GROUPS[k].text for k in keys)
    return text, ko_text(text)


def group_meta(key: str) -> tuple[str, bool]:
    """묶음 키 → (짧은 제목, 메모 칸이 필요한가). 모르는 키(원문 묶음)는 ("", False) — 행마다 같은 원문이다.

    Group key → (short title, whether rows need a memo column)."""
    keys = [k for k in (key or "").split("+") if k in GROUPS]
    if not keys or len(keys) != len((key or "").split("+")):
        return "", False
    return " · ".join(GROUPS[k].title for k in keys), any(GROUPS[k].memo for k in keys)


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


__all__ = ["SEVERITY_LEVELS", "severity_of", "severity_thresholds", "COUNT_KO", "DECISION_KO", "FINDING_KIND_KO", "GROUPS", "KEY_PHRASES", "KIND_KO", "ORIGIN_KO", "REASON_KO", "RISK_KO",
           "RULES", "detail_group", "evidence_ko", "group_meta", "inhouse_gas", "ko_text", "label"]
