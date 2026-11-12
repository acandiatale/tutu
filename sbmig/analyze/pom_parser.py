"""저장소 안의 pom.xml만 읽어 PomInfo 목록을 만드는 모듈.
네트워크나 mvn을 쓰지 않고, 네임스페이스가 있든 없든 똑같이 읽는다.
분석의 첫 단계(``analyze_project`` -> :func:`parse_poms` -> :func:`module_order`)이고, 집계 pom 계획, 권고
검증, 구조 검토, 인수인계 보고서도 호출한다.
- 1단계에서 pom마다 원시 파싱을 하고, 2단계에서 저장소 안 부모 체인을 연결한 뒤 속성 치환, 관리 버전 채우기,
  Java / Boot / Spring 버전 도출을 한다
- 저장소 밖 부모(사내 ``base-parent`` 등)와 import된 BOM은 읽지 않으므로 거기서 오는 값은 알 수 없다
- 깨진 pom은 경고를 남기고 건너뛴다. 예외로 멈추지 않는다

Maven pom graph parser (namespace-aware, no network, no mvn).

Two passes:
1. parse every pom.xml into a `_RawPom` (own coordinates, parent, properties, deps, depMgmt, plugins, modules);
2. link the in-repo parent chain (relativePath + GAV, then GAV against all repo poms), compute effective
   properties (parent chain + own + well-known project.* keys), interpolate `${prop}` and fill dependency
   versions from the effective dependencyManagement, then derive java / Boot / Spring versions.


"""
from __future__ import annotations

import codecs
import logging
import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

from sbmig.core.models import GAV, PluginInfo, PomInfo
from sbmig.core.workspace import DEFAULT_ENCODING_CANDIDATES, is_build_output_path

log = logging.getLogger("sbmig.analyze")

SKIP_DIR_PARTS = {"node_modules", ".git", ".idea", ".svn"}     # + Maven build output (`target/` beside a pom)
BOOT_GROUP = "org.springframework.boot"
BOOT_PARENTS = {"spring-boot-starter-parent", "spring-boot-parent"}
SPRING_CORE_ARTIFACTS = ("spring-core", "spring-context", "spring-webmvc", "spring-web", "spring-beans")
COMPILER_PLUGIN = "maven-compiler-plugin"
_PROP_RE = re.compile(r"\$\{([^}]+)\}")


class WorkspaceView(Protocol):
    """분석기가 쓰는 ``Workspace``의 최소 기능(root, glob, read, pom_files)을 정의한 프로토콜.
    실행 작업공간과 PathView 모두 해당한다.

    The subset of `Workspace` the analyzer needs."""
    root: Any

    def glob(self, pattern: str) -> list[str]: ...
    def read(self, rel: Any, encoding: str = "utf-8") -> str: ...
    def pom_files(self) -> list[str]: ...


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def is_skipped_path(rel: str) -> bool:
    """분석에서 뺄 경로인지 확인한다.
    node_modules, .git, .idea, .svn, Maven 빌드 출력(``target/``), ``src/test/resources``가 해당한다.
    ``src/test/resources``는 테스트용 pom을 프로젝트 모듈로 잘못 보지 않기 위해 뺀다."""
    parts = rel.replace("\\", "/").split("/")
    if any(p in SKIP_DIR_PARTS for p in parts) or is_build_output_path(rel):
        return True
    return "src/test/resources" in rel.replace("\\", "/")


def _local(tag: Any) -> str:
    """``{네임스페이스}이름``에서 이름만 반환한다. 네임스페이스가 없는 pom도 똑같이 읽기 위해 쓴다."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _child(el: Optional[ET.Element], name: str) -> Optional[ET.Element]:
    if el is None:
        return None
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el: Optional[ET.Element], name: str) -> list[ET.Element]:
    if el is None:
        return []
    return [c for c in el if _local(c.tag) == name]


def _text(el: Optional[ET.Element], name: str) -> Optional[str]:
    c = _child(el, name)
    if c is None or c.text is None:
        return None
    t = c.text.strip()
    return t or None


def _path(el: Optional[ET.Element], *names: str) -> Optional[ET.Element]:
    """자식 요소 이름을 차례로 따라 내려간 요소를 반환한다. 중간에 없으면 None을 반환한다."""
    cur = el
    for n in names:
        cur = _child(cur, n)
        if cur is None:
            return None
    return cur


def interpolate(value: Optional[str], props: dict[str, str], depth: int = 0) -> Optional[str]:
    """``${key}``를 ``props[key]``로 치환한다.
    순환 참조를 막기 위해 최대 깊이 10까지만 재귀하고, 모르는 키는 그대로 둔다.

    Replace `${key}` with props[key] (recursively, max depth 10); unknown keys are left in place."""
    if value is None or "${" not in value or depth > 10:
        return value

    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        return props[key] if key in props else m.group(0)

    new = _PROP_RE.sub(repl, value)
    if new == value:
        return new
    return interpolate(new, props, depth + 1)


def _flat_config(el: Optional[ET.Element]) -> dict[str, Any]:
    """플러그인 ``<configuration>``을 1단계 딕셔너리로 바꾼다.
    자식이 모두 단순 값이면 목록으로 만들고 더 깊은 구조는 버린다."""
    out: dict[str, Any] = {}
    if el is None:
        return out
    for c in el:
        name = _local(c.tag)
        if not name:
            continue
        kids = [k for k in c if _local(k.tag)]
        if not kids:
            out[name] = (c.text or "").strip()
        elif all(len(list(k)) == 0 for k in kids):
            out[name] = [(k.text or "").strip() for k in kids]
    return out


# ---------------------------------------------------------------------------
# raw parse
# ---------------------------------------------------------------------------
@dataclass
class _RawDep:
    group_id: str
    artifact_id: str
    version: Optional[str]
    scope: Optional[str]
    type: Optional[str]
    classifier: Optional[str]
    optional: bool


@dataclass
class _RawPlugin:
    group_id: str
    artifact_id: str
    version: Optional[str]
    configuration: dict[str, Any]
    managed: bool


@dataclass
class _RawPom:
    """pom 하나를 치환 전 상태로 파싱한 결과를 담는 클래스.
    부모 연결(``parent_raw``)과 유효 속성은 2단계에서 채운다."""
    path: str
    group_id: Optional[str]
    artifact_id: str
    version: Optional[str]
    packaging: str
    parent: Optional[GAV]
    parent_relative_path: Optional[str]     # None = element absent (default ../pom.xml); "" = explicit empty
    modules: list[str]
    properties: dict[str, str]
    deps: list[_RawDep]
    managed: list[_RawDep]
    plugins: list[_RawPlugin]
    parent_raw: Optional["_RawPom"] = None
    effective_props: dict[str, str] = field(default_factory=dict)
    repositories: list[str] = field(default_factory=list)   # <repository>/<pluginRepository> urls as written

    @property
    def eff_group(self) -> Optional[str]:
        return self.group_id or (self.parent.group_id if self.parent else None)

    @property
    def eff_version(self) -> Optional[str]:
        return self.version or (self.parent.version if self.parent else None)

    @property
    def dir(self) -> str:
        d = posixpath.dirname(self.path)
        return d


def _parse_dep(el: ET.Element) -> Optional[_RawDep]:
    """``<dependency>`` 하나를 파싱한다. artifactId가 없으면 None을 반환한다."""
    g, a = _text(el, "groupId"), _text(el, "artifactId")
    if not a:
        return None
    return _RawDep(group_id=g or "", artifact_id=a, version=_text(el, "version"), scope=_text(el, "scope"),
                   type=_text(el, "type"), classifier=_text(el, "classifier"),
                   optional=(_text(el, "optional") or "").lower() == "true")


def _parse_plugins(container: Optional[ET.Element], managed: bool) -> list[_RawPlugin]:
    """``<plugins>``의 플러그인 목록을 파싱한다.
    groupId가 없으면 Maven 기본값 ``org.apache.maven.plugins``를 쓴다."""
    out = []
    for p in _children(_child(container, "plugins"), "plugin"):
        a = _text(p, "artifactId")
        if not a:
            continue
        out.append(_RawPlugin(group_id=_text(p, "groupId") or "org.apache.maven.plugins", artifact_id=a,
                              version=_text(p, "version"), configuration=_flat_config(_child(p, "configuration")),
                              managed=managed))
    return out


_REPO_URL_RE = re.compile(r"<(repository|pluginRepository)>(?:(?!</\1>).)*?<url>\s*([^<]+?)\s*</url>(?:(?!</\1>).)*?</\1>",
                          re.S)
_XML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def repository_urls(text: str) -> list[str]:
    """pom 텍스트의 ``<repository>``와 ``<pluginRepository>`` URL을 순서대로, 중복 없이 반환한다.
    profile 안의 것도 포함하고 주석과 distributionManagement는 제외한다.
    폐쇄망에서 접속할 수 없는 저장소를 찾는 데 쓴다.

    URLs of every `<repository>` / `<pluginRepository>` in the pom text (profiles included, comments ignored,
    `<distributionManagement>` excluded), in order, de-duplicated. These are the repositories Maven contacts in
    addition to the settings — the ones an air-gapped host cannot reach (RUNBOOK §0.1)."""
    body = _XML_COMMENT_RE.sub("", text)
    body = re.sub(r"<distributionManagement>.*?</distributionManagement>", "", body, flags=re.S)
    out: list[str] = []
    for m in _REPO_URL_RE.finditer(body):
        url = m.group(2).strip()
        if url and url not in out:
            out.append(url)
    return out


# ---------------------------------------------------------------------------
# encoding / XML declaration (A1)
# ---------------------------------------------------------------------------
_XML_DECL_RE = re.compile(r"^\s*<\?xml\b[^>]*\?>")
_DECL_ENCODING_RE = re.compile(rb"^\s*<\?xml\b[^>]*?\bencoding\s*=\s*[\"']([A-Za-z0-9._:-]+)[\"']")
# Python 코덱 목록에 없는 Java 인코딩 이름을 Python 코덱으로 바꾸는 표
# Java charset names Python's codec registry does not know
_JAVA_ENCODING_ALIASES = {"x-windows-949": "cp949", "windows-949": "cp949", "x-euc-kr": "euc_kr"}
# cp949는 EUC-KR의 상위 집합이므로 한글 계열은 모두 cp949로 읽는다.
# 선언은 EUC-KR인데 확장 한글이 cp949로 저장된 pom도 깨지지 않는다
_KOREAN_CODECS = {"euc_kr", "cp949"}


def xml_without_declaration(text: str) -> str:
    """디코딩된 XML 텍스트에서 BOM과 맨 앞의 XML 선언을 뗀다.
    expat은 EUC-KR / MS949 같은 멀티바이트 선언을 거부하고 x-windows-949 같은 이름에 LookupError를 던지므로,
    UTF-8 바이트로 넘기기 전에 선언을 없앤다.

    Drop a leading BOM and the XML declaration from already-decoded XML text, so that its `encoding=` cannot
    contradict the UTF-8 bytes handed to the parser (expat rejects multi-byte declarations such as EUC-KR / MS949
    and raises LookupError on names it does not know, e.g. x-windows-949)."""
    return _XML_DECL_RE.sub("", text.lstrip("\ufeff"), count=1)


def xml_codec(name: str | None) -> str | None:
    """XML 선언의 encoding 이름에 맞는 Python 코덱 이름을 반환한다.
    모르는 이름이거나 빈 값이면 None을 반환한다.
    Java 이름(``x-windows-949``)도 받고, 한글 계열은 상위 집합인 ``cp949``를 반환한다.

    Python codec for an XML `encoding=` name (None when unknown). Java names such as x-windows-949 are accepted;
    the Korean family (EUC-KR, KSC5601, MS949, CP949) maps to its superset cp949."""
    if not name:
        return None
    n = name.strip().lower()
    try:
        codec = codecs.lookup(_JAVA_ENCODING_ALIASES.get(n, n)).name
    except LookupError:
        return None
    return "cp949" if codec in _KOREAN_CODECS else codec


def pom_encoding(data: bytes, candidates: Iterable[str] = DEFAULT_ENCODING_CANDIDATES) -> str:
    """pom 바이트를 읽을 인코딩을 정한다. BOM -> 엄격한 UTF-8 -> XML 선언의 인코딩 -> ``candidates`` 순서로
    파일 전체를 오류 없이 읽는 첫 인코딩을 쓰고, 모두 실패하면 ``utf-8``을 반환한다.
    UTF-8을 선언보다 먼저 보는 것은 ``Workspace.read``와 순서를 맞춰 분석이 읽은 인코딩과 재구성이 쓰는
    인코딩이 같도록 하기 위해서다.

    Encoding to decode pom bytes with: BOM (UTF-8 → "utf-8-sig", UTF-16); strict UTF-8 → "utf-8"; else the
    declared encoding (Korean family → cp949) when it decodes the whole file strictly; else the first of
    `candidates` that does; "utf-8" as the last resort (read with replacement characters)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    m = _DECL_ENCODING_RE.match(data)
    declared = xml_codec(m.group(1).decode("ascii")) if m else None
    order: list[str] = []
    for enc in ("utf-8", declared, *candidates):
        if enc and enc not in order:
            order.append(enc)
    for enc in order:
        try:
            data.decode(enc)                 # strict
        except (UnicodeDecodeError, LookupError):
            continue
        return enc
    return "utf-8"


def read_pom_text(ws: WorkspaceView, rel: str) -> str:
    """pom 바이트로 :func:`pom_encoding`을 정한 뒤 그 인코딩으로 pom 텍스트를 읽는다.
    바이트를 읽지 못하면 ``ws.read(rel)``을 쓴다.
    ``PathView``의 기본 utf-8 읽기는 cp949 pom의 한글을 깨뜨리므로 쓰지 않는다.

    Text of a pom, decoded with `pom_encoding` of its bytes (the workspace's candidate order when it has one);
    falls back to `ws.read(rel)` when the bytes cannot be read."""
    try:
        data = (Path(ws.root) / rel).read_bytes()
    except (OSError, TypeError, ValueError):
        return ws.read(rel)
    cands = tuple(getattr(ws, "encoding_candidates", None) or DEFAULT_ENCODING_CANDIDATES)
    return ws.read(rel, encoding=pom_encoding(data, cands))


def parse_pom_text(text: str | bytes, path: str) -> _RawPom:
    """pom 하나를 원시 파싱한다.
    XML이 깨졌거나 ``<project>``가 아니거나 artifactId가 없으면 ValueError를 던진다.

    Parse one pom; raises ValueError on malformed XML or a missing artifactId."""
    # 인코딩 선언이 UTF-8 바이트와 어긋나면 expat이 오류를 내므로, 디코딩된 텍스트에서 선언을 떼고 UTF-8로 넘긴다
    if isinstance(text, (bytes, bytearray)):
        data = bytes(text)
        text = data.decode(pom_encoding(data), errors="replace")
    try:
        root = ET.fromstring(xml_without_declaration(text).encode("utf-8"))
    except ET.ParseError as e:
        raise ValueError(f"{path}: not well-formed XML ({e})") from e
    if _local(root.tag) != "project":
        raise ValueError(f"{path}: root element is <{_local(root.tag)}>, expected <project>")
    artifact = _text(root, "artifactId")
    if not artifact:
        raise ValueError(f"{path}: missing <artifactId>")
    parent_el = _child(root, "parent")
    parent = None
    rel_path: Optional[str] = None
    if parent_el is not None:
        pg, pa = _text(parent_el, "groupId"), _text(parent_el, "artifactId")
        if pa:
            parent = GAV(group_id=pg or "", artifact_id=pa, version=_text(parent_el, "version"))
        rp = _child(parent_el, "relativePath")
        if rp is not None:
            rel_path = (rp.text or "").strip()
    props: dict[str, str] = {}
    props_el = _child(root, "properties")
    for c in (list(props_el) if props_el is not None else []):
        name = _local(c.tag)
        if name:
            props[name] = (c.text or "").strip()
    deps = [d for d in (_parse_dep(e) for e in _children(_child(root, "dependencies"), "dependency")) if d]
    managed = [d for d in (_parse_dep(e) for e in
                           _children(_path(root, "dependencyManagement", "dependencies"), "dependency")) if d]
    build = _child(root, "build")
    plugins = _parse_plugins(build, managed=False) + _parse_plugins(_child(build, "pluginManagement"), managed=True)
    modules = [(m.text or "").strip() for m in _children(_child(root, "modules"), "module") if (m.text or "").strip()]
    return _RawPom(path=path, group_id=_text(root, "groupId"), artifact_id=artifact, version=_text(root, "version"),
                   packaging=_text(root, "packaging") or "jar", parent=parent, parent_relative_path=rel_path,
                   modules=modules, properties=props, deps=deps, managed=managed, plugins=plugins)


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------
def find_pom_files(ws: WorkspaceView) -> list[str]:
    """작업공간의 pom.xml 경로를 정렬해서 반환한다.
    건너뛸 경로와 접수가 제외한 폴더(``ws.ignored_dirs``)는 뺀다."""
    try:
        files = ws.pom_files()
    except Exception:  # pragma: no cover - defensive
        files = ws.glob("pom.xml")
    ignored = getattr(ws, "is_ignored", None)
    return sorted(f.replace("\\", "/") for f in files
                  if not is_skipped_path(f) and not (callable(ignored) and ignored(f)))


def _module_pom_path(pom_path: str, module: str) -> str:
    """``<module>`` 값을 그 모듈 pom의 프로젝트 상대 경로로 바꾼다(``a/b``는 ``a/b/pom.xml``)."""
    base = posixpath.dirname(pom_path)
    target = posixpath.normpath(posixpath.join(base or ".", module.strip().replace("\\", "/").rstrip("/")))
    if not target.endswith(".xml"):
        target = posixpath.join(target, "pom.xml")
    return posixpath.normpath(target)


@dataclass
class StrayPom:
    """어느 pom의 ``<modules>``에도 없으면서 다른 pom과 groupId:artifactId가 같은 pom을 나타내는 클래스.
    IDE 출력 폴더나 백업 폴더에 든 모듈 복사본이다.
    접수가 분석, 구조 검토, 재구성에서 빼고 인수인계에 기록한다. 이 pom을 모듈로 등록하면 Maven이
    "is duplicated in the reactor"로 reactor 전체를 거부하기 때문이다.

    A pom no `<modules>` lists whose coordinates duplicate another pom's: a stray copy of a module (an IDE output or
    backup folder holding src + .project + pom.xml). Ignored by every scan, named in the Handover."""
    path: str                 # the ignored pom (e.g. boot/bin/pom.xml)
    duplicate_of: str         # the pom it copies (e.g. boot/pom.xml)
    coordinates: str          # "groupId:artifactId" (":artifactId" when neither pom has a group)
    nested: bool              # its directory lies under duplicate_of's directory

    @property
    def dir(self) -> str:
        return posixpath.dirname(self.path)

    def why(self) -> str:
        """경고, 저널, 인수인계에 함께 쓰는 짧은 영문 이유를 반환한다."""
        where = "inside the directory of" if self.nested else "beside the module"
        return (f"`{self.path}` has the same coordinates as `{self.duplicate_of}` (`{self.coordinates}`), lies {where} "
                f"`{self.duplicate_of}` and no pom lists it as a module")


def stray_pom_copies(raws: list[_RawPom]) -> list[StrayPom]:
    """파싱한 pom 중 모듈 복사본을 찾아 반환한다.
    루트가 아니고 어디에도 나열되지 않은 pom이 다른 pom과 좌표가 같고, 그 pom의 폴더 안에 있거나 그 pom이
    reactor에 포함되면 복사본으로 본다.
    둘 다 나열되지 않은 형제 pom과 이미 나열된 pom은 여기서 빼지 않는다.

    Stray copies among the parsed poms: an UNLISTED, non-root pom whose effective groupId:artifactId equals another
    pom's, when it is nested under that pom's directory or that pom is reactor-reachable (root or listed). Two
    unlisted siblings with equal coordinates (a rootless tree) are left alone; a LISTED duplicate is never dropped here
    (that is `reactor_duplicate` / the review's invariant check)."""
    referenced = {_module_pom_path(r.path, m) for r in raws for m in r.modules}
    by_key: dict[str, list[_RawPom]] = {}
    for r in raws:
        by_key.setdefault(f"{r.eff_group or ''}:{r.artifact_id}", []).append(r)
    out: list[StrayPom] = []
    for r in raws:
        if r.path == "pom.xml" or r.path in referenced or not r.artifact_id:
            continue
        key = f"{r.eff_group or ''}:{r.artifact_id}"
        others = [q for q in by_key[key] if q is not r]
        if not others:
            continue
        nested = [q for q in others if q.path != r.path and (q.dir == "" or r.dir.startswith(q.dir + "/"))]
        reachable = [q for q in others if q.path == "pom.xml" or q.path in referenced]
        pick = (sorted(nested, key=lambda q: -len(q.dir)) or sorted(reachable, key=lambda q: q.path) or [None])[0]
        if pick is None:
            continue
        out.append(StrayPom(path=r.path, duplicate_of=pick.path, coordinates=key, nested=bool(nested)))
    return out


def _read_raw_poms(ws: WorkspaceView) -> tuple[list[_RawPom], list[str]]:
    """작업공간의 pom을 원시 파싱해서 (원시 목록, 경고)를 반환한다."""
    warnings: list[str] = []
    raws: list[_RawPom] = []
    for rel in find_pom_files(ws):
        try:
            # cp949 pom의 한글 속성 값이 깨지지 않도록 감지한 인코딩으로 읽는다
            text = read_pom_text(ws, rel)
            raw = parse_pom_text(text, rel)
            raw.repositories = repository_urls(text)
            raws.append(raw)
        except (ValueError, OSError, LookupError) as e:
            msg = f"pom skipped (parse failure): {e}"
            log.warning(msg)
            warnings.append(msg)
    return raws, warnings


def find_stray_pom_copies(ws: WorkspaceView) -> list[StrayPom]:
    """작업공간에서 모듈 복사본 pom을 찾는다. 접수가 호출하며 파일을 바꾸지 않는다.

    The stray pom copies of the workspace (read-only; the intake calls it, see `stray_pom_copies`)."""
    raws, _warnings = _read_raw_poms(ws)
    return stray_pom_copies(raws)


def _resolve_parent(raw: _RawPom, by_path: dict[str, _RawPom], all_raw: list[_RawPom]) -> Optional[_RawPom]:
    """저장소 안에서 ``raw``의 부모 pom을 찾는다. 없으면 외부 부모로 보고 None을 반환한다.
    Maven처럼 relativePath(기본 ``../pom.xml``)를 먼저 확인하고, 맞지 않으면 저장소 전체에서 좌표로 찾는다."""
    if raw.parent is None:
        return None

    def gav_match(c: _RawPom) -> bool:
        """``c``가 선언된 부모 좌표와 모순되지 않는지 확인한다. 그룹과 버전은 둘 다 값이 있을 때만 비교한다."""
        if c is raw or c.artifact_id != raw.parent.artifact_id:  # type: ignore[union-attr]
            return False
        if raw.parent.group_id and c.eff_group and c.eff_group != raw.parent.group_id:  # type: ignore[union-attr]
            return False
        pv, cv = raw.parent.version, c.eff_version  # type: ignore[union-attr]
        if pv and cv and "${" not in pv and "${" not in cv and pv != cv:
            return False
        return True

    rel = raw.parent_relative_path
    # 요소가 없으면 Maven 기본값 ``../pom.xml``을 쓰고, 빈 ``<relativePath/>``이면 경로 확인 없이 좌표로만 찾는다
    if rel is None:
        rel = "../pom.xml"
    if rel:
        cand = posixpath.normpath(posixpath.join(raw.dir or ".", rel))
        if not cand.endswith(".xml"):
            cand = posixpath.join(cand, "pom.xml")
        cand = posixpath.normpath(cand)
        c = by_path.get(cand)
        if c is not None and gav_match(c):
            return c
    matches = [c for c in all_raw if gav_match(c)]
    return matches[0] if matches else None


def _chain(raw: _RawPom) -> list[_RawPom]:
    """최상위 조상부터 ``raw``까지의 부모 체인을 반환한다. 순환이 있어도 멈춘다.

    [top-most ancestor, ..., raw] (cycle safe)."""
    out: list[_RawPom] = []
    seen: set[int] = set()
    cur: Optional[_RawPom] = raw
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        out.append(cur)
        cur = cur.parent_raw
    return list(reversed(out))


def _effective_props(raw: _RawPom) -> dict[str, str]:
    """부모 체인 위에서부터 덮어쓴 ``<properties>``에 ``project.*``, ``parent.*`` 키를 더한 유효 속성을 반환한다.
    ``basedir``은 "."이고, 마지막에 속성끼리의 참조를 한 번 치환한다."""
    props: dict[str, str] = {}
    for p in _chain(raw):
        props.update(p.properties)
    if raw.eff_version:
        props["project.version"] = raw.eff_version
        props["pom.version"] = raw.eff_version
    if raw.eff_group:
        props["project.groupId"] = raw.eff_group
    props["project.artifactId"] = raw.artifact_id
    if raw.parent:
        if raw.parent.version:
            props["project.parent.version"] = raw.parent.version
            props["parent.version"] = raw.parent.version
        if raw.parent.group_id:
            props["project.parent.groupId"] = raw.parent.group_id
    props.setdefault("project.basedir", ".")
    props.setdefault("basedir", ".")
    # resolve self-references once so lookups are cheap
    return {k: interpolate(v, props) or "" for k, v in props.items()}


def _to_gav(d: _RawDep, props: dict[str, str], version: Optional[str] = None) -> GAV:
    """원시 의존성을 속성 치환한 GAV로 바꾼다."""
    return GAV(group_id=interpolate(d.group_id, props) or "", artifact_id=interpolate(d.artifact_id, props) or "",
               version=interpolate(version if version is not None else d.version, props),
               scope=interpolate(d.scope, props), type=interpolate(d.type, props),
               classifier=interpolate(d.classifier, props), optional=d.optional)


def _effective_managed(raw: _RawPom) -> dict[str, GAV]:
    """부모 체인 전체의 dependencyManagement를 합쳐 반환한다. 아래 pom이 위를 덮어쓴다.
    저장소 밖 부모와 import된 BOM의 관리 버전은 포함하지 않는다."""
    out: dict[str, GAV] = {}
    for p in _chain(raw):
        for d in p.managed:
            g = _to_gav(d, p.effective_props)
            out[g.key] = g
    return out


def _effective_plugin_mgmt(raw: _RawPom) -> dict[str, tuple[Optional[str], dict[str, Any]]]:
    """부모 체인의 플러그인 선언을 ``group:artifact``별 (버전, 합친 configuration)으로 모은다.
    자식의 버전 없는 플러그인을 채우는 데 쓴다."""
    out: dict[str, tuple[Optional[str], dict[str, Any]]] = {}
    for p in _chain(raw):
        for pl in p.plugins:
            key = f"{pl.group_id}:{pl.artifact_id}"
            ver = interpolate(pl.version, p.effective_props)
            prev = out.get(key)
            cfg = {**(prev[1] if prev else {}), **pl.configuration}
            out[key] = (ver or (prev[0] if prev else None), cfg)
    return out


def _clean_version(v: Optional[str]) -> Optional[str]:
    """버전을 반환한다. 풀리지 않은 ``${...}``가 남아 있으면 None을 반환한다."""
    if not v or "${" in v:
        return None
    return v


def build_pom_infos(raws: list[_RawPom]) -> list[PomInfo]:
    """원시 pom 목록으로 PomInfo 목록을 만들어 입력 순서대로 반환한다.
    Java / Boot / Spring 버전을 자기 pom에서 찾지 못하면 저장소 안 부모의 값을 쓴다."""
    by_path = {r.path: r for r in raws}
    for r in raws:
        r.parent_raw = _resolve_parent(r, by_path, raws)
    # effective props must be computed top-down
    for r in sorted(raws, key=lambda x: len(_chain(x))):
        r.effective_props = _effective_props(r)

    infos: dict[str, PomInfo] = {}
    for r in sorted(raws, key=lambda x: len(_chain(x))):
        props = r.effective_props
        managed = _effective_managed(r)
        deps: list[GAV] = []
        for d in r.deps:
            g = _to_gav(d, props)
            if not g.version and g.key in managed:
                g.version = managed[g.key].version
                if not g.scope and managed[g.key].scope:
                    g.scope = managed[g.key].scope
            deps.append(g)
        dm = [_to_gav(d, props) for d in r.managed]
        mgmt = _effective_plugin_mgmt(r)
        plugins: list[PluginInfo] = []
        for pl in r.plugins:
            key = f"{pl.group_id}:{pl.artifact_id}"
            ver = interpolate(pl.version, props)
            conf = {k: (interpolate(v, props) if isinstance(v, str) else v) for k, v in pl.configuration.items()}
            if not pl.managed and key in mgmt:
                ver = ver or mgmt[key][0]
                conf = {**mgmt[key][1], **conf}
            plugins.append(PluginInfo(group_id=pl.group_id, artifact_id=pl.artifact_id, version=ver,
                                      configuration=conf))
        parent_info = infos.get(r.parent_raw.path) if r.parent_raw else None
        info = PomInfo(
            path=r.path,
            group_id=r.eff_group,
            artifact_id=r.artifact_id,
            version=interpolate(r.eff_version, props),
            packaging=interpolate(r.packaging, props) or "jar",
            parent=r.parent,
            parent_relative_path=r.parent_relative_path,
            parent_in_repo=r.parent_raw is not None,
            modules=list(r.modules),
            properties=dict(r.properties),
            dependencies=deps,
            dependency_management=dm,
            repositories=[interpolate(u, props) or u for u in r.repositories],
            plugins=plugins,
            # ``<build><plugins>``에 있어야 Boot 앱이므로 pluginManagement에만 있는 Boot 플러그인은 세지 않는다
            has_spring_boot_plugin=any(p.group_id == BOOT_GROUP and p.artifact_id == "spring-boot-maven-plugin"
                                       for p, raw_pl in zip(plugins, r.plugins) if not raw_pl.managed),
            is_root=r.parent_raw is None,
        )
        info.is_aggregator = info.packaging == "pom" and bool(info.modules)
        info.java_version = _java_version(props, plugins, r.plugins) or (parent_info.java_version if parent_info else None)
        info.spring_boot_version = _boot_version(r, props, deps, dm) or (
            parent_info.spring_boot_version if parent_info else None)
        info.spring_version = _spring_version(props, deps, managed) or (
            parent_info.spring_version if parent_info else None)
        infos[r.path] = info
    return [infos[r.path] for r in raws]


def _java_version(props: dict[str, str], plugins: list[PluginInfo], raw_plugins: list[_RawPlugin]) -> Optional[str]:
    """Java 버전을 구한다. maven-compiler-plugin의 release / source / target을 먼저 보고, 없으면
    ``maven.compiler.*``와 ``java.version`` 속성을 본다."""
    for p in plugins:
        if p.artifact_id == COMPILER_PLUGIN:
            for k in ("release", "source", "target"):
                v = p.configuration.get(k)
                if isinstance(v, str) and _clean_version(v):
                    return v
    for k in ("maven.compiler.release", "maven.compiler.source", "maven.compiler.target", "java.version"):
        v = _clean_version(props.get(k))
        if v:
            return v
    return None


def _boot_version(r: _RawPom, props: dict[str, str], deps: list[GAV], dm: list[GAV]) -> Optional[str]:
    """Boot 버전을 구한다.
    Boot 부모, spring-boot-dependencies BOM, ``spring-boot.version`` 속성, Boot 의존성 순으로 본다."""
    if r.parent and r.parent.group_id == BOOT_GROUP and r.parent.artifact_id in BOOT_PARENTS:
        v = _clean_version(interpolate(r.parent.version, props))
        if v:
            return v
    for g in dm:
        if g.group_id == BOOT_GROUP and g.artifact_id == "spring-boot-dependencies":
            v = _clean_version(g.version)
            if v:
                return v
    v = _clean_version(props.get("spring-boot.version"))
    if v:
        return v
    for g in deps:
        if g.group_id == BOOT_GROUP and g.artifact_id.startswith("spring-boot"):
            v = _clean_version(g.version)
            if v:
                return v
    return None


def _spring_version(props: dict[str, str], deps: list[GAV], managed: dict[str, GAV]) -> Optional[str]:
    """Spring Framework 버전을 구한다.
    속성(``spring-framework.version``, ``spring.version``), 핵심 spring-* 의존성, BOM 순으로 본다."""
    for k in ("spring-framework.version", "spring.version"):
        v = _clean_version(props.get(k))
        if v:
            return v
    for g in deps:
        if g.group_id == "org.springframework" and g.artifact_id in SPRING_CORE_ARTIFACTS:
            v = _clean_version(g.version)
            if v:
                return v
    bom = managed.get("org.springframework:spring-framework-bom")
    return _clean_version(bom.version) if bom else None


def parse_poms(ws: WorkspaceView) -> tuple[list[PomInfo], list[str]]:
    """작업공간의 모든 pom을 파싱해서 (PomInfo 목록, 경고)를 반환한다.
    파싱하지 못한 pom과 모듈 복사본은 경고를 남기고 뺀다.

    Parse every pom in the workspace. Returns (poms, warnings); unparsable poms are skipped with a warning. v2.1z-h:
    stray copies (`stray_pom_copies`) are left out with a `pom ignored (stray copy): …` warning — they are not
    modules, and every consumer (analysis, aggregator, structure review, restructure, Handover) must agree."""
    raws, warnings = _read_raw_poms(ws)
    strays = stray_pom_copies(raws)
    if strays:
        drop = {st.path for st in strays}
        raws = [r for r in raws if r.path not in drop]
        for st in strays:
            # parse_poms는 실행마다 여러 번 호출되므로 여기서는 DEBUG로 남기고, 접수가 INFO로 한 번 알린다
            msg = f"pom ignored (stray copy): {st.why()} — not a module"
            log.debug(msg)
            warnings.append(msg)
    return build_pom_infos(raws), warnings


def module_order(poms: Iterable[PomInfo]) -> list[str]:
    """pom 경로를 reactor와 비슷한 순서로 반환한다.
    루트 집계 pom에서 ``<modules>``를 따라 전위 순회하고, 어디에도 없는 pom은 경로순으로 뒤에 붙인다.

    Reactor-ish order of pom paths: pre-order DFS through <modules> from the root aggregator(s)."""
    poms = list(poms)
    by_path = {p.path: p for p in poms}
    referenced: set[str] = set()

    def module_path(p: PomInfo, m: str) -> str:
        base = posixpath.dirname(p.path)
        target = posixpath.normpath(posixpath.join(base or ".", m))
        if not target.endswith(".xml"):
            target = posixpath.join(target, "pom.xml")
        return posixpath.normpath(target)

    for p in poms:
        for m in p.modules:
            referenced.add(module_path(p, m))
    roots = sorted((p for p in poms if p.path not in referenced),
                   key=lambda p: (p.path.count("/"), p.path))
    out: list[str] = []
    seen: set[str] = set()

    def visit(p: PomInfo) -> None:
        if p.path in seen:
            return
        seen.add(p.path)
        out.append(p.path)
        for m in p.modules:
            child = by_path.get(module_path(p, m))
            if child is not None:
                visit(child)

    for r in roots:
        visit(r)
    for p in sorted(poms, key=lambda p: p.path):
        visit(p)
    return out


def pom_dir(path: str) -> str:
    """pom 경로의 디렉토리를 반환한다. 최상위 pom.xml이면 빈 문자열을 반환한다.

    Directory of a pom path ('' for the top-level pom.xml)."""
    return posixpath.dirname(path)


def owning_pom(file_path: str, poms: Iterable[PomInfo]) -> Optional[PomInfo]:
    """``file_path``가 속한 모듈, 즉 디렉토리가 가장 깊게 겹치는 pom을 반환한다.

    The pom whose directory is the deepest prefix of `file_path`."""
    best: Optional[PomInfo] = None
    best_len = -1
    for p in poms:
        d = pom_dir(p.path)
        if d == "" or file_path == d or file_path.startswith(d + "/"):
            if len(d) > best_len:
                best, best_len = p, len(d)
    return best
