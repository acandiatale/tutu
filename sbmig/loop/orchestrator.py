"""프로젝트 하나의 이관을 처음부터 끝까지 실행하는 모듈.
변환, 재구성, 구조 검토, 검증 빌드를 차례로 실행한다. 빌드가 실패하면 수정 루프로 고치고, 끝에 보고서를 남긴다.

``Orchestrator.run`` is called by the CLI, the web UI, the fleet and ``sbmig.api.migrate``. A failure, a
blocked case, a used-up budget or an internal error still gets the final ``mvn clean package`` attempt and a
report. A run stops early (``aborted:*``, exit 3) only when no build can work, such as Maven missing or the
company parent not found. Ant projects are refused at intake unless ``intake.ant_to_maven: true``, which
synthesizes a pom.xml and goes on.

Flow `full`: intake -> analyze -> plan -> phases -> verify -> report. Flow `remediation`: intake (no project
edits) -> `sbmig.remediations.chain.run_only` -> report (no KB). `loop.engine` graph and inline run the same
`step_*` methods inside the same `_run_guard`, so the reports are equal.

Phases: transform (every OpenRewrite recipe in ONE run, `transform.rewrite`; `transform.compile` only with
`plan.compile_after_rewrite`) -> restructure (every pom correction it reports is an entry with status
`pom-verify`) -> structure review (`StepKind.REVIEW`: critical findings fixed by edits, committed `review: …`;
warnings become `note` entries; a fixed critical issue is an entry resolved by `review`, its script stored
under KB case `review|structure`) -> verify `mvn clean package` (only NEW errors are added; pom-verify entries
are verified) -> on failure the fix loop (`loop.phases.verify`, <= 50 rounds) and the final build again ->
finalize (`end_of_run`, `errors.jsonl` / `errors.md`, `report.error_entries`, `kb.record_run`). Every log line
carries its stage (`sbmig.core.stage`).

A phase is green only on BUILD SUCCESS; the final build runs whenever `loop.always_final_verify` is set.
Blocked clusters live in one run-wide `BlockedRegistry` shared by every phase and are listed in the report's
"Blocked" section (docs/ERROR-HANDLING.md).
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import posixpath
import re
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Optional

from sbmig.analyze.framework import NO_FIX_LOOP_NOTE, no_fix_loop_followup
from sbmig.build.log_parser import BUILD_EXTENSION_RE
from sbmig.core.config import Config
from sbmig.core.models import (FLOW_FULL, FLOW_REMEDIATION, GAV, RUN_FLOWS, BlockedItem, BuildError, BuildResult,
                               CaseType, DecisionKind, ErrorKind, IterationRecord, LLMUsage, MigrationPlan, Phase,
                               PhaseResult, PlanStep, ProjectProfile, RunReport, StepKind, asked_model_name,
                               merge_model_names, real_model_name, served_beside, utcnow)
from sbmig.core.stage import stage
from sbmig.core.workspace import Workspace, logs_dir
from sbmig.triage.blocked import TRANSFER_CAUSE_PATTERN

from .blocked_registry import BlockedRegistry, default_locator
from .budget import LoopBudget
from .callee import takes_keyword
from .components import LazyFixer, LazyScriptRunner, MissingComponentError, NullKnowledgeBase, lazy_attr
from .entries import REVIEW_L2, CaseScripts, RunErrors
from .fixloop import NOTES_LOGGED, BuildFixLoop, _restore_build_byproducts, _status_paths, refine_build
from .notes import (answer_notes, fallback_guide, log_major_upgrades, note_line, operator_changes, pom_changed,
                    try_notes)

log = logging.getLogger("sbmig.loop")

# 레시피 id를 찾지 못하면 설정이나 코드 오류다. 빌드로는 고칠 수 없으니 그 단계를 중단한다
_MISSING_RECIPE_RE = re.compile(r"(?i)(recipe(?:s|\(s\))? (?:were )?not found|could not find recipe|no recipe(?:s)? (?:found|named)|"
                                r"unknown recipe|recipe\s+\S+\s+(?:does not exist|not found))")
_PARENT_UNRESOLVABLE_RE = re.compile(r"(?i)non-resolvable parent|parent pom|failed to read artifact descriptor")
_BUILD_EXT_IN_TEXT_RE = BUILD_EXTENSION_RE          # 로그 파서와 같은 정규식을 쓴다
# 사전 점검에서 `mvn -v` 출력의 Java 버전을 읽고, JAVA_HOME 문제로 Maven이 시작하지 못한 경우를 가려낸다
_JAVA_VERSION_RE = re.compile(r"(?i)java version:?\s*\"?(?P<v>[0-9][0-9._+-]*)")
_JAVA_RUNTIME_RE = re.compile(r"(?i)runtime:\s*(?P<home>[^,;]+)")
_JAVA_HOME_PROBLEM_RE = re.compile(r"(?i)JAVA_HOME|UnsupportedClassVersionError|Unable to locate a Java Runtime|"
                                   r"java: (?:command )?not found|'java' is not recognized|no java runtime")
# 의존성을 받지 못한 원인이 프로젝트가 아니라 저장소나 네트워크인지 마무리에서 가려낸다. 플릿은 이 값을 보고 멈춘다
# 수정 루프가 repository_unreachable로 차단하는 전송 실패 문구(TRANSFER_CAUSE_PATTERN)도 포함한다
_INFRA_REPO_RE = re.compile(r"(?i)" + TRANSFER_CAUSE_PATTERN + r"|could not transfer|failed to transfer|connection refused|connection reset|timed out|"
                            r"unknown ?host|no route to host|pkix path building failed|"
                            r"(?:status code|return code is):?\s*(?:401|403|5\d\d)\b|"
                            r"was cached in the local repository")
# 멈춤 요청으로 수정 루프를 일찍 끝낸 실행의 중단 사유. 최종 빌드는 실행했으니 STOP_REQUEST_REASON과는 다르다
STOP_LOOP_REASON = ("stopped by user (stop.request) — the fix loop was left early; the last build shows the tree as it "
                    "was left")
# 부모 pom도 의존성 트리도 읽지 못해서 사내 프레임워크인지 알 수 없을 때의 플래그와 인수인계 문장
FRAMEWORK_UNCHECKED_FLAG = "framework-unchecked"
FRAMEWORK_UNCHECKED_LINE = ("framework check could not run (the parent pom and the dependency tree could not be read): if "
                            "this project uses the in-house framework it must not be migrated in full — confirm before "
                            "using the result")


def _framework_undecided(use: Any) -> bool:
    """사내 프레임워크 검사가 아무것도 찾지 못했고, 부모 pom도 의존성 트리도 읽지 못했으면 True를 반환한다.

    True when nothing was found and both deciding sources are in `unchecked` (`parent: …` and `transitive: …`); a
    transitive lookup switched off or not made (dry run, custom runner) is not "could not be read"."""
    unchecked = [str(u) for u in getattr(use, "unchecked", None) or []]
    return not getattr(use, "found", False) and any(u.startswith("parent:") for u in unchecked) \
        and any(u.startswith("transitive:") for u in unchecked)


# 디스크가 가득 차면 infra:disk 플래그를 붙인다. 플릿은 다음 프로젝트까지 실패시키지 않고 멈춘다
# "디스크 공간이 부족"은 한국어 Windows의 문구다
_INFRA_DISK_RE = re.compile(r"(?i)no space left on device|not enough space on the disk|disk quota exceeded|"
                            r"디스크 공간이 부족|ENOSPC")
_DEFAULT_MODE = {StepKind.MSA_POM: "msa_pom", StepKind.RESTRUCTURE: "layout"}
# 사내 프레임워크 프로젝트에서 재구성 없이 JSP 이름 변경만 실행하는 단계의 모드
JSP_JAKARTA_MODE = "jsp_jakarta"
REDUCED_SCOPE = "jdk-jakarta"
COMPILE_GOAL = "clean compile"            # transform.compile에서 재구성 전에 한 번 컴파일할 때의 goal


def step_stage(step: PlanStep) -> str:
    """계획 단계의 로그 태그를 반환한다. 로그 줄마다 ``[rewrite]``처럼 붙는다.

    One of rewrite / compile / restructure / review / build / fix."""
    k = step.kind
    if k == StepKind.OPENREWRITE:
        return "rewrite"
    if k == StepKind.BUILD:
        return "build" if step.phase == Phase.VERIFY else "compile"
    if k in (StepKind.RESTRUCTURE, StepKind.MSA_POM):
        # JSP 이름 변경 단계는 jakarta 변환의 일부이므로 rewrite 태그를 붙인다
        return "rewrite" if step.params.get("mode") == JSP_JAKARTA_MODE else "restructure"
    if k == StepKind.REVIEW:
        return "review"
    return "fix" if k == StepKind.BUILD_FIX_LOOP else step.phase.value
# 선택 아티팩트 버전 올리기는 실패해도 후속 조치로만 남기고 재구성을 계속한다
NON_FATAL_RESTRUCTURE_MODES = frozenset({"optional_bump"})


def _first_sentence(text: str, limit: int = 160) -> str:
    """LLM 분석의 첫 문장을 ``limit``자까지 잘라서 반환한다. 인수인계 메모에 쓴다."""
    t = " ".join((text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    t = m.group(1) if m else t
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


class _RunAborted(Exception):
    """실행을 멈출 때 던지는 내부 예외 클래스. 남은 단계와 검증 빌드는 건너뛰지만 보고서는 남긴다.
    Maven이 없거나 parent를 찾을 수 없을 때처럼, 빌드해도 같은 이유로 실패할 때만 쓴다."""


def parse_renames(out: str) -> dict[str, str]:
    """git diff 출력에서 이름이 바뀐 파일만 골라 {이전 경로: 새 경로}로 반환한다.
    입력은 ``git diff --name-status -M -z`` 출력이다. 재구성으로 옮겨진 파일도 차단 목록이 알아보게 하는 데 쓴다.

    Only `R<score>` entries count; `C` (copy) entries are skipped with their two paths."""
    toks = out.split("\0")
    moves: dict[str, str] = {}
    i = 0
    while i < len(toks):
        st = toks[i].strip()
        if not st:
            i += 1
            continue
        if st[0] in "RC" and i + 2 < len(toks):          # 이름 변경과 복사는 뒤에 경로가 두 개 온다
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
    """``--stop-after`` 값을 단계로 바꾼다. 대소문자를 무시하고 ``report`` 앞 단계만 받는다.
    None이나 빈 문자열이면 끝까지 실행하도록 None을 반환한다.
    잘못된 값이면 ValueError를 던진다.

    Shared by the CLI, the web UI and `sbmig.api.migrate`. The ValueError names the accepted values."""
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


def parse_flow(value: Any) -> str:
    """실행 방식 값(``--flow``)을 ``RUN_FLOWS`` 중 하나로 바꾼다. 대소문자와 앞뒤 공백은 무시한다.
    None이나 빈 문자열이면 ``full``을 반환한다. 모르는 값이면 ValueError를 던진다."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return FLOW_FULL
    text = str(value).strip().lower()
    if text not in RUN_FLOWS:
        raise ValueError(f"flow must be one of: {', '.join(RUN_FLOWS)}")
    return text


def check_flow(flow: Any, stop_after: Phase | None = None, dry_run: bool = False) -> str:
    """실행 방식과 다른 옵션을 함께 쓸 수 있는지 검사하고, 정리한 실행 방식을 반환한다.
    ``remediation``에는 이관 단계가 없다. ``stop_after``나 ``dry_run``과 함께 쓰면 ValueError를 던진다."""
    f = parse_flow(flow)
    if f == FLOW_REMEDIATION and stop_after is not None:
        raise ValueError("--stop-after cannot be combined with --flow remediation (the remediation flow runs no "
                         "migration phases)")
    if f == FLOW_REMEDIATION and dry_run:
        raise ValueError("--dry-run cannot be combined with --flow remediation (the remediation builds and scans the "
                         "project; it has no dry-run mode)")
    return f


class _BudgetedLLM:
    """호출할 때마다 실행 예산(``LoopBudget``)에서 1회를 차감하는 LLM 클라이언트 래퍼 클래스.
    구조 검토도 수정 루프와 같은 예산을 쓰게 하는 데 쓴다. 모델별 사용량은 ``sink``에 모아 보고서 비용 표에 쓴다."""

    def __init__(self, inner: Any, budget: LoopBudget, sink: Optional[dict[str, LLMUsage]] = None,
                 default_model: Optional[str] = None):
        self._inner, self._budget, self._sink = inner, budget, sink
        self._default_model = default_model

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        """LLM을 한 번 호출하고 모델별 토큰 사용량을 모은다.
        예산은 호출 전에 차감한다. 그래서 실패한 호출도 1회로 센다."""
        self._budget.charge_llm(1)
        res = self._inner.complete(*args, **kwargs)
        if self._sink is not None:
            # 이름은 요청한 모델이고, 응답이 알려 준 모델은 served로 남긴다
            model = asked_model_name(getattr(res, "model", ""), kwargs.get("model"), getattr(self._inner, "model", ""),
                                     self._default_model)
            u = LLMUsage(model=model, calls=1, input_tokens=int(getattr(res, "input_tokens", 0) or 0),
                         output_tokens=int(getattr(res, "output_tokens", 0) or 0),
                         served=served_beside(model, list(getattr(res, "served_models", None) or [])))
            self._sink[model] = self._sink.get(model, LLMUsage(model=model)).add(u)
        return res

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


STRAY_POM_TAIL = ("treated as a stray copy (an IDE output / backup folder): not built, not migrated, left where it is; "
                  "delete it from the source, or give it its own coordinates and list it in <modules> if it is a real module")


def ant_refusal_detail(build_file: str, flow: str = FLOW_FULL) -> str:
    """Ant 프로젝트를 거부한 기술적 이유를 한 줄로 만든다. 어느 build.xml을 봤고 왜 변환하지 않는지 담는다.
    후속 조치의 둘째 줄과 저널 ``build_tool.detail``에 쓴다.
    사람에게 보이는 거부 문구는 ``intake.ant_message``다."""
    why = ("the remediation flow builds the project as uploaded and needs a Maven project" if flow == FLOW_REMEDIATION
           else "the Ant → Maven conversion is switched off (intake.ant_to_maven: false)")
    return f"Ant build file {build_file} found and no pom.xml: {why}"


def stray_pom_followup(st: Any) -> str:
    """다른 모듈을 통째로 복사해 둔 폴더 하나에 대한 인수인계 문장을 만든다.
    이 문장은 ``sbmig/report/i18n.py``의 ``PATTERNS``가 한국어로 바꾼다. 문장을 고치면 그쪽도 고친다."""
    return f"pom copy ignored: {st.why()} — {STRAY_POM_TAIL}"


class Orchestrator:
    """프로젝트 하나의 이관을 실행하는 클래스. ``step_*`` 단계를 차례로 실행하고 마지막에 반드시 보고서를 남긴다.
    분석기, Maven, fixer, KB, LLM 같은 협력 객체는 처음 쓸 때 만든다.
    실행 전체가 함께 쓰는 객체는 ``budget``, ``blocked``(차단 목록), ``errors``(오류 목록), ``loop``(수정 루프)다."""
    def __init__(self, cfg: Config, ws: Workspace, *, analyzer: Optional[Callable] = None,
                 planner: Optional[Callable] = None,
                 restructurer: Any = None, rewrite_runner: Any = None, build_runner: Any = None,
                 triager: Any = None, fixer: Any = None, kb: Any = None, llm: Any = None,
                 report_writer: Optional[Callable] = None, clock: Callable[[], float] = time.monotonic,
                 locator: Any = None, effective_capture: Optional[Callable] = None,
                 javax_inventory: Any = None, script_runner: Any = None, reviewer: Any = None,
                 error_list: Optional[Callable[..., Any]] = None, edit_applier: Optional[Callable[..., Any]] = None,
                 source: Any = None, remediation_components: Any = None):
        """오케스트레이터를 만든다. 협력 객체는 모두 주입할 수 있고, None이면 실제 구현을 처음 쓸 때 만든다.
        ``build_runner``를 직접 주면 실제 Maven이 아닌 것으로 본다.
        그때는 mvn을 실행하는 classpath 분석과 effective pom 캡처를 끈다.

        `script_runner`: ScriptRunnerLike (None = `sbmig.script.ScriptRunner(cfg)`). `reviewer`: a
        StructureReviewerLike or a factory `fn(cfg, llm, runner, kb)` (None =
        `sbmig.review.StructureReviewer`). `error_list`: `fn(ws, cfg, project)` -> the run's error list (None
        = `sbmig.errors.entries.ErrorList. for_workspace`). `edit_applier`: `fn(ws, edits, *, cfg,
        errors=None) -> ScriptRunResult` for the LLM's direct edits (`errors` only when the signature takes
        it; None = `sbmig.script.apply_edits`). Flow `remediation`: `source` = the uploaded ZIP or directory
        (None = read from the `intake` journal line), `remediation_components` = the remediation's
        `Components` (None = the real ones)."""
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
        self.errors = RunErrors(None)               # 실행의 오류 목록(errors.jsonl). step_plan에서 정한다
        self._change_base = ""                      # 자동 변환 직후 커밋. 보고서의 코드 수정 내역이 여기서 시작한다
        self._verify_results: list[BuildResult] = []
        self._rewrite_failures: list[tuple[str, list[str], str]] = []   # 실패한 변환 단계, 레시피 세트, 원인
        self._review_usage: dict[str, LLMUsage] = {}       # 구조 검토의 모델별 LLM 사용량
        self._review_commit_pre: str | None = None          # 구조 검토 커밋 직전의 HEAD
        self._review_guide_source = ""                     # 구조 검토 안내를 쓴 쪽, "llm" 또는 "sbmig"
        self._restructure_results: list[Any] = []          # 이번 실행의 RestructureResult 목록, 구조 검토의 입력
        self._restructure_modes: list[Optional[str]] = []  # 결과마다의 모드(layout, boot_pom, msa_pom)
        self._restructure_notes: list[str] = []
        self._phase_failed = False
        self._dry_run = False
        self._pending_notes: list[str] = []
        # 차단 목록은 실행 전체에 하나만 둔다. 어느 루프에서 차단한 오류든 다른 루프와 마무리 단계가 함께 본다
        self.blocked = BlockedRegistry()
        # 직접 준 빌드 실행기는 실제 Maven이 아니다. mvn을 띄우는 기능은 끄고, 켜려면 locator / effective_capture를 준다
        self._real_maven = build_runner is None
        self._locator = locator
        self._effective_capture = effective_capture
        # report.javax_api_inventory용 함수. None이면 실제 Maven일 때만 실제 구현을 쓰고, False면 끈다
        self._javax_inventory = javax_inventory
        self._running = False
        self.abort_reason: str | None = None   # 이번 실행에서 처음 `_abort`한 사유
        self._run_steps: list[PlanStep] = []   # step_plan이 덧붙인 verify.build까지 포함한 실행 단계
        self._verify_done: list[str] = []      # 최종 빌드 결과가 나온 verify 단계 id
        self._parse_problem_files: set[str] = set()   # 변환되지 않은 파일로 이미 알린 소스 파일
        self._flow = FLOW_FULL                 # 현재 실행 방식(full 또는 remediation). _run_guard가 정한다
        self._source = source                  # remediation 실행의 입력 ZIP이나 폴더. None이면 저널의 intake 줄에서 읽는다
        self._remediation_components = remediation_components

    # ------------------------------------------------------------------ 협력 객체(처음 쓸 때 생성)
    @property
    def analyzer(self) -> Callable:
        """분석기(``sbmig.analyze.analyze_project``)를 반환한다. 처음 쓸 때 import한다."""
        if self._analyzer is None:
            self._analyzer = lazy_attr("sbmig.analyze", "analyze_project", "A (analyze)")
        return self._analyzer

    @property
    def planner(self) -> Callable:
        """계획기(``sbmig.plan.build_plan``)를 반환한다. 처음 쓸 때 import한다."""
        if self._planner is None:
            self._planner = lazy_attr("sbmig.plan", "build_plan", "A (plan)")
        return self._planner

    @property
    def script_runner(self) -> Any:
        """수정 루프와 구조 검토가 함께 쓰는 스크립트 실행기(``sbmig.script``)를 반환한다. 처음 쓸 때 만든다."""
        if self._script_runner is None:
            self._script_runner = LazyScriptRunner(
                lambda: lazy_attr("sbmig.script", "ScriptRunner", "S (script)")(self.cfg))
        return self._script_runner

    @property
    def restructurer(self) -> Any:
        """프로젝트를 회사 레이아웃으로 바꾸는 재구성 객체를 반환한다(``sbmig.restructure.Restructurer``)."""
        if self._restructurer is None:
            self._restructurer = lazy_attr("sbmig.restructure", "Restructurer", "B (restructure)")()
        return self._restructurer

    @property
    def rewrite_runner(self) -> Any:
        """OpenRewrite 실행기를 반환한다(``sbmig.rewrite.OpenRewriteRunner``)."""
        if self._rewrite_runner is None:
            cls = lazy_attr("sbmig.rewrite", "OpenRewriteRunner", "C (rewrite)")
            self._rewrite_runner = cls(self.ws, self.cfg, self.kb)
        return self._rewrite_runner

    @property
    def build_runner(self) -> Any:
        """Maven 실행기(``sbmig.build.MavenRunner``)를 반환한다."""
        if self._build_runner is None:
            self._build_runner = lazy_attr("sbmig.build", "MavenRunner", "C (build)")(self.ws, self.cfg)
        return self._build_runner

    @property
    def triager(self) -> Any:
        """트리아지 객체(``sbmig.triage.Triager``)를 반환한다.
        빌드 오류를 케이스로 묶고, 어느 단계에서 처리할지와 차단 정책을 정한다."""
        if self._triager is None:
            from sbmig.triage import Triager
            self._triager = Triager(self.cfg)
        return self._triager

    @property
    def kb(self) -> Any:
        """파일 KB(``sbmig.kb.FileKnowledgeBase``)를 반환한다.
        import할 수 없으면 경고를 남기고 ``NullKnowledgeBase``를 쓴다.
        그때도 실행은 계속되고, 저장된 스크립트만 재사용하지 않는다."""
        if self._kb is None:
            try:
                self._kb = lazy_attr("sbmig.kb", "FileKnowledgeBase", "F (kb)")(self.cfg)
            except MissingComponentError as e:
                log.warning("%s — running without a knowledge base", e)
                self.ws.journal({"event": "warning", "message": str(e)})
                self._kb = NullKnowledgeBase()
        return self._kb

    def llm_client(self) -> Any:
        """LLM 클라이언트(``sbmig.fix.make_llm_client``)를 반환한다. API 키는 환경 변수에서만 읽는다."""
        if self._llm is None:
            self._llm = lazy_attr("sbmig.fix", "make_llm_client", "D (fix)")(self.cfg)
        return self._llm

    @property
    def fixer(self) -> Any:
        """수정 스크립트를 만드는 fixer를 반환한다. 실제 객체는 첫 요청 때 만든다(``LazyFixer``).
        ``fix.plugin``이 있으면 사내 코딩 에이전트 플러그인을 쓴다. 없으면 ``sbmig.fix.LLMFixer``다.

        Plugin = tier 2 (`resolve_fixer_plugin`, docs/ADAPTERS.md); it may run without an sbmig LLM client."""
        if self._fixer is None:
            def make() -> Any:
                spec = getattr(getattr(self.cfg, "fix", None), "plugin", None)
                if spec:
                    try:
                        llm = self.llm_client()
                    except Exception as e:                 # 사내 에이전트는 우리 LLM이 없어도 동작할 수 있다
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

    # ------------------------------------------------------------------ 실행
    def run(self, stop_after: Phase | None = None, dry_run: bool = False, flow: str = FLOW_FULL) -> RunReport:
        """이관을 한 번 실행하고 보고서를 반환한다. 엔진은 ``loop.engine``의 ``graph``(기본)나 ``inline``이다.
        ``flow="remediation"``이면 접수 -> 취약점 조치 -> 보고서 순서로 실행한다.
        ``stop_after``나 ``dry_run``과 함께 쓰면 ValueError를 던진다.

        Both engines call the same `step_*` methods inside the same `_run_guard` (abort / missing-component /
        internal-error handling + `_finalize`), so reports, journals and exit codes are identical apart from
        the graph's additive ``graph_node`` journal events. Graph = `sbmig.graph.run_graph`, inline =
        `_run`."""
        flow = check_flow(flow, stop_after, dry_run)
        with self._run_guard(dry_run, flow=flow) as report:
            if self.refused_at_intake(report):
                pass                                # 접수에서 거절한 작업 공간은 아무 단계도 실행하지 않는다
            elif self.engine == "graph":
                from sbmig.graph import run_graph
                run_graph(self, stop_after=stop_after, dry_run=dry_run, flow=flow)
            elif flow == FLOW_REMEDIATION:
                self._run_remediation_flow(report)
            else:
                self._run(report, stop_after, dry_run)
        return report

    @property
    def flow(self) -> str:
        """현재 또는 마지막 실행의 실행 방식(``full``, ``remediation``)을 반환한다."""
        return self._flow

    @property
    def engine(self) -> str:
        """``loop.engine`` 값(graph, inline)을 반환한다. 비어 있으면 inline이다."""
        return getattr(getattr(self.cfg, "loop", None), "engine", "inline") or "inline"

    @property
    def running(self) -> bool:
        """``_run_guard``가 열려 있는지 반환한다. 실행 중이면 ``self.report``가 그 실행의 보고서다."""
        return self._running

    @contextmanager
    def _run_guard(self, dry_run: bool = False, flow: str = FLOW_FULL) -> Iterator[RunReport]:
        """새 RunReport로 실행을 시작하고, 무슨 일이 있어도 보고서를 남기고 끝낸다.
        - ``_RunAborted``는 조용히 끝낸다
        - 필요한 패키지가 없거나 다른 예외가 나면 중단으로 기록하고 최종 빌드를 시도한다
        - 어떤 경우에도 ``_finalize``(보고서, KB 기록, 산출물)를 실행한다

        Used by `run()` for both engines and by `sbmig.graph.run_graph` / `stream_graph` when called directly.
        A second guard while one is open raises RuntimeError."""
        if self._running:
            raise RuntimeError("Orchestrator run already in progress")
        self._running = True
        self._dry_run = dry_run
        self._flow = parse_flow(flow)
        self.abort_reason = None
        # 같은 객체로 두 번 실행해도 앞 실행의 결과가 보이지 않도록 실행별 상태를 비운다
        self._verify_results = []
        self._rewrite_failures = []
        self._verify_done = []
        self._parse_problem_files = set()
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
        self._change_base = ""
        t0 = self.clock()
        report = RunReport(run_id=self.ws.run_id, project_name=self.ws.root.name, case_type=CaseType.UNKNOWN,
                           started_at=utcnow())
        self.report = report
        if dry_run:
            report.flags.append("dry-run")
        if self._flow == FLOW_REMEDIATION:
            # 웹 화면이 단계 표시를 고를 수 있도록 첫 저널 줄에 실행 방식을 남긴다
            report.flow = self._flow
            self.ws.journal({"event": "flow", "flow": self._flow})
            # 분석 단계가 없으면 이름이 작업 폴더 이름으로 남는다. 웹에서 입력한 프로젝트명을 보고서 제목에 쓴다
            report.project_name = self._web_project_name() or report.project_name
        try:
            yield report
        except _RunAborted:
            pass
        except MissingComponentError as e:
            log.error("%s", e)
            self._abort(report, f"missing component: {e}")
            with stage("build"):
                self._final_build_after_abort(report)
        except Exception as e:  # 버그가 있어도 보고서는 남긴다
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
                self._run_hook("teardown", report)                 # 보고서를 쓴 뒤 결과와 상관없이 실행한다

    def _run_hook(self, which: str, report: RunReport) -> bool:
        """``build.setup_command``나 ``build.teardown_command``를 셸 없이 한 번 실행한다.
        setup이 실패하면 빌드할 수 없으니 실행을 중단하고 False를 반환한다.
        teardown 실패는 경고만 남긴다. 명령이 비어 있으면 True를 반환한다.

        Environment adds SBMIG_RUN_ID / SBMIG_RUN_DIR / SBMIG_PROJECT_DIR; timeout `build.hook_timeout_s`
        (600). Journal `build_hook`."""
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
        except Exception:  # noqa: BLE001 - 저널 문제로 실행 결과가 바뀌지 않게 한다
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
        """이 실행의 최종 빌드 단계 목록을 반환한다. 루트 aggregator가 없는 MSA는 서비스마다 하나씩이다.
        계획에 빌드 단계가 없어도 ``loop.always_final_verify``가 켜져 있으면 ``verify.build`` 하나를 반환한다.

        The synthesized `verify.build` matches what `step_plan` appends."""
        steps = list(self._run_steps or (self.plan.steps if self.plan is not None else []))
        out = [s for s in steps if s.kind == StepKind.BUILD and s.phase == Phase.VERIFY]
        if not out and self.plan is not None and self.cfg.loop.always_final_verify:
            out = [PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY, title="final mvn clean package",
                            build_goal="clean package", reason="always_final_verify")]
        return out

    def _verify_missing(self) -> list[PlanStep]:
        return [s for s in self._verify_steps() if s.id not in self._verify_done]

    def _final_build_after_abort(self, report: RunReport) -> None:
        """계획을 세운 뒤에 내부 오류나 빠진 패키지로 중단된 실행도 최종 ``mvn clean package``를 시도한다.
        그래야 report.md에 프로젝트에 남은 실제 오류가 나온다. 수정 루프는 실행하지 않는다.
        Maven이 없거나 parent를 찾을 수 없어 일부러 멈춘 실행은 빌드하지 않는다. 같은 이유로 실패하기 때문이다.

        Invariant 1 (build gate). Every final-build step without a result runs: an MSA run whose svc-a verify
        passed before svc-b's step crashed still builds svc-b."""
        if self._dry_run or self.plan is None or not self.cfg.loop.always_final_verify:
            return
        steps = self._verify_missing()
        if not steps:
            return
        self._after_abort = True              # 방금 실패한 실행 위에 수정 루프를 더 돌리지 않고 빌드만 한다
        pr = PhaseResult(phase=Phase.VERIFY, step_ids=[s.id for s in steps], ok=False)
        pr.notes.append("final build after the run aborted (best effort: earlier phases are incomplete)")
        self.ws.journal({"event": "phase_start", "phase": Phase.VERIFY.value, "steps": pr.step_ids,
                         "after_abort": True})
        try:
            for s in steps:
                self._step_verify(pr, s)
        except Exception as e:   # 빌드 실행기 자체가 실패해도 중단 보고서는 지킨다
            log.warning("final build after the abort failed: %s", e)
            pr.notes.append(f"final build failed: {type(e).__name__}: {e}")
        pr.ok = pr.build_ok = bool(self._verify_results) and all(r.ok for r in self._verify_results)
        pr.finished_at = utcnow()
        report.phases.append(pr)
        self.ws.journal({"event": "phase_end", "phase": Phase.VERIFY.value, "ok": pr.ok, "build_ok": pr.build_ok,
                         "iterations": pr.iterations, "attempts": 0, "remaining_errors": len(pr.remaining_errors),
                         "blocked": [], "after_abort": True})

    def build_succeeded(self, report: RunReport) -> bool:
        """이관이 성공했는지 판단한다. ``report.success``가 이 값이다.
        계획한 최종 빌드가 모두 성공해야 한다. 재구성 실패, 소스 없는 reactor, 중단도 없어야 한다.
        그래프의 ``remediation`` 노드는 ``_finalize``보다 먼저 실행되므로 수정 루프의 플래그도 함께 본다.

        Shared by `_finalize` and the graph's `remediation` node."""
        flags = list(report.flags) + (list(self.loop.flags) if self.loop is not None else [])
        # 변환만 하는 실행(프레임워크 프로젝트)은 변환이 실패하면 빌드가 통과해도 성공이 아니다
        return bool(self._verify_results) and all(r.ok for r in self._verify_results) \
            and "restructure-failed" not in flags and "hollow-reactor" not in flags \
            and not (self.reduced_scope and self.conversion_failed) \
            and not _aborted(flags) and not self._verify_missing()

    @property
    def conversion_failed(self) -> bool:
        """OpenRewrite 변환이 실패한 채로 실행이 계속됐으면 True를 반환한다. 플래그는 ``rewrite-failed``다.

        v2.1z-ay"""
        return bool(self._rewrite_failures)

    @property
    def final_build_failed(self) -> bool:
        """계획한 최종 빌드 가운데 실패한 것이 있는지 반환한다."""
        return any(not r.ok for r in self._verify_results)

    def refused_at_intake(self, report: RunReport) -> bool:
        """작업 공간이 접수에서 거절되었으면 그 사유로 실행을 중단하고 True를 반환한다.
        압축 해제 한도를 넘은 ZIP처럼 ``ws.intake_refusal``이 있는 경우다.
        같은 사유로 이미 중단했으면 다시 기록하지 않는다.

        Same record as the other intake refusals (`run aborted: <reason>`, flag `aborted`, exit 3), before
        preflight, Maven, the LLM or any intake edit. Called first inside the run guard by `run()` (both
        engines, every flow) and by `sbmig.graph.run_graph` / `stream_graph` called directly; the CLI's
        `refuse_intake` may have aborted first."""
        why = getattr(self.ws, "intake_refusal", None)
        if not why:
            return False
        why = str(why)
        if self.abort_reason != why:
            self._abort(report, why)
        return True

    def _abort(self, report: RunReport, why: str) -> None:
        """실행 중단을 기록한다. ``aborted`` 플래그, 후속 조치, ERROR 로그 한 줄, 저널을 남긴다."""
        if "aborted" not in report.flags:
            report.flags.append("aborted")
        report.manual_followups.append(f"run aborted: {why}")
        if self.abort_reason is None:
            self.abort_reason = why
        log.error("run aborted: %s", why)          # 운영자가 grep으로 찾는 한 줄
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
        """LangGraph 없이 같은 단계 순서로 실행한다. ``loop.engine: inline``일 때 쓴다."""
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
        if self.late_stop(report) == "aborted":
            return
        self.step_finish(report, stop_after)
        self._note_inline_remediation(report, stop_after, dry_run)

    def _run_remediation_flow(self, report: RunReport) -> None:
        """inline 엔진에서 ``remediation`` 실행 방식을 실행한다.
        프로젝트를 바꾸지 않는 접수 -> 취약점 조치 순서다.
        보고서는 ``_run_guard``의 마무리 단계가 쓴다.

        Mirrors the graph's intake -> remediation -> finalize nodes."""
        if not self.step_intake(report, None):
            return
        if self._stop_file_abort(report, "remediation"):
            return
        self.step_remediation(report)

    def _note_inline_remediation(self, report: RunReport, stop_after: Phase | None, dry_run: bool) -> None:
        # 이관 뒤 취약점 조치는 그래프 엔진에서만 이어서 실행한다
        # inline 엔진에서는 그래프였다면 실행했을 때만 건너뛰었다고 한 줄 남긴다
        rem = getattr(self.cfg, "remediation", None)
        if rem is None or not bool(getattr(rem, "after_migration", False)):
            return
        if dry_run or stop_after is not None or "aborted" in report.flags:
            return
        if not self.build_succeeded(report) and not self.remediation_after_failed_build(report):
            return
        try:
            self.ws.journal({"event": "remediation_skipped", "reason": "inline_engine", "scope": "remediation"})
        except Exception:  # noqa: BLE001 - 저널 한 줄 때문에 실행 결과가 바뀌지 않게 한다
            log.debug("remediation_skipped journal line not written", exc_info=True)
        report.remediation = {"status": "skipped", "reason": "inline_engine", "dir": "remediation"}
        log.info("vulnerability remediation not chained: loop.engine is inline (the graph engine chains it)")

    def _stop_file_abort(self, report: RunReport, before: str) -> bool:
        """노드 사이에서 실행 폴더에 ``stop.request`` 파일이 있으면 실행을 중단하고 True를 반환한다.
        그래프 엔진의 ``user_stop``처럼 보고서를 aborted로 표시하고 최종 빌드는 하지 않는다."""
        fn = getattr(self.ws, "stop_requested", None)
        if not callable(fn) or not fn():
            return False
        from sbmig.core.workspace import STOP_REQUEST_REASON
        log.warning("stop requested (stop.request) — stopping before %s (no final build)", before)
        self.ws.journal({"event": "stop_request", "node": before.split()[0], "file": "stop.request"})
        if "aborted" not in report.flags:
            self._abort(report, STOP_REQUEST_REASON)
        return True

    def late_stop(self, report: RunReport) -> str:
        """finalize 직전에 ``stop.request``를 처리하고 결과를 반환한다. 두 엔진이 함께 쓴다.
        - 파일이 없으면 빈 문자열을 반환한다
        - 아직 결과가 없는 최종 빌드가 있으면 ``STOP_REQUEST_REASON``으로 중단한다. ``aborted``를 반환한다
        - 수정 루프가 멈춤 요청으로 끝났으면 ``STOP_LOOP_REASON``으로 중단한다. ``aborted``를 반환한다
        - 최종 빌드가 모두 끝났으면 요청을 무시하고 ``ignored``를 반환한다

        On "ignored" the run finishes as what it is (journal `stop_request` with `ignored`); a chained
        remediation still skips itself because the file is present."""
        fn = getattr(self.ws, "stop_requested", None)
        if not callable(fn) or not fn():
            return ""
        if not self._verify_results or self._verify_missing():
            self._stop_file_abort(report, "finalize")
            return "aborted"
        if self.loop is not None and getattr(self.loop, "stopped_by_request", False):
            log.warning("stop requested (stop.request) — the fix loop was left early; the last build stands")
            self.ws.journal({"event": "stop_request", "node": "finalize", "file": "stop.request", "after": "fix loop"})
            if "aborted" not in report.flags:
                self._abort(report, STOP_LOOP_REASON)
            return "aborted"
        log.warning("stop requested (stop.request) after every final build ran — ignored, the run finishes as it is")
        self.ws.journal({"event": "stop_request", "node": "finalize", "file": "stop.request",
                         "ignored": "every final build ran"})
        return "ignored"

    # ------------------------------------------------------------------ 단계(두 엔진 공용)
    def step_intake(self, report: RunReport, stop_after: Phase | None) -> bool:
        """접수 단계를 실행한다. 여기서 멈추거나 중단하면 False를 반환한다.
        git 스냅샷, 사전 점검(Maven, JDK 21, parent 설정), Maven 프로젝트인지 확인, aggregator 생성,
        as-is effective dependencyManagement 캡처를 한다."""
        # setup 훅은 실행 맨 처음, 가드 안에서 실행한다. 실패해도 다른 중단처럼 보고서와 teardown이 남는다
        if not self._dry_run and not self._run_hook("setup", report):
            raise _RunAborted()
        with stage("intake"):
            return self._step_intake(report, stop_after, edits=self._flow != FLOW_REMEDIATION)

    def _step_intake(self, report: RunReport, stop_after: Phase | None, *, edits: bool = True) -> bool:
        """접수 단계의 본문이다.
        ``edits=False``(remediation 실행)면 프로젝트를 바꾸는 작업은 건너뛴다.
        그때는 설정 경고, 사전 점검, 빌드 도구 확인만 한다.

        With `edits=False` nothing writes into the project or calls Maven on it."""
        t = self.clock()
        if edits:
            self.ws.git_init()
        # 설정 경고는 설정을 읽을 때 콘솔에만 나간다. 실행 로그에 한 번 더 남긴다
        for line in getattr(self.cfg, "_config_warnings", None) or []:
            log.warning("%s", line)
        setter = getattr(self.ws, "set_encoding_candidates", None)
        if callable(setter):
            setter(list(self.cfg.build.encoding_candidates))
        self._preflight(report)
        if "settings-not-found" in report.flags:
            self._abort(report, self._settings_problem)
            return False
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
        if not self._check_build_tool(report, edits=edits):
            self._timed("intake", t)
            return False
        if not edits:
            self._intake_project_settings(report)   # 취약점 조치만 하는 실행도 빌드하므로 .mvn 안내를 남긴다
            self._timed("intake", t)
            return True
        if not self._intake_layout(report):
            self._timed("intake", t)
            return False
        self._intake_parent_extensions(report)    # 아래 캡처가 첫 Maven 호출이므로 그 전에 확인한다
        self._intake_project_settings(report)
        self._capture_effective(report)
        self._timed("intake", t)
        return not self._stop(stop_after, Phase.INTAKE)

    def _intake_parent_extensions(self, report: RunReport) -> None:
        """사내 부모 pom 체인의 ``<build><extensions>``를 받을 수 있는지 첫 Maven 호출 전에 확인한다.
        상속된 확장은 자식 pom에서 뺄 수 없다. Maven은 어떤 goal이든 먼저 이 확장을 받아야 한다.
        - 저장소에는 있는데 로컬에 오래된 ``*.lastUpdated`` 표식만 있으면 그 표식을 지운다
        - 어디서도 받을 수 없으면 경고와 플래그 ``parent-extension-unresolvable``, 인수인계 한 줄을 남긴다
        실패해도 예외를 던지지 않는다.

        Uses `sbmig.build.parent_extensions.check_parent_build_extensions`; journal
        `parent_build_extensions`."""
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
                        # sbmig는 build.repository_url에서 읽고 Maven은 settings 미러에서 받는다.
                        # 그래서 sbmig가 찾은 확장도 Maven에서는 404가 날 수 있다
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
        except Exception as e:  # noqa: BLE001 - 사전 점검이 실패해도 실행은 계속한다
            log.warning("parent build extension pre-check failed: %s: %s", type(e).__name__, e)

    def _check_build_tool(self, report: RunReport, *, edits: bool = True) -> bool:
        """Maven 프로젝트인지 확인한다. Gradle이거나 빌드 파일이 없으면 Maven 호출 전에 중단한다(exit 3).
        Ant 프로젝트는 ``intake.ant_message``로 거부한다.
        ``intake.ant_to_maven``이 켜져 있고 프로젝트를 바꿀 수 있는 실행이면 pom.xml을 만들어서 계속한다.

        A failed synthesis is refused with its reason. `step_analyze` repeats the check on
        `profile.build_tool` as a fallback."""
        try:
            from sbmig.analyze import detect_build_tool
        except ImportError:  # pragma: no cover - 분석 패키지가 없어도 step_analyze에서 다시 확인한다
            return True
        det = detect_build_tool(self.ws)
        intake = getattr(self.cfg, "intake", None)
        if det.tool == "ant":
            if edits and intake is not None and getattr(intake, "ant_to_maven", False):
                return self._synthesize_ant_pom(report, det)
            return self._refuse_ant(report, det)
        if det.tool != "maven":
            report.flags.append(f"unsupported-build-tool:{det.tool}")
            self._abort(report, det.reason)
            self.ws.journal({"event": "build_tool", "tool": det.tool, "ok": False, "reason": det.reason})
            return False
        self.ws.journal({"event": "build_tool", "tool": "maven", "ok": True})
        return True

    def _refuse_ant(self, report: RunReport, det: Any) -> bool:
        """Ant 프로젝트를 거부한다. 플래그 ``unsupported-build-tool:ant``를 붙이고 exit 3으로 끝난다. 파일은 쓰지 않는다.
        화면의 중단 사유는 ``intake.ant_message`` 그대로다.
        어느 build.xml을 봤는지는 그다음 후속 조치 줄과 저널에만 남긴다."""
        intake = getattr(self.cfg, "intake", None)
        message = str(getattr(intake, "ant_message", "") or "").strip() or det.reason
        build_file = getattr(det, "ant_build_file", None) or "build.xml"
        detail = ant_refusal_detail(build_file, self._flow)
        report.flags.append("unsupported-build-tool:ant")
        self._abort(report, message)
        if detail not in report.manual_followups:
            report.manual_followups.append(detail)
        log.info("접수: %s", detail)
        self.ws.journal({"event": "build_tool", "tool": "ant", "ok": False, "reason": message, "detail": detail,
                         "build_file": build_file})
        return False

    def _synthesize_ant_pom(self, report: RunReport, det: Any) -> bool:
        """build.xml과 폴더 구조를 보고 pom.xml을 만든다. ``intake: pom.xml synthesized from build.xml``로 커밋한다.
        as-is 스냅샷 뒤, effective-pom 캡처 앞에 실행해야 한다.
        그래야 changes.patch에 pom.xml이 추가된 파일로 보이고, 캡처가 그 pom을 쓸 수 있다.
        만들지 못하면 ``unsupported-build-tool:ant``로 중단한다(exit 3).

        `sbmig.analyze.ant.synthesize_pom` writes the file only. Flags `pom-synthesized` (+
        `jar-coordinates-unknown` when unknown jars were declared / left out); result on
        `report.pom_synthesis`. Any failure (module missing, exception, `ok=False`) is the rejection."""
        build_file = getattr(det, "ant_build_file", None) or "build.xml"
        try:
            # 테스트가 sys.modules에 가짜 모듈을 넣을 수 있도록 호출할 때마다 importlib로 찾는다
            synthesize = importlib.import_module("sbmig.analyze.ant").synthesize_pom
        except (ImportError, AttributeError) as e:
            return self._reject_ant(report, f"Ant build.xml detected and no pom.xml: the pom synthesis "
                                            f"(sbmig.analyze.ant) is not available in this installation "
                                            f"({type(e).__name__}: {e}) — convert the build to Maven first")
        try:
            res = synthesize(self.ws, self.cfg)
        except Exception as e:  # 분석기에 버그가 있어도 보고서를 남기고 거부로 끝낸다

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
        # 운영자가 화면을 보고 옮겨 쓰는 한 줄. manifest에서 찾은 좌표는 참고용이라 미상으로 센다
        log.info("접수: Ant build.xml → pom.xml 합성 (jar %d개: pom.properties %d · 이름표 %d · 조회 %d · 미상 %d)",
                 len(res.jars), counts.get("pom_properties", 0), counts.get("name_table", 0),
                 counts.get("sha1_lookup", 0), counts.get("unknown", 0) + counts.get("manifest", 0))
        self.ws.journal({"event": "pom_synthesized", "pom": res.pom_path, "build_file": res.build_file,
                         "packaging": res.packaging, "jars": len(res.jars), "unknown": len(res.unknown_jars),
                         "dropped": len(res.dropped_jars), "warnings": len(res.warnings)})
        self.ws.journal({"event": "build_tool", "tool": "ant→maven", "ok": True, "build_file": res.build_file})
        return True

    def _reject_ant(self, report: RunReport, why: str) -> bool:
        """pom.xml 생성에 실패한 Ant 프로젝트를 그 이유로 거부한다.
        플래그 ``unsupported-build-tool:ant``, 중단 사유, 저널 ``build_tool``을 남긴다."""
        report.flags.append("unsupported-build-tool:ant")
        self._abort(report, why)
        self.ws.journal({"event": "build_tool", "tool": "ant", "ok": False, "reason": why})
        return False

    def _intake_layout(self, report: RunReport) -> bool:
        """접수 단계에서 pom 구성을 정리한다. 모두 effective-pom 캡처와 다른 Maven 호출보다 먼저 실행한다.
        - 다른 모듈을 복사해 둔 폴더는 이후 검사에서 뺀다
        - 모듈이 프로젝트에 없는 EAR 루트는 거부한다. EAR에는 코드가 없고, 그 WAR 의존성이 back 모듈로 들어가면 안 된다
        - 루트 pom 없이 서로 의존하는 형제 pom이 있으면 루트 aggregator pom을 만든다
        - reactor에 두 번 포함된 모듈의 중복 ``<module>``을 지운다

        Company shape: APP/BATCH, APP/WEB, APP/EAR without a root pom -> one reactor, merged by the
        restructure (`layout.generate_aggregator`). The stray-pom pass runs first: every later step must see
        the clean pom set."""
        try:
            from sbmig.analyze.aggregator import ear_without_modules
        except ImportError:  # pragma: no cover
            return True
        self._intake_stray_poms(report)          # 이후 단계가 정리된 pom 목록을 보도록 가장 먼저 실행한다
        why = ear_without_modules(self.ws)
        if why:
            report.flags.append("ear-without-modules")
            self._abort(report, why)
            self.ws.journal({"event": "ear_without_modules", "reason": why})
            return False
        if getattr(self.cfg.layout, "generate_aggregator", True):
            self._intake_aggregator(report)
        self._intake_reactor_dedupe(report)
        self._intake_build_extensions(report)     # 받을 수 없는 확장은 모든 Maven 호출을 막는다. 첫 호출 전에 정리한다
        return True

    def _intake_build_extensions(self, report: RunReport) -> None:
        """배포 전송에만 쓰는 build extension을 첫 Maven 호출 전에 모든 pom에서 지운다.
        패키징에는 필요 없는데, 저장소에 없으면 Maven이 ``Unresolveable build extension``으로 멈추기 때문이다.
        지우는 대상은 ``knowledge/libraries/build_extensions.yml`` 목록에 있는 확장이다. 목록에 없으면 기록만 한다.
        지우면 커밋하고 플래그 ``deploy-extensions-removed``와 인수인계 줄을 남긴다. 예외를 던지지 않는다.

        Switch `intake.strip_deploy_extensions`; journal `build_extensions`."""
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
        except Exception as e:  # noqa: BLE001 - 실패해도 Maven이 같은 오류를 보고하니 그대로 계속한다
            log.warning("build extension check failed: %s: %s", type(e).__name__, e)

    def _intake_project_settings(self, report: RunReport) -> None:
        """프로젝트의 ``.mvn/maven.config``가 자기 settings를 지정하면 플래그와 인수인계 줄을 한 번 남긴다.
        ``build.ignore_project_mvn``이 켜져 있으면 ``.mvn`` 폴더 전체를 쓰지 않았다는 안내를 대신 남긴다.
        파일은 바꾸지 않는다. 예외를 던지지 않는다.

        Flag `project-maven-settings-ignored`, or `-used` when Maven still reads a project `-gs`
        (`sbmig.build.mvn.project_settings_note`); journal `project_maven_settings`. The exit code does not
        change."""
        from sbmig.build.mvn import ignore_project_mvn
        if ignore_project_mvn(self.cfg):
            self._intake_project_mvn(report)
            return
        try:
            from sbmig.build.mvn import project_maven_settings, project_settings_ignored, project_settings_note
            found = project_maven_settings(self.ws.root)
            note = project_settings_note(self.cfg, found)
        except Exception as e:  # noqa: BLE001 - 안내 때문에 실행을 멈추지 않는다
            log.debug("project maven settings check failed: %s: %s", type(e).__name__, e)
            return
        if note is None or found is None:
            return
        flag, line = note
        ignored = project_settings_ignored(self.cfg, found)
        self._flag(flag)
        if line not in report.manual_followups:
            report.manual_followups.append(line)
        log.info("intake: %s names its own Maven settings (%s): %s", found.path, found.shown(),
                 "ignored, every build uses the server's settings" if ignored
                 else "Maven still reads its global settings")
        self.ws.journal({"event": "project_maven_settings", "file": found.path, "settings": found.settings,
                         "global_settings": found.global_settings, "offline": found.offline, "ignored": ignored})

    def _intake_project_mvn(self, report: RunReport) -> None:
        """프로젝트의 ``.mvn`` 폴더를 빌드에 쓰지 않았다는 플래그와 인수인계 줄을 한 번 남긴다.
        폴더는 바꾸지 않는다. 예외를 던지지 않는다.

        Flag `project-mvn-ignored` (`sbmig.build.mvn.project_mvn_note`); journal `project_mvn` lists what each
        non-empty `.mvn` folder holds and which options are carried over to the command line."""
        try:
            from sbmig.build.mvn import maven_basedir, project_mvn_folders, project_mvn_note
            folders = project_mvn_folders(self.ws.root, skip=getattr(self.ws, "ignored_dirs", ()) or ())
            note = project_mvn_note(self.cfg, folders)
            if note is None:
                return
            basedir = str(maven_basedir())
        except Exception as e:  # noqa: BLE001 - 안내 때문에 실행을 멈추지 않는다
            log.debug("project .mvn check failed: %s: %s", type(e).__name__, e)
            return
        flag, line = note
        self._flag(flag)
        if line not in report.manual_followups:
            report.manual_followups.append(line)
        carried: list[str] = []
        for f in folders:
            carried += [a for a in f.carried() if a not in carried]
        log.info("intake: %s not read by Maven in this run (MAVEN_BASEDIR=%s); carried over to the command line: %s",
                 ", ".join(f.rel for f in folders), basedir, " ".join(carried) or "nothing")
        self.ws.journal({"event": "project_mvn", "basedir": basedir, "carried": carried, "folders": [
            {"dir": f.rel, "maven_config": list(f.maven_config.options) if f.maven_config is not None else None,
             "jvm_config": f.jvm_options if f.has_jvm_config else None,
             "extensions": f.extensions if f.has_extensions else None,
             "wrapper": f.wrapper, "other_files": f.other_files, "carried": f.carried()} for f in folders]})

    def _intake_stray_poms(self, report: RunReport) -> None:
        """IDE 출력이나 백업 폴더에 복사된 pom을 찾아 그 폴더를 ``ws.ignored_dirs``에 넣는다.
        구조 검토가 이 복사본을 모듈로 등록하면 reactor 중복으로 빌드가 막히기 때문이다.
        플래그 ``stray-pom-ignored``와 복사본마다 인수인계 줄을 남긴다. 파일은 바꾸지 않고, 예외를 던지지 않는다.

        Detector: `sbmig.analyze.pom_parser.find_stray_pom_copies` (same g:a as another pom). Every scan
        prunes `ignored_dirs` like build output. Journal `stray_pom_ignored`."""
        try:
            from sbmig.analyze.pom_parser import find_stray_pom_copies
            strays = find_stray_pom_copies(self.ws)
        except Exception as e:  # noqa: BLE001 - 찾지 못해도 실행은 계속한다
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
        # 운영자가 화면을 보고 옮겨 쓰는 한국어 한 줄
        log.info("접수: pom 복사본 %d개 무시 — %s", len(strays),
                 "; ".join(f"{st.path} (= {st.duplicate_of}, {st.coordinates})" for st in strays))
        self.ws.journal({"event": "stray_pom_ignored", "poms": [st.path for st in strays],
                         "duplicates_of": [st.duplicate_of for st in strays],
                         "coordinates": [st.coordinates for st in strays], "dirs": sorted(st.dir for st in strays)})

    def _intake_aggregator(self, report: RunReport) -> None:
        """루트 pom이 없고 형제 pom이 서로 의존하면 루트 aggregator pom을 만들고 커밋한다.
        파일 쓰기에 실패하면 후속 조치 한 줄만 남기고 실행을 계속한다."""
        from sbmig.analyze.aggregator import plan_aggregator, write_aggregator
        plan = plan_aggregator(self.ws)
        if plan is None:
            return
        try:
            path = write_aggregator(self.ws, plan)
            self.ws.commit(f"intake: generated root aggregator pom ({', '.join(plan.modules)})")
        except Exception as e:  # 실패하면 분석은 루트 pom이 없는 상태를 그대로 본다. 실행은 계속한다
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
            # 빠진 pom은 <modules>에 넣고 다시 실행하면 함께 이관된다. 그래서 인수인계에 따로 남긴다
            report.manual_followups.append(
                f"poms left out of the generated root ({', '.join(plan.excluded)}): no aggregator lists them and no module "
                f"depends on them — if they belong to the application, add them to the <modules> of "
                f"{plan.modules[0]}/pom.xml (or the generated root pom.xml) and run sbmig again; they were not migrated")
        log.info("%s", msg)
        self.ws.journal({"event": "aggregator_generated", "path": path, "artifact_id": plan.artifact_id,
                         "name": plan.name, "modules": plan.modules, "excluded": plan.excluded})

    def _intake_reactor_dedupe(self, report: RunReport) -> None:
        """reactor에 두 번 포함된 모듈의 중복 ``<module>``을 Maven 호출 전에 지운다.
        중복이 있으면 Maven이 reactor 전체를 거부한다. 그러면 OpenRewrite와 빌드가 모두 실패한다.
        - 루트가 아닌 aggregator pom의 중복 줄만 지우고 루트의 모듈 목록은 그대로 둔다
        - 지우면 커밋하고 플래그 ``reactor-duplicate-removed``와 후속 조치를 남긴다
        - 기본으로 꺼진 프로필 안의 중복은 후속 조치 메모만 남긴다
        실패해도 실행을 멈추지 않는다.

        `sbmig.analyze.aggregator.dedupe_reactor`; journal `reactor_dedupe`."""
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
            # 운영자가 화면을 보고 옮겨 쓰는 한 줄
            log.info("접수: reactor 중복 모듈 %d개 정리 (지운 줄 %d · 메모 %d)", len(dups), len(removed),
                     len(notes) - len(removed))
            self.ws.journal({"event": "reactor_dedupe", "duplicates": [d.dir for d in dups],
                             "removed": [f"{pom}: {text}" for _, pom, text in removed],
                             "kept": [f"{d.keep[0]}: {d.keep[1]}" for d in dups if d.keep],
                             "notes": notes})
        except Exception as e:  # noqa: BLE001 - 실패해도 Maven이 같은 오류를 보고하니 그대로 계속한다
            log.warning("reactor duplicate check failed: %s: %s", type(e).__name__, e)

    def step_analyze(self, report: RunReport, stop_after: Phase | None) -> bool:
        """분석 단계를 실행한다. 프로젝트 정보(케이스 타입, Java, Spring/Boot 버전, 라이브러리)와 권고를 만든다.
        여기서 멈추거나 중단하면 False를 반환한다. Maven 프로젝트가 아니거나 pom.xml이 없을 때다."""
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
        self._check_framework(report, profile)
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
        """루트 pom.xml이 있는지 확인한다. 없으면 MSA가 아닌 한 중단한다(exit 3, 플래그 ``no-root-pom``).
        루트에 pom이 없으면 Maven 호출과 재구성이 모두 실패하기 때문이다.
        MSA는 서비스마다 따로 빌드하니 루트 pom이 없어도 된다."""
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
        """분석 단계의 ADF 판단 결과(``profile.adf``)를 적용한다. Oracle ADF 프로젝트는 변환하지 않는다.
        - 근거 종류가 둘 이상이면 플래그 ``adf-project``와 인수인계 첫 줄을 남긴다.
          ``intake.adf_policy: stop``(기본값)이면 Maven과 LLM을 호출하지 않고 중단한다. True를 반환한다.
          ``continue``면 보고만 하고 False를 반환한다
        - 근거가 한 종류뿐이면 플래그 ``adf-traces``와 메모만 남기고 False를 반환한다

        Journal `adf_project` / `adf_traces`. A `.jpr` name or `adf-config.xml` alone is no evidence family
        (decided in `sbmig.analyze.adf`)."""
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
        if not summary.is_adf:                                   # 근거가 한 종류뿐이면 변환을 계속한다
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
        log.error("%s", line)                                    # 거부한 ADF 프로젝트를 grep으로 찾는 한 줄
        if policy != "stop":
            return False
        self._abort(report, adf_abort_reason(policy))
        return True

    def _check_hollow(self, report: RunReport, profile: ProjectProfile) -> None:
        """모듈이 아무 소스도 컴파일하지 않는 reactor인지 확인한다. 실행은 멈추지 않는다.
        - 소스가 모듈 안에는 없고 프로젝트의 다른 곳에 있으면 플래그 ``hollow-reactor``와 인수인계 첫 줄을 남긴다.
          이 실행은 성공으로 치지 않는다(exit 2)
        - 모듈 바깥에 java 파일이 더 많기만 하면 ``sources-outside-reactor`` 알림만 남긴다

        Detector `sbmig.analyze.hollow.detect_hollow`; journal `hollow_reactor`."""
        try:
            from sbmig.analyze.hollow import (HOLLOW_FLAG, OUTSIDE_FLAG, detect_hollow, hollow_followup,
                                              outside_followup)
            res = detect_hollow(self.ws, getattr(profile, "poms", None) or [])
        except Exception as e:  # noqa: BLE001 - 검사가 실패해도 실행은 계속한다
            log.warning("hollow-reactor check skipped: %s: %s", type(e).__name__, e)
            return
        if res.hollow:
            self._flag(HOLLOW_FLAG)
            line = hollow_followup(res)
            if line not in report.manual_followups:
                # ADF 줄이 이미 맨 앞에 있으면(adf_policy: continue) 그 다음에 넣는다

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
        """동봉 jar 가운데 목표 부모 체인이 이미 관리하는 g:a에 ``superseded: …`` 메모를 붙인다.
        보고서의 동봉 jar 표에 쓴다. 그런 jar는 의존성으로 연결하지 않는다. 예외를 던지지 않는다.

        Managed keys: the post-restructure capture (`effective-depmgmt-after-restructure.json`), else the
        loop's `_managed_keys()`."""
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
                keys = set(self.loop._managed_keys())  # noqa: SLF001 - 사전 시도와 같은 값을 쓴다
            for j in jars:
                key = f"{j.group_id}:{j.artifact_id}" if j.group_id and j.artifact_id else ""
                if key and key in keys and "superseded:" not in (j.note or ""):
                    mark = f"superseded: {key} is managed by the target parent chain"
                    j.note = f"{j.note}; {mark}" if j.note else mark
        except Exception as e:  # noqa: BLE001 - 보고서 표시 때문에 실행이 실패하지 않게 한다
            log.debug("bundled jar classification skipped: %s: %s", type(e).__name__, e)

    def _note_bundled_jars(self, profile: ProjectProfile) -> None:
        """분석 단계가 찾은 동봉 jar를 기록한다. 프로젝트에 있지만 어떤 pom도 선언하지 않은 jar다.
        플래그 ``bundled-jars``, 저널, 로그 한 줄, ``logs/bundled-jars.json``을 남긴다. 예외를 던지지 않는다.

        Wiring happens elsewhere: the restructure (`layout.wire_bundled_jars`) and the fix loop's pre-try
        `sbmig-bundled` on a build error."""
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
        except Exception as e:  # noqa: BLE001 - 기록이 실패해도 실행은 계속한다
            log.warning("bundled-jar record skipped: %s: %s", type(e).__name__, e)

    def _check_framework(self, report: RunReport, profile: ProjectProfile) -> None:
        """분석 단계의 사내 프레임워크 검사 결과(``profile.framework``)를 기록한다. 예외를 던지지 않는다.
        - 아직 못 찾았으면 실제 Maven일 때만 ``dependency:tree``로 한 번 더 찾는다
        - 찾았으면 플래그 ``framework-project``와 인수인계 줄을 남긴다
        - 부모 pom도 의존성 트리도 읽지 못했으면 플래그 ``framework-unchecked``를 남긴다

        No-op when `profile.framework` is None (no patterns). The tree lookup needs `framework.transitive` !=
        off and no dry run; a failure only adds `transitive: <why>` to `unchecked`; then `decide_scope` again
        and `profile.json` re-saved. `framework-project` is not an attention flag. The Handover line goes
        after an ADF / hollow-reactor first line. Journal `framework_project` / `framework_check`. Contract
        `v2.1z-ak`."""
        use = getattr(profile, "framework", None)
        if use is None:
            return
        try:
            fw = importlib.import_module("sbmig.analyze.framework")
        except ImportError as e:  # pragma: no cover - the module ships with sbmig
            log.warning("framework check skipped: %s", e)
            return
        try:
            fcfg = getattr(self.cfg, "framework", None)
            if not use.found and str(getattr(fcfg, "transitive", "apply")) != "off" and self._real_maven \
                    and not self._dry_run:
                try:
                    evidence, why = fw.transitive_evidence(self.ws, self.cfg, profile)
                except Exception as e:  # noqa: BLE001 - 예외를 던지지 않는 함수지만 방어적으로 받는다
                    evidence, why = [], f"{type(e).__name__}: {e}"
                use.evidence.extend(evidence or [])
                if why:
                    use.unchecked.append(f"transitive: {why}")
                use = fw.decide_scope(use, self.cfg)
                profile.framework = use
                self.ws.save_model("profile.json", profile)
            if not use.found:
                self.ws.journal({"event": "framework_check", "found": False, "unchecked": list(use.unchecked)})
                # 판단에 필요한 부모 pom도 의존성 트리도 읽지 못했다. 실행은 그대로 하고 그 사실만 알린다
                if _framework_undecided(use):
                    self._flag(FRAMEWORK_UNCHECKED_FLAG)
                    log.warning("분석: 사내 프레임워크 여부를 확인하지 못함 (%s)", "; ".join(use.unchecked)[:300])
                    if FRAMEWORK_UNCHECKED_LINE not in report.manual_followups:
                        report.manual_followups.insert(self._first_lines_end(report), FRAMEWORK_UNCHECKED_LINE)
                return
            self._flag(fw.FLAG)
            self.ws.journal({"event": "framework_project", "scope": use.scope, "artifacts": use.keys(),
                             "evidence": [e.model_dump() for e in use.evidence], "unchecked": list(use.unchecked),
                             "reason": use.reason, "name": use.name, "fix_loop": bool(use.fix_loop)})
            what = "JDK와 jakarta만 적용" if use.reduced else "설정에 따라 전체 이관"
            log.info("분석: 사내 프레임워크 프로젝트 (%s) - %s", fw.summary_text(use), what)
            line = fw.framework_followup(use)
            if line and line not in report.manual_followups:
                report.manual_followups.insert(self._first_lines_end(report), line)
        except Exception as e:  # noqa: BLE001 - 기록이 실패해도 실행은 계속한다
            log.warning("framework check record skipped: %s: %s", type(e).__name__, e)

    @staticmethod
    def _first_lines_end(report: RunReport) -> int:
        """인수인계 맨 앞의 ADF 줄과 소스 없는 reactor 줄 바로 다음 위치를 반환한다. 프레임워크 줄이 여기 들어간다."""
        from sbmig.analyze.adf import ADF_PREFIX
        from sbmig.analyze.hollow import HOLLOW_PREFIX
        pos = 0
        for line in report.manual_followups:
            if not (line.startswith(ADF_PREFIX) or line.startswith(HOLLOW_PREFIX)):
                break
            pos += 1
        return pos

    @property
    def reduced_scope(self) -> bool:
        """JDK와 jakarta만 올리는 사내 프레임워크 프로젝트 실행이면 True를 반환한다.

        `MigrationPlan.scope` once a plan exists, else `ProjectProfile.reduced_scope`."""
        if self.plan is not None:
            return getattr(self.plan, "scope", "full") == REDUCED_SCOPE
        return bool(getattr(self.profile, "reduced_scope", False)) if self.profile is not None else False

    @property
    def no_fix_loop(self) -> bool:
        """프레임워크 프로젝트를 변환과 빌드 한 번으로 끝내면 True를 반환한다.
        ``framework.fix_loop``가 false(기본값)일 때다. 그때는 수정 루프, 계획 조언, LLM 호출을 하지 않는다.

        Reads `FrameworkUse.fix_loop` recorded at analyze. Contract `v2.1z-aw`."""
        if not self.reduced_scope:
            return False
        use = getattr(self.profile, "framework", None) if self.profile is not None else None
        return not bool(getattr(use, "fix_loop", False))

    def remediation_after_failed_build(self, report: RunReport) -> bool:
        """빌드가 실패해도 취약점 조치를 이어서 실행해야 하면 True를 반환한다.
        ``remediation.after_failed_build``가 ``framework``이고, 프레임워크 프로젝트의 최종 빌드가 실패했을 때다.
        빌드는 통과했지만 변환이 실패한 프레임워크 실행도 True다. 수정 루프를 실행했는지와는 상관없다.

        The other chain conditions (switch, abort, stop) are the caller's; the caller passes
        `baseline_may_fail` only when `final_build_failed`."""
        rem = getattr(self.cfg, "remediation", None)
        if str(getattr(rem, "after_failed_build", "never") or "never") != "framework" or not self.reduced_scope:
            return False
        if not self._verify_results or report.final_build is None:
            return False
        if self.final_build_failed:
            return True
        # 빌드는 통과했지만 변환이 실패해 성공이 아닌 실행도 조치를 이어 간다
        return self.conversion_failed

    def step_plan(self, report: RunReport, stop_after: Phase | None,
                  dry_run: bool) -> list[tuple[Phase, list[tuple[int, PlanStep]]]] | None:
        """계획 단계를 실행한다. 계획을 세우고 오류 목록과 ``BuildFixLoop``를 만든 뒤 단계 묶음 목록을 반환한다.
        묶음 순서는 변환 -> 재구성(구조 검토 포함) -> 검증이다. 계획 뒤에 멈추면 None을 반환한다.

        A group = consecutive plan steps of the same phase; MSA plans may repeat a phase per target."""
        with stage("plan"):
            return self._step_plan(report, stop_after, dry_run)

    def _step_plan(self, report: RunReport, stop_after: Phase | None,
                   dry_run: bool) -> list[tuple[Phase, list[tuple[int, PlanStep]]]] | None:
        profile = self.profile
        assert profile is not None
        t = self.clock()
        plan = self._plan_with_advisories(profile, report.advisories)
        plan = self._advise_plan(plan)
        try:    # pom에 선언된 저장소를 알린다. settings 미러가 덮지 않는 저장소는 후속 조치로 남긴다
            from sbmig.build.mvn import repository_notes
            for n in repository_notes(profile.poms, self.cfg):
                if n not in plan.notes:
                    plan.notes.append(n)
                (log.warning if n.startswith("followup:") else log.info)("%s", n)
        except Exception as e:  # noqa: BLE001 - 안내 때문에 계획을 실패시키지 않는다
            log.debug("repository notes skipped: %s", e)
        self.plan = plan
        report.plan = plan
        self.ws.save_model("plan.json", plan)
        fw_use = getattr(profile, "framework", None)
        self.ws.journal({"event": "plan", "steps": [s.id for s in plan.steps], "notes": plan.notes,
                         "scope": getattr(plan, "scope", "full"), "name": getattr(fw_use, "name", "") or "",
                         "fix_loop": bool(getattr(fw_use, "fix_loop", False))})
        for n in plan.notes:
            if n.startswith("followup:"):
                report.manual_followups.append(n[len("followup:"):].strip())
        self._timed("plan", t)
        if self._stop(stop_after, Phase.PLAN):
            return None
        # 4. 단계 실행 ------------------------------------------------------------
        # 오류 목록은 계획 뒤에 연다. 첫 기록은 transform.compile에서 난 오류(seen compile)다
        self._start_error_list(dry_run)
        self.loop = BuildFixLoop(self.cfg, self.ws, self.build_runner, self.triager, self.fixer, self.kb, profile,
                                 plan_notes=plan.notes,
                                 budget=self.budget, dry_run=dry_run, blocked=self.blocked,
                                 locator=self._resolve_locator(), advisories=list(report.advisories or []),
                                 script_runner=self.script_runner, edit_applier=self._edit_applier,
                                 errors=self.errors)
        self._handoff_restructure()        # 재구성 뒤에 만든 루프도 재구성 결과를 받게 한다
        steps = list(plan.steps)
        # 계획에 최종 빌드가 없어도 always_final_verify가 켜져 있으면 verify.build를 덧붙인다. 최종 빌드는 반드시 한다
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
        """단계 묶음 하나를 실행하고 PhaseResult를 반환한다. 결과는 ``report.phases``에도 넣는다.
        ``stop_after`` 뒤의 묶음이면 None을 반환한다. 수정 루프가 실행을 중단했으면 ``_RunAborted``를 던진다.
        단계 안에서 예외가 나면 그때까지 한 일을 보고서에 남기고 예외를 다시 던진다."""
        if stop_after is not None and _PHASE_ORDER.index(phase) > _PHASE_ORDER.index(stop_after):
            # MSA 계획은 서비스마다 단계가 반복된다. stop_after 뒤의 단계만 건너뛴다
            self.ws.journal({"event": "phase_skipped", "phase": phase.value, "reason": f"stop_after={stop_after.value}"})
            return None
        t = self.clock()
        pr = PhaseResult(phase=phase, step_ids=[s.id for _, s in items], ok=True)
        self.ws.journal({"event": "phase_start", "phase": phase.value, "steps": pr.step_ids})
        try:
            with stage(phase.value):
                self._run_phase(pr, items)
        except BaseException as e:
            # git 타임아웃이나 버그로 예외가 나도 그때까지의 결과(MSA에서 먼저 성공한 서비스 빌드 등)는 보고서에 남긴다
            pr.ok = False
            pr.notes.append(f"phase interrupted: {type(e).__name__}: {e}")
            pr.finished_at = utcnow()
            report.phases.append(pr)        # 차단 항목은 실행 전체의 차단 목록에 이미 있다
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
        """마지막 묶음까지 실행한 뒤 ``stopped-after-*`` 플래그를 남긴다. ``stop_after``가 없거나 verify면 남기지 않는다."""
        if stop_after is not None and stop_after != Phase.VERIFY:
            self._stop(stop_after, stop_after)

    # ------------------------------------------------------------------ 취약점 조치만 하는 실행
    def step_remediation(self, report: RunReport) -> dict:
        """``remediation`` 실행 방식의 취약점 조치 단계를 실행하고 요약 dict를 반환한다.
        업로드한 ZIP으로 ``chain.run_only``를 호출한다. 요약은 ``report.remediation``에 넣는다.
        상태가 ``SUCCEEDED``면 ``report.success``를 True로 한다.
        조치 준비가 안 됐으면 플래그 ``remediation:not-ready``와 빠진 설정 이름을 남긴다. 그리고 중단한다(exit 3).

        The missing names never carry their values."""
        with stage("remediation"):
            from sbmig.remediations import chain
            t0 = self.clock()
            try:
                rs = chain.run_only(self.ws.run_dir, self._remediation_source(), self.cfg, run_id=self.ws.run_id,
                                    journal=self.ws.journal, stop=self._remediation_stop,
                                    components=self._remediation_components, build_targets=self._remediation_targets())
            except chain.RemediationNotReady as e:
                self._flag("remediation:not-ready")
                report.remediation = chain.summary(None, self.ws.run_dir,
                                                   skipped={"reason": "not_ready", "missing": list(e.missing)})
                self._abort(report, "vulnerability remediation not started (not ready): " + "; ".join(e.missing))
                return dict(report.remediation)
            finally:
                report.timings_s["remediation"] = round(self.clock() - t0, 3)
            report.remediation = chain.summary(rs, self.ws.run_dir)
            report.success = rs.status == "SUCCEEDED"
            return dict(report.remediation)

    def _web_project_name(self) -> Optional[str]:
        """웹에서 올릴 때 입력한 프로젝트명을 반환한다. 보고서 제목에만 쓴다.
        CLI로 직접 실행했거나 읽지 못하면 None을 반환한다.

        `sbmig.web.contract.display_project`: `control/request.json` in Job modes, the journal's `web_run`
        line in-process, else the project part of a web run id."""
        try:
            from sbmig.web.contract import display_project
            run_dir = getattr(self.ws, "run_dir", None)
            return display_project(run_dir, self.ws.run_id) if run_dir is not None else None
        except Exception:  # noqa: BLE001 - 표시 이름 때문에 실행이 실패하지 않게 한다
            log.debug("entered project name not read", exc_info=True)
            return None

    def _remediation_stop(self) -> bool:
        """취약점 조치 중에 ``stop.request``가 있으면 True를 반환한다. 확인할 수 없으면 멈춤 요청이 없는 것으로 본다."""
        fn = getattr(self.ws, "stop_requested", None)
        try:
            return bool(fn()) if callable(fn) else False
        except OSError:
            return False

    def _remediation_source(self) -> Any:
        """취약점 조치에 넘길 원본을 반환한다. 업로드한 ZIP을 그대로 쓴다.
        생성자에 ``source``가 없으면 저널의 ``intake`` 줄에 기록된 경로를 쓴다.
        ZIP이 아니거나 찾을 수 없으면 프로젝트 폴더를 쓴다. 이 실행 방식은 프로젝트를 바꾸지 않으니 원본과 같다."""
        import zipfile
        from pathlib import Path
        src = self._source
        if src is None:
            try:
                with open(self.ws.journal_path, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(ev, dict) and ev.get("event") == "intake" and ev.get("source"):
                            src = ev["source"]
                            break
            except (OSError, AttributeError, TypeError):
                src = None
        if src is not None:
            p = Path(src)
            try:
                if p.is_file() and zipfile.is_zipfile(p):
                    return p
            except OSError:
                pass
        return self.ws.root

    def _remediation_targets(self) -> list[str]:
        """취약점 조치가 빌드할 폴더 목록을 프로젝트 루트 기준으로 반환한다.
        루트 pom.xml이 있으면 루트에서 한 번 빌드하니 빈 목록이다.
        없으면 어떤 ``<modules>``에도 들어 있지 않은 pom의 폴더를 반환한다. pom을 읽을 수 없으면 빈 목록이다.

        Stray copies are already excluded by `parse_poms`. Read-only."""
        try:
            if (self.ws.root / "pom.xml").is_file():
                return []
            from sbmig.analyze.pom_parser import parse_poms
            poms, _ = parse_poms(self.ws)
            referenced: set[str] = set()
            for p in poms:
                base = posixpath.dirname(p.path) or "."
                for m in p.modules:
                    t = posixpath.normpath(posixpath.join(base, m))
                    referenced.add(posixpath.normpath(t if t.endswith(".xml") else posixpath.join(t, "pom.xml")))
            roots = sorted((p.path for p in poms if p.path not in referenced), key=lambda x: (x.count("/"), x))
            return [posixpath.dirname(r) or "." for r in roots]
        except Exception as e:  # noqa: BLE001 - 실패하면 취약점 조치가 루트에서 빌드한다

            log.debug("remediation build targets not derived: %s: %s", type(e).__name__, e)
            return []

    def _finalize_remediation(self, report: RunReport) -> None:
        """``remediation`` 실행 방식의 마무리를 한다. 조치가 ``SUCCEEDED``이고 중단되지 않았으면 성공이다.
        infra 플래그, 출력 경로, 짧은 보고서를 남긴다.
        오류 목록, KB 기록, changes.patch, result.zip은 만들지 않는다.
        조치 결과 파일은 ``remediation/`` 아래에 있다."""
        report.finished_at = utcnow()
        rem = report.remediation if isinstance(report.remediation, dict) else {}
        report.success = rem.get("status") == "SUCCEEDED" and not _aborted(report.flags)
        try:
            self._infra_flags(report)
        except Exception as e:  # noqa: BLE001 - 보고서는 반드시 남긴다
            log.debug("infra flags not computed: %s", e)
        out = report.outputs
        out["run_dir"] = str(self.ws.run_dir)
        out["project_dir"] = str(self.ws.root)
        out["journal"] = str(self.ws.journal_path)
        out["logs"] = str(self.ws.logs_dir)
        if "md" in self.cfg.report.formats:
            out["report_md"] = str(self.ws.run_dir / "report.md")
        if "json" in self.cfg.report.formats:
            out["report_json"] = str(self.ws.run_dir / "report.json")
        for key, rel in (rem.get("files") or {}).items():
            out[f"remediation_{key}"] = str(self.ws.run_dir / rel)
        writer = self._report_writer
        if writer is None:
            from sbmig.report import write_report
            writer = lambda r, w: write_report(r, w, cfg=self.cfg)      # noqa: E731 - 설정의 report.language를 쓴다
        try:
            paths = writer(report, self.ws) or {}
            out.update({k: v for k, v in paths.items()})
        except Exception as e:
            log.exception("report writing failed")
            report.flags.append(f"report-failed: {e}")
        self.ws.journal({"event": "report", "success": report.success, "flags": report.flags, "flow": report.flow,
                         "remediation": rem.get("status"), "outputs": out})

    def _resolve_locator(self) -> Any:
        if self._locator is not None:
            return self._locator or None
        return default_locator() if self._real_maven else None

    # ------------------------------------------------------------------ 라이브러리 권고(분석, 계획 쪽)
    def _evaluate_advisories(self, profile) -> list:
        """이 프로젝트가 쓰는 EOL 라이브러리의 권고를 찾는다. springfox, hystrix, sleuth 같은 라이브러리다.
        차단하지는 않는다. 스크립트 프롬프트의 힌트와 보고서 인수인계에만 쓴다. 예외를 던지지 않는다.

        Source `knowledge/libraries/advisories.yml`; an unavailable advisory package means no findings. The
        finalize side (outcome, handover, follow-ups) lives in `_finalize`."""
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
        """계획기를 호출한다. 권고 목록은 계획기가 ``advisories=`` 인자를 받을 때만 넘긴다.

        Test fakes may not take `advisories=`: a TypeError naming it falls back to a call without them."""
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
        """접수 단계에서 아무것도 바꾸기 전에 as-is effective dependencyManagement를 캡처한다.
        인수인계의 바뀐 버전 목록과, as-is 버전을 그대로 이어받을 때 쓴다. 실제 Maven이 아니면 건너뛴다."""
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

    # ------------------------------------------------------------------ 사전 점검
    def _preflight(self, report: RunReport) -> None:
        """사전 점검을 한다. 결과는 저널 ``preflight`` 한 줄과 후속 조치로 남긴다.
        - ``build.settings_file``이 없거나 비어 있으면 플래그 ``settings-not-found``를 붙이고 바로 끝낸다
        - Maven이 실행되는지 본다. JAVA_HOME 문제와 Maven이 없는 경우를 구분한다
        - Maven의 Java가 ``build.require_java_major``(21)인지 본다
        - 사내 parent 설정이 채워졌는지 본다"""
        checks: dict[str, Any] = {}
        # build.settings_file이 없거나 비어 있으면 모든 Maven 호출이 실패한다. Maven을 호출하기 전에 멈춘다
        problem = self._settings_file_problem()
        if problem is not None:
            state, path = problem
            if state == "empty":
                self._settings_problem = (f"Maven settings file is empty: {path} (build.settings_file) — copy the "
                                          "settings.xml there and rerun")
            else:
                self._settings_problem = (f"Maven settings file not found: {path} (build.settings_file) — put the "
                                          "settings.xml there and rerun")
            report.flags.append("settings-not-found")
            log.error("preflight: %s", self._settings_problem)
            self.ws.journal({"event": "preflight", "settings_file": f"{state} ({path})"})
            return
        if self._build_runner is None:
            try:
                avail = lazy_attr("sbmig.build", "maven_available", "C (build)")
                try:
                    res = avail(self.cfg)
                except TypeError:
                    res = avail()
                # 실제 구현은 (ok, detail)을, 테스트 가짜 구현은 bool을 반환한다. 튜플은 늘 참이라서 나눠서 본다
                ok, detail = (bool(res[0]), str(res[1] or "")) if isinstance(res, tuple) else (bool(res), "")
                checks["mvn"] = ok
                if detail:
                    checks["mvn_detail"] = detail
                if not ok:
                    report.flags.append("mvn-not-found")
                    if _JAVA_HOME_PROBLEM_RE.search(detail):     # Maven은 있는데 Java를 찾지 못한 경우
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
        for name in ("boot_parent", "comp_parent"):               # 모듈 역할 parent
            spec = getattr(self.cfg.artifacts, name, None)
            if spec is not None:
                checks[f"artifacts.{name}"] = f"{spec.key}:{spec.version or '-'}"
        checks["java_home"] = self.cfg.build.java_home or "(inherited)"
        self.ws.journal({"event": "preflight", **checks})
        if self._build_runner is None:
            self._check_http_blocker()

    def _check_http_blocker(self) -> None:
        """Maven settings의 차단용 미러가 ``http://`` 저장소를 가로채면 경고 한 줄과 저널을 남긴다.
        그 저장소에서 받을 게 없을 수도 있으니 중단하지는 않는다.
        Maven이 실제로 거부하면 그때 ``BuildFixLoop.http_blocked_abort``가 중단한다.

        Reads user + global settings (`sbmig.build.env_errors.active_http_blockers`); journal
        `maven_http_blocker_active`."""
        try:
            from sbmig.build.env_errors import active_http_blockers
            found = active_http_blockers(self.cfg, self.ws.root)
        except Exception as e:  # noqa: BLE001 - 사전 점검 때문에 실행이 실패하지 않게 한다
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
    _settings_problem = ""

    def _settings_file_problem(self) -> tuple[str, str] | None:
        """``build.settings_file``에 문제가 있으면 (``missing`` 또는 ``empty``, 경로)를 반환한다.
        파일을 읽을 수 없으면 missing, 비어 있으면 empty다. 설정하지 않았거나 내용이 있으면 None을 반환한다.

        The path is the absolute one Maven would get as `-s`. In the cluster the file is a plain file the
        operator copies onto the data volume, so an empty file means it was created and never filled. Unset =
        Maven's default settings (as on a PC)."""
        value = self.cfg.build.settings_file
        if value is None or not str(value).strip() or str(value).strip().lower() == "null":
            return None
        path = Path(str(value).strip()).expanduser().absolute()
        try:
            if path.is_file() and os.access(path, os.R_OK):
                return ("empty", str(path)) if path.stat().st_size == 0 else None
        except OSError:
            pass
        return ("missing", str(path))

    def _java_major_wanted(self) -> int:
        return int(getattr(self.cfg.build, "require_java_major", 0) or 0) or 21

    def _java_home_text(self) -> str:
        import os
        home = self.cfg.build.java_home or os.environ.get("JAVA_HOME") or ""
        return (f"build.java_home={home}" if self.cfg.build.java_home else
                f"JAVA_HOME={home}" if home else "JAVA_HOME not set: mvn uses the java on PATH")

    def _check_java_major(self, report: RunReport, detail: str, checks: dict[str, Any]) -> None:
        """``mvn -v``의 Java가 ``build.require_java_major``(21)가 아니면 플래그 ``jdk-not-21``을 붙인다.
        그대로 두면 모든 빌드가 수정 루프로는 고칠 수 없는 이유로 실패한다. 그래서 접수 단계가 바로 중단한다.
        버전을 읽을 수 없으면 검사하지 않는다."""
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
        # 중단 후속 조치에 그대로 들어가는 한 줄
        self._jdk_problem = (f"Maven runs on Java {ver} ({where}), not Java {want}: set build.java_home (or JAVA_HOME) "
                             f"to a JDK {want} home and rerun")
        report.flags.append("jdk-not-21")
        log.error("preflight: %s", self._jdk_problem)

    # ------------------------------------------------------------------ 단계 묶음
    def _run_phase(self, pr: PhaseResult, items: list[tuple[int, PlanStep]]) -> None:
        """묶음 안의 단계를 차례로 실행한다. 로그에는 단계마다 그 단계의 태그가 붙는다.
        어떤 단계가 묶음을 중단시키면 나머지 단계는 건너뛴다.
        검증 묶음은 최종 빌드가 모두 성공했을 때만 성공이다."""
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
        """계획 단계 하나를 종류에 맞게 실행하고 (성공했는지, 묶음을 중단할지)를 반환한다.
        레시피 id를 찾지 못하거나 재구성이 실패하면 묶음을 중단한다. 선택 아티팩트 버전 올리기는 실패해도 계속한다.
        묶음이 중단되어도 최종 빌드는 실행된다."""
        ok: Any = True
        if step.kind == StepKind.OPENREWRITE:
            ok = self._step_rewrite(pr, step)
            if ok is None:          # 레시피 id가 없으면 설정이나 코드 오류다
                aborted, ok = True, False
                pr.ok = False
        elif step.kind == StepKind.REVIEW:
            ok = self._step_review(pr, step)
        elif step.kind == StepKind.BUILD_FIX_LOOP:
            # 중간 수정 루프. 계획기는 만들지 않지만 직접 만든 계획에는 있을 수 있다
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
        """변환 단계를 실행한다. 계획된 레시피를 OpenRewrite 한 번에 모두 실행하고 ``rewrite: <step>``로 커밋한다.
        - 레시피가 Maven 메타데이터를 받지 못해 ``4.0.x`` 같은 버전 문자열을 남기면 되돌리고 한 번 다시 시도한다
        - 사내 부모의 as-is 버전이 저장소에 없어 실패하면 설정 버전으로 맞추고 한 번 더 실행한다
        - 그 밖의 실패는 기록하고 계속한다. 실제 문제는 빌드에서 드러난다
        레시피 id를 찾지 못하면 설정 오류다. None을 반환해서 묶음을 중단한다."""
        self._flush_notes(pr)
        if self.budget.time_exceeded():
            pr.notes.append(f"{step.id} skipped: run time budget exhausted")
            self._flag("time-budget-exhausted")
            return True
        # 선언한 인코딩과 실제 바이트가 다른 pom이 있으면 OpenRewrite 변환 전체가 실패한다. 레시피보다 먼저 바로잡는다
        self._correct_pom_encodings(pr, step)
        if self.reduced_scope:
            # 레시피가 MS949 / EUC-KR 소스를 UTF-8로 읽다가 건너뛰지 않게, OpenRewrite보다 먼저 인코딩을 기록한다
            self._write_encodings_reduced(pr, step)
        if not step.recipes:
            return True
        runner = self.rewrite_runner
        pre = self.ws.head()
        clean_before = self._dry_run or not self.ws.is_dirty()
        res = self._run_rewrite_once(runner, step)
        # 레시피가 Maven 메타데이터를 받지 못하면 `4.0.x` 같은 버전 문자열이 그대로 남는다
        unresolved = self._unresolved_versions(res)
        if unresolved and self.cfg.openrewrite.download_retry > 0 and clean_before:
            wait_s = max(0, int(self.cfg.openrewrite.download_retry_wait_s))
            self.ws.revert_to(pre)                 # 반쯤 해석된 결과를 버리고 실행 전 상태로 되돌린다

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
        # Maven의 HTTP 차단 미러에 막혔으면 부모 버전을 맞추거나 다시 시도해도 풀리지 않는다. 아래에서 실행을 중단한다
        http_blocked = not res.ok and self._http_blocked_text(res)
        # 사내 부모 g:a의 as-is 버전이 저장소에 없어서 실패했으면, 설정 버전으로 맞추고 LLM 없이 한 번 더 실행한다
        # 프레임워크 프로젝트는 부모 pom을 그대로 두니 맞추지 않는다
        if not res.ok and not self._dry_run and not http_blocked and not self.reduced_scope:
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
        if res.ok and self.reduced_scope:
            self._restore_scopes_reduced(pr, step)
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
            self._note_rewrite_failed(step, err)
        return True

    def _note_rewrite_failed(self, step: PlanStep, err: str) -> None:
        """OpenRewrite가 실패했지만 실행은 계속할 때, 플래그 ``rewrite-failed``와 저널을 남긴다.
        인수인계 줄은 마무리 단계에서 ``_rewrite_failed_followup``이 쓴다. 이미 중단된 실행이면 건너뛴다.

        Not for the aborting cases (missing recipe id, HTTP blocker, parent not found). Journal
        `rewrite_failed {step, sets, reason}`. A reduced run with this flag is not a success
        (`build_succeeded`). Contract `v2.1z-ay`."""
        flags = list(self.report.flags if self.report is not None else []) + \
            (list(self.loop.flags) if self.loop is not None else [])
        if _aborted(flags):
            return
        from sbmig.rewrite.failure import FLAG, clean_reason, recipe_set_names
        sets = recipe_set_names([r.id for r in step.recipes], self.cfg)
        reason = clean_reason(err, roots=[str(self.ws.root), str(self.ws.run_dir)])
        self._rewrite_failures.append((step.id, sets, reason))
        self._flag(FLAG)
        self.ws.journal({"event": "rewrite_failed", "step": step.id, "sets": sets, "reason": reason})
        log.warning("%s: the automatic conversion failed (%s): %s", step.id, ", ".join(sets), reason)

    def _rewrite_failed_followup(self, report: RunReport) -> None:
        """변환이 실패한 실행의 인수인계 줄을 ADF 줄과 소스 없는 reactor 줄 바로 뒤에 넣는다.
        적용하지 못한 레시피 세트는 모두 넣고, 사유는 첫 번째 것만 쓴다.

        Goes before the framework no-fix-loop line."""
        if not self._rewrite_failures:
            return
        from sbmig.rewrite.failure import rewrite_failed_followup
        sets = list(dict.fromkeys(s for _, names, _ in self._rewrite_failures for s in names))
        line = rewrite_failed_followup(sets, self._rewrite_failures[0][2])
        if line not in report.manual_followups:
            report.manual_followups.insert(self._first_lines_end(report), line)

    def _correct_pom_encodings(self, pr: PhaseResult, step: PlanStep) -> None:
        """XML 선언과 실제 바이트의 인코딩이 다른 pom을 OpenRewrite 실행 전에 맞추고 커밋한다.
        ``layout.legacy_context.correct_pom_encoding``이 꺼져 있으면 아무것도 하지 않는다.
        dry-run이면 메모만 남긴다.
        예외를 던지지 않는다.

        Runs at the start of every `transform.rewrite` (idempotent). `sbmig.restructure.pom_charset.
        correct_pom_encodings`; flag `pom-encoding-corrected`, journal `pom_encodings`, one hand-over line,
        one error-list entry per pom (`seen = transform`). Contract `v2.1z-ay`."""
        from sbmig.core.config import LegacyContextConfig
        switches = getattr(self.cfg.layout, "legacy_context", None) or LegacyContextConfig()
        if not bool(getattr(switches, "correct_pom_encoding", True)):
            return
        try:
            from sbmig.restructure.pom_charset import FLAG, correct_pom_encodings, handover_line
            out = correct_pom_encodings(self.ws, write=not self._dry_run)
            if not out.fixes:
                return
            if self._dry_run:
                pr.notes.append(f"{step.id}: dry-run → pom encodings not corrected: "
                                + ", ".join(f.pom for f in out.fixes))
                return
            if self.ws.is_dirty():
                self.ws.commit("transform: pom encodings corrected")
            self.ws.journal({"event": "pom_encodings", "step": step.id, "entries": out.entries()})
            self._flag(FLAG)
            for f in out.fixes:
                note = f"{f.pom}: {f.note()}"
                pr.notes.append(note)
                log.info("%s: %s", step.id, note)
                self.errors.add_pom_verify(f.pom, f.note(), "transform")
            line = handover_line(out.fixes)
            if self.report is not None and line not in self.report.manual_followups:
                self.report.manual_followups.append(line)
        except Exception as e:  # noqa: BLE001 - 보조 단계다. 실패해도 남은 문제는 OpenRewrite와 빌드가 보여 준다
            log.warning("%s: pom encoding step skipped: %s: %s", step.id, type(e).__name__, e)
            pr.notes.append(f"{step.id}: pom encodings not corrected ({type(e).__name__}: {e})")

    # 레시피가 maven-metadata.xml을 읽지 못했을 때 pom에 남기는 버전 문자열
    UNRESOLVED_VERSIONS_FLAG = "rewrite-unresolved-versions"
    _POM_VALUE_RE = re.compile(r"<([\w.\-]+)>([^<>]*)</\1>")
    _UNRESOLVED_VERSION_RE = re.compile(r"^(?:\d+(?:\.\d+)*\.x|latest\.(?:release|integration|patch))$")

    @staticmethod
    def _fmt_unresolved(unresolved: dict[str, list[str]]) -> str:
        return "; ".join(f"{p}: {', '.join(vs)}" for p, vs in unresolved.items())

    def _run_rewrite_once(self, runner: Any, step: PlanStep) -> Any:
        # 형제 모듈은 aggregator 루트에서 실행해야 의존성이 해석된다. 그래서 reactor 인자를 만든다
        from .fixloop import build_target_args
        module, reactor_args = build_target_args(self.profile, step.target)
        kwargs = dict(dry_run=self._dry_run, label=step.id)
        # 실행기가 extra_args를 받는지는 시그니처로 미리 본다. 실행 중 TypeError 때문에 Maven을 두 번 실행하지 않는다
        if takes_keyword(runner.run, "extra_args"):
            return runner.run(list(step.recipes), module=module, extra_args=reactor_args, **kwargs)
        return runner.run(list(step.recipes), module=step.target, **kwargs)

    def _unresolved_versions(self, res: Any) -> dict[str, list[str]]:
        """rewrite가 바꾼 pom에 ``4.0.x``, ``latest.release`` 같은 버전 문자열이 남았으면 pom 경로별로 반환한다.
        실행기가 바뀐 파일을 알려 주지 않으면 모든 pom을 본다. dry-run이거나 실패한 실행이면 빈 dict를 반환한다."""
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
        """해석되지 않은 버전이 남은 이유를 플래그, 단계 메모, 후속 조치로 남긴다. rewrite 결과는 그대로 둔다.
        무엇이 깨지는지는 빌드에서 드러난다."""
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

    # OpenRewrite가 파싱하지 못한 파일에는 타입 정보가 필요한 레시피가 적용되지 않는다. 보고서에 알린다
    PARSE_PROBLEMS_FLAG = "rewrite-parse-problems"

    def _report_parse_problems(self, pr: PhaseResult, step: PlanStep, res: Any) -> None:
        """OpenRewrite가 파싱하지 못한 소스 파일을 플래그, 단계 메모, WARNING으로 남긴다.
        전체 이관에서는 그 때문에 생기는 빌드 오류를 수정 스크립트가 고친다.
        사내 프레임워크 실행에서는 그 파일이 변환되지 않은 채 남는다. 직접 확인하라는 후속 조치를 한 줄 남긴다.

        The follow-up names only files no earlier step of this run named (an MSA plan has one rewrite step per
        service), first 5 by name and `+N`; a full run gets no follow-up (its fix loop covers those files).
        `PARSE_PROBLEMS_UNKNOWN` (the log had only the plugin's summary line) is never a file name: it gets its own
        line without a count that names the step's rewrite log (run-dir relative), deduplicated by text.
        Contract: docs/INTERFACES.md "v2.1z-ck"."""
        files = [str(f) for f in (getattr(res, "parse_problems", None) or [])]
        self.ws.journal({"event": "rewrite_parse_problems", "step": step.id, "files": files,
                         "log": getattr(res, "log_path", None)})
        self._flag(self.PARSE_PROBLEMS_FLAG)
        shown = ", ".join(files[:5]) + (f", … (+{len(files) - 5})" if len(files) > 5 else "")
        tail = ("the build shows what follows" if self.no_fix_loop else "the fix scripts cover what the build finds")
        pr.notes.append(f"{step.id}: OpenRewrite could not parse {len(files)} source file(s) ({shown}) — recipes "
                        f"that need type information skipped them; {tail}")
        log.warning("%s: rewrite parse problems in %d file(s): %s", step.id, len(files), shown)
        if not self.reduced_scope or self.report is None:
            return
        # 변환만 하는 실행에서는 건너뛴 파일이 그대로 결과에 남는다. 요청 담당자가 볼 후속 조치로 남긴다
        from sbmig.rewrite.runner import PARSE_PROBLEMS_UNKNOWN
        lines: list[str] = []
        new = [f for f in dict.fromkeys(files) if f != PARSE_PROBLEMS_UNKNOWN and f not in self._parse_problem_files]
        if new:
            self._parse_problem_files.update(new)
            listed = ", ".join(new[:5]) + (f", … (+{len(new) - 5})" if len(new) > 5 else "")
            lines.append(f"OpenRewrite could not read {len(new)} source file(s) ({listed}): the JDK 21 / Jakarta "
                         "recipes skipped them, so they are not converted — check and convert them by hand")
        # 로그에 요약 줄만 있으면 파일 이름을 모른다. 개수 없이 그 단계의 rewrite 로그를 가리킨다
        if PARSE_PROBLEMS_UNKNOWN in files:
            # 보고서를 읽는 사람이 찾을 수 있게 실행 폴더 기준 경로로 쓴다. 로그가 없으면 logs/를 가리킨다
            where = str(getattr(res, "log_path", None) or "").replace("\\", "/")
            base = str(getattr(self.ws, "run_dir", "") or "").replace("\\", "/").rstrip("/")
            if base and where.startswith(base + "/"):
                where = where[len(base) + 1:]
            lines.append(f"OpenRewrite could not read some source files (their names are in the rewrite log "
                         f"`{where or 'logs/'}`): the JDK 21 / Jakarta recipes skipped them, so they are not "
                         "converted — check and convert them by hand")
        for fu in lines:
            if fu not in self.report.manual_followups:
                self.report.manual_followups.append(fu)

    @staticmethod
    def _http_blocked_text(res: Any) -> bool:
        """실패한 OpenRewrite 실행이 Maven의 HTTP 차단 미러에 막혔으면 True를 반환한다. 오류 메시지와 로그를 본다."""
        from sbmig.build.env_errors import http_blocker_hit
        return http_blocker_hit(BuildFixLoop.build_text(res)) is not None

    def _check_parent_in_rewrite(self, pr: PhaseResult, step: PlanStep, err: str) -> None:
        """사내 parent를 찾지 못해 OpenRewrite가 실패했으면 수정 루프와 같은 방식으로 실행을 중단한다.
        - 사내 부모가 선언한 build extension을 받지 못한 경우도 LLM 없이 중단한다
        - 프레임워크 프로젝트는 어떤 부모든 찾지 못하면 중단한다
        - 중단하지 않을 때는 레시피가 실행되지 않았다는 사실만 남긴다

        Abort = configured parent missing, or an as-is version with `openrewrite.unresolvable_asis_parent:
        abort`."""
        keys = self.cfg.artifacts.parent_keys()                # 루트 parent와 boot_parent, comp_parent
        if self.loop is None:
            return
        # 사내 부모가 선언한 build extension을 받지 못하면 Maven이 pom을 읽는 단계에서 실패한다. LLM 없이 중단한다
        m_ext = _BUILD_EXT_IN_TEXT_RE.search(err)
        if m_ext:
            chain = self.loop._chain_extensions()          # 같은 패키지
            key = f"{m_ext.group('g')}:{m_ext.group('a')}"
            coords = f"{key}:{m_ext.group('v')}"
            if chain.get(key) == coords:                   # 부모 체인의 g:a:v와 같을 때만 중단한다. 프로젝트가 정한 버전은 수정 루프가 다룬다
                gav = GAV.parse(coords)
                e = BuildError(kind=ErrorKind.POM_INVALID, message=err.splitlines()[0][:500], raw=err[:2000], artifact=gav,
                               module=step.target)
                br = BuildResult(ok=False, goal="rewrite", command=[], exit_code=1, duration_s=0.0, log_path="", errors=[e])
                self.loop._parent_abort(pr, step.phase, br, 0)   # 같은 패키지
                self._merge_blocked(pr, [it for it in self.blocked.items() if it.reason.value == "parent_not_found"])
            # 확장 실패 메시지 끝의 `@ <parent gav>`는 확장을 선언한 부모다. 아래 parent 검사로 넘기지 않는다
            return
        if not _PARENT_UNRESOLVABLE_RE.search(err):
            return
        from sbmig.restructure.asis_parent import classify_parent_misses
        misses = classify_parent_misses(err, self.cfg, self.ws)
        if self.reduced_scope:
            if misses:
                self._reduced_parent_abort(pr, step, err, misses)
            return
        if not misses and any(k in err for k in keys):
            # Maven 메시지에서 좌표를 읽지 못했어도 사내 부모 g:a가 보이면, 설정 버전을 찾지 못한 것으로 본다
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
            self.loop._parent_abort(pr, step.phase, br, 0)   # noqa: SLF001 - 같은 패키지
            self._merge_blocked(pr, [it for it in self.blocked.items() if it.reason.value == "parent_not_found"])
            return
        # 중단하지 않을 때(정책 continue, 맞춘 뒤에도 실패, 사내가 아닌 부모)는 레시피를 건너뛴 사실만 남긴다
        for m in misses:
            self._report_rewrite_skipped_parent(pr, step, m)

    def _restore_scopes_reduced(self, pr: PhaseResult, step: PlanStep) -> None:
        """재구성이 없는 실행에서, 레시피가 compile로 바꾼 as-is ``provided`` scope를 되돌리고 커밋한다.
        dry-run이면 메모만 남긴다. 실패해도 메모와 경고만 남긴다. 단계는 실패로 보지 않는다.

        Reduced run only, after a successful `transform.rewrite`; uses `sbmig.restructure.scope_restore` with
        the as-is profile as the source of the as-is scopes. Journal `scopes_restored`, one `pom-verify` entry
        per restored scope (same path as `RestructureResult.scope_restored`). Idempotent (MSA: one call per
        transform step)."""
        if self.profile is None:
            return
        if self._dry_run:
            pr.notes.append(f"{step.id}: dry-run → as-is provided scopes not restored")
            return
        try:
            from types import SimpleNamespace

            from sbmig.core.config import LegacyContextConfig
            from sbmig.restructure import legacy_context as lc
            from sbmig.restructure.scope_restore import project_pom_paths, restore_asis_scopes, scope_note
            switches = getattr(self.cfg.layout, "legacy_context", None) or LegacyContextConfig()
            rules = lc.load_rules(self.cfg.resolve_path(lc.RULES_FILE))
            prof = self.profile
            poms = [*prof.poms, *([prof.root_pom] if prof.root_pom else [])]
            out = restore_asis_scopes(self.ws, poms, project_pom_paths(self.ws), rules.scope_successors,
                                      on=bool(getattr(switches, "restore_asis_scopes", True)))
            if self.report is not None:
                self.report.manual_followups += [f for f in out.followups if f not in self.report.manual_followups]
            if not out.restored:
                return
            if self.ws.is_dirty():
                self.ws.commit("transform: as-is provided scopes restored")
            self.ws.journal({"event": "scopes_restored", "step": step.id, "entries": list(out.restored)})
            pr.notes.append(scope_note(out.restored))
            log.info("%s: %s", step.id, scope_note(out.restored))
            self._pom_verify_entries(SimpleNamespace(scope_restored=list(out.restored)), seen="transform")
        except Exception as e:  # noqa: BLE001 - 보조 단계다. 실패해도 남은 문제는 빌드가 보여 준다
            log.warning("%s: as-is scope restore skipped: %s: %s", step.id, type(e).__name__, e)
            pr.notes.append(f"{step.id}: as-is provided scopes not restored ({type(e).__name__}: {e})")

    def _write_encodings_reduced(self, pr: PhaseResult, step: PlanStep) -> None:
        """재구성이 없는 실행에서, MS949나 EUC-KR 소스가 있는 모듈의 pom에 ``project.build.sourceEncoding``을 기록한다.
        JDK 21 javac의 기본 인코딩이 UTF-8이기 때문이다. 기록하면 커밋한다.
        그대로 두는 부모 pom이 정한 값은 as-is 캡처에서 확인한다.
        dry-run이면 메모만 남긴다. 실패해도 메모와 경고만 남긴다.

        Must run before OpenRewrite: the plugin reads sources with the declared charset, and an MS949 file
        without it is a parse problem the recipes skip. A conflicting parent value becomes a Handover
        sentence. Journal `source_encodings`, one `pom-verify` entry per written pom. Idempotent."""
        if self._dry_run:
            pr.notes.append(f"{step.id}: dry-run → source encodings not written")
            return
        try:
            from types import SimpleNamespace

            from sbmig.core.config import LegacyContextConfig
            from sbmig.restructure.source_encoding import write_source_encodings
            switches = getattr(self.cfg.layout, "legacy_context", None) or LegacyContextConfig()
            out = write_source_encodings(self.ws, self.cfg, on=bool(getattr(switches, "write_source_encoding", True)),
                                         capture=self._asis_effective() or {})
            if self.report is not None:
                self.report.manual_followups += [f for f in out.followups if f not in self.report.manual_followups]
            if not out.written:
                return
            if self.ws.is_dirty():
                self.ws.commit("transform: source encodings written")
            self.ws.journal({"event": "source_encodings", "step": step.id, "entries": list(out.written)})
            for n in out.notes:
                if n not in pr.notes:
                    pr.notes.append(n)
                log.info("%s: %s", step.id, n)
            self._pom_verify_entries(SimpleNamespace(source_encodings_written=list(out.written)), seen="transform")
        except Exception as e:  # noqa: BLE001 - 보조 단계다. 실패해도 남은 문제는 빌드가 보여 준다
            log.warning("%s: source encoding step skipped: %s: %s", step.id, type(e).__name__, e)
            pr.notes.append(f"{step.id}: source encodings not written ({type(e).__name__}: {e})")

    def _reduced_parent_abort(self, pr: PhaseResult, step: PlanStep, err: str, misses: list) -> None:
        """사내 프레임워크 프로젝트에서 부모 pom을 찾지 못하면 LLM 없이 실행을 중단한다.
        이 실행은 부모 pom을 바꾸지 않는다. 저장소에 그 pom이 있어야만 빌드할 수 있다.

        Same record as the fix loop's `_parent_abort`: flag `aborted:parent-not-found`, a `parent_not_found`
        blocked item, `run aborted: …` follow-up, journal `abort`. `openrewrite.unresolvable_asis_parent` is
        not consulted."""
        lp = self.loop
        assert lp is not None
        coords = ", ".join(dict.fromkeys(m.coordinates for m in misses))
        settings = self.cfg.build.settings_file or "default"
        why = (f"Maven cannot resolve the parent pom {coords}; a framework project keeps its own parent pom, so the "
               f"repository must serve it (settings: {settings}). Make the repository serve it and rerun")
        errors = [BuildError(kind=ErrorKind.PARENT_RESOLUTION, message=err.splitlines()[0][:500], raw=err[:2000],
                             artifact=m.gav, signature=f"parent_resolution|{m.coordinates}", module=step.target)
                  for m in misses]
        try:
            from sbmig.core.models import BlockReason
            from sbmig.triage.blocked import BlockedCandidate, repositories_in
            c = lp.triager.cluster(errors, self.profile)[0]
            cand = BlockedCandidate(reason=BlockReason.PARENT_NOT_FOUND, errors=errors,
                                    repositories=repositories_in(err), evidence=[errors[0].message[:300]])
            lp._register_blocked(step.phase, 0, c, cand,   # noqa: SLF001 - 같은 패키지
                                 f"blocked: parent_not_found — parent {coords} is not resolvable and a framework project "
                                 "keeps its parent; run aborted")
        except Exception as e:  # noqa: BLE001 - 기록이 실패해도 아래 중단은 해야 한다
            log.warning("parent blocked item not recorded: %s: %s", type(e).__name__, e)
        lp.aborted = why
        lp._flag("aborted:parent-not-found")              # noqa: SLF001 - 같은 패키지
        lp._followup(f"run aborted: {why}")               # noqa: SLF001 - 같은 패키지
        pr.notes.append(f"{step.id}: {why}")
        self.ws.journal({"event": "abort", "phase": step.phase.value, "iteration": 0, "reason": why,
                         "scope": REDUCED_SCOPE, "parents": [m.coordinates for m in misses]})
        log.error("run aborted: %s", why)
        self._merge_blocked(pr, [it for it in self.blocked.items() if it.reason.value == "parent_not_found"])

    REWRITE_SKIPPED_PARENT_FLAG = "rewrite-skipped-asis-parent"
    ASIS_PARENT_ALIGNED_FLAG = "asis-parent-aligned"

    def _report_rewrite_skipped_parent(self, pr: PhaseResult, step: PlanStep, miss: Any) -> None:
        """as-is 부모 pom이 저장소에 없어서 레시피가 실행되지 않았다는 사실을 기록한다. 실행은 중단하지 않는다.
        플래그 ``rewrite-skipped-asis-parent``, 저널, 단계 메모, 인수인계 한 줄을 남긴다."""
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
        """사내 부모의 as-is 버전을 받지 못해 OpenRewrite가 실패했으면, 설정 버전으로 맞추고 다시 실행한다.
        ``openrewrite.unresolvable_asis_parent: align``일 때만 한다. 다시 실행한 결과를 반환한다.
        맞춘 pom은 커밋하고, 플래그 ``asis-parent-aligned``와 인수인계 한 줄을 남긴다.
        설정 버전도 찾지 못한 부모가 섞여 있거나 바꿀 pom이 없으면 ``res``를 그대로 반환한다.

        Journal `asis_parent_aligned`."""
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
            self.ws.revert_to(pre)                     # 실패한 실행이 남긴 반쪽 결과는 커밋하지 않는다

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
        """변환 뒤 컴파일 단계(``transform.compile``)를 실행한다. ``mvn clean compile``을 한 번 실행하고 고치지는 않는다.
        나온 오류는 모두 오류 목록(seen ``compile``)에 들어간다. 이후 재구성, 검토, 빌드가 이 목록에서 출발한다.
        컴파일 실패는 예상한 결과라서 단계를 실패로 보지 않는다.

        StepKind.BUILD outside the verify phase; only with `plan.compile_after_rewrite`."""
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
        except Exception as e:  # noqa: BLE001 - 기록용 단계다. 실제 상태는 검증 빌드가 보여 준다
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
        """중간 수정 루프 단계(``StepKind.BUILD_FIX_LOOP``)를 실행한다. 직접 작성한 계획에만 있는 단계다.
        계획기는 이 단계를 만들지 않는다. 수정 루프는 보통 최종 빌드가 실패한 뒤 ``_verify_fix_loop``에서 실행된다."""
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
        """이 실행의 재구성 결과와 모드를 수정 루프에 넘긴다.
        수정 루프는 이것으로 LLM 프롬프트의 "Project after the restructure" 절을 만든다.

        Sets `BuildFixLoop.restructure_results` / `restructure_modes`, the source of
        `FixContext.layout_facts`."""
        if self.loop is not None:
            self.loop.restructure_results = list(self._restructure_results)
            self.loop.restructure_modes = list(self._restructure_modes)

    def _step_restructure(self, pr: PhaseResult, step: PlanStep) -> bool:
        """재구성을 실행하고 ``restructure: <step>``로 커밋한다.
        base-parent 지정, ``{name}-boot`` / ``-back`` / ``-front`` 분할, 필수 아티팩트 추가, 선택 아티팩트 버전 올리기를 한다.
        옮긴 파일은 차단 목록에 알리고, pom 수정마다 ``pom-verify`` 항목을 만든다.
        실패하면 플래그 ``restructure-failed``를 붙인다. 그러면 최종 빌드가 통과해도 성공으로 보지 않는다."""
        self._flush_notes(pr)
        assert self.profile is not None and self.report is not None
        mode = self._step_mode(step)
        if mode == JSP_JAKARTA_MODE:
            return self._step_jsp_jakarta(pr, step)
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
            self._reset_restructure(step, pre)
            self._restructure_failed(step, mode, f"{type(e).__name__}: {e}")
            return False
        # 재구성이 실패했으면 남긴 커밋과 변경을 되돌려서 단계 전 상태로 둔다
        if not getattr(res, "ok", True):
            self._reset_restructure(step, pre)
        sha = self.ws.commit(f"restructure: {step.id}") if self.ws.is_dirty() else self.ws.head()
        if sha != pre:
            self._follow_moves(step, pre, sha)
        self._pom_verify_entries(res)
        self.report.manual_followups += [f for f in res.manual_followups if f not in self.report.manual_followups]
        for flag in list(getattr(res, "flags", None) or []):      # web-xml-registered 같은 재구성 플래그
            self._flag(flag)
        pr.notes += res.notes
        self._jsp_jakarta_note(pr, step, res)
        self._restructure_results.append(res)            # 구조 검토의 입력
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
        # 모듈을 합칠 때 같은 클래스가 겹치면 먼저 온 것을 남긴다. 버린 쪽 클래스는 LLM에 읽기 전용 참고로 보여 준다
        if self.loop is not None:
            self.loop.merge_conflicts = list(self.report.merge_conflicts)
        self._handoff_restructure()
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

    def _step_jsp_jakarta(self, pr: PhaseResult, step: PlanStep) -> bool:
        """재구성 없이 JSP와 태그 파일의 javax EE 이름만 jakarta로 바꾸고 커밋한다. 사내 프레임워크 프로젝트용이다.
        dry-run이거나 ``layout.jsp_jakarta_rename``이 false면 메모만 남긴다. 실패해도 메모만 남기고 True를 반환한다.

        `rename_jsp_files` is called the way the restructurer calls it; no RestructureResult reaches the fix
        loop or a structure review. The report reads the note back through `_jsp_jakarta_note`."""
        from types import SimpleNamespace
        if self._dry_run:
            pr.notes.append(f"{step.id}: dry-run → JSP javax → jakarta not applied")
            return True
        if not getattr(self.cfg.layout, "jsp_jakarta_rename", True):
            pr.notes.append(f"{step.id}: JSP javax → jakarta skipped (layout.jsp_jakarta_rename: false)")
            return True
        try:
            from sbmig.restructure.jsp_jakarta import rename_jsp_files
            out = rename_jsp_files(self.ws)
        except Exception as e:  # noqa: BLE001 - 보조 단계다. 실패해도 남은 문제는 빌드가 보여 준다
            log.warning("JSP javax → jakarta skipped: %s: %s", type(e).__name__, e)
            pr.notes.append(f"{step.id}: JSP javax → jakarta skipped: {type(e).__name__}: {e}")
            return True
        if out.renamed:
            log.info("JSP javax → jakarta: %d file(s), %d reference(s) of %d JSP / tag file(s)",
                     out.files, out.refs, out.scanned)
            if self.ws.is_dirty():
                self.ws.commit("transform: JSP javax → jakarta")
            res = SimpleNamespace(jsp_jakarta_files=out.files, jsp_jakarta_refs=out.refs,
                                  jsp_jakarta_renamed=[f"{rel}: {n}" for rel, n in out.renamed.items()])
            self._jsp_jakarta_note(pr, step, res)
        else:
            pr.notes.append(f"{step.id}: JSP javax → jakarta: nothing to rename ({out.scanned} JSP / tag file(s))")
        if out.failed:
            names = sorted(out.failed)
            pr.notes.append(f"{step.id}: JSP javax → jakarta: {len(names)} file(s) not renamed ({', '.join(names[:3])}"
                            f"{' …' if len(names) > 3 else ''}; see sbmig.log)")
        return True

    def _journal_restructure_jars(self, res: Any) -> None:
        """재구성이 동봉 jar를 어떻게 처리했는지 저널에 남긴다.
        연결한 jar마다 ``bundled_jar_wired``를, 연결하지 않은 jar는 모아서 ``bundled_jars_skipped`` 한 줄을 남긴다.
        프로젝트 정보의 같은 jar에 ``restructure: …`` 메모를 붙여 보고서 표에 나오게 한다. 예외를 던지지 않는다."""
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
        except Exception as e:  # noqa: BLE001 - 기록이 실패해도 실행은 계속한다
            log.debug("restructure bundled-jar journal skipped: %s: %s", type(e).__name__, e)

    # ------------------------------------------------------------------ 구조 검토
    def _make_reviewer(self) -> tuple[Any, str]:
        """구조 검토기를 만들어 (검토기, LLM을 쓸 수 없는 이유)를 반환한다. 만들지 못하면 (None, 이유)다.
        주입된 검토기가 없으면 예산이 걸린 LLM으로 ``StructureReviewer``를 만든다.
        KB는 None으로 넘긴다. 검토 기록은 커밋 뒤에 오케스트레이터가 남긴다."""
        llm, why = None, ""
        if self.budget.llm_allowed():
            try:
                llm = _BudgetedLLM(self.llm_client(), self.budget, sink=self._review_usage,
                                   default_model=self.cfg.llm.model)
            except Exception as e:  # noqa: BLE001 - LLM이 없으면 검토기가 skipped로 처리한다
                why = f"no LLM client: {type(e).__name__}: {e}"
                log.info("structure review: %s", why)
        else:
            why = f"LLM budget exhausted ({self.budget.llm_text()})"
        made = self._reviewer
        try:
            if made is not None and not isinstance(made, type) and callable(getattr(made, "review", None)):
                return made, why                    # 인스턴스면 그대로 쓴다. 클래스는 팩토리로 본다
            factory = made if callable(made) else lazy_attr("sbmig.review", "StructureReviewer", "R (review)")
            return factory(self.cfg, llm, self.script_runner, None), why
        except Exception as e:  # noqa: BLE001 - 검토기를 만들지 못해도 실행은 계속한다
            return None, f"structure reviewer unavailable: {type(e).__name__}: {e}"

    def _step_review(self, pr: PhaseResult, step: PlanStep) -> bool:
        """구조 검토 단계(``restructure.review``)를 실행한다. 실행을 멈추지 않는다.
        LLM이 재구성된 프로젝트의 pom, 의존성, 모듈 구조, 인코딩이 회사 규칙에 맞는지 보고 critical 문제만 고친다.
        고친 내용은 되돌릴 수 있게 ``review: fix critical structure issues (<n>)``로 커밋한다.
        warning은 보고서와 오류 목록에 메모로 남긴다. 실패하면 결과가 ``failed``, dry-run이면 ``skipped``다.

        The reviewer applies its fix uncommitted; `_commit_review` commits it here."""
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
        self._mark_change_base(step)
        if not self._dry_run:
            try:
                pre = self.ws.head()
            except Exception:  # noqa: BLE001 - git이 없으면 검토 항목에 diff를 넣지 않는다
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
                except Exception as e:  # noqa: BLE001 - 구조 검토는 실행을 멈추지 않는다
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
        how = f"edits ({len(ans.edits)})" if ans is not None and ans.edits else "script"   # 수정 방식
        line = (f"{step.id}: structure review {rv.verdict} ({len(rv.issues)} issue(s), {critical} critical)"
                + (f"; {how} {status}" if status else "") + (f" → commit {sha[:10]}" if sha else "")
                + (f" — {rv.note}" if rv.note else ""))
        pr.notes.append(line[:600])
        log.info("%s", line[:600])
        self._timed("review", t)
        return True

    def _mark_change_base(self, step: PlanStep) -> None:
        """자동 변환이 끝난 지금의 커밋을 보고서 코드 수정 내역의 시작점으로 기억한다. 한 번만 기억한다.
        구조 검토 직전과 첫 검증 빌드 직전 가운데 먼저 오는 쪽에서 호출한다.
        dry-run이거나 git이 없거나 중단 뒤의 빌드면 기억하지 않는다.

        Calling it before the first verify build too gives a base to runs without the review or the
        restructure (`scope=jdk-jakarta`). Journal `code_changes_base`."""
        if self._change_base or self._dry_run or getattr(self, "_after_abort", False):
            return
        try:
            self._change_base = self.ws.head()
        except Exception as e:  # noqa: BLE001 - git이 없으면 보고서에 코드 수정 내역이 없다
            log.debug("code change base not taken: %s", e)
            return
        self.ws.journal({"event": "code_changes_base", "commit": self._change_base, "step": step.id})

    def _commit_review(self, pr: PhaseResult, step: PlanStep, rv: Any) -> Optional[str]:
        """구조 검토가 남긴 변경을 커밋하고 커밋 해시를 반환한다.
        검토의 수정이 성공하지 않았는데 파일이 바뀌어 있으면 그 변경을 버린다. 실패한 검토는 프로젝트를 받은 그대로 둬야 한다.
        커밋할 것이 없거나 dry-run이면 None을 반환한다."""
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
                # 검토기 메모는 커밋 전에 쓴 것이다. "not committed yet"을 실제 커밋으로 바꾼다
                note = rv.note or ""
                rv.note = (note.replace(" — not committed yet", f" — committed {sha[:10]}")
                           if " — not committed yet" in note else note)
                return sha
            self.ws.discard_uncommitted()
            pr.notes.append(f"{step.id}: the structure review left uncommitted changes without a successful script "
                            "run — discarded")
        except Exception as e:  # noqa: BLE001 - git 문제가 있어도 지금 상태는 빌드가 보여 준다
            log.warning("structure review commit failed: %s: %s", type(e).__name__, e)
            pr.notes.append(f"{step.id}: committing the review script failed: {type(e).__name__}: {e}")
        return None

    def _review_entries(self, rv: Any, pre: Optional[str], sha: Optional[str]) -> None:
        """구조 검토 결과를 오류 목록과 KB에 기록한다. skipped이거나 dry-run이면 아무것도 남기지 않는다.
        - warning과 커밋된 수정이 다루지 못한 critical은 ``note`` 항목으로 남긴다
        - 커밋된 수정이 다룬 critical은 ``review``로 해결된 항목과 시도 하나로 남긴다
        - 응답의 스크립트는 KB 케이스 ``review|structure``에 저장한다
        - 검토의 LLM 호출 수는 첫 해결 항목의 시도에만 기록한다
        예외를 던지지 않는다.

        A resolved try carries source review, 원인 / 해결 / edits / diff / commit and the fix notes from
        `sbmig.loop.notes` (edits' `desc` / `<changes>` + diff lines), logged once as `수정: …` INFO lines;
        tries after the first carry 0 LLM calls and say so in their note."""

        if self._dry_run or rv is None or rv.verdict == "skipped":
            return
        try:
            ans = getattr(rv, "answer", None)
            diff, files = "", []
            if sha and pre:
                try:
                    diff = self.ws.diff(pre, sha)
                    files = self.ws.changed_between(pre, sha)
                except Exception as e:  # noqa: BLE001 - 커밋은 있고 diff 문자열만 없는 경우다
                    log.debug("review diff unavailable: %s", e)
            n_edits = len(getattr(ans, "edits", None) or []) if ans is not None else 0
            applied = ("edits" if n_edits else "script") if sha else "none"
            notes: list = []
            if sha:
                notes = try_notes(answer_notes(ans, edits=bool(n_edits)), diff,       # as-is / to-be 조각도 붙인다
                                  max_lines=int(getattr(getattr(self.cfg, "errors", None), "snippet_lines", 0) or 0))
                for nt in notes[:NOTES_LOGGED]:
                    log.info("수정: %s", note_line(nt))
                if len(notes) > NOTES_LOGGED:
                    log.info("수정: … %d more (errors.md)", len(notes) - NOTES_LOGGED)
            # 검토의 LLM 호출 한 번이 여러 critical을 고친다. 호출 수가 겹치지 않게 첫 항목에만 싣는다
            # 보고서의 LLM 사용량은 따로 _review_llm_usage에서 집계한다
            review_calls = int(rv.llm_usage.calls or 0)
            first_fixed = True
            for issue in rv.issues:
                text = " ".join((issue.problem or "").split()) + (f" — {issue.fix_hint}" if issue.fix_hint else "")
                # 근거 자료에 없는 버전이나 속성을 제시한 이슈는 적용되지 않았다. 검토가 고친 것으로 기록하지 않는다
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
        except Exception as e:  # noqa: BLE001 - 기록이 실패해도 실행은 계속한다
            log.warning("recording the structure review failed: %s: %s", type(e).__name__, e)

    def _review_major_upgrades(self, rv: Any, pre: str | None, sha: str) -> None:
        """구조 검토 커밋의 pom 변경에서 메이저 업그레이드와 라이브러리 교체를 찾아 ``rv.major_upgrades``에 남긴다.
        운영자 확인 가이드는 LLM 응답의 ``<verify>``를 쓰고, 없으면 sbmig가 만든다.
        수정 루프와 같은 로그를 남긴다. 변경을 되돌리지는 않는다. 예외를 던지지 않는다.

        A synthesized guide goes into `rv.answer.verify`, or into `rv.note` when there is no answer."""
        try:
            files: list[str] = []
            try:
                files = list(self.ws.changed_between(pre, sha)) if pre else []
            except Exception:  # noqa: BLE001 - 바뀐 파일을 모르면 커밋의 pom을 모두 보게 한다
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
        except Exception as e:  # noqa: BLE001 - 보고서용 정보다. 실패해도 검토는 실패로 보지 않는다
            log.debug("review read-back failed: %s: %s", type(e).__name__, e)

    def _review_llm_usage(self, rv: Any) -> None:
        """구조 검토의 LLM 호출을 실행의 사용량과 예산에 더한다. 같은 호출을 두 번 세지 않는다.
        예산이 걸린 클라이언트로 이미 셌으면 그대로 둔다.
        검토기가 자기 클라이언트를 썼으면 ``StructureReview.llm_usage``로 센다."""
        u = getattr(rv, "llm_usage", None)
        if self._review_usage or u is None or not u.calls:
            return
        self.budget.charge_llm(u.calls)
        key = asked_model_name(u.model, self.cfg.llm.model)
        self._review_usage[key] = self._review_usage.get(key, LLMUsage(model=key)).add(
            u.model_copy(update={"model": key}))

    def _follow_moves(self, step: PlanStep, pre: str, sha: str) -> None:
        """재구성이 옮긴 파일(``git diff -M``의 이름 변경)을 차단 목록에 알린다.
        그래서 옮기기 전에 차단된 파일은 ``{name}-back/src/…``로 옮겨진 뒤에도 차단 상태로 남는다.
        git 문제로 실패해도 괜찮다. 옮겨진 파일의 오류를 한 번 더 시도할 뿐이다."""
        try:
            res = self.ws._git("diff", "--name-status", "-M", "-z", pre, sha, check=False)   # noqa: SLF001
            moves = parse_renames(res.stdout or "") if res.returncode == 0 else {}
        except Exception as e:  # noqa: BLE001 - 기록이 실패해도 재구성은 실패로 보지 않는다
            log.debug("restructure moves of %s unavailable: %s", step.id, e)
            return
        if not moves:
            return
        n = self.blocked.note_moves(moves)
        self.ws.journal({"event": "restructure_moves", "step": step.id, "moved": len(moves), "blocked_rekeyed": n})

    def _reset_restructure(self, step: PlanStep, pre: str) -> None:
        """실패한 재구성 단계가 남긴 커밋과 변경을 버리고, 단계 전 커밋 ``pre``로 되돌린다.
        재구성기가 이미 되돌렸으면 아무것도 하지 않는다. 예외를 던지지 않는다.

        Needed so that the follow-up "the tree keeps the as-is layout / parents" is true. Journal
        `restructure_reset`."""
        try:
            moved = bool(pre) and self.ws.head() != pre
            dirty = self.ws.is_dirty()
            if not moved and not dirty:
                return
            if moved:
                self.ws.revert_to(pre)
            else:
                self.ws.discard_uncommitted()
        except Exception as e:  # noqa: BLE001 - git 문제가 있어도 실패 기록은 남긴다
            log.warning("restructure step %s: tree not reset to %s: %s: %s", step.id, pre, type(e).__name__, e)
            return
        log.warning("restructure step %s failed: tree reset to the commit before the step (%s)", step.id, pre[:10])
        self.ws.journal({"event": "restructure_reset", "step": step.id, "to": pre, "commits_dropped": moved})

    def _restructure_failed(self, step: PlanStep, mode: str, why: str) -> None:
        """재구성 실패를 기록해서 성공한 이관처럼 보이지 않게 한다.
        as-is 상태로도 최종 빌드는 통과할 수 있지만, 회사 레이아웃과 parent는 적용되지 않았기 때문이다.
        플래그 ``restructure-failed``(성공 아님, exit 2)와 인수인계 문장을 남긴다.
        선택 아티팩트 버전 올리기의 실패는 후속 조치만 남긴다."""
        assert self.report is not None
        self.ws.journal({"event": "restructure_failed", "step": step.id, "mode": mode, "error": why[:500]})
        if mode == "optional_bump":          # 선택 아티팩트 버전 올리기는 실패해도 후속 조치로만 남긴다
            self.report.manual_followups.append(f"optional artifact bump step {step.id} failed: {why[:300]} — bump "
                                                "the in-house optional artifacts by hand")
            return
        self._flag("restructure-failed")
        self.report.manual_followups.append(
            f"restructure step {step.id} ({mode}) failed: {why[:300]} — the tree keeps the as-is layout / parents; "
            "the final build ran on it, so BUILD SUCCESS here does not mean the migration is complete")

    _MANAGED_POM_RE = re.compile(r"\(([^()]*pom\.xml)\)\s*$")
    _PROCESSOR_PATH_RE = re.compile(r"\(annotationProcessorPaths, (?P<pom>[^()]*pom\.xml)\)\s*$")
    _RECIPE_DROPPED_RE = re.compile(r"^(?P<key>\S+) (?P<explicit>\S+) \(as-is (?P<asis>[^,]+), managed "
                                    r"(?P<managed>[^,]+), (?P<pom>[^()]+)\)\s*$")
    _OPTIONAL_ADDED_RE = re.compile(r"^(?P<key>\S+) → (?P<pom>.+?) \(for (?P<matched>[^()]*)\)\s*$")

    def _pom_verify_entries(self, res: Any, seen: str = "restructure") -> None:
        """재구성이 보고한 pom 수정을 하나씩 오류 목록의 ``pom-verify`` 항목으로 만든다.
        다음 빌드가 그 pom에서 오류를 내지 않으면 해결로 닫힌다.
        parent 지정, 필수 아티팩트 추가, 선택 아티팩트 버전 올리기와 추가, 관리 버전 삭제, as-is 버전 이어받기,
        scope 복원, 소스 인코딩, nonFilteredFileExtensions, 속성 삭제, BOM import, 생성한 pom이 대상이다.

        Each RestructureResult field is a list of strings in a fixed shape; the comments above each loop give
        the input shape -> the entry text. Changing a shape in `sbmig.restructure` needs the matching regex
        here."""
        root = "pom.xml"
        items: list[tuple[str, str]] = []
        for e in list(getattr(res, "parents_set", None) or []):
            pom, sep, what = str(e).partition(": ")
            items.append((pom if sep else root, f"parent {what if sep else e}"))
        for k in list(getattr(res, "mandatory_added", None) or []):
            items.append((root, f"mandatory {k} added"))
        for k in list(getattr(res, "optional_bumped", None) or []):
            items.append((root, f"optional {k}"))
        # "g:a → <pom> (for <matched>[, …])" → 'optional g:a added for <matched> in <pom>'
        for d in list(getattr(res, "optional_added", None) or []):
            m = self._OPTIONAL_ADDED_RE.match(str(d))
            if m:
                key, pom, matched = m.group("key"), m.group("pom"), m.group("matched")
                items.append((pom, f"optional {key} added for {matched} in {pom}"))
        for d in list(getattr(res, "managed_versions_dropped", None) or []):
            m = self._MANAGED_POM_RE.search(str(d))
            items.append((m.group(1) if m else root, f"explicit version dropped: {str(d)[:m.start()].strip() if m else d}"))
        # "g:a <explicit> (as-is <v | ->, managed <m>, <pom>)" → 'explicit version dropped: g:a <explicit> →
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
        # "g:a: <v> ← as-is <source> (<pom>)" → 'version carried over from the as-is build: g:a <v>';
        # "g:a: <v> ← repository newest release (…) (<pom>)" → 'version chosen from the repository: g:a <v>'
        for d in list(getattr(res, "versions_pinned", None) or []):
            # annotationProcessorPaths에 넣은 버전은 출처가 여러 가지다. 따로 기록한다
            # "g:a: <v> ← <source> (annotationProcessorPaths, <pom>)" → 'annotation processor path version
            # written: g:a <v> (<source>)'
            pm = self._PROCESSOR_PATH_RE.search(str(d))
            if pm:
                key, _, rest = str(d)[:pm.start()].strip().partition(": ")
                ver, _, how = rest.partition(" ← ")
                items.append((pm.group("pom").strip(),
                              f"annotation processor path version written: {key} {ver.strip()} ({how.strip()})"))
                continue
            m = self._MANAGED_POM_RE.search(str(d))
            head = str(d)[:m.start()].strip() if m else str(d)
            key, _, rest = head.partition(": ")
            ver, _, how = rest.partition(" ← ")
            if how.startswith("Boot "):
                # "g:a: <v> ← Boot <bv> BOM (not managed by the parent chain) (<pom>)"
                bom = how.split(" ", 2)[1] if how.count(" ") >= 1 else "?"
                row = f"version pinned from the Spring Boot {bom} BOM: {key} {ver.strip()} (the parent chain does not " \
                      "manage it)"
                items.append((m.group(1) if m else root, row))
                continue
            what = ("version chosen from the repository" if how.startswith("repository")
                    else "version carried over from the as-is build")
            items.append((m.group(1) if m else root, f"{what}: {key} {ver.strip()}".rstrip()))
        # 레시피가 compile로 바꾼 as-is provided scope를 재구성이 되돌린 경우도 항목으로 남긴다
        # "g:a: compile → provided (as-is scope, <pom>)" →
        # 'scope restored: provided (as-is): g:a (was compile)'
        for d in list(getattr(res, "scope_restored", None) or []):
            m = re.match(r"^(?P<key>\S+): (?P<old>\S+) → provided \(as-is scope, (?P<pom>[^()]+)\)\s*$", str(d))
            if m:
                items.append((m.group("pom").strip(),
                              f"scope restored: provided (as-is): {m.group('key')} (was {m.group('old')})"))
        # "<pom>: project.build.sourceEncoding=MS949 (N files)" → 'source encoding written: …'
        for d in list(getattr(res, "source_encodings_written", None) or []):
            pom, sep, what = str(d).partition(": ")
            items.append((pom if sep else root, f"source encoding written: {what.split(' (')[0]}"))
        # "<pom>: ext1, ext2 → maven-resources-plugin[, maven-war-plugin]" →
        # 'nonFilteredFileExtensions added to maven-resources-plugin in <pom>: ext1, ext2'
        for d in list(getattr(res, "non_filtered_extensions", None) or []):
            pom, sep, rest = str(d).partition(": ")
            exts, _, plugins = rest.partition(" → ")
            pom = pom if sep else root
            plugin_names = plugins.strip() or "maven-resources-plugin"
            items.append((pom, f"nonFilteredFileExtensions added to {plugin_names} in {pom}: {exts.strip()}"))
        # "<pom>: name=value" → 'property name=value removed: it overrides the Spring Boot <v> BOM's name'
        boot = str(getattr(getattr(self.cfg, "target", None), "spring_boot_version", "") or "")
        parent_names = set(getattr(getattr(self.cfg, "layout", None), "parent_property_names", None) or [])
        defined_by: dict[str, str] = dict(getattr(res, "bom_property_sources", None) or {})
        for d in list(getattr(res, "bom_properties_removed", None) or []):
            pom, sep, nv = str(d).partition(": ")
            name = nv.split("=", 1)[0]
            # 지운 속성이 누구의 값을 덮어썼는지 문장에 쓴다. 정의한 BOM이나 부모 체인을 알면 그것을 쓴다.
            # 설정 목록에 있는 이름이면 사내 부모를 쓴다. 둘 다 아니면 Boot BOM이다
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
        # "<pom>: g:a ${prop} (property of <pom>)" → 'BOM imported: g:a ${prop} (property of <pom>)'
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
            self.errors.add_pom_verify(pom, note, seen)
            log.info("%s: %s (pom-verify #%d)", pom, note, self.errors.size())

    def _jsp_jakarta_note(self, pr: PhaseResult, step: PlanStep, res: Any) -> None:
        """JSP의 javax EE 이름을 바꿨으면 파일 수와 참조 수는 단계 메모로, 파일 목록은 저널로 남긴다.
        RunReport에 담을 필드가 없어서, 보고서가 이 메모를 다시 읽어 숫자를 만든다. 예외를 던지지 않는다.

        Reader: `sbmig.report.handover.jsp_jakarta_counts`. Input fields `jsp_jakarta_files` / `_refs` /
        `_renamed`."""
        try:
            files = int(getattr(res, "jsp_jakarta_files", 0) or 0)
            refs = int(getattr(res, "jsp_jakarta_refs", 0) or 0)
            if files <= 0:
                return
            from sbmig.report.handover import jsp_jakarta_note
            pr.notes.append(jsp_jakarta_note(step.id, files, refs))
            self.ws.journal({"event": "jsp_jakarta", "step": step.id, "files": files, "refs": refs,
                             "renamed": list(getattr(res, "jsp_jakarta_renamed", None) or [])[:500]})
        except Exception as e:  # noqa: BLE001 - 보고서용 정보다. 실패해도 재구성은 실패로 보지 않는다
            log.debug("JSP rename note skipped: %s: %s", type(e).__name__, e)

    def _merge_placeholders(self, files: list[str], keys: list[str]) -> None:
        """재구성이 만든 플레이스홀더 파일과 키를 보고서의 properties 목록에 합친다. 순서는 지키고 중복은 뺀다.
        보고서에 목록이 아직 없으면 프로젝트 정보의 목록에서 시작한다."""
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
        """재구성기를 호출한다. ``params``를 받지 못하는 재구성기는 mode만으로 호출한다.
        모드가 ``layout``, ``msa_pom``, ``boot_pom``이면 params에 인자 없는 함수 두 개를 넣는다.
        재구성 뒤 effective pom을 읽는 ``effective_pom``과, 접수 때의 as-is 캡처를 읽는 ``asis_effective``다.

        The restructurer calls `effective_pom` only when it drops managed versions or has as-is versions to
        check."""
        assert self.profile is not None
        r = self.restructurer
        try:
            import inspect
            sig = inspect.signature(r.apply)
            takes_params = "params" in sig.parameters or any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
        except (TypeError, ValueError):  # pragma: no cover - builtins / mocks
            takes_params = True
        if mode in ("layout", "msa_pom", "boot_pom"):
            # as-is 버전을 이어받을 때는 drop_managed_versions가 꺼져 있어도 새 부모 체인이 관리하는지 봐야 한다
            # 그래서 재구성 뒤 effective pom을 항상 넘긴다. 이어받은 버전이 있을 때만 실제로 호출된다
            params.setdefault("asis_effective", self._asis_effective)
            params.setdefault("effective_pom", self._effective_pom_after)
        if takes_params:
            return r.apply(self.ws, self.profile, self.cfg, mode=mode, params=params)
        return r.apply(self.ws, self.profile, self.cfg, mode=mode)

    def _asis_effective(self) -> dict | None:
        """접수 때 캡처한 as-is effective pom(``logs/effective-depmgmt.json``)을 읽어 반환한다.
        재구성이 새 부모로 옮기면서 잃은 버전을 채울 때 쓴다.
        파일이 없거나 비었거나 읽을 수 없으면 None을 반환한다. 예외를 던지지 않는다."""
        try:
            load = lazy_attr("sbmig.build.effective", "load_effective", "G1 (build)")
            data = load(self.ws)
        except Exception as e:  # noqa: BLE001 - 캡처가 없으면 버전을 이어받지 않을 뿐이다
            log.debug("as-is effective pom unavailable: %s: %s", type(e).__name__, e)

            return None
        if not isinstance(data, dict) or not (data.get("managed") or data.get("direct")
                                              or data.get("by_module")):
            return None
        return data

    EFFECTIVE_AFTER_FILE = "depmgmt-after-restructure.json"

    def _effective_pom_after(self) -> Optional[dict]:
        """재구성기가 사내 parent를 붙인 뒤의 effective dependencyManagement를 캡처해 반환한다.
        as-is 캡처(``depmgmt.json``)를 덮어쓰지 않게 별도 파일에 저장한다.
        캡처할 수 없으면 None을 반환한다. 그러면 재구성기는 관리 버전 정리를 건너뛴다.

        None also for a custom build runner without a capture and for a capture / loader that takes no
        `name=`."""
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
        except TypeError as e:               # name을 받지 못하는 캡처는 depmgmt.json을 덮어쓴다. 쓰지 않는다
            self.ws.journal({"event": "effective_depmgmt_after", "skipped": f"capture without name=: {e}"})
            return None
        except Exception as e:
            log.warning("effective dependencyManagement (after restructure) capture failed: %s", e)
            self.ws.journal({"event": "effective_depmgmt_after", "ok": False, "error": f"{type(e).__name__}: {e}"})
            return None
        self.ws.journal({"event": "effective_depmgmt_after", "ok": bool(data), "path": str(path) if path else None})
        return data or None

    def _step_verify(self, pr: PhaseResult, step: PlanStep) -> bool:
        """검증 빌드 단계를 실행한다. ``mvn clean package -Dmaven.test.skip=true``를 실행하고 결과를 오류 목록에 반영한다.
        실패하면 수정 루프를 실행한 뒤 최종 빌드를 한 번 더 한다. 실행이 성공했는지는 이 최종 빌드로 정한다.
        - HTTP 차단 미러에 막혔으면 수정 루프 없이 중단한다
        - 멈춤 요청으로 루프를 나왔고 그 뒤 바뀐 파일이 없으면, 루프의 마지막 빌드를 최종 빌드로 쓴다"""
        self._flush_notes(pr)
        if self._dry_run:
            pr.notes.append(f"{step.id}: dry-run → final build skipped")
            return True
        if not self.cfg.loop.always_final_verify and self._phase_failed:
            pr.notes.append(f"{step.id}: skipped (an earlier phase failed and loop.always_final_verify=false)")
            return False
        self._mark_change_base(step)
        label = "verify" + (f"-{step.params['service']}" if step.params.get("service") else "")
        from .fixloop import build_target_args
        module, extra = build_target_args(self.profile, step.target)
        kw: dict[str, Any] = {"extra_args": extra} if extra else {}
        goal = step.build_goal or "clean package"

        def _skip_flags() -> tuple[bool, bool]:
            # 수정 루프가 테스트 건너뛰기로 바꿨을 수 있다. 빌드할 때마다 빌드 대상별로 다시 읽는다
            tc, ts = self._loop_skips(step.target)
            skip_tests = bool(getattr(self.cfg.build, "skip_test_compile", False)) or tc
            # 테스트 실행 실패만 남았으면 -DskipTests로 패키징한다. -Dmaven.test.skip=true가 있으면 그쪽이 우선이다
            skip_run = ts and not skip_tests
            return skip_tests, skip_run

        def _run_final() -> BuildResult:
            skip_tests, skip_run = _skip_flags()
            if skip_run:
                # skip_tests를 받지 않는 빌드 실행기에는 -DskipTests 인자로 넘긴다. 시그니처로 미리 봐서 빌드는 한 번만 한다
                if takes_keyword(self.build_runner.run, "skip_tests"):
                    return self.build_runner.run(goal, module=module, label=label, skip_test_compile=False,
                                                 skip_tests=True, **kw)
                return self.build_runner.run(goal, module=module, label=label, skip_test_compile=False,
                                             **{**kw, "extra_args": [*extra, "-DskipTests"]})
            return self.build_runner.run(goal, module=module, label=label, skip_test_compile=skip_tests, **kw)

        def _final_build() -> BuildResult:
            before = _status_paths(self.ws)
            res = refine_build(_run_final())           # 세분화한 케이스 키를 붙인다(sbmig.triage.cluster)
            _restore_build_byproducts(self.ws, before)
            return res

        res = _final_build()
        self._observe_build(goal, res, step.target)
        # Maven의 HTTP 차단 미러에 막혔으면 수정 루프(사전 시도, LLM)를 시작하지 않고 실행을 중단한다
        blocked_http = (not res.ok and self.loop is not None and not getattr(self, "_after_abort", False)
                        and self.loop.http_blocked_abort(pr, step.phase, res, 1, where=step.id))
        # 실패한 빌드를 수정 루프의 첫 빌드로 쓴다. 같은 상태를 다시 빌드하지 않고, 루프가 끝나면 다시 빌드한다
        if not res.ok and not blocked_http and self._verify_fix_loop(pr, step, res):
            if self.loop is not None and self.loop.aborted:
                pr.notes.append(f"{step.id}: final fix loop aborted the run ({self.loop.aborted}); the failed build stands")
            elif self.loop is not None and getattr(self.loop, "left_on_stop", False) \
                    and not getattr(self.loop, "end_rollbacks", 0) and getattr(self.loop, "last_build", None) is not None:
                # 멈춤 요청으로 루프를 빠져나왔고 그 뒤 바뀐 파일이 없으면 다시 빌드하지 않는다
                pr.notes.append(f"{step.id}: final build not repeated — the fix loop left on a stop request and the tree "
                                "is unchanged since its last build")
                log.info("stop requested: the fix loop's last build stands as the final build (tree unchanged)")
                res = self.loop.last_build
                self._observe_build(goal, res, step.target)
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
        # MSA는 서비스마다 최종 빌드가 있다. 앞 서비스가 실패했으면 그 결과를 남겨서 보고서가 실패를 보게 한다
        if self.report.final_build is None or self.report.final_build.ok:
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
        """수정 루프가 테스트를 건너뛰기로 했는지 (test-compile 건너뜀, 테스트 실행 건너뜀)으로 반환한다.
        MSA는 빌드 대상인 서비스별로 따로 본다."""
        lp = self.loop
        if lp is None:
            return False, False
        fn = getattr(lp, "skips_for", None)
        if callable(fn):
            try:
                tc, ts = fn(target)
                return bool(tc), bool(ts)
            except Exception:  # pragma: no cover - 다른 구현의 루프 객체
                pass
        return bool(getattr(lp, "test_compile_skipped", False)), bool(getattr(lp, "tests_skipped", False))

    def _verify_fix_loop(self, pr: PhaseResult, step: PlanStep, first: BuildResult) -> bool:
        """최종 빌드가 실패하면 ``loop.phases.verify``의 스크립트 수정 루프를 실행한다. 루프를 실행했으면 True다.
        이관에서 수정 루프가 실행되는 곳은 여기뿐이다. 최종 빌드는 호출한 쪽이 다시 한다.
        - 남은 케이스가 모두 이미 차단됐으면 실행하지 않는다
        - 수정 루프를 끈 프레임워크 프로젝트면 실행하지 않고, 남은 오류에 메모만 붙인다

        Invariant 1: the run still ends on a `clean package`. The loop's build 1 is the failed final build
        itself. A case blocked earlier stays blocked; per-case LLM cap `script.max_llm_tries_per_case`."""
        pc = self.cfg.loop.phases.get(Phase.VERIFY.value)
        if pc is None or not pc.enabled or pc.max_iterations <= 0 or self.loop is None or self._dry_run \
                or getattr(self, "_after_abort", False):
            return False
        if self.budget.time_exceeded():
            pr.notes.append(f"{step.id}: failed; no final fix loop (run time budget exhausted)")
            return False
        if self.no_fix_loop:
            # 프레임워크 프로젝트는 변환 뒤 한 번만 빌드한다. 남은 오류는 그대로 담당자에게 넘긴다
            n = self.errors.note_open(NO_FIX_LOOP_NOTE)
            pr.notes.append(f"{step.id}: failed; no fix loop (framework project, framework.fix_loop: false) — "
                            f"{first.error_count} error(s) stay for the owners")
            self.ws.journal({"event": "verify_loop_skipped", "step": step.id, "target": step.target,
                             "reason": "framework.fix_loop: false", "errors": first.error_count, "entries": n})
            log.info("final build failed with %d error(s); framework project (framework.fix_loop: false) → no fix "
                     "loop, the converted tree is delivered as it is", first.error_count)
            return False
        clusters = self.loop._cluster(list(first.errors), step.target)          # noqa: SLF001 - 같은 패키지
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
            # 테스트를 건너뛰어서 통과했으면, 그 테스트 오류는 담당자가 고칠 미해결 오류로 남긴다
            pr.remaining_errors += [e for e in res.remaining_errors if e not in pr.remaining_errors]
        self._merge_blocked(pr, res.blocked)
        self.ws.journal({"event": "verify_loop_end", "step": step.id, "build_ok": res.build_ok,
                         "attempts": len(res.attempts), "iterations": res.iterations,
                         "blocked": [b.id for b in res.blocked]})
        return True

    # ------------------------------------------------------------------ KB
    # 마무리에서 infra:repository를 판단할 때 보는 해석 오류 종류
    _RESOLUTION_KINDS = frozenset({ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.PARENT_RESOLUTION,
                                   ErrorKind.PLUGIN_RESOLUTION})

    def _advise_plan(self, plan):
        """규칙으로 세운 계획을 저렴한 레시피 모델에게 한 번 더 검토하게 한다. ``plan.advisor: llm``일 때만 한다(기본은 꺼짐).
        모델은 레시피를 추가만 할 수 있다. 실패하면 원래 계획을 반환한다."""
        if self.cfg.plan.advisor != "llm" or self._dry_run or self.profile is None:
            return plan
        # 변환만 하는 프레임워크 프로젝트는 LLM을 호출하지 않는다
        if bool(getattr(self.profile, "reduced_scope", False)) and \
                not bool(getattr(getattr(self.profile, "framework", None), "fix_loop", False)):
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
        except Exception as e:  # 계획 조언은 없어도 된다
            log.warning("plan advisor failed: %s", e)
            self.ws.journal({"event": "plan_advice", "error": f"{type(e).__name__}: {e}"})
        return plan

    # ------------------------------------------------------------------ 오류 목록
    def _start_error_list(self, dry_run: bool) -> None:
        """실행의 오류 목록(``RunErrors``)을 연다. 빌드 오류, pom 수정, 검토 결과, 수정 시도가 여기에 쌓인다.
        dry-run은 아무것도 빌드하지 않으니 빈 목록으로 둔다."""
        self.errors = RunErrors(None)
        if dry_run:
            return
        project = ((self.profile.name if self.profile is not None else "") or
                   (self.report.project_name if self.report is not None else "") or self.ws.root.name)
        self.errors = RunErrors.create(self.ws, self.cfg, project, factory=self._error_list_factory)

    def _observe_build(self, goal: str, res: BuildResult, target: Optional[str]) -> None:
        """최종 빌드 결과를 오류 목록에 반영하고 로그 한 줄을 남긴다.
        새 오류는 추가한다. 더 이상 나오지 않는 열린 항목은 해결로 닫는다. pom-verify 항목도 확인한다.
        타임아웃이거나 오류를 하나도 읽지 못한 실패 빌드면 아무 항목도 닫지 않는다. 오류가 안 보인다고 해결된 것은 아니다.

        Log line shape: `mvn clean package: 25 error(s), 새 에러 2건 추가, pom-verify 1건 resolved`."""
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

    def _no_fix_loop_followup(self, report: RunReport) -> None:
        """수정 루프 없이 실행한 프레임워크 프로젝트의 빌드가 실패했으면, 인수인계 앞쪽에 한 줄 넣는다.
        ADF 줄과 소스 없는 reactor 줄 바로 뒤에 들어간다."""
        if not self.no_fix_loop or not any(not r.ok for r in self._verify_results):
            return
        line = no_fix_loop_followup(len(report.unresolved))
        if line not in report.manual_followups:
            report.manual_followups.insert(self._first_lines_end(report), line)

    def _finalize_errors(self, report: RunReport) -> None:
        """오류 목록을 마무리한다.
        열린 항목은 unresolved로 닫는다. errors.jsonl과 errors.md를 쓰고, 보고서의 오류 항목을 채우고, KB에 기록한다.
        ``NullKnowledgeBase``면 KB에는 기록하지 않는다. 실패해도 실행은 실패로 보지 않고 플래그 ``errors-failed: …``만 남긴다.

        Calls `end_of_run`, `write`, `to_report` (diffs capped) and `kb.record_run`."""
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
                    except Exception as e:  # noqa: BLE001 - KB는 예외를 던지지 않지만 방어적으로 받는다
                        log.warning("kb.record_run failed: %s: %s", type(e).__name__, e)
                        flag(f"errors-failed: kb: {type(e).__name__}: {e}")
            self.ws.journal({"event": "error_list", "counts": counts, "entries": len(report.error_entries),
                             "kb_recorded": kb_n})
            log.info("error list: %d entr%s (%s)%s", len(report.error_entries),
                     "y" if len(report.error_entries) == 1 else "ies",
                     ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "-",
                     f"; {kb_n} recorded in the KB" if kb_n is not None else "")
        except Exception as e:  # noqa: BLE001 - 오류 목록 때문에 실행이 실패하면 안 된다
            log.exception("error list failed")
            flag(f"errors-failed: {type(e).__name__}: {e}")

    def _finalize_pomless(self, report: RunReport) -> None:
        """sbmig가 만든 최소 pom으로 빌드한 라이브러리가 있으면 플래그 ``pomless-library``와 인수인계 한 줄을 남긴다.
        이번 실행이 만든 것과 앞선 실행이 만든 것을 모두 넣는다. Maven은 호출하지 않는다. 예외를 던지지 않는다.

        This run's: the workspace memo of `sbmig.build.pomless`; earlier runs':
        `stubs_used(known_coordinates(ws))`, journal `pomless_stub_used`."""
        try:
            from sbmig.build.pomless import FLAG, handover_line, known_coordinates, memo_for, stubs_used
            libs = list(dict.fromkeys(f"{s['ga']}:{s['version']}" for s in memo_for(self.ws).stubs))
            # 앞선 실행이 만든 최소 pom으로 조용히 빌드된 라이브러리도 같은 줄에 넣는다
            for used in stubs_used(self.cfg, known_coordinates(self.ws), skip=set(libs)):
                lib = f"{used['ga']}:{used['version']}"
                if lib not in libs:
                    libs.append(lib)
                    self.ws.journal({"event": "pomless_stub_used", **used})
                    log.info("maven: %s builds on a minimal pom an earlier run wrote (%s) — the repository has no pom "
                             "for it", lib, used["path"])
            if not libs:
                return
            self._flag(FLAG)
            if FLAG not in report.flags:
                report.flags.append(FLAG)
            line = handover_line(libs)
            if line not in report.manual_followups:
                report.manual_followups.append(line)
        except Exception as e:  # noqa: BLE001 - 안내 때문에 실행을 멈추지 않는다
            log.debug("pomless note failed: %s: %s", type(e).__name__, e)

    def _flag(self, flag: str) -> None:
        if self.report is not None and flag not in self.report.flags:
            self.report.flags.append(flag)

    def _finalize_operator_checks(self, report: RunReport) -> None:
        """보고서의 "운영자 확인 필요" 항목을 만든다. 예외를 던지지 않는다.
        수정 시도와 구조 검토가 만든 메이저 업그레이드와 라이브러리 교체마다 하나씩 만든다. 플래그 ``major-upgrade``를 붙인다.
        최종 결과에 남은 변경만 넣는다. 같은 (pom, 라이브러리, 버전)은 마지막 시도의 것 하나만 남긴다.

        Result in `report.operator_checks`; filtering by `still_applied` (see `_operator_checks`)."""
        try:
            checks = self._operator_checks(report)
        except Exception as e:  # noqa: BLE001 - 이것 때문에 보고서를 잃으면 안 된다
            log.warning("operator checks failed: %s: %s", type(e).__name__, e)
            return
        report.operator_checks = checks
        if checks:
            self._flag("major-upgrade")
            log.warning("운영자 확인 필요: 메이저 업그레이드 %d건 (report Handover \"운영자 확인 필요\")", len(checks))
            self.ws.journal({"event": "operator_checks", "count": len(checks),
                             "changes": [c.change.text for c in checks]})

    def _operator_checks(self, report: RunReport) -> list:
        """운영자 확인 항목을 모아 반환한다.
        구조 검토 커밋 -> 오류 목록의 시도(라운드, 번호 순) 순서로 모은다. 그중 최종 결과에 남은 것만 ``still_applied``로 거른다.
        적용되지 않았거나, 악화되어 되돌렸거나, 적용에 실패한 시도는 뺀다. 오류 목록이 없으면 FixAttempt 기록을 쓴다."""
        from sbmig.core.models import OperatorCheck

        from .fixloop import ROLLED_BACK_NOTE
        by_commit = {a.commit: a for pr in report.phases for a in pr.attempts if a.commit}
        found: dict[tuple, OperatorCheck] = {}

        def add(ch: Any, **kw: Any) -> None:
            key = (ch.path, ch.key, ch.after)
            found.pop(key, None)                    # 같은 변경은 마지막 시도의 가이드와 커밋을 쓴다

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
        else:                                       # 오류 목록이 없으면 같은 정보를 가진 시도 기록을 쓴다
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
        """그 변경이 최종 결과에 아직 남아 있으면 True를 반환한다.
        판단하지 못하면 남은 것으로 본다. 놓치는 것보다 한 번 더 보여 주는 편이 낫다.

        `sbmig.build.depchanges.still_applied`."""
        try:
            from sbmig.build.depchanges import still_applied
            return bool(still_applied(self.ws, change))
        except Exception as e:  # noqa: BLE001
            log.debug("still_applied(%s) unavailable: %s: %s", getattr(change, "key", "?"), type(e).__name__, e)
            return True

    # ------------------------------------------------------------------ 마무리
    def _finalize_blocked(self, report: RunReport, unresolved: list[BuildError]) -> None:
        """보고서의 차단 목록을 만든다. 이 실행의 모든 차단 항목을 id 기준으로 중복 없이 모은다.
        최종 빌드가 성공했으면 오류가 더 이상 보이지 않는 항목은 뺀다.
        다만 테스트를 건너뛰어서 통과한 테스트 오류 항목은 남긴다."""
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
        """javax jar 목록을 만드는 함수를 반환한다. 꺼져 있으면 None을 반환한다.
        주입된 함수가 있으면 그것을 쓴다. False가 주입됐으면 끈다.
        기본 구현 ``sbmig.build.deptree``는 mvn을 실행하므로 실제 Maven일 때만 쓴다."""
        if self._javax_inventory is False:
            return None
        if self._javax_inventory is not None:
            return self._javax_inventory
        if not self._real_maven:
            return None
        from sbmig.build.deptree import javax_api_inventory
        return javax_api_inventory

    def _finalize_javax_inventory(self, report: RunReport) -> None:
        """최종 빌드 뒤 classpath에 남은 javax EE API jar를 ``mvn dependency:tree``로 찾는다.
        그 jar를 끌어오는 직접 의존성도 찾는다. 보고서 섹션, 플래그 ``javax-api-on-classpath``, 후속 조치를 남긴다.
        javax 관련 차단 항목에는 원인 의존성(via)과 그 라이브러리를 바꾸라는 제안을 붙인다. 실패해도 실행에는 영향이 없다.

        Skipped when no jakarta migration phase ran (`Phase.SPRING` / `Phase.TRANSFORM`) or
        `report.javax_api_inventory` is off."""
        if not getattr(self.cfg.report, "javax_api_inventory", True) or self._dry_run:
            return
        if not any(pr.phase in (Phase.SPRING, Phase.TRANSFORM) for pr in report.phases):
            return                                   # jakarta 변환을 하지 않았으면 찾을 것이 없다
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
            # 프레임워크 프로젝트에서 javax jar가 프레임워크 아티팩트를 거쳐 들어왔으면 제안 문구에 그 아티팩트를 넣는다
            from .fixloop import framework_via_suggestions
            framework_via_suggestions(self.blocked.items(), self.profile, self.cfg)
        except Exception as e:  # pragma: no cover - 보고서는 반드시 남긴다
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
        """라이브러리 권고를 마무리한다. 최종 결과에서 다시 확인하고 후속 조치와 인수인계 섹션을 만든다.
        예외를 던지지 않는다. 종료 코드도 바꾸지 않는다.

        Re-detection sets each finding's `outcome`; the Handover = versions replaced + "from here on, your
        team"."""
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
        except Exception as e:  # pragma: no cover - build_handover는 예외를 던지지 않는다
            log.warning("handover failed: %s", e)
            return
        h = report.handover
        self.ws.journal({"event": "handover", "changes": len(h.changes), "carried_over": len(h.carried_over),
                         "from_here": len(h.from_here)})

    @staticmethod
    def _attribute_llm_fixes(report: RunReport, findings: list) -> None:
        """권고 힌트를 넣고 성공한 LLM 수정이 있으면, 그 수정이 바꾼 파일과 원인 첫 문장을 권고에 붙인다.
        인수인계에서 LLM이 그 권고를 어떻게 처리했는지 보여 주기 위해서다. 인수인계를 만들기 전에 호출한다.

        Successful = `FixAttempt.advisory_ids` set, a script decision, applied, outcome improved / build_ok,
        cluster not left unresolved. Fills `AdvisoryFinding.llm_fixed_files` / `llm_fix_notes`."""
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
        """수정 시도, 계획 조언, 구조 검토의 LLM 사용량을 모델별로 모아 반환한다. 보고서의 비용 표에 쓴다."""
        by: dict[str, LLMUsage] = {}

        def acc(u: Optional[LLMUsage], fallback: str) -> None:
            if u is None or not (u.calls or u.input_tokens or u.output_tokens):
                return
            # 행 이름은 요청한 모델이다. 비었거나 `unknown` 같은 값이면 시도의 모델과 설정의 모델 순으로 쓴다
            key = asked_model_name(u.model, fallback, self.cfg.llm.model)
            by[key] = by.get(key, LLMUsage(model=key)).add(
                u.model_copy(update={"model": key, "served": served_beside(key, u.served)}))
        for pr in report.phases:
            for a in pr.attempts:
                acc(a.llm_usage, a.model or self.cfg.llm.model)
        if plan_usage is not None:
            acc(plan_usage, self.cfg.llm.recipe_model or self.cfg.llm.model)
        for k, u in self._review_usage.items():
            acc(u, k or self.cfg.llm.model)
        return list(by.values())

    def _infra_flags(self, report: RunReport) -> None:
        """인프라 문제로 끝난 실행에 ``infra:<종류>`` 플래그를 붙인다.
        플릿은 이 플래그를 보고 멈춘다. 다음 프로젝트까지 연달아 실패시키지 않기 위해서다.

        Kinds (fleet circuit breaker):
        - `infra:llm`: llm-unavailable / llm-config-error and the run did not succeed
        - `infra:maven`: mvn-not-found or settings-not-found; `infra:jdk`: jdk-not-21 or Maven failing on
          JAVA_HOME
        - `infra:http-blocked` (first, instead of repository): a real Maven HTTP blocker hit
        - `infra:repository`: parent not resolvable, or every remaining error is a resolution error whose text
          points at the repository / network (`_INFRA_REPO_RE`)
        - `infra:disk`, `infra:file-lock`: a failed final build on a full disk / a Windows file lock under
          target/"""
        flags = report.flags
        add: list[str] = []
        if ("llm-unavailable" in flags or "llm-config-error" in flags) and not report.success:
            add.append("infra:llm")                 # 그래도 성공한 실행은 그 이유로 끝난 것이 아니다
        if "mvn-not-found" in flags:
            add.append("infra:jdk" if self._mvn_java_problem else "infra:maven")
        if "jdk-not-21" in flags:
            add.append("infra:jdk")
        if "settings-not-found" in flags:
            add.append("infra:maven")
        # HTTP 차단 미러는 운영자가 Maven settings를 고쳐야 한다. infra:repository와 따로 표시한다
        http_blocked = "aborted:http-blocked" in flags or (not report.success and self._http_blocker_trouble())
        if http_blocked:
            add.insert(0, "infra:http-blocked")
        elif "aborted:parent-not-found" in flags or self._repository_trouble():
            add.append("infra:repository")
        if not report.success and self._disk_full():
            add.append("infra:disk")                # 플릿이 다음 프로젝트까지 실패시키지 않고 멈추게 한다
        # 다시 시도해도 target/ 파일 잠금으로 실패했으면 빌드 PC 환경 문제다. 플릿이 나중에 다시 실행하게 한다
        if not report.success and self._file_lock_trouble():
            add.append("infra:file-lock")
        for f in add:
            if f not in flags:
                flags.append(f)

    def _file_lock_trouble(self) -> bool:
        """실패한 최종 빌드 중 하나라도 Windows 파일 잠금으로 끝났으면 True를 반환한다."""
        from sbmig.build.env_errors import file_lock_hit

        return any(file_lock_hit(f"{e.message}\n{e.raw}", self.cfg) is not None for r in self._verify_results
                   if not r.ok for e in r.errors)

    def _http_blocker_trouble(self) -> bool:
        """실패한 최종 빌드 중 하나라도 Maven의 HTTP 차단 미러에 막혔으면 True를 반환한다."""
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

    def _finalize_code_changes(self, report: RunReport) -> None:
        """자동 변환 뒤에 고친 파일 목록(``report.code_changes``)을 채운다. 시작점이 없으면 빈 목록으로 둔다.

        The base is the commit `_mark_change_base` took."""
        base = "" if self._dry_run else (getattr(self, "_change_base", "") or "")
        try:
            from sbmig.report.changes import collect_code_changes
            collect_code_changes(self.ws, report, base, self.cfg)
        except Exception as e:  # noqa: BLE001 - 이 목록 때문에 보고서를 잃으면 안 된다
            log.warning("code changes not collected: %s", e)

    def _finalize_code_diff(self) -> None:
        """웹 화면의 「코드 변경」 기록(``codediff/``)을 쓴다. dry-run이면 건너뛴다.
        실패하면 경고 로그만 남긴다. 예외를 던지지 않는다.

        `report.code_diff: false` and a project without `.git` are `sbmig.report.codediff.write_code_diff`'s
        to handle (journal `code_diff_skipped`, removes an earlier record in the same run folder). Not listed
        in `report.outputs`."""
        if self._dry_run:
            return
        try:
            from sbmig.report.codediff import write_code_diff
            write_code_diff(self.ws, self.cfg, run_id=self.ws.run_id)
        except Exception as e:  # noqa: BLE001 - 코드 변경 화면 때문에 실행이 실패하지 않게 한다
            log.warning("code diff not written: %s", e)

    def _finalize(self, report: RunReport) -> None:
        """실행을 마무리한다. ``_run_guard``가 어떤 경우에도 호출한다.
        순서: LLM 사용량과 비용 -> 수정 루프의 플래그와 후속 조치 -> 성공 판단 -> 미해결 오류와 차단 목록
        -> infra 플래그 -> 운영자 확인 -> 권고와 인수인계 -> 오류 목록과 KB -> 보고서 -> patch와 zip.
        오래 걸리는 patch와 zip은 마지막에 만든다."""
        if self._flow == FLOW_REMEDIATION:
            self._finalize_remediation(report)
            return
        report.finished_at = utcnow()
        usage = LLMUsage()
        for pr in report.phases:
            for a in pr.attempts:
                usage = usage.add(a.llm_usage)
        for u in self._review_usage.values():      # 구조 검토
            usage = usage.add(u.model_copy(update={"model": usage.model}))
        usage.calls = max(usage.calls, self.budget.llm_calls)
        plan_usage = getattr(getattr(self, "plan", None), "llm_usage", None)
        if plan_usage is not None and plan_usage.calls:
            usage = usage.add(plan_usage)          # 계획 조언(레시피 모델) 호출
        report.llm_usage_by_model = self._usage_by_model(report, plan_usage)
        if usage.calls or usage.input_tokens or usage.output_tokens:
            # 모든 호출이 실패해 행이 없는 실행도 이름이 남도록 합계에도 요청한 이름을 둔다
            first = next((a.model for pr in report.phases for a in pr.attempts if real_model_name(a.model)), "")
            rows = report.llm_usage_by_model
            usage = usage.model_copy(update={
                "model": asked_model_name(usage.model, rows[0].model if rows else "", first, self.cfg.llm.model),
                "served": merge_model_names(*(r.served for r in rows))})
        report.llm_usage = usage
        try:
            from sbmig.report.cost import compute_llm_cost
            report.llm_cost = compute_llm_cost(report.llm_usage_by_model, self.cfg)
        except Exception as e:  # pragma: no cover - 비용 계산 때문에 실행이 실패하지 않게 한다
            log.warning("llm cost not computed: %s", e)
        if self.loop is not None:
            for f in self.loop.flags:
                if f not in report.flags:
                    report.flags.append(f)
            report.manual_followups += [f for f in self.loop.followups if f not in report.manual_followups]
        self._classify_bundled_jars(report)      # 보고서용으로 관리되는 g:a의 동봉 jar에 superseded를 붙인다
        # 호출 상한(loop.max_llm_calls > 0)에 닿았거나, -1(플릿 전체 예산 소진)인데 LLM이 필요한 케이스가 있었다
        if (self.budget.llm_limited and self.budget.llm_exhausted()) or (
                self.budget.llm_off and any(it.reason.value == "budget_exhausted" for it in self.blocked.items())):
            self._flag("llm-budget-exhausted")
        built = bool(self._verify_results) or any(pr.iterations for pr in report.phases)
        if getattr(self.cfg.build, "skip_test_compile", False) and not self._dry_run and built:
            # 빌드를 한 번이라도 했을 때만 남긴다. 빌드 전에 중단된 실행에는 이 후속 조치가 없다
            self._flag("tests-not-compiled")
            fu = ("test sources were neither compiled nor run (build.skip_test_compile: true): the owning team "
                  "compiles and fixes the tests (`mvn test-compile`) before relying on CI")
            if fu not in report.manual_followups:
                report.manual_followups.append(fu)
        # 중단된 실행은 성공이 아니다. 계획한 최종 빌드가 모두 실행되어 모두 성공해야 성공이다
        # MSA에서 일부 서비스만 통과했거나 소스 없는 reactor면 빌드가 통과해도 성공이 아니다
        report.success = self.build_succeeded(report)
        # 미해결 오류는 as-is 담당자가 봐야 하는 오류다
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
        except Exception as e:  # 보고서는 반드시 남긴다
            log.exception("finalizing the blocked items failed")

            report.flags.append(f"blocked-finalize-failed: {e}")
        report.unresolved = [e for e in unresolved if not self.blocked.is_blocked_error(e)]
        self._no_fix_loop_followup(report)
        # 변환 실패 줄은 프레임워크 줄보다 앞에 와야 한다. 그래서 나중에 같은 위치에 넣는다
        self._rewrite_failed_followup(report)
        self._finalize_pomless(report)
        self._infra_flags(report)
        out = report.outputs
        out["run_dir"] = str(self.ws.run_dir)
        out["project_dir"] = str(self.ws.root)
        out["journal"] = str(self.ws.journal_path)
        out["logs"] = str(self.ws.logs_dir)
        for name in ("profile.json", "plan.json"):
            if (self.ws.run_dir / name).exists():
                out[name.replace(".json", "")] = str(self.ws.run_dir / name)
        # zip을 만들다 중단되어도 중요한 결과는 남아야 한다. KB 기록과 보고서를 먼저 쓰고 patch와 zip은 마지막에 만든다
        # 경로는 미리 보고서에 넣고, 만들지 못하면 다시 뺀다
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
        self._finalize_code_changes(report)
        writer = self._report_writer
        if writer is None:
            from sbmig.report import write_report
            writer = lambda r, w: write_report(r, w, cfg=self.cfg)      # noqa: E731 - 설정의 report.language를 쓴다

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
        if dropped:                        # 보고서가 없는 파일을 가리키게 된다. 한 번 더 쓴다

            write()
        self._finalize_code_diff()
        self.ws.journal({"event": "report", "success": report.success, "flags": report.flags,
                         "unresolved": len(report.unresolved), "blocked": len(report.blocked),
                         "llm_calls": report.llm_usage.calls,
                         "outputs": out})
