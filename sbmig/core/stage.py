"""로그 줄의 단계 태그 — 모든 `sbmig.*` 로그 줄에 `[intake]`, `[fix r3 7a52b1cea8]` 같은 단계를 붙인다. 태그는
단계 이름이고, 수정 루프에서는 라운드 + 케이스 ID 다. v2.1 요청(2026-10-27 "로그가 추적이 안 된다"): sbmig.log
한 줄만 보고도 어느 단계·케이스인지 알게 한다. 진입점: `with stage(...)`(코드 블록), set_stage /
reset_stage(그래프 노드), install_stage_filter, stage_formatter. 단계는 ContextVar 라서 LangGraph 실행
스레드가 컨텍스트를 복사해도 유지되고, 웹 서버의 동시 실행끼리는 섞이지 않는다.

The pipeline stage a log line belongs to (v2.1, 2026-10-27: "로그가 추적이 안 된다").

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
    """지금 스레드(컨텍스트)의 단계 이름(단계 밖이면 "-")."""
    return _STAGE.get()


def set_stage(name: str) -> contextvars.Token[str]:
    """with 블록 없이 단계에 들어간다(그래프 노드). 나올 때는 돌려받은 토큰으로 reset_stage.

    Enter a stage without a `with` block (graph nodes); reset with :func:`reset_stage`."""
    return _STAGE.set(name or "-")


def reset_stage(token: contextvars.Token[str]) -> None:
    """set_stage 이전 단계로 되돌린다."""
    _STAGE.reset(token)


@contextlib.contextmanager
def stage(name: str) -> Iterator[None]:
    """`with stage("fix r3 7a52b1cea8"): …` — 안의 모든 로그
    줄에 태그가 붙고, 겹쳐 쓰면 나올 때 바깥 단계로 돌아간다.

    `with stage("fix r3 7a52b1cea8"): …` — every log line inside is tagged; nesting restores the outer stage."""
    token = _STAGE.set(name or "-")
    try:
        yield
    finally:
        _STAGE.reset(token)


class StageFilter(logging.Filter):
    """`record.stage` 를 채우는 필터(아무것도 거르지 않음). 로거
    필터는 그 로거에서 만든 레코드만 보므로 포매터도 같은 일을 한다.

    Sets `record.stage` (never filters anything). A logger-level filter only sees records created on that logger
    (not those propagated from `sbmig.loop` & co), so the formatter below fills the field as well."""

    def filter(self, record: logging.LogRecord) -> bool:
        """레코드에 stage 가 없으면 현재 단계를 넣는다. 항상 True."""
        if not hasattr(record, "stage"):
            record.stage = _STAGE.get()
        return True


class StageFormatter(logging.Formatter):
    """`%(stage)s` 를 쓸 수 있는 포매터: 어느 로거의 레코드든 현재 단계를 받는다.

    A formatter whose format string may use `%(stage)s`: records from any logger get the current stage."""

    def format(self, record: logging.LogRecord) -> str:
        """stage 를 채운 뒤 일반 포맷."""
        if not hasattr(record, "stage"):
            record.stage = _STAGE.get()
        return super().format(record)


def install_stage_filter(target: logging.Logger | logging.Handler | None = None) -> None:
    """`sbmig` 로거(또는 주어진 로거·핸들러)에 StageFilter 를 한 번만 단다."""
    t = target or logging.getLogger("sbmig")
    if not any(isinstance(f, StageFilter) for f in t.filters):
        t.addFilter(StageFilter())


def stage_formatter(fmt: str | None = None, *, for_file: bool = False, datefmt: str | None = LOG_DATE_FORMAT
                    ) -> logging.Formatter:
    """콘솔(LOG_FORMAT) 또는 파일(FILE_LOG_FORMAT, 로거 이름 포함)용 StageFormatter 를 만든다."""
    return StageFormatter(fmt or (FILE_LOG_FORMAT if for_file else LOG_FORMAT), datefmt=datefmt)
