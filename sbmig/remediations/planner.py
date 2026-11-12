"""취약점 조치에서 버전 변경 제안과 조치하지 못한 항목을 만드는 계획 모듈.
인벤토리를 받아 제안을 만들고 승인 필요 여부와 해시를 붙인다.
순서는 inventory -> plan -> 승인 대기 -> apply이고, 진입점은 ``plan_round``, ``policy_hash``,
``validate_decision``, ``RepositoryVersionSource``다.
- 새 버전은 ``policy.pins``에 있거나 승인된 저장소에 실제로 있는 버전에서만 고른다.
  IVAS 패치 버전은 힌트일 뿐 근거가 아니다
- 같은 메이저에서 가장 낮은 버전을 먼저 고르고, 없으면 ``allow_major``일 때만 더 높은 메이저를 고른다
- 부모 / BOM 버전은 서버가 그 부모 / BOM을 ``pins``로 지정했을 때만 바꾼다
- 간접 의존성과 pin이 없는 부모 / BOM 관리 버전은 루트 pom의 ``<dependencyManagement>``에 override 항목을
  넣는다. 이 제안은 설정과 관계없이 항상 승인을 기다린다
- 사내 아티팩트 자체와, 모든 모듈에서 사내 아티팩트를 거쳐 들어온 라이브러리는 제안하지
  않는다(``inhouse_artifact``)
- 승인은 제안 해시, 원본 트리 해시, 정책 해시, diff가 모두 같을 때만 받아들인다

Planning for the remediation feature: proposals + residuals from the inventory, approval classes, and the hashes that
bind a proposal to the current tree and policy. A target version comes only from `policy.pins` or a version the approved
repository lists (IVAS patched versions are hints, never evidence). A parent / BOM version text changes only when the
server pins that parent / BOM. Transitive items and parent / BOM managed versions without such a pin get a
managed-version override entry in the root pom (user decision 2026-11-11); such a proposal ALWAYS waits for approval,
whatever `require_approval` / `always_approve` say — inserting XML is never automatic. A library that is an in-house
artifact, or that every one of its modules gets transitively / managed through an in-house artifact, is an
`inhouse_artifact` residual (its owner publishes the fix).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sbmig.build.repo_versions import is_prerelease, parse_version

from . import apply as _apply
from . import inventory as _inv
from .inhouse import inhouse_keys
from .inventory import split_patched_versions as split_hints
from .models import (
    D_APPROVED,
    D_AUTO,
    D_PENDING,
    D_REJECTED,
    E_INSERT,
    K_BOM_VERSION,
    K_DIRECT_VERSION,
    K_MANAGED_OVERRIDE,
    K_OWN_MANAGEMENT_VERSION,
    K_PARENT_VERSION,
    K_PROPERTY_VERSION,
    MANUAL_ORIGINS,
    O_BOM_MANAGED,
    O_DIRECT_LITERAL,
    O_DIRECT_PROPERTY,
    O_OWN_MANAGEMENT,
    O_PARENT_MANAGED,
    O_TRANSITIVE,
    R_INHOUSE_ARTIFACT,
    R_INVENTORY_INCOMPLETE,
    R_MANUAL_REVIEW,
    R_NO_CANDIDATE_VERSION,
    R_POLICY_DENIED,
    R_REJECTED_PROPOSALS,
    R_STALE_LOCATION,
    R_UNKNOWN_PROVENANCE,
    RISK_BOM,
    RISK_MAJOR,
    RISK_MANAGED_OVERRIDE,
    RISK_PARENT,
    ApprovalMismatch,
    Decision,
    Inventory,
    InventoryItem,
    PomLocation,
    Proposal,
    Residual,
    RoundPlan,
    TextEdit,
    canonical_json,
    sha256_text,
)

log = logging.getLogger("sbmig.remediations.planner")

# ``planner.R_…``로 import하는 코드가 있으므로 R_POLICY_DENIED 등 models의 사유 코드를 다시 내보낸다
EVIDENCE_PINS = "policy:pins"
# override 제안의 note에 항상 넣는 문장이다.
# 넣은 항목은 이후 부모, BOM, 직접 의존성을 올려도 바뀌지 않는다는 경고다
OVERRIDE_WARNING = ("later upgrades of the parent / BOM / direct dependency will NOT move this version any more — "
                    "the owners must remove the override when the parent / BOM / direct dependency catches up")
OVERRIDE_APPROVAL = "always needs approval: an inserted <dependencyManagement> entry is never applied automatically"
_LIST_CAP = 20                       # 설명이 너무 길어지지 않도록 나열하는 버전 수를 제한한다


# ---------------------------------------------------------------------------------------------------- 정책 해시
def _rem(cfg: Any) -> Any:
    return cfg.remediation


def _policy_dict(policy: Any) -> dict[str, Any]:
    if hasattr(policy, "model_dump"):
        return policy.model_dump()
    return dict(vars(policy))


def policy_hash(cfg: Any) -> str:
    """서버 정책의 해시를 계산한다. ``remediation.policy``, ``require_approval``, ``always_approve``,
    ``failed_candidate``, ``ignore_actions``, 사내 아티팩트 설정을 포함한다.
    제안 내용이나 승인 대상을 바꾸는 값이 하나라도 바뀌면 해시가 바뀌고, 이전의 승인 대기 제안은 무효가 된다.

    Hash of everything that changes what is proposed or what needs approval (incl. `ignore_actions` and the sorted
    in-house g:a keys)."""
    from .ivas import ignore_set
    rem = _rem(cfg)
    return sha256_text(canonical_json({
        "policy": _policy_dict(rem.policy),
        "require_approval": bool(rem.require_approval),
        "always_approve": sorted(str(x) for x in (rem.always_approve or [])),
        "failed_candidate": str(rem.failed_candidate),
        "ignore_actions": sorted(ignore_set(getattr(rem, "ignore_actions", None))),
        "inhouse": sorted(inhouse_keys(cfg)),
    }))


# ---------------------------------------------------------------------------------------------------- 버전 출처
class RepositoryVersionSource:
    """저장소에서 버전 목록과 버전의 근거를 조회하는 클래스(``RepositoryLookup``을 감싼
    ``interfaces.VersionSource`` 구현).
    근거는 버전 목록을 준 URL의 호스트("repository:<host>")이고, 그 호스트가 승인된 저장소일 때만 인정한다.
    로컬 저장소에만 있는 버전은 근거가 되지 않는다.
    승인된 호스트는 ``allowed_hosts``, ``remediation.allowed_repository_hosts``, 서버 settings.xml의 호스트
    순으로 정한다. 테스트는 ``lookup``에 가짜 구현을 넣는다.

    VersionSource over `RepositoryLookup`; evidence names only the host of the URL that listed the versions, and only
    when that host is an approved repository host. A version known only from the local repository is no evidence."""

    def __init__(self, cfg: Any, *, project_root: Path | None = None, lookup: Any = None,
                 allowed_hosts: Any = None):
        self.cfg = cfg
        self.project_root = project_root
        self._lookup = lookup
        self._allowed_src = allowed_hosts
        self._allowed: set[str] | None = None

    @property
    def lookup(self) -> Any:
        """저장소 조회 객체를 처음 쓸 때 만들어 반환한다.
        테스트가 오프라인용 가짜로 바꿔 넣을 수 있도록 모듈 속성으로 찾는다."""
        if self._lookup is None:
            from sbmig.build import repo_versions as rv
            self._lookup = rv.RepositoryLookup(self.cfg, project_root=self.project_root)
        return self._lookup

    def allowed_hosts(self) -> set[str]:
        """승인된 저장소 호스트(소문자)를 반환한다.
        정할 수 없으면 빈 집합을 반환하므로 어떤 저장소도 근거가 되지 않는다.

        The approved repository hosts (lower case); empty when they cannot be determined."""
        if self._allowed is None:
            src = self._allowed_src
            hosts: list[str] = []
            try:
                if callable(src):
                    hosts = list(src() or [])
                elif src is not None:
                    hosts = list(src)
                elif self.cfg is not None and getattr(self.cfg, "remediation", None) is not None:
                    from .maven import ServerMaven
                    hosts = list(ServerMaven(self.cfg).allowed_hosts())
            except Exception as e:  # noqa: BLE001 - unknown approved hosts = no evidence, never a crash
                log.warning("remediation allowed hosts unavailable: %s", type(e).__name__)
                hosts = []
            self._allowed = {str(h).strip().lower() for h in hosts if str(h).strip()}
        return self._allowed

    def versions(self, group_id: str, artifact_id: str) -> list[str]:
        """저장소에 있는 버전 목록을 중복 없이 ``parse_version`` 오름차순으로 반환한다.
        조회에 실패하면 빈 목록을 반환한다.

        Versions the repository lists, ascending."""
        try:
            got = list(self.lookup.versions(group_id, artifact_id) or [])
        except Exception as e:  # noqa: BLE001 - an unreadable repository is "no evidence", never a crash
            log.warning("remediation versions(%s:%s): %s", group_id, artifact_id, type(e).__name__)
            return []
        return sorted(dict.fromkeys(v for v in got if v), key=parse_version)

    def evidence(self, group_id: str, artifact_id: str, version: str) -> str:
        """``version``의 근거 "repository:<host>"를 반환한다.
        승인된 호스트에 그 버전이 있을 때만 반환하고, 아니면 빈 문자열을 반환한다.

        Evidence for `version`; "" unless an approved host listed it."""
        if version not in self.versions(group_id, artifact_id):
            return ""
        answered = getattr(self.lookup, "_answered", {}) or {}
        url = answered.get((group_id, artifact_id))
        if not url:
            return ""          # 로컬 저장소나 출처를 모르는 목록은 근거로 인정하지 않는다
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
        if not host:
            return ""
        if host not in self.allowed_hosts():
            log.warning("remediation: %s:%s:%s listed by an unapproved repository host %s — no evidence",
                        group_id, artifact_id, version, host)
            return ""
        return f"repository:{host}"


# ---------------------------------------------------------------------------------------------------- 도우미
def _major(v: str) -> int | None:
    first = parse_version(v)[0]
    return first[1] if first[0] == 2 else None


def _newer(a: str, b: str) -> bool:
    return parse_version(a) > parse_version(b)


def _ga_parts(ga: str) -> tuple[str, str]:
    g, _, a = ga.partition(":")
    return g, a


def _denied(ga: str, deny: list[str]) -> bool:
    g, _a = _ga_parts(ga)
    return any(d == ga or d == f"{g}:*" for d in deny or [])


def _cap(vs: list[str]) -> str:
    if not vs:
        return "nothing"
    head = ", ".join(vs[:_LIST_CAP])
    return head + (f" (+{len(vs) - _LIST_CAP} more)" if len(vs) > _LIST_CAP else "")


def _read_line(project: Path, loc: PomLocation) -> str | None:
    """pom 위치가 가리키는 줄의 내용을 읽는다.
    apply와 같은 인코딩 규칙으로 읽고, 줄이 없거나 읽지 못하면 None을 반환한다."""
    try:
        path = _apply._safe_path(project, loc.pom)
        doc = _apply._PomText(path.read_bytes(), loc.pom)
    except (_apply.EditError, OSError):
        return None
    lines = _apply._split_lines(doc.text)
    if not isinstance(loc.line, int) or loc.line < 1 or loc.line > len(lines):
        return None
    return lines[loc.line - 1]


def _stale(project: Path, loc: PomLocation) -> str:
    """그 줄에 ``loc.text``가 정확히 한 번 있지 않으면 위치가 낡은 것으로 보고 이유 한 줄을 반환한다.
    문제가 없으면 빈 문자열을 반환한다."""
    line = _read_line(project, loc)
    if line is None:
        return f"{loc.pom}:{loc.line} no longer exists or cannot be read"
    count = line.count(loc.text) if loc.text else 0
    if count != 1:
        return f"{loc.pom}:{loc.line} contains {loc.text!r} {count} time(s), expected exactly once (file changed?)"
    return ""


@dataclass
class _Change:
    """제안이 되기 전의 변경 하나를 담는 클래스. 같은 줄을 고치는 변경끼리 합친 뒤 Proposal로 만든다."""
    ga: str
    from_version: str
    to_version: str
    kind: str
    edit: TextEdit
    evidence: str
    finding_ids: list[str]
    risks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    origin: str = ""
    modules: list[str] = field(default_factory=list)          # 이 값이 버전을 정하는 모듈. 비어 있으면 from 버전이던 모든 모듈
    also_modules: list[str] = field(default_factory=list)     # 같은 버전이 다른 경로로 해석된 모듈
    # 이 변경이 조치하지 못한 항목이 될 때 보고서에 보여 줄 경로와 사내 아티팩트다. plan_round가 채운다
    via: list[str] = field(default_factory=list)
    inhouse: list[str] = field(default_factory=list)


def _scope(item: InventoryItem) -> tuple[list[str], list[str]]:
    """항목의 (버전을 정하는 모듈, also 모듈)을 반환한다.
    버전을 정하는 모듈은 modules에서 also_modules를 뺀 것이다."""
    also = sorted(set(item.also_modules or []))
    return sorted(set(item.modules or []) - set(also)), also


def _residual(item: InventoryItem, reason: str, detail: str) -> Residual:
    return Residual(ga=item.ga, version=item.version, origin=item.origin, reason=reason, detail=detail,
                    finding_ids=list(item.finding_ids), modules=sorted(set(item.modules or [])),
                    via=list(item.via or []), inhouse=sorted(set(item.inhouse or [])))


def _change_residual(c: _Change, reason: str, detail: str) -> Residual:
    """제안이 되지 못한 변경을 조치하지 못한 항목으로 바꾼다."""
    return Residual(ga=c.ga, version=c.from_version, origin=c.origin, reason=reason, detail=detail,
                    finding_ids=list(c.finding_ids), modules=sorted(set(c.modules) | set(c.also_modules)),
                    via=list(c.via), inhouse=sorted(set(c.inhouse)))


def _inhouse_residual(item: InventoryItem) -> Residual | None:
    """항목 자체가 사내 아티팩트이거나, 간접 / 관리 항목이 모든 모듈에서 사내 아티팩트를 거쳐 들어왔으면
    ``inhouse_artifact`` 항목을 반환한다.
    일부 모듈만 그렇다면 None을 반환하고 평소처럼 계획한다.

    The in-house rule: the item IS an in-house artifact, or a transitive / managed item reaches every module through one."""
    if not item.inhouse:
        return None
    if item.ga in item.inhouse:
        return _residual(item, R_INHOUSE_ARTIFACT, f"{item.ga} is an in-house artifact: its owner publishes the fixed version")
    if (item.origin in (O_TRANSITIVE, O_PARENT_MANAGED, O_BOM_MANAGED) and item.modules
            and set(item.modules) <= set(item.inhouse_modules or [])):
        return _residual(item, R_INHOUSE_ARTIFACT, f"brought in by the in-house artifact(s) {', '.join(item.inhouse)}: "
                                                   "only their owner can change it")
    return None


_LINE_NUMS = re.compile(r"\d+")


def _line(v: str) -> tuple[int, int]:
    """버전의 major.minor를 숫자 쌍으로 반환한다. 숫자가 없는 자리는 0이다.
    예: "2.12.7.1"은 (2, 12), "3"은 (3, 0)."""
    head = re.match(r"\d+(?:\.\d+)*", (v or "").strip())
    nums = [int(x) for x in _LINE_NUMS.findall(head.group(0))] if head else []
    nums += [0, 0]
    return nums[0], nums[1]


def _fixes(v: str, alternatives: list[str]) -> bool:
    """``v``가 발견 항목 하나의 패치 버전 묶음을 만족하는지 확인한다.
    가장 높은 대안 이상이거나, 다른 대안과 major.minor가 같으면서 그 대안 이상이면 만족한다."""
    top = max(alternatives, key=parse_version)
    pv = parse_version(v)
    for h in alternatives:
        if pv >= parse_version(h) and (h == top or _line(v) == _line(h)):
            return True
    return False


def _choose_target(item: InventoryItem, policy: Any, source: Any, current: str) -> tuple[str, str, str, list[str]]:
    """새 버전을 고르고 (버전, 근거, 사유 코드, 메모)를 반환한다.
    버전이 빈 문자열이면 고르지 못한 것이고 사유와 설명이 메모에 있다.
    1. ``policy.pins``에 있으면 그 버전을 쓴다
    2. 없으면 승인된 저장소의 버전 중 IVAS 힌트를 모두 만족하는 가장 낮은 버전을 고른다.
      같은 메이저를 먼저 찾고, ``allow_major``일 때만 더 높은 메이저를 찾는다
    발견 항목끼리는 모두 만족해야 하고(AND), 한 항목 안의 여러 버전은 major.minor 라인별 대안이다(OR). 예를
    들어 현재 2.12.3에 힌트가 "2.12.7.1, 2.13.4.2"면 2.12.7.1 이상인 2.12.x 중 가장 낮은 버전을, 없으면
    2.13.4.2 이상에서 고른다. 범위처럼 버전이 아닌 힌트가 있으면 고르지 않는다(``no_candidate_version``).

    Choose the target: a policy pin, else the lowest repository version (same major first) satisfying every finding's
    hint. Different findings AND together; several versions inside one finding's hint are per-line alternatives (OR):
    the highest alternative or above, or ≥ another alternative on that alternative's major.minor line. A hint that is
    not a plain version (a range) makes the item a `no_candidate_version` residual naming the raw hint."""
    g, a = _ga_parts(item.ga)
    deny_versions = set((policy.deny_versions or {}).get(item.ga, []) or [])
    pin = (policy.pins or {}).get(item.ga)
    groups = [alts for alts in (split_hints(h) for h in item.patched_versions or []) if alts]
    if pin:
        if not _newer(pin, current):
            return "", "", R_NO_CANDIDATE_VERSION, [f"policy pin {pin} is not newer than the current {current}"]
        if pin in deny_versions:
            return "", "", R_NO_CANDIDATE_VERSION, [f"policy pin {pin} is also listed in policy.deny_versions"]
        if _major(pin) != _major(current) and not policy.allow_major:
            # allow_major가 false면 서버 pin이라도 메이저 변경은 제안하지 않는다
            return "", "", R_NO_CANDIDATE_VERSION, [f"policy pin {pin} is a major change and policy.allow_major is false"]
        notes = []
        unmet = [", ".join(alts) for alts in groups
                 if all(_apply.is_version_text(h) for h in alts) and not _fixes(pin, alts)]
        if unmet:
            notes.append(f"policy pin {pin} is below the IVAS patched hint {'; '.join(unmet)}")
        return pin, EVIDENCE_PINS, "", notes
    if not groups:
        return "", "", R_NO_CANDIDATE_VERSION, ["no patched version known (IVAS gave none) and no policy pin"]
    bad = [h for alts in groups for h in alts if not _apply.is_version_text(h)]
    if bad:
        # 범위나 문장을 버전으로 읽으면 엉뚱한 버전을 고르므로 사람이 확인하도록 남긴다
        return "", "", R_NO_CANDIDATE_VERSION, [
            "IVAS patched version hint(s) " + ", ".join(repr(h) for h in bad) + " are not plain versions; "
            "not chosen automatically"]
    listed = list(source.versions(g, a))
    excluded: list[str] = []
    cands: list[str] = []
    shown = "; ".join(", ".join(alts) for alts in groups)
    for v in listed:
        if not _newer(v, current):
            continue                                     # ≤ current: not an upgrade (not worth listing as excluded)
        if v in deny_versions:
            excluded.append(f"{v} (deny_versions)")
        elif is_prerelease(v) and not policy.allow_prerelease:
            excluded.append(f"{v} (prerelease)")
        elif not all(_fixes(v, alts) for alts in groups):
            excluded.append(f"{v} (< patched hint {shown})")
        else:
            cands.append(v)
    cands.sort(key=parse_version)
    cur_major = _major(current)
    same = [v for v in cands if _major(v) == cur_major]
    if same:
        target = same[0]
    else:
        higher = [v for v in cands if (_major(v) or 0) > (cur_major or 0)]
        if higher and not policy.allow_major:
            return "", "", R_NO_CANDIDATE_VERSION, [
                f"only major upgrades are available ({_cap(higher)}) and policy.allow_major is false"]
        if not higher:
            facts = (f"repository listed {_cap(listed)}; excluded: {_cap(excluded)}; "
                     f"needed > {current} and ≥ patched hint {shown}")
            missing = [h for alts in groups for h in alts if h not in listed]
            if missing:
                facts += f"; the hinted {', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not in the repository"
            return "", "", R_NO_CANDIDATE_VERSION, [facts]
        target = higher[0]
    evidence = source.evidence(g, a, target)
    if not evidence:
        return "", "", R_NO_CANDIDATE_VERSION, [f"the repository gave no evidence for {target}"]
    return target, evidence, "", []


def _change_for(project: Path, item: InventoryItem, policy: Any, source: Any, poms: _Poms, *,
                parents: frozenset[str] = frozenset()) -> _Change | Residual:
    """인벤토리 항목 하나를 변경 또는 조치하지 못한 항목으로 바꾼다."""
    if item.origin in MANUAL_ORIGINS:
        return _residual(item, R_MANUAL_REVIEW, item.reason or f"{item.origin}: not changed automatically")
    if _denied(item.ga, policy.deny):
        return _residual(item, R_POLICY_DENIED, f"{item.ga} matches policy.deny")
    # 사내 아티팩트의 pom은 담당자만 고칠 수 있고, 프로젝트에 override를
    # 넣으면 담당자의 수정과 어긋나므로 제안하지 않는다
    inhouse = _inhouse_residual(item)
    if inhouse is not None:
        if (inhouse.reason == R_INHOUSE_ARTIFACT and item.ga not in item.inhouse
                and item.origin in (O_PARENT_MANAGED, O_BOM_MANAGED) and set(item.inhouse) <= parents):
            # 회사 부모가 관리하는 버전이라도 서버 정책이 그 부모를 pin했으면 부모 버전 제안을 낸다
            # pin이 없을 때만 사내 아티팩트 항목으로 남긴다
            got = _managed_change(project, item, policy)
            if got is not None:
                return got
        return inhouse
    if item.origin in (O_PARENT_MANAGED, O_BOM_MANAGED):
        got = _managed_change(project, item, policy)
        return got if got is not None else _override_change(project, item, policy, source, poms)
    if item.origin == O_TRANSITIVE:
        return _override_change(project, item, policy, source, poms)
    if item.origin not in (O_DIRECT_LITERAL, O_DIRECT_PROPERTY, O_OWN_MANAGEMENT):
        return _residual(item, R_MANUAL_REVIEW, f"unknown origin {item.origin!r}")
    loc = item.location
    if loc is None:
        return _residual(item, R_MANUAL_REVIEW, "no version location in the project poms")
    if not _apply.is_version_text(loc.text):
        return _residual(item, R_MANUAL_REVIEW, f"{loc.pom}:{loc.line} version text {loc.text!r} is not a plain version")
    stale = _stale(project, loc)
    if stale:
        return _residual(item, R_STALE_LOCATION, stale)
    current = item.version or loc.text
    target, evidence, reason, notes = _choose_target(item, policy, source, current)
    if not target:
        return _residual(item, reason, "; ".join(notes))
    if item.origin == O_DIRECT_LITERAL:
        kind = K_DIRECT_VERSION
    elif item.origin == O_DIRECT_PROPERTY or loc.property:
        kind = K_PROPERTY_VERSION
    else:
        kind = K_OWN_MANAGEMENT_VERSION
    if kind == K_PROPERTY_VERSION:
        prop = loc.property or "?"
        notes.append(f"property ${{{prop}}} may govern other artifacts")
    governed, also = _scope(item)
    if also:
        # 같은 g:a:version이 다른 경로로 해석된 모듈에는 이 수정이 적용되지 않을 수 있다.
        # 검증은 실패로 치지 않고 다음 라운드에서 다시 계획한다
        notes.append(f"also resolved in {', '.join(also)} through another origin; this edit may not govern them — "
                     f"whatever stays at {current} is planned in the next round")
    risks = [RISK_MAJOR] if _major(target) != _major(current) else []
    return _Change(ga=item.ga, from_version=current, to_version=target, kind=kind,
                   edit=TextEdit(pom=loc.pom, line=loc.line, old=loc.text, new=target), evidence=evidence,
                   finding_ids=list(item.finding_ids), risks=risks, notes=notes, origin=item.origin,
                   modules=governed, also_modules=also)


def _managed_change(project: Path, item: InventoryItem, policy: Any) -> _Change | Residual | None:
    """부모 / BOM이 관리하는 항목을 처리한다.
    서버가 그 부모 / BOM을 pin했으면 버전 변경 제안이나 조치하지 못한 항목을 반환한다.
    pin이 없으면 None을 반환하고 호출한 쪽이 override로 처리한다.
    어느 부모 버전이 취약점을 고치는지는 서버 pin 말고는 근거가 없기 때문이다."""
    kind_word = "parent" if item.origin == O_PARENT_MANAGED else "BOM"
    mloc = item.manager_location
    parts = item.manager.split(":") if item.manager else []
    if len(parts) < 2:
        return None
    mga = f"{parts[0]}:{parts[1]}"
    pin = (policy.pins or {}).get(mga)
    if not pin:
        return None
    if mloc is None:
        return _residual(item, R_UNKNOWN_PROVENANCE,
                         f"policy pins the {kind_word} {mga} at {pin}, but its version text is not in the project poms")
    if _denied(mga, policy.deny):
        return _residual(item, R_POLICY_DENIED, f"the {kind_word} {mga} matches policy.deny")
    if not _apply.is_version_text(mloc.text):
        return _residual(item, R_MANUAL_REVIEW, f"{mloc.pom}:{mloc.line} {kind_word} version {mloc.text!r} is not plain")
    stale = _stale(project, mloc)
    if stale:
        return _residual(item, R_STALE_LOCATION, stale)
    current = parts[2] if len(parts) > 2 and parts[2] else mloc.text
    if not _newer(pin, current):
        return _residual(item, R_NO_CANDIDATE_VERSION, f"policy pin {pin} for {mga} is not newer than {current}")
    if _major(pin) != _major(current) and not policy.allow_major:
        return _residual(item, R_NO_CANDIDATE_VERSION,
                         f"policy pin {pin} for {mga} is a major change and policy.allow_major is false")
    kind = K_PARENT_VERSION if item.origin == O_PARENT_MANAGED else K_BOM_VERSION
    risks = [RISK_PARENT if kind == K_PARENT_VERSION else RISK_BOM]
    if _major(pin) != _major(current):
        risks.append(RISK_MAJOR)
    return _Change(ga=mga, from_version=current, to_version=pin, kind=kind,
                   edit=TextEdit(pom=mloc.pom, line=mloc.line, old=mloc.text, new=pin), evidence=EVIDENCE_PINS,
                   finding_ids=list(item.finding_ids), risks=risks, origin=item.origin,
                   notes=[(f"raises the {kind_word} {mga} {current} → {pin} (server pin) for {item.ga}:{item.version}; "
                           "the versions it manages may change too")])


class _Poms:
    """루트 pom과 reactor의 lxml 모델을 처음 필요할 때 한 번만 읽어 두는 클래스."""

    def __init__(self, project: Path):
        self.project = project
        self._pp: Any = None

    @property
    def pp(self) -> Any:
        if self._pp is None:
            self._pp = _inv.ProjectPoms(self.project)
        return self._pp


def _root_entries(pp: Any, ga: str) -> list[tuple[Any, str]]:
    """루트 pom의 ``<dependencyManagement>``에서 이 g:a의 항목 중 import가 아닌 것을 찾는다.
    각 항목의 위치는 최상위면 빈 문자열, 프로필이면 "profile <id>"다."""
    root = pp.poms.get(".")
    out = []
    for dep, where in _inv._deps(root, management=True):
        if _inv._text(_inv._kid(dep, "scope")) == "import":
            continue
        g = pp.interp(root, _inv._text(_inv._kid(dep, "groupId")))
        a = pp.interp(root, _inv._text(_inv._kid(dep, "artifactId")))
        if f"{g}:{a}" == ga:
            out.append((dep, where))
    return out


def _existing_entry_change(project: Path, item: InventoryItem, policy: Any, source: Any, pp: Any,
                           entries: list[tuple[Any, str]]) -> _Change | Residual:
    """루트 pom이 이미 이 g:a를 관리할 때 항목을 하나 더 넣지 않고 기존 항목으로 처리한다.
    최상위 항목의 버전 값이 해석된 버전과 같으면 그 값을 바꾸는 제안을, 아니면 조치하지 못한 항목을 반환한다."""
    root = pp.poms.get(".")
    dep, where = entries[0]
    if where or len(entries) > 1:
        places = ", ".join(w or "top level" for _d, w in entries)
        return _residual(item, R_MANUAL_REVIEW, f"the root pom.xml already has <dependencyManagement> entries for "
                                                f"{item.ga} ({places}); a second entry is never added")
    vel = _inv._kid(dep, "version")
    ref = pp.resolve_version(root, _inv._text(vel))
    loc = None
    if ref.kind == "literal":
        loc = _inv.location_of(root, vel, "management")
    elif ref.kind == "property" and ref.pom is not None:
        loc = _inv.location_of(ref.pom, ref.el, "property", ref.prop)
    if loc is None or ref.value != item.version or not _apply.is_version_text(loc.text):
        return _residual(item, R_MANUAL_REVIEW,
                         f"the root pom.xml already has a <dependencyManagement> entry for {item.ga} whose version "
                         f"({_inv._text(vel) or 'none'}) is not the resolved {item.version} as plain text; "
                         "a second entry is never added")
    stale = _stale(project, loc)
    if stale:
        return _residual(item, R_STALE_LOCATION, stale)
    target, evidence, reason, notes = _choose_target(item, policy, source, item.version)
    if not target:
        return _residual(item, reason, "; ".join(notes))
    kind = K_PROPERTY_VERSION if loc.property else K_OWN_MANAGEMENT_VERSION
    if loc.property:
        notes.append(f"property ${{{loc.property}}} may govern other artifacts")
    notes.append("uses the existing <dependencyManagement> entry of the root pom.xml")
    risks = [RISK_MAJOR] if _major(target) != _major(item.version) else []
    return _Change(ga=item.ga, from_version=item.version, to_version=target, kind=kind,
                   edit=TextEdit(pom=loc.pom, line=loc.line, old=loc.text, new=target), evidence=evidence,
                   finding_ids=list(item.finding_ids), risks=risks, notes=notes, origin=item.origin,
                   modules=sorted(set(item.modules)))


def _override_note(item: InventoryItem, target: str) -> str:
    what = f"adds a <dependencyManagement> entry {item.ga}:{target} to the root pom.xml"
    if item.origin == O_TRANSITIVE:
        chain = " → ".join(item.via) or "another dependency"
        return f"{what}: pins the transitive dependency brought by {chain} ({item.version} → {target})"
    manager = item.manager or "a parent / BOM outside the project"
    return f"{what}: overrides the version managed by {manager} ({item.version} → {target})"


def _override_change(project: Path, item: InventoryItem, policy: Any, source: Any, poms: _Poms) -> _Change | Residual:
    """간접 의존성과 부모 / BOM 관리 버전을 루트 pom의 override 항목 하나로 고친다.
    막히는 조건이 있으면 조치하지 못한 항목을 반환한다.
    확인 순서: jar가 아니거나 classifier가 있는지 -> 루트 pom을 읽을 수 있는지 -> 기존 관리 항목 -> 모듈이
    루트 pom을 상속하는지 -> 모듈에 직접 선언된 버전 -> 새 버전 고르기 -> 넣을 줄 정하기."""
    kind_word = "transitive dependency" if item.origin == O_TRANSITIVE else "managed version"
    if _inv.VARIANT_MARK in (item.reason or ""):
        return _residual(item, R_MANUAL_REVIEW,
                         f"{kind_word} {item.ga} resolves as a non-jar type or with a classifier ({item.reason}); "
                         "a plain override entry would not govern it")
    pp = poms.pp
    root = pp.poms.get(".")
    if root is None or not root.ok:
        return _residual(item, R_MANUAL_REVIEW, "cannot place an override entry safely: the root pom.xml is not readable")
    entries = _root_entries(pp, item.ga)
    if entries:
        return _existing_entry_change(project, item, policy, source, pp, entries)
    outside = []
    for mdir in item.modules:
        pf = pp.poms.get(mdir)
        if pf is None or not pf.ok or root not in pp.chain(pf):
            outside.append(mdir)
    if outside:
        return _residual(item, R_MANUAL_REVIEW,
                         f"module(s) {', '.join(sorted(outside))} do not inherit from the root pom.xml: an override "
                         "entry there would not reach them")
    declared = []
    for mdir in item.modules:
        hit = _inv._find_decl(pp, pp.poms[mdir], item.ga, management=False)
        if hit is not None and _inv._text(_inv._kid(hit[1], "version")):
            declared.append(f"{hit[0].rel} ({_inv._text(_inv._kid(hit[1], 'version'))})")
    if declared:
        return _residual(item, R_MANUAL_REVIEW,
                         f"{item.ga} is declared with a version in {', '.join(sorted(set(declared)))}: a declared "
                         "version wins over <dependencyManagement>, so an override entry would not change it")
    current = item.version
    if not _apply.is_version_text(current or ""):
        return _residual(item, R_MANUAL_REVIEW, f"resolved version {current!r} is not a plain version")
    target, evidence, reason, notes = _choose_target(item, policy, source, current)
    if not target:
        return _residual(item, reason, "; ".join(notes))
    g, a = _ga_parts(item.ga)
    try:
        edit = _apply.locate_override(project, g, a, target)
    except (_apply.EditError, OSError) as e:
        return _residual(item, R_MANUAL_REVIEW, f"cannot place an override entry safely: {e}")
    risks = [RISK_MANAGED_OVERRIDE] + ([RISK_MAJOR] if _major(target) != _major(current) else [])
    return _Change(ga=item.ga, from_version=current, to_version=target, kind=K_MANAGED_OVERRIDE, edit=edit,
                   evidence=evidence, finding_ids=list(item.finding_ids), risks=risks, origin=item.origin,
                   notes=[_override_note(item, target), *notes, OVERRIDE_WARNING, OVERRIDE_APPROVAL],
                   modules=sorted(set(item.modules)))


def _merge(group: list[_Change], project: Path | None = None) -> _Change:
    """같은 pom 줄을 고치는 변경들을 하나로 합친다. 발견 항목 ID는 합치고, 목표 버전은 가장 높은 것을 쓴다."""
    first = group[0]
    if len(group) == 1:
        return first
    best = max(group, key=lambda c: parse_version(c.to_version))
    ids: list[str] = []
    for c in group:
        ids += [i for i in c.finding_ids if i not in ids]
    notes = list(dict.fromkeys(n for c in group for n in c.notes))
    others = sorted({f"{c.ga}:{c.from_version}" for c in group[1:]})
    insert = first.edit.op == E_INSERT
    notes.append(f"one {'override entry' if insert else 'edit'} also covers {', '.join(others)} "
                 f"({'same artifact' if insert else 'same version line'})")
    if best is not first:
        notes.append(f"target {best.to_version} chosen for {best.ga} (highest of the merged targets)")
    risks = sorted({r for c in group for r in c.risks if r != RISK_MAJOR})
    if _major(best.to_version) != _major(first.from_version):
        risks.append(RISK_MAJOR)
    if insert:
        g, a = _ga_parts(first.ga)
        edit = _apply.locate_override(Path(project or "."), g, a, best.to_version)
    else:
        edit = TextEdit(pom=first.edit.pom, line=first.edit.line, old=first.edit.old, new=best.to_version)
    # 버전을 정하는 모듈은 모두 합치고, 그중 하나라도 정하는 모듈은 also에서 뺀다
    # 범위가 빈 변경이 하나라도 있으면 어느 모듈에 적용되는지 모르므로 합친 제안도 범위를 비워 둔다
    if all(c.modules for c in group):
        governed = sorted({m for c in group for m in c.modules})
        also = sorted({m for c in group for m in c.also_modules} - set(governed))
    else:
        governed, also = [], []
    return _Change(ga=first.ga, from_version=first.from_version, to_version=best.to_version, kind=first.kind, edit=edit,
                   evidence=best.evidence, finding_ids=ids, risks=sorted(risks), notes=notes, origin=first.origin,
                   modules=governed, also_modules=also, via=list(first.via),
                   inhouse=sorted({x for c in group for x in c.inhouse}))


def plan_round(project: Path, inventory: Inventory, cfg: Any, source: Any, *, round: int, source_tree_hash: str,
               rejected: set[tuple[str, str]] | None = None) -> RoundPlan:
    """라운드 하나의 계획을 만든다(``interfaces.PlanFn``). 항목마다 제안이나 조치하지 못한 항목을 만들고,
    같은 pom 줄은 합친 뒤, 승인 필요 여부와 diff, 해시를 붙인다.
    입력이 같으면 결과도 항상 같고, 제안 ID는 ``p-<round>-<n>``이다.

    One round's plan: proposals + residuals, merged per pom line, with approval classes, diff and hashes; deterministic."""
    project = Path(project)
    rem = _rem(cfg)
    policy = rem.policy
    phash = policy_hash(cfg)
    # 회사 부모(parents 그룹)의 g:a다.
    # 부모가 관리하는 버전은 서버 pin이 있으면 부모 버전 제안을 먼저 검토한다
    parents = frozenset(k for k, grp in inhouse_keys(cfg).items() if grp == "parents")
    rejected = set(rejected or set())
    plan = RoundPlan(round=round, source_tree_hash=source_tree_hash, policy_hash=phash)
    items = sorted(inventory.items, key=lambda i: (i.ga, i.version, i.origin,
                                                   i.location.pom if i.location else "", i.location.line if i.location else 0))
    if not inventory.complete:
        # 불완전한 인벤토리로 계획하면 빠진 항목이 0건처럼 보이므로 계획하지 않고 모두 남긴다
        plan.notes.append("inventory incomplete: nothing planned (" + "; ".join(inventory.notes or ["no detail"]) + ")")
        plan.residuals = [_residual(i, R_INVENTORY_INCOMPLETE, "inventory incomplete") for i in items]
        return plan
    changes: list[_Change] = []
    poms = _Poms(project)
    for item in items:
        got = _change_for(project, item, policy, source, poms, parents=parents)
        if isinstance(got, Residual):
            plan.residuals.append(got)
        else:
            # 나중에 거절이나 diff 실패로 남게 되어도 보고서가 경로와 사내 아티팩트를 보여 줄 수 있도록 옮겨 둔다
            got.via, got.inhouse = list(item.via or []), sorted(set(item.inhouse or []))
            changes.append(got)
    # 같은 pom 줄의 replace끼리, 같은 g:a의 override끼리 합친다. override 위치는 모두 원래 파일 기준이고
    # apply가 제안 ID 순서로 한 번에 넣는다
    groups: dict[tuple[str, int, str], list[_Change]] = {}
    for c in changes:
        key = (c.edit.pom, 0, c.ga) if c.edit.op == E_INSERT else (c.edit.pom, c.edit.line, "")
        groups.setdefault(key, []).append(c)
    merged: list[tuple[_Change, list[_Change]]] = []
    for key in sorted(groups):
        group = groups[key]
        if len({c.edit.old for c in group}) > 1:
            for c in group:
                plan.residuals.append(_change_residual(c, R_STALE_LOCATION, f"{key[0]}:{key[1]}: inventory names "
                                                                            "different version texts for one line"))
            continue
        try:
            merged.append((_merge(group, project), group))
        except (_apply.EditError, OSError) as e:
            for c in group:
                plan.residuals.append(_change_residual(c, R_MANUAL_REVIEW, f"cannot place an override entry safely: {e}"))
    always = set(rem.always_approve or [])
    n = 0
    for change, group in merged:
        if any((c.ga, change.to_version) in rejected for c in group):
            for c in group:
                plan.residuals.append(_change_residual(c, R_REJECTED_PROPOSALS,
                                                       f"{change.ga} → {change.to_version} was rejected in an earlier "
                                                       "decision; not proposed again"))
            continue
        try:
            diff = _apply.render_diff(project, [change.edit])
        except (_apply.EditError, OSError) as e:
            for c in group:
                plan.residuals.append(_change_residual(c, R_STALE_LOCATION, str(e)))
            continue
        n += 1
        # override 항목은 XML을 새로 넣으므로 설정과 관계없이 항상 승인을 기다린다
        needs = (change.kind == K_MANAGED_OVERRIDE or bool(rem.require_approval)
                 or any(r in always for r in change.risks))
        p = Proposal(id=f"p-{round}-{n}", round=round, ga=change.ga, from_version=change.from_version,
                     to_version=change.to_version, kind=change.kind, risks=sorted(change.risks), requires_approval=needs,
                     decision=D_PENDING if needs else D_AUTO, evidence=change.evidence,
                     finding_ids=list(change.finding_ids), edits=[change.edit], diff=diff,
                     source_tree_hash=source_tree_hash, policy_hash=phash, note="; ".join(change.notes),
                     modules=list(change.modules), also_modules=list(change.also_modules))
        p.proposal_hash = p.compute_hash()
        plan.proposals.append(p)
    plan.residuals.sort(key=lambda r: (r.ga, r.version, r.reason))
    if plan.pending():
        plan.notes.append(f"{len(plan.pending())} proposal(s) wait for approval of their exact diff")
    log.info("remediation plan round %s: %d proposal(s), %d residual(s)", round, len(plan.proposals), len(plan.residuals))
    return plan


# 재개할 때 낡은 승인 대기 제안을 쓰지 않도록, 그래프가 계획 없이도 정책 해시를 계산할 수 있게 함수에 붙여 둔다
plan_round.policy_hash = policy_hash   # type: ignore[attr-defined]


# ---------------------------------------------------------------------------------------------------- 승인 확인
def _norm_diff(s: str) -> str:
    return (s or "").replace("\r\n", "\n")


def validate_decision(proposal: Proposal, decision: Decision, *, current_tree_hash: str, current_policy_hash: str) -> None:
    """승인이나 거절 요청을 저장된 제안과 대조하고, 다르면 무엇이 다른지 담아 ``ApprovalMismatch``를 던진다.
    네 값(제안 해시, 원본 트리 해시, 정책 해시, diff)이 모두 같고, 제안이 현재 트리와 정책 기준으로
    만들어졌으며, 아직 ``pending``이어야 통과한다.

    Raise `ApprovalMismatch` naming every differing item unless the four bindings all match and the proposal is current."""
    problems: list[str] = []
    fields: list[str] = []
    if decision.proposal_id != proposal.id:
        problems.append(f"proposal id {decision.proposal_id!r} does not match {proposal.id!r}")
        fields.append("proposal_id")
    if proposal.decision != D_PENDING:
        problems.append(f"proposal {proposal.id} is not pending (decision={proposal.decision})")
        fields.append("state")
    if decision.decision not in (D_APPROVED, D_REJECTED):
        problems.append(f"decision must be {D_APPROVED} or {D_REJECTED}, got {decision.decision!r}")
        fields.append("decision")
    if proposal.compute_hash() != proposal.proposal_hash:
        problems.append("proposal hash: the stored proposal no longer matches its own hash")
        fields.append("proposal_hash")
    elif decision.proposal_hash != proposal.proposal_hash:
        problems.append("proposal hash differs")
        fields.append("proposal_hash")
    if decision.source_tree_hash != proposal.source_tree_hash:
        problems.append("source tree hash differs")
        fields.append("source_tree_hash")
    if proposal.source_tree_hash != current_tree_hash:
        problems.append("source tree hash: the accepted tree changed since the proposal was made (stale proposal)")
        fields.append("source_tree_hash")
    if decision.policy_hash != proposal.policy_hash:
        problems.append("policy hash differs")
        fields.append("policy_hash")
    if proposal.policy_hash != current_policy_hash:
        problems.append("policy hash: the server policy changed since the proposal was made (stale proposal)")
        fields.append("policy_hash")
    if _norm_diff(decision.diff) != _norm_diff(proposal.diff):
        problems.append("diff differs")
        fields.append("diff")
    if problems:
        raise ApprovalMismatch(f"decision for {proposal.id} rejected: " + "; ".join(problems),
                               proposal_id=proposal.id, fields=sorted(set(fields)))
