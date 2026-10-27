"""Error-case tracker (2026-10-23, docs/INTERFACES.md "Error cases"): every build error of a run on its own record.

Business requirement: the knowledge base must say, for EVERY build error of a run (N errors → N records), what it
was, what was done about it, how it was resolved (with the code that was applied) or why it was not and which errors
were the problem — failed fix attempts included. The fix loop and the orchestrator feed this tracker (all hooks are
optional and guarded: a tracker problem never breaks a run):

    note_step        a plan step outside the fix loop changed the tree (rewrite / restructure / review)
    observe          a build (the loop after scoring, the orchestrator after each final `clean package`)
    observe_clusters the triage clusters of the last observed build (which errors a decision was about)
    note_attempt     a recorded fix attempt (any outcome)
    note_rollback    v2: the loop rolled a script attempt back before the case's next try (its fixes are undone)
    note_decision    a triage decision (SKIP / ESCALATE / BLOCKED rationales → "why no attempt")
    finalize         raw `ErrorCaseRecord`s + `FixAttemptRecord`s; `sbmig.kb.error_cases.prepare_error_cases`
                     sanitizes them and fills the project / change-classification / label fields

Identity of an error = (kind, file from its first `src/` segment on, signature or message[:120]) — it survives the
restructure moving files into `{name}-back/`; several occurrences of one identity in a build are matched by exact
file first, then message, then nearest line. A targeted build (MSA per-service loops, `observe(target=…)`) only
matches records of its own scope (round 10: svc-b's `src/main/java/X.java` never merges into svc-a's open record; a
record of the root `src/` still follows the restructure move). An error absent from a later build is only "gone" when
that build could have shown it (it got far enough: same or later stage, the error's module built, a goal that reaches
it, tests not skipped) — otherwise it is masked (a pom error hides every compile error) and stays open; a closed
error that shows up again reopens.

Round 10: a new error records `introduced_by` (the attempts scored in the build where it first appeared that changed
its file, else the plan steps noted since the previous build); attempt diffs are passed in FULL (`prepare` classifies
them and only then applies `kb.error_diff_chars`).

v2 (flow-v2) fix scripts: a SCRIPT attempt's fix method follows its mode (new → `script`, reuse → `script_reuse`,
augment → `script_augment`), its resolver is `script:<case_id>:v<N>`, its record carries the script fields (status,
error, the script's own log ≤ 50 lines) and its description is the LLM analysis (a reuse: the stored version's); a
resolved error names the resolving attempt's `script_case_id` / `script_version`; `llm_attempts` counts the attempts
that asked the LLM (`script`, `script_augment` — a reuse made no call). Every attempt of the v2 flow is a SCRIPT
attempt; anything else is recorded with fix method `none`. An attempt the loop rolled back before
the case's next try (note `rolled back before …`) is no longer in the tree: never part of a resolution chain.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from sbmig.core.models import (
    BlockedItem,
    BuildError,
    BuildResult,
    DecisionKind,
    ErrorAttemptRef,
    ErrorCaseRecord,
    ErrorCluster,
    ErrorKind,
    FixAttempt,
    FixAttemptRecord,
    Phase,
    TriageDecision,
)

log = logging.getLogger("sbmig.loop")

# how far a build got when it stopped with an error of this kind (an absent error of stage s is only "gone" when the
# build reached s); plugin_execution has no stage — it is only proven gone by its module building
STAGE: dict[ErrorKind, int] = {
    ErrorKind.POM_INVALID: 0, ErrorKind.PARENT_RESOLUTION: 0,
    ErrorKind.DEPENDENCY_RESOLUTION: 1, ErrorKind.PLUGIN_RESOLUTION: 1,
    ErrorKind.ENFORCER: 2, ErrorKind.REWRITE: 2,
    ErrorKind.COMPILE: 3, ErrorKind.UNKNOWN: 3,
    ErrorKind.TEST_COMPILE: 4,
    ErrorKind.TEST_FAILURE: 6,
}
# Maven lifecycle rank of a goal: an ok `compile` build says nothing about a test-compile error seen in a
# `test-compile` build, nor about a packaging error seen in a `package` build
GOAL_RANK: dict[str, int] = {
    "validate": 0, "initialize": 0, "generate-sources": 1, "process-sources": 1, "generate-resources": 1,
    "process-resources": 1, "compile": 1, "process-classes": 1, "generate-test-sources": 2,
    "process-test-sources": 2, "generate-test-resources": 2, "process-test-resources": 2, "test-compile": 2,
    "process-test-classes": 2, "test": 3, "prepare-package": 4, "package": 4, "pre-integration-test": 5,
    "integration-test": 5, "post-integration-test": 5, "verify": 5, "install": 6, "deploy": 7,
}
UNKNOWN_GOAL_RANK = 99                 # a goal we cannot place (a plugin goal): do not restrict visibility by goal
KIND_MIN_RANK: dict[ErrorKind, int] = {ErrorKind.COMPILE: 1, ErrorKind.TEST_COMPILE: 2, ErrorKind.TEST_FAILURE: 3}
TEST_KINDS = (ErrorKind.TEST_COMPILE, ErrorKind.TEST_FAILURE)

MAX_RAW = 2000                         # stored raw text per error (prepare_error_cases caps at the same size)
MAX_ATTEMPT_REFS = 20                  # attempt history per error (first half + latest half when longer)
MAX_NOTES = 20
MAX_LIST = 10                          # introduced / remaining / problem errors
MAX_MASKED_BY = 5
ANALYSIS_CHARS = 1500                  # LLM analysis in FixAttemptRecord.description
TEXT_CHARS = 600                       # failure texts, rationales
TRUNCATED_MARK = "\n… (truncated)"
MAX_DIFF_CHARS = 4_000_000             # a pathological diff only (a regenerated file); kb.error_diff_chars caps later
_SCRIPT_METHOD: dict[str, str] = {"new": "script", "reuse": "script_reuse", "augment": "script_augment"}
LLM_METHODS = ("script", "script_augment")   # fix methods that called the LLM (llm_attempts)
SCRIPT_LOG_LINES = 50                  # FixAttemptRecord.script_log
ROLLED_BACK_NOTE = "rolled back before "   # the fix loop's note on a script attempt it rolled back (no longer in the tree)
_SYNTHETIC_PREFIXES = ("mvn exited with code", "build timed out")


# ------------------------------------------------------------------------------------------------------------- helpers
def _posix(path: Optional[str]) -> str:
    p = (path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def norm_file(path: Optional[str]) -> str:
    """The path from its first `src/` segment on ('shop-back/src/main/java/a/B.java' → 'src/main/java/a/B.java'),
    else the whole (posix) path; '' without a file."""
    p = _posix(path)
    if not p:
        return ""
    parts = p.split("/")
    return "/".join(parts[parts.index("src"):]) if "src" in parts else p


def identity_of(e: BuildError) -> tuple[str, str, str]:
    return e.kind.value, norm_file(e.file), e.signature or (e.message or "")[:120]


def fmt_error(e: BuildError) -> str:
    """'file:line message' (module or '-' without a file) — the form of problem / introduced / remaining errors."""
    loc = e.file or e.module or "-"
    if e.file and e.line:
        loc += f":{e.line}"
    msg = " ".join((e.message or e.raw or "").split())
    return f"{loc} {msg[:300]}".strip()


def _short(text: str, limit: int = TEXT_CHARS) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[:limit - 1] + "…"


def _cap_error(e: BuildError) -> BuildError:
    if len(e.raw or "") <= MAX_RAW:
        return e
    return e.model_copy(update={"raw": e.raw[:MAX_RAW] + " …"})


def _goal_rank(goal: str) -> int:
    ranks = [GOAL_RANK[t] for t in (goal or "").split() if t in GOAL_RANK]
    return max(ranks) if ranks else UNKNOWN_GOAL_RANK


def _synthetic(e: BuildError) -> bool:
    """The runner's stand-in error for a crashed / timed-out Maven: says nothing about how far the build got."""
    return e.kind == ErrorKind.UNKNOWN and not e.file and (e.message or "").startswith(_SYNTHETIC_PREFIXES)


def _test_skips(build: BuildResult) -> tuple[bool, bool]:
    """(test sources not compiled, tests not run) for this build's command line."""
    cmd = [str(c) for c in (build.command or [])]
    no_compile = any(c in ("-Dmaven.test.skip=true", "-Dmaven.test.skip") for c in cmd)
    no_run = no_compile or any(c in ("-DskipTests", "-DskipTests=true") for c in cmd)
    return no_compile, no_run


def _match_key(e: BuildError) -> tuple:
    return identity_of(e), e.file, e.line, e.message


def _method(att: FixAttempt) -> str:
    if att.decision.kind == DecisionKind.SCRIPT:
        return _SCRIPT_METHOD.get(att.script_mode or "", "script")
    return "none"


def _rolled_back(att: FixAttempt) -> bool:
    return any(str(n).startswith(ROLLED_BACK_NOTE) for n in att.notes or [])


def _resolver(att: FixAttempt) -> str:
    """`script:<case_id>:v<N>` of a script attempt; "" otherwise."""
    if att.decision.kind == DecisionKind.SCRIPT and att.script_case_id:
        return f"script:{att.script_case_id}:v{att.script_version}"
    return ""


def _touched(att: FixAttempt) -> set[str]:
    out: set[str] = set()
    for ch in att.changes:
        for p in (ch.path, ch.new_path):
            if p:
                out.add(norm_file(p))
    return out


def _target_dir(target: Optional[str]) -> str:
    """The module directory of a build target ('service-a/pom.xml' → 'service-a'); '' = the whole project."""
    t = _posix(target).strip("/")
    if t.endswith(".xml"):
        t = t.rsplit("/", 1)[0] if "/" in t else ""
    return t


# --------------------------------------------------------------------------------------------------------- state rows
@dataclass
class _Rec:
    """One error occurrence of the run (→ one ErrorCaseRecord)."""
    key: int
    identity: tuple[str, str, str]
    occurrence: int
    error: BuildError                      # latest copy (current path after a restructure move)
    first_seen: str
    first_phase: Phase
    last_seen: str
    last_obs: int                          # index of the last observation it was present in
    min_rank: int                          # lowest goal rank of the builds it was present in
    builds_present: int = 1
    status: str = "open"                   # open | resolved | resolved_indirectly
    state: str = "present"                 # while open: present | masked
    masked_since: str = ""
    masked_by: list[str] = field(default_factory=list)
    scope_ok: bool = False                 # outcome of the last build that covered it
    resolved_at: str = ""
    resolved_phase: Optional[Phase] = None
    fix_method: str = "none"
    resolver_id: str = ""
    resolved_by: Optional[str] = None      # attempt key, or "step:<id>"
    resolved_att: Optional[str] = None     # attempt key of the resolver (FixAttemptRecord id at finalize)
    chain: list[str] = field(default_factory=list)              # attempt keys of the resolution (run order)
    open_from: int = 0                     # len(_att_order) when it (re)appeared: earlier attempts predate it
    introduced_by: list[str] = field(default_factory=list)      # attempt keys / "step:<id>" before its first build
    targeted: bool = False
    description: str = ""
    attempt_keys: list[str] = field(default_factory=list)
    omitted_attempts: int = 0
    decision: str = ""                     # last triage rationale for a cluster that contained it
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        if text not in self.notes:
            if len(self.notes) >= MAX_NOTES:
                self.notes.pop(MAX_NOTES // 2)
            self.notes.append(text)

    def add_attempt(self, key: str) -> None:
        if key in self.attempt_keys:
            return
        if len(self.attempt_keys) >= MAX_ATTEMPT_REFS:     # keep the first attempts and the latest ones
            self.attempt_keys.pop(MAX_ATTEMPT_REFS // 2)
            self.omitted_attempts += 1
        self.attempt_keys.append(key)


@dataclass
class _Att:
    key: str                               # FixAttempt.id (+ "#n" when an id repeats: MSA loops per service)
    att: FixAttempt
    targets: list[int] = field(default_factory=list)            # record keys of its cluster's errors
    target_ids: set = field(default_factory=set)                # identities of its cluster's errors
    scored_at: str = ""
    scored_obs: int = -1                   # index of the observation that scored it
    remaining: list[str] = field(default_factory=list)
    scoring_note: str = ""


@dataclass
class _Obs:
    label: str
    phase: Phase
    void: bool
    ok: bool
    scored: list[_Att] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------------------------- tracker
class ErrorTracker:
    """One per run (created by the orchestrator, not in dry-run). Every public method catches and logs."""

    def __init__(self, ws: Any, cfg: Any):
        self.ws = ws
        self.cfg = cfg
        self.problems: list[str] = []      # internal failures (the orchestrator flags them: error-cases-failed)
        self._recs: list[_Rec] = []
        self._by_identity: dict[tuple, list[_Rec]] = {}
        self._obs: list[_Obs] = []
        self._labels: dict[str, int] = {}
        self._steps: list[str] = []
        self._atts: dict[int, _Att] = {}   # id(FixAttempt) → row (the tracker keeps the object alive)
        self._att_order: list[_Att] = []
        self._att_ids: dict[str, int] = {}
        self._last_match: dict[tuple, int] = {}
        self._clusters: dict[str, list[int]] = {}

    # ------------------------------------------------------------------ public hooks
    def note_step(self, step_id: str, phase: Phase) -> None:
        try:
            if step_id and step_id not in self._steps:
                self._steps.append(str(step_id))
        except Exception as e:  # pragma: no cover - defensive
            self._problem("note_step", e)

    def observe(self, phase: Phase, label: str, build: BuildResult, scored: Optional[list[FixAttempt]] = None, *,
                void: bool = False, target: Optional[str] = None) -> None:
        """`target` (additive to the contract): the module directory a targeted build covered (MSA per-service
        loops / verify steps) — errors outside it are neither present nor absent in that build. The build's errors
        get their refined case keys first (`sbmig.triage.cluster.refine_signatures`, idempotent — the loop and the
        orchestrator already refined them), so a record's L2 is the case the fix loop worked on."""
        try:
            if build is not None and build.errors:
                from sbmig.triage.cluster import refine_signatures
                refine_signatures(build.errors)
            self._observe(phase, label, build, list(scored or []), void, target)
        except Exception as e:
            self._problem(f"observe({label})", e)

    def observe_clusters(self, clusters: list[ErrorCluster]) -> None:
        try:
            out: dict[str, list[int]] = {}
            for c in clusters or []:
                keys: list[int] = []
                for e in c.errors:
                    for k in self._keys_for(e):
                        if k not in keys:
                            keys.append(k)
                out[c.id] = keys
            self._clusters = out
        except Exception as e:
            self._problem("observe_clusters", e)

    def note_attempt(self, att: FixAttempt) -> None:
        try:
            self._ensure_att(att)
        except Exception as e:
            self._problem(f"note_attempt({getattr(att, 'id', '?')})", e)

    def note_rollback(self, att: FixAttempt) -> None:
        """v2: `att` was rolled back before its case's next attempt — the errors it resolved are back in the tree
        (present as of the last build, reopened); it is no candidate for later closes or chains."""
        try:
            x = self._atts.get(id(att))
            if x is None:
                return
            last = len(self._obs) - 1
            for r in self._recs:
                if r.status == "open" or r.resolved_att != x.key:
                    continue
                r.note(f"reopened: attempt {x.key}, which resolved it, was rolled back before the case's next try")
                self._reopen(r, len(self._att_order))
                r.last_obs = max(r.last_obs, last)
        except Exception as e:
            self._problem(f"note_rollback({getattr(att, 'id', '?')})", e)

    def note_decision(self, d: TriageDecision) -> None:
        try:
            text = _short(d.rationale or d.kind.value, TEXT_CHARS)
            for k in self._clusters.get(d.cluster_id, []):
                self._recs[k].decision = text
        except Exception as e:
            self._problem("note_decision", e)

    def finalize(self, blocked: list[BlockedItem], *, run_id: str
                 ) -> tuple[list[ErrorCaseRecord], list[FixAttemptRecord]]:
        try:
            return self._finalize(list(blocked or []), run_id)
        except Exception as e:
            self._problem("finalize", e)
            return [], []

    # ------------------------------------------------------------------ internals: bookkeeping
    def _problem(self, where: str, e: Exception) -> None:
        log.warning("error tracker: %s failed: %s: %s", where, type(e).__name__, e, exc_info=e)
        msg = f"{where}: {type(e).__name__}: {e}"
        if len(self.problems) < 10 and msg not in self.problems:
            self.problems.append(msg[:300])

    def _unique_label(self, label: str, target: Optional[str]) -> str:
        lab = label or "build"
        if target:
            lab += f"@{_posix(target).strip('/')}"
        n = self._labels.get(lab, 0) + 1
        self._labels[lab] = n
        return lab if n == 1 else f"{lab}#{n}"

    def _keys_for(self, e: BuildError) -> list[int]:
        """Record keys of a cluster error: the record matched to it in the last build, else the open records of its
        identity — those of exactly its file first (another service's same relative path is not it)."""
        k = self._last_match.get(_match_key(e))
        if k is not None:
            return [k]
        opened = [r for r in self._by_identity.get(identity_of(e), []) if r.status == "open"]
        exact = [r for r in opened if e.file and _posix(r.error.file) == _posix(e.file)]
        return [r.key for r in (exact or opened)]

    def _ensure_att(self, att: FixAttempt) -> _Att:
        x = self._atts.get(id(att))
        if x is not None:
            return x
        n = self._att_ids.get(att.id, 0) + 1
        self._att_ids[att.id] = n
        x = _Att(key=att.id if n == 1 else f"{att.id}#{n}", att=att)
        for e in att.cluster_errors:
            x.target_ids.add(identity_of(e))
            for k in self._keys_for(e):
                if k not in x.targets:
                    x.targets.append(k)
        for k in x.targets:
            self._recs[k].add_attempt(x.key)
        self._atts[id(att)] = x
        self._att_order.append(x)
        return x

    # ------------------------------------------------------------------ internals: observation
    def _observe(self, phase: Phase, label: str, build: BuildResult, scored: list[FixAttempt], void: bool,
                 target: Optional[str]) -> None:
        lab = self._unique_label(label, target)
        void = void or (not build.ok and (build.timed_out or not build.errors))
        atts = [self._ensure_att(a) for a in scored]
        for x in atts:
            x.scored_at, x.scored_obs = lab, len(self._obs)
            if not build.ok and not build.errors:
                x.scoring_note = f"scored at {lab}: the build failed without parseable errors"
            elif build.timed_out:
                x.scoring_note = f"scored at {lab}: the build timed out"
            x.remaining = [fmt_error(e) for e in build.errors if identity_of(e) in x.target_ids][:MAX_LIST]
        obs = _Obs(label=lab, phase=phase, void=void, ok=build.ok, scored=atts,
                   steps=[] if void else list(self._steps))
        self._obs.append(obs)
        if void:
            # a reverted round / a build that told us nothing: only the scored attempts are annotated
            log.debug("error tracker: %s is void (no open / close)", lab)
            return
        idx = len(self._obs) - 1
        self._steps = []
        present = self._match(build, idx, lab, phase, atts, target)
        rank = _goal_rank(build.goal)
        for r in self._recs:
            if r.status != "open" or r.key in present:
                continue
            if not self._in_scope(r, build, target):
                continue
            r.scope_ok = build.ok
            if self._visible(r, build, rank):
                self._close(r, idx, lab, phase)
            else:
                if r.state != "masked":
                    r.state, r.masked_since = "masked", lab
                r.masked_by = [fmt_error(e) for e in build.errors[:MAX_MASKED_BY]]

    def _match(self, build: BuildResult, idx: int, lab: str, phase: Phase, atts: list[_Att],
               target: Optional[str] = None) -> set[int]:
        groups: dict[tuple, list[BuildError]] = {}
        for e in build.errors:
            groups.setdefault(identity_of(e), []).append(e)
        rank = _goal_rank(build.goal)
        new_match: dict[tuple, int] = {}
        present: set[int] = set()
        for ident, errs in groups.items():
            recs = self._by_identity.setdefault(ident, [])
            cands = [r for r in recs if self._matchable(r, build, target)]
            pairs = self._assign(errs, [r for r in cands if r.status == "open"])
            left = [i for i in range(len(errs)) if i not in pairs]
            pairs.update(self._assign([errs[i] for i in left], [r for r in cands if r.status != "open"],
                                      index=left))
            for i, e in enumerate(errs):
                r = pairs.get(i)
                if r is None:
                    r = _Rec(key=len(self._recs), identity=ident, occurrence=len(recs), error=_cap_error(e),
                             first_seen=lab, first_phase=phase, last_seen=lab, last_obs=idx, min_rank=rank,
                             open_from=len(self._att_order))
                    self._recs.append(r)
                    recs.append(r)
                    by = [x.key for x in atts if norm_file(e.file) and norm_file(e.file) in _touched(x.att)
                          and x.att.applied and x.att.outcome not in ("reverted", "apply_failed")]
                    if by:
                        r.note(f"appeared at {lab} after attempt(s) {', '.join(by)} changed its file")
                        r.introduced_by = list(by)
                    else:
                        r.introduced_by = [f"step:{s}" for s in self._obs[idx].steps]
                else:
                    if r.status != "open":
                        r.note(f"reopened at {lab}: was {r.status} at {r.resolved_at}"
                               + (f" by {r.resolved_by}" if r.resolved_by else f" ({r.fix_method})"))
                        self._reopen(r, len(self._att_order))
                    r.error = _cap_error(e)
                    r.last_seen, r.last_obs = lab, idx
                    r.builds_present += 1
                    r.min_rank = min(r.min_rank, rank)
                    r.state, r.masked_since, r.masked_by = "present", "", []
                r.scope_ok = build.ok
                present.add(r.key)
                new_match[_match_key(e)] = r.key
        self._last_match = new_match
        return present

    def _matchable(self, r: _Rec, build: BuildResult, target: Optional[str]) -> bool:
        """May an error of this build be record `r`? Without a target always; with one, only a record of its scope —
        or a record of the root module's `src/` (it may have moved into the target with the restructure)."""
        if not _target_dir(target):
            return True
        if self._in_scope(r, build, target):
            return True
        return _posix(r.error.file).startswith("src/")

    @staticmethod
    def _assign(errs: list[BuildError], recs: list[_Rec], index: Optional[list[int]] = None) -> dict[int, _Rec]:
        """Occurrences of one identity: greedy one-to-one by (same file, same message, nearest line, occurrence) —
        the exact file first: the same relative path in two services is two errors."""
        if not errs or not recs:
            return {}
        idx = index if index is not None else list(range(len(errs)))
        pairs: list[tuple[tuple, int, int]] = []
        for i, e in enumerate(errs):
            for j, r in enumerate(recs):
                line = abs(e.line - r.error.line) if e.line is not None and r.error.line is not None else 10 ** 6
                cost = (0 if _posix(e.file) == _posix(r.error.file) else 1, 0 if e.message == r.error.message else 1,
                        line, r.occurrence)
                pairs.append((cost, i, j))
        pairs.sort()
        out: dict[int, _Rec] = {}
        used: set[int] = set()
        for _cost, i, j in pairs:
            if idx[i] in out or j in used:
                continue
            out[idx[i]] = recs[j]
            used.add(j)
        return out

    @staticmethod
    def _reopen(r: _Rec, open_from: int) -> None:
        r.status, r.state = "open", "present"
        r.resolved_at, r.resolved_phase, r.fix_method, r.resolver_id = "", None, "none", ""
        r.resolved_by, r.resolved_att, r.targeted, r.description = None, None, False, ""
        r.chain, r.open_from = [], open_from

    @staticmethod
    def _in_scope(r: _Rec, build: BuildResult, target: Optional[str]) -> bool:
        t = _target_dir(target)                        # "service-a/pom.xml" → its directory
        if not t:
            return True
        f = _posix(r.error.file)
        if f and (f == t or f.startswith(t + "/")):
            return True
        mods = set(build.modules_built) | {build.failed_module} | {e.module for e in build.errors}
        return bool(r.error.module) and r.error.module in mods

    @staticmethod
    def _visible(r: _Rec, build: BuildResult, rank: int) -> bool:
        kind = r.error.kind
        if rank < KIND_MIN_RANK.get(kind, r.min_rank):
            return False                               # the goal never reached the step that reported it
        no_compile, no_run = _test_skips(build)
        if (kind == ErrorKind.TEST_COMPILE and no_compile) or (kind == ErrorKind.TEST_FAILURE and no_run):
            return False
        if build.ok:
            return True
        m = r.error.module
        if m and m in build.modules_built:
            return True
        failed = {build.failed_module} | {e.module for e in build.errors if e.module}
        if m and build.failed_module and m not in failed:
            return False                               # its module never ran (skipped after another one failed)
        st = STAGE.get(kind)
        if st is None:
            return False
        return any(STAGE.get(e.kind, -1) >= st for e in build.errors if not _synthetic(e))

    def _close(self, r: _Rec, idx: int, lab: str, phase: Phase) -> None:
        span = self._obs[r.last_obs + 1: idx + 1]
        cands = [x for o in span for x in o.scored if x.att.outcome != "reverted" and not _rolled_back(x.att)]
        steps = list(dict.fromkeys(s for o in span for s in o.steps))
        r.resolved_at, r.resolved_phase = lab, phase
        targeted = [x for x in cands if r.key in x.targets or r.identity in x.target_ids]
        f = norm_file(r.error.file)
        touched = [x for x in cands if f and f in _touched(x.att)]
        # the attempt that changed the error's own file is the strongest evidence (2026-10-23 e2e: an attempt aimed at
        # ClientIpFilter edited MemberController, while the attempt aimed at MemberController fixed ClientIpFilter);
        # targeted-only (e.g. a pom edit clearing a compile error) comes after a non-targeted edit of the file
        both = [x for x in targeted if x in touched]
        if both or touched or targeted:
            x = (both or touched or targeted)[0]
            r.status, r.targeted = "resolved", x in targeted
            self._resolved_by_attempt(r, x)
            self._set_chain(r, x, idx)
            if x not in targeted:
                r.note(f"its file was changed by attempt {x.key}, whose cluster did not contain it")
            elif f and x not in touched and touched:
                r.note(f"attempt {x.key} targeted it without changing its file")
        elif cands:
            x = cands[0]
            r.status, r.targeted = "resolved_indirectly", False
            self._resolved_by_attempt(r, x)
            self._set_chain(r, x, idx)
            r.note("gone after attempt(s) that did not target it: " + ", ".join(c.key for c in cands[:MAX_LIST]))
        elif steps:
            r.status, r.targeted = "resolved_indirectly", False
            r.fix_method, r.resolver_id = "plan_step", ",".join(steps)
            r.resolved_by, r.resolved_att = f"step:{steps[0]}", None
            r.description = f"gone after plan step(s) {', '.join(steps)} (no fix attempt targeted it)"
        else:
            r.status, r.targeted = "resolved_indirectly", False
            r.fix_method, r.resolver_id, r.resolved_by, r.resolved_att = "none", "", None, None
            r.description = (f"gone between {r.last_seen} and {lab} without an attempt or plan step aimed at it "
                             "(another change cleared it)")
            r.note(f"gone between {r.last_seen} and {lab}")
        r.state, r.masked_since, r.masked_by = "present", "", []

    @staticmethod
    def _resolved_by_attempt(r: _Rec, x: _Att) -> None:
        r.fix_method, r.resolver_id = _method(x.att), _resolver(x.att)
        r.resolved_by, r.resolved_att = x.key, x.key

    def _set_chain(self, r: _Rec, x: _Att, idx: int) -> None:
        """The resolution of `r` = the resolver `x` plus every earlier-or-same-span attempt since `r` (re)appeared whose
        change is still in the tree (applied, not reverted) and that aimed at `r` or changed its file: a `no_change`
        attempt that added the import the resolver's edit needs is part of the fix (2026-10-25)."""
        f = norm_file(r.error.file)
        chain: list[str] = []
        for y in self._att_order[r.open_from:]:
            if y is not x:
                a = y.att
                if y.scored_obs < 0 or y.scored_obs > idx or not a.applied or a.outcome in ("reverted", "apply_failed") \
                        or _rolled_back(a):
                    continue
                if not (r.key in y.targets or r.identity in y.target_ids or (f and f in _touched(a))):
                    continue
            chain.append(y.key)
        if x.key not in chain:                         # x predates the (re)appearance (defensive): it still resolved it
            chain.append(x.key)
        r.chain = chain
        if len(chain) > 1:
            r.note(f"resolved by {len(chain)} attempts whose changes stayed in the tree: {', '.join(chain)}")

    # ------------------------------------------------------------------ internals: finalize
    def _finalize(self, blocked: list[BlockedItem], run_id: str
                  ) -> tuple[list[ErrorCaseRecord], list[FixAttemptRecord]]:
        rid = {r.key: hashlib.sha1("|".join([run_id, *r.identity, str(r.occurrence)]).encode("utf-8"))
               .hexdigest()[:16] for r in self._recs}
        aid = {x.key: f"{run_id}:{x.key}" for x in self._att_order}
        att_records: dict[str, FixAttemptRecord] = {}
        for x in self._att_order:
            try:
                att_records[x.key] = self._attempt_record(x, run_id, [rid[k] for k in x.targets if k in rid])
            except Exception as e:  # one broken attempt must not lose the others
                self._problem(f"attempt record {x.key}", e)
        last_ok = next((o.ok for o in reversed(self._obs) if not o.void), None)
        errors: list[ErrorCaseRecord] = []
        for r in self._recs:
            try:
                errors.append(self._error_record(r, run_id, rid[r.key], aid, att_records, blocked, last_ok))
            except Exception as e:
                self._problem(f"error record {r.key}", e)
        return errors, list(att_records.values())

    def _attempt_record(self, x: _Att, run_id: str, error_ids: list[str]) -> FixAttemptRecord:
        att = x.att
        diff, truncated = self._diff(att)
        notes = [_short(n, 300) for n in att.notes[:MAX_LIST]]
        if x.scored_at:
            notes.append(f"scored at {x.scored_at}")
        if x.scoring_note:
            notes.append(x.scoring_note)
        u = att.llm_usage
        rec = FixAttemptRecord(
            id=f"{run_id}:{x.key}", at=att.created_at, run_id=run_id, phase=att.phase, iteration=att.iteration,
            attempt_id=att.id, cluster_id=att.cluster_id, error_ids=error_ids, fix_method=_method(att),
            resolver_id=_resolver(att), model=att.model or "", outcome=att.outcome, applied=att.applied,
            description=self._describe(att), failure=self._failure(att),
            introduced_errors=[fmt_error(e) for e in att.introduced_errors[:MAX_LIST]],
            remaining_errors=list(x.remaining), diff=diff, diff_truncated=truncated,
            llm_calls=u.calls,
            input_tokens=u.input_tokens, output_tokens=u.output_tokens, advisory_ids=list(att.advisory_ids),
            notes=notes)
        if att.decision.kind == DecisionKind.SCRIPT:          # v2: which version of which case ran, and how it went
            res = att.script_result
            rec.script_mode, rec.script_case_id = att.script_mode, att.script_case_id
            rec.script_version, rec.script_parent = int(att.script_version or 0), att.script_parent
            if res is not None:
                rec.script_status, rec.script_error = res.status, _short(res.error or "", TEXT_CHARS)
                rec.script_log = [_short(str(line), 300) for line in res.log[:SCRIPT_LOG_LINES]]
        return rec

    @staticmethod
    def _describe(att: FixAttempt) -> str:
        d = att.decision
        if d.kind == DecisionKind.SCRIPT:             # the analysis (a reuse: the stored version's)
            analysis = (att.analysis or "").strip()
            if len(analysis) > ANALYSIS_CHARS:
                analysis = analysis[:ANALYSIS_CHARS - 1] + "…"
            return analysis or f"fix script {att.script_case_id or '?'}:v{att.script_version} ({att.script_mode})"
        return _short(att.analysis or d.rationale or d.kind.value)

    @staticmethod
    def _failure(att: FixAttempt) -> str:
        o = att.outcome
        if o == "apply_failed":
            return _short("; ".join(att.apply_errors) or "nothing applied")
        if o in ("regressed", "reverted"):
            why = next((n for n in att.notes if n.startswith("regressed:")), f"{o} after the rebuild")
            intro = [fmt_error(e) for e in att.introduced_errors[:5]]
            text = why + (" — introduced: " + "; ".join(intro) if intro else "")
            if o == "reverted":
                text += " (reverted)"
            return _short(text)
        if o == "no_change":
            if not att.applied:
                return _short("nothing applied: " + (att.notes[-1] if att.notes else "no change"))
            return "the targeted error(s) were still there after the rebuild"
        if o == "pending":
            return "not scored (the run ended before the rebuild)"
        if o == "blocked":
            return "blocked"
        if o in ("partial", "inconclusive"):           # round 10: progress / no verdict — the loop's own note says why
            why = next((n for n in att.notes if n.startswith(o + ":")), "")
            return _short(why or ("some targeted error(s) remain after the rebuild" if o == "partial"
                                  else "the rebuild could not show the targeted error(s)"))
        return ""

    def _diff(self, att: FixAttempt) -> tuple[str, bool]:
        text = ""
        if att.applied and att.commit and att.base_commit and att.commit != att.base_commit:
            files = sorted({_posix(p) for ch in att.changes for p in (ch.path, ch.new_path) if p})
            git = getattr(self.ws, "_git", None)
            if callable(git):
                args = ["diff", "--no-color", att.base_commit, att.commit]
                text = git(*args, *(["--", *files] if files else []), check=False).stdout or ""
                if files and not text.strip():   # the paths it reported are not the ones it changed
                    text = git(*args, check=False).stdout or ""
            else:  # pragma: no cover - a workspace without git access
                text = self.ws.diff(att.base_commit, att.commit)
        if not text.strip():                     # no git diff: what the attempt reports it changed
            changes = att.changes
            if changes and any(ch.search is not None or ch.content is not None or ch.op != "modify"
                               for ch in changes):
                from sbmig.kb.common import changes_as_diff
                text = changes_as_diff(list(changes))
        # the FULL diff (round 10): prepare_error_cases classifies it, then applies kb.error_diff_chars — a cap here
        # cut later files' code edits off (misread as text replaces) and emptied errors' own excerpts
        if len(text) > MAX_DIFF_CHARS:
            return text[:MAX_DIFF_CHARS - len(TRUNCATED_MARK)] + TRUNCATED_MARK, True
        return text, False

    def _error_record(self, r: _Rec, run_id: str, rec_id: str, aid: dict[str, str],
                      atts: dict[str, FixAttemptRecord], blocked: list[BlockedItem],
                      last_ok: Optional[bool]) -> ErrorCaseRecord:
        e = r.error
        refs = [ErrorAttemptRef(attempt_id=aid[k], phase=atts[k].phase, iteration=atts[k].iteration,
                                fix_method=atts[k].fix_method, resolver_id=atts[k].resolver_id,
                                outcome=atts[k].outcome, failure=atts[k].failure,
                                script_version=atts[k].script_version)
                for k in r.attempt_keys if k in atts]
        notes = list(r.notes)
        if r.omitted_attempts:
            notes.append(f"{r.omitted_attempts} earlier attempt(s) omitted from the history")
        introduced = ",".join(aid.get(k, k) if not k.startswith("step:") else k for k in r.introduced_by)
        rec = ErrorCaseRecord(
            id=rec_id, run_id=run_id, error_kind=e.kind, message=e.message, file=e.file, line=e.line,
            symbol=e.symbol, location=e.location, module=e.module,
            artifact=e.artifact.coordinates if e.artifact is not None else "", signature=e.signature,
            signature_generic=e.signature_generic, raw=e.raw or "", first_seen=r.first_seen,
            first_phase=r.first_phase, last_seen=r.last_seen, builds_present=r.builds_present, attempts=refs,
            attempt_count=len(refs) + r.omitted_attempts,
            llm_attempts=sum(1 for a in refs if a.fix_method in LLM_METHODS), notes=notes, introduced_by=introduced)
        if r.status != "open":
            rec.status = r.status                                      # type: ignore[assignment]
            rec.resolved_at, rec.resolved_phase = r.resolved_at, r.resolved_phase
            rec.fix_method, rec.resolver_id, rec.targeted = r.fix_method, r.resolver_id, r.targeted  # type: ignore
            if r.resolved_att and r.resolved_att in atts:
                a = atts[r.resolved_att]
                rec.resolved_by = aid[r.resolved_att]
                if a.script_case_id:                       # v2: the case script that resolved it
                    rec.script_case_id, rec.script_version = a.script_case_id, a.script_version
                rec.resolution_attempts = [aid[k] for k in r.chain if k in aid and k in atts] or [aid[r.resolved_att]]
                how = a.description or f"{a.fix_method} {a.resolver_id}".strip()
                rec.description = how if r.targeted else (
                    f"its file was changed by attempt {a.attempt_id}: {how}" if r.status == "resolved"
                    else f"gone after attempt {a.attempt_id} (not aimed at it): {how}")
            else:
                rec.resolved_by, rec.description = r.resolved_by, r.description
            return rec
        last = atts.get(r.attempt_keys[-1]) if r.attempt_keys else None
        tried = ", ".join(f"{a.fix_method} {a.outcome}" for a in refs[-5:])
        rec.description = f"tried {len(refs)} attempt(s): {tried}" if refs else "no fix attempt"
        item = self._covering(r, blocked)
        if item is not None:
            rec.block_reason, rec.suggestion = item.reason.value, item.suggestion or ""
        if r.state == "masked" and last_ok is not None and not r.scope_ok:
            rec.status = "not_visible"
            rec.failure_reason = (f"status unknown: not seen since {r.last_seen} — the build(s) from {r.masked_since} "
                                  "on stopped before this error's stage")
            if last is not None:
                rec.failure_reason += f"; last attempt: {self._attempt_text(last)}"
            rec.problem_errors = list(r.masked_by)
            rec.notes.append(f"not seen since {r.last_seen} (masked from {r.masked_since} on)")
            return rec
        if item is not None:
            rec.status = "blocked"
            evidence = item.suggestion or next((n for n in item.notes if n), "") or item.reason.value
            rec.failure_reason = _short(f"blocked ({item.reason.value}): {evidence}")
        else:
            rec.status = "unresolved"
            if r.state == "masked":            # an ok build that did not compile / run tests: still there
                rec.notes.append(f"not seen since {r.last_seen}: the later build(s) skipped this stage "
                                 "(tests not compiled / run)")
            if last is not None:
                rec.failure_reason = self._attempt_text(last)
            elif r.decision:
                rec.failure_reason = _short(f"no fix attempt: {r.decision}")
            else:
                rec.failure_reason = "no fix attempt before the loop ended"
        rec.problem_errors = self._problem_errors(r, refs, atts)
        return rec

    @staticmethod
    def _attempt_text(a: FixAttemptRecord) -> str:
        return _short(f"{a.fix_method} attempt {a.attempt_id} {a.outcome}" + (f": {a.failure}" if a.failure else ""))

    @staticmethod
    def _problem_errors(r: _Rec, refs: list[ErrorAttemptRef], atts: dict[str, FixAttemptRecord]) -> list[str]:
        self_err = fmt_error(r.error)
        for k in reversed(r.attempt_keys):
            a = atts.get(k)
            if a is None or a.outcome in ("improved", "build_ok", "pending"):
                continue
            if a.outcome in ("regressed", "reverted") and a.introduced_errors:
                return list(a.introduced_errors[:MAX_LIST])
            if a.outcome in ("no_change", "regressed", "reverted", "partial", "inconclusive"):
                return [self_err]
            if a.outcome == "apply_failed":
                return [_short(x, 300) for x in (a.failure.split("; ") if a.failure else [])][:MAX_LIST] or [self_err]
        return []

    @staticmethod
    def _covering(r: _Rec, blocked: list[BlockedItem]) -> Optional[BlockedItem]:
        """The BlockedItem covering this error: same identity as one of its errors, or its signature with (for
        file-keyed kinds) its file among the item's files — the BlockedRegistry key semantics."""
        e = r.error
        file_kind = e.kind in (ErrorKind.COMPILE, ErrorKind.TEST_COMPILE) and bool(e.file)
        sig = e.signature or f"{e.kind.value}|{e.message}"
        for it in blocked:
            if any(identity_of(x) == r.identity for x in it.errors):
                return it
            sigs = set(it.signatures) | {x.signature for x in it.errors if x.signature}
            if sig not in sigs and e.signature not in sigs:
                continue
            if not file_kind:
                return it
            files = {norm_file(f.rsplit(":", 1)[0] if f.rsplit(":", 1)[-1].isdigit() else f) for f in it.files}
            files |= {norm_file(x.file) for x in it.errors if x.file}
            if norm_file(e.file) in files:
                return it
        return None
