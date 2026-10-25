# Adapters: plugging in an in-house LLM gateway or coding agent

sbmig has two adapter tiers. Pick the one that matches what you already have.

| Tier | Replaces | You implement | Config |
|---|---|---|---|
| 1. LLM adapter | our clients (`company` = requests.post to the gateway, `openai_compatible`, `anthropic`) | `complete(request: LLMRequest) -> LLMResponse` | `llm.provider: plugin` + `llm.plugin` (or an entry point) |
| 2. Fixer adapter | `LLMFixer` (our SEARCH/REPLACE protocol over a chat completion) | `propose(ws, cluster, ctx) -> FixProposal` [+ `apply`] | `fix.plugin` |

With tier 1, sbmig keeps building the prompts, parsing the patches, validating them and running the feedback
rounds. Your adapter only moves one request to your gateway and brings back the text.
With tier 2, your agent decides what to change for one error cluster. sbmig still owns clustering, triage,
deterministic handlers, the KB, budgets, git commits and rollbacks, rebuilds and the report.

The two tiers can be combined. A tier-2 fixer receives sbmig's LLM client (your tier-1 adapter, if one is
configured) as its `llm` argument.

Working examples: `examples/adapters/` (`company_gateway_adapter.py`, `agent_fixer.py`, README).
Example config: `config/llm-plugin.example.yml`.

## Contracts (`sbmig/core/interfaces.py`, `sbmig/core/models.py`)

```python
class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

class LLMRequest(BaseModel):
    messages: list[ChatMessage]
    model: Optional[str] = None          # llm.model / recipe_model / escalation_model, or None = your default
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None  # None = your default
    stop: Optional[list[str]] = None
    role: LLMRole = "other"              # see the role table below
    metadata: dict = {}                  # run_id, project, phase, iteration, cluster_id, ... (never secrets)

class LLMResponse(BaseModel):
    content: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: Optional[str] = None

class LLMAdapterLike(Protocol):          # tier 1
    def complete(self, request: LLMRequest) -> LLMResponse: ...

class FixerLike(Protocol):               # tier 2
    def propose(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext) -> FixProposal: ...
    def apply(self, ws: Workspace, changes: list[FileChange]) -> tuple[list[FileChange], list[str]]: ...

class FixContext(BaseModel):
    phase: Phase; iteration: int; stall_level: int = 0
    profile_summary: str = ""; plan_notes: list[str] = []; kb_hints: list[str] = []
    previous_attempts: list[FixAttempt] = []; other_errors_summary: str = ""
    use_escalation_model: bool = False

class FileChange(BaseModel):             # modify = search/replace (search must match exactly once)
    path: str
    op: Literal["modify", "create", "delete", "rename"]
    search: Optional[str] = None; replace: Optional[str] = None
    content: Optional[str] = None; new_path: Optional[str] = None

class FixProposal(BaseModel):
    changes: list[FileChange]
    analysis: str = ""; confidence: float = 0.5; model: str = ""; prompt_hash: str = ""
    llm_usage: LLMUsage = LLMUsage(); requested_files: list[str] = []; raw_response: str = ""
```

A message-based `LLMClientLike` (`complete(messages, *, model, max_tokens, temperature, stop, role, metadata)`)
also works as a tier-1 plugin. If its `complete` does not accept `role`/`metadata`, sbmig drops those keywords
for you.

Optional base class: `sbmig.llm.LLMAdapter` (abstract `complete(request)`, `name` property, no-op `close()`).

## Tier 1: resolution order

`make_llm_client(cfg)` picks the client like this:

1. `llm.provider` is `company` (default), `openai_compatible`, `anthropic` or `fake`: a builtin client
   (`api_key_env` names the environment variable holding the key). `company` is the in-house gateway done the
   plain way — `requests.post(LLM_BASE_URL, headers={Authorization: Bearer LLM_API_KEY}, json={model, messages,
   need_origin}, timeout=…)` (`sbmig/fix/company.py`); URL / key / model / optional `LLM_RESPONSE_PATH` come from
   `.env`, nothing else to configure. `sbmig llm-probe` shows the exact request and the raw response.
2. `llm.provider: plugin`: load `llm.plugin` (`"pkg.module:attr"`; `"pkg.module.attr"` also works).
3. Any other provider name: the entry point with that name in group `sbmig.llm_adapters`. If there is none,
   `llm.plugin` is used when set. Otherwise loading fails with `PluginLoadError`, which names the provider and
   both ways to register it.

The loaded object can be:

* a **class or factory**: called with as many of `(options, cfg)` as its signature takes. `(options, cfg)`,
  `(options)` and `()` all work.
* an **instance** that already has `complete`: used as is.

`options` is `llm.plugin_options`. When the environment variable named by `llm.api_key_env` is set, its value
is added as `options["api_key"]`. This happens in memory only: the key is never written to config, records or
the journal. Plugins don't need a key, so a missing variable is not an error.

sbmig wraps the result in `AdapterClient` when it is an adapter. It tells an adapter from a client by checking,
in order:

1. a subclass of `sbmig.llm.LLMAdapter`;
2. `adapter_kind = "request"` (or `"client"`);
3. the first parameter of `complete` is named `request` or annotated `LLMRequest`.

Anything else is treated as a message-based client.

Entry-point registration, in **your** package's `pyproject.toml`:

```toml
[project.entry-points."sbmig.llm_adapters"]
mygateway = "mypkg.adapter:make_adapter"
```

Then `pip install` your package into the sbmig environment and set `llm.provider: mygateway`.

### What sbmig guarantees (tier 1)

* **Recording**: when `llm.record_dir` is set, every call goes through `RecordingClient`, including plugin
  calls. Each call writes `llm-NNNN.json` with the messages, response, model, usage, latency, `role` and
  `metadata`, and journals an `llm_call` event. Recordings can be replayed with `provider: fake` +
  `base_url: replay:<dir>`.
* **Budgets**: `loop.max_llm_calls`, `loop.max_llm_attempts_per_cluster` and `fleet.max_llm_calls_total` still apply.
  They count calls; a tier-2 `propose()` is charged `max(1, proposal.llm_usage.calls)`, so fill `llm_usage` when your
  agent makes several calls and you want the budget to reflect them.
* **Validation**: the result is coerced to `LLMResponse`. A dict with `content`, a `str`, or an object with
  `.content` are accepted. Anything else raises `LLMError`. An empty `model` is filled in with the requested
  model.
* **Error mapping**: any exception your adapter raises becomes `sbmig.fix.client.LLMError` (with `status` kept
  when the exception has one) whose text reads `LLM adapter <name> failed (status=<status>, cause=<ExceptionClass>):
  …`. The build-fix loop classifies failures by these explicit tokens only (round 10): `status=None|401|403|404|408|
  429|5xx`, `cause=…` or a known transport exception class name count as transport / configuration failures; two in a
  row pause the loop once for `llm.cooldown_seconds`, the next streak marks the LLM unavailable (`llm_unavailable`) —
  from then on the clusters that needed it are reported as blocked, and handlers and recipes keep running.
  `status=400|413|422`, a content filter and `finish_reason=length` are rejections of that one request and never
  count; the same HTTP 400 body for two different prompts is a configuration error (flag `llm-config-error`, no more
  LLM calls in the run).

### What the plugin must do (tier 1)

* Return an `LLMResponse`, or raise. Never return an error text as `content`.
* Handle retries, rate limiting, auth and timeouts itself. sbmig's HTTP retry logic only covers the builtin
  clients.
* Never log or persist the key. Read it from `options["api_key"]` or from your own environment variable.

## Roles

| `role` | Caller | Model sbmig asks for |
|---|---|---|
| `fix` | `LLMFixer` (build-fix loop) | `llm.model` |
| `fix_escalation` | `LLMFixer` after a stall (`ctx.use_escalation_model`) | `llm.escalation_model` (else `llm.model`) |
| `plan_advisor` | plan advisor (`plan.advisor: llm`) | `llm.recipe_model` (else `llm.model`) |
| `recipe_candidate` | KB harvest: recipe candidate generation | `llm.recipe_model` (else `llm.model`) |
| `other` | anything else | as passed |

Unknown roles become `other`, with the original value kept in `metadata["role_detail"]`. Fixer calls carry
`metadata = {"run_id", "project", "phase", "iteration", "cluster_id"}` (`run_id` / `project` since round 10;
`project` is the workspace directory name). Your adapter can route on `request.role` to override the model, as
`company_gateway_adapter.py` does with its `models` table.

## Tier 2: fixer plugin

`fix.plugin: "pkg.module:attr"` (a bare name is looked up in the entry-point group `sbmig.fixers`). It works as
follows:

* The plugin can be a class, a factory, or an instance with `propose`. It is called with as many of
  `(options, cfg, llm)` as it takes, where `options` = `fix.plugin_options` and `llm` = sbmig's LLM client.
  `llm` is `None` when no provider is configured or the provider fails to load. A `notes_dir` keyword is passed
  when the factory declares one.
* If the object has no `apply`, sbmig adds one that delegates to `sbmig.fix.PatchApplier(ws).apply(changes)`.
  It is atomic per file, and `modify` blocks must match exactly once. So an agent that only proposes
  `FileChange`s works.
* The fixer is created lazily, the first time a cluster needs an LLM fix. sbmig journals
  `{"event": "fixer", "plugin": spec}` at that point, or the same event with `"error"` if loading fails.
* The context we give our LLM is available to you: `ContextPacker(cfg).pack(ws, cluster, ctx,
  ctx.profile_summary)` returns a `PackedContext` (`.sections`, `.files_included`, `.render()`, which gives
  markdown).
* Return `FixProposal(changes=[], analysis="llm-error: ...")` (or raise) when your agent is unreachable. Since round
  10 such a text counts as a transport failure (cool-down, then `llm_unavailable` after repeats) only when it carries
  an explicit marker: `status=None` / `status=<401|403|404|408|429|5xx>`, `cause=<ExceptionClass>: …`, or the
  exception class name itself (`ConnectionError`, `ReadTimeout`, …); a raised exception is classified by its class
  name and message the same way. Anything else is an ordinary failed attempt of that cluster. An empty `changes`
  with a normal analysis means "no safe fix".
* Don't edit files in `propose`. Return changes, so that sbmig can apply, commit, rebuild and roll back.

## Embedding: `sbmig.api`

```python
from sbmig.api import migrate, exit_code
from mypkg.adapter import MyGatewayAdapter       # an LLMAdapter subclass

report = migrate("path/to/project", configs=["config/artifacts.company.yml"], out="out",
                 llm=MyGatewayAdapter(...),       # adapter or client; adapters are wrapped automatically
                 fixer=None,                      # or your FixerLike (overrides fix.plugin)
                 kb=None,                         # None/True = file KB, False = no KB, or a KnowledgeBaseLike
                 stop_after=None, dry_run=False)
print(report.success, exit_code(report))         # 0 ok / 2 incomplete / 3 aborted
```

`overrides={...}` is merged like CLI options (for example `{"llm": {"record_dir": "out/rec"}}`).
`components={...}` passes extra `Orchestrator` collaborators (`build_runner`, `rewrite_runner`, ...).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `PluginLoadError: cannot import plugin module` | The package is not installed in sbmig's venv and not on `PYTHONPATH`. The `sbmig` console script does not add the current directory, so run `PYTHONPATH=. sbmig run ...` for the repo-local `examples.adapters.*`. |
| `unknown llm.provider 'x' ... no entry point` | The entry point is not installed (`pip install -e yourpkg`), or the name differs. The message lists the installed names. |
| `requires N positional argument(s)` | Your factory takes more than `(options, cfg)` (tier 1) or `(options, cfg, llm)` (tier 2). Give the extra parameters defaults. |
| `failed to initialise: ...` | Your factory raised. Usually a missing option such as `base_url`, or a missing key env var. |
| `LLM adapter X returned int, expected LLMResponse` | Return an `LLMResponse`, a dict with `content`, or a `str`. |
| Run reports `llm-unavailable` | Two transport / configuration failures in a row after the one cool-down. Check the `llm_call` / `llm_fix` journal events, the `status=` / `cause=` part of the error and the `error` field in `llm-NNNN.json`. |
| Run reports `llm-config-error` | The gateway answered the same HTTP 400 body to two different prompts (model name, a payload field). The follow-up quotes the body; fix the setting in `.env` / `llm.*` and rerun. |
| Gateway gets `role: other` | The call site predates role tagging. Route on `metadata` instead, or default to your general model. |
| Fixer plugin is never called | Every cluster was resolved by handlers, recipes or KB patch replay first. That is expected. Look for `fixer` / `llm_fix` events in `journal.jsonl`. |
