"""사내 아티팩트 판정: 회사 artifacts yml(``cfg.artifacts``)이 적은 사내 라이브러리의 g:a 표와, 의존성 사슬에 그것이 있는지.
흐름상 위치: 그래프의 ``inventory`` 노드가 ``inhouse_keys`` 로 표를 만들어 ``inventory.build_inventory`` 에 넘기고, 인벤토리가
모듈마다 ``inhouse_on_path`` 로 사슬을 본다. planner 는 그 결과로 잔여(``inhouse_artifact``)를 정하고, 표는 정책 해시에도 들어간다.

회사 규칙(2026-11-12): 취약 라이브러리가 사내 아티팩트를 거쳐 들어왔거나 사내 아티팩트 자체이면 프로젝트에서 고치지 않는다 —
그 아티팩트의 담당자가 pom 을 고쳐 새 버전을 내야 반영된다. 어느 목록을 사내로 볼지는 ``remediation.inhouse_groups``
(``mandatory`` / ``optional`` / ``parents``)가 정한다. 비어 있으면 표도 비고 이 규칙은 꺼진다.

In-house artifacts for the remediation: the g:a table from the company artifacts yml (groups named by
`remediation.inhouse_groups`, aliases included) and the check whether a dependency path goes through one of them.
A vulnerable library brought in by an in-house artifact (or that is one) is left to that artifact's owner.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

log = logging.getLogger("sbmig.remediations.inhouse")

GROUPS = ("mandatory", "optional", "parents")
_PARENT_FIELDS = ("parent", "boot_parent", "comp_parent")


def _ga(text: Any) -> str:
    """"g:a" 또는 "g:a:v…" 글자에서 앞의 두 조각("g:a"). 모양이 아니면 ""."""
    parts = [x.strip() for x in str(text or "").split(":")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return ""
    return f"{parts[0]}:{parts[1]}"


def _spec_keys(spec: Any) -> list[str]:
    """아티팩트 하나의 g:a 와 별칭(예전 g:a)들."""
    if spec is None:
        return []
    out = [_ga(f"{getattr(spec, 'group_id', '')}:{getattr(spec, 'artifact_id', '')}")]
    aliases = getattr(spec, "aliases", None) or []
    if isinstance(aliases, (list, tuple)):
        out += [_ga(a) for a in aliases]
    return [k for k in out if k]


def _group_specs(arts: Any, group: str) -> Iterable[Any]:
    if group == "parents":
        return [getattr(arts, f, None) for f in _PARENT_FIELDS]
    got = getattr(arts, group, None) or []
    return list(got) if isinstance(got, (list, tuple)) else []


def inhouse_keys(cfg: Any) -> dict[str, str]:
    """사내 아티팩트 표: "g:a" → 그룹 이름(``mandatory`` / ``optional`` / ``parents``). ``remediation.inhouse_groups`` 에 적힌
    그룹만, 각 아티팩트의 g:a 와 별칭까지. 같은 g:a 가 두 그룹에 있으면 먼저 적힌 그룹. 모르는 그룹 이름은 경고 한 줄로
    건너뛴다. 설정이 없거나 모양이 이상하면 빈 표 — 예외를 내지 않는다(이 안내 때문에 조치가 멈추면 안 된다).

    "g:a" → group label for the groups named in `remediation.inhouse_groups` (aliases included; first group wins).
    Unknown group names are skipped with a warning; a missing / odd configuration gives {} and never raises."""
    try:
        rem = getattr(cfg, "remediation", None)
        arts = getattr(cfg, "artifacts", None)
        groups = getattr(rem, "inhouse_groups", None) if rem is not None else None
        if arts is None or not isinstance(groups, (list, tuple)) or not groups:
            return {}
        out: dict[str, str] = {}
        unknown: list[str] = []
        for g in groups:
            name = str(g or "").strip().lower()
            if name not in GROUPS:
                unknown.append(str(g))
                continue
            for spec in _group_specs(arts, name):
                for key in _spec_keys(spec):
                    out.setdefault(key, name)
        if unknown:
            log.warning("remediation.inhouse_groups: unknown group name(s) %s ignored (allowed: %s)",
                        ", ".join(unknown), ", ".join(GROUPS))
        return out
    except Exception as e:  # noqa: BLE001 - 잘못된 설정은 '사내 아티팩트 없음' 이지 조치 실패가 아니다
        log.warning("remediation in-house artifacts unavailable: %s", type(e).__name__)
        return {}


def inhouse_on_path(dep_ga: str, path: Iterable[str] | None, keys: Mapping[str, str] | None) -> list[str]:
    """의존성 ``dep_ga`` 자신과 그것을 끌고 온 사슬(``ResolvedDep.path`` 의 "g:a:v…" — 앞의 두 조각만 본다) 중 사내 아티팩트인
    g:a 들. 사슬 순서(모듈 쪽 먼저, 자신은 맨 끝), 중복 없음. 표가 비면 빈 목록. 직접 의존성은 사슬이 비어 있으므로 자신이
    사내 아티팩트일 때만 ``[자신]``.

    The in-house g:a's among the elements of `path` and `dep_ga` itself (first two colon parts compared), in path
    order (module side first, the dependency last), unique."""
    if not keys:
        return []
    out: list[str] = []
    for el in [*(path or []), dep_ga]:
        ga = _ga(el)
        if ga and ga in keys and ga not in out:
            out.append(ga)
    return out
