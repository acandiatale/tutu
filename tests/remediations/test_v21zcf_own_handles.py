"""v2.1z-cf rule 6 (docs/INTERFACES.md "v2.1z-cf"): does THIS process hold a descriptor on a path at the moment the
remediation replaces, renames or removes it? Windows refuses such a call (WinError 5 / 32); POSIX allows it, so only a
guard that looks at our own descriptors can tell (`tests/remediations/own_handles.py`).

The question came from a company PC run of 2026-11-25: round 2's delivery swap `fsops.replace(delivery/.result.next.zip,
delivery/result.zip)` in `graph._Holder.install_delivery` was refused for 60 s; the destination was round 1's result ZIP.

Layout of this file:
1. the guard proves itself: a file held open as the destination of a replace, as the source, inside a directory being
   renamed / removed, a directory handle, a `zipfile.ZipFile` left open, the working directory; and silence when nothing
   is held;
2. remediation flows under the guard, with the real intake (`sbmig.remediations.intake`: `accept_zip`, `copy_tree`,
   `sync_tree_table`, `freeze_delivery`), the real IVAS client and scan procedure (`IvasHttpClient` + `run_scan`) over
   an in-memory IVAS that reads every uploaded byte, the real chain (`run_after_migration`, `run_only`, `resume`: run
   claim, `remediation.log` handler, `_clear_root`, `source_zip`), and fakes for Maven / planner / apply (`fakes.py`):
   (a) two rounds, round 2 replacing round 1's `result.zip` (both rounds built; round 2 failing with the verified ZIP
   kept), (b) a single round from intake to report (chained and remediation-only), (c) `accept_zip` alone,
   (d) the fix loop on a failed candidate (the real `run_fix_loop` / `BuildFixLoop` with the loop tests' fakes),
   (e) a stopped run resumed;
3. `IvasHttpClient.upload` after success and after each of its error paths, also with a real `requests.Session`
   against a local HTTP server.

Every flow asserts that the call it is about was seen by the guard (so a silent guard is not a guard that saw nothing).
"""
from __future__ import annotations

import functools
import io
import json
import os
import shutil
import socket
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

from sbmig.remediations import chain, fsops, graph
from sbmig.remediations import fixer as fixer_mod
from sbmig.remediations import intake as intake_mod
from sbmig.remediations.ivas import IvasHttpClient, IvasSettings, run_scan
from sbmig.remediations.models import (
    FAILED,
    STOPPED,
    SUCCEEDED,
    IvasError,
    IvasUploadUncertain,
    RoundPlan,
    RunPaths,
)
from tests.loop.fakes import (
    FakeBuildRunner,
    FakeScriptRunner,
    bad_build,
    compile_error,
    make_profile,
    ok_build,
    write_script,
)
from tests.loop.fakes import FakeFixer as LoopLLM

from . import own_handles
from .fakes import (
    POM,
    FakeMaven,
    ScriptedPlanner,
    deterministic_zip,
    make_cfg,
    make_components,
    proposal,
)
from .own_handles import HeldPathError, guard, held_on, open_descriptors
from .test_ivas import FakeSession, Resp, connect_refused, partial_then, upload_ok

pytestmark = pytest.mark.skipif(not own_handles.supported(), reason="descriptors cannot be listed on this platform")

BASE = "https://ivas.invalid/api"
ENV = {"IVAS_BASE_URL": BASE, "IVAS_USER_ID": "tester", "IVAS_PASSWORD": "not-a-real-pw"}
APP = "src/main/java/App.java"
POM2 = POM.replace("  </dependencies>", """    <dependency>
      <groupId>example</groupId>
      <artifactId>other</artifactId>
      <version>2.0</version>
    </dependency>
  </dependencies>""")


@pytest.fixture()
def ivas_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def _real(p) -> str:
    return os.path.realpath(str(p))


# ============================================================================ 1. the guard proves itself
def test_open_descriptors_resolve_real_paths(tmp_path):
    f = tmp_path / "held.txt"
    f.write_text("x")
    # on macOS tmp_path lives under /private/var; a path spelled /var/... must compare equal
    with open(f, "rb") as fh:
        fds = open_descriptors()
        assert fds.get(fh.fileno()) == _real(f)
        assert held_on(f) == [(fh.fileno(), _real(f))]
    assert held_on(f) == []


def test_guard_reports_the_destination_of_a_replace_held_open(tmp_path):
    src, dst = tmp_path / "next.zip", tmp_path / "result.zip"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    fh = open(dst, "rb")  # noqa: SIM115 - held open on purpose
    try:
        with guard(only_under=tmp_path) as g:
            fsops.replace(src, dst)                       # POSIX lets it through; the guard records it
        assert [(f.op, f.target, f.fd) for f in g.findings] == [("replace", _real(dst), fh.fileno())]
        assert g.calls_to("replace", name="result.zip")[0].via_fsops
        assert "result.zip" in g.report()
    finally:
        fh.close()


def test_strict_guard_raises_before_the_call_and_names_the_source(tmp_path):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_bytes(b"1")
    with open(src, "rb"), guard(strict=True) as g, pytest.raises(HeldPathError) as ei:
        fsops.replace(src, dst)
    assert ei.value.finding.target == _real(src) and src.exists() and not dst.exists()
    assert len(g.findings) == 1


def test_guard_reports_a_file_inside_a_directory_being_renamed(tmp_path):
    d = tmp_path / ".accepted.partial"
    (d / "src").mkdir(parents=True)
    inner = d / "src" / "App.java"
    inner.write_text("class App {}")
    with open(inner, "rb") as fh, guard(only_under=tmp_path) as g:
        fsops.rename(d, tmp_path / "accepted")
        fd = fh.fileno()
    # the descriptor's path at the moment of the call (before the rename moved it)
    assert [(f.op, f.target, f.held, f.fd) for f in g.findings] == [("rename", _real(d), _real(inner), fd)]


def test_guard_reports_a_directory_handle_and_an_unclosed_scandir(tmp_path):
    d = tmp_path / "tree"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "f").write_text("x")
    dfd = os.open(d / "sub", os.O_RDONLY)
    try:
        with guard() as g:
            fsops.rmtree(d)
        assert [(f.op, f.fd) for f in g.findings] == [("rmtree", dfd)]
    finally:
        os.close(dfd)
    (d / "sub").mkdir(parents=True)
    it = os.scandir(d)                                    # an iterator never exhausted keeps the directory open
    try:
        with guard() as g:
            fsops.rmtree(d)
        assert len(g.findings) == 1 and g.findings[0].held == _real(d)
    finally:
        it.close()


def test_guard_reports_a_zipfile_left_open_and_an_unlink(tmp_path):
    z = tmp_path / "result.zip"
    z.write_bytes(deterministic_zip({"a": b"1"}))
    zf = zipfile.ZipFile(z)                               # never closed: the object owns a descriptor
    try:
        with guard() as g:
            fsops.unlink(z)
        assert [(f.op, f.target) for f in g.findings] == [("unlink", _real(z))]
    finally:
        zf.close()


def test_guard_reports_the_working_directory_inside_a_removed_tree(tmp_path, monkeypatch):
    d = tmp_path / "work"
    d.mkdir()
    monkeypatch.chdir(d)
    with guard() as g:
        fsops.rmtree(d)
    monkeypatch.chdir(tmp_path)
    assert [(f.op, f.fd) for f in g.findings] == [("rmtree", -1)]


def test_guard_is_silent_when_nothing_is_held_and_restores_the_functions(tmp_path):
    before = (os.replace, os.rename, os.unlink, shutil.rmtree)
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_bytes(b"1")
    with open(src, "rb") as fh:
        data = fh.read()
    (tmp_path / "d" / "e").mkdir(parents=True)
    (tmp_path / "d" / "e" / "f").write_bytes(data)
    with guard(strict=True) as g:
        fsops.replace(src, dst)
        fsops.rename(tmp_path / "d", tmp_path / "d2")
        fsops.unlink(dst)
        fsops.rmtree(tmp_path / "d2")
    assert g.findings == [] and [c.op for c in g.calls] == ["replace", "rename", "unlink", "rmtree"]
    assert all(c.via_fsops for c in g.calls)
    assert (os.replace, os.rename, os.unlink, shutil.rmtree) == before


def test_guard_ignores_descriptors_outside_the_paths_of_the_call(tmp_path):
    other = tmp_path / "other.log"
    with open(other, "a"):
        a = tmp_path / "x"
        a.write_bytes(b"1")
        with guard(strict=True) as g:
            fsops.replace(a, tmp_path / "y")
    assert g.findings == []


# ---------------------------------------------------------------------------- the test run's own disk watch
def _bounded():
    """The `scripts/bounded.py` module the root conftest loaded and registered (the one its disk watch uses)."""
    import sys
    mod = sys.modules.get("_repo_bounded")
    assert mod is not None and hasattr(mod, "WALK_LOCK"), "root conftest.py did not register _repo_bounded"
    return mod


def _wide_tree(root: Path, dirs: int = 300) -> Path:
    for i in range(dirs):
        d = root / f"d{i // 20}" / f"e{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "f.txt").write_bytes(b"x")
    return root


class _Walker:
    """A thread calling `walk()` in a tight loop until stopped; counts the walks it finished."""

    def __init__(self, walk):
        self.walk, self.walks, self._stop = walk, 0, threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            self.walk()
            self.walks += 1
            # a Lock is not fair: a walker that takes it again at once can starve the guard waiting for it (the
            # real watch sleeps >= 10 s between walks). A 1 ms pause between walks leaves the guard its turn.
            time.sleep(0.001)

    def __enter__(self):
        self._t.start()
        while self.walks < 1:                          # it is really walking before the guarded calls start
            time.sleep(0.001)
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=30)


def test_the_disk_watchs_walk_is_never_reported(tmp_path):
    """The root conftest's disk watch walks the basetemp with `tree_mb` in a thread of this process; its `scandir`
    descriptors sit under our paths. The guard reads the descriptors under `WALK_LOCK`, so 200 guarded directory
    renames / removals under a tree that `tree_mb` walks in a tight loop report nothing."""
    b = _bounded()
    tree = _wide_tree(tmp_path / "tree")
    with _Walker(lambda: b.tree_mb(tmp_path)) as w, guard(only_under=tmp_path) as g:
        for i in range(100):
            src = tree / f"d{i % 15}"
            fsops.rename(src, tree / f"r{i % 15}")
            fsops.rename(tree / f"r{i % 15}", src)
        for i in range(15):
            fsops.rmtree(tree / f"d{i}")
        start = w.walks
    assert g.findings == [], g.report()
    assert len(g.calls) == 215 and w.walks >= start >= 1


def test_a_held_descriptor_is_still_reported_while_the_watch_walks(tmp_path):
    b = _bounded()
    tree = _wide_tree(tmp_path / "tree")
    inner = tree / "d3" / "e60" / "f.txt"
    with open(inner, "rb") as fh, _Walker(lambda: b.tree_mb(tmp_path)), guard(only_under=tmp_path) as g:
        for _ in range(50):
            fsops.rename(tree / "d3", tree / "r3")
            fsops.rename(tree / "r3", tree / "d3")
        fd = fh.fileno()
    assert len(g.findings) == 100 and {f.fd for f in g.findings} == {fd}, g.report()


def test_a_walk_that_does_not_take_the_lock_is_still_reported(tmp_path):
    """A walk by other code of this process (a lookup of our own running during a retry) does not take `WALK_LOCK`:
    paused with a `scandir` open under the directory, it is reported, deterministically."""
    tree = _wide_tree(tmp_path / "tree", dirs=40)
    opened, release = threading.Event(), threading.Event()

    def walker():
        with os.scandir(tree / "d1") as it:
            next(it)
            opened.set()
            release.wait(10)
    t = threading.Thread(target=walker, daemon=True)
    t.start()
    try:
        assert opened.wait(10)
        with guard(only_under=tmp_path) as g:
            fsops.rmtree(tree / "d1")
    finally:
        release.set()
        t.join(10)
    assert [(f.op, f.held) for f in g.findings] == [("rmtree", os.path.realpath(tree / "d1"))], g.report()


def test_the_guard_does_not_hold_the_walk_lock_while_the_real_call_runs(tmp_path, monkeypatch):
    b = _bounded()
    seen: list[bool] = []
    real_replace, real_rmtree = os.replace, shutil.rmtree

    def probe_replace(*a, **kw):
        seen.append(b.WALK_LOCK.locked())
        return real_replace(*a, **kw)

    def probe_rmtree(*a, **kw):
        seen.append(b.WALK_LOCK.locked())
        return real_rmtree(*a, **kw)
    monkeypatch.setattr(os, "replace", probe_replace)      # the guard wraps these as its "real" calls
    monkeypatch.setattr(shutil, "rmtree", probe_rmtree)
    (tmp_path / "a").write_bytes(b"1")
    (tmp_path / "d" / "e").mkdir(parents=True)
    with guard(strict=True, only_under=tmp_path) as g:
        fsops.replace(tmp_path / "a", tmp_path / "b")
        fsops.rmtree(tmp_path / "d")
        assert not b.WALK_LOCK.locked()                     # nor between calls
    assert seen == [False, False] and g.findings == []


# ============================================================================ 2. remediation flows under the guard
class IvasServer:
    """A stand-in for the IVAS server behind `IvasHttpClient` (the `session` argument): every upload body is read to the
    end as a transport would; upload n gets analysis `anl-<n>` and answers with the library findings `findings[n-1]`
    (a list of (ga, version, cve)); status is DONE at once; summary totals match the pages."""

    def __init__(self, findings: list[list[tuple[str, str, str]]]):
        self.findings = [list(f) for f in findings]
        self.uploads: list[str] = []          # file names as sent
        self.after_upload = None              # optional callable(n)

    def _rows(self, n: int) -> list[dict]:
        rows = self.findings[n - 1] if n - 1 < len(self.findings) else []
        return [{"id": cve, "library": {"identifier": ga, "version": v, "info": {"patchedVersion": "9.9"}},
                 "detectedLocations": [{"filePath": "pom.xml"}], "action": {"actionDivCd": "NE"},
                 "rating": [{"version": "v3", "score": 8.0}]} for ga, v, cve in rows]

    def request(self, method, url, **kw):
        assert url.startswith(BASE), url
        path = url[len(BASE):]
        if method == "POST" and path == "/session":
            return Resp(200, {"token": "tok-1"})
        if method == "POST" and path == "/projects/SELF/sourcecode":
            body = kw["data"]
            while body.read(65536):
                pass
            self.uploads.append(str(kw["headers"].get("Content-Type", "")))
            n = len(self.uploads)
            if self.after_upload is not None:
                self.after_upload(n)
            return Resp(200, {"projectId": "prj", "lbrdrAnlId": f"anl-{n}"})
        parts = path.strip("/").split("/")
        # /projects/SELF/prj/analysis/anl-N/...
        n = int(parts[4].split("-")[1])
        rest = "/".join(parts[5:])
        if rest == "status":
            return Resp(200, {"projectId": "prj", "lbrdrAnlId": f"anl-{n}", "anlStatusDivCd": "DONE"})
        rows = self._rows(n)
        if rest == "result/summary":
            cats = ["components", "swComponents", "fileComponents", "codeSnippets", "file", "functionComponents",
                    "ospkgComponents", "binaryComponents"]
            totals = {c: {"total": len(rows) if c == "components" else 0} for c in cats}
            return Resp(200, {"summary": {"resultCount": {"vulnerabilities": totals}}})
        if rest == "result/libraryvulnerabilities":
            p, size = int(kw["params"]["page"]), int(kw["params"]["size"])
            return Resp(200, {"content": rows[p * size:(p + 1) * size], "totalElements": len(rows)})
        return Resp(200, {"content": [], "totalElements": 0})


def real_parts(server: IvasServer, **kw):
    """Components: the real IVAS client + scan procedure over `server`, the real intake module, fakes for the rest."""
    st = IvasSettings(base_url=BASE, user_id="tester", password="not-a-real-pw", page_size=50, poll_seconds=0,
                      analysis_timeout_seconds=60, project_prefix="sbmig")
    client = IvasHttpClient(st, session=server, sleep=lambda _x: None)
    scan = functools.partial(run_scan, settings=st, sleep=lambda _x: None, clock=lambda: 0.0)
    return make_components(ivas=client, scan=scan, intake=intake_mod, **kw)


def lib(cve="CVE-2026-0001"):
    return ("example:lib", "1.0", cve)


def other(cve="CVE-2026-0002"):
    return ("example:other", "2.0", cve)


def two_round_plan():
    return ScriptedPlanner([
        RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal(1)]),
        RoundPlan(round=2, source_tree_hash="", policy_hash="",
                  proposals=[proposal(1, round=2, ga="example:other", old="2.0", new="2.1")])])


def project(tmp_path, pom=POM, files=None):
    run_dir = tmp_path / "run"
    root = run_dir / "project"
    for rel, text in ({"pom.xml": pom, APP: "class App {}\n"} if files is None else files).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return run_dir, root


def chained(run_dir, root, comps, cfg=None, stop=None):
    events: list[dict] = []
    out = chain.run_after_migration(run_dir, root, cfg or make_cfg(after_migration=True), run_id="run-1",
                                    journal=events.append, stop=stop or (lambda: False), components=comps)
    return out, events


def assert_clean(g, run_dir):
    """No finding, and the guard looked at calls inside the run folder."""
    assert g.findings == [], g.report()
    assert g.calls_to("replace", under=run_dir), "the guard saw no replace inside the run folder"


def delivery_swaps(g, run_dir) -> list:
    return [c for c in g.calls_to("replace", name="result.zip", under=run_dir / "remediation" / "delivery")
            if os.path.basename(c.paths[0]) == graph.NEXT_ZIP]


# ---------------------------------------------------------------------------- (a) two rounds
def test_a_two_rounds_round_two_replaces_round_ones_result_zip(tmp_path, ivas_env):
    run_dir, root = project(tmp_path, POM2)
    server = IvasServer([[lib(), other()], [other()], []])
    comps = real_parts(server, plan=two_round_plan())
    with guard(strict=True, only_under=tmp_path) as g:
        out, _ = chained(run_dir, root, comps)
    assert out.status == SUCCEEDED, (out.reason, out.message)
    assert [r.round for r in out.rounds] == [1, 2] and out.delivery.round == 2
    assert len(server.uploads) == 3                       # baseline, final-r1 (= round 1's result.zip), final-r2
    swaps = delivery_swaps(g, run_dir)
    assert len(swaps) == 2 and all(c.caller.startswith("sbmig/remediations/graph.py") for c in swaps)
    assert_clean(g, run_dir)


def test_a_the_flow_guard_trips_on_round_twos_swap_when_a_reader_leaks_its_handle(tmp_path, ivas_env, monkeypatch):
    """The proof at flow level: the same run with `graph.sha256_file` replaced by a reader that never closes its
    file. The guard must name round 2's `install_delivery` replace of `delivery/result.zip`."""
    leaked: list = []
    real_sha = graph.sha256_file

    def leaking_sha(path, *a, **kw):
        leaked.append(open(path, "rb"))  # noqa: SIM115 - the handle a careless reader would keep
        return real_sha(path, *a, **kw)
    monkeypatch.setattr(graph, "sha256_file", leaking_sha)
    run_dir, root = project(tmp_path, POM2)
    server = IvasServer([[lib(), other()], [other()], []])
    try:
        with guard(only_under=tmp_path) as g:
            chained(run_dir, root, real_parts(server, plan=two_round_plan()))
    finally:
        for fh in leaked:
            fh.close()
    result_zip = _real(chain.chain_paths(run_dir).delivery_zip)
    hits = [f for f in g.findings if f.op == "replace" and f.target == result_zip]
    assert hits, g.report()
    assert hits[0].caller.startswith("sbmig/remediations/graph.py:") and "install_delivery" in hits[0].caller


def test_a_two_rounds_round_two_fails_and_keeps_the_verified_round_one_zip(tmp_path, ivas_env):
    run_dir, root = project(tmp_path, POM2)
    server = IvasServer([[lib(), other()], [other()]])
    comps = real_parts(server, plan=two_round_plan(), maven=FakeMaven(outcomes=[True, True, False]))
    with guard(strict=True, only_under=tmp_path) as g:
        out, _ = chained(run_dir, root, comps)
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    paths = chain.chain_paths(run_dir)
    assert out.delivery.round == 2 and graph.find_verified_zip(paths) == graph.verified_zip_path(paths, 1)
    swaps = delivery_swaps(g, run_dir)
    assert len(swaps) == 2
    # the verified copy of round 1's result.zip was made (copy + replace) right before round 2's swap
    assert g.calls_to("replace", name="result-r1-verified.zip")
    assert_clean(g, run_dir)


# ---------------------------------------------------------------------------- (b) one round, intake → report
def test_b_one_round_chained_from_intake_to_report(tmp_path, ivas_env):
    run_dir, root = project(tmp_path)
    server = IvasServer([[lib()], []])
    comps = real_parts(server, plan=ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="",
                                                               proposals=[proposal()])]))
    with guard(strict=True, only_under=tmp_path) as g:
        out, _ = chained(run_dir, root, comps)
    assert out.status == SUCCEEDED, (out.reason, out.message)
    paths = chain.chain_paths(run_dir)
    assert paths.report_md.is_file() and paths.delivery_zip.is_file() and paths.log.is_file()
    assert g.calls_to("replace", name="accepted")       # the intake's rename of .accepted.partial
    assert g.calls_to("replace", name="report.md") and len(delivery_swaps(g, run_dir)) == 1
    assert g.calls_to("unlink", name=chain.CLAIM_NAME)    # the claim released (bare os.unlink, not fsops)
    assert_clean(g, run_dir)


def test_b_remediation_only_from_an_uploaded_zip(tmp_path, ivas_env):
    upload = tmp_path / "upload" / "app.zip"
    upload.parent.mkdir()
    upload.write_bytes(deterministic_zip({"pom.xml": POM.encode(), APP: b"class App {}\n",
                                          "target/classes/App.class": b"\xca\xfe"}))
    run_dir = tmp_path / "run"
    server = IvasServer([[lib()], []])
    comps = real_parts(server, plan=ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="",
                                                               proposals=[proposal()])]))
    with guard(strict=True, only_under=tmp_path) as g:
        out = chain.run_only(run_dir, upload, make_cfg(), run_id="run-1", journal=lambda e: None,
                             stop=lambda: False, components=comps)
    assert out.status == SUCCEEDED, (out.reason, out.message)
    # build output left out → the baseline scanned original/scan-source.zip
    assert g.calls_to("replace", name="scan-source.zip")
    assert_clean(g, run_dir)


def test_b_zero_findings_delivers_the_scan_source(tmp_path, ivas_env):
    run_dir, root = project(tmp_path)
    server = IvasServer([[]])
    with guard(strict=True, only_under=tmp_path) as g:
        out, _ = chained(run_dir, root, real_parts(server))
    assert out.status == SUCCEEDED and out.delivery.round == 0
    assert len(delivery_swaps(g, run_dir)) == 1
    assert_clean(g, run_dir)


# ---------------------------------------------------------------------------- (c) accept_zip
@pytest.mark.parametrize("extra", [{}, {"target/classes/App.class": b"\xca\xfe"}], ids=["plain", "build-output"])
def test_c_accept_zip_renames_the_partial_tree_with_nothing_held(tmp_path, extra):
    files = {"pom.xml": POM.encode(), APP: b"class App {}\n", "src/main/resources/a.txt": b"a", **extra}
    upload = tmp_path / "in.zip"
    upload.write_bytes(deterministic_zip(files))
    root = tmp_path / "run" / "remediation"
    with guard(strict=True, only_under=tmp_path) as g:
        res = intake_mod.accept_zip(upload, root, intake_mod.limits_from_config(make_cfg()))
    assert res.files == 3
    calls = g.calls_to("replace", name="accepted")
    assert len(calls) == 1 and os.path.basename(calls[0].paths[0]) == ".accepted.partial"
    assert calls[0].caller.startswith("sbmig/remediations/intake.py")
    assert g.findings == [], g.report()


def test_c_accept_zip_in_place_and_after_a_left_partial_tree(tmp_path):
    root = tmp_path / "run" / "remediation"
    paths = RunPaths(root)
    paths.original_zip.parent.mkdir(parents=True)
    paths.original_zip.write_bytes(deterministic_zip({"pom.xml": POM.encode(), APP: b"class App {}\n"}))
    (root / ".accepted.partial" / "src").mkdir(parents=True)       # a killed earlier attempt
    (root / ".accepted.partial" / "src" / "old.java").write_text("x")
    with guard(strict=True, only_under=tmp_path) as g:
        intake_mod.accept_zip(paths.original_zip, root, intake_mod.limits_from_config(make_cfg()))
    assert g.calls_to("rmtree", name=".accepted.partial") and g.calls_to("replace", name="accepted")
    assert g.findings == [], g.report()


# ---------------------------------------------------------------------------- (d) the fix loop
class LoopFixer:
    """The graph's `Fixer`: the real `run_fix_loop` (real `BuildFixLoop`, real git work copy) with the loop tests'
    fakes for the build, the LLM and the script runner. The build fails until App.java says `ok`; `fix=False` never
    writes it."""

    def __init__(self, cfg, fix=True):
        self.cfg, self.fix = cfg, fix
        scripts = [write_script({APP: "class App { ok }\n"})] if fix else \
            [write_script({APP: f"class App {{ /* try {i} */ }}\n"}) for i in range(5)]
        self.llm = LoopLLM(scripts=scripts)

    def _runner(self, ws):
        def build(ws_, call):
            text = (ws_.root / APP).read_text(encoding="utf-8")
            return ok_build(call["goal"]) if "ok" in text else bad_build(compile_error(file=APP), goal=call["goal"])
        return FakeBuildRunner(ws, fn=build)

    def run(self, cfg, tree, work_dir, *, run_id, targets, applied, manifest_paths, first_outcome, stop, deadline,
            journal):
        return fixer_mod.run_fix_loop(self.cfg, tree, work_dir, run_id=run_id, targets=targets, applied=applied,
                                      manifest_paths=manifest_paths, first_outcome=first_outcome, stop=stop,
                                      deadline=deadline, journal=journal, build_runner=self._runner,
                                      llm_fixer=self.llm, script_runner=FakeScriptRunner(), profile=make_profile())


@pytest.mark.parametrize("fix", [True, False], ids=["fixed", "not-fixed"])
def test_d_fix_loop_on_a_failed_candidate(tmp_path, ivas_env, cfg, fix):
    cfg.remediation.fix_loop_max_iterations = 3
    run_dir, root = project(tmp_path)
    server = IvasServer([[lib()], []])
    outcomes = [True, False, True] if fix else [True, False]
    comps = real_parts(server, plan=ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="",
                                                               proposals=[proposal()])]),
                       maven=FakeMaven(outcomes=outcomes), fixer=LoopFixer(cfg, fix=fix))
    with guard(strict=True, only_under=tmp_path) as g:
        out, _ = chained(run_dir, root, comps)
    fixres = out.rounds[0].fix
    assert fixres is not None and fixres.attempted and fixres.fixed is fix, fixres
    if fix:
        assert out.status == SUCCEEDED, (out.reason, out.message)
        with zipfile.ZipFile(chain.chain_paths(run_dir).delivery_zip) as zf:
            assert zf.read(APP) == b"class App { ok }\n"
    else:
        assert out.status == FAILED and out.reason == "candidate_build_failed"
    # the loop's work copy (with its .git) was removed under the guard
    assert g.calls_to("rmtree", name="tree", under=run_dir / "remediation" / "rounds" / "1" / "fixloop")
    assert_clean(g, run_dir)


# ---------------------------------------------------------------------------- (e) a stopped run resumed
def test_e_stopped_after_round_one_and_resumed_replaces_round_ones_result_zip(tmp_path, ivas_env):
    run_dir, root = project(tmp_path, POM2)
    server = IvasServer([[lib(), other()], [other()], []])
    flag = {"stop": False}
    server.after_upload = lambda n: flag.__setitem__("stop", n >= 2)      # stop once round 1's rescan was sent
    with guard(strict=True, only_under=tmp_path) as g1:
        out, _ = chained(run_dir, root, real_parts(server, plan=two_round_plan()), stop=lambda: flag["stop"])
    assert out.status == STOPPED, (out.status, out.reason, out.message)
    assert len(delivery_swaps(g1, run_dir)) == 1
    assert g1.findings == [], g1.report()
    # a new "pod": new components, the stored state and the round-1 result.zip on disk
    flag["stop"] = False
    server.after_upload = None
    plan = ScriptedPlanner([RoundPlan(round=2, source_tree_hash="", policy_hash="",
                                      proposals=[proposal(1, round=2, ga="example:other", old="2.0", new="2.1")])])
    if out.round == 1:                                    # stopped before round 1's evaluate: the plan list starts at 1
        plan = two_round_plan()
        plan.plans.pop(0)
    with guard(strict=True, only_under=tmp_path) as g2:
        out = chain.resume(run_dir, make_cfg(after_migration=True), journal=lambda e: None, stop=lambda: False,
                           components=real_parts(server, plan=plan))
    assert out.status == SUCCEEDED, (out.status, out.reason, out.message)
    assert out.delivery.round == 2 and len(delivery_swaps(g2, run_dir)) == 1
    assert_clean(g2, run_dir)


class Killed(BaseException):
    """The pod dies inside the fix loop (not an Exception: nothing in the graph or the chain catches it)."""


class KilledLoopFixer(LoopFixer):
    """The real loop, killed at its second build (after one LLM try), in THIS process: whatever the loop opened and
    did not close is still open when the resume renames its folder (the exception, and with it every frame, is kept)."""

    def _runner(self, ws):
        inner = super()._runner(ws)
        real_fn, n = inner.fn, {"builds": 0}

        def build(ws_, call):
            n["builds"] += 1
            if n["builds"] >= 2:
                raise Killed()
            return real_fn(ws_, call)
        inner.fn = build
        return inner


def test_e_resume_after_a_fix_loop_killed_in_this_process_renames_its_folder(tmp_path, ivas_env, cfg):
    cfg.remediation.fix_loop_max_iterations = 3
    run_dir, root = project(tmp_path)
    server = IvasServer([[lib()], []])
    plan = [RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()])]
    killed = KilledLoopFixer(cfg, fix=True)
    with guard(strict=True, only_under=tmp_path) as g1, pytest.raises(Killed) as kept:
        chained(run_dir, root, real_parts(server, plan=ScriptedPlanner(list(plan)),
                                          maven=FakeMaven(outcomes=[True, False]), fixer=killed))
    assert g1.findings == [], g1.report()
    paths = chain.chain_paths(run_dir)
    assert (paths.round_dir(1) / "fixloop" / "tree").is_dir()
    # what the service's restart recovery does with a run whose process died: RUNNING → INTERRUPTED
    st = chain.load_state(run_dir)
    st.status = "INTERRUPTED"
    from sbmig.remediations.models import write_json_atomic
    write_json_atomic(paths.state, st.to_dict())
    with guard(strict=True, only_under=tmp_path) as g2:
        out = chain.resume(run_dir, make_cfg(after_migration=True), journal=lambda e: None, stop=lambda: False,
                           components=real_parts(server, plan=ScriptedPlanner(list(plan)),
                                                 maven=FakeMaven(outcomes=[False, True]),
                                                 fixer=LoopFixer(cfg, fix=True)))
    assert kept.value is not None                          # the killed run's traceback is alive until here
    assert out.status == SUCCEEDED, (out.status, out.reason, out.message)
    renamed = [c for c in g2.calls_to("replace", name="fixloop-interrupted-1")]
    assert renamed and os.path.basename(renamed[0].paths[0]) == "fixloop"
    assert g2.calls_to("rmtree", name="tree", under=paths.round_dir(1) / "fixloop-interrupted-1")
    assert_clean(g2, run_dir)


# ============================================================================ 3. IvasHttpClient.upload
def _ivas_settings(base=BASE, **kw):
    s = {"base_url": base, "user_id": "u", "password": "pw", "timeout_seconds": 2, "upload_timeout_seconds": 2,
         "poll_seconds": 0, "analysis_timeout_seconds": 10, "page_size": 10, "project_prefix": "sbmig"}
    s.update(kw)
    return IvasSettings(**s)


def _zip(tmp_path) -> Path:
    p = tmp_path / "delivery" / "result.zip"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"PK\x03\x04" + os.urandom(200_000))
    return p


def _no_open_file_objects(*roots) -> list[str]:
    """Open file objects reachable from the given objects (the client, the session's recorded calls, a traceback)."""
    seen, out, todo = set(), [], list(roots)
    while todo:
        o = todo.pop()
        if id(o) in seen or o is None:
            continue
        seen.add(id(o))
        if isinstance(o, io.IOBase):
            if not o.closed:
                out.append(repr(o))
            continue
        if isinstance(o, dict):
            todo.extend(o.values())
        elif isinstance(o, (list, tuple, set)):
            todo.extend(o)
        elif hasattr(o, "tb_frame"):                         # a traceback: every frame's locals
            todo.append(o.tb_frame.f_locals)
            todo.append(o.tb_next)
        elif isinstance(o, BaseException):
            todo += [o.__traceback__, o.__context__, o.__cause__]
        elif hasattr(o, "__dict__") and type(o).__module__.startswith(("sbmig", "tests")):
            todo.append(vars(o))
    return out


UPLOAD_PATHS = {
    "ok": (upload_ok, None),
    "read-timeout": (requests.exceptions.ReadTimeout("t"), IvasUploadUncertain),
    "reset-mid-body": (partial_then(requests.exceptions.ConnectionError("reset")), IvasUploadUncertain),
    "connect-timeout": (requests.exceptions.ConnectTimeout("t"), IvasError),
    "refused": (connect_refused(), IvasError),
    "other-exception": (partial_then(RuntimeError("boom")), IvasUploadUncertain),
    "400": (Resp(400, text="bad"), IvasError),
    "500": (Resp(500, text="boom"), IvasUploadUncertain),
    "2xx-not-json": (Resp(200, text="<html>"), IvasUploadUncertain),
    "2xx-list": (Resp(200, ["x"]), IvasUploadUncertain),
    "2xx-no-ids": (Resp(200, {"projectId": "p"}), IvasUploadUncertain),
}


@pytest.mark.parametrize("name", sorted(UPLOAD_PATHS))
def test_upload_holds_no_descriptor_after_it_returns_or_raises(tmp_path, name):
    handler, raises = UPLOAD_PATHS[name]
    z = _zip(tmp_path)
    s = FakeSession().on("POST", "/projects/SELF/sourcecode", handler)
    c = IvasHttpClient(_ivas_settings(), session=s, sleep=lambda _x: None)
    err = None
    if raises is None:
        receipt = c.upload(z, "n")
        kept = [receipt]
    else:
        with pytest.raises(raises) as ei:
            c.upload(z, "n")
        err = ei.value
        assert not isinstance(err, IvasUploadUncertain) or raises is IvasUploadUncertain
        kept = []
    assert held_on(z) == []
    assert _no_open_file_objects(c, s.calls, err, *kept) == []
    # and the swap that failed on Windows would now go through
    with guard(strict=True):
        fsops.replace(_zip_next(z), z)


def _zip_next(z: Path) -> Path:
    n = z.with_name(".result.next.zip")
    n.write_bytes(b"next")
    return n


def test_upload_with_a_missing_zip_and_with_a_failed_login_holds_nothing(tmp_path):
    z = _zip(tmp_path)
    s = FakeSession().on("POST", "/session", Resp(401))
    with pytest.raises(IvasError):
        IvasHttpClient(_ivas_settings(), session=s).upload(z, "n")
    assert held_on(z) == []
    with pytest.raises(IvasError):
        IvasHttpClient(_ivas_settings(), session=FakeSession()).upload(tmp_path / "missing.zip", "n")


class _Ivas(BaseHTTPRequestHandler):
    mode = "ok"

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.endswith("/session"):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            return self._json(200, {"token": "tok"})
        mode = type(self).mode
        if mode == "close-early":
            self.rfile.read(1000)
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        n = int(self.headers.get("Content-Length") or 0)
        left = n
        while left > 0:
            chunk = self.rfile.read(min(left, 65536))
            if not chunk:
                break
            left -= len(chunk)
        if mode == "slow":
            time.sleep(1.5)                                # longer than the client's read timeout
        if mode in ("ok", "slow"):
            return self._json(200, {"projectId": "p", "lbrdrAnlId": "a"})
        return self._json(int(mode), {"error": "x"})


@pytest.fixture()
def http_ivas():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Ivas)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("mode,raises", [("ok", None), ("400", IvasError), ("500", IvasUploadUncertain),
                                         ("close-early", IvasUploadUncertain), ("slow", IvasUploadUncertain)])
def test_upload_through_a_real_requests_session_holds_nothing(tmp_path, http_ivas, mode, raises):
    _Ivas.mode = mode
    z = _zip(tmp_path)
    base = f"http://127.0.0.1:{http_ivas.server_address[1]}/api"
    session = requests.Session()
    session.trust_env = False                              # no proxy from the environment
    c = IvasHttpClient(_ivas_settings(base=base, upload_timeout_seconds=1 if mode == "slow" else 5), session=session,
                       sleep=lambda _x: None)
    try:
        if raises is None:
            r = c.upload(z, "n")
            assert (r.project_id, r.analysis_id) == ("p", "a") and r.zip_sha256
        else:
            with pytest.raises(raises):
                c.upload(z, "n")
        assert held_on(z) == []
        assert _no_open_file_objects(c) == []
    finally:
        session.close()
