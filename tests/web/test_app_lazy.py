"""v2.1z-ac (kind test, 2026-11-09): importing `sbmig.web.app` must not build an app. `sbmig serve` imports the module and
builds its own app from its arguments; the module-level `app` (plain `uvicorn sbmig.web.app:app`) is created on first access only,
so a k8s-job startup validation can never fail on the import-time app with the wrong output directory."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _run(code: str, **env: str) -> subprocess.CompletedProcess:
    full = {**os.environ, "SBMIG_WEB_TOKEN": "", "SBMIG_WEB_OPEN": "1", **env}
    full.pop("KUBECONFIG", None)
    return subprocess.run([sys.executable, "-c", code], check=False, capture_output=True, text=True, env=full, timeout=120)


def test_importing_the_app_module_builds_nothing_even_in_job_mode(tmp_path: Path):
    r = _run("import sbmig.web.app as m; print(m._module_app is None)",
             SBMIG_WEB_MODE="k8s-job", KUBERNETES_SERVICE_HOST="10.0.0.1", SBMIG_JOB_TEMPLATE=str(tmp_path / "nope.yaml"),
             SBMIG_WEB_OUT=str(tmp_path / "out"))
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip() == "True"


def test_the_module_level_app_is_built_on_first_access(tmp_path: Path):
    r = _run("import sbmig.web.app as m; a = m.app; print(type(a).__name__, a is m.app)", SBMIG_WEB_MODE="in-process",
             SBMIG_WEB_OUT=str(tmp_path / "out"))
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip() == "FastAPI True"
