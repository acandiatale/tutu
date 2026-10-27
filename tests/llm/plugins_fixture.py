"""Plugin objects referenced by tests via 'tests.llm.plugins_fixture:<attr>'."""
from __future__ import annotations

from typing import Any

from pydantic import Field

from sbmig.core.interfaces import LLMRequest, LLMResponse
from sbmig.core.models import ScriptProposal
from sbmig.llm import LLMAdapter

SEEN: list = []


class EchoAdapter(LLMAdapter):
    def __init__(self, options: dict, cfg=None):
        self.options = options
        self.cfg = cfg
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        SEEN.append(request)
        return LLMResponse(content="echo:" + request.messages[-1].content, model=request.model or "")


class NoArgAdapter(LLMAdapter):
    def complete(self, request):
        return {"content": "noarg"}


def factory_options_only(options):
    a = EchoAdapter(options)
    return a


def factory_none():
    return EchoAdapter({"from": "none"})


def factory_two(options, cfg):
    return EchoAdapter(options, cfg)


class DuckAdapter:                         # no base class, detected by the parameter name
    def complete(self, request):
        return LLMResponse(content="duck", model="d")


class MarkedAdapter:
    adapter_kind = "request"

    def complete(self, req):
        return LLMResponse(content="marked", model="m")


class MessageClient:                        # client tier without role/metadata support
    model = "mc"

    def __init__(self):
        self.kw: list[dict] = []

    def complete(self, messages, *, model=None, max_tokens=None, temperature=None, stop=None):
        self.kw.append({"model": model})
        return LLMResponse(content="client", model=model or self.model)


INSTANCE = EchoAdapter({"instance": True})


def needs_three(a, b, c):
    return EchoAdapter({})


def broken_factory(options, cfg):
    raise RuntimeError("missing base_url")


SCRIPT = "def fix(p):\n    p.replace('A.txt', 'old', 'new')\n"


class ScriptAgent:
    """A tier-2 fixer on the script seam: one fix script per case (the script applies itself)."""

    def __init__(self, options, cfg, llm):
        self.options, self.cfg, self.llm = options, cfg, llm
        self.seen: list = []

    def propose_script(self, ws, cluster, ctx):
        self.seen.append((cluster.id, ctx.script_mode))
        return ScriptProposal(code=SCRIPT, analysis="agent script", model="agent")


def make_script_agent(options, cfg, llm=None, notes_dir=None):
    f = ScriptAgent(options, cfg, llm)
    f.notes_dir = notes_dir
    return f


class ProposeOnly:
    """A SEARCH/REPLACE-era fixer (`propose` / `apply` only): refused by the script seam."""

    def __init__(self, options=None, cfg=None, llm=None):
        self.options = options

    def propose(self, ws, cluster, ctx):  # pragma: no cover - never called
        raise AssertionError("v1 propose must not be called")

    def apply(self, ws, changes):  # pragma: no cover - never called
        raise AssertionError("v1 apply must not be called")


def returns_junk(options):
    return object()
