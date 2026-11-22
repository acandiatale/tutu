"""The inline part of the report view (`MdView`) takes time linear in the length of a line (INTERFACES "v2.1z-br",
visible effect 4) and draws the same nodes as before.

The renderer is cut out of `index.html` the way `test_page_report_view.py` does and run by node. The expected nodes
below were produced by the renderer of c1ebe82 (before this change) and are pinned: the rewrite was compared with that
renderer node for node over every report sample of the repository and generated edge cases while it was written.
The timing check runs one crafted line of about 195 KB made of the constructs that used to take quadratic time."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import sbmig.web

PAGE = (Path(sbmig.web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _component() -> str:
    start = SCRIPT.index("const MdView = (() => {")
    end = SCRIPT.index("  })();", start) + len("  })();")
    return SCRIPT[start:end]


def _run(js: str, tmp_path: Path, data: object, timeout: int = 120) -> object:
    src = tmp_path / "data.json"
    src.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    script = tmp_path / "run.js"
    script.write_text(_component() + "\nconst data = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));\n" + js,
                      encoding="utf-8")
    p = subprocess.run([NODE, str(script), str(src)], capture_output=True, text=True, timeout=timeout, encoding="utf-8")
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


# name: (text, opt, inside a link label, nodes drawn by the renderer of c1ebe82)
PINNED = {
    'emphasis': ("*a* **b** ***c*** _d_ __e__ ___f___", {}, False,
        [{"t": "em", "c": [{"t": "text", "v": "a"}]}, {"t": "text", "v": " "}, {"t": "strong", "c": [{"t": "text", "v": "b"}]}, {"t": "text", "v": " "}, {"t": "strong", "c": [{"t": "em", "c": [{"t": "text", "v": "c"}]}]}, {"t": "text", "v": " "}, {"t": "em", "c": [{"t": "text", "v": "d"}]}, {"t": "text", "v": " "}, {"t": "strong", "c": [{"t": "text", "v": "e"}]}, {"t": "text", "v": " "}, {"t": "strong", "c": [{"t": "em", "c": [{"t": "text", "v": "f"}]}]}]),
    'nested_emphasis': ("**a *b* c** and *a **b** c* and *a _b_ c*", {}, False,
        [{"t": "strong", "c": [{"t": "text", "v": "a "}, {"t": "em", "c": [{"t": "text", "v": "b"}]}, {"t": "text", "v": " c"}]}, {"t": "text", "v": " and "}, {"t": "em", "c": [{"t": "text", "v": "a "}, {"t": "strong", "c": [{"t": "text", "v": "b"}]}, {"t": "text", "v": " c"}]}, {"t": "text", "v": " and "}, {"t": "em", "c": [{"t": "text", "v": "a "}, {"t": "em", "c": [{"t": "text", "v": "b"}]}, {"t": "text", "v": " c"}]}]),
    'unbalanced_emphasis': ("*a *b c* and **x *y and _z", {}, False,
        [{"t": "em", "c": [{"t": "text", "v": "a *b c"}]}, {"t": "text", "v": " and **x *y and _z"}]),
    'intraword_underscore': ("snake_case_name a_b_ _a_b *a*b*c", {}, False,
        [{"t": "text", "v": "snake_case_name a_b_ _a_b "}, {"t": "em", "c": [{"t": "text", "v": "a"}]}, {"t": "text", "v": "b*c"}]),
    'escaped_emphasis': ("*a\\*b* \\*c* \\\\*d*", {}, False,
        [{"t": "em", "c": [{"t": "text", "v": "a*b"}]}, {"t": "text", "v": " *c* \\"}, {"t": "em", "c": [{"t": "text", "v": "d"}]}]),
    'emphasis_closer_at_end_of_inner': ("*a _b_*", {}, False,
        [{"t": "em", "c": [{"t": "text", "v": "a "}, {"t": "em", "c": [{"t": "text", "v": "b"}]}]}]),
    'code_spans': ("`code` ``co`de`` ` spaced ` `a\nb` `unclosed ``x`", {}, False,
        [{"t": "code", "v": "code"}, {"t": "text", "v": " "}, {"t": "code", "v": "co`de"}, {"t": "text", "v": " "}, {"t": "code", "v": "spaced"}, {"t": "text", "v": " "}, {"t": "code", "v": "a b"}, {"t": "text", "v": " "}, {"t": "code", "v": "unclosed ``x"}]),
    'code_hides_emphasis': ("*a`*`b* `*a`*", {}, False,
        [{"t": "em", "c": [{"t": "text", "v": "a"}, {"t": "code", "v": "*"}, {"t": "text", "v": "b"}]}, {"t": "text", "v": " "}, {"t": "code", "v": "*a"}, {"t": "text", "v": "*"}]),
    'links': ("[a](http://x.example) [b](<http://y.example/a b>) [c](u \"t\") [d](a(b)c) [e](a\\)b) [f]( u )", {}, False,
        [{"t": "link", "href": "http://x.example", "c": [{"t": "text", "v": "a"}]}, {"t": "text", "v": " "}, {"t": "link", "href": "http://y.example/a b", "c": [{"t": "text", "v": "b"}]}, {"t": "text", "v": " "}, {"t": "link", "href": "u", "c": [{"t": "text", "v": "c"}]}, {"t": "text", "v": " "}, {"t": "link", "href": "a(b)c", "c": [{"t": "text", "v": "d"}]}, {"t": "text", "v": " "}, {"t": "link", "href": "a)b", "c": [{"t": "text", "v": "e"}]}, {"t": "text", "v": " "}, {"t": "link", "href": "u", "c": [{"t": "text", "v": "f"}]}]),
    'unbalanced_links': ("[unclosed [nested [a](u)](v) [a]b [a] (u) [a](", {}, False,
        [{"t": "text", "v": "[unclosed "}, {"t": "link", "href": "v", "c": [{"t": "text", "v": "nested [a](u)"}]}, {"t": "text", "v": " [a]b [a] (u) [a]("}]),
    'link_label_with_code_and_bracket': ("[`]`](u) [a\\]b](u) [*a](u)* *[a*](u)", {}, False,
        [{"t": "link", "href": "u", "c": [{"t": "code", "v": "]"}]}, {"t": "text", "v": " "}, {"t": "link", "href": "u", "c": [{"t": "text", "v": "a]b"}]}, {"t": "text", "v": " "}, {"t": "link", "href": "u", "c": [{"t": "text", "v": "*a"}]}, {"t": "text", "v": "* "}, {"t": "em", "c": [{"t": "text", "v": "[a"}]}, {"t": "text", "v": "](u)"}]),
    'images': ("![img](u) ![a ![b](c)](d) [x ![y](z)](w)", {}, False,
        [{"t": "text", "v": "img"}, {"t": "text", "v": " "}, {"t": "text", "v": "a "}, {"t": "text", "v": "b"}, {"t": "text", "v": " "}, {"t": "link", "href": "w", "c": [{"t": "text", "v": "x "}, {"t": "text", "v": "y"}]}]),
    'autolinks': ("see http://a.example/x. and (http://b.example/y) http://c.example/a_(b) http://d.example/** http:///x xhttp://no", {}, False,
        [{"t": "text", "v": "see "}, {"t": "link", "href": "http://a.example/x", "c": [{"t": "text", "v": "http://a.example/x"}]}, {"t": "text", "v": ". and ("}, {"t": "link", "href": "http://b.example/y", "c": [{"t": "text", "v": "http://b.example/y"}]}, {"t": "text", "v": ") "}, {"t": "link", "href": "http://c.example/a_(b)", "c": [{"t": "text", "v": "http://c.example/a_(b)"}]}, {"t": "text", "v": " "}, {"t": "link", "href": "http://d.example/", "c": [{"t": "text", "v": "http://d.example/"}]}, {"t": "text", "v": "** http:///x xhttp://no"}]),
    'autolink_trailing_parens': ("http://a.example/b))) and http://a.example/c.)", {}, False,
        [{"t": "link", "href": "http://a.example/b", "c": [{"t": "text", "v": "http://a.example/b"}]}, {"t": "text", "v": "))) and "}, {"t": "link", "href": "http://a.example/c.", "c": [{"t": "text", "v": "http://a.example/c."}]}, {"t": "text", "v": ")"}]),
    'angle_autolinks': ("<http://a.example/x> <https://b.example> <ftp://no> <http://bad space>", {}, False,
        [{"t": "link", "href": "http://a.example/x", "c": [{"t": "text", "v": "http://a.example/x"}]}, {"t": "text", "v": " "}, {"t": "link", "href": "https://b.example", "c": [{"t": "text", "v": "https://b.example"}]}, {"t": "text", "v": " <ftp://no> <"}, {"t": "link", "href": "http://bad", "c": [{"t": "text", "v": "http://bad"}]}, {"t": "text", "v": " space>"}]),
    'hard_breaks': ("one  \ntwo\\\nthree   \nfour \nfive", {}, False,
        [{"t": "text", "v": "one"}, {"t": "br"}, {"t": "text", "v": "two"}, {"t": "br"}, {"t": "text", "v": "three"}, {"t": "br"}, {"t": "text", "v": "four\nfive"}]),
    'escapes': ("\\# \\[x\\] \\` \\\\ \\a \\한 \\", {}, False,
        [{"t": "text", "v": "# [x] ` \\ \\a \\한 \\"}]),
    'summary_code': ("<code>blk-1</code> and <code>open", {"summary": True}, False,
        [{"t": "code", "v": "blk-1"}, {"t": "text", "v": " and <code>open"}]),
    'inside_link_label': ("[a](u) <http://x.example> http://y.example *b*", {}, True,
        [{"t": "text", "v": "[a](u) <http://x.example> http://y.example "}, {"t": "em", "c": [{"t": "text", "v": "b"}]}]),
    'korean_mixed': ("한글 *강조* 와 **굵게** `코드` [링크](https://example.com) 끝_밑줄_표시 _한_ 글*자* 혼합 *한글a* a_한_", {}, False,
        [{"t": "text", "v": "한글 "}, {"t": "em", "c": [{"t": "text", "v": "강조"}]}, {"t": "text", "v": " 와 "}, {"t": "strong", "c": [{"t": "text", "v": "굵게"}]}, {"t": "text", "v": " "}, {"t": "code", "v": "코드"}, {"t": "text", "v": " "}, {"t": "link", "href": "https://example.com", "c": [{"t": "text", "v": "링크"}]}, {"t": "text", "v": " 끝_밑줄_표시 "}, {"t": "em", "c": [{"t": "text", "v": "한"}]}, {"t": "text", "v": " 글"}, {"t": "em", "c": [{"t": "text", "v": "자"}]}, {"t": "text", "v": " 혼합 "}, {"t": "em", "c": [{"t": "text", "v": "한글a"}]}, {"t": "text", "v": " a_한_"}]),
}


@needs_node
def test_inline_nodes_are_the_pinned_ones(tmp_path):
    data = {k: [v[0], v[1], v[2]] for k, v in PINNED.items()}
    out = _run("const out = {};\n"
               "for (const [k, [s, opt, inLink]] of Object.entries(data)) out[k] = MdView.inline(s, opt, inLink);\n"
               "process.stdout.write(JSON.stringify(out));", tmp_path, data)
    for name, (_, _, _, expected) in PINNED.items():
        assert out[name] == expected, name


@needs_node
def test_a_crafted_195_kb_line_renders_in_well_under_ten_seconds(tmp_path):
    # every construct that used to rescan the rest of the line: unclosed * _ ** [ ![, unpaired links, an autolink
    # with many closing parentheses, an address cut by a third slash, a long run of spaces before a line break
    k = 21_600
    head = ["*a " * (k // 3), " _a" * (k // 3), "**a " * (k // 4), "[a " * (k // 3), "![a " * (k // 4),
            "[](( " * (k // 5), "http:///x " * (k // 10)]
    line = "".join(head) + "http://a" + ")" * k + "z" + " " * k + "\nend"
    assert 190_000 <= len(line) <= 200_000
    out = _run("const t0 = process.hrtime.bigint();\n"
               "const nodes = MdView.inline(data.line, {}, false);\n"
               "const ms = Number(process.hrtime.bigint() - t0) / 1e6;\n"
               "MdView.render(MdView.parse(data.line), { createElement: () => ({ appendChild() {}, setAttribute() {} }),"
               " createTextNode: () => ({}), createDocumentFragment: () => ({ appendChild() {} }) }, {});\n"
               "const ms2 = Number(process.hrtime.bigint() - t0) / 1e6 - ms;\n"
               "process.stdout.write(JSON.stringify({ ms, ms2, kinds: nodes.map((n) => n.t),"
               " text: nodes.map((n) => (n.t === 'text' ? n.v : n.href || '')) }));",
               tmp_path, {"line": line}, timeout=60)
    assert out["ms"] < 10_000 and out["ms2"] < 10_000, out
    # the same nodes as the renderer of c1ebe82 (compared on this line: 12 s there): every unclosed mark stays text,
    # the address runs up to the spaces (it ends in z, so no parenthesis is cut), the spaces before the line
    # end make a break
    assert out["kinds"] == ["text", "link", "br", "text"], out["kinds"]
    assert out["text"] == ["".join(head), "http://a" + ")" * k + "z", "", "end"]
