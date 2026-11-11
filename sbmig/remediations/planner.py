"""취약점 조치의 계획: 인벤토리(취약한 라이브러리와 그 버전 글자의 자리)에서 버전 변경 제안(proposal)과 남길 항목(residual)을
만들고, 승인이 필요한지 정하고, 제안을 지금의 트리·정책에 묶는 해시를 단다. 흐름상 위치: inventory → **plan** → (승인 대기) → apply.
핵심 진입점: ``plan_round`` (``interfaces.PlanFn``), ``policy_hash``, ``validate_decision``, ``RepositoryVersionSource``.

지키는 결정(회사 규칙):
- 새 버전은 **근거가 있는 곳에서만** 고른다: 서버 정책 ``policy.pins`` 또는 승인된 저장소(``VersionSource``)가 실제로 가진 버전.
  IVAS 의 patched 버전은 힌트일 뿐이다 — 저장소가 그 버전을 갖고 있지 않으면 고르지 않고, 힌트가 없으면 고를 근거가 없다(잔여).
- 같은 메이저 안의 가장 낮은 수정 버전을 먼저, 없을 때만 더 높은 메이저(``allow_major`` 일 때, 승인 부류 major).
- 부모·BOM 버전 글자는 서버가 그 부모·BOM 자체를 ``pins`` 로 정해 두었을 때만 바꾼다(승인 부류 parent / bom).
- 간접(transitive) 의존성과, pin 이 없는 부모·BOM 관리 버전은 (사용자 결정 2026-11-11) **관리 버전 override 항목을 더해서**
  고친다: 직접 의존성과 같은 규칙(힌트 필수·승인된 저장소 근거·같은 메이저 우선·deny 목록)으로 고른 버전을 루트 ``pom.xml`` 의
  ``<dependencyManagement>`` 에 ``<dependency>`` 항목 하나로 넣는 제안(종류 ``managed_override``, 위험 ``managed_override``).
  이 제안은 **언제나 승인을 기다린다** — ``require_approval`` 이 false 여도, 운영자가 ``always_approve`` 에서
  ``managed_override`` 를 빼도 마찬가지다(XML 을 넣는 변경은 절대 자동으로 적용하지 않는다는 고정 규칙).
  루트 pom 에 그 g:a 의 관리 항목이 이미 있으면 두 번째 항목을 넣지 않고 기존 버전 글자를 바꾸는 길로 간다(안 되면 잔여).
  모듈이 루트 pom 을 상속하지 않거나, 모듈이 그 의존성을 버전과 함께 직접 선언했거나(선언 버전이 관리 버전을 이긴다),
  해석된 의존성이 jar 가 아니거나 분류자가 있거나, 루트 pom 에 항목을 안전하게 넣을 줄을 못 찾으면 잔여로 남긴다.
- 승인은 제안 해시·원본 트리 해시·정책 해시·diff 가 **모두** 같을 때만 받아들인다(``validate_decision``).

Planning for the remediation feature: proposals + residuals from the inventory, approval classes, and the hashes that
bind a proposal to the current tree and policy. A target version comes only from `policy.pins` or a version the approved
repository lists (IVAS patched versions are hints, never evidence). A parent / BOM version text changes only when the
server pins that parent / BOM. Transitive items and parent / BOM managed versions without such a pin get a
managed-version override entry in the root pom (user decision 2026-11-11); such a proposal ALWAYS waits for approval,
whatever `require_approval` / `always_approve` say — inserting XML is never automatic.
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

# 잔여 이유 코드 R_POLICY_DENIED / R_UNKNOWN_PROVENANCE / R_STALE_LOCATION / R_INVENTORY_INCOMPLETE 는 models 의 것을
# 그대로 다시 내보낸다(예전 import 경로 ``planner.R_…`` 도 계속 쓸 수 있게)
EVIDENCE_PINS = "policy:pins"
# override 제안의 note 에 늘 들어가는 문장(보고서로 간다): 넣은 항목은 그 뒤로 부모·BOM·직접 의존성을 올려도 움직이지 않는다
OVERRIDE_WARNING = ("later upgrades of the parent / BOM / direct dependency will NOT move this version any more — "
                    "the owners must remove the override when the parent / BOM / direct dependency catches up")
OVERRIDE_APPROVAL = "always needs approval: an inserted <dependencyManagement> entry is never applied automatically"
_LIST_CAP = 20                       # 잔여 설명에 늘어놓는 버전 수 한도(문장이 끝없이 길어지지 않게)


# ---------------------------------------------------------------------------------------------------- 정책 해시
def _rem(cfg: Any) -> Any:
    return cfg.remediation


def _policy_dict(policy: Any) -> dict[str, Any]:
    if hasattr(policy, "model_dump"):
        return policy.model_dump()
    return dict(vars(policy))


def policy_hash(cfg: Any) -> str:
    """서버 정책의 해시: ``remediation.policy`` 전부 + ``require_approval`` + ``always_approve``(정렬) + ``failed_candidate``.
    무엇을 제안하는지·무엇이 승인을 기다리는지를 바꾸는 값이 하나라도 바뀌면 해시가 바뀌어, 전에 만든 승인 후보는 무효가 된다.

    Hash of everything that changes what is proposed or what needs approval."""
    rem = _rem(cfg)
    return sha256_text(canonical_json({
        "policy": _policy_dict(rem.policy),
        "require_approval": bool(rem.require_approval),
        "always_approve": sorted(str(x) for x in (rem.always_approve or [])),
        "failed_candidate": str(rem.failed_candidate),
    }))


# ---------------------------------------------------------------------------------------------------- 버전 출처
class RepositoryVersionSource:
    """``interfaces.VersionSource`` 의 진짜 구현: ``sbmig.build.repo_versions.RepositoryLookup`` (서버 settings.xml 의 미러·저장소,
    없으면 로컬 저장소)를 감싼다. 근거 문장은 버전 목록을 준 URL 의 **호스트만**("repository:<host>", 자격증명·경로 없음)이고,
    그 호스트가 **승인된 저장소 호스트** 목록에 있을 때만 근거가 된다. 승인 목록은 ``allowed_hosts`` (목록 또는 목록을 돌려주는
    함수 — 운영은 ``ServerMaven.allowed_hosts``)에서, 주지 않으면 ``remediation.allowed_repository_hosts`` → 서버 settings.xml 의
    호스트(``ServerMaven(cfg).allowed_hosts()``) 순으로 정한다. 로컬 저장소(~/.m2)에서만 나온 버전은 승인된 저장소의 근거가
    아니다(누가 어디서 받아 두었는지 모른다) — 근거 없음. 테스트는 ``lookup`` 에 가짜를 넣는다.

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
        """조회기(처음 쓸 때 만든다 — 테스트의 오프라인 대역 교체가 닿도록 모듈 속성으로 찾는다)."""
        if self._lookup is None:
            from sbmig.build import repo_versions as rv
            self._lookup = rv.RepositoryLookup(self.cfg, project_root=self.project_root)
        return self._lookup

    def allowed_hosts(self) -> set[str]:
        """승인된 저장소 호스트(소문자). 정할 수 없으면 빈 집합 = 어떤 저장소도 근거가 되지 않는다(보수적).

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
        """저장소가 가진 버전들(``parse_version`` 오름차순, 중복 없음). 조회 실패는 빈 목록.

        Versions the repository lists, ascending."""
        try:
            got = list(self.lookup.versions(group_id, artifact_id) or [])
        except Exception as e:  # noqa: BLE001 - an unreadable repository is "no evidence", never a crash
            log.warning("remediation versions(%s:%s): %s", group_id, artifact_id, type(e).__name__)
            return []
        return sorted(dict.fromkeys(v for v in got if v), key=parse_version)

    def evidence(self, group_id: str, artifact_id: str, version: str) -> str:
        """``version`` 의 근거 "repository:<host>" — 저장소가 그 버전을 갖고 있고 목록을 준 호스트가 승인된 호스트일 때만.
        아니면 ""(로컬 저장소만 아는 버전, 승인되지 않은 호스트 포함).

        Evidence for `version`; "" unless an approved host listed it."""
        if version not in self.versions(group_id, artifact_id):
            return ""
        answered = getattr(self.lookup, "_answered", {}) or {}
        url = answered.get((group_id, artifact_id))
        if not url:
            return ""          # 왜: 로컬 저장소(또는 출처를 모르는 목록)는 승인된 저장소의 근거가 아니다
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
    """위치의 줄 글자(없거나 읽을 수 없으면 None). 인코딩은 apply 와 같은 규칙으로 읽는다."""
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
    """위치가 낡았으면(그 줄에 ``loc.text`` 가 정확히 한 번 있지 않으면) 이유 한 줄, 아니면 ""."""
    line = _read_line(project, loc)
    if line is None:
        return f"{loc.pom}:{loc.line} no longer exists or cannot be read"
    count = line.count(loc.text) if loc.text else 0
    if count != 1:
        return f"{loc.pom}:{loc.line} contains {loc.text!r} {count} time(s), expected exactly once (file changed?)"
    return ""


@dataclass
class _Change:
    """제안이 되기 전의 변경 하나(같은 줄을 고치는 것끼리 합친 뒤 Proposal 이 된다)."""
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


def _residual(item: InventoryItem, reason: str, detail: str) -> Residual:
    return Residual(ga=item.ga, version=item.version, origin=item.origin, reason=reason, detail=detail,
                    finding_ids=list(item.finding_ids))


_LINE_NUMS = re.compile(r"\d+")


def _line(v: str) -> tuple[int, int]:
    """버전의 줄(major.minor): 앞의 숫자 두 개(없으면 0). "2.12.7.1" → (2, 12), "3" → (3, 0)."""
    head = re.match(r"\d+(?:\.\d+)*", (v or "").strip())
    nums = [int(x) for x in _LINE_NUMS.findall(head.group(0))] if head else []
    nums += [0, 0]
    return nums[0], nums[1]


def _fixes(v: str, alternatives: list[str]) -> bool:
    """``v`` 가 이 힌트 묶음(발견 항목 하나의 "고쳐진 버전들")을 만족하는가: 가장 높은 대안 h 이상이거나, 다른 대안 h 와
    **같은 줄(major.minor)** 에서 h 이상."""
    top = max(alternatives, key=parse_version)
    pv = parse_version(v)
    for h in alternatives:
        if pv >= parse_version(h) and (h == top or _line(v) == _line(h)):
            return True
    return False


def _choose_target(item: InventoryItem, policy: Any, source: Any, current: str) -> tuple[str, str, str, list[str]]:
    """새 버전 고르기: (버전, 근거, 잔여 이유, 메모). 버전이 ""이면 잔여 이유와 사실 설명이 메모에 있다.

    1) ``policy.pins`` 가 있으면 그것(힌트는 메모만).
    2) 아니면 승인된 저장소가 가진 버전(지금보다 새 것, deny_versions·시험판 제외) 중에서 IVAS 힌트를 **모두** 만족하는
       가장 낮은 것 — 같은 메이저 먼저, 없을 때만 더 높은 메이저(``allow_major`` 일 때).
       힌트의 모양: ``patched_versions`` 의 원소 하나 = 발견 항목 하나의 패치 버전 글자. 서로 다른 발견 항목(다른 CVE)은
       **모두** 고쳐야 하므로 AND 다(가장 높은 요구가 이긴다). 원소 하나에 버전이 여럿이면("2.12.7.1, 2.13.4.2" — 회사가
       모양을 확인해 주지 않았다) 줄마다의 대안(OR)으로 읽는다: 가장 높은 대안 이상이거나, 다른 대안 h 와 같은 줄(major.minor)
       에서 h 이상이면 만족. 지금보다 낮은 줄은 후보가 아니므로 결과적으로 **지금 줄의 힌트가 먼저** 쓰인다.
       예: 지금 2.12.3, 힌트 "2.12.7.1, 2.13.4.2" → 2.12.x 안의 ≥ 2.12.7.1 중 가장 낮은 것, 없으면 ≥ 2.13.4.2.
       버전 글자가 아닌 힌트(범위 "[2.0,3.0)" 등)가 하나라도 있으면 고르지 않는다(잔여 ``no_candidate_version``, 원문을 적는다).

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
            # 왜: allow_major=false 는 "메이저 변경은 제안조차 하지 않는다" — 서버 pin 이어도 같은 규칙을 따른다(보수적)
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
        # 왜: 범위·문장 같은 힌트를 버전으로 읽으면 엉뚱한 바닥을 고른다 — 사람이 보게 남긴다
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


def _change_for(project: Path, item: InventoryItem, policy: Any, source: Any, poms: _Poms) -> _Change | Residual:
    """인벤토리 항목 하나 → 변경 하나 또는 잔여 하나."""
    if item.origin in MANUAL_ORIGINS:
        return _residual(item, R_MANUAL_REVIEW, item.reason or f"{item.origin}: not changed automatically")
    if _denied(item.ga, policy.deny):
        return _residual(item, R_POLICY_DENIED, f"{item.ga} matches policy.deny")
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
    risks = [RISK_MAJOR] if _major(target) != _major(current) else []
    return _Change(ga=item.ga, from_version=current, to_version=target, kind=kind,
                   edit=TextEdit(pom=loc.pom, line=loc.line, old=loc.text, new=target), evidence=evidence,
                   finding_ids=list(item.finding_ids), risks=risks, notes=notes, origin=item.origin)


def _managed_change(project: Path, item: InventoryItem, policy: Any) -> _Change | Residual | None:
    """부모·BOM 이 관리하는 항목: 서버가 그 부모·BOM 자체를 pin 했으면 그 버전 글자를 바꾸는 제안(또는 pin 을 쓸 수 없는 이유의
    잔여). pin 이 없으면(관리자를 모르는 경우 포함) None — 부르는 쪽이 관리 버전 override 길로 간다.
    왜: 어느 부모·BOM 버전이 이 취약점을 고쳐 주는지 오프라인으로는 근거가 없다 — 서버 pin 만 근거다."""
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
    """루트 pom 과 reactor 의 lxml 모델(``inventory.ProjectPoms``)을 처음 필요할 때 한 번 읽는다."""

    def __init__(self, project: Path):
        self.project = project
        self._pp: Any = None

    @property
    def pp(self) -> Any:
        if self._pp is None:
            self._pp = _inv.ProjectPoms(self.project)
        return self._pp


def _root_entries(pp: Any, ga: str) -> list[tuple[Any, str]]:
    """루트 pom 의 ``<dependencyManagement>`` (최상위 "" / 프로필 "profile <id>")에 있는 이 g:a 의 항목(import 아닌 것)."""
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
    """루트 pom 이 이 g:a 를 이미 관리할 때: 두 번째 항목은 넣지 않는다. 최상위 항목의 버전 글자(글자 또는 프로젝트 속성)가
    해석된 버전과 같으면 그 글자를 바꾸는 제안(own_management_version / property_version), 아니면 잔여."""
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
                   finding_ids=list(item.finding_ids), risks=risks, notes=notes, origin=item.origin)


def _override_note(item: InventoryItem, target: str) -> str:
    what = f"adds a <dependencyManagement> entry {item.ga}:{target} to the root pom.xml"
    if item.origin == O_TRANSITIVE:
        chain = " → ".join(item.via) or "another dependency"
        return f"{what}: pins the transitive dependency brought by {chain} ({item.version} → {target})"
    manager = item.manager or "a parent / BOM outside the project"
    return f"{what}: overrides the version managed by {manager} ({item.version} → {target})"


def _override_change(project: Path, item: InventoryItem, policy: Any, source: Any, poms: _Poms) -> _Change | Residual:
    """간접 의존성·부모/BOM 관리 버전 → 루트 pom 의 관리 버전 override 항목 하나(insert edit). 막는 사실이 있으면 잔여.
    순서: 변종(jar 아님 / 분류자) → 루트 pom 을 읽을 수 있는가 → 이미 있는 관리 항목(→ 기존 글자 바꾸기) → 모듈이 루트 pom 을
    상속하는가 → 모듈의 직접 선언 버전 → 새 버전 고르기(직접 의존성과 같은 규칙) → 넣을 줄 정하기."""
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
                   notes=[_override_note(item, target), *notes, OVERRIDE_WARNING, OVERRIDE_APPROVAL])


def _merge(group: list[_Change], project: Path | None = None) -> _Change:
    """같은 pom 줄을 고치는 변경들을 하나로: 발견 ID 합집합, 목표는 가장 높은 것, ga 는 첫 번째, 나머지는 메모에."""
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
    return _Change(ga=first.ga, from_version=first.from_version, to_version=best.to_version, kind=first.kind, edit=edit,
                   evidence=best.evidence, finding_ids=ids, risks=sorted(risks), notes=notes, origin=first.origin)


def plan_round(project: Path, inventory: Inventory, cfg: Any, source: Any, *, round: int, source_tree_hash: str,
               rejected: set[tuple[str, str]] | None = None) -> RoundPlan:
    """라운드 하나의 계획(``interfaces.PlanFn``). 인벤토리 항목마다 제안 또는 잔여를 만들고, 같은 pom 줄을 고치는 것은 하나로
    합치고, 승인 부류·결정 상태·diff·해시를 단다. 같은 입력이면 늘 같은 결과(정렬된 순서, ``p-<round>-<n>``).

    One round's plan: proposals + residuals, merged per pom line, with approval classes, diff and hashes; deterministic."""
    project = Path(project)
    rem = _rem(cfg)
    policy = rem.policy
    phash = policy_hash(cfg)
    rejected = set(rejected or set())
    plan = RoundPlan(round=round, source_tree_hash=source_tree_hash, policy_hash=phash)
    items = sorted(inventory.items, key=lambda i: (i.ga, i.version, i.origin,
                                                   i.location.pom if i.location else "", i.location.line if i.location else 0))
    if not inventory.complete:
        # 왜: 불완전한 인벤토리로 계획하면 빠진 항목이 "0건" 처럼 보인다 — 계획하지 않고 전부 잔여로 남긴다
        plan.notes.append("inventory incomplete: nothing planned (" + "; ".join(inventory.notes or ["no detail"]) + ")")
        plan.residuals = [_residual(i, R_INVENTORY_INCOMPLETE, "inventory incomplete") for i in items]
        return plan
    changes: list[_Change] = []
    poms = _Poms(project)
    for item in items:
        got = _change_for(project, item, policy, source, poms)
        if isinstance(got, Residual):
            plan.residuals.append(got)
        else:
            changes.append(got)
    # 같은 pom 줄을 고치는 replace 끼리, 같은 g:a 의 override 끼리 합친다. override 의 자리는 모두 **원래 파일** 기준으로
    # 정했고(같은 기준 줄을 여럿이 가질 수 있다), apply 가 그것들을 제안 ID 순서로 한 번에 넣는다.
    groups: dict[tuple[str, int, str], list[_Change]] = {}
    for c in changes:
        key = (c.edit.pom, 0, c.ga) if c.edit.op == E_INSERT else (c.edit.pom, c.edit.line, "")
        groups.setdefault(key, []).append(c)
    merged: list[tuple[_Change, list[_Change]]] = []
    for key in sorted(groups):
        group = groups[key]
        if len({c.edit.old for c in group}) > 1:
            for c in group:
                plan.residuals.append(Residual(ga=c.ga, version=c.from_version, origin=c.origin, reason=R_STALE_LOCATION,
                                               detail=f"{key[0]}:{key[1]}: inventory names different version texts "
                                                      "for one line", finding_ids=list(c.finding_ids)))
            continue
        try:
            merged.append((_merge(group, project), group))
        except (_apply.EditError, OSError) as e:
            for c in group:
                plan.residuals.append(Residual(ga=c.ga, version=c.from_version, origin=c.origin, reason=R_MANUAL_REVIEW,
                                               detail=f"cannot place an override entry safely: {e}",
                                               finding_ids=list(c.finding_ids)))
    always = set(rem.always_approve or [])
    n = 0
    for change, group in merged:
        if any((c.ga, change.to_version) in rejected for c in group):
            for c in group:
                plan.residuals.append(Residual(ga=c.ga, version=c.from_version, origin=c.origin,
                                               reason=R_REJECTED_PROPOSALS,
                                               detail=f"{change.ga} → {change.to_version} was rejected in an earlier "
                                                      "decision; not proposed again", finding_ids=list(c.finding_ids)))
            continue
        try:
            diff = _apply.render_diff(project, [change.edit])
        except (_apply.EditError, OSError) as e:
            for c in group:
                plan.residuals.append(Residual(ga=c.ga, version=c.from_version, origin=c.origin, reason=R_STALE_LOCATION,
                                               detail=str(e), finding_ids=list(c.finding_ids)))
            continue
        n += 1
        # 고정 규칙: override 항목(XML 넣기)은 설정과 무관하게 늘 승인을 기다린다
        needs = (change.kind == K_MANAGED_OVERRIDE or bool(rem.require_approval)
                 or any(r in always for r in change.risks))
        p = Proposal(id=f"p-{round}-{n}", round=round, ga=change.ga, from_version=change.from_version,
                     to_version=change.to_version, kind=change.kind, risks=sorted(change.risks), requires_approval=needs,
                     decision=D_PENDING if needs else D_AUTO, evidence=change.evidence,
                     finding_ids=list(change.finding_ids), edits=[change.edit], diff=diff,
                     source_tree_hash=source_tree_hash, policy_hash=phash, note="; ".join(change.notes))
        p.proposal_hash = p.compute_hash()
        plan.proposals.append(p)
    plan.residuals.sort(key=lambda r: (r.ga, r.version, r.reason))
    if plan.pending():
        plan.notes.append(f"{len(plan.pending())} proposal(s) wait for approval of their exact diff")
    log.info("remediation plan round %s: %d proposal(s), %d residual(s)", round, len(plan.proposals), len(plan.residuals))
    return plan


# 그래프가 지금 정책의 해시를 계획 없이 알 수 있게(재개 때 낡은 승인 후보를 다시 쓰지 않으려고) 함수에 붙여 둔다
plan_round.policy_hash = policy_hash   # type: ignore[attr-defined]


# ---------------------------------------------------------------------------------------------------- 승인 확인
def _norm_diff(s: str) -> str:
    return (s or "").replace("\r\n", "\n")


def validate_decision(proposal: Proposal, decision: Decision, *, current_tree_hash: str, current_policy_hash: str) -> None:
    """승인·거절 요청 하나를 저장된 제안과 맞춰 본다. 제안 해시·원본 트리 해시·정책 해시·diff 넷이 **모두** 같고, 저장된 제안의
    해시가 지금의 트리·정책과도 같고(낡은 제안이 아님), 제안이 아직 ``pending`` 이고 결정이 approved / rejected 일 때만 통과.
    아니면 무엇이 다른지 이름을 붙여 ``ApprovalMismatch``.

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
