"""Regression tests for the fourth review of the remediation (and its fix loop).

The review's reproductions (`out/scratch/review-bm/test_repro_bm.py`, which asserted the faulty behaviour) turned
around, plus the items without a reproduction:
1. a result ZIP is frozen under a temporary name first; a freeze that fails leaves the previous delivery intact and
   truthful, the run ends FAILED saying so; a kill at any point of the swap leaves (or a resume repairs) a state that
   names a ZIP which exists with that sha256;
2. a candidate that cannot be put back after a PASSING build: the build is recorded and the report does not blame a
   failed build; every 「결과 파일 — 없음」 sentence is true for its state;
3. a failure while preparing the fix loop's folder is "not started" and the intact failed candidate is delivered;
4. the usage of a loop an exception escaped from comes from the loop's own journal (unknown tokens: no cost);
5. the executable bit is part of the expected table and put back; an unreadable file is made readable once;
6. a loop that made no attempt is worded 「빌드 오류 자동 수정을 하지 못했습니다(<사유>).」;
7. a kill during the report node: the next entry writes the report (and the combined fix.patch);
8. `intake.table_hash` of `intake.file_table` equals `intake.tree_hash` for a non-regular file too.
No Maven, no LLM.
"""
from __future__ import annotations

import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from sbmig.remediations import chain, graph, i18n, report
from sbmig.remediations import fixer as fixer_mod
from sbmig.remediations import intake as intake_mod
from sbmig.remediations.fixer import fix_usage, fix_usage_rows
from sbmig.remediations.models import (
    FAILED,
    SUCCEEDED,
    FixResult,
    RemediationState,
    RunPaths,
    read_json,
    sha256_file,
)

from .fakes import FakeMaven, Recorder, make_cfg, make_components, make_run
from .test_fix_loop_review2 import Killed
from .test_fix_loop_review3 import _edit_on_build, _stopped_at_build, _unwritable
from .test_fixer import broken_build, make_tree, rcfg  # noqa: F401 - rcfg is a fixture
from .test_fixer import run as run_loop
from .test_graph import POM2, _two_round_components, one_fix_components, run
from .test_graph_fix_loop import APP, FakeFixer, zip_files

NO_ROOT = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any file")


def _saved(rec: Recorder) -> RemediationState:
    return RemediationState.from_dict(rec.saves[-1])


def _result_line(md: str) -> str:
    """The record's 「**N. 결과 파일** — …」 line (the summary has its own short line)."""
    return next(ln for ln in md.splitlines() if ln.startswith("**") and "결과 파일**" in ln)


def _consistent(paths: RunPaths, st: RemediationState) -> None:
    """The state's delivery names a ZIP that exists with that sha256, and delivery.json says the same."""
    if st.delivery is None:
        return
    assert paths.delivery_zip.is_file()
    assert sha256_file(paths.delivery_zip) == st.delivery.sha256
    assert read_json(paths.delivery_json)["sha256"] == st.delivery.sha256


def _no_swap_left(paths: RunPaths) -> None:
    folder = paths.delivery_zip.parent
    assert not (folder / graph.NEXT_JSON).exists() and not (folder / graph.NEXT_ZIP).exists()
    assert not list(folder.glob(f"*{graph.NEXT_ZIP}*"))


def _failing_freeze(comps, at: int):
    real = comps.intake.freeze_delivery
    n = {"k": 0}

    def freeze(tree, out, r, pl):
        n["k"] += 1
        if n["k"] == at:
            raise OSError(28, "No space left on device")
        return real(tree, out, r, pl)
    comps.intake.freeze_delivery = freeze


# ------------------------------------------------------------------------------------------------ 1. the freeze
def test_a_failed_freeze_of_the_failed_candidate_keeps_the_verified_delivery(tmp_path):
    state, paths = make_run(tmp_path, {"pom.xml": POM2})
    comps = _two_round_components()                  # round 2 build fails, failed_candidate include
    _failing_freeze(comps, 2)                        # the failed round-2 candidate
    out, rec = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "internal_error"
    assert out.message.startswith("the result ZIP of round 2 could not be written (OSError: [Errno 28] No space left "
                                  "on device); delivery/result.zip is still the result ZIP of round 1")
    assert out.delivery is not None and out.delivery.round == 1 and out.delivery.build_ok and out.delivery.scanned
    _consistent(paths, out)
    _consistent(paths, _saved(rec))
    assert not graph.verified_zip_path(paths, 1).exists()          # nothing was moved aside
    assert b"<version>2.0</version>" in zip_files(paths.delivery_zip)["pom.xml"]
    _no_swap_left(paths)
    md = paths.report_md.read_text(encoding="utf-8")
    assert "`delivery/result.zip`" in _result_line(md) and "빌드 성공" in _result_line(md)
    assert "라운드 2의 결과 ZIP을 쓰지 못했습니다(OSError). 결과 ZIP은 라운드 1의 결과 그대로입니다." in md
    assert next(e for e in rec.events if e.get("event") == "delivery_not_written")["kept_round"] == 1


def test_a_failed_freeze_in_the_freeze_node_keeps_round_one(tmp_path):
    state, paths = make_run(tmp_path, {"pom.xml": POM2})
    comps = _two_round_components(fail_round_two=False)
    _failing_freeze(comps, 2)                        # round 2's committed tree
    out, _rec = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "internal_error"
    assert "delivery/result.zip is still the result ZIP of round 1" in out.message
    assert out.delivery.round == 1 and out.delivery.scanned
    _consistent(paths, out)
    _no_swap_left(paths)


def test_a_failed_first_freeze_leaves_no_delivery_and_says_so(tmp_path):
    state, paths = make_run(tmp_path)
    comps = one_fix_components()
    _failing_freeze(comps, 1)
    out, _rec = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "internal_error" and out.delivery is None
    assert out.message.startswith("the result ZIP of round 1 could not be written (OSError: ")
    assert out.message.endswith("no result ZIP exists; the original is preserved as original/source.zip")
    assert not paths.delivery_zip.exists() and not paths.delivery_json.exists()
    _no_swap_left(paths)
    md = paths.report_md.read_text(encoding="utf-8")
    assert _result_line(md).endswith("없음 — 라운드 1의 결과 ZIP을 쓰지 못했습니다. 원본은 그대로입니다.")


def _kill_at(monkeypatch, paths: RunPaths, point: str, armed: dict) -> None:
    """Kill the pod at one point of `install_delivery` once `armed["on"]` is set."""
    folder = paths.delivery_zip.parent
    real_json, real_copy, real_unlink = graph.write_json_atomic, graph._copy_atomic, graph._unlink_quiet
    real_replace = os.replace
    state = {"replaced": False}

    def json_spy(path, data):
        path = Path(path)
        if armed["on"] and point == "after_marker" and path.name == graph.NEXT_JSON:
            real_json(path, data)
            raise Killed()
        if armed["on"] and point == "after_delivery_json" and path == paths.delivery_json:
            real_json(path, data)
            raise Killed()
        return real_json(path, data)

    def copy_spy(src, dst):
        real_copy(src, dst)
        if armed["on"] and point == "after_verified_copy":
            raise Killed()

    def replace_spy(src, dst):
        real_replace(src, dst)
        if armed["on"] and Path(dst) == paths.delivery_zip:
            state["replaced"] = True
            if point == "after_replace":
                raise Killed()

    def unlink_spy(p):
        if armed["on"] and point == "after_save" and state["replaced"] and Path(p).name == graph.NEXT_JSON:
            raise Killed()
        return real_unlink(p)

    monkeypatch.setattr(graph, "write_json_atomic", json_spy)
    monkeypatch.setattr(graph, "_copy_atomic", copy_spy)
    monkeypatch.setattr(graph.os, "replace", replace_spy)
    monkeypatch.setattr(graph, "_unlink_quiet", unlink_spy)
    assert folder  # the folder is made by the run


@pytest.mark.parametrize("point", ["in_freeze", "after_verified_copy", "after_marker", "after_replace",
                                   "after_delivery_json", "after_save"])
def test_every_kill_point_of_the_swap_leaves_or_repairs_a_truthful_delivery(tmp_path, monkeypatch, point):
    state, paths = make_run(tmp_path, {"pom.xml": POM2})
    comps = _two_round_components()
    armed = {"on": False}
    real = comps.intake.freeze_delivery
    n = {"k": 0}

    def freeze(tree, out, r, pl):
        n["k"] += 1
        if n["k"] == 2:
            armed["on"] = True                       # the failed round-2 candidate's swap
            if point == "in_freeze":
                Path(out).write_bytes(b"partial")
                raise Killed()
        return real(tree, out, r, pl)
    comps.intake.freeze_delivery = freeze
    _kill_at(monkeypatch, paths, point, armed)
    rec = Recorder()
    with pytest.raises(Killed):
        run(state, paths, comps, rec=rec)
    monkeypatch.undo()
    st = _saved(rec)
    if point in ("in_freeze", "after_verified_copy", "after_marker", "after_save"):
        _consistent(paths, st)                       # what is on disk already agrees
    graph.recover_delivery(paths, st)
    _consistent(paths, st)
    _no_swap_left(paths)
    if point in ("after_replace", "after_delivery_json", "after_save"):
        assert st.delivery.round == 2 and not st.delivery.build_ok          # the swap is completed
    else:
        assert st.delivery.round == 1 and not graph.verified_zip_path(paths, 1).exists()   # rolled back
    # a resume repairs the same way on its own and ends like an uninterrupted run
    st2 = _saved(rec)
    rec2 = Recorder()
    out, _ = run(st2, paths, make_components(maven=FakeMaven(outcomes=[False]), plan=comps.plan, scan=comps.scan),
                 rec=rec2)
    assert out.status == FAILED and out.reason == "candidate_build_failed", out.message
    assert out.delivery.round == 2 and not out.delivery.build_ok
    _consistent(paths, out)
    _no_swap_left(paths)
    kept = graph.find_verified_zip(paths)
    assert kept is not None and b"<version>2.0</version>" in zip_files(kept)["pom.xml"]


def test_recover_delivery_without_a_marker_only_removes_temporary_files(tmp_path):
    state, paths = make_run(tmp_path)
    out, _ = run(state, paths, one_fix_components())
    assert out.status == SUCCEEDED
    folder = paths.delivery_zip.parent
    (folder / graph.NEXT_ZIP).write_bytes(b"x")
    (folder / f".{graph.NEXT_ZIP}.abc123").write_bytes(b"x")
    before = out.delivery.to_dict()
    assert graph.recover_delivery(paths, out) is False
    assert out.delivery.to_dict() == before
    _no_swap_left(paths)
    _consistent(paths, out)


# ------------------------------------------------------------------------------------------------ 2. not restored
def test_not_restored_after_a_passing_build_records_the_build_and_does_not_blame_it(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path)
    _unwritable(monkeypatch, APP)
    out, _rec = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, True],
                                                                     on_package=_edit_on_build(2))))
    assert out.status == FAILED and out.reason == "internal_error"
    assert out.rounds[0].build is not None and out.rounds[0].build.ok          # the passing build is recorded
    md = paths.report_md.read_text(encoding="utf-8")
    line = _result_line(md)
    assert line.endswith("없음 — 수정본을 빌드 뒤 원래 상태로 되돌리지 못해 결과 ZIP을 만들지 않았습니다. 원본은 그대로입니다.")
    assert "빌드에 실패한 수정본" not in md


@pytest.mark.parametrize("reason,message,want", [
    ("candidate_build_failed", "x", "빌드에 실패한 수정본은 결과 ZIP으로 만들지 않았습니다."),
    ("baseline_build_failed", "x", "조치 전 빌드가 실패해 검사와 조치를 진행하지 않았습니다."),
    ("text_only_violation", "x", "버전 표기 외의 변경이 발견된 수정본은 결과 ZIP으로 만들지 않았습니다."),
    ("resolution_mismatch", "x", "의존성 버전이 계획과 다르게 적용된 수정본은 결과 ZIP으로 만들지 않았습니다."),
    ("zip_hash_mismatch", "x", "검사한 파일과 가진 파일이 같은지 확인하지 못해 결과 ZIP을 만들지 않았습니다."),
    ("not_ready", "x", "조치를 시작하기 전에 멈춰 결과 ZIP을 만들지 않았습니다."),
    ("execution_boundary_refused", "x", "조치를 시작하기 전에 멈춰 결과 ZIP을 만들지 않았습니다."),
    ("internal_error", "RuntimeError: boom", "도구 내부 오류로 결과 ZIP을 만들지 못했습니다."),
    ("internal_error", graph.CANDIDATE_NOT_RESTORED.format(r=1, n=1, p=APP, t="PermissionError"),
     "수정본을 빌드 뒤 원래 상태로 되돌리지 못해 결과 ZIP을 만들지 않았습니다."),
    ("internal_error", graph.DELIVERY_NOT_WRITTEN.format(r=3, t="OSError", e="disk full", left=graph.DELIVERY_LEFT_NONE),
     "라운드 3의 결과 ZIP을 쓰지 못했습니다."),
    ("some_new_code", "x", "결과 ZIP을 만들지 않았습니다."),
])
def test_every_no_delivery_sentence_fits_its_state(tmp_path, reason, message, want):
    state, _paths = make_run(tmp_path)
    state.status, state.reason, state.message, state.delivery = FAILED, reason, message, None
    assert report._no_delivery_why(state) == f" — {want} 원본은 그대로입니다."


# ------------------------------------------------------------------------------------------------ 3. folder setup
@pytest.mark.parametrize("where", ["loop_file", "old_folder"])
def test_a_failing_fix_folder_setup_delivers_the_untouched_failed_candidate(tmp_path, monkeypatch, where):
    state, paths = make_run(tmp_path)
    work = paths.round_dir(1) / graph.FIX_DIR
    if where == "old_folder":
        work.mkdir(parents=True)
        (work / graph.LOOP_FILE).write_text(json.dumps({"loop": "rem-1-fix-r1-old", "round": 1}), encoding="utf-8")
    real_json, real_rm = graph.write_json_atomic, graph._safe_rmtree

    def json_spy(path, data):
        if where == "loop_file" and Path(path).name == graph.LOOP_FILE:
            raise OSError(30, "Read-only file system")
        return real_json(path, data)

    def rm_spy(p):
        if where == "old_folder" and Path(p) == work:
            raise PermissionError(13, "Permission denied")
        return real_rm(p)
    monkeypatch.setattr(graph, "write_json_atomic", json_spy)
    monkeypatch.setattr(graph, "_safe_rmtree", rm_spy)
    fx = FakeFixer()
    out, _rec = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=fx))
    assert fx.calls == []
    fix = out.rounds[0].fix
    assert not fix.attempted and not fix.fixed and fix.reason.startswith("error: ")
    assert ("OSError" if where == "loop_file" else "PermissionError") in fix.reason
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert out.delivery is not None and out.delivery.round == 1 and not out.delivery.build_ok
    assert "automatic build fix not started: error: " in out.message
    assert (paths.candidate(1) / graph.CANDIDATE_READY).exists() or (paths.round_dir(1) / graph.CANDIDATE_READY).exists()


# ------------------------------------------------------------------------------------------------ 4. usage
class JournalingFixer:
    """The loop tests' FakeFixer that also journals each answer like the real `LLMFixer` (`llm_script`)."""

    def __init__(self, scripts):
        from tests.loop.fakes import FakeFixer as LoopFakeFixer
        self.inner = LoopFakeFixer(scripts=scripts)

    def propose_script(self, ws, cluster, ctx):
        p = self.inner.propose_script(ws, cluster, ctx)
        ws.journal({"event": "llm_script", "model": p.model, "served": list(p.llm_usage.served),
                    "calls": p.llm_usage.calls,
                    "tokens": {"input": p.llm_usage.input_tokens, "output": p.llm_usage.output_tokens}})
        return p

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_usage_after_an_escaped_exception_is_read_from_the_loop_journal(rcfg, tmp_path):  # noqa: F811
    tree = make_tree(tmp_path)
    calls = {"n": 0}

    def build(ws, call):
        calls["n"] += 1
        if calls["n"] == 2:                       # after the first try asked the LLM
            raise RuntimeError("maven exploded")
        return broken_build(ws, call)
    from tests.loop.fakes import write_script
    fx = JournalingFixer([write_script({"src/main/java/com/x/Foo.java": "class Foo { ok }\n"})])
    res = run_loop(rcfg, tree, build=build, llm_fixer=fx)
    assert res.reason.startswith("error: RuntimeError")
    assert res.llm_calls == 1 and res.input_tokens == 100 and res.output_tokens == 20      # not 0 tokens
    assert [u.model for u in res.usage] == ["fake-model"]
    assert not any(u.tokens_unknown for u in res.usage)


def test_usage_after_an_escaped_exception_with_an_unreadable_journal_has_no_cost(rcfg, tmp_path, monkeypatch):  # noqa: F811
    tree = make_tree(tmp_path)
    calls = {"n": 0}

    def build(ws, call):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("maven exploded")
        return broken_build(ws, call)
    monkeypatch.setattr(fixer_mod, "journal_usage", lambda folder, cfg=None: None)
    from tests.loop.fakes import write_script
    res = run_loop(rcfg, tree, scripts=[write_script({"src/main/java/com/x/Foo.java": "class Foo { ok }\n"})],
                   build=build)
    assert res.llm_calls == 1 and res.input_tokens == 0 and res.output_tokens == 0
    [row] = res.usage
    assert row.tokens_unknown and row.cost is None and row.calls == 1
    assert FixResult.from_dict(res.to_dict()).usage[0].tokens_unknown
    [agg] = fix_usage_rows([res])
    assert agg["cost"] is None and agg["tokens_unknown"] is True
    assert fix_usage({"rounds": [{"round": 1, "fix": res.to_dict()}]})["calls"] == 1


def test_journal_beyond_count_goes_to_an_unpriced_row(tmp_path):
    folder = tmp_path / "fixloop"
    folder.mkdir()
    (folder / "journal.jsonl").write_text(json.dumps({"event": "llm_script", "model": "m1", "calls": 1,
                                                      "tokens": {"input": 7, "output": 3}}) + "\n", encoding="utf-8")
    rows, _cur = fixer_mod._usage_after_error(folder, 3, None)
    assert next((r.model, r.calls, r.input_tokens, r.tokens_unknown) for r in rows) == ("m1", 1, 7, False)
    assert rows[-1].calls == 2 and rows[-1].tokens_unknown and rows[-1].cost is None
    assert "tokens_unknown" not in rows[0].to_dict()


# ------------------------------------------------------------------------------------------------ 5. exec bit
def _tree_pair(tmp_path, data=b"hello\n"):
    tree, exp = tmp_path / "cand", tmp_path / "acc"
    for root in (tree, exp):
        (root / "src").mkdir(parents=True)
        (root / "src" / "a.txt").write_bytes(data)
        os.chmod(root / "src" / "a.txt", 0o644)
    return tree, exp, ["src/a.txt"]


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def test_exec_bit_changed_by_a_build_is_put_back_and_reported(tmp_path):
    tree, exp, paths = _tree_pair(tmp_path)
    table = intake_mod.file_table(exp, paths)
    assert table["src/a.txt"][2] is False
    os.chmod(tree / "src" / "a.txt", 0o755)                 # the "build" flips the exec bit
    missing, changed = intake_mod.sync_tree_table(tree, lambda r: exp / r, table, paths)
    assert (missing, changed) == ([], ["src/a.txt"])
    assert _mode(tree / "src" / "a.txt") == 0o644
    z = tmp_path / "out.zip"
    intake_mod.freeze_delivery(tree, z, 1, paths)
    with zipfile.ZipFile(z) as zf:
        assert (zf.getinfo("src/a.txt").external_attr >> 16) & 0o777 == 0o644


@NO_ROOT
@pytest.mark.parametrize("same", [True, False])
def test_an_unreadable_file_is_made_readable_once_and_put_back(tmp_path, same):
    tree, exp, paths = _tree_pair(tmp_path)
    f = tree / "src" / "a.txt"
    if not same:
        f.write_bytes(b"HELLO\n")                           # same size, other bytes
    os.chmod(f, 0)
    try:
        res = intake_mod.sync_tree_table(tree, lambda r: exp / r, intake_mod.file_table(exp, paths), paths)
        assert res == ([], ["src/a.txt"])
        assert f.read_bytes() == b"hello\n" and _mode(f) == 0o644
    finally:
        if f.exists():
            os.chmod(f, 0o644)


def test_a_build_that_only_flips_an_exec_bit_is_named_and_not_delivered(tmp_path):
    state, paths = make_run(tmp_path)

    def on_package(project):
        os.chmod(project / APP, 0o755)
    comps = one_fix_components(maven=FakeMaven(outcomes=[True, True], on_package=on_package), intake=intake_mod)
    out, _rec = run(state, paths, comps)
    assert out.status == SUCCEEDED, out.message
    assert out.rounds[0].build_changed == [APP]
    assert _mode(paths.accepted / APP) == 0o644
    with zipfile.ZipFile(paths.delivery_zip) as zf:
        assert (zf.getinfo(APP).external_attr >> 16) & 0o777 == 0o644
    rec = read_json(paths.round_dir(1) / graph.EXPECTED_FILE)
    assert rec["schema"] == 2 and all(len(v) == 3 and isinstance(v[2], bool) for v in rec["files"].values())


def test_a_table_without_the_exec_bit_sends_the_candidate_back_through_apply(tmp_path):
    paths, rec = _stopped_at_build(tmp_path)
    rd = paths.round_dir(1)
    exp = rd / graph.EXPECTED_FILE
    data = read_json(exp)
    data["schema"] = 1
    data["files"] = {k: v[:2] for k, v in data["files"].items()}            # the never-deployed 51d3809 shape
    exp.write_text(json.dumps(data), encoding="utf-8")
    marker = read_json(rd / graph.CANDIDATE_READY)
    marker["expected"] = sha256_file(exp)
    (rd / graph.CANDIDATE_READY).write_text(json.dumps(marker), encoding="utf-8")
    rec2 = Recorder()
    out, _ = run(_saved(rec), paths, one_fix_components(maven=FakeMaven(outcomes=[True])), rec=rec2)
    assert [e["why"] for e in rec2.events if e.get("event") == "candidate_rebuilt"] == [graph.WHY_NO_PROOF]
    assert out.status == SUCCEEDED, out.message
    assert len(next(iter(read_json(exp)["files"].values()))) == 3


# ------------------------------------------------------------------------------------------------ 6. no attempt
def test_a_loop_that_made_no_attempt_does_not_speak_of_tried_changes(tmp_path):
    state, paths = make_run(tmp_path)
    fx = FakeFixer(fixed=False, reason="llm unavailable")
    real = fx.run

    def no_attempt(*a, **kw):
        res = real(*a, **kw)
        res.attempts, res.iterations, res.llm_calls = 0, 1, 0
        return res
    fx.run = no_attempt
    out, _rec = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=fx))
    assert out.message.endswith(" | automatic build fix made no attempt: llm unavailable")
    md = paths.report_md.read_text(encoding="utf-8")
    head = md.split("\n## ", 1)[0]
    assert "> 빌드 오류 자동 수정을 하지 못했습니다(LLM을 사용할 수 없음)." in head.splitlines()
    assert "시도한 소스 수정" not in md and "시도했지만" not in md
    assert "- 빌드 오류 자동 수정: 하지 못함(LLM을 사용할 수 없음) · LLM 호출 0회" in md
    ko, raw = i18n.split_known(out.message)
    assert "빌드 오류 자동 수정을 하지 못했습니다(LLM을 사용할 수 없음)." in ko
    assert not any("automatic build fix" in r for r in raw)


@pytest.mark.parametrize("reason,ko", [("error: OSError: [Errno 28] No space left on device", "내부 오류"),
                                       ("time budget used up", "제한 시간 초과"),
                                       ("llm unavailable", "LLM을 사용할 수 없음")])
def test_no_attempt_sentence_per_reason(reason, ko):
    out = i18n.fix_no_attempt_ko(reason)
    assert out.startswith(f"빌드 오류 자동 수정을 하지 못했습니다({ko}).")
    assert "시도한" not in out
    assert i18n.ko_text(f"{graph.FIX_NO_ATTEMPT}: {reason}") == out


def test_no_attempt_rule_spares_results_that_had_been_fixes():
    assert graph.fix_made_no_attempt(FixResult(attempted=True, reason="llm unavailable"))
    assert not graph.fix_made_no_attempt(FixResult(attempted=True, attempts=2, reason="not fixed: 1 error(s) left"))
    assert not graph.fix_made_no_attempt(FixResult(attempted=True, reason=graph.R_FIX_CONFIRM_FAILED))
    assert not graph.fix_made_no_attempt(FixResult(attempted=True, reason=graph.R_FIX_DISCARDED))
    assert not graph.fix_made_no_attempt(FixResult(attempted=False, reason="stop requested"))


# ------------------------------------------------------------------------------------------------ 7. report node killed
def _kill_report_once(monkeypatch):
    real = report.write_report
    n = {"k": 0}

    def write(*a, **kw):
        n["k"] += 1
        if n["k"] == 1:
            raise Killed()
        return real(*a, **kw)
    monkeypatch.setattr(report, "write_report", write)


def test_a_kill_during_the_report_node_gets_the_report_on_the_next_entry(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path)
    _kill_report_once(monkeypatch)
    rec = Recorder()
    with pytest.raises(Killed):
        run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, True])), rec=rec)
    st = _saved(rec)
    assert st.status == SUCCEEDED and st.next_node == "report" and not paths.report_md.exists()
    assert graph.report_pending(paths, st)
    rec2 = Recorder()
    out, _ = run(st, paths, make_components(), rec=rec2)
    assert paths.report_md.is_file() and not graph.report_pending(paths, out)
    assert read_json(paths.report_json)["state"]["updated"] == out.updated
    assert any(e.get("event") == "report_rewritten" for e in rec2.events)
    # a report that was written is not written again
    mtime = paths.report_md.stat().st_mtime_ns
    assert graph.write_pending_report(out, paths, make_cfg(), save=Recorder().save, journal=Recorder().journal) is False
    assert paths.report_md.stat().st_mtime_ns == mtime


def test_a_kill_during_the_report_node_writes_the_combined_fix_patch_too(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path)
    _kill_report_once(monkeypatch)
    rec = Recorder()
    with pytest.raises(Killed):
        run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False, True]), fixer=FakeFixer()),
            rec=rec)
    assert not report.fix_patch_path(paths).exists()
    _out, _ = run(_saved(rec), paths, make_components())
    assert report.fix_patch_path(paths).is_file() and paths.report_md.is_file()


def test_a_state_that_ended_outside_the_graph_is_not_pending(tmp_path):
    state, paths = make_run(tmp_path)
    state.status, state.next_node = FAILED, ""
    assert not graph.report_pending(paths, state)


def test_chain_resume_writes_the_pending_report_of_a_final_state(tmp_path, monkeypatch):
    run_dir = tmp_path / "mig"
    rpaths = chain.chain_paths(run_dir)
    state, paths = make_run(tmp_path / "x")
    # move the hand-made run folder into the chained layout
    rpaths.root.parent.mkdir(parents=True)
    os.replace(paths.root, rpaths.root)
    _kill_report_once(monkeypatch)
    rec = Recorder()
    with pytest.raises(Killed):
        run(state, rpaths, one_fix_components(maven=FakeMaven(outcomes=[True, True])), rec=rec)
    rpaths.state.write_text(json.dumps(rec.saves[-1]), encoding="utf-8")
    out = chain.resume(run_dir, make_cfg(), journal=None, stop=None, components=make_components())
    assert out.status == SUCCEEDED and rpaths.report_md.is_file()
    assert not graph.report_pending(rpaths, chain.load_state(run_dir))


def test_the_in_process_service_writes_a_pending_report_at_startup(tmp_path, monkeypatch):
    from sbmig.remediations.models import REMEDIATIONS_DIR
    from sbmig.remediations.service import RemediationManager
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "default.yml").write_text("{}\n", encoding="utf-8")
    rid = "r20261120-120000-abcdef"
    state, paths = make_run(tmp_path / "x")
    target = RunPaths(tmp_path / "out" / REMEDIATIONS_DIR / rid)
    target.root.parent.mkdir(parents=True)
    os.replace(paths.root, target.root)
    state.id = rid
    _kill_report_once(monkeypatch)
    rec = Recorder()
    with pytest.raises(Killed):
        run(state, target, one_fix_components(maven=FakeMaven(outcomes=[True, True])), rec=rec)
    target.state.write_text(json.dumps(rec.saves[-1]), encoding="utf-8")
    RemediationManager(tmp_path / "out", repo, components_factory=lambda cfg, p: make_components(),
                       config_loader=lambda names: make_cfg())
    assert target.report_md.is_file()
    assert not graph.report_pending(target, RemediationState.from_dict(read_json(target.state)))


# ------------------------------------------------------------------------------------------------ 8. hashes agree
def test_table_hash_of_a_file_table_equals_tree_hash_for_a_folder_at_a_manifest_path(tmp_path):
    tree = tmp_path / "t"
    (tree / "dir_here").mkdir(parents=True)
    (tree / "a.txt").write_bytes(b"a")
    paths = ["a.txt", "dir_here", "gone.txt"]
    table = intake_mod.file_table(tree, paths)
    assert table["dir_here"][1] == intake_mod.NOT_REGULAR and "gone.txt" not in table
    assert intake_mod.table_hash(table, paths) == intake_mod.tree_hash(tree, paths)
