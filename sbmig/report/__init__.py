"""실행 보고서(``report.json``, ``report.md``, 콘솔 요약)를 만드는 패키지.
수정 루프와 최종 빌드가 끝난 뒤 오케스트레이터가 ``write_report``를 호출한다.
- ``write_report``: 보고서 파일을 쓴다
- ``render_markdown``: report.md 본문을 만든다
- ``build_handover``: 인수인계 섹션의 데이터를 만든다
- ``console_summary``: 콘솔의 마지막 요약을 만든다
담당 팀은 인수인계 섹션만 읽으므로 이 섹션을 보고서 맨 앞에 둔다.

sbmig.report — report.json / report.md / console summary."""
from .handover import build_handover
from .markdown import (BLOCKED_TITLE, HANDOVER_TITLE, blocked_count_line, flag_help, group_by_file, handover_lines,
                       render_markdown)
from .writer import advisory_lines, console_summary, write_report

__all__ = ["write_report", "render_markdown", "console_summary", "group_by_file", "BLOCKED_TITLE",
           "blocked_count_line", "flag_help", "build_handover", "HANDOVER_TITLE", "handover_lines", "advisory_lines"]
