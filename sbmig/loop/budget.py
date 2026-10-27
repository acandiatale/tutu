"""Run-wide budgets shared by every build-fix loop of a run.

v2.1 (2026-10-27): `loop.max_llm_calls: 0` = no limit on LLM calls (the default); > 0 = a hard cap per project;
-1 = no LLM call at all (the fleet passes it when `fleet.max_llm_calls_total` is used up — stored scripts are still
reused, every step that needs the LLM is blocked `budget_exhausted` "LLM budget exhausted (fleet)")."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class LoopBudget:
    max_llm_calls: int = 0                  # 0 = unlimited, > 0 = cap, -1 = no LLM call (fleet budget used up)
    max_minutes: float = 180.0
    clock: Callable[[], float] = time.monotonic
    llm_calls: int = 0
    started_at: Optional[float] = None
    exhausted_reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.started_at is None:
            self.started_at = self.clock()

    @classmethod
    def from_config(cls, cfg, clock: Callable[[], float] = time.monotonic) -> "LoopBudget":
        return cls(max_llm_calls=int(cfg.loop.max_llm_calls or 0), max_minutes=float(cfg.loop.max_total_minutes),
                   clock=clock)

    # LLM ---------------------------------------------------------------
    @property
    def llm_limited(self) -> bool:
        """True when a hard cap is configured (`loop.max_llm_calls` > 0)."""
        return self.max_llm_calls > 0

    @property
    def llm_off(self) -> bool:
        """`loop.max_llm_calls` = -1: no LLM call in this project (the fleet's total is used up)."""
        return self.max_llm_calls < 0

    def llm_allowed(self) -> bool:
        if self.llm_off:
            return False
        return not self.llm_limited or self.llm_calls < self.max_llm_calls

    def llm_exhausted(self) -> bool:
        return self.llm_off or (self.llm_limited and self.llm_calls >= self.max_llm_calls)

    def llm_text(self) -> str:
        """'12/100 calls', '12 calls (no limit)' or 'fleet' (no LLM call at all) — the text of
        "LLM budget exhausted (…)"."""
        if self.llm_off:
            return "fleet"
        return (f"{self.llm_calls}/{self.max_llm_calls} calls" if self.llm_limited
                else f"{self.llm_calls} calls (no limit)")

    def charge_llm(self, calls: int = 1) -> None:
        self.llm_calls += max(0, calls)

    # time ----------------------------------------------------------------
    def elapsed(self) -> float:
        return self.clock() - (self.started_at or 0.0)

    def time_left(self) -> float:
        """Seconds left of the run's wall-clock budget (negative once exceeded)."""
        return self.max_minutes * 60.0 - self.elapsed()

    def time_exceeded(self) -> bool:
        return self.max_minutes > 0 and self.time_left() <= 0
