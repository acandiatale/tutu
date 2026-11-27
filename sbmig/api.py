"""사내 코드나 에이전트 프레임워크에서 sbmig를 호출할 때 쓰는 진입점 모듈.
`sbmig run`과 같은 파이프라인을 실행한다.

The entry point is ``migrate(...)`` (docs/ADAPTERS.md). Company integrations depend on the signature of
`sbmig.api.migrate(...)`: never change it. Imports happen inside the functions, so `import sbmig.api` stays
light.

    from sbmig.api import migrate, exit_code
    report = migrate("path/to/project", configs=["config/artifacts.yml"], llm=MyGatewayAdapter())
    print(report.success, exit_code(report))
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence, Union

if TYPE_CHECKING:  # pragma: no cover
    from sbmig.core.interfaces import KnowledgeBaseLike, LLMAdapterLike, LLMClientLike, ScriptFixerLike
    from sbmig.core.models import RunReport

# `kb=False`일 때 덧붙이는 kb 설정. 설정에는 KB를 끄는 스위치가 없어서 비어 있다. KB는 NullKnowledgeBase로 끈다
# 저장된 스크립트를 재사용하지 않고 KB에 기록하지도 않는다. 실행 디렉토리의 errors.jsonl, errors.md는 그대로 쓴다
NO_KB_OVERRIDES: dict = {}
_NO_KB_OVERRIDES = NO_KB_OVERRIDES          # 이전 이름으로 import하는 코드를 위해 남겨 둔다


def _merge(base: dict, extra: Mapping) -> dict:
    """dict를 안쪽까지 합친다. overrides에 kb 설정을 더할 때 쓴다."""
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
    framework_mode: Optional[str] = None,
) -> "RunReport":
    """프로젝트 하나(디렉토리나 zip)를 `sbmig run`과 같은 파이프라인으로 이관하고 RunReport를 반환한다.
    - ``flow="remediation"``이면 이관 없이 취약점 조치만 한다
    - ``llm``에는 회사 게이트웨이 어댑터나 클라이언트를 줄 수 있다. ``fixer``에는 사내 수정기(propose_script)를 줄 수 있다
    - ``framework_mode``는 사내 프레임워크 프로젝트의 처리 방법(reduced, convert)이다. None이면 ``framework.mode``를 쓴다
    - ``stop_after``, ``flow``, ``framework_mode`` 값이 잘못되었으면 ValueError를 던진다
    - CLI 종료 코드는 ``exit_code(report)``로 얻는다

    llm        an LLMClientLike (message-based) or an LLMAdapterLike (`complete(request)`, wrapped in
               AdapterClient automatically). None → built from config (`llm.provider`, incl. plugins).
               Recorded to `llm.record_dir` when that is set.
    fixer      a fixer with `propose_script(ws, cluster, ctx) -> ScriptProposal` (ScriptFixerLike) replacing
               LLMFixer (takes precedence over `fix.plugin`).
    kb         None/True → the file knowledge base; False → no KB (a NullKnowledgeBase); an object → used as is.
    stop_after intake|analyze|plan|transform|restructure.
    components advanced: extra Orchestrator collaborators (build_runner, rewrite_runner, restructurer, …).
    flow       "full" (default: the migration) | "remediation" (no migration: the vulnerability remediation of the
               project as uploaded; not with stop_after / dry_run — ValueError).
    framework_mode "reduced" | "convert" | None (= `framework.mode`); ignored by the remediation flow."""
    from sbmig.core.config import load_config
    from sbmig.core.workspace import Workspace
    from sbmig.loop import NullKnowledgeBase, Orchestrator, check_flow, parse_stop_after

    phase = parse_stop_after(stop_after)        # 잘못된 값이면 허용 값을 담은 ValueError를 던진다
    flow = check_flow(flow, phase, dry_run)     # 모르는 flow, stop_after나 dry_run과 함께 준 remediation은 ValueError
    if framework_mode is not None and not str(framework_mode).strip():
        framework_mode = None                   # 빈 값은 주지 않은 것과 같다(framework.mode를 쓴다)
    if framework_mode is not None:
        from sbmig.loop.convert import parse_framework_mode
        framework_mode = parse_framework_mode(framework_mode)     # 모르는 값은 ValueError
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

    # 올린 ZIP의 풀기 한도는 설정(intake.max_*)을 따른다. 넘으면 run()이 접수 거절로 끝낸다
    from sbmig.core.workspace import ZipLimits
    ws = Workspace.from_input(input, out, run_id=run_id, limits=ZipLimits.from_config(cfg.intake))
    extra = dict(components or {})
    extra.setdefault("source", input)
    orch = Orchestrator(cfg, ws, llm=client, fixer=fixer, kb=kb_obj, **extra)
    run_kw: dict = {"flow": flow} if flow != "full" else {}
    if framework_mode is not None:
        run_kw["framework_mode"] = framework_mode
    return orch.run(stop_after=phase, dry_run=dry_run, **run_kw)


def exit_code(report: "RunReport") -> int:
    """RunReport를 CLI 종료 코드로 바꾼다.
    0은 최종 빌드가 성공하고 차단이 없을 때다. 2는 차단됐거나 해결하지 못한 오류가 남았을 때, 3은 중단이다."""
    from sbmig.cli import run_exit_code

    return run_exit_code(report)


__all__ = ["migrate", "exit_code", "NO_KB_OVERRIDES"]
