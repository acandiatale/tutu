"""LLM이 쓴 수정 스크립트를 검사하고 실행하는 모듈.
정적 검사, 별도 프로세스 실행, 도우미 API ``p``를 묶는다.
LLM이 ``def fix(p)`` 스크립트로 답하면 ``check_script``로 검사한 뒤 별도 ``python -I``
프로세스(``ScriptRunner``)에서 ``p``(``sbmig.script.api``)를 넘겨 실행한다.
수정 루프, LLM 답변 파서, 구조 검토의 critical 수정이 이 모듈을 호출한다.
- ``p``는 프로젝트 폴더 안의 파일만 다룬다. 실수를 막는 장치일 뿐 악의적인 코드를 막는 샌드박스가 아니다
- LLM의 ``<edits>``도 같은 API와 같은 제한으로 적용한다.
  ``apply_edits``가 편집 목록으로 ``def fix(p)``를 만들어 실행한다
- 수정 기록(``EditNote``)과 변경 조각(``EditHunk``)은 ``sbmig.script.notes``에 있다

sbmig.script — v2 fix scripts: the LLM answers a build error with a Python script `def fix(p)`; sbmig checks it
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
