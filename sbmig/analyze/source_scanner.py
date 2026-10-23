"""Regex-based Java source scanner (no parser dependency) → `SourceStats`.

`scan_sources(ws)` returns a `ScanResult` that keeps per-file facts so the classifier can slice it per
maven module / MSA service (`ScanResult.subset(dir)`); `ScanResult.stats()` folds it into `SourceStats`.
"""
from __future__ import annotations

import fnmatch
import logging
import posixpath
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from sbmig.core.models import PomInfo, SourceStats, WebXmlModel

from .jsp import JSP_GLOBS, jsp_javax_refs
from .pom_parser import WorkspaceView
from .webxml import parse_web_xml

log = logging.getLogger("sbmig.analyze")

TRACKED_ANNOTATIONS = (
    "SpringBootApplication", "Controller", "RestController", "Configuration", "Service", "Repository",
    "Component", "WebServlet", "WebFilter", "WebListener", "SpringBootTest", "RunWith", "ExtendWith", "Test",
)
CONFIG_FILE_PATTERNS = (
    "application*.yml", "application*.yaml", "application*.properties",
    "bootstrap*.yml", "bootstrap*.yaml", "bootstrap*.properties",
    "logback*.xml", "log4j2*.xml",
)
BEANS_NS = "springframework.org/schema/beans"
# round 9c (front module): files that make a directory a static-web directory. Static assets (markup, styles,
# scripts + source maps, images, fonts), JSON / txt assets (manifest.json, robots.txt) and server-side template
# files living in `templates/` (Thymeleaf html, FreeMarker, Velocity, Mustache, Handlebars). JSPs are not listed
# here — they are reported separately (`SourceStats.jsp_paths`).
WEB_STATIC_EXTENSIONS = frozenset({
    "html", "htm", "css", "js", "map", "json", "txt",
    "png", "jpg", "jpeg", "gif", "svg", "ico", "webp",
    "woff", "woff2", "ttf", "eot", "otf",
    "ftl", "vm", "mustache", "hbs",
})
# web roots: `<module>/src/main/webapp` (minus WEB-INF) and `<module>/src/main/resources/{static,public,templates}`
_WEB_ROOT_RE = re.compile(r"^(?:(.*?)/)?src/main/(webapp|resources/(?:static|public|templates))/(.+)$")
MAX_XML_BYTES = 2_000_000
# 2026-10-23 (non-standard directories): media / archive files a web root or resource dir packages into a jar
LARGE_MEDIA_EXTENSIONS = frozenset({"mp4", "mov", "avi", "mkv", "wmv", "mp3", "wav", "zip", "tgz", "7z", "rar", "iso",
                                    "psd"})
LARGE_MEDIA_BYTES = 5 * 1024 * 1024
MAX_LARGE_MEDIA = 20
DEFAULT_WEBAPP = "src/main/webapp"
_BASEDIR_RE = re.compile(r"^\$\{(?:project\.)?basedir\}/?")

# strings/chars are matched first so "//" inside a literal is not treated as a comment
_COMMENT_RE = re.compile(r'("(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\')|//[^\n]*|/\*.*?\*/', re.S)
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_IMPORT_RE = re.compile(r"^\s*import\s+(static\s+)?([\w.]+(?:\.\*)?)\s*;", re.M)
_ANNOT_RE = re.compile(r"@(\w+)\b")
_PSVM_RE = re.compile(r"\b(?:public\s+static|static\s+public)\s+(?:final\s+)?void\s+main\s*\(")
_EXTENDS_SERVLET_RE = re.compile(r"\bextends\s+(?:[\w.]+\.)?HttpServlet\b")
_IMPLEMENTS_RE = re.compile(r"\bimplements\s+([\w\s,.<>?]+?)\s*\{")
_SERVLET_IFACES = {"Filter", "ServletContextListener", "HttpSessionListener", "ServletRequestListener",
                   "ServletContainerInitializer"}


def strip_comments(src: str) -> str:
    return _COMMENT_RE.sub(lambda m: m.group(1) or "", src)


@dataclass
class JavaFileInfo:
    path: str
    fqcn: str
    package: str
    is_test: bool
    imports: list[str] = field(default_factory=list)
    annotations: Counter = field(default_factory=Counter)
    has_psvm: bool = False
    is_servlet: bool = False
    is_filter_listener: bool = False
    lombok: bool = False

    @property
    def is_boot_app(self) -> bool:
        return self.annotations.get("SpringBootApplication", 0) > 0

    @property
    def is_controller(self) -> bool:
        return bool(self.annotations.get("Controller") or self.annotations.get("RestController"))


def scan_java_text(path: str, text: str) -> JavaFileInfo:
    src = strip_comments(text)
    pm = _PACKAGE_RE.search(src)
    package = pm.group(1) if pm else ""
    stem = posixpath.splitext(posixpath.basename(path))[0]
    imports = [m.group(2) for m in _IMPORT_RE.finditer(src)]
    annotations: Counter = Counter(a for a in _ANNOT_RE.findall(src) if a in TRACKED_ANNOTATIONS)
    servlet_import = any(i.startswith(("javax.servlet", "jakarta.servlet")) for i in imports)
    is_servlet = bool(_EXTENDS_SERVLET_RE.search(src)) or annotations.get("WebServlet", 0) > 0
    is_fl = bool(annotations.get("WebFilter") or annotations.get("WebListener"))
    if not is_fl and servlet_import:
        for m in _IMPLEMENTS_RE.finditer(src):
            names = {n.strip().split("<")[0].split(".")[-1] for n in m.group(1).split(",")}
            if names & _SERVLET_IFACES:
                is_fl = True
                break
    return JavaFileInfo(
        path=path, fqcn=f"{package}.{stem}" if package else stem, package=package,
        is_test="src/test/" in path or path.startswith("test/"),
        imports=imports, annotations=annotations, has_psvm=bool(_PSVM_RE.search(src)),
        is_servlet=is_servlet, is_filter_listener=is_fl,
        lombok=any(i.startswith("lombok.") for i in imports),
    )


def _ns_key(imp: str, ns: str) -> Optional[str]:
    if not imp.startswith(ns + "."):
        return None
    parts = imp.split(".")
    if len(parts) < 2:
        return None
    # javax.xml.* is split between the JDK (javax.xml.parsers) and Java EE (javax.xml.bind/ws/soap): keep 3 segments
    if parts[1] == "xml" and len(parts) >= 4:
        return ".".join(parts[:3])
    return ".".join(parts[:2])


def base_package(packages: list[str]) -> Optional[str]:
    """Longest common package prefix with ≥2 segments; else the most frequent top-2-segment prefix."""
    pkgs = [p for p in packages if p]
    if not pkgs:
        return None
    split = [p.split(".") for p in pkgs]
    common: list[str] = []
    for segs in zip(*split):
        if all(s == segs[0] for s in segs):
            common.append(segs[0])
        else:
            break
    if len(common) >= 2:
        return ".".join(common)
    tops = Counter(".".join(s[:2]) for s in split if len(s) >= 2)
    if tops:
        return sorted(tops.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return pkgs[0]


def _under(path: str, prefix: str) -> bool:
    return prefix in ("", ".") or path == prefix or path.startswith(prefix.rstrip("/") + "/")


@dataclass
class ScanResult:
    files: list[JavaFileInfo] = field(default_factory=list)
    kotlin_files: list[str] = field(default_factory=list)
    web_xml_paths: list[str] = field(default_factory=list)
    jsp_paths: list[str] = field(default_factory=list)
    # JSP / tag files (*.jsp, *.jspf, *.jspx, *.tag, *.tagx) naming a javax EE type of the jakarta table in a page /
    # tag import, a scriptlet or a useBean (sbmig.analyze.jsp) — what jakarta-namespace-sweep renames
    jsp_javax_paths: list[str] = field(default_factory=list)
    xml_bean_configs: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    # round 9c: static web files as (web root dir, file path), both root-relative (see WEB_STATIC_EXTENSIONS)
    web_static_files: list[tuple[str, str]] = field(default_factory=list)
    # 2026-10-23: parsed web.xml descriptors by path (`sbmig.analyze.webxml`); the first one fills SourceStats.web_xml_*
    web_xml_models: dict[str, WebXmlModel] = field(default_factory=dict)
    # 2026-10-23 (non-standard directories): web roots / war webResources dirs (root-relative) and large media files
    # (path, bytes) under a web root or resource dir — filled only when scan_sources gets the poms
    webapp_dirs: list[str] = field(default_factory=list)
    large_media: list[tuple[str, int]] = field(default_factory=list)

    def subset(self, prefix: str) -> "ScanResult":
        """Files below directory `prefix` (relative, '' = everything)."""
        f = lambda xs: [x for x in xs if _under(x, prefix)]  # noqa: E731
        return ScanResult(files=[j for j in self.files if _under(j.path, prefix)],
                          kotlin_files=f(self.kotlin_files), web_xml_paths=f(self.web_xml_paths),
                          jsp_paths=f(self.jsp_paths), jsp_javax_paths=f(self.jsp_javax_paths),
                          xml_bean_configs=f(self.xml_bean_configs),
                          config_files=f(self.config_files),
                          web_static_files=[w for w in self.web_static_files if _under(w[1], prefix)],
                          web_xml_models={k: v for k, v in self.web_xml_models.items() if _under(k, prefix)},
                          webapp_dirs=f(self.webapp_dirs),
                          large_media=[m for m in self.large_media if _under(m[0], prefix)])

    def import_counts(self) -> Counter:
        c: Counter = Counter()
        for j in self.files:
            c.update(j.imports)
        return c

    def stats(self) -> SourceStats:
        main = [j for j in self.files if not j.is_test]
        tests = [j for j in self.files if j.is_test]
        packages = sorted({j.package for j in self.files if j.package})
        main_pkgs = [j.package for j in main if j.package]
        javax: Counter = Counter()
        jakarta: Counter = Counter()
        annots: Counter = Counter()
        for j in self.files:
            for i in j.imports:
                k = _ns_key(i, "javax")
                if k:
                    javax[k] += 1
                k = _ns_key(i, "jakarta")
                if k:
                    jakarta[k] += 1
            annots.update(j.annotations)
        wx = next((self.web_xml_models[p] for p in self.web_xml_paths if p in self.web_xml_models), None)
        return SourceStats(
            java_files=len(self.files),
            test_files=len(tests),
            kotlin_files=len(self.kotlin_files),
            packages=packages,
            base_package=base_package(main_pkgs or [j.package for j in self.files]),
            main_classes=sorted(j.fqcn for j in main if j.is_boot_app),
            psvm_classes=sorted(j.fqcn for j in main if j.has_psvm and not j.is_boot_app),
            servlet_classes=sorted(j.fqcn for j in main if j.is_servlet),
            filter_listener_classes=sorted(j.fqcn for j in main if j.is_filter_listener),
            controllers=sorted(j.fqcn for j in main if j.is_controller),
            javax_imports=dict(sorted(javax.items())),
            jakarta_imports=dict(sorted(jakarta.items())),
            spring_annotations=dict(sorted(annots.items())),
            has_web_xml=bool(self.web_xml_paths),
            web_xml_paths=list(self.web_xml_paths),
            jsp_count=len(self.jsp_paths),
            jsp_paths=sorted(self.jsp_paths),
            jsp_javax_files=len(self.jsp_javax_paths),
            web_static_dirs=sorted({d for d, _ in self.web_static_files}),
            web_static_count=len(self.web_static_files),
            xml_bean_configs=list(self.xml_bean_configs),
            config_files=list(self.config_files),
            spring_boot_test_classes=sorted(j.fqcn for j in tests if j.annotations.get("SpringBootTest")),
            lombok=any(j.lombok for j in self.files),
            web_xml_spec=wx.spec if wx else None,
            web_xml_servlets=len(wx.servlets) if wx else 0,
            web_xml_filters=len(wx.filters) if wx else 0,
            web_xml_listeners=len(wx.listeners) if wx else 0,
            webapp_dirs=sorted(set(self.webapp_dirs)),
            large_media_files=sorted(p for p, _ in self.large_media)[:MAX_LARGE_MEDIA],
            large_media_bytes=sum(n for _, n in self.large_media),
        )


def _glob_plain(ws: WorkspaceView, pattern: str) -> list[str]:
    return [p.replace("\\", "/") for p in ws.glob(pattern)
            if not any(x in p.replace("\\", "/").split("/") for x in ("target", "node_modules", ".git"))]


def web_static_root(rel: str, web_dirs: Iterable[str] = (), resource_dirs: Iterable[str] = ()) -> Optional[str]:
    """The web root dir holding `rel` when `rel` is a static web file (extension in WEB_STATIC_EXTENSIONS, not under
    `<root>/WEB-INF`): `[<module>/]src/main/webapp`, `.../src/main/resources/{static,public,templates}` or (2026-10-23)
    one of `web_dirs` — the war plugin's `warSourceDirectory` / `<webResources>` dirs (`webapp_dirs`; the longest
    match wins). A file of a custom root that lies under one of `resource_dirs` inside that root is a classpath resource
    (the restructure keeps it in the comp module), not a static web file."""
    ext = posixpath.splitext(rel)[1].lstrip(".").lower()
    for d in sorted({x.strip("/") for x in web_dirs if x.strip("/")}, key=len, reverse=True):
        if not rel.startswith(d + "/"):
            continue
        rest = rel[len(d) + 1:]
        if rest == "WEB-INF" or rest.startswith("WEB-INF/") or ext not in WEB_STATIC_EXTENSIONS:
            return None
        if any(r.strip("/").startswith(d + "/") and rel.startswith(r.strip("/") + "/") for r in resource_dirs):
            return None
        return d
    m = _WEB_ROOT_RE.match(rel)
    if not m:
        return None
    prefix, kind, rest = m.group(1), m.group(2), m.group(3)
    if kind == "webapp" and (rest == "WEB-INF" or rest.startswith("WEB-INF/")):
        return None
    if ext not in WEB_STATIC_EXTENSIONS:
        return None
    return f"{prefix}/src/main/{kind}" if prefix else f"src/main/{kind}"


def _scan_web_static(ws: WorkspaceView, web_dirs: Iterable[str] = (),
                     resource_dirs: Iterable[str] = ()) -> list[tuple[str, str]]:
    """Static web files as (web root, path) — one walk over the workspace (target / node_modules / .git skipped)."""
    out: list[tuple[str, str]] = []
    web_dirs, resource_dirs = list(web_dirs), list(resource_dirs)
    custom = tuple(d.strip("/") + "/" for d in web_dirs if d.strip("/"))
    for rel in _glob_plain(ws, "*"):
        if "src/main/" in rel or (custom and rel.startswith(custom)):
            d = web_static_root(rel, web_dirs, resource_dirs)
            if d is not None:
                out.append((d, rel))
    return out


# ---------------------------------------------------------------------------
# 2026-10-23: web roots declared by maven-war-plugin (non-standard directories, e.g. `STATIC/`)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WebDir:
    """A directory the WAR takes web content from, relative to its pom's directory."""
    dir: str
    kind: str = "root"          # root = warSourceDirectory (or the default webapp) | resources = <webResources>
    target: str = ""            # <webResources><resource><targetPath> (web-path prefix), "" = the web root itself


def clean_pom_dir(raw: Optional[str], allow_parent: bool = False) -> Optional[str]:
    """A pom directory value as a relative posix path (`${basedir}/` / `${project.basedir}/` stripped); None for
    empty, other `${…}`, absolute and `..` paths. `allow_parent` (restructure, 2026-10-23): a path that climbs out of
    the pom's directory (`${basedir}/../../STATIC`) is returned normalized (`../../STATIC`) instead of None — the
    caller decides whether it still lies inside the project; a bare `..` stays None."""
    if not raw:
        return None
    p = _BASEDIR_RE.sub("", raw.strip().replace("\\", "/")).strip()
    if not p or "${" in p or p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        return None
    p = posixpath.normpath(p)
    if p in (".", "") or p == "..":
        return None
    if p.startswith("../") and not allow_parent:
        return None
    return p


def _xml_root(text: str) -> Any:
    import xml.etree.ElementTree as ET

    try:
        return ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text.lstrip("\ufeff")))
    except Exception:  # noqa: BLE001 - unparsable pom: nothing declared
        return None


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _kids(el: Any, name: str) -> list[Any]:
    return [] if el is None else [k for k in el if _local(k.tag) == name]


def _kid(el: Any, name: str) -> Any:
    ks = _kids(el, name)
    return ks[0] if ks else None


def _ktext(el: Any, name: str) -> Optional[str]:
    k = _kid(el, name)
    return k.text.strip() if k is not None and k.text else None


def pom_web_dirs(text: str, allow_parent: bool = False) -> list[WebDir]:
    """`maven-war-plugin` `<warSourceDirectory>` and `<webResources><resource><directory>` of one pom (`<build>`
    plugins, then pluginManagement; profiles ignored), relative to the pom's directory, deduped. `allow_parent`:
    `../` paths are kept (see `clean_pom_dir`)."""
    root = _xml_root(text)
    build = _kid(root, "build") if root is not None else None
    out: list[WebDir] = []
    for plugins in (_kid(build, "plugins"), _kid(_kid(build, "pluginManagement"), "plugins")):
        for p in _kids(plugins, "plugin"):
            if _ktext(p, "artifactId") != "maven-war-plugin":
                continue
            conf = _kid(p, "configuration")
            d = clean_pom_dir(_ktext(conf, "warSourceDirectory"), allow_parent)
            if d and all(w.dir != d for w in out):
                out.append(WebDir(d, "root"))
            for r in _kids(_kid(conf, "webResources"), "resource"):
                d = clean_pom_dir(_ktext(r, "directory"), allow_parent)
                if d and all(w.dir != d for w in out):
                    tgt = (_ktext(r, "targetPath") or "").replace("\\", "/").strip("/")
                    out.append(WebDir(d, "resources", "" if "${" in tgt or tgt.startswith("..") else tgt))
    return out


def pom_build_dirs(text: str, allow_parent: bool = False) -> list[str]:
    """Directories `<build>` compiles / packages as classpath content: sourceDirectory, scriptSourceDirectory and
    every `<resources>` directory (Maven default `src/main/resources` when no `<resources>` is declared), relative to
    the pom's directory. Test directories are not listed. `allow_parent`: `../` paths are kept (see `clean_pom_dir`)."""
    root = _xml_root(text)
    build = _kid(root, "build") if root is not None else None
    out: list[str] = []
    for name in ("sourceDirectory", "scriptSourceDirectory"):
        d = clean_pom_dir(_ktext(build, name), allow_parent)
        if d:
            out.append(d)
    res = _kid(build, "resources")
    if res is None:
        out.append("src/main/resources")
    for r in _kids(res, "resource"):
        d = clean_pom_dir(_ktext(r, "directory"), allow_parent) if _kid(r, "directory") is not None \
            else "src/main/resources"
        if d and d not in out:
            out.append(d)
    return out


def _join(base: str, rel: str) -> str:
    return posixpath.normpath(posixpath.join(base, rel)) if base else rel


def webapp_dirs(ws: WorkspaceView, poms: Iterable[PomInfo]) -> tuple[list[str], list[str]]:
    """(web dirs, classpath dirs), root-relative: per pom the default `src/main/webapp` when it exists plus every
    `warSourceDirectory` / `<webResources>` directory (`pom_web_dirs`), and its `pom_build_dirs`."""
    web: list[str] = []
    res: list[str] = []
    root = Path(ws.root)
    for p in poms:
        base = posixpath.dirname(p.path.replace("\\", "/"))
        try:
            text = ws.read(p.path)
        except OSError:  # pragma: no cover - unreadable pom
            continue
        default = _join(base, DEFAULT_WEBAPP)
        if (root / default).is_dir() and default not in web:
            web.append(default)
        for w in pom_web_dirs(text):
            d = _join(base, w.dir)
            if d not in web:
                web.append(d)
        for d in pom_build_dirs(text):
            d = _join(base, d)
            if d not in res:
                res.append(d)
    return web, res


def _scan_large_media(ws: WorkspaceView, dirs: list[str]) -> list[tuple[str, int]]:
    """(path, bytes) of every file ≥ LARGE_MEDIA_BYTES with a LARGE_MEDIA_EXTENSIONS extension below `dirs`."""
    roots = tuple(d.strip("/") + "/" for d in dirs if d.strip("/"))
    if not roots:
        return []
    out: dict[str, int] = {}
    base = Path(ws.root)
    for rel in _glob_plain(ws, "*"):
        ext = posixpath.splitext(rel)[1].lstrip(".").lower()
        if ext not in LARGE_MEDIA_EXTENSIONS or rel in out or not rel.startswith(roots):
            continue
        try:
            size = (base / rel).stat().st_size
        except OSError:  # pragma: no cover
            continue
        if size >= LARGE_MEDIA_BYTES:
            out[rel] = size
    return sorted(out.items())


def _scan_jsp_javax(ws: WorkspaceView) -> list[str]:
    """JSP / tag files whose imports / scriptlets / useBeans name a javax EE type of the jakarta table."""
    out: set[str] = set()
    for pattern in JSP_GLOBS:
        for rel in _glob_plain(ws, pattern):
            try:
                if jsp_javax_refs(ws.read(rel)):
                    out.add(rel)
            except OSError:  # pragma: no cover - unreadable file
                continue
    return sorted(out)


def scan_sources(ws: WorkspaceView, poms: Optional[Iterable[PomInfo]] = None) -> ScanResult:
    """`poms` (2026-10-23): the parsed poms — their war plugin web roots count as web roots (`webapp_dirs`) and the
    large media files below the web roots / resource dirs are listed; None keeps the pom-less behaviour."""
    res = ScanResult()
    web_dirs, res_dirs = webapp_dirs(ws, poms) if poms is not None else ([], [])
    res.webapp_dirs = web_dirs
    for rel in _glob_plain(ws, "*.java"):
        try:
            res.files.append(scan_java_text(rel, ws.read(rel)))
        except OSError as e:  # pragma: no cover - unreadable file
            log.warning("cannot read %s: %s", rel, e)
    res.kotlin_files = _glob_plain(ws, "*.kt")
    res.web_xml_paths = [p for p in _glob_plain(ws, "web.xml") if p.endswith("WEB-INF/web.xml")]
    for rel in res.web_xml_paths:
        try:
            text = ws.read(rel)
        except OSError as e:  # pragma: no cover - unreadable file
            log.warning("cannot read %s: %s", rel, e)
            continue
        if len(text) <= MAX_XML_BYTES:
            res.web_xml_models[rel] = parse_web_xml(text, rel)
    res.jsp_paths = sorted(_glob_plain(ws, "*.jsp"))
    res.jsp_javax_paths = _scan_jsp_javax(ws)
    res.web_static_files = _scan_web_static(ws, web_dirs, res_dirs)
    if poms is not None:
        res.large_media = _scan_large_media(ws, [*web_dirs, *res_dirs])
    for rel in _glob_plain(ws, "*.xml"):
        base = posixpath.basename(rel)
        if base == "pom.xml":
            continue
        if any(fnmatch.fnmatch(base, pat) for pat in CONFIG_FILE_PATTERNS):
            continue
        try:
            text = ws.read(rel)
        except OSError:  # pragma: no cover
            continue
        if len(text) <= MAX_XML_BYTES and BEANS_NS in text and "<beans" in text:
            res.xml_bean_configs.append(rel)
    cfgs: set[str] = set()
    for pat in CONFIG_FILE_PATTERNS:
        cfgs.update(_glob_plain(ws, pat))
    res.config_files = sorted(cfgs)
    return res


def scan_source_stats(ws: WorkspaceView) -> SourceStats:
    return scan_sources(ws).stats()
