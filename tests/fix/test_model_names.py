"""v2.1z-bg point 3: the name of a usage row is the name a call ASKED with (the configured model; the role on the agent
gateway), never the gateway's placeholder `unknown` / an empty name that the admin page showed as `?`. What the answer
says it served (`model` when real, every `usage.by_model[].model`) is kept beside it as `served`."""
from __future__ import annotations

import logging

import httpx
import pytest

from sbmig.core.interfaces import ChatMessage, LLMResponse
from sbmig.core.models import LLMUsage, ModelCost, real_model_name
from sbmig.fix import CompanyClient
from sbmig.fix.client import OpenAICompatibleClient, served_models_of, usage_from
from sbmig.fix.company import TokenManager
from sbmig.llm.adapter import AdapterClient
from sbmig.web.admin import LLM_CALL_LINE

from .test_company_client import FakePost, FakeResponse

MSGS = [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hello")]


def agent_answer(top_model="unknown", served=("gpt-5.5",)) -> dict:
    """The shape the company's agent gateway answers with (user, 2026-11-20): a placeholder top-level `model`, the
    real names in `usage.by_model[].model`."""
    body = {"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150,
                      "by_model": [{"model": m, "prompt_tokens": 120, "completion_tokens": 30} for m in served]},
            "session_id": "s-1"}
    if top_model is not None:
        body["model"] = top_model
    return body


def agent_client(post):
    tm = TokenManager("http://gw.local/token", "app", post=post)
    return CompanyClient("http://gw.local/chat", "unused", "build_error_fixer", post=post, gateway="agent",
                         asset_id="asset-42", token_manager=tm, sleep=lambda s: None)


def token():
    return FakeResponse(200, {"access_token": "tk", "token_type": "Bearer", "expires_in": 3600})


def test_agent_gateway_answer_is_named_by_the_role_with_the_served_model_beside(caplog):
    post = FakePost(token(), FakeResponse(200, agent_answer()))
    c = agent_client(post)
    with caplog.at_level(logging.INFO, logger="sbmig.fix.client"):
        r = c.complete(MSGS, role="fix")
    assert r.model == "build_error_fixer" and r.served_models == ["gpt-5.5"]
    u = usage_from(r)
    assert u.model == "build_error_fixer" and u.served == ["gpt-5.5"] and u.calls == 1 and u.input_tokens == 120
    line = next(m for m in caplog.messages if m.startswith("llm call provider=company model="))
    assert "model=build_error_fixer in=120 out=30" in line and line.endswith("served=gpt-5.5")
    m = LLM_CALL_LINE.search(line.encode())                   # the admin's old expression still reads the new line
    assert m and m.group(1) == b"build_error_fixer" and m.group(2) == b"120"


def test_agent_gateway_escalation_and_answers_without_a_model():
    post = FakePost(token(), FakeResponse(200, agent_answer(top_model=None, served=("gpt-5.6-sol", "gpt-5.5"))))
    r = agent_client(post).complete(MSGS, model="escalation_fixer", role="fix_escalation")
    # before: the asset id (the agent payload's top-level `model`) named the call when the answer had no `model`
    assert r.model == "escalation_fixer" and r.served_models == ["gpt-5.6-sol", "gpt-5.5"]
    post = FakePost(token(), FakeResponse(200, {"choices": [{"message": {"content": "x"}}], "usage": {}}))
    r = agent_client(post).complete(MSGS)
    assert r.model == "build_error_fixer" and r.served_models == []


def test_legacy_gateway_names_the_asked_model_and_keeps_a_different_served_name():
    post = FakePost(FakeResponse(200, {"choices": [{"message": {"content": "hi"}}], "model": "m-1",
                                       "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
    r = CompanyClient("http://gw.local/chat", "k", "gpt-6-luna", post=post, sleep=lambda s: None).complete(MSGS)
    assert r.model == "gpt-6-luna" and r.served_models == ["m-1"]
    post = FakePost(FakeResponse(200, {"choices": [{"message": {"content": "hi"}}], "model": "GPT-6-luna"}))
    r = CompanyClient("http://gw.local/chat", "k", "gpt-6-luna", post=post, sleep=lambda s: None).complete(MSGS)
    assert r.model == "gpt-6-luna" and r.served_models == []       # the same name is not repeated as served


def test_openai_compatible_client_names_the_asked_model():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=agent_answer(top_model="unknown")))
    c = OpenAICompatibleClient("http://llm.local", "", "m-asked", transport=transport, sleep=lambda s: None)
    r = c.complete(MSGS)
    assert r.model == "m-asked" and r.served_models == ["gpt-5.5"]


def test_served_models_of_reads_real_names_only():
    assert served_models_of({"model": "unknown", "usage": {"by_model": [{"model": "a"}, {"model": "?"}, "x",
                                                                         {"model": "a"}, {"model": "b"}]}}) == ["a", "b"]
    assert served_models_of({"model": "gpt-x", "usage": {"by_model": {"model": "gpt-y"}}}) == ["gpt-x"]
    assert served_models_of({"data": {"model": "inner"}}) == []
    assert served_models_of(None) == [] and served_models_of([1]) == []
    for bad in ("", " ", "unknown", "UNKNOWN", "?", "none", "null", "-", "default", None, 3):
        assert real_model_name(bad) == ""
    assert real_model_name(" gpt-5.5 ") == "gpt-5.5"


@pytest.mark.parametrize("answer,model,served", [
    ({"content": "d"}, "m", []),
    ({"content": "d", "model": "unknown"}, "m", []),
    ({"content": "d", "model": "gw"}, "m", ["gw"]),
    (LLMResponse(content="d", model="o", served_models=["p"]), "m", ["o", "p"]),
    ("text", "m", []),
])
def test_adapter_names_the_asked_model(answer, model, served):
    class Plugin:
        def complete(self, request):
            return answer
    r = AdapterClient(Plugin(), default_model="m").complete(MSGS)
    assert r.model == model and r.served_models == served


def test_adapter_without_an_asked_model_keeps_a_real_reported_name_else_its_own_name():
    class Plugin:
        def __init__(self, answer):
            self.answer = answer

        def complete(self, request):
            return self.answer
    assert AdapterClient(Plugin({"content": "d", "model": "gw"})).complete(MSGS).model == "gw"
    assert AdapterClient(Plugin({"content": "d", "model": "unknown"})).complete(MSGS).model == "Plugin"


def test_usage_add_merges_served_as_a_set_and_old_json_still_loads():
    a = LLMUsage(model="build_error_fixer", calls=1, served=["gpt-5.5"])
    b = LLMUsage(model="build_error_fixer", calls=2, served=["gpt-5.6-sol", "gpt-5.5"])
    s = a.add(b)
    assert s.calls == 3 and s.served == ["gpt-5.5", "gpt-5.6-sol"] and s.model == "build_error_fixer"
    assert LLMUsage.model_validate({"model": "x", "calls": 1, "input_tokens": 2, "output_tokens": 3}).served == []
    # an empty `served` is not written, so a report without served models is byte-identical to before
    assert LLMUsage(model="x", calls=1).model_dump() == {"model": "x", "calls": 1, "input_tokens": 0,
                                                         "output_tokens": 0}
    assert s.model_dump()["served"] == ["gpt-5.5", "gpt-5.6-sol"]
    assert "served" not in ModelCost(model="x").model_dump()
    assert LLMUsage.model_validate_json(s.model_dump_json()) == s
