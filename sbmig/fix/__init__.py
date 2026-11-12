"""빌드 오류 수정을 LLM에 요청하는 패키지. 케이스 하나의 원인, 해결, 편집 내용, 재사용할 스크립트를 LLM에서
받아 오는 LLMFixer와, 회사 게이트웨이를 포함한 LLM 클라이언트를 제공한다.
수정 루프는 재사용, 보강, 신규 단계에서 LLM이 필요할 때 `LLMFixer.propose_script`를 호출한다.
구조 검토도 같은 클라이언트와 답변 파서를 쓴다.
- fixer는 작업 트리를 직접 고치지 않는다.
  적용, 커밋, 재빌드로 하는 시도 결과 평가, 되돌리기는 수정 루프가 하고 성공 여부는 모두 빌드로 판단한다
- API 키는 환경 변수 `LLM_API_KEY`에서만 읽고 로그나 기록에 남기지 않는다

sbmig.fix — LLM fix module (see docs/INTERFACES.md for the public entry points).

    make_llm_client(cfg) -> LLMClientLike             provider switch: company | openai_compatible | anthropic | fake
                                                      | plugin / entry-point name (→ sbmig.llm, docs/ADAPTERS.md)
    LLMFixer(cfg, client, notes_dir=None)             ScriptFixerLike: propose_script(ws, cluster, ctx)
                                                      -> FixAnswer (= ScriptProposal): analysis 원인, solution
                                                         해결 방법, edits (apply: sbmig.script.apply_edits; each
                                                         with its desc), changes (EditNote: <changes> + the edits'
                                                         desc), code (the script), no_script_reason,
                                                         blocked_reason (v2.1y <blocked>), error, …
    ContextPacker(cfg).pack_script(ws, cluster, ctx, ctx.profile_summary)
                                                      -> PackedContext; fixer plugins can reuse it for the same
                                                         errors/files/hints our LLM gets (.render() → markdown)
    parse_script_response(text)                       -> ParsedScript(code, analysis, solution, edits, edits_problem,
                                                         changes, changes_problem, no_script_reason,
                                                         blocked_reason, need_files,
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
