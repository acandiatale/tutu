"""A4 (v2.1h): the plugin edit applier gets ``errors`` only when its signature takes it."""
from __future__ import annotations

from sbmig.loop.fixloop import call_edit_applier


def test_applier_without_errors_is_called_without_it():
    seen = {}

    def fn(ws, edits, *, cfg):
        seen.update(ws=ws, edits=edits, cfg=cfg)
        return "ok"

    assert call_edit_applier(fn, "WS", ["e1"], "CFG", ["err"]) == "ok"
    assert seen == {"ws": "WS", "edits": ["e1"], "cfg": "CFG"}


def test_applier_with_errors_receives_the_case_errors():
    seen = {}

    def fn(ws, edits, *, cfg, errors=None):
        seen["errors"] = errors
        return "ok"

    call_edit_applier(fn, "WS", [], "CFG", ["err1", "err2"])
    assert seen["errors"] == ["err1", "err2"]


def test_applier_with_kwargs_receives_errors():
    seen = {}

    def fn(ws, edits, **kw):
        seen.update(kw)
        return "ok"

    call_edit_applier(fn, "WS", [], "CFG", ["err"])
    assert seen == {"cfg": "CFG", "errors": ["err"]}


def test_callable_without_inspectable_signature_gets_errors():
    seen = {}

    class Odd:
        __signature__ = property(lambda self: (_ for _ in ()).throw(ValueError("no signature")))

        def __call__(self, ws, edits, *, cfg, errors=None):
            seen["errors"] = errors
            return "ok"

    call_edit_applier(Odd(), "WS", [], "CFG", ["err"])
    assert seen["errors"] == ["err"]
