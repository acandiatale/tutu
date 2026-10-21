# Interfaces between workstreams

Source of truth: `sbmig/core/models.py`, `sbmig/core/interfaces.py`, `sbmig/core/config.py`, `sbmig/core/workspace.py`.
Do not edit these without a note here. If you need a new field, add it with a default so nobody breaks.

## Public entry points each WS must expose

```python
# A
from sbmig.analyze import analyze_project          # (ws: Workspace, cfg: Config) -> ProjectProfile
from sbmig.plan import build_plan                   # (profile, cfg, kb: KnowledgeBaseLike | None = None) -> MigrationPlan
from sbmig.plan import pre_handlers_for             # (profile, cfg) -> list[str]  handler ids to run pre-emptively

# B
from sbmig.restructure import PomEditor             # PomEditor(path: Path)  implements PomEditorLike
from sbmig.restructure import Restructurer          # Restructurer().apply(ws, profile, cfg, mode="layout"|"msa_pom") -> RestructureResult

# C
from sbmig.rewrite import OpenRewriteRunner         # OpenRewriteRunner(ws, cfg, kb=None) implements RewriteRunnerLike
from sbmig.build import MavenRunner                 # MavenRunner(ws, cfg) implements BuildRunnerLike
from sbmig.build import parse_maven_log             # (text: str, project_root: Path) -> list[BuildError]  (signatures filled)
from sbmig.build import signature_for               # (error: BuildError) -> tuple[str, str]  (L1, L2)

# D
from sbmig.fix import make_llm_client               # (cfg: Config) -> LLMClientLike   (provider switch, incl. FakeLLMClient)
from sbmig.fix import LLMFixer                      # LLMFixer(cfg, client, notes_dir=None) implements FixerLike
from sbmig.fix import PatchApplier                  # PatchApplier(ws).apply(changes) -> (applied, errors)
from sbmig.fix import parse_fix_response            # (text) -> ParsedResponse(changes, analysis, need_files, need_grep)

# E
from sbmig.triage import Triager                    # Triager(cfg) implements TriagerLike
from sbmig.triage import HandlerRegistry            # HandlerRegistry.default(cfg) -> registry; .get(id), .matching(cluster, profile, cfg)
from sbmig.loop import Orchestrator                 # Orchestrator(cfg, ws, ...).run() -> RunReport
from sbmig.report import write_report               # (report: RunReport, ws) -> dict[str, str] paths
# sbmig/cli.py                                       typer app: run | analyze | plan | build | rewrite | kb ...

# F
from sbmig.kb import FileKnowledgeBase              # FileKnowledgeBase(cfg) implements KnowledgeBaseLike
from sbmig.kb import kb_cli                         # typer sub-app mounted by sbmig/cli.py as `sbmig kb`
```

## Conventions
* All file paths inside models are **relative to `ws.root`** (posix separators). Logs/outputs are absolute.
* Never print; use `logging.getLogger("sbmig.<pkg>")`. The CLI configures rich logging.
* Subprocesses: always `cwd=ws.root` (or module dir), `capture_output`, timeout from config, write full log to
  `ws.new_log_path(label)`.
* Every mutation of the project goes through `ws.write/move/delete` or a `PomEditor` and is followed by
  `ws.commit(...)` **by the orchestrator** (components do not commit themselves, except Restructurer which may
  commit intermediate steps for clarity).
* Tests: `tests/<ws>/`, pure-python by default; mark `@pytest.mark.integration` for anything needing mvn/java/network.
* Python ≥ 3.10, type hints everywhere, pydantic v2.

## Change log
* 2026-10-18 PM: initial contracts.
* 2026-10-18 WS-F: `FixAttempt.cluster_errors: list[BuildError] = []` (default empty). The orchestrator should fill it
  with the cluster's errors when it creates an attempt; KB harvest needs them for L2 signatures and sanitized error
  copies. Without it harvest falls back to L1-only cases with errors synthesized from `cluster_signatures`.
* 2026-10-18 PM: `BuildConfig.external_package_prefixes` (default []) + `sbmig.build.register_external_prefixes` / `external_prefixes_from_config`; `MavenRunner.__init__` registers in-house group ids so they are not masked in signatures.
* 2026-10-18 PM: default `openrewrite.recipe_bom_version` pinned to 3.37.0 (3.38.0 references unpublished artifacts).
* 2026-10-18 PM: `@ServletComponentScan` FQCN verified as `org.springframework.boot.web.server.servlet.context.ServletComponentScan` (Boot 4.1.0).
* 2026-10-18 PM (integration): `BuildFixLoop` fills `FixAttempt.cluster_errors`; new handler `add-known-dependency` (`sbmig/triage/handlers/known_packages.yml`); project recipe `com.company.migrate.Boot4JacksonTwins` (Boot Jackson types → Jackson 3 twins) added to the `spring_boot_4` set; Restructurer copies as-is test/runtime-scoped deps into boot-jar when `@SpringBootTest` classes move and bumps optional artifacts through the conventional `<artifactId>.version` property when present.
* 2026-10-18 PM (integration 2): `RewriteResult.recipes_applied` / `forked`; `RewriteRunnerLike.run(..., fork=None)` (False → runNoFork); `KBConfig.try_candidates_in_plan`; planner appends candidate KB recipes to `spring.rewrite`; `Orchestrator._credit_plan_recipes` credits KB recipes by phase build outcome; fix loop runs reactive KB batches with `fork=False`.
* 2026-10-18 G1 (edge infra): `Workspace.read/write(encoding=None)` detect & preserve legacy encodings (`detect_encoding`, `encoding_of`, `set_encoding_candidates`; `read` keeps CRLF bytes), zip names re-decoded cp437→cp949; `sbmig.build.classpath` (`module_classpath`, `locate_class`, `locate_type_owner`, `owner_fqcn`, `type_mismatch`, `gav_for_jar`); `sbmig.build.effective` (`capture_effective_depmgmt`, `load_effective`, `effective_version`); edge handlers in `sbmig/triage/handlers/edge_handlers.py` expose `flags`/`notes` after apply; rewrite runner retries once on OutOfMemoryError with `oom_retry_jvm_args`.
* 2026-10-18 PM: `DecisionKind.BLOCKED`, `BlockReason`, `BlockedItem`, `PhaseResult.blocked`, `RunReport.blocked`; `LoopConfig.oscillation_window/max_llm_attempts_per_cluster/blocked_policy/structural_regression_kinds`; `BuildConfig.source_encoding_policy/encoding_candidates/enforcer_policy/werror_policy`; `OpenRewriteConfig.oom_retry_jvm_args`; `PlanConfig.optional_bump_phase`. See docs/ERROR-HANDLING.md.
* 2026-10-18 PM (round 3 contracts — adapters, recipe catalog, fleet learning):
  * **LLM adapter tier** (`sbmig/core/interfaces.py`): `LLMRequest` (messages, model, max_tokens, temperature, stop,
    `role: LLMRole`, `metadata`), `LLMAdapterLike.complete(request) -> LLMResponse`; `LLMClientLike.complete` gained
    optional `role=` / `metadata=` keywords (every client must accept and may ignore them). Roles: `fix`,
    `fix_escalation`, `plan_advisor`, `recipe_candidate`, `other`.
    `LLMConfig.provider` is now an open string: builtin (`openai_compatible|anthropic|fake`), `plugin`
    (+ `llm.plugin: "pkg.module:attr"`, `llm.plugin_options: {}`), or an entry point name in group `sbmig.llm_adapters`.
  * **Fixer adapter tier**: `FixConfig(plugin, plugin_options)` as `cfg.fix` — a `FixerLike` class or factory
    `fn(options, cfg, llm) -> FixerLike` replaces `LLMFixer` (the in-house coding agent gets `ws`, the `ErrorCluster`
    and the `FixContext`; `sbmig.fix.ContextPacker` is public so it can reuse our packed context).
  * **Recipe catalog**: `PlanConfig.catalog_file` (default `knowledge/recipes/catalog.yml`, `cfg.recipe_catalog_file`)
    — single source of truth for known recipe ids (artifact, description, phase, `covers`, `gate`, origin). The
    planner's `ADVISOR_CATALOG/COVERAGE/GATES` are derived from it; `sbmig recipes list|show|check` reads it.
  * **Learning ledger & labels** (`sbmig/core/models.py`): `FixLabel` taxonomy, `ResolutionRecord` (one line of
    `kb/ledger.jsonl`: project × error-case symbol × resolution × labels × replacement), `KBCase.project/symbol/
    fix_labels/replacement`, `HarvestResult.recipes_promoted/ledger_entries`; `KBConfig.ledger/symbol_index/
    deterministic_candidates` (labels such as `type-rename`/`dependency-add` compile straight to recipe candidates
    without the LLM; `kb/symbols.json` feeds "symbol X was fixed by Y in N projects" hints).
  * **Fleet** (`FleetConfig` as `cfg.fleet`, `FleetProjectResult`, `FleetState`): `sbmig fleet <manifest>` runs N
    projects strictly sequentially with one agent, resumable (`fleet/state.json`), KB fed between projects,
    `fleet/summary.{md,csv,json}` + `fleet/learning.md` after every project.
* 2026-10-18 round 3 (implementation notes): `sbmig.llm` (`LLMAdapter` ABC, `AdapterClient`, `load_plugin`,
  `resolve_llm_plugin`, `resolve_fixer_plugin`; entry-point groups `sbmig.llm_adapters` and `sbmig.fixers`); every
  built-in client tolerates `role=`/`metadata=`; `RecordingClient` records them; `sbmig.api.migrate(...)` for
  embedding; `sbmig.plan.catalog` (`RecipeCatalog`, `GATES`, `default_catalog`) — `ADVISOR_*` constants are derived
  from `knowledge/recipes/catalog.yml`; `artifact_for(recipe_id, cfg=None)` consults the catalog first;
  `FileKnowledgeBase.credit_recipe` now returns the `KBRecipe` and records auto-promotions; `sbmig.kb.labels`
  (`label_changes`, `compile_recipe`), `sbmig.kb.ledger` (`Ledger`, `SymbolIndex`); `Orchestrator._record_run_ledger`
  (called in `_finalize` before the report writer) adds `unresolved`/late-blocked ledger lines; `sbmig.fleet`
  (`FleetRunner`, `load_manifest`, `fleet_cli`). CLI: `sbmig run --llm-plugin/--fix-plugin`, `sbmig recipes`,
  `sbmig fleet`, `sbmig kb ledger|symbols|learning`.
* 2026-10-18 PM (round 4, library advisories + handover): `sbmig/core/models.py` gains `AdvisoryPolicy` (guide|recipe —
  **no block**), `AdvisoryStatus`, `AdvisoryOutcome`, `LibraryAdvisory` (one entry of `knowledge/libraries/advisories.yml`),
  `AdvisoryFinding`, `VersionChange`, `HandoverSummary`; `RunReport.advisories: list[AdvisoryFinding]` and
  `RunReport.handover: Optional[HandoverSummary]`; `ResolutionRecord.resolution` accepts `"advisory"`;
  `FleetProjectResult.advisories: list[str]` ("id:outcome") and `.carried_over: int`. `Config.advisories: AdvisoryConfig`
  (`enabled`, `file`, `extra_files`, `allow_recipes`, `policy_overrides`, `report_carried_over`, `ledger`,
  `max_from_here`) + `Config.advisories_file` / `Config.advisory_files`. Package `sbmig/advisory` exposes
  `load_advisories(cfg)`, `evaluate_advisories(profile, cfg, catalog=None)`,
  `advisory_recipe_refs(findings, cfg, planned_ids, catalog=None)` (planner; mutates findings: recipes/recipe_source),
  `verify_advisories(findings, view, cfg)` (finalize; sets outcome), `followups_for(findings)`, `advisories_cli`
  (`sbmig advisories list|show|check`). Planner: `build_plan(profile, cfg, kb=None, advisories=None)` — the
  orchestrator passes the findings when the planner accepts the keyword (fallback to the old call on TypeError).
  Report: new first section "Handover" (versions replaced, libraries needing attention, "from here on, your team");
  `sbmig/report/handover.py: build_handover(profile, view, report, effective=None, cfg=None) -> HandoverSummary`.
  `CatalogEntry.advisories: list[str]` (informational; `recipes check` cross-checks advisories.yml recipe ids).
* 2026-10-18 PM (round 5 contracts — `method-rename` → `ChangeMethodName`, jar-checked catalog):
  * `KBCase.symbol_owner: str = ""` — FQCN of the type owning the error-case symbol. javac prints imported types by
    their simple name (`location: variable client of type Client`, `location: class Client`), so `sanitize_case`
    (harvest) resolves it: explicit import in a changed `.java` file → FQCN; wildcard import → `locate_class`
    candidates; otherwise "". `compile_recipe(case)` compiles `labels == {"method-rename"}` (all `method_renames`
    agreeing on old/new, owner a library FQCN via `details` or `case.symbol_owner`) to
    `org.openrewrite.java.ChangeMethodName {methodPattern: "<owner FQCN> <old>(..)", newMethodName: <new>,
    matchOverrides: true}`, recipe name `com.company.kb.<OwnerSimple><Old>To<New>` (PascalCase of the parts).
    Argument types never go into the pattern (javac prints simple names). Method references (`x::old`) are
    `method-rename` too.
  * `sbmig/plan/jarcheck.py` (new): `check_jars(catalog, cfg, *, work_dir, artifacts=None) -> JarCheckReport`
    resolves every configured `openrewrite.recipe_artifacts` entry (BOM version via `resolve_recipe_artifact_versions`)
    to its transitive jar classpath with Maven (`dependency:build-classpath` on a generated pom under `work_dir`),
    and reports per catalog entry: the jar that declares the recipe id (`META-INF/rewrite/*.yml` `name:` or the
    `.class`), whether `artifact` is on that classpath, and for `provided_by` whether `artifact` is on the
    `provided_by` artifact's own transitive classpath. `JarCheckReport.problems: list[str]` (same style as
    `RecipeCatalog.check`), `.rows: list[JarCheckRow]`. CLI: `sbmig recipes check --jars [--work-dir DIR] [--json]`
    (Maven + network; `scripts/e2e.sh` runs it). Without `--jars` the check is unchanged (offline).
  * e2e `expected.json` gains `"kb": {"dir": "<relative to repo>", "candidates_min": n, "recipes": [{"recipe": "<id>",
    "options": {...}}]}` (scripts/e2e.py reads `<dir>/recipes/{candidates,active}/*.yml`; `options` must be a subset
    of the recipeList entry's options). Fixtures that exercise the KB set their own `kb.dir` in `sbmig.e2e.yml`
    because every candidate is appended to every plan (`kb.try_candidates_in_plan`).
* 2026-10-18 round 5 (WS-B implementation notes): `check_jars(catalog, cfg, *, work_dir=None, artifacts=None,
  resolver=None)` — `work_dir` defaults to `out/recipes-check`, `resolver` defaults to `jarcheck.DEFAULT_RESOLVER`
  (tests monkeypatch it); `JarCheckReport` also carries `bom_version` and the properties `ok`, `jars_scanned`,
  `declaring_jars`. CLI: `sbmig recipes check --jars [--work-dir DIR] [--artifact g:a ...] [--json]`; the offline
  check runs first and aborts `--jars` on problems. Verified 2026-10 on BOM 3.37.0: all 21 `origin: openrewrite`
  entries found in their declared jars; `rewrite-java-dependencies` 1.60.2 is on `rewrite-migrate-java` 3.42.0's
  transitive classpath (the former `provided_by` assumption).
* 2026-10-18 round 5 (WS-C implementation notes): e2e `expected.json` `"kb"` block also accepts
  `harvest_candidates_min` (compared with `len(report.harvest.candidates_created)`) and an optional `name` per
  wanted recipe; `scripts/e2e.py` wipes the fixture's `kb.dir` before the run (only when it is under `out/`).
  Fixture `boot2-method-rename` (legacy-web-lib 1.0.0 → 3.0.0, `clientIp` renamed `remoteClientIp`) needs
  **2** LLM calls, not 1: clusters are keyed by file (`sbmig/triage/cluster.py`), so the two call sites are two
  clusters in one round, each with its own replayed response.
* 2026-10-20 PM: `LLMConfig.payload_extra: dict = {}`, `LLMConfig.payload_generation_params: bool = False`,
  `LLMConfig.response_path: Optional[str] = None`; builtin provider `chat_post` (`sbmig.fix.ChatPostClient`,
  `sbmig.fix.extract_reply(data, response_path)`); CLI `sbmig llm-probe`, global `--log-file`, per-run
  `<run dir>/sbmig.log` (`sbmig.cli.attach_file_log`). Profile `config/llm-company.yml`.
* 2026-10-20 PM: `sbmig.web` (`RunManager(out_dir, repo_root, command=default_command)`, `make_server`, `serve`);
  HTTP routes in `sbmig/web/server.py` docstring; SSE events `status | journal | log | console | report | ping | end`.
  CLI `sbmig serve --host --port --out --repo-root`. Package data now includes `**/*.html`.
* 2026-10-20 PM (company convention: LangGraph + LangChain, served by uvicorn) — contracts:
  * **Graph engine** (`sbmig/graph/`, workstream G): `MigrationState` (TypedDict: `run_id`, `stop_after: str | None`,
    `dry_run`, `phases: list[str]` (remaining phase names), `phase: str | None` (current), `aborted: str | None`,
    `stopped: bool`, `summary: dict` (small, JSON-able: case_type, phase outcomes, llm_calls, blocked, unresolved)).
    Nodes: `intake` → `analyze` → `plan` → `phase` (one node invocation per plan phase group, conditional edge loops
    while `phases` remain and nothing aborted/stopped) → `finalize` → END. `build_graph(orch: Orchestrator)` returns
    the compiled `StateGraph`; `run_graph(orch, stop_after=None, dry_run=False) -> RunReport` and
    `stream_graph(orch, ...) -> Iterator[tuple[str, dict]]` (node name, state update) for the web app.
    `Orchestrator.run()` dispatches on `cfg.loop.engine` (`graph` default, `inline` = today's `_run`); the step
    bodies are shared methods (`step_intake/step_analyze/step_plan/step_phase/step_finish`) so both engines produce
    identical reports, journals and exit codes. Build gate, abort semantics (`_RunAborted`), `stop_after`, harvest
    and ledger calls stay exactly where they are today.
  * **LangChain tier** (`sbmig/llm/langchain.py`, workstream L): `ChatPostModel(BaseChatModel)` — the in-house
    gateway as a LangChain chat model (`_generate` does the same `POST url` with
    `{"model", "messages", **payload_extra}` + Bearer token, reply via `response_path` / `extract_reply`; usage in
    `usage_metadata`); `LangChainClient(model: BaseChatModel, default_model=None)` implements `LLMClientLike`
    (`role` → `tags=[role]`, `metadata` → `config["metadata"]`, `stop` honoured, token counts from
    `usage_metadata`). `make_llm_client` provider `langchain`: `llm.langchain_model` factory (`fn(cfg)` or `fn()`
    → BaseChatModel, or an instance) else `ChatPostModel` from the `chat_post` fields. `RecordingClient` still wraps
    it; `LLMRequest`/`LLMResponse`/`FixerLike` unchanged (invariant 4). `config/llm-company.yml` switches to
    `provider: langchain`.
  * **Web app** (`sbmig/web/app.py`, workstream W): FastAPI `create_app(out_dir, repo_root) -> FastAPI`, module-level
    `app` for `uvicorn sbmig.web.app:app` (env `SBMIG_WEB_OUT`, `SBMIG_WEB_REPO_ROOT`); static page mounted from
    `sbmig/web/static/` at `/static` and `/`. Routes as before (`/api/configs`, `/api/runs` GET/POST with the zip
    body, `/api/runs/{id}`, `/api/runs/{id}/events` SSE, `/api/runs/{id}/stop`, `/api/runs/{id}/files/{name}`).
    Runs execute **in-process** through `stream_graph` in a worker thread (one per run), SSE events: `status`,
    `node` (graph node update), `journal`, `log`, `console`→dropped, `report`, `ping`, `end`. Per-run log file via
    `attach_file_log(path, thread_id=...)` (a logging filter by thread). `sbmig serve` launches uvicorn.
* 2026-10-20 (WS-W notes): SSE also sends `error` (worker exception); per-run events are an append-only list (replayable
  by every SSE client); stop takes effect between graph nodes (sets the abort flag, closes the iterator → exit 3).
  `attach_file_log(path, thread_id=None)`; `sbmig serve` = `uvicorn.run(create_app(...))`.
* 2026-10-20 PM (simplification, user request): LangChain tier removed (`sbmig/llm/langchain.py`, provider `langchain`,
  `LLMConfig.langchain_model`, `http_backend`, `verify_ssl`, `payload_generation_params`, provider `chat_post`,
  `config/llm-company.yml`). New default provider `company` = `sbmig.fix.company.CompanyClient` (requests.post,
  `payload_extra` default `{need_origin: true}` in default.yml, `response_path` from `LLM_RESPONSE_PATH`, records when
  `LLM_RECORD_DIR` is set). `extract_reply` stays in `sbmig/fix/client.py`. LangGraph engine and web app unchanged.
* 2026-10-20 PM (round 6 contracts — Hystrix → Resilience4j, advisory-guided LLM fixes):
  * `FixContext.advisory_hints: list[str] = []` — for the cluster being fixed, the matching library advisories as
    short guidance lines ("hystrix (eol): replace with org.springframework.cloud:spring-cloud-starter-circuitbreaker-
    resilience4j; @HystrixCommand(fallbackMethod=…) → @CircuitBreaker(name=…, fallbackMethod=…); …" built from the
    advisory's title / replacement / followup). `sbmig.advisory.hints_for(errors, findings, profile=None) ->
    tuple[list[str], list[str]]` (hints, advisory ids): an advisory matches a cluster when an error mentions one of
    its library's import packages (`LibraryRule` packages, e.g. `com.netflix.hystrix`) or artifacts, or a symbol /
    owner class in those packages. `ContextPacker` renders them as a section "Library migration guidance" right after
    the errors (never truncated before `kb_hints`). `BuildFixLoop(..., advisories=report.advisories)` fills the
    context; `FixAttempt.advisory_ids` records which advisories were attached.
  * `AdvisoryFinding.llm_fixed_files / llm_fix_notes` — filled in `Orchestrator._finalize_advisories` from the run's
    successful `llm_fix` attempts (applied, outcome in SUCCESS_OUTCOMES, cluster resolved) whose `advisory_ids`
    include the finding; the Handover "from here on, your team" gets one line per such advisory: "code rewritten by
    the LLM for <library>: <files> — <notes>; review (changes.patch, attempts table)". The ledger line for the
    attempt carries the advisory ids in `labels`-adjacent metadata (`resolution=llm_patch`, `advisories=[…]`).
  * Recipe: `com.company.migrate.HystrixToResilience4j` (knowledge/recipes/base), advisory `hystrix` policy
    `recipe`. Programmatic `HystrixCommand` subclasses / `HystrixObservableCommand` are left to the LLM with the
    advisory hint (fixture `boot2-hystrix-command`, replayed LLM, `max_llm_calls` = number of files).
* 2026-10-20 PM (round 7a flow-review fixes): `sbmig.loop.parse_stop_after(value) -> Phase | None` (+
  `STOP_AFTER_PHASES`) is the one validator for `--stop-after` / web `stop_after=` / `api.migrate(stop_after=)`:
  intake…verify, `report` rejected with `ValueError`. `LazyFixer.apply` applies with `PatchApplier` when the real
  fixer cannot be built (KB patch replay never needs an LLM). `Orchestrator._final_build_after_abort(report)` runs
  the plan's verify steps after an internal-error / missing-component abort (invariant 1). `FileKnowledgeBase.
  record_run` writes `unresolved` ledger lines only when `report.final_build` is set and the run is not aborted.
  Web: `RunManager` serializes migrations (`_run_lock`); `status().state` gains `queued`; a stop observed after the
  verify phase group / `finalize` node no longer aborts the run; the abort reason of an earlier stop says the final
  build did not run. `is_llm_transport_error` also matches unparseable 200s / bad `LLM_RESPONSE_PATH` and ignores
  `status=400|413|422`.
* 2026-10-20 PM (round 7b contracts — multi-module `merge` v2, Maven-only intake):
  * `MergeConflict(path, kept_from, dropped_from, identical, saved_copies)` (`sbmig/core/models.py`);
    `RestructureResult.merged_modules / kept_boot_module / merge_conflicts`; `RunReport.merge_conflicts`
    (copied from the layout step's result by `Orchestrator._step_restructure`); `HandoverSummary.merge_conflicts`
    (rendered lines; the report prints them as a "Merge conflicts" block inside the Handover, before "From here on").
  * `LayoutConfig.multi_module_policy` default is now **`merge`** (the company layout is exactly comp-jar + boot-jar);
    `wrap` stays available. New `LayoutConfig.merge_collision: first-wins | fail` (default first-wins).
  * `merge` semantics: an as-is module whose directory basename equals `layout.boot_module` (default `boot-jar`) is
    the **kept boot module** — moved to `module/boot-jar` if elsewhere, parent relativePath recomputed, its
    dependencies on the merged modules replaced by one dependency on `<root artifactId>-comp`, spring-boot-maven-plugin
    ensured, sources/resources untouched, no Application class generated; every other module (and the root `src/`)
    is folded into comp-jar. "Already restructured" under `merge` = root packaging pom with **both** boot-jar and
    comp-jar among the modules (under `wrap` the old rule stays: boot-jar present and every module under `module/`).
    Collisions: first module in root `<modules>` order wins (`<root>` first when the root has `src/`); identical
    copies are dropped silently (one note); differing copies are saved as
    `<run dir>/merge-conflicts/<module>/src/...` and reported (`MergeConflict`, Handover, manual follow-up).
  * `ProjectProfile.build_tool` gains `"ant"`. `sbmig.analyze.detect_build_tool(ws) -> BuildToolDetection(tool,
    reason)` (cheap globs: `pom.xml`, `build.gradle[.kts]` / `settings.gradle[.kts]`, `build.xml`) is used by the
    analyzer (`warnings[0]`) and by `Orchestrator.step_intake`, which aborts **before** the effective-pom capture
    when the tool is not Maven: flag `unsupported-build-tool:<tool>`, `aborted`, exit 3, reason in
    `manual_followups[0]`. `Workspace._detect_project_root` also descends into a single top-level folder for
    Gradle / Ant builds so the reason names the real tool.
* 2026-10-21 (round 8 contracts — externalized-config inventory, placeholders, tests-skipped fallback):
  * Models: `PropertyRef(key, source, file, line, default, scope, raw)`, `PropertyFinding(key, status
    defined|defaulted|external, refs, defined_in, boot_keys, supplied_by, note)`, `PropertyInventory(findings,
    packaged_files, profiles, filtered_resources, placeholder_files, placeholders_written, notes; .external)`.
    `ProjectProfile.properties: PropertyInventory` (analysis: `sbmig.analyze.properties.scan_properties(ws, poms, scan,
    rules) -> PropertyInventory`, wired by `analyze_project`), `RunReport.properties: Optional[PropertyInventory]`
    (profile inventory + the placeholders the restructure wrote; set by `Orchestrator._step_restructure`, else copied
    from the profile at finalize), `HandoverSummary.properties_to_set / properties_guide` (rendered rows / guide lines),
    `RestructureResult.placeholder_files / placeholders_written`.
  * Config: `Config.externalized: ExternalizedConfig(enabled, sentinel="__SET_ME__", write_placeholders,
    placeholder_file="application.properties", rules_file="knowledge/config/externalized.yml", include_test_refs,
    max_refs_per_key)`; `BuildConfig.allow_skip_tests_fallback: bool = True`.
  * Rules YAML `knowledge/config/externalized.yml`: `jndi[]` (glob → boot_keys, `{leaf}`/`{name}` templates),
    `context_param` (default_boot_key template + known names: skip / boot_keys), `system_property.skip`,
    `boot_defaults`, `guide[]`. Company additions go there (invariant 2).
  * Restructure (layout + msa_pom): appends/replaces an idempotent block between the markers
    `# --- sbmig externalized-config` … `# --- end sbmig externalized-config` in `<boot-jar|service>/src/main/resources/
    <placeholder_file>` (created when absent): one `key=<sentinel>` line per `boot_key` of every `external` finding
    (JNDI / context-param mapped keys included; `boot_defaults` and rule `skip` excluded; context-param with an as-is
    value writes that value, not the sentinel). Journal event `placeholders` {step, files, keys: <count>}.
  * Build / loop: `BuildRunnerLike.run(..., skip_tests: bool = False)` → `-DskipTests`;
    `BuildFixLoop._maybe_skip_tests` (mirror of `_maybe_skip_test_compile`): when `build.allow_skip_tests_fallback`,
    the goal runs tests and only `TEST_FAILURE` errors remain → rebuild with `skip_tests=True`; on success
    `pr.build_ok=True`, flag `tests-skipped`, `loop.tests_skipped=True` (the final verify builds with `-DskipTests`
    too), failing tests listed as follow-ups ("packaged with -DskipTests: <test> — <message>") and as unresolved.
    Journal event `tests_fallback`.
  * Report: Handover gets "### Properties to set before running" (table: key | before migration | referenced at
    (≤ max_refs_per_key, "+N more") | written as | set via) + the guide lines ("How to supply them") + the placeholder
    file line, placed before "From here on"; rows only for `external` findings, and no block at all when no external
    finding needs a value (rule-skipped context-params / Boot defaults alone do not count); `report.json` carries
    `properties`; `console_summary` prints "properties to set: N". e2e `expect.properties: {external_min, external: [keys], placeholder_file_contains: [strings],
    absent: [keys]}` evaluated by `scripts/e2e.py evaluate_properties`.

* 2026-10-21 (round 9 — follow-ups + edge cases; contracts written first, then WS-A rewrite, WS-B loop/fix/web,
  WS-C analyze, WS-D restructure, WS-E fixtures):
  * Config: `layout.drop_managed_versions: gte | any | off = gte`, `openrewrite.download_retry: int = 1`,
    `openrewrite.download_retry_wait_s: int = 30`, `llm.cooldown_seconds: int = 60` (mirrored in `config/default.yml`).
  * Models: `PropertyRefSource` += `config-placeholder` (a `${K}` inside the value of a packaged definition file;
    `property-source` now means `@PropertySource` / placeholder-configurer locations only), `PropertyFinding.service:
    Optional[str]` (MSA: the deployable whose code reads the key and whose resources were checked; None = shared code
    or project-wide mode; in MSA mode `findings` is unique on (`service`, `key`)), `PropertyInventory.services:
    list[str]` (empty = project-wide), `MergeConflict.union: bool` (line-list registry merged as the union of lines —
    nothing dropped, originals in `saved_copies`).
  * Interfaces: `FixContext.reference_notes: list[str]` and `FixContext.reference_files: dict[str, str]` (read-only
    context that is NOT part of the build; a proposed change whose path is a reference key is dropped before apply),
    `RestructureResult.managed_versions_dropped: list[str]` ("g:a: <explicit> → managed <version> (<pom>)").
  * Recipe-run download resilience (catalog X8): `OpenRewriteRunner.run` — a failed run (not OOM) whose error / log
    matches a transfer failure (`Failed to download`, `Could not transfer artifact`, `Transfer failed`, `status code:
    429|502|503`, `Too Many Requests`, `Service Unavailable`, `Bad Gateway`, `Connection reset`, `Read timed out`,
    `Connection timed out`, `SocketTimeoutException`, `UnknownHostException`; never `Could not find artifact`) waits
    and re-runs up to `download_retry` times (log `rewrite-<label>-dlretry[N].log`); still failing →
    `RewriteResult.error` prefixed `download-retry: `. Journal `rewrite_download_retry {recipes, module, attempt,
    wait_s, first_log, reason}`, `rewrite_download_retry_result {ok, log, error}`. Order: OOM retry | download retry →
    compile→runNoFork fallback. Injectable wait `OpenRewriteRunner._sleep`.
    Orchestrator `_step_rewrite`: a successful non-dry run that leaves literal versions (`^\d+(\.\d+)*\.x$`,
    `latest.release|integration|patch`) in a changed pom.xml → `ws.revert_to(<head before the step>)`, wait, re-run the
    step once (only when `download_retry > 0` and the tree was clean before the step); journal
    `rewrite_unresolved_versions {step, poms: {path: [versions]}, retry, log}`; still there → result kept, flag
    `rewrite-unresolved-versions`, phase note, follow-up "the recipe run could not download Maven metadata (proxy /
    rate limit): literal versions `4.0.x` remain in pom.xml — re-run sbmig later or set the versions by hand".
    KB credit (`_credit_plan_recipes`): a failed phase is not credited when it failed on dependency resolution
    (`rewrite-unresolved-versions` raised in the phase, or every remaining error is DEPENDENCY/PARENT/PLUGIN_RESOLUTION
    or a `download-retry:` rewrite error, or no code error remains and every blocked item is `artifact_not_found` /
    `parent_not_found`); journal `kb_credit_skipped {phase, recipes, reason}`.
  * LLM cool-down (L5): the second consecutive transport/config failure pauses the loop once per run for
    `llm.cooldown_seconds`, then retries (journal `llm_cooldown {phase, iteration, seconds, reason, clusters}`, flag
    `llm-cooldown`, attempt note; the two cooled-down attempts are left out of the previous attempts given to the
    triager / prompt so they do not count against `max_llm_attempts_per_cluster`); only a second streak marks the LLM
    unavailable (`llm_unavailable` journal gains `cooldown_used`); the pause is skipped when less than
    `cooldown_seconds` of `loop.max_total_minutes` is left; `0` = pre-round-9 behaviour. Module-level
    `sbmig.loop.fixloop.sleep_seconds` (tests: the root conftest autouse fixture `no_llm_cooldown_sleep`).
    Request-level rejections never count: `status=400|413|422` and a gateway content-filter answer — provider
    `company` raises `LLMError("content filtered by the gateway (finish_reason=content_filter | content_filter_result:
    … | prompt_filter_results: …)")` from `sbmig.fix.company.content_filter_reason(data)` (OpenAI / Azure shape:
    `choices[0].finish_reason == content_filter`, a `filtered: true` category next to an empty message, or a
    `prompt_filter_results` hit).
  * Merge-conflict hints: `BuildFixLoop.merge_conflicts: list[MergeConflict]` (set by the orchestrator after every
    restructure step from `report.merge_conflicts`). A cluster whose file is a first-wins kept file
    (`<layout.module_dir>/<layout.comp_module>/<conflict.path>`) or whose errors name that class's simple name gets
    `reference_notes` + `reference_files` (run-dir saved copy → content, ≤ 12,000 chars with `… (truncated)`, ≤ 3 per
    cluster; `identical` / `union` conflicts and conflicts without `saved_copies` skipped). Attempt note
    `merge-conflict hint: <path>`, journal `merge_conflict_hints {phase, iteration, cluster, reference_files}`.
    `ContextPacker` sections: `reference_files` "Reference files (read-only, NOT in the build — never write
    SEARCH/REPLACE blocks for these paths)" right after the source section (one fenced block per copy headed
    `// reference copy (read-only, not in the build): <label>`), `reference_notes` "Module merge notes (first-wins; the
    dropped copy is below as reference)" between the advisory guidance and the KB hints; `TRUNCATE_ORDER` = notes,
    plan_notes, other_errors, previous_attempts, kb_hints, reference_files, advisory_hints, reference_notes, pom, files.
  * Handover: `sbmig.report.handover.resolve_current_path(ws, path, cfg=None) -> str` (an existing path unchanged;
    else the part from `src/` on under `<module_dir>/<comp_module>/`, then `<module_dir>/<boot_module>/`; else the
    original) applied to `AdvisoryFinding.llm_fixed_files`.
  * Web: `RunManager(out_dir, repo_root, *, max_finished_runs=200)` — when a worker finishes, the oldest finished
    runs beyond the cap leave memory (running / queued never do); `status` / `list_runs` / `events` answer evicted
    runs from the run directory (`report.json` + `web-status.json` {exit_code, error, stop_requested, last_node,
    success}, written when the worker finishes).
  * Analyze: `scan_properties(ws, poms, scan, rules, cfg=None, *, services: Optional[list[ServiceInfo]] = None)` —
    None / `[]` keeps the project-wide inventory; a non-empty list (the analyzer passes
    `[s for s in profile.services if s.kind == "service"]` when `case_type == MSA`) switches to per-service: a service
    dir is dirname(`root_pom`) ("" for a root service), every ref / definition file belongs to the deepest service dir
    containing it, refs inside a service are checked against that service's definitions only, refs outside every
    service against every definition. The host entry never defines the placeholder it reads (`log.dir=${LOG_DIR}`
    leaves `LOG_DIR` external — round-8 bug). `@ConditionalOnProperty` / `@ConditionalOnBooleanProperty` keys are
    `value-annotation` refs with a pseudo default (`defaulted` unless defined, `boot_keys=[]`, note; never a
    placeholder). UTF-8 BOM stripped, `\${x}` is a literal, `bootstrap.*` counts as defined with a note when
    `spring-cloud-starter-bootstrap` is absent. Edge table: `sbmig/analyze/properties.py` docstring.
  * Library rule + advisory `ibatis2` (artifacts `org.apache.ibatis:ibatis*`, `ibatis:ibatis*`, `com.ibatis:*`;
    imports `com.ibatis`, `org.springframework.orm.ibatis`; pom + imports only, distinct from `mybatis`): status
    incompatible, policy `guide`, replacement `org.mybatis:mybatis + org.mybatis:mybatis-spring`, note
    `knowledge/notes/ibatis2.md` (`## fix-loop: ibatis2`, in `notes/index.yml`). 13 advisories.
  * Handlers: `compiler-legacy-config` (`sbmig.triage.handlers.pom_handlers.CompilerLegacyConfig`, pre-emptive from
    the profile via `sbmig.plan.pre_handlers.compiler_legacy_config_reason`, reactive on the javac 21 wording):
    removes `<fork>`, `<executable>`, `<compilerVersion>`, `<compilerArguments>` children `bootclasspath` /
    `Xbootclasspath*` / `extdirs` / `endorseddirs` / `source` / `target`, `<compilerArgument>` and `<compilerArgs><arg>`
    entries with those flags (a flag's separate value `<arg>` goes too); text-based, formatting kept (E10).
  * Fleet: a run with flag `unsupported-build-tool:<tool>` is a `skipped` project (`error` "unsupported build tool:
    <tool> (only Maven projects are migrated)"), never re-run by `retry_failed`, never a `stop_on_abort` trigger,
    stays skipped on resume (`sbmig.fleet.runner.unsupported_build_tool(report)`).
  * e2e (`scripts/e2e.py`): post-check kinds `pom_dependency_unversioned` / `pom_dependency_versioned` {path,
    artifacts}; `expect.properties.external_in: {service: [keys]}`. Fixtures `multi-module-conflict` (replayed LLM,
    own KB dir), `legacy-ibatis`, `msa` per-service keys (20 fixtures).
  * Restructure (WS-D): `layout.drop_managed_versions` — after the parent / mandatory / optional edits (`layout`,
    `msa_pom`) explicit `<dependencies>` versions the restructured parent chain manages are dropped (`gte`: only when
    managed ≥ explicit, unparsable → keep; `any`: always; `off`: never). Never touched: `artifacts` parent / mandatory /
    optional (incl. aliases) / remove_when_present, the project's own modules, system scope, non-jar type or
    classifier, dependencyManagement, plugins, properties. `RestructureResult.managed_versions_dropped` entries
    `"g:a: <explicit> → managed <M> (<pom path>)"` + one note (deliberately newer pins kept by `gte` are noted).
    `params["effective_pom"]`: zero-arg callable → effective depmgmt dict (shape of `load_effective`) or None; the
    declaring module's `by_module` entry is used (g:a, then artifactId), never the merged map as a fallback (no
    leaking between MSA services); absent / None / raising → note "managed-version cleanup skipped: no effective
    pom". Orchestrator wiring: `Orchestrator._effective_pom_after()` (custom build runner without a capture → None;
    captures into `EFFECTIVE_AFTER_FILE = "depmgmt-after-restructure.json"`, journal `effective_depmgmt_after`).
    `sbmig.build.effective`: `capture_effective_depmgmt(ws, cfg, module=None, name=DEPMGMT_FILE)`,
    `load_effective(ws, name=DEPMGMT_FILE)` (defaults keep the as-is `depmgmt.json`; a non-default name prefixes the
    xml and log with the file stem), `AFTER_RESTRUCTURE_FILE`, `effective_pom_provider(ws, cfg, module=None,
    name=AFTER_RESTRUCTURE_FILE)` (drops a stale file, captures, loads). `PomEditor.remove_version(el) -> bool`.
  * Merge edge cases (S5 update, S9–S13): registry union (`MergeConflict.union=True`, `identical=False`,
    `saved_copies` = every original incl. the kept copy, `dropped_from` = the non-first modules; not a collision under
    `fail`), `src/test/**` follows the main rules, pom-packaging sub-modules (children merged depth-first, properties +
    dependencyManagement folded into the root first-wins, internal BOM import removed, stray `src/` saved), outside
    `../x` modules skipped under `merge` (note + follow-up; `wrap` still aborts), WAR module webapp →
    `module/comp-jar/src/main/webapp` + follow-ups, kept boot module keeps its own boot resources (shadowing
    follow-up), custom `<build><resources>` carried over (comp-jar `<resources>` = union + default dir).
  * MSA placeholders: when `PropertyInventory.services` is non-empty or any finding carries `service`, each service
    file gets the findings whose `service` is that service's name, its directory or the directory's basename, plus
    `service is None`; otherwise the round-8 location-based selection; `layout` ignores `service`.
    `PLACEHOLDER_SOURCE_LABELS["config-placeholder"] = "config placeholder"`.
