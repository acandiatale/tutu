"""Run-wide budgets shared by every build-fix loop of a run."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class LoopBudget:
    max_llm_calls: int = 60
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
        return cls(max_llm_calls=cfg.loop.max_llm_calls, max_minutes=float(cfg.loop.max_total_minutes), clock=clock)

    # LLM ---------------------------------------------------------------
    def llm_allowed(self) -> bool:
        return self.llm_calls < self.max_llm_calls

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
