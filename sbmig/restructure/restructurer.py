"""재구성(restructure) 본체 — 회사 레이아웃으로 디렉토리를 옮기고 pom 을 다시 쓴다(ARCHITECTURE §8, 라운드
9c). LLM 없이 결정적으로 동작한다. 흐름상 위치: 변환(OpenRewrite 1회) 다음 ``restructure.*`` 단계에서
오케스트레이터가 ``Restructurer().apply(ws, profile, cfg, mode=..., params=...)`` 를 부르고, 그 뒤가 구조
검토와 검증 빌드다. 모드: ``layout`` = 비 Boot 프로젝트(Spring 4/5, servlet, 순수 Java)를 ``{name}-boot`` /
``{name}-back`` / ``{name}-front`` 로 나눔, ``boot_pom`` = Spring Boot as-is 는 디렉토리를 두고 pom 만,
``msa_pom`` = MSA 서비스 루트마다 pom 만, ``optional_bump`` = 선택 사내 아티팩트 버전만. 회사 규칙(CLAUDE.md):
루트 → 사내 부모 pom ``artifacts.parent`` (aggregator), boot 모듈 → ``boot_parent``, 나머지 모듈 →
``comp_parent``; 필수 사내 아티팩트 2개는 루트 pom 에(``mandatory_target: root``), 선택 사내 아티팩트 8개는
이미 쓰던 것만 올린다. 좌표는 전부 config 에서 읽는다(불변조건 2). 지킬 것: wrap / merge 는 모든 검사·결정을
첫 이동 전에 끝낸다(실패 = ``RestructureError`` → ok=False); 다시 돌려도 결과가 같다(이미 재구성된 프로젝트는
pom 정리만); 프로젝트 밖 경로는 읽지도 바꾸지도 않는다.
v2.1l: 새 부모로 옮기며 버전을 잃은 의존성에는 as-is 빌드의 버전을 적고(``_pin_asis_versions``), 새 부모
체인이 관리하는 것은 관리 버전 정리가 다시 비운다 — LLM 이 버전을 추측하지 않게.
v2.1m: 그 직전에 필터링되는 리소스 아래 바이너리 파일의 확장자를 ``<nonFilteredFileExtensions>`` 에 적는다
(``_non_filtered_extensions`` → resources_filtering.py) — MalformedInputException 을 LLM 에 넘기지 않게.

Company-layout restructuring (ARCHITECTURE §8).

Module paths come from `layout.module_path(which, name)` (round 9c; `{name}` = `layout.project_name` or the root
artifactId): by default `{name}-boot` / `{name}-back` / `{name}-front` directly under the root, pre-9c
`module/boot-jar` + `module/comp-jar` (no front) when configured so.

mode="layout"   non-MSA: root becomes an aggregator (parent = in-house parent, packaging pom, mandatory deps per
                `artifacts.mandatory_target`) with the back module (all as-is sources + deps), the boot module
                (Application, boot resources, spring-boot-maven-plugin repackage) and — when the as-is project has web
                resources — the front module (static files, JSPs; JSPs make the boot module a WAR for external
                Tomcat). As-is multi-module projects are merged into the back module (default; an as-is module named
                like the boot module is kept, first-wins collisions) or wrapped (`wrap`).
mode="boot_pom" Spring Boot as-is (round 9c): directories kept, poms only — root → in-house parent, boot modules →
                `boot_parent`, other modules → `comp_parent`; a single-module Boot app is wrapped into `{name}-boot`.
mode="msa_pom"  MSA: pom-only edits (parent / mandatory / optional / BOM / java props) per service root; no moves.
                `params["library_roots"]` (pom paths or directories) and the library entries of `profile.services`
                (see `sbmig.analyze.is_library_root`) get parent swap + optional bumps + remove_when_present but no
                mandatory dependencies. Also used for pom-only projects (no sources, S3).
mode="optional_bump"
                only the optional-artifact bumps (aliases → canonical rename, property indirection, `<artifactId>.version`
                convention) and `remove_when_present` on every project pom (or, with `params["target"]`, the poms below
                that directory plus their in-repo parents); no moves, no parent change. Idempotent: a later `layout` /
                `msa_pom` run finds nothing left to bump and reports nothing.

Optional artifact bumps and `remove_when_present` are applied to every project pom in all modes.

v2.1l (`layout`, `boot_pom`, `msa_pom`): right before the managed-version cleanup, every version-less
`<dependencies>` entry gets the version the as-is build resolved (`params["asis_effective"]`, the intake
capture); the cleanup then empties the pins the new parent chain manages (docs/INTERFACES.md "v2.1l").

v2.1m (`layout`, `boot_pom`, `msa_pom`): right before the as-is version carry-over, the binary files under a
filtered resource get their extensions into maven-resources-plugin (maven-war-plugin for filtered
`<webResources>`) `<nonFilteredFileExtensions>` (`resources_filtering.py`, docs/INTERFACES.md "v2.1m").

Edge cases (docs/ERROR-HANDLING.md): system-scoped jars are copied next to the module that declares them (E7);
`<module>` entries outside the project root abort a `wrap` layout with ok=False before anything moves and are skipped
(note + follow-up) by `merge` (S5); nested
`<modules>` / `<parent><relativePath>` are recomputed after a wrap; WAR modules are wrapped unchanged (S7); an
already restructured project is left alone apart from the idempotent pom pass (S2).


"""
from __future__ import annotations

import fnmatch
import logging
import posixpath
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import jinja2

from sbmig.analyze.source_scanner import (
    DEFAULT_WEBAPP,
    WEB_STATIC_EXTENSIONS,
    clean_pom_dir,
    pom_build_dirs,
    pom_web_dirs,
    strip_comments,
)
from sbmig.core.config import Config
from sbmig.core.interfaces import RestructureResult
from sbmig.core.models import GAV, CaseType, MergeConflict, PluginInfo, ProjectProfile
from sbmig.core.workspace import GitError, Workspace, is_build_output_path
from sbmig.plan.versions import compare_versions, version_lt

from .jsp_jakarta import rename_jsp_files
from .pom_editor import PomEditError, PomEditor, property_ref
from .webxml_registrations import CLASS_NAME as WEB_XML_CLASS
from .webxml_registrations import RULES_FILE as WEB_XML_RULES
from .webxml_registrations import ClassInfo, load_skip_rules, plan_registrations

log = logging.getLogger("sbmig.restructure")
# v2.1l: versions_pinned 의 저장소 출처 표시(as-is 빌드가 버전을 주지 못한 의존성)
REPO_PIN_SOURCE = "repository newest release (the as-is build did not resolve it)"

BOOT_GROUP = "org.springframework.boot"
BOOT_PLUGIN = (BOOT_GROUP, "spring-boot-maven-plugin")
WAR_PLUGIN = ("org.apache.maven.plugins", "maven-war-plugin")
COMPILER_PLUGIN = ("org.apache.maven.plugins", "maven-compiler-plugin")
JAR_PLUGIN = ("org.apache.maven.plugins", "maven-jar-plugin")
EAR_PLUGIN = ("org.apache.maven.plugins", "maven-ear-plugin")   # never copied into a generated pom (2026-10-23)
JAVA_PROPS = ("maven.compiler.source", "maven.compiler.target", "maven.compiler.release", "java.version")
SERVLET_COMPONENT_SCAN_FQCN = "org.springframework.boot.web.server.servlet.context.ServletComponentScan"  # verified in spring-boot-web-server-4.1.0.jar
FALLBACK_PACKAGE = "com.company.app"
BUILD_TO_COMP = ("sourceDirectory", "scriptSourceDirectory", "testSourceDirectory", "resources", "testResources",
                 "filters")

PLACEHOLDER_BEGIN = "# --- sbmig externalized-config"
PLACEHOLDER_END = "# --- end sbmig externalized-config"
PLACEHOLDER_HEADER = (
    PLACEHOLDER_BEGIN + ": keys the code reads that no packaged resource defined.",
    "# --- Before the migration they came from the container (JNDI, web.xml context-param, -D, catalina.properties).",
    "# --- Values are placeholders: supply each one via environment (db.url → DB_URL), an external "
    "application.properties or -D.",
)
PLACEHOLDER_SOURCE_LABELS = {
    "value-annotation": "@Value", "configuration-properties": "@ConfigurationProperties",
    "environment": "Environment.getProperty", "system-property": "System.getProperty", "system-env": "System.getenv",
    "xml-placeholder": "XML placeholder", "context-param": "web.xml context-param", "jndi": "JNDI",
    "property-source": "@PropertySource", "profiles": "spring.profiles",
    "config-placeholder": "config placeholder",       # round 9: `${K}` inside a packaged definition file's value
}

WEB_LAYER_RE = re.compile(
    r"^\s*@(?:org\.springframework\.\S+\.)?(?:Controller|RestController|ControllerAdvice|RestControllerAdvice|"
    r"Configuration)\b", re.MULTILINE)
SERVLET_INIT_RE = re.compile(r"\bextends\s+(?:[\w.]+\.)?SpringBootServletInitializer\b")
PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
# merge: line-list registries whose copies are merged as the union of lines instead of first-wins (round 9)
UNION_RE = re.compile(r"(?:^|/)META-INF/(?:services/[^/]+|spring\.factories|spring/[^/]+\.(?:imports|factories))$")
RESOURCE_SECTIONS = {"resources": ("resource", "src/main/resources"),
                     "testResources": ("testResource", "src/test/resources")}

# round 9c: front module / JSP WAR
FRONT_ARTIFACT_SUFFIX = "-front"
WEBINF_STASH = "asis-webinf"            # non-JSP WEB-INF files (web.xml, XML configs) under the boot module resources
# 2026-10-23 (sibling poms + EAR): the files of a dropped EAR module (application-server descriptors) under the boot
# module resources, `asis-ear/<module dir basename>/<module-relative path>` (not read by Spring Boot)
EAR_STASH = "asis-ear"
# root pom property of the intake aggregator (sbmig.analyze.aggregator): the project name when the aggregator could
# not take it as its artifactId (a member had that g:a, e.g. the EAR) — `{name}` and, after the merge, the root
# artifactId
NAME_PROP = "sbmig.project-name"
MAIN_METHOD_RE = re.compile(r"\b(?:public\s+static|static\s+public)\s+(?:final\s+)?void\s+main\s*\(\s*"
                            r"(?:final\s+)?String\b")
BOOT_APP_RE = re.compile(r"@(?:org\.springframework\.boot\.autoconfigure\.)?SpringBootApplication\b")
APPLICATION_XML_WEB_RE = re.compile(
    r"<(?:\w+:)?web-uri>\s*([^<]+?)\s*</(?:\w+:)?web-uri>\s*<(?:\w+:)?context-root>\s*([^<]*?)\s*"
    r"</(?:\w+:)?context-root>")
JSP_EXTS = (".jsp", ".jspf", ".jspx")
# 2026-10-28: WEB-INF files that are configuration (web.xml, Spring XML, properties …) go to the boot stash; every
# other WEB-INF file (views, html / js / css fragments, templates, downloads) keeps its path under the front jar's
# META-INF/resources/WEB-INF/ — reachable by jsp:include / RequestDispatcher as before, never served directly
WEBINF_CONFIG_EXTS = (".xml", ".properties", ".yml", ".yaml", ".conf", ".ini", ".cfg")
WEB_RESOURCE_DIRS = ("static", "public", "templates")   # src/main/resources/<dir>/** → the front module, same path
JSTL_URI_RE = re.compile(r"""<%@\s*taglib\b[^%]*\buri\s*=\s*["'](?:https?://java\.sun\.com/jsp/jstl/|"""
                         r"""https?://xmlns\.jcp\.org/jsp/jstl/|jakarta\.tags\.)""", re.IGNORECASE)
# verified in spring-boot-dependencies-4.1.0.pom (managed: jakarta-servlet-jsp-jstl.version 3.0.2,
# glassfish-jstl.version 3.0.1, tomcat-embed-jasper ${tomcat.version}, spring-boot-starter-tomcat 4.1.0)
JSTL_DEPS = [GAV(group_id="jakarta.servlet.jsp.jstl", artifact_id="jakarta.servlet.jsp.jstl-api"),
             GAV(group_id="org.glassfish.web", artifact_id="jakarta.servlet.jsp.jstl")]
VIEW_RESOLVER = "InternalResourceViewResolver"

JAVA_KEYWORDS = {
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char", "class", "const", "continue",
    "default", "do", "double", "else", "enum", "extends", "final", "finally", "float", "for", "goto", "if",
    "implements", "import", "instanceof", "int", "interface", "long", "native", "new", "package", "private",
    "protected", "public", "return", "short", "static", "strictfp", "super", "switch", "synchronized", "this",
    "throw", "throws", "transient", "try", "void", "volatile", "while", "true", "false", "null", "var", "record",
    "yield", "sealed", "permits", "_",
}

_TEMPLATES = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=lambda name: bool(name and name.endswith(".xml.j2")),
    keep_trailing_newline=True,
    undefined=jinja2.StrictUndefined,
)


class RestructureError(Exception):
    """재구성을 멈춰야 하는 오류(루트 pom 없음, 프로젝트 밖 모듈의 wrap, ``merge_collision: fail`` 충돌,
    덮어쓰기가 필요한 이동 ...). ``apply`` 가 잡아 ok=False + error 로 돌려준다."""


def render(template: str, **kw: Any) -> str:
    """``templates/`` 의 Jinja2 템플릿 렌더링(boot / comp / front pom, Application, WebXmlRegistrations).
    StrictUndefined 라 빠진 변수는 바로 오류가 난다."""
    return _TEMPLATES.get_template(template).render(**kw)


def sanitize_identifier(name: str, fallback: str = "x") -> str:
    """임의 문자열 → Java 식별자(허용 밖 문자는 _, 숫자로 시작하면 앞에 _, 예약어면 뒤에 _).
    생성 클래스 이름에 쓴다."""
    out = re.sub(r"[^A-Za-z0-9_$]", "_", name or "")
    if not out:
        out = fallback
    if out[0].isdigit():
        out = "_" + out
    if out in JAVA_KEYWORDS:
        out = out + "_"
    return out


def sanitize_package(pkg: str | None) -> str | None:
    """패키지 이름의 마디마다 Java 식별자로 정리(``com.my-co.app`` → ``com.my_co.app``); 비면 None."""
    if not pkg:
        return None
    parts = [sanitize_identifier(p) for p in pkg.strip().strip(".").split(".") if p]
    return ".".join(parts) or None


@dataclass
class _Ctx:
    """``apply`` 한 번 동안 들고 다니는 작업 상태: 워크스페이스·프로필·설정·결과, 바뀐 파일(``changed``),
    이동 기록(``moves``), 모듈 경로(``boot_rel`` / ``comp_rel`` / ``front_rel``), front / WAR / JSP /
    overlay 판단, 이름 변경. 메서드는 이동·복사·쓰기·노트·커밋을 한곳에서 기록한다."""
    ws: Workspace
    profile: ProjectProfile
    cfg: Config
    result: RestructureResult
    params: dict[str, Any]
    commit_enabled: bool = True
    base: str = ""
    changed: set[str] = field(default_factory=set)
    moves: list[tuple[str, str]] = field(default_factory=list)
    # round 9c: `{name}` of the module names and the root-pom-relative module paths (`layout.module_path`)
    name: str = ""
    boot_rel: str = ""
    comp_rel: str = ""
    front_rel: str | None = None
    front_made: bool = False            # a front module was created (web resources found)
    boot_war: bool = False              # JSPs in the front module → the boot module is a WAR for external Tomcat
    jstl: bool = False                  # a JSP declares a JSTL taglib
    jsp_moved: int = 0
    web_xml_class: str | None = None    # FQCN of the generated WebXmlRegistrations (2026-10-23)
    web_xml_summary: str | None = None
    # 2026-10-23 (sibling poms + EAR, the intake aggregator)
    root_aid_old: str | None = None     # root artifactId before the `sbmig.project-name` rename
    rename_root: str | None = None      # the root artifactId after the merge (`sbmig.project-name`)
    # base-relative directory a merged module referenced outside itself (`${basedir}/../../STATIC`) → comp-relative
    relocated: dict[str, str] = field(default_factory=dict)
    war_overlays: list[tuple[str, GAV]] = field(default_factory=list)   # (as-is pom, external <type>war</type> dep)
    overlay_conf: Any = None            # the as-is maven-war-plugin <configuration><overlays> element
    overlay_carry: bool = False         # the overlays go into the boot WAR
    module_mains: set[str] = field(default_factory=set)    # main classes reported per merged jar module
    # v2.1l: 이번 패스에서 버전을 적은 의존성 (pom, g:a) → (버전, versions_pinned 줄, 출처 "asis" |
    # "repository"). 바로 뒤의 _drop_managed_versions 가 새 부모 체인이 관리하는 것은 다시 비우고, 새 체인을
    # 모르면 전부 되돌린다. asis_unknown = (pom, g:a) neither the as-is build nor the repository resolved;
    # asis_keys = what the as-is project declared; asis_before = pom → (bytes before the pins, changed before)
    asis_pins: dict[tuple[str, str], tuple[str, str, str]] = field(default_factory=dict)
    asis_unknown: list[tuple[str, str]] = field(default_factory=list)
    asis_keys: set[str] = field(default_factory=set)
    asis_before: dict[str, tuple[bytes, bool]] = field(default_factory=dict)
    # the restructure's RepositoryLookup, made on first use (None after a failed construction)
    repo_lookup: Any = None
    repo_lookup_made: bool = False
    # v2.1o: Boot BOM 의 사실(재구성 한 번에 한 번 읽음)과 관리 버전을 정한 출처
    # ("effective pom" | "Boot BOM facts" | "none")
    bom_facts: Any = None
    bom_facts_loaded: bool = False
    # v2.1p: 사내 부모 체인의 사실(재구성 한 번에 한 번 읽음)
    chain_facts: Any = None
    chain_facts_loaded: bool = False
    managed_source: str = ""
    # v2.1o: the as-is intake capture (`params["asis_effective"]()`), read once per restructure; None = unusable
    asis_data: Any = None
    asis_data_loaded: bool = False

    def p(self, *parts: str) -> str:
        """프로젝트 루트(``base``) 기준 경로 → 워크스페이스 기준 정규화 경로(루트 자신은 ``.``)."""
        j = posixpath.normpath(posixpath.join(self.base, *parts)) if (self.base or parts) else "."
        return "." if j in ("", ".") else j

    def new_location(self, old: str) -> str:
        """as-is 경로가 지금 어디 있는지: 이동 기록을 순서대로 적용한다(comp 로 갔다가 front / boot 로
        다시 간 파일은 마지막 위치).

        Where an as-is path is now: the moves applied in order (a file moved twice — into comp, then into the
        front / boot module — resolves to its last location)."""
        cur = old
        for o, n in self.moves:
            if cur == o or cur.startswith(o + "/"):
                cur = n + cur[len(o):]
        return cur

    def move(self, src: str, dst: str) -> int:
        """파일 / 디렉토리 이동 + 기록(moves, changed, moved_files). 대상 디렉토리가 이미 있으면
        파일 단위로 합치되 절대 덮어쓰지 않는다(같은 파일이 있으면 RestructureError) — 사람
        파일을 조용히 잃지 않게."""
        absrc = self.ws.abs(src)
        if not absrc.exists():
            return 0
        if self.ws.abs(dst).exists() and absrc.is_dir():
            # merge into an existing directory file by file (never overwrite)
            n = 0
            for f in sorted(x for x in absrc.rglob("*") if x.is_file()):
                rel = f.relative_to(absrc).as_posix()
                target = f"{dst}/{rel}"
                if self.ws.exists(target):
                    raise RestructureError(f"cannot move {src}/{rel}: {target} already exists")
                n += self.move(f"{src}/{rel}", target)
            self.ws.delete(src)
            self.moves.append((src, dst))
            return n
        files = [absrc] if absrc.is_file() else [x for x in absrc.rglob("*") if x.is_file()]
        rels = [dst if absrc.is_file() else f"{dst}/{f.relative_to(absrc).as_posix()}" for f in files]
        self.ws.move(src, dst)
        self.moves.append((src, dst))
        self.changed.update(rels)
        self.result.moved_files += len(rels)
        return len(rels)

    def copy(self, src: str, dst: str) -> int:
        """파일 하나를 바이트 그대로 복사(v2.1g: web root 안 <resource> 디렉토리 파일을 front static 에도 두고
        classpath 원본은 남긴다). 원본이 파일이 아니면 0, 대상이 있으면 덮어쓰지 않고 RestructureError.

        Copy ONE file byte for byte (2026-10-28: a <resource> dir file inside the web root reaches the front
        module's static dir while its classpath copy stays). 0 when `src` is not a file; never overwrites."""
        absrc, abdst = self.ws.abs(src), self.ws.abs(dst)
        if not absrc.is_file():
            return 0
        if abdst.exists():
            raise RestructureError(f"cannot copy {src}: {dst} already exists")
        abdst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(absrc, abdst)
        self.changed.add(dst)
        self.result.moved_files += 1
        return 1

    def write(self, rel: str, text: str, generated: bool = True) -> None:
        """파일 쓰기 + 변경 기록. ``generated`` 면 결과의 generated_files 에도 올린다(보고서 / 구조 검토가
        '새로 만든 파일' 로 본다)."""
        self.ws.write(rel, text)
        self.changed.add(rel)
        if generated and rel not in self.result.generated_files:
            self.result.generated_files.append(rel)

    def save(self, ed: PomEditor) -> bool:
        """pom 이 실제로 바뀌었을 때만 저장하고 기록한다 — 바뀌지 않은 pom 은 diff 에 나오지 않는다."""
        if not ed.changed:
            return False
        ed.save()
        self.changed.add(self.ws.rel(ed.path))
        return True

    def note(self, text: str) -> None:
        """노트 한 줄(보고서에 남는 사실, 중복은 한 번만)."""
        if text not in self.result.notes:
            self.result.notes.append(text)

    def followup(self, text: str) -> None:
        """후속 작업 한 줄(as-is 담당자가 할 일, 보고서 Handover; 중복은 한 번만)."""
        if text not in self.result.manual_followups:
            self.result.manual_followups.append(text)

    def commit(self, message: str) -> None:
        """중간 git 커밋(``restructure: ...``). ``commit=False`` 이거나 git 이 없으면 건너뛴다 — 커밋
        실패로 재구성을 멈추지 않는다."""
        if not self.commit_enabled:
            return
        try:
            self.ws.commit(message)
        except (GitError, OSError) as e:  # pragma: no cover - git missing / misconfigured
            log.warning("restructure commit skipped: %s", e)


class Restructurer:
    """RestructurerLike 구현. 상태를 갖지 않고 ``apply`` 마다 ``_Ctx`` 를 새로 만든다.
    ``Restructurer(commit=False)`` 는 중간 git 커밋을 하지 않는다.

    Implements RestructurerLike. `Restructurer(commit=False)` skips intermediate git commits."""

    def __init__(self, commit: bool = True):
        self.commit_enabled = commit

    # ================================================================== entry point
    def apply(self, ws: Workspace, profile: ProjectProfile, cfg: Config, *,
              mode: Literal["layout", "msa_pom", "optional_bump", "boot_pom"] = "layout",
              params: dict[str, Any] | None = None) -> RestructureResult:
        """재구성 진입점: ``mode`` 에 따라 ``_layout`` / ``_msa`` / ``_optional_bump`` / ``_boot_pom`` 을
        부르고 RestructureResult(노트, 후속 작업, 바뀐 파일, 부모 변경, 올린 버전 ...)를 돌려준다.
        RestructureError / PomEditError / OSError 는 예외 대신 ok=False + error. ``params`` = 계획
        단계(PlanStep)의 값 + 오케스트레이터가 넣는 ``effective_pom`` / ``asis_effective`` (v2.1l).

        params (optional, from the PlanStep): starter, split_policy, multi_module_policy, merge_collision (layout);
        library_roots (msa_pom); target (optional_bump: restrict to the poms below this directory); from the
        orchestrator: effective_pom (the tree after the pom edits) and asis_effective (v2.1l: the as-is intake
        capture), zero-arg callables."""
        result = RestructureResult(ok=True)
        c = _Ctx(ws, profile, cfg, result, dict(params or {}), self.commit_enabled)
        try:
            if mode == "layout":
                self._layout(c)
            elif mode == "msa_pom":
                self._msa(c)
            elif mode == "optional_bump":
                self._optional_bump(c)
            elif mode == "boot_pom":
                self._boot_pom(c)
            else:
                raise RestructureError(f"unknown restructure mode: {mode}")
        except (RestructureError, PomEditError, OSError) as e:
            log.error("restructure failed: %s", e)
            result.ok = False
            result.error = str(e)
        # v2.1h: 모든 모드 공통 — 파일이 최종 위치에 간 뒤 JSP 의 javax 이름을 jakarta 로(레시피는 JSP 를 못 본다).
        # 실패한 재구성(ok=False)에는 손대지 않는다: "아무것도 바꾸지 않았다" 는 오류 메시지가 사실로 남게
        self._jsp_jakarta(c)
        result.changed_files = sorted(c.changed)
        return result

    # ================================================================== v2.1h: JSP javax → jakarta
    def _jsp_jakarta(self, c: _Ctx) -> None:
        """``layout.jsp_jakarta_rename`` 이면 워크스페이스의 모든 JSP / 태그 파일 코드 영역에서 javax EE 이름을
        jakarta 로 바꾸고(``jsp_jakarta.rename_jsp_files``) 결과의 ``jsp_jakarta_*``, 바뀐 파일, 노트 한 줄을
        채운 뒤 따로 커밋한다. 꺼져 있으면 분석기가 센 javax JSP 수로 후속 작업 한 줄만. 파일 하나의 실패는
        경고 + 노트로 남기고 재구성을 멈추지 않는다.

        Every mode, after the mode's work put the files in place (v2.1h, A5): rename the javax EE names inside the
        JSP / tag files' code regions (`layout.jsp_jakarta_rename`), fill `result.jsp_jakarta_*`, add the changed
        files and one note, commit. Off → a follow-up when the analyzer counted javax JSPs. Skipped when the mode
        failed (ok=False)."""
        res = c.result
        if not getattr(c.cfg.layout, "jsp_jakarta_rename", True):
            src = getattr(c.profile, "source", None)
            n = int(getattr(src, "jsp_javax_files", 0) or 0)
            if n:
                c.followup(f"JSP javax references not renamed (layout.jsp_jakarta_rename: false): {n} files")
            return
        if not res.ok:
            return
        try:
            out = rename_jsp_files(c.ws)
        except Exception as e:  # noqa: BLE001 - the rename is an add-on: a walk failure never fails the restructure
            log.warning("JSP javax → jakarta skipped: %s: %s", type(e).__name__, e)
            c.note(f"JSP javax → jakarta skipped: {type(e).__name__}: {e}")
            return
        if out.renamed:
            res.jsp_jakarta_files += out.files
            res.jsp_jakarta_refs += out.refs
            res.jsp_jakarta_renamed += [f"{rel}: {n}" for rel, n in out.renamed.items()]
            c.changed.update(out.renamed)
            c.note(f"JSP javax → jakarta: {out.files} files, {out.refs} references")
            log.info("JSP javax → jakarta: %d file(s), %d reference(s) of %d JSP / tag file(s)",
                     out.files, out.refs, out.scanned)
            c.commit("restructure: JSP javax -> jakarta")
        if out.failed:
            names = sorted(out.failed)
            c.note(f"JSP javax → jakarta: {len(names)} file(s) not renamed ({', '.join(names[:3])}"
                   f"{' …' if len(names) > 3 else ''}; see sbmig.log)")

    # ================================================================== layout
    def _layout(self, c: _Ctx) -> None:
        """mode ``layout`` (비 Boot 프로젝트의 회사 레이아웃). 이미 재구성된 프로젝트면 사내 부모
        버전 맞춤과 pom 정리만 하고 끝. 아니면 프로젝트 밖 모듈 검사 → 단일 모듈 ``_single`` / 다중 모듈
        ``_merge`` (기본) 또는 ``_wrap`` → 루트 이름 변경 반영, 외부 설정 placeholder, 역할 부모 정렬, WEB-INF
        stash 중복 제거, 남은 웹 콘텐츠 후속 작업, 모든 pom 정리(선택 사내 아티팩트,
        부모가 관리하는 버전 삭제)."""
        L = c.cfg.layout
        root_rel = c.profile.root_pom.path if c.profile.root_pom else "pom.xml"
        c.base = posixpath.dirname(root_rel)
        if not c.ws.exists(root_rel):
            raise RestructureError(f"root pom not found: {root_rel}")
        root = PomEditor(c.ws.abs(root_rel))
        self._resolve_names(c, root)
        comp_mod = c.comp_rel
        boot_mod = c.boot_rel
        mods = [m.strip().rstrip("/") for m in root.modules()]
        policy = c.params.get("multi_module_policy", L.multi_module_policy)
        norm_mods = [posixpath.normpath(m) for m in mods]
        # 이미 재구성됐는가: merge 는 boot / comp(back) 모듈이
        # 둘 다 루트 <modules> 에 있어야 회사 레이아웃이다
        if policy == "merge":     # the company layout is exactly comp-jar + boot-jar
            done = boot_mod in norm_mods and comp_mod in norm_mods
        else:
            done = boot_mod in norm_mods and (
                comp_mod in norm_mods or not L.module_dir.strip("/")
                or all(m.startswith(L.module_dir + "/") for m in norm_mods))
        if root.packaging() == "pom" and done:
            c.note("already restructured")
            self._record_modules(c, existing=True)
            parent = root.get_parent()
            if parent is None or parent.key != c.cfg.artifacts.parent.key:
                c.note(f"already restructured but {root_rel} does not inherit {c.cfg.artifacts.parent.key}; "
                       "parent left unchanged")
            else:
                want = c.cfg.artifacts.parent.version
                # 더 낮은 사내 부모 버전으로 이미 옮긴 프로젝트:
                # 설정된 버전으로만 올린다(구조는 건드리지 않음)
                if want and parent.version != want:   # migrated on an older in-house parent → the configured one
                    root.set_parent(c.cfg.artifacts.parent.gav(), root.parent_relative_path())
                    c.save(root)
                    self._record_parent(c, root_rel, parent, c.cfg.artifacts.parent.gav())
                    c.note(f"in-house parent {parent.key} {parent.version or '?'} → {want} (artifacts.parent)")
            self._align_role_parents(c, report_modules=True)
            self._all_poms_pass(c, java_fix=False)
            self._non_filtered_extensions(c)
            self._pin_asis_versions(c)
            self._drop_managed_versions(c)
            self._drop_bom_property_overrides(c)
            self._write_placeholders(c, f"{c.p(boot_mod)}/src/main/resources")
            self._placeholder_followup(c)
            c.commit("restructure: optional artifact versions / cleanup (already restructured)")
            return
        parent = root.get_parent()
        if parent is not None and parent.key == c.cfg.artifacts.parent.key:
            c.note(f"{root_rel} already inherits the in-house parent {parent.key}; splitting into the company layout")
        outside = []
        inside: list[str] = []
        for m in mods:
            tgt = posixpath.normpath(posixpath.join(c.base or ".", m))
            if m.startswith("/") or tgt == ".." or tgt.startswith("../"):
                outside.append(f"'{m}' → {tgt}")
                if policy == "merge":                  # merge: skip it (note + follow-up), fold the rest
                    self._skip_outside(c, root_rel, m, tgt)
                    continue
            inside.append(m)
        # wrap 은 프로젝트 밖 모듈을 옮길 수 없다: 아무것도 옮기기 전에 멈춘다(반쯤 옮긴 상태를 남기지 않음)
        if outside and policy != "merge":
            msg = (f"module(s) declared in {root_rel} resolve outside the project root: {', '.join(outside)}; the "
                   "layout restructure cannot move them — nothing was changed (move the module into the project or "
                   "migrate it separately)")
            c.note(msg)
            raise RestructureError(msg)
        coords = self._root_coords(root, c.profile)
        if policy == "merge" or not mods:
            coords = self._project_name_coords(c, root, coords)
        if mods:
            if policy == "merge":
                self._merge(c, root, inside, coords)
            else:
                self._wrap(c, root, mods, coords)
        else:
            self._single(c, root, coords)
        self._repoint_renamed_root(c)
        # 외부 설정 placeholder 는 boot 모듈에: 런타임 설정(application.properties)은 boot jar 가 읽는다
        # single / merge (generated or kept boot module) / wrap: the boot-jar holds the runtime configuration
        self._write_placeholders(c, f"{c.p(c.boot_rel)}/src/main/resources")
        self._placeholder_followup(c)
        self._align_role_parents(c)
        if self._role(c) and c.result.parents_set:
            front = (f", {c.front_rel} {c.cfg.artifacts.comp_parent.key}"                    # type: ignore[union-attr]
                     if c.front_made and c.front_rel else "")
            where = ("mandatory dependencies in the root pom; BOM / java level / root management in each module pom"
                     if self._mandatory_in_root(c) else
                     "mandatory dependencies / BOM / java level / root management are in each module pom")
            c.note(f"role parents: {len(c.result.parents_set)} parent(s) set — root {c.cfg.artifacts.parent.key}, "
                   f"{c.boot_rel} {c.cfg.artifacts.boot_parent.key}, "                         # type: ignore[union-attr]
                   f"{c.comp_rel} {c.cfg.artifacts.comp_parent.key}{front} "                   # type: ignore[union-attr]
                   f"(the modules do not inherit the root pom; {where})")
        self._record_modules(c)
        self._dedupe_webinf_stash(c)
        self._large_media_note(c)
        self._web_content_left(c)
        self._all_poms_pass(c, java_fix=False)
        self._non_filtered_extensions(c)
        self._pin_asis_versions(c)
        self._drop_managed_versions(c)
        self._drop_bom_property_overrides(c)
        c.commit("restructure: optional artifact versions / cleanup")

    # ------------------------------------------------------------------ project name (2026-10-23)
    def _project_name_coords(self, c: _Ctx, root: PomEditor, coords: GAV) -> GAV:
        """intake aggregator 가 붙인 ``sbmig.project-name`` (멤버, 보통 EAR 이 이미 ``<group>:<name>`` 을 써서
        aggregator 는 ``<name>-aggregator`` 가 됨)을 merge 뒤 루트 artifactId 로 쓴다 — 멤버가 합쳐지거나 빠져
        reactor 충돌이 없어지므로. 생성 모듈 이름(``<name>-back`` ...)도 따라간다. 남길 boot 모듈이 그
        artifactId 면 적용하지 않는다.

        The intake aggregator's `sbmig.project-name` (it took `<name>-aggregator` because a member, typically the
        EAR, had the g:a `<group>:<name>`) becomes the root artifactId once the members are merged / dropped (no reactor
        collision left), and the generated module artifactIds follow it (`<name>-back`, …). Not applied when a kept
        boot module has that artifactId."""
        name = (root.get_property(NAME_PROP) or "").strip()
        if not name or name == coords.artifact_id:
            return coords
        boot_name = c.cfg.layout.module_name("boot", c.name)
        root_rel = c.p("pom.xml")
        for pom in self._project_poms(c):
            if pom == root_rel or posixpath.basename(posixpath.dirname(pom)) != boot_name:
                continue
            try:
                ed = PomEditor(c.ws.abs(pom))
            except PomEditError:
                continue
            if ed.artifact_id() == name and (ed.effective_group_id() or coords.group_id) == coords.group_id:
                c.note(f"{NAME_PROP}={name} not applied to the root pom: the kept boot module {pom} has that "
                       "artifactId")
                return coords
        c.root_aid_old, c.rename_root = coords.artifact_id, name
        return GAV(group_id=coords.group_id, artifact_id=name, version=coords.version)

    def _repoint_renamed_root(self, c: _Ctx) -> None:
        """루트 이름을 바꾼 뒤(``_rewrite_root``), 옛 루트 artifactId 를 <parent> 로 가리키던 프로젝트
        pom(남겨 둔 as-is 모듈)을 새 이름으로 고친다. relativePath / version 은 그대로.

        After the root rename (`_rewrite_root`): every project pom whose `<parent>` still names the old root
        artifactId (a kept as-is module) points at the new one; relativePath / version untouched."""
        if not c.rename_root or not c.root_aid_old:
            return
        root_rel = c.p("pom.xml")
        try:
            group = PomEditor(c.ws.abs(root_rel)).effective_group_id()
        except PomEditError:
            return
        fixed: list[str] = []
        for pom in self._project_poms(c):
            if pom == root_rel:
                continue
            try:
                ed = PomEditor(c.ws.abs(pom))
            except PomEditError:
                continue
            parent = ed.get_parent()
            if parent is None or parent.artifact_id != c.root_aid_old or (parent.group_id or group) != group:
                continue
            ed.set_child_text(ed.child(ed.root, "parent"), "artifactId", c.rename_root)
            c.save(ed)
            fixed.append(pom)
        if fixed:
            c.note(f"<parent> of {', '.join(fixed)} re-pointed to the renamed root pom ({c.root_aid_old} → "
                   f"{c.rename_root})")

    # ------------------------------------------------------------------ web content no pom references (2026-10-23)
    def _web_content_left(self, c: _Ctx) -> None:
        """어느 pom 도 참조하지 않아 옮겨지지 않은 JSP(예: 모듈 디렉토리 옆 ``STATIC/index.jsp``)는 그 자리에
        둔다: 최상위 디렉토리마다 후속 작업 하나 + 플래그 ``web-content-left``. 어디서 서비스되는지 모르는
        파일을 추측으로 옮기지 않는다.

        JSPs of the analysis that did not move and lie in no generated module (e.g. `STATIC/index.jsp` next to the
        module directories, referenced by no pom) stay where they are: one follow-up per top directory, flag
        `web-content-left`. Nothing is moved."""
        src = c.profile.source
        mods = [c.p(m) for m in (c.comp_rel, c.boot_rel, c.front_rel) if m]
        left: dict[str, list[str]] = {}
        for p in src.jsp_paths:
            if c.new_location(p) != p or not c.ws.exists(p):
                continue
            if any(p == m or p.startswith(m + "/") for m in mods):
                continue
            rel = p[len(c.base) + 1:] if c.base and p.startswith(c.base + "/") else p
            if rel.startswith("../"):
                continue
            left.setdefault(rel.split("/")[0] if "/" in rel else ".", []).append(p)
        if not left:
            return
        dest = c.front_rel or c.comp_rel
        for top, jsps in sorted(left.items()):
            if top == ".":
                where, others = "the project root", None
            else:
                where = f"{c.p(top)}/"
                base = c.ws.abs(c.p(top))
                names = set(jsps)
                others = sum(1 for f in base.rglob("*") if f.is_file() and ".git" not in f.parts
                             and not is_build_output_path(c.ws.rel(f), c.ws.root) and c.ws.rel(f) not in names)
            n = len(jsps)
            counts = f"{n} JSP{'s' if n != 1 else ''}" + (
                f", {others} other file{'s' if others != 1 else ''}" if others is not None else "")
            c.followup(f"web content not referenced by any pom left in place: {where} ({counts}) — serve it as "
                       f"before (web server) or move it into {dest}")
        if "web-content-left" not in c.result.flags:
            c.result.flags.append("web-content-left")

    def _dedupe_webinf_stash(self, c: _Ctx) -> None:
        """같은 XML 빈 설정이 boot 모듈에 두 번 들어간 경우(``legacy-config/`` 복사 + @ImportResource, 그리고
        front 이동의 ``asis-webinf/`` stash): import 되는 사본만 남긴다 — 같은 빈 정의 파일이 두 곳에 있으면
        어느 쪽이 쓰이는지 헷갈린다.

        An XML bean config that reached the boot module twice — `legacy-config/` (copied + `@ImportResource`) and
        the `asis-webinf/` stash of the front move — keeps the imported copy only."""
        boot = getattr(c, "boot_rel", None)
        if not boot:
            return
        stash = c.ws.abs(f"{c.p(boot)}/src/main/resources/{WEBINF_STASH}")
        legacy = c.ws.abs(f"{c.p(boot)}/src/main/resources/legacy-config")
        if not stash.is_dir() or not legacy.is_dir():
            return
        removed: list[str] = []
        for f in sorted(x for x in stash.rglob("*.xml") if x.is_file()):
            if (legacy / f.name).is_file() and "springframework.org/schema/beans" in f.read_text(encoding="utf-8",
                                                                                                errors="replace"):
                c.ws.delete(c.ws.rel(f))
                removed.append(f.name)
        if removed:
            self._prune_empty_dirs(c, c.ws.rel(stash))
            c.note(f"{len(removed)} XML bean config(s) kept once in {c.p(boot)}/src/main/resources/legacy-config/ "
                   f"(the {WEBINF_STASH}/ copies were dropped: {', '.join(removed[:4])})")

    @staticmethod
    def _root_coords(root: PomEditor, profile: ProjectProfile) -> GAV:
        """루트 pom 의 groupId / artifactId / version(부모에서 물려받은 값, 분석 프로필 값으로 보충).
        groupId / version 을 못 정하면 RestructureError — 생성 모듈의 좌표를 만들 수 없다."""
        g = root.effective_group_id() or (profile.root_pom.group_id if profile.root_pom else None)
        v = root.effective_version() or (profile.root_pom.version if profile.root_pom else None)
        a = root.artifact_id() or (profile.root_pom.artifact_id if profile.root_pom else None) or profile.name
        if not g or not v:
            raise RestructureError("root pom has no resolvable groupId/version")
        return GAV(group_id=g, artifact_id=a, version=v)

    # ------------------------------------------------------------------ single module
    def _single(self, c: _Ctx, root: PomEditor, coords: GAV) -> None:
        """단일 모듈 프로젝트: as-is 소스 루트를 전부 comp(back) 모듈로 옮기고 → front 모듈(웹 리소스) →
        WAR overlay 계획 → comp pom → boot 모듈(boot 리소스, Application, repackage) → 루트를 aggregator
        로. 순서가 중요하다: front 가 JSP 여부를 정해야 boot 가 WAR 인지 정해진다."""
        L = c.cfg.layout
        comp = c.p(c.comp_rel)
        boot = c.p(c.boot_rel)
        asis_packaging = root.packaging()
        asis_deps = root.dependencies()
        final_name = root.text_of(root.find("build"), "finalName") if root.find("build") is not None else None
        for d in self._source_roots(c, root):
            if c.ws.exists(c.p(d)):
                c.move(c.p(d), f"{comp}/{d}")
        for raw in self._climbing_dirs(root):
            c.note(f"directory {raw} of the root pom resolves outside the project root; not moved (nothing outside "
                   "the project is read or changed)")
        c.commit("restructure: move as-is sources to comp-jar")
        # front 를 boot 보다 먼저: JSP 가 옮겨졌는지(c.jsp_moved)가 boot 모듈의 WAR / jar 를 정한다
        self._populate_front(c, comp, boot, coords, [root])
        self._plan_overlays(c, [root], set())
        self._write_comp_pom(c, [root], coords, comp, asis_packaging)
        moved_main, moved_tests = self._populate_boot(c, [comp], boot)
        lib = GAV(group_id="${project.groupId}", artifact_id=coords.artifact_id + L.comp_artifact_suffix,
                  version="${project.version}")
        self._write_boot(c, coords, boot, [lib, *self._front_lib(c, coords)], asis_deps, moved_main, moved_tests,
                         final_name, [comp], root=root)
        self._rewrite_root(c, root, self._layout_modules(c), keep_deps=False, keep_build_plugins=False,
                           aggregator=self._role(c))
        self._packaging_followups(c, asis_packaging)
        c.commit("restructure: comp-jar / boot-jar layout")

    def _source_roots(self, c: _Ctx, ed: PomEditor) -> list[str]:
        """pom 이 쓰는 최상위 소스 디렉토리들(``src`` + build 의 source / resource 디렉토리 +
        maven-war-plugin 의 web root) = 옮길 대상. ``${...}``, 프로젝트 밖(``../``), 절대 경로, target,
        회사 모듈 디렉토리는 뺀다."""
        roots = ["src"]
        build = ed.find("build")
        paths: list[str] = []
        if build is not None:
            for name in ("sourceDirectory", "testSourceDirectory", "scriptSourceDirectory"):
                t = ed.text_of(build, name)
                if t:
                    paths.append(t)
            for sec, item in (("resources", "resource"), ("testResources", "testResource")):
                for r in ed.children(ed.child(build, sec), item):
                    t = ed.text_of(r, "directory")
                    if t:
                        paths.append(t)
            # 2026-10-23: maven-war-plugin warSourceDirectory / webResources (e.g. `STATIC/`) move with the sources;
            # _populate_front then splits them between the front and the comp module
            paths.extend(w.dir for w in pom_web_dirs(ed.to_string()))
        for pth in paths:
            pth = re.sub(r"^\$\{(?:project\.)?basedir\}/?", "", pth).strip().strip("/")
            if not pth or "${" in pth or pth.startswith(("..", "/")) or ":" in pth:
                continue
            top = pth.split("/")[0]
            if top not in roots and top not in ("target", c.cfg.layout.module_dir, ".", *self._module_tops(c)):
                roots.append(top)
        return roots

    # ------------------------------------------------------------------ multi-module: wrap
    def _wrap(self, c: _Ctx, root: PomEditor, mods: list[str], coords: GAV) -> None:
        """``multi_module_policy: wrap``: as-is 모듈을 그대로 두고(``layout.module_dir`` 가 설정돼 있으면 그
        아래로 옮기고) jar 모듈에 의존하는 boot 모듈을 하나 더한다(N+1 모듈). WAR 모듈은 WAR 그대로 두고 boot
        는 의존하지 않는다(S7). 옮긴 pom 의 <modules> / <parent><relativePath> 는 다시 계산한다(S5). 루트의
        의존성 / build 플러그인은 모듈이 상속하므로 남긴다.

        Move every as-is module under module/ unchanged and add boot-jar depending on the **jar** modules.

        WAR modules keep building as WARs (S7): no class is taken out of them and boot-jar does not depend on them.
        Nested `<modules>` and `<parent><relativePath>` of moved poms are recomputed (S5)."""
        L = c.cfg.layout
        md = L.module_dir.strip("/")
        boot = c.p(c.boot_rel)
        mapping: dict[str, str] = {}
        for m in mods:
            mn = posixpath.normpath(m)
            if mn.endswith(".xml"):
                mn = posixpath.dirname(mn) or "."
            # module_dir 가 "" (9c 기본)면 as-is 모듈은 제자리에
            # 둔다; 9c 이전 설정(module)일 때만 그 아래로 옮긴다
            if not md or mn.startswith(md + "/") or not c.ws.exists(c.p(mn)):
                if not c.ws.exists(c.p(mn)):
                    c.note(f"module directory {m} not found; left as declared")
                mapping[m] = m
                continue
            new = f"{md}/{mn}"
            c.move(c.p(mn), c.p(new))
            mapping[m] = new
        old2new = {}
        for o, n in mapping.items():
            on = posixpath.normpath(o)
            on = (posixpath.dirname(on) or ".") if on.endswith(".xml") else on
            if c.p(on) != c.p(n):
                old2new[c.p(on)] = c.p(n)
        self._fix_child_parents(c, old2new)
        module_eds: dict[str, PomEditor] = {}
        war_modules: list[str] = []
        for o, new in mapping.items():
            pom = c.p(new, "pom.xml")
            if not c.ws.exists(pom):
                continue
            ed = PomEditor(c.ws.abs(pom))
            if ed.packaging() == "war":
                war_modules.append(new)
                c.followup(f"WAR module {new} kept as-is (still packaged as a WAR, not a dependency of "
                           f"{c.boot_rel}); embedded deployment needs conversion (move its web layer to a jar "
                           "module or convert it to the boot-jar)")
            # repackage 는 boot 모듈에서만: 다른 jar 모듈이
            # repackage 되면 comp 의존성으로 쓸 수 없는 jar 가 된다
            elif ed.remove_plugin(*BOOT_PLUGIN):
                c.note(f"spring-boot-maven-plugin removed from {new} (repackage happens in {posixpath.basename(c.boot_rel)})")
            self._fix_java(c, ed, top=False)
            asis = posixpath.normpath(posixpath.join(c.base or ".", o))
            asis = (posixpath.dirname(asis) or ".") if asis.endswith(".xml") else asis
            self._relocate_module_system_deps(c, ed, "" if asis == "." else asis, c.p(new))
            c.save(ed)
            module_eds[new] = ed
        c.commit("restructure: wrap as-is modules under module/")

        jar_modules = [n for n, ed in module_eds.items() if ed.packaging() == "jar"]
        main_dirs: list[str] = []
        main_roots = {new: c.p(new, "src/main/java") for new in module_eds}
        for fqcn in c.profile.source.main_classes:
            f = self._locate_class(c, fqcn, list(main_roots.values()))
            if f:
                for new, r in main_roots.items():
                    if f.startswith(r + "/") and new not in main_dirs:
                        if new in war_modules:
                            c.note(f"@SpringBootApplication {fqcn} stays in WAR module {new}")
                        elif new in jar_modules:
                            main_dirs.append(new)
        if not main_dirs:
            main_dirs = list(jar_modules)
            c.note("no @SpringBootApplication class found in jar modules; boot-jar depends on all jar modules")
        lib_deps = []
        asis_deps = list(root.dependencies())
        for new in main_dirs:
            ed = module_eds[new]
            lib_deps.append(GAV(group_id=ed.group_id() or "${project.groupId}", artifact_id=ed.artifact_id() or new,
                                version=ed.version() or "${project.version}"))
            asis_deps += ed.dependencies()
        moved_main, moved_tests = self._populate_boot(c, [c.p(d) for d in main_dirs], boot)
        final_name = root.text_of(root.find("build"), "finalName") if root.find("build") is not None else None
        self._write_boot(c, coords, boot, lib_deps, asis_deps, moved_main, moved_tests, final_name,
                         [c.p(d) for d in main_dirs], root=root)
        new_modules = [mapping[m] for m in mods] + [c.boot_rel]
        self._rewrite_root(c, root, new_modules, keep_deps=True, keep_build_plugins=True)
        c.note("multi-module wrap: as-is root dependencies/build plugins kept (inherited by the wrapped modules)")
        c.commit("restructure: boot-jar added to wrapped multi-module project")

    def _fix_child_parents(self, c: _Ctx, old2new: dict[str, str]) -> None:
        """디렉토리를 옮긴 뒤, 그 아래 모든 pom 의 <parent><relativePath> 와 중첩 <modules> 를 새 위치
        기준으로 다시 계산한다 — 같은 부모 / 같은 모듈을 계속
        가리키게. 부모가 프로젝트 밖이면 relativePath 만 다시 계산한다.

        Re-point `<parent><relativePath>` and nested `<modules>` of every pom below moved directories."""
        if not old2new:
            return

        def map_path(p: str) -> str:
            for o, n in old2new.items():
                if p == o or p.startswith(o + "/"):
                    return n + p[len(o):]
            return p

        def unmap_path(p: str) -> str:
            for o, n in old2new.items():
                if p == n or p.startswith(n + "/"):
                    return o + p[len(n):]
            return p

        for pom_new in c.ws.pom_files():
            if not any(pom_new.startswith(n + "/") for n in old2new.values()) or "/src/" in pom_new:
                continue
            ed = PomEditor(c.ws.abs(pom_new))
            new_dir = posixpath.dirname(pom_new) or "."
            old_dir = posixpath.dirname(unmap_path(pom_new)) or "."
            # nested <modules> (e.g. <module>../shared</module>) keep pointing at the same (possibly moved) module
            mods = ed.modules()
            if mods:
                fixed = []
                for m in mods:
                    old_t = posixpath.normpath(posixpath.join(old_dir, m))
                    new_t = posixpath.relpath(map_path(old_t), new_dir)
                    fixed.append(new_t if new_t != posixpath.normpath(m) else m)
                if fixed != mods:
                    ed.set_modules(fixed)
                    c.note(f"<modules> of {pom_new} recomputed: {', '.join(fixed)}")
            parent = ed.get_parent()
            rp = ed.parent_relative_path()
            if parent is not None and rp != "":
                current = rp or "../pom.xml"
                old_target = posixpath.normpath(posixpath.join(old_dir, current))
                if not old_target.endswith(".xml"):
                    old_target = posixpath.join(old_target, "pom.xml")
                new_target = map_path(old_target)
                outside = new_target == ".." or new_target.startswith("../")
                ok = False
                if outside:
                    ok = True
                    c.note(f"parent of {pom_new} lives outside the project ({old_target}); relativePath recomputed")
                elif c.ws.exists(new_target):
                    try:
                        ok = PomEditor(c.ws.abs(new_target)).artifact_id() == parent.artifact_id
                    except PomEditError:
                        ok = False
                if ok:
                    new_rp = posixpath.relpath(new_target, new_dir)
                    if new_rp != current:
                        ed.set_parent_relative_path(new_rp)
            c.save(ed)

    # ------------------------------------------------------------------ multi-module: merge
    @staticmethod
    def _module_dir(m: str) -> str:
        """<module> 값 → 모듈 디렉토리(``x/pom.xml`` 처럼 pom 파일을 가리키면 그 디렉토리)."""
        mn = posixpath.normpath(m)
        return (posixpath.dirname(mn) or ".") if mn.endswith(".xml") else mn

    def _merge(self, c: _Ctx, root: PomEditor, mods: list[str], coords: GAV) -> None:
        """``multi_module_policy: merge`` (기본, 회사 레이아웃 = comp + boot (+ front)): 모든 as-is 모듈과
        루트 ``src/`` 를 comp(back) 모듈 하나로 합친다. 디렉토리 이름이 boot 모듈
        이름(``layout.boot_module``)인 모듈은 boot 모듈로 남긴다(소스 그대로, pom 만 조정). 단계: 0 pom 패키징
        모듈(aggregator / BOM)은 자식만 내놓음 → 1 남길 boot 모듈 → 2 이동 계획과 충돌 결정(아직 아무것도
        옮기지 않음) → 3 boot 모듈 이동 → 4 나머지를 comp 로, EAR 은 소스 없이 빼기(``_drop_ear``), front,
        comp pom → 5 boot pom(남긴 것 조정 또는 생성), 관리 항목 접기, 루트 pom. 충돌(두 모듈의 같은 경로):
        ``layout.merge_collision`` first-wins = ``<modules>`` 순서상 먼저 것이 이기고 다른 사본은 ``<run
        dir>/merge-conflicts/`` 에 저장(같은 FQCN 이 다르면 버려진 사본이 수정 스크립트 프롬프트의 참고 자료가
        된다), fail = RestructureError. META-INF/services,
        spring.factories, *.imports 는 충돌이 아니라 줄 합집합.

        Fold every as-is module (and the root `src/`) into comp-jar; an as-is module named like
        `layout.boot_module` is kept as boot-jar (moved to module/boot-jar, pom adjusted, sources untouched).

        `<packaging>pom</packaging>` sub-modules (BOM / nested aggregator) move nothing: their `<modules>` children
        are merged like any other module and their dependencyManagement / properties are folded into the root pom
        (first wins per g:a / key). Modules resolving outside the project root are skipped (note + follow-up).
        Collisions (same comp-jar path from two sources): `layout.merge_collision` first-wins (the first source in
        `<modules>` order wins; identical copies dropped, differing ones saved under `<run dir>/merge-conflicts/`)
        or fail (RestructureError). Line-list registries (META-INF/services/*, spring.factories,
        META-INF/spring/*.imports|*.factories) are never collisions: the kept file is the union of every copy's
        lines (all originals saved). Every check and decision happens before the first move.

        2026-10-23 (sibling poms + EAR, the intake aggregator): an `ear` module merges no sources — its files (the
        application-server descriptors) go to `<boot>/src/main/resources/asis-ear/<dir>/`, its dependencies on
        modules of this project are dropped, the others (its lib/ classpath) go to the comp module, `<webModule>`
        context roots become follow-ups (flag `ear-module-dropped`). A dependency on a module of this project is
        internal whatever its groupId spelling (module / parent groupIds, `${…}` or empty). Directories a module
        references outside itself but inside the project (`${basedir}/../../STATIC`) are folded in once and used from
        there (`_plan_outside_dirs`). Standalone `main` classes of merged jar modules get a follow-up per module."""
        L = c.cfg.layout
        comp = c.p(c.comp_rel)
        boot = c.p(c.boot_rel)
        boot_name = L.module_name("boot", c.name)
        collision = c.params.get("merge_collision", L.merge_collision)

        def load(label: str, mdir: str) -> PomEditor:
            pom = c.p(mdir, "pom.xml")
            if not c.ws.exists(pom):
                raise RestructureError(f"merge: module pom {pom} not found")
            ed = PomEditor(c.ws.abs(pom))
            if ed.packaging() != "pom" and ed.modules():
                raise RestructureError(f"merge policy does not support {ed.packaging()} module {label} declaring "
                                       "<modules>")
            return ed

        # 0. pom 패키징 모듈(aggregator / BOM)은 자식 모듈만 내놓는다. 깊이 우선 + 선언 순서 — 이 순서가
        # first-wins 의 '먼저' 를 정한다
        # 0. flatten: pom-packaging modules (aggregator / BOM) contribute their children, depth-first in order
        flat: list[tuple[str, str, PomEditor]] = []          # (label, module dir, editor)
        pom_mods: list[tuple[str, str, PomEditor]] = []
        order: list[tuple[str, str, PomEditor]] = []         # both kinds, declaration order (depth-first)

        def visit(label: str, mdir: str, depth: int) -> None:
            if any(mdir == d for _, d, _ in order):
                return
            ed = load(label, mdir)
            entry = (label, mdir, ed)
            order.append(entry)
            if ed.packaging() != "pom":
                flat.append(entry)
                return
            pom_mods.append(entry)
            if depth > 10:
                raise RestructureError(f"merge: module nesting too deep at {label}")
            for sub in ed.modules():
                sub = sub.strip().rstrip("/")
                sdir = posixpath.normpath(posixpath.join(mdir, self._module_dir(sub)))
                if sub.startswith("/") or self._outside(c, sdir):
                    self._skip_outside(c, f"{label}/pom.xml", sub, c.p(sdir))
                    continue
                visit(sdir, sdir, depth + 1)

        for m in mods:
            visit(m, self._module_dir(m), 0)

        # 1. 디렉토리 이름이 boot 모듈 이름인 첫 jar / war 모듈만 남긴다(이미 *-boot 로 나뉜 프로젝트를 다시
        # 쪼개지 않게); 같은 이름이 또 있으면 comp 로 합친다
        # 1. kept boot module (first jar/war module whose directory basename is the boot module name)
        kept: tuple[str, str, PomEditor] | None = None
        for entry in flat:
            if posixpath.basename(entry[1]) != boot_name:
                continue
            if kept is None:
                kept = entry
            else:
                c.note(f"module {entry[0]} is also named {boot_name}; merged into {c.comp_rel} "
                       f"(only {kept[0]} is kept as the boot module)")
        merged_jar = [e for e in flat if e is not kept]
        kept_ed = kept[2] if kept is not None else None
        eds = [ed for _, _, ed in merged_jar]
        all_merged = [e for e in order if e is not kept]
        # EAR 모듈은 소스를 합치지 않는다: Spring Boot 는 EAR 을 만들지 않는다(_drop_ear)
        ears = [e for e in merged_jar if e[2].packaging() == "ear"]          # no sources merged (2026-10-23)
        merged_src = [e for e in merged_jar if e[2].packaging() != "ear"]
        module_dirs = {d.split("/")[0] for _, d, _ in [*flat, *pom_mods]}
        sources_of: list[tuple[str, str, list[str]]] = []    # (module dir, label, top-level source dirs)
        root_tops = [t for t in self._source_roots(c, root) if t == "src" or t not in module_dirs]
        if any(c.ws.exists(c.p(t)) for t in root_tops):
            sources_of.append((".", "<root>", root_tops))
        # 루트 <build> 의 리소스 디렉토리는 모든 모듈이 상속하므로 모듈마다 그 디렉토리도 찾아본다
        inherited = self._source_roots(c, root)            # root <build> resource dirs apply to every module
        for label, mdir, ed in merged_src:
            sources_of.append((mdir, label, list(dict.fromkeys([*self._source_roots(c, ed), *inherited]))))
        self._plan_outside_dirs(c, root, merged_src, sources_of, [*flat, *pom_mods])
        mains = [(label, found) for label, mdir, ed in merged_src if ed.packaging() == "jar"
                 for found in [self._module_mains(c, mdir, ed)] if found]
        c.module_mains.update(f for _, found in mains for f in found)

        # 2. 이동 계획과 충돌 결정을 전부 먼저 한다 — fail 정책이면 아무것도 옮기기 전에 멈추도록
        # 2. plan the moves and decide every collision (nothing is moved yet)
        plan: list[tuple[str, str]] = []
        seen: dict[str, tuple[str, str]] = {}           # comp dst → (label, as-is src)
        conflicts: dict[str, MergeConflict] = {}
        unions: dict[str, list[tuple[str, str]]] = {}   # comp dst → [(label, as-is src)] of a line-list registry
        to_save: list[tuple[str, str]] = []             # (as-is src, run-dir-relative copy)

        def saved_path(label: str, rel: str) -> str:
            return f"merge-conflicts/{'root' if label == '<root>' else label}/{rel}"

        for mdir, label, tops in sources_of:
            for top in tops:
                src_abs = c.ws.abs(c.p(mdir, top))
                if not src_abs.is_dir():
                    continue
                for f in sorted(x for x in src_abs.rglob("*") if x.is_file()):
                    rel = f"{top}/{f.relative_to(src_abs).as_posix()}"
                    dst = f"{comp}/{rel}"
                    src = c.p(mdir, rel)
                    if dst in seen:
                        first_label, first_src = seen[dst]
                        # 줄 목록 레지스트리(META-INF/services, spring.factories, *.imports): 한쪽만 남기면
                        # 다른 모듈의 등록이 사라지므로 합집합
                        if UNION_RE.search(rel):
                            unions.setdefault(dst, [(first_label, first_src)]).append((label, src))
                            continue
                        if collision == "fail":
                            raise RestructureError(f"merge collision: {rel} exists in both {first_label} and {label}")
                        mc = conflicts.setdefault(dst, MergeConflict(path=rel, kept_from=first_label, identical=True))
                        mc.dropped_from.append(label)
                        # 같은 내용이면 하나만 남기고, 다르면 버려지는 사본을 merge-conflicts/ 에
                        # 저장한다(사람과 수정 스크립트 프롬프트가 볼 수 있게)
                        if c.ws.abs(first_src).read_bytes() != f.read_bytes():
                            mc.identical = False
                            mc.saved_copies.append(saved_path(label, rel))
                            to_save.append((src, saved_path(label, rel)))
                        continue
                    seen[dst] = (label, src)
                    if src != dst:
                        plan.append((src, dst))
        union_texts: dict[str, str] = {}
        for dst, copies in unions.items():
            rel = dst[len(comp) + 1:]
            texts = [c.ws.abs(s_).read_bytes() for _, s_ in copies]
            mc = MergeConflict(path=rel, kept_from=copies[0][0], dropped_from=[lb for lb, _ in copies[1:]])
            if all(t_ == texts[0] for t_ in texts):
                mc.identical = True
            else:
                mc.union = True
                for lb, s_ in copies:
                    mc.saved_copies.append(saved_path(lb, rel))
                    to_save.append((s_, saved_path(lb, rel)))
                union_texts[dst] = self._union_registry(rel, [t_.decode("utf-8", "replace") for t_ in texts])
            conflicts[dst] = mc
        # pom 패키징 모듈의 src/ 는 Maven 이 한 번도 컴파일하지
        # 않았다: 합치면 새 컴파일 오류만 생기므로 저장만 한다
        for _label, mdir, ed in pom_mods:            # sources Maven never compiled (pom packaging): saved, not merged
            if c.ws.abs(c.p(mdir, "src")).is_dir():
                for f in sorted(x for x in c.ws.abs(c.p(mdir, "src")).rglob("*") if x.is_file()):
                    rel = f"src/{f.relative_to(c.ws.abs(c.p(mdir, 'src'))).as_posix()}"
                    to_save.append((c.p(mdir, rel), saved_path(mdir, rel)))
                c.followup(f"pom-packaging module {mdir} has a src/ directory Maven never compiled; not merged — "
                           f"saved under {c.ws.run_dir}/merge-conflicts/{mdir}/src/")
        for src, saved in to_save:
            target = c.ws.run_dir / saved
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(c.ws.abs(src), target)
        if conflicts:
            c.result.merge_conflicts.extend(conflicts.values())
            same = [mc.path for mc in conflicts.values() if mc.identical]
            union = [mc.path for mc in conflicts.values() if mc.union]
            diff = [mc.path for mc in conflicts.values() if not mc.identical and not mc.union]
            if same:
                c.note(f"{len(same)} identical duplicate file(s) dropped: {', '.join(same[:5])}"
                       f"{' …' if len(same) > 5 else ''}")
            if union:
                c.note(f"{len(union)} registry file(s) (META-INF/services, spring.factories, *.imports) merged as the "
                       f"union of every module's lines (originals saved under {c.ws.run_dir}/merge-conflicts/): "
                       f"{', '.join(union[:5])}{' …' if len(union) > 5 else ''}")
            if diff:
                c.followup(f"merge conflicts: {len(diff)} file(s) existed in more than one module with different "
                           "content; the copy from the first module in <modules> order was kept, the others are "
                           f"saved under {c.ws.run_dir}/merge-conflicts/ — review: {', '.join(diff[:5])}"
                           f"{' …' if len(diff) > 5 else ''}")

        # 3. 남길 boot 모듈을 boot 모듈 경로(c.boot_rel, 기본 {name}-boot)로 옮긴다
        # 3. kept boot module → module/boot-jar
        kept_dir = kept[1] if kept is not None else None
        if kept is not None and kept_dir is not None and c.p(kept_dir) != boot:
            c.move(c.p(kept_dir), boot)
            self._fix_child_parents(c, {c.p(kept_dir): boot})
            kept_ed = PomEditor(c.ws.abs(f"{boot}/pom.xml"))

        # 4. fold the other sources into comp-jar
        for src, dst in plan:
            c.move(src, dst)
        for dst, text in union_texts.items():
            c.write(dst, text, generated=False)
        for mdir, _label, tops in sources_of:
            for top in tops:
                # 파일 단위 이동과 별도로 디렉토리 단위 이동도 기록: 분석의 as-is 경로(web.xml, XML 설정
                # ...)를 new_location 으로 풀 수 있게
                c.moves.append((c.p(mdir, top), f"{comp}/{top}"))
        for tgt, rel in c.relocated.items():              # folded-in directories outside every module: now empty
            if tgt == rel and c.ws.abs(c.p(tgt)).is_dir():
                self._prune_empty_dirs(c, c.p(tgt))
                if not c.ws.abs(c.p(tgt)).exists():
                    self._prune_empty_parents(c, c.p(tgt))
        merged_internal, internal = self._internal_keys(c, root, coords, [ed for _, _, ed in all_merged], kept_ed)
        ear_roots: list[tuple[str, str, str, bool]] = []    # (EAR label, web module, context root, explicit)
        for label, mdir, ed in ears:
            ear_roots += self._drop_ear(c, label, mdir, ed, boot, internal,
                                        [d for _, d, _ in order if d != mdir])
        packaging = "war" if any(ed.packaging() == "war" for ed in [*eds, *([kept_ed] if kept_ed else [])]) \
            else root.packaging()
        # front 는 comp pom 보다 먼저: comp pom 이 웹 리소스가 빠진
        # 뒤를 기준으로 쓰이고, JSP 여부(boot WAR)도 여기서 정해진다
        self._populate_front(c, comp, boot, coords, [root, *(ed for _, _, ed in all_merged)])
        self._plan_overlays(c, [root, *(ed for _, _, ed in all_merged if ed.packaging() != "ear"),
                                *([kept_ed] if kept_ed is not None else [])], internal)
        asis_deps = [d for d in root.dependencies() + [d for _, _, ed in all_merged for d in ed.dependencies()]
                     if d.key not in internal]
        self._write_comp_pom(c, [root, *(ed for _, _, ed in all_merged)], coords, comp, packaging, exclude=internal,
                             copy_props_from=eds)
        for label, mdir_, ed in all_merged:
            mdir = c.p(mdir_)
            if mdir != comp and c.ws.exists(mdir):
                if any(x == mdir or x.startswith(mdir + "/") for x in (comp, boot)):
                    # as-is 모듈 디렉토리가 곧 회사 모듈 디렉토리면 pom 만
                    # 지운다(디렉토리를 지우면 방금 합친 파일까지 사라짐)
                    c.ws.delete(f"{mdir}/pom.xml")          # the as-is module dir is the company module dir
                else:
                    c.ws.delete(mdir)
                    self._prune_empty_parents(c, mdir)
            if ed.packaging() != "ear":
                c.note(f"merged module {label} into {c.comp_rel} (module directory removed)")
        c.result.merged_modules = [label for label, _, ed in all_merged if ed.packaging() != "ear"]
        c.commit("restructure: merge as-is modules into comp-jar")

        # 5. boot 모듈 pom: 남긴 as-is 모듈이면 _keep_boot 로 조정, 아니면 새로 생성(_write_boot)
        # 5. boot-jar: the kept as-is module, or a generated one
        lib = GAV(group_id="${project.groupId}", artifact_id=coords.artifact_id + L.comp_artifact_suffix,
                  version="${project.version}")
        if kept_ed is not None and kept is not None and kept_dir is not None:
            parent = kept_ed.get_parent()
            # 역할 부모가 아닐 때, 남긴 boot 모듈의 부모가 합쳐져 없어진 pom 모듈이면 루트 pom 으로 다시
            # 잡는다(없는 부모 = 빌드 실패)
            if not self._role(c) and parent is not None and any(parent.artifact_id == ed.artifact_id()
                                                                 for _, _, ed in pom_mods):
                kept_ed.set_parent(GAV(group_id=coords.group_id, artifact_id=coords.artifact_id,
                                       version=coords.version), posixpath.relpath(c.p("pom.xml"), boot))
                c.note(f"kept boot module {kept[0]} inherited the merged pom module {parent.artifact_id}; parent "
                       f"re-pointed to the root pom {coords.artifact_id}")
            inherits_root = parent is not None and (
                parent.artifact_id in (coords.artifact_id, c.root_aid_old)
                or any(parent.artifact_id == ed.artifact_id() for _, _, ed in pom_mods))
            self._keep_boot(c, kept_ed, kept[0], kept_dir, boot, comp, coords, lib, merged_internal,
                            root=root if inherits_root else None)
            self._shadowed_boot_resources(c, comp, boot)
        else:
            moved_main, moved_tests = self._populate_boot(c, [comp], boot)
            final_name = root.text_of(root.find("build"), "finalName") if root.find("build") is not None else None
            self._write_boot(c, coords, boot, [lib, *self._front_lib(c, coords)], asis_deps, moved_main,
                             moved_tests, final_name, [comp], root=root, exclude=merged_internal)
        role = self._role(c)
        comp_ed = PomEditor(c.ws.abs(f"{comp}/pom.xml")) if role else None
        self._fold_management(c, root, all_merged, merged_internal, target=comp_ed)
        if comp_ed is not None:
            c.save(comp_ed)
        self._rewrite_root(c, root, self._layout_modules(c), keep_deps=False, keep_build_plugins=False,
                           aggregator=role)
        self._packaging_followups(c, packaging)
        self._ear_followups(c, ear_roots)
        kind = "WAR" if c.boot_war else "jar"
        for label, found in mains:
            c.followup(f"module {label} had main class{'es' if len(found) > 1 else ''} {', '.join(found)} (a standalone "
                       f"batch?): merged into {c.comp_rel} — run it from the boot {kind}'s classpath or keep it as its "
                       "own module")
        c.commit("restructure: comp-jar / boot-jar layout (merged)")

    @staticmethod
    def _internal_keys(c: _Ctx, root: PomEditor, coords: GAV, merged: list[PomEditor],
                       kept: PomEditor | None) -> tuple[set[str], set[str]]:
        """(merged_internal, internal): 합쳐진 모듈(internal 은 남긴 boot 모듈까지)을 가리키는 g:a 키 집합. 이
        의존성들은 comp 안으로 들어왔으므로 의존성 목록에서 빠져야 한다 — 남으면 저장소에 없는 아티팩트를 찾다
        빌드가 실패한다. groupId 철자(모듈 / 부모 groupId, ``${...}``, 빈 값)와 상관없이 artifactId 로도
        잡는다(2026-10-23 회사 EAR 사례).

        (merged_internal, internal): the g:a keys that name a merged module (merged_internal) or the kept boot
        module too (internal). groupIds: the root's, `${project.groupId}`, every module's own and its parent's (a
        module without `<groupId>` inherits it). On top, every dependency of the root / module poms (managed or not)
        whose artifactId is a module artifactId and whose groupId is empty or a `${…}` expression (2026-10-23: the
        company EAR named its WAR module with a property groupId)."""
        eds = [*merged, *([kept] if kept is not None else [])]
        groups = {coords.group_id, "${project.groupId}"} | {ed.group_id() for ed in eds if ed.group_id()}
        for ed in eds:
            parent = ed.get_parent()
            if parent is not None and parent.group_id:
                groups.add(parent.group_id)
        merged_aids = {a for a in (ed.artifact_id() for ed in merged) if a}
        kept_aid = kept.artifact_id() if kept is not None else None
        merged_internal = {f"{g}:{a}" for g in groups for a in merged_aids}
        internal = merged_internal | ({f"{g}:{kept_aid}" for g in groups} if kept_aid else set())
        for ed in [root, *eds]:
            for managed in (False, True):
                for el in ed.dependency_elements(managed):
                    g = ed.gav_of(el)
                    # 리터럴 groupId 는 위의 groups 조합으로 판단 끝: 빈 값 / ${...} groupId 만 artifactId 로
                    # 더 판정한다(회사 EAR 이 WAR 모듈을 속성 groupId 로 불렀던 사례)
                    if g.group_id and "${" not in g.group_id:
                        continue
                    if g.artifact_id in merged_aids:
                        merged_internal.add(g.key)
                        internal.add(g.key)
                    elif kept_aid and g.artifact_id == kept_aid:
                        internal.add(g.key)
        return merged_internal, internal

    # ------------------------------------------------------------------ EAR modules (2026-10-23)
    def _drop_ear(self, c: _Ctx, label: str, mdir: str, ed: PomEditor, boot: str, internal: set[str],
                  other_dirs: list[str]) -> list[tuple[str, str, str, bool]]:
        """EAR 모듈은 합치지 않는다(Spring Boot 는 EAR 을 만들지 않음): pom / target 을 뺀 파일(애플리케이션
        서버 descriptor)은 ``<boot>/src/main/resources/asis-ear/<dir>/`` 에 참고용으로(Boot 는 읽지 않음), 이
        프로젝트 모듈에 대한 의존성은 버리고, 나머지(EAR 의 lib/ = 공유 런타임 classpath)는 comp
        모듈로(``_write_comp_pom``), 다른 곳의 WAR 의존성은 가져가지 않는다(후속 작업). 웹 모듈 context root
        목록을 돌려준다(``_ear_followups``).

        An `ear` module is not merged: Spring Boot builds no EAR. Its files other than pom.xml / target/ (the
        application-server descriptors under src/main/application) → `<boot>/src/main/resources/asis-ear/<dir>/…`;
        its dependencies on modules of this project are dropped (internal), the others go to the comp module with the
        merged dependencies (`_write_comp_pom`; the EAR's lib/ was the shared runtime classpath), external WAR
        dependencies (web modules from elsewhere) are not carried. Returns the web modules' context roots
        (label, web module, context root, explicit) for `_ear_followups`."""
        roots = self._ear_context_roots(c, label, mdir, ed)
        name = posixpath.basename(mdir) or label.replace("/", "_")
        stash = f"{boot}/src/main/resources/{EAR_STASH}/{name}"
        if c.ws.exists(stash):
            stash = f"{boot}/src/main/resources/{EAR_STASH}/{mdir.replace('/', '_')}"
        base = c.ws.abs(c.p(mdir))
        # EAR 디렉토리 안에 다른 모듈이 있으면 그 파일은 stash 로 옮기지 않는다(그 모듈이 따로 합쳐짐)
        nested = [d for d in other_dirs if d.startswith(mdir + "/") or mdir in (".", "")]
        moved: list[str] = []
        if base.is_dir():
            for f in sorted(x for x in base.rglob("*") if x.is_file()):
                rel = f.relative_to(base).as_posix()
                if rel == "pom.xml" or rel.split("/")[0] == "target":
                    continue
                if any(c.p(mdir, rel).startswith(c.p(d) + "/") for d in nested):
                    continue
                c.move(c.p(mdir, rel), f"{stash}/{rel}")
                moved.append(rel)
        deps = ed.dependencies()
        dropped = [g.key for g in deps if g.key in internal]
        drop = set(c.cfg.artifacts.remove_when_present) | {s.key for s in c.cfg.artifacts.mandatory}
        web_ext = [g for g in deps if g.key not in internal and (g.type or "") == "war"]
        libs = [g.key for g in deps if g.key not in internal and (g.type or "") != "war" and g.key not in drop]
        gav = f"{ed.effective_group_id() or '?'}:{ed.artifact_id() or '?'}"
        c.note(f"EAR module {label} ({gav}) dropped: Spring Boot builds no EAR — "
               + (f"{len(moved)} file(s) (application-server descriptors) kept for reference under {stash}/ (not read "
                  "by Spring Boot); " if moved else "no files besides its pom; ")
               + f"dependencies on modules of this project dropped ({', '.join(dropped) or 'none'}); "
               + (f"{len(libs)} library dependenc{'y' if len(libs) == 1 else 'ies'} (the EAR's shared lib/ classpath) "
                  f"moved to {c.comp_rel} ({', '.join(libs[:5])}{' …' if len(libs) > 5 else ''})" if libs
                  else "no library dependencies")
               + ("; maven-ear-plugin not carried" if ed.find_plugins(*EAR_PLUGIN, where="all") else "")
               + "; module directory removed")
        for g in web_ext:
            c.followup(f"EAR module {label} also packaged the web module {g.coordinates}, which is not part of this "
                       "project: not carried — deploy it separately (it is no dependency of any generated module)")
        try:
            c.ws.journal({"event": "ear_module_dropped", "module": label, "artifact": gav, "stash": stash,
                          "files": len(moved), "internal_dropped": dropped, "libraries": libs,
                          "context_roots": [{"web_module": w, "context_root": r} for _, w, r, _ in roots]})
        except (OSError, AttributeError) as e:  # pragma: no cover - journal unavailable
            log.debug("journal ear_module_dropped skipped: %s", e)
        if "ear-module-dropped" not in c.result.flags:
            c.result.flags.append("ear-module-dropped")
        return roots

    def _ear_context_roots(self, c: _Ctx, label: str, mdir: str,
                           ed: PomEditor) -> list[tuple[str, str, str, bool]]:
        """EAR 웹 모듈별 context root: maven-ear-plugin <webModule><contextRoot> → application.xml 의
        <web-uri> / <context-root> → 둘 다 없는 WAR 의존성은 maven-ear-plugin 기본값 ``/<artifactId>``
        (explicit=False). as-is URL 을 지키라는 후속 작업의 재료.

        (EAR label, web module, context root, explicit) of an EAR: maven-ear-plugin `<webModule><contextRoot>`
        (build plugins, then pluginManagement), then `<web><web-uri>/<context-root>` of
        src/main/application/META-INF/application.xml; a WAR dependency with neither gets the maven-ear-plugin
        default `/<artifactId>` (explicit=False)."""
        out: list[tuple[str, str, str, bool]] = []
        seen: set[str] = set()
        for p in ed.find_plugins(*EAR_PLUGIN, where="all"):
            for wm in ed.children(ed.child(ed.child(p, "configuration"), "modules"), "webModule"):
                aid = ed.text_of(wm, "artifactId") or "?"
                root_ = ed.text_of(wm, "contextRoot")
                if root_ is not None and aid not in seen:
                    seen.add(aid)
                    out.append((label, aid, root_ or "/", True))
        app = c.ws.abs(c.p(mdir, "src/main/application/META-INF/application.xml"))
        if app.is_file():
            for uri, root_ in APPLICATION_XML_WEB_RE.findall(app.read_text(encoding="utf-8", errors="replace")):
                aid = posixpath.splitext(posixpath.basename(uri.strip()))[0]
                matches = [g.artifact_id for g in ed.dependencies() if (g.type or "") == "war"
                           and aid.startswith(g.artifact_id)]
                aid = matches[0] if matches else aid
                if aid not in seen:
                    seen.add(aid)
                    out.append((label, aid, root_ or "/", True))
        for g in ed.dependencies():
            if (g.type or "") == "war" and g.artifact_id not in seen:
                seen.add(g.artifact_id)
                out.append((label, g.artifact_id, f"/{g.artifact_id}", False))
        return out

    def _ear_followups(self, c: _Ctx, roots: list[tuple[str, str, str, bool]]) -> None:
        """context root 마다 후속 작업: boot WAR 면 외부 Tomcat 에 ``<root>.war`` 이름으로 배포하거나
        context-path 설정, 실행 jar 면 ``server.servlet.context-path`` — as-is URL 이 바뀌지 않게."""
        for label, web, root_, explicit in roots:
            cr = "/" + root_.strip().strip("/") if root_.strip().strip("/") else "/"
            how = "" if explicit else " (the maven-ear-plugin default)"
            if c.boot_war:
                war = cr.strip("/").replace("/", "#") or "ROOT"
                c.followup(f"EAR {label} deployed {web} at context root {cr}{how}: deploy {c.boot_rel} as {war}.war "
                           f"on the external Tomcat, or set server.servlet.context-path={cr} (embedded Tomcat)")
            else:
                c.followup(f"EAR {label} deployed {web} at context root {cr}{how}: set "
                           f"server.servlet.context-path={cr} for {c.boot_rel} (embedded Tomcat) so the URLs stay the "
                           "same")

    def _strip_ear_plugin(self, c: _Ctx, ed: PomEditor) -> None:
        """maven-ear-plugin 은 생성 pom 에 들어가지 않는다(EAR 모듈이 없어졌으므로).

        maven-ear-plugin never reaches a generated pom (there is no EAR module any more)."""
        if ed.find_plugins(*EAR_PLUGIN, where="all") and ed.remove_plugin(*EAR_PLUGIN):
            c.note(f"maven-ear-plugin dropped from {c.ws.rel(ed.path)} (the EAR module was not carried)")

    # ------------------------------------------------------------------ WAR overlays (2026-10-23)
    def _plan_overlays(self, c: _Ctx, sources: list[PomEditor], exclude: set[str]) -> None:
        """``<type>war</type>`` 외부 의존성 = maven-war-plugin overlay: jar 모듈의 의존성이 될 수
        없다(``_write_comp_pom`` 이 건너뜀). 웹 콘텐츠를 가져오므로 boot 가 WAR 이거나
        ``layout.jsp_boot_packaging: war`` 면 boot 를 WAR 로 만들고 overlay 와 as-is <overlays> 설정을
        그쪽으로 옮긴다(플래그 ``war-overlay-moved``), 아니면 후속 작업. overlay WAR 는 Maven Central 에
        없으므로 언제나 '사내 Nexus 에 올려야 함' 후속 작업.

        External `<type>war</type>` dependencies left after the internal exclusion are maven-war-plugin overlays:
        never a dependency of a jar module (`_write_comp_pom` skips them). They carry web content, so with a boot
        WAR — or `layout.jsp_boot_packaging: war` — they make the boot module a WAR (like JSPs) and go there with the
        as-is `<overlays>` configuration (`_apply_overlays`, flag `war-overlay-moved`); otherwise follow-up. Always a
        follow-up that the overlay WAR must be resolvable from the configured repositories."""
        drop = set(c.cfg.artifacts.remove_when_present) | set(exclude)
        seen: set[str] = set()
        managed = {s_.gav_of(el).key: s_.resolve(s_.gav_of(el).version) for s_ in reversed(sources)
                   for el in s_.dependency_elements(True) if s_.gav_of(el).version}
        for src in sources:
            for el in src.dependency_elements(False):
                g = src.gav_of(el)
                if (g.type or "") != "war" or g.key in drop or g.key in seen:
                    continue
                seen.add(g.key)
                v = src.resolve(g.version) if g.version else managed.get(g.key)
                if v and "${" not in v:
                    g = GAV(group_id=g.group_id, artifact_id=g.artifact_id, version=v, type=g.type,
                            classifier=g.classifier, scope=g.scope)
                c.war_overlays.append((c.ws.rel(src.path), g))
            if c.overlay_conf is None:
                for p in src.find_plugins(*WAR_PLUGIN, where="all"):
                    ov = src.child(src.child(p, "configuration"), "overlays")
                    if ov is not None:
                        c.overlay_conf = ov
                        break
        if not c.war_overlays:
            return
        # overlay 는 웹 콘텐츠: boot 가 WAR 일 때만 실을 수
        # 있다 — 그래서 overlay 가 있으면 boot 를 WAR 로 만든다
        c.overlay_carry = c.boot_war or c.cfg.layout.jsp_boot_packaging == "war"
        c.boot_war = c.boot_war or c.overlay_carry
        for pom, g in c.war_overlays:
            if c.overlay_carry:
                c.note(f"WAR overlay {g.coordinates} ({pom}) moved to {c.boot_rel} (a WAR for external Tomcat: the "
                       "overlay carries web content)"
                       + ("; maven-war-plugin <overlays> copied" if c.overlay_conf is not None else ""))
            else:
                c.followup(f"WAR overlay {g.coordinates} ({pom}) not carried: {c.boot_rel} is an executable jar "
                           f"(layout.jsp_boot_packaging: jar) — copy the overlay's web content into "
                           f"{c.front_rel or c.comp_rel} or set layout.jsp_boot_packaging: war")
            c.followup(f"WAR overlay {g.coordinates} must be resolvable from the configured repositories: install it "
                       "into the company Nexus (build.settings_file mirror) — it is not on Maven Central")
        if c.overlay_carry and "war-overlay-moved" not in c.result.flags:
            c.result.flags.append("war-overlay-moved")

    def _apply_overlays(self, c: _Ctx, ed: PomEditor) -> None:
        """boot WAR pom 에 overlay 의존성(type war, scope 그대로)과 as-is <overlays> 블록을
        넣는다(maven-war-plugin 버전은 부모 / Maven 기본에 맡김).

        The boot WAR's pom: the overlay dependencies (type war, scope as declared) and the as-is
        maven-war-plugin `<overlays>` block in its maven-war-plugin (no version: the parent / Maven default manages
        it; `_strip_war_web_config` keeps `<overlays>`)."""
        if not c.overlay_carry:
            return
        for _pom, g in c.war_overlays:
            if not ed.has_dependency(g.group_id, g.artifact_id):
                ed.add_dependency(GAV(group_id=g.group_id, artifact_id=g.artifact_id, version=g.version, type="war",
                                      classifier=g.classifier, scope=g.scope))
        if c.overlay_conf is None:
            return
        plugins = ed.find_plugins(*WAR_PLUGIN, where="build")
        if not plugins:
            ed.add_plugin(PluginInfo(group_id=WAR_PLUGIN[0], artifact_id=WAR_PLUGIN[1]))
            plugins = ed.find_plugins(*WAR_PLUGIN, where="build")
        p = plugins[0]
        conf = ed.child(p, "configuration")
        if conf is None:
            conf = ed._build_value(p, "configuration", None)
            p.remove(conf)
            ed._append(p, conf)
        if ed.child(conf, "overlays") is None:
            ed.copy_into(conf, c.overlay_conf)
            ed.reindent(p)

    # ------------------------------------------------------------------ directories outside a module (2026-10-23)
    @staticmethod
    def _climbing_dirs(ed: PomEditor) -> list[str]:
        """pom 하나가 자기 디렉토리 밖(``../``)을 가리키는 디렉토리들: web root(warSourceDirectory /
        webResources), source / script / test source, resources / testResources.

        Directories of one pom that climb out of its directory (`../…`, normalized): maven-war-plugin
        warSourceDirectory / webResources, source / script / test source directories, resources / testResources."""
        text = ed.to_string()
        out = [w.dir for w in pom_web_dirs(text, allow_parent=True)] + pom_build_dirs(text, allow_parent=True)
        build = ed.find("build")
        if build is not None:
            d = clean_pom_dir(ed.text_of(build, "testSourceDirectory"), allow_parent=True)
            if d:
                out.append(d)
            for r in ed.children(ed.child(build, "testResources"), "testResource"):
                d = clean_pom_dir(ed.text_of(r, "directory"), allow_parent=True)
                if d:
                    out.append(d)
        return [d for d in dict.fromkeys(out) if d.startswith("../")]

    @staticmethod
    def _ed_dir(c: _Ctx, ed: PomEditor) -> str:
        """as-is pom 디렉토리의 프로젝트 루트(c.base) 기준 상대 경로.

        The as-is pom's directory relative to the project root (c.base)."""
        d = posixpath.dirname(c.ws.rel(ed.path)) or "."
        if c.base:
            d = posixpath.relpath(d, c.base)
        return d

    def _comp_dir(self, c: _Ctx, ed: PomEditor, d: str | None) -> str | None:
        """모듈 기준 pom 디렉토리 → comp 모듈 기준: 모듈 안 경로는 그대로(파일이 같은 상대 경로로
        옮겨졌으므로), ``../`` 경로는 ``c.relocated`` 로 바꾸고, 옮겨지지 않은 ``../`` 는 None.

        A module-relative pom directory → comp-module-relative: unchanged for paths inside the module (the files
        moved with the same relative path), mapped through `c.relocated` for `../` paths, None when not relocated."""
        if not d or not d.startswith("../"):
            return d
        tgt = posixpath.normpath(posixpath.join(self._ed_dir(c, ed), d))
        for t in sorted(c.relocated, key=len, reverse=True):
            if tgt == t or tgt.startswith(t + "/"):
                return posixpath.normpath(c.relocated[t] + tgt[len(t):])
        return None

    def _plan_outside_dirs(self, c: _Ctx, root: PomEditor, merged_src: list[tuple[str, str, PomEditor]],
                           sources_of: list[tuple[str, str, list[str]]],
                           modules: list[tuple[str, str, PomEditor]]) -> None:
        """합쳐질 모듈이 자기 밖이지만 프로젝트 안을 가리키는 디렉토리(``APP/WEB`` 의
        ``${basedir}/../../STATIC`` = ``STATIC``)는 comp 모듈의 같은 프로젝트 상대
        경로(``<comp>/STATIC/...``)로 한 번만 옮기고 거기서 쓴다(``c.relocated``: front 의 web root, comp pom
        의 <resources> / 소스 디렉토리 재작성). 다른 합쳐질 모듈 안이면 그 모듈과 함께 움직인다. 프로젝트 밖,
        루트 자체, 남길 / EAR / pom 모듈을 품거나 그 안인 디렉토리는 옮기지 않는다(노트).

        A merged module's directory that resolves outside the module but inside the project (`APP/WEB` →
        `${basedir}/../../STATIC` = `STATIC`) is folded into the comp module once, at its project-relative path
        (`<comp>/STATIC/…`), and used from there (`c.relocated`: web root for `_populate_front`, rewritten
        `<resources>` / source directories in the comp pom). Inside another merged module it is mapped to that
        module's relative path (moving with that module). A path leaving the project root, the root itself, or a
        directory holding / inside a kept / EAR / pom module is skipped (note)."""
        comp = c.comp_rel
        owners = {d: label for label, d, _ in merged_src}
        others = [d for _, d, _ in modules if d not in owners]
        all_dirs = [d for _, d, _ in modules]
        root_entry = next((e for e in sources_of if e[1] == "<root>"), None)
        found: list[tuple[str, str, str]] = []
        for label, mdir, ed in [("<root>", ".", root), *merged_src]:
            for raw in self._climbing_dirs(ed):
                tgt = posixpath.normpath(posixpath.join(mdir, raw))
                where = "the root pom" if label == "<root>" else f"module {label}"
                if tgt in (".", ""):
                    c.note(f"directory {raw} of {where} is the project root itself; not moved")
                    continue
                if self._outside(c, tgt):
                    c.note(f"directory {raw} of {where} resolves outside the project root ({c.p(tgt)}); not moved "
                           "(nothing outside the project is read or changed)")
                    continue
                found.append((label, raw, tgt))
        for label, raw, tgt in sorted(found, key=lambda x: (len(x[2]), x[2])):
            if any(tgt == t or tgt.startswith(t + "/") for t in c.relocated):
                continue
            if any(d.startswith(tgt + "/") for d in all_dirs) or any(tgt == d or tgt.startswith(d + "/")
                                                                    for d in others):
                c.note(f"directory {raw} of module {label} ({c.p(tgt)}) holds or belongs to another module that is "
                       "not merged; not moved")
                continue
            owner = next((d for d in sorted(owners, key=len, reverse=True)
                          if d not in (".", "") and (tgt == d or tgt.startswith(d + "/"))), None)
            if owner is not None:
                rel = posixpath.relpath(tgt, owner)
                if rel == ".":
                    continue
                entry = next(e for e in sources_of if e[0] == owner)
                if rel.split("/")[0] not in entry[2]:
                    entry[2].append(rel.split("/")[0])
                c.relocated[tgt] = rel
                c.note(f"directory {raw} of module {label} lies in module {owners[owner]} ({c.p(tgt)}): used as "
                       f"{comp}/{rel}")
                continue
            if root_entry is not None and tgt.split("/")[0] in root_entry[2]:
                c.relocated[tgt] = tgt
                c.note(f"directory {raw} of module {label} ({c.p(tgt)}) moves with the root sources: used as "
                       f"{comp}/{tgt}")
                continue
            sources_of.append((".", label, [tgt]))
            c.relocated[tgt] = tgt
            c.note(f"directory {raw} of module {label} lies outside the module but inside the project ({c.p(tgt)}): "
                   f"moved to {comp}/{tgt} and used from there")

    def _module_mains(self, c: _Ctx, mdir: str, ed: PomEditor) -> list[str]:
        """모듈의 main 소스 아래 ``public static void main(String...)`` 이 있는 비 Boot 클래스 FQCN 들 — 합친
        뒤에는 따로 실행되지 않으므로(배치일 수 있음) 모듈별 후속 작업으로 알린다.

        FQCNs with `public static void main(String…)` in a non-Boot class below the module's main source dirs."""
        roots = ["src/main/java"]
        build = ed.find("build")
        sd = clean_pom_dir(ed.text_of(build, "sourceDirectory")) if build is not None else None
        if sd:
            roots.insert(0, sd)
        out: list[str] = []
        for r in dict.fromkeys(roots):
            base = c.ws.abs(c.p(mdir, r))
            if not base.is_dir():
                continue
            for f in sorted(base.rglob("*.java")):
                text = strip_comments(f.read_text(encoding="utf-8", errors="replace"))
                if not MAIN_METHOD_RE.search(text) or BOOT_APP_RE.search(text):
                    continue
                m = PACKAGE_RE.search(text)
                fq = f"{m.group(1)}.{f.stem}" if m else f.stem
                if fq not in out:
                    out.append(fq)
        return out

    def _prune_empty_parents(self, c: _Ctx, rel: str) -> None:
        """모듈 디렉토리를 지운 뒤 비게 된 상위 디렉토리를 프로젝트 루트 직전까지 지운다.

        After a module directory was removed: drop its now-empty parent directories inside the project."""
        cur = posixpath.dirname(rel)
        stop = c.base or ""
        while cur and cur not in (".", stop) and (not stop or cur.startswith(stop + "/")):
            d = c.ws.abs(cur)
            if not d.is_dir() or any(d.iterdir()):
                break
            d.rmdir()
            cur = posixpath.dirname(cur)

    @staticmethod
    def _outside(c: _Ctx, mdir: str) -> bool:
        """디렉토리가 프로젝트 루트 밖(``..``)으로 나가는가."""
        tgt = c.p(mdir)
        return tgt == ".." or tgt.startswith("../")

    def _skip_outside(self, c: _Ctx, declared_in: str, module: str, target: str) -> None:
        """프로젝트 밖 모듈은 합치지 않는다: 노트 + 후속 작업(따로 마이그레이션해 Nexus 에 올리거나
        프로젝트 안으로 옮기기). 그 모듈에 대한 의존성은 이제 저장소에서 찾는다."""
        c.note(f"module '{module}' declared in {declared_in} resolves outside the project root ({target}); "
               "not merged (nothing outside the workspace is read or changed)")
        c.followup(f"module {module} ({declared_in}) lives outside the project root and was not merged into "
                   f"{c.comp_rel}: migrate it separately and install it into "
                   "Nexus, or move it into the project — dependencies on it now resolve from the repository")

    def _fold_management(self, c: _Ctx, root: PomEditor, merged: list[tuple[str, str, PomEditor]],
                         internal: set[str], target: PomEditor | None = None) -> None:
        """합쳐진 모듈의 관리 항목을 살린다: 대상 pom 의 dependencyManagement 중 합쳐진 모듈 항목(내부 BOM
        import, 내부 모듈 버전)은 지우고, 각 모듈의 dependencyManagement 는 대상 pom 으로 접는다(g:a 마다 먼저
        것이 이김, 대상 자신의 것이 우선) — 버전이 쓰는 ``${props}`` 도 함께. pom 패키징 모듈의 <properties>
        도 접는다. 대상 = 루트, 역할 부모(라운드 9b)면 ``target`` = comp pom(모듈이 루트를 상속하지 않으므로).

        Keep the merged modules' management: root `<dependencyManagement>` entries of merged modules (an internal
        BOM import, versions of internal modules) are removed; every merged module's dependencyManagement entry
        is folded into the root pom (first wins per g:a, the root's own entries first), with the `${props}` its
        version references; pom-packaging modules' `<properties>` are folded too (first wins per key).
        `target` (role parents, round 9b): fold into that pom (the comp pom) instead of the root — the modules do
        not inherit the root pom then."""
        drop = set(c.cfg.artifacts.remove_when_present)
        tgt = target if target is not None else root
        where = "the root pom" if target is None else c.ws.rel(target.path)
        removed = []
        for el in tgt.dependency_elements(True):
            g = tgt.gav_of(el)
            if g.key in internal:
                tgt.remove_node(el)
                removed.append(g.key)
        if removed:
            tgt.remove_if_empty(tgt.find("dependencyManagement/dependencies"))
            tgt.remove_if_empty(tgt.find("dependencyManagement"))
            c.note(f"{'root ' if target is None else where + ' '}dependencyManagement entries for merged modules "
                   f"removed: {', '.join(removed)}")
        for label, _mdir, ed in merged:
            folded: list[str] = []
            props: list[str] = []
            for el in ed.dependency_elements(True):
                g = ed.gav_of(el)
                if g.key in internal or g.key in drop or tgt.has_managed_dependency(g.group_id, g.artifact_id):
                    continue
                tgt.copy_into(tgt.ensure_path("dependencyManagement/dependencies"), el)
                folded.append(g.key)
                for prop in re.findall(r"\$\{([^}]+)\}", "".join(el.itertext())):
                    v = ed.get_property(prop)
                    if v is not None and not tgt.has_property(prop):
                        tgt.set_property(prop, v)
                        props.append(prop)
            if ed.packaging() == "pom":
                for k, v in ed.properties().items():
                    if not tgt.has_property(k) and k != NAME_PROP:
                        tgt.set_property(k, v)
                        props.append(k)
            if folded or props:
                c.note(f"module {label}: {len(folded)} dependencyManagement entr{'y' if len(folded) == 1 else 'ies'}"
                       f"{' (' + ', '.join(folded[:5]) + (' …' if len(folded) > 5 else '') + ')' if folded else ''}"
                       f" and {len(set(props))} propert{'y' if len(set(props)) == 1 else 'ies'} folded into {where} "
                       "(first wins)")

    def _shadowed_boot_resources(self, c: _Ctx, comp: str, boot: str) -> None:
        """남긴 boot 모듈: boot 리소스와 같은 이름의 comp 사본은 런타임에 가려진다(BOOT-INF/classes 가
        BOOT-INF/lib 보다 먼저, Boot 는 위치마다 파일 하나만 읽음) → 합치라는 후속 작업.

        Kept boot module: comp-jar copies of boot resources with the same name as boot-jar's own are shadowed at
        runtime (BOOT-INF/classes precede BOOT-INF/lib; Boot reads one file per location)."""
        res = f"{comp}/src/main/resources"
        shadowed = [f[len(res) + 1:] for f in self._boot_resource_files(c, res)
                    if c.ws.exists(f"{boot}/src/main/resources/{f[len(res) + 1:]}")]
        if shadowed:
            c.followup(f"{', '.join(shadowed)} exist in both {boot} (kept) and {comp} (merged modules): the boot-jar "
                       "copy wins at runtime and the comp-jar settings are not read — merge them into the boot-jar "
                       "file")

    @staticmethod
    def _union_registry(rel: str, texts: list[str]) -> str:
        """줄 목록 레지스트리의 합집합: ``*.factories`` 는 키마다 쉼표 값을 합치고(중복 제거, 줄 이음 처리),
        그 밖(META-INF/services, *.imports)은 첫 사본의 줄을 그대로 두고 다른 사본의 새 줄만 덧붙인다.

        Union of line-list registries: `*.factories` (key=comma list, `\\` continuations) merged per key with
        deduped values; other registries keep the first copy's lines in order, then new lines of the others."""
        if rel.endswith(".factories"):
            merged: dict[str, list[str]] = {}
            for text in texts:
                logical: list[str] = []
                buf = ""
                for raw in text.splitlines():
                    line = raw.strip()
                    if not buf and (not line or line.startswith(("#", "!"))):
                        continue
                    if (len(line) - len(line.rstrip("\\"))) % 2 == 1:      # odd trailing backslashes: continued
                        buf += line[:-1]
                        continue
                    logical.append(buf + line)
                    buf = ""
                if buf:
                    logical.append(buf)
                for ln in logical:
                    m = re.match(r"\s*([^=:\s]+)\s*[=:]\s*(.*)$", ln)
                    if not m:
                        continue
                    vals = merged.setdefault(m.group(1), [])
                    for v in (x.strip() for x in m.group(2).split(",")):
                        if v and v not in vals:
                            vals.append(v)
            out = []
            for k, vals in merged.items():
                out.append(f"{k}=" + ",\\\n".join(vals))
            return "\n".join(out) + "\n"
        lines: list[str] = []
        seen: set[str] = set()
        for i, text in enumerate(texts):
            for raw in text.splitlines():
                key = raw.split("#", 1)[0].strip()
                if i == 0:
                    lines.append(raw.rstrip("\r"))
                    if key:
                        seen.add(key)
                elif key and key not in seen:
                    lines.append(key)
                    seen.add(key)
        return "\n".join(lines).rstrip("\n") + "\n"

    def _keep_boot(self, c: _Ctx, ed: PomEditor, declared: str, asis_dir: str, boot: str, comp: str, coords: GAV,
                   lib: GAV, merged_internal: set[str], root: PomEditor | None = None) -> None:
        """남긴 as-is boot 모듈의 pom 조정(소스는 그대로): 합쳐진 모듈에 대한 의존성 → comp(back) 의존성
        하나(첫 compile 선언 자리를 재사용), front 의존성, test-jar, spring-boot-maven-plugin(repackage) 보장,
        WAR / jar 패키징, java 버전, system jar. 역할 부모(라운드 9b)면 부모를 무엇이었든
        ``artifacts.boot_parent`` 로 바꾸고(<relativePath/>, groupId / version 명시) 루트를
        상속했었다면(``root``) 그 속성·관리 항목을 복사하며 필수 사내 아티팩트·BOM·java 버전도 이 pom 에.

        Adjust the kept as-is boot module's pom: dependencies on merged modules → one comp-jar dependency,
        spring-boot-maven-plugin ensured, java level, system jars. Its src/ stays untouched.

        Role parents (round 9b): the parent is replaced by `artifacts.boot_parent` whatever it was (an older
        boot-jar / comp-jar role parent, the root, a starter parent), `<relativePath/>`, explicit groupId / version
        (own or the root's); `root` (given when the as-is module inherited the root pom) supplies the properties /
        management it inherited before; mandatory dependencies, BOM and java level go into this pom."""
        A = c.cfg.artifacts
        role = self._role(c)
        if role:
            old_parent = ed.get_parent()
            if not ed.group_id():
                ed.set_coordinate("groupId", coords.group_id)
            if not ed.version():
                ed.set_coordinate("version", coords.version)
            new_parent = A.boot_parent.gav()                                           # type: ignore[union-attr]
            if old_parent is None or old_parent.key != new_parent.key or old_parent.version != new_parent.version \
                    or ed.parent_relative_path() != "":
                ed.set_parent(new_parent, None)
            self._record_parent(c, c.ws.rel(ed.path), old_parent, new_parent)
        if ed.group_id() and ed.group_id() != coords.group_id:
            lib = GAV(group_id=coords.group_id, artifact_id=lib.artifact_id, version=lib.version)
        if ed.version() and ed.version() != coords.version:
            lib = GAV(group_id=lib.group_id, artifact_id=lib.artifact_id, version=coords.version)
        placed = ed.has_dependency(lib.group_id, lib.artifact_id)
        test_jar = False
        removed: list[str] = []
        for el in ed.dependency_elements(False):
            g = ed.gav_of(el)
            if g.key not in merged_internal:
                continue
            removed.append(g.key)
            # 합쳐진 모듈의 test-jar 의존성은 지우고, 아래에서 comp 의 test-jar 하나로 다시 건다
            if g.type == "test-jar" or g.classifier == "tests":
                test_jar = True
                ed.remove_node(el)
            # 첫 compile 의존성 자리를 comp(back) 의존성으로 바꿔 쓴다: 선언 순서가 유지된다
            elif not placed and (g.scope or "compile") == "compile" and not g.classifier and not g.type:
                ed.set_child_text(el, "groupId", lib.group_id)
                ed.set_child_text(el, "artifactId", lib.artifact_id)
                ed.set_child_text(el, "version", lib.version)
                placed = True
            else:
                ed.remove_node(el)
        if not placed:
            ed._add_dep(lib, managed=False, if_absent=True, first=True)
        for fl in self._front_lib(c, coords):
            if not ed.has_dependency(fl.group_id, fl.artifact_id):
                ed.add_dependency(GAV(group_id=lib.group_id, artifact_id=fl.artifact_id, version=lib.version))
        if test_jar:
            ed.add_dependency(GAV(group_id=lib.group_id, artifact_id=lib.artifact_id, version=lib.version,
                                  type="test-jar", scope="test"))
            self._add_test_jar_goal(c, comp)
        if not ed.has_plugin(*BOOT_PLUGIN, where="build"):
            plugin_version = None if A.parent_manages_spring_boot else c.cfg.target.spring_boot_version
            ed.add_plugin(PluginInfo(group_id=BOOT_PLUGIN[0], artifact_id=BOOT_PLUGIN[1], version=plugin_version),
                          executions=[{"goals": ["repackage"]}])
            c.note(f"spring-boot-maven-plugin (repackage) added to {c.boot_rel}")
        else:
            self._fix_boot_plugin(c, ed)
        if c.boot_war:
            ed.set_packaging("war")
            for g in self._war_deps(c, {d.key for d in ed.dependencies()}):
                ed.add_dependency(g)
            self._apply_overlays(c, ed)
        # JSP 도 overlay 도 없는데 as-is boot 모듈이 WAR 였으면
        # 실행 jar 로 바꾼다(회사 규칙: JSP 가 있을 때만 WAR)
        elif ed.packaging() == "war":
            ed.set_packaging("jar")
            for p in ed.find_plugins(*WAR_PLUGIN, where="build"):
                ed.remove_node(p)
        parent = ed.get_parent()
        if role:
            self._role_module(c, ed, root, exclude=merged_internal)
        elif parent is None or parent.artifact_id not in (coords.artifact_id, c.root_aid_old):
            c.note(f"kept boot module {declared} does not inherit the root pom "
                   f"({parent.key if parent else 'no parent'}); parent left unchanged")
        self._fix_java(c, ed, top=role)
        self._strip_ear_plugin(c, ed)
        ed.remove_if_empty(ed.find("properties"))
        asis = posixpath.normpath(posixpath.join(c.base or ".", asis_dir))
        self._relocate_module_system_deps(c, ed, "" if asis == "." else asis, boot)
        c.save(ed)
        kept_rel = c.boot_rel
        c.result.kept_boot_module = kept_rel
        where = "" if posixpath.normpath(declared) == kept_rel else f" (moved from {declared})"
        c.note(f"as-is boot module {declared} kept as {kept_rel}{where}: sources untouched, dependencies on merged "
               f"modules ({', '.join(sorted(set(removed))) or 'none'}) replaced by {lib.artifact_id}")
        kept_main = [fqcn for fqcn in c.profile.source.main_classes
                     if self._locate_class(c, fqcn, [f"{boot}/src/main/java"])]
        if not kept_main:
            c.note(f"kept boot module {kept_rel} has no @SpringBootApplication class; none was generated")
        elif c.boot_war:
            self._initializer_followup(c, boot, kept_main)
        if c.jsp_moved:
            self._jsp_view_props(c, boot)
        self._web_followups(c)

    # ------------------------------------------------------------------ comp pom
    def _write_comp_pom(self, c: _Ctx, sources: list[PomEditor], coords: GAV, comp: str, asis_packaging: str,
                        exclude: set[str] | None = None, copy_props_from: list[PomEditor] | None = None) -> None:
        """comp(back) 모듈 pom 생성: 역할 부모면 ``comp_parent`` + 자기 좌표(루트에서 아무것도 상속하지
        않음), 아니면 루트 pom 이 부모. as-is pom 들의 의존성(필수 사내 아티팩트, remove_when_present, 내부
        모듈, WAR overlay 제외)과 build 디렉토리 / 플러그인(boot / war / ear 플러그인 제외)을 복사한다.
        여러 모듈을 합쳤으면 리소스 디렉토리는 합집합(``_union_resources``), 모듈 속성도 복사."""
        L = c.cfg.layout
        A = c.cfg.artifacts
        path = f"{comp}/pom.xml"
        role = self._role(c)
        if role:            # round 9b: external in-house comp parent, own coordinates (nothing inherited from root)
            parent = A.comp_parent.gav()                                               # type: ignore[union-attr]
            c.write(path, render("comp-pom.xml.j2", parent=parent, relative_path="", group_id=coords.group_id,
                                 version=coords.version, artifact_id=coords.artifact_id + L.comp_artifact_suffix,
                                 name=None))
            self._record_parent(c, path, None, parent)
        else:
            root_pom = c.p("pom.xml")
            rel_parent = posixpath.relpath(root_pom, comp)
            c.write(path, render("comp-pom.xml.j2", parent=coords, relative_path=rel_parent, group_id=None,
                                 version=None, artifact_id=coords.artifact_id + L.comp_artifact_suffix, name=None))
        ed = PomEditor(c.ws.abs(path))
        drop = set(A.remove_when_present) | {s.key for s in A.mandatory} | set(exclude or ())
        seen_deps: set[str] = set()
        seen_plugins: set[str] = set()
        for src in sources:
            asis_dir = posixpath.dirname(c.ws.rel(src.path))
            asis_dir = "" if asis_dir in ("", ".") else asis_dir
            for d in src.dependency_elements(False):
                g = src.gav_of(d)
                ident = f"{g.key}:{g.type or 'jar'}:{g.classifier or ''}"
                if g.key in drop or ident in seen_deps:
                    continue
                # war 타입 의존성은 jar 모듈의 classpath 에 쓸모가 없다(overlay 는 war 패키징에서만 동작):
                # overlay 는 _plan_overlays 가 boot 로
                if (g.type or "") == "war":            # a WAR overlay / web module is never a jar module dependency
                    continue
                # 컨테이너 의존성은 boot 모듈이 정한다(WAR 면 거기서
                # provided, jar 면 내장 Tomcat): comp 에서는 뺀다
                if asis_packaging == "war" and g.key == f"{BOOT_GROUP}:spring-boot-starter-tomcat" \
                        and g.scope == "provided":
                    c.note(f"provided spring-boot-starter-tomcat dropped from {comp} (declared provided in the boot "
                           "WAR)" if c.boot_war else "provided spring-boot-starter-tomcat dropped (WAR → executable jar)")
                    continue
                seen_deps.add(ident)
                new_el = ed.copy_into(ed.ensure_path("dependencies"), d)
                self._relocate_system_dep(c, ed, new_el, asis_dir, comp, src.properties())
            build = src.find("build")
            # EAR 의 build 설정(maven-ear-plugin 등)은 Boot jar 에 의미가 없어 가져오지 않는다
            if build is None or src.packaging() == "ear":      # an EAR's build (maven-ear-plugin …) is not carried
                continue
            for name in BUILD_TO_COMP:
                if name in RESOURCE_SECTIONS and len(sources) > 1:
                    continue                           # merged modules: union below
                el = src.child(build, name)
                if el is not None and ed.find(f"build/{name}") is None:
                    self._remap_dirs(c, ed, ed.copy_into(ed.ensure_path("build"), el, ordered=True), src)
            for p in src.plugin_elements("build"):
                key = src.plugin_key(p)
                if key in (":".join(BOOT_PLUGIN), ":".join(WAR_PLUGIN), ":".join(EAR_PLUGIN)) or key in seen_plugins \
                        or key in drop:
                    continue
                seen_plugins.add(key)
                ed.copy_into(ed.ensure_path("build/plugins"), p)
        if len(sources) > 1:
            self._union_resources(c, ed, sources)
        root_props = sources[0].properties() if sources else {}
        if role and sources:
            self._role_module(c, ed, sources[0], exclude=set(exclude or ()), all_props=True)
        for src in copy_props_from or ():
            for k, v in src.properties().items():
                if k not in root_props and not ed.has_property(k) and k != NAME_PROP:
                    ed.set_property(k, v)
        self._fix_java(c, ed, top=role)
        self._strip_war_web_config(c, ed)
        self._strip_ear_plugin(c, ed)
        ed.save()

    def _remap_dirs(self, c: _Ctx, ed: PomEditor, el: Any, src: PomEditor) -> None:
        """comp pom 에 복사한 <build> 디렉토리 요소(소스 디렉토리, <resources> / <testResources>, <resource>
        하나)의 ``../`` 경로를 comp 안으로 옮겨진 위치(``c.relocated``)로 고친다.

        A `<build>` directory element copied from `src` into the comp pom (source directories, `<resources>` /
        `<testResources>` or one `<resource>`): `../` directories that were folded into the comp module
        (`c.relocated`) point at the moved location."""
        name = PomEditor.local_name(el)
        if name in RESOURCE_SECTIONS:
            holders = [ed.child(r, "directory") for r in ed.children(el, RESOURCE_SECTIONS[name][0])]
        elif name in ("resource", "testResource"):
            holders = [ed.child(el, "directory")]
        else:
            holders = [el]
        for h in holders:
            if h is None or not (h.text or "").strip():
                continue
            d = clean_pom_dir(h.text, allow_parent=True)
            if not d or not d.startswith("../"):
                continue
            new = self._comp_dir(c, src, d)
            if new:
                old = h.text.strip()
                h.text = new
                c.note(f"{c.ws.rel(ed.path)}: <{PomEditor.local_name(h)}> {old} → {new} (the directory moved into "
                       f"{c.comp_rel})")

    def _union_resources(self, c: _Ctx, ed: PomEditor, sources: list[PomEditor]) -> None:
        """여러 모듈을 합친 comp pom 의 <resources> / <testResources> = 모든 모듈의 실제 리소스 디렉토리
        합집합(디렉토리 + targetPath 로 중복 제거, 먼저 것이 이김). 어떤 jar 모듈이 Maven 기본 디렉토리에
        기댔으면 기본 디렉토리도 넣는다 — 한 모듈의 ``src/main/config``
        가 다른 모듈의 ``src/main/resources`` 를 가리지 않게.

        Merged modules (sources[0] = the root pom, its `<build>` inherited by every module): comp-jar's
        `<resources>` / `<testResources>` = the union of every module's effective resource directories (deduped by
        directory + targetPath, first wins). The Maven default directory is added when some jar module relied on it
        (declared none and inherits none), so a custom `src/main/config` of one module does not hide the others'
        `src/main/resources`. Directories are module-relative and the files moved with the same relative path."""
        for sec, (item, default) in RESOURCE_SECTIONS.items():
            def items_of(src: PomEditor, sec: str = sec, item: str = item) -> list[Any]:
                b = src.find("build")
                return src.children(src.child(b, sec), item) if b is not None else []
            root_items = items_of(sources[0])
            entries: list[tuple[PomEditor, Any]] = []
            seen: set[tuple[str, str]] = set()
            relies_default = False
            for i, src in enumerate(sources):
                own = items_of(src)
                # 리소스 디렉토리를 선언하지 않은 jar 모듈은 Maven
                # 기본(src/main/resources)을 썼다 — 루트도 선언이 없을 때만
                if i > 0 and not own and src.packaging() not in ("pom", "ear"):
                    relies_default = relies_default or not root_items
                for r in own:
                    raw = src.text_of(r, "directory") or default
                    d = re.sub(r"^\$\{(?:project\.)?basedir\}/?", "", raw).strip()
                    cd = clean_pom_dir(raw, allow_parent=True)
                    if cd and cd.startswith("../"):
                        d = self._comp_dir(c, src, cd) or d
                    key = (d.strip("/"), (src.text_of(r, "targetPath") or "").strip())
                    if key in seen:
                        continue
                    seen.add(key)
                    entries.append((src, r))
            if not entries:
                continue
            container = ed.ensure_path(f"build/{sec}")
            if relies_default and (default, "") not in seen:
                el = ed._build_value(container, item, {"directory": default})
                container.remove(el)
                ed._append(container, el)
                ed.reindent(el)
            for src, r in entries:
                self._remap_dirs(c, ed, ed.copy_into(container, r), src)
            dirs = [ed.text_of(x, "directory") or default for x in ed.children(container, item)]
            c.note(f"comp-jar <{sec}>: union of the merged modules' resource directories ({', '.join(dirs)})")

    # ------------------------------------------------------------------ system-scoped jars (E7)
    def _relocate_system_dep(self, c: _Ctx, ed: PomEditor, el: Any, asis_dir: str, module_dir: str,
                             props: dict[str, str] | None = None) -> None:
        """``system`` scope 의존성의 jar 를 지금 선언한 모듈에서 찾을 수 있게(E7): jar 를
        ``<module>/lib/<name>`` 로 복사하고 systemPath 를 ``${project.basedir}/lib/<name>`` 로. 없거나
        프로젝트 밖 jar 는 선언을 그대로 두고 후속 작업. 언제나 'Nexus 에 설치해 일반 의존성으로' 후속 작업.

        Keep a `system`-scoped dependency's jar reachable from the module that now declares it: copy the jar to
        `<module>/lib/<name>` and point `systemPath` at `${project.basedir}/lib/<name>`. Missing / external jars keep
        their declaration and get a follow-up."""
        from sbmig.analyze.edges import resolve_system_path

        g = ed.gav_of(el)
        raw = ed.text_of(el, "systemPath")
        if (g.scope or "") != "system" or not raw:
            return
        install = (f"mvn install:install-file -Dfile=<jar> -DgroupId={g.group_id} -DartifactId={g.artifact_id} "
                   f"-Dversion={g.version or '<version>'} -Dpackaging=jar")
        resolved, inside = resolve_system_path(raw, asis_dir, {**ed.properties(), **(props or {})}, c.ws.root)
        if resolved is None or not inside:
            c.followup(f"system-scoped dependency {g.key} ({raw}) points outside the project or cannot be resolved; "
                       f"declaration kept — install the jar into Nexus ({install}) and use a normal dependency")
            return
        cur = c.new_location(resolved)
        name = posixpath.basename(resolved)
        if not c.ws.abs(cur).is_file():
            c.followup(f"system-scoped jar `{name}` ({g.key}, systemPath {raw}) is missing from the project; "
                       f"declaration kept — install it into Nexus ({install.replace('<jar>', name)}) and use a "
                       "normal dependency")
            return
        dst = f"{module_dir}/lib/{name}"
        if dst != cur:
            # 같은 이름의 다른 jar 가 이미 lib/ 에 있으면 artifactId 를 앞에 붙여 둘 다 살린다
            if c.ws.abs(dst).exists() and c.ws.abs(dst).read_bytes() != c.ws.abs(cur).read_bytes():
                name = f"{g.artifact_id}-{name}"
                dst = f"{module_dir}/lib/{name}"
            if not c.ws.abs(dst).exists():
                c.ws.abs(dst).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(c.ws.abs(cur), c.ws.abs(dst))
                c.changed.add(dst)
                c.note(f"system-scoped jar {cur} copied to {dst} ({g.key})")
        new_path = f"${{project.basedir}}/lib/{name}"
        if raw.strip() != new_path:
            ed.set_child_text(el, "systemPath", new_path)
            c.note(f"systemPath of {g.key} in {module_dir}/pom.xml rewritten to {new_path}")
        c.followup(f"system-scoped jar `{name}` ({g.key}) should be installed into Nexus "
                   f"({install.replace('<jar>', dst)}) and declared as a normal dependency")

    def _relocate_module_system_deps(self, c: _Ctx, ed: PomEditor, asis_dir: str, module_dir: str) -> None:
        """pom 하나의 모든 system scope 의존성(직접 + 관리)에 ``_relocate_system_dep`` 적용."""
        for el in [*ed.dependency_elements(False), *ed.dependency_elements(True)]:
            self._relocate_system_dep(c, ed, el, asis_dir, module_dir)

    # ------------------------------------------------------------------ boot-jar content
    def _populate_boot(self, c: _Ctx, src_dirs: list[str], boot: str) -> tuple[list[str], list[str]]:
        """boot 모듈로 옮길 것: boot 리소스(``layout.boot_resources_glob``, resources 루트와 config/),
        @SpringBootApplication 클래스, @SpringBootTest 테스트, SpringBootServletInitializer 하위 클래스,
        ``split_policy: web-layer`` 면 웹 계층(@Controller / @Configuration ...)까지. 기본 ``minimal`` 은
        나머지를 전부 comp 에 둔다(회사 규칙: back 에 컨트롤러를 포함한 모든 소스).

        Move boot resources, @SpringBootApplication / @SpringBootTest classes (+ web layer) into boot-jar."""
        L = c.cfg.layout
        src = c.profile.source
        split = c.params.get("split_policy", L.split_policy)
        boot_tests = list(src.spring_boot_test_classes)
        for sd in src_dirs:
            for kind in ("main", "test"):
                if kind == "test" and not boot_tests:
                    continue
                res = f"{sd}/src/{kind}/resources"
                for f in self._boot_resource_files(c, res):
                    dst = f"{boot}/src/{kind}/resources/{f[len(res) + 1:]}"
                    if c.ws.exists(dst):
                        c.note(f"boot resource {f} not moved: {dst} already exists")
                        continue
                    c.move(f, dst)
        moved_main: list[str] = []
        main_roots = [f"{sd}/src/main/java" for sd in src_dirs]
        for fqcn in src.main_classes:
            f = self._locate_class(c, fqcn, main_roots)
            if not f:
                c.note(f"@SpringBootApplication class {fqcn} not found in {', '.join(main_roots)}")
                continue
            root = next(r for r in main_roots if f.startswith(r + "/"))
            c.move(f, f"{boot}/src/main/java/{f[len(root) + 1:]}")
            moved_main.append(fqcn)
        moved_tests: list[str] = []
        test_roots = [f"{sd}/src/test/java" for sd in src_dirs]
        for fqcn in boot_tests:
            f = self._locate_class(c, fqcn, test_roots)
            if not f:
                c.note(f"@SpringBootTest class {fqcn} not found")
                continue
            root = next(r for r in test_roots if f.startswith(r + "/"))
            c.move(f, f"{boot}/src/test/java/{f[len(root) + 1:]}")
            moved_tests.append(fqcn)
        if moved_tests:
            c.followup(f"@SpringBootTest tests moved to {c.boot_rel} ({len(moved_tests)}); test "
                       "helpers they reference from comp-jar's src/test are wired through a comp test-jar dependency "
                       "when detected, test resources outside the boot resource globs stay in comp-jar")
        for r in main_roots:
            rabs = c.ws.abs(r)
            if not rabs.exists():
                continue
            for f in sorted(rabs.rglob("*.java")):
                text = f.read_text(encoding="utf-8", errors="replace")
                is_init = bool(SERVLET_INIT_RE.search(text))
                # SpringBootServletInitializer 하위 클래스는 정책과 상관없이
                # boot 로: 외부 Tomcat 이 WAR 를 기동하는 진입점이다
                is_web = split == "web-layer" and bool(WEB_LAYER_RE.search(text))
                if is_init or is_web:
                    rel = f.relative_to(rabs).as_posix()
                    c.move(f"{r}/{rel}", f"{boot}/src/main/java/{rel}")
                    if is_init:
                        c.note(f"SpringBootServletInitializer subclass {rel} moved to boot-jar")
        return moved_main, moved_tests

    def _boot_resource_files(self, c: _Ctx, res_dir: str) -> list[str]:
        """boot 리소스 파일 목록: resources 루트와 ``config/`` 바로 아래에서 ``layout.boot_resources_glob``
        (application*.yml / properties, bootstrap*, logback*.xml, banner.txt ...)에 맞는 파일."""
        out: list[str] = []
        for sub in ("", "config"):
            d = c.ws.abs(f"{res_dir}/{sub}" if sub else res_dir)
            if not d.is_dir():
                continue
            for f in sorted(d.iterdir()):
                if f.is_file() and any(fnmatch.fnmatch(f.name, g) for g in c.cfg.layout.boot_resources_glob):
                    out.append(f"{res_dir}/{sub + '/' if sub else ''}{f.name}")
        return out

    # ------------------------------------------------------------------ externalized-config placeholders (round 8)
    def _placeholder_entries(self, c: _Ctx, under: str | None = None, others: tuple[str, ...] = (),
                             service: tuple[str, ...] | None = None) -> list[tuple[str, str, str]]:
        """외부 설정 placeholder 목록 (boot 키, 값, 주석): 분석이 ``external`` 로 판정한 속성(코드가 읽지만
        패키지된 리소스 어디에도 정의가 없는 키 — as-is 에서는 컨테이너의 JNDI / context-param / -D 가
        줬다)마다, 키별 첫 발견이 이김, 정렬. MSA 는 서비스별 인벤토리가 있으면 ``service`` 이름으로, 없으면
        ``under`` 디렉토리 아래에서 참조된 것 + 어느 서비스에도 속하지 않는 공유 코드의 것.

        (boot_key, value, comment) for every `external` finding, deduped by key (first finding wins), sorted.
        MSA: `service` (the names a service root answers to: `ServiceInfo.name`, its directory, the directory's
        basename) selects by `PropertyFinding.service` when the inventory is per service (round 9: findings of that
        service + `service is None` shared ones). With a project-wide inventory (no finding carries a service)
        `under` keeps only findings with a reference below that directory — plus findings referenced below none of
        `others` (shared library code) — so each MSA service gets the keys its own code reads."""
        X = c.cfg.externalized
        inv = getattr(c.profile, "properties", None)
        if inv is None:
            return []
        per_service = service is not None and (bool(inv.services) or any(f.service for f in inv.findings))
        out: dict[str, tuple[str, str]] = {}
        for f in inv.findings:
            if f.status != "external" or not f.boot_keys:
                continue
            refs = list(f.refs)
            # 테스트만 읽는 키도 기본적으로 placeholder 를
            # 쓴다(include_test_refs): 테스트 컨텍스트가 뜨려면 값이 있어야 함
            if not X.include_test_refs and refs and all(r.scope == "test" for r in refs):
                continue
            if per_service:
                if f.service is not None and f.service not in (service or ()):
                    continue
            elif under is not None:
                def below(r: Any, d: str) -> bool:
                    return d in ("", ".") or r.file == d or r.file.startswith(d.rstrip("/") + "/")
                if refs and not any(below(r, under) for r in refs) and \
                        any(below(r, o) for r in refs for o in others):
                    continue
            ref = refs[0] if refs else None
            value = X.sentinel
            # web.xml context-param 은 as-is 값이 있으니 sentinel 대신 그 값을 그대로 쓴다
            if ref is not None and ref.source == "context-param" and ref.default not in (None, ""):
                value = str(ref.default)
            comment = "# from " + (self._ref_origin(ref) if ref is not None else f.key)
            if f.note:
                comment += f" ({f.note})"
            for k in f.boot_keys:
                k = k.strip()
                if k and k not in out:
                    out[k] = (value, " ".join(comment.split()))
        return [(k, v, cm) for k, (v, cm) in sorted(out.items())]

    @staticmethod
    def _ref_origin(ref: Any) -> str:
        """placeholder 주석의 출처 표기(``@Value Foo.java:12``, ``JNDI jdbc/ds`` ...)."""
        label = PLACEHOLDER_SOURCE_LABELS.get(ref.source, ref.source)
        if ref.source == "jndi":
            return f"{label} {ref.key}"
        where = ref.file + (f":{ref.line}" if ref.line else "")
        if ref.source == "context-param":
            return f"{label} {ref.key} {where}"
        return f"{label} {where}"

    def _write_placeholders(self, c: _Ctx, resources_dir: str, *, under: str | None = None,
                            others: tuple[str, ...] = (), service: tuple[str, ...] | None = None) -> list[str]:
        """``<resources_dir>/<placeholder_file>`` (기본 application.properties)에 sbmig 외부 설정 블록을
        덧붙이거나 바꾼다. 값은 sentinel(``__SET_ME__``): 운영자가 환경 변수 / 외부 파일 / -D 로 채운다. 표식
        밖의 내용과 줄 끝은 바이트 그대로, 두 번 돌려도 같은 바이트(멱등). 쓴 키 목록을 돌려준다.

        Append (or replace) the sbmig externalized-config block in `<resources_dir>/<placeholder_file>`.
        Returns the keys written ([] when disabled / nothing to write). Idempotent: a second run yields the same bytes;
        everything outside the markers is kept byte-for-byte, the file's line endings are kept."""
        X = c.cfg.externalized
        if not X.enabled or not X.write_placeholders:
            return []
        entries = self._placeholder_entries(c, under, others, service)
        if not entries:
            return []
        rel = posixpath.normpath(f"{resources_dir}/{X.placeholder_file}")
        existed = c.ws.exists(rel)
        text = c.ws.read(rel) if existed else ""
        nl = "\r\n" if "\r\n" in text else "\n"
        lines = list(PLACEHOLDER_HEADER)
        # application.yml 만 있던 프로젝트: yml 을 고치지 않고 properties 를 새로 만든다(Boot 는 둘 다 읽음)
        if not existed and X.placeholder_file == "application.properties" and \
                c.ws.exists(posixpath.join(resources_dir, "application.yml")):
            lines.append("# --- application.yml exists; placeholders go to application.properties (Boot loads both)")
        for k, v, cm in entries:
            lines += [cm, f"{k}={v}"]
        lines.append(PLACEHOLDER_END)
        block = nl.join(lines) + nl
        start = text.find(PLACEHOLDER_BEGIN)
        end = text.find(PLACEHOLDER_END, start) if start >= 0 else -1
        # 이전 실행의 블록이 있으면 그 자리만 바꾼다: 표식 밖은 그대로라 재실행해도 같은 바이트(멱등)
        if start >= 0 and end >= 0:
            tail = end + len(PLACEHOLDER_END)
            if text.startswith("\r\n", tail):
                tail += 2
            elif text.startswith("\n", tail):
                tail += 1
            new = text[:start] + block + text[tail:]
        else:
            sep = "" if not text or text.endswith(("\n", "\r")) else nl
            new = (text + sep + nl + block) if text.strip() else block
        if new != text:
            c.write(rel, new, generated=not existed)
        keys = [k for k, _, _ in entries]
        if rel not in c.result.placeholder_files:
            c.result.placeholder_files.append(rel)
        for k in keys:
            if k not in c.result.placeholders_written:
                c.result.placeholders_written.append(k)
        c.note(f"{len(keys)} externalized-config placeholder(s) written to {rel}")
        return keys

    def _placeholder_followup(self, c: _Ctx) -> None:
        """placeholder 를 쓴 파일이 있으면 '실행 전에 값을 채워야 함' 후속 작업
        하나(Handover 'Properties to set')."""
        files = c.result.placeholder_files
        if not files:
            return
        n = len(c.result.placeholders_written)
        c.followup(f"{n} externalized key(s) have placeholder values ({c.cfg.externalized.sentinel}) in "
                   f"{', '.join(files)} — set them before running (see Handover 'Properties to set')")

    def _locate_class(self, c: _Ctx, fqcn: str, roots: list[str]) -> str | None:
        """FQCN 의 소스 파일 찾기: 먼저 패키지 경로 그대로(중첩 클래스는 바깥 클래스 파일), 없으면 같은
        이름 파일 중 package 선언이 맞는 것 — 디렉토리가 패키지와 다른 레거시 소스 때문."""
        parts = fqcn.split(".")
        for root in roots:
            for i in range(len(parts), 0, -1):       # Outer.Inner → Outer.java
                cand = f"{root}/{'/'.join(parts[:i])}.java"
                if c.ws.exists(cand):
                    return cand
        simple = parts[-1]
        pkg = ".".join(parts[:-1])
        for root in roots:
            rabs = c.ws.abs(root)
            if not rabs.exists():
                continue
            for f in rabs.rglob(f"{simple}.java"):
                m = PACKAGE_RE.search(f.read_text(encoding="utf-8", errors="replace"))
                if (m.group(1) if m else "") == pkg:
                    return f"{root}/{f.relative_to(rabs).as_posix()}"
        return None

    def _write_boot(self, c: _Ctx, coords: GAV, boot: str, lib_deps: list[GAV], asis_deps: list[GAV],
                    moved_main: list[str], moved_tests: list[str], final_name: str | None,
                    comp_dirs: list[str], *, root: PomEditor | None = None,
                    exclude: set[str] | None = None) -> None:
        """생성 boot 모듈 pom + 내용: starter(``layout.starter_by_case``; spring_boot 는 auto = as-is 에
        starter 가 없을 때만), boot WAR 면 컨테이너 의존성(``_war_deps``), JSP + 실행 jar 면 jasper(+ JSP 를
        서비스하지 못한다는 후속 작업), 옮긴 @SpringBootTest 의 테스트 classpath, repackage 플러그인. 역할
        부모(라운드 9b)면 부모 = ``boot_parent`` (<relativePath/>, groupId / version 명시)에 ``root`` 의
        속성·관리 항목, 필수 사내 아티팩트, BOM, java 버전. 이어서 Application 생성(옮긴
        @SpringBootApplication 이 없을 때), JSP 뷰 설정, web.xml 등록 클래스, web.xml 후속 작업.

        Generated boot-jar pom. Role parents (round 9b): parent = `artifacts.boot_parent` (`<relativePath/>`),
        explicit groupId / version, `root`'s properties / management copied in, mandatory dependencies, BOM and
        java level in this pom."""
        L = c.cfg.layout
        A = c.cfg.artifacts
        case = c.profile.case_type
        case_key = case.value if isinstance(case, CaseType) else str(case)
        deps = list(lib_deps)
        starter = c.params.get("starter") or L.starter_by_case.get(case_key, "spring-boot-starter")
        # Boot as-is(auto): as-is 에 이미 starter 가 있으면 더하지 않는다 — 원래 고른 starter 를 존중
        if starter == "auto":
            has_starter = any(d.group_id == BOOT_GROUP and d.artifact_id.startswith("spring-boot-starter")
                              and d.scope != "test" for d in asis_deps)
            starter = None if has_starter else "spring-boot-starter"
        if starter:
            sg = GAV.parse(starter) if ":" in starter else GAV(group_id=BOOT_GROUP, artifact_id=starter)
            deps.append(GAV(group_id=sg.group_id, artifact_id=sg.artifact_id, version=sg.version))
        src = c.profile.source
        if c.boot_war:
            deps.extend(self._war_deps(c, {d.key for d in deps}))
        # JSP 가 있는데 jar 로 두라는 설정(jsp_boot_packaging: jar): 실행 jar 는 JSP 를 서비스하지 못하므로
        # jasper 를 넣되 후속 작업으로 경고
        elif c.jsp_moved:
            deps.append(GAV(group_id="org.apache.tomcat.embed", artifact_id="tomcat-embed-jasper"))
            deps.extend(JSTL_DEPS if c.jstl else [])
            c.note(f"tomcat-embed-jasper added to {c.boot_rel} for JSP support (layout.jsp_boot_packaging=jar)")
            c.followup(f"JSPs are in the front jar ({c.front_rel}/{c.cfg.layout.front_jsp_dir}) but {c.boot_rel} is an "
                       "executable jar: Spring Boot does not serve JSPs from an executable jar — set "
                       "layout.jsp_boot_packaging: war or replace the JSPs")
        elif src.jsp_count > 0:
            deps.append(GAV(group_id="org.apache.tomcat.embed", artifact_id="tomcat-embed-jasper"))
            c.note("tomcat-embed-jasper added to boot-jar for JSP support")
            c.followup("JSPs under src/main/webapp require tomcat-embed-jasper and may need relocation to "
                       "src/main/resources/META-INF/resources to be served from an executable jar")
        if moved_tests:
            # the moved @SpringBootTest classes need the SAME test classpath they had in the as-is module
            # (starter-test, *-test modular starters, security-test, h2, testcontainers ...), not just starter-test
            have = {d.key for d in deps}
            test_deps = [d for d in asis_deps if d.scope == "test" and d.key not in have]
            if not any(d.artifact_id == "spring-boot-starter-test" for d in test_deps) \
                    and "org.springframework.boot:spring-boot-starter-test" not in have:
                test_deps.insert(0, GAV(group_id=BOOT_GROUP, artifact_id="spring-boot-starter-test", scope="test"))
            deps.extend(test_deps)
            # runtime-scoped deps (embedded DB drivers etc.) are needed to boot the context in tests
            deps.extend(d for d in asis_deps if d.scope == "runtime" and d.key not in have
                        and d.key not in {t.key for t in test_deps})
            # test helpers the moved tests use from comp-jar's src/test → comp test-jar on the boot test classpath
            for lib, comp_dir in zip(lib_deps, comp_dirs):
                refs = self._test_helper_refs(c, comp_dir, boot)
                if not refs:
                    continue
                deps.append(GAV(group_id=lib.group_id, artifact_id=lib.artifact_id, version=lib.version,
                                type="test-jar", scope="test"))
                self._add_test_jar_goal(c, comp_dir)
                c.note(f"moved @SpringBootTest classes use test classes of {comp_dir} ({', '.join(refs[:5])}"
                       f"{' …' if len(refs) > 5 else ''}) → {lib.artifact_id} test-jar dependency added to "
                       f"{posixpath.basename(c.boot_rel)} and maven-jar-plugin test-jar goal added to {comp_dir}")
        plugin_version = None if A.parent_manages_spring_boot else c.cfg.target.spring_boot_version
        # @SpringBootApplication 이 여럿이면 repackage 가 main
        # 클래스를 고르지 못해 빌드가 실패한다: 첫 번째로 고정
        main_class = moved_main[0] if len(moved_main) > 1 else None
        if main_class:
            c.note(f"several @SpringBootApplication classes; repackage mainClass set to {main_class}")
        role = self._role(c)
        if role:
            parent, rel_parent, gid, ver = A.boot_parent.gav(), "", coords.group_id, coords.version  # type: ignore
        else:
            parent, rel_parent, gid, ver = coords, posixpath.relpath(c.p("pom.xml"), boot), None, None
        c.write(f"{boot}/pom.xml", render(
            "boot-pom.xml.j2", parent=parent, relative_path=rel_parent, group_id=gid, version=ver,
            artifact_id=coords.artifact_id + L.boot_artifact_suffix, name=None, dependencies=deps,
            plugin_version=plugin_version, main_class=main_class, final_name=final_name,
            packaging="war" if c.boot_war else "jar"))
        # overlay 를 역할 부모 처리보다 먼저 넣는다: overlay 버전이 쓰는 속성이 루트에서 함께 복사되도록
        if c.overlay_carry:                 # before _role_module: the overlay versions' properties come along
            ov_ed = PomEditor(c.ws.abs(f"{boot}/pom.xml"))
            self._apply_overlays(c, ov_ed)
            c.save(ov_ed)
        if role:
            self._record_parent(c, f"{boot}/pom.xml", None, parent)
            ed = PomEditor(c.ws.abs(f"{boot}/pom.xml"))
            self._role_module(c, ed, root, exclude=set(exclude or ()))
            self._fix_java(c, ed, top=True)
            c.save(ed)
        boot_ed = PomEditor(c.ws.abs(f"{boot}/pom.xml"))
        self._strip_war_web_config(c, boot_ed)
        self._strip_ear_plugin(c, boot_ed)
        c.save(boot_ed)
        if not moved_main:
            self._generate_application(c, boot, comp_dirs)
        elif c.boot_war:
            self._initializer_followup(c, boot, moved_main)
        if c.jsp_moved:
            self._jsp_view_props(c, boot)
        self._web_xml_registrations(c, boot, comp_dirs, moved_main)
        self._web_followups(c)

    def _test_helper_refs(self, c: _Ctx, comp_dir: str, boot: str) -> list[str]:
        """boot 모듈로 옮긴 테스트가 참조하는, comp 의 src/test/java 에 남은 테스트 클래스 이름들(주석을 뺀 뒤
        단어 일치) — 있으면 comp test-jar 의존성을 건다.

        Simple names of test classes left in `comp_dir/src/test/java` referenced by boot-jar's test sources."""
        helpers_root = c.ws.abs(f"{comp_dir}/src/test/java")
        boot_tests = c.ws.abs(f"{boot}/src/test/java")
        if not helpers_root.is_dir() or not boot_tests.is_dir():
            return []
        moved = sorted(boot_tests.rglob("*.java"))
        names = sorted({f.stem for f in helpers_root.rglob("*.java")} - {f.stem for f in moved})
        if not names:
            return []
        text = "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in moved)
        text = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.DOTALL)
        return [n for n in names if re.search(rf"\b{re.escape(n)}\b", text)]

    def _add_test_jar_goal(self, c: _Ctx, comp_dir: str) -> None:
        """comp pom 의 maven-jar-plugin 에 test-jar goal 을 보장한다(없으면 플러그인 추가, 있으면 execution
        추가) — boot 테스트가 comp 테스트 클래스를 쓰려면 필요."""
        pom = f"{comp_dir}/pom.xml"
        if not c.ws.exists(pom):
            return
        ed = PomEditor(c.ws.abs(pom))
        existing = ed.find_plugins(*JAR_PLUGIN, where="build")
        if not existing:
            ed.add_plugin(PluginInfo(group_id=JAR_PLUGIN[0], artifact_id=JAR_PLUGIN[1]),
                          executions=[{"goals": ["test-jar"]}])
        else:
            p = existing[0]
            goals = [(g.text or "").strip() for g in p.iter()
                     if isinstance(g.tag, str) and PomEditor.local_name(g) == "goal"]
            if "test-jar" not in goals:
                ex = ed.child(p, "executions")
                if ex is None:
                    ex = ed._build_value(p, "executions", None)
                ed._build_value(ex, "execution", {"goals": ["test-jar"]})
                ed.reindent(p)
        c.save(ed)

    def _generate_application(self, c: _Ctx, boot: str, comp_dirs: list[str]) -> None:
        """boot 모듈에 Application 클래스 생성(``templates/Application.java.j2``): 패키지 = 분석의
        base package(모르면 ``com.company.app`` + 경고), comp 에 같은 FQCN 이 있으면 ``Boot`` 접두사, servlet
        케이스면 @ServletComponentScan, boot WAR 면 SpringBootServletInitializer 상속, XML 빈 설정은 boot 의
        ``legacy-config/`` 에 사본을 만들어 @ImportResource. 기존 main
        클래스는 comp 에 남는다(후속 작업)."""
        L = c.cfg.layout
        src = c.profile.source
        pkg = sanitize_package(src.base_package)
        if not pkg:
            pkg = FALLBACK_PACKAGE
            c.note(f"base package unknown; Application generated in {FALLBACK_PACKAGE}")
            c.profile.warnings.append(f"restructure: base package unknown, used {FALLBACK_PACKAGE}")
        cls = sanitize_identifier(L.application_class_name, "Application")
        pkg_path = pkg.replace(".", "/")
        # comp 에 같은 FQCN 이 있으면 boot jar 와 comp jar 에 같은 클래스가 생긴다: 생성 클래스 이름을 바꾼다
        if any(c.ws.exists(f"{d}/src/main/java/{pkg_path}/{cls}.java") for d in comp_dirs):
            cls = "Boot" + cls
            c.note(f"class {pkg}.{L.application_class_name} already exists in comp-jar; generated {cls}")
        servlet = self._servlet_scan(c)
        imports: list[str] = []
        if L.import_xml_bean_configs and src.xml_bean_configs:
            used: set[str] = set()
            for old in src.xml_bean_configs:
                cur = c.new_location(old)
                if not c.ws.exists(cur):
                    c.note(f"xml bean config {old} not found at {cur}; not imported")
                    continue
                name = posixpath.basename(cur)
                if name in used:
                    name = f"{posixpath.basename(posixpath.dirname(cur))}-{name}"
                used.add(name)
                # 원본은 그 자리에 두고 boot 에 사본을 만든다(@ImportResource
                # 가 boot classpath 의 legacy-config/ 를 읽음)
                c.write(f"{boot}/src/main/resources/legacy-config/{name}", c.ws.read(cur))
                imports.append(f"classpath:legacy-config/{name}")
            if imports:
                c.note(f"{len(imports)} XML bean config(s) copied to boot-jar legacy-config/ and imported")
                c.followup("XML bean configs imported via @ImportResource (" + ", ".join(imports) + ") — verify "
                           "bean overlaps with Boot auto-configuration and relative <import> paths")
        text = render("Application.java.j2", package=pkg, class_name=cls, servlet_component_scan=servlet,
                      servlet_initializer=c.boot_war,
                      servlet_component_scan_fqcn=SERVLET_COMPONENT_SCAN_FQCN, import_resources=imports,
                      case_type=c.profile.case_type.value if isinstance(c.profile.case_type, CaseType)
                      else str(c.profile.case_type))
        c.write(f"{boot}/src/main/java/{pkg_path}/{cls}.java", text)
        rest = [x for x in src.psvm_classes if x not in c.module_mains]     # per-module follow-up (`_merge`)
        if rest:
            c.followup("existing main class(es) " + ", ".join(rest) + " kept in comp-jar; they are "
                       "not invoked by the generated Spring Boot Application")

    def _web_followups(self, c: _Ctx) -> None:
        """web.xml 마다 후속 작업: WebXmlRegistrations 를 만들었으면 '매핑 확인', boot WAR 면
        '참고용으로만 남음(WAR 의 WEB-INF/web.xml 로 패키지되지 않음)', 실행 jar 면 '내장 Tomcat
        은 web.xml 을 읽지 않음'."""
        src = c.profile.source
        scan = c.cfg.layout.add_servlet_component_scan
        for i, p in enumerate(src.web_xml_paths or ([] if not src.has_web_xml else ["src/main/webapp/WEB-INF/web.xml"])):
            loc = c.new_location(p)
            if c.web_xml_class and i == 0:
                where = ("is kept for reference only (not packaged as the WAR's WEB-INF/web.xml)" if c.boot_war
                         else "is not read by embedded Tomcat")
                c.followup(f"web.xml at {loc} {where}; its servlets / filters / listeners / error pages are "
                           f"registered by the generated {c.web_xml_class} — verify the mappings")
                if c.web_xml_summary:
                    c.followup(c.web_xml_summary)
                continue
            if c.boot_war:
                c.followup(f"web.xml at {loc} is kept for reference only (not packaged as the WAR's WEB-INF/web.xml); "
                           + ("servlets/filters are registered via @ServletComponentScan — verify mappings" if scan
                              else "register servlets/filters as beans"))
            elif scan:
                c.followup(f"web.xml at {loc} is not read by embedded Tomcat; servlets/filters are registered via "
                           "@ServletComponentScan — verify mappings")
            else:
                c.followup(f"web.xml at {loc} is not read by embedded Tomcat — register servlets/filters as beans")

    # ------------------------------------------------------------------ web.xml → WebXmlRegistrations (2026-10-23)
    def _web_xml_registrations(self, c: _Ctx, boot: str, comp_dirs: list[str], moved_main: list[str]) -> None:
        """``layout.web_xml_registrations: generate``: 첫 web.xml 의 servlet / filter / listener / error page
        → boot 모듈의 ``<pkg>.WebXmlRegistrations`` (sbmig.restructure.webxml_registrations), session-timeout
        → boot application.properties 의 ``server.servlet.session.timeout``. 빈이 없는 것은 후속 작업, 전부
        Boot / 스캔에 맡겨지면 만들지 않는다.

        `layout.web_xml_registrations: generate`: the first web.xml's servlets / filters / listeners / error pages
        → `<pkg>.WebXmlRegistrations` in the boot module (`sbmig.restructure.webxml_registrations`), its
        session-timeout → `server.servlet.session.timeout` in the boot `application.properties`; what has no bean
        becomes a follow-up. Nothing is generated when every declaration is skipped / annotated."""
        from sbmig.analyze.webxml import parse_web_xml

        L = c.cfg.layout
        src = c.profile.source
        if L.web_xml_registrations != "generate" or not src.web_xml_paths:
            return
        asis = src.web_xml_paths[0]
        loc = c.new_location(asis)
        if not c.ws.exists(loc):
            c.note(f"web.xml {asis} not found at {loc}; no WebXmlRegistrations generated")
            return
        model = parse_web_xml(c.ws.read(loc), asis)
        if not (model.servlets or model.filters or model.listeners or model.error_pages
                or model.session_timeout_minutes is not None):
            return
        rules = load_skip_rules(c.cfg.resolve_path(WEB_XML_RULES))
        search = [d for d in [*comp_dirs, boot] if d]
        cache: dict[str, ClassInfo | None] = {}

        def lookup(fqcn: str) -> ClassInfo | None:
            if fqcn not in cache:
                cache[fqcn] = self._class_info(c, fqcn, search)
            return cache[fqcn]

        web_path = (lambda p: self._jsp_web_path(c, p)) if c.jsp_moved else (lambda p: p)
        plan = plan_registrations(model, rules=rules, lookup=lookup, component_scan=self._servlet_scan(c),
                                  web_path=web_path, war=c.boot_war)
        for n in plan.notes:
            c.note(f"web.xml: {n}")
        for f in plan.followups:
            c.followup(f)
        if plan.session_timeout is not None:
            self._session_timeout_prop(c, boot, plan.session_timeout, asis)
        if not plan.has_beans:
            if plan.skipped or plan.annotated:
                c.note(f"web.xml {asis}: every servlet / filter / listener is provided by Spring Boot or "
                       "@ServletComponentScan; no WebXmlRegistrations generated")
            return
        # Application 과 같은 패키지에 만든다: @SpringBootApplication
        # 의 컴포넌트 스캔 범위 안이어야 이 @Configuration 이 읽힌다
        pkg = sanitize_package(moved_main[0].rsplit(".", 1)[0]) if moved_main and "." in moved_main[0] else None
        pkg = pkg or sanitize_package(src.base_package) or FALLBACK_PACKAGE
        pkg_path = pkg.replace(".", "/")
        cls = WEB_XML_CLASS
        if any(c.ws.exists(f"{d}/src/main/java/{pkg_path}/{cls}.java") for d in search):
            cls = "Sbmig" + cls
        from datetime import date

        from .webxml_registrations import doc_text
        text = render("WebXmlRegistrations.java.j2", package=pkg, class_name=cls, date=date.today().isoformat(),
                      web_xml_path=doc_text(asis), spec=doc_text(model.spec or ""),
                      servlets=plan.servlets, filters=plan.filters, listeners=plan.listeners,
                      error_pages=plan.error_pages,
                      skipped=[{k: doc_text(v) for k, v in s.items()} for s in plan.skipped],
                      notes=[doc_text(n) for n in plan.notes])
        rel = f"{boot}/src/main/java/{pkg_path}/{cls}.java"
        c.write(rel, text)
        fqcn = f"{pkg}.{cls}"
        c.web_xml_class = fqcn
        if "web-xml-registered" not in c.result.flags:
            c.result.flags.append("web-xml-registered")
        skipped = [f"{s['cls'].rsplit('.', 1)[-1]} ({s['reason']})" for s in plan.skipped]
        c.web_xml_summary = (
            f"web.xml ({model.spec or 'spec unknown'}) → {cls}: {len(plan.servlets)} servlet(s), "
            f"{len(plan.filters)} filter(s), {len(plan.listeners)} listener(s), {len(plan.error_pages)} error page(s)"
            + (f"; skipped: {', '.join(skipped)}" if skipped else "")
            + (f"; jsp-file servlets: {', '.join(plan.jsp_file_servlets)}" if plan.jsp_file_servlets else ""))
        c.note(f"web.xml {asis} → {rel} ({len(plan.servlets)} servlet(s), {len(plan.filters)} filter(s), "
               f"{len(plan.listeners)} listener(s), {len(plan.error_pages)} error page(s))")
        try:
            c.ws.journal({"event": "web_xml_registrations", "path": asis, "spec": model.spec,
                          "servlets": len(plan.servlets), "filters": len(plan.filters),
                          "listeners": len(plan.listeners), "error_pages": len(plan.error_pages),
                          "skipped": [s["cls"] for s in plan.skipped],
                          "jsp_file_servlets": list(plan.jsp_file_servlets), "class": fqcn})
        except (OSError, AttributeError) as e:  # pragma: no cover - journal unavailable (tests without a run dir)
            log.debug("journal web_xml_registrations skipped: %s", e)

    def _class_info(self, c: _Ctx, fqcn: str, dirs: list[str]) -> ClassInfo | None:
        """web.xml 의 클래스 FQCN → 프로젝트 소스(``dirs`` 아래, target/ 제외)의 ClassInfo(@Web* 어노테이션,
        HttpServlet 직접 상속); 없으면 None(라이브러리 클래스).

        The project source of `fqcn` below `dirs` (any source root; `target/` skipped), annotated / HttpServlet."""
        outer = fqcn.split("$", 1)[0]
        pkg, _, simple = outer.rpartition(".")
        for d in dirs:
            base = c.ws.abs(d)
            if not base.is_dir():
                continue
            for f in base.rglob(f"{simple}.java"):
                if is_build_output_path(c.ws.rel(f), c.ws.root):
                    continue
                text = f.read_text(encoding="utf-8", errors="replace")
                m = PACKAGE_RE.search(text)
                if (m.group(1) if m else "") != pkg:
                    continue
                return ClassInfo(path=c.ws.rel(f),
                                 annotated=bool(re.search(r"@(?:[\w.]+\.)?Web(?:Servlet|Filter|Listener)\b", text)),
                                 http_servlet="$" not in fqcn and bool(
                                     re.search(r"\bextends\s+(?:[\w.]+\.)?HttpServlet\b", text)))
        return None

    def _session_timeout_prop(self, c: _Ctx, boot: str, minutes: int, asis: str) -> None:
        """web.xml session-timeout(분) → boot application.properties 의
        ``server.servlet.session.timeout=<n>m``. boot 리소스에 이미 설정돼 있으면 그대로 둔다."""
        res = f"{boot}/src/main/resources"
        key = "server.servlet.session.timeout"
        for f in self._boot_resource_files(c, res):
            if key in c.ws.read(f):
                c.note(f"{key} already configured in {f}; web.xml session-timeout {minutes} left unchanged")
                return
        self._append_props(c, f"{res}/application.properties",
                           [f"# sbmig: web.xml session-config ({asis})", f"{key}={minutes}m"])
        c.note(f"{key}={minutes}m written to {res}/application.properties (web.xml session-timeout)")

    def _append_props(self, c: _Ctx, rel: str, lines: list[str]) -> None:
        """properties 파일 끝에 줄들을 덧붙인다(줄 끝 유지, 기존 내용과 빈 줄로 구분; 새
        파일이면 generated 로 기록)."""
        existed = c.ws.exists(rel)
        text = c.ws.read(rel) if existed else ""
        nl = "\r\n" if "\r\n" in text else "\n"
        sep = "" if not text or text.endswith(("\n", "\r")) else nl
        c.write(rel, text + sep + (nl if text.strip() else "") + nl.join(lines) + nl, generated=not existed)

    def _packaging_followups(self, c: _Ctx, asis_packaging: str) -> None:
        """패키징 후속 작업: boot 가 외부 Tomcat 용 WAR 인 이유(overlay / JSP), 또는 as-is WAR
        를 실행 jar 로 바꾼 사실."""
        if c.boot_war and not c.jsp_moved and c.overlay_carry:
            c.followup(f"boot module {c.boot_rel} packaged as WAR for external Tomcat: the WAR overlay(s) "
                       f"{', '.join(g.coordinates for _, g in c.war_overlays)} carry web content")
        elif c.boot_war:
            c.followup("boot module packaged as WAR for external Tomcat; JSPs are served from the front jar "
                       f"(META-INF/resources) — {c.boot_rel}, {c.front_rel}")
        elif asis_packaging == "war":
            c.followup(f"WAR packaging converted to executable jar ({c.boot_rel})")

    # ------------------------------------------------------------------ module names (round 9c)
    @staticmethod
    def _resolve_names(c: _Ctx, root: PomEditor | None) -> None:
        """모듈 이름의 ``{name}``: ``layout.project_name`` → 루트 pom 속성 ``sbmig.project-name`` (intake
        aggregator 의 프로젝트 이름) → 루트 artifactId. 모듈 경로는 ``layout.module_path`` (기본 루트 바로
        아래 ``{name}-boot`` / ``{name}-back`` / ``{name}-front``).

        `{name}` = `layout.project_name`, else the root pom property `sbmig.project-name` (the intake aggregator's
        project name, 2026-10-23), else the root pom's artifactId; module paths via `layout.module_path`."""
        L = c.cfg.layout
        aid = (root.artifact_id() if root is not None else None) or \
            (c.profile.root_pom.artifact_id if c.profile.root_pom else None) or c.profile.name
        prop = ((root.get_property(NAME_PROP) or "").strip() if root is not None else "") or None
        c.name = L.project_name or prop or aid or "app"
        c.boot_rel = L.module_path("boot", c.name) or "boot"
        c.comp_rel = L.module_path("comp", c.name) or "back"
        c.front_rel = L.module_path("front", c.name)

    @staticmethod
    def _module_tops(c: _Ctx) -> list[str]:
        """회사 모듈 경로의 최상위 디렉토리들(소스 루트로 착각해 옮기지 않게)."""
        return [p.split("/")[0] for p in (c.boot_rel, c.comp_rel, c.front_rel) if p]

    @staticmethod
    def _layout_modules(c: _Ctx) -> list[str]:
        """루트 <modules> 목록: back, front(만들었을 때), boot 순 — boot 가 앞의 둘에
        의존하는 방향과 같은 순서."""
        return [c.comp_rel, *([c.front_rel] if c.front_made and c.front_rel else []), c.boot_rel]

    @staticmethod
    def _front_lib(c: _Ctx, coords: GAV) -> list[GAV]:
        """front 모듈을 만들었으면 boot 가 의존할 front jar(``<root>-front``) GAV, 아니면 빈 목록."""
        if not c.front_made:
            return []
        return [GAV(group_id="${project.groupId}", artifact_id=coords.artifact_id + FRONT_ARTIFACT_SUFFIX,
                    version="${project.version}")]

    @staticmethod
    def _mandatory_in_root(c: _Ctx) -> bool:
        """필수 사내 아티팩트를 루트 pom 에 둘지(``artifacts.mandatory_target: root``, 회사
        기본) 각 모듈 pom 에 둘지."""
        return getattr(c.cfg.artifacts, "mandatory_target", "root") == "root"

    def _record_modules(self, c: _Ctx, existing: bool = False) -> None:
        """결과에 실제로 있는 boot / comp / front 모듈 경로와 boot 패키징(jar / war)을 적는다(보고서, 구조
        검토 입력). 이미 재구성된 프로젝트면 front 파일 수도 센다.

        RestructureResult.boot_module / comp_module / front_module (the module dirs that exist) + boot_packaging."""
        r = c.result
        for attr, rel in (("boot_module", c.boot_rel), ("comp_module", c.comp_rel), ("front_module", c.front_rel)):
            if rel and c.ws.exists(f"{c.p(rel)}/pom.xml"):
                setattr(r, attr, c.p(rel))
        if r.boot_module:
            try:
                r.boot_packaging = PomEditor(c.ws.abs(f"{r.boot_module}/pom.xml")).packaging()
            except PomEditError:
                r.boot_packaging = None
        if existing and r.front_module:
            front = c.ws.abs(r.front_module)
            r.front_files = sum(1 for f in front.rglob("*") if f.is_file() and f.name != "pom.xml")

    # ------------------------------------------------------------------ front module (round 9c)
    def _web_layout(self, c: _Ctx, eds: list[PomEditor]) -> tuple[list[tuple[str, str]], list[str]]:
        """as-is pom 들의 (web root 목록, classpath 디렉토리 목록). web root = 기본 ``src/main/webapp`` +
        maven-war-plugin warSourceDirectory(접두사 없음) + <webResources> 디렉토리(targetPath 가 웹 경로
        접두사); classpath = source / script source / <resources> 디렉토리. 소스가 comp 로 같은 상대 경로로
        옮겨졌으므로 모듈 기준 = comp 기준이고, ``../`` 디렉토리는 ``_comp_dir`` 로 바꾼다(못 하면 뺀다).

        (web roots as (module-relative dir, web-path prefix), classpath dirs) of the as-is poms: the default
        `src/main/webapp`, then every maven-war-plugin `warSourceDirectory` (prefix "") and `<webResources>` directory
        (prefix = its `targetPath`); the classpath dirs are `sourceDirectory` / `scriptSourceDirectory` / `<resources>`
        directories (`source_scanner.pom_build_dirs`). The sources were folded into the comp module at the same
        relative paths, so module-relative = comp-relative; a `../` directory folded in by `_plan_outside_dirs`
        (2026-10-23) is mapped to its comp-relative location (`_comp_dir`), other `../` directories are skipped."""
        roots: list[tuple[str, str]] = [(DEFAULT_WEBAPP, "")]
        keep: list[str] = []
        for ed in eds:
            text = ed.to_string()
            for w in pom_web_dirs(text, allow_parent=True):
                d = self._comp_dir(c, ed, w.dir)
                if d and all(d != x for x, _ in roots):
                    roots.append((d, w.target))
            for d in pom_build_dirs(text, allow_parent=True):
                d = self._comp_dir(c, ed, d)
                if d and d not in keep:
                    keep.append(d)
        return roots, keep

    def _populate_front(self, c: _Ctx, comp: str, boot: str, coords: GAV,
                        eds: list[PomEditor] | None = None) -> None:
        """웹 리소스를 comp 에서 front 모듈(``{name}-front``)로 — 웹 리소스가 하나도 없으면 front 모듈을
        만들지 않는다. web root 안 파일마다 첫 규칙이 이긴다:
        (1) ``WEB-INF/`` 아래: JSP → ``<front_jsp_dir>/<WEB-INF/jsp 아래 경로>`` (WEB-INF 의 다른 곳이면
        WEB-INF 아래 경로 유지: ``WEB-INF/views/a.jsp`` → ``jsp/views/a.jsp``), tags/ 와 *.tld → front jar 의
        ``META-INF/resources/WEB-INF/...``, lib/ 와 classes/ → comp 에 남김(후속 작업), 설정
        파일(``WEBINF_CONFIG_EXTS``: xml, properties, yml ...) → boot 의 ``asis-webinf/`` stash(Boot 는 읽지
        않음, 참고용), 그 밖(뷰, html / js / css 조각, 템플릿, 다운로드) → front jar 의
        ``META-INF/resources/WEB-INF/`` 같은 경로(v2.1g: include / forward 로는 닿고 브라우저에는 직접 안
        보임). (2) web root 의 ``META-INF/`` → comp 에 남김(후속 작업). (3) WEB-INF 밖의 JSP → front jar 의
        ``META-INF/resources/`` 루트(같은 URL). (4) web root 안에 있는 pom 의 <resource> / sourceDirectory
        아래 파일 → classpath 콘텐츠라 확장자와 상관없이 comp 에 남긴다(복사된 <resources> 가 계속 가리킴).
        ``layout.front_copy_resource_dirs`` (기본 true, v2.1g)면 front 의 ``<front_static_dir>/<웹 경로>``
        에도 바이트 그대로 복사해 as-is WAR 와 같은 URL 로 서비스된다. (5) 나머지 전부(html, js, css, 이미지,
        mp4, exe ... 확장자 무관) → ``<front_static_dir>/<웹 경로>``. 그리고 comp 의
        ``src/main/resources/{static,public,templates}/**`` → front 의 같은 경로.
        web root = ``src/main/webapp`` + maven-war-plugin warSourceDirectory / <webResources>
        (``_web_layout``). 대상이 이미 있으면 옮기지 않고 후속 작업. 끝에 ``c.front_made`` / ``c.jsp_moved`` /
        ``c.jstl`` / ``c.boot_war`` 를 정하고 front pom 을 쓴다 — JSP 가 옮겨졌고 ``jsp_boot_packaging: war``
        면 boot 모듈은 외부 Tomcat 용 WAR.

        Move the web resources out of the comp module into `{name}-front` (only when there are any):
        `src/main/webapp/**` minus WEB-INF / META-INF → `<front>/<front_static_dir>/…` (same relative path);
        `src/main/resources/{static,public,templates}/**` → `<front>/src/main/resources/<dir>/…` (same path);
        JSPs (`*.jsp|jspf|jspx`) under `WEB-INF/` → `<front>/<front_jsp_dir>/<path under WEB-INF/jsp>` (a JSP
        elsewhere under WEB-INF, e.g. `WEB-INF/views/a.jsp` or `WEB-INF/a.jsp`, keeps its path under WEB-INF:
        `jsp/views/a.jsp`, `jsp/a.jsp`); JSPs outside WEB-INF → the jar's `META-INF/resources/` root (same URL);
        `WEB-INF/tags/**` and `*.tld` under WEB-INF → `META-INF/resources/WEB-INF/…` (same web path). Other WEB-INF
        files (web.xml, XML configs, …) → `<boot>/src/main/resources/asis-webinf/…` (not read by Boot); WEB-INF/lib
        and WEB-INF/classes and webapp META-INF stay in the comp module (follow-up). Sets `c.front_made`,
        `c.jsp_moved`, `c.jstl`, `c.boot_war` and writes the front pom.

        Non-standard web roots (2026-10-23, `eds` = the as-is poms): every maven-war-plugin `warSourceDirectory`
        (e.g. `STATIC/`) and `<webResources>` directory (its `targetPath` prefixes the web path) is a web root like
        `src/main/webapp`. Inside a web root, per file, first match wins: (1) `WEB-INF/**` and `META-INF/**` (web
        path) → the WEB-INF / META-INF rules above; (2) JSPs → front; (3) a file below a `<resource>` /
        `sourceDirectory` of the pom that lies inside the web root (e.g. `<resource><directory>STATIC/resources`) is
        classpath content and stays in the comp module, whatever its extension (Excel files, template XML, media,
        images) — the copied `<resources>` entry keeps pointing at it; static-web-extension files kept this way get a
        follow-up (the WAR also served them as web content); (4) everything else → `<front_static_dir>/<web path>`."""
        L = c.cfg.layout
        if not c.front_rel:
            return
        front = c.p(c.front_rel)
        static_dst = f"{front}/{L.front_static_dir.strip('/')}"
        jsp_dir = L.front_jsp_dir.strip("/")
        jsp_dst = f"{front}/{jsp_dir}"
        i = jsp_dir.find("META-INF/resources")
        # front jar 의 META-INF/resources (Servlet 3: jar 안 이 경로는 웹 루트처럼 서비스됨) — JSP 디렉토리
        # 설정에서 그 위치를 뽑는다
        meta_dst = f"{front}/{jsp_dir[:i + len('META-INF/resources')]}" if i >= 0 else static_dst
        stash = f"{boot}/src/main/resources/{WEBINF_STASH}"
        plan: list[tuple[str, str, str, str]] = []      # (src, dst, kind: static | jsp | webinf, web path)
        left: list[str] = []
        kept_web: list[str] = []                        # static-extension files kept as classpath resources (rule 3)
        kept_res = 0
        copy_res = bool(getattr(L, "front_copy_resource_dirs", True))
        roots, keep = self._web_layout(c, list(eds or ()))
        web_dirs = [f"{comp}/{d}" for d, _ in roots]
        for wdir, target in roots:
            webapp = f"{comp}/{wdir}"
            wabs = c.ws.abs(webapp)
            if not wabs.is_dir():
                continue
            inner = [k for k in keep if k.startswith(wdir + "/")]
            for f in sorted(x for x in wabs.rglob("*") if x.is_file()):
                rel = f.relative_to(wabs).as_posix()
                if is_build_output_path(c.ws.rel(f), c.ws.root):
                    continue
                web = posixpath.normpath(f"{target}/{rel}") if target else rel
                top, _, sub = web.partition("/")
                src = f"{webapp}/{rel}"
                # WEB-INF 아래(웹 경로 기준)는 브라우저가 직접 못 여는 곳: JSP 는 jsp 디렉토리, 설정은 boot
                # stash, 나머지는 front jar 의 WEB-INF 그대로
                if top == "WEB-INF" and sub:
                    if sub.lower().endswith(JSP_EXTS):
                        under = sub.removeprefix("jsp/")
                        plan.append((src, f"{jsp_dst}/{under}", "jsp", web))
                    elif sub.startswith("tags/") or sub.lower().endswith(".tld"):
                        plan.append((src, f"{meta_dst}/WEB-INF/{sub}", "jsp", web))
                    # WEB-INF/lib 의 jar 와 classes 는 Maven 의존성
                    # / 소스로 바꿔야 할 대상: 옮기지 않고 후속 작업
                    elif sub.startswith(("lib/", "classes/")):
                        left.append(src)
                    # v2.1g: 설정이 아닌 WEB-INF 파일은 front jar 의 META-INF/resources/WEB-INF/ 같은 경로 —
                    # include / forward 경로가 그대로 맞는다.
                    # 설정(web.xml, Spring XML, properties)만 boot stash
                    elif posixpath.splitext(sub)[1].lower() not in WEBINF_CONFIG_EXTS:
                        plan.append((src, f"{meta_dst}/WEB-INF/{sub}", "static", web))   # a view / fragment / file
                    else:
                        plan.append((src, f"{stash}/{sub}", "webinf", web))
                elif top == "META-INF" and sub:
                    left.append(src)
                elif web.lower().endswith(JSP_EXTS):
                    plan.append((src, f"{meta_dst}/{web}", "jsp", web))
                # web root 안의 <resource> 디렉토리(예: STATIC/resources): classpath 로 읽는 코드가 있을 수
                # 있어 확장자와 상관없이 comp 에 남긴다
                elif any(f"{wdir}/{rel}".startswith(k + "/") for k in inner):
                    kept_res += 1
                    if posixpath.splitext(rel)[1].lstrip(".").lower() in WEB_STATIC_EXTENSIONS:
                        kept_web.append(src)
                    # v2.1g: 이동이 아니라 복사 — 페이지가 링크할 수도 있어 front static 에도 같은 웹 경로로
                    # 둔다(현업: '어디다 쓸지 모르니 디렉토리까지 똑같이')
                    if copy_res:                       # 2026-10-28: also served as before, from the front jar
                        plan.append((src, f"{static_dst}/{web}", "copy", web))
                else:
                    plan.append((src, f"{static_dst}/{web}", "static", web))
        for d in WEB_RESOURCE_DIRS:
            rabs = c.ws.abs(f"{comp}/src/main/resources/{d}")
            if not rabs.is_dir():
                continue
            for f in sorted(x for x in rabs.rglob("*") if x.is_file()):
                rel = f.relative_to(rabs).as_posix()
                plan.append((f"{comp}/src/main/resources/{d}/{rel}", f"{front}/src/main/resources/{d}/{rel}",
                             "static", rel))
        # front 로 갈 웹 리소스가 없으면(WEB-INF 설정 파일뿐이어도)
        # front 모듈을 만들지 않고 아무것도 옮기지 않는다
        if not any(k in ("static", "jsp", "copy") for _, _, k, _ in plan):
            return                                     # no web resources: no front module
        taken: set[str] = set()
        moved = {"static": 0, "jsp": 0, "webinf": 0, "copy": 0}
        jsp_files = 0                                  # JSP pages actually moved (A24: a clashing one is not)
        clash: list[str] = []
        jsp_relocated: list[str] = []
        for src, dst, kind, web in plan:
            # 같은 대상 경로가 두 번 나오거나 이미 있으면 덮어쓰지 않는다: 사람이 합치도록 후속 작업(clash)
            if dst in taken or c.ws.exists(dst):
                clash.append(src)
                continue
            taken.add(dst)
            if kind == "jsp" and dst.startswith(jsp_dst + "/"):
                text = c.ws.abs(src).read_text(encoding="utf-8", errors="replace")
                c.jstl = c.jstl or bool(JSTL_URI_RE.search(text))
                web_old = "/" + web
                # WEB-INF/jsp 밖에 있던 JSP 는 웹 경로가 바뀐다: include / forward 경로를 고치라는 후속 작업용
                if not web_old.startswith("/WEB-INF/jsp/"):
                    jsp_relocated.append(web_old)
            elif kind == "jsp" and src.lower().endswith(JSP_EXTS):
                c.jstl = c.jstl or bool(JSTL_URI_RE.search(c.ws.abs(src).read_text(encoding="utf-8",
                                                                                    errors="replace")))
            if kind == "copy":
                c.copy(src, dst)
            else:
                c.move(src, dst)
            moved[kind] += 1
            if kind == "jsp" and src.lower().endswith(JSP_EXTS):
                jsp_files += 1
        for d in (*web_dirs, *(f"{comp}/src/main/resources/{x}" for x in WEB_RESOURCE_DIRS)):
            self._prune_empty_dirs(c, d)
        c.front_made = True
        # A24: 같은 대상으로 가는 JSP 두 개는 하나만 옮겨진다(나머지는 clash) — 옮겨진 것만 센다
        c.jsp_moved = jsp_files
        c.result.front_files = moved["static"] + moved["jsp"] + moved["copy"]
        # JSP 가 있으면 boot 모듈은 외부 Tomcat 용 WAR(회사 규칙):
        # Spring Boot 는 실행 jar 에서 JSP 를 지원하지 않는다
        c.boot_war = c.boot_war or (c.jsp_moved > 0 and L.jsp_boot_packaging == "war")   # a WAR overlay may set it
        self._write_front_pom(c, coords, front)
        custom = [d for d, _ in roots
                  if d != DEFAULT_WEBAPP and any(s_.startswith(f"{comp}/{d}/") for s_, _, _, _ in plan)]
        c.note(f"front module {front}: {moved['static']} static web file(s) → {L.front_static_dir}, "
               f"{moved['jsp']} JSP file(s) → {L.front_jsp_dir} (META-INF/resources of the front jar)"
               + (f"; {moved['webinf']} other WEB-INF file(s) (web.xml, XML configs) → {stash}" if moved["webinf"]
                  else "")
               + (f"; {moved['copy']} file(s) of <resource> dir(s) inside the web root copied to {L.front_static_dir} "
                  f"at their web path (the classpath copies stay in {comp})" if moved["copy"] else "")
               + (f"; web root(s) from maven-war-plugin: {', '.join(custom)}" if custom else ""))
        if kept_res:
            c.note(f"{kept_res} file(s) under a <resource> / source directory inside a web root stay in {comp} as "
                   "classpath resources (the copied <resources> entries still point at them)"
                   + (f"; served as before from the front jar's {L.front_static_dir} copy" if moved["copy"] else ""))
        if kept_web and not moved["copy"]:
            c.followup(f"{len(kept_web)} static web file(s) under a <resource> directory inside the web root stay "
                       f"classpath resources in {comp} ({', '.join(kept_web[:3])}"
                       f"{' …' if len(kept_web) > 3 else ''}); "
                       f"the as-is WAR also served them as web content — copy them into {static_dst} if pages link "
                       "them (layout.front_copy_resource_dirs: true does it)")
        if jsp_relocated:
            c.followup(f"{len(jsp_relocated)} JSP(s) outside /WEB-INF/jsp/ now live under /WEB-INF/jsp/ in "
                       f"{front} ({', '.join(jsp_relocated[:3])}{' …' if len(jsp_relocated) > 3 else ''}): update "
                       "jsp:include / forward paths and view-resolver prefixes that name the old location")
        if left:
            where = ", ".join(sorted({d for d in web_dirs if any(x.startswith(d + "/") for x in left)}))
            c.followup(f"{len(left)} file(s) under WEB-INF/lib, WEB-INF/classes or META-INF stay in {where} (not "
                       "packaged): declare the jars as Maven dependencies / move the classes into src/main/java")
        if clash:
            c.followup(f"{len(clash)} web file(s) not moved to {front}: the target path was already taken "
                       f"({', '.join(clash[:3])}{' …' if len(clash) > 3 else ''}) — merge them by hand")

    def _strip_war_web_config(self, c: _Ctx, ed: PomEditor) -> None:
        """생성 / 재작성 pom 에서 maven-war-plugin 의 warSourceDirectory / webResources 를 뺀다: web root 는
        front 모듈로 갔고 boot WAR 에는 webapp 디렉토리가 없다(JSP 는 front jar 에서). 루트 pluginManagement
        를 통해(상속, 역할 부모면 복사) boot pom 에도 닿으므로 모든 생성 pom 에서 지운다.

        A generated / rewritten pom must not carry the as-is maven-war-plugin `warSourceDirectory` /
        `webResources` (2026-10-23): the web root moved to the front module, the boot WAR has no webapp directory
        (its JSPs come from the front jar). Reaches the boot pom through the root pluginManagement (inherited, or
        copied for role parents)."""
        removed: list[str] = []
        for p in ed.find_plugins(*WAR_PLUGIN, where="all"):
            conf = ed.child(p, "configuration")
            for name in ("warSourceDirectory", "webResources"):
                el = ed.child(conf, name)
                if el is not None:
                    ed.remove_node(el)
                    removed.append(name)
            ed.remove_if_empty(conf)
        if removed:
            c.note(f"maven-war-plugin {' / '.join(dict.fromkeys(removed))} dropped from {c.ws.rel(ed.path)} (the web "
                   "root moved to the front module; the boot WAR has no webapp directory)")

    def _large_media_note(self, c: _Ctx) -> None:
        """분석이 찾은 큰 미디어 파일이 front / comp jar 에 들어간다는 노트(파일 서버로 옮기는 것을 권함)."""
        src = c.profile.source
        if not src.large_media_files:
            return
        mb = src.large_media_bytes / (1024 * 1024)
        c.note(f"{len(src.large_media_files)} large media file(s) ({mb:.0f} MB) are packaged into the front / comp "
               f"jar: consider moving them to a file server ({', '.join(src.large_media_files[:3])}"
               f"{' …' if len(src.large_media_files) > 3 else ''})")

    @staticmethod
    def _prune_empty_dirs(c: _Ctx, rel: str) -> None:
        """디렉토리 아래의 빈 디렉토리를 깊은 것부터 지운다(자기 자신도 비면 지움)."""
        top = c.ws.abs(rel)
        if not top.is_dir():
            return
        for d in sorted((x for x in top.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
        if not any(top.iterdir()):
            top.rmdir()

    def _write_front_pom(self, c: _Ctx, coords: GAV, front: str) -> None:
        """front 모듈 pom 생성(``<root>-front``): 역할 부모면 ``comp_parent`` + 명시 좌표 + 필수 사내
        아티팩트 / BOM / java 버전, 아니면 루트 pom 이 부모."""
        A = c.cfg.artifacts
        path = f"{front}/pom.xml"
        aid = coords.artifact_id + FRONT_ARTIFACT_SUFFIX
        if self._role(c):
            parent = A.comp_parent.gav()                                               # type: ignore[union-attr]
            c.write(path, render("front-pom.xml.j2", parent=parent, relative_path="", group_id=coords.group_id,
                                 version=coords.version, artifact_id=aid, name=None))
            self._record_parent(c, path, None, parent)
            ed = PomEditor(c.ws.abs(path))
            self._role_module(c, ed, None, exclude=set())
            self._fix_java(c, ed, top=True)
            self._strip_war_web_config(c, ed)
            self._strip_ear_plugin(c, ed)
            c.save(ed)
        else:
            c.write(path, render("front-pom.xml.j2", parent=coords,
                                 relative_path=posixpath.relpath(c.p("pom.xml"), front), group_id=None,
                                 version=None, artifact_id=aid, name=None))

    def _war_deps(self, c: _Ctx, have: set[str]) -> list[GAV]:
        """boot WAR 의 컨테이너 의존성: spring-boot-starter-tomcat / tomcat-embed-jasper 는 provided(외부
        Tomcat 이 제공), @ServletComponentScan 을 쓰면 spring-boot-web-server 는 compile(Boot 4.1 은 Tomcat
        starter 로만 끌어오므로 provided 면 WEB-INF/lib 에서 빠짐),
        JSP 가 JSTL 을 쓰면 JSTL. 버전은 Spring Boot 4.1 BOM 이 관리.

        The boot WAR's container dependencies: Tomcat + Jasper `provided` (external Tomcat), JSTL when a JSP uses
        it (managed by the Spring Boot 4.1 BOM, so no versions)."""
        out = [GAV(group_id=BOOT_GROUP, artifact_id="spring-boot-starter-tomcat", scope="provided"),
               GAV(group_id="org.apache.tomcat.embed", artifact_id="tomcat-embed-jasper", scope="provided")]
        if self._servlet_scan(c):
            # @ServletComponentScan lives in spring-boot-web-server, which Boot 4.1 only brings through the Tomcat
            # starter — `provided` would leave it out of WEB-INF/lib (verified with a WAR build, 2026-10-21)
            out.append(GAV(group_id=BOOT_GROUP, artifact_id="spring-boot-web-server"))
        if c.jstl:
            out += JSTL_DEPS
        out = [g for g in out if g.key not in have]
        if out:
            c.note(f"boot WAR for external Tomcat: {', '.join(g.artifact_id + (' (provided)' if g.scope else '') for g in out)}"
                   f" added to {c.boot_rel}")
        return out

    @staticmethod
    def _servlet_scan(c: _Ctx) -> bool:
        """생성 Application 에 @ServletComponentScan 을 붙이는가: 설정(``add_servlet_component_scan``)이 켜져
        있고 servlet 케이스이거나 @WebServlet / filter / listener 클래스가 있을 때.

        The generated Application carries @ServletComponentScan (servlet case or @WebServlet / filter classes)."""
        src = c.profile.source
        return c.cfg.layout.add_servlet_component_scan and (
            c.profile.case_type == CaseType.SERVLET_TOMCAT or bool(src.servlet_classes)
            or bool(src.filter_listener_classes))

    def _initializer_followup(self, c: _Ctx, boot: str, mains: list[str]) -> None:
        """boot 가 WAR 인데 SpringBootServletInitializer 를 상속한 클래스가 없으면 후속 작업 — 외부 Tomcat
        에서 앱이 기동되지 않는다."""
        root = c.ws.abs(f"{boot}/src/main/java")
        if root.is_dir() and any(SERVLET_INIT_RE.search(f.read_text(encoding="utf-8", errors="replace"))
                                 for f in root.rglob("*.java")):
            return
        c.followup(f"{c.boot_rel} is a WAR for external Tomcat but {', '.join(mains)} does not extend "
                   "SpringBootServletInitializer — extend it and override configure(SpringApplicationBuilder)")

    def _view_resolver(self, c: _Ctx) -> tuple[str | None, str | None, str | None]:
        """as-is 설정에서 JSP 뷰 prefix / suffix 찾기: XML 의 InternalResourceViewResolver 빈 / ``<mvc:jsp>``,
        Java 의 ``new InternalResourceViewResolver(...)`` / ``registry.jsp(...)`` / setPrefix·setSuffix. 처음
        찾은 파일의 (prefix, suffix, 파일).

        (prefix, suffix, file) of an as-is InternalResourceViewResolver / `<mvc:jsp>` / `registry.jsp(…)`."""
        base = c.ws.abs(c.p("."))
        for f in sorted(base.rglob("*")):
            if (not f.is_file() or f.suffix not in (".xml", ".java") or ".git" in f.parts
                    or is_build_output_path(c.ws.rel(f), c.ws.root)):
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            if VIEW_RESOLVER not in text and "mvc:jsp" not in text and ".jsp(" not in text:
                continue
            rel = c.ws.rel(f)
            pre = suf = None
            if f.suffix == ".xml":
                m = re.search(r"<mvc:jsp\b([^>]*)>", text)
                if m:
                    pre = (re.search(r'prefix\s*=\s*"([^"]*)"', m.group(1)) or [None, None])[1]
                    suf = (re.search(r'suffix\s*=\s*"([^"]*)"', m.group(1)) or [None, None])[1]
                i = text.find(VIEW_RESOLVER)
                if pre is None and i >= 0:
                    end = text.find("</bean>", i)
                    chunk = text[i:end if end > 0 else i + 2000]
                    for name in ("prefix", "suffix"):
                        v = re.search(rf'name\s*=\s*"{name}"\s+value\s*=\s*"([^"]*)"', chunk) or                             re.search(rf'name\s*=\s*"{name}"\s*>\s*<value>\s*([^<]*?)\s*</value>', chunk) or                             re.search(rf'p:{name}\s*=\s*"([^"]*)"', chunk)
                        if v:
                            pre, suf = (v.group(1), suf) if name == "prefix" else (pre, v.group(1))
            else:
                m = re.search(rf'new\s+{VIEW_RESOLVER}\s*\(\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\)', text) or                     re.search(r'\.jsp\(\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\)', text)
                if m:
                    pre, suf = m.group(1), m.group(2)
                else:
                    p_ = re.search(r'\.setPrefix\(\s*"([^"]*)"\s*\)', text)
                    s_ = re.search(r'\.setSuffix\(\s*"([^"]*)"\s*\)', text)
                    pre, suf = (p_.group(1) if p_ else None), (s_.group(1) if s_ else None)
            if pre is not None or suf is not None:
                return pre, suf, rel
        return None, None, None

    def _jsp_web_path(self, c: _Ctx, web: str) -> str:
        """JSP 의 as-is 웹 경로 → 이동 뒤 경로(``_populate_front`` 규칙): ``/WEB-INF/jsp/...`` 는 jsp 디렉토리
        마운트 아래 그대로, WEB-INF 의 다른 곳은 마운트 아래에 WEB-INF 뒤 경로, WEB-INF 밖은
        그대로(META-INF/resources 루트).

        As-is web path of a JSP location → its path after the move (see `_populate_front`)."""
        jsp_dir = c.cfg.layout.front_jsp_dir.strip("/")
        i = jsp_dir.find("META-INF/resources")
        if i < 0:
            return web
        mount = "/" + jsp_dir[i + len("META-INF/resources"):].strip("/")
        if web == "/WEB-INF/jsp" or web.startswith("/WEB-INF/jsp/"):
            return mount + web[len("/WEB-INF/jsp"):]
        if web.startswith("/WEB-INF/"):
            return mount + "/" + web[len("/WEB-INF/"):]
        return web                                      # outside WEB-INF: META-INF/resources root, same path

    def _jsp_view_props(self, c: _Ctx, boot: str) -> None:
        """as-is 뷰 리졸버의 prefix / suffix → boot application.properties 의 ``spring.mvc.view.prefix`` /
        ``spring.mvc.view.suffix`` (옮긴 JSP 위치로 변환). 이미 설정돼 있으면 그대로, 못 찾으면 후속 작업.
        prefix 가 바뀌면 as-is 리졸버 빈도 고치라는 후속 작업(그 빈이 Boot 의 리졸버보다 우선).

        `spring.mvc.view.prefix/suffix` into the boot `application.properties` when the as-is configuration names
        an InternalResourceViewResolver prefix / suffix (mapped to the moved JSP location), else a follow-up."""
        res = f"{boot}/src/main/resources"
        for f in self._boot_resource_files(c, res):
            if "spring.mvc.view.prefix" in c.ws.read(f) or re.search(r"(?ms)^\s*view:\s*$.*?prefix:", c.ws.read(f)):
                c.note(f"spring.mvc.view.* already configured in {f}; left unchanged")
                return
        pre, suf, where = self._view_resolver(c)
        if pre is None and suf is None:
            c.followup(f"no InternalResourceViewResolver prefix / suffix found in the as-is configuration: set "
                       f"spring.mvc.view.prefix (JSPs are under /WEB-INF/jsp/ in {c.front_rel}) and "
                       f"spring.mvc.view.suffix=.jsp in {res}/application.properties")
            return
        lines = [f"# sbmig: JSP views (as-is {VIEW_RESOLVER} in {where}; JSPs are served from {c.front_rel})"]
        if pre is not None:
            new_pre = self._jsp_web_path(c, pre)
            lines.append(f"spring.mvc.view.prefix={new_pre}")
            if new_pre != pre:
                c.followup(f"view prefix {pre} ({where}) maps to {new_pre} after the JSP move: "
                           f"spring.mvc.view.prefix is set to it — update or remove the as-is {VIEW_RESOLVER} bean "
                           "(it takes precedence over Boot's view resolver)")
        if suf is not None:
            lines.append(f"spring.mvc.view.suffix={suf}")
        rel = f"{res}/application.properties"
        self._append_props(c, rel, lines)
        c.note(f"spring.mvc.view.prefix / suffix written to {rel} (from {where})")

    # ------------------------------------------------------------------ root pom
    def _rewrite_root(self, c: _Ctx, root: PomEditor, modules: list[str], *, keep_deps: bool,
                      keep_build_plugins: bool, aggregator: bool = False) -> None:
        """루트 pom 을 aggregator 로: 좌표 명시, 이름 변경(``sbmig.project-name``, merge / single), 부모 →
        사내 부모 pom(``artifacts.parent``, <relativePath/>), packaging pom, <modules> = 회사 모듈, 의존성 /
        build 정리(wrap 은 유지). ``aggregator`` (역할 부모)면 모듈이 루트를 상속하지 않으므로 필수 사내
        아티팩트(``mandatory_target: root``)만 두고 관리 항목, BOM, 루트가 안 쓰는 속성은 지운다(이미 모듈 pom
        에 복사됨); 아니면 필수 사내 아티팩트, BOM, java 버전을 루트에.

        `aggregator` (role parents, round 9b): the modules do not inherit the root pom — no mandatory
        dependencies, BOM or java level here, and the root's properties / dependencyManagement / pluginManagement
        (already copied into the module poms) are removed, except properties the root itself still references."""
        A = c.cfg.artifacts
        g, v = root.effective_group_id(), root.effective_version()
        if not root.group_id() and g:
            root.set_coordinate("groupId", g)
        if not root.version() and v:
            root.set_coordinate("version", v)
        if not keep_build_plugins:          # 2026-10-23: the intake aggregator's project name (merge / single only)
            if c.rename_root and root.artifact_id() != c.rename_root:
                root.set_coordinate("artifactId", c.rename_root)
                c.note(f"root pom artifactId {c.root_aid_old} → {c.rename_root} ({NAME_PROP}: the member that had "
                       "this artifactId was merged / dropped, the reactor has no collision any more)")
            if root.has_property(NAME_PROP):
                root.remove_property(NAME_PROP)
                root.remove_if_empty(root.find("properties"))
        old_parent = root.get_parent()
        # 루트는 언제나 사내 부모 pom(base-parent) + 빈 <relativePath/>: 부모는 저장소(Nexus)에서 받는다
        root.set_parent(A.parent.gav(), None)
        self._record_parent(c, c.ws.rel(root.path), old_parent, A.parent.gav())
        root.set_packaging("pom")
        root.set_modules(modules)   # round 9c: layout.module_path names (`{name}-back`, `{name}-front`, `{name}-boot`)
        if not keep_deps:
            root.remove_all_dependencies()
        if aggregator:
            if self._mandatory_in_root(c):
                self._add_mandatory(c, root)
            self._remove_unwanted(c, root)
            root.remove_element("dependencyManagement")
            root.remove_element("build/pluginManagement")
            self._fix_java(c, root, top=False)
        else:
            self._add_mandatory(c, root)
            self._remove_unwanted(c, root)
            self._fix_java(c, root, top=True)
            self._boot_bom(c, root)
        if not keep_build_plugins:
            for name in BUILD_TO_COMP + ("finalName",):
                root.remove_element(f"build/{name}")
            root.remove_build_plugins_except([])
        else:
            root.remove_element("build/finalName")
            for p in root.find_plugins(*WAR_PLUGIN, where="build"):
                root.remove_node(p)
        root.remove_plugin(*BOOT_PLUGIN)
        if not keep_build_plugins:
            self._strip_ear_plugin(c, root)
        if c.front_made:
            self._strip_war_web_config(c, root)
        root.remove_if_empty(root.find("build/plugins"))
        root.remove_if_empty(root.find("build"))
        if aggregator:
            self._prune_root_properties(root)
        c.save(root)

    @staticmethod
    def _prune_root_properties(root: PomEditor) -> None:
        """aggregator 루트: 루트 pom 에 남은 내용이 (속성끼리 이어지는 것까지) 참조하지 않는 속성을 지운다 —
        모듈 pom 으로 복사됐고 모듈은 더 이상 루트를 상속하지 않는다.

        Aggregator root: drop the properties nothing left in the root pom references (they were copied into the
        module poms, which no longer inherit the root)."""
        props = root.properties()
        if not props:
            return
        text = "".join("".join(el.itertext()) for el in root.children(root.root)
                       if PomEditor.local_name(el) != "properties")
        used: set[str] = set()
        todo = re.findall(r"\$\{([^}]+)\}", text)
        while todo:
            k = todo.pop()
            if k in used or k not in props:
                continue
            used.add(k)
            todo.extend(re.findall(r"\$\{([^}]+)\}", props[k] or ""))
        for k in props:
            if k not in used:
                root.remove_property(k)
        root.remove_if_empty(root.find("properties"))

    # ------------------------------------------------------------------ role parents (round 9b)
    @staticmethod
    def _role(c: _Ctx) -> bool:
        """역할 부모 방식인가(``artifacts.role_parents`` = boot_parent 와 comp_parent 가 둘 다 설정됨, 회사
        설정): 모듈이 루트 대신 사내 부모 pom(boot-jar / comp-jar)을 직접 상속한다."""
        return bool(c.cfg.artifacts.role_parents)

    @staticmethod
    def _gav_str(g: GAV | None) -> str:
        """GAV → ``g:a:v`` 문자열(없으면 ``-``), parents_set 기록용."""
        return "-" if g is None else f"{g.group_id}:{g.artifact_id}:{g.version or '?'}"

    def _record_parent(self, c: _Ctx, pom: str, old: GAV | None, new: GAV) -> None:
        """``RestructureResult.parents_set`` 에 부모 변경 한 줄(``pom: 옛 → 새``). 바뀌지
        않은 부모는 적지 않는다.

        `RestructureResult.parents_set`: one entry per parent set or replaced (unchanged parents are skipped)."""
        if old is not None and old.key == new.key and (old.version or None) == (new.version or None):
            return
        entry = f"{pom}: {self._gav_str(old)} → {self._gav_str(new)}"
        if entry not in c.result.parents_set:
            c.result.parents_set.append(entry)

    def _role_module(self, c: _Ctx, ed: PomEditor, root: PomEditor | None, *, exclude: set[str],
                     all_props: bool = False) -> None:
        """역할 부모를 받는 모듈 pom 에, 예전에 루트에서 물려받던 것을 넣는다(``_inherit_root_content``;
        ``root`` None = 원래 루트를 상속하지 않던 모듈). 그다음 필수 사내 아티팩트 (``mandatory_target:
        modules`` 일 때만), remove_when_present, Spring Boot BOM. java 버전은
        호출부가(``_fix_java(top=True)``).

        A module pom on an external role parent gets what it used to inherit from the root pom: the root's
        dependencyManagement (minus `exclude`, the merged modules), pluginManagement, repositories and properties
        (`all_props`: every root property — the comp pom, which holds the root's dependencies; else the ones this
        pom references plus `project.*` / `maven.*` build settings), `root` None = the module never inherited the
        root; then the mandatory dependencies, remove_when_present and the Spring Boot BOM. The caller fixes the java
        level (`_fix_java(top=True)`). Round 9c: the mandatory dependencies only with `mandatory_target: modules`
        (`root`: they are declared in the root pom)."""
        if root is not None:
            self._inherit_root_content(c, ed, root, exclude=exclude, all_props=all_props)
        if not self._mandatory_in_root(c):
            self._add_mandatory(c, ed)
        self._remove_unwanted(c, ed)
        self._boot_bom(c, ed)

    @staticmethod
    def _inherit_root_content(c: _Ctx, ed: PomEditor, root: PomEditor, *, exclude: set[str],
                              all_props: bool = False) -> None:
        """``root`` 에서 물려받던 관리 항목을 ``ed`` 로 복사: dependencyManagement(``exclude`` =
        합쳐진 모듈 제외), pluginManagement, repositories / pluginRepositories, 속성(``all_props`` = 전부:
        루트 의존성을 받은 comp pom; 아니면 이 pom 이 참조하는 것 +
        project.* / maven.*). ``ed`` 에 이미 있는 것은 덮어쓰지 않는다.

        Copy the management `ed` inherited from `root` into `ed` (see `_role_module`): dependencyManagement
        (minus `exclude`), pluginManagement, repositories / pluginRepositories and properties (`all_props`, else the
        referenced ones plus `project.*` / `maven.*`). Nothing already present in `ed` is overwritten."""
        for el in root.dependency_elements(True):
            g = root.gav_of(el)
            if g.key in exclude or ed.has_managed_dependency(g.group_id, g.artifact_id):
                continue
            ed.copy_into(ed.ensure_path("dependencyManagement/dependencies"), el)
        pm = root.find("build/pluginManagement")
        if pm is not None and ed.find("build/pluginManagement") is None:
            ed.copy_into(ed.ensure_path("build"), pm, ordered=True)
        for name in ("repositories", "pluginRepositories"):
            el = root.find(name)
            if el is not None and ed.find(name) is None:
                ed.copy_into(ed.root, el, ordered=True)
        props = root.properties()
        if all_props:
            wanted = list(props)
        else:
            text = "".join("".join(el.itertext()) for el in ed.children(ed.root))
            wanted = [k for k in props if k.startswith(("project.", "maven."))]
            todo = re.findall(r"\$\{([^}]+)\}", text)
            while todo:
                k = todo.pop()
                if k in props and k not in wanted:
                    wanted.append(k)
                    todo.extend(re.findall(r"\$\{([^}]+)\}", props[k] or ""))
        for k in props:
            if k in wanted and not ed.has_property(k) and k != NAME_PROP:
                ed.set_property(k, props[k])

    @staticmethod
    def _inherit_build(c: _Ctx, ed: PomEditor, anc: PomEditor, *, boot: bool) -> list[str]:
        """Boot as-is / MSA pom 전용(라운드 9c): 저장소 안 pom 의 상속을 끊는 모듈이 그 pom 의 <dependencies>
        와 <build><plugins> 를 받는다(이미 선언된 것, <inherited>false</inherited> 플러그인, boot 가 아닌
        모듈의 spring-boot-maven-plugin 제외) — 부모만 바꾸면 물려받던 의존성이 사라져 컴파일이 깨지므로.
        복사한 g:a 키를 돌려준다.

        Boot as-is / MSA pom-only (round 9c): a module that stops inheriting an in-repo pom gets that pom's
        `<dependencies>` and `<build><plugins>` (not already declared; `<inherited>false</inherited>` plugins and,
        for non-boot modules, spring-boot-maven-plugin skipped). Returns the g:a keys copied."""
        copied: list[str] = []
        have = {f"{g.key}:{g.type or 'jar'}:{g.classifier or ''}" for g in ed.dependencies()}
        for d in anc.dependency_elements(False):
            g = anc.gav_of(d)
            ident = f"{g.key}:{g.type or 'jar'}:{g.classifier or ''}"
            if ident in have:
                continue
            have.add(ident)
            ed.copy_into(ed.ensure_path("dependencies"), d)
            copied.append(g.key)
        own = {ed.plugin_key(p) for p in ed.plugin_elements("build")}
        for p in anc.plugin_elements("build"):
            key = anc.plugin_key(p)
            if key in own or (anc.text_of(p, "inherited") or "").strip() == "false" \
                    or (key == ":".join(BOOT_PLUGIN) and not boot):
                continue
            own.add(key)
            ed.copy_into(ed.ensure_path("build/plugins"), p)
            copied.append(key)
        return copied

    def _role_for(self, c: _Ctx, pom: str, parent: GAV) -> Any:
        """as-is <parent> 와 같은 groupId:artifactId 인 역할 부모 설정(boot_parent / comp_parent, 없으면
        None). 두 키가 같으면 모듈 위치(boot 모듈 디렉토리인가)로 정한다.

        The configured role parent an as-is `<parent>` with the same groupId:artifactId maps to (None if none).
        When boot_parent and comp_parent share the key, the module location decides."""
        A = c.cfg.artifacts
        hits = [r for r in (A.boot_parent, A.comp_parent) if r is not None and r.key == parent.key]
        if not hits:
            return None
        if len(hits) == 1:
            return hits[0]
        boot_dir = c.p(c.boot_rel)
        return A.boot_parent if posixpath.dirname(pom) == boot_dir else A.comp_parent

    def _align_role_parents(self, c: _Ctx, *, report_modules: bool = False, skip: set[str] | None = None) -> None:
        """R2: 루트를 뺀 모든 프로젝트 pom 중 <parent> 가 boot_parent / comp_parent 의
        groupId:artifactId 인 것(버전 무관, 예: 이전 마이그레이션의 1.0.0)을 설정된 GAV + <relativePath/> 로
        맞춘다. ``report_modules`` (이미 재구성된 경우): 회사 모듈(boot / back / front) pom 이 다른 부모를
        가지면 노트만 남기고 둔다.

        R2: every project pom (root excluded) whose `<parent>` has the groupId:artifactId of `boot_parent` /
        `comp_parent` (any version, e.g. an earlier migration's 1.0.0) moves to the configured GAV with
        `<relativePath/>`. `report_modules`: a module/boot-jar or module/comp-jar pom on a different parent is noted
        and left (already-restructured pass)."""
        A = c.cfg.artifacts
        if A.boot_parent is None and A.comp_parent is None:
            return
        root_rel = c.p("pom.xml")
        role_dirs = {c.p(c.boot_rel): A.boot_parent, c.p(c.comp_rel): A.comp_parent}
        if c.front_rel:
            role_dirs[c.p(c.front_rel)] = A.comp_parent
        for pom in self._project_poms(c):
            if pom == root_rel or pom in (skip or ()):
                continue
            try:
                ed = PomEditor(c.ws.abs(pom))
            except PomEditError:
                continue
            parent = ed.get_parent()
            if parent is None:
                continue
            spec = self._role_for(c, pom, parent)
            if spec is None:
                want = role_dirs.get(posixpath.dirname(pom))
                if report_modules and want is not None and parent.key != A.parent.key:
                    c.note(f"{pom} inherits {parent.key} (not the role parent {want.key}); parent left unchanged")
                continue
            new = spec.gav()
            if parent.version == new.version and ed.parent_relative_path() == "":
                continue
            if not ed.group_id() and parent.group_id:
                ed.set_coordinate("groupId", ed.effective_group_id())
            if not ed.version() and parent.version:
                ed.set_coordinate("version", parent.version)
            ed.set_parent(new, None)
            c.save(ed)
            if parent.version != new.version:
                self._record_parent(c, pom, parent, new)
                c.note(f"{pom}: role parent {parent.key} {parent.version or '?'} → {new.version}")

    @staticmethod
    def _mandatory_managed(spec) -> bool:
        """필수 사내 아티팩트가 BOM 형(``scope: import`` + ``type: pom``)인가 — 그러면 <dependencyManagement>
        에만 둔다(Maven 은 <dependencies> 의 import scope 를 거부).

        `scope: import` (+ `type: pom`) = a BOM-style mandatory artifact: it belongs in <dependencyManagement>,
        never in <dependencies> (Maven rejects import scope there)."""
        return (getattr(spec, "scope", None) or "").strip().lower() == "import"

    def _mandatory_align_existing(self, c: _Ctx, ed: PomEditor, spec, managed: bool, where: str) -> None:
        """이미 선언된 필수 사내 아티팩트를 설정에 맞춘다: 버전(None → 부모가 관리), 설정에 있으면
        <type> / <scope>. pom 패키징 사내 아티팩트를 <type>pom</type>
        없이 선언하면 없는 jar 를 찾아 빌드가 실패하기 때문.

        An existing declaration of a mandatory artifact takes the configured version (None → managed by the
        parent) and, when the spec says so, the configured `<type>` / `<scope>` — a pom-packaged in-house artifact
        declared without `<type>pom</type>` would otherwise be resolved as a jar that does not exist."""
        for e in ed.dependency_elements(managed):
            g = ed.gav_of(e)
            if g.key != spec.key:
                continue
            aligned: list[str] = []
            if g.version != spec.version:
                ed.set_ref_version(e, spec.version)
                aligned.append("version")
            if spec.type and (g.type or "jar") != spec.type:
                ed.set_child_text(e, "type", spec.type)
                aligned.append(f"type={spec.type}")
            if spec.scope and (g.scope or "compile") != spec.scope:
                ed.set_child_text(e, "scope", spec.scope)
                aligned.append(f"scope={spec.scope}")
            if aligned:
                c.note(f"mandatory {spec.key} {where} {c.ws.rel(ed.path)}; {', '.join(aligned)} aligned")

    def _add_mandatory(self, c: _Ctx, ed: PomEditor) -> None:
        """필수 사내 아티팩트를 이 pom 에 추가(BOM 형이면 dependencyManagement); 이미 있으면 설정 버전 /
        type / scope 로 맞춘다. 새로 넣은 것은 결과의 mandatory_added 에 기록."""
        for spec in c.cfg.artifacts.mandatory:
            managed = self._mandatory_managed(spec)
            added = (ed.add_managed_dependency(spec.gav(), if_absent=True) if managed
                     else ed.add_dependency(spec.gav(), if_absent=True))
            if added:
                if spec.key not in c.result.mandatory_added:
                    c.result.mandatory_added.append(spec.key)
            else:
                self._mandatory_align_existing(c, ed, spec, managed, "already present in")

    def _align_mandatory(self, c: _Ctx, ed: PomEditor) -> None:
        """필수 사내 아티팩트를 루트에 두는 설정(``mandatory_target: root``)에서, 모듈 pom 에 따로 남은 선언은
        추가 대신 설정 버전 / type / scope 로만 맞춘다.

        A pom that keeps its own declaration of a mandatory artifact (declared in the root, `mandatory_target:
        root`) gets the configured version (None → managed by the parent) and type / scope."""
        for spec in c.cfg.artifacts.mandatory:
            self._mandatory_align_existing(c, ed, spec, self._mandatory_managed(spec), "declared in")

    def _remove_unwanted(self, c: _Ctx, ed: PomEditor) -> None:
        """``artifacts.remove_when_present`` 에 적힌 g:a 를 의존성·관리 의존성·플러그인에서
        지운다(있을 때만, 노트)."""
        for key in c.cfg.artifacts.remove_when_present:
            if ":" not in key:
                continue
            gid, aid = key.split(":", 1)
            hit = ed.remove_dependency(gid, aid)
            hit = ed.remove_managed_dependency(gid, aid) or hit
            hit = ed.remove_plugin(gid, aid) or hit
            if hit:
                c.note(f"removed {key} from {c.ws.rel(ed.path)} (remove_when_present)")

    def _boot_bom(self, c: _Ctx, ed: PomEditor) -> None:
        """Spring Boot BOM: 사내 부모가 Boot 를 관리하면(``parent_manages_spring_boot``) as-is
        ``spring-boot-dependencies`` import 를 지우고(두 관리가 다투지 않게), 아니면 BOM 을 대상 버전으로
        맞추거나 dependencyManagement 맨 앞에 넣는다."""
        A = c.cfg.artifacts
        bom_aid = "spring-boot-dependencies"
        # 사내 부모가 Boot 버전을 관리: as-is BOM import 가 남으면 두 관리가 다투므로 지운다
        if A.parent_manages_spring_boot:
            if ed.remove_managed_dependency(BOOT_GROUP, bom_aid):
                c.note(f"as-is {BOOT_GROUP}:{bom_aid} import removed from {c.ws.rel(ed.path)} "
                       "(the in-house parent manages Spring Boot)")
            return
        target = c.cfg.target.spring_boot_version
        if ed.has_managed_dependency(BOOT_GROUP, bom_aid):
            for e in ed.dependency_elements(True):
                if ed.gav_of(e).key == f"{BOOT_GROUP}:{bom_aid}":
                    ed.set_ref_version(e, target)
        else:
            ed.add_managed_dependency(GAV(group_id=BOOT_GROUP, artifact_id=bom_aid, version=target, type="pom",
                                          scope="import"), first=True)
            c.note(f"{BOOT_GROUP}:{bom_aid}:{target} BOM imported in {c.ws.rel(ed.path)}")

    def _fix_java(self, c: _Ctx, ed: PomEditor, *, top: bool) -> None:
        """Java 버전 설정 정리: 사내 부모가 java 버전을 관리하면(``parent_manages_java_version``) java
        속성과 compiler 플러그인의 source / target / release 를 지운다. 아니면 source / target 을 빼고
        ``maven.compiler.release`` = 대상(21)(``top`` 이거나 이미 있을 때), ``java.version`` 도 대상으로;
        compiler 플러그인의 source / target 은 release 로 바꾼다."""
        target = c.cfg.target.java_version
        manages = c.cfg.artifacts.parent_manages_java_version
        props = ed.properties()
        if manages:
            for k in JAVA_PROPS:
                if k in props:
                    ed.remove_property(k)
        else:
            for k in ("maven.compiler.source", "maven.compiler.target"):
                if k in props:
                    ed.remove_property(k)
            # release 는 java 수준을 정하는 pom(top: 외부 부모를 받는 pom)이나 이미 적혀 있던 pom 에만: 루트를
            # 상속하는 모듈은 물려받는다
            if top or "maven.compiler.release" in props:
                ed.set_property("maven.compiler.release", target)
            if "java.version" in props:
                ed.set_property("java.version", target)
        for p in ed.find_plugins(*COMPILER_PLUGIN, where="all"):
            conf = ed.child(p, "configuration")
            if conf is None:
                continue
            had = False
            for n in ("source", "target", "testSource", "testTarget"):
                el = ed.child(conf, n)
                if el is not None:
                    ed.remove_node(el)
                    had = True
            rel = ed.child(conf, "release")
            if manages:
                if rel is not None:
                    ed.remove_node(rel)
            elif rel is not None or had:
                ed.set_child_text(conf, "release", target)
            ed.remove_if_empty(conf)

    # ================================================================== optional bumps / cleanup (all poms)
    def _project_poms(self, c: _Ctx) -> list[str]:
        """루트 pom 먼저, 그다음 프로젝트의 다른 pom 전부(src/ 아래 pom 은 테스트 데이터라 뺀다).

        Root pom first, then every other pom of the project (poms under src/ are test data, skipped)."""
        root_rel = c.p("pom.xml")
        poms = [p for p in c.ws.pom_files()
                if "/src/" not in f"/{p}" and (not c.base or p.startswith(c.base + "/")) and p != root_rel]
        return ([root_rel] if c.ws.exists(root_rel) else []) + poms

    def _all_poms_pass(self, c: _Ctx, *, java_fix: bool, boot_plugin_fix: bool = False,
                       only: set[str] | None = None) -> None:
        """모든 프로젝트 pom(또는 ``only``)에: remove_when_present, (옵션) java 버전 / boot 플러그인 버전
        정리, 트리거가 있는 선택 사내 아티팩트 도입(v2.1n), 선택 사내 아티팩트 버전 올림. 속성 주인은 어느
        프로젝트 pom 이든 될 수 있어 전부 읽는다.

        remove_when_present (+ java / boot plugin fixes), the optional artifacts their triggers call for (v2.1n,
        `_apply_optional_triggers`) and optional bumps on every project pom, or only on the poms in `only` (property
        owners may still be any project pom). Every mode runs this before `_pin_asis_versions` (v2.1l), so an
        optional added here is a protected key there."""
        paths = self._project_poms(c)
        eds: dict[str, PomEditor] = {}
        for p in paths:
            try:
                eds[p] = PomEditor(c.ws.abs(p))
            except PomEditError as e:
                c.note(f"skipped unparsable pom {p}: {e}")
        for p, ed in eds.items():
            if only is not None and p not in only:
                continue
            self._remove_unwanted(c, ed)
            if java_fix:
                self._fix_java(c, ed, top=False)
            if boot_plugin_fix:
                self._fix_boot_plugin(c, ed)
        # v2.1n: 트리거 의존성이 있으면 선택 사내 아티팩트를 먼저 선언하고, 그다음 버전 맞춤이 다른 선언과 똑같이 본다
        self._apply_optional_triggers(c, eds, editable=only)
        self._bump_optionals(c, eds, editable=only)
        for ed in eds.values():
            c.save(ed)

    # ================================================================== optional bump only (spring phase)
    def _optional_bump(self, c: _Ctx) -> None:
        """mode ``optional_bump``: 선택 사내 아티팩트 버전 올림과 remove_when_present 만(이동·부모 변경
        없음). ``params['target']`` 이 있으면 그 디렉토리 아래 pom 과 그 저장소 안 부모들만. v2 계획은 이
        모드를 쓰지 않는다(다른 모드 안에서 함께 올림)."""
        root_rel = c.profile.root_pom.path if c.profile.root_pom else "pom.xml"
        c.base = posixpath.dirname(root_rel) if c.ws.exists(root_rel) else ""
        target = c.params.get("target")
        only: set[str] | None = None
        if target and posixpath.normpath(str(target)) not in ("", "."):
            t = posixpath.normpath(str(target))
            if t.endswith(".xml"):
                t = posixpath.dirname(t)
            c.base = ""
            below = [p for p in self._project_poms(c) if p == f"{t}/pom.xml" or p.startswith(t + "/")]
            if not below:
                c.note(f"optional_bump: no pom below {t}; nothing to do")
                return
            only = set(below)
            for p in below:
                only.update(self._chain(c, p))
        before = len(c.result.optional_bumped)
        self._all_poms_pass(c, java_fix=False, only=only)
        n = len(c.result.optional_bumped) - before
        where = f" below {target}" if only is not None else ""
        c.note(f"optional_bump: {n} optional artifact version(s) bumped{where}" if n
               else f"optional_bump: nothing to bump{where}")
        c.commit("spring: optional artifact bumps" + (f" ({target})" if only is not None else ""))

    def _fix_boot_plugin(self, c: _Ctx, ed: PomEditor) -> None:
        """spring-boot-maven-plugin 버전: 사내 부모가 Boot 를 관리하면 버전을 지우고, 아니면 리터럴 버전을
        대상 Boot 버전으로(``${...}`` 참조는 그대로)."""
        manages = c.cfg.artifacts.parent_manages_spring_boot
        for p in ed.find_plugins(*BOOT_PLUGIN, where="all"):
            v = ed.text_of(p, "version")
            if manages and v:
                ed.set_child_text(p, "version", None)
            elif not manages and v and v != c.cfg.target.spring_boot_version and not property_ref(v):
                ed.set_child_text(p, "version", c.cfg.target.spring_boot_version)

    def _apply_optional_triggers(self, c: _Ctx, eds: dict[str, PomEditor],
                                 editable: set[str] | None = None) -> None:
        """v2.1n: ``when_present`` 트리거가 있는 선택 사내 아티팩트 도입(모든 모드, ``_bump_optionals`` 직전). 고칠 수
        있는 프로젝트 pom 의 ``<dependencies>`` 에 트리거가 있고(scope 무관; dependencyManagement 만 있는 것은 쓰는
        것이 아니므로 아님) 그 pom 이나 저장소 안 조상이 이 아티팩트(별칭 포함)를 아직 선언하지 않았으면 그 pom 에
        선언한다 — 버전·scope·type 은 설정대로(버전 None = <version> 없음, 부모가 관리), scope 가 없으면 처음 맞은
        트리거의 scope(test 는 test 로). ``replaces: true`` 면 맞은 트리거 선언(그 exclusions 포함)과, 더는 아무도
        쓰지 않는 그 버전 속성을 지운다. 멱등. 생성한 boot / front pom 은 스스로 트리거를 선언했을 때만 해당.

        For every optional spec with `when_present` and every editable project pom whose `<dependencies>` declare a
        trigger (`ArtifactSpec.triggered_by`; own modules and `${…}` coordinates never match): unless the optional's
        key or an alias is already declared there or in an in-repo ancestor, declare it (spec version / scope / type;
        no scope → the first matched declaration's, `system` / `import` dropped); `replaces: true` removes the matched
        `<dependencies>` entries (exclusions included) and a version property nothing references any more. Fills
        `optional_added` / `optional_replaced`, one note (the Handover reads it), journal `optional_triggers`.
        Idempotent: once added (and the triggers removed), a second run finds the optional declared."""
        specs = [s for s in c.cfg.artifacts.optional if s.when_present]
        if not specs or not eds:
            return
        own = self._own_keys(eds)
        added: list[str] = []
        replaced: list[str] = []
        dropped_props: list[tuple[str, str]] = []           # (pom, property) the removed triggers used
        for p, ed in eds.items():
            if editable is not None and p not in editable:
                continue
            above: set[str] | None = None
            drop: list[Any] = []
            for spec in specs:
                hits: list[tuple[Any, GAV]] = []
                for el in ed.dependency_elements(False):
                    g = ed.gav_of(el)
                    if not g.group_id or not g.artifact_id or "${" in g.key or g.key in own:
                        continue
                    if el not in drop and spec.triggered_by(g.key):
                        hits.append((el, g))
                if not hits:
                    continue
                names = {spec.key, *spec.aliases}
                here = {g.key for g in ed.dependencies()}
                if above is None:
                    above = self._ancestor_dependency_keys(c, p, eds)
                # 이미 선언(이 pom 또는 물려주는 저장소 안 조상): 버전 맞춤(_bump_optionals)만 — 다시 넣지 않는다
                if names & (here | above):
                    continue
                scope = spec.scope or (hits[0][1].scope if hits[0][1].scope not in ("system", "import") else None)
                ed.add_dependency(GAV(group_id=spec.group_id, artifact_id=spec.artifact_id, version=spec.version,
                                      scope=scope, type=spec.type))
                matched = list(dict.fromkeys(g.key for _el, g in hits))
                added.append(f"{spec.key} → {p} (for {', '.join(matched)})")
                if not spec.replaces:
                    continue
                for el, g in hits:
                    drop.append(el)
                    replaced.append(f"{g.key} removed from {p} → {spec.key}")
                    prop = property_ref(g.version)
                    if prop:
                        dropped_props.append((p, prop))
            for el in drop:
                ed.remove_node(el)
            if drop:
                ed.remove_if_empty(ed.child(ed.root, "dependencies"))
        self._drop_unused_properties(c, eds, dropped_props)
        if not added and not replaced:
            return
        res = c.result
        res.optional_added += [x for x in added if x not in res.optional_added]
        res.optional_replaced += [x for x in replaced if x not in res.optional_replaced]
        c.note("optional artifacts added for trigger dependencies: " + "; ".join(added)
               + (" — replaced: " + "; ".join(replaced) if replaced else ""))
        log.info("optional triggers: added %s%s", "; ".join(added),
                 f"; replaced {'; '.join(replaced)}" if replaced else "")
        try:
            c.ws.journal({"event": "optional_triggers", "added": added, "replaced": replaced})
        except OSError as e:  # pragma: no cover - the journal is bookkeeping, never a reason to fail
            log.debug("optional_triggers journal skipped: %s", e)

    def _ancestor_dependency_keys(self, c: _Ctx, pom: str, eds: dict[str, PomEditor]) -> set[str]:
        """저장소 안 조상 pom 들의 ``<dependencies>`` g:a (자식이 물려받는 선언). 이번 패스에서 바뀐 것을 보도록
        ``eds`` 의 편집기를 먼저 쓴다."""
        out: set[str] = set()
        for q in self._chain(c, pom)[1:]:
            ed = eds.get(q)
            if ed is None:
                try:
                    ed = PomEditor(c.ws.abs(q))
                except PomEditError:
                    continue
            out |= {g.key for g in ed.dependencies()}
        return out

    @staticmethod
    def _referencing_poms(eds: dict[str, PomEditor], prop: str) -> list[str]:
        """``${prop}`` 를 아직 참조하는 프로젝트 pom 들(의존성·플러그인·다른 속성 어디든) — 그 속성은 지우지 않는다."""
        ref = f"${{{prop}}}"
        return [q for q, ed in eds.items() if ref in ed.to_string()]

    def _drop_unused_properties(self, c: _Ctx, eds: dict[str, PomEditor], props: list[tuple[str, str]]) -> None:
        """지운 트리거가 쓰던 버전 속성(``${prop}``)을, 어느 프로젝트 pom 에서도 더는 참조하지 않으면 정의한
        pom(그 pom, 아니면 저장소 안 조상)에서 지운다 — 남은 참조가 있으면 그대로 둔다."""
        for p, prop in dict.fromkeys(props):
            if self._referencing_poms(eds, prop):
                continue                                     # another dependency / plugin still uses it
            # 그 선언이 볼 수 있던 정의만: 자기 pom, 그다음 저장소 안 조상(형제 pom 의 속성은 손대지 않는다)
            for q in self._chain(c, p):
                ed = eds.get(q)
                if ed is not None and ed.has_property(prop):
                    ed.remove_property(prop)
                    ed.remove_if_empty(ed.child(ed.root, "properties"))
                    c.note(f"property {prop} removed from {q} (only the replaced trigger used it)")
                    break

    def _bump_optionals(self, c: _Ctx, eds: dict[str, PomEditor], editable: set[str] | None = None) -> None:
        """선택 사내 아티팩트 버전 올림(모든 모드 공통). 이미 선언된 참조만(의존성, dependencyManagement,
        플러그인 의존성, annotationProcessorPaths) — 전이로만 들어오는 것은 추가하지 않는다(S6, 회사 규칙:
        쓰는 것만 올린다). 1) 별칭 → 정식 좌표, 2) 버전: ``${prop}`` 이면 그 속성의 주인 pom 에서, 리터럴이면
        그 자리(설정된 property_name 이 있으면 속성으로), 버전 없는 선언이면 ``<artifactId>.version`` 같은
        관례 속성이 있을 때 그 속성. ``editable`` 은 고칠 pom 범위(None = 전부). 멱등.

        Shared by every mode. `editable` restricts which poms' references are touched (None = all); property
        owners may be any pom in `eds`. Idempotent: an already bumped project produces no `optional_bumped` entry.
        Only declared references are bumped (dependencies, dependencyManagement, plugin dependencies,
        annotationProcessorPaths); an optional artifact that arrives only transitively is never added (S6)."""
        keys = c.cfg.optional_keys()
        if not keys or not eds:
            return
        root_path = next(iter(eds))
        targets = {p: ed for p, ed in eds.items() if editable is None or p in editable}
        for p, ed in targets.items():                  # 1. alias → canonical coordinates
            for ref in ed.artifact_refs():
                spec = keys.get(ref.key)
                if spec and ref.key != spec.key:
                    ed.rename_ref(ref.element, spec.group_id, spec.artifact_id)
                    c.note(f"{ref.key} renamed to {spec.key} in {p}")
        managed = {r.key for ed in eds.values() for r in ed.artifact_refs() if r.section == "dependencyManagement"}
        done_props: set[tuple[str, str]] = set()

        def owner_of(prop: str, here: str) -> tuple[str, PomEditor] | None:
            for cand in (here, root_path, *eds.keys()):
                if eds[cand].has_property(prop):
                    return cand, eds[cand]
            return None

        # floor(기본): as-is 가 더 새 버전이면 내리지 않는다 / pin: 설정 버전으로 정확히 맞춘다
        policy = getattr(c.cfg.artifacts, "optional_version_policy", "floor")

        def needs(cur: str | None, target: str) -> bool:
            """pin: any difference; floor: only when the current version is unknown/unparsable or older."""
            if cur == target:
                return False
            if policy != "floor" or cur is None or property_ref(cur):
                return True
            lt = version_lt(cur, target)
            if lt is None:                 # unparsable → be conservative and set the configured version
                return True
            if not lt:
                c.note(f"{target}: kept newer as-is version {cur} (optional_version_policy=floor)")
            return lt

        def record(key: str, old: str | None, new: str) -> None:
            entry = f"{key} {old or 'managed'}→{new}"
            if entry not in c.result.optional_bumped:
                c.result.optional_bumped.append(entry)

        for p, ed in targets.items():                  # 2. versions
            for ref in ed.artifact_refs():
                spec = keys.get(ref.key)
                if not spec or not spec.version:
                    continue
                old = ref.version
                prop = property_ref(old)
                if prop:
                    own = owner_of(prop, p)
                    if own is not None:
                        # 같은 속성을 여러 참조가 공유하면 한 번만 올리고 한 번만 기록한다
                        if (own[0], prop) in done_props:
                            continue
                        done_props.add((own[0], prop))
                        cur = own[1].get_property(prop)
                        if needs(cur, spec.version):
                            own[1].set_property(prop, spec.version)
                            record(spec.key, cur, spec.version)
                        continue
                    ed.set_ref_version(ref.element, spec.version)
                    record(spec.key, old, spec.version)
                elif old:
                    own = owner_of(spec.property_name, p) if spec.property_name else None
                    if own is not None:
                        cur = own[1].get_property(spec.property_name or "")
                        if needs(cur, spec.version):
                            own[1].set_property(spec.property_name or "", spec.version)
                        ed.set_ref_version(ref.element, f"${{{spec.property_name}}}")
                        if old != spec.version or cur != spec.version:
                            record(spec.key, old, spec.version)
                    elif needs(old, spec.version):
                        ed.set_ref_version(ref.element, spec.version)
                        record(spec.key, old, spec.version)
                else:
                    # 버전 없는 직접 의존성인데 프로젝트 안에서 관리되는 것: 관리하는
                    # 쪽(dependencyManagement)에서 올린다
                    if ref.section == "dependencies" and ref.key in managed:
                        continue                       # bumped where it is managed
                    if ref.section == "dependencyManagement":
                        continue
                    # version managed by the (Boot/in-house) parent: prefer the conventional override property
                    # (`<artifactId>.version`, as Boot's BOM uses) or the configured property_name when one exists
                    # in the project, instead of pinning a literal that would shadow the property.
                    conv = spec.property_name or f"{spec.artifact_id}.version"
                    own = owner_of(conv, p)
                    if own is not None:
                        if (own[0], conv) in done_props:
                            continue
                        done_props.add((own[0], conv))
                        cur = own[1].get_property(conv)
                        if needs(cur, spec.version):
                            own[1].set_property(conv, spec.version)
                            record(spec.key, cur, spec.version)
                        continue
                    ed.set_ref_version(ref.element, spec.version)
                    record(spec.key, None, spec.version)

    # ================================================================== v2.1l: as-is version carry-over
    @staticmethod
    def _protected_keys(c: _Ctx) -> set[str]:
        """버전을 config 가 정하는 g:a: 사내 부모·역할 부모·필수·선택·remove_when_present
        아티팩트 — as-is 버전 이어 쓰기도 관리 버전 정리도 건드리지 않는다.

        g:a keys whose versions the config owns (parent / role parents / mandatory / optional /
        remove_when_present)."""
        A = c.cfg.artifacts
        return ({A.parent.key} | {r.key for r in (A.boot_parent, A.comp_parent) if r is not None}
                | {s.key for s in A.mandatory} | set(c.cfg.optional_keys())
                | {k for k in A.remove_when_present if ":" in k})

    @staticmethod
    def _own_keys(eds: dict[str, PomEditor]) -> set[str]:
        """프로젝트 자신의 모듈 g:a (``${project.groupId}:a`` 표기 포함) — 모듈끼리의 버전은
        reactor 가 정한다."""
        own: set[str] = set()
        for ed in eds.values():
            a = ed.artifact_id()
            if a:
                own |= {f"{ed.effective_group_id()}:{a}", f"${{project.groupId}}:{a}"}
        return own

    @staticmethod
    def _capture_module(by_module: dict[str, Any], ed: PomEditor) -> tuple[str | None, dict[str, Any] | None]:
        """캡처의 ``by_module`` 에서 이 pom 의 항목(키, 값): effective g:a 로, 없으면 같은
        artifactId 로 찾는다(관리 버전 정리와 같은 규칙). 재구성이 새로 만든 모듈
        (``{name}-back``)은 as-is 캡처에 없다 → (None, None).

        The pom's `by_module` entry of a capture, by effective g:a then by artifactId; (None, None) when
        absent."""
        aid = ed.artifact_id() or ""
        key = f"{ed.effective_group_id()}:{aid}"
        mod = by_module.get(key)
        if isinstance(mod, dict):
            return key, mod
        for k, v in by_module.items():
            if aid and k.split(":")[-1] == aid and isinstance(v, dict):
                return k, v
        return None, None

    @staticmethod
    def _asis_parent(by_module: dict[str, Any], key: str | None, default: Any) -> str | None:
        """as-is 모듈이 버전을 물려받은 부모(g:a:v): 저장소 안 부모(캡처에 모듈로 있는 것)를
        따라 올라가 처음 만나는 바깥 부모. 모르면 캡처 전체의 ``parent``.

        The parent (g:a:v) an as-is module's managed versions came from: in-repo parents (modules of the
        capture) are followed up to the first one outside it; the capture's top-level `parent` when
        unknown."""
        par: str | None = None
        cur = key
        for _ in range(20):
            mod = by_module.get(cur) if cur else None
            p = mod.get("parent") if isinstance(mod, dict) else None
            if not p:
                break
            par = str(p)
            ga = ":".join(par.split(":")[:2])
            if ga not in by_module or ga == cur:
                break
            cur = ga
        return par or (str(default) if default else None)

    def _asis_version(self, data: dict[str, Any], mkey: str | None, mod: dict[str, Any] | None,
                      key: str) -> tuple[str, str] | None:
        """해결 순서 2단계: 모듈의 direct → 모듈의 managed → 합친 managed → 합친 direct 에서
        g:a 의 as-is 버전. 합친 범위는 managed 가 먼저다 — 버전 없는 선언은 관리 버전으로
        풀렸고, 합친 direct 는 '먼저 나온 모듈이 이김' 이라 다른 모듈이 직접 적은 버전일 수
        있다. (버전, 출처) — 그 범위의 managed 와 같은 버전이면 ``managed by <as-is 부모>``,
        아니면 ``declared`` (as-is pom 이 직접 적었는데 그 뒤 빠졌다: OpenRewrite / 병합).
        못 찾으면 None.

        Resolution order step 2: the module's `direct`, its `managed`, then the merged `managed`, `direct`
        (a version-less declaration was resolved by management; the merged `direct` is first-wins and may hold
        another module's explicit version). (version, source) — `managed by <as-is parent>` when the scope's
        managed map has that version, else `declared`; None when the as-is build did not resolve g:a."""
        by_module: dict[str, Any] = data.get("by_module") or {}
        scopes: list[tuple[dict[str, str], dict[str, str], str | None]] = []
        if mod is not None:
            scopes.append((mod.get("direct") or {}, mod.get("managed") or {},
                           self._asis_parent(by_module, mkey, data.get("parent"))))
        scopes.append((data.get("direct") or {}, data.get("managed") or {},
                       str(data["parent"]) if data.get("parent") else None))
        for i, (direct, managed, parent) in enumerate(scopes):
            # by_module: direct → managed; merged (the last scope): managed → direct
            last = i == len(scopes) - 1
            v = (managed.get(key) or direct.get(key)) if last else (direct.get(key) or managed.get(key))
            if not v or "${" in str(v):
                continue
            v = str(v)
            src = (f"managed by {parent or 'the as-is dependencyManagement'}" if managed.get(key) == v
                   else "declared")
            return v, src
        return None

    def _non_filtered_extensions(self, c: _Ctx) -> None:
        """v2.1m: 모듈 pom 이 확정된 뒤(as-is 버전 이어 쓰기 직전) 필터링되는 리소스 아래 바이너리 파일의 확장자를
        maven-resources-plugin(/ webResources 면 maven-war-plugin) ``<nonFilteredFileExtensions>`` 에 적는다
        (``resources_filtering``: 스캔 + ``build.non_filtered_extensions``). 결과의 ``non_filtered_extensions``,
        노트 한 줄, 확장자 없는 파일은 후속 작업, 저널 ``non_filtered_extensions``. 실패해도 재구성을 멈추지 않는다.

        v2.1m (docs/INTERFACES.md "v2.1m"): binary files under a `<filtering>true</filtering>` resource fail
        maven-resources-plugin's copy (`MalformedInputException: Input length = 1`); `binary_resource_plan` on the
        current tree (the module poms are final here) + `apply_non_filtered_extensions` write the missing
        `<nonFilteredFileExtension>` entries. Fills `result.non_filtered_extensions` ("<pom>: ext1, ext2 →
        maven-resources-plugin[, maven-war-plugin]"), one note, a follow-up per extension-less binary, journal
        `non_filtered_extensions`. Never fails the restructure."""
        from .resources_filtering import apply_non_filtered_extensions, binary_resource_plan

        try:
            plan = binary_resource_plan(c.ws, c.cfg, self._project_poms(c))
            changed = apply_non_filtered_extensions(c.ws, plan)
        except Exception as e:  # noqa: BLE001 - an add-on: the first build reports what is left
            log.warning("nonFilteredFileExtensions skipped: %s: %s", type(e).__name__, e)
            return
        lines = plan.lines(only=changed)
        c.changed.update(changed)
        for line in lines:
            if line not in c.result.non_filtered_extensions:
                c.result.non_filtered_extensions.append(line)
        for f in plan.followups:
            c.followup(f)
        if lines:
            c.note("nonFilteredFileExtensions written for binary files under filtered resources (filtering stays "
                   "on): " + "; ".join(lines))
            log.info("nonFilteredFileExtensions: %s", "; ".join(lines))
        if not changed and not plan.followups:
            return
        try:
            c.ws.journal({"event": "non_filtered_extensions",
                          "poms": {p: plan.extensions_by_pom.get(p, []) for p in changed},
                          "plugins": {p: plan.plugins_by_pom.get(p, []) for p in changed},
                          "followups": list(plan.followups)})
        except OSError as e:  # pragma: no cover - the journal is bookkeeping, never a reason to fail
            log.debug("non_filtered_extensions journal skipped: %s", e)

    def _pin_asis_versions(self, c: _Ctx) -> None:
        """v2.1l: 새 부모(comp-jar / boot-jar / base-parent)로 옮기며 버전을 잃은 의존성에 as-is
        빌드가 쓰던 버전을 적는다(해결 순서 2단계). ``params['asis_effective']`` = 오케스트레이터가
        넘기는 인자 없는 함수(접수 때 캡처한 ``logs/effective-depmgmt.json``); 없거나 비면 노트
        한 줄만. 바로 뒤 ``_drop_managed_versions`` 가 재구성 뒤 effective pom 으로 새 부모 체인이
        관리하는 것은 다시 비운다(1단계) — 그래서 순서가 pin → 캡처 → drop: 버전이 빠진 pom
        으로는 Maven 이 effective pom 을 만들지 못한다.

        v2.1l (resolution order step 2, docs/INTERFACES.md "v2.1l"): every project pom's `<dependencies>`
        entry without `<version>` gets the version the as-is build resolved (`_asis_version` on
        `params["asis_effective"]()`, the intake capture in the shape of `sbmig.build.effective`). Not
        touched: parent / role parents / mandatory / optional / remove_when_present artifacts, the project's
        own modules, system scope, non-jar types and classifiers (the captures key g:a only, so the
        managed-version step could not verify such a pin; the build reports them if they are missing),
        entries managed inside the project's pom chain, entries that have a version (`${…}` included). No
        capture → one note, nothing pinned. An entry the as-is capture does not resolve gets the repository's
        newest release (`_repository_release`, step 3) — one version left empty makes `help:effective-pom`
        fail model validation, and without that capture every pin would be undone; `_drop_managed_versions`
        empties such a pin whenever the new chain manages the artifact. What the repository cannot answer
        either is "unknown" only when the as-is project declared it (`profile.poms`): a starter the
        restructure generated or a coordinate OpenRewrite renamed was left version-less for the new chain on
        purpose. The pins and the unknown ones are kept on the context; `_drop_managed_versions` (always
        called right after) settles them through `_settle_asis_versions`."""
        c.asis_pins, c.asis_unknown, c.asis_before = {}, [], {}
        data = self._asis_capture(c)
        if data is None:
            c.note("as-is version carry-over skipped: no as-is effective pom")
            return
        protected = self._protected_keys(c)
        by_module: dict[str, Any] = data.get("by_module") or {}
        eds: dict[str, PomEditor] = {}
        for p in self._project_poms(c):
            try:
                eds[p] = PomEditor(c.ws.abs(p))
            except PomEditError as e:
                c.note(f"as-is version carry-over: skipped unparsable pom {p}: {e}")
        own = self._own_keys(eds)
        # '버전을 모른다' 는 as-is 프로젝트가 선언했던 의존성에만 — 재구성이 만든 boot 모듈의
        # starter, OpenRewrite 가 바꾼 jakarta 좌표는 as-is 빌드에 없던 것이고 새 부모 체인이
        # 관리하도록 일부러 버전 없이 둔 것이다
        inventory = [*c.profile.poms, c.profile.root_pom] if c.profile.root_pom else list(c.profile.poms)
        asis_keys = {d.key for pi in inventory for d in (pi.dependencies or [])
                     if d.group_id and d.artifact_id}
        c.asis_keys = asis_keys
        for p, ed in eds.items():
            mkey, mod = self._capture_module(by_module, ed)
            # 프로젝트 pom 체인(자기 / 저장소 안 조상)의 dependencyManagement 가 관리하는 것은
            # 그쪽이 버전을 준다
            local: set[str] = set()
            for q in self._chain(c, p):
                q_ed = eds.get(q)
                if q_ed is None:
                    try:
                        q_ed = PomEditor(c.ws.abs(q))
                    except PomEditError:
                        continue
                local |= {d.key for d in q_ed.managed_dependencies() if d.version}
            for el in ed.dependency_elements(False):
                g = ed.gav_of(el)
                if g.version or not g.group_id or not g.artifact_id or "${" in g.key:
                    continue
                if g.key in protected or g.key in own or (g.scope or "") == "system" or g.key in local:
                    continue
                if (g.type or "jar") != "jar" or g.classifier:
                    continue
                found = self._asis_version(data, mkey, mod, g.key)
                kind = "asis"
                if found is None:
                    # 3단계(저장소): 버전이 하나라도 비면 재구성 뒤 effective pom 캡처가 모델 검증에서 실패해
                    # 맞게 적은 버전까지 모두 되돌려진다 → 캡처 전에 채우고, 새 체인이 관리하면 drop 이 비운다
                    rel = self._repository_release(c, g.group_id, g.artifact_id)
                    if rel is None:
                        if g.key in asis_keys and (p, g.key) not in c.asis_unknown:
                            c.asis_unknown.append((p, g.key))
                        continue
                    found, kind = (rel, REPO_PIN_SOURCE), "repository"
                v, src = found
                if p not in c.asis_before:
                    # 새 체인이 관리해 다시 비우면 pom 이 바이트 그대로 돌아온다 → 바뀐 파일
                    # 목록에서 뺄 수 있게
                    c.asis_before[p] = (c.ws.abs(p).read_bytes(), p in c.changed)
                ed.set_ref_version(el, v)
                how = f"as-is {src}" if kind == "asis" else src
                c.asis_pins[(p, g.key)] = (v, f"{g.key}: {v} ← {how} ({p})", kind)
        for ed in eds.values():
            c.save(ed)
        for _v, entry, _kind in c.asis_pins.values():
            if entry not in c.result.versions_pinned:
                c.result.versions_pinned.append(entry)

    @staticmethod
    def _asis_capture(c: _Ctx) -> dict[str, Any] | None:
        """as-is 접수 캡처(``params['asis_effective']()``)를 재구성 한 번에 한 번만 읽는다. 쓸 수 없으면(없음 / 실패 /
        빈 모양) None — 이어 쓰기와 레시피가 올린 버전 판정(v2.1o)이 같은 값을 본다."""
        if c.asis_data_loaded:
            return c.asis_data
        c.asis_data_loaded = True
        fn = c.params.get("asis_effective")
        data: Any = None
        if callable(fn):
            try:
                data = fn()
            except Exception as e:  # noqa: BLE001 - a broken capture must never fail the restructure
                log.warning("as-is effective pom for the version carry-over failed: %s: %s",
                            type(e).__name__, e)
                data = None
        if not isinstance(data, dict) or not (data.get("direct") or data.get("managed") or data.get("by_module")):
            data = None
        c.asis_data = data
        return data

    def _repository_release(self, c: _Ctx, group_id: str, artifact_id: str) -> str | None:
        """해결 순서 3단계: 저장소(``build.repository_url`` → settings 미러 → Central)가 가진 g:a 의 최신
        정식 릴리스(프리릴리스 제외). 조회기는 재구성 한 번에 하나, 처음 쓸 때 만든다. 실패 → None, 예외
        없음.

        Resolution order step 3: the newest release `RepositoryLookup.versions` lists for g:a that
        `is_prerelease` does not reject; the lookup is made once per restructure call, on first use. None on
        any failure (never raises)."""
        try:
            if not c.repo_lookup_made:
                c.repo_lookup_made = True
                from sbmig.build import repo_versions
                c.repo_lookup = repo_versions.RepositoryLookup(c.cfg)
            if c.repo_lookup is None:
                return None
            from sbmig.build.repo_versions import is_prerelease
            versions = list(c.repo_lookup.versions(group_id, artifact_id) or [])
            return next((str(v) for v in reversed(versions) if v and not is_prerelease(str(v))), None)
        except Exception as e:  # noqa: BLE001 - no repository answer = unknown, never a failed restructure
            log.debug("repository releases of %s:%s unavailable: %s: %s", group_id, artifact_id,
                      type(e).__name__, e)
            return None

    def _undo_asis_pins(self, c: _Ctx) -> list[tuple[str, str]]:
        """이번 패스가 적은 as-is 버전을 지운다(아직 그 값 그대로인 것만). 지운 (pom, g:a) 목록."""
        by_pom: dict[str, dict[str, str]] = {}
        for (p, k), (v, _e, _kind) in c.asis_pins.items():
            by_pom.setdefault(p, {})[k] = v
        undone: list[tuple[str, str]] = []
        for p, keys in by_pom.items():
            try:
                ed = PomEditor(c.ws.abs(p))
            except PomEditError:
                continue
            for el in ed.dependency_elements(False):
                g = ed.gav_of(el)
                if keys.get(g.key) is not None and g.version == keys[g.key]:
                    ed.remove_version(el)
                    if (p, g.key) not in undone:
                        undone.append((p, g.key))
            c.save(ed)
        return undone

    def _settle_asis_versions(self, c: _Ctx, managed_of: dict[str, dict[str, str]] | None,
                              unpinned: list[tuple[str, str, str]]) -> None:
        """as-is 버전 이어 쓰기 마무리(``_drop_managed_versions`` 끝에서): 새 부모 체인이 관리해
        다시 비운 것(``unpinned``)과 남은 것을 결과·노트·저널에 반영하고, 버전을 모르는 것은 새
        체인도 관리하지 않으면 후속 작업으로 남긴다. 새 체인의 effective pom 이 없으면
        (``managed_of`` None) 적은 버전을 모두 되돌린다 — 새 체인이 관리하는지 모르는 채 남기면
        Boot starter 가 옛 Boot 버전으로 고정될 수 있다; 정말 빠진 것은 빌드가 'version is
        missing' 으로 알려 주고 수정 루프가 재빌드로 검증하며 채운다.

        Settle this pass's carry-over: pins the new chain manages (`unpinned`, emptied again — step 1) leave
        `versions_pinned`; a kept repository pin gets a follow-up asking the owners to confirm the version;
        version-less entries neither the as-is build nor the repository resolved become `versions_unknown` +
        a follow-up unless the new chain manages them; one summary note per outcome; journal
        `versions_pinned`. Without the new chain's effective pom (`managed_of` None: Maven unavailable or
        failed, e.g. on a version still missing) every pin of this pass is undone: whether the new chain
        manages it is unknown, and a kept pin could fix a Boot starter at the as-is Boot version — the build
        reports what is really missing."""
        pins, unknown = c.asis_pins, list(c.asis_unknown)
        if not pins and not unknown:
            return
        undone: list[tuple[str, str]] = self._undo_asis_pins(c) if managed_of is None and pins else []
        removed = {(p, k) for p, k, _m in unpinned} | set(undone)
        for pk in removed:
            entry = pins[pk][1] if pk in pins else None
            if entry in c.result.versions_pinned:
                c.result.versions_pinned.remove(entry)  # type: ignore[arg-type]
        for p, (raw, was_changed) in c.asis_before.items():
            try:
                if not was_changed and c.ws.abs(p).read_bytes() == raw:
                    c.changed.discard(p)
            except OSError:
                pass
        kept_pk = [pk for pk in pins if pk not in removed]
        kept = [pins[pk][1] for pk in kept_pk]
        kept_asis = [pins[pk][1] for pk in kept_pk if pins[pk][2] == "asis"]
        kept_repo = [pk for pk in kept_pk if pins[pk][2] == "repository"]
        # 되돌린 저장소 버전은 as-is 가 선언했던 의존성이면 다시 '모름' 이다
        for pk in undone:
            if pins[pk][2] == "repository" and pk[1] in c.asis_keys and pk not in unknown:
                unknown.append(pk)
        still: list[str] = []
        for p, k in unknown:
            if managed_of is not None and (managed_of.get(p) or {}).get(k):
                continue                                    # step 1: the new chain manages it → stays empty
            item = f"{k} ({p})"
            still.append(item)
            if item not in c.result.versions_unknown:
                c.result.versions_unknown.append(item)
            c.followup(f"declare a version for {k} in {p} or manage it in the parent — the as-is build did "
                       "not resolve one")
        for p, k in kept_repo:
            c.followup(f"confirm {k} {pins[(p, k)][0]} in {p}: the as-is build did not resolve a version, so "
                       "sbmig took the newest release the repository has")
        unpinned_asis = [(p, k, m) for p, k, m in unpinned if pins.get((p, k), ("", "", "asis"))[2] == "asis"]

        def few(items: list[str]) -> str:
            uniq = list(dict.fromkeys(items))               # the same g:a in several poms is named once
            return ", ".join(uniq[:5]) + (" …" if len(uniq) > 5 else "")
        if kept_asis:
            c.note(f"{len(kept_asis)} dependency version(s) carried over from the as-is build (not managed "
                   f"by the new parent chain): {few([e.split(': ')[0] for e in kept_asis])}")
        if kept_repo:
            named = [f"{k} {pins[(p, k)][0]}" for p, k in kept_repo]
            c.note(f"{len(kept_repo)} dependency version(s) taken from the repository (newest release; the "
                   f"as-is build did not resolve them): {few(named)}")
        if unpinned_asis:
            # 저장소 버전을 새 체인이 관리해 다시 비운 것은 캡처를 위한 임시 값이었다 → 저널에만
            c.note(f"{len(unpinned_asis)} as-is version(s) not carried over: managed by the new parent chain "
                   f"({few([f'{k} {m}' for _p, k, m in unpinned_asis])})")
        if undone:
            c.note(f"as-is version carry-over undone for {len(undone)} dependency(ies): no effective pom of "
                   "the new parent chain (and no Boot BOM facts) to tell which of them it manages — the build "
                   f"reports the missing ones ({few([k for _p, k in undone])})")
        if still:
            c.note(f"{len(still)} version-less dependency(ies) the as-is build did not resolve either: "
                   f"{few(still)}")
        try:
            c.ws.journal({"event": "versions_pinned", "pinned": kept, "unknown": still,
                          "from_repository": [pins[pk][1] for pk in kept_repo],
                          "managed_by_new_chain": [f"{k} {m} ({p})" for p, k, m in unpinned],
                          "undone": [f"{k} ({p})" for p, k in undone]})
        except OSError as e:  # pragma: no cover - the journal is bookkeeping, never a reason to fail
            log.debug("versions_pinned journal skipped: %s", e)

    # ================================================================== managed-version cleanup (round 9)
    def _drop_managed_versions(self, c: _Ctx) -> None:
        """``layout.drop_managed_versions``: 새 부모 체인이 관리하는 버전을 <dependencies> 에서 지운다(gte =
        관리 버전이 같거나 더 새 것일 때만, 비교 불가면 유지; any = 항상; off). ``params['effective_pom']``
        (오케스트레이터가 넘기는 인자 없는 함수)가 지금 트리의 effective dependencyManagement 를 준다 — Maven
        이 없어 None 이면 건너뛴다. 사내 부모·필수·선택·remove_when_present 아티팩트, 자기 모듈, system scope,
        jar 가 아닌 type / classifier, dependencyManagement, 플러그인은 건드리지 않는다.
        v2.1l: 바로 앞 ``_pin_asis_versions`` 가 적은 as-is 버전도 같은 규칙으로 확인한다(off 여도
        gte; 비교 불가면 비움), 저장소 버전은 새 체인이 관리하기만 하면 비운다 — 다시 비운 것은
        ``managed_versions_dropped`` 에 넣지 않고(as-is pom 이 적은 버전이 아니므로), 더 새것이라 남긴
        것은 '이어 쓴 버전' 으로 따로 알린다. off 이고 적은 것이 없으면 캡처하지 않는다. 끝에
        ``_settle_asis_versions``.

        `layout.drop_managed_versions`: drop explicit `<dependencies>` versions the new parent chain manages.

        `params["effective_pom"]` is a zero-arg callable returning the effective dependency management of the tree
        *as it is now* (shape of `sbmig.build.effective.load_effective`) or None (Maven unavailable). The managed
        version of the declaring module (`by_module`, matched by g:a then artifactId) is used when that module is in
        the capture, else the merged `managed` map. gte: drop only when managed ≥ explicit (unknown comparison →
        keep); any: always. Never touched: parent / mandatory / optional / remove_when_present artifacts, the
        project's own modules, system scope, non-jar types or classifiers (managed per type:classifier),
        dependencyManagement entries, plugins. Unresolvable `${…}` versions are kept; properties stay.

        v2.1l: the as-is versions `_pin_asis_versions` wrote just before (`c.asis_pins`) are checked by the
        same rule — gte when the mode is `off`, and an unknown comparison empties the pin (resolution order
        step 1: the new chain manages it); a repository pin is emptied whenever the new chain manages the
        artifact. An emptied pin is not an "explicit version dropped" (the as-is pom never declared it), and a
        pin kept because it is newer is reported as a carried-over version, not as an explicit one;
        `_settle_asis_versions` reports the outcome. With `off` and nothing pinned, nothing is captured.

        v2.1o / v2.1p: without that capture, `_fallback_capture` (the in-house parent chain's facts read from its poms,
        plus the Boot BOM for poms importing it when no parent manages Boot) decides instead — the pins it manages are
        emptied, the others stay (no undo); a note names the source (`effective pom` / `parent chain facts`; neither →
        the skip note and the undo)."""
        mode = c.cfg.layout.drop_managed_versions
        pins = c.asis_pins
        # v2.1l: off 여도 이번 패스가 적은 버전은 새 체인이 관리하는지 확인해야 한다(1단계). 적은 것이 없으면
        # 캡처하지 않는다 — '모름' 만 남았다면 그 빈 버전 때문에 Maven 캡처는 어차피 실패한다
        if mode == "off" and not pins:
            self._settle_asis_versions(c, None, [])
            return
        fn = c.params.get("effective_pom")
        data: Any = None
        if callable(fn):
            try:
                data = fn()
            except Exception as e:  # noqa: BLE001 - a broken capture must never fail the restructure
                log.warning("effective pom for the managed-version cleanup failed: %s: %s", type(e).__name__, e)
                data = None
        per_pom: dict[str, dict[str, str]] | None = None
        if not isinstance(data, dict) or not (data.get("managed") or data.get("by_module")):
            # v2.1p: 캡처가 없으면 사내 부모 체인의 사실로(부모 pom 들 + import BOM, 필요하면 Boot BOM) — 부모가 Boot 를
            # 관리하지 않으면 BOM 을 import 하는 pom 에는 Boot BOM 도. 둘 다 없으면 예전처럼 건너뛰고 되돌린다
            fallback = self._fallback_capture(c)
            if fallback is None:
                if mode != "off":
                    c.note("managed-version cleanup skipped: no effective pom")
                c.managed_source = "none"
                self._settle_asis_versions(c, None, [])
                return
            data, per_pom, why = fallback
            c.managed_source = "parent chain facts"
            c.note(f"managed-version source: {why}; the effective pom of the new parent chain is unavailable")
        else:
            c.managed_source = "effective pom"
            c.note("managed-version source: effective pom of the new parent chain")
        protected = self._protected_keys(c)
        merged_managed: dict[str, str] = data.get("managed") or {}
        by_module: dict[str, Any] = data.get("by_module") or {}
        eds: dict[str, PomEditor] = {}
        for p in self._project_poms(c):
            try:
                eds[p] = PomEditor(c.ws.abs(p))
            except PomEditError as e:
                c.note(f"managed-version cleanup: skipped unparsable pom {p}: {e}")
        own = self._own_keys(eds)
        dropped: list[str] = []
        kept_newer: list[str] = []
        kept_pin_newer: list[str] = []
        unpinned: list[tuple[str, str, str]] = []
        managed_of: dict[str, dict[str, str]] = {}
        dropped_refs: list[tuple[str, str]] = []      # v2.1o: (pom, property) a dropped explicit version referenced
        recipe_dropped: list[str] = []                # v2.1o: newer than managed, but only because a recipe raised it
        asis = self._asis_capture(c) if mode == "gte" else None
        for p, ed in eds.items():
            amkey, amod = self._capture_module(asis.get("by_module") or {}, ed) if asis else (None, None)
            aid = ed.artifact_id() or ""
            mod = by_module.get(f"{ed.effective_group_id()}:{aid}")
            if mod is None:
                mod = next((v for k, v in by_module.items() if aid and k.split(":")[-1] == aid), None)
            managed = (mod.get("managed") or {}) if isinstance(mod, dict) else merged_managed
            if per_pom is not None and p in per_pom:
                managed = per_pom[p]             # v2.1p: this pom imports the Boot BOM (no parent manages Boot)
            managed_of[p] = managed
            props: dict[str, str] = {}
            # 버전 속성을 풀 때 조상 pom 의 속성부터 쌓고 자기
            # pom 이 마지막에 덮는다(Maven 상속과 같은 우선순위)
            for q in reversed(self._chain(c, p)):          # top-most ancestor first, the pom itself wins
                q_ed = eds.get(q)
                if q_ed is None:
                    try:
                        q_ed = PomEditor(c.ws.abs(q))
                    except PomEditError:
                        continue
                props.update(q_ed.properties())
            for el in ed.dependency_elements(False):
                g = ed.gav_of(el)
                if not g.version or g.key in protected or g.key in own or (g.scope or "") == "system":
                    continue
                if (g.type or "jar") != "jar" or g.classifier:
                    continue
                pin = pins.get((p, g.key))
                is_pin = pin is not None and pin[0] == g.version
                if mode == "off" and not is_pin:
                    continue
                explicit = self._resolve_version(ed, g.version, props)
                m = managed.get(g.key)
                if explicit is None or not m:
                    continue
                if is_pin and pin is not None and pin[2] == "repository":
                    pass        # step 1 before step 3: the new chain manages it → empty, whatever the version
                # gte: 관리 버전이 더 낮으면 지우지 않는다 — 지우면 버전이 내려가 쓰던 API 가 사라질 수 있다
                elif mode in ("gte", "off"):
                    cmp = compare_versions(m, explicit)
                    if cmp is None and not is_pin:
                        continue
                    if cmp is not None and cmp < 0:
                        # v2.1o: 관리 버전보다 새 명시 버전이라도 as-is 빌드는 관리 버전 이하(또는 없었다)였다면 레시피가
                        # 올린 것(Framework 7 레시피의 7.0.x = 저장소 최신)이지 담당자의 고정이 아니다 → 지운다
                        asis_v = self._recipe_raised(asis, amkey, amod, g.key, m) if not is_pin else False
                        if asis_v is not False:
                            ed.remove_version(el)
                            recipe_dropped.append(f"{g.key} {explicit} (as-is {asis_v or '-'}, managed {m}, {p})")
                            dropped_refs += [(p, name) for name in re.findall(r"\$\{([^}]+)\}", g.version)]
                            continue
                        # sbmig 가 적은 as-is 버전은 as-is pom 의 명시 버전이 아니다 → 따로 보고
                        kept_as = kept_pin_newer if is_pin else kept_newer
                        kept_as.append(f"{g.key} {explicit} (managed {m}, {p})")
                        continue
                ed.remove_version(el)
                if is_pin:
                    unpinned.append((p, g.key, m))
                else:
                    dropped.append(f"{g.key}: {explicit} → managed {m} ({p})")
                    dropped_refs += [(p, name) for name in re.findall(r"\$\{([^}]+)\}", g.version)]
        # v2.1o: 지운 명시 버전만 쓰던 버전 속성(spring.version=5.3.31 …)은 이제 아무도 안 쓴다 → 함께 지운다
        self._drop_dead_version_properties(c, eds, dropped_refs)
        for ed in eds.values():
            c.save(ed)
        c.result.managed_versions_dropped.extend(d for d in dropped if d not in c.result.managed_versions_dropped)
        c.result.recipe_versions_dropped.extend(d for d in recipe_dropped if d not in c.result.recipe_versions_dropped)
        for d in recipe_dropped:
            c.note(f"recipe-raised version dropped: {d}")
        if dropped:
            c.note(f"{len(dropped)} explicit dependency version(s) dropped: managed by the new parent chain "
                   f"(layout.drop_managed_versions={mode}): {', '.join(d.split(': ')[0] for d in dropped[:5])}"
                   f"{' …' if len(dropped) > 5 else ''}")
        if kept_newer:
            c.note(f"explicit version(s) newer than the parent-managed one kept: {', '.join(kept_newer[:5])}"
                   f"{' …' if len(kept_newer) > 5 else ''}")
        if kept_pin_newer:
            c.note("carried-over version newer than the parent-managed one kept: "
                   f"{', '.join(kept_pin_newer[:5])}{' …' if len(kept_pin_newer) > 5 else ''}")
        self._settle_asis_versions(c, managed_of, unpinned)

    # ================================================================== v2.1o: Boot BOM facts
    def _boot_bom_facts(self, c: _Ctx) -> Any:
        """``sbmig.build.boot_bom`` 의 사실을 재구성 한 번에 한 번만 읽는다(못 읽으면 빈 사실). 모듈 속성으로 불러
        테스트가 바꿔 끼울 수 있다(tests/conftest.py)."""
        if not c.bom_facts_loaded:
            c.bom_facts_loaded = True
            try:
                from sbmig.build import boot_bom
                c.bom_facts = boot_bom.load_boot_bom_facts(c.cfg)
            except Exception as e:  # noqa: BLE001 - facts are an aid, never a failed restructure
                log.debug("Boot BOM facts unavailable: %s: %s", type(e).__name__, e)
                c.bom_facts = None
        return c.bom_facts

    def _bom_importers(self, c: _Ctx) -> set[str]:
        """``spring-boot-dependencies`` 를 ``<scope>import</scope>`` 하는 프로젝트 pom 과, 그 pom 을 저장소 안에서
        상속하는 pom 들(부모가 Boot 를 관리하지 않는 설정에서 재구성이 BOM 을 적은 곳)."""
        importing: set[str] = set()
        poms = self._project_poms(c)
        for p in poms:
            try:
                ed = PomEditor(c.ws.abs(p))
            except PomEditError:
                continue
            if any(g.key == f"{BOOT_GROUP}:spring-boot-dependencies" and (g.scope or "") == "import"
                   for g in ed.managed_dependencies()):
                importing.add(p)
        return {p for p in poms if importing & set(self._chain(c, p))} if importing else set()

    def _chain_facts(self, c: _Ctx) -> Any:
        """v2.1p: 사내 부모 체인의 사실(``sbmig.build.parent_chain``)을 재구성 한 번에 한 번만 읽고 저널
        ``parent_chain_facts`` 를 남긴다(못 읽으면 빈 사실). 모듈 속성으로 불러 테스트가 바꿔 끼울 수 있다."""
        if not c.chain_facts_loaded:
            c.chain_facts_loaded = True
            try:
                from sbmig.build import parent_chain
                c.chain_facts = parent_chain.load_parent_chain_facts(c.cfg)
            except Exception as e:  # noqa: BLE001 - facts are an aid, never a failed restructure
                log.debug("parent chain facts unavailable: %s: %s", type(e).__name__, e)
                c.chain_facts = None
            f = c.chain_facts
            if f is not None:
                try:
                    c.ws.journal({"event": "parent_chain_facts", "roots": list(f.roots), "poms": f.poms,
                                  "managed": len(f.managed), "properties": len(f.properties),
                                  "boot_bom_added": bool(f.boot_bom_added), "unreadable": list(f.unreadable)})
                except (OSError, AttributeError) as e:  # pragma: no cover - bookkeeping only
                    log.debug("parent_chain_facts journal skipped: %s", e)
        return c.chain_facts

    def _fallback_capture(self, c: _Ctx) -> tuple[dict[str, Any], dict[str, dict[str, str]] | None, str] | None:
        """재구성 뒤 캡처가 없을 때 쓸 관리 버전(v2.1p): 사내 부모 체인의 사실(모든 pom 에) + 부모가 Boot 를 관리하지
        않으면 BOM 을 import 하는 pom 에 Boot BOM(체인의 선언이 이김). ``(캡처 모양, pom 별 관리 버전 | None, 출처 문구)``,
        아무것도 없으면 None.

        The managed source when the post-restructure capture is None: the parent chain's facts for every pom (the
        modules inherit the configured parents) and, when `artifacts.parent_manages_spring_boot` is false, the Boot
        BOM's facts for the poms importing `spring-boot-dependencies` (the chain's declared entries win, as in Maven).
        None when neither could be read."""
        chain = self._chain_facts(c)
        chain_managed: dict[str, str] = dict(chain.managed) if chain is not None and not chain.empty else {}
        per_pom: dict[str, dict[str, str]] | None = None
        extra = ""
        if not c.cfg.artifacts.parent_manages_spring_boot:
            importers = self._bom_importers(c)
            boot = self._boot_bom_facts(c) if importers else None
            if boot is not None and not getattr(boot, "empty", True):
                per_pom = {p: {**boot.managed, **chain_managed} for p in importers}
                extra = (f"Boot BOM {boot.version} ({len(boot.managed)} managed) for the {len(importers)} pom(s) "
                         "importing it")
        if not chain_managed and not per_pom:
            return None
        parts = []
        if chain_managed:
            parts.append(f"parent chain facts — {', '.join(chain.roots)} ({len(chain_managed)} managed artifacts"
                         + ("; + the Boot BOM" if chain.boot_bom_added else "") + ")")
        if extra:
            parts.append(extra)
        capture = {"managed": chain_managed, "direct": {}, "plugins": {}, "properties": {}, "parent": None,
                   "modules": [], "by_module": {}}
        return capture, per_pom, " + ".join(parts)

    def _override_name_sets(self, c: _Ctx) -> tuple[set[str], set[str]]:
        """``_drop_bom_property_overrides`` 가 맡을 속성 이름: (사내 부모 체인이 정의한 버전 속성 — 체인의 pom 을 읽은 대로,
        ``layout.parent_property_names`` — 체인 사실 없이도 쓰는 비상구). 단계가 꺼져 있으면 둘 다 빈 집합. 체인 이름은
        ``*.version`` 만이고 Java 수준 속성(``java.version`` / ``maven.compiler.*``)은 빼다 — 재구성이 직접 21 로 적는
        것이라(spring-boot-starter-parent 는 java.version=17 을 정의한다) 지우면 안 된다.

        (The version properties the in-house parent chain defines — `*.version` names of `ParentChainFacts.properties`
        except the Java-level ones the restructure writes itself; `layout.parent_property_names`, the escape hatch
        that needs no facts). Both empty when `layout.drop_bom_property_overrides` is off."""
        L = c.cfg.layout
        if not getattr(L, "drop_bom_property_overrides", True):
            return set(), set()
        parent_names = {str(n).strip() for n in (getattr(L, "parent_property_names", None) or []) if str(n).strip()}
        chain = self._chain_facts(c)
        names = set(getattr(chain, "properties", None) or {}) if chain is not None else set()
        # 버전 속성만, 재구성이 소유한 Java 수준 속성은 빼고
        chain_names = {n for n in names if n.endswith(".version") and n not in JAVA_PROPS}
        return chain_names, parent_names

    def _override_names(self, c: _Ctx) -> set[str]:
        """``_drop_bom_property_overrides`` 가 맡을 속성 이름 전부(부모 체인의 것 + ``parent_property_names``)."""
        bom_names, parent_names = self._override_name_sets(c)
        return bom_names | parent_names

    def _drop_dead_version_properties(self, c: _Ctx, eds: dict[str, PomEditor],
                                      refs: list[tuple[str, str]]) -> None:
        """v2.1o: 이번 관리 버전 정리가 지운 명시 버전이 참조하던 속성(``${spring.version}``) 중 이제 어느 프로젝트 pom
        도 참조하지 않는 것을 정의한 pom(그 pom 또는 저장소 안 조상)에서 지운다 — 남겨 두면 옛 버전 값이 남아 사람과
        검토를 헷갈리게 한다. 새 부모 체인이 관리하는 버전을 지운 경우에만(그 버전들만 쓰던 속성만); Boot BOM 의 속성
        이름은 ``_drop_bom_property_overrides`` 가 따로 맡는다.

        Properties whose last references were explicit versions this pass dropped (`refs` = (pom, name) of every
        `${name}` in a dropped version) are removed from the pom that defines them (the pom itself, else an in-repo
        ancestor) when no project pom references `${name}` any more. Names the BOM property-override step will
        handle are left to it. Fills `result.version_properties_removed` ("<pom>: name=value"), one note."""
        if not refs:
            return
        skip = self._override_names(c)
        removed: list[str] = []
        for p, prop in dict.fromkeys(refs):
            if prop in skip or self._referencing_poms(eds, prop):
                continue                     # the override step decides / another dependency still uses it
            for q in self._chain(c, p):
                ed = eds.get(q)
                if ed is not None and ed.has_property(prop):
                    value = ed.get_property(prop)
                    ed.remove_property(prop)
                    ed.remove_if_empty(ed.child(ed.root, "properties"))
                    removed.append(f"{q}: {prop}={value}")
                    break
        if not removed:
            return
        res = c.result
        res.version_properties_removed += [x for x in removed if x not in res.version_properties_removed]
        shown = ", ".join(x.split(": ", 1)[1] for x in removed[:6]) + (" …" if len(removed) > 6 else "")
        c.note(f"{len(removed)} version propert(ies) removed: no dependency references them after the "
               f"parent-managed versions were dropped: {shown}")
        log.info("version properties removed after the version drop: %s", "; ".join(removed))

    def _drop_bom_property_overrides(self, c: _Ctx) -> None:
        """v2.1o / v2.1p: 프로젝트 pom 의 ``<properties>`` 중 이름이 사내 부모 체인(그 pom 들 — Boot 부모를 상속하면
        Boot BOM 의 속성까지)이 정의한 버전 속성과 같은 것을 지운다 — Maven 속성 상속으로 as-is 값
        (``spring-boot.version=2.5.15``)이 부모의 값을 이겨 옛 버전을 끌어오지 않게. 관리 버전 정리 뒤라 명시 버전은
        이미 빠졌다; 그래도 어느 프로젝트 pom 이 ``${이름}`` 을 참조하면 남기고 노트. ``layout.parent_property_names`` 는
        체인에 없어도 지울 이름(비상구). ``layout.drop_bom_property_overrides`` 로 끈다.

        Every project pom's `<properties>` entry whose name is a version property the in-house parent chain defines
        (`_override_name_sets`: read from the chain's poms by `sbmig.build.parent_chain`, the Boot BOM's included when
        the chain inherits it) or one of `layout.parent_property_names` is removed when no project pom references
        `${name}` any more (repeated until nothing changes: a removed property may have been the last reference of
        another). Fills `result.bom_properties_removed` ("<pom>: name=value") and `result.bom_property_sources`
        (name → the chain pom defining it), one note per origin, `property X kept: still referenced by <pom>` notes,
        journal `bom_property_overrides` (`source`: `parent chain` / `parent_property_names` / both). Never raises."""
        chain_names, parent_names = self._override_name_sets(c)
        names = chain_names | parent_names
        if not names:
            return
        chain = c.chain_facts
        defined_by: dict[str, str] = dict(getattr(chain, "property_sources", None) or {}) if chain is not None else {}
        eds: dict[str, PomEditor] = {}
        for p in self._project_poms(c):
            try:
                eds[p] = PomEditor(c.ws.abs(p))
            except PomEditError:
                continue
        removed: list[str] = []
        for _ in range(10):                                    # a removed property may free another one
            progress = False
            for p, ed in eds.items():
                for name, value in list(ed.properties().items()):
                    if name not in names or self._referencing_poms(eds, name):
                        continue
                    ed.remove_property(name)
                    ed.remove_if_empty(ed.child(ed.root, "properties"))
                    removed.append(f"{p}: {name}={value}")
                    progress = True
            if not progress:
                break
        kept: list[str] = []
        for p, ed in eds.items():
            for name in ed.properties():
                if name in names:
                    refs = self._referencing_poms(eds, name)
                    kept.append(f"{p}: {name} (referenced by {', '.join(refs)})")
                    c.note(f"property {name} kept: still referenced by {', '.join(refs)}")
        for ed in eds.values():
            c.save(ed)
        res = c.result
        res.bom_properties_removed += [x for x in removed if x not in res.bom_properties_removed]

        def name_of(entry: str) -> str:
            return entry.split(": ", 1)[1].split("=", 1)[0].split(" ", 1)[0]
        for x in removed:
            if name_of(x) in chain_names and defined_by.get(name_of(x)):
                res.bom_property_sources[name_of(x)] = defined_by[name_of(x)]
        # 이름이 어디서 왔는지로 문구를 나눈다: 체인이 정의한 것이 먼저(설정 목록에도 있으면 체인 쪽 설명이 더 맞다)
        by_chain = [x for x in removed if name_of(x) in chain_names]
        by_parent = [x for x in removed if name_of(x) not in chain_names]

        def shown(items: list[str]) -> str:
            return ", ".join(x.split(": ", 1)[1] for x in items[:6]) + (" …" if len(items) > 6 else "")
        if by_chain:
            roots = ", ".join(getattr(chain, "roots", None) or []) or "-"
            c.note(f"{len(by_chain)} as-is version propert(ies) removed: they override the in-house parent chain's "
                   f"properties of the same name ({roots}): {shown(by_chain)}")
        if by_parent:
            c.note(f"{len(by_parent)} as-is propert(ies) removed: they override the in-house parent's properties of "
                   f"the same name (layout.parent_property_names): {shown(by_parent)}")
        if removed:
            log.info("parent property overrides removed: %s", "; ".join(removed))
        if not removed and not kept:
            return
        # 실제로 맞은 이름의 출처만(설정 목록이 있다고 늘 두 출처를 적지 않게)
        matched = {name_of(x) for x in removed + kept}
        source = " + ".join(x for x, on in (("parent chain", bool(matched & chain_names)),
                                             ("parent_property_names", bool(matched - chain_names))) if on)
        try:
            c.ws.journal({"event": "bom_property_overrides", "removed": removed, "kept": kept, "source": source})
        except OSError as e:  # pragma: no cover - the journal is bookkeeping, never a reason to fail
            log.debug("bom_property_overrides journal skipped: %s", e)

    def _recipe_raised(self, asis: dict[str, Any] | None, mkey: str | None, mod: dict[str, Any] | None, key: str,
                       managed: str) -> str | None | bool:
        """v2.1o: 관리 버전보다 새 명시 버전이 레시피가 올린 것인가 — as-is 빌드의 그 아티팩트 버전(``_asis_version``)이
        관리 버전 이하면 그 버전, as-is 에 없었으면 None(둘 다 '지운다'), as-is 가 관리 버전보다 새것이었거나(담당자의
        고정) as-is 캡처가 없거나 비교할 수 없으면 False(남긴다).

        Whether an explicit version newer than the managed one was raised by a recipe: the as-is version when the
        as-is build had it at or below `managed`, None when the as-is build did not have the artifact (both mean
        "drop"), False when the as-is project itself was above `managed`, there is no as-is capture, or the versions
        do not compare (keep)."""
        if asis is None:
            return False
        found = self._asis_version(asis, mkey, mod, key)
        if found is None:
            return None
        cmp = compare_versions(managed, found[0])
        return found[0] if cmp is not None and cmp >= 0 else False

    @staticmethod
    def _resolve_version(ed: PomEditor, version: str, props: dict[str, str]) -> str | None:
        """리터럴 버전, 또는 pom 체인 속성으로 푼 ``${prop}`` (5 단계 중첩까지); 못 풀면
        None(그 의존성은 그대로 둔다).

        Literal version, or `${prop}` resolved from the pom chain's properties (nested up to 5 levels); None when
        something stays unresolved."""
        builtins = {"project.version": ed.effective_version() or "", "project.groupId": ed.effective_group_id() or "",
                    "project.artifactId": ed.artifact_id() or ""}
        cur = version.strip()
        for _ in range(5):
            if "${" not in cur:
                break
            cur = re.sub(r"\$\{([^}]+)\}",
                         lambda mm: props.get(mm.group(1), builtins.get(mm.group(1)) or mm.group(0)), cur)
        return None if "${" in cur or not cur else cur

    # ================================================================== Spring Boot as-is in place (round 9c)
    def _boot_pom(self, c: _Ctx) -> None:
        """mode ``boot_pom`` (라운드 9c, ``layout.boot_as_is: in-place``): Spring Boot as-is 프로젝트(이전
        사내 도구로 옮긴 것 포함)는 회사 결정대로 디렉토리를 그대로 두고 pom 만 바꾼다. 루트 →
        ``artifacts.parent`` (aggregator), boot 모듈(``profile.boot_modules``, 없으면 spring-boot-maven-plugin
        이나 @SpringBootApplication 이 있는 모듈) → ``boot_parent``, 나머지 → ``comp_parent`` (역할 부모가
        아니면 모듈은 루트를 계속 상속). 모듈 없는 단일 Boot 앱은 먼저 ``{name}-boot`` 로 감싼다. 멱등.

        `boot_pom`: a Spring Boot as-is project keeps its directories; only the poms change. Root with
        `<modules>`: root → `artifacts.parent` (aggregator), the boot modules (`profile.boot_modules`, else modules
        whose pom has spring-boot-maven-plugin or that hold a @SpringBootApplication class) → `boot_parent`, every
        other module → `comp_parent` (without role parents the modules keep inheriting the root). A root that is the
        app itself (no modules, sources present) is first wrapped into `{name}-boot`. Idempotent."""
        root_rel = c.profile.root_pom.path if c.profile.root_pom else "pom.xml"
        c.base = posixpath.dirname(root_rel)
        if not c.ws.exists(root_rel):
            raise RestructureError(f"root pom not found: {root_rel}")
        root = PomEditor(c.ws.abs(root_rel))
        self._resolve_names(c, root)
        coords = self._root_coords(root, c.profile)
        before = set(c.changed)
        wrapped = False
        if not root.modules():
            if any(c.ws.exists(c.p(d)) for d in self._source_roots(c, root)):
                self._boot_wrap(c, root, coords)
                root = PomEditor(c.ws.abs(root_rel))
                wrapped = True
            else:
                c.note("boot_pom: the root pom has no modules and no sources; pom-only edits on the root")
        boot_dirs = [c.p(c.boot_rel)] if wrapped else self._boot_dirs(c, root)
        boots, comps = self._boot_in_place(c, root, coords, boot_dirs)
        for bd in boots:
            others = tuple(o for o in boots if o != bd)
            self._write_placeholders(c, f"{bd}/src/main/resources", under=bd if others else None, others=others)
        self._placeholder_followup(c)
        self._all_poms_pass(c, java_fix=True, boot_plugin_fix=True)
        self._non_filtered_extensions(c)
        self._pin_asis_versions(c)
        self._drop_managed_versions(c)
        self._drop_bom_property_overrides(c)
        r = c.result
        if boots:
            r.boot_module = boots[0]
            try:
                r.boot_packaging = PomEditor(c.ws.abs(f"{boots[0]}/pom.xml")).packaging()
            except PomEditError:
                r.boot_packaging = None
        else:
            c.followup("boot_pom: no boot module found (no module pom with spring-boot-maven-plugin or a "
                       "@SpringBootApplication class) — every module got the comp parent")
        if len(comps) == 1:
            r.comp_module = comps[0]
        c.note(f"boot_pom: directories kept — boot module(s) {', '.join(boots) or '-'}"
               + (f", other module(s) {', '.join(comps)}" if comps else ""))
        if set(c.changed) == before and not wrapped:
            c.note("boot_pom: already in place (root and module parents already configured)")
        c.commit("restructure: boot_pom parents / mandatory / optional (directories kept)")

    def _boot_dirs(self, c: _Ctx, root: PomEditor) -> list[str]:
        """boot 모듈 디렉토리: 분석의 ``profile.boot_modules`` (이름 규칙 + pom), 없으면 <build><plugins> 에
        spring-boot-maven-plugin 이 있거나 src/main/java 에 @SpringBootApplication 이 있는 모듈.

        Root-relative dirs of the boot modules: `profile.boot_modules` (analyzer: naming rule + pom), else
        every module pom with spring-boot-maven-plugin in `<build><plugins>` or a @SpringBootApplication class
        under its `src/main/java`."""
        if c.profile.boot_modules:
            return [c.p(posixpath.normpath(d.rstrip("/"))) for d in c.profile.boot_modules]
        out: list[str] = []
        root_rel = c.p("pom.xml")
        for pom in self._project_poms(c):
            if pom == root_rel:
                continue
            d = posixpath.dirname(pom) or "."
            try:
                ed = PomEditor(c.ws.abs(pom))
            except PomEditError:
                continue
            if ed.packaging() == "pom":
                continue
            if ed.has_plugin(*BOOT_PLUGIN, where="build") or any(
                    self._locate_class(c, fqcn, [f"{d}/src/main/java"]) for fqcn in c.profile.source.main_classes):
                out.append(d)
        if out:
            c.note(f"boot_pom: boot module(s) detected by pom / @SpringBootApplication: {', '.join(out)}")
        return out

    def _boot_wrap(self, c: _Ctx, root: PomEditor, coords: GAV) -> None:
        """모듈 없는 단일 Boot 앱: ``src/**`` (+ 별도 소스 루트, ``lib/``)를 ``{name}-boot/`` 로 옮기고, 그
        pom = as-is 루트 pom(의존성, build, profiles; artifactId ``<root>-boot``, groupId / version 명시,
        패키징 유지)이 루트를 상속한다. 루트는 관리 항목(properties, dependencyManagement, pluginManagement,
        repositories)만 남긴 aggregator. 부모 지정은 ``_boot_in_place`` 가 한다.

        Single-module Boot app: `src/**` (+ custom source roots, `lib/`) → `{name}-boot/`, whose pom is the as-is
        root pom (dependencies, build, profiles; artifactId `<root>-boot`, explicit groupId / version, packaging
        kept) inheriting the root; the root keeps only its management (properties, dependencyManagement,
        pluginManagement, repositories) and becomes the aggregator. `_boot_in_place` then sets the parents."""
        L = c.cfg.layout
        boot = c.p(c.boot_rel)
        if c.ws.exists(boot):
            raise RestructureError(f"boot_pom: {boot} already exists; cannot wrap the root module into it")
        root_rel = c.ws.rel(root.path)
        asis = c.ws.read(root_rel)
        for d in [*self._source_roots(c, root), "lib"]:
            if c.ws.exists(c.p(d)):
                c.move(c.p(d), f"{boot}/{d}")
        c.write(f"{boot}/pom.xml", asis)
        ed = PomEditor(c.ws.abs(f"{boot}/pom.xml"))
        for name in ("modules", "properties", "dependencyManagement", "repositories", "pluginRepositories",
                     "distributionManagement", "build/pluginManagement"):
            ed.remove_element(name)
        ed.set_parent(GAV(group_id=coords.group_id, artifact_id=coords.artifact_id, version=coords.version),
                      posixpath.relpath(c.p("pom.xml"), boot))
        ed.set_coordinate("groupId", coords.group_id)
        ed.set_coordinate("artifactId", coords.artifact_id + L.boot_artifact_suffix)
        ed.set_coordinate("version", coords.version)
        asis_dir = c.base or ""
        self._relocate_module_system_deps(c, ed, asis_dir, boot)
        ed.save()
        if not root.group_id():
            root.set_coordinate("groupId", coords.group_id)
        if not root.version():
            root.set_coordinate("version", coords.version)
        root.remove_all_dependencies()
        root.remove_element("profiles")
        build = root.find("build")
        if build is not None:
            for el in list(root.children(build)):
                if PomEditor.local_name(el) != "pluginManagement":
                    root.remove_node(el)
            root.remove_if_empty(build)
        root.set_packaging("pom")
        root.set_modules([c.boot_rel])
        c.save(root)
        c.note(f"boot_pom: single-module Spring Boot app wrapped into {c.boot_rel} (sources, dependencies, build and "
               "profiles moved; the root pom is the aggregator)")
        c.commit(f"restructure: wrap the Boot app into {c.boot_rel}")

    def _boot_in_place(self, c: _Ctx, root: PomEditor, coords: GAV,
                       boot_dirs: list[str]) -> tuple[list[str], list[str]]:
        """모든 모듈 pom 과 루트의 부모 / 좌표 / 필수 사내 아티팩트 / BOM / java 버전(``_boot_pom`` 참고).
        저장소 안 pom 의 상속을 끊는 모듈(역할 부모)은 그 pom 들의 의존성·build 플러그인·관리 항목을
        복사받는다. 처리한 (boot 모듈들, 나머지 모듈들)을 돌려준다.

        Parents / coordinates / mandatory / BOM / java of every module pom and the root (see `_boot_pom`).
        A module that stops inheriting in-repo poms (role parents) gets their dependencies, build plugins and
        management. Returns (boot module dirs, other module dirs) that were processed."""
        A = c.cfg.artifacts
        role = self._role(c)
        root_rel = c.p("pom.xml")
        in_root = self._mandatory_in_root(c)
        boots: list[str] = []
        comps: list[str] = []
        cache: dict[str, PomEditor] = {}

        def load(p: str) -> PomEditor:
            if p not in cache:
                cache[p] = PomEditor(c.ws.abs(p))
            return cache[p]

        for pom in self._project_poms(c):
            if pom == root_rel:
                continue
            d = posixpath.dirname(pom) or "."
            try:
                ed = load(pom)
            except PomEditError as e:
                c.note(f"boot_pom: skipped unparsable pom {pom}: {e}")
                continue
            chain = self._chain(c, pom)
            is_boot = d in boot_dirs
            # 루트가 아닌 저장소 안 중간 pom 을 상속하는 모듈은
            # 건너뛴다: 그 중간 pom 자신이 처리되며 부모가 바뀐다
            if len(chain) > 1 and chain[1] != root_rel and not is_boot:
                continue                     # inherits an in-repo intermediate pom, which is re-pointed itself
            if not role:
                if root_rel in chain:
                    (boots if is_boot else comps).append(d)
                    continue                 # keeps inheriting the root (which gets the in-house parent)
                new, mandatory = A.parent.gav(), True     # its own root (external / no parent)
            else:
                spec = A.boot_parent if is_boot else A.comp_parent
                new, mandatory = spec.gav(), not in_root                               # type: ignore[union-attr]
            old = ed.get_parent()
            if not ed.group_id():
                ed.set_coordinate("groupId", ed.effective_group_id() or coords.group_id)
            if not ed.version():
                ed.set_coordinate("version", ed.effective_version() or coords.version)
            copied: list[str] = []
            for anc in chain[1:]:
                aed = load(anc)
                copied += self._inherit_build(c, ed, aed, boot=is_boot)
                self._inherit_root_content(c, ed, aed, exclude=set())
            if copied:
                c.note(f"{pom}: {len(copied)} dependency / plugin declaration(s) inherited from "
                       f"{', '.join(chain[1:])} copied into the module ({', '.join(copied[:5])}"
                       f"{' …' if len(copied) > 5 else ''})")
            if old is None or old.key != new.key or old.version != new.version or ed.parent_relative_path() != "":
                ed.set_parent(new, None)
            self._record_parent(c, pom, old, new)
            if mandatory:
                self._add_mandatory(c, ed)
            else:
                self._align_mandatory(c, ed)      # an as-is declaration of a mandatory artifact: configured version
            self._remove_unwanted(c, ed)
            self._boot_bom(c, ed)
            self._fix_java(c, ed, top=True)
            c.save(ed)
            (boots if is_boot else comps).append(d)
        # the root: aggregator on the in-house parent
        old = root.get_parent()
        if not root.group_id() and root.effective_group_id():
            root.set_coordinate("groupId", root.effective_group_id())
        if not root.version() and root.effective_version():
            root.set_coordinate("version", root.effective_version())
        new = A.parent.gav()
        if old is None or old.key != new.key or old.version != new.version or root.parent_relative_path() != "":
            root.set_parent(new, None)
        self._record_parent(c, root_rel, old, new)
        if root.modules():
            root.set_packaging("pom")
        if role:
            # 역할 부모: 모듈이 루트를 상속하지 않으므로 루트 의존성·관리 항목은 의미가 없다 — 필수 사내
            # 아티팩트(루트에 두는 설정)만 남긴다
            keep = {s.key for s in A.mandatory} if in_root else set()
            for el in root.dependency_elements(False):
                if root.gav_of(el).key not in keep:
                    root.remove_node(el)
            root.remove_if_empty(root.find("dependencies"))
            root.remove_element("dependencyManagement")
            root.remove_element("build/pluginManagement")
            root.remove_build_plugins_except([])
            if in_root:
                self._add_mandatory(c, root)
            self._remove_unwanted(c, root)
            self._fix_java(c, root, top=False)
            self._prune_root_properties(root)
        else:
            self._add_mandatory(c, root)
            self._remove_unwanted(c, root)
            self._fix_java(c, root, top=True)
            self._boot_bom(c, root)
        c.save(root)
        if role:
            c.note(f"boot_pom role parents: root {A.parent.key}, boot module(s) {A.boot_parent.key}, other "  # type: ignore[union-attr]
                   f"module(s) {A.comp_parent.key} (mandatory dependencies in "                              # type: ignore[union-attr]
                   f"{'the root pom' if in_root else 'each module pom'})")
        return boots, comps

    # ================================================================== MSA (pom only)
    def _msa(self, c: _Ctx) -> None:
        """mode ``msa_pom``: MSA 는 디렉토리를 건드리지 않고 서비스 루트마다 pom 만(사내 부모, 필수 / 선택
        사내 아티팩트, BOM, java 버전). 서비스 루트 → 역할 부모면 ``boot_parent`` (아니면
        ``artifacts.parent``), 공유 라이브러리 루트(``params['library_roots']``, 분석의 library 서비스) →
        ``comp_parent`` 이고 필수 사내 아티팩트는 넣지 않는다. 서비스가 저장소 안 pom 을 상속하면 그
        꼭대기에서 부모를 바꾼다. ``msa_parent_policy: per-service-root`` + 역할 부모면 최상위 aggregator 가
        ``artifacts.parent`` 를 받고, 그 아래 서비스 / 라이브러리 루트는 aggregator 에서 물려받던 것을
        복사받는다. 끝으로 서비스마다 외부 설정 placeholder."""
        L = c.cfg.layout
        A = c.cfg.artifacts
        root_rel = c.profile.root_pom.path if c.profile.root_pom else "pom.xml"
        from sbmig.analyze.classifier import is_library_root

        lib_entries = [s for s in c.profile.services if is_library_root(s, c.profile)]
        svc_entries = [s for s in c.profile.services if s not in lib_entries]
        lib_poms: list[str] = [s.root_pom for s in lib_entries]
        for x in c.params.get("library_roots") or []:
            x = posixpath.normpath(str(x))
            x = x if x.endswith(".xml") else posixpath.join(x, "pom.xml")
            if x not in lib_poms:
                lib_poms.append(x)
        if L.msa_parent_policy == "top-aggregator":
            targets = [root_rel]
            if lib_poms:
                c.note("top-aggregator policy: library roots " + ", ".join(lib_poms) + " only get optional bumps / "
                       "remove_when_present (the parent is set on the top aggregator only)")
            lib_poms = []
        else:
            targets = [s.root_pom for s in svc_entries] or \
                      ([] if lib_poms else [p.path for p in c.profile.poms if p.is_root]) or \
                      ([] if lib_poms else [root_rel])
        role = self._role(c)
        top_agg = L.msa_parent_policy == "top-aggregator"
        # round 9c (role parents, per-service-root): the top aggregator gets artifacts.parent; a service / library
        # root that inherited it stops there (it gets its role parent and what it inherited from the aggregator)
        agg: str | None = None
        if role and not top_agg and c.ws.exists(root_rel) and root_rel not in targets and root_rel not in lib_poms:
            try:
                red = PomEditor(c.ws.abs(root_rel))
                if red.packaging() == "pom" and red.modules():
                    agg = root_rel
            except PomEditError:
                agg = None
        below_agg: set[str] = set()

        # 서비스 pom 에서 저장소 안 부모를 따라 올라가 부모를 바꿀 pom 을 고른다: aggregator 가 있으면 그 바로
        # 아래에서 멈춘다(aggregator 는 따로 처리)
        def climb(t: str) -> str:
            chain = self._chain(c, t)
            if agg is not None and agg in chain and chain[0] != agg:
                top_ = chain[chain.index(agg) - 1]
                below_agg.add(top_)
                return top_
            return chain[-1]

        resolved: list[str] = []
        for t in targets:
            if not c.ws.exists(t):
                c.note(f"service pom {t} not found; skipped")
                continue
            top = climb(t)
            if top != t:
                c.note(f"{t} inherits from in-repo {top}; parent set on {top}")
            if top not in resolved:
                resolved.append(top)
        lib_resolved: list[str] = []
        for t in lib_poms:
            if not c.ws.exists(t):
                c.note(f"library root pom {t} not found; skipped")
                continue
            top = climb(t)
            if top != t:
                c.note(f"library {t} inherits from in-repo {top}; parent set on {top}")
            if top not in resolved and top not in lib_resolved:
                lib_resolved.append(top)
        if not resolved and not lib_resolved:
            raise RestructureError("msa_pom: no service poms found")
        # round 9b role parents: services → boot_parent, shared libraries → comp_parent, the top aggregator
        # (policy top-aggregator; round 9c: also per-service-root) → artifacts.parent
        agg_ed = PomEditor(c.ws.abs(agg)) if agg is not None and below_agg else None
        svc_parent = A.boot_parent.gav() if role and not top_agg else A.parent.gav()   # type: ignore[union-attr]
        lib_parent = A.comp_parent.gav() if role else A.parent.gav()                    # type: ignore[union-attr]
        for t in lib_resolved:
            ed = PomEditor(c.ws.abs(t))
            old_parent = ed.get_parent()
            if old_parent is not None:
                if not ed.group_id() and old_parent.group_id:
                    ed.set_coordinate("groupId", old_parent.group_id)
                if not ed.version() and old_parent.version:
                    ed.set_coordinate("version", old_parent.version)
            if agg_ed is not None and t in below_agg:
                self._inherit_build(c, ed, agg_ed, boot=False)
                self._inherit_root_content(c, ed, agg_ed, exclude=set())
            ed.set_parent(lib_parent, None)
            self._record_parent(c, t, old_parent, lib_parent)
            self._fix_java(c, ed, top=True)
            self._boot_bom(c, ed)
            c.save(ed)
            c.note(f"msa_pom: patched library root {t} (parent only, no mandatory dependencies)")
        for t in resolved:
            ed = PomEditor(c.ws.abs(t))
            old_parent = ed.get_parent()
            if old_parent is not None:
                if not ed.group_id() and old_parent.group_id:
                    ed.set_coordinate("groupId", old_parent.group_id)
                if not ed.version() and old_parent.version:
                    ed.set_coordinate("version", old_parent.version)
            if agg_ed is not None and t in below_agg:
                self._inherit_build(c, ed, agg_ed, boot=True)
                self._inherit_root_content(c, ed, agg_ed, exclude=set())
            ed.set_parent(svc_parent, None)
            self._record_parent(c, t, old_parent, svc_parent)
            self._add_mandatory(c, ed)
            self._fix_java(c, ed, top=True)
            self._boot_bom(c, ed)
            c.save(ed)
            c.note(f"msa_pom: patched {t}")
        if agg is not None:
            self._msa_aggregator(c, agg)
        self._align_role_parents(c, skip=set(resolved) | set(lib_resolved) | ({agg} if agg else set()))
        if role:
            c.note(f"msa_pom role parents: service roots → {svc_parent.key}, library roots → {lib_parent.key}"
                   + (f", top aggregator → {A.parent.key}" if top_agg or agg is not None else "")
                   + ("; mandatory dependencies in the service roots and the top aggregator"
                      if agg is not None and self._mandatory_in_root(c) else ""))
        self._all_poms_pass(c, java_fix=True, boot_plugin_fix=True)
        self._non_filtered_extensions(c)
        self._pin_asis_versions(c)
        self._drop_managed_versions(c)
        self._drop_bom_property_overrides(c)
        self._msa_placeholders(c, svc_entries)
        c.commit("restructure: msa pom-only parent/mandatory/optional")

    def _msa_aggregator(self, c: _Ctx, agg: str) -> None:
        """라운드 9c (역할 부모): MSA 최상위 aggregator → ``artifacts.parent`` (좌표 명시, <relativePath/>),
        ``mandatory_target: root`` 면 필수 사내 아티팩트도 여기에.

        Round 9c (role parents): the MSA top aggregator → `artifacts.parent` (explicit coordinates,
        `<relativePath/>`), the mandatory dependencies there too with `mandatory_target: root`."""
        A = c.cfg.artifacts
        ed = PomEditor(c.ws.abs(agg))
        old = ed.get_parent()
        if old is not None:
            if not ed.group_id() and old.group_id:
                ed.set_coordinate("groupId", old.group_id)
            if not ed.version() and old.version:
                ed.set_coordinate("version", old.version)
        new = A.parent.gav()
        if old is None or old.key != new.key or old.version != new.version or ed.parent_relative_path() != "":
            ed.set_parent(new, None)
        self._record_parent(c, agg, old, new)
        if self._mandatory_in_root(c):
            self._add_mandatory(c, ed)
        self._remove_unwanted(c, ed)
        self._fix_java(c, ed, top=False)
        c.save(ed)

    def _msa_placeholders(self, c: _Ctx, svc_entries: list[Any]) -> None:
        """서비스 루트마다 그 서비스 코드가 읽는 키(+ 어느 서비스에도 속하지 않는 공유 코드의 키)를
        src/main/resources 에 placeholder 로; src/main/java 가 있는 서비스만. 서비스별 인벤토리가 있으면
        서비스 이름으로, 없으면 참조 위치로 고른다.

        Each service root's src/main/resources gets the keys its own code reads (plus keys read only by shared
        code outside every service); directories are created only for services with src/main/java. A per-service
        inventory (round 9, `PropertyFinding.service`) selects by service name, else by reference location."""
        dirs: list[str] = []
        names: dict[str, list[str]] = {}
        for s in svc_entries:
            d = posixpath.dirname(posixpath.normpath(s.root_pom))
            d = "" if d in ("", ".") else d
            if d not in dirs:
                dirs.append(d)
            ids = names.setdefault(d, [])
            for n in (s.name, d, posixpath.basename(d)):
                if n and n not in ids:
                    ids.append(n)
        for d in dirs:
            res = posixpath.join(d, "src/main/resources") if d else "src/main/resources"
            if not c.ws.abs(res).is_dir() and not c.ws.abs(
                    posixpath.join(d, "src/main/java") if d else "src/main/java").is_dir():
                continue
            self._write_placeholders(c, res, under=d, others=tuple(o for o in dirs if o != d),
                                     service=tuple(names.get(d, ())))
        self._placeholder_followup(c)

    def _climb(self, c: _Ctx, pom: str) -> str:
        """저장소 안 <parent> 링크를 따라 올라간 최상위 pom.

        Follow in-repo <parent> links upwards; returns the top-most in-repo pom of the chain."""
        return self._chain(c, pom)[-1]

    def _chain(self, c: _Ctx, pom: str) -> list[str]:
        """[pom, 저장소 안 부모, ..., 최상위 저장소 안 조상]. 빈 <relativePath/>, 대상 pom 없음 / 프로젝트 밖,
        artifactId 불일치면 멈춘다(20 단계까지, 순환 방지).

        [pom, its in-repo parent, …, top-most in-repo ancestor]."""
        out = [pom]
        cur = pom
        for _ in range(20):
            try:
                ed = PomEditor(c.ws.abs(cur))
            except PomEditError:
                break
            parent = ed.get_parent()
            rp = ed.parent_relative_path()
            if parent is None or rp == "":
                break
            target = posixpath.normpath(posixpath.join(posixpath.dirname(cur), rp or "../pom.xml"))
            if not target.endswith(".xml"):
                target = posixpath.join(target, "pom.xml")
            if target.startswith("..") or not c.ws.exists(target) or target in out:
                break
            try:
                ped = PomEditor(c.ws.abs(target))
            except PomEditError:
                break
            if ped.artifact_id() != parent.artifact_id:
                break
            out.append(target)
            cur = target
        return out
