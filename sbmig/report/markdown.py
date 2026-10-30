"""report.md 그리기 — 에이전트가 못 고친 것을 이어받는 as-is 담당팀을 위해 쓴다.
흐름상 위치: 보고서 단계에서 writer.py 의 ``write_report`` 가 ``render_markdown`` 을 부른다. 섹션 순서(v2.1):
머리 표(``SBMIG-RESULT`` 줄 포함) → 인수인계(운영자 확인 필요 → Ant pom.xml 합성(v2.1j) → 교체된 버전 → 주의
라이브러리 → 이제부터 할 일) → Error list(단계별 표) → Fixes by case → Structure review → Blocked → javax API jar →
Manual follow-ups → Flags → What we changed → LLM usage and cost → Notes → Outputs.
인수인계가 맨 앞인 이유: 담당팀은 그 섹션만 읽는다. 모든 문장은 ``tr`` / ``tr_line`` 을 거쳐
``report.language`` 로 번역된다(영어 원문이 번역 표의 키 — 문장을 바꾸면 i18n.py 도 함께). v2.1i: ``fix.mode:
edits`` 실행이면 시도 칸·Fixes by case 가 ``edits:<case>#<n>`` 로, LLM 사용량 섹션에 수정 방식 한 줄(script 모드는 그대로).

report.md rendering — written for the as-is owners who pick up whatever the agent could not fix. v2.1i: an edits-mode
run (`RunReport.fix_mode == "edits"`) labels its tries `edits:<case>#<n>` and names the mode in "LLM usage and cost";
a script-mode report is unchanged."""
from __future__ import annotations

import re
from collections import Counter, OrderedDict
from datetime import datetime
from typing import Any, Iterable, Optional

from sbmig.core.models import AdvisoryFinding, BlockedItem, BuildError, RunReport, StructureReview
from sbmig.errors.entries import resolved_text

from .errors import (
    EDITS_FIX_MODE,
    case_rows,
    counts_text,
    deciding_label,
    entries_of,
    entry_text,
    notes_text,
    review_changes,
    seen_groups,
    stats_text,
    status_counts,
    tries_text,
    where,
)
from .handover import (
    is_jsp_jakarta_note,
    jsp_jakarta_counts,
    pom_synthesis_counts,
    tr_reason,
    unknown_jar_commands,
    unknown_jars_system,
)
from .i18n import normalize, tr, tr_line
from .result_line import report_result_line

# 플래그 → 보고서 "Flags" 섹션의 설명. 값은 i18n.py STRINGS 의 키이기도 하다(바꾸면 번역도 함께)
FLAG_HELP = {
    "test-compile-skipped": "main code builds, but test sources only compile with `-Dmaven.test.skip=true` "
                            "(see follow-ups) — fix the tests before relying on CI",
    "tests-skipped": "main code compiles and packages, but some tests fail at runtime: the jar was built with "
                     "`-DskipTests` (failing tests listed in follow-ups / unresolved)",
    "tests-not-compiled": "every build ran with `-Dmaven.test.skip=true` (build.skip_test_compile): test sources were "
                          "neither compiled nor run — the owning team compiles and fixes them",
    "time-budget-exhausted": "the run hit `loop.max_total_minutes`; remaining loops were skipped, verify still ran",
    "llm-budget-exhausted": "the run used all `loop.max_llm_calls`; remaining clusters were escalated",
    "dry-run": "dry run: decisions were computed but nothing was applied",
    "aborted": "the run stopped early (see follow-ups for the reason)",
    "mvn-not-found": "Maven was not runnable on this machine",
    "llm-unavailable": "the LLM failed twice in a row (transport/config); the cases still needing a fix script are "
                       "listed as blocked (llm_unavailable) — stored scripts and the final build still ran",
    "aborted:parent-not-found": "the configured in-house parent POM is not resolvable — nothing can build until it is "
                                "(see Blocked)",
    "rewrite-parse-problems": "OpenRewrite could not parse some source files (see the phase notes / rewrite log): "
                              "recipes that need type information skipped them; the fix loop covered what the build "
                              "found",
    "javax-api-on-classpath": "javax EE API jars are still on the resolved classpath after the migration (section "
                              "\"javax API jars still on the classpath\"): the direct dependencies that drag them in "
                              "are compiled against javax and need Jakarta builds",
    "web-xml-registered": "web.xml servlets / filters / listeners / error pages were turned into a generated "
                          "WebXmlRegistrations @Configuration in the boot module (web.xml itself is not read by "
                          "Spring Boot) — verify the mappings in the Handover",
    "aggregator-generated": "the project had no pom.xml at its root but sibling poms that depend on each other (e.g. APP/BATCH, APP/WEB, APP/EAR) or one aggregator below the root (e.g. APP/pom.xml listing ../DEV): sbmig generated a root aggregator pom so they build in one reactor and are merged into the company layout (layout.generate_aggregator)",
    "ear-without-modules": "the input was an EAR pom whose modules are not in the directory (e.g. APP/EAR alone): run sbmig on the directory that contains every module",
    "ear-module-dropped": "an EAR module (application-server packaging) was dropped: its modules were merged and the boot module is deployed instead; descriptors are kept under asis-ear/ and the context root is a follow-up",
    "war-overlay-moved": "a WAR dependency (maven-war-plugin overlay) was moved from the jar modules to the boot WAR — the overlay WAR must be resolvable from your repositories",
    "web-content-left": "web content that no pom references (e.g. a STATIC/ folder with JSPs / downloads) was left in place — serve it as before or move it into the front module",
    "jdk-not-21": "`mvn -v` reported another Java version than `build.require_java_major`: set build.java_home / "
                  "JAVA_HOME to a JDK 21 and run again",
    "llm-config-error": "the LLM is not configured (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL in .env, or the "
                        "gateway rejected the key / model): LLM fixes were skipped — check with `sbmig llm-probe`",
    "major-upgrade": "a fix raised a dependency across its major version or replaced a library (the build passes): "
                     "the operator verifies the runtime behaviour — see \"Operator checks\" in the Handover",
    # v2.1j: Ant 프로젝트의 pom.xml 합성(정보) / 좌표 미상 jar(주의 — SBMIG-RESULT attn= 에도 나온다)
    "pom-synthesized": "the project had an Ant build.xml and no pom.xml: sbmig synthesized a pom.xml from build.xml "
                       "and the directories at intake (intake.ant_to_maven) and migrated it as a Maven project — see "
                       "\"Ant project: pom.xml synthesized\" in the Handover",
    "jar-coordinates-unknown": "jars of the Ant project have no Maven coordinates (no pom.properties inside, not in "
                               "jar_names.yml, not found by sha1 in the repository): declared with `system` scope "
                               "(intake.unknown_jar) the build passes, but they are not packaged into the WAR — "
                               "install them into Nexus and replace the pom entries (commands in the Handover)",
    # v2.1k: reactor 에 두 번 닿던 모듈의 중복 <module> 줄을 접수에서 지움(정보 — 후속 조치에 되돌리는 법)
    "reactor-duplicate-removed": "a module was listed twice in the reactor (e.g. by the root pom and as `../DEV` by a "
                                 "nested aggregator), which Maven refuses (\"is duplicated in the reactor\"): the "
                                 "nested aggregator's listing was removed at intake so the build runs — see Manual "
                                 "follow-ups",
    "no-root-pom": "the project has no pom.xml at its root and sbmig could not generate one (the top-level poms are "
                   "not one application and not MSA services): the run stopped before any Maven call — add a root "
                   "pom.xml or run sbmig on each application's directory",
    # v2.1s: 사내 부모 체인이 Boot BOM 을 import 하지 않는데 parent_manages_spring_boot 가 true
    "parent-chain-no-boot-bom": "artifacts.parent_manages_spring_boot is true but the in-house parent chain does not "
                                "import the Spring Boot BOM: the versions it does not manage were pinned from the "
                                "target Boot BOM — import spring-boot-dependencies in base-parent (see the Handover's "
                                "first line)",
}
FLAG_PREFIX_HELP = {
    "stopped-after-": "the run was stopped on purpose after this phase (--stop-after)",
    "aborted:": "the run stopped early (see Blocked / follow-ups)",
    "infra:": "the run ended for an infrastructure reason (LLM gateway, Maven, JDK, repository / Nexus, disk), not "
              "because of the project: fix the environment and run it again (a fleet re-queues the project)",
}
BLOCKED_TITLE = "Blocked — needs human / library action"
HANDOVER_TITLE = "Handover — what we replaced, what is yours"
PROPERTIES_TITLE = "Properties to set before running"
JAVAX_JARS_TITLE = "javax API jars still on the classpath"
REVIEW_TITLE = "Structure review"
REVIEW_MAX_ROWS = 30
REVIEW_FILES_SHOWN = 8


def flag_help(flag: str, lang: str = "en") -> Optional[str]:
    """플래그 하나의 설명(정확한 이름 먼저, 없으면 ``infra:`` 같은 접두어 설명). 모르는 플래그는 None."""
    if flag in FLAG_HELP:
        return tr(lang, FLAG_HELP[flag])
    h = next((h for p, h in FLAG_PREFIX_HELP.items() if flag.startswith(p)), None)
    return tr(lang, h) if h is not None else None


def blocked_count_line(items: list[BlockedItem], lang: str = "en") -> str:
    """차단 항목 요약 한 줄: ``N item(s) — <차단 이유> <개수>, …`` (많은 이유 먼저)."""
    reasons = Counter(b.reason.value for b in items)
    return tr(lang, "{n} item(s) — ", n=len(items)) + ", ".join(f"{k} {v}" for k, v in reasons.most_common())


def _blocked_who(b: BlockedItem, lang: str = "en") -> str:
    """차단 표의 "artifact / owner class" 칸: 아티팩트(끌어오는 직접 의존성은 ← 로), 없으면 소유 클래스."""
    if b.artifact is not None:
        who = f"`{b.artifact.coordinates}`"
        if b.via_direct is not None:            # the library that drags the javax API jar in (dependency:tree)
            who += f" ← `{b.via_direct.coordinates}`"
        elif len(b.via) == 2:
            who += tr(lang, " ← declared in the pom")
        return who + (tr(lang, " (class `{owner}`)", owner=b.owner_class) if b.owner_class else "")
    if b.owner_class:
        return tr(lang, "class `{owner}`", owner=b.owner_class)
    return "-"


def _blocked_types(b: BlockedItem) -> str:
    if b.required_type or b.found_type:
        return f"`{b.required_type or '?'}` → `{b.found_type or '?'}`"
    return "-"


def blocked_rows(items: list[BlockedItem], lang: str = "en") -> list[str]:
    """"Blocked" 섹션의 표(이유 · 아티팩트 / 클래스 · 필요 → 실제 타입 · 파일 · 시도 수 · 제안). 사람이나
    라이브러리 담당이 무엇을 해야 하는지가 한 줄에 보이게 한다."""
    rows = [tr(lang, "| reason | artifact / owner class | required → found | files | attempts | suggestion |"),
            "|---|---|---|---|---|---|"]
    for b in items:
        files = ", ".join(f"`{f}`" for f in b.files[:5]) + (f" (+{len(b.files) - 5})" if len(b.files) > 5 else "")
        rows.append(f"| `{b.reason.value}` ({b.phase.value}) | {_cell(_blocked_who(b, lang), 160)} | "
                    f"{_cell(_blocked_types(b), 200)} | {_cell(files or '-', 400)} | {b.attempts} | "
                    f"{_cell(tr_line(lang, b.suggestion), 500)} |")
    return rows


def _in_house_groups(r: RunReport, cfg: Any = None) -> list[str]:
    """"in-house → Jakarta build" 판정에 쓰는 사내 groupId 들: 설정이 있으면 parent / 필수 / 선택 아티팩트의
    그룹, 없으면 인수인계의 최종 parent 그룹. Spring 자신의
    그룹은 절대 넣지 않는다(회사 좌표는 코드에 두지 않는다).

    Company groups for "in-house → Jakarta build": the configured parent / mandatory / optional groups when the
    config is at hand, else the group(s) of the final parent(s) in the Handover (never Spring's own groups)."""
    out: list[str] = []
    arts = getattr(cfg, "artifacts", None) if cfg is not None else None
    if arts is not None:
        try:
            out += [s.group_id for s in [*arts.parent_specs(), *arts.mandatory, *arts.optional]]
        except Exception:  # pragma: no cover
            pass
    h = r.handover
    if h is not None and h.parent_after:
        out += [c.strip().split(":", 1)[0] for c in h.parent_after.split(",") if ":" in c]
    return [g for g in dict.fromkeys(out) if g and not g.startswith("org.springframework")]


def _javax_what_to_do(j: Any, groups: list[str], lang: str = "en") -> str:
    """javax jar 한 줄의 "what to do" 칸: 끌어오는 라이브러리가 jakarta 라이브러리 표에 있으면 그 버전(좌표가
    바뀐 것은 이전 안내), 사내 그룹이면 "in-house → Jakarta build",
    pom 에 직접 선언됐으면 삭제, 그 밖은 업그레이드 / 교체."""
    via = j.via
    if via is not None:
        try:
            from sbmig.core.jakarta_libraries import jakarta_library_for
            lib = jakarta_library_for(via.key)
        except Exception:  # pragma: no cover - knowledge table unreadable
            lib = None
        if lib is not None:
            if via.key != lib.key:                       # earlier coordinates (ch.simas.qlrm → org.qlrm)
                txt = tr(lang, "coordinates changed: relocate to `{key}:{version}` (jakarta.* since {since})",
                         key=lib.key, version=lib.version, since=lib.jakarta_since)
            else:
                txt = tr(lang, "raise to {version} (jakarta.* since {since})", version=lib.version,
                         since=lib.jakarta_since)
            if lib.requires_java:
                txt += tr(lang, ", requires Java {java}", java=lib.requires_java)
            return txt + (f" — {lib.note}" if lib.note else "")
        if any(via.group_id == g or via.group_id.startswith(g + ".") for g in groups):
            return tr(lang, "in-house → Jakarta build")
        return tr(lang, "upgrade / replace")
    if j.declared:
        return tr(lang, "remove the declaration from the pom")
    return tr(lang, "upgrade / replace")


def javax_api_lines(r: RunReport, lang: str = "en", cfg: Any = None) -> list[str]:
    """``## javax API jars still on the classpath`` 섹션(X1b, 2026-10-22). 빌드는 됐어도 javax 기준
    라이브러리가 남아 있으면 jakarta 타입을 넘기는 코드는 런타임에 깨진다 — 그래서 따로 보여 준다. 없으면 [].

    `## javax API jars still on the classpath` (report.javax_api_inventory, 2026-10-22); [] when none."""
    jars = list(r.javax_api_jars or [])
    if not jars:
        return []
    groups = _in_house_groups(r, cfg)
    L = [f"## {tr(lang, JAVAX_JARS_TITLE)}", "",
         tr(lang, "{n} javax EE API jar(s) are still on the resolved classpath after the migration "
                  "(`mvn dependency:tree`). The library in \"via\" is compiled against javax: code that passes "
                  "jakarta.* types to it does not compile, and it fails at runtime.", n=len(jars)), "",
         tr(lang, "| artifact | module | scope | via (direct dependency) | declared in pom | what to do |"),
         "|---|---|---|---|---|---|"]
    for j in jars:
        via = f"`{j.via.coordinates}`" if j.via is not None else "-"
        L.append(f"| `{_cell(j.artifact.coordinates, 120)}` | {_cell(j.module or tr(lang, '(root)'), 60)} | "
                 f"{j.scope or '-'} | {_cell(via, 120)} | {tr(lang, 'yes') if j.declared else tr(lang, 'no')} | "
                 f"{_cell(_javax_what_to_do(j, groups, lang), 400)} |")
    L.append("")
    return L


def _cell(text: object, limit: int = 160) -> str:
    """markdown 표 칸: 공백 정리, ``limit`` 자 자르기(…), ``|`` 이스케이프, 빈 값은 ``-``."""
    s = " ".join(str(text if text is not None else "").split())
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s.replace("|", "\\|") or "-"


def _dt(d: Optional[datetime]) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S UTC") if d else "-"


def _duration(r: RunReport, lang: str = "en") -> str:
    if "total" in r.timings_s:
        secs = r.timings_s["total"]
    elif r.finished_at:
        secs = (r.finished_at - r.started_at).total_seconds()
    else:
        return "-"
    m, s = divmod(int(secs), 60)
    return tr(lang, "{m}m {s}s", m=m, s=f"{s:02d}") if m else tr(lang, "{secs}s", secs=f"{secs:.1f}")


def group_by_file(errors: Iterable[BuildError], lang: str = "en") -> "OrderedDict[str, list[BuildError]]":
    """빌드 오류를 파일별로 묶는다(파일이 없으면 모듈, 그것도 없으면 아티팩트).
    오류 목록이 없는 실행의 fallback 표에 쓴다."""
    groups: "OrderedDict[str, list[BuildError]]" = OrderedDict()
    for e in errors:
        key = e.file or (tr(lang, "module {module}", module=e.module) if e.module else
                         (tr(lang, "artifact {coords}", coords=e.artifact.coordinates) if e.artifact
                          else tr(lang, "(no file)")))
        groups.setdefault(key, []).append(e)
    return groups


def _v(v: Optional[str]) -> str:
    return f"`{v}`" if v else "-"


def _what_we_did(f: AdvisoryFinding, lang: str = "en") -> str:
    """권고 표의 "what we did" 칸. 권고는 안내만 한다(차단 정책 없음): 실행 중 레시피·수정 스크립트가
    교체했거나, as-is 버전 그대로 빌드했거나 둘 중 하나다.

    Advisories are guide-only: the library was replaced during the run (the plan's recipes / fix scripts) or the
    build kept the as-is version."""
    if f.outcome == "replaced":
        return tr(lang, "replaced during the run")
    return tr(lang, "built with the as-is version pinned")


def properties_lines(h: Any, r: RunReport, lang: str = "en") -> list[str]:
    """인수인계의 "Properties to set before running" 블록(round 8):
    외부 설정 키 표, 넣는 방법 안내, 자리표시 파일.

    Handover block "Properties to set before running" (round 8): table of external keys, guide, placeholder file."""
    from .handover import PROPERTY_COLUMNS, split_property_row

    L = [f"### {tr(lang, PROPERTIES_TITLE)}", "",
         tr(lang, "The code reads these settings but no packaged resource defines them (the external container "
                  "supplied them before). Set each one before the first start."), ""]
    L.append("| " + " | ".join(tr(lang, c) for c in PROPERTY_COLUMNS) + " |")
    L.append("|" + "---|" * len(PROPERTY_COLUMNS))
    for row in h.properties_to_set:
        c = split_property_row(row)
        L.append(f"| `{_cell(c[0], 120)}` | {_cell(c[1], 40)} | {_cell(c[2], 300)} | {_cell(c[3], 300)} | "
                 f"{_cell(c[4], 120)} |")
    L.append("")
    if h.properties_guide:
        L += [tr(lang, "How to supply them:"), ""]
        L += [f"- {_cell(g, 500)}" for g in h.properties_guide]
        L.append("")
    files = list(getattr(r.properties, "placeholder_files", None) or []) if r.properties is not None else []
    if files:
        L.append(tr(lang, "Packaged placeholders: {files} (grep `__SET_ME__`)",
                    files=", ".join(f"`{f}`" for f in files)))
        L.append("")
    return L


OPERATOR_GUIDE_LINES = 20                    # guide lines per check in report.md (report.json keeps them all)


def _guide_items(guide: str) -> list[str]:
    """운영자 가이드 글을 항목 목록으로(줄머리의 ``-`` / ``*`` / ``•`` 를 떼고 빈 줄 제외)."""
    return [x for x in (re.sub(r"^\s*[-*•]\s+", "", ln).strip() for ln in (guide or "").splitlines()) if x]


def operator_check_lines(r: RunReport, lang: str = "en") -> list[str]:
    """인수인계 맨 위의 "운영자 확인 필요 (메이저 업그레이드 N건)" 블록(v2.1d). 최종 트리에 남은 메이저
    업그레이드 / 라이브러리 교체마다 좌표·pom·케이스·커밋·승인 여부와 운영자 가이드(작성자: LLM 또는 sbmig)를
    적는다. 메이저는 막지 않는 대신(빌드는 통과해야 함) 사람이
    런타임 동작을 꼭 확인하게 하려는 것. 확인할 것이 없으면 [].

    v2.1d Handover block "Operator checks (N major upgrades)" (`report.operator_checks`): one item per major
    upgrade / library replacement still in the final tree — coordinates, pom, case, the try, the commit, approved —
    with the 운영자 확인 guide and who wrote it. [] without checks."""
    checks = list(getattr(r, "operator_checks", None) or [])
    if not checks:
        return []
    L = [tr(lang, "### Operator checks ({n} major upgrades)", n=len(checks)), "",
         tr(lang, "The build passes, but these changes cross a major version line or replace a library: check the "
                  "runtime behaviour (configuration, registrations, renamed packages) before you ship."), ""]
    for c in checks:
        ch = c.change
        where = [f"`{_cell(ch.path, 160)}` ({ch.section})"]
        if c.case_id:
            where.append(tr(lang, "case `{case}`", case=c.case_id))
        if c.source:
            where.append(tr(lang, "structure review") if c.source == "review" else c.source)
        if c.commit:
            where.append(tr(lang, "commit `{sha}`", sha=c.commit[:7]))
        line = f"- **{_cell(ch.text, 200)}** — " + ", ".join(where)
        if ch.approved:                          # depchanges.apply_table: "approved by the shipped / company table"
            src = "jakarta_libraries.yml" if "shipped table" in (ch.note or "") else "libraries.yml"
            line += tr(lang, " — approved ({src})", src=src)
        L.append(line)
        items = _guide_items(c.guide)
        L += [f"  - {_cell(x, 500)}" for x in items[:OPERATOR_GUIDE_LINES]]
        if len(items) > OPERATOR_GUIDE_LINES:
            L.append("  - " + tr(lang, "… {n} more (report.json)", n=len(items) - OPERATOR_GUIDE_LINES))
        if not items:
            L.append("  - " + tr(lang, "(no guide recorded)"))
        L.append("  - " + (tr(lang, "guide source: LLM") if c.guide_source == "llm"
                           else tr(lang, "guide source: sbmig (synthesized)")))
    L.append("")
    return L


POM_SYNTH_LIST_MAX = 50                 # install-file commands / dropped jars / build.xml notes shown (report.json has all)


def _code_list(items: Iterable[str], limit: int, lang: str) -> str:
    """``a`` , ``b`` … 목록(넘치면 "… N more (report.json)")."""
    items = list(items)
    out = ", ".join(f"`{_cell(x, 200)}`" for x in items[:limit]) or "-"
    if len(items) > limit:
        out += " " + tr(lang, "… {n} more (report.json)", n=len(items) - limit)
    return out


def pom_synthesis_lines(r: RunReport, lang: str = "en", cfg: Any = None) -> list[str]:
    """인수인계의 "Ant 프로젝트: pom.xml 합성" 블록(v2.1j, 운영자 확인 블록 바로 다음): 합성 pom 의 좌표·packaging·
    소스 / 웹 루트, jar 좌표 출처별 수, 좌표 미상 jar 마다 ``mvn install:install-file`` 한 줄(system scope 는 빌드는
    통과해도 WAR 에 안 들어가 실행 시 실패한다 — Nexus 에 설치하고 pom 항목을 바꿀 것), 뺀 jar, 옮기지 못한 build.xml
    단계. 합성하지 않은 실행은 [].

    v2.1j Handover block "Ant project: pom.xml synthesized" (`report.pom_synthesis`): coordinates, packaging, layout,
    jar counts per source, one install-file command per unknown jar (system scope builds but is not packaged:
    runtime!), the jars left out, the build.xml steps not translated. [] when the intake synthesized nothing."""
    ps = getattr(r, "pom_synthesis", None)
    if ps is None or not getattr(ps, "ok", False):
        return []
    gav = ":".join(x for x in (ps.group_id, ps.artifact_id, ps.version) if x) or "-"
    n = pom_synthesis_counts(ps)
    L = [tr(lang, "### Ant project: pom.xml synthesized"), "",
         tr(lang, "The project had an Ant `{build}` and no pom.xml: sbmig wrote `{pom}` at intake from build.xml and "
                  "the directories (commit `intake: pom.xml synthesized from build.xml`); build.xml itself is left as "
                  "it was.", build=ps.build_file, pom=ps.pom_path), "",
         tr(lang, "- pom: `{gav}` ({packaging}), Java {java}, encoding {enc}", gav=gav, packaging=ps.packaging,
            java=ps.java_source or "-", enc=ps.encoding or "-"),
         tr(lang, "- layout: sources {src}, web root {web}, resources {res}, lib {lib}",
            src=_code_list(ps.source_dirs, 10, lang), web=f"`{ps.web_root}`" if ps.web_root else "-",
            res=_code_list(ps.resource_dirs, 10, lang), lib=_code_list(ps.lib_dirs, 10, lang)),
         tr(lang, "- jars: {n} — {a} pom.properties, {b} name table, {c} repository lookup, {d} unknown",
            n=n["jars"], a=n["pom_properties"], b=n["name_table"], c=n["sha1_lookup"], d=n["unknown"]), ""]
    cmds = unknown_jar_commands(ps, cfg)
    if cmds:
        if unknown_jars_system(ps, cfg):
            L.append(tr(lang, "**{n} jar(s) without Maven coordinates** — declared with `system` scope "
                              "(`<systemPath>`): the build passes, but system-scope jars are not packaged into the "
                              "WAR / boot jar, so the application fails at runtime until each one is installed into "
                              "Nexus and its pom entry is replaced by a normal dependency (drop `<scope>system</scope>` "
                              "and `<systemPath>`):", n=len(cmds)))
        else:
            L.append(tr(lang, "**{n} jar(s) without Maven coordinates** — not declared (`intake.unknown_jar: skip`): "
                              "the build shows which ones the code needs; install each into Nexus and add it to the "
                              "pom as a dependency:", n=len(cmds)))
        L += ["", "```", *cmds[:POM_SYNTH_LIST_MAX], "```"]
        if len(cmds) > POM_SYNTH_LIST_MAX:
            L.append(tr(lang, "… {n} more (report.json)", n=len(cmds) - POM_SYNTH_LIST_MAX))
        L += [tr(lang, "(`-Dfile` paths are those of the as-is tree; publishing to Nexus is `mvn deploy:deploy-file` "
                       "with the same coordinates plus `-Durl` / `-DrepositoryId`.)"), ""]
    if ps.dropped_jars:
        L += [tr(lang, "Left out (no coordinates and no source imports their packages): {jars} — add one back if the "
                       "build or the runtime needs it.", jars=_code_list(ps.dropped_jars, POM_SYNTH_LIST_MAX, lang)),
              ""]
    if ps.build_xml_notes:
        L += [tr(lang, "build.xml steps not translated to Maven — check whether the build still needs them:")]
        L += [f"- {_cell(x, 300)}" for x in ps.build_xml_notes[:POM_SYNTH_LIST_MAX]]
        if len(ps.build_xml_notes) > POM_SYNTH_LIST_MAX:
            L.append("- " + tr(lang, "… {n} more (report.json)", n=len(ps.build_xml_notes) - POM_SYNTH_LIST_MAX))
        L.append("")
    if ps.warnings:
        L += [tr(lang, "Synthesis warnings:")]
        L += [f"- {_cell(x, 300)}" for x in ps.warnings[:POM_SYNTH_LIST_MAX]]
        L.append("")
    return L


def pom_synthesis_changed_lines(r: RunReport, lang: str = "en") -> list[str]:
    """"What we changed" 의 한 줄 ``pom.xml synthesized from build.xml (N jars: a pom.properties, b table, c lookup,
    d unknown)``(v2.1j). 합성하지 않은 실행은 [].

    "What we changed": the intake's pom synthesis in one line; [] without one."""
    ps = getattr(r, "pom_synthesis", None)
    if ps is None or not getattr(ps, "ok", False):
        return []
    n = pom_synthesis_counts(ps)
    return [tr(lang, "pom.xml synthesized from build.xml ({n} jars: {a} pom.properties, {b} table, {c} lookup, "
                     "{d} unknown)", n=n["jars"], a=n["pom_properties"], b=n["name_table"], c=n["sha1_lookup"],
               d=n["unknown"]), ""]


def handover_lines(r: RunReport, lang: str = "en", cfg: Any = None) -> list[str]:
    """report.md 의 첫 섹션 = 인수인계: 운영자 확인 필요(v2.1d) → Ant pom.xml 합성(v2.1j) → 교체된 버전 → 주의
    라이브러리(권고) → merge 충돌 → 외부 설정 → 이제부터 할 일. 데이터 줄은 영어로 저장돼 있고 ``tr_line`` 으로 번역한다.

    The first section of report.md: operator checks (v2.1d), the Ant pom synthesis (v2.1j, `cfg` gives the
    `intake.*` policy of its install-file lines), versions replaced, libraries needing attention, 'from here on'."""
    lang = normalize(lang)
    L = [f"## {tr(lang, HANDOVER_TITLE)}", ""]
    L += operator_check_lines(r, lang)
    L += pom_synthesis_lines(r, lang, cfg)
    h = r.handover
    findings = list(r.advisories or [])
    has_versions = h is not None and bool(h.changes or h.added or h.removed or h.parent_before or h.parent_after)
    if not findings and not has_versions and not (h and (h.from_here or h.merge_conflicts or h.properties_to_set)):
        L.append(tr(lang, "No version changes and no library advisories recorded for this run.")
                 + (f" ({'; '.join(tr_line(lang, n) for n in h.notes)})" if h and h.notes else ""))
        L.append("")
        return L
    if h is not None:
        L += [tr(lang, "### Versions replaced"), ""]
        if h.parent_before or h.parent_after:
            L.append(tr(lang, "- parent: {before} → {after}", before=_v(h.parent_before), after=_v(h.parent_after)))
        if h.java_before or h.java_after:
            L.append(tr(lang, "- java: {before} → {after}", before=_v(h.java_before), after=_v(h.java_after)))
        if h.spring_boot_before or h.spring_boot_after:
            L.append(tr(lang, "- spring boot: {before} → {after}", before=_v(h.spring_boot_before),
                        after=_v(h.spring_boot_after)))
        L.append("")
        if h.changes:
            L += [tr(lang, "| what | before | after | why |"), "|---|---|---|---|"]
            managed = tr(lang, "managed")
            for c in h.changes:
                warn = "⚠ " if c.carried_over or c.boot_bom_pin else ""
                before = c.before or (managed if c.before_source == "managed" else "-")
                if c.before_source == "managed" and c.before:
                    before += tr(lang, " (managed)")
                after = c.after or (managed if c.after_source == "managed" else "-")
                L.append(f"| {warn}{c.kind} `{_cell(c.key, 120)}` | {_cell(before, 120)} | {_cell(after, 80)} | "
                         f"{_cell(tr_reason(lang, c.reason), 80)} |")
            L.append("")
            if h.carried_over:
                if len(h.carried_over) == 1:
                    L.append(tr(lang, "⚠ 1 dependency pinned at the as-is version: the new parent / Boot BOM no "
                                      "longer manages it."))
                else:
                    L.append(tr(lang, "⚠ {n} dependencies pinned at the as-is version: the new parent / Boot BOM "
                                      "no longer manages them.", n=len(h.carried_over)))
                L.append("")
        if h.added:
            L.append(tr(lang, "Added: {items}", items=", ".join(f"`{x}`" for x in h.added)))
            L.append("")
        if h.removed:
            L.append(tr(lang, "Removed: {items}", items=", ".join(f"`{x}`" for x in h.removed)))
            L.append("")
        if not h.changes and not h.added and not h.removed:
            L.append(tr(lang, "No dependency versions changed."))
            L.append("")
        for n in h.notes:
            L.append(tr(lang, "_note: {note}_", note=_cell(tr_line(lang, n), 300)))
        if h.notes:
            L.append("")
    if findings:
        L += [tr(lang, "### Libraries needing your attention"), ""]
        L += [tr(lang, "| library | as-is | status | what we did | outcome | what you do next | docs |"),
              "|---|---|---|---|---|---|---|"]
        outcomes = {"replaced": tr(lang, "✅ replaced"), "still-present": tr(lang, "⚠ still present")}
        for f in findings:
            nxt = f.followup or f.summary
            if f.replacement:
                nxt = (nxt + " " if nxt else "") + f"→ `{f.replacement}`" + (
                    f" {f.replacement_version}" if f.replacement_version else "")
            docs = ", ".join(f"[{i}]({d})" for i, d in enumerate(f.docs[:3], 1)) or "-"
            outcome = outcomes.get(f.outcome, f.outcome)
            L.append(f"| **{_cell(f.library, 60)}** | {_cell(f.detected_version or '-', 40)} | {f.status} | "
                     f"{_cell(_what_we_did(f, lang), 160)} | {outcome} | {_cell(nxt, 400)} | {docs} |")
        L.append("")
    if h is not None and h.merge_conflicts:
        L += [tr(lang, "**Merge conflicts** (multi-module merge: first module in `<modules>` order wins)"), ""]
        L += [f"- {_cell(tr_line(lang, x), 500)}" for x in h.merge_conflicts]
        L.append("")
    if h is not None and h.properties_to_set:
        L += properties_lines(h, r, lang)
    L += [tr(lang, "### From here on, your team"), ""]
    items = h.from_here if h is not None else [f"{f.title}: {f.followup}".rstrip(": ") for f in findings
                                               if f.outcome != "replaced"]
    if h is None and r.operator_checks:
        from .handover import operator_checks_item
        items = [operator_checks_item(len(r.operator_checks)), *items]
    if items:
        L += [f"{i}. {_cell(tr_line(lang, x), 500)}" for i, x in enumerate(items, 1)]
    else:
        L.append(tr(lang, "Nothing beyond the usual review — the migrated project builds with the versions above."))
    L.append("")
    return L


# ---- v2.1 error list (RunReport.error_entries): one line per build error / pom correction, grouped by where it was
# first seen, and one row per case with the LLM's cause / solution, the applied changes and the stored script
ERROR_LIST_TITLE = "Error list"
FIXES_TITLE = "Fixes by case"
ERROR_LIST_MAX_ROWS = 50                     # per `seen` group; errors.md in the run directory has every entry
FIXES_MAX_ROWS = 40
SEEN_TITLES = {
    "compile": "compile — `mvn compile` after OpenRewrite",
    "restructure": "restructure — pom corrections, verified by the next build",
    "review": "review — structure review",
    "build": "build — first `mvn clean package`, new errors only",
}
SEEN_FIX_TITLE = "{seen} — new errors of round {n}'s build"


def _short(text: object, limit: int) -> str:
    """``_cell`` 처럼 공백 정리 + 자르기, 단 ``|`` 이스케이프는 안 한다(호출한 쪽이 칸 전체를 ``_cell`` 한다).

    Whitespace-collapsed and capped like `_cell`, without the pipe escaping (the caller `_cell`s the whole cell)."""
    s = " ".join(str(text if text is not None else "").split())
    return s[: limit - 1] + "…" if len(s) > limit else s


def _seen_title(seen: str, n: int, lang: str = "en") -> str:
    """Error list 의 단계별 소제목(``fix rN`` 은 "N 라운드 빌드의 새 오류") + 개수."""
    if seen in SEEN_TITLES:
        title = tr(lang, SEEN_TITLES[seen])
    else:
        m = re.match(r"^fix r(\d+)$", seen)
        title = tr(lang, SEEN_FIX_TITLE, seen=seen, n=m.group(1)) if m else seen
    return f"{title} ({n})"


def error_list_lines(r: RunReport, lang: str = "en") -> list[str]:
    """``## Error list``: 개수 → ``seen`` 단계별 표(compile → restructure → review → build → fix rN, 묶음당
    ``ERROR_LIST_MAX_ROWS`` 줄, 전부는 실행 디렉토리의 errors.md) → 마지막 빌드 로그 경로. 오류 목록이
    없으면(목록을 남기기 전에 멈춘 실행) 마지막 빌드의 미해결 오류를 파일별로.

    `## Error list`: counts, one table per `seen` group (compile → restructure → review → build → fix rN), the
    last build log. Without entries: the unresolved errors of the last build grouped by file (runs that stopped
    before the error list was kept), else one line."""
    entries = entries_of(r)
    mode = fix_mode_of(r)
    L = [f"## {tr(lang, ERROR_LIST_TITLE)}", ""]
    if entries:
        L += [tr(lang, "{n} error(s) in this run, one line each (all of them with every try and diff: `errors.md` / "
                       "`errors.jsonl` in the run directory): {counts}.", n=len(entries),
                 counts=counts_text(status_counts(entries))), ""]
        for seen, rows in seen_groups(entries).items():
            L += [f"### {_seen_title(seen, len(rows), lang)}", "",
                  tr(lang, "| # | case | file:line | message | status | tries | resolved |"),
                  "|---|---|---|---|---|---|---|"]
            for i, e in rows[:ERROR_LIST_MAX_ROWS]:
                loc = where(e)
                L.append(f"| {i} | {('`' + e.case_id[:12] + '`') if e.case_id else '-'} | "
                         f"{('`' + _cell(loc, 140) + '`') if loc else '-'} | {_cell(entry_text(e), 200)} | "
                         f"{e.status} | {_cell(tries_text(e.tries, e.case_id, mode) or '-', 300)} | "
                         f"{_cell(resolved_text(e) or '-', 60)} |")
            if len(rows) > ERROR_LIST_MAX_ROWS:
                L += ["", tr(lang, "… {n} more in this group — see `errors.md`", n=len(rows) - ERROR_LIST_MAX_ROWS)]
            L.append("")
    elif r.unresolved:
        L += [tr(lang, "No error list was kept for this run; {n} error(s) were left after the last build, grouped by "
                       "file:", n=len(r.unresolved)), ""]
        for key, errs in group_by_file(r.unresolved, lang).items():
            L.append(f"### `{key}`")
            for e in errs:
                loc = f"L{e.line}: " if e.line else ""
                extra = tr(lang, " — symbol: `{symbol}`", symbol=e.symbol) if e.symbol else ""
                at = tr(lang, " — location: {location}", location=e.location) if e.location else ""
                L.append(f"- [{e.kind.value}] {loc}{_cell(e.message, 300)}{extra}{at}")
            L.append("")
    else:
        L += [tr(lang, "None. The final build passed.") if r.success
              else tr(lang, "None. No structured errors were captured; check the last build log."), ""]
    if r.final_build is not None and r.final_build.log_path:
        L += [tr(lang, "Last build log: `{path}`", path=r.final_build.log_path), ""]
    return L


def fix_mode_of(r: Any) -> str:
    """보고서의 수정 방식(v2.1i ``RunReport.fix_mode``, 없으면 script).

    The run's `fix.mode` as the report recorded it (`RunReport.fix_mode`; `script` when absent)."""
    return str(getattr(r, "fix_mode", "") or "script")


def _case_script_cell(row: Any, lang: str = "en", fix_mode: str = "script") -> str:
    """Fixes by case 의 script 칸: 저장된 스크립트(버전 + 경로), 없으면 스크립트로 못 만든 이유. v2.1i edits 모드는
    결정적 시도의 꼬리표 ``edits:<case>#<n>``(스크립트가 없다)."""
    if fix_mode == EDITS_FIX_MODE:
        label = deciding_label(row, fix_mode)
        return f"`{label}`" if label else "-"
    if row.script:
        return f"v{row.script_version} `{_cell(row.script, 120)}`" if row.script_version else f"`{_cell(row.script, 120)}`"
    if row.no_script_reason:
        return tr(lang, "no script: {reason}", reason=_cell(row.no_script_reason, 200))
    return "-"


def fixes_by_case_lines(r: RunReport, lang: str = "en") -> list[str]:
    """``## Fixes by case``: 손댄 케이스마다 한 줄 — 결정적 시도의 원인 / 해결 방법, 남은 시도가 바꾼
    파일(+/−), 수정 기록(v2.1b ``경로 줄 — 내용``), 저장된
    스크립트. 메이저 업그레이드가 있으면 ⚠ 표시. 시도가 없으면 [].

    `## Fixes by case`: one row per case that was worked on — cause / solution of the deciding try, the files the
    kept tries changed (+/−), what each change did (수정, v2.1b: `path lines — change` per note) and the stored
    script; [] when no case had a try."""
    rows = case_rows(entries_of(r))
    if not rows:
        return []
    mode = fix_mode_of(r)
    head = (tr(lang, "| case | errors | cause | solution | changes (files, +/−) | edits | fix try |")
            if mode == EDITS_FIX_MODE else
            tr(lang, "| case | errors | cause | solution | changes (files, +/−) | edits | script |"))
    L = [f"## {tr(lang, FIXES_TITLE)}", "", head, "|---|---|---|---|---|---|---|"]
    for row in rows[:FIXES_MAX_ROWS]:
        solution = row.solution or (tr(lang, "(no solution text) {reason}", reason=row.no_script_reason)
                                    if row.no_script_reason else "")
        major = (" " + tr(lang, "⚠ major ({n}, see Operator checks)", n=row.major_upgrades)) if row.major_upgrades \
            else ""
        L.append(f"| `{row.case_id}`{major} {_cell(_short(row.name, 100), 120)} | "
                 f"{tr(lang, '{resolved}/{n} resolved', resolved=row.resolved, n=row.entries)} | "
                 f"{_cell(row.analysis or '-', 400)} | {_cell(solution or '-', 400)} | "
                 f"{_cell(stats_text(row.changes) or '-', 300)} | {_cell(notes_text(row.edits) or '-', 700)} | "
                 f"{_case_script_cell(row, lang, mode)} |")
    if len(rows) > FIXES_MAX_ROWS:
        L += ["", tr(lang, "… {n} more case(s) — see `errors.md`", n=len(rows) - FIXES_MAX_ROWS)]
    if mode == EDITS_FIX_MODE:     # v2.1i: no fix scripts in this run — the try label points into errors.md
        L += ["", "_" + tr(lang, "fix.mode edits: no fix scripts; `edits:<case>#<n>` is try n of the case in "
                                 "`errors.md` (with its diff), the whole change set is in `changes.patch`.") + "_", ""]
        return L
    L += ["", "_" + tr(lang, "Stored scripts live in the KB (`<kb>/cases/<case>/fix_vN.py`); every try with its diff "
                             "is in `errors.md`, the whole change set in `changes.patch`.") + "_", ""]
    return L


# ---- v2 structure review (sbmig.review): LLM check of poms / dependencies / modules before the first build
def _review_fix_pending(rv: StructureReview) -> bool:
    """구조 검토가 수정(edits 또는 스크립트)을 내놓았는데 실행되지 않았는가.

    The review proposed a fix (edits or a script) that did not run."""
    a = getattr(rv, "answer", None)
    return bool(rv.script) or (a is not None and bool(a.edits or a.code))


def _review_script_state(rv: StructureReview, lang: str = "en") -> str:
    """구조 검토 수정이 한 일 한 구절(v2.1: 직접 수정 edits 와
    / 또는 스크립트 — 결과는 어느 쪽이든 ``script_result``).

    What the review's fix (v2.1: direct edits and / or a script; `script_result` either way) did."""
    r = rv.script_result
    if r is not None:
        if rv.reverted:
            return tr(lang, "fix {status}, changes restored", status=r.status)
        if rv.commit:
            return tr(lang, "fix {status}, {n} file(s) changed (commit `{commit}`)", status=r.status,
                      n=len(r.changed_files), commit=rv.commit[:8])
        return tr(lang, "fix {status}, {n} file(s) changed", status=r.status, n=len(r.changed_files))
    if _review_fix_pending(rv):
        return tr(lang, "fix not applied")
    if rv.critical:
        return tr(lang, "no fix")
    return ""


def review_summary(rv: StructureReview, lang: str = "en") -> str:
    """머리 표 / 콘솔용 구조 검토 한 줄: 판정, critical / warning 개수, 수정이 한 일.
    판정이 failed 여도 빌드는 그대로 진행했다는 점을 밝힌다(검토는 빌드 게이트가 아니다).

    One line for the header table / console: verdict, counts, what the fix (edits / script) did."""
    if rv.verdict == "skipped":
        return tr(lang, "skipped ({note})", note=_short(rv.note or "-", 160))
    if rv.verdict == "failed":
        return tr(lang, "failed — no usable answer, the build went ahead ({note})", note=_short(rv.note or "-", 160))
    if not rv.issues:
        return tr(lang, "ok — no issues")
    crit = len(rv.critical)
    txt = tr(lang, "{c} critical, {w} warning(s)", c=crit, w=len(rv.issues) - crit)
    state = _review_script_state(rv, lang)
    return f"{txt}; {state}" if state else txt


def _review_script_lines(rv: StructureReview, lang: str = "en") -> list[str]:
    """구조 검토 섹션의 수정 결과 줄들: 상태·바뀐 파일, 커밋(되돌리면 검토 변경 취소), 되돌림, 오류, 로그."""
    r = rv.script_result
    L: list[str] = []
    if r is not None:
        files = ", ".join(f"`{f}`" for f in r.changed_files[:REVIEW_FILES_SHOWN])
        if len(r.changed_files) > REVIEW_FILES_SHOWN:
            files += f" (+{len(r.changed_files) - REVIEW_FILES_SHOWN})"
        L.append(tr(lang, "- fix (edits / script): `{status}`, {n} file(s) changed{files}", status=r.status,
                    n=len(r.changed_files), files=f": {files}" if files else ""))
        if rv.commit:
            L.append(tr(lang, "- commit: `{commit}` (revert it to undo the review's changes)", commit=rv.commit[:12]))
        if rv.reverted:
            L.append(tr(lang, "- the fix's changes were restored: they are not in the result"))
        if r.error:
            L.append(tr(lang, "- fix error: {error}", error=_cell(r.error, 300)))
        if r.log:
            L.append(tr(lang, "- fix log: {lines}", lines=_cell("; ".join(r.log[:6]), 400)))
    elif _review_fix_pending(rv):
        L.append(tr(lang, "- fix (edits / script): not applied"))
    elif rv.critical:
        L.append(tr(lang, "- no fix for the critical issue(s): they are handed over"))
    if rv.note:
        L.append(tr(lang, "- note: {note}", note=_cell(rv.note, 400)))
    return L


def _review_answer_lines(r: RunReport, rv: StructureReview, lang: str = "en") -> list[str]:
    """v2.1: 구조 검토 답변(해결 방법, 직접 수정 개수, KB 용 스크립트 유무)과 남은 검토 시도가 바꾼 파일.

    v2.1: the review's answer (solution, direct edits, script for the KB) and what its kept tries changed."""
    L: list[str] = []
    a = getattr(rv, "answer", None)
    if a is not None:
        if a.solution:
            L.append(tr(lang, "- solution: {solution}", solution=_cell(a.solution, 600)))
        if a.edits:
            L.append(tr(lang, "- direct edits in the answer: {n}", n=len(a.edits)))
        if a.code and fix_mode_of(r) == EDITS_FIX_MODE:      # v2.1i: the orchestrator did not store it
            L.append(tr(lang, "- script in the answer: not stored (fix.mode edits)"))
        elif a.code:
            L.append(tr(lang, "- script for the KB: yes"))
        elif a.no_script_reason:
            L.append(tr(lang, "- script for the KB: none — {reason}", reason=_cell(a.no_script_reason, 300)))
    changes = review_changes(entries_of(r))
    if changes:
        L.append(tr(lang, "- changed: {files}", files=_cell(stats_text(changes, limit=REVIEW_FILES_SHOWN), 600)))
    return L


def structure_review_lines(r: RunReport, lang: str = "en") -> list[str]:
    """``## Structure review``: 판정, 이슈 표(critical 먼저), 검토가 바꾼 것(edits / 스크립트) 또는 못 바꾼
    이유, LLM 분석. 검토가 없었으면 [].

    `## Structure review`: verdict, issues table, what the review changed (edits / script) or why not; []
    without a review."""
    rv = r.structure_review
    if rv is None:
        return []
    L = [f"## {tr(lang, REVIEW_TITLE)}", "",
         tr(lang, "After the restructure and before the first build, the LLM checked the poms, dependencies and "
                  "module layout against the company rules. It fixes critical issues itself (committed, revertible) "
                  "and leaves a script for the KB; warnings are only recorded."), "",
         tr(lang, "- verdict: `{verdict}` — {summary}", verdict=rv.verdict, summary=review_summary(rv, lang))]
    if rv.model or rv.llm_usage.calls:
        L.append(tr(lang, "- model: `{model}`, {calls} LLM call(s)", model=rv.model or "-", calls=rv.llm_usage.calls))
    if rv.issues:
        issues = sorted(rv.issues, key=lambda i: i.severity != "critical")
        L += ["", tr(lang, "| severity | file | problem | fix hint |"), "|---|---|---|---|"]
        for i in issues[:REVIEW_MAX_ROWS]:
            sev = f"**{tr(lang, 'critical')}**" if i.severity == "critical" else tr(lang, "warning")
            where = f"`{_cell(i.file, 120)}`" if i.file else tr(lang, "(project)")
            L.append(f"| {sev} | {where} | {_cell(i.problem, 400)} | {_cell(i.fix_hint or '-', 300)} |")
        if len(issues) > REVIEW_MAX_ROWS:
            L += ["", tr(lang, "… {n} more issue(s) — see `structure_review` in report.json",
                         n=len(issues) - REVIEW_MAX_ROWS)]
        L.append("")
    L += _review_script_lines(rv, lang)
    L += _review_answer_lines(r, rv, lang)
    analysis = rv.analysis or (rv.answer.analysis if getattr(rv, "answer", None) is not None else "")
    if analysis:
        L += ["", tr(lang, "Analysis: {analysis}", analysis=_cell(analysis, 800))]
    L.append("")
    return L


def _cost_summary(c: Any, lang: str = "en") -> str:
    """머리 표의 LLM 비용 칸: 합계 + 단가 없는 모델 + 토큰 수 누락 표시."""
    from .cost import fmt_money
    txt = fmt_money(c.total, c.currency)
    if c.unpriced_models:
        txt += " " + tr(lang, "(+ unpriced: {models})", models=", ".join(c.unpriced_models))
    if c.usage_missing:
        txt += " " + tr(lang, "(token counts missing for some calls)")
    return txt


def cost_lines(r: RunReport, lang: str = "en") -> list[str]:
    """``## LLM usage and cost``: 모델마다 한 줄(수정 / 에스컬레이션 모델 등), 단가는 ``llm.pricing`` 의 정가.
    회사가 프로젝트당 API 비용을 보고 싶어 해서 생긴 섹션이다(2026-10-22).

    `## LLM usage and cost`: one row per model (fix / escalation / recipe), list prices from `llm.pricing`."""
    from .cost import fmt_money
    c = r.llm_cost
    if c is None or not (c.by_model or r.llm_usage.calls):
        return []
    L = [tr(lang, "## LLM usage and cost"), ""]
    if fix_mode_of(r) == EDITS_FIX_MODE:      # v2.1i: the two experiments of the token comparison told apart
        L += [tr(lang, "fix mode: edits — no fix scripts generated, stored or reused"), ""]
    L.append(tr(lang, "| Model | Calls | Input tokens | Output tokens | Price in / out (per 1M) | Cost |"))
    L.append("|---|---|---|---|---|---|")
    for m in c.by_model:
        price = (f"{fmt_money(m.input_per_mtok, c.currency)} / {fmt_money(m.output_per_mtok, c.currency)}"
                 if m.cost is not None else tr(lang, "no price configured"))
        L.append(f"| `{m.model}` | {m.calls} | {m.input_tokens:,} | {m.output_tokens:,} | {price} | "
                 f"{fmt_money(m.cost, c.currency) if m.cost is not None else '-'} |")
    if not c.by_model:
        L.append(tr(lang, "| (no per-model usage recorded) | {calls} | {inp:,} | {out:,} | - | - |",
                    calls=r.llm_usage.calls, inp=r.llm_usage.input_tokens, out=r.llm_usage.output_tokens))
    L.append(tr(lang, "| **Total** | | | | | **{total}** |", total=fmt_money(c.total, c.currency)))
    L.append("")
    notes = []
    if c.note:
        notes.append(tr(lang, "Prices: {note}.", note=c.note))
    if c.unpriced_models:
        notes.append(tr(lang, "No price for {models} in `llm.pricing` — not included in the total.",
                        models=", ".join(f"`{m}`" for m in c.unpriced_models)))
    if c.usage_missing:
        notes.append(tr(lang, "Some calls came back without token counts (the gateway did not report `usage`); "
                              "their cost is not included."))
    notes.append(tr(lang, "Cost = input tokens × input price + output tokens × output price, per 1M tokens, in "
                          "{currency}; cached-input discounts are not visible to sbmig.", currency=c.currency))
    L += [f"_{n}_" for n in notes]
    L.append("")
    return L


def _to_be(r: RunReport) -> Optional[str]:
    """머리 표의 To-be 칸(java / spring boot / parent 의 이후 값). 인수인계 데이터가 없으면 None."""
    h = r.handover
    if h is None or not (h.java_after or h.spring_boot_after or h.parent_after):
        return None
    parts = []
    if h.java_after:
        parts.append(f"java {h.java_after}")
    if h.spring_boot_after:
        parts.append(f"spring boot {h.spring_boot_after}")
    if h.parent_after:
        parts.append(f"parent `{_cell(h.parent_after, 160)}`")
    return ", ".join(parts)


def jsp_jakarta_lines(r: RunReport, lang: str = "en") -> list[str]:
    """"What we changed" 의 JSP 한 줄 ``JSP javax → jakarta: N files, M references — …``(v2.1h, A5). 재구성이 JSP 의
    javax EE 이름을 하나도 안 바꿨으면 [] (남은 것은 인수인계의 후속 작업 줄이 말한다).

    "What we changed": `JSP javax → jakarta: N files, M references — …` when the restructure renamed any; []
    otherwise (what is left is a Handover follow-up)."""
    files, refs = jsp_jakarta_counts(r)
    if not files:
        return []
    # tr 의 원문은 리터럴이어야 한다(tests/report/test_report_i18n.py 가 번역 표와 맞춰 본다)
    fw = tr(lang, "{n} file", n=files) if files == 1 else tr(lang, "{n} files", n=files)
    rw = tr(lang, "{n} reference", n=refs) if refs == 1 else tr(lang, "{n} references", n=refs)
    what = tr(lang, "JSP javax → jakarta: {files}, {refs}", files=fw, refs=rw)
    return [what + tr(lang, " — renamed by the restructure in JSP / tag files (page imports, useBean, scriptlets); "
                            "OpenRewrite does not parse JSPs"), ""]


def render_markdown(report: RunReport, language: str = "en", cfg: Any = None) -> str:
    """report.md 전체를 ``language``("en" | "ko", 그 밖은 영어)로 그린다. ``cfg`` 는 javax 섹션의 사내 그룹
    판정만 정확하게 해 준다(없어도 된다). 섹션 순서는 모듈 docstring 참고.

    report.md for `language` (`report.language`: "en" | "ko"; anything else renders English). `cfg` (optional)
    only sharpens the javax API section's in-house detection.

    v2.1 (slim): header → Handover → Error list → Fixes by case → Structure review → Blocked → javax API jars →
    Manual follow-ups → Flags → What we changed (v2.1j the intake's pom synthesis, plan steps, OpenRewrite runs,
    v2.1h the JSP javax → jakarta rename) → LLM usage and cost → Notes → Outputs."""
    lang = normalize(language)
    r = report
    L: list[str] = []
    ok = tr(lang, "✅ BUILD SUCCESS") if r.success else tr(lang, "❌ NOT BUILDING")
    L.append(tr(lang, "# Migration report — {name}", name=r.project_name))
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append(tr(lang, "| **Result** | {ok} (final `mvn clean package`) |", ok=ok))
    # 콘솔 마지막 줄과 같은 형식의 SBMIG-RESULT 줄을 보고서 머리에도 둔다(운영자가 보고서만 보고도 옮겨 적게)
    L.append(f"| SBMIG-RESULT | `{report_result_line(r)}` |")
    entries = entries_of(r)
    if entries:
        L.append(tr(lang, "| **Error list** | {n} error(s): {counts} (see \"Error list\") |", n=len(entries),
                    counts=counts_text(status_counts(entries))))
    L.append(tr(lang, "| Case | `{case}` |", case=r.case_type.value))
    if r.profile is not None:
        p = r.profile
        L.append(tr(lang, "| As-is | java {java}, spring boot {boot}, spring {spring}, {poms} pom(s) |",
                    java=p.java_version or '?', boot=p.spring_boot_version or '-', spring=p.spring_version or '-',
                    poms=len(p.poms)))
    to_be = _to_be(r)
    if to_be:
        L.append(tr(lang, "| To-be | {versions} |", versions=to_be))
    L.append(tr(lang, "| Run id | `{run_id}` |", run_id=r.run_id))
    L.append(tr(lang, "| Started / finished | {started} / {finished} ({duration}) |", started=_dt(r.started_at),
                finished=_dt(r.finished_at), duration=_duration(r, lang)))
    u = r.llm_usage
    L.append(tr(lang, "| LLM | {calls} call(s), {inp} in / {out} out tokens{model} |", calls=u.calls,
                inp=u.input_tokens, out=u.output_tokens, model=f" ({u.model})" if u.model else ""))
    c = r.llm_cost
    if c is not None and (c.by_model or u.calls):
        L.append(tr(lang, "| LLM cost | {cost} (see \"LLM usage and cost\") |",
                    cost=_cost_summary(c, lang)))
    if r.flags:
        L.append(tr(lang, "| Flags | {flags} |", flags=", ".join(f"`{f}`" for f in r.flags)))
    if r.blocked:
        L.append(tr(lang, "| **Blocked** | {line} (see below) |", line=blocked_count_line(r.blocked, lang)))
    if r.javax_api_jars:
        L.append(tr(lang, "| javax API jars | {n} |", n=len(r.javax_api_jars)))
    if r.structure_review is not None:
        L.append(tr(lang, "| Structure review | {summary} |", summary=_cell(review_summary(r.structure_review, lang),
                                                                          300)))
    L.append("")

    # 인수인계가 맨 앞: 담당팀은 보고서에서 이 섹션만 읽는다(무엇을 바꿨고 이제 무엇이 그들 몫인지)
    # ---- handover first: what we replaced, what the owning team does from here on
    L += handover_lines(r, lang, cfg)

    # ---- the centre: every error of the run, then how each case was fixed
    L += error_list_lines(r, lang)
    L += fixes_by_case_lines(r, lang)

    # ---- the structure review (ran before the first build; its own entries are in the error list too)
    L += structure_review_lines(r, lang)

    # 차단 = 프로젝트 밖(라이브러리·저장소·환경)이라 일부러 손대지 않은 것. 나머지 마이그레이션은 계속했다
    # ---- blocked: deliberately skipped, each row says what a human / the library owner must do
    if r.blocked:
        L.append(f"## {tr(lang, BLOCKED_TITLE)}")
        L.append("")
        L.append(tr(lang, "{n} cluster(s) were deliberately left unchanged because the fix is outside this project (a "
                          "library, a repository, the environment) or the agent gave up on them. The rest of the "
                          "migration continued.", n=len(r.blocked)))
        L.append("")
        L += blocked_rows(r.blocked, lang)
        L.append("")
        for b in r.blocked:
            if not b.errors:
                continue
            L.append(tr(lang, "<details><summary><code>{id}</code> — {n} error(s)</summary>", id=b.id,
                        n=len(b.errors)))
            L.append("")
            for e in b.errors[:10]:
                loc = f"{e.file}:{e.line}" if e.file and e.line else (e.file or (e.artifact.coordinates if e.artifact
                                                                                  else e.module or "-"))
                L.append(f"- `{_cell(loc, 200)}` {_cell(e.message, 300)}")
            L.append("")
            L.append("</details>")
            L.append("")

    # ---- javax API jars still on the classpath (X1b): the libraries the Blocked rows point at
    L += javax_api_lines(r, lang, cfg)

    if r.manual_followups:
        L.append(tr(lang, "## Manual follow-ups"))
        L.append("")
        L += [f"- {f}" for f in r.manual_followups]
        L.append("")

    if r.flags:
        L.append(tr(lang, "## Flags"))
        L.append("")
        for f in r.flags:
            h = flag_help(f, lang)
            L.append(f"- `{f}`" + (f": {h}" if h else ""))
        L.append("")

    # ---- what we changed (the fixes themselves are in "Fixes by case", the diff in changes.patch)
    titles = {s.id: s.title for s in (r.plan.steps if r.plan else [])}
    executed = [sid for pr in r.phases for sid in pr.step_ids]
    rw = [(pr.phase.value, x) for pr in r.phases for x in pr.rewrite_results]
    jsp = jsp_jakarta_lines(r, lang)
    ant = pom_synthesis_changed_lines(r, lang)          # v2.1j: first, it happened at intake
    if executed or rw or jsp or ant:
        L.append(tr(lang, "## What we changed"))
        L.append("")
    L += ant
    if executed:
        L.append(tr(lang, "Plan steps executed:"))
        L += [f"1. `{sid}` — {titles.get(sid, '')}" for sid in executed]
        L.append("")
    if rw:
        L.append(tr(lang, "OpenRewrite runs:"))
        L.append("")
        L.append(tr(lang, "| phase | recipes | changed files | ok |"))
        L.append("|---|---|---|---|")
        for ph, x in rw:
            L.append(f"| {ph} | {_cell(', '.join(x.recipes), 200)} | {len(x.changed_files)} | "
                     f"{tr(lang, 'yes') if x.ok else tr(lang, 'no: {error}', error=_cell(x.error, 80))} |")
        L.append("")
    L += jsp

    # ---- LLM usage and cost (2026-10-22: the company wants the API cost per project)
    L += cost_lines(r, lang)

    # the JSP rename note is shown in "What we changed" (jsp_jakarta_lines), not again here
    notes = [(pr.phase.value, n) for pr in r.phases for n in pr.notes if not is_jsp_jakarta_note(n)]
    if notes or (r.plan and r.plan.notes):
        L.append(tr(lang, "## Notes"))
        L.append("")
        for n in (r.plan.notes if r.plan else []):
            L.append(tr(lang, "- plan: {note}", note=_cell(n, 400)))
        for ph, n in notes:
            L.append(f"- {ph}: {_cell(n, 400)}")
        L.append("")

    if r.outputs:
        L.append(tr(lang, "## Outputs"))
        L.append("")
        L += [f"- {k}: `{v}`" for k, v in r.outputs.items()]
        L.append("")
    return "\n".join(L).rstrip() + "\n"
