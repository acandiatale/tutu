"""실행 보고서 패키지 — ``report.json`` / ``report.md`` / 콘솔 요약을 만든다.
흐름상 위치: 수정 루프와 최종 빌드가 끝난 뒤 오케스트레이터가 ``write_report`` 를 부른다(보고서 단계). 핵심
진입점: ``write_report``(파일 쓰기), ``render_markdown``(report.md 본문), ``build_handover``(인수인계 섹션
데이터), ``console_summary``(콘솔 마지막 요약). 보고서는 인수인계 섹션이 맨 앞 — 담당팀은 그 섹션만 읽는다.

sbmig.report — report.json / report.md / console summary."""
from .handover import build_handover
from .markdown import (BLOCKED_TITLE, HANDOVER_TITLE, blocked_count_line, flag_help, group_by_file, handover_lines,
                       render_markdown)
from .writer import advisory_lines, console_summary, write_report

__all__ = ["write_report", "render_markdown", "console_summary", "group_by_file", "BLOCKED_TITLE",
           "blocked_count_line", "flag_help", "build_handover", "HANDOVER_TITLE", "handover_lines", "advisory_lines"]
