import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from sbmig.api import exit_code, migrate
from sbmig.core.interfaces import LLMRequest, LLMResponse
from sbmig.core.models import RunReport
from sbmig.llm import LLMAdapter

REPO = Path(__file__).resolve().parents[2]
PURE = REPO / "fixtures" / "projects" / "pure-java"
E2E = REPO / "config" / "artifacts.local-e2e.yml"


class Adapter(LLMAdapter):
    def __init__(self):
        self.requests: list[LLMRequest] = []

    def complete(self, request):
        self.requests.append(request)
        return LLMResponse(content="[]", model="gw")


# a build runner that is never reached before the plan phase: keeps the test free of Maven
NO_MAVEN = {"build_runner": SimpleNamespace(run=lambda *a, **k: pytest.fail("build must not run"))}


@pytest.fixture()
def pure(tmp_path):
    if not PURE.exists():
        pytest.skip("pure-java fixture missing")
    dst = tmp_path / "pure-java"
    shutil.copytree(PURE, dst)
    return dst


def test_migrate_stop_after_plan_with_adapter(pure, tmp_path):
    adapter = Adapter()
    rec = tmp_path / "rec"
    report = migrate(pure, configs=[E2E], out=tmp_path / "out", run_id="api-1", llm=adapter, kb=False,
                     stop_after="plan", repo_root=REPO, components=NO_MAVEN,
                     overrides={"plan": {"advisor": "llm"}, "llm": {"record_dir": str(rec)}})
    assert isinstance(report, RunReport)
    assert "stopped-after-plan" in report.flags and report.plan is not None
    assert exit_code(report) in (0, 2)
    assert (tmp_path / "out" / "api-1").is_dir()
    assert adapter.requests, "advisor did not call the adapter"
    if adapter.requests:                       # the advisor called our adapter through AdapterClient → recorded
        recs = [json.loads(p.read_text()) for p in sorted(rec.glob("llm-*.json"))]
        assert recs and recs[0]["response"] == "[]"


def test_migrate_rejects_bad_stop_after(pure, tmp_path):
    with pytest.raises(ValueError, match="stop_after"):
        migrate(pure, out=tmp_path / "out", stop_after="nope")


def test_exit_code_aborted():
    r = RunReport(run_id="x", project_name="p", case_type="unknown", started_at="2026-01-01T00:00:00Z",
                  flags=["aborted"])
    assert exit_code(r) == 3


@pytest.mark.integration
def test_migrate_dry_run_real_maven(pure, tmp_path):
    if not shutil.which("mvn"):
        pytest.skip("mvn not on PATH")
    report = migrate(pure, configs=[E2E], out=tmp_path / "out", kb=False, stop_after="plan", dry_run=True,
                     repo_root=REPO)
    assert "dry-run" in report.flags and "stopped-after-plan" in report.flags
