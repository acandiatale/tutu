"""sbmig.errors — the v2.1 error list and knowledge base (docs/v2/PLAN-v2.1.md §3.3 – §3.5).

    ErrorList(run_dir, run_id, project=, cfg=, roots=)   one run: <run dir>/errors.jsonl + errors.md
    ErrorList.for_workspace(ws, cfg, project)            the same for a Workspace (roots stripped from texts)
    CaseStore(kb_dir, cfg)                               the KB: <kb>/errors.jsonl, cases.md, cases/<id>/case.json +
                                                         fix_v<N>.py (sbmig.kb.FileKnowledgeBase is its facade)
    case_id_for(l2) / case_id_of(error) / entry_id(…)    the ids (sha1(l2)[:10]; sha1(run|case|file|line|msg)[:12])
"""
from .cases import CaseStore, case_name, entry_lines, render_cases_md, scripts_text, valid_case_id
from .common import Cleaner, atomic_write, case_id_for, case_id_of, entry_id, note_text
from .entries import ErrorList, fmt_entry, is_synthetic, render_md, round_of, tries_text

__all__ = ["ErrorList", "CaseStore", "case_id_for", "case_id_of", "entry_id", "Cleaner", "atomic_write",
           "render_md", "render_cases_md", "tries_text", "fmt_entry", "entry_lines", "is_synthetic", "round_of",
           "case_name", "scripts_text", "valid_case_id", "note_text"]
