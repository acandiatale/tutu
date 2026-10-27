"""The pipeline stage a log line belongs to (v2.1, 2026-10-27: "로그가 추적이 안 된다").

Every `sbmig.*` log record carries `record.stage` — the innermost :func:`stage` context of the thread that logs
(`intake`, `analyze`, `plan`, `rewrite`, `compile`, `restructure`, `review`, `build`, `fix r3`,
`fix r3 7a52b1cea8` = round + case, `kb`, `report`, `fleet`; `-` outside any stage) — and the file / console
formatters print it in brackets: `10:14:00 INFO  [fix r1 7a52b1cea8] KB 탐색: 저장된 스크립트 없음 …`.

The context is a :class:`contextvars.ContextVar`, so a LangGraph executor thread that copies the context keeps the
run's stage, and the web server's concurrent runs never see each other's. :func:`install_stage_filter` adds the filter
once to the `sbmig` logger; records from other loggers get `-`.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import Iterator

_STAGE: contextvars.ContextVar[str] = contextvars.ContextVar("sbmig_stage", default="-")

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(stage)s] %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"
FILE_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(stage)s] %(name)s: %(message)s"


def current_stage() -> str:
    return _STAGE.get()


def set_stage(name: str) -> contextvars.Token[str]:
    """Enter a stage without a `with` block (graph nodes); reset with :func:`reset_stage`."""
    return _STAGE.set(name or "-")


def reset_stage(token: contextvars.Token[str]) -> None:
    _STAGE.reset(token)


@contextlib.contextmanager
def stage(name: str) -> Iterator[None]:
    """`with stage("fix r3 7a52b1cea8"): …` — every log line inside is tagged; nesting restores the outer stage."""
    token = _STAGE.set(name or "-")
    try:
        yield
    finally:
        _STAGE.reset(token)


class StageFilter(logging.Filter):
    """Sets `record.stage` (never filters anything). A logger-level filter only sees records created on that logger
    (not those propagated from `sbmig.loop` & co), so the formatter below fills the field as well."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "stage"):
            record.stage = _STAGE.get()
        return True


class StageFormatter(logging.Formatter):
    """A formatter whose format string may use `%(stage)s`: records from any logger get the current stage."""

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "stage"):
            record.stage = _STAGE.get()
        return super().format(record)


def install_stage_filter(target: logging.Logger | logging.Handler | None = None) -> None:
    t = target or logging.getLogger("sbmig")
    if not any(isinstance(f, StageFilter) for f in t.filters):
        t.addFilter(StageFilter())


def stage_formatter(fmt: str | None = None, *, for_file: bool = False, datefmt: str | None = LOG_DATE_FORMAT
                    ) -> logging.Formatter:
    return StageFormatter(fmt or (FILE_LOG_FORMAT if for_file else LOG_FORMAT), datefmt=datefmt)
