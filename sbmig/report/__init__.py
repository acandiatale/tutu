"""sbmig.report — report.json / report.md / console summary."""
from .markdown import group_by_file, render_markdown
from .writer import console_summary, write_report

__all__ = ["write_report", "render_markdown", "console_summary", "group_by_file"]
