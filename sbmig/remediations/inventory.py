"""IVAS가 찾은 취약점을 Maven이 실제로 고른 의존성(``ResolvedModel``)과 pom의 버전 문자열 위치에 연결하는 모듈.
그래프의 ``inventory`` 노드가 호출하고, planner가 결과 ``Inventory``로 변경을 제안한다.
- 모듈마다 버전의 출처를 정한다. 직접 쓴 버전은 ``direct_literal``, 프로젝트 property면 ``direct_property``다
- 외부 부모의 property면 ``parent_managed``다. ``direct_property``의 위치는 property를 정의한 줄이다
- 선언에 버전이 없고 프로젝트 pom의 dependencyManagement에 있으면 ``own_management``다(import 제외)
- 선언도 관리 항목도 없이 다른 의존성을 거쳐 들어왔으면 ``transitive``이고, ``via``에 그 경로를 둔다
- 프로젝트에 버전이 없으면 BOM 하나뿐일 때 ``bom_managed``, 외부 부모뿐일 때 ``parent_managed``다. 둘 다면 출처 불명이다
- 선언한 버전이 Maven이 고른 버전과 다르면 고쳐도 소용없으므로 ``unmapped``다
- ``PomLocation``은 pom을 다시 읽어 그 줄에 그 문자열이 있는지 확인한 뒤에만 만든다
- 고칠 수 있는 항목은 (g:a, 버전)의 위치마다 하나다. ``unmapped``는 문제를 숨기지 않도록 합치지 않는다
- 나머지 모듈은 가장 많은 모듈을 다루는 항목의 ``also_modules``에 넣는다. 검증에서 실패로 치지 않고 다음 라운드에서 계획한다
- 사내 아티팩트를 거쳐 들어온 모듈은 묶기 전에 따로 나눈다
- IVAS와 Maven 표기는 ``matching``으로 비교한다. 버전의 대소문자만 다르면 같은 라이브러리다
- 의존성 결과에 없는 라이브러리는 ``plugin`` -> ``vendored`` -> ``unmapped`` 순으로 분류한다. 파일 컴포넌트는 항상 ``vendored``다

Origin per module, in this order: 1. a version on the declaration in the module's or an in-project parent's
`<dependencies>`: `direct_literal`; a
   project property: `direct_property` (location = the property definition line); an external parent's
   property: `parent_managed`
2. no version on the declaration, an entry in a project pom's `<dependencyManagement>` (imports excluded):
   `own_management`
3. neither declared nor managed, reached through another dependency: `transitive` (`via` = the path)
4. a direct dependency without a version in the project: one BOM -> `bom_managed`, external parents only ->
   `parent_managed`, both or several -> unknown provenance; `managed_by` is used first when given
5. a declared version that is not the one Maven resolved: `unmapped` (editing it would change nothing)
Folding: one item per distinct editable location; non-editable module results fold into the item that governs
most modules and are listed in its `also_modules` (the edit is not known to govern them; verification does not
fail for them, the next round plans them); `unmapped` results never fold. File-component findings are always
`vendored`. Also holds the small lxml pom model `ProjectPoms` that `maven.py` reuses for module discovery.
"""
from __future__ import annotations

import logging
import posixpath
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

from .inhouse import inhouse_on_path
from .matching import ga_key, same_version, version_key
from .models import (
    O_BOM_MANAGED,
    O_DIRECT_LITERAL,
    O_DIRECT_PROPERTY,
    O_OWN_MANAGEMENT,
    O_PARENT_MANAGED,
    O_PLUGIN,
    O_TRANSITIVE,
    O_UNMAPPED,
    O_VENDORED,
    Finding,
    Inventory,
    InventoryItem,
    ModuleSource,
    PomLocation,
    ResolvedDep,
    ResolvedModel,
)

log = logging.getLogger("sbmig.remediations.inventory")

# ${a} -> ${b}로 이어지는 단계는 이 정도면 충분하고, 순환 참조도 여기서 끊긴다
MAX_PROPERTY_DEPTH = 5
EDITABLE = (O_DIRECT_LITERAL, O_DIRECT_PROPERTY, O_OWN_MANAGEMENT)
_FULL_PROP = re.compile(r"^\$\{([^}]+)\}$")
_BUILTIN_PREFIXES = ("project.", "pom.", "env.", "settings.", "maven.")
_BUILTIN_NAMES = {"version", "groupId", "artifactId", "basedir"}
FILE_KINDS = ("file_component", "file_component_potential")
# 파일 컴포넌트로 만든 vendored 항목의 설명. report 모드는 이 설명의 항목을 남은 항목으로 세지 않는다
FILE_COMPONENT_REASON = "file component in the tree; not changed automatically"


def is_file_residual(r: Any) -> bool:
    """조치하지 못한 항목이 IVAS 파일 컴포넌트에서 온 것인지 확인한다.
    ``vendored`` 출처이고 설명이 ``FILE_COMPONENT_REASON``이면 True다.

    States written under `remediation.file_components: count` (or before the setting) hold file findings as
    such residuals."""
    return getattr(r, "origin", "") == O_VENDORED and str(getattr(r, "detail", "") or "") == FILE_COMPONENT_REASON
# Maven이 고른 의존성이 jar가 아니거나 classifier가 있을 때 reason에 붙는 표시.
# planner는 이 표시가 있는데 변종 정보가 없는 항목에는 override 항목을 넣지 않는다
VARIANT_MARK = "type/classifier variant"


# ---------------------------------------------------------------------------- lxml 도우미
def _local(el: Any) -> str:
    tag = getattr(el, "tag", None)
    if not isinstance(tag, str):          # 주석과 처리 명령은 tag가 함수이므로 건너뛴다
        return ""
    return tag.rsplit("}", 1)[-1]


def _kid(el: Any, name: str) -> Any:
    if el is None:
        return None
    for c in el:
        if _local(c) == name:
            return c
    return None


def _kids(el: Any, name: str) -> list[Any]:
    if el is None:
        return []
    return [c for c in el if _local(c) == name]


def _text(el: Any) -> str:
    return (el.text or "").strip() if el is not None else ""


def _parser() -> Any:
    # 업로드된 ZIP의 pom은 믿을 수 없는 입력이므로 외부 엔티티, DTD, 네트워크를 끈다
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


# ---------------------------------------------------------------------------- pom 모델
@dataclass
class PomFile:
    """프로젝트 안의 pom 하나를 줄 번호와 함께 담는 데이터 클래스.
    ``dir``은 프로젝트 기준 폴더("."은 루트), ``rel``은 pom 경로다.
    ``parent``는 프로젝트 안에서 찾은 부모다. 이것이 None인데 ``parent_gav``가 있으면 외부 부모다."""
    dir: str
    rel: str
    path: Path
    root: Any = None
    error: str = ""
    lines: list[str] = field(default_factory=list)
    group_id: str = ""
    artifact_id: str = ""
    version: str = ""
    packaging: str = "jar"
    parent_gav: tuple[str, str, str] | None = None
    parent_version_el: Any = None
    relative_path: str | None = None      # None은 기본값 ../pom.xml, 빈 문자열은 빈 <relativePath/>를 뜻한다
    parent: PomFile | None = None
    props: dict[str, Any] = field(default_factory=dict)
    modules: list[str] = field(default_factory=list)
    in_reactor: bool = False

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"

    @property
    def ok(self) -> bool:
        return self.root is not None and not self.error


def _pom_rel(d: str) -> str:
    return "pom.xml" if d in ("", ".") else f"{d}/pom.xml"


def _norm_dir(base: str, rel: str) -> str | None:
    """``base`` 폴더 기준의 ``rel``을 프로젝트 기준 폴더로 바꾼다. 프로젝트 밖(..)이면 None을 반환한다."""
    joined = posixpath.normpath(posixpath.join("" if base == "." else base, rel.replace("\\", "/")))
    if joined.startswith("..") or joined.startswith("/"):
        return None
    return "." if joined in ("", ".") else joined


def normalize_roots(roots: list[str] | None) -> list[str]:
    """reactor 루트 목록을 프로젝트 기준 폴더로 정리한다.
    프로젝트 밖(``..``)과 절대 경로는 버리고, 순서를 유지하며 중복을 뺀다.
    목록이 비었거나 남는 것이 없으면 ``["."]``을 반환한다."""
    out: list[str] = []
    for r in roots or []:
        nd = _norm_dir(".", str(r).strip().strip("/") or ".")
        if nd is not None and nd not in out:
            out.append(nd)
    return out or ["."]


def parse_pom_file(project: Path, d: str) -> PomFile:
    """``project/<d>/pom.xml`` 하나를 읽는다.
    읽지 못하면 ``error``를 채운 PomFile을 반환하고 예외를 던지지 않는다."""
    rel = _pom_rel(d)
    path = project / rel
    pf = PomFile(dir=d, rel=rel, path=path)
    from .apply import PomReadError, read_pom_xml, xml_reason
    try:
        # Maven이 받는 pom(UTF-8 선언의 MS949 등)을 읽도록 apply와 같은 규칙으로 UTF-8로 바꾼다
        data = read_pom_xml(path)
    except PomReadError as exc:
        pf.error = f"cannot read {rel}: {exc}"
        return pf
    try:
        root = etree.fromstring(data, _parser())
    except etree.XMLSyntaxError as exc:
        pf.error = f"cannot read {rel}: {xml_reason(exc)}"
        return pf
    pf.root = root
    # 줄 내용 확인용 원문. libxml2는 '\n' 기준으로 줄을 세므로 같은 기준으로 나눈다
    pf.lines = [ln.rstrip("\r") for ln in data.decode("utf-8", errors="replace").split("\n")]
    par = _kid(root, "parent")
    if par is not None:
        pf.parent_gav = (_text(_kid(par, "groupId")), _text(_kid(par, "artifactId")), _text(_kid(par, "version")))
        pf.parent_version_el = _kid(par, "version")
        rp = _kid(par, "relativePath")
        pf.relative_path = None if rp is None else _text(rp)
    pf.group_id = _text(_kid(root, "groupId")) or (pf.parent_gav[0] if pf.parent_gav else "")
    pf.artifact_id = _text(_kid(root, "artifactId"))
    pf.version = _text(_kid(root, "version")) or (pf.parent_gav[2] if pf.parent_gav else "")
    pf.packaging = _text(_kid(root, "packaging")) or "jar"
    for c in (_kid(root, "properties") if _kid(root, "properties") is not None else []):
        name = _local(c)
        if name and name not in pf.props:
            pf.props[name] = c
    mods = [_text(m) for m in _kids(_kid(root, "modules"), "module")]
    for prof in _kids(_kid(root, "profiles"), "profile"):
        mods += [_text(m) for m in _kids(_kid(prof, "modules"), "module")]   # 프로필의 모듈도 reactor 후보다
    pf.modules = [m for m in mods if m]
    return pf


class ProjectPoms:
    """프로젝트의 pom 목록을 담는 클래스.
    루트의 ``<modules>``(프로필 포함)를 따라간 reactor 모듈과, ``relativePath``나 좌표로 찾은 프로젝트 안 부모를 담는다.
    네트워크를 쓰지 않으므로 외부 부모와 BOM은 읽지 않는다. 읽지 못한 pom이 있으면 ``complete``가 False다."""

    def __init__(self, project: Path, roots: list[str] | None = None):
        self.project = Path(project)
        self.poms: dict[str, PomFile] = {}
        self.reactor: list[str] = []
        self.notes: list[str] = []
        self.complete = True
        self._linked: set[str] = set()
        # reactor 루트 목록. 기본은 루트 pom 하나이고, 루트 aggregator 없이 서비스마다 빌드한 프로젝트는
        # 그 서비스 폴더들이다
        self.roots: list[str] = normalize_roots(roots)
        self._discover()

    # -- pom 찾기
    def _load(self, d: str) -> PomFile:
        if d not in self.poms:
            pf = parse_pom_file(self.project, d)
            self.poms[d] = pf
            if pf.error:
                self.complete = False
                self.notes.append(pf.error)
        return self.poms[d]

    def _discover(self) -> None:
        queue: list[str] = []
        for r in self.roots:
            if r != "." and not (self.project / _pom_rel(r)).is_file():
                self.complete = False
                self.notes.append(f"build target {r}: no pom.xml")
                continue
            if self._load(r).ok:
                queue.append(r)
        if not queue:
            return
        seen: set[str] = set()
        while queue:
            d = queue.pop(0)
            if d in seen:
                continue
            seen.add(d)
            pf = self._load(d)
            pf.in_reactor = True
            self.reactor.append(d)
            if not pf.ok:
                continue
            for m in pf.modules:
                target = m
                if m.endswith(".xml"):                 # <module>sub/pom.xml</module> 형태도 허용한다
                    if posixpath.basename(m) != "pom.xml":
                        self.complete = False
                        self.notes.append(f"{pf.rel}: module {m!r} names a pom file other than pom.xml — not read")
                        continue
                    target = posixpath.dirname(m) or "."
                nd = _norm_dir(d, target)
                if nd is None:
                    self.complete = False
                    self.notes.append(f"{pf.rel}: module {m!r} points outside the project — not read")
                    continue
                if not (self.project / _pom_rel(nd)).is_file():
                    self.complete = False
                    self.notes.append(f"{pf.rel}: module {m!r} has no pom.xml")
                    continue
                queue.append(nd)
        self._link_parents()

    def _link_parents(self) -> None:
        changed = True
        while changed:
            changed = False
            for pf in list(self.poms.values()):
                if not pf.ok or pf.parent_gav is None or pf.parent is not None or pf.dir in self._linked:
                    continue
                self._linked.add(pf.dir)
                found = self._find_parent(pf)
                if found is not None:
                    pf.parent = found
                    changed = True

    def _find_parent(self, pf: PomFile) -> PomFile | None:
        assert pf.parent_gav is not None
        g, a, v = pf.parent_gav
        if pf.relative_path != "":                     # 빈 <relativePath/>면 부모 파일을 찾지 않는다
            rp = pf.relative_path or "../pom.xml"
            rp = rp.replace("\\", "/")
            target = posixpath.dirname(rp) if rp.endswith(".xml") else rp
            nd = _norm_dir(pf.dir, target or ".")
            if nd is not None and nd != pf.dir and (self.project / _pom_rel(nd)).is_file():
                cand = self._load(nd)
                if cand.ok and cand.ga == f"{g}:{a}" and (not v or not cand.version or cand.version == v):
                    return cand
        for cand in self.poms.values():               # reactor 안에서 같은 좌표를 찾는다
            if cand is not pf and cand.ok and cand.ga == f"{g}:{a}" and (not v or cand.version == v):
                return cand
        return None

    # -- 조회
    def chain(self, pf: PomFile) -> list[PomFile]:
        """``pf``와 프로젝트 안의 부모들을 자식부터 순서대로 반환한다. 순환이 있으면 거기서 끊는다."""
        out: list[PomFile] = []
        cur: PomFile | None = pf
        while cur is not None and cur not in out:
            out.append(cur)
            cur = cur.parent
        return out

    def external_parent(self, pf: PomFile) -> tuple[str, PomFile, Any] | None:
        """부모 체인 맨 위 pom의 외부 부모를 (g:a:v, 그 ``<parent>``가 있는 pom, ``<version>`` 요소)로 반환한다.
        외부 부모가 없으면 None을 반환한다."""
        top = self.chain(pf)[-1]
        if top.parent_gav is None or top.parent is not None:
            return None
        g, a, v = top.parent_gav
        return f"{g}:{a}:{v}", top, top.parent_version_el

    def boms(self, pf: PomFile) -> list[tuple[str, PomFile, Any]]:
        """부모 체인의 ``<dependencyManagement>``가 import하는 BOM 목록을 반환한다.
        항목은 (g:a:v, 선언한 pom, ``<version>`` 요소)이고, 같은 g:a는 자식 쪽 하나만 남긴다."""
        out: list[tuple[str, PomFile, Any]] = []
        seen: set[str] = set()
        for p in self.chain(pf):
            for dep, _ in _deps(p, management=True):
                if _text(_kid(dep, "scope")) == "import" and _text(_kid(dep, "type")) == "pom":
                    g, a = self.interp(pf, _text(_kid(dep, "groupId"))), self.interp(pf, _text(_kid(dep, "artifactId")))
                    vel = _kid(dep, "version")
                    ref = self.resolve_version(pf, _text(vel))
                    gav = f"{g}:{a}:{ref.value or _text(vel)}"
                    if f"{g}:{a}" in seen:
                        continue
                    seen.add(f"{g}:{a}")
                    out.append((gav, p, vel))
        return out

    def lookup(self, pf: PomFile, name: str) -> tuple[PomFile, Any] | None:
        """property 정의를 자식 pom부터 찾아 (정의한 pom, 요소)를 반환한다. Maven처럼 자식 값이 우선한다.
        없으면 None을 반환한다."""
        for p in self.chain(pf):
            if name in p.props:
                return p, p.props[name]
        return None

    def builtin(self, pf: PomFile, name: str) -> str | None:
        """``project.version`` 같은 내장 property의 값을 반환한다. 내장 이름이 아니면 None을 반환한다.
        값을 모르는 내장 이름은 빈 문자열이다. 이 값은 고칠 대상이 아니다."""
        n = name
        for pre in ("project.", "pom."):
            n = n.removeprefix(pre)
        table = {"version": pf.version, "groupId": pf.group_id, "artifactId": pf.artifact_id,
                 "parent.version": pf.parent_gav[2] if pf.parent_gav else "",
                 "parent.groupId": pf.parent_gav[0] if pf.parent_gav else ""}
        if name.startswith(("project.", "pom.")) or name in _BUILTIN_NAMES:
            return table.get(n, "")
        if name.startswith(_BUILTIN_PREFIXES):
            return ""
        return None

    def interp(self, pf: PomFile, raw: str) -> str:
        """좌표 문자열의 ``${…}``를 바꿀 수 있는 만큼 값으로 바꾼다.
        비교용이며 고칠 위치는 ``resolve_version``이 정한다."""
        out = raw
        for _ in range(MAX_PROPERTY_DEPTH):
            m = re.search(r"\$\{([^}]+)\}", out)
            if not m:
                break
            name = m.group(1)
            hit = self.lookup(pf, name)
            val = _text(hit[1]) if hit else self.builtin(pf, name)
            if val is None:
                break
            out = out[:m.start()] + val + out[m.end():]
        return out

    def resolve_version(self, pf: PomFile, raw: str) -> VersionRef:
        """버전 문자열을 ``pf``(모듈) 기준으로 해석한다.
        결과 ``kind``는 literal, property(마지막 정의까지 따라감), external(프로젝트 밖), composite(식),
        builtin, none 중 하나다."""
        raw = (raw or "").strip()
        if not raw:
            return VersionRef("none")
        if "${" not in raw:
            return VersionRef("literal", raw)
        names: list[str] = []
        cur = raw
        for _ in range(MAX_PROPERTY_DEPTH):
            m = _FULL_PROP.match(cur)
            if not m:
                return VersionRef("composite", "", names=names, detail=cur)
            name = m.group(1)
            names.append(name)
            hit = self.lookup(pf, name)
            if hit is None:
                if self.builtin(pf, name) is not None:
                    return VersionRef("builtin", self.builtin(pf, name) or "", names=names)
                return VersionRef("external", "", names=names)
            dpom, el = hit
            val = _text(el)
            if "${" not in val:
                return VersionRef("property", val, pom=dpom, el=el, prop=name, names=names)
            cur = val
        return VersionRef("composite", "", names=names, detail=f"more than {MAX_PROPERTY_DEPTH} levels")


@dataclass
class VersionRef:
    """버전 문자열을 해석한 결과를 담는 데이터 클래스.
    ``kind``는 literal / property / external / composite / builtin / none 중 하나다."""
    kind: str
    value: str = ""
    pom: PomFile | None = None
    el: Any = None
    prop: str = ""
    names: list[str] = field(default_factory=list)
    detail: str = ""


def _deps(pf: PomFile, *, management: bool) -> list[tuple[Any, str]]:
    """pom의 ``<dependency>`` 요소 목록(프로필 포함)을 (요소, 위치) 쌍으로 반환한다.
    위치는 최상위면 빈 문자열, 프로필이면 "profile <id>"다. ``management``가 True면 관리 항목을 반환한다."""
    if not pf.ok:
        return []
    out: list[tuple[Any, str]] = []

    def collect(base: Any, where: str) -> None:
        holder = _kid(base, "dependencyManagement") if management else base
        for dep in _kids(_kid(holder, "dependencies"), "dependency"):
            out.append((dep, where))

    collect(pf.root, "")
    for prof in _kids(_kid(pf.root, "profiles"), "profile"):
        collect(prof, f"profile {_text(_kid(prof, 'id'))}")
    return out


def location_of(pf: PomFile, el: Any, element: str, prop: str = "") -> PomLocation | None:
    """요소 ``el``의 값이 그 줄에 그대로 있으면 ``PomLocation``을 반환한다.
    apply가 줄 단위로 고치므로 확인하지 못하면 None을 반환한다.

    The element's start line plus the newlines before its text is tried first, then the next lines up to five
    below the start line."""
    if el is None:
        return None
    raw = el.text or ""
    text = raw.strip()
    if not text or not el.sourceline:
        return None
    lead = raw[:len(raw) - len(raw.lstrip())]
    line = int(el.sourceline) + lead.count("\n")
    for ln in (line, *range(int(el.sourceline), int(el.sourceline) + 6)):
        if 1 <= ln <= len(pf.lines) and text in pf.lines[ln - 1]:
            return PomLocation(pom=pf.rel, line=ln, text=text, property=prop, element=element)
    return None


# ---------------------------------------------------------------------------- 모듈 하나의 출처
@dataclass
class _Res:
    origin: str
    location: PomLocation | None = None
    manager: str = ""
    manager_location: PomLocation | None = None
    reason: str = ""
    via: list[str] = field(default_factory=list)
    # 이 모듈에서 이 라이브러리를 끌어온 사내 아티팩트 g:a 목록(``inhouse.inhouse_on_path``).
    # 묶는 키에는 넣지 않고 항목의 ``inhouse`` / ``inhouse_modules``로 모은다
    inhouse: list[str] = field(default_factory=list)

    def key(self) -> tuple:
        if self.origin in EDITABLE and self.location is not None:
            return (self.origin, self.location.pom, self.location.line)
        if self.origin == O_UNMAPPED:
            return (self.origin, self.reason)
        ml = self.manager_location
        return (self.origin, self.manager, ml.pom if ml else "", ml.line if ml else 0)

    @property
    def editable(self) -> bool:
        return self.origin in EDITABLE and self.location is not None


def _find_decl(pp: ProjectPoms, mod: PomFile, ga: str, *, management: bool) -> tuple[PomFile, Any] | None:
    for p in pp.chain(mod):
        for dep, _ in _deps(p, management=management):
            if management and _text(_kid(dep, "scope")) == "import":
                continue
            g = pp.interp(mod, _text(_kid(dep, "groupId")))
            a = pp.interp(mod, _text(_kid(dep, "artifactId")))
            if f"{g}:{a}" == ga:
                return p, dep
    return None


def _active_by_default(profile: Any) -> bool:
    act = _kid(profile, "activation")
    return _text(_kid(act, "activeByDefault")).lower() == "true"


def _decl_key(pp: ProjectPoms, mod: PomFile, dep: Any) -> tuple[str, str]:
    return (pp.interp(mod, _text(_kid(dep, "type"))) or "jar", pp.interp(mod, _text(_kid(dep, "classifier"))))


def _decl_problem(pp: ProjectPoms, mod: PomFile, ga: str, *, management: bool) -> str:
    """``_find_decl``이 고르는 pom의 선언을 고쳐도 되는지 확인한다. 안 되면 이유 한 줄을, 괜찮으면 빈 문자열을 반환한다.
    - 기본으로 켜지지 않는 프로필에만 있는 선언은 Maven이 쓰지 않을 수 있으므로 고치지 않는다
    - 한 ``<dependencies>`` 안에 같은 관리 키로 두 번 있는 선언은 Maven이 그중 하나만 쓰므로 고치지 않는다
    부모 체인에서 ``ga``를 처음 선언한 pom만 본다."""
    for p in pp.chain(mod):
        if not p.ok:
            continue
        blocks: list[tuple[str, bool, list[Any]]] = []
        bases = [("", True, p.root)] + [(_text(_kid(prof, "id")), _active_by_default(prof), prof)
                                        for prof in _kids(_kid(p.root, "profiles"), "profile")]
        for pid, active, base in bases:
            holder = _kid(base, "dependencyManagement") if management else base
            hits = []
            for dep in _kids(_kid(holder, "dependencies"), "dependency"):
                if management and _text(_kid(dep, "scope")) == "import":
                    continue
                if f"{pp.interp(mod, _text(_kid(dep, 'groupId')))}:{pp.interp(mod, _text(_kid(dep, 'artifactId')))}" == ga:
                    hits.append(dep)
            if hits:
                blocks.append((pid, active, hits))
        if not blocks:
            continue
        for _pid, active, hits in blocks:
            if not active:
                continue                     # 꺼진 프로필 안의 중복은 Maven이 쓰지 않으므로 보지 않는다
            keys = [_decl_key(pp, mod, d) for d in hits]
            dup = max(keys.count(k) for k in keys)
            if dup > 1:
                return f"{ga} is declared {dup} times in one <dependencies> block of {p.rel}; not changed automatically"
        if not any(active for _pid, active, _h in blocks):
            names = ", ".join(pid or "?" for pid, _a, _h in blocks)
            return (f"{ga} is declared in {p.rel} only inside profile(s) {names}, which are not active by default; "
                    "not changed automatically")
        return ""
    return ""


def _manager_loc(pp: ProjectPoms, mod: PomFile, pf: PomFile, vel: Any, element: str) -> PomLocation | None:
    ref = pp.resolve_version(mod, _text(vel))
    if ref.kind == "literal":
        return location_of(pf, vel, element)
    if ref.kind == "property" and ref.pom is not None:
        return location_of(ref.pom, ref.el, element, ref.prop)
    return None


def _external(pp: ProjectPoms, mod: PomFile, ga: str, managed_by: Callable[[str], str | None] | None,
              why: str = "") -> _Res:
    """프로젝트 밖에서 관리되는 버전의 출처를 정한다. 외부 부모, import한 BOM, 출처 불명 중 하나다.
    ``managed_by``가 관리자를 알려 주면 그것을 먼저 쓴다. 관리할 곳이 없으면 ``unmapped``다."""
    parent = pp.external_parent(mod)
    boms = pp.boms(mod)
    prefix = f"{why}; " if why else ""
    fact = managed_by(ga) if managed_by else None
    if fact:
        fga = ":".join(fact.split(":")[:2])
        if parent and ":".join(parent[0].split(":")[:2]) == fga:
            return _Res(O_PARENT_MANAGED, manager=parent[0],
                        manager_location=_manager_loc(pp, mod, parent[1], parent[2], "parent"),
                        reason=f"{prefix}managed by {fact}")
        for gav, pf, vel in boms:
            if ":".join(gav.split(":")[:2]) == fga:
                return _Res(O_BOM_MANAGED, manager=gav, manager_location=_manager_loc(pp, mod, pf, vel, "bom"),
                            reason=f"{prefix}managed by {fact}")
        return _Res(O_PARENT_MANAGED if parent else O_BOM_MANAGED, manager=fact,
                    reason=f"{prefix}managed by {fact}, which the project poms do not declare directly")
    if parent and not boms:
        return _Res(O_PARENT_MANAGED, manager=parent[0],
                    manager_location=_manager_loc(pp, mod, parent[1], parent[2], "parent"), reason=why)
    if len(boms) == 1 and not parent:
        gav, pf, vel = boms[0]
        return _Res(O_BOM_MANAGED, manager=gav, manager_location=_manager_loc(pp, mod, pf, vel, "bom"), reason=why)
    if parent or boms:
        what = ([f"parent {parent[0]}"] if parent else []) + [f"BOM {b[0]}" for b in boms]
        return _Res(O_PARENT_MANAGED if parent else O_BOM_MANAGED,
                    reason=f"{prefix}ambiguous provenance: version may come from " + ", ".join(what))
    return _Res(O_UNMAPPED, reason=f"{prefix}no version declared in the project and no parent / BOM to manage it")


def _from_ref(pp: ProjectPoms, mod: PomFile, ga: str, version: str, ref: VersionRef, decl_pom: PomFile, vel: Any,
              origin_literal: str, origin_prop: str, element: str,
              managed_by: Callable[[str], str | None] | None) -> _Res:
    if ref.kind == "literal":
        loc = location_of(decl_pom, vel, element)
        if loc is None:
            return _Res(O_UNMAPPED, reason=f"version text of {ga} in {decl_pom.rel} could not be located on one line")
        if not same_version(ref.value, version):      # Maven은 한정자의 대소문자를 구분하지 않는다
            return _Res(O_UNMAPPED, reason=f"{decl_pom.rel} declares {ga} {ref.value}, Maven resolves {version}")
        return _Res(origin_literal, location=loc)
    if ref.kind == "property":
        assert ref.pom is not None
        loc = location_of(ref.pom, ref.el, "property", ref.prop)
        if loc is None:
            return _Res(O_UNMAPPED, reason=f"property {ref.prop} in {ref.pom.rel} could not be located on one line")
        if not same_version(ref.value, version):      # Maven은 한정자의 대소문자를 구분하지 않는다
            return _Res(O_UNMAPPED, reason=f"property {ref.prop} in {ref.pom.rel} is {ref.value}, Maven resolves {version}")
        return _Res(origin_prop, location=loc)
    names = " → ".join(f"${{{n}}}" for n in ref.names)
    if ref.kind == "external":
        return _external(pp, mod, ga, managed_by, why=f"version property {names} is not defined in the project poms")
    if ref.kind == "builtin":
        return _Res(O_UNMAPPED, reason=f"version of {ga} comes from the built-in property {names}")
    return _Res(O_UNMAPPED, reason=f"version of {ga} is the expression {ref.detail or names!r}; manual review")


def _classify(pp: ProjectPoms, mod: PomFile, dep: ResolvedDep,
              managed_by: Callable[[str], str | None] | None) -> _Res:
    ga, version = dep.ga, dep.version
    decl = _find_decl(pp, mod, ga, management=False)
    if decl is not None:
        dpom, del_ = decl
        vel = _kid(del_, "version")
        if _text(vel):
            # 꺼진 프로필에만 있거나 두 번 선언된 버전은 고쳐도 Maven이 쓰는 버전이 바뀐다는 보장이 없다
            problem = _decl_problem(pp, mod, ga, management=False)
            if problem:
                return _Res(O_UNMAPPED, reason=problem)
            ref = pp.resolve_version(mod, _text(vel))      # Maven은 자식(모듈) 기준으로 property 값을 정한다
            return _from_ref(pp, mod, ga, version, ref, dpom, vel, O_DIRECT_LITERAL, O_DIRECT_PROPERTY, "dependency",
                             managed_by)
    mg = _find_decl(pp, mod, ga, management=True)
    if mg is not None and _text(_kid(mg[1], "version")):
        problem = _decl_problem(pp, mod, ga, management=True)
        if problem:
            return _Res(O_UNMAPPED, reason=problem)
        mpom, mel = mg
        vel = _kid(mel, "version")
        ref = pp.resolve_version(mod, _text(vel))
        return _from_ref(pp, mod, ga, version, ref, mpom, vel, O_OWN_MANAGEMENT, O_OWN_MANAGEMENT, "management", managed_by)
    if decl is None and not dep.direct:
        fact = managed_by(ga) if managed_by else None
        if fact:
            res = _external(pp, mod, ga, managed_by)
            res.via = list(dep.path)
            return res
        return _Res(O_TRANSITIVE, via=list(dep.path))
    return _external(pp, mod, ga, managed_by)


# ---------------------------------------------------------------------------- 묶기
def _merge_modules(item: InventoryItem, extra: list[tuple[str, _Res]], *, also: bool = False) -> None:
    governed = set(item.modules)
    for mdir, res in extra:
        if mdir not in item.modules:
            item.modules.append(mdir)
    if also:
        # 합쳐 들어온 모듈은 다른 경로로 같은 버전이 된 곳이라 이 항목의 버전 문자열이 적용되는지 알 수 없다.
        # 검증에서 실패로 치지 않고, 남으면 다음 라운드에서 따로 계획한다
        item.also_modules = sorted(({m for m, _ in extra} | set(item.also_modules)) - governed)
    shapes: dict[tuple, list[str]] = {}
    for mdir, res in extra:
        shapes.setdefault((res.origin, res.manager, " → ".join(res.via)), []).append(mdir)
    parts = []
    for (origin, manager, via), mods in sorted(shapes.items()):
        desc = origin + (f" by {manager}" if manager else "") + (f" via {via}" if via else "")
        parts.append(f"also resolved in {', '.join(sorted(mods))} ({desc})")
    item.reason = "; ".join([x for x in [item.reason, *parts] if x])


def _item_from(ga: str, version: str, res: _Res, mods: list[str]) -> InventoryItem:
    return InventoryItem(ga=ga, version=version, origin=res.origin, modules=sorted(set(mods)), via=list(res.via),
                         location=res.location, manager=res.manager, manager_location=res.manager_location,
                         reason=res.reason)


def _group(ga: str, version: str, results: list[tuple[str, _Res]]) -> list[InventoryItem]:
    groups: dict[tuple, list[tuple[str, _Res]]] = {}
    for mdir, res in results:
        groups.setdefault(res.key(), []).append((mdir, res))
    editable = {k: v for k, v in groups.items() if v[0][1].editable}
    unmapped = {k: v for k, v in groups.items() if v[0][1].origin == O_UNMAPPED}
    others = {k: v for k, v in groups.items() if k not in editable and k not in unmapped}
    items: list[InventoryItem] = []
    for k in sorted(editable):
        v = editable[k]
        items.append(_item_from(ga, version, v[0][1], [m for m, _ in v]))
    if others:
        if items:
            # 고칠 수 있는 항목 중 가장 많은 모듈을 관리하는 것에 합치고, 같으면 정렬 순서상 첫 번째에 합친다
            target = sorted(items, key=lambda it: (-len(it.modules), it.location.pom if it.location else "",
                                                   it.location.line if it.location else 0))[0]
            _merge_modules(target, [x for v in others.values() for x in v], also=True)
        else:
            def shortest_via(v: list[tuple[str, _Res]]) -> list[str]:
                vias = [r.via for _, r in v if r.via]
                return min(vias, key=lambda p: (len(p), p)) if vias else []
            keys = sorted(others, key=lambda k: (-len(others[k]), [str(x) for x in k]))
            primary = others[keys[0]]
            item = _item_from(ga, version, primary[0][1], [m for m, _ in primary])
            item.via = shortest_via(primary)
            rest = [x for k in keys[1:] for x in others[k]]
            if rest:
                _merge_modules(item, rest)
            items.append(item)
    for k in sorted(unmapped, key=lambda k: [str(x) for x in k]):
        v = unmapped[k]
        items.append(_item_from(ga, version, v[0][1], [m for m, _ in v]))
    return items


AMBIGUOUS_MARK = "ambiguous provenance"
PARENTS_GROUP = "parents"


def _parent_inhouse(pp: ProjectPoms, mod: PomFile, dep: ResolvedDep, res: _Res, parents: set[str]) -> list[str]:
    """결과가 회사 부모(``inhouse_groups``의 ``parents``)에서 온 것이면 ``[그 부모 g:a]``를, 아니면 빈 목록을 반환한다.
    부모 g:a는 의존 경로에 나오지 않으므로 모듈의 외부 부모로 판단한다.
    - 출처가 분명한 ``parent_managed``면 부모에서 온 것이다
    - 간접 의존성인데 경로의 첫 의존성을 프로젝트 pom이 선언하지 않았으면 부모에서 온 것이다
    - 고칠 수 있는 출처, unmapped, 프로젝트가 import한 BOM의 관리는 부모에서 온 것으로 보지 않는다"""
    if not parents or res.editable or res.origin == O_UNMAPPED:
        return []
    ext = pp.external_parent(mod)
    if ext is None:
        return []
    pga = _split_ga(ext[0])
    if pga is None or pga not in parents:
        return []
    if res.origin == O_PARENT_MANAGED and AMBIGUOUS_MARK not in (res.reason or ""):
        return [pga]
    if not dep.direct and dep.path and (res.origin == O_TRANSITIVE or res.via):
        head = _split_ga(dep.path[0])
        if head is not None and head != dep.ga and _find_decl(pp, mod, head, management=False) is None:
            return [pga]
    return []


def _fill_inhouse(item: InventoryItem, results: list[tuple[str, _Res]]) -> None:
    """항목의 모듈들(합쳐 들어온 also 모듈 포함)에서 사내 아티팩트 결과를 모아 항목에 채운다.
    ``inhouse``는 정렬한 합집합, ``inhouse_modules``는 사내 아티팩트가 있는 모듈 목록이다."""
    per_mod = {m: r.inhouse for m, r in results}
    mods = [m for m in item.modules if per_mod.get(m)]
    item.inhouse = sorted({ga for m in mods for ga in per_mod[m]})
    item.inhouse_modules = sorted(mods)


# ---------------------------------------------------------------------------- 해석 결과에 없는 항목
def _safe_rel(loc: str) -> str | None:
    p = (loc or "").replace("\\", "/").lstrip("/")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or any(x == ".." for x in parts) or ":" in parts[0]:
        return None
    return "/".join(parts)


def _vendored_paths(project: Path, locations: list[str]) -> list[str]:
    """IVAS가 알려 준 위치 중 트리에 실제로 있는 pom이 아닌 파일을 IVAS 표기 그대로 반환한다.
    ZIP이 최상위 폴더로 감싸였으면 위치 앞에 그 폴더 이름이 붙어 오므로 앞부분을 세 단계까지 떼어 보며 찾는다."""
    out: list[str] = []
    for loc in locations:
        rel = _safe_rel(loc)
        if rel is None:
            continue
        name = rel.rsplit("/", 1)[-1]
        if name == "pom.xml" or name.endswith(".pom"):
            continue
        parts = rel.split("/")
        for k in range(min(4, len(parts))):
            cand = project.joinpath(*parts[k:])
            try:
                if cand.is_file() and not cand.is_symlink():
                    out.append(loc)
                    break
            except OSError:
                break
    return sorted(set(out))


def _plugin_hits(resolved: ResolvedModel, gas: list[str]) -> list[tuple[str, ResolvedDep]]:
    """IVAS의 g:a 표기에 맞는 빌드 플러그인과 그 의존성을 (모듈, 항목) 목록으로 반환한다."""
    match = _matching_gas(gas, {p.ga for m in resolved.modules for p in m.plugins})
    return [(m.dir, p) for m in resolved.modules for p in m.plugins if p.ga in match]


def split_patched_versions(text: str) -> list[str]:
    """IVAS 패치 버전 문자열("2.12.7.1, 2.13.4.2")을 개별 버전 목록으로 나눈다.
    쉼표, 세미콜론, 공백으로 나누되 ``[`` ``(`` … ``]`` ``)`` 범위 안의 쉼표에서는 나누지 않는다.
    범위는 한 덩어리로 남겨 planner가 버전이 아닌 힌트로 처리한다.
    빈 조각은 버리고 순서를 유지하며 중복을 뺀다.

    Whether IVAS ever sends several versions in one text is not confirmed; this is how such a text is read."""
    out: list[str] = []
    buf, depth = "", 0
    for ch in str(text or ""):
        if ch in "[(":
            depth += 1
        elif ch in "])" and depth:
            depth -= 1
        if depth == 0 and (ch in ",;" or ch.isspace()):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
            continue
        buf += ch
    if buf.strip():
        out.append(buf.strip())
    return list(dict.fromkeys(out))


def _split_ga(identifier: str) -> str | None:
    parts = [x.strip() for x in (identifier or "").split(":")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return f"{parts[0]}:{parts[1]}"


# ---------------------------------------------------------------------------- 진입점
def _matching_gas(reported: list[str], available: set[str]) -> set[str]:
    """IVAS의 g:a 표기에 맞는 Maven g:a 집합을 반환한다.
    Maven의 g:a는 대소문자를 구분하므로 똑같은 표기가 있으면 그것만 쓰고, 없을 때만 대소문자를 무시해 찾는다."""
    exact = set(reported) & available
    if exact:
        return exact
    keys = {ga_key(r) for r in reported}
    return {a for a in available if ga_key(a) in keys}


def _variant_of(d: ResolvedDep) -> str:
    """일반 jar면 빈 문자열을, 아니면 ``"<type>/<classifier>"``를 반환한다."""
    t = d.type or "jar"
    return "" if t == "jar" and not d.classifier else f"{t}/{d.classifier}"


def _declared(pp: ProjectPoms, mod: PomFile, ga: str) -> PomLocation | None:
    """프로젝트 pom이 이 모듈의 ``<dependencies>``에 ``ga``를 선언한 위치를 반환한다.
    버전 문자열이 있으면 그 줄을, 없으면 ``<dependency>`` 줄을 ``text=""``로 반환한다."""
    decl = _find_decl(pp, mod, ga, management=False)
    if decl is None:
        return None
    dpom, del_ = decl
    vel = _kid(del_, "version")
    if _text(vel):
        loc = location_of(dpom, vel, "dependency")
        if loc is not None:
            return loc
    return PomLocation(pom=dpom.rel, line=int(del_.sourceline or 0), text="", element="dependency")


@dataclass
class _Mod:
    """모듈 하나의 출처 결과와 그 근거가 된 의존성 정보를 담는 클래스."""
    res: _Res
    dep: ResolvedDep
    variants: list[str]
    declared: PomLocation | None = None
    plain: bool = False                          # 같은 버전을 일반 jar로도 해석했으면 True

    def source(self, mdir: str) -> ModuleSource:
        # 변종이 있을 때만 일반 jar를 "jar/"로 앞에 적는다. 일반 jar만이면 빈 문자열이다
        shown = (["jar/"] if self.plain and self.variants else []) + self.variants
        return ModuleSource(module=mdir, how=self.res.origin, via=list(self.dep.path), declared=self.declared,
                            manager=self.res.manager, inhouse=list(self.res.inhouse), variant=", ".join(shown))


def _split_inhouse(ga: str, results: list[tuple[str, _Res]]) -> tuple[list[tuple[str, _Res]], list[tuple[str, _Res]]]:
    """모듈 결과를 (사내 아티팩트가 없는 쪽, 있는 쪽)으로 나눈다. 묶기 전에 나누므로 한 항목에 섞이지 않는다.
    고칠 수 있는 버전 문자열 위치는 프로젝트의 것이므로 나누지 않는다.
    라이브러리 자체가 사내 아티팩트일 때만 그 위치의 모듈을 모두 사내 쪽에 둔다."""
    inh_keys: set[tuple] = set()
    by_key: dict[tuple, list[_Res]] = {}
    for _m, r in results:
        if r.editable:
            by_key.setdefault(r.key(), []).append(r)
    for k, rs in by_key.items():
        if all(ga in r.inhouse for r in rs):
            inh_keys.add(k)
    plain: list[tuple[str, _Res]] = []
    inh: list[tuple[str, _Res]] = []
    for m, r in results:
        side = (r.key() in inh_keys) if r.editable else bool(r.inhouse)
        (inh if side else plain).append((m, r))
    return plain, inh


def build_inventory(project: Path, findings: list[Finding], resolved: ResolvedModel, *,
                    managed_by: Callable[[str], str | None] | None = None,
                    inhouse: Mapping[str, str] | None = None) -> Inventory:
    """IVAS가 찾은 취약점으로 ``Inventory``를 만든다(``interfaces.InventoryFn``).
    의존성 결과와 g:a, 버전이 맞는 라이브러리만 출처를 정하고, 나머지는 plugin / vendored / unmapped로 분류한다.
    - 항목은 Maven 표기를 쓰고, IVAS 표기가 다르면 ``reported_ga`` / ``reported_version``에 남긴다
    - 의존성 결과가 불완전하거나 프로젝트 pom을 못 읽으면 ``complete``가 False다
    - ``inhouse``를 주면 사내 아티팩트를 거쳐 들어온 모듈과 아닌 모듈을 다른 항목으로 나눈다
    항목은 (ga, 버전, 출처, pom, 줄) 순으로 정렬한다.

    Matching (`matching.py`): findings that differ only by case are one group; a group matches the resolution
    by exact g:a first and by case-insensitive g:a only when no module resolves the exact one; versions
    compare case-insensitively. Several IVAS spellings: `reported_*` takes the first in sorted order. With
    `inhouse` (in-house g:a -> group) an in-house item has `inhouse` = the union and `inhouse_modules` = all
    of `modules`; the other items have both lists empty (`_split_inhouse` for the editable-location rule).
    Every item from the resolution has `sources` (one `ModuleSource` per module, sorted) and `variants` (the
    non-plain-jar "<type>/<classifier>" of its modules). `ModuleSource.variant`: "" when the module resolves
    only the plain jar; else its "<type>/<classifier>" values joined with ", ", led by "jar/" when the module
    resolves the plain jar too ("jar/, jar/jakarta")."""
    project = Path(project)
    # 루트 aggregator 없이 서비스마다의 reactor로 의존성을 구했으면 같은 루트 목록으로 pom을 읽는다
    pp = ProjectPoms(project, roots=list(getattr(resolved, "roots", None) or []) or None)
    inv = Inventory(complete=bool(resolved.complete) and pp.complete)
    inv.notes += list(resolved.notes) + list(pp.notes)
    if not resolved.complete and not resolved.notes:
        inv.notes.append("Maven resolution is incomplete")

    items: list[InventoryItem] = []
    # 회사 부모(parents 그룹)는 의존 경로에 나오지 않으므로 모듈의 외부 부모로 따로 판단한다(_parent_inhouse)
    parent_keys = {k for k, grp in (inhouse or {}).items() if grp == PARENTS_GROUP}
    resolved_gas = {d.ga for m in resolved.modules for d in m.dependencies}

    # 라이브러리 발견 항목을 대소문자를 무시한 (g:a, 버전)으로 묶는다
    lib_groups: dict[tuple[str, str], list[tuple[str, Finding]]] = {}
    unparsable: dict[tuple[str, str], list[Finding]] = {}
    file_groups: dict[tuple[str, str], list[Finding]] = {}
    for f in findings:
        if f.kind == "library":
            ga = _split_ga(f.identifier)
            if ga is None:
                unparsable.setdefault((f.identifier, f.version), []).append(f)
            else:
                lib_groups.setdefault((ga_key(ga), version_key(f.version)), []).append((ga, f))
        elif f.kind in FILE_KINDS:
            file_groups.setdefault((f.identifier, f.version), []).append(f)
        else:
            unparsable.setdefault((f.identifier, f.version), []).append(f)

    def decorate(it: InventoryItem, fs: list[Finding]) -> InventoryItem:
        it.finding_ids = sorted({f.id for f in fs if f.id})
        # 원소 하나가 취약점 하나의 힌트다. 원소끼리는 AND, 원소 안의 여러 버전은 대안(OR)이므로
        # 하나의 목록으로 펼치지 않고 묶어 둔다
        it.patched_versions = sorted({", ".join(alts) for f in fs if (alts := split_patched_versions(f.patched_version))})
        return it

    for (_gk, vkey), pairs in sorted(lib_groups.items()):
        fs = [f for _, f in pairs]
        ivas_gas = sorted({ga for ga, _ in pairs})
        ivas_versions = sorted({f.version or "" for f in fs})
        gas = _matching_gas(ivas_gas, resolved_gas)
        # Maven 표기 (g:a, 버전)마다 모듈 결과를 모은다
        sub: dict[tuple[str, str], list[tuple[str, _Res]]] = {}
        info: dict[tuple[str, str], dict[str, _Mod]] = {}
        for m in resolved.modules:
            hits = [d for d in m.dependencies if vkey and d.ga in gas and same_version(d.version, vkey)]
            if not hits:
                continue
            dep = hits[0]                               # 모듈마다 한 번만 센다. classifier만 다른 같은 g:a는 같은 위치다
            ga, version = dep.ga, dep.version
            variants = sorted({v for d in m.dependencies
                               if d.ga == ga and same_version(d.version, version) and (v := _variant_of(d))})
            plain = any(d.ga == ga and same_version(d.version, version) and not _variant_of(d) for d in m.dependencies)
            pf = pp.poms.get(m.dir)
            if pf is None:
                pf = parse_pom_file(project, m.dir)
            if not pf.ok:
                inv.complete = False
                note = f"module {m.dir}: pom not readable ({pf.error or 'missing'})"
                if note not in inv.notes:
                    inv.notes.append(note)
                res = _Res(O_UNMAPPED, reason=note, inhouse=inhouse_on_path(dep.ga, dep.path, inhouse))
                sub.setdefault((ga, version), []).append((m.dir, res))
                info.setdefault((ga, version), {})[m.dir] = _Mod(res, dep, variants, plain=plain)
                continue
            res = _classify(pp, pf, dep, managed_by)
            res.inhouse = inhouse_on_path(dep.ga, dep.path, inhouse)
            for pga in _parent_inhouse(pp, pf, dep, res, parent_keys):
                if pga not in res.inhouse:
                    res.inhouse.append(pga)
            if variants and not res.editable:
                res.reason = "; ".join(x for x in [res.reason, f"{VARIANT_MARK} ({', '.join(variants)})"] if x)
            sub.setdefault((ga, version), []).append((m.dir, res))
            info.setdefault((ga, version), {})[m.dir] = _Mod(res, dep, variants, _declared(pp, pf, ga), plain)
        if not sub:
            items.append(decorate(_unresolved(project, resolved, ivas_gas, ivas_versions, fs, gas), fs))
            continue
        for (ga, version), results in sorted(sub.items()):
            mods = info[(ga, version)]
            marked = sorted({m for m, r in results if VARIANT_MARK in r.reason})
            plain, inh = _split_inhouse(ga, results)
            for part, in_house in ((plain, False), (inh, True)):
                if not part:
                    continue
                for it in _group(ga, version, part):
                    # 합쳐진 모듈의 reason은 사라지므로 override를 막도록 변종 표시를 항목에 남긴다
                    if it.origin not in EDITABLE and VARIANT_MARK not in it.reason and set(marked) & set(it.modules):
                        it.reason = "; ".join(x for x in [it.reason, f"{VARIANT_MARK} in {', '.join(marked)}"] if x)
                    if in_house:
                        _fill_inhouse(it, part)
                    it.sources = [mods[m].source(m) for m in sorted(it.modules) if m in mods]
                    it.variants = sorted({v for m in it.modules if m in mods for v in mods[m].variants})
                    it.reported_ga = "" if ga in ivas_gas else ivas_gas[0]
                    it.reported_version = "" if version in ivas_versions else ivas_versions[0]
                    items.append(decorate(it, fs))

    for (ident, version), fs in sorted(unparsable.items()):
        paths = _vendored_paths(project, [loc for f in fs for loc in f.locations])
        kinds = sorted({f.kind for f in fs})
        if paths:
            it = InventoryItem(ga=ident, version=version, origin=O_VENDORED, paths=paths,
                               reason="file in the tree (not a Maven dependency); not changed automatically")
        elif kinds != ["library"]:
            it = InventoryItem(ga=ident, version=version, origin=O_UNMAPPED,
                               reason=f"finding kind {', '.join(kinds)} is not supported by the remediation")
        else:
            it = InventoryItem(ga=ident, version=version, origin=O_UNMAPPED,
                               reason=f"identifier {ident!r} is not group:artifact")
        items.append(decorate(it, fs))

    for (ident, version), fs in sorted(file_groups.items()):
        locs = sorted({loc for f in fs for loc in f.locations})
        it = InventoryItem(ga=ident, version=version, origin=O_VENDORED, paths=locs,
                           reason=FILE_COMPONENT_REASON)
        items.append(decorate(it, fs))

    items.sort(key=lambda it: (it.ga, it.version, it.origin, it.location.pom if it.location else "",
                               it.location.line if it.location else 0))
    inv.items = items
    return inv


def _unresolved(project: Path, resolved: ResolvedModel, ivas_gas: list[str], ivas_versions: list[str],
                fs: list[Finding], gas: set[str]) -> InventoryItem:
    """의존성 결과에 그 버전으로 없는 라이브러리의 항목을 만든다.
    plugin -> vendored -> unmapped 순으로 분류하고 근거를 reason에 기록한다.
    ``gas``는 이 발견 항목에 맞는 Maven g:a 집합이다. 대소문자만 다른 버전은 다른 버전으로 보지 않는다."""
    ga, version = ivas_gas[0], ivas_versions[0]
    hits = _plugin_hits(resolved, ivas_gas)
    if hits:
        pga = min({p.ga for _, p in hits})
        mods = sorted({mdir for mdir, _ in hits})
        pversions = sorted({p.version for _, p in hits if p.version})
        first: dict[str, ResolvedDep] = {}
        for mdir, p in hits:
            first.setdefault(mdir, p)
        return InventoryItem(ga=pga, version=version, origin=O_PLUGIN, modules=mods,
                             reported_ga="" if pga in ivas_gas else ga,
                             sources=[ModuleSource(module=m, how=O_PLUGIN, via=list(first[m].path)) for m in mods],
                             reason=("build plugin or plugin dependency"
                                     + (f" (declared {', '.join(pversions)})" if pversions else "")
                                     + "; plugin versions are declared, not resolved; not changed automatically"))
    paths = _vendored_paths(project, [loc for f in fs for loc in f.locations])
    if paths:
        return InventoryItem(ga=ga, version=version, origin=O_VENDORED, paths=paths,
                             reason="file in the tree (not resolved by Maven); not changed automatically")
    # Maven이 고른 버전을 대소문자 무시로 하나씩만 보여 주고, 보고된 버전과 같은 것은 빼고 보여 준다
    seen_by_key: dict[str, str] = {}
    for v in sorted({d.version for m in resolved.modules for d in m.dependencies if d.ga in gas}):
        if version and same_version(v, version):
            continue
        seen_by_key.setdefault(version_key(v), v)
    seen = sorted(seen_by_key.values())
    shown = version or "no version"
    if not version:
        reason = f"IVAS reports {ga} without a version" + (f"; Maven resolves {', '.join(seen)}" if seen else "")
    elif seen:
        reason = f"IVAS reports {shown}, Maven resolves {', '.join(seen)}"
    else:
        reason = f"IVAS reports {ga} {shown}, Maven does not resolve {ga} in any module"
    if not resolved.complete:
        reason += " (resolution incomplete)"
    return InventoryItem(ga=ga, version=version, origin=O_UNMAPPED, reason=reason)
