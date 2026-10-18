from pathlib import Path

import httpx
import pytest

from sbmig.core.interfaces import ChatMessage, FixContext, FixerLike, LLMRequest
from sbmig.core.models import FileChange, Phase
from sbmig.core.workspace import Workspace
from sbmig.fix import FakeLLMClient, LLMFixer
from sbmig.llm import AdapterClient, ApplyDelegatingFixer, PluginLoadError, resolve_fixer_plugin
from sbmig.loop import Orchestrator
from tests.fix.conftest import make_cluster
from tests.loop.fakes import journal_events

MOD = "tests.llm.plugins_fixture"
A = "src/main/java/a/A.java"


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "project"
    root.mkdir()
    return Workspace(root, tmp_path / "run", "t-run")


def ctx(**kw):
    return FixContext(phase=Phase.SPRING, iteration=2, profile_summary="case=spring_boot java=17", **kw)


def test_fixer_tags_calls_with_role_and_metadata(cfg, ws):
    ws.write(A, "package a;\n\npublic class A {}\n")
    client = FakeLLMClient()
    LLMFixer(cfg, client).propose(ws, make_cluster(file=A, cid="c-42"), ctx())
    assert client.calls[0]["role"] == "fix"
    assert client.calls[0]["metadata"] == {"phase": "spring", "iteration": 2, "cluster_id": "c-42"}

    cfg.llm.escalation_model = "big-model"
    client2 = FakeLLMClient()
    LLMFixer(cfg, client2).propose(ws, make_cluster(file=A), ctx(use_escalation_model=True))
    assert client2.calls[0]["role"] == "fix_escalation" and client2.calls[0]["model"] == "big-model"


def test_call_signature_backward_compatible(cfg):
    client = FakeLLMClient()
    fixer = LLMFixer(cfg, client)
    from sbmig.core.models import LLMUsage
    fixer._call([ChatMessage(role="user", content="x")], None, LLMUsage(), [])
    assert client.calls[0]["role"] == "fix" and client.calls[0]["metadata"] is None


def test_propose_only_plugin_gets_patch_applier(cfg, ws):
    ws.write("A.txt", "old text\n")
    cfg.fix.plugin = f"{MOD}:make_propose_only"
    cfg.fix.plugin_options = {"agent": "x"}
    llm = FakeLLMClient()
    fx = resolve_fixer_plugin(cfg, llm, notes_dir="notes")
    assert isinstance(fx, ApplyDelegatingFixer) and isinstance(fx, FixerLike)
    assert fx.inner.options == {"agent": "x"} and fx.inner.llm is llm and fx.inner.notes_dir == "notes"
    p = fx.propose(ws, make_cluster(file="A.txt"), ctx())
    applied, errs = fx.apply(ws, p.changes)
    assert errs == [] and len(applied) == 1 and ws.read("A.txt") == "new text\n"
    _, errs = fx.apply(ws, [FileChange(path="A.txt", op="modify", search="absent", replace="y")])
    assert errs


def test_fixer_plugin_none_and_errors(cfg):
    assert resolve_fixer_plugin(cfg, None) is None
    cfg.fix.plugin = f"{MOD}:EchoAdapter"
    with pytest.raises(PluginLoadError, match="propose"):
        resolve_fixer_plugin(cfg, None)
    cfg.fix.plugin = "plainname"
    with pytest.raises(PluginLoadError, match="sbmig.fixers"):
        resolve_fixer_plugin(cfg, None)


def test_orchestrator_uses_fixer_plugin_and_journals(cfg, ws):
    cfg.fix.plugin = f"{MOD}:ProposeOnly"
    orch = Orchestrator(cfg, ws, llm=FakeLLMClient())
    fx = orch.fixer
    ws.write("A.txt", "old\n")
    p = fx.propose(ws, make_cluster(file="A.txt"), ctx())
    assert p.model == "agent"
    assert fx.apply(ws, p.changes)[0]
    ev = [e for e in journal_events(ws) if e.get("event") == "fixer"]
    assert ev == [{**ev[0], "plugin": f"{MOD}:ProposeOnly"}] and "error" not in ev[0]


def test_orchestrator_fixer_plugin_without_llm(cfg, ws, monkeypatch):
    cfg.fix.plugin = f"{MOD}:make_propose_only"
    cfg.llm.provider = "openai_compatible"
    cfg.llm.base_url = "${LLM_BASE_URL}"
    monkeypatch.delenv(cfg.llm.api_key_env, raising=False)
    orch = Orchestrator(cfg, ws)
    ws.write("A.txt", "old\n")
    orch.fixer.propose(ws, make_cluster(file="A.txt"), ctx())
    assert orch.fixer._get().inner.llm is None


def test_example_agent_fixer(cfg, ws):
    from examples.adapters import agent_fixer

    ws.write(A, "package a;\n\nimport javax.servlet.Filter;\n")
    cfg.fix.plugin = "examples.adapters.agent_fixer:make_fixer"
    fx = resolve_fixer_plugin(cfg, None)
    seen = {}

    def run(task):
        seen.update(task)
        return {"analysis": "jakarta", "confidence": 0.9,
                "changes": [{"path": A, "search": "javax.servlet", "replace": "jakarta.servlet"}]}

    fx.agent.run = run
    p = fx.propose(ws, make_cluster(file=A), ctx())
    assert "Errors to fix" in seen["context"] and seen["cluster_id"] == "c1"
    applied, errs = fx.apply(ws, p.changes)
    assert not errs and "jakarta.servlet.Filter" in ws.read(A)
    fx.agent.run = lambda task: (_ for _ in ()).throw(RuntimeError("agent down"))
    assert fx.propose(ws, make_cluster(file=A), ctx()).analysis.startswith("llm-error:")
    assert isinstance(fx, agent_fixer.AgentFixer)


def test_example_gateway_adapter_routing_and_retry(cfg, monkeypatch):
    from examples.adapters.company_gateway_adapter import make_adapter

    seen: list[httpx.Request] = []
    answers = [httpx.Response(429, text="slow down"),
               httpx.Response(200, json={"model": "internal-coder-l", "usage": {"prompt_tokens": 5},
                                         "choices": [{"message": {"content": "patched"}, "finish_reason": "stop"}]})]

    def handler(req):
        seen.append(req)
        return answers.pop(0)

    cfg.llm.provider = "plugin"
    cfg.llm.plugin = "examples.adapters.company_gateway_adapter:make_adapter"
    cfg.llm.api_key_env = "SBMIG_GW_KEY"
    monkeypatch.setenv("SBMIG_GW_KEY", "gw-secret")
    cfg.llm.plugin_options = {"base_url": "http://gw.local/api", "models": {"fix": "internal-coder-l"},
                              "transport": httpx.MockTransport(handler), "max_retries": 2}
    from sbmig.fix import make_llm_client
    client = make_llm_client(cfg)
    assert isinstance(client, AdapterClient)
    client.adapter._sleep = lambda s: None
    resp = client.complete([ChatMessage(role="user", content="x")], role="fix", metadata={"cluster_id": "c1"})
    assert resp.content == "patched" and len(seen) == 2
    import json
    body = json.loads(seen[-1].content)
    assert body["model"] == "internal-coder-l" and body["metadata"]["cluster_id"] == "c1"
    assert seen[-1].headers["X-Api-Key"] == "gw-secret" and "gw-secret" not in repr(client.adapter)
    with pytest.raises(ValueError, match="API key"):
        monkeypatch.delenv("COMPANY_LLM_KEY", raising=False)
        make_adapter({"base_url": "u"})
    assert make_adapter({"base_url": "u", "api_key": "k"}).pick_model(
        LLMRequest(messages=[], role="plan_advisor")) == "internal-coder-m"
