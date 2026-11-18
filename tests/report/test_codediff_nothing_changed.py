"""codediff: a run that changed nothing writes no code-change record (review finding R4).

A run refused or aborted at intake (Ant refusal, settings-not-found) used to write an artifact with 0 files, and the
page showed an empty 「코드 변경」 block. Now nothing is written, an existing record of an earlier attempt in the same
folder is removed, and the journal says `code_diff_skipped` with the reason."""
from __future__ import annotations

from sbmig.core.workspace import Workspace
from sbmig.report.codediff import write_code_diff

from .test_codediff import events, load, make_ws, put


def _earlier_record(ws: Workspace) -> None:
    d = ws.run_dir / "codediff"
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.json").write_text('{"files": [{"path": "old"}]}', encoding="utf-8")


def test_a_run_that_changed_nothing_writes_no_record(tmp_path, cfg):
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n", "build.xml": "<project/>\n"})
    _earlier_record(ws)
    assert write_code_diff(ws, cfg) is None
    assert not (ws.run_dir / "codediff").exists()
    assert events(ws, "code_diff_skipped")[-1]["reason"] == "the run changed no file"
    assert events(ws, "code_diff") == []


def test_only_build_leftovers_count_as_nothing_changed(tmp_path, cfg):
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n"})
    put(ws, "dependency-reduced-pom.xml", "<project/>\n")
    assert write_code_diff(ws, cfg) is None and not (ws.run_dir / "codediff").exists()


def test_switched_off_removes_an_earlier_record(tmp_path, cfg):
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n"})
    _earlier_record(ws)
    cfg.report.code_diff = False
    assert write_code_diff(ws, cfg) is None and not (ws.run_dir / "codediff").exists()


def test_no_git_repository_removes_an_earlier_record(tmp_path, cfg):
    src = tmp_path / "p"
    src.mkdir()
    (src / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    ws = Workspace.from_input(src, tmp_path / "out", run_id="t-nogit")
    _earlier_record(ws)
    assert write_code_diff(ws, cfg) is None and not (ws.run_dir / "codediff").exists()
    assert events(ws, "code_diff_skipped")[-1]["reason"] == "the project has no git repository"


def test_a_change_still_writes_the_record(tmp_path, cfg):
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n"})
    put(ws, "pom.xml", "<project><x/></project>\n")
    summary = write_code_diff(ws, cfg)
    index, _ = load(ws)
    assert summary["files"] == 1 and [f["path"] for f in index["files"]] == ["pom.xml"]
