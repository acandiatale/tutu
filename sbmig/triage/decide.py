"""케이스 하나에 이번 라운드에 할 일(SCRIPT / BLOCKED / SKIP / ESCALATE)을 정하는 결정 엔진 모듈.
수정 루프가 ``Triager.decide``를 통해 ``DecisionEngine.decide``를 호출한다.
SCRIPT면 시도 단계(``scripts.py``)를 ``script_steps[cluster.id]``에 남기고 루프가 그 단계를 실행한다.
판단 순서:
1. 이 실행에서 이미 차단됨
2. 사람이 ``block``으로 고정한 케이스
3. 저장소 응답 없음. 그 이유의 차단 정책이 0이면 저장된 스크립트도 실행하지 않고 차단한다
4. 저장된 스크립트 재사용. LLM을 호출하지 않으므로 차단 정책, LLM 예산, LLM 장애에 막히지 않는다
5. 차단 판단(``loop.blocked_policy`` 횟수만큼 LLM으로 시도한 뒤 차단)
6. LLM 장애, 시도 횟수 소진(``repeated_failure``), LLM 예산
7. SCRIPT(보강 또는 신규)
- BLOCKED는 LLM을 호출하지 않는다. 루프는 차단 항목을 남긴 채 나머지 케이스를 계속 고친다
- ``replacement: true`` 노트가 맞는 라이브러리 차단 후보는 프로젝트 코드로 고칠 수 있으므로 차단하지 않는다
- 케이스별 한도는 같은 클러스터에 대한, 결과가 나온 시도만 센다. pending은 세지 않는다
- ``plan_iteration``은 같은 모듈에 의존성 해결 오류가 있으면 컴파일 케이스를 이번 라운드에서 건너뛴다

Decision engine (ARCHITECTURE §4, ERROR-HANDLING §5/§6; docs/INTERFACES.md "Script fix loop (L)").

One decision per case (L2) and round, the ladder of `sbmig.triage.scripts`:

    run-wide blocked registry → pinned `block` case action → KB search: the stored script (no LLM)
    → blocked check (cfg.loop.blocked_policy, counted in LLM tries) → LLM unavailable
    → ladder exhausted (repeated_failure: stored 1 + script.max_llm_tries_per_case LLM asks)
    → LLM budget (`loop.max_llm_calls`, 0 = no limit)
    → SCRIPT augment | new

plus masking (`plan_iteration`). A BLOCKED decision never calls the LLM; the fix loop records a BlockedItem for it and
keeps working on everything else. A stored, proven script of the case is reused even when the LLM is unavailable or
its budget is spent — only the steps that need the LLM are blocked.

The per-case limits count only SCORED attempts of the SAME cluster (`same_cluster`: same cluster
id — a case's id is stable across rounds —, or same file, also after the restructure move, with an overlapping
signature); `pending` never counts. A case pinned to `block` by a person (`CaseHit.pinned`) is obeyed first."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from sbmig.core.config import Config
from sbmig.core.models import (
    BlockReason,
    BuildError,
    DecisionKind,
    ErrorCluster,
    FixAttempt,
    KBMatch,
    ProjectProfile,
    TriageDecision,
)

from .blocked import BlockedCandidate, classify_blocked, classify_transfer, note_text
from .cluster import FILE_KINDS, RESOLUTION_KINDS, cluster_label
from .scripts import FAILED_OUTCOMES, LLM_MODES, ScriptStep, plan_script_step, script_attempts

log = logging.getLogger("sbmig.triage")

# 시도 결과 분류. pending은 적용만 하고 아직 재빌드하지 않아 아무것도 증명하지 않으므로 어디에도 세지 않는다
SUCCESS_OUTCOMES = frozenset({"improved", "build_ok"})
# 실패로 세는 결과 FAILED_OUTCOMES는 scripts.py에 있다. inconclusive는 평가 빌드가 오류를 보여 주지 못한 경우로 시도한 것으로 센다
# 일부 오류만 사라진 시도는 실패가 아니라 진행이다. 그 케이스의 다음 시도는 이 시도의 보강이 된다
PROGRESS_OUTCOMES = frozenset({"partial"})
# 쓸 만한 것을 만들지 못한 시도: 스크립트 없음, 실행되지 않음, 적용된 것 없음, llm-error
GARBAGE_OUTCOMES = frozenset({"apply_failed"})
# 사람이 고정한 block의 이유가 BlockReason 값이 아닌 자유 문장일 때 쓰는 일반 라이브러리 이유
PINNED_BLOCK_FALLBACK = BlockReason.EXTERNAL_API_INCOMPATIBLE


def blocked_rationale(reason: BlockReason, detail: str) -> str:
    """BLOCKED 결정의 근거 문장을 만든다. 로그와 보고서에 그대로 출력된다."""
    return f"blocked: {reason.value} — {detail}"


def _posix(path: Optional[str]) -> str:
    p = (path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def same_cluster(cluster: ErrorCluster, attempt: FixAttempt,
                 canon: Optional[Callable[[Optional[str]], str]] = None) -> bool:
    """``attempt``가 이 클러스터에 대한 시도였는지 확인한다.
    클러스터 id가 같거나, 재구성이 파일을 옮겨 id가 바뀌었을 때 같은 파일이면서 서명이 겹치면 같은 클러스터로 본다. 다른
    파일이 L1 서명만 같으면 같은 클러스터가 아니다. 케이스별 시도 한도는 이 기준으로 센다.

    `canon` maps a moved path to its current one (`BlockedRegistry.canonical_file`)."""
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


def error_place(e: BuildError, canon: Optional[Callable[[Optional[str]], str]] = None) -> str:
    """오류의 위치를 반환한다. 파일이 있으면 ``canon``으로 옮긴 현재 경로, 없고 모듈이 있으면 ``@<module>``을 반환한다.
    둘 다 없으면 빈 문자열을 반환한다.

    Same rule as the fix loop's `_place` (docs/INTERFACES.md "v2.1z-cb")."""
    if e.file:
        return (canon or _posix)(e.file)
    return f"@{e.module}" if e.module else ""


def error_places(errors: Iterable[BuildError], canon: Optional[Callable[[Optional[str]], str]] = None) -> set[str]:
    """오류들의 위치 집합을 반환한다. 위치가 없는 오류('')는 넣지 않는다."""
    return {p for p in (error_place(e, canon) for e in errors) if p}


def made_here(cluster: ErrorCluster, attempt: FixAttempt,
              canon: Optional[Callable[[Optional[str]], str]] = None) -> bool:
    """케이스의 이전 시도가 지금 오류가 있는 위치에서 한 시도인지 확인한다.
    시도가 겨냥한 오류의 위치 중 하나가 지금 오류의 위치면 여기서 한 시도다.
    어느 한쪽 위치를 알 수 없으면 여기서 한 시도로 본다.

    docs/INTERFACES.md "v2.1z-cd"."""
    mine = error_places(cluster.errors, canon)
    theirs = error_places(attempt.cluster_errors, canon)
    return not mine or not theirs or bool(mine & theirs)


def same_cluster_attempts(cluster: ErrorCluster, attempts: Iterable[FixAttempt],
                          canon: Optional[Callable[[Optional[str]], str]] = None) -> list[FixAttempt]:
    """``same_cluster``가 참인 시도만 반환한다. 케이스별 시도 한도를 셀 때 사용한다."""
    return [a for a in attempts if same_cluster(cluster, a, canon)]


@dataclass
class IterationItem:
    """이번 라운드에 다룰 클러스터 하나를 담는 데이터 클래스.
    건너뛰는 경우 ``skip``에 SKIP 결정을, ``masked_by``에 원인 클러스터 id를 담는다."""
    cluster: ErrorCluster
    skip: Optional[TriageDecision] = None          # 가려져서 건너뛸 때의 SKIP 결정
    masked_by: list[str] = field(default_factory=list)


def related_attempts(cluster: ErrorCluster, attempts: Iterable[FixAttempt]) -> list[FixAttempt]:
    """같은 클러스터 id이거나 서명이 하나라도 겹치는 시도를 다른 파일까지 포함해 반환한다.
    프롬프트 힌트로만 쓰고 한도 계산에는 쓰지 않는다. 한도는 ``same_cluster_attempts``로 센다."""
    sigs = set(cluster.signatures)
    return [a for a in attempts if a.cluster_id == cluster.id or (sigs and sigs & set(a.cluster_signatures))]


def _modules(c: ErrorCluster) -> set[str]:
    """클러스터 오류들이 속한 모듈 집합을 반환한다. 여러 모듈에 걸친 케이스는 ``module``이 None이므로 오류에서 모은다."""
    mods = {e.module for e in c.errors if e.module}
    return mods or ({c.module} if c.module else set())


def plan_iteration(clusters: list[ErrorCluster]) -> list[IterationItem]:
    """의존성 해결 오류(parent, pom, 의존성, 플러그인)가 있는 모듈의 컴파일 클러스터를 이번 라운드에서
    SKIP으로 정한다. 모듈을 모르면 모든 모듈에 적용한다.
    pom이 고쳐지면 컴파일 오류는 대개 사라지므로 LLM 호출을 아낄 수 있다.
    여러 모듈에 걸친 케이스는 그중 한 모듈에라도 해결 오류가 있으면 건너뛴다."""
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
    """케이스마다 할 일을 정하는 클래스. 실행 내내 하나만 사용한다.
    루프가 읽는 ``blocked_info``(차단 이유)와 ``script_steps``(시도 단계와 KB 탐색 로그 줄)를 채운다."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # 아래 실행 전체 상태는 수정 루프가 Triager.attach_run으로 붙인다
        self.blocked_registry = None            # sbmig.loop.blocked_registry.BlockedRegistry
        self.ws = None                          # 차단 규칙의 프로젝트 클래스 확인에 쓰는 Workspace
        self.llm_unavailable: Optional[str] = None
        self.cases: Any = None                  # KB 케이스 저장소(`kb.cases` / CaseScripts). None이면 KB 없음
        # knowledge/notes 항목(sbmig.fix.context.NoteEntry). replacement: true 노트가 맞으면 라이브러리 차단을 풀어 준다
        self.notes: list[Any] = []
        # 클러스터 id별 차단 이유. 루프가 BlockedItem을 만들 때 읽는다
        self.blocked_info: dict[str, BlockedCandidate] = {}
        # 클러스터 id별 마지막 결정의 시도 단계. 루프가 무엇을 실행하고 요청할지, KB 로그 줄을 읽는다
        self.script_steps: dict[str, ScriptStep] = {}

    def _replacement_note(self, cluster: ErrorCluster, cand: BlockedCandidate, profile: ProjectProfile) -> Any:
        """라이브러리 차단 후보의 오류, import 줄, 소유 클래스에 키워드가 맞는 ``replacement: true`` 노트 중
        첫 번째를 반환한다. 없으면 None을 반환한다.
        이런 노트가 있으면 프로젝트 코드로 대체하는 방법을 알고 있으므로 차단 정책을 적용하지 않는다."""
        if not self.notes:
            return None
        try:
            from sbmig.fix.context import replacement_notes
            owners = [o for o in [cand.owner_class, *(cand.owner_classes or [])] if o]
            text = note_text(self.ws, cand.errors or cluster.errors) + "\n" + "\n".join(owners)
            case = getattr(getattr(profile, "case_type", None), "value", None)
            hits = replacement_notes(self.notes, text, case=case, phase=None)
        except Exception as e:  # noqa: BLE001 - a note problem must never stop the triage
            log.debug("replacement-note check for %s failed: %s: %s", cluster.id, type(e).__name__, e)
            return None
        return hits[0] if hits else None

    def _canon(self) -> Optional[Callable[[Optional[str]], str]]:
        """재구성이 옮긴 파일 경로를 현재 경로로 바꾸는 함수를 반환한다. 차단 목록에 없으면 None을 반환한다."""
        fn = getattr(self.blocked_registry, "canonical_file", None)
        return fn if callable(fn) else None

    # -- 사람이 고정한 케이스 동작 ----------------------------------
    @staticmethod
    def _pinned_hits(cluster: ErrorCluster, kb_match: KBMatch) -> list[Any]:
        """사람이 할 일을 고정한(``CaseHit.pinned``) KB 케이스 중 그 오류가 아직 이 클러스터에 있는 것을 반환한다."""
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
        """사람이 ``block``으로 고정한 케이스를 그 사람이 남긴 이유와 문장으로 BLOCKED 처리한다.
        스크립트도 LLM도 쓰지 않는다.
        이유가 BlockReason 값이 아니면 일반 라이브러리 이유로 두고 문장은 제안 칸에 넣는다.

        A pinned `script` version is the store's `best()` choice (`kb.cases`), not a decision here."""
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

    # -- 결정 ----------------------------------------------------------
    def _blocked(self, cluster: ErrorCluster, cand: BlockedCandidate, extra: str = "") -> TriageDecision:
        """BLOCKED 결정을 만들고 그 이유를 ``blocked_info``에 남긴다. BlockedItem은 루프가 만든다."""
        self.blocked_info[cluster.id] = cand
        return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.BLOCKED,
                              rationale=blocked_rationale(cand.reason, cand.detail()) + extra)

    def decide(self, cluster: ErrorCluster, kb_match: Optional[KBMatch], ctx, profile: ProjectProfile) -> TriageDecision:
        """케이스의 결정(SCRIPT, BLOCKED, 케이스 ID가 없으면 ESCALATE)을 반환한다.
        SCRIPT의 시도 단계는 ``script_steps[cluster.id]``에 남는다.
        판단 순서는 모듈 docstring에 있다. 이 클러스터의 이전 결정 기록은 먼저 지운다."""
        kb_match = kb_match or KBMatch()
        prev = same_cluster_attempts(cluster, ctx.previous_attempts, self._canon())
        label = cluster_label(cluster)
        self.blocked_info.pop(cluster.id, None)
        self.script_steps.pop(cluster.id, None)

        # 0. 이 실행에서 이미 차단된 케이스는 스크립트도 LLM도 쓰지 않는다
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

        # 0b. 사람이 `sbmig kb cases pin --action block…`으로 차단한 케이스
        d = self._pinned_block(cluster, kb_match)
        if d is not None:
            return d

        # 0c. 저장소가 응답하지 않은 전송 실패는 프로젝트 문제가 아니므로 정책이 0이면 저장된 스크립트도 실행하지 않는다.
        #   네트워크가 돌아와 통과한 재빌드를 스크립트 실적으로 기록하지 않기 위해서다
        transfer = classify_transfer(cluster)
        if transfer is not None and int(self.cfg.loop.blocked_policy.get(transfer.reason.value, 0)) <= 0:
            return self._blocked(cluster, transfer, " (policy: no LLM script for this reason)")

        # 1. 저장된 스크립트는 LLM 호출이 없으므로 차단 정책이나 LLM 예산과 장애에 막히지 않게 가장 먼저
        #   시도한다. 프로젝트를 많이 처리할수록 LLM 호출을 줄여 주는 핵심 경로다
        # 다른 위치에서 이 케이스를 해결한 이번 실행의 버전은 루프가 넘긴다. 플러그인 컨텍스트에는 없을 수 있다
        elsewhere = list(getattr(ctx, "resolved_elsewhere", None) or [])
        # 모든 위치를 합친 LLM 요청 수다. 루프가 넘기지 않으면 None이라 제한하지 않는다
        total = getattr(ctx, "llm_asks_total", None)
        step = plan_script_step(cluster, prev, self.cases, self.cfg, elsewhere=elsewhere, llm_total=total)
        self.script_steps[cluster.id] = step          # BLOCKED 결정도 KB 탐색 로그 줄이 필요하므로 남긴다
        if not step.case_id:
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.ESCALATE,
                                  rationale=f"escalated: {label} has no case id (no errors)")
        if step.mode == "reuse":
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.SCRIPT, rationale=step.why)

        # 2. 프로젝트 안에서 고칠 수 없어 보이는 오류는 blocked_policy[이유] 횟수만큼만 LLM에 맡긴 뒤
        #   차단하고 인수인계로 넘긴다
        llm_failed = [a for a in script_attempts(prev) if a.script_mode in LLM_MODES and a.outcome in FAILED_OUTCOMES]
        policy_note = ""
        try:
            cand = classify_blocked(cluster, profile, self.cfg, ws=self.ws)
        except Exception:
            # 분류기 오류로 루프가 멈추지 않도록 분류에 실패하면 차단이 아닌 것으로 보고 LLM에 기회를 준다
            cand = None
        # 프로젝트 코드로 대체하는 방법을 적은 노트가 맞으면 고칠 수 있는 것이므로 라이브러리 차단 후보라도 차단 정책을
        # 적용하지 않는다. 이 케이스는 직접 수정 한도(script.max_edit_tries_per_case)와 LLM 한도에서 끝난다
        if cand is not None and cand.reason in (BlockReason.EXTERNAL_JAVAX_API, BlockReason.EXTERNAL_API_INCOMPATIBLE):
            note = self._replacement_note(cluster, cand, profile)
            if note is not None:
                policy_note = (f"; looks like {cand.reason.value} ({cand.detail()}) but the migration note "
                               f"'{note.title}' gives the replacement as project code — not blocked, "
                               f"script.max_edit_tries_per_case applies")
                cand = None
        if cand is not None:
            allowed = int(self.cfg.loop.blocked_policy.get(cand.reason.value, 0))
            if len(llm_failed) >= allowed:
                extra = (f" (after {len(llm_failed)} LLM script attempt(s))" if llm_failed else
                         " (policy: no LLM script for this reason)")
                return self._blocked(cluster, cand, extra)
            policy_note = (f"; looks like {cand.reason.value} ({cand.detail()}) — LLM script attempt "
                           f"{len(llm_failed) + 1}/{allowed} before blocking")

        # 3. LLM 단계(보강, 신규). LLM을 호출할 수 없으면 차단한다
        if self.llm_unavailable:
            return self._blocked(cluster, BlockedCandidate(
                reason=BlockReason.LLM_UNAVAILABLE, errors=list(cluster.errors),
                evidence=[f"LLM unavailable ({self.llm_unavailable}); no stored script left to reuse for {label}"]))
        if step.mode is None:
            return self._blocked(cluster, BlockedCandidate(
                reason=BlockReason.REPEATED_FAILURE, errors=list(cluster.errors),
                evidence=[step.why + "; needs a human"]))
        # 0은 무제한, 양수는 상한, -1은 플릿 전체 한도를 다 써서 LLM을 호출하지 않고 저장된 스크립트만 쓴다는 뜻이다
        if ctx.llm_calls_budget < 0 or (ctx.llm_calls_budget > 0 and ctx.llm_calls_used >= ctx.llm_calls_budget):
            spent = ("fleet" if ctx.llm_calls_budget < 0 else f"{ctx.llm_calls_used}/{ctx.llm_calls_budget} calls")
            return self._blocked(cluster, BlockedCandidate(
                reason=BlockReason.BUDGET_EXHAUSTED, errors=list(cluster.errors),
                evidence=[f"LLM budget exhausted ({spent}) and no stored script left to reuse for {label}"]))
        why = step.why
        if ctx.stall_level:
            why += f"; stall level {ctx.stall_level}"
        return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.SCRIPT, rationale=why + policy_note)
