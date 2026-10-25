"""sbmig.fleet — one agent, N projects, strictly sequential, one shared KB (see docs/FLEET.md).

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
    if name == "fleet_cli":
        from .cli import fleet_cli

        return fleet_cli
    raise AttributeError(name)
