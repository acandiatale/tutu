"""사내 부모 pom 체인이 정의한 속성과 관리 버전을 읽는 모듈.
설정한 사내 부모의 pom을 저장소에서 읽고 ``<parent>``를 따라 올라간다.

Used in place of the effective pom when it cannot be captured after the restructure;
``_drop_bom_property_overrides`` uses the chain's property names. Never raises: a pom that cannot be read is
listed in ``unreadable`` and ``sources``. Only a result that read something is cached in the process.
Entry points: ``load_parent_chain_facts(cfg) -> ParentChainFacts`` and ``ParentChainFacts.as_capture()``.
Every configured company parent (``artifacts.parent``, ``boot_parent``, ``comp_parent``; de-duplicated, versionless ones
skipped) is read from the local repository or through ``RepositoryLookup.pom_text`` (``boot_bom.PomReader``), its
``<parent>`` followed upwards by repository coordinates (``relativePath`` ignored, ``${…}`` in the parent coordinates
resolved from the child's properties), and the ``import`` BOMs of every pom of the chain read one level deep
(``boot_bom.read_import_boms``). Merge rules (Maven's):
* a property defined by a child pom wins over its parent's;
* a ``<dependencyManagement>`` entry declared nearer the root parent wins over one declared further up, and every
  declared entry wins over the imported BOMs' (imports in child-first order, then declaration order);
* an imported BOM's properties are NOT inherited (not in ``properties``);
* with several roots the first root (``artifacts.parent``) wins in the merged views.
When ``artifacts.parent_manages_spring_boot`` and no pom of the chain is (or imports) ``spring-boot-dependencies`` /
``spring-boot-starter-parent``, the Boot BOM of ``target.spring_boot_version`` is added at the lowest priority
(``boot_bom.load_boot_bom_facts``). At most ``MAX_POMS`` poms per load.
Contract: docs/INTERFACES.md "v2.1p".
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from .boot_bom import (
    BOOT_BOM,
    BOOT_GROUP,
    PomReader,
    PomSummary,
    local_repository,
    make_resolver,
    parse_pom,
    pom_builtins,
    read_import_boms,
)

log = logging.getLogger("sbmig.build.parent_chain")

MAX_POMS = 80                     # 모든 루트 체인의 pom과 import BOM을 합친 개수
MAX_DEPTH = 12                    # 루트마다 따라갈 ``<parent>`` 단계 수. 순환 참조를 막는다
BOOT_CHAIN_KEYS = frozenset({f"{BOOT_GROUP}:{BOOT_BOM}", f"{BOOT_GROUP}:spring-boot-starter-parent"})

_CACHE: dict[tuple[Any, ...], ParentChainFacts] = {}


class ParentChainFacts(BaseModel):
    """사내 부모 체인에서 읽은 속성, 관리 버전, 읽은 pom 기록을 담는 클래스.
    속성마다 그 속성을 정의한 가장 가까운 pom을 ``property_sources``에 둔다.

    ``properties`` are resolved; ``sources`` has one line per pom read or not; ``poms`` counts the poms read;
    ``boot_bom_added`` says the Boot BOM was added because the chain did not reach it."""
    roots: list[str] = Field(default_factory=list)
    managed: dict[str, str] = Field(default_factory=dict)
    properties: dict[str, str] = Field(default_factory=dict)
    property_sources: dict[str, str] = Field(default_factory=dict)
    sources: list[str] = Field(default_factory=list)
    unreadable: list[str] = Field(default_factory=list)
    poms: int = 0
    boot_bom_added: bool = False
    # Boot BOM을 보충했을 때 체인이 직접 관리하는 항목. None이면 보충하지 않은 것이고 ``managed``와 같다
    chain_managed: dict[str, str] | None = None
    # 체인이 상속이나 import로 spring-boot-dependencies까지 이어지는지. None은 읽은 pom이 없어 알 수 없다는 뜻
    reaches_boot_bom: bool | None = None
    # 루트 부모 g:a:v별로 그 체인이 정의한 속성 이름. comp-jar를 상속한 모듈은 comp-jar 체인의 속성만 본다
    root_properties: dict[str, list[str]] = Field(default_factory=dict)
    # 체인 중 회사 pom이 정의한 속성. 재구성이 ``${…}``로 참조해도 되는 속성은 이것뿐이다
    # Company pom: see is_company_group (never org.springframework.*); `properties` stays complete.
    company_properties: dict[str, str] = Field(default_factory=dict)
    company_property_sources: dict[str, str] = Field(default_factory=dict)
    # 체인의 pom이 선언한 build extension. 상속되는 확장 하나만 받지 못해도 모든 모듈의 Maven 호출이 실패한다
    build_extensions: list[str] = Field(default_factory=list)
    build_extension_sources: dict[str, str] = Field(default_factory=dict)
    # 치환 전 선언 (groupId, artifactId, version, 선언한 pom). 체인 속성을 모두 합친 뒤 ``_load``에서 치환한다
    extension_decls: list[tuple[str, str, str, str]] = Field(default_factory=list)
    # 루트 부모 g:a:v별 관리 버전. 모듈은 사내 부모 하나만 상속한다. Boot BOM 보충분은 넣지 않는다
    root_managed: dict[str, dict[str, str]] = Field(default_factory=dict)
    # 보충한 Boot BOM의 관리 버전. ``boot_bom_added``가 아니면 비어 있고 루트마다 가장 낮은 우선순위로 쓴다
    addon_managed: dict[str, str] = Field(default_factory=dict)

    @property
    def own_managed(self) -> dict[str, str]:
        """Boot BOM에서 보충한 버전을 빼고 체인이 직접 관리하는 버전만 반환한다."""
        return self.managed if self.chain_managed is None else self.chain_managed

    def managed_for(self, parent: str | None, *, own: bool = False) -> dict[str, str]:
        """``<parent>``가 ``parent``인 pom이 상속받는 관리 버전을 반환한다.
        ``g:a:v``가 맞는 루트가 없으면 ``g:a``로 찾는다. 사내 루트 부모가 아니면 빈 dict를 반환한다.
        ``own``이면 Boot BOM 보충분을 뺀다.

        That root's chain only, plus the Boot BOM add-on at the lowest priority unless ``own``. Facts without per-root
        sets (built by hand, or stored without ``root_managed``) give the merged view (``managed``, or ``own_managed``
        with ``own``)."""
        if not self.root_managed:
            return dict(self.own_managed if own else self.managed)
        hit = self.root_managed.get(parent or "")
        if hit is None and parent:
            ga = ":".join(parent.split(":")[:2])
            hit = next((m for r, m in self.root_managed.items() if ":".join(r.split(":")[:2]) == ga), None)
        if hit is None:
            return {}
        if own or not self.boot_bom_added:
            return dict(hit)
        return {**self.addon_managed, **hit}

    @property
    def empty(self) -> bool:
        """관리 버전도 속성도 없으면 True를 반환한다. pom을 하나도 읽지 못했다는 뜻이다."""
        return not self.managed and not self.properties

    def as_capture(self) -> dict[str, Any]:
        """``load_effective``와 같은 형태의 dict로 반환한다. 재구성 뒤 effective pom 대신 쓴다.

        The ``sbmig.build.effective.load_effective`` shape; ``direct``, ``plugins``, ``modules`` and ``by_module`` are
        empty, ``parent`` is the first root."""
        return {"managed": dict(self.managed), "direct": {}, "plugins": {}, "properties": dict(self.properties),
                "parent": self.roots[0] if self.roots else None, "modules": [], "by_module": {}}


def clear_cache() -> None:
    """프로세스 안의 부모 pom 캐시를 비운다. 테스트에서 호출한다."""
    _CACHE.clear()


def _roots(cfg: Any) -> list[tuple[str, str, str]]:
    a = getattr(cfg, "artifacts", None)
    out: list[tuple[str, str, str]] = []
    for spec in (getattr(a, "parent", None), getattr(a, "boot_parent", None), getattr(a, "comp_parent", None)):
        if spec is None:
            continue
        g, art, v = (str(getattr(spec, k, "") or "").strip() for k in ("group_id", "artifact_id", "version"))
        if g and art and v and (g, art, v) not in out:
            out.append((g, art, v))
    return out


def _walk(reader: PomReader, root: tuple[str, str, str], facts: ParentChainFacts) -> list[PomSummary]:
    """루트 부모에서 ``<parent>``를 따라 올라가며 읽은 pom 목록을 자식부터 반환한다.
    읽지 못한 pom이나 치환할 수 없는 부모 좌표에서 멈춘다. 읽지 못한 pom은 ``unreadable``에 넣는다.

    Also collects each pom's build extensions into ``facts.extension_decls`` (resolved later in ``_load``)."""
    chain: list[PomSummary] = []
    seen: set[tuple[str, str, str]] = set()
    cur: tuple[str, str, str] | None = root
    for _ in range(MAX_DEPTH):
        if cur is None or cur in seen:
            break
        seen.add(cur)
        coord = ":".join(cur)
        text, where = reader.pom(*cur)
        pom = parse_pom(text) if text else None
        if pom is None:
            why = where if not text else "not a pom"
            facts.sources.append(f"{coord}: {why}")
            if coord not in facts.unreadable:
                facts.unreadable.append(coord)
            break
        facts.poms += 1
        facts.sources.append(f"{coord}: {where}")
        chain.append(pom)
        for eg, ea, ev in pom.extensions:                     # 상속되는 build extension은 체인 속성을 합친 뒤 치환한다
            facts.extension_decls.append((eg, ea, ev, coord))
        resolve = make_resolver(pom.properties, pom_builtins(pom))
        if not (pom.parent_group_id and pom.parent_artifact_id and pom.parent_version):
            break
        # <parent> 좌표의 ${…}는 자식 pom의 속성으로 치환한다. relativePath는 보지 않는다
        pg, pa, pv = resolve(pom.parent_group_id), resolve(pom.parent_artifact_id), resolve(pom.parent_version)
        if not (pg and pa and pv):
            facts.sources.append(f"{coord}: parent {pom.parent_group_id}:{pom.parent_artifact_id}:"
                                 f"{pom.parent_version} not resolvable — chain stops here")
            break
        cur = (pg, pa, pv)
    return chain


def _company_groups(cfg: Any, roots: list[tuple[str, str, str]]) -> list[str]:
    """회사 pom으로 볼 groupId 목록을 반환한다. 사내 부모와 필수, 선택 아티팩트의 group이다.
    하위 group은 ``is_company_group``이 함께 본다."""
    out: list[str] = [g for g, _a, _v in roots]
    a = getattr(cfg, "artifacts", None)
    for spec in [*(getattr(a, "mandatory", None) or []), *(getattr(a, "optional", None) or [])]:
        g = str(getattr(spec, "group_id", "") or "")
        if g and g not in out:
            out.append(g)
    return out


def is_company_group(group: str, groups: list[str]) -> bool:
    """``group``이 ``groups`` 중 하나이거나 그 하위 group이면 회사 pom으로 보고 True를 반환한다.
    ``org.springframework`` 계열은 사내 부모 체인에 있어도 False다."""
    g = (group or "").strip()
    if not g or g == "org.springframework" or g.startswith("org.springframework."):
        return False
    return any(g == x or g.startswith(x + ".") for x in groups)


def _merge_root(reader: PomReader, chain: list[PomSummary], facts: ParentChainFacts, root: str = "",
                company: list[str] | None = None) -> bool:
    """루트 하나의 체인을 결과에 합치고, 체인이 Boot BOM까지 이어지는지 반환한다.

    Fills this root's ``root_properties`` / ``root_managed`` and the merged ``properties``, ``company_properties`` and
    ``managed``; read import BOMs count in ``poms``, unreadable ones go to ``unreadable``."""
    if not chain:
        return False
    raw: dict[str, str] = {}
    defined_by: dict[str, str] = {}
    for pom in reversed(chain):                                 # 조상부터 넣어 자식 값이 덮어쓰게 한다
        for k, v in pom.properties.items():
            raw[k] = v
            defined_by[k] = pom.coord
    if root:
        facts.root_properties[root] = sorted(raw)
    resolve = make_resolver(raw, pom_builtins(chain[0]))
    resolved = {k: (resolve(v) or v) for k, v in raw.items()}
    for k, v in resolved.items():
        if k not in facts.properties:                           # 루트가 여럿이면 먼저 읽은 루트의 값을 쓴다
            facts.properties[k] = v
            facts.property_sources[k] = defined_by[k]
        # ${…}로 참조해도 되는 것은 회사 pom이 정의한 속성뿐이다. Boot BOM의 lombok.version 같은 속성은 뺀다
        if k not in facts.company_properties and is_company_group(defined_by[k].split(":", 1)[0], company or []):
            facts.company_properties[k] = v
            facts.company_property_sources[k] = defined_by[k]
    reached = any(f"{p.group_id}:{p.artifact_id}" in BOOT_CHAIN_KEYS for p in chain)
    imports: list[tuple[str, str, str]] = []
    managed: dict[str, str] = {}
    for pom in chain:                                           # 자식부터 보므로 가까운 선언이 우선한다
        presolve = make_resolver(resolved, pom_builtins(pom))
        for g, a, v, typ, scope in pom.managed:
            rg, ra, rv = presolve(g), presolve(a), presolve(v)
            if not (rg and ra and rv):
                continue
            if scope == "import" and typ == "pom":
                if (rg, ra, rv) not in imports:
                    imports.append((rg, ra, rv))
                reached = reached or f"{rg}:{ra}" in BOOT_CHAIN_KEYS
                continue
            managed.setdefault(f"{rg}:{ra}", rv)
    # Maven 규칙대로 직접 선언한 관리 항목이 import BOM보다 우선한다. import에서는 아직 없는 g:a만 더한다
    before = len(facts.sources)
    read_import_boms(reader, imports, resolved, managed, facts.sources)
    for line in facts.sources[before:]:
        if line.endswith(" managed)"):
            facts.poms += 1
        else:
            coord = line.split(": ", 1)[0]
            if coord not in facts.unreadable:
                facts.unreadable.append(coord)
    for k, v in managed.items():
        facts.managed.setdefault(k, v)
    if root and root not in facts.root_managed:
        facts.root_managed[root] = dict(managed)
    return reached


def _load(cfg: Any, roots: list[tuple[str, str, str]], local: Any) -> ParentChainFacts:
    facts = ParentChainFacts(roots=[":".join(r) for r in roots])
    if not roots:
        facts.sources.append("no in-house parent with a version configured (artifacts.parent / boot_parent / "
                             "comp_parent)")
    reader = PomReader(cfg, local, limit=MAX_POMS)
    reached = False
    read_any = False
    company = _company_groups(cfg, roots)
    for root in roots:
        chain = _walk(reader, root, facts)
        read_any = read_any or bool(chain)
        reached = _merge_root(reader, chain, facts, ":".join(root), company) or reached
    # extension 버전 속성은 선언한 pom의 부모에 있을 수도 있다. 그래서 체인 전체 속성으로 치환한다
    resolve_all = make_resolver(facts.properties)
    for eg, ea, ev, coord in facts.extension_decls:
        ext = ":".join(resolve_all(x) or x or "?" for x in (eg, ea, ev))
        if ext not in facts.build_extensions:
            facts.build_extensions.append(ext)
            facts.build_extension_sources[ext] = coord
    facts.reaches_boot_bom = reached if read_any else None
    manages_boot = bool(getattr(getattr(cfg, "artifacts", None), "parent_manages_spring_boot", False))
    if manages_boot and not reached:
        # 함수가 아닌 모듈을 import한다. 테스트가 ``load_boot_bom_facts``를 바꿔 넣을 수 있다
        from sbmig.build import boot_bom
        bb = boot_bom.load_boot_bom_facts(cfg)
        if not bb.empty:
            facts.boot_bom_added = True
            facts.chain_managed = dict(facts.managed)          # 보충하기 전에 체인이 직접 관리하던 버전
            facts.addon_managed = dict(bb.managed)
            for k, v in bb.managed.items():
                facts.managed.setdefault(k, v)
            # 보충한 Boot BOM은 import와 같아서 속성이 상속되지 않는다. properties에는 넣지 않는다
        facts.sources += [f"Boot BOM add-on (the chain does not reach {BOOT_BOM}): {s}" for s in bb.sources]
    return facts


def load_parent_chain_facts(cfg: Any) -> ParentChainFacts:
    """설정한 사내 부모 체인을 읽어 반환한다. 예외를 던지지 않는다.
    읽은 것이 있으면 프로세스 안에 캐시한다. 아무것도 읽지 못했으면 다음 호출에서 다시 읽는다.

    Cache key: (roots, local repository, ``parent_manages_spring_boot``, ``target.spring_boot_version``)."""
    try:
        roots = _roots(cfg)
        local = local_repository(cfg)
        boot = (bool(getattr(getattr(cfg, "artifacts", None), "parent_manages_spring_boot", False)),
                str(getattr(getattr(cfg, "target", None), "spring_boot_version", "") or ""))
        key = (tuple(roots), str(local), boot)
        hit = _CACHE.get(key)
        if hit is not None:
            return hit
        facts = _load(cfg, roots, local)
        if not facts.empty:
            _CACHE[key] = facts
            log.info("parent chain facts %s: %d pom(s), %d managed artifact(s), %d propert(ies)%s%s",
                     ", ".join(facts.roots), facts.poms, len(facts.managed), len(facts.properties),
                     " + the Boot BOM" if facts.boot_bom_added else "",
                     f"; unreadable: {', '.join(facts.unreadable)}" if facts.unreadable else "")
        else:
            log.info("parent chain facts unavailable: %s", "; ".join(facts.sources) or "-")
        return facts
    except Exception as e:  # 부모 체인 정보는 보조 자료다. 실패해도 재구성을 막지 않는다
        log.debug("parent chain facts failed: %s: %s", type(e).__name__, e, exc_info=True)
        return ParentChainFacts(sources=[f"failed: {type(e).__name__}: {e}"[:200]])


__all__ = ["BOOT_CHAIN_KEYS", "MAX_POMS", "ParentChainFacts", "clear_cache", "is_company_group",
           "load_parent_chain_facts"]
