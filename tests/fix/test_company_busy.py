"""Several build pods share one gateway and one app token (2026-11-10): "busy" is not "broken", and a 401 right after
a fresh exchange is retried a bounded number of times. No real sleeping: sleep and jitter are injected."""
from __future__ import annotations

import json
import logging

import pytest

from sbmig.fix import company
from sbmig.core.config import load_config
from sbmig.core.interfaces import ChatMessage
from sbmig.fix import LLMError
from sbmig.fix.company import (
    BUSY_MAX_DELAY,
    BUSY_RETRY_AFTER_CAP,
    TOKEN_RACE_WARNING,
    CompanyClient,
    TokenManager,
)
from sbmig.loop.fixloop import is_llm_transport_error

MSGS = [ChatMessage(role="user", content="hello")]
OK = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}], "usage": {}}


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class Post:
    """Answers in order; the last one repeats."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, url, **kw):
        self.calls.append({"url": url, **kw})
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


def lo(a, b):
    return a


def hi(a, b):
    return b


def make(post, *, rand=lo, **kw):
    slept: list[float] = []
    c = CompanyClient("http://gw.local/chat", "sk-secret", "gpt-6-luna", post=post, sleep=slept.append,
                      heartbeat_s=0, rand=rand, **kw)
    return c, slept


def test_busy_then_success_does_not_use_max_retries(caplog):
    post = Post(*([Resp(429, "busy")] * 6), Resp(200, OK))
    c, slept = make(post, max_retries=1)
    with caplog.at_level(logging.DEBUG, logger="sbmig.fix.client"):
        assert c.complete(MSGS, role="fix").content == "hi"
    assert len(post.calls) == 7                                         # 6 busy answers > max_retries=1
    assert slept == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]                    # rand=lo: half of 2, 4, 8, 16, 32, 60
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("llm gateway busy (HTTP 429): waiting for a free slot, up to 600s") for m in msgs)
    assert any("llm gateway answered after waiting 61s on busy (6 busy answer(s))" in m for m in msgs)
    starts = [r for r in caplog.records if "→ POST" in r.getMessage()]
    assert [r.levelno for r in starts] == [logging.INFO] + [logging.DEBUG] * 6   # busy re-sends are quiet


def test_busy_backoff_is_capped_and_jitter_stays_in_bounds():
    c, _ = make(Post(Resp(200, OK)))
    for n in range(12):
        base = min(BUSY_MAX_DELAY, 2.0 * 2 ** n)
        c._rand = lo
        assert c._busy_delay(n, None) == base / 2
        c._rand = hi
        assert c._busy_delay(n, None) == base
    c._rand = hi
    assert c._busy_delay(0, 30.0) == pytest.approx(33.0)                 # Retry-After + at most 10 %
    assert c._busy_delay(0, 9999.0) == pytest.approx(BUSY_RETRY_AFTER_CAP * 1.1)
    c._rand = lo
    assert c._busy_delay(0, 0.0) == 1.0                                  # never a tight loop


def test_busy_budget_exhausted_raises_the_same_error_shape(caplog):
    post = Post(Resp(429, "slow down"))
    c, slept = make(post, busy_wait_s=100)
    with caplog.at_level(logging.INFO, logger="sbmig.fix.client"), pytest.raises(LLMError) as ei:
        c.complete(MSGS)
    assert sum(slept) == pytest.approx(100.0) and max(slept) <= BUSY_MAX_DELAY
    assert ei.value.status == 429
    text = str(ei.value)
    assert text == f"LLM request failed after {len(post.calls)} attempt(s) (status=429): slow down"
    assert is_llm_transport_error(f"llm-error: {text}") is True              # the fix loop's handling is unchanged
    give_up = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert give_up and give_up[-1].startswith("llm company giving up: gateway busy for 100s (llm.busy_wait_seconds=100")


def test_still_busy_line_at_most_every_minute(caplog):
    post = Post(Resp(429, "busy"))
    c, _ = make(post, busy_wait_s=600, rand=hi)
    with caplog.at_level(logging.INFO, logger="sbmig.fix.client"), pytest.raises(LLMError):
        c.complete(MSGS)
    still = [r.getMessage() for r in caplog.records if r.getMessage().startswith("llm gateway still busy")]
    assert 5 <= len(still) <= 10                                         # ~600 s / 60 s
    assert "waited " in still[0] and "of 600s, last HTTP 429" in still[0]


def test_retry_after_is_honoured_and_capped_and_marks_a_5xx_as_busy():
    post = Post(Resp(503, "queue", {"retry-after": "7"}), Resp(503, "queue", {"retry-after": "900"}),
                Resp(502, "q", {"retry-after": "3"}), Resp(504, "q", {"retry-after": "3"}), Resp(200, OK))
    c, slept = make(post, max_retries=0)
    assert c.complete(MSGS).content == "hi"
    assert slept == [7.0, BUSY_RETRY_AFTER_CAP, 3.0, 3.0]


def test_a_5xx_without_retry_after_keeps_the_max_retries_path_with_jitter():
    post = Post(Resp(503, "Service Unavailable"))
    c, slept = make(post, max_retries=3, rand=hi)
    with pytest.raises(LLMError) as ei:
        c.complete(MSGS)
    assert len(post.calls) == 4 and slept == [1.25, 2.5, 5.0]            # 1·2·4 s + at most 25 %
    assert str(ei.value) == "LLM request failed after 4 attempt(s) (status=503): Service Unavailable"
    c, slept = make(Post(Resp(503, "x")), max_retries=3, rand=lo)
    with pytest.raises(LLMError):
        c.complete(MSGS)
    assert slept == [1.0, 2.0, 4.0]                                      # the old delays are the lower bound


def test_busy_wait_zero_is_the_old_behaviour():
    post = Post(Resp(429, "busy"))
    c, slept = make(post, busy_wait_s=0, max_retries=3)
    with pytest.raises(LLMError) as ei:
        c.complete(MSGS)
    assert len(post.calls) == 4 and slept == [1.0, 2.0, 4.0]
    assert str(ei.value) == "LLM request failed after 4 attempt(s) (status=429): busy"
    post = Post(Resp(429, "busy", {"retry-after": "5"}), Resp(200, OK))
    c, slept = make(post, busy_wait_s=0, max_retries=3)
    assert c.complete(MSGS).content == "hi" and slept == [5.0]


def test_busy_and_auth_keys_from_config_and_env(monkeypatch, tmp_path):
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("SBMIG_ENV_FILE", str(tmp_path / "none.env"))
    cfg = load_config(repo / "config" / "artifacts.example.yml")
    assert cfg.llm.busy_wait_seconds == 600 and cfg.llm.auth_retries == 2
    monkeypatch.setenv("SBMIG_LLM__BUSY_WAIT_SECONDS", "0")
    monkeypatch.setenv("SBMIG_LLM__AUTH_RETRIES", "0")
    cfg = load_config(repo / "config" / "artifacts.example.yml")
    assert cfg.llm.busy_wait_seconds == 0 and cfg.llm.auth_retries == 0


# --------------------------------------------------------------------------------------------------- 401 after exchange
def token_post(*tokens):
    return Post(*[Resp(200, {"access_token": t, "expires_in": 3600}) for t in tokens])


class Gateway:
    """Token endpoint + chat endpoint in one post function; the chat answers come from `chat`."""

    def __init__(self, chat, tokens=("tk-1", "tk-2", "tk-3", "tk-4", "tk-5")):
        self.tokens, self.chat, self.calls = list(tokens), list(chat), []

    def __call__(self, url, **kw):
        self.calls.append({"url": url, **kw})
        if url.endswith("/token"):
            return Resp(200, {"access_token": self.tokens.pop(0), "expires_in": 3600})
        return self.chat.pop(0) if len(self.chat) > 1 else self.chat[0]


@pytest.fixture(autouse=True)
def _fresh_warning_flag(monkeypatch):
    monkeypatch.setattr(company, "_TOKEN_RACE_WARNED", False)


def agent(gw, **kw):
    tm = TokenManager("http://gw.local/token", "app-secret", post=gw)
    slept: list[float] = []
    c = CompanyClient("http://gw.local/chat", "static-secret", "build_error_fixer", post=gw, gateway="agent",
                      asset_id="asset-1", token_manager=tm, sleep=slept.append, heartbeat_s=0, rand=hi,
                      max_retries=0, **kw)
    return c, slept


def chat_tokens(gw):
    return [c["headers"]["Authorization"].split()[1] for c in gw.calls if c["url"].endswith("/chat")]


def test_a_401_after_a_fresh_exchange_gets_another_round(caplog):
    gw = Gateway([Resp(401, "invalid token"), Resp(401, "invalid token"), Resp(200, OK)])
    c, slept = agent(gw)
    with caplog.at_level(logging.DEBUG, logger="sbmig.fix.client"):
        assert c.complete(MSGS, role="fix").content == "hi"
    assert chat_tokens(gw) == ["tk-1", "tk-2", "tk-3"] and slept == [5.0]       # rand=hi: the 1-5 s pause at its top
    warnings = [r.getMessage() for r in caplog.records if r.getMessage() == TOKEN_RACE_WARNING]
    assert len(warnings) == 1 and "another client may be exchanging the same app token" in warnings[0]
    _no_secret(caplog)


def test_the_extra_rounds_are_bounded_and_the_warning_is_logged_once_per_process(caplog):
    gw = Gateway([Resp(401, "invalid token")], tokens=[f"tk-{i}" for i in range(20)])
    c, slept = agent(gw)
    with caplog.at_level(logging.DEBUG, logger="sbmig.fix.client"):
        with pytest.raises(LLMError) as ei:
            c.complete(MSGS)
        assert ei.value.status == 401
        assert len(chat_tokens(gw)) == 1 + 1 + 2                       # first try, the old refresh, auth_retries=2
        assert len(slept) == 2 and all(1.0 <= s <= 5.0 for s in slept)
        with pytest.raises(LLMError):
            c.complete(MSGS)                                            # a second call: no second warning
    assert sum(r.getMessage() == TOKEN_RACE_WARNING for r in caplog.records) == 1
    _no_secret(caplog)
    gw0 = Gateway([Resp(401, "invalid token")])
    c0, slept0 = agent(gw0, auth_retries=0)                             # 0 = one refresh after a 401, as before
    with pytest.raises(LLMError):
        c0.complete(MSGS)
    assert len(chat_tokens(gw0)) == 2 and slept0 == []


def test_another_pods_exchange_between_our_refresh_and_retry_is_survived():
    """The reviewer's ping-pong: a gateway keeps only the newest exchanged token; pod B exchanges while A retries."""
    state = {"n": 0, "valid": None}

    def tok(url, headers=None, timeout=None):
        state["n"] += 1
        state["valid"] = f"t{state['n']}"
        return Resp(200, {"access_token": state["valid"], "expires_in": 3600})

    tm_a, tm_b = TokenManager("http://tok", "app", post=tok), TokenManager("http://tok", "app", post=tok)
    calls = {"n": 0}

    def gw(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 2:
            tm_b.refresh()
        return Resp(200, OK) if headers["Authorization"].split()[1] == state["valid"] else Resp(401, "invalid token")

    tm_a.token()
    tm_b.token()
    a = CompanyClient("http://gw", "", "m", post=gw, sleep=lambda s: None, heartbeat_s=0, token_manager=tm_a,
                      rand=lo, max_retries=0)
    assert a.complete(MSGS).content == "hi" and calls["n"] == 3


def test_invalidate_keeps_a_token_another_call_already_refreshed():
    tm = TokenManager("http://gw.local/token", "app", post=token_post("tk-1", "tk-2", "tk-3"))
    assert tm.token() == "tk-1"
    assert tm.refresh() == "tk-2"                  # another call of this process refreshed meanwhile
    tm.invalidate("tk-1")                          # our stale 401 must not throw the new one away
    assert tm.token() == "tk-2" and tm.refreshes == 2
    tm.invalidate("tk-2")
    assert tm.token() == "tk-3"
    tm.invalidate()                                # no token given: unconditional, as before
    assert not tm.valid()


def test_every_exchanged_token_is_noted_as_a_secret(monkeypatch):
    noted = []
    monkeypatch.setattr(company, "note_secret", noted.append)
    gw = Gateway([Resp(401, "x"), Resp(401, "x"), Resp(200, OK)])
    c, _ = agent(gw)
    c.complete(MSGS)
    assert noted == ["tk-1", "tk-2", "tk-3"]


def test_a_busy_call_counts_as_in_flight_for_the_watchdog():
    from sbmig.fix.client import llm_calls_in_flight
    seen = []
    post = Post(Resp(429, "busy"), Resp(429, "busy"), Resp(200, OK))
    c = CompanyClient("http://gw.local/chat", "k", "m", post=post, heartbeat_s=0, rand=lo,
                      sleep=lambda s: seen.append(llm_calls_in_flight()))
    before = llm_calls_in_flight()
    assert c.complete(MSGS).content == "hi"
    assert seen == [before + 1, before + 1] and llm_calls_in_flight() == before


def _no_secret(caplog):
    for r in caplog.records:
        m = r.getMessage()
        assert "tk-" not in m and "app-secret" not in m and "static-secret" not in m and "Bearer" not in m, m
