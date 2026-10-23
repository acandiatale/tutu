"""Deterministic pom handlers (version floors, relocations, jakarta swaps …).

Every handler works reactively (`matches(cluster, …)` + `apply(ws, cluster, …)`) and pre-emptively
(`apply(ws, None, profile, cfg)`, driven by `sbmig.plan.pre_handlers_for`). Pom edits go through a PomEditor
obtained from `registry.pom_editor_factory`; a few edits the PomEditorLike protocol cannot express
(annotationProcessorPaths versions, compiler <source>/<target>, loaderImplementation) are done as small,
anchored text substitutions after the editor saved. `apply` returns one `FileChange(op="modify")` per changed pom
(bookkeeping only — the orchestrator commits).
"""
from __future__ import annotations

import logging
import re
from abc import abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from sbmig.core.config import Config
from sbmig.core.models import GAV, ErrorCluster, ErrorKind, FileChange, Phase, PluginInfo, ProjectProfile
from sbmig.core.workspace import Workspace

from .base import (Handler, _default_pom_editor_factory, all_poms, cluster_artifact_keys, cluster_text,
                   parse_version, poms_for_cluster, profile_declares, root_pom, version_lt)

log = logging.getLogger("sbmig.triage.handlers")

_DATA_DIR = Path(__file__).parent
DEFAULT_PLUGIN_GROUP = "org.apache.maven.plugins"

LOMBOK_VERSION = "1.18.38"
MAPSTRUCT_VERSION = "1.6.3"
QUERYDSL_VERSION = "5.1.0"
COMPILER_VERSION = "3.14.0"
SUREFIRE_VERSION = "3.5.3"

_COMPILE_KINDS = frozenset({ErrorKind.COMPILE, ErrorKind.TEST_COMPILE, ErrorKind.PLUGIN_EXECUTION,
                            ErrorKind.UNKNOWN})
_POM_KINDS = frozenset({ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.POM_INVALID})


# ---------------------------------------------------------------------------
# data tables
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _versions_doc() -> dict[str, Any]:
    return yaml.safe_load((_DATA_DIR / "versions.yml").read_text(encoding="utf-8")) or {}


def known_versions() -> dict[str, str]:
    return {k: str(v) for k, v in (_versions_doc().get("versions") or {}).items()}


def known_plugin_versions() -> dict[str, str]:
    return {k: str(v) for k, v in (_versions_doc().get("plugins") or {}).items()}


def version_for(key: str, cfg: Optional[Config] = None) -> Optional[str]:
    """Known-good version for g:a (Spring Boot artifacts → target Boot version; company optional artifacts)."""
    if key.startswith("org.springframework.boot:"):
        return cfg.target.spring_boot_version if cfg else None
    if cfg is not None:
        spec = cfg.optional_keys().get(key)
        if spec is not None and spec.version and spec.key == key:
            return spec.version
    return known_versions().get(key)


class Relocation(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(alias="from")
    to: str
    version: Optional[str] = None
    scope: Optional[str] = None
    classifier: Optional[str] = None
    packages: list[str] = Field(default_factory=list)
    min_target_boot: Optional[int] = None
    version_policy: Literal["if-explicit", "always"] = "if-explicit"
    managed_since_boot: Optional[str] = None      # old dep without version in a Boot < this → set an explicit version

    @property
    def from_ga(self) -> tuple[str, str]:
        g, a = self.from_.split(":")[:2]
        return g, a

    def new_gav(self, version: Optional[str]) -> GAV:
        g, a = self.to.split(":")[:2]
        return GAV(group_id=g, artifact_id=a, version=version, scope=self.scope, classifier=self.classifier)


@lru_cache(maxsize=1)
def relocation_table() -> tuple[Relocation, ...]:
    doc = yaml.safe_load((_DATA_DIR / "relocations.yml").read_text(encoding="utf-8")) or {}
    return tuple(Relocation.model_validate(r) for r in doc.get("relocations", []))


def _boot_major(cfg: Optional[Config]) -> int:
    v = parse_version(cfg.target.spring_boot_version if cfg else "4.1.0")
    return v[0] if v else 4


# ---------------------------------------------------------------------------
# editor helpers (protocol methods + optional extras of the real PomEditor)
# ---------------------------------------------------------------------------
def _deps(ed: Any) -> list[GAV]:
    out = list(ed.dependencies())
    managed = getattr(ed, "managed_dependencies", None)
    if callable(managed):
        out += list(managed())
    return out


def _declared(ed: Any, key: str) -> Optional[GAV]:
    for d in _deps(ed):
        if d.key == key:
            return d
    return None


def _resolve(ed: Any, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    res = getattr(ed, "resolve", None)
    if callable(res):
        return res(value)
    m = re.fullmatch(r"\$\{([^}]+)\}", value.strip())
    if m:
        return ed.get_property(m.group(1)) or value
    return value


def _plugins(ed: Any) -> list[PluginInfo]:
    out: list[PluginInfo] = []
    for name in ("plugins", "managed_plugins"):
        fn = getattr(ed, name, None)
        if callable(fn):
            out += list(fn())
    return out


def _prop_ref(value: Optional[str]) -> Optional[str]:
    m = re.fullmatch(r"\s*\$\{([^}]+)\}\s*", value or "")
    return m.group(1) if m else None


def _bump_dependency(ed: Any, group: str, artifact: str, floor: str) -> bool:
    """Raise g:a to `floor` (direct + managed); through a local ${prop} when there is one."""
    d = _declared(ed, f"{group}:{artifact}")
    if d is None:
        return False
    prop = _prop_ref(d.version)
    if prop and ed.get_property(prop) is not None:
        cur = ed.get_property(prop)
        if version_lt(cur, floor) is False:
            return False
        return bool(ed.set_property(prop, floor))
    if d.version is not None and version_lt(_resolve(ed, d.version), floor) is False:
        return False
    return bool(ed.set_dependency_version(group, artifact, floor))


_GA_VERSION_RE = r"(<groupId>\s*{g}\s*</groupId>\s*<artifactId>\s*{a}\s*</artifactId>\s*<version>\s*)([^<]+?)(\s*</version>)"


def _text_bump(ws: Workspace, rel: str, group: str, artifact: str, floor: str,
               keep_from: Optional[str] = None) -> bool:
    """Bump literal (or ${prop}) versions of g:a anywhere in the pom text — covers annotationProcessorPaths and
    plugin dependencies which the editor protocol does not reach. `keep_from`: versions at or above it stay as they
    are even when they are below `floor` (jakarta-library-floor: never touch a declaration already on jakarta)."""
    def low(v: str) -> bool:
        if keep_from is not None and version_lt(v, keep_from) is False:
            return False
        return version_lt(v, floor) is True

    text = ws.read(rel)
    pat = re.compile(_GA_VERSION_RE.format(g=re.escape(group), a=re.escape(artifact)), re.S)
    props: set[str] = set()

    def repl(m: re.Match[str]) -> str:
        v = m.group(2).strip()
        p = _prop_ref(v)
        if p:
            props.add(p)
            return m.group(0)
        if low(v):
            return m.group(1) + floor + m.group(3)
        return m.group(0)

    new = pat.sub(repl, text)
    for p in props:
        pm = re.compile(rf"(<{re.escape(p)}>\s*)([^<]+?)(\s*</{re.escape(p)}>)")
        new = pm.sub(lambda m: m.group(1) + floor + m.group(3) if low(m.group(2)) else m.group(0), new)
    if new != text:
        ws.write(rel, new)
        return True
    return False


# ---------------------------------------------------------------------------
# base pom handler
# ---------------------------------------------------------------------------
class PomHandler(Handler):
    def editor(self, ws: Workspace, rel: str) -> Any:
        factory = self.registry.pom_editor_factory if self.registry is not None else _default_pom_editor_factory()
        return factory(ws.abs(rel))

    def apply(self, ws: Workspace, cluster: Optional[ErrorCluster] = None,
              profile: Optional[ProjectProfile] = None, cfg: Optional[Config] = None) -> list[FileChange]:
        changed = self.edit(ws, cluster, profile, cfg)
        seen: list[str] = []
        for p in changed:
            if p not in seen:
                seen.append(p)
        if seen:
            log.info("handler %s changed %s", self.id, ", ".join(seen))
        return [FileChange(path=p, op="modify") for p in seen]

    @abstractmethod
    def edit(self, ws: Workspace, cluster: Optional[ErrorCluster], profile: Optional[ProjectProfile],
             cfg: Optional[Config]) -> list[str]: ...


# ---------------------------------------------------------------------------
# version floors
# ---------------------------------------------------------------------------
class VersionFloorHandler(PomHandler):
    artifacts: ClassVar[tuple[tuple[str, str], ...]] = ()
    properties: ClassVar[tuple[str, ...]] = ()
    floor: ClassVar[str] = ""
    library: ClassVar[str] = ""

    def applicable(self, cluster, profile, cfg) -> bool:
        if profile is None or not profile.poms:
            return True
        if not profile_declares(profile, [f"{g}:{a}" for g, a in self.artifacts]) and \
                not (self.library and profile.has_library(self.library)):
            return False
        for lib in profile.libraries:
            if lib.name == self.library and version_lt(lib.version, self.floor) is False:
                return False
        return True

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        changed: list[str] = []
        for rel in all_poms(ws):
            ed = self.editor(ws, rel)
            ch = False
            for g, a in self.artifacts:
                ch |= _bump_dependency(ed, g, a, self.floor)
            for p in self.properties:
                cur = ed.get_property(p)
                if cur is not None and version_lt(cur, self.floor) is True:
                    ch |= bool(ed.set_property(p, self.floor))
            if ch:
                ed.save()
                changed.append(rel)
            for g, a in self.artifacts:
                if _text_bump(ws, rel, g, a, self.floor):
                    changed.append(rel)
        return changed


class LombokVersionFloor(VersionFloorHandler):
    id = "lombok-version-floor"
    description = f"raise lombok to ≥ {LOMBOK_VERSION} (older lombok cannot run on JDK 21 javac)"
    kinds = _COMPILE_KINDS
    triggers = (r"lombok", r"TypeTag\s*::\s*UNKNOWN", r"com\.sun\.tools\.javac\.code\.TypeTags")
    artifacts = (("org.projectlombok", "lombok"),)
    properties = ("lombok.version",)
    floor = LOMBOK_VERSION
    library = "lombok"
    # javac-internals wording = an old lombok IS on the processor path, whatever the poms declare (round 9: seen in
    # the first company run — the declared version was fine, the old jar came from the parent chain; the base
    # handler found nothing to bump and the LLM was asked first)
    _HARD_RE = re.compile(r"(?i)IllegalAccessError.*lombok|lombok\.javac\.apt|cannot access class com\.sun\.tools"
                          r"\.javac|TypeTag\s*::\s*UNKNOWN|com\.sun\.tools\.javac\.code\.TypeTags")
    _PATH_RE = re.compile(r"(<path>(?:(?!</path>).)*?<artifactId>\s*lombok\s*</artifactId>)((?:(?!</path>).)*?)(</path>)",
                          re.S)

    def _hard(self, cluster: Optional[ErrorCluster]) -> bool:
        return cluster is not None and bool(self._HARD_RE.search(cluster_text(cluster)))

    def applicable(self, cluster, profile, cfg) -> bool:
        return self._hard(cluster) or super().applicable(cluster, profile, cfg)

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        changed = super().edit(ws, cluster, profile, cfg)
        inherited = profile is not None and profile.has_library(self.library) \
            and not profile_declares(profile, ["org.projectlombok:lombok"])
        if not (self._hard(cluster) or (cluster is None and inherited)):
            return changed
        g, a = self.artifacts[0]
        # 1. an annotationProcessorPaths entry for lombok without a version (plugin-managed by the parent) → pin it
        for rel in all_poms(ws):
            text = ws.read(rel)
            new = self._PATH_RE.sub(
                lambda m: m.group(0) if "<version>" in m.group(2)
                else f"{m.group(1)}\n{_indent_of(m.group(1))}<version>{self.floor}</version>{m.group(2)}{m.group(3)}",
                text)
            if new != text:
                ws.write(rel, new)
                changed.append(rel)
        if changed:
            return list(dict.fromkeys(changed))
        # 2. lombok is not declared in any project pom (inherited from the parent's <dependencies>) → an explicit
        #    provided dependency in the root pom overrides the inherited version (Boot 4's BOM manages it later and
        #    RemoveRedundantDependencyVersions / drop_managed_versions take the pin away again)
        root = root_pom(ws, profile)
        if root:
            ed = self.editor(ws, root)
            if not any(d.key == f"{g}:{a}" for d in _deps(ed)):
                ed.add_dependency(GAV(group_id=g, artifact_id=a, version=self.floor, scope="provided"))
                ed.save()
                changed.append(root)
        if changed or not self._hard(cluster):
            return changed
        # 3. the project declares a fine lombok but the *parent* pom's maven-compiler-plugin carries an
        #    <annotationProcessorPaths> with an old one — Maven merges the inherited list into the child's
        #    configuration and the processor path wins over the dependency (seen in the first company run:
        #    "store POM declares Lombok 1.18.40 … inherited compiler configuration merges in an older processor
        #    path"). Cut the inheritance: write the effective list, versions floored, with combine.self="override".
        return self._override_processor_paths(ws, profile)

    # ------------------------------------------------------------------ inherited annotationProcessorPaths
    _APP_RE = re.compile(r"\n?[ \t]*<annotationProcessorPaths\b[^>]*>(?:(?!</annotationProcessorPaths>).)*"
                         r"</annotationProcessorPaths>", re.S)
    _CONF_RE = re.compile(r"(<configuration>)((?:(?!</configuration>).)*)(</configuration>)", re.S)

    def _effective_processor_paths(self, ws: Workspace) -> list[tuple[str, str, Optional[str]]]:
        """(groupId, artifactId, version) of every annotationProcessorPaths/path in the as-is effective pom(s)
        captured at intake (<run dir>/effective/*.xml, the merged parent + child view); [] when unavailable."""
        out: list[tuple[str, str, Optional[str]]] = []
        d = ws.run_dir / "effective"
        if not d.is_dir():
            return out
        try:
            from lxml import etree
        except ImportError:  # pragma: no cover
            return out
        for xml in sorted(d.glob("*.xml")):
            if xml.name.startswith("depmgmt-after-restructure"):
                continue
            try:
                root = etree.fromstring(xml.read_bytes(), parser=etree.XMLParser(recover=True, huge_tree=True))
            except (etree.XMLSyntaxError, OSError, ValueError):
                continue
            if root is None:
                continue
            for path in root.iter():
                if not isinstance(path.tag, str) or path.tag.split("}")[-1] != "path":
                    continue
                parent = path.getparent()
                if parent is None or parent.tag.split("}")[-1] != "annotationProcessorPaths":
                    continue
                vals = {}
                for child in path:
                    if isinstance(child.tag, str):
                        vals[child.tag.split("}")[-1]] = (child.text or "").strip()
                g, a = vals.get("groupId", ""), vals.get("artifactId", "")
                if a and (g, a) not in [(x, y) for x, y, _ in out]:
                    out.append((g or DEFAULT_PLUGIN_GROUP, a, vals.get("version") or None))
        return out

    def _override_processor_paths(self, ws: Workspace, profile: Optional[ProjectProfile]) -> list[str]:
        g, a = self.artifacts[0]
        paths = self._effective_processor_paths(ws)
        has_mapstruct = profile is not None and profile.has_library("mapstruct")
        if not paths:                                   # no effective pom: the processors the project itself uses
            paths = [(g, a, None)]
            if has_mapstruct:
                paths.append(("org.mapstruct", "mapstruct-processor", None))
        if not any(x == (g, a) for x, _ in [((p[0], p[1]), p) for p in paths]):
            paths.insert(0, (g, a, None))
        if has_mapstruct and any(p[1] == "mapstruct-processor" for p in paths) \
                and not any(p[1] == "lombok-mapstruct-binding" for p in paths):
            paths.append(("org.projectlombok", "lombok-mapstruct-binding", "0.2.0"))
        declared = next((lib.version for lib in (profile.libraries if profile else []) if lib.name == "lombok"), None)
        floors = {"lombok": self.floor, "mapstruct-processor": MAPSTRUCT_VERSION}
        rows: list[tuple[str, str, str]] = []
        for pg, pa, pv in paths:
            v = pv
            if pa == "lombok" and declared and "${" not in declared and version_lt(declared, v or "0") is False:
                v = declared                            # the project's own (newer) lombok wins
            floor = floors.get(pa)
            if floor and (v is None or "${" in (v or "") or version_lt(v, floor) is not False):
                v = floor
            rows.append((pg, pa, v or self.floor))
        # the pom to edit: the first project pom that configures maven-compiler-plugin, else the root pom
        target = None
        for rel in all_poms(ws):
            if CompilerPluginFloor._BLOCK.search(ws.read(rel)):
                target = rel
                break
        if target is None:
            target = root_pom(ws, profile)
            if target is None:
                return []
            ed = self.editor(ws, target)
            ed.add_plugin(PluginInfo(group_id=DEFAULT_PLUGIN_GROUP, artifact_id="maven-compiler-plugin",
                                     configuration={"release": "21"}))
            ed.save()
        text = ws.read(target)
        m = CompilerPluginFloor._BLOCK.search(text)
        if m is None:
            return []
        block = m.group(0)
        indent = _indent_of(block.rsplit("</plugin>", 1)[0].rstrip()) or "        "
        inner = indent + "  "
        lines = [f'{inner}<annotationProcessorPaths combine.self="override">']
        for pg, pa, pv in rows:
            lines += [f"{inner}  <path>", f"{inner}    <groupId>{pg}</groupId>", f"{inner}    <artifactId>{pa}</artifactId>",
                      f"{inner}    <version>{pv}</version>", f"{inner}  </path>"]
        lines.append(f"{inner}</annotationProcessorPaths>")
        new_app = "\n" + "\n".join(lines)
        if self._APP_RE.search(block):
            new_block = self._APP_RE.sub(lambda _m: new_app, block, count=1)
        elif self._CONF_RE.search(block):
            new_block = self._CONF_RE.sub(lambda cm: cm.group(1) + cm.group(2).rstrip() + new_app + "\n" + indent
                                          + cm.group(3), block, count=1)
        else:
            new_block = block.replace("</plugin>", f"{indent}<configuration>{new_app}\n{indent}</configuration>\n"
                                                   f"{indent}</plugin>", 1)
        if new_block == block:
            return []
        ws.write(target, text[:m.start()] + new_block + text[m.end():])
        return [target]


def _indent_of(block: str) -> str:
    """Indentation of the last line of `block` (to place a new element under it)."""
    last = block.rsplit("\n", 1)[-1]
    return last[: len(last) - len(last.lstrip())]


class MapstructVersionFloor(VersionFloorHandler):
    id = "mapstruct-version-floor"
    description = f"raise mapstruct and mapstruct-processor to ≥ {MAPSTRUCT_VERSION}"
    kinds = _COMPILE_KINDS
    triggers = (r"(?i)mapstruct", r"No implementation (was )?(created|generated) for")
    artifacts = (("org.mapstruct", "mapstruct"), ("org.mapstruct", "mapstruct-processor"))
    properties = ("mapstruct.version", "org.mapstruct.version")
    floor = MAPSTRUCT_VERSION
    library = "mapstruct"


class CompilerPluginFloor(PomHandler):
    id = "maven-compiler-plugin-floor"
    description = (f"maven-compiler-plugin ≥ {COMPILER_VERSION} and <release> instead of <source>/<target>")
    kinds = frozenset({ErrorKind.COMPILE, ErrorKind.TEST_COMPILE, ErrorKind.PLUGIN_EXECUTION,
                       ErrorKind.PLUGIN_RESOLUTION, ErrorKind.UNKNOWN})
    triggers = (r"invalid (target|source) release", r"(Source|Target) option \d+ is no longer supported",
                r"maven-compiler-plugin:(2\.|3\.([0-9]|1[0-2])\.)", r"Unsupported class file major version")
    G, A = DEFAULT_PLUGIN_GROUP, "maven-compiler-plugin"
    _BLOCK = re.compile(r"<plugin>(?:(?!</plugin>).)*?<artifactId>\s*maven-compiler-plugin\s*</artifactId>"
                        r"(?:(?!</plugin>).)*?</plugin>", re.S)

    def _java(self, cfg: Optional[Config]) -> str:
        return cfg.target.java_version if cfg else "21"

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        changed: list[str] = []
        jv = self._java(cfg)
        declared = False
        poms = all_poms(ws)
        for rel in poms:
            ed = self.editor(ws, rel)
            ch = False
            for pl in _plugins(ed):
                if pl.key != f"{self.G}:{self.A}" and not (pl.artifact_id == self.A and not pl.group_id):
                    continue
                declared = True
                v = _resolve(ed, pl.version)
                if pl.version is None or version_lt(v, COMPILER_VERSION) is not False:
                    ch |= bool(ed.set_plugin_version(self.G, self.A, COMPILER_VERSION))
            jprop = ed.get_property("java.version")
            if jprop is not None and "${" not in jprop and jprop != jv:
                ch |= bool(ed.set_property("java.version", jv))
            src, tgt = ed.get_property("maven.compiler.source"), ed.get_property("maven.compiler.target")
            if src is not None or tgt is not None:
                ch |= bool(ed.remove_property("maven.compiler.source"))
                ch |= bool(ed.remove_property("maven.compiler.target"))
                if jprop is not None:
                    ch |= bool(ed.set_property("maven.compiler.release", "${java.version}"))
                else:
                    ch |= bool(ed.set_property("maven.compiler.release", jv))
            else:
                rel_prop = ed.get_property("maven.compiler.release")
                if rel_prop is not None and "${" not in rel_prop and rel_prop != jv:
                    ch |= bool(ed.set_property("maven.compiler.release", jv))
            if ch:
                ed.save()
                changed.append(rel)
            if self._text_release(ws, rel, jv):
                changed.append(rel)
        if not declared:
            root = root_pom(ws, profile)
            if root:
                ed = self.editor(ws, root)
                if ed.add_plugin(PluginInfo(group_id=self.G, artifact_id=self.A, version=COMPILER_VERSION,
                                            configuration={"release": jv})):
                    ed.save()
                    changed.append(root)
        return changed

    def _text_release(self, ws: Workspace, rel: str, jv: str) -> bool:
        text = ws.read(rel)

        def fix(m: re.Match[str]) -> str:
            block = m.group(0)
            if "<release>" in block or "<source>" not in block:
                return block
            block = re.sub(r"<source>[^<]*</source>", f"<release>{jv}</release>", block, count=1)
            block = re.sub(r"\n?[ \t]*<target>[^<]*</target>", "", block)
            block = re.sub(r"\n?[ \t]*<test(Source|Target)>[^<]*</test(Source|Target)>", "", block)
            return block

        new = self._BLOCK.sub(fix, text)
        if new != text:
            ws.write(rel, new)
            return True
        return False


class CompilerLegacyConfig(PomHandler):
    """Round 9 (Java 1.6/1.7 projects): maven-compiler-plugin configuration that javac 21 rejects or that points at a
    JDK that no longer exists — `<fork>true</fork>` + `<executable>…/jdk1.6/bin/javac</executable>`,
    `<compilerVersion>`, `<compilerArguments><bootclasspath>…/rt.jar</bootclasspath>` (javac 21: "option
    --boot-class-path cannot be used together with --release"), `-Xbootclasspath/p:`, `-extdirs`, `-endorseddirs`,
    `-source/-target` passed as compiler args. Text-based like CompilerPluginFloor._text_release so the surrounding
    formatting stays; only the offending elements go."""
    id = "compiler-legacy-config"
    description = ("remove fork/executable/compilerVersion and bootclasspath/extdirs/endorseddirs/-source/-target "
                   "compiler arguments that javac 21 rejects with --release")
    kinds = frozenset({ErrorKind.COMPILE, ErrorKind.TEST_COMPILE, ErrorKind.PLUGIN_EXECUTION, ErrorKind.UNKNOWN})
    triggers = (r"option --?boot-?class-?path", r"-Xbootclasspath", r"cannot be used together with --release",
                r"not allowed with target \d+", r"Cannot run program .*javac", r"invalid flag: -(bootclasspath|extdirs|endorseddirs)",
                r"option --?(source|target) cannot be used together with --release")
    _BLOCK = CompilerPluginFloor._BLOCK
    _FLAT = ("fork", "executable", "compilerVersion")
    _ARG_RE = re.compile(r"^-(bootclasspath|Xbootclasspath(/[ap])?:?|extdirs|endorseddirs|source|target)(\b|$)")
    _ARG_NAMES = ("bootclasspath", "extdirs", "endorseddirs", "source", "target")

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        changed: list[str] = []
        for rel in all_poms(ws):
            text = ws.read(rel)
            new = self._BLOCK.sub(self._fix_block, text)
            if new != text:
                ws.write(rel, new)
                changed.append(rel)
        return changed

    def _fix_block(self, m: re.Match[str]) -> str:
        block = m.group(0)
        for tag in self._FLAT:
            block = re.sub(rf"\n?[ \t]*<{tag}>[^<]*</{tag}>", "", block)
        # <compilerArguments><bootclasspath>…</bootclasspath><Xbootclasspath/p>…</…>…</compilerArguments>
        def args_block(mm: re.Match[str]) -> str:
            inner = mm.group(0)
            for name in self._ARG_NAMES:
                inner = re.sub(rf"\n?[ \t]*<{name}>[^<]*</{name}>", "", inner)
            inner = re.sub(r"\n?[ \t]*<Xbootclasspath[^>]*>[^<]*</Xbootclasspath[^>]*>", "", inner)
            if not re.search(r"<compilerArguments>\s*<\w", inner):
                return ""                                             # nothing left → drop the container
            return inner
        block = re.sub(r"\n?[ \t]*<compilerArguments>(?:(?!</compilerArguments>).)*</compilerArguments>", args_block,
                       block, flags=re.S)
        # <compilerArgument>-bootclasspath ${java.home}/lib/rt.jar</compilerArgument>
        block = re.sub(r"\n?[ \t]*<compilerArgument>\s*-(bootclasspath|Xbootclasspath[^<]*|extdirs|endorseddirs|source|target)"
                       r"\b[^<]*</compilerArgument>", "", block)
        # <compilerArgs><arg>-bootclasspath</arg><arg>/path/rt.jar</arg> … </compilerArgs>
        def compiler_args(mm: re.Match[str]) -> str:
            inner = mm.group(0)
            args = re.findall(r"\n?[ \t]*<arg>([^<]*)</arg>", inner)
            keep: list[str] = []
            skip_next = False
            for a in args:
                v = a.strip()
                if skip_next:
                    skip_next = False
                    continue
                if self._ARG_RE.match(v):
                    # a flag written without its value takes the next <arg> as the value
                    if v in ("-bootclasspath", "-extdirs", "-endorseddirs", "-source", "-target"):
                        skip_next = True
                    continue
                keep.append(a)
            if len(keep) == len(args):
                return inner
            if not keep:
                return ""
            head = re.match(r"(\n?[ \t]*<compilerArgs>)", inner).group(1)
            indent = re.match(r"\n?([ \t]*)", inner).group(1)
            body = "".join(f"\n{indent}  <arg>{a.strip()}</arg>" for a in keep)
            return f"{head}{body}\n{indent}</compilerArgs>"
        block = re.sub(r"\n?[ \t]*<compilerArgs>(?:(?!</compilerArgs>).)*</compilerArgs>", compiler_args, block,
                       flags=re.S)
        return block


class SurefirePluginFloor(PomHandler):
    id = "surefire-plugin-floor"
    description = f"maven-surefire-plugin / maven-failsafe-plugin ≥ {SUREFIRE_VERSION}"
    kinds = frozenset({ErrorKind.PLUGIN_EXECUTION, ErrorKind.PLUGIN_RESOLUTION, ErrorKind.TEST_FAILURE,
                       ErrorKind.UNKNOWN})
    triggers = (r"maven-(surefire|failsafe)-plugin",)
    ARTIFACTS = ("maven-surefire-plugin", "maven-failsafe-plugin")

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        changed: list[str] = []
        for rel in all_poms(ws):
            ed = self.editor(ws, rel)
            ch = False
            for pl in _plugins(ed):
                if pl.artifact_id not in self.ARTIFACTS:
                    continue
                v = _resolve(ed, pl.version)
                if pl.version is None and cluster is None:
                    continue           # managed by the parent; only pin reactively
                if pl.version is None or version_lt(v, SUREFIRE_VERSION) is not False:
                    ch |= bool(ed.set_plugin_version(pl.group_id or DEFAULT_PLUGIN_GROUP, pl.artifact_id,
                                                     SUREFIRE_VERSION))
            if ch:
                ed.save()
                changed.append(rel)
        return changed


# ---------------------------------------------------------------------------
# relocations
# ---------------------------------------------------------------------------
def _missing_packages(cluster: Optional[ErrorCluster]) -> list[str]:
    if cluster is None:
        return []
    out = []
    for e in cluster.errors:
        m = re.search(r"package (\S+) does not exist", e.message or "")
        if m:
            out.append(m.group(1))
    return out


class RelocationHandler(PomHandler):
    """Replace old g:a by new g:a in every pom declaring it (table-driven)."""
    kinds = frozenset({ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.POM_INVALID, ErrorKind.COMPILE,
                       ErrorKind.TEST_COMPILE})
    extra_triggers: ClassVar[tuple[str, ...]] = ()

    def table(self) -> list[Relocation]:
        return list(relocation_table())

    def active(self, cfg: Optional[Config]) -> list[Relocation]:
        major = _boot_major(cfg)
        return [r for r in self.table() if r.min_target_boot is None or major >= r.min_target_boot]

    def relevant(self, cluster: Optional[ErrorCluster], profile: Optional[ProjectProfile],
                 cfg: Optional[Config]) -> list[Relocation]:
        rels = self.active(cfg)
        if cluster is None:
            return [r for r in rels if profile_declares(profile, [r.from_])]
        keys = cluster_artifact_keys(cluster)
        pkgs = _missing_packages(cluster)
        text = cluster_text(cluster) if self.extra_triggers else ""
        out: list[Relocation] = []
        for r in rels:
            if r.from_ in keys:
                out.append(r)
            elif pkgs and any(p == rp or p.startswith(rp + ".") for p in pkgs for rp in r.packages) \
                    and profile_declares(profile, [r.from_]):
                out.append(r)
            elif text and any(re.search(t, text) for t in self.extra_triggers) \
                    and profile_declares(profile, [r.from_]):
                out.append(r)
        return out

    def matches(self, cluster, profile, cfg) -> bool:
        return cluster.kind in self.kinds and bool(self.relevant(cluster, profile, cfg))

    def _version(self, r: Relocation, old: GAV, profile: Optional[ProjectProfile],
                 cfg: Optional[Config]) -> Optional[str]:
        want = r.version or version_for(r.to, cfg)
        if r.version_policy == "always":
            return want
        if old.version:
            return want
        if r.managed_since_boot and profile is not None:
            boot = profile.spring_boot_version
            if boot is None or version_lt(boot, r.managed_since_boot) is not False:
                return want
        return None

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        rels = self.relevant(cluster, profile, cfg) if cluster is not None else self.active(cfg)
        changed: list[str] = []
        for rel in all_poms(ws):
            ed = self.editor(ws, rel)
            ch = False
            for r in rels:
                old = _declared(ed, r.from_)
                if old is None:
                    continue
                g, a = r.from_ga
                ch |= bool(ed.replace_dependency(g, a, r.new_gav(self._version(r, old, profile, cfg))))
            if ch:
                ed.save()
                changed.append(rel)
        return changed


class DependencyRelocation(RelocationHandler):
    """Reactive: every row of relocations.yml the error names. Pre-emptive (after the spring recipes, `cluster is
    None`): only the javax → jakarta API rows (rows with `packages`) — an old javax API jar left on the classpath next
    to the migrated sources yields `jakarta.X cannot be converted to javax.X` against libraries; the Boot starter
    renames and the hibernate rows stay reactive (the recipes handle them, and a pre-emptive rename could fight
    them)."""
    id = "dependency-relocation"
    description = "relocate moved artifacts (javax→jakarta APIs, hibernate → org.hibernate.orm, Boot 4 starters)"
    preemptive_phase = Phase.SPRING

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        if cluster is not None:
            return super().edit(ws, cluster, profile, cfg)
        rels = [r for r in self.active(cfg) if r.packages]
        changed: list[str] = []
        for rel in all_poms(ws):
            ed = self.editor(ws, rel)
            ch = False
            for r in rels:
                old = _declared(ed, r.from_)
                if old is None:
                    continue
                g, a = r.from_ga
                ch |= bool(ed.replace_dependency(g, a, r.new_gav(self._version(r, old, profile, cfg))))
            if ch:
                ed.save()
                changed.append(rel)
        return changed


# ---------------------------------------------------------------------------
# X1b third-party libraries with a later jakarta release (knowledge/libraries/jakarta_libraries.yml)
# ---------------------------------------------------------------------------
def _jakarta_library_triggers() -> tuple[str, ...]:
    from sbmig.core.jakarta_libraries import jakarta_libraries

    base = (r"cannot be converted to (?:javax|jakarta)\.", r"required:\s*(?:javax|jakarta)\.",
            r"no suitable (?:method|constructor) found for \w+\([^)]*\bjakarta\.",
            r"class file for javax\.\S+ not found", r"package javax\.\S+ does not exist")
    words = tuple(dict.fromkeys(rf"\b{re.escape(k.split(':', 1)[1])}\b" for lib in jakarta_libraries()
                                for k in lib.all_keys))
    return base + words


def _pom_version(pom: Any, version: Optional[str]) -> Optional[str]:
    """A profile PomInfo dependency version with a local `${prop}` resolved (unresolvable → as written)."""
    p = _prop_ref(version)
    if p and p in (pom.properties or {}):
        return pom.properties[p]
    return version


def jakarta_libraries_below(profile: Optional[ProjectProfile]) -> list[tuple[Any, str]]:
    """[(JakartaLibrary, shown version)] for every table library the profile declares below its first jakarta release
    (any parsed version < jakarta_since) or only with versions that cannot be parsed (unversioned / `${prop}` from a
    parent). A library declared at least once at a parsed version ≥ jakarta_since and never below is fine. A
    declaration under one of its `old_keys` (earlier coordinates) always counts, whatever its version
    (shown as "<old key> <version>"). For an entry with a `classifier` (Ehcache 3: the jakarta build is the `jakarta`
    classifier jar) a declaration without that classifier counts as below whatever its version (shown as
    "<version> without classifier <c>")."""
    from sbmig.core.jakarta_libraries import jakarta_libraries

    out: list[tuple[Any, str]] = []
    if profile is None:
        return out
    for lib in jakarta_libraries():
        olds = [(d.key, _pom_version(p, d.version)) for p in profile.poms
                for d in [*p.dependencies, *p.dependency_management] if d.key in lib.old_keys]
        if olds:
            k, v = next(((k, v) for k, v in olds if v), olds[0])
            out.append((lib, f"{k} {v or 'unversioned'}"))
            continue
        decls = [(_pom_version(p, d.version), d.classifier) for p in profile.poms
                 for d in [*p.dependencies, *p.dependency_management] if d.key == lib.key]
        if not decls:
            continue
        vers = [v for v, _ in decls]
        low = [v for v in vers if version_lt(v, lib.jakarta_since) is True]
        if low:
            out.append((lib, low[0] or "?"))
        elif not any(version_lt(v, lib.jakarta_since) is False for v in vers):
            out.append((lib, next((v for v in vers if v), "version unknown")))
        elif lib.classifier and any(c != lib.classifier for _, c in decls):
            v = next((v for v, c in decls if c != lib.classifier and v), None) or "unversioned"
            out.append((lib, f"{v} without classifier {lib.classifier}"))
    return out


class JakartaLibraryFloor(PomHandler):
    """X1b (fourth company project, 2026-10-22): after the javax → jakarta migration the build failed with
    `jakarta.persistence.Query cannot be converted to javax.persistence.Query`; the owner was QLRM's
    `JpaResultMapper`, compiled against javax.persistence up to 3.0.4. Libraries of the knowledge table moved to
    jakarta.* in a known release: raise them to the table's `version` (direct + managed, through a local `${prop}`,
    annotation-processor / plugin declarations by text). A declaration already at or above `jakarta_since` is never
    touched. Flags / notes as every EdgeHandler (the orchestrator reads `getattr(handler, "flags" / "notes")`)."""
    id = "jakarta-library-floor"
    description = ("third-party libraries that moved to jakarta.* in a later release "
                   "(knowledge/libraries/jakarta_libraries.yml): raise them to that release after the javax→jakarta "
                   "migration")
    preemptive_phase = Phase.SPRING
    kinds = frozenset({ErrorKind.COMPILE, ErrorKind.TEST_COMPILE})
    triggers = _jakarta_library_triggers()

    def __init__(self) -> None:
        super().__init__()
        self.flags: list[str] = []
        self.notes: list[str] = []

    def flag(self, f: str) -> None:
        if f not in self.flags:
            self.flags.append(f)

    def note(self, n: str) -> None:
        if n not in self.notes:
            self.notes.append(n)

    def apply(self, ws: Workspace, cluster: Optional[ErrorCluster] = None,
              profile: Optional[ProjectProfile] = None, cfg: Optional[Config] = None) -> list[FileChange]:
        self.flags, self.notes = [], []
        changes = super().apply(ws, cluster, profile, cfg)
        if not changes:
            self.flags, self.notes = [], []
        return changes

    def applicable(self, cluster, profile, cfg) -> bool:
        if profile is None or not profile.poms:
            return True
        return bool(jakarta_libraries_below(profile))

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        from sbmig.core.jakarta_libraries import jakarta_libraries

        libs = jakarta_libraries()
        if not libs:
            return []
        poms = all_poms(ws)
        eds = {rel: self.editor(ws, rel) for rel in poms}
        # keys some project pom manages with an explicit version (an unversioned use elsewhere follows that pom)
        managed_here = {d.key for ed in eds.values()
                        for d in (getattr(ed, "managed_dependencies", lambda: [])() or []) if d.version}
        managed_here |= {lib.key for lib in libs if any(k in managed_here for k in lib.old_keys)}   # relocated below
        changed: list[str] = []
        done: dict[str, tuple[str, list[str]]] = {}          # key → (old version, poms)
        moved: dict[tuple[str, str], tuple[str, list[str]]] = {}   # (old key, key) → (old version, poms)
        classified: set[str] = set()                         # keys that got the entry's classifier
        raised: set[str] = set()                             # keys whose version was raised
        for rel in poms:
            ed = eds[rel]
            ch = False
            # earlier coordinates (ch.simas.qlrm:qlrm → org.qlrm:qlrm): relocate whatever the version, with a literal
            # version (a ${prop} use is dropped); an unversioned use stays unversioned when a project pom manages it
            for lib in libs:
                g, a = lib.group_id, lib.artifact_id
                for old_key in lib.old_keys:
                    decls = [d for d in _deps(ed) if d.key == old_key]
                    if not decls:
                        continue
                    og, oa = old_key.split(":", 1)
                    unversioned = all(d.version is None for d in decls)
                    keep_managed = unversioned and lib.key in managed_here
                    version = None if keep_managed else lib.version
                    if ed.replace_dependency(og, oa, GAV(group_id=g, artifact_id=a, version=version,
                                                         classifier=lib.classifier)):
                        ch = True
                        old = next((_resolve(ed, d.version) for d in decls if d.version), None) or "unversioned"
                        moved.setdefault((old_key, lib.key), (old, []))[1].append(rel)
            for lib in libs:
                g, a = lib.group_id, lib.artifact_id
                decls = [d for d in _deps(ed) if d.key == lib.key]
                if not decls:
                    continue
                vers = [_resolve(ed, d.version) for d in decls]
                # a classifier entry (Ehcache 3 `jakarta`): a declaration without the classifier is the javax flavour
                # whatever its version — set the classifier (direct + managed, Maven matches management on it too)
                no_classifier = bool(lib.classifier) and any(d.classifier != lib.classifier for d in decls)
                on_line = any(version_lt(v, lib.jakarta_since) is False for v in vers)
                if on_line and not no_classifier:
                    continue                                  # already on the jakarta line here: never touch it
                old = next((v for v in vers if v), None) or "unversioned"
                bumped = False
                unversioned = all(d.version is None for d in decls)
                if not on_line and unversioned and lib.key not in managed_here:
                    # managed by the parent chain (company base-parent / Boot BOM): never pin over the parent — if the
                    # build still fails on javax types the parent manages a release below jakarta_since (X1 says so)
                    log.info("jakarta-library-floor: %s unversioned in %s (parent-managed) — not pinned", lib.key, rel)
                elif not on_line and not (unversioned and lib.key in managed_here):
                    bumped = _bump_dependency(ed, g, a, lib.version)   # else the managing project pom gets the bump
                    if bumped:
                        raised.add(lib.key)
                if no_classifier and ed.replace_dependency(g, a, GAV(group_id=g, artifact_id=a,
                                                                     classifier=lib.classifier)):
                    classified.add(lib.key)
                    bumped = True
                if bumped:
                    ch = True
                    done.setdefault(lib.key, (old, []))[1].append(rel)
            if ch:
                ed.save()
                changed.append(rel)
            for lib in libs:
                before = ws.read(rel)
                if _text_bump(ws, rel, lib.group_id, lib.artifact_id, lib.version, keep_from=lib.jakarta_since):
                    changed.append(rel)
                    raised.add(lib.key)
                    if lib.key not in done or rel not in done[lib.key][1]:
                        m = re.search(_GA_VERSION_RE.format(g=re.escape(lib.group_id), a=re.escape(lib.artifact_id)),
                                      before, re.S)
                        old = m.group(2).strip() if m else "?"
                        done.setdefault(lib.key, (old, []))[1].append(rel)
        by_key = {lib.key: lib for lib in libs}
        for (old_key, key), (old, rels) in moved.items():
            lib = by_key[key]
            java = f", requires Java {lib.requires_java}" if lib.requires_java else ""
            cls = f" (classifier {lib.classifier})" if lib.classifier else ""
            self.note(f"{old_key} {old} → {key} {lib.version}{cls} (coordinates changed; jakarta release since "
                      f"{lib.jakarta_since}{java}) in {', '.join(dict.fromkeys(rels))}")
        for key, (old, rels) in done.items():
            lib = by_key[key]
            java = f"; requires Java {lib.requires_java}" if lib.requires_java else ""
            new = lib.version if key in raised else old
            cls = f" (classifier {lib.classifier})" if key in classified else ""
            self.note(f"{key} {old} → {new}{cls} ({lib.note}{java}) in {', '.join(dict.fromkeys(rels))}")
        if done or moved:
            self.flag("jakarta-library-bumped")
            log.info("jakarta-library-floor: %s", ", ".join(sorted({*done, *(k for _, k in moved)})))
        return changed


class JavaxServletApiToJakarta(RelocationHandler):
    id = "javax-servlet-api-to-jakarta"
    description = "javax.servlet(-api)/jsp-api → jakarta.servlet-api 6.1.0 / jakarta.servlet.jsp-api 4.0.0 (provided)"
    preemptive_phase = Phase.SPRING
    _TABLE = (
        Relocation.model_validate({"from": "javax.servlet:javax.servlet-api", "to": "jakarta.servlet:jakarta.servlet-api",
                                   "version": "6.1.0", "scope": "provided", "version_policy": "always",
                                   "packages": ["jakarta.servlet"]}),
        Relocation.model_validate({"from": "javax.servlet:servlet-api", "to": "jakarta.servlet:jakarta.servlet-api",
                                   "version": "6.1.0", "scope": "provided", "version_policy": "always",
                                   "packages": ["jakarta.servlet"]}),
        Relocation.model_validate({"from": "javax.servlet.jsp:javax.servlet.jsp-api",
                                   "to": "jakarta.servlet.jsp:jakarta.servlet.jsp-api", "version": "4.0.0",
                                   "scope": "provided", "version_policy": "always",
                                   "packages": ["jakarta.servlet.jsp"]}),
        Relocation.model_validate({"from": "javax.servlet.jsp:jsp-api",
                                   "to": "jakarta.servlet.jsp:jakarta.servlet.jsp-api", "version": "4.0.0",
                                   "scope": "provided", "version_policy": "always",
                                   "packages": ["jakarta.servlet.jsp"]}),
        Relocation.model_validate({"from": "javax.servlet:jsp-api",
                                   "to": "jakarta.servlet.jsp:jakarta.servlet.jsp-api", "version": "4.0.0",
                                   "scope": "provided", "version_policy": "always",
                                   "packages": ["jakarta.servlet.jsp"]}),
    )

    def table(self) -> list[Relocation]:
        return list(self._TABLE)


class JstlToJakarta(RelocationHandler):
    """Round 9c (first company run): `javax.servlet:jstl` lost its version when the old parent chain went
    (`'dependencies.dependency.version' for javax.servlet:jstl:jar is missing`) — and the javax JSTL cannot serve
    Jakarta JSPs anyway. Every javax-era JSTL coordinate moves to the Jakarta JSTL that the Boot 4 BOM manages:
    the Glassfish implementation (its API comes transitively) or the API artifact for api-only declarations."""
    id = "jstl-to-jakarta"
    description = "javax JSTL (javax.servlet:jstl, taglibs standard, jstl-api) → jakarta.servlet.jsp.jstl (Boot-managed)"
    preemptive_phase = Phase.SPRING
    extra_triggers = (r"javax\.servlet:jstl", r"javax\.servlet\.jsp\.jstl", r"taglibs:standard",
                      r"org\.glassfish\.web:javax\.servlet\.jsp\.jstl", r"jakarta/servlet/jsp/jstl", r"javax/servlet/jsp/jstl")
    _IMPL = "org.glassfish.web:jakarta.servlet.jsp.jstl"
    _API = "jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api"
    _TABLE = tuple(Relocation.model_validate(r) for r in (
        {"from": "javax.servlet:jstl", "to": _IMPL, "version": "3.0.1", "packages": ["jakarta.servlet.jsp.jstl"]},
        {"from": "jstl:jstl", "to": _IMPL, "version": "3.0.1", "packages": ["jakarta.servlet.jsp.jstl"]},
        {"from": "taglibs:standard", "to": _IMPL, "version": "3.0.1", "packages": ["jakarta.servlet.jsp.jstl"]},
        {"from": "org.apache.taglibs:taglibs-standard-impl", "to": _IMPL, "version": "3.0.1",
         "packages": ["jakarta.servlet.jsp.jstl"]},
        {"from": "org.apache.taglibs:taglibs-standard-spec", "to": _API, "version": "3.0.0",
         "packages": ["jakarta.servlet.jsp.jstl"]},
        {"from": "org.glassfish.web:javax.servlet.jsp.jstl", "to": _IMPL, "version": "3.0.1",
         "packages": ["jakarta.servlet.jsp.jstl"]},
        {"from": "javax.servlet.jsp.jstl:jstl-api", "to": _API, "version": "3.0.0", "packages": ["jakarta.servlet.jsp.jstl"]},
        {"from": "javax.servlet.jsp.jstl:javax.servlet.jsp.jstl-api", "to": _API, "version": "3.0.0",
         "packages": ["jakarta.servlet.jsp.jstl"]},
    ))

    def table(self) -> list[Relocation]:
        return list(self._TABLE)


class MysqlConnectorRelocation(RelocationHandler):
    id = "mysql-connector-relocation"
    description = "mysql:mysql-connector-java → com.mysql:mysql-connector-j"
    extra_triggers = (r"mysql-connector-java",)
    _TABLE = (Relocation.model_validate({"from": "mysql:mysql-connector-java", "to": "com.mysql:mysql-connector-j",
                                         "version": "9.7.0", "managed_since_boot": "2.7.8"}),)   # Boot 4.1 managed

    def table(self) -> list[Relocation]:
        return list(self._TABLE)


# ---------------------------------------------------------------------------
# querydsl / jaxb / boot plugin / missing versions
# ---------------------------------------------------------------------------
class QuerydslJakartaClassifier(PomHandler):
    id = "querydsl-jakarta-classifier"
    description = f"querydsl-jpa / querydsl-apt → {QUERYDSL_VERSION} with classifier 'jakarta'"
    preemptive_phase = Phase.SPRING
    kinds = frozenset({ErrorKind.COMPILE, ErrorKind.TEST_COMPILE, ErrorKind.PLUGIN_EXECUTION,
                       ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.UNKNOWN})
    triggers = (r"querydsl", r"javax/persistence", r"NoClassDefFoundError: javax", r"(?m)symbol:\s+class Q[A-Z]\w*",
                r"\bclass Q[A-Z]\w*")
    ARTIFACTS = (("com.querydsl", "querydsl-jpa"), ("com.querydsl", "querydsl-apt"))

    def applicable(self, cluster, profile, cfg) -> bool:
        if profile is None or not profile.poms:
            return True
        return profile.has_library("querydsl") or profile_declares(profile, [f"{g}:{a}" for g, a in self.ARTIFACTS])

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        changed: list[str] = []
        for rel in all_poms(ws):
            ed = self.editor(ws, rel)
            ch = False
            for g, a in self.ARTIFACTS:
                d = _declared(ed, f"{g}:{a}")
                if d is None:
                    continue
                v = _resolve(ed, d.version)
                if d.classifier == "jakarta" and v is not None and version_lt(v, "5.0.0") is False:
                    continue
                ch |= bool(ed.replace_dependency(g, a, GAV(group_id=g, artifact_id=a, version=QUERYDSL_VERSION,
                                                           classifier="jakarta")))
            if ch:
                ed.save()
                changed.append(rel)
        return changed


class JaxbRuntimeAdd(PomHandler):
    id = "jaxb-runtime-add"
    description = "add JAXB API + runtime (removed from the JDK); javax or jakarta flavour depending on the sources"
    kinds = _COMPILE_KINDS
    triggers = (r"javax\.xml\.bind", r"jakarta\.xml\.bind", r"javax/xml/bind", r"\bJAXB(Exception|Context)\b")

    def _uses_javax(self, ws: Workspace) -> bool:
        for f in ws.java_files():
            try:
                if "import javax.xml.bind" in ws.read(f):
                    return True
            except OSError:  # pragma: no cover
                continue
        return False

    def _targets(self, ws: Workspace, cluster, profile) -> list[str]:
        if cluster is not None:
            return poms_for_cluster(ws, cluster, profile)
        out: list[str] = []
        if profile is not None and profile.poms:
            out = [p.path for p in profile.poms if p.packaging != "pom" and ws.exists(p.path)]
        if not out:
            out = [p for p in all_poms(ws) if not re.search(r"<packaging>\s*pom\s*</packaging>", ws.read(p))]
        if not out:
            r = root_pom(ws, profile)
            out = [r] if r else []
        return out

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        kv = known_versions()
        if self._uses_javax(ws):
            api = GAV(group_id="javax.xml.bind", artifact_id="jaxb-api", version=kv.get("javax.xml.bind:jaxb-api"))
            rt = GAV(group_id="org.glassfish.jaxb", artifact_id="jaxb-runtime",
                     version=kv.get("org.glassfish.jaxb:jaxb-runtime@javax"), scope="runtime")
            api_keys = ("javax.xml.bind:jaxb-api", "jakarta.xml.bind:jakarta.xml.bind-api")
        else:
            api = GAV(group_id="jakarta.xml.bind", artifact_id="jakarta.xml.bind-api",
                      version=kv.get("jakarta.xml.bind:jakarta.xml.bind-api"))
            rt = GAV(group_id="org.glassfish.jaxb", artifact_id="jaxb-runtime",
                     version=kv.get("org.glassfish.jaxb:jaxb-runtime"), scope="runtime")
            api_keys = ("jakarta.xml.bind:jakarta.xml.bind-api",)
        changed: list[str] = []
        for rel in self._targets(ws, cluster, profile):
            ed = self.editor(ws, rel)
            ch = False
            if not any(ed.has_dependency(*k.split(":")) for k in api_keys):
                ch |= bool(ed.add_dependency(api))
            if not ed.has_dependency(rt.group_id, rt.artifact_id) and \
                    not ed.has_dependency("com.sun.xml.bind", "jaxb-impl"):
                ch |= bool(ed.add_dependency(rt))
            if ch:
                ed.save()
                changed.append(rel)
        return changed


class RemoveClassicLoaderImplementation(PomHandler):
    id = "remove-classic-loader-implementation"
    description = "drop spring-boot-maven-plugin <loaderImplementation>CLASSIC</loaderImplementation> (gone in Boot 4)"
    kinds = frozenset({ErrorKind.PLUGIN_EXECUTION, ErrorKind.POM_INVALID, ErrorKind.PLUGIN_RESOLUTION,
                       ErrorKind.UNKNOWN})
    triggers = (r"(?i)loaderImplementation", r"\bCLASSIC\b")
    _RE = re.compile(r"\n?[ \t]*<loaderImplementation>\s*CLASSIC\s*</loaderImplementation>")
    _PROP_RE = re.compile(r"\n?[ \t]*<spring-boot\.[\w.]*loaderImplementation>\s*CLASSIC\s*"
                          r"</spring-boot\.[\w.]*loaderImplementation>")

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        changed: list[str] = []
        for rel in all_poms(ws):
            text = ws.read(rel)
            new = self._PROP_RE.sub("", self._RE.sub("", text))
            if new != text:
                ws.write(rel, new)
                changed.append(rel)
        return changed


# ---------------------------------------------------------------------------
# binary files under a filtered resource (catalog E12)
# ---------------------------------------------------------------------------
def _indent_unit(text: str) -> str:
    m = re.search(r"^([ \t]+)<(?:modelVersion|parent|groupId|artifactId|version|packaging|name|properties|"
                  r"dependencies|dependencyManagement|build|modules)\b", text, flags=re.M)
    return m.group(1) if m else "  "


def _mask_comments(text: str) -> str:
    """Same length as `text` with XML comments blanked: spans found on the mask index into the original."""
    return re.sub(r"<!--.*?-->", lambda m: " " * len(m.group(0)), text, flags=re.S)


def _inside(masked: str, pos: int, tag: str) -> bool:
    return masked.count(f"<{tag}>", 0, pos) > masked.count(f"</{tag}>", 0, pos)


def _insert_before_close(text: str, close_idx: int, lines: list[str], unit: str) -> str:
    """Insert `lines` (indented relative to each other with `unit`) as children in front of the closing tag that
    starts at `close_idx`; a closing tag sharing its line with content is moved onto its own line."""
    line_start = text.rfind("\n", 0, close_idx) + 1
    lead = text[line_start:close_idx]
    if not lead.strip():
        body = "".join(f"{lead}{unit}{ln}\n" for ln in lines)
        return text[:line_start] + body + text[line_start:]
    indent = re.match(r"[ \t]*", text[line_start:]).group(0)
    body = "".join(f"\n{indent}{unit}{ln}" for ln in lines)
    return text[:close_idx] + body + "\n" + indent + text[close_idx:]


class ResourcesBinaryFiltering(PomHandler):
    """Binary files under a `<filtering>true</filtering>` resource (webfonts, keystores, archives, Office templates):
    maven-resources-plugin ≥ 3.2 — what the Boot 2.4+ / Boot 4 parents manage — reads them as text and fails the copy
    with `filtering <f> to <t> failed with MalformedInputException: Input length = 1` (2.x silently corrupted the
    copy instead). Fix = `<nonFilteredFileExtensions>` on the plugin that failed (resources; war for `<webResources>`):
    filtering itself stays on so `@project.version@` in application.properties keeps working — never
    `<filtering>false</filtering>`, never deleting the files. Text-based (formatting kept); pre-emptive from the
    analysis (`PropertyInventory.filtered_binary_files`), reactive on the plugin wording. Edited pom: the failing
    module's when it declares the filtering (or the filtering comes from an outside parent), else the root pom that
    declares it (one list for every module)."""
    id = "resources-binary-filtering"
    description = ("binary files under a filtered resource dir: add nonFilteredFileExtensions (webfonts, keystores, "
                   "archives, documents) to maven-resources-plugin / maven-war-plugin, filtering stays on")
    kinds = frozenset({ErrorKind.PLUGIN_EXECUTION, ErrorKind.UNKNOWN})
    triggers = (r"MalformedInputException", r"UnmappableCharacterException", r"Input length = \d+",
                r"filtering \S.* to \S.* failed with")
    RESOURCES, WAR = "maven-resources-plugin", "maven-war-plugin"
    IMAGE_DEFAULTS = frozenset({"jpg", "jpeg", "gif", "bmp", "png"})       # the plugins' built-in list
    _FILE_RE = re.compile(r"filtering\s+(?P<path>\S(?:.*?\S)?)\s+to\s+\S")
    _ENTRY_RE = re.compile(r"<nonFilteredFileExtension>\s*([^<\s]+)\s*</nonFilteredFileExtension>")
    _FILTERING_RE = re.compile(r"<filtering>\s*true\s*</filtering>")
    _PLUGIN_BLOCK = r"<plugin>(?:(?!</plugin>).)*?<artifactId>\s*{a}\s*</artifactId>(?:(?!</plugin>).)*?</plugin>"

    # -- what to add ----------------------------------------------------
    def wanted_extensions(self, cluster: Optional[ErrorCluster]) -> list[str]:
        """The table + the extension of every file the error names (an unusual one is still binary)."""
        from sbmig.analyze.properties import BINARY_RESOURCE_EXTENSIONS

        exts = [e.lower() for e in BINARY_RESOURCE_EXTENSIONS]
        for m in self._FILE_RE.finditer(cluster_text(cluster)):
            base = m.group("path").replace("\\", "/").rsplit("/", 1)[-1]
            ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
            if re.fullmatch(r"[a-z0-9]{1,8}", ext) and ext not in exts and ext not in self.IMAGE_DEFAULTS:
                exts.append(ext)
        return exts

    def plugin_for(self, cluster: Optional[ErrorCluster]) -> str:
        text = cluster_text(cluster)
        return self.WAR if re.search(r"maven-war-plugin|\bwar:(?:war|exploded|inplace)\b", text) else self.RESOURCES

    def _declares_filtering(self, text: str) -> bool:
        return bool(self._FILTERING_RE.search(_mask_comments(text)))

    def plugins_for_pom(self, text: str) -> list[str]:
        """Pre-emptive: the resources plugin, plus the war plugin when the pom filters `<webResources>`."""
        out = [self.RESOURCES]
        m = re.search(r"<webResources>.*?</webResources>", _mask_comments(text), flags=re.S)
        if m and self._FILTERING_RE.search(m.group(0)):
            out.append(self.WAR)
        return out

    def targets(self, ws: Workspace, cluster: Optional[ErrorCluster], profile: Optional[ProjectProfile]) -> list[str]:
        poms = all_poms(ws)
        if cluster is None:
            return [p for p in poms if self._declares_filtering(ws.read(p))]
        root = root_pom(ws, profile)
        out: list[str] = []
        for p in poms_for_cluster(ws, cluster, profile):
            pick = p
            if root and p != root and not self._declares_filtering(ws.read(p)) \
                    and self._declares_filtering(ws.read(root)):
                pick = root                      # filtering inherited from the root → one list for every module
            if pick not in out:
                out.append(pick)
        return out

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        exts = self.wanted_extensions(cluster)
        changed: list[str] = []
        for rel in self.targets(ws, cluster, profile):
            text = ws.read(rel)
            new = text
            for artifact in ([self.plugin_for(cluster)] if cluster is not None else self.plugins_for_pom(text)):
                new = self.add_extensions(new, artifact, exts)
            if new != text:
                ws.write(rel, new)
                changed.append(rel)
        return changed

    # -- text surgery ---------------------------------------------------
    def add_extensions(self, text: str, artifact: str, exts: list[str]) -> str:
        """`text` with `exts` in the plugin's `<nonFilteredFileExtensions>` (declaration created when absent)."""
        unit = _indent_unit(text)
        masked = _mask_comments(text)
        blocks = list(re.finditer(self._PLUGIN_BLOCK.format(a=re.escape(artifact)), masked, flags=re.S))
        if blocks:
            # a <build><plugins> declaration wins over <pluginManagement> (its configuration merges over the managed one)
            m = next((b for b in blocks if not _inside(masked, b.start(), "pluginManagement")), blocks[0])
            return text[:m.start()] + self._extend_block(text[m.start():m.end()], exts, unit) + text[m.end():]
        plugin_lines = ["<plugin>", f"{unit}<groupId>{DEFAULT_PLUGIN_GROUP}</groupId>",
                        f"{unit}<artifactId>{artifact}</artifactId>", f"{unit}<configuration>",
                        f"{unit * 2}<nonFilteredFileExtensions>",
                        *[f"{unit * 3}<nonFilteredFileExtension>{e}</nonFilteredFileExtension>" for e in exts],
                        f"{unit * 2}</nonFilteredFileExtensions>", f"{unit}</configuration>", "</plugin>"]
        build = next((b for b in re.finditer(r"<build>", masked) if not _inside(masked, b.start(), "profiles")), None)
        if build is None:
            close = masked.rfind("</project>")
            if close < 0:
                return text
            return _insert_before_close(text, close, ["<build>", f"{unit}<plugins>",
                                                      *[f"{unit * 2}{ln}" for ln in plugin_lines],
                                                      f"{unit}</plugins>", "</build>"], unit)
        b_end = masked.find("</build>", build.end())
        if b_end < 0:
            return text
        plugins_close = next((c.start() for c in re.compile(r"</plugins>").finditer(masked, build.end(), b_end)
                              if not _inside(masked, c.start(), "pluginManagement")), None)
        if plugins_close is not None:
            return _insert_before_close(text, plugins_close, plugin_lines, unit)
        return _insert_before_close(text, b_end, ["<plugins>", *[f"{unit}{ln}" for ln in plugin_lines], "</plugins>"],
                                    unit)

    def _extend_block(self, block: str, exts: list[str], unit: str) -> str:
        masked = _mask_comments(block)
        # plugin-level configuration only: an execution's <configuration> is masked away
        level = re.sub(r"<executions>.*?</executions>", lambda m: " " * len(m.group(0)), masked, flags=re.S)
        m = re.search(r"<nonFilteredFileExtensions>(.*?)</nonFilteredFileExtensions>", level, flags=re.S)
        if m:
            existing = {e.lower().lstrip(".") for e in self._ENTRY_RE.findall(level[m.start(1):m.end(1)])}
            missing = [e for e in exts if e not in existing]
            if not missing:
                return block
            return _insert_before_close(block, m.end(1),
                                        [f"<nonFilteredFileExtension>{e}</nonFilteredFileExtension>" for e in missing],
                                        unit)
        lines = ["<nonFilteredFileExtensions>",
                 *[f"{unit}<nonFilteredFileExtension>{e}</nonFilteredFileExtension>" for e in exts],
                 "</nonFilteredFileExtensions>"]
        c = re.search(r"</configuration>", level)
        if c:
            return _insert_before_close(block, c.start(), lines, unit)
        close = level.rfind("</plugin>")
        return _insert_before_close(block, close, ["<configuration>", *[f"{unit}{ln}" for ln in lines],
                                                   "</configuration>"], unit)


class StripPomRepositories(PomHandler):
    """`build.pom_repositories: remove` (opt-in): delete `<repositories>` / `<pluginRepositories>` from every pom
    (profiles included, comments kept). Legacy poms carry old Nexus / Central / Spring-milestone URLs that an
    air-gapped host cannot reach; with a catch-all mirror in `build.settings_file` they are redundant, without one
    they cost a resolution failure or a connect timeout per artifact. Pre-emptive (before the first build) when the
    profile shows declared URLs; reactive on transfer failures. `keep` (default) → never matches (RUNBOOK §0.1)."""
    id = "strip-pom-repositories"
    description = "remove <repositories>/<pluginRepositories> declared in the project poms (build.pom_repositories=remove)"
    kinds = frozenset({ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.PLUGIN_RESOLUTION, ErrorKind.PARENT_RESOLUTION,
                       ErrorKind.UNKNOWN})
    triggers = (r"Could not transfer artifact", r"Blocked mirror for repositories", r"Connection refused",
                r"UnknownHostException", r"Connect(?:ion)? timed out", r"No route to host", r"Transfer failed")
    _BLOCK_RE = re.compile(r"\n?[ \t]*<(repositories|pluginRepositories)>.*?</\1>[ \t]*", re.S)

    def applicable(self, cluster, profile, cfg) -> bool:
        return cfg is not None and getattr(cfg.build, "pom_repositories", "keep") == "remove"

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        if not self.applicable(cluster, profile, cfg):
            return []
        changed: list[str] = []
        for rel in all_poms(ws):
            text = ws.read(rel)
            masked = _mask_comments(text)
            spans = [(m.start(), m.end()) for m in self._BLOCK_RE.finditer(masked)]
            if not spans:
                continue
            new = text
            for a, b in reversed(spans):
                new = new[:a] + new[b:]
            if new != text:
                ws.write(rel, new)
                changed.append(rel)
        return changed


_MISSING_RE = re.compile(r"'(?:dependencyManagement\.)?dependencies\.dependency\.version' for "
                         r"([^:\s']+):([^:\s']+)(?::[^\s']+)? (?:is missing|must be a valid version)")


class MissingDependencyVersion(PomHandler):
    id = "missing-dependency-version"
    description = "set a known-good version for a dependency whose version is missing / unresolvable"
    kinds = frozenset({ErrorKind.POM_INVALID, ErrorKind.UNKNOWN})

    def missing(self, cluster: Optional[ErrorCluster], cfg: Optional[Config]) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        if cluster is None:
            return out
        for e in cluster.errors:
            for m in _MISSING_RE.finditer(f"{e.message}\n{e.raw}"):
                g, a = m.group(1), m.group(2)
                v = version_for(f"{g}:{a}", cfg)
                if v and (g, a, v) not in out:
                    out.append((g, a, v))
        return out

    def matches(self, cluster, profile, cfg) -> bool:
        return cluster.kind in self.kinds and bool(self.missing(cluster, cfg))

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        items = self.missing(cluster, cfg)
        if not items:
            return []
        targets = [p for p in poms_for_cluster(ws, cluster, profile)]
        changed: list[str] = []
        for rel in [*targets, *[p for p in all_poms(ws) if p not in targets]]:
            ed = self.editor(ws, rel)
            ch = False
            for g, a, v in items:
                d = _declared(ed, f"{g}:{a}")
                if d is None:
                    continue
                if d.version and "${" not in d.version:
                    continue
                if d.version and _resolve(ed, d.version) != d.version:
                    continue           # property resolves locally → not the culprit
                ch |= bool(ed.set_dependency_version(g, a, v))
            if ch:
                ed.save()
                changed.append(rel)
        return changed


class TestCompileSkip(Handler):
    """Placeholder: `-Dmaven.test.skip=true` is a last-resort flag owned by the orchestrator (never auto-matched)."""
    id = "test-compile-skip"
    description = "last resort: build with -Dmaven.test.skip=true (implemented by the build-fix loop, flagged)"
    __test__ = False

    def matches(self, cluster, profile, cfg) -> bool:
        return False

    def apply(self, ws, cluster=None, profile=None, cfg=None) -> list[FileChange]:
        return []


# ---------------------------------------------------------------------------
# add-known-dependency: compile error references a package whose artifact is known → add it to the module pom
# ---------------------------------------------------------------------------
class KnownPackage(BaseModel):
    model_config = ConfigDict(frozen=True)
    package: str
    artifact: str
    managed: bool = True
    version: Optional[str] = None
    scope: Optional[str] = None
    symbols: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.artifact


@lru_cache(maxsize=1)
def known_package_table() -> tuple[KnownPackage, ...]:
    doc = yaml.safe_load((_DATA_DIR / "known_packages.yml").read_text(encoding="utf-8")) or {}
    rows = []
    for row in doc.get("packages", []):
        row = dict(row)
        if "symbols" in row:
            row["symbols"] = tuple(row["symbols"])
        rows.append(KnownPackage(**row))
    # longest package prefix first so `org.springframework.boot.test` beats `org.springframework.boot`
    return tuple(sorted(rows, key=lambda r: -len(r.package)))


_PKG_PATTERNS = (
    re.compile(r"package (\S+) does not exist"),
    re.compile(r"cannot access ([\w.$]+)"),                 # cannot access org.hamcrest.Matcher → org.hamcrest
    re.compile(r"class file for ([\w.$]+) not found"),
    re.compile(r"location:\s+package ([\w.]+)"),
    re.compile(r"location:\s+(?:class|interface) ([\w.$]+)"),
)


def _missing_refs(cluster: Optional[ErrorCluster]) -> list[tuple[str, str]]:
    """(package-ish reference, simple symbol) pairs mentioned by the cluster's errors."""
    if cluster is None:
        return []
    out: list[tuple[str, str]] = []
    for e in cluster.errors:
        text = "\n".join(x for x in (e.message, e.raw, e.location) if x)
        sym = ""
        m = re.search(r"symbol:\s+(?:class|interface|variable|method|enum|record)\s+([\w$]+)", e.raw or "")
        if m:
            sym = m.group(1)
        elif e.symbol:
            parts = e.symbol.split()
            sym = parts[-1].split("(")[0] if parts else ""
        for pat in _PKG_PATTERNS:
            for mm in pat.finditer(text):
                out.append((mm.group(1).rstrip(".;,"), sym))
    return out


def _boot_managed_project(ws: Workspace, cfg: Optional[Config]) -> bool:
    """True when some pom is governed by a Spring Boot parent / BOM or by the configured in-house parent."""
    markers = ["<artifactId>spring-boot-starter-parent</artifactId>", "<artifactId>spring-boot-dependencies</artifactId>"]
    if cfg is not None:
        markers += [f"<artifactId>{s.artifact_id}</artifactId>" for s in cfg.artifacts.parent_specs()]
    for rel in all_poms(ws):
        try:
            text = ws.read(rel)
        except OSError:  # pragma: no cover
            continue
        if any(mk in text for mk in markers):
            return True
    return False


class AddKnownDependency(PomHandler):
    id = "add-known-dependency"
    description = ("add a well-known artifact when a compile error references its package "
                   "(hamcrest, jakarta.*, spring-boot-jackson2, modular *-test starters …)")
    kinds = frozenset({ErrorKind.COMPILE, ErrorKind.TEST_COMPILE})
    triggers = (r"package \S+ does not exist", r"cannot access [\w.$]+", r"class file for [\w.$]+ not found",
                r"cannot find symbol")
    preemptive_phase = Phase.SPRING

    @staticmethod
    def _lookup(ref: str, sym: str) -> Optional[KnownPackage]:
        for row in known_package_table():
            if ref == row.package or ref.startswith(row.package + "."):
                if row.symbols and sym and sym not in row.symbols:
                    continue
                if row.symbols and not sym:
                    continue
                return row
        return None

    def _wanted(self, cluster: Optional[ErrorCluster]) -> list[KnownPackage]:
        out: list[KnownPackage] = []
        for ref, sym in _missing_refs(cluster):
            row = self._lookup(ref, sym)
            if row is not None and row not in out:
                out.append(row)
        return out

    def applicable(self, cluster, profile, cfg) -> bool:
        return bool(self._wanted(cluster))

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        wanted = self._wanted(cluster)
        if not wanted or cluster is None:
            return []
        managed = _boot_managed_project(ws, cfg)
        kv = known_versions()
        test_scope = cluster.kind == ErrorKind.TEST_COMPILE
        changed: list[str] = []
        for rel in poms_for_cluster(ws, cluster, profile):
            ed = self.editor(ws, rel)
            ch = False
            for row in wanted:
                g, a = row.artifact.split(":", 1)
                if ed.has_dependency(g, a):
                    continue
                version = None if (row.managed and managed) else \
                    (row.version or kv.get(row.artifact) or version_for(row.artifact, cfg))
                if version is None and not (row.managed and managed):
                    log.info("add-known-dependency: no version known for %s in an unmanaged project — skipped",
                             row.artifact)
                    continue
                scope = "test" if test_scope else row.scope
                ch |= bool(ed.add_dependency(GAV(group_id=g, artifact_id=a, version=version, scope=scope)))
                log.info("add-known-dependency: %s%s → %s", row.artifact, f":{version}" if version else "", rel)
            if ch:
                ed.save()
                changed.append(rel)
        return changed


def default_handlers(cfg: Optional[Config] = None) -> list[Handler]:
    """Registration order = reactive precedence (first matching handler wins)."""
    from .case_replace import CaseReplace
    from .edge_handlers import edge_handlers
    from .source_handlers import source_handlers

    return [
        *edge_handlers(cfg),
        *source_handlers(cfg),          # E13 jakarta-namespace-sweep, E14 spring-data-crud-repository
        MissingDependencyVersion(),
        MysqlConnectorRelocation(),
        JavaxServletApiToJakarta(),
        JstlToJakarta(),
        DependencyRelocation(),
        JakartaLibraryFloor(),          # X1b: third-party libraries with a later jakarta release
        QuerydslJakartaClassifier(),
        JaxbRuntimeAdd(),
        AddKnownDependency(),
        LombokVersionFloor(),
        MapstructVersionFloor(),
        CompilerPluginFloor(),
        CompilerLegacyConfig(),
        SurefirePluginFloor(),
        RemoveClassicLoaderImplementation(),
        ResourcesBinaryFiltering(),
        StripPomRepositories(),
        TestCompileSkip(),
        CaseReplace(),                  # learned text replace of a catalog case; last = lowest precedence
    ]
