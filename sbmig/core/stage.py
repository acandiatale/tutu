"""모든 `sbmig.*` 로그 줄에 `[intake]`, `[fix r3 7a52b1cea8]` 같은 단계 태그를 붙이는 모듈.
수정 루프에서는 라운드와 케이스 ID가 태그다. 단계 밖이면 `-`다.
진입점은 `with stage(...)`, set_stage / reset_stage(그래프 노드), install_stage_filter, stage_formatter다.
단계는 ContextVar라서 LangGraph 실행 스레드에서도 유지되고 웹 서버의 동시 실행끼리 섞이지 않는다.

Stage names: `intake`, `analyze`, `plan`, `rewrite`, `compile`, `restructure`, `review`, `build`, `fix r3`,
`fix r3 <case>`, `kb`, `report`, `fleet`. Output: `10:14:00 INFO  [fix r1 7a52b1cea8] KB 탐색: ...`.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import Iterator

from .scrub import scrub

_STAGE: contextvars.ContextVar[str] = contextvars.ContextVar("sbmig_stage", default="-")

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(stage)s] %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"
FILE_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(stage)s] %(name)s: %(message)s"


def current_stage() -> str:
    """현재 스레드(컨텍스트)의 단계 이름을 반환한다. 단계 밖이면 "-"를 반환한다."""
    return _STAGE.get()


def set_stage(name: str) -> contextvars.Token[str]:
    """with 블록 없이 단계에 들어간다(그래프 노드에서 쓴다).
    단계를 나올 때는 반환된 토큰으로 reset_stage를 호출한다."""
    return _STAGE.set(name or "-")


def reset_stage(token: contextvars.Token[str]) -> None:
    """``set_stage``를 호출하기 전의 단계 이름으로 되돌린다."""
    _STAGE.reset(token)


@contextlib.contextmanager
def stage(name: str) -> Iterator[None]:
    """블록 안에서 기록되는 모든 로그 줄에 단계 태그를 붙인다. 중첩하면 블록을 나올 때 바깥 단계로 돌아간다."""
    token = _STAGE.set(name or "-")
    try:
        yield
    finally:
        _STAGE.reset(token)


class StageFilter(logging.Filter):
    """로그 레코드에 `record.stage`를 채우는 필터 클래스. 레코드를 걸러 내지는 않는다.
    로거 필터는 그 로거에서 만든 레코드만 보고 하위 로거에서 올라온 레코드는 못 보므로 포매터에서도 단계를 채운다."""

    def filter(self, record: logging.LogRecord) -> bool:
        """레코드에 stage가 없으면 현재 단계를 넣고 항상 True를 반환한다."""
        if not hasattr(record, "stage"):
            record.stage = _STAGE.get()
        return True


class StageFormatter(logging.Formatter):
    """로그 형식에 `%(stage)s`를 쓸 수 있게 하는 포매터 클래스. 어느 로거의 레코드든 현재 단계를 받는다."""

    def format(self, record: logging.LogRecord) -> str:
        """레코드에 stage를 채워 포맷하고, 완성된 줄을 출력 마스킹 훅(`sbmig.core.scrub`)에 통과시킨다."""
        if not hasattr(record, "stage"):
            record.stage = _STAGE.get()
        return scrub(super().format(record))

    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802, logging.Formatter의 메서드 이름을 그대로 쓴다
        """메시지 부분만 포맷한다.
        RichHandler는 예외가 있는 레코드에서 이 메서드만 호출하므로 여기서도 마스킹한다."""
        if not hasattr(record, "stage"):
            record.stage = _STAGE.get()
        return scrub(super().formatMessage(record))


def install_stage_filter(target: logging.Logger | logging.Handler | None = None) -> None:
    """`sbmig` 로거(또는 주어진 로거나 핸들러)에 StageFilter를 한 번만 붙인다."""
    t = target or logging.getLogger("sbmig")
    if not any(isinstance(f, StageFilter) for f in t.filters):
        t.addFilter(StageFilter())


def stage_formatter(fmt: str | None = None, *, for_file: bool = False, datefmt: str | None = LOG_DATE_FORMAT
                    ) -> logging.Formatter:
    """콘솔용 또는 파일용 StageFormatter를 만들어 반환한다."""
    return StageFormatter(fmt or (FILE_LOG_FORMAT if for_file else LOG_FORMAT), datefmt=datefmt)
