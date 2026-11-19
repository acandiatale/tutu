"""The code-change view in a large dialog (the owner, 2026-11-19: "소스 변경사항 보여주는거 크게 모달로 변경 가능해?"), one file
at a time and static resources shown by their move only ("선택한 파일만 볼 수 있도록 해줘! 정적자원들은 위치 이동 표시만 하고
컨텐츠 내용은 없어도 돼!"). Static checks on the page source; the behaviour was looked at in a browser."""
from __future__ import annotations

import re
from pathlib import Path

import sbmig.web

PAGE = (Path(sbmig.web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
STYLE = PAGE.split("<style>", 1)[1].split("</style>", 1)[0]


def _function(name: str, indent: str = "  ") -> str:
    m = re.search(r"\n" + indent + r"(?:async )?function " + name + r"\(.*?\n" + indent + r"}\n", SCRIPT, re.S)
    assert m, name
    return m.group(0)


def _component() -> str:
    start = SCRIPT.index("const CodeDiff = (() => {")
    return SCRIPT[start:SCRIPT.index("  })();", start)]


# ---- the dialog

def test_native_dialog_labelled_by_its_title_with_a_close_button() -> None:
    assert '<dialog class="cdmodal" id="cd-dialog" aria-labelledby="cd-title">' in PAGE
    dlg = PAGE[PAGE.index('<dialog class="cdmodal"'):PAGE.index("</dialog>")]
    assert '<h2 id="cd-title">코드 변경</h2>' in dlg and 'id="cd-dname"' in dlg and 'id="cd-dcounts"' in dlg
    assert '<button class="btn ghost cdm-close" id="cd-close" type="button">닫기</button>' in dlg
    assert '<div class="cdm-body" id="cd-host"></div>' in dlg


def test_open_mounts_and_close_unmounts() -> None:
    opener = _function("openCodeDiff")
    assert "dlg.showModal();" in opener and "CodeDiff.mount($('cd-host'), cdv.run);" in opener
    assert opener.index("dlg.showModal();") < opener.index("CodeDiff.mount(")
    assert "cdv.opener = document.activeElement;" in opener
    assert "$('cd-dname').textContent = cdv.name;" in opener                      # the upload name as text
    closed = _function("onCodeDiffClosed")
    assert "CodeDiff.unmount();" in closed and "back.focus()" in closed           # focus returns to the opener
    assert "$('cd-dialog').addEventListener('close', onCodeDiffClosed);" in SCRIPT
    assert "$('cd-open').addEventListener('click', openCodeDiff);" in SCRIPT
    assert "$('cd-close').addEventListener('click', () => $('cd-dialog').close());" in SCRIPT


def test_backdrop_click_and_escape_close() -> None:
    # the backdrop: a click whose target is the dialog element itself (its content fills it edge to edge)
    assert "$('cd-dialog').addEventListener('click', (e) => { if (e.target === $('cd-dialog')) $('cd-dialog').close(); });" in SCRIPT
    assert ".cdmodal { width: min(96vw, 1680px);" in STYLE and "padding: 0;" in STYLE.split(".cdmodal {", 1)[1].split("}", 1)[0]
    # Tab wraps inside the dialog instead of leaving for the browser's address bar
    assert "if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }" in SCRIPT
    assert "else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }" in SCRIPT
    # Esc: the native dialog's cancel closes it and ends in the same close event; nothing in the component swallows Esc
    assert "Escape" not in _component()


def test_size_backdrop_scroll_lock_and_motion() -> None:
    rule = STYLE.split(".cdmodal {", 1)[1].split("}", 1)[0]
    assert "width: min(96vw, 1680px)" in rule and "height: 92vh" in rule
    assert ".cdmodal::backdrop { background: rgba(19, 33, 58, .55); }" in STYLE          # the page's ink, dimmed
    phone = STYLE.split("@media (max-width: 640px) {\n    .cdmodal {", 1)[1].split("}", 1)[0]
    assert "width: 100vw; height: 100dvh" in phone
    assert ".cdmodal .cdm-body { flex: 1 1 auto; min-height: 0; overflow: auto;" in STYLE
    assert "html.cdm-lock, html.cdm-lock body { overflow: hidden; }" in STYLE
    assert "document.documentElement.classList.add('cdm-lock');" in _function("openCodeDiff")
    assert "document.documentElement.classList.remove('cdm-lock');" in _function("onCodeDiffClosed")
    assert "@media (prefers-reduced-motion: reduce) { .cdmodal[open] { animation: none; } }" in STYLE


# ---- one file at a time

def test_pane_and_tree_scroll_on_their_own() -> None:
    assert ".cdiff .cd-pane { min-height: 0; min-width: 0; overflow: auto;" in STYLE
    assert "grid-template-rows: minmax(0, 1fr)" in STYLE
    assert ".cdiff .cd-scroll { overflow-x: auto;" in STYLE                         # long lines scroll inside the pane
    comp = _component()
    # no loop renders every file into the pane: one card, replaced on each selection
    assert "s.ui.pane.replaceChildren(navBar(s, i), st.card);" in comp
    assert not re.search(r"for \(.*\) .*fileCard\(", comp)


def test_first_selection_and_empty_case() -> None:
    comp = _component()
    assert ("const hasContent = (f) => !isStatic(f.path) && !movedOnly(f) && !f.binary && !f.omitted;" in comp)
    refresh = _function("refresh", "    ")
    assert "const first = keep != null ? keep : (s.order.find(hasContent) || {}).id;" in refresh
    assert "'이 목록에는 줄 단위로 보여 줄 변경이 없으니, 목록에서 파일을 골라 이동과 상태를 확인해 주세요.'" in refresh
    assert "select(s, first, false);" in refresh


def test_keyboard_in_the_list() -> None:
    key = _function("treeKey", "    ")
    for k in ("ArrowDown", "ArrowUp", "Home", "End", "ArrowRight", "ArrowLeft"):
        assert f"'{k}'" in key, k
    assert "rove(s, to); to.focus();" in key
    assert "ui.tree.addEventListener('keydown', (e) => treeKey(s, e));" in _component()
    assert "s.rove = b; b.tabIndex = 0;" in _function("rove", "    ")             # one tab stop for the whole list
    # a file row selects itself on click / Enter / Space (a button)
    assert "const b = button(null, null, () => select(s, f.id, true));" in _component()
    # a row in a folded folder is revealed before it is marked
    assert "for (const d of s.anc.get(id) || []) if (d.open) d.open();" in _function("select", "    ")


def test_previous_and_next_file() -> None:
    nav = _function("navBar", "    ")
    assert "button('이전 파일'" in nav and "button('다음 파일'" in nav
    assert "`${nf(i + 1)} / ${nf(s.order.length)}`" in nav
    assert "prev.disabled = i <= 0; next.disabled = i >= s.order.length - 1;" in nav


# ---- static resources

STATIC = ("js", "mjs", "cjs", "css", "scss", "sass", "less", "map", "html", "htm", "png", "jpg", "jpeg", "gif", "bmp",
          "ico", "svg", "webp", "tif", "tiff", "woff", "woff2", "ttf", "otf", "eot", "mp3", "mp4", "wav", "ogg", "webm",
          "avi", "mov", "swf", "zip", "gz", "tgz", "tar", "7z", "rar", "jar", "war", "ear", "pdf", "doc", "docx", "xls",
          "xlsx", "ppt", "pptx", "hwp")
NOT_STATIC = ("jsp", "jspf", "tag", "tld", "java", "xml", "properties", "yml", "yaml", "sql", "gradle", "json", "txt", "ts", "vue")


def test_static_rule_by_extension() -> None:
    table = SCRIPT.split("const STATIC_EXT = new Set([", 1)[1].split("]);", 1)[0]
    exts = set(re.findall(r"'([a-z0-9]+)'", table))
    assert exts == set(STATIC), sorted(exts ^ set(STATIC))
    assert not exts & set(NOT_STATIC)
    fn = _function("isStatic", "    ")
    assert "STATIC_EXT.has(b.slice(i + 1).toLowerCase())" in fn and "i > 0" in fn


def test_a_static_file_is_never_fetched() -> None:
    load = _function("loadBody", "    ")
    assert "if (st.loading || st.loaded || isStatic(st.f.path)) return;" in load
    assert load.index("isStatic(st.f.path)") < load.index("getJson(")
    card = _function("fileCard", "    ")
    assert "if (isStatic(f.path)) { st.loaded = true; fold.hidden = true; body.appendChild(staticNote(f)); }" in card
    assert card.index("isStatic(f.path)") < card.index("f.binary || f.omitted === 'binary'")


def test_static_notices() -> None:
    note = _function("staticNote", "    ")
    for text in ("'위치만 옮긴 파일입니다.'", "'새로 추가한 정적 파일입니다. 정적 파일은 내용을 보여 주지 않습니다.'",
                 "'삭제한 정적 파일입니다. 정적 파일은 내용을 보여 주지 않습니다.'", "'위치를 옮기면서 내용도 바뀐 정적 파일입니다'",
                 "'내용이 바뀐 정적 파일입니다'", "`${what}${counts}. 정적 파일은 줄 단위로 보여 주지 않습니다.`",
                 "` (추가 ${nf(a)}줄, 삭제 ${nf(r)}줄)`", "'원래 위치'", "'옮긴 위치'"):
        assert text in note, text
    assert "innerHTML" not in note
    # the summary says how many are static
    assert "` 정적 파일 ${nf(nStatic)}개는 위치와 상태만 표시합니다.`" in _component()


# ---- the list and the pane get the dialog's height (coordinator, after looking at the first screenshots)

def test_top_area_is_bounded_and_the_main_area_fills_the_rest() -> None:
    assert ".cdiff .cd-top { max-height: 28%; overflow: auto; min-height: 0; }" in STYLE
    main = STYLE.split(".cdiff .cd-main {", 1)[1].split("}", 1)[0]
    assert "flex: 1 1 auto" in main and "min-height: 0" in main and "grid-template-rows: minmax(0, 1fr)" in main
    assert ".cdiff { --cd-add" in STYLE and "display: flex; flex-direction: column; height: 100%;" in STYLE
    # narrow: the tree on top, bounded; the pane takes the rest
    narrow = STYLE.split("@container (max-width: 720px) {", 1)[1].split("\n  }", 1)[0]
    assert "grid-template-rows: auto minmax(0, 1fr)" in narrow and ".cdiff .cd-side { max-height: 22vh;" in narrow
    build = _function("build", "    ")
    assert "const top = el('div', 'cd-top');" in build and "root.appendChild(bar);" in build   # the filter row stays visible


def test_folder_structure_box_folds() -> None:
    panel = _function("movesPanel", "    ")
    assert "const head = button(null, 'cd-mhead');" in panel
    assert "` · 묶음 ${nf(total)}개, 파일 ${nf(nFiles)}개`" in panel and "'폴더 구조가 바뀌었습니다'" in panel
    assert "head.setAttribute('aria-expanded', String(on)); inner.hidden = !on;" in panel
    assert "head.setAttribute('aria-controls', inner.id);" in panel
    # open when short and wide, folded when long or on a phone
    assert "const MOVES_OPEN = 3;" in _component()
    assert "const open = moves.length <= MOVES_OPEN && s.host.clientWidth >= 640;" in panel
    # a group row still filters the list
    assert "tr.addEventListener('click', () => { s.f.move = s.f.move === key ? null : key; refresh(s); });" in panel
    # unfolded, the table scrolls inside a bounded height
    assert ".cdiff .cd-mwrap { max-height: 96px;" in STYLE


def test_summary_sentence_is_short() -> None:
    text = _function("summaryText", "    ")
    assert "추가한 줄은" not in text and " 바이너리 파일 ${" not in text          # in the header chips / per-file notes
    assert "`(내용도 바뀐 파일 ${nf(both)}개)`" in text
