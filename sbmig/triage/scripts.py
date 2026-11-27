"""케이스가 이번 실행에서 다음에 받을 수정 단계(재사용 -> 보강 -> 신규)를 정하는 모듈.
재사용은 저장된 스크립트를 LLM 없이 다시 실행한다는 뜻이다. 한도에 도달하면 차단한다.

Fix ladder (docs/v2/PLAN-v2.1.md §3.1): what a case gets next in this run.

``DecisionEngine.decide`` calls ``plan_script_step`` and the fix loop runs the returned ``ScriptStep``. A
case blocked as ``repeated_failure`` goes to the Handover. An answer with direct edits only (no script) is
asked for at most ``script.max_edit_tries_per_case`` times. The LLM asks over every place of the case are
bounded by twice ``script.max_llm_tries_per_case``. A KB store problem is logged and read as "nothing
stored"; it never stops the loop. ``kb_line`` is worded so that a case seen for the first time never reads
like a KB hit.

Per case and run, in this order ("per case and run" = per case, run and set of places, docs/INTERFACES.md
"v2.1z-cd"; the fix loop passes the attempts made at the places of the case's errors now, and when none was
made there the stored version whose run resolved the case at another place in this run runs first,
`resolved_elsewhere`):

1. **KB search → stored script** (`reuse`, source `kb-script`) — the store's best version (`kb.cases.best(case_id)`:
   proven newest, else unverified newest) that did not run in this run yet: its code runs again, no LLM call.
   At most `script.reuse_versions` stored versions per case and run (default 1). With more than 1, a stored
   version that FAILED here (every reuse of this run failed: no_change / regressed / reverted / apply_failed /
   inconclusive) is followed by the next one in the same order — proven newest first, then unverified newest;
   retired never (`stored_next`, over `find(case_id).scripts` + `code(case_id, v)`). A reuse that helped
   (improved / partial) goes to the LLM augment instead: extending what worked beats starting over from an
   older version. With the run's scope known (`full` / `jdk-jakarta`, the fix loop's `CaseScripts.run_scope`),
   the order is (same scope, proven, version): the run's own scope first, at most one version of the other
   scope per case and run.
2. **LLM augment** (`augment`) — there is a script to extend and `script.max_augments_per_case` (15) augment steps
   are not used up: this run's last try of the case that had code (a stored version that failed here, or an
   LLM script), else — reuse switched off — the stored version. The prompt gets the script and what it did;
   the answer is a new version (source `llm-augment`) or, when the LLM says the fix cannot be a script
   (`no_script_reason`), edits applied directly (`llm-edit`).
3. **LLM new** (`new`) — nothing to extend, or the augments are used up (a fresh start): the LLM answers with
   원인 · 해결 · edits · script (source `llm-new`, or `llm-edit` without a script).
4. `script.max_llm_tries_per_case` (20) LLM asks (new + augment) used → nothing left: the decision engine BLOCKS the
   case (`repeated_failure`). Both caps are plain: `max_augments_per_case: 0` never augments,
   `max_llm_tries_per_case: 0` never asks the LLM for the case.

`ScriptStep.kb_line` is the KB search RESULT the loop logs for the case ("KB 탐색: 저장된 스크립트 없음 (처음 보는
케이스) → LLM 에 새 수정 요청", "KB 탐색: 저장된 스크립트 v2 (proven, 성공 3 / 실패 0, 프로젝트 2) → 실행"; the phrase
is the store's `describe`).

Pure bookkeeping over the case's earlier attempts in this run (`FixAttempt.script_*`) and the case store (`kb.cases`,
None = no KB).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from sbmig.core.models import DecisionKind, ErrorCluster, FixAttempt

log = logging.getLogger("sbmig.triage")

LLM_MODES = ("new", "augment")              # LLM을 호출하는 단계. 재사용은 저장된 버전을 실행한다
SCOPE_FULL = "full"                         # 범위가 빈 저장 버전은 범위를 기록하기 전의 것이라 full로 본다
# 차단 정책의 LLM 실패 횟수와 다음 저장 버전으로 넘어갈지 판단할 때 실패로 보는 결과. decide.py도 사용한다
FAILED_OUTCOMES = frozenset({"no_change", "regressed", "reverted", "apply_failed", "inconclusive"})


@dataclass
class ScriptStep:
    """케이스의 다음 단계를 담는 데이터 클래스.
    ``mode``가 None이면 케이스별 한도에 도달했다는 뜻이다. BLOCKED로 정한다."""
    case_id: str
    mode: Optional[str] = None                      # reuse | augment | new | None
    version: int = 0                                # 재사용할 저장 버전, 저장본을 보강하면 그 바탕 버전
    code: str = ""                                  # 그 버전의 코드
    meta: Any = None                                # 그 버전의 CaseScript
    base_attempt: Optional[FixAttempt] = None       # 보강이 이어받는 이번 실행의 시도
    llm_used: int = 0                               # 이번 실행에서 이 케이스에 한 LLM 요청 수(신규 + 보강)
    llm_limit: int = 0                              # 케이스의 LLM 요청 한도(script.max_llm_tries_per_case)
    stored_used: int = 0                            # 이번 실행에서 이미 실행한 저장 버전 수
    augments_used: int = 0                          # 이미 한 보강 요청 수. 두 번째부터 상위 모델을 쓴다
    augment_limit: int = 0                          # 케이스의 보강 한도(script.max_augments_per_case)
    why: str = ""                                   # 결정 근거 문장(영어)
    kb_line: str = ""                               # 로그에 남길 "KB 탐색: …" 줄
    kb_found: str = ""                              # KB 탐색이 찾은 것("저장된 스크립트 없음", "저장본 v2 (…)")

    @property
    def needs_llm(self) -> bool:
        """LLM을 호출해야 하는 단계(보강, 신규)인지 반환한다. 재사용은 LLM을 호출하지 않는다."""
        return self.mode in LLM_MODES

    @property
    def base_version(self) -> int:
        """보강이 이어받는 버전을 반환한다. 0은 이번 실행에서 만들어 아직 저장하지 않은 스크립트다."""
        if self.base_attempt is not None:
            return int(self.base_attempt.script_version or 0)
        return int(getattr(self.meta, "version", 0) or 0) if self.meta is not None else 0


def script_attempts(attempts: Iterable[FixAttempt]) -> list[FixAttempt]:
    """SCRIPT 결정으로 한 시도만 반환한다."""
    return [a for a in attempts if a.decision.kind == DecisionKind.SCRIPT]


def cluster_case_id(cluster: ErrorCluster) -> str:
    """클러스터의 케이스 ID를 반환한다. 없으면 첫 오류로 계산한다."""
    if cluster.case_id:
        return cluster.case_id
    from .cluster import case_id_of
    return case_id_of(cluster.errors[0]) if cluster.errors else ""


def _scope(value: Any) -> str:
    # 빈 값은 범위를 기록하기 전의 기록이라 full로 본다
    v = value.strip() if isinstance(value, str) else ""
    return v or SCOPE_FULL


def asking_scope(store: Any, scope: Optional[str] = None) -> Optional[str]:
    """재사용할 버전을 고를 실행의 이관 범위를 반환한다. 알 수 없으면 None을 반환한다.
    ``scope``가 없으면 저장소의 ``run_scope``를 쓴다.

    `run_scope` is set by the fix loop on its `CaseScripts` adapter: a string or a callable returning one.
    None = the store's own `best` order."""
    if isinstance(scope, str):
        return _scope(scope)
    rs = getattr(store, "run_scope", None) if store is not None else None
    try:
        rs = rs() if callable(rs) else rs
    except Exception:  # noqa: BLE001 - a foreign store
        rs = None
    return _scope(rs) if isinstance(rs, str) else None


def stored_best(store: Any, case_id: str, scope: Optional[str] = None) -> Optional[tuple[Any, str]]:
    """KB 저장소의 ``best(case_id)``에서 (CaseScript, 코드)를 가져온다.
    저장된 스크립트가 없으면 None을 반환한다. 코드가 비었거나, 사람이 끈 retired 버전이거나, 저장소 오류여도 None이다.
    ``scope``가 있으면 같은 범위의 버전을 먼저 고른다.
    비멱등이나 왕복(``round_trip``)으로 표시된 버전이면 다음 저장 버전을 쓴다.

    `store` = a `CaseScripts` adapter or a `CaseStore`. With `scope` and a `best` answer (None stays None:
    nothing usable, or a case id held by another signature), the order is `stored_next`'s (same scope, proven,
    version); a store that cannot list the scripts keeps `best`'s answer."""
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
    # 실행 범위로 다시 순위를 매긴다. 저장소의 best가 고를 것이 있다고 한 경우에만 한다(다른 서명의 케이스 ID는 None)
    # 범위가 기록된 버전이 하나도 없으면 모두 full이라 best의 순서와 같다
    if scope is not None and any(isinstance(getattr(x, "scope", ""), str) and getattr(x, "scope", "").strip()
                                 for x in _stored_scripts(store, case_id)):
        ranked = stored_next(store, case_id, (), scope=scope)
        if ranked is not None:
            return ranked
    # 여러 번 실행하면 결과가 달라지는 버전, 다른 케이스와 서로 되돌린 버전은 재사용하지 않는다. 다음 저장 버전을 쓴다
    # CaseStore.best는 이미 건너뛰지만 다른 저장소 구현에 대비한다
    if getattr(meta, "non_idempotent", False) or getattr(meta, "round_trip", False):
        return stored_next(store, case_id, ())
    return meta, code


def _stored_scripts(store: Any, case_id: str) -> list[Any]:
    """케이스의 저장된 스크립트 메타 목록을 반환한다.
    ``find(case_id).scripts``가 없으면 ``scripts(case_id)``를 쓴다. 읽지 못하면 빈 리스트를 반환한다."""
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


def stored_next(store: Any, case_id: str, skip: Iterable[int], scope: Optional[str] = None
                ) -> tuple[Any, str] | None:
    """이번 실행에서 이미 실행한 버전(``skip``)을 빼고 다음 저장 버전의 (CaseScript, 코드)를 반환한다.
    남은 것이 없으면 None을 반환한다.
    순서는 ``best``와 같이 proven 최신, unverified 최신 순이다. retired, 비멱등, 왕복, 빈 코드 버전은 건너뛴다.
    ``scope``가 있으면 같은 범위의 버전을 모두 먼저 보고, 다른 범위의 버전은 실행마다 하나까지만 고른다.

    With `scope`, rank = (same scope, proven, version); a version of the other scope is picked only when none
    of the run's scope is left and no other-scope version is in `skip` yet (one per case and run, within
    `script.reuse_versions`). A version's `scope` "" counts as `full`."""
    if store is None or not case_id:
        return None
    done = {int(v) for v in skip}
    scripts = _stored_scripts(store, case_id)
    want = _scope(scope) if scope is not None else None
    other_tried = want is not None and any(int(getattr(s, "version", 0) or 0) in done
                                           and _scope(getattr(s, "scope", "")) != want for s in scripts)
    ranked = sorted((s for s in scripts
                     if getattr(s, "status", "") != "retired" and not getattr(s, "non_idempotent", False)
                     and not getattr(s, "round_trip", False)
                     and int(getattr(s, "version", 0) or 0) not in done
                     and not (other_tried and _scope(getattr(s, "scope", "")) != want)),
                    key=lambda s: (want is None or _scope(getattr(s, "scope", "")) == want,
                                   getattr(s, "status", "") == "proven", int(getattr(s, "version", 0) or 0)),
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


def describe_stored(meta: Any, scope: Optional[str] = None) -> str:
    """저장된 스크립트 설명 구절(``저장된 스크립트 v2 (proven, 성공 3 / 실패 0, 프로젝트 2)``)을 만든다.
    ``scope``와 범위가 다른 버전이면 ``, 다른 범위: full``처럼 그 범위를 붙인다. ``CaseStore.describe``와 같은 구절이다."""
    own = _scope(getattr(meta, "scope", ""))
    other = f", 다른 범위: {own}" if scope is not None and own != _scope(scope) else ""
    return (f"저장된 스크립트 v{getattr(meta, 'version', '?')} ({getattr(meta, 'status', '?')}, 성공 "
            f"{getattr(meta, 'successes', 0)} / 실패 {getattr(meta, 'failures', 0)}, 프로젝트 "
            f"{len(list(getattr(meta, 'projects', None) or []))}{other})")


def kb_found(store: Any, case_id: str, best: Optional[tuple[Any, str]], scope: Optional[str] = None) -> str:
    """KB 탐색이 찾은 것을 한 구절로 만든다. 저장소에 ``describe(case_id)``가 있으면 그것을 쓴다.
    실행 범위를 알고 고른 버전에 범위가 기록돼 있으면 그 버전으로 직접 만든다.
    저장소의 ``describe``는 범위 없이 순위를 매기기 때문이다."""
    sc = getattr(best[0], "scope", "") if best is not None else ""
    if scope is not None and isinstance(sc, str) and sc.strip():
        return describe_stored(best[0], scope)
    fn = getattr(store, "describe", None) if store is not None else None
    if callable(fn) and case_id:
        try:
            text = str(fn(case_id) or "").strip()
            if text:
                return text
        except Exception as e:  # noqa: BLE001 - the KB never stops the loop
            log.debug("kb.cases.describe(%s) failed: %s", case_id, e)
    return describe_stored(best[0]) if best is not None else NOTHING_STORED


def _where(errors: Iterable[Any]) -> str:
    # 로그와 근거 문장에 쓸 위치 목록이다. 파일, 없으면 @모듈 순으로 세 개까지 쓴다
    seen = list(dict.fromkeys(
        p for p in ((getattr(e, "file", None) or "").replace("\\", "/") or
                    (f"@{getattr(e, 'module', None)}" if getattr(e, "module", None) else "") for e in errors) if p))
    return ", ".join(seen[:3]) + (", …" if len(seen) > 3 else "") if seen else "?"


def resolved_elsewhere(store: Any, case_id: str, elsewhere: Iterable[FixAttempt]
                       ) -> Optional[tuple[FixAttempt, Any, str]]:
    """다른 위치에서 저장 버전을 실행해 케이스를 해결한 가장 최근 시도와 그 버전의 (메타, 코드)를 반환한다.
    저장소에서 그 버전이 retired, 비멱등, 왕복으로 표시됐거나 코드가 없으면 None을 반환한다.

    "Resolved by running a stored version" = outcome improved / build_ok, version > 0, a script result, no
    edits applied (docs/INTERFACES.md "v2.1z-cd" rule 2)."""
    if store is None or not case_id:
        return None
    ok = [a for a in elsewhere
          if a.decision.kind == DecisionKind.SCRIPT and a.outcome in ("improved", "build_ok")
          and int(a.script_version or 0) > 0 and a.script_case_id in ("", case_id)
          and a.script_result is not None and not a.edits_applied]
    if not ok:
        return None
    a = ok[-1]
    v = int(a.script_version)
    meta = next((s for s in _stored_scripts(store, case_id) if int(getattr(s, "version", 0) or 0) == v), None)
    if meta is None or getattr(meta, "status", "") == "retired" or getattr(meta, "non_idempotent", False) \
            or getattr(meta, "round_trip", False):
        return None
    code_of = getattr(store, "code", None)
    try:
        code = code_of(case_id, v) if callable(code_of) else None
    except Exception as e:  # noqa: BLE001 - the KB never stops the loop
        log.warning("kb.cases.code(%s, v%s) failed: %s: %s", case_id, v, type(e).__name__, e)
        code = None
    if not (code or "").strip():
        return None
    return a, meta, str(code)


def plan_script_step(cluster: ErrorCluster, previous: Iterable[FixAttempt], store: Any, cfg: Any,
                     scope: Optional[str] = None, elsewhere: Iterable[FixAttempt] = (),
                     llm_total: Optional[int] = None) -> ScriptStep:
    """케이스의 다음 단계를 정한다. 순서는 모듈 docstring에 있다.
    ``previous``는 이 실행에서 같은 케이스에 대해 결과가 나온 시도 목록이다.
    저장된 스크립트는 이번 실행에서 아직 실행하지 않은 버전만 재사용한다. best가 이미 실패했으면
    ``script.reuse_versions``개까지 다음 저장 버전(``stored_next``)을 쓴다.
    ``elsewhere``는 다른 위치에서 이 케이스를 해결한 시도다. 여기서 한 시도가 없으면 그 버전을 먼저 다시 실행한다.
    ``llm_total``은 모든 위치에서 이 케이스에 한 LLM 요청 수다. ``script.max_llm_tries_per_case``의 두 배에 도달하면
    LLM을 호출할 단계 대신 차단한다. None이면 알 수 없는 것이라 제한하지 않는다.

    The loop passes `_previous(cluster)` as `previous`: no pending ones, no cooled-down transport failures.
    `llm_used` counts new + augment against `script.max_llm_tries_per_case`, `augments_used` the augments
    against `script.max_augments_per_case`. `scope` None = the store's `run_scope`. The `elsewhere` rerun is
    skipped when `script.reuse` is off. The `llm_total` bound blocks only a step that would ask the LLM (mode
    None → `repeated_failure`); a rerun or a stored reuse costs no ask and still runs."""
    sc = getattr(cfg, "script", None)
    previous = list(previous)
    reuse_on = bool(getattr(sc, "reuse", True))
    reuse_n = max(1, int(getattr(sc, "reuse_versions", 1) or 1))
    max_llm = max(0, int(getattr(sc, "max_llm_tries_per_case", 20)))
    max_aug = max(0, int(getattr(sc, "max_augments_per_case", 15)))
    case_id = cluster_case_id(cluster)
    tried = [a for a in script_attempts(previous) if a.outcome != "pending"]     # pending은 아직 결과가 없다
    tried_versions = {int(a.script_version) for a in tried if a.script_version}
    llm_used = sum(1 for a in tried if a.script_mode in LLM_MODES)
    stored_used = sum(1 for a in tried if a.script_mode == "reuse")
    augments_used = sum(1 for a in tried if a.script_mode == "augment")
    step = ScriptStep(case_id=case_id, llm_used=llm_used, llm_limit=max_llm, stored_used=stored_used,
                      augments_used=augments_used, augment_limit=max_aug)
    want = asking_scope(store, scope)
    best = stored_best(store, case_id, want)
    stored_text = kb_found(store, case_id, best, want)
    step.kb_found = stored_text

    failed_here = [a for a in tried if a.script_mode == "reuse"]
    stored_part = stored_text + (f" — 이번 실행에서 v{failed_here[-1].script_version} 실패" if failed_here else "")

    # 0. 여기서 한 시도가 없으면 다른 위치에서 이 케이스를 해결한 이번 실행의 버전을 LLM 없이 다시 실행한다
    prev_all = [a for a in previous if a.outcome != "pending"]
    if reuse_on and not prev_all and elsewhere:
        got = resolved_elsewhere(store, case_id, elsewhere)
        if got is not None:
            a, meta, code = got
            v = int(getattr(meta, "version", 0) or 0)
            there, here = _where(a.cluster_errors), _where(cluster.errors)
            step.mode, step.version, step.code, step.meta = "reuse", v, code, meta
            step.why = (f"reuse: this run's v{v} of case {case_id} resolved it at {there} ({a.id}: {a.outcome}); "
                        f"run again for {here} — no LLM call")
            step.kb_line = f"KB 탐색: 이번 실행의 v{v} ({a.id}, {there}에서 해결) → {here}에 다시 실행"
            return step

    # 1. 이번 실행에서 아직 실행하지 않은 저장 버전을 LLM 없이 실행한다
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
        # best가 이번 실행에서 실패했고 더 실행해도 되면 다음 저장 버전을 쓴다.
        # 도움이 된 재사용이 있었다면 그것을 LLM이 보강하는 편이 낫다
        if reuse_n > 1 and failed_here and all(a.outcome in FAILED_OUTCOMES for a in failed_here):
            nxt = stored_next(store, case_id, tried_versions, scope=want)
            if nxt is not None:
                meta, code = nxt
                v = int(getattr(meta, "version", 0) or 0)
                step.mode, step.version, step.code, step.meta = "reuse", v, code, meta
                step.why = (f"reuse: next stored script v{v} of case {case_id} ({getattr(meta, 'status', '?')}, "
                            f"{getattr(meta, 'successes', 0)} success(es), {getattr(meta, 'failures', 0)} "
                            f"failure(s)) after v{failed_here[-1].script_version} failed in this run "
                            f"(stored version {stored_used + 1}/{reuse_n}) — no LLM call")
                step.kb_line = (f"KB 탐색: {stored_part} → 다음 저장본 {describe_stored(meta, want)} 실행 "
                                f"({stored_used + 1}/{reuse_n})")
                return step

    # 2, 3. LLM 단계. 먼저 신규와 보강을 합친 한도를 본다
    # 모든 위치를 합친 LLM 요청이 케이스 한도의 두 배에 도달하면 위치가 바뀌어도 더 요청하지 않는다
    # max_llm 0은 아래에서 따로 기록한다
    bound = 2 * max_llm
    if max_llm > 0 and llm_total is not None and int(llm_total) >= bound:
        step.why = (f"{int(llm_total)} LLM tr{'y' if int(llm_total) == 1 else 'ies'} over every place did not resolve "
                    f"case {case_id} (bound across places: 2 × script.max_llm_tries_per_case {max_llm} = {bound}; "
                    f"{llm_used} at this place)")
        step.kb_line = (f"KB 탐색: {stored_part} → 위치 전체 한도 도달 (전체 LLM {int(llm_total)}회, 한도 {bound}회 = "
                        f"케이스 한도 {max_llm}회의 2배) → blocked")
        return step
    if llm_used >= max_llm:
        if max_llm == 0:
            # 한도 0은 이 케이스에 LLM을 호출하지 않는 설정이다. 해결에 실패한 것으로 읽히지 않게 따로 기록한다
            step.why = (f"LLM asks are switched off for case {case_id} (script.max_llm_tries_per_case 0)"
                        + (f"; {len(failed_here)} stored script run(s) did not resolve it" if failed_here else ""))
            step.kb_line = f"KB 탐색: {stored_part} → LLM 요청 없음 (script.max_llm_tries_per_case 0) → blocked"
            return step
        step.why = (f"{len(failed_here)} stored script run(s) and {llm_used} LLM tr{'y' if llm_used == 1 else 'ies'} "
                    f"did not resolve case {case_id} (script.max_llm_tries_per_case {max_llm})")
        step.kb_line = (f"KB 탐색: {stored_part} → 한도 도달 (저장본 {len(failed_here)} + LLM {llm_used}회) → "
                        "blocked")
        return step
    # LLM이 스크립트 없이 직접 수정(<no_script> + <edits>)만 하는 케이스는 다른 프로젝트가 재사용할 것이 없다.
    # 매번 이전 시도를 담은 새 요청이라서 script.max_edit_tries_per_case번까지만 요청한다
    max_edit = max(0, int(getattr(sc, "max_edit_tries_per_case", 4) or 0))
    llm_tried = [a for a in tried if a.script_mode in LLM_MODES]
    edit_tries = [a for a in llm_tried if not (getattr(a, "script_code", "") or "").strip()
                  and (getattr(a, "no_script_reason", "") or getattr(a, "edits_applied", 0))]
    if max_edit and llm_tried and len(edit_tries) == len(llm_tried) and len(edit_tries) >= max_edit:
        step.why = (f"{len(edit_tries)} direct-edit attempt(s) (no script: project code) did not resolve case {case_id} "
                    f"(script.max_edit_tries_per_case {max_edit}) — the attempts are in the report and the case history")
        step.kb_line = (f"KB 탐색: {stored_part} → 직접 수정 {len(edit_tries)}회 한도 도달 (script.max_edit_tries_per_case) → "
                        "blocked")
        return step
    nth = f"{llm_used + 1}/{max_llm}"
    can_augment = augments_used < max_aug
    aug_nth = f"보강 {augments_used + 1}/{max_aug}"
    # 코드가 있었던 마지막 시도를 보강의 기준으로 삼는다. 무엇을 했고 무엇이 남았는지 LLM에 함께 주기 위해서다.
    # 코드 없는 답만 있었으면 다시 신규로 요청한다
    base_att = next((a for a in reversed(tried) if (a.script_code or "").strip()), None)
    if base_att is not None and can_augment:
        step.mode, step.base_attempt = "augment", base_att
        what = (f"this run's v{base_att.script_version} ({base_att.script_mode}, {base_att.outcome})"
                if base_att.script_version else f"this run's unstored script ({base_att.outcome})")
        step.why = f"augment {nth} (augment {augments_used + 1}/{max_aug}) of case {case_id}: extend {what}"
        base = (f"v{base_att.script_version}" if base_att.script_version else "이번 실행의 스크립트")
        step.kb_line = f"KB 탐색: {stored_part} → LLM 보강 요청 ({nth}, {aug_nth}, {base} 기준)"
        return step
    if base_att is None and best is not None and can_augment:   # 재사용이 꺼져 있으면 저장 버전을 보강한다
        step.mode, step.meta, step.code = "augment", best[0], best[1]
        step.version = int(getattr(best[0], "version", 0) or 0)
        step.why = (f"augment {nth} (augment {augments_used + 1}/{max_aug}) of case {case_id}: extend stored "
                    f"v{step.version} (script.reuse is off)")
        step.kb_line = f"KB 탐색: {stored_text} (재사용 꺼짐) → LLM 보강 요청 ({nth}, {aug_nth})"
        return step
    step.mode = "new"
    if base_att is not None or best is not None:
        # 보강 한도를 다 썼다. 이어 붙이지 않고 처음부터 다시 쓰게 한다
        step.why = (f"new fix for case {case_id} (augments used up: script.max_augments_per_case {max_aug}; "
                    f"request {nth})")
        step.kb_line = f"KB 탐색: {stored_part} → 보강 한도({max_aug}) 소진 → LLM 에 새 수정 요청 ({nth})"
    elif llm_used:
        step.why = (f"new fix for case {case_id} (request {nth}: the earlier answer(s) had no script)")
        step.kb_line = f"KB 탐색: {stored_part} → LLM 에 새 수정 요청 ({nth})"
    else:
        step.why = f"new fix for case {case_id} (nothing stored)"
        step.kb_line = f"KB 탐색: {stored_text} → LLM 에 새 수정 요청"
    return step
