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
    matchOverrides: true}`, recipe name `<OwnerSimple><Old>To<New>` (PascalCase of the parts; KB recipe names are one PascalCase word
    without dots since 2026-10-25, legacy `com.company.kb.<Name>` ids stay valid — `sbmig/kb/recipes.py`).
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
    packaged_files, profiles, filtered_resources, filtered_binary_files (binary files a `<filtering>true</filtering>` resource would filter — `sbmig.analyze.properties.filtered_binary_resources`, Ant includes / excludes honoured, capped at 200; drives the pre-emptive `resources-binary-filtering`), placeholder_files, placeholders_written, notes; .external)`.
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
  * Handler `resources-binary-filtering` (`sbmig.triage.handlers.pom_handlers.ResourcesBinaryFiltering`; pre-emptive when
    `PropertyInventory.filtered_binary_files` is non-empty, reactive on `MalformedInputException` / `Input length = N` /
    `filtering … failed with`; kinds plugin_execution, unknown): `<nonFilteredFileExtensions>` on the failing plugin
    (`maven-resources-plugin`, or `maven-war-plugin` when the cluster names it / `war:war`) in the pom that declares the
    filtering (the failing module, else the root — one list for every module), entries = `BINARY_RESOURCE_EXTENSIONS` +
    the failing file's extension, existing entries kept, a `<build><plugins>` declaration created when absent (a
    `<build><plugins>` block wins over `<pluginManagement>`, profiles untouched); text-based, comments ignored (E12).
  * Mandatory artifact `type` / `scope` (user request, 2026-10-21): `ArtifactSpec.type` (e.g. `pom` for pom-packaged
    in-house artifacts) is written into the `<dependency>` and aligned on an as-is declaration that lacks it, like the
    version; `scope: import` (+ `type: pom`) puts the mandatory artifact into `<dependencyManagement>` (BOM style)
    instead of `<dependencies>` (`Restructurer._add_mandatory` / `_align_mandatory`, notes `type=… aligned`).
  * Pom repositories (X9): `PomInfo.repositories` (URLs of `<repository>` / `<pluginRepository>` incl. profiles, comments
    and `<distributionManagement>` excluded, properties interpolated; `sbmig.analyze.pom_parser.repository_urls`),
    `BuildConfig.pom_repositories: keep | remove` (default keep), `sbmig.build.mvn.settings_catch_all_mirror(cfg)`
    (None / False / True: `build.settings_file` has a `<mirrorOf>*</mirrorOf>` / `external:*` mirror) and
    `repository_notes(poms, cfg)` (plan notes; `followup:` prefix when Maven would contact the URLs directly), handler
    `strip-pom-repositories` (`StripPomRepositories`, applicable only under `remove`; pre-emptive from the profile).
    Template: `config/settings.nexus.example.xml`.
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

* 2026-10-21 (round 9b — company role parents, from the first company run):
  * Config: `ArtifactsConfig.boot_parent` / `comp_parent: Optional[ArtifactSpec]` (default None), property
    `role_parents` (both set), `parent_specs()` (root parent + role parents), `parent_keys()` (`g:a` + aliases of
    every configured parent). `parent` stays the ROOT pom's parent (company: `base-parent`). `config/artifacts.example.yml`
    shows the company shape (`base-parent` / `boot-jar` / `comp-jar`, all 4.0.0). Every "configured in-house parent"
    rule now covers the role parents: `BuildFixLoop._parent_abort` (an unresolvable role parent aborts the run,
    `parent_not_found`), `Orchestrator._check_parent_in_rewrite`, preflight checks `artifacts.boot_parent` /
    `artifacts.comp_parent`, `_boot_managed_project` markers.
  * `RestructureResult.parents_set: list[str]` — one entry per parent set or replaced
    (`"<pom>: <old g:a:v or '-'> → <new g:a:v>"`), recorded in every mode (without role parents: the root entry);
    with role parents one note `role parents: N parent(s) set`.
  * Layout with role parents: root = aggregator on `artifacts.parent` (`<modules>` = comp + boot; no dependencies,
    dependencyManagement, pluginManagement, mandatory deps, BOM or release property; only the properties it still
    references); `module/boot-jar` on `boot_parent`, `module/comp-jar` on `comp_parent`, both `<relativePath/>` +
    explicit groupId/version (the project's); both module poms get the 2 mandatory dependencies, the Boot BOM
    (`parent_manages_spring_boot: false`), `maven.compiler.release` (`parent_manages_java_version: false`),
    `remove_when_present`, the root's dependencyManagement (minus merged-module entries), pluginManagement and
    repositories; the comp pom also gets every root property + the management / properties folded from merged
    modules; the boot pom the root properties it references + `project.*` / `maven.*`; a kept boot module takes
    `boot_parent` whatever its old parent was (explicit coordinates: its own, else the root's) and the root content
    only when it inherited the root before. Templates `boot-pom.xml.j2` / `comp-pom.xml.j2`: optional `group_id` /
    `version`, empty `relative_path` → `<relativePath/>` (byte-identical output without role parents).
  * `wrap` with role parents: wrapped modules stay on the root (which keeps the mandatory deps); the generated
    boot-jar takes `boot_parent`. `msa_pom`: service roots → `boot_parent`, library roots → `comp_parent`, the top
    aggregator (`msa_parent_policy: top-aggregator`) → `artifacts.parent`.
  * Old role parents: any project pom (root excluded) whose `<parent>` has the groupId:artifactId of `boot_parent` /
    `comp_parent` at any version moves to the configured GAV with `<relativePath/>` — in layout (kept boot, wrapped
    modules), `msa_pom`, and the already-restructured pass (which also moves the root to the configured
    `artifacts.parent` version); a different parent key is noted and left; when both role parents share one key the
    module location decides. Idempotent: a second run changes nothing; a configured version change moves all three.
    `layout.drop_managed_versions` never touches the role-parent artifacts.
  * Fixture `legacy-role-parents` (21 fixtures): root{boot-jar, catalog-service, order-service} on the stand-in
    `spring-boot-starter-parent` 2.7.18 (old 1.0.0 line), `sbmig.e2e.yml` sets `boot_parent` / `comp_parent` =
    `spring-boot-starter-parent` 4.1.0; `tests/fixtures` `PARENT_OVERRIDES` registry for such overrides.

* 2026-10-21 (round 9c — the company layout as stated by the user: root = base-parent aggregator, `{name}-boot` /
  `{name}-back` / `{name}-front`, Boot as-is in place):
  * Config `LayoutConfig` (docstring = the rule): `module_dir: "" ` (modules directly under the root), `boot_module
    "{name}-boot"`, `comp_module "{name}-back"`, `front_module "{name}-front"` (None = no front module),
    `project_name` (`{name}` override; default the root artifactId), `front_static_dir`
    (`src/main/resources/static`), `front_jsp_dir` (`src/main/resources/META-INF/resources/WEB-INF/jsp`),
    `boot_as_is: in-place | layout`, `boot_module_pattern "*-boot"`, `jsp_boot_packaging: war | jar`,
    `comp_artifact_suffix "-back"`; helpers `module_name(which, name)` / `module_path(which, name)`.
    `ArtifactsConfig.mandatory_target: root | modules` (default `root`). The pre-9c layout is pinned by
    `config/artifacts.local-e2e.yml` (`module/boot-jar` + `module/comp-jar`, `front_module: null`, `boot_as_is:
    layout`, `comp_artifact_suffix: -comp`) so the 21 older fixtures are unchanged (byte-identical trees, snapshot
    `tests/restructure/snapshots/pre9c_trees.json`).
  * Models: `SourceStats.jsp_paths` (every `*.jsp`, sorted), `web_static_dirs` (`[<module>/]src/main/webapp` minus
    `WEB-INF`, `[<module>/]src/main/resources/{static,public,templates}`, listed when holding a file with an
    extension in `sbmig.analyze.source_scanner.WEB_STATIC_EXTENSIONS`), `web_static_count`;
    `ProjectProfile.boot_modules` (module dirs reachable from the root `<modules>` — MSA: service roots too — whose
    basename matches `layout.boot_module_pattern` AND whose pom has spring-boot-maven-plugin or a non-test
    `@SpringBootApplication`; the project root is never listed; function
    `sbmig.analyze.classifier.boot_module_findings`). Profile warnings: "boot module <dir> is not named <pattern>
    (company naming rule)", "<dir> is named like a boot module but its pom is not a Boot app"; reason "boot modules
    (<pattern>): <dirs>". `RestructureResult.boot_module / comp_module / front_module / boot_packaging /
    front_files`; `RestructurerLike.apply(mode="boot_pom")`.
  * Planner: `case_type == spring_boot` (non-MSA, with sources) + `layout.boot_as_is == "in-place"` →
    `restructure.boot_pom` (`StepKind.MSA_POM`, params `{"mode": "boot_pom", "boot_modules": [...]}`, title "Boot
    as-is: parents / mandatory / optional in place (directories kept)") then `restructure.loop` + `verify.build`;
    `layout` and every non-Boot case keep `restructure.apply` (title lists the modules from `module_path`, front only
    when `jsp_paths` / `web_static_count`). `restructure.msa_pom` params gain `boot_modules`. Plan notes name the rule.
  * Restructure (layout mode): every module path is `module_path(which, name)`, `name = layout.project_name or the
    root artifactId`; root `<modules>` order back, (front), boot; artifactIds `<root>` + `boot_artifact_suffix` /
    `comp_artifact_suffix` / `-front`; "already restructured" = root packaging pom with the boot and back dirs among
    its modules. Front rules (`front_module` set and web files exist): `src/main/webapp/**` minus `WEB-INF` /
    `META-INF` → `<front>/<front_static_dir>/…`; `src/main/resources/{static,public,templates}/**` →
    `<front>/src/main/resources/<dir>/…`; `WEB-INF` JSPs → `<front>/<front_jsp_dir>/<path under WEB-INF/jsp, else under
    WEB-INF>`; JSPs outside `WEB-INF` → `META-INF/resources/<path>`; `WEB-INF/tags/**` + `*.tld` →
    `META-INF/resources/WEB-INF/…`; other `WEB-INF` files → `<boot>/src/main/resources/asis-webinf/`; `WEB-INF/lib|
    classes` and webapp `META-INF` stay in back (follow-up); front pom packaging jar, parent `comp_parent` (role) or
    the root. WAR rules (JSPs moved and `jsp_boot_packaging: war`): boot packaging `war`, `spring-boot-starter-tomcat`
    + `tomcat-embed-jasper` provided, `spring-boot-web-server` compile when `@ServletComponentScan` is used, JSTL
    taglib → `jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api` + `org.glassfish.web:jakarta.servlet.jsp.jstl`
    (BOM-managed), the generated Application extends `SpringBootServletInitializer` (template `servlet_initializer`),
    `spring.mvc.view.prefix/suffix` written from an as-is `InternalResourceViewResolver` / `<mvc:jsp>` /
    `registry.jsp` (prefix mapped to the new location) else a follow-up; follow-up "boot module packaged as WAR for
    external Tomcat; JSPs are served from the front jar (META-INF/resources)". Templates: `boot-pom.xml.j2`
    `{{ packaging }}`, new `front-pom.xml.j2`, `Application.java.j2` `servlet_initializer`.
  * `apply(mode="boot_pom")` (Boot as-is): directories kept; root → `artifacts.parent` (aggregator); modules in
    `profile.boot_modules` (fallback: boot plugin or `@SpringBootApplication` under the dir) → `boot_parent`, every
    other module → `comp_parent`, explicit groupId/version + `<relativePath/>` plus the dependencies / plugins /
    management they inherited (without role parents the modules keep inheriting the root); a single-module app is
    wrapped into `{name}-boot` under an aggregator root; placeholders into each boot module; idempotent ("boot_pom:
    already in place"). `drop_managed_versions` runs here too (orchestrator passes `effective_pom`).
  * `mandatory_target`: `root` → the mandatory dependencies go into the root pom (also an aggregator; MSA with role
    parents: each service root + the top aggregator); `modules` → every role module pom incl. front (round 9b).
    MSA parents with role parents: service root → `boot_parent`, library root → `comp_parent`, top aggregator →
    `artifacts.parent` (also under `per-service-root`).
  * Run outcome: a failed / crashed `layout` / `boot_pom` / `msa_pom` step → flag `restructure-failed`, follow-up
    "restructure step <id> (<mode>) failed: … — the tree keeps the as-is layout / parents; the final build ran on it,
    so BUILD SUCCESS here does not mean the migration is complete", journal `restructure_failed`, `report.success =
    False` (exit 2); a failed `optional_bump` is a follow-up only.
  * Report / loop paths: `sbmig.report.handover.resolve_current_path(ws, path, cfg, name)` and the merge-conflict
    hints resolve module paths through `module_path` (`sbmig.report.handover.project_name(profile)`); the
    system-scoped-jar handler tries every dir matching the comp template.
  * e2e: post-check kinds `pom_packaging {path, packaging}` (missing = jar), `pom_module_list {path, modules,
    any_order}`; `tests/fixtures` `COMPANY_OVERRIDES` registry (layout + role-parent overrides). Fixtures
    `legacy-role-parents` (Boot as-is in place), `company-boot-single`, `company-spring5-web`, `company-servlet-jsp`
    (24 fixtures; 26 with `boot2-rewrite-excluded` and `boot2-qlrm-javax`, 2026-10-22).
  * Follow-ups from the first company run (same day): `openrewrite.exclusions` (default `**/*.jsp, **/*.jspx,
    **/*.html, **/*.htm` → `-Drewrite.exclusions`; OpenRewrite's XML parser reads those and logs ANTLR noise);
    handler `jstl-to-jakarta` (`JstlToJakarta(RelocationHandler)`, pre-emptive in the spring phase via
    `pre_handlers.JSTL_JAVAX_KEYS`, reactive on the missing-version / jstl wording): javax JSTL coordinates →
    `org.glassfish.web:jakarta.servlet.jsp.jstl` / `jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api`
    (BOM-managed; explicit old version → 3.0.1 / 3.0.0); library rule + advisory `jdk-generified-apis`
    (`javax.swing.tree` imports, guide) with `## fix-loop: jdk-generified-apis` in `knowledge/notes/jdk21.md`
    (15 advisories).
  * Operators (round 9c): `BuildFixLoop` milestones (`_mark`: `loop start`, `mvn <goal> (build N)`, `KB match`,
    `deciding`, `handler <id>`, `advisory hints`, `merge-conflict hints`, `fixer.propose … (prompt packing + LLM
    call)`, `LLM answered: applying`, `git commit`, `idle`; DEBUG lines `[phase] → <milestone>`) and a watchdog
    thread (`WATCHDOG_S` 30 s, `WATCHDOG_QUIET_S` 60 s) that warns `fix loop quiet for Ns since '<milestone>'`;
    `LLMFixer.propose` logs `fix prompt for <cluster>: packing …` and `… packed: N chars, sections […] → calling the
    LLM`; `sbmig.cli.dump_threads(out_dir)` / `install_thread_dump_signal()` (`SIGUSR1`, installed by
    `setup_logging` in the main thread) write `out/threads-<ts>.txt`.
  * Windows / stuck-run follow-ups (round 9c): `sbmig.core.diag` (`thread_dump_text`, `dump_threads(out_dir)`,
    `install_thread_dump_signal`; `sbmig.cli` re-exports), `GET /api/threads` (text + `<out>/threads-<ts>.txt`),
    fix-loop watchdog auto-dump after `WATCHDOG_DUMP_S` (300 s) into the run dir; `Workspace._git` runs with
    `stdin=DEVNULL`, `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=echo`, `GCM_INTERACTIVE=never`, `GIT_EDITOR=true`,
    `-c commit.gpgsign=false -c tag.gpgsign=false -c core.hooksPath=<run dir>/no-hooks` and `timeout=GIT_TIMEOUT_S`
    (600 s → `GitError` naming the likely blocker).
  * `plan.parent_phase: restructure | spring` (default `restructure`; `config/artifacts.example.yml` sets `spring`):
    `spring` moves `restructure.apply` / `restructure.boot_pom` / `restructure.msa_pom` into the SPRING phase right
    before the first spring build-fix loop (after `spring.rewrite` and `spring.optional_bump`), drops
    `restructure.loop` (the spring loop gates; `verify.build` stays), and notes it (`planner._apply_parent_phase`);
    `stop_after=spring` then includes the parents / layout. Windows: `_kill_group` uses `taskkill /PID <pid> /T /F`
    (a terminated `mvn.cmd` left its `java.exe` running); `fixer.grep` is bounded (`MAX_GREP_FILES` 3000,
    `MAX_GREP_FILE_BYTES` 512 KB, `GREP_BUDGET_S` 45 s, one read per file) and logs the context / feedback rounds.
* 2026-10-22 (company run, final verify failed on `org.hibernate.annotations.common`): `LoopConfig.phases["verify"]` (`PhaseLoopConfig(goal="package", max_iterations=3)`) + `LoopConfig.verify_extra_llm_attempts` (2): `Orchestrator._verify_fix_loop` runs one last `BuildFixLoop.run(Phase.VERIFY, …)` after a failed final build and repeats the final build (journal `verify_loop_start` / `verify_loop_end` / `blocked_released`); `Triager.decide` adds `verify_extra_llm_attempts` to both per-cluster limits when `TriageContext.phase == VERIFY`; `BuildFixLoop` requests the escalation model in that phase; `NoteEntry.applies` treats `verify` as `restructure`; the verify phase is harvested when it has attempts. Library rule + advisory `hibernate-commons-annotations`, note `knowledge/notes/hibernate-7.md`.
* 2026-10-22 (company run, jakarta leftovers + Spring Data paging repositories after the rewrite):
  `RewriteResult.parse_problems: list[str]` — files rewrite-maven-plugin reported as `There were problems parsing
  <path>` (`OpenRewriteRunner.parse_problems(text, root=…)`: `\` → `/`, root-relative where possible, module prefix
  like `changed_files`, de-duplicated, log order; `["(unknown files — see the rewrite log)"]` when only the summary
  line `… parsing some source files` appears); the `rewrite` journal event carries a `parse_problems` count, the
  orchestrator adds journal event `rewrite_parse_problems` (`step`, `files`), a phase note and flag
  `rewrite-parse-problems`. Two deterministic source handlers (`sbmig/triage/handlers/source_handlers.py`):
  `jakarta-namespace-sweep` (E13; flag `jakarta-leftovers-renamed`; table `sbmig/triage/handlers/jakarta_packages.yml`,
  mirror of OpenRewrite's `JavaxMigrationToJakarta` ChangePackage rules) and `spring-data-crud-repository` (E14; flag
  `spring-data-crud-restored`). Pre-handler selection (`sbmig/plan/pre_handlers.py`): javax Jakarta EE imports in
  the as-is scan → sweep; a `spring-data*` / `jpa` library → CRUD handler; both run once after the spring recipes
  (`Orchestrator._maybe_pre_handlers`, group SPRING) and reactively on their compile-error wording.
* 2026-10-22, second (fourth company project: `jakarta.persistence.Query cannot be converted to javax.persistence.Query`
  was attributed to `javax.persistence:javax.persistence-api:2.2` — the type's jar, not `org.qlrm:qlrm:2.1.1` that
  still uses it): `sbmig/build/deptree.py` — `dependency_tree(ws, cfg, module=None, includes=()) -> list[TreeNode]`
  (`maven-dependency-plugin:3.8.1:tree -DoutputFile=<file> -DappendOutput=true [-Dincludes=…] -q`, `-pl <dir> -am`
  for a module, package fallback for unresolved reactor siblings, cache `<run dir>/deptree/<name>[-<hash>].txt` +
  `.meta.json` keyed by the pom stamp, 10 min timeout, never raises; journal `deptree` {`module`, `ok`, `includes`,
  `log`}), `parse_tree(text)` (reactor multi-root, classifier, scope, notes, CRLF), `paths_to(roots, predicate)`,
  `TreeNode(gav, scope, depth, children, note, root, text)`, `JAVAX_API_PATTERNS`, `is_javax_api(gav)`,
  `javax_api_inventory(ws, cfg, profile=None) -> list[JavaxApiJar]` (root run + modules that did not show up; test
  scope only when nothing else matches; reactor siblings skipped for `via`; de-duplicated by (artifact, via)).
  Models: `JavaxApiJar(artifact, module, scope, path, via, declared)`, `BlockedItem.via: list[str]` /
  `via_direct: Optional[GAV]`, `RunReport.javax_api_jars`. Config `report.javax_api_inventory: true`. Knowledge
  `knowledge/libraries/jakarta_libraries.yml` + `sbmig/core/jakarta_libraries.py` (`jakarta_libraries()`,
  `jakarta_library_for(key)` — current or `old_keys` coordinates). `sbmig/loop/blocked_registry.py`: `fill_via(items,
  inventory, cfg)`, `match_javax_jar`, `is_javax_api_artifact`, `is_in_house`, `compose_suggestion(…, javax_jar=None)`
  with the X1 ladder (1)–(5) of `docs/ERROR-HANDLING.md`; notes `via: a → b → c`. `Orchestrator(…,
  javax_inventory=None)` — callable `(ws, cfg, profile)`; None = the real inventory with the real Maven only, False =
  off; `_finalize_javax_inventory` runs before `_finalize_blocked` when a spring phase ran (not on dry runs): flag
  `javax-api-on-classpath`, follow-up "N javax API jar(s) still on the classpath after the migration …", journal
  `javax_api_inventory` {`jars`, `via`}. Report: section "javax API jars still on the classpath"
  (`markdown.javax_api_lines`, `render_markdown(…, cfg=None)`), Blocked cell "`api jar` ← `via`", summary row, Handover
  pointer. Flag `jakarta-library-bumped` and handler ids `jakarta-library-floor` / pre-emptive `dependency-relocation`
  (declared javax API artifacts, after the spring recipes) belong to the parallel triage / plan workstream.
* 2026-10-22 era libraries (knowledge tables, no new code seams): `JakartaLibrary.classifier: Optional[str] = None`
  (`sbmig/core/jakarta_libraries.py`, yml field `classifier`) — `jakarta-library-floor` sets it on the bumped /
  relocated declaration (`replace_dependency(g, a, GAV(classifier=…))`, direct + managed) and
  `jakarta_libraries_below` counts a declaration without it as below whatever its version (shown as
  "`<v>` without classifier `<c>`"); the relocation note now says "jakarta release since" (was "jakarta.persistence
  since"). `knowledge/libraries/jakarta_libraries.yml` 1 → 44 entries (one per `g:a`; families via YAML anchors),
  `relocations.yml` 22 → 44 rows (new rows reactive, no `packages`; hibernate rows on the Boot 4.1 versions
  7.4.1.Final / 9.1.0.Final), `known_packages.yml` 27 → 56 rows, `versions.yml` refreshed to the Boot 4.1.0 values
  (+8 keys), `MysqlConnectorRelocation` → 9.7.0. Analyzer rules (`LIBRARY_RULES`, contract names) +16:
  `hibernate-template`, `commons-fileupload`, `jackson1`, `tiles`, `velocity`, `axis1`, `commons-httpclient3`,
  `commons-collections3`, `ehcache2`, `json-lib`, `jjwt`, `poi`, `struts2`, `egovframe`, `dbcp1`,
  `querydsl-hibernate-native`. Advisories 15 → 64 (49 new, all `guide` except `commons-collections3` = `recipe`
  with `org.openrewrite.apache.commons.collections.UpgradeApacheCommonsCollections_3_4`, catalog `artifact:
  rewrite-apache`, `provided_by: rewrite-spring`; recipe catalog 33 → 34 ids). Note `knowledge/notes/era-libraries.md`
  (`## fix-loop: <library>` keyed on the finding's library = rule name, or the advisory id for artifact-only entries).
  Mirror gap: `sbmig/plan/pre_handlers.py::jakarta_library_reason` does not know `classifier` yet (pre-emptive
  selection misses a classifier-only gap; the reactive path covers it). See docs/ADVISORIES.md "2026-10-22 era libraries".
* 2026-10-23 `sbmig inventory` (pom-less Tomcat webapps, round 1 = read only): models `JarInfo`, `WebappRoot`,
  `SourceRoot`, `ModuleProposal`, `WebappInventory` (`sbmig/core/models.py`). Analyzer
  `sbmig/analyze/webapp_inventory.py::scan_webapp_tree(root, *, max_sample=8, name=None) -> WebappInventory` (pure
  file-system facts, never raises on a single bad file; `name` replaces the root basename in the proposals, used for
  a flat zip), `jar_names(path=None)` (lru_cached `knowledge/libraries/jar_names.yml` → {stem.lower(): (g:a, note)}),
  `guess_jar(name) -> (artifact, version, stem)` (longest known stem first, else the regex split), `jar_note(jar)`,
  `java_level(major)`; exported from `sbmig.analyze`. Writer `sbmig/report/inventory.py::write_inventory(inv, out_dir,
  cfg=None, *, language=None) -> {"inventory_md", "inventory_json"}` (also sets `inv.outputs`; `language` wins over
  `cfg.report.language`), `render_inventory(inv, language)`; its `tr` literals are part of the i18n drift test. CLI
  `sbmig inventory <dir|zip> [--out out/inventory] [--config …] [--json]` (exit 0, 3 for a missing path; config
  optional — artifacts stubbed like `llm-probe`), `sbmig.cli.inventory_console_lines(inv)`. Fixture
  `fixtures/webapps/legacy-exploded` (not an e2e fixture). Round 2 (`synthesize-pom`) consumes `inventory.json`.
* 2026-10-23 web.xml → Boot registrations (catalog W1): models `WebXmlServlet`, `WebXmlFilter`, `WebXmlErrorPage`,
  `WebXmlModel`, `SourceStats.web_xml_spec / web_xml_servlets / web_xml_filters / web_xml_listeners`
  (`sbmig/core/models.py`), `LayoutConfig.web_xml_registrations: generate | followup` (default `generate`). Parser
  `sbmig/analyze/webxml.py::parse_web_xml(text, path) -> WebXmlModel` (namespace-agnostic, DTD 2.2 / 2.3 spec from
  the system id, never raises; exported from `sbmig.analyze`); `ScanResult.web_xml_models` (path → model, kept by
  `subset`). Generator `sbmig/restructure/webxml_registrations.py`: `load_skip_rules(path)` (skip table
  `knowledge/config/web_xml_rules.yml`), `plan_registrations(model, *, rules, lookup, component_scan, web_path, war)
  -> RegistrationPlan` (pure; `lookup(fqcn) -> ClassInfo | None`), template `templates/WebXmlRegistrations.java.j2`;
  `Restructurer._write_boot` calls `_web_xml_registrations` after the Application (layout mode). New
  `RestructureResult.flags: list[str]` (the orchestrator adds them to `RunReport.flags`; first flag
  `web-xml-registered`); journal `web_xml_registrations` (`path`, `spec`, `servlets`, `filters`, `listeners`,
  `error_pages`, `skipped`, `jsp_file_servlets`, `class`), written by the restructurer. Boot 4.1 FQCNs (javap on
  spring-boot-4.1.0.jar): `org.springframework.boot.web.servlet.{ServletRegistrationBean, FilterRegistrationBean,
  ServletListenerRegistrationBean}`, `org.springframework.boot.web.error.{ErrorPage, ErrorPageRegistrar}`;
  `setDispatcherTypes` takes `jakarta.servlet.DispatcherType`. Fixture `servlet-webxml-legacy`.
* 2026-10-23 non-standard directories (`APP/src`, web root `STATIC/`, `DB/`): `SourceStats.webapp_dirs: list[str]`
  (root-relative web dirs: the default `src/main/webapp` when it exists + every maven-war-plugin `warSourceDirectory`
  and `<webResources>` directory; kinds not recorded), `SourceStats.large_media_files: list[str]` (≥ 5 MB media /
  archive files under a web dir or classpath resource dir, capped at 20) and `large_media_bytes: int` (total).
  `sbmig/analyze/source_scanner.py`: `scan_sources(ws, poms=None)` (the analyzer passes the poms; None = old
  behaviour), `web_static_root(rel, web_dirs=(), resource_dirs=())`, `WebDir(dir, kind: root | resources, target)`,
  `pom_web_dirs(text)`, `pom_build_dirs(text)`, `clean_pom_dir(raw)`, `webapp_dirs(ws, poms) -> (web dirs, classpath
  dirs)`, constants `LARGE_MEDIA_EXTENSIONS`, `LARGE_MEDIA_BYTES`, `MAX_LARGE_MEDIA`; `ScanResult.webapp_dirs /
  large_media`. Restructure: `_source_roots` includes the war web dirs; `_populate_front(c, comp, boot, coords,
  eds=None)` splits every web root (WEB-INF / META-INF rules → JSPs → files under a `<resource>` / source dir inside
  the root stay in comp → the rest to `front_static_dir`; `targetPath` prefixes webResources); `warSourceDirectory`
  / `webResources` are stripped from the rewritten root and the generated boot / comp / front poms (note); large
  media note. e2e post-check `file_same_as {path, source}` (byte-equal to the as-is fixture file;
  `run_post_check(check, project, fixture_dir=None)`). Fixture `servlet-nonstandard-dirs`.
* 2026-10-23 **Error cases** (business requirement: "KB holds too little — every build error needs its own record:
  what it was, what was done, how it was resolved or why not; failed fixes included; the code an LLM applied; text
  replace vs LLM"). Models in `sbmig/core/models.py`: `FixMethod`, `ChangeKind`, `ErrorCaseStatus`, `ReplacePair`,
  `ChangeClass`, `FixAttemptRecord`, `ErrorAttemptRef`, `ErrorCaseRecord`; `FixAttempt.base_commit /
  proposed_changes / introduced_errors`; `RunReport.error_cases / error_case_counts`. Config `kb.error_cases`,
  `kb.error_hints`, `kb.error_diff_chars`, `kb.error_excerpt_chars`. Contract below (section "Error cases").

## Error cases (2026-10-23)

Three parts, three owners: **tracker** (`sbmig/loop/error_tracker.py` + hooks in `fixloop.py` / `orchestrator.py`),
**KB** (`sbmig/kb/changes.py`, `sbmig/kb/error_cases.py`, `store.py`, `cli.py`, `ledger.py` learning section),
**report** (`sbmig/report/`). Files:

| file | written by | content |
|---|---|---|
| `<kb.dir>/errors.jsonl` | KB `append_error_cases` | `ErrorCaseRecord` per build error per run (append-only) |
| `<kb.dir>/fix_attempts.jsonl` | KB `append_error_cases` | `FixAttemptRecord` per attempt per run (append-only, full diff) |
| `<kb.dir>/error_hints.json` | KB (incremental; `sbmig kb reindex` rebuilds) | `{version: 1, updated_at, by_generic: {L2: entry}, by_symbol: {symbol: entry}}` |
| `<run dir>/error_cases.jsonl`, `fix_attempts.jsonl` | orchestrator | the run's prepared records (also with `kb.harvest: false`; not in dry-run) |
| `<run dir>/error_cases.csv` | orchestrator via KB `write_error_cases_csv` | Excel copy (UTF-8 with BOM; columns `ERROR_CSV_COLUMNS`, round 10 below) |

### Tracker — `sbmig.loop.error_tracker.ErrorTracker(ws, cfg)`
One per run, created by the orchestrator (not in dry-run), handed to the loop as `BuildFixLoop.error_tracker`
(attribute, default None — the loop never requires it). Never raises (every public method catches and logs).

```python
note_step(step_id: str, phase: Phase) -> None
    # a plan step outside the fix loop ran (openrewrite / handler / restructure / msa_pom / optional bump): errors that
    # disappear before the next observed build with no scored attempt are attributed to these steps (plan_step)
observe(phase: Phase, label: str, build: BuildResult, scored: list[FixAttempt], *, void: bool = False) -> None
    # every build: the loop after `_score` (scored = the attempts of the previous round, in order), the orchestrator
    # after each final `clean package` (label "verify-final", "verify-final-2", scored = []).
    # void=True (the round was reverted, or a failed build parsed no errors / timed out): only the scored attempts
    # are annotated; nothing opens or closes.
observe_clusters(clusters: list[ErrorCluster]) -> None     # the triage clusters of the last observed build
note_attempt(att: FixAttempt) -> None                     # every recorded attempt (BuildFixLoop._record), any outcome
note_decision(d: TriageDecision) -> None                  # every decision (SKIP / ESCALATE / BLOCKED rationales)
finalize(blocked: list[BlockedItem], *, run_id: str) -> tuple[list[ErrorCaseRecord], list[FixAttemptRecord]]
    # raw records: ids, error fields (unsanitized), lifecycle, status, attribution, description, failure_reason,
    # problem_errors, attempts history, block_reason / suggestion, introduced_by; FixAttemptRecord with description,
    # failure, introduced / remaining errors, raw `diff` in FULL (git `base_commit..commit` limited to the attempt's
    # files; the proposal as search/replace diff when nothing applied; only a pathological diff above
    # `MAX_DIFF_CHARS` = 4 MB is cut — round 10). Project fields, ChangeClass, labels, sanitizing, the
    # `kb.error_diff_chars` cap and the denormalized change fields are left to `prepare_error_cases`.
```

Rules the tracker implements:
* **Identity** of an error = `(kind, norm_file, signature or message[:120])`, `norm_file` = the path from the first
  `src/` segment on (survives the restructure moving files into `{name}-back/`), else the path; several occurrences
  of one identity are matched by exact file first (round 10: the same relative path in two services is two
  errors), then message, then nearest line; a targeted build (`observe(target=…)`, MSA per-service loops) only
  matches records of its own scope — or a record of the root `src/` that the restructure may have moved into the
  target. One record per occurrence. Record id = `sha1(run_id|identity|occurrence index)[:16]`.
* **Visibility** of an absent error in a later build B: B ok → visible; the error's module in `B.modules_built` →
  visible; module == `B.failed_module` or module unknown → visible when B has an error of the same or a later stage
  (stage: pom_invalid / parent_resolution 0, dependency / plugin_resolution 1, enforcer / rewrite 2, compile /
  unknown 3, test_compile 4, test_failure 6; plugin_execution = no stage → not visible unless the module built);
  otherwise masked. Absent + visible → closed (resolved / resolved_indirectly); absent + masked → stays open but
  marked "not seen since <label>"; a closed error that shows up again reopens (history kept in `notes`).
* **Attribution** when closed at build B: scored attempts of B (not `reverted`) whose cluster contained the identity
  → `resolved`, `targeted=True`, first such attempt; else a scored attempt that touched its file → `resolved`,
  `targeted=False`; else scored attempts exist → `resolved_indirectly`, `resolved_by` = the first, all ids in
  `notes`; else steps noted since the last build → `resolved_indirectly`, `fix_method="plan_step"`,
  `resolved_by="step:<id>"`; else `resolved_indirectly`, `fix_method="none"`, note "gone between <label> and
  <label>".
* **Resolution chain** (2026-10-25) of an error closed by an attempt: `resolution_attempts` = the attempts, in run
  order, scored by build B since the error (re)appeared, that are applied, not `reverted` / `apply_failed`, and whose
  cluster contained it or that changed its file — plus the resolver itself. An earlier `no_change` attempt stays in
  the tree (only regressions are reverted), so its change (an import, a helper) is part of the fix the resolver
  completes; note "resolved by N attempts whose changes stayed in the tree: …" when N > 1. A reopen starts a new
  chain.
* **introduced_by** (round 10) of a new error: the attempts scored in the build where it first appeared that
  changed its file (applied, not reverted / apply_failed; note "appeared at <label> after attempt(s) … changed its
  file"), else the plan steps noted since the previous build (`step:<id>`); comma-joined in the record.
* **End state** (finalize): open and present in the last observation → `blocked` when a BlockedItem covers it
  (its signature in `item.signatures` or the item's errors; `block_reason`, `suggestion`), else `unresolved`;
  open and masked → `not_visible` (only when the last build failed).
* **failure_reason**: last targeting attempt → "`<method>` attempt `<id>` `<outcome>`: `<failure>`"; no attempt →
  the last decision rationale for a cluster that contained it ("no fix attempt: …"), else "no fix attempt before the
  loop ended". `problem_errors`: the last failed attempt's introduced errors (regressed), else the error itself
  (no_change), else its apply errors. Blocked: "blocked (`<reason>`): `<suggestion or evidence>`".
* **FixAttemptRecord.failure** by outcome: apply_failed → apply errors; regressed / reverted → the "regressed: …"
  note + introduced errors; no_change → "the targeted error(s) were still there after the rebuild"; pending →
  "not scored (the run ended before the rebuild)"; partial / inconclusive (round 10) → the loop's own note
  ("partial: 1 of 2 targeted error(s) gone, 1 remain", "inconclusive: the build timed out — …"); improved /
  build_ok → "". `description`: handler →
  "handler `<id>`: `<description>`" (+ notes); recipe → "OpenRewrite `<ids>`"; kb_patch → `att.analysis`; llm →
  "LLM `<model>`: `<analysis>`" (analysis ≤ 1500 chars). `fix_method` from the decision: HANDLER → handler,
  KB_RECIPE → recipe, KB_PATCH → kb_patch, LLM_FIX → llm.

Loop hooks (`fixloop.py`): `_new_attempt` sets `base_commit = ws.head()`; `_apply_llm` / `_apply_kb_patch` keep the
proposal in `proposed_changes` when not everything applied; `_score` stores `introduced_errors` (the `new_in_touched`
errors, ≤ 10) on regressed attempts; `_run` calls `observe` after `_score` (void when `reverted` or the failed build
has no parsed errors) and `observe_clusters` after clustering; `_record` → `note_attempt`; `_journal_decision` →
`note_decision`. Orchestrator: create the tracker before the phases, `note_step` in each non-loop step,
`observe` after each final build, and in `_finalize` (after `_finalize_blocked`, before the report is written):
`errors, attempts = tracker.finalize(report.blocked, run_id=ws.run_id)` → `prepare_error_cases(...)` → run-dir
files + CSV → `kb.append_error_cases(...)` when `kb.harvest and kb.error_cases` and a profile exists →
`report.error_cases` (copies with `diff_excerpt=""`), `report.error_case_counts` (status → n, `llm_text_replace`),
`report.outputs["error_cases"]` / `["fix_attempts"]` / `["error_cases_csv"]`, journal `error_cases` (`count`,
`by_status`, `attempts`). A crash here never fails the run (flag `error-cases-failed: <msg>`).

### KB — `sbmig/kb/changes.py`, `sbmig/kb/error_cases.py`
```python
from sbmig.kb.changes import classify_diff        # (diff_text: str) -> ChangeClass   pure, never raises
from sbmig.kb.error_cases import prepare_error_cases
    # (errors, attempts, profile, ws, cfg) -> tuple[list[ErrorCaseRecord], list[FixAttemptRecord]]
    # fills project / fingerprint / case_type (+ asis_java / asis_spring_boot / asis_spring / packaging from the
    # profile, schema_version / agent_version, round 10); sanitizes (relative posix paths, ws roots stripped, secret
    # VALUES masked by sbmig.kb.redact, raw ≤ 2000); classify_diff on the FULL diff → attempt.change, THEN the cap
    # (diff ≤ kb.error_diff_chars, 0 or negative = no cap, diff_truncated); labels
    # (label_changes on the attempt's FileChanges when they carry search/replace, else from the diff's pairs);
    # error_symbol (symbol_for_errors); denormalizes the resolution into each resolved error: `resolution_attempts`
    # (the tracker's chain, else the resolver alone; unknown ids dropped), change_kind / replace_pairs of all of them
    # combined (`combine_changes`: one kind → it, several → mixed), fix_labels (union), diff_excerpt = per attempt the
    # hunk of the error's own file containing / nearest the error line (`changes.excerpt_for`, then the next nearest
    # while they fit) ≤ kb.error_excerpt_chars ("# attempt <id>: <method> <resolver> → <outcome>" header per section
    # when there are several), replace_rule_candidate = fix_method == "llm" and the combined change_kind ==
    # "text_replace" and no replace pair touched a masked secret); attempt_count / llm_attempts.
    # A record that fails to prepare is stored minimal (no diff / raw), never as given.
from sbmig.kb.changes import combine_changes      # (list[ChangeClass]) -> ChangeClass   pure, never raises
from sbmig.kb.error_cases import write_error_cases_jsonl, write_error_cases_csv   # (records, path) -> Path
from sbmig.kb.error_cases import ErrorCaseStore    # ErrorCaseStore(kb_dir): append(errors, attempts) -> int,
    # iter_errors(**regex filters: status, method, change_kind, project, symbol, signature, run_id),
    # iter_attempts(...), get_error(id), attempts_of(error), summary(run_ids=None) -> dict,
    # replace_candidates(min_projects=1) -> list[dict], hints_index() / rebuild_hints() / hints_for(sig2, symbol)
FileKnowledgeBase.append_error_cases(errors, attempts) -> int     # store + hints index update; never raises
FileKnowledgeBase.match(cluster)    # KBMatch.hints also gets error-case hints (kb.error_hints): per L2 / symbol of
                                    # the cluster at most 2 lines — the most frequent past fix ("past runs: fixed by
                                    # <method> <resolver> in N project(s): <description>; replace `a` → `b`") and the
                                    # most frequent past failure ("past failure ×k: <method> <outcome> — <failure>;
                                    # do not repeat it"); kb.max_hints still caps the list. Round 10: a failure is
                                    # counted per error from ITS attempts (`error_cases.failed_refs`): an attempt
                                    # that is part of that error's resolution, or ended partial / inconclusive /
                                    # pending, is never a failure for it
```
CLI (`sbmig kb errors …`): `list` (filters `--status --method --change-kind --project --symbol --signature --run
--case <id prefix> --resolver <regex> --limit --json`; `--case` / `--resolver` round 10), `show <id>` (record + its
attempts; the resolving diff — every resolution attempt's diff, in run order, when there are several), `summary
[--run]` (streamed, round 10), `replace-candidates [--min-projects]` (LLM text-replace fixes grouped by L2 + replace
pair: count, projects, example — rule / command candidates; never a pair with `***`), `export --csv PATH
[--attempts-csv PATH]` (same filters; a filtered `--attempts-csv` also holds each error's resolution attempts).
`sbmig kb reindex` also rebuilds
`error_hints.json`; `sbmig kb stats` adds `error_cases` / `fix_attempts` counts. The fleet `learning.md`
(`render_learning_md`) gains an "Error cases" section (status × method, top failure reasons, top text-replace
candidates) when the KB has error cases for the fleet's runs.

### Report — `sbmig/report/`
`report.md` section "Build errors and how each was handled" (after "Unresolved errors"): one line of counts by
status and by fix method, then a table (≤ 40 rows, unresolved / blocked first): status, `file:line`, message, how
(method + resolver) or why not (failure_reason), attempts; a note pointing to `error_cases.csv` / `.jsonl`. Korean
strings in `sbmig/report/i18n.py` (drift test). `report.json` carries `error_cases` / `error_case_counts` as-is.

Implementation notes (2026-10-23, as built — they refine the contract above):
* `ErrorTracker.observe(phase, label, build, scored, *, void=False, target=None)`: `target` = the MSA service /
  module the build covered; errors outside it are untouched (an ok build of one service never closes another
  service's errors). Labels get `@<target>`, repeated labels `#n`. Repeated attempt ids (MSA loops number per
  service) get record ids `<run_id>:<attempt id>#2`.
* Visibility, additionally: an ok build proves an error gone only when its goal reached the error's stage
  (compile errors need `compile`, test-compile errors `test-compile`); `-Dmaven.test.skip=true` / `-DskipTests` builds
  prove nothing about test-compile / test-failure errors (such errors end `unresolved` with a note); a build without
  `failed_module` uses the stage rule; the runner's stand-in errors ("mvn exited with code …", "build timed out") are
  no evidence. Attribution covers the whole masked period (any attempt / step between the last sighting and the
  closing build). A masked error covered by a BlockedItem stays `not_visible` but keeps `block_reason` /
  `suggestion`; `problem_errors` of a `not_visible` error = the errors that hid it. Internal tracker failures go to
  `tracker.problems` (→ flags). `note_step` is called only when the step committed something. A recipe batch
  attempt's `base_commit` is the HEAD before the rewrite run.
* Orchestrator: no tracker in dry-run or with `kb.error_cases: false`; `_finalize_error_cases` runs after the
  advisories and before `_record_run_ledger`. If `sbmig.kb.error_cases` cannot be imported, raw records go to the
  run directory only (no CSV, no KB). Journal `error_cases` also carries `kb_appended`, `llm_text_replace`.
* KB: `ErrorCaseStore.append` returns errors + attempts written (`append_counts` gives both); `replace_rule_candidate`
  only for `status == "resolved"`; an error's `replace_pairs` = the pairs in its own file, else all of the attempt's;
  classification runs on the path-stripped, not yet masked FULL diff, before the cap (round 10); a replace pair
  that touches a masked secret value is dropped (note `redacted: …`) and that resolution is never a rule
  candidate; the stored diff / excerpt are masked; sanitizing also maps the home directory to `~`;
  `error_*_chars <= 0` = no cap. Hint lines start with
  `[error <key>]`; `error_hints.json` entries carry `variants` (≤ 25 per key, first-seen order) so an incremental
  update equals a rebuild. `Ledger.summary()` includes `error_cases` (read from `errors.jsonl` next to the ledger),
  so the fleet `learning.md` gets the section; `render_learning_md(..., error_cases=None)`. `sbmig kb export` /
  `import` carry the three files (merge = append unknown ids, then rebuild the hints; counters `error_cases_added`,
  `fix_attempts_added`); `write_fix_attempts_csv` exists next to `write_error_cases_csv`.
* Report: `sbmig/report/markdown.py::error_case_lines(r, lang)`, `ERROR_CASE_MAX_ROWS = 40`; web `SERVED_FILES` +
  `error_cases.csv`, `error_cases.jsonl`, `fix_attempts.jsonl` (`.zip` / `.csv` / `.jsonl` always as attachments).
* e2e: `expect.error_cases` (`min`, `status`, `status_min`, `records[]`, `run_files`, `kb_errors_min`,
  `kb_attempts_min`; `scripts/e2e.py::evaluate_error_cases`). Fixture `boot2-fix-failures`.

* 2026-10-23 **Sibling poms without a root pom, EAR modules, WAR overlays** (company case: upload root `APP/`, `DB/`,
  `STATIC/`, no pom.xml; `APP/BATCH` jar, `APP/WEB` war, `APP/EAR` ear packaging WEB as `<type>war</type>`; a run on
  `APP/EAR` alone copied that WAR dependency into the back module). `sbmig/analyze/aggregator.py`:
  `plan_aggregator(ws) -> AggregatorPlan | None` (no root pom.xml, ≥ 2 top poms linked by dependencies — artifactId
  equal, groupId equal / empty / `${…}`, through aggregator subtrees — exactly one linked group; dependency-order
  `modules`, group / version / `name` from the EAR, else the WAR, else the most common / common prefix;
  `artifact_id` = name, or `<name>-aggregator` on a collision; unlinked top poms in `excluded`),
  `aggregator_pom_text(plan)`, `write_aggregator(ws, plan) -> "pom.xml"` (property `sbmig.project-name` =
  `PROJECT_NAME_PROPERTY`), `ear_without_modules(ws) -> str | None` (root pom packaging ear without `<modules>` whose
  war / ejb / ear or same-group dependencies are not in the tree). `LayoutConfig.generate_aggregator: bool = True`.
  Orchestrator `_intake_layout(report)` after the build-tool check, before the effective-pom capture: EAR alone →
  flag `ear-without-modules` + abort (exit 3), journal `ear_without_modules`; plan → write + commit, flag
  `aggregator-generated`, follow-up, journal `aggregator_generated` (`path`, `artifact_id`, `name`, `modules`,
  `excluded`). `sbmig/analyze/source_scanner.py`: `clean_pom_dir` / `pom_web_dirs` / `pom_build_dirs` take
  `allow_parent: bool = False` (True: `../` paths returned normalized). Restructurer: `{name}` = `layout.project_name`
  → root property `sbmig.project-name` → root artifactId (the root is renamed to it and the property removed after
  the merge); EAR modules not merged (files → `<boot>/src/main/resources/asis-ear/<dir>/…`, internal dependencies
  dropped, the rest → back, `maven-ear-plugin` never in a generated pom, context-root follow-up, flag
  `ear-module-dropped`, journal `ear_module_dropped`); internal dependencies also by parent groupIds and by
  artifactId with an empty / `${…}` groupId; `<type>war</type>` dependencies never in a jar module — to the boot WAR
  with the as-is `<overlays>` block (flag `war-overlay-moved`, repository follow-up) or a follow-up when boot is a jar;
  `../` module directories inside the project moved to `<back>/<project path>`; JSPs left outside every module →
  follow-up per top directory + flag `web-content-left`; standalone `main` classes of merged jar modules →
  follow-up. Report FLAG_HELP (+ ko) for the five flags. Fixtures `ear-sibling-poms`, `servlet-war-overlay`
  (+ `fixtures/libs/legacy-base-web/v1.0.0`, installed by `fixtures/libs/install.sh`).

## Case catalog (2026-10-23)

User decisions: the case key is the L2 signature (variants by owner class / L1), promotion is automatic, the learned
text replace applies to the reported error lines (+ imports) only. Models in `sbmig/core/models.py`: `CaseStatus`,
`CaseActionKind`, `CaseVariant`, `CaseFix`, `CaseFailure`, `CaseEvent`, `CaseAction`, `CaseEvidence`, `CaseHuman`,
`CatalogCase` (`effective_status / effective_action / effective_name`: the `human` block wins), `CaseHit`;
`ErrorCaseRecord.case_id`. Config `kb.catalog`, `kb.case_replace`, `kb.case_replace_scope`; promotion uses
`kb.auto_promote_after_projects`.

Files (`<kb.dir>/`): `catalog/<error_kind>/<error_class|other>/<slug(symbol)|unknown>--<id>.yml` (one case, YAML,
`human:` block edited by people), `catalog/index.json` (`{version: 1, updated_at, cases: {id: path}, by_l2: {sig: id},
by_l1: {sig: id}, by_symbol: {symbol: [id]}}`, rebuildable), `views/cases.md` + `views/cases.csv` (generated, one
row per case sorted by projects then seen: id, name, status, action, seen, projects, resolved by, latest fix, top
failure). The events (`errors.jsonl`, `fix_attempts.jsonl`) stay the source of truth.

Case id = `sha1(signature_generic)[:10]`; `ErrorCaseRecord.case_id` is set in `prepare_error_cases` (empty when
there is no L2). Auto rules (recomputed on every update; `human` untouched):
* **name**: `<Owner simple name>.<symbol> — <class with spaces>` when the top variant has an owner, else
  `<symbol> — <class>`; **variants**: by owner (`location` / symbol owner), each with its L1, seen, projects.
* **evidence**: counts over the case's error records; `fixes` grouped by (method, resolver, replace pairs) with the
  latest attempt as example; `failures` grouped by (method, outcome, failure prefix) — round 10: counted per error
  record from ITS attempts (`failed_refs`: an attempt in its `resolution_attempts` / `resolved_by`, or one that ended
  `partial` / `inconclusive` / `pending`, is never a failure for it, so a working fix is no longer "do not repeat
  it"); `timeline` = last 20 records (`attempts` = "method:outcome" in order, so a failure followed by a fix is
  visible); `action_*` = records resolved / not resolved by the handler `case-replace` (`fix_method == "handler"`,
  `resolver_id == "case-replace:<case id>:<pair-set hash>"` since round 10 — counted per pair set; a plain
  `case-replace` of older records counts for the top-ranked set).
* **action / status** (first that applies): (1) a KB recipe (active or candidate) whose signatures contain the L2 or a
  variant's L1 → `recipe` (status active if any such recipe is active, else candidate); (2) resolved records with
  `replace_rule_candidate` (LLM text replaces) → `replace` with the pair set fixed in the most distinct PROJECTS
  (then records, then first seen — round 10; before: the most records), status candidate; active once that set's
  `action_projects` ≥ `kb.auto_promote_after_projects` and its `action_failures == 0`; a set with failures ≥ 2 and
  failures > successes falls back (audit reason `fallen back: …`) and the next set is tried; (3) replayable LLM patch
  cases (`KBCase` with this L2) → `patch`, candidate (replayed by the existing KB_PATCH step); (4) otherwise `hint`,
  observed. `block` and `handler` only come from `human.action`; a hit whose action comes from `human.action` is
  `CaseHit.pinned` and triage obeys it (round 10 below).
* **hints** (≤ 3): the best fix ("past runs: fixed by …; replace `a` → `b`") and the top failure ("past failure ×k:
  … — do not repeat it"), same wording as the error hints; `human.hints` go first.

APIs:
```python
from sbmig.kb.catalog import CaseCatalog, case_id_for      # case_id_for(signature_generic) -> str
CaseCatalog(kb_dir, cfg=None)
    .update_from(errors: list[ErrorCaseRecord], attempts: list[FixAttemptRecord]) -> list[str]   # changed ids
    .rebuild() -> int                    # from errors.jsonl + fix_attempts.jsonl + recipes + cases (human kept)
    .get(key) -> CatalogCase | None      # id, unique id prefix, L2 signature or symbol
    .cases() -> list[CatalogCase]
    .match(errors: list[BuildError]) -> list[CaseHit]       # by L1 → L2 → error symbol; effective status / action;
                                                            # rejected cases never hit; cached by index mtime
    .write_views() -> list[Path]
    .set_human(case_id, **fields) -> CatalogCase            # name / status / action / hints / notes
FileKnowledgeBase.append_error_cases(...)   # also updates the catalog + views (kb.catalog)
FileKnowledgeBase.match(cluster)            # KBMatch.hints: case hints (human first) instead of the raw error-hint
                                            # lines when kb.catalog is on; KBMatch.case_hits: list[CaseHit] (new field)
FileKnowledgeBase.reindex / export / import # include catalog/ and views/
```
CLI `sbmig kb cases list [--status --action --kind --search] [--json]`, `show <key>` (readable: name, match,
effective status / action, evidence, timeline with failed attempts before the fix, the latest successful fix's diff
from `fix_attempts.jsonl` — every attempt's diff when `CaseFix.attempt_ids` holds a resolution of several), `reject <id> [--reason]`, `pin <id> --action recipe:<id>|replace:<old>=<new>[,…]|
handler:<id>|hint|block[:<reason>]`, `unpin <id>`, `hint <id> "<text>"`, `note <id> "<text>"`. `sbmig kb errors show`
prints a readable view by default (`--json` for the raw record) and names the case.

Triage: handler **`case-replace`** (`sbmig/triage/handlers/case_replace.py`, registered last in `default_handlers`, so
the specific built-in handlers win; the HANDLER step precedes KB recipes and the LLM). `matches(cluster, profile, cfg)`:
`cfg.kb.catalog and cfg.kb.case_replace` and `CaseCatalog(cfg.kb_dir, cfg).match(cluster.errors)` returns a hit with
effective action `replace` and status candidate / active for an error with file + line. `apply`: per file, the
reported lines (and `import` lines) — or the whole file with `case_replace_scope: file` — get each pair replaced
(identifier-bounded `(?<![\w$])old(?![\w$])` when `old` is an identifier; otherwise literal but bounded at each end that is a word character, so `1.0.0` never hits `11.0.0` and `javax.servlet` never hits `javax.servletx`; all pairs of a case in one pass, longest first) only where `old` occurs; the scope comes from `kb.case_replace_scope` only (a case action's own `replace_scope` is not consulted);
returns `FileChange(path, op="modify")` per changed file; `notes` name the case id and the pairs, plus one
`resolver: case-replace:<case id>:<8-hex pair-set hash>` note per applied case (`sbmig.kb.catalog.pairset_hash`: sha1
of the sorted unique `old<TAB>new` lines, round 10). The build scores it like any handler; the tracker takes the
attempt's `resolver_id` from those notes, so the error records carry `fix_method: handler`, `resolver_id:
case-replace:<case>:<hash>`, which the catalog counts as action successes / failures of exactly that pair set (a
changed action does not inherit old successes; auto promotion).

Implementation notes (case catalog, as built 2026-10-23):
* `CaseCatalog(kb_dir, cfg=None, *, kb=None)` (`kb` = the store, for recipes / patch cases); extras `resolve`,
  `refresh(l2s)`, `refresh_signatures(sigs)`, `hint_lines(case)`, `status_counts()`, `path_of(id)`; `get` also takes an
  L1. `update_from` recomputes the touched cases from all their records in `errors.jsonl` (its `attempts` argument is
  not needed: the error records carry their attempt history); no index yet → full rebuild. `rebuild` keeps `human:`
  blocks and drops cases with neither events nor human edits. Incremental == rebuild (tested, `updated_at` aside).
* Case YAML: `id`, `signature_generic`, the `human:` block first (its raw text and comments survive updates while it
  parses to the same values; `set_human` / CLI edits re-render it), then the generated part. Variants keyed by
  (owner, L1), ≤ 50. The error class is the 2nd L2 segment when it is a lowercase token, else `other`. The owner
  prefix in the name only when the symbol has no `.` / space. A failed replace (≥ 2 action failures, no success)
  falls through the priority list (patch if any, else hint). Only `resolved` records count as fixes.
* Hints: with `kb.catalog` and an index, `KBMatch.hints` get `[case <id>] …` lines (human hints always, generated ones
  under `kb.error_hints`) instead of the raw `[error …]` lines.
* Store: `FileKnowledgeBase.catalog()`; `stats()` adds `catalog_cases`, `catalog_observed|candidate|active|rejected`;
  export / import carry `catalog/` and `views/` (counter `case_humans_added`); `Ledger.summary()["top_cases"]`; recipe
  promote / reject refreshes the cases they cover. CLI `sbmig kb cases …` as in the contract (+ `show --no-diff`,
  `list -n`); `sbmig kb errors show` readable by default (`--json`, `--all-diffs`).
* Handler `case-replace`: only errors whose L2 is the case key or whose L1 is a variant's get the replace (never a
  symbol-only hit); `clear_catalog_cache()` for tests. e2e `expect.error_cases.kb_cases_min`.

## Round 10 (2026-10-26)

A five-role review (architecture, fix-loop bugs, operations, KB data quality, migration coverage); every finding fixed
in one round. The KB format changed without a migration (the company wipes `kb/` and rebuilds). Statements of the
"Error cases" and "Case catalog" sections above were updated in place; the rest is here.

### Models and seams (`sbmig/core/models.py`, `sbmig/core/interfaces.py`)
* `AttemptOutcome` += **`partial`** (some of the cluster's targeted errors are gone, the rest remain, nothing new in
  the touched files: progress — not a failure for the per-cluster limits or the KB failure counts, not a success for
  harvest / recipe credit) and **`inconclusive`** (the scoring build cannot tell: timed out, failed without a parsed
  error, or stopped at an earlier stage the attempt did not cause — no KB credit either way, counts as tried).
  Outcome sets: triage `SUCCESS_OUTCOMES` = improved, build_ok; `FAILED_OUTCOMES` (what the limits count) = no_change,
  regressed, reverted, apply_failed, inconclusive; `PROGRESS_OUTCOMES` = partial (a deterministic option that only got
  this far is not chosen again); KB `FAILED_OUTCOMES` = no_change, regressed, reverted, apply_failed (partial /
  inconclusive / pending never fail).
* `KB_RECORD_SCHEMA = 2`: `FixAttemptRecord` / `ErrorCaseRecord.schema_version` (absent in a stored line = 1; bump on
  a change of a field's meaning) and `agent_version` (`sbmig.__version__`); audit lines carry both.
  `ErrorCaseRecord` += `asis_java`, `asis_spring_boot`, `asis_spring`, `packaging` (from the profile), `introduced_by`.
* `CaseHit.pinned: bool` — the hit's action is the human block's (`sbmig kb cases pin --action …`); see triage below.
* `LLMRequest.metadata` = `run_id`, `project` (workspace directory name), `phase`, `iteration`, `cluster_id`
  (`LLMFixer.call_tags(ctx, cluster_id, ws)`); never secrets. Invariant 4 unchanged.
* `LLMError` texts the fix loop classifies (explicit tokens, nothing else): `status=<code>` (`status=None` = no HTTP
  answer), `cause=<ExceptionClass>: <innermost message ≤ 120 ASCII chars>` (`sbmig.fix.company.transport_cause`;
  `[WinError N]` / `[Errno N]` kept, non-ASCII → `?`) on the company client's warning / retry / give-up lines and in
  the give-up `LLMError`; `LLMAdapter` wraps a plugin exception as `LLM adapter <name> failed (status=<s>,
  cause=<Class>): …`; an empty answer with `finish_reason=length` raises `LLMError(200, …, "empty answer with
  finish_reason=length: request rejected by its size …")` (not retried; a cut answer that has text is returned with a
  warning).
* Flags: `jdk-not-21`, `llm-config-error`, `infra:llm | infra:maven | infra:jdk | infra:repository | infra:disk`,
  `jdk-base64-shimmed`, `boot4-types-relocated`, `security-path-matchers-rewritten`, `specification-unrestricted`,
  `tools-jar-removed` (handlers: docs/ERROR-HANDLING.md E15–E19).
* Journal events: `kb_credit_skipped` (also per attempt: `{phase, recipes, attempt, reason: partial|inconclusive}`),
  `llm_config_error {phase, iteration, body, prompts}`, `restructure_moves {step, moved, blocked_rekeyed}`,
  `phase_end.interrupted`, `build_negative_cache_retry {goal, module, artifacts, first_log}`.

### Config keys (mirrored in `config/default.yml`)
| key | default | meaning |
|---|---|---|
| `build.require_java_major` | 21 | preflight: `mvn -v` must report this Java major (0 = no check) |
| `build.retry_negative_cache` | true | a build failing on a cached resolution failure runs once more with `-U` |
| `kb.redact_file` | `knowledge/kb/redact.yml` | secret-masking rules added to the built-ins (company additions) |
| `kb.lock_timeout_seconds` | 300 | wait for `kb/.lock`; a lock older than this (or whose pid is gone) is stale |
| `kb.error_diff_chars` | 12000 | cap of a stored attempt diff, applied AFTER the classification (0 or negative = no cap) |
| `loop.structural_regression_kinds` | + `dependency_resolution` | a made-up dependency an LLM added is reverted like a broken pom |
| `fleet.preflight` | true | infrastructure checks before the first project |
| `fleet.infra_failure_limit` / `infra_retry_minutes` / `infra_max_waits` | 3 / 15 / 4 | infrastructure breaker |
| `fleet.cleanup` | `build-output` | `none` \| `build-output` \| `workspace` |
| `fleet.min_free_gb` | 5 | free space on the output drive before each project (0 = no check) |

`RecipeSetConfig` defaults equal `openrewrite.recipes` of `config/default.yml` (drift test
`tests/core/test_recipe_set_defaults.py`). `.env` is read as UTF-8 (BOM or not), UTF-16 or cp949 and read again when
edited (shell variables still win; values the loader set earlier follow the file).

### Fix loop, triage, orchestrator
* **Scoring** (`BuildFixLoop._score`): `build_ok`; `improved` = every targeted error gone AND the build reached their
  stage; `partial` (note "partial: k of n targeted error(s) gone, m remain"); `no_change`; `regressed` = new errors
  in a touched file counted by (file, signature) occurrences (a same-L1 error elsewhere no longer hides them), the
  ×`regression_ratio` rule, a new `structural_regression_kinds` kind after a pom edit, or — after a pom edit — new
  errors of an earlier stage that hide the targeted ones; `inconclusive` otherwise when the build cannot show them.
  A revert rebuild scores the reverted round's inconclusive attempts again (note "scored again on the rebuild after
  the revert"). KB recipes are credited only on improved / build_ok vs failed outcomes (partial / inconclusive →
  `kb_credit_skipped`).
* **Per-cluster limits** (`max_llm_attempts_per_cluster`, `blocked_policy`, the repeated-failure limit) count only
  SCORED failures of the SAME cluster (`sbmig.triage.decide.same_cluster`: same cluster id, or same file — followed
  through restructure moves — with an overlapping signature); `pending` never counts. Attempts on other files with
  the same signature only reach the prompt as KB hints ("this run: the same error (…) in … was fixed by attempt …",
  `BuildFixLoop._sibling_hints`, ≤ 3).
* **Blocked registry** keys = (L1, full posix path) (`''` for module / artifact errors); the orchestrator feeds the
  restructure step's git renames (`git diff -M`) into `BlockedRegistry.note_moves` / `canonical_file` (journal
  `restructure_moves`), so MSA services never share a block and a block survives the move.
* **Pinned case actions** (`DecisionEngine._pinned_decision` in `sbmig/triage/decide.py`, step 0b after the run-wide
  registry): `block` → BLOCKED without
  handler or LLM (a `BlockReason` value as is; free text → `external_api_incompatible` with the text as the
  suggestion); else `handler` → that registered handler first (unknown id → note + warning, already tried → note);
  else `recipe` → a KB_RECIPE decision with the pinned ids not yet run; `replace` / `hint` follow the normal ladder.
* **LLM classification** (`sbmig.loop.fixloop.is_llm_transport_error`): transport / configuration only on
  `status=None|401|403|404|408|429|5xx`, `cause=…`, a known exception class name (`ConnectionError`, `ReadTimeout`,
  `SSLError`, `LLMHardDeadline`, `PluginLoadError`, …) or the explicit config phrases (response path, invalid JSON,
  API key not set, unknown provider); `status=400|413|422`, a content filter and `finish_reason=length|max_tokens` are
  request rejections. The same HTTP 400 body (`llm_400_body`, prompt-too-big texts excluded) for
  `LLM_CONFIG_ERROR_PROMPTS` = 2 different prompts → flag `llm-config-error`, no more LLM calls in the run, one
  follow-up with the short body, journal `llm_config_error`, ERROR line `LLM configuration error: the gateway answered
  HTTP 400 '…' to N different prompts — no more LLM calls in this run`.
* **Invariant 1**: after an internal abort every planned final-build step without a result runs
  (`_final_build_after_abort`); an interrupted phase stays in the report ("phase interrupted: …", journal `phase_end`
  with `interrupted`); `report.success` is False when the run aborted or a planned final build has no result
  (`_verify_missing`). `_abort` logs one ERROR line `run aborted: <reason>`.
* Test-skip fallbacks are recorded per build target (`test_compile_skipped_targets` / `tests_skipped_targets`, `""` =
  the whole project), not run-wide.
* **Java preflight** (`Orchestrator._check_java_major`): `mvn -v` reporting another major than
  `build.require_java_major` aborts before any build (exit 3, flag `jdk-not-21`, follow-up / ERROR line `run aborted:
  Maven runs on Java X (<runtime home>), not Java 21: set build.java_home (or JAVA_HOME) to a JDK 21 home and rerun`);
  `mvn-not-found` caused by JAVA_HOME says so instead of "install Maven" (`maven_available` keeps `runtime: <home>`).
* **Finalize order**: KB records (advisories, error cases, ledger) and the report first, `changes.patch` /
  `result.zip` last (paths announced up front; a path whose file could not be written is dropped and the report
  rewritten).
* **Infrastructure flags** (`Orchestrator._infra_flags`, at finalize): `infra:llm` (llm-unavailable /
  llm-config-error on a run that did not succeed), `infra:maven` (mvn-not-found), `infra:jdk` (jdk-not-21, or
  mvn-not-found because of JAVA_HOME), `infra:repository` (`aborted:parent-not-found`, or every error of the failed
  final build(s) is a resolution error whose text names the repository / network: could not / failed to transfer,
  connection refused / reset, timed out, unknown host, no route to host, PKIX, status / return code 401 / 403 / 5xx,
  "was cached in the local repository"; a plain "Could not find artifact" does not count), `infra:disk` (a failed
  run whose final-build errors say "No space left on device", "not enough space on the disk", "disk quota exceeded",
  "디스크 공간이 부족" or ENOSPC).
* The watchdog stays silent while a loop build, a rewrite run, an LLM HTTP call (`sbmig.fix.client.llm_call_in_flight`)
  or a live Maven child is running.
* `add-known-dependency`: a test library referenced from MAIN code is added with compile scope.

### Build, workspace, signatures
* `Workspace.rel()` / `glob()` / `java_files()` / `pom_files()` return POSIX paths on every OS; new
  `sbmig.core.workspace.posix_rel(p)`, `Workspace.walk()` (os.walk with `.git` and build output pruned; `""` = root),
  `Workspace.is_build_output(rel)`, `is_build_output_path(rel, root=None)`, `Workspace.changed_between(a, b)`
  (NUL-separated name lists, non-ASCII names verbatim; `changed_files_since` too).
* **`target` rule**: a `target` dir is build output only when no `src` component comes before it and a `pom.xml` sits
  beside it (or it holds `maven-status/` / `maven-archiver/`). Packages named `target` stay in the run copy, KB scans,
  restructure, analyze, handlers and `result.zip`.
* **Generated `.gitignore`** (part of `changes.patch` / `result.zip`): `target/`, `!**/src/**/target/`,
  `!**/src/**/target/**`, `!/<dir>/target/` (+ `/**`) for every non-output `target` dir, `.sbmig/`, `*.class`, and the
  `GENERATED_IGNORES` (`.flattened-pom.xml`, `**/src/main/generated/`, `**/src/test/generated/`, `.apt_generated/`,
  `.apt_generated_tests/`) unless the as-is tree has them.
* Jars a project `.gitignore` hides (`*.jar`, `lib/`) are force-added to every snapshot (`_force_add_ignored_jars`;
  outside build output and `node_modules/`); nothing else the project ignores is forced in.
* Workspace git always passes `-c commit.gpgsign=false -c tag.gpgsign=false -c core.longpaths=true
  -c core.autocrlf=false -c core.quotepath=false -c safe.directory=* -c core.hooksPath=<run dir>/no-hooks`
  (`GIT_CONFIG`); `git_init` writes `.git/info/attributes` = `* -text -eol -filter -ident -working-tree-encoding`
  (a project `.gitattributes eol=crlf` / LFS filter no longer rewrites files on revert). mvn / git children start
  with `CREATE_NO_WINDOW` on Windows.
* Windows javac paths `/C:/…`, `file:///C:/…`, `file:/C:/…`, percent-encoded (Korean folders) and lower-case drives
  are relativized (roots compared case-insensitively); raw text and build summary scrubbed in every spelling.
* `MavenRunner.run`: with `build.retry_negative_cache` a failed build whose log says a resolution failure "was
  cached in the local repository" (Maven 3.6–3.9 wordings, `sbmig.build.runner.negative_cache_hits(text)` →
  coordinates) runs once more with `-U` — once per artifact per run, never offline (`-o`) or when `-U` is already
  there; log `maven: negative cache hit for <g:a:v> → retrying once with -U`, journal `build_negative_cache_retry`,
  log file `NNN-build-<label>-U.log`; the retry's result is returned.
* `mvn_env` appends `-Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8` to `MAVEN_OPTS` (`with_utf8_output`); a property
  already set in `build.env` / `MAVEN_OPTS` wins. The encoding-detection cache is a thread-safe LRU of 20,000 entries.
* **Signatures (L2 = case key)**: `package_does_not_exist` keeps the whole package (`javax.xml.bind` ≠ `javax.xml.ws`);
  `method_does_not_override` / `static_import_only` carry the method / class the log parser reads from the reported
  source line into `BuildError.symbol`; `illegal_start` the javac phrase (first 8 words); `plugin_execution` = plugin
  goal + the normalized cause (`CAUSE_WORDS` = 10: `plugin_execution|war:war|error assembling war: webxml attribute
  is required`); L1 masks project FQCNs inside the symbol. Old L2s are not comparable — the KB must be rebuilt.
* Thread dumps (`sbmig.core.diag`): Ctrl+Break (`SIGBREAK`) on Windows, and a trigger file `dump-threads` /
  `dump-threads.txt` in the start dir, the out dir or a recent run dir (polled every 2 s; the dump is written next to
  it, the file removed, log `thread dump requested by <path>`); under `sbmig serve` use the file or `/api/threads`.

### Knowledge base
* **Secret masking** (`sbmig/kb/redact.py`: `redact(text)`, `Redactor.spans` / `redact_ex`, `get_redactor(cfg)`):
  built-in contexts + `kb.redact_file` (`key_words`, `key_patterns`, `patterns`, `allow_keys`; commented examples
  only). Only VALUES are masked (`***`), by context: key = value / key: value whose key's last word is a secret word,
  Java / XML / JS string literals assigned to such names, `<password>`, `<property name value>`, `<param-name>` /
  `<param-value>`, JSON, URL user info, JDBC (Oracle `user/pass@`, `;password=`), Authorization / Bearer / Basic, PEM
  keys; never comparisons, non-literal assignments or placeholders (`${…}`, `__SET_ME__`, …). Applied to raw Maven
  text, messages, diffs, KB patch cases and ledger notes; KB patch cases with a masked secret get tag
  `no-replay:redacted` (hint only).
* **Append-only jsonl** (`sbmig/kb/jsonl.py`: `append_lines`, `repair_tail`, `parse_record`, `quarantine`) for
  `errors.jsonl`, `fix_attempts.jsonl`, `ledger.jsonl`, `audit.jsonl`, `quarantine.jsonl`: a torn last line is moved to
  `<file>.corrupt` (warning) before the next append; one write + fsync per batch; only whole records count as known.
  Lines a model cannot validate → warning + `kb/quarantine.jsonl`; `rebuild` never deletes a case whose L2 had such
  lines.
* **Cross-process lock** (`sbmig/kb/lock.py`: `kb_lock(root, timeout=None)`): `kb/.lock` created with `O_CREAT |
  O_EXCL` holding pid / host / time / token / cmd; re-entrant per process; stale after `kb.lock_timeout_seconds` or
  when the pid is gone (same host); waits with backoff, then logs and proceeds without it (never fails a run). Every KB
  write runs inside it; `index.json` is reloaded when it changed on disk (web, fleet and `sbmig kb …` may share one KB).
* **Catalog** (`sbmig/kb/catalog.py`, rules in "Case catalog" above): `pairset_hash(pairs)`,
  `case_replace_resolver(case_id, pairs)`; `kb/audit.jsonl` = every human edit through `sbmig kb cases …` (OS user,
  command, before → after) and every automatic status / action transition (created / promoted / demoted / action
  changed / `fallen back: …`) with the evidence counts; `cases show` has a history section; `cases reject` also stops
  that L2's KB patch replay, KB recipes and symbol hints (`CaseCatalog.rejected_signatures`).
* **Learned replace pairs** (`sbmig/kb/changes.py`): a package-qualified name is one token and a change in its package
  part becomes one pair of the full package parts (`javax.xml.bind.JAXBContext → jakarta.xml.bind.JAXBContext` is
  learned as `javax.xml.bind → jakarta.xml.bind`, never `javax → jakarta`); member renames stay single-token pairs.
  Handler `case-replace` only renames javax names the jakarta table renames (`javax.sql`, `javax.xml.parsers`,
  `javax.naming` untouched).
* **Error CSV** (`ERROR_CSV_COLUMNS`, snapshot in `tests/kb/test_error_cases.py`): the round-9 columns, then `signature`
  (L1), `location`, `module`, `case_type`, `asis_java`, `asis_spring_boot`, `asis_spring`, `packaging`, `targeted`,
  `resolved_by`, `fix_labels`, `at`, `introduced_by`, `notes`, `schema_version`, `raw`, and `diff_excerpt` last. Formula
  guard (`formula_guarded`): a leading `'` only for cells starting with `=` / `@` / tab / CR, or with `+` / `-` and
  containing one of `( ) | ! = @` (`-source 8`, `-Xmx512m` stay as written; read back by dropping one `'`).
* `ErrorCaseStore.summary()` / `FileKnowledgeBase.stats()` stream (30k records: +342 MB → +11 MB); `kb import
  --replace` builds in a hidden sibling dir, swaps by rename and keeps `<kb>.bak-<ts>`; recipe moves write the new meta
  first. New KB files: `audit.jsonl`, `quarantine.jsonl`, `*.jsonl.corrupt`, `.lock`, `kb.bak-<ts>/`; the repo
  `.gitignore` ignores the KB data files (`.gitkeep`s stay).
* `--no-kb` (CLI and `migrate(kb=False)`) = `sbmig.api.NO_KB_OVERRIDES`: also `catalog`, `case_replace`,
  `error_hints`, `try_candidates_in_plan`, `ledger`, `symbol_index`, `deterministic_candidates` off (`error_cases`
  stays on: the run dir still gets `error_cases.*`).

### Fleet, CLI, web
* `sbmig/fleet/preflight.py` (`run_preflight(cfg, out_dir, echo=…)`, `Check(name, ok, detail, kind, transient,
  skipped)`, one line each `preflight: <check> ok|FAIL|skip <detail>`): disk, maven, java, settings, repository (one GET
  to the settings mirror like Maven: settings `<proxy>` honoured, environment proxies not; any answer < 500 = up, an
  unverifiable TLS handshake = up), llm (`sbmig.fix.company.probe_llm`; skipped for the fake provider or
  `loop.max_llm_calls: 0`). Kinds `disk | maven | jdk | repository | llm`; `transient` (outage, full disk) vs a
  configuration error.
* **Infrastructure breaker** (`FleetRunner`): a project ending with `infra:<kind>` (or the older llm-unavailable /
  llm-config-error / mvn-not-found / jdk-not-21 flags on a run that is not green, or crashing with an infrastructure
  exception: full disk, requests / LLM error, mvn missing) goes back to `pending` with `error: infra:<kind>`; after
  `fleet.infra_failure_limit` such projects in a row the fleet waits `fleet.infra_retry_minutes`, re-runs the
  preflight and resumes (deferred projects first, each re-run at most `MAX_INFRA_REQUEUES` = 2 times); after
  `fleet.infra_max_waits` waits, at once with `infra_retry_minutes: 0`, or at once on a configuration error it stops:
  `fleet stopped: infrastructure <kind> unavailable since <project> - fix it and run sbmig fleet again` (state saved,
  exit 3 = `EXIT_STOPPED_ON_INFRA`; a plain re-run resumes). Waiting line `fleet: infrastructure <kind> unavailable
  since <project> - waiting N min, then re-checking (i/max)`. Before each project the free space is checked
  (`fleet.min_free_gb`, kind `disk`).
* `sbmig/fleet/cleanup.py`: `cleanup_run(run_dir, mode)` after the report / zip (`build-output` = `target/` dirs beside
  a pom; `workspace` also `<run dir>/project` when `result.zip` exists, else falls back to `build-output`; keeps
  `KEEP_FILES` + `logs/`); `plan_clean(...)` / `execute(items)` for `sbmig clean [--out DIR] [--fleet DIR]
  [--older-than DAYS] [--keep-failed] [--dry-run] [--records DIR]` (finished runs' project copies, web uploads of
  finished runs, stale `.part` uploads, `llm-*.json` records; prints per-kind count and size, then `would free …` /
  `freed …`).
* Fleet logging: every project logs DEBUG into its own `<run dir>/sbmig.log`; `fleet/sbmig.log` is INFO.
* Manifests (`sbmig.fleet.manifest.read_text_any`): UTF-8 (BOM or not), UTF-16 (BOM or BOM-less LE), else cp949.
  Names with non-ASCII characters or colliding names → ASCII slug + 6-char hash of the input path
  (`name_with_hash`); plain ASCII names unchanged. Fleet state is keyed by `input_key(path)` (absolute, normalized,
  case-folded on Windows; older state files match unchanged).
* Summary: `summary.csv` UTF-8 with BOM, + columns `attention` (`;`-separated) and `input`; `summary.md` section
  "Needs attention" grouped by the first code; each summary file is written on its own (an Excel-locked csv is
  skipped with a log line).
* `sbmig/report/result_line.py`: `result_line(...)`, `report_result_line(report, exit_code)` →
  `SBMIG-RESULT run=<id> exit=<n> build=<ok|FAIL> unresolved=<n> blocked=<n> llm=<calls> attn=<codes|->` (ASCII)
  at the end of `sbmig run` (also on a crash: `attn=crash`), one per fleet project, in a web run's `sbmig.log`.
  `attention_codes(...)`, most important first: `infra:<kind>`, `skipped:<tool|input>`, `crash`, `aborted[:reason]`,
  `blocked:<reason>` (per distinct reason), `unresolved:<n>`, `restructure-failed`, `llm-budget-exhausted`,
  `time-budget-exhausted`, `javax-api-on-classpath`, `tests-skipped`, `test-compile-skipped`, `tests-not-compiled`,
  `incomplete`. `sbmig fleet status --attention` (`summary.render_attention`) lists only those projects, grouped by
  their first code.
* Console: stdout / stderr / logging handlers use `errors="replace"` (a cp949 console never kills a run after the
  report); the console summary prints `ok` / `FAIL`. `sbmig llm-probe` ends with `ProbeResult.line()` = `LLM-PROBE
  ok|FAIL <kind> provider=… model=… status=…` (exit 0 / 1); `probe_llm(cfg)` is shared with the fleet preflight.
* **Web token** (`sbmig.web.app.TokenGuard`, `create_app(..., token=None)`): without a token only loopback clients are
  served (a LAN client gets 403, also under a plain `uvicorn --host 0.0.0.0`); `sbmig serve --token <t>` or
  `SBMIG_WEB_TOKEN` (also from `.env`) is required for a non-loopback `--host`, else exit 2. Requests send
  `Authorization: Bearer <token>`, or `?token=` once (sets an HttpOnly SameSite=Strict cookie holding a hash of the
  token for 7 days; the page `/` is redirected without the token, an API call is answered and keeps the cookie);
  401 / 403 bodies carry the hint the UI shows.

## v2 — flow-v2 (2026-10-26, branch `flow-v2`, plan `docs/v2/PLAN-v2.md`)

The business team's flow: every OpenRewrite recipe in ONE run without a build, the company restructure as before, an
LLM structure review, then `mvn clean package`; every build error is fixed by an **LLM-written Python fix script**
stored per case (L2) and reused / augmented on later projects. Stable v1 flow = tag `company-v1`. This section is the
contract of PLAN-v2 step 0; the implementations follow it (step 1: S `sbmig/script/`, K `sbmig/kb/`, F `sbmig/fix/`,
R `sbmig/review/` + `sbmig/report/`, L `sbmig/loop/` + `sbmig/plan/` + `sbmig/triage/` + `sbmig/graph/`).

### Models (`sbmig/core/models.py`)
* `Phase.TRANSFORM = "transform"` (the one rewrite run; ordered between `plan` and `jdk21`), `StepKind.REVIEW`,
  `DecisionKind.SCRIPT`, `CaseActionKind` += `script` (`CaseAction.script_version` = the case's best version).
* `ErrorCluster.case_id` — v2 clusters are ONE case (L2) across files (`file` None, id `case:<case_id>`).
* `ScriptRunResult` (`status` ok | error | timeout | rejected, `changed_files`, `log`, `error`, `problems`,
  `duration_s`), `ScriptMeta` (`<kb>/scripts/<case_id>/v<N>.meta.json`: version, parent, sha1, run / project / model,
  analysis, successes / failures / partials, success_projects, last_outcome, status active | retired | quarantined),
  `ScriptBase` (the version an augment extends + what it did in this project + `history` lines), `ScriptProposal`
  (code, analysis, usage, `error` when there is no script), `ReviewIssue` / `StructureReview` (verdict ok | issues |
  skipped | failed, issues, script + result + commit, usage, note); `RunReport.structure_review`.
* `FixAttempt` += `script_mode` (new | reuse | augment), `script_case_id`, `script_version`, `script_parent`,
  `script_code`, `script_result`.
* `FixMethod` += `script` (new LLM script), `script_reuse` (stored version, no LLM call), `script_augment`; the
  resolver id of a script attempt is `script:<case_id>:v<N>`. `FixAttemptRecord` += `script_mode`, `script_case_id`,
  `script_version`, `script_parent`, `script_status`, `script_error`, `script_log` (≤ 50 lines);
  `ErrorCaseRecord` += `script_case_id`, `script_version` (of the resolving attempt); `ErrorAttemptRef.script_version`.
  `KB_RECORD_SCHEMA = 3`.

### Seams (`sbmig/core/interfaces.py`)
* `ScriptRunnerLike.check(code) -> list[str]`, `.run(ws, code, *, errors=(), timeout_s=None) -> ScriptRunResult`
  (changes stay uncommitted; never raises).
* `ScriptStoreLike`: `versions`, `get`, `best(case_id, limit=1)` (active versions with ≥ 1 success: most success
  projects, then successes − failures, then newest; a human-pinned `{kind: script, script_version: N}` in the case's
  `human` block comes first), `latest`, `add` (next version; same sha1 → existing meta), `credit(case_id, version,
  outcome, project=, run_id=)`, `case_ids`. `ScriptKnowledgeLike` = `FileKnowledgeBase.scripts` + `record_review`.
* `ScriptFixerLike.propose_script(ws, cluster, ctx) -> ScriptProposal` — `FixContext` += `script_mode` (new |
  augment), `case_id`, `signature_generic`, `script_base`. An augment answer is the COMPLETE new script (it replaces
  the base and runs on a tree where the base was rolled back). `FixerLike.propose` stays for tier-2 plugins; the v2
  loop needs `propose_script` (a tier-2 plugin without it cannot fix in v2 — documented, no fallback).
* `StructureReviewerLike.review(ws, profile, cfg, *, run_id, restructure_notes, restructure_results)
  -> StructureReview`. `LLMRole` += `structure_review`.

### Config keys (mirrored in `config/default.yml`; `tests/core/test_v2_contracts.py`)
| key | default | meaning |
|---|---|---|
| `openrewrite.recipes.*` | jdk21 `UpgradeToJava21`; jakarta `JavaxMigrationToJakarta`; spring_boot_4 `UpgradeSpringBoot_4_0`, `SpringBootProperties_4_1`; framework 7 / security 7 / junit5 empty | v2 recipe scope (business decision) |
| `plan.intermediate_builds` | false | |
| `plan.optional_bump_phase` | restructure | the bump runs inside the restructure |
| `loop.phases.{jdk21,spring,restructure}.enabled` | false | no intermediate loops |
| `loop.phases.verify` | package, 12 rounds | the script fix loop |
| `loop.max_llm_calls` / `max_total_minutes` | 100 / 240 | |
| `build.skip_test_compile` | true | `-Dmaven.test.skip=true` on every build |
| `kb.dir` | `kb-v2` | v1 KB (`kb/`) stays apart |
| `kb.{ledger,symbol_index,deterministic_candidates,use_active_recipes,use_patch_replay,use_hints,harvest,generate_recipe_candidates,try_candidates_in_plan,case_replace,error_hints}` | false | v1 learning paths (code removed in step 4) |
| `advisories.allow_recipes` / `advisories.ledger` | false / false | advisories = LLM hints + Handover only |
| `script.timeout_seconds` | 60 | per run, then the process tree is killed |
| `script.python` | null | interpreter of the script process (null = sys.executable) |
| `script.extra_allowed_imports` | [] | added to the import whitelist |
| `script.max_code_chars` / `script.log_lines` | 30000 / 200 | |
| `script.max_files_changed_warn` | 40 | a run changing more files gets a warning note |
| `script.reuse` / `script.reuse_versions` | true / 1 | stored versions tried before the LLM is asked |
| `script.max_augments_per_case` | 3 | LLM extensions per case and run, then BLOCKED (repeated_failure) |
| `review.enabled` / `fix_critical` / `record` | true / true / true | structure review, its script, `<kb>/reviews.jsonl` |
| `review.max_pom_chars` / `max_poms` | 60000 / 40 | prompt size |

### Plan (L)
Non-MSA: `transform.rewrite` (OPENREWRITE, TRANSFORM; jdk21 set + jakarta set when the case needs it — spring_legacy,
servlet_tomcat, pure_java with Java EE javax imports — + spring_boot_4 set for spring_boot; one run) →
`restructure.apply` | `restructure.boot_pom` | `restructure.pom_only` (RESTRUCTURE) → `restructure.review` (REVIEW,
RESTRUCTURE) → `verify.build`. MSA: `transform.rewrite@<target>` per library root / service (one un-suffixed run under
`top-aggregator`), `restructure.msa_pom`, `restructure.review`, `verify.build[@<svc>]`. No BUILD_FIX_LOOP steps before
verify, no pre-emptive handlers, no project (`com.company.migrate.*`) / library / KB / advisory recipes;
`plan.parent_phase` is ignored.

### Script fix loop (L; `loop.phases.verify` after a failed final build)
* Clusters by L2 (`signature_generic`) across files; errors without an L2 get `case_id =
  sha1(kind|normalized file|L1)[:10]`. Triage priorities (pom / parent / dependency resolution first, masking) as v1.
  Decisions: SCRIPT, or BLOCKED / SKIP / ESCALATE as v1 (`loop.blocked_policy`, budget, LLM unavailable, already
  blocked). One attempt per case and round; the rebuild scores every pending attempt (`_score`, round 10 rules).
* Ladder per case and run: (1) **reuse** — `script.reuse` and `kb.scripts.best(case_id, limit=script.reuse_versions)`
  versions not yet tried in this run (no LLM call); (2) **augment** — base = this run's last script attempt of the
  case (code, result, remaining / introduced errors) or else `kb.scripts.latest(case_id)`; ask `propose_script(mode
  augment)`, `add(parent=base.version)`, run; at most `script.max_augments_per_case`; (3) **new** — nothing stored and
  nothing tried: `propose_script(mode new)`, `add(parent=None)`, run; (4) limits reached → BLOCKED repeated_failure.
* A script attempt: `base_commit = ws.head()`; `ScriptRunner.run(ws, code, errors=cluster.errors)`; status rejected /
  error / timeout → `apply_failed` with the working tree restored (no partial application); ok without a change →
  `apply_failed` "no file changed"; ok with changes → commit `fix(script:<case_id>:v<N> <mode>): <cluster id>`.
* Before a case's next attempt, its previous attempt is rolled back unless it was improved / build_ok (regressed was
  already reverted by scoring) — every version is measured from the same base, so a stored version is complete.
* Every LLM script is stored before it runs (failures keep their lineage); after scoring `kb.scripts.credit(...)` for
  every script attempt. Records: `FixAttempt.script_*`; tracker fix method by mode, resolver `script:<id>:v<N>`.
* Out of the flow (code removed in step 4): reactive and pre-emptive handlers (incl. `case-replace`), KB recipe /
  candidate batches, KB patch replay, `recipe_rules.yml`, harvest / ledger / symbols / recipe credit, SEARCH/REPLACE.

### Script contract (S)
Answer: `<analysis>…</analysis>` + `<script>…</script>` (fences inside allowed) defining `def fix(p)`;
`<need_file>` / `<need_grep>` as v1. Helper `p` (relative posix paths; absolute, `..`, drive letters, `.git/`,
`target/` and anything resolving outside the project are refused → status error):

| API | |
|---|---|
| `p.errors` | list of dicts: file, line, column, message, symbol, location, kind, module, signature, signature_generic |
| `p.files(glob="**/*")` | sorted relative posix paths (no `.git/`, `target/`, `node_modules/`) |
| `p.read(path)` / `p.exists(path)` | text decoded with the file's detected encoding (`build.encoding_candidates`) |
| `p.write(path, text)` | same encoding and newline style as the original (a new file: UTF-8, `\n`) |
| `p.replace(path, old, new, count=-1)` / `p.sub(path, pattern, repl, count=0, flags=0)` | number of replacements |
| `p.create(path, text)` / `p.delete(path)` / `p.move(src, dst)` | create refuses an existing file |
| `p.pom(path="pom.xml")` | editor (sbmig.restructure.pom_editor): `add_dependency(g, a, version=None, scope=None, type=None, classifier=None)`, `remove_dependency`, `set_dependency_version`, `add_exclusion(g, a, ex_g, ex_a)`, `add_managed_dependency`, `set_property`, `remove_property`, `set_plugin_version`, `text()`, `save()` — mutators return True when the pom changed |
| `p.log(msg)` | a log line (KB); `print` goes to the log too |

Static check (`ast`): module-level `def fix(p)` required; imports only `re json textwrap fnmatch posixpath itertools
collections functools string math` (+ `script.extra_allowed_imports`); refused names `open exec eval compile
__import__ globals locals vars getattr setattr delattr input breakpoint help memoryview`; refused modules `os sys
subprocess socket pathlib importlib shutil ctypes threading multiprocessing builtins io`; any `__dunder__` attribute;
code longer than `script.max_code_chars`. Run: `python -I` harness in a temp cwd, restricted builtins and a guarded
`__import__`, timeout → process tree killed (Windows `taskkill /T /F`), result JSON → `ScriptRunResult`. A guard
against mistakes, not a sandbox against a hostile author (documented).

### Script KB (K)
`<kb>/scripts/<case_id>/v<N>.py` (UTF-8) + `v<N>.meta.json`; writes under the KB lock; analysis / logs redacted; code
matching a secret pattern → stored masked, `quarantined`, never selected. Catalog: action `script` (best version)
when one is proven, `evidence.fixes` by resolver `script:<id>:v<N>`; views list versions / best. CLI `sbmig kb
scripts list | show <case_id> [--version N] | retire <case_id> <N> | activate <case_id> <N>`. `record_review` → one
line in `<kb>/reviews.jsonl`.

### Structure review (R) — `StructureReviewer(cfg, llm, runner, kb)`
Input: every pom after the restructure (≤ `review.max_poms`, ≤ `review.max_pom_chars`, head + tail cut), module dirs
and packaging, restructure notes, the company rules (`artifacts.parent` / `boot_parent` / `comp_parent`, mandatory
target, optional artifacts, `layout.*`), case type, detected source encodings. Prompt `sbmig/review/prompts/
review_v1.md`, role `structure_review`; answer JSON `{verdict, issues[{severity, file, problem, fix_hint}],
analysis}` + `<script>` for critical issues. Non-UTF-8 sources without a matching `project.build.sourceEncoding` are
critical (JDK 21 garbles them with a warning only — no build error would ever show it). Critical + script +
`review.fix_critical` → `runner.run(ws, code)` (uncommitted; the orchestrator commits `review: …` and fills
`commit`); script failure → working tree restored, note. Recorded via `kb.record_review`; report section "Structure
review" (ko/en) and warnings as Handover follow-ups.
