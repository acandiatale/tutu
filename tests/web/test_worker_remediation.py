"""2026-11-11 (user): the vulnerability remediation runs in the BUILD POD, chained after a migration that ended BUILD SUCCESS
(`<run dir>/remediation/`). What the Worker side owes it: the IVAS variables reach the child `sbmig run`; the IVAS user id is
treated as a secret although its name has no secret word; no IVAS value is ever env-pinned; the final redaction pass covers
the remediation's text files but never its ZIPs, project trees or the read-only original; a stop signal during the chained
remediation keeps the migration's result line in the error text."""
from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import zipfile
from pathlib import Path

import pytest

from sbmig.web import contract, redact, worker

RUN_ID = "20261111-120000-ivas"
PASSWORD = "ivas-pass-0123456789"
USER_ID = "svc_ivas"
BASE_URL = "https://ivas.invalid/api"


def _request(tmp_path: Path) -> dict:
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
        sbmig_version=contract.sbmig_version(), mode="k8s-job").write(run_dir)
    env = {"SBMIG_SOURCE_ZIP": str(zip_path), "SBMIG_OUT": str(out), "SBMIG_RUN_ID": RUN_ID,
           "SBMIG_APP_DIR": str(repo), "SBMIG_PYTHON": "py", "SBMIG_KB_DIR": str(tmp_path / "kb"),
           "IVAS_BASE_URL": BASE_URL, "IVAS_USER_ID": USER_ID, "IVAS_PASSWORD": PASSWORD}
    return {"repo": repo, "run_dir": run_dir, "env": env}


def _write_zip(path: Path, payload: str) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as z:
        z.writestr("pom.xml", payload)          # stored: the secret's bytes are literally inside the zip
    return path.read_bytes()


def _chained_remediation(run_dir: Path) -> dict[str, Path]:
    """What a chained remediation leaves behind (RunPaths layout), every file mentioning the IVAS credentials."""
    rem = run_dir / "remediation"
    leak = f"user={USER_ID} password={PASSWORD} url={BASE_URL}"
    files = {
        "state": rem / "state.json", "journal": rem / "journal.jsonl", "log": rem / "remediation.log",
        "report": rem / "report.md", "plan": rem / "rounds" / "1" / "plan.json",
        "patch": rem / "rounds" / "1" / "candidate.patch", "build_log": rem / "rounds" / "1" / "build.log",
        "baseline_log": rem / "baseline" / "build.log", "scan": rem / "scans" / "baseline.json",
        "delivery_json": rem / "delivery" / "delivery.json",
        "accepted_pom": rem / "accepted" / "pom.xml", "candidate_pom": rem / "rounds" / "1" / "candidate" / "pom.xml",
        "manifest": rem / "original" / "manifest.json",
    }
    for key, p in files.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"msg": leak}) if p.suffix in (".json", ".jsonl") else f"{key}: {leak}\n", encoding="utf-8")
    files["original_zip"] = rem / "original" / "source.zip"
    files["delivery_zip"] = rem / "delivery" / "result.zip"
    _write_zip(files["original_zip"], leak)
    _write_zip(files["delivery_zip"], leak)
    files["original_zip"].chmod(0o444)       # intake keeps the original read-only
    return files


def test_ivas_user_id_is_a_secret_name_and_base_url_is_not():
    assert contract.is_secret_name("IVAS_USER_ID") and contract.is_secret_name("ivas_user_id")
    assert contract.is_secret_name("IVAS_PASSWORD")
    assert not contract.is_secret_name("IVAS_BASE_URL")
    assert not contract.is_secret_name("SBMIG_REMEDIATION__AFTER_MIGRATION")


def test_a_short_ivas_user_id_is_still_redacted_but_not_a_three_letter_one():
    env = {"IVAS_USER_ID": "kim1", "IVAS_PASSWORD": PASSWORD, "IVAS_BASE_URL": BASE_URL, "OTHER_TOKEN": "abcd"}
    vals = contract.secret_values(env)
    assert "kim1" in vals and PASSWORD in vals
    assert BASE_URL not in vals and "abcd" not in vals         # the 6-character floor still holds for the word rule
    assert "abc" not in contract.secret_values({"IVAS_USER_ID": "abc"})


def test_no_ivas_value_is_ever_env_pinned(tmp_path: Path):
    """IVAS_* are neither `SBMIG_` overrides nor `${VAR}` placeholders of a config file — and the two secret ones would be
    excluded even if a config referenced them. The remediation switch itself is an override and is pinned."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.yml").write_text("remediation:\n  after_migration: false\n", encoding="utf-8")
    (tmp_path / "config" / "c.yml").write_text("x: ${IVAS_USER_ID}\ny: ${IVAS_PASSWORD}\n", encoding="utf-8")
    env = {"IVAS_BASE_URL": BASE_URL, "IVAS_USER_ID": USER_ID, "IVAS_PASSWORD": PASSWORD,
           "SBMIG_REMEDIATION__AFTER_MIGRATION": "true"}
    pins = contract.env_pins(tmp_path, ["c.yml"], env)
    assert not [n for n in pins if n.startswith("IVAS_")]
    assert "SBMIG_REMEDIATION__AFTER_MIGRATION" in pins
    pins_plain = contract.env_pins(tmp_path, [], env)
    assert "IVAS_BASE_URL" not in pins_plain and "SBMIG_REMEDIATION__AFTER_MIGRATION" in pins_plain


def test_the_child_receives_the_ivas_values(tmp_path: Path):
    req = _request(tmp_path)
    seen: dict[str, str] = {}

    def child(cmd, cwd, env):
        seen.update(env)
        return 0

    assert worker.main(environ=req["env"], run_child=child, install_signals=lambda h: (lambda: None)) == 0
    assert (seen["IVAS_BASE_URL"], seen["IVAS_USER_ID"], seen["IVAS_PASSWORD"]) == (BASE_URL, USER_ID, PASSWORD)
    assert seen["SBMIG_REDACT_OUTPUT"] == "1"


def test_final_redaction_covers_remediation_text_but_not_zips_trees_or_the_original(tmp_path: Path):
    req = _request(tmp_path)
    files: dict[str, Path] = {}

    def child(cmd, cwd, env):
        files.update(_chained_remediation(req["run_dir"]))
        return 0

    zips_before = {}
    try:
        assert worker.main(environ=req["env"], run_child=child, install_signals=lambda h: (lambda: None)) == 0
        zips_before = {k: files[k].read_bytes() for k in ("original_zip", "delivery_zip")}
        for key in ("state", "journal", "log", "report", "plan", "patch", "build_log", "baseline_log", "scan", "delivery_json"):
            text = files[key].read_text(encoding="utf-8")
            assert PASSWORD not in text and USER_ID not in text, key
            assert "***" in text and BASE_URL in text, key          # the host is not a secret
        json.loads(files["state"].read_text(encoding="utf-8"))       # still valid JSON
        # project trees and the original are hash-bound: never rewritten
        for key in ("accepted_pom", "candidate_pom", "manifest"):
            assert PASSWORD in files[key].read_text(encoding="utf-8"), key
        for key, raw in zips_before.items():
            assert PASSWORD.encode() in raw, key                         # untouched bytes
            with zipfile.ZipFile(files[key]) as z:
                assert z.testzip() is None
        assert stat.S_IMODE(files["original_zip"].stat().st_mode) == 0o444
        fin = [json.loads(ln) for ln in (req["run_dir"] / "journal.jsonl").read_text(encoding="utf-8").splitlines()][-1]
        assert fin["event"] == "worker_finished" and fin["redacted_files"] == 10
        # a second pass changes nothing (and does not trip over the read-only original)
        assert redact.redact_run_dir(req["run_dir"], contract.secret_values(req["env"])) == 0
    finally:
        # a later cleanup of the run dir works with the read-only original inside
        shutil.rmtree(req["run_dir"])
    assert not req["run_dir"].exists()


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_redaction_never_follows_a_link_out_of_the_remediation_dir(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.md").write_text(f"pw {PASSWORD}\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    rem = run_dir / "remediation"
    rem.mkdir(parents=True)
    (rem / "linked").symlink_to(outside, target_is_directory=True)
    (rem / "link.md").symlink_to(outside / "notes.md")
    assert redact.redact_run_dir(run_dir, [PASSWORD]) == 0
    assert PASSWORD in (outside / "notes.md").read_text(encoding="utf-8")


def test_a_stop_during_the_chained_remediation_keeps_the_migration_result_line(tmp_path: Path):
    req = _request(tmp_path)
    holder = {}

    def install(handler):
        holder["h"] = handler
        return lambda: None

    def child(cmd, cwd, env):
        (req["run_dir"] / "sbmig.log").write_text("SBMIG-RESULT status=SUCCESS exit=0 remediation=RUNNING\n", encoding="utf-8")
        holder["h"](signal.SIGTERM, None)          # the Job deadline hits while the remediation runs
        raise AssertionError("not reached")

    assert worker.main(environ=req["env"], run_child=child, install_signals=install) == 3
    status = contract.JobStatus.read(req["run_dir"])
    assert status.exit_code == 3 and status.error.startswith(worker.terminated_error(signal.SIGTERM))
    assert "the migration itself had finished" in status.error and "SBMIG-RESULT status=SUCCESS" in status.error
