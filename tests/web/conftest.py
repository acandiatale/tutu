"""2026-11-09: a developer shell may export KUBECONFIG; the web tests decide cluster access themselves (a kubeconfig now
keeps `k8s-job` from degrading to `local-job`), so every web test starts without one and sets it explicitly when it wants it."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_developer_kubeconfig(monkeypatch):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    # the NFS grace for a late job-status.json (90 s in production) would make every "pod ended without a status file" test wait
    monkeypatch.setenv("SBMIG_JOB_STATUS_GRACE_S", "0.2")
    # 2026-11-10: Main keeps a run's turn until its Job ended; a pod still alive this long after its job-status.json is deleted
    # (120 s in production)
    monkeypatch.setenv("SBMIG_JOB_EXIT_GRACE_S", "0.3")
