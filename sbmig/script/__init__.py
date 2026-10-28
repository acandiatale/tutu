"""sbmig.script — v2 수정 스크립트 실행기(정적 검사 + 별도 프로세스 + 도우미 API ``p``).
LLM 이 빌드 오류에 파이썬 스크립트 ``def fix(p)`` 로 답하면 sbmig 가 정적 검사(``check_script``) 후 별도
``python -I`` 프로세스(``ScriptRunner``)에서 도우미 API ``p``(``sbmig.script.api``)로 돌린다. 흐름상 위치:
수정 루프(사다리의 재사용·보강·신규), LLM 답변 파서(저장 전 검사), 구조 검토의 critical 수정이 부른다. ``p``
는 프로젝트 폴더 안의 파일만 만진다. 이것은 실수를 막는 가드이지 악의적인 작성자를 막는 샌드박스가
아니다(불변조건 7). v2.1: LLM 의 ``<edits>``(직접 수정)도 같은 API·가드로 적용한다 — ``apply_edits`` 가 편집
목록으로 ``def fix(p)`` 를 합성해 돌린다. v2.1b / v2.1e: 모든 수정을 설명하는 수정 기록(``EditNote``)과 변경
조각(수정 전/후, ``EditHunk``)은 ``sbmig.script.notes``.

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
