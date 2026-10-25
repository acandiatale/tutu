"""The company gateway client: requests.post(url, headers=, json=, timeout=) exactly, configured from .env."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from sbmig.core.config import load_config
from sbmig.core.interfaces import ChatMessage, LLMClientLike
from sbmig.fix import CompanyClient, LLMConfigError, LLMError, RecordingClient, make_llm_client

MSGS = [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hello")]
REPO = Path(__file__).resolve().parents[2]


class FakeResponse:
    def __init__(self, status: int, body, headers: dict | None = None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class FakePost:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, **kw):
        self.calls.append({"url": url, **kw})
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


def client(post, **kw):
    kw.setdefault("payload_extra", {"need_origin": True})
    return CompanyClient("http://gw.local/chat", "sk-secret", "gpt-6-luna", post=post, sleep=lambda s: None, **kw)


def test_call_shape_is_exactly_the_company_snippet():
    post = FakePost(FakeResponse(200, {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 11, "completion_tokens": 7}, "model": "m-1"}))
    c = client(post, timeout=42)
    assert isinstance(c, LLMClientLike)
    resp = c.complete(MSGS, max_tokens=100, temperature=0.2, stop=["x"], role="fix", metadata={"a": 1})
    assert post.calls == [{"url": "http://gw.local/chat",
                           "headers": {"Authorization": "Bearer sk-secret"},
                           "json": {"model": "gpt-6-luna", "messages": [m.model_dump() for m in MSGS],
                                    "need_origin": True},          # no max_tokens / temperature / stop
                           "timeout": 42.0}]
    assert resp.content == "hi" and resp.model == "m-1" and resp.input_tokens == 11 and resp.output_tokens == 7
    assert resp.finish_reason == "stop"
    assert "sk-secret" not in repr(c)


def test_response_path_and_auto_detect_and_extra_headers():
    post = FakePost(FakeResponse(200, {"data": {"result": {"content": "deep"}},
                                       "usage": {"input_tokens": 3, "output_tokens": 2}}))
    c = client(post, response_path="data.result.content", extra_headers={"x-team": "mig"})
    r = c.complete(MSGS)
    assert r.content == "deep" and r.input_tokens == 3 and r.output_tokens == 2 and r.model == "gpt-6-luna"
    assert post.calls[0]["headers"] == {"Authorization": "Bearer sk-secret", "x-team": "mig"}
    assert client(FakePost(FakeResponse(200, {"response": "plain"}))).complete(MSGS).content == "plain"
    with pytest.raises(LLMError, match="response_path"):
        client(FakePost(FakeResponse(200, {"weird": 1}))).complete(MSGS)


def test_retries_transport_errors_and_5xx_then_reports_the_last_status():
    post = FakePost(requests.ConnectionError("refused"), FakeResponse(503, "Service Unavailable"),
                    FakeResponse(503, "Service Unavailable"), FakeResponse(503, "Service Unavailable"))
    with pytest.raises(LLMError) as ei:
        client(post, max_retries=3).complete(MSGS)
    assert ei.value.status == 503 and "Service Unavailable" in str(ei.value) and len(post.calls) == 4
    post = FakePost(FakeResponse(401, "bad token"))
    with pytest.raises(LLMError) as ei:
        client(post).complete(MSGS)
    assert ei.value.status == 401 and len(post.calls) == 1          # not retried
    with pytest.raises(LLMError, match="invalid JSON"):
        client(FakePost(FakeResponse(200, "<html>oops</html>"))).complete(MSGS)


def test_default_config_is_the_company_provider_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_BASE_URL", "http://gw.local:8080/api/chat")
    monkeypatch.setenv("LLM_API_KEY", "tok")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_RESPONSE_PATH", raising=False)
    monkeypatch.setenv("SBMIG_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.delenv("LLM_RECORD_DIR", raising=False)
    cfg = load_config(REPO / "config" / "artifacts.example.yml")
    assert cfg.llm.provider == "company" and cfg.llm.model == "gpt-6-luna"
    assert cfg.llm.payload_extra == {"need_origin": True} and not cfg.llm.response_path and not cfg.llm.record_dir
    c = make_llm_client(cfg)
    assert isinstance(c, CompanyClient)
    assert c.url == "http://gw.local:8080/api/chat" and c.payload_extra == {"need_origin": True}
    monkeypatch.setenv("LLM_RECORD_DIR", str(tmp_path / "rec"))
    c = make_llm_client(load_config(REPO / "config" / "artifacts.example.yml"))
    assert isinstance(c, RecordingClient) and isinstance(c.inner, CompanyClient)   # records opt-in via .env
    monkeypatch.setenv("LLM_RESPONSE_PATH", "data.result.content")
    monkeypatch.setenv("LLM_MODEL", "other-model")
    cfg = load_config(REPO / "config" / "artifacts.example.yml")
    assert cfg.llm.response_path == "data.result.content" and cfg.llm.model == "other-model"
    # role models (round 9): unset → falsy (same as LLM_MODEL everywhere they are read); set → used as given
    assert not cfg.llm.recipe_model and not cfg.llm.escalation_model
    monkeypatch.setenv("LLM_RECIPE_MODEL", "cheap-model")
    monkeypatch.setenv("LLM_ESCALATION_MODEL", "big-model")
    cfg = load_config(REPO / "config" / "artifacts.example.yml")
    assert cfg.llm.recipe_model == "cheap-model" and cfg.llm.escalation_model == "big-model"


def test_missing_env_gives_clear_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("SBMIG_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "tok")
    cfg = load_config(REPO / "config" / "artifacts.example.yml")
    with pytest.raises(LLMConfigError, match="LLM_BASE_URL"):
        make_llm_client(cfg)
    monkeypatch.setenv("LLM_BASE_URL", "http://gw/chat")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    cfg = load_config(REPO / "config" / "artifacts.example.yml")
    with pytest.raises(LLMConfigError, match="LLM_API_KEY"):
        make_llm_client(cfg)


def test_azure_style_response_shape_and_content_filter():
    """The in-house GPT gateway answers in the OpenAI / Azure chat-completions shape (routing, created,
    prompt_filter_results, usage, model, service_tier, id, choices[{content_filter_result, finish_reason, index,
    message, logprobs}], system_fingerprint, object): auto-detected without a response_path. A content-filter hit
    (finish_reason=content_filter, null content) is a request-level rejection, not a transport failure."""
    ok = {"routing": {"deployment": "gpt-6-luna-eastus"}, "created": 1760000000, "id": "chatcmpl-1", "object": "chat.completion",
          "model": "gpt-6-luna", "service_tier": "default", "system_fingerprint": "fp_1",
          "prompt_filter_results": [{"prompt_index": 0, "content_filter_results": {
              "hate": {"filtered": False, "severity": "safe"}, "jailbreak": {"filtered": False, "detected": False}}}],
          "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
          "choices": [{"index": 0, "finish_reason": "stop", "logprobs": None,
                       "content_filter_result": {"hate": {"filtered": False, "severity": "safe"}},
                       "message": {"role": "assistant", "content": "<analysis>ok</analysis>"}}]}
    r = client(FakePost(FakeResponse(200, ok))).complete(MSGS)
    assert r.content == "<analysis>ok</analysis>" and r.input_tokens == 120 and r.output_tokens == 30
    assert r.finish_reason == "stop" and r.model == "gpt-6-luna"
    filtered = json.loads(json.dumps(ok))
    filtered["choices"][0].update({"finish_reason": "content_filter", "message": {"role": "assistant", "content": None},
                                   "content_filter_result": {"hate": {"filtered": True, "severity": "high"}}})
    with pytest.raises(LLMError, match="content filtered by the gateway"):
        client(FakePost(FakeResponse(200, filtered))).complete(MSGS)
    from sbmig.loop.fixloop import is_llm_transport_error
    assert is_llm_transport_error("llm-error: content filtered by the gateway (finish_reason=content_filter); "
                                  "this request only, not an outage") is False
    # a prompt filter that fired with an empty answer is reported the same way
    prompt_hit = json.loads(json.dumps(ok))
    prompt_hit["choices"][0]["message"]["content"] = ""
    prompt_hit["prompt_filter_results"][0]["content_filter_results"]["jailbreak"] = {"filtered": True, "detected": True}
    with pytest.raises(LLMError, match="prompt_filter_results: jailbreak"):
        client(FakePost(FakeResponse(200, prompt_hit))).complete(MSGS)


def test_progress_lines_call_start_heartbeat_and_retry_delay(caplog):
    """Round 9 (operators): every call logs its start, a heartbeat while the gateway is silent, and the retry delay."""
    import logging as _logging
    import time as _time

    class SlowPost(FakePost):
        def __call__(self, url, **kw):
            _time.sleep(0.08)
            return super().__call__(url, **kw)

    post = SlowPost(FakeResponse(503, "busy"), FakeResponse(200, {"choices": [{"message": {"content": "pong"}}]}))
    c = client(post, heartbeat_s=0.02)
    with caplog.at_level(_logging.INFO, logger="sbmig.fix.client"):
        assert c.complete(MSGS, role="fix").content == "pong"
    text = caplog.text
    assert "llm call provider=company role=fix model=gpt-6-luna attempt 1/4 → POST http://gw.local/chat" in text
    assert "llm call still in flight:" in text and "timeout 180s" in text
    assert "llm call retrying in 1s (attempt 1/4 failed: status=503)" in text
    assert "attempt 2/4 → POST" in text and "latency=" in text
    quiet = client(FakePost(FakeResponse(200, {"choices": [{"message": {"content": "x"}}]})), heartbeat_s=0)
    assert quiet.complete(MSGS).content == "x"                     # heartbeat off → no thread, still works


def test_hard_deadline_abandons_a_hung_call(caplog):
    """Round 9: a gateway that never answers (keep-alive, no bytes) must not hang the run — past the wall-clock
    deadline the call is abandoned and counted as a transport failure like any other."""
    import logging as _logging
    import threading as _threading
    import time as _time

    release = _threading.Event()

    class HungPost(FakePost):
        def __call__(self, url, **kw):
            release.wait(5)                 # the "connection" stays open until the test lets go
            return super().__call__(url, **kw)

    post = HungPost(FakeResponse(200, {"choices": [{"message": {"content": "late"}}]}))
    c = client(post, hard_deadline_s=0.1, heartbeat_s=0, max_retries=1)
    t0 = _time.monotonic()
    with caplog.at_level(_logging.INFO, logger="sbmig.fix.client"), pytest.raises(LLMError, match="hard deadline"):
        c.complete(MSGS)
    assert _time.monotonic() - t0 < 2.0                       # two attempts × 0.1 s + backoff, not 5 s
    assert "transport error (attempt 1/2): LLMHardDeadline" in caplog.text
    release.set()
    # a call that answers in time is untouched by the deadline
    ok = client(FakePost(FakeResponse(200, {"choices": [{"message": {"content": "pong"}}]})), hard_deadline_s=5)
    assert ok.complete(MSGS).content == "pong"


def test_heartbeat_reaches_the_per_run_log_file(tmp_path):
    """Round 9: the web app filters sbmig.log by run thread (contextvar); the heartbeat thread must inherit it."""
    import logging as _logging
    import threading as _threading
    import time as _time

    from sbmig.cli import attach_file_log

    class SlowPost(FakePost):
        def __call__(self, url, **kw):
            _time.sleep(0.08)
            return super().__call__(url, **kw)

    path = tmp_path / "sbmig.log"
    attach_file_log(path, thread_id=_threading.get_ident())
    try:
        c = client(SlowPost(FakeResponse(200, {"choices": [{"message": {"content": "pong"}}]})), heartbeat_s=0.02)
        assert c.complete(MSGS, role="fix").content == "pong"
        for h in list(_logging.getLogger("sbmig").handlers):
            h.flush()
        text = path.read_text(encoding="utf-8")
    finally:
        root = _logging.getLogger("sbmig")
        for h in list(root.handlers):
            if isinstance(h, _logging.FileHandler) and Path(h.baseFilename) == path.resolve():
                root.removeHandler(h)
                h.close()
    assert "llm call provider=company role=fix" in text          # the call-start line (run thread)
    assert "llm call still in flight" in text                      # the heartbeat (helper thread, inherited context)


# ---------------------------------------------------------------- round 10: causes, finish_reason=length, probe
def _winerror_10061() -> requests.ConnectionError:
    """The shape requests raises on a Korean Windows PC when nothing listens on the gateway port (urllib3 1.x text)."""
    from urllib3.exceptions import MaxRetryError, NewConnectionError

    inner = NewConnectionError("<urllib3.connection.HTTPConnection object at 0x000001F2A3B4C5D0>",
                               "Failed to establish a new connection: [WinError 10061] 대상 컴퓨터에서 연결을 "
                               "거부했으므로 연결하지 못했습니다")
    mre = MaxRetryError(None, "/v1/chat", inner)
    return requests.ConnectionError(mre)


def test_transport_cause_names_the_real_reason():
    from sbmig.fix.company import transport_cause

    c = transport_cause(_winerror_10061())
    assert c == "ConnectionError: Failed to establish a new connection: [WinError 10061] ?"
    assert "object at 0x" not in c and "HTTPConnectionPool" not in c
    # a real refused connection (nothing listens on port 1)
    try:
        requests.post("http://127.0.0.1:1/chat", json={}, timeout=3)
    except requests.RequestException as e:
        real = transport_cause(e)
    assert real.startswith("ConnectionError: ") and ("[Errno" in real or "[WinError" in real), real
    from urllib3.exceptions import MaxRetryError, NewConnectionError
    from urllib3.exceptions import ProxyError as U3ProxyError
    from urllib3.exceptions import SSLError as U3SSLError
    proxy = requests.exceptions.ProxyError(MaxRetryError(None, "http://gw/chat", U3ProxyError(
        "Unable to connect to proxy", NewConnectionError(None, "Failed to establish a new connection: [Errno 111] "
                                                               "Connection refused"))))
    assert transport_cause(proxy) == "ProxyError: Failed to establish a new connection: [Errno 111] Connection refused"
    import ssl
    cert = requests.exceptions.SSLError(MaxRetryError(None, "/chat", U3SSLError(ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate "
           "(_ssl.c:1006)"))))
    assert transport_cause(cert).startswith("SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    timeout = requests.exceptions.ReadTimeout("HTTPConnectionPool(host='gw', port=80): Read timed out. (read "
                                              "timeout=180.0)")
    assert transport_cause(timeout) == "ReadTimeout: Read timed out. (read timeout=180.0)"


def test_transport_failures_log_and_raise_the_cause(caplog):
    """WinError 10061 used to be lost: the retry line said only the class, the give-up line was cut at 300 chars."""
    import logging as _logging

    post = FakePost(_winerror_10061())
    with caplog.at_level(_logging.INFO, logger="sbmig.fix.client"), pytest.raises(LLMError) as ei:
        client(post, max_retries=1).complete(MSGS)
    text = str(ei.value)
    assert "status=None" in text and "cause=ConnectionError: " in text and "[WinError 10061]" in text
    assert ei.value.status is None and len(post.calls) == 2
    log_text = caplog.text
    assert "transport error (attempt 1/2): ConnectionError cause=ConnectionError: " in log_text
    assert "llm call retrying in 1s (attempt 1/2 failed: status=None cause=ConnectionError: " in log_text
    give_up = next(r.getMessage() for r in caplog.records if "giving up" in r.getMessage())
    assert "[WinError 10061]" in give_up and give_up.isascii()
    from sbmig.loop.fixloop import is_llm_transport_error
    assert is_llm_transport_error(f"llm-error: {ei.value}") is True          # still an outage for the fix loop


def test_empty_answer_with_finish_reason_length_is_a_request_rejection(caplog):
    """A reasoning model that used the gateway's max_tokens answers HTTP 200, finish_reason=length, empty content:
    that is the request's size, not the response shape (no 'set llm.response_path') and not an outage."""
    body = {"choices": [{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": ""}}],
            "usage": {"prompt_tokens": 90000, "completion_tokens": 4096}}
    post = FakePost(FakeResponse(200, body))
    with pytest.raises(LLMError) as ei:
        client(post).complete(MSGS)
    text = str(ei.value)
    assert "finish_reason=length" in text and "max_tokens" in text and "response_path" not in text
    assert "output tokens 4096" in text and len(post.calls) == 1                  # not retried
    from sbmig.loop.fixloop import is_llm_transport_error
    assert is_llm_transport_error(f"llm-error: {ei.value}") is False
    none_body = {"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": None}}]}
    with pytest.raises(LLMError, match="finish_reason=length"):
        client(FakePost(FakeResponse(200, none_body))).complete(MSGS)
    # a cut answer that has text is returned (the protocol parser decides), with a warning in the log
    cut = {"choices": [{"finish_reason": "length", "message": {"content": "<analysis>partial"}}]}
    import logging as _logging
    with caplog.at_level(_logging.WARNING, logger="sbmig.fix.client"):
        assert client(FakePost(FakeResponse(200, cut))).complete(MSGS).content == "<analysis>partial"
    assert "finish_reason=length" in caplog.text


def _company_cfg(monkeypatch, tmp_path, **llm):
    monkeypatch.setenv("SBMIG_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("LLM_BASE_URL", "http://gw.local/chat")
    monkeypatch.setenv("LLM_API_KEY", "sk-secret")
    monkeypatch.setenv("LLM_MODEL", "gpt-6-luna")
    monkeypatch.delenv("LLM_RECORD_DIR", raising=False)
    monkeypatch.delenv("LLM_RESPONSE_PATH", raising=False)
    return load_config(REPO / "config" / "artifacts.example.yml", overrides={"llm": llm} if llm else None)


def test_probe_llm_one_call_and_short_lines(monkeypatch, tmp_path):
    """The probe shared by `sbmig llm-probe` and the fleet preflight: ONE post (no retries), a retypeable line."""
    from sbmig.fix.company import probe_llm

    cfg = _company_cfg(monkeypatch, tmp_path)
    post = FakePost(FakeResponse(200, {"choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}]}))
    monkeypatch.setattr(requests, "post", post)
    seen = []
    res = probe_llm(cfg, on_request=seen.append)
    assert res.ok and res.reply == "pong" and res.status == 200 and len(post.calls) == 1
    assert seen and seen[0]["model"] == "gpt-6-luna" and post.calls[0]["json"] == seen[0]
    assert res.line().startswith("LLM-PROBE ok provider=company model=gpt-6-luna status=200 latency=")
    assert "reply='pong'" in res.line() and res.line().isascii()

    monkeypatch.setattr(requests, "post", FakePost(_winerror_10061(), FakeResponse(200, {})))
    res = probe_llm(cfg)
    assert not res.ok and res.kind == "transport" and res.transient and "[WinError 10061]" in res.problem
    assert res.line().startswith("LLM-PROBE FAIL transport provider=company") and res.line().isascii()

    monkeypatch.setattr(requests, "post", FakePost(FakeResponse(503, "busy")))
    res = probe_llm(cfg)
    assert res.kind == "http" and res.status == 503 and res.transient
    monkeypatch.setattr(requests, "post", FakePost(FakeResponse(400, "unsupported model")))
    res = probe_llm(cfg)
    assert res.kind == "http" and not res.transient                             # a config error: do not wait for it
    monkeypatch.setattr(requests, "post", FakePost(FakeResponse(200, {
        "choices": [{"finish_reason": "length", "message": {"content": ""}}]})))
    res = probe_llm(cfg)
    assert res.kind == "rejected" and "finish_reason=length" in res.problem and "max_tokens" in " ".join(res.hints)
    monkeypatch.setattr(requests, "post", FakePost(FakeResponse(200, {"weird": 1})))
    res = probe_llm(cfg)
    assert res.kind == "reply" and "LLM_RESPONSE_PATH" in " ".join(res.hints)
    monkeypatch.delenv("LLM_API_KEY")
    res = probe_llm(load_config(REPO / "config" / "artifacts.example.yml"))
    assert res.kind == "config" and "LLM_API_KEY" in res.problem and not res.transient
    fake = probe_llm(load_config(REPO / "config" / "artifacts.example.yml", overrides={"llm": {"provider": "fake"}}))
    assert fake.ok and fake.client_name == "FakeLLMClient"
