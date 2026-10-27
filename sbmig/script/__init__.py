"""sbmig.script — v2 fix scripts: the LLM answers a build error with a Python script `def fix(p)`; sbmig checks it
statically (`check_script`) and runs it in a separate `python -I` process (`ScriptRunner`) against the helper API `p`
(`sbmig.script.api`), which only touches files inside the project folder. A guard against mistakes, not a sandbox
against a hostile author (docs/v2/PLAN-v2.md §3.2, docs/INTERFACES.md "Script contract (S)").

v2.1: the LLM's `<edits>` (`Edit`: replace / write / create / delete / move / pom) are applied through the same
helper API and guard — `apply_edits` synthesizes a `def fix(p)` from them (`edits_script`) and runs it here.

v2.1b: every modification is described (`EditNote`) — `answer_notes` (the LLM's `<changes>` + each edit's `desc`),
`notes_from_diff` (one note per file of a unified diff) and `merge_notes` (the LLM's notes with the diff's lines, plus
notes for the files it did not mention), in `sbmig.script.notes`."""
from .api import HELPER_API_DOC, helper_api_doc
from .check import ALLOWED_IMPORTS, REFUSED_MODULES, REFUSED_NAMES, check_script
from .edits import EDIT_OPS, POM_CALLS, apply_edits, edit_problems, edits_from_json, edits_script
from .notes import answer_notes, merge_notes, notes_from_diff
from .runner import ScriptRunner

__all__ = [
    "ALLOWED_IMPORTS",
    "EDIT_OPS",
    "POM_CALLS",
    "HELPER_API_DOC",
    "REFUSED_MODULES",
    "REFUSED_NAMES",
    "ScriptRunner",
    "answer_notes",
    "apply_edits",
    "check_script",
    "edit_problems",
    "edits_from_json",
    "edits_script",
    "helper_api_doc",
    "merge_notes",
    "notes_from_diff",
]
