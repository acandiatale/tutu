"""Maven pom 그래프 파서 — 네임스페이스 무관, 네트워크·mvn 호출 없이 저장소 안의 pom.xml 만으로
PomInfo 를 만든다. 흐름상 위치: 분석의 첫 단계(``analyze_project`` → :func:`parse_poms` →
:func:`module_order`). 집계 pom 계획, 권고 검증, 구조 검토 입력, 인수인계 보고서도 다시 부른다.
두 단계: (1) pom 마다 원시 파싱(_RawPom), (2) 저장소 안 부모 체인 연결(relativePath + GAV, 그다음 GAV 만으로)
→ 유효 속성 → ``${prop}`` 치환 → dependencyManagement 로 의존성 버전 채우기 → Java / Boot / Spring 버전 도출.
한계: 저장소 밖 부모(사내 ``base-parent`` 등)와 import 된 BOM 은 읽지 않으므로 거기서 오는 값은 모른다(mvn 의
effective-pom 은 접수 단계가 따로 캡처해 버전 이어받기에
쓴다). 깨진 pom 은 경고 후 건너뛴다(예외로 멈추지 않음).

Maven pom graph parser (namespace-aware, no network, no mvn).

Two passes:
1. parse every pom.xml into a `_RawPom` (own coordinates, parent, properties, deps, depMgmt, plugins, modules);
2. link the in-repo parent chain (relativePath + GAV, then GAV against all repo poms), compute effective
   properties (parent chain + own + well-known project.* keys), interpolate `${prop}` and fill dependency
   versions from the effective dependencyManagement, then derive java / Boot / Spring versions.


"""
from __future__ import annotations

import logging
import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol

from sbmig.core.models import GAV, PluginInfo, PomInfo
from sbmig.core.workspace import is_build_output_path

log = logging.getLogger("sbmig.analyze")

SKIP_DIR_PARTS = {"node_modules", ".git", ".idea", ".svn"}     # + Maven build output (`target/` beside a pom)
BOOT_GROUP = "org.springframework.boot"
BOOT_PARENTS = {"spring-boot-starter-parent", "spring-boot-parent"}
SPRING_CORE_ARTIFACTS = ("spring-core", "spring-context", "spring-webmvc", "spring-web", "spring-beans")
COMPILER_PLUGIN = "maven-compiler-plugin"
_PROP_RE = re.compile(r"\$\{([^}]+)\}")


class WorkspaceView(Protocol):
    """분석기가 쓰는 ``Workspace`` 의 최소 기능(root, glob,
    read, pom_files) — 실행 작업공간과 PathView 둘 다 맞는다.

    The subset of `Workspace` the analyzer needs."""
    root: Any

    def glob(self, pattern: str) -> list[str]: ...
    def read(self, rel: Any, encoding: str = "utf-8") -> str: ...
    def pom_files(self) -> list[str]: ...


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def is_skipped_path(rel: str) -> bool:
    """분석에서 뺄 경로인지: node_modules / .git / .idea / .svn, pom 옆 Maven 빌드 출력(``target/``),
    그리고 ``src/test/resources`` (테스트 픽스처 pom 을 프로젝트 모듈로 오인하지 않게)."""
    parts = rel.replace("\\", "/").split("/")
    if any(p in SKIP_DIR_PARTS for p in parts) or is_build_output_path(rel):
        return True
    return "src/test/resources" in rel.replace("\\", "/")


def _local(tag: Any) -> str:
    """``{네임스페이스}이름`` 에서 이름만 — 네임스페이스 없는 옛 pom 과 있는 pom 을 똑같이 읽기 위해."""
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
    """자식 이름을 차례로 따라 내려간 요소(중간에 없으면 None)."""
    cur = el
    for n in names:
        cur = _child(cur, n)
        if cur is None:
            return None
    return cur


def interpolate(value: Optional[str], props: dict[str, str], depth: int = 0) -> Optional[str]:
    """``${key}`` 를 ``props[key]`` 로 치환(재귀, 최대 깊이 10 — 순환 참조 방지). 모르는 키는 그대로 남긴다.

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
    """플러그인 ``<configuration>`` 을 1단계 dict 로: 단순 값은
    문자열, 자식이 모두 단순 값이면 목록, 더 깊은 것은 버린다."""
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
    """pom 하나의 원시 파싱 결과(치환 전). 부모 연결(``parent_raw``)과 유효 속성은 2단계에서 채운다."""
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
    """``<dependency>`` 하나(artifactId 가 없으면 None)."""
    g, a = _text(el, "groupId"), _text(el, "artifactId")
    if not a:
        return None
    return _RawDep(group_id=g or "", artifact_id=a, version=_text(el, "version"), scope=_text(el, "scope"),
                   type=_text(el, "type"), classifier=_text(el, "classifier"),
                   optional=(_text(el, "optional") or "").lower() == "true")


def _parse_plugins(container: Optional[ET.Element], managed: bool) -> list[_RawPlugin]:
    """``<plugins>`` 의 플러그인들(groupId 가 없으면 Maven 기본 ``org.apache.maven.plugins``)."""
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
    """pom 텍스트의 ``<repository>`` / ``<pluginRepository>`` URL 들(profile
    포함, 주석·distributionManagement 제외, 순서 유지, 중복 제거). settings 외에 Maven 이 접속하는 저장소 —
    폐쇄망 호스트에서 닿지 않는 곳(RUNBOOK §0.1).

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


def parse_pom_text(text: str, path: str) -> _RawPom:
    """pom 하나를 원시 파싱한다. XML 이 깨졌거나 ``<project>`` 가 아니거나 artifactId 가 없으면 ValueError.

    Parse one pom; raises ValueError on malformed XML or a missing artifactId."""
    # 주의: 텍스트를 UTF-8 바이트로 다시 넘기므로 ``encoding="EUC-KR"`` 같은 멀티바이트 선언이 있는 pom 은
    # expat 이 ValueError("multi-byte encodings are not supported")를 내고 parse_poms 가 그 pom 을 건너뛴다
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
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
    """작업공간의 pom.xml 경로들(정렬, posix, 건너뛸 경로 제외)."""
    try:
        files = ws.pom_files()
    except Exception:  # pragma: no cover - defensive
        files = ws.glob("pom.xml")
    return sorted(f.replace("\\", "/") for f in files if not is_skipped_path(f))


def _resolve_parent(raw: _RawPom, by_path: dict[str, _RawPom], all_raw: list[_RawPom]) -> Optional[_RawPom]:
    """저장소 안에서 ``raw`` 의 부모 pom 을 찾는다: Maven 처럼 relativePath(없으면
    ``../pom.xml``)를 먼저 보고 좌표가 맞는지 확인, 아니면 저장소 전체에서
    좌표(artifactId, 그룹·버전이 모순되지 않음)로. 없으면 None(외부 부모)."""
    if raw.parent is None:
        return None

    def gav_match(c: _RawPom) -> bool:
        """``c`` 가 선언된 부모 좌표와 모순되지 않는지(artifactId
        같음; 그룹·버전은 둘 다 구체적일 때만 비교)."""
        if c is raw or c.artifact_id != raw.parent.artifact_id:  # type: ignore[union-attr]
            return False
        if raw.parent.group_id and c.eff_group and c.eff_group != raw.parent.group_id:  # type: ignore[union-attr]
            return False
        pv, cv = raw.parent.version, c.eff_version  # type: ignore[union-attr]
        if pv and cv and "${" not in pv and "${" not in cv and pv != cv:
            return False
        return True

    rel = raw.parent_relative_path
    # 요소가 없으면 Maven 기본값 ``../pom.xml``. 빈 ``<relativePath/>``
    # 는 경로 확인을 건너뛰고 아래의 좌표 검색만 한다
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
    """``[최상위 조상, …, raw]`` (순환 안전).

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
    """유효 속성: 부모 체인 위에서부터 덮어쓴 ``<properties>`` + 잘 알려진 ``project.*`` / ``parent.*`` 키,
    ``basedir`` 는 "." — 마지막에 속성끼리의 참조를 한 번 풀어 둔다."""
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
    """원시 의존성을 속성 치환한 GAV 로."""
    return GAV(group_id=interpolate(d.group_id, props) or "", artifact_id=interpolate(d.artifact_id, props) or "",
               version=interpolate(version if version is not None else d.version, props),
               scope=interpolate(d.scope, props), type=interpolate(d.type, props),
               classifier=interpolate(d.classifier, props), optional=d.optional)


def _effective_managed(raw: _RawPom) -> dict[str, GAV]:
    """부모 체인 전체의 dependencyManagement(아래 pom 이 위를 덮어씀) — 버전 없는 의존성의
    버전을 채우는 데 쓴다. 저장소 밖 부모·import 된 BOM 의 관리 버전은 모른다."""
    out: dict[str, GAV] = {}
    for p in _chain(raw):
        for d in p.managed:
            g = _to_gav(d, p.effective_props)
            out[g.key] = g
    return out


def _effective_plugin_mgmt(raw: _RawPom) -> dict[str, tuple[Optional[str], dict[str, Any]]]:
    """부모 체인의 플러그인 선언 → ``group:artifact`` 별 (버전,
    합친 configuration). 자식의 버전 없는 플러그인을 채운다."""
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
    """풀리지 않은 ``${...}`` 가 남은 값은 버전으로 치지 않는다."""
    if not v or "${" in v:
        return None
    return v


def build_pom_infos(raws: list[_RawPom]) -> list[PomInfo]:
    """2단계: 부모 연결 → 유효 속성(위에서 아래로) → 치환·관리 버전 채우기 → PomInfo. 입력 순서대로 돌려준다.
    Java / Boot / Spring 버전은 자기 pom 에서 못 찾으면 저장소 안 부모의 값을 물려받는다."""
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
            # pluginManagement 에만 있는 Boot 플러그인은 세지 않는다
            # — 실제로 ``<build><plugins>`` 에 있어야 Boot 앱이다
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
    """Java 수준: maven-compiler-plugin 의 release / source / target 이 먼저, 그다음 ``maven.compiler.*`` /
    ``java.version`` 속성."""
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
    """Boot 버전: Boot 부모 → spring-boot-dependencies BOM →
    ``spring-boot.version`` 속성 → Boot 의존성의 버전 순."""
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
    """Spring Framework 버전: 속성(``spring-framework.version``
    / ``spring.version``) → 핵심 spring-* 의존성 → BOM."""
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
    """작업공간의 모든 pom 을 파싱한다. (PomInfo 목록, 경고) — 파싱 못 한 pom 은 경고를 남기고 건너뛴다.

    Parse every pom in the workspace. Returns (poms, warnings); unparsable poms are skipped with a warning."""
    warnings: list[str] = []
    raws: list[_RawPom] = []
    for rel in find_pom_files(ws):
        try:
            text = ws.read(rel)
            raw = parse_pom_text(text, rel)
            raw.repositories = repository_urls(text)
            raws.append(raw)
        except (ValueError, OSError) as e:
            msg = f"pom skipped (parse failure): {e}"
            log.warning(msg)
            warnings.append(msg)
    return build_pom_infos(raws), warnings


def module_order(poms: Iterable[PomInfo]) -> list[str]:
    """reactor 비슷한 pom 경로 순서: 루트 집계 pom 에서 ``<modules>``
    를 따라 전위 DFS, 어디에도 안 걸린 pom 은 경로순으로 뒤에.

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
    """pom 경로의 디렉토리(최상위 pom.xml 이면 '').

    Directory of a pom path ('' for the top-level pom.xml)."""
    return posixpath.dirname(path)


def owning_pom(file_path: str, poms: Iterable[PomInfo]) -> Optional[PomInfo]:
    """``file_path`` 를 품는 pom 중 디렉토리가 가장 깊은 것(= 그 파일이 속한 모듈).

    The pom whose directory is the deepest prefix of `file_path`."""
    best: Optional[PomInfo] = None
    best_len = -1
    for p in poms:
        d = pom_dir(p.path)
        if d == "" or file_path == d or file_path.startswith(d + "/"):
            if len(d) > best_len:
                best, best_len = p, len(d)
    return best
