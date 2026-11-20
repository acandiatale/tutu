"""The two report previews of the result card (user, 2026-11-20): the migration report preview exists only when a
migration ran; the remediation report preview sits in the remediation card whenever the remediation lists `report.md`,
for a remediation-only run and for a chained run, and shows exactly `GET /api/remediations/<id>/files/report.md` (the
bytes of the 「조치 결과 보고서」 download). A remediation-only run shows its short run report under 「보고서 미리보기」
until the remediation report exists, then only the remediation preview.

The preview code of `index.html` is cut out and run by node against a small fake DOM with a controllable `fetch`, so
the tests can say how often the report is fetched (once per change of the status's `updated`), what a late answer for
a run the user has left does (nothing), and that the 원문 pane, the rendered pane and the dialog show one document."""
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


def _block() -> str:
    start = SCRIPT.index("  // 보기와 원문 전환 버튼.")
    end = SCRIPT.index("  $('report-big').addEventListener('click', openMigrationReport);")
    return SCRIPT[start:end]


def _line_fn(name: str) -> str:
    m = re.search(r"\n  function " + name + r"\(.*?\n", SCRIPT)
    assert m, name
    return m.group(0)


def _function(name: str) -> str:
    m = re.search(r"\n  (?:async )?function " + name + r"\(.*?\n  }\n", SCRIPT, re.DOTALL)
    assert m, name
    return m.group(0)


HARNESS = r"""
const mkEl = (id) => ({ id, textContent: '', hidden: true, open: false, dataset: {}, href: '', attrs: {}, scrollTop: 5,
  replaceChildren(...c) { this.kids = c; },
  setAttribute(k, v) { this.attrs[k] = String(v); }, removeAttribute(k) { delete this.attrs[k]; } });
const els = {};
for (const id of ['preview', 'preview-label', 'report', 'report-view', 'rem-preview', 'rem-report', 'rem-report-view',
                  'rv-dialog', 'rv-title', 'rv-name', 'rv-raw', 'rv-view', 'rv-dl', 'rv-dlname', 'rv-body', 'runname', 'runid'])
  els[id] = mkEl(id);
els['rv-dialog'].open = false;
els['rv-dialog'].showModal = function () { this.open = true; };
els['rv-dialog'].close = function () { this.open = false; };
els.runname.hidden = true; els.runid.textContent = 'R1';
const mkBtn = (mode) => ({ dataset: { mode }, attrs: {}, setAttribute(k, v) { this.attrs[k] = String(v); }, addEventListener() {} });
const segs = ['mig', 'rem', 'dlg'].map((g) => ({ dataset: { rv: g }, btns: [mkBtn('view'), mkBtn('raw')],
  querySelectorAll() { return this.btns; } }));
const document = { querySelectorAll: (q) => (q === '.rvseg' ? segs : []), activeElement: null,
  documentElement: { classList: { add() {}, remove() {} } } };
const $ = (id) => els[id];
const FLOW_FULL = 'full', FLOW_REM = 'remediation';
let current = 'R1', runFlow = FLOW_REM;
const MdView = { parse: (t) => t, render: (t, d, o) => ({ md: t, link: o.resolve('report.json') }) };
function remUrl(id, tail) { return `/api/remediations/${encodeURIComponent(id)}` + (tail || ''); }
function fileUrl(name, download) { return `/api/runs/${encodeURIComponent(current)}/files/${name}` + (download ? '?download=1' : ''); }
const ACTIVE_JOB_DONE = ['succeeded', 'failed', 'completed', 'finished', 'done', 'missing', 'ended'];
__FNS__
__BLOCK__
// fetch: every call waits until the test answers it
const calls = [];
globalThis.fetch = (url) => new Promise((resolve) => calls.push({ url, answer: (ok, text) => resolve({ ok, text: async () => text }) }));
const tick = () => new Promise((r) => setTimeout(r, 0));
const snap = () => ({
  label: els['preview-label'].textContent, main: els.preview.hidden ? null : els.report.textContent,
  mainView: els['report-view'].dataset.src || null,
  rem: els['rem-preview'].hidden ? null : els['rem-report'].textContent, remView: els['rem-report-view'].dataset.src || null,
  remOpen: els['rem-preview'].open, dlg: els['rv-dialog'].open ? { title: els['rv-title'].textContent, raw: els['rv-raw'].textContent,
    view: els['rv-view'].dataset.src || null, dl: els['rv-dl'].attrs.download || null, href: els['rv-dl'].href } : null,
  fetches: calls.length });
const out = {};
(async () => {
__SCENARIO__
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def run(scenario: str, tmp_path: Path) -> dict:
    fns = "".join(_line_fn(n) for n in ("remActive",)) + _function("jobActive") + _function("mdInto")
    js = tmp_path / "run.js"
    js.write_text(HARNESS.replace("__FNS__", fns).replace("__BLOCK__", _block()).replace("__SCENARIO__", scenario),
                  encoding="utf-8")
    p = subprocess.run([NODE, str(js)], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


DONE = "{ status: 'SUCCEEDED', updated: %s }"


@needs_node
def test_remediation_only_short_report_then_the_remediation_report(tmp_path):
    out = run(r"""
  setReportText('SHORT');
  out.before = snap();
  remPreviewSync('R1', { status: 'RUNNING', updated: 1 }, ['remediation.log']);   // no report.md yet: nothing fetched
  await tick(); out.noReport = snap();
  remPreviewSync('R1', """ + DONE % 2 + r""", ['report.md', 'result.zip']);
  remPreviewSync('R1', """ + DONE % 2 + r""", ['report.md', 'result.zip']);       // the same status twice: one fetch
  out.inFlight = snap();
  out.url = calls[0].url;
  calls[0].answer(true, 'REM'); await tick();
  out.after = snap();
  for (let i = 0; i < 5; i++) remPreviewSync('R1', """ + DONE % 2 + r""", ['report.md', 'result.zip']);   // polls
  await tick(); out.polled = snap();
  setReportText('SHORT');                                   // the run's report event again (re-attach): no flicker
  out.reportAgain = snap();
""", tmp_path)
    assert out["before"]["label"] == "보고서 미리보기" and out["before"]["main"] == "SHORT"
    assert out["before"]["mainView"] == "SHORT" and out["before"]["rem"] is None
    assert out["noReport"]["fetches"] == 0 and out["noReport"]["main"] == "SHORT"
    # the fallback stays until the remediation report has arrived (no empty gap)
    assert out["inFlight"]["fetches"] == 1 and out["inFlight"]["main"] == "SHORT" and out["inFlight"]["rem"] is None
    assert out["url"] == "/api/remediations/R1/files/report.md"
    assert out["after"]["rem"] == "REM" and out["after"]["remView"] == "REM"
    assert out["after"]["main"] is None and out["after"]["mainView"] is None      # never both in a remediation-only run
    assert out["polled"]["fetches"] == 1
    assert out["reportAgain"]["main"] is None and out["reportAgain"]["rem"] == "REM"


@needs_node
def test_refetch_only_when_updated_changes_and_not_while_a_resume_runs(tmp_path):
    out = run(r"""
  const F = ['report.md'];
  remPreviewSync('R1', """ + DONE % 1 + r""", F); calls[0].answer(true, 'V1'); await tick();
  remPreviewSync('R1', { status: 'RUNNING', updated: 5 }, F);                   // a resume pod runs: wait for its end
  remPreviewSync('R1', { status: 'QUEUED', updated: 6 }, F);
  await tick(); out.running = snap();
  remPreviewSync('R1', """ + DONE % 7 + r""", F);
  out.ended = snap().fetches;
  calls[1].answer(true, 'V2'); await tick();
  out.v2 = snap();
""", tmp_path)
    assert out["running"]["fetches"] == 1 and out["running"]["rem"] == "V1"
    assert out["ended"] == 2 and out["v2"]["rem"] == "V2"


@needs_node
def test_failed_fetch_keeps_the_text_and_tries_again_on_the_next_change(tmp_path):
    out = run(r"""
  const F = ['report.md'];
  setReportText('SHORT');
  remPreviewSync('R1', """ + DONE % 1 + r""", F); calls[0].answer(false, 'err'); await tick();
  out.failed = snap();
  remPreviewSync('R1', """ + DONE % 1 + r""", F); await tick();                  // same status: no retry on every poll
  out.samePoll = snap().fetches;
  remPreviewSync('R1', """ + DONE % 2 + r""", F); calls[1].answer(true, 'V1'); await tick();
  out.ok = snap();
  remPreviewSync('R1', """ + DONE % 3 + r""", F);
  calls[2].answer(false, ''); await tick();
  out.failAfterOk = snap();
""", tmp_path)
    assert out["failed"]["main"] == "SHORT" and out["failed"]["rem"] is None
    assert out["samePoll"] == 1
    assert out["ok"]["fetches"] == 2 and out["ok"]["rem"] == "V1"
    assert out["failAfterOk"]["rem"] == "V1"


@needs_node
def test_a_late_answer_for_a_run_the_user_left_is_dropped(tmp_path):
    out = run(r"""
  const F = ['report.md'];
  remPreviewSync('R1', """ + DONE % 1 + r""", F);
  current = 'R2'; remPreviewReset(); setReportText('');                       // attach to another run
  calls[0].answer(true, 'R1 REPORT'); await tick();
  out.late = snap();
  // R2: an older answer arriving after a newer request is dropped too
  remPreviewSync('R2', """ + DONE % 1 + r""", F);
  remPreviewSync('R2', """ + DONE % 2 + r""", F);
  calls[2].answer(true, 'NEW'); await tick();
  calls[1].answer(true, 'OLD'); await tick();
  out.order = snap();
  // back to R1 from 지난 실행: R2's text is never shown under R1
  current = 'R1'; remPreviewReset(); setReportText('R1 SHORT');
  out.back = snap();
""", tmp_path)
    assert out["late"]["rem"] is None and out["late"]["main"] is None
    assert out["order"]["rem"] == "NEW"
    assert out["back"]["rem"] is None and out["back"]["main"] == "R1 SHORT"


@needs_node
def test_full_and_chained_runs_show_both_previews_separately(tmp_path):
    out = run(r"""
  runFlow = FLOW_FULL;
  setReportText('MIG');
  out.full = snap();
  remPreviewSync('R1', """ + DONE % 1 + r""", ['report.md']); calls[0].answer(true, 'REM'); await tick();
  out.chained = snap();
""", tmp_path)
    assert out["full"]["label"] == "마이그레이션 보고서 미리보기" and out["full"]["main"] == "MIG" and out["full"]["rem"] is None
    assert out["chained"]["main"] == "MIG" and out["chained"]["rem"] == "REM"
    assert out["chained"]["mainView"] == "MIG" and out["chained"]["remView"] == "REM"


@needs_node
def test_dialog_shows_the_document_of_the_preview_that_opened_it(tmp_path):
    out = run(r"""
  runFlow = FLOW_FULL;
  setReportText('MIG');
  remPreviewSync('R1', """ + DONE % 1 + r""", ['report.md', 'report.json']); calls[0].answer(true, 'REM'); await tick();
  openRemReport(); out.rem = snap();
  out.remLink = els['rv-view'].kids[0].link;
  remPreviewSync('R1', """ + DONE % 2 + r""", ['report.md']); calls[1].answer(true, 'REM2'); await tick();
  out.remUpdated = snap();
  setReportText('MIG2');                               // the other preview's text does not touch the dialog
  out.untouched = snap().dlg.raw;
  closeReport(); openMigrationReport(); out.mig = snap();
""", tmp_path)
    assert out["rem"]["dlg"] == {"title": "취약점 조치 보고서", "raw": "REM", "view": "REM", "dl": "remediation-report.md",
                                 "href": "/api/remediations/R1/files/report.md?download=1"}
    assert out["remLink"] == "/api/remediations/R1/files/report.json"
    assert out["remUpdated"]["dlg"]["raw"] == "REM2" and out["remUpdated"]["dlg"]["view"] == "REM2"
    assert out["remUpdated"]["rem"] == "REM2" and out["remUpdated"]["remView"] == "REM2"
    assert out["untouched"] == "REM2"
    assert out["mig"]["dlg"]["title"] == "마이그레이션 보고서" and out["mig"]["dlg"]["raw"] == "MIG2"


@needs_node
def test_remediation_only_dialog_follows_the_switch_to_the_remediation_report(tmp_path):
    out = run(r"""
  setReportText('SHORT');
  els.preview.open = true;
  openMigrationReport(); out.short = snap();
  remPreviewSync('R1', """ + DONE % 1 + r""", ['report.md']); calls[0].answer(true, 'REM'); await tick();
  out.after = snap();
""", tmp_path)
    assert out["short"]["dlg"]["title"] == "실행 보고서" and out["short"]["dlg"]["raw"] == "SHORT"
    # the fallback disappears, so a dialog showing it closes; an opened fallback hands its open state on
    assert out["after"]["dlg"] is None and out["after"]["remOpen"] is True and out["after"]["main"] is None


@needs_node
def test_each_preview_keeps_its_own_view_mode_and_the_dialog_follows_its_opener(tmp_path):
    out = run(r"""
  runFlow = FLOW_FULL;
  setReportText('MIG');
  remPreviewSync('R1', """ + DONE % 1 + r""", ['report.md']); calls[0].answer(true, 'REM'); await tick();
  setRvMode('rem', 'raw');
  const st = () => ({ migRaw: !els.report.hidden, migView: !els['report-view'].hidden, remRaw: !els['rem-report'].hidden,
                      remView: !els['rem-report-view'].hidden, dlgRaw: !els['rv-raw'].hidden,
                      pressed: segs.map((s) => s.btns.find((b) => b.attrs['aria-pressed'] === 'true').dataset.mode) });
  out.a = st();
  openRemReport(); out.b = st();
  setRvMode('dlg', 'view'); out.c = st();
  closeReport(); els['rv-dialog'].open = false; rv.dlg = ''; openMigrationReport(); out.d = st();
""", tmp_path)
    assert out["a"] == {"migRaw": False, "migView": True, "remRaw": True, "remView": False, "dlgRaw": False,
                        "pressed": ["view", "raw", "view"]}
    assert out["b"]["dlgRaw"] is True and out["b"]["pressed"] == ["view", "raw", "raw"]
    # switching inside the dialog switches the preview that opened it, so both show the same pane
    assert out["c"]["remRaw"] is False and out["c"]["dlgRaw"] is False and out["c"]["pressed"] == ["view", "view", "view"]
    assert out["d"]["dlgRaw"] is False and out["d"]["migView"] is True


def test_page_wiring():
    prev = PAGE[PAGE.index('<details class="preview" id="rem-preview" hidden>'):]
    prev = prev[:prev.index("</details>")]
    assert "<summary>조치 결과 보고서 미리보기</summary>" in prev
    assert 'id="rem-report-view"' in prev and '<pre class="report" id="rem-report" hidden></pre>' in prev
    assert 'id="rem-report-big"' in prev and "크게 보기" in prev and ">원문<" in prev and ">보기<" in prev
    # the remediation preview lives inside the remediation card, the migration preview stays at the end of the card
    remed = PAGE.index('<div class="remed" id="remed" hidden>')
    assert remed < PAGE.index('id="rem-preview"') < PAGE.index('id="cdblock"') < PAGE.index('id="preview"')
    assert '<summary id="preview-label">보고서 미리보기</summary>' in PAGE
    render = _function("renderRemediation")
    assert render.index("remPreviewSync(runId, st, files);") < render.index("if (rem.busy) return;")
    assert "openRemReport(runId, files)" not in SCRIPT and "remed-view" not in SCRIPT      # the duplicate button is gone
    assert "if (rp.run !== runId) remPreviewReset(); setReportText('');" in _function("attach")
    assert "$('rem-report-big').addEventListener('click', openRemReport);" in SCRIPT
    assert "if (flowChanged) renderPreview();" in _function("setStatus")
    # report text only ever goes in as text or DOM nodes
    assert "innerHTML" not in _block()
