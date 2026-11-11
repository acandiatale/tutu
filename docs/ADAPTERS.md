# Adapters: plugging in an in-house LLM gateway or coding agent

sbmig has a built-in client for the company gateway (`llm.provider: company`) and two plugin seams (invariant 4).

| Tier | Replaces | You implement | Config |
|---|---|---|---|
| 1. LLM adapter | the built-in clients (`company`, `openai_compatible`, `anthropic`) | `complete(request: LLMRequest) -> LLMResponse` | `llm.provider: plugin` + `llm.plugin` (or an entry point) |
| 2. Fixer adapter | `LLMFixer` (sbmig's fix-script prompts over a chat completion) | `propose_script(ws, cluster, ctx) -> ScriptProposal` | `fix.plugin` (CLI `--fix-plugin`) |

With tier 1, sbmig keeps building the prompts, parsing the answers and running the context / feedback rounds; your
adapter only moves one request to your gateway and brings back the text. With tier 2, your agent answers one error
case with a fix script and / or edits; sbmig still owns clustering, triage, the KB, budgets, the script runtime, git
commits and rollbacks, rebuilds and the report. A tier-2 fixer receives sbmig's LLM client (your tier-1 adapter, if
configured) as its `llm` argument.

Working examples: `examples/adapters/` (`company_gateway_adapter.py`, `agent_fixer.py`, README). Example config:
`config/llm-plugin.example.yml`. Loading code: `sbmig/llm/registry.py`.

## The built-in company client (`llm.provider: company`, default)

`sbmig/fix/company.py`: one plain `requests.post(url, headers={Authorization: Bearer …}, json=…, timeout=…)` per
call — no yml profile, no LangChain, no session pool. Values come from `.env` (git-ignored; read from the package
folder and the cwd up to the git root, nearest first; shell variables win) through `${VAR}` placeholders in
`config/default.yml`. Keys are read from the environment only and never written to yml, logs or records.

| Variable | Config key | Meaning |
|---|---|---|
| `LLM_BASE_URL` | `llm.base_url` | the full endpoint URL, used verbatim (`{asset_id}` is filled in agent mode) |
| `LLM_API_KEY` | `llm.api_key_env` names it | bearer key (or the app token for the token exchange) |
| `LLM_MODEL` | `llm.model` | fix model (agent mode: the `params.role` value, e.g. `build_error_fixer`) |
| `LLM_ESCALATION_MODEL` | `llm.escalation_model` | stronger model (third ask of a case, or a stall); empty → `LLM_MODEL` |
| `LLM_RECIPE_MODEL` | `llm.recipe_model` | the optional plan advisor's model |
| `LLM_RESPONSE_PATH` | `llm.response_path` | where the reply text sits in the JSON, only when auto-detection fails (`data.result.content`) |
| `LLM_RECORD_DIR` | `llm.record_dir` | one `llm-NNNN.json` per call (no keys / headers) |
| `LLM_GATEWAY` | `llm.gateway` | `legacy` (default) or `agent` |
| `LLM_ASSET_ID` | `llm.asset_id` | agent mode: the top-level `model` |
| `LLM_TOKEN_URL` | `llm.token_url` | agent mode: token exchange endpoint (empty = static key) |
| `LLM_APP_TOKEN` | `llm.app_token_env` names it | app token for the exchange (unset → `LLM_API_KEY`) |

**`llm.gateway: legacy`** — body `{model: LLM_MODEL, messages, **llm.payload_extra}` (default `need_origin: true`);
max_tokens / temperature are left to the gateway. The reply is found at `choices[0].message.content`, `content`,
`response`, `data.content`, … or `LLM_RESPONSE_PATH`.

**`llm.gateway: agent`** (the company GPT agent gateway) — body `{model: LLM_ASSET_ID, messages, stream: false,
async: false, params: {role: <LLM_MODEL or LLM_ESCALATION_MODEL>}, trace_id, max_tokens, temperature,
**llm.agent_extra}`; the gateway maps the role names to its models. With `LLM_TOKEN_URL` set, `TokenManager` first
exchanges the app token for an access token, refreshes it `llm.token_refresh_margin_s` (300 s) before expiry and
once more after a 401 (`llm.auth_retries`). **Sessions:** sbmig sends no `session_id` (and never `thread_id`); the
gateway creates a session and returns its id (`LLMResponse.session_id`). A call that passes
`metadata["session_id"]` continues that session.

**Case thread** (`llm.case_thread`, default false; agent mode only): a case's augments continue the base attempt's
session and send only the delta (`LLMFixer.build_thread_turn`: the base script's result, the current errors, source
excerpts, pom, version facts); context / feedback rounds send one message in the session. An open session cannot
change its model, so the escalation step and the way back open a new session with the full prompt (the session's
model travels as `ScriptProposal.session_model` → `FixAttempt.llm_session_model`). A rejected continuation falls back
to the full prompt (`llm.thread_fallback_full`). Every turn logs `sent N chars → prompt_tokens=T` to measure billing.

**Transport:** transport errors and retryable statuses are retried `llm.max_retries` (3) times; a 429 / 5xx with
`Retry-After` is "busy" and queued for up to `llm.busy_wait_seconds` (600). The failure text carries
`status=<code>` / `cause=<ExceptionClass>: …`, which the loop classifies (below).

## `sbmig llm-probe` — run it first on a new gateway

Prints the resolved provider / model / URL / key variable name, the exact payload, the token exchange, the sent
headers (token masked), the proxies in effect, the HTTP status and response JSON, the extracted reply, and a last
line `LLM-PROBE ok …` / `LLM-PROBE FAIL <kind> …`. Options: `--prompt`, `--model`, `--llm-provider`,
`--payload-extra '<json>'` (replaces `payload_extra`), `--no-system`, `--bare` (the same payload via plain
`requests.post`, bypassing sbmig's client). The fleet preflight makes the same call.

## Offline: the replay provider

`llm.provider: fake` + `llm.base_url: replay:<dir>` answers from `<dir>/llm-NNN.json` (`response`, optional
`replay_role: fix | structure_review | verify_guide`, optional `match` regex, e.g. `match: "case:<case_id>"`) instead
of by position. The fixtures' `sbmig.e2e.yml` use it, and recordings from `llm.record_dir` can be replayed the same
way. `config/llm-anthropic.yml` is kept for Anthropic validation runs (key in `LLM_API_KEY`).

## Contracts (`sbmig/core/interfaces.py`, `sbmig/core/models.py`)

```python
class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

class LLMRequest(BaseModel):
    messages: list[ChatMessage]
    model: Optional[str] = None          # llm.model / escalation_model / recipe_model, or None = your default
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    stop: Optional[list[str]] = None
    role: LLMRole = "other"              # fix | fix_escalation | plan_advisor | structure_review | verify_guide | other
    metadata: dict = {}                  # run_id, project, phase, cluster_id, case_id, session_id … (never secrets)

class LLMResponse(BaseModel):
    content: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: Optional[str] = None
    session_id: Optional[str] = None     # the gateway session the answer came from, if any

class LLMAdapterLike(Protocol):          # tier 1
    def complete(self, request: LLMRequest) -> LLMResponse: ...

class FixerLike(Protocol):               # tier 2
    def propose_script(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext) -> ScriptProposal: ...

class ScriptProposal(BaseModel):         # the answer
    code: str = ""                       # the <script> body defining `def fix(p)`; "" = no script
    analysis: str = ""; solution: str = ""
    edits: list[Edit] = []               # applied directly to this project
    changes: list[EditNote] = []         # what the fix modifies where
    no_script_reason: str = ""           # then `edits` is the whole fix (recorded as edits, never stored)
    verify: str = ""                     # operator check after a major upgrade / library replacement
    blocked_reason: str = ""             # the case cannot be fixed inside the project → declined
    error: str = ""                      # transport / parse failure (never raise)
    model: str = ""; llm_usage: LLMUsage; session_id: Optional[str]; session_model: str = ""; ...
```

`FixContext` carries `script_mode` (`new` | `augment`), `case_id`, `script_base` (augment: the version to extend and
what it did in this project), `profile_summary`, `plan_notes`, `layout_facts`, `kb_hints`, `advisory_hints`,
`library_hints`, `previous_attempts`, `other_errors_summary`, `reference_files` and `use_escalation_model`.

A message-based `LLMClientLike` (`complete(messages, *, model, max_tokens, temperature, stop, role, metadata)`) also
works as a tier-1 plugin; if its `complete` does not accept `role` / `metadata`, sbmig drops them. Optional base
class: `sbmig.llm.LLMAdapter` (abstract `complete(request)`, `name`, no-op `close()`).

## Tier 1: resolution order

`make_llm_client(cfg)` (`sbmig/fix/client.py`):

1. `llm.provider` is `company`, `openai_compatible`, `anthropic` or `fake`: a built-in client.
2. `llm.provider: plugin`: load `llm.plugin` (`"pkg.module:attr"`).
3. Any other name: the entry point of that name in group `sbmig.llm_adapters`, else `llm.plugin`; otherwise
   `PluginLoadError` naming both ways to register it.

The loaded object may be a class / factory (called with as many of `(options, cfg)` as it takes) or an instance with
`complete`. `options` = `llm.plugin_options`, plus `options["api_key"]` from the `llm.api_key_env` variable when set
(in memory only). Adapters (a subclass of `LLMAdapter`, `adapter_kind = "request"`, or a `complete` whose first
parameter is `request` / annotated `LLMRequest`) are wrapped in `AdapterClient`.

Entry-point registration in **your** package's `pyproject.toml`:

```toml
[project.entry-points."sbmig.llm_adapters"]
mygateway = "mypkg.adapter:make_adapter"
```

### What sbmig guarantees (tier 1)

- **Recording**: with `llm.record_dir`, every call (plugins included) goes through `RecordingClient` and writes
  `llm-NNNN.json` (messages, response, session id, model, usage, latency, role, metadata, error) and a journal
  `llm_call` event.
- **Budgets**: `loop.max_llm_calls` (0 = no cap), `script.max_llm_tries_per_case` / `script.max_augments_per_case`
  and `fleet.max_llm_calls_total` apply. A tier-2 `propose_script()` is charged `max(1, proposal.llm_usage.calls)`.
- **Validation**: the result is coerced to `LLMResponse` (a dict with `content`, a `str`, or an object with
  `.content`); an empty `model` is filled with the requested one.
- **Error mapping**: an exception from your adapter becomes `sbmig.fix.client.LLMError` reading
  `LLM adapter <name> failed (status=<status>, cause=<ExceptionClass>): …`. `status=None|401|403|404|408|429|5xx`,
  `cause=…` or a transport exception class name are transport / configuration failures: two in a row pause the loop
  once for `llm.cooldown_seconds`, the next streak marks the LLM unavailable (`llm_unavailable`; stored-script reuse
  and the zero-LLM pre-tries keep running). `status=400|413|422`, a content filter and `finish_reason=length` reject
  only that request; the same HTTP 400 body for two different prompts is a configuration error (flag
  `llm-config-error`, no more LLM calls in the run).

### What the plugin must do (tier 1)

- Return an `LLMResponse`, or raise. Never return an error text as `content`.
- Handle retries, rate limiting, auth and timeouts itself (sbmig's retry logic covers the built-in clients only).
- Never log or persist the key.

## Roles

| `role` | Caller | Model sbmig asks for |
|---|---|---|
| `fix` | `LLMFixer.propose_script` (fix loop, new / augment) | `llm.model` |
| `fix_escalation` | the same, with `ctx.use_escalation_model` (third ask of a case, or a stall) | `llm.escalation_model` (else `llm.model`) |
| `structure_review` | the structure review after the restructure (review + at most one format / apply retry) | `llm.model` |
| `verify_guide` | `LLMFixer.verify_guide`: one call after an applied fix changed a major version / replaced a library without `<verify>` | `llm.model` |
| `plan_advisor` | the optional plan advisor (`plan.advisor: llm`) | `llm.recipe_model` (else `llm.model`) |
| `other` | anything else (`sbmig llm-probe`, …) | as passed |

Unknown roles become `other` with the original value in `metadata["role_detail"]`. Your adapter can route on
`request.role`, as `company_gateway_adapter.py` does with its `models` table.

## Tier 2: fixer plugin

`fix.plugin: "pkg.module:attr"` (a bare name is looked up in the entry-point group `sbmig.fixers`):

- The plugin can be a class, a factory, or an instance with `propose_script`. It is called with as many of
  `(options, cfg, llm)` as it takes (`options` = `fix.plugin_options`, `llm` = sbmig's client or `None`), plus a
  `notes_dir` keyword when declared.
- It is created lazily, the first time a case needs an LLM; journal `{"event": "fixer", "plugin": spec}` (with
  `"error"` when loading fails).
- `ctx.script_mode == "augment"`: the answer must be the **complete** new script; it replaces `ctx.script_base` and
  runs on a tree where the base was rolled back.
- Never edit files in `propose_script`. Return the script and / or edits; sbmig runs the script through
  `sbmig.script` (static check, `python -I` child process, helper API `p` limited to the project, timeout), applies
  edits through `sbmig.script.apply_edits`, commits, rebuilds, scores and rolls back.
- Report unreachability as `ScriptProposal(error="… status=<code> …")` or `cause=<ExceptionClass>: …`; those markers
  count as transport failures. An answer with neither script nor edits and a `blocked_reason` is a decline.
- The prompt sbmig would send is available: `ContextPacker(cfg)` in `sbmig/fix/context.py`.

## Embedding: `sbmig.api`

```python
from sbmig.api import migrate, exit_code
from mypkg.adapter import MyGatewayAdapter       # an LLMAdapter subclass

report = migrate("path/to/project", configs=["config/artifacts.yml"], out="out",
                 llm=MyGatewayAdapter(...),       # adapter or client; adapters are wrapped automatically
                 fixer=None,                      # or your FixerLike (overrides fix.plugin)
                 kb=None,                         # None/True = file KB, False = no KB, or a KnowledgeBaseLike
                 stop_after=None, dry_run=False)  # stop_after: intake|analyze|plan|transform|restructure
print(report.success, exit_code(report))         # 0 ok / 2 incomplete / 3 aborted
```

`overrides={...}` is merged like CLI options (e.g. `{"llm": {"record_dir": "out/rec"}}`); `components={...}` passes
extra `Orchestrator` collaborators (`build_runner`, `rewrite_runner`, `edit_applier`, …). An `edit_applier` has the
shape `fn(ws, edits, *, cfg, errors=None) -> ScriptRunResult`; `errors` is passed only when the function takes it.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `PluginLoadError: cannot import plugin module` | The package is not installed in sbmig's venv and not on `PYTHONPATH` (`PYTHONPATH=. sbmig run …` for the repo-local `examples.adapters.*`). |
| `unknown llm.provider 'x' ... no entry point` | The entry point is not installed or the name differs; the message lists the installed names. |
| `requires N positional argument(s)` | Your factory takes more than `(options, cfg)` (tier 1) or `(options, cfg, llm)` (tier 2); give the extras defaults. |
| `LLM adapter X returned int, expected LLMResponse` | Return an `LLMResponse`, a dict with `content`, or a `str`. |
| Run reports `llm-unavailable` | Two transport / configuration failures in a row after the one cool-down. Check the `llm_call` journal events and the `error` field in `llm-NNNN.json`; rerun `sbmig llm-probe`. |
| Run reports `llm-config-error` | The gateway answered the same HTTP 400 body to two different prompts (model name, a payload field). Fix `.env` / `llm.*` and rerun. |
| `LLM-PROBE FAIL` with a reply in an unexpected field | Set `LLM_RESPONSE_PATH` to the field the probe's response JSON shows. |
| Fixer plugin is never called | Every case was fixed by a stored script (reuse) or a zero-LLM pre-try first. Look for `fixer` / `llm_script` events in `journal.jsonl`. |
