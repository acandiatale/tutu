"""첫 Maven 호출 전에 사내 부모 체인의 build extension을 받을 수 있는지 점검하는 모듈.
상속된 확장은 자식 pom에서 뺄 수 없고, 하나라도 받지 못하면 OpenRewrite를 포함한 모든 Maven 호출이
``Unresolveable build extension``으로 실패한다.
- 확장마다 로컬 저장소나 원격 저장소에서 pom을 읽을 수 있는지 확인한다
- 원격에서는 읽히는데 로컬에 오래된 ``*.lastUpdated`` 표식만 있으면 그 폴더의 표식만 지운다.
  ``~/.m2`` 전체는 건드리지 않는다
- 어디서도 읽지 못하면 경고, 플래그, 인수인계로 운영자에게 알린다.
  해당 확장을 Nexus에 올리거나 부모 pom에서 빼야 한다

Pre-check of the configured parent chain's build extensions before the first Maven call: readable from the local
repository / the repository set? Stale `*.lastUpdated` markers of an extension the repository does serve are deleted
(that artifact folder only); an extension nobody can serve is reported (warning, flag, Handover line). Never raises."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .boot_bom import PomReader, local_repository, make_resolver, parse_pom, pom_builtins

MAX_ANCESTORS = 8      # the extension pom's <parent> chain Maven must also read (wagon-webdav → wagon-providers → wagon → maven-parent → apache)

log = logging.getLogger("sbmig.build.parent_extensions")


@dataclass
class ExtensionCheck:
    """build extension 하나의 점검 결과를 담는 데이터 클래스.
    좌표, 선언한 pom, 읽기 결과와 출처, 지운 표식 파일을 담는다."""
    coordinates: str
    declared_by: str
    readable: bool
    where: str
    markers_removed: list[str] = field(default_factory=list)
    ancestors: list[str] = field(default_factory=list)          # the extension pom's <parent> chain that was read

    def line(self) -> str:
        """로그와 저널에 남길 한 줄 요약을 만든다."""
        tail = f"; stale markers removed: {', '.join(self.markers_removed)}" if self.markers_removed else ""
        anc = f"; parent poms: {' → '.join(self.ancestors)}" if self.ancestors else ""
        return f"{self.coordinates} (declared by {self.declared_by}): {'readable' if self.readable else 'NOT readable'} — {self.where}{tail}{anc}"


def _split(coord: str) -> tuple[str, str, str] | None:
    parts = coord.split(":")
    ok = len(parts) == 3 and all(parts) and "?" not in parts and not any("${" in x for x in parts)
    return (parts[0], parts[1], parts[2]) if ok else None


def check_parent_build_extensions(cfg: Any, facts: Any = None, *, clear_markers: bool = True) -> list[ExtensionCheck]:
    """부모 체인이 선언한 build extension을 하나씩 점검해 결과 목록을 반환한다.
    ``clear_markers``가 참이면 원격에서 읽히는 확장의 로컬 표식을 지운다.
    체인을 읽지 못했거나 확장이 없으면 빈 목록을 반환하고, 예외를 던지지 않는다.

    One `ExtensionCheck` per build extension the parent chain declares; stale local markers of a repository-served
    extension are deleted when `clear_markers`. [] when the chain was not read or declares none. Never raises."""
    out: list[ExtensionCheck] = []
    try:
        if facts is None:
            from .parent_chain import load_parent_chain_facts
            facts = load_parent_chain_facts(cfg)
        exts = list(getattr(facts, "build_extensions", []) or [])
        if not exts:
            return out
        local = local_repository(cfg)
        # every extension may need its whole <parent> chain (wagon-webdav → providers → wagon → maven-parent → apache); a shared
        # ancestor is read once (memo) — the old limit of 8 made the second extension "NOT readable — skipped (more than 8 poms)"
        reader = PomReader(cfg, local, limit=len(exts) * (MAX_ANCESTORS + 1) + 2)
        memo: dict[tuple[str, str, str], tuple[str | None, str]] = {}

        def read(g: str, a: str, v: str) -> tuple[str | None, str]:
            key = (g, a, v)
            if key not in memo:
                memo[key] = reader.pom(g, a, v)
            return memo[key]
        for coord in exts:
            declared = (getattr(facts, "build_extension_sources", {}) or {}).get(coord, "?")
            gav = _split(coord)
            if gav is None:
                out.append(ExtensionCheck(coord, declared, False, "coordinates not resolvable from the chain's properties"))
                continue
            g, a, v = gav
            text, where = read(g, a, v)
            check = ExtensionCheck(coord, declared, bool(text), where)
            if text and clear_markers and local is not None:
                # readable locally or from the repository: the folder is Maven's → a stale `.jar.lastUpdated` beside a present pom
                # would still make Maven refuse the jar for a day, so the markers go in both cases
                check.markers_removed = _remove_stale_markers(local.joinpath(*g.split("."), a, v))
            if text:
                # Maven reads the extension's <parent> chain too ("or one of its dependencies could not be resolved"): a 2007 pom's
                # ancestors (wagon-providers, wagon, maven-parent, apache) must be served as well — follow them and name the first gap
                _check_ancestors(read, text, check, local, clear_markers)
            out.append(check)
    except Exception as e:  # noqa: BLE001 - a pre-check never stops the intake; Maven reports the problem as before
        log.warning("parent build extension check failed: %s: %s", type(e).__name__, e)
    return out


def _check_ancestors(read: Any, text: str, check: ExtensionCheck, local: Path | None, clear_markers: bool) -> None:
    """확장 pom의 ``<parent>`` 체인을 최대 MAX_ANCESTORS단계까지 읽는다.
    읽지 못한 조상이 있으면 그 확장을 "NOT readable"로 표시하고 사유에 조상 좌표를 남긴다."""
    pom = parse_pom(text)
    seen: set[str] = set()
    for _ in range(MAX_ANCESTORS):
        if pom is None or not (pom.parent_group_id and pom.parent_artifact_id and pom.parent_version):
            return
        resolve = make_resolver(pom.properties, pom_builtins(pom))
        pg, pa, pv = (resolve(pom.parent_group_id) or pom.parent_group_id, resolve(pom.parent_artifact_id) or pom.parent_artifact_id,
                      resolve(pom.parent_version) or pom.parent_version)
        coord = f"{pg}:{pa}:{pv}"
        if coord in seen:
            return
        seen.add(coord)
        ptext, where = read(pg, pa, pv)
        if not ptext:
            check.readable = False
            check.where = f"its parent pom {coord} is not readable — {where}"
            return
        check.ancestors.append(coord)
        if clear_markers and local is not None:
            check.markers_removed += _remove_stale_markers(local.joinpath(*pg.split("."), pa, pv))
        pom = parse_pom(ptext)


def _remove_stale_markers(folder: Path) -> list[str]:
    """``<local repo>/<g>/<a>/<v>/*.lastUpdated`` 파일만 지우고 지운 파일 이름 목록을 반환한다."""
    removed: list[str] = []
    try:
        if not folder.is_dir():
            return removed
        for f in sorted(folder.glob("*.lastUpdated")):
            try:
                f.unlink()
                removed.append(f.name)
            except OSError as e:
                log.debug("stale marker %s not removed: %s", f, e)
    except OSError as e:
        log.debug("stale markers under %s not listed: %s", folder, e)
    return removed
