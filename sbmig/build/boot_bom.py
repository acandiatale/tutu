"""Maven 없이 ``spring-boot-dependencies:<대상 Boot 버전>`` pom을 읽어 관리 버전과 속성을 구하는 모듈.
재구성이 effective pom을 얻지 못했을 때 대신 쓰고, 부모의 속성을 덮어쓰는 as-is 속성을 찾을 때도 쓴다.
- 진입점: ``load_boot_bom_facts(cfg, version=None)``, ``BootBomFacts.as_capture()``
- 로컬 저장소를 먼저 보고, 없으면 ``RepositoryLookup(cfg).pom_text``로 원격에서 읽는다
- import BOM은 한 단계만 따라가고, pom은 최대 ``MAX_POMS``개까지 읽는다
- 예외를 던지지 않는다. 읽지 못하면 빈 결과를 반환하고 ``sources``에 이유를 남긴다

Spring Boot BOM facts without Maven (v2.1o, docs/INTERFACES.md "v2.1o (2)"): the managed versions and the properties
of `org.springframework.boot:spring-boot-dependencies:<version>` (default `cfg.target.spring_boot_version`), read from
the local repository (`sbmig.build.mvn.local_repo_path`) or, failing that, through `RepositoryLookup(cfg).pom_text`
(the configured repository / mirror / Central, `build.repository_timeout_seconds` per fetch). The BOM's own `${…}`
are resolved from its properties; its `<scope>import</scope>` BOMs (spring-framework-bom, spring-security-bom,
spring-data-bom, jackson-bom, … ~46) are read the same way ONE level deep, their `${…}` resolved from their own
properties plus the Boot BOM's, so their managed keys count too (the Boot BOM's own entries win, then the imports in
declaration order — Maven's rule). At most `MAX_POMS` poms per load. Successful loads are cached per process (one
entry per version and local repository); never raises — `BootBomFacts.empty` is True when nothing could be read and
`sources` says why. `as_capture()` gives the `sbmig.build.effective.load_effective` shape."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from lxml import etree
from pydantic import BaseModel, Field

log = logging.getLogger("sbmig.build.boot_bom")

BOOT_GROUP = "org.springframework.boot"
BOOT_BOM = "spring-boot-dependencies"
MAX_POMS = 60                      # the Boot BOM + its imports (4.1.0: 46) — a bound on fetches per load
_PROP_RE = re.compile(r"\$\{([^}]+)\}")

_CACHE: dict[tuple[str, str], BootBomFacts] = {}


class BootBomFacts(BaseModel):
    """Boot BOM 한 버전에서 읽은 정보를 담는 데이터 클래스. ``managed``는 import BOM까지 포함한 관리 버전,
    ``properties``는 치환한 BOM 속성, ``sources``는 pom별 읽기 결과다.

    The facts of one Boot BOM version: `managed` (g:a → version, the imported BOMs' keys included), `properties`
    (the Boot BOM's own, resolved), `sources` (one line per pom read or not readable)."""
    version: str = ""
    managed: dict[str, str] = Field(default_factory=dict)
    properties: dict[str, str] = Field(default_factory=dict)
    sources: list[str] = Field(default_factory=list)

    @property
    def empty(self) -> bool:
        """관리 버전과 속성을 하나도 읽지 못했는지 반환한다."""
        return not self.managed and not self.properties

    def as_capture(self) -> dict[str, Any]:
        """``load_effective``와 같은 형태의 dict로 바꿔 반환한다.
        재구성 뒤 effective pom 대신 그대로 쓸 수 있다.

        The `sbmig.build.effective.load_effective` shape, so the restructure can take it in place of a capture."""
        return {"managed": dict(self.managed), "direct": {}, "plugins": {}, "properties": dict(self.properties),
                "parent": None, "modules": [], "by_module": {}}


def clear_cache() -> None:
    """프로세스 안의 캐시를 비운다. 테스트에서 저장소를 바꾼 뒤 호출한다."""
    _CACHE.clear()


# ---------------------------------------------------------------------------------------------------------------------
# pom parsing — shared with sbmig.build.parent_chain (v2.1p)
# ---------------------------------------------------------------------------------------------------------------------
def _local(tag: Any) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _child(el: Any, name: str) -> Any:
    if el is None:
        return None
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el: Any, name: str) -> str:
    c = _child(el, name)
    return (c.text or "").strip() if c is not None and c.text else ""


class PomSummary(BaseModel):
    """정보 계산에 쓰는 pom 하나의 값을 담는 클래스.
    좌표, 치환 전 ``<parent>`` 좌표, 속성, dependencyManagement 항목을 담는다.

    What the BOM / parent-chain facts need from one pom: its coordinates, its `<parent>` coordinates (raw), its
    `<properties>` and its `<dependencyManagement>` entries (groupId, artifactId, version, type, scope)."""
    group_id: str = ""
    artifact_id: str = ""
    version: str = ""
    parent_group_id: str = ""
    parent_artifact_id: str = ""
    parent_version: str = ""
    properties: dict[str, str] = Field(default_factory=dict)
    managed: list[tuple[str, str, str, str, str]] = Field(default_factory=list)
    # <build><extensions>는 상속되어 부모 체인의 확장 하나가 모든 모듈의 빌드를 막을 수 있으므로 원래 좌표를 남긴다
    extensions: list[tuple[str, str, str]] = Field(default_factory=list)

    @property
    def coord(self) -> str:
        """보고에 쓸 ``g:a:v`` 문자열을 반환한다."""
        return f"{self.group_id}:{self.artifact_id}:{self.version}"


def parse_pom(text: str) -> PomSummary | None:
    """pom 텍스트를 ``PomSummary``로 파싱한다.
    ``<project>`` 문서가 아니면 None을 반환하고, 예외를 던지지 않는다.

    Parse a pom (comments ignored, no network / entities); None when it is not a `<project>`."""
    try:
        parser = etree.XMLParser(recover=True, remove_comments=True, resolve_entities=False, no_network=True,
                                 huge_tree=True)
        root = etree.fromstring(text.encode("utf-8") if isinstance(text, str) else text, parser)
    except (etree.XMLSyntaxError, ValueError):
        return None
    if root is None or _local(root.tag) != "project":
        return None
    parent = _child(root, "parent")
    pom = PomSummary(group_id=_text(root, "groupId") or _text(parent, "groupId"),
                     artifact_id=_text(root, "artifactId"), version=_text(root, "version") or _text(parent, "version"),
                     parent_group_id=_text(parent, "groupId"), parent_artifact_id=_text(parent, "artifactId"),
                     parent_version=_text(parent, "version"))
    props = _child(root, "properties")
    for p in (props if props is not None else []):
        if _local(p.tag):
            pom.properties[_local(p.tag)] = (p.text or "").strip()
    deps = _child(_child(root, "dependencyManagement"), "dependencies")
    for d in (deps if deps is not None else []):
        if _local(d.tag) != "dependency":
            continue
        pom.managed.append((_text(d, "groupId"), _text(d, "artifactId"), _text(d, "version"),
                            _text(d, "type") or "jar", _text(d, "scope")))
    exts = _child(_child(root, "build"), "extensions")
    for e in (exts if exts is not None else []):
        if _local(e.tag) == "extension":
            pom.extensions.append((_text(e, "groupId"), _text(e, "artifactId"), _text(e, "version")))
    return pom


def make_resolver(*layers: dict[str, str]):
    """``${…}``를 치환하는 함수를 만든다. 앞에 준 속성 묶음이 우선하고, 10단계 중첩까지 푼다.
    풀지 못한 값이 남으면 None을 반환한다.

    A `${…}` resolver over `layers` (the first layer wins), nested up to 10 levels; None when something stays."""
    merged: dict[str, str] = {}
    for layer in reversed(layers):
        merged.update(layer)

    def resolve(value: str) -> str | None:
        cur = (value or "").strip()
        for _ in range(10):
            if "${" not in cur:
                break
            cur = _PROP_RE.sub(lambda m: merged.get(m.group(1), m.group(0)), cur)
        return None if not cur or "${" in cur else cur
    return resolve


def pom_builtins(pom: PomSummary) -> dict[str, str]:
    """``${…}`` 치환에 쓸 pom 자신의 ``project.version`` / ``project.groupId`` / ``project.artifactId`` 값을 반환한다."""
    return {k: v for k, v in (("project.version", pom.version), ("project.groupId", pom.group_id),
                              ("project.artifactId", pom.artifact_id), ("version", pom.version)) if v}


# ---------------------------------------------------------------------------------------------------------------------
# reading poms
# ---------------------------------------------------------------------------------------------------------------------
class PomReader:
    """로컬 저장소, 원격 저장소 순으로 pom을 최대 ``limit``개까지 읽는 클래스.
    원격 조회 객체는 처음 필요할 때 만들고, 예외를 던지지 않는다.

    Reads poms from the local repository, else through `RepositoryLookup.pom_text` (made on first use, found as a
    module attribute so tests can swap it); at most `limit` poms per reader. Never raises."""

    def __init__(self, cfg: Any, local: Path | None, limit: int = MAX_POMS):
        self.cfg = cfg
        self.local = local
        self.limit = limit
        self.count = 0
        self._lookup: Any = None
        self._lookup_made = False

    def _repository(self) -> Any:
        if not self._lookup_made:
            self._lookup_made = True
            try:
                # 테스트가 RepositoryLookup을 오프라인 가짜 구현으로 바꾸므로 모듈 속성으로 찾는다
                from sbmig.build import repo_versions
                self._lookup = repo_versions.RepositoryLookup(self.cfg)
            except Exception as e:  # noqa: BLE001 - no lookup = local repository only
                log.debug("pom facts: no repository lookup: %s: %s", type(e).__name__, e)
                self._lookup = None
        return self._lookup

    def pom(self, g: str, a: str, v: str) -> tuple[str | None, str]:
        """pom 하나를 읽어 (텍스트, 출처)를 반환한다. 읽지 못하면 (None, 이유)를 반환한다."""
        if self.count >= self.limit:
            return None, f"skipped (more than {self.limit} poms)"
        self.count += 1
        if self.local is not None:
            d = self.local.joinpath(*g.split("."), a, v)
            try:
                exact = d / f"{a}-{v}.pom"
                files = [exact] if exact.is_file() else sorted(d.glob("*.pom")) if d.is_dir() else []
                if files:
                    return files[0].read_text(encoding="utf-8", errors="replace"), str(files[0])
            except OSError as e:
                log.debug("pom facts: %s unreadable: %s", d, e)
        lookup = self._repository()
        if lookup is None:
            return None, "not in the local repository, no repository lookup"
        try:
            text = lookup.pom_text(g, a, v)
        except Exception as e:  # noqa: BLE001 - one unreadable pom never fails the load
            log.debug("pom facts: pom_text(%s:%s:%s): %s", g, a, v, e)
            text = None
        if text:
            # Maven이 settings 미러로 요청하는 URL과 다르면 sbmig는 읽어도 Maven은 404일 수 있으므로 응답한 URL도 남긴다
            source = getattr(lookup, "pom_source", None)
            src = source(g, a, v) if callable(source) else None
            return text, f"repository {src}" if src else "repository"
        return None, "not in the local repository and not readable from the repository"


def local_repository(cfg: Any) -> Path | None:
    """로컬 Maven 저장소 경로(``sbmig.build.mvn.local_repo_path``)를 반환한다. 알 수 없으면 None을 반환한다."""
    try:
        from .mvn import local_repo_path
        return local_repo_path(cfg)
    except Exception as e:  # noqa: BLE001 - no local repository = repository only
        log.debug("pom facts: local repository path: %s", e)
        return None


def read_import_boms(reader: PomReader, imports: list[tuple[str, str, str]], outer: dict[str, str],
                     managed: dict[str, str], sources: list[str]) -> None:
    """``<scope>import</scope>`` BOM을 한 단계만 읽어 관리 버전을 ``managed``에 더한다.
    이미 있는 g:a는 그대로 둔다.
    BOM 안의 ``${…}``는 BOM 자신의 속성을 먼저 보고, 그다음 ``outer``를 본다.

    Read each imported BOM (one level deep: its own imports are not followed) and add its managed entries to
    `managed` unless the key is there already; its `${…}` resolve from its own properties first, then `outer`. One
    `sources` line per BOM (read with the count, or why not)."""
    for g, a, v in imports:
        itext, iwhere = reader.pom(g, a, v)
        ipom = parse_pom(itext) if itext else None
        if ipom is None:
            sources.append(f"{g}:{a}:{v}: {iwhere if not itext else 'not a pom'}")
            continue
        iresolve = make_resolver(ipom.properties, pom_builtins(ipom), outer)
        n = 0
        for dg, da, dv, _typ, scope in ipom.managed:
            if scope == "import":
                continue                                       # one level deep only
            rg, ra, rv = iresolve(dg), iresolve(da), iresolve(dv)
            if rg and ra and rv and f"{rg}:{ra}" not in managed:
                managed[f"{rg}:{ra}"] = rv
                n += 1
        sources.append(f"{g}:{a}:{v}: {iwhere} ({n} managed)")


def _load(cfg: Any, version: str, local: Path | None) -> BootBomFacts:
    facts = BootBomFacts(version=version)
    reader = PomReader(cfg, local)
    coord = f"{BOOT_GROUP}:{BOOT_BOM}:{version}"
    text, where = reader.pom(BOOT_GROUP, BOOT_BOM, version)
    bom = parse_pom(text) if text else None
    if bom is None:
        facts.sources.append(f"{coord}: {where if not text else 'not a pom'}")
        return facts
    own = {**pom_builtins(bom), **bom.properties}
    resolve = make_resolver(bom.properties, pom_builtins(bom))
    facts.properties = {k: (resolve(v) or v) for k, v in bom.properties.items()}
    imports: list[tuple[str, str, str]] = []
    unresolved = 0
    for g, a, v, typ, scope in bom.managed:
        rg, ra, rv = resolve(g), resolve(a), resolve(v)
        if not (rg and ra and rv):
            unresolved += 1
            continue
        if scope == "import" and typ == "pom":
            imports.append((rg, ra, rv))
            continue
        facts.managed.setdefault(f"{rg}:{ra}", rv)            # the BOM's own entries win over the imports
    facts.sources.append(f"{coord}: {where} ({len(facts.managed)} managed, {len(bom.properties)} properties, "
                         f"{len(imports)} imported BOMs" + (f", {unresolved} unresolved" if unresolved else "") + ")")
    # import BOM의 ${…}는 그 BOM의 속성을 먼저 보고, 그다음 Boot BOM 속성으로 푼다
    read_import_boms(reader, imports, {**own, **facts.properties}, facts.managed, facts.sources)
    return facts


def load_boot_bom_facts(cfg: Any, version: str | None = None) -> BootBomFacts:
    """``spring-boot-dependencies:<version>`` pom에서 관리 버전과 속성을 읽는다.
    성공한 결과만 프로세스 안에 캐시하며, 예외를 던지지 않는다.

    The facts of the Boot BOM `version` (default `cfg.target.spring_boot_version`). Successful loads are cached per
    (version, local repository) for the process; a failed one is retried on the next call. Never raises."""
    v = (version or str(getattr(getattr(cfg, "target", None), "spring_boot_version", "") or "")).strip()
    if not v:
        return BootBomFacts(sources=["no Spring Boot version configured (target.spring_boot_version)"])
    try:
        local = local_repository(cfg)
        key = (v, str(local))
        hit = _CACHE.get(key)
        if hit is not None:
            return hit
        facts = _load(cfg, v, local)
        if not facts.empty:
            _CACHE[key] = facts
            log.info("Boot BOM facts %s: %d managed artifact(s), %d propert(ies) from %d pom(s)", v,
                     len(facts.managed), len(facts.properties), len(facts.sources))
        else:
            log.info("Boot BOM facts %s unavailable: %s", v, "; ".join(facts.sources) or "-")
        return facts
    except Exception as e:  # facts are an aid, never a failed restructure (logged with the traceback)
        log.debug("Boot BOM facts %s failed: %s: %s", v, type(e).__name__, e, exc_info=True)
        return BootBomFacts(version=v, sources=[f"failed: {type(e).__name__}: {e}"[:200]])


__all__ = ["BOOT_BOM", "BOOT_GROUP", "MAX_POMS", "BootBomFacts", "PomReader", "PomSummary", "clear_cache",
           "load_boot_bom_facts", "local_repository", "make_resolver", "parse_pom", "pom_builtins",
           "read_import_boms"]
