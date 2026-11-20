"""한국어 report.md를 만드는 모듈. 마이그레이션을 요청한 담당자에게 무엇을 바꿨고 무엇을 확인해야 하는지 알려 주는 안내서다.
영어 보고서를 줄마다 번역하지 않고 이 모듈에서 한국어 문장을 직접 쓴다.
보고서에 영어로 저장된 줄(인수인계 할 일, 후속 조치, 플래그 설명)과 특수 절의 문장은 ``i18n``의 번역을 그대로 쓴다.
절 순서: 제목, Ant 안내, 머리 표, 사내 프레임워크, 안내 문장, 전환 내용, 확인해 주세요, 코드 수정 내역,
해결하지 못한 오류, 진행하지 않은 항목, 특수 절, 참고. 내용이 없는 절은 넣지 않는다.

The Korean report.md (`report.language: ko`): a guide for the person who asked for the migration, written in Korean
here rather than translated line by line (INTERFACES "v2.1z-as"). Stored English lines (hand-over items, follow-ups,
flag explanations) and the special sections keep going through `i18n`. Never raises on missing optional data."""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from sbmig.core.models import AdvisoryFinding, RunReport, VersionChange

from .errors import OPEN_STATUSES, entries_of, entry_text
from .handover import (
    HTTP_BLOCKED_PREFIX,
    REASON_BOOT,
    REASON_BOOT_BOM_PIN,
    REASON_CARRIED,
    REASON_MANDATORY,
    REASON_OPTIONAL,
    REASON_OTHER,
    REASON_PARENT,
    REASON_RECIPE,
    is_jsp_jakarta_note,
    jsp_jakarta_counts,
    pom_synthesis_counts,
    tr_reason,
)
from .i18n import tr, tr_line
from .markdown import (
    ABORT_PREFIX,
    ANT_DETAIL_PREFIX,
    ANT_FLAG,
    JAVAX_JARS_TITLE,
    _cell,
    _dt,
    _duration,
    _same_change,
    _same_version,
    _to_be,
    adf_lines,
    blocked_rows,
    bundled_jars_lines,
    cost_lines,
    model_cell,
    flag_help,
    framework_lines,
    intake_notice_lines,
    no_fix_loop,
    javax_api_lines,
    operator_check_lines,
    pom_correction_lines,
    pom_synthesis_lines,
    properties_lines,
    remediation_lines,
    result_label,
    review_summary,
)
from sbmig.restructure.pom_charset import HANDOVER_PREFIX as POM_CHARSET_PREFIX
from sbmig.rewrite.failure import FLAG as REWRITE_FAILED, HANDOVER_PREFIX as REWRITE_FAILED_PREFIX

from .result_line import conversion_only_failed
from .result_line import report_result_line, run_scope

LANG = "ko"
OPEN_ERRORS_MAX = 50                 # 해결하지 못한 오류 표의 최대 줄 수(나머지는 errors.md)
ADVISORY_DOCS_MAX = 3
ADVISORY_FILES_MAX = 5
ITEM_CHARS = 1500                    # 확인해 주세요 한 항목의 최대 길이

# 버전 변경 줄의 이유 코드(handover.REASON_*)를 비고 칸의 문장으로 바꾼다
REASON_KO = {
    REASON_CARRIED: "새 부모 pom에 버전이 없어 기존 버전을 유지",
    REASON_PARENT: "부모 pom 교체",
    REASON_OPTIONAL: "사내 선택 아티팩트 버전 맞춤",
    REASON_MANDATORY: "사내 필수",
    REASON_BOOT_BOM_PIN: "새 부모 pom에 버전이 없어 Spring Boot BOM의 버전을 지정",
    REASON_BOOT: "Spring Boot 4 전환",
    REASON_OTHER: "OpenRewrite 레시피 또는 빌드 오류 수정",
    REASON_RECIPE: "OpenRewrite 레시피",
}
_TRIGGER_RE = re.compile(r"^optional \(trigger: (?P<m>.*)\)$")
KIND_KO = {"dependency": "의존성", "plugin": "플러그인", "bom": "BOM", "property": "프로퍼티", "parent": "부모 pom"}

# 계획 단계 id를 실행 정보에 쓸 이름으로 바꾼다(MSA 서비스 접미어 @svc는 떼고 찾는다)
STEP_KO = {
    "transform.rewrite": "OpenRewrite 변환",
    "transform.compile": "변환 뒤 컴파일 확인",
    "transform.jsp_jakarta": "JSP javax → jakarta 변환",
    "restructure.apply": "구조 재구성",
    "restructure.msa_pom": "서비스별 pom 정리",
    "restructure.pom_only": "pom 정리",
    "restructure.boot_pom": "Spring Boot 프로젝트 pom 정리",
    "restructure.review": "구조 점검",
    "verify.build": "최종 빌드",
}
# OpenRewrite 레시피 id의 마지막 이름을 사람이 읽는 말로 바꾼다
_RECIPE_WORDS = (
    (re.compile(r"^UpgradeToJava(\d+)$"), lambda m: f"Java {m.group(1)}"),
    (re.compile(r"^JavaxMigrationToJakarta$"), lambda m: "javax → jakarta"),
    (re.compile(r"^UpgradeSpringFramework_(\d+)_\d+$"), lambda m: f"Spring Framework {m.group(1)}"),
    (re.compile(r"^UpgradeSpringSecurity_(\d+)_\d+$"), lambda m: f"Spring Security {m.group(1)}"),
    (re.compile(r"^UpgradeSpringBoot_(\d+)_\d+$"), lambda m: f"Spring Boot {m.group(1)}"),
    (re.compile(r"^SpringBootProperties_(\d+)_(\d+)$"), lambda m: f"Spring Boot {m.group(1)}.{m.group(2)} 설정 키"),
    (re.compile(r"^SpringBatch\d+To(\d+)Migration$"), lambda m: f"Spring Batch {m.group(1)}"),
)
_RESTRUCTURE_INTO_RE = re.compile(r"^restructure into (?P<mods>.+?) with ")

# 인수인계 할 일 목록의 영어 줄 가운데 안내서가 다른 곳에서 이미 보여 주거나 한국어로 새로 쓰는 줄
_BLOCKED_PTR = re.compile(r"^(\d+) cluster\(s\) blocked — see Blocked$")
_UNRESOLVED_PTR = re.compile(r"^(\d+) unresolved error\(s\) — see Error list$")
# 수정 루프 없이 빌드가 실패한 프레임워크 프로젝트의 인수인계 줄(sbmig.analyze.framework.no_fix_loop_followup)
_NO_FIX_PTR = re.compile(r"^framework project: build errors are not fixed automatically \(framework\.fix_loop: false\) "
                         r"— (\d+) error\(s\) remain after ")
NO_FIX_TITLE = "변환 뒤 남은 빌드 오류"
_JAVAX_PTR = re.compile(r"^(\d+) javax API jar\(s\) still on the classpath — see that section$")
_OPERATOR_PTR = re.compile(r"^operator checks: (\d+) major upgrade\(s\) — see \"Operator checks\" above$")
_CAP_LINE = re.compile(r"^… and \d+ more — see Manual follow-ups / Blocked / Error list$")
_REVIEW_MORE = re.compile(r"^… and (\d+) more structure review issue\(s\) — see Structure review$")
_CARRIED_LINE = re.compile(r"^(?P<key>\S+) pinned at \S+ — the new parent/Boot BOM no longer manages it: confirm the "
                           r"version or replace$")
_CARRIED_SUMMARY = re.compile(r"^\d+ dependencies are pinned at their as-is versions because the new parent / ")

OUTPUT_KO = (("zip", "결과 ZIP"), ("diff", "전체 변경"), ("errors_md", "오류별 상세 기록"), ("errors", "오류별 상세 기록"),
             ("logs", "로그"), ("journal", "실행 기록"), ("report_json", "보고서 데이터"), ("plan", "계획"),
             ("profile", "분석 결과"), ("effective_depmgmt", "변경 전 dependencyManagement"),
             ("project_dir", "변환한 프로젝트"))


# ---------------------------------------------------------------------------------------------------------- helpers
def _code(text: object) -> str:
    """식별자 하나를 백틱으로 감싼다. 값 안의 백틱은 지운다."""
    return f"`{str(text).replace('`', '')}`"


def _norm(text: object) -> str:
    return " ".join(str(text or "").split()).lower()


def _aborted(r: RunReport) -> bool:
    """실행이 중간에 멈췄는지 확인한다."""
    flags = list(r.flags or [])
    return "aborted" in flags or any(f.startswith("aborted:") for f in flags) \
        or any(str(m).startswith(ABORT_PREFIX) for m in r.manual_followups or [])


def _build_failed(r: RunReport) -> bool:
    """중단되지 않은 실행에서 최종 빌드가 통과하지 못했는지 확인한다."""
    if _aborted(r) or r.success:
        return False
    return r.final_build is None or not r.final_build.ok


def _ant_refused(r: RunReport) -> bool:
    return bool(intake_notice_lines(r, LANG))


def _fw_who(r: RunReport) -> str:
    """프레임워크 프로젝트를 이름과 함께 부른다. 이름이 없으면 사내 프레임워크 프로젝트다."""
    name = str(getattr(getattr(getattr(r, "profile", None), "framework", None), "name", "") or "").strip()
    return f"{name} 프레임워크 프로젝트" if name else "사내 프레임워크 프로젝트"


def _rel(path: str, r: RunReport) -> str:
    """실행 디렉토리 아래 경로를 상대 경로로 바꾼다."""
    p = str(path or "").replace("\\", "/")
    base = str((r.outputs or {}).get("run_dir") or "").replace("\\", "/").rstrip("/")
    if base and p.startswith(base + "/"):
        return p[len(base) + 1:]
    return p


def _promote(lines: list[str]) -> list[str]:
    """특수 절의 첫 ``###`` 제목을 ``##``로 올린다. 안내서에서는 독립된 절이기 때문이다."""
    out = list(lines)
    for i, ln in enumerate(out):
        if ln.startswith("### "):
            out[i] = "## " + ln[4:]
            break
        if ln.strip():
            break
    return out


def _fence(text: str) -> str:
    """diff를 감쌀 펜스를 고른다. 본문에 백틱 세 개 이상이 있으면 그보다 하나 긴 펜스를 쓴다."""
    longest = max((len(m.group(0)) for m in re.finditer(r"`{3,}", text or "")), default=0)
    return "`" * max(3, longest + 1)


# ---------------------------------------------------------------------------------------------------------- head
def _to_be_ko(r: RunReport) -> Optional[str]:
    """머리 표의 전환 후 칸을 만든다. 인수인계 데이터가 없으면 None을 반환한다."""
    if _to_be(r) is None:
        return None
    h = r.handover
    parts = []
    if h.java_after:
        parts.append(f"Java {h.java_after}")
    if h.spring_boot_after:
        parts.append(f"Spring Boot {h.spring_boot_after}")
    if h.parent_after:
        parts.append(f"부모 pom {_parents(h.parent_after)}")
    return ", ".join(parts)


def _parents(value: Optional[str]) -> str:
    """``g:a:v, g:a:v`` 형태의 부모 pom 값을 좌표마다 백틱으로 감싼다."""
    items = [x.strip() for x in str(value or "").split(",") if x.strip()]
    return ", ".join(_code(_cell(x, 200)) for x in items) or "없음"


def head_lines(r: RunReport) -> list[str]:
    """제목 아래 머리 표를 만든다. 결과, SBMIG-RESULT, 전환 후, 실행 ID, 시작 / 종료, 플래그 순이다."""
    if _aborted(r) and r.final_build is None:
        result = "❌ 실행 중단 (빌드까지 진행하지 못했습니다)"
    elif _aborted(r):
        # 빌드를 한 뒤에 멈춘 실행은 멈췄다는 사실과 마지막 빌드 결과를 함께 쓴다
        result = f"❌ 실행 중단 ({_last_build_ko(r)})"
    else:
        result = f"{result_label(r, LANG)} (최종 `mvn clean package`)"
    L = ["| | |", "|---|---|", f"| **결과** | {result} |",
         f"| SBMIG-RESULT | `{report_result_line(r)}` |"]
    to_be = _to_be_ko(r)
    if to_be:
        L.append(f"| 전환 후 | {to_be} |")
    L.append(f"| 실행 ID | `{r.run_id}` |")
    L.append(f"| 시작 / 종료 | {_dt(r.started_at)} / {_dt(r.finished_at)} ({_duration(r, LANG)}) |")
    if r.flags:
        L.append(f"| 플래그 | {', '.join(_code(f) for f in r.flags)} |")
    L.append("")
    return L


def _last_build_ko(r: RunReport) -> str:
    """중단된 실행의 마지막 빌드 결과를 한 문장으로 만든다. 빌드가 없었으면 빈 문자열을 반환한다."""
    if r.final_build is None:
        return ""
    return "마지막 빌드는 통과했습니다" if r.final_build.ok else "마지막 빌드는 통과하지 않았습니다"


def purpose_lines(r: RunReport) -> list[str]:
    """머리 표 아래 안내 문장을 만든다. 빌드가 실패했으면 그 사실과 먼저 볼 절을 앞에 쓴다."""
    if _aborted(r):
        last = (f" {_last_build_ko(r)}." if r.final_build is not None else "")
        if r.final_build is not None and r.final_build.log_path:
            last += " 마지막 빌드 로그는 「참고」에 있습니다."
        return ["이번 실행은 끝까지 진행하지 못했습니다. 멈춘 이유와 다시 실행하기 전에 확인하실 사항을 아래에 적었습니다."
                + last, ""]
    L: list[str] = []
    if conversion_only_failed(r):
        # 변환만 하는 실행은 변환이 전부이므로 빌드 결과와 상관없이 변환 실패를 먼저 쓰고 끝낸다
        built = r.final_build is not None and r.final_build.ok
        head = ("빌드는 통과했지만 JDK 21과 Jakarta 자동 변환이 실패해 아무것도 변환되지 않았습니다. 그래서 이번 실행은 "
                "성공이 아닙니다." if built else
                "JDK 21과 Jakarta 자동 변환이 실패해 아무것도 변환되지 않았고, 변환하지 않은 코드로 실행한 빌드도 "
                "통과하지 않았습니다.")
        L += [f"{head} 결과 파일의 코드는 변환 전과 같습니다. 실패 원인은 「확인해 주세요」의 첫 항목에 적었습니다. "
              "원인을 해결한 뒤 다시 실행해 주세요.", ""]
        return L + ["이 문서는 마이그레이션을 요청하신 담당자께 드리는 안내입니다. 무엇을 바꿨는지, 전환 후 무엇을 확인하셔야 "
                    "하는지 순서대로 적었습니다.", ""]
    if REWRITE_FAILED in (r.flags or []):
        L += ["자동 변환(OpenRewrite)이 실패해 Java 21, Jakarta, Spring 버전 변환 레시피가 적용되지 않았습니다. 그 뒤의 구조 "
              "정리와 빌드 오류 수정은 진행했지만, 레시피가 바꿨어야 할 코드는 바뀌지 않았을 수 있습니다. 실패 원인은 "
              "「확인해 주세요」의 첫 항목에 적었습니다.", ""]
    if _build_failed(r) and no_fix_loop(r):
        # 고치려고 한 것이 없으므로 남은 오류를 정리했다는 것과 고칠 사람만 쓴다
        rest = (f"남은 오류를 아래 「{NO_FIX_TITLE}」에 정리했습니다." if open_errors(r)
                else "「참고」의 마지막 빌드 로그를 봐 주세요.")
        L += [f"JDK 21과 Jakarta 변환 뒤 빌드가 통과하지 않았습니다. {_fw_who(r)}라 빌드 오류는 자동으로 고치지 않았고, "
              f"{rest} 프로젝트 담당자와 프레임워크 담당자가 고쳐 주셔야 합니다.", ""]
        return L + ["이 문서는 마이그레이션을 요청하신 담당자께 드리는 안내입니다. 무엇을 바꿨는지, 전환 후 무엇을 확인하셔야 "
                    "하는지 순서대로 적었습니다.", ""]
    if _build_failed(r):
        where = [x for x, has in (("「해결하지 못한 오류」", bool(open_errors(r))), ("「진행하지 않은 항목」", bool(r.blocked)))
                 if has]
        if where:
            obj = "을" if where[-1].endswith("항목」") else "를"
            L += [f"최종 빌드가 아직 통과하지 않습니다. 아래 {'와 '.join(where)}{obj} 먼저 봐 주세요.", ""]
        else:
            L += ["최종 빌드가 아직 통과하지 않습니다. 「참고」의 마지막 빌드 로그를 먼저 봐 주세요.", ""]
    return L + ["이 문서는 마이그레이션을 요청하신 담당자께 드리는 안내입니다. 무엇을 어떻게 바꿨는지, 전환 후 무엇을 "
                "확인하셔야 하는지 순서대로 적었습니다.", ""]


# ---------------------------------------------------------------------------------------------------------- 전환 내용
def _reason_ko(reason: str) -> str:
    """버전 변경 줄의 이유 코드를 비고 문장으로 바꾼다. 모르는 코드는 기존 번역을 쓴다."""
    reason = str(reason or "")
    if not reason:
        return "-"
    if reason in REASON_KO:
        return REASON_KO[reason]
    if reason.startswith("advisory:"):
        return f"{_code(reason.split(':', 1)[1])} 교체 권고"
    m = _TRIGGER_RE.match(reason)
    if m:
        return f"사내 선택 아티팩트로 대체({', '.join(_code(x.strip()) for x in m.group('m').split(','))})"
    return tr_reason(LANG, reason)


def _before_cell(c: VersionChange) -> str:
    if c.before_source == "managed":
        return f"{c.before} (부모 pom에서 지정)" if c.before else "부모 pom에서 지정"
    if c.before_source == "absent" and not c.before:
        return "없음"
    return c.before or "-"


def _after_cell(c: VersionChange) -> str:
    if c.after_source == "removed":
        return "삭제"
    if c.after_source == "managed" and not c.after:
        return "부모 pom에서 지정"
    if (c.carried_over or c.boot_bom_pin) and c.after:
        return f"{c.after} (pom에 직접 지정)"
    if c.after_source == "managed":
        return f"{c.after} (부모 pom에서 지정)"
    return c.after or "-"


def _dep_item(item: str) -> Optional[str]:
    """추가 / 제거 목록의 한 항목(``g:a[:v] (tag)``)을 백틱과 한국어 꼬리표로 바꾼다. 부모 pom은 위 표에 있으므로 뺀다."""
    s = str(item or "").strip()
    if not s or s.startswith("parent "):
        return None
    prefix = ""
    if s.startswith("bom "):
        prefix, s = "BOM ", s[4:]
    m = re.match(r"^(?P<coord>\S+)(?: \((?P<tag>.+)\))?$", s)
    if not m:
        return prefix + _code(s)
    tag = m.group("tag")
    return prefix + _code(m.group("coord")) + (f" ({_reason_ko(tag)})" if tag else "")


def conversion_lines(r: RunReport) -> list[str]:
    """``## 전환 내용``을 만든다. Java, Spring Boot, 부모 pom 표와 의존성 버전 표, 추가 / 제거한 의존성을 담는다.
    인수인계 데이터가 없거나 바뀐 것이 없으면 빈 목록을 반환한다."""
    h = r.handover
    if h is None:
        return []
    tops = []
    for label, b, a, empty in (("Java", h.java_before, h.java_after, "없음"),
                               ("Spring Boot", h.spring_boot_before, h.spring_boot_after, "사용하지 않음"),
                               ("부모 pom", h.parent_before, h.parent_after, "없음")):
        if not (b or a) or _same_version(b, a):
            continue
        if label == "부모 pom":
            tops.append((label, _parents(b) if b else empty, _parents(a) if a else empty))
        else:
            tops.append((label, _cell(b, 60) if b else empty, _cell(a, 60) if a else empty))
    changes = [c for c in h.changes if c.kind != "parent" and not _same_change(c)]
    added = [x for x in (_dep_item(i) for i in h.added) if x]
    removed = [x for x in (_dep_item(i) for i in h.removed) if x]
    if not (tops or changes or added or removed):
        return []
    L = ["## 전환 내용", ""]
    if tops:
        L += ["| 항목 | 변경 전 | 변경 후 |", "|---|---|---|"]
        L += [f"| {label} | {b} | {a} |" for label, b, a in tops]
        L.append("")
    if changes:
        L += ["### 의존성 버전", "", "| 항목 | 변경 전 | 변경 후 | 비고 |", "|---|---|---|---|"]
        for c in changes:
            warn = "⚠ " if c.carried_over or c.boot_bom_pin else ""
            L.append(f"| {warn}{KIND_KO.get(c.kind, c.kind)} {_code(_cell(c.key, 120))} | {_cell(_before_cell(c), 120)} | "
                     f"{_cell(_after_cell(c), 120)} | {_cell(_reason_ko(c.reason), 200)} |")
        L.append("")
        cells = " ".join(_before_cell(c) + " " + _after_cell(c) for c in changes)
        if "부모 pom에서 지정" in cells:
            L += ["\"부모 pom에서 지정\"은 pom에 버전을 적지 않고 부모 pom이나 BOM이 정한 버전을 쓴다는 뜻입니다.", ""]
        carried = [c for c in changes if c.carried_over]
        pinned = [c for c in changes if c.boot_bom_pin and not c.carried_over]
        if carried:
            lead = f"⚠ 표시한 의존성 {len(carried)}개는" if not pinned else f"⚠ 표시한 의존성 중 {len(carried)}개는"
            L += [f"{lead} 새 부모 pom과 Spring Boot BOM에 버전이 없습니다. 빌드가 되도록 기존에 쓰던 버전을 pom에 직접 "
                  "적어 두었으며, Spring Boot 4에서 동작하는지는 확인이 필요합니다.", ""]
        if pinned:
            lead = f"⚠ 표시한 의존성 {len(pinned)}개는" if not carried else f"나머지 {len(pinned)}개는"
            L += [f"{lead} 새 부모 pom에 버전이 없어 Spring Boot BOM의 버전을 pom에 직접 적었습니다. 기존 버전과 다르므로 "
                  "동작을 확인해 주세요.", ""]
    elif tops:
        L += ["의존성 버전은 바뀌지 않았습니다.", ""]
    if added:
        L.append(f"- 추가한 의존성: {', '.join(added)}")
    if removed:
        L.append(f"- 제거한 의존성: {', '.join(removed)}")
    if added or removed:
        L.append("")
    return L


# ---------------------------------------------------------------------------------------------------------- 확인해 주세요
def _replacement_text(f: AdvisoryFinding) -> str:
    """대체 라이브러리를 보여 줄 말을 만든다. 한국어 문장이 있으면 그것을 쓰고, 좌표면 백틱으로 감싼다."""
    if (f.replacement_ko or "").strip():
        return f.replacement_ko.strip()
    rep = (f.replacement or "").strip()
    if not rep:
        return ""
    txt = _code(rep) if " " not in rep else rep
    v = (f.replacement_version or "").strip()
    if v and v[0].isdigit():
        txt += f" {v}"
    return txt


def _sentence(text: str) -> str:
    """문장 끝에 마침표가 없으면 붙인다."""
    s = " ".join(str(text or "").split())
    return s if not s or s[-1] in ".!?。…" else s + "."


def _path_tail(path: str) -> str:
    """파일 경로에서 패키지 꼬리를 뗀다. Java 소스 루트 뒤의 경로이고, 없으면 마지막 두 단계다."""
    p = str(path or "").replace("\\", "/").strip("/")
    for root in ("src/main/java/", "src/test/java/", "src/main/resources/", "src/test/resources/"):
        i = p.rfind(root)
        if i != -1:
            return p[i + len(root):]
    return "/".join(p.split("/")[-2:])


def current_path(path: str, changed: Iterable[str]) -> str:
    """LLM이 고친 파일의 경로를 지금 경로로 바꾼다.
    코드 수정 내역에 없는 경로이고 파일 이름과 패키지 꼬리가 같은 경로가 하나뿐이면 그 경로를 쓴다."""
    paths = [str(c) for c in changed]
    if path in paths:
        return path
    tail = _path_tail(path)
    hits = [c for c in paths if c == tail or c.endswith("/" + tail)] if tail else []
    return hits[0] if len(hits) == 1 else path


def _subs(parts: Iterable[str]) -> str:
    """항목 아래 하위 목록 줄을 만든다. 번호 들여쓰기는 ``check_lines``가 맞춘다."""
    return "".join(f"\n- {x}" for x in parts if x)


def advisory_item(f: AdvisoryFinding, changed: Iterable[str] = ()) -> str:
    """라이브러리 권고 하나를 확인해 주세요 항목으로 만든다.
    머리 줄은 권고 제목과 이번 전환에서 한 일이고, 하위 목록은 대체 라이브러리, 설명, 할 일, 수정한 파일, 문서 순이다.
    내용이 없는 하위 줄은 넣지 않는다."""
    title = " ".join((f.title_ko or f.title or f.library or f.advisory_id or "").split())
    lead = [f"**{_cell(_sentence(title), 300)}**"]
    summary = (f.summary_ko or "").strip() or (f.summary or "").strip()
    follow = (f.followup_ko or "").strip() or (f.followup or "").strip()
    if f.outcome == "replaced":
        lead.append("이번 전환에서 교체했습니다. " + ("아래 「할 일」대로 동작을 확인해 주세요." if follow
                                                else "동작을 확인해 주세요."))
    else:
        v = (f.detected_version or "").strip()
        kept = (f"이번 전환에서는 자동으로 바꾸지 않고 기존 버전({v})을 그대로 두었습니다." if v
                else "이번 전환에서는 자동으로 바꾸지 않았습니다.")
        if f.outcome != "still-present":
            kept = "전환 후에도 남아 있는지는 확인하지 못했습니다. " + kept
        rep_there = bool(_replacement_text(f))
        if rep_there and follow:
            kept += " 아래 「대체할 라이브러리」로 바꾸고 「할 일」을 확인해 주세요."
        elif rep_there:
            kept += " 아래 「대체할 라이브러리」로 바꿔 주세요."
        elif follow:
            kept += " 아래 「할 일」을 확인해 주세요."
        lead.append(kept)
    rep = _replacement_text(f)
    subs = []
    if rep:
        subs.append(f"{'교체한' if f.outcome == 'replaced' else '대체할'} 라이브러리: {_cell(rep, 300)}")
    if summary:
        subs.append(f"설명: {_cell(_sentence(summary), ITEM_CHARS)}")
    if follow:
        subs.append(f"할 일: {_cell(_sentence(follow), ITEM_CHARS)}")
    changed = list(changed)
    files = list(dict.fromkeys(current_path(x, changed) for x in f.llm_fixed_files or []))
    if files:
        shown = ", ".join(_code(x) for x in files[:ADVISORY_FILES_MAX])
        more = f" 외 {len(files) - ADVISORY_FILES_MAX}개" if len(files) > ADVISORY_FILES_MAX else ""
        subs.append(f"수정한 파일: {shown}{more} (아래 「코드 수정 내역」)")
    if f.docs:
        subs.append("문서: " + ", ".join(f"[{i}]({d})" for i, d in enumerate(f.docs[:ADVISORY_DOCS_MAX], 1)))
    return _cell(" ".join(lead), ITEM_CHARS) + _subs(subs)


def _advisory_keys(findings: list[AdvisoryFinding]) -> set[str]:
    keys: set[str] = set()
    for f in findings:
        keys |= {":".join(a.split(":")[:2]) for a in f.artifacts}
        rep = (f.replacement or "").strip()
        if rep and " " not in rep and rep.count(":") in (1, 2):
            keys.add(":".join(rep.split(":")[:2]))
    return keys


def _from_advisory(line: str, findings: list[AdvisoryFinding], adv_followups: set[str], adv_keys: set[str]) -> bool:
    """인수인계 줄이 라이브러리 권고에서 나온 줄인지 확인한다. 권고 항목이 이미 같은 내용을 말한다."""
    s = " ".join(str(line).split())
    if _norm(s) in adv_followups:
        return True
    for f in findings:
        if f.title and s.startswith(" ".join(f.title.split())):
            return True
        if f.library and (s.startswith(f"verify {f.library} → ") or s.startswith(f"code rewritten by the LLM for {f.library}:")
                          or (s.startswith(f"{f.library} → ") and "applied by the migration" in s)):
            return True
    m = _CARRIED_LINE.match(s)
    return bool(m and ":".join(m.group("key").split(":")[:2]) in adv_keys)


def _adv_followups(findings: list[AdvisoryFinding]) -> set[str]:
    try:
        from sbmig.advisory import followups_for
        return {_norm(x) for x in (followups_for(findings) or [])}
    except Exception:  # pragma: no cover - advisory package half-built
        return set()


def _pointer(line: str, r: RunReport) -> Optional[str]:
    """다른 절을 가리키는 영어 줄을 한국어 항목으로 바꾼다. 해당하지 않으면 None, 버릴 줄이면 빈 문자열을 반환한다."""
    if _CAP_LINE.match(line):
        return ""
    m = _BLOCKED_PTR.match(line)
    if m:
        return (f"**진행하지 않은 항목 {m.group(1)}건** — 프로젝트 안에서 해결할 수 없어 손대지 않았습니다. "
                "아래 「진행하지 않은 항목」을 봐 주세요.")
    m = _UNRESOLVED_PTR.match(line) or _NO_FIX_PTR.match(line)
    if m and no_fix_loop(r):
        return f"**{NO_FIX_TITLE} {m.group(1)}건** — 자동으로 고치지 않았습니다. 아래 「{NO_FIX_TITLE}」를 봐 주세요."
    if m and _UNRESOLVED_PTR.match(line):
        return f"**해결하지 못한 오류 {m.group(1)}건** — 최종 빌드에 남아 있습니다. 아래 「해결하지 못한 오류」를 봐 주세요."
    m = _JAVAX_PTR.match(line)
    if m:
        return (f"**javax API jar {m.group(1)}개** — 전환 뒤에도 클래스패스에 남아 있습니다. 아래 "
                f"「{tr(LANG, JAVAX_JARS_TITLE)}」를 봐 주세요.")
    m = _OPERATOR_PTR.match(line)
    if m:
        return (f"**메이저 버전을 올린 변경 {m.group(1)}건** — 빌드는 되지만 동작이 바뀔 수 있습니다. 아래 「운영자 확인 "
                "필요」를 봐 주세요.")
    m = _REVIEW_MORE.match(line)
    if m:
        return (f"구조 점검에서 나온 문제가 {m.group(1)}건 더 있습니다. `migration-report.json`의 "
                "`structure_review`에서 볼 수 있습니다.")
    return None


def _carried_item(keys: list[str]) -> str:
    shown = ", ".join(_code(k) for k in keys[:10]) + (f" 외 {len(keys) - 10}개" if len(keys) > 10 else "")
    return (f"**기존 버전을 유지한 의존성** — {shown}. 새 부모 pom에 버전이 없어 기존 버전을 pom에 직접 적었습니다. "
            "Spring Boot 4에서 동작하는지 확인하고, 필요하면 버전을 올리거나 다른 라이브러리로 교체해 주세요.")


def _merge_item(lines: list[str]) -> str:
    return ("**모듈 병합에서 겹친 파일** — 같은 경로의 파일이 여러 모듈에 있어 `<modules>` 순서에서 첫 모듈의 파일을 "
            "남겼습니다. 나머지 파일은 실행 디렉토리에 따로 저장했습니다." + _subs(_cell(tr_line(LANG, x), 500) for x in lines))


_REVIEW_LINES = (("structure review warning — ", "구조 점검에서 나온 주의 사항"),)
# tr_line으로 옮기는 영어 줄 가운데 첫머리로 종류를 확실히 알 수 있는 줄에만 굵은 머리를 붙인다
_LEADS = ((re.compile(r"^test sources (were neither compiled nor run|do not compile)"), "테스트"),
          (re.compile(r"^test fails at runtime"), "실행할 때 실패하는 테스트"),
          (re.compile(r"^\d+ externalized (setting|key)\(s\) "), "외부 설정 값"),
          (re.compile(r"^bundled jar `"), "프로젝트에 포함된 jar"),
          (re.compile(r"^JSP javax references remain"), "JSP에 남은 javax 이름"),
          (re.compile(r"^structure review fix \(edits / script\) changed"), "구조 점검에서 고친 pom"),
          (re.compile(r"^structure review: critical issue not fixed — "), "구조 점검에서 고치지 못한 문제"),
          (re.compile("^" + re.escape(HTTP_BLOCKED_PREFIX)), "Maven 저장소 접근 차단"),
          (re.compile(r"^the LLM was unavailable during this run"), "LLM 호출 실패"),
          (re.compile("^" + re.escape(REWRITE_FAILED_PREFIX)), "자동 변환 실패"),
          (re.compile("^" + re.escape(POM_CHARSET_PREFIX)), "pom 글자 인코딩"))


def _review_item(line: str, r: RunReport) -> Optional[str]:
    """구조 점검의 주의 사항 줄을 한국어 항목으로 만든다.
    점검 결과에 같은 이슈가 있으면 저장된 문제와 힌트 전문을 쓴다. 해당하지 않으면 None을 반환한다."""
    for prefix, lead in _REVIEW_LINES:
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix):]
        where, _, tail = rest.partition(": ")
        problem, _, hint = tail.partition(" → ")
        rv = r.structure_review
        for i in (rv.issues if rv is not None else []):
            p = " ".join(str(i.problem or "").split())
            if (i.file or "(project)") == where and p and _norm(tail).startswith(_norm(p[:60]).rstrip("…")):
                problem, hint = p, " ".join(str(i.fix_hint or "").split())
                break
        text = " ".join(x for x in (_sentence(problem), _sentence(hint)) if x)
        head = f"**{lead}** — " + (f"{_code(where)}: " if where and where != "(project)" else "")
        return head + _cell(text, 800)
    return None


def _lead_line(line: str, limit: int) -> str:
    """영어 줄을 ``tr_line``으로 옮긴다. 종류를 알 수 있는 줄이면 굵은 머리를 앞에 붙인다."""
    text = _cell(tr_line(LANG, line), limit)
    for rx, lead in _LEADS:
        if rx.search(line):
            return f"**{lead}** — {text}"
    return text


def check_items(r: RunReport) -> list[str]:
    """``## 확인해 주세요``의 항목들을 만든다. 라이브러리 권고, 인수인계 할 일, 남은 후속 조치 순이며 같은 내용은 한 번만 넣는다.
    최종 빌드가 실패했으면 해결하지 못한 오류와 진행하지 않은 항목을 맨 앞에 둔다."""
    findings = list(r.advisories or [])
    adv_fu = _adv_followups(findings)
    adv_keys = _advisory_keys(findings)
    changed = [c.path for c in r.code_changes or []]
    items: list[str] = [advisory_item(f, changed) for f in findings]
    first: dict[str, str] = {}                    # 빌드가 실패했을 때 앞에 둘 항목
    seen: list[str] = []
    ant = _ant_refused(r)

    def said(line: str) -> bool:
        n = _norm(line)
        for x in seen:
            short, long_ = (n, x) if len(n) <= len(x) else (x, n)
            if long_ == short or (long_.startswith(short) and not long_[len(short)].isalnum()):
                return True
        return False

    def add_pointer(line: str) -> None:
        ptr = _pointer(line, r)
        if not ptr:
            return
        if _UNRESOLVED_PTR.match(line) or _NO_FIX_PTR.match(line):
            first.setdefault("unresolved", ptr)
        elif _BLOCKED_PTR.match(line):
            first.setdefault("blocked", ptr)
        items.append(ptr)

    carried: list[str] = []
    h = r.handover
    lines = list(h.from_here) if h is not None else []
    lines += list(r.manual_followups or [])
    if h is None:
        if r.operator_checks:
            add_pointer(f"operator checks: {len(r.operator_checks)} major upgrade(s) — see \"Operator checks\" above")
        if r.blocked:
            add_pointer(f"{len(r.blocked)} cluster(s) blocked — see Blocked")
        left = sum(1 for e in entries_of(r) if e.status in OPEN_STATUSES) if entries_of(r) else len(r.unresolved)
        if left:
            add_pointer(f"{left} unresolved error(s) — see Error list")
    for raw in lines:
        line = " ".join(str(raw or "").split())
        if not line or said(line):
            continue
        seen.append(_norm(line))
        if ant and (line.startswith(ABORT_PREFIX) or line.startswith(ANT_DETAIL_PREFIX)):
            continue                              # 제목 아래 안내에 이미 있다
        if _from_advisory(line, findings, adv_fu, adv_keys):
            continue
        m = _CARRIED_LINE.match(line)
        if m:
            carried.append(m.group("key"))
            continue
        if _CARRIED_SUMMARY.match(line):
            continue
        if _pointer(line, r) is not None:
            add_pointer(line)
            continue
        if line.startswith(ABORT_PREFIX):
            items.append(f"**실행 중단** — {_cell(tr_line(LANG, line[len(ABORT_PREFIX):]), 800)}")
            continue
        review = _review_item(line, r)
        if review:
            items.append(review)
            continue
        items.append(_lead_line(line, 2000 if line.startswith(HTTP_BLOCKED_PREFIX) else 800))
    if carried:
        items.append(_carried_item(carried))
    if h is not None and h.merge_conflicts:
        items.append(_merge_item(list(h.merge_conflicts)))
    if _build_failed(r):
        top = [first[k] for k in ("unresolved", "blocked") if k in first]
        items = top + [x for x in items if x not in top]
    # 자동 변환 실패는 결과 전체에 걸리므로 언제나 첫 항목으로 둔다
    failed = [x for x in items if x.startswith("**자동 변환 실패**")]
    items = failed + [x for x in items if x not in failed]
    return [x for x in dict.fromkeys(items) if x]


def check_lines(r: RunReport) -> list[str]:
    """``## 확인해 주세요``를 만든다. 확인할 것이 없으면 그렇다고 한 문장을 쓴다.
    항목의 하위 목록은 번호 너비만큼 들여 써서 번호가 이어지게 한다."""
    items = check_items(r)
    L = ["## 확인해 주세요", ""]
    if not items:
        return L + ["따로 확인하실 사항은 없습니다.", ""]
    lead = ("다시 실행하기 전에 확인하실 사항입니다." if _aborted(r) else
            "전환 후 담당 팀에서 확인하실 사항입니다. 위에서부터 차례로 봐 주세요.")
    L += [lead, ""]
    for i, x in enumerate(items, 1):
        mark = f"{i}. "
        head, *subs = x.split("\n")
        L.append(mark + head)
        L += [" " * len(mark) + s for s in subs]
    L.append("")
    return L


# ---------------------------------------------------------------------------------------------------------- 코드 수정 내역
def recipe_words(recipes: Iterable[str]) -> list[str]:
    """OpenRewrite 레시피 id 목록을 사람이 읽는 말로 바꾼다. 같은 말은 한 번만 쓴다."""
    out: list[str] = []
    security: list[int] = []
    for rid in recipes:
        name = str(rid).rsplit(".", 1)[-1]
        word = None
        for rx, fn in _RECIPE_WORDS:
            m = rx.match(name)
            if m:
                if name.startswith("UpgradeSpringSecurity_"):
                    security.append(int(m.group(1)))
                    word = "Spring Security"
                else:
                    word = fn(m)
                break
        word = word or _code(name)
        if word not in out:
            out.append(word)
    if security:
        out = [f"Spring Security {max(security)}" if w == "Spring Security" else w for w in out]
    return out


def _restructure_line(r: RunReport) -> Optional[str]:
    titles = {s.id: s.title for s in (r.plan.steps if r.plan else [])}
    executed = [sid for pr in r.phases or [] for sid in pr.step_ids]
    for sid in executed:
        base = sid.split("@", 1)[0]
        if base == "restructure.apply":
            m = _RESTRUCTURE_INTO_RE.match(titles.get(sid, "") or "")
            if m:
                mods = ", ".join(_code(x.strip()) for x in m.group("mods").split(",") if x.strip())
                return f"프로젝트를 {mods} 구조로 재구성하고 사내 부모 pom을 적용했습니다."
            return "프로젝트를 회사 표준 구조로 재구성했습니다."
        if base in ("restructure.msa_pom", "restructure.pom_only", "restructure.boot_pom"):
            return "디렉토리 구조는 그대로 두고 pom의 부모와 사내 필수 / 선택 아티팩트를 정리했습니다."
    return None


def auto_lines(r: RunReport) -> list[str]:
    """``### 자동 변환``의 줄을 만든다. pom 합성, OpenRewrite, 재구성, JSP 변환, sbmig가 고친 pom 순이다. 없으면 빈 목록이다."""
    out: list[str] = []
    ps = getattr(r, "pom_synthesis", None)
    if ps is not None and getattr(ps, "ok", False):
        n = pom_synthesis_counts(ps)
        out.append(f"- Ant 프로젝트여서 빌드 파일({_code(ps.build_file)})과 폴더 구조를 읽어 pom을 새로 만들었습니다"
                   f"({_code(ps.pom_path)}). jar {n['jars']}개 가운데 좌표를 찾지 못한 jar는 {n['unknown']}개입니다.")
    for pr in r.phases or []:
        for x in pr.rewrite_results:
            words = ", ".join(recipe_words(x.recipes))
            if not x.ok:
                out.append(f"- OpenRewrite 레시피({words})를 실행하지 못했습니다: {_cell(x.error or '-', 300)}")
            elif x.changed_files:
                out.append(f"- OpenRewrite 레시피로 파일 {len(x.changed_files)}개를 변환했습니다: {words}.")
            else:
                out.append(f"- OpenRewrite 레시피({words})를 실행했지만 바뀐 파일은 없습니다.")
    rs = _restructure_line(r)
    if rs:
        out.append(f"- {rs}")
    files, refs = jsp_jakarta_counts(r)
    if files:
        out.append(f"- JSP 파일 {files}개에서 javax 이름 {refs}곳을 jakarta로 바꿨습니다. OpenRewrite는 JSP를 읽지 않아 "
                   "sbmig가 따로 바꿨습니다.")
    fixes = [ln for ln in pom_correction_lines(r, LANG) if ln.startswith("- ")]
    if fixes:
        out.append("- sbmig가 변환 단계에서 직접 고친 pom:")
        out += ["  " + ln for ln in fixes]
    return out


def _change_head(c: Any) -> str:
    if c.status == "added":
        stat = f"새 파일, +{c.added}"
    elif c.status == "deleted":
        stat = f"삭제, −{c.removed}"
    else:
        stat = f"+{c.added} −{c.removed}"
    return f"{_code(c.path)} ({stat})"


def code_change_lines(r: RunReport) -> list[str]:
    """``## 코드 수정 내역``을 만든다. 자동 변환 요약과 직접 수정한 파일(diff 포함)을 담는다. 둘 다 없으면 빈 목록이다."""
    auto = auto_lines(r)
    changes = list(r.code_changes or [])
    total = max(int(r.code_changes_total or 0), len(changes))
    if not auto and not total:
        return []
    L = ["## 코드 수정 내역", ""]
    if auto:
        L += ["### 자동 변환", ""] + auto + [""]
        L += ["자동 변환으로 바뀐 내용은 양이 많아 여기에 싣지 않았습니다. 전체 변경은 `changes.patch`에서 볼 수 있습니다.", ""]
    if not total:
        return L
    L += [f"### 직접 수정한 파일 — {total}개", "",
          "자동 변환 뒤 빌드 오류와 구조 점검에서 나온 문제를 해결하려고 수정한 파일입니다. 아래 diff는 자동 변환 직후와 "
          "최종 결과를 비교한 것입니다.", ""]
    with_diff = [c for c in changes if (c.diff or "").strip()]
    without = [c for c in changes if not (c.diff or "").strip()]
    for c in with_diff:
        L += [f"#### {_change_head(c)}", ""]
        notes = [" ".join(n.split()) for n in c.notes if str(n).strip()]
        if notes:
            L += [f"- {n}" for n in notes] + [""]
        reasons = [" ".join(x.split()) for x in c.reasons if str(x).strip()]
        if reasons:
            L += ["수정 이유: " + " ".join(reasons), ""]
        body = c.diff.rstrip("\n")
        fence = _fence(body)
        L += [f"{fence}diff", body, fence, ""]
        if c.truncated:
            L += ["diff가 길어 앞부분만 실었습니다. 전체는 `changes.patch`에서 볼 수 있습니다.", ""]
    extra = total - len(changes)
    rest = len(without) + extra
    if rest:
        lead = "그 밖에 수정한 파일" if with_diff else "수정한 파일"
        L += [f"{lead} {rest}개는 diff를 싣지 않았습니다. `changes.patch`에서 볼 수 있습니다.", ""]
        if without:
            L += ["| 파일 | 변경 |", "|---|---|"]
            for c in without:
                stat = ("새 파일" if c.status == "added" else "삭제" if c.status == "deleted" else "") or ""
                nums = f"+{c.added} −{c.removed}"
                L.append(f"| {_code(_cell(c.path, 300))} | {stat + ', ' if stat else ''}{nums} |")
            L.append("")
        if extra > 0 and without:
            L += [f"표에 없는 파일 {extra}개도 `changes.patch`에 있습니다.", ""]
    return L


# ---------------------------------------------------------------------------------------------------------- 오류 / 진행하지 않은 항목
def open_errors(r: RunReport) -> list[tuple[str, str, str]]:
    """최종 빌드에 남은 오류를 (파일, 줄, 메시지)로 반환한다. 오류 목록이 없으면 마지막 빌드의 미해결 오류를 쓴다."""
    entries = entries_of(r)
    rows: list[tuple[str, str, str]] = []
    if entries:
        for e in entries:
            if getattr(e, "status", "") in OPEN_STATUSES:
                rows.append((e.file or e.module or "-", str(e.line) if e.line else "-", entry_text(e)))
    else:
        for e in r.unresolved or []:
            where = e.file or e.module or (e.artifact.coordinates if e.artifact else "-")
            rows.append((where, str(e.line) if e.line else "-", e.message))
    order: dict[str, int] = {}
    for f, _, _ in rows:
        order.setdefault(f, len(order))
    return sorted(rows, key=lambda x: order[x[0]])


def open_error_lines(r: RunReport) -> list[str]:
    """``## 해결하지 못한 오류``를 만든다. 남은 오류가 없으면 빈 목록이다."""
    rows = open_errors(r)
    if not rows:
        return []
    log = ""
    if r.final_build is not None and r.final_build.log_path:
        log = f" 마지막 빌드 로그는 {_code(_rel(r.final_build.log_path, r))}입니다."
    if no_fix_loop(r):
        L = [f"## {NO_FIX_TITLE} — {len(rows)}건", "",
             f"JDK 21과 Jakarta 변환 뒤 최종 빌드에 남아 있는 오류입니다. {_fw_who(r)}라 자동으로 고치지 않았으므로 프로젝트 "
             f"담당자와 프레임워크 담당자가 고쳐 주셔야 합니다. 파일별로 정리했습니다. 오류별 상세 기록은 `errors.md`에 "
             f"있습니다.{log}", ""]
    else:
        L = [f"## 해결하지 못한 오류 — {len(rows)}건", "",
             f"최종 빌드에 남아 있는 오류입니다. 파일별로 정리했습니다. 오류별 상세 기록은 `errors.md`에 있습니다.{log}", ""]
    L += ["| 파일 | 줄 | 오류 |", "|---|---|---|"]
    for f, line, msg in rows[:OPEN_ERRORS_MAX]:
        L.append(f"| {_code(_cell(f, 200)) if f != '-' else '-'} | {line} | {_cell(msg, 300)} |")
    if len(rows) > OPEN_ERRORS_MAX:
        L += ["", f"그 밖에 {len(rows) - OPEN_ERRORS_MAX}건은 `errors.md`에 있습니다."]
    L.append("")
    return L


def blocked_lines(r: RunReport) -> list[str]:
    """``## 진행하지 않은 항목``을 만든다. 차단 항목이 없으면 빈 목록이다."""
    if not r.blocked:
        return []
    L = ["## 진행하지 않은 항목", "",
         f"프로젝트 안에서 해결할 수 없어 손대지 않은 항목이 {len(r.blocked)}건 있습니다. 라이브러리 담당자나 환경 쪽의 조치가 "
         "필요합니다. 나머지 전환은 그대로 진행했습니다.", ""]
    L += blocked_rows(r.blocked, LANG)
    L.append("")
    for b in r.blocked:
        if not b.errors:
            continue
        L += [f"<details><summary><code>{b.id}</code> 관련 오류 {len(b.errors)}건</summary>", ""]
        for e in b.errors[:10]:
            loc = f"{e.file}:{e.line}" if e.file and e.line else (e.file or (e.artifact.coordinates if e.artifact
                                                                              else e.module or "-"))
            L.append(f"- `{_cell(loc, 200)}` {_cell(e.message, 300)}")
        L += ["", "</details>", ""]
    return L


def special_lines(r: RunReport, cfg: Any = None) -> list[str]:
    """해당하는 실행에만 붙는 특수 절을 정해진 순서로 모은다.
    운영자 확인, pom 합성, ADF, 포함된 jar, javax API jar, 외부 설정 값, 취약점 조치 순이다."""
    L: list[str] = []
    L += _promote(operator_check_lines(r, LANG))
    L += _promote(pom_synthesis_lines(r, LANG, cfg))
    L += adf_lines(r, LANG)
    L += bundled_jars_lines(r, LANG)
    L += javax_api_lines(r, LANG, cfg)
    h = r.handover
    if h is not None and h.properties_to_set:
        L += _promote(properties_lines(h, r, LANG))
    L += remediation_lines(r, LANG)
    return L


# ---------------------------------------------------------------------------------------------------------- 참고
def _review_ko(r: RunReport) -> Optional[str]:
    rv = r.structure_review
    if rv is None:
        return None
    if rv.verdict in ("skipped", "failed"):
        return review_summary(rv, LANG)
    if not rv.issues:
        return "문제 없음"
    crit = len(rv.critical)
    return f"심각 {crit}건, 주의 {len(rv.issues) - crit}건"


def _details(summary: str, body: list[str]) -> list[str]:
    return [f"<details><summary>{summary}</summary>", "", *body, "", "</details>", ""]


def reference_lines(r: RunReport) -> list[str]:
    """``## 참고``를 만든다. 실행 정보, LLM 사용량과 비용, 산출물, 진단용 기록을 접힌 블록으로 담는다."""
    blocks: list[str] = []
    info = [f"- 프로젝트 유형: `{r.case_type.value}`"]
    p = r.profile
    if p is not None:
        parts = []
        if p.java_version:
            parts.append(f"Java {p.java_version}")
        if p.spring_boot_version:
            parts.append(f"Spring Boot {p.spring_boot_version}")
        if p.spring_version:
            parts.append(f"Spring {p.spring_version}")
        parts.append(f"pom {len(p.poms)}개")
        info.append("- 전환 전: " + ", ".join(parts))
    if run_scope(r) is not None:
        info.append("- 범위: JDK 21과 jakarta만 적용(사내 프레임워크 프로젝트)")
    executed = [sid for pr in r.phases or [] for sid in pr.step_ids]
    if executed:
        names = list(dict.fromkeys(STEP_KO.get(s.split("@", 1)[0], f"`{s}`") for s in executed))
        info.append("- 실행 단계: " + " → ".join(names))
    recipes = list(dict.fromkeys(x for pr in r.phases or [] for rw in pr.rewrite_results for x in rw.recipes))
    if recipes:
        info.append("- OpenRewrite 레시피: " + ", ".join(_code(x) for x in recipes))
    rv = _review_ko(r)
    if rv:
        info.append(f"- 구조 점검: {_cell(rv, 300)}")
    for f in r.flags or []:
        h = flag_help(f, LANG)
        info.append(f"- 플래그 {_code(f)}" + (f": {h}" if h else ""))
    blocks += _details("실행 정보", info)
    cost = cost_lines(r, LANG)
    if cost:
        body = cost[1:] if cost[0].startswith("## ") else cost
        while body and not body[0].strip():
            body = body[1:]
        while body and not body[-1].strip():
            body = body[:-1]
        blocks += _details("LLM 사용량과 비용", body)
    elif r.llm_usage.calls:
        u = r.llm_usage
        blocks += _details("LLM 사용량과 비용", [f"- LLM 호출 {u.calls}회, 입력 토큰 {u.input_tokens:,}, 출력 토큰 "
                                                f"{u.output_tokens:,}" + (f" ({model_cell(u)})" if u.model else "")])
    o = r.outputs or {}
    seen: dict[str, list[str]] = {}
    for key, label in OUTPUT_KO:
        if o.get(key):
            path = _rel(o[key], r) + ("/" if key in ("logs", "project_dir") else "")
            seen.setdefault(label, []).append(_code(path))
    outs = [f"- {label}: {', '.join(v)}" for label, v in seen.items()]
    if r.final_build is not None and r.final_build.log_path:
        outs.append(f"- 마지막 빌드 로그: {_code(_rel(r.final_build.log_path, r))}")
    if outs:
        blocks += _details("산출물", outs)
    diag = [f"- plan: {_cell(n, 400)}" for n in (r.plan.notes if r.plan else [])]
    diag += [f"- {pr.phase.value}: {_cell(n, 400)}" for pr in r.phases or [] for n in pr.notes
             if not is_jsp_jakarta_note(n)]
    if r.handover is not None:
        diag += [f"- handover: {_cell(n, 400)}" for n in r.handover.notes]
    srv = r.structure_review
    if srv is not None:
        analysis = srv.analysis or (srv.answer.analysis if getattr(srv, "answer", None) is not None else "")
        if analysis:
            diag.append(f"- structure review: {_cell(analysis, 800)}")
    if diag:
        blocks += _details("진단용 기록", diag)
    return ["## 참고", ""] + blocks


# ---------------------------------------------------------------------------------------------------------- entry
def render_guide_ko(report: RunReport, cfg: Any = None) -> str:
    """한국어 안내서 report.md 전체를 만든다. 선택 데이터가 없어도 예외를 던지지 않는다.

    The Korean guide (INTERFACES "v2.1z-as"); `cfg` only feeds the javax / Ant sections."""
    r = report
    L = [f"# 마이그레이션 보고서 — {r.project_name}", ""]
    L += intake_notice_lines(r, LANG)
    L += head_lines(r)
    L += framework_lines(r, LANG)
    L += purpose_lines(r)
    L += conversion_lines(r)
    L += check_lines(r)
    L += code_change_lines(r)
    L += open_error_lines(r)
    L += blocked_lines(r)
    L += special_lines(r, cfg)
    L += reference_lines(r)
    return "\n".join(L).rstrip() + "\n"
