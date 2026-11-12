"""사내 코드나 에이전트 프레임워크에서 sbmig를 호출하는 프로그램용 진입점 ``migrate(...)`` 모듈(docs/ADAPTERS.md).
`sbmig run`과 같은 파이프라인을 실행한다.
- 회사 쪽 통합이 의존하므로 `sbmig.api.migrate(...)`의 시그니처는 바꾸지 않는다
- import는 함수 안에서 필요할 때 하므로 `import sbmig.api`는 가볍다

Programmatic entry point: run sbmig from your own code / agent framework (docs/ADAPTERS.md).

    from sbmig.api import migrate, exit_code
    report = migrate("path/to/project", configs=["config/artifacts.yml"], llm=MyGatewayAdapter())
    print(report.success, exit_code(report))

Imports are lazy (like the CLI) so `import sbmig.api` is cheap.


"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence, Union

if TYPE_CHECKING:  # pragma: no cover
    from sbmig.core.interfaces import KnowledgeBaseLike, LLMAdapterLike, LLMClientLike, ScriptFixerLike
    from sbmig.core.models import RunReport

# `kb=False`면 KB를 읽지도 쓰지도 않는 NullKnowledgeBase를 쓴다.
# 실행 디렉토리의 errors.jsonl / errors.md는 그대로 쓴다
# `kb=False` here: the knowledge base is neither read nor fed — the KB object is a
# NullKnowledgeBase (no stored fix script is reused, nothing is recorded); the run's own errors.jsonl / errors.md are
# still written to the run dir. v2.1 has no KB switches left in the config, so nothing is overridden.
NO_KB_OVERRIDES: dict = {}
_NO_KB_OVERRIDES = NO_KB_OVERRIDES          # the pre-round-10 name


def _merge(base: dict, extra: Mapping) -> dict:
    """overrides에 kb 설정을 더할 때 쓰기 위해 dict를 깊게 합친다."""
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def migrate(
    input: Union[str, Path],
    *,
    configs: Sequence[Union[str, Path]] = (),
    out: Union[str, Path] = "out",
    run_id: Optional[str] = None,
    overrides: Optional[dict] = None,
    llm: "LLMClientLike | LLMAdapterLike | None" = None,
    fixer: "ScriptFixerLike | None" = None,
    kb: "KnowledgeBaseLike | None | bool" = None,
    stop_after: Optional[str] = None,
    dry_run: bool = False,
    repo_root: Optional[Union[str, Path]] = None,
    components: Optional[Mapping[str, Any]] = None,
    flow: str = "full",
) -> "RunReport":
    """프로젝트 하나(디렉토리 또는 zip)를 `sbmig run`과 같은 파이프라인으로 끝까지 이관하고 RunReport를
    반환한다. ``flow="remediation"``이면 이관 없이 취약점 조치만 한다.
    llm에는 회사 게이트웨이 어댑터(LLMAdapterLike)나 클라이언트를, fixer에는 사내 수정기(propose_script)를
    줄 수 있다. CLI 종료 코드는 exit_code(report)로 얻는다.

    Migrate one project (directory or zip) end to end — same pipeline as `sbmig run`.

    llm        an LLMClientLike (message-based) or an LLMAdapterLike (`complete(request)`, wrapped in
               AdapterClient automatically). None → built from config (`llm.provider`, incl. plugins).
               Recorded to `llm.record_dir` when that is set.
    fixer      a fixer with `propose_script(ws, cluster, ctx) -> ScriptProposal` (ScriptFixerLike) replacing
               LLMFixer (takes precedence over `fix.plugin`).
    kb         None/True → the file knowledge base; False → no KB (a NullKnowledgeBase); an object → used as is.
    stop_after intake|analyze|plan|transform|restructure (v2; jdk21|spring are v1 phases).
    components advanced: extra Orchestrator collaborators (build_runner, rewrite_runner, restructurer, …).
    flow       "full" (default: the migration) | "remediation" (no migration: the vulnerability remediation of the
               project as uploaded; not with stop_after / dry_run — ValueError).
    Returns the RunReport; `exit_code(report)` maps it to the CLI exit code (0 / 2 / 3)."""
    from sbmig.core.config import load_config
    from sbmig.core.workspace import Workspace
    from sbmig.loop import NullKnowledgeBase, Orchestrator, check_flow, parse_stop_after

    phase = parse_stop_after(stop_after)        # ValueError names the accepted values
    flow = check_flow(flow, phase, dry_run)     # ValueError: unknown flow / remediation with stop_after or dry_run
    ov: dict = dict(overrides or {})
    if kb is False:
        ov = _merge(ov, {"kb": NO_KB_OVERRIDES})
    cfg = load_config(*configs, overrides=ov or None, repo_root=Path(repo_root) if repo_root else None)

    client: Any = None
    if llm is not None:
        from sbmig.fix.client import RecordingClient
        from sbmig.llm import wrap_llm

        client = wrap_llm(llm, cfg)
        if cfg.llm.record_dir and not isinstance(client, RecordingClient):
            client = RecordingClient(client, cfg.resolve_path(cfg.llm.record_dir))
    kb_obj: Any = None
    if kb is False:
        kb_obj = NullKnowledgeBase()
    elif kb is not None and kb is not True:
        kb_obj = kb

    ws = Workspace.from_input(input, out, run_id=run_id)
    extra = dict(components or {})
    extra.setdefault("source", input)
    orch = Orchestrator(cfg, ws, llm=client, fixer=fixer, kb=kb_obj, **extra)
    return orch.run(stop_after=phase, dry_run=dry_run, **({"flow": flow} if flow != "full" else {}))


def exit_code(report: "RunReport") -> int:
    """RunReport를 CLI 종료 코드로 바꾼다.
    0은 최종 빌드 성공에 차단 없음, 2는 차단이나 미해결 오류가 남은 채 끝남, 3은 중단이다.

    0 = final build green and nothing blocked, 2 = finished with blocked/unresolved errors, 3 = aborted."""
    from sbmig.cli import run_exit_code

    return run_exit_code(report)


__all__ = ["migrate", "exit_code", "NO_KB_OVERRIDES"]
