"""The build-fix loop (ARCHITECTURE §3) — v2 (flow-v2): the script fix loop (docs/INTERFACES.md "Script fix loop (L)").

Invariant: every applied change is followed by a real build. A loop round is
    build → score previous attempts (revert regressions, credit the scripts) → stop on BUILD SUCCESS → cluster by
    case → decide → run the case's fix script + commit
and the loop only exits *after* a build: on success, when the fix rounds are used up (the last round's changes are
still built and scored), when nothing actionable is left, when only blocked clusters remain, or when the run's time
budget is exceeded.

v2 fix scripts: the errors of a build are clustered by **case** (L2 signature) across files (`Triager.cluster`,
`sbmig.triage.cluster_cases`; ids `case:<case_id>`, `@<target>` in a targeted MSA loop) and every case gets at most
one script attempt per fix round, by the ladder of `sbmig.triage.scripts` (reuse a proven stored version without an
LLM call → augment the last version → a new script → BLOCKED repeated_failure). A script attempt:
`ScriptRunner.run(ws, code, errors=case errors)` (a separate `python -I` process, `sbmig.script`); status rejected /
error / timeout → `apply_failed` with the working tree restored (no partial application); ok without a change →
`apply_failed` "no file changed"; ok with changes → commit `fix(script:<case_id>:v<N> <mode>): <cluster id>`. Every
LLM script is stored in the KB (`kb.scripts.add`) BEFORE it runs, so failures keep their lineage; after scoring each
script attempt is credited (`kb.scripts.credit`). **Rollback before the next try**: before a case's next attempt, its
previous attempt is rolled back (reset when it is HEAD, else `git revert`) unless it was improved / build_ok (a
regressed one was already reverted by scoring) — every version is measured from the same base, so a stored version
is complete; the errors that previous attempt had removed come back into the case (and into the next scoring's
baseline, so they never read as "new"). Handlers, KB recipes / patches, case-replace and SEARCH/REPLACE are not part
of the v2 flow (`sbmig.triage.handlers` stays until PLAN-v2 step 4).

Loop safety (docs/ERROR-HANDLING.md §3): blocked clusters (run-wide `BlockedRegistry`) are skipped in every later
iteration and phase; oscillation (A↔B↔A) counts as a stall and, when it repeats, blocks the involved clusters;
structural kinds (pom_invalid / parent / plugin resolution) appearing after a pom edit revert that attempt regardless
of error counts; two LLM transport/config failures in a row pause the loop once per run (`llm.cooldown_seconds`,
round 9) and only a second such streak marks the LLM unavailable for the run.

Merge-conflict hints (round 9): `merge_conflicts` (set by the orchestrator after the restructure step) — a cluster whose
file is a first-wins merged file, or whose errors name its class, gets a note plus the dropped copy as read-only
reference text in the fix prompt (`FixContext.reference_notes` / `reference_files`).

Scoring (round 10, `_score`): "not visible" is not "resolved". An attempt is `improved` only when every targeted error
is gone AND the build reached the stage that reports them; a timed-out build, a failed build without parsed errors,
or a build that stopped earlier (dependency resolution, …) makes it `inconclusive` (no KB credit) — unless the
attempt edited a pom and the earlier-stage errors are new, which is a structural regression (reverted). `partial` =
some targeted errors gone, the rest remain, nothing new in the touched files. New errors in a touched file are
counted by (file, signature) occurrences, not by L1 signature alone (the same L1 in another file hid them).
"""
from __future__ import annotations

import contextvars
import logging
import re
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

from sbmig.core.config import Config
from sbmig.core.interfaces import FixContext, TriageContext
from sbmig.core.models import (GAV, AdvisoryFinding, BlockReason, BuildError, BuildResult, DecisionKind, ErrorCluster,
                               ErrorKind, FileChange, FixAttempt, IterationRecord, KBMatch, MergeConflict, Phase,
                               PhaseResult, ProjectProfile, ScriptBase, ScriptRunResult, TriageDecision, utcnow)
from sbmig.core.workspace import Workspace

from .blocked_registry import BlockedRegistry, attribute, compose_suggestion, sanitize_errors
from .budget import LoopBudget
from .components import ScriptFixerMissing, profile_summary
from .error_tracker import ROLLED_BACK_NOTE, _SYNTHETIC_PREFIXES, STAGE, fmt_error   # one definition each

log = logging.getLogger("sbmig.loop")

TEST_COMPILING_GOALS = ("test-compile", "package", "verify", "install", "deploy", "test")
TEST_RUNNING_GOALS = ("test", "package", "verify", "install", "deploy")
RESOLUTION_PREFIXES = ("parent_resolution:", "pom_invalid:", "dependency_resolution:", "plugin_resolution:")
# fixer analysis of a transport/config failure (vs. a protocol-garbage response, which is the model's fault). Round 10:
# only explicit markers — the HTTP status the client reports (`status=<code>`: auth, endpoint 404, timeouts, rate
# limit after the retries, 5xx; `status=None` = no HTTP answer at all), the transport exception (`cause=<Class>` or
# the class name itself) and the known config phrases. The old list matched `config`, bare 5xx-looking numbers
# (`line 512`) and paths, so a Windows `PermissionError [WinError 32] … config …` marked the LLM unavailable.
_LLM_TRANSPORT_RE = re.compile(
    r"(?i)\bstatus=(?:none|40[134]|408|429|5\d\d)\b|\bcause=[A-Za-z_][\w.]*|response_path|invalid json|"
    r"cannot find the reply text|not found in the response|api[_ ]?key\b[^.]*\bnot set|needs an api key|"
    r"\bllm[._][\w.]*\s+is not set|unknown llm\.provider|cannot be imported")
_LLM_TRANSPORT_CLASS_RE = re.compile(      # case-sensitive: exception class names, not the English words
    r"\b(?:ConnectionError|ConnectTimeout|ConnectTimeoutError|ConnectError|ReadTimeout|ReadTimeoutError|Timeout|"
    r"TimeoutError|TimeoutException|WriteTimeout|PoolTimeout|ProxyError|SSLError|TransportError|RemoteProtocolError|"
    r"RemoteDisconnected|ChunkedEncodingError|ConnectionResetError|ConnectionRefusedError|ConnectionAbortedError|"
    r"NewConnectionError|MaxRetryError|NameResolutionError|LLMHardDeadline|LLMConfigError|MissingComponentError|"
    r"PluginLoadError)\b")
# the gateway rejected *this request* for its content (too long, malformed, cut by the token limit): the prompt's
# fault, not the wire's — such a failure must not count towards "LLM unavailable for the run"
_LLM_REQUEST_REJECTED_RE = re.compile(r"status=4(?:00|13|22)\b|content[ _-]?filter|"
                                      r"finish_reason=(?:length|max_tokens)\b")
# a 400 that says the prompt is too big: different prompts may well get the same text — never a configuration error
_LLM_PROMPT_TOO_BIG_RE = re.compile(r"(?i)context[ _-]?length|too (?:long|large|many tokens)|"
                                    r"max(?:imum)?[ _-]?(?:context|tokens?|input)|token limit|payload too large|"
                                    r"finish_reason=(?:length|max_tokens)")
_LLM_400_BODY_RE = re.compile(r"status=400\)?:?\s*(?P<body>.*)", re.S)
LLM_CONFIG_ERROR_PROMPTS = 2         # the same HTTP 400 body for this many different prompts → configuration error
_BLOCKED_RAT_RE = re.compile(r"blocked:\s*(?P<r>[a-z_]+)")
REFERENCE_MAX_CHARS = 12_000         # one dropped merge copy in the fix prompt (round 9)
WATCHDOG_S = 30.0                    # the fix loop's watchdog wakes up this often …
WATCHDOG_QUIET_S = 60.0              # … and warns when no milestone was reached for this long (operators: stuck?)
WATCHDOG_DUMP_S = 300.0              # … and writes a thread dump into the run dir by itself after this long (Windows has no SIGUSR1)
REFERENCE_MAX_FILES = 3              # dropped copies per cluster
_TRUNCATED_MARK = "\n… (truncated)"
SCRIPT_KEEP = frozenset({"improved", "build_ok"})   # a case's previous script attempt is kept (not rolled back)
NO_ROLLBACK = frozenset({"improved", "build_ok", "reverted", "pending", "apply_failed", "blocked"})
SCRIPT_BASE_LOG = 50                 # ScriptBase.log lines (augment prompt)
SCRIPT_HISTORY = 10                  # ScriptBase.history lines
FIXER_WITHOUT_SCRIPTS_FLAG = "fixer-without-scripts"


def sleep_seconds(seconds: float) -> None:
    """The LLM cool-down pause (module-level so a test conftest can replace it for every loop)."""
    time.sleep(seconds)


def _first_error_line(c: ErrorCluster, limit: int = 220) -> str:
    """`file:line: message` of the cluster's first error for the progress log (operators read sbmig.log only)."""
    if not c.errors:
        return "-"
    e = c.errors[0]
    loc = (e.file or "-") + (f":{e.line}" if e.line else "")
    text = f"{loc}: {(e.message or e.raw or '').strip()}"
    if e.symbol and e.symbol not in text:
        text += f" (symbol {e.symbol})"
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _first_sentence(text: str, limit: int = 160) -> str:
    """The LLM's one-line verdict (its <analysis> first sentence, or the llm-error text)."""
    t = " ".join(text.strip().split())
    for sep in (". ", "。", "\n"):
        if sep in t:
            t = t.split(sep, 1)[0] + ("." if sep == ". " else "")
            break
    return t if len(t) <= limit else t[:limit - 1] + "…"


def _head(c: ErrorCluster) -> str:
    """'case abc1234567 (compile, 7 error(s) in 4 file(s))' — the progress-log name of a cluster."""
    if c.case_id:
        name = c.id[len("case:"):] if c.id.startswith("case:") else c.id
        files = len({(e.file or "").replace("\\", "/") for e in c.errors if e.file})
        return (f"case {name} ({c.kind.value}, {len(c.errors)} error(s)"
                + (f" in {files} file(s))" if files else ")"))
    return f"cluster {c.id} ({c.kind.value}, {len(c.errors)} error(s){', ' + c.file if c.file else ''})"


def _describe_decision(d: TriageDecision, c: ErrorCluster, history: list[FixAttempt], cfg: Config,
                       step: Any = None) -> str:
    """One human line per decision for the progress log ("reuse: stored script v2 …", "augment 2/3 …", …)."""
    k = d.kind
    if k == DecisionKind.SCRIPT:
        if step is not None and getattr(step, "needs_llm", False):
            return (f"{d.rationale} — asking the LLM for a fix script (a call can take up to "
                    f"{cfg.llm.timeout_seconds}s × {cfg.llm.max_retries + 1} tries — quiet until it answers)")
        return d.rationale or "fix script"
    if k == DecisionKind.HANDLER:
        return f"deterministic handler {d.handler_id or '?'}"
    if k == DecisionKind.KB_PATCH:
        return f"replaying KB patch {', '.join(d.kb_case_ids) or '?'} (no LLM)"
    if k == DecisionKind.KB_RECIPE:
        return f"KB recipe(s) {', '.join(d.recipe_ids) or '?'} (OpenRewrite, no LLM)"
    if k == DecisionKind.LLM_FIX:
        n = sum(1 for a in history if a.cluster_id == c.id and a.decision.kind == DecisionKind.LLM_FIX) + 1
        return (f"asking the LLM (attempt {n}/{cfg.loop.max_llm_attempts_per_cluster} for this cluster; a call can "
                f"take up to {cfg.llm.timeout_seconds}s × {cfg.llm.max_retries + 1} tries — quiet until it answers)")
    if k == DecisionKind.BLOCKED:
        return d.rationale or "blocked"
    if k == DecisionKind.ESCALATE:
        return "giving up on this cluster (" + (d.rationale or "escalated") + ")"
    if k == DecisionKind.SKIP:
        return "skipped this round (" + (d.rationale or "masked by another cluster") + ")"
    return d.rationale or k.value


def is_llm_transport_error(analysis: str) -> bool:
    """`llm-error: …` of a transport / configuration failure (counts towards the cool-down / "LLM unavailable")."""
    a = (analysis or "").strip()
    if not a.lower().startswith("llm-error:"):
        return False
    if "did not follow the protocol" in a:
        return False
    if _LLM_REQUEST_REJECTED_RE.search(a):
        return False
    return bool(_LLM_TRANSPORT_RE.search(a) or _LLM_TRANSPORT_CLASS_RE.search(a))


def llm_400_body(analysis: str) -> Optional[str]:
    """The short body of an HTTP 400 `llm-error` (whitespace collapsed, ≤ 200 chars), or None — None as well for a
    400 that says the prompt is too big (two big prompts legitimately get the same text)."""
    a = (analysis or "").strip()
    if not a.lower().startswith("llm-error:") or "status=400" not in a or _LLM_PROMPT_TOO_BIG_RE.search(a):
        return None
    m = _LLM_400_BODY_RE.search(a)
    body = " ".join((m.group("body") if m else "").split())
    body = body.split(" [sbmig] ", 1)[0]
    return body[:200] or "(empty body)"


def build_target_args(profile: Optional[ProjectProfile], target: Optional[str]) -> tuple[Optional[str], list[str]]:
    """(module, extra_args) for building `target`. When a root aggregator pom lists the target among its modules, build
    from the root with `-pl <target> -am` (in-repo library roots get built too, error paths stay root-relative);
    otherwise build inside the target directory."""
    if not target or profile is None:
        return target, []
    t = target.strip("/")
    for p in profile.poms or []:
        if p.path == "pom.xml" and p.modules:
            mods = {m.strip("/").removeprefix("./") for m in p.modules}
            if t in mods:
                return None, ["-pl", t, "-am"]
    return target, []


def _reduced(c: ErrorCluster, errors: list[BuildError]) -> ErrorCluster:
    sigs: list[str] = []
    for e in errors:
        s = e.signature or f"{e.kind.value}|{e.message}"
        if s not in sigs:
            sigs.append(s)
    return c.model_copy(update={"errors": list(errors), "signatures": sigs})


@dataclass
class _Round:
    phase: Phase
    iteration: int
    build: BuildResult
    target: Optional[str]
    stall_level: int
    others: dict
    n: int = 0
    applied_any: bool = False
    pending: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    hopeless: set = field(default_factory=set)
    restored: list = field(default_factory=list)   # errors this round's rollbacks brought back (next baseline)


@dataclass
class _Pending:
    attempt: FixAttempt
    pre_sha: str
    post_sha: str


def _label(c: ErrorCluster) -> str:
    try:
        from sbmig.triage.cluster import cluster_label
        return cluster_label(c)
    except Exception:  # pragma: no cover - triage is ours, but stay defensive
        return c.id


def _script_name(a: FixAttempt) -> str:
    """'script abc1234567:v2 (augment)' — who made a script attempt (hints, logs)."""
    return f"script {a.script_case_id or '?'}:v{a.script_version} ({a.script_mode or '?'})"


def _related(cluster: ErrorCluster, attempts: list[FixAttempt]) -> list[FixAttempt]:
    """Same cluster id or any shared signature (other files too): hint material, never a limit (round 10)."""
    sigs = set(cluster.signatures)
    return [a for a in attempts if a.cluster_id == cluster.id or (sigs and sigs & set(a.cluster_signatures))]


def _same_cluster(cluster: ErrorCluster, attempts: list[FixAttempt],
                  canon: Optional[Callable[[Optional[str]], str]] = None) -> list[FixAttempt]:
    """The attempts made on THIS cluster (same id, or same — possibly moved — file with an overlapping signature)."""
    from sbmig.triage.decide import same_cluster_attempts
    return same_cluster_attempts(cluster, attempts, canon)


class BuildFixLoop:
    """v2 script fix loop. `handlers` / `rewrite_runner` are v1 collaborators kept in the signature (not used by the
    v2 flow); `script_runner` = ScriptRunnerLike (the orchestrator passes a lazy `sbmig.script.ScriptRunner(cfg)`);
    the KB's script store is `kb.scripts` (None = no KB: LLM scripts run unstored as v0, nothing is reused)."""

    def __init__(self, cfg: Config, ws: Workspace, build_runner: Any, triager: Any, handlers: Any, fixer: Any,
                 kb: Any, rewrite_runner: Any, profile: ProjectProfile, plan_notes: Optional[list[str]] = None,
                 budget: Optional[LoopBudget] = None, *, dry_run: bool = False,
                 history: Optional[list[FixAttempt]] = None, blocked: Optional[BlockedRegistry] = None,
                 locator: Any = None, advisories: Optional[list[AdvisoryFinding]] = None,
                 script_runner: Any = None):
        self.cfg = cfg
        self.ws = ws
        self.build_runner = build_runner
        self.triager = triager
        self.handlers = handlers
        self.fixer = fixer
        self.kb = kb
        self.rewrite_runner = rewrite_runner
        self.script_runner = script_runner
        self.profile = profile
        self.plan_notes = list(plan_notes or [])
        self.budget = budget or LoopBudget.from_config(cfg)
        self.dry_run = dry_run
        self.history: list[FixAttempt] = history if history is not None else []
        self.flags: list[str] = []
        self.followups: list[str] = []
        self.credited: list[str] = []
        self.escalated: dict[str, TriageDecision] = {}
        self.test_compile_skipped = False
        self.tests_skipped = False               # round 8: only runtime test failures left → packaged with -DskipTests
        # round 10: per build target ("" = the whole project) — one MSA service's fallback must not change how the
        # other services' final builds run (`skips_for`)
        self.test_compile_skipped_targets: set[str] = set()
        self.tests_skipped_targets: set[str] = set()
        self._profile_summary: Optional[str] = None
        # run-wide blocked state (shared by every phase of the run through the orchestrator)
        self.blocked = blocked if blocked is not None else BlockedRegistry()
        # v1: blocked items released for the final verify loop (`loop.verify_extra_llm_attempts`). v2 releases nothing
        # — the verify loop is the only fix loop and its per-case limit is script.max_augments_per_case; the list stays
        # (always empty) for the orchestrator
        self.released_ids: list[str] = []
        self.locator = locator                   # owner class → artifact (sbmig.build.classpath); None = no attribution
        # library advisory findings of the run (round 6): matched per cluster → FixContext.advisory_hints
        self.advisories: list[AdvisoryFinding] = list(advisories or [])
        self.llm_unavailable: Optional[str] = None
        self.llm_config_error: Optional[str] = None   # round 10: the same HTTP 400 for different prompts (the short body)
        self._http400: dict[str, set[str]] = {}  # short 400 body → prompt hashes (cluster ids when a fixer sets none)
        self.aborted: Optional[str] = None       # set when the run must stop (configured parent not resolvable)
        self._llm_streak: list[tuple[ErrorCluster, str]] = []
        self._milestone = "idle"                 # last point the loop passed (watchdog, round 9c)
        self._milestone_at = time.monotonic()
        self._busy: Optional[str] = None         # a Maven build / OpenRewrite run of the loop in progress (watchdog)
        self._quiet_from = 0.0                   # the watchdog last saw such a call (or an LLM call) in progress
        self._streak_attempts: list[str] = []    # attempt ids of the current transport-failure streak
        self.llm_cooldown_used = False           # round 9 (L5): one cool-down per run
        self._cooled_attempts: set[str] = set()  # attempts of the streak that triggered the cool-down: not counted
        self._sleep: Optional[Callable[[float], None]] = None   # injectable (tests); None = module `sleep_seconds`
        # round 9: first-wins merge conflicts of the restructure step (set by the orchestrator) → fix-prompt hints
        self.merge_conflicts: list[MergeConflict] = []
        # error cases (2026-10-23): `sbmig.loop.error_tracker.ErrorTracker`, set by the orchestrator (None = off);
        # every hook goes through `_track`, so a tracker problem never changes what the loop does
        self.error_tracker: Any = None
        self._phase_items: list[str] = []
        self._cur_phase: Phase = Phase.JDK21
        # v2 fix scripts: scripts credited (attempt → outcome credited), attempts rolled back before the next try, the
        # errors the last fix round's rollbacks brought back (added to the next scoring's baseline)
        self._credited: dict[int, str] = {}
        self._rolled_back: set[int] = set()
        self._ref_paths: dict[int, set[str]] = {}   # attempt → merge-conflict reference labels shown to the LLM
        self._target: Optional[str] = None
        attach = getattr(triager, "attach_run", None)
        if callable(attach):
            try:
                attach(blocked_registry=self.blocked, ws=ws, scripts=self.scripts)
            except TypeError:       # a triager from before v2 (no script store)
                try:
                    attach(blocked_registry=self.blocked, ws=ws)
                except Exception as e:  # pragma: no cover - a foreign triager
                    log.warning("triager.attach_run failed: %s", e)
            except Exception as e:  # pragma: no cover - a foreign triager
                log.warning("triager.attach_run failed: %s", e)

    @property
    def scripts(self) -> Any:
        """The KB's script store (`ScriptStoreLike`), None when the KB has none (NullKnowledgeBase, a v1 fake)."""
        try:
            st = getattr(self.kb, "scripts", None)
        except Exception as e:  # noqa: BLE001 - a broken KB never stops the loop
            log.warning("kb.scripts unavailable: %s: %s", type(e).__name__, e)
            return None
        return st if st is not None and callable(getattr(st, "add", None)) else None

    # ------------------------------------------------------------------ public
    def _mark(self, what: str) -> None:
        """Milestone for the watchdog: what the loop is doing right now (also a DEBUG line in sbmig.log)."""
        self._milestone, self._milestone_at = what, time.monotonic()
        log.debug("[%s] → %s", self._cur_phase.value, what)

    @contextmanager
    def _busy_with(self, what: str):
        """A call that logs its own heartbeat (Maven build, OpenRewrite run): the watchdog stays silent meanwhile, and
        its quiet time starts again when the call returns."""
        prev, self._busy = self._busy, what
        try:
            yield
        finally:
            self._busy = prev
            self._quiet_from = time.monotonic()

    def _activity(self) -> Optional[str]:
        """What is running right now that heartbeats by itself (round 10, A12): a build / rewrite of this loop, an LLM
        HTTP call (`sbmig.fix.client.llm_calls_in_flight`), a Maven child process (`sbmig.build.mvn.live_processes`,
        e.g. the classpath attribution) — None when nothing is (the watchdog then speaks)."""
        if self._busy:
            return self._busy
        try:
            from sbmig.fix.client import llm_calls_in_flight
            if llm_calls_in_flight():
                return "LLM call"
        except Exception:  # pragma: no cover - the fix package is ours; stay defensive
            pass
        try:
            from sbmig.build.mvn import live_processes
            if live_processes():
                return "Maven child process"
        except Exception:  # pragma: no cover
            pass
        return None

    def _start_watchdog(self, phase: Phase) -> threading.Event:
        """A daemon thread that says, every WATCHDOG_S once the loop was quiet for WATCHDOG_QUIET_S, where the loop is
        (last milestone) — a slow LLM / build keeps logging through its own heartbeat, so a quiet watchdog line
        points at the code between those (hints, prompt packing, patch apply, git). Round 10: while such a call is in
        progress (`_activity`) the watchdog neither warns nor dumps; the quiet time counts from its end."""
        stop = threading.Event()
        started = time.monotonic()
        dumped_for: set[float] = set()

        def beat() -> None:
            while not stop.wait(WATCHDOG_S):
                now = time.monotonic()
                if self._activity():
                    self._quiet_from = now
                    continue
                quiet = now - max(self._milestone_at, self._quiet_from)
                if quiet >= WATCHDOG_QUIET_S:
                    log.warning("[%s] fix loop quiet for %.0fs since '%s' (loop running %.0fs) — still working or "
                                "stuck? a thread dump follows after %.0fs (also: GET /api/threads, kill -USR1 <pid>)",
                                phase.value, quiet, self._milestone, time.monotonic() - started, WATCHDOG_DUMP_S)
                if quiet >= WATCHDOG_DUMP_S and self._milestone_at not in dumped_for:
                    dumped_for.add(self._milestone_at)
                    try:
                        from sbmig.core.diag import dump_threads
                        dump_threads(self.ws.run_dir)
                    except Exception as e:  # pragma: no cover - diagnostics never break the loop
                        log.warning("thread dump failed: %s", e)

        threading.Thread(target=contextvars.copy_context().run, args=(beat,), name="fixloop-watchdog",
                         daemon=True).start()
        return stop

    def run(self, phase: Phase, goal: str, max_iterations: int, target: Optional[str] = None,
            first_build: Optional[BuildResult] = None) -> PhaseResult:
        """`first_build` (v2): the build that made the caller start the loop (the failed final `clean package`) —
        used as the loop's build 1 instead of building the same tree again (one Maven build less per failing
        project; the caller already showed it to the error tracker)."""
        self._cur_phase = phase
        stop = self._start_watchdog(phase)
        try:
            return self._run(phase, goal, max_iterations, target, first_build)
        finally:
            stop.set()
            self._mark("idle")

    def _run(self, phase: Phase, goal: str, max_iterations: int, target: Optional[str] = None,
             first_build: Optional[BuildResult] = None) -> PhaseResult:
        pr = PhaseResult(phase=phase)
        self._phase_items = []
        self._cur_phase = phase
        self._target = target
        self._mark(f"loop start ({goal})")
        self.ws.journal({"event": "loop_start", "phase": phase.value, "goal": goal, "max_iterations": max_iterations,
                         "target": target, "dry_run": self.dry_run, "blocked_known": len(self.blocked),
                         "first_build": first_build.log_path if first_build is not None else None})
        prev_build: Optional[BuildResult] = None
        prev_restored: list[BuildError] = []     # errors the previous round's rollbacks brought back (its baseline)
        prev_multiset: Optional[dict[str, int]] = None
        window: list[dict[str, int]] = []        # error multisets of the previous builds (oscillation detection)
        oscillations = 0
        stall_level = 0
        pending: list[_Pending] = []
        fix_rounds = 0
        build_no = 0
        last: Optional[BuildResult] = None
        while True:
            build_no += 1
            reused = build_no == 1 and first_build is not None
            if reused:
                build = first_build
                self.ws.journal({"event": "build", "phase": phase.value, "iteration": build_no, "goal": build.goal,
                                 "target": target, "ok": build.ok, "exit_code": build.exit_code,
                                 "errors": build.error_count, "log": build.log_path, "reused": True})
                log.info("[%s] build 1 = the failed final build (%d error(s), %s) — not built again", phase.value,
                         build.error_count, build.log_path or "-")
            else:
                self._mark(f"mvn {goal} (build {build_no})")
                build = self._build(goal, target, f"{phase.value}-{build_no}", phase, build_no)
            last = build
            pr.iterations = build_no
            reverted = self._score(pr, pending, build, prev_build, prev_restored)
            scored = [p.attempt for p in pending]
            self._credit_scripts(pr, scored)
            # a revert rebuild shows the tree minus the reverted commits against the same "before" build: attempts the
            # reverted round left inconclusive (a pom edit's new dependency error hid their errors) are scored again
            pending = [p for p in pending if p.attempt.outcome == "inconclusive"] if reverted else []
            for p in pending:
                p.attempt.notes.append("scored again on the rebuild after the revert")
            # a reverted round's build shows a tree that no longer exists; a failed build without parsed errors (or a
            # timeout) says nothing about which errors are gone → void: only the scored attempts are annotated
            if not reused:      # the reused first build was observed by the caller (verify-final)
                self._track("observe", phase, f"{phase.value}-{build_no}", build, scored,
                            void=bool(reverted) or (not build.ok and not build.errors) or bool(build.timed_out),
                            target=target)
            rec = IterationRecord(phase=phase, iteration=build_no, build_ok=build.ok, error_count=build.error_count,
                                  signatures=build.signature_multiset(), build_log=build.log_path)
            pr.iteration_records.append(rec)
            if build.ok:
                pr.build_ok = True
                break
            if reverted:
                pr.notes.append(f"build {build_no}: regressed attempt(s) reverted → rebuilding the reverted tree")
                continue
            if self._parent_abort(pr, phase, build, build_no):
                break
            multiset = build.signature_multiset()
            stalled = prev_multiset is not None and multiset == prev_multiset
            oscillating = (not stalled) and any(m == multiset for m in window[:-1])
            stall_level = stall_level + 1 if (stalled or oscillating) else 0
            rec.stalled, rec.stall_level = (stalled or oscillating), stall_level
            if stalled:
                self.ws.journal({"event": "stall", "phase": phase.value, "iteration": build_no,
                                 "stall_level": stall_level, "error_count": build.error_count})
            if oscillating:
                oscillations += 1
                self.ws.journal({"event": "oscillation", "phase": phase.value, "iteration": build_no,
                                 "count": oscillations, "stall_level": stall_level, "error_count": build.error_count})
                pr.notes.append(f"build {build_no}: error set equals an earlier one (oscillation #{oscillations}) — "
                                "treated as a stall")
            window = (window + [multiset])[-max(2, self.cfg.loop.oscillation_window):]
            clusters = self._cluster(build.errors, target)
            self._track("observe_clusters", clusters)
            if fix_rounds >= max_iterations:
                log.info("[%s] build %d: %d error(s) in %d case(s) — max fix rounds (%d) used up; the phase ends "
                         "here and the final build carries the rest (reported as unresolved)",
                         phase.value, build_no, build.error_count, len(clusters), max_iterations)
            else:
                log.info("[%s] build %d: %d error(s) in %d case(s) → fix round %d/%d%s", phase.value, build_no,
                         build.error_count, len(clusters), fix_rounds + 1, max_iterations,
                         " (stall level %d)" % stall_level if stall_level else "")
            if oscillating and oscillations >= 2:
                self._block_oscillation(pr, phase, build_no, clusters)
            # v2: no release of blocked items for the final loop (`loop.verify_extra_llm_attempts` is a v1 setting):
            # the verify loop is the only fix loop, a case's limit is script.max_augments_per_case per run
            if self._only_blocked(pr, clusters, build_no):
                break
            if fix_rounds >= max_iterations:
                pr.notes.append(f"max iterations ({max_iterations}) reached with {build.error_count} error(s) left")
                break
            if self.budget.time_exceeded():
                pr.notes.append("run time budget exhausted: stopping the build-fix loop")
                self._flag("time-budget-exhausted")
                self.ws.journal({"event": "budget_exhausted", "phase": phase.value, "kind": "time"})
                self._block_remaining(pr, phase, build_no, clusters, BlockReason.BUDGET_EXHAUSTED,
                                      f"run time budget ({self.cfg.loop.max_total_minutes} min) exhausted before this "
                                      "cluster was fixed")
                break
            applied_any = False
            new_pending: list[_Pending] = []
            restored: list[BuildError] = []
            while True:
                fix_rounds += 1
                new_pending, applied_any, decisions, tried, back = self._fix_round(pr, phase, build_no, build, target,
                                                                                   stall_level, clusters)
                restored += back
                rec.decisions += decisions
                rec.cluster_count = len(rec.decisions)
                rec.attempt_ids = [a.id for a in pr.attempts if a.iteration == build_no]
                if self.dry_run or applied_any or not tried or fix_rounds >= max_iterations \
                        or self.budget.time_exceeded() or all(self.blocked.covering(c) for c in clusters):
                    break
                # no script changed anything: the tree is unchanged, so a rebuild would show the same errors — the
                # next rung of each case's ladder on the same build (bounded by script.max_augments_per_case)
                self.ws.journal({"event": "reround", "phase": phase.value, "iteration": build_no,
                                 "fix_round": fix_rounds + 1})
            pending = new_pending
            if self.dry_run:
                pr.notes.append("dry-run: decisions computed and journaled, nothing applied")
                break
            if not applied_any:
                if self._only_blocked(pr, clusters, build_no):
                    break
                pr.notes.append(f"build {build_no}: no actionable fixes (all clusters skipped/escalated/blocked/failed "
                                "to apply) — rebuilding would not change anything")
                break
            prev_build, prev_multiset, prev_restored = build, multiset, restored

        assert last is not None
        self._credit_scripts(pr, [a for a in pr.attempts if a.outcome == "inconclusive"])
        if not pr.build_ok and not self.dry_run and not self.aborted:
            self._maybe_skip_test_compile(pr, phase, goal, target, last)
        if not pr.build_ok and not self.dry_run and not self.aborted:
            self._maybe_skip_tests(pr, phase, goal, target, last)
        if pr.build_ok and not self.test_compile_skipped_in(pr) and not self.tests_skipped_in(pr):
            pr.remaining_errors = []
        elif not pr.remaining_errors:
            pr.remaining_errors = list(last.errors)
        pr.ok = pr.build_ok
        pr.blocked = [it for it in (self.blocked.get(i) for i in self._phase_items) if it is not None]
        pr.finished_at = utcnow()
        self.ws.journal({"event": "loop_end", "phase": phase.value, "ok": pr.ok, "iterations": pr.iterations,
                         "attempts": len(pr.attempts), "remaining_errors": len(pr.remaining_errors),
                         "blocked": [b.id for b in pr.blocked], "llm_calls_used": self.budget.llm_calls,
                         "aborted": self.aborted})
        return pr

    # ------------------------------------------------------------------ blocked bookkeeping
    def _touch_item(self, item_id: str) -> None:
        if item_id not in self._phase_items:
            self._phase_items.append(item_id)

    def _only_blocked(self, pr: PhaseResult, clusters: list[ErrorCluster], build_no: int) -> bool:
        if not clusters:
            return False
        items = [self.blocked.covering(c) for c in clusters]
        if not all(items):
            return False
        for it in items:
            self._touch_item(it.id)
        ids = list(dict.fromkeys(it.id for it in items))
        pr.notes.append(f"build {build_no}: only blocked cluster(s) remain ({len(clusters)} cluster(s), "
                        f"{len(ids)} blocked item(s)) — ending the phase; see 'Blocked' in the report")
        self.ws.journal({"event": "only_blocked", "phase": pr.phase.value, "iteration": build_no,
                         "clusters": [c.id for c in clusters], "blocked": ids})
        return True

    def _candidate_for(self, c: ErrorCluster, d: TriageDecision):
        from sbmig.triage.blocked import BlockedCandidate
        getter = getattr(self.triager, "blocked_candidate", None)
        cand = None
        if callable(getter):
            try:
                cand = getter(c.id)
            except Exception:  # pragma: no cover
                cand = None
        if cand is None:
            m = _BLOCKED_RAT_RE.search(d.rationale or "")
            try:
                reason = BlockReason(m.group("r")) if m else BlockReason.REPEATED_FAILURE
            except ValueError:
                reason = BlockReason.REPEATED_FAILURE
            cand = BlockedCandidate(reason=reason, evidence=[d.rationale], errors=list(c.errors))
        return cand

    def _register_blocked(self, phase: Phase, iteration: int, c: ErrorCluster, cand, rationale: str,
                          target: Optional[str] = None) -> list[BuildError]:
        """Record a BlockedItem for the errors `cand` covers; returns the cluster errors that are NOT blocked."""
        key = self.blocked.key
        covered = {key(e) for e in (cand.errors or c.errors)}
        errs = [e for e in c.errors if key(e) in covered] or list(c.errors)
        rest = [e for e in c.errors if key(e) not in covered]
        attempts = len(_same_cluster(c, self.history, self.blocked.canonical_file))
        reason = cand.reason
        artifact: Optional[GAV] = None
        owner = cand.owner_class
        notes: list[str] = []
        required, found = cand.required_type, cand.found_type
        if reason in (BlockReason.EXTERNAL_JAVAX_API, BlockReason.EXTERNAL_API_INCOMPATIBLE):
            owner, required, found = self._classpath_hints(errs, owner, required, found)
            artifact, owner, notes = attribute(self.locator, self.ws, self.cfg, owner, errs, c.module or target)
        elif reason in (BlockReason.ARTIFACT_NOT_FOUND, BlockReason.PARENT_NOT_FOUND):
            artifact = next((e.artifact for e in errs if e.artifact is not None), None)
        elif getattr(cand, "plugin", None):
            try:
                artifact = GAV.parse(cand.plugin)
            except ValueError:
                artifact = None
        item, created = self.blocked.block(c, reason, phase, errors=errs, iteration=iteration, attempts=attempts,
                                           required_type=required, found_type=found,
                                           owner_class=owner, artifact=artifact, notes=[rationale, *notes])
        if getattr(cand, "suggestion", ""):          # round 10: a person's text (pinned `block` case action)
            item.suggestion = cand.suggestion
        elif created or not item.suggestion:
            extra = ""
            if reason == BlockReason.PLUGIN_INCOMPATIBLE:
                extra = getattr(cand, "plugin", None) or ""
            elif reason == BlockReason.LLM_UNAVAILABLE:
                extra = self.llm_unavailable or ""
            item.suggestion = compose_suggestion(item, self.cfg, settings_file=self.cfg.build.settings_file,
                                                 repositories=list(getattr(cand, "repositories", []) or []),
                                                 extra=extra)
        self._touch_item(item.id)
        self.ws.journal({"event": "blocked", "phase": phase.value, "iteration": iteration, "id": item.id,
                         "created": created, "reason": reason.value, "cluster": c.id,
                         "signatures": [e.signature for e in errs][:10], "files": item.files[:10],
                         "artifact": item.artifact.coordinates if item.artifact else None,
                         "owner_class": item.owner_class, "required_type": item.required_type,
                         "found_type": item.found_type, "attempts": item.attempts, "suggestion": item.suggestion,
                         "rest_errors": len(rest)})
        return rest

    def _classpath_hints(self, errs: list[BuildError], owner: Optional[str], required: Optional[str],
                         found: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Fill gaps with sbmig.build.classpath.owner_fqcn / type_mismatch (G1) when the locator provides them."""
        loc = self.locator
        if loc is None:
            return owner, required, found
        for e in errs[:3]:
            try:
                if owner is None and hasattr(loc, "owner_fqcn"):
                    o = loc.owner_fqcn(e)
                    if o and not o.startswith(("javax.", "jakarta.")):   # a bare mismatch names the type, not an owner
                        owner = o
                if (required is None or found is None) and hasattr(loc, "type_mismatch"):
                    r, f = loc.type_mismatch(e)
                    required, found = required or r, found or f
            except Exception as ex:  # pragma: no cover - attribution is best effort
                log.debug("classpath hints failed: %s", ex)
        return owner, required, found

    def _block_simple(self, phase: Phase, iteration: int, c: ErrorCluster, reason: BlockReason, why: str) -> None:
        from sbmig.triage.blocked import BlockedCandidate
        _blocked, rest = self.blocked.partition(c)
        if not rest:
            return
        cc = _reduced(c, rest)
        self._register_blocked(phase, iteration, cc, BlockedCandidate(reason=reason, evidence=[why], errors=rest),
                               f"blocked: {reason.value} — {why}")

    def _block_remaining(self, pr: PhaseResult, phase: Phase, iteration: int, clusters: list[ErrorCluster],
                         reason: BlockReason, why: str) -> None:
        for c in clusters:
            self._block_simple(phase, iteration, c, reason, why)

    def _block_oscillation(self, pr: PhaseResult, phase: Phase, iteration: int, clusters: list[ErrorCluster]) -> None:
        # round 10: the clusters this phase actually worked on — not every cluster that shares an L1 signature with one
        involved = [c for c in clusters if _same_cluster(c, pr.attempts, self.blocked.canonical_file)] or clusters
        why = "fixes keep toggling between the same error sets (A↔B↔A) within loop.oscillation_window"
        pr.notes.append(f"build {iteration}: oscillation repeated → blocking {len(involved)} cluster(s)")
        self._block_remaining(pr, phase, iteration, involved, BlockReason.OSCILLATION, why)

    def _parent_abort(self, pr: PhaseResult, phase: Phase, build: BuildResult, iteration: int) -> bool:
        """The configured in-house parent is not resolvable → nothing can build: abort the run with a clear item."""
        keys = set(self.cfg.artifacts.parent_keys())           # root parent + boot_parent / comp_parent (round 9b)
        errs = [e for e in build.errors if e.kind == ErrorKind.PARENT_RESOLUTION
                and e.artifact is not None and e.artifact.key in keys]
        if not errs:
            return False
        from sbmig.triage.blocked import BlockedCandidate, repositories_in
        c = self.triager.cluster(errs, self.profile)[0]
        coords = errs[0].artifact.coordinates
        cand = BlockedCandidate(reason=BlockReason.PARENT_NOT_FOUND, errors=errs,
                                repositories=repositories_in("\n".join(f"{e.message}\n{e.raw}" for e in errs)),
                                evidence=[errs[0].message[:300]])
        self._register_blocked(phase, iteration, c, cand, f"blocked: parent_not_found — configured parent {coords} "
                                                          "is not resolvable; run aborted")
        self.aborted = (f"the configured in-house parent {coords} is not resolvable (settings: "
                        f"{self.cfg.build.settings_file or 'default'}) — fix the repository/settings or "
                        "artifacts.parent / boot_parent / comp_parent and rerun")
        self._flag("aborted:parent-not-found")
        self._followup(f"run aborted: {self.aborted}")
        pr.notes.append(f"build {iteration}: {self.aborted}")
        self.ws.journal({"event": "abort", "phase": phase.value, "iteration": iteration, "reason": self.aborted})
        return True

    def skips_for(self, target: Optional[str]) -> tuple[bool, bool]:
        """(test-compile skipped, tests skipped) by the L7 / L7b fallbacks for a build of `target` (round 10): a loop
        on the whole project ("") applies to every target; a whole-project build (target None) inherits any
        service's fallback (its reactor contains that service); a service build only its own."""
        t = _target_key(target)
        tc, ts = self.test_compile_skipped_targets, self.tests_skipped_targets
        if not t:
            return bool(tc), bool(ts)
        return (t in tc or "" in tc), (t in ts or "" in ts)

    @staticmethod
    def test_compile_skipped_in(pr: PhaseResult) -> bool:
        return any("test-compile-skipped" in n for n in pr.notes)

    @staticmethod
    def tests_skipped_in(pr: PhaseResult) -> bool:
        return any("tests-skipped:" in n for n in pr.notes)

    # ------------------------------------------------------------------ build
    def _build(self, goal: str, target: Optional[str], label: str, phase: Phase, iteration: int,
               skip_test_compile: bool = False, skip_tests: bool = False) -> BuildResult:
        module, extra = build_target_args(self.profile, target)
        skip_test_compile = skip_test_compile or bool(getattr(self.cfg.build, "skip_test_compile", False))
        kw: dict[str, Any] = {"module": module, "label": label, "skip_test_compile": skip_test_compile}
        if extra:
            kw["extra_args"] = extra
        with self._busy_with(f"mvn {goal} ({label})"):
            if skip_tests:
                try:
                    res = self.build_runner.run(goal, **kw, skip_tests=True)
                except TypeError:  # an older runner without the `skip_tests` option: pass -DskipTests as an argument
                    kw["extra_args"] = [*extra, "-DskipTests"]
                    res = self.build_runner.run(goal, **kw)
            else:
                res = self.build_runner.run(goal, **kw)
        self.ws.journal({"event": "build", "phase": phase.value, "iteration": iteration, "goal": goal,
                         "target": target, "extra_args": extra, "ok": res.ok, "exit_code": res.exit_code, "errors": res.error_count,
                         "duration_s": res.duration_s, "log": res.log_path, "skip_test_compile": skip_test_compile,
                         "skip_tests": skip_tests})
        return res

    # ------------------------------------------------------------------ scoring
    def _score(self, pr: PhaseResult, pending: list[_Pending], build: BuildResult,
               prev: Optional[BuildResult], restored: Optional[list[BuildError]] = None) -> bool:
        """Score the previous round's attempts against `build`. Returns True when a revert changed the tree.
        `restored` (v2) = the errors that round's rollbacks brought back: part of the baseline (`prev`), so they are
        never "new errors in a touched file" nor counted by the ×regression_ratio rule.

        Outcomes: `build_ok`; `improved` = every targeted error gone AND the build reached the stage that reports them;
        `partial` = some gone, the rest remain, nothing new in the touched files; `no_change`; `regressed` = new
        errors in a touched file (by (file, signature) occurrences), the ×regression_ratio rule, or — after a pom
        edit — a new structural error kind / new errors of an earlier stage that hide the targeted ones (reverted);
        `inconclusive` = the build cannot tell (timed out, failed without parsed errors, stopped at an earlier stage
        this attempt did not cause): no KB credit, scored again on a revert rebuild."""
        if not pending:
            return False
        real = [e for e in build.errors if not _synthetic(e)]
        cur_count = Counter(_pair(e) for e in build.errors)
        restored = list(restored or [])
        prev_errors = (list(prev.errors) if prev is not None else []) + restored
        prev_count = Counter(_pair(e) for e in prev_errors)
        base_total = (prev.error_count if prev is not None else 0) + len(restored)
        total = build.error_count
        ratio = self.cfg.loop.regression_ratio
        struct_kinds = set(self.cfg.loop.structural_regression_kinds)
        prev_kinds = {e.kind.value for e in prev_errors}
        new_struct = sorted({e.kind.value for e in real if e.kind.value in struct_kinds} - prev_kinds)
        # a timed-out build, or a failed one without a single parsed error, says nothing about any attempt
        blind = not build.ok and (bool(build.timed_out) or not real)
        structural: set[str] = set()
        left_of: dict[int, tuple[int, int]] = {}
        for p in pending:
            a = p.attempt
            a.errors_after = total
            if build.ok:
                a.outcome, a.cluster_resolved = "build_ok", True
                continue
            left, targeted = _targeted_left(a, cur_count)
            left_of[id(a)] = (left, targeted)
            # True = gone and the build could have shown them; None = gone from a build that could not; False = present
            a.cluster_resolved = (False if left else
                                  True if not blind and _visible_in(a.cluster_errors, build, real) else None)
        any_resolved = any(p.attempt.cluster_resolved for p in pending)
        for p in pending:
            a = p.attempt
            if build.ok:
                continue
            if blind:
                a.outcome, a.cluster_resolved = "inconclusive", None
                a.notes.append("inconclusive: the build " + ("timed out" if build.timed_out else
                               "failed without parseable errors") + " — it cannot show whether this attempt worked "
                               "(no KB credit)")
                continue
            touched = _touched(a)
            pom_touched = any(t.rsplit("/", 1)[-1] == "pom.xml" for t in touched)
            if new_struct and pom_touched:
                a.outcome = "regressed"
                a.notes.append(f"regressed: structural error kind(s) {', '.join(new_struct)} appeared after this "
                               "attempt edited a pom.xml → reverted regardless of error counts")
                a.introduced_errors = sanitize_errors([e for e in real if e.kind.value in new_struct], 10)
                structural.add(a.id)
                continue
            left, targeted = left_of[id(a)]
            if a.cluster_resolved is None:
                # the targeted errors are not in this build, but the build never got to report them
                stage = _stage_of(a.cluster_errors)
                earlier = [e for e in _new_errors(real, prev_count) if STAGE.get(e.kind, 99) < stage]
                if pom_touched and earlier:
                    a.outcome = "regressed"
                    kinds = ", ".join(sorted({e.kind.value for e in earlier}))
                    a.notes.append(f"regressed: new {kinds} error(s) appeared after this attempt edited a pom.xml and "
                                   "hide the targeted errors → reverted")
                    a.introduced_errors = sanitize_errors(earlier, 10)
                    structural.add(a.id)
                    continue
                a.outcome, a.cluster_resolved = "inconclusive", None
                a.notes.append("inconclusive: the build stopped before the stage of the targeted errors ("
                               + ", ".join(sorted({e.kind.value for e in real})[:4]) + " first) — whether they are "
                               "gone is unknown (no KB credit)")
                continue
            new_in_touched = _new_in_files(build.errors, touched, prev_count)
            before = max(a.errors_before, base_total) if restored else a.errors_before
            ratio_regress = (not any_resolved) and total > max(before, 1) * ratio
            if not a.cluster_resolved and (new_in_touched or ratio_regress):
                a.outcome = "regressed"
                a.introduced_errors = sanitize_errors(new_in_touched, 10)   # error cases: why it regressed
                why = (f"{len(new_in_touched)} new error(s) in touched files" if new_in_touched
                       else f"errors {before} → {total} (> ×{ratio})")
                a.notes.append(f"regressed: {why}")
            elif a.cluster_resolved:
                a.outcome = "improved"
            elif 0 < left < targeted:
                a.outcome = "partial"
                a.notes.append(f"partial: {targeted - left} of {targeted} targeted error(s) gone, {left} remain")
            else:
                a.outcome = "no_change"
        # v2: no KB recipe credit (no KB recipes in the flow); the fix scripts are credited by the caller after the
        # revert below settled the outcomes (`_credit_scripts`)
        reverted = False
        if not build.ok and self.cfg.loop.revert_on_regression and any(p.attempt.outcome == "regressed" for p in pending):
            commits: list[str] = []
            for p in pending:
                if p.post_sha not in commits:
                    commits.append(p.post_sha)
            for sha in reversed(commits):
                group = [p for p in pending if p.post_sha == sha]
                if not all(p.attempt.outcome == "regressed" for p in group):
                    break
                self.ws.revert_to(group[0].pre_sha)
                reverted = True
                for p in group:
                    p.attempt.outcome = "reverted"
                    p.attempt.notes.append(f"reverted to {group[0].pre_sha[:10]}")
                self.ws.journal({"event": "revert", "phase": pr.phase.value, "to": group[0].pre_sha,
                                 "commit": sha, "attempts": [p.attempt.id for p in group]})
            # structural regressions are reverted even when later commits of the round depend on them (git revert)
            for p in pending:
                a = p.attempt
                if a.outcome != "regressed" or a.id not in structural:
                    continue
                group = [q for q in pending if q.post_sha == p.post_sha]
                res = self.ws._git("revert", "--no-edit", p.post_sha, check=False)   # noqa: SLF001
                if res.returncode != 0:
                    self.ws._git("revert", "--abort", check=False)                      # noqa: SLF001
                    a.notes.append(f"structural revert of {p.post_sha[:10]} failed: {(res.stderr or '').strip()[:200]}")
                    continue
                reverted = True
                for q in group:
                    q.attempt.outcome = "reverted"
                    q.attempt.notes.append(f"reverted commit {p.post_sha[:10]} (git revert: structural regression)")
                self.ws.journal({"event": "revert", "phase": pr.phase.value, "commit": p.post_sha, "mode": "git-revert",
                                 "structural": new_struct, "attempts": [q.attempt.id for q in group]})
            for p in pending:
                if p.attempt.outcome == "regressed":
                    p.attempt.notes.append("not reverted: later commits that improved things depend on it")
        for p in pending:
            self.ws.journal({"event": "attempt_scored", "phase": pr.phase.value, "attempt": p.attempt.id,
                             "cluster": p.attempt.cluster_id, "outcome": p.attempt.outcome,
                             "cluster_resolved": p.attempt.cluster_resolved, "errors_after": total})
        return reverted

    # ------------------------------------------------------------------ fixing
    def _kb_match(self, cluster: ErrorCluster) -> KBMatch:
        try:
            m = self.kb.match(cluster, self.ws, self.profile)
            return m if m is not None else KBMatch()
        except Exception as e:
            log.warning("kb.match failed for %s: %s", cluster.id, e)
            return KBMatch()

    def _plan(self, clusters: list[ErrorCluster]) -> list[tuple[ErrorCluster, Optional[TriageDecision], list[str]]]:
        planner = getattr(self.triager, "plan_iteration", None)
        if not callable(planner):
            return [(c, None, []) for c in clusters]
        return [(it.cluster, it.skip, list(it.masked_by)) for it in planner(clusters)]

    def _decide(self, phase: Phase, iteration: int, stall_level: int, cluster: ErrorCluster,
                match: KBMatch) -> TriageDecision:
        ctx = TriageContext(phase=phase, iteration=iteration, stall_level=stall_level,
                            previous_attempts=self._previous(cluster), llm_calls_used=self.budget.llm_calls,
                            llm_calls_budget=self.budget.max_llm_calls)
        try:
            return self.triager.decide(cluster, match, ctx, self.profile)
        except Exception as e:
            log.exception("triage.decide failed for %s", cluster.id)
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.ESCALATE,
                                  rationale=f"triage failed: {type(e).__name__}: {e}")

    def _previous(self, cluster: ErrorCluster) -> list[FixAttempt]:
        """Earlier SCORED attempts on THIS cluster (round 10: not other files that share an L1 signature, not this
        round's pending ones), without the transport failures that triggered the LLM cool-down (L5): the cool-down
        round must not count against `loop.max_llm_attempts_per_cluster`."""
        return [a for a in _same_cluster(cluster, self.history, self.blocked.canonical_file)
                if a.id not in self._cooled_attempts and a.outcome != "pending"]

    def _sibling_hints(self, cluster: ErrorCluster, limit: int = 3) -> list[str]:
        """The same error in OTHER files of this run (shared signature): hint material for the prompt only (round 10)
        — how a sibling was fixed, or what did not work there. v2: a case already spans its files, so this is the
        same case in another scope (another MSA service's loop)."""
        mine = {id(a) for a in _same_cluster(cluster, self.history, self.blocked.canonical_file)}
        out: list[str] = []
        for a in reversed(_related(cluster, self.history)):
            if id(a) in mine or a.outcome in ("pending", "blocked") or a.id in self._cooled_attempts:
                continue
            where = next((e.file for e in a.cluster_errors if e.file), None) or a.cluster_id
            sig = next((s for s in a.cluster_signatures if s in cluster.signatures), "")
            who = (_script_name(a) if a.decision.kind == DecisionKind.SCRIPT else
                   a.decision.handler_id or ", ".join(a.decision.recipe_ids) or a.decision.kind.value)
            if a.outcome in ("improved", "build_ok", "partial"):
                text = (f"this run: the same error ({sig}) in {where} was {'partly ' if a.outcome == 'partial' else ''}"
                        f"fixed by attempt {a.id} ({who}): {_change_line(a)}")
            else:
                text = (f"this run: attempt {a.id} ({who}) on the same error ({sig}) in {where} did not fix it "
                        f"({a.outcome}): {_change_line(a)}")
            out.append(text[:400])
            if len(out) >= limit:
                break
        return out

    def _cluster(self, errors: list[BuildError], target: Optional[str]) -> list[ErrorCluster]:
        """v2: one cluster per case (L2) across files, scoped by the loop's build target (MSA services)."""
        try:
            return self.triager.cluster(errors, self.profile, scope=target)
        except TypeError:           # a triager without `scope` (v1 / foreign): its own clusters
            return self.triager.cluster(errors, self.profile)

    def _fix_round(self, pr: PhaseResult, phase: Phase, iteration: int, build: BuildResult, target: Optional[str],
                   stall_level: int, clusters: Optional[list[ErrorCluster]] = None
                   ) -> tuple[list[_Pending], bool, list[TriageDecision], bool, list[BuildError]]:
        """One fix round: at most one script attempt per case, `loop.max_clusters_per_iteration` cases. Returns
        (pending attempts, anything applied, decisions, a script attempt was made, errors brought back by rollbacks)."""
        if clusters is None:
            clusters = self._cluster(build.errors, target)
        planned = self._plan(clusters)
        limit = max(1, self.cfg.loop.max_clusters_per_iteration)
        self.ws.journal({"event": "triage", "phase": phase.value, "iteration": iteration,
                         "clusters": [{"id": c.id, "kind": c.kind.value, "file": c.file, "module": c.module,
                                       "case_id": c.case_id, "errors": len(c.errors),
                                       "files": len({e.file for e in c.errors if e.file}), "priority": c.priority}
                                      for c in clusters],
                         "masked": [c.id for c, skip, _ in planned if skip is not None]})
        st = _Round(phase=phase, iteration=iteration, build=build, target=target, stall_level=stall_level,
                    others={c.id: _label(c) for c in clusters},
                    n=sum(1 for a in pr.attempts if a.iteration == iteration))   # ids stay unique across re-rounds
        masked: list[tuple[ErrorCluster, TriageDecision, list[str]]] = []
        handled = 0
        before = sum(1 for a in pr.attempts if a.decision.kind == DecisionKind.SCRIPT)
        for c, skip, by in planned:
            c2 = self._unblocked_part(st, c)
            if c2 is None:
                continue
            if skip is not None:
                masked.append((c2, skip, by))
                continue
            if handled >= limit:
                continue
            if self._handle_cluster(pr, st, c2) != "blocked":
                handled += 1
        # unmask: a masker that cannot be acted upon must not hide compile errors we could fix
        for c, skip, by in masked:
            if by and all(b in st.hopeless for b in by) and handled < limit:
                if self._handle_cluster(pr, st, c, prefix="(unmasked: its pom problem was escalated/blocked) ") \
                        != "blocked":
                    handled += 1
            else:
                st.decisions.append(skip)
                self._journal_decision(st, skip)
        tried = sum(1 for a in pr.attempts if a.decision.kind == DecisionKind.SCRIPT) > before
        return st.pending, st.applied_any, st.decisions, tried, list(st.restored)

    def _unblocked_part(self, st: "_Round", c: ErrorCluster) -> Optional[ErrorCluster]:
        """Drop the errors the run already blocked; None when nothing is left (decision BLOCKED is recorded)."""
        blocked, rest = self.blocked.partition(c)
        if not blocked:
            return c
        if rest:
            return _reduced(c, rest)
        item = self.blocked.covering(c)
        assert item is not None
        self._touch_item(item.id)
        st.hopeless.add(c.id)
        d = TriageDecision(cluster_id=c.id, kind=DecisionKind.BLOCKED,
                           rationale=f"blocked: {item.reason.value} — already blocked ({item.id}, first seen in "
                                     f"{item.phase.value}); skipped")
        st.decisions.append(d)
        self._journal_decision(st, d)
        return None

    def _journal_decision(self, st: "_Round", d: TriageDecision) -> None:
        self.ws.journal({"event": "decision", "phase": st.phase.value, "iteration": st.iteration,
                         **d.model_dump(mode="json")})
        self._track("note_decision", d)

    def _track(self, method: str, *args: Any, **kwargs: Any) -> None:
        """Error-case tracker hook (2026-10-23): no tracker → nothing; a failing tracker is logged, never raised."""
        t = self.error_tracker
        if t is None:
            return
        try:
            getattr(t, method)(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - the tracker never breaks the loop
            log.warning("error tracker %s failed: %s: %s", method, type(e).__name__, e)

    def _script_step(self, c: ErrorCluster) -> Any:
        """The ladder step of the triager's last SCRIPT decision for `c` (computed here for a triager without it)."""
        getter = getattr(self.triager, "script_step", None)
        step = None
        if callable(getter):
            try:
                step = getter(c.id)
            except Exception:  # pragma: no cover - a foreign triager
                step = None
        if step is None:
            from sbmig.triage.scripts import plan_script_step
            step = plan_script_step(c, self._previous(c), self.scripts, self.cfg)
        return step

    def _handle_cluster(self, pr: PhaseResult, st: "_Round", c: ErrorCluster, prefix: str = "") -> str:
        """Decide → (v2) run the case's fix script → commit. Returns "blocked" (nothing left to do for this cluster),
        "escalated", "skipped" or "attempted"."""
        from sbmig.triage.blocked import BlockedCandidate
        self._mark(f"KB match for {c.id}")
        m = self._kb_match(c)            # v2: only a case pinned to `block` by a person is used from it
        for _ in range(4):
            self._mark(f"deciding {c.id}")
            d = self._decide(st.phase, st.iteration, st.stall_level, c, m)
            if prefix:
                d.rationale = prefix + d.rationale
            if d.kind in (DecisionKind.HANDLER, DecisionKind.KB_RECIPE, DecisionKind.KB_PATCH, DecisionKind.LLM_FIX):
                # a v1 decision (a foreign triager): v2 fixes every error with a fix script
                d = TriageDecision(cluster_id=c.id, kind=DecisionKind.SCRIPT,
                                   rationale=f"{d.kind.value} is not part of the v2 flow → fix script ({d.rationale})")
            step = self._script_step(c) if d.kind == DecisionKind.SCRIPT else None
            forced = None
            if step is not None and step.mode is None:
                forced = BlockedCandidate(reason=BlockReason.REPEATED_FAILURE, errors=list(c.errors),
                                          evidence=[(step.why or "fix-script limits reached") + "; needs a human"])
            elif step is not None and step.needs_llm and self.llm_unavailable:
                forced = BlockedCandidate(reason=BlockReason.LLM_UNAVAILABLE, errors=list(c.errors),
                                          evidence=[f"LLM unavailable ({self.llm_unavailable})"])
            elif step is not None and step.needs_llm and not self.budget.llm_allowed():
                forced = BlockedCandidate(reason=BlockReason.BUDGET_EXHAUSTED, errors=list(c.errors),
                                          evidence=[f"LLM budget exhausted ({self.budget.llm_calls}/"
                                                    f"{self.budget.max_llm_calls} calls)"])
            if forced is not None:
                d = TriageDecision(cluster_id=c.id, kind=DecisionKind.BLOCKED,
                                   rationale=prefix + f"blocked: {forced.reason.value} — {forced.evidence[0]}")
                step = None
            st.decisions.append(d)
            self._journal_decision(st, d)
            log.info("[%s it%d] %s: %s\n    first error: %s", st.phase.value, st.iteration, _head(c),
                     _describe_decision(d, c, self.history, self.cfg, step), _first_error_line(c))
            if d.kind == DecisionKind.ESCALATE:
                self.escalated[c.id] = d
                st.hopeless.add(c.id)
                return "escalated"
            if d.kind == DecisionKind.BLOCKED:
                cand = forced or self._candidate_for(c, d)
                rest = self._register_blocked(st.phase, st.iteration, c, cand, d.rationale, st.target)
                if not rest:
                    st.hopeless.add(c.id)
                    return "blocked"
                # only part of the case is blocked (e.g. one javax constructor mismatch): keep working on the rest
                c = _reduced(c, rest)
                prefix = "(rest of a partially blocked case) "
                continue
            if d.kind == DecisionKind.SKIP or self.dry_run:
                return "skipped"
            self._script_attempt(pr, st, c, d, step)
            return "attempted"      # one script attempt per case and round
        return "attempted"

    # ------------------------------------------------------------------ v2 fix scripts
    def _project(self) -> str:
        """The project name the KB records use (`sbmig.kb.ledger.project_name`)."""
        try:
            from sbmig.kb.ledger import project_name
            return project_name(self.profile, self.ws)
        except Exception:  # pragma: no cover - the KB package is ours
            return (getattr(self.profile, "name", "") or "") or self.ws.root.name

    def _script_attempt(self, pr: PhaseResult, st: "_Round", c: ErrorCluster, d: TriageDecision, step: Any) -> None:
        """One script attempt of a case: build the augment base (before anything moves), roll back the case's previous
        attempt, get the code (stored version or the LLM — stored before it runs), run it, commit. An attempt that
        changes nothing gives the rollback back (the tree stays as the last build saw it)."""
        case_id = step.case_id
        base = self._script_base(c, step) if step.mode == "augment" else None
        head_before = self.ws.head()
        rolled = self._rollback_previous(c, st)
        restored = rolled[1] if rolled else []
        if restored:                                    # the case as it stands on the rolled-back tree
            c = _reduced(c, [*c.errors, *restored])
        st.n += 1
        att = self._new_attempt(st.phase, st.iteration, st.n, c, d, st.build)
        att.errors_before = st.build.error_count + len(st.restored)
        att.script_mode, att.script_case_id = step.mode, case_id
        if rolled is not None and rolled[2]:
            att.notes.append(rolled[2])
        if step.mode == "reuse":
            meta = step.meta
            code = step.code
            att.script_version, att.script_parent, att.script_code = step.version, getattr(meta, "parent", None), code
            att.analysis = (getattr(meta, "analysis", "") or "").strip() or \
                f"stored fix script {case_id} v{step.version} (reused, no LLM call)"
            att.notes.append(f"reused stored v{step.version}: {getattr(meta, 'successes', 0)} success(es) in "
                             f"{len(getattr(meta, 'success_projects', None) or [])} project(s), "
                             f"{getattr(meta, 'failures', 0)} failure(s)")
        else:
            code = self._ask_script(att, c, st, step, base)
        if code:
            self._run_script(att, c, code, st)
        if rolled is not None and rolled[3]:
            prev_att = rolled[0]
            if att.applied:
                self._rolled_back.add(id(prev_att))
                prev_att.notes.append(ROLLED_BACK_NOTE + (f"v{att.script_version}" if att.script_version
                                                           else f"attempt {att.id}"))
                self._track("note_rollback", prev_att)
            else:
                self._undo_rollback(st, prev_att, head_before, restored, att)
        self._record(pr, att)
        if att.outcome == "apply_failed":
            self._credit_script(att)
        self._log_script_result(st, c, att)

    def _script_base(self, c: ErrorCluster, step: Any) -> Optional[ScriptBase]:
        """The augment context: the version to extend and what it did in this project (or, for the store's newest
        version, what the store knows of it), the case's remaining errors and the version history."""
        try:
            a = step.base_attempt
            if a is not None:
                res = a.script_result
                changed = [ch.new_path or ch.path for ch in a.changes] or (list(res.changed_files) if res else [])
                err = (res.error if res is not None else "") or "; ".join(a.apply_errors)
                return ScriptBase(case_id=step.case_id, version=int(a.script_version or 0), code=a.script_code,
                                  outcome=a.outcome, status=(res.status if res is not None else ""), error=err[:2000],
                                  log=list(res.log[-SCRIPT_BASE_LOG:]) if res is not None else [],
                                  changed_files=list(dict.fromkeys(changed))[:100],
                                  remaining_errors=[fmt_error(e) for e in c.errors][:30],
                                  introduced_errors=[fmt_error(e) for e in a.introduced_errors][:20],
                                  history=self._script_history(step.case_id, c))
            meta = step.meta
            return ScriptBase(case_id=step.case_id, version=int(getattr(meta, "version", 0) or 0), code=step.code,
                              remaining_errors=[fmt_error(e) for e in c.errors][:30],
                              history=self._script_history(step.case_id, c))
        except Exception as e:  # noqa: BLE001 - a broken base means a new script, never a crash
            log.warning("script base for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return None

    def _script_history(self, case_id: str, c: ErrorCluster) -> list[str]:
        """'v1: 2 ok / 1 failed / 0 partial — last: no_change' per stored version, then this run's attempts."""
        out: list[str] = []
        store = self.scripts
        if store is not None:
            try:
                for m in list(store.versions(case_id) or [])[-SCRIPT_HISTORY:]:
                    line = (f"v{m.version}: {m.successes} ok / {m.failures} failed / {m.partials} partial — last: "
                            f"{m.last_outcome or '-'}")
                    if m.parent:
                        line += f" (augments v{m.parent})"
                    if m.status != "active":
                        line += f" [{m.status}]"
                    out.append(line)
            except Exception as e:  # noqa: BLE001
                log.debug("script history of %s unavailable: %s", case_id, e)
        for a in _same_cluster(c, self.history, self.blocked.canonical_file):
            if a.decision.kind == DecisionKind.SCRIPT:
                st = a.script_result.status if a.script_result is not None else "-"
                out.append(f"this run: {a.id} v{a.script_version} {a.script_mode} → {a.outcome} (script {st})")
        return out[-2 * SCRIPT_HISTORY:]

    def _rollback_previous(self, c: ErrorCluster, st: "_Round"
                           ) -> Optional[tuple[FixAttempt, list[BuildError], str, bool]]:
        """Roll back the case's previous script attempt unless it was improved / build_ok (or already reverted /
        never applied). Returns (that attempt, the errors it had removed — back in the tree now, a note for the next
        attempt, rolled back) or None. A rollback that fails (a later commit changed the same lines) is a note (rolled
        back False): the next version then runs on top of it."""
        prev = [a for a in _same_cluster(c, self.history, self.blocked.canonical_file)
                if a.decision.kind == DecisionKind.SCRIPT and a.applied and a.commit]
        if not prev:
            return None
        a = prev[-1]
        if a.outcome in NO_ROLLBACK or id(a) in self._rolled_back:
            return None
        how = ""
        try:
            if self.ws.head() == a.commit and a.base_commit:
                self.ws.revert_to(a.base_commit)
                how = "reset"
            else:
                res = self.ws._git("revert", "--no-edit", a.commit, check=False)   # noqa: SLF001
                if res.returncode != 0:
                    self.ws._git("revert", "--abort", check=False)                  # noqa: SLF001
                    why = (res.stderr or res.stdout or "").strip()[:200]
                    a.notes.append(f"rollback before the next attempt failed: {why}")
                    log.warning("[%s it%d] %s: rollback of %s failed (%s) — the next version runs on top of it",
                                st.phase.value, st.iteration, _head(c), a.id, why)
                    return a, [], (f"the previous attempt {a.id} (v{a.script_version}, {a.outcome}) could not be "
                                   f"rolled back ({why}): this version ran on top of it"), False
                how = "git revert"
        except Exception as e:  # noqa: BLE001 - a git problem: keep going on top of it
            a.notes.append(f"rollback before the next attempt failed: {type(e).__name__}: {e}")
            return a, [], (f"the previous attempt {a.id} could not be rolled back ({type(e).__name__}): this "
                           "version ran on top of it"), False
        restored = _gone(a.cluster_errors, st.build)
        st.restored += restored
        self.ws.journal({"event": "script_rollback", "phase": st.phase.value, "iteration": st.iteration,
                         "attempt": a.id, "case": a.script_case_id, "version": a.script_version,
                         "outcome": a.outcome, "commit": a.commit, "mode": how, "restored_errors": len(restored)})
        log.info("[%s it%d] %s: rolled back v%s (%s, %s) before the next attempt%s", st.phase.value, st.iteration,
                 _head(c), a.script_version, a.id, a.outcome,
                 f" — {len(restored)} error(s) it had removed are back" if restored else "")
        return a, restored, (f"the previous attempt {a.id} (v{a.script_version}, {a.outcome}) was rolled back "
                             f"({how}) before this one"), True

    def _undo_rollback(self, st: "_Round", prev_att: FixAttempt, head_before: str, restored: list[BuildError],
                       att: FixAttempt) -> None:
        """The next attempt changed nothing: put the rolled-back attempt back (the tree the last build saw)."""
        try:
            if self.ws.is_dirty():
                self.ws.discard_uncommitted()
            self.ws.revert_to(head_before)
        except Exception as e:  # noqa: BLE001
            log.warning("could not undo the rollback of %s: %s: %s", prev_att.id, type(e).__name__, e)
            return
        for e in restored:
            if e in st.restored:
                st.restored.remove(e)
        att.notes.append(f"the rollback of {prev_att.id} was undone: this attempt changed nothing")
        self.ws.journal({"event": "script_rollback_undone", "phase": st.phase.value, "iteration": st.iteration,
                         "attempt": prev_att.id, "next_attempt": att.id, "head": head_before})

    def _ask_script(self, att: FixAttempt, c: ErrorCluster, st: "_Round", step: Any,
                    base: Optional[ScriptBase]) -> str:
        """new / augment: the LLM writes the script; it is stored in the KB before it runs. Returns the code, or ""
        when there is none (the attempt is then apply_failed with the reason)."""
        hints = [h for h in self._sibling_hints(c)]
        other = "\n".join(v for k, v in st.others.items() if k != c.id)[:4000]
        self._mark(f"advisory hints for {c.id}")
        adv_hints, att.advisory_ids = self._advisory_hints(c)
        self._mark(f"merge-conflict hints for {c.id}")
        ref_notes, ref_files = self._merge_conflict_hints(att, c)
        if ref_files:
            self._ref_paths[id(att)] = {_norm_path(k) for k in ref_files}
        mode = step.mode if base is not None or step.mode == "new" else "new"
        if step.mode == "augment" and base is None:
            att.notes.append("augment without a usable base → asking for a new script")
        att.script_mode = mode
        if mode == "augment":
            att.script_parent = int(base.version) or None
        # the stronger model on a stall, and from the second augment of a case on (v2: every fix runs in the verify
        # loop, so "the final loop" no longer selects it)
        escalate = st.stall_level >= self.cfg.loop.stall_limit or (mode == "augment" and step.augments_used >= 1)
        sig = next((e.signature_generic for e in c.errors if e.signature_generic), "") or ""
        ctx = FixContext(phase=st.phase, iteration=st.iteration, stall_level=st.stall_level,
                         profile_summary=self._summary(), plan_notes=self.plan_notes, kb_hints=hints,
                         advisory_hints=adv_hints, previous_attempts=self._previous(c), other_errors_summary=other,
                         use_escalation_model=escalate, reference_notes=ref_notes, reference_files=ref_files,
                         script_mode=mode, case_id=step.case_id, signature_generic=sig,
                         script_base=base if mode == "augment" else None)
        self._mark(f"fixer.propose_script for {c.id} ({mode}: prompt packing + LLM call)")
        try:
            proposal = self.fixer.propose_script(self.ws, c, ctx)
        except ScriptFixerMissing as e:
            self._fail(att, str(e))
            self._no_script_fixer(att, c, str(e))
            return ""
        except Exception as e:
            self.budget.charge_llm(1)
            log.warning("fixer.propose_script failed for %s: %s", c.id, e)
            self._fail(att, f"LLM fixer failed: {type(e).__name__}: {e}")
            if is_llm_transport_error(f"llm-error: {type(e).__name__}: {e}"):
                self._llm_transport_failure(att, c, f"{type(e).__name__}: {e}")
            return ""
        self.budget.charge_llm(max(1, proposal.llm_usage.calls))
        self._mark(f"LLM answered for {c.id}")
        att.model, att.prompt_hash, att.llm_usage = proposal.model, proposal.prompt_hash, proposal.llm_usage
        att.analysis = proposal.analysis
        if is_llm_transport_error(proposal.analysis):
            self._fail(att, proposal.analysis.strip()[:300])
            self._llm_transport_failure(att, c, proposal.analysis.strip()[len("llm-error:"):].strip()[:300])
            return ""
        self._llm_streak, self._streak_attempts = [], []
        if self._llm_config_check(att, c, proposal.analysis, proposal.prompt_hash):
            return ""
        if escalate:
            att.notes.append(f"escalation model requested (stall level {st.stall_level})"
                             if st.stall_level >= self.cfg.loop.stall_limit else
                             f"escalation model requested (augment {step.augments_used + 1} of the case)")
        code = (proposal.code or "").strip("\n")
        log.info("[%s it%d] %s: LLM answered (%s) — %s%s", st.phase.value, st.iteration, _head(c), mode,
                 "a script" if code.strip() else "no script",
                 f" — {_first_sentence(proposal.analysis)}" if proposal.analysis else "")
        if not code.strip():
            why = "LLM proposed no script" + (f": {proposal.error}" if proposal.error else "")
            if proposal.requested_files:
                why += f" (asked for: {', '.join(proposal.requested_files[:5])})"
            self._fail(att, why)
            return ""
        att.script_code = code
        if base is not None and mode == "augment" and code.strip() == (base.code or "").strip():
            self._fail(att, f"the augmented script is identical to its base v{base.version} — not run again")
            return ""
        if not self._store_script(att, c, step, proposal, code):
            return ""
        return code

    def _store_script(self, att: FixAttempt, c: ErrorCluster, step: Any, proposal: Any, code: str) -> bool:
        """Store an LLM script before it runs (`kb.scripts.add`, parent = the augmented version): failures keep their
        lineage. No KB → it runs unstored (v0). False = do not run it (the same code already ran in this run)."""
        store = self.scripts
        if store is None:
            att.notes.append("fix script not stored (no knowledge base): v0")
            return True
        sig = next((e.signature_generic for e in c.errors if e.signature_generic), "") or ""
        try:
            meta = store.add(step.case_id, sig, code, parent=att.script_parent, analysis=proposal.analysis or "",
                             model=proposal.model or "", run_id=self.ws.run_id, project=self._project(), source="llm")
        except Exception as e:  # noqa: BLE001 - the KB never stops the loop: run it unstored
            log.warning("kb.scripts.add(%s) failed: %s: %s", step.case_id, type(e).__name__, e)
            att.notes.append(f"fix script not stored ({type(e).__name__}: {e}): v0")
            return True
        att.script_version = int(getattr(meta, "version", 0) or 0)
        if getattr(meta, "status", "active") == "quarantined":
            att.notes.append(f"stored as v{att.script_version} masked and quarantined (the code matched a secret "
                             "pattern) — never reused")
        ran = next((a for a in _same_cluster(c, self.history, self.blocked.canonical_file)
                    if a.decision.kind == DecisionKind.SCRIPT and a.script_version == att.script_version
                    and att.script_version and a.script_result is not None), None)
        if ran is not None:
            self._credited[id(att)] = "duplicate"          # that run was credited already
            self._fail(att, f"the LLM answered with v{att.script_version} again (identical code), which already "
                            f"ran in this run ({ran.id}: {ran.outcome}) — not run again")
            return False
        self.ws.journal({"event": "script_stored", "case": step.case_id, "version": att.script_version,
                         "parent": att.script_parent, "mode": att.script_mode, "attempt": att.id,
                         "status": getattr(meta, "status", "")})
        return True

    def _run_script(self, att: FixAttempt, c: ErrorCluster, code: str, st: "_Round") -> None:
        """Run the script on the project (`errors` = the case's errors); rejected / error / timeout → nothing applied
        (working tree restored); ok + changes → commit."""
        case_id, v = att.script_case_id, att.script_version
        runner = self.script_runner
        if runner is None:
            return self._fail(att, "no script runner (sbmig.script) in this run")
        pre = self.ws.head()
        self._mark(f"fix script {case_id}:v{v} on {c.id}")
        with self._busy_with(f"fix script {case_id}:v{v}"):
            try:
                res = runner.run(self.ws, code, errors=list(c.errors))
            except Exception as e:  # noqa: BLE001 - the runner says it never raises; stay defensive
                res = ScriptRunResult(status="error", error=f"script runner failed: {type(e).__name__}: {e}")
        att.script_result = res
        if res.status != "ok":
            why = f"script {res.status}: {(res.error or '').strip()[:400] or 'no detail'}"
            self._fail(att, why)                   # no partial application: the working tree is restored
            if res.problems:
                att.notes.append("static check: " + "; ".join(res.problems[:5]))
            return
        self._drop_reference_writes(att, pre)
        self._mark(f"git commit for {c.id}")
        sha = self._commit(f"fix(script:{case_id}:v{v} {att.script_mode}): {c.id}", pre)
        if sha is None:
            att.outcome = "apply_failed"
            att.apply_errors.append("no file changed")
            return
        att.applied, att.commit = True, sha
        try:
            files = self._changed_between(pre, sha)
        except Exception:  # pragma: no cover - git trouble: fall back to the script's own account
            files = list(res.changed_files)
        att.changes = [FileChange(path=f, op="modify" if self.ws.exists(f) else "delete") for f in files]
        warn = int(getattr(getattr(self.cfg, "script", None), "max_files_changed_warn", 0) or 0)
        if warn and len(files) > warn:
            note = (f"the script changed {len(files)} files (more than script.max_files_changed_warn = {warn}) — "
                    "check its diff")
            att.notes.append(note)
            log.warning("[%s it%d] %s: %s", st.phase.value, st.iteration, _head(c), note)
        st.pending.append(_Pending(att, pre, sha))
        st.applied_any = True

    def _drop_reference_writes(self, att: FixAttempt, pre: str) -> None:
        """The merge-conflict reference copies the prompt showed are not in the build (they live under the run dir):
        a script that wrote a file at such a path wrote junk into the project — undone before the commit (round 9:
        "never a patch target")."""
        refs = self._ref_paths.get(id(att))
        if not refs:
            return
        try:
            hit = [f for f in self.ws.changed_files_since(pre) if _norm_path(f) in refs]
            for f in hit:
                tracked = self.ws._git("cat-file", "-e", f"{pre}:{f}", check=False).returncode == 0   # noqa: SLF001
                if tracked:
                    self.ws._git("checkout", pre, "--", f, check=False)                            # noqa: SLF001
                elif self.ws.exists(f):
                    self.ws.delete(f)
        except Exception as e:  # noqa: BLE001 - best effort; the diff shows what stayed
            log.warning("could not undo writes to reference copies for %s: %s", att.id, e)
            return
        if hit:
            att.notes.append(f"dropped {len(hit)} change(s) aimed at a read-only reference copy: {', '.join(hit[:3])}")

    def _log_script_result(self, st: "_Round", c: ErrorCluster, att: FixAttempt) -> None:
        """`[verify it2] case abc123 (compile, 7 error(s) in 4 file(s)): script v2 reuse — ok, 4 file(s) changed`."""
        res = att.script_result
        if res is None:
            what = "no script — " + _first_sentence("; ".join(att.apply_errors) or "nothing to run", 200)
        elif res.status != "ok":
            what = f"{res.status} — {_first_sentence(res.error or '-', 200)} (nothing applied)"
        elif att.applied:
            what = f"ok, {len(att.changes)} file(s) changed"
        else:
            what = "ok, no file changed"
        log.info("[%s it%d] %s: script v%s %s — %s", st.phase.value, st.iteration, _head(c), att.script_version,
                 att.script_mode or "?", what)

    def _credit_script(self, att: FixAttempt) -> None:
        """`kb.scripts.credit(case, version, outcome)` once per attempt and final outcome (an inconclusive one is
        credited again when a revert rebuild scores it); unstored (v0) scripts / dry run / no KB → nothing."""
        if self.dry_run or att.decision.kind != DecisionKind.SCRIPT or not att.script_version \
                or not att.script_case_id or att.outcome == "pending":
            return
        done = self._credited.get(id(att))
        if done is not None and done != "inconclusive":
            return
        store = self.scripts
        if store is None:
            return
        try:
            store.credit(att.script_case_id, att.script_version, att.outcome, project=self._project(),
                         run_id=self.ws.run_id)
        except Exception as e:  # noqa: BLE001 - the store says it never raises
            log.warning("kb.scripts.credit(%s v%s) failed: %s", att.script_case_id, att.script_version, e)
            return
        self._credited[id(att)] = att.outcome
        self.ws.journal({"event": "script_credit", "case": att.script_case_id, "version": att.script_version,
                         "attempt": att.id, "outcome": att.outcome})

    def _credit_scripts(self, pr: PhaseResult, attempts: list[FixAttempt]) -> None:
        for a in attempts:
            self._credit_script(a)

    def _no_script_fixer(self, att: FixAttempt, c: ErrorCluster, why: str) -> None:
        """A v1 tier-2 fixer without `propose_script`: no LLM script can be written in this run (stored versions are
        still reused). Not an infrastructure problem (no `llm-unavailable` / `infra:llm`): flag
        `fixer-without-scripts` + one follow-up."""
        if not self.llm_unavailable:
            self.llm_unavailable = why[:300]
            setter = getattr(self.triager, "set_llm_unavailable", None)
            if callable(setter):
                setter(self.llm_unavailable)
            self._flag(FIXER_WITHOUT_SCRIPTS_FLAG)
            self._followup(f"{why} — no LLM fix script could be written in this run; the cases that needed one are "
                           "blocked (llm_unavailable)")
            log.error("%s", why)
            self.ws.journal({"event": "fixer_without_scripts", "phase": att.phase.value, "iteration": att.iteration,
                             "reason": why})
        self._block_simple(att.phase, att.iteration, c, BlockReason.LLM_UNAVAILABLE, self.llm_unavailable)

    def _new_attempt(self, phase: Phase, iteration: int, n: int, c: ErrorCluster, d: TriageDecision,
                     build: BuildResult) -> FixAttempt:
        att = FixAttempt(id=f"{phase.value}-{iteration}-{n}", phase=phase, iteration=iteration, cluster_id=c.id,
                         decision=d, errors_before=build.error_count, cluster_signatures=list(c.signatures),
                         cluster_errors=list(c.errors))   # KB harvest needs the errors (L2 signatures, sanitized copies)
        try:   # error cases: `git diff base_commit..commit` = the exact code the attempt applied
            att.base_commit = self.ws.head()
        except Exception as e:  # noqa: BLE001 - a workspace without git: no diff, nothing else changes
            log.debug("base commit for %s unavailable: %s", att.id, e)
        return att

    def _record(self, pr: PhaseResult, att: FixAttempt) -> None:
        pr.attempts.append(att)
        self.history.append(att)
        self.ws.journal({"event": "attempt", "phase": att.phase.value, "iteration": att.iteration,
                         "attempt": att.id, "cluster": att.cluster_id, "decision": att.decision.kind.value,
                         "handler": att.decision.handler_id, "recipes": att.decision.recipe_ids,
                         "applied": att.applied, "commit": att.commit, "outcome": att.outcome,
                         "model": att.model, "files": [ch.path for ch in att.changes],
                         "apply_errors": att.apply_errors, "analysis": att.analysis[:500],
                         "advisory_ids": list(att.advisory_ids),
                         "script": ({"mode": att.script_mode, "case": att.script_case_id,
                                     "version": att.script_version, "parent": att.script_parent,
                                     "status": att.script_result.status if att.script_result is not None else None,
                                     "error": (att.script_result.error or "")[:300] if att.script_result else "",
                                     "log_lines": len(att.script_result.log) if att.script_result else 0}
                                    if att.decision.kind == DecisionKind.SCRIPT else None)})
        self._track("note_attempt", att)

    def _commit(self, message: str, pre: str) -> Optional[str]:
        if not self.ws.is_dirty():
            return None
        sha = self.ws.commit(message)
        return sha if sha != pre else None

    def _fail(self, att: FixAttempt, why: str) -> None:
        att.outcome = "apply_failed"
        att.apply_errors.append(why)
        try:
            if self.ws.is_dirty():
                self.ws.discard_uncommitted()
        except Exception:  # pragma: no cover
            pass

    def _summary(self) -> str:
        if self._profile_summary is None:
            self._profile_summary = profile_summary(self.profile)
        return self._profile_summary

    def _advisory_hints(self, c: ErrorCluster) -> tuple[list[str], list[str]]:
        """(hints, advisory ids) of the run's library advisories that match this cluster (sbmig.advisory.hints_for)."""
        if not self.advisories:
            return [], []
        try:
            from sbmig.advisory.hints import hints_for
            root = getattr(self.cfg, "repo_root", None)
            root = Path(root) if isinstance(root, (str, Path)) else None
            hints, ids = hints_for(c.errors, self.advisories, self.profile, notes_root=root)
        except Exception as e:  # pragma: no cover - hints_for never raises; stay defensive about imports
            log.warning("advisory hints unavailable: %s", e)
            return [], []
        if ids:
            log.debug("advisory hints for %s (%s):\n%s", c.id, ", ".join(ids), "\n".join(f"- {h}" for h in hints))
        return list(hints), list(ids)

    def _llm_transport_failure(self, att: FixAttempt, c: ErrorCluster, why: str) -> None:
        """Two transport/config failures in a row (L5): the first such streak of the run pauses the loop for
        `llm.cooldown_seconds` and retries (the streak's attempts do not count against the per-cluster LLM limit);
        a second streak — or a streak with no cool-down configured / left in the time budget — marks the LLM
        unavailable for the rest of the run."""
        self._llm_streak.append((c, why))
        self._streak_attempts.append(att.id)
        att.notes.append(f"LLM transport/config failure: {why[:200]}")
        if self.llm_unavailable or len(self._llm_streak) < 2:
            return
        if self._llm_cooldown(att, why):
            return
        self.llm_unavailable = why[:300] or "LLM transport errors"
        self._flag("llm-unavailable")
        setter = getattr(self.triager, "set_llm_unavailable", None)
        if callable(setter):
            setter(self.llm_unavailable)
        self._followup(f"the LLM was unavailable during this run ({self.llm_unavailable}); cases that needed it "
                       "are listed as blocked (llm_unavailable) — proven stored fix scripts were still reused")
        self.ws.journal({"event": "llm_unavailable", "phase": att.phase.value, "iteration": att.iteration,
                         "reason": self.llm_unavailable, "clusters": [x.id for x, _ in self._llm_streak],
                         "cooldown_used": self.llm_cooldown_used})
        for x, _w in self._llm_streak:
            self._block_simple(att.phase, att.iteration, x, BlockReason.LLM_UNAVAILABLE,
                               f"LLM unavailable ({self.llm_unavailable})")
        self._llm_streak, self._streak_attempts = [], []

    def _llm_config_check(self, att: FixAttempt, c: ErrorCluster, analysis: str, prompt_hash: str) -> bool:
        """Round 10: the gateway answered the same HTTP 400 body to LLM_CONFIG_ERROR_PROMPTS different prompts → not
        the prompts' fault but the configuration (an unsupported model name, a payload field): stop calling the LLM
        for the run (flag `llm-config-error`, one follow-up with the short body). True = this attempt ends here."""
        body = llm_400_body(analysis)
        if body is None:
            return False
        self._fail(att, analysis.strip()[:300])
        seen = self._http400.setdefault(body, set())
        seen.add(prompt_hash or f"{c.id}:{att.id}")
        if len(seen) < LLM_CONFIG_ERROR_PROMPTS or self.llm_unavailable:
            return True
        self.llm_config_error = body
        self.llm_unavailable = f"LLM configuration error: HTTP 400 '{body[:120]}' for {len(seen)} different prompts"
        self._flag("llm-config-error")
        setter = getattr(self.triager, "set_llm_unavailable", None)
        if callable(setter):
            setter(self.llm_unavailable)
        log.error("LLM configuration error: the gateway answered HTTP 400 '%s' to %d different prompts — no more LLM "
                  "calls in this run", body[:200], len(seen))
        self._followup(f"the LLM gateway rejected {len(seen)} different prompts with the same HTTP 400 ({body[:160]}) "
                       "— a configuration error (model name / payload fields in .env or llm.*), not the prompts; LLM "
                       "calls stopped for this run, fix the setting and rerun")
        self.ws.journal({"event": "llm_config_error", "phase": att.phase.value, "iteration": att.iteration,
                         "body": body, "prompts": len(seen)})
        self._block_simple(att.phase, att.iteration, c, BlockReason.LLM_UNAVAILABLE, self.llm_unavailable)
        return True

    def _llm_cooldown(self, att: FixAttempt, why: str) -> bool:
        """Pause once per run instead of giving up on the LLM (round 9). False = no cool-down (used up, disabled,
        or it would run past the run's time budget) → the caller marks the LLM unavailable."""
        seconds = max(0, int(getattr(self.cfg.llm, "cooldown_seconds", 0) or 0))
        if self.llm_cooldown_used or seconds <= 0:
            return False
        b = self.budget
        if b.max_minutes > 0 and b.time_left() <= seconds:
            att.notes.append(f"LLM cool-down skipped: less than {seconds}s left of the run time budget")
            return False
        clusters = [x.id for x, _ in self._llm_streak]
        self.llm_cooldown_used = True
        self._flag("llm-cooldown")
        att.notes.append(f"LLM cool-down: 2 transport/config failures in a row → pausing {seconds}s, then retrying "
                         "(once per run; the next streak marks the LLM unavailable)")
        self.ws.journal({"event": "llm_cooldown", "phase": att.phase.value, "iteration": att.iteration,
                         "seconds": seconds, "reason": why[:300], "clusters": clusters})
        log.warning("LLM transport failed twice in a row (%s) — cooling down %ss before retrying", why[:200], seconds)
        (self._sleep or sleep_seconds)(seconds)
        self._cooled_attempts |= set(self._streak_attempts)
        self._llm_streak, self._streak_attempts = [], []
        return True

    # ------------------------------------------------------------------ merge-conflict hints (round 9)
    def _merge_conflict_hints(self, att: FixAttempt, c: ErrorCluster) -> tuple[list[str], dict[str, str]]:
        """(reference_notes, reference_files) for the first-wins merge conflicts relevant to `c`: by file (the kept
        file is the cluster's / an error's file) or by symbol (an error names the kept class's simple name)."""
        if not self.merge_conflicts:
            return [], {}
        try:
            return self._merge_conflict_hints_unsafe(att, c)
        except Exception as e:  # pragma: no cover - hints never break a fix attempt
            log.warning("merge-conflict hints unavailable for %s: %s", c.id, e)
            return [], {}

    def _merge_conflict_hints_unsafe(self, att: FixAttempt, c: ErrorCluster) -> tuple[list[str], dict[str, str]]:
        lay = self.cfg.layout
        rp = getattr(self.profile, "root_pom", None)
        pname = (getattr(rp, "artifact_id", None) or getattr(self.profile, "name", "") or "")
        base = _norm_path(lay.module_path("comp", pname) or f"{lay.module_dir}/{lay.comp_module}")
        files = {_norm_path(f) for f in [c.file, *(e.file for e in c.errors)] if f}
        text = "\n".join(f"{e.message}\n{e.symbol or ''}\n{e.location or ''}\n{e.raw}" for e in c.errors)
        notes: list[str] = []
        refs: dict[str, str] = {}
        for mc in self.merge_conflicts:
            if mc.identical or getattr(mc, "union", False) or not mc.saved_copies:
                continue
            kept = _norm_path(f"{base}/{mc.path}")
            rel = _norm_path(mc.path)
            by_file = kept in files
            stem = PurePosixPath(rel).name
            stem = stem[:-len(".java")] if stem.endswith(".java") else ""
            by_symbol = bool(stem) and re.search(rf"(?<![\w$]){re.escape(stem)}(?![\w$])", text) is not None
            if not (by_file or by_symbol):
                continue
            dropped = ", ".join(f"`{d}`" for d in mc.dropped_from) or "another module"
            notes.append(f"{kept} was merged first-wins from module `{mc.kept_from}`; the copy from {dropped} was "
                         "dropped (saved under the run directory). If this error is about a member that only the "
                         "dropped copy had, port it from the reference copy below — do not re-create a second module.")
            att.notes.append(f"merge-conflict hint: {kept}")
            for saved in mc.saved_copies:
                if len(refs) >= REFERENCE_MAX_FILES:
                    break
                label = _norm_path(saved)
                try:
                    content = (self.ws.run_dir / saved).read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    notes.append(f"(the saved copy {label} could not be read: {type(e).__name__})")
                    continue
                if len(content) > REFERENCE_MAX_CHARS:
                    content = content[:REFERENCE_MAX_CHARS - len(_TRUNCATED_MARK)] + _TRUNCATED_MARK
                refs[label] = content
        if notes:
            self.ws.journal({"event": "merge_conflict_hints", "phase": att.phase.value, "iteration": att.iteration,
                             "cluster": c.id, "reference_files": list(refs)})
        return notes, refs

    def _changed_between(self, a: str, b: str) -> list[str]:
        changed = getattr(self.ws, "changed_between", None)          # NUL-separated: Korean / spaced names verbatim
        if callable(changed):
            return changed(a, b)
        out = self.ws._git("diff", "--name-only", a, b).stdout   # noqa: SLF001 - a stand-in workspace without it
        return [l.strip() for l in out.splitlines() if l.strip()]

    # ------------------------------------------------------------------ fallback
    def _maybe_skip_test_compile(self, pr: PhaseResult, phase: Phase, goal: str, target: Optional[str],
                                 last: BuildResult) -> None:
        if not self.cfg.build.allow_skip_test_compile_fallback:
            return
        if not any(g in goal.split() for g in TEST_COMPILING_GOALS):
            return
        if not last.errors or any(e.kind != ErrorKind.TEST_COMPILE for e in last.errors):
            return
        self.ws.journal({"event": "test_compile_fallback", "phase": phase.value, "test_errors": len(last.errors)})
        res = self._build(goal, target, f"{phase.value}-skip-tests", phase, pr.iterations + 1, skip_test_compile=True)
        pr.iterations += 1
        pr.iteration_records.append(IterationRecord(phase=phase, iteration=pr.iterations, build_ok=res.ok,
                                                    error_count=res.error_count,
                                                    signatures=res.signature_multiset(), build_log=res.log_path))
        if not res.ok:
            pr.notes.append("test-compile fallback (-Dmaven.test.skip=true) also failed")
            return
        pr.build_ok = True
        self.test_compile_skipped = True
        self.test_compile_skipped_targets.add(_target_key(target))
        self._flag("test-compile-skipped")
        pr.remaining_errors = list(last.errors)
        pr.notes.append(f"test-compile-skipped: main code builds, {len(last.errors)} test compile error(s) remain "
                        "(-Dmaven.test.skip=true)")
        for e in last.errors:
            where = f"{e.file}:{e.line}" if e.file else (e.module or "?")
            self._followup(f"test sources do not compile (build passes only with -Dmaven.test.skip=true): "
                           f"{where} {e.message}")

    def _maybe_skip_tests(self, pr: PhaseResult, phase: Phase, goal: str, target: Optional[str],
                          last: BuildResult) -> None:
        """Round 8: main code compiles and packages, only runtime test failures (TEST_FAILURE) remain — usually an
        externalized setting or infrastructure not reachable at build time. Rebuild with -DskipTests; on success the
        phase passes with flag `tests-skipped` and every failing test as a follow-up (and unresolved at finalize)."""
        if not getattr(self.cfg.build, "allow_skip_tests_fallback", False):
            return
        if not any(g in goal.split() for g in TEST_RUNNING_GOALS):
            return
        if not last.errors or any(e.kind != ErrorKind.TEST_FAILURE for e in last.errors):
            return
        self.ws.journal({"event": "tests_fallback", "phase": phase.value, "failing": len(last.errors)})
        res = self._build(goal, target, f"{phase.value}-skip-tests-run", phase, pr.iterations + 1, skip_tests=True)
        pr.iterations += 1
        pr.iteration_records.append(IterationRecord(phase=phase, iteration=pr.iterations, build_ok=res.ok,
                                                    error_count=res.error_count,
                                                    signatures=res.signature_multiset(), build_log=res.log_path))
        if not res.ok:
            pr.notes.append("tests fallback (-DskipTests) also failed")
            return
        pr.build_ok = True
        self.tests_skipped = True
        self.tests_skipped_targets.add(_target_key(target))
        self._flag("tests-skipped")
        pr.remaining_errors = list(last.errors)
        pr.notes.append(f"tests-skipped: main code builds and packages, {len(last.errors)} test(s) fail at runtime "
                        "(-DskipTests)")
        for e in last.errors:
            who = e.symbol or e.module or "?"
            self._followup(f"test fails at runtime (packaged with -DskipTests): {who} — {e.message} — likely an "
                           "externalized setting (see Handover 'Properties to set') or infrastructure not reachable "
                           "at build time")

    def _flag(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)

    def _followup(self, text: str) -> None:
        if text not in self.followups:
            self.followups.append(text)


def _synthetic(e: BuildError) -> bool:
    """The runner's stand-in error for a crashed / timed-out Maven: says nothing about how far the build got."""
    return e.kind == ErrorKind.UNKNOWN and not e.file and (e.message or "").startswith(_SYNTHETIC_PREFIXES)


def _pair(e: BuildError) -> tuple[str, str]:
    """(file, signature) of an error — file '' for module / artifact errors. L1 signatures carry no file: the same
    renamed method in two files is one signature, so scoring compares these pairs (with their occurrence counts)."""
    return (_norm_path(e.file) if e.file else ""), (e.signature or f"{e.kind.value}|{e.message}")


def _touched(a: FixAttempt) -> set[str]:
    return {_norm_path(p) for c in a.changes for p in (c.path, c.new_path) if p}


def _targeted_left(a: FixAttempt, cur: Counter) -> tuple[int, int]:
    """(targeted errors still in the build, targeted errors) — by (file, signature) occurrence; attempts without
    recorded errors fall back to the cluster signatures (all or nothing)."""
    if not a.cluster_errors:
        sigs = set(a.cluster_signatures)
        return (1 if any(s in sigs for _f, s in cur) else 0), 1
    targeted = Counter(_pair(e) for e in a.cluster_errors)
    return sum(min(n, cur[k]) for k, n in targeted.items()), sum(targeted.values())


def _cluster_still_failing(a: FixAttempt, cur: set[str], cur_pairs: set[tuple[str, str]]) -> bool:
    """Is any error of the attempt's cluster in the new build? (kept for callers; `_score` counts occurrences)"""
    if not a.cluster_errors:
        return bool(set(a.cluster_signatures) & cur)
    return any(((_norm_path(e.file), e.signature) in cur_pairs) if e.file else (e.signature in cur)
               for e in a.cluster_errors)


def _stage_of(errors: list[BuildError]) -> int:
    """The earliest build stage (error_tracker.STAGE) at which these errors are reported; 99 when none has one."""
    return min((STAGE[e.kind] for e in errors if e.kind in STAGE), default=99)


def _visible_in(errors: list[BuildError], build: BuildResult, real: list[BuildError]) -> bool:
    """Could `build` have reported these (now absent) errors? Their module was built, or the build got at least as far
    as their stage — the same notion as the error tracker's visibility (round 10: "not visible" ≠ "resolved")."""
    if build.ok:
        return True
    if not real:
        return False
    built = set(build.modules_built or [])
    failed = {build.failed_module} | {x.module for x in real if x.module}
    # how far the build got: an UNKNOWN error without a file (a crashed JVM, `mvn` itself failing, a generic BUILD
    # FAILURE line) proves no stage
    reached = max((STAGE.get(x.kind, -1) for x in real if x.kind != ErrorKind.UNKNOWN or x.file), default=-1)
    for e in errors:
        if _synthetic(e):
            return False                         # a stand-in error is never "fixed" by disappearing
        m = e.module
        if m and m in built:
            continue
        if m and build.failed_module and m not in failed:
            return False                         # its module never ran (skipped after another one failed)
        st = STAGE.get(e.kind)
        if st is None or reached < st:
            return False
    return True


def _new_errors(errors: list[BuildError], prev: Counter) -> list[BuildError]:
    """The occurrences of (file, signature) beyond what the previous build had."""
    seen: Counter = Counter()
    out: list[BuildError] = []
    for e in errors:
        k = _pair(e)
        seen[k] += 1
        if seen[k] > prev[k]:
            out.append(e)
    return out


def _new_in_files(errors: list[BuildError], files: set[str], prev: Counter) -> list[BuildError]:
    """New errors (by (file, signature) occurrence) in `files` — round 10: a new error whose L1 signature another file
    already had is still new here."""
    return [e for e in _new_errors([e for e in errors if e.file and _norm_path(e.file) in files], prev)]


def _target_key(target: Optional[str]) -> str:
    t = _norm_path(target or "").strip("/")
    if t.endswith("pom.xml"):
        t = t[:-len("pom.xml")].rstrip("/")
    return t


def _gone(errors: list[BuildError], build: BuildResult) -> list[BuildError]:
    """The occurrences of `errors` (by (file, signature)) that `build` no longer has — what an attempt removed."""
    cur = Counter(_pair(e) for e in build.errors)
    seen: Counter = Counter()
    out: list[BuildError] = []
    for e in errors:
        k = _pair(e)
        seen[k] += 1
        if seen[k] > cur[k]:
            out.append(e)
    return out


def _change_line(a: FixAttempt) -> str:
    """One line of what an attempt changed (sibling hints): its first change, or its analysis; a script attempt: the
    files it changed."""
    if a.decision.kind == DecisionKind.SCRIPT and a.changes:
        files = [c.new_path or c.path for c in a.changes]
        return f"changed {len(files)} file(s): {', '.join(files[:4])}" + (" …" if len(files) > 4 else "")
    for c in a.changes[:1]:
        if c.op == "modify" and (c.search or c.replace):
            s = (c.search or "").strip().splitlines()
            r = (c.replace or "").strip().splitlines()
            return f"MODIFY {c.path}: `{(s[0] if s else '')[:90]}` → `{(r[0] if r else '')[:90]}`"
        return f"{c.op.upper()} {c.path}"
    return _first_sentence(a.analysis or "", 160) or "-"


def _norm_path(p: str) -> str:
    p = (p or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _modify(path: str):
    from sbmig.core.models import FileChange
    return FileChange(path=path, op="modify")
