from types import SimpleNamespace

import pytest

from sbmig.core.interfaces import ChatMessage, LLMClientLike, LLMRequest, LLMResponse
from sbmig.fix.client import LLMError
from sbmig.llm import AdapterClient, LLMAdapter, is_request_adapter

MSGS = [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="u")]


class Capture(LLMAdapter):
    def __init__(self, answer=None, exc=None):
        self.answer, self.exc = answer, exc
        self.requests: list[LLMRequest] = []

    def complete(self, request):
        self.requests.append(request)
        if self.exc:
            raise self.exc
        return self.answer if self.answer is not None else LLMResponse(content="ok", model="")


def test_builds_request_with_defaults_and_merged_metadata():
    a = Capture()
    c = AdapterClient(a, default_model="m-default", base_metadata={"run_id": "r1", "phase": "x"})
    assert isinstance(c, LLMClientLike) and c.model == "m-default"
    resp = c.complete(MSGS, max_tokens=10, temperature=0.0, stop=["<x>"], metadata={"phase": "spring"})
    req = a.requests[0]
    assert req.role == "other" and req.model == "m-default" and req.max_tokens == 10 and req.stop == ["<x>"]
    assert req.metadata == {"run_id": "r1", "phase": "spring"} and req.messages == MSGS
    assert resp.content == "ok" and resp.model == "m-default"           # empty model filled
    c.complete(MSGS, model="override", role="fix", unknown_future_kw=1)
    assert a.requests[1].model == "override" and a.requests[1].role == "fix"
    c.complete(MSGS, role="something_new")
    assert a.requests[2].role == "other" and a.requests[2].metadata["role_detail"] == "something_new"


def test_no_default_model_leaves_none_and_falls_back_to_name():
    a = Capture()
    resp = AdapterClient(a).complete(MSGS)
    assert a.requests[0].model is None and resp.model == "Capture"


@pytest.mark.parametrize("answer,content,model", [
    ({"content": "d", "input_tokens": 3}, "d", "m"),
    ({"content": "d", "model": "gw"}, "d", "gw"),
    ("plain text", "plain text", "m"),
    (SimpleNamespace(content="obj", model="o", input_tokens=1, output_tokens=2), "obj", "o"),
])
def test_coerces_responses(answer, content, model):
    resp = AdapterClient(Capture(answer), default_model="m").complete(MSGS)
    assert isinstance(resp, LLMResponse) and resp.content == content and resp.model == model


@pytest.mark.parametrize("garbage", [42, {"text": "no content key"}, {"content": 5}, SimpleNamespace(x=1)])
def test_garbage_raises_llm_error(garbage):
    with pytest.raises(LLMError, match="adapter Capture"):
        AdapterClient(Capture(garbage)).complete(MSGS)


def test_adapter_exceptions_become_llm_error():
    exc = RuntimeError("gateway down")
    exc.status = 503
    with pytest.raises(LLMError) as ei:
        AdapterClient(Capture(exc=exc)).complete(MSGS)
    assert ei.value.status == 503 and "gateway down" in str(ei.value)
    with pytest.raises(LLMError) as ei2:
        AdapterClient(Capture(exc=LLMError(429, "x"))).complete(MSGS)
    assert ei2.value.status == 429


def test_is_request_adapter_detection():
    class ByName:
        def complete(self, request): ...

    class ByAnnotation:
        def complete(self, r: LLMRequest): ...

    class Client:
        def complete(self, messages, **kw): ...

    class Marked:
        adapter_kind = "client"

        def complete(self, request): ...

    assert is_request_adapter(Capture()) and is_request_adapter(ByName()) and is_request_adapter(ByAnnotation())
    assert not is_request_adapter(Client()) and not is_request_adapter(Marked())


def test_rejects_object_without_complete():
    with pytest.raises(TypeError):
        AdapterClient(object())
