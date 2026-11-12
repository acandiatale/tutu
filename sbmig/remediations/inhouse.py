"""취약한 라이브러리가 사내 아티팩트에서 온 것인지 판단하는 모듈.
회사 artifacts yml(``cfg.artifacts``)에서 사내 라이브러리 g:a 목록을 만들고, 의존 경로에 그 아티팩트가
있는지 확인한다. 그래프의 ``inventory`` 노드가 ``inhouse_keys``로 목록을 만들어
``inventory.build_inventory``에 넘기며, 이 목록은 정책 해시에도 들어간다.
- 취약한 라이브러리가 사내 아티팩트 자체이거나 그것을 통해 들어왔으면 프로젝트에서 고치지 않는다.
  그 아티팩트의 담당자가 pom을 고쳐 새 버전을 내야 반영되기 때문이다
- 사내로 볼 그룹은 ``remediation.inhouse_groups``(``mandatory`` / ``optional`` / ``parents``)로 정하고, 비어
  있으면 이 판단을 하지 않는다

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
    """"g:a" 또는 "g:a:v…" 문자열에서 앞의 두 부분("g:a")만 잘라서 반환한다.
    형식이 맞지 않으면 빈 문자열을 반환한다."""
    parts = [x.strip() for x in str(text or "").split(":")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return ""
    return f"{parts[0]}:{parts[1]}"


def _spec_keys(spec: Any) -> list[str]:
    """아티팩트 하나의 g:a와 별칭(이전 g:a) 목록을 반환한다."""
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
    """사내 아티팩트 목록 ``{"g:a": 그룹 이름}``을 만든다.
    ``remediation.inhouse_groups``에 있는 그룹만 담고 별칭도 포함한다.
    같은 g:a가 두 그룹에 있으면 먼저 나온 그룹을 쓰고, 모르는 그룹 이름은 경고를 남기고 건너뛴다.
    이 판단 때문에 조치가 멈추면 안 되므로 설정이 없거나 잘못되면 빈 목록을 반환하고 예외를 던지지 않는다.

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
    except Exception as e:  # noqa: BLE001 - 잘못된 설정은 사내 아티팩트가 없는 것으로 본다
        log.warning("remediation in-house artifacts unavailable: %s", type(e).__name__)
        return {}


def inhouse_on_path(dep_ga: str, path: Iterable[str] | None, keys: Mapping[str, str] | None) -> list[str]:
    """의존성 ``dep_ga`` 자신과 그것을 끌어온 경로(``ResolvedDep.path``)에서 사내 아티팩트인 g:a 목록을 반환한다.
    경로 순서대로(모듈 쪽 먼저, 자신은 맨 끝) 중복 없이 담는다.
    직접 의존성은 경로가 비어 있으므로 자신이 사내 아티팩트일 때만 ``[자신]``을 반환한다.

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
