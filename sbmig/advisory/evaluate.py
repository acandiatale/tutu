"""권고 평가 — 분석 뒤 이 프로젝트에 해당하는 권고를 찾고(``evaluate_advisories``), 인수인계의 후속 작업
문장을 만든다(``followups_for``: 교체 안 된 것 먼저, 교체됐으면 "확인" 문장). 흐름상 위치: 분석 단계가
``evaluate_advisories`` 를 부르고 결과는 프로필·보고서의 권고(findings)가 된다. 결과 (outcome)는 마무리의
``verify_advisories`` 가 정할 때까지 "unknown". 권고는 안내만 한다 — 레시피를 더하지 않는다.
``advisories.policy_overrides`` 에서 ``{id: off}`` 로 항목을 끌 수 있다(그 밖의 값은 아무 효과 없음).

Advisory evaluation (after analyze) and the handover follow-up sentences. Advisories are guide-only: they never
add a recipe; the fix loop gets their hints (`sbmig.advisory.hints`), the Handover their follow-ups.

* `evaluate_advisories(profile, cfg)` — which advisories match this project (`advisories.policy_overrides
  {id: off}` switches an entry off).
* `followups_for(findings)` — one "from here on, your team …" sentence per finding, not-replaced first.


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
# matching helpers
# ---------------------------------------------------------------------------
def _concrete(v: Optional[str]) -> Optional[str]:
    """``${...}`` 참조가 아닌 실제 버전만(아니면 None)."""
    return v if v and "${" not in v else None


def gav_matches(pattern: str, g: GAV) -> bool:
    """``group:artifact-glob`` 패턴(fnmatch, 대소문자 구분)이 좌표에 맞는가. artifact 가 없으면 ``*``."""
    pg, _, pa = pattern.partition(":")
    return fnmatch.fnmatchcase(g.group_id, pg) and fnmatch.fnmatchcase(g.artifact_id, pa or "*")


def pom_gavs(poms: Iterable[Any]) -> list[GAV]:
    """모든 pom 의 의존성 → dependencyManagement → parent 좌표(이 순서). 권고 매칭의 재료.

    Dependencies, dependencyManagement entries and parents of every pom (in that order)."""
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
    """패턴 중 하나라도 맞는 좌표들."""
    pats = list(patterns)
    return [g for g in gavs if any(gav_matches(pt, g) for pt in pats)]


def _evidence_keys(evidence: Iterable[str]) -> list[str]:
    """분석기 근거 문자열(``pom:g:a:v`` 등)에서 ``g:a`` 키를 뽑는다(중복 없이)."""
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
    """``version`` < ``bound`` 인가. 모르거나 못 읽는 버전(릴리스 트레인 이름 등)은 "아래" 로 친다 — 권고를
    놓치는 것보다 한 번 더 안내하는 편이 낫다.

    True when `version` < `bound`; unknown / unparsable versions (release-train names) count as below."""
    from sbmig.plan.versions import version_lt

    r = version_lt(version, bound)
    return True if r is None else bool(r)


def _sort_key(f: AdvisoryFinding) -> tuple[int, str]:
    """정렬: eol → incompatible → unmanaged → superseded, 그다음 id."""
    return STATUS_ORDER.get(f.status, 9), f.advisory_id


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------
def switched_off(entry: LibraryAdvisory, cfg: Config) -> bool:
    """``advisories.policy_overrides`` 가 이 항목을 "off" 로
    껐는가(다른 값은 아무것도 바꾸지 않는다 — block 없음).

    True when `advisories.policy_overrides` sets the entry to "off" (any other value changes nothing)."""
    return (cfg.advisories.policy_overrides or {}).get(entry.id) == "off"


def _match(entry: LibraryAdvisory, profile: ProjectProfile, gavs: list[GAV]) -> Optional[dict[str, Any]]:
    """권고 하나가 이 프로젝트에 맞는가: 분석기가 찾은 라이브러리 이름 또는 pom 좌표 패턴. 맞으면
    라이브러리·근거· 아티팩트 키·감지 버전, 아니면 None."""
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
    """이 프로젝트의 권고 결과(findings). 안내 전용(``policy="guide"``)이며, 결과는 ``verify_advisories``
    전까지 "unknown". 케이스 타입(applies_to), 버전
    하한(applies_when_version_below), off 스위치를 차례로 본다.

    Advisory findings for this project (guide-only, outcome "unknown" until `verify_advisories`)."""
    if not cfg.advisories.enabled:
        return []
    cat = catalog if catalog is not None else load_advisories(cfg)
    if not len(cat):
        return []
    gavs = pom_gavs(profile.poms)
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
            followup=entry.followup.strip(), docs=list(entry.docs), notes=notes)
    return sorted(out.values(), key=_sort_key)


# ---------------------------------------------------------------------------
# follow-ups ("from here on, your team …")
# ---------------------------------------------------------------------------
def _replacement_hint(f: AdvisoryFinding) -> str:
    """후속 작업 문장 끝에 붙일 `` (replacement: …)``."""
    if not f.replacement:
        return ""
    return f" (replacement: {f.replacement}" + (f" {f.replacement_version}" if f.replacement_version else "") + ")"


def followups_for(findings: Optional[list[AdvisoryFinding]]) -> list[str]:
    """권고마다 실행할 수 있는 문장 하나: 교체 안 된 것(still-present / unknown) 먼저, 그다음 교체됐으니
    확인할 것. 인수인계의 "이제부터 할 일" 이 이 문장들과 겹치는 수동 후속 작업을 걸러 낸다.

    One actionable sentence per finding: not replaced (still-present / unknown) first, then replaced-but-verify."""
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
