"""에이전트 하나가 여러 프로젝트를 하나씩 차례로 이관하는 일괄 실행(플릿) 패키지.
앞 프로젝트가 KB에 남긴 수정 스크립트를 다음 프로젝트가 재사용한다.

Operator doc: docs/FLEET.md.

All projects share one file KB. `sbmig/cli.py` mounts ``fleet_cli`` as ``sbmig fleet``; :class:`FleetRunner`
runs the same orchestrator as ``sbmig run`` for each project. Projects never run in parallel: a later
project must see what an earlier one left in the KB.

Public API:
    load_manifest(path) -> list[FleetProject]
    FleetRunner(cfg, manifest_path, out_dir, *, run_project=None, kb=None, preflight=None, disk_check=None,
                sleep=time.sleep).run() -> FleetState
    write_summaries(state, fleet_dir, cfg, kb)
    fleet_cli                       typer sub-app, mounted by sbmig/cli.py as `sbmig fleet`
    sbmig.fleet.preflight           run_preflight(cfg, out_dir, echo=) -> [Check]  (Java / Maven / settings /
                                    repository / LLM / disk)
    sbmig.fleet.cleanup             cleanup_run(run_dir, mode), plan_clean(...) / execute(items)  (`sbmig clean`)
"""

from .manifest import FleetProject, ManifestError, load_manifest
from .runner import FleetRunner, fleet_exit_code
from .summary import write_summaries

__all__ = [
    "FleetProject",
    "ManifestError",
    "load_manifest",
    "FleetRunner",
    "fleet_exit_code",
    "write_summaries",
    "fleet_cli",
]


def __getattr__(name: str):
    """``fleet_cli``는 처음 쓸 때 import한다. ``import sbmig.fleet``만으로 typer와 CLI 모듈까지 읽지 않게 한다."""
    if name == "fleet_cli":
        from .cli import fleet_cli

        return fleet_cli
    raise AttributeError(name)
