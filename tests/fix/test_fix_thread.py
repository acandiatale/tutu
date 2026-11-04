"""v2.1z-p: the case thread — with `llm.case_thread` the first ask of a case opens a gateway session (full prompt, no
session id), an augment continues the base attempt's session with the DELTA only (what the script did, the errors now, their
sources; no system prompt, no case header / rules / base code), context and feedback rounds send one user message in the
session, a rejected continuation (4xx) falls back to the full prompt in a new session, and the mode off keeps the stateless
chain. The gateway remembers the system prompt and the earlier turns (the user checked it on 2026-11-04)."""
from __future__ import annotations

import pytest

from sbmig.core.interfaces import LLMResponse
from sbmig.core.models import ScriptBase
from sbmig.fix import LLMError, LLMFixer

from .test_fix_answer import FILES, FULL, cluster, ctx

NEED = "<analysis>need more</analysis>\n<need_files>demo-back/src/main/java/a/S0.java</need_files>"


class SessionFake:
    """An LLM client that records (messages, metadata) and answers with session ids like the agent gateway."""

    def __init__(self, answers, sids):
        self.answers, self.sids, self.calls = list(answers), list(sids), []

    def complete(self, messages, *, model=None, max_tokens=None, temperature=None, stop=None, role=None, metadata=None, **_):
        self.calls.append({"messages": list(messages), "metadata": dict(metadata or {}), "model": model})
        text = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(text, Exception):
            raise text
        sid = self.sids.pop(0) if len(self.sids) > 1 else (self.sids[0] if self.sids else None)
        return LLMResponse(content=text, model="gpt-5.5", input_tokens=max(1, len(text) // 4), output_tokens=10,
                           session_id=sid)


@pytest.fixture()
def proj(ws, put):
    for i, f in enumerate(FILES):
        put(f, f"package a;\n\nimport javax.annotation.PostConstruct;\n\nclass S{i} {{}}\n")
    put("demo-back/pom.xml", "<project><artifactId>demo-back</artifactId></project>\n")
    return ws


def base(sid="s1") -> ScriptBase:
    return ScriptBase(case_id="abc123", version=1, code="def fix(p):\n    p.log('x')\n", outcome="no_change", status="ok",
                      remaining_errors=["demo-back/src/main/java/a/S0.java:3 package javax.annotation does not exist"],
                      session_id=sid)


def test_the_first_ask_opens_a_session_and_the_proposal_keeps_its_id(cfg, proj):
    cfg.llm.case_thread = True
    client = SessionFake([FULL], ["s1"])
    p = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    [call] = client.calls
    assert [m.role for m in call["messages"]] == ["system", "user"] and "session_id" not in call["metadata"]
    assert p.session_id == "s1" and p.code


def test_an_augment_continues_the_base_session_with_the_delta_only(cfg, proj):
    cfg.llm.case_thread = True
    client = SessionFake([FULL], ["s1"])
    p = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx(script_mode="augment", script_base=base("s1")))
    [call] = client.calls
    assert [m.role for m in call["messages"]] == ["user"] and call["metadata"]["session_id"] == "s1"
    turn = call["messages"][0].content
    assert turn.startswith("Same conversation, next turn for this case: your version 1 ran here and was rolled back")
    assert "## Errors of this case" in turn and "## What version 1 did in this project" in turn
    assert "## Case" not in turn and "## Script to extend" not in turn and "p.log('x')" not in turn   # remembered by the session
    assert "COMPLETE new script" in turn and p.session_id == "s1"


def test_context_rounds_send_one_message_in_the_session(cfg, proj):
    cfg.llm.case_thread = True
    client = SessionFake([NEED, FULL], ["s9", "s9"])
    LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    first, second = client.calls
    assert "session_id" not in first["metadata"]
    assert [m.role for m in second["messages"]] == ["user"] and second["metadata"]["session_id"] == "s9"
    assert "S0.java" in second["messages"][0].content


def test_a_rejected_continuation_falls_back_to_the_full_prompt_in_a_new_session(cfg, proj):
    cfg.llm.case_thread = True
    client = SessionFake([LLMError(404, "unknown session"), FULL], ["s2"])
    p = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx(script_mode="augment", script_base=base("stale")))
    rejected, full = client.calls
    assert rejected["metadata"]["session_id"] == "stale" and len(rejected["messages"]) == 1
    assert [m.role for m in full["messages"]] == ["system", "user"] and "session_id" not in full["metadata"]
    assert "## Script to extend (version 1)" in full["messages"][1].content and p.code and p.session_id == "s2"


def test_a_rejected_continuation_is_not_retried_when_the_fallback_is_off(cfg, proj):
    cfg.llm.case_thread, cfg.llm.thread_fallback_full = True, False
    client = SessionFake([LLMError(404, "unknown session"), FULL], ["s2"])
    p = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx(script_mode="augment", script_base=base("stale")))
    assert len(client.calls) == 1 and not p.code                 # the attempt reports the error, as any LLM failure


def test_thread_off_keeps_the_stateless_chain(cfg, proj):
    cfg.llm.case_thread = False
    client = SessionFake([NEED, FULL], ["s1", "s1"])
    LLMFixer(cfg, client).propose_script(proj, cluster(), ctx(script_mode="augment", script_base=base("s1")))
    first, second = client.calls
    assert [m.role for m in first["messages"]] == ["system", "user"] and "session_id" not in first["metadata"]
    assert [m.role for m in second["messages"]] == ["system", "user", "assistant", "user"]
    assert "session_id" not in second["metadata"]
