"""Tier-2 fixer seam (`fix.plugin`): a plugin implements `propose_script(ws, cluster, ctx) -> ScriptProposal`
(ScriptFixerLike); a SEARCH/REPLACE-only `propose` fixer is refused. Plus the call tags of our own LLMFixer and the
example adapters in examples/adapters/."""
from pathlib import Path

import httpx
import pytest

from sbmig.core.interfaces import ChatMessage, FixContext, LLMRequest, ScriptFixerLike
from sbmig.core.models import Phase, ScriptProposal
from sbmig.core.workspace import Workspace
from sbmig.fix import FakeLLMClient, LLMFixer
from sbmig.llm import AdapterClient, PluginLoadError, resolve_fixer_plugin
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
    return FixContext(phase=Phase.VERIFY, iteration=2, profile_summary="case=spring_boot java=17", script_mode="new",
                      **kw)


def test_fixer_tags_calls_with_role_and_metadata(cfg, ws):
    ws.write(A, "package a;\n\npublic class A {}\n")
    client = FakeLLMClient()
    LLMFixer(cfg, client).propose_script(ws, make_cluster(file=A, cid="c-42"), ctx())
    assert client.calls[0]["role"] == "fix"
    md = client.calls[0]["metadata"]                       # round 10: + run_id / project (LLMRequest contract)
    assert {k: md[k] for k in ("phase", "iteration", "cluster_id", "script_mode")} == {
        "phase": "verify", "iteration": 2, "cluster_id": "c-42", "script_mode": "new"}
    assert "run_id" in md and "project" in md

    cfg.llm.escalation_model = "big-model"
    client2 = FakeLLMClient()
    LLMFixer(cfg, client2).propose_script(ws, make_cluster(file=A), ctx(use_escalation_model=True))
    assert client2.calls[0]["role"] == "fix_escalation" and client2.calls[0]["model"] == "big-model"


def test_call_signature_backward_compatible(cfg):
    client = FakeLLMClient()
    fixer = LLMFixer(cfg, client)
    from sbmig.core.models import LLMUsage
    fixer._call([ChatMessage(role="user", content="x")], None, LLMUsage(), [])
    assert client.calls[0]["role"] == "fix" and client.calls[0]["metadata"] is None


def test_script_plugin_is_built_with_options_llm_and_notes_dir(cfg, ws):
    cfg.fix.plugin = f"{MOD}:make_script_agent"
    cfg.fix.plugin_options = {"agent": "x"}
    llm = FakeLLMClient()
    fx = resolve_fixer_plugin(cfg, llm, notes_dir="notes")
    assert isinstance(fx, ScriptFixerLike) and not hasattr(fx, "apply")       # used as is, no wrapper
    assert fx.options == {"agent": "x"} and fx.llm is llm and fx.notes_dir == "notes"
    p = fx.propose_script(ws, make_cluster(file="A.txt"), ctx())
    assert isinstance(p, ScriptProposal) and p.code.startswith("def fix(p):") and fx.seen == [("c1", "new")]


def test_propose_only_plugin_is_refused(cfg):
    for spec in (f"{MOD}:ProposeOnly", f"{MOD}:EchoAdapter"):
        cfg.fix.plugin = spec
        with pytest.raises(PluginLoadError, match="propose_script"):
            resolve_fixer_plugin(cfg, None)


def test_fixer_plugin_none_and_errors(cfg):
    assert resolve_fixer_plugin(cfg, None) is None
    cfg.fix.plugin = "plainname"
    with pytest.raises(PluginLoadError, match="sbmig.fixers"):
        resolve_fixer_plugin(cfg, None)


def test_orchestrator_uses_fixer_plugin_and_journals(cfg, ws):
    cfg.fix.plugin = f"{MOD}:ScriptAgent"
    orch = Orchestrator(cfg, ws, llm=FakeLLMClient())
    p = orch.fixer.propose_script(ws, make_cluster(file="A.txt"), ctx())
    assert p.model == "agent" and p.code
    ev = [e for e in journal_events(ws) if e.get("event") == "fixer"]
    assert ev == [{**ev[0], "plugin": f"{MOD}:ScriptAgent"}] and "error" not in ev[0]


def test_orchestrator_fixer_plugin_without_llm(cfg, ws, monkeypatch):
    cfg.fix.plugin = f"{MOD}:make_script_agent"
    cfg.llm.provider = "openai_compatible"
    cfg.llm.base_url = "${LLM_BASE_URL}"
    monkeypatch.delenv(cfg.llm.api_key_env, raising=False)
    orch = Orchestrator(cfg, ws)
    orch.fixer.propose_script(ws, make_cluster(file="A.txt"), ctx())
    assert orch.fixer._get().llm is None


def test_example_agent_fixer(cfg, ws):
    from examples.adapters import agent_fixer

    ws.write(A, "package a;\n\nimport javax.servlet.Filter;\n\npublic class A {}\n")
    cfg.fix.plugin = "examples.adapters.agent_fixer:make_fixer"
    cfg.fix.plugin_options = {"agent": "migrator-x"}
    fx = resolve_fixer_plugin(cfg, None)
    assert isinstance(fx, agent_fixer.AgentFixer) and isinstance(fx, ScriptFixerLike) and fx.agent.name == "migrator-x"
    seen = {}
    script = "def fix(p):\n    for e in p.errors:\n        p.replace(e['file'], 'javax.servlet', 'jakarta.servlet')\n"

    def run(task):
        seen.update(task)
        return {"answer": f"<analysis>jakarta</analysis>\n<script>\n{script}</script>",
                "usage": {"model": "agent-m", "calls": 2, "input_tokens": 100, "output_tokens": 20}}

    fx.agent.run = run
    p = fx.propose_script(ws, make_cluster(file=A), ctx(case_id="k1"))
    assert p.code == script and p.analysis == "jakarta" and p.error == "" and p.model == "agent-m"
    assert p.llm_usage.calls == 2 and p.llm_usage.input_tokens == 100
    assert "Errors of this case" in seen["context"] and "import javax.servlet.Filter;" in seen["context"]
    assert seen["mode"] == "new" and seen["base_script"] == "" and seen["case_id"] == "k1"
    assert "def fix(p)" in seen["instructions"] and "p.replace" in seen["instructions"]
    # no usable script → empty code with the parser's reason; agent failure → llm-error (classified as transport)
    fx.agent.run = lambda task: {"answer": "<analysis>no safe fix</analysis>"}
    p = fx.propose_script(ws, make_cluster(file=A), ctx())
    assert p.code == "" and p.error == "no <script> block" and p.analysis == "no safe fix"
    fx.agent.run = lambda task: (_ for _ in ()).throw(RuntimeError("agent down"))
    p = fx.propose_script(ws, make_cluster(file=A), ctx())
    assert p.code == "" and p.analysis.startswith("llm-error:") and p.error == "agent failed (RuntimeError)"


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
