"""사내 부모 pom의 as-is 버전이 저장소에 없을 때 설정된 버전으로 맞추는 모듈.
프로젝트가 이미 사내 부모 g:a나 별칭을 쓰는데 그 버전이 Nexus에 없어도, 설정 버전을 받을 수 있으면 계속 진행한다.
as-is pom에서 변환이 실패했을 때와 수정 루프의 사전 시도 ``sbmig-parent``에서 호출된다.
- ``classify_parent_misses``: 찾지 못한 부모를 configured, asis-version, foreign으로 나눈다
- ``align_asis_parent``: 그 부모를 쓰는 pom의 ``<parent>``를 설정 버전으로 바꾼다
- 설정 버전 자체가 없으면(configured) 중단한다. 버전은 추정하지 않고 설정 값만 쓴다
- 예외를 던지지 않는다

Contract: docs/INTERFACES.md "v2.1z-ag". Config keys: `artifacts.parent` / `boot_parent` / `comp_parent`.
Caller of the rewrite path: `Orchestrator._step_rewrite`. Edits go through `PomEditor` (formatting kept).
A `${prop}` parent version defined in a project pom and used for nothing but parent versions is aligned at the
property; otherwise (defined outside the project, or used elsewhere too) the `<version>` literal is
written."""
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
    """Maven이 찾지 못한 부모 pom 하나와 그 종류를 담는 클래스.

    `gav` as Maven (or the poms on disk) named it. For a company parent: `target` = the configured
    coordinates, `spec_name` = the config key that holds them (e.g. `artifacts.parent`)."""
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
    """``align_asis_parent``의 결과를 담는 클래스.
    바꾼 pom 목록과, 속성 값을 바꿔 맞춘 pom의 ``{pom: 속성 이름}``을 담는다.
    """
    poms: list[str] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)


def _specs(cfg: Any) -> list[tuple[str, Any]]:
    a = cfg.artifacts
    return [(f"artifacts.{n}", getattr(a, n, None)) for n in SPEC_NAMES if getattr(a, n, None) is not None]


def matching_spec(cfg: Any, key: str) -> tuple[str | None, Any]:
    """g:a나 별칭이 ``key``인 첫 사내 부모 설정을 (설정 이름, spec)으로 반환한다.
    루트 부모를 먼저 본다. 없으면 (None, None)을 반환한다."""
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
        except Exception as e:  # noqa: BLE001 - 깨진 pom은 정보에서 뺄 뿐이다
            log.debug("properties of %s unreadable: %s", rel, e)
    return out


def declared_parents(ws: Any) -> list[tuple[str, GAV, str | None]]:
    """부모를 선언한 pom마다 (경로, ``<parent>`` 좌표, 버전 속성 이름)을 반환한다.
    버전이 ``${속성}``이면 그 pom의 속성, 다른 프로젝트 pom의 속성 순으로 값을 찾는다.
    속성이 아니면 속성 이름은 None이다."""
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
    - configured: 사내 부모 g:a이고 버전이 설정 버전과 같다. 설정 좌표 자체가 없으니 중단한다
    - asis-version: 사내 부모 g:a나 별칭인데 버전이 다르다
    - foreign: 사내 부모가 아니다
    설정 버전이 비어 있으면 비교할 수 없으므로 configured로 본다.

    When Maven's text names no version, the versions the poms on disk declare decide."""
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
    """Maven 출력에서 찾지 못한 부모마다 ``ParentMiss``를 만들어 반환한다.
    없으면 빈 목록을 반환한다. 예외를 던지지 않는다.

    Parent names come from `sbmig.build.log_parser.parent_coordinates_in`."""
    try:
        from sbmig.build.log_parser import parent_coordinates_in
        return [classify_gav(g, cfg, ws) for g in parent_coordinates_in(text or "")]
    except Exception as e:  # noqa: BLE001
        log.debug("parent miss classification failed: %s: %s", type(e).__name__, e)
        return []


def align_asis_parent(ws: Any, miss: ParentMiss) -> AlignResult:
    """asis-version인 ``miss``의 부모를 쓰는 pom을 모두 설정된 부모로 맞춘다.
    g:a가 같으면 ``<version>``만 바꾸고, 별칭이면 groupId, artifactId, version을 모두 바꾼다.
    버전 속성이 프로젝트 pom에 정의돼 부모 버전에만 쓰이면 속성 값을 바꾸고, 아니면 ``<version>``을 바꾼다.
    예외를 던지지 않는다. 실패하면 WARNING을 남기고 그때까지의 결과를 반환한다.

    relativePath is left untouched."""
    from .pom_editor import PomEditor
    out = AlignResult()
    if miss.kind != "asis-version" or miss.target is None or not miss.target.version:
        return out
    old, new = miss.gav.version, miss.target.version
    alias = miss.gav.key != miss.target.key
    try:
        # pom 밖(.mvn/maven.config, -D)에서 정의돼 풀리지 않은 ${prop}도 Maven이 말한 버전으로 본다
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
    except Exception as e:  # noqa: BLE001 - 맞추지 못하면 호출하는 쪽이 원래 경로(메모 후 계속 또는 중단)로 간다
        log.warning("as-is parent alignment %s → %s failed: %s: %s", miss.coordinates, new, type(e).__name__, e)
    return out


def aligned_followup(miss: ParentMiss, poms: list[str]) -> str:
    """레시피 실행 전에 as-is 부모 버전을 맞췄다는 인수인계 문장을 만든다.
    레시피가 목표 부모의 classpath로 실행됐다는 내용도 담는다."""
    target = miss.target.coordinates if miss.target else "?"
    return (f"as-is parent `{miss.coordinates}` is not in the repository: sbmig set the configured `{target}` "
            f"({miss.spec_name}) in {', '.join(poms)} before the recipes — the recipes ran on the target parent's classpath, "
            "not the as-is one (code that only compiled against the old parent was fixed by the build loop)")


def loop_aligned_followup(miss: ParentMiss, poms: list[str]) -> str:
    """수정 루프의 ``sbmig-parent``가 as-is 부모 버전을 맞췄다는 인수인계 문장을 만든다."""
    target = miss.target.coordinates if miss.target else "?"
    return (f"as-is parent `{miss.coordinates}` is not in the repository: sbmig set the configured `{target}` "
            f"({miss.spec_name}) in {', '.join(poms)} (fix loop, sbmig-parent)")


def skipped_followup(miss: ParentMiss) -> str:
    """as-is 부모가 저장소에 없어 레시피가 실행되지 않았다는 인수인계 문장을 만든다."""
    return (f"OpenRewrite did not run: the as-is parent `{miss.coordinates}` is not in the repository — no recipe changed "
            "the code; the restructure replaced the parent and the fix loop did the rest (check the repository / settings "
            "if that parent should be there)")
