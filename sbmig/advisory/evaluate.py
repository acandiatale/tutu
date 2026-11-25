"""프로젝트에 맞는 권고를 찾고 인수인계에 넣을 후속 작업 문장을 만드는 모듈.
- ``evaluate_advisories``: 분석 단계가 호출한다. 결과는 프로필과 보고서의 권고가 된다
- ``followups_for``: 권고마다 후속 작업 문장을 하나씩 만든다
- 권고는 안내만 하고 레시피를 더하지 않는다
- 결과(outcome)는 마무리 단계의 ``verify_advisories``가 정할 때까지 "unknown"이다
- ``advisories.policy_overrides``에서 ``{id: off}``로 항목을 끌 수 있다. 다른 값은 효과가 없다

The fix loop gets the findings' hints through `sbmig.advisory.hints`, the Handover their follow-ups.
"""
from __future__ import annotations

import fnmatch
import logging
from typing import Any, Iterable, Optional

from sbmig.core.config import Config
from sbmig.core.models import GAV, AdvisoryFinding, LibraryAdvisory, ProjectProfile

from .catalog import AdvisoryCatalog, load_advisories

log = logging.getLogger("sbmig.advisory")

STATUS_ORDER = {"eol": 0, "incompatible": 1, "unmanaged": 2, "superseded": 3}
_POM_EVIDENCE = ("pom:", "pom-managed:", "pom-parent:")


# ---------------------------------------------------------------------------
# 매칭 도구
# ---------------------------------------------------------------------------
def _concrete(v: Optional[str]) -> Optional[str]:
    """``${...}`` 참조가 아닌 실제 버전이면 그대로 반환하고, 아니면 None을 반환한다."""
    return v if v and "${" not in v else None


def gav_matches(pattern: str, g: GAV) -> bool:
    """``group:artifact-glob`` 패턴(fnmatch, 대소문자 구분)이 좌표에 맞는지 확인한다.
    artifact가 없으면 ``*``로 본다."""
    pg, _, pa = pattern.partition(":")
    return fnmatch.fnmatchcase(g.group_id, pg) and fnmatch.fnmatchcase(g.artifact_id, pa or "*")


def pom_gavs(poms: Iterable[Any]) -> list[GAV]:
    """모든 pom의 의존성, dependencyManagement, parent 좌표를 이 순서로 모은다. 권고를 매칭할 때 쓴다."""
    deps: list[GAV] = []
    managed: list[GAV] = []
    parents: list[GAV] = []
    for p in poms:
        deps += list(p.dependencies)
        managed += list(p.dependency_management)
        if p.parent is not None:
            parents.append(p.parent)
    return deps + managed + parents


def artifact_hits(patterns: Iterable[str], gavs: list[GAV]) -> list[GAV]:
    """패턴 중 하나라도 맞는 좌표를 반환한다."""
    pats = list(patterns)
    return [g for g in gavs if any(gav_matches(pt, g) for pt in pats)]


def _evidence_keys(evidence: Iterable[str]) -> list[str]:
    """분석기 근거 문자열(``pom:g:a:v`` 등)에서 ``g:a`` 키를 중복 없이 뽑는다."""
    out: list[str] = []
    for ev in evidence:
        for pre in _POM_EVIDENCE:
            if ev.startswith(pre):
                parts = ev[len(pre):].split(":")
                if len(parts) >= 2:
                    k = f"{parts[0]}:{parts[1]}"
                    if k not in out:
                        out.append(k)
                break
    return out


def below(version: Optional[str], bound: str) -> bool:
    """``version``이 ``bound``보다 낮은지 확인한다.
    권고를 놓치는 것보다 한 번 더 안내하는 편이 나으므로, 없거나 읽지 못하는 버전(릴리스 트레인 이름 등)은 낮은 것으로 본다."""
    from sbmig.plan.versions import version_lt

    r = version_lt(version, bound)
    return True if r is None else bool(r)


def _sort_key(f: AdvisoryFinding) -> tuple[int, str]:
    """eol, incompatible, unmanaged, superseded 순서로, 같으면 id 순서로 정렬하는 키를 만든다."""
    return STATUS_ORDER.get(f.status, 9), f.advisory_id


# ---------------------------------------------------------------------------
# 권고 평가
# ---------------------------------------------------------------------------
def switched_off(entry: LibraryAdvisory, cfg: Config) -> bool:
    """``advisories.policy_overrides``에서 이 항목이 "off"로 꺼져 있는지 확인한다. 다른 값은 아무것도 바꾸지 않는다."""
    return (cfg.advisories.policy_overrides or {}).get(entry.id) == "off"


def _match(entry: LibraryAdvisory, profile: ProjectProfile, gavs: list[GAV]) -> Optional[dict[str, Any]]:
    """분석기가 찾은 라이브러리 이름이나 pom 좌표 패턴으로 권고 하나가 이 프로젝트에 맞는지 확인한다.
    맞으면 라이브러리, 근거, 아티팩트 키, 감지된 버전을 담은 dict를 반환하고, 아니면 None을 반환한다."""
    lib = next((u for u in profile.libraries if entry.library and u.name == entry.library), None)
    hits = artifact_hits(entry.artifacts, gavs) if entry.artifacts else []
    if lib is None and not hits:
        return None
    evidence = list(lib.evidence) if lib is not None else []
    keys = _evidence_keys(evidence)
    for g in hits:
        ev = f"pom:{g.coordinates}"
        if ev not in evidence:
            evidence.append(ev)
        if g.key not in keys:
            keys.append(g.key)
    version = _concrete(lib.version) if lib is not None else None
    if version is None:
        version = next((_concrete(g.version) for g in hits if _concrete(g.version)), None)
    return {"library": entry.library if lib is not None else (entry.library or entry.id),
            "evidence": evidence, "artifacts": keys, "version": version}


def evaluate_advisories(profile: ProjectProfile, cfg: Config,
                        catalog: Optional[AdvisoryCatalog] = None) -> list[AdvisoryFinding]:
    """이 프로젝트에 맞는 권고 목록을 만든다. 상태 순서(eol부터), 같으면 id 순서로 정렬한다.
    - 권고가 꺼져 있거나 카탈로그가 비어 있으면 빈 목록을 반환한다
    - 케이스 타입(``applies_to``), 버전 상한(``applies_when_version_below``), off 설정을 차례로 확인한다
    - 결과는 안내 전용(``policy="guide"``)이고 outcome은 "unknown"이다

    Bundled jars (``profile.bundled_jars``, no pom declares them) are matched like declared dependencies."""
    if not cfg.advisories.enabled:
        return []
    cat = catalog if catalog is not None else load_advisories(cfg)
    if not len(cat):
        return []
    gavs = pom_gavs(profile.poms)
    # WEB-INF/lib 등에 jar로만 들어온 라이브러리도 권고가 인수인계와 수정 프롬프트에 전달되도록 좌표에 넣는다
    for j in getattr(profile, "bundled_jars", None) or []:
        if getattr(j, "group_id", "") and getattr(j, "artifact_id", ""):
            gavs.append(GAV(group_id=j.group_id, artifact_id=j.artifact_id, version=j.version or None))
    out: dict[str, AdvisoryFinding] = {}
    for entry in cat.entries:
        if entry.id in out:
            continue
        if entry.applies_to and profile.case_type not in entry.applies_to:
            continue
        m = _match(entry, profile, gavs)
        if m is None:
            continue
        if entry.applies_when_version_below and not below(m["version"], entry.applies_when_version_below):
            log.debug("advisory %s: version %s ≥ %s; not applicable", entry.id, m["version"],
                      entry.applies_when_version_below)
            continue
        if switched_off(entry, cfg):
            log.info("advisory %s switched off by advisories.policy_overrides", entry.id)
            continue
        notes = [f"see {entry.notes}"] if entry.notes else []
        out[entry.id] = AdvisoryFinding(
            advisory_id=entry.id, library=m["library"], status=entry.status, title=entry.title,
            summary=entry.summary, policy="guide", detected_version=m["version"],
            evidence=m["evidence"], artifacts=m["artifacts"], outcome="unknown",
            replacement=entry.replacement, replacement_version=entry.replacement_version,
            followup=entry.followup.strip(), docs=list(entry.docs), notes=notes,
            title_ko=entry.title_ko.strip(), summary_ko=entry.summary_ko.strip(),
            followup_ko=entry.followup_ko.strip(), replacement_ko=entry.replacement_ko.strip())
    return sorted(out.values(), key=_sort_key)


# ---------------------------------------------------------------------------
# 후속 작업 문장
# ---------------------------------------------------------------------------
def _replacement_hint(f: AdvisoryFinding) -> str:
    """후속 작업 문장 끝에 붙일 `` (replacement: …)``를 만든다."""
    if not f.replacement:
        return ""
    return f" (replacement: {f.replacement}" + (f" {f.replacement_version}" if f.replacement_version else "") + ")"


def followups_for(findings: Optional[list[AdvisoryFinding]]) -> list[str]:
    """권고마다 후속 작업 문장을 하나씩 만든다. 같은 문장은 한 번만 넣는다.
    교체되지 않은 권고(still-present / unknown)를 먼저 쓰고, 교체되어 확인만 하면 되는 권고를 뒤에 쓴다.

    The Handover's manual follow-ups drop the items these sentences already cover."""
    out: list[str] = []
    items = list(findings or [])
    pending = [f for f in items if f.outcome != "replaced"]
    done = [f for f in items if f.outcome == "replaced"]
    for f in pending:
        text = f"{f.title}: {f.followup}".strip().rstrip(":") if f.followup else f.title
        if f.replacement and f.replacement.split()[0].rsplit(":", 1)[-1] not in text:
            text += _replacement_hint(f)
        if text not in out:
            out.append(text)
    for f in done:
        text = f"{f.library} → {f.replacement or 'replacement'} applied by the migration"
        if f.followup:
            text += f"; {f.followup}"
        if text not in out:
            out.append(text)
    return out
