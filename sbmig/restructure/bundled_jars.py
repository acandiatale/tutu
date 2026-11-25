"""어느 pom도 선언하지 않은 트리 안의 jar를 재구성에서 첫 빌드 전에 모듈 pom에 연결하는 모듈.
JDBC 드라이버, 필터, taglib처럼 컴파일 오류를 내지 않는 런타임 전용 jar도 as-is WAR처럼 패키징하려는 것이다.
jar마다 아래 결과 중 하나를 정한다.
- ``superseded``: 목표 부모 체인이나 Boot BOM이 관리한다. 손대지 않는다
- ``container``: Tomcat이 제공하는 서블릿, JSP API다. 손대지 않는다
- ``unused``: 어디서도 참조하지 않는다. 선언하지 않고 인수인계에 이름만 남긴다
- ``declared``: 대상 pom이 이미 선언했다
- ``compile``: 좌표를 알고 저장소에 있다
- ``system``: 그 밖의 경우다. ``<module>/lib``에 복사하고 system scope로 선언한다
- ``skip``: ``intake.unknown_jar: skip``이거나 저장소를 읽지 못했거나 연결 중 오류가 났다

Contract: docs/INTERFACES.md "v2.1z-j". `plan_bundled_jars` is pure; `apply_bundled_jars` writes through
`system_jars.wire_bundled_jar` and never raises past a per-jar WARNING (a failed jar becomes `skip`)."""
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
           "repository_has_fn", "repository_state_fn", "unused_jar_followup"]

log = logging.getLogger(__name__)

VERDICTS = ("superseded", "container", "unused", "declared", "compile", "system", "skip")
# 저장소에 조회할 만한 좌표 출처. 수정 루프의 BUNDLED_TRUSTED_SOURCES와 같아야 한다.
# manifest로 추정한 좌표는 믿지 않는다
TRUSTED_SOURCES = ("pom_properties", "sha1_lookup", "name_table")


@dataclass
class BundledItem:
    """jar 하나의 결과를 담는 클래스.
    as-is jar, 재구성 뒤 경로, 결과 값, 사유를 담는다. 연결하면 ``wired``가 채워진다.
    """
    jar: JarCoordinate
    verdict: str
    why: str
    current: str = ""
    module_pom: str = ""
    boot_poms: list[str] = field(default_factory=list)
    wired: WiredJar | None = None


@dataclass
class BundledPlan:
    """``plan_bundled_jars``의 결과를 담는 클래스. jar마다 ``BundledItem``이 하나씩 있다.
    ``wired()``와 ``skipped()``는 ``RestructureResult.jars_wired`` / ``jars_skipped`` 형태로 반환한다.
    """
    items: list[BundledItem] = field(default_factory=list)

    def by_verdict(self, *verdicts: str) -> list[BundledItem]:
        """결과 값이 ``verdicts`` 중 하나인 항목을 반환한다."""
        return [i for i in self.items if i.verdict in verdicts]

    def wired(self) -> list[dict[str, Any]]:
        """연결된 jar마다 ``{path, pom, coordinates, scope, placeholder, boot_poms}``를 반환한다.
        path는 as-is 경로다."""
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
        """연결하지 않은 jar마다 ``{path, why}``를 반환한다. ``why``는 결과 값으로 시작한다."""
        return [{"path": i.jar.path, "why": i.why} for i in self.items if i.wired is None]


def repository_has_fn(lookup: Any) -> Callable[[str, str, str], bool]:
    """저장소에 g:a:v가 정확히 있는지 확인하는 함수를 만든다.
    ``pom_text(g, a, v)``가 있으면 True, 없으면 ``versions(g, a)``에 v가 있는지로 정한다.
    조회기가 없거나 예외가 나면 False다. 그러면 그 jar는 system scope로 연결된다."""
    def _has(g: str, a: str, v: str) -> bool:
        if lookup is None:
            return False
        try:
            if lookup.pom_text(g, a, v):
                return True
            return v in list(lookup.versions(g, a) or [])
        except Exception as e:  # noqa: BLE001 - 읽지 못한 저장소는 없는 것으로 본다
            log.debug("repository check %s:%s:%s: %s: %s", g, a, v, type(e).__name__, e)
            return False
    return _has


def repository_state_fn(lookup: Any) -> Callable[[str, str, str], tuple[str, str]]:
    """저장소에 g:a:v가 있는지 (상태, 사유)로 답하는 함수를 만든다. 상태는 present, absent, unreadable이다.
    원격 저장소가 응답하지 않아 찾지 못했으면 없다고 하지 않고 unreadable과 사유를 반환한다.

    present: the pom of g:a:v, else the version in the metadata (remote or local).
    absent: asked and not there, or no lookup configured.
    unreadable: nothing found while no remote answered (`lookup.remote_failures(g, a)`'s sentence) or the
    lookup raised. A caller must not treat `unreadable` as "not in the repository"."""
    def _state(g: str, a: str, v: str) -> tuple[str, str]:
        if lookup is None:
            return "absent", ""
        try:
            if lookup.pom_text(g, a, v):
                return "present", ""
            if v in list(lookup.versions(g, a) or []):
                return "present", ""
            failures = lookup.remote_failures(g, a) if hasattr(lookup, "remote_failures") else ""
        except Exception as e:  # noqa: BLE001 - 조회가 실패하면 답을 모르는 것이다
            log.debug("repository check %s:%s:%s: %s: %s", g, a, v, type(e).__name__, e)
            return "unreadable", f"{type(e).__name__}: {e}"[:300]
        return ("unreadable", str(failures)) if failures else ("absent", "")
    return _state


def unused_jar_followup(name: str) -> str:
    """참조가 없어 선언하지 않은 jar의 인수인계 문장을 만든다.
    한국어 보고서는 i18n PATTERNS로 번역하므로 문장을 바꾸면 그쪽도 함께 고친다."""
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
                      repository: Callable[[str, str, str], Any] | None = None,
                      source_roots: Iterable[str] = (), extra_dirs: Iterable[str] = (),
                      current: dict[str, str] | None = None, managed_known: bool = True) -> BundledPlan:
    """jar마다 결과를 하나 정한다. 파일은 바꾸지 않는다.
    참조 여부는 ``source_roots``의 Java와 ``extra_dirs``의 JSP, XML, properties로 다시 확인한다.
    sha1이 같은 두 번째 jar는 첫 jar의 결과를 따른다.

    First rule wins: managed g:a -> superseded; container API copy -> container; nothing references it ->
    unused; the target pom declares the g:a -> declared; `managed_known=False` and known coordinates -> skip;
    trusted full coordinates the repository has -> compile; repository unreadable -> skip;
    `intake.unknown_jar: skip` -> skip; else system.
    `managed_known=False` means the target chain's facts could not be read: a jar with known coordinates
    (an old spring-webmvc) must not be wired blind; an unknown-coordinate jar still goes system."""
    from sbmig.analyze.jar_coords import mark_used

    managed = set(managed_keys or ())
    boots = list(dict.fromkeys(boot_poms or ()))
    cur = dict(current or {})
    policy = getattr(getattr(cfg, "intake", None), "unknown_jar", "system") if cfg is not None else "system"
    # 분석 결과는 그대로 두고, 깊은 복사본으로 재구성 뒤 위치 기준의 used를 다시 계산한다
    copies = [j.model_copy(deep=True) for j in jars]
    roots = [d for d in source_roots if d is not None]
    extra = [d for d in extra_dirs if d is not None]
    if roots or extra:
        try:
            mark_used(copies, roots, ws, extra_dirs=extra)
        except Exception as e:  # noqa: BLE001 - 검사가 실패하면 분석기의 used를 그대로 쓴다
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
            unreadable = ""
            if j.source in TRUSTED_SOURCES and g and a and ver and repository is not None:
                try:
                    answer = repository(g, a, ver)
                    # repository_state_fn은 (상태, 사유)를 반환하고, bool을 반환하는 함수도 받는다
                    if isinstance(answer, tuple):
                        has = answer[0] == "present"
                        unreadable = (answer[1] or "no reason given") if answer[0] == "unreadable" else ""
                    else:
                        has = bool(answer)
                except Exception as e:  # noqa: BLE001 - 실패하면 저장소에 없는 것으로 본다
                    log.debug("repository check %s:%s:%s failed: %s", g, a, ver, e)
                    has = False
            if unreadable:
                # 응답이 없었을 뿐인데 없다고 보고 Nexus 등록을 요청하면 안 되므로 연결하지 않는다
                item.verdict, item.why = "skip", (f"skip: repository unreadable — {g}:{a}:{ver} could not be looked up "
                                                  f"({unreadable}); not wired and not called missing — run again when "
                                                  "the repository answers")
            elif has:
                item.verdict, item.why = "compile", f"compile: {g}:{a}:{ver} is in the repository"
            elif policy == "skip":
                item.verdict, item.why = "skip", ("skip: not in the repository and intake.unknown_jar is skip — "
                                                  "declare it by hand (publish it to Nexus first)")
            else:
                coords = f"{g}:{a}:{ver} not in the repository" if g and a and ver else "coordinates unknown"
                item.verdict, item.why = "system", f"system: {coords}"
    return plan


def apply_bundled_jars(ws: Any, cfg: Any, plan: BundledPlan, *, project_group: str = "") -> list[WiredJar]:
    """``compile``과 ``system`` 항목을 ``wire_bundled_jar``로 pom에 쓴다. 커밋은 하지 않는다.
    jar 하나가 실패하면 WARNING을 남기고 그 항목을 ``skip``으로 바꾼 뒤 다음 jar로 넘어간다.
    예외를 던지지 않는다."""
    out: list[WiredJar] = []
    for item in plan.items:
        if item.verdict not in ("compile", "system"):
            continue
        jar = item.jar.model_copy(update={"path": item.current or item.jar.path})
        try:
            w = wire_bundled_jar(ws, cfg, module_pom=item.module_pom, jar=jar,
                                 repository_has=item.verdict == "compile", boot_poms=list(item.boot_poms),
                                 project_group=project_group)
        except Exception as e:  # noqa: BLE001 - jar 하나 때문에 재구성을 실패시키지 않는다
            log.warning("bundled jar %s not wired into %s: %s: %s", item.jar.path, item.module_pom,
                        type(e).__name__, e)
            item.verdict, item.why = "skip", f"skip: wiring failed: {type(e).__name__}: {e}"[:300]
            continue
        # 같은 g:a가 이미 있으면 wire_bundled_jar는 아무것도 쓰지 않고 "already declared"만 남긴다
        if any(n.startswith(f"{w.key} already declared") for n in w.notes):
            item.verdict, item.why = "declared", f"declared: {w.key} is already declared in {w.module_pom}"
            continue
        item.wired = w
        out.append(w)
    return out


def jar_name(path: str) -> str:
    """경로에서 파일 이름만 반환한다. 슬래시와 역슬래시 구분자를 모두 처리한다."""
    return posixpath.basename(path.replace("\\", "/"))
