"""Company-layout restructuring (ARCHITECTURE §8).

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
from sbmig.core.workspace import GitError, Workspace
from sbmig.plan.versions import compare_versions, version_lt

from .pom_editor import PomEditError, PomEditor, property_ref
from .webxml_registrations import CLASS_NAME as WEB_XML_CLASS
from .webxml_registrations import RULES_FILE as WEB_XML_RULES
from .webxml_registrations import ClassInfo, load_skip_rules, plan_registrations

log = logging.getLogger("sbmig.restructure")

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
    pass


def render(template: str, **kw: Any) -> str:
    return _TEMPLATES.get_template(template).render(**kw)


def sanitize_identifier(name: str, fallback: str = "x") -> str:
    out = re.sub(r"[^A-Za-z0-9_$]", "_", name or "")
    if not out:
        out = fallback
    if out[0].isdigit():
        out = "_" + out
    if out in JAVA_KEYWORDS:
        out = out + "_"
    return out


def sanitize_package(pkg: str | None) -> str | None:
    if not pkg:
        return None
    parts = [sanitize_identifier(p) for p in pkg.strip().strip(".").split(".") if p]
    return ".".join(parts) or None


@dataclass
class _Ctx:
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

    def p(self, *parts: str) -> str:
        j = posixpath.normpath(posixpath.join(self.base, *parts)) if (self.base or parts) else "."
        return "." if j in ("", ".") else j

    def new_location(self, old: str) -> str:
        """Where an as-is path is now: the moves applied in order (a file moved twice — into comp, then into the
        front / boot module — resolves to its last location)."""
        cur = old
        for o, n in self.moves:
            if cur == o or cur.startswith(o + "/"):
                cur = n + cur[len(o):]
        return cur

    def move(self, src: str, dst: str) -> int:
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

    def write(self, rel: str, text: str, generated: bool = True) -> None:
        self.ws.write(rel, text)
        self.changed.add(rel)
        if generated and rel not in self.result.generated_files:
            self.result.generated_files.append(rel)

    def save(self, ed: PomEditor) -> bool:
        if not ed.changed:
            return False
        ed.save()
        self.changed.add(self.ws.rel(ed.path))
        return True

    def note(self, text: str) -> None:
        if text not in self.result.notes:
            self.result.notes.append(text)

    def followup(self, text: str) -> None:
        if text not in self.result.manual_followups:
            self.result.manual_followups.append(text)

    def commit(self, message: str) -> None:
        if not self.commit_enabled:
            return
        try:
            self.ws.commit(message)
        except (GitError, OSError) as e:  # pragma: no cover - git missing / misconfigured
            log.warning("restructure commit skipped: %s", e)


class Restructurer:
    """Implements RestructurerLike. `Restructurer(commit=False)` skips intermediate git commits."""

    def __init__(self, commit: bool = True):
        self.commit_enabled = commit

    # ================================================================== entry point
    def apply(self, ws: Workspace, profile: ProjectProfile, cfg: Config, *,
              mode: Literal["layout", "msa_pom", "optional_bump", "boot_pom"] = "layout",
              params: dict[str, Any] | None = None) -> RestructureResult:
        """params (optional, from the PlanStep): starter, split_policy, multi_module_policy, merge_collision (layout);
        library_roots (msa_pom); target (optional_bump: restrict to the poms below this directory)."""
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
        result.changed_files = sorted(c.changed)
        return result

    # ================================================================== layout
    def _layout(self, c: _Ctx) -> None:
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
                if want and parent.version != want:   # migrated on an older in-house parent → the configured one
                    root.set_parent(c.cfg.artifacts.parent.gav(), root.parent_relative_path())
                    c.save(root)
                    self._record_parent(c, root_rel, parent, c.cfg.artifacts.parent.gav())
                    c.note(f"in-house parent {parent.key} {parent.version or '?'} → {want} (artifacts.parent)")
            self._align_role_parents(c, report_modules=True)
            self._all_poms_pass(c, java_fix=False)
            self._drop_managed_versions(c)
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
        self._drop_managed_versions(c)
        c.commit("restructure: optional artifact versions / cleanup")

    # ------------------------------------------------------------------ project name (2026-10-23)
    def _project_name_coords(self, c: _Ctx, root: PomEditor, coords: GAV) -> GAV:
        """The intake aggregator's `sbmig.project-name` (it took `<name>-aggregator` because a member, typically the
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
        """After the root rename (`_rewrite_root`): every project pom whose `<parent>` still names the old root
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
        """JSPs of the analysis that did not move and lie in no generated module (e.g. `STATIC/index.jsp` next to the
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
                             and "target" not in f.relative_to(base).parts and c.ws.rel(f) not in names)
            n = len(jsps)
            counts = f"{n} JSP{'s' if n != 1 else ''}" + (
                f", {others} other file{'s' if others != 1 else ''}" if others is not None else "")
            c.followup(f"web content not referenced by any pom left in place: {where} ({counts}) — serve it as "
                       f"before (web server) or move it into {dest}")
        if "web-content-left" not in c.result.flags:
            c.result.flags.append("web-content-left")

    def _dedupe_webinf_stash(self, c: _Ctx) -> None:
        """An XML bean config that reached the boot module twice — `legacy-config/` (copied + `@ImportResource`) and
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
        g = root.effective_group_id() or (profile.root_pom.group_id if profile.root_pom else None)
        v = root.effective_version() or (profile.root_pom.version if profile.root_pom else None)
        a = root.artifact_id() or (profile.root_pom.artifact_id if profile.root_pom else None) or profile.name
        if not g or not v:
            raise RestructureError("root pom has no resolvable groupId/version")
        return GAV(group_id=g, artifact_id=a, version=v)

    # ------------------------------------------------------------------ single module
    def _single(self, c: _Ctx, root: PomEditor, coords: GAV) -> None:
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
        """Move every as-is module under module/ unchanged and add boot-jar depending on the **jar** modules.

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
        """Re-point `<parent><relativePath>` and nested `<modules>` of every pom below moved directories."""
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
        mn = posixpath.normpath(m)
        return (posixpath.dirname(mn) or ".") if mn.endswith(".xml") else mn

    def _merge(self, c: _Ctx, root: PomEditor, mods: list[str], coords: GAV) -> None:
        """Fold every as-is module (and the root `src/`) into comp-jar; an as-is module named like
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
        ears = [e for e in merged_jar if e[2].packaging() == "ear"]          # no sources merged (2026-10-23)
        merged_src = [e for e in merged_jar if e[2].packaging() != "ear"]
        module_dirs = {d.split("/")[0] for _, d, _ in [*flat, *pom_mods]}
        sources_of: list[tuple[str, str, list[str]]] = []    # (module dir, label, top-level source dirs)
        root_tops = [t for t in self._source_roots(c, root) if t == "src" or t not in module_dirs]
        if any(c.ws.exists(c.p(t)) for t in root_tops):
            sources_of.append((".", "<root>", root_tops))
        inherited = self._source_roots(c, root)            # root <build> resource dirs apply to every module
        for label, mdir, ed in merged_src:
            sources_of.append((mdir, label, list(dict.fromkeys([*self._source_roots(c, ed), *inherited]))))
        self._plan_outside_dirs(c, root, merged_src, sources_of, [*flat, *pom_mods])
        mains = [(label, found) for label, mdir, ed in merged_src if ed.packaging() == "jar"
                 for found in [self._module_mains(c, mdir, ed)] if found]
        c.module_mains.update(f for _, found in mains for f in found)

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
                        if UNION_RE.search(rel):
                            unions.setdefault(dst, [(first_label, first_src)]).append((label, src))
                            continue
                        if collision == "fail":
                            raise RestructureError(f"merge collision: {rel} exists in both {first_label} and {label}")
                        mc = conflicts.setdefault(dst, MergeConflict(path=rel, kept_from=first_label, identical=True))
                        mc.dropped_from.append(label)
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
                    c.ws.delete(f"{mdir}/pom.xml")          # the as-is module dir is the company module dir
                else:
                    c.ws.delete(mdir)
                    self._prune_empty_parents(c, mdir)
            if ed.packaging() != "ear":
                c.note(f"merged module {label} into {c.comp_rel} (module directory removed)")
        c.result.merged_modules = [label for label, _, ed in all_merged if ed.packaging() != "ear"]
        c.commit("restructure: merge as-is modules into comp-jar")

        # 5. boot-jar: the kept as-is module, or a generated one
        lib = GAV(group_id="${project.groupId}", artifact_id=coords.artifact_id + L.comp_artifact_suffix,
                  version="${project.version}")
        if kept_ed is not None and kept is not None and kept_dir is not None:
            parent = kept_ed.get_parent()
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
        """(merged_internal, internal): the g:a keys that name a merged module (merged_internal) or the kept boot
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
        """An `ear` module is not merged: Spring Boot builds no EAR. Its files other than pom.xml / target/ (the
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
        """(EAR label, web module, context root, explicit) of an EAR: maven-ear-plugin `<webModule><contextRoot>`
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
        """maven-ear-plugin never reaches a generated pom (there is no EAR module any more)."""
        if ed.find_plugins(*EAR_PLUGIN, where="all") and ed.remove_plugin(*EAR_PLUGIN):
            c.note(f"maven-ear-plugin dropped from {c.ws.rel(ed.path)} (the EAR module was not carried)")

    # ------------------------------------------------------------------ WAR overlays (2026-10-23)
    def _plan_overlays(self, c: _Ctx, sources: list[PomEditor], exclude: set[str]) -> None:
        """External `<type>war</type>` dependencies left after the internal exclusion are maven-war-plugin overlays:
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
        """The boot WAR's pom: the overlay dependencies (type war, scope as declared) and the as-is
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
        """Directories of one pom that climb out of its directory (`../…`, normalized): maven-war-plugin
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
        """The as-is pom's directory relative to the project root (c.base)."""
        d = posixpath.dirname(c.ws.rel(ed.path)) or "."
        if c.base:
            d = posixpath.relpath(d, c.base)
        return d

    def _comp_dir(self, c: _Ctx, ed: PomEditor, d: str | None) -> str | None:
        """A module-relative pom directory → comp-module-relative: unchanged for paths inside the module (the files
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
        """A merged module's directory that resolves outside the module but inside the project (`APP/WEB` →
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
        """FQCNs with `public static void main(String…)` in a non-Boot class below the module's main source dirs."""
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
        """After a module directory was removed: drop its now-empty parent directories inside the project."""
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
        tgt = c.p(mdir)
        return tgt == ".." or tgt.startswith("../")

    def _skip_outside(self, c: _Ctx, declared_in: str, module: str, target: str) -> None:
        c.note(f"module '{module}' declared in {declared_in} resolves outside the project root ({target}); "
               "not merged (nothing outside the workspace is read or changed)")
        c.followup(f"module {module} ({declared_in}) lives outside the project root and was not merged into "
                   f"{c.comp_rel}: migrate it separately and install it into "
                   "Nexus, or move it into the project — dependencies on it now resolve from the repository")

    def _fold_management(self, c: _Ctx, root: PomEditor, merged: list[tuple[str, str, PomEditor]],
                         internal: set[str], target: PomEditor | None = None) -> None:
        """Keep the merged modules' management: root `<dependencyManagement>` entries of merged modules (an internal
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
        """Kept boot module: comp-jar copies of boot resources with the same name as boot-jar's own are shadowed at
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
        """Union of line-list registries: `*.factories` (key=comma list, `\\` continuations) merged per key with
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
        """Adjust the kept as-is boot module's pom: dependencies on merged modules → one comp-jar dependency,
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
            if g.type == "test-jar" or g.classifier == "tests":
                test_jar = True
                ed.remove_node(el)
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
                if (g.type or "") == "war":            # a WAR overlay / web module is never a jar module dependency
                    continue
                if asis_packaging == "war" and g.key == f"{BOOT_GROUP}:spring-boot-starter-tomcat" \
                        and g.scope == "provided":
                    c.note(f"provided spring-boot-starter-tomcat dropped from {comp} (declared provided in the boot "
                           "WAR)" if c.boot_war else "provided spring-boot-starter-tomcat dropped (WAR → executable jar)")
                    continue
                seen_deps.add(ident)
                new_el = ed.copy_into(ed.ensure_path("dependencies"), d)
                self._relocate_system_dep(c, ed, new_el, asis_dir, comp, src.properties())
            build = src.find("build")
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
        """A `<build>` directory element copied from `src` into the comp pom (source directories, `<resources>` /
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
        """Merged modules (sources[0] = the root pom, its `<build>` inherited by every module): comp-jar's
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
        """Keep a `system`-scoped dependency's jar reachable from the module that now declares it: copy the jar to
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
        for el in [*ed.dependency_elements(False), *ed.dependency_elements(True)]:
            self._relocate_system_dep(c, ed, el, asis_dir, module_dir)

    # ------------------------------------------------------------------ boot-jar content
    def _populate_boot(self, c: _Ctx, src_dirs: list[str], boot: str) -> tuple[list[str], list[str]]:
        """Move boot resources, @SpringBootApplication / @SpringBootTest classes (+ web layer) into boot-jar."""
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
                is_web = split == "web-layer" and bool(WEB_LAYER_RE.search(text))
                if is_init or is_web:
                    rel = f.relative_to(rabs).as_posix()
                    c.move(f"{r}/{rel}", f"{boot}/src/main/java/{rel}")
                    if is_init:
                        c.note(f"SpringBootServletInitializer subclass {rel} moved to boot-jar")
        return moved_main, moved_tests

    def _boot_resource_files(self, c: _Ctx, res_dir: str) -> list[str]:
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
        """(boot_key, value, comment) for every `external` finding, deduped by key (first finding wins), sorted.
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
        label = PLACEHOLDER_SOURCE_LABELS.get(ref.source, ref.source)
        if ref.source == "jndi":
            return f"{label} {ref.key}"
        where = ref.file + (f":{ref.line}" if ref.line else "")
        if ref.source == "context-param":
            return f"{label} {ref.key} {where}"
        return f"{label} {where}"

    def _write_placeholders(self, c: _Ctx, resources_dir: str, *, under: str | None = None,
                            others: tuple[str, ...] = (), service: tuple[str, ...] | None = None) -> list[str]:
        """Append (or replace) the sbmig externalized-config block in `<resources_dir>/<placeholder_file>`.
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
        if not existed and X.placeholder_file == "application.properties" and \
                c.ws.exists(posixpath.join(resources_dir, "application.yml")):
            lines.append("# --- application.yml exists; placeholders go to application.properties (Boot loads both)")
        for k, v, cm in entries:
            lines += [cm, f"{k}={v}"]
        lines.append(PLACEHOLDER_END)
        block = nl.join(lines) + nl
        start = text.find(PLACEHOLDER_BEGIN)
        end = text.find(PLACEHOLDER_END, start) if start >= 0 else -1
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
        files = c.result.placeholder_files
        if not files:
            return
        n = len(c.result.placeholders_written)
        c.followup(f"{n} externalized key(s) have placeholder values ({c.cfg.externalized.sentinel}) in "
                   f"{', '.join(files)} — set them before running (see Handover 'Properties to set')")

    def _locate_class(self, c: _Ctx, fqcn: str, roots: list[str]) -> str | None:
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
        """Generated boot-jar pom. Role parents (round 9b): parent = `artifacts.boot_parent` (`<relativePath/>`),
        explicit groupId / version, `root`'s properties / management copied in, mandatory dependencies, BOM and
        java level in this pom."""
        L = c.cfg.layout
        A = c.cfg.artifacts
        case = c.profile.case_type
        case_key = case.value if isinstance(case, CaseType) else str(case)
        deps = list(lib_deps)
        starter = c.params.get("starter") or L.starter_by_case.get(case_key, "spring-boot-starter")
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
        """Simple names of test classes left in `comp_dir/src/test/java` referenced by boot-jar's test sources."""
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
        L = c.cfg.layout
        src = c.profile.source
        pkg = sanitize_package(src.base_package)
        if not pkg:
            pkg = FALLBACK_PACKAGE
            c.note(f"base package unknown; Application generated in {FALLBACK_PACKAGE}")
            c.profile.warnings.append(f"restructure: base package unknown, used {FALLBACK_PACKAGE}")
        cls = sanitize_identifier(L.application_class_name, "Application")
        pkg_path = pkg.replace(".", "/")
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
        """`layout.web_xml_registrations: generate`: the first web.xml's servlets / filters / listeners / error pages
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
        """The project source of `fqcn` below `dirs` (any source root; `target/` skipped), annotated / HttpServlet."""
        outer = fqcn.split("$", 1)[0]
        pkg, _, simple = outer.rpartition(".")
        for d in dirs:
            base = c.ws.abs(d)
            if not base.is_dir():
                continue
            for f in base.rglob(f"{simple}.java"):
                if "target" in f.relative_to(base).parts:
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
        existed = c.ws.exists(rel)
        text = c.ws.read(rel) if existed else ""
        nl = "\r\n" if "\r\n" in text else "\n"
        sep = "" if not text or text.endswith(("\n", "\r")) else nl
        c.write(rel, text + sep + (nl if text.strip() else "") + nl.join(lines) + nl, generated=not existed)

    def _packaging_followups(self, c: _Ctx, asis_packaging: str) -> None:
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
        """`{name}` = `layout.project_name`, else the root pom property `sbmig.project-name` (the intake aggregator's
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
        return [p.split("/")[0] for p in (c.boot_rel, c.comp_rel, c.front_rel) if p]

    @staticmethod
    def _layout_modules(c: _Ctx) -> list[str]:
        return [c.comp_rel, *([c.front_rel] if c.front_made and c.front_rel else []), c.boot_rel]

    @staticmethod
    def _front_lib(c: _Ctx, coords: GAV) -> list[GAV]:
        if not c.front_made:
            return []
        return [GAV(group_id="${project.groupId}", artifact_id=coords.artifact_id + FRONT_ARTIFACT_SUFFIX,
                    version="${project.version}")]

    @staticmethod
    def _mandatory_in_root(c: _Ctx) -> bool:
        return getattr(c.cfg.artifacts, "mandatory_target", "root") == "root"

    def _record_modules(self, c: _Ctx, existing: bool = False) -> None:
        """RestructureResult.boot_module / comp_module / front_module (the module dirs that exist) + boot_packaging."""
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
        """(web roots as (module-relative dir, web-path prefix), classpath dirs) of the as-is poms: the default
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
        """Move the web resources out of the comp module into `{name}-front` (only when there are any):
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
        meta_dst = f"{front}/{jsp_dir[:i + len('META-INF/resources')]}" if i >= 0 else static_dst
        stash = f"{boot}/src/main/resources/{WEBINF_STASH}"
        plan: list[tuple[str, str, str, str]] = []      # (src, dst, kind: static | jsp | webinf, web path)
        left: list[str] = []
        kept_web: list[str] = []                        # static-extension files kept as classpath resources (rule 3)
        kept_res = 0
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
                if any(x == "target" for x in rel.split("/")[:-1]):
                    continue
                web = posixpath.normpath(f"{target}/{rel}") if target else rel
                top, _, sub = web.partition("/")
                src = f"{webapp}/{rel}"
                if top == "WEB-INF" and sub:
                    if sub.lower().endswith(JSP_EXTS):
                        under = sub.removeprefix("jsp/")
                        plan.append((src, f"{jsp_dst}/{under}", "jsp", web))
                    elif sub.startswith("tags/") or sub.lower().endswith(".tld"):
                        plan.append((src, f"{meta_dst}/WEB-INF/{sub}", "jsp", web))
                    elif sub.startswith(("lib/", "classes/")):
                        left.append(src)
                    else:
                        plan.append((src, f"{stash}/{sub}", "webinf", web))
                elif top == "META-INF" and sub:
                    left.append(src)
                elif web.lower().endswith(JSP_EXTS):
                    plan.append((src, f"{meta_dst}/{web}", "jsp", web))
                elif any(f"{wdir}/{rel}".startswith(k + "/") for k in inner):
                    kept_res += 1
                    if posixpath.splitext(rel)[1].lstrip(".").lower() in WEB_STATIC_EXTENSIONS:
                        kept_web.append(src)
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
        if not any(k in ("static", "jsp") for _, _, k, _ in plan):
            return                                     # no web resources: no front module
        taken: set[str] = set()
        moved = {"static": 0, "jsp": 0, "webinf": 0}
        clash: list[str] = []
        jsp_relocated: list[str] = []
        for src, dst, kind, web in plan:
            if dst in taken or c.ws.exists(dst):
                clash.append(src)
                continue
            taken.add(dst)
            if kind == "jsp" and dst.startswith(jsp_dst + "/"):
                text = c.ws.abs(src).read_text(encoding="utf-8", errors="replace")
                c.jstl = c.jstl or bool(JSTL_URI_RE.search(text))
                web_old = "/" + web
                if not web_old.startswith("/WEB-INF/jsp/"):
                    jsp_relocated.append(web_old)
            elif kind == "jsp" and src.lower().endswith(JSP_EXTS):
                c.jstl = c.jstl or bool(JSTL_URI_RE.search(c.ws.abs(src).read_text(encoding="utf-8",
                                                                                    errors="replace")))
            c.move(src, dst)
            moved[kind] += 1
        for d in (*web_dirs, *(f"{comp}/src/main/resources/{x}" for x in WEB_RESOURCE_DIRS)):
            self._prune_empty_dirs(c, d)
        c.front_made = True
        c.jsp_moved = sum(1 for s_, d, k, _ in plan if k == "jsp" and s_.lower().endswith(JSP_EXTS) and d in taken)
        c.result.front_files = moved["static"] + moved["jsp"]
        c.boot_war = c.boot_war or (c.jsp_moved > 0 and L.jsp_boot_packaging == "war")   # a WAR overlay may set it
        self._write_front_pom(c, coords, front)
        custom = [d for d, _ in roots
                  if d != DEFAULT_WEBAPP and any(s_.startswith(f"{comp}/{d}/") for s_, _, _, _ in plan)]
        c.note(f"front module {front}: {moved['static']} static web file(s) → {L.front_static_dir}, "
               f"{moved['jsp']} JSP file(s) → {L.front_jsp_dir} (META-INF/resources of the front jar)"
               + (f"; {moved['webinf']} other WEB-INF file(s) (web.xml, XML configs) → {stash}" if moved["webinf"]
                  else "")
               + (f"; web root(s) from maven-war-plugin: {', '.join(custom)}" if custom else ""))
        if kept_res:
            c.note(f"{kept_res} file(s) under a <resource> / source directory inside a web root stay in {comp} as "
                   "classpath resources (the copied <resources> entries still point at them)")
        if kept_web:
            c.followup(f"{len(kept_web)} static web file(s) under a <resource> directory inside the web root stay "
                       f"classpath resources in {comp} ({', '.join(kept_web[:3])}"
                       f"{' …' if len(kept_web) > 3 else ''}); "
                       f"the as-is WAR also served them as web content — copy them into {static_dst} if pages link "
                       "them")
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
        """A generated / rewritten pom must not carry the as-is maven-war-plugin `warSourceDirectory` /
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
        src = c.profile.source
        if not src.large_media_files:
            return
        mb = src.large_media_bytes / (1024 * 1024)
        c.note(f"{len(src.large_media_files)} large media file(s) ({mb:.0f} MB) are packaged into the front / comp "
               f"jar: consider moving them to a file server ({', '.join(src.large_media_files[:3])}"
               f"{' …' if len(src.large_media_files) > 3 else ''})")

    @staticmethod
    def _prune_empty_dirs(c: _Ctx, rel: str) -> None:
        top = c.ws.abs(rel)
        if not top.is_dir():
            return
        for d in sorted((x for x in top.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
        if not any(top.iterdir()):
            top.rmdir()

    def _write_front_pom(self, c: _Ctx, coords: GAV, front: str) -> None:
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
        """The boot WAR's container dependencies: Tomcat + Jasper `provided` (external Tomcat), JSTL when a JSP uses
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
        """The generated Application carries @ServletComponentScan (servlet case or @WebServlet / filter classes)."""
        src = c.profile.source
        return c.cfg.layout.add_servlet_component_scan and (
            c.profile.case_type == CaseType.SERVLET_TOMCAT or bool(src.servlet_classes)
            or bool(src.filter_listener_classes))

    def _initializer_followup(self, c: _Ctx, boot: str, mains: list[str]) -> None:
        root = c.ws.abs(f"{boot}/src/main/java")
        if root.is_dir() and any(SERVLET_INIT_RE.search(f.read_text(encoding="utf-8", errors="replace"))
                                 for f in root.rglob("*.java")):
            return
        c.followup(f"{c.boot_rel} is a WAR for external Tomcat but {', '.join(mains)} does not extend "
                   "SpringBootServletInitializer — extend it and override configure(SpringApplicationBuilder)")

    def _view_resolver(self, c: _Ctx) -> tuple[str | None, str | None, str | None]:
        """(prefix, suffix, file) of an as-is InternalResourceViewResolver / `<mvc:jsp>` / `registry.jsp(…)`."""
        base = c.ws.abs(c.p("."))
        for f in sorted(base.rglob("*")):
            if not f.is_file() or f.suffix not in (".xml", ".java") or ".git" in f.parts or "target" in f.parts:
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
        """As-is web path of a JSP location → its path after the move (see `_populate_front`)."""
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
        """`spring.mvc.view.prefix/suffix` into the boot `application.properties` when the as-is configuration names
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
        """`aggregator` (role parents, round 9b): the modules do not inherit the root pom — no mandatory
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
        """Aggregator root: drop the properties nothing left in the root pom references (they were copied into the
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
        return bool(c.cfg.artifacts.role_parents)

    @staticmethod
    def _gav_str(g: GAV | None) -> str:
        return "-" if g is None else f"{g.group_id}:{g.artifact_id}:{g.version or '?'}"

    def _record_parent(self, c: _Ctx, pom: str, old: GAV | None, new: GAV) -> None:
        """`RestructureResult.parents_set`: one entry per parent set or replaced (unchanged parents are skipped)."""
        if old is not None and old.key == new.key and (old.version or None) == (new.version or None):
            return
        entry = f"{pom}: {self._gav_str(old)} → {self._gav_str(new)}"
        if entry not in c.result.parents_set:
            c.result.parents_set.append(entry)

    def _role_module(self, c: _Ctx, ed: PomEditor, root: PomEditor | None, *, exclude: set[str],
                     all_props: bool = False) -> None:
        """A module pom on an external role parent gets what it used to inherit from the root pom: the root's
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
        """Copy the management `ed` inherited from `root` into `ed` (see `_role_module`): dependencyManagement
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
        """Boot as-is / MSA pom-only (round 9c): a module that stops inheriting an in-repo pom gets that pom's
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
        """The configured role parent an as-is `<parent>` with the same groupId:artifactId maps to (None if none).
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
        """R2: every project pom (root excluded) whose `<parent>` has the groupId:artifactId of `boot_parent` /
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
        """`scope: import` (+ `type: pom`) = a BOM-style mandatory artifact: it belongs in <dependencyManagement>,
        never in <dependencies> (Maven rejects import scope there)."""
        return (getattr(spec, "scope", None) or "").strip().lower() == "import"

    def _mandatory_align_existing(self, c: _Ctx, ed: PomEditor, spec, managed: bool, where: str) -> None:
        """An existing declaration of a mandatory artifact takes the configured version (None → managed by the
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
        """A pom that keeps its own declaration of a mandatory artifact (declared in the root, `mandatory_target:
        root`) gets the configured version (None → managed by the parent) and type / scope."""
        for spec in c.cfg.artifacts.mandatory:
            self._mandatory_align_existing(c, ed, spec, self._mandatory_managed(spec), "declared in")

    def _remove_unwanted(self, c: _Ctx, ed: PomEditor) -> None:
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
        A = c.cfg.artifacts
        bom_aid = "spring-boot-dependencies"
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
        """Root pom first, then every other pom of the project (poms under src/ are test data, skipped)."""
        root_rel = c.p("pom.xml")
        poms = [p for p in c.ws.pom_files()
                if "/src/" not in f"/{p}" and (not c.base or p.startswith(c.base + "/")) and p != root_rel]
        return ([root_rel] if c.ws.exists(root_rel) else []) + poms

    def _all_poms_pass(self, c: _Ctx, *, java_fix: bool, boot_plugin_fix: bool = False,
                       only: set[str] | None = None) -> None:
        """remove_when_present (+ java / boot plugin fixes) and optional bumps on every project pom, or only on the
        poms in `only` (property owners may still be any project pom)."""
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
        self._bump_optionals(c, eds, editable=only)
        for ed in eds.values():
            c.save(ed)

    # ================================================================== optional bump only (spring phase)
    def _optional_bump(self, c: _Ctx) -> None:
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
        manages = c.cfg.artifacts.parent_manages_spring_boot
        for p in ed.find_plugins(*BOOT_PLUGIN, where="all"):
            v = ed.text_of(p, "version")
            if manages and v:
                ed.set_child_text(p, "version", None)
            elif not manages and v and v != c.cfg.target.spring_boot_version and not property_ref(v):
                ed.set_child_text(p, "version", c.cfg.target.spring_boot_version)

    def _bump_optionals(self, c: _Ctx, eds: dict[str, PomEditor], editable: set[str] | None = None) -> None:
        """Shared by every mode. `editable` restricts which poms' references are touched (None = all); property
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

    # ================================================================== managed-version cleanup (round 9)
    def _drop_managed_versions(self, c: _Ctx) -> None:
        """`layout.drop_managed_versions`: drop explicit `<dependencies>` versions the new parent chain manages.

        `params["effective_pom"]` is a zero-arg callable returning the effective dependency management of the tree
        *as it is now* (shape of `sbmig.build.effective.load_effective`) or None (Maven unavailable). The managed
        version of the declaring module (`by_module`, matched by g:a then artifactId) is used when that module is in
        the capture, else the merged `managed` map. gte: drop only when managed ≥ explicit (unknown comparison →
        keep); any: always. Never touched: parent / mandatory / optional / remove_when_present artifacts, the
        project's own modules, system scope, non-jar types or classifiers (managed per type:classifier),
        dependencyManagement entries, plugins. Unresolvable `${…}` versions are kept; properties stay."""
        mode = c.cfg.layout.drop_managed_versions
        if mode == "off":
            return
        fn = c.params.get("effective_pom")
        data: Any = None
        if callable(fn):
            try:
                data = fn()
            except Exception as e:  # noqa: BLE001 - a broken capture must never fail the restructure
                log.warning("effective pom for the managed-version cleanup failed: %s: %s", type(e).__name__, e)
                data = None
        if not isinstance(data, dict) or not (data.get("managed") or data.get("by_module")):
            c.note("managed-version cleanup skipped: no effective pom")
            return
        A = c.cfg.artifacts
        protected = ({A.parent.key} | {r.key for r in (A.boot_parent, A.comp_parent) if r is not None}
                     | {s.key for s in A.mandatory} | set(c.cfg.optional_keys())
                     | {k for k in A.remove_when_present if ":" in k})
        merged_managed: dict[str, str] = data.get("managed") or {}
        by_module: dict[str, Any] = data.get("by_module") or {}
        eds: dict[str, PomEditor] = {}
        for p in self._project_poms(c):
            try:
                eds[p] = PomEditor(c.ws.abs(p))
            except PomEditError as e:
                c.note(f"managed-version cleanup: skipped unparsable pom {p}: {e}")
        own: set[str] = set()
        for ed in eds.values():
            a = ed.artifact_id()
            if a:
                own |= {f"{ed.effective_group_id()}:{a}", f"${{project.groupId}}:{a}"}
        dropped: list[str] = []
        kept_newer: list[str] = []
        for p, ed in eds.items():
            aid = ed.artifact_id() or ""
            mod = by_module.get(f"{ed.effective_group_id()}:{aid}")
            if mod is None:
                mod = next((v for k, v in by_module.items() if aid and k.split(":")[-1] == aid), None)
            managed = (mod.get("managed") or {}) if isinstance(mod, dict) else merged_managed
            props: dict[str, str] = {}
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
                explicit = self._resolve_version(ed, g.version, props)
                m = managed.get(g.key)
                if explicit is None or not m:
                    continue
                if mode == "gte":
                    cmp = compare_versions(m, explicit)
                    if cmp is None:
                        continue
                    if cmp < 0:
                        kept_newer.append(f"{g.key} {explicit} (managed {m}, {p})")
                        continue
                ed.remove_version(el)
                dropped.append(f"{g.key}: {explicit} → managed {m} ({p})")
        for ed in eds.values():
            c.save(ed)
        c.result.managed_versions_dropped.extend(d for d in dropped if d not in c.result.managed_versions_dropped)
        if dropped:
            c.note(f"{len(dropped)} explicit dependency version(s) dropped: managed by the new parent chain "
                   f"(layout.drop_managed_versions={mode}): {', '.join(d.split(': ')[0] for d in dropped[:5])}"
                   f"{' …' if len(dropped) > 5 else ''}")
        if kept_newer:
            c.note(f"explicit version(s) newer than the parent-managed one kept: {', '.join(kept_newer[:5])}"
                   f"{' …' if len(kept_newer) > 5 else ''}")

    @staticmethod
    def _resolve_version(ed: PomEditor, version: str, props: dict[str, str]) -> str | None:
        """Literal version, or `${prop}` resolved from the pom chain's properties (nested up to 5 levels); None when
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
        """`boot_pom`: a Spring Boot as-is project keeps its directories; only the poms change. Root with
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
        self._drop_managed_versions(c)
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
        """Root-relative dirs of the boot modules: `profile.boot_modules` (analyzer: naming rule + pom), else
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
        """Single-module Boot app: `src/**` (+ custom source roots, `lib/`) → `{name}-boot/`, whose pom is the as-is
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
        """Parents / coordinates / mandatory / BOM / java of every module pom and the root (see `_boot_pom`).
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
        self._drop_managed_versions(c)
        self._msa_placeholders(c, svc_entries)
        c.commit("restructure: msa pom-only parent/mandatory/optional")

    def _msa_aggregator(self, c: _Ctx, agg: str) -> None:
        """Round 9c (role parents): the MSA top aggregator → `artifacts.parent` (explicit coordinates,
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
        """Each service root's src/main/resources gets the keys its own code reads (plus keys read only by shared
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
        """Follow in-repo <parent> links upwards; returns the top-most in-repo pom of the chain."""
        return self._chain(c, pom)[-1]

    def _chain(self, c: _Ctx, pom: str) -> list[str]:
        """[pom, its in-repo parent, …, top-most in-repo ancestor]."""
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
