"""LangChain tier: ChatPostModel (gateway as a BaseChatModel), LangChainClient (LLMClientLike), provider langchain."""
import json

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import SecretStr

from sbmig.core.interfaces import ChatMessage, LLMResponse
from sbmig.fix import LLMConfigError, LLMError, RecordingClient, make_llm_client
from sbmig.fix.client import _HTTPClient
from sbmig.llm import ChatPostModel, LangChainClient, PluginLoadError, resolve_langchain_model
from tests.llm import plugins_fixture as pf

URL = "https://gw.example.internal/api/v2/chat?tenant=x"
MSGS = [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hello"),
        ChatMessage(role="assistant", content="a1"), ChatMessage(role="user", content="u2")]
NESTED = {"data": {"result": {"content": "pong"}}, "usage": {"prompt_tokens": 12, "completion_tokens": 3}}


class Gateway:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


def model(gw: Gateway, **kw) -> ChatPostModel:
    kw.setdefault("payload_extra", {"need_orgin": True})
    return ChatPostModel(url=URL, api_key=SecretStr("tok-123"), model="gpt-6-luna",
                         transport=httpx.MockTransport(gw), max_retries=0, **kw)


# --------------------------------------------------------------------------- ChatPostModel
def test_chat_post_model_request_is_the_gateway_contract():
    gw = Gateway(NESTED)
    m = model(gw, response_path="data.result.content")
    out = m.invoke([SystemMessage(content="be brief"), HumanMessage(content="ping")])
    req = gw.requests[0]
    assert req.method == "POST" and str(req.url) == URL                     # url untouched
    assert req.headers["authorization"] == "Bearer tok-123"
    assert req.headers["content-type"] == "application/json"
    body = gw.body()
    assert set(body) == {"model", "messages", "need_orgin"}
    assert body == {"model": "gpt-6-luna", "need_orgin": True,
                    "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "ping"}]}
    assert isinstance(out, AIMessage) and out.content == "pong"


def test_chat_post_model_is_a_langchain_model_and_reports_usage():
    gw = Gateway({"model": "gpt-6-luna-0910", "choices": [{"message": {"content": "hey"}, "finish_reason": "stop"}],
                  "usage": {"input_tokens": 4, "output_tokens": 2}})
    m = model(gw)
    assert isinstance(m, BaseChatModel) and m._llm_type == "sbmig-chat-post"
    out = m.invoke("hi")                                                      # plain string input
    assert gw.body()["messages"] == [{"role": "user", "content": "hi"}]
    assert out.content == "hey"                                               # auto-detected reply path
    assert out.usage_metadata == {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}
    assert out.response_metadata["model"] == "gpt-6-luna-0910"
    assert out.response_metadata["finish_reason"] == "stop"


def test_chat_post_model_response_path_and_auto_detect():
    assert model(Gateway(NESTED), response_path="data.result.content").invoke("x").content == "pong"
    assert model(Gateway({"answer": "auto"})).invoke("x").content == "auto"
    with pytest.raises(LLMError, match="response_path"):
        model(Gateway(NESTED), response_path="data.nope").invoke("x")
    with pytest.raises(LLMError, match="cannot find the reply"):
        model(Gateway({"weird": 1})).invoke("x")


def test_chat_post_model_generation_params_only_when_enabled():
    gw = Gateway({"content": "ok"})
    m = model(gw)
    m.invoke("x", stop=["END"], max_tokens=5, temperature=0.3)
    assert set(gw.body()) == {"model", "messages", "need_orgin"}
    gw2 = Gateway({"content": "ok"})
    model(gw2, generation_params=True).invoke("x", stop=["END"], max_tokens=5, temperature=0.3)
    b = gw2.body()
    assert b["stop"] == ["END"] and b["max_tokens"] == 5 and b["temperature"] == 0.3 and b["need_orgin"] is True


def test_chat_post_model_http_errors_and_retries():
    gw = Gateway(httpx.Response(400, text="bad request"))
    with pytest.raises(LLMError) as ei:
        model(gw).invoke("x")
    assert ei.value.status == 400 and len(gw.requests) == 1
    gw = Gateway(httpx.Response(503, headers={"retry-after": "0"}), {"content": "second"})
    m = ChatPostModel(url=URL, api_key=SecretStr("k"), model="m", transport=httpx.MockTransport(gw), max_retries=1)
    assert m.invoke("x").content == "second" and len(gw.requests) == 2


def test_chat_post_model_never_shows_the_key():
    m = model(Gateway({"content": "x"}))
    for text in (repr(m), str(m), json.dumps(m._identifying_params), str(m.model_dump()), str(m.asdict())):
        assert "tok-123" not in text
    assert m.api_key.get_secret_value() == "tok-123"


# --------------------------------------------------------------------------- LangChainClient
def fake(**kw) -> pf.FakeChatModel:
    return pf.FakeChatModel(calls=[], configs=[], **kw)


def test_langchain_client_complete_maps_messages_role_metadata():
    fm = fake()
    c = LangChainClient(fm, default_model="gpt-6-luna")
    resp = c.complete(MSGS, role="fix", metadata={"cluster_id": "c1", "phase": "boot4"}, stop=["</patches>"])
    assert isinstance(resp, LLMResponse)
    assert resp.content == "lc-reply" and resp.model == "fake-lc-1"
    assert resp.input_tokens == 5 and resp.output_tokens == 2 and resp.finish_reason == "stop"
    call = fm.calls[0]
    assert [type(m) for m in call["messages"]] == [SystemMessage, HumanMessage, AIMessage, HumanMessage]
    assert [m.content for m in call["messages"]] == ["sys", "hello", "a1", "u2"]
    assert call["stop"] == ["</patches>"]
    config = fm.configs[0]
    assert config["tags"] == ["fix"]
    assert {"cluster_id": "c1", "phase": "boot4"}.items() <= config["metadata"].items()
    assert c.model == "gpt-6-luna" and not isinstance(c, _HTTPClient)


def test_langchain_client_defaults_and_generation_kwargs():
    fm = fake()
    c = LangChainClient(fm)
    assert c.model == "fake-lc"                                   # model_name of the chat model
    c.complete(MSGS)
    assert fm.configs[0]["tags"] == ["other"] and fm.calls[0]["kwargs"] == {}
    c.complete(MSGS, model="bigger", max_tokens=100, temperature=0.0)
    assert fm.calls[1]["kwargs"] == {"model": "bigger", "max_tokens": 100, "temperature": 0.0}


def test_langchain_client_retries_without_kwargs_on_type_error():
    fm = fake(strict_kwargs=True)
    resp = LangChainClient(fm).complete(MSGS, max_tokens=10, role="plan_advisor")
    assert resp.content == "lc-reply" and fm.calls[-1]["kwargs"] == {}


def test_langchain_client_content_blocks_and_missing_usage():
    class Bare(BaseChatModel):
        @property
        def _llm_type(self):
            return "bare"

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            from langchain_core.outputs import ChatGeneration, ChatResult
            msg = AIMessage(content=[{"type": "text", "text": "a"}, {"type": "reasoning", "reasoning": "z"},
                                     {"type": "text", "text": "b"}])
            return ChatResult(generations=[ChatGeneration(message=msg)])

    resp = LangChainClient(Bare(), default_model="dm").complete(MSGS)
    assert resp.content == "ab" and resp.input_tokens == 0 and resp.output_tokens == 0 and resp.model == "dm"


def test_langchain_client_maps_errors_to_llm_error():
    class Boom(BaseChatModel):
        @property
        def _llm_type(self):
            return "boom"

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            e = RuntimeError("gateway down")
            e.status_code = 502
            raise e

    with pytest.raises(LLMError) as ei:
        LangChainClient(Boom()).complete(MSGS)
    assert ei.value.status == 502 and "gateway down" in str(ei.value)
    with pytest.raises(LLMError) as ei:                           # LLMError from ChatPostModel passes through
        LangChainClient(model(Gateway(httpx.Response(401, text="no")))).complete(MSGS)
    assert ei.value.status == 401


def test_langchain_client_over_chat_post_model_end_to_end():
    gw = Gateway(NESTED)
    c = LangChainClient(model(gw, response_path="data.result.content"), default_model="gpt-6-luna")
    resp = c.complete(MSGS, role="fix", metadata={"cluster_id": "c1"}, max_tokens=8000, temperature=0.0)
    assert resp == LLMResponse(content="pong", model="gpt-6-luna", input_tokens=12, output_tokens=3)
    assert gw.body() == {"model": "gpt-6-luna", "need_orgin": True,
                         "messages": [m.model_dump() for m in MSGS]}
    c.complete(MSGS, model="gpt-6-luna-pro")                     # role-specific model override reaches the payload
    assert gw.body()["model"] == "gpt-6-luna-pro"
    c.close()


def test_recording_client_wraps_langchain_client(tmp_path):
    rc = RecordingClient(LangChainClient(fake()), tmp_path / "rec")
    assert rc.model == "fake-lc"
    assert rc.complete(MSGS, role="recipe_candidate", metadata={"cluster_id": "c9"}).content == "lc-reply"
    (rec,) = [json.loads(p.read_text()) for p in sorted((tmp_path / "rec").glob("llm-*.json"))]
    assert rec["response"] == "lc-reply" and rec["role"] == "recipe_candidate"
    assert rec["usage"]["input_tokens"] == 5 and rec["metadata"] == {"cluster_id": "c9"}


# --------------------------------------------------------------------------- make_llm_client(provider: langchain)
def _lc_cfg(cfg, **llm):
    cfg.llm.provider = "langchain"
    for k, v in llm.items():
        setattr(cfg.llm, k, v)
    return cfg


def test_make_llm_client_langchain_factory(cfg, monkeypatch, tmp_path):
    monkeypatch.delenv(cfg.llm.api_key_env, raising=False)       # a factory needs no LLM_API_KEY
    _lc_cfg(cfg, model="gpt-6-luna", langchain_model="tests.llm.plugins_fixture:make_lc_model")
    c = make_llm_client(cfg)
    assert isinstance(c, LangChainClient) and isinstance(c.chat_model, pf.FakeChatModel)
    assert c.model == "gpt-6-luna" and c.chat_model is pf.LC_MODELS[-1]
    assert c.complete(MSGS, role="fix").content == "from-factory:gpt-6-luna"

    cfg.llm.langchain_model = "tests.llm.plugins_fixture:make_lc_model_noarg"
    assert make_llm_client(cfg).complete(MSGS).content == "noarg"
    cfg.llm.langchain_model = "tests.llm.plugins_fixture:LC_INSTANCE"
    assert make_llm_client(cfg).chat_model is pf.LC_INSTANCE
    cfg.llm.langchain_model = "tests.llm.plugins_fixture:FakeChatModel"   # a class: built with no args
    assert isinstance(make_llm_client(cfg).chat_model, pf.FakeChatModel)
    cfg.llm.langchain_model = "tests.llm.plugins_fixture:nope"
    with pytest.raises(PluginLoadError, match="no attribute"):
        make_llm_client(cfg)

    cfg.llm.langchain_model = "tests.llm.plugins_fixture:make_lc_model"
    cfg.llm.record_dir = str(tmp_path / "rec")
    rc = make_llm_client(cfg)
    assert isinstance(rc, RecordingClient) and isinstance(rc.inner, LangChainClient)
    rc.complete(MSGS, role="fix")
    assert len(list((tmp_path / "rec").glob("llm-*.json"))) == 1


def test_make_llm_client_langchain_chat_post_model(cfg, monkeypatch):
    _lc_cfg(cfg, base_url=URL, model="gpt-6-luna", api_key_env="SBMIG_LC_KEY", payload_extra={"need_orgin": True},
            response_path="data.result.content", langchain_model=None)
    monkeypatch.delenv("SBMIG_LC_KEY", raising=False)
    with pytest.raises(LLMConfigError, match="SBMIG_LC_KEY"):
        make_llm_client(cfg)
    monkeypatch.setenv("SBMIG_LC_KEY", "k-lc")
    gw = Gateway(NESTED)
    c = make_llm_client(cfg, transport=httpx.MockTransport(gw))
    assert isinstance(c, LangChainClient) and isinstance(c.chat_model, ChatPostModel)
    assert not isinstance(getattr(c, "inner", c), _HTTPClient)    # llm-probe takes the generic complete() path
    assert c.complete(MSGS, role="fix").content == "pong"
    assert gw.requests[0].headers["authorization"] == "Bearer k-lc"
    assert set(gw.body()) == {"model", "messages", "need_orgin"}
    assert "k-lc" not in repr(c)

    cfg.llm.base_url = "${LLM_BASE_URL}"
    with pytest.raises(LLMConfigError, match="base_url"):
        resolve_langchain_model(cfg)
    cfg.llm.base_url = URL
    cfg.llm.model = "${LLM_MODEL}"
    with pytest.raises(LLMConfigError, match="model"):
        resolve_langchain_model(cfg)


def test_chat_post_model_passes_backend_and_verify_to_the_http_client(monkeypatch):
    import requests

    from sbmig.llm.langchain import ChatPostModel
    from pydantic import SecretStr

    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: type("R", (), {
        "status_code": 200, "headers": {}, "text": "{}", "json": lambda s: {"content": "ok"}})())
    m = ChatPostModel(url="http://gw/chat", api_key=SecretStr("k"), model="m", http_backend="requests",
                      verify_ssl=False)
    c = m._http_client()
    assert c.http_backend == "requests" and c.verify is False and c._session.verify is False
    assert m.invoke("hi").content == "ok"
