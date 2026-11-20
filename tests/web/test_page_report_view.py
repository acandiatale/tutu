"""The rendered report view (INTERFACES "v2.1z-bg" point 1) and the names a person receives (point 2) on the page.

The Markdown renderer `MdView` of `index.html` is cut out of the page and executed by node against a small fake DOM,
on real reports (the Korean migration guide, the English diagnostic layout, the remediation report) and on hostile
text. Every attribute the renderer sets is recorded, so a test can say which attributes exist at all. Static checks
cover the wiring (dialog, toggle, download names)."""
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
STYLE = PAGE.split("<style>", 1)[1].split("</style>", 1)[0]
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
RESOLVER = "/api/runs/R/files/"


def _component() -> str:
    start = SCRIPT.index("const MdView = (() => {")
    end = SCRIPT.index("  })();", start) + len("  })();")
    return SCRIPT[start:end]


HARNESS = r"""
const fs = require('fs');
const ATTRS = new Set();
class Node_ { constructor() { this.children = []; } appendChild(c) { this.children.push(c); return c; } }
class Text_ { constructor(v) { this.v = v; } }
class El_ extends Node_ {
  constructor(tag) { super(); this.tag = tag; this.attrs = {}; }
  set className(v) { ATTRS.add('class'); this.attrs['class'] = String(v); }
  get className() { return this.attrs['class'] || ''; }
  setAttribute(k, v) { ATTRS.add(k); this.attrs[k] = String(v); }
}
const doc = { createElement: (t) => new El_(t), createTextNode: (v) => new Text_(v), createDocumentFragment: () => new Node_() };
const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
function ser(n) {
  if (n instanceof Text_) return esc(n.v);
  const inner = n.children.map(ser).join('');
  if (!(n instanceof El_)) return inner;
  const a = Object.entries(n.attrs).map(([k, v]) => ` ${k}="${esc(v)}"`).join('');
  return `<${n.tag}${a}>${inner}</${n.tag}>`;
}
__COMPONENT__
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = {};
const files = ['report.md', 'report.json', 'changes.patch', 'errors.md', 'logs/build.log'];
const resolve = (p) => (files.includes(p) ? '__RESOLVER__' + encodeURIComponent(p) : null);
for (const [k, text] of Object.entries(cases)) out[k] = ser(MdView.render(MdView.parse(text), doc, { resolve }));
out.__attrs = [...ATTRS].sort();
process.stdout.write(JSON.stringify(out));
"""


def render(cases: dict[str, str], tmp_path: Path) -> dict:
    src = tmp_path / "cases.json"
    src.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    js = tmp_path / "run.js"
    js.write_text(HARNESS.replace("__COMPONENT__", _component()).replace("__RESOLVER__", RESOLVER), encoding="utf-8")
    p = subprocess.run([NODE, str(js), str(src)], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def outside_code(html: str) -> str:
    return re.sub(r"<pre\b.*?</pre>|<code\b.*?</code>", "", html, flags=re.S)


ALLOWED_ATTRS = {"class", "href", "target", "rel", "start", "type", "disabled", "checked", "data-lang"}


# ------------------------------------------------------------------------------------------------ real reports
def _real_reports(tmp_path: Path) -> dict[str, str]:
    from sbmig.remediations import report as rem_report
    from sbmig.report import render_markdown
    from tests.remediations.test_report import _company
    from tests.report.test_guide_ko import failed_report, guide_report

    state, paths = _company(tmp_path / "rem")
    return {"ko_success": render_markdown(guide_report(), "ko"), "ko_failed": render_markdown(failed_report(), "ko"),
            "en_success": render_markdown(guide_report(), "en"), "en_failed": render_markdown(failed_report(), "en"),
            "remediation": rem_report.build_report_md(state, paths)}


@needs_node
def test_real_reports_render_every_construct(tmp_path):
    md = _real_reports(tmp_path)
    out = render(md, tmp_path)
    assert set(out["__attrs"]) <= ALLOWED_ATTRS
    for name, text in md.items():
        html = out[name]
        lines = text.splitlines()
        assert html.count("<h2>") == sum(1 for ln in lines if ln.startswith("## ")), name
        assert html.count("<table>") == sum(1 for ln in lines if re.match(r"^\|(-+\|)+$", ln)), name
        assert html.count("<details>") == sum(1 for ln in lines if ln.startswith("<details>")), name
        # a diff block carries a second class so that its lines can fill the box with a background colour
        assert len(re.findall(r'<pre class="mdv-pre(?: mdv-diff)?">', html)) \
            == sum(1 for ln in lines if re.match(r"^`{3,}\S", ln)), name
        assert html.count('<pre class="mdv-pre mdv-diff">') == sum(1 for ln in lines if re.match(r"^`{3,}diff\b", ln)), name
        assert html.count('<div class="mdv-tw">') == html.count("<table>")          # every table scrolls in its box
        rest = outside_code(html)
        for marker in ("**", "|---", "&lt;details", "&lt;summary", "```"):
            assert marker not in rest, (name, marker)
    ko = out["ko_failed"]
    assert "<td>cannot find symbol | Document</td>" in ko                            # an escaped pipe inside a cell
    assert "<summary><code>blk-1</code> 관련 오류 1건</summary>" in ko                 # <code> in a summary
    assert '<span class="da">+import java.util.Base64;</span>' in ko                 # diff lines coloured
    assert "<ol><li><strong>Netflix Hystrix는 지원이 종료되었습니다.</strong>" in ko and "<ul><li>대체할 라이브러리: <code>" in ko
    assert '<a href="https://docs.example.com/r4j" target="_blank" rel="noopener noreferrer">1</a>' in ko
    assert "<em>비용은 입력 토큰" in ko
    assert "<th>파일</th><th>줄</th><th>오류</th>" in ko
    rem = out["remediation"]
    assert "<blockquote>" in rem and "🔴" in rem


@needs_node
def test_hostile_text_never_becomes_markup_or_a_dangerous_link(tmp_path):
    cases = {
        "script": "# t <script>alert(1)</script>\n\n<script>alert(2)</script>\n\n<img src=x onerror=alert(3)>",
        "html_in_table": "| a | b |\n|---|---|\n| <img src=x onerror=alert(1)> | <b>x</b> |",
        "js_link": "[click](javascript:alert(1)) [c2](JaVaScRiPt:alert(1)) [c3](java\tscript:alert(1)) "
                   "[c4](data:text/html,<script>alert(1)</script>) [c5](vbscript:x) [c6](//evil.example/x) "
                   "[c7](/etc/passwd) [c8](../report.md) [c9](%2e%2e/report.md) [c10](file:///etc/passwd)",
        "ok_links": "[web](https://example.com/a?b=1) [run](report.md) [run2](./changes.patch) [nope](other.md) "
                    "see https://example.com/x. and <https://example.com/y>",
        "pipe_in_code": "| code | v |\n|---|---|\n| `a \\| b` | c\\|d |",
        "unclosed_fence": "before\n\n```diff\n+<script>x</script>\n- line\n\n## not a heading",
        "unclosed_details": "<details><summary>s <script>x</script></summary>\n\nbody **b**",
        "summary_html": "<details><summary><img src=x onerror=alert(1)><code>ok</code></summary>\n\nx\n\n</details>",
        "image": "![alt <b>](https://example.com/i.png)",
        "attr_break": '[x" onclick="alert(1)](https://example.com/"onmouseover="alert(1))',
        "emph_paths": "snake_case_name and `fix_vN.py` and *a* **b** _c_ ***d***",
        "task": "- [ ] open\n- [x] done\n\n1. one\n2. two\n   - nested\n\n> quote `c`\n\n---",
    }
    out = render(cases, tmp_path)
    assert set(out["__attrs"]) <= ALLOWED_ATTRS                     # no onerror / onclick / src / style can exist
    joined = "".join(v for k, v in out.items() if k != "__attrs")
    assert "<script" not in joined and "<img" not in joined and "<b>" not in joined
    for href in re.findall(r'href="([^"]*)"', joined):
        assert href.startswith(("https://", "http://", RESOLVER)), href
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in out["script"]
    assert "&lt;img src=x onerror=alert(3)&gt;" in out["script"]
    assert "<td>&lt;img src=x onerror=alert(1)&gt;</td>" in out["html_in_table"]
    js = out["js_link"]
    assert "<a " not in js and "click" in js and "c10" in js        # the label stays as text
    ok = out["ok_links"]
    assert '<a href="https://example.com/a?b=1"' in ok
    assert f'<a href="{RESOLVER}report.md"' in ok and f'<a href="{RESOLVER}changes.patch"' in ok
    assert "nope" in ok and "other.md" not in re.findall(r'href="([^"]*)"', ok)
    assert '<a href="https://example.com/x" target="_blank" rel="noopener noreferrer">https://example.com/x</a>.' in ok
    assert '<a href="https://example.com/y"' in ok
    assert "<td><code>a | b</code></td><td>c|d</td>" in out["pipe_in_code"]
    fence = out["unclosed_fence"]
    assert '<span class="da">+&lt;script&gt;x&lt;/script&gt;</span>' in fence and "<h2>" not in fence
    assert "<details><summary>s &lt;script&gt;x&lt;/script&gt;</summary><p>body <strong>b</strong></p></details>" \
        == out["unclosed_details"]
    assert "<summary>&lt;img src=x onerror=alert(1)&gt;<code>ok</code></summary>" in out["summary_html"]
    assert out["image"] == "<p>alt &lt;b&gt;</p>"                  # an image is never loaded: its alt text only
    for tag in re.findall(r"<a [^>]*>", out["attr_break"]):
        assert re.fullmatch(r'<a href="[^"]*" target="_blank" rel="noopener noreferrer">', tag), tag
    em = out["emph_paths"]
    assert "snake_case_name" in em and "<code>fix_vN.py</code>" in em
    assert "<em>a</em> <strong>b</strong> <em>c</em> <strong><em>d</em></strong>" in em
    task = out["task"]
    assert '<li class="task"><input type="checkbox" disabled=""></input> open</li>' in task
    assert '<input type="checkbox" disabled="" checked=""></input>' in task
    assert "<ol><li>one</li><li>two<ul><li>nested</li></ul></li></ol>" in task
    assert "<blockquote><p>quote <code>c</code></p></blockquote><hr></hr>" in task


@needs_node
def test_long_report_renders_quickly_and_fully(tmp_path):
    rows = "\n".join(f"| `com.example:lib-{i}` | {i} | " + "x" * 300 + " |" for i in range(2000))
    text = "# big\n\n| a | b | c |\n|---|---|---|\n" + rows + "\n\n```text\n" + "y" * 5000 + "\n```\n"
    out = render({"big": text}, tmp_path)
    assert out["big"].count("<tr>") == 2001 and "y" * 5000 in out["big"]


# ------------------------------------------------------------------------------------------------ wiring
def test_renderer_builds_nodes_only():
    comp = _component()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                   "setAttribute('on", "setAttribute('style", "setAttribute('src"):
        assert banned not in comp, banned
    assert "createTextNode" in comp and "document" not in comp.replace("doc.", "")    # the DOM comes in as `doc`


def test_dialog_and_toggle_in_the_page():
    dlg = PAGE[PAGE.index('<dialog class="cdmodal rvmodal" id="rv-dialog"'):]
    dlg = dlg[:dlg.index("</dialog>")]
    for marker in ('aria-labelledby="rv-title"', 'id="rv-view"', 'id="rv-raw"', 'id="rv-dl"', 'id="rv-close"',
                   'data-mode="view"', 'data-mode="raw"'):
        assert marker in dlg, marker
    prev = PAGE[PAGE.index('<details class="preview" id="preview" hidden>'):]
    prev = prev[:prev.index("</details>")]
    assert 'id="report-view"' in prev and '<pre class="report" id="report" hidden></pre>' in prev
    assert 'id="report-big"' in prev and "크게 보기" in prev and ">원문<" in prev and ">보기<" in prev
    assert ".mdv .mdv-tw { margin: 10px 0; overflow-x: auto;" in STYLE
    assert ".mdv .mdv-pre { margin: 10px 0; padding: 10px 12px; overflow-x: auto;" in STYLE
    assert "src.addEventListener('report', (e) => { if (mine()) setReportText(JSON.parse(e.data)); });" in SCRIPT
    # the raw text goes in as text only
    assert "$('report').textContent = text;" in SCRIPT and "$('rem-report').textContent = text;" in SCRIPT
    assert "$('rv-raw').textContent = src.text;" in SCRIPT


def test_download_names_on_the_page():
    assert "const MIG_DL = { 'report.md': 'migration-report.md', 'report.json': 'migration-report.json' };" in SCRIPT
    assert "const REM_DL = { 'report.md': 'remediation-report.md', 'report.json': 'remediation-report.json' };" in SCRIPT
    assert '<span>보고서 받기 <span class="sub">migration-report.md</span></span>' in PAGE
    assert "a.setAttribute('download', MIG_DL[name] || name);" in SCRIPT
    assert "a.href = remUrl(runId, '/files/' + name + (REM_DL[name] ? '?download=1' : '')); a.setAttribute('download', shown);" \
        in SCRIPT
    # the remediation report is read in its own preview; its 「크게 보기」 opens the dialog (no separate 보고서 보기 button)
    assert "$('rem-report-big').addEventListener('click', openRemReport);" in SCRIPT
    assert "href: remUrl(runId, '/files/report.md?download=1'), dl: REM_DL['report.md']" in SCRIPT
