from __future__ import annotations

import pytest

from sbmig.core.workspace import Workspace
from sbmig.triage import Triager

from .fakes import FakeKB, FakeRewriteRunner, make_profile, make_project


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    w = Workspace.from_input(make_project(tmp_path), tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


@pytest.fixture()
def profile():
    return make_profile()


@pytest.fixture()
def triager(cfg):
    return Triager(cfg)


@pytest.fixture()
def kb():
    return FakeKB()


@pytest.fixture()
def rewrite(ws):
    return FakeRewriteRunner(ws)


from tests.conftest import OfflineRepositoryLookup   # the root autouse guard patches the lookup


@pytest.fixture()
def offline_repository():
    """The offline stand-in the root conftest installs (autouse); here for tests that name it."""
    return OfflineRepositoryLookup
