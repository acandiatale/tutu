"""The scan source of an upload whose build output the intake left out (fourth review, item 9).

Found by the real-Maven smoke run: the baseline scan uploaded `original/source.zip` (which still holds
`<module>/target/…`) and a no-change run delivered those bytes, while the report said the build output was left out
of the scan and the result ZIP. Now, when `intake.build_output_files > 0`, the baseline scan uploads
`original/scan-source.zip` (a deterministic ZIP of accepted/ over the manifest paths, its sha256 recorded in
`state.scan_source_sha256`) and a no-change delivery is that ZIP. Nothing left out → byte for byte as before. A run the
deployed version started (the original already sent to the baseline scan) resumes with the original. No Maven, no
IVAS, no LLM.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from sbmig.remediations import chain, graph
from sbmig.remediations import intake as intake_mod
from sbmig.remediations.models import (
    AWAITING_CONFIRMATION,
    FAILED,
    SUCCEEDED,
    BuildOutcome,
    IntakeLimits,
    RemediationState,
    RunPaths,
    ScanResult,
    UploadReceipt,
    read_json,
    sha256_file,
    write_json_atomic,
)

from .fakes import (
    POLICY_HASH,
    POM,
    UNCERTAIN,
    FakeMaven,
    FakeScanner,
    Recorder,
    ScriptedPlanner,
    deterministic_zip,
    finding,
    make_cfg,
    make_components,
    scan_result,
)
from .test_graph import one_fix_components, run

APP = "src/main/java/App.java"
SOURCES = {"pom.xml": POM.encode(), APP: b"class App {}\n"}
BUILD_OUTPUT = {"target/app.jar": b"jar bytes", "target/classes/App.class": b"\xca\xfe\xba\xbe"}


def _upload(tmp_path: Path, *, left_out: bool = True) -> tuple[RemediationState, RunPaths]:
    raw = {**SOURCES, **(BUILD_OUTPUT if left_out else {})}
    up = tmp_path / "upload.zip"
    up.write_bytes(deterministic_zip(raw))
    result = intake_mod.accept_zip(up, tmp_path / "run", IntakeLimits())
    assert result.build_output_files == (len(BUILD_OUTPUT) if left_out else 0)
    state = graph.new_state("rem-1", "app.zip", make_cfg(), result, ["default.yml"], policy_hash=POLICY_HASH)
    return state, RunPaths(tmp_path / "run")


def _names(z: Path) -> list[str]:
    with zipfile.ZipFile(z) as zf:
        return sorted(zf.namelist())


def _comps(**kw):
    kw.setdefault("intake", intake_mod)
    return make_components(**kw)


def test_left_out_build_output_is_neither_scanned_nor_delivered(tmp_path):
    state, paths = _upload(tmp_path)
    scan = FakeScanner()
    rec = Recorder()
    out, _ = run(state, paths, _comps(scan=scan), rec=rec)
    assert out.status == SUCCEEDED, out.message
    src = paths.scan_source
    assert src.is_file() and out.scan_source_sha256 == sha256_file(src)
    assert _names(src) == sorted(SOURCES)                                   # no target/ in what IVAS saw
    [call] = scan.calls
    assert call.label == "baseline" and call.zip_sha256 == out.scan_source_sha256 and call.uploaded
    # the no-change delivery is the scanned ZIP, and it counts as scanned
    assert out.delivery.round == 0 and out.delivery.sha256 == out.scan_source_sha256 and out.delivery.scanned
    assert sha256_file(paths.delivery_zip) == out.scan_source_sha256
    assert _names(paths.delivery_zip) == sorted(SOURCES)
    # the upload itself is never changed
    assert sha256_file(paths.original_zip) == out.intake.zip_sha256
    assert "target/app.jar" in _names(paths.original_zip)
    assert next(e for e in rec.events if e.get("event") == "scan_source_made")["left_out"] == 2
    md = paths.report_md.read_text(encoding="utf-8")
    assert "- 결과 ZIP은 원본 ZIP에서 빌드 산출물만 뺀 것입니다." in md
    assert "업로드한 ZIP의 빌드 산출물 2개(`target`)는 검사와 결과 ZIP에서 제외했습니다." in md


def test_left_out_build_output_with_a_finding_compares_like_with_like(tmp_path):
    state, paths = _upload(tmp_path)
    comps = one_fix_components(intake=intake_mod, maven=FakeMaven(outcomes=[True, True]))
    out, _ = run(state, paths, comps)
    assert out.status == SUCCEEDED, out.message
    base = ScanResult.from_dict(read_json(paths.scan("baseline")))
    assert base.zip_sha256 == out.scan_source_sha256 != out.intake.zip_sha256
    final = ScanResult.from_dict(read_json(paths.scan("final-r1")))
    assert final.zip_sha256 == out.delivery.sha256 and out.delivery.round == 1 and out.delivery.scanned
    assert _names(paths.delivery_zip) == sorted(SOURCES)


def test_nothing_left_out_scans_and_delivers_the_original_byte_for_byte(tmp_path):
    state, paths = _upload(tmp_path, left_out=False)
    scan = FakeScanner()
    out, _ = run(state, paths, _comps(scan=scan))
    assert out.status == SUCCEEDED
    assert not paths.scan_source.exists() and "scan_source_sha256" not in out.to_dict()
    assert scan.calls[0].zip_sha256 == out.intake.zip_sha256
    assert paths.delivery_zip.read_bytes() == paths.original_zip.read_bytes()
    assert "- 결과 ZIP은 원본 ZIP과 같은 파일입니다." in paths.report_md.read_text(encoding="utf-8")


def test_a_run_of_the_deployed_version_waiting_on_its_receipt_resumes_with_the_original(tmp_path):
    state, paths = _upload(tmp_path)
    # what the deployed version left: the original ZIP sent to the baseline scan, a receipt, no scan-source sha
    state.status, state.next_node = AWAITING_CONFIRMATION, "baseline_scan"
    state.baseline_build = BuildOutcome(ok=True, exit_code=0)
    state.pending_receipt["baseline"] = UploadReceipt(project_id="p", analysis_id="a",
                                                      zip_sha256=state.intake.zip_sha256).to_dict()
    scan = FakeScanner()
    out, _ = run(state, paths, _comps(scan=scan))
    assert out.status == SUCCEEDED, out.message
    [call] = scan.calls
    assert call.receipt is not None and not call.uploaded and call.zip_sha256 == out.intake.zip_sha256
    assert not paths.scan_source.exists() and out.scan_source_sha256 == ""
    assert paths.delivery_zip.read_bytes() == paths.original_zip.read_bytes() and out.delivery.scanned
    md = paths.report_md.read_text(encoding="utf-8")
    assert "- 결과 ZIP은 원본 ZIP과 같은 파일입니다." in md
    assert "검사와 결과 ZIP에서 제외했습니다" not in md                   # not true for that run


def test_a_run_of_the_deployed_version_past_its_baseline_scan_keeps_the_original(tmp_path):
    state, paths = _upload(tmp_path)
    base = scan_result(finding())
    base.label, base.zip_sha256 = "baseline", state.intake.zip_sha256
    write_json_atomic(paths.scan("baseline"), base.to_dict())
    state.baseline_scan = "scans/baseline.json"
    state.status, state.next_node, state.round = AWAITING_CONFIRMATION, "inventory", 1
    state.baseline_build = BuildOutcome(ok=True, exit_code=0)
    out, _ = run(state, paths, _comps(scan=FakeScanner(), plan=ScriptedPlanner()))
    assert out.delivery is not None and out.delivery.round == 0
    assert out.delivery.sha256 == out.intake.zip_sha256 and out.delivery.scanned           # no sha check fails
    assert not paths.scan_source.exists()
    assert "zip_hash_mismatch" not in out.reasons


def test_an_uncertain_upload_then_a_receipt_keeps_the_scan_source_sha(tmp_path):
    state, paths = _upload(tmp_path)
    scan = FakeScanner({"baseline": [UNCERTAIN, scan_result()]})
    rec = Recorder()
    out, _ = run(state, paths, _comps(scan=scan), rec=rec)
    assert out.status == AWAITING_CONFIRMATION
    sha = out.scan_source_sha256
    assert sha and out.pending_upload == {"baseline": sha} and sha != out.intake.zip_sha256
    # the page's "receipt" answer, as the service writes it (`RemediationManager.resume`)
    st = RemediationState.from_dict(rec.saves[-1])
    st.pending_receipt["baseline"] = UploadReceipt(project_id="p", analysis_id="a",
                                                   zip_sha256=st.pending_upload.pop("baseline")).to_dict()
    out2, _ = run(st, paths, _comps(scan=scan))
    assert out2.status == SUCCEEDED, out2.message
    call = scan.calls[-1]
    assert call.receipt is not None and call.zip_sha256 == sha and not call.uploaded
    assert out2.delivery.sha256 == sha and out2.delivery.scanned


def test_the_scan_source_is_the_same_bytes_when_made_again(tmp_path):
    state, paths = _upload(tmp_path)
    h = graph._Holder(state, paths, make_cfg(), _comps(), Recorder().save, Recorder().journal, lambda: False)
    p1, s1 = h.scan_source()
    assert p1 == paths.scan_source and state.scan_source_sha256 == s1
    # a kill after the file was written and before its sha was saved: made again, same bytes
    state.scan_source_sha256 = ""
    paths.scan_source.unlink()
    assert h.scan_source() == (p1, s1)
    # recorded sha, file gone: made again and verified
    paths.scan_source.unlink()
    assert h.scan_source() == (p1, s1)


def test_a_scan_source_that_cannot_be_made_again_fails_the_run(tmp_path):
    state, paths = _upload(tmp_path)
    scan = FakeScanner({"baseline": [UNCERTAIN]})
    rec = Recorder()
    out, _ = run(state, paths, _comps(scan=scan), rec=rec)
    assert out.status == AWAITING_CONFIRMATION
    paths.scan_source.unlink()
    (paths.accepted / APP).write_bytes(b"class App { changed }\n")
    st = RemediationState.from_dict(rec.saves[-1])
    out2, _ = run(st, paths, _comps(scan=scan))
    assert out2.status == FAILED and out2.reason == "zip_hash_mismatch"
    assert out2.message == graph.SCAN_SOURCE_CHANGED
    assert len(scan.calls) == 1                                            # nothing uploaded again


def test_the_chained_source_zip_never_carries_build_output(tmp_path):
    tree = tmp_path / "migrated"
    for rel, data in {**SOURCES, "target/app.jar": b"x", "mod/pom.xml": POM.encode(), "mod/target/m.jar": b"y",
                      "mod/src/main/java/M.java": b"class M {}\n"}.items():
        f = tree / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    z = tmp_path / "chain" / "source.zip"
    chain.source_zip(tree, z)
    assert not any("/target/" in f"/{n}" for n in _names(z))
    result = intake_mod.accept_zip(z, tmp_path / "chain-run", IntakeLimits())
    assert result.build_output_files == 0                                   # so the original ZIP is scanned


def test_the_files_route_still_serves_the_upload_as_source_zip(tmp_path):
    from sbmig.remediations.service import _FIXED_FILES
    paths = RunPaths(tmp_path)
    assert _FIXED_FILES["source.zip"](paths) == paths.original_zip
    assert "scan-source.zip" not in _FIXED_FILES


@pytest.mark.parametrize("left_out", [True, False])
def test_old_state_json_reads_back(tmp_path, left_out):
    state, _paths = _upload(tmp_path, left_out=left_out)
    data = state.to_dict()
    assert "scan_source_sha256" not in data
    assert RemediationState.from_dict(data).scan_source_sha256 == ""
