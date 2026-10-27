"""v2.1 fix ladder (docs/v2/PLAN-v2.1.md §3.1): what a case gets next in this run.

Per case and run, in this order:

1. **KB search → stored script** (`reuse`, source `kb-script`) — the store's best version (`kb.cases.best(case_id)`:
   proven newest, else unverified newest) that did not run in this run yet: its code runs again, no LLM call. At most
   `script.reuse_versions` stored versions per case and run (default 1).
2. **LLM augment** (`augment`) — there is a script to extend: this run's last try of the case that had code (a stored
   version that failed here, or an LLM script), else — reuse switched off — the stored version. The prompt gets the
   script and what it did; the answer is a new version (source `llm-augment`) or, when the LLM says the fix cannot be
   a script (`no_script_reason`), edits applied directly (`llm-edit`).
3. **LLM new** (`new`) — nothing to extend: the LLM answers with 원인 · 해결 · edits · script (source `llm-new`, or
   `llm-edit` without a script).
4. `script.max_augments_per_case` (3) LLM tries used → nothing left: the decision engine BLOCKS the case
   (`repeated_failure`) — "케이스당 저장본 1 + LLM 3회".

`ScriptStep.kb_line` is the KB search RESULT the loop logs for the case ("KB 탐색: 저장된 스크립트 없음 (처음 보는
케이스) → LLM 에 새 수정 요청", "KB 탐색: 저장된 스크립트 v2 (proven, 성공 3 / 실패 0, 프로젝트 2) → 실행"; the phrase
is the store's `describe`): a first project must never read as a KB hit (company feedback 7).

Pure bookkeeping over the case's earlier attempts in this run (`FixAttempt.script_*`) and the case store (`kb.cases`,
None = no KB): the decision engine calls it to decide SCRIPT vs BLOCKED, the fix loop executes the step it returns. A
store problem is logged and treated as "nothing stored" — it never stops the loop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from sbmig.core.models import DecisionKind, ErrorCluster, FixAttempt

log = logging.getLogger("sbmig.triage")

LLM_MODES = ("new", "augment")              # script modes that ask the LLM (reuse runs a stored version)


@dataclass
class ScriptStep:
    """The next step of a case. `mode` None = the per-case limits are reached (→ BLOCKED)."""
    case_id: str
    mode: Optional[str] = None                      # reuse | augment | new | None
    version: int = 0                                # reuse: the stored version to run; augment from the store: base
    code: str = ""                                  # reuse: its code; augment from the store: the base code
    meta: Any = None                                # reuse / augment from the store: the stored CaseScript
    base_attempt: Optional[FixAttempt] = None       # augment: this run's try it extends
    llm_used: int = 0                               # LLM tries already made for the case in this run
    llm_limit: int = 0                              # the case's LLM try limit (script.max_augments_per_case)
    stored_used: int = 0                            # stored versions already run for the case in this run
    augments_used: int = 0                          # augment requests already made (escalation model from the 2nd)
    why: str = ""                                   # rationale (decision text, English)
    kb_line: str = ""                               # "KB 탐색: …" — the KB search result for the log
    kb_found: str = ""                              # what the KB search found ("저장된 스크립트 없음", "저장본 v2 (…)")

    @property
    def needs_llm(self) -> bool:
        return self.mode in LLM_MODES

    @property
    def base_version(self) -> int:
        """The version an augment extends (0 = an unstored script of this run)."""
        if self.base_attempt is not None:
            return int(self.base_attempt.script_version or 0)
        return int(getattr(self.meta, "version", 0) or 0) if self.meta is not None else 0


def script_attempts(attempts: Iterable[FixAttempt]) -> list[FixAttempt]:
    return [a for a in attempts if a.decision.kind == DecisionKind.SCRIPT]


def cluster_case_id(cluster: ErrorCluster) -> str:
    if cluster.case_id:
        return cluster.case_id
    from .cluster import case_id_of
    return case_id_of(cluster.errors[0]) if cluster.errors else ""


def stored_best(store: Any, case_id: str) -> Optional[tuple[Any, str]]:
    """(CaseScript, code) from `store.best(case_id)` (a `CaseScripts` adapter or E1's `CaseStore`); None = nothing
    stored or the store failed (logged)."""
    fn = getattr(store, "best", None) if store is not None else None
    if not callable(fn) or not case_id:
        return None
    try:
        got = fn(case_id)
    except Exception as e:  # noqa: BLE001 - the KB never stops the loop
        log.warning("kb.cases.best(%s) failed: %s: %s", case_id, type(e).__name__, e)
        return None
    if not got:
        return None
    try:
        meta, code = got
    except (TypeError, ValueError):
        return None
    if meta is None or not (code or "").strip() or getattr(meta, "status", "") == "retired":
        return None
    return meta, code


NOTHING_STORED = "저장된 스크립트 없음 (처음 보는 케이스)"


def describe_stored(meta: Any) -> str:
    """'저장된 스크립트 v2 (proven, 성공 3 / 실패 0, 프로젝트 2)' — the phrase of `CaseStore.describe`."""
    return (f"저장된 스크립트 v{getattr(meta, 'version', '?')} ({getattr(meta, 'status', '?')}, 성공 "
            f"{getattr(meta, 'successes', 0)} / 실패 {getattr(meta, 'failures', 0)}, 프로젝트 "
            f"{len(list(getattr(meta, 'projects', None) or []))})")


def kb_found(store: Any, case_id: str, best: Optional[tuple[Any, str]]) -> str:
    """What the KB search found, one phrase: the store's own `describe(case_id)` when it has one, else built here."""
    fn = getattr(store, "describe", None) if store is not None else None
    if callable(fn) and case_id:
        try:
            text = str(fn(case_id) or "").strip()
            if text:
                return text
        except Exception as e:  # noqa: BLE001 - the KB never stops the loop
            log.debug("kb.cases.describe(%s) failed: %s", case_id, e)
    return describe_stored(best[0]) if best is not None else NOTHING_STORED


def plan_script_step(cluster: ErrorCluster, previous: Iterable[FixAttempt], store: Any, cfg: Any) -> ScriptStep:
    """The case's next step (see the module doc). `previous` = this run's scored tries on the same case (the loop
    passes `_previous(cluster)`: no pending ones, no cooled-down transport failures)."""
    sc = getattr(cfg, "script", None)
    reuse_on = bool(getattr(sc, "reuse", True))
    reuse_n = max(1, int(getattr(sc, "reuse_versions", 1) or 1))
    max_llm = max(0, int(getattr(sc, "max_augments_per_case", 3)))
    case_id = cluster_case_id(cluster)
    tried = [a for a in script_attempts(previous) if a.outcome != "pending"]     # pending proves nothing yet
    tried_versions = {int(a.script_version) for a in tried if a.script_version}
    llm_used = sum(1 for a in tried if a.script_mode in LLM_MODES)
    stored_used = sum(1 for a in tried if a.script_mode == "reuse")
    step = ScriptStep(case_id=case_id, llm_used=llm_used, llm_limit=max_llm, stored_used=stored_used,
                      augments_used=sum(1 for a in tried if a.script_mode == "augment"))
    best = stored_best(store, case_id)
    stored_text = kb_found(store, case_id, best)
    step.kb_found = stored_text

    # 1. the stored script: a version not run in this run yet (no LLM call)
    if reuse_on and best is not None and stored_used < reuse_n:
        meta, code = best
        v = int(getattr(meta, "version", 0) or 0)
        if v not in tried_versions:
            step.mode, step.version, step.code, step.meta = "reuse", v, code, meta
            step.why = (f"reuse: stored script v{v} of case {case_id} ({getattr(meta, 'status', '?')}, "
                        f"{getattr(meta, 'successes', 0)} success(es) in {len(getattr(meta, 'projects', None) or [])} "
                        f"project(s), {getattr(meta, 'failures', 0)} failure(s)) — no LLM call")
            step.kb_line = f"KB 탐색: {stored_text} → 실행"
            return step

    failed_here = [a for a in tried if a.script_mode == "reuse"]
    stored_part = stored_text + (f" — 이번 실행에서 v{failed_here[-1].script_version} 실패" if failed_here else "")

    # 2./3. the LLM: limit first
    if llm_used >= max_llm:
        step.why = (f"{len(failed_here)} stored script run(s) and {llm_used} LLM tr{'y' if llm_used == 1 else 'ies'} "
                    f"did not resolve case {case_id} (script.max_augments_per_case {max_llm})")
        step.kb_line = (f"KB 탐색: {stored_part} → 한도 도달 (저장본 {len(failed_here)} + LLM {llm_used}회) → "
                        "blocked")
        return step
    nth = f"{llm_used + 1}/{max_llm}"
    base_att = next((a for a in reversed(tried) if (a.script_code or "").strip()), None)
    if base_att is not None:
        step.mode, step.base_attempt = "augment", base_att
        what = (f"this run's v{base_att.script_version} ({base_att.script_mode}, {base_att.outcome})"
                if base_att.script_version else f"this run's unstored script ({base_att.outcome})")
        step.why = f"augment {nth} of case {case_id}: extend {what}"
        base = (f"v{base_att.script_version}" if base_att.script_version else "이번 실행의 스크립트")
        step.kb_line = f"KB 탐색: {stored_part} → LLM 보강 요청 ({nth}, {base} 기준)"
        return step
    if best is not None:                            # reuse switched off: extend the stored version
        step.mode, step.meta, step.code = "augment", best[0], best[1]
        step.version = int(getattr(best[0], "version", 0) or 0)
        step.why = f"augment {nth} of case {case_id}: extend stored v{step.version} (script.reuse is off)"
        step.kb_line = f"KB 탐색: {stored_text} (재사용 꺼짐) → LLM 보강 요청 ({nth})"
        return step
    step.mode = "new"
    if llm_used:
        step.why = (f"new fix for case {case_id} (request {nth}: the earlier answer(s) had no script)")
        step.kb_line = f"KB 탐색: {stored_part} → LLM 에 새 수정 요청 ({nth})"
    else:
        step.why = f"new fix for case {case_id} (nothing stored)"
        step.kb_line = f"KB 탐색: {stored_text} → LLM 에 새 수정 요청"
    return step
