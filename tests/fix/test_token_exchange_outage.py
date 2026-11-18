"""K2: a failed token exchange of the agent gateway (5xx, 429, a 401 from the token endpoint, a non-JSON body, no
`access_token`, a connection failure) is an LLM outage: retried like the chat call, and when it still fails the
error text carries the marker the fix loop's `is_llm_transport_error` counts (cool-down, wait) — never a body, a
token or the app token."""
from __future__ import annotations

import logging

import pytest
import requests

from sbmig.fix.client import LLMError
from sbmig.fix.company import TokenExchangeError, TokenManager
from sbmig.loop.fixloop import is_llm_transport_error

from .test_company_client import MSGS, FakePost, FakeResponse, agent_client, llm_ok, token_resp

APP = "app-token-SECRET-123"
LEAK = "access_token=tk-LEAKED-999"


def _client(*responses, retries=2):
    post = FakePost(*responses)
    tm = TokenManager("http://gw.local/token", APP, post=post)
    return agent_client(post, tm, max_retries=retries), post


@pytest.mark.parametrize("bad", [
    FakeResponse(503, f"<html>busy {LEAK}</html>"),
    FakeResponse(429, LEAK),
    FakeResponse(401, f"bad app token {APP}"),
    FakeResponse(200, f"not json {LEAK}"),
    FakeResponse(200, {"token_type": "Bearer"}),
    FakeResponse(400, LEAK),
])
def test_a_failed_exchange_is_retried_and_then_a_transport_error(bad, caplog):
    caplog.set_level(logging.DEBUG)
    c, post = _client(bad, retries=2)
    with pytest.raises(LLMError) as ei:
        c.complete(MSGS, role="fix")
    assert [k["url"] for k in post.calls] == ["http://gw.local/token"] * 3      # retried like the chat call
    text = f"llm-error: {ei.value}"
    assert is_llm_transport_error(text), text
    assert "status=400" not in text                                         # never "the request was rejected"
    for secret in (APP, "tk-LEAKED-999", "<html>", "bad app token"):
        assert secret not in text and secret not in caplog.text
        assert secret not in (ei.value.body_excerpt or "")


def test_a_failed_exchange_that_recovers_answers_normally():
    c, post = _client(FakeResponse(503, "busy"), token_resp("tk-2"), llm_ok(), retries=2)
    assert c.complete(MSGS, role="fix").content == "hi"
    assert [k["url"] for k in post.calls] == ["http://gw.local/token", "http://gw.local/token", "http://gw.local/chat"]
    assert post.calls[2]["headers"] == {"Authorization": "Bearer tk-2"}


def test_a_connection_failure_of_the_exchange_is_a_transport_error():
    c, post = _client(requests.ConnectionError("Failed to establish a new connection"), retries=1)
    with pytest.raises(LLMError) as ei:
        c.complete(MSGS, role="fix")
    assert len(post.calls) == 2 and is_llm_transport_error(f"llm-error: {ei.value}")


def test_the_exchange_after_a_chat_401_is_retried_too():
    c, post = _client(token_resp("tk-old"), FakeResponse(401, "expired"), FakeResponse(503, "busy"),
                      token_resp("tk-new"), llm_ok(), retries=1)
    assert c.complete(MSGS, role="fix").content == "hi"
    assert post.calls[-1]["headers"] == {"Authorization": "Bearer tk-new"}


def test_the_error_class_markers():
    assert "(status=503)" in str(TokenExchangeError(503, "HTTP 503"))
    assert "(status=401)" in str(TokenExchangeError(401, "HTTP 401"))
    assert "(cause=TokenExchangeError)" in str(TokenExchangeError(400, "HTTP 400"))
    assert "(cause=TokenExchangeError)" in str(TokenExchangeError(200, "the answer is not JSON"))
    assert TokenExchangeError(503, "x").body_excerpt == ""
