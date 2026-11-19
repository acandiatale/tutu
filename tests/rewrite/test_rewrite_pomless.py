"""v2.1z-ax: the OpenRewrite run goes through `pomless.run_with_stubs` — a run that stops on a library whose
repository has the jar but no pom gets a minimal pom in the (tmp) local repository and the same plugin call again."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import sbmig.build.mvn as mvn_mod
from sbmig.core.models import RecipeRef
from sbmig.core.workspace import Workspace
from sbmig.rewrite.runner import OpenRewriteRunner

DIRECT = (Path(__file__).parents[1] / "build" / "logs" / "real-pomless-direct.log").read_text(encoding="utf-8")
UP21 = "org.openrewrite.java.migrate.UpgradeToJava21"


class SeqPopen:
    outputs: list[tuple[str, int]] = []
    calls: list[list[str]] = []

    def __init__(self, cmd, **kw):
        SeqPopen.calls.append(list(cmd))
        text, self._code = SeqPopen.outputs.pop(0) if SeqPopen.outputs else ("[INFO] BUILD SUCCESS\n", 0)
        self.stdout = io.BytesIO(text.encode("utf-8"))
        self.returncode = None
        self.pid = -424246

    def wait(self, timeout=None):
        self.returncode = self._code
        return self._code


class FakeRepo:
    def __init__(self, cfg, project_root=None):
        pass

    def repository_urls(self):
        return [("https://nexus.invalid/releases/", None, "t")]

    def file_answers(self, g, a, v, ext):
        return [("https://nexus.invalid/releases/", "missing" if ext == "pom" else "ok", "")]


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pom.xml").write_text("<project/>\n")
    w = Workspace(root, tmp_path / "run", "t-run")
    w.git_init()
    return w


def test_rewrite_run_retries_after_the_stub(ws, cfg, tmp_path, monkeypatch):
    cfg.openrewrite.recipe_artifact_versions = {"org.openrewrite.recipe:rewrite-migrate-java": "3.42.0"}
    cfg.build.local_repo = str(tmp_path / "m2")
    SeqPopen.outputs, SeqPopen.calls = [(DIRECT, 1), ("[INFO] BUILD SUCCESS\n", 0)], []
    monkeypatch.setattr(mvn_mod, "_popen", SeqPopen)
    monkeypatch.setattr("sbmig.build.repo_versions.RepositoryLookup", FakeRepo)
    res = OpenRewriteRunner(ws, cfg).run([RecipeRef(id=UP21)])
    assert res.ok and len(SeqPopen.calls) == 2 and SeqPopen.calls[0] == SeqPopen.calls[1]
    assert res.log_path.endswith("-pomless1.log")
    assert (tmp_path / "m2" / "com" / "example" / "pomless" / "1.0" / "pomless-1.0.pom").is_file()
    ev = [json.loads(x)["event"] for x in ws.journal_path.read_text().splitlines()]
    assert "pomless_stub" in ev
