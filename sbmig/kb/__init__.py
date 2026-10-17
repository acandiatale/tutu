"""sbmig.kb — file-based knowledge base (see docs/ARCHITECTURE.md §7, docs/INTERFACES.md).

Public API:
    FileKnowledgeBase(cfg)          implements KnowledgeBaseLike (match / record / harvest / promote …)
    kb_cli                          typer sub-app, mounted by sbmig/cli.py as `sbmig kb`
    validate_recipe_yaml(text)      -> (ok, errors, parsed) strict whitelist validation of a declarative recipe
    sanitize_case(attempt, profile, ws, phase) -> KBCase
"""
from .cli import kb_cli
from .harvest import sanitize_case
from .recipes import WHITELIST, validate_recipe_yaml
from .store import FileKnowledgeBase

__all__ = ["FileKnowledgeBase", "kb_cli", "validate_recipe_yaml", "sanitize_case", "WHITELIST"]
