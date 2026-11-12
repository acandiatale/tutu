"""사내 부모 pom 체인이 정의한 속성과 관리 버전을 읽는 모듈.
설정한 사내 부모의 pom을 저장소에서 읽고 ``<parent>``를 따라 올라간다.
속성은 자식 pom이 우선하고, 관리 버전은 먼저 정의한 쪽이 우선한다(import BOM 포함).
재구성 뒤 effective pom을 얻지 못했을 때 대신 쓰는 정보이고,
``_drop_bom_property_overrides``가 부모 속성 이름 목록으로도 쓴다.
- 진입점은 ``load_parent_chain_facts(cfg) -> ParentChainFacts``와 ``ParentChainFacts.as_capture()``다
- pom 읽기, ``${…}`` 치환, import BOM 읽기는 ``boot_bom.py``의 공유 함수를 쓰고, 최대 ``MAX_POMS``개까지 읽는다
- 성공한 결과만 프로세스 안에서 캐시한다.
  예외를 던지지 않고, 읽지 못한 pom은 ``unreadable`` / ``sources``에 기록한다

The in-house parent chain's facts (v2.1p, docs/INTERFACES.md "v2.1p"): every configured company parent (`artifacts.parent`,
`boot_parent`, `comp_parent`; de-duplicated, versionless ones skipped) is read from the local repository or through
`RepositoryLookup.pom_text` (`boot_bom.PomReader`), its `<parent>` followed upwards by repository coordinates
(`relativePath` ignored, `${…}` in the parent coordinates resolved from the child's properties), and the `import` BOMs of
every pom of the chain read one level deep (`boot_bom.read_import_boms`). Merge rules (Maven's): a property defined by a
child pom wins over its parent's; a `<dependencyManagement>` entry declared nearer the root parent wins over one declared
further up, and every declared entry wins over the imported BOMs' (imports in child-first order, then declaration order);
an imported BOM's properties are NOT inherited (not in `properties`). With several roots the first root (artifacts.parent)
wins. When `artifacts.parent_manages_spring_boot` and no pom of the chain is (or imports) `spring-boot-dependencies` /
`spring-boot-starter-parent`, the Boot BOM of `target.spring_boot_version` is added at the lowest priority
(`boot_bom.load_boot_bom_facts`, so nothing is lost against v2.1o). At most `MAX_POMS` poms per load; successful loads
cached per process per (roots, local repository, Boot switch / version); never raises."""
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

MAX_POMS = 80                     # every pom of every root chain + their imported BOMs
MAX_DEPTH = 12                    # <parent> hops per root (a cycle guard)
BOOT_CHAIN_KEYS = frozenset({f"{BOOT_GROUP}:{BOOT_BOM}", f"{BOOT_GROUP}:spring-boot-starter-parent"})

_CACHE: dict[tuple[Any, ...], ParentChainFacts] = {}


class ParentChainFacts(BaseModel):
    """사내 부모 체인의 속성, 관리 버전, 읽은 pom 기록을 담는 데이터 클래스.
    속성마다 그 속성을 정의한 가장 가까운 pom을 ``property_sources``에 기록한다.

    The chain's facts: the roots read, managed versions, the inherited properties (resolved) with the pom that defines
    each (the child-most one), one `sources` line per pom read or not, the unreadable poms, the number of poms read,
    and whether the Boot BOM was added because the chain did not reach it."""
    roots: list[str] = Field(default_factory=list)
    managed: dict[str, str] = Field(default_factory=dict)
    properties: dict[str, str] = Field(default_factory=dict)
    property_sources: dict[str, str] = Field(default_factory=dict)
    sources: list[str] = Field(default_factory=list)
    unreadable: list[str] = Field(default_factory=list)
    poms: int = 0
    boot_bom_added: bool = False
    # v2.1s: the chain's OWN managed entries when the Boot BOM was added (None = no add-on: the same as `managed`)
    chain_managed: dict[str, str] | None = None
    # v2.1t: did the chain itself reach spring-boot-dependencies (inherited or imported)? None = unknown (nothing read)
    reaches_boot_bom: bool | None = None
    # v2.1t: root parent g:a:v → the property names ITS chain defines (a module on comp-jar sees only comp-jar's chain)
    root_properties: dict[str, list[str]] = Field(default_factory=dict)
    # v2.1t: the properties a COMPANY pom of the chain defines (groupId of a root / an in-house group, never
    # org.springframework.*) — the only ones the restructure may reference as ${…}; `properties` stays complete
    company_properties: dict[str, str] = Field(default_factory=dict)
    company_property_sources: dict[str, str] = Field(default_factory=dict)
    # 체인의 pom이 선언한 build extension. 상속되는 확장 하나만 받지 못해도 모든 모듈의 Maven 호출이 실패한다
    build_extensions: list[str] = Field(default_factory=list)
    build_extension_sources: dict[str, str] = Field(default_factory=dict)
    # the raw declarations (groupId, artifactId, version, declaring pom) — resolved after the chain's properties are merged
    extension_decls: list[tuple[str, str, str, str]] = Field(default_factory=list)

    @property
    def own_managed(self) -> dict[str, str]:
        """Boot BOM에서 보충한 버전을 빼고, 체인이 직접 관리하는 버전만 반환한다.

        The chain's own managed versions, without the Boot BOM add-on."""
        return self.managed if self.chain_managed is None else self.chain_managed

    @property
    def empty(self) -> bool:
        """관리 버전도 속성도 없는지, 즉 pom을 하나도 읽지 못했는지 확인한다."""
        return not self.managed and not self.properties

    def as_capture(self) -> dict[str, Any]:
        """재구성 뒤 effective pom 대신 쓸 수 있도록 ``load_effective``와 같은 형태로 바꿔 반환한다.

        The `sbmig.build.effective.load_effective` shape (`by_module` empty)."""
        return {"managed": dict(self.managed), "direct": {}, "plugins": {}, "properties": dict(self.properties),
                "parent": self.roots[0] if self.roots else None, "modules": [], "by_module": {}}


def clear_cache() -> None:
    """프로세스 안의 부모 pom 캐시를 비운다. 테스트에서 호출한다."""
    _CACHE.clear()


def _roots(cfg: Any) -> list[tuple[str, str, str]]:
    """설정한 사내 부모의 (g, a, v) 목록을 반환한다. 중복된 것과 버전이 없는 것은 뺀다."""
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
    """루트 부모에서 ``<parent>``를 따라 올라가며 pom 목록을 자식부터 반환한다.
    읽지 못한 pom에서 멈추고 ``unreadable``에 넣는다."""
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
        for eg, ea, ev in pom.extensions:                     # 2026-11-05: inherited build extensions, resolved in _load (merged props)
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
    """회사 pom으로 볼 groupId 목록을 반환한다.
    사내 부모와 필수, 선택 아티팩트의 group과 그 하위 group이 대상이다."""
    out: list[str] = [g for g, _a, _v in roots]
    a = getattr(cfg, "artifacts", None)
    for spec in [*(getattr(a, "mandatory", None) or []), *(getattr(a, "optional", None) or [])]:
        g = str(getattr(spec, "group_id", "") or "")
        if g and g not in out:
            out.append(g)
    return out


def is_company_group(group: str, groups: list[str]) -> bool:
    """이 groupId의 pom이 회사 것인지 확인한다.
    ``org.springframework`` 계열은 사내 부모 체인에 있어도 회사 것으로 보지 않는다.

    A pom of `group` is a company pom: equal to / under one of `groups`, never `org.springframework` or below."""
    g = (group or "").strip()
    if not g or g == "org.springframework" or g.startswith("org.springframework."):
        return False
    return any(g == x or g.startswith(x + ".") for x in groups)


def _merge_root(reader: PomReader, chain: list[PomSummary], facts: ParentChainFacts, root: str = "",
                company: list[str] | None = None) -> bool:
    """루트 하나의 체인을 결과에 합치고, 체인이 Boot BOM까지 이어지는지 반환한다."""
    if not chain:
        return False
    raw: dict[str, str] = {}
    defined_by: dict[str, str] = {}
    for pom in reversed(chain):                                 # ancestor first: the child overwrites (child wins)
        for k, v in pom.properties.items():
            raw[k] = v
            defined_by[k] = pom.coord
    if root:
        facts.root_properties[root] = sorted(raw)
    resolve = make_resolver(raw, pom_builtins(chain[0]))
    resolved = {k: (resolve(v) or v) for k, v in raw.items()}
    for k, v in resolved.items():
        if k not in facts.properties:                           # several roots: the first one wins
            facts.properties[k] = v
            facts.property_sources[k] = defined_by[k]
        # 회사 pom이 정의한 속성만 ${…}로 참조할 수 있으므로 Boot BOM의 lombok.version 같은 속성은 제외한다
        if k not in facts.company_properties and is_company_group(defined_by[k].split(":", 1)[0], company or []):
            facts.company_properties[k] = v
            facts.company_property_sources[k] = defined_by[k]
    reached = any(f"{p.group_id}:{p.artifact_id}" in BOOT_CHAIN_KEYS for p in chain)
    imports: list[tuple[str, str, str]] = []
    managed: dict[str, str] = {}
    for pom in chain:                                           # child first: the nearest declaration wins
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
    # Maven 규칙대로 직접 선언한 관리 항목이 import BOM보다 우선하므로 import에서는 아직 없는 g:a만 더한다
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
    # the extensions' `${…}` resolve against the whole chain's properties (a version a parent of the declaring pom defines — the
    # declaring pom alone left `${wagon.version}` unresolved and the pre-check called it "coordinates not resolvable")
    resolve_all = make_resolver(facts.properties)
    for eg, ea, ev, coord in facts.extension_decls:
        ext = ":".join(resolve_all(x) or x or "?" for x in (eg, ea, ev))
        if ext not in facts.build_extensions:
            facts.build_extensions.append(ext)
            facts.build_extension_sources[ext] = coord
    facts.reaches_boot_bom = reached if read_any else None
    manages_boot = bool(getattr(getattr(cfg, "artifacts", None), "parent_manages_spring_boot", False))
    if manages_boot and not reached:
        # 부모가 Boot를 관리한다고 했는데 체인이 Boot BOM까지 이어지지 않으면 Boot BOM을 가장 낮은
        # 우선순위로 더한다. 테스트에서 교체할 수 있도록 모듈로 참조한다
        from sbmig.build import boot_bom
        bb = boot_bom.load_boot_bom_facts(cfg)
        if not bb.empty:
            facts.boot_bom_added = True
            facts.chain_managed = dict(facts.managed)          # v2.1s: what the chain itself manages
            for k, v in bb.managed.items():
                facts.managed.setdefault(k, v)
            # 보충한 Boot BOM은 import와 같아서 속성이 상속되지 않으므로 properties에는 넣지 않는다
        facts.sources += [f"Boot BOM add-on (the chain does not reach {BOOT_BOM}): {s}" for s in bb.sources]
    return facts


def load_parent_chain_facts(cfg: Any) -> ParentChainFacts:
    """설정한 사내 부모 체인을 읽어 반환한다. 성공한 결과는 프로세스 안에서 캐시하고, 예외를 던지지 않는다.

    The configured in-house parent chain's facts (see the module docstring). Cached per (roots, local repository,
    `parent_manages_spring_boot`, `target.spring_boot_version`) for the process when something could be read; a failed
    load is retried on the next call. Never raises."""
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
    except Exception as e:  # facts are an aid, never a failed restructure (logged with the traceback)
        log.debug("parent chain facts failed: %s: %s", type(e).__name__, e, exc_info=True)
        return ParentChainFacts(sources=[f"failed: {type(e).__name__}: {e}"[:200]])


__all__ = ["BOOT_CHAIN_KEYS", "MAX_POMS", "ParentChainFacts", "clear_cache", "is_company_group",
           "load_parent_chain_facts"]
