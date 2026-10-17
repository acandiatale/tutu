"""Company-layout restructuring (ARCHITECTURE §8).

mode="layout"   non-MSA: root becomes an aggregator (parent = in-house parent, packaging pom, mandatory deps) with
                module/<comp-jar> (all as-is sources + deps) and module/<boot-jar> (Application, boot resources,
                spring-boot-maven-plugin repackage). As-is multi-module projects are wrapped (or merged).
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
`<module>` entries outside the project root abort the layout with ok=False before anything moves (S5); nested
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

from sbmig.core.config import Config
from sbmig.core.interfaces import RestructureResult
from sbmig.core.models import GAV, CaseType, PluginInfo, ProjectProfile
from sbmig.core.workspace import GitError, Workspace

from .pom_editor import PomEditError, PomEditor, property_ref
from sbmig.plan.versions import version_lt

log = logging.getLogger("sbmig.restructure")

BOOT_GROUP = "org.springframework.boot"
BOOT_PLUGIN = (BOOT_GROUP, "spring-boot-maven-plugin")
WAR_PLUGIN = ("org.apache.maven.plugins", "maven-war-plugin")
COMPILER_PLUGIN = ("org.apache.maven.plugins", "maven-compiler-plugin")
JAR_PLUGIN = ("org.apache.maven.plugins", "maven-jar-plugin")
JAVA_PROPS = ("maven.compiler.source", "maven.compiler.target", "maven.compiler.release", "java.version")
SERVLET_COMPONENT_SCAN_FQCN = "org.springframework.boot.web.server.servlet.context.ServletComponentScan"  # verified in spring-boot-web-server-4.1.0.jar
FALLBACK_PACKAGE = "com.company.app"
BUILD_TO_COMP = ("sourceDirectory", "scriptSourceDirectory", "testSourceDirectory", "resources", "testResources",
                 "filters")

WEB_LAYER_RE = re.compile(
    r"^\s*@(?:org\.springframework\.\S+\.)?(?:Controller|RestController|ControllerAdvice|RestControllerAdvice|"
    r"Configuration)\b", re.MULTILINE)
SERVLET_INIT_RE = re.compile(r"\bextends\s+(?:[\w.]+\.)?SpringBootServletInitializer\b")
PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)

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

    def p(self, *parts: str) -> str:
        j = posixpath.normpath(posixpath.join(self.base, *parts)) if (self.base or parts) else "."
        return "." if j in ("", ".") else j

    def new_location(self, old: str) -> str:
        for o, n in reversed(self.moves):
            if old == o or old.startswith(o + "/"):
                return n + old[len(o):]
        return old

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
              mode: Literal["layout", "msa_pom", "optional_bump"] = "layout",
              params: dict[str, Any] | None = None) -> RestructureResult:
        """params (optional, from the PlanStep): starter, split_policy, multi_module_policy (layout);
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
        comp_mod = f"{L.module_dir}/{L.comp_module}"
        boot_mod = f"{L.module_dir}/{L.boot_module}"
        mods = [m.strip().rstrip("/") for m in root.modules()]
        if root.packaging() == "pom" and boot_mod in mods and (
                comp_mod in mods or all(posixpath.normpath(m).startswith(L.module_dir + "/") for m in mods)):
            c.note("already restructured")
            parent = root.get_parent()
            if parent is None or parent.key != c.cfg.artifacts.parent.key:
                c.note(f"already restructured but {root_rel} does not inherit {c.cfg.artifacts.parent.key}; "
                       "parent left unchanged")
            self._all_poms_pass(c, java_fix=False)
            c.commit("restructure: optional artifact versions / cleanup (already restructured)")
            return
        parent = root.get_parent()
        if parent is not None and parent.key == c.cfg.artifacts.parent.key:
            c.note(f"{root_rel} already inherits the in-house parent {parent.key}; splitting into the company layout")
        outside = []
        for m in mods:
            tgt = posixpath.normpath(posixpath.join(c.base or ".", m))
            if m.startswith("/") or tgt == ".." or tgt.startswith("../"):
                outside.append(f"'{m}' → {tgt}")
        if outside:
            msg = (f"module(s) declared in {root_rel} resolve outside the project root: {', '.join(outside)}; the "
                   "layout restructure cannot move them — nothing was changed (move the module into the project or "
                   "migrate it separately)")
            c.note(msg)
            raise RestructureError(msg)
        coords = self._root_coords(root, c.profile)
        policy = c.params.get("multi_module_policy", L.multi_module_policy)
        if mods:
            if policy == "merge":
                self._merge(c, root, mods, coords)
            else:
                self._wrap(c, root, mods, coords)
        else:
            self._single(c, root, coords)
        self._all_poms_pass(c, java_fix=False)
        c.commit("restructure: optional artifact versions / cleanup")

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
        comp = c.p(L.module_dir, L.comp_module)
        boot = c.p(L.module_dir, L.boot_module)
        asis_packaging = root.packaging()
        asis_deps = root.dependencies()
        final_name = root.text_of(root.find("build"), "finalName") if root.find("build") is not None else None
        for d in self._source_roots(c, root):
            if c.ws.exists(c.p(d)):
                c.move(c.p(d), f"{comp}/{d}")
        c.commit("restructure: move as-is sources to comp-jar")
        self._write_comp_pom(c, [root], coords, comp, asis_packaging)
        moved_main, moved_tests = self._populate_boot(c, [comp], boot)
        lib = GAV(group_id="${project.groupId}", artifact_id=coords.artifact_id + L.comp_artifact_suffix,
                  version="${project.version}")
        self._write_boot(c, coords, boot, [lib], asis_deps, moved_main, moved_tests, final_name, [comp])
        self._rewrite_root(c, root, [f"{L.module_dir}/{L.comp_module}", f"{L.module_dir}/{L.boot_module}"],
                           keep_deps=False, keep_build_plugins=False)
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
        for pth in paths:
            pth = re.sub(r"^\$\{(?:project\.)?basedir\}/?", "", pth).strip().strip("/")
            if not pth or "${" in pth or pth.startswith(("..", "/")) or ":" in pth:
                continue
            top = pth.split("/")[0]
            if top not in roots and top not in ("target", c.cfg.layout.module_dir, "."):
                roots.append(top)
        return roots

    # ------------------------------------------------------------------ multi-module: wrap
    def _wrap(self, c: _Ctx, root: PomEditor, mods: list[str], coords: GAV) -> None:
        """Move every as-is module under module/ unchanged and add boot-jar depending on the **jar** modules.

        WAR modules keep building as WARs (S7): no class is taken out of them and boot-jar does not depend on them.
        Nested `<modules>` and `<parent><relativePath>` of moved poms are recomputed (S5)."""
        L = c.cfg.layout
        md = L.module_dir
        boot = c.p(md, L.boot_module)
        mapping: dict[str, str] = {}
        for m in mods:
            mn = posixpath.normpath(m)
            if mn.endswith(".xml"):
                mn = posixpath.dirname(mn) or "."
            if mn.startswith(md + "/") or not c.ws.exists(c.p(mn)):
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
                           f"{md}/{L.boot_module}); embedded deployment needs conversion (move its web layer to a jar "
                           "module or convert it to the boot-jar)")
            elif ed.remove_plugin(*BOOT_PLUGIN):
                c.note(f"spring-boot-maven-plugin removed from {new} (repackage happens in {L.boot_module})")
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
                         [c.p(d) for d in main_dirs])
        new_modules = [mapping[m] for m in mods] + [f"{md}/{L.boot_module}"]
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
    def _merge(self, c: _Ctx, root: PomEditor, mods: list[str], coords: GAV) -> None:
        L = c.cfg.layout
        comp = c.p(L.module_dir, L.comp_module)
        boot = c.p(L.module_dir, L.boot_module)
        eds: list[PomEditor] = []
        plan: list[tuple[str, str]] = []
        seen: dict[str, str] = {}
        sources_of = [(c.p(m), m) for m in mods]
        if c.ws.exists(c.p("src")):
            sources_of.insert(0, (c.p("."), "<root>"))
        for mdir, label in sources_of:
            if label != "<root>":
                pom = c.p(mdir, "pom.xml")
                if not c.ws.exists(pom):
                    raise RestructureError(f"merge: module pom {pom} not found")
                ed = PomEditor(c.ws.abs(pom))
                if ed.packaging() == "pom" or ed.modules():
                    raise RestructureError(f"merge policy does not support nested aggregator {label}")
                eds.append(ed)
            src_abs = c.ws.abs(c.p(mdir, "src"))
            if not src_abs.exists():
                continue
            for f in sorted(x for x in src_abs.rglob("*") if x.is_file()):
                rel = f.relative_to(src_abs).as_posix()
                dst = f"{comp}/src/{rel}"
                if dst in seen:
                    raise RestructureError(f"merge collision: src/{rel} exists in both {seen[dst]} and {label}")
                seen[dst] = label
                plan.append((c.p(mdir, "src", rel), dst))
        for src, dst in plan:
            c.move(src, dst)
        for mdir, label in sources_of:
            c.moves.append((c.p(mdir, "src"), f"{comp}/src"))
        module_aids = {ed.artifact_id() for ed in eds}
        groups = {coords.group_id, "${project.groupId}"} | {ed.group_id() for ed in eds if ed.group_id()}
        internal = {f"{g}:{a}" for g in groups for a in module_aids if a}
        packaging = "war" if any(ed.packaging() == "war" for ed in eds) else root.packaging()
        asis_deps = root.dependencies() + [d for ed in eds for d in ed.dependencies()]
        self._write_comp_pom(c, [root, *eds], coords, comp, packaging, exclude=internal, copy_props_from=eds)
        for m in mods:
            c.ws.delete(c.p(m))
            c.note(f"merged module {m} into {L.module_dir}/{L.comp_module} (module directory removed)")
        c.commit("restructure: merge as-is modules into comp-jar")
        moved_main, moved_tests = self._populate_boot(c, [comp], boot)
        lib = GAV(group_id="${project.groupId}", artifact_id=coords.artifact_id + L.comp_artifact_suffix,
                  version="${project.version}")
        final_name = root.text_of(root.find("build"), "finalName") if root.find("build") is not None else None
        self._write_boot(c, coords, boot, [lib], asis_deps, moved_main, moved_tests, final_name, [comp])
        self._rewrite_root(c, root, [f"{L.module_dir}/{L.comp_module}", f"{L.module_dir}/{L.boot_module}"],
                           keep_deps=False, keep_build_plugins=False)
        self._packaging_followups(c, packaging)
        c.commit("restructure: comp-jar / boot-jar layout (merged)")

    # ------------------------------------------------------------------ comp pom
    def _write_comp_pom(self, c: _Ctx, sources: list[PomEditor], coords: GAV, comp: str, asis_packaging: str,
                        exclude: set[str] | None = None, copy_props_from: list[PomEditor] | None = None) -> None:
        L = c.cfg.layout
        A = c.cfg.artifacts
        path = f"{comp}/pom.xml"
        root_pom = c.p("pom.xml")
        rel_parent = posixpath.relpath(root_pom, comp)
        c.write(path, render("comp-pom.xml.j2", parent=coords, relative_path=rel_parent,
                             artifact_id=coords.artifact_id + L.comp_artifact_suffix, name=None))
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
                if asis_packaging == "war" and g.key == f"{BOOT_GROUP}:spring-boot-starter-tomcat" \
                        and g.scope == "provided":
                    c.note("provided spring-boot-starter-tomcat dropped (WAR → executable jar)")
                    continue
                seen_deps.add(ident)
                new_el = ed.copy_into(ed.ensure_path("dependencies"), d)
                self._relocate_system_dep(c, ed, new_el, asis_dir, comp, src.properties())
            build = src.find("build")
            if build is None:
                continue
            for name in BUILD_TO_COMP:
                el = src.child(build, name)
                if el is not None and ed.find(f"build/{name}") is None:
                    ed.copy_into(ed.ensure_path("build"), el, ordered=True)
            for p in src.plugin_elements("build"):
                key = src.plugin_key(p)
                if key in (":".join(BOOT_PLUGIN), ":".join(WAR_PLUGIN)) or key in seen_plugins or key in drop:
                    continue
                seen_plugins.add(key)
                ed.copy_into(ed.ensure_path("build/plugins"), p)
        root_props = sources[0].properties() if sources else {}
        for src in copy_props_from or ():
            for k, v in src.properties().items():
                if k not in root_props and not ed.has_property(k):
                    ed.set_property(k, v)
        self._fix_java(c, ed, top=False)
        ed.save()

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
            c.followup(f"@SpringBootTest tests moved to {L.module_dir}/{L.boot_module} ({len(moved_tests)}); test "
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
                    comp_dirs: list[str]) -> None:
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
        if src.jsp_count > 0:
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
                       f"{L.boot_module} and maven-jar-plugin test-jar goal added to {comp_dir}")
        plugin_version = None if A.parent_manages_spring_boot else c.cfg.target.spring_boot_version
        main_class = moved_main[0] if len(moved_main) > 1 else None
        if main_class:
            c.note(f"several @SpringBootApplication classes; repackage mainClass set to {main_class}")
        rel_parent = posixpath.relpath(c.p("pom.xml"), boot)
        c.write(f"{boot}/pom.xml", render(
            "boot-pom.xml.j2", parent=coords, relative_path=rel_parent,
            artifact_id=coords.artifact_id + L.boot_artifact_suffix, name=None, dependencies=deps,
            plugin_version=plugin_version, main_class=main_class, final_name=final_name))
        if not moved_main:
            self._generate_application(c, boot, comp_dirs)
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
        text = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)
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
        case = c.profile.case_type
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
        servlet = L.add_servlet_component_scan and (
            case == CaseType.SERVLET_TOMCAT or bool(src.servlet_classes) or bool(src.filter_listener_classes))
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
                      servlet_component_scan_fqcn=SERVLET_COMPONENT_SCAN_FQCN, import_resources=imports,
                      case_type=c.profile.case_type.value if isinstance(c.profile.case_type, CaseType)
                      else str(c.profile.case_type))
        c.write(f"{boot}/src/main/java/{pkg_path}/{cls}.java", text)
        if src.psvm_classes:
            c.followup("existing main class(es) " + ", ".join(src.psvm_classes) + " kept in comp-jar; they are "
                       "not invoked by the generated Spring Boot Application")

    def _web_followups(self, c: _Ctx) -> None:
        src = c.profile.source
        scan = c.cfg.layout.add_servlet_component_scan
        for p in src.web_xml_paths or ([] if not src.has_web_xml else ["src/main/webapp/WEB-INF/web.xml"]):
            loc = c.new_location(p)
            if scan:
                c.followup(f"web.xml at {loc} is not read by embedded Tomcat; servlets/filters are registered via "
                           "@ServletComponentScan — verify mappings")
            else:
                c.followup(f"web.xml at {loc} is not read by embedded Tomcat — register servlets/filters as beans")

    def _packaging_followups(self, c: _Ctx, asis_packaging: str) -> None:
        if asis_packaging == "war":
            c.followup("WAR packaging converted to executable jar "
                       f"({c.cfg.layout.module_dir}/{c.cfg.layout.boot_module})")

    # ------------------------------------------------------------------ root pom
    def _rewrite_root(self, c: _Ctx, root: PomEditor, modules: list[str], *, keep_deps: bool,
                      keep_build_plugins: bool) -> None:
        A = c.cfg.artifacts
        g, v = root.effective_group_id(), root.effective_version()
        if not root.group_id() and g:
            root.set_coordinate("groupId", g)
        if not root.version() and v:
            root.set_coordinate("version", v)
        root.set_parent(A.parent.gav(), None)
        root.set_packaging("pom")
        root.set_modules(modules)
        if not keep_deps:
            root.remove_all_dependencies()
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
        root.remove_if_empty(root.find("build/plugins"))
        root.remove_if_empty(root.find("build"))
        c.save(root)

    def _add_mandatory(self, c: _Ctx, ed: PomEditor) -> None:
        for spec in c.cfg.artifacts.mandatory:
            existing = [d for d in ed.dependencies() if d.key == spec.key]
            if ed.add_dependency(spec.gav(), if_absent=True):
                if spec.key not in c.result.mandatory_added:
                    c.result.mandatory_added.append(spec.key)
            elif existing and existing[0].version != spec.version:
                for e in ed.dependency_elements(False):
                    if ed.gav_of(e).key == spec.key:
                        ed.set_ref_version(e, spec.version)      # None → managed by the parent
                c.note(f"mandatory {spec.key} already present in {c.ws.rel(ed.path)}; version aligned")

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
        resolved: list[str] = []
        for t in targets:
            if not c.ws.exists(t):
                c.note(f"service pom {t} not found; skipped")
                continue
            top = self._climb(c, t)
            if top != t:
                c.note(f"{t} inherits from in-repo {top}; parent set on {top}")
            if top not in resolved:
                resolved.append(top)
        lib_resolved: list[str] = []
        for t in lib_poms:
            if not c.ws.exists(t):
                c.note(f"library root pom {t} not found; skipped")
                continue
            top = self._climb(c, t)
            if top != t:
                c.note(f"library {t} inherits from in-repo {top}; parent set on {top}")
            if top not in resolved and top not in lib_resolved:
                lib_resolved.append(top)
        if not resolved and not lib_resolved:
            raise RestructureError("msa_pom: no service poms found")
        for t in lib_resolved:
            ed = PomEditor(c.ws.abs(t))
            old_parent = ed.get_parent()
            if old_parent is not None:
                if not ed.group_id() and old_parent.group_id:
                    ed.set_coordinate("groupId", old_parent.group_id)
                if not ed.version() and old_parent.version:
                    ed.set_coordinate("version", old_parent.version)
            ed.set_parent(A.parent.gav(), None)
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
            ed.set_parent(A.parent.gav(), None)
            self._add_mandatory(c, ed)
            self._fix_java(c, ed, top=True)
            self._boot_bom(c, ed)
            c.save(ed)
            c.note(f"msa_pom: patched {t}")
        self._all_poms_pass(c, java_fix=True, boot_plugin_fix=True)
        c.commit("restructure: msa pom-only parent/mandatory/optional")

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
