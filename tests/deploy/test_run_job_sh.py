"""2026-11-05 (k8s Job dispatch, shared volume): run-job.sh in the shared-volume mode — the web pod's zip path, the shared
out dir and run id, no git, `job-status.json` at the end. Runs the real script with a fake `python`."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "docker" / "run-job.sh"
FAKE_PY = """#!/usr/bin/env bash
# fake `python -m sbmig.cli run <zip> --config … --out <out> --run-id <id> [--stop-after X]`: writes what a run writes
args=("$@"); out=""; rid=""; stop=""
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    --out) out="${args[$((i+1))]}";; --run-id) rid="${args[$((i+1))]}";; --stop-after) stop="${args[$((i+1))]}";;
  esac
done
mkdir -p "$out/$rid"
printf 'args=%s\n' "$*" > "$out/$rid/sbmig.log"
printf 'stop=%s\n' "$stop" >> "$out/$rid/sbmig.log"
printf 'SBMIG-RESULT ok fake\n' >> "$out/$rid/sbmig.log"
printf '# report\n' > "$out/$rid/report.md"
exit "${FAKE_RC:-0}"
"""


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_shared_volume_mode_runs_in_the_web_pods_run_dir(tmp_path: Path):
    fake = tmp_path / "bin" / "python"
    fake.parent.mkdir()
    fake.write_text(FAKE_PY, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    data = tmp_path / "data"
    run_dir = data / "out" / "web-1"
    run_dir.mkdir(parents=True)
    (run_dir / "source.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    env = {**os.environ, "SBMIG_PYTHON": str(fake), "SBMIG_WORKSPACE": str(tmp_path / "ws"), "SBMIG_APP_DIR": str(tmp_path),
           "SBMIG_SOURCE_ZIP": str(run_dir / "source.zip"), "SBMIG_OUT": str(data / "out"), "SBMIG_RUN_ID": "web-1",
           "SBMIG_KB_DIR": str(data / "kb-v2"), "SBMIG_CONFIG": "/app/config/artifacts.yml", "SBMIG_STOP_AFTER": "plan"}
    for k in ("SBMIG_SOURCE_GIT_URL", "SBMIG_SOURCE_ZIP_URL", "SBMIG_KB_GIT_URL", "SBMIG_RESULT_GIT_URL", "RESULT_PUSH_CMD"):
        env.pop(k, None)
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    log = (run_dir / "sbmig.log").read_text(encoding="utf-8")
    assert f"--out {data / 'out'}" in log and "--run-id web-1" in log and "stop=plan" in log
    assert "--config /app/config/artifacts.yml" in log and "paths.yml" in log
    paths_yml = (tmp_path / "ws" / "paths.yml").read_text(encoding="utf-8")
    assert f"dir: {data / 'kb-v2'}" in paths_yml and "llm-records" in paths_yml and "web-1/llm-records" in paths_yml
    status = json.loads((run_dir / "job-status.json").read_text(encoding="utf-8"))
    assert status["exit_code"] == 0 and status["finished_at"].endswith("Z")
    assert "결과는 공유 볼륨" in r.stdout and "SBMIG-RESULT ok fake" in r.stdout
    # an aborted run (sbmig exit 3) → Job exit 3, status written
    env["FAKE_RC"] = "3"
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 3 and json.loads((run_dir / "job-status.json").read_text())["exit_code"] == 3
