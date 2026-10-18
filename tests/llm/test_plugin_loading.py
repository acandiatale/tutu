import json
from types import SimpleNamespace

import pytest

from sbmig.core.interfaces import ChatMessage
from sbmig.fix import RecordingClient, make_llm_client
from sbmig.llm import AdapterClient, KwargFilterClient, PluginLoadError, load_plugin, resolve_llm_plugin
from tests.llm import plugins_fixture as pf

MSGS = [ChatMessage(role="user", content="hi")]
MOD = "tests.llm.plugins_fixture"


def test_load_plugin_spec_forms():
    assert load_plugin(f"{MOD}:EchoAdapter") is pf.EchoAdapter
    assert load_plugin(f"{MOD}.EchoAdapter") is pf.EchoAdapter
    with pytest.raises(PluginLoadError, match="cannot import"):
        load_plugin("no_such_pkg.mod:x")
    with pytest.raises(PluginLoadError, match="no attribute"):
        load_plugin(f"{MOD}:Nope")
    with pytest.raises(PluginLoadError):
        load_plugin("justaname")


def _plugin_cfg(cfg, attr, **opts):
    cfg.llm.provider = "plugin"
    cfg.llm.plugin = f"{MOD}:{attr}"
    cfg.llm.plugin_options = dict(opts)
    return cfg


@pytest.mark.parametrize("attr,expect_opts", [
    ("EchoAdapter", {"k": 1}),            # class (options, cfg)
    ("factory_two", {"k": 1}),
    ("factory_options_only", {"k": 1}),
    ("factory_none", {"from": "none"}),
    ("INSTANCE", {"instance": True}),     # already built
])
def test_resolve_llm_plugin_signatures(cfg, monkeypatch, attr, expect_opts):
    monkeypatch.delenv(cfg.llm.api_key_env, raising=False)
    client = resolve_llm_plugin(_plugin_cfg(cfg, attr, k=1))
    assert isinstance(client, AdapterClient)
    assert client.adapter.options == expect_opts
    assert client.complete(MSGS, role="fix").content == "echo:hi"


def test_class_factory_gets_cfg_and_api_key_from_env(cfg, monkeypatch):
    monkeypatch.setenv("SBMIG_PLUGIN_KEY", "secret-value")
    cfg.llm.api_key_env = "SBMIG_PLUGIN_KEY"
    client = resolve_llm_plugin(_plugin_cfg(cfg, "factory_two", base="x"))
    assert client.adapter.cfg is cfg
    assert client.adapter.options == {"base": "x", "api_key": "secret-value"}
    assert "api_key" not in cfg.llm.plugin_options        # config untouched


def test_other_adapter_shapes(cfg):
    assert resolve_llm_plugin(_plugin_cfg(cfg, "NoArgAdapter")).complete(MSGS).content == "noarg"
    assert resolve_llm_plugin(_plugin_cfg(cfg, "DuckAdapter")).complete(MSGS).content == "duck"
    assert resolve_llm_plugin(_plugin_cfg(cfg, "MarkedAdapter")).complete(MSGS).content == "marked"
    c = resolve_llm_plugin(_plugin_cfg(cfg, "MessageClient"))
    assert isinstance(c, KwargFilterClient)
    assert c.complete(MSGS, model="x", role="fix", metadata={"a": 1}).content == "client"
    assert c.inner.kw == [{"model": "x"}]


def test_bad_plugins_raise_plugin_load_error(cfg):
    with pytest.raises(PluginLoadError, match="requires 3 positional"):
        resolve_llm_plugin(_plugin_cfg(cfg, "needs_three"))
    with pytest.raises(PluginLoadError, match="failed to initialise.*missing base_url"):
        resolve_llm_plugin(_plugin_cfg(cfg, "broken_factory"))
    with pytest.raises(PluginLoadError, match="neither a class"):
        resolve_llm_plugin(_plugin_cfg(cfg, "SEEN"))
    with pytest.raises(PluginLoadError, match="has no complete"):
        resolve_llm_plugin(_plugin_cfg(cfg, "returns_junk"))
    cfg.llm.plugin = None
    with pytest.raises(PluginLoadError, match="llm.plugin is not set"):
        resolve_llm_plugin(cfg)


def _fake_eps(mapping):
    def entry_points(group=None, **kw):
        if group != "sbmig.llm_adapters":
            return []
        return [SimpleNamespace(name=n, value=v, load=(lambda v=v: load_plugin(v))) for n, v in mapping.items()]
    return entry_points


def test_entry_point_lookup(cfg, monkeypatch):
    monkeypatch.setattr("importlib.metadata.entry_points", _fake_eps({"mygateway": f"{MOD}:factory_two"}))
    cfg.llm.provider = "mygateway"
    cfg.llm.plugin_options = {"via": "ep"}
    client = make_llm_client(cfg)
    assert isinstance(client, AdapterClient) and client.adapter.options["via"] == "ep"


def test_unknown_provider_error_names_both_registration_ways(cfg, monkeypatch):
    monkeypatch.setattr("importlib.metadata.entry_points", _fake_eps({"other": f"{MOD}:factory_two"}))
    cfg.llm.provider = "corp-gw"
    cfg.llm.plugin = None
    with pytest.raises(PluginLoadError) as ei:
        make_llm_client(cfg)
    msg = str(ei.value)
    assert "corp-gw" in msg and "llm.provider: plugin" in msg and "sbmig.llm_adapters" in msg
    assert "installed: other" in msg
    assert isinstance(ei.value, ValueError)


def test_make_llm_client_plugin_recording_has_role(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv(cfg.llm.api_key_env, raising=False)
    _plugin_cfg(cfg, "EchoAdapter")
    cfg.llm.record_dir = str(tmp_path / "rec")
    client = make_llm_client(cfg)
    assert isinstance(client, RecordingClient) and isinstance(client.inner, AdapterClient)
    client.complete(MSGS, role="plan_advisor", metadata={"phase": "plan"})
    rec = json.loads(next((tmp_path / "rec").glob("llm-*.json")).read_text())
    assert rec["role"] == "plan_advisor" and rec["metadata"] == {"phase": "plan"} and rec["response"] == "echo:hi"


def test_plugin_needs_no_api_key(cfg, monkeypatch):
    cfg.llm.api_key_env = "SBMIG_DEFINITELY_UNSET"
    monkeypatch.delenv("SBMIG_DEFINITELY_UNSET", raising=False)
    client = make_llm_client(_plugin_cfg(cfg, "EchoAdapter"))
    assert "api_key" not in client.adapter.options
