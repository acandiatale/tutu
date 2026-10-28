"""sbmig.fix — LLM 수정 모듈: 수정 루프가 케이스 하나를 LLM 에 물어 원인·해결·편집·재사용 스크립트를 받아 오는
쪽(2단 기본 구현 LLMFixer)과, 회사 LLM 게이트웨이를 포함한 LLM 클라이언트들(1단 내장 구현). 흐름상 위치:
verify.build 실패 → 수정 루프가 케이스마다 사다리(재사용 → 보강 → 신규)에서 LLM 이 필요할 때
`LLMFixer.propose_script` 를 부른다. 구조 검토도 같은 클라이언트(`make_llm_client`)와 같은 답변
파서(`script_protocol`)를 쓴다. 핵심: make_llm_client(provider: company 기본 | openai_compatible | anthropic |
fake(`replay:<dir>` 재생) | 플러그인), LLMFixer(propose_script / verify_guide),
ContextPacker.pack_script(프롬프트 문맥), parse_script_response(답변 파싱), ReplayResponder(오프라인 재생).
지킬 것: fixer 는 작업 트리를 직접 고치지 않는다 — 적용·커밋·재빌드 채점·되돌리기는 루프 몫이다(예전 사내
에이전트가 빌드를 돌리지 않고 "끝났다" 고 한 실패를 되풀이하지 않도록 모든 판정은 빌드로). API 키는 환경변수
`LLM_API_KEY` 에서만 읽고 로그·기록에 남기지 않는다.

sbmig.fix — LLM fix module (see docs/INTERFACES.md for the public entry points).

    make_llm_client(cfg) -> LLMClientLike             provider switch: company | openai_compatible | anthropic | fake
                                                      | plugin / entry-point name (→ sbmig.llm, docs/ADAPTERS.md)
    LLMFixer(cfg, client, notes_dir=None)             ScriptFixerLike: propose_script(ws, cluster, ctx)
                                                      -> FixAnswer (= ScriptProposal): analysis 원인, solution
                                                         해결 방법, edits (apply: sbmig.script.apply_edits; each
                                                         with its desc), changes (EditNote: <changes> + the edits'
                                                         desc), code (the script), no_script_reason, error, …
    ContextPacker(cfg).pack_script(ws, cluster, ctx, ctx.profile_summary)
                                                      -> PackedContext; fixer plugins can reuse it for the same
                                                         errors/files/hints our LLM gets (.render() → markdown)
    parse_script_response(text)                       -> ParsedScript(code, analysis, solution, edits, edits_problem,
                                                         changes, changes_problem, no_script_reason, need_files,
                                                         need_grep, problem, …)
    parse_edits(text)                                 -> (list[Edit], problem) of one <edits> block (lenient JSON)
    ReplayResponder(load_replay_entries(dir))         FakeLLMClient answers picked by `match` / `replay_role`


"""
from .company import CompanyClient
from .client import (
    AnthropicClient,
    FakeLLMClient,
    LLMConfigError,
    LLMError,
    OpenAICompatibleClient,
    extract_reply,
    RecordingClient,
    ReplayEntry,
    ReplayResponder,
    load_replay_entries,
    load_replay_responses,
    make_llm_client,
    messages_hash,
    usage_from,
)
from .context import ContextPacker, NoteEntry, PackedContext, load_notes_index
from .fixer import LLMFixer, load_script_prompts, profile_summary
from .script_protocol import ParsedScript, normalize_path, parse_edits, parse_script_response

__all__ = [
    "make_llm_client", "LLMFixer", "FakeLLMClient", "ContextPacker", "load_notes_index", "AnthropicClient",
    "CompanyClient", "extract_reply", "OpenAICompatibleClient", "RecordingClient", "LLMError", "LLMConfigError",
    "usage_from", "messages_hash", "load_replay_responses", "PackedContext", "NoteEntry", "profile_summary",
    "load_script_prompts", "ParsedScript", "parse_script_response", "parse_edits", "normalize_path", "ReplayEntry",
    "ReplayResponder", "load_replay_entries",
]
