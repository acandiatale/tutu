"""v2.1 수정 사다리 — 이 실행에서 케이스가 다음에 받을 단계: 재사용 → 보강 → 신규 → (한도) 차단.
흐름상 위치: ``DecisionEngine.decide`` 가 ``plan_script_step`` 을 불러 SCRIPT / BLOCKED 를 정하고, 수정 루프가
돌려받은 ``ScriptStep`` 을 실행한다(재사용 = 저장된 스크립트 실행, LLM 0회). 한도: 케이스당 한 실행에서 저장본
``script.reuse_versions``(1) + LLM ``script.max_augments_per_case``(3) — "케이스당 저장본 1 + LLM 3회". 2 이상이면
이번 실행에서 실패한 저장본 다음 버전(proven 최신 → unverified 최신 순)을 LLM 전에 더 돌린다(A14). 다
쓰면 ``repeated_failure`` 로 차단되어 인수인계로 간다. ``kb_line`` 은 KB 탐색 "결과" 로그 줄이다: 처음 보는
프로젝트가 KB 적중처럼 읽히면 안 된다(회사 피드백 7). KB 저장소
문제는 로그만 남기고 "저장본 없음" 으로 다룬다 — 루프를 멈추지 않는다. v2.1i ``fix.mode: edits`` 면 저장소를
보지 않고 신규(LLM)만 — 케이스당 ``fix.edits_max_asks_per_case`` 회 뒤 차단, 0 이면 무제한(``edits_llm_limit``).

v2.1 fix ladder (docs/v2/PLAN-v2.1.md §3.1): what a case gets next in this run.

Per case and run, in this order:

1. **KB search → stored script** (`reuse`, source `kb-script`) — the store's best version (`kb.cases.best(case_id)`:
   proven newest, else unverified newest) that did not run in this run yet: its code runs again, no LLM call. At most
   `script.reuse_versions` stored versions per case and run (default 1). With more than 1, a stored version that
   FAILED here (every reuse of this run failed: no_change / regressed / reverted / apply_failed / inconclusive) is
   followed by the next one in the same order — proven newest first, then unverified newest; retired never
   (`stored_next`, over `find(case_id).scripts` + `code(case_id, v)`). A reuse that helped (improved / partial) goes
   to the LLM augment instead: extending what worked beats starting over from an older version.
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

v2.1i `fix.mode: edits` (the token comparison, 2026-10-28): no rung but `new` — the store is not consulted (no reuse,
no augment); a case gets `new` while its LLM tries in this run are below `fix.edits_max_asks_per_case` (0 = always)
(`edits_llm_limit`), then `mode` None (BLOCKED `repeated_failure`). `loop.blocked_policy` and the LLM budget apply as
in `script` mode (the decision engine's checks are unchanged).


"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from sbmig.core.models import DecisionKind, ErrorCluster, FixAttempt

log = logging.getLogger("sbmig.triage")

LLM_MODES = ("new", "augment")              # script modes that ask the LLM (reuse runs a stored version)
# 실패로 보는 채점 결과(차단 정책의 LLM 실패 횟수, 다음 저장본으로 넘어갈지) — decide.py 도 이것을 쓴다
FAILED_OUTCOMES = frozenset({"no_change", "regressed", "reverted", "apply_failed", "inconclusive"})


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
    stored or the store failed (logged). v2.1y: a `non_idempotent` best (a store that does not skip it) → the next
    stored version (`stored_next`)."""
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
    # v2.1y: 비멱등으로 표시된 버전은 재사용하지 않는다(CaseStore.best 가 이미 건너뛴다; 다른 저장소 대역에 대비) — 다음 저장본
    if getattr(meta, "non_idempotent", False):
        return stored_next(store, case_id, ())
    return meta, code


def _stored_scripts(store: Any, case_id: str) -> list[Any]:
    """케이스의 저장된 스크립트 메타 목록: ``find(case_id).scripts``(CaseStore · 루프의 CaseScripts 어댑터
    공통), 없으면 ``scripts(case_id)``. 못 읽으면 [].

    The case's stored CaseScripts: `find(case_id).scripts` (CaseStore and the loop's CaseScripts adapter alike),
    else `scripts(case_id)`; [] when the store cannot say (logged)."""
    try:
        find = getattr(store, "find", None)
        if callable(find):
            info = find(case_id)
            if info is not None:
                return list(getattr(info, "scripts", None) or [])
        fn = getattr(store, "scripts", None)
        return list(fn(case_id) or []) if callable(fn) else []
    except Exception as e:  # noqa: BLE001 - the KB never stops the loop
        log.warning("kb.cases scripts of %s unavailable: %s: %s", case_id, type(e).__name__, e)
        return []


def stored_next(store: Any, case_id: str, skip: Iterable[int]) -> tuple[Any, str] | None:
    """``skip`` (이번 실행에서 이미 돈 버전)을 뺀 다음 저장본 (CaseScript, 코드): ``best`` 와 같은 순서 —
    proven 최신 → unverified 최신, retired·비멱등(v2.1y)·빈 코드는 건너뜀. 없으면 None(A14, ``script.reuse_versions`` > 1).

    The next stored version not in `skip` (the versions this run already ran), in `best`'s order: proven newest,
    then unverified newest; retired ones, `non_idempotent` ones (v2.1y) and versions without code never. None =
    nothing left (A14)."""
    if store is None or not case_id:
        return None
    done = {int(v) for v in skip}
    ranked = sorted((s for s in _stored_scripts(store, case_id)
                     if getattr(s, "status", "") != "retired" and not getattr(s, "non_idempotent", False)
                     and int(getattr(s, "version", 0) or 0) not in done),
                    key=lambda s: (getattr(s, "status", "") == "proven", int(getattr(s, "version", 0) or 0)),
                    reverse=True)
    code_of = getattr(store, "code", None)
    if not callable(code_of):
        return None
    for s in ranked:
        try:
            code = code_of(case_id, int(getattr(s, "version", 0) or 0))
        except Exception as e:  # noqa: BLE001 - the KB never stops the loop
            log.warning("kb.cases.code(%s, v%s) failed: %s: %s", case_id, getattr(s, "version", "?"),
                        type(e).__name__, e)
            continue
        if (code or "").strip():
            return s, str(code)
    return None


NOTHING_STORED = "저장된 스크립트 없음 (처음 보는 케이스)"
EDITS_MODE_FOUND = "fix.mode edits — 저장 스크립트 사용 안 함"   # v2.1i: the KB search phrase of an edits-mode run


def fix_mode(cfg: Any) -> str:
    """설정의 수정 방식(``fix.mode``: script | edits, 없거나 비면 script).

    `fix.mode` of the config (`script` | `edits`); `script` when the config has none (older configs, test doubles)."""
    mode = getattr(getattr(cfg, "fix", None), "mode", "script")
    return str(mode or "script")


def edits_mode(cfg: Any) -> bool:
    """v2.1i: ``fix.mode`` 가 edits 인가(스크립트 생성·저장·재사용 없이 LLM 직접 수정만).

    v2.1i: is `fix.mode` `edits` (LLM edits only — no script generated, stored, reused or augmented)?"""
    return fix_mode(cfg) == "edits"


def edits_llm_limit(cfg: Any) -> int:
    """v2.1i edits 모드의 케이스당 LLM 요청 한도 = ``fix.edits_max_asks_per_case``. 0 = 무제한(기본, 2026-10-29 사용자:
    "제한 없이 수정하도록 — LLM 한테 다 맡겼을 때의 토큰량을 보여 줄 수 있게"), 양수 = 그만큼 묻고 ``repeated_failure``
    로 차단(3 이면 script 모드가 저장본 없는 케이스에 묻는 횟수와 같다).

    The per-case LLM asks of an edits-mode run = `fix.edits_max_asks_per_case`: 0 = no cap (default; the whole fix is
    left to the LLM — the rounds, the time budget and `loop.max_llm_calls` still stop a run), > 0 = BLOCKED
    `repeated_failure` after that many (3 = what `script` mode asks for a case without a stored script)."""
    fx = getattr(cfg, "fix", None)
    try:
        return max(0, int(getattr(fx, "edits_max_asks_per_case", 0) or 0))
    except (TypeError, ValueError):
        return 0


def edits_unlimited(cfg: Any) -> bool:
    """edits 모드이고 케이스당 한도가 0(무제한)인가 — 차단 정책의 양수 횟수도 적용하지 않는다.

    True when the run is in edits mode with no per-case cap (then the positive `loop.blocked_policy` counts are lifted too)."""
    return edits_mode(cfg) and edits_llm_limit(cfg) == 0


def plan_edits_step(cluster: ErrorCluster, previous: Iterable[FixAttempt], cfg: Any) -> ScriptStep:
    """v2.1i edits 모드의 다음 단계: 저장소는 보지 않는다(재사용·보강 없음). 이 실행에서 케이스의 LLM 요청이
    ``edits_llm_limit`` 보다 적으면 신규(앞 시도들은 수정 루프가 FixContext 에 싣는다), 아니면 mode None(→ 차단).

    The next step of a case in an edits-mode run: the store is never consulted (no reuse, no augment). `new` while
    the case's LLM tries in this run are below `edits_llm_limit(cfg)` (the loop passes the earlier tries in the
    FixContext), else `mode` None (→ BLOCKED repeated_failure). `previous` = this run's scored tries on the case."""
    limit = edits_llm_limit(cfg)
    case_id = cluster_case_id(cluster)
    tried = [a for a in script_attempts(previous) if a.outcome != "pending"]
    llm_used = sum(1 for a in tried if a.script_mode in LLM_MODES)
    step = ScriptStep(case_id=case_id, llm_used=llm_used, llm_limit=limit, kb_found=EDITS_MODE_FOUND)
    if limit and llm_used >= limit:
        step.why = (f"{llm_used} LLM tr{'y' if llm_used == 1 else 'ies'} did not resolve case {case_id} "
                    f"(fix.mode edits: fix.edits_max_asks_per_case = {limit})")
        step.kb_line = f"KB 탐색: {EDITS_MODE_FOUND} → 한도 도달 (LLM {llm_used}회) → blocked"
        return step
    step.mode = "new"
    nth = f"{llm_used + 1}/{limit}" if limit else f"{llm_used + 1}번째, 한도 없음"
    if llm_used:
        step.why = f"new edits for case {case_id} (fix.mode edits, request {nth}: the earlier tries did not resolve it)"
        step.kb_line = f"KB 탐색: {EDITS_MODE_FOUND} → LLM 에 다시 수정 요청 ({nth}, 앞 시도 {llm_used}건 포함)"
    else:
        step.why = f"new edits for case {case_id} (fix.mode edits, request {nth})"
        step.kb_line = f"KB 탐색: {EDITS_MODE_FOUND} → LLM 에 수정 요청 ({nth})"
    return step


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
    뺐다). 저장본은 이 실행에서 아직 안 돌린 버전만 재사용한다 — best 가 이미 실패했으면
    ``script.reuse_versions`` 개까지 다음 저장본(``stored_next``).

    The case's next step (see the module doc). `previous` = this run's scored tries on the same case (the loop
    passes `_previous(cluster)`: no pending ones, no cooled-down transport failures). v2.1i: `fix.mode: edits` →
    `plan_edits_step` (the store is not consulted)."""
    if edits_mode(cfg):
        return plan_edits_step(cluster, previous, cfg)
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

    failed_here = [a for a in tried if a.script_mode == "reuse"]
    stored_part = stored_text + (f" — 이번 실행에서 v{failed_here[-1].script_version} 실패" if failed_here else "")

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
        # A14: best 가 이번 실행에서 실패했고 저장본을 더 돌려도 되면(reuse_versions > 1) 다음 저장본 — 도움이 된
        # 재사용(improved / partial)이 있었으면 그것을 LLM 이 보강하는 편이 낫다
        if reuse_n > 1 and failed_here and all(a.outcome in FAILED_OUTCOMES for a in failed_here):
            nxt = stored_next(store, case_id, tried_versions)
            if nxt is not None:
                meta, code = nxt
                v = int(getattr(meta, "version", 0) or 0)
                step.mode, step.version, step.code, step.meta = "reuse", v, code, meta
                step.why = (f"reuse: next stored script v{v} of case {case_id} ({getattr(meta, 'status', '?')}, "
                            f"{getattr(meta, 'successes', 0)} success(es), {getattr(meta, 'failures', 0)} "
                            f"failure(s)) after v{failed_here[-1].script_version} failed in this run "
                            f"(stored version {stored_used + 1}/{reuse_n}) — no LLM call")
                step.kb_line = (f"KB 탐색: {stored_part} → 다음 저장본 {describe_stored(meta)} 실행 "
                                f"({stored_used + 1}/{reuse_n})")
                return step

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
