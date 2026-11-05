"""v2.1z-p: the case thread — with `llm.case_thread` the first ask of a case opens a gateway session (full prompt, no
session id), an augment continues the base attempt's session with the DELTA only (what the script did, the errors now, their
sources; no system prompt, no case header / rules / base code), context and feedback rounds send one user message in the
session, a rejected continuation (4xx) falls back to the full prompt in a new session, and the mode off keeps the stateless
chain. The gateway remembers the system prompt and the earlier turns (the user checked it on 2026-11-04).

v2.1z-x: a gateway session cannot change its model (the user, 2026-11-05) — an augment that goes to another model (the escalation
model from the case's third ask / after a stall, or back to the base model) does not continue the session: the full prompt opens
a new one, and the attempt records the model its session runs on so the next rung continues only with the same model."""
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


def base(sid="s1", model=None) -> ScriptBase:
    """A base attempt of this run: its session `sid` was opened with `model` ("" / None = the gateway default)."""
    return ScriptBase(case_id="abc123", version=1, code="def fix(p):\n    p.log('x')\n", outcome="no_change", status="ok",
                      remaining_errors=["demo-back/src/main/java/a/S0.java:3 package javax.annotation does not exist"],
                      session_id=sid, session_model=model or "")


def test_the_first_ask_opens_a_session_and_the_proposal_keeps_its_id(cfg, proj):
    cfg.llm.case_thread = True
    client = SessionFake([FULL], ["s1"])
    p = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    [call] = client.calls
    assert [m.role for m in call["messages"]] == ["system", "user"] and "session_id" not in call["metadata"]
    assert p.session_id == "s1" and p.code
    assert p.session_model == (cfg.llm.model or "")           # v2.1z-x: the next rung continues only with this model


def test_an_augment_continues_the_base_session_with_the_delta_only(cfg, proj):
    cfg.llm.case_thread = True
    client = SessionFake([FULL], ["s1"])
    p = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx(script_mode="augment", script_base=base("s1", cfg.llm.model)))
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
    p = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx(script_mode="augment", script_base=base("stale", cfg.llm.model)))
    rejected, full = client.calls
    assert rejected["metadata"]["session_id"] == "stale" and len(rejected["messages"]) == 1
    assert [m.role for m in full["messages"]] == ["system", "user"] and "session_id" not in full["metadata"]
    assert "## Script to extend (version 1)" in full["messages"][1].content and p.code and p.session_id == "s2"


def test_a_rejected_continuation_is_not_retried_when_the_fallback_is_off(cfg, proj):
    cfg.llm.case_thread, cfg.llm.thread_fallback_full = True, False
    client = SessionFake([LLMError(404, "unknown session"), FULL], ["s2"])
    p = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx(script_mode="augment", script_base=base("stale", cfg.llm.model)))
    assert len(client.calls) == 1 and not p.code                 # the attempt reports the error, as any LLM failure


def test_thread_off_keeps_the_stateless_chain(cfg, proj):
    cfg.llm.case_thread = False
    client = SessionFake([NEED, FULL], ["s1", "s1"])
    LLMFixer(cfg, client).propose_script(proj, cluster(), ctx(script_mode="augment", script_base=base("s1")))
    first, second = client.calls
    assert [m.role for m in first["messages"]] == ["system", "user"] and "session_id" not in first["metadata"]
    assert [m.role for m in second["messages"]] == ["system", "user", "assistant", "user"]
    assert "session_id" not in second["metadata"]


# ---------------------------------------------------------------- v2.1z-x: a session keeps its model
def test_a_model_change_opens_a_new_session_with_the_full_prompt(cfg, proj):
    """The case's third ask goes to the escalation model: the base session (opened with llm.model) is NOT continued."""
    cfg.llm.case_thread, cfg.llm.escalation_model = True, "escalation_fixer"
    client = SessionFake([FULL], ["s2"])
    p = LLMFixer(cfg, client).propose_script(
        proj, cluster(), ctx(script_mode="augment", script_base=base("s1", cfg.llm.model), use_escalation_model=True))
    [call] = client.calls
    assert [m.role for m in call["messages"]] == ["system", "user"] and "session_id" not in call["metadata"]
    assert call["model"] == "escalation_fixer"
    assert "## Script to extend (version 1)" in call["messages"][1].content      # the full augment prompt, not the delta
    assert p.code and p.session_id == "s2" and p.session_model == "escalation_fixer"


def test_the_next_augment_on_the_same_escalation_model_continues_its_session(cfg, proj):
    cfg.llm.case_thread, cfg.llm.escalation_model = True, "escalation_fixer"
    client = SessionFake([FULL], ["s2"])
    p = LLMFixer(cfg, client).propose_script(
        proj, cluster(), ctx(script_mode="augment", script_base=base("s2", "escalation_fixer"), use_escalation_model=True))
    [call] = client.calls
    assert [m.role for m in call["messages"]] == ["user"] and call["metadata"]["session_id"] == "s2"
    assert call["model"] == "escalation_fixer" and p.session_id == "s2" and p.session_model == "escalation_fixer"


def test_escalation_without_an_escalation_model_keeps_the_session(cfg, proj):
    """No llm.escalation_model → the escalated ask runs on the same model, so the session goes on."""
    cfg.llm.case_thread, cfg.llm.escalation_model = True, None
    client = SessionFake([FULL], ["s1"])
    LLMFixer(cfg, client).propose_script(
        proj, cluster(), ctx(script_mode="augment", script_base=base("s1", cfg.llm.model), use_escalation_model=True))
    [call] = client.calls
    assert [m.role for m in call["messages"]] == ["user"] and call["metadata"]["session_id"] == "s1"


def test_falling_back_to_the_base_model_also_opens_a_new_session(cfg, proj):
    """A stall ended: the ask goes back to llm.model while the base session runs on the escalation model → new session."""
    cfg.llm.case_thread, cfg.llm.escalation_model = True, "escalation_fixer"
    client = SessionFake([FULL], ["s3"])
    p = LLMFixer(cfg, client).propose_script(
        proj, cluster(), ctx(script_mode="augment", script_base=base("s2", "escalation_fixer"), use_escalation_model=False))
    [call] = client.calls
    assert [m.role for m in call["messages"]] == ["system", "user"] and "session_id" not in call["metadata"]
    assert p.session_id == "s3" and p.session_model == (cfg.llm.model or "")
