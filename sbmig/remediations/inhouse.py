"""취약한 라이브러리가 사내 아티팩트에서 온 것인지 판단하는 모듈.
회사 artifacts yml(``cfg.artifacts``)에서 사내 라이브러리 g:a 목록을 만들고, 의존 경로에 그 아티팩트가
있는지 확인한다. 그래프의 ``inventory`` 노드가 ``inhouse_keys``로 목록을 만들어
``inventory.build_inventory``에 넘기며, 이 목록은 정책 해시에도 들어간다.
- 취약한 라이브러리가 사내 아티팩트 자체이거나 그것을 통해 들어왔으면 프로젝트에서 고치지 않는다.
  그 아티팩트의 담당자가 pom을 고쳐 새 버전을 내야 반영되기 때문이다
- 사내로 볼 그룹은 ``remediation.inhouse_groups``(``mandatory`` / ``optional`` / ``parents`` / ``framework``)로
  정하고, 비어 있으면 이 판단을 하지 않는다
- ``framework`` 그룹은 ``framework.artifacts``의 패턴으로 판단하므로 목록에 g:a가 들어가지 않는다

In-house artifacts for the remediation: the g:a table from the company artifacts yml (groups named by
`remediation.inhouse_groups`, aliases included) and the check whether a dependency path goes through one of them.
A vulnerable library brought in by an in-house artifact (or that is one) is left to that artifact's owner.
Group `framework` = the in-house framework artifacts of `framework.artifacts`: patterns (artifactId, groupId:artifactId,
`*` globs), not exact keys. `inhouse_keys` returns an `InhouseKeys` mapping: the exact keys of the yml groups as dict
entries plus the framework patterns, which its `in` / `get` / `group_of` understand; iterating it yields only the exact
keys, so callers that list or hash the keys see what they saw before (the policy hash adds the patterns separately).
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

log = logging.getLogger("sbmig.remediations.inhouse")

GROUPS = ("mandatory", "optional", "parents", "framework")
FRAMEWORK_GROUP = "framework"
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


class InhouseKeys(dict):
    """사내 아티팩트 목록 ``{"g:a": 그룹 이름}``에 프레임워크 패턴을 더한 클래스.
    ``in``, ``get``, ``group_of``는 목록에 없는 g:a도 패턴에 맞으면 ``framework`` 그룹으로 본다.

    A `{"g:a": group}` dict plus the framework patterns. Membership / `get` / `group_of` fall back to the patterns
    (group `framework`) for a g:a the exact keys do not hold; iteration, `len`, `items` and equality are the dict's,
    so a table without patterns behaves exactly like the plain dict it replaces. Truthy when it holds keys or patterns."""

    def __init__(self, keys: Mapping[str, str] | None = None, patterns: Iterable[str] = ()):
        super().__init__(keys or {})
        self.patterns: tuple[str, ...] = tuple(dict.fromkeys(str(p).strip() for p in patterns if str(p or "").strip()))
        self._matcher = _matcher(self.patterns)

    def _matches(self, key: Any) -> bool:
        if self._matcher is None:
            return False
        ga = _ga(key)
        if not ga:
            return False
        g, a = ga.split(":", 1)
        try:
            return bool(self._matcher.matches(g, a))
        except Exception:  # noqa: BLE001 - 패턴을 비교하지 못하면 사내 아티팩트가 아닌 것으로 본다
            return False

    def __bool__(self) -> bool:
        return bool(len(self)) or bool(self.patterns)

    def __contains__(self, key: object) -> bool:
        return dict.__contains__(self, key) or self._matches(key)

    def __getitem__(self, key: Any) -> str:
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        if self._matches(key):
            return FRAMEWORK_GROUP
        raise KeyError(key)

    def get(self, key: Any, default: Any = None) -> Any:
        """정확한 g:a의 그룹을, 없으면 패턴에 맞을 때 ``framework``를 반환한다."""
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        return FRAMEWORK_GROUP if self._matches(key) else default

    def group_of(self, key: Any) -> str | None:
        """g:a(또는 g:a:v)가 속한 사내 그룹 이름을 반환한다. 사내 아티팩트가 아니면 None을 반환한다."""
        return self.get(_ga(key) or key)


def _matcher(patterns: tuple[str, ...]) -> Any:
    """패턴 비교에 쓸 ``FrameworkConfig``를 만든다. 패턴이 없거나 만들 수 없으면 None을 반환한다."""
    if not patterns:
        return None
    try:
        from sbmig.core.config import FrameworkConfig
        return FrameworkConfig(artifacts=list(patterns))
    except Exception:  # noqa: BLE001 - 패턴을 읽지 못하면 사내 아티팩트가 아닌 것으로 본다
        return None


def framework_patterns(cfg: Any) -> list[str]:
    """``inhouse_groups``에 ``framework``가 있을 때 ``framework.artifacts``의 패턴 목록을 반환한다.
    그룹이 없거나 패턴이 비어 있으면 빈 목록을 반환하고 예외를 던지지 않는다.

    The framework patterns when `framework` is listed in `remediation.inhouse_groups`; [] otherwise (never raises)."""
    try:
        rem = getattr(cfg, "remediation", None)
        groups = getattr(rem, "inhouse_groups", None) if rem is not None else None
        if not isinstance(groups, (list, tuple)):
            return []
        if FRAMEWORK_GROUP not in {str(g or "").strip().lower() for g in groups}:
            return []
        fw = getattr(cfg, "framework", None)
        pats = fw.patterns() if fw is not None and hasattr(fw, "patterns") else []
        return [p for p in dict.fromkeys(str(x).strip() for x in pats) if p] if isinstance(pats, (list, tuple)) else []
    except Exception as e:  # noqa: BLE001 - 잘못된 설정은 프레임워크 패턴이 없는 것으로 본다
        log.warning("remediation framework patterns unavailable: %s", type(e).__name__)
        return []


def inhouse_keys(cfg: Any) -> InhouseKeys:
    """사내 아티팩트 목록 ``{"g:a": 그룹 이름}``을 만든다.
    ``remediation.inhouse_groups``에 있는 그룹만 담고 별칭도 포함한다.
    같은 g:a가 두 그룹에 있으면 먼저 나온 그룹을 쓰고, 모르는 그룹 이름은 경고를 남기고 건너뛴다.
    이 판단 때문에 조치가 멈추면 안 되므로 설정이 없거나 잘못되면 빈 목록을 반환하고 예외를 던지지 않는다.

    ``framework`` 그룹은 g:a를 담지 않고 ``framework.artifacts`` 패턴으로 판단한다(``InhouseKeys``).

    "g:a" → group label for the groups named in `remediation.inhouse_groups` (aliases included; first group wins).
    Group `framework` adds the `framework.artifacts` patterns instead of keys (an exact key of another group wins over
    a pattern). Unknown group names are skipped with a warning; a missing / odd configuration gives an empty table and
    never raises."""
    try:
        rem = getattr(cfg, "remediation", None)
        arts = getattr(cfg, "artifacts", None)
        groups = getattr(rem, "inhouse_groups", None) if rem is not None else None
        if not isinstance(groups, (list, tuple)) or not groups:
            return InhouseKeys()
        out: dict[str, str] = {}
        unknown: list[str] = []
        for g in groups:
            name = str(g or "").strip().lower()
            if name not in GROUPS:
                unknown.append(str(g))
                continue
            if name == FRAMEWORK_GROUP or arts is None:
                continue
            for spec in _group_specs(arts, name):
                for key in _spec_keys(spec):
                    out.setdefault(key, name)
        if unknown:
            log.warning("remediation.inhouse_groups: unknown group name(s) %s ignored (allowed: %s)",
                        ", ".join(unknown), ", ".join(GROUPS))
        return InhouseKeys(out, framework_patterns(cfg))
    except Exception as e:  # noqa: BLE001 - 잘못된 설정은 사내 아티팩트가 없는 것으로 본다
        log.warning("remediation in-house artifacts unavailable: %s", type(e).__name__)
        return InhouseKeys()


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
