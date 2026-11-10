"""Review 2026-11-10: the env pin helpers of the Main ↔ Worker contract (`sbmig.web.contract`) and `placeholder_names`.

The Main that creates a Job fingerprints the environment values the configuration reads (`${VAR}` placeholders of the
yml files, `SBMIG_<SECTION>__<KEY>` overrides) into `control/state.json`; the Worker recomputes them and compares names.
Hermetic: a temp repo root with `config/`, explicit `environ` dicts (one test shows `environ=None` reads `os.environ`)."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from sbmig.core.config import placeholder_names
from sbmig.web import contract
from sbmig.web.contract import (
    CONTROL_DIR,
    ENV_PIN_ALIASES,
    ENV_PIN_HASH_LEN,
    ENV_PIN_OFF,
    ENV_PIN_POLICIES,
    ENV_PIN_REFUSE,
    ENV_PIN_SKIP,
    ENV_PIN_WARN,
    STATE_FILE,
    ControlState,
    RunState,
    compare_env_pins,
    config_env_names,
    env_pin_names,
    env_pin_policy,
    env_pin_value,
    env_pins,
)

NO_DIFF = {"differs": [], "main_only": [], "worker_only": []}

DEFAULT_YML = """\
# ${ONLY_IN_A_COMMENT} — a comment is not configuration
llm:
  base_url: "${LLM_BASE_URL}"
  model: "${LLM_MODEL:-gpt-6-luna}"
  token_url: "${LLM_TOKEN_URL:-}"        # secret-looking name: never pinned
  record_dir: "${LLM_RECORD_DIR:-}"      # may differ per pod: never pinned
  "${NOT_A_KEY_PLACEHOLDER}": 1          # dict keys are not expanded
  payload_extra: {need_origin: true}
"""
EXTRA_YML = """\
build:
  java_home: "${JAVA21_HOME:-}"
  # settings_file: "${COMMENTED_OUT}"
  jvm_args: ["-Xmx4g", "${MAVEN_HEAP_FLAG:-}"]
  credentials: "${NEXUS_PASSWORD}"
"""


def _repo(tmp_path: Path, extra: str = EXTRA_YML) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "default.yml").write_text(DEFAULT_YML, encoding="utf-8")
    (repo / "config" / "extra.yml").write_text(extra, encoding="utf-8")
    return repo


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:ENV_PIN_HASH_LEN]


# ── placeholder_names ────────────────────────────────────────────────────────────────────────────────────────────────
def test_placeholder_names_of_strings():
    assert placeholder_names("${A}") == {"A"}
    assert placeholder_names("x ${B:-some default} y ${C} z") == {"B", "C"}
    assert placeholder_names("${D:-}") == {"D"}
    assert placeholder_names("no placeholder, $E, {F}, $(G)") == set()
    assert placeholder_names("") == set()


def test_placeholder_names_of_nested_lists_and_dicts():
    assert placeholder_names(["${A}", ["${B:-x}", ["plain", "${C}"]]]) == {"A", "B", "C"}
    assert placeholder_names({"k": "${A}", "nested": {"deeper": ["${B}"], "n": 3}}) == {"A", "B"}
    assert placeholder_names({"${KEY_NAME}": "value", "other": "${VALUE_NAME}"}) == {"VALUE_NAME"}   # keys ignored
    assert placeholder_names([]) == set() and placeholder_names({}) == set()


@pytest.mark.parametrize("value", [None, 0, 3, 2.5, True])
def test_placeholder_names_of_non_strings_is_empty(value):
    assert placeholder_names(value) == set()


# ── config_env_names ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_config_env_names_reads_values_not_comments(tmp_path: Path):
    repo = _repo(tmp_path)
    assert config_env_names(repo, []) == {"LLM_BASE_URL", "LLM_MODEL", "LLM_TOKEN_URL", "LLM_RECORD_DIR"}
    names = config_env_names(repo, ["extra.yml"])
    assert {"JAVA21_HOME", "MAVEN_HEAP_FLAG", "NEXUS_PASSWORD", "LLM_BASE_URL"} <= names
    assert not {"ONLY_IN_A_COMMENT", "COMMENTED_OUT", "NOT_A_KEY_PLACEHOLDER"} & names


def test_config_env_names_skips_a_missing_config(tmp_path: Path):
    repo = _repo(tmp_path)
    assert config_env_names(repo, ["missing.yml"]) == config_env_names(repo, [])
    (repo / "config" / "default.yml").unlink()
    assert config_env_names(repo, ["missing.yml"]) == set()
    assert config_env_names(repo, ["extra.yml"]) == {"JAVA21_HOME", "MAVEN_HEAP_FLAG", "NEXUS_PASSWORD"}


def test_config_env_names_scans_a_file_that_is_not_yaml_as_text(tmp_path: Path):
    repo = _repo(tmp_path, extra="build: [unclosed\n  java_home: ${TEXT_SCAN}\n# ${IN_A_COMMENT_TOO}\n")
    names = config_env_names(repo, ["extra.yml"])
    # the fallback cannot tell a comment from a value: everything in the text counts
    assert {"TEXT_SCAN", "IN_A_COMMENT_TOO", "LLM_MODEL"} <= names


# ── env_pin_names ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_env_pin_names_placeholders_overrides_and_alias(tmp_path: Path):
    repo = _repo(tmp_path)
    environ = {
        "SBMIG_BUILD__MVN": "mvn",                         # an override present in the environment
        "SBMIG_LLM__MAX_OUTPUT_TOKENS": "8000",            # overrides are exempt from the secret-name rule
        "SBMIG_LLM__TOKEN_URL": "${LLM_TOKEN_URL}",        # exempt as an override; its placeholder is secret-looking
        "SBMIG_LLM__BASE_URL": "${GATEWAY_URL:-x}",        # a placeholder inside an override value (unset: still a name)
        "SBMIG_LLM__RECORD_DIR": "${LLM_RECORD_DIR:-}",    # ENV_PIN_SKIP, and so is its placeholder
        "SBMIG_KB_DIR": "/data/kb-v2",                     # alias only → SBMIG_KB__DIR
        "SBMIG_FOO": "x",                                  # single underscore: not an override
        "SBMIG_REDACT_OUTPUT": "1",
        "LLM_MODEL": "build_error_fixer",
        "LLM_API_KEY": "k" * 20, "LLM_APP_TOKEN": "t" * 20, "NEXUS_PASSWORD": "p" * 20,
        "HTTP_PROXY": "http://user:pw@proxy:3128",
        "UNRELATED": "y",
    }
    names = env_pin_names(repo, ["extra.yml"], environ)
    assert names == sorted(names)
    assert names == sorted({
        "LLM_BASE_URL", "LLM_MODEL",                                   # default.yml placeholders (set or not)
        "JAVA21_HOME", "MAVEN_HEAP_FLAG",                              # extra.yml placeholders
        "SBMIG_BUILD__MVN", "SBMIG_LLM__MAX_OUTPUT_TOKENS", "SBMIG_LLM__TOKEN_URL", "SBMIG_LLM__BASE_URL",
        "GATEWAY_URL", "SBMIG_KB__DIR",
    })
    for never in ("LLM_API_KEY", "LLM_APP_TOKEN", "NEXUS_PASSWORD", "LLM_TOKEN_URL", *ENV_PIN_SKIP,
                  "SBMIG_FOO", "SBMIG_REDACT_OUTPUT", "SBMIG_KB_DIR", "HTTP_PROXY", "UNRELATED"):
        assert never not in names, never


def test_env_pin_names_lists_an_override_and_its_alias_once(tmp_path: Path):
    repo = _repo(tmp_path)
    names = env_pin_names(repo, [], {"SBMIG_KB__DIR": "/a", "SBMIG_KB_DIR": "/b"})
    assert names.count("SBMIG_KB__DIR") == 1 and "SBMIG_KB_DIR" not in names
    assert "SBMIG_KB__DIR" not in env_pin_names(repo, [], {})
    assert set(ENV_PIN_ALIASES) == {"SBMIG_KB__DIR"} and ENV_PIN_ALIASES["SBMIG_KB__DIR"] == ("SBMIG_KB_DIR",)


# ── env_pin_value ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_env_pin_value_own_name_then_alias():
    assert env_pin_value("SBMIG_KB__DIR", {"SBMIG_KB__DIR": "/own", "SBMIG_KB_DIR": "/alias"}) == "/own"
    assert env_pin_value("SBMIG_KB__DIR", {"SBMIG_KB_DIR": "/alias"}) == "/alias"
    assert env_pin_value("SBMIG_KB__DIR", {}) is None
    assert env_pin_value("SBMIG_KB__DIR", {"SBMIG_KB__DIR": "", "SBMIG_KB_DIR": "/alias"}) == ""   # set-but-empty wins
    assert env_pin_value("LLM_MODEL", {"LLM_MODEL": ""}) == ""
    assert env_pin_value("LLM_MODEL", {"SBMIG_KB_DIR": "/alias"}) is None


# ── env_pins ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_env_pins_fingerprint_only_the_set_names(tmp_path: Path):
    repo = _repo(tmp_path)
    secret_value = "build_error_fixer-distinctive-4711"
    environ = {"LLM_MODEL": secret_value, "SBMIG_BUILD__MVN": "mvn", "LLM_APP_TOKEN": "t" * 20}
    pins = env_pins(repo, ["extra.yml"], environ)
    assert sorted(pins) == ["LLM_MODEL", "SBMIG_BUILD__MVN"]                  # LLM_BASE_URL, JAVA21_HOME … are unset
    assert all(re.fullmatch(r"[0-9a-f]{16}", v) for v in pins.values())
    assert pins["LLM_MODEL"] == _hash(secret_value)
    text = json.dumps(pins)
    assert secret_value not in text and "mvn" not in text


def test_env_pins_equal_values_equal_pins_different_values_differ(tmp_path: Path):
    repo = _repo(tmp_path)
    a = env_pins(repo, [], {"LLM_MODEL": "m1", "LLM_BASE_URL": "https://gw/x"})
    b = env_pins(repo, [], {"LLM_BASE_URL": "https://gw/x", "LLM_MODEL": "m1"})
    c = env_pins(repo, [], {"LLM_MODEL": "m2", "LLM_BASE_URL": "https://gw/x"})
    assert a == b
    assert a["LLM_MODEL"] != c["LLM_MODEL"] and a["LLM_BASE_URL"] == c["LLM_BASE_URL"]
    assert compare_env_pins(a, c) == {**NO_DIFF, "differs": ["LLM_MODEL"]}


def test_env_pins_set_but_empty_is_pinned_and_differs_from_unset(tmp_path: Path):
    repo = _repo(tmp_path)
    empty = env_pins(repo, [], {"LLM_MODEL": ""})
    assert empty == {"LLM_MODEL": _hash("")}
    assert compare_env_pins(empty, env_pins(repo, [], {})) == {**NO_DIFF, "main_only": ["LLM_MODEL"]}


def test_env_pins_override_and_alias_of_the_same_value_match(tmp_path: Path):
    repo = _repo(tmp_path)
    web = env_pins(repo, [], {"SBMIG_KB__DIR": "/data/kb-v2"})
    job = env_pins(repo, [], {"SBMIG_KB_DIR": "/data/kb-v2"})
    assert web == job == {"SBMIG_KB__DIR": _hash("/data/kb-v2")}
    assert compare_env_pins(web, env_pins(repo, [], {"SBMIG_KB_DIR": "/elsewhere"})) == {**NO_DIFF, "differs": ["SBMIG_KB__DIR"]}


def test_env_pins_without_environ_reads_os_environ(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("LLM_MODEL", "from-os-environ")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert env_pin_value("LLM_MODEL") == "from-os-environ"
    assert "LLM_MODEL" in env_pin_names(repo, [])
    pins = env_pins(repo, [])
    assert pins["LLM_MODEL"] == _hash("from-os-environ") and "LLM_BASE_URL" not in pins


# ── compare_env_pins ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_compare_env_pins_categories_are_sorted_names():
    pinned = {"Z_SAME": "1", "B_DIFF": "1", "A_DIFF": "1", "M_MAIN": "1", "C_MAIN": "1"}
    actual = {"Z_SAME": "1", "B_DIFF": "2", "A_DIFF": "2", "Y_WORKER": "1", "D_WORKER": "1"}
    before = dict(pinned)
    assert compare_env_pins(pinned, actual) == {"differs": ["A_DIFF", "B_DIFF"], "main_only": ["C_MAIN", "M_MAIN"],
                                                "worker_only": ["D_WORKER", "Y_WORKER"]}
    assert pinned == before                                                   # inputs are not modified


def test_compare_env_pins_identical_none_and_empty():
    assert compare_env_pins({"A": "1"}, {"A": "1"}) == NO_DIFF
    assert compare_env_pins({}, {}) == NO_DIFF
    assert compare_env_pins(None, None) == NO_DIFF
    assert compare_env_pins({"A": "1"}, None) == {**NO_DIFF, "main_only": ["A"]}
    assert compare_env_pins(None, {"A": "1"}) == {**NO_DIFF, "worker_only": ["A"]}


# ── env_pin_policy ───────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("value", "expected"), [("refuse", ENV_PIN_REFUSE), (" WARN ", ENV_PIN_WARN), ("Off\n", ENV_PIN_OFF),
                                                 ("\tRefuse", ENV_PIN_REFUSE)])
def test_env_pin_policy_normalizes_case_and_whitespace(value: str, expected: str):
    assert env_pin_policy(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "bogus", "true", "0", "refuse,warn"])
def test_env_pin_policy_unknown_or_empty_gives_the_default(value):
    assert env_pin_policy(value) == ENV_PIN_REFUSE
    assert env_pin_policy(value, ENV_PIN_OFF) == ENV_PIN_OFF
    assert env_pin_policy(value, default=ENV_PIN_WARN) == ENV_PIN_WARN


def test_env_pin_policy_a_valid_value_beats_the_default():
    assert env_pin_policy("refuse", default=ENV_PIN_OFF) == ENV_PIN_REFUSE
    assert env_pin_policy("off", default=ENV_PIN_REFUSE) == ENV_PIN_OFF
    assert ENV_PIN_POLICIES == (ENV_PIN_REFUSE, ENV_PIN_WARN, ENV_PIN_OFF) == ("refuse", "warn", "off")
    assert contract.ENV_PIN_POLICY_ENV == "SBMIG_JOB_ENV_PIN"


# ── ControlState ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_control_state_creating_transition_carries_the_pins(tmp_path: Path):
    run_dir = tmp_path / "out" / "web-1"
    pins = {"LLM_MODEL": _hash("m1"), "SBMIG_KB__DIR": _hash("/data/kb-v2")}
    st = ControlState(run_id="web-1")
    assert st.env_pins is None and st.env_pin_policy is None
    st.transition(RunState.CREATING, job="sbmig-web-1", env_pins=pins, env_pin_policy=ENV_PIN_WARN)
    assert (st.state, st.job, st.env_pins, st.env_pin_policy) == ("creating", "sbmig-web-1", pins, "warn")
    st.write(run_dir)
    raw = json.loads((run_dir / CONTROL_DIR / STATE_FILE).read_text(encoding="utf-8"))
    assert raw["env_pins"] == pins and raw["env_pin_policy"] == "warn"
    back = ControlState.read(run_dir)
    assert back is not None and back.env_pins == pins and back.env_pin_policy == "warn" and back.owner == st.owner
    # a later transition keeps them
    back.transition(RunState.RUNNING).write(run_dir)
    again = ControlState.read(run_dir)
    assert again.state == "running" and again.env_pins == pins and again.env_pin_policy == "warn"


def test_control_state_written_by_an_older_main_reads_without_pins(tmp_path: Path):
    run_dir = tmp_path / "out" / "web-1"
    old = {"run_id": "web-1", "state": "creating", "attempt": 1, "job": "sbmig-web-1", "owner": "0123456789abcdef",
           "accepted_at": "2026-11-09T00:00:00Z", "updated_at": "2026-11-09T00:00:01Z", "exit_code": None, "error": None}
    contract.atomic_write_json(contract.control_dir(run_dir) / STATE_FILE, old)
    st = ControlState.read(run_dir)
    assert st is not None and st.state == "creating" and st.job == "sbmig-web-1"
    assert st.env_pins is None and st.env_pin_policy is None


def test_control_state_pins_off_round_trip_as_none(tmp_path: Path):
    run_dir = tmp_path / "out" / "web-1"
    ControlState(run_id="web-1").transition(RunState.CREATING, job="sbmig-web-1", env_pins=None,
                                            env_pin_policy=ENV_PIN_OFF).write(run_dir)
    st = ControlState.read(run_dir)
    assert st.env_pins is None and st.env_pin_policy == "off"


def test_env_pin_names_whole_section_override_is_pinned(tmp_path: Path):
    """`load_config` reads `SBMIG_<SECTION>` (no double underscore) as a replacement of that whole section, so it is
    pinned like a key override; other single-word `SBMIG_*` names (run values, web-pod knobs) are not configuration."""
    repo = _repo(tmp_path)
    env = {"SBMIG_BUILD": "{mvn: /opt/maven/bin/mvn, java_home: '${OTHER_JAVA_HOME:-}'}", "SBMIG_WEB_MODE": "k8s-job",
           "SBMIG_OUT": "/data/out", "SBMIG_RUN_ID": "r1", "SBMIG_CONFIG": "a.yml", "SBMIG_KB_DIR": "/data/kb-v2"}
    names = env_pin_names(repo, ["extra.yml"], env)
    assert "SBMIG_BUILD" in names and "OTHER_JAVA_HOME" in names
    assert not {"SBMIG_WEB_MODE", "SBMIG_OUT", "SBMIG_RUN_ID", "SBMIG_CONFIG", "SBMIG_KB_DIR"} & set(names)
    other = dict(env, SBMIG_BUILD="{mvn: mvn}")
    assert compare_env_pins(env_pins(repo, ["extra.yml"], env), env_pins(repo, ["extra.yml"], other))["differs"] == ["SBMIG_BUILD"]


@pytest.mark.parametrize("value", [3, 1.5, True, ["refuse"], {"policy": "off"}])
def test_env_pin_policy_a_non_string_gives_the_default(value):
    """A damaged `state.json` may carry any JSON value: never an exception, always the default."""
    assert env_pin_policy(value, ENV_PIN_WARN) == ENV_PIN_WARN
    assert env_pin_policy(value) == ENV_PIN_REFUSE
