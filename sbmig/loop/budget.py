"""실행 전체가 함께 쓰는 예산(LLM 호출 수, 실행 시간) — 한 실행의 수정 루프와 구조 검토가 같은 객체를 센다.
오케스트레이터가 ``LoopBudget.from_config`` 로 하나 만들어 ``BuildFixLoop`` 와 구조 검토 (``_BudgetedLLM``)에
넘긴다. ``loop.max_llm_calls``: 0 = 무제한(기본), 양수 = 프로젝트당 상한, -1 = LLM 호출 없음(플릿 총량
``fleet.max_llm_calls_total`` 소진 — 저장된 스크립트는 그대로 재사용, LLM 이 필요한 케이스는
``budget_exhausted`` 차단). 시간 상한은 ``loop.max_total_minutes``:
넘으면 루프는 멈추지만 최종 빌드는 여전히 돈다(빌드 게이트).

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
    """한 실행의 LLM 호출 수와 경과 시간을 세는 예산(0 / 양수 / -1 규칙은 모듈 설명 참고)."""
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
        """설정(``loop.max_llm_calls``, ``loop.max_total_minutes``)으로 예산을 만든다. 만든 시각부터 잰다."""
        return cls(max_llm_calls=int(cfg.loop.max_llm_calls or 0), max_minutes=float(cfg.loop.max_total_minutes),
                   clock=clock)

    # LLM ---------------------------------------------------------------
    @property
    def llm_limited(self) -> bool:
        """상한이 걸린 실행인가(``loop.max_llm_calls`` > 0).

        True when a hard cap is configured (`loop.max_llm_calls` > 0)."""
        return self.max_llm_calls > 0

    @property
    def llm_off(self) -> bool:
        """``loop.max_llm_calls`` = -1: 이 프로젝트는 LLM 을 아예 부르지 않는다(플릿 전체 총량 소진).

        `loop.max_llm_calls` = -1: no LLM call in this project (the fleet's total is used up)."""
        return self.max_llm_calls < 0

    def llm_allowed(self) -> bool:
        """지금 LLM 을 한 번 더 불러도 되는가(꺼져 있지 않고, 상한이 없거나 아직 남았을 때)."""
        if self.llm_off:
            return False
        return not self.llm_limited or self.llm_calls < self.max_llm_calls

    def llm_exhausted(self) -> bool:
        """LLM 예산을 다 썼는가(꺼짐, 또는 상한 도달). 마무리의 ``llm-budget-exhausted`` 플래그 근거."""
        return self.llm_off or (self.llm_limited and self.llm_calls >= self.max_llm_calls)

    def llm_text(self) -> str:
        """차단 사유 "LLM budget exhausted (…)" 의 괄호 안 글: '12/100 calls', '12 calls (no limit)',
        'fleet'(LLM 호출 없음).

        '12/100 calls', '12 calls (no limit)' or 'fleet' (no LLM call at all) — the text of
        "LLM budget exhausted (…)"."""
        if self.llm_off:
            return "fleet"
        return (f"{self.llm_calls}/{self.max_llm_calls} calls" if self.llm_limited
                else f"{self.llm_calls} calls (no limit)")

    def charge_llm(self, calls: int = 1) -> None:
        """LLM 호출 수를 더한다(음수는 0). 실패한 호출도 부른 것이므로 센다."""
        self.llm_calls += max(0, calls)

    # time ----------------------------------------------------------------
    def elapsed(self) -> float:
        """예산을 만든 뒤 지난 초."""
        return self.clock() - (self.started_at or 0.0)

    def time_left(self) -> float:
        """실행 시간 예산의 남은 초(넘기면 음수). LLM 쿨다운은 이보다 길면 쉬지 않는다.

        Seconds left of the run's wall-clock budget (negative once exceeded)."""
        return self.max_minutes * 60.0 - self.elapsed()

    def time_exceeded(self) -> bool:
        """실행 시간 예산을 넘었는가(``max_minutes`` 0 이하 = 시간 제한 없음)."""
        return self.max_minutes > 0 and self.time_left() <= 0
