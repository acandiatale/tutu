"""v2.1 (2026-10-27, docs/v2/PLAN-v2.1.md §3.3): the ONE place the loop and the orchestrator talk to the run's error
list (`sbmig.errors.entries.ErrorList`, workstream E1) and to the KB's case store (`kb.cases`, E1).

Every call goes through these adapters, so a rename on the other side costs one line here, and a problem there never
changes what the loop does: an exception is logged (WARNING) and the call returns its "nothing" value.

`RunErrors` wraps the run's error list (None = off: dry run, or `sbmig.errors` not importable):
    add_build(build, seen, round=, void=) → new entries (count) every build of the run (compile, build, fix rN)
    add_pom_verify(file, note)       → the entry                one per pom correction of the restructure
    add_note(file, text, seen)       → the entry                review warnings
    review_fixed(file, text, try)    → the entry                a critical review issue the review's edits fixed
                                                                (try fields incl. `edits`, the 수정 notes)
    verify_poms(build_errors)        → pom-verify entries resolved by that build
    begin_try / finish_try / resolve / block                    the fix loop's tries
    end_of_run / write / to_report / entries / open_count       finalize

`CaseScripts` wraps `kb.cases` (None = no KB): best / code / find / add_script / credit.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

log = logging.getLogger("sbmig.loop")


def _count(x: Any) -> int:
    """A count from whatever the other side returned: an int, a list of entries, None."""
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, int):
        return x
    if isinstance(x, (list, tuple, set, dict)):
        return len(x)
    return 0


class _Guarded:
    _target: Any = None
    _what = "?"

    @property
    def active(self) -> bool:
        return self._target is not None

    def _call(self, name: str, *args: Any, optional: Optional[dict[str, Any]] = None, default: Any = None,
              **kwargs: Any) -> Any:
        """`target.name(*args, **kwargs, **optional)`; `optional` kwargs are dropped once when the callee does not
        take them (TypeError naming the keyword). Never raises."""
        t = self._target
        fn = getattr(t, name, None) if t is not None else None
        if not callable(fn):
            return default
        opt = dict(optional or {})
        try:
            while True:
                try:
                    return fn(*args, **kwargs, **opt)
                except TypeError as e:          # drop only the optional keyword(s) the callee does not take
                    named = [k for k in opt if f"'{k}'" in str(e)]
                    if not named:
                        raise
                    for k in named:
                        opt.pop(k)
        except Exception as e:  # noqa: BLE001 - the error list / KB never stops a run
            log.warning("%s.%s failed: %s: %s", self._what, name, type(e).__name__, e)
            return default


REVIEW_L2 = "review|structure"      # the case of the structure review's own fixes (entries + stored scripts)


class RunErrors(_Guarded):
    """The run's error list (E1 `sbmig.errors.entries.ErrorList`); every method is a no-op without one."""
    _what = "errors"

    def __init__(self, target: Any = None):
        self._target = target
        self.pom_verify_added = 0

    @classmethod
    def create(cls, ws: Any, cfg: Any, project: str, factory: Optional[Callable[..., Any]] = None) -> "RunErrors":
        """`factory(ws, cfg, project)` (tests) or `ErrorList.for_workspace(ws, cfg, project)`; a missing / broken
        package gives an inactive list (a WARNING line: the run goes on without errors.jsonl)."""
        try:
            if factory is None:
                from sbmig.errors.entries import ErrorList
                return cls(ErrorList.for_workspace(ws, cfg, project=project))
            return cls(factory(ws, cfg, project))
        except Exception as e:  # noqa: BLE001
            log.warning("error list unavailable (%s: %s) — no errors.jsonl in this run", type(e).__name__, e)
            return cls(None)

    # ---------------------------------------------------------------- builds
    def add_build(self, build: Any, seen: str, *, round: Optional[int] = None, void: bool = False,
                  scope: Optional[str] = None) -> int:
        """One build (the BuildResult: ok / timed out / modules built) → the list adds its NEW errors and resolves
        the open entries it no longer shows (except this round's pending tries: the loop's `resolve` decides those).
        `round` = the fix round this build scores; `void` = the build says nothing. Returns the new entries' count.
        Call it BEFORE `resolve` for the tries it scores."""
        opt: dict[str, Any] = {"void": void}
        if round is not None:
            opt["round"] = round
        if scope:
            opt["scope"] = scope
        return _count(self._call("add_build", build, seen, optional=opt))

    def verify_poms(self, build: Any) -> int:
        """The pom-verify entries this build settles (resolved "build"); returns how many."""
        return _count(self._call("verify_poms", build))

    def add_pom_verify(self, file: str, note: str) -> Any:
        e = self._call("add_pom_verify", file, note)
        if self.active:
            self.pom_verify_added += 1
        return e

    def size(self) -> int:
        """How many entries the list holds (the `#n` of a log line); 0 without a list."""
        return len(self.entries()) if self.active else 0

    def add_note(self, file: str, text: str, seen: str = "review") -> Any:
        return self._call("add_note", file, text, seen)

    def review_fixed(self, file: str, text: str, **try_fields: Any) -> Any:
        """A critical review issue the review's own fix addressed: an entry (seen `review`, case `REVIEW_L2`) with
        one try (source `review`, round 0) resolved `review`."""
        if self._target is None:
            return None
        entry = self._call("add_note", file, text, "review", status="open", l2=REVIEW_L2)
        eid, case_id = str(getattr(entry, "id", "") or ""), str(getattr(entry, "case_id", "") or "")
        if entry is None or not eid:
            return entry
        tr = self._call("begin_try", case_id, 0, "review", entry_ids=[eid])
        n = int(getattr(tr, "n", 0) or 0)
        if n:
            self.finish_try(case_id, n, **try_fields)
            self._call("resolve", case_id, 0, n, entry_ids=[eid])
        return entry

    # ---------------------------------------------------------------- tries
    def begin_try(self, case_id: str, round: int, source: str, script_version: int = 0) -> Any:
        """A new FixTry (its `n`) for the case's entries; None = no list."""
        return self._call("begin_try", case_id, round, source, script_version=script_version)

    def finish_try(self, case_id: str, n: int, *, optional: Optional[dict[str, Any]] = None, **fields: Any) -> None:
        """Set the try's fields (analysis, solution, no_script_reason, applied, files, edits, diff, commit, result,
        note, llm_calls, seconds); called after the apply, again after the scoring / a rollback. `optional` fields
        (script_version, edits — the v2.1b 수정 notes) are dropped when the list does not take them."""
        opt = dict(optional or {})
        if "edits" in fields:
            opt["edits"] = fields.pop("edits")
        self._call("finish_try", case_id, n, optional=opt, **fields)

    def resolve(self, case_id: str, round: int, n: int) -> None:
        self._call("resolve", case_id, round, n)

    def block(self, case_id: str, reason: str) -> None:
        self._call("block", case_id, reason)

    # ---------------------------------------------------------------- views / finalize
    def entries(self) -> list:
        t = self._target
        if t is None:
            return []
        got = getattr(t, "entries", None)
        try:
            got = got() if callable(got) else got
        except Exception as e:  # noqa: BLE001
            log.warning("errors.entries failed: %s: %s", type(e).__name__, e)
            got = None
        if got is None:
            got = self.to_report()
        return list(got or [])

    def counts(self) -> dict[str, int]:
        """status → entries (open / pom-verify / resolved / blocked / unresolved / note)."""
        got = self._call("counts", default=None)
        if isinstance(got, dict):
            return {str(k): int(v) for k, v in got.items()}
        out: dict[str, int] = {}
        for e in self.entries():
            st = str(getattr(e, "status", "") or "")
            out[st] = out.get(st, 0) + 1
        return out

    def open_count(self) -> Optional[int]:
        """Entries still open (None = no list)."""
        return self.counts().get("open", 0) if self.active else None

    def open_cases(self) -> dict:
        got = self._call("open_cases", default={})
        return dict(got) if isinstance(got, dict) else {}

    def entries_for(self, case_id: str) -> list:
        return list(self._call("entries_for", case_id, default=[]) or [])

    def end_of_run(self) -> dict[str, int]:
        """Open entries → unresolved (or resolved by a verdict that never came); returns the counts by status."""
        got = self._call("end_of_run")
        return dict(got) if isinstance(got, dict) else self.counts()

    def write(self) -> dict[str, str]:
        """Rewrite <run dir>/errors.jsonl (+ errors.md); returns the paths written ({'errors': …, 'errors_md': …})."""
        self._call("write")
        out: dict[str, str] = {}
        for key, attr in (("errors", "path"), ("errors_md", "md_path")):
            try:
                p = getattr(self._target, attr, None) if self._target is not None else None
                if p is not None and hasattr(p, "exists") and p.exists():
                    out[key] = str(p)
            except Exception:  # noqa: BLE001
                continue
        return out

    @property
    def problems(self) -> list[str]:
        return list(getattr(self._target, "problems", None) or [])

    def to_report(self) -> list:
        return list(self._call("to_report", default=[]) or [])


class CaseScripts(_Guarded):
    """The KB's case store (`kb.cases`, E1 `CaseStore`); None = no KB (nothing reused, LLM scripts run unstored)."""
    _what = "kb.cases"

    def __init__(self, target: Any = None):
        self._target = target if target is not None and callable(getattr(target, "best", None)) else None

    @classmethod
    def of(cls, kb: Any) -> "CaseScripts":
        try:
            return cls(getattr(kb, "cases", None))
        except Exception as e:  # noqa: BLE001 - a broken KB never stops the loop
            log.warning("kb.cases unavailable: %s: %s", type(e).__name__, e)
            return cls(None)

    def best(self, case_id: str) -> Optional[tuple[Any, str]]:
        """(CaseScript, code) of the version to reuse (proven newest → unverified newest), None = nothing stored."""
        got = self._call("best", case_id)
        if not got:
            return None
        try:
            meta, code = got
        except (TypeError, ValueError):
            return None
        return (meta, code or "") if meta is not None else None

    def code(self, case_id: str, version: int) -> str:
        return str(self._call("code", case_id, version, default="") or "")

    def find(self, case_id: str) -> Any:
        """The case (CaseInfo) or None."""
        return self._call("find", case_id)

    def describe(self, case_id: str) -> str:
        """The KB lookup result as one log phrase (`저장된 스크립트 없음 (처음 보는 케이스)`, `저장된 스크립트 v2 (proven,
        성공 3 / 실패 0, 프로젝트 2)`); "" when the store cannot say."""
        return str(self._call("describe", case_id, default="") or "")

    def add_script(self, case_id: str, l2: str, code: str, *, source: str, run_id: str, project: str,
                   analysis: str = "", solution: str = "") -> Any:
        """Store a script → its CaseScript (version, file, status); None = not stored."""
        return self._call("add_script", case_id, l2, code, source=source, run_id=run_id, project=project,
                          analysis=analysis, solution=solution)

    def credit(self, case_id: str, version: int, result: str, *, project: str, run_id: str) -> bool:
        if not self.active:
            return False
        self._call("credit", case_id, version, result, project=project, run_id=run_id)
        return True


def script_path(case_id: str, cs: Any) -> str:
    """'cases/<case_id>/fix_v2.py' — where a stored script lives in the KB (log lines, entries)."""
    f = str(getattr(cs, "file", "") or f"fix_v{int(getattr(cs, 'version', 0) or 0)}.py")
    return f if f.startswith("cases/") else f"cases/{case_id}/{f}"
