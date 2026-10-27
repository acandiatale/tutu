"""v2.1d (docs/INTERFACES.md "v2.1d"): a try's major upgrades / library replacements and the 운영자 확인 guide on the
error list — `ErrorList.finish_try(major_upgrades=, verify=)` (masked, capped), errors.jsonl / the per-error JSON
round trip, errors.md (`⚠ 메이저 업그레이드` in the tries cell, `메이저 변경:` / `운영자 확인:` per try) and
`entry_lines` (`sbmig kb errors show`)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sbmig.core.models import DependencyChange, ErrorEntry
from sbmig.errors import CaseStore, ErrorList, entry_lines, render_md

from .test_entries import build, err

MAJOR = DependencyChange(path="demo-back/pom.xml", key="org.sitemesh:sitemesh", before="2.4.2", after="3.2.1",
                         kind="major")
REPLACED = DependencyChange(path="demo-back/pom.xml", key="opensymphony:sitemesh", before="2.4.2", after="3.2.1",
                            kind="replaced", replaced_by="org.sitemesh:sitemesh", approved=True,
                            note="libraries.yml allow_major")
GUIDE = "- web.xml 의 SiteMeshFilter 등록 확인\n- decorators.xml → sitemesh3.xml\n\n* JSP 태그 <decorator:*> 확인"


@pytest.fixture()
def el(tmp_path: Path, cfg) -> ErrorList:
    return ErrorList(tmp_path / "run", "run-1", project="demo", cfg=cfg, roots=[tmp_path / "run" / "project"])


def tried(el: ErrorList) -> tuple[str, int]:
    el.add_build(build(err()), "compile")
    case = el.entries[0].case_id
    t = el.begin_try(case, 1, "llm-new")
    return case, t.n


def test_finish_try_stores_the_changes_and_the_guide(el: ErrorList, tmp_path):
    case, n = tried(el)
    secret_path = str(tmp_path / "run" / "project" / "demo-back" / "pom.xml")
    t = el.finish_try(case, n, applied="edits", result="improved", verify=GUIDE + "\n" + "x" * 5000,
                      major_upgrades=[MAJOR, REPLACED.model_dump(), {"bad": "item"},
                                      MAJOR.model_copy(update={"path": secret_path})])
    assert [c.kind for c in t.major_upgrades] == ["major", "replaced", "major"]       # the malformed item is left out
    assert t.major_upgrades[1].approved and t.major_upgrades[1].replaced_by == "org.sitemesh:sitemesh"
    assert str(tmp_path) not in t.major_upgrades[2].path                              # workspace roots stripped
    assert t.verify.startswith("- web.xml 의 SiteMeshFilter") and len(t.verify) < 4200
    # None keeps what is there (the scoring call)
    el.finish_try(case, n, result="build_ok")
    assert el.get_try(case, n).major_upgrades[0] == MAJOR and el.get_try(case, n).verify


def test_the_changes_round_trip_through_errors_jsonl_and_the_per_error_json(el: ErrorList, tmp_path, cfg):
    case, n = tried(el)
    el.finish_try(case, n, applied="edits", result="improved", major_upgrades=[MAJOR, REPLACED], verify=GUIDE)
    rows = [json.loads(x) for x in el.path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["tries"][0]["major_upgrades"][0]["key"] == "org.sitemesh:sitemesh"
    assert rows[0]["tries"][0]["verify"] == GUIDE
    [back] = ErrorList.load(el.path)
    assert back.tries[0].major_upgrades == [MAJOR, REPLACED] and back.tries[0].verify == GUIDE
    store = CaseStore(tmp_path / "kb", cfg)
    assert store.record_run(el.entries) == 1
    [f] = store.entry_files(case)
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["tries"][0]["major_upgrades"][1]["replaced_by"] == "org.sitemesh:sitemesh"
    assert data["tries"][0]["verify"] == GUIDE
    assert ErrorEntry.model_validate(data).tries[0].major_upgrades == [MAJOR, REPLACED]


def test_errors_md_marks_the_try_and_lists_the_changes_and_the_guide(el: ErrorList):
    case, n = tried(el)
    el.finish_try(case, n, applied="edits", result="improved", major_upgrades=[MAJOR, REPLACED], verify=GUIDE)
    md = render_md(el.entries, project="demo", run_id="run-1")
    row = next(ln for ln in md.splitlines() if ln.startswith("| 1 |"))
    assert "⚠ 메이저 업그레이드 2" in row
    assert "- 메이저 변경 (⚠ 메이저 업그레이드, 운영자 확인 필요):" in md
    assert "  - org.sitemesh:sitemesh 2.4.2 → 3.2.1 (dependencies, demo-back/pom.xml)" in md
    assert ("  - opensymphony:sitemesh 2.4.2 → org.sitemesh:sitemesh 3.2.1 (dependencies, demo-back/pom.xml), "
            "approved") in md
    assert "- 운영자 확인:" in md and "  - web.xml 의 SiteMeshFilter 등록 확인" in md
    assert "  - JSP 태그 <decorator:*> 확인" in md and "  - - " not in md
    # a try without them: no marker, no section
    el2_case, n2 = case, el.begin_try(case, 2, "llm-augment").n
    el.finish_try(el2_case, n2, applied="script", result="no_change")
    md2 = render_md(el.entries)
    assert md2.count("- 메이저 변경") == 1


def test_entry_lines_print_the_changes_and_the_guide(el: ErrorList):
    case, n = tried(el)
    el.finish_try(case, n, applied="edits", result="improved", major_upgrades=[MAJOR, REPLACED], verify=GUIDE)
    text = "\n".join(entry_lines(el.entries[0], diff=False))
    assert ("    메이저 변경:\n      org.sitemesh:sitemesh 2.4.2 → 3.2.1 (dependencies, demo-back/pom.xml)\n"
            "      opensymphony:sitemesh 2.4.2 → org.sitemesh:sitemesh 3.2.1 (dependencies, demo-back/pom.xml), "
            "approved\n    운영자 확인:\n      web.xml 의 SiteMeshFilter 등록 확인\n      decorators.xml → sitemesh3.xml\n"
            "      JSP 태그 <decorator:*> 확인") in text
    plain = "\n".join(entry_lines(ErrorEntry(id="x", tries=[]), diff=False))
    assert "메이저 변경" not in plain and "운영자 확인" not in plain
