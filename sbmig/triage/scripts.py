"""v2.1 수정 사다리 — 이 실행에서 케이스가 다음에 받을 단계: 재사용 → 보강 → 신규 → (한도) 차단.
흐름상 위치: ``DecisionEngine.decide`` 가 ``plan_script_step`` 을 불러 SCRIPT / BLOCKED 를 정하고, 수정 루프가
돌려받은 ``ScriptStep`` 을 실행한다(재사용 = 저장된 스크립트 실행, LLM 0회). 한도: 케이스당 한 실행에서 저장본
``script.reuse_versions``(1) + LLM ``script.max_augments_per_case``(3) — "케이스당 저장본 1 + LLM 3회". 다
쓰면 ``repeated_failure`` 로 차단되어 인수인계로 간다. ``kb_line`` 은 KB 탐색 "결과" 로그 줄이다: 처음 보는
프로젝트가 KB 적중처럼 읽히면 안 된다(회사 피드백 7). KB 저장소
문제는 로그만 남기고 "저장본 없음" 으로 다룬다 — 루프를 멈추지 않는다.

v2.1 fix ladder (docs/v2/PLAN-v2.1.md §3.1): what a case gets next in this run.

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
    """케이스의 다음 단계. ``mode`` 가 None 이면 케이스별 한도에 도달한 것(→ BLOCKED).

    The next step of a case. `mode` None = the per-case limits are reached (→ BLOCKED)."""
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
        """LLM 을 불러야 하는 단계인가(보강 / 신규). 재사용은 LLM 을 부르지 않는다."""
        return self.mode in LLM_MODES

    @property
    def base_version(self) -> int:
        """보강이 이어받는 버전(0 = 이 실행에서 만든, 아직 저장 안 된 스크립트).

        The version an augment extends (0 = an unstored script of this run)."""
        if self.base_attempt is not None:
            return int(self.base_attempt.script_version or 0)
        return int(getattr(self.meta, "version", 0) or 0) if self.meta is not None else 0


def script_attempts(attempts: Iterable[FixAttempt]) -> list[FixAttempt]:
    """SCRIPT 결정으로 한 시도들만."""
    return [a for a in attempts if a.decision.kind == DecisionKind.SCRIPT]


def cluster_case_id(cluster: ErrorCluster) -> str:
    """클러스터의 케이스 ID(없으면 첫 오류에서 계산)."""
    if cluster.case_id:
        return cluster.case_id
    from .cluster import case_id_of
    return case_id_of(cluster.errors[0]) if cluster.errors else ""


def stored_best(store: Any, case_id: str) -> Optional[tuple[Any, str]]:
    """KB 저장소의 ``best(case_id)`` 에서 (CaseScript, 코드).
    저장본 없음 · 빈 코드 · retired(사람이 끔) · 저장소 오류면 None.

    (CaseScript, code) from `store.best(case_id)` (a `CaseScripts` adapter or E1's `CaseStore`); None = nothing
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
    """저장본 설명 구절 ``저장된 스크립트 v2 (proven, 성공 3 / 실패 0, 프로젝트 2)``
    (``CaseStore.describe`` 와 같은 꼴).

    '저장된 스크립트 v2 (proven, 성공 3 / 실패 0, 프로젝트 2)' — the phrase of `CaseStore.describe`."""
    return (f"저장된 스크립트 v{getattr(meta, 'version', '?')} ({getattr(meta, 'status', '?')}, 성공 "
            f"{getattr(meta, 'successes', 0)} / 실패 {getattr(meta, 'failures', 0)}, 프로젝트 "
            f"{len(list(getattr(meta, 'projects', None) or []))})")


def kb_found(store: Any, case_id: str, best: Optional[tuple[Any, str]]) -> str:
    """KB 탐색이 찾은 것 한 구절: 저장소의 ``describe(case_id)`` 가 있으면 그것, 없으면 여기서 만든다.

    What the KB search found, one phrase: the store's own `describe(case_id)` when it has one, else built here."""
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
    """케이스의 다음 단계를 정한다(모듈 docstring 의 순서). ``previous`` = 이 실행에서 같은 케이스에 대해
    채점된 시도들(pending 과 쉬는 중인 전송 실패는 루프가 이미
    뺐다). 저장본은 이 실행에서 아직 안 돌린 버전만 재사용한다.

    The case's next step (see the module doc). `previous` = this run's scored tries on the same case (the loop
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
    # 보강의 기준: 이 실행에서 코드가 있었던 마지막 시도(실패한 저장본 또는 LLM 스크립트) — 무엇을 했고 무엇이
    # 남았는지를 LLM 에 함께 준다. 코드 없는 답(no_script)만 있었으면 다시 신규 요청
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
