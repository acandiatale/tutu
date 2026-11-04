"""동봉 jar 를 재구성 단계에서 연결한다(v2.1z-j): 어느 pom 도 선언하지 않은 트리 안 jar(``profile.bundled_jars``)를 첫
빌드 전에 대상 모듈 pom 에 넣는다 — Ant 접수(v2.1j)가 하던 일과 같은 규칙. 컴파일 오류가 없는 런타임 전용 jar(JDBC
드라이버, web.xml 의 sitemesh 필터, taglib 구현)도 as-is WAR 처럼 패키징되게 하려는 것이다.

jar 마다 판정 하나: ``superseded`` (목표 부모 체인 / Boot BOM 이 그 g:a 를 관리 — 관리 버전이 대신, 손대지 않음),
``container`` (서블릿 / JSP API 사본 — Tomcat 이 준다), ``unused`` (소스 / JSP / XML / properties 어디서도 참조하지 않음 —
선언하지 않고 인수인계에 이름만), ``declared`` (대상 pom 이 이미 그 g:a 를 선언), ``compile`` (좌표를 알고 저장소에 있음 →
보통 의존성), ``system`` (좌표를 모르거나 저장소에 없음 → ``<module>/lib`` 복사 + system scope), ``skip``
(``intake.unknown_jar: skip`` 이라 저장소에 없는 jar 는 선언하지 않음, 또는 연결 중 오류). 판정(``plan_bundled_jars``)은
파일을 바꾸지 않고, 쓰기(``apply_bundled_jars``)는 ``system_jars.wire_bundled_jar`` 를 통한다.

Bundled jars wired at the restructure (v2.1z-j): every `profile.bundled_jars` entry gets one verdict
(`superseded | container | unused | declared | compile | system | skip`, with the reason) from the pure planner;
`apply_bundled_jars` writes the `compile` / `system` ones through `wire_bundled_jar` and never raises past a per-jar
WARNING (a failed jar becomes a `skip` item carrying the error)."""
from __future__ import annotations

import logging
import posixpath
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sbmig.core.models import JarCoordinate

from .pom_editor import PomEditError, PomEditor
from .system_jars import WiredJar, wire_bundled_jar

__all__ = ["TRUSTED_SOURCES", "VERDICTS", "BundledItem", "BundledPlan", "apply_bundled_jars", "jar_name", "plan_bundled_jars",
           "repository_has_fn", "unused_jar_followup"]

log = logging.getLogger(__name__)

VERDICTS = ("superseded", "container", "unused", "declared", "compile", "system", "skip")
# 저장소에 물어볼 만한 좌표의 출처(루프의 BUNDLED_TRUSTED_SOURCES 와 같다) — manifest 추정은 믿지 않는다
TRUSTED_SOURCES = ("pom_properties", "sha1_lookup", "name_table")


@dataclass
class BundledItem:
    """jar 하나의 판정: as-is jar(분석 시점 경로), 지금 위치(재구성 뒤), 판정 낱말, 사유. 연결됐으면 ``wired``.

    One jar's verdict: the as-is jar, its current (post-move) path, the verdict word, the reason; `wired` once
    applied."""
    jar: JarCoordinate
    verdict: str
    why: str
    current: str = ""
    module_pom: str = ""
    boot_poms: list[str] = field(default_factory=list)
    wired: WiredJar | None = None


@dataclass
class BundledPlan:
    """``plan_bundled_jars`` 의 결과: jar 마다 ``BundledItem`` 하나. ``wired()`` / ``skipped()`` 는
    ``RestructureResult.jars_wired`` / ``jars_skipped`` 모양의 dict 목록.

    The planner's outcome; `wired()` / `skipped()` give the `RestructureResult.jars_wired` / `jars_skipped` shapes."""
    items: list[BundledItem] = field(default_factory=list)

    def by_verdict(self, *verdicts: str) -> list[BundledItem]:
        """판정이 ``verdicts`` 중 하나인 항목들."""
        return [i for i in self.items if i.verdict in verdicts]

    def wired(self) -> list[dict[str, Any]]:
        """연결된 jar 마다 ``{path, pom, coordinates, scope, placeholder, boot_poms}`` (path = as-is 경로)."""
        out: list[dict[str, Any]] = []
        for i in self.items:
            w = i.wired
            if w is None:
                continue
            out.append({"path": i.jar.path, "pom": w.module_pom,
                        "coordinates": f"{w.group_id}:{w.artifact_id}:{w.version}", "scope": w.scope,
                        "placeholder": bool(w.placeholder), "boot_poms": list(w.boot_poms)})
        return out

    def skipped(self) -> list[dict[str, Any]]:
        """연결하지 않은 jar 마다 ``{path, why}`` (``why`` 는 판정 낱말로 시작)."""
        return [{"path": i.jar.path, "why": i.why} for i in self.items if i.wired is None]


def repository_has_fn(lookup: Any) -> Callable[[str, str, str], bool]:
    """``RepositoryLookup`` (또는 None)으로 "저장소에 정확히 g:a:v 가 있는가" 함수를 만든다: ``pom_text(g, a, v)`` 가
    있으면 True, 아니면 ``v in versions(g, a)``. 조회기가 없거나 어떤 예외든 False(= system scope).

    "Does the repository have exactly g:a:v": `pom_text` truthy, else `v in versions(g, a)`; no lookup / any
    exception → False."""
    def _has(g: str, a: str, v: str) -> bool:
        if lookup is None:
            return False
        try:
            if lookup.pom_text(g, a, v):
                return True
            return v in list(lookup.versions(g, a) or [])
        except Exception as e:  # noqa: BLE001 - an unreadable repository counts as "not there"
            log.debug("repository check %s:%s:%s: %s: %s", g, a, v, type(e).__name__, e)
            return False
    return _has


def unused_jar_followup(name: str) -> str:
    """참조가 없어 선언하지 않은 동봉 jar 의 인수인계 한 줄(i18n PATTERNS 가 옮긴다 — 바꾸면 함께).

    The Handover line for a bundled jar nothing references (not declared)."""
    return (f"bundled jar `{name}` is referenced by no source / JSP / XML / properties file — not declared (add it by "
            "hand if a runtime needs it)")


def _container(jar: JarCoordinate) -> bool:
    from sbmig.analyze.jar_coords import is_container_api, provided_scope

    if (jar.scope or "") == "provided":
        return True
    if jar.group_id and jar.artifact_id and provided_scope(jar.group_id, jar.artifact_id):
        return True
    return is_container_api(jar.provides or jar.packages)


def _declares(ws: Any, pom: str, g: str, a: str) -> bool:
    try:
        return PomEditor(ws.abs(pom)).has_dependency(g, a)
    except (PomEditError, OSError) as e:
        log.debug("bundled jars: cannot read %s: %s", pom, e)
        return False


def plan_bundled_jars(ws: Any, cfg: Any, jars: Iterable[JarCoordinate], *, module_pom: str,
                      boot_poms: Iterable[str] = (), managed_keys: Iterable[str] = (),
                      repository: Callable[[str, str, str], bool] | None = None,
                      source_roots: Iterable[str] = (), extra_dirs: Iterable[str] = (),
                      current: dict[str, str] | None = None, managed_known: bool = True) -> BundledPlan:
    """jar 마다 판정 하나(순서대로 첫 규칙): 관리되는 g:a → superseded, 컨테이너 API → container, 참조 없음(``mark_used``
    — ``source_roots`` 의 Java + ``extra_dirs`` 의 JSP / XML / properties; 분석의 ``used`` 는 바꾸지 않는다) → unused,
    대상 pom 이 g:a 를 이미 선언 → declared, 믿을 만한 출처의 완전한 좌표를 저장소가 가짐 → compile, 그 밖 → system
    (``intake.unknown_jar: skip`` 이면 skip). 같은 sha1 의 두 번째 jar 는 첫 jar 의 판정을 따른다(연결은 한 번). 파일을
    바꾸지 않는다. ``current`` = as-is 경로 → 지금 경로.

    Pure classification (first rule wins): managed g:a → superseded; container API copy → container; nothing
    references it → unused; the target pom declares the g:a → declared; trusted full coordinates the repository has →
    compile; else system (`intake.unknown_jar: skip` → skip). A second jar with the same sha1 follows the first.
    `managed_known=False` (the target chain's facts could not be read, so nothing could be called superseded): a jar
    with KNOWN coordinates is not wired blind — `skip`; an unknown-coordinate jar still goes system (integrator, 2026-11-04:
    an old spring-webmvc must never slip through as compile / system when Nexus was unreadable)."""
    from sbmig.analyze.jar_coords import mark_used

    managed = set(managed_keys or ())
    boots = list(dict.fromkeys(boot_poms or ()))
    cur = dict(current or {})
    policy = getattr(getattr(cfg, "intake", None), "unknown_jar", "system") if cfg is not None else "system"
    # 분석의 JarCoordinate 는 그대로 두고 사본으로 used 를 다시 본다(재구성 뒤의 소스·웹 루트 기준)
    copies = [j.model_copy(deep=True) for j in jars]
    roots = [d for d in source_roots if d is not None]
    extra = [d for d in extra_dirs if d is not None]
    if roots or extra:
        try:
            mark_used(copies, roots, ws, extra_dirs=extra)
        except Exception as e:  # noqa: BLE001 - a failed reference scan keeps the analyzer's `used`
            log.warning("bundled jars: reference scan failed: %s: %s", type(e).__name__, e)
    plan = BundledPlan()
    first_by_sha: dict[str, BundledItem] = {}
    for orig, j in zip(jars, copies):
        item = BundledItem(jar=orig, verdict="", why="", current=cur.get(orig.path, orig.path),
                           module_pom=module_pom, boot_poms=list(boots))
        plan.items.append(item)
        prior = first_by_sha.get(j.sha1) if j.sha1 else None
        if prior is not None:
            v = "declared" if prior.verdict in ("compile", "system", "declared") else prior.verdict
            item.verdict, item.why = v, f"{v}: same jar as {prior.jar.path}"
            continue
        if j.sha1:
            first_by_sha[j.sha1] = item
        key = j.key
        g, a, ver = j.group_id, j.artifact_id, j.version
        if key and key in managed:
            item.verdict, item.why = "superseded", f"superseded: {key} is managed by the target parent chain"
        elif _container(j):
            item.verdict, item.why = "container", "container: a servlet / JSP API copy the container supplies"
        elif j.used is False:
            item.verdict, item.why = "unused", "unused: referenced by no source / JSP / XML / properties file"
        elif key and _declares(ws, module_pom, g, a):
            item.verdict, item.why = "declared", f"declared: {key} is already declared in {module_pom}"
        elif key and not managed_known:
            item.verdict, item.why = "skip", (f"skip: managed state unknown — the target parent chain's facts could not be "
                                              f"read, so {key} may be a library the chain manages; not wired blind (the fix "
                                              "loop's pre-try may wire it on a build error)")
        else:
            has = False
            if j.source in TRUSTED_SOURCES and g and a and ver and repository is not None:
                try:
                    has = bool(repository(g, a, ver))
                except Exception as e:  # noqa: BLE001 - "not there" on any failure
                    log.debug("repository check %s:%s:%s failed: %s", g, a, ver, e)
                    has = False
            if has:
                item.verdict, item.why = "compile", f"compile: {g}:{a}:{ver} is in the repository"
            elif policy == "skip":
                item.verdict, item.why = "skip", ("skip: not in the repository and intake.unknown_jar is skip — "
                                                  "declare it by hand (publish it to Nexus first)")
            else:
                coords = f"{g}:{a}:{ver} not in the repository" if g and a and ver else "coordinates unknown"
                item.verdict, item.why = "system", f"system: {coords}"
    return plan


def apply_bundled_jars(ws: Any, cfg: Any, plan: BundledPlan, *, project_group: str = "") -> list[WiredJar]:
    """``compile`` / ``system`` 항목을 ``wire_bundled_jar`` 로 쓴다(compile = 저장소에 있음). jar 하나의 실패는 WARNING
    한 줄 + 그 항목을 ``skip`` (사유 = 오류)으로 바꾸고 다음 jar 로 — 예외를 밖으로 내지 않는다. 커밋하지 않는다.

    Write the `compile` / `system` items through `wire_bundled_jar`; a failing jar becomes `skip` with the error
    (one WARNING), never raises. Never commits."""
    out: list[WiredJar] = []
    for item in plan.items:
        if item.verdict not in ("compile", "system"):
            continue
        jar = item.jar.model_copy(update={"path": item.current or item.jar.path})
        try:
            w = wire_bundled_jar(ws, cfg, module_pom=item.module_pom, jar=jar,
                                 repository_has=item.verdict == "compile", boot_poms=list(item.boot_poms),
                                 project_group=project_group)
        except Exception as e:  # noqa: BLE001 - one jar never fails the restructure
            log.warning("bundled jar %s not wired into %s: %s: %s", item.jar.path, item.module_pom,
                        type(e).__name__, e)
            item.verdict, item.why = "skip", f"skip: wiring failed: {type(e).__name__}: {e}"[:300]
            continue
        # wire_bundled_jar 는 멱등: 같은 g:a 가 이미 있으면 아무것도 쓰지 않고 "already declared" 를 남긴다
        if any(n.startswith(f"{w.key} already declared") for n in w.notes):
            item.verdict, item.why = "declared", f"declared: {w.key} is already declared in {w.module_pom}"
            continue
        item.wired = w
        out.append(w)
    return out


def jar_name(path: str) -> str:
    """경로의 파일 이름(``/`` · ``\\`` 모두)."""
    return posixpath.basename(path.replace("\\", "/"))
