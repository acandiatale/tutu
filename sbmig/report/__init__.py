"""실행 보고서(``report.json``, ``report.md``, 콘솔 요약)를 만드는 패키지.

The orchestrator calls ``write_report`` after the fix loop and the final build. ``write_report`` writes the
report files, ``render_markdown`` builds the report.md text, ``build_handover`` the Handover section's data,
``console_summary`` the last console summary. The owning team reads only the Handover section, so it comes
first in the report.
"""
from .handover import build_handover
from .markdown import (BLOCKED_TITLE, HANDOVER_TITLE, blocked_count_line, flag_help, group_by_file, handover_lines,
                       render_markdown)
from .writer import advisory_lines, console_summary, write_report

__all__ = ["write_report", "render_markdown", "console_summary", "group_by_file", "BLOCKED_TITLE",
           "blocked_count_line", "flag_help", "build_handover", "HANDOVER_TITLE", "handover_lines", "advisory_lines"]
