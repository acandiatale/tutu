"""2026-11-09: a developer shell may export KUBECONFIG; the web tests decide cluster access themselves (a kubeconfig now
keeps `k8s-job` from degrading to `local-job`), so every web test starts without one and sets it explicitly when it wants it."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_developer_kubeconfig(monkeypatch):
    monkeypatch.delenv("KUBECONFIG", raising=False)
