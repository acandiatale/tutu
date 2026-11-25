"""취약한 라이브러리가 사내 아티팩트에서 온 것인지 판단하는 모듈.
회사 artifacts yml(``cfg.artifacts``)로 사내 아티팩트 g:a 목록을 만들고, 의존 경로에 그 아티팩트가 있는지 본다.
그래프의 ``inventory`` 노드가 ``inhouse_keys``로 목록을 만들어 넘기며, 이 목록은 정책 해시에도 들어간다.
- 사내 아티팩트 자체이거나 그것을 거쳐 들어온 라이브러리는 프로젝트에서 고치지 않는다.
  그 아티팩트 담당자가 새 버전을 내야 반영되기 때문이다
- 사내로 볼 그룹은 ``remediation.inhouse_groups``로 정한다. 비어 있으면 이 판단을 하지 않는다
- ``framework`` 그룹은 g:a 대신 ``framework.artifacts``의 패턴으로 판단한다

In-house artifacts for the remediation: the g:a table from the company artifacts yml (groups named by
`remediation.inhouse_groups`, aliases included) and the check whether a dependency path goes through one of
them. Groups: `mandatory` / `optional` / `parents` / `framework`. Group `framework` = the in-house framework
artifacts of `framework.artifacts`: patterns (artifactId, groupId:artifactId, `*` globs), not exact keys.
`inhouse_keys` returns an `InhouseKeys` mapping: the exact keys of the yml groups as dict entries plus the
framework patterns, which its `in` / `get` / `group_of` understand; iterating it yields only the exact keys,
so callers that list or hash the keys see what they saw before (the policy hash adds the patterns separately).
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
    """"g:a"나 "g:a:v…" 문자열에서 앞의 "g:a"만 잘라서 반환한다. 형식이 맞지 않으면 빈 문자열을 반환한다."""
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
    """사내 아티팩트 목록 ``{"g:a": 그룹 이름}``에 프레임워크 패턴을 더한 dict 클래스.
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
        """g:a가 목록에 있으면 그 그룹을, 패턴에 맞으면 ``framework``를, 둘 다 아니면 ``default``를 반환한다."""
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        return FRAMEWORK_GROUP if self._matches(key) else default

    def group_of(self, key: Any) -> str | None:
        """g:a나 g:a:v가 속한 사내 그룹 이름을 반환한다. 사내 아티팩트가 아니면 None을 반환한다."""
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
    """``inhouse_groups``에 ``framework``가 있으면 ``framework.artifacts``의 패턴 목록을 반환한다.
    그룹이 없거나 패턴이 비어 있으면 빈 목록을 반환한다. 예외를 던지지 않는다."""
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
    """사내 아티팩트 목록 ``{"g:a": 그룹 이름}``을 만든다. ``remediation.inhouse_groups``의 그룹만 담고 별칭도 넣는다.
    같은 g:a가 두 그룹에 있으면 먼저 나온 그룹을 쓴다. 모르는 그룹 이름은 경고를 남기고 건너뛴다.
    설정이 없거나 잘못되면 빈 목록을 반환한다. 이 판단 때문에 조치가 멈추면 안 되므로 예외를 던지지 않는다.

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
    """의존성 ``dep_ga``와 그것을 끌어온 경로(``ResolvedDep.path``)에서 사내 아티팩트인 g:a 목록을 반환한다.
    모듈 쪽부터 경로 순서대로 중복 없이 담고, 의존성 자신은 맨 끝에 둔다.

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
