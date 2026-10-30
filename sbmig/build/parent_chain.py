"""사내 부모 체인의 사실(v2.1p) — 설정한 사내 부모(``artifacts.parent`` / ``boot_parent`` / ``comp_parent``)의 pom 을
저장소에서 읽어 ``<parent>`` 를 위로 따라가며 속성(이름 → 값, 자식이 이김)과 관리 버전(g:a → 버전, 먼저 정의한 쪽이 이김, import
BOM 포함)을 낸다. 흐름상 위치: 재구성의 관리 버전 정리가 재구성 뒤 effective pom 캡처를 얻지 못했을 때의 대체 출처, 그리고
``_drop_bom_property_overrides`` 의 '부모가 정의한 속성 이름' 목록(부모 버전이 올라가 속성이 늘어도 고정 목록 없이 따라간다).
핵심 진입점: ``load_parent_chain_facts(cfg) -> ParentChainFacts``, ``ParentChainFacts.as_capture()``. 지킬 것: pom 읽기·
``${…}`` 풀이·import BOM 읽기는 ``boot_bom.py`` 의 공유 함수를 쓴다; 최대 ``MAX_POMS`` 개; 프로세스 안 캐시(성공한 것만);
예외를 내지 않는다(못 읽은 pom 은 ``unreadable`` / ``sources`` 에).

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
    """사내 부모 체인의 사실: ``roots`` (읽기 시작한 부모 g:a:v), ``managed`` (g:a → 버전), ``properties`` (상속되는 속성,
    값은 푼 것), ``property_sources`` (속성 이름 → 그것을 정의한 가장 가까운 pom g:a:v), ``sources`` (읽은 / 못 읽은 pom
    마다 한 줄), ``unreadable`` (못 읽은 pom g:a:v), ``poms`` (읽은 pom 수), ``boot_bom_added`` (Boot BOM 을 보탬).

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

    @property
    def empty(self) -> bool:
        """관리 버전도 속성도 없는가(아무 pom 도 못 읽음)."""
        return not self.managed and not self.properties

    def as_capture(self) -> dict[str, Any]:
        """``load_effective`` 모양 — 재구성 뒤 effective pom 캡처 자리에 넣을 수 있게.

        The `sbmig.build.effective.load_effective` shape (`by_module` empty)."""
        return {"managed": dict(self.managed), "direct": {}, "plugins": {}, "properties": dict(self.properties),
                "parent": self.roots[0] if self.roots else None, "modules": [], "by_module": {}}


def clear_cache() -> None:
    """프로세스 캐시를 비운다(테스트용)."""
    _CACHE.clear()


def _roots(cfg: Any) -> list[tuple[str, str, str]]:
    """설정한 사내 부모(parent → boot_parent → comp_parent)의 (g, a, v), 중복·버전 없는 것 제외."""
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
    """루트 부모에서 ``<parent>`` 를 따라 올라간 pom 들(자식 먼저). 못 읽으면 거기서 멈추고 ``unreadable`` 에."""
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
        if not (pom.parent_group_id and pom.parent_artifact_id and pom.parent_version):
            break
        # <parent> 좌표의 ${…} 는 자식 pom 의 속성으로(저장소 좌표만 — relativePath 는 보지 않는다)
        resolve = make_resolver(pom.properties, pom_builtins(pom))
        pg, pa, pv = resolve(pom.parent_group_id), resolve(pom.parent_artifact_id), resolve(pom.parent_version)
        if not (pg and pa and pv):
            facts.sources.append(f"{coord}: parent {pom.parent_group_id}:{pom.parent_artifact_id}:"
                                 f"{pom.parent_version} not resolvable — chain stops here")
            break
        cur = (pg, pa, pv)
    return chain


def _merge_root(reader: PomReader, chain: list[PomSummary], facts: ParentChainFacts) -> bool:
    """루트 하나의 체인(자식 먼저)을 사실에 합친다. 체인이 Boot BOM 에 닿았는지(상속 또는 import) 돌려준다."""
    if not chain:
        return False
    raw: dict[str, str] = {}
    defined_by: dict[str, str] = {}
    for pom in reversed(chain):                                 # ancestor first: the child overwrites (child wins)
        for k, v in pom.properties.items():
            raw[k] = v
            defined_by[k] = pom.coord
    resolve = make_resolver(raw, pom_builtins(chain[0]))
    resolved = {k: (resolve(v) or v) for k, v in raw.items()}
    for k, v in resolved.items():
        if k not in facts.properties:                           # several roots: the first one wins
            facts.properties[k] = v
            facts.property_sources[k] = defined_by[k]
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
    # Maven: 선언된 관리 항목(상속 포함)이 import BOM 보다 먼저 — import 는 아직 없는 g:a 만 보탠다
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
    for root in roots:
        reached = _merge_root(reader, _walk(reader, root, facts), facts) or reached
    manages_boot = bool(getattr(getattr(cfg, "artifacts", None), "parent_manages_spring_boot", False))
    if manages_boot and not reached:
        # 부모가 Boot 를 관리한다고 했는데 체인이 Boot BOM 에 닿지 않는다(드문 사내 부모): v2.1o 처럼 Boot BOM 을 가장
        # 낮은 우선순위로 보탠다. 모듈 속성으로 불러 테스트가 바꿔 끼울 수 있게
        from sbmig.build import boot_bom
        bb = boot_bom.load_boot_bom_facts(cfg)
        if not bb.empty:
            facts.boot_bom_added = True
            src = f"{BOOT_GROUP}:{BOOT_BOM}:{bb.version}"
            for k, v in bb.managed.items():
                facts.managed.setdefault(k, v)
            for k, v in bb.properties.items():
                if k not in facts.properties:
                    facts.properties[k] = v
                    facts.property_sources[k] = src
        facts.sources += [f"Boot BOM add-on (the chain does not reach {BOOT_BOM}): {s}" for s in bb.sources]
    return facts


def load_parent_chain_facts(cfg: Any) -> ParentChainFacts:
    """설정한 사내 부모 체인의 사실을 읽는다(로컬 저장소 → 저장소, ``<parent>`` 위로, import BOM 한 단계, 필요하면 Boot
    BOM 을 보탬). 성공한 결과는 프로세스 안에서 캐시. 예외 없음.

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


__all__ = ["BOOT_CHAIN_KEYS", "MAX_POMS", "ParentChainFacts", "clear_cache", "load_parent_chain_facts"]
