"""sbmig.fleet — 일괄 실행: 에이전트 하나가 프로젝트 N 개를 엄격히 하나씩 차례로 이관하고,
지식베이스(파일) 하나를 함께 쓴다. 앞 프로젝트가 KB 에 남긴 수정 스크립트를 다음 프로젝트가 먼저 재사용한다.
흐름상 위치: ``sbmig/cli.py`` 가 ``fleet_cli`` 를 ``sbmig fleet`` 으로 붙이고,
:class:`FleetRunner` 가 프로젝트마다 ``sbmig run`` 과 같은 오케스트레이터를 돌린다
(접수 → 분석 → 계획 → 변환 → 재구성 → 구조 검토 → 검증 빌드 → 수정 루프). 핵심 진입점: ``load_manifest``
(목록), ``FleetRunner.run`` (실행·재개), ``write_summaries`` (요약), ``sbmig.fleet.preflight`` (인프라 사전
점검), ``sbmig.fleet.cleanup`` (디스크 정리, ``sbmig clean``). 병렬 실행은 하지 않는다: KB 가 파일 하나라
동시에 돌리면 k 번째의 학습을 k+1 번째가 못 본다.

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
    """``fleet_cli`` 만 지연 import 한다: ``import sbmig.fleet`` 가 typer·CLI 모듈까지 끌어오지 않게."""
    if name == "fleet_cli":
        from .cli import fleet_cli

        return fleet_cli
    raise AttributeError(name)
