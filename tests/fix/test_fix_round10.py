"""Round 10 (2026-10-26) fix-package items: A11 run_id / project in the LLM metadata; A12 LLM calls marked in flight
(the loop's watchdog stays silent meanwhile); A6 transport failures of the HTTP clients / adapters carry
`cause=<ExceptionClass>`; Windows backslash paths in context serving."""
from __future__ import annotations

from pathlib import PureWindowsPath

import httpx
import pytest

from sbmig.core.interfaces import ChatMessage, FixContext
from sbmig.core.models import Phase
from sbmig.core.workspace import Workspace
from sbmig.fix import FakeLLMClient, LLMFixer
from sbmig.fix.client import LLMError, OpenAICompatibleClient, RecordingClient, llm_calls_in_flight
from sbmig.llm import AdapterClient
from sbmig.loop.fixloop import is_llm_transport_error

from .conftest import make_cluster

A = "src/main/java/a/A.java"
MSGS = [ChatMessage(role="user", content="hi")]


def ctx(**kw):
    return FixContext(phase=Phase.SPRING, iteration=2, profile_summary="case=spring_boot java=17", **kw)


def test_metadata_carries_run_id_and_project(cfg, ws):
    ws.write(A, "package a;\n\npublic class A {}\n")
    client = FakeLLMClient()
    LLMFixer(cfg, client).propose(ws, make_cluster(file=A, cid="c-42"), ctx())
    meta = client.calls[0]["metadata"]
    assert meta == {"run_id": "t-run", "project": "project", "phase": "spring", "iteration": 2, "cluster_id": "c-42"}
    role, m = LLMFixer.call_tags(None, "c1", ws)
    assert role == "fix" and m == {"run_id": "t-run", "project": "project", "cluster_id": "c1"}
    assert LLMFixer.call_tags(None) == ("fix", {})


class _Probe:
    """A client that records whether a call was marked in flight while it ran."""

    def __init__(self):
        self.seen: list[int] = []

    def complete(self, messages, **kw):
        from sbmig.core.interfaces import LLMResponse
        self.seen.append(llm_calls_in_flight())
        return LLMResponse(content="<analysis>no fix</analysis><patches></patches>", model="m")


def test_llm_calls_are_marked_in_flight(cfg, ws):
    ws.write(A, "package a;\n\npublic class A {}\n")
    probe = _Probe()
    assert llm_calls_in_flight() == 0
    LLMFixer(cfg, probe).propose(ws, make_cluster(file=A), ctx())
    assert probe.seen and all(n >= 1 for n in probe.seen) and llm_calls_in_flight() == 0
    rec = RecordingClient(_Probe(), ws.run_dir / "rec")
    rec.complete(MSGS)
    assert rec.inner.seen == [1] and llm_calls_in_flight() == 0


def test_http_client_transport_failure_names_the_cause():
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    c = OpenAICompatibleClient("http://llm.local", "sk", "m", transport=httpx.MockTransport(handler),
                               sleep=lambda s: None, max_retries=1)
    with pytest.raises(LLMError) as ei:
        c.complete(MSGS)
    text = str(ei.value)
    assert "status=None, cause=ConnectTimeout" in text and llm_calls_in_flight() == 0
    assert is_llm_transport_error(f"llm-error: {text}") is True


def test_http_client_500_after_retries_has_no_cause():
    c = OpenAICompatibleClient("http://llm.local", "sk", "m", sleep=lambda s: None, max_retries=0,
                               transport=httpx.MockTransport(lambda r: httpx.Response(503, text="busy")))
    with pytest.raises(LLMError) as ei:
        c.complete(MSGS)
    assert "(status=503)" in str(ei.value) and "cause=" not in str(ei.value)


def test_adapter_failure_names_status_and_cause():
    class Boom:
        adapter_kind = "request"

        def complete(self, request):
            raise RuntimeError("gateway said no")

    with pytest.raises(LLMError) as ei:
        AdapterClient(Boom()).complete(MSGS)
    assert "(status=None, cause=RuntimeError)" in str(ei.value)
    assert is_llm_transport_error(f"llm-error: {ei.value}") is True


def test_context_serving_with_windows_paths(cfg, tmp_path, monkeypatch):
    root = tmp_path / "p"
    root.mkdir()
    ws = Workspace(root, tmp_path / "run", "t")
    ws.write("src/main/java/com/x/Foo.java", "class Foo {}\n")
    ws.write("src/main/resources/application.properties", "a=b\n")
    orig = Workspace.glob
    monkeypatch.setattr(Workspace, "glob", lambda self, pat: [str(PureWindowsPath(p)) for p in orig(self, pat)])
    fixer = LLMFixer(cfg, FakeLLMClient())
    assert fixer.resolve_path(ws, "com.x.Foo") == ("src/main/java/com/x/Foo.java", [])
    assert fixer.resolve_path(ws, "Foo.java") == ("src/main/java/com/x/Foo.java", [])
    assert LLMFixer._resource_files(ws) == ["src/main/resources/application.properties"]
    assert "src/main/java/com/x/Foo.java:1" in fixer.grep(ws, ["class Foo"])
