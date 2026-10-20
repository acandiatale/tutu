import json
import logging

import httpx
import pytest

from sbmig.core.interfaces import ChatMessage, LLMClientLike, LLMResponse
from sbmig.fix import (
    AnthropicClient,
    FakeLLMClient,
    LLMConfigError,
    LLMError,
    OpenAICompatibleClient,
    RecordingClient,
    load_replay_responses,
    make_llm_client,
    usage_from,
)
from sbmig.fix.client import NO_FIX_RESPONSE, _retry_after_seconds

MSGS = [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hello"),
        ChatMessage(role="assistant", content="a1"), ChatMessage(role="user", content="u2")]

OPENAI_OK = {"model": "m-1", "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 11, "completion_tokens": 7}}
ANTH_OK = {"model": "claude-opus-5-5", "stop_reason": "end_turn",
           "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
           "usage": {"input_tokens": 20, "output_tokens": 3}}


class Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


def openai(handler, **kw):
    return OpenAICompatibleClient(kw.pop("base_url", "http://llm.local"), "sk-secret", "m-1",
                                  transport=httpx.MockTransport(handler), sleep=kw.pop("sleep", lambda s: None), **kw)


def test_openai_request_shape_and_parse():
    rec = Recorder([httpx.Response(200, json=OPENAI_OK)])
    c = openai(rec, extra_headers={"X-Team": "mig"})
    resp = c.complete(MSGS, max_tokens=100, temperature=0.2, stop=["</patches>"])
    assert isinstance(c, LLMClientLike)
    req = rec.requests[0]
    assert str(req.url) == "http://llm.local/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-secret"
    assert req.headers["x-team"] == "mig"
    body = json.loads(req.content)
    assert body == {"model": "m-1", "messages": [m.model_dump() for m in MSGS], "max_tokens": 100,
                    "temperature": 0.2, "stop": ["</patches>"]}
    assert resp == LLMResponse(content="hi", model="m-1", input_tokens=11, output_tokens=7, finish_reason="stop")
    assert usage_from(resp).model_dump() == {"model": "m-1", "calls": 1, "input_tokens": 11, "output_tokens": 7}
    assert "sk-secret" not in repr(c)


@pytest.mark.parametrize("base,url", [
    ("http://h/v1", "http://h/v1/chat/completions"),
    ("http://h/v1/", "http://h/v1/chat/completions"),
    ("http://h/openai/v2", "http://h/openai/v2/chat/completions"),
    ("http://h/x/chat/completions", "http://h/x/chat/completions"),
    ("http://h", "http://h/v1/chat/completions"),
])
def test_openai_base_url_variants(base, url):
    rec = Recorder([httpx.Response(200, json=OPENAI_OK)])
    openai(rec, base_url=base).complete(MSGS)
    assert str(rec.requests[0].url) == url


def test_openai_defaults_and_content_parts():
    data = {"choices": [{"message": {"content": [{"type": "text", "text": "x"}, {"type": "text", "text": "y"}]}}]}
    rec = Recorder([httpx.Response(200, json=data)])
    c = openai(rec, default_max_tokens=55, default_temperature=None)
    r = c.complete(MSGS)
    body = json.loads(rec.requests[0].content)
    assert body["max_tokens"] == 55 and "temperature" not in body and "stop" not in body
    assert r.content == "xy" and r.input_tokens == 0 and r.model == "m-1"


def test_openai_no_choices_and_bad_json():
    with pytest.raises(LLMError):
        openai(Recorder([httpx.Response(200, json={"choices": []})])).complete(MSGS)
    with pytest.raises(LLMError):
        openai(Recorder([httpx.Response(200, content=b"not json")])).complete(MSGS)


def test_anthropic_request_shape_and_parse():
    rec = Recorder([httpx.Response(200, json=ANTH_OK)])
    c = AnthropicClient("https://api.anthropic.com/v1", "ak", "claude-opus-5-5",
                        transport=httpx.MockTransport(rec), sleep=lambda s: None)
    resp = c.complete(MSGS + [ChatMessage(role="user", content="u3")], max_tokens=500, temperature=0.0, stop=["X"])
    req = rec.requests[0]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "ak" and req.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in req.headers
    body = json.loads(req.content)
    assert body["system"] == "sys" and body["max_tokens"] == 500 and body["model"] == "claude-opus-5-5"
    assert body["messages"] == [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "a1"},
                                {"role": "user", "content": "u2\n\nu3"}]
    assert "temperature" not in body                    # current Claude models reject sampling params
    assert body["stop_sequences"] == ["X"]
    assert resp.content == "ab" and resp.input_tokens == 20 and resp.output_tokens == 3
    assert resp.finish_reason == "end_turn"


def test_anthropic_temperature_for_older_models_and_defaults():
    rec = Recorder([httpx.Response(200, json=ANTH_OK)])
    c = AnthropicClient(api_key="k", model="", transport=httpx.MockTransport(rec))
    assert c.model == "claude-opus-5-5"
    c.complete([ChatMessage(role="user", content="x")], model="claude-sonnet-4-5", temperature=0.3)
    body = json.loads(rec.requests[0].content)
    assert body["temperature"] == 0.3 and "system" not in body
    assert str(rec.requests[0].url) == "https://api.anthropic.com/v1/messages"
    assert AnthropicClient.accepts_temperature("claude-haiku-4-5")
    assert not AnthropicClient.accepts_temperature("claude-sonnet-5-5")
    assert not AnthropicClient.accepts_temperature("claude-fable-5-1")


def test_retry_on_429_with_retry_after(caplog):
    sleeps = []
    rec = Recorder([httpx.Response(429, headers={"Retry-After": "2"}, text="slow down"),
                    httpx.Response(503, text="busy"),
                    httpx.Response(200, json=OPENAI_OK)])
    c = openai(rec, max_retries=3, sleep=sleeps.append)
    with caplog.at_level(logging.INFO, logger="sbmig.fix.client"):
        r = c.complete(MSGS)
    assert r.content == "hi" and len(rec.requests) == 3
    assert sleeps[0] == 2.0 and 0 < sleeps[1] <= 30
    info = [rec_.getMessage() for rec_ in caplog.records if rec_.levelno == logging.INFO]
    assert any("model=m-1" in m and "in=11" in m and "latency=" in m for m in info)
    assert not any("sk-secret" in rec_.getMessage() or "hello" in rec_.getMessage() for rec_ in caplog.records)


def test_give_up_after_max_retries():
    sleeps = []
    rec = Recorder([httpx.Response(500, text="boom " * 300)])
    c = openai(rec, max_retries=2, sleep=sleeps.append)
    with pytest.raises(LLMError) as ei:
        c.complete(MSGS)
    assert ei.value.status == 500 and len(rec.requests) == 3 and len(sleeps) == 2
    assert len(ei.value.body_excerpt) <= 500


def test_non_retryable_status_raises_immediately():
    rec = Recorder([httpx.Response(400, text="bad request")])
    with pytest.raises(LLMError) as ei:
        openai(rec, max_retries=3).complete(MSGS)
    assert ei.value.status == 400 and len(rec.requests) == 1


def test_transport_errors_are_retried():
    rec = Recorder([httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.Response(200, json=OPENAI_OK)])
    assert openai(rec, max_retries=2).complete(MSGS).content == "hi"
    rec = Recorder([httpx.ConnectError("refused")])
    with pytest.raises(LLMError) as ei:
        openai(rec, max_retries=1).complete(MSGS)
    assert ei.value.status is None and "ConnectError" in ei.value.body_excerpt


def test_retry_after_parsing():
    assert _retry_after_seconds(httpx.Response(429, headers={"retry-after-ms": "1500"})) == 1.5
    assert _retry_after_seconds(httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"})) == 0.0
    assert _retry_after_seconds(httpx.Response(429, headers={"retry-after": "soon"})) is None
    assert _retry_after_seconds(httpx.Response(429)) is None


def test_fake_client_script_and_calls(tmp_path):
    f = FakeLLMClient(["one", "two"], record_dir=tmp_path / "rec")
    assert [f.complete(MSGS).content for _ in range(3)] == ["one", "two", "two"]
    assert len(f.calls) == 3 and f.calls[0]["messages"] == MSGS
    assert len(list((tmp_path / "rec").glob("llm-*.json"))) == 3
    assert FakeLLMClient().complete(MSGS).content == NO_FIX_RESPONSE
    assert FakeLLMClient([]).complete(MSGS).content == NO_FIX_RESPONSE
    assert FakeLLMClient(lambda m: m[-1].content.upper()).complete(MSGS, model="x").content == "U2"
    with pytest.raises(LLMError):
        FakeLLMClient([LLMError(503, "x")]).complete(MSGS)


def test_recording_client_and_replay(tmp_path, ws):
    rc = RecordingClient(FakeLLMClient(["r1", "r2"]), tmp_path / "rec", ws=ws)
    rc.complete(MSGS)
    rc.complete(MSGS, model="m2")
    files = sorted((tmp_path / "rec").glob("llm-*.json"))
    assert [p.name for p in files] == ["llm-0001.json", "llm-0002.json"]
    data = json.loads(files[0].read_text())
    assert set(data) >= {"messages", "response", "model", "usage", "latency"}
    assert data["response"] == "r1" and data["usage"]["calls"] == 1
    assert load_replay_responses(tmp_path / "rec") == ["r1", "r2"]
    assert "llm_call" in ws.journal_path.read_text()
    bad = RecordingClient(FakeLLMClient([LLMError(500, "x")]), tmp_path / "rec")
    with pytest.raises(LLMError):
        bad.complete(MSGS)
    assert json.loads(sorted((tmp_path / "rec").glob("llm-*.json"))[-1].read_text())["error"]
    assert rc.record_dir == tmp_path / "rec"


def test_make_llm_client_provider_switch(cfg, monkeypatch, tmp_path):
    assert isinstance(make_llm_client(cfg), FakeLLMClient)

    cfg.llm.provider = "openai_compatible"
    cfg.llm.base_url = "http://company-llm/v1"
    cfg.llm.model = "gpt-company"
    cfg.llm.api_key_env = "SBMIG_TEST_KEY"
    monkeypatch.delenv("SBMIG_TEST_KEY", raising=False)
    with pytest.raises(LLMConfigError, match="SBMIG_TEST_KEY"):
        make_llm_client(cfg)
    monkeypatch.setenv("SBMIG_TEST_KEY", "k")
    c = make_llm_client(cfg)
    assert isinstance(c, OpenAICompatibleClient) and c.model == "gpt-company"

    cfg.llm.base_url = "${LLM_BASE_URL}"
    with pytest.raises(LLMConfigError, match="base_url"):
        make_llm_client(cfg)
    cfg.llm.base_url = "http://x"
    cfg.llm.model = "${LLM_MODEL}"
    with pytest.raises(LLMConfigError, match="model"):
        make_llm_client(cfg)

    cfg.llm.provider = "anthropic"
    cfg.llm.base_url = "http://localhost:8000/v1"
    cfg.llm.model = "default"
    a = make_llm_client(cfg)
    assert isinstance(a, AnthropicClient) and a.model == "claude-opus-5-5" and a.base_url == "https://api.anthropic.com"

    cfg.llm.record_dir = str(tmp_path / "rec")
    assert isinstance(make_llm_client(cfg), RecordingClient)


def test_make_llm_client_provider_langchain(cfg, monkeypatch, tmp_path):
    from sbmig.fix.client import _HTTPClient
    from sbmig.llm import ChatPostModel, LangChainClient

    cfg.llm.provider = "langchain"
    cfg.llm.base_url = "http://gw/chat"
    cfg.llm.model = "gpt-company"
    cfg.llm.api_key_env = "SBMIG_TEST_KEY"
    monkeypatch.delenv("SBMIG_TEST_KEY", raising=False)
    with pytest.raises(LLMConfigError, match="SBMIG_TEST_KEY"):
        make_llm_client(cfg)
    monkeypatch.setenv("SBMIG_TEST_KEY", "k")
    rec = Recorder([httpx.Response(200, json={"content": "hi"})])
    c = make_llm_client(cfg, transport=httpx.MockTransport(rec))
    assert isinstance(c, LangChainClient) and isinstance(c.chat_model, ChatPostModel)
    assert not isinstance(c, _HTTPClient) and c.model == "gpt-company"
    assert c.complete(MSGS, role="fix").content == "hi"
    assert str(rec.requests[0].url) == "http://gw/chat"

    cfg.llm.base_url = "${LLM_BASE_URL}"
    with pytest.raises(LLMConfigError, match="base_url"):
        make_llm_client(cfg)

    monkeypatch.delenv("SBMIG_TEST_KEY", raising=False)          # a langchain_model factory needs no key
    cfg.llm.langchain_model = "tests.llm.plugins_fixture:make_lc_model"
    cfg.llm.record_dir = str(tmp_path / "rec")
    rc = make_llm_client(cfg)
    assert isinstance(rc, RecordingClient) and isinstance(rc.inner, LangChainClient)
    assert rc.complete(MSGS).content == "from-factory:gpt-company"


def test_make_llm_client_fake_replay(cfg, tmp_path):
    rc = RecordingClient(FakeLLMClient(["recorded"]), tmp_path / "rec")
    rc.complete(MSGS)
    cfg.llm.base_url = f"replay:{tmp_path / 'rec'}"
    c = make_llm_client(cfg)
    assert c.complete(MSGS).content == "recorded"


def test_clients_accept_role_and_metadata(tmp_path, ws):
    rec = Recorder([httpx.Response(200, json=OPENAI_OK)])
    assert openai(rec).complete(MSGS, role="fix", metadata={"cluster_id": "c1"}, future_kw=1).content == "hi"
    assert "role" not in json.loads(rec.requests[0].content)          # never sent to the HTTP provider

    f = FakeLLMClient(["x"])
    f.complete(MSGS, role="plan_advisor", metadata={"phase": "plan"})
    assert f.calls[0]["role"] == "plan_advisor" and f.calls[0]["metadata"] == {"phase": "plan"}

    rc = RecordingClient(FakeLLMClient(["r"]), tmp_path / "rec", ws=ws)
    rc.complete(MSGS, role="recipe_candidate", metadata={"cluster_id": "c9"})
    rc.complete(MSGS)
    first, second = [json.loads(p.read_text()) for p in sorted((tmp_path / "rec").glob("llm-*.json"))]
    assert first["role"] == "recipe_candidate" and first["metadata"] == {"cluster_id": "c9"}
    assert second["role"] is None and second["metadata"] is None
    assert '"role": "recipe_candidate"' in ws.journal_path.read_text()


# --------------------------------------------------------------------------- chat_post (in-house gateway)
from sbmig.fix import ChatPostClient, extract_reply  # noqa: E402


def chat_post(handler, **kw):
    return ChatPostClient(kw.pop("base_url", "http://gw.local/api/chat"), "sk-secret", "gpt-6-luna",
                          transport=httpx.MockTransport(handler), sleep=lambda s: None, **kw)


def test_chat_post_request_shape_is_exactly_the_gateway_contract():
    rec = Recorder([httpx.Response(200, json=OPENAI_OK)])
    c = chat_post(rec, payload_extra={"need_orgin": True})
    resp = c.complete(MSGS, max_tokens=100, temperature=0.2, stop=["x"])
    req = rec.requests[0]
    assert str(req.url) == "http://gw.local/api/chat"               # the full URL, no /chat/completions appended
    assert req.headers["authorization"] == "Bearer sk-secret"
    assert json.loads(req.content) == {"model": "gpt-6-luna", "need_orgin": True,
                                       "messages": [m.model_dump() for m in MSGS]}   # no generation params
    assert resp.content == "hi" and resp.input_tokens == 11 and resp.output_tokens == 7 and resp.model == "m-1"


def test_chat_post_generation_params_and_response_path():
    rec = Recorder([httpx.Response(200, json={"data": {"result": {"content": "deep"}},
                                              "usage": {"input_tokens": 3, "output_tokens": 2}})])
    c = chat_post(rec, generation_params=True, response_path="data.result.content")
    resp = c.complete(MSGS, max_tokens=50, temperature=0.0)
    body = json.loads(rec.requests[0].content)
    assert body["max_tokens"] == 50 and body["temperature"] == 0.0
    assert resp.content == "deep" and resp.input_tokens == 3 and resp.output_tokens == 2 and resp.model == "gpt-6-luna"


def test_chat_post_auto_detects_common_shapes_and_fails_loudly():
    assert extract_reply({"content": "a"}) == "a"
    assert extract_reply({"response": "b"}) == "b"
    assert extract_reply({"data": {"content": "c"}}) == "c"
    assert extract_reply({"choices": [{"message": {"content": [{"type": "text", "text": "d"}]}}]}) == "d"
    assert extract_reply("plain") == "plain"
    with pytest.raises(LLMError, match="response_path"):
        extract_reply({"weird": 1})
    with pytest.raises(LLMError, match="not found"):
        extract_reply({"weird": 1}, "data.x")
    rec = Recorder([httpx.Response(200, json={"weird": 1})])
    with pytest.raises(LLMError, match="llm-probe"):
        chat_post(rec).complete(MSGS)


def test_make_llm_client_chat_post(cfg, monkeypatch):
    cfg.llm.provider = "chat_post"
    cfg.llm.base_url = "http://gw/chat"
    cfg.llm.model = "gpt-6-luna"
    cfg.llm.api_key_env = "SBMIG_TEST_KEY"
    cfg.llm.payload_extra = {"need_orgin": True}
    cfg.llm.response_path = "data.content"
    monkeypatch.setenv("SBMIG_TEST_KEY", "k")
    c = make_llm_client(cfg)
    assert isinstance(c, ChatPostClient) and c.payload_extra == {"need_orgin": True} \
        and c.response_path == "data.content" and not c.generation_params
    cfg.llm.base_url = "${LLM_BASE_URL}"
    with pytest.raises(LLMConfigError, match="full endpoint URL"):
        make_llm_client(cfg)


def test_company_profile_loads(cfg_factory=None):
    from pathlib import Path

    from sbmig.core.config import load_config
    repo = Path(__file__).resolve().parents[2]
    c = load_config(repo / "config" / "artifacts.local-e2e.yml", repo / "config" / "llm-company.yml")
    assert c.llm.provider == "langchain" and c.llm.payload_extra == {"need_orgin": True}
    assert c.llm.langchain_model is None                 # → sbmig.llm.langchain.ChatPostModel
    assert c.llm.model == "gpt-6-luna" and c.llm.recipe_model == "gpt-6-luna" and not c.llm.payload_generation_params
    assert c.llm.http_backend == "requests" and c.llm.verify_ssl is True


# --------------------------------------------------------------------------- requests backend
class _FakeRequestsResponse:
    def __init__(self, status: int, body: dict | str, headers: dict | None = None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


def test_requests_backend_posts_exactly_like_the_company_snippet(monkeypatch):
    import requests

    calls: list[dict] = []

    def fake_post(self, url, **kw):
        calls.append({"url": url, **kw})
        return _FakeRequestsResponse(200, {"data": {"result": {"content": "pong"}}})

    monkeypatch.setattr(requests.Session, "post", fake_post)
    c = ChatPostClient("http://gw.local/chat", "sk-secret", "gpt-6-luna", payload_extra={"need_orgin": True},
                       response_path="data.result.content", http_backend="requests", timeout=42.0,
                       sleep=lambda s: None)
    assert c.complete(MSGS).content == "pong"
    assert calls == [{"url": "http://gw.local/chat",
                      "headers": {"Content-Type": "application/json", "Authorization": "Bearer sk-secret"},
                      "json": {"model": "gpt-6-luna", "messages": [m.model_dump() for m in MSGS], "need_orgin": True},
                      "timeout": 42.0}]
    assert c._session.verify is True
    c.close()


def test_requests_backend_retries_transport_errors_and_reports_503(monkeypatch):
    import requests

    seq = [requests.ConnectionError("proxy refused"), _FakeRequestsResponse(503, "Service Unavailable"),
           _FakeRequestsResponse(503, "Service Unavailable"), _FakeRequestsResponse(503, "Service Unavailable")]

    def fake_post(self, url, **kw):
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(requests.Session, "post", fake_post)
    c = ChatPostClient("http://gw.local/chat", "k", "m", http_backend="requests", max_retries=3, sleep=lambda s: None)
    with pytest.raises(LLMError) as ei:
        c.complete(MSGS)
    assert ei.value.status == 503 and "Service Unavailable" in str(ei.value)


def test_backend_and_verify_flow_from_config(cfg, monkeypatch):
    cfg.llm.provider = "chat_post"
    cfg.llm.base_url = "http://gw/chat"
    cfg.llm.model = "m"
    cfg.llm.api_key_env = "SBMIG_TEST_KEY"
    cfg.llm.http_backend = "requests"
    cfg.llm.verify_ssl = "/etc/ssl/corp-ca.pem"
    monkeypatch.setenv("SBMIG_TEST_KEY", "k")
    c = make_llm_client(cfg)
    assert isinstance(c, ChatPostClient) and c.http_backend == "requests" and c.verify == "/etc/ssl/corp-ca.pem"
    assert c._session.verify == "/etc/ssl/corp-ca.pem"
    cfg.llm.http_backend = "bogus"
    with pytest.raises(LLMConfigError, match="http_backend"):
        make_llm_client(cfg)
