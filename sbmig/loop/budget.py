"""실행 한 번의 LLM 호출 수와 실행 시간 예산을 관리하는 모듈.
오케스트레이터가 ``LoopBudget.from_config``로 만들어 ``BuildFixLoop``와 구조 검토(``_BudgetedLLM``)에
전달하므로 두 단계가 같은 예산을 함께 쓴다.
- ``loop.max_llm_calls``가 0이면 무제한(기본값), 양수면 프로젝트당 상한, -1이면 LLM을 호출하지 않는다
- -1은 플릿 전체 총량 ``fleet.max_llm_calls_total``을 다 쓴 경우다.
  저장된 스크립트는 재사용하고, LLM이 필요한 케이스는 ``budget_exhausted``로 차단한다
- ``loop.max_total_minutes``를 넘으면 수정 루프는 멈추지만 최종 빌드는 반드시 실행한다

Run-wide budgets shared by every build-fix loop of a run.

v2.1 (2026-10-27): `loop.max_llm_calls: 0` = no limit on LLM calls (the default); > 0 = a hard cap per project;
-1 = no LLM call at all (the fleet passes it when `fleet.max_llm_calls_total` is used up — stored scripts are still
reused, every step that needs the LLM is blocked `budget_exhausted` "LLM budget exhausted (fleet)")."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class LoopBudget:
    """실행 한 번의 LLM 호출 수와 경과 시간을 세는 클래스."""
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
        """설정 ``loop.max_llm_calls``와 ``loop.max_total_minutes``로 예산을 만든다.
        경과 시간은 생성 시각부터 잰다."""
        return cls(max_llm_calls=int(cfg.loop.max_llm_calls or 0), max_minutes=float(cfg.loop.max_total_minutes),
                   clock=clock)

    # LLM ---------------------------------------------------------------
    @property
    def llm_limited(self) -> bool:
        """LLM 호출 수에 상한(``loop.max_llm_calls`` > 0)이 있는지 반환한다.

        True when a hard cap is configured (`loop.max_llm_calls` > 0)."""
        return self.max_llm_calls > 0

    @property
    def llm_off(self) -> bool:
        """이 프로젝트에서 LLM 호출이 꺼져 있는지(``loop.max_llm_calls``가 -1) 반환한다.
        플릿 전체 총량을 다 쓰면 -1이 된다.

        `loop.max_llm_calls` = -1: no LLM call in this project (the fleet's total is used up)."""
        return self.max_llm_calls < 0

    def llm_allowed(self) -> bool:
        """지금 LLM을 한 번 더 호출해도 되는지 반환한다.
        호출이 꺼져 있지 않고 상한이 없거나 남아 있으면 참이다."""
        if self.llm_off:
            return False
        return not self.llm_limited or self.llm_calls < self.max_llm_calls

    def llm_exhausted(self) -> bool:
        """LLM 예산을 다 썼는지 반환한다. 호출이 꺼졌거나 상한에 도달하면 참이며, 마무리 단계에서
        ``llm-budget-exhausted`` 플래그를 붙이는 근거가 된다."""
        return self.llm_off or (self.llm_limited and self.llm_calls >= self.max_llm_calls)

    def llm_text(self) -> str:
        """차단 사유 "LLM budget exhausted (…)"의 괄호 안에 들어갈 문구를 만든다.
        '12/100 calls', '12 calls (no limit)', 'fleet'(LLM 호출 꺼짐) 중 하나를 반환한다.

        '12/100 calls', '12 calls (no limit)' or 'fleet' (no LLM call at all) — the text of
        "LLM budget exhausted (…)"."""
        if self.llm_off:
            return "fleet"
        return (f"{self.llm_calls}/{self.max_llm_calls} calls" if self.llm_limited
                else f"{self.llm_calls} calls (no limit)")

    def charge_llm(self, calls: int = 1) -> None:
        """LLM 호출 수를 더한다. 실패한 호출도 호출한 것이므로 세고, 음수는 0으로 본다."""
        self.llm_calls += max(0, calls)

    # time ----------------------------------------------------------------
    def elapsed(self) -> float:
        """예산이 시작된 뒤 지난 시간(초)을 반환한다."""
        return self.clock() - (self.started_at or 0.0)

    def time_left(self) -> float:
        """실행 시간 예산에서 남은 초를 반환한다. 예산을 넘으면 음수다.
        LLM 쿨다운이 이 값보다 길면 기다리지 않는다.

        Seconds left of the run's wall-clock budget (negative once exceeded)."""
        return self.max_minutes * 60.0 - self.elapsed()

    def time_exceeded(self) -> bool:
        """실행 시간 예산을 넘었는지 반환한다. ``max_minutes``가 0 이하면 시간 제한이 없다."""
        return self.max_minutes > 0 and self.time_left() <= 0
