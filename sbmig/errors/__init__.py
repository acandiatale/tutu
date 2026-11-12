"""실행 한 번의 오류 목록과 파일 KB를 다루는 패키지.
검증 빌드와 수정 루프가 ``ErrorList``에 오류와 시도를 기록하고, 실행이 끝나면 ``CaseStore.record_run``이 그
목록을 KB(``errors.jsonl``, ``cases/<case_id>/``)에 추가한다.
- ``ErrorList``: 실행 디렉토리의 ``errors.jsonl`` / ``errors.md``를 관리한다
- ``CaseStore``: KB의 케이스 폴더를 관리한다. 바깥에서는 파사드 ``sbmig.kb.FileKnowledgeBase``로 쓴다
- ``case_id_for`` / ``case_id_of``: 케이스 ID(L2 서명의 sha1 앞 10자리)를 만든다
- ``entry_id``: 항목 ID(sha1 앞 12자리)를 만든다
KB는 파일로만 관리한다(DB 사용 금지).

sbmig.errors — the v2.1 error list and knowledge base (docs/v2/PLAN-v2.1.md §3.3 – §3.5).

    ErrorList(run_dir, run_id, project=, cfg=, roots=)   one run: <run dir>/errors.jsonl + errors.md
    ErrorList.for_workspace(ws, cfg, project)            the same for a Workspace (roots stripped from texts)
    CaseStore(kb_dir, cfg)                               the KB: <kb>/errors.jsonl, cases.md, cases/<id>/case.json +
                                                         fix_v<N>.py (sbmig.kb.FileKnowledgeBase is its facade)
    case_id_for(l2) / case_id_of(error) / entry_id(…)    the ids (sha1(l2)[:10]; sha1(run|case|file|line|msg)[:12])
    CaseStore.resign(dry_run=False) -> ResignReport      v2.1z-ai: records of older signature rules → current case ids
                                                         (sbmig.errors.resign holds the pure parts)
    CaseKeyMismatch                                      the stored case holds another L2 (exact-key guard; an OSError)


"""
from .cases import CaseKeyMismatch, CaseStore, case_name, entry_lines, render_cases_md, scripts_text, valid_case_id
from .common import Cleaner, atomic_write, case_id_for, case_id_of, entry_id, note_block, note_text
from .entries import ErrorList, fmt_entry, is_synthetic, render_md, round_of, tries_text
from .resign import ResignReport, rebuild_error, resigned_l2

__all__ = ["ErrorList", "CaseStore", "case_id_for", "case_id_of", "entry_id", "Cleaner", "atomic_write",
           "render_md", "render_cases_md", "tries_text", "fmt_entry", "entry_lines", "is_synthetic", "round_of",
           "case_name", "scripts_text", "valid_case_id", "note_text", "note_block", "CaseKeyMismatch", "ResignReport",
           "rebuild_error", "resigned_l2"]
