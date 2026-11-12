"""프로젝트가 쓰는 사내 부모 pom의 as-is 버전이 저장소에 없을 때 이를 처리하는 모듈.
프로젝트가 이미 사내 부모(``artifacts.parent`` / ``boot_parent`` / ``comp_parent``의 g:a 또는 별칭)를 쓰는데
그 버전이 Nexus에 없어도, 설정된 버전을 받을 수 있으면 실행을 멈추지 않게 한다.
변환이 as-is pom에서 실패했을 때(``Orchestrator._step_rewrite``)와 수정 루프의 사전 시도
``sbmig-parent``에서 쓴다.
- ``classify_parent_misses``: Maven 메시지에서 찾지 못한 부모마다 configured / asis-version / foreign으로 분류한다
- ``align_asis_parent``: 그 부모를 쓰는 pom의 ``<parent>``를 설정 버전으로 바꾼다.
  PomEditor로 서식을 유지한다
- 설정 버전 자체를 찾지 못한 경우(configured)는 그대로 중단한다. 버전은 추정하지 않고 설정 값만 쓴다
- 예외를 던지지 않는다

An as-is version of the company parent that the repository does not have (v2.1z-ag, docs/INTERFACES.md). A project that
already inherits a configured in-house parent g:a (or an alias) at another version the repository lacks used to abort the
run as "configured parent not resolvable" although the configured version resolves. `classify_parent_misses` tells the
three kinds apart from Maven's text; `align_asis_parent` sets the configured version in every pom whose `<parent>` is that
g:a at that version (text-preserving `PomEditor` edits). A `${prop}` parent version defined in a project pom and used
for nothing but parent versions is aligned at the property; otherwise (defined outside the project, or used elsewhere
too) the `<version>` literal is written. Never raises."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from sbmig.core.models import GAV

log = logging.getLogger("sbmig.restructure.asis_parent")

MissKind = Literal["configured", "asis-version", "foreign"]
_PROP_REF_RE = re.compile(r"^\$\{([^}]+)\}$")
SPEC_NAMES = ("parent", "boot_parent", "comp_parent")


@dataclass
class ParentMiss:
    """Maven이 찾지 못한 부모 pom 하나와 그 종류를 담는 데이터 클래스.

    One parent Maven could not resolve: `gav` as Maven (or the poms on disk) named it, `kind`, and for a company parent
    the configured coordinates (`target`) and the config key that holds them (`spec_name`, e.g. `artifacts.parent`)."""
    gav: GAV
    kind: MissKind
    target: GAV | None = None
    spec_name: str | None = None

    @property
    def coordinates(self) -> str:
        """부모 좌표를 ``g:a:v``로 반환한다. 버전을 모르면 ``g:a``를 반환한다."""
        return self.gav.coordinates


@dataclass
class AlignResult:
    """``align_asis_parent``의 결과를 담는 데이터 클래스.
    바꾼 pom 목록과, 속성 값을 바꿔 맞춘 pom(``{pom: 속성 이름}``)을 담는다.

    The poms changed and the ones aligned at a property (`{pom: property}`)."""
    poms: list[str] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)


def _specs(cfg: Any) -> list[tuple[str, Any]]:
    a = cfg.artifacts
    return [(f"artifacts.{n}", getattr(a, n, None)) for n in SPEC_NAMES if getattr(a, n, None) is not None]


def matching_spec(cfg: Any, key: str) -> tuple[str | None, Any]:
    """g:a 키에 맞는 첫 사내 부모 설정을 (이름, spec)으로 반환한다.
    루트 부모를 먼저 보고, 없으면 (None, None)을 반환한다.

    The first configured parent whose key or alias is `key` (root parent first), with its config name."""
    for name, s in _specs(cfg):
        if key == s.key or key in (s.aliases or []):
            return name, s
    return None, None


def _tree_properties(ws: Any) -> dict[str, dict[str, str]]:
    from .pom_editor import PomEditor
    out: dict[str, dict[str, str]] = {}
    for rel in ws.pom_files():
        try:
            out[rel] = PomEditor(ws.root / rel).properties()
        except Exception as e:  # noqa: BLE001 - a broken pom is just not a source of facts
            log.debug("properties of %s unreadable: %s", rel, e)
    return out


def declared_parents(ws: Any) -> list[tuple[str, GAV, str | None]]:
    """트리의 pom마다 (경로, 버전을 해석한 <parent> 좌표, 버전이 ``${속성}``이면 그 속성 이름)을 반환한다.
    부모가 없는 pom은 뺀다.

    (pom, its `<parent>` with the version resolved against the pom's own properties then any project pom's, the property
    name when the version is `${prop}`) for every pom that declares a parent."""
    from .pom_editor import PomEditor
    props = _tree_properties(ws)
    out: list[tuple[str, GAV, str | None]] = []
    for rel in ws.pom_files():
        try:
            par = PomEditor(ws.root / rel).get_parent()
        except Exception as e:  # noqa: BLE001
            log.debug("parent of %s unreadable: %s", rel, e)
            continue
        if par is None or not par.group_id or not par.artifact_id:
            continue
        raw = (par.version or "").strip()
        m = _PROP_REF_RE.match(raw)
        prop = m.group(1) if m else None
        version = raw or None
        if prop:
            own = props.get(rel, {})
            version = own.get(prop) or next((p[prop] for p in props.values() if prop in p), None) or raw
        out.append((rel, GAV(group_id=par.group_id, artifact_id=par.artifact_id, version=version), prop))
    return out


def classify_gav(gav: GAV, cfg: Any, ws: Any = None) -> ParentMiss:
    """찾지 못한 부모 하나가 어떤 종류인지 정한다.
    - configured: 사내 부모 g:a이고 버전이 설정 버전과 같다(버전을 모르면 디스크의 pom이 설정 버전을 선언한 경우)
    - asis-version: 사내 부모 g:a나 별칭인데 버전이 다르다. foreign: 사내 부모가 아니다
    설정 버전이 비어 있으면 비교할 수 없으므로 configured로 보고 중단한다.

    configured = the configured coordinates themselves are missing (abort); asis-version = a company parent g:a (or an
    alias) at another version; foreign = not a company parent. A configured parent without a version is `configured`."""
    name, spec = matching_spec(cfg, gav.key)
    if spec is None:
        return ParentMiss(gav=gav, kind="foreign")
    target = GAV(group_id=spec.group_id, artifact_id=spec.artifact_id, version=spec.version)
    if not spec.version:
        return ParentMiss(gav=gav, kind="configured", target=target, spec_name=name)
    configured = {s.version for _n, s in _specs(cfg) if s.version and gav.key in (s.key, *(s.aliases or []))}
    alias = gav.key != spec.key
    version = gav.version
    if not version and ws is not None:
        on_disk = [g.version for _p, g, _prop in declared_parents(ws) if g.key == gav.key and g.version]
        other = [v for v in on_disk if alias or v not in configured]
        if other and not (set(on_disk) & configured and not alias):
            version = other[0]
    if not version or (not alias and version in configured):
        return ParentMiss(gav=GAV(group_id=gav.group_id, artifact_id=gav.artifact_id, version=version or spec.version),
                          kind="configured", target=target, spec_name=name)
    return ParentMiss(gav=GAV(group_id=gav.group_id, artifact_id=gav.artifact_id, version=version),
                      kind="asis-version", target=target, spec_name=name)


def classify_parent_misses(text: str, cfg: Any, ws: Any = None) -> list[ParentMiss]:
    """Maven 메시지(OpenRewrite 실패, 빌드 로그)에서 찾지 못한 부모마다 ``ParentMiss``를 만든다.
    없으면 빈 목록을 반환하며, 예외를 던지지 않는다.

    One `ParentMiss` per unresolvable parent named in Maven's text (`sbmig.build.log_parser.parent_coordinates_in`)."""
    try:
        from sbmig.build.log_parser import parent_coordinates_in
        return [classify_gav(g, cfg, ws) for g in parent_coordinates_in(text or "")]
    except Exception as e:  # noqa: BLE001
        log.debug("parent miss classification failed: %s: %s", type(e).__name__, e)
        return []


def align_asis_parent(ws: Any, miss: ParentMiss) -> AlignResult:
    """``miss``(asis-version)의 부모를 쓰는 pom을 모두 설정된 부모로 맞춘다.
    g:a가 같으면 ``<version>``만, 별칭이면 groupId, artifactId, version을 모두 바꾼다.
    버전이 ``${속성}``이고 그 속성이 프로젝트 pom에 정의돼 부모 버전에만 쓰이면 속성 값을 바꾸고, 아니면
    ``<version>`` 값을 직접 바꾼다.
    예외를 던지지 않으며, 고치지 못한 pom은 DEBUG 로그로 남긴다.

    Align every pom whose `<parent>` is `miss.gav` (g:a at that version) to `miss.target`: the version only for the same
    g:a, all three coordinates for an alias (relativePath untouched). A `${prop}` version defined in a project pom and
    referenced only by parent versions is aligned at the property; else the literal is written. Never raises."""
    from .pom_editor import PomEditor
    out = AlignResult()
    if miss.kind != "asis-version" or miss.target is None or not miss.target.version:
        return out
    old, new = miss.gav.version, miss.target.version
    alias = miss.gav.key != miss.target.key
    try:
        # an unresolved `${prop}` (defined outside the poms: .mvn/maven.config, -D) is the version Maven named
        hits = [(rel, prop) for rel, g, prop in declared_parents(ws)
                if g.key == miss.gav.key and (g.version == old or (prop and g.version == "${" + prop + "}"))]
        if not hits:
            return out
        texts = {}
        for rel in ws.pom_files():
            try:
                texts[rel] = ws.read(rel)
            except (OSError, UnicodeDecodeError):
                continue
        props = _tree_properties(ws)
        at_property: set[str] = set()
        for prop in {p for _r, p in hits if p}:
            refs = sum(t.count("${" + prop + "}") for t in texts.values())
            parent_refs = sum(1 for _r, p in hits if p == prop)
            definers = [r for r, ps in props.items() if ps.get(prop) == old]
            if definers and refs == parent_refs and not alias:
                at_property.add(prop)
                for rel in definers:
                    ed = PomEditor(ws.root / rel)
                    if ed.set_property(prop, new):
                        ed.save()
                        out.poms.append(rel)
                        out.properties[rel] = prop
        for rel, prop in hits:
            if prop and prop in at_property:
                continue
            ed = PomEditor(ws.root / rel)
            changed = False
            if alias:
                p = ed.child(ed.root, "parent")
                for name, value in (("groupId", miss.target.group_id), ("artifactId", miss.target.artifact_id),
                                    ("version", new)):
                    before = ed.text_of(p, name)
                    if before != value:
                        ed.set_child_text(p, name, value)
                        changed = True
            else:
                changed = ed.set_parent_version(new)
            if changed:
                ed.save()
                if rel not in out.poms:
                    out.poms.append(rel)
    except Exception as e:  # noqa: BLE001 - an alignment that fails leaves the old path (note + continue / abort)
        log.warning("as-is parent alignment %s → %s failed: %s: %s", miss.coordinates, new, type(e).__name__, e)
    return out


def aligned_followup(miss: ParentMiss, poms: list[str]) -> str:
    """변환 단계에서 as-is 부모 버전을 맞췄다는 인수인계 문구를 만든다.
    레시피가 목표 부모의 classpath로 실행됐다는 사실도 담는다.

    The Handover line of an alignment before the recipes (they ran on the target parent's classpath)."""
    target = miss.target.coordinates if miss.target else "?"
    return (f"as-is parent `{miss.coordinates}` is not in the repository: sbmig set the configured `{target}` "
            f"({miss.spec_name}) in {', '.join(poms)} before the recipes — the recipes ran on the target parent's classpath, "
            "not the as-is one (code that only compiled against the old parent was fixed by the build loop)")


def loop_aligned_followup(miss: ParentMiss, poms: list[str]) -> str:
    """수정 루프의 ``sbmig-parent``가 as-is 부모 버전을 맞췄다는 인수인계 문구를 만든다.

    The Handover line of the fix loop's `sbmig-parent` alignment."""
    target = miss.target.coordinates if miss.target else "?"
    return (f"as-is parent `{miss.coordinates}` is not in the repository: sbmig set the configured `{target}` "
            f"({miss.spec_name}) in {', '.join(poms)} (fix loop, sbmig-parent)")


def skipped_followup(miss: ParentMiss) -> str:
    """as-is 부모가 저장소에 없어 레시피가 실행되지 않았다는 인수인계 문구를 만든다.

    The Handover line of a rewrite that did not run because the as-is parent is not in the repository."""
    return (f"OpenRewrite did not run: the as-is parent `{miss.coordinates}` is not in the repository — no recipe changed "
            "the code; the restructure replaced the parent and the fix loop did the rest (check the repository / settings "
            "if that parent should be there)")
