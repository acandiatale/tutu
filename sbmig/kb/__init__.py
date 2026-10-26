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
    redact(text, cfg=None)                              secret VALUES masked by context (sbmig.kb.redact, round 10)
    kb_lock(kb_dir, timeout)                            the cross-process write lock <kb>/.lock (sbmig.kb.lock)
    pairset_hash(pairs) / case_replace_note(id, pairs)  how handler case-replace names the pair set it applied
    ScriptStore(kb_dir, cfg=None)                       v2 fix scripts per case kb/scripts/<case>/v<N>.py (+ meta);
                                                        FileKnowledgeBase.scripts, .record_review(review)
"""
from .catalog import CaseCatalog, case_id_for, case_replace_note, pairset_hash
from .changes import classify_diff
from .cli import kb_cli
from .error_cases import ErrorCaseStore, prepare_error_cases
from .harvest import sanitize_case
from .labels import LabelResult, compile_recipe, label_changes
from .ledger import Ledger, SymbolIndex, render_learning_md
from .lock import kb_lock
from .recipes import WHITELIST, validate_recipe_yaml
from .redact import redact
from .scripts import ScriptStore, parse_script_resolver, script_resolver
from .store import FileKnowledgeBase

__all__ = ["FileKnowledgeBase", "kb_cli", "validate_recipe_yaml", "sanitize_case", "WHITELIST", "LabelResult",
           "label_changes", "compile_recipe", "Ledger", "SymbolIndex", "render_learning_md", "classify_diff",
           "prepare_error_cases", "ErrorCaseStore", "CaseCatalog", "case_id_for", "redact", "kb_lock",
           "pairset_hash", "case_replace_note", "ScriptStore", "script_resolver", "parse_script_resolver"]
