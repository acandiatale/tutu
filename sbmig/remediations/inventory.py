"""IVAS 발견 항목을 Maven 이 실제로 고른 의존성(``ResolvedModel``)과 프로젝트 pom 의 **버전 글자 자리**에 잇는다.
흐름상 위치: 그래프의 ``inventory`` 노드(기준 빌드·기준 검사 뒤). 결과 ``Inventory`` 를 planner 가 읽어 제안을 만든다.
여기서 정한 ``PomLocation`` (pom, 줄, 그 줄의 글자 그대로)이 나중에 apply 가 줄 단위로 바꾸는 자리이므로, 줄 번호와 글자는
pom 파일을 다시 읽어 **그 줄에 그 글자가 정말 있는지** 확인한 뒤에만 낸다(확인 못 하면 자리 없음 + 메모).

판정 규칙(한 모듈씩):
    1. 모듈 pom → 프로젝트 안 부모 사슬의 ``<dependencies>`` 에 g:a 가 선언돼 있고 ``<version>`` 이 있으면
       글자 → ``direct_literal`` / ``${속성}`` (모듈 기준으로 풀어 프로젝트 pom 의 글자 정의에 닿으면) → ``direct_property``
       (자리 = 속성 정의 줄). 속성이 프로젝트 밖(외부 부모)에서 온다 → ``parent_managed``.
    2. 버전이 없거나 직접 선언이 없으면 프로젝트 pom 의 ``<dependencyManagement>`` (import 아닌 항목) → ``own_management``.
    3. 선언도 관리도 없고 Maven 이 간접 의존성으로 골랐다 → ``transitive`` (``via`` = 끌고 온 사슬).
    4. 직접 의존성인데 프로젝트 안 어디에도 버전이 없다 → 밖에서 관리된다. 오프라인이라 외부 pom 은 못 읽으므로:
       BOM 하나만 import 하고 외부 부모가 없으면 ``bom_managed``, 외부 부모만 있고 BOM 이 없으면 ``parent_managed``,
       둘 다 / 여럿이면 **출처 불명**(manager 비움, reason "ambiguous provenance: …") — planner 가 수동 검토로 남긴다.
       ``managed_by`` (g:a → 관리자 g:a:v) 사실을 주면 그것을 먼저 쓴다.
    선언된 글자가 해석된 버전과 다르면(예: 선언 1.1, 해석 1.0) 그 자리를 고쳐도 의미가 없으므로 ``unmapped`` 로 남긴다.

모듈들을 한 항목으로 묶는 규칙((g:a, 버전) 하나에 대해):
    * 프로젝트 안에서 고칠 수 있는 자리(direct_literal / direct_property / own_management + 자리 확인됨)는 **서로 다른 자리마다
      항목 하나** — 두 모듈이 각자 pom 에 같은 취약 버전을 적었으면 둘 다 고쳐야 하기 때문이다.
    * 나머지(parent_managed / bom_managed / transitive)는 고칠 수 있는 항목이 있으면 그중 모듈을 가장 많이 다스리는 항목에
      모듈로 합치고 reason 에 사실을 적는다(형제 모듈을 거쳐 온 transitive 가 대부분이다 — 따로 두면 거짓 수동 검토가 된다).
      합쳐 들어온 모듈은 ``also_modules`` 에도 적는다: 그 버전 글자가 그 모듈까지 다스리는지는 정적으로 알 수 없어(외부 역할
      부모를 상속하는 회사 구조에서는 대개 아니다) 검증이 실패로 치지 않고, 남으면 다음 라운드가 따로 계획한다.
      고칠 수 있는 항목이 없으면 모듈을 가장 많이 다스리는 모양 하나로 항목 하나, 나머지 모양은 reason 에.
    * ``unmapped`` 결과(글자 불일치 등)는 합치지 않고 늘 따로 항목이 된다(문제를 숨기지 않게).

해석 결과에서 못 찾은 라이브러리(다른 버전으로 해석됐거나 아예 없음)는 플러그인(``plugin``) → 트리 안의 pom 아닌 파일
(``vendored``) → ``unmapped`` (사실을 적은 이유) 순으로 판정한다. 파일 컴포넌트 발견 항목은 늘 ``vendored``.

Links IVAS findings to what Maven resolved and to the exact line / literal text of the version in the project poms.
Rules: see the Korean block above (one item per distinct editable location; non-editable module results fold into the
item that governs most modules and are listed in its `also_modules` — the edit is not known to govern them; unmapped
results never fold). Also holds the small lxml pom model `ProjectPoms`
that `maven.py` reuses for module discovery.
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
    PomLocation,
    ResolvedDep,
    ResolvedModel,
)

log = logging.getLogger("sbmig.remediations.inventory")

MAX_PROPERTY_DEPTH = 5                   # ${a} → ${b} → … 이 정도면 충분하다; 순환은 여기서 끊긴다
EDITABLE = (O_DIRECT_LITERAL, O_DIRECT_PROPERTY, O_OWN_MANAGEMENT)
_FULL_PROP = re.compile(r"^\$\{([^}]+)\}$")
_BUILTIN_PREFIXES = ("project.", "pom.", "env.", "settings.", "maven.")
_BUILTIN_NAMES = {"version", "groupId", "artifactId", "basedir"}
FILE_KINDS = ("file_component", "file_component_potential")
# 해석된 의존성이 jar 가 아니거나 분류자(classifier)가 있을 때 reason 에 붙는 표시. planner 는 이 표시가 있는 항목에 관리 버전
# override 항목(groupId / artifactId / version 뿐인 jar 항목)을 넣지 않는다 — 그 항목은 그 변종을 다스리지 못한다.
VARIANT_MARK = "type/classifier variant"


# ---------------------------------------------------------------------------- lxml 도우미
def _local(el: Any) -> str:
    tag = getattr(el, "tag", None)
    if not isinstance(tag, str):          # 주석·처리 명령은 tag 가 함수다
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
    # 올린 ZIP 의 pom 은 믿을 수 없는 입력이다: 외부 엔티티·DTD·네트워크를 끈다
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


# ---------------------------------------------------------------------------- pom 모델
@dataclass
class PomFile:
    """프로젝트 안 pom 하나(lxml, 줄 번호 유지). ``dir`` 은 프로젝트 기준 폴더("." = 루트), ``rel`` 은 pom 경로.
    ``parent`` 는 프로젝트 안에서 찾은 부모(없으면 None — ``parent_gav`` 가 있으면 외부 부모).

    One project pom parsed with lxml (source lines kept)."""
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
    relative_path: str | None = None      # None = 기본값 ../pom.xml, "" = 빈 <relativePath/>
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
    """``base`` 폴더 기준 ``rel`` 을 프로젝트 기준 폴더로. 프로젝트 밖(..)이면 None."""
    joined = posixpath.normpath(posixpath.join("" if base == "." else base, rel.replace("\\", "/")))
    if joined.startswith("..") or joined.startswith("/"):
        return None
    return "." if joined in ("", ".") else joined


def normalize_roots(roots: list[str] | None) -> list[str]:
    """reactor 뿌리 목록을 프로젝트 기준 폴더로 고르게 한다: 빈 값·None → ``["."]``, 프로젝트 밖(``..``)·절대 경로는 버린다,
    중복 없이 순서 유지. 남는 것이 없으면 ``["."]``.

    Normalize reactor root directories (project-relative; empty → ["."], outside paths dropped, order kept)."""
    out: list[str] = []
    for r in roots or []:
        nd = _norm_dir(".", str(r).strip().strip("/") or ".")
        if nd is not None and nd not in out:
            out.append(nd)
    return out or ["."]


def parse_pom_file(project: Path, d: str) -> PomFile:
    """``project/<d>/pom.xml`` 하나를 읽는다. 읽지 못하면 ``error`` 가 채워진 PomFile(예외 없음).

    Parse one project pom; never raises (``error`` set instead)."""
    rel = _pom_rel(d)
    path = project / rel
    pf = PomFile(dir=d, rel=rel, path=path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        pf.error = f"cannot read {rel}: {type(exc).__name__}"
        return pf
    try:
        root = etree.fromstring(data, _parser())
    except etree.XMLSyntaxError as exc:
        pf.error = f"cannot parse {rel}: {str(exc)[:160]}"
        return pf
    pf.root = root
    # 줄 확인용 원문 — libxml2 는 '\n' 으로 줄을 센다
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
    """프로젝트의 pom 들: 루트에서 ``<modules>`` (프로필 포함)를 따라간 reactor 모듈과, ``relativePath`` / reactor 로 닿는
    프로젝트 안 부모. 외부 부모·BOM 은 읽지 않는다(오프라인).

    The project's poms: the reactor reached from the root `<modules>` (profiles included) plus project-internal
    parents reached by relativePath / reactor coordinates. External parents / BOMs are never read."""

    def __init__(self, project: Path, roots: list[str] | None = None):
        self.project = Path(project)
        self.poms: dict[str, PomFile] = {}
        self.reactor: list[str] = []
        self.notes: list[str] = []
        self.complete = True
        self._linked: set[str] = set()
        # reactor 의 뿌리들: 기본은 루트 pom 하나. 이관이 서비스마다 빌드한 프로젝트(루트 aggregator 없음)는 그 폴더들이다
        self.roots: list[str] = normalize_roots(roots)
        self._discover()

    # -- discovery
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
                if m.endswith(".xml"):                 # <module>sub/pom.xml</module> 도 허용
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
        if pf.relative_path != "":                     # 빈 <relativePath/> 는 파일을 보지 않는다
            rp = pf.relative_path or "../pom.xml"
            rp = rp.replace("\\", "/")
            target = posixpath.dirname(rp) if rp.endswith(".xml") else rp
            nd = _norm_dir(pf.dir, target or ".")
            if nd is not None and nd != pf.dir and (self.project / _pom_rel(nd)).is_file():
                cand = self._load(nd)
                if cand.ok and cand.ga == f"{g}:{a}" and (not v or not cand.version or cand.version == v):
                    return cand
        for cand in self.poms.values():               # reactor 안의 같은 좌표
            if cand is not pf and cand.ok and cand.ga == f"{g}:{a}" and (not v or cand.version == v):
                return cand
        return None

    # -- queries
    def chain(self, pf: PomFile) -> list[PomFile]:
        """``pf`` 와 프로젝트 안 부모들(자식 먼저). 순환이면 끊는다."""
        out: list[PomFile] = []
        cur: PomFile | None = pf
        while cur is not None and cur not in out:
            out.append(cur)
            cur = cur.parent
        return out

    def external_parent(self, pf: PomFile) -> tuple[str, PomFile, Any] | None:
        """사슬 맨 위 pom 의 외부 부모: (g:a:v, 그 ``<parent>`` 를 적은 pom, ``<version>`` 요소). 없으면 None."""
        top = self.chain(pf)[-1]
        if top.parent_gav is None or top.parent is not None:
            return None
        g, a, v = top.parent_gav
        return f"{g}:{a}:{v}", top, top.parent_version_el

    def boms(self, pf: PomFile) -> list[tuple[str, PomFile, Any]]:
        """사슬의 ``<dependencyManagement>`` 가 import 하는 BOM 들: (g:a:v 글자, 적은 pom, ``<version>`` 요소)."""
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
        """속성 정의를 자식부터 찾는다(Maven 처럼 자식이 이긴다)."""
        for p in self.chain(pf):
            if name in p.props:
                return p, p.props[name]
        return None

    def builtin(self, pf: PomFile, name: str) -> str | None:
        """``project.version`` 같은 내장 속성의 값(편집 대상이 아니다). 내장 이름이 아니면 None."""
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
        """좌표 글자의 ``${…}`` 를 풀 수 있는 만큼 푼다(비교용; 편집 자리는 ``resolve_version``)."""
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
        """버전 글자를 ``pf`` (모듈) 문맥에서 푼다: 글자 / 프로젝트 속성(마지막 글자 정의까지) / 프로젝트 밖 / 합성식 / 내장."""
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
    """버전 글자를 푼 결과. ``kind``: literal / property / external / composite / builtin / none."""
    kind: str
    value: str = ""
    pom: PomFile | None = None
    el: Any = None
    prop: str = ""
    names: list[str] = field(default_factory=list)
    detail: str = ""


def _deps(pf: PomFile, *, management: bool) -> list[tuple[Any, str]]:
    """pom 의 ``<dependency>`` 요소들(프로필 포함): (요소, 출처 표시)."""
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
    """요소 ``el`` 의 글자가 **그 줄에 그대로** 있으면 ``PomLocation``. 확인 못 하면 None (apply 가 줄 단위로 바꾸므로).

    The location of an element's text, verified against the file's lines; None when it cannot be verified."""
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


# ---------------------------------------------------------------------------- 모듈 하나의 판정
@dataclass
class _Res:
    origin: str
    location: PomLocation | None = None
    manager: str = ""
    manager_location: PomLocation | None = None
    reason: str = ""
    via: list[str] = field(default_factory=list)
    # 이 모듈에서 이 라이브러리를 끌고 온 사내 아티팩트 g:a 들(``inhouse.inhouse_on_path``). 묶는 열쇠에는 넣지 않는다 —
    # 같은 버전 글자 자리는 사내 아티팩트와 상관없이 한 항목이다; 항목의 ``inhouse`` / ``inhouse_modules`` 로 모은다
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


def _manager_loc(pp: ProjectPoms, mod: PomFile, pf: PomFile, vel: Any, element: str) -> PomLocation | None:
    ref = pp.resolve_version(mod, _text(vel))
    if ref.kind == "literal":
        return location_of(pf, vel, element)
    if ref.kind == "property" and ref.pom is not None:
        return location_of(ref.pom, ref.el, element, ref.prop)
    return None


def _external(pp: ProjectPoms, mod: PomFile, ga: str, managed_by: Callable[[str], str | None] | None,
              why: str = "") -> _Res:
    """프로젝트 밖에서 관리되는 버전: 외부 부모 / import 한 BOM / 출처 불명."""
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
        if ref.value != version:
            return _Res(O_UNMAPPED, reason=f"{decl_pom.rel} declares {ga} {ref.value}, Maven resolves {version}")
        return _Res(origin_literal, location=loc)
    if ref.kind == "property":
        assert ref.pom is not None
        loc = location_of(ref.pom, ref.el, "property", ref.prop)
        if loc is None:
            return _Res(O_UNMAPPED, reason=f"property {ref.prop} in {ref.pom.rel} could not be located on one line")
        if ref.value != version:
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
            ref = pp.resolve_version(mod, _text(vel))      # Maven 은 자식(모듈) 문맥에서 속성을 푼다
            return _from_ref(pp, mod, ga, version, ref, dpom, vel, O_DIRECT_LITERAL, O_DIRECT_PROPERTY, "dependency",
                             managed_by)
    mg = _find_decl(pp, mod, ga, management=True)
    if mg is not None and _text(_kid(mg[1], "version")):
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
        # 왜: 합쳐 들어온 모듈은 다른 길(간접·부모/BOM 관리)로 같은 버전이 된 곳이다 — 이 항목의 버전 글자가 그 모듈도
        # 다스리는지는 정적으로 알 수 없다. 검증은 이 모듈들을 실패로 치지 않고, 남으면 다음 라운드가 따로 계획한다
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
            # 고칠 수 있는 항목 중 모듈을 가장 많이 다스리는 것(동률이면 정렬 순서 첫째)에 합친다
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
    """회사 부모(``inhouse_groups`` 의 ``parents``)에서 파생된 결과면 ``[그 부모 g:a]``, 아니면 []. 부모 g:a 는 의존성 사슬에
    나타나지 않으므로 사슬로는 못 본다 — 모듈 사슬 맨 위의 **외부** 부모가 회사 부모일 때 다음 둘 중 하나면 그 부모 몫이다
    (사용자 2026-11-12: "parent, boot-jar, comp-jar 에서 파생된 것도 아키텍처팀에 문의해야 고쳐진다"):
      1. 출처가 ``parent_managed`` 이고 출처 불명(부모 + 프로젝트 BOM 등) 갈래가 아니다 — 버전이 부모 사슬에서 관리되거나,
         프로젝트 pom 이 선언하지 않은 직접 의존성(부모의 ``<dependencies>`` 에서 물려받음)이다;
      2. 간접 결과(``transitive`` 또는 사슬이 있는 관리 결과)인데 사슬 머리(모듈의 직접 의존성)를 모듈의 프로젝트 pom 사슬이
         선언하지 않았다 — 머리를 부모에서 물려받았으니 그 아래도 부모와 함께 왔다.
    고칠 수 있는 출처·unmapped·프로젝트가 import 한 BOM 의 관리는 부모 몫이 아니다(프로젝트가 다스린다).

    `[parent g:a]` when the result derives from an in-house company parent (managed through its chain, inherited from
    its `<dependencies>`, or transitive under a dependency the project poms do not declare); else []."""
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
    """항목의 모듈들(합쳐 들어온 also 모듈 포함)의 사내 아티팩트 결과를 모은다: ``inhouse`` = 합집합(정렬),
    ``inhouse_modules`` = 결과가 비지 않은 모듈."""
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
    """IVAS 위치 중 트리에 실제로 있는 pom 아닌 파일(IVAS 가 준 글자 그대로). ZIP 이 최상위 폴더로 싸여 있으면 위치에 그
    폴더 이름이 붙어 오므로 앞 조각을 몇 개까지 떼어 보고 찾는다."""
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


def _plugin_hits(resolved: ResolvedModel, ga: str) -> tuple[list[str], list[str]]:
    mods, versions = [], []
    for m in resolved.modules:
        for p in m.plugins:
            if p.ga == ga:
                mods.append(m.dir)
                if p.version:
                    versions.append(p.version)
    return sorted(set(mods)), sorted(set(versions))


def split_patched_versions(text: str) -> list[str]:
    """IVAS 패치 버전 글자 하나("2.12.7.1, 2.13.4.2")를 낱개 버전으로: 쉼표·세미콜론·공백으로 나누되 ``[`` ``(`` … ``]`` ``)``
    범위 안의 쉼표는 나누지 않는다(범위는 한 덩어리로 남아 planner 가 "버전이 아닌 힌트" 로 잔여에 남긴다). 빈 조각은 버리고,
    순서는 유지, 중복은 뺀다. 회사가 여러 버전이 한 글자에 오는 모양을 확인해 주지 않았다 — 오면 이렇게 읽는다.

    Split one IVAS patched-version text on commas / semicolons / whitespace, keeping bracketed ranges whole."""
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
def build_inventory(project: Path, findings: list[Finding], resolved: ResolvedModel, *,
                    managed_by: Callable[[str], str | None] | None = None,
                    inhouse: Mapping[str, str] | None = None) -> Inventory:
    """발견 항목 → ``Inventory`` (``interfaces.InventoryFn``). 해석 결과와 같은 g:a·같은 버전인 라이브러리만 자동 출처를 받고,
    나머지는 plugin / vendored / unmapped. ``managed_by`` (g:a → 관리하는 부모·BOM 의 g:a:v)를 주면 외부 관리 판정에 그 사실을
    쓴다. 해석 결과가 불완전하거나 프로젝트 pom 을 못 읽으면 ``complete=False``. 순서는 (ga, 버전, 출처, pom, 줄).
    ``inhouse`` (사내 아티팩트 g:a → 그룹, ``inhouse.inhouse_keys``)를 주면 모듈마다 그 라이브러리를 끌고 온 사내 아티팩트를
    항목의 ``inhouse`` / ``inhouse_modules`` 에 적는다(planner 가 그 항목을 담당자 몫의 잔여로 남긴다): 의존성 사슬에 있는 것,
    그리고 그룹 ``parents`` 의 회사 부모에서 파생된 것(``_parent_inhouse``). 없으면 둘 다 빈 목록.

    Findings → inventory. Deterministic order; `complete=False` when the resolution is incomplete or a pom is unreadable.
    With `inhouse` (in-house g:a → group) each item records the in-house artifacts that bring it and in which modules."""
    project = Path(project)
    # 해석 결과가 서비스마다의 reactor 에서 왔으면(루트 aggregator 없음) 같은 뿌리들로 pom 을 읽는다
    pp = ProjectPoms(project, roots=list(getattr(resolved, "roots", None) or []) or None)
    inv = Inventory(complete=bool(resolved.complete) and pp.complete)
    inv.notes += list(resolved.notes) + list(pp.notes)
    if not resolved.complete and not resolved.notes:
        inv.notes.append("Maven resolution is incomplete")

    items: list[InventoryItem] = []
    # 회사 부모(그룹 parents)는 의존성 사슬에 나오지 않는다 — 모듈의 외부 부모로 따로 본다(_parent_inhouse)
    parent_keys = {k for k, grp in (inhouse or {}).items() if grp == PARENTS_GROUP}

    # 라이브러리 발견 항목을 (g:a, 버전)으로 묶는다
    lib_groups: dict[tuple[str, str], list[Finding]] = {}
    unparsable: dict[tuple[str, str], list[Finding]] = {}
    file_groups: dict[tuple[str, str], list[Finding]] = {}
    for f in findings:
        if f.kind == "library":
            ga = _split_ga(f.identifier)
            if ga is None:
                unparsable.setdefault((f.identifier, f.version), []).append(f)
            else:
                lib_groups.setdefault((ga, f.version), []).append(f)
        elif f.kind in FILE_KINDS:
            file_groups.setdefault((f.identifier, f.version), []).append(f)
        else:
            unparsable.setdefault((f.identifier, f.version), []).append(f)

    def decorate(it: InventoryItem, fs: list[Finding]) -> InventoryItem:
        it.finding_ids = sorted({f.id for f in fs if f.id})
        # 원소 하나 = 발견 항목 하나의 힌트(낱개 버전을 ", " 로 다시 이은 정규형). 발견 항목끼리는 AND(모두 고쳐야 한다),
        # 원소 안의 여러 버전은 줄마다의 대안(OR) — planner 가 그렇게 읽는다. 납작하게 펴면 이 구분이 사라진다.
        it.patched_versions = sorted({", ".join(alts) for f in fs if (alts := split_patched_versions(f.patched_version))})
        return it

    for (ga, version), fs in sorted(lib_groups.items()):
        results: list[tuple[str, _Res]] = []
        for m in resolved.modules:
            for dep in m.dependencies:
                if dep.ga != ga or dep.version != version or not version:
                    continue
                pf = pp.poms.get(m.dir)
                if pf is None:
                    pf = parse_pom_file(project, m.dir)
                if not pf.ok:
                    inv.complete = False
                    note = f"module {m.dir}: pom not readable ({pf.error or 'missing'})"
                    if note not in inv.notes:
                        inv.notes.append(note)
                    results.append((m.dir, _Res(O_UNMAPPED, reason=note,
                                                inhouse=inhouse_on_path(dep.ga, dep.path, inhouse))))
                    continue
                res = _classify(pp, pf, dep, managed_by)
                res.inhouse = inhouse_on_path(dep.ga, dep.path, inhouse)
                for pga in _parent_inhouse(pp, pf, dep, res, parent_keys):
                    if pga not in res.inhouse:
                        res.inhouse.append(pga)
                variants = sorted({f"{d.type or 'jar'}/{d.classifier}" for d in m.dependencies
                                   if d.ga == ga and d.version == version and ((d.type or "jar") != "jar" or d.classifier)})
                if variants and not res.editable:
                    res.reason = "; ".join(x for x in [res.reason, f"{VARIANT_MARK} ({', '.join(variants)})"] if x)
                results.append((m.dir, res))
                break                                   # 모듈마다 한 번(분류자만 다른 같은 g:a 는 같은 자리)
        if results:
            marked = sorted({m for m, r in results if VARIANT_MARK in r.reason})
            for it in _group(ga, version, results):
                # 왜: 다른 모듈의 결과가 이 항목에 합쳐지면 그 모듈의 reason 은 사라진다 — 변종 표시는 남겨야 override 를 막는다
                if it.origin not in EDITABLE and VARIANT_MARK not in it.reason and set(marked) & set(it.modules):
                    it.reason = "; ".join(x for x in [it.reason, f"{VARIANT_MARK} in {', '.join(marked)}"] if x)
                _fill_inhouse(it, results)
                items.append(decorate(it, fs))
            continue
        items.append(decorate(_unresolved(project, resolved, ga, version, fs), fs))

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
                           reason="file component in the tree; not changed automatically")
        items.append(decorate(it, fs))

    items.sort(key=lambda it: (it.ga, it.version, it.origin, it.location.pom if it.location else "",
                               it.location.line if it.location else 0))
    inv.items = items
    return inv


def _unresolved(project: Path, resolved: ResolvedModel, ga: str, version: str, fs: list[Finding]) -> InventoryItem:
    """해석 결과에 (그 버전으로) 없는 라이브러리: plugin → vendored → unmapped (사실을 적는다)."""
    mods, pversions = _plugin_hits(resolved, ga)
    if mods:
        return InventoryItem(ga=ga, version=version, origin=O_PLUGIN, modules=mods,
                             reason=("build plugin or plugin dependency"
                                     + (f" (declared {', '.join(pversions)})" if pversions else "")
                                     + "; plugin versions are declared, not resolved; not changed automatically"))
    paths = _vendored_paths(project, [loc for f in fs for loc in f.locations])
    if paths:
        return InventoryItem(ga=ga, version=version, origin=O_VENDORED, paths=paths,
                             reason="file in the tree (not resolved by Maven); not changed automatically")
    seen = sorted(resolved.versions_of(ga))
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
