"""sbmig.errors — 실행 하나의 오류 목록과 파일 지식베이스(KB)의 패키지 입구(v2.1).
흐름상 위치: 변환 후 컴파일·검증 빌드·수정 루프가 ``ErrorList`` 에 오류와 시도를 적고, 실행이 끝나면
``CaseStore.record_run`` 이 그 목록을 KB(``errors.jsonl`` + ``cases/<case_id>/``)에 덧붙인다. 핵심 진입점:
``ErrorList``(실행 디렉터리의 ``errors.jsonl`` / ``errors.md``), ``CaseStore``(KB 케이스 폴더),
``case_id_for`` / ``case_id_of``(케이스 ID = L2 서명의 sha1 앞 10자리), ``entry_id``(항목 ID, sha1 앞 12자리).
KB 는 파일뿐이다(DB 금지, CLAUDE.md 불변조건 3). ``sbmig.kb.FileKnowledgeBase`` 가 CaseStore 의 바깥 창구다.

sbmig.errors — the v2.1 error list and knowledge base (docs/v2/PLAN-v2.1.md §3.3 – §3.5).

    ErrorList(run_dir, run_id, project=, cfg=, roots=)   one run: <run dir>/errors.jsonl + errors.md
    ErrorList.for_workspace(ws, cfg, project)            the same for a Workspace (roots stripped from texts)
    CaseStore(kb_dir, cfg)                               the KB: <kb>/errors.jsonl, cases.md, cases/<id>/case.json +
                                                         fix_v<N>.py (sbmig.kb.FileKnowledgeBase is its facade)
    case_id_for(l2) / case_id_of(error) / entry_id(…)    the ids (sha1(l2)[:10]; sha1(run|case|file|line|msg)[:12])


"""
from .cases import CaseStore, case_name, entry_lines, render_cases_md, scripts_text, valid_case_id
from .common import Cleaner, atomic_write, case_id_for, case_id_of, entry_id, note_block, note_text
from .entries import ErrorList, fmt_entry, is_synthetic, render_md, round_of, tries_text

__all__ = ["ErrorList", "CaseStore", "case_id_for", "case_id_of", "entry_id", "Cleaner", "atomic_write",
           "render_md", "render_cases_md", "tries_text", "fmt_entry", "entry_lines", "is_synthetic", "round_of",
           "case_name", "scripts_text", "valid_case_id", "note_text", "note_block"]
