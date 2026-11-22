"""v2.1z-br, visible effect 2: the framework guard of a reduced run fails closed.

A pom that cannot be read (OSError) or does not parse (before or after the attempt) is left to the build
(answer "" for that pom). Any other exception
inside the check rejects the attempt with `FRAMEWORK_GUARD_INCOMPLETE`: the working tree is restored, the journal has
the `framework_guard` event, the next ask sees the sentence, no exception text of the project is in it, and one
WARNING line names the exception type. sbmig's pre-tries and full-scope runs are still not checked."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from sbmig.loop.fixloop import (
    FRAMEWORK_GUARD_INCOMPLETE,
    FRAMEWORK_GUARD_PREFIX,
    PRE_TRY_SOURCES,
    BuildFixLoop,
)
from sbmig.restructure.pom_editor import PomEditError, PomEditor

from .fakes import FakeFixer, journal_events
from .test_fixloop_framework import FW, FW_V, _guard_run, fw_cfg, fw_pom, reduce
from .test_v21l_versions import FOO, _ws, make_loop, pom_build

SECRET = "CHANGE_ME_project_text_in_the_exception"
COMMENT = "<!-- harmless edit -->"


def _boom(*_a, **_k):
    raise RuntimeError(SECRET)


def _harmless_pom_edit() -> dict:
    """An edit that touches pom.xml (so the guard reads poms) without changing anything it protects."""
    return {"op": "write", "path": "pom.xml",
            "content": fw_pom(parent="com.example.fw:example-fw-parent:1.0").replace(
                "<modelVersion>", f"{COMMENT}\n  <modelVersion>", 1)}


def _loop(cfg, tmp_path, pom_text: str):
    ws = _ws(tmp_path, {"pom.xml": pom_text, FOO: "class Foo { broken }\n"})
    loop = reduce(make_loop(fw_cfg(cfg), ws, FakeFixer(), pom_build()))
    return ws, loop


LLM_ATT = SimpleNamespace(id="a1", source="llm")


# ------------------------------------------------------------------------------------------------ fails closed
def test_an_exception_inside_the_check_rejects_the_attempt(cfg, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(BuildFixLoop, "_framework_decls", _boom)
    caplog.set_level(logging.WARNING, logger="sbmig")
    ws, fixer, _, pr = _guard_run(cfg, tmp_path, [_harmless_pom_edit()])
    assert pr.ok
    first, second = pr.attempts
    assert first.outcome == "apply_failed" and not first.applied
    assert FRAMEWORK_GUARD_INCOMPLETE in first.apply_errors
    assert all(SECRET not in e for e in first.apply_errors)
    # the working tree is restored: the pom edit and the source edit of the rejected attempt are gone
    assert COMMENT not in ws.read("pom.xml")
    # the next ask sees the rejected attempt and its reason
    prev = fixer.contexts[1].previous_attempts
    assert any(FRAMEWORK_GUARD_INCOMPLETE in a.apply_errors for a in prev)
    # the journal has the event with the same sentence
    ev = [e for e in journal_events(ws) if e["event"] == "framework_guard"]
    assert len(ev) == 1 and ev[0]["reason"] == FRAMEWORK_GUARD_INCOMPLETE and ev[0]["attempt"] == first.id
    # an attempt that does not touch a pom is not checked and is kept
    assert second.applied and second.outcome == "build_ok"
    # one warning line with the exception type, never the exception text
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
             and "RuntimeError" in r.getMessage()]
    assert len(lines) == 1 and first.id in lines[0]
    assert not any(SECRET in r.getMessage() for r in caplog.records)


def test_the_rejection_sentence_has_the_form_of_the_guard_findings():
    assert FRAMEWORK_GUARD_INCOMPLETE.startswith(f"{FRAMEWORK_GUARD_PREFIX} the ")
    assert "could not be completed" in FRAMEWORK_GUARD_INCOMPLETE
    assert "nothing of it was kept" in FRAMEWORK_GUARD_INCOMPLETE
    assert "must stay exactly as they are" in FRAMEWORK_GUARD_INCOMPLETE


def _parse_raises(exc: BaseException):
    def from_string(cls, text, path="pom.xml"):
        raise exc
    return classmethod(from_string)


def test_a_parse_that_raises_anything_but_pom_edit_error_rejects(cfg, tmp_path, monkeypatch):
    """`_parse_poms` skips only PomEditError: any other exception from parsing fails the check closed."""
    ws, loop = _loop(cfg, tmp_path, fw_pom())
    pre = ws.head()
    ws.write("pom.xml", fw_pom().replace("<modelVersion>", f"{COMMENT}\n  <modelVersion>", 1))
    monkeypatch.setattr(PomEditor, "from_string", _parse_raises(RuntimeError(SECRET)))
    assert loop._framework_guard(LLM_ATT, pre, ["pom.xml"]) == FRAMEWORK_GUARD_INCOMPLETE


def test_a_parse_that_raises_pom_edit_error_is_left_to_the_build(cfg, tmp_path, monkeypatch):
    ws, loop = _loop(cfg, tmp_path, fw_pom())
    pre = ws.head()
    ws.write("pom.xml", fw_pom(version="2.0.0"))
    monkeypatch.setattr(PomEditor, "from_string", _parse_raises(PomEditError("pom.xml: malformed XML")))
    assert loop._framework_guard(LLM_ATT, pre, ["pom.xml"]) == ""


# ------------------------------------------------------------------------------------------------ unreadable poms
def _module_pom_cannot_be_read(ws, how: str) -> Path:
    """A module pom that cannot be read before any attempt: mode 000, or a directory named pom.xml."""
    p = ws.abs("mod/pom.xml")
    if how == "directory":
        p.mkdir(parents=True)
        (p / "x").write_text("x")
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(fw_pom().replace("<artifactId>demo</artifactId>", "<artifactId>mod</artifactId>", 1))
    ws.commit("pre-existing module pom")
    if how == "mode000":
        os.chmod(p, 0)
        try:
            p.read_bytes()
        except PermissionError:
            pass
        else:                                    # e.g. run as root: the file stays readable
            os.chmod(p, 0o644)
            pytest.skip("this process can read a mode-000 file")
    return p


@pytest.fixture()
def restore_modes():
    paths: list[Path] = []
    yield paths
    for p in paths:
        if p.is_file():
            os.chmod(p, 0o644)


@pytest.mark.parametrize("how", ["mode000", "directory"])
def test_an_unreadable_pom_is_left_to_the_build(cfg, tmp_path, restore_modes, how):
    ws, loop = _loop(cfg, tmp_path, fw_pom())
    restore_modes.append(_module_pom_cannot_be_read(ws, how))
    pre = ws.head()
    ws.write("pom.xml", fw_pom().replace("<modelVersion>", f"{COMMENT}\n  <modelVersion>", 1))
    assert loop._framework_guard(LLM_ATT, pre, ["pom.xml", "mod/pom.xml"]) == ""


@pytest.mark.parametrize("how", ["mode000", "directory"])
def test_an_unreadable_pom_does_not_hide_a_change_in_another(cfg, tmp_path, restore_modes, how):
    ws, loop = _loop(cfg, tmp_path, fw_pom())
    restore_modes.append(_module_pom_cannot_be_read(ws, how))
    pre = ws.head()
    ws.write("pom.xml", fw_pom(version="2.0.0"))
    why = loop._framework_guard(LLM_ATT, pre, ["pom.xml"])
    assert why.startswith(FRAMEWORK_GUARD_PREFIX) and why != FRAMEWORK_GUARD_INCOMPLETE
    assert f"changed the version of the framework artifact {FW} in pom.xml from {FW_V} to 2.0.0" in why


def test_a_pom_unreadable_only_after_the_attempt_is_left_to_the_build(cfg, tmp_path, restore_modes):
    ws, loop = _loop(cfg, tmp_path, fw_pom())
    pre = ws.head()
    p = ws.abs("pom.xml")
    ws.write("pom.xml", fw_pom(version="2.0.0"))
    os.chmod(p, 0)
    restore_modes.append(p)
    try:
        p.read_bytes()
        pytest.skip("this process can read a mode-000 file")
    except PermissionError:
        pass
    assert loop._framework_guard(LLM_ATT, pre, ["pom.xml"]) == ""


# ------------------------------------------------------------------------------------------------ unparsable poms
def test_a_pom_that_does_not_parse_after_the_attempt_is_left_to_the_build(cfg, tmp_path):
    ws, loop = _loop(cfg, tmp_path, fw_pom())
    pre = ws.head()
    ws.write("pom.xml", "<project><dependencies><dependency>")           # malformed XML
    assert loop._framework_guard(LLM_ATT, pre, ["pom.xml"]) == ""


def test_a_pom_that_did_not_parse_before_the_attempt_is_left_to_the_build(cfg, tmp_path):
    ws, loop = _loop(cfg, tmp_path, "<project><dependencies>\n")         # malformed in the commit `pre`
    pre = ws.head()
    ws.write("pom.xml", fw_pom(version="2.0.0"))
    assert loop._framework_guard(LLM_ATT, pre, ["pom.xml"]) == ""


def test_a_pom_whose_root_is_not_project_is_left_to_the_build(cfg, tmp_path):
    ws, loop = _loop(cfg, tmp_path, fw_pom())
    pre = ws.head()
    ws.write("pom.xml", "<?xml version=\"1.0\"?>\n<settings/>\n")
    assert loop._framework_guard(LLM_ATT, pre, ["pom.xml"]) == ""


def test_a_parsable_change_is_still_found(cfg, tmp_path):
    """Control: the same harness finds a real change, so the "" answers above are the parse rule."""
    ws, loop = _loop(cfg, tmp_path, fw_pom())
    pre = ws.head()
    ws.write("pom.xml", fw_pom(version="2.0.0"))
    why = loop._framework_guard(LLM_ATT, pre, ["pom.xml"])
    assert why.startswith(FRAMEWORK_GUARD_PREFIX)
    assert f"changed the version of the framework artifact {FW} in pom.xml from {FW_V} to 2.0.0" in why


# ------------------------------------------------------------------------------------------------ not checked
@pytest.mark.parametrize("source", sorted(PRE_TRY_SOURCES))
def test_pre_tries_are_not_checked_even_when_the_check_would_raise(cfg, tmp_path, monkeypatch, source):
    monkeypatch.setattr(BuildFixLoop, "_framework_guard_unsafe", _boom)
    ws, loop = _loop(cfg, tmp_path, fw_pom())
    assert loop._framework_guard(SimpleNamespace(id="p1", source=source), ws.head(), ["pom.xml"]) == ""


def test_a_full_scope_run_is_not_checked_even_when_the_check_would_raise(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(BuildFixLoop, "_framework_guard_unsafe", _boom)
    ws = _ws(tmp_path, {"pom.xml": fw_pom(), FOO: "class Foo { broken }\n"})
    loop = make_loop(fw_cfg(cfg), ws, FakeFixer(), pom_build())             # no reduce(): full scope
    assert loop._framework_guard(LLM_ATT, ws.head(), ["pom.xml"]) == ""


def test_a_full_scope_loop_keeps_the_attempt_when_the_check_would_raise(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(BuildFixLoop, "_framework_guard_unsafe", _boom)
    ws, _, _, pr = _guard_run(cfg, tmp_path, [_harmless_pom_edit()], reduced=False)
    assert pr.ok and pr.attempts[0].applied and COMMENT in ws.read("pom.xml")
    assert not any(e["event"] == "framework_guard" for e in journal_events(ws))
