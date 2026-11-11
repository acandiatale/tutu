"""저장소에 없는 사내 부모의 as-is 버전(v2.1z-ag) — 프로젝트가 이미 사내 부모(``artifacts.parent`` / ``boot_parent`` /
``comp_parent`` 의 g:a 또는 별칭)를 쓰는데 그 버전(예: 옛 도구가 남긴 ``1.0.0-SNAPSHOT``)이 Nexus 에 없을 때, 설정된
버전은 멀쩡한데도 "사내 부모를 못 찾는다"며 실행을 멈추던 것을 고친다. 흐름상 위치: 변환(OpenRewrite)이 as-is pom
위에서 실패했을 때(``Orchestrator._step_rewrite``)와 수정 루프의 LLM 0회 사전 시도 ``sbmig-parent``. 핵심 진입점:
``classify_parent_misses`` (Maven 문장 → 못 찾은 부모마다 configured / asis-version / foreign), ``align_asis_parent``
(그 g:a·버전을 부모로 둔 pom 전부의 ``<parent><version>`` 을 설정된 버전으로, PomEditor 로 — 서식 그대로). 지킬 것:
설정된 버전 자체를 못 찾으면 그대로 중단(configured), 버전은 추정하지 않는다(설정 값만 쓴다), 예외를 내지 않는다.

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
    """Maven 이 해석하지 못한 부모 하나와 그 종류.

    One parent Maven could not resolve: `gav` as Maven (or the poms on disk) named it, `kind`, and for a company parent
    the configured coordinates (`target`) and the config key that holds them (`spec_name`, e.g. `artifacts.parent`)."""
    gav: GAV
    kind: MissKind
    target: GAV | None = None
    spec_name: str | None = None

    @property
    def coordinates(self) -> str:
        """``g:a:v`` (버전을 모르면 ``g:a``)."""
        return self.gav.coordinates


@dataclass
class AlignResult:
    """``align_asis_parent`` 의 결과: 바꾼 pom 들, 속성으로 맞춘 것 ``{pom: 속성 이름}``.

    The poms changed and the ones aligned at a property (`{pom: property}`)."""
    poms: list[str] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)


def _specs(cfg: Any) -> list[tuple[str, Any]]:
    a = cfg.artifacts
    return [(f"artifacts.{n}", getattr(a, n, None)) for n in SPEC_NAMES if getattr(a, n, None) is not None]


def matching_spec(cfg: Any, key: str) -> tuple[str | None, Any]:
    """g:a 키가 가리키는 사내 부모 설정(이름, spec) — 처음 맞는 것(루트 부모 먼저). 없으면 (None, None).

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
    """트리의 pom 마다 (경로, <parent> 좌표(버전은 풀어 둔 값), 버전이 ``${속성}`` 이면 그 속성 이름). 부모 없는 pom 은 뺀다.

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
    """못 찾은 부모 하나의 종류. 사내 부모 g:a 이고 버전이 설정 버전과 같으면(또는 버전을 모르고 디스크의 pom 들이 설정
    버전을 선언하면) configured, 사내 부모 g:a(또는 별칭)인데 다른 버전이면 asis-version, 사내 부모가 아니면 foreign.
    설정 버전이 비어 있으면 비교할 수 없으니 configured(예전처럼 중단).

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
    """Maven 문장(OpenRewrite 실패, 빌드 로그 메시지)에서 못 찾은 부모마다 ``ParentMiss``. 없으면 []. 예외 없음.

    One `ParentMiss` per unresolvable parent named in Maven's text (`sbmig.build.log_parser.parent_coordinates_in`)."""
    try:
        from sbmig.build.log_parser import parent_coordinates_in
        return [classify_gav(g, cfg, ws) for g in parent_coordinates_in(text or "")]
    except Exception as e:  # noqa: BLE001
        log.debug("parent miss classification failed: %s: %s", type(e).__name__, e)
        return []


def align_asis_parent(ws: Any, miss: ParentMiss) -> AlignResult:
    """``miss`` (asis-version) 의 g:a·버전을 부모로 둔 pom 전부를 설정된 부모로 맞춘다: 같은 g:a 면 ``<version>`` 만, 별칭이면
    groupId·artifactId·version 셋 다(relativePath 그대로). ``${속성}`` 버전은 그 속성이 프로젝트 pom 에 정의돼 있고 부모
    버전 말고는 아무도 안 쓰면 속성 값을, 아니면 ``<version>`` 글자를 바꾼다. 예외 없음(못 고친 pom 은 DEBUG 로그).

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
    """변환 단계에서 as-is 부모 버전을 맞췄다는 인수인계 한 줄(레시피가 목표 부모의 클래스패스로 돌았다는 사실 포함).

    The Handover line of an alignment before the recipes (they ran on the target parent's classpath)."""
    target = miss.target.coordinates if miss.target else "?"
    return (f"as-is parent `{miss.coordinates}` is not in the repository: sbmig set the configured `{target}` "
            f"({miss.spec_name}) in {', '.join(poms)} before the recipes — the recipes ran on the target parent's classpath, "
            "not the as-is one (code that only compiled against the old parent was fixed by the build loop)")


def loop_aligned_followup(miss: ParentMiss, poms: list[str]) -> str:
    """수정 루프의 ``sbmig-parent`` 가 as-is 부모 버전을 맞췄다는 인수인계 한 줄.

    The Handover line of the fix loop's `sbmig-parent` alignment."""
    target = miss.target.coordinates if miss.target else "?"
    return (f"as-is parent `{miss.coordinates}` is not in the repository: sbmig set the configured `{target}` "
            f"({miss.spec_name}) in {', '.join(poms)} (fix loop, sbmig-parent)")


def skipped_followup(miss: ParentMiss) -> str:
    """as-is 부모가 저장소에 없어 레시피가 돌지 않았다는 인수인계 한 줄.

    The Handover line of a rewrite that did not run because the as-is parent is not in the repository."""
    return (f"OpenRewrite did not run: the as-is parent `{miss.coordinates}` is not in the repository — no recipe changed "
            "the code; the restructure replaced the parent and the fix loop did the rest (check the repository / settings "
            "if that parent should be there)")
