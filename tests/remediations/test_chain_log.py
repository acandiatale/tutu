"""remediation/remediation.log exists for every remediation that ran (review finding S1).

The chained run in the build pod, the `--flow remediation` run and a resume in a new pod get the same file handler
the standalone run has: removed in `finally`, never two handlers on one logger, lines scrubbed of secrets."""
from __future__ import annotations

import logging
import os

from sbmig.remediations import chain

from .test_chain import ENV, Journal, _zip, go, ivas_env, one_fix_components, only, project  # noqa: F401 - fixture
from .fakes import POM, make_cfg


def _remediation_handlers():
    return [h for h in logging.getLogger("sbmig").handlers
            if isinstance(h, logging.FileHandler) and os.path.basename(h.baseFilename) == "remediation.log"]


def test_chained_run_writes_remediation_log(tmp_path, ivas_env):
    run_dir, root = project(tmp_path)
    out, _ = go(run_dir, root, comps=one_fix_components())
    log_file = chain.chain_paths(run_dir).log
    assert out is not None and log_file.is_file()
    text = log_file.read_text(encoding="utf-8")
    assert "vulnerability remediation run-1: start" in text and "chained remediation run-1: SUCCEEDED" in text
    assert _remediation_handlers() == []                    # removed again


def test_remediation_only_run_writes_remediation_log(tmp_path, ivas_env):
    z = _zip(tmp_path, {"pom.xml": POM, "src/main/java/App.java": "class App {}\n"})
    run_dir = tmp_path / "run"
    only(run_dir, z, comps=one_fix_components())
    text = chain.chain_paths(run_dir).log.read_text(encoding="utf-8")
    assert "remediation only" in text and _remediation_handlers() == []


def test_resume_appends_to_remediation_log(tmp_path, ivas_env):
    run_dir, root = project(tmp_path)
    stop = {"on": True}
    go(run_dir, root, comps=one_fix_components(), stop=lambda: stop["on"])
    log_file = chain.chain_paths(run_dir).log
    before = log_file.read_text(encoding="utf-8")
    chain.resume(run_dir, make_cfg(), journal=Journal(), stop=lambda: False, components=one_fix_components())
    after = log_file.read_text(encoding="utf-8")
    assert after.startswith(before) and "chained remediation run-1: SUCCEEDED" in after[len(before):]
    assert _remediation_handlers() == []


def test_secrets_are_scrubbed_in_remediation_log(tmp_path, ivas_env):
    run_dir, root = project(tmp_path)
    probe = logging.getLogger("sbmig.remediations.probe")

    def plan(*a, **k):
        probe.warning("talking to IVAS as %s with %s", ENV["IVAS_USER_ID"], ENV["IVAS_PASSWORD"])
        from sbmig.remediations.models import RoundPlan
        return RoundPlan(round=1, source_tree_hash="", policy_hash="")
    from .fakes import ScriptedPlanner
    go(run_dir, root, comps=one_fix_components(plan=ScriptedPlanner([plan])))
    text = chain.chain_paths(run_dir).log.read_text(encoding="utf-8")
    assert "talking to IVAS" in text
    assert ENV["IVAS_PASSWORD"] not in text and ENV["IVAS_USER_ID"] not in text


def test_an_existing_handler_on_the_same_file_is_not_doubled(tmp_path, ivas_env):
    # the in-process chained resume attaches its own handler first (service._attach_log); chain must not add a second
    run_dir, root = project(tmp_path)
    paths = chain.chain_paths(run_dir)
    paths.root.mkdir(parents=True, exist_ok=True)
    outer = logging.FileHandler(paths.log, encoding="utf-8")
    root_logger = logging.getLogger("sbmig")
    root_logger.addHandler(outer)
    try:
        with chain.remediation_log(paths) as inner:
            assert inner is None and len(_remediation_handlers()) == 1
    finally:
        root_logger.removeHandler(outer)
        outer.close()


def test_handler_is_removed_when_the_run_raises(tmp_path, ivas_env):
    run_dir, root = project(tmp_path)
    paths = chain.chain_paths(run_dir)
    try:
        with chain.remediation_log(paths):
            assert len(_remediation_handlers()) == 1
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert _remediation_handlers() == []


def test_other_threads_do_not_reach_the_file(tmp_path, ivas_env):
    import threading
    run_dir, root = project(tmp_path)
    paths = chain.chain_paths(run_dir)
    other = logging.getLogger("sbmig.web.probe")
    with chain.remediation_log(paths):
        t = threading.Thread(target=lambda: other.warning("another run's line"))
        t.start()
        t.join()
        other.warning("this run's line")
    text = paths.log.read_text(encoding="utf-8")
    assert "this run's line" in text and "another run's line" not in text
