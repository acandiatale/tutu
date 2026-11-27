"""로그 줄마다 `[intake]`, `[fix r3 7a52b1cea8]` 같은 단계 태그를 붙이는 모듈.

Entry points: `with stage(...)`, set_stage / reset_stage (graph nodes), install_stage_filter,
stage_formatter. In the fix loop the tag is the round and the case id; outside any stage it is `-`. The
stage is a ContextVar, so it survives LangGraph's executor threads and concurrent runs of the web server do
not mix.
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
    """지금 단계 이름을 반환한다. 단계 밖이면 "-"다."""
    return _STAGE.get()


def set_stage(name: str) -> contextvars.Token[str]:
    """with 블록 없이 단계에 들어간다. 그래프 노드에서 쓴다.
    나올 때는 여기서 받은 토큰으로 ``reset_stage``를 호출한다."""
    return _STAGE.set(name or "-")


def reset_stage(token: contextvars.Token[str]) -> None:
    """``set_stage`` 이전 단계로 되돌린다."""
    _STAGE.reset(token)


@contextlib.contextmanager
def stage(name: str) -> Iterator[None]:
    """블록 안에서 남기는 로그에 단계 태그를 붙인다. 블록을 나오면 바깥 단계로 돌아간다."""
    token = _STAGE.set(name or "-")
    try:
        yield
    finally:
        _STAGE.reset(token)


class StageFilter(logging.Filter):
    """로그 레코드에 단계(`record.stage`)를 채우는 필터 클래스. 레코드를 걸러 내지는 않는다.

    A logger's filter sees only records created on that logger, not the ones child loggers pass up, so the
    formatter fills the stage as well."""

    def filter(self, record: logging.LogRecord) -> bool:
        """레코드에 단계가 없으면 지금 단계를 넣는다. 항상 True를 반환한다."""
        if not hasattr(record, "stage"):
            record.stage = _STAGE.get()
        return True


class StageFormatter(logging.Formatter):
    """로그 형식에서 `%(stage)s`를 쓸 수 있게 해 주는 포매터 클래스."""

    def format(self, record: logging.LogRecord) -> str:
        """로그 한 줄을 만든다. 단계를 채우고, 완성된 줄의 비밀 값을 마스킹한다."""
        if not hasattr(record, "stage"):
            record.stage = _STAGE.get()
        return scrub(super().format(record))

    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802, logging.Formatter의 메서드 이름을 그대로 쓴다
        """메시지 부분만 만든다.
        RichHandler는 예외가 있는 레코드에서 이 메서드만 호출하므로 여기서도 마스킹한다."""
        if not hasattr(record, "stage"):
            record.stage = _STAGE.get()
        return scrub(super().formatMessage(record))


def install_stage_filter(target: logging.Logger | logging.Handler | None = None) -> None:
    """`sbmig` 로거에 StageFilter를 붙인다. 이미 붙어 있으면 그대로 둔다.
    ``target``을 주면 그 로거나 핸들러에 붙인다."""
    t = target or logging.getLogger("sbmig")
    if not any(isinstance(f, StageFilter) for f in t.filters):
        t.addFilter(StageFilter())


def stage_formatter(fmt: str | None = None, *, for_file: bool = False, datefmt: str | None = LOG_DATE_FORMAT
                    ) -> logging.Formatter:
    """StageFormatter를 만든다. ``for_file``이면 파일용 형식, 아니면 콘솔용 형식을 쓴다."""
    return StageFormatter(fmt or (FILE_LOG_FORMAT if for_file else LOG_FORMAT), datefmt=datefmt)
