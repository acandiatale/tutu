"""v2.1z-ac (2026-11-09): the Main ↔ Worker file contract — pinned inputs, atomic control state, attempt-aware job status, secret names."""
from __future__ import annotations

import json
from pathlib import Path

from sbmig.web.contract import (
    CONTROL_DIR,
    ControlState,
    JobStatus,
    RequestRecord,
    RunState,
    config_digests,
    secret_env_names,
    secret_values,
    sha256_file,
    verify_request,
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "default.yml").write_text("a: 1\n", encoding="utf-8")
    (repo / "config" / "artifacts.yml").write_text("b: 2\n", encoding="utf-8")
    return repo


def test_request_record_pins_the_inputs_and_the_worker_detects_every_change(tmp_path: Path):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "out" / "web-1"
    zip_path = run_dir / "source.zip"
    run_dir.mkdir(parents=True)
    zip_path.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    rec = RequestRecord(run_id="web-1", accepted_at="2026-11-09T00:00:00Z", upload="p.zip", configs=["artifacts.yml"],
                        zip_sha256=sha256_file(zip_path), config_digests=config_digests(repo, ["artifacts.yml"]),
                        sbmig_version="0.1.0", mode="k8s-job")
    rec.write(run_dir)
    back = RequestRecord.read(run_dir)
    assert back == rec and (run_dir / CONTROL_DIR / "request.json").is_file()
    assert verify_request(back, repo, zip_path, "web-1") == []
    # a changed zip, a changed config, a different version and a different run id are each named
    zip_path.write_bytes(b"PK\x05\x06" + b"\1" * 18)
    (repo / "config" / "artifacts.yml").write_text("b: 3\n", encoding="utf-8")
    problems = verify_request(RequestRecord.from_json({**back.to_json(), "sbmig_version": "9.9.9"}), repo, zip_path, "web-2")
    text = "\n".join(problems)
    assert "run id differs" in text and "source zip changed" in text and "config artifacts.yml differs" in text
    assert "sbmig version differs" in text and len(problems) == 4
    assert "source zip missing" in "\n".join(verify_request(back, repo, run_dir / "nope.zip"))


def test_control_state_round_trips_and_transitions_atomically(tmp_path: Path):
    run_dir = tmp_path / "out" / "web-1"
    st = ControlState(run_id="web-1")
    assert st.state == "queued" and len(st.owner) == 16 and not st.terminal
    st.transition(RunState.CREATING, job="sbmig-web-1").write(run_dir)
    again = ControlState.read(run_dir)
    assert again is not None and again.state == "creating" and again.job == "sbmig-web-1" and again.owner == st.owner
    again.transition(RunState.FINISHED, exit_code=0).write(run_dir)
    assert ControlState.read(run_dir).terminal
    assert not list((run_dir / CONTROL_DIR).glob(".*.tmp"))          # the temp file was replaced, not left behind
    assert ControlState.read(tmp_path / "missing") is None
    (run_dir / CONTROL_DIR / "state.json").write_text("{broken", encoding="utf-8")
    assert ControlState.read(run_dir) is None


def test_job_status_matches_only_the_current_attempt_and_reads_legacy_files(tmp_path: Path):
    run_dir = tmp_path / "out" / "web-1"
    run_dir.mkdir(parents=True)
    JobStatus(exit_code=2, run_id="web-1", attempt=1).write(run_dir)
    st = JobStatus.read(run_dir)
    assert st is not None and st.exit_code == 2 and st.matches("web-1", 1)
    assert not st.matches("web-1", 2) and not st.matches("web-9", 1)
    (run_dir / "job-status.json").write_text(json.dumps({"exit_code": 0, "finished_at": "t"}), encoding="utf-8")   # run-job.sh style
    legacy = JobStatus.read(run_dir)
    assert legacy is not None and legacy.matches("anything", 7)
    (run_dir / "job-status.json").write_text("{}", encoding="utf-8")
    assert JobStatus.read(run_dir) is None


def test_secret_env_names_and_values():
    env = {"LLM_APP_TOKEN": "abcdefgh", "NEXUS_PASSWORD": "pw-secret-1", "LLM_BASE_URL": "https://x", "SBMIG_WEB_TOKEN": "t",
           "MY_API_KEY": "key-12345", "HOME": "/home/x"}
    assert secret_env_names(env) == ["LLM_APP_TOKEN", "MY_API_KEY", "NEXUS_PASSWORD", "SBMIG_WEB_TOKEN"]
    vals = secret_values(env)
    assert vals == ["pw-secret-1", "key-12345", "abcdefgh"] and "t" not in vals and "https://x" not in vals


def test_config_overrides_are_not_secret_names():
    """2026-11-10: `SBMIG_<SECTION>__<KEY>` is a config override — no config key holds a credential, so a name that merely
    contains TOKEN (`llm.max_output_tokens`, `llm.token_url`) is neither redacted from the reports nor refused as a literal."""
    from sbmig.web.contract import is_secret_name
    for name in ("SBMIG_LLM__MAX_OUTPUT_TOKENS", "SBMIG_LLM__TOKEN_URL", "sbmig_llm__token_refresh_margin_s", "LLM_BASE_URL"):
        assert not is_secret_name(name), name
    for name in ("LLM_APP_TOKEN", "LLM_API_KEY", "SBMIG_WEB_TOKEN", "nexus_password", "SBMIG_TOKEN", "MY__SECRET"):
        assert is_secret_name(name), name
    env = {"SBMIG_LLM__MAX_OUTPUT_TOKENS": "128000", "LLM_APP_TOKEN": "abcdefgh"}
    assert secret_env_names(env) == ["LLM_APP_TOKEN"] and secret_values(env) == ["abcdefgh"]
