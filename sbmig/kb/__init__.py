"""sbmig.kb — file-based knowledge base (see docs/ARCHITECTURE.md §7, docs/INTERFACES.md).

Public API:
    FileKnowledgeBase(cfg)          implements KnowledgeBaseLike (match / record / harvest / promote …)
    kb_cli                          typer sub-app, mounted by sbmig/cli.py as `sbmig kb`
    validate_recipe_yaml(text)      -> (ok, errors, parsed) strict whitelist validation of a declarative recipe
    sanitize_case(attempt, profile, ws, phase) -> KBCase   (with project / symbol / fix_labels / replacement)
    label_changes(changes, errors) -> LabelResult      deterministic fix labels (sbmig.kb.labels)
    compile_recipe(case) -> yaml | None                 recipe candidate from mechanical labels, no LLM
    Ledger(path) / SymbolIndex(path)                    kb/ledger.jsonl and kb/symbols.json (sbmig.kb.ledger)
    classify_diff(diff) -> ChangeClass                  text replace vs code edit of a diff (sbmig.kb.changes)
    prepare_error_cases(errors, attempts, profile, ws, cfg) -> (errors, attempts)   error-case records → KB records
    ErrorCaseStore(kb_dir)                              kb/errors.jsonl, fix_attempts.jsonl, error_hints.json
    CaseCatalog(kb_dir, cfg=None) / case_id_for(L2)     kb/catalog/ (one case per L2 signature) + kb/views/
"""
from .catalog import CaseCatalog, case_id_for
from .changes import classify_diff
from .cli import kb_cli
from .error_cases import ErrorCaseStore, prepare_error_cases
from .harvest import sanitize_case
from .labels import LabelResult, compile_recipe, label_changes
from .ledger import Ledger, SymbolIndex, render_learning_md
from .recipes import WHITELIST, validate_recipe_yaml
from .store import FileKnowledgeBase

__all__ = ["FileKnowledgeBase", "kb_cli", "validate_recipe_yaml", "sanitize_case", "WHITELIST", "LabelResult",
           "label_changes", "compile_recipe", "Ledger", "SymbolIndex", "render_learning_md", "classify_diff",
           "prepare_error_cases", "ErrorCaseStore", "CaseCatalog", "case_id_for"]
