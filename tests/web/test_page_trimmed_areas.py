"""The upload page shows neither the advanced settings nor the list of other run files (the admin page serves those)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[2] / "sbmig" / "web" / "static" / "index.html").read_text(encoding="utf-8")


def test_advanced_settings_are_hidden_but_their_fields_stay_for_the_start_request():
    m = re.search(r"<details class=\"adv\" id=\"adv\"([^>]*)>", PAGE)
    assert m and "hidden" in m.group(1)
    for field in ('id="cfgs"', 'id="llm"', 'id="stop_after"'):
        assert field in PAGE
    assert "고급 설정에서 바꿈" not in PAGE
    # no style gives the block a display value that would beat the hidden attribute
    assert not re.search(r"details\.adv\s*\{[^}]*display", PAGE)


def test_other_files_are_not_listed_on_the_page():
    assert "<span>그 밖의 파일</span>" not in PAGE
    assert "$('otherfiles').textContent = '';" in PAGE
    # the notice about a missing result ZIP stays
    assert "변환된 프로젝트 ZIP이 없습니다" in PAGE


# ---------------------------------------------------------------------------- third review of the fix loop, item 7
# A browser restores form values (back / forward, session restore): a `llm=fake` or a `stop_after` chosen before the
# block was hidden would still be sent. While `#adv` is hidden the start request carries no `llm_provider`, no
# `stop_after` and no `configs` (the server's default list), whatever the hidden controls hold.
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
NODE = shutil.which("node")


def _function(name: str) -> str:
    m = re.search(r"\n  function " + name + r"\(.*?\n  }\n", SCRIPT, re.DOTALL)
    assert m, name
    return m.group(0)


def test_the_hidden_controls_do_not_keep_restored_values():
    for sel in ("llm", "stop_after"):
        m = re.search(r'<select id="' + sel + r'"([^>]*)>', PAGE)
        assert m and 'autocomplete="off"' in m.group(1), sel
    assert '<input type="checkbox" autocomplete="off"' in PAGE          # the config checkboxes


def test_the_start_request_goes_through_start_query_and_asks_whether_the_block_is_shown():
    start = SCRIPT[SCRIPT.index("$('start').addEventListener('click'"):]
    start = start[:start.index("$('start').disabled = true;")]
    assert "startQuery({" in start and "adv: advShown()" in start
    # project and flow are set beside it; no other place of the handler adds the hidden settings
    assert "q.set('project', projectValue());" in start and "q.set('flow', flow);" in start
    assert "q.set('llm_provider'" not in start and "q.set('stop_after'" not in start and "q.set('configs'" not in start
    assert "function advShown() { return !$('adv').hidden; }" in SCRIPT
    # the configs the request is built from are the server's default while the block is hidden
    assert "if (!advShown()) return defaultConfigs.slice();" in _function("chosenConfigs")


def _run_start_query(cases: list[dict]) -> list[dict]:
    js = ("const FLOW_FULL = 'full', FLOW_REM = 'remediation';\n" + _function("startQuery")
          + f"\nconst cases = {json.dumps(cases)};\n"
          + "console.log(JSON.stringify(cases.map((c) => Object.fromEntries(startQuery(c).entries()))));\n")
    out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_start_query_sends_no_hidden_value_while_the_block_is_hidden():
    restored = {"name": "a.zip", "flow": "full", "configs": ["artifacts.local-e2e.yml"],
                "defaults": ["artifacts.yml"], "stopAfter": "plan", "llm": "fake"}
    hidden, shown = _run_start_query([{**restored, "adv": False}, {**restored, "adv": True}])
    assert hidden == {"name": "a.zip"}
    assert shown == {"name": "a.zip", "configs": "artifacts.local-e2e.yml", "stop_after": "plan", "llm_provider": "fake"}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_start_query_with_the_block_shown_keeps_its_old_rules():
    base = {"name": "a.zip", "adv": True, "defaults": ["artifacts.yml"], "llm": ""}
    same, rem = _run_start_query([
        {**base, "flow": "full", "configs": ["artifacts.yml"], "stopAfter": ""},
        {**base, "flow": "remediation", "configs": ["artifacts.yml"], "stopAfter": "plan"}])
    assert same == {"name": "a.zip"}                       # the default list is not sent
    assert rem == {"name": "a.zip"}                        # no stop_after for a remediation run
