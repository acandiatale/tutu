"""Ant 프로젝트(build.xml 만 있고 pom.xml 없음)에 pom.xml 을 합성한다(v2.1j, 2026-10-29 현업: "Ant 프로젝트도 Maven
으로 변경 후 마이그레이션"). 흐름상 위치: 접수의 ``_check_build_tool`` 이 도구가 ant 이고 ``intake.ant_to_maven``
이면 :func:`synthesize_pom` 을 부르고, 결과가 ok 면 커밋(``intake: pom.xml synthesized from build.xml``)한 뒤
여느 Maven 프로젝트처럼 분석부터 계속한다. 핵심 진입점: :func:`describe_ant` (build.xml + 폴더 구조 → 읽기 전용
:class:`AntProject`), :func:`synthesize_pom` (``<root>/pom.xml`` 하나만
씀 — 커밋·삭제 없음, build.xml 은 그대로). 지킬 것: 번역 못 하는 build.xml 작업(exec / java / 코드 생성 / 바깥
copy / 하위 빌드)은 ``build_xml_notes`` 에 한 줄씩; 좌표 없는 jar 는 쓰이면 system scope(``intake.unknown_jar``,
``${maven.multiModuleProjectDirectory}/<as-is 경로>`` — 재구성이 모듈로 옮길 때 복사·경로 수정), 안 쓰이면 뺌; 컨테이너
API 는 provided; Java 수준은 ``intake.min_java_source`` 로 올리되 ``<release>`` 는 쓰지 않음(source / target 유지).

Ant → Maven at intake (v2.1j, docs/INTERFACES.md "v2.1j").

`describe_ant(ws)` reads build.xml with its properties resolved (`<property name value|location>`, `<property file>`,
an unreferenced `build.properties` as fallback, `${basedir}`, nested `${a.b}`; `<property environment>` ignored;
unresolved references stay as they are + a warning), the `<javac>` tasks (srcdir lists, nested `<src>`, destdir,
source / target / encoding, classpath attribute / classpathref / nested `<classpath>` with pathelement / fileset /
refid → `<path id>`), the `<war>` / `<jar>` tasks (destfile, webxml, `<lib>`, `<classes>`, `<fileset>`), the
`<copy>` tasks (into the classes dir = resources), version hints, and the directories: source roots confirmed by
`package a.b;`, the web root (the `<war webxml>` WEB-INF parent, else a directory holding WEB-INF/web.xml outside
build output), lib dirs (classpath filesets, WEB-INF/lib, a root lib/). `synthesize_pom(ws, cfg)` resolves every
jar (`sbmig.analyze.jar_coords`) and writes `<root>/pom.xml` (UTF-8, 2-space indent); the only other files it writes
is nothing else: unknown-but-used jars stay where they are, declared with systemPath
`${maven.multiModuleProjectDirectory}/<as-is path>` (the restructure copies them into the module when it splits the project).
"""
from __future__ import annotations

import json
import logging
import posixpath
import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from sbmig.core.models import JarCoordinate, PomSynthesis
from sbmig.core.workspace import detect_encoding

from .build_tool import find_ant_build_file
from .edges import normalize_java_version
from .jar_coords import (
    REF_TEXT_EXT,
    JarScan,
    Sha1Lookup,
    apply_used,
    is_container_api,
    jar_name_table,
    references,
    resolve_jar_scan,
    stem_and_version,
)
from .pom_parser import pom_encoding, xml_without_declaration
from .source_scanner import scan_java_text
from .webapp_inventory import _source_root_of, _walk

log = logging.getLogger("sbmig.analyze.ant")

# 합성 pom 에는 부모가 없다(재구성이 나중에 base-parent 를 붙임): Maven 3.8 이하의 기본 maven-war-plugin 2.2 는
# JDK 16+ 에서 죽으므로 버전을 고정한다(3.4.0 = Maven 3.9 의 기본 바인딩). 컴파일러 플러그인은 선언하지 않는다 —
# maven.compiler.source / target 속성이면 JDK 21 이 -source 8 로 컴파일한다(<release> 는 쓰지 않음).
# the synthesized pom has no parent until the restructure: Maven ≤ 3.8 binds maven-war-plugin 2.2, which fails on
# JDK 16+ → pinned (3.4.0 = what Maven 3.9 binds). No compiler plugin: the maven.compiler.* properties are enough.
WAR_PLUGIN = ("org.apache.maven.plugins", "maven-war-plugin", "3.4.0")
BUILD_HELPER_PLUGIN = ("org.codehaus.mojo", "build-helper-maven-plugin", "3.6.1")
# 좌표 없는 jar 의 system scope 경로: as-is 위치를 최상위 pom 디렉토리 기준으로(복사 없음 — _system_jar_path;
# 재구성이 모듈로 옮길 때 알아서 복사). unknown-but-used jars: as-is path from the top-level pom directory, no copy
SYSTEM_LIB_DIR = "lib"
SYSTEM_PATH_BASE = "${maven.multiModuleProjectDirectory}"
POM_NS = ('xmlns="http://maven.apache.org/POM/4.0.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
          '         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd"')
OUTPUT_DIR_NAMES = ("build", "dist", "out", "bin", "classes", "deploy", "release")
WEB_ROOT_NAMES = ("WebContent", "WebRoot", "web", "webapp", "src/main/webapp", "webroot", "www", "public_html",
                  "docroot", "html")
TEST_TOKENS = frozenset({"test", "tests", "junit", "unittest", "testsrc", "unittests", "it"})
VERSION_PROPS = ("version", "app.version", "build.version", "project.version")
CONTAINER_TAGS = frozenset({"sequential", "parallel", "then", "else", "if", "trycatch", "try", "catch", "finally",
                            "for", "retry", "timed", "elseif"})
CODEGEN_TARGET_RE = re.compile(r"gen|wsdl|xjc|jaxb|idl|stub|codegen|axis|wsimport|castor|xmlbeans|jibx|hbm2java|"
                               r"javacc|antlr|cxf|proto|schema", re.IGNORECASE)
CODEGEN_TASKS = {
    "wsimport": "generates JAX-WS sources", "wsgen": "generates JAX-WS artifacts", "xjc": "generates JAXB sources",
    "schemagen": "generates XML schemas", "wsdl2java": "generates sources from WSDL",
    "axis-wsdl2java": "generates Axis sources from WSDL", "javacc": "generates a parser", "jjtree": "generates a parser",
    "antlr": "generates a parser", "idlj": "generates CORBA sources", "rmic": "generates RMI stubs",
    "jspc": "precompiles JSPs", "jasper": "precompiles JSPs", "jasper2": "precompiles JSPs",
    "native2ascii": "converts properties to \\uXXXX escapes (Maven copies them as they are)",
    "hibernatetool": "generates Hibernate code", "apt": "runs annotation processing",
}
JAVA_CHARSET = {"utf-8": "UTF-8", "utf-8-sig": "UTF-8", "utf_8": "UTF-8", "ascii": "UTF-8", "cp949": "MS949",
                "ms949": "MS949", "euc-kr": "EUC-KR", "euc_kr": "EUC-KR", "shift_jis": "Shift_JIS",
                "latin-1": "ISO-8859-1", "latin_1": "ISO-8859-1", "iso8859-1": "ISO-8859-1"}
# 소스가 쓰는데 프로젝트 안 어떤 jar 도 주지 않는 컨테이너 API(Ant 는 보통 ${tomcat.home}/lib 로 컴파일) → provided 추가
# container APIs the sources use that no project jar provides (Ant compiled against ${tomcat.home}/lib) → provided
IMPLIED_APIS = (
    ("javax.servlet", ("javax.servlet", "javax.servlet.http"), "javax.servlet", "javax.servlet-api", "3.1.0",
     frozenset({"javax.servlet:servlet-api", "javax.servlet:javax.servlet-api", "org.apache.tomcat:servlet-api",
                "org.apache.tomcat:tomcat-servlet-api"})),
    ("javax.servlet.jsp", ("javax.servlet.jsp",), "javax.servlet.jsp", "javax.servlet.jsp-api", "2.3.3",
     frozenset({"javax.servlet.jsp:jsp-api", "javax.servlet.jsp:javax.servlet.jsp-api", "org.apache.tomcat:jsp-api",
                "org.apache.tomcat:tomcat-jsp-api"})),
    ("javax.el", ("javax.el",), "javax.el", "javax.el-api", "3.0.0",
     frozenset({"javax.el:el-api", "javax.el:javax.el-api", "org.apache.tomcat:el-api",
                "org.apache.tomcat:tomcat-el-api"})),
)
MAX_NOTES = 30
MAX_UNRESOLVED_WARNINGS = 12

_PROP_RE = re.compile(r"\$\{([^}$]+)\}")
_SPLIT_LIST_RE = re.compile(r"[;,]")
_PATTERN_SPLIT_RE = re.compile(r"[,\s]+")


# ------------------------------------------------------------------------------------------------ model
@dataclass
class ClasspathEntry:
    """클래스패스 항목 하나: jar 파일, 디렉토리(클래스 폴더), fileset(dir + includes / excludes), 프로젝트 밖·못 푼
    경로(outside, 원문 그대로)."""
    kind: str                                  # jar | dir | fileset | outside
    path: str                                  # repo-relative ("" = root); the raw text for `outside`
    includes: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)


@dataclass
class AntJavac:
    """``<javac>`` 하나(속성은 풀린 값): 소스 디렉토리, 출력, source / target / encoding, 클래스패스, 테스트 여부."""
    target_name: str
    srcdirs: list[str] = field(default_factory=list)
    destdir: str | None = None
    source: str = ""
    java_target: str = ""
    encoding: str = ""
    classpath: list[ClasspathEntry] = field(default_factory=list)
    test: bool = False


@dataclass
class AntArchive:
    """``<war>`` / ``<jar>`` / ``<ear>`` 하나: 결과 파일, web.xml, 내용 디렉토리, lib 디렉토리, Main-Class."""
    kind: str                                  # war | jar | ear
    target_name: str
    destfile: str | None = None
    raw_destfile: str = ""
    webxml: str | None = None
    basedir: str | None = None
    lib_dirs: list[str] = field(default_factory=list)
    content_dirs: list[str] = field(default_factory=list)
    main_class: str = ""


@dataclass
class AntCopy:
    """``<copy>`` / ``<move>`` / ``<sync>`` 하나: 대상 디렉토리, 원본 fileset 들, 필터링 여부."""
    task: str
    target_name: str
    todir: str | None
    raw_todir: str
    filesets: list[ClasspathEntry] = field(default_factory=list)
    filtering: bool = False


@dataclass
class ResourceDir:
    """pom ``<resources>`` 의 항목 하나(디렉토리 + includes / excludes)."""
    dir: str
    includes: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)


@dataclass
class AntProject:
    """build.xml + 폴더 구조를 읽은 결과(읽기 전용, :func:`describe_ant`). ``error`` 가 있으면 합성할 수 없다
    (build.xml 없음 / 못 읽음 / XML 아님). 경로는 전부 저장소 상대 posix ("" = 루트).

    What build.xml and the directories say (read only). `error` set → no synthesis possible."""
    build_file: str = ""
    error: str | None = None
    name: str = ""
    basedir: str = ""
    default_target: str = ""
    properties: dict[str, str] = field(default_factory=dict)      # resolved values
    targets: list[str] = field(default_factory=list)
    javac: list[AntJavac] = field(default_factory=list)
    archives: list[AntArchive] = field(default_factory=list)
    copies: list[AntCopy] = field(default_factory=list)
    version: str = ""
    java_source: str = ""                      # as build.xml says (max over the main javac tasks), "" = not set
    java_target: str = ""
    encoding: str = ""                         # javac encoding (resolved), "" = not set
    source_encoding: str = ""                  # detected from the sources (Java charset name)
    source_roots: list[str] = field(default_factory=list)
    source_files: dict[str, int] = field(default_factory=dict)     # source root → .java count
    test_roots: list[str] = field(default_factory=list)
    resources: list[ResourceDir] = field(default_factory=list)
    web_root: str | None = None
    web_xml: str | None = None              # web.xml path when the war plugin must be told (not <web root>/WEB-INF)
    has_web_xml: bool = False
    jsp_count: int = 0
    lib_dirs: list[str] = field(default_factory=list)
    jar_files: list[str] = field(default_factory=list)
    output_dirs: list[str] = field(default_factory=list)
    outside_classpath: list[str] = field(default_factory=list)
    sub_builds: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def wars(self) -> list[AntArchive]:
        """``<war>`` 작업들."""
        return [a for a in self.archives if a.kind == "war"]


# ------------------------------------------------------------------------------------------------ helpers
def _local(tag: Any) -> str:
    """네임스페이스(``{uri}``)를 뗀 태그 이름(주석·처리 지시는 "")."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _under(rel: str, d: str) -> bool:
    """``rel`` 이 디렉토리 ``d`` 자체이거나 그 아래인지("" = 루트)."""
    return d == "" or rel == d or rel.startswith(d + "/")


def _uniq(items: list[str]) -> list[str]:
    """순서를 지키며 중복 제거."""
    return list(dict.fromkeys(items))


@lru_cache(maxsize=512)
def _ant_regex(pattern: str) -> re.Pattern[str]:
    """Ant 패턴(``**/*.jar``, ``lib/**``, ``*.jar``) → 정규식. 끝이 ``/`` 면 ``**`` 를 붙인다."""
    p = pattern.replace("\\", "/").strip().lstrip("/")
    if p.endswith("/"):
        p += "**"
    segs = p.split("/")
    out = ""
    for i, seg in enumerate(segs):
        last = i == len(segs) - 1
        if seg == "**":
            out += ".*" if last else "(?:.*/)?"
            continue
        s = "".join("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch) for ch in seg)
        out += s if last else s + "/"
    return re.compile("^" + out + "$")


def ant_match(patterns: list[str], rel: str) -> bool:
    """Ant 패턴 목록 중 하나라도 맞는지(``rel`` 은 fileset 디렉토리 기준 상대 경로).

    True when one of the Ant patterns matches `rel` (relative to the fileset dir)."""
    return any(_ant_regex(p).match(rel) for p in patterns if p)


def _split_patterns(value: str | None) -> list[str]:
    """``includes="a/**, b.jar"`` → 패턴 목록(쉼표·공백 구분)."""
    return [p for p in _PATTERN_SPLIT_RE.split(value or "") if p]


def split_path_list(value: str) -> list[str]:
    """Ant 경로 목록(``src:gen``, ``a;b``, ``a,b``)을 나눈다. Windows 드라이브(``C:\\x`` / ``C:/x``)의 콜론은
    구분자가 아니다.

    Split an Ant path list on `;`, `,` and `:` (a drive letter colon stays)."""
    out: list[str] = []
    for part in _SPLIT_LIST_RE.split(value or ""):
        merged: list[str] = []
        for bit in part.split(":"):
            if merged and len(merged[-1].strip()) == 1 and merged[-1].strip().isalpha() and bit.startswith(("/", "\\")):
                merged[-1] = merged[-1] + ":" + bit
            else:
                merged.append(bit)
        out += [m.strip() for m in merged if m.strip()]
    return out


def parse_properties_text(text: str) -> dict[str, str]:
    """Java ``.properties`` 텍스트 → dict: ``key=value`` / ``key: value`` / ``key value``, ``#`` / ``!`` 주석, 줄 끝
    ``\\`` 이어짐, ``\\uXXXX`` 와 ``\\t`` 같은 이스케이프. 같은 키는 처음 것이 이긴다(Ant 속성은 바뀌지 않음).

    Java .properties text → dict (first definition of a key wins, like Ant)."""
    logical: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.lstrip()
        if not buf and (not line or line[0] in "#!"):
            continue
        trailing = len(line) - len(line.rstrip("\\"))
        if trailing % 2 == 1:
            buf += line[:-1]
            continue
        logical.append(buf + line)
        buf = ""
    if buf:
        logical.append(buf)
    out: dict[str, str] = {}
    for line in logical:
        m = re.match(r"((?:\\.|[^=:\s\\])+)\s*(?:[=:]\s*|\s+|$)(.*)$", line)
        if not m:
            continue
        key, value = _unescape(m.group(1)), _unescape(m.group(2).rstrip())
        out.setdefault(key, value)
    return out


def _unescape(s: str) -> str:
    """.properties 이스케이프 풀기(``\\uXXXX``, ``\\t``, ``\\n``, ``\\r``, ``\\f``, 그 밖의 ``\\x`` → x)."""
    if "\\" not in s:
        return s
    out: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", s[i + 2:i + 6] or ""):
                out.append(chr(int(s[i + 2:i + 6], 16)))
                i += 6
                continue
            out.append({"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _decode(data: bytes) -> str:
    """바이트 → 텍스트(utf-8 → cp949 → latin-1)."""
    for enc in ("utf-8", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _parse_build_xml(data: bytes) -> tuple[ET.Element | None, str]:
    """build.xml 바이트 → (루트 요소, 메모). 선언된 인코딩(EUC-KR 등)으로 풀고 선언을 뗀 뒤 파싱한다. XML 엔티티
    (``<!DOCTYPE project [<!ENTITY common SYSTEM "common.xml">]>`` + ``&common;``)로 깨지면 DOCTYPE 과 엔티티
    참조를 지우고 다시 — 포함 파일은 읽지 않았다는 메모를 남긴다. 그래도 안 되면 (None, 오류)."""
    text = data.decode(pom_encoding(data), errors="replace")
    body = xml_without_declaration(text)
    try:
        return ET.fromstring(body.encode("utf-8")), ""
    except ET.ParseError as e:
        first = str(e)
    stripped = re.sub(r"<!DOCTYPE[^\[>]*(\[.*?\])?\s*>", "", body, count=1, flags=re.DOTALL)
    ents = sorted(set(re.findall(r"&([A-Za-z_][\w.-]*);", stripped)) - {"amp", "lt", "gt", "quot", "apos"})
    stripped = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)[A-Za-z_][\w.-]*;", "", stripped)
    try:
        root = ET.fromstring(stripped.encode("utf-8"))
    except ET.ParseError:
        return None, first
    return root, ("build.xml includes XML entities (" + ", ".join(f"&{x};" for x in ents) + "): the included "
                  "files were not read" if ents else "")


def _folder_name(ws: Any) -> str:
    """프로젝트 폴더 이름. 실행 작업공간의 루트는 언제나 ``<run>/project`` 이므로 저널의 접수 기록(원본 경로,
    zip 이면 확장자 뗀 이름)을 먼저 본다."""
    root = Path(ws.root)
    jp = getattr(ws, "journal_path", None)
    if root.name == "project" and jp:
        try:
            for line in Path(jp).read_text(encoding="utf-8", errors="replace").splitlines():
                ev = json.loads(line) if line.strip().startswith("{") else {}
                if ev.get("event") == "intake" and ev.get("source"):
                    src = Path(str(ev["source"]))
                    return src.stem if src.suffix.lower() == ".zip" else src.name
        except (OSError, ValueError):
            pass
    return root.name


def maven_artifact_id(name: str, fallback: str = "app") -> str:
    """Maven artifactId 로 쓸 수 있게 다듬는다(영문·숫자·``_ - .`` 밖의 글자 묶음은 ``-``). 비면 ``fallback``.

    A string usable as artifactId (`[A-Za-z0-9_.-]`, other runs → `-`); `fallback` when nothing is left."""
    s = re.sub(r"[^A-Za-z0-9_.\-]+", "-", (name or "").strip()).strip("-.")
    return s or fallback


def java_level(value: str | None) -> int | None:
    """Java 수준 표기 → 숫자(``1.6`` / ``6`` → 6, ``11`` → 11). 모르면 None."""
    v = normalize_java_version((value or "").strip()) if value else None
    if not v or "${" in v:
        return None
    m = re.fullmatch(r"1\.(\d+)", v)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"(\d+)(?:\.\d+)*", v)
    return int(m.group(1)) if m else None


def java_level_text(n: int) -> str:
    """숫자 → pom 표기(8 이하는 ``1.8`` 처럼, 9 이상은 ``11``)."""
    return f"1.{n}" if n <= 8 else str(n)


def _is_test_name(text: str) -> bool:
    """타깃 이름 / 경로 마디에 test 낱말이 있는지(``compile-tests``, ``test-src``; ``latest`` 는 아님)."""
    toks = [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t]
    return any(t in TEST_TOKENS or t.startswith("test") for t in toks)


# ------------------------------------------------------------------------------------------------ reader
class _AntReader:
    """build.xml 하나를 읽는 상태: 속성 표(불변: 처음 정의가 이김), id 가 붙은 요소(``<path id>``), 기준 디렉토리,
    못 푼 속성 이름. 모든 경로는 프로젝트 기준 상대 posix 로 바꾼다."""

    def __init__(self, root: Path, build_file: str, project: ET.Element, files: list[str], dirs: set[str]):
        self.root = root
        self.build_file = build_file
        self.project = project
        self.files = files
        self.dirs = dirs
        self.props: dict[str, str] = {}
        self.unresolved: Counter = Counter()
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.loaded: set[str] = set()
        self.ids: dict[str, ET.Element] = {}
        bf_dir = posixpath.dirname(build_file)
        bd = (project.get("basedir") or ".").strip().replace("\\", "/") or "."
        if bd.startswith("/") or re.match(r"^[A-Za-z]:/", bd):
            self.warnings.append(f"{build_file}: absolute basedir {bd!r} — the build file's directory is used")
            bd = "."
        base = posixpath.normpath(posixpath.join(bf_dir, bd)) if bf_dir else posixpath.normpath(bd)
        if base == ".":
            base = ""
        if base.startswith(".."):
            self.warnings.append(f"{build_file}: basedir {bd!r} points outside the project — the build file's "
                                 "directory is used")
            base = bf_dir
        self.base = base
        name = (project.get("name") or "").strip()
        # basedir 는 "." 표식: 경로 속성에서 기준 디렉토리와 이어 붙여 정규화된다(Ant 의 절대 경로 대신)
        self.props.update({"basedir": ".", "ant.project.name": name, "ant.file": posixpath.basename(build_file)})
        for el in project.iter():
            i = el.get("id") if isinstance(el.tag, str) else None
            if i and i not in self.ids:
                self.ids[i] = el

    # -------------------------------------------------------------- properties
    def expand(self, text: str | None, _stack: tuple[str, ...] = ()) -> str:
        """``${name}`` 을 속성 값으로(값 안의 ``${…}`` 도 재귀로) 바꾼다. 모르는 이름은 그대로 두고 센다."""
        if not text or "${" not in text:
            return text or ""

        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            if name in self.props and name not in _stack:
                return self.expand(self.props[name], _stack + (name,))
            if name not in self.props:
                self.unresolved[name] += 1
            return m.group(0)
        return _PROP_RE.sub(repl, text)

    def define(self, name: str, value: str) -> None:
        """Ant 속성은 한 번 정해지면 바뀌지 않는다: 이미 있으면 무시."""
        if name and name not in self.props:
            self.props[name] = value

    def load_file(self, raw: str, prefix: str = "", *, quiet: bool = False) -> None:
        """``<property file>`` / ``<loadproperties srcfile>`` 의 .properties 를 읽어 정의한다(없는 파일은 Ant 처럼
        조용히 넘어가되 경고 한 줄)."""
        rel, why = self.rel(raw)
        if rel is None:
            if not quiet:
                self.warnings.append(f"{self.build_file}: property file {raw!r} not read ({why})")
            return
        if rel in self.loaded:
            return
        p = self.root / rel
        if not p.is_file():
            if not quiet:
                self.warnings.append(f"{self.build_file}: property file {rel} not found (Ant skips it too)")
            return
        self.loaded.add(rel)
        try:
            data = p.read_bytes()
        except OSError as e:
            self.warnings.append(f"{rel}: unreadable ({e})")
            return
        for k, v in parse_properties_text(_decode(data)).items():
            self.define(f"{prefix}.{k}" if prefix else k, v)

    def property_el(self, el: ET.Element) -> None:
        """``<property>`` 하나: name + value / location, file(+ prefix). environment / resource / url / refid 는 무시."""
        if el.get("file"):
            self.load_file(el.get("file") or "", (el.get("prefix") or "").strip().rstrip("."))
            return
        if el.get("environment") or el.get("resource") or el.get("url"):
            return
        name = (el.get("name") or "").strip()
        if not name:
            return
        if el.get("value") is not None:
            self.define(name, el.get("value") or "")
        elif el.get("location") is not None:
            self.define(name, el.get("location") or "")

    def load_properties(self) -> None:
        """속성 정의 순서: 최상위 ``<property>`` / ``<loadproperties>`` (문서 순서) → 타깃 안의 것(문서 순서, 아직
        없는 이름만) → 참조되지 않은 루트 ``build.properties`` (남은 이름만)."""
        for el in self.project:
            self._property_like(el)
        for t in self.project:
            if _local(t.tag) == "target":
                for el, _tn, parent in self.walk(t, t.get("name") or ""):
                    if parent not in ("ant", "subant", "antcall"):
                        self._property_like(el)
        self.load_file("build.properties", quiet=True)

    def _property_like(self, el: ET.Element) -> None:
        tag = _local(el.tag)
        if tag == "property":
            self.property_el(el)
        elif tag == "loadproperties" and el.get("srcfile"):
            self.load_file(el.get("srcfile") or "", (el.get("prefix") or "").strip().rstrip("."))

    # -------------------------------------------------------------- paths
    def rel(self, value: str | None) -> tuple[str | None, str]:
        """속성을 푼 경로 → (프로젝트 상대 posix, "") 또는 (None, 이유: unresolved / outside / empty)."""
        v = self.expand(value).strip().replace("\\", "/")
        if not v:
            return None, "empty"
        if "${" in v:
            return None, "unresolved property"
        if v.startswith("/") or re.match(r"^[A-Za-z]:/", v):
            try:
                rel = Path(v).resolve().relative_to(self.root.resolve()).as_posix()
            except (ValueError, OSError):
                return None, "outside the project"
            return ("" if rel == "." else rel), ""
        joined = posixpath.normpath(posixpath.join(self.base, v)) if self.base else posixpath.normpath(v)
        if joined == ".":
            return "", ""
        if joined == ".." or joined.startswith("../"):
            return None, "outside the project"
        return joined, ""

    def path_entry(self, raw: str) -> ClasspathEntry:
        """경로 하나 → 클래스패스 항목(jar / dir / outside)."""
        rel, _why = self.rel(raw)
        if rel is None:
            return ClasspathEntry("outside", self.expand(raw))
        if rel.lower().endswith((".jar", ".zip")):
            return ClasspathEntry("jar", rel)
        return ClasspathEntry("dir", rel)

    def fileset(self, el: ET.Element, seen: tuple[str, ...] = ()) -> list[ClasspathEntry]:
        """``<fileset>`` / ``<zipfileset>`` / ``<lib>`` 같은 fileset 류: dir(또는 file) + includes / excludes
        (속성과 중첩 ``<include>`` / ``<exclude>`` / ``<patternset>``). ``refid`` 는 그 요소로."""
        refid = el.get("refid")
        if refid:
            if refid in seen or refid not in self.ids:
                if refid not in self.ids:
                    self.warnings.append(f"{self.build_file}: refid {refid!r} not found")
                return []
            return self.fileset(self.ids[refid], seen + (refid,))
        if el.get("file") and not el.get("dir"):
            return [self.path_entry(el.get("file") or "")]
        rel, _why = self.rel(el.get("dir") or "")
        if rel is None:
            return [ClasspathEntry("outside", self.expand(el.get("dir") or ""))] if el.get("dir") else []
        inc = _split_patterns(self.expand(el.get("includes")))
        exc = _split_patterns(self.expand(el.get("excludes")))
        for c in el.iter():
            t = _local(c.tag)
            if t == "include" and c.get("name"):
                inc.append(self.expand(c.get("name")))
            elif t == "exclude" and c.get("name"):
                exc.append(self.expand(c.get("name")))
        return [ClasspathEntry("fileset", rel, inc, exc)]

    def classpath(self, el: ET.Element, seen: tuple[str, ...] = ()) -> list[ClasspathEntry]:
        """``<classpath>`` / ``<path>`` / ``<src>`` 류의 항목들: refid, path / location 속성, 중첩 pathelement /
        fileset / filelist / path(재귀, 순환은 끊음). dirset 은 클래스 폴더라 뺀다."""
        out: list[ClasspathEntry] = []
        refid = el.get("refid")
        if refid:
            if refid in self.ids and refid not in seen:
                out += self.classpath(self.ids[refid], seen + (refid,))
            elif refid not in self.ids:
                self.warnings.append(f"{self.build_file}: classpath refid {refid!r} not found")
        if el.get("path"):
            out += [self.path_entry(p) for p in split_path_list(self.expand(el.get("path")))]
        if el.get("location"):
            out.append(self.path_entry(el.get("location") or ""))
        for c in el:
            t = _local(c.tag)
            if t == "pathelement":
                if c.get("location"):
                    out.append(self.path_entry(c.get("location") or ""))
                if c.get("path"):
                    out += [self.path_entry(p) for p in split_path_list(self.expand(c.get("path")))]
            elif t in ("fileset", "zipfileset"):
                out += self.fileset(c, seen)
            elif t == "filelist":
                d = c.get("dir") or "."
                for f in _split_patterns(self.expand(c.get("files"))):
                    out.append(self.path_entry(posixpath.join(self.expand(d), f)))
            elif t in ("path", "classpath"):
                out += self.classpath(c, seen)
        return out

    # -------------------------------------------------------------- tasks
    def walk(self, el: ET.Element, target_name: str = "",
             parent: str = "") -> Iterator[tuple[ET.Element, str, str]]:
        """작업 요소를 (요소, 타깃 이름, 부모 태그)로 훑는다. 타깃과 묶음 작업(sequential, if/then …) 안으로만
        들어가고, 다른 작업의 중첩 요소(fileset 등)는 그 작업이 직접 읽는다. macrodef 는 건너뛴다(메모)."""
        for c in el:
            tag = _local(c.tag)
            if not tag:
                continue
            if tag == "target":
                yield from self.walk(c, c.get("name") or "", tag)
                continue
            if tag == "macrodef":
                self.note(f"<macrodef name=\"{c.get('name') or '?'}\">: macro bodies were not read")
                continue
            yield c, target_name, parent
            if tag in CONTAINER_TAGS:
                yield from self.walk(c, target_name, tag)

    def note(self, text: str) -> None:
        """번역 못 한 build.xml 항목 한 줄(중복 없이, 최대 ``MAX_NOTES``)."""
        if text not in self.notes:
            self.notes.append(text)

    def where(self, target_name: str) -> str:
        """메모 앞머리: ``build.xml target `war`: ``."""
        return f"{self.build_file} target `{target_name}`" if target_name else f"{self.build_file} (top level)"


def _javac(r: _AntReader, el: ET.Element, tname: str) -> AntJavac:
    """``<javac>`` 하나를 읽는다(srcdir 목록 + 중첩 ``<src>``, destdir, source / target / release, encoding,
    classpath 속성 / classpathref / 중첩 ``<classpath>``). includes / excludes 는 번역하지 않고 메모."""
    j = AntJavac(target_name=tname)
    raws = split_path_list(r.expand(el.get("srcdir"))) if el.get("srcdir") else []
    for c in el:
        if _local(c.tag) == "src":
            for e in r.classpath(c):
                if e.kind in ("dir", "fileset"):
                    j.srcdirs.append(e.path)
                elif e.kind == "outside":
                    r.warnings.append(f"{r.where(tname)}: javac source dir {e.path!r} is outside the project or "
                                      "unresolved — ignored")
    for raw in raws:
        rel, why = r.rel(raw)
        if rel is None:
            r.warnings.append(f"{r.where(tname)}: javac srcdir {raw!r} not usable ({why}) — ignored")
        else:
            j.srcdirs.append(rel)
    j.srcdirs = _uniq(j.srcdirs)
    j.destdir = r.rel(el.get("destdir"))[0] if el.get("destdir") else None
    rel_attr = r.expand(el.get("release") or "").strip()
    j.source = r.expand(el.get("source") or "").strip() or rel_attr
    j.java_target = r.expand(el.get("target") or "").strip() or rel_attr
    j.encoding = r.expand(el.get("encoding") or "").strip()
    if el.get("classpath"):
        j.classpath += [r.path_entry(p) for p in split_path_list(r.expand(el.get("classpath")))]
    if el.get("classpathref"):
        ref = el.get("classpathref") or ""
        if ref in r.ids:
            j.classpath += r.classpath(r.ids[ref], (ref,))
        else:
            r.warnings.append(f"{r.where(tname)}: classpathref {ref!r} not found")
    for c in el:
        if _local(c.tag) == "classpath":
            j.classpath += r.classpath(c)
    j.test = _is_test_name(tname) or any(any(_is_test_name(seg) for seg in d.split("/")) for d in j.srcdirs)
    inc = el.get("includes") or el.get("excludes") or any(_local(c.tag) in ("include", "exclude") for c in el)
    if inc and not j.test:
        r.note(f"{r.where(tname)}: <javac> includes / excludes not translated (Maven compiles every .java file "
               "under the source root)")
    return j


def _archive(r: _AntReader, el: ET.Element, tag: str, tname: str) -> AntArchive:
    """``<war>`` / ``<jar>`` / ``<ear>`` 하나: destfile(warfile / jarfile), webxml, basedir, 중첩 ``<lib>`` (lib
    디렉토리), ``<fileset>`` / ``<zipfileset>`` (prefix 없음 = 내용, WEB-INF/lib = lib), ``<webinf>`` (그 부모 =
    내용), ``<classes>`` (출력이라 무시), manifest 의 Main-Class."""
    a = AntArchive(kind=tag, target_name=tname)
    raw = el.get("destfile") or el.get("warfile") or el.get("jarfile") or el.get("earfile") or ""
    a.raw_destfile = r.expand(raw)
    a.destfile = r.rel(raw)[0] if raw else None
    if el.get("webxml"):
        a.webxml = r.rel(el.get("webxml"))[0]
    if el.get("basedir"):
        a.basedir = r.rel(el.get("basedir"))[0]
    for c in el:
        t = _local(c.tag)
        if t == "lib":
            a.lib_dirs += [e.path for e in r.fileset(c) if e.kind == "fileset"]
        elif t in ("fileset", "zipfileset"):
            prefix = (r.expand(c.get("prefix")) or "").strip("/")
            for e in r.fileset(c):
                if e.kind != "fileset":
                    continue
                if prefix == "WEB-INF/lib":
                    a.lib_dirs.append(e.path)
                elif prefix in ("", "/"):
                    a.content_dirs.append(e.path)
        elif t == "webinf":
            for e in r.fileset(c):
                if e.kind == "fileset" and posixpath.basename(e.path) == "WEB-INF":
                    a.content_dirs.append(posixpath.dirname(e.path))
        elif t == "manifest":
            for at in c.iter():
                if _local(at.tag) == "attribute" and (at.get("name") or "").lower() == "main-class":
                    a.main_class = r.expand(at.get("value") or "")
    a.lib_dirs, a.content_dirs = _uniq(a.lib_dirs), _uniq(a.content_dirs)
    return a


def _copy(r: _AntReader, el: ET.Element, tag: str, tname: str) -> AntCopy:
    """``<copy>`` / ``<move>`` / ``<sync>`` 하나: todir(또는 tofile 의 디렉토리), 원본 fileset / file, 필터링
    (``filtering`` / ``<filterset>`` / ``<filterchain>``)."""
    raw = el.get("todir") or ""
    if not raw and el.get("tofile"):
        raw = posixpath.dirname(r.expand(el.get("tofile")).replace("\\", "/")) or "."
    cp = AntCopy(task=tag, target_name=tname, todir=r.rel(raw)[0] if raw else None, raw_todir=r.expand(raw))
    if el.get("file"):
        cp.filesets.append(r.path_entry(el.get("file") or ""))
    for c in el:
        t = _local(c.tag)
        if t in ("fileset", "zipfileset"):
            cp.filesets += r.fileset(c)
        elif t in ("filterset", "filterchain"):
            cp.filtering = True
    cp.filtering = cp.filtering or (el.get("filtering") or "").lower() in ("true", "yes", "on")
    return cp


def _read_tasks(r: _AntReader, ap: AntProject) -> None:
    """모든 작업을 훑어 javac / war / jar / copy 를 모으고, 번역 못 하는 것(exec, java, xslt, 코드 생성 작업,
    코드 생성처럼 보이는 antcall, 하위 빌드, taskdef, import)을 메모로 남긴다."""
    taskdefs: list[str] = []
    for el, tname, _parent in r.walk(r.project):
        tag = _local(el.tag)
        ns = el.tag[1:].split("}", 1)[0] if isinstance(el.tag, str) and el.tag.startswith("{") else ""
        w = r.where(tname)
        if "ivy" in ns.lower():
            r.note(f"{w}: Ivy <{tag}> — ivy.xml is not translated; only the jars present in the tree are declared")
            continue
        if tag == "javac":
            ap.javac.append(_javac(r, el, tname))
        elif tag in ("war", "jar", "ear"):
            ap.archives.append(_archive(r, el, tag, tname))
        elif tag in ("copy", "move", "sync"):
            ap.copies.append(_copy(r, el, tag, tname))
        elif tag == "exec":
            r.note(f"{w}: <exec executable=\"{r.expand(el.get('executable') or '?')}\"> — not translated (runs an "
                   "external program)")
        elif tag == "java":
            what = r.expand(el.get("classname") or el.get("jar") or "?")
            r.note(f"{w}: <java {('classname' if el.get('classname') else 'jar')}=\"{what}\"> — not translated "
                   "(runs a Java program; code generation?)")
        elif tag in ("xslt", "style"):
            r.note(f"{w}: <{tag}> — not translated (XSLT transformation)")
        elif tag in CODEGEN_TASKS:
            r.note(f"{w}: <{tag}> — not translated ({CODEGEN_TASKS[tag]})")
        elif tag == "antcall":
            tgt = el.get("target") or ""
            if CODEGEN_TARGET_RE.search(tgt):
                r.note(f"{w}: <antcall target=\"{tgt}\"> — looks like code generation; not translated")
        elif tag == "subant":
            # fileset / pathelement 은 classpath() 가, dirset 과 buildpath 속성은 여기서 본다
            found = [e.path for e in r.classpath(el)]
            found += [x.path for c in el if _local(c.tag) == "dirset" for x in r.fileset(c)]
            found += [r.expand(p) for p in split_path_list(r.expand(el.get("buildpath")))]
            what = ", ".join(_uniq([f or "." for f in found])) or "?"
            ap.sub_builds.append(what)
            r.note(f"{w}: <subant> ({what}) — a multi-module Ant build: only the root pom was synthesized")
        elif tag == "ant":
            d = r.expand(el.get("dir") or "")
            f = r.expand(el.get("antfile") or "")
            if d or (f and posixpath.basename(f) != posixpath.basename(r.build_file)):
                what = posixpath.join(d, f) if d and f else (d or f)
                ap.sub_builds.append(what)
                r.note(f"{w}: <ant {('dir' if d else 'antfile')}=\"{d or f}\"> — a sub-build: only the root pom was "
                       "synthesized")
            elif CODEGEN_TARGET_RE.search(el.get("target") or ""):
                r.note(f"{w}: <ant target=\"{el.get('target')}\"> — looks like code generation; not translated")
        elif tag in ("taskdef", "typedef"):
            n = el.get("name") or el.get("resource") or el.get("classname") or "?"
            if n not in taskdefs:
                taskdefs.append(n)
        elif tag in ("import", "include"):
            r.note(f"{w}: <{tag} file=\"{r.expand(el.get('file') or '?')}\"> — the imported build file was not read")
    if taskdefs:
        r.note(f"{r.build_file}: custom Ant tasks ({', '.join(taskdefs[:6])}{' …' if len(taskdefs) > 6 else ''}) — "
               "not translated")


# ------------------------------------------------------------------------------------------------ directories
def _output_dirs(r: _AntReader, ap: AntProject) -> list[str]:
    """빌드 산출물 디렉토리: javac destdir, war / jar 결과 파일의 디렉토리, 루트의 관례 이름(build, dist, out,
    bin, classes …, 소스·웹 루트가 아닌 것)."""
    out: list[str] = []
    for j in ap.javac:
        if j.destdir:
            out.append(j.destdir)
    for a in ap.archives:
        if a.destfile and posixpath.dirname(a.destfile):
            out.append(posixpath.dirname(a.destfile))
    src = {d for j in ap.javac for d in j.srcdirs}
    for name in OUTPUT_DIR_NAMES:
        d = posixpath.join(r.base, name) if r.base else name
        if d in r.dirs and d not in src and not any(_under(s, d) for s in src):
            out.append(d)
    return [d for d in _uniq(out) if d]


def _web_root(r: _AntReader, ap: AntProject) -> None:
    """웹 루트를 정한다: ``<war webxml>`` 이 WEB-INF 아래면 그 부모, 아니면 war 의 내용 디렉토리(산출물 아래의
    스테이징이면 거기로 copy 한 원본), 그다음 ``WEB-INF/web.xml`` 을 가진 디렉토리(산출물 밖, 관례 이름 우선,
    얕은 것 우선). 여럿이면 경고."""
    outputs = ap.output_dirs
    cands: list[str] = []

    def staged(d: str) -> list[str]:
        # 산출물 아래 스테이징 디렉토리면 그리로 copy 한 원본 디렉토리들
        if not any(_under(d, o) for o in outputs):
            return [d]
        return [e.path for c in ap.copies if c.todir is not None and _under(c.todir, d)
                for e in c.filesets if e.kind == "fileset" and not any(_under(e.path, o) for o in outputs)]

    for w in ap.wars:
        if w.webxml:
            parent = posixpath.dirname(w.webxml)
            if posixpath.basename(parent) == "WEB-INF":
                cands += staged(posixpath.dirname(parent))
        for d in w.content_dirs + ([w.basedir] if w.basedir is not None else []):
            cands += staged(d)
    cands = [c for c in _uniq(cands) if (c == "" or c in r.dirs) and _join(c, "WEB-INF") in r.dirs]
    files = set(r.files)
    found = sorted(f[:-len("WEB-INF/web.xml")].rstrip("/") for f in r.files
                   if (f == "WEB-INF/web.xml" or f.endswith("/WEB-INF/web.xml"))
                   and "WEB-INF" not in f.split("/")[:-2]
                   and not any(_under(f, o) for o in outputs))
    if not found and ap.wars:
        found = sorted(posixpath.dirname(d) for d in r.dirs if posixpath.basename(d) == "WEB-INF"
                       and "WEB-INF" not in d.split("/")[:-1] and not any(_under(d, o) for o in outputs))

    def rank(d: str) -> tuple[int, int, str]:
        rel = d[len(r.base) + 1:] if r.base and d.startswith(r.base + "/") else d
        names = [n.lower() for n in WEB_ROOT_NAMES]
        return (names.index(rel.lower()) if rel.lower() in names else len(names), d.count("/"), d)

    chosen = cands[0] if cands else (min(found, key=rank) if found else None)
    if chosen is None:
        if ap.wars:
            ap.warnings.append(f"{r.build_file}: a <war> task but no web root (no WEB-INF directory) found — "
                               "packaging war without web content")
        return
    others = [d for d in _uniq(cands + found) if d != chosen]
    if others:
        ap.warnings.append(f"web root {chosen or '.'} chosen; other WEB-INF directories ignored: "
                           + ", ".join(d or "." for d in others[:6]))
    ap.web_root = chosen
    wx = _join(chosen, "WEB-INF/web.xml")
    ap.has_web_xml = wx in files
    for w in ap.wars:
        if w.webxml and w.webxml != wx and w.webxml in files:
            ap.web_xml = w.webxml
            ap.has_web_xml = True
            break
    ap.jsp_count = sum(1 for f in r.files if _under(f, chosen) and f.lower().endswith((".jsp", ".jspf", ".jspx")))


def _join(d: str, rel: str) -> str:
    return f"{d}/{rel}" if d else rel


def _jars(r: _AntReader, ap: AntProject) -> None:
    """선언할 jar 와 lib 디렉토리: WEB-INF/lib(바로 아래), 본 javac 의 classpath(fileset 패턴, pathelement jar),
    war 의 ``<lib>``, 루트 ``lib/`` (하위 포함). 산출물 아래 jar 와 자기 결과물은 classpath 가 직접 가리킬
    때만. 프로젝트 밖·못 푼 classpath 는 경고."""
    outputs = ap.output_dirs
    own = {a.destfile for a in ap.archives if a.destfile}
    jars: dict[str, None] = {}
    lib_dirs: list[str] = []
    file_set = r.files

    def add_dir(d: str, inc: list[str], exc: list[str], *, explicit: bool, recursive: bool = True) -> None:
        hit = False
        for f in file_set:
            if not f.lower().endswith(".jar") or not _under(f, d) or f == d:
                continue
            sub = f[len(d) + 1:] if d else f
            if not recursive and "/" in sub:
                continue
            if inc and not ant_match(inc, sub):
                continue
            if exc and ant_match(exc, sub):
                continue
            if f in own or (not explicit and any(_under(f, o) for o in outputs)):
                continue
            jars.setdefault(f, None)
            hit = True
        if hit:
            lib_dirs.append(d)

    if ap.web_root is not None:
        add_dir(_join(ap.web_root, "WEB-INF/lib"), [], [], explicit=False, recursive=False)
    for j in ap.javac:
        if j.test:
            continue
        for e in j.classpath:
            if e.kind == "jar":
                if e.path in file_set and e.path not in own:
                    jars.setdefault(e.path, None)
                    if posixpath.dirname(e.path) not in lib_dirs:
                        lib_dirs.append(posixpath.dirname(e.path))
                elif e.path not in file_set:
                    ap.warnings.append(f"{r.where(j.target_name)}: classpath jar {e.path} does not exist")
            elif e.kind == "fileset":
                add_dir(e.path, e.includes, e.excludes, explicit=True)
            elif e.kind == "outside" and e.path not in ap.outside_classpath:
                ap.outside_classpath.append(e.path)
    for a in ap.wars:
        for d in a.lib_dirs:
            add_dir(d, [], [], explicit=True)
    root_lib = _join(r.base, "lib")
    if root_lib in r.dirs:
        add_dir(root_lib, [], [], explicit=False)
    for o in ap.outside_classpath:
        ap.warnings.append(f"classpath entry {o} is outside the project or not resolvable: its jars are not declared "
                           "(a servlet / JSP API the sources use is added as provided)")
    ap.jar_files = list(jars)
    ap.lib_dirs = _uniq(lib_dirs)


def _sources(r: _AntReader, ap: AntProject, cands: tuple[str, ...]) -> None:
    """소스 루트를 ``package a.b;`` 로 확인한다. 본 javac 의 srcdir 에 .java 가 있으면 그 아래만(파일의 패키지가
    경로와 맞으면 그 위가 루트, 아니면 srcdir), 없으면 트리 전체(산출물·WEB-INF/classes 제외). test 이름의
    루트는 무시(테스트는 범위 밖), 다른 루트 안의 루트는 뺀다(두 번 컴파일되지 않게). 인코딩도 여기서 센다."""
    outputs = ap.output_dirs
    main_src = _uniq([d for j in ap.javac if not j.test for d in j.srcdirs])
    test_src = _uniq([d for j in ap.javac if j.test for d in j.srcdirs])
    java = [f for f in r.files if f.endswith(".java") and not any(_under(f, o) for o in outputs)
            and "/WEB-INF/classes/" not in "/" + f]
    javac_mode = any(_under(f, d) for f in java for d in main_src)
    if main_src and not javac_mode:
        ap.warnings.append("javac srcdir " + ", ".join(d or "." for d in main_src) + " holds no .java file — "
                           "source roots taken from the package declarations of the whole tree")
    roots: Counter = Counter()
    first_seen: dict[str, int] = {}
    tests: Counter = Counter()
    mismatched: list[str] = []
    outside = 0
    encs: Counter = Counter()
    for rel in java:
        owner = max((d for d in main_src if _under(rel, d)), key=len, default=None) if javac_mode else None
        towner = max((d for d in test_src if _under(rel, d)), key=len, default=None)
        if towner is not None and (owner is None or len(towner) > len(owner)):
            tests[towner] += 1
            continue
        if javac_mode and owner is None:
            outside += 1
            continue
        p = r.root / rel
        try:
            data = p.read_bytes()
        except OSError as e:
            ap.warnings.append(f"{rel}: unreadable ({e})")
            continue
        enc = "ascii" if data.isascii() else detect_encoding(p, cands or None)
        if enc != "ascii":
            encs[JAVA_CHARSET.get(enc.lower(), enc)] += 1
        pkg = scan_java_text(rel, data.decode("utf-8" if enc in ("ascii", "utf-8-sig") else enc,
                                              errors="replace")).package
        sroot, ok = _source_root_of(rel, pkg)
        if owner is not None:
            if ok and _under(sroot, owner):
                root = sroot
            else:
                root = owner
                mismatched.append(rel)
        else:
            root = sroot
            if not ok:
                mismatched.append(rel)
        # build.xml 이 직접 준 srcdir 은 그대로 두고, 그보다 깊이 찾은 루트(또는 javac 없이 찾은 루트)의 test 마디만 본다
        if owner is None or owner == "":
            extra_segs = root.split("/")
        else:
            extra_segs = root[len(owner) + 1:].split("/") if root != owner else []
        if any(_is_test_name(seg) for seg in extra_segs if seg):
            tests[root] += 1
            continue
        roots[root] += 1
        first_seen.setdefault(root, len(first_seen))
    if mismatched:
        ap.warnings.append(f"{len(mismatched)} .java file(s) whose package does not match the directory: "
                           + ", ".join(sorted(mismatched)[:6]))
    if outside:
        ap.warnings.append(f"{outside} .java file(s) outside the javac source dirs ignored (Ant does not compile them)")
    order = {d: i for i, d in enumerate(main_src)}

    def key(d: str) -> tuple[int, int, int]:
        owner = max((m for m in main_src if _under(d, m)), key=len, default=None)
        return (order.get(owner, len(order)) if owner is not None else len(order), -roots[d], first_seen[d])

    kept: list[str] = []
    for d in sorted(roots, key=lambda x: (x.count("/"), x)):
        if not any(_under(d, k) for k in kept):
            kept.append(d)
        else:
            outer = next(k for k in kept if _under(d, k))
            roots[outer] += roots[d]
            ap.warnings.append(f"source root {d} lies inside {outer or '.'}: compiled as part of {outer or '.'}")
    ap.source_roots = sorted(kept, key=key)
    ap.source_files = {d: roots[d] for d in ap.source_roots}
    ap.test_roots = sorted(tests)
    if tests:
        ap.warnings.append("test sources ignored (tests are out of scope): "
                           + ", ".join(f"{d or '.'} ({n})" for d, n in sorted(tests.items())))
    if encs:
        ap.source_encoding = min(encs.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if len(encs) > 1:
            ap.warnings.append("sources in mixed encodings (" + ", ".join(f"{k} {v}" for k, v in sorted(encs.items()))
                               + f"): {ap.source_encoding} declared")


def _resources(r: _AntReader, ap: AntProject) -> None:
    """``<resources>``: 비 .java 파일이 있는 소스 루트(**/*.java 제외), 클래스 디렉토리로 copy 하는 fileset(그
    includes / excludes), 있으면 ``src/main/resources``. 산출물 밖으로의 copy, 필터링 copy 는 메모."""
    outputs = ap.output_dirs
    classes_dirs = [j.destdir for j in ap.javac if j.destdir and not j.test]
    res: dict[str, ResourceDir] = {}
    for d in ap.source_roots:
        if any(_under(f, d) and not f.endswith((".java", ".class")) and not any(
                _under(f, o) for o in ap.source_roots if o != d and _under(o, d)) for f in r.files):
            res[d] = ResourceDir(d, [], ["**/*.java"])
    for c in ap.copies:
        w = r.where(c.target_name)
        if c.todir is None:
            r.note(f"{w}: <{c.task} todir=\"{c.raw_todir}\"> — outside the project or unresolved; not translated")
            continue
        into_classes = any(_under(c.todir, k) for k in classes_dirs) or "/WEB-INF/classes" in "/" + c.todir
        if into_classes:
            for e in c.filesets:
                if e.kind != "fileset" or any(_under(e.path, o) for o in outputs):
                    continue
                if e.path in res and e.path in ap.source_roots:
                    res[e.path] = ResourceDir(e.path, list(e.includes), _uniq(list(e.excludes) + ["**/*.java"]))
                elif e.path not in res:
                    res[e.path] = ResourceDir(e.path, list(e.includes), list(e.excludes))
            if c.filtering:
                r.note(f"{w}: <{c.task}> into the classes directory with token filtering — values are not "
                       "substituted by Maven")
        elif any(_under(c.todir, o) for o in outputs):
            continue                                       # staging for the archive (web content, dist)
        else:
            r.note(f"{w}: <{c.task} todir=\"{c.todir or '.'}\"> — copies into the source tree; not translated")
    smr = _join(r.base, "src/main/resources")
    if smr in r.dirs and smr not in res:
        res[smr] = ResourceDir(smr)
    ap.resources = [v for v in res.values() if v.dir in r.dirs or v.dir == ""]


def _misc_notes(r: _AntReader, ap: AntProject) -> None:
    """결과물 여럿, ear, jar 의 Main-Class 같은 메모."""
    outs = [a for a in ap.archives if a.kind in ("war", "jar", "ear")]
    if len(outs) > 1:
        r.note(f"{r.build_file} makes {len(outs)} archives ("
               + ", ".join(a.raw_destfile or a.kind for a in outs[:5]) + "): the pom packages one "
               + ("war" if ap.web_root is not None or ap.wars else "jar"))
    for a in ap.archives:
        if a.kind == "ear":
            r.note(f"{r.where(a.target_name)}: <ear> — EAR packaging is not translated (the WAR / JAR is)")
        if a.main_class and a.kind == "jar":
            r.note(f"{r.where(a.target_name)}: <jar> manifest Main-Class {a.main_class} — not translated")


def describe_ant(ws: Any) -> AntProject:
    """build.xml 과 폴더를 읽어 :class:`AntProject` 를 만든다(읽기 전용, 예외 없음 — 문제는 ``error`` /
    ``warnings``). build.xml 은 루트의 것, 없으면 가장 얕은 것.

    Read build.xml (root, else the shallowest) and the directories into an `AntProject`. Never raises."""
    ap = AntProject()
    root = Path(ws.root)
    bf = find_ant_build_file(ws)
    if not bf:
        ap.error = "no build.xml found"
        return ap
    ap.build_file = bf
    try:
        data = (root / bf).read_bytes()
    except OSError as e:
        ap.error = f"{bf} cannot be read ({e})"
        return ap
    project, note = _parse_build_xml(data)
    if project is None:
        ap.error = f"{bf} is not well-formed XML ({note})"
        return ap
    if _local(project.tag) != "project":
        ap.error = f"{bf}: root element is <{_local(project.tag)}>, not an Ant <project>"
        return ap
    files, dirs = _walk(root)
    r = _AntReader(root, bf, project, files, dirs)
    if note:
        r.note(note)
    ap.name = (project.get("name") or "").strip()
    ap.default_target = (project.get("default") or "").strip()
    ap.basedir = r.base
    ap.targets = [t.get("name") or "" for t in project if _local(t.tag) == "target"]
    r.load_properties()
    _read_tasks(r, ap)
    for name in VERSION_PROPS:
        v = r.expand(r.props.get(name, "")).strip() if name in r.props else ""
        if v and "${" not in v and re.match(r"^\d[\w.\-]*$", v):
            ap.version = v
            break
    main = [j for j in ap.javac if not j.test]
    lv_src = [n for n in (java_level(j.source) for j in main) if n]
    lv_tgt = [n for n in (java_level(j.java_target) for j in main) if n]
    ap.java_source = java_level_text(max(lv_src)) if lv_src else ""
    ap.java_target = java_level_text(max(lv_tgt)) if lv_tgt else ""
    for j in main:
        for raw, what in ((j.source, "source"), (j.java_target, "target")):
            if raw and java_level(raw) is None:
                ap.warnings.append(f"{r.where(j.target_name)}: javac {what}={raw!r} not understood")
    ap.encoding = next((j.encoding for j in main if j.encoding and "${" not in j.encoding), "")
    ap.output_dirs = _output_dirs(r, ap)
    _web_root(r, ap)
    _jars(r, ap)
    cands = tuple(getattr(ws, "encoding_candidates", None) or ())
    _sources(r, ap, cands)
    _resources(r, ap)
    _misc_notes(r, ap)
    ap.properties = {k: r.expand(v) for k, v in r.props.items()}
    for name, n in r.unresolved.most_common(MAX_UNRESOLVED_WARNINGS):
        ap.warnings.append(f"{bf}: ${{{name}}} is not defined in build.xml / its property files (used {n}×, left as "
                           "is)")
    if len(r.unresolved) > MAX_UNRESOLVED_WARNINGS:
        ap.warnings.append(f"{bf}: … {len(r.unresolved) - MAX_UNRESOLVED_WARNINGS} more undefined properties")
    ap.notes = r.notes[:MAX_NOTES] + ([f"… {len(r.notes) - MAX_NOTES} more build.xml items not translated"]
                                       if len(r.notes) > MAX_NOTES else [])
    ap.warnings = _uniq(r.warnings + ap.warnings)
    return ap


# ------------------------------------------------------------------------------------------------ pom
@dataclass
class _Dep:
    """pom 에 쓸 의존성 하나(system 이면 systemPath 와 이유 주석)."""
    group_id: str
    artifact_id: str
    version: str
    scope: str = "compile"
    system_path: str = ""
    comment: str = ""
    path: str = ""


def _comment(text: str) -> str:
    """XML 주석 본문(``--`` 은 쓸 수 없다)."""
    return text.replace("--", "- -").replace("\n", " ")


def render_pom(*, group_id: str, artifact_id: str, version: str, packaging: str, name: str, build_file: str,
               java_source: str, java_target: str, encoding: str, source_dirs: list[str],
               resources: list[ResourceDir], web_root: str | None, web_xml: str | None, has_web_xml: bool,
               war_excludes: list[str], final_name: str, deps: list[_Dep]) -> str:
    """합성 pom 텍스트(UTF-8 선언, 들여쓰기 2칸). 소스 루트가 여럿이면 첫 것이 sourceDirectory, 나머지는
    build-helper ``add-source``.

    The synthesized pom text (UTF-8 declaration, 2-space indent)."""
    out: list[str] = []

    def add(depth: int, text: str) -> None:
        out.append("  " * depth + text)

    def el(depth: int, tag: str, value: str) -> None:
        add(depth, f"<{tag}>{escape(value)}</{tag}>")

    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append(f"<!-- {_comment(f'synthesized by sbmig (v2.1j) from {build_file}; {build_file} is kept as it was')} -->")
    out.append(f"<project {POM_NS}>")
    el(1, "modelVersion", "4.0.0")
    el(1, "groupId", group_id)
    el(1, "artifactId", artifact_id)
    el(1, "version", version)
    el(1, "packaging", packaging)
    if name and name != artifact_id:
        el(1, "name", name)
    out.append("")
    add(1, "<properties>")
    el(2, "maven.compiler.source", java_source)
    el(2, "maven.compiler.target", java_target)
    el(2, "project.build.sourceEncoding", encoding)
    add(1, "</properties>")
    if deps:
        out.append("")
        add(1, "<dependencies>")
        for d in deps:
            if d.comment:
                add(2, f"<!-- {_comment(d.comment)} -->")
            add(2, "<dependency>")
            el(3, "groupId", d.group_id)
            el(3, "artifactId", d.artifact_id)
            el(3, "version", d.version)
            if d.scope and d.scope != "compile":
                el(3, "scope", d.scope)
            if d.system_path:
                el(3, "systemPath", d.system_path)
            add(2, "</dependency>")
        add(1, "</dependencies>")
    out.append("")
    add(1, "<build>")
    if final_name:
        el(2, "finalName", final_name)
    el(2, "sourceDirectory", source_dirs[0] or ".")
    if resources:
        add(2, "<resources>")
        for rd in resources:
            add(3, "<resource>")
            el(4, "directory", rd.dir or ".")
            for tag, items in (("includes", rd.includes), ("excludes", rd.excludes)):
                if items:
                    add(4, f"<{tag}>")
                    for it in items:
                        el(5, tag[:-1], it)
                    add(4, f"</{tag}>")
            add(3, "</resource>")
        add(2, "</resources>")
    plugins = packaging == "war" or len(source_dirs) > 1
    if plugins:
        add(2, "<plugins>")
    if packaging == "war":
        g, a, v = WAR_PLUGIN
        add(3, "<plugin>")
        el(4, "groupId", g)
        el(4, "artifactId", a)
        el(4, "version", v)
        conf: list[tuple[str, str]] = []
        if web_root is not None:
            conf.append(("warSourceDirectory", web_root or "."))
        if web_xml:
            conf.append(("webXml", web_xml))
        if war_excludes:
            conf.append(("warSourceExcludes", ",".join(war_excludes)))
        if not has_web_xml:
            conf.append(("failOnMissingWebXml", "false"))
        if conf:
            add(4, "<configuration>")
            for k, val in conf:
                el(5, k, val)
            add(4, "</configuration>")
        add(3, "</plugin>")
    if len(source_dirs) > 1:
        g, a, v = BUILD_HELPER_PLUGIN
        add(3, "<plugin>")
        el(4, "groupId", g)
        el(4, "artifactId", a)
        el(4, "version", v)
        add(4, "<executions>")
        add(5, "<execution>")
        el(6, "id", "add-source")
        el(6, "phase", "generate-sources")
        add(6, "<goals>")
        el(7, "goal", "add-source")
        add(6, "</goals>")
        add(6, "<configuration>")
        add(7, "<sources>")
        for d in source_dirs[1:]:
            el(8, "source", d or ".")
        add(7, "</sources>")
        add(6, "</configuration>")
        add(5, "</execution>")
        add(4, "</executions>")
        add(3, "</plugin>")
    if plugins:
        add(2, "</plugins>")
    add(1, "</build>")
    out.append("</project>")
    return "\n".join(out) + "\n"


def _system_jar_path(jc: JarCoordinate) -> str:
    """좌표 없는 jar 의 system scope 경로 — **복사하지 않고** as-is 위치를 최상위 pom 디렉토리 기준으로 가리킨다
    (``${maven.multiModuleProjectDirectory}/<as-is 경로>``, Maven 3.3.1+, sbmig 는 언제나 루트에서 mvn 실행).
    왜 복사하지 않나(2026-10-29): 재구성이 system scope 의존성을 모듈로 옮길 때 jar 를 모듈 ``lib/`` 로 복사하고
    경로를 ``${project.basedir}/lib/<이름>`` 으로 다시 쓰므로, 여기서 루트 ``lib/`` 에 복사해 두면 같은 jar 가 두 번
    남는다. Boot as-is(pom 만) 모드는 디렉토리가 안 움직이니 이 경로가 그대로 맞다.

    The systemPath of an unknown-but-used jar: its as-is path from the top-level pom directory, no copy — the
    restructure copies the jar into the module's `lib/` itself when it moves the dependency."""
    return jc.path


def _final_name(ap: AntProject, packaging: str) -> str:
    """결과 파일 이름(확장자 뺌) — 그 종류의 결과물이 하나이고 속성이 다 풀렸을 때만(Tomcat 컨텍스트 경로 유지)."""
    outs = [a for a in ap.archives if a.kind == packaging and a.raw_destfile]
    if len(outs) != 1:
        return ""
    base = posixpath.basename(outs[0].raw_destfile.replace("\\", "/"))
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return stem if stem and "${" not in stem and re.fullmatch(r"[\w.\-]+", stem) else ""


def _covered(api: tuple, declared: list[tuple[str, set[str]]]) -> bool:
    """선언한 의존성 중 하나가 그 컨테이너 API 를 주는지(좌표, 또는 jar 안의 패키지)."""
    ref, pkgs, _g, _a, _v, keys = api

    def provides(p: str) -> bool:
        # JSTL 은 JSP API 가 아니고, JSP 패키지는 서블릿 API 가 아니다(이름이 javax.servlet. 으로 시작해도)
        if p.startswith("javax.servlet.jsp.jstl") or (ref == "javax.servlet" and p.startswith("javax.servlet.jsp")):
            return False
        return any(p == x or p.startswith(x + ".") for x in pkgs)

    return any(key in keys or any(provides(p) for p in packages) for key, packages in declared)


def synthesize_pom(ws: Any, cfg: Any, *, lookup: Any = None) -> PomSynthesis:
    """Ant 프로젝트에 ``<root>/pom.xml`` 을 쓴다(그 파일 하나만 — 커밋은
    오케스트레이터, build.xml·원래 jar 는 그대로). ``ok=False`` + ``error``: build.xml 없음 / 못 읽음 / XML 아님,
    이미 pom.xml 있음, 소스 루트 없음. ``lookup`` 은 sha1 조회기(None → ``intake.jar_lookup`` 이면
    :class:`~sbmig.analyze.jar_coords.Sha1Lookup` 하나). 예외 없음(예상 못 한 오류도 ``ok=False``).

    Write `<root>/pom.xml` for an Ant project (that file only — no copies; each unknown-but-used jar is listed in
    `warnings`). `ok=False` + `error` when build.xml is missing / unreadable, a pom.xml exists, or no source root is
    found."""
    try:
        return _synthesize(ws, cfg, lookup)
    except Exception as e:  # the intake turns ok=False into the old rejection, never a crash (logged)
        log.exception("pom synthesis failed")
        return PomSynthesis(ok=False, error=f"pom synthesis failed: {type(e).__name__}: {e}")


def _synthesize(ws: Any, cfg: Any, lookup: Any) -> PomSynthesis:
    intake = getattr(cfg, "intake", None)
    root = Path(ws.root)
    ap = describe_ant(ws)
    res = PomSynthesis(ok=False, build_file=ap.build_file or "build.xml", build_xml_notes=list(ap.notes),
                       warnings=list(ap.warnings))
    if ap.error:
        res.error = ap.error
        return res
    if (root / "pom.xml").exists():
        res.error = "pom.xml already exists at the project root (not an Ant-only project)"
        return res
    if not ap.source_roots:
        where = ", ".join(d or "." for j in ap.javac for d in j.srcdirs) or "the tree"
        res.error = f"no Java source root found ({where}: no .java file outside build output / test dirs)"
        return res

    # --- coordinates / levels
    res.group_id = str(getattr(intake, "synth_group_id", "") or "com.company.migrated")
    res.artifact_id = maven_artifact_id(ap.name or _folder_name(ws), maven_artifact_id(_folder_name(ws)))
    res.version = ap.version or str(getattr(intake, "synth_version", "") or "1.0.0-SNAPSHOT")
    res.packaging = "war" if ap.web_root is not None or ap.wars else "jar"
    res.source_dirs = list(ap.source_roots)
    res.web_root = ap.web_root if res.packaging == "war" else None
    res.resource_dirs = [r.dir for r in ap.resources]
    res.lib_dirs = list(ap.lib_dirs)
    floor = java_level(str(getattr(intake, "min_java_source", "1.8") or "1.8")) or 8
    src_n = java_level(ap.java_source) or floor
    tgt_n = java_level(ap.java_target) or src_n
    if src_n < floor or tgt_n < floor:
        res.warnings.append(f"build.xml source / target {ap.java_source or '?'} / {ap.java_target or '?'} raised to "
                            f"{java_level_text(floor)} (JDK 21 compiles 8+ only)")
    src_n, tgt_n = max(src_n, floor), max(tgt_n, floor)
    tgt_n = max(tgt_n, src_n)
    res.java_source = java_level_text(src_n)
    res.encoding = ap.encoding or ap.source_encoding or "UTF-8"

    # --- jars: ① pom.properties ② name table ③ sha1 lookup (④ manifest note)
    table = jar_name_table(cfg)
    lk: Any = lookup
    if lk is None:
        lk = Sha1Lookup(cfg) if getattr(intake, "jar_lookup", True) else False
    scanned: list[tuple[JarCoordinate, JarScan]] = []
    for rel in ap.jar_files:
        try:
            scanned.append(resolve_jar_scan(rel, root / rel, cfg, lk, table=table))
        except Exception as e:  # noqa: BLE001 - one bad jar never stops the synthesis
            scanned.append((JarCoordinate(path=rel, note=f"could not be resolved ({type(e).__name__}: {e})"[:200]),
                            JarScan()))
    jars = [jc for jc, _ in scanned]

    # --- used: Java sources (imports / qualified names) + JSP / XML / properties of the web root and resources
    java_refs = references(ws, ap.source_roots, (".java",))
    extra = _uniq(([ap.web_root] if ap.web_root is not None else []) + [r.dir for r in ap.resources])
    all_refs = java_refs | (references(ws, extra, REF_TEXT_EXT) if extra else set())
    apply_used(jars, all_refs)
    for jc, js in scanned:
        plug = [m for m in js.markers if not m.startswith("taglib") or ap.jsp_count]
        if plug and jc.used is not True:
            jc.used = True                       # used without an import: taglib / ServiceLoader / Spring namespace

    # --- dependencies
    unknown_policy = str(getattr(intake, "unknown_jar", "system") or "system")
    regular: dict[str, _Dep] = {}
    regular_scan: dict[str, JarScan] = {}
    system: list[_Dep] = []
    system_keys: set[str] = set()
    seen_sha1: dict[str, str] = {}
    war_excludes: list[str] = []
    web_lib = _join(ap.web_root, "WEB-INF/lib") if ap.web_root is not None else None
    from sbmig.plan.versions import compare_versions

    def exclude_from_war(path: str) -> None:
        if web_lib is not None and posixpath.dirname(path) == web_lib:
            war_excludes.append("WEB-INF/lib/" + posixpath.basename(path))

    for jc, js in scanned:
        if jc.sha1 and jc.sha1 in seen_sha1:
            res.warnings.append(f"{jc.path} is the same jar as {seen_sha1[jc.sha1]}: declared once")
            if jc.key and jc.version:
                exclude_from_war(jc.path)
            continue
        if jc.sha1:
            seen_sha1[jc.sha1] = jc.path
        if jc.group_id and jc.artifact_id and jc.version:
            key = jc.key
            cur = regular.get(key)
            if cur is not None:
                newer = compare_versions(jc.version, cur.version)
                keep_new = newer is not None and newer > 0
                lo, hi = (cur, jc.version) if keep_new else (None, cur.version)
                res.warnings.append(f"{key}: several versions ({cur.version} in {cur.path}, {jc.version} in "
                                    f"{jc.path}) — {hi} declared")
                exclude_from_war(jc.path)
                if keep_new and lo is not None:
                    regular[key] = _Dep(jc.group_id, jc.artifact_id, jc.version, jc.scope, path=jc.path)
                    regular_scan[key] = js
                continue
            regular[key] = _Dep(jc.group_id, jc.artifact_id, jc.version, jc.scope, path=jc.path)
            regular_scan[key] = js
            exclude_from_war(jc.path)
            continue
        # 좌표(버전 포함)가 없는 jar
        if is_container_api(js.packages):
            res.dropped_jars.append(jc.path)
            exclude_from_war(jc.path)
            res.warnings.append(f"{jc.path}: a copy of the servlet / JSP / EL API (Tomcat supplies it) — not declared")
            continue
        if jc.used is False:
            res.dropped_jars.append(jc.path)
            continue
        res.unknown_jars.append(jc.path)
        if unknown_policy != "system":
            res.warnings.append(f"{jc.path}: no Maven coordinates — not declared (intake.unknown_jar: skip)")
            continue
        stem, file_version = stem_and_version(jc.path)
        g = jc.group_id or res.group_id
        a = jc.artifact_id or maven_artifact_id(stem, "lib")
        if f"{g}:{a}" in system_keys or f"{g}:{a}" in regular:
            a = f"{a}-{(jc.sha1 or 'x')[:7]}"
        system_keys.add(f"{g}:{a}")
        why = ("in the jar name table but the file name has no version" if jc.source == "name_table"
               else "no pom.properties, not in the jar name table")
        lookup_note = next((n.strip() for n in jc.note.split(";") if "sha1" in n), "")
        comment = (f"{posixpath.basename(jc.path)}: Maven coordinates unknown ({why}"
                   + (f"; {lookup_note}" if lookup_note else "") + ") — system scope keeps the build passing; "
                   "install the jar into Nexus and replace this entry")
        lib_rel = _system_jar_path(jc)
        v = file_version or jc.version or "0"
        system.append(_Dep(g, a, v, "system", system_path=SYSTEM_PATH_BASE + "/" + lib_rel, comment=comment,
                           path=jc.path))
        # jar 표에 선언한 좌표 그대로(source 는 unknown 유지): 인수인계의 install-file 명령이 pom 의 system 항목과
        # 같은 좌표를 써야 사람이 바꿔 끼울 수 있다(이름 충돌로 붙인 꼬리까지)
        jc.group_id, jc.artifact_id, jc.version, jc.scope = g, a, v, "system"
        jc.note = "; ".join(x for x in (jc.note, f"declared with system scope as {g}:{a}:{v} ({lib_rel})") if x)

    # --- container APIs the Java sources use and no declared jar provides (Ant compiled against Tomcat's lib)
    declared = [(k, regular_scan[k].packages) for k in regular] + [
        (f"{d.group_id}:{d.artifact_id}", next((js.packages for jc, js in scanned if jc.path == d.path), set()))
        for d in system]
    implied: list[_Dep] = []
    for api in IMPLIED_APIS:
        ref, _pkgs, g, a, v, _keys = api
        needs = any((x == ref or x.startswith(ref + ".")) and not x.startswith("javax.servlet.jsp.jstl")
                    for x in java_refs)
        if needs and not _covered(api, declared) and f"{g}:{a}" not in regular:
            implied.append(_Dep(g, a, v, "provided", comment=f"the sources use {ref} and no jar of the project "
                                f"provides it (Ant compiled against the container's lib): added as provided"))
            res.warnings.append(f"sources use {ref} but no project jar provides it: {g}:{a}:{v} added as provided")

    deps = sorted(regular.values(), key=lambda d: (d.scope == "provided", d.group_id, d.artifact_id))
    deps = deps + implied + system
    if ap.web_root is not None and any(j.destdir == _join(ap.web_root, "WEB-INF/classes") for j in ap.javac):
        war_excludes.append("WEB-INF/classes/**")
        res.warnings.append(f"javac writes into {_join(ap.web_root, 'WEB-INF/classes')}: those class files are left "
                            "out of the WAR (Maven compiles the sources itself)")
    res.jars = jars
    text = render_pom(group_id=res.group_id, artifact_id=res.artifact_id, version=res.version,
                      packaging=res.packaging, name=ap.name, build_file=res.build_file, java_source=res.java_source,
                      java_target=java_level_text(tgt_n), encoding=res.encoding, source_dirs=res.source_dirs,
                      resources=ap.resources, web_root=res.web_root, web_xml=ap.web_xml,
                      has_web_xml=ap.has_web_xml or res.packaging != "war",
                      war_excludes=_uniq(war_excludes) if res.packaging == "war" else [],
                      final_name=_final_name(ap, res.packaging), deps=deps)
    write = getattr(ws, "write", None)
    if callable(write):
        write("pom.xml", text, encoding="utf-8")
    else:
        (root / "pom.xml").write_text(text, encoding="utf-8")
    res.pom_path = "pom.xml"
    res.ok = True
    res.warnings = _uniq(res.warnings)
    log.info("pom.xml synthesized from %s: %s:%s:%s (%s), %d source root(s), %d jar(s), %d unknown, %d dropped",
             res.build_file, res.group_id, res.artifact_id, res.version, res.packaging, len(res.source_dirs),
             len(jars), len(res.unknown_jars), len(res.dropped_jars))
    return res
