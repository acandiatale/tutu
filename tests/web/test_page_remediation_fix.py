"""The remediation card after the build fix loop (INTERFACES "v2.1z-bi" B, "Report and page"): a chip 「소스 <n>개
수정」 when a round was fixed, a `fix.patch` download when the server lists it, and the page's 「LLM 호출 N회」
including the calls of the remediation's fix loop exactly once: `GET /api/runs/<id>` reports them inside `llm_calls`
(`remediation_llm_calls` of them), and the live `fix.llm_calls` of `GET /api/remediations/<id>` replaces that part. The small
helpers are cut out of the page and run under node; the wiring is checked on the source."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import sbmig.web

PAGE = (Path(sbmig.web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _function(name: str) -> str:
    m = re.search(r"\n  (?:async )?function " + name + r"\(.*?\n  }\n", SCRIPT, re.DOTALL)
    assert m, name
    return m.group(0)


def _run(calls: list[list], tmp_path: Path) -> list:
    src = "\n".join(_function(n) for n in ("remFixChip", "remFixLlm", "llmTotal"))
    js = tmp_path / "run.js"
    js.write_text(src + "\nconst calls = " + json.dumps(calls) + ";\n"
                  "process.stdout.write(JSON.stringify(calls.map(([f, ...a]) => eval(f)(...a))));\n", encoding="utf-8")
    p = subprocess.run([NODE, str(js)], check=False, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@needs_node
def test_fix_chip_text(tmp_path):
    out = _run([["remFixChip", {"fixed": True, "changed": 3, "llm_calls": 2}],
                ["remFixChip", {"fixed": False, "changed": 0, "llm_calls": 5}],
                ["remFixChip", {"fixed": True, "changed": 0}],
                ["remFixChip", None],
                ["remFixChip", "x"],
                ["remFixChip", {"fixed": "true", "changed": 2}]], tmp_path)
    assert out == ["소스 3개 수정", "", "", "", "", ""]


@needs_node
def test_llm_total_counts_the_remediation_calls_once(tmp_path):
    # the server's llm_calls already holds the remediation's fix-loop calls (remediation_llm_calls of them);
    # the live count of GET /api/remediations replaces that part, it is never added on top
    out = _run([["llmTotal", {"llm_calls": 10, "remediation_llm_calls": 3}, 3],
                ["llmTotal", {"llm_calls": 10, "remediation_llm_calls": 3}, None],   # remediation status not read yet
                ["llmTotal", {"llm_calls": 10, "remediation_llm_calls": 3}, 5],      # a resume pod made two more calls
                ["llmTotal", {"llm_calls": 7, "remediation_llm_calls": 0}, 0],
                ["llmTotal", {"llm_calls": 7}, None],                                # an older server: no breakdown
                ["llmTotal", {"llm_calls": 4, "remediation_llm_calls": 4}, 4],       # a remediation-only run
                ["llmTotal", {"llm_calls": None}, 4],                                # report not written yet
                ["llmTotal", {}, None],                                              # nothing known: no count
                ["llmTotal", {}, 0],
                ["llmTotal", None, 2],
                ["remFixLlm", {"llm_calls": 6}],
                ["remFixLlm", {"llm_calls": "6"}],
                ["remFixLlm", None]], tmp_path)
    assert out == [10, 10, 12, 7, 7, 4, 4, None, None, 2, 6, 6, 0]


REFRESH = r"""
const els = { summary: { textContent: '' }, llmchip: { textContent: 'LLM 호출 2회' } };
const $ = (id) => els[id] || null;
const setText = (id, v) => { els[id].textContent = v; };
__FN__
const out = [];
for (const [before, n] of JSON.parse(process.argv[2])) { els.summary.textContent = before; refreshLlm(n); out.push(els.summary.textContent); }
out.push(els.llmchip.textContent);
process.stdout.write(JSON.stringify(out));
"""


@needs_node
def test_refresh_changes_only_the_llm_count(tmp_path):
    js = tmp_path / "refresh.js"
    js.write_text(REFRESH.replace("__FN__", _function("refreshLlm")), encoding="utf-8")
    cases = [["LLM 호출 2회 · 취약점 조치 완료", 5], ["취약점 조치 완료", 3], ["", 4], ["LLM 호출 2회", None]]
    p = subprocess.run([NODE, str(js), json.dumps(cases)], check=False, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout) == ["LLM 호출 5회 · 취약점 조치 완료", "LLM 호출 3회 · 취약점 조치 완료", "LLM 호출 4회",
                                    "LLM 호출 2회", "LLM 호출 4회"]


def test_card_wiring():
    render = _function("renderRemediation")
    assert "remFixChip(st.fix)" in render and "el('span', 'chip warn', fixChip)" in render
    assert "link('fix.patch', '자동 수정 내용 받기', 'quiet')" in render
    # the remediation's calls reach the summary line and the result card's chip, as text only
    assert "remLlm = fixLlm" in render and "refreshLlm(llmTotal(lastRunSt, remLlm))" in render
    assert "innerHTML" not in render and "innerHTML" not in _function("refreshLlm")
    assert "llmTotal(st, remLlm)" in _function("setStatus")
    assert "llmTotal(st, remLlm)" in _function("showResult")
    assert "llmTotal(st.llm_calls" not in SCRIPT and "lastRunSt.llm_calls" not in SCRIPT
    assert "st.llm_calls != null" not in SCRIPT
    # a new run starts from "not read yet", and so does a hidden card
    assert "remLlm = null" in _function("hideRemediation")
    assert "lastRunSt = null; remLlm = null;" in _function("attach")
