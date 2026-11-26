"""v2.1z-ck, the page: a framework project (scope `jdk-jakarta`) whose JDK 21 + Jakarta conversion is done reads as
done (완료, success colour) also when the build after it fails. The user: "사내 프레임워크는 이정도면 완료니까 미완료로
하지말고 … 완료로 화면에 표시되도록 해줘! 빌드가 안되더라도 프로세스상으로는 완료니까!"

The status carries `conversion_complete: true` for such a run. The page's helpers are cut out of the page and run
under node with crafted status objects and journal events (no server key needed); the wiring is checked on the
source. The rail was also looked at in a real browser against a stored run."""
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
CSS = PAGE.split("<style>", 1)[1].split("</style>", 1)[0]
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

TITLE = "JDK 21, Jakarta 변환을 마쳤습니다. 변환 뒤 빌드는 통과하지 않았습니다"
NOTE = "변환 뒤 빌드는 통과하지 않았습니다. 남은 오류는 보고서에 있습니다."
OLD_NOTE = "빌드 검증 단계가 실패로 끝났습니다"


def _function(name: str) -> str:
    one = re.search(r"\n(  function " + re.escape(name) + r"\([^\n]*\{[^\n]*\})\n", SCRIPT)
    if one and one.group(1).count("{") == one.group(1).count("}"):     # a function written on one line
        return "\n" + one.group(1) + "\n"
    m = re.search(r"\n  function " + re.escape(name) + r"\(.*?\n  }\n", SCRIPT, re.DOTALL)
    assert m, name
    return m.group(0)


def _line(prefix: str) -> str:
    m = re.search(r"\n(  " + re.escape(prefix) + r"[^\n]*?;)[^\n]*\n", SCRIPT)
    assert m, prefix
    return m.group(1)


def _const_block(start: str, end: str) -> str:
    i = SCRIPT.index(start)
    j = SCRIPT.index(end, i) + len(end)
    return SCRIPT[i:j]


def _node(js: str, tmp_path: Path):
    f = tmp_path / "run.js"
    f.write_text(js, encoding="utf-8")
    p = subprocess.run([NODE, str(f)], capture_output=True, text=True, timeout=60, encoding="utf-8", check=False)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)



# ------------------------------------------------------------------------------------------------- 1. describe()
def _describe_js() -> str:
    parts = [
        "const FLOW_FULL = 'full', FLOW_REM = 'remediation';",
        _line("const CONV_DONE_TITLE"),
        _line("const LEGACY_WAITING ="),
        _line("const LEGACY_WAITING_KO"),
        _const_block("  const REM_TITLE = {", "RUNNING: ['', '취약점 조치 진행 중', '진행 중'] };"),
        _function("qpos"), _function("convDoneOf"), _function("describe"), _function("userStop"),
        _function("isInternalReason"), _function("engineRefusal"), _function("abortText"), _function("remTitle"),
    ]
    return "\n".join(parts)


BASE = {"state": "finished", "flow": "full", "success": False, "exit_code": 2}
OLD = {"cls": "warn", "text": "빌드가 끝까지 가지 못했습니다. 남은 오류 3건", "short": "미완료"}


@needs_node
def test_describe_reads_a_converted_framework_run_as_done(tmp_path):
    cases = [
        {**BASE, "conversion_complete": True, "open": 3},
        {**BASE, "conversion_complete": True, "open": 2, "blocked": 1},
        {**BASE, "conversion_complete": True},
        {**BASE, "conversion_complete": True, "unresolved": 4},
        # not true: exactly as before
        {**BASE, "open": 3},
        {**BASE, "conversion_complete": False, "open": 3},
        {**BASE, "conversion_complete": "true", "open": 3},
        # the earlier cases keep their wording
        {**BASE, "conversion_complete": True, "user_stop": True},
        {**BASE, "conversion_complete": True, "exit_code": 3, "abort_reason": "Maven 변환 후 신청 부탁드립니다."},
        {**BASE, "conversion_complete": True, "exit_code": 3},
        {**BASE, "conversion_complete": True, "success": True, "exit_code": 0},
        {"state": "running", "flow": "full", "conversion_complete": True},
    ]
    js = _describe_js() + f"\nprocess.stdout.write(JSON.stringify({json.dumps(cases)}.map(describe)));\n"
    out = _node(js, tmp_path)
    assert out[0] == {"cls": "ok", "text": f"{TITLE}. 남은 오류 3건", "short": "완료"}
    assert out[1] == {"cls": "ok", "text": f"{TITLE}. 남은 오류 2건, 막힌 항목 1건", "short": "완료"}
    assert out[2] == {"cls": "ok", "text": TITLE, "short": "완료"}
    assert out[3] == {"cls": "ok", "text": f"{TITLE}. 남은 오류 4건", "short": "완료"}
    assert out[4] == OLD and out[5] == OLD and out[6] == OLD
    assert out[7] == {"cls": "warn", "text": "중단되었습니다", "short": "중단"}
    assert out[8] == {"cls": "warn", "text": "처리할 수 없습니다", "short": "처리 불가"}
    assert out[9]["short"] == "끊김" and out[9]["cls"] == "bad"
    assert out[10] == {"cls": "ok", "text": "빌드에 성공했습니다", "short": "완료"}
    assert out[11]["short"] == "진행 중"


@needs_node
def test_describe_done_only_for_exit_0_2_or_no_recorded_code(tmp_path):
    """A container that ended with another code after report.json was written (137: pod killed during the chained
    remediation) is not a done conversion; it reads exactly as it did before this increment. Exit 0 with a failed
    build is the Job-mode Worker's own code (unless SBMIG_FAIL_JOB_ON_BUILD_FAIL=1), so it counts like 2."""
    cases = [
        {**BASE, "conversion_complete": True, "open": 3, "exit_code": 0},
        {**BASE, "open": 3, "exit_code": 0},
        {**BASE, "conversion_complete": True, "open": 3, "exit_code": 2},
        {**BASE, "conversion_complete": True, "open": 3, "exit_code": None},
        {k: v for k, v in {**BASE, "conversion_complete": True, "open": 3}.items() if k != "exit_code"},
        {**BASE, "conversion_complete": True, "open": 3, "exit_code": 137},
        {**BASE, "open": 3, "exit_code": 137},
        {**BASE, "conversion_complete": True, "open": 3, "exit_code": 1},
        {**BASE, "conversion_complete": True, "open": 3, "exit_code": 3},
        {**BASE, "open": 3, "exit_code": 3},
    ]
    js = _describe_js() + (f"\nconst cs = {json.dumps(cases)};\n"
                           "process.stdout.write(JSON.stringify({ d: cs.map(describe), done: cs.map(convDoneOf) }));\n")
    out = _node(js, tmp_path)
    d0, done0 = out["d"][:2], out["done"][:2]
    # exit 0 with the key: done; exit 0 without it: what it read before this increment
    assert done0 == [True, False]
    assert d0[0] == {"cls": "ok", "text": f"{TITLE}. 남은 오류 3건", "short": "완료"}
    assert d0[1] == OLD
    d, done = out["d"][2:], out["done"][2:]
    assert done == [True, True, True, False, False, False, False, False]
    assert d[0]["short"] == d[1]["short"] == d[2]["short"] == "완료" and d[0]["cls"] == "ok"
    # exit 137 with the key reads byte for byte as exit 137 without it (the wording before this increment)
    assert d[3] == d[4] == OLD
    assert d[5] == OLD
    # exit 3 unchanged, with or without the key
    assert d[6] == d[7] == {"cls": "bad", "text": "실행이 중간에 끊겼습니다", "short": "끊김"}


# ------------------------------------------------------------------------------------------------- 2. the result card
@needs_node
def test_result_card_chips_are_the_same_with_and_without_the_key(tmp_path):
    show = _function("showResult")
    start = show.index("    const chips = [];")
    end = show.index("    $('counts').innerHTML = chips.join('');") + len("    $('counts').innerHTML = chips.join('');")
    js = ("const els = { counts: { innerHTML: '' } }; const $ = (id) => els[id]; let remLlm = null;\n"
          + _function("llmTotal") + "\nfunction chipsOf(st) {\n" + show[start:end] + "\n  return els.counts.innerHTML;\n}\n"
          "const base = { state: 'finished', success: false, exit_code: 2, open: 3, blocked: 1, resolved: 0, llm_calls: 0 };\n"
          "process.stdout.write(JSON.stringify([chipsOf(base), chipsOf({ ...base, conversion_complete: true })]));\n")
    without, with_key = _node(js, tmp_path)
    assert with_key == without
    assert "남은 오류 3건" in with_key and "막힌 항목 1건" in with_key and "LLM 호출 0회" in with_key


# ------------------------------------------------------------------------------------------------- 3. the rail
RAIL_HARNESS = r"""
const KO = { verify: '빌드 검증', transform: '레시피 적용' };
function mkStep(node, phase) {
  const set = new Set();
  return { dataset: phase ? { node, phase } : { node },
           classList: { add: (c) => set.add(c), remove: (c) => set.delete(c), contains: (c) => set.has(c),
                        toggle: (c, on) => { if (on) set.add(c); else set.delete(c); } },
           classes: () => [...set].sort() };
}
let order = [];
const steps = {};
function build(list) { order = list.map(([node, phase]) => mkStep(node, phase)); for (const k of Object.keys(steps)) delete steps[k]; for (const s of order) steps[s.dataset.phase || s.dataset.node] = s; }
build([['intake'], ['analyze'], ['plan'], ['phase', 'transform'], ['phase', 'verify'], ['finalize']]);
// a tiny selector engine for what the rail code asks: .step, .step[data-node|data-phase="x"] with :not(.c)...
function match(s, sel) {
  const m = /^\.step(?:\[data-(node|phase)="([^"]+)"\])?((?::not\(\.[\w-]+\))*)$/.exec(sel);
  if (!m) throw new Error('selector not supported: ' + sel);
  if (m[1] && s.dataset[m[1]] !== m[2]) return false;
  for (const n of (m[3].match(/:not\(\.([\w-]+)\)/g) || [])) if (s.classList.contains(n.slice(6, -1))) return false;
  return true;
}
const els = { railnote: { textContent: '' },
              rail: { querySelector: (sel) => order.find((s) => match(s, sel)) || null,
                      querySelectorAll: (sel) => order.filter((s) => match(s, sel)),
                      set innerHTML(v) { order = []; }, insertAdjacentHTML() {} } };
const $ = (id) => els[id];
const CSS = { escape: (s) => s };
let fwReduced = false, abortNote = '', railFlow = 'full', queueText = '', current = null;
const FLOW_FULL = 'full', FLOW_REM = 'remediation';
function placeSkipped() {}
const FIXED = ['intake', 'analyze', 'plan'], REM_STEPS = ['intake', 'remediation', 'finalize'];
function stepHtml() { return ''; }
function loadRemediation() {}
__CONSTS__
__FUNCS__
function journal(ev) {
  const name = ev.event;
__JOURNAL__
}
function setCurrent(el) { for (const n of railSteps()) n.classList.remove('current'); if (el) el.classList.add('current'); }
// the finished branch of setStatus, as written in the page
function finished(st) { const d = {}; function showResult() {}
__FINISHED__
}
function reset() { for (const s of order) for (const c of s.classes()) s.classList.remove(c); els.railnote.textContent = ''; convFailed = false; verifyFailed = false; convDone = null; }
function snap() { return { verify: steps.verify.classes(), note: els.railnote.textContent }; }
const out = [];
"""


def _rail_js(body: str) -> str:
    j = _function("journalRow")
    start = j.index("    // 변환 단계의 결과(step이 있는 rewrite)만 본다")
    end = j.index("    const tl = $('timeline');")
    status = _function("setStatus")
    fin = [ln for ln in status.splitlines() if ln.strip().startswith("if (st.state === 'finished') {")]
    assert len(fin) == 1
    consts = "\n".join([
        _line("const CONV_DONE_TITLE"), _line("const CONV_DONE_NOTE"),
        _line("let convFailed"), _line("let verifyFailed"), _line("let convDone"), _line("const VERIFY_FAIL_NOTE"),
    ])
    funcs = "\n".join(_function(n) for n in ("convDoneOf", "markVerify", "railSteps", "engineSeen", "isStopReason",
                                              "isInternalReason", "onNode", "resetRail"))
    harness = (RAIL_HARNESS.replace("__CONSTS__", consts).replace("__FUNCS__", funcs)
               .replace("__JOURNAL__", j[start:end]).replace("__FINISHED__", fin[0]))
    return harness + body + "\nprocess.stdout.write(JSON.stringify(out));\n"


VERIFY_FAIL = {"event": "phase_end", "phase": "verify", "ok": False}
STEP_OK = {"event": "rewrite", "step": "transform.rewrite", "ok": True, "dry_run": False}
STEP_BAD = {"event": "rewrite", "step": "transform.rewrite", "ok": False, "dry_run": False}
ATTEMPT_BAD = {"event": "rewrite", "recipes": ["x"], "ok": False, "dry_run": False}   # a single try the runner retries
DONE = {"state": "finished", "success": False, "exit_code": 2, "conversion_complete": True}


@needs_node
def test_live_rail_of_a_reduced_run(tmp_path):
    body = f"""
    // 1. reduced run, the build fails: the new mark and note
    reset(); fwReduced = true; journal({json.dumps(ATTEMPT_BAD)}); journal({json.dumps(STEP_OK)}); journal({json.dumps(VERIFY_FAIL)}); out.push(snap());
    // 2. reduced run whose conversion failed: the red cross
    reset(); fwReduced = true; journal({json.dumps(STEP_BAD)}); journal({json.dumps(VERIFY_FAIL)}); out.push(snap());
    // 3. rewrite_failed after the build: corrected to the red cross
    reset(); fwReduced = true; journal({json.dumps(VERIFY_FAIL)}); journal({{event: 'rewrite_failed', step: 'transform.rewrite'}}); out.push(snap());
    // 4. a full run: the red cross as today
    reset(); fwReduced = false; journal({json.dumps(VERIFY_FAIL)}); out.push(snap());
    // 5. the scope becomes known after the phase_end (status framework / plan later)
    reset(); fwReduced = false; journal({json.dumps(VERIFY_FAIL)}); fwReduced = true; markVerify(); out.push(snap());
    // 6. a build that passed is not touched
    reset(); fwReduced = true; journal({{event: 'phase_end', phase: 'verify', ok: true}}); out.push(snap());
    // 7. a dry-run step result is not a failed conversion
    reset(); fwReduced = true; journal({{event: 'rewrite', step: 'x', ok: false, dry_run: true}}); journal({json.dumps(VERIFY_FAIL)}); out.push(snap());
    """
    out = _node(_rail_js(body), tmp_path)
    assert out[0] == {"verify": ["buildwarn"], "note": NOTE}
    assert out[1] == {"verify": ["failed"], "note": OLD_NOTE}
    assert out[2] == {"verify": ["failed"], "note": OLD_NOTE}
    assert out[3] == {"verify": ["failed"], "note": OLD_NOTE}
    assert out[4] == {"verify": ["buildwarn"], "note": NOTE}
    assert out[5] == {"verify": [], "note": "빌드 검증 단계 끝"}
    assert out[6] == {"verify": ["buildwarn"], "note": NOTE}


@needs_node
def test_final_status_corrects_the_mark_either_way(tmp_path):
    body = f"""
    const DONE = {json.dumps(DONE)};
    // live guess was the new mark, the final status says not converted: red cross and the old note
    reset(); fwReduced = true; journal({json.dumps(VERIFY_FAIL)}); finished({{state: 'finished', success: false, exit_code: 2}}); out.push(snap());
    // live guess was the red cross (scope never seen), the final status says converted
    reset(); fwReduced = false; journal({json.dumps(VERIFY_FAIL)}); finished(DONE); out.push(snap());
    // a finished run opened from the list: the status comes first, the journal is replayed after it
    reset(); finished(DONE); fwReduced = true; journal({json.dumps(ATTEMPT_BAD)}); journal({json.dumps(VERIFY_FAIL)}); out.push(snap());
    reset(); finished({{state: 'finished', success: false, exit_code: 2}}); fwReduced = true; journal({json.dumps(VERIFY_FAIL)}); out.push(snap());
    // a more important note is never overwritten
    reset(); fwReduced = false; journal({json.dumps(VERIFY_FAIL)}); els.railnote.textContent = '중단 요청으로 멈췄습니다. 마지막 빌드는 하지 않았습니다.';
    finished(DONE); out.push(snap());
    reset(); fwReduced = true; journal({json.dumps(VERIFY_FAIL)}); els.railnote.textContent = '취약점 조치 중';
    finished({{state: 'finished', success: false, exit_code: 2}}); out.push(snap());
    // a stopped or refused step keeps its own mark
    reset(); fwReduced = true; steps.verify.classList.add('stopped'); journal({json.dumps(VERIFY_FAIL)}); out.push(snap());
    // exit 137 with the key (pod killed after the report): the red cross, also when the live guess was the new mark
    reset(); fwReduced = true; journal({json.dumps(VERIFY_FAIL)}); finished({{...DONE, exit_code: 137}}); out.push(snap());
    reset(); finished({{...DONE, exit_code: 137}}); fwReduced = true; journal({json.dumps(VERIFY_FAIL)}); out.push(snap());
    // no recorded exit code (older folder) with the key: the new mark
    reset(); fwReduced = false; journal({json.dumps(VERIFY_FAIL)}); finished({{...DONE, exit_code: null}}); out.push(snap());
    // exit 0 (the Job-mode Worker's code for a failed build) with the key: the new mark; without it: the red cross
    reset(); fwReduced = false; journal({json.dumps(VERIFY_FAIL)}); finished({{...DONE, exit_code: 0}}); out.push(snap());
    reset(); fwReduced = true; journal({json.dumps(VERIFY_FAIL)}); finished({{state: 'finished', success: false, exit_code: 0}}); out.push(snap());
    // exit 1 with the key: the red cross
    reset(); fwReduced = true; journal({json.dumps(VERIFY_FAIL)}); finished({{...DONE, exit_code: 1}}); out.push(snap());
    """
    out = _node(_rail_js(body), tmp_path)
    assert out[0] == {"verify": ["failed"], "note": OLD_NOTE}
    assert out[1] == {"verify": ["buildwarn"], "note": NOTE}
    assert out[2] == {"verify": ["buildwarn"], "note": NOTE}
    assert out[3] == {"verify": ["failed"], "note": OLD_NOTE}
    assert out[4] == {"verify": ["buildwarn"], "note": "중단 요청으로 멈췄습니다. 마지막 빌드는 하지 않았습니다."}
    assert out[5] == {"verify": ["failed"], "note": "취약점 조치 중"}
    assert out[6]["verify"] == ["failed", "stopped"]
    assert out[7] == {"verify": ["failed"], "note": OLD_NOTE}
    assert out[8] == {"verify": ["failed"], "note": OLD_NOTE}
    assert out[9] == {"verify": ["buildwarn"], "note": NOTE}
    assert out[10] == {"verify": ["buildwarn"], "note": NOTE}
    assert out[11] == {"verify": ["failed"], "note": OLD_NOTE}
    assert out[12] == {"verify": ["failed"], "note": OLD_NOTE}


@needs_node
def test_reset_rail_forgets_the_previous_run(tmp_path):
    # opening another run calls resetRail (attach); nothing of the earlier run's verdict survives it
    body = f"""
    reset(); fwReduced = true; journal({json.dumps(STEP_BAD)}); journal({json.dumps(VERIFY_FAIL)}); finished({{state: 'finished', success: false, exit_code: 2}});
    resetRail('full'); out.push([convFailed, verifyFailed, convDone]);
    build([['intake'], ['analyze'], ['plan'], ['phase', 'transform'], ['phase', 'verify'], ['finalize']]);
    fwReduced = true; journal({json.dumps(VERIFY_FAIL)}); out.push(snap());
    resetRail('full'); build([['intake'], ['analyze'], ['plan'], ['phase', 'transform'], ['phase', 'verify'], ['finalize']]);
    finished({json.dumps(DONE)}); resetRail('full'); out.push([convFailed, verifyFailed, convDone]);
    """
    out = _node(_rail_js(body), tmp_path)
    assert out[0] == [False, False, None]
    assert out[1] == {"verify": ["buildwarn"], "note": NOTE}
    assert out[2] == [False, False, None]


@needs_node
def test_a_buildwarn_step_counts_as_finished(tmp_path):
    body = f"""
    // engineSeen: a rail whose only finished step is the build step with the new mark
    reset(); steps.verify.classList.add('buildwarn'); out.push(engineSeen());
    reset(); out.push(engineSeen());
    // the chained remediation step becomes current after finalize when the build step carries the new mark
    build([['intake'], ['analyze'], ['plan'], ['phase', 'transform'], ['phase', 'verify'], ['finalize'], ['remediation']]);
    for (const n of ['intake', 'analyze', 'plan', 'transform']) steps[n].classList.add('done');
    fwReduced = true; journal({json.dumps(VERIFY_FAIL)});
    onNode({{node: 'finalize', update: {{}}}});
    out.push({{ rem: steps.remediation.classes(), verify: steps.verify.classes(), fin: steps.finalize.classes() }});
    // a later engine node does not pick the build step as the next one
    build([['intake'], ['analyze'], ['plan'], ['phase', 'transform'], ['phase', 'verify'], ['finalize']]);
    reset(); for (const n of ['intake', 'analyze', 'plan', 'transform']) steps[n].classList.add('done');
    fwReduced = true; journal({json.dumps(VERIFY_FAIL)}); onNode({{node: 'plan', update: {{}}}});
    out.push(order.filter((s) => s.classList.contains('current')).map((s) => s.dataset.phase || s.dataset.node));
    """
    out = _node(_rail_js(body), tmp_path)
    assert out[0] is True and out[1] is False
    assert out[2] == {"rem": ["current"], "verify": ["buildwarn"], "fin": ["done"]}
    assert out[3] == ["finalize"]


def test_rail_mark_style():
    rule = CSS.split(".rail .step.buildwarn .dot {", 1)[1].split("}", 1)[0]
    assert "var(--warn)" in rule and "--bad" not in rule
    assert ".rail .step.buildwarn .label { color: var(--warn); font-weight: 600; }" in CSS
    # its own mark: a bar and a point, resetting what the check mark of `done` sets (a step can carry both)
    assert ".rail .step.buildwarn .dot::before, .rail .step.buildwarn .dot::after {" in CSS
    assert "border: 0; transform: none;" in CSS
    # drawn after `done` / `failed` so it wins, before `stopped` / `refused` so those win over it
    assert CSS.index(".rail .step.failed .dot::before") < CSS.index(".rail .step.buildwarn .dot {") < CSS.index(".rail .step.stopped .dot {")
