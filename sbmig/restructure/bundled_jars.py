"""어느 pom도 선언하지 않은 트리 안의 jar(``profile.bundled_jars``)를 재구성
단계에서 첫 빌드 전에 대상 모듈 pom에 연결하는 모듈.
JDBC 드라이버, sitemesh 필터, taglib 구현처럼 컴파일 오류를 내지 않는
런타임 전용 jar도 as-is WAR처럼 패키징되게 하기 위해서다.
jar마다 아래 결과 중 하나를 정한다.
- ``superseded``(목표 부모 체인이나 Boot BOM이 관리), ``container``(Tomcat이 제공하는 서블릿 / JSP API): 손대지 않는다
- ``unused``: 어디서도 참조하지 않으므로 선언하지 않고 인수인계에 이름만 남긴다.
  ``declared``: 대상 pom이 이미 선언했다
- ``compile``: 좌표를 알고 저장소에 있다.
  ``system``: 그렇지 않으므로 ``<module>/lib``에 복사하고 system scope로 선언한다
- ``skip``: ``intake.unknown_jar: skip``이거나 연결 중 오류가 났다
결정(``plan_bundled_jars``)은 파일을 바꾸지 않고, 쓰기(``apply_bundled_jars``)는
``system_jars.wire_bundled_jar``를 거친다.

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
# 저장소에 조회할 만한 좌표 출처이며 수정 루프의 BUNDLED_TRUSTED_SOURCES와 같다.
# manifest로 추정한 좌표는 믿지 않는다
TRUSTED_SOURCES = ("pom_properties", "sha1_lookup", "name_table")


@dataclass
class BundledItem:
    """jar 하나에 대한 결정을 담는 데이터 클래스.
    as-is 경로, 재구성 뒤 경로, 결과 값, 사유를 담고, 연결되면 ``wired``가 된다.

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
    """``plan_bundled_jars``의 결과를 담는 데이터 클래스. jar마다 ``BundledItem``이 하나씩 있다.
    ``wired()`` / ``skipped()``는 ``RestructureResult.jars_wired`` / ``jars_skipped`` 형태의 dict 목록을 반환한다.

    The planner's outcome; `wired()` / `skipped()` give the `RestructureResult.jars_wired` / `jars_skipped` shapes."""
    items: list[BundledItem] = field(default_factory=list)

    def by_verdict(self, *verdicts: str) -> list[BundledItem]:
        """결과 값이 ``verdicts`` 중 하나인 항목을 반환한다."""
        return [i for i in self.items if i.verdict in verdicts]

    def wired(self) -> list[dict[str, Any]]:
        """연결된 jar마다 ``{path, pom, coordinates, scope, placeholder, boot_poms}`` dict를 만들어
        반환한다. path는 as-is 경로다."""
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
        """연결하지 않은 jar마다 ``{path, why}`` dict를 만들어 반환한다. ``why``는 결과 값으로 시작한다."""
        return [{"path": i.jar.path, "why": i.why} for i in self.items if i.wired is None]


def repository_has_fn(lookup: Any) -> Callable[[str, str, str], bool]:
    """``RepositoryLookup``으로 저장소에 g:a:v가 정확히 있는지 확인하는 함수를 만든다.
    ``pom_text(g, a, v)``가 있으면 True, 없으면 ``v in versions(g, a)``로 판단한다.
    조회기가 없거나 예외가 나면 False가 되어 그 jar는 system scope로 연결된다.

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
    """참조가 없어 선언하지 않은 동봉 jar에 대한 인수인계 문구를 만든다.
    i18n PATTERNS가 번역하므로 고칠 때 함께 고친다.

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
    """jar마다 결과를 하나 정한다. 아래 순서로 처음 맞는 규칙을 쓰며 파일은 바꾸지 않는다.
    관리되는 g:a면 superseded, 컨테이너 API면 container, 참조가 없으면 unused, 대상 pom이 이미 선언했으면
    declared, 믿을 만한 좌표가 저장소에 있으면 compile, 그 밖에는 system이다.
    참조 여부는 ``source_roots``의 Java와 ``extra_dirs``의 JSP / XML / properties로 다시 확인한다.
    sha1이 같은 두 번째 jar는 첫 jar의 결과를 따른다.

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
    # 분석 결과의 JarCoordinate는 그대로 두고, 깊은 복제본으로 재구성 뒤의 소스와 웹 루트 기준 used를 다시 계산한다
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
    """``compile`` / ``system`` 항목을 ``wire_bundled_jar``로 pom에 쓴다. 커밋은 하지 않는다.
    jar 하나가 실패하면 WARNING 한 줄을 남기고 그 항목을 ``skip``(사유는 오류 내용)으로 바꾼 뒤 다음 jar로
    넘어간다. 예외를 던지지 않는다.

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
        # wire_bundled_jar는 같은 g:a가 이미 있으면 아무것도 쓰지 않고 "already declared"만 남긴다
        if any(n.startswith(f"{w.key} already declared") for n in w.notes):
            item.verdict, item.why = "declared", f"declared: {w.key} is already declared in {w.module_pom}"
            continue
        item.wired = w
        out.append(w)
    return out


def jar_name(path: str) -> str:
    """경로에서 파일 이름만 반환한다. 슬래시와 역슬래시 구분자를 모두 처리한다."""
    return posixpath.basename(path.replace("\\", "/"))
