"""v2.1k (2026-10-29): a module reached twice through the reactor's `<module>` lists ("Project … is duplicated in the
reactor") is found by `reactor_duplicates` and its duplicate listings of the non-root aggregators are removed by
`dedupe_reactor` (sbmig.analyze.aggregator) — before any Maven call."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze.aggregator import (
    dedupe_reactor,
    reactor_commit_message,
    reactor_duplicates,
    reactor_followups,
)
from sbmig.analyze.analyzer import PathView

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'


def _pom(aid: str, packaging: str = "jar", modules: list[str] | None = None, profiles: str = "",
         parent: str = "") -> str:
    mods = ("  <modules>\n" + "".join(f"    <module>{m}</module>\n" for m in modules) + "  </modules>\n"
            if modules is not None else "")
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n{parent}'
            f"  <groupId>com.xxx</groupId>\n  <artifactId>{aid}</artifactId>\n  <version>1.0.0-SNAPSHOT</version>\n"
            f"  <packaging>{packaging}</packaging>\n{mods}{profiles}</project>\n")


def _profile(pid: str, modules: list[str], default: bool) -> str:
    act = "      <activation>\n        <activeByDefault>true</activeByDefault>\n      </activation>\n" if default else ""
    return ("  <profiles>\n    <profile>\n      <id>" + pid + "</id>\n" + act + "      <modules>\n"
            + "".join(f"        <module>{m}</module>\n" for m in modules) + "      </modules>\n    </profile>\n"
            "  </profiles>\n")


def _tree(root: Path, files: dict[str, str | bytes]) -> PathView:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            p.write_bytes(text)
        else:
            p.write_text(text, encoding="utf-8")
    return PathView(root)


def _company(root: Path, app_modules: list[str] | None = None) -> PathView:
    """The company shape: root lists APP, DEV; APP lists ../DEV, ./EAR, ./BATCH."""
    return _tree(root, {
        "pom.xml": _pom("xxx-root", "pom", ["APP", "DEV"]),
        "APP/pom.xml": _pom("xxx-app", "pom", app_modules if app_modules is not None else ["../DEV", "./EAR", "./BATCH"]),
        "APP/EAR/pom.xml": _pom("xxx-ear", "ear"),
        "APP/BATCH/pom.xml": _pom("xxx-batch"),
        "DEV/pom.xml": _pom("xxx", "war"),
    })


def test_company_shape_keeps_the_root_listing_and_removes_the_nested_one(tmp_path):
    ws = _company(tmp_path)
    root_before = (tmp_path / "pom.xml").read_bytes()
    app_before = (tmp_path / "APP/pom.xml").read_text(encoding="utf-8")
    dups = reactor_duplicates(ws)
    assert len(dups) == 1
    d = dups[0]
    assert d.dir == "DEV" and d.is_duplicate
    assert d.listings == [("APP/pom.xml", "../DEV"), ("pom.xml", "DEV")]      # reactor order: APP is walked first
    assert d.keep == ("pom.xml", "DEV") and d.remove == [("APP/pom.xml", "../DEV")] and not d.left
    notes = dedupe_reactor(ws, duplicates=dups)
    assert d.removed == [("APP/pom.xml", "../DEV")]
    assert notes == [("APP/pom.xml: <module>../DEV</module> removed — DEV is already listed by pom.xml (Maven: "
                      "project duplicated in the reactor)")]
    assert (tmp_path / "pom.xml").read_bytes() == root_before                  # the root pom is never touched
    app = (tmp_path / "APP/pom.xml").read_text(encoding="utf-8")
    assert app == app_before.replace("    <module>../DEV</module>\n", "")     # only that line goes
    assert reactor_duplicates(ws) == []
    assert reactor_commit_message(dups) == ("intake: reactor duplicate removed (DEV listed by pom.xml and APP/pom.xml)"
                                            "\n\nAPP/pom.xml: <module>../DEV</module> removed — DEV stays listed by "
                                            "pom.xml")
    assert reactor_followups(dups) == [
        ("DEV was listed twice in the reactor (pom.xml and APP/pom.xml); the APP listing was removed so Maven can build "
         "— if APP is meant to be built on its own, add DEV back there and remove it from the root")]


def test_module_paths_are_normalised(tmp_path):
    for i, (root_text, app_text) in enumerate([("DEV", "../DEV/pom.xml"), ("./DEV/", "..\\DEV"),
                                               ("DEV/pom.xml", "../APP/../DEV")]):
        ws = _company(tmp_path / str(i), app_modules=[app_text, "./BATCH"])
        root = (tmp_path / str(i) / "pom.xml")
        root.write_text(root.read_text(encoding="utf-8").replace("<module>DEV</module>",
                                                                 f"<module>{root_text}</module>"), encoding="utf-8")
        dups = reactor_duplicates(ws)
        assert [(d.dir, d.keep, d.remove) for d in dups] == [("DEV", ("pom.xml", root_text),
                                                              [("APP/pom.xml", app_text)])], (root_text, app_text)
        dedupe_reactor(ws, duplicates=dups)
        assert app_text not in (tmp_path / str(i) / "APP/pom.xml").read_text(encoding="utf-8")
        assert reactor_duplicates(ws) == []


def test_a_profile_not_active_by_default_is_noted_not_deduped(tmp_path):
    ws = _company(tmp_path, app_modules=["./BATCH"])
    app = tmp_path / "APP/pom.xml"
    app.write_text(app.read_text(encoding="utf-8").replace(
        "</project>", _profile("full", ["../DEV"], default=False) + "</project>"), encoding="utf-8")
    before = app.read_bytes()
    dups = reactor_duplicates(ws)
    assert len(dups) == 1 and not dups[0].is_duplicate
    assert dups[0].listings == [("pom.xml", "DEV")] and dups[0].inactive == [("APP/pom.xml", "../DEV", "full")]
    notes = dedupe_reactor(ws, duplicates=dups)
    assert app.read_bytes() == before and dups[0].removed == []
    assert notes == [("APP/pom.xml: profile 'full' (not active by default) also lists DEV (../DEV) — left as is; "
                      "building with -Pfull would list DEV twice in the reactor")]
    assert reactor_commit_message(dups) is None
    assert reactor_followups(dups) == [
        ("DEV is also listed by profile 'full' of APP/pom.xml (not active by default): building with -Pfull lists DEV "
         "twice in the reactor — remove it from one of them if that profile is still used")]


def test_a_profile_active_by_default_is_part_of_the_reactor(tmp_path):
    ws = _company(tmp_path, app_modules=["./EAR"])
    app = tmp_path / "APP/pom.xml"
    app.write_text(app.read_text(encoding="utf-8").replace(
        "</project>", _profile("all", ["../DEV", "./BATCH"], default=True) + "</project>"), encoding="utf-8")
    dups = reactor_duplicates(ws)
    assert [(d.dir, d.listings, d.remove) for d in dups] == [
        ("DEV", [("APP/pom.xml", "../DEV"), ("pom.xml", "DEV")], [("APP/pom.xml", "../DEV")])]
    dedupe_reactor(ws, duplicates=dups)
    text = app.read_text(encoding="utf-8")
    assert "../DEV" not in text and "<module>./BATCH</module>" in text and "<module>./EAR</module>" in text
    assert reactor_duplicates(ws) == []


def test_the_same_module_twice_in_one_block(tmp_path):
    ws = _company(tmp_path, app_modules=["./EAR", "./BATCH", "./BATCH"])
    root = tmp_path / "pom.xml"
    root.write_text(_pom("xxx-root", "pom", ["APP", "DEV", "DEV"]), encoding="utf-8")
    (tmp_path / "APP/pom.xml").write_text(_pom("xxx-app", "pom", ["./EAR", "./BATCH", "./BATCH"]), encoding="utf-8")
    dups = {d.dir: d for d in reactor_duplicates(ws)}
    assert set(dups) == {"DEV", "APP/BATCH"}
    assert dups["DEV"].keep == ("pom.xml", "DEV") and dups["DEV"].remove == [("pom.xml", "DEV")]   # the repeat
    assert dups["APP/BATCH"].remove == [("APP/pom.xml", "./BATCH")]
    notes = dedupe_reactor(ws, duplicates=list(dups.values()))
    assert root.read_text(encoding="utf-8").count("<module>DEV</module>") == 1    # the root keeps one listing
    assert (tmp_path / "APP/pom.xml").read_text(encoding="utf-8").count("<module>./BATCH</module>") == 1
    assert any(n.startswith("pom.xml: repeated <module>DEV</module> removed") for n in notes)
    assert reactor_duplicates(ws) == []
    fu = reactor_followups(list(dups.values()))
    assert "DEV was listed more than once in the same <modules> of pom.xml; the repeated listing was removed so " \
           "Maven can build" in fu


def test_between_two_nested_aggregators_the_first_in_reactor_order_stays(tmp_path):
    ws = _tree(tmp_path, {
        "pom.xml": _pom("root", "pom", ["A", "B"]),
        "A/pom.xml": _pom("a", "pom", ["../LIB", "./X"]),
        "A/X/pom.xml": _pom("x"),
        "B/pom.xml": _pom("b", "pom", ["../LIB"]),
        "LIB/pom.xml": _pom("lib"),
    })
    dups = reactor_duplicates(ws)
    assert [(d.dir, d.keep, d.remove) for d in dups] == [("LIB", ("A/pom.xml", "../LIB"), [("B/pom.xml", "../LIB")])]
    dedupe_reactor(ws, duplicates=dups)
    b = (tmp_path / "B/pom.xml").read_text(encoding="utf-8")
    assert "<modules>" not in b and "<module>" not in b                          # an emptied <modules> goes too
    assert reactor_followups(dups) == [
        ("LIB was listed twice in the reactor (A/pom.xml and B/pom.xml); the B listing was removed so Maven can build "
         "— if B is meant to be built on its own, add LIB back there and remove it from A")]


def test_two_blocks_of_the_root_pom_are_left_and_noted(tmp_path):
    ws = _tree(tmp_path, {"pom.xml": _pom("root", "pom", ["DEV"], profiles=_profile("all", ["./DEV"], default=True)),
                          "DEV/pom.xml": _pom("dev")})
    before = (tmp_path / "pom.xml").read_bytes()
    dups = reactor_duplicates(ws)
    assert [(d.dir, d.keep, d.remove, d.left) for d in dups] == [("DEV", ("pom.xml", "DEV"), [], [("pom.xml", "./DEV")])]
    notes = dedupe_reactor(ws, duplicates=dups)
    assert (tmp_path / "pom.xml").read_bytes() == before
    assert notes == [("pom.xml: <module>./DEV</module> left in place — DEV is listed more than once by the root "
                      "pom; remove the extra listing by hand")]
    assert reactor_followups(dups) == [("DEV is listed more than once by the root pom (pom.xml): left as it is — "
                                        "remove the extra <module> by hand so Maven can build")]


def test_nothing_to_do(tmp_path):
    ws = _company(tmp_path, app_modules=["./EAR", "./BATCH"])
    assert reactor_duplicates(ws) == [] and dedupe_reactor(ws) == []
    assert reactor_duplicates(_tree(tmp_path / "none", {"APP/pom.xml": _pom("app")})) == []     # no root pom
    # a module that does not exist and a module pointing outside the project are not this error
    ws2 = _tree(tmp_path / "odd", {"pom.xml": _pom("root", "pom", ["MISSING", "../../elsewhere", "A"]),
                                   "A/pom.xml": _pom("a", "pom", [".."])})                     # A points back
    assert reactor_duplicates(ws2) == []


def test_encoding_line_endings_and_comments_survive(tmp_path):
    korean = "<!-- 배치 모듈 -->"
    app = (f'<?xml version="1.0" encoding="EUC-KR"?>\r\n<project {NS}>\r\n  <artifactId>xxx-app</artifactId>\r\n'
           f"  <packaging>pom</packaging>\r\n  <modules>\r\n    <module>../DEV</module>\r\n    {korean}\r\n"
           f"    <module>./BATCH</module>\r\n  </modules>\r\n</project>\r\n").encode("cp949")
    ws = _tree(tmp_path, {"pom.xml": _pom("root", "pom", ["APP", "DEV"]), "APP/pom.xml": app,
                          "APP/BATCH/pom.xml": _pom("b"), "DEV/pom.xml": _pom("dev", "war")})
    dedupe_reactor(ws)
    raw = (tmp_path / "APP/pom.xml").read_bytes()
    text = raw.decode("cp949")
    assert korean in text and "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert text == app.decode("cp949").replace("    <module>../DEV</module>\r\n", "")
