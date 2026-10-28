"""분석기의 특이 사항 탐지(ERROR-HANDLING.md E7, S3–S6, X6) — 실행을 막지 않고 ``profile.warnings`` 와
``LibraryUsage`` 로만 남긴다(차단 정책 없음; 보고서·구조 검토·수정 스크립트 프롬프트가 읽는다). 흐름상 위치:
``analyze_project`` 가 부른다. 재구성도 :func:`resolve_system_path` 를 쓴다(system jar 옮기기). 핵심 진입점:
:func:`system_dependencies` / :func:`system_dep_findings` (system scope jar → 사내 Nexus 로 옮기라는 안내),
:func:`language_findings` (Kotlin / Groovy / Scala, 소스 생성 플러그인), :func:`normalize_java_version` /
:func:`java_level_findings` (Java 1.6 / 1.7 프로젝트: JDK 21 은 8 미만을 못 컴파일하지만 거부하지 않고
``UpgradeToJava21`` + 수정 루프에 맡긴다 — 회사에 오래된 프로젝트가 많다), :func:`detect_encoding` /
:func:`encoding_findings` (``sourceEncoding`` 선언 없이 cp949 / euc-kr 소스). 전부 읽기 전용, 예외 없음.

Edge-case detection for the analyzer (ERROR-HANDLING.md E7, S3–S6, X6).

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

from .pom_parser import (
    WorkspaceView,
    _child,
    _children,
    _text,
    interpolate,
    is_skipped_path,
    pom_dir,
    read_pom_text,
    xml_without_declaration,
)

CODEGEN_PLUGINS = ("protobuf-maven-plugin", "jaxb2-maven-plugin", "openapi-generator-maven-plugin",
                   "maven-jaxb2-plugin", "querydsl-maven-plugin",
                   # round 10: WSDL → Java generators (SOAP clients of 2013–2020 Korean enterprise code)
                   "cxf-codegen-plugin", "jaxws-maven-plugin")
# 플러그인별 안내 문구(경고 끝에 붙는다). 주의: cxf 문구의 "handler jakarta-library-floor" 는 v1 핸들러
# 이름이다(v2 에는 그 핸들러가 없음 — 문구만 남아 있음)
# plugin-specific advice, keyed by "groupId:artifactId" (or artifactId); versions verified on Maven Central 2026-10-26
CODEGEN_HINTS = {
    "org.apache.cxf:cxf-codegen-plugin": (
        "CXF 3.x generates javax.xml.ws / javax.jws code; 4.x generates jakarta.* — keep cxf-codegen-plugin on the same "
        "4.x line as the org.apache.cxf runtime (handler jakarta-library-floor raises both to 4.2.4, "
        "knowledge/libraries/jakarta_libraries.yml)"),
    "org.codehaus.mojo:jaxws-maven-plugin": (
        "org.codehaus.mojo:jaxws-maven-plugin (last 2.6) runs JAX-WS 2.x wsimport and generates javax.xml.ws code that "
        "no longer compiles after the jakarta migration — move to com.sun.xml.ws:jaxws-maven-plugin 4.0.5 (jakarta "
        "wsimport; wsdlDirectory / wsdlFiles / packageName / sourceDestDir keep their names) plus the "
        "com.sun.xml.ws:jaxws-rt runtime, or regenerate with cxf-codegen-plugin 4.x"),
    "com.sun.xml.ws:jaxws-maven-plugin": (
        "com.sun.xml.ws:jaxws-maven-plugin below 3.0 generates javax.xml.ws code; 4.0.x (latest 4.0.5) generates "
        "jakarta.xml.ws code"),
}
LANGUAGES = (("kotlin", "*.kt", "Kotlin"), ("groovy", "*.groovy", "Groovy"), ("scala", "*.scala", "Scala"))
LANGUAGE_PLUGINS = {"kotlin": ("kotlin-maven-plugin",), "groovy": ("gmavenplus-plugin", "gmaven-plugin"),
                    "scala": ("scala-maven-plugin", "maven-scala-plugin")}
# 인코딩 후보는 이 순서로 엄격 디코딩: UTF-8 이 먼저(ASCII
# 만 있는 파일도 utf-8), 다음이 한국 레거시 코드의 cp949
ENCODING_CANDIDATES = ("utf-8", "cp949", "euc-kr")
_BASEDIR_RE = re.compile(r"\$\{(?:project\.)?basedir\}")


# ---------------------------------------------------------------------------
# system-scoped dependencies (E7)
# ---------------------------------------------------------------------------
@dataclass
class SystemDep:
    """``<scope>system</scope>`` 의존성 하나: 선언한 pom, 좌표, 원래
    ``<systemPath>``, 풀린 경로, 프로젝트 안인지, 파일이 있는지."""
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
        """좌표 키 ``groupId:artifactId``."""
        return f"{self.group_id}:{self.artifact_id}"


def resolve_system_path(raw: str, declaring_dir: str, props: Optional[dict[str, str]] = None,
                        root: Optional[Path] = None) -> tuple[Optional[str], bool]:
    """``declaring_dir`` 의 pom 이 선언한 ``<systemPath>`` 를 푼다. 반환 (경로, 프로젝트 안인지):
    프로젝트 안이면 루트 기준 posix 상대 경로, 밖이면 절대 경로(False), 속성을 못 풀면 (None, False).
    ``${basedir}`` / ``${project.basedir}`` 는 선언한 pom 의 디렉토리다(루트가 아니다).

    Resolve a `<systemPath>` declared by a pom in `declaring_dir` (project-relative, '' = root).

    Returns (path, inside): a project-relative posix path when the jar lies inside the project, the absolute path
    (inside=False) when it points elsewhere, or (None, False) when a property cannot be resolved."""
    value = (raw or "").strip()
    rel_base = declaring_dir or "."
    # basedir 는 "선언한 pom 의 디렉토리" 여야 하므로 먼저 표식으로 바꿔 둔다 — 다른 속성을 푸는 interpolate
    # 가 basedir 를 엉뚱한 값(루트 등)으로 풀지 않게
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
    """pom 의 모든 ``<dependency>`` 요소: dependencies,
    dependencyManagement, 그리고 각 profile 안의 것까지."""
    yield from _children(_child(root, "dependencies"), "dependency")
    dm = _child(root, "dependencyManagement")
    yield from _children(_child(dm, "dependencies"), "dependency")
    for prof in _children(_child(root, "profiles"), "profile"):
        yield from _children(_child(prof, "dependencies"), "dependency")


def system_dependencies(ws: WorkspaceView, poms: list[PomInfo]) -> list[SystemDep]:
    """모든 pom 의 system scope 의존성(또는 ``<systemPath>`` 가 있는 의존성)을
    찾아 경로를 풀고 파일 존재를 확인한다. 속성은 저장소 안 부모 체인(부모 →
    자식 순으로 덮어씀)에서 모은다. 읽을 수 없는 pom 은 건너뛴다."""
    root = Path(ws.root)
    out: list[SystemDep] = []
    for p in poms:
        # pom_parser 와 같은 길로 읽는다(A1): EUC-KR / MS949 선언 pom 이 이제 프로필에 들어오므로 여기서도 선언을 떼야
        # expat 의 ValueError("multi-byte encodings are not supported")로 분석 전체가 죽지 않는다
        try:
            tree = ET.fromstring(xml_without_declaration(read_pom_text(ws, p.path)).encode("utf-8"))
        except (ET.ParseError, OSError, ValueError, LookupError):
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
    """system scope 의존성마다 경고 한 줄(사내 저장소 Nexus 에 올려 일반 의존성으로 바꾸라는 안내)과
    ``system-scope-jars`` LibraryUsage(근거: pom 좌표, jar 파일). 없으면 ([], None)."""
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
    """``pattern`` 에 맞는 파일(빌드 출력 등 건너뛸 경로 제외, posix 경로). 실패하면 빈 목록."""
    try:
        return [f.replace("\\", "/") for f in ws.glob(pattern) if not is_skipped_path(f.replace("\\", "/"))]
    except Exception:  # pragma: no cover - defensive
        return []


def language_findings(ws: WorkspaceView, poms: list[PomInfo]) -> tuple[list[str], list[LibraryUsage]]:
    """Java 가 아닌 JVM 언어(Kotlin / Groovy / Scala: 파일 또는 플러그인)와 소스 생성 플러그인(protobuf,
    jaxb2, openapi, querydsl, cxf / jaxws wsimport)을 경고 + LibraryUsage 로. sbmig 는 Java 만 고친다 —
    생성 코드는 생성기 자체가 jakarta / Java 21 코드를 내야 하므로 플러그인 버전을 사람이 봐야 한다."""
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
        groups = sorted({pl.group_id or "" for p in poms for pl in p.plugins if pl.artifact_id == aid})
        hint = next((CODEGEN_HINTS[f"{g}:{aid}"] for g in groups if f"{g}:{aid}" in CODEGEN_HINTS), None)
        warnings.append(f"source generator plugin {aid}{':' + ver if ver else ''} in {', '.join(hits)}: generated "
                        "sources are not rewritten (the generator itself must emit jakarta/Java 21 compatible code; "
                        "check the plugin version and its jakarta options)" + (f" — {hint}" if hint else ""))
        libs.append(LibraryUsage(name=f"codegen:{aid}", version=ver, evidence=[f"pom:{h}" for h in hits]
                                 + [f"plugin:{g}:{aid}" for g in groups if g]))
    return warnings, libs


# ---------------------------------------------------------------------------
# java version (S-legacy levels)
# ---------------------------------------------------------------------------
def normalize_java_version(v: Optional[str]) -> Optional[str]:
    """Java 버전 표기 통일: '1.7' / '7' / '1.7.0' → '1.7' (7 이하는 1.x 표기), 그 밖의 값은 그대로.

    '1.7' / '7' / '1.7.0' → '1.7' (levels ≤ 7 use the 1.x spelling); other values unchanged."""
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
    """JDK 21 이 더 이상 컴파일하지 못하는 소스 수준(1.1 … 1.7)이면 True."""
    n = normalize_java_version(v)
    return n in ("1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7")


def java_level_findings(poms: list[PomInfo]) -> list[str]:
    """Java ≤ 1.7 수준을 선언한 pom 마다 경고 한 줄. 거부하지 않는다: ``UpgradeToJava21`` 이 release 21
    로 올리고, 제거된 JDK API·``_`` / ``enum`` 식별자 같은 컴파일 오류는 수정 루프(스크립트)가 고친다."""
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
    """후보 인코딩으로 순서대로 엄격 디코딩해 처음 성공한 것을
    돌려준다(BOM 이 있으면 ``utf-8-sig``). 모두 실패하면 None.

    Strict decode attempts in order; None when no candidate decodes the bytes."""
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
    """pom 들이 선언한 소스 인코딩: ``project.build.sourceEncoding`` 속성, 없으면 maven-compiler-plugin 의
    ``<encoding>``. 처음 찾은 값(없으면 None)."""
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
    """``sourceEncoding`` 선언이 없는데 UTF-8 이 아닌 자바 소스가 있으면 경고 + ``legacy-encoding``
    LibraryUsage. 왜 중요한가: JDK 21 은 기본이 UTF-8 이라 cp949 소스를 경고만 내고 깨진 문자열로
    컴파일한다(빌드 오류로는 절대 드러나지 않음). 구조 검토도 같은 점을 치명(critical)으로 본다.
    주의: 경고 문구의 ``convert-utf8`` 은 v1 핸들러 이름이다(v2 에는 없음)."""
    # 선언이 있으면 javac 가 그 인코딩으로 읽으므로 문제 없음 — 파일을 하나도 열지 않는다
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


__all__ = ["CODEGEN_HINTS", "CODEGEN_PLUGINS", "SystemDep", "declared_source_encoding", "detect_encoding", "encoding_findings",
           "java_level_findings", "language_findings", "legacy_java_level", "normalize_java_version",
           "resolve_system_path", "system_dep_findings", "system_dependencies"]
