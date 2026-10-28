"""결정 엔진 — 케이스(클러스터) 하나에 이번 라운드에 무엇을 할지 정한다(SCRIPT / BLOCKED / SKIP / ESCALATE).
흐름상 위치: 수정 루프 → ``Triager.decide`` → ``DecisionEngine.decide``. SCRIPT 면 사다리
단계(``scripts.py``)를 ``script_steps[cluster.id]`` 에 남기고, 루프가 그 단계를 실행한다. 판단 순서: 이
실행에서 이미 차단됨 → 사람이 ``block`` 으로 고정한 케이스 → 저장된 스크립트 재사용(LLM 없음 — 그래서 차단
정책·LLM 예산·LLM 장애가 막지 않는다) → 차단 판정(``loop.blocked_policy`` 횟수만큼 LLM 시도 후 차단) → LLM
장애 → 사다리 소진(``repeated_failure``) → LLM 예산 → SCRIPT(보강 | 신규). BLOCKED 는 LLM 을 부르지 않고,
루프는 차단 항목을 남긴 채 나머지 케이스를 계속 고친다. ``plan_iteration`` 은 같은 모듈에 해결형 오류(parent /
pom / 의존성 / 플러그인)가 있으면 컴파일 케이스를 이번 라운드에서 가린다.

Decision engine (ARCHITECTURE §4, ERROR-HANDLING §1/§3; v2: docs/INTERFACES.md "Script fix loop (L)").

v2 (flow-v2) / v2.1 — one decision per case (L2) and round, the ladder of `sbmig.triage.scripts`:

    run-wide blocked registry → pinned `block` case action → KB search: the stored script (no LLM)
    → blocked check (cfg.loop.blocked_policy, counted in LLM tries) → LLM unavailable
    → ladder exhausted (repeated_failure: stored 1 + LLM 3) → LLM budget (`loop.max_llm_calls`, 0 = no limit)
    → SCRIPT augment | new

plus masking (`plan_iteration`). A BLOCKED decision never calls the LLM; the fix loop records a BlockedItem for it and
keeps working on everything else. A stored, proven script of the case is reused even when the LLM is unavailable or
its budget is spent — only the steps that need the LLM are blocked.

Round 10 rules kept: the per-case limits count only SCORED attempts of the SAME cluster (`same_cluster`: same cluster
id — a case's id is stable across rounds —, or same file, also after the restructure move, with an overlapping
signature); `pending` never counts. A case pinned to `block` by a person (`CaseHit.pinned`) is obeyed first."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from sbmig.core.config import Config
from sbmig.core.models import BlockReason, DecisionKind, ErrorCluster, FixAttempt, KBMatch, ProjectProfile, TriageDecision

from .blocked import BlockedCandidate, classify_blocked
from .cluster import FILE_KINDS, RESOLUTION_KINDS, cluster_label
from .scripts import FAILED_OUTCOMES, LLM_MODES, ScriptStep, plan_script_step, script_attempts

log = logging.getLogger("sbmig.triage")

# 시도 결과 분류. pending(이번 라운드에 적용했지만 아직
# 재빌드 전)은 아무것도 증명하지 않으므로 어디에도 세지 않는다
SUCCESS_OUTCOMES = frozenset({"improved", "build_ok"})
# scored failures: what the per-cluster limits count (round 10: no "pending" — an attempt applied this round and not
# built yet proves nothing; "inconclusive" = the scoring build could not show the cluster's errors, counts as tried).
# FAILED_OUTCOMES lives in .scripts since A14 (the ladder's "next stored version" reads it too); re-exported here.
# some targeted errors gone, the rest remain: progress, not a failure (v2: the next script attempt of the case is an
# augment of it — the per-case limit is script.max_augments_per_case)
PROGRESS_OUTCOMES = frozenset({"partial"})
# attempts that produced nothing usable (no script / it did not run / nothing applied / llm-error)
GARBAGE_OUTCOMES = frozenset({"apply_failed"})
# a pinned `block` action whose block_reason is free text (not a BlockReason value): the generic library-side reason
PINNED_BLOCK_FALLBACK = BlockReason.EXTERNAL_API_INCOMPATIBLE


def blocked_rationale(reason: BlockReason, detail: str) -> str:
    """BLOCKED 결정의 근거 문장 ``blocked: <이유> — <설명>`` (로그·보고서에 그대로 나간다)."""
    return f"blocked: {reason.value} — {detail}"


def _posix(path: Optional[str]) -> str:
    p = (path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def same_cluster(cluster: ErrorCluster, attempt: FixAttempt,
                 canon: Optional[Callable[[Optional[str]], str]] = None) -> bool:
    """``attempt`` 가 이 클러스터에 대한 시도였나: 같은 클러스터 id, 또는 재구성이 파일을 옮겨 id 가 바뀐 경우
    같은 파일(``canon`` 이 옮긴 경로를 지금 경로로 맞춤) + 겹치는 서명. 다른 파일이 L1 서명만 같으면 같은
    클러스터가 아니다 — 케이스별 시도 한도는 이 기준으로만 센다(round 10).

    Was `attempt` made on this cluster? Same cluster id, or — the cluster id changes when the restructure moves
    the file — the same file (`canon` maps a moved path to its current one, `BlockedRegistry.canonical_file`) with
    an overlapping signature. An attempt on ANOTHER file that shares an L1 signature is not the same cluster."""
    if attempt.cluster_id == cluster.id:
        return True
    norm = canon or _posix
    files = {norm(f) for f in [cluster.file, *(e.file for e in cluster.errors)] if f}
    if not files:
        return False
    a_files = {norm(e.file) for e in attempt.cluster_errors if e.file}
    if not files & a_files:
        return False
    return bool(set(cluster.signatures) & set(attempt.cluster_signatures))


def same_cluster_attempts(cluster: ErrorCluster, attempts: Iterable[FixAttempt],
                          canon: Optional[Callable[[Optional[str]], str]] = None) -> list[FixAttempt]:
    """``same_cluster`` 인 시도들만 — 케이스별 한도(재사용 1 + LLM 3)를 셀 때 쓴다."""
    return [a for a in attempts if same_cluster(cluster, a, canon)]


@dataclass
class IterationItem:
    """이번 라운드에 다룰 클러스터 하나. 가려졌으면 ``skip``
    에 SKIP 결정과 ``masked_by``(가린 클러스터 id)."""
    cluster: ErrorCluster
    skip: Optional[TriageDecision] = None          # SKIP decision when masked
    masked_by: list[str] = field(default_factory=list)


def related_attempts(cluster: ErrorCluster, attempts: Iterable[FixAttempt]) -> list[FixAttempt]:
    """같은 클러스터 id 이거나 서명이 하나라도 겹치는 시도(다른 파일 포함). round 10 부터는 프롬프트 힌트
    재료로만 쓰고, 한도 계산에는 쓰지 않는다(``same_cluster_attempts``).

    Same cluster id or ANY shared signature (other files included) — hint material only since round 10; the
    limits use `same_cluster_attempts`."""
    sigs = set(cluster.signatures)
    return [a for a in attempts if a.cluster_id == cluster.id or (sigs and sigs & set(a.cluster_signatures))]


def _modules(c: ErrorCluster) -> set[str]:
    """The modules a cluster's errors are in (a v2 case may span modules; its `module` is then None)."""
    mods = {e.module for e in c.errors if e.module}
    return mods or ({c.module} if c.module else set())


def plan_iteration(clusters: list[ErrorCluster]) -> list[IterationItem]:
    """가리기: 해결형 클러스터(parent / pom / 의존성 / 플러그인 해결 실패)가 있으면 같은 모듈(모듈이 없으면
    모든 모듈)의 컴파일 클러스터는 이번 라운드에 SKIP 한다. pom
    이 고쳐지면 컴파일 오류는 대개 사라지므로 LLM 호출을 아낀다.

    Masking: resolution-type clusters (parent/pom/dependency/plugin resolution) mask compile and test-compile
    clusters of the same module (or of every module when the resolution error has no module). A case spanning
    modules is masked when one of its modules has such a problem (the pom is fixed first)."""
    resolution = [c for c in clusters if c.kind in RESOLUTION_KINDS]
    out: list[IterationItem] = []
    for c in clusters:
        if c.kind in FILE_KINDS and resolution:
            mods = _modules(c)
            maskers = [r for r in resolution if r.module is None or r.module in mods]
            if maskers:
                names = ", ".join(cluster_label(r) for r in maskers[:3])
                out.append(IterationItem(c, TriageDecision(
                    cluster_id=c.id, kind=DecisionKind.SKIP,
                    rationale=f"masked this iteration by pom/resolution problem(s) in the same module: {names} — "
                              "compile errors usually disappear once the pom is fixed"),
                    [r.id for r in maskers]))
                continue
        out.append(IterationItem(c))
    return out


class DecisionEngine:
    """케이스 결정기. 실행 내내 하나가 살아 있으며, 루프가 읽어 가는 두 표를 채운다:
    ``blocked_info``(왜 차단했나 → BlockedItem) 와 ``script_steps``(사다리 단계 + KB 탐색 로그 줄)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # run-wide state attached by the build-fix loop (see Triager.attach_run)
        self.blocked_registry = None            # sbmig.loop.blocked_registry.BlockedRegistry
        self.ws = None                          # Workspace: project-class checks for the blocked rules
        self.llm_unavailable: Optional[str] = None
        self.cases: Any = None                  # v2.1: the KB's case store (`kb.cases` / CaseScripts), None = no KB
        # cluster id → why it was blocked (read by the loop to build the BlockedItem)
        self.blocked_info: dict[str, BlockedCandidate] = {}
        # cluster id → the ladder step of its last decision (read by the loop: what to run / ask for, the KB line)
        self.script_steps: dict[str, ScriptStep] = {}

    def _canon(self) -> Optional[Callable[[Optional[str]], str]]:
        """재구성이 옮긴 파일 경로를 지금 경로로 바꾸는 함수(차단 목록이 가진 것), 없으면 None."""
        fn = getattr(self.blocked_registry, "canonical_file", None)
        return fn if callable(fn) else None

    # -- pinned case actions (round 10) ----------------------------------
    @staticmethod
    def _pinned_hits(cluster: ErrorCluster, kb_match: KBMatch) -> list[Any]:
        """사람이 행동을 고정한(``CaseHit.pinned``) KB 케이스 중, 그 오류가 아직 이 클러스터에 있는 것들.

        Case hits whose action a person pinned (`CaseHit.pinned`) and whose errors are (still) in this cluster."""
        def ek(e) -> tuple:
            return (e.signature, _posix(e.file), e.line, e.message)
        mine = {ek(e) for e in cluster.errors}
        out = []
        for h in list(getattr(kb_match, "case_hits", None) or []):
            if not getattr(h, "pinned", False) or getattr(h, "action", None) is None:
                continue
            errs = [e for e in (getattr(h, "errors", None) or []) if ek(e) in mine]
            if errs or not getattr(h, "errors", None):
                out.append((h, errs or list(cluster.errors)))
        return out

    def _pinned_block(self, cluster: ErrorCluster, kb_match: KBMatch) -> Optional[TriageDecision]:
        """사람이 ``block`` 으로 고정한 케이스 → 그 사람의 이유 / 문장으로 BLOCKED(round 10). 스크립트도 LLM
        도 쓰지 않는다. 이유가 BlockReason 값이 아니면 일반 라이브러리 쪽 이유로 두고 문장을 제안 칸에 넣는다.

        A case a person pinned to `block` → BLOCKED with their reason / text (round 10). A pinned `script` version
        is the store's `best()` choice (`kb.cases`), not a decision here."""
        for h, errs in self._pinned_hits(cluster, kb_match):
            a = h.action
            if a.kind != "block":
                continue
            raw = (a.block_reason or "").strip()
            try:
                reason = BlockReason(raw)
                text = ""
            except ValueError:
                reason, text = PINNED_BLOCK_FALLBACK, raw
            why = f"pinned by case {h.case_id}" + (f" ({h.name})" if h.name else "") + (f": {text}" if text else "")
            sugg = " ".join(x for x in (text, (a.suggestion or "").strip()) if x)
            cand = BlockedCandidate(reason=reason, errors=list(errs), evidence=[why], case_id=h.case_id,
                                    suggestion=sugg or f"a person pinned case {h.case_id} to `block`: handle it by hand "
                                                       "(see `sbmig kb cases show " + h.case_id + "`)")
            return self._blocked(cluster, cand, " (pinned case action: no script, no LLM)")
        return None

    # -- main ----------------------------------------------------------
    def _blocked(self, cluster: ErrorCluster, cand: BlockedCandidate, extra: str = "") -> TriageDecision:
        """BLOCKED 결정을 만들고 그 이유를 ``blocked_info`` 에 남긴다(루프가 BlockedItem 을 만든다)."""
        self.blocked_info[cluster.id] = cand
        return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.BLOCKED,
                              rationale=blocked_rationale(cand.reason, cand.detail()) + extra)

    def decide(self, cluster: ErrorCluster, kb_match: Optional[KBMatch], ctx, profile: ProjectProfile) -> TriageDecision:
        """v2: SCRIPT(사다리 단계는 ``script_steps[cluster.id]``), BLOCKED, 또는 ESCALATE(케이스 ID 가 없을
        때). 판단 순서는 모듈 docstring 참고. 이 클러스터의 이전 결정 흔적은 먼저 지운다.

        v2: SCRIPT (with the ladder step in `script_steps[cluster.id]`), BLOCKED or ESCALATE (no case id)."""
        kb_match = kb_match or KBMatch()
        prev = same_cluster_attempts(cluster, ctx.previous_attempts, self._canon())
        label = cluster_label(cluster)
        self.blocked_info.pop(cluster.id, None)
        self.script_steps.pop(cluster.id, None)

        # 0. already blocked earlier in this run (any phase) → no script, no LLM
        reg = self.blocked_registry
        if reg is not None:
            item = reg.covering(cluster)
            if item is not None:
                cand = BlockedCandidate(reason=item.reason, required_type=item.required_type,
                                        found_type=item.found_type, owner_class=item.owner_class,
                                        evidence=[f"already blocked earlier in this run ({item.id})"],
                                        errors=list(cluster.errors))
                self.blocked_info[cluster.id] = cand
                return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.BLOCKED,
                                      rationale=f"blocked: {item.reason.value} — already blocked earlier in this run "
                                                f"({item.id}, {item.phase.value}); skipped")

        # 0b. a person pinned the case to `block` (`sbmig kb cases pin --action block…`)
        d = self._pinned_block(cluster, kb_match)
        if d is not None:
            return d

        # 1. 저장된 스크립트(``best``: proven 최신, 없으면 unverified 최신)부터 — LLM 호출이 없으므로 차단
        # 정책도 LLM 예산 / 장애도 막지 않는다. 500개 프로젝트를 도는 동안 LLM 호출을 줄이는 핵심 경로
        # 1. the script ladder: a proven stored version first — no LLM call, so neither the blocked policy nor the
        #    LLM budget / availability stops it
        step = plan_script_step(cluster, prev, self.cases, self.cfg)
        self.script_steps[cluster.id] = step          # also for a BLOCKED decision: its KB search line (the log)
        if not step.case_id:
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.ESCALATE,
                                  rationale=f"escalated: {label} has no case id (no errors)")
        if step.mode == "reuse":
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.SCRIPT, rationale=step.why)

        # 2. 프로젝트 안에서 못 고칠 것 같은 오류: blocked_policy[이유] 횟수만큼만 LLM 에 맡겨 보고 차단 →
        # 인수인계로 넘긴다(javax 라이브러리·없는 아티팩트·플러그인 2회, 사내 parent 미해결 등 0회)
        # 2. blocked check: cannot be fixed inside the project → blocked_policy[reason] LLM script attempts, then BLOCKED
        llm_failed = [a for a in script_attempts(prev) if a.script_mode in LLM_MODES and a.outcome in FAILED_OUTCOMES]
        policy_note = ""
        try:
            cand = classify_blocked(cluster, profile, self.cfg, ws=self.ws)
        except Exception:  # a classifier bug must never stop the loop
            # 분류기 버그로 루프가 멈추면 안 된다 — 판정 실패는 "차단 아님" 으로 보고 LLM 에게 기회를 준다
            cand = None
        if cand is not None:
            allowed = int(self.cfg.loop.blocked_policy.get(cand.reason.value, 0))
            if len(llm_failed) >= allowed:
                extra = (f" (after {len(llm_failed)} LLM script attempt(s))" if llm_failed else
                         " (policy: no LLM script for this reason)")
                return self._blocked(cluster, cand, extra)
            policy_note = (f"; looks like {cand.reason.value} ({cand.detail()}) — LLM script attempt "
                           f"{len(llm_failed) + 1}/{allowed} before blocking")

        # 3. the LLM steps (augment / new) → BLOCKED when the LLM cannot be asked
        if self.llm_unavailable:
            return self._blocked(cluster, BlockedCandidate(
                reason=BlockReason.LLM_UNAVAILABLE, errors=list(cluster.errors),
                evidence=[f"LLM unavailable ({self.llm_unavailable}); no stored script left to reuse for {label}"]))
        if step.mode is None:
            return self._blocked(cluster, BlockedCandidate(
                reason=BlockReason.REPEATED_FAILURE, errors=list(cluster.errors),
                evidence=[step.why + "; needs a human"]))
        # v2.1: 0 = 무제한, 양수 = 상한, -1 = LLM 호출 금지(플릿 전체 한도를 다 씀 — 저장본만 쓴다)
        # v2.1: 0 = no limit, > 0 = a cap, -1 = no LLM call at all (the fleet's total is used up)
        if ctx.llm_calls_budget < 0 or (ctx.llm_calls_budget > 0 and ctx.llm_calls_used >= ctx.llm_calls_budget):
            spent = ("fleet" if ctx.llm_calls_budget < 0 else f"{ctx.llm_calls_used}/{ctx.llm_calls_budget} calls")
            return self._blocked(cluster, BlockedCandidate(
                reason=BlockReason.BUDGET_EXHAUSTED, errors=list(cluster.errors),
                evidence=[f"LLM budget exhausted ({spent}) and no stored script left to reuse for {label}"]))
        why = step.why
        if ctx.stall_level:
            why += f"; stall level {ctx.stall_level}"
        return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.SCRIPT, rationale=why + policy_note)
