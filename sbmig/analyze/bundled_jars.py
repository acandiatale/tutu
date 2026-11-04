"""동봉 jar(v2.1z-i): pom 이 선언하지 않은 채 트리 안에 들어 있는 jar(``WEB-INF/lib``, ``lib/`` …)를 찾아 좌표와
클래스 패키지 목록을 적는다. as-is 는 IDE 의 웹앱 라이브러리 컨테이너로 컴파일했으니 Maven 은 그 jar 를 모른다 — 현업
케이스(2026-11-04): ``WEB-INF/lib/Crypt.jar`` 의 ``com….crypto.seed.Seed`` 를 ``package … does not exist`` 로 잃고 LLM 은
좌표 부재로 사양했다. 폴더 이름이 아니라 내용(``*.jar`` 이고 어떤 pom 의 system 의존성도 가리키지 않음)으로 고른다.

**계약(PM, 2026-11-04) — 구현은 분석 에이전트 몫**

Bundled jars (v2.1z-i): jars that sit in the tree and that no pom declares. The as-is IDE build saw them on its
"Web App Libraries" classpath; Maven does not. `scan_bundled_jars` lists them with coordinates (`resolve_jar`:
pom.properties → jar name table → sha1 lookup) and EVERY class-holding package (`JarCoordinate.provides`);
`providers` answers "which bundled jar holds package p" for the fix loop's `sbmig-bundled` pre-try.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sbmig.core.models import JarCoordinate, PomInfo
from sbmig.core.workspace import is_build_output_path

from .edges import system_dependencies
from .jar_coords import JarScan, Sha1Lookup, jar_name_table, mark_used, resolve_jar_scan

__all__ = ["declared_system_jars", "providers", "scan_bundled_jars"]

log = logging.getLogger("sbmig.analyze")


def declared_system_jars(ws: Any, poms: Iterable[PomInfo]) -> set[str]:
    """어떤 pom 이 ``<scope>system</scope>`` 으로 이미 가리키는 jar 의 작업공간 상대 경로들(``systemPath`` 를
    ``${project.basedir}`` / ``${basedir}`` 기준으로 풀어서). 이 jar 들은 동봉 jar 가 아니다(이미 선언됨).

    Workspace-relative paths of the jars some pom already declares with `system` scope (`systemPath` resolved
    against the pom's directory for `${project.basedir}` / `${basedir}`). Those are not bundled jars."""
    try:
        # edges.system_dependencies 가 이미 systemPath 를 같은 규칙(basedir = 선언한 pom 의 폴더, 상대 경로 = 그 폴더
        # 기준, 저장소 안 부모 체인 속성)으로 푼다 — 두 번째 해석기를 만들지 않는다
        deps = system_dependencies(ws, list(poms))
    except Exception as e:  # noqa: BLE001 - a pom that cannot be read never fails the scan
        log.warning("bundled jars: system dependencies not read: %s: %s", type(e).__name__, e)
        return set()
    return {d.resolved for d in deps if d.inside and d.resolved}


def _skipped(ws: Any, rel: str) -> bool:
    """빌드 산출물 · ``.git`` · 접수가 무시한 폴더 안인가(``ws.glob`` 이 이미 거르지만 대역 객체를 위해 한 번 더)."""
    if ".git" in rel.split("/") or is_build_output_path(rel, Path(ws.root)):
        return True
    is_ignored = getattr(ws, "is_ignored", None)
    return bool(is_ignored(rel)) if callable(is_ignored) else False


def scan_bundled_jars(ws: Any, cfg: Any, poms: Iterable[PomInfo], *, lookup: Any = None) -> list[JarCoordinate]:
    """트리의 ``*.jar`` 중 빌드 산출물(``target/`` 등)·``.git``·접수가 무시한 폴더(``ws.is_ignored``) 밖에 있고
    ``declared_system_jars`` 에 없는 것 전부 → ``resolve_jar``(pom.properties → 이름표 → sha1 조회, ``lookup`` 이
    None 이면 ``intake.jar_lookup`` 에 따라 만든다) 로 좌표를 적고 ``provides`` 에 클래스가 있는 패키지 전부를 넣는다.
    경로순 정렬, 같은 sha1 은 둘 다 적되 note 에 "same jar as …". 예외는 WARNING 로그 + 그 jar 만 건너뛴다(분석은 절대
    실패하지 않는다). ``cfg.intake.bundled_jars`` 가 False 면 [].

    Every `*.jar` in the tree outside build output / `.git` / the intake's ignored dirs and not in
    `declared_system_jars` → `resolve_jar(path, data, cfg, lookup)` for the coordinates, `provides` = every package
    holding a class (uncapped; `packages` keeps the capped display list). Sorted by path; a duplicate sha1 is kept with
    a note. Never raises (a jar that cannot be read is skipped with a WARNING). `[]` when `cfg.intake.bundled_jars`
    is False."""
    intake = getattr(cfg, "intake", None)
    if intake is not None and not getattr(intake, "bundled_jars", True):
        return []
    try:
        poms = list(poms)
        candidates = sorted(r for r in ws.glob("*.jar") if not _skipped(ws, r))
        if not candidates:
            return []
        declared = declared_system_jars(ws, poms)
        candidates = [r for r in candidates if r not in declared]
        if not candidates:
            return []
        root = Path(ws.root)
        table = jar_name_table(cfg)
        lk: Any = lookup
        if lk is None:
            # 실행 전체에 조회기 하나 — "연속 실패 2번이면 더 묻지 않음" 규칙이 jar 마다 새로 시작하지 않게
            lk = Sha1Lookup(cfg) if getattr(intake, "jar_lookup", True) else False
    except Exception as e:  # noqa: BLE001 - the analysis never fails on the bundled-jar scan
        log.warning("bundled jars: scan skipped: %s: %s", type(e).__name__, e)
        return []
    out: list[JarCoordinate] = []
    first_by_sha1: dict[str, str] = {}
    for rel in candidates:
        try:
            p = root / rel
            if not p.is_file():
                continue
            jc, js = resolve_jar_scan(rel, p, cfg, lk, table=table)
        except Exception as e:  # noqa: BLE001 - one unreadable jar is skipped, the others are still listed
            log.warning("bundled jars: %s skipped: %s: %s", rel, type(e).__name__, e)
            continue
        jc.provides = sorted((js if isinstance(js, JarScan) else JarScan()).packages)
        if jc.sha1:
            first = first_by_sha1.setdefault(jc.sha1, rel)
            if first != rel:
                jc.note = "; ".join(n for n in (jc.note, f"same jar as {first}") if n)
        out.append(jc)
    if out:
        # used: 트리의 Java 소스가 jar 의 패키지를 가리키는가(동봉 jar 가 있을 때만 한 번 훑는다 — 드문 경우라 비용 작음)
        try:
            mark_used(out, [""], ws)
        except Exception as e:  # noqa: BLE001 - `used` stays None
            log.debug("bundled jars: used check skipped: %s", e)
    return out


def providers(jars: Iterable[JarCoordinate], package: str) -> list[JarCoordinate]:
    """``package`` 를 가진 동봉 jar 들 — ``provides`` 에 정확히 있거나(우선), 그 패키지의 상위가 아닌 **하위** 패키지만
    있는 경우(``p.x`` 는 있고 ``p`` 는 없음 — 오류가 상위 패키지를 말할 때)도 포함. 정확 일치 먼저, 그 다음 경로순.

    The bundled jars holding `package`: an exact entry of `provides` first, then jars holding only sub-packages of it
    (`p.x` present, `p` absent — javac may name the parent). Exact matches first, then by path. Never raises."""
    try:
        pkg = (package or "").strip().strip(".")
        if not pkg:
            return []
        exact: list[JarCoordinate] = []
        sub: list[JarCoordinate] = []
        prefix = pkg + "."
        for j in jars or ():
            provided = getattr(j, "provides", None) or ()
            if pkg in provided:
                exact.append(j)
            elif any(p.startswith(prefix) for p in provided):
                sub.append(j)
        return sorted(exact, key=lambda j: j.path) + sorted(sub, key=lambda j: j.path)
    except Exception as e:  # noqa: BLE001 - a malformed entry never breaks the fix loop
        log.debug("bundled jars: providers(%s): %s", package, e)
        return []
