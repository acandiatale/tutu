"""에이전트 하나가 프로젝트 여러 개를 하나씩 차례로 이관하는 일괄 실행(플릿) 패키지.
모든 프로젝트가 파일 KB 하나를 함께 쓰므로 앞 프로젝트가 남긴 수정 스크립트를 다음 프로젝트가 재사용한다.
``sbmig/cli.py``가 ``fleet_cli``를 ``sbmig fleet`` 명령으로 등록하고, :class:`FleetRunner`가 프로젝트마다
``sbmig run``과 같은 오케스트레이터를 실행한다.
- 진입점: ``load_manifest``, ``FleetRunner.run``, ``write_summaries``,
  ``sbmig.fleet.preflight``, ``sbmig.fleet.cleanup``
- 동시에 실행하면 앞 프로젝트가 KB에 남긴 결과를 다음 프로젝트가 보지 못하므로 병렬로 실행하지 않는다

sbmig.fleet — one agent, N projects, strictly sequential, one shared KB (see docs/FLEET.md).

Public API:
    load_manifest(path) -> list[FleetProject]
    FleetRunner(cfg, manifest_path, out_dir, *, run_project=None, kb=None, preflight=None, disk_check=None,
                sleep=time.sleep).run() -> FleetState
    write_summaries(state, fleet_dir, cfg, kb)
    fleet_cli                       typer sub-app, mounted by sbmig/cli.py as `sbmig fleet`
    sbmig.fleet.preflight           run_preflight(cfg, out_dir, echo=) -> [Check]  (Java / Maven / settings /
                                    repository / LLM / disk, round 10)
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
    """``import sbmig.fleet``이 typer와 CLI 모듈까지 불러오지 않도록 ``fleet_cli``만 필요할 때 import한다."""
    if name == "fleet_cli":
        from .cli import fleet_cli

        return fleet_cli
    raise AttributeError(name)
