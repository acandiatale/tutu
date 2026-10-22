"""rewrite-maven-plugin "There were problems parsing …" → RewriteResult.parse_problems + journal count."""
from __future__ import annotations

import io
import json

import pytest

import sbmig.build.mvn as mvn_mod
from sbmig.core.models import RecipeRef
from sbmig.core.workspace import Workspace
from sbmig.rewrite import OpenRewriteRunner
from sbmig.rewrite.runner import PARSE_PROBLEMS_UNKNOWN

UP21 = "org.openrewrite.java.migrate.UpgradeToJava21"
SUMMARY = "[WARNING] There were problems parsing some source files, run with --errors to see full stack traces"


def test_posix_relative_and_absolute_paths_dedup_order():
    text = "\n".join([
        "[INFO] Running recipe(s)...",
        "[WARNING] There were problems parsing src/main/java/com/x/B.java",
        "[WARNING] There were problems parsing /work/proj/src/main/java/com/x/A.java (see the log)",
        "[WARNING] There were problems parsing src/main/java/com/x/B.java",
        SUMMARY,
        "[INFO] BUILD SUCCESS",
    ])
    assert OpenRewriteRunner.parse_problems(text, root="/work/proj") == [
        "src/main/java/com/x/B.java", "src/main/java/com/x/A.java"]


def test_windows_paths_are_normalized_and_made_relative():
    text = ("[WARNING] There were problems parsing C:\\work\\proj\\src\\main\\java\\A.java\n"
            "[WARNING] There were problems parsing src\\main\\java\\B.java:\n"
            "[WARNING] There were problems parsing D:\\elsewhere\\C.java\n")
    assert OpenRewriteRunner.parse_problems(text, root="c:\\work\\proj") == [
        "src/main/java/A.java", "src/main/java/B.java", "D:/elsewhere/C.java"]


def test_summary_only_and_none():
    assert OpenRewriteRunner.parse_problems(SUMMARY + "\n") == [PARSE_PROBLEMS_UNKNOWN]
    assert OpenRewriteRunner.parse_problems("[WARNING] There were problems parsing some source files\n") == [
        PARSE_PROBLEMS_UNKNOWN]
    assert OpenRewriteRunner.parse_problems("[INFO] BUILD SUCCESS\n") == []
    assert OpenRewriteRunner.parse_problems("") == []


def test_absolute_path_outside_root_is_kept():
    text = "[WARNING] There were problems parsing /other/X.java\n"
    assert OpenRewriteRunner.parse_problems(text, root="/work/proj") == ["/other/X.java"]


# ------------------------------------------------------------------------------------------- through _run_once
class _FakePopen:
    output = b""

    def __init__(self, cmd, cwd=None, env=None, **kw):
        self.stdout = io.BytesIO(_FakePopen.output)
        self.returncode = None
        self.pid = -424243

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    root = tmp_path / "proj"
    (root / "svc").mkdir(parents=True)
    (root / "pom.xml").write_text("<project/>\n")
    (root / "svc" / "pom.xml").write_text("<project/>\n")
    w = Workspace(root, tmp_path / "run", "t-run")
    w.git_init()
    return w


@pytest.fixture()
def rcfg(cfg):
    cfg.openrewrite.recipe_artifact_versions = {"org.openrewrite.recipe:rewrite-migrate-java": "3.42.0"}
    return cfg


def _events(ws):
    return [json.loads(x) for x in ws.journal_path.read_text().splitlines() if '"rewrite"' in x]


def test_run_fills_parse_problems_with_module_prefix(ws, rcfg, monkeypatch):
    monkeypatch.setattr(mvn_mod, "_popen", _FakePopen)
    _FakePopen.output = (
        "[WARNING] There were problems parsing src/main/java/A.java\n"
        f"[WARNING] There were problems parsing {ws.root / 'svc' / 'src' / 'main' / 'java' / 'B.java'}\n"
        f"[WARNING] There were problems parsing {ws.root / 'other' / 'C.java'}\n"
        f"{SUMMARY}\n[INFO] BUILD SUCCESS\n").encode()
    res = OpenRewriteRunner(ws, rcfg).run([RecipeRef(id=UP21)], module="svc")
    assert res.ok
    assert res.parse_problems == ["svc/src/main/java/A.java", "svc/src/main/java/B.java", "other/C.java"]
    assert _events(ws)[-1]["parse_problems"] == 3


def test_run_without_problems_journals_zero(ws, rcfg, monkeypatch):
    monkeypatch.setattr(mvn_mod, "_popen", _FakePopen)
    _FakePopen.output = b"[INFO] BUILD SUCCESS\n"
    res = OpenRewriteRunner(ws, rcfg).run([RecipeRef(id=UP21)])
    assert res.parse_problems == []
    assert _events(ws)[-1]["parse_problems"] == 0


def test_run_summary_only(ws, rcfg, monkeypatch):
    monkeypatch.setattr(mvn_mod, "_popen", _FakePopen)
    _FakePopen.output = f"{SUMMARY}\n[INFO] BUILD SUCCESS\n".encode()
    res = OpenRewriteRunner(ws, rcfg).run([RecipeRef(id=UP21)], module="svc")
    assert res.parse_problems == [PARSE_PROBLEMS_UNKNOWN]
