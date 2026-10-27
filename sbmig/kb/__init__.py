"""sbmig.kb — file-based knowledge base (see docs/ARCHITECTURE.md §7, docs/INTERFACES.md, docs/v2/PLAN-v2.md §3.4).

Public API:
    FileKnowledgeBase(cfg)          KnowledgeBaseLike.match / reindex + ScriptKnowledgeLike (scripts, record_review),
                                    append_error_cases, stats, export_zip / import_zip
    kb_cli                          typer sub-app, mounted by sbmig/cli.py as `sbmig kb`
    label_changes(changes, errors) -> LabelResult      deterministic fix labels (sbmig.kb.labels)
    classify_diff(diff) -> ChangeClass                  text replace vs code edit of a diff (sbmig.kb.changes)
    prepare_error_cases(errors, attempts, profile, ws, cfg) -> (errors, attempts)   error-case records → KB records
    ErrorCaseStore(kb_dir)                              kb/errors.jsonl, kb/fix_attempts.jsonl
    CaseCatalog(kb_dir, cfg=None) / case_id_for(L2)     kb/catalog/ (one case per L2 signature) + kb/views/
    redact(text, cfg=None)                              secret VALUES masked by context (sbmig.kb.redact, round 10)
    kb_lock(kb_dir, timeout)                            the cross-process write lock <kb>/.lock (sbmig.kb.lock)
    ScriptStore(kb_dir, cfg=None)                       v2 fix scripts per case kb/scripts/<case>/v<N>.py (+ meta)
    project_name(profile, ws) / changes_as_diff(changes)   shared record helpers (sbmig.kb.common)
"""
from .catalog import CaseCatalog, case_id_for
from .changes import classify_diff
from .cli import kb_cli
from .common import changes_as_diff, project_name
from .error_cases import ErrorCaseStore, prepare_error_cases
from .labels import LabelResult, label_changes
from .lock import kb_lock
from .redact import redact
from .scripts import ScriptStore, parse_script_resolver, script_resolver
from .store import FileKnowledgeBase

__all__ = ["FileKnowledgeBase", "kb_cli", "LabelResult", "label_changes", "classify_diff", "prepare_error_cases",
           "ErrorCaseStore", "CaseCatalog", "case_id_for", "redact", "kb_lock", "ScriptStore", "script_resolver",
           "parse_script_resolver", "project_name", "changes_as_diff"]
