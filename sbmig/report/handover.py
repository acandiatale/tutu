"""보고서 맨 앞의 인수인계 섹션을 만드는 모듈.
이관 전후 pom을 비교해서 버전 변경, 추가되거나 삭제된 아티팩트, 이어서 할 일을 정리한다.

The owning team reads only this section of the report, so it stays short. `writer.py` calls `build_handover`
and `markdown.py` renders the result first in the report. A ⚠ marks a dependency kept at its as-is version
because the new parent / Boot BOM does not manage it. For an Ant project, every jar with unknown coordinates
gets an `mvn install:install-file` command. Nothing here raises: a failure leaves one "handover unavailable:
…" line in `notes`.

`build_handover(profile, view, report, effective=None, cfg=None)` compares the as-is poms (`profile.poms`,
plus the as-is effective dependencyManagement captured at intake for versions the old parent managed) with
the final poms of the migrated tree (`view` = Workspace / PathView). Output: parent / java / spring boot
before -> after, one row per dependency whose version changed (carried-over pins: the new parent / Boot BOM
no longer manages them), added / removed artifacts, the ordered "from here on" list (advisories not replaced,
replaced-but-verify, carried-over pins, blocked / unresolved pointers, manual follow-ups) and the merge
conflicts (`report.merge_conflicts`). Also: one `properties_to_set` row per `external` externalized-config
finding (`property_rows`) plus the guide lines of `knowledge/config/externalized.yml`; file paths a fix
attempt recorded before the restructure moved the sources are mapped to their current place
(`resolve_current_path`); the structure review adds what its script changed, unfixed critical issues and
warnings (`review_followups`).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from sbmig.core.models import (AdvisoryFinding, GAV, HandoverSummary, PomInfo, ProjectProfile, RunReport,
                               VersionChange)

log = logging.getLogger("sbmig.report")

REASON_CARRIED = "carried over (no longer managed)"
REASON_PARENT = "parent swap"
REASON_OPTIONAL = "optional bump"
# 트리거 의존성(when_present)이 있어서 추가한 선택 아티팩트, 그리고 그때 지운 트리거 의존성에 붙이는 이유 문구
REASON_OPTIONAL_TRIGGER = "optional (trigger: {matched})"
REASON_MANDATORY = "mandatory"
# 새 부모 체인이 관리하지 않아 대상 Spring Boot BOM의 버전으로 고정한 의존성에 붙이는 이유 문구
REASON_BOOT_BOM_PIN = "pinned from the target Boot BOM (not managed by the parent chain)"
REASON_BOOT = "spring boot 4 upgrade"
REASON_OTHER = "recipe / fix script"
# 수정 시도 없이 레시피만 pom을 바꾼 축소 실행에서 쓰는 이유 문구
REASON_RECIPE = "recipe"
CARRIED_LIST_MAX = 5
# "code rewritten by the LLM for <library>" 한 줄에 나열하는 파일 수
LLM_FILES_MAX = 5
# 오케스트레이터가 남기는 javax API jar 후속 작업 줄. report.javax_api_inventory에서 온다
_JAVAX_FOLLOWUP_RE = re.compile(r"^\d+ javax API jar\(s\) still on the classpath after the migration")
# "N identical duplicate(s) dropped" 줄에 나열하는 경로 수
IDENTICAL_LIST_MAX = 5
# 이어서 할 일에 넣는 구조 검토 줄 수. 넘치면 "… and N more" 한 줄로 줄인다
REVIEW_FOLLOWUPS_MAX = 8
REVIEW_FILES_MAX = 5
# sbmig가 직접 추가한 구조 검토 이슈의 접두어. 검토 스크립트가 고치지 않는다
REVIEW_SBMIG_CHECK = "(sbmig check) "
# properties_to_set 한 줄의 칸 구분자. 칸 값에는 "|"가 들어가지 않는다
PROPERTY_SEP = " | "
PROPERTY_COLUMNS = ("key", "before migration", "referenced at", "written as", "set via")
DEFAULT_SENTINEL = "__SET_ME__"
DEFAULT_MAX_REFS = 3

# RunReport에는 JSP 변환 결과를 담을 필드가 없다. 그래서 오케스트레이터가 단계 메모에 이 형식으로 남기고
# 보고서가 다시 읽는다. 쓰는 함수와 읽는 함수를 여기 한 곳에 둔다
JSP_JAKARTA_LABEL = "JSP javax → jakarta"
_JSP_JAKARTA_NOTE_RE = re.compile(r"^(?P<step>[\w.\-]+): JSP javax → jakarta: (?P<files>\d+) files?, "
                                  r"(?P<refs>\d+) references?$")
# 재구성도 같은 문장을 남기므로 개수는 단계 접두어가 붙은 메모로만 센다.
# Notes에서는 둘 다 빼서 "What we changed"에 한 번만 보이게 한다
_JSP_JAKARTA_ANY_RE = re.compile(r"^(?:[\w.\-]+: )?JSP javax → jakarta: \d+ files?, \d+ references?$")
JSP_JAVAX_LEFT = ("JSP javax references remain ({files}) — rename the javax EE names in page imports / scriptlets / "
                  "useBean to jakarta.* (the build passes, Tomcat 10+ fails at runtime)")
# 최종 빌드가 통과하지 않은 실행에서는 빌드가 통과한다고 쓰지 않는다
JSP_JAVAX_LEFT_NOT_BUILT = ("JSP javax references remain ({files}) — rename the javax EE names in page imports / "
                            "scriptlets / useBean to jakarta.* (Tomcat 10+ fails at runtime on them)")
JSP_RENAME_OFF = "; `layout.jsp_jakarta_rename` is off"
# 좌표를 찾지 못한 jar마다 install-file 명령을 만들 때 파일 이름을 이름과 버전으로 나누는 정규식.
# 분석기가 채운 artifact_id / version이 있으면 그것을 쓴다
_JAR_NAME_VERSION_RE = re.compile(r"^(?P<stem>.+?)[-_](?P<ver>\d[\w.\-]*?)\.jar$", re.IGNORECASE)

_OPTIONAL_RE = re.compile(r"optional bumped: (.*)$")
_MANDATORY_RE = re.compile(r"mandatory added: ([^;]*)")
# 트리거 의존성 때문에 재구성이 선택 아티팩트를 추가했을 때 남기는 메모의 접두어.
# Format: "optional artifacts added for trigger dependencies: g:a → <pom> (for m1, m2); …
#   — replaced: m1 removed from <pom> → g:a; …"
TRIGGER_NOTE_PREFIX = "optional artifacts added for trigger dependencies:"
# Boot BOM 버전 고정 메모와, 부모 체인에 Boot BOM이 없을 때의 인수인계 첫 줄을 알아보는 문자열.
# 보고서는 재구성 패키지를 import하지 않는다. 그래서 sbmig.restructure.restructurer의
# BOOT_PIN_NOTE_PREFIX / PARENT_NO_BOOT_BOM_*와 같은 값을 여기 둔다
BOOT_PIN_NOTE_PREFIX = "dependency versions pinned from the target Spring Boot BOM (not managed by the parent chain):"
PARENT_NO_BOOT_BOM_PREFIX = "사내 부모 체인("
PARENT_NO_BOOT_BOM_FLAG = "parent-chain-no-boot-bom"
# 보고서는 analyze 패키지를 import하지 않는다. 그래서 sbmig.analyze.hollow의
# HOLLOW_PREFIX / HOLLOW_FLAG와 같은 문자열을 여기 둔다
HOLLOW_PREFIX = "the Maven reactor compiles no sources:"
HOLLOW_FLAG = "hollow-reactor"
# 변환할 수 없는 Oracle ADF 프로젝트를 알아보는 문자열.
# sbmig.analyze.adf의 ADF_PREFIX / ADF_FLAG와 같아야 한다
ADF_PREFIX = "ADF project (Oracle JDeveloper / ADF): conversion is not possible —"
ADF_FLAG = "adf-project"
# Maven의 HTTP 차단 미러로 중단된 실행을 알아보는 문자열.
# sbmig.build.env_errors의 HTTP_BLOCKED_PREFIX / HTTP_BLOCKED_FLAG와 같아야 한다
HTTP_BLOCKED_PREFIX = "Maven (3.8.1+) refuses the HTTP repositor"
HTTP_BLOCKED_FLAG = "aborted:http-blocked"
_TRIGGER_ADDED_RE = re.compile(r"([^\s;:]+:[^\s;:]+) → \S+ \(for ([^()]*)\)")
_TRIGGER_REPLACED_RE = re.compile(r"([^\s;:]+:[^\s;:]+) removed from \S+ → ([^\s;:]+:[^\s;:]+)")

# key -> (version, source)
_Dep = tuple[Optional[str], str]


def _clean(v: Optional[str]) -> Optional[str]:
    # ${...} 속성 참조는 실제 버전이 아니다. 모르는 값으로 둬야 잘못된 버전 변경 줄이 생기지 않는다
    if not v or "${" in v:
        return None
    return v.strip() or None


def _is_bom(g: GAV) -> bool:
    return (g.type or "") == "pom" and (g.scope or "") == "import"


def _own_keys(poms: list[PomInfo]) -> set[str]:
    return {f"{p.group_id}:{p.artifact_id}" for p in poms if p.group_id}


def _roots(poms: list[PomInfo]) -> list[PomInfo]:
    """parent가 프로젝트 밖에 있는 pom을 얕은 경로부터 반환한다. 사내 parent나 BOM을 실제로 상속하는 pom이다."""
    out = [p for p in poms if p.parent is not None and not p.parent_in_repo]
    return sorted(out, key=lambda p: (p.path.count("/"), p.path))


def _main_pom(poms: list[PomInfo]) -> Optional[PomInfo]:
    for p in poms:
        if p.path == "pom.xml":
            return p
    roots = [p for p in poms if p.is_root]
    return sorted(roots, key=lambda p: (p.path.count("/"), p.path))[0] if roots else (poms[0] if poms else None)


def _parents(poms: list[PomInfo]) -> dict[str, Optional[str]]:
    out: dict[str, Optional[str]] = {}
    for p in _roots(poms):
        assert p.parent is not None
        out.setdefault(p.parent.key, _clean(p.parent.version))
    return out


def _fmt_parents(parents: dict[str, Optional[str]]) -> Optional[str]:
    if not parents:
        return None
    return ", ".join(f"{k}:{v}" if v else k for k, v in parents.items())


def _eff_lookup(effective: dict[str, Any], key: str) -> Optional[str]:
    if not effective:
        return None
    return _clean((effective.get("direct") or {}).get(key)) or _clean((effective.get("managed") or {}).get(key))


def _deps_before(poms: list[PomInfo], effective: dict[str, Any], own: set[str],
                 needs_eff: list[str]) -> dict[str, _Dep]:
    """as-is 의존성마다 (버전, 출처)를 구한다.
    pom에 버전이 있으면 declared다. 없으면 as-is effective pom에서 찾아 managed, 거기도 없으면 absent다.
    effective pom에 없는 키는 ``needs_eff``에 모은다. 프로젝트 자신의 모듈(``own``)은 비교하지 않는다."""
    out: dict[str, _Dep] = {}
    for p in poms:
        for d in p.dependencies:
            if d.key in own or not d.group_id:
                continue
            v = _clean(d.version)
            if v:
                if out.get(d.key, (None, ""))[1] != "declared":
                    out[d.key] = (v, "declared")
                continue
            if d.key in out:
                continue
            ev = _eff_lookup(effective, d.key)
            if ev is None:
                needs_eff.append(d.key)
            out[d.key] = (ev, "managed") if ev else (None, "absent")
    return out


def _deps_after(poms: list[PomInfo], own: set[str]) -> tuple[dict[str, _Dep], dict[str, str]]:
    """최종 의존성마다 (버전, declared 또는 managed)와 선언한 pom 경로를 구한다.
    managed 버전은 새 parent나 Boot BOM이 정한다. 그래서 None으로 둔다."""
    out: dict[str, _Dep] = {}
    where: dict[str, str] = {}
    for p in poms:
        for d in p.dependencies:
            if d.key in own or not d.group_id:
                continue
            v = _clean(d.version)
            if v and out.get(d.key, (None, ""))[1] != "declared":
                out[d.key] = (v, "declared")
                where[d.key] = p.path
            elif d.key not in out:
                out[d.key] = (None, "managed")
                where[d.key] = p.path
    return out, where


def _boms(poms: list[PomInfo]) -> dict[str, tuple[Optional[str], str]]:
    out: dict[str, tuple[Optional[str], str]] = {}
    for p in poms:
        for d in p.dependency_management:
            if _is_bom(d) and d.key not in out:
                out[d.key] = (_clean(d.version), p.path)
    return out


def _plugins(poms: list[PomInfo]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for p in poms:
        for pl in p.plugins:
            v = _clean(pl.version)
            if v and pl.key not in out:
                out[pl.key] = (v, p.path)
    return out


def _notes(report: RunReport) -> list[str]:
    """모든 단계와 시도의 notes를 한 목록으로 모은다."""
    out: list[str] = []
    for pr in report.phases:
        out += list(pr.notes)
        for a in pr.attempts:
            out += list(a.notes)
    return out


def _restructure_keys(notes: list[str]) -> tuple[set[str], set[str]]:
    """재구성 단계 notes에서 (버전을 올린 선택 아티팩트 키, 추가한 필수 아티팩트 키)를 꺼낸다.
    버전 변경 줄의 이유 칸에 쓴다."""
    optional: set[str] = set()
    mandatory: set[str] = set()
    for n in notes:
        m = _OPTIONAL_RE.search(n)
        if m:
            for entry in m.group(1).split(", "):
                tok = entry.strip().split(" ", 1)[0]
                if tok and tok != "-" and ":" in tok:
                    optional.add(tok)
        m = _MANDATORY_RE.search(n)
        if m:
            for tok in m.group(1).split(", "):
                tok = tok.strip()
                if tok and tok != "-" and ":" in tok:
                    mandatory.add(":".join(tok.split(":")[:2]))
    return optional, mandatory


def _trigger_keys(notes: list[str]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """재구성 메모에서 트리거로 추가한 선택 아티팩트와 그때 지운 트리거 의존성을 꺼낸다.
    인수인계의 추가 / 삭제 줄에 이유를 붙이는 데 쓴다.

    ({added optional key: matched trigger keys}, {removed trigger key: [itself]}) from the restructure's
    `optional artifacts added for trigger dependencies: …` note."""
    added: dict[str, list[str]] = {}
    removed: dict[str, list[str]] = {}
    for n in notes:
        i = n.find(TRIGGER_NOTE_PREFIX)
        if i < 0:
            continue
        text = n[i + len(TRIGGER_NOTE_PREFIX):]
        head, _, tail = text.partition(" — replaced: ")
        for m in _TRIGGER_ADDED_RE.finditer(head):
            keys = added.setdefault(m.group(1), [])
            keys += [k.strip() for k in m.group(2).split(",") if k.strip() and k.strip() not in keys]
        for m in _TRIGGER_REPLACED_RE.finditer(tail):
            removed.setdefault(m.group(1), [m.group(1)])
    return added, removed


def _boot_pins(notes: list[str]) -> dict[str, str]:
    """재구성 메모에서 대상 Boot BOM의 버전으로 고정한 의존성을 ``{g:a: 버전}``으로 꺼낸다."""
    out: dict[str, str] = {}
    for n in notes:
        i = n.find(BOOT_PIN_NOTE_PREFIX)
        if i < 0:
            continue
        for item in n[i + len(BOOT_PIN_NOTE_PREFIX):].split(","):
            parts = item.strip().split(" ")
            if len(parts) == 2 and ":" in parts[0]:
                out.setdefault(parts[0], parts[1])
    return out


def optional_trigger_reason(matched: list[str]) -> str:
    """트리거로 들어온 선택 아티팩트에 붙일 이유 문구를 만든다. 형식은 ``optional (trigger: <g:a>[, …])``다.

    The Handover reason of an optional artifact a trigger introduced, and of the trigger it replaced."""
    return REASON_OPTIONAL_TRIGGER.format(matched=", ".join(matched))


_TRIGGER_REASON_RE = re.compile(r"^optional \(trigger: (?P<matched>.*)\)$")


def tr_reason(lang: str, reason: str) -> str:
    """버전 변경 줄의 이유 문구를 ``lang`` 언어로 바꾼다.
    값이 채워진 ``optional (trigger: …)``는 번역 템플릿에 값을 다시 넣어 만든다."""
    from .i18n import tr

    m = _TRIGGER_REASON_RE.match(reason or "")
    if m:
        return tr(lang, REASON_OPTIONAL_TRIGGER, matched=m.group("matched"))
    return tr(lang, reason)


def _replacement_key(f: AdvisoryFinding) -> Optional[str]:
    r = (f.replacement or "").strip()
    if r and " " not in r and r.count(":") in (1, 2):
        return ":".join(r.split(":")[:2])
    return None


def _advisory_maps(findings: list[AdvisoryFinding]) -> tuple[dict[str, str], dict[str, str]]:
    """권고에서 (as-is 아티팩트 키별 권고 id, 대체 아티팩트 키별 권고 id) 두 딕셔너리를 만든다."""
    arts: dict[str, str] = {}
    repl: dict[str, str] = {}
    for f in findings:
        for a in f.artifacts:
            arts.setdefault(":".join(a.split(":")[:2]), f.advisory_id)
        k = _replacement_key(f)
        if k:
            repl.setdefault(k, f.advisory_id)
    return arts, repl


def _is_spring(key: str) -> bool:
    return key.startswith("org.springframework")


def _coord(key: str, v: Optional[str]) -> str:
    return f"{key}:{v}" if v else key


def _versions(profile: ProjectProfile, final: list[PomInfo], cfg: Any,
              reduced: bool = False) -> dict[str, Optional[str]]:
    """Java와 Boot 버전을 이관 전후로 구한다. 이관 후 값은 메인 pom, 다른 pom, 설정의 ``target`` 순으로 찾는다.
    사내 parent가 버전을 관리하면 최종 pom에 값이 없다. 이때는 설정 값을 쓴다.
    사내 프레임워크 프로젝트(``reduced``)는 Boot를 올리지 않는다. 그래서 Boot는 최종 pom에 있는 값만 쓴다.

    A reduced run (scope jdk-jakarta) never applies the target Boot line, so its Boot after-value is never
    `target.spring_boot_version`. Java keeps the `target.java_version` fallback (the run applied Java 21)."""
    main = _main_pom(final)
    target = getattr(cfg, "target", None) if cfg is not None else None
    java_after = main.java_version if main else None
    if not java_after:
        java_after = next((p.java_version for p in final if p.java_version), None)
    if not java_after and target is not None:
        # 사내 parent가 Java 버전을 관리하면 최종 pom에는 값이 없다
        java_after = target.java_version
    boot_after = main.spring_boot_version if main else None
    if not boot_after:
        boot_after = next((p.spring_boot_version for p in final if p.spring_boot_version), None)
    if not boot_after and target is not None and not reduced:
        boot_after = target.spring_boot_version
    return {"java_before": profile.java_version, "java_after": java_after,
            "spring_boot_before": profile.spring_boot_version, "spring_boot_after": boot_after}


def _dedupe_add(out: list[str], item: str) -> None:
    """할 일 목록에 한 줄을 추가한다.
    같은 문장이 이미 있거나 한쪽이 다른 쪽의 앞부분이면 넣지 않는다. 권고와 후속 작업이 같은 말을 자주 반복한다."""
    s = " ".join(item.split())
    if not s:
        return
    low = s.lower()
    for x in out:
        xl = x.lower()
        short, long_ = (low, xl) if len(low) <= len(xl) else (xl, low)
        if long_ == short or (long_.startswith(short) and not long_[len(short)].isalnum()):
            # 뒤에 "(replacement: …)" 같은 덧붙임만 다른 같은 문장이다
            return
    out.append(s)


# ---------------------------------------------------------------------------- 외부 설정
def _inventory(report: RunReport) -> Any:
    """외부 설정 조사 결과를 반환한다. 보고서에 없으면 프로필의 것을 쓴다."""
    inv = getattr(report, "properties", None)
    if inv is None and getattr(report, "profile", None) is not None:
        inv = getattr(report.profile, "properties", None)
    return inv


def _pcell(text: object) -> str:
    s = " ".join(str(text if text is not None else "").split()).replace("|", "/")
    return s or "-"


def env_name(key: str) -> str:
    """설정 키를 Boot relaxed binding 규칙의 환경 변수 이름으로 바꾼다.
    예를 들어 ``spring.datasource.url``은 ``SPRING_DATASOURCE_URL``이 된다."""
    out = re.sub(r"[.\-\[\]]+", "_", key.strip()).upper()
    return out.strip("_")


def _refs_cell(refs: list[Any], max_refs: int) -> str:
    seen: list[str] = []
    for r in refs:
        where = r.file + (f":{r.line}" if r.line else "")
        if where not in seen:
            seen.append(where)
    if not seen:
        return "-"
    cell = ", ".join(seen[:max_refs])
    if len(seen) > max_refs:
        cell += f" (+{len(seen) - max_refs} more)"
    return cell


def _written_cell(f: Any, written: set[str], sentinel: str) -> str:
    """"written as" 칸의 값을 만든다.
    재구성이 실제로 써 넣은 Boot 키와 값(``__SET_ME__`` 또는 context-param 기본값)이다."""
    if not f.boot_keys:
        return f.note or "-"
    ref = f.refs[0] if f.refs else None
    value = sentinel
    if ref is not None and ref.source == "context-param" and ref.default not in (None, ""):
        value = str(ref.default)
    keys = [k for k in f.boot_keys if k in written]
    if not keys:
        return "-"
    return ", ".join(f"{k}={value}" for k in keys)


def _set_via_cell(f: Any) -> str:
    """"set via" 칸을 만든다. 운영에서 값을 넣는 방법(환경 변수 또는 ``-D``)이다.
    ``System.getProperty``로만 읽는 값은 ``-D``만 안내한다. 환경 변수와 application.properties를 보지 않기 때문이다."""
    sources = {r.source for r in f.refs}
    if sources and sources <= {"system-env"}:
        return f"environment variable {f.key}"
    if sources and sources <= {"system-property"}:
        return f"-D{f.key}"
    parts: list[str] = []
    if f.boot_keys:
        parts.append(env_name(f.boot_keys[0]))
    if "system-property" in sources:
        parts.append(f"-D{f.key}")
    if "system-env" in sources:
        parts.append(f"environment variable {f.key}")
    return " / ".join(parts) or "-"


def needs_value(f: Any) -> bool:
    """담당 팀이 값을 넣어야 하는 외부 설정인지 확인한다. Boot 키가 있거나 어디에도 매핑되지 않으면 True다.
    규칙상 건너뛴 context-param과 Boot 기본값은 값이 필요 없다.

    An external finding the owners must supply: it has Boot keys, or nothing maps it (system env, unmapped JNDI,
    `@ConfigurationProperties` prefix, -D). Rule-skipped context-params and Boot defaults need nothing."""
    if f.boot_keys:
        return True
    return f.supplied_by != "context-param" and not str(f.note or "").startswith("Boot default")


def external_to_set(inv: Any) -> list[Any]:
    """값을 넣어야 하는 외부 설정 항목만 골라 반환한다(``needs_value`` 기준)."""
    if inv is None:
        return []
    return [f for f in (getattr(inv, "findings", None) or []) if f.status == "external" and needs_value(f)]


def property_rows(inv: Any, cfg: Any = None) -> list[str]:
    """인수인계 "Properties to set" 표의 줄을 만든다.
    ``external`` 항목마다 칸(``PROPERTY_COLUMNS``)을 `` | ``로 이은 한 줄이다.
    값이 필요한 항목이 하나도 없으면 빈 목록이다. 하나라도 있으면 값이 필요 없는 항목도 함께 넣는다.
    예외를 던지지 않는다."""
    if inv is None or not external_to_set(inv):
        return []
    x = getattr(cfg, "externalized", None) if cfg is not None else None
    sentinel = str(getattr(x, "sentinel", None) or DEFAULT_SENTINEL)
    try:
        max_refs = max(1, int(getattr(x, "max_refs_per_key", DEFAULT_MAX_REFS) or DEFAULT_MAX_REFS))
    except (TypeError, ValueError):
        max_refs = DEFAULT_MAX_REFS
    written = set(getattr(inv, "placeholders_written", None) or [])
    rows: list[str] = []
    for f in list(getattr(inv, "findings", None) or []):
        try:
            if f.status != "external":
                continue
            cells = [f.key, f.supplied_by or "unknown", _refs_cell(list(f.refs), max_refs),
                     _written_cell(f, written, sentinel), _set_via_cell(f)]
            rows.append(PROPERTY_SEP.join(_pcell(c) for c in cells))
        except Exception as e:  # pragma: no cover - 잘못된 항목 하나 때문에 나머지를 버리지 않는다
            log.debug("property row skipped: %s", e)
    return rows


def split_property_row(row: str) -> list[str]:
    """``property_rows``의 한 줄을 다시 칸으로 나눈다.
    항상 ``len(PROPERTY_COLUMNS)``칸을 반환한다. 칸이 넘치면 마지막 칸에 합치고, 모자라면 "-"로 채운다."""
    cells = [c.strip() for c in str(row).split(PROPERTY_SEP)]
    n = len(PROPERTY_COLUMNS)
    if len(cells) > n:
        cells = cells[: n - 1] + [" / ".join(cells[n - 1:])]
    return cells + ["-"] * (n - len(cells))


def property_guide(cfg: Any) -> list[str]:
    """외부 설정 규칙 파일(``knowledge/config/externalized.yml``)의 안내 줄을 반환한다.
    설정이 없거나 파일을 읽지 못하면 빈 목록을 반환한다."""
    if cfg is None:
        return []
    try:
        from sbmig.analyze import load_rules
        x = cfg.externalized
        return [str(g) for g in (load_rules(cfg.resolve_path(x.rules_file)).guide or []) if str(g).strip()]
    except Exception as e:
        log.debug("externalized guide unavailable: %s", e)
        return []


def _properties_item(inv: Any, cfg: Any) -> Optional[str]:
    """할 일 목록의 외부 설정 줄을 만든다.
    첫 기동 전에 값을 넣어야 하는 설정 개수와 플레이스홀더를 쓴 파일을 담는다."""
    ext = external_to_set(inv)
    if not ext:
        return None
    x = getattr(cfg, "externalized", None) if cfg is not None else None
    sentinel = str(getattr(x, "sentinel", None) or DEFAULT_SENTINEL)
    files = list(getattr(inv, "placeholder_files", None) or [])
    where = (f"placeholders ({sentinel}) in {', '.join(files)}" if files
             else "no placeholders were written (see the table)")
    return (f"{len(ext)} externalized setting(s) need values before the first start — {where}; "
            "see 'Properties to set'")


def _properties_followup_present(report: RunReport) -> bool:
    """재구성이 같은 외부 설정 안내를 수동 후속 작업에 이미 남겼는지 확인한다.
    남겼으면 중복 줄을 만들지 않는다."""
    for m in report.manual_followups or []:
        low = m.lower()
        if "externalized setting(s)" in low and ("placeholder" in low or "properties to set" in low):
            return True
    return False


def _view_exists(view: Any, path: str) -> bool:
    try:
        if hasattr(view, "exists"):
            return bool(view.exists(path))
        root = getattr(view, "root", None)
        return bool(root is not None and (Path(root) / path).exists())
    except (OSError, ValueError):
        return False


def resolve_current_path(ws: Any, path: str, cfg: Any = None, name: Optional[str] = None) -> str:
    """재구성 전에 기록된 파일 경로가 지금 어디 있는지 찾아 반환한다.
    파일이 그대로 있거나 ``ws``가 None이면 경로를 그대로 반환한다.
    없으면 ``src/`` 이후 부분을 레이아웃의 comp, boot, front 모듈 아래에서 찾는다.
    그래도 못 찾으면 원래 경로를 반환한다.

    Module directories come from `cfg.layout.module_path(which, name)` (company `{name}-back` / `-boot` /
    `-front`); a layout without `module_path`, or no config, falls back to `module/comp-jar` / `module/boot-jar`.
    Reads only `ws.exists`."""
    if not path or ws is None:
        return path
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    if _view_exists(ws, norm):
        return path
    parts = norm.split("/")
    if "src" not in parts:
        return path
    suffix = "/".join(parts[parts.index("src"):])
    lay = getattr(cfg, "layout", None) if cfg is not None else None
    bases: list[str] = []
    if lay is not None and callable(getattr(lay, "module_path", None)):
        for which in ("comp", "boot", "front"):
            mp = lay.module_path(which, name or "")
            if mp and "{name}" not in mp:
                bases.append(mp.strip("/"))
    if not bases:
        mdir = (getattr(lay, "module_dir", None) or "module").strip("/")
        for mod in (getattr(lay, "comp_module", None) or "comp-jar", getattr(lay, "boot_module", None) or "boot-jar"):
            bases.append(f"{mdir}/{mod.strip('/')}" if mdir else mod.strip("/"))
    for base in bases:
        cand = f"{base}/{suffix}"
        if cand != norm and _view_exists(ws, cand):
            return cand
    return path


def project_name(profile: Any) -> str:
    """회사 레이아웃의 ``{name}``, 즉 루트 pom의 artifactId를 반환한다. 없으면 프로필 이름을 반환한다."""
    rp = getattr(profile, "root_pom", None)
    return (getattr(rp, "artifact_id", None) or getattr(profile, "name", None) or "") if profile is not None else ""


def operator_checks_item(n: int) -> str:
    """메이저 업그레이드가 있었던 실행에서 할 일 목록 첫 줄(운영자 확인 필요)을 만든다.

    Counts `report.operator_checks`."""
    return f"operator checks: {n} major upgrade(s) — see \"Operator checks\" above"


def plural(n: int, word: str) -> str:
    """영어 데이터 줄에 쓸 단수 / 복수형(``1 file`` / ``3 files``)을 만든다."""
    return f"{n} {word}" + ("" if n == 1 else "s")


def jsp_jakarta_text(files: int, refs: int) -> str:
    """JSP 안의 javax EE 이름을 바꾼 결과를 영어 구절 하나로 만든다. 바꾼 파일 수와 참조 수가 들어간다.

    Inputs: `RestructureResult.jsp_jakarta_files` / `jsp_jakarta_refs`."""
    return f"{JSP_JAKARTA_LABEL}: {plural(files, 'file')}, {plural(refs, 'reference')}"


def jsp_jakarta_note(step_id: str, files: int, refs: int) -> str:
    """JSP 변환의 파일 수와 참조 수를 담은 단계 메모를 만든다. 단계는 재구성이나 ``transform.jsp_jakarta``다.
    오케스트레이터가 이 메모를 남기고 ``jsp_jakarta_counts``가 다시 읽는다."""
    return f"{step_id}: {jsp_jakarta_text(files, refs)}"


def is_jsp_jakarta_note(note: Any) -> bool:
    """``jsp_jakarta_note``가 만든 메모이거나 재구성이 남긴 같은 문장인지 확인한다.
    보고서 Notes에서 빼는 데 쓴다."""
    return bool(_JSP_JAKARTA_ANY_RE.match(str(note or "")))


def jsp_jakarta_counts(report: Any) -> tuple[int, int]:
    """이번 실행이 JSP에서 바꾼 (파일 수, 참조 수)를 모든 단계 메모에서 더해 반환한다.
    메모가 없으면 (0, 0)을 반환한다."""
    files = refs = 0
    for pr in list(getattr(report, "phases", None) or []):
        for n in list(getattr(pr, "notes", None) or []):
            m = _JSP_JAKARTA_NOTE_RE.match(str(n))
            if m:
                files += int(m.group("files"))
                refs += int(m.group("refs"))
    return files, refs


def jsp_javax_left(report: Any) -> int:
    """아직 javax EE 이름이 남았을 JSP 파일 수를 반환한다.
    분석이 센 ``SourceStats.jsp_javax_files``에서 바꾼 파일 수를 뺀 값이다. 0보다 작아지지 않는다."""
    src = getattr(getattr(report, "profile", None), "source", None)
    found = int(getattr(src, "jsp_javax_files", 0) or 0)
    return max(0, found - jsp_jakarta_counts(report)[0]) if found else 0


def _jsp_followup_present(report: RunReport) -> bool:
    """재구성이 JSP javax 후속 작업 줄을 이미 남겼는지 확인한다."""
    for m in report.manual_followups or []:
        low = m.lower()
        if "jsp" in low and "javax" in low:
            return True
    return False


def jsp_javax_item(report: RunReport, cfg: Any = None) -> str | None:
    """할 일 목록의 JSP 줄 ``JSP javax references remain (N files) …``을 만든다.
    javax가 남은 JSP가 있고 재구성이 같은 후속 작업 줄을 남기지 않았을 때만 만든다. 아니면 None을 반환한다.
    이름 변경이 꺼져 있었으면 그 사실을 뒤에 붙인다.
    최종 빌드가 통과하지 않았으면 빌드가 통과한다는 말은 빼고 쓴다."""
    left = jsp_javax_left(report)
    if not left or _jsp_followup_present(report):
        return None
    layout = getattr(cfg, "layout", None) if cfg is not None else None
    off = layout is not None and not getattr(layout, "jsp_jakarta_rename", True)
    fb = getattr(report, "final_build", None)
    text = JSP_JAVAX_LEFT if fb is not None and bool(getattr(fb, "ok", False)) else JSP_JAVAX_LEFT_NOT_BUILT
    return text.format(files=plural(left, "file")) + (JSP_RENAME_OFF if off else "")


def jar_stem_version(path: str) -> tuple[str, str]:
    """jar 파일 이름을 (stem, 버전)으로 나눈다. 분석기와 같은 규칙이다.
    ``acme-util-1.2.jar``는 (acme-util, 1.2), ``ojdbc6.jar``는 (ojdbc6, "")가 된다."""
    name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    m = _JAR_NAME_VERSION_RE.match(name)
    if m:
        return m.group("stem"), m.group("ver")
    return (name[:-4] if name.lower().endswith(".jar") else name), ""


def install_file_command(path: str, group_id: str, jar: Any = None) -> str:
    """좌표를 모르는 jar 하나를 저장소에 설치하는 ``mvn install:install-file …`` 명령을 만든다.
    나중에 합성 pom의 system 항목을 바꿔 넣을 수 있도록 같은 좌표를 쓴다. 버전을 모르면 0으로 둔다."""
    stem, ver = jar_stem_version(path)
    artifact = getattr(jar, "artifact_id", "") or stem
    version = getattr(jar, "version", "") or ver or "0"
    group = getattr(jar, "group_id", "") or group_id
    f = str(path).replace("\\", "/")
    # 공백이 있는 경로도 인자 하나로 넘어가도록 따옴표로 감싼다
    f = f'"{f}"' if " " in f else f
    return (f"mvn install:install-file -Dfile={f} -DgroupId={group} -DartifactId={artifact} -Dversion={version} "
            "-Dpackaging=jar")


def pom_synthesis_counts(ps: Any) -> dict[str, int]:
    """합성 pom의 jar 수와 좌표 출처별 개수를 반환한다.
    manifest에서 얻은 값은 좌표가 아니다. 그래서 접수 로그처럼 unknown으로 센다."""
    c = dict(getattr(ps, "counts", None) or {})
    return {"jars": len(getattr(ps, "jars", None) or []), "pom_properties": c.get("pom_properties", 0),
            "name_table": c.get("name_table", 0), "sha1_lookup": c.get("sha1_lookup", 0),
            "unknown": c.get("unknown", 0) + c.get("manifest", 0)}


def unknown_jars_system(ps: Any, cfg: Any = None) -> bool:
    """좌표를 모르는 jar를 ``system`` scope로 선언했는지 확인한다.
    jar 표의 scope, ``intake.unknown_jar``, 기본값(system) 순으로 확인한다."""
    by_path = {j.path: j for j in getattr(ps, "jars", None) or []}
    scopes = {getattr(by_path.get(p), "scope", "") for p in getattr(ps, "unknown_jars", None) or []}
    if "system" in scopes:
        return True
    intake = getattr(cfg, "intake", None) if cfg is not None else None
    return str(getattr(intake, "unknown_jar", "system") or "system") == "system"


def unknown_jar_commands(ps: Any, cfg: Any = None) -> list[str]:
    """``PomSynthesis.unknown_jars``의 jar마다 install-file 명령을 한 줄씩 만든다.
    groupId는 ``intake.synth_group_id``, 합성 pom의 groupId 순으로 정한다."""
    by_path = {j.path: j for j in getattr(ps, "jars", None) or []}
    intake = getattr(cfg, "intake", None) if cfg is not None else None
    group = getattr(intake, "synth_group_id", "") or getattr(ps, "group_id", "") or "com.company.migrated"
    return [install_file_command(p, group, by_path.get(p)) for p in getattr(ps, "unknown_jars", None) or []]


def _from_here(h: HandoverSummary, report: RunReport, cfg: Any, view: Any = None) -> list[str]:
    """인수인계의 할 일 목록을 만든다. 목록 순서가 중요도 순서다.
    순서: HTTP 차단 / ADF / 빈 reactor -> 사내 부모 체인에 Boot BOM 없음 -> 메이저 업그레이드 확인 ->
    교체되지 않은 권고 -> 교체된 권고 확인 -> 고정 버전 -> 차단 / 미해결 오류 -> 구조 검토 -> 외부 설정 ->
    남은 JSP javax -> 나머지 수동 후속 작업
    ``advisories.max_from_here``가 양수이면 그 줄 수로 자른다. 마지막 줄은 "… and N more"가 된다."""
    adv_cfg = getattr(cfg, "advisories", None) if cfg is not None else None
    out: list[str] = []
    findings = list(report.advisories or [])
    # HTTP 차단 미러로 중단된 경우를 가장 먼저 알린다. 운영자가 Maven settings를 고쳐야 한다
    if HTTP_BLOCKED_FLAG in (report.flags or []):
        first = next((m for m in report.manual_followups if m.startswith(HTTP_BLOCKED_PREFIX)), None)
        if first:
            _dedupe_add(out, first)
    # 변환하지 않는 ADF 프로젝트는 그 사실과 규모를 앞에 둔다
    if ADF_FLAG in (report.flags or []):
        first = next((m for m in report.manual_followups if m.startswith(ADF_PREFIX)), None)
        if first:
            _dedupe_add(out, first)
    # 소스가 이관되지 않았다는 사실을 다른 무엇보다 먼저 알린다
    if HOLLOW_FLAG in (report.flags or []):
        first = next((m for m in report.manual_followups if m.startswith(HOLLOW_PREFIX)), None)
        if first:
            _dedupe_add(out, first)
    # 사내 부모 체인에 Boot BOM이 없으면 앞에 둔다. 모든 프로젝트에 걸리는 구조 문제다
    if PARENT_NO_BOOT_BOM_FLAG in (report.flags or []):
        first = next((m for m in report.manual_followups if m.startswith(PARENT_NO_BOOT_BOM_PREFIX)), None)
        if first:
            _dedupe_add(out, first)
    # 메이저 업그레이드는 막지 않는 대신 사람이 꼭 확인하도록 앞에 둔다
    checks = list(getattr(report, "operator_checks", None) or [])
    if checks:
        _dedupe_add(out, operator_checks_item(len(checks)))
    # 교체하지 않은 권고
    for f in findings:
        if f.outcome == "replaced":
            continue
        txt = f"{f.title}: {f.followup}".rstrip(": ") if f.followup else f.title
        if f.replacement:
            txt += f" (replacement: {f.replacement}{' ' + f.replacement_version if f.replacement_version else ''})"
        _dedupe_add(out, txt)
    # 교체는 레시피나 수정 스크립트가 했다. 사람이 동작을 확인해야 한다
    for f in findings:
        if f.outcome != "replaced":
            continue
        txt = f"verify {f.library} → {f.replacement or '?'} (replaced during the run)"
        if f.followup:
            txt += f": {f.followup}"
        _dedupe_add(out, txt)
    # 권고 안내에 따라 LLM 수정 스크립트가 고친 코드는 검토를 요청한다
    for f in findings:
        if not f.llm_fixed_files:
            continue
        fixed = list(dict.fromkeys(resolve_current_path(view, p, cfg, project_name(getattr(report, "profile", None)))
                                   for p in f.llm_fixed_files))
        files = ", ".join(fixed[:LLM_FILES_MAX])
        if len(fixed) > LLM_FILES_MAX:
            files += f" (+{len(fixed) - LLM_FILES_MAX} more)"
        txt = f"code rewritten by the LLM for {f.library}: {files}"
        if f.llm_fix_notes:
            txt += f" — {'; '.join(f.llm_fix_notes[:2])}"
        _dedupe_add(out, txt + " — review these changes (changes.patch, Fixes by case)")
    # as-is 버전을 그대로 둔 의존성
    if adv_cfg is None or getattr(adv_cfg, "report_carried_over", True):
        co = h.carried_over
        if len(co) > CARRIED_LIST_MAX:
            _dedupe_add(out, f"{len(co)} dependencies are pinned at their as-is versions because the new parent / "
                             f"Boot BOM no longer manages them (⚠ in Versions replaced) — confirm or replace each; "
                             f"the first {CARRIED_LIST_MAX}:")
        for c in co[:CARRIED_LIST_MAX]:
            _dedupe_add(out, f"{c.key} pinned at {c.after} — the new parent/Boot BOM no longer manages it: "
                             "confirm the version or replace")
    # 차단과 미해결 오류는 그 섹션을 가리키는 줄만 둔다
    if report.blocked:
        _dedupe_add(out, f"{len(report.blocked)} cluster(s) blocked — see Blocked")
    entries = list(getattr(report, "error_entries", None) or [])
    left = sum(1 for e in entries if getattr(e, "status", "") in ("open", "unresolved")) if entries \
        else len(report.unresolved)
    # 미해결 개수는 오류 목록의 open, unresolved를 센다. 오류 목록이 없을 때만 unresolved를 쓴다
    if left:
        _dedupe_add(out, f"{left} unresolved error(s) — see Error list")
    jars = list(getattr(report, "javax_api_jars", None) or [])
    if jars and not any(_JAVAX_FOLLOWUP_RE.match(m) for m in report.manual_followups):
        # 오케스트레이터의 후속 작업 줄이 없을 때만 그 섹션을 가리키는 줄을 대신 넣는다
        _dedupe_add(out, f"{len(jars)} javax API jar(s) still on the classpath — see that section")
    # 구조 검토가 바꾼 것, 남은 critical, warning
    for line in review_followups(report):
        _dedupe_add(out, line)
    # 외부 설정. 재구성이 같은 후속 작업 줄을 남겼으면 그 줄을 쓴다
    try:
        item = None if _properties_followup_present(report) else _properties_item(_inventory(report), cfg)
        if item:
            _dedupe_add(out, item)
    except Exception as e:  # pragma: no cover
        log.debug("properties item skipped: %s", e)
    # JSP에 남은 javax는 Tomcat 10 이상에서 실행할 때 실패한다. OpenRewrite는 JSP를 읽지 않는다.
    # 재구성이 같은 후속 작업 줄을 남겼으면 그 줄을 쓴다
    try:
        jsp = jsp_javax_item(report, cfg)
        if jsp:
            _dedupe_add(out, jsp)
    except Exception as e:  # noqa: BLE001  # pragma: no cover - 한 줄 때문에 인수인계 전체가 실패하지 않게 한다
        log.debug("JSP javax item skipped: %s", e)
    if "test-compile-skipped" in report.flags:
        _dedupe_add(out, "test sources do not compile yet (main code builds with -Dmaven.test.skip=true) — "
                         "fix the tests before relying on CI")
    # 권고의 후속 작업 문장은 앞에서 이미 나왔다. 수동 후속 작업에 같은 문장이 있으면 건너뛴다
    adv_fu: set[str] = set()
    if findings:
        try:
            from sbmig.advisory import followups_for
            adv_fu = {" ".join(x.split()).lower() for x in (followups_for(findings) or [])}
        except Exception as e:  # pragma: no cover - 권고 패키지를 쓸 수 없으면 이 비교 없이 계속한다
            log.debug("followups_for unavailable: %s", e)
    for m in report.manual_followups:
        if " ".join(m.split()).lower() in adv_fu:
            continue
        _dedupe_add(out, m)
    cap = int(getattr(adv_cfg, "max_from_here", 0) or 0) if adv_cfg is not None else 0
    if cap > 0 and len(out) > cap:
        rest = len(out) - (cap - 1)
        out = out[: cap - 1] + [f"… and {rest} more — see Manual follow-ups / Blocked / Error list"]
    return out


def _short(text: object, limit: int) -> str:
    """공백을 정리하고 ``limit``자로 자른다. 넘치면 끝에 …를 붙인다."""
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def review_followups(report: RunReport) -> list[str]:
    """구조 검토 결과를 할 일 목록 줄로 압축한다.
    검토가 바꾼 파일, 고치지 못한 critical, warning을 담는다. 최대 ``REVIEW_FOLLOWUPS_MAX``줄이다.
    이슈가 없으면 빈 목록을 반환한다."""
    rv = getattr(report, "structure_review", None)
    # 입력 자료에 없는 버전이나 속성을 말한 이슈(`unsupported`)는 인수인계에 넣지 않는다. 보고서에만 남는다
    issues = [i for i in (rv.issues if rv is not None else []) if not getattr(i, "unsupported", False)]
    if rv is None or not issues:
        return []
    res = rv.script_result
    fixed = res is not None and res.status == "ok" and not rv.reverted and bool(res.changed_files)

    def where(i: Any) -> str:
        hint = f" → {_short(i.fix_hint, 160)}" if i.fix_hint else ""
        return f"{i.file or '(project)'}: {_short(i.problem, 220)}{hint}"

    out: list[str] = []
    critical = [i for i in issues if i.severity == "critical"]
    if fixed:
        files = ", ".join(res.changed_files[:REVIEW_FILES_MAX])
        if len(res.changed_files) > REVIEW_FILES_MAX:
            files += f" (+{len(res.changed_files) - REVIEW_FILES_MAX} more)"
        # "(sbmig check)" 이슈는 sbmig가 직접 추가한 것이라 검토 수정이 다루지 않는다.
        # 고친 것으로 세지 않고 아래에 남긴다
        handled = [i for i in critical if not i.problem.startswith(REVIEW_SBMIG_CHECK)]
        out.append(f"structure review fix (edits / script) changed {len(res.changed_files)} file(s) for "
                   f"{len(handled)} critical issue(s): {files} (commit {(rv.commit or '-')[:8]}) — check these pom "
                   "changes")
        critical = [i for i in critical if i.problem.startswith(REVIEW_SBMIG_CHECK)]
    out += [f"structure review: critical issue not fixed — {where(i)}" for i in critical]
    out += [f"structure review warning — {where(i)}" for i in issues if i.severity != "critical"]
    if len(out) > REVIEW_FOLLOWUPS_MAX:
        rest = len(out) - (REVIEW_FOLLOWUPS_MAX - 1)
        out = out[: REVIEW_FOLLOWUPS_MAX - 1] + [f"… and {rest} more structure review issue(s) — see Structure review"]
    return out


def merge_conflict_lines(report: RunReport) -> list[str]:
    """다중 모듈 ``merge`` 레이아웃의 충돌(``report.merge_conflicts``)을 줄로 만든다.
    내용이 다른 경로는 한 줄씩, 내용이 같은 중복은 한 줄로 모은다."""
    out: list[str] = []
    identical: list[str] = []
    for c in list(getattr(report, "merge_conflicts", None) or []):
        if c.identical:
            identical.append(c.path)
            continue
        out.append(f"{c.path}: kept the copy from {c.kept_from}, dropped {', '.join(c.dropped_from) or '-'} "
                   f"(saved: {', '.join(c.saved_copies) or '-'})")
    if identical:
        names = ", ".join(identical[:IDENTICAL_LIST_MAX])
        if len(identical) > IDENTICAL_LIST_MAX:
            names += ", …"
        out.append(f"{len(identical)} identical duplicate(s) dropped (same content in every module): {names}")
    return out


def _build(profile: ProjectProfile, view: Any, report: RunReport, effective: Optional[dict[str, Any]],
           cfg: Any) -> HandoverSummary:
    """인수인계 데이터를 실제로 만든다.
    최종 pom을 다시 파싱해 parent, BOM, 의존성, 플러그인 순으로 전후를 비교한다.
    버전 변경, 추가와 삭제, 할 일 목록을 채운다."""
    from sbmig.analyze.pom_parser import parse_poms

    effective = effective or {}
    h = HandoverSummary()
    before_poms = list(profile.poms)
    final, warnings = parse_poms(view)
    h.notes += [f"final poms: {w}" for w in warnings[:5]]
    own = _own_keys(before_poms) | _own_keys(final)

    # ---- parent, Java, Boot
    pb, pa = _parents(before_poms), _parents(final)
    h.parent_before, h.parent_after = _fmt_parents(pb), _fmt_parents(pa)
    from .result_line import run_scope
    reduced = run_scope(report) is not None
    for k, v in _versions(profile, final, cfg, reduced=reduced).items():
        setattr(h, k, v)
    # 프레임워크 프로젝트는 Boot를 올리지 않는다. 수정 시도가 없었다면 pom을 바꾼 것은 레시피뿐이다
    tried = any(pr.attempts for pr in report.phases or [])
    other_reason = REASON_RECIPE if reduced and not tried else REASON_OTHER
    boot_reason = other_reason if reduced else REASON_BOOT

    notes = _notes(report)
    optional, mandatory = _restructure_keys(notes)
    trig_added, trig_removed = _trigger_keys(notes)
    boot_pins = _boot_pins(notes)
    adv_arts, adv_repl = _advisory_maps(list(report.advisories or []))
    changes: list[VersionChange] = []
    main = _main_pom(final)
    main_path = main.path if main else "pom.xml"

    # 같은 parent면 버전 변경 줄, 다른 parent면 짝마다 교체 줄 하나를 만든다
    same = [k for k in pb if k in pa]
    for k in same:
        if pb[k] != pa[k]:
            changes.append(VersionChange(kind="parent", key=k, before=pb[k], after=pa[k], reason=REASON_PARENT,
                                         pom=main_path))
    only_b = [k for k in pb if k not in pa]
    only_a = [k for k in pa if k not in pb]
    for kb_, ka in zip(only_b, only_a):
        changes.append(VersionChange(kind="parent", key=ka, before=_coord(kb_, pb[kb_]), after=pa[ka],
                                     reason=REASON_PARENT, pom=main_path))
    for kb_ in only_b[len(only_a):]:
        h.removed.append(f"parent {_coord(kb_, pb[kb_])}")
    for ka in only_a[len(only_b):]:
        h.added.append(f"parent {_coord(ka, pa[ka])}")

    # ---- BOM import
    bb, ba = _boms(before_poms), _boms(final)
    for k in sorted(set(bb) | set(ba)):
        if k in bb and k in ba:
            if bb[k][0] != ba[k][0]:
                changes.append(VersionChange(kind="bom", key=k, before=bb[k][0], after=ba[k][0],
                                             reason=boot_reason if _is_spring(k) else other_reason, pom=ba[k][1]))
        elif k in bb:
            h.removed.append(f"bom {_coord(k, bb[k][0])}")
        else:
            h.added.append(f"bom {_coord(k, ba[k][0])}")

    # ---- 의존성
    needs_eff: list[str] = []
    db = _deps_before(before_poms, effective, own, needs_eff)
    da, where = _deps_after(final, own)
    if needs_eff and not effective:
        h.notes.append("as-is effective dependencyManagement unavailable — versions the as-is parent managed are "
                       "shown as '-'")

    def reason_for(key: str) -> str:
        if key in adv_arts:
            return f"advisory:{adv_arts[key]}"
        if key in adv_repl:
            return f"advisory:{adv_repl[key]}"
        # 트리거로 들어온 선택 아티팩트와 그때 지운 트리거 의존성은 단순한 버전 올림보다 먼저 확인한다
        if key in trig_added:
            return optional_trigger_reason(trig_added[key])
        if key in trig_removed:
            return optional_trigger_reason(trig_removed[key])
        if key in optional:
            return REASON_OPTIONAL
        if key in mandatory:
            return REASON_MANDATORY
        if _is_spring(key):
            return boot_reason
        return other_reason

    for k in sorted(set(db) | set(da)):
        if k in db and k in da:
            (bv, bs), (av, as_) = db[k], da[k]
            # 전후 모두 parent나 BOM이 관리하면 의존성 줄을 만들지 않는다. parent / BOM 줄이 이미 보여 준다
            if bs in ("managed", "absent") and as_ == "managed":
                continue
            if bv == av and bs == as_:
                continue
            # 이전 parent가 관리하던 버전이다. 새 parent나 Boot BOM이 관리하지 않아서 pom에 그대로 뒀다.
            # 담당 팀이 확인해야 한다
            carried = (bs == "managed" and as_ == "declared" and av is not None
                       and av == _eff_lookup(effective, k))
            # 대상 Boot BOM의 버전으로 고정한 의존성. as-is 버전과 다를 때만 ⚠를 붙여 담당 팀에 알린다
            boot_pin = as_ == "declared" and av is not None and boot_pins.get(k) == av
            if boot_pin:
                carried = False
            # 버전은 같고 선언한 위치만 바뀐 경우다
            if bv == av and not carried and not boot_pin:
                continue
            changes.append(VersionChange(kind="dependency", key=k, before=bv, after=av, before_source=bs,
                                         after_source=as_, carried_over=carried, boot_bom_pin=boot_pin and bv != av,
                                         reason=(REASON_BOOT_BOM_PIN if boot_pin else
                                                 REASON_CARRIED if carried else reason_for(k)),
                                         pom=where.get(k, main_path)))
        elif k in db:
            r = reason_for(k)
            tagged = r.startswith("advisory:") or k in trig_removed
            h.removed.append(_coord(k, db[k][0]) + (f" ({r})" if tagged else ""))
        else:
            r = reason_for(k)
            tag = r if r.startswith("advisory:") or r in (REASON_MANDATORY, REASON_OPTIONAL) or k in trig_added else ""
            h.added.append(_coord(k, da[k][0]) + (f" ({tag})" if tag else ""))

    # ---- 플러그인. 버전 변경만 본다. 추가와 삭제는 재구성 때문에 생긴다
    plb, pla = _plugins(before_poms), _plugins(final)
    for k in sorted(set(plb) & set(pla)):
        if plb[k][0] != pla[k][0]:
            changes.append(VersionChange(kind="plugin", key=k, before=plb[k][0], after=pla[k][0],
                                         reason=boot_reason if _is_spring(k) else other_reason, pom=pla[k][1]))

    order = {"parent": 0, "bom": 1, "dependency": 2, "plugin": 3, "property": 4}
    changes.sort(key=lambda c: (order.get(c.kind, 9), c.key))
    h.changes = changes
    h.carried_over = [c for c in changes if c.carried_over]
    h.merge_conflicts = merge_conflict_lines(report)
    _fill_properties(h, report, cfg)
    h.from_here = _from_here(h, report, cfg, view)
    return h


def _fill_properties(h: HandoverSummary, report: RunReport, cfg: Any) -> None:
    """외부 설정 표와 안내 줄을 채운다. 표가 비면 안내도 비운다. 실패해도 인수인계는 계속 만든다."""
    try:
        h.properties_to_set = property_rows(_inventory(report), cfg)
        h.properties_guide = property_guide(cfg) if h.properties_to_set else []
    except Exception as e:  # pragma: no cover
        log.debug("properties rows skipped: %s", e)


def build_handover(profile: Optional[ProjectProfile], view: Any, report: RunReport,
                   effective: Optional[dict[str, Any]] = None, cfg: Any = None) -> HandoverSummary:
    """as-is pom, 최종 pom, 권고를 비교해서 인수인계 섹션(HandoverSummary)을 만든다.
    예외를 던지지 않는다. 프로필이 없으면 "handover unavailable" 메모만 담아 반환한다.
    비교가 실패해도 merge 충돌, 외부 설정, 할 일 목록은 따로 채운다. 담당 팀이 읽을 내용은 남긴다."""
    if profile is None:
        return HandoverSummary(notes=["handover unavailable: no project profile (the run stopped before analyze)"])
    try:
        return _build(profile, view, report, effective, cfg)
    except Exception as e:
        log.warning("handover failed: %s", e)
        h = HandoverSummary(notes=[f"handover unavailable: {type(e).__name__}: {e}"])
        try:
            h.merge_conflicts = merge_conflict_lines(report)
            _fill_properties(h, report, cfg)
            h.from_here = _from_here(h, report, cfg, view)
        except Exception:  # pragma: no cover
            pass
        return h
