"""The admin page's sums carry the "partly unknown" mark of a run whose remediation fix-loop usage is partly unknown.

`collect_usage` adds such a run's priced part into the project row, the overall total and the per-model rows; each of
those sums says `usage_partly_unknown: true` (left out when false), and the page writes the existing 「일부 알 수 없음」
note beside that call count / cost. A healthy answer renders without the note. No Maven, no LLM.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import sbmig.web
from sbmig.remediations.models import FixResult, FixUsage, RemediationState, RoundState
from sbmig.web.admin import collect_usage

PAGE = (Path(sbmig.web.__file__).parent / "static" / "admin.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]


def _run(out: Path, rid: str, project: str, *, report_calls: int, report_cost: float, fix: FixResult | None) -> None:
    rd = out / rid
    rd.mkdir(parents=True)
    rep = {"project_name": project, "started_at": "2026-11-20T09:00:00+00:00",
           "llm_usage": {"calls": report_calls, "input_tokens": 10 * report_calls, "output_tokens": report_calls},
           "llm_usage_by_model": [{"model": "build_error_fixer", "calls": report_calls,
                                   "input_tokens": 10 * report_calls, "output_tokens": report_calls}],
           "llm_cost": {"currency": "USD", "total": report_cost,
                        "by_model": [{"model": "build_error_fixer", "calls": report_calls, "cost": report_cost}]}}
    (rd / "report.json").write_text(json.dumps(rep), encoding="utf-8")
    if fix is not None:
        (rd / "remediation").mkdir()
        st = RemediationState(id=rid, rounds=[RoundState(round=1, fix=fix)])
        (rd / "remediation" / "state.json").write_text(json.dumps(st.to_dict()), encoding="utf-8")


def _partly_unknown_fix() -> FixResult:
    # two priced calls and one call whose tokens are unknown, all on the migration's model name
    return FixResult(attempted=True, llm_calls=3, currency="USD", usage=[
        FixUsage(model="build_error_fixer", calls=2, input_tokens=100, output_tokens=10, cost=0.5),
        FixUsage(model="build_error_fixer", calls=1, tokens_unknown=True)])


def _healthy(out: Path) -> dict:
    _run(out, "demo-20261120-090000-aaaaaa", "demo", report_calls=1, report_cost=0.25, fix=None)
    _run(out, "demo-20261120-090000-bbbbbb", "demo", report_calls=2, report_cost=0.5, fix=FixResult(
        attempted=True, llm_calls=1, currency="USD",
        usage=[FixUsage(model="build_error_fixer", calls=1, input_tokens=5, output_tokens=1, cost=0.1)]))
    return collect_usage(out)


def _one_partly_unknown(out: Path) -> dict:
    _run(out, "demo-20261120-090000-aaaaaa", "demo", report_calls=1, report_cost=0.25, fix=None)
    _run(out, "demo-20261120-090000-bbbbbb", "demo", report_calls=1, report_cost=0.25, fix=_partly_unknown_fix())
    _run(out, "other-20261120-090000-cccccc", "other", report_calls=1, report_cost=0.25, fix=None)
    return collect_usage(out)


def test_a_healthy_answer_has_no_mark_anywhere(tmp_path):
    d = _healthy(tmp_path)
    assert "usage_partly_unknown" not in json.dumps(d)
    assert d["totals"]["cost"] == 0.85 and d["projects"][0]["cost"] == 0.85


def test_one_partly_unknown_run_marks_its_project_the_total_and_the_model_row(tmp_path):
    d = _one_partly_unknown(tmp_path)
    demo = next(p for p in d["projects"] if p["project"] == "demo")
    other = next(p for p in d["projects"] if p["project"] == "other")
    # the project of two runs: the known part summed, marked
    assert demo["usage_partly_unknown"] is True and demo["calls"] == 5 and demo["cost"] == 1.0
    assert "usage_partly_unknown" not in other and other["cost"] == 0.25
    # only the run whose usage is partly unknown carries the run-level mark
    marks = {r["run_id"]: r.get("usage_partly_unknown") for r in demo["run_list"]}
    assert marks == {"demo-20261120-090000-aaaaaa": None, "demo-20261120-090000-bbbbbb": True}
    # the overall total
    assert d["totals"]["usage_partly_unknown"] is True and d["totals"]["cost"] == 1.25 and d["totals"]["calls"] == 6
    # the model row that received the call without token counts keeps its numeric cost, marked
    [m] = d["by_model"]
    assert m["model"] == "build_error_fixer" and m["usage_partly_unknown"] is True
    assert m["calls"] == 6 and m["cost"] == 1.25


def test_a_model_row_without_unknown_calls_is_not_marked(tmp_path):
    _run(tmp_path, "demo-20261120-090000-bbbbbb", "demo", report_calls=1, report_cost=0.25, fix=FixResult(
        attempted=True, llm_calls=2, currency="USD", usage=[
            FixUsage(model="build_error_fixer", calls=1, input_tokens=5, output_tokens=1, cost=0.1),
            FixUsage(model="escalation_fixer", calls=1, tokens_unknown=True)]))
    d = collect_usage(tmp_path)
    rows = {m["model"]: m for m in d["by_model"]}
    assert "usage_partly_unknown" not in rows["build_error_fixer"] and rows["build_error_fixer"]["cost"] == 0.35
    assert rows["escalation_fixer"]["usage_partly_unknown"] is True and rows["escalation_fixer"]["cost"] is None
    assert d["projects"][0]["usage_partly_unknown"] is True and d["totals"]["usage_partly_unknown"] is True


def test_an_unread_loop_marks_the_sums_but_no_model_row(tmp_path):
    _run(tmp_path, "demo-20261120-090000-bbbbbb", "demo", report_calls=1, report_cost=0.25,
         fix=FixResult(attempted=True, usage_unknown=True))
    d = collect_usage(tmp_path)
    assert d["projects"][0]["usage_partly_unknown"] is True and d["totals"]["usage_partly_unknown"] is True
    assert d["totals"]["cost"] == 0.25
    assert all("usage_partly_unknown" not in m for m in d["by_model"])


# ------------------------------------------------------------------------------------------------ the page
def test_the_page_passes_the_mark_of_every_sum_to_the_existing_note():
    assert "costCell(p.cost, p.usage_partly_unknown === true)" in SCRIPT
    assert "sumCallsCell(p.calls, p.usage_partly_unknown === true)" in SCRIPT
    assert "const mu = m.usage_partly_unknown === true;" in SCRIPT
    assert "sumCallsCell(m.calls, mu)" in SCRIPT and "costCell(m.cost, mu)" in SCRIPT
    assert "tile('호출 수', fmtInt(t.calls), t.usage_partly_unknown === true ? UNKNOWN_NOTE : '')" in SCRIPT
    assert "tile('비용', esc(fmtCost(t.cost)), t.usage_partly_unknown === true ? UNKNOWN_NOTE : '')" in SCRIPT
    # numbers only through the formatters; the note is the page's own constant
    assert "const sumCallsCell = (v, unknown) => unknown ? `<td class=\"num\">${fmtInt(v)}${UNKNOWN_NOTE}</td>` : numCell(v);" \
        in SCRIPT


_STUB = r"""
const els = {};
const el = () => ({ innerHTML: '', textContent: '', hidden: false, title: '', disabled: false, value: '',
  dataset: {}, style: { setProperty() {} }, classList: { toggle() {}, add() {}, remove() {} },
  addEventListener() {}, focus() {}, querySelector() { return null; }, closest() { return null; },
  getAttribute() { return null; }, setAttribute() {}, clientWidth: 1000, firstElementChild: null });
globalThis.document = { getElementById: (id) => (els[id] = els[id] || el()), addEventListener() {} };
globalThis.window = { addEventListener() {}, location: { assign() {} } };
globalThis.fetch = () => new Promise(() => {});
"""


def render(data: dict, script: str = SCRIPT) -> dict:
    """Run the page's own script in node with a stub DOM and render the three usage blocks from `data`."""
    hook = ("\n  globalThis.__t = (d) => { data = d; currency = d.totals && d.totals.currency ? "
            "String(d.totals.currency) : null; renderTotals(); renderModels(); renderProjects(); };\n")
    body = re.sub(r"\n\}\)\(\);\s*$", hook + "})();\n", script)
    assert "globalThis.__t" in body
    code = _STUB + body + (
        "\n__t(" + json.dumps(data) + ");\nconsole.log(JSON.stringify({tiles: els.tiles.innerHTML, "
        "costnote: els.costnote.textContent, models: els.models.innerHTML, projects: els.projects.innerHTML}));")
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_renders_the_note_beside_every_marked_sum_and_nowhere_on_a_healthy_page(tmp_path):
    healthy = render(_healthy(tmp_path / "a"))
    assert all("일부 알 수 없음" not in v for v in healthy.values())
    html = render(_one_partly_unknown(tmp_path / "b"))
    note = "일부 알 수 없음</span>"
    # totals: beside the call count and the cost
    assert html["tiles"].count(note) == 2
    # the model row: calls and cost
    assert html["models"].count(note) == 2
    # the project rows: only the project of the partly unknown run, calls and cost
    demo_row = next(r for r in html["projects"].split('<tr class="prow') if 'data-name="demo"' in r)
    other_row = next(r for r in html["projects"].split('<tr class="prow') if 'data-name="other"' in r)
    assert demo_row.count(note) == 2 and note not in other_row


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_total_without_a_cost_says_why(tmp_path):
    _run(tmp_path, "demo-20261120-090000-bbbbbb", "demo", report_calls=0, report_cost=0.0, fix=FixResult(
        attempted=True, llm_calls=1, usage=[FixUsage(model="build_error_fixer", calls=1, tokens_unknown=True)]))
    d = collect_usage(tmp_path)
    assert d["totals"]["cost"] is None and d["totals"]["usage_partly_unknown"] is True
    html = render(d)
    assert html["costnote"] == "사용량 일부를 알 수 없는 실행이 있어 전체 비용은 표시하지 않습니다."
