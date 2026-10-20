"""Plugin objects referenced by tests via 'tests.llm.plugins_fixture:<attr>'."""
from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from sbmig.core.interfaces import LLMRequest, LLMResponse
from sbmig.core.models import FileChange, FixProposal
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


class ProposeOnly:
    def __init__(self, options, cfg, llm):
        self.options, self.cfg, self.llm = options, cfg, llm

    def propose(self, ws, cluster, ctx):
        return FixProposal(changes=[FileChange(path="A.txt", op="modify", search="old", replace="new")],
                           model="agent")


def make_propose_only(options, cfg, llm=None, notes_dir=None):
    f = ProposeOnly(options, cfg, llm)
    f.notes_dir = notes_dir
    return f


def returns_junk(options):
    return object()


# --------------------------------------------------------------------------- LangChain tier (provider: langchain)
class FakeChatModel(BaseChatModel):
    """Records what LangChainClient hands to a LangChain chat model; answers `reply`."""
    reply: Any = "lc-reply"
    model_name: str = "fake-lc"
    calls: list = Field(default_factory=list)
    configs: list = Field(default_factory=list)
    strict_kwargs: bool = False            # True → _generate raises TypeError on unknown kwargs

    @property
    def _llm_type(self) -> str:
        return "fake-lc"

    def invoke(self, input, config=None, *, stop=None, **kwargs):
        self.configs.append(config)
        return super().invoke(input, config, stop=stop, **kwargs)

    def _generate(self, messages, stop: list | None = None, run_manager=None, **kwargs) -> ChatResult:
        if self.strict_kwargs and kwargs:
            raise TypeError(f"unexpected kwargs {sorted(kwargs)}")
        self.calls.append({"messages": list(messages), "stop": stop, "kwargs": dict(kwargs)})
        msg = AIMessage(content=self.reply,
                        usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
                        response_metadata={"model_name": "fake-lc-1", "finish_reason": "stop"})
        return ChatResult(generations=[ChatGeneration(message=msg)])


LC_MODELS: list = []


def make_lc_model(cfg):
    m = FakeChatModel(calls=[], configs=[], reply=f"from-factory:{cfg.llm.model}")
    LC_MODELS.append(m)
    return m


def make_lc_model_noarg():
    return FakeChatModel(calls=[], configs=[], reply="noarg")


LC_INSTANCE = FakeChatModel(calls=[], configs=[], reply="instance")
