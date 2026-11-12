"""pom에 선언되지 않은 채 트리에 들어 있는 jar(``WEB-INF/lib``, ``lib/`` 등)를 찾아 좌표와 패키지 목록을 기록하는 모듈.
as-is 프로젝트는 IDE의 웹앱 라이브러리로 컴파일했으므로 Maven은 이 jar를 모른다.
폴더 이름이 아니라 내용으로 고른다.
``*.jar`` 파일 중 어떤 pom의 system 의존성도 가리키지 않는 파일이 대상이다.

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
    """pom이 ``<scope>system</scope>``으로 이미 가리키는 jar의 작업공간 상대 경로 목록을 반환한다.
    이 jar들은 동봉 jar로 보지 않는다.

    Workspace-relative paths of the jars some pom already declares with `system` scope (`systemPath` resolved
    against the pom's directory for `${project.basedir}` / `${basedir}`). Those are not bundled jars."""
    try:
        # edges.system_dependencies가 같은 규칙으로 systemPath를 해석하므로 해석 코드를 따로 만들지 않는다
        deps = system_dependencies(ws, list(poms))
    except Exception as e:  # noqa: BLE001 - a pom that cannot be read never fails the scan
        log.warning("bundled jars: system dependencies not read: %s: %s", type(e).__name__, e)
        return set()
    return {d.resolved for d in deps if d.inside and d.resolved}


def _skipped(ws: Any, rel: str) -> bool:
    """경로가 빌드 산출물, ``.git``, 접수가 무시한 폴더 안에 있는지 확인한다.
    ``ws.glob``이 이미 거르지만 가짜 구현을 위해 한 번 더 확인한다."""
    if ".git" in rel.split("/") or is_build_output_path(rel, Path(ws.root)):
        return True
    is_ignored = getattr(ws, "is_ignored", None)
    return bool(is_ignored(rel)) if callable(is_ignored) else False


def scan_bundled_jars(ws: Any, cfg: Any, poms: Iterable[PomInfo], *, lookup: Any = None) -> list[JarCoordinate]:
    """트리의 동봉 jar를 모두 찾아 ``resolve_jar``로 좌표를 구하고,
    클래스가 들어 있는 패키지를 모두 ``provides``에 넣는다.
    경로순으로 정렬하고 sha1이 같은 jar에는 note를 붙인다.
    읽지 못한 jar는 WARNING을 남기고 건너뛰므로 분석은 실패하지 않는다.
    ``cfg.intake.bundled_jars``가 False면 빈 목록을 반환한다.

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
            # 연속 실패 횟수가 jar마다 초기화되지 않도록 실행 전체에서 조회 객체 하나를 쓴다
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
        # Java 소스가 jar의 패키지를 쓰는지 표시한다. 동봉 jar가 있을 때만 검사하므로 비용이 작다
        try:
            mark_used(out, [""], ws)
        except Exception as e:  # noqa: BLE001 - `used` stays None
            log.debug("bundled jars: used check skipped: %s", e)
    return out


def providers(jars: Iterable[JarCoordinate], package: str) -> list[JarCoordinate]:
    """``package``를 제공하는 동봉 jar 목록을 반환한다.
    ``provides``에 그 패키지가 정확히 있는 jar가 먼저, 하위 패키지만 가진 jar가 다음이다.
    javac가 상위 패키지 이름으로 오류를 내는 경우가 있어 하위 패키지도 본다.

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
