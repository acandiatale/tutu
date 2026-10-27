# Adapter examples

Two ways to plug in-house infrastructure into sbmig (full reference: `docs/ADAPTERS.md`).

| File | Tier | What it shows |
|---|---|---|
| `company_gateway_adapter.py` | 1: LLM adapter | An `LLMAdapter` subclass for an OpenAI-style internal gateway. It sends an `X-Api-Key` header, maps requests and responses, retries 429/5xx with httpx, and routes models by role. |
| `agent_fixer.py` | 2: fixer | A script fixer (`propose_script`) that packs the error case with `ContextPacker.pack_script`, calls `MyAgent.run(task)` (a stub: replace it with your agent framework) and parses its `<analysis>` / `<script>` answer into a `ScriptProposal`. |

## Tier 1: your LLM gateway

```yaml
llm:
  provider: plugin
  plugin: examples.adapters.company_gateway_adapter:make_adapter
  api_key_env: COMPANY_LLM_KEY        # name of the env var only, never the key
  plugin_options:
    base_url: https://llm-gateway.internal.example/api
    default_model: internal-coder-m
    models: {fix: internal-coder-l, fix_escalation: internal-coder-xl, plan_advisor: internal-small,
             structure_review: internal-coder-l}
  record_dir: out/llm-records         # optional: plugin calls are recorded too (role + metadata, no secrets)
```

```bash
export COMPANY_LLM_KEY=...            # sbmig passes it to the factory as options["api_key"], in memory only
PYTHONPATH=. sbmig run path/to/project --config config/artifacts.company.yml --config my-llm.yml
```

`PYTHONPATH=.` is only needed for these repo-local examples. A real adapter lives in your own installed package.

### Entry-point alternative

In **your** package's `pyproject.toml`:

```toml
[project.entry-points."sbmig.llm_adapters"]
mygateway = "mypkg.adapter:make_adapter"
```

After `pip install -e .` into the sbmig venv, configure `llm.provider: mygateway`. You don't need `llm.plugin`.

## Tier 2: your coding agent

```yaml
fix:
  plugin: examples.adapters.agent_fixer:make_fixer
  plugin_options:
    agent: code-migrator
```

The factory gets `(options, cfg, llm)`, where `llm` is sbmig's configured LLM client or `None`. Replace
`MyAgent.run` with a call into your framework. The agent answers with one Python fix script per error case
(`def fix(p)` over sbmig's helper API) and must not edit files itself. sbmig checks the script, runs it in a separate
process against the project folder, commits, rebuilds and rolls back on regressions.

## Python embedding

```python
from sbmig.api import migrate, exit_code
from examples.adapters.company_gateway_adapter import make_adapter

report = migrate("path/to/project", configs=["config/artifacts.company.yml"],
                 llm=make_adapter({"base_url": "https://llm-gateway.internal.example/api"}))
raise SystemExit(exit_code(report))
```

## Rule: keys live only in environment variables

Never put a key in yml, `plugin_options`, code or a recording. Set `llm.api_key_env` to the *name* of the
variable. sbmig reads the value and hands it to your factory in memory. Recordings (`llm-NNNN.json`) contain
messages, responses, `role` and `metadata`, never headers or keys.
