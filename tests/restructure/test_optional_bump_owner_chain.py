"""The optional-artifact bump edits the property the module really inherits — its own pom, then its parent chain
inside the project — never a sibling's that happens to define the same name (review P4)."""
from __future__ import annotations

from pathlib import Path

from sbmig.core.config import Config
from sbmig.core.models import CaseType
from sbmig.restructure import PomEditor, Restructurer

from .helpers import HEAD, dep, make_ws, profile


def _pom(aid: str, *, parent: str | None = None, rel: str | None = None, modules: tuple[str, ...] = (),
         props: str = "", deps: str = "", packaging: str = "jar") -> str:
    par = ""
    if parent:
        par = (f"    <parent>\n        <groupId>com.acme</groupId>\n        <artifactId>{parent}</artifactId>\n"
               f"        <version>1.0</version>\n" + (f"        <relativePath>{rel}</relativePath>\n" if rel else "")
               + "    </parent>\n")
    mods = ("    <modules>\n" + "".join(f"        <module>{m}</module>\n" for m in modules) + "    </modules>\n"
            if modules else "")
    return (HEAD + par + f"    <groupId>com.acme</groupId>\n    <artifactId>{aid}</artifactId>\n    <version>1.0</version>\n"
            f"    <packaging>{packaging}</packaging>\n" + mods
            + (f"    <properties>\n{props}    </properties>\n" if props else "")
            + (f"    <dependencies>\n{deps}    </dependencies>\n" if deps else "") + "</project>\n")


GUAVA_PROP = "        <guava.version>30.0-jre</guava.version>\n"


def _files(chain_defines: bool) -> dict[str, str]:
    return {
        "pom.xml": _pom("platform", modules=("aaa-sib", "zzz-parent"), packaging="pom"),
        "aaa-sib/pom.xml": _pom("aaa-sib", parent="platform", props=GUAVA_PROP),
        "zzz-parent/pom.xml": _pom("zzz-parent", parent="platform", modules=("a",), packaging="pom",
                                   props=GUAVA_PROP if chain_defines else ""),
        "zzz-parent/a/pom.xml": _pom("a", parent="zzz-parent",
                                     deps=dep("com.google.guava", "guava", "${guava.version}")),
    }


def test_the_property_of_the_modules_own_parent_chain_is_bumped_not_a_siblings(tmp_path: Path, cfg: Config) -> None:
    ws = make_ws(tmp_path, _files(True), "chain")
    res = Restructurer().apply(ws, profile(ws, CaseType.MSA, artifact_id="platform", version="1.0"), cfg,
                               mode="optional_bump")
    assert res.ok, res.error
    assert PomEditor(ws.root / "zzz-parent/pom.xml").get_property("guava.version") == "33.4.8-jre"
    assert PomEditor(ws.root / "aaa-sib/pom.xml").get_property("guava.version") == "30.0-jre"
    assert res.optional_bumped == ["com.google.guava:guava 30.0-jre→33.4.8-jre"]


def test_a_property_only_a_sibling_defines_is_not_reported_as_bumped_there(tmp_path: Path, cfg: Config) -> None:
    ws = make_ws(tmp_path, _files(False), "sib")
    res = Restructurer().apply(ws, profile(ws, CaseType.MSA, artifact_id="platform", version="1.0"), cfg,
                               mode="optional_bump")
    assert res.ok, res.error
    assert PomEditor(ws.root / "aaa-sib/pom.xml").get_property("guava.version") == "30.0-jre"
    a = PomEditor(ws.root / "zzz-parent/a/pom.xml").dependencies()[0]
    # the module itself now names the configured version: the reported bump is the one that happened
    assert a.version == "33.4.8-jre"
    assert res.optional_bumped == ["com.google.guava:guava ${guava.version}→33.4.8-jre"]
