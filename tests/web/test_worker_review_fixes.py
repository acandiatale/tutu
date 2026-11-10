"""Review 2026-11-10, two Worker fixes.

1. Stop signals: the Worker is PID 1 of the Job's container, where SIGTERM has no default action — without a handler a
   deleted pod / a Job deadline left `sbmig run` and Maven writing the slot's repository and the run dir until SIGKILL.
   Now SIGTERM / SIGINT while the child runs stops the child's whole tree (Maven runs in its own session, so the tree is
   snapshotted and every descendant + its group is signalled; SIGKILL after `TERM_GRACE_S`), then `job-status.json`
   gets exit 3 + a "terminated by SIGTERM" error (never a verified success) and the Worker exits 3.
2. Slot repositories: when `<base>-slot<k>` cannot be created or written (or the base is unknown) the run uses a
   pod-local repository under the temp directory (`pod_local: true`), and the shared repository only when that fails too.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sbmig.web import contract, worker

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "20261110-200000-beef"
POSIX = pytest.mark.skipif(os.name == "nt", reason="process groups / signals: the pod is Linux")


def _request(tmp_path: Path, attempt: int = 1) -> dict:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "artifacts.yml").write_text("artifacts: {}\n", encoding="utf-8")
    out = tmp_path / "out"
    run_dir = out / RUN_ID
    run_dir.mkdir(parents=True)
    zip_path = run_dir / "source.zip"
    zip_path.write_bytes(b"PK\x03\x04 fake zip")
    contract.RequestRecord(
        run_id=RUN_ID, accepted_at=contract.utc_now(), upload="app.zip", configs=["artifacts.yml"],
        zip_sha256=contract.sha256_file(zip_path), config_digests=contract.config_digests(repo, ["artifacts.yml"]),
        sbmig_version=contract.sbmig_version(), mode="k8s-job", attempt=attempt).write(run_dir)
    env = {"SBMIG_SOURCE_ZIP": str(zip_path), "SBMIG_OUT": str(out), "SBMIG_RUN_ID": RUN_ID,
           "SBMIG_APP_DIR": str(repo), "SBMIG_PYTHON": "py", "SBMIG_KB_DIR": str(tmp_path / "kb")}
    return {"repo": repo, "out": out, "run_dir": run_dir, "zip": zip_path, "env": env}


@pytest.fixture()
def req(tmp_path):
    return _request(tmp_path, attempt=2)


def journal(run_dir: Path) -> list[dict]:
    return [json.loads(ln) for ln in (run_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()]


class FakeSignals:
    """An injected `install_signals`: keeps the handler, counts restores."""

    def __init__(self):
        self.handler = None
        self.restored = 0

    def __call__(self, handler):
        self.handler = handler

        def restore():
            self.restored += 1
        return restore


# ---- 1a. the signal state machine

def test_stop_signals_record_outside_the_child_and_raise_once_inside():
    s = worker.StopSignals()
    s.handle(signal.SIGTERM)                       # not armed: remembered only
    assert s.signum == signal.SIGTERM
    with pytest.raises(worker.WorkerTerminated) as e:
        s.arm()                                    # a signal before the child starts: the child never starts
    assert e.value.signum == signal.SIGTERM and str(e.value) == "SIGTERM"

    s = worker.StopSignals()
    s.arm()
    with pytest.raises(worker.WorkerTerminated):
        s.handle(signal.SIGINT)
    s.handle(signal.SIGTERM)                       # a second signal during the cleanup does not interrupt it
    assert s.signum == signal.SIGINT and not s.armed

    s = worker.StopSignals()
    s.arm()
    s.disarm()
    s.handle(signal.SIGTERM)                       # after the child ended: the status file is still written
    assert s.signum == signal.SIGTERM


def test_signal_name():
    assert worker.signal_name(signal.SIGTERM) == "SIGTERM"
    assert worker.signal_name(4242) == "signal 4242"


def test_install_stop_signals_restores_the_previous_handlers():
    before = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    seen = []
    restore = worker.install_stop_signals(lambda signum, frame: seen.append(signum))
    try:
        assert signal.getsignal(signal.SIGTERM) is not before[0]
    finally:
        restore()
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before


# ---- 1b. main(): what the Main sees after a stop signal

def test_sigterm_while_the_child_runs_writes_a_terminated_status(req, capsys):
    sigs = FakeSignals()
    calls = []

    def child(cmd, cwd, env):
        calls.append(cmd)
        sigs.handler(signal.SIGTERM, None)         # the kubelet's SIGTERM arrives while sbmig runs
        raise AssertionError("not reached: the handler raises")

    rc = worker.main(environ=req["env"], run_child=child, install_signals=sigs)
    assert rc == worker.TERMINATED_EXIT == 3 and len(calls) == 1 and sigs.restored == 1
    status = contract.JobStatus.read(req["run_dir"])
    assert status.exit_code == 3 and status.matches(RUN_ID, 2)
    assert status.error == worker.terminated_error(signal.SIGTERM)
    assert status.error.startswith("the Worker was terminated by SIGTERM") and "no result" in status.error
    events = journal(req["run_dir"])
    assert [e["event"] for e in events] == ["worker_started", "worker_finished"]
    fin = events[-1]
    assert fin["exit_code"] == 3 and fin["terminated"] == "SIGTERM" and fin["error"] == status.error
    assert "ERROR: the Worker was terminated by SIGTERM" in capsys.readouterr().out
    assert not (req["run_dir"] / "report.json").exists()


def test_sigint_is_named_too(req):
    sigs = FakeSignals()

    def child(cmd, cwd, env):
        sigs.handler(signal.SIGINT, None)
        return 0

    assert worker.main(environ=req["env"], run_child=child, install_signals=sigs) == 3
    assert "terminated by SIGINT" in contract.JobStatus.read(req["run_dir"]).error


def test_a_signal_before_the_child_starts_never_starts_it(req):
    sigs = FakeSignals()

    def resolver(configs, paths_yml):          # runs between the handler installation and the child's start (slot 2)
        sigs.handler(signal.SIGTERM, None)
        return req["out"].parent / "m2" / "repository"

    calls = []
    env = {**req["env"], contract.SLOT_ENV: "2"}
    rc = worker.main(environ=env, run_child=lambda *a: calls.append(a) or 0, repo_resolver=resolver,
                     install_signals=sigs)
    assert rc == 3 and calls == []
    assert "terminated by SIGTERM" in contract.JobStatus.read(req["run_dir"]).error
    assert sigs.restored == 1


def test_no_signal_is_unchanged(req):
    sigs = FakeSignals()
    rc = worker.main(environ=req["env"], run_child=lambda cmd, cwd, env: 0, install_signals=sigs)
    assert rc == 0 and sigs.restored == 1
    status = contract.JobStatus.read(req["run_dir"])
    assert status.exit_code == 0 and status.error is None
    assert "terminated" not in journal(req["run_dir"])[-1]


def test_a_refused_request_does_not_install_handlers(tmp_path):
    sigs = FakeSignals()
    env = {"SBMIG_SOURCE_ZIP": str(tmp_path / "x.zip"), "SBMIG_OUT": str(tmp_path / "out"), "SBMIG_RUN_ID": RUN_ID,
           "SBMIG_APP_DIR": str(tmp_path)}
    assert worker.main(environ=env, run_child=lambda *a: 0, install_signals=sigs) == 3
    assert sigs.handler is None


def test_descendants():
    table = {10: (1, 10, "S"), 11: (10, 10, "S"), 12: (11, 12, "S"), 13: (12, 12, "S"), 20: (1, 20, "S")}
    assert worker.descendants(10, table) == {11, 12, 13}
    assert worker.descendants(13, table) == set()


def test_parse_proc_stat():
    assert worker.parse_proc_stat("1234 (java) S 1 1234 1234 0 -1 4194560 ...") == (1, 1234, "S")
    assert worker.parse_proc_stat("77 (we ird) (x)) Z 12 70 70 0") == (12, 70, "Z")      # comm with spaces / parens
    with pytest.raises((ValueError, IndexError)):
        worker.parse_proc_stat("garbage")


@POSIX
def test_process_table_sees_this_process():
    table = worker.process_table()
    assert table[os.getpid()][0] == os.getppid()


# ---- 1c. real processes: SIGTERM to the Worker, the child and its grandchild (own session, ignores SIGTERM) are gone

FAKE_CHILD = r'''
import os, signal, subprocess, sys, time
rd = os.path.join(os.environ["SBMIG_OUT"], os.environ["SBMIG_RUN_ID"])
ignore = os.environ.get("FAKE_GRANDCHILD_IGNORES_TERM") == "1"
code = ("import os, signal, sys, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore else "")
        + "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        + "time.sleep(300)\n")
# like mvn.run_logged: Maven runs in its own session, out of the child's process group
subprocess.Popen([sys.executable, "-c", code, os.path.join(rd, "grandchild.pid")], start_new_session=True)
open(os.path.join(rd, "child.pid"), "w").write(str(os.getpid()))
print("fake sbmig running", flush=True)
time.sleep(300)
'''


def _gone(pid: int) -> bool:
    hit = worker.process_table().get(pid)
    return hit is None or hit[2].startswith("Z")


def _wait_file(p: Path, timeout: float = 30.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = p.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"{p} never appeared")


@POSIX
@pytest.mark.parametrize("ignores_term", [False, True])
def test_sigterm_to_the_worker_process_stops_child_and_grandchild(tmp_path, ignores_term):
    r = _request(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "fake_child.py").write_text(FAKE_CHILD, encoding="utf-8")
    wrapper = scripts / "fake-python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{scripts / "fake_child.py"}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), **r["env"], "SBMIG_PYTHON": str(wrapper),
           "FAKE_GRANDCHILD_IGNORES_TERM": "1" if ignores_term else "0"}
    log = open(tmp_path / "worker.out", "wb")          # noqa: SIM115 — closed in the finally below
    proc = subprocess.Popen([sys.executable, "-m", "sbmig.web.worker"], cwd=str(REPO_ROOT), env=env, stdout=log,
                            stderr=subprocess.STDOUT)
    pids: list[int] = []
    try:
        pids.append(_wait_file(r["run_dir"] / "child.pid"))
        pids.append(_wait_file(r["run_dir"] / "grandchild.pid"))
        time.sleep(0.3)                                       # the Worker is inside its pipe loop
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=worker.TERM_GRACE_S + worker.KILL_GRACE_S + 10)
        elapsed = time.monotonic() - t0
        deadline = time.monotonic() + 3                       # an orphan is reaped by its new parent (launchd / init)
        while not all(_gone(p) for p in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert all(_gone(p) for p in pids), f"left alive: {[p for p in pids if not _gone(p)]}"
        assert rc == 3
        bound = worker.TERM_GRACE_S + worker.KILL_GRACE_S + 5
        assert elapsed < (bound if ignores_term else worker.TERM_GRACE_S), elapsed
        status = contract.JobStatus.read(r["run_dir"])
        assert status.exit_code == 3 and status.error.startswith("the Worker was terminated by SIGTERM")
        assert journal(r["run_dir"])[-1]["terminated"] == "SIGTERM"
    finally:
        log.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for p in pids:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass


# ---- 2. slot repositories: pod-local before shared

UNWRITABLE = pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                                reason="needs POSIX permissions and a non-root user")


@UNWRITABLE
def test_prepare_repo_dir_probes_writability(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        value, why = worker.prepare_repo_dir(ro)              # it exists (mkdir succeeds) but nothing can be written
        assert value is None and why.startswith(f"cannot write to {ro} (")
        value, why = worker.prepare_repo_dir(ro / "sub")
        assert value is None and why.startswith(f"cannot create {ro / 'sub'} (")
    finally:
        ro.chmod(0o755)
    value, why = worker.prepare_repo_dir(tmp_path / "ok")
    assert value == str(tmp_path / "ok") and why is None and list((tmp_path / "ok").iterdir()) == []


@UNWRITABLE
def test_an_unwritable_slot_directory_goes_pod_local(tmp_path):
    base = tmp_path / "m2" / "repository"
    sibling = base.parent / "repository-slot3"
    sibling.mkdir(parents=True)
    sibling.chmod(0o555)                                      # the NFS shape: the folder exists, the pod's uid cannot write
    try:
        child: dict = {}
        data = worker.apply_slot_repo(child, {contract.SLOT_ENV: "3", worker.LOCAL_REPO_ENV: str(base)}, [],
                                      tmp_path / "p.yml", cwd=tmp_path, local_root=tmp_path / "pod")
    finally:
        sibling.chmod(0o755)
    local = tmp_path / "pod" / "sbmig-m2-slot3"
    assert data == {"slot": 3, "local_repo": str(local), "shared": False, "pod_local": True,
                    "note": data["note"]}
    assert data["note"].startswith(f"pod-local repository: cannot write to {sibling} (")
    assert child == {worker.LOCAL_REPO_ENV: str(local)} and local.is_dir()
    msg = worker.slot_message(data)
    assert msg.startswith("WARNING: job slot 3: pod-local repository: cannot write to ")
    assert f"the run uses the pod-local Maven local repository {local} and downloads all its dependencies again" in msg
    assert "PVC sbmig-m2" in msg and "shared Maven local repository" not in msg


def test_both_fallbacks_failing_uses_the_shared_repository(tmp_path):
    base = tmp_path / "m2" / "repository"
    base.parent.mkdir(parents=True)
    (base.parent / "repository-slot2").write_text("in the way", encoding="utf-8")
    blocked = tmp_path / "tmpfile"
    blocked.write_text("not a folder", encoding="utf-8")
    child: dict = {}
    data = worker.apply_slot_repo(child, {contract.SLOT_ENV: "2", worker.LOCAL_REPO_ENV: str(base)}, [],
                                  tmp_path / "p.yml", cwd=tmp_path, local_root=blocked)
    assert child == {}
    assert data["shared"] is True and data["local_repo"] is None and "pod_local" not in data
    assert data["note"].startswith(f"cannot create {base.parent / 'repository-slot2'} (")
    assert f"; pod-local fallback: cannot create {blocked / 'sbmig-m2-slot2'} (" in data["note"]
    assert worker.slot_message(data).endswith("; the run uses the shared Maven local repository")


def test_slot_one_and_no_slot_are_untouched_by_the_fallback(tmp_path):
    for environ, want in (({}, None), ({contract.SLOT_ENV: "1"}, {"slot": 1, "local_repo": None, "shared": False})):
        child: dict = {}
        assert worker.apply_slot_repo(child, environ, [], tmp_path / "p.yml", cwd=tmp_path,
                                      local_root=tmp_path / "pod") == want
        assert child == {} and not (tmp_path / "pod").exists()


def test_pod_local_repo_default_is_the_temp_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(worker.tempfile, "gettempdir", lambda: str(tmp_path))
    assert worker.pod_local_repo(2) == tmp_path / "sbmig-m2-slot2"
    assert worker.pod_local_repo(3, "/x") == Path("/x/sbmig-m2-slot3")
