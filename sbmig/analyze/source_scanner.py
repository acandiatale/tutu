"""자바 소스와 웹 파일을 검사해서 import, 어노테이션, 웹 파일 같은 정보를 모으는 모듈.
파일은 읽기만 한다.

Regex only, no parser library. The result is the per-file ``ScanResult`` and its aggregate ``SourceStats``.
The analyzer calls :func:`scan_sources` after parsing the poms; the classifier splits the result per module
or MSA service with ``ScanResult.subset(dir)``. The restructure uses :func:`pom_web_dirs`,
:func:`pom_build_dirs`, :func:`clean_pom_dir` and ``WEB_STATIC_EXTENSIONS`` to pick the web files that move
to the front module. Comments are stripped before matching, so a commented-out import is not counted.
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
from sbmig.core.workspace import is_build_output_path

from .jsp import JSP_GLOBS, jsp_javax_refs
from .pom_parser import WorkspaceView, read_pom_text, xml_without_declaration
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
# 디렉토리를 정적 웹 디렉토리로 보게 하는 확장자. front 모듈로 옮길 파일을 정한다
# Static assets, JSON / txt assets (manifest.json, robots.txt) and server-side templates living in
# `templates/` (Thymeleaf html, FreeMarker, Velocity, Mustache, Handlebars). JSPs are reported separately
# (`SourceStats.jsp_paths`).
WEB_STATIC_EXTENSIONS = frozenset({
    "html", "htm", "css", "js", "map", "json", "txt",
    "png", "jpg", "jpeg", "gif", "svg", "ico", "webp",
    "woff", "woff2", "ttf", "eot", "otf",
    "ftl", "vm", "mustache", "hbs",
})
# 웹 루트: `<module>/src/main/webapp`(WEB-INF 제외)와 `<module>/src/main/resources/{static,public,templates}`
_WEB_ROOT_RE = re.compile(r"^(?:(.*?)/)?src/main/(webapp|resources/(?:static|public|templates))/(.+)$")
MAX_XML_BYTES = 2_000_000
# 웹 루트나 리소스 디렉토리에 있으면 jar에 함께 들어가는 대용량 미디어, 압축 파일 확장자
LARGE_MEDIA_EXTENSIONS = frozenset({"mp4", "mov", "avi", "mkv", "wmv", "mp3", "wav", "zip", "tgz", "7z", "rar", "iso",
                                    "psd"})
LARGE_MEDIA_BYTES = 5 * 1024 * 1024
MAX_LARGE_MEDIA = 20
DEFAULT_WEBAPP = "src/main/webapp"
_BASEDIR_RE = re.compile(r"^\$\{(?:project\.)?basedir\}/?")

# 문자열 안의 "http://..."를 주석으로 보고 뒷부분을 지우지 않도록 문자열을 먼저 찾는다
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
    """자바 주석(// 와 /* */)을 지운다. 문자열과 문자 리터럴은 그대로 둔다."""
    return _COMMENT_RE.sub(lambda m: m.group(1) or "", src)


@dataclass
class JavaFileInfo:
    """자바 파일 하나에서 뽑은 정보를 담는 클래스. 패키지, import, 어노테이션, main 메서드가 있는지 등이다."""
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
        """@SpringBootApplication이 붙은 클래스이면 True를 반환한다."""
        return self.annotations.get("SpringBootApplication", 0) > 0

    @property
    def is_controller(self) -> bool:
        """@Controller나 @RestController가 붙어 있으면 True를 반환한다."""
        return bool(self.annotations.get("Controller") or self.annotations.get("RestController"))


def scan_java_text(path: str, text: str) -> JavaFileInfo:
    """자바 소스 하나에서 패키지, import, 어노테이션 같은 정보를 뽑는다. 주석은 먼저 지운다.
    @WebFilter나 @WebListener가 붙어 있으면 필터, 리스너로 본다.
    servlet을 import하고 ``implements Filter`` 같은 선언이 있어도 필터, 리스너로 본다."""
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
    """import를 집계 키로 바꾼다. ``javax.servlet.http.X``는 ``javax.servlet``이 된다.
    ``ns``로 시작하지 않으면 None을 반환한다.
    ``javax.xml`` 아래는 세 단계까지 쓴다. JDK의 ``javax.xml.parsers``와 EE의 ``javax.xml.bind``를 따로 세야 한다."""
    if not imp.startswith(ns + "."):
        return None
    parts = imp.split(".")
    if len(parts) < 2:
        return None
    if parts[1] == "xml" and len(parts) >= 4:
        return ".".join(parts[:3])
    return ".".join(parts[:2])


def base_package(packages: list[str]) -> Optional[str]:
    """프로젝트의 기본 패키지를 반환한다. 패키지가 없으면 None이다.
    두 단계 이상인 공통 접두어가 있으면 그것을 쓴다. 없으면 가장 많이 쓰인 상위 두 단계를 쓴다."""
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
    """스캔 결과를 파일 단위로 보관하는 클래스. ``subset``으로 모듈이나 서비스별로 나누고 ``stats``로 집계한다."""
    files: list[JavaFileInfo] = field(default_factory=list)
    kotlin_files: list[str] = field(default_factory=list)
    web_xml_paths: list[str] = field(default_factory=list)
    jsp_paths: list[str] = field(default_factory=list)
    # javax EE 타입을 쓰는 JSP와 태그 파일 목록. 재구성이 jakarta로 바꾼다. SourceStats에는 개수만 남긴다
    jsp_javax_paths: list[str] = field(default_factory=list)
    xml_bean_configs: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    # 정적 웹 파일의 (웹 루트, 파일 경로). 둘 다 루트 기준 상대 경로다
    web_static_files: list[tuple[str, str]] = field(default_factory=list)
    # 경로별로 파싱한 web.xml. 첫 번째 것이 SourceStats.web_xml_* 값을 채운다
    web_xml_models: dict[str, WebXmlModel] = field(default_factory=dict)
    # 웹 루트와 war webResources 디렉토리, 그 아래 대용량 미디어 파일의 (경로, 바이트)
    # scan_sources가 pom을 받았을 때만 채운다
    webapp_dirs: list[str] = field(default_factory=list)
    large_media: list[tuple[str, int]] = field(default_factory=list)

    def subset(self, prefix: str) -> "ScanResult":
        """디렉토리 ``prefix`` 아래 파일만 남긴 ScanResult를 반환한다. 빈 문자열이면 전체를 반환한다."""
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
        """import 문자열별 등장 횟수를 반환한다. 라이브러리 탐지의 근거로 쓴다."""
        c: Counter = Counter()
        for j in self.files:
            c.update(j.imports)
        return c

    def stats(self) -> SourceStats:
        """파일별 정보를 프로필용 ``SourceStats``로 집계한다.
        main 클래스, 서블릿, 컨트롤러 등은 테스트 소스를 빼고 센다.
        web.xml이 여러 개면 ``web_xml_paths``에서 처음 파싱된 것의 값을 ``web_xml_*``에 쓴다."""
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
    """``pattern``에 맞는 파일 경로를 반환한다. node_modules, .git, Maven 빌드 출력은 제외한다."""
    return [p.replace("\\", "/") for p in ws.glob(pattern)
            if not any(x in p.replace("\\", "/").split("/") for x in ("node_modules", ".git"))
            and not is_build_output_path(p)]


def web_static_root(rel: str, web_dirs: Iterable[str] = (), resource_dirs: Iterable[str] = ()) -> Optional[str]:
    """``rel``이 정적 웹 파일이면 그 웹 루트를, 아니면 None을 반환한다. front 모듈로 옮길 파일을 고를 때 쓴다.
    웹 루트는 ``src/main/webapp``(WEB-INF 제외), ``src/main/resources/{static,public,templates}``,
    war 플러그인이 선언한 디렉토리(``web_dirs``)다. 선언한 디렉토리는 긴 것부터 본다.
    웹 루트 안이라도 ``resource_dirs`` 아래 파일은 클래스패스 리소스다. 정적 파일로 보지 않는다.

    `web_dirs` = the war plugin's `warSourceDirectory` / `<webResources>` dirs (`webapp_dirs`). A classpath
    resource under a custom root stays in the comp module (restructure)."""
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
    """정적 웹 파일을 (웹 루트, 경로) 목록으로 반환한다. 작업공간을 한 번만 순회한다."""
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
# maven-war-plugin이 선언한 웹 루트
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WebDir:
    """WAR가 웹 콘텐츠를 가져오는 디렉토리를 담는 클래스. 경로는 pom 디렉토리 기준이다.
    ``kind``가 ``root``면 warSourceDirectory, ``resources``면 ``<webResources>``다."""
    dir: str
    kind: str = "root"          # root는 warSourceDirectory나 기본 webapp, resources는 <webResources>
    target: str = ""            # <webResources>의 targetPath. 빈 문자열이면 웹 루트 바로 아래


def clean_pom_dir(raw: Optional[str], allow_parent: bool = False) -> Optional[str]:
    """pom에 쓰인 디렉토리 값을 상대 경로로 바꾼다. 앞의 ``${basedir}/``는 뗀다.
    비었거나, 다른 ``${…}``가 있거나, 절대 경로나 ``..``으로 올라가는 경로면 None을 반환한다.
    ``allow_parent``가 True면 ``../../STATIC``처럼 pom 밖을 가리키는 경로도 반환한다.

    With `allow_parent` (the restructure) the caller decides whether the path still lies inside the project;
    a bare `..` stays None."""
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
    """pom 텍스트의 XML 루트를 반환한다. 읽지 못하면 None이다.
    expat은 EUC-KR 같은 멀티바이트 선언을 거부한다. 그래서 XML 선언을 떼고 파싱한다."""
    import xml.etree.ElementTree as ET

    try:
        return ET.fromstring(xml_without_declaration(text))
    except Exception:  # noqa: BLE001 - 파싱하지 못한 pom은 선언이 없는 것으로 본다
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
    """pom 하나에서 maven-war-plugin이 선언한 웹 디렉토리를 중복 없이 반환한다.
    ``<warSourceDirectory>``와 ``<webResources>``를 본다. profile 안은 보지 않는다.
    ``STATIC/`` 같은 회사의 비표준 웹 루트를 찾는 데 쓴다.

    Order: `<build><plugins>`, then pluginManagement. Paths are relative to the pom's directory;
    `allow_parent` as in `clean_pom_dir`."""
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
    """``<build>``가 컴파일하고 패키징하는 클래스패스 디렉토리 목록을 반환한다. 테스트 디렉토리는 뺀다.
    sourceDirectory, scriptSourceDirectory, ``<resources>``의 모든 디렉토리다.
    ``<resources>``가 없으면 Maven 기본값 ``src/main/resources``를 쓴다.

    Paths are relative to the pom's directory; `allow_parent` as in `clean_pom_dir`."""
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
    """루트 기준 (웹 디렉토리, 클래스패스 디렉토리) 목록을 반환한다.
    웹 디렉토리는 pom마다 실제로 있는 ``src/main/webapp``과 war 플러그인이 선언한 디렉토리다.
    클래스패스 디렉토리는 :func:`pom_build_dirs`의 결과다."""
    web: list[str] = []
    res: list[str] = []
    root = Path(ws.root)
    for p in poms:
        base = posixpath.dirname(p.path.replace("\\", "/"))
        try:
            text = read_pom_text(ws, p.path)        # cp949 pom도 깨지지 않게 감지한 인코딩으로 읽는다
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
    """``dirs`` 아래에서 5 MB 이상인 미디어, 압축 파일을 (경로, 바이트) 목록으로 반환한다.
    이런 파일이 jar에 그대로 들어간다고 알릴 때 쓴다."""
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
    """import, 스크립틀릿, useBean에서 javax EE 타입을 쓰는 JSP와 태그 파일 목록을 반환한다.

    The javax EE types are those of the jakarta table (`sbmig.analyze.jsp`)."""
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
    """프로젝트 전체를 검사해서 ScanResult를 반환한다.
    ``poms``를 주면 war 플러그인이 선언한 비표준 디렉토리도 웹 루트로 본다.
    이때 웹 루트와 리소스 디렉토리 아래의 큰 미디어 파일도 모은다."""
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
    # beans 네임스페이스와 <beans가 모두 있는 XML을 Spring XML 빈 설정으로 본다. spring_legacy 판단에 쓴다
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
    """pom 없이 검사해서 바로 SourceStats로 집계한다."""
    return scan_sources(ws).stats()
