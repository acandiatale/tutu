"""LLM이 쓴 수정 스크립트를 검사하고 실행하는 패키지.
``def fix(p)`` 스크립트를 정적 검사한 뒤 별도 ``python -I`` 프로세스에서 도우미 API ``p``를 넘겨 실행한다.

Spec: docs/v2/PLAN-v2.md §3.2, docs/INTERFACES.md "Script contract (S)".
* Edits (`Edit`: replace / write / create / delete / move / pom): `edits_script` synthesizes the `def fix(p)`.
* Notes: `answer_notes` (the LLM's `<changes>` + each edit's `desc`), `notes_from_diff` (one note per
  file of a unified diff), `merge_notes` (the LLM's notes with the diff's lines, plus notes for files it
  did not mention).

Callers: the fix loop, the LLM answer parser and the structure review's critical fixes. ``check_script``
checks a script, ``ScriptRunner`` runs it, ``p`` is ``sbmig.script.api``. ``p`` handles files inside the
project folder only: a guard against mistakes, not a sandbox against hostile code. The LLM's ``<edits>`` go
through the same API and the same limits (``apply_edits`` turns them into a ``def fix(p)`` and runs it).
Change notes (``EditNote``) and hunks (``EditHunk``) live in ``sbmig.script.notes``.
"""
from .api import HELPER_API_DOC, helper_api_doc
from .check import ALLOWED_IMPORTS, REFUSED_MODULES, REFUSED_NAMES, check_script
from .edits import EDIT_OPS, POM_CALLS, apply_edits, edit_problems, edits_from_json, edits_script
from .notes import answer_notes, attach_hunks, hunks_from_diff, merge_notes, notes_from_diff
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
    "attach_hunks",
    "hunks_from_diff",
    "apply_edits",
    "check_script",
    "edit_problems",
    "edits_from_json",
    "edits_script",
    "helper_api_doc",
    "merge_notes",
    "notes_from_diff",
]
