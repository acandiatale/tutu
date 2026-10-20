"""Edge-case detection for the analyzer (ERROR-HANDLING.md E7, S3–S6, X6).

* `system`-scoped dependencies (`<scope>system</scope>` + `<systemPath>`) → resolved path + existence;
* unsupported JVM languages (Kotlin / Groovy / Scala) and source-generator plugins (protobuf, jaxb2, openapi, querydsl);
* very old `maven.compiler.source` levels (1.5 / 1.6 / 1.7) and Java version normalization;
* legacy (non UTF-8) source encodings when the project does not declare `project.build.sourceEncoding`.

Everything here is read-only and never raises on unreadable files.
"""
from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from sbmig.core.models import LibraryUsage, PomInfo

from .pom_parser import WorkspaceView, _child, _children, _text, interpolate, is_skipped_path, pom_dir

CODEGEN_PLUGINS = ("protobuf-maven-plugin", "jaxb2-maven-plugin", "openapi-generator-maven-plugin",
                   "maven-jaxb2-plugin", "querydsl-maven-plugin")
LANGUAGES = (("kotlin", "*.kt", "Kotlin"), ("groovy", "*.groovy", "Groovy"), ("scala", "*.scala", "Scala"))
LANGUAGE_PLUGINS = {"kotlin": ("kotlin-maven-plugin",), "groovy": ("gmavenplus-plugin", "gmaven-plugin"),
                    "scala": ("scala-maven-plugin", "maven-scala-plugin")}
ENCODING_CANDIDATES = ("utf-8", "cp949", "euc-kr")
_BASEDIR_RE = re.compile(r"\$\{(?:project\.)?basedir\}")


# ---------------------------------------------------------------------------
# system-scoped dependencies (E7)
# ---------------------------------------------------------------------------
@dataclass
class SystemDep:
    pom: str                    # declaring pom (relative)
    group_id: str
    artifact_id: str
    version: Optional[str]
    system_path: str            # raw <systemPath>
    resolved: Optional[str]     # project-relative posix path, absolute path, or None when unresolvable
    inside: bool                # resolved path lies inside the project root
    exists: bool

    @property
    def key(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"


def resolve_system_path(raw: str, declaring_dir: str, props: Optional[dict[str, str]] = None,
                        root: Optional[Path] = None) -> tuple[Optional[str], bool]:
    """Resolve a `<systemPath>` declared by a pom in `declaring_dir` (project-relative, '' = root).

    Returns (path, inside): a project-relative posix path when the jar lies inside the project, the absolute path
    (inside=False) when it points elsewhere, or (None, False) when a property cannot be resolved."""
    value = (raw or "").strip()
    rel_base = declaring_dir or "."
    value = _BASEDIR_RE.sub(lambda _m: "\0BASEDIR\0", value)
    value = interpolate(value, dict(props or {})) or ""
    if "${" in value or not value:
        return None, False
    if value.startswith("\0BASEDIR\0"):
        rest = value[len("\0BASEDIR\0"):].lstrip("/\\")
        p = posixpath.normpath(posixpath.join(rel_base, rest.replace("\\", "/")))
    else:
        value = value.replace("\0BASEDIR\0", rel_base)
        if value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value):
            if root is not None:
                try:
                    return Path(value).resolve().relative_to(Path(root).resolve()).as_posix(), True
                except (ValueError, OSError):
                    pass
            return value, False
        p = posixpath.normpath(posixpath.join(rel_base, value.replace("\\", "/")))
    if p == ".." or p.startswith("../"):
        return p, False
    return p, True


def _dep_elements(root: ET.Element) -> Iterable[ET.Element]:
    yield from _children(_child(root, "dependencies"), "dependency")
    dm = _child(root, "dependencyManagement")
    yield from _children(_child(dm, "dependencies"), "dependency")
    for prof in _children(_child(root, "profiles"), "profile"):
        yield from _children(_child(prof, "dependencies"), "dependency")


def system_dependencies(ws: WorkspaceView, poms: list[PomInfo]) -> list[SystemDep]:
    root = Path(ws.root)
    out: list[SystemDep] = []
    for p in poms:
        try:
            tree = ET.fromstring(ws.read(p.path).encode("utf-8"))
        except (ET.ParseError, OSError, UnicodeError):
            continue
        props: dict[str, str] = {}
        chain: list[PomInfo] = []
        cur: Optional[PomInfo] = p
        while cur is not None and cur not in chain:   # in-repo parent chain (best effort, default relativePath)
            chain.append(cur)
            nxt = None
            if cur.parent_in_repo and cur.parent is not None:
                nxt = next((q for q in poms if q.artifact_id == cur.parent.artifact_id and q is not cur), None)
            cur = nxt
        for q in reversed(chain):
            props.update(q.properties)
        props.setdefault("project.version", p.version or "")
        for el in _dep_elements(tree):
            scope = (_text(el, "scope") or "").strip()
            sp = _text(el, "systemPath")
            if scope != "system" and not sp:
                continue
            resolved, inside = resolve_system_path(sp or "", pom_dir(p.path), props, root)
            exists = False
            if resolved is not None:
                exists = (root / resolved).is_file() if inside else Path(resolved).is_file()
            out.append(SystemDep(pom=p.path, group_id=interpolate(_text(el, "groupId"), props) or "",
                                 artifact_id=interpolate(_text(el, "artifactId"), props) or "",
                                 version=interpolate(_text(el, "version"), props), system_path=sp or "",
                                 resolved=resolved, inside=inside, exists=exists))
    return out


def system_dep_findings(deps: list[SystemDep]) -> tuple[list[str], Optional[LibraryUsage]]:
    warnings: list[str] = []
    evidence: list[str] = []
    for d in deps:
        if not d.system_path:
            where = "no <systemPath>"
        elif d.resolved is None:
            where = f"systemPath {d.system_path} (unresolvable property)"
        else:
            state = "exists" if d.exists else "MISSING"
            loc = "in project" if d.inside else "outside the project"
            where = f"systemPath {d.system_path} → {d.resolved} ({state}, {loc})"
        warnings.append(f"system-scoped dependency {d.key} in {d.pom}: {where}; it should be installed into the "
                        "company repository (Nexus) and declared as a normal dependency")
        evidence.append(f"pom:{d.key}" + (f":{d.version}" if d.version else ""))
        if d.resolved:
            evidence.append(f"file:{d.resolved}" + ("" if d.exists else " (missing)"))
    if not deps:
        return [], None
    return warnings, LibraryUsage(name="system-scope-jars", evidence=evidence)


# ---------------------------------------------------------------------------
# languages / generators (X6)
# ---------------------------------------------------------------------------
def _files(ws: WorkspaceView, pattern: str) -> list[str]:
    try:
        return [f.replace("\\", "/") for f in ws.glob(pattern) if not is_skipped_path(f.replace("\\", "/"))]
    except Exception:  # pragma: no cover - defensive
        return []


def language_findings(ws: WorkspaceView, poms: list[PomInfo]) -> tuple[list[str], list[LibraryUsage]]:
    warnings: list[str] = []
    libs: list[LibraryUsage] = []
    plugin_ids = {pl.artifact_id: p.path for p in poms for pl in p.plugins}
    for name, pattern, label in LANGUAGES:
        files = _files(ws, pattern)
        plugins = [a for a in LANGUAGE_PLUGINS[name] if a in plugin_ids]
        if not files and not plugins:
            continue
        ev = [f"file:{pattern}({len(files)})"] if files else []
        ev += [f"plugin:{a}" for a in plugins]
        sample = ", ".join(files[:3])
        warnings.append(f"{len(files)} {label} file(s) present" + (f" ({sample})" if sample else "")
                        + (f", plugin {', '.join(plugins)}" if plugins else "")
                        + ": not migrated by sbmig (OpenRewrite Java recipes and the LLM fixer only handle Java); "
                          "compile errors there are reported as blocked (unsupported_source)")
        libs.append(LibraryUsage(name=name, evidence=ev))
    for aid in CODEGEN_PLUGINS:
        hits = sorted({p.path for p in poms for pl in p.plugins if pl.artifact_id == aid})
        if not hits:
            continue
        ver = next((pl.version for p in poms for pl in p.plugins if pl.artifact_id == aid and pl.version), None)
        warnings.append(f"source generator plugin {aid}{':' + ver if ver else ''} in {', '.join(hits)}: generated "
                        "sources are not rewritten (the generator itself must emit jakarta/Java 21 compatible code; "
                        "check the plugin version and its jakarta options)")
        libs.append(LibraryUsage(name=f"codegen:{aid}", version=ver, evidence=[f"pom:{h}" for h in hits]))
    return warnings, libs


# ---------------------------------------------------------------------------
# java version (S-legacy levels)
# ---------------------------------------------------------------------------
def normalize_java_version(v: Optional[str]) -> Optional[str]:
    """'1.7' / '7' / '1.7.0' → '1.7' (levels ≤ 7 use the 1.x spelling); other values unchanged."""
    if not v:
        return v
    m = re.fullmatch(r"\s*(?:1\.)?([0-9]+)(?:\.[0-9]+)*\s*", v)
    if not m:
        return v
    major = int(m.group(1))
    if major <= 7 and (v.strip().startswith("1.") or major >= 5):
        return f"1.{major}"
    return v


def legacy_java_level(v: Optional[str]) -> bool:
    n = normalize_java_version(v)
    return n in ("1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7")


def java_level_findings(poms: list[PomInfo]) -> list[str]:
    out: list[str] = []
    for p in poms:
        if legacy_java_level(p.java_version):
            out.append(f"{p.path}: Java source/target level {p.java_version} cannot be compiled by JDK 21 (minimum 8); "
                       "UpgradeToJava21 sets release 21 and adds the JAXB / JAX-WS / common-annotations jars; removed "
                       "JDK APIs (sun.misc, sun.security, com.sun.image, javax.activation) are reported by the "
                       "jdk-removed-apis advisory and ported by the build-fix loop (`_` / `enum` identifiers are "
                       "compile errors fixed there too)")
    return out


# ---------------------------------------------------------------------------
# encoding (E1)
# ---------------------------------------------------------------------------
def detect_encoding(data: bytes, candidates: Iterable[str] = ENCODING_CANDIDATES) -> Optional[str]:
    """Strict decode attempts in order; None when no candidate decodes the bytes."""
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for enc in candidates:
        try:
            data.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def declared_source_encoding(poms: list[PomInfo]) -> Optional[str]:
    for p in poms:
        v = p.properties.get("project.build.sourceEncoding")
        if v:
            return v
        for pl in p.plugins:
            if pl.artifact_id == "maven-compiler-plugin":
                enc = pl.configuration.get("encoding")
                if isinstance(enc, str) and enc:
                    return enc
    return None


def encoding_findings(ws: WorkspaceView, poms: list[PomInfo],
                      java_files: Optional[list[str]] = None) -> tuple[list[str], Optional[LibraryUsage]]:
    if declared_source_encoding(poms):
        return [], None
    root = Path(ws.root)
    files = java_files if java_files is not None else _files(ws, "*.java")
    bad: list[tuple[str, Optional[str]]] = []
    for rel in files:
        try:
            data = (root / rel).read_bytes()
        except OSError:
            continue
        enc = detect_encoding(data)
        if enc not in ("utf-8", "utf-8-sig"):
            bad.append((rel, enc))
    if not bad:
        return [], None
    encs = sorted({e or "unknown" for _, e in bad})
    sample = ", ".join(f for f, _ in bad[:3])
    w = (f"sources are not UTF-8 (sample: {sample}; detected {', '.join(encs)}; {len(bad)} file(s)): set "
         "project.build.sourceEncoding or enable convert-utf8")
    return [w], LibraryUsage(name="legacy-encoding", evidence=encs)


__all__ = ["CODEGEN_PLUGINS", "SystemDep", "declared_source_encoding", "detect_encoding", "encoding_findings",
           "java_level_findings", "language_findings", "legacy_java_level", "normalize_java_version",
           "resolve_system_path", "system_dep_findings", "system_dependencies"]
