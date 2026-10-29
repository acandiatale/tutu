"""인수인계 섹션 데이터 — "여기까지 바꿨고, 이제부터는 담당팀 몫" (report.md 의 맨 앞 섹션).
흐름상 위치: 보고서 단계에서 writer.py 가 ``build_handover`` 를 부르고 markdown.py 가 그 결과를 맨 앞에
그린다. as-is pom(``profile.poms`` + 접수 때 잡은 as-is effective dependencyManagement)과 최종 트리의 pom 을
비교해 parent / Java / Boot 전후, 버전이 바뀐 의존성(⚠ = 새 parent / Boot BOM 이 더는 관리하지 않아 as-is
버전으로 고정된 것), 추가 / 삭제된 아티팩트, "이제부터 할 일" 목록(``_from_here``: 운영자 확인 필요 → 교체 안
된 권고 → 교체됐으니 확인 → 고정 버전 → 차단 / 미해결 안내 → 구조 검토 → 외부 설정값 → 남은 JSP javax(v2.1h)
→ 수동 후속 작업)을
만든다. v2.1j: Ant 프로젝트의 합성 pom 에서 좌표 미상 jar 의 ``mvn install:install-file`` 명령(``unknown_jar_commands``). 담당팀은 보고서에서 이 섹션만 읽는다 — 그래서 맨 앞이고 짧게 압축한다. 절대 예외를 내지
않는다(실패해도 ``notes`` 에 "handover unavailable: …" 한 줄).

Handover: "here is what we replaced, from here on it is yours" — the first section of report.md.

`build_handover(profile, view, report, effective=None, cfg=None)` compares the as-is poms (`profile.poms`, plus
the as-is effective dependencyManagement captured at intake for versions the old parent managed) with the final
poms of the migrated tree (`view` = Workspace / PathView) and compresses the result for the owning team:
parent / java / spring boot before → after, one row per dependency whose version changed (⚠ carried-over pins:
the new parent / Boot BOM no longer manages them), added / removed artifacts, and an ordered "from here on, your
team" list (advisories not replaced, replaced-but-verify, carried-over pins, blocked / unresolved pointers,
manual follow-ups) and the multi-module merge conflicts (`report.merge_conflicts`). Round 8: one
`properties_to_set` row per `external` externalized-config finding (`report.properties`, see `property_rows`) plus the
guide lines of `knowledge/config/externalized.yml`. Round 9: file paths recorded by fix attempts before the restructure
moved the sources (`llm_fixed_files`) are mapped to where the file lives now (`resolve_current_path`). v2: the
structure review (`report.structure_review`) adds what its script changed, unfixed critical issues and warnings
(`review_followups`). Pure and defensive: never raises (→ `HandoverSummary(notes=["handover unavailable: …"])`).


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
REASON_MANDATORY = "mandatory"
REASON_BOOT = "spring boot 4 upgrade"
REASON_OTHER = "recipe / fix script"
CARRIED_LIST_MAX = 5
LLM_FILES_MAX = 5                # files listed per "code rewritten by the LLM for <library>" line
# the orchestrator's X1b follow-up (report.javax_api_inventory, 2026-10-22)
_JAVAX_FOLLOWUP_RE = re.compile(r"^\d+ javax API jar\(s\) still on the classpath after the migration")
IDENTICAL_LIST_MAX = 5           # paths named in the "N identical duplicate(s) dropped" line
REVIEW_FOLLOWUPS_MAX = 8         # v2 structure-review lines in "from here on" (then one "… and N more" line)
REVIEW_FILES_MAX = 5
REVIEW_SBMIG_CHECK = "(sbmig check) "   # issues sbmig added itself (sbmig.review): never fixed by the review script
PROPERTY_SEP = " | "             # cell separator of a properties_to_set row (cells never contain "|")
PROPERTY_COLUMNS = ("key", "before migration", "referenced at", "written as", "set via")
DEFAULT_SENTINEL = "__SET_ME__"
DEFAULT_MAX_REFS = 3

# v2.1h (A5) JSP javax → jakarta: RunReport 에 따로 필드가 없으므로 오케스트레이터가 재구성 단계 메모(PhaseResult.notes)에
# 이 형식으로 남기고 보고서가 같은 형식을 읽는다 — 쓰는 쪽(jsp_jakarta_note)과 읽는 쪽(jsp_jakarta_counts)이 여기 한 곳
JSP_JAKARTA_LABEL = "JSP javax → jakarta"
_JSP_JAKARTA_NOTE_RE = re.compile(r"^(?P<step>[\w.\-]+): JSP javax → jakarta: (?P<files>\d+) files?, "
                                  r"(?P<refs>\d+) references?$")
# 재구성 자신의 노트(단계 접두어 없음)도 같은 문장이다: 수는 접두어 있는 오케스트레이터 메모로만 세고(위 정규식),
# 보고서 Notes 에서는 둘 다 뺀다 — "What we changed" 에 한 번만 보이게
_JSP_JAKARTA_ANY_RE = re.compile(r"^(?:[\w.\-]+: )?JSP javax → jakarta: \d+ files?, \d+ references?$")
JSP_JAVAX_LEFT = ("JSP javax references remain ({files}) — rename the javax EE names in page imports / scriptlets / "
                  "useBean to jakarta.* (the build passes, Tomcat 10+ fails at runtime)")
JSP_RENAME_OFF = "; `layout.jsp_jakarta_rename` is off"
# v2.1j Ant → pom.xml 합성: 좌표를 못 찾은 jar 마다 install-file 명령 한 줄. 파일 이름을 이름-버전으로 가르는 규칙은
# sbmig/analyze/webapp_inventory.py 의 _VERSION_RE 와 같다(분석기가 JarCoordinate.artifact_id / version 을 채웠으면 그것이 이긴다)
_JAR_NAME_VERSION_RE = re.compile(r"^(?P<stem>.+?)[-_](?P<ver>\d[\w.\-]*?)\.jar$", re.IGNORECASE)

_OPTIONAL_RE = re.compile(r"optional bumped: (.*)$")
_MANDATORY_RE = re.compile(r"mandatory added: ([^;]*)")

# key -> (version, source)
_Dep = tuple[Optional[str], str]


def _clean(v: Optional[str]) -> Optional[str]:
    # ${...} 속성 참조는 실제 버전이 아니다 — 비교에서 "모름" 으로 취급해야 가짜 버전 변경 줄이 안 생긴다
    if not v or "${" in v:
        return None
    return v.strip() or None


def _is_bom(g: GAV) -> bool:
    return (g.type or "") == "pom" and (g.scope or "") == "import"


def _own_keys(poms: list[PomInfo]) -> set[str]:
    return {f"{p.group_id}:{p.artifact_id}" for p in poms if p.group_id}


def _roots(poms: list[PomInfo]) -> list[PomInfo]:
    """parent 가 레포 밖에 있는 pom 들(= 사내 parent / BOM 을 실제로 물고 있는 pom), 얕은 경로 먼저.

    Poms whose parent lives outside the repo (or that have none): the ones carrying the parent / BOM."""
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
    """as-is 의존성 → (버전, 출처). 선언된 버전(declared)이 우선이고, 버전 없는 것은 as-is effective 에서 찾아
    managed, 거기도 없으면 absent(그 키는 ``needs_eff`` 에 모아 "as-is 관리 버전 없음" 안내를 남긴다).
    프로젝트 자신의 모듈(``own``)은 비교 대상이 아니다."""
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
    """최종 의존성 → (버전, declared | managed)와 그 의존성을 선언한 pom 경로. 최종 트리의 managed 버전은 새
    parent / Boot BOM 이 정하므로 여기서는 버전을 알 수 없다(None)."""
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
    """모든 단계·시도의 notes 를 한 목록으로(재구성 단계가 남긴 "optional bumped: …" 등을 찾기 위해)."""
    out: list[str] = []
    for pr in report.phases:
        out += list(pr.notes)
        for a in pr.attempts:
            out += list(a.notes)
    return out


def _restructure_keys(notes: list[str]) -> tuple[set[str], set[str]]:
    """재구성 단계 notes 에서 (버전을 올린 선택 아티팩트 키, 추가한 필수 아티팩트 키)를 뽑는다 —
    버전 변경 줄의 이유(optional bump / mandatory) 칸에 쓴다.

    (optional bumped keys, mandatory added keys) from the restructure's phase / attempt notes."""
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


def _replacement_key(f: AdvisoryFinding) -> Optional[str]:
    r = (f.replacement or "").strip()
    if r and " " not in r and r.count(":") in (1, 2):
        return ":".join(r.split(":")[:2])
    return None


def _advisory_maps(findings: list[AdvisoryFinding]) -> tuple[dict[str, str], dict[str, str]]:
    """권고에서 두 표를 만든다: as-is 아티팩트 키 → 권고 id, 대체 아티팩트 키 → 권고 id
    (버전 변경 줄의 이유 칸 ``advisory:<id>``).

    (as-is artifact key → advisory id, replacement key → advisory id)."""
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


def _versions(profile: ProjectProfile, final: list[PomInfo], cfg: Any) -> dict[str, Optional[str]]:
    """Java / Boot 버전 전후. 이후 값은 메인 pom → 다른 pom → 설정의 ``target`` 순서로 찾는다
    (사내 parent 가 버전을 관리하면 최종 pom 에는 값이 없다)."""
    main = _main_pom(final)
    target = getattr(cfg, "target", None) if cfg is not None else None
    java_after = main.java_version if main else None
    if not java_after:
        java_after = next((p.java_version for p in final if p.java_version), None)
    if not java_after and target is not None:
        java_after = target.java_version          # managed by the (in-house) parent
    boot_after = main.spring_boot_version if main else None
    if not boot_after:
        boot_after = next((p.spring_boot_version for p in final if p.spring_boot_version), None)
    if not boot_after and target is not None:
        boot_after = target.spring_boot_version
    return {"java_before": profile.java_version, "java_after": java_after,
            "spring_boot_before": profile.spring_boot_version, "spring_boot_after": boot_after}


def _dedupe_add(out: list[str], item: str) -> None:
    """"이제부터 할 일" 에 한 줄 추가하되, 같은 문장(대소문자·공백 무시, 한쪽이 다른 쪽에 꼬리만 붙은 것
    포함)이면 넣지 않는다. 권고·수동 후속 작업·재구성 안내가 같은 말을 여러 번 하는 일이 잦아서다."""
    s = " ".join(item.split())
    if not s:
        return
    low = s.lower()
    for x in out:
        xl = x.lower()
        short, long_ = (low, xl) if len(low) <= len(xl) else (xl, low)
        if long_ == short or (long_.startswith(short) and not long_[len(short)].isalnum()):
            return                                   # same sentence, possibly with a suffix ("… (replacement: …)")
    out.append(s)


# ---------------------------------------------------------------------------- externalized config (round 8)
def _inventory(report: RunReport) -> Any:
    """외부 설정 조사 결과(round 8): 보고서의 ``properties``, 없으면 프로필의 것."""
    inv = getattr(report, "properties", None)
    if inv is None and getattr(report, "profile", None) is not None:
        inv = getattr(report.profile, "properties", None)
    return inv


def _pcell(text: object) -> str:
    s = " ".join(str(text if text is not None else "").split()).replace("|", "/")
    return s or "-"


def env_name(key: str) -> str:
    """Boot relaxed binding 의 환경변수 이름으로 바꾼다(``spring.datasource.url`` →
    ``SPRING_DATASOURCE_URL``).

    Relaxed-binding environment variable name: `spring.datasource.url` → `SPRING_DATASOURCE_URL`."""
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
    """"written as" 칸: 재구성이 실제로 써 넣은 Boot 키와 값(``__SET_ME__``
    자리표시, context-param 기본값이 있으면 그 값)."""
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
    """"set via" 칸: 운영에서 값을 넣는 방법(환경변수 / ``-D`` 시스템 속성). 코드가 읽는 방식에 맞춰야 한다 —
    ``System.getProperty`` 는 환경변수도 application.properties 도 보지 않으므로 ``-D`` 만 안내한다."""
    sources = {r.source for r in f.refs}
    if sources and sources <= {"system-env"}:
        return f"environment variable {f.key}"
    if sources and sources <= {"system-property"}:
        return f"-D{f.key}"                        # System.getProperty sees neither env vars nor application.properties
    parts: list[str] = []
    if f.boot_keys:
        parts.append(env_name(f.boot_keys[0]))
    if "system-property" in sources:
        parts.append(f"-D{f.key}")
    if "system-env" in sources:
        parts.append(f"environment variable {f.key}")
    return " / ".join(parts) or "-"


def needs_value(f: Any) -> bool:
    """담당팀이 값을 넣어야 하는 외부 설정인가: Boot 키가 있거나, 아무것도 매핑해 주지 않는 것(시스템
    환경변수, 매핑 안 된 JNDI, ``@ConfigurationProperties`` 접두어,
    -D). 규칙상 건너뛴 context-param 과 Boot 기본값은 필요 없다.

    An external finding the owners must supply: it has Boot keys, or nothing maps it (system env, unmapped JNDI,
    `@ConfigurationProperties` prefix, -D). Rule-skipped context-params and Boot defaults need nothing."""
    if f.boot_keys:
        return True
    return f.supplied_by != "context-param" and not str(f.note or "").startswith("Boot default")


def external_to_set(inv: Any) -> list[Any]:
    """값이 필요한 외부 설정 항목들(``needs_value`` 기준).

    External findings that need a value (see `needs_value`)."""
    if inv is None:
        return []
    return [f for f in (getattr(inv, "findings", None) or []) if f.status == "external" and needs_value(f)]


def property_rows(inv: Any, cfg: Any = None) -> list[str]:
    """인수인계 "Properties to set" 표: ``external`` 항목마다 `` | `` 로 이은 한 줄(칸은
    ``PROPERTY_COLUMNS``). 값이 필요 없는 항목도 함께 적지만, 값이 필요한 항목이 하나도 없으면 표 자체가 비어
    있다. 예외를 내지 않는다.

    One `PROPERTY_SEP`-joined row per `external` finding (columns: `PROPERTY_COLUMNS`); findings that need no
    value (rule-skipped context-param, Boot default) are listed too, but only when at least one finding needs a
    value — otherwise there is nothing to set and the list is empty. Never raises."""
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
        except Exception as e:  # pragma: no cover - a malformed finding must not drop the others
            log.debug("property row skipped: %s", e)
    return rows


def split_property_row(row: str) -> list[str]:
    """``property_rows`` 한 줄을 다시 칸으로 나눈다.
    항상 ``len(PROPERTY_COLUMNS)`` 칸이고, 넘치면 마지막 칸에 합친다.

    Inverse of `property_rows` for one row (always `len(PROPERTY_COLUMNS)` cells)."""
    cells = [c.strip() for c in str(row).split(PROPERTY_SEP)]
    n = len(PROPERTY_COLUMNS)
    if len(cells) > n:
        cells = cells[: n - 1] + [" / ".join(cells[n - 1:])]
    return cells + ["-"] * (n - len(cells))


def property_guide(cfg: Any) -> list[str]:
    """외부 설정 규칙 파일(``knowledge/config/externalized.yml``)의 안내 줄.
    설정이 없거나 파일을 못 읽으면 [].

    Guide lines from the externalized-config rules file; [] when cfg is None or the file does not load."""
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
    """"이제부터 할 일" 의 외부 설정 한 줄: 첫 기동 전에 값을 넣어야 하는 설정 N개와 자리표시를 쓴 파일."""
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
    """재구성이 이미 같은 외부 설정 안내를 수동 후속 작업에 남겼는가.
    남겼으면 그 줄을 쓰고 중복 줄은 만들지 않는다."""
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
    """재구성 전에 기록된 파일 경로가 지금 어디 있는지. 그대로 있으면(또는 ``ws`` 가 None) 그대로 두고, 없으면
    ``src/`` 부터의 뒷부분을 설정된 레이아웃의 comp / boot / front 모듈 아래에서 찾는다 (회사 ``{name}-back``
    / ``-boot`` / ``-front``, 또는 예전 ``module/comp-jar`` / ``module/boot-jar``). 재구성이 소스를 옮기므로,
    그 전에 수정 시도가 남긴 경로는 보고서에서 낡은 경로가 된다(round 9).

    Where a file recorded before the restructure step lives now. Unchanged when it still exists (or `ws` is None);
    else the part from the `src/` component on is looked up under the comp, boot and front module directories of
    the configured layout (`cfg.layout.module_path(which, name)` — company `{name}-back` / `-boot` / `-front`, or
    the legacy `module/comp-jar` / `module/boot-jar`; without a config the legacy names); the original path when
    none exists. Pure (reads only `ws.exists`)."""
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
    """회사 레이아웃의 ``{name}``: 루트 pom 의 artifactId(없으면 프로필 이름).

    `{name}` of the company layout: the root pom's artifactId (else the profile name)."""
    rp = getattr(profile, "root_pom", None)
    return (getattr(rp, "artifact_id", None) or getattr(profile, "name", None) or "") if profile is not None else ""


def operator_checks_item(n: int) -> str:
    """메이저 업그레이드가 있었던 실행의 "이제부터 할 일" 첫 줄(v2.1d 운영자 확인 필요,
    ``report.operator_checks``).

    The first "from here on" line when the run made major upgrades (v2.1d, `report.operator_checks`)."""
    return f"operator checks: {n} major upgrade(s) — see \"Operator checks\" above"


def plural(n: int, word: str) -> str:
    """``1 file`` / ``3 files`` — 영어 데이터 줄의 단·복수."""
    return f"{n} {word}" + ("" if n == 1 else "s")


def jsp_jakarta_text(files: int, refs: int) -> str:
    """``JSP javax → jakarta: 3 files, 12 references`` — 재구성이 JSP 안의 javax EE 이름을 바꾼 결과 한 구절(A5).

    The JSP rename in one phrase (`RestructureResult.jsp_jakarta_files` / `jsp_jakarta_refs`)."""
    return f"{JSP_JAKARTA_LABEL}: {plural(files, 'file')}, {plural(refs, 'reference')}"


def jsp_jakarta_note(step_id: str, files: int, refs: int) -> str:
    """재구성 단계 메모 ``<step id>: JSP javax → jakarta: N files, M references`` — 오케스트레이터가 남기고
    ``jsp_jakarta_counts`` 가 다시 읽는다(report.json 에 남는 유일한 자리).

    The restructure step's note the orchestrator writes; `jsp_jakarta_counts` reads it back."""
    return f"{step_id}: {jsp_jakarta_text(files, refs)}"


def is_jsp_jakarta_note(note: Any) -> bool:
    """``jsp_jakarta_note`` 가 만든 메모이거나 재구성 자신의 같은 문장인가(보고서 Notes 에서 "What we changed" 와
    겹치지 않게 뺀다)."""
    return bool(_JSP_JAKARTA_ANY_RE.match(str(note or "")))


def jsp_jakarta_counts(report: Any) -> tuple[int, int]:
    """이번 실행이 JSP 에서 바꾼 (파일 수, 참조 수): 모든 단계 메모의 ``jsp_jakarta_note`` 를 더한다. 없으면 (0, 0).

    (files, references) the run renamed in JSPs: the sum of every phase's `jsp_jakarta_note`; (0, 0) without one."""
    files = refs = 0
    for pr in list(getattr(report, "phases", None) or []):
        for n in list(getattr(pr, "notes", None) or []):
            m = _JSP_JAKARTA_NOTE_RE.match(str(n))
            if m:
                files += int(m.group("files"))
                refs += int(m.group("refs"))
    return files, refs


def jsp_javax_left(report: Any) -> int:
    """아직 javax EE 이름이 남았을 JSP 파일 수: 분석이 센 ``SourceStats.jsp_javax_files`` − 재구성이 바꾼 파일 수
    (0 아래로는 안 내려감). 이름 바꾸기가 꺼졌거나 돌지 않았으면 분석 수 그대로.

    JSP / tag files that still name javax EE types: the analyzer's `jsp_javax_files` minus the files renamed."""
    src = getattr(getattr(report, "profile", None), "source", None)
    found = int(getattr(src, "jsp_javax_files", 0) or 0)
    return max(0, found - jsp_jakarta_counts(report)[0]) if found else 0


def _jsp_followup_present(report: RunReport) -> bool:
    """재구성이 이미 JSP javax 후속 작업 줄을 남겼는가(남겼으면 그 줄을 쓰고 인수인계 줄은 만들지 않는다)."""
    for m in report.manual_followups or []:
        low = m.lower()
        if "jsp" in low and "javax" in low:
            return True
    return False


def jsp_javax_item(report: RunReport, cfg: Any = None) -> str | None:
    """인수인계 "이제부터 할 일" 의 JSP 한 줄 ``JSP javax references remain (N files) — …``(A5): 분석이 찾은 javax
    JSP 중 재구성이 바꾸지 않은 것이 있고, 재구성이 같은 내용의 후속 작업 줄을 남기지 않았을 때만. 없으면 None.

    `JSP javax references remain (N files) — …` when JSPs naming javax EE types were left unrenamed (rename off,
    or files it could not rewrite) and the restructure did not leave its own follow-up line; None otherwise."""
    left = jsp_javax_left(report)
    if not left or _jsp_followup_present(report):
        return None
    layout = getattr(cfg, "layout", None) if cfg is not None else None
    off = layout is not None and not getattr(layout, "jsp_jakarta_rename", True)
    return JSP_JAVAX_LEFT.format(files=plural(left, "file")) + (JSP_RENAME_OFF if off else "")


def jar_stem_version(path: str) -> tuple[str, str]:
    """jar 파일 이름 → (stem, 버전). ``acme-util-1.2.jar`` → (acme-util, 1.2), ``ojdbc6.jar`` → (ojdbc6, "").

    (stem, version) of a jar file name — the analyzer's split (`acme-util-1.2.jar` → acme-util / 1.2)."""
    name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    m = _JAR_NAME_VERSION_RE.match(name)
    if m:
        return m.group("stem"), m.group("ver")
    return (name[:-4] if name.lower().endswith(".jar") else name), ""


def install_file_command(path: str, group_id: str, jar: Any = None) -> str:
    """좌표 미상 jar 하나를 저장소에 넣는 ``mvn install:install-file …`` 한 줄(v2.1j). artifactId / 버전은
    ``JarCoordinate`` 에 있으면 그것, 없으면 파일 이름에서(버전도 없으면 0). groupId 는 jar 에 있으면 그것, 없으면
    ``group_id``(``intake.synth_group_id``) — 합성 pom 의 system 항목과 같은 좌표라야 바꿔 끼울 수 있다.

    `mvn install:install-file -Dfile=<jar> -DgroupId=… -DartifactId=<stem> -Dversion=<version or 0> -Dpackaging=jar`
    for one jar without coordinates; the coordinates match the synthesized pom's system-scope entry."""
    stem, ver = jar_stem_version(path)
    artifact = getattr(jar, "artifact_id", "") or stem
    version = getattr(jar, "version", "") or ver or "0"
    group = getattr(jar, "group_id", "") or group_id
    f = str(path).replace("\\", "/")
    f = f'"{f}"' if " " in f else f            # a path with a space must stay one argument
    return (f"mvn install:install-file -Dfile={f} -DgroupId={group} -DartifactId={artifact} -Dversion={version} "
            "-Dpackaging=jar")


def pom_synthesis_counts(ps: Any) -> dict[str, int]:
    """합성 pom 의 jar 수와 좌표 출처별 수(``jars`` · ``pom_properties`` · ``name_table`` · ``sha1_lookup`` ·
    ``unknown``). manifest 는 메모일 뿐 좌표가 아니므로 미상으로 센다 — 접수 로그 줄과 같은 셈.

    Jar count and coordinates per source of a `PomSynthesis` (manifest counts as unknown, as in the intake log)."""
    c = dict(getattr(ps, "counts", None) or {})
    return {"jars": len(getattr(ps, "jars", None) or []), "pom_properties": c.get("pom_properties", 0),
            "name_table": c.get("name_table", 0), "sha1_lookup": c.get("sha1_lookup", 0),
            "unknown": c.get("unknown", 0) + c.get("manifest", 0)}


def unknown_jars_system(ps: Any, cfg: Any = None) -> bool:
    """좌표 미상 jar 를 ``system`` scope 로 선언했나(아니면 ``skip`` = 선언 안 함). jar 표의 scope 가 먼저,
    없으면 ``intake.unknown_jar``, 그것도 없으면 기본값(system).

    True when the unknown jars were declared with `system` scope (`intake.unknown_jar: system`, the default)."""
    by_path = {j.path: j for j in getattr(ps, "jars", None) or []}
    scopes = {getattr(by_path.get(p), "scope", "") for p in getattr(ps, "unknown_jars", None) or []}
    if "system" in scopes:
        return True
    intake = getattr(cfg, "intake", None) if cfg is not None else None
    return str(getattr(intake, "unknown_jar", "system") or "system") == "system"


def unknown_jar_commands(ps: Any, cfg: Any = None) -> list[str]:
    """``PomSynthesis.unknown_jars`` 마다 install-file 명령 한 줄(인수인계 "Ant 프로젝트: pom.xml 합성" 블록).

    One `install_file_command` per unknown jar of a `PomSynthesis` (Handover block "Ant project: pom.xml
    synthesized"); groupId = `intake.synth_group_id` (cfg) or the synthesized pom's groupId."""
    by_path = {j.path: j for j in getattr(ps, "jars", None) or []}
    intake = getattr(cfg, "intake", None) if cfg is not None else None
    group = getattr(intake, "synth_group_id", "") or getattr(ps, "group_id", "") or "com.company.migrated"
    return [install_file_command(p, group, by_path.get(p)) for p in getattr(ps, "unknown_jars", None) or []]


def _from_here(h: HandoverSummary, report: RunReport, cfg: Any, view: Any = None) -> list[str]:
    """인수인계의 "이제부터 할 일" 목록. 순서가 곧 중요도다: (0) 운영자 확인 필요(메이저 업그레이드) → (a)
    교체 안 된 권고 → (b) 실행 중 교체됐으니 확인 → (b2) LLM 이 권고에 따라 고친 코드 → (c) as-is 버전으로
    고정된 의존성 → (d) 차단 / 미해결 / javax jar 안내 → (d1) 구조 검토 → (d2) 외부 설정값 → (d3) 남은 JSP javax
    → (e) 나머지 수동 후속 작업. ``advisories.max_from_here`` 가 양수면 그 줄 수로 자르고 마지막에 "… and N more" 를 붙인다."""
    adv_cfg = getattr(cfg, "advisories", None) if cfg is not None else None
    out: list[str] = []
    findings = list(report.advisories or [])
    # 메이저 업그레이드는 막지 않는 대신(빌드는 통과해야 함) 사람이 꼭 보게 맨 첫 줄에 둔다(v2.1d)
    # (0) v2.1d: major upgrades / library replacements the operator must verify (the block above Versions replaced)
    checks = list(getattr(report, "operator_checks", None) or [])
    if checks:
        _dedupe_add(out, operator_checks_item(len(checks)))
    # (a) advisories not replaced
    for f in findings:
        if f.outcome == "replaced":
            continue
        txt = f"{f.title}: {f.followup}".rstrip(": ") if f.followup else f.title
        if f.replacement:
            txt += f" (replacement: {f.replacement}{' ' + f.replacement_version if f.replacement_version else ''})"
        _dedupe_add(out, txt)
    # 권고는 안내만 한다(차단 정책 없음) — 교체는 레시피나 수정 스크립트가 했으니 사람이 동작을 확인해야 한다
    # (b) replaced → verify (advisories are guide-only: the plan's recipes / the fix scripts replaced it)
    for f in findings:
        if f.outcome != "replaced":
            continue
        txt = f"verify {f.library} → {f.replacement or '?'} (replaced during the run)"
        if f.followup:
            txt += f": {f.followup}"
        _dedupe_add(out, txt)
    # (b2) code the LLM fix scripts rewrote with a library advisory's guidance → review
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
    # (c) carried-over pins
    if adv_cfg is None or getattr(adv_cfg, "report_carried_over", True):
        co = h.carried_over
        if len(co) > CARRIED_LIST_MAX:
            _dedupe_add(out, f"{len(co)} dependencies are pinned at their as-is versions because the new parent / "
                             f"Boot BOM no longer manages them (⚠ in Versions replaced) — confirm or replace each; "
                             f"the first {CARRIED_LIST_MAX}:")
        for c in co[:CARRIED_LIST_MAX]:
            _dedupe_add(out, f"{c.key} pinned at {c.after} — the new parent/Boot BOM no longer manages it: "
                             "confirm the version or replace")
    # (d) pointers
    if report.blocked:
        _dedupe_add(out, f"{len(report.blocked)} cluster(s) blocked — see Blocked")
    entries = list(getattr(report, "error_entries", None) or [])
    left = sum(1 for e in entries if getattr(e, "status", "") in ("open", "unresolved")) if entries \
        else len(report.unresolved)
    # v2.1: 미해결 개수는 오류 목록(open + unresolved)에서 센다(목록이 없을 때만 예전 unresolved 섹션)
    if left:                               # v2.1: the error list holds them (the old section is its fallback)
        _dedupe_add(out, f"{left} unresolved error(s) — see Error list")
    jars = list(getattr(report, "javax_api_jars", None) or [])
    if jars and not any(_JAVAX_FOLLOWUP_RE.match(m) for m in report.manual_followups):
        # the orchestrator's follow-up (listed under (e)) already names the libraries; the pointer stands in for it
        _dedupe_add(out, f"{len(jars)} javax API jar(s) still on the classpath — see that section")
    # (d1) v2 structure review: what its script changed, critical issues left, warnings for the owning team
    for line in review_followups(report):
        _dedupe_add(out, line)
    # (d2) externalized settings — the restructurer's follow-up says the same; keep that one when it exists
    try:
        item = None if _properties_followup_present(report) else _properties_item(_inventory(report), cfg)
        if item:
            _dedupe_add(out, item)
    except Exception as e:  # pragma: no cover
        log.debug("properties item skipped: %s", e)
    # (d3) v2.1h: OpenRewrite 는 JSP 를 읽지 않는다 — 재구성이 못 바꾼 JSP 의 javax 는 빌드는 통과해도 Tomcat 10+ 에서
    # 실행 시 깨진다. 재구성이 같은 내용의 후속 작업 줄을 남겼으면 그 줄((e) 에 나옴)을 쓴다
    try:
        jsp = jsp_javax_item(report, cfg)
        if jsp:
            _dedupe_add(out, jsp)
    except Exception as e:  # noqa: BLE001  # pragma: no cover - the Handover never fails over one line
        log.debug("JSP javax item skipped: %s", e)
    if "test-compile-skipped" in report.flags:
        _dedupe_add(out, "test sources do not compile yet (main code builds with -Dmaven.test.skip=true) — "
                         "fix the tests before relying on CI")
    # (e) 권고의 후속 작업 문장은 (a)/(b) 에서 이미 나왔으므로, 수동 후속 작업에서 같은 문장은 건너뛴다
    # (e) manual follow-ups not already listed (advisory follow-ups are covered by (a)/(b))
    adv_fu: set[str] = set()
    if findings:
        try:
            from sbmig.advisory import followups_for
            adv_fu = {" ".join(x.split()).lower() for x in (followups_for(findings) or [])}
        except Exception as e:  # pragma: no cover - advisory package half-built
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
    """공백을 정리하고 ``limit`` 자로 자른다(넘치면 끝에 …)."""
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def review_followups(report: RunReport) -> list[str]:
    """구조 검토 결과를 "이제부터 할 일" 줄로 압축: 검토의 수정(edits / 스크립트)이 바꾼 것 한 줄, 고치지 못한
    critical, warning 들. 최대 ``REVIEW_FOLLOWUPS_MAX`` 줄. 이슈가 없으면 [].

    v2 structure review → compact "from here on" lines: what the review's fix (edits / script) changed (one line),
    critical issues it did not fix, warnings; at most REVIEW_FOLLOWUPS_MAX lines. [] without issues."""
    rv = getattr(report, "structure_review", None)
    if rv is None or not rv.issues:
        return []
    res = rv.script_result
    fixed = res is not None and res.status == "ok" and not rv.reverted and bool(res.changed_files)

    def where(i: Any) -> str:
        hint = f" → {_short(i.fix_hint, 160)}" if i.fix_hint else ""
        return f"{i.file or '(project)'}: {_short(i.problem, 220)}{hint}"

    out: list[str] = []
    critical = [i for i in rv.issues if i.severity == "critical"]
    if fixed:
        files = ", ".join(res.changed_files[:REVIEW_FILES_MAX])
        if len(res.changed_files) > REVIEW_FILES_MAX:
            files += f" (+{len(res.changed_files) - REVIEW_FILES_MAX} more)"
        # "(sbmig check)" 이슈는 sbmig 가 직접 더한 것이라 검토
        # 수정이 다루지 않는다 — 고쳐졌다고 세지 않고 아래에 남긴다
        handled = [i for i in critical if not i.problem.startswith(REVIEW_SBMIG_CHECK)]
        out.append(f"structure review fix (edits / script) changed {len(res.changed_files)} file(s) for "
                   f"{len(handled)} critical issue(s): {files} (commit {(rv.commit or '-')[:8]}) — check these pom "
                   "changes")
        critical = [i for i in critical if i.problem.startswith(REVIEW_SBMIG_CHECK)]
    out += [f"structure review: critical issue not fixed — {where(i)}" for i in critical]
    out += [f"structure review warning — {where(i)}" for i in rv.issues if i.severity != "critical"]
    if len(out) > REVIEW_FOLLOWUPS_MAX:
        rest = len(out) - (REVIEW_FOLLOWUPS_MAX - 1)
        out = out[: REVIEW_FOLLOWUPS_MAX - 1] + [f"… and {rest} more structure review issue(s) — see Structure review"]
    return out


def merge_conflict_lines(report: RunReport) -> list[str]:
    """다중 모듈 ``merge`` 레이아웃의 충돌(``report.merge_conflicts``): 내용이 다른 경로는 한 줄씩(어느 모듈
    것을 남겼고 무엇을 버렸는지), 내용이 같은 중복은 한 줄로 모은다.

    `report.merge_conflicts` (layout `merge`) → one line per differing path, identical copies in one line."""
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
    """인수인계 본체: 최종 pom 을 다시 파싱해 parent → BOM → 의존성 → 플러그인 순으로 전후를 비교하고,
    버전 변경 줄(VersionChange)·추가 / 삭제·고정 버전·merge 충돌·외부 설정·"이제부터 할 일" 을 채운다."""
    from sbmig.analyze.pom_parser import parse_poms

    effective = effective or {}
    h = HandoverSummary()
    before_poms = list(profile.poms)
    final, warnings = parse_poms(view)
    h.notes += [f"final poms: {w}" for w in warnings[:5]]
    own = _own_keys(before_poms) | _own_keys(final)

    # ---- parent / java / boot
    pb, pa = _parents(before_poms), _parents(final)
    h.parent_before, h.parent_after = _fmt_parents(pb), _fmt_parents(pa)
    for k, v in _versions(profile, final, cfg).items():
        setattr(h, k, v)

    notes = _notes(report)
    optional, mandatory = _restructure_keys(notes)
    adv_arts, adv_repl = _advisory_maps(list(report.advisories or []))
    changes: list[VersionChange] = []
    main = _main_pom(final)
    main_path = main.path if main else "pom.xml"

    # parent rows: same key → version change; different keys → one swap row per pair
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

    # ---- BOM imports
    bb, ba = _boms(before_poms), _boms(final)
    for k in sorted(set(bb) | set(ba)):
        if k in bb and k in ba:
            if bb[k][0] != ba[k][0]:
                changes.append(VersionChange(kind="bom", key=k, before=bb[k][0], after=ba[k][0],
                                             reason=REASON_BOOT if _is_spring(k) else REASON_OTHER, pom=ba[k][1]))
        elif k in bb:
            h.removed.append(f"bom {_coord(k, bb[k][0])}")
        else:
            h.added.append(f"bom {_coord(k, ba[k][0])}")

    # ---- dependencies
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
        if key in optional:
            return REASON_OPTIONAL
        if key in mandatory:
            return REASON_MANDATORY
        if _is_spring(key):
            return REASON_BOOT
        return REASON_OTHER

    for k in sorted(set(db) | set(da)):
        if k in db and k in da:
            (bv, bs), (av, as_) = db[k], da[k]
            # 전후 모두 parent / BOM 이 관리: parent·BOM 줄이 이미 말해 주므로 의존성 줄은 만들지 않는다
            if bs in ("managed", "absent") and as_ == "managed":
                continue                              # managed before and after: covered by the parent / BOM row
            if bv == av and bs == as_:
                continue
            # ⚠ 고정 버전: as-is 에선 옛 parent 가 관리하던 버전이, 새 parent / Boot BOM 이 관리하지 않아 그
            # 값 그대로 pom 에 박혔다 — 담당팀이 확인하거나 바꿔야 한다
            carried = (bs == "managed" and as_ == "declared" and av is not None
                       and av == _eff_lookup(effective, k))
            if bv == av and not carried:
                continue                              # same version, only the declaring place changed
            changes.append(VersionChange(kind="dependency", key=k, before=bv, after=av, before_source=bs,
                                         after_source=as_, carried_over=carried,
                                         reason=REASON_CARRIED if carried else reason_for(k),
                                         pom=where.get(k, main_path)))
        elif k in db:
            r = reason_for(k)
            h.removed.append(_coord(k, db[k][0]) + (f" ({r})" if r.startswith("advisory:") else ""))
        else:
            r = reason_for(k)
            tag = r if r.startswith("advisory:") or r in (REASON_MANDATORY, REASON_OPTIONAL) else ""
            h.added.append(_coord(k, da[k][0]) + (f" ({tag})" if tag else ""))

    # ---- plugins (version changes only; added / removed plugins are restructure noise)
    plb, pla = _plugins(before_poms), _plugins(final)
    for k in sorted(set(plb) & set(pla)):
        if plb[k][0] != pla[k][0]:
            changes.append(VersionChange(kind="plugin", key=k, before=plb[k][0], after=pla[k][0],
                                         reason=REASON_BOOT if _is_spring(k) else REASON_OTHER, pom=pla[k][1]))

    order = {"parent": 0, "bom": 1, "dependency": 2, "plugin": 3, "property": 4}
    changes.sort(key=lambda c: (order.get(c.kind, 9), c.key))
    h.changes = changes
    h.carried_over = [c for c in changes if c.carried_over]
    h.merge_conflicts = merge_conflict_lines(report)
    _fill_properties(h, report, cfg)
    h.from_here = _from_here(h, report, cfg, view)
    return h


def _fill_properties(h: HandoverSummary, report: RunReport, cfg: Any) -> None:
    """외부 설정 표와 안내 줄을 채운다(표가 비면 안내도 비움). 실패해도 인수인계는 계속."""
    try:
        h.properties_to_set = property_rows(_inventory(report), cfg)
        h.properties_guide = property_guide(cfg) if h.properties_to_set else []
    except Exception as e:  # pragma: no cover
        log.debug("properties rows skipped: %s", e)


def build_handover(profile: Optional[ProjectProfile], view: Any, report: RunReport,
                   effective: Optional[dict[str, Any]] = None, cfg: Any = None) -> HandoverSummary:
    """인수인계 섹션 데이터를 만든다(as-is pom vs 최종 pom + 권고 → HandoverSummary). 절대 예외를 내지 않는다
    — 비교가 실패해도 merge 충돌·외부 설정·"이제부터 할 일" 은 따로 채워 담당팀이 읽을 것이 남게 한다.

    As-is vs final poms + advisories → HandoverSummary. Never raises."""
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
