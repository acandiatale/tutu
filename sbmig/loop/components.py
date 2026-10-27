"""Lazy resolution of collaborators from other workstreams + null/lazy stand-ins."""
from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Optional

from sbmig.core.models import KBMatch, ScriptRunResult

log = logging.getLogger("sbmig.loop")


class MissingComponentError(RuntimeError):
    """A collaborator package (another workstream) is missing or broken."""


def lazy_attr(module: str, attr: str, workstream: str) -> Any:
    try:
        mod = importlib.import_module(module)
    except Exception as e:
        raise MissingComponentError(
            f"{module}.{attr} (workstream {workstream}) cannot be imported: {type(e).__name__}: {e}") from e
    try:
        return getattr(mod, attr)
    except AttributeError as e:
        raise MissingComponentError(
            f"{module} (workstream {workstream}) does not expose '{attr}' yet — see docs/INTERFACES.md") from e


class ScriptFixerMissing(RuntimeError):
    """v2: the fixer (a tier-2 `fix.plugin` written for v1) has no `propose_script` — it cannot write fix scripts."""


class NullKnowledgeBase:
    """The knowledge base that knows nothing (``--no-kb``, or sbmig.kb unavailable) — what the v2.1 flow calls on a KB:
    `match` (an empty KBMatch: no pinned case actions, no hints), `cases` None (nothing is reused, LLM scripts run
    unstored as v0) and `record_run` (nothing recorded: the run's errors.jsonl stays in the run directory)."""

    cases = None

    def match(self, cluster, ws, profile) -> KBMatch:
        return KBMatch()

    def record_run(self, entries) -> int:
        return 0


class LazyFixer:
    """Fixer proxy that creates the real fixer (and its LLM client) only when the first fix script is asked for."""

    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._fixer: Any = None

    def _get(self) -> Any:
        if self._fixer is None:
            self._fixer = self._factory()
        return self._fixer

    def propose_script(self, ws, cluster, ctx):
        """A fixer without `propose_script` (a tier-2 plugin written for v1) raises ScriptFixerMissing: v2 fixes every
        error with a fix script (docs/INTERFACES.md "Seams")."""
        fx = self._get()
        fn = getattr(fx, "propose_script", None)
        if not callable(fn):
            raise ScriptFixerMissing(f"the fixer {type(fx).__name__} has no propose_script — v2 fixes every error with "
                                     "a fix script; a fix.plugin must implement propose_script(ws, cluster, ctx)")
        return fn(ws, cluster, ctx)


def basic_profile_summary(profile) -> str:
    if profile is None:
        return ""
    libs = ", ".join(sorted({lib.name for lib in profile.libraries})[:20])
    return (f"project {profile.name}: case {profile.case_type.value}, java {profile.java_version or '?'}, "
            f"spring boot {profile.spring_boot_version or '-'}, spring {profile.spring_version or '-'}, "
            f"{len(profile.poms)} pom(s){', multi-module' if profile.is_multi_module else ''}; libs: {libs or '-'}")


def profile_summary(profile) -> str:
    try:
        fn = importlib.import_module("sbmig.fix").profile_summary  # type: ignore[attr-defined]
        return fn(profile)
    except Exception:
        return basic_profile_summary(profile)


class LazyScriptRunner:
    """ScriptRunnerLike proxy (v2): the real `sbmig.script.ScriptRunner(cfg)` is built on first use; when it cannot
    be built, every run is an `error` result naming why (never raises — the loop records a failed attempt)."""

    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._runner: Any = None
        self._error: Optional[str] = None

    def _get(self) -> Any:
        if self._runner is None and self._error is None:
            try:
                self._runner = self._factory()
            except Exception as e:  # noqa: BLE001 - reported on every run instead
                self._error = f"script runner unavailable: {type(e).__name__}: {e}"
                log.error("%s", self._error)
        return self._runner

    def check(self, code: str) -> list[str]:
        r = self._get()
        return [self._error or "script runner unavailable"] if r is None else list(r.check(code))

    def run(self, ws, code: str, *, errors=(), timeout_s: Optional[int] = None) -> ScriptRunResult:
        r = self._get()
        if r is None:
            return ScriptRunResult(status="error", error=self._error or "script runner unavailable")
        return r.run(ws, code, errors=errors, timeout_s=timeout_s)
