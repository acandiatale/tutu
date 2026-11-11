"""실행 한 번(프로젝트 하나)을 처음부터 끝까지 모는 오케스트레이터:
접수 → 분석 → 계획 → 단계들 → 검증 빌드 → 보고서. v2.1 순서: 변환(OpenRewrite 1회) → 변환 후 컴파일(고치지
않고 오류 목록에만, seen ``compile``) → 재구성(pom 교정마다 ``pom-verify`` 항목) → 구조 검토(LLM, critical 만
고쳐 ``review: …`` 커밋) → 검증 빌드 ``mvn clean package`` → 실패하면 수정 루프(``loop.phases.verify``, 최대
50 라운드) → 최종 빌드 한 번 더 → 마무리(errors.jsonl / errors.md, KB 기록, 보고서). 진입점:
``Orchestrator.run`` — CLI·웹 UI·플릿·``sbmig.api.migrate`` 가 부르고, ``loop.engine`` (graph = LangGraph,
inline) 둘 다 같은 ``step_*`` 메서드를 같은 ``_run_guard`` 안에서 돌린다(보고서가 같다). 불변(빌드 게이트):
실패·차단·예산 소진·내부 오류 뒤에도 최종 ``mvn clean package`` 시도와 보고서는 반드시 남는다. 일찍 멈추는
것(``aborted:*``, exit 3)은 아예 빌드할 수 없을 때뿐(Maven 없음, Maven 프로젝트 아님, 사내 parent 해석 불가).
v2.1j: Ant 프로젝트(build.xml, pom.xml 없음)는 접수에서 pom.xml 을 합성·커밋한 뒤 Maven 프로젝트처럼 계속한다.

Orchestrator: intake → analyze → plan → phases → verify → report.

v2.1 (2026-10-27, docs/v2/PLAN-v2.1.md §3): transform (every OpenRewrite recipe in ONE run, `transform.rewrite`) →
`transform.compile` (`mvn clean compile` once, no fixing: every error goes on the run's error list, seen `compile`)
→ restructure (the company layout; every pom correction it reports — parents set, mandatory added, optional bumped,
managed versions dropped, generated poms — is an entry with status `pom-verify`) → structure review (`StepKind.
REVIEW`: the LLM checks poms / dependencies / modules and fixes critical issues with edits — committed `review: …`;
warnings become `note` entries, a fixed critical issue an entry resolved by `review`, its script is stored in the KB
case `review|structure`) → verify: `mvn clean package` (only NEW errors are added, the pom-verify entries are
verified); when it fails, the fix loop (`loop.phases.verify`, ≤ 50 rounds) works on the open entries case by case and
the final build runs again → finalize: `end_of_run`, `errors.jsonl` / `errors.md`, `report.error_entries`, the KB
gets the run's entries (`kb.record_run`). Every log line carries its stage (`sbmig.core.stage`).

The orchestrator owns the loop. A phase is green only on BUILD SUCCESS, and the final `mvn clean package` runs
whenever `loop.always_final_verify` is set — even after failures, blocked clusters or budget exhaustion.

Blocked clusters (docs/ERROR-HANDLING.md) live in one run-wide `BlockedRegistry` shared by every phase: a cluster
blocked in the spring phase is skipped by restructure and listed in the report's "Blocked" section; the run only
stops early (``aborted:*`` flags, exit code 3) when nothing can build at all (no Maven, not a Maven project, the
configured in-house parent is not resolvable).


"""
from __future__ import annotations

import importlib
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from pathlib import PurePosixPath
from typing import Any, Callable, Iterator, Optional

from sbmig.build.log_parser import BUILD_EXTENSION_RE
from sbmig.core.config import Config
from sbmig.core.models import (GAV, BlockedItem, BuildError, BuildResult, CaseType, DecisionKind, ErrorKind,
                               IterationRecord, LLMUsage, MigrationPlan, Phase, PhaseResult, PlanStep, ProjectProfile,
                               RunReport, StepKind, utcnow)
from sbmig.core.stage import stage
from sbmig.core.workspace import Workspace, logs_dir

from .blocked_registry import BlockedRegistry, default_locator
from .budget import LoopBudget
from .components import LazyFixer, LazyScriptRunner, MissingComponentError, NullKnowledgeBase, lazy_attr
from .entries import REVIEW_L2, CaseScripts, RunErrors
from .fixloop import NOTES_LOGGED, BuildFixLoop, _restore_build_byproducts, _status_paths, refine_build
from .notes import (answer_notes, fallback_guide, log_major_upgrades, note_line, operator_changes, pom_changed,
                    try_notes)

log = logging.getLogger("sbmig.loop")

# 레시피 id 를 못 찾음 = 설정·프로그래밍 오류 → 그 단계를 중단한다(빌드로 고칠 수 있는 문제가 아니다)
_MISSING_RECIPE_RE = re.compile(r"(?i)(recipe(?:s|\(s\))? (?:were )?not found|could not find recipe|no recipe(?:s)? (?:found|named)|"
                                r"unknown recipe|recipe\s+\S+\s+(?:does not exist|not found))")
_PARENT_UNRESOLVABLE_RE = re.compile(r"(?i)non-resolvable parent|parent pom|failed to read artifact descriptor")
_BUILD_EXT_IN_TEXT_RE = BUILD_EXTENSION_RE          # one regex for the parser and this check (sbmig.build.log_parser)
# preflight (round 10): `mvn -v` detail → Java major; Maven that cannot start because of JAVA_HOME (not "install Maven")
_JAVA_VERSION_RE = re.compile(r"(?i)java version:?\s*\"?(?P<v>[0-9][0-9._+-]*)")
_JAVA_RUNTIME_RE = re.compile(r"(?i)runtime:\s*(?P<home>[^,;]+)")
_JAVA_HOME_PROBLEM_RE = re.compile(r"(?i)JAVA_HOME|UnsupportedClassVersionError|Unable to locate a Java Runtime|"
                                   r"java: (?:command )?not found|'java' is not recognized|no java runtime")
# infrastructure classification at finalize (round 10, fleet circuit breaker): a resolution error that points at the
# repository / network rather than at the project
_INFRA_REPO_RE = re.compile(r"(?i)could not transfer|failed to transfer|connection refused|connection reset|timed out|"
                            r"unknown ?host|no route to host|pkix path building failed|"
                            r"(?:status code|return code is):?\s*(?:401|403|5\d\d)\b|"
                            r"was cached in the local repository")
# 디스크가 가득 차면 infra:disk 플래그 → 플릿은 다음 프로젝트들까지 실패시키지 않고 멈춘다
# a full disk during a build (javac / Maven / git on Windows and POSIX; "디스크 공간이 부족" = the Korean Windows text)
_INFRA_DISK_RE = re.compile(r"(?i)no space left on device|not enough space on the disk|disk quota exceeded|"
                            r"디스크 공간이 부족|ENOSPC")
_DEFAULT_MODE = {StepKind.MSA_POM: "msa_pom", StepKind.RESTRUCTURE: "layout"}
COMPILE_GOAL = "clean compile"            # transform.compile: OpenRewrite's result compiled once, before the restructure


def step_stage(step: PlanStep) -> str:
    """계획 단계의 로그 단계 태그(rewrite / compile / restructure / review / build / fix) — 로그 줄마다
    ``[rewrite]`` 처럼 붙어 운영자가 어디서 난 줄인지 안다.

    The log stage of a plan step (§3.2): rewrite / compile / restructure / review / build / fix."""
    k = step.kind
    if k == StepKind.OPENREWRITE:
        return "rewrite"
    if k == StepKind.BUILD:
        return "build" if step.phase == Phase.VERIFY else "compile"
    if k in (StepKind.RESTRUCTURE, StepKind.MSA_POM):
        return "restructure"
    if k == StepKind.REVIEW:
        return "review"
    return "fix" if k == StepKind.BUILD_FIX_LOOP else step.phase.value
# 선택 아티팩트 버전 올리기는 최선 노력: 실패해도 후속 조치로만 남기고 재구성 단계를 계속한다
NON_FATAL_RESTRUCTURE_MODES = frozenset({"optional_bump"})   # a failure is noted, the phase continues


def _first_sentence(text: str, limit: int = 160) -> str:
    """LLM 분석의 첫 문장(공백 정리, ``limit`` 자 이하) — 인수인계 섹션 메모용.

    First sentence of an LLM analysis, whitespace-collapsed, ≤ limit chars (Handover note)."""
    t = " ".join((text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    t = m.group(1) if m else t
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


class _RunAborted(Exception):
    """내부용: 실행을 멈춘다(다음 단계도 검증 빌드도 없음). 빌드해 봐야 같은 이유로 실패할 때(Maven 없음,
    parent 해석 불가)만 쓴다 — 그래도 보고서는 쓰인다.

    Internal: stop the run (no further phases, no verify) — the report is still written."""


def parse_renames(out: str) -> dict[str, str]:
    """``git diff --name-status -M -z`` 출력에서 이름 바꾸기(R)만 뽑아 {옛 경로: 새 경로}. 재구성이 옮긴
    파일을 차단 등록부가 따라가게 하는 데 쓴다.

    `git diff --name-status -M -z` output → {old path: new path} of the renames (`R<score>` entries)."""
    toks = out.split("\0")
    moves: dict[str, str] = {}
    i = 0
    while i < len(toks):
        st = toks[i].strip()
        if not st:
            i += 1
            continue
        if st[0] in "RC" and i + 2 < len(toks):          # rename / copy: two paths follow
            if st[0] == "R" and toks[i + 1] and toks[i + 2]:
                moves[toks[i + 1]] = toks[i + 2]
            i += 3
        else:
            i += 2
    return moves


def _aborted(flags: list[str]) -> bool:
    return any(f == "aborted" or f.startswith("aborted:") for f in flags)


_PHASE_ORDER = [Phase.INTAKE, Phase.ANALYZE, Phase.PLAN, Phase.TRANSFORM, Phase.JDK21, Phase.SPRING,
                Phase.RESTRUCTURE, Phase.VERIFY, Phase.REPORT]
STOP_AFTER_PHASES = tuple(p for p in _PHASE_ORDER if p != Phase.REPORT)


def parse_stop_after(value: Any) -> Phase | None:
    """CLI·웹 UI·``sbmig.api.migrate`` 의 ``--stop-after`` 값을 단계로 바꾼다(대소문자 무시, ``report`` 앞
    단계만). None / '' = 끝까지. 잘못된 값이면 받을 수 있는 값을 나열한 ValueError.

    `--stop-after` / `stop_after=` of the CLI, the web UI and `sbmig.api.migrate`: a phase name (case-insensitive)
    before `report`; None / '' = run everything. ValueError names the accepted values."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = value.value if isinstance(value, Phase) else str(value).strip().lower()
    try:
        phase = Phase(text)
    except ValueError:
        phase = None
    if phase is None or phase not in STOP_AFTER_PHASES:
        raise ValueError(f"stop_after must be one of: {', '.join(p.value for p in STOP_AFTER_PHASES)}")
    return phase


class _BudgetedLLM:
    """호출마다 실행 예산(``LoopBudget``)에 한 번씩 매기는 LLM 클라이언트 감싸개. 구조 검토도 수정 루프와
    같은 예산을 쓰고, 모델별 사용량은 ``sink`` 에 모여 보고서의 비용 표가 된다.

    An LLM client whose calls are charged to the run's `LoopBudget` (v2: the structure review goes through the same
    budget as the fix loop; per-model usage lands in `sink` for the cost table)."""

    def __init__(self, inner: Any, budget: LoopBudget, sink: Optional[dict[str, LLMUsage]] = None):
        self._inner, self._budget, self._sink = inner, budget, sink

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        """LLM 호출 한 번: 먼저 예산에 1회를 매기고(실패해도 부른 것), 응답의 모델별 토큰 사용량을 모은다."""
        self._budget.charge_llm(1)
        res = self._inner.complete(*args, **kwargs)
        if self._sink is not None:          # per-model usage for the cost table (recipe model calls)
            model = str(getattr(res, "model", "") or kwargs.get("model") or "")
            u = LLMUsage(model=model, calls=1, input_tokens=int(getattr(res, "input_tokens", 0) or 0),
                         output_tokens=int(getattr(res, "output_tokens", 0) or 0))
            self._sink[model] = self._sink.get(model, LLMUsage(model=model)).add(u)
        return res

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


STRAY_POM_TAIL = ("treated as a stray copy (an IDE output / backup folder): not built, not migrated, left where it is; "
                  "delete it from the source, or give it its own coordinates and list it in <modules> if it is a real module")


def stray_pom_followup(st: Any) -> str:
    """v2.1z-h: 떠도는 pom 복사본의 인수인계 한 줄(영문; 한국어는 report/i18n.py ``PATTERNS`` 가 맞춘다 — 바꾸면 함께).

    The Handover line for one stray pom copy (Korean: i18n `PATTERNS`)."""
    return f"pom copy ignored: {st.why()} — {STRAY_POM_TAIL}"


class Orchestrator:
    """한 실행(프로젝트 하나)의 지휘자: 협력
    객체(분석기·계획기·재구성기·OpenRewrite·Maven·트리아지·fixer·KB·LLM)를 처음 쓸 때 만들고,
    단계(``step_*``)를 차례로 돌리고, 무슨 일이 있어도 마지막에 보고서를 쓴다. 실행 전체가 공유하는 것:
    ``budget``(LLM·시간), ``blocked``(차단 등록부), ``errors``(오류 목록), ``loop``(수정 루프)."""
    def __init__(self, cfg: Config, ws: Workspace, *, analyzer: Optional[Callable] = None,
                 planner: Optional[Callable] = None,
                 restructurer: Any = None, rewrite_runner: Any = None, build_runner: Any = None,
                 triager: Any = None, fixer: Any = None, kb: Any = None, llm: Any = None,
                 report_writer: Optional[Callable] = None, clock: Callable[[], float] = time.monotonic,
                 locator: Any = None, effective_capture: Optional[Callable] = None,
                 javax_inventory: Any = None, script_runner: Any = None, reviewer: Any = None,
                 error_list: Optional[Callable[..., Any]] = None, edit_applier: Optional[Callable[..., Any]] = None):
        """협력 객체는 모두 주입할 수 있다(테스트·사내 플러그인). None 이면 실제 구현을 처음 쓸 때 만든다.
        커스텀 ``build_runner`` 를 주면 진짜 Maven 이 아니므로 classpath
        귀속·effective pom 캡처는 기본으로 꺼진다(mvn 을 띄우므로).

        v2 collaborators: `script_runner` (ScriptRunnerLike; None = `sbmig.script.ScriptRunner(cfg)` on first use)
        and `reviewer` (a StructureReviewerLike object, or a factory `fn(cfg, llm, runner, kb)`; None =
        `sbmig.review.StructureReviewer`). v2.1: `error_list` = `fn(ws, cfg, project)` → the run's error list (None =
        `sbmig.errors.entries.ErrorList.for_workspace`), `edit_applier` = `fn(ws, edits, *, cfg, errors=None) -> ScriptRunResult` (`errors` only when the signature takes it)
        for the LLM's direct edits (None = `sbmig.script.apply_edits`)."""
        self.cfg = cfg
        self.ws = ws
        self.clock = clock
        self._analyzer = analyzer
        self._planner = planner
        self._script_runner = script_runner
        self._reviewer = reviewer
        self._error_list_factory = error_list
        self._edit_applier = edit_applier
        self._restructurer = restructurer
        self._rewrite_runner = rewrite_runner
        self._build_runner = build_runner
        self._triager = triager
        self._fixer = fixer
        self._kb = kb
        self._llm = llm
        self._report_writer = report_writer
        self.budget = LoopBudget.from_config(cfg, clock=clock)
        self.report: Optional[RunReport] = None
        self.profile: Optional[ProjectProfile] = None
        self.plan: Optional[MigrationPlan] = None
        self.loop: Optional[BuildFixLoop] = None
        self.errors = RunErrors(None)               # v2.1: the run's error list (errors.jsonl), set in step_plan
        self._verify_results: list[BuildResult] = []
        self._review_usage: dict[str, LLMUsage] = {}       # v2: structure-review calls, by model
        self._review_commit_pre: str | None = None          # v2.1d: HEAD before the review commit (read-back)
        self._review_guide_source = ""                     # v2.1d: "llm" | "sbmig" — who wrote the review's guide
        self._restructure_results: list[Any] = []          # v2: this run's RestructureResults (structure review input)
        self._restructure_modes: list[Optional[str]] = []  # v2.1v: each result's mode (layout / boot_pom / msa_pom)
        self._restructure_notes: list[str] = []
        self._phase_failed = False
        self._dry_run = False
        self._pending_notes: list[str] = []
        # 차단 등록부는 실행 전체에 하나: 어느 루프에서 차단된 오류든 다른 루프·마무리가 같은 목록을 본다
        self.blocked = BlockedRegistry()            # run-wide: shared by every BuildFixLoop of this run
        # a custom build runner means we are not driving the real Maven → no classpath attribution / effective-pom
        # capture by default (they would spawn mvn); pass `locator` / `effective_capture` explicitly to enable them
        self._real_maven = build_runner is None
        self._locator = locator
        self._effective_capture = effective_capture
        # report.javax_api_inventory: `fn(ws, cfg, profile) -> list[JavaxApiJar]` (sbmig.build.deptree, spawns mvn);
        # None = the real one with the real Maven only, False = off — tests pass a fake
        self._javax_inventory = javax_inventory
        self._running = False
        self.abort_reason: str | None = None   # first `_abort` reason of the current run
        self._run_steps: list[PlanStep] = []   # the run's steps incl. an appended verify.build (step_plan)
        self._verify_done: list[str] = []      # verify step ids whose final build has a result (invariant 1, round 10)

    # ------------------------------------------------------------------ lazy collaborators
    @property
    def analyzer(self) -> Callable:
        """분석기(``sbmig.analyze.analyze_project``) — 처음 쓸 때 불러온다."""
        if self._analyzer is None:
            self._analyzer = lazy_attr("sbmig.analyze", "analyze_project", "A (analyze)")
        return self._analyzer

    @property
    def planner(self) -> Callable:
        """계획기(``sbmig.plan.build_plan``) — 처음 쓸 때 불러온다."""
        if self._planner is None:
            self._planner = lazy_attr("sbmig.plan", "build_plan", "A (plan)")
        return self._planner

    @property
    def script_runner(self) -> Any:
        """수정 루프와 구조 검토가 함께 쓰는 스크립트 실행기(``sbmig.script``, 처음 쓸 때 만든다).

        v2: the fix-script runner shared by the fix loop and the structure review (lazy `sbmig.script`)."""
        if self._script_runner is None:
            self._script_runner = LazyScriptRunner(
                lambda: lazy_attr("sbmig.script", "ScriptRunner", "S (script)")(self.cfg))
        return self._script_runner

    @property
    def restructurer(self) -> Any:
        """재구성기(``sbmig.restructure.Restructurer``, 회사 레이아웃)."""
        if self._restructurer is None:
            self._restructurer = lazy_attr("sbmig.restructure", "Restructurer", "B (restructure)")()
        return self._restructurer

    @property
    def rewrite_runner(self) -> Any:
        """OpenRewrite 실행기(``sbmig.rewrite.OpenRewriteRunner``)."""
        if self._rewrite_runner is None:
            cls = lazy_attr("sbmig.rewrite", "OpenRewriteRunner", "C (rewrite)")
            self._rewrite_runner = cls(self.ws, self.cfg, self.kb)
        return self._rewrite_runner

    @property
    def build_runner(self) -> Any:
        """Maven 실행기(``sbmig.build.MavenRunner``)."""
        if self._build_runner is None:
            self._build_runner = lazy_attr("sbmig.build", "MavenRunner", "C (build)")(self.ws, self.cfg)
        return self._build_runner

    @property
    def triager(self) -> Any:
        """트리아지(오류 → 케이스 묶기, 사다리 단계·차단 정책 결정, ``sbmig.triage.Triager``)."""
        if self._triager is None:
            from sbmig.triage import Triager
            self._triager = Triager(self.cfg)
        return self._triager

    @property
    def kb(self) -> Any:
        """파일 지식베이스(``sbmig.kb.FileKnowledgeBase``). 불러올 수 없으면 경고 한 줄 +
        ``NullKnowledgeBase`` — KB 가 없다고 실행이 멈추지는 않는다(재사용만 없을 뿐)."""
        if self._kb is None:
            try:
                self._kb = lazy_attr("sbmig.kb", "FileKnowledgeBase", "F (kb)")(self.cfg)
            except MissingComponentError as e:
                log.warning("%s — running without a knowledge base", e)
                self.ws.journal({"event": "warning", "message": str(e)})
                self._kb = NullKnowledgeBase()
        return self._kb

    def llm_client(self) -> Any:
        """LLM 클라이언트(``sbmig.fix.make_llm_client``, 기본은 회사 게이트웨이). 키는 환경변수에서만 온다."""
        if self._llm is None:
            self._llm = lazy_attr("sbmig.fix", "make_llm_client", "D (fix)")(self.cfg)
        return self._llm

    @property
    def fixer(self) -> Any:
        """수정 스크립트를 쓰는 fixer(``LazyFixer`` 로 감싸 첫 요청 때 만든다). ``fix.plugin`` 이 있으면
        tier 2 사내 코딩 에이전트(``resolve_fixer_plugin``), 없으면 ``sbmig.fix.LLMFixer``."""
        if self._fixer is None:
            def make() -> Any:
                spec = getattr(getattr(self.cfg, "fix", None), "plugin", None)
                if spec:                                   # tier 2: in-house coding agent (docs/ADAPTERS.md)
                    try:
                        llm = self.llm_client()
                    except Exception as e:                 # the agent may not need our LLM at all
                        log.info("fix.plugin %s: no sbmig LLM client (%s)", spec, e)
                        llm = None
                    resolve = lazy_attr("sbmig.llm", "resolve_fixer_plugin", "A (llm adapters)")
                    try:
                        fx = resolve(self.cfg, llm)
                    except Exception as e:
                        self.ws.journal({"event": "fixer", "plugin": spec, "error": f"{type(e).__name__}: {e}"})
                        raise
                    self.ws.journal({"event": "fixer", "plugin": spec})
                    return fx
                cls = lazy_attr("sbmig.fix", "LLMFixer", "D (fix)")
                return cls(self.cfg, self.llm_client())
            self._fixer = LazyFixer(make)
        return self._fixer

    # ------------------------------------------------------------------ run
    def run(self, stop_after: Phase | None = None, dry_run: bool = False) -> RunReport:
        """마이그레이션 한 번. ``loop.engine`` 이 단계를 모는 엔진을 고른다: ``graph``(기본, LangGraph
        ``StateGraph``) 또는 ``inline``. 둘 다 같은 ``step_*`` 를 같은 보호막(``_run_guard``) 안에서 부르므로
        보고서·저널·종료 코드가 같다.

        One migration run. `loop.engine` picks the driver of the steps: ``graph`` (default, LangGraph
        `StateGraph`, `sbmig.graph`) or ``inline`` (`_run`). Both call the same `step_*` methods inside the same
        guard (`_run_guard`: abort / missing-component / internal-error handling + `_finalize`), so reports,
        journals and exit codes are identical apart from the graph's additive ``graph_node`` journal events."""
        with self._run_guard(dry_run) as report:
            if self.engine == "graph":
                from sbmig.graph import run_graph
                run_graph(self, stop_after=stop_after, dry_run=dry_run)
            else:
                self._run(report, stop_after, dry_run)
        return report

    @property
    def engine(self) -> str:
        """``loop.engine`` 값(graph | inline, 비어 있으면 inline)."""
        return getattr(getattr(self.cfg, "loop", None), "engine", "inline") or "inline"

    @property
    def running(self) -> bool:
        """실행 중인가(``_run_guard`` 가 열려 있고 ``self.report`` 가 그 실행의 보고서).

        True while a `_run_guard` is open (a run is in progress and `self.report` is its report)."""
        return self._running

    @contextmanager
    def _run_guard(self, dry_run: bool = False) -> Iterator[RunReport]:
        """실행을 열고(새 RunReport) 반드시 닫는다: ``_RunAborted`` 는 조용히 끝, 빠진 협력 패키지나 그 밖의
        예외는 중단으로 기록하고 그래도 최종 빌드를 시도한다(빌드 게이트), 그리고 무슨 일이 있어도
        ``_finalize``(보고서·KB 기록·산출물). 버그 하나 때문에 보고서를 잃지 않으려는 장치다.

        Start a run (fresh `RunReport` on `self.report`) and always finish it: `_RunAborted` ends the run
        quietly, a `MissingComponentError` or any other exception is recorded as an abort (never lose the report
        because of a bug), and `_finalize` (report, KB records, outputs) runs no matter what. Used by `run()` for both
        engines and by `sbmig.graph.run_graph` / `stream_graph` when they are called directly."""
        if self._running:
            raise RuntimeError("Orchestrator run already in progress")
        self._running = True
        self._dry_run = dry_run
        self.abort_reason = None
        # per-run state (an orchestrator is normally used once; a second run must not see the first one's results)
        self._verify_results = []
        self._verify_done = []
        self._run_steps = []
        self._review_usage = {}
        self._restructure_results = []
        self._restructure_modes = []
        self._restructure_notes = []
        self._mvn_java_problem = self._jdk_problem = ""
        self._after_abort = False
        self._phase_failed = False
        self._pending_notes = []
        self.loop = None
        self.errors = RunErrors(None)
        t0 = self.clock()
        report = RunReport(run_id=self.ws.run_id, project_name=self.ws.root.name, case_type=CaseType.UNKNOWN,
                           started_at=utcnow())
        self.report = report
        if dry_run:
            report.flags.append("dry-run")
        try:
            yield report
        except _RunAborted:
            pass
        except MissingComponentError as e:
            log.error("%s", e)
            self._abort(report, f"missing component: {e}")
            with stage("build"):
                self._final_build_after_abort(report)
        except Exception as e:  # never lose the report because of a bug
            log.exception("run failed")
            self._abort(report, f"internal error: {type(e).__name__}: {e}")
            with stage("build"):
                self._final_build_after_abort(report)
        finally:
            self._running = False
            report.timings_s["total"] = round(self.clock() - t0, 3)
            with stage("report"):
                self._finalize(report)
            if not dry_run:
                self._run_hook("teardown", report)                 # v2.1z-s: after the report, whatever happened

    def _run_hook(self, which: str, report: RunReport) -> bool:
        """v2.1z-s: ``build.setup_command`` / ``build.teardown_command`` — 실행마다 한 번 도는 argv(셸 없음). 환경에 ``SBMIG_RUN_ID``,
        ``SBMIG_RUN_DIR``, ``SBMIG_PROJECT_DIR`` 를 더해 준다. setup 실패(0 아닌 종료·타임아웃·실행 불가)는 ``_abort`` 뒤 False(빌더 없이는 아무것도
        빌드할 수 없다); teardown 실패는 WARNING 만. 명령이 비어 있으면 True. 저널 ``build_hook`` (which, rc, seconds).

        Run the per-run hook `which` ("setup" | "teardown") once: argv from `build.<which>_command` with SBMIG_RUN_ID /
        SBMIG_RUN_DIR / SBMIG_PROJECT_DIR in the environment; a failing setup aborts the run (False), a failing teardown only
        warns. Empty command → True. Journal `build_hook`."""
        b = getattr(self.cfg, "build", None)
        cmd = list(getattr(b, f"{which}_command", None) or [])
        if not cmd:
            return True
        import subprocess
        env = {**os.environ, "SBMIG_RUN_ID": str(self.ws.run_id), "SBMIG_RUN_DIR": str(self.ws.run_dir),
               "SBMIG_PROJECT_DIR": str(self.ws.root)}
        timeout = float(getattr(b, "hook_timeout_s", 600) or 600)
        t0 = self.clock()
        log.info("build %s hook: %s", which, " ".join(cmd))
        try:
            res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
            rc, tail = res.returncode, (res.stdout + res.stderr)[-800:]
        except (OSError, subprocess.TimeoutExpired) as e:
            rc, tail = -1, f"{type(e).__name__}: {e}"
        seconds = round(self.clock() - t0, 1)
        try:
            self.ws.journal({"event": "build_hook", "hook": which, "rc": rc, "seconds": seconds,
                             "command": cmd[0] if cmd else ""})
        except Exception:  # noqa: BLE001 - a journal problem never decides a run
            pass
        if rc == 0:
            log.info("build %s hook ok (%.1fs)", which, seconds)
            return True
        if which == "setup":
            self._abort(report, f"build.setup_command failed (rc={rc}): {tail.strip()[-300:]}")
            return False
        log.warning("build %s hook failed (rc=%s): %s", which, rc, tail.strip()[-300:])
        return True

    def _verify_steps(self) -> list[PlanStep]:
        """이 실행의 최종 빌드 단계들(aggregator 없는 MSA 는 서비스마다 하나). 계획에 없고
        ``loop.always_final_verify`` 면 ``verify.build`` 하나.

        The run's final-build steps (MSA without an aggregator: one per service); `verify.build` when the plan had
        none and `loop.always_final_verify` is set (as `step_plan` appends it)."""
        steps = list(self._run_steps or (self.plan.steps if self.plan is not None else []))
        out = [s for s in steps if s.kind == StepKind.BUILD and s.phase == Phase.VERIFY]
        if not out and self.plan is not None and self.cfg.loop.always_final_verify:
            out = [PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY, title="final mvn clean package",
                            build_goal="clean package", reason="always_final_verify")]
        return out

    def _verify_missing(self) -> list[PlanStep]:
        return [s for s in self._verify_steps() if s.id not in self._verify_done]

    def _final_build_after_abort(self, report: RunReport) -> None:
        """불변조건 1(빌드 게이트): 계획 뒤 내부 오류·빠진 패키지로 죽은 실행도 최종 ``mvn clean package``
        를 시도해, 남겨진 트리의 진짜 미해결 오류가 report.md 에 실리게 한다. 수정 루프는 돌리지 않는다(방금
        죽은 것 위에 또 고치지 않음). 일부러 멈춘 실행(Maven 없음,
        parent 불가)은 빌드하지 않는다 — 같은 이유로 실패할 뿐이므로.

        Invariant 1 (build gate): a run that died of an internal error / missing component after planning still
        ends with the final `mvn clean package` attempt, so report.md lists the real unresolved errors of the tree
        as it was left. Deliberate aborts (`_RunAborted`: no Maven, unresolvable parent) build nothing — the build
        would fail for the very reason the run aborted. Round 10: every final-build step without a result runs — an
        MSA run whose svc-a verify passed before svc-b's step crashed still builds svc-b."""
        if self._dry_run or self.plan is None or not self.cfg.loop.always_final_verify:
            return
        steps = self._verify_missing()
        if not steps:
            return
        self._after_abort = True              # the build only: no last fix loop on top of what just crashed
        pr = PhaseResult(phase=Phase.VERIFY, step_ids=[s.id for s in steps], ok=False)
        pr.notes.append("final build after the run aborted (best effort: earlier phases are incomplete)")
        self.ws.journal({"event": "phase_start", "phase": Phase.VERIFY.value, "steps": pr.step_ids,
                         "after_abort": True})
        try:
            for s in steps:
                self._step_verify(pr, s)
        except Exception as e:   # the build runner itself failed: keep the abort report
            log.warning("final build after the abort failed: %s", e)
            pr.notes.append(f"final build failed: {type(e).__name__}: {e}")
        pr.ok = pr.build_ok = bool(self._verify_results) and all(r.ok for r in self._verify_results)
        pr.finished_at = utcnow()
        report.phases.append(pr)
        self.ws.journal({"event": "phase_end", "phase": Phase.VERIFY.value, "ok": pr.ok, "build_ok": pr.build_ok,
                         "iterations": pr.iterations, "attempts": 0, "remaining_errors": len(pr.remaining_errors),
                         "blocked": [], "after_abort": True})

    def build_succeeded(self, report: RunReport) -> bool:
        """이관의 성공 판정(``report.success`` 의 정의): 계획된 최종 빌드가 전부 돌았고 전부 성공, 재구성 실패·속 빈 리액터·중단 없음.
        ``_finalize`` 와 그래프의 ``remediation`` 노드(``_finalize`` 보다 먼저 돈다)가 같은 판정을 쓴다 — 루프의 플래그도 본다
        (``_finalize`` 가 보고서에 옮기기 전이어도 같은 답이 나오게).

        The migration's success predicate (`report.success`): every planned final build ran and passed, no
        restructure failure, no hollow reactor, not aborted. Shared by `_finalize` and the graph's `remediation` node."""
        flags = list(report.flags) + (list(self.loop.flags) if self.loop is not None else [])
        return bool(self._verify_results) and all(r.ok for r in self._verify_results) \
            and "restructure-failed" not in flags and "hollow-reactor" not in flags \
            and not _aborted(flags) and not self._verify_missing()

    def _abort(self, report: RunReport, why: str) -> None:
        """실행 중단 기록: ``aborted`` 플래그, 후속 조치, ERROR 한 줄(운영자가 찾는 줄), 저널."""
        if "aborted" not in report.flags:
            report.flags.append("aborted")
        report.manual_followups.append(f"run aborted: {why}")
        if self.abort_reason is None:
            self.abort_reason = why
        log.error("run aborted: %s", why)          # one greppable line (round 10)
        self.ws.journal({"event": "abort", "reason": why})

    def _stop(self, stop_after: Phase | None, phase: Phase) -> bool:
        if stop_after is not None and stop_after == phase:
            assert self.report is not None
            self.report.flags.append(f"stopped-after-{phase.value}")
            self.ws.journal({"event": "stop_after", "phase": phase.value})
            return True
        return False

    def _timed(self, key: str, t0: float) -> None:
        assert self.report is not None
        self.report.timings_s[key] = round(self.report.timings_s.get(key, 0.0) + self.clock() - t0, 3)

    def _run(self, report: RunReport, stop_after: Phase | None, dry_run: bool) -> None:
        """inline 엔진(``loop.engine: inline``): LangGraph 없이 같은 단계 순서를 그대로 돈다.

        The inline engine (`loop.engine: inline`): the step sequence without LangGraph."""
        if not self.step_intake(report, stop_after):
            return
        if not self.step_analyze(report, stop_after):
            return
        groups = self.step_plan(report, stop_after, dry_run)
        if groups is None:
            return
        for phase, items in groups:
            if self._stop_file_abort(report, f"phase {phase.value}"):
                return
            self.step_phase(report, phase, items, stop_after)
        if self._stop_file_abort(report, "finalize"):
            return
        self.step_finish(report, stop_after)
        self._note_inline_remediation(report, stop_after, dry_run)

    def _note_inline_remediation(self, report: RunReport, stop_after: Phase | None, dry_run: bool) -> None:
        # 2026-11-11: 취약점 조치는 그래프 엔진의 remediation 노드에서만 이어서 돈다. inline 엔진은 잇지 않고, 스위치가 켜져 있고
        # 그래프였다면 돌았을 실행(빌드 성공·끝까지)에만 그 사실을 한 줄 남긴다 — 스위치가 꺼진 실행에는 아무 줄도 없다
        rem = getattr(self.cfg, "remediation", None)
        if rem is None or not bool(getattr(rem, "after_migration", False)):
            return
        if dry_run or stop_after is not None or not self.build_succeeded(report):
            return
        try:
            self.ws.journal({"event": "remediation_skipped", "reason": "inline_engine", "scope": "remediation"})
        except Exception:  # noqa: BLE001 - a journal line never decides a run
            log.debug("remediation_skipped journal line not written", exc_info=True)
        report.remediation = {"status": "skipped", "reason": "inline_engine", "dir": "remediation"}
        log.info("vulnerability remediation not chained: loop.engine is inline (the graph engine chains it)")

    def _stop_file_abort(self, report: RunReport, before: str) -> bool:
        """2026-11-05 (k8s Job 모드): 실행 폴더의 ``stop.request`` 를 노드 사이에서 본다 — 그래프 엔진의 ``user_stop`` 과 같은 뜻
        (보고서를 aborted 로 표시, 최종 빌드 없음). 있으면 True."""
        fn = getattr(self.ws, "stop_requested", None)
        if not callable(fn) or not fn():
            return False
        from sbmig.core.workspace import STOP_REQUEST_REASON
        log.warning("stop requested (stop.request) — stopping before %s (no final build)", before)
        self.ws.journal({"event": "stop_request", "node": before.split()[0], "file": "stop.request"})
        if "aborted" not in report.flags:
            self._abort(report, STOP_REQUEST_REASON)
        return True

    # ------------------------------------------------------------------ steps (shared by both engines)
    def step_intake(self, report: RunReport, stop_after: Phase | None) -> bool:
        """1. 접수: git 스냅샷, 사전 점검(Maven·JDK 21·parent 설정), Maven 프로젝트인지(v2.1j: Ant 면 pom.xml 합성),
        aggregator 생성, as-is effective dependencyManagement 캡처. False = 멈춤 또는 중단.

        1. intake: git snapshot, preflight, build tool (v2.1j: an Ant project gets a synthesized pom.xml), effective
        dependencyManagement. False = stop or abort."""
        # v2.1z-s: the per-run setup hook (k8s split: the builder pod) — first thing of the run, inside the guard so that a
        # failure ends the run like any abort (report, KB records, teardown)
        if not self._dry_run and not self._run_hook("setup", report):
            raise _RunAborted()
        with stage("intake"):
            return self._step_intake(report, stop_after)

    def _step_intake(self, report: RunReport, stop_after: Phase | None) -> bool:
        t = self.clock()
        self.ws.git_init()
        # 2026-11-05: 설정 경고(중복 블록·모르는 키·없어진 키)는 설정을 읽을 때 콘솔에만 찍혔다 — 실행 로그가 붙은 지금 다시 적는다
        for line in getattr(self.cfg, "_config_warnings", None) or []:
            log.warning("%s", line)
        setter = getattr(self.ws, "set_encoding_candidates", None)
        if callable(setter):
            setter(list(self.cfg.build.encoding_candidates))
        self._preflight(report)
        if "mvn-not-found" in report.flags:
            if self._mvn_java_problem:
                self._abort(report, f"'{self.cfg.build.mvn}' cannot start: {self._mvn_java_problem} — set "
                                    f"build.java_home (or JAVA_HOME) to a JDK {self._java_major_wanted()} home")
            else:
                self._abort(report, f"'{self.cfg.build.mvn}' is not runnable (install Maven or set build.mvn)")
            return False
        if "jdk-not-21" in report.flags:
            self._abort(report, self._jdk_problem)
            return False
        if not self._check_build_tool(report):
            self._timed("intake", t)
            return False
        if not self._intake_layout(report):
            self._timed("intake", t)
            return False
        self._intake_parent_extensions(report)    # 2026-11-05: before the first Maven call (the capture below)
        self._capture_effective(report)
        self._timed("intake", t)
        return not self._stop(stop_after, Phase.INTAKE)

    def _intake_parent_extensions(self, report: RunReport) -> None:
        """2026-11-05 회사 실행: 이관 대상 사내 부모(base-parent 4.0.0)가 ``<build><extensions>`` 에 wagon-webdav 를 선언한다 — 상속되는
        확장은 자식에서 뺄 수 없고 Maven 은 goal 전에(OpenRewrite 포함) 그것을 받아야 한다. 첫 Maven 호출 전에 체인의 확장마다
        읽히는지 보고(``check_parent_build_extensions``), 저장소는 주는데 로컬에 묵은 ``*.lastUpdated`` 만 있으면 그 표식을 지우고,
        어디서도 못 읽으면 WARNING + 플래그 ``parent-extension-unresolvable`` + 인수인계 한 줄. 저널 ``parent_build_extensions``.
        예외로 실행을 멈추지 않는다.

        Pre-check the configured parent chain's build extensions before the first Maven call: stale markers of a
        repository-served extension are cleared; an extension nobody serves is a WARNING, the flag
        `parent-extension-unresolvable` and a Handover line; journal `parent_build_extensions`. Never raises."""
        try:
            from sbmig.build.parent_extensions import check_parent_build_extensions
            checks = check_parent_build_extensions(self.cfg, clear_markers=bool(getattr(self.cfg.build, "retry_negative_cache", True)))
            if not checks:
                return
            for c in checks:
                if c.readable:
                    if c.markers_removed:
                        log.info("접수: 사내 부모 체인의 빌드 확장 %s — 저장소에 있음 (%s), 로컬 저장소의 묵은 실패 표식 %d개 제거 (%s): "
                                 "Maven 이 하루 동안 다시 받지 않던 것", c.coordinates, c.where, len(c.markers_removed), ", ".join(c.markers_removed))
                    else:
                        log.info("접수: 사내 부모 체인의 빌드 확장 %s — 읽힘 (%s)", c.coordinates, c.where)
                    if c.where.startswith("repository http"):
                        # 2026-11-06 회사 실행: sbmig 는 build.repository_url(그룹)에서 읽었는데 Maven 은 settings 미러의 다른 저장소에 물어 404
                        log.info("접수: Maven 은 이 URL 이 아니라 settings.xml 의 미러 URL 로 받는다 — 둘이 다른 저장소면 sbmig 는 읽어도 Maven 은 "
                                 "'Unresolveable build extension' 으로 실패할 수 있다(그 미러가 가리키는 저장소에 %s 가 있어야 한다)", c.coordinates)
                    continue
                log.warning("접수: 사내 부모 체인의 빌드 확장 %s (선언: %s) 을 sbmig 가 로컬 저장소에서도 저장소에서도 읽지 못했다 (%s) — Maven 도 "
                            "못 받으면 모든 Maven 호출이 'Unresolveable build extension' 으로 실패한다. Nexus 에 그 pom·jar(와 부모 pom)을 "
                            "올리거나 부모 pom 에서 확장을 빼야 한다(배포 전송 확장은 패키징에 필요 없다)", c.coordinates, c.declared_by, c.where)
                self._flag("parent-extension-unresolvable")
                line = (f"the company parent {c.declared_by} declares the build extension {c.coordinates}, which sbmig could not read "
                        f"from the local repository or the repository set ({c.where}) — if Maven cannot download it either, every Maven "
                        f"call fails with 'Unresolveable build extension' until Nexus serves its pom and jar (and their parent poms) or "
                        f"the parent pom drops it (a deploy transport is never needed for packaging)")
                if line not in report.manual_followups:
                    report.manual_followups.append(line)
            self.ws.journal({"event": "parent_build_extensions", "checks": [c.line() for c in checks],
                             "unresolvable": [c.coordinates for c in checks if not c.readable],
                             "markers_removed": {c.coordinates: c.markers_removed for c in checks if c.markers_removed}})
        except Exception as e:  # noqa: BLE001 - never lose the run over a pre-check
            log.warning("parent build extension pre-check failed: %s: %s", type(e).__name__, e)

    def _check_build_tool(self, report: RunReport) -> bool:
        """회사 결정: Maven 프로젝트만 받는다. Gradle / 빌드 없음은 Maven 을 부르기 전에 중단(exit 3). v2.1j: Ant
        (build.xml 만 있고 pom.xml 없음)는 ``intake.ant_to_maven`` 이면 pom.xml 을 합성·커밋하고 Maven 처럼 계속한다
        (``_synthesize_ant_pom``); 꺼져 있거나 합성이 실패하면 예전처럼 거부.

        Catalog S4 at intake: only Maven projects; Gradle / no build abort before any Maven call. v2.1j: an Ant
        project gets a synthesized pom.xml when `intake.ant_to_maven` is on and then runs as a Maven project; off or
        a failed synthesis → the old rejection. `step_analyze` keeps the same check (on `profile.build_tool`) as a
        fallback — it passes once the synthesized pom exists."""
        try:
            from sbmig.analyze import detect_build_tool
        except ImportError:  # pragma: no cover - analyze component missing; step_analyze still checks
            return True
        det = detect_build_tool(self.ws)
        intake = getattr(self.cfg, "intake", None)
        if det.tool == "ant" and intake is not None and getattr(intake, "ant_to_maven", False):
            return self._synthesize_ant_pom(report, det)
        if det.tool != "maven":
            report.flags.append(f"unsupported-build-tool:{det.tool}")
            self._abort(report, det.reason)
            self.ws.journal({"event": "build_tool", "tool": det.tool, "ok": False, "reason": det.reason})
            return False
        self.ws.journal({"event": "build_tool", "tool": "maven", "ok": True})
        return True

    def _synthesize_ant_pom(self, report: RunReport, det: Any) -> bool:
        """v2.1j 현업 요청("Ant 프로젝트도 Maven 으로 변경 후 마이그레이션"): build.xml + 폴더 구조로 pom.xml 을 쓰고
        (``sbmig.analyze.ant.synthesize_pom``, 파일만 씀) 여기서 ``intake: pom.xml synthesized from build.xml`` 로
        커밋한다. 순서가 중요하다: as-is 스냅샷(``git_init``) 뒤라서 changes.patch 에 pom.xml 이 "추가" 로 보이고,
        effective-pom 캡처(``_capture_effective``, pom 필요) 앞이다. 실패(모듈 없음·예외·``ok=False``)는 예전 거부와
        같다: ``unsupported-build-tool:ant`` + 중단(exit 3) — 플릿은 그것을 건너뜀으로 센다.

        v2.1j: write pom.xml from build.xml (`synthesize_pom` writes the file only), commit it, flag `pom-synthesized`
        (+ `jar-coordinates-unknown` when unknown jars were declared / left out), keep the result on
        `report.pom_synthesis` and continue as a Maven project. Runs after the as-is snapshot (changes.patch shows
        the pom as an addition) and before the effective-pom capture (which needs a pom). Any failure — the analyzer
        module missing, an exception, `ok=False` — is the old rejection: `unsupported-build-tool:ant`, exit 3."""
        build_file = getattr(det, "ant_build_file", None) or "build.xml"
        try:
            # importlib (not `from sbmig.analyze import ant`): the module is looked up in sys.modules on every call,
            # so a test can inject a fake analyzer without the real one being imported first
            synthesize = importlib.import_module("sbmig.analyze.ant").synthesize_pom
        except (ImportError, AttributeError) as e:
            return self._reject_ant(report, f"Ant build.xml detected and no pom.xml: the pom synthesis "
                                            f"(sbmig.analyze.ant) is not available in this installation "
                                            f"({type(e).__name__}: {e}) — convert the build to Maven first")
        try:
            res = synthesize(self.ws, self.cfg)
        except Exception as e:  # an analyzer bug must end as a rejection, not lose the report
            log.exception("pom synthesis failed")
            return self._reject_ant(report, f"pom.xml could not be synthesized from {build_file}: "
                                            f"{type(e).__name__}: {e} (Ant project rejected: convert the build to "
                                            f"Maven first)")
        if not getattr(res, "ok", False):
            why = getattr(res, "error", None) or "unknown reason"
            return self._reject_ant(report, f"pom.xml could not be synthesized from "
                                            f"{getattr(res, 'build_file', None) or build_file}: {why} (Ant project "
                                            f"rejected: convert the build to Maven first)")
        self.ws.commit("intake: pom.xml synthesized from build.xml")
        report.pom_synthesis = res
        self._flag("pom-synthesized")
        if res.unknown_jars:
            self._flag("jar-coordinates-unknown")
        counts = res.counts
        # 운영자가 화면에서 옮겨 적는 한 줄(한국어) — jar 출처별 수. manifest 는 메모일 뿐이라 미상으로 센다
        log.info("접수: Ant build.xml → pom.xml 합성 (jar %d개: pom.properties %d · 이름표 %d · 조회 %d · 미상 %d)",
                 len(res.jars), counts.get("pom_properties", 0), counts.get("name_table", 0),
                 counts.get("sha1_lookup", 0), counts.get("unknown", 0) + counts.get("manifest", 0))
        self.ws.journal({"event": "pom_synthesized", "pom": res.pom_path, "build_file": res.build_file,
                         "packaging": res.packaging, "jars": len(res.jars), "unknown": len(res.unknown_jars),
                         "dropped": len(res.dropped_jars), "warnings": len(res.warnings)})
        self.ws.journal({"event": "build_tool", "tool": "ant→maven", "ok": True, "build_file": res.build_file})
        return True

    def _reject_ant(self, report: RunReport, why: str) -> bool:
        """Ant 프로젝트를 예전처럼 거부: ``unsupported-build-tool:ant`` + 중단 사유 + 저널 ``build_tool`` (ok False)."""
        report.flags.append("unsupported-build-tool:ant")
        self._abort(report, why)
        self.ws.journal({"event": "build_tool", "tool": "ant", "ok": False, "reason": why})
        return False

    def _intake_layout(self, report: RunReport) -> bool:
        """루트 pom 없이 서로 의존하는 형제 pom 들(회사 사례 APP/BATCH, APP/WEB)에는 루트 aggregator pom 을
        만들어 한 reactor 로 빌드하게 한다. 모듈이 트리에 없는 EAR 루트는 거부한다 — EAR 은 코드가 없고, 그
        WAR 의존성이 back 모듈로 가면 안 된다. v2.1k: 끝으로(aggregator 를 만들었으면 그 뒤에) reactor 에 두 번
        닿는 모듈의 중복 ``<module>`` 을 지운다(``_intake_reactor_dedupe``) — effective-pom 캡처 앞, Maven 호출 전.

        2026-10-23 (company case: APP/BATCH, APP/WEB, APP/EAR with no root pom): sibling poms that depend on each
        other get a generated root aggregator pom (`layout.generate_aggregator`), so they build in one reactor and the
        restructure merges them; a root pom that is an EAR packaging modules missing from the tree (a run on APP/EAR
        alone) is rejected — an EAR holds no code, and its WAR dependency must never reach the back module. v2.1k
        (2026-10-29): then a module listed twice in the reactor loses its duplicate listing (`_intake_reactor_dedupe`),
        before the effective-pom capture and any other Maven call."""
        try:
            from sbmig.analyze.aggregator import ear_without_modules
        except ImportError:  # pragma: no cover
            return True
        self._intake_stray_poms(report)          # v2.1z-h: first — every later step must see the clean pom set
        why = ear_without_modules(self.ws)
        if why:
            report.flags.append("ear-without-modules")
            self._abort(report, why)
            self.ws.journal({"event": "ear_without_modules", "reason": why})
            return False
        if getattr(self.cfg.layout, "generate_aggregator", True):
            self._intake_aggregator(report)
        self._intake_reactor_dedupe(report)
        self._intake_build_extensions(report)     # 2026-11-05: before the first Maven call — an unresolvable one stops everything
        return True

    def _intake_build_extensions(self, report: RunReport) -> None:
        """2026-11-05 회사 실행: ``<build><extensions>`` 의 ``org.apache.maven.wagon:wagon-webdav:1.0-beta-2`` 를 저장소가 더는
        주지 않아 Maven 이 모델 단계에서 ``Unresolveable build extension`` 으로 멈췄다 — OpenRewrite 도 빌드도 돌지 않았다.
        배포 전송 확장은 패키징에 필요 없으므로(``knowledge/libraries/build_extensions.yml``) 첫 Maven 호출 전에 모든 pom 에서
        지우고 ``intake: deploy transport extension(s) removed (…)`` 로 커밋, 플래그 ``deploy-extensions-removed``, 저널
        ``build_extensions``, 인수인계 한 줄씩. 목록에 없는 확장은 기록만. 예외로 실행을 멈추지 않는다.

        Remove the deploy-transport build extensions (knowledge list) from every pom before the first Maven call: commit,
        flag `deploy-extensions-removed`, journal `build_extensions`, one Handover line each; unlisted extensions are only
        recorded. Never raises."""
        if not getattr(self.cfg.intake, "strip_deploy_extensions", True):
            return
        try:
            from sbmig.analyze import build_extensions as bx
            res = bx.strip_deploy_extensions(self.ws, self.cfg)
            if not res.removed and not res.kept:
                return
            if res.removed:
                self.ws.commit(bx.commit_message(res))
                self._flag("deploy-extensions-removed")
                report.manual_followups.extend(f for f in bx.followups(res) if f not in report.manual_followups)
                log.info("접수: 배포 전송 빌드 확장 %d개 제거 (%s) — mvn deploy 전용이라 패키징과 OpenRewrite 에 필요 없다",
                         len(res.removed), ", ".join(f"{r.coordinates} in {r.where()}" for r in res.removed))
            for pom, coords in res.kept:
                log.info("접수: 빌드 확장 %s (%s) 은 배포 전송 목록에 없어 그대로 둔다 — 저장소에 없으면 수정 루프가 다룬다", coords, pom)
            self.ws.journal({"event": "build_extensions", "removed": [f"{r.coordinates} in {r.where()}" for r in res.removed],
                             "kept": [f"{c} in {p}" for p, c in res.kept], "poms": res.poms_changed})
        except Exception as e:  # noqa: BLE001 - never lose the run over this: Maven then reports it as before
            log.warning("build extension check failed: %s: %s", type(e).__name__, e)

    def _intake_stray_poms(self, report: RunReport) -> None:
        """v2.1z-h 회사 사례(2026-11-03): 입력 zip 의 ``boot/bin/`` 에 boot 모듈의 통째 복사본(src, .project, pom.xml — 같은
        좌표)이 있었고, pom 탐색이 그것을 독립 pom 으로 읽자 구조 검토 LLM 이 "등록 안 된 모듈" 로 보고 루트 ``<modules>`` 에
        넣었다 → Maven "is duplicated in the reactor" → ``reactor_duplicate`` 로 LLM 없이 차단. 여기서 떠도는 복사본
        (``find_stray_pom_copies``)을 찾아 ``ws.ignored_dirs`` 에 넣고(모든 스캔이 target/ 처럼 가지치기), 플래그
        ``stray-pom-ignored``, 저널 ``stray_pom_ignored``, 인수인계 한 줄씩. 파일은 건드리지 않는다(커밋 없음). 예외 없음.

        Stray pom copies (`sbmig.analyze.pom_parser.find_stray_pom_copies`): their directories go into
        `ws.ignored_dirs` (every scan prunes them like build output), flag `stray-pom-ignored`, journal
        `stray_pom_ignored`, one Handover follow-up per copy. Nothing is written or committed. Never raises."""
        try:
            from sbmig.analyze.pom_parser import find_stray_pom_copies
            strays = find_stray_pom_copies(self.ws)
        except Exception as e:  # noqa: BLE001 - a detector never stops the run
            log.debug("stray pom detection failed: %s: %s", type(e).__name__, e)
            return
        if not strays:
            return
        ignored = getattr(self.ws, "ignored_dirs", None)
        if isinstance(ignored, set):
            ignored.update(st.dir for st in strays if st.dir)
        self._flag("stray-pom-ignored")
        for st in strays:
            line = stray_pom_followup(st)
            if line not in report.manual_followups:
                report.manual_followups.append(line)
        # 운영자가 화면에서 옮겨 적는 한 줄(한국어)
        log.info("접수: pom 복사본 %d개 무시 — %s", len(strays),
                 "; ".join(f"{st.path} (= {st.duplicate_of}, {st.coordinates})" for st in strays))
        self.ws.journal({"event": "stray_pom_ignored", "poms": [st.path for st in strays],
                         "duplicates_of": [st.duplicate_of for st in strays],
                         "coordinates": [st.coordinates for st in strays], "dirs": sorted(st.dir for st in strays)})

    def _intake_aggregator(self, report: RunReport) -> None:
        """루트 pom 이 없고 형제 pom 들이 서로 의존하면 루트 aggregator pom 을 쓰고 커밋한다(``plan_aggregator``).
        쓰기에 실패해도 실행은 계속한다(후속 조치 한 줄)."""
        from sbmig.analyze.aggregator import plan_aggregator, write_aggregator
        plan = plan_aggregator(self.ws)
        if plan is None:
            return
        try:
            path = write_aggregator(self.ws, plan)
            self.ws.commit(f"intake: generated root aggregator pom ({', '.join(plan.modules)})")
        except Exception as e:  # never lose the run over this: the analysis then sees no root pom as before
            log.warning("could not write the aggregator pom: %s", e)
            report.manual_followups.append(f"sibling poms without a root pom: the aggregator pom could not be written ({type(e).__name__}: {e}) — add a root pom.xml listing {', '.join(plan.modules)}")
            return
        if "aggregator-generated" not in report.flags:
            report.flags.append("aggregator-generated")
        msg = (f"no aggregator pom: sbmig generated {path} ({plan.artifact_id}, modules {', '.join(plan.modules)}) — "
               f"{plan.reason}; the as-is build of each module stays as it was")
        if plan.excluded and "left out" not in plan.reason:
            msg += f"; not linked to them and left out: {', '.join(plan.excluded)}"
        report.manual_followups.append(msg)
        if plan.excluded:
            # v2.1z-a: 빠진 pom 은 인수인계에 따로 한 줄 — 집계 pom 의 <modules> 에 넣고 다시 돌리면 함께 전환된다
            report.manual_followups.append(
                f"poms left out of the generated root ({', '.join(plan.excluded)}): no aggregator lists them and no module "
                f"depends on them — if they belong to the application, add them to the <modules> of "
                f"{plan.modules[0]}/pom.xml (or the generated root pom.xml) and run sbmig again; they were not migrated")
        log.info("%s", msg)
        self.ws.journal({"event": "aggregator_generated", "path": path, "artifact_id": plan.artifact_id,
                         "name": plan.name, "modules": plan.modules, "excluded": plan.excluded})

    def _intake_reactor_dedupe(self, report: RunReport) -> None:
        """v2.1k 회사 사례: 루트 pom 이 ``APP, DEV`` 를, ``APP/pom.xml`` 이 ``../DEV, ./EAR, ./BATCH`` 를 나열해 DEV 가
        reactor 에 두 번 닿았다 → Maven 이 "is duplicated in the reactor" 로 reactor 전체를 거부해 effective-pom 캡처,
        OpenRewrite(레시피가 하나도 안 돌았다), 컴파일, 빌드가 모두 실패했고 수정 루프는 LLM 만 불렀다. pom 구조
        문제이므로 Maven 을 부르기 전에 여기서 결정적으로 고친다: 루트가 아닌 집계 pom 의 중복 줄을 지우고(루트 목록은
        그대로) ``intake: reactor duplicate removed (…)`` 로 커밋, 플래그 ``reactor-duplicate-removed``, 저널
        ``reactor_dedupe``, 쉬운 말 후속 조치. 꺼진 프로필의 중복은 후속 조치 메모만. 예외로 실행을 멈추지 않는다.

        v2.1k: remove the duplicate `<module>` listings of the non-root aggregators (`dedupe_reactor`), commit them,
        flag `reactor-duplicate-removed`, journal `reactor_dedupe`, plain-words follow-ups; noted-only findings (a
        profile not active by default) become follow-ups without a commit. Never raises."""
        try:
            from sbmig.analyze import aggregator as agg
        except ImportError:  # pragma: no cover
            return
        try:
            dups = agg.reactor_duplicates(self.ws)
            if not dups:
                return
            notes = agg.dedupe_reactor(self.ws, duplicates=dups)
            removed = [(d, pom, text) for d in dups for pom, text in d.removed]
            message = agg.reactor_commit_message(dups)
            if message:
                self.ws.commit(message)
                self._flag("reactor-duplicate-removed")
            report.manual_followups.extend(f for f in agg.reactor_followups(dups) if f not in report.manual_followups)
            # 운영자가 화면에서 옮겨 적는 한 줄(한국어)
            log.info("접수: reactor 중복 모듈 %d개 정리 (지운 줄 %d · 메모 %d)", len(dups), len(removed),
                     len(notes) - len(removed))
            self.ws.journal({"event": "reactor_dedupe", "duplicates": [d.dir for d in dups],
                             "removed": [f"{pom}: {text}" for _, pom, text in removed],
                             "kept": [f"{d.keep[0]}: {d.keep[1]}" for d in dups if d.keep],
                             "notes": notes})
        except Exception as e:  # noqa: BLE001 - never lose the run over this: Maven then reports it as before
            log.warning("reactor duplicate check failed: %s: %s", type(e).__name__, e)

    def step_analyze(self, report: RunReport, stop_after: Phase | None) -> bool:
        """2. 분석: 프로필(케이스 타입·Java·Spring/Boot 버전·라이브러리) + 라이브러리 권고(EOL, 안내만).
        False = 멈춤 또는 중단(Maven 아님, pom.xml 없음).

        2. analyze: profile + library advisories. False = stop or abort (not Maven, no pom.xml)."""
        with stage("analyze"):
            return self._step_analyze(report, stop_after)

    def _step_analyze(self, report: RunReport, stop_after: Phase | None) -> bool:
        t = self.clock()
        profile = self.analyzer(self.ws, self.cfg)
        self.profile = profile
        report.profile = profile
        report.case_type = profile.case_type
        report.project_name = profile.name or report.project_name
        self.ws.save_model("profile.json", profile)
        self.ws.journal({"event": "analyze", "case_type": profile.case_type.value, "poms": len(profile.poms),
                         "build_tool": profile.build_tool, "java": profile.java_version,
                         "spring_boot": profile.spring_boot_version,
                         "properties_external": len(getattr(getattr(profile, "properties", None), "external", None)
                                                    or [])})
        if self._check_adf(report, profile):
            self._timed("analyze", t)
            return False
        self._check_hollow(report, profile)
        self._note_bundled_jars(profile)
        report.advisories = self._evaluate_advisories(profile)
        self._timed("analyze", t)
        if profile.build_tool != "maven":
            self._abort(report, f"build tool '{profile.build_tool}' is not supported (Maven only)")
            return False
        if not profile.poms:
            self._abort(report, "no pom.xml found in the project")
            return False
        if not self._root_pom_present(report, profile):
            return False
        return not self._stop(stop_after, Phase.ANALYZE)

    def _root_pom_present(self, report: RunReport, profile: ProjectProfile) -> bool:
        """v2.1k: 루트 pom.xml 이 없으면(접수가 집계 pom 을 만들지 못함) MSA 가 아닌 한 여기서 멈춘다(exit 3, 플래그
        ``no-root-pom``) — Maven 을 pom 없는 폴더에서 부르면 OpenRewrite·컴파일·빌드가 "there is no POM in this
        directory" 로, 재구성이 "root pom not found: pom.xml" 로 실패하고 LLM 만 쓴다. MSA 는 서비스마다 따로 돈다.

        v2.1k: without a root pom.xml (no aggregator generated) a non-MSA run stops here (exit 3, flag `no-root-pom`)
        instead of failing in every Maven call; MSA services are migrated one by one and need no root pom."""
        if self.ws.exists("pom.xml") or profile.case_type == CaseType.MSA:
            return True
        try:
            from sbmig.analyze.aggregator import no_root_pom_reason
            why = no_root_pom_reason(self.ws, getattr(self.cfg.layout, "generate_aggregator", True))
        except ImportError:  # pragma: no cover
            why = "no pom.xml at the project root and none was generated"
        report.flags.append("no-root-pom")
        self._abort(report, why)
        self.ws.journal({"event": "no_root_pom", "reason": why})
        return False

    def _check_adf(self, report: RunReport, profile: ProjectProfile) -> bool:
        """v2.1z-d (2026-11-02, 현업 결정: ADF 프로젝트는 변환하지 않는다): 분석이 프로필에 남긴 ADF 판정(``profile.adf``)을 적용한다.
        근거 계열이 **둘 이상**(``is_adf``)이면 플래그 ``adf-project``, 저널 ``adf_project``(계열 · 근거 경로 · 개수), 인수인계 첫 줄
        "ADF 프로젝트라 변환이 불가능합니다 (…규모…)" — ``intake.adf_policy: stop``(기본)이면 여기서 중단(exit 3: Maven · OpenRewrite ·
        LLM 호출 없음) → True, ``continue`` 면 보고만 하고 계속(권장하지 않음) → False. 한 계열뿐이면(``traces``) 플래그 ``adf-traces`` 와
        메모("ADF 흔적: 변환은 계속하되 확인 필요")만 남기고 계속 → False. 오탐 금지: ``.jpr`` 기술 이름이나 ``adf-config.xml`` 하나로는
        멈추지 않는다.

        v2.1z-d: apply the profile's ADF verdict. Two or more evidence families → flag `adf-project`, journal `adf_project`,
        Handover first line ("ADF 프로젝트라 변환이 불가능합니다 (…)"); `intake.adf_policy: stop` (default) aborts here (exit 3,
        no Maven / LLM call) → True, `continue` = report only → False. One family → soft flag `adf-traces` + a note → False."""
        summary = getattr(profile, "adf", None)
        if summary is None or not getattr(summary, "families", None):
            return False
        try:
            from sbmig.analyze.adf import ADF_FLAG, TRACES_FLAG, adf_abort_reason, adf_followup, traces_followup
        except ImportError:  # pragma: no cover
            return False
        counts = {"families": list(summary.families), "evidence": dict(summary.evidence), "entities": summary.entities,
                  "tables": summary.tables_total, "view_objects": summary.view_objects, "app_modules": summary.app_modules,
                  "page_defs": summary.page_defs, "task_flows": summary.task_flows, "pages": summary.pages,
                  "java_total": summary.java_total, "java_adf": summary.java_adf, "by_dir": dict(summary.by_dir)}
        if not summary.is_adf:                                   # exactly one family: traces, the conversion goes on
            self._flag(TRACES_FLAG)
            note = traces_followup(summary)
            if note not in report.manual_followups:
                report.manual_followups.append(note)
            self.ws.journal({"event": "adf_traces", **counts})
            log.warning("%s", note)
            return False
        policy = str(getattr(getattr(self.cfg, "intake", None), "adf_policy", "stop") or "stop")
        self._flag(ADF_FLAG)
        line = adf_followup(summary, policy)
        if line not in report.manual_followups:
            report.manual_followups.insert(0, line)
        self.ws.journal({"event": "adf_project", "policy": policy, **counts})
        log.error("%s", line)                                    # the one greppable line of a rejected ADF project
        if policy != "stop":
            return False
        self._abort(report, adf_abort_reason(policy))
        return True

    def _check_hollow(self, report: RunReport, profile: ProjectProfile) -> None:
        """v2.1z-c (2026-11-02): 속 빈 리액터 판정 — pom 들이 선언한 소스·웹 루트 안에는 소스가 없고 트리의 다른 곳에 있으면
        플래그 ``hollow-reactor``(성공 아님, exit 2, 인수인계 첫 줄, 저널 ``hollow_reactor``); 안쪽보다 바깥에 java 가 더
        많기만 하면 ``sources-outside-reactor``(알림만). 실행은 계속된다(빌드·검토는 그대로) — 회사 사례: ``APP/WEB/pom.xml``
        이 ``DEV/`` 의 산출물을 포장만 해서 pom · web.xml 만 옮기고 "성공" 으로 끝났다.

        v2.1z-c: hollow-reactor detection right after the profile (`sbmig.analyze.hollow`). `hollow` → flag
        `hollow-reactor` (the run goes on but is not a success: exit 2, Handover first line, journal `hollow_reactor`);
        `sources_outside` → flag `sources-outside-reactor` and a follow-up only. Never stops the run."""
        try:
            from sbmig.analyze.hollow import (HOLLOW_FLAG, OUTSIDE_FLAG, detect_hollow, hollow_followup,
                                              outside_followup)
            res = detect_hollow(self.ws, getattr(profile, "poms", None) or [])
        except Exception as e:  # noqa: BLE001 - analysis must never stop the run
            log.warning("hollow-reactor check skipped: %s: %s", type(e).__name__, e)
            return
        if res.hollow:
            self._flag(HOLLOW_FLAG)
            line = hollow_followup(res)
            if line not in report.manual_followups:
                # v2.1z-d: ADF 줄이 이미 맨 앞이면(intake.adf_policy: continue) 그 다음 자리 — 인수인계 순서와 같게
                from sbmig.analyze.adf import ADF_PREFIX
                pos = 1 if report.manual_followups and report.manual_followups[0].startswith(ADF_PREFIX) else 0
                report.manual_followups.insert(pos, line)
            log.warning("hollow reactor: %s outside the modules (%s)%s — the run will not count as a success",
                        res.counts_text(), res.by_dir_text(), res.unresolved_text())
            self.ws.journal({"event": "hollow_reactor", **res.as_journal()})
        elif res.sources_outside:
            self._flag(OUTSIDE_FLAG)
            line = outside_followup(res)
            if line not in report.manual_followups:
                report.manual_followups.append(line)
            log.info("sources outside the reactor: %s (%s)", res.counts_text(), res.by_dir_text())
            self.ws.journal({"event": "sources_outside_reactor", **res.as_journal()})

    def _classify_bundled_jars(self, report: RunReport) -> None:
        """v2.1z-i: 보고서의 동봉 jar 표를 위해, 좌표가 알려졋고 그 g:a 를 목표 부모 체인(재구성 뒤 캡처
        ``effective-depmgmt-after-restructure.json``, 없으면 루프의 관리 키)이 관리하는 jar 에 note
        "superseded: g:a is managed by the target parent chain" 을 붙인다 — 그런 jar 는 연결 대상이 아니다. 예외 없음.

        Mark the bundled jars whose known g:a the target parent chain manages (the post-restructure capture, else the
        loop's managed keys) with the note `superseded: …` for the report table. Never raises."""
        jars = list(getattr(getattr(report, "profile", None), "bundled_jars", None) or [])
        if not jars:
            return
        try:
            from sbmig.build.effective import AFTER_RESTRUCTURE_FILE, load_effective
            data = load_effective(self.ws, AFTER_RESTRUCTURE_FILE) or {}
            keys = {str(k) for k in (data.get("managed") or {})}
            for mod in (data.get("by_module") or {}).values():
                if isinstance(mod, dict):
                    keys |= {str(k) for k in (mod.get("managed") or {})}
            if not keys and self.loop is not None:
                keys = set(self.loop._managed_keys())  # noqa: SLF001 - same source of truth as the pre-try
            for j in jars:
                key = f"{j.group_id}:{j.artifact_id}" if j.group_id and j.artifact_id else ""
                if key and key in keys and "superseded:" not in (j.note or ""):
                    mark = f"superseded: {key} is managed by the target parent chain"
                    j.note = f"{j.note}; {mark}" if j.note else mark
        except Exception as e:  # noqa: BLE001 - a report decoration must never fail the run
            log.debug("bundled jar classification skipped: %s: %s", type(e).__name__, e)

    def _note_bundled_jars(self, profile: ProjectProfile) -> None:
        """v2.1z-i: 분석이 찾은 동봉 jar(트리 안에 있지만 어떤 pom 도 선언하지 않은 jar)를 실행 기록에 남긴다 — 플래그
        ``bundled-jars``, 저널 ``bundled_jars``(경로 · 좌표), 운영자 로그 한 줄, ``logs/bundled-jars.json``(JarCoordinate 목록).
        연결(의존성 선언)은 빌드 오류가 그 jar 를 필요로 할 때 수정 루프의 ``sbmig-bundled`` 가 한다. 절대 예외를 내지 않는다.

        v2.1z-i: record the profile's bundled jars — flag `bundled-jars`, journal `bundled_jars`, one operator log line,
        `logs/bundled-jars.json`. Wiring happens in the fix loop (`sbmig-bundled`) on evidence of a build error. Never raises."""
        try:
            jars = list(getattr(profile, "bundled_jars", None) or [])
            if not jars:
                return
            self._flag("bundled-jars")
            paths = [j.path for j in jars]
            coords = {j.path: (f"{j.group_id}:{j.artifact_id}:{j.version}" if j.group_id and j.artifact_id else None)
                      for j in jars}
            self.ws.journal({"event": "bundled_jars", "jars": paths, "coordinates": coords})
            names = [p.rsplit("/", 1)[-1] for p in paths]
            log.info("분석: 동봉 jar %d개 (pom 미선언) — %s", len(jars),
                     ", ".join(names[:5]) + (" …" if len(names) > 5 else ""))
            out = logs_dir(self.ws)
            out.mkdir(parents=True, exist_ok=True)
            (out / "bundled-jars.json").write_text(
                json.dumps([j.model_dump() for j in jars], ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 - a record of facts must never stop the run
            log.warning("bundled-jar record skipped: %s: %s", type(e).__name__, e)

    def step_plan(self, report: RunReport, stop_after: Phase | None,
                  dry_run: bool) -> list[tuple[Phase, list[tuple[int, PlanStep]]]] | None:
        """3. 계획: 결정적 계획(레시피·재구성 모드), 실행의 오류 목록과 ``BuildFixLoop`` 를 만들고, 같은
        단계가 이어진 계획 단계들을 묶어 돌려준다(변환 → 재구성(+구조 검토) → 검증). None = 계획 뒤 멈춤.

        3. plan: the migration plan, the run's error list and `BuildFixLoop`, and the phase groups (consecutive
        plan steps of the same phase; v2.1: transform (rewrite + compile) → restructure (+ review) → verify; MSA
        plans may repeat a phase per target). None = the run stops after `plan`."""
        with stage("plan"):
            return self._step_plan(report, stop_after, dry_run)

    def _step_plan(self, report: RunReport, stop_after: Phase | None,
                   dry_run: bool) -> list[tuple[Phase, list[tuple[int, PlanStep]]]] | None:
        profile = self.profile
        assert profile is not None
        t = self.clock()
        plan = self._plan_with_advisories(profile, report.advisories)
        plan = self._advise_plan(plan)
        try:    # X9: repositories declared inside the poms — a note, a follow-up when no settings mirror covers them
            from sbmig.build.mvn import repository_notes
            for n in repository_notes(profile.poms, self.cfg):
                if n not in plan.notes:
                    plan.notes.append(n)
                (log.warning if n.startswith("followup:") else log.info)("%s", n)
        except Exception as e:  # noqa: BLE001 - never fail planning over a note
            log.debug("repository notes skipped: %s", e)
        self.plan = plan
        report.plan = plan
        self.ws.save_model("plan.json", plan)
        self.ws.journal({"event": "plan", "steps": [s.id for s in plan.steps], "notes": plan.notes})
        for n in plan.notes:
            if n.startswith("followup:"):
                report.manual_followups.append(n[len("followup:"):].strip())
        self._timed("plan", t)
        if self._stop(stop_after, Phase.PLAN):
            return None
        # 4. phases ------------------------------------------------------------
        # 오류 목록은 계획 뒤에 연다: 첫 레코드는 transform.compile 의 오류(seen compile)
        self._start_error_list(dry_run)
        self.loop = BuildFixLoop(self.cfg, self.ws, self.build_runner, self.triager, self.fixer, self.kb, profile,
                                 plan_notes=plan.notes,
                                 budget=self.budget, dry_run=dry_run, blocked=self.blocked,
                                 locator=self._resolve_locator(), advisories=list(report.advisories or []),
                                 script_runner=self.script_runner, edit_applier=self._edit_applier,
                                 errors=self.errors)
        self._handoff_restructure()        # v2.1v: a loop made after a restructure gets its results too
        steps = list(plan.steps)
        # 빌드 게이트: 계획에 최종 빌드가 없어도 always_final_verify 면 verify.build 를 덧붙인다
        if self.cfg.loop.always_final_verify and not any(s.kind == StepKind.BUILD and s.phase == Phase.VERIFY
                                                         for s in steps):
            steps.append(PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY,
                                  title="final mvn clean package", build_goal="clean package",
                                  reason="always_final_verify"))
        self._run_steps = list(steps)
        groups: list[tuple[Phase, list[tuple[int, PlanStep]]]] = []
        for i, s in enumerate(steps):
            if groups and groups[-1][0] == s.phase:
                groups[-1][1].append((i, s))
            else:
                groups.append((s.phase, [(i, s)]))
        return groups

    def step_phase(self, report: RunReport, phase: Phase, items: list[tuple[int, PlanStep]],
                   stop_after: Phase | None) -> PhaseResult | None:
        """단계 묶음 하나를 돈다 → PhaseResult(``report.phases`` 에도 붙음). ``stop_after`` 뒤의 묶음이면
        None. 단계 안에서 예외가 나도 그 단계가 한 일(예: MSA
        의 svc-a 성공 빌드)은 보고서에 남긴 뒤 다시 올린다.

        One phase group: run its steps. Returns the `PhaseResult` (also appended to `report.phases`), or None when
        the group lies after `stop_after` (``phase_skipped``). Raises `_RunAborted` when the fix loop aborted the run."""
        if stop_after is not None and _PHASE_ORDER.index(phase) > _PHASE_ORDER.index(stop_after):
            # MSA plans repeat jdk21/spring per service: skip only the phases after `stop_after`
            self.ws.journal({"event": "phase_skipped", "phase": phase.value, "reason": f"stop_after={stop_after.value}"})
            return None
        t = self.clock()
        pr = PhaseResult(phase=phase, step_ids=[s.id for _, s in items], ok=True)
        self.ws.journal({"event": "phase_start", "phase": phase.value, "steps": pr.step_ids})
        try:
            with stage(phase.value):
                self._run_phase(pr, items)
        except BaseException as e:
            # round 10: an exception inside a phase (git timeout, a bug) must not lose what the phase did — svc-a's
            # green verify of an MSA run stays in the report next to the abort
            pr.ok = False
            pr.notes.append(f"phase interrupted: {type(e).__name__}: {e}")
            pr.finished_at = utcnow()
            report.phases.append(pr)        # its blocked items are in the run-wide registry (report.blocked)
            self.ws.journal({"event": "phase_end", "phase": phase.value, "ok": False, "build_ok": pr.build_ok,
                             "iterations": pr.iterations, "attempts": len(pr.attempts),
                             "remaining_errors": len(pr.remaining_errors), "blocked": [b.id for b in pr.blocked],
                             "interrupted": f"{type(e).__name__}: {e}"[:300]})
            raise
        pr.finished_at = utcnow()
        report.phases.append(pr)
        self.ws.journal({"event": "phase_end", "phase": phase.value, "ok": pr.ok, "build_ok": pr.build_ok,
                         "iterations": pr.iterations, "attempts": len(pr.attempts),
                         "remaining_errors": len(pr.remaining_errors), "blocked": [b.id for b in pr.blocked]})
        if self.loop is not None and self.loop.aborted:
            self.ws.journal({"event": "run_aborted", "phase": phase.value, "reason": self.loop.aborted})
            self._timed(phase.value, t)
            raise _RunAborted(self.loop.aborted)
        if not pr.ok and phase != Phase.VERIFY:
            self._phase_failed = True
        self._timed(phase.value, t)
        return pr

    def step_finish(self, report: RunReport, stop_after: Phase | None) -> None:
        """마지막 묶음 뒤: ``stop_after`` 가 돌린 단계를
        가리켰으면 그 사실(``stopped-after-*`` 플래그)을 남긴다.

        After the last phase group: record `stop_after` when it named a phase that the groups ran."""
        if stop_after is not None and stop_after != Phase.VERIFY:
            self._stop(stop_after, stop_after)

    def _resolve_locator(self) -> Any:
        if self._locator is not None:
            return self._locator or None
        return default_locator() if self._real_maven else None

    # ------------------------------------------------------------------ library advisories (analyze / plan side)
    def _evaluate_advisories(self, profile) -> list:
        """이 프로젝트의 라이브러리 권고(EOL: springfox, hystrix, sleuth …). 회사 정책상 차단은 없고 안내만
        한다(스크립트 프롬프트의 힌트 + 보고서 인수인계 섹션). 예외 없음.

        knowledge/libraries/advisories.yml findings for this project (springfox, hystrix, sleuth …). Never
        raises; an unavailable advisory package just means no findings. The finalize side (verify → outcome,
        handover, follow-ups) lives in `_finalize`."""
        adv_cfg = getattr(self.cfg, "advisories", None)
        if adv_cfg is None or not adv_cfg.enabled:
            return []
        try:
            from sbmig.advisory import evaluate_advisories
            findings = list(evaluate_advisories(profile, self.cfg) or [])
        except Exception as e:
            log.warning("library advisories unavailable: %s", e)
            self.ws.journal({"event": "advisories", "error": f"{type(e).__name__}: {e}"})
            return []
        self.ws.journal({"event": "advisories",
                         "findings": [{"id": f.advisory_id, "library": f.library, "status": f.status,
                                       "policy": f.policy, "version": f.detected_version} for f in findings]})
        return findings

    def _plan_with_advisories(self, profile, findings: list):
        """계획기 호출. 권고는 계획기가 ``advisories=`` 를 받을 때만 넘긴다(테스트 대역·옛 계획기는 못 받음).

        Planner call; findings are passed as `advisories=` when the planner accepts them (fakes in tests and
        older planners do not)."""
        if findings:
            try:
                plan = self.planner(profile, self.cfg, self.kb, advisories=findings)
            except TypeError as e:
                if "advisories" not in str(e):
                    raise
                log.info("planner does not take advisories (%s); planning without them", e)
                plan = self.planner(profile, self.cfg, self.kb)
            self.ws.journal({"event": "advisories_planned",
                             "findings": [{"id": f.advisory_id, "policy": f.policy} for f in findings]})
            return plan
        return self.planner(profile, self.cfg, self.kb)

    def _capture_effective(self, report: RunReport) -> None:
        """접수: 아무것도 바꾸기 전의 as-is effective dependencyManagement 를 캡처한다(나중에 인수인계
        섹션의 "바뀐 버전"과 버전 이어받기에 쓰인다).

        Intake: capture the as-is effective dependencyManagement (before any change) for later version carry-over."""
        fn = self._effective_capture
        if fn is None:
            if not self._real_maven:
                self.ws.journal({"event": "effective_depmgmt", "skipped": "custom build runner"})
                return
            try:
                fn = lazy_attr("sbmig.build.effective", "capture_effective_depmgmt", "G1 (build)")
            except MissingComponentError as e:
                self.ws.journal({"event": "effective_depmgmt", "skipped": str(e)})
                return
        try:
            path = fn(self.ws, self.cfg)
        except Exception as e:
            log.warning("effective dependencyManagement capture failed: %s", e)
            self.ws.journal({"event": "effective_depmgmt", "ok": False, "error": f"{type(e).__name__}: {e}"})
            return
        self.ws.journal({"event": "effective_depmgmt", "ok": path is not None, "path": str(path) if path else None})
        if path is not None:
            report.outputs["effective_depmgmt"] = str(path)

    # ------------------------------------------------------------------ preflight
    def _preflight(self, report: RunReport) -> None:
        """사전 점검: Maven 이 도는지(JAVA_HOME 문제와 Maven 없음을 구분), Maven 의 Java 가
        ``build.require_java_major``(21) 인지, 사내 parent·역할 parent 설정이 채워졌는지. 결과는 저널
        ``preflight`` 한 줄과 후속 조치."""
        checks: dict[str, Any] = {}
        if self._build_runner is None:
            try:
                avail = lazy_attr("sbmig.build", "maven_available", "C (build)")
                try:
                    res = avail(self.cfg)
                except TypeError:
                    res = avail()
                # maven_available returns (ok, detail); test fakes return a bare bool — a tuple is always truthy,
                # so `bool(res)` alone never flagged a missing Maven (found while writing the handbook, 2026-10-21)
                ok, detail = (bool(res[0]), str(res[1] or "")) if isinstance(res, tuple) else (bool(res), "")
                checks["mvn"] = ok
                if detail:
                    checks["mvn_detail"] = detail
                if not ok:
                    report.flags.append("mvn-not-found")
                    if _JAVA_HOME_PROBLEM_RE.search(detail):     # Maven is there; its Java is not (round 10)
                        self._mvn_java_problem = " ".join(detail.split())[:200]
                        report.manual_followups.append(
                            f"'{self.cfg.build.mvn}' cannot start: {self._mvn_java_problem} — set build.java_home "
                            f"(or JAVA_HOME) to a JDK {self._java_major_wanted()} home ({self._java_home_text()}); "
                            "every build below would fail")
                    else:
                        report.manual_followups.append(f"'{self.cfg.build.mvn}' is not runnable"
                                                       + (f" ({detail})" if detail else "")
                                                       + ": install Maven or set build.mvn; every build below will "
                                                         "fail")
                else:
                    self._check_java_major(report, detail, checks)
            except MissingComponentError as e:
                checks["mvn"] = f"unknown ({e})"
        parent = self.cfg.artifacts.parent
        if not parent.group_id or not parent.artifact_id or "CHANGE" in parent.group_id.upper():
            checks["artifacts.parent"] = "not configured"
            report.manual_followups.append("artifacts.parent is not configured: pass the company artifacts yml "
                                           "with --config")
        else:
            checks["artifacts.parent"] = f"{parent.key}:{parent.version or '-'}"
        for name in ("boot_parent", "comp_parent"):               # round 9b: the module role parents
            spec = getattr(self.cfg.artifacts, name, None)
            if spec is not None:
                checks[f"artifacts.{name}"] = f"{spec.key}:{spec.version or '-'}"
        checks["java_home"] = self.cfg.build.java_home or "(inherited)"
        self.ws.journal({"event": "preflight", **checks})
        if self._build_runner is None:
            self._check_http_blocker()

    def _check_http_blocker(self) -> None:
        """v2.1z-ah 사전 점검: Maven 이 쓸 settings(사용자 + 전역)에서 차단 미러가 ``http://`` 저장소를 덮고 있으면 WARNING 한 줄과
        저널 ``maven_http_blocker_active`` (저장소들). 중단하지 않는다 — 그 저장소에서 받을 것이 없을 수도 있다; 중단은 Maven 이 실제로
        거부했을 때만(``BuildFixLoop.http_blocked_abort``).

        Preflight: one WARNING + journal `maven_http_blocker_active` when the effective Maven settings route an `http://`
        repository to a blocked mirror. Never aborts — the abort happens only when Maven really reports the block."""
        try:
            from sbmig.build.env_errors import active_http_blockers
            found = active_http_blockers(self.cfg, self.ws.root)
        except Exception as e:  # noqa: BLE001 - a preflight check never fails a run
            log.debug("http blocker preflight: %s", e)
            return
        if not found:
            return
        repos = ", ".join(f"{b.repo_id} ({b.url})" for b in found)
        mirrors = ", ".join(dict.fromkeys(f"{b.mirror_id} from {b.mirror_origin}" for b in found))
        log.warning("preflight: Maven's HTTP blocker covers %s (mirror %s) — Maven >= 3.8.1 will refuse to download from it; "
                    "if the build needs it, the run stops: use an https URL or override the mirror in the settings.xml",
                    repos, mirrors)
        self.ws.journal({"event": "maven_http_blocker_active",
                         "repositories": [{"id": b.repo_id, "url": b.url, "declared": b.declared, "mirror": b.mirror_id,
                                           "mirror_origin": b.mirror_origin} for b in found]})

    _mvn_java_problem = ""
    _jdk_problem = ""

    def _java_major_wanted(self) -> int:
        return int(getattr(self.cfg.build, "require_java_major", 0) or 0) or 21

    def _java_home_text(self) -> str:
        import os
        home = self.cfg.build.java_home or os.environ.get("JAVA_HOME") or ""
        return (f"build.java_home={home}" if self.cfg.build.java_home else
                f"JAVA_HOME={home}" if home else "JAVA_HOME not set: mvn uses the java on PATH")

    def _check_java_major(self, report: RunReport, detail: str, checks: dict[str, Any]) -> None:
        """``mvn -v`` 의 Java 가 ``build.require_java_major``(21)여야 한다. 회사 실행에서 JAVA_HOME 의 JDK
        17 때문에 모든 빌드가 수정 루프로는 못 고치는 이유로 실패했다(round 10) → ``jdk-not-21`` 로 바로 중단.
        버전을 읽을 수 없으면 검사 안 함.

        Round 10 (A10): `mvn -v` must report `build.require_java_major` (21) — a JDK 17 on JAVA_HOME made every
        build of a company run fail for a reason no fix loop can change. Unparseable / absent version → no check."""
        want = int(getattr(self.cfg.build, "require_java_major", 0) or 0)
        m = _JAVA_VERSION_RE.search(detail or "")
        if want <= 0 or m is None:
            return
        ver = m.group("v").strip(".,")
        parts = re.split(r"[._+-]", ver)
        try:
            major = int(parts[1]) if parts[0] == "1" and len(parts) > 1 else int(parts[0])
        except ValueError:
            return
        checks["java_major"] = major
        if major == want:
            return
        rt = _JAVA_RUNTIME_RE.search(detail or "")
        where = f"runtime {rt.group('home').strip()}" if rt else self._java_home_text()
        # one line, the abort's follow-up (`run aborted: Maven runs on Java 17.0.9 (…), not Java 21: …`)
        self._jdk_problem = (f"Maven runs on Java {ver} ({where}), not Java {want}: set build.java_home (or JAVA_HOME) "
                             f"to a JDK {want} home and rerun")
        report.flags.append("jdk-not-21")
        log.error("preflight: %s", self._jdk_problem)

    # ------------------------------------------------------------------ phases
    def _run_phase(self, pr: PhaseResult, items: list[tuple[int, PlanStep]]) -> None:
        """한 묶음의 단계를 차례로(각자 로그 단계 태그 안에서). 단계가 묶음을 중단시키면 나머지는 건너뛴다.
        검증 단계의 성공 여부는 최종 빌드 결과들이 전부 성공했는가로만 정한다."""
        aborted = False
        loop_ran = False
        for idx, step in items:
            if aborted:
                pr.notes.append(f"step {step.id} skipped: phase aborted")
                continue
            self.ws.journal({"event": "step_start", "step": step.id, "kind": step.kind.value,
                             "phase": step.phase.value, "target": step.target})
            with stage(step_stage(step)):
                ok, aborted = self._run_step(pr, step, aborted)
            self.ws.journal({"event": "step_end", "step": step.id, "ok": bool(ok)})
            if self.loop is not None and self.loop.aborted:
                pr.ok = False
                break
            loop_ran = loop_ran or step.kind == StepKind.BUILD_FIX_LOOP
        if pr.phase == Phase.VERIFY:
            pr.ok = pr.build_ok = bool(self._verify_results) and all(r.ok for r in self._verify_results)
        elif not loop_ran and pr.ok:
            pr.build_ok = False

    def _run_step(self, pr: PhaseResult, step: PlanStep, aborted: bool) -> tuple[Any, bool]:
        """계획 단계 하나를 종류별로 처리 → (성공, 묶음 중단). 레시피 id 없음·재구성 실패는 묶음을
        중단하지만(선택 버전 올리기는 예외), 최종 빌드는 그래도 돈다.

        One plan step (inside its log stage). Returns (ok, the phase is aborted)."""
        ok: Any = True
        if step.kind == StepKind.OPENREWRITE:
            ok = self._step_rewrite(pr, step)
            if ok is None:          # missing recipe → programming error
                aborted, ok = True, False
                pr.ok = False
        elif step.kind == StepKind.REVIEW:
            ok = self._step_review(pr, step)
        elif step.kind == StepKind.BUILD_FIX_LOOP:
            # an intermediate fix loop (the v2 planner plans none; a hand-made plan may)
            ok = self._step_loop(pr, step)
            pr.ok = ok
        elif step.kind in (StepKind.RESTRUCTURE, StepKind.MSA_POM):
            ok = self._step_restructure(pr, step)
            if not ok and self._step_mode(step) in NON_FATAL_RESTRUCTURE_MODES:
                pr.notes.append(f"{step.id}: failed — continuing with the rest of the {step.phase.value} phase")
            elif not ok:
                aborted = True
                pr.ok = False
        elif step.kind == StepKind.BUILD and step.phase != Phase.VERIFY:
            ok = self._step_compile(pr, step)
        elif step.kind == StepKind.BUILD:
            ok = self._step_verify(pr, step)
        return ok, aborted

    def _flush_notes(self, pr: PhaseResult) -> None:
        if self._pending_notes:
            pr.notes.extend(self._pending_notes)
            self._pending_notes = []

    def _step_rewrite(self, pr: PhaseResult, step: PlanStep) -> Optional[bool]:
        """변환: 계획된 레시피 전부를 OpenRewrite 한 번에(빌드 없음) 돌리고 ``rewrite: <step>`` 로 커밋.
        레시피가 Maven 메타데이터를 못 받아 ``4.0.x`` 같은 글자 그대로의 버전을 남기면 되돌리고 한 번
        재시도한다(X8). 실패해도 계속 간다 — 진짜 문제는 빌드가 보여 준다. None = 레시피 id 를 찾지
        못함(설정 오류 → 묶음 중단)."""
        self._flush_notes(pr)
        if self.budget.time_exceeded():
            pr.notes.append(f"{step.id} skipped: run time budget exhausted")
            self._flag("time-budget-exhausted")
            return True
        if not step.recipes:
            return True
        runner = self.rewrite_runner
        pre = self.ws.head()
        clean_before = self._dry_run or not self.ws.is_dirty()
        res = self._run_rewrite_once(runner, step)
        # catalog X8: the recipe's own resolver could not download Maven metadata → literal `4.0.x` versions
        unresolved = self._unresolved_versions(res)
        if unresolved and self.cfg.openrewrite.download_retry > 0 and clean_before:
            wait_s = max(0, int(self.cfg.openrewrite.download_retry_wait_s))
            self.ws.revert_to(pre)                 # drop the half-resolved result; tree is clean again
            self.ws.journal({"event": "rewrite_unresolved_versions", "step": step.id, "poms": unresolved,
                             "retry": True, "wait_s": wait_s, "log": res.log_path})
            log.warning("%s: literal versions left after the rewrite (%s) → retrying once in %ss", step.id,
                        self._fmt_unresolved(unresolved), wait_s)
            if wait_s:
                getattr(self, "_sleep", time.sleep)(wait_s)
            res = self._run_rewrite_once(runner, step)
            unresolved = self._unresolved_versions(res)
        if unresolved:
            self._report_unresolved_versions(pr, step, res, unresolved)
        # v2.1z-ah: Maven 의 HTTP 차단 미러가 저장소를 거부했다 → 부모 맞추기도 재시도도 소용없다(아래에서 실행을 멈춘다)
        http_blocked = not res.ok and self._http_blocked_text(res)
        # v2.1z-ag: 사내 부모 g:a 의 as-is 버전이 저장소에 없어 실패 → 설정 버전으로 맞추고 한 번 더(LLM 없음)
        if not res.ok and not self._dry_run and not http_blocked:
            res = self._align_asis_parent_and_retry(pr, step, res, pre, clean_before)
        pr.rewrite_results.append(res)
        sha = None
        if not self._dry_run and self.ws.is_dirty():
            sha = self.ws.commit(f"rewrite: {step.id}")
        self.ws.journal({"event": "rewrite", "step": step.id, "ok": res.ok, "recipes": res.recipes,
                         "changed_files": len(res.changed_files), "commit": sha if sha != pre else None,
                         "error": res.error, "log": res.log_path, "dry_run": self._dry_run})
        if getattr(res, "parse_problems", None):
            self._report_parse_problems(pr, step, res)
        if not res.ok and http_blocked and self.loop is not None:
            self.loop.http_blocked_abort(pr, step.phase, res, 0, where=step.id)
            return True
        if not res.ok:
            err = res.error or "OpenRewrite run failed"
            self._check_parent_in_rewrite(pr, step, err)
            if _MISSING_RECIPE_RE.search(err):
                pr.notes.append(f"{step.id}: recipe id not found — {err} (programming/config error, phase aborted)")
                return None
            pr.notes.append(f"{step.id}: OpenRewrite failed ({err}); continuing — the build will show the real "
                            "problem")
            pr.remaining_errors.append(BuildError(kind=ErrorKind.REWRITE, message=err[:500], raw=err,
                                                  module=step.target))
        return True

    # catalog X8: literal versions the rewrite leaves when its resolver could not read maven-metadata.xml
    UNRESOLVED_VERSIONS_FLAG = "rewrite-unresolved-versions"
    _POM_VALUE_RE = re.compile(r"<([\w.\-]+)>([^<>]*)</\1>")
    _UNRESOLVED_VERSION_RE = re.compile(r"^(?:\d+(?:\.\d+)*\.x|latest\.(?:release|integration|patch))$")

    @staticmethod
    def _fmt_unresolved(unresolved: dict[str, list[str]]) -> str:
        return "; ".join(f"{p}: {', '.join(vs)}" for p, vs in unresolved.items())

    def _run_rewrite_once(self, runner: Any, step: PlanStep) -> Any:
        # in-repo reactor siblings (shared library roots) only resolve when the plugin runs from the aggregator root
        from .fixloop import build_target_args
        module, reactor_args = build_target_args(self.profile, step.target)
        kwargs = dict(dry_run=self._dry_run, label=step.id)
        try:
            return runner.run(list(step.recipes), module=module, extra_args=reactor_args, **kwargs)
        except TypeError:                      # a runner without `extra_args` (fakes / older implementations)
            return runner.run(list(step.recipes), module=step.target, **kwargs)

    def _unresolved_versions(self, res: Any) -> dict[str, list[str]]:
        """성공한 rewrite 가 바뀐 pom 에 남긴 해석 안 된 글자
        그대로의 버전(``4.0.x``, ``latest.release`` …), pom 경로별.

        pom path → literal unresolved versions (`4.0.x`, `latest.release`, …) a successful, non-dry rewrite
        left in the changed poms (every pom when the runner reports no changed files)."""
        if self._dry_run or not getattr(res, "ok", False):
            return {}
        changed = list(getattr(res, "changed_files", None) or [])
        poms = [p for p in changed if p == "pom.xml" or p.endswith("/pom.xml")] if changed else self.ws.pom_files()
        out: dict[str, list[str]] = {}
        for p in sorted(dict.fromkeys(poms)):
            try:
                text = self.ws.read(p)
            except (OSError, UnicodeDecodeError):
                continue
            found = list(dict.fromkeys(m.group(2).strip() for m in self._POM_VALUE_RE.finditer(text)
                                       if self._UNRESOLVED_VERSION_RE.match(m.group(2).strip())))
            if found:
                out[p] = found
        return out

    def _report_unresolved_versions(self, pr: PhaseResult, step: PlanStep, res: Any,
                                    unresolved: dict[str, list[str]]) -> None:
        """결과는 그대로 두되(무엇이 깨지는지는 빌드가 보여 준다) 이유를 남긴다: 플래그, 단계 메모, 후속 조치.

        Keep the result (the build loop shows what breaks), but say why: flag, phase note, follow-up."""
        self.ws.journal({"event": "rewrite_unresolved_versions", "step": step.id, "poms": unresolved,
                         "retry": False, "log": getattr(res, "log_path", None)})
        self._flag(self.UNRESOLVED_VERSIONS_FLAG)
        what = self._fmt_unresolved(unresolved)
        pr.notes.append(f"{step.id}: {self.UNRESOLVED_VERSIONS_FLAG} — the recipe run left literal versions "
                        f"({what}); the build will fail on dependency resolution")
        versions = ", ".join(f"`{v}`" for v in dict.fromkeys(v for vs in unresolved.values() for v in vs))
        fu = (f"the recipe run could not download Maven metadata (proxy / rate limit): literal versions {versions} "
              f"remain in {', '.join(unresolved)} — re-run sbmig later or set the versions by hand")
        if self.report is not None and fu not in self.report.manual_followups:
            self.report.manual_followups.append(fu)

    # 2026-10-22 (company run): files the rewrite plugin could not parse never see the type-attributed recipes
    # (Spring Data, Hibernate, method renames) — the fix scripts cover what the build finds; say so in the report
    PARSE_PROBLEMS_FLAG = "rewrite-parse-problems"

    def _report_parse_problems(self, pr: PhaseResult, step: PlanStep, res: Any) -> None:
        """OpenRewrite 가 파싱하지 못한 소스 파일: 플래그 + 단계
        메모 + WARNING (빌드 오류는 수정 스크립트가 맡는다)."""
        files = [str(f) for f in (getattr(res, "parse_problems", None) or [])]
        self.ws.journal({"event": "rewrite_parse_problems", "step": step.id, "files": files,
                         "log": getattr(res, "log_path", None)})
        self._flag(self.PARSE_PROBLEMS_FLAG)
        shown = ", ".join(files[:5]) + (f", … (+{len(files) - 5})" if len(files) > 5 else "")
        pr.notes.append(f"{step.id}: OpenRewrite could not parse {len(files)} source file(s) ({shown}) — recipes "
                        "that need type information skipped them; the fix scripts cover what the build finds")
        log.warning("%s: rewrite parse problems in %d file(s): %s", step.id, len(files), shown)

    @staticmethod
    def _http_blocked_text(res: Any) -> bool:
        """v2.1z-ah: 실패한 OpenRewrite 실행(오류 문장 + 로그)에 Maven 의 HTTP 차단 미러가 보이는가."""
        from sbmig.build.env_errors import http_blocker_hit
        return http_blocker_hit(BuildFixLoop.build_text(res)) is not None

    def _check_parent_in_rewrite(self, pr: PhaseResult, step: PlanStep, err: str) -> None:
        """사내 parent 가 해석되지 않아 OpenRewrite 가 실패했다 → 수정 루프와 똑같이 실행을 중단한다.

        OpenRewrite failed because the configured in-house parent does not resolve → abort like the build loop."""
        keys = self.cfg.artifacts.parent_keys()                # root parent + boot_parent / comp_parent (round 9b)
        if self.loop is None:
            return
        # 2026-11-05: 사내 부모가 선언한 빌드 확장을 Maven 이 못 받아 OpenRewrite 가 모델 단계에서 죽었다 → 같은 중단(LLM 없음)
        m_ext = _BUILD_EXT_IN_TEXT_RE.search(err)
        if m_ext:
            chain = self.loop._chain_extensions()          # same package
            key = f"{m_ext.group('g')}:{m_ext.group('a')}"
            coords = f"{key}:{m_ext.group('v')}"
            if chain.get(key) == coords:                   # the full g:a:v — a project-owned version is the loop's (LLM) case
                gav = GAV.parse(coords)
                e = BuildError(kind=ErrorKind.POM_INVALID, message=err.splitlines()[0][:500], raw=err[:2000], artifact=gav,
                               module=step.target)
                br = BuildResult(ok=False, goal="rewrite", command=[], exit_code=1, duration_s=0.0, log_path="", errors=[e])
                self.loop._parent_abort(pr, step.phase, br, 0)   # same package
                self._merge_blocked(pr, [it for it in self.blocked.items() if it.reason.value == "parent_not_found"])
            # an extension failure is never a parent-resolution failure: Maven's location tail (`@ <parent gav>, <pom>`) names the
            # parent that DECLARES it, which the parent regex below would mistake for an unresolvable parent
            return
        if not _PARENT_UNRESOLVABLE_RE.search(err):
            return
        from sbmig.restructure.asis_parent import classify_parent_misses
        misses = classify_parent_misses(err, self.cfg, self.ws)
        if not misses and any(k in err for k in keys):
            # Maven 의 문장에서 좌표를 못 읽었는데 사내 부모 g:a 가 보인다 → 예전처럼(설정 버전을 못 찾은 것으로) 판정
            from sbmig.restructure.asis_parent import classify_gav
            key = next(k for k in keys if k in err)
            misses = [classify_gav(GAV.parse(key), self.cfg, self.ws)]
        policy = getattr(self.cfg.openrewrite, "unresolvable_asis_parent", "align")
        abort = [m for m in misses if m.kind == "configured" or (m.kind == "asis-version" and policy == "abort")]
        if abort:
            m = abort[0]
            e = BuildError(kind=ErrorKind.PARENT_RESOLUTION, message=err.splitlines()[0][:500], raw=err[:2000],
                           artifact=m.gav, signature=f"parent_resolution|{m.coordinates}", module=step.target)
            br = BuildResult(ok=False, goal="rewrite", command=[], exit_code=1, duration_s=0.0, log_path="", errors=[e])
            self.loop._parent_abort(pr, step.phase, br, 0)   # noqa: SLF001 - same package
            self._merge_blocked(pr, [it for it in self.blocked.items() if it.reason.value == "parent_not_found"])
            return
        # 중단하지 않는 경우(as-is 버전 — continue 이거나 맞춘 뒤에도 남음 / 사내 부모가 아닌 부모): 레시피가 돌지 않았다는 사실을 남긴다
        for m in misses:
            self._report_rewrite_skipped_parent(pr, step, m)

    REWRITE_SKIPPED_PARENT_FLAG = "rewrite-skipped-asis-parent"
    ASIS_PARENT_ALIGNED_FLAG = "asis-parent-aligned"

    def _report_rewrite_skipped_parent(self, pr: PhaseResult, step: PlanStep, miss: Any) -> None:
        """v2.1z-ag (경우 c 와 continue): as-is 부모가 저장소에 없어 레시피가 돌지 않았다 — 플래그
        ``rewrite-skipped-asis-parent``, 저널 ``rewrite_skipped_asis_parent``, 단계 메모, 인수인계 한 줄. 중단하지 않는다.

        The recipes did not run because the as-is parent is not in the repository: flag, journal, note, one Handover line."""
        company = miss.kind == "asis-version"
        self.ws.journal({"event": "rewrite_skipped_asis_parent", "step": step.id, "parent": miss.coordinates,
                         "company_parent": company,
                         "policy": getattr(self.cfg.openrewrite, "unresolvable_asis_parent", "align")})
        self._flag(self.REWRITE_SKIPPED_PARENT_FLAG)
        what = (f"the as-is version of the company parent ({miss.spec_name} = {miss.target.coordinates})"
                if company and miss.target is not None else "a parent that is not a company parent")
        pr.notes.append(f"{step.id}: {self.REWRITE_SKIPPED_PARENT_FLAG} — the recipes did not run because the as-is parent "
                        f"`{miss.coordinates}` is not in the repository ({what}); the restructure replaces the parent and "
                        "the fix loop covers what the recipes would have changed")
        log.warning("%s: the recipes did not run — the as-is parent %s is not in the repository (%s)", step.id,
                    miss.coordinates, what)
        from sbmig.restructure.asis_parent import skipped_followup
        fu = skipped_followup(miss)
        if self.report is not None and fu not in self.report.manual_followups:
            self.report.manual_followups.append(fu)

    def _align_asis_parent_and_retry(self, pr: PhaseResult, step: PlanStep, res: Any, pre: Optional[str],
                                     clean_before: bool) -> Any:
        """v2.1z-ag 경우 b: OpenRewrite 가 사내 부모 g:a 의 as-is 버전(설정 버전이 아님)을 저장소에서 못 받아 실패했다 →
        (``openrewrite.unresolvable_asis_parent: align`` 일 때) 그 g:a·버전을 부모로 둔 pom 전부에 설정 버전을 적고 커밋
        (``transform: as-is parent <g:a> <old> → <configured> (as-is version not in the repository)``), 저널
        ``asis_parent_aligned``, 플래그 ``asis-parent-aligned``, 단계 메모와 인수인계 한 줄, 그리고 OpenRewrite 를 한 번 더.
        설정 버전 자체를 못 찾은 경우가 섞여 있거나 바꿀 pom 이 없으면 그대로 돌려준다(뒤의 판정이 맡음). 다시 돈 결과를 돌려준다.

        Case b: align every pom whose parent is that g:a at that version to the configured version, commit, journal, flag,
        note + one Handover line, run OpenRewrite once more and return that result. Returns `res` unchanged when the
        switch is not `align`, a configured parent is missing too, or no pom could be changed."""
        policy = getattr(self.cfg.openrewrite, "unresolvable_asis_parent", "align")
        if policy != "align":
            return res
        err = res.error or ""
        if not _PARENT_UNRESOLVABLE_RE.search(err):
            return res
        from sbmig.restructure.asis_parent import align_asis_parent, classify_parent_misses
        misses = classify_parent_misses(err, self.cfg, self.ws)
        asis = [m for m in misses if m.kind == "asis-version"]
        if not asis or any(m.kind == "configured" for m in misses):
            return res
        if clean_before and self.ws.is_dirty():
            self.ws.revert_to(pre)                     # a failed run normally leaves nothing; never commit half of one
        aligned = []
        for m in asis:
            r = align_asis_parent(self.ws, m)
            if not r.poms:
                continue
            old, new = m.gav.version, m.target.version
            sha = self.ws.commit(f"transform: as-is parent {m.gav.key} {old} → {new} (as-is version not in the repository)")
            self.ws.journal({"event": "asis_parent_aligned", "step": step.id, "parent": m.gav.key, "from": old, "to": new,
                             "target": m.target.coordinates, "spec": m.spec_name, "poms": list(r.poms),
                             "properties": dict(r.properties), "commit": sha, "where": "rewrite"})
            log.warning("%s: as-is parent %s:%s is not in the repository → set to the configured %s (%s) in %s; "
                        "OpenRewrite runs once more on the target parent's classpath", step.id, m.gav.key, old,
                        m.target.coordinates, m.spec_name, ", ".join(r.poms))
            aligned.append((m, r))
        if not aligned:
            return res
        self._flag(self.ASIS_PARENT_ALIGNED_FLAG)
        for m, r in aligned:
            how = "".join(f" (property `{p}` in {pom})" for pom, p in r.properties.items())
            pr.notes.append(f"{step.id}: {self.ASIS_PARENT_ALIGNED_FLAG} — the as-is parent `{m.coordinates}` is not in the "
                            f"repository; set to the configured `{m.target.coordinates}` ({m.spec_name}) in "
                            f"{', '.join(r.poms)}{how}; OpenRewrite ran once more")
            from sbmig.restructure.asis_parent import aligned_followup
            fu = aligned_followup(m, r.poms)
            if self.report is not None and fu not in self.report.manual_followups:
                self.report.manual_followups.append(fu)
        return self._run_rewrite_once(self.rewrite_runner, step)

    def _merge_blocked(self, pr: PhaseResult, items: list[BlockedItem]) -> None:
        for it in items:
            if all(b.id != it.id for b in pr.blocked):
                pr.blocked.append(it)

    def _step_compile(self, pr: PhaseResult, step: PlanStep) -> bool:
        """v2.1 변환 후 컴파일(``transform.compile``): OpenRewrite 결과에 ``mvn clean compile`` 한 번 —
        고치지 않는다. 나온 오류는 전부 오류 목록에(seen ``compile``) 들어가 뒤의 재구성·검토·빌드가 그것에서
        출발한다. 실패한 컴파일이 정상 입력이므로 단계를 실패시키지 않는다(빌드 게이트가 아니다).

        v2.1 `transform.compile` (StepKind.BUILD outside the verify phase): `mvn clean compile` once on
        OpenRewrite's result — no fixing; every error goes on the error list (seen `compile`) and the restructure /
        review / build that follow work from it. Never fails the phase (a failed compile is the expected input)."""
        self._flush_notes(pr)
        if self._dry_run:
            pr.notes.append(f"{step.id}: dry-run → compile skipped")
            return True
        if self.budget.time_exceeded():
            pr.notes.append(f"{step.id} skipped: run time budget exhausted")
            self._flag("time-budget-exhausted")
            return True
        from .fixloop import build_target_args
        module, extra = build_target_args(self.profile, step.target)
        kw: dict[str, Any] = {"extra_args": extra} if extra else {}
        goal = step.build_goal or COMPILE_GOAL
        label = "compile" + (f"-{step.params['service']}" if step.params.get("service") else "")
        t = self.clock()
        before = _status_paths(self.ws)
        try:
            res = refine_build(self.build_runner.run(goal, module=module, label=label,
                                                     skip_test_compile=bool(self.cfg.build.skip_test_compile), **kw))
            _restore_build_byproducts(self.ws, before)
        except Exception as e:  # noqa: BLE001 - a recording step: the verify build shows the real state
            log.warning("mvn %s failed to run: %s: %s — the error list starts at the first build", goal,
                        type(e).__name__, e)
            pr.notes.append(f"{step.id}: mvn {goal} could not run ({type(e).__name__}: {e})")
            return True
        finally:
            self._timed("compile", t)
        added = self.errors.add_build(res, "compile", void=bool(res.timed_out), scope=step.target)
        pr.iterations += 1
        pr.iteration_records.append(IterationRecord(phase=step.phase, iteration=pr.iterations, build_ok=res.ok,
                                                    error_count=res.error_count,
                                                    signatures=res.signature_multiset(), build_log=res.log_path))
        if res.ok:
            line = f"mvn {goal}: BUILD SUCCESS → 목록 0건"
        else:
            line = (f"mvn {goal}: {res.error_count} error(s) → 목록에 {added}건 (seen=compile)"
                    + (" — timed out" if res.timed_out else ""))
        log.info("%s", line)
        pr.notes.append(f"{step.id}: {line}")
        self.ws.journal({"event": "compile", "step": step.id, "goal": goal, "target": step.target, "ok": res.ok,
                         "errors": res.error_count, "added": added, "log": res.log_path})
        return True

    def _step_loop(self, pr: PhaseResult, step: PlanStep) -> bool:
        """중간 수정 루프 단계(``StepKind.BUILD_FIX_LOOP``). v2 계획기는 만들지 않는다 — 손으로 만든
        계획에만 있다. 수정 루프는 보통 최종 빌드 실패 뒤 ``_verify_fix_loop`` 에서 돈다."""
        self._flush_notes(pr)
        assert self.loop is not None
        if self.budget.time_exceeded():
            pr.notes.append(f"{step.id} skipped: run time budget exhausted ({self.cfg.loop.max_total_minutes} min)")
            self._flag("time-budget-exhausted")
            return False
        pc = self.cfg.loop.phases.get(step.phase.value)
        goal = step.build_goal or (pc.goal if pc else "compile")
        max_it = step.max_iterations or (pc.max_iterations if pc else 5)
        res = self.loop.run(step.phase, goal, max_it, target=step.target)
        pr.iterations += res.iterations
        pr.attempts += res.attempts
        pr.iteration_records += res.iteration_records
        pr.rewrite_results += res.rewrite_results
        pr.remaining_errors = [e for e in pr.remaining_errors if e.kind == ErrorKind.REWRITE and not res.build_ok] \
            + res.remaining_errors
        pr.build_ok = res.build_ok
        pr.notes += [f"{step.id}: {n}" for n in res.notes]
        self._merge_blocked(pr, res.blocked)
        return res.ok

    def _handoff_restructure(self) -> None:
        """v2.1v (B-H2): 이 실행의 재구성 결과와 모드를 수정 루프에 넘긴다 — 루프가 LLM 요청마다 "Project after the
        restructure" 절(``FixContext.layout_facts``)을 만든다. 그래프 엔진도 같은 단계 함수를 부르므로 같은 자리에서.

        Hand the run's RestructureResults and their modes to the fix loop (`BuildFixLoop.restructure_results` /
        `restructure_modes`) — the source of `FixContext.layout_facts`; the graph engine runs the same step method."""
        if self.loop is not None:
            self.loop.restructure_results = list(self._restructure_results)
            self.loop.restructure_modes = list(self._restructure_modes)

    def _step_restructure(self, pr: PhaseResult, step: PlanStep) -> bool:
        """재구성(회사 레이아웃: base-parent, ``{name}-boot`` / ``-back`` / ``-front``, 필수 2개, 쓰는 선택
        8개 올리기)을 돌리고 ``restructure: <step>`` 로 커밋. 옮긴 파일은 차단 등록부가 따라가게 하고, pom
        교정마다 ``pom-verify`` 항목을 만든다. 실패하면 ``restructure-failed`` — 최종 빌드가 통과해도
        마이그레이션 완료가 아니다(success=False)."""
        self._flush_notes(pr)
        assert self.profile is not None and self.report is not None
        mode = self._step_mode(step)
        if self._dry_run:
            pr.notes.append(f"{step.id}: dry-run → restructure ({mode}) not applied")
            return True
        pre = self.ws.head()
        try:
            res = self._call_restructurer(mode, dict(step.params))
        except MissingComponentError:
            raise
        except Exception as e:
            log.exception("restructure failed")
            pr.notes.append(f"{step.id}: restructure crashed: {type(e).__name__}: {e}")
            if self.ws.is_dirty():
                self.ws.discard_uncommitted()
            self._restructure_failed(step, mode, f"{type(e).__name__}: {e}")
            return False
        sha = self.ws.commit(f"restructure: {step.id}") if self.ws.is_dirty() else self.ws.head()
        if sha != pre:
            self._follow_moves(step, pre, sha)
        self._pom_verify_entries(res)
        self.report.manual_followups += [f for f in res.manual_followups if f not in self.report.manual_followups]
        for flag in list(getattr(res, "flags", None) or []):      # 2026-10-23: e.g. web-xml-registered
            self._flag(flag)
        pr.notes += res.notes
        self._jsp_jakarta_note(pr, step, res)
        self._restructure_results.append(res)            # v2: the structure review's input
        self._restructure_modes.append(mode)
        self._restructure_notes += [f"{step.id}: {n}" for n in res.notes]
        merged = list(getattr(res, "merged_modules", None) or [])
        kept_boot = getattr(res, "kept_boot_module", None)
        conflicts = list(getattr(res, "merge_conflicts", None) or [])
        seen = {(c.path, c.kept_from) for c in self.report.merge_conflicts}
        for c in conflicts:
            if (c.path, c.kept_from) not in seen:
                seen.add((c.path, c.kept_from))
                self.report.merge_conflicts.append(c)
        # 모듈 병합에서 먼저 온 것이 이긴(first-wins) 충돌: 버려진 사본을 LLM 에 읽기 전용 참고로 보여 준다
        if self.loop is not None:          # round 9: the restructure-phase fix loop shows dropped copies to the LLM
            self.loop.merge_conflicts = list(self.report.merge_conflicts)
        self._handoff_restructure()        # v2.1v: the fix prompt's "Project after the restructure"
        ph_files = list(getattr(res, "placeholder_files", None) or [])
        ph_keys = list(dict.fromkeys(getattr(res, "placeholders_written", None) or []))
        self._merge_placeholders(ph_files, ph_keys)
        if ph_files or ph_keys:
            self.ws.journal({"event": "placeholders", "step": step.id, "files": ph_files, "keys": len(ph_keys)})
        summary = (f"{step.id} ({mode}): {len(res.changed_files)} file(s) changed, {res.moved_files} moved, "
                   f"{len(res.generated_files)} generated; mandatory added: {', '.join(res.mandatory_added) or '-'}; "
                   f"optional bumped: {', '.join(res.optional_bumped) or '-'}; placeholders: {len(ph_keys)}")
        pr.notes.append(summary)
        self._restructure_notes.append(summary)
        if merged or kept_boot or conflicts:
            differing = sum(1 for c in conflicts if not c.identical)
            pr.notes.append(f"{step.id}: merged modules: {', '.join(merged) or '-'}; kept boot module: "
                            f"{kept_boot or '-'}; merge conflicts: {len(conflicts)} ({differing} differing)")
        self.ws.journal({"event": "restructure", "step": step.id, "mode": mode, "ok": res.ok,
                         "commit": sha if sha != pre else None, "changed_files": len(res.changed_files),
                         "moved": res.moved_files, "generated": res.generated_files,
                         "mandatory_added": res.mandatory_added, "optional_bumped": res.optional_bumped,
                         "optional_added": list(getattr(res, "optional_added", None) or []),
                         "optional_replaced": list(getattr(res, "optional_replaced", None) or []),
                         "bom_properties_removed": list(getattr(res, "bom_properties_removed", None) or []),
                         "version_properties_removed": list(getattr(res, "version_properties_removed", None) or []),
                         "recipe_versions_dropped": list(getattr(res, "recipe_versions_dropped", None) or []),
                         "bom_imports": list(getattr(res, "bom_imports", None) or []),
                         "parent_manages_spring_boot_effective": getattr(res, "parent_manages_spring_boot_effective",
                                                                         None),
                         "merged_modules": merged, "kept_boot_module": kept_boot,
                         "merge_conflicts": len(conflicts), "error": res.error})
        self._journal_restructure_jars(res)
        if not res.ok:
            pr.notes.append(f"{step.id}: restructure failed: {res.error or 'unknown error'} → skipping the rest of "
                            "the restructure phase (verify still runs)")
            self._restructure_failed(step, mode, res.error or "unknown error")
        return res.ok

    def _journal_restructure_jars(self, res: Any) -> None:
        """v2.1z-j: 재구성이 연결한 동봉 jar 마다 저널 ``bundled_jar_wired`` (stage restructure), 연결하지 않은 것은
        ``bundled_jars_skipped`` 한 줄. 프로필의 같은 jar 에 note(``restructure: wired (<scope>)`` / ``restructure: <사유>``)
        를 붙여 보고서 표가 단계와 상태를 읽게 한다. 인수인계 줄은 ``res.manual_followups`` 로 이미 왔다. 예외 없음.

        v2.1z-j: journal one `bundled_jar_wired` (`stage: restructure`) per jar the restructure wired and one
        `bundled_jars_skipped` for the rest; mark the profile's jars (`restructure: …` note) for the report table."""
        try:
            wired = list(getattr(res, "jars_wired", None) or [])
            skipped = list(getattr(res, "jars_skipped", None) or [])
            for w in wired:
                self.ws.journal({"event": "bundled_jar_wired", "stage": "restructure", "jar": w.get("path"),
                                 "pom": w.get("pom"), "coordinates": w.get("coordinates"), "scope": w.get("scope"),
                                 "placeholder": bool(w.get("placeholder")), "boot_poms": list(w.get("boot_poms") or [])})
            if skipped:
                self.ws.journal({"event": "bundled_jars_skipped", "jars": skipped})
            marks = {str(w.get("path")): f"restructure: wired ({w.get('scope')})" for w in wired}
            marks.update({str(x.get("path")): f"restructure: {x.get('why')}" for x in skipped if x.get("path") not in marks})
            for j in list(getattr(self.profile, "bundled_jars", None) or []):
                mark = marks.get(j.path)
                if mark and mark not in (j.note or ""):
                    j.note = f"{j.note}; {mark}" if j.note else mark
        except Exception as e:  # noqa: BLE001 - bookkeeping must never fail the run
            log.debug("restructure bundled-jar journal skipped: %s: %s", type(e).__name__, e)

    # ------------------------------------------------------------------ v2: structure review
    def _make_reviewer(self) -> tuple[Any, str]:
        """(검토기, 못 만든 이유). 주입된 것 또는
        ``StructureReviewer(cfg, 예산이 걸린 LLM, 스크립트 실행기, kb=None)``. KB 는 일부러 None — 검토
        기록(오류 목록 + KB 케이스)은 커밋 뒤 오케스트레이터가 남긴다. 검토는 실행을 멈추지 않는다.

        (reviewer, why-not) — the injected one, or `sbmig.review.StructureReviewer(cfg, <budgeted LLM>,
        <script runner>, kb)`; (None, reason) when it cannot be built (the review never stops a run)."""
        llm, why = None, ""
        if self.budget.llm_allowed():
            try:
                llm = _BudgetedLLM(self.llm_client(), self.budget, sink=self._review_usage)
            except Exception as e:  # noqa: BLE001 - no LLM: the reviewer says "skipped"
                why = f"no LLM client: {type(e).__name__}: {e}"
                log.info("structure review: %s", why)
        else:
            why = f"LLM budget exhausted ({self.budget.llm_text()})"
        made = self._reviewer
        try:
            if made is not None and not isinstance(made, type) and callable(getattr(made, "review", None)):
                return made, why                    # an instance (a class is a factory, like StructureReviewer)
            factory = made if callable(made) else lazy_attr("sbmig.review", "StructureReviewer", "R (review)")
            # kb=None: the orchestrator records the review (error list + the KB case of its script) after the commit
            return factory(self.cfg, llm, self.script_runner, None), why
        except Exception as e:  # noqa: BLE001 - ImportError / a broken reviewer: noted, the run goes on
            return None, f"structure reviewer unavailable: {type(e).__name__}: {e}"

    def _step_review(self, pr: PhaseResult, step: PlanStep) -> bool:
        """v2 구조 검토(``restructure.review``): LLM 이 재구성된 트리의 pom·의존성·모듈 구조·인코딩을 회사
        규칙에 비춰 보고, critical 문제만 고친다(v2.1: 답변의 edits, pom 만). 바뀐 것은 여기서 ``review: fix
        critical structure issues (<n>)`` 로 커밋(되돌릴 수 있게). warning 은 보고서 "Structure review" 와
        오류 목록 메모. 실행을 멈추지 않는다: 실패 = 판정 ``failed`` + 메모, 드라이런 = LLM 호출 없이
        ``skipped``. 프로젝트당 LLM 1회(형식 오류면 1회 재시도).

        v2 `restructure.review` (StepKind.REVIEW): the LLM checks poms / dependencies / module layout of the
        restructured tree; the script it writes for critical issues runs uncommitted in the reviewer and is committed
        here as `review: fix critical structure issues (<n>)` (revertible). Never stops a run: a failure is a note
        and verdict `failed`; a dry run is verdict `skipped` without an LLM call."""
        from sbmig.core.models import StructureReview
        self._flush_notes(pr)
        assert self.report is not None
        t = self.clock()
        base = dict(id=f"{self.ws.run_id}:review", run_id=self.ws.run_id,
                    project=(self.profile.name if self.profile is not None and self.profile.name else "")
                    or self.report.project_name or self.ws.root.name,
                    case_type=self.profile.case_type if self.profile is not None else CaseType.UNKNOWN)
        rv: Optional[StructureReview] = None
        pre: Optional[str] = None
        if not self._dry_run:
            try:
                pre = self.ws.head()
            except Exception:  # noqa: BLE001 - no git: no diff for the review's entries
                pre = None
        if self._dry_run:
            rv = StructureReview(**base, verdict="skipped", note="dry run: no LLM call, nothing changed")
        elif not getattr(getattr(self.cfg, "review", None), "enabled", True):
            rv = StructureReview(**base, verdict="skipped", note="review.enabled is off")
        else:
            reviewer, why = self._make_reviewer()
            if reviewer is None:
                rv = StructureReview(**base, verdict="failed", note=why)
            else:
                try:
                    rv = reviewer.review(self.ws, self.profile, self.cfg, run_id=self.ws.run_id,
                                         restructure_notes=list(self._restructure_notes),
                                         restructure_results=list(self._restructure_results))
                except Exception as e:  # noqa: BLE001 - the review never stops a run
                    log.warning("structure review failed: %s: %s", type(e).__name__, e)
                    rv = StructureReview(**base, verdict="failed", note=f"review error: {type(e).__name__}: {e}")
                if rv is None:
                    rv = StructureReview(**base, verdict="failed", note="the reviewer returned nothing")
                if why and rv.verdict == "skipped" and not rv.note:
                    rv.note = why
        self._review_llm_usage(rv)
        sha = self._commit_review(pr, step, rv)
        if sha:
            self._review_major_upgrades(rv, self._review_commit_pre or pre, sha)
        self._review_entries(rv, pre, sha)
        self.report.structure_review = rv
        critical = len(rv.critical)
        status = rv.script_result.status if rv.script_result is not None else None
        self.ws.journal({"event": "structure_review", "step": step.id, "verdict": rv.verdict,
                         "issues": len(rv.issues), "critical": critical, "script_status": status, "commit": sha,
                         "note": (rv.note or "")[:300]})
        ans = getattr(rv, "answer", None)
        how = f"edits ({len(ans.edits)})" if ans is not None and ans.edits else "script"   # v2.1: what the fix was
        line = (f"{step.id}: structure review {rv.verdict} ({len(rv.issues)} issue(s), {critical} critical)"
                + (f"; {how} {status}" if status else "") + (f" → commit {sha[:10]}" if sha else "")
                + (f" — {rv.note}" if rv.note else ""))
        pr.notes.append(line[:600])
        log.info("%s", line[:600])
        self._timed("review", t)
        return True

    def _commit_review(self, pr: PhaseResult, step: PlanStep, rv: Any) -> Optional[str]:
        """검토가 남긴 변경을 커밋한다. 수정이 성공(``script_result.status == ok``)하지 않았는데 트리가
        바뀌어 있으면 버린다 — 검토기는 실패했을 때 트리를 받은 그대로 두어야 한다.

        Commit the review script's changes (`review.commit`); changes without a successful script run are
        discarded (a reviewer must leave the tree as it found it when its script failed)."""
        self._review_commit_pre = None
        if self._dry_run:
            return None
        try:
            if not self.ws.is_dirty():
                return None
            if rv.script_result is not None and rv.script_result.status == "ok":
                pre = self.ws.head()
                self._review_commit_pre = pre
                sha = self.ws.commit(f"review: fix critical structure issues ({len(rv.critical)})",
                                     paths=list(rv.script_result.changed_files or []) or None)
                if sha == pre:
                    return None
                rv.commit = sha
                # the reviewer's note was written before this commit ("… — not committed yet"): say what happened
                note = rv.note or ""
                rv.note = (note.replace(" — not committed yet", f" — committed {sha[:10]}")
                           if " — not committed yet" in note else note)
                return sha
            self.ws.discard_uncommitted()
            pr.notes.append(f"{step.id}: the structure review left uncommitted changes without a successful script "
                            "run — discarded")
        except Exception as e:  # noqa: BLE001 - git trouble: the build shows the tree as it is
            log.warning("structure review commit failed: %s: %s", type(e).__name__, e)
            pr.notes.append(f"{step.id}: committing the review script failed: {type(e).__name__}: {e}")
        return None

    def _review_entries(self, rv: Any, pre: Optional[str], sha: Optional[str]) -> None:
        """v2.1: 검토 결과를 오류 목록과 KB 에 남긴다(예외 없음, skipped·드라이런이면 없음). warning 마다,
        그리고 커밋된 수정이 다루지 못한 critical 마다 ``note`` 항목; 커밋된 수정이 다룬 critical 마다
        ``review`` 로 해결된 항목(시도 1개: 원인 / 해결 / 수정 기록 / diff / 커밋); 답변의 스크립트는 KB
        케이스 ``review|structure`` 에 저장. 수정 기록은 ``수정: …`` 로그로. 검토의 LLM 호출 수는 첫 해결 항목의
        시도에만 싣는다(나머지 0, A23).

        v2.1: the review on the error list and in the KB (never raises, nothing for a skipped review / dry run):
        a `note` entry per warning — and per critical issue no committed fix addressed —, an entry resolved by
        `review` per critical issue the committed fix addressed (one try: source review, 원인 / 해결 / edits /
        diff / commit), and the answer's script stored in the KB case `review|structure` (source review). v2.1b:
        the committed fix's 수정 notes (the edits' `desc` / the answer's `<changes>` + the diff's lines,
        `sbmig.loop.notes`) go on every such try and are logged once, one `수정: …` INFO line per note. v2.1h (A23):
        the review's LLM calls go on the first such try only (the others carry 0 and say so in their note)."""
        if self._dry_run or rv is None or rv.verdict == "skipped":
            return
        try:
            ans = getattr(rv, "answer", None)
            diff, files = "", []
            if sha and pre:
                try:
                    diff = self.ws.diff(pre, sha)
                    files = self.ws.changed_between(pre, sha)
                except Exception as e:  # noqa: BLE001 - the commit is there; only its text is missing
                    log.debug("review diff unavailable: %s", e)
            n_edits = len(getattr(ans, "edits", None) or []) if ans is not None else 0
            applied = ("edits" if n_edits else "script") if sha else "none"
            notes: list = []
            if sha:
                notes = try_notes(answer_notes(ans, edits=bool(n_edits)), diff,       # v2.1e: + as-is / to-be hunks
                                  max_lines=int(getattr(getattr(self.cfg, "errors", None), "snippet_lines", 0) or 0))
                for nt in notes[:NOTES_LOGGED]:
                    log.info("수정: %s", note_line(nt))
                if len(notes) > NOTES_LOGGED:
                    log.info("수정: … %d more (errors.md)", len(notes) - NOTES_LOGGED)
            # A23: 검토의 LLM 호출은 한 번에 여러 critical 을 고친다 — 첫 항목의 시도에만 호출 수를 싣고 나머지는
            # 0 으로 둔다(FixTry.llm_calls 를 합산하는 KB · errors.md 가 검토 호출을 항목 수만큼 세지 않게).
            # 보고서의 LLM 사용량은 이와 무관하게 예산 / 사용량 집계(_review_llm_usage)에서 온다
            review_calls = int(rv.llm_usage.calls or 0)
            first_fixed = True
            for issue in rv.issues:
                text = " ".join((issue.problem or "").split()) + (f" — {issue.fix_hint}" if issue.fix_hint else "")
                # v2.1y (E-M5): 재료에 없는 버전·속성을 말한 이슈는 적용되지 않았으므로 "검토가 고침" 으로 적지 않는다
                # v2.1y: an issue the reviewer marked `unsupported` was never applied — a note, never "fixed by the review"
                if getattr(issue, "unsupported", False):
                    self.errors.add_note(issue.file or "",
                                         f"{issue.severity} (not in the review material, not applied): {text}", "review")
                    continue
                if issue.severity == "critical" and sha:
                    calls = review_calls if first_fixed else 0
                    shared = "" if first_fixed or not review_calls else \
                        f"; the review's {review_calls} LLM call(s) are counted on its first fixed issue"
                    first_fixed = False
                    self.errors.review_fixed(
                        issue.file or "", f"critical: {text}",
                        analysis=(getattr(ans, "analysis", "") if ans is not None else "") or rv.analysis,
                        solution=getattr(ans, "solution", "") if ans is not None else "",
                        no_script_reason=getattr(ans, "no_script_reason", "") if ans is not None else "",
                        applied=applied, files=files, edits=list(notes), diff=diff, commit=sha, result="improved",
                        note=f"fixed by the structure review ({n_edits} edit(s), commit {sha[:10]}){shared}",
                        llm_calls=calls, seconds=0.0,
                        major_upgrades=list(getattr(rv, "major_upgrades", None) or []),
                        verify=(getattr(ans, "verify", "") if ans is not None else "") or "",
                        verify_source=(self._review_guide_source or ("llm" if ans is not None and ans.verify
                                                                      else "")) if getattr(rv, "major_upgrades", None)
                        else "")
                elif issue.severity == "critical":
                    self.errors.add_note(issue.file or "", f"critical (not fixed by the review): {text}", "review")
                else:
                    self.errors.add_note(issue.file or "", f"warning: {text}", "review")
            code = ((getattr(ans, "code", "") if ans is not None else "") or rv.script or "").strip("\n")
            store = CaseScripts.of(self.kb)
            if code.strip() and rv.critical and store.active:
                from sbmig.triage.cluster import l2_case_id
                cs = store.add_script(l2_case_id(REVIEW_L2), REVIEW_L2, code, source="review", run_id=self.ws.run_id,
                                      project=(self.profile.name if self.profile is not None else "") or
                                      self.ws.root.name,
                                      analysis=(getattr(ans, "analysis", "") if ans is not None else "") or rv.analysis,
                                      solution=getattr(ans, "solution", "") if ans is not None else "")
                if cs is not None:
                    self.ws.journal({"event": "review_script_stored", "case": l2_case_id(REVIEW_L2),
                                     "version": getattr(cs, "version", 0), "status": getattr(cs, "status", "")})
        except Exception as e:  # noqa: BLE001 - recording never stops a run
            log.warning("recording the structure review failed: %s: %s", type(e).__name__, e)

    def _review_major_upgrades(self, rv: Any, pre: str | None, sha: str) -> None:
        """v2.1d: 검토 커밋의 pom 을 되읽어 메이저 업그레이드·라이브러리 교체를 ``rv.major_upgrades`` 에
        운영자 확인 가이드와 함께 남긴다(답변의 ``<verify>``, 없으면 sbmig 가 만든 것). 수정 루프와 같은
        WARNING / INFO 줄. 되돌리지 않는다.

        v2.1d: the review commit's major upgrades / library replacements (`sbmig.build.depchanges`) →
        `rv.major_upgrades` with the 운영자 확인 guide (the answer's `<verify>`, else sbmig's synthesized one, stored
        into `rv.answer.verify` — or the note without an answer). Same WARNING / INFO lines as the fix loop (stage
        review). Never raises; nothing is reverted for it."""
        try:
            files: list[str] = []
            try:
                files = list(self.ws.changed_between(pre, sha)) if pre else []
            except Exception:  # noqa: BLE001 - let the read-back look at every pom of the commit
                files = ["pom.xml"]
            if not pom_changed(files):
                return
            changes = operator_changes(self.ws, pre, sha, self.cfg)
            if not changes:
                return
            rv.major_upgrades = changes
            ans = getattr(rv, "answer", None)
            guide = str(getattr(ans, "verify", "") or "").strip() if ans is not None else ""
            source = "llm" if guide else "sbmig"
            if not guide:
                lang = str(getattr(self.cfg.report, "language", "") or "ko").lower()
                guide = fallback_guide(changes, lang if lang in ("en", "ko") else "ko")
                if ans is not None:
                    ans.verify = guide
                else:
                    rv.note = (f"{rv.note}; " if rv.note else "") + "운영자 확인: " + " / ".join(
                        x.strip() for x in guide.splitlines() if x.strip())[:600]
            self._review_guide_source = source
            log_major_upgrades(changes, guide, self.cfg)
            for ch in changes:
                self.ws.journal({"event": "major_upgrade", "step": "review", "change": ch.model_dump(mode="json"),
                                 "guide_source": source})
        except Exception as e:  # noqa: BLE001 - a report aid, never a reason to fail the review
            log.debug("review read-back failed: %s: %s", type(e).__name__, e)

    def _review_llm_usage(self, rv: Any) -> None:
        """검토의 LLM 호출도 실행의 사용량·예산에 들어간다: 예산 걸린 클라이언트로 이미 셌으면 그대로, 자기
        클라이언트를 쓴 검토기면 ``StructureReview.llm_usage`` 로 센다(두 번 세지 않는다).

        The review's LLM calls count toward the run's usage and budget: through the budgeted client (`_review_usage`)
        or — a reviewer with its own client — from `StructureReview.llm_usage`."""
        u = getattr(rv, "llm_usage", None)
        if self._review_usage or u is None or not u.calls:
            return
        self.budget.charge_llm(u.calls)
        key = u.model or self.cfg.llm.model or "?"
        self._review_usage[key] = self._review_usage.get(key, LLMUsage(model=key)).add(u)

    def _follow_moves(self, step: PlanStep, pre: str, sha: str) -> None:
        """재구성이 옮긴 파일(``git diff -M`` 이름 바꾸기)을 차단 등록부에 알려, 옮기기 전에 차단된 파일이
        ``{name}-back/src/…`` 에서도 차단된 채로 있게 한다.
        최선 노력: git 문제면 옮겨진 오류를 다시 볼 뿐이다.

        Round 10 (A4): the files the step moved (`git diff -M` renames) → the run-wide blocked registry re-keys
        them, so a file blocked before the move stays blocked at `{name}-back/src/…`. Best effort: a git problem
        only means the moved errors are looked at again."""
        try:
            res = self.ws._git("diff", "--name-status", "-M", "-z", pre, sha, check=False)   # noqa: SLF001
            moves = parse_renames(res.stdout or "") if res.returncode == 0 else {}
        except Exception as e:  # noqa: BLE001 - never fail the restructure over the bookkeeping
            log.debug("restructure moves of %s unavailable: %s", step.id, e)
            return
        if not moves:
            return
        n = self.blocked.note_moves(moves)
        self.ws.journal({"event": "restructure_moves", "step": step.id, "moved": len(moves), "blocked_rekeyed": n})

    def _restructure_failed(self, step: PlanStep, mode: str, why: str) -> None:
        """재구성 실패가 성공한 마이그레이션처럼 보이면 안 된다: as-is 트리로도 최종 빌드는 통과할 수 있지만
        회사 레이아웃·parent 는 만들어지지 않았다 → ``restructure-failed`` 플래그(success=False, exit 2) +
        인수인계 문구. 선택 버전 올리기만 예외.

        Round 9c: a failed / crashed restructure step must not look like a successful migration — the final build
        may pass on the as-is tree, but the company layout / parents were not produced. Flag `restructure-failed`
        (→ `success=False`, exit 2) and say so in the Handover."""
        assert self.report is not None
        self.ws.journal({"event": "restructure_failed", "step": step.id, "mode": mode, "error": why[:500]})
        if mode == "optional_bump":          # optional in-house bumps are best effort: a follow-up, not a failure
            self.report.manual_followups.append(f"optional artifact bump step {step.id} failed: {why[:300]} — bump "
                                                "the in-house optional artifacts by hand")
            return
        self._flag("restructure-failed")
        self.report.manual_followups.append(
            f"restructure step {step.id} ({mode}) failed: {why[:300]} — the tree keeps the as-is layout / parents; "
            "the final build ran on it, so BUILD SUCCESS here does not mean the migration is complete")

    _MANAGED_POM_RE = re.compile(r"\(([^()]*pom\.xml)\)\s*$")
    _RECIPE_DROPPED_RE = re.compile(r"^(?P<key>\S+) (?P<explicit>\S+) \(as-is (?P<asis>[^,]+), managed "
                                    r"(?P<managed>[^,]+), (?P<pom>[^()]+)\)\s*$")
    _OPTIONAL_ADDED_RE = re.compile(r"^(?P<key>\S+) → (?P<pom>.+?) \(for (?P<matched>[^()]*)\)\s*$")

    def _pom_verify_entries(self, res: Any) -> None:
        """v2.1: 재구성이 보고한 pom 교정 하나하나(parent 지정, 필수 추가, 선택 올리기, 관리 버전 삭제,
        as-is 버전 이어 쓰기(v2.1l), nonFilteredFileExtensions(v2.1m), 트리거로 넣은 선택 아티팩트(v2.1n),
        생성된 pom)를 오류 목록의 ``pom-verify`` 항목으로 — 다음 빌드가 그 pom 에서 오류를 내지 않으면
        해결된다.

        v2.1: every pom correction the restructure reports → one error-list entry, status `pom-verify` (the next
        build verifies it): parents set (`<pom>: old → new`), mandatory artifacts added and optional ones bumped (the
        root pom), explicit versions dropped for the managed ones (`… (<pom>)`), versions carried over from
        the as-is build (v2.1l, `versions_pinned`), binary extensions written to `<nonFilteredFileExtensions>`
        (v2.1m, `non_filtered_extensions`), optional artifacts their triggers introduced (v2.1n, `optional_added`),
        as-is properties removed because they override the Boot BOM's (v2.1o, `bom_properties_removed`), generated
        poms."""
        root = "pom.xml"
        items: list[tuple[str, str]] = []
        for e in list(getattr(res, "parents_set", None) or []):
            pom, sep, what = str(e).partition(": ")
            items.append((pom if sep else root, f"parent {what if sep else e}"))
        for k in list(getattr(res, "mandatory_added", None) or []):
            items.append((root, f"mandatory {k} added"))
        for k in list(getattr(res, "optional_bumped", None) or []):
            items.append((root, f"optional {k}"))
        # v2.1n: "g:a → <pom> (for <matched>[, …])" → 'optional g:a added for <matched> in <pom>'
        for d in list(getattr(res, "optional_added", None) or []):
            m = self._OPTIONAL_ADDED_RE.match(str(d))
            if m:
                key, pom, matched = m.group("key"), m.group("pom"), m.group("matched")
                items.append((pom, f"optional {key} added for {matched} in {pom}"))
        for d in list(getattr(res, "managed_versions_dropped", None) or []):
            m = self._MANAGED_POM_RE.search(str(d))
            items.append((m.group(1) if m else root, f"explicit version dropped: {str(d)[:m.start()].strip() if m else d}"))
        # v2.1o: "g:a <explicit> (as-is <v | ->, managed <m>, <pom>)" → 'explicit version dropped: g:a <explicit> →
        # managed <m> (raised by a recipe from the as-is <v>)'
        for d in list(getattr(res, "recipe_versions_dropped", None) or []):
            m = self._RECIPE_DROPPED_RE.match(str(d))
            if not m:
                continue
            asis = m.group("asis").strip()
            how = (f"raised by a recipe from the as-is {asis}" if asis != "-"
                   else "added by a recipe: not in the as-is build")
            what = f"{m.group('key')} {m.group('explicit')} → managed {m.group('managed').strip()} ({how})"
            items.append((m.group("pom").strip(), f"explicit version dropped: {what}"))
        # v2.1l: "g:a: <v> ← as-is <source> (<pom>)" → 'version carried over from the as-is build: g:a <v>';
        # "g:a: <v> ← repository newest release (…) (<pom>)" → 'version chosen from the repository: g:a <v>'
        for d in list(getattr(res, "versions_pinned", None) or []):
            m = self._MANAGED_POM_RE.search(str(d))
            head = str(d)[:m.start()].strip() if m else str(d)
            key, _, rest = head.partition(": ")
            ver, _, how = rest.partition(" ← ")
            if how.startswith("Boot "):
                # v2.1s: "g:a: <v> ← Boot <bv> BOM (not managed by the parent chain) (<pom>)"
                bom = how.split(" ", 2)[1] if how.count(" ") >= 1 else "?"
                row = f"version pinned from the Spring Boot {bom} BOM: {key} {ver.strip()} (the parent chain does not " \
                      "manage it)"
                items.append((m.group(1) if m else root, row))
                continue
            what = ("version chosen from the repository" if how.startswith("repository")
                    else "version carried over from the as-is build")
            items.append((m.group(1) if m else root, f"{what}: {key} {ver.strip()}".rstrip()))
        # v2.1x (E-M6): "g:a: compile → provided (as-is scope, <pom>)" → 'scope restored: provided (as-is): g:a (was compile)'
        # v2.1x: 레시피가 compile 로 바꾼 as-is provided 스코프를 재구성이 되돌린 행
        for d in list(getattr(res, "scope_restored", None) or []):
            m = re.match(r"^(?P<key>\S+): (?P<old>\S+) → provided \(as-is scope, (?P<pom>[^()]+)\)\s*$", str(d))
            if m:
                items.append((m.group("pom").strip(),
                              f"scope restored: provided (as-is): {m.group('key')} (was {m.group('old')})"))
        # v2.1x (D-M7): "<pom>: project.build.sourceEncoding=MS949 (N files)" → 'source encoding written: …'
        for d in list(getattr(res, "source_encodings_written", None) or []):
            pom, sep, what = str(d).partition(": ")
            items.append((pom if sep else root, f"source encoding written: {what.split(' (')[0]}"))
        # v2.1m: "<pom>: ext1, ext2 → maven-resources-plugin[, maven-war-plugin]" →
        # 'nonFilteredFileExtensions added to maven-resources-plugin in <pom>: ext1, ext2'
        for d in list(getattr(res, "non_filtered_extensions", None) or []):
            pom, sep, rest = str(d).partition(": ")
            exts, _, plugins = rest.partition(" → ")
            pom = pom if sep else root
            plugin_names = plugins.strip() or "maven-resources-plugin"
            items.append((pom, f"nonFilteredFileExtensions added to {plugin_names} in {pom}: {exts.strip()}"))
        # v2.1o: "<pom>: name=value" → 'property name=value removed: it overrides the Spring Boot <v> BOM's name'
        boot = str(getattr(getattr(self.cfg, "target", None), "spring_boot_version", "") or "")
        parent_names = set(getattr(getattr(self.cfg, "layout", None), "parent_property_names", None) or [])
        defined_by: dict[str, str] = dict(getattr(res, "bom_property_sources", None) or {})
        for d in list(getattr(res, "bom_properties_removed", None) or []):
            pom, sep, nv = str(d).partition(": ")
            name = nv.split("=", 1)[0]
            # v2.1p: 정의한 pom 을 알면 그것으로(Boot BOM 이면 BOM 문구), 설정 목록의 이름이면 사내 부모 문구
            src = defined_by.get(name, "")
            if src.startswith("org.springframework.boot:spring-boot-dependencies:"):
                whose = f"the Spring Boot {src.rsplit(':', 1)[1]} BOM's {name}"
            elif src:
                whose = f"the in-house parent chain's {name} ({src})"
            elif name in parent_names:
                whose = f"the in-house parent's {name}"
            else:
                whose = f"the Spring Boot {boot} BOM's {name}"
            items.append((pom if sep else root, f"property {nv} removed: it overrides {whose}"))
        # v2.1t: "<pom>: g:a ${prop} (property of <pom>)" → 'BOM imported: g:a ${prop} (property of <pom>)'
        for d in list(getattr(res, "bom_imports", None) or []):
            pom, sep, what = str(d).partition(": ")
            items.append((pom if sep else root, f"BOM imported: {what if sep else d}"))
        for d in list(getattr(res, "version_properties_removed", None) or []):
            pom, sep, nv = str(d).partition(": ")
            why = f"version property {nv} removed: no dependency references it after the parent-managed versions " \
                  "were dropped"
            items.append((pom if sep else root, why))
        for g in list(getattr(res, "generated_files", None) or []):
            if PurePosixPath(str(g).replace("\\", "/")).name == "pom.xml":
                items.append((str(g).replace("\\", "/"), "generated by the restructure"))
        for pom, note in items:
            self.errors.add_pom_verify(pom, note)
            log.info("%s: %s (pom-verify #%d)", pom, note, self.errors.size())

    def _jsp_jakarta_note(self, pr: PhaseResult, step: PlanStep, res: Any) -> None:
        """v2.1h (A5): 재구성이 JSP 의 javax EE 이름을 바꿨으면 그 수를 단계 메모(``<step>: JSP javax → jakarta:
        N files, M references``)로 남기고 저널에 파일 목록을 쓴다. RunReport 에 따로 필드가 없어 보고서의 "What we
        changed" 와 인수인계의 남은 JSP 수는 이 메모를 읽는다(``sbmig.report.handover.jsp_jakarta_counts``).

        v2.1h (A5): the restructure's JSP rename (`RestructureResult.jsp_jakarta_files` / `_refs` / `_renamed`) as a
        step note the report reads back, plus a `jsp_jakarta` journal event with the files. Never raises."""
        try:
            files = int(getattr(res, "jsp_jakarta_files", 0) or 0)
            refs = int(getattr(res, "jsp_jakarta_refs", 0) or 0)
            if files <= 0:
                return
            from sbmig.report.handover import jsp_jakarta_note
            pr.notes.append(jsp_jakarta_note(step.id, files, refs))
            self.ws.journal({"event": "jsp_jakarta", "step": step.id, "files": files, "refs": refs,
                             "renamed": list(getattr(res, "jsp_jakarta_renamed", None) or [])[:500]})
        except Exception as e:  # noqa: BLE001 - a report aid, never a reason to fail the restructure
            log.debug("JSP rename note skipped: %s: %s", type(e).__name__, e)

    def _merge_placeholders(self, files: list[str], keys: list[str]) -> None:
        """보고서의 properties = 프로필의 설정 목록 + 재구성이 쓴 placeholder 들(중복 제거, 순서 유지).

        RunReport.properties = profile inventory + the placeholders every restructure step wrote (dedupe, ordered)."""
        assert self.report is not None
        base = self.report.properties
        if base is None:
            base = getattr(self.profile, "properties", None)
        if base is None:
            from sbmig.core.models import PropertyInventory
            base = PropertyInventory()
        merged_files = list(base.placeholder_files) + [f for f in files if f not in base.placeholder_files]
        merged_keys = list(base.placeholders_written)
        for k in keys:
            if k not in merged_keys:
                merged_keys.append(k)
        self.report.properties = base.model_copy(update={"placeholder_files": merged_files,
                                                         "placeholders_written": merged_keys})

    @staticmethod
    def _step_mode(step: PlanStep) -> str:
        return str(step.params.get("mode") or _DEFAULT_MODE.get(step.kind, "layout"))

    def _call_restructurer(self, mode: str, params: dict) -> Any:
        """재구성기 호출. ``layout`` / ``msa_pom`` / ``boot_pom`` 이면 인자 없는 함수 두 개를 params
        에 넣어 준다: 재구성 뒤의 effective pom(``effective_pom``)과 접수 때의 as-is effective pom
        (``asis_effective``, v2.1l). ``params`` 를 못 받는 옛 재구성기는 mode 만으로 부른다.

        Calls the restructurer; for `layout` / `msa_pom` / `boot_pom` it adds the zero-arg callables
        `effective_pom` (the tree after the pom edits, round 9) and `asis_effective` (the intake capture,
        v2.1l). The restructurer calls `effective_pom` only when it drops managed versions or has as-is
        versions to check."""
        assert self.profile is not None
        r = self.restructurer
        try:
            import inspect
            sig = inspect.signature(r.apply)
            takes_params = "params" in sig.parameters or any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
        except (TypeError, ValueError):  # pragma: no cover - builtins / mocks
            takes_params = True
        if mode in ("layout", "msa_pom", "boot_pom"):
            # v2.1l: as-is 버전 이어 쓰기는 drop_managed_versions 와 무관하다. 재구성 뒤 effective
            # pom 도 늘 넘긴다 — off 여도 재구성이 적은 as-is 버전을 새 부모 체인이 관리하는지
            # 확인해야 하므로(적은 것이 있을 때만 부른다)
            params.setdefault("asis_effective", self._asis_effective)       # v2.1l, zero-arg callable
            params.setdefault("effective_pom", self._effective_pom_after)   # round 9 / 9c, zero-arg callable
        if takes_params:
            return r.apply(self.ws, self.profile, self.cfg, mode=mode, params=params)
        return r.apply(self.ws, self.profile, self.cfg, mode=mode)

    def _asis_effective(self) -> dict | None:
        """v2.1l: 접수 때 캡처한 as-is effective pom(``logs/effective-depmgmt.json``) — 재구성이
        새 부모로 옮기며 잃은 버전을 채울 때 쓴다. 파일이 없거나 비었거나 못 읽으면 None, 예외 없음
        (캡처는 덮어쓰지 않고 읽기만).

        v2.1l: the as-is capture taken at intake (`load_effective(ws)`, `logs/effective-depmgmt.json`), the
        source of the versions the restructure carries over; None when absent / empty / unreadable. Never
        raises."""
        try:
            load = lazy_attr("sbmig.build.effective", "load_effective", "G1 (build)")
            data = load(self.ws)
        except Exception as e:  # noqa: BLE001 - no capture only means no carry-over
            log.debug("as-is effective pom unavailable: %s: %s", type(e).__name__, e)
            return None
        if not isinstance(data, dict) or not (data.get("managed") or data.get("direct")
                                              or data.get("by_module")):
            return None
        return data

    EFFECTIVE_AFTER_FILE = "depmgmt-after-restructure.json"

    def _effective_pom_after(self) -> Optional[dict]:
        """재구성기가 사내 parent 를 붙인 *뒤* 의 effective dependencyManagement — 별도 파일에 캡처해 as-is
        캡처(``depmgmt.json``, 인수인계의 as-is 버전)를 절대 덮어쓰지
        않는다. None 이면 재구성기는 관리 버전 정리를 건너뛴다.

        Round 9 (`layout.drop_managed_versions`): the effective dependencyManagement of the tree *after* the
        restructurer set the in-house parent, captured into a separate file so the as-is capture (`depmgmt.json`,
        the as-is versions of the Handover) is never overwritten. None = not available (custom build runner without a
        capture, a capture that cannot take a file name, Maven failure) → the restructurer skips the cleanup."""
        fn = self._effective_capture
        if fn is None:
            if not self._real_maven:
                self.ws.journal({"event": "effective_depmgmt_after", "skipped": "custom build runner"})
                return None
            try:
                fn = lazy_attr("sbmig.build.effective", "capture_effective_depmgmt", "G1 (build)")
            except MissingComponentError as e:
                self.ws.journal({"event": "effective_depmgmt_after", "skipped": str(e)})
                return None
        try:
            load = lazy_attr("sbmig.build.effective", "load_effective", "G1 (build)")
            path = fn(self.ws, self.cfg, name=self.EFFECTIVE_AFTER_FILE)
            data = load(self.ws, name=self.EFFECTIVE_AFTER_FILE) if path is not None else None
        except TypeError as e:               # a capture / loader without `name` → never clobber depmgmt.json
            self.ws.journal({"event": "effective_depmgmt_after", "skipped": f"capture without name=: {e}"})
            return None
        except Exception as e:
            log.warning("effective dependencyManagement (after restructure) capture failed: %s", e)
            self.ws.journal({"event": "effective_depmgmt_after", "ok": False, "error": f"{type(e).__name__}: {e}"})
            return None
        self.ws.journal({"event": "effective_depmgmt_after", "ok": bool(data), "path": str(path) if path else None})
        return data or None

    def _step_verify(self, pr: PhaseResult, step: PlanStep) -> bool:
        """검증 빌드(빌드 게이트): ``mvn clean package -Dmaven.test.skip=true`` 를 돌려 오류 목록에
        반영하고, 실패하면 수정 루프(``_verify_fix_loop``)를 돌린 뒤 최종 빌드를 한 번 더 한다. 실행의 성공
        여부는 이 최종 빌드들이 정한다."""
        self._flush_notes(pr)
        if self._dry_run:
            pr.notes.append(f"{step.id}: dry-run → final build skipped")
            return True
        if not self.cfg.loop.always_final_verify and self._phase_failed:
            pr.notes.append(f"{step.id}: skipped (an earlier phase failed and loop.always_final_verify=false)")
            return False
        label = "verify" + (f"-{step.params['service']}" if step.params.get("service") else "")
        from .fixloop import build_target_args
        module, extra = build_target_args(self.profile, step.target)
        kw: dict[str, Any] = {"extra_args": extra} if extra else {}
        goal = step.build_goal or "clean package"

        def _skip_flags() -> tuple[bool, bool]:
            # read at build time: the final fix loop may have triggered the test-compile / tests fallback (L7 / L7b);
            # round 10: per build target — svc-a's fallback does not change how svc-b's final build runs
            tc, ts = self._loop_skips(step.target)
            skip_tests = bool(getattr(self.cfg.build, "skip_test_compile", False)) or tc
            # round 8: only runtime test failures were left → the final verify packages with -DskipTests too
            # (skip_test_compile wins when both apply: -Dmaven.test.skip=true already implies no test run)
            skip_run = ts and not skip_tests
            return skip_tests, skip_run

        def _run_final() -> BuildResult:
            skip_tests, skip_run = _skip_flags()
            if skip_run:
                try:
                    return self.build_runner.run(goal, module=module, label=label, skip_test_compile=False,
                                                 skip_tests=True, **kw)
                except TypeError:  # an older runner without the `skip_tests` option
                    kw["extra_args"] = [*extra, "-DskipTests"]
                    return self.build_runner.run(goal, module=module, label=label, skip_test_compile=False, **kw)
            return self.build_runner.run(goal, module=module, label=label, skip_test_compile=skip_tests, **kw)

        def _final_build() -> BuildResult:
            before = _status_paths(self.ws)
            res = refine_build(_run_final())           # the refined case keys (sbmig.triage.cluster)
            _restore_build_byproducts(self.ws, before)
            return res

        res = _final_build()
        self._observe_build(goal, res, step.target)
        # v2.1z-ah: Maven 의 HTTP 차단 미러 — 수정 루프(사전 시도·LLM)를 열지 않고 실행을 멈춘다
        blocked_http = (not res.ok and self.loop is not None and not getattr(self, "_after_abort", False)
                        and self.loop.http_blocked_abort(pr, step.phase, res, 1, where=step.id))
        # 실패한 첫 빌드가 곧 수정 루프의 빌드 1 이다(같은 트리를
        # 다시 빌드하지 않음). 루프가 끝나면 무조건 다시 빌드
        if not res.ok and not blocked_http and self._verify_fix_loop(pr, step, res):
            if self.loop is not None and self.loop.aborted:
                pr.notes.append(f"{step.id}: final fix loop aborted the run ({self.loop.aborted}); the failed build stands")
            else:
                pr.notes.append(f"{step.id}: final build repeated after the fix loop")
                log.info("repeating `mvn %s` after the fix loop", goal)
                res = _final_build()
                self._observe_build(goal, res, step.target)
        self._verify_results.append(res)
        if step.id not in self._verify_done:
            self._verify_done.append(step.id)
        skip_tests, skip_run = _skip_flags()
        assert self.report is not None
        self.report.final_build = res
        pr.iterations += 1
        pr.remaining_errors += res.errors
        pr.iteration_records.append(IterationRecord(phase=Phase.VERIFY, iteration=pr.iterations, build_ok=res.ok,
                                                    error_count=res.error_count, signatures=res.signature_multiset(),
                                                    build_log=res.log_path))
        if skip_tests and getattr(self.cfg.build, "skip_test_compile", False):
            pr.notes.append(f"{step.id}: built with -Dmaven.test.skip=true (build.skip_test_compile: tests are never "
                            "compiled in this configuration)")
        elif skip_tests:
            pr.notes.append(f"{step.id}: built with -Dmaven.test.skip=true (test-compile-skipped earlier)")
        if skip_run:
            pr.notes.append(f"{step.id}: built with -DskipTests (tests-skipped earlier: runtime test failures only)")
        self.ws.journal({"event": "verify", "step": step.id, "target": step.target, "ok": res.ok,
                         "errors": res.error_count, "log": res.log_path, "skip_test_compile": skip_tests,
                         "skip_tests": skip_run})
        return res.ok

    def _loop_skips(self, target: Optional[str]) -> tuple[bool, bool]:
        """수정 루프의 대체 빌드 결과(test-compile 건너뜀, 테스트
        실행 건너뜀) — 빌드 대상별(MSA 서비스마다 따로).

        (test-compile skipped, tests skipped) of the fix loop's fallbacks for a build of `target`."""
        lp = self.loop
        if lp is None:
            return False, False
        fn = getattr(lp, "skips_for", None)
        if callable(fn):
            try:
                tc, ts = fn(target)
                return bool(tc), bool(ts)
            except Exception:  # pragma: no cover - a foreign loop object
                pass
        return bool(getattr(lp, "test_compile_skipped", False)), bool(getattr(lp, "tests_skipped", False))

    def _verify_fix_loop(self, pr: PhaseResult, step: PlanStep, first: BuildResult) -> bool:
        """최종 ``mvn clean package`` 가 실패했다 → ``loop.phases.verify`` 의 스크립트 수정 루프(최대 50
        라운드). v2 에서는 이것이 유일한 수정 루프다. 남은 클러스터가 전부 이미 차단된 것이면 돌리지
        않는다(같은 실행에서 케이스당 한도는 ``script.max_llm_tries_per_case``). 루프를 돌렸으면 True — 호출한
        쪽이 최종 빌드를 다시 한다(불변조건 1).

        The final `mvn clean package` failed → the script fix loop on `loop.phases.verify` (goal `package`), after
        which the caller repeats the final build (invariant 1: the run still ends on a `clean package`). v2: this is
        THE fix loop (no intermediate loops); its build 1 is the failed final build itself (not built again). A case
        blocked earlier stays blocked: its limit is script.max_llm_tries_per_case per run. Returns True when the loop
        ran."""
        pc = self.cfg.loop.phases.get(Phase.VERIFY.value)
        if pc is None or not pc.enabled or pc.max_iterations <= 0 or self.loop is None or self._dry_run \
                or getattr(self, "_after_abort", False):
            return False
        if self.budget.time_exceeded():
            pr.notes.append(f"{step.id}: failed; no final fix loop (run time budget exhausted)")
            return False
        clusters = self.loop._cluster(list(first.errors), step.target)          # noqa: SLF001 - same package
        covering = [self.blocked.covering(c) for c in clusters]
        if clusters and all(it is not None for it in covering):
            reasons = sorted({it.reason.value for it in covering if it is not None})
            pr.notes.append(f"{step.id}: failed with blocked case(s) only ({', '.join(reasons)}) → no fix loop; "
                            "see 'Blocked' in the report")
            return False
        self.ws.journal({"event": "verify_loop_start", "step": step.id, "goal": pc.goal, "target": step.target,
                         "max_iterations": pc.max_iterations, "errors": first.error_count,
                         "clusters": [c.id for c in clusters]})
        log.info("final build failed with %d error(s) in %d case(s) → fix loop (mvn %s per round, up to %d round(s))",
                 first.error_count, len(clusters), pc.goal, pc.max_iterations)
        res = self.loop.run(Phase.VERIFY, pc.goal, pc.max_iterations, target=step.target, first_build=first)
        pr.iterations += res.iterations
        pr.attempts += res.attempts
        pr.iteration_records += res.iteration_records
        pr.rewrite_results += res.rewrite_results
        pr.notes += [f"{step.id} loop: {n}" for n in res.notes]
        if BuildFixLoop.test_compile_skipped_in(res) or BuildFixLoop.tests_skipped_in(res):
            # the L7 / L7b fallback passed the loop: its test errors are what the owners must fix (unresolved)
            pr.remaining_errors += [e for e in res.remaining_errors if e not in pr.remaining_errors]
        self._merge_blocked(pr, res.blocked)
        self.ws.journal({"event": "verify_loop_end", "step": step.id, "build_ok": res.build_ok,
                         "attempts": len(res.attempts), "iterations": res.iterations,
                         "blocked": [b.id for b in res.blocked]})
        return True

    # ------------------------------------------------------------------ kb
    # resolution error kinds (infra:repository at finalize)
    _RESOLUTION_KINDS = frozenset({ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.PARENT_RESOLUTION,
                                   ErrorKind.PLUGIN_RESOLUTION})

    def _advise_plan(self, plan):
        """``plan.advisor: llm`` 일 때만(기본 꺼짐) 싼 레시피 모델이 규칙 기반 계획을 검토한다(추가만 가능).

        cfg.plan.advisor == "llm": let the (cheap) recipe model review the rule-based plan (additive only)."""
        if self.cfg.plan.advisor != "llm" or self._dry_run or self.profile is None:
            return plan
        try:
            advise = lazy_attr("sbmig.plan", "advise_plan", "A (plan)")
            llm = self.llm_client()
            before = {r.id for st in plan.steps for r in st.recipes}
            plan = advise(plan, self.profile, llm=llm, cfg=self.cfg)
            after = {r.id for st in plan.steps for r in st.recipes}
            self.ws.journal({"event": "plan_advice", "model": self.cfg.llm.recipe_model or self.cfg.llm.model,
                             "added": sorted(after - before),
                             "notes": [n for n in plan.notes if n.startswith("advisor")]})
        except Exception as e:  # the advisor is optional
            log.warning("plan advisor failed: %s", e)
            self.ws.journal({"event": "plan_advice", "error": f"{type(e).__name__}: {e}"})
        return plan

    # ------------------------------------------------------------------ v2.1 error list
    def _start_error_list(self, dry_run: bool) -> None:
        """실행의 오류 목록을 연다(``RunErrors``): 컴파일 단계부터 모든 빌드 오류, pom 교정, 검토 결과, 수정
        시도가 여기에 쌓인다. 드라이런이면 없음(아무것도 빌드하지 않으니까).

        The run's error list (`sbmig.errors`, through `RunErrors`): from the compile step on, every build error,
        pom correction, review finding and fix try. None in a dry run (nothing builds)."""
        self.errors = RunErrors(None)
        if dry_run:
            return
        project = ((self.profile.name if self.profile is not None else "") or
                   (self.report.project_name if self.report is not None else "") or self.ws.root.name)
        self.errors = RunErrors.create(self.ws, self.cfg, project, factory=self._error_list_factory)

    def _observe_build(self, goal: str, res: BuildResult, target: Optional[str]) -> None:
        """최종 ``mvn clean package`` 를 오류 목록에 반영(새 오류만 추가, 안 보이는 열린 항목은 해결,
        pom-verify 검증) + 로그 한 줄. 타임아웃이거나 오류를 하나도 파싱 못 한 실패 빌드는 아무것도 해결하지
        않는다("안 보임 ≠ 해결").

        A final `mvn clean package` on the error list (only its NEW errors are added; the open entries it no longer
        shows are resolved; the pom-verify entries are verified) + one line: `mvn clean package: 25 error(s), 새 에러
        2건 추가, pom-verify 1건 resolved`."""
        void = bool(res.timed_out) or (not res.ok and not res.errors)
        added = self.errors.add_build(res, "build", void=void, scope=target)
        poms = self.errors.verify_poms(res)
        if res.ok:
            line = f"mvn {goal}: BUILD SUCCESS"
        else:
            line = f"mvn {goal}: {res.error_count} error(s)" + (", timed out" if res.timed_out else "")
            line += f", 새 에러 {added}건 추가" if self.errors.active else ""
        if poms:
            line += f", pom-verify {poms}건 resolved"
        log.info("%s", line)

    def _finalize_errors(self, report: RunReport) -> None:
        """v2.1 마무리: 열린 항목 → unresolved, 실행 디렉터리에 errors.jsonl + errors.md, 보고서의 오류
        항목, KB 에 이 실행의 항목을 덧붙임(``kb.record_run``, KB 가 없으면(``NullKnowledgeBase``) 없음). 실행을 실패시키지 않는다
        — 문제는 ``errors-failed: …`` 플래그.

        v2.1 finalize: the open entries → unresolved (`end_of_run`), `<run dir>/errors.jsonl` + `errors.md`,
        `report.error_entries` (diffs capped), the KB gets the run's entries (`kb.record_run`; not with a `NullKnowledgeBase`).
        Never fails the run: a problem becomes the flag `errors-failed: <msg>`."""
        el = self.errors
        if not el.active or self._dry_run:
            return

        def flag(text: str) -> None:
            if text not in report.flags:
                report.flags.append(text)

        try:
            counts = el.end_of_run()
            report.outputs.update(el.write())
            report.error_entries = el.to_report()
            for p in el.problems[:3]:
                flag(f"errors-failed: {p}")
            kb_n = None
            rec = getattr(self._kb, "record_run", None) if self._kb is not None else None
            if callable(rec):
                with stage("kb"):
                    try:
                        kb_n = rec(el.entries())
                    except Exception as e:  # noqa: BLE001 - the KB says it never raises; stay defensive
                        log.warning("kb.record_run failed: %s: %s", type(e).__name__, e)
                        flag(f"errors-failed: kb: {type(e).__name__}: {e}")
            self.ws.journal({"event": "error_list", "counts": counts, "entries": len(report.error_entries),
                             "kb_recorded": kb_n})
            log.info("error list: %d entr%s (%s)%s", len(report.error_entries),
                     "y" if len(report.error_entries) == 1 else "ies",
                     ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "-",
                     f"; {kb_n} recorded in the KB" if kb_n is not None else "")
        except Exception as e:  # noqa: BLE001 - never fail the run over the error list
            log.exception("error list failed")
            flag(f"errors-failed: {type(e).__name__}: {e}")

    def _flag(self, flag: str) -> None:
        if self.report is not None and flag not in self.report.flags:
            self.report.flags.append(flag)

    def _finalize_operator_checks(self, report: RunReport) -> None:
        """v2.1d 보고서의 "운영자 확인 필요": 시도들과 구조 검토가 만든 메이저 업그레이드·라이브러리 교체마다
        하나. 최종 트리에 아직 남은 것만(되돌린 시도는 빼고), 같은 (pom, 라이브러리, 버전)은 마지막 시도 것
        하나. 플래그 ``major-upgrade``. 예외 없음.

        v2.1d `report.operator_checks` (운영자 확인 필요): one OperatorCheck per major upgrade / library replacement
        of the error list's tries (the fix attempts' records without a list) and of the structure review — only the
        changes still in the final tree (`still_applied`; a rolled-back try is not listed), the same (pom, library,
        version) once (the latest try that made it). Flag `major-upgrade`. Never raises."""
        try:
            checks = self._operator_checks(report)
        except Exception as e:  # noqa: BLE001 - never lose the report over it
            log.warning("operator checks failed: %s: %s", type(e).__name__, e)
            return
        report.operator_checks = checks
        if checks:
            self._flag("major-upgrade")
            log.warning("운영자 확인 필요: 메이저 업그레이드 %d건 (report Handover \"운영자 확인 필요\")", len(checks))
            self.ws.journal({"event": "operator_checks", "count": len(checks),
                             "changes": [c.change.text for c in checks]})

    def _operator_checks(self, report: RunReport) -> list:
        """운영자 확인 항목 모으기: 구조 검토 커밋(되돌리지 않았을 때) → 오류 목록의 시도들(라운드·번호 순,
        적용 안 됨·악화·되돌림·적용 실패·롤백된 시도는 제외; 목록이 없으면 FixAttempt 로) →
        ``still_applied`` 로 최종 트리에 남은 것만."""
        from sbmig.core.models import OperatorCheck

        from .fixloop import ROLLED_BACK_NOTE
        by_commit = {a.commit: a for pr in report.phases for a in pr.attempts if a.commit}
        found: dict[tuple, OperatorCheck] = {}

        def add(ch: Any, **kw: Any) -> None:
            key = (ch.path, ch.key, ch.after)
            found.pop(key, None)                    # the latest try that made the change wins (its guide, commit)
            found[key] = OperatorCheck(kind="library_replaced" if ch.kind == "replaced" else "major_upgrade",
                                       change=ch, **kw)

        rv = report.structure_review
        if rv is not None and rv.commit and not rv.reverted:
            ans = rv.answer
            for ch in rv.major_upgrades or []:
                add(ch, source="review", commit=rv.commit or "", guide=(ans.verify if ans is not None else "") or "",
                    guide_source=self._review_guide_source or ("llm" if ans is not None and ans.verify else "sbmig"))
        tries: list[tuple[str, Any]] = []
        seen: set[tuple] = set()
        for e in list(self.errors.entries() or []):
            for t in getattr(e, "tries", None) or []:
                k = (getattr(e, "case_id", ""), getattr(t, "n", 0), getattr(t, "round", 0))
                if k in seen or not getattr(t, "major_upgrades", None) or getattr(t, "source", "") == "review":
                    continue
                seen.add(k)
                tries.append((str(getattr(e, "case_id", "") or ""), t))
        tries.sort(key=lambda ct: (getattr(ct[1], "round", 0), getattr(ct[1], "n", 0)))
        if tries:
            for case_id, t in tries:
                if getattr(t, "applied", "none") == "none" or t.result in ("regressed", "reverted", "apply_failed") \
                        or ROLLED_BACK_NOTE in (t.note or ""):
                    continue
                att = by_commit.get(t.commit) if t.commit else None
                for ch in t.major_upgrades:
                    add(ch, case_id=case_id, source=f"fix r{t.round} try {t.n}", commit=t.commit or "",
                        guide=t.verify or "",
                        guide_source=(getattr(t, "verify_source", "") or (att.verify_source if att is not None
                                                                           and att.verify_source else "sbmig")),
                        files=list(t.files or []))
        else:                                       # no error list (inactive): the attempts carry the same facts
            for pr in report.phases:
                for a in pr.attempts:
                    if not a.major_upgrades or not a.applied or a.outcome in ("regressed", "reverted",
                                                                               "apply_failed") \
                            or any(n.startswith(ROLLED_BACK_NOTE) for n in a.notes):
                        continue
                    for ch in a.major_upgrades:
                        add(ch, case_id=a.script_case_id, source=f"fix {a.id}", commit=a.commit or "",
                            guide=a.verify, guide_source=a.verify_source or "sbmig",
                            files=list(dict.fromkeys(c.new_path or c.path for c in a.changes)))
        out = []
        for c in found.values():
            if self._still_applied(c.change):
                out.append(c)
        return out

    def _still_applied(self, change: Any) -> bool:
        """그 변경이 최종 트리에 아직 있는가. 판단에 실패하면
        남긴다 — 한 번 더 보여 주는 편이 놓치는 것보다 낫다.

        `sbmig.build.depchanges.still_applied` (a failure keeps the check: better listed once too often)."""
        try:
            from sbmig.build.depchanges import still_applied
            return bool(still_applied(self.ws, change))
        except Exception as e:  # noqa: BLE001
            log.debug("still_applied(%s) unavailable: %s: %s", getattr(change, "key", "?"), type(e).__name__, e)
            return True

    # ------------------------------------------------------------------ finalize
    def _finalize_blocked(self, report: RunReport, unresolved: list[BuildError]) -> None:
        """보고서의 차단 목록 = 이 실행의 모든 차단 항목(id 로 중복 제거). 최종 빌드가 성공했으면 오류가 더
        이상 어디에도 없는 항목은 뺀다(예: 선택 버전 올리기로 풀림)
        — 테스트를 건너뛰어서만 통과한 테스트 오류 항목은 남긴다.

        RunReport.blocked = every item of the run (dedupe by id). After a green final build, items whose errors no
        longer show up anywhere (e.g. fixed by the optional bump / restructure) are dropped — unless they belong to
        test sources that only build with -Dmaven.test.skip=true, or tests that only fail at runtime (tests-skipped)."""
        items: list[BlockedItem] = []
        for it in [*self.blocked.items(), *(b for pr in report.phases for b in pr.blocked)]:
            if all(x.id != it.id for x in items):
                items.append(it)
        if report.success:
            still = {e.signature for e in unresolved}
            kept: list[BlockedItem] = []
            for it in items:
                tests_only = bool(it.errors) and all(e.kind == ErrorKind.TEST_COMPILE for e in it.errors)
                runtime_only = bool(it.errors) and all(e.kind == ErrorKind.TEST_FAILURE for e in it.errors)
                if (tests_only and "test-compile-skipped" in report.flags) or \
                        (runtime_only and "tests-skipped" in report.flags) or (set(it.signatures) & still):
                    kept.append(it)
                else:
                    self.ws.journal({"event": "blocked_resolved", "id": it.id, "reason": it.reason.value,
                                     "note": "not present in the green final build"})
            items = kept
        report.blocked = items

    def _resolve_javax_inventory(self) -> Optional[Callable]:
        """javax jar 목록 함수: 주입된 것, False = 끔, 진짜 Maven
        일 때만 ``sbmig.build.deptree``(mvn 을 띄우므로)."""
        if self._javax_inventory is False:
            return None
        if self._javax_inventory is not None:
            return self._javax_inventory
        if not self._real_maven:
            return None
        from sbmig.build.deptree import javax_api_inventory
        return javax_api_inventory

    def _finalize_javax_inventory(self, report: RunReport) -> None:
        """최종 빌드 뒤 classpath 에 아직 남은 javax EE API jar 와 그것을 끌고 오는 직접
        의존성(``mvn dependency:tree``) → 보고서 섹션·플래그·후속 조치, 그리고 차단된 X1 / X2 항목에 via + 그
        라이브러리를 가리키는 제안. 최선 노력.

        X1b (2026-10-22): after the final build, the javax EE API jars still on the resolved classpath and the
        direct dependency that drags each in (`mvn dependency:tree`) → report section, flag, follow-up, and the
        blocked X1 / X2 items get `via` + a suggestion naming that library. Best effort: never fails the run."""
        if not getattr(self.cfg.report, "javax_api_inventory", True) or self._dry_run:
            return
        if not any(pr.phase in (Phase.SPRING, Phase.TRANSFORM) for pr in report.phases):
            return                                   # no jakarta migration ran → nothing to look for
        fn = self._resolve_javax_inventory()
        if fn is None:
            return
        try:
            jars = list(fn(self.ws, self.cfg, self.profile) or [])
        except Exception as e:
            log.warning("javax API inventory failed: %s: %s", type(e).__name__, e)
            return
        report.javax_api_jars = jars
        try:
            from .blocked_registry import fill_via
            fill_via(self.blocked.items(), jars, self.cfg)
        except Exception as e:  # pragma: no cover - never lose the report
            log.warning("blocked via attribution failed: %s: %s", type(e).__name__, e)
        vias = list(dict.fromkeys(j.via.coordinates for j in jars if j.via is not None))
        self.ws.journal({"event": "javax_api_inventory", "jars": [j.artifact.coordinates for j in jars],
                         "via": vias})
        if not jars:
            return
        if "javax-api-on-classpath" not in report.flags:
            report.flags.append("javax-api-on-classpath")
        named = ", ".join(f"`{v}`" for v in vias[:3]) + (f" (+{len(vias) - 3} more)" if len(vias) > 3 else "")
        if not named:
            named = "declared directly in the poms (remove them)"
        fu = (f"{len(jars)} javax API jar(s) still on the classpath after the migration (see the report section) — "
              f"the libraries that drag them in need Jakarta builds: {named}")
        if fu not in report.manual_followups:
            report.manual_followups.append(fu)

    def _finalize_advisories(self, report: RunReport) -> None:
        """라이브러리 권고의 마무리: 최종 트리에서 다시 확인(결과), 후속 조치, 그리고 인수인계 섹션(바뀐
        버전, "이제부터 할 일")을 만든다. 예외 없음, 종료 코드를 바꾸지 않는다.

        Library advisories, finalize side: re-detect on the final tree (outcome), add their follow-ups and build
        the Handover (versions replaced + "from here on, your team"). Never raises, never changes the exit code."""
        findings = list(report.advisories or [])
        if findings and not self._dry_run:
            try:
                from sbmig.advisory import verify_advisories
                verified = verify_advisories(findings, self.ws, self.cfg)
                if verified is not None:
                    findings = list(verified)
                    report.advisories = findings
            except Exception as e:
                log.warning("advisory verification failed: %s", e)
                self.ws.journal({"event": "advisories_verified", "error": f"{type(e).__name__}: {e}"})
            self.ws.journal({"event": "advisories_verified",
                             "findings": [{"id": f.advisory_id, "policy": f.policy, "outcome": f.outcome}
                                          for f in findings]})
        if findings:
            self._attribute_llm_fixes(report, findings)
            try:
                from sbmig.advisory import followups_for
                fus = list(followups_for(findings) or [])
            except Exception as e:
                log.warning("advisory follow-ups unavailable: %s", e)
                fus = []
            report.manual_followups += [f for f in dict.fromkeys(fus) if f not in report.manual_followups]
        if self.profile is None:
            return
        effective: dict = {}
        try:
            from sbmig.build.effective import load_effective
            effective = load_effective(self.ws) or {}
        except Exception as e:
            log.debug("as-is effective pom unavailable for the handover: %s", e)
        try:
            from sbmig.report.handover import build_handover
            report.handover = build_handover(self.profile, self.ws, report, effective=effective, cfg=self.cfg)
        except Exception as e:  # pragma: no cover - build_handover never raises
            log.warning("handover failed: %s", e)
            return
        h = report.handover
        self.ws.journal({"event": "handover", "changes": len(h.changes), "carried_over": len(h.carried_over),
                         "from_here": len(h.from_here)})

    @staticmethod
    def _attribute_llm_fixes(report: RunReport, findings: list) -> None:
        """권고 힌트를 프롬프트에 실었던 성공한 LLM 수정(신규·보강, 적용됨, 개선·빌드 성공)이 바꾼 파일과
        원인 첫 문장을 그 권고에 붙인다 — 인수인계에서 "이 권고는 LLM 이 여기서 이렇게 처리했다" 를 보여
        주려고. 인수인계 생성 전에 돈다.

        AdvisoryFinding.llm_fixed_files / llm_fix_notes from the run's successful LLM fixes whose prompt carried the
        advisory's hint (FixAttempt.advisory_ids; v2: LLM fix scripts — new / augment, a reuse had no prompt —,
        applied, improved / build_ok, cluster not left unresolved). Runs before the Handover is built."""
        good = [a for pr in report.phases for a in pr.attempts
                if a.advisory_ids and a.decision.kind == DecisionKind.SCRIPT and a.applied
                and a.outcome in ("improved", "build_ok") and a.cluster_resolved is not False]
        for f in findings:
            files: list[str] = []
            notes: list[str] = []
            for a in good:
                if f.advisory_id not in a.advisory_ids:
                    continue
                for ch in a.changes:
                    for p in (ch.path, ch.new_path):
                        p = (p or "").replace("\\", "/").removeprefix("./")
                        if p and p not in files:
                            files.append(p)
                note = _first_sentence(a.analysis, 160)
                if note and note not in notes:
                    notes.append(note)
            f.llm_fixed_files, f.llm_fix_notes = files, notes

    def _usage_by_model(self, report: RunReport, plan_usage: Optional[LLMUsage]) -> list[LLMUsage]:
        """모델별 LLM 사용량(수정 시도·계획 조언·구조 검토) — 보고서의 비용 표.

        Fix-script attempts (their own model), the plan advisor and the structure review, per model."""
        by: dict[str, LLMUsage] = {}

        def acc(u: Optional[LLMUsage], fallback: str) -> None:
            if u is None or not (u.calls or u.input_tokens or u.output_tokens):
                return
            key = (u.model or fallback or "?").strip()
            by[key] = by.get(key, LLMUsage(model=key)).add(u.model_copy(update={"model": key}))
        for pr in report.phases:
            for a in pr.attempts:
                acc(a.llm_usage, a.model or self.cfg.llm.model)
        if plan_usage is not None:
            acc(plan_usage, self.cfg.llm.recipe_model or self.cfg.llm.model)
        for k, u in self._review_usage.items():
            acc(u, k or self.cfg.llm.model)
        return list(by.values())

    def _infra_flags(self, report: RunReport) -> None:
        """기반 시설 탓으로 끝난 실행에 ``infra:<종류>`` 플래그(llm / maven / jdk / repository / disk).
        플릿의 차단기가 이것을 보고 500개 프로젝트를 줄줄이 실패시키는 대신 멈춘다. repository = 남은 오류가
        전부 저장소·네트워크를 가리키는 해석 오류.

        Round 10 (A14, fleet circuit breaker): `infra:<kind>` when the run ended for an infrastructure reason —
        `infra:llm` (llm-unavailable / llm-config-error, and the run did not succeed), `infra:maven` (mvn-not-found),
        `infra:jdk` (jdk-not-21, or a Maven that cannot start because of JAVA_HOME), `infra:repository` (the
        configured parent is not resolvable, or every remaining error of the failed final build(s) is a dependency /
        parent / plugin resolution error whose text points at the repository or the network: could not / failed to
        transfer, connection refused / reset, timed out, unknown host, no route to host, PKIX, status / return code
        401 / 403 / 5xx, "was cached in the local repository"); v2.1r: `infra:file-lock` (a failed final build ended
        on a Windows file lock under target/ — the runner's retries did not clear it)."""
        flags = report.flags
        add: list[str] = []
        if ("llm-unavailable" in flags or "llm-config-error" in flags) and not report.success:
            add.append("infra:llm")                 # a run that still passed did not end for that reason
        if "mvn-not-found" in flags:
            add.append("infra:jdk" if self._mvn_java_problem else "infra:maven")
        if "jdk-not-21" in flags:
            add.append("infra:jdk")
        # v2.1z-ah: Maven 의 HTTP 차단 미러 — infra:repository 보다 구체적인 원인(운영자가 고칠 곳은 Maven settings)
        http_blocked = "aborted:http-blocked" in flags or (not report.success and self._http_blocker_trouble())
        if http_blocked:
            add.insert(0, "infra:http-blocked")
        elif "aborted:parent-not-found" in flags or self._repository_trouble():
            add.append("infra:repository")
        if not report.success and self._disk_full():
            add.append("infra:disk")                # the fleet pauses instead of failing the next projects too
        # v2.1r: 최종 빌드가 target/ 파일 잠금으로 실패(러너가 다시 돌려도) — 빌드 PC 환경 문제, 플릿은 나중에 다시 돈다
        if not report.success and self._file_lock_trouble():
            add.append("infra:file-lock")
        for f in add:
            if f not in flags:
                flags.append(f)

    def _file_lock_trouble(self) -> bool:
        """v2.1r: 실패한 최종 빌드 중 하나라도 Windows 파일 잠금(``sbmig.build.env_errors.file_lock_hit``)으로 끝났는가."""
        from sbmig.build.env_errors import file_lock_hit

        return any(file_lock_hit(f"{e.message}\n{e.raw}", self.cfg) is not None for r in self._verify_results
                   if not r.ok for e in r.errors)

    def _http_blocker_trouble(self) -> bool:
        """v2.1z-ah: 실패한 최종 빌드 중 하나라도 Maven 의 HTTP 차단 미러(``http_blocker_hit``)를 보였는가."""
        from sbmig.build.env_errors import http_blocker_hit

        return any(http_blocker_hit(f"{e.message}\n{e.raw}") is not None for r in self._verify_results
                   if not r.ok for e in r.errors)

    def _disk_full(self) -> bool:
        return any(_INFRA_DISK_RE.search(f"{e.message}\n{e.raw}") for r in self._verify_results if not r.ok
                   for e in r.errors)

    def _repository_trouble(self) -> bool:
        failed = [r for r in self._verify_results if not r.ok]
        errs = [e for r in failed for e in r.errors]
        if not failed or not errs:
            return False
        return all(e.kind in self._RESOLUTION_KINDS and _INFRA_REPO_RE.search(f"{e.message}\n{e.raw}")
                   for e in errs)

    def _finalize(self, report: RunReport) -> None:
        """마무리(어떤 경우에도 ``_run_guard`` 가 부른다): LLM 사용량·비용, 루프의 플래그·후속 조치, 성공
        판정(모든 최종 빌드 성공 + 재구성 실패 없음 + 중단 아님 + 빠진 최종 빌드 없음), 미해결 오류, 차단
        목록, infra 플래그, 운영자 확인, 권고·인수인계, 오류
        목록·KB 기록, 보고서 → 마지막에 느린 patch / zip."""
        report.finished_at = utcnow()
        usage = LLMUsage()
        for pr in report.phases:
            for a in pr.attempts:
                usage = usage.add(a.llm_usage)
        for u in self._review_usage.values():      # v2: the structure review
            usage = usage.add(u.model_copy(update={"model": usage.model}))
        usage.calls = max(usage.calls, self.budget.llm_calls)
        plan_usage = getattr(getattr(self, "plan", None), "llm_usage", None)
        if plan_usage is not None and plan_usage.calls:
            usage = usage.add(plan_usage)          # plan advisor (recipe model) calls
        report.llm_usage = usage
        report.llm_usage_by_model = self._usage_by_model(report, plan_usage)
        try:
            from sbmig.report.cost import compute_llm_cost
            report.llm_cost = compute_llm_cost(report.llm_usage_by_model, self.cfg)
        except Exception as e:  # pragma: no cover - pricing must never fail a run
            log.warning("llm cost not computed: %s", e)
        if self.loop is not None:
            for f in self.loop.flags:
                if f not in report.flags:
                    report.flags.append(f)
            report.manual_followups += [f for f in self.loop.followups if f not in report.manual_followups]
        self._classify_bundled_jars(report)      # v2.1z-i: superseded marks for the report (managed g:a)
        # a cap (loop.max_llm_calls > 0) reached, or -1 (the fleet's total used up) when a case needed the LLM
        if (self.budget.llm_limited and self.budget.llm_exhausted()) or (
                self.budget.llm_off and any(it.reason.value == "budget_exhausted" for it in self.blocked.items())):
            self._flag("llm-budget-exhausted")
        built = bool(self._verify_results) or any(pr.iterations for pr in report.phases)
        if getattr(self.cfg.build, "skip_test_compile", False) and not self._dry_run and built:
            # only when a build ran (v2 default true: an aborted run before any build carries no such follow-up)
            self._flag("tests-not-compiled")
            fu = ("test sources were neither compiled nor run (build.skip_test_compile: true): the owning team "
                  "compiles and fixes the tests (`mvn test-compile`) before relying on CI")
            if fu not in report.manual_followups:
                report.manual_followups.append(fu)
        # 성공 = 계획된 최종 빌드가 전부 돌았고 전부 성공 — MSA
        # 에서 svc-a 만 통과하고 svc-b 가 죽은 실행은 성공이 아니다
        # round 10 (invariant 1, A7): an aborted run is never a success, and every planned final build must have
        # run — an MSA run whose svc-a verify passed before svc-b's step crashed used to say BUILD SUCCESS
        # v2.1z-c: 속 빈 리액터(소스가 모듈 밖에 있어 이관되지 않음)는 빌드가 통과해도 성공이 아니다
        report.success = self.build_succeeded(report)
        # unresolved: what the as-is owner must look at
        unresolved: list[BuildError] = []
        for r in self._verify_results:
            if not r.ok:
                unresolved += r.errors
        if not self._verify_results and report.phases:
            unresolved += report.phases[-1].remaining_errors
        if "test-compile-skipped" in report.flags:
            for pr in report.phases:
                if BuildFixLoop.test_compile_skipped_in(pr):
                    unresolved += [e for e in pr.remaining_errors if e not in unresolved]
        if "tests-skipped" in report.flags:
            for pr in report.phases:
                if BuildFixLoop.tests_skipped_in(pr):
                    unresolved += [e for e in pr.remaining_errors if e not in unresolved]
        if report.properties is None and self.profile is not None:
            report.properties = getattr(self.profile, "properties", None)
        self._finalize_javax_inventory(report)
        try:
            self._finalize_blocked(report, unresolved)
        except Exception as e:  # never lose the report
            log.exception("finalizing the blocked items failed")
            report.flags.append(f"blocked-finalize-failed: {e}")
        report.unresolved = [e for e in unresolved if not self.blocked.is_blocked_error(e)]
        self._infra_flags(report)
        out = report.outputs
        out["run_dir"] = str(self.ws.run_dir)
        out["project_dir"] = str(self.ws.root)
        out["journal"] = str(self.ws.journal_path)
        out["logs"] = str(self.ws.logs_dir)
        for name in ("profile.json", "plan.json"):
            if (self.ws.run_dir / name).exists():
                out[name.replace(".json", "")] = str(self.ws.run_dir / name)
        # 순서가 중요: KB 기록과 보고서가 먼저, 느린 patch / zip
        # 은 마지막 — zip 도중 죽어도 중요한 것은 이미 남아 있다
        # round 10 (A13): the KB records and the report come first, the slow patch / zip last — a kill while zipping
        # a 1 GB tree loses nothing that matters; their paths are announced up front and dropped again on failure
        want_diff = bool(self.cfg.report.include_diff and (self.ws.root / ".git").exists())
        want_zip = bool(self.cfg.report.zip_result and not self._dry_run)
        if want_diff:
            out["diff"] = str(self.ws.run_dir / "changes.patch")
        if want_zip:
            out["zip"] = str(self.ws.run_dir / "result.zip")
        if "md" in self.cfg.report.formats:
            out["report_md"] = str(self.ws.run_dir / "report.md")
        if "json" in self.cfg.report.formats:
            out["report_json"] = str(self.ws.run_dir / "report.json")
        self._finalize_operator_checks(report)
        self._finalize_advisories(report)
        self._finalize_errors(report)
        writer = self._report_writer
        if writer is None:
            from sbmig.report import write_report
            writer = lambda r, w: write_report(r, w, cfg=self.cfg)      # noqa: E731 - report.language from the config

        def write() -> None:
            try:
                paths = writer(report, self.ws) or {}
                out.update({k: v for k, v in paths.items()})
            except Exception as e:
                log.exception("report writing failed")
                report.flags.append(f"report-failed: {e}")
        write()
        dropped: list[str] = []
        if want_diff:
            try:
                out["diff"] = str(self.ws.write_patch())
            except Exception as e:
                log.warning("could not write changes.patch: %s", e)
                dropped.append(str(out.pop("diff", "")))
        if want_zip:
            try:
                out["zip"] = str(self.ws.zip_result())
            except Exception as e:
                log.warning("could not write result.zip: %s", e)
                dropped.append(str(out.pop("zip", "")))
        if dropped:                        # the report named a file that does not exist: write it once more
            write()
        self.ws.journal({"event": "report", "success": report.success, "flags": report.flags,
                         "unresolved": len(report.unresolved), "blocked": len(report.blocked),
                         "llm_calls": report.llm_usage.calls,
                         "outputs": out})
