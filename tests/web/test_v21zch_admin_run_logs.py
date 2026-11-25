"""v2.1z-ch: the admin page's log ZIP of ONE run carries the run's diagnostic files (`logs/`, reports, error lists,
status files, the remediation's text files), never a copy of the project, a ZIP, a diff or an LLM record; the
archive of all runs is unchanged. Hand-written run folders under tmp_path; the real admin password never appears
here (the shared fixture of test_admin patches the built-in hash with a test password)."""
from __future__ import annotations

import builtins
import io
import json
import os
import re
import sys
import urllib.parse
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sbmig.core import scrub
from sbmig.web import admin, redact
from sbmig.web.redact import TEXT_SUFFIXES

from .test_admin import _app, _env, _login, _no_temp, _req, _w, _zip, repo  # noqa: F401 - fixtures

RID = "shop-20261125-100000-abcdef"
OTHER = "bill-20261125-110000-fedcba"

# every member kind the per-run archive adds, at its path in the run folder
ADDED = (
    "errors.md", "errors.jsonl", "report.md", "report.json", "plan.json", "profile.json",
    "job-status.json", "web-status.json", "remediation-job-status.json", "job-local.log", "worker-paths.yml",
    "logs/001-effective-pom-root.log", "logs/002-rewrite-run.log", "logs/003-build-verify.log",
    "logs/deptree-root-19ec6d12.txt", "logs/deptree-root-19ec6d12.meta.json", "logs/effective-depmgmt.json",
    "logs/rewrite-1.yml", "logs/bundled-jars.json", "logs/org.x__lib/build-classpath.log", "logs/org.x__lib/cp.txt",
    "remediation/state.json", "remediation/journal.jsonl", "remediation/report.md", "remediation/report.json",
    "remediation/claim.json", "remediation/delivery/delivery.json", "remediation/baseline/build.log",
    "remediation/scans/baseline.json", "remediation/scans/final-r1.json",
    "remediation/rounds/1/plan.json", "remediation/rounds/1/inventory.json", "remediation/rounds/1/before.json",
    "remediation/rounds/1/apply.json", "remediation/rounds/1/expected.json", "remediation/rounds/1/build.log",
    "remediation/rounds/1/build-fixed.log", "remediation/rounds/1/candidate.ready.json",
    "remediation/rounds/1/work-before/deptree.txt", "remediation/rounds/1/work-before/deptree-1.log",
    "remediation/rounds/1/fixloop/loop.json", "remediation/rounds/1/fixloop/journal.jsonl",
    "remediation/rounds/1/fixloop/logs/001-build-verify-1.log",
    "remediation/rounds/1/fixloop-interrupted-1/journal.jsonl",
)
ALWAYS = ("sbmig.log", "journal.jsonl", "control/request.json")
FOUR = (*ALWAYS, "remediation/remediation.log")

# never in the archive (rule 3), and files the end-of-run pass does not rewrite (rule 4)
EXCLUDED = (
    "project/pom.xml", "project/README.md", "project/build.log", "project/target/app.log",
    "codediff/summary.json", "codediff/files/a.json", "merge-conflicts/mod/src/A.md",
    "control/state.json", "control/remediation-resume.json", "control/notes.txt",
    "result.zip", "source.zip", "changes.patch", "stop.request", ".x.123.redact.tmp", "llm-records/llm-0001.json",
    "remediation/fix.patch", "remediation/rounds/1/candidate.patch", "remediation/rounds/1/fix.patch",
    "remediation/accepted/notes.md", "remediation/accepted/pom.xml", "remediation/original/manifest.json",
    "remediation/original/source.zip", "remediation/baseline/tree/build.log", "remediation/baseline/tree/x.json",
    "remediation/rounds/1/candidate/target/x.log", "remediation/rounds/1/candidate/notes.md",
    "remediation/rounds/1/applied/pom.xml", "remediation/rounds/1/applied/notes.json",
    "remediation/rounds/1/fixloop/tree/app.log", "remediation/rounds/1/fixloop/tree/.git/config",
    "remediation/rounds/1/fixloop/fixed/src/A.md", "remediation/delivery/result.zip",
    "remediation/delivery/result-r1-verified.zip", "remediation/delivery/.result.next.json",
    "logs/review-input.md", "logs/llm-0002.json", "logs/rewrite.zip", "logs/lib.jar", "logs/A.class",
    "logs/changes.patch", "logs/x.diff", "logs/effective-pom.xml", "logs/target/x.log", "logs/.hidden/x.log",
    "logs/.cache.json",
)


def _fill(rd: Path, names, text: str | None = None) -> None:
    for n in names:
        _w(rd / n, text if text is not None else f"{n} contents\n")


def _full_run(out: Path, rid: str = RID) -> Path:
    rd = out / rid
    _req(rd, "shop.zip", "2026-11-25T10:00:00+00:00")
    _fill(rd, [m for m in FOUR if m != "control/request.json"])
    _fill(rd, ADDED)
    _fill(rd, EXCLUDED)
    return rd


def _client(tmp_path, repo) -> tuple[TestClient, object]:  # noqa: F811
    app, _ = _app(tmp_path, repo)
    c = TestClient(app)
    _login(c)
    return c, app


def _skipped(files: dict[str, bytes]) -> dict[str, str]:
    body = files[admin.SKIPPED_NAME].decode()
    head, _, rest = body.partition("\n\n")
    assert "left out" in head
    rows = [ln.split("\t", 1) for ln in rest.splitlines() if ln]
    names = [r[0] for r in rows]
    assert len(names) == len(set(names)), "a file is named twice"
    return dict(rows)


# ---------------------------------------------------------------------------
# what is in, what is out
# ---------------------------------------------------------------------------
def test_every_added_member_kind_is_in_at_its_path_and_nothing_excluded_is(tmp_path, repo):  # noqa: F811
    out = tmp_path / "out"
    rd = _full_run(out)
    c, _ = _client(tmp_path, repo)
    files = _zip(c.get(f"/admin/api/runs/{RID}/logs.zip"))
    assert sorted(files) == sorted(f"{RID}/{m}" for m in (*FOUR, *ADDED))
    for name, data in files.items():
        assert "\\" not in name and name.startswith(f"{RID}/")
        assert data == (out / name).read_bytes()
    assert admin.SKIPPED_NAME not in files and "README.txt" not in files
    # every member has a suffix the end-of-run pass rewrites (rule 4)
    for name in files:
        if not name.endswith("control/request.json"):
            assert Path(name).suffix in TEXT_SUFFIXES, name
    assert (rd / "project" / "README.md").is_file()             # the excluded files exist; they were left out
    _no_temp(tmp_path)


def test_links_into_excluded_trees_or_out_of_the_run_folder_are_never_followed(tmp_path, repo):  # noqa: F811
    out = tmp_path / "out"
    rd = _full_run(out)
    outside = tmp_path / "outside"
    _w(outside / "secret.log", "outside the run folder\n")
    _w(outside / "deep" / "d.log", "outside, in a folder\n")
    os.symlink(rd / "project" / "build.log", rd / "logs" / "004-link.log")       # a file link into project/
    os.symlink(rd / "project", rd / "logs" / "projlink")                         # a folder link into project/
    os.symlink(outside / "secret.log", rd / "extra.log")                         # a top-level link out of the run
    os.symlink(outside / "deep", rd / "remediation" / "rounds" / "2")            # a round that is a link
    os.symlink(rd / "codediff", rd / "remediation" / "scans" / "cd")
    c, _ = _client(tmp_path, repo)
    files = _zip(c.get(f"/admin/api/runs/{RID}/logs.zip"))
    assert sorted(files) == sorted(f"{RID}/{m}" for m in (*FOUR, *ADDED))
    blob = b"".join(files.values())
    assert b"outside" not in blob
    assert not [n for n in EXCLUDED if f"{n} contents".encode() in blob]


def test_a_logs_folder_that_is_a_link_is_left_out_and_so_is_a_linked_remediation(tmp_path, repo):  # noqa: F811
    out = tmp_path / "out"
    rd = out / RID
    _req(rd, "shop.zip", "2026-11-25T10:00:00+00:00")
    _fill(rd, ("sbmig.log", "journal.jsonl", "report.md"))
    _fill(rd / "project", ("logs/001-x.log", "remediation/state.json", "remediation/remediation.log"))
    os.symlink(rd / "project" / "logs", rd / "logs")
    os.symlink(rd / "project" / "remediation", rd / "remediation")
    c, _ = _client(tmp_path, repo)
    files = _zip(c.get(f"/admin/api/runs/{RID}/logs.zip"))
    assert sorted(files) == sorted(f"{RID}/{m}" for m in (*ALWAYS, "report.md"))


def test_a_run_folder_without_logs_behaves_as_before(tmp_path, repo):  # noqa: F811
    out = tmp_path / "out"
    rd = out / RID
    _req(rd, "shop.zip", "2026-11-25T10:00:00+00:00")
    _fill(rd, ("sbmig.log", "journal.jsonl"))
    c, _ = _client(tmp_path, repo)
    files = _zip(c.get(f"/admin/api/runs/{RID}/logs.zip"))
    assert sorted(files) == sorted(f"{RID}/{m}" for m in ALWAYS)
    assert c.get(f"/admin/api/runs/{RID}/logs/check").json() == {
        "ok": True, "bytes": sum((rd / m).stat().st_size for m in ALWAYS), "runs": 1, "skipped": 0}
    # a run without any log file is still the README archive, even with a report beside it
    rd2 = out / OTHER
    _req(rd2, "bill.zip", "2026-11-25T11:00:00+00:00")
    _fill(rd2, ("report.json", "logs/001-x.log"))
    assert _zip(c.get(f"/admin/api/runs/{OTHER}/logs.zip")) == {"README.txt": admin.EMPTY_README.encode()}
    assert c.get(f"/admin/api/runs/{OTHER}/logs/check").json() == {"ok": True, "bytes": 0, "runs": 0, "skipped": 0}


# ---------------------------------------------------------------------------
# the end-of-run pass (rule 4)
# ---------------------------------------------------------------------------
def test_before_the_final_pass_only_the_always_in_files_go_and_the_rest_is_named(tmp_path, repo):  # noqa: F811
    out = tmp_path / "out"
    _full_run(out)
    c, app = _client(tmp_path, repo)
    app.state.remediations._withheld = lambda rid: True
    files = _zip(c.get(f"/admin/api/runs/{RID}/logs.zip"))
    assert sorted(n for n in files if n != admin.SKIPPED_NAME) == sorted(f"{RID}/{m}" for m in ALWAYS)
    skipped = _skipped(files)
    assert sorted(skipped) == sorted(f"{RID}/{m}" for m in ("remediation/remediation.log", *ADDED))
    assert set(skipped.values()) == {admin.REASON_PENDING}
    check = c.get(f"/admin/api/runs/{RID}/logs/check").json()
    assert check["skipped"] == len(skipped) and check["runs"] == 1
    # the archive of all runs is exactly as before: the three files, nothing named
    allf = _zip(c.get("/admin/api/logs.zip"))
    assert sorted(allf) == sorted(f"{RID}/{m}" for m in ALWAYS)


def test_an_active_remediation_resume_withholds_the_same_files(tmp_path, repo, monkeypatch):  # noqa: F811
    out = tmp_path / "out"
    _full_run(out)
    c, app = _client(tmp_path, repo)
    rem = app.state.remediations
    assert _zip(c.get(f"/admin/api/runs/{RID}/logs.zip")).keys() >= {f"{RID}/remediation/state.json"}
    monkeypatch.setattr(rem, "_resume_active", lambda rid: True)          # a resume pod writes the remediation
    files = _zip(c.get(f"/admin/api/runs/{RID}/logs.zip"))
    assert sorted(n for n in files if n != admin.SKIPPED_NAME) == sorted(f"{RID}/{m}" for m in ALWAYS)
    assert set(_skipped(files).values()) == {admin.REASON_PENDING}


def test_no_rule_reachable_withholds(tmp_path):
    out = tmp_path / "out"
    rd = _full_run(out)
    plan = admin.plan_run_logs_zip(rd, admin.remediation_rule(None))
    assert sorted(a for a, _, _ in plan.members) == sorted(f"{RID}/{m}" for m in ALWAYS)


# ---------------------------------------------------------------------------
# limits (rule 5)
# ---------------------------------------------------------------------------
def test_a_file_over_the_per_file_limit_is_named_with_its_size(tmp_path, repo, monkeypatch):  # noqa: F811
    out = tmp_path / "out"
    rd = _full_run(out)
    (rd / "logs" / "002-rewrite-run.log").write_bytes(b"r" * 5000)
    monkeypatch.setattr(admin, "ZIP_FILE_LIMIT", 4096)
    c, _ = _client(tmp_path, repo)
    assert c.get(f"/admin/api/runs/{RID}/logs/check").json()["skipped"] == 1
    files = _zip(c.get(f"/admin/api/runs/{RID}/logs.zip"))
    assert f"{RID}/logs/002-rewrite-run.log" not in files
    skipped = _skipped(files)
    assert list(skipped) == [f"{RID}/logs/002-rewrite-run.log"]
    assert "too large" in skipped[f"{RID}/logs/002-rewrite-run.log"] and "5000" in skipped[f"{RID}/logs/002-rewrite-run.log"]


def test_over_the_total_cap_members_are_left_out_in_the_order_until_it_fits(tmp_path, monkeypatch,
                                                                           repo):  # noqa: F811
    out = tmp_path / "out"
    rd = _full_run(out)
    (rd / "logs" / "003-build-verify.log").write_bytes(b"b\n" * 1500)
    (rd / "remediation" / "baseline" / "build.log").write_bytes(b"c\n" * 1500)
    (rd / "remediation" / "remediation.log").write_bytes(b"r\n" * 2500)
    monkeypatch.setattr(admin, "ZIP_TOTAL_LIMIT", 4096)
    full = admin.plan_run_logs_zip(rd, lambda rid: False)
    c, _ = _client(tmp_path, repo)
    check = c.get(f"/admin/api/runs/{RID}/logs/check")
    assert check.status_code == 200 and check.json()["bytes"] <= 4096
    files = _zip(c.get(f"/admin/api/runs/{RID}/logs.zip"))
    skipped = _skipped(files)
    assert set(skipped.values()) == {"over the archive's total size limit (4096 bytes)"}
    for keep in ALWAYS:
        assert f"{RID}/{keep}" in files
    # the left-out files are exactly the shortest prefix of the drop order that makes the rest fit
    sizes = {a[len(RID) + 1:]: s for a, _, s in full.members}
    sizes.update({a[len(RID) + 1:]: (rd / a[len(RID) + 1:]).stat().st_size for a in skipped})
    order = admin._drop_order(list(sizes), set(ALWAYS))
    total, want = sum(sizes.values()), []
    for rel in order:
        if total <= 4096:
            break
        want.append(f"{RID}/{rel}")
        total -= sizes[rel]
    assert sorted(skipped) == sorted(want) and want[0] == f"{RID}/logs/001-effective-pom-root.log"
    assert sum(len(v) for n, v in files.items() if n != admin.SKIPPED_NAME) == total
    # the archive of all runs keeps its 413 (its four members, remediation.log included, are over the cap)
    r = c.get("/admin/api/logs.zip")
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    # the three always-in files alone over the cap: the one-run archive answers 413 too
    monkeypatch.setattr(admin, "ZIP_TOTAL_LIMIT", 10)
    assert c.get(f"/admin/api/runs/{RID}/logs/check").status_code == 413
    assert not admin.ZIP_GATE.locked()


def test_over_the_member_cap_the_lowest_numbered_logs_go_first(tmp_path, repo, monkeypatch):  # noqa: F811
    out = tmp_path / "out"
    rd = out / RID
    _req(rd, "shop.zip", "2026-11-25T10:00:00+00:00")
    _fill(rd, ("sbmig.log", "journal.jsonl", "report.md", "remediation/state.json", "logs/effective-depmgmt.json"))
    for n in range(1, 13):
        _w(rd / "logs" / f"{n:03d}-build-verify-{n}.log", f"build {n}\n")
    _w(rd / "logs" / "1000-rewrite-run.log", "the last call\n")        # a number past 999 sorts by value
    monkeypatch.setattr(admin, "RUN_ZIP_MAX_FILES", 10)
    c, _ = _client(tmp_path, repo)
    files = _zip(c.get(f"/admin/api/runs/{RID}/logs.zip"))
    members = sorted(n for n in files if n != admin.SKIPPED_NAME)
    assert len(members) == 10
    # 19 candidates, 9 over: logs 001 to 009 go; the always-in files, the unnumbered one and the newest logs stay
    dropped = [f"{RID}/logs/{n:03d}-build-verify-{n}.log" for n in range(1, 10)]
    skipped = _skipped(files)
    assert sorted(skipped) == sorted(dropped)
    assert all("per-run file limit" in why and "10 files" in why for why in skipped.values())
    for keep in ("sbmig.log", "journal.jsonl", "control/request.json", "report.md", "remediation/state.json",
                 "logs/effective-depmgmt.json", "logs/012-build-verify-12.log", "logs/1000-rewrite-run.log"):
        assert f"{RID}/{keep}" in files, keep
    assert c.get(f"/admin/api/runs/{RID}/logs/check").json()["skipped"] == 9


def test_logs_use_at_most_half_of_the_listing_budget_and_remediation_is_reached(tmp_path, monkeypatch):
    out = tmp_path / "out"
    rd = out / RID
    _req(rd, "shop.zip", "2026-11-25T10:00:00+00:00")
    _fill(rd, ("sbmig.log", "journal.jsonl"))
    for n in range(1, 40):
        _w(rd / "logs" / f"{n:03d}-x.log", "x\n")
    _fill(rd, ("remediation/state.json", "remediation/scans/baseline.json"))
    monkeypatch.setattr(admin, "RUN_ZIP_SCAN_LIMIT", 20)
    listed: list[str] = []
    real = admin._scandir
    monkeypatch.setattr(admin, "_scandir", lambda p: listed.append(os.path.relpath(str(p), str(rd))) or real(p))
    plan = admin.plan_run_logs_zip(rd, lambda rid: False)
    # top: 5 entries; logs/: 10 of its 39 (half the budget), one line; remediation/ listed completely
    assert listed == [".", "logs", "remediation", "remediation/scans"]
    scan_lines = [a for a, why in plan.left_out if "is not complete" in why]
    assert scan_lines == [f"{RID}/logs/"]
    assert "at most 20 folder entries" in dict(plan.left_out)[f"{RID}/logs/"]
    members = {a for a, _, _ in plan.members}
    assert {f"{RID}/remediation/state.json", f"{RID}/remediation/scans/baseline.json"} <= members
    assert len([m for m in members if m.startswith(f"{RID}/logs/")]) == 10


def test_every_folder_not_listed_completely_gets_its_line(tmp_path, monkeypatch):
    out = tmp_path / "out"
    rd = out / RID
    _req(rd, "shop.zip", "2026-11-25T10:00:00+00:00")
    _fill(rd, ("sbmig.log", "journal.jsonl", "logs/a/1.log", "logs/b/1.log", "logs/c.log", "logs/d.log",
               "remediation/state.json"))
    monkeypatch.setattr(admin, "RUN_ZIP_SCAN_LIMIT", 8)        # top 5 entries, then 3 left (logs/ may use 4)
    plan = admin.plan_run_logs_zip(rd, lambda rid: False)
    lines = sorted(a for a, why in plan.left_out if "is not complete" in why)
    # logs/ was cut; its subfolders seen before the cut and remediation/ were never listed: each has its line
    assert f"{RID}/logs/" in lines and f"{RID}/remediation/" in lines
    seen_dirs = [a for a in lines if a.startswith(f"{RID}/logs/") and a != f"{RID}/logs/"]
    assert all(a in (f"{RID}/logs/a/", f"{RID}/logs/b/") for a in seen_dirs)
    assert {f"{RID}/sbmig.log", f"{RID}/journal.jsonl", f"{RID}/control/request.json"} <= {a for a, _, _ in plan.members}
    assert admin.RUN_ZIP_SCAN_LIMIT == 8 and admin.RUN_ZIP_SCAN_LIMIT // 2 == 4


def test_the_scan_limit_is_the_contract_value():
    assert admin.RUN_ZIP_SCAN_LIMIT == 20000 and admin.RUN_ZIP_MAX_FILES == 2000
    assert admin.RUN_LINE_LIMIT == 1024 * 1024


# ---------------------------------------------------------------------------
# planning is cheap; copying is safe
# ---------------------------------------------------------------------------
def test_planning_reads_no_file_and_never_lists_an_excluded_tree(tmp_path, monkeypatch):
    out = tmp_path / "out"
    rd = _full_run(out)
    for i in range(50):                                          # a big project tree that must not be walked
        _w(rd / "project" / f"d{i}" / "x.log", "x\n")
    # Measured on the planning call alone. A process-wide patch of os.scandir also sees the root conftest's disk watch
    # (thread "bounded-watch", scripts/bounded.py tree_mb walking the basetemp, this test's project/ included), which
    # once failed this test in a parallel run. So: the listing goes through the module's own seam `admin._scandir`
    # (only planning code reads it), and calls of the C functions are counted by a profile hook, which
    # `sys.setprofile` installs for the calling thread only.
    listed: list[str] = []
    real_scandir = admin._scandir

    def scandir(path):
        listed.append(os.path.relpath(str(path), str(rd)).replace(os.sep, "/"))
        return real_scandir(path)

    opens = {id(f): name for name, f in (("open", builtins.open), ("io.open_code", io.open_code),
                                         ("os.open", os.open))}
    called: list[str] = []
    scandir_calls = 0

    def profile(frame, event, arg):
        nonlocal scandir_calls
        if event == "c_call":
            if id(arg) in opens:
                called.append(opens[id(arg)])
            elif arg is os.scandir:
                scandir_calls += 1

    monkeypatch.setattr(admin, "_scandir", scandir)
    before = sys.getprofile()
    sys.setprofile(profile)
    try:
        plan = admin.plan_run_logs_zip(rd, lambda rid: False)
    finally:
        sys.setprofile(before)
    assert called == [], f"planning opened a file: {called}"
    # every listing of the planning call went through the seam, none past it
    assert scandir_calls == len(listed)
    assert len(plan.members) == len(FOUR) + len(ADDED)
    assert listed[0] == "."
    pruned = re.compile(r"(^|/)(project|codediff|merge-conflicts|accepted|original|tree|candidate|applied|fixed|"
                        r"target|llm-records|control|\.git|\.hidden)(/|$)")
    assert not [p for p in listed if pruned.search(p) or p.startswith("..")], listed


def test_a_member_that_vanishes_shrinks_or_turns_into_a_link_does_not_break_the_archive(tmp_path):
    out = tmp_path / "out"
    rd = _full_run(out)
    (rd / "logs" / "003-build-verify.log").write_bytes(b"v" * 4000)
    outside = tmp_path / "outside.log"
    outside.write_text("outside the run folder\n")
    plan = admin.plan_run_logs_zip(rd, lambda rid: False)
    (rd / "report.md").unlink()                                          # vanished
    (rd / "logs" / "003-build-verify.log").write_bytes(b"v" * 10)       # shrank
    (rd / "errors.md").unlink()
    os.symlink(outside, rd / "errors.md")                                # swapped for a link
    path = admin.write_logs_zip(plan)
    try:
        with zipfile.ZipFile(path) as zf:
            assert zf.testzip() is None
            names = zf.namelist()
            assert f"{RID}/report.md" not in names and f"{RID}/errors.md" not in names
            assert zf.read(f"{RID}/logs/003-build-verify.log") == b"v" * 10
            assert b"outside" not in b"".join(zf.read(n) for n in names)
    finally:
        path.unlink()


def test_a_member_swapped_for_a_fifo_is_skipped_without_blocking(tmp_path):
    out = tmp_path / "out"
    rd = _full_run(out)
    plan = admin.plan_run_logs_zip(rd, lambda rid: False)
    (rd / "report.md").unlink()
    os.mkfifo(rd / "report.md")                       # opening it for reading without O_NONBLOCK would wait forever
    path = admin.write_logs_zip(plan)
    try:
        with zipfile.ZipFile(path) as zf:
            assert f"{RID}/report.md" not in zf.namelist() and f"{RID}/errors.md" in zf.namelist()
    finally:
        path.unlink()


def test_the_file_cap_counts_only_files_that_can_go_in(tmp_path, monkeypatch):
    out = tmp_path / "out"
    rd = out / RID
    _req(rd, "shop.zip", "2026-11-25T10:00:00+00:00")
    _fill(rd, ("sbmig.log", "journal.jsonl", "report.md"))
    for n in range(1, 4):
        _w(rd / "logs" / f"{n:03d}-x.log", "x\n")
    (rd / "logs" / "004-big.log").write_bytes(b"b" * 5000)
    monkeypatch.setattr(admin, "ZIP_FILE_LIMIT", 4096)
    monkeypatch.setattr(admin, "RUN_ZIP_MAX_FILES", 7)
    plan = admin.plan_run_logs_zip(rd, lambda rid: False)
    # 8 candidates, one too large: the 7 others fit the cap, none is dropped for it
    assert dict(plan.left_out) == {f"{RID}/logs/004-big.log": admin._reason_large(5000)}
    assert len(plan.members) == 7


# ---------------------------------------------------------------------------
# a file the end-of-run pass takes for binary (rule 4, last sentence)
# ---------------------------------------------------------------------------
SECRET = "TOPSECRETVALUE123"
# (name, bytes before the secret); a NUL in the first 8 KiB makes a file binary for the pass
HEADS = (
    ("plain", b"plain text\n"),
    ("nul-first", b"\x00rest\n"),
    ("nul-at-8191", b"a " * 4095 + b"a" + b"\x00"),          # spaced: a long run of letters is left out anyway
    ("nul-at-8192", b"a " * 4096 + b"\x00"),
    ("nul-far", b"a " * 10000 + b"\x00tail"),
    ("utf16", "로그 한 줄\n".encode("utf-16")),
    ("high-bytes", b"\xff\xfe\x80 no nul here\n"),
    ("empty-head", b""),
)


def test_the_binary_rule_agrees_with_the_end_of_run_pass(tmp_path):
    """Both sides on one table: the pass (`redact.redact_file`, which leaves a file it takes for binary unchanged
    although it holds a secret) and the one-run archive (a member is in exactly when the pass would rewrite it)."""
    out = tmp_path / "out"
    rd = out / RID
    _req(rd, "shop.zip", "2026-11-25T10:00:00+00:00")
    _fill(rd, ("sbmig.log", "journal.jsonl"))
    copies = tmp_path / "copies"
    copies.mkdir()
    pass_binary: dict[str, bool] = {}
    for name, head in HEADS:
        data = head + f" {SECRET}\n".encode()
        (rd / "logs").mkdir(exist_ok=True)
        (rd / "logs" / f"{name}.log").write_bytes(data)
        c = copies / f"{name}.log"
        c.write_bytes(data)
        rewritten = redact.redact_file(c, [SECRET])
        assert rewritten != (SECRET.encode() in c.read_bytes())
        pass_binary[name] = not rewritten
        assert admin._binary_head(data[:admin.BINARY_HEAD_BYTES]) is pass_binary[name], name
    assert pass_binary == {"plain": False, "nul-first": True, "nul-at-8191": True, "nul-at-8192": False,
                           "nul-far": False, "utf16": True, "high-bytes": False, "empty-head": False}
    path = admin.write_logs_zip(admin.plan_run_logs_zip(rd, lambda rid: False))
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            skipped = _skipped({admin.SKIPPED_NAME: zf.read(admin.SKIPPED_NAME)})
    finally:
        path.unlink()
    for name, binary in pass_binary.items():
        assert (f"{RID}/logs/{name}.log" in names) is not binary, name
    assert skipped == {f"{RID}/logs/{n}.log": "not a text file" for n, b in pass_binary.items() if b}


def test_a_binary_member_is_named_and_the_always_in_files_and_the_all_runs_archive_are_unchanged(
        tmp_path, repo):  # noqa: F811
    out = tmp_path / "out"
    rd = _full_run(out)
    (rd / "logs" / "002-rewrite-run.log").write_bytes(b"[INFO] start\n\x00\x01binary tail\n")
    (rd / "remediation" / "remediation.log").write_bytes(b"\x00 remediation\n")
    (rd / "sbmig.log").write_bytes(b"line\n\x00 a NUL in the live log\n")
    (rd / "journal.jsonl").write_bytes(b'{"e": 1}\n\x00\n')
    c, _ = _client(tmp_path, repo)
    check = c.get(f"/admin/api/runs/{RID}/logs/check").json()
    files = _zip(c.get(f"/admin/api/runs/{RID}/logs.zip"))
    binary = (f"{RID}/logs/002-rewrite-run.log", f"{RID}/remediation/remediation.log")
    assert sorted(n for n in files if n != admin.SKIPPED_NAME) == sorted(
        f"{RID}/{m}" for m in (*FOUR, *ADDED) if f"{RID}/{m}" not in binary)
    assert _skipped(files) == {b: "not a text file" for b in binary}
    # the three always-in files are copied as before, NUL or not
    assert files[f"{RID}/sbmig.log"] == (rd / "sbmig.log").read_bytes()
    assert files[f"{RID}/journal.jsonl"] == (rd / "journal.jsonl").read_bytes()
    # the check reads no file: it does not know the binary ones (documented undercount)
    assert check["skipped"] == 0
    assert check["bytes"] == sum(len(v) for n, v in files.items() if n != admin.SKIPPED_NAME) + sum(
        (out / b).stat().st_size for b in binary)
    # the archive of all runs carries its four members as before, the binary remediation.log included
    allf = _zip(c.get("/admin/api/logs.zip"))
    assert sorted(allf) == sorted(f"{RID}/{m}" for m in FOUR)
    assert allf[f"{RID}/remediation/remediation.log"] == b"\x00 remediation\n"


def test_a_text_member_that_grew_is_still_cut_at_its_planned_size(tmp_path):
    out = tmp_path / "out"
    rd = _full_run(out)
    log_file = rd / "logs" / "003-build-verify.log"
    log_file.write_bytes(b"line one\nline tw")                    # the last line is still being written
    plan = admin.plan_run_logs_zip(rd, lambda rid: False)
    with log_file.open("ab") as fh:
        fh.write(b"o\nline three\n")
    path = admin.write_logs_zip(plan)
    try:
        with zipfile.ZipFile(path) as zf:
            # the bytes READ stop at the planned size; the line cut there is not written (a secret could be half in)
            assert zf.read(f"{RID}/logs/003-build-verify.log") == b"line one\n"
            # a file that did not grow keeps its last line without a newline
            assert zf.read(f"{RID}/report.md") == (rd / "report.md").read_bytes()
    finally:
        path.unlink()


# ---------------------------------------------------------------------------
# redaction while copying (rule 4): the archive does not rely on the end-of-run pass
# ---------------------------------------------------------------------------
ENV_SECRET = "sk-live-ENVSECRET-0123456789"


def _secret_env(monkeypatch) -> None:
    monkeypatch.setenv("SBMIG_ADMIN_TEST_API_KEY", ENV_SECRET)
    assert ENV_SECRET in redact.live_secret_values()


def _spellings(value: str) -> list[str]:
    return [value, json.dumps(value)[1:-1], urllib.parse.quote(value, safe="")]


def _assert_no_secret(files: dict[str, bytes], value: str = ENV_SECRET) -> None:
    for name, data in files.items():
        if name.endswith(("/sbmig.log", "/journal.jsonl", "/control/request.json")):
            continue
        for spelled in _spellings(value):
            assert spelled.encode() not in data, (name, spelled)


def test_every_added_member_kind_is_redacted_although_no_pass_touched_it(tmp_path, monkeypatch,
                                                                        repo):  # noqa: F811
    _secret_env(monkeypatch)
    out = tmp_path / "out"
    rd = _full_run(out)
    for n in ADDED:
        text = (f"{n}: token {ENV_SECRET}\n" if not n.endswith((".json", ".jsonl"))
                else json.dumps({"k": f"x {ENV_SECRET} y"}) + "\n")
        _w(rd / n, text)
    for n in ALWAYS:
        if n != "control/request.json":
            _w(rd / n, f"raw {ENV_SECRET}\n")
    c, _ = _client(tmp_path, repo)
    files = _zip(c.get(f"/admin/api/runs/{RID}/logs.zip"))
    assert sorted(n for n in files if n != admin.SKIPPED_NAME) == sorted(f"{RID}/{m}" for m in (*FOUR, *ADDED))
    _assert_no_secret(files)
    for n in ADDED:
        assert b"***" in files[f"{RID}/{n}"], n
    # a JSON member keeps its structure (the mask lands inside the string)
    assert json.loads(files[f"{RID}/report.json"]) == {"k": "x *** y"}
    # the three always-in files are copied byte for byte, as before
    for n in ALWAYS:
        assert files[f"{RID}/{n}"] == (rd / n).read_bytes(), n


def test_every_spelling_of_a_value_is_masked(tmp_path, monkeypatch):
    value = 'pa"ss wo/rd+ENV-0123'
    monkeypatch.setenv("SBMIG_ADMIN_TEST_PASSWORD", value)
    out = tmp_path / "out"
    rd = _full_run(out)
    _w(rd / "logs" / "002-rewrite-run.log", "".join(f"form {v}\n" for v in _spellings(value)))
    path = admin.write_logs_zip(admin.plan_run_logs_zip(rd, lambda rid: False))
    try:
        with zipfile.ZipFile(path) as zf:
            data = zf.read(f"{RID}/logs/002-rewrite-run.log")
    finally:
        path.unlink()
    assert data == b"form ***\n" * 3


def test_the_two_reviewer_pass_failures_no_longer_leak(tmp_path, monkeypatch):
    _secret_env(monkeypatch)
    out = tmp_path / "out"
    # (a) the pass could not rewrite a file (its folder is not writable)
    rd = _full_run(out)
    sub = rd / "logs" / "mod"
    _w(sub / "build.log", f"password={ENV_SECRET}\n")
    os.chmod(sub, 0o555)
    try:
        assert redact.redact_run_dir(rd, [ENV_SECRET]) >= 0
        assert ENV_SECRET in (sub / "build.log").read_text()
        path = admin.write_logs_zip(admin.plan_run_logs_zip(rd, lambda rid: False))
        with zipfile.ZipFile(path) as zf:
            assert zf.read(f"{RID}/logs/mod/build.log") == b"password=***\n"
        path.unlink()
    finally:
        os.chmod(sub, 0o755)
    # (b) one file aborts the whole pass (RecursionError); files after it stay unredacted on disk
    rd2 = _full_run(out, OTHER)
    _w(rd2 / "logs" / "a.json", "[" * 200000 + "]" * 200000)
    _w(rd2 / "logs" / "b.log", f"key {ENV_SECRET}\n")
    with pytest.raises(RecursionError):
        redact.redact_run_dir(rd2, [ENV_SECRET])
    path = admin.write_logs_zip(admin.plan_run_logs_zip(rd2, lambda rid: False))
    try:
        with zipfile.ZipFile(path) as zf:
            assert zf.read(f"{OTHER}/logs/b.log") == b"key ***\n"
            assert f"{OTHER}/logs/a.json" not in zf.namelist()
            skipped = _skipped({admin.SKIPPED_NAME: zf.read(admin.SKIPPED_NAME)})
            assert skipped == {f"{OTHER}/logs/a.json": "cannot be redacted (RecursionError)"}
    finally:
        path.unlink()


@pytest.mark.parametrize("control", ["missing", "truncated"])
def test_a_run_without_a_readable_control_state_does_not_leak(tmp_path, repo, monkeypatch, control):  # noqa: F811
    from sbmig.web import runs
    monkeypatch.setattr(runs, "output_scrubbing", lambda: True)
    _secret_env(monkeypatch)
    out = tmp_path / "out"
    rd = _full_run(out)
    if control == "truncated":
        _w(rd / "control" / "state.json", '{"run_id": "' + RID + '", "state": "running"')
    _w(rd / "logs" / "003-build-verify.log", f"Downloading from x: https://u:{ENV_SECRET}@nexus/repo\n")
    _w(rd / "report.md", f"token {ENV_SECRET}\n")
    c, _ = _client(tmp_path, repo)
    files = _zip(c.get(f"/admin/api/runs/{RID}/logs.zip"))
    _assert_no_secret(files)
    assert files[f"{RID}/report.md"] == b"token ***\n"


def test_url_userinfo_is_masked_with_no_secret_value_set(tmp_path, monkeypatch):
    monkeypatch.setattr(admin.redact, "live_secret_values", list)
    out = tmp_path / "out"
    rd = _full_run(out)
    _w(rd / "logs" / "002-rewrite-run.log", "get https://deploy:hunter2-pw@nexus.example/repo/x.pom\n")
    _w(rd / "report.json", json.dumps({"url": "git+ssh://bot:t0ken@git.example/p.git"}))
    path = admin.write_logs_zip(admin.plan_run_logs_zip(rd, lambda rid: False))
    try:
        with zipfile.ZipFile(path) as zf:
            assert zf.read(f"{RID}/logs/002-rewrite-run.log") == b"get https://***@nexus.example/repo/x.pom\n"
            assert json.loads(zf.read(f"{RID}/report.json")) == {"url": "git+ssh://***@git.example/p.git"}
    finally:
        path.unlink()


@pytest.mark.parametrize("whole", [True, False])
def test_a_secret_split_across_read_chunks_is_masked(tmp_path, monkeypatch, whole):
    _secret_env(monkeypatch)
    monkeypatch.setattr(admin, "ZIP_CHUNK", 7)                 # every secret spans several reads
    if not whole:
        monkeypatch.setattr(admin, "RUN_WHOLE_REDACT_BYTES", 0)
    out = tmp_path / "out"
    rd = _full_run(out)
    _w(rd / "logs" / "002-rewrite-run.log", "".join(f"{i} {ENV_SECRET} end\n" for i in range(50)))
    path = admin.write_logs_zip(admin.plan_run_logs_zip(rd, lambda rid: False))
    try:
        with zipfile.ZipFile(path) as zf:
            data = zf.read(f"{RID}/logs/002-rewrite-run.log")
    finally:
        path.unlink()
    assert data == "".join(f"{i} *** end\n" for i in range(50)).encode()


def test_a_line_over_the_line_limit_leaves_a_large_member_out_and_never_leaks(tmp_path, monkeypatch):
    _secret_env(monkeypatch)
    monkeypatch.setattr(admin, "RUN_WHOLE_REDACT_BYTES", 0)    # the members are "large": line by line
    out = tmp_path / "out"
    rd = _full_run(out)
    limit = admin.RUN_LINE_LIMIT
    words = b"ab " * (limit // 3)
    # a secret that straddles the line limit and a read-chunk boundary, on one line longer than the limit
    edge = len(words) - len(ENV_SECRET) // 2
    assert edge % admin.ZIP_CHUNK != 0 and edge < limit
    long_line = words[:edge] + ENV_SECRET.encode() + b" x" * 100 + b"\n"
    assert len(long_line) > limit
    (rd / "logs" / "002-rewrite-run.log").write_bytes(b"short\n" + long_line + b"tail\n")
    # a line just under the limit is still copied (redacted)
    under = words[:limit - len(ENV_SECRET) - 2] + ENV_SECRET.encode() + b"\n"
    (rd / "logs" / "003-build-verify.log").write_bytes(under)
    path = admin.write_logs_zip(admin.plan_run_logs_zip(rd, lambda rid: False))
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            skipped = _skipped({admin.SKIPPED_NAME: zf.read(admin.SKIPPED_NAME)})
            kept = zf.read(f"{RID}/logs/003-build-verify.log")
            blob = b"".join(zf.read(n) for n in names)
    finally:
        path.unlink()
    assert f"{RID}/logs/002-rewrite-run.log" not in names
    assert skipped[f"{RID}/logs/002-rewrite-run.log"] == admin._reason_long()
    assert kept == under.replace(ENV_SECRET.encode(), b"***")
    assert ENV_SECRET.encode() not in blob and ENV_SECRET[:10].encode() not in blob


def test_a_small_member_has_no_line_limit_it_is_redacted_whole(tmp_path, monkeypatch):
    _secret_env(monkeypatch)
    out = tmp_path / "out"
    rd = _full_run(out)
    long_line = b"ab " * admin.RUN_LINE_LIMIT + ENV_SECRET.encode() + b"\n"     # 3 MiB on one line
    (rd / "logs" / "002-rewrite-run.log").write_bytes(long_line)
    path = admin.write_logs_zip(admin.plan_run_logs_zip(rd, lambda rid: False))
    try:
        with zipfile.ZipFile(path) as zf:
            assert zf.read(f"{RID}/logs/002-rewrite-run.log") == long_line.replace(ENV_SECRET.encode(), b"***")
    finally:
        path.unlink()


@pytest.mark.parametrize("whole", [True, False])
def test_a_long_run_of_letters_is_left_out_because_the_userinfo_regex_is_quadratic(tmp_path, monkeypatch, whole):
    if not whole:
        monkeypatch.setattr(admin, "RUN_WHOLE_REDACT_BYTES", 0)
    monkeypatch.setattr(admin, "ZIP_CHUNK", 1000)              # the run spans read chunks on the line path
    out = tmp_path / "out"
    rd = _full_run(out)
    n = admin.RUN_TOKEN_LIMIT
    (rd / "logs" / "002-rewrite-run.log").write_bytes(b"x " + b"Q" * (n + 1) + b" y\n")
    (rd / "logs" / "003-build-verify.log").write_bytes(b"x " + b"Q" * n + b" y\n")
    path = admin.write_logs_zip(admin.plan_run_logs_zip(rd, lambda rid: False))
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            skipped = _skipped({admin.SKIPPED_NAME: zf.read(admin.SKIPPED_NAME)})
    finally:
        path.unlink()
    assert skipped == {f"{RID}/logs/002-rewrite-run.log": admin._reason_token()}
    assert f"{RID}/logs/003-build-verify.log" in names


def test_a_line_that_grew_long_after_the_look_is_withheld_not_copied(monkeypatch):
    _secret_env(monkeypatch)
    monkeypatch.setattr(admin, "RUN_LINE_LIMIT", 64)
    monkeypatch.setattr(admin, "ZIP_CHUNK", 16)
    line = redact.Redactor(redact.live_secret_values).line
    data = b"ok 1\n" + b"x" * 60 + ENV_SECRET.encode() + b"y" * 40 + b"\nok 2\n"
    out = io.BytesIO()
    admin._copy_redacted(io.BytesIO(data), out, len(data), line)
    assert out.getvalue() == ("ok 1\n" + scrub.WITHHELD + "\nok 2\n").encode()


def test_no_redactor_means_no_added_member(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("secret source down")
    monkeypatch.setattr(admin.redact, "live_secret_values", broken)
    out = tmp_path / "out"
    rd = _full_run(out)
    path = admin.write_logs_zip(admin.plan_run_logs_zip(rd, lambda rid: False))
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            skipped = _skipped({admin.SKIPPED_NAME: zf.read(admin.SKIPPED_NAME)})
    finally:
        path.unlink()
    assert names - {admin.SKIPPED_NAME} == {f"{RID}/{m}" for m in ALWAYS}
    assert set(skipped.values()) == {"cannot be redacted now (RuntimeError)"}
    assert len(skipped) == len(FOUR) + len(ADDED) - len(ALWAYS)


# ---------------------------------------------------------------------------
# a small member is redacted as a whole, as the end-of-run pass would (rule 4)
# ---------------------------------------------------------------------------
PEM = ("-----BEGIN KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Q\n"
       "uKUpRKfFLfRYC9AIKjbJTWit\nZx9==\n-----END KEY-----")
GSON = "c2VjcmV0LXRva2VuLXZhbHVl=="


def _one(rd: Path, rel: str) -> bytes:
    path = admin.write_logs_zip(admin.plan_run_logs_zip(rd, lambda rid: False))
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.read(f"{rd.name}/{rel}")
    finally:
        path.unlink()


@pytest.mark.parametrize("whole", [True, False])
def test_a_multi_line_secret_and_a_json_escaped_value(tmp_path, monkeypatch, whole):
    monkeypatch.setenv("SERVICE_SECRET", PEM)
    monkeypatch.setenv("LLM_APP_TOKEN", GSON)
    if not whole:
        monkeypatch.setattr(admin, "RUN_WHOLE_REDACT_BYTES", 0)
    rd = _full_run(tmp_path / "out")
    _w(rd / "logs" / "k.log", PEM + "\n")
    esc = GSON.replace("=", "\\u003d")
    _w(rd / "logs" / "facts.json", '{"auth": "' + esc + '"}\n')
    k = _one(rd, "logs/k.log").decode()
    facts = _one(rd, "logs/facts.json").decode()
    assert "uKUpRKfFLfRYC9AIKjbJTWit" not in k and GSON not in facts
    if whole:                                    # masked as the pass masks them
        assert "Zx9==" not in k and esc not in facts and json.loads(facts) == {"auth": "***"}
    else:                                        # a large member keeps the line-by-line behaviour (rule 4)
        assert "Zx9==" in k and esc in facts


def test_a_jsonl_member_with_one_broken_line(tmp_path, monkeypatch):
    _secret_env(monkeypatch)
    rd = _full_run(tmp_path / "out")
    rows = [json.dumps({"k": f"a {ENV_SECRET}"}), '{"broken": "' + ENV_SECRET, json.dumps({"n": 1})]
    _w(rd / "remediation" / "journal.jsonl", "\n".join(rows) + "\n")
    got = _one(rd, "remediation/journal.jsonl").decode().splitlines()
    assert json.loads(got[0]) == {"k": "a ***"} and got[1] == '{"broken": "***' and json.loads(got[2]) == {"n": 1}


AGREE = (
    ("a.log", f"plain {ENV_SECRET} and https://u:pw@host/x\n".encode()),
    ("b.md", b"no secret here\n"),
    ("c.json", json.dumps({"k": ENV_SECRET, "short": 123456, "esc": ENV_SECRET.replace("-", "\\u002d")}).encode()),
    ("d.json", f'{{"not json": {ENV_SECRET}'.encode()),
    ("e.jsonl", (json.dumps({"k": ENV_SECRET}) + "\r\n" + "broken " + ENV_SECRET + "\n\n").encode()),
    ("f.txt", b"\xc7\xd1 latin " + ENV_SECRET.encode() + b"\n"),
    ("g.yml", ("key: |\n" + PEM + "\n").encode()),
    ("h.log", b"no newline at the end " + ENV_SECRET.encode()),
    ("i.json", b"[1, 2, 3]"),
)


@pytest.mark.parametrize("name,data", AGREE, ids=[n for n, _ in AGREE])
def test_a_whole_member_comes_out_as_the_pass_would_have_written_it(tmp_path, monkeypatch, name, data):
    monkeypatch.setenv("SERVICE_SECRET", PEM)
    values = [ENV_SECRET, PEM]
    c = tmp_path / name
    c.write_bytes(data)
    redact.redact_file(c, values)
    assert admin._redact_whole(name, data, values) == c.read_bytes()


class _Growing:
    """A source that appends the rest of a line to its file right after its `n`-th read (a writer racing the copy)."""

    def __init__(self, f, path: Path, extra: bytes, n: int) -> None:
        self._f, self._path, self._extra, self._n, self.reads = f, path, extra, n, 0

    def read(self, k=-1):
        data = self._f.read(k)
        self.reads += 1
        if self.reads == self._n:
            with open(self._path, "ab") as fh:
                fh.write(self._extra)
        return data

    def __getattr__(self, name):
        return getattr(self._f, name)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._f.close()


@pytest.mark.parametrize("whole", [True, False])
def test_a_file_that_grows_during_the_copy_never_gives_half_a_secret(tmp_path, monkeypatch, whole):
    _secret_env(monkeypatch)
    if not whole:
        monkeypatch.setattr(admin, "RUN_WHOLE_REDACT_BYTES", 0)
    rd = _full_run(tmp_path / "out")
    p = rd / "logs" / "g.log"
    p.write_bytes(b"first line\ntoken=" + ENV_SECRET[:12].encode())    # planned with the line half written
    plan = admin.plan_run_logs_zip(rd, lambda rid: False)
    real = admin._open_planned

    def opened(path, ident):
        src, st = real(path, ident)
        if Path(path) != p:
            return src, st
        return _Growing(src, p, ENV_SECRET[12:].encode() + b"\n", n=2), st    # after the head check

    monkeypatch.setattr(admin, "_open_planned", opened)
    path = admin.write_logs_zip(plan)
    try:
        with zipfile.ZipFile(path) as zf:
            got = zf.read(f"{RID}/logs/g.log")
    finally:
        path.unlink()
    assert got == b"first line\n"


def test_a_folder_that_cannot_be_read_gets_its_line(tmp_path):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads a folder of mode 000")
    rd = _full_run(tmp_path / "out")
    _w(rd / "logs" / "sub" / "x.log", "x\n")
    os.chmod(rd / "logs" / "sub", 0o000)
    try:
        plan = admin.plan_run_logs_zip(rd, lambda rid: False)
    finally:
        os.chmod(rd / "logs" / "sub", 0o755)
    assert dict(plan.left_out)[f"{RID}/logs/sub/"] == "could not be read (PermissionError)"
    assert len(plan.members) == len(FOUR) + len(ADDED)


# ---------------------------------------------------------------------------
# the file copied is the file planned (rule 5)
# ---------------------------------------------------------------------------
def test_a_hard_link_is_refused_at_planning(tmp_path):
    out = tmp_path / "out"
    rd = _full_run(out)
    outside = tmp_path / "m2" / "settings.xml"
    _w(outside, "<password>nexus-pass</password>\n")
    os.link(outside, rd / "logs" / "settings.txt")
    plan = admin.plan_run_logs_zip(rd, lambda rid: False)
    assert dict(plan.left_out)[f"{RID}/logs/settings.txt"] == "a hard link (the file has 2 names)"
    assert f"{RID}/logs/settings.txt" not in {a for a, _, _ in plan.members}


def test_a_file_that_became_a_hard_link_after_planning_is_refused(tmp_path):
    out = tmp_path / "out"
    rd = _full_run(out)
    plan = admin.plan_run_logs_zip(rd, lambda rid: False)
    os.link(rd / "report.md", tmp_path / "elsewhere.md")
    path = admin.write_logs_zip(plan)
    try:
        with zipfile.ZipFile(path) as zf:
            assert f"{RID}/report.md" not in zf.namelist()
            skipped = _skipped({admin.SKIPPED_NAME: zf.read(admin.SKIPPED_NAME)})
    finally:
        path.unlink()
    assert skipped == {f"{RID}/report.md": "a hard link (the file has 2 names)"}


def test_a_folder_swapped_for_a_link_into_project_after_planning_is_refused(tmp_path):
    out = tmp_path / "out"
    rd = _full_run(out)
    _w(rd / "logs" / "mod" / "a.log", "harmless\n")
    _w(rd / "project" / "src" / "a.log", "PROJECT SOURCE\n")
    plan = admin.plan_run_logs_zip(rd, lambda rid: False)
    os.rename(rd / "logs" / "mod", rd / "logs" / "mod.old")
    os.symlink("../project/src", rd / "logs" / "mod")
    path = admin.write_logs_zip(plan)
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            blob = b"".join(zf.read(n) for n in names)
            skipped = _skipped({admin.SKIPPED_NAME: zf.read(admin.SKIPPED_NAME)})
    finally:
        path.unlink()
    assert f"{RID}/logs/mod/a.log" not in names and b"PROJECT" not in blob
    assert skipped == {f"{RID}/logs/mod/a.log": admin.REASON_REPLACED}


def test_a_file_replaced_by_another_after_planning_is_refused(tmp_path):
    out = tmp_path / "out"
    rd = _full_run(out)
    plan = admin.plan_run_logs_zip(rd, lambda rid: False)
    os.replace(rd / "project" / "README.md", rd / "errors.md")      # another inode under the planned name
    path = admin.write_logs_zip(plan)
    try:
        with zipfile.ZipFile(path) as zf:
            assert f"{RID}/errors.md" not in zf.namelist()
            skipped = _skipped({admin.SKIPPED_NAME: zf.read(admin.SKIPPED_NAME)})
    finally:
        path.unlink()
    assert skipped == {f"{RID}/errors.md": admin.REASON_REPLACED}


def test_the_inside_check_itself(tmp_path):
    root = tmp_path / "run"
    _w(root / "logs" / "a.log", "a\n")
    _w(tmp_path / "outside" / "b.log", "b\n")
    os.symlink(tmp_path / "outside", root / "logs" / "out")
    os.symlink(root / "logs" / "a.log", root / "logs" / "link.log")
    assert admin._inside(root, root / "logs" / "a.log") is True
    assert admin._inside(root, root / "logs" / "link.log") is False             # the member itself is a link
    assert admin._inside(root, root / "logs" / "out" / "b.log") is False        # resolves outside the run folder
    assert admin._inside(root, root / ".." / "outside" / "b.log") is False


def test_opening_never_follows_a_link_even_to_the_planned_file(tmp_path):
    f = tmp_path / "a.log"
    f.write_text("a\n")
    st = os.lstat(f)
    os.symlink(f, tmp_path / "link.log")
    with pytest.raises(OSError):
        admin._open_planned(tmp_path / "link.log", (st.st_dev, st.st_ino))
    src, _ = admin._open_planned(f, (st.st_dev, st.st_ino))
    src.close()


# ---------------------------------------------------------------------------
# names (rule 5, last item)
# ---------------------------------------------------------------------------
def test_a_name_that_is_not_utf8_is_left_out_and_named(tmp_path):
    out = tmp_path / "out"
    rd = _full_run(out)
    plan = admin.plan_run_logs_zip(rd, lambda rid: False)
    bad = f"{RID}/logs/bad-\udcc7.log"                 # what os.scandir gives for the byte 0xC7 of a Latin-1 name
    f = rd / "report.md"
    plan.members.append((bad, f, f.stat().st_size))
    plan.text_checked.add(bad)
    path = admin.write_logs_zip(plan)
    try:
        with zipfile.ZipFile(path) as zf:
            assert zf.testzip() is None
            skipped = _skipped({admin.SKIPPED_NAME: zf.read(admin.SKIPPED_NAME)})
    finally:
        path.unlink()
    assert skipped == {f"{RID}/logs/bad-\\udcc7.log": admin.REASON_NAME}
    # planning names such a file too, when the file system takes the name
    try:
        fd = os.open(os.fsencode(str(rd / "logs")) + b"/raw-\xc7.log", os.O_WRONLY | os.O_CREAT, 0o644)
    except OSError:
        return                                           # APFS refuses names that are not UTF-8
    os.close(fd)
    plan = admin.plan_run_logs_zip(rd, lambda rid: False)
    assert (f"{RID}/logs/raw-\udcc7.log", admin.REASON_NAME) in plan.left_out


def test_the_excluded_names_match_without_regard_to_case(tmp_path):
    out = tmp_path / "out"
    rd = _full_run(out)
    upper = ("logs/llm-0001.JSON", "logs/Review-Input.MD", "logs/X.PATCH", "logs/a.DIFF", "logs/b.XML",
             "logs/TARGET/x.log", "remediation/rounds/1/Candidate/x.log", "remediation/Accepted/y.md")
    _fill(rd, upper)
    plan = admin.plan_run_logs_zip(rd, lambda rid: False)
    names = {a for a, _, _ in plan.members} | {a for a, _ in plan.left_out}
    for n in upper:
        assert f"{RID}/{n}" not in names, n
    assert len(plan.members) == len(FOUR) + len(ADDED)


# ---------------------------------------------------------------------------
# the archive of all runs and the preflight
# ---------------------------------------------------------------------------
def test_the_archive_of_all_runs_is_unchanged_on_the_same_folder(tmp_path, repo):  # noqa: F811
    out = tmp_path / "out"
    _full_run(out, RID)
    _full_run(out, OTHER)
    c, _ = _client(tmp_path, repo)
    allf = _zip(c.get("/admin/api/logs.zip"))
    # exactly the four members of v2.1z-ao per run, nothing else, no SKIPPED.txt
    assert sorted(allf) == sorted(f"{r}/{m}" for r in (RID, OTHER) for m in FOUR)
    for name, data in allf.items():
        assert data == (out / name).read_bytes()
    plan = admin.plan_logs_zip(sorted(admin.iter_run_dirs(out)), lambda rid: False)
    assert [a for a, _, _ in plan.members] == [f"{r}/{m}" for r in sorted((RID, OTHER)) for m in admin.LOG_MEMBERS]
    assert plan.left_out == [] and plan.root is None
    check = c.get("/admin/api/logs/check").json()
    assert check == {"ok": True, "bytes": sum(len(v) for v in allf.values()), "runs": 2, "skipped": 0}
    # the archive of one run of the same folder is the larger one
    one = _zip(c.get(f"/admin/api/runs/{RID}/logs.zip"))
    assert {n for n in allf if n.startswith(RID)} < set(one)


def test_the_check_numbers_agree_with_the_archive_built(tmp_path, repo, monkeypatch):  # noqa: F811
    out = tmp_path / "out"
    rd = _full_run(out)
    (rd / "logs" / "002-rewrite-run.log").write_bytes(b"r" * 5000)
    monkeypatch.setattr(admin, "ZIP_FILE_LIMIT", 4096)
    c, _ = _client(tmp_path, repo)
    check = c.get(f"/admin/api/runs/{RID}/logs/check").json()
    files = _zip(c.get(f"/admin/api/runs/{RID}/logs.zip"))
    assert check["ok"] is True and check["runs"] == 1
    assert check["bytes"] == sum(len(v) for n, v in files.items() if n != admin.SKIPPED_NAME)
    assert check["skipped"] == len(_skipped(files))


# ---------------------------------------------------------------------------
# the page (rule 6)
# ---------------------------------------------------------------------------
def test_the_run_button_says_what_it_gives():
    page = (Path(admin.__file__).parent / "static" / "admin.html").read_text(encoding="utf-8")
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    run_link = re.search(r'<a href="\$\{API\}/runs/\$\{encodeURIComponent\(id\)\}/logs\.zip"[^`]*`', script).group(0)
    assert 'data-zip="run"' in run_link and ">로그와 보고서</a>" in run_link
    assert "실행의 로그, 보고서, 상태 파일을 ZIP으로 받습니다. 소스는 들어 있지 않습니다." in run_link
    assert "이 실행의 파일 ${fmtInt(pre.skipped)}개는 빼고 받습니다. 뺀 파일과 이유는 ZIP 안의 SKIPPED.txt에 있습니다." \
        in script
    # the button of all runs is unchanged
    assert '<span>전체 로그 받기</span>' in page and 'title="모든 실행의 로그를 ZIP 하나로 받습니다"' in page
    for sentence in ("로그와 보고서", "소스는 들어 있지 않습니다", "뺀 파일과 이유"):
        assert "—" not in sentence and "→" not in sentence
    # never file content into innerHTML: the page only builds the link from the run id (escaped)
    assert "${esc(id)} 실행의 로그" in run_link


def test_the_skipped_file_is_english_with_one_line_per_file(tmp_path):
    out = tmp_path / "out"
    rd = _full_run(out)
    plan = admin.plan_run_logs_zip(rd, lambda rid: True)
    path = admin.write_logs_zip(plan)
    try:
        with zipfile.ZipFile(path) as zf:
            text = zf.read(admin.SKIPPED_NAME).decode("utf-8")
    finally:
        path.unlink()
    assert text.isascii()                                       # the reasons are English
    assert io.StringIO(text).readline().startswith("These files of the run are left out")
    rows = [ln for ln in text.splitlines()[2:] if ln]
    assert len(rows) == len(ADDED) + 1 and all(ln.count("\t") == 1 for ln in rows)


@pytest.mark.parametrize("name,inside", [("logs/a.log", True), ("logs/a.patch", False), ("logs/A.class", False),
                                         ("logs/review-input.md", False), ("logs/llm-0001.json", False),
                                         ("logs/notes.yml", True), ("logs/.x.json", False)])
def test_text_name_rule(name, inside):
    assert admin._text_name(name.rsplit("/", 1)[1]) is inside
