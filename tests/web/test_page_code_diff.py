"""v2.1z-au: the code-change view on the page (`sbmig/web/static/index.html`). One block 「코드 변경」 in the result card
(after 「취약점 조치」, before the report preview), shown when the run status carries `code_diff`, closed until opened;
inside it the self-contained component `CodeDiff` (`mount(host, runId)` / `unmount()`, styles under `.cdiff`) so a
dialog can host it later. Static checks on the page source, like `test_page_remediation.py`."""
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
    end = SCRIPT.index("  })();", start)
    return SCRIPT[start:end]


def test_block_sits_between_remediation_and_preview_as_one_row() -> None:
    remed, block, preview = PAGE.index('id="remed"'), PAGE.index('id="cdblock"'), PAGE.index('id="preview"')
    assert remed < block < preview
    assert '<div class="cdblock" id="cdblock" hidden>' in PAGE
    assert "<h2>코드 변경</h2>" in PAGE
    # one row: heading, the summary chips, one button that opens the dialog (the view itself lives in the dialog)
    assert ('<button class="btn quiet" id="cd-open" type="button" aria-haspopup="dialog" aria-controls="cd-dialog">코드 변경 보기</button>'
            in PAGE)
    card = PAGE[PAGE.index('id="cdblock"'):PAGE.index('id="preview"')]
    assert 'id="cd-host"' not in card


def test_shown_from_the_status_hidden_for_remediation_runs_and_reset_on_attach() -> None:
    assert "showCodeDiff(st, remOnly);" in _function("showResult")
    show = _function("showCodeDiff")
    assert "st.code_diff" in show and "if (remOnly || !sum || !current) { hideCodeDiff(); return; }" in show
    for chip in ("`파일 ${nf(sum.files)}개`", "`+${nf(sum.added_lines)}줄`", "`−${nf(sum.removed_lines)}줄`", "`위치 이동 ${nf(sum.moved)}개`"):
        assert chip in _function("cdChips"), chip
    assert "hideCodeDiff();" in _function("attach")
    hide = _function("hideCodeDiff")
    assert "closeCodeDiff();" in hide and "$('cdblock').hidden = true;" in hide
    # opening another run closes an open dialog first (attach → hideCodeDiff → closeCodeDiff)
    assert "if (dlg.open) dlg.close();" in _function("closeCodeDiff")


def test_nothing_is_fetched_until_the_dialog_is_opened() -> None:
    # the page glue mounts only when the dialog opens; the component fetches the index in mount
    glue = SCRIPT[SCRIPT.index("const cdv = {"):SCRIPT.index("// ---------------------------------------------------------------- 3d. 보고서 보기")]
    assert "fetch(" not in glue
    assert glue.count("CodeDiff.mount(") == 1 and "CodeDiff.mount($('cd-host'), cdv.run);" in _function("openCodeDiff")
    assert "CodeDiff.mount" not in _function("showCodeDiff")


def test_component_is_self_contained() -> None:
    comp = _component()
    assert "return { mount, unmount };" in SCRIPT[SCRIPT.index("const CodeDiff = (() => {"):]
    top = SCRIPT[SCRIPT.index("3c. 코드 변경"):SCRIPT.index("const CodeDiff = (() => {")]
    assert "CodeDiff.mount(대화상자 본문, 실행 id)" in top            # the "move into a dialog later" note on top
    # no page state, no page element ids: everything is built inside host
    code = re.sub(r"//.*", "", comp)
    for page_thing in (r"\$\(", r"(?<![-\w])current\b", r"\brunFlow\b", r"\blastState\b", r"getElementById", r"\bauthProblem\b", r"\btoast\("):
        assert not re.search(page_thing, code), page_thing
    unmount = _function("unmount", "    ")
    for token in ("c.abort()", "clearTimeout(t)", "s.root.remove()"):
        assert token in unmount, token
    # every rule of the component lives under .cdiff
    css = STYLE[STYLE.index("/* ---- CodeDiff"):STYLE.index("/* ---- 4. 지난 실행")]
    rules = [r.strip() for r in re.findall(r"([^{}]+)\{[^{}]*\}", re.sub(r"/\*.*?\*/", "", css, flags=re.S))]
    selectors = [s for r in rules for s in r.split(",") if not re.fullmatch(r"\s*(\d+%|from|to)\s*", s)]
    assert selectors and all(s.strip().startswith(".cdiff") for s in selectors), [s for s in selectors if not s.strip().startswith(".cdiff")]


def test_late_answers_are_dropped_and_requests_time_out() -> None:
    comp = _component()
    assert "const live = (s) => S === s;" in comp
    assert comp.count("if (!live(s)") >= 4                                   # index, file body, lines, whole file
    get = _function("getJson", "    ")
    assert "new AbortController()" in get and "setTimeout(() => c.abort(), TIMEOUT_MS)" in get
    assert "finally" in get and "s.reqs.delete(c)" in get


def test_routes_of_the_contract() -> None:
    comp = _component()
    assert "`/api/runs/${encodeURIComponent(s.run)}/codediff`" in comp
    assert "`/files/${id}`" in comp
    assert "`/files/${id}/lines?start=${start}&end=${Math.min(b, start + CHUNK - 1)}`" in comp
    lines = _function("fetchLines", "    ")
    assert "d.end" in lines and "d.total" in lines and "start = end + 1" in lines   # ask again past a cut answer


def test_one_file_at_a_time() -> None:
    """The owner (2026-11-19): "선택한 파일만 볼 수 있도록 해줘!" — the pane shows the file chosen in the list, no paging."""
    comp = _component()
    assert "PAGE_FILES" not in comp and "renderMore" not in comp and "IntersectionObserver" not in comp
    assert "BIG_LINES = 400" in comp
    sel = _function("select", "    ")
    assert "s.ui.pane.replaceChildren(navBar(s, i), st.card);" in sel and "s.cur = id;" in sel
    assert "row.setAttribute('aria-current', 'true'); row.scrollIntoView({ block: 'nearest' });" in sel
    assert "s.ui.pane.scrollTop = 0;" in sel and "if (!st.folded && !st.loaded) loadBody(s, st);" in sel
    # fileCard is called for the selected file only
    assert comp.count("fileCard(s, ") == 2 and "const st = fileCard(s, f);" in sel   # the definition and this one call


def test_texts_from_the_server_go_in_as_text() -> None:
    comp = _component()
    assert "innerHTML" not in comp and "insertAdjacentHTML" not in comp
    assert "innerHTML" not in _function("showCodeDiff") and "innerHTML" not in _function("hideCodeDiff")


def test_states_and_korean_texts() -> None:
    comp = _component()
    for text in ("폴더 구조가 바뀌었습니다", "회사 표준 구조에 맞춰 옮긴 위치입니다. 줄을 누르면 그 묶음의 파일만 목록에 보여 줍니다.",
                 "'(프로젝트 최상위)'", "'원래 위치'", "'옮긴 위치'", "'파일 수'", "'파일 경로 검색'", "'위치만 바뀐 파일도 보기'",
                 "['all', '전체'], ['modified', '수정'], ['added', '추가'], ['deleted', '삭제'], ['moved', '이동']",
                 "'이동 후 수정'", "'원래 위치:'", "'바이너리 파일이라 내용을 표시하지 않습니다.'", "변경 내용이 커서 화면에 싣지 않았습니다.",
                 "'변환된 프로젝트 ZIP에서 확인해 주세요.'", "'불러오는 중입니다.'", "'변경 내용을 불러오지 못했습니다.'",
                 "'다시 시도'", "'바뀐 파일이 없습니다.'", "'조건에 맞는 파일이 없습니다.'", "'내용은 그대로이고 위치만 바뀌었습니다.'",
                 "'파일 내용 보기'", "'파일 전체 보기'", "결과 파일을 아직 정리하고 있습니다.", "이 파일의 변경 내용을 표시할 수 없습니다.",
                 "`가려진 ${nf(n)}줄 모두 보기`", "`아래로 ${STEP}줄 더 보기`", "`위로 ${STEP}줄 더 보기`", "`바뀐 파일이 많아 ${nf(total)}개 가운데"):
        assert text in comp, text
    assert ">코드 변경 보기</button>" in PAGE
    # omitted values of the API, each with its own sentence
    for value in ("'binary'", "'too_large'", "'cap'", "'unreadable'", "'unavailable'"):
        assert value in comp, value
    # no word-for-word code vocabulary in what the reader sees
    shown = " ".join(a or b for a, b in re.findall(r"'([^'\n]*[가-힣][^'\n]*)'|`([^`\n]*[가-힣][^`\n]*)`", comp))
    for word in ("hunk", "diff", "omitted", "헝크", "디프"):
        assert word not in shown.lower(), word


def test_expansion_hidden_for_deleted_added_and_not_expandable_files() -> None:
    body = _function("renderBody", "    ")
    assert "const canMore = !!d.expandable && f.status !== 'deleted' && f.status !== 'added';" in body
    assert "if (d.expandable) box.appendChild(button('파일 내용 보기'" in body
    gap = _function("expand", "    ")
    assert "x.no + g.delta" in gap                              # the old number follows from the neighbouring change


def test_look_uses_the_page_tokens_and_keeps_marks_besides_colour() -> None:
    css = STYLE[STYLE.index("/* ---- CodeDiff"):STYLE.index("/* ---- 4. 지난 실행")]
    for token in ("var(--blue)", "var(--line)", "var(--ok)", "var(--bad)", "var(--mono)", "var(--radius-s)"):
        assert token in css, token
    assert "white-space: pre" in css and "tab-size: 4" in css and "overflow-x: auto" in css
    assert "@container (max-width: 720px)" in css
    row = _function("lineRow", "    ")
    assert "'+', text" in row and "'−', text" in row             # the + / − marks stay next to the colours


def test_new_page_comments_read_hand_written() -> None:
    start = SCRIPT.index("// ---------------------------------------------------------------- 3c. 코드 변경")
    end = SCRIPT.index("// ---------------------------------------------------------------- 실행에 연결 (SSE)")
    comments = [ln.split("//", 1)[1] for ln in SCRIPT[start:end].splitlines() if "//" in ln and "://" not in ln]
    css = STYLE[STYLE.index("/* ---- 3c."):STYLE.index("/* ---- 4. 지난 실행")]
    comments += re.findall(r"/\*(.*?)\*/", css, re.S)
    comments += re.findall(r"<!--(.*?)-->", PAGE[PAGE.index('id="remed-act"'):PAGE.index('id="preview"')], re.S)
    assert comments
    for c in comments:
        assert not re.search(r"[—→]|\bv\d+\.\d|\d{4}-\d{2}-\d{2}|\*\*", c), c


def test_moved_only_files_are_listed_from_the_start_when_there_are_few() -> None:
    """The requester asked to see where a Java file came from; a restructure that only moves a few hundred files shows
    them at once, a larger one keeps them behind the checkbox so the real changes are not drowned."""
    assert "const MOVED_ONLY_SHOWN = 200;" in SCRIPT
    assert "s.f.movedOnly = s.movedOnly > 0 && s.movedOnly <= MOVED_ONLY_SHOWN;" in SCRIPT
    assert "cb.checked = s.f.movedOnly;" in SCRIPT
