"""v2 fix-script ladder (docs/INTERFACES.md "Script fix loop (L)"): which script a case gets next in this run.

Per case and run, in this order:

1. **reuse** — `script.reuse` and the store's proven versions (`best(case_id, limit=script.reuse_versions)`) that
   did not run in this run yet: the stored code runs again, no LLM call;
2. **augment** — the base is this run's last script attempt of the case that had code (what it did here: outcome,
   run status, log, changed files, remaining / introduced errors), else the store's newest version
   (`latest(case_id)`); the LLM writes the COMPLETE next version; at most `script.max_augments_per_case`;
3. **new** — nothing to extend (nothing stored, nothing with code tried): the LLM writes the first version;
4. nothing left → the case is BLOCKED (`repeated_failure`) by the decision engine.

LLM requests per case and run: at most `max_augments_per_case` augments, plus the first `new` when the case had no
stored script (a `new` answer without a script does not open an augment — the next request is `new` again, inside
the same total). `loop.max_llm_attempts_per_cluster` does not apply to scripts.

Pure bookkeeping over the case's earlier attempts in this run (`FixAttempt.script_*`) and the script store
(`ScriptStoreLike`, None = no KB): the decision engine calls it to decide SCRIPT vs BLOCKED, the fix loop executes
the step it returns. A store problem is logged and treated as "nothing stored" — it never stops the loop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from sbmig.core.models import DecisionKind, ErrorCluster, FixAttempt, ScriptMeta

log = logging.getLogger("sbmig.triage")

LLM_MODES = ("new", "augment")              # script modes that ask the LLM (reuse runs a stored version)


@dataclass
class ScriptStep:
    """The next script step of a case. `mode` None = the per-case limits are reached (→ BLOCKED)."""
    case_id: str
    mode: Optional[str] = None                      # reuse | augment | new | None
    version: int = 0                                # reuse: the stored version to run
    code: str = ""                                  # reuse: its code; augment from the store: the base code
    meta: Optional[ScriptMeta] = None               # reuse: its meta; augment from the store: the base meta
    base_attempt: Optional[FixAttempt] = None       # augment: this run's attempt it extends
    llm_used: int = 0                               # LLM requests (new + augment) already made for the case
    llm_limit: int = 0                              # the case's LLM request limit in this run
    augments_used: int = 0
    why: str = ""                                   # rationale (decision text)

    @property
    def needs_llm(self) -> bool:
        return self.mode in LLM_MODES

    @property
    def base_version(self) -> int:
        """The version an augment extends (0 = an unstored script of this run)."""
        if self.base_attempt is not None:
            return int(self.base_attempt.script_version or 0)
        return int(self.meta.version) if self.meta is not None else 0


def script_attempts(attempts: Iterable[FixAttempt]) -> list[FixAttempt]:
    return [a for a in attempts if a.decision.kind == DecisionKind.SCRIPT]


def cluster_case_id(cluster: ErrorCluster) -> str:
    if cluster.case_id:
        return cluster.case_id
    from .cluster import case_id_of
    return case_id_of(cluster.errors[0]) if cluster.errors else ""


def _store_call(store: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    fn = getattr(store, name, None) if store is not None else None
    if not callable(fn):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - the KB never stops the loop
        log.warning("script store %s(%s) failed: %s: %s", name, ", ".join(map(str, args)), type(e).__name__, e)
        return None


def plan_script_step(cluster: ErrorCluster, previous: Iterable[FixAttempt], store: Any, cfg: Any) -> ScriptStep:
    """The case's next step (see the module doc). `previous` = this run's scored attempts on the same case (the loop
    passes `_previous(cluster)`: no pending ones, no cooled-down transport failures)."""
    sc = getattr(cfg, "script", None)
    reuse_on = bool(getattr(sc, "reuse", True))
    reuse_n = max(1, int(getattr(sc, "reuse_versions", 1) or 1))
    max_aug = max(0, int(getattr(sc, "max_augments_per_case", 3)))
    case_id = cluster_case_id(cluster)
    tried = [a for a in script_attempts(previous) if a.outcome != "pending"]     # pending proves nothing yet
    tried_versions = {int(a.script_version) for a in tried if a.script_version}
    new_used = sum(1 for a in tried if a.script_mode == "new")
    aug_used = sum(1 for a in tried if a.script_mode == "augment")
    llm_used = new_used + aug_used
    step = ScriptStep(case_id=case_id, llm_used=llm_used, augments_used=aug_used)

    # 1. reuse: proven versions not run in this run yet (no LLM call)
    if reuse_on and case_id:
        for item in _store_call(store, "best", case_id, limit=reuse_n) or []:
            try:
                meta, code = item
            except (TypeError, ValueError):
                continue
            if int(meta.version) in tried_versions or not (code or "").strip():
                continue
            step.mode, step.version, step.code, step.meta = "reuse", int(meta.version), code, meta
            step.why = (f"reuse: stored script v{meta.version} of case {case_id} ({meta.successes} success(es) in "
                        f"{len(meta.success_projects)} project(s), {meta.failures} failure(s)) — no LLM call")
            return step

    # 2. augment: this run's last attempt that had code, else the store's newest version
    base_att = next((a for a in reversed(tried) if (a.script_code or "").strip()), None)
    stored = None
    if base_att is None and case_id:
        stored = _store_call(store, "latest", case_id)
        if stored is not None:
            try:
                meta, code = stored
                stored = (meta, code) if (code or "").strip() else None
            except (TypeError, ValueError):
                stored = None
    if base_att is not None or stored is not None:
        step.llm_limit = max_aug + (1 if new_used else 0)
        if aug_used >= max_aug or llm_used >= step.llm_limit:
            step.why = (f"{aug_used} augment(s) of case {case_id} did not resolve it (script.max_augments_per_case "
                        f"{max_aug}, {llm_used} LLM request(s) in this run)")
            return step
        step.mode = "augment"
        if base_att is not None:
            step.base_attempt = base_att
            what = (f"this run's v{base_att.script_version} ({base_att.script_mode}, {base_att.outcome})"
                    if base_att.script_version else f"this run's unstored script ({base_att.outcome})")
        else:
            step.meta, step.code = stored
            what = f"stored v{step.meta.version} (newest; {step.meta.successes} success(es), " \
                   f"{step.meta.failures} failure(s))"
        step.why = f"augment {aug_used + 1}/{max_aug} of case {case_id}: extend {what}"
        return step

    # 3. new: nothing to extend
    step.llm_limit = max_aug + 1
    if llm_used >= step.llm_limit:
        step.why = (f"{llm_used} LLM request(s) for case {case_id} gave no usable script (limit {step.llm_limit})")
        return step
    step.mode = "new"
    step.why = (f"new script for case {case_id} (nothing stored)" if not llm_used else
                f"new script for case {case_id} (request {llm_used + 1}/{step.llm_limit}: the earlier answer(s) had "
                "no script)")
    return step
