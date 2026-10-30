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
R `sbmig/review/` + `sbmig/report/`, L `sbmig/loop/` + `sbmig/plan/` + `sbmig/triage/` + `sbmig/graph/`). Statements
that step 1 proved wrong are corrected in place; what it added is under "As built (step 1)" at the end.

### Models (`sbmig/core/models.py`)
* `Phase.TRANSFORM = "transform"` (the one rewrite run; ordered between `plan` and `jdk21`), `StepKind.REVIEW`,
  `DecisionKind.SCRIPT`, `CaseActionKind` += `script` (`CaseAction.script_version` = the case's best version).
* `ErrorCluster.case_id` — v2 clusters are ONE case (L2) across files (`file` None, id `case:<case_id>`).
* `ScriptRunResult` (`status` ok | error | timeout | rejected, `changed_files`, `log`, `error`, `problems`,
  `duration_s`), `ScriptMeta` (`<kb>/scripts/<case_id>/v<N>.meta.json`: case_id, signature_generic, version, parent,
  sha1, created_at, run / project / model, source llm | human | review, analysis, successes / failures / partials,
  success_projects, last_outcome / last_at / last_run_id, status active | retired | quarantined, notes),
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
  `human` block comes first while that version is active — retire wins over a pin; a rejected case gives []),
  `latest`, `add` (next version; same sha1 → existing meta), `credit(case_id, version, outcome, project=, run_id=)`,
  `case_ids`. `ScriptKnowledgeLike` = `FileKnowledgeBase.scripts` + `record_review`.
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
servlet_tomcat, pure_java with Java EE javax imports; never for spring_boot, the Boot umbrella migrates jakarta — +
spring_boot_4 set for spring_boot; one run; no transform step without java sources) →
`restructure.apply` | `restructure.boot_pom` | `restructure.pom_only` (RESTRUCTURE) → `restructure.review` (REVIEW,
RESTRUCTURE) → `verify.build`. MSA: `transform.rewrite@<target>` per library root (never the Boot set) / service
(one un-suffixed run under `top-aggregator`), `restructure.msa_pom`, `restructure.review`, `verify.build[@<svc>]`.
No BUILD_FIX_LOOP steps before verify, no pre-emptive handlers, no project (`com.company.migrate.*`) / library / KB /
advisory recipes; `plan.parent_phase` is ignored.

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
`<need_files>` (`<need_file>` accepted) / `<need_grep>` as v1. Helper `p` (relative posix paths; absolute, `..`,
drive letters, `.git/`, writes into Maven build output (`target/` beside a pom) and anything resolving outside the
project are refused → status error):

| API | |
|---|---|
| `p.errors` | list of dicts: file, line, column, message, symbol, location, kind, module, artifact, signature, signature_generic |
| `p.files(glob="**/*")` | sorted relative posix paths (no `.git/`, `node_modules/`, Maven build output) |
| `p.read(path)` / `p.exists(path)` | text decoded with the file's detected encoding (`build.encoding_candidates`) |
| `p.write(path, text)` | same encoding and newline style as the original (a new file: UTF-8, `\n`) |
| `p.replace(path, old, new, count=-1)` / `p.sub(path, pattern, repl, count=0, flags=0)` | number of replacements |
| `p.create(path, text)` / `p.delete(path)` / `p.move(src, dst)` | create refuses an existing file |
| `p.pom(path="pom.xml")` | editor (sbmig.restructure.pom_editor): `add_dependency(g, a, version=None, scope=None, type=None, classifier=None)`, `remove_dependency`, `set_dependency_version`, `add_exclusion(g, a, ex_g, ex_a)`, `add_managed_dependency`, `remove_managed_dependency`, `has_dependency`, `dependencies(managed=False)`, `get_property`, `set_property`, `remove_property`, `set_plugin_version`, `text()`, `save()` — mutators return True when the pom changed |
| `p.log(msg)` | a log line (KB); `print` goes to the log too |

Static check (`ast`): module-level `def fix(p)` required; imports only `re json textwrap fnmatch posixpath itertools
collections functools string math` (+ `script.extra_allowed_imports`); refused names `open exec eval compile
__import__ globals locals vars getattr setattr delattr input breakpoint help memoryview`; refused modules `os sys
subprocess socket pathlib importlib shutil ctypes threading multiprocessing builtins io` (never re-allowed by
`script.extra_allowed_imports`); any attribute starting with `_`, and the refused module names / `codecs` / `open` as
attributes (`posixpath.os`); code longer than `script.max_code_chars`. Run: `python -I -X utf8` harness in a temp
cwd, restricted builtins and a guarded `__import__`, timeout → process tree killed (Windows `taskkill /T /F`), result
JSON → `ScriptRunResult`. A guard against mistakes, not a sandbox against a hostile author (documented).

### Script KB (K)
`<kb>/scripts/<case_id>/v<N>.py` (UTF-8) + `v<N>.meta.json`; writes under the KB lock; analysis / logs redacted; code
matching a secret pattern → stored masked, `quarantined`, never selected. Catalog: action `script` (best version)
when one is proven, `evidence.fixes` by resolver `script:<id>:v<N>`; views list versions / best. CLI `sbmig kb
scripts list | show <case_id> [--version N] | retire <case_id> <N> | activate <case_id> <N> | validate`.
`record_review` → one line in `<kb>/reviews.jsonl`.

### Structure review (R) — `StructureReviewer(cfg, llm, runner, kb)`
Input: every pom after the restructure (≤ `review.max_poms`, ≤ `review.max_pom_chars`, head + tail cut), module dirs
and packaging, restructure notes, the company rules (`artifacts.parent` / `boot_parent` / `comp_parent`, mandatory
target, optional artifacts, `layout.*`), case type, detected source encodings. Prompt `sbmig/review/prompts/
review_v1.md`, role `structure_review`; answer JSON `{verdict, issues[{severity, file, problem, fix_hint}],
analysis}` (inside `<review>…</review>`) + `<script>` for critical issues. Non-UTF-8 sources without a matching
`project.build.sourceEncoding` are critical (JDK 21 garbles them with a warning only — no build error would ever show
it); sbmig adds that issue itself (`(sbmig check) …`) when the LLM misses it. Critical + script +
`review.fix_critical` → `runner.run(ws, code)` (uncommitted; the orchestrator commits `review: …` and fills
`commit`); script failure → working tree restored, note. Recorded by the orchestrator after its commit
(`kb.record_review`, so the line carries `commit`; skipped reviews are not recorded); report section "Structure
review" (ko/en) and warnings as Handover follow-ups.

### As built (step 1, 2026-10-26)

What the step-1 implementations (commits `cb9a3b1`, `63773af`) add to the contract above; wrong statements above were
corrected in place. The v1 code paths stay in the tree until PLAN-v2 step 4 (v1 = tag `company-v1`).

**Plan and flow**
* Plan notes (English): `transform: one OpenRewrite run (…) — no build until the restructure is done (v2)`,
  `pre-emptive handlers: off in v2 (errors are fixed by LLM scripts)`, `kb: KB recipes are not used in v2`; a v1
  setting set again is named as ignored (`plan.parent_phase=spring ignored…`, `v2: no intermediate build-fix loops
  (…ignored)`). `--stop-after` / web stop: intake | analyze | plan | transform | restructure | verify.
  `config/artifacts.example.yml` no longer sets `plan.parent_phase` / `loop.phases.verify`.
* The failed final build is the verify loop's build 1 (`loop.run(..., first_build=…)`, one Maven build saved): INFO
  `verify: final build failed with N error(s) in M case(s) → script fix loop (mvn package, up to 12 round(s))`, then
  `[verify] build 1 = the failed final build (N error(s), <log>) — not built again`; after the loop the final build runs
  again (invariant 1).

**Script fix loop** (`sbmig/loop/fixloop.py`, ladder `sbmig/triage/scripts.py` `plan_script_step`, clusters
`sbmig.triage.cluster.cluster_cases`)
* Cluster id `case:<case_id>`; a targeted MSA loop appends `@<target>` (`case:<id>@<svc>`: two services never count
  against each other), `ErrorCluster.case_id` stays the plain id (store key). Order: triage priority, then error count.
* Ladder details: **reuse** = `best(limit=script.reuse_versions)` versions not run in this run yet — no LLM call, so it
  also works with the LLM down or the budget used up, and it comes before `loop.blocked_policy`; **augment** base =
  this run's last attempt of the case that had code, else `latest(case_id)` (a stored but unproven version is
  augmented, not re-run); **new** only when neither exists. LLM requests per case and run ≤
  `script.max_augments_per_case` augments + the first `new`; a `new` answer without a script does not open an augment
  (the next request is `new` again, inside that total). Escalation model on a stall (`loop.stall_limit`) or from a
  case's second augment on. `loop.max_llm_attempts_per_cluster` / `loop.verify_extra_llm_attempts` are v1-only.
* `loop.blocked_policy` v2 (LLM script attempts before BLOCKED): `external_javax_api`, `external_api_incompatible`,
  `artifact_not_found`, `plugin_incompatible` 2; `parent_not_found`, `unsupported_source`, `encoding` 0.
* **Rollback before the next try** (`BuildFixLoop._rollback_previous`): the case's previous applied script attempt is
  rolled back when its outcome is partial / no_change / inconclusive (`NO_ROLLBACK` = improved, build_ok, reverted,
  pending, apply_failed, blocked): HEAD = its commit → **reset** to its `base_commit`, else **`git revert --no-edit`**
  (a failing revert is aborted and noted; the next version then runs on top of it). The errors it had removed come
  back into the round and into the next scoring baseline (never counted as regressions); journal `script_rollback
  {attempt, case, version, outcome, commit, mode: reset|git revert, restored_errors}`, note `rolled back before …`
  (the tracker never puts such an attempt into a resolution chain). When the next attempt changes nothing the rollback
  is **undone** (tree back to the HEAD the last build saw, journal `script_rollback_undone`). A partial attempt of a
  case that ends BLOCKED stays in the tree.
* Attempt extras: an augment identical to its base, or an LLM answer identical to a version already run (same sha1),
  is `apply_failed` without running; writes to a merge-conflict reference copy are undone before the commit; more
  than `script.max_files_changed_warn` changed files → note + WARNING line; journal `script_stored {case, version,
  parent, mode, attempt}`, `script_credit`. Attempt ids stay `<phase>-<round>-<n>` (`verify-1-1`).
* `LLMFixer.propose_script` (`sbmig/fix/fixer.py`, parser `sbmig/fix/script_protocol.py`, prompts
  `sbmig/fix/prompts/script_system_v1.md`, `script_user_v1.md.j2`): `<need_files>` / `<need_grep>` rounds as v1, then
  **one feedback round** when the script cannot be used (no `<script>`, no `def fix(p)`, static-check findings,
  identical to the base, a v1 `<patches>` answer). The parser is tolerant (fences, CRLF, a block indented as a whole,
  a missing `</script>`, a bare ```` ```python ```` block defining `fix(p)`) and never raises.
* Flag **`fixer-without-scripts`**: a tier-2 `fix.plugin` without `propose_script` cannot write scripts in the run
  (stored versions are still reused); one follow-up, journal `fixer_without_scripts`; not an `infra:llm` flag.
* INFO lines per case: `[verify it1] case <id> (<kind>, N error(s) in M file(s)): <decision> — asking the LLM for a
  fix script …`, `…: LLM answered (new|augment) — a script — <first sentence of the analysis>`, `…: script v<N> <mode>
  — ok, K file(s) changed` (or the run status and error).

**Script runner and helper API** (`sbmig/script/`)
* `<script.python> -I -X utf8 harness.py <payload.json>` in a fresh temp dir outside the project, own process group,
  killed with the tree on timeout and at web shutdown (the Maven child registry of `sbmig/build/mvn.py`); `sbmig`
  cannot be imported by the script; environment variables matching `KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH` or `LLM_*`
  are not passed on; a refused path (`PathRefused`) fails the run even when the script catches it; ~0.2–0.3 s per run.
  `script.python` needs pydantic and lxml (else status error `ModuleNotFoundError`; RUNBOOK).
* `p.files(glob)`: `**/` = zero or more folders (`**/*.java` also matches a root file), `*` / `?` / `[…]` stay inside
  one segment (a glob without `/` matches the project root only), a trailing `/` = everything below. `p.write` returns
  True when the bytes changed and keeps encoding, CRLF / LF and a UTF-8 BOM; a character the legacy encoding cannot
  hold becomes `\uXXXX` in `.java` / `.properties`, any other such file is written as UTF-8 (log line). `p.delete` /
  `p.move` take folders too (`move` refuses an existing `dst`); reads inside `target/` are allowed; `print` → log.

**Structure review** (`sbmig/review/`, orchestrator `_step_review`)
* Input also carries the facts sbmig computed (mandatory / optional artifacts where declared, javax next to jakarta,
  duplicate declarations) and per-module source encodings as Java charset names (cp949 → `MS949`); `report.language:
  ko` → the model answers in Korean. At most 2 LLM calls (one "answer in the format" retry when unreadable); an LLM
  error → verdict `failed` and the build goes ahead (the fake LLM without answers ends `failed` after 2 calls).
* Script guards: not run when the tree has uncommitted changes; after an `ok` run the tree is restored (`reverted`,
  note) when the script touched sources (`.java .kt .groovy .scala .jsp .jspx .tag`) or left a pom that no longer
  parses. The orchestrator commits `review: fix critical structure issues (<n>)` (uncommitted changes without an ok
  run are discarded), then records the review, so the `reviews.jsonl` line has the commit sha. Journal
  `structure_review {step, verdict, issues, critical, script_status, commit, note}`. The review script is not stored
  under `scripts/` (it is in the review line, redacted).
* Report: section "Structure review" / "구조 점검" after Handover, before Blocked; header row; console line; Handover
  follow-ups (what the script changed + commit, each unfixed critical issue, warnings; at most 8). `SBMIG-RESULT …
  attn=<codes> review=<ok|issues|skipped|failed>`: the `review=` token is appended only when the report has a review
  (v1 and fleet lines unchanged).

**Script KB** (`sbmig/kb/scripts.py`, `sbmig/kb/catalog.py`, `sbmig/kb/store.py`)
* Identical code (sha1 of the stored bytes) → the existing version, nothing written; version numbers are never reused;
  a broken meta is skipped with a warning. `credit`: improved / build_ok → success (+ project), partial → partial,
  no_change / regressed / reverted / apply_failed / blocked → failure, inconclusive / pending → nothing; `last_*`
  always.
* best / pin / retire: a pin (`sbmig kb cases pin <id> --action script:N`, checked: a stored, not quarantined version
  of that case) comes first while active and needs no success; **retire wins over a pin**; a case with `human.status:
  rejected` gives nothing to reuse; `latest` = newest active (augment base).
* Quarantine: code matching a secret pattern (`kb.redact_file` rules) → stored masked, status `quarantined`, note,
  never selected; `retire` / `activate` refuse it (it stays quarantined).
* Audit: `<kb>/audit.jsonl` lines `event: "script"`, `action` created | quarantined | credited | retired | activated,
  with `case_id`, `signature_generic`, `version`, `parent`, `who`, run / project / model / outcome / reason / command /
  `before` when known, and `after {status, successes, failures, partials, success_projects}` (one `credited` line per
  scored attempt).
* Catalog: action `script` (best proven version) is computed first; the v1 actions only when their v1 flags are on
  (`recipe` ← `kb.use_active_recipes`, `replace` ← `kb.case_replace`, `patch` ← `kb.use_patch_replay`), so v2 gives
  `script` or `hint`. Status `candidate` once a version succeeded, `active` after `kb.auto_promote_after_projects`
  success projects with no failure of that version. `evidence.fixes` one row per script version (method `script`
  whatever the mode), `evidence.failures` per method, timeline steps name the version (`script_reuse v1:improved`), the
  case hint leads with the best script (version, record, analysis).
* Views `cases.md|csv`: columns `scripts` (stored versions) and `best_script` (`v2* (3 ok / 0 failed, 2 project(s))`,
  `*` = pinned) at the end. Error CSV: `script_case_id`, `script_version` before `raw`; attempt CSV: `script_mode`,
  `script_case_id`, `script_version`, `script_parent`, `script_status`, `script_error`, `script_log` before
  `diff_truncated`.
* Tracker: fix method by mode, resolver `script:<case_id>:v<N>` (v0 = not stored), description = the LLM analysis (a
  reuse: the stored version's), `llm_attempts` counts `llm` / `script` / `script_augment` (not `script_reuse`); error
  records reach the KB without `kb.harvest`.
* CLI: `sbmig kb scripts list [--case ID] [--status S] | show <case_id> [--version N] | retire <case_id> <N> | activate
  <case_id> <N> | validate`; script lines in `sbmig kb errors list / show`; `sbmig kb stats` keys `script_cases`,
  `script_versions`, `scripts_active`, `scripts_retired`, `scripts_quarantined`, `scripts_proven`, `reviews`.
  `kb export` / `import` carry `scripts/` and `reviews.jsonl`: the import merge matches versions by sha1 (the larger
  of each counter, success projects united, the newer `last_*`, the local status wins), adds the others as the next
  local version with their parents mapped (note `imported (vN there)`); reviews by id.
* `record_review`: texts, script and run log redacted, the home directory as `~`; an id already stored is not written
  again.
* v1 files v2 does not write: `recipes/`, `cases/`, `patterns/`, `symbols.json`, `index.json`, `ledger.jsonl`,
  `error_hints.json` (their flags are off; code removed in step 4). Kept: `errors.jsonl`, `fix_attempts.jsonl`,
  `catalog/`, `views/`, `audit.jsonl`, `quarantine.jsonl`, `.lock`; new: `scripts/`, `reviews.jsonl`.

**E2E replay** (step-2 fixtures): `replay/llm-*.json` = `{"note", "match", "replay_role", "response"}`; `match` is a
regex searched in the request's non-system messages (`case: <case_id>`), `replay_role` `fix` (also answers
`fix_escalation`) | `structure_review`; each request takes the first unused matching entry (file order), the last
match repeats, no match → the "no fix" answer; entries without `match` / `replay_role` = the old positional replay.

## v2.1d — dependency version policy (2026-10-27)

User request (company run, SiteMesh 2 → the LLM added `org.sitemesh:sitemesh:3.2.1` and ported the filter; the build
passed, the runtime is unverified): "메이저를 올리지 마라. 마이너에서 jdk21 / jakarta 를 지원하는지 검색 후 올려라. 메이저를
올렸으면 이런이런 거 변경했으니 운영자분이 꼭 확인해 보세요 라고 가이드를 줘라. 라이브러리별 지식은 사람이 주입하고,
재배포는 없어야 한다. 빌드는 꼭 통과해야 하니 Handover 로 막지 마라." Four pieces, none of them a block:

1. **Policy in the prompt** (`sbmig/fix/prompts/script_system_v1.md`, also the review prompt): never change a
   dependency's major version or replace a library on your own; raise a javax-era library to the newest release of the
   SAME major that is a jakarta build (the prompt section "Dependency versions" lists what the repository has); a
   higher major only when the same major has no jakarta build — then port everything that major changes (config
   files, registrations, JSP tags, renamed packages), add `<verify>` (운영자 확인 사항) and MIGRATION-TODO what is left.
   A table entry marked approved may be used as given. If the model can look releases up itself it says so in
   `<analysis>`; the repository list is authoritative for what the build can download.
2. **Facts in the prompt** (`sbmig/build/repo_versions.py`, `FixContext.library_hints`): for the libraries behind a
   case (the errors' `artifact`, else the owner class located on the module classpath → `ClassLocation.artifact`,
   ≤ `libraries.max_artifacts_per_case`), the people's table entry and the repository's releases: same-major releases
   newer than the current one with "jakarta ✓ / javax" from their poms, the newest release of each higher major.
3. **Read-back guard** (`sbmig/build/depchanges.py`): after every applied try (script or edits) and the review commit,
   the pom diff is parsed; `major` / `replaced` changes become `FixAttempt.major_upgrades` / `FixTry.major_upgrades`
   / `StructureReview.major_upgrades` with a guide (`verify`): the answer's `<verify>`, else ONE LLM call
   (`VerifyGuideLike.verify_guide`, `libraries.verify_guide_llm`), else `synthesized_guide`. WARNING log line
   `메이저 업그레이드: <key> <before> → <after> (<pom>) — 운영자 확인 필요` + the guide lines at INFO. Nothing is
   blocked or reverted for this reason.
4. **Operator section**: `RunReport.operator_checks` (changes still in the final tree, `still_applied`), rendered
   first in the report Handover as "운영자 확인 필요 (메이저 업그레이드 N건)" / "Operator checks (N major upgrades)",
   `report.json`, the error list (`errors.md`: `메이저 변경:` + `운영자 확인:` lines, the per-error JSON), and
   `SBMIG-RESULT … major=<n>` (token appended only when n > 0, like `review=`).

### Models (`sbmig/core/models.py`)
* `DependencyChange(path, key, before, after, kind: major|minor|added|removed|replaced, replaced_by, section:
  dependencies|dependencyManagement|plugins|pluginManagement|parent, approved, note)`; `.needs_operator` (major or
  replaced), `.text` (`g:a 2.4.2 → 3.2.1`).
* `VersionCandidate(version, jakarta: bool|None, prerelease, java)`; `RepositoryVersions(key, current, major,
  same_major[], higher[], first_jakarta, source, error, checked_at)`.
* `OperatorCheck(kind: major_upgrade|library_replaced, change, case_id, source "fix r3 try 2"|"review", commit, guide,
  guide_source llm|sbmig, files)`; `RunReport.operator_checks`.
* `ScriptProposal.verify` (`<verify>`), `FixTry.major_upgrades` / `.verify`, `FixAttempt.major_upgrades` / `.verify` /
  `.verify_source`, `StructureReview.major_upgrades`.
* `JakartaLibrary.allow_major` (a person allows the cross-major move to `version`), `.source` shipped|company.

### Seams (`sbmig/core/interfaces.py`)
* `FixContext.library_hints: list[str]` — the "Dependency versions" section lines (table entry + repository facts).
* `VerifyGuideLike.verify_guide(ws, cluster, ctx, changes, diff) -> str` (optional on a fixer; `LLMFixer` has it;
  "" on failure, never raises).

### Config (`config/default.yml`, `tests/core/test_v21d_contracts.py`)
* `build.repository_url` (None → settings mirror → Central unless offline), `build.repository_timeout_seconds` 10.
* `libraries.lookup` true, `max_pom_checks` 6, `include_prerelease` false, `max_artifacts_per_case` 3,
  `company_file` `libraries.yml` (relative to `kb.dir`), `verify_guide_llm` true.

### Functions
* `sbmig.core.jakarta_libraries`: `jakarta_libraries(path=None, company_path=None)` merges the shipped table and the
  company file (same schema, `allow_major`; a company entry replaces the shipped one with the same key);
  `jakarta_library_for(key, ...)`; `company_libraries_file(cfg) -> Path`; `library_hint_line(entry, current) -> str`.
  The company file is read fresh per run (mtime-aware cache).
* `sbmig.build.repo_versions`: `RepositoryLookup(cfg, fetch=None)` (`repository_url()`, `versions(g, a)`,
  `pom_text(g, a, v)`, `candidates(key, current) -> RepositoryVersions`), `mirror_from_settings(path)`,
  `parse_version`, `is_prerelease`, `hint_lines(rv, entry)`.
* `sbmig.build.depchanges`: `major_of`, `dependency_changes(before, after, path)`, `changes_in_commit(ws, pre, sha)`
  (needs `Workspace.file_at(sha, path) -> str | None`, `git show sha:path`), `apply_table(changes, libraries)`,
  `still_applied(ws, change)`, `synthesized_guide(changes, lang)`.
* Fix loop (`sbmig/loop/fixloop.py`): `_library_hints(c)` before `_ask_llm` (new and augment; INFO line
  `의존성 버전 조회: <key> <current> → 같은 메이저 …, 상위 …` or the lookup error); after `_applied` the diff read-back,
  the guide and `finish_try(..., major_upgrades=…, verify=…)`; the reused stored script goes through the same read-back
  (guide synthesized). Orchestrator: the review commit read back into `StructureReview.major_upgrades`; at finalize
  `report.operator_checks` from the entries' tries + the review, `still_applied` filter.
* Report (`sbmig/report/handover.py`, `markdown.py`, `result_line.py`, `i18n.py`): the operator block first in
  Handover; `major=<n>` on the result line.

### As built (v2.1d, 2026-10-27)
* **Table approval is stricter than the brief**: "approved" means a person's `allow_major` (onto the entry's major
  line) or a same-major pin (`version` == after, for the library the entry describes — the key's own entry, or for
  `replaced` an entry whose `old_keys` cover the old coordinates). A cross-major table version without `allow_major`
  is a **fact**, not an approval: the hint line says `the table knows the jakarta release 4.2.1 (crosses 1.x → 4.x —
  use it only if the repository has no same-major jakarta build, then port … and write <verify>)`, the read-back note
  says `the shipped table knows this jakarta release (no same-major jakarta build listed)` and the change stays an
  unapproved operator check. So the shipped `org.sitemesh:sitemesh` 3.3.0 entry does **not** approve a SiteMesh 2 →
  3 swap; a company entry with `allow_major: true` does. A company entry may omit
  `jakarta_since` (defaults to `version`); YAML numbers become strings. A broken company file → one warning, shipped
  table only. `library_hint_line` adds "crosses N.x → M.x: port what that major changes and write <verify>" when a
  non-approved table version is a higher major.
* **Repository lookup**: `build.repository_url` wins (borrowing the mirror's credentials when the URLs match), else the
  catch-all mirror of `build.settings_file`, else `~/.m2/settings.xml`'s mirror (what Maven uses), else Maven Central
  unless `build.offline`. Two timeouts / connection failures in a row stop all further calls for the run (one INFO
  line); the local repository is the fallback. Same-major candidate poms are checked newest → oldest and stop at the
  first javax build; the remaining `max_pom_checks` budget walks a jakarta higher major down to `first_jakarta`.
  `is_prerelease` treats any unknown qualifier as a prerelease except RELEASE / Final / GA / sp / jre / android.
* **Read-back**: a classifier-only change is `minor` / `major` with a `classifier a → b` note (not `replaced`); an
  empty pom text (new / deleted pom) gives []. `changes_in_commit` needs `Workspace.file_at(sha, rel)` (`git show`).
* **Guide**: a reused stored script (`kb-script`) never spends an LLM call — its guide is synthesized; a `new` /
  `augment` answer without `<verify>` gets ONE `verify_guide` call (`libraries.verify_guide_llm`, budget allowing),
  usage read from `fixer.last_verify_usage` (the method returns text only) and charged like `_ask_llm`; the review
  commit prefers `rv.answer.verify`, else synthesized. Replay clients are role-scoped, so e2e runs synthesize. The
  synthesized guide follows `report.language` (log lines stay Korean). `FixTry.verify_source` records who wrote it.
  The orchestrator's `LazyFixer` forwards `verify_guide` / `last_verify_usage` / `verify_usage` (explicit methods only:
  no catch-all `__getattr__`, so a probe never builds the LLM client).
* **Operator checks**: deduplicated by (pom, key, after) — the latest try wins; tries `regressed` / `reverted` /
  `apply_failed` or rolled back are skipped; `still_applied` filters the rest (a failing check keeps the item). Flag
  `major-upgrade` is an attention flag (`attn=`, fleet `attention` column). `SBMIG-RESULT … review=… major=<n>`.
* **Prompt section** "Dependency versions (what the repository has; policy: same major first, see rule 14)" sits after
  the POM sections (order 3.5) and is dropped late in truncation (after `reference_notes`). Its last line per library
  is an explicit **verdict** (`hint_lines` → `_verdict_line`, 2026-10-27 "qlrm 같은 경우는 메이저로 올라가야 하지 않아?"):
  `raise to 2.6.0 (same major 2.x, jakarta build) — no major change, no <verify> needed` / `the 1.x line has NO
  jakarta build, so a major upgrade to 4.2.1 is allowed here (rule 14: …; jakarta since 4.0.1) — port … and write
  <verify>; the change will be listed for the operator` / `a person approved the move to 3.3.0 (allow_major)` / `no
  jakarta build is known on any line — stay on 1.x … <no_script>`. No verdict without facts (lookup error, unknown
  current version). QLRM is the reference case: coordinates moved (`ch.simas.qlrm` → `org.qlrm`), the Java packages
  `org.qlrm.*` did not (4.x drops `org.qlrm.util`) — the port is the pom coordinates; the change is still an
  unapproved operator check unless the company table says `allow_major`.
* Tests never reach a repository: `tests/conftest.py` installs an offline `RepositoryLookup` stand-in (autouse);
  `tests/build/test_repo_versions.py` drives the real class with a fake fetch.

## v2.1e — KB learning records: why a script failed, as-is / to-be code (2026-10-27)

User: "비슷한 케이스는 스크립트로 실행해서 고치고, 실패하면 개선 버전을 만드는데 **왜 실패했는지 이유도 json 에**, 성공한 카운트도;
코드 수정은 코드화 가망이 희박하지만 뭐가 바뀐지 알면 써먹으니 **as-is 코드와 to-be 코드도**; 사람이 같이 개선하는 에이전트."
Snippets are whole by default (user: "사람이 보는 거면 40줄 고정 안 해도 되지 않을까" — `errors.snippet_lines: 0`).

### Models (`sbmig/core/models.py`)
* `EditHunk(lines, before, after, truncated)` — one changed region: `before` = the removed / replaced lines (as-is),
  `after` = the added lines (to-be), `lines` spelled like `EditNote.lines` (`~12`, `+42–47`, `-3`).
  `EditNote.hunks: list[EditHunk]` in diff order (a note that covers several regions has several hunks; a new file =
  one hunk with `before == ""`; a deletion = `after == ""`; a binary / mode-only change = no hunks).
* `ScriptRun(run_id, project, round, n, result, reason, errors_before, errors_left, errors_new, files, at)` — one run
  of a stored version; `reason` = the try's note in words (`no_change: 3 error(s) remained …`, `regressed: 2 new
  error(s) in … `, `script error: KeyError …`, `timeout after 60s`, `rejected: …`), "" when it resolved the case.
* `CaseScript.parent` (augment lineage), `.reason` (why the version was made = the parent's failure text),
  `.last_failure`, `.runs` (newest last, `errors.script_runs` kept). `successes / failures / projects / last_result`
  stay as they are (the counts the user asked for were already there; now the reasons sit next to them).
* Config: `errors.snippet_lines` 0 (whole hunks), `errors.script_runs` 30.

### Functions
* `sbmig.script.notes` (agent N): `hunks_from_diff(diff_text, *, max_lines=0) -> dict[str, list[EditHunk]]` (path →
  hunks, the diff's "after" path, old path for a deletion); `notes_from_diff` fills `hunks` on its diff notes;
  `merge_notes(llm_notes, diff)` attaches the matched file's hunks to each LLM note (a note that names a `where` still
  gets ALL the file's hunks — the diff is the truth); `attach_hunks(notes, diff, *, max_lines=0) -> list[EditNote]`
  for notes that lack hunks (the reindex backfill). `max_lines` > 0 keeps that many lines per side and sets
  `truncated`. Never raises.
* `sbmig.errors.entries.ErrorList.finish_try` keeps `hunks` (masked with `clean`, no cap unless `snippet_lines`);
  `sbmig.errors.common.note_text` is unchanged (one line); a new `note_block(n) -> list[str]` renders the note line
  plus fenced `as-is` / `to-be` blocks per hunk for `kb errors show` / `cases show` (`entry_lines`); `errors.md` /
  report.md keep the one-line notes (the JSON files carry the code).
* `sbmig.errors.cases.CaseStore` (agent K): `add_script(..., parent: int | None = None, reason: str = "")`;
  `credit(case_id, version, result, project="", run_id="", *, reason="", round=0, n=0, errors_before=None,
  errors_left=None, errors_new=None, files=())` appends a `ScriptRun` (capped), sets `last_failure` on a failure;
  `recount()` also rebuilds every version's `runs` from `errors.jsonl` (tries with `script_version` > 0 and source
  `kb-script` / `llm-new` / `llm-augment`: result, note → reason, files; deduplicated by run_id + round + n) and
  `parent` / `reason` where an augment try's record says so, and backfills `hunks` on tries that have a `diff` but
  notes without hunks (rewrites `errors.jsonl` and the per-error JSON files once, under the lock).
* Fix loop (agent K): `_store_script` passes `parent=att.script_parent` and `reason=` the base attempt's failure
  (its `_try_note`, prefixed `v<N>: `) for an augment; `_credit_script` passes `reason=_try_note(att)` (only for a
  non-success), `round`, `n` (the try), `errors_before=att.errors_before`, `errors_left=att.errors_after`,
  `errors_new=len(att.introduced_errors)`, `files` = the attempt's changed paths. Journal `script_credit` gains
  `reason`. INFO line after a credit: `cases/<id>/fix_v<N>.py: <result> (성공 S / 실패 F) — <reason>`.
* `cases.md` gets a "last failure" cell per script (`v2 unverified 0/1 — no_change: 3 error(s) remained`).
* Report: unchanged (the KB files carry the detail).

### As built (v2.1e, 2026-10-27)
* `hunks_from_diff` keys: the "after" path (old path for a deletion, new path for a rename; quoted Korean paths
  decoded); binary / mode-only files map to `[]`; line ends normalized to `\n`; `\ No newline at end of file`
  dropped. A stored diff cut at `errors.diff_chars` yields a last hunk ending in the line `DIFF_CUT_LINE = "… (diff
  cut: errors.diff_chars)"` — not marked `truncated` (that flag is reserved for `snippet_lines`); the loop attaches
  hunks from the FULL diff at apply time, so only the reindex backfill of old records sees cut diffs.
* `merge_notes`: every LLM note of a file gets ALL of that file's hunks (separate copies per note); `try_notes(…,
  max_lines=)` / the review call site pass `errors.snippet_lines`.
* `ErrorList.to_report()` drops the hunks from the report copies (report.json stays size-bounded; the report text was
  already one line per note); `record_run` keeps the full entries, so `<kb>/errors.jsonl` and the per-error JSON
  files carry the code. `note_block` renders the code in `sbmig kb errors show` / `cases show`.
* Masking: each hunk's `before` / `after` goes through `ErrorList.clean` (secret patterns), no length cap.
* `CaseStore(kb_dir, cfg=None, *, lock_timeout=None, script_runs=None)` — `errors.script_runs` **0 = no cap** (same
  convention as `snippet_lines`). `credit` replaces an earlier run with the same (run_id, round, n) only when n > 0;
  `last_failure = reason or result` on a failure. `ScriptRun.round` = the **fix round** (`FixTry.round`), not the
  attempt's iteration, so live records and reindex-rebuilt ones deduplicate. The loop's `_run_reason(att)` makes the
  reason say something (`no_change: 1 of 1 targeted error(s) remained — <first error>`, the try's note without the
  reuse banner / escalation / merge-hint noise, the first new error of a regression); the adapter
  `sbmig.loop.entries.CaseScripts.add_script / credit` pass the v2.1e keywords through `**optional` (dropped on an
  older store).
* `kb reindex`: rebuilt runs are **merged** with the store's own records (the store's error counts win; the rebuild
  fills blanks); a try counts as a run only when its script really ran (a script stored next to applied edits, a
  "not run again" duplicate and pending tries are skipped; a failure before the edits applied counts `apply_failed`).
  `errors.jsonl` is rewritten atomically under the lock, only changed lines re-serialized, unreadable lines kept.
  INFO `reindex: N script run record(s) rebuilt, M entr(ies) given as-is/to-be hunks, F build-resolved entr(ies)
  given a probable fixer` (v2.1h: hunks and fixer attribution counted apart, A15). `kb cases show` prints
  `parent`, `reason`, `last failure` and `runs (N, newest last)` per script; `cases.md` shows `v2 unverified 0/1 —
  <last_failure>`. `_clean_entry` masks note `where` / `change` and hunk text; `merge_from` (KB import) maps an
  imported script's `parent` to the local numbering and merges runs.

## v2.1f — who fixed an error that had no try of its own (2026-10-28)

User (company KB): a case with `seen 4 / resolved 4`, no script, no try — "뭘 어떻게 수정했다 이런 건 안 적혀 있는데". Such an
error disappeared from a build after ANOTHER case's fix of the same round (root causes first; the `does not override
renderMergedOutputModel` error vanishes once the file's javax → jakarta import case is applied). The record now says so.

* `ResolvedBy(case_id, round, n, commit, files, match: file|pom|reindex, text)`; `ErrorEntry.resolved_by: list[ResolvedBy]`
  (nearest first: the try that changed the same file, else the deepest pom above it, up to 3).
* `sbmig.errors.entries.probable_fixers(entry, tries, *, rounds=None, match="file", limit=3)` — the applied tries
  (`(case_id, FixTry)` pairs; not `reverted` / `apply_failed` / `blocked`) of the given rounds that touched the entry's
  file or a pom above it. Live: `ErrorList.add_build` fills it for every entry it resolves by `build` with a case
  (`rounds=[rnd]`, the round the build scores; round 0 = the review's edits). `resolved_text(entry)` → `build ←
  7a52b1cea8 r1 try 1` for `errors.md`, the report's error list and the case summary (`case.json` `last_result`:
  `resolved 4 (build ← 7a52b1cea8 r1 try 1) — …`, so `cases.md` shows it); `kb errors show` / `cases show` print
  `resolved by: <case> r<round> try <n> — <file lines — change>`.
* `kb reindex` backfills it for old records (`CaseStore._fixer_attributed`): entries resolved by `build` without
  tries get the run's applied tries from their `seen` round on, earliest round first, `match="reindex"`; INFO line
  `… N build-resolved entr(ies) given a probable fixer`. Restructure (`pom-verify`) entries are never attributed.

## v2.1g — every static resource reaches the front module (2026-10-28)

User (company run): "정적 리소스들(html, js, mp4, exe …)은 기존 디렉토리에 그대로 남고 jsp 만 front 로 간다 — 어디다 쓸지 모르니 일단
front resources 에 디렉토리까지 똑같이 그대로". The company layout (`APP/` + `STATIC/` + `DB/`, fixture
`servlet-nonstandard-dirs`) keeps files under a `<resource>` dir INSIDE the web root (`STATIC/resources/**`: media,
templates, css, downloads) as classpath content of the comp module (rule 3 of `_populate_front`) — that is what the
user saw left behind.

* `layout.front_copy_resource_dirs` (default **true**): those files stay on the comp classpath (the copied
  `<resources>` entry still points at them) AND are **copied** byte for byte into `<front>/<front_static_dir>/<web
  path>` — the same URL the as-is WAR served (`/resources/media/intro.mp4` → `static/resources/media/intro.mp4`).
  `_Ctx.copy(src, dst)` (never overwrites; counts in `moved_files` / `front_files`). The note says `N file(s) of
  <resource> dir(s) inside the web root copied to src/main/resources/static at their web path (the classpath copies
  stay in <comp>)`; the old follow-up ("copy them into … if pages link them") only appears with the switch off.
* WEB-INF files that are not configuration (`WEBINF_CONFIG_EXTS` = xml, properties, yml, yaml, conf, ini, cfg) —
  views, html / js / css fragments, templates, downloads — keep their path under the front jar's
  `META-INF/resources/WEB-INF/…` (reachable by jsp:include / RequestDispatcher, never served to the browser), like
  JSPs, tags and TLDs already did; configuration files still go to the boot stash `asis-webinf/`; `WEB-INF/lib`,
  `WEB-INF/classes` and `META-INF` stay in the comp module (follow-up, unchanged).
* Unchanged: everything else under a web root already moved to the front module whatever its extension; web
  content in a directory no pom references (`_web_content_left`) is still only a follow-up (`web-content-left`); the
  Boot as-is mode (`boot_as_is: in-place`) moves nothing.

## v2.1h — JSP javax → jakarta rename + fixes from the handover read (2026-10-28)

User decisions on `docs/v2/HANDOVER-FINDINGS-2026-10-28.md`: A1 fix (EUC-KR pom), A2 keep (highest higher major), A3 "3회로
맞추자" (docs follow the code), A4 pending (explained), A5 "JSP 도 javax → jakarta 로" (OpenRewrite never parses JSPs), every
other suspected bug fixed. Dead code / unread keys (§B) untouched.

### Config (`sbmig/core/config.py`, `config/default.yml`)
- `layout.jsp_jakarta_rename: bool = True` — after the restructure put every file in its final place (all modes: layout,
  boot_pom / in-place, msa_pom, optional_bump), the javax EE names of the analyzer's table
  (`sbmig/analyze/jakarta_packages.yml`, `jakarta_rules()` / `javax_ee_token()`) are renamed to `jakarta.` inside the
  code regions of every JSP / JSPF / JSPX / tag file (`sbmig.analyze.jsp.jsp_code_spans`: page / tag `import`, useBean
  `class` / `type`, scriptlets; Java string literals and comments untouched, taglib URIs untouched — JSTL 3 still serves
  the `java.sun.com` URIs). Files are written back in their own encoding (UTF-8 / MS949 / EUC-KR) with their line ends.
  False = the pre-10-28 behaviour (Handover follow-up only).
- `llm.provider` class default is now `company` (A6; `config/default.yml` already said so).

### Models
- `RestructureResult.jsp_jakarta_files: int`, `jsp_jakarta_refs: int`, `jsp_jakarta_renamed: list[str]` ("<rel path>: N").
- `GAV.parse` (A26): `g:a:packaging[:classifier]:v` (four or more parts) takes the last part as the version.
- `LLMRole` gains `"verify_guide"` (A21): the one call `LLMFixer.verify_guide` makes; replay files answer it with
  `replay_role: verify_guide`.

### Report
- "What we changed": `JSP javax → jakarta: N files, M references` when the rename ran; Handover follow-up
  `JSP javax references remain (N files)` when `layout.jsp_jakarta_rename` is off or files were left untouched
  (`SourceStats.jsp_javax_files`, A5 — the count existed since round 9 but nothing read it).

### Fixes (file → finding id in `docs/v2/HANDOVER-FINDINGS-2026-10-28.md`)
`analyze/pom_parser.py` A1 (an XML declaration naming a multi-byte encoding is replaced before expat parses the UTF-8
text — same as `source_scanner._xml_root`), `analyze/classifier.py` A25; `fleet/manifest.py` A7, `fleet/runner.py` A8 / A9;
`web/runs.py` A10 (FIFO queue), `web/app.py` A11; `report/writer.py` A12 / A13; `triage/scripts.py` A14
(`script.reuse_versions` honoured: the next stored version when the best one failed this run); `fix/fixer.py` A20 (target
from `cfg.target`), A21; `loop/orchestrator.py` A23; `loop/entries.py` A22; `errors/cases.py` A15 / A16 / A17 / A18;
`kb/cli.py` A19 (`cases pin` refuses an unknown case id unless `--create`); `restructure/restructurer.py` A24.
Docs: `docs/v2/KB-AND-FLOW.md` review call limit 2 → 3 (A3).

### As built (v2.1h, 2026-10-28)
- Restructure: `jsp_jakarta.rename_jsp_files` runs for `.jsp / .jspf / .jspx / .tag / .tagx` (the analyzer's
  `JSP_SUFFIXES`), skips `.git / .svn / .hg / node_modules` anywhere, `target/` (via `Workspace.walk`) and `out/` at the
  root; encoding = UTF-8 BOM → `pageEncoding` / `contentType` charset / XML declaration → UTF-8 → cp949 → EUC-KR, accepted
  only when decode + encode reproduces the bytes (else the file is left and counted as "not renamed" when it names javax);
  target names come from the table's `to` (`javax.security.cert` → `java.security.cert`). Its own commit
  `restructure: JSP javax -> jakarta`; the orchestrator adds the step note `<step>: JSP javax → jakarta: N files, M
  references` (`sbmig.report.handover.jsp_jakarta_note`, parsed back by `jsp_jakarta_counts`) and a `jsp_jakarta` journal
  event (files, refs, renamed ≤ 500). Not covered: `<%@ attribute type=… %>` / `<%@ variable variable-class=… %>` in tag
  files (the analyzer's code regions do not include them either).
- `PomEditor`: `ed.encoding` = the declared name (unchanged), `ed.codec` = the codec it saves with; an ISO-8859-1-declared
  pom whose bytes happen to be valid UTF-8 is decoded as UTF-8 and re-saved as latin-1 (rare).
- Fleet (A7–A9): `manifest.has_pom()` (top or one level down) is the one rule for listing and `_validate`; a row with a
  missing input is `skipped` only when this run would start it (pending / running / infra-deferred / retried) — success
  rows and failed rows kept without `--retry-failed` stay; `FleetProjectResult.llm_calls`, token counts and `llm_cost`
  add up every attempt, the other per-run numbers are cleared when an attempt starts, `SBMIG-RESULT llm=` is that run's
  own calls, the progress line adds `(all attempts N)` when they differ.
- Web (A10): `_FifoTurns` (deque + condition) — a run takes its turn when `start_file` accepts it; the worker leaves the
  queue in `finally`.
- KB (A15–A19): `kb reindex` → `N case(s), V script version(s); R script run record(s) rebuilt, H entr(ies) given
  as-is/to-be hunks, F build-resolved entr(ies) given a probable fixer — <cases.md>`; WARNING wording `hunk / resolved_by
  backfill`; `cases pin` refusals `unknown case <id>; use --create to pin a case the KB has not seen` and `<id> is no case
  id (10 characters, …)`; `merge_from` also skips unlisted `fix_v*.py` of a case folder without `case.json`.
- Triage (A14): `stored_next(store, case_id, tried)` (proven newest first, then unverified; retired / codeless skipped)
  is consulted only when every reuse of this run failed (`FAILED_OUTCOMES` now lives in `triage/scripts.py`); an
  improved / partial reuse goes to augment.
- Fix (A20 / A21): `profile_summary(profile, cfg=None)`; `ContextPacker.pack_script` and the verify prompt rewrite the
  ` | target: …` tail from `cfg.target` because the loop builds the summary without `cfg`; `LLMRole` includes
  `verify_guide` (plugin adapters receive it as is; `docs/ADAPTERS.md` lists it with `structure_review`).
- Orchestrator (A23): the review's LLM calls sit on the first fixed critical issue's try; the others carry 0 and a note.
- A4 (user: "C로 가자"): edit applier shape `fn(ws, edits, *, cfg, errors=None)`; `fixloop.call_edit_applier` passes
  `errors` only when the signature takes it (`_accepted`, A22); docs/ADAPTERS.md "Edit applier (tier 2b)".
- Still the old shape: `fixloop.py` `try_notes` / `verify_guide` TypeError-retry (A22 fixed `_Guarded` only); the
  `examples/adapters/company_gateway_adapter.py` models table has no `verify_guide` row.
- `.env` lookup (2026-10-28, the company keeps the package under `<root>/src/migration-agent/`): `load_dotenv_files` reads
  `$SBMIG_ENV_FILE`, then every `.env` from `PACKAGE_ROOT` up to the folder holding `.git` (`dotenv_chain`), then the same
  from the cwd; nearest first, so a nearer file wins a key and shell variables still win everything.


## v2.1i — `fix.mode: script | edits` (the script ladder on / off, for the token comparison, 2026-10-28)

User: the business team wants "현행 버전(스크립트 저장·재사용) vs 스크립트 없이 LLM 한테 전체 수정을 맡겼을 때" token usage side by
side; runs go through the web UI (uvicorn, zip upload); "스크립트 만드는 부분을 껐다 켰다 하는 기능" so the comparison is fair.

### Config (`sbmig/core/config.py`, `config/default.yml`)
- `fix.mode: "script" | "edits"` (default `script`).
  - `script` = unchanged v2 ladder: the LLM answers with `<edits>` AND a reusable `<script>` (+ `<changes>`), scripts are
    stored per case, reused (`kb-script`) and augmented on later projects.
  - `edits` = the LLM answers with `<edits>` only: the system prompt is `sbmig/fix/prompts/edits_system_v1.md` (the same
    rules minus everything about scripts: no helper API table, no `<script>` / `<changes>` / `<no_script>`, no "Script or
    no script"; `<analysis>`, `<solution>`, `<verify>` (rule 14 unchanged), `<edits>`, `<need_files>` / `<need_grep>` stay);
    no stored script is consulted, stored, credited or augmented; every try of a case is a fresh ask that carries the
    previous tries of this run (what was edited, why it failed) — the same per-case LLM asks as `script` mode gives a
    case without a stored script (`script.max_augments_per_case`, the first ask counted; then BLOCKED `repeated_failure`); `verify_guide` unchanged; KB error
    records (`errors.jsonl`, `resolved_by`) unchanged, `kb.match` hints are still injected unless the KB is off.
- `FixContext.fix_mode` (interfaces.py) carries it to the fixer and to tier-2 plugins.
- `RunReport.fix_mode` (models.py).

### Loop / triage / report (`sbmig/loop/`, `sbmig/triage/`, `sbmig/report/`)
- Triage in `edits` mode never decides `reuse` / `augment` (`DecisionKind` stays; the decision is `new` with the try
  count); `BuildFixLoop` skips `_store_script` / `_credit_script`; `FixAttempt.source = "llm-edit"` (the existing
  `TrySource` value); the report's fix-attempt label is `edits:<case>#<n>` instead of `script:<case>:v<N>`.
- `SBMIG-RESULT … fix=edits` is appended when `report.fix_mode != "script"` (the default line is unchanged).
- Report header "LLM usage and cost" names the mode (`fix mode: edits`) so the two experiments can be told apart.

### CLI / web / fleet
- `sbmig run --fix-mode script|edits` (overrides `fix.mode`); `sbmig fleet run --fix-mode …` and `--no-kb` (the fleet
  builds `Orchestrator(cfg, ws, kb=NullKnowledgeBase())` per project, like `sbmig run --no-kb`).
- Web `POST /api/runs` takes `fix_mode` (`script` | `edits`, default `script`) and `no_kb` (`0` | `1`, default `0`)
  as query / form fields next to `configs`, `stop_after`, `llm_provider`; `RunManager.start_file(..., fix_mode=,
  no_kb=)`; `GET /api/runs` rows carry `fix_mode` and `kb` (`on` | `off`). `sbmig/web/static/index.html` shows two
  checkboxes under the upload form: "LLM 직접 수정만 (스크립트 생성·재사용 없음)" → `fix_mode=edits`, "KB 사용 안 함
  (저장 스크립트 재사용·기록 없음)" → `no_kb=1`; the run list shows the mode.

### As built (v2.1i, 2026-10-28)
- `FixContext.previous_edits: dict[str, str]` (attempt id → the try's label, result, why it failed, whether its changes are
  still in the tree, the applied diff ≤ 4000 chars or the edits it asked for) — filled by the loop in edits mode for tier-2
  plugins; the built-in fixer rebuilds previous edits from its own proposals through `att.prompt_hash` (one `LLMFixer`
  per run). `effective_fix_mode(cfg, ctx)` (`sbmig/fix/context.py`): edits when either says so.
- Budget: `edits_llm_limit(cfg) = max(1, script.max_augments_per_case)` — script mode counts the first ask inside that
  limit too, so both modes ask the LLM the same number of times for a case without a stored script (with one, script mode
  adds a reuse that costs no call). The contract text above was corrected accordingly.
- The structure review (`restructure.review`) still asks with its own prompt; in edits mode its script is applied but not
  stored ("not stored (fix.mode edits)"). `errors.md` keeps the `llm-edit` source wording in the tries column.
- Web: `fix_mode` / `no_kb` are query fields next to `name` / `configs`; bad values → 400 before the upload is read; rows
  for runs no longer in memory read `web-status.json` → `report.json` → defaults. Fleet: `fleet-config.json` keeps
  `overrides.fix.mode` and `no_kb` for `fleet resume`; a resume with other switches adds a second `settings:` note.
- 2026-10-29 (user: "한도 도달(LLM 3회) 가 나와서 실패 — 제한 없이 수정하도록, LLM 한테 다 맡겨서 수정했을 때의 토큰량을
  보여 줄 수 있게"): `fix.edits_max_asks_per_case: int = 0` — edits mode asks per case, **0 = no cap (default)**: the case
  is never BLOCKED `repeated_failure`, every round asks again until the rounds (`loop.phases.verify.max_iterations`), the
  time budget or `loop.max_llm_calls` stop the run, and the positive `loop.blocked_policy` counts are lifted too
  (`edits_unlimited(cfg)`; the 0 ones — company parent unresolved, encoding, unsupported source — still block at once);
  `> 0` = BLOCKED after that many asks (3 = what script mode asks a case without a stored script). Log line
  `KB 탐색: fix.mode edits — 저장 스크립트 사용 안 함 → LLM 에 다시 수정 요청 (N번째, 한도 없음, 앞 시도 M건 포함)`.


## v2.1j — Ant projects: pom.xml synthesized at intake, then the normal flow (2026-10-29)

User (business team): "Ant 프로젝트도 Maven 으로 변경 후 마이그레이션이 되도록". The 2026-10-23 decision stays for trees with
neither pom.xml nor build.xml (rejected, `sbmig inventory` describes them); a tree with `build.xml` and no pom.xml is now
accepted: the intake writes a pom.xml from build.xml + the directories, commits it, and every later step runs as for any
Maven project (case type from the analyzer: servlet_tomcat / pure_java / spring_legacy).

### Config (`sbmig/core/config.py` `IntakeConfig`, `config/default.yml` `intake:`)
`ant_to_maven` (true), `synth_group_id` (`com.company.migrated` — placeholder, company config overrides), `synth_version`
(`1.0.0-SNAPSHOT`), `unknown_jar` (`system` | `skip`), `jar_lookup` (true), `jar_names_file`, `company_jar_names_file`
(`<kb>/jar_names.yml`, merged over the shipped table, no redeploy — like `libraries.yml`), `min_java_source` (`1.8`),
`max_jar_bytes_scan`.

### Models (`sbmig/core/models.py`)
- `JarCoordinate(path, sha1, group_id, artifact_id, version, source: pom_properties | name_table | sha1_lookup | manifest |
  unknown, scope, used, packages, note)`; `.key` = `g:a`.
- `PomSynthesis(ok, pom_path, build_file, group_id, artifact_id, version, packaging, source_dirs, web_root, resource_dirs,
  lib_dirs, java_source, encoding, jars, unknown_jars, dropped_jars, build_xml_notes, warnings, error)`; `.counts` per source.
- `RunReport.pom_synthesis: Optional[PomSynthesis]`.

### Functions (`sbmig/analyze/ant.py`, `sbmig/analyze/jar_coords.py`)
- `describe_ant(ws) -> AntProject` (dataclass inside ant.py): build.xml parsed with properties resolved (`<property name/value
  | file>`, `build.properties`, `${…}` in attributes, `basedir`): the `<javac>` tasks (`srcdir`, nested `<src path>`,
  `destdir`, `source`, `target`, `encoding`, `classpath` / `<classpath refid>` → `<path>` / `<fileset dir includes>`),
  `<war>` / `<jar>` tasks (`webxml`, `destfile`, `<lib>`, `<classes>`), `<property name="version">`-like version hints, the
  project `name`; plus directory facts: a web root = the directory holding `WEB-INF/web.xml` (WebContent, web, WebRoot,
  src/main/webapp, …), lib dirs = every directory with `*.jar` that a classpath names or `WEB-INF/lib`, source roots
  confirmed by `package a.b;` matching the path (reuse `sbmig/analyze/webapp_inventory.py` helpers where they fit).
- `resolve_jar(path, data_or_file, cfg, lookup=None) -> JarCoordinate`: ① `META-INF/maven/<g>/<a>/pom.properties` inside
  the jar (groupId / artifactId / version) ② the stem table (`jar_names.yml` shipped + company file; version from the file
  name) ③ when `intake.jar_lookup`: sha1 lookup through the repository chain of `sbmig/build/repo_versions.py`
  (`build.repository_url` Nexus 3 `/service/rest/v1/search/assets?sha1=` → settings mirror → Central
  `https://search.maven.org/solrsearch/select?q=1:"<sha1>"&rows=1&wt=json`; offline / failure → unknown, never raises;
  the root `tests/conftest.py` offline guard must keep tests off the network) ④ MANIFEST `Implementation-Title` /
  `Bundle-SymbolicName` only as a note. `packages` = top-level packages of the jar's classes (for the `used` check);
  servlet / jsp / el / jstl APIs → scope `provided`.
- `synthesize_pom(ws, cfg, *, lookup=None) -> PomSynthesis`: writes `<root>/pom.xml` ONLY (no commit — the orchestrator
  commits `intake: pom.xml synthesized from build.xml`): coordinates (`synth_group_id`, artifactId = build.xml project
  name or the folder name, version from build.xml or `synth_version`), packaging war / jar, `<properties>`
  (`maven.compiler.source/target` = build.xml source/target raised to `min_java_source`, `project.build.sourceEncoding`
  = javac encoding or the detected source encoding), `<build><sourceDirectory>` (first source root; more roots → the
  build-helper plugin `add-source`), `<resources>` for non-java files under the source roots, `maven-war-plugin`
  `warSourceDirectory` = web root (+ `failOnMissingWebXml` false only when no web.xml), `<dependencies>` from the jar
  table (dedupe by `g:a`, provided scope for container APIs, `system` scope + `<systemPath>${project.basedir}/<jar>`
  for unknown-but-used jars when `unknown_jar: system`), unused unknown jars left out (`dropped_jars`), test source dirs
  ignored (tests are skipped anyway). Never deletes build.xml or any file. `ok=False` + `error` when build.xml cannot be
  read or no source root is found.
- `detect_build_tool` (build_tool.py) unchanged in shape; add `ant_build_file` (path) to `BuildToolDetection` when tool
  is ant.

### Orchestrator / report
- `_check_build_tool`: tool `ant` and `intake.ant_to_maven` → `synthesize_pom`; ok → commit, journal
  `{"event": "pom_synthesized", "pom": …, "jars": N, "unknown": M}`, flags `pom-synthesized` (informational) and
  `jar-coordinates-unknown` (attention, only when `unknown_jars`), `report.pom_synthesis = res`, then continue exactly as a
  Maven project; not ok → abort exit 3 with `res.error` (as before). `--no-kb` does not matter here.
- Handover block "Ant 프로젝트: pom.xml 합성" (right after the operator checks): the pom's coordinates and packaging,
  jar counts per source, the unknown jars with one `mvn install:install-file -Dfile=<jar> -DgroupId=<synth_group_id>
  -DartifactId=<stem> -Dversion=<version or 0> -Dpackaging=jar` line each (the operator installs them into Nexus and
  replaces the system-scope entries), the dropped jars, the untranslated build.xml targets; "What we changed" one line
  `pom.xml synthesized from build.xml (N jars: a pom.properties, b table, c lookup, d unknown)`; `FLAG_HELP` entries.
- `SBMIG-RESULT` unchanged (the flags carry it).

### As built (v2.1j, 2026-10-29)
- Analyzer (`sbmig/analyze/ant.py`, `jar_coords.py`): `describe_ant(ws) -> AntProject` (never raises; `.error` /
  `.warnings` / `.notes`), `synthesize_pom(ws, cfg, *, lookup=None)` (`lookup=None` → one `Sha1Lookup` per run when
  `intake.jar_lookup`; `False` → none), `resolve_jar`, `jar_packages`, `stem_and_version`, `mark_used`, `Sha1Lookup(cfg,
  fetch=, repo=).find(sha1)`, `find_ant_build_file(ws)`. Provided scope: servlet / JSP / EL / WebSocket APIs (javax and
  jakarta forms) and `org.apache.tomcat:*`; JSTL stays compile. Container APIs the sources use but no jar provides are
  added as provided (`javax.servlet-api:3.1.0`, `javax.servlet.jsp-api:2.3.3`, `javax.el-api:3.0.0`) with a warning.
  Source roots from the main `<javac>` srcdirs confirmed by `package a.b;` (test-like roots ignored, nested roots folded);
  extra roots through `build-helper-maven-plugin` 3.6.1 `add-source`. `used` = qualified names in Java sources, JSP page
  imports, class names in XML / properties / TLD under the web root and resources; jars with `META-INF/services`,
  `spring.handlers`, `web-fragment.xml` or taglibs (when JSPs exist) count as used; unsure → declared. War plugin pinned
  3.4.0 (an unparented pom on Maven ≤ 3.8 gets 2.2, which fails on JDK 16+); no compiler plugin / `<release>`.
  Unknown-but-used jars are NOT copied (2026-10-29 fix of the first cut): they are declared at their as-is path as
  `<systemPath>${maven.multiModuleProjectDirectory}/<as-is path>` (sbmig always runs `mvn` at the root), and the restructure
  copies them into the module's `lib/` when it moves the dependency; `JarCoordinate.path` keeps the as-is path, the
  declared `g:a:v` + `scope="system"` are filled in.
  `edges.resolve_system_path` reads `${maven.multiModuleProjectDirectory}` as the project root, so the restructure's
  normal system-path handling applies (copied into the module's `lib/`, Nexus follow-up). `warSourceExcludes` lists the
  WEB-INF/lib jars that became Maven dependencies. `ok=False`: no / broken build.xml, a pom.xml already at the root, no
  source root, any unexpected exception. sha1 lookup: `<base>/repository/<name>/` → Nexus 3 assets search, Central →
  search.maven.org; 2 failures in a row → no more lookups this run.
- Orchestrator (`_step_intake`): snapshot commit → preflight (`mvn -v`, JDK) → `_check_build_tool` → `_synthesize_ant_pom`
  (`importlib.import_module("sbmig.analyze.ant")` each call — patchable in tests) → commit `intake: pom.xml synthesized
  from build.xml` (so `changes.patch` shows pom.xml as an addition) → `_intake_layout` → effective-pom capture. Failure
  (module missing / exception / `ok=False`) → flag `unsupported-build-tool:ant`, exit 3 with the reason; `ant_to_maven:
  false` → the old rejection. INFO `접수: Ant build.xml → pom.xml 합성 (jar N개: pom.properties a · 이름표 b · 조회 c ·
  미상 d)` (manifest-only jars count as 미상); journal `pom_synthesized` and `build_tool` with `tool: "ant→maven"`.
- Report: Handover `### Ant project: pom.xml synthesized` / `### Ant 프로젝트: pom.xml 합성` right after the operator
  checks (pom g:a:v + packaging + Java + encoding, layout line, jar counts, the unknown jars with one `mvn
  install:install-file -Dfile=<as-is path> -DgroupId=<declared or synth_group_id> -DartifactId=<stem> -Dversion=<v or 0>
  -Dpackaging=jar` each and the sentence that system-scope jars are not packaged into the WAR / boot jar, dropped jars,
  `build_xml_notes`, warnings; lists cut at 50 → "… N more (report.json)"); "What we changed" first line `pom.xml
  synthesized from build.xml (N jars: …)`; `FLAG_HELP` for both flags; `jar-coordinates-unknown` is an attention flag
  (`SBMIG-RESULT attn=`, fleet attention column).
- Known gaps: a `<jar>` Main-Class and `<javac>` includes / excludes are noted, not translated; a second top-level source
  root only exists in the build-helper config, so the layout split may not move it (`gen-src`); the restructure's follow-up
  "files under WEB-INF/lib stay … declare the jars as Maven dependencies" still appears for an Ant project although the
  synthesized pom already declares them (wording only).

## v2.1k — rootless trees: the aggregator for a single top, reactor de-duplication, `reactor_duplicate` (2026-10-29)

Company failure: a tree with no root pom.xml, `APP/pom.xml` (aggregator: `../DEV`, `./EAR`, `./BATCH`), `DEV/pom.xml` (WAR,
`warSourceDirectory ../STATIC`), `STATIC/`, `DB/`. The intake's generated aggregator listed `APP` AND `DEV` → Maven refused the
reactor (`Project 'g:a:v' is duplicated in the reactor`) → effective-pom, OpenRewrite (no Java 21 / jakarta recipe ran) and
`transform.compile` all failed with that one error, the fix loop augmented a stored script 3 times, exit 2; a tree with only
`APP/` under the root got no aggregator at all (one top → None) and every Maven call ran in a root without a pom.

### Intake (`sbmig/analyze/aggregator.py`, `sbmig/loop/orchestrator.py`)
- `plan_aggregator`: with no root pom.xml, ONE top pom also gets the root aggregator (`<module>APP</module>`); several linked
  tops are grouped as before; module paths are normalised (`..\DEV` → `../DEV`); `{name}` from all listed poms and their
  modules (EAR → WAR → common prefix, trailing `-ear` dropped); a pom reachable through another top's main `<modules>` is
  never listed. Two or more unrelated groups → still no aggregator; the run then stops at analyze (non-MSA only) with exit 3,
  flag `no-root-pom`, journal `no_root_pom`, the reason in the Handover — an MSA tree without a root pom keeps working.
- `reactor_duplicates(ws, root_pom="pom.xml") -> list[ReactorDuplicate(dir, listings)]` walks the reactor in Maven order
  through the main `<modules>` plus activeByDefault profiles; `dedupe_reactor(ws, root_pom)` removes a repeat inside one
  `<modules>` block (the only root-pom edit) and otherwise the listings in NON-root poms (the root's stays; else the first
  in reactor order) through `PomEditor` (encoding / CRLF / comments kept, an emptied `<modules>` removed); noted, never
  removed: a second listing in another block of the root pom, profiles not active by default. Called at the end of
  `_intake_layout` before the effective-pom capture; a removal → commit `intake: reactor duplicate removed (DEV listed by
  pom.xml and APP/pom.xml)` (one body line per removal), flag `reactor-duplicate-removed`, journal `reactor_dedupe`,
  Handover follow-up `DEV was listed twice in the reactor (pom.xml and APP/pom.xml); the APP listing was removed so Maven
  can build — if APP is meant to be built on its own, add DEV back there and remove it from the root`.
- Profile-aware reachability in the planner was tried and reverted: the restructure / analyzer follow the main `<modules>`
  only, so a module listed only in a profile must stay a top (it is merged) and the dedupe removes the profile listing.

### Loop (`sbmig/triage/blocked.py`, `sbmig/loop/blocked_registry.py`)
- `BlockReason.REACTOR_DUPLICATE` (`reactor_duplicate`), `loop.blocked_policy.reactor_duplicate: 0`: a build error saying
  `is duplicated in the reactor` / `The projects in the reactor contain a duplicate` is BLOCKED at once (no LLM call); the
  evidence and the Blocked table's artifact column carry the coordinate; `compose_suggestion` explains it (Korean via
  `tr_line`).

### Report
`FLAG_HELP`: `reactor-duplicate-removed`, `no-root-pom`; `aggregator-generated` now covers the single-top case.

### e2e
`fixtures/projects/sibling-aggregator-dup` (the company shape, plain `<modules>`): aggregator lists only `APP`, OpenRewrite
runs from the root (5 files), company layout at the root (`dup-back / -front / -boot`, root renamed `dup` after the EAR is
merged), DEV sources in back, `STATIC` in front, `DB/` untouched, BUILD SUCCESS.


## v2.1l — dependency versions: carried over from the as-is build, never guessed by the LLM (2026-10-30)

Company failures (user, 2026-10-30): Boot / MSA runs and a Spring Framework 6.2.12 run ended after rounds of "add a dependency"
answers, the last ones saying "정책상 버전을 추정하여 추가할 수 없습니다". Cause: the versions an as-is parent (the old in-house
parent, or any external parent) managed are lost when the restructure moves a pom to `comp-jar` / `boot-jar` / `base-parent`;
v1's `carry-over-managed-version` handler pinned them from the as-is effective pom, v2 step 4 deleted it with the other handlers,
and the fix prompt's rule 14 (no invented versions) left the LLM with nothing to say. The as-is effective capture
(`logs/depmgmt.json`, `sbmig.build.effective`) still exists and is now used again; the LLM is told to leave unknown versions
empty and sbmig fills them. Decisions (user): the pins are reviewed by the LLM structure review like every other pom change
and verified by the build; the non-Boot WAR services of an MSA tree stay pom-only for now; Spring Security recipes run only
for projects that use Spring Security; Spring Cloud needs nothing new (the Boot 4 umbrella already runs
`UpgradeSpringCloud_2025_1`, a non-Boot project cannot use Spring Cloud).

### Resolution order (one rule, used by the restructure and by the fix loop)
For a `<dependencies>` entry without `<version>` (and not: the in-house parent / role parents / mandatory / optional /
`remove_when_present` artifacts, the project's own modules, `system` scope):
1. the new parent chain manages it (post-restructure effective pom, `depmgmt-after-restructure.json`) → stays empty;
2. the as-is build resolved it (`logs/depmgmt.json`: the module's `by_module[...].direct`, then merged `direct`, then merged
   `managed`) → that version is written (`carried over`; the Handover already marks such pins ⚠ "carried over (no longer
   managed)" — `sbmig/report/handover.py`);
3. only for a dependency the LLM has just added: the repository (`RepositoryLookup.versions`, `build.repository_url` →
   settings mirror → Central) → the newest release that is not a prerelease (noted "chosen from the repository");
4. nothing found → left empty; the build's `'dependencies.dependency.version' for g:a is missing` becomes a case the loop
   resolves (below) and, when it cannot, a Handover follow-up names the coordinate for the owners.
Explicit versions are never changed here (rule 14 stays: no major upgrades by sbmig); `layout.drop_managed_versions` still
removes a pin the new chain manages at the same or a newer version, so the order inside the restructure is: pin (step 2) →
effective-after capture → drop managed (gte).

### Restructure (`sbmig/restructure/restructurer.py`, `sbmig/loop/orchestrator.py`, `sbmig/core/interfaces.py`)
- `params["asis_effective"]`: zero-arg callable → the as-is capture (`load_effective(ws)`) or None; set by
  `_call_restructurer` for `layout` / `boot_pom` / `msa_pom` (independent of `drop_managed_versions`).
- `_pin_asis_versions(c)` runs in `_layout` (both passes), `_boot_pom` and `_msa` right before `_drop_managed_versions(c)`:
  every project pom's version-less dependency gets the as-is version (step 2); no as-is capture → one note and nothing pinned.
- `RestructureResult.versions_pinned: list[str]` ("g:a: <version> ← as-is <managed by g:a:v | declared> (<pom>)") and
  `versions_unknown: list[str]` ("g:a (<pom>)"); each pin is a "pom correction, verified by the next build" row like the
  dropped versions; each unknown one a manual follow-up (`declare a version for g:a in <pom> or manage it in the parent — the
  as-is build did not resolve one`); journal `{"event": "versions_pinned", "pinned": [...], "unknown": [...]}`; the
  structure review sees the pins wherever it sees `managed_versions_dropped` / the restructure notes.

### Fix loop (`sbmig/loop/fixloop.py`, `sbmig/fix/prompts/*.md`, `sbmig/fix/context.py`)
- After an attempt's edits or script applied (`_apply_edits` / `_run_script`, status ok, before the commit): `_fill_versions`
  walks the changed poms; a version-less dependency the attempt ADDED is resolved by steps 1–3, one that was already there by
  steps 1–2; the pom change joins the attempt's commit; `att.notes` + journal `{"event": "version_filled", "case", "key",
  "version", "source": "asis" | "repository"}`. Never raises; nothing filled = nothing noted.
- A case whose generic signature is `pom_invalid|missing_version` is tried first WITHOUT the LLM: the versions of its
  artifacts by steps 2–3 applied as edits (`set_dependency_version`, or `add` the version where it is declared) → an attempt
  with `source = "sbmig-version"`, `script_mode = "new"`, no stored script, committed `fix(sbmig-version): <case>` and scored
  by the rebuild like every attempt (improved / partial / …, rolled back when regressed). Nothing resolvable → the normal
  ladder (the LLM gets the facts below); after the ladder the usual BLOCKED `repeated_failure`.
- Prompt facts: the "Dependency versions" section now starts, for every artifact of the case's errors (in-house ones too),
  with `as-is build: g:a <version> (managed by <parent g:a:v>)` / `new parent chain: manages g:a at <version>` /
  `as-is build: g:a not declared` lines, then the repository lines as before.
- Rule 14 (both prompts): "If you add a dependency and do not know a version the repository has, write `\"version\": null`:
  sbmig fills it before the build (the as-is build's version, else the newest release the repository has). Never refuse a fix,
  answer `<no_script>` or leave an error unfixed only because a version is unknown." The no-guessing sentence stays.

### Recipes (`sbmig/core/config.py`, `config/default.yml`, `knowledge/recipes/catalog.yml`)
- `openrewrite.recipes.spring_security_7` default = `UpgradeSpringSecurity_6_0`, `_6_1`, `_6_2`, `_6_3`, `_6_4`, `_6_5`
  (`org.openrewrite.java.spring.security6`), `UpgradeSpringSecurity_7_0` (`security7`) — in rewrite-spring 6.37.0 `6_0`
  chains down to `5_8` → `5_7`, `6_1` … `6_5` and `7_0` do NOT include their predecessor, hence the list. The planner already
  adds the set only when the analyzer found `spring-security` (`spring_legacy` only; Boot projects get it through the
  umbrella). Catalog: entries for the six `security6` ids (gate `has_spring_security`, `sets: [spring_security_7]`), `7_0`
  listed in that set. `spring_framework_7` stays empty (not asked).

### Fixture
`fixtures/projects/legacy-external-parent/` (e2e): a Spring Framework 6 jar app whose as-is `<parent>` is the local stand-in
`com.company.legacy:legacy-parent:1.0.0` (`fixtures/libs/legacy-parent`, installed by `fixtures/libs/install.sh`) managing
`org.apache.commons:commons-text:1.10.0` and `com.company.legacy:legacy-web-lib:2.0.0`; the project declares both without a
version and uses them. Expected: `restructure` ok, both pinned in `<name>-back/pom.xml` (the Boot 4.1 BOM manages neither),
BUILD SUCCESS, ≤ 1 LLM call (the review), Handover "carried over".

### As built (2026-10-30)
- Restructure: `_pin_asis_versions` runs before `_drop_managed_versions` at its four call sites (both `_layout` passes,
  `_boot_pom`, `_msa`); the new pom text comes from `PomEditor.set_ref_version`. **A pin is kept only when the new chain's
  effective pom could be captured after pinning** (`_settle_asis_versions`): no capture (Maven missing / failing) → every pin
  of this pass is undone and noted — otherwise a `boot_pom` / `msa_pom` run would keep Boot 2.7 starters at the old version;
  the loop then fills what Maven itself reports missing. A pin the new chain manages (gte) is emptied again (a note, not a
  `managed_versions_dropped` row); `${…}` versions, non-jar types / classifiers and artifacts managed by the project's own
  pom chain are left alone; "unknown" counts only dependencies the as-is poms declared (the generated boot module's starter
  and OpenRewrite-renamed coordinates are not unknowns). Second runs are idempotent (a pinned-then-emptied pom is byte-identical
  and leaves `changed_files`). Journal `versions_pinned` also carries `managed_by_new_chain` and `undone`. The orchestrator passes
  `effective_pom` for the three modes whatever `drop_managed_versions` says (with `off` nothing explicit is dropped, pins are
  still checked). Error-list row: `version carried over from the as-is build: g:a <v>`; `sbmig/review/inputs.py:restructure_lines`
  shows both lists to the structure review; the Handover marks a pin ⚠ carried over when it equals the merged as-is effective
  version (per-module differences show as plain version changes).
- Fix loop: `VersionResolver` (both captures from the run dir, the loop's repository lookup, cached per key) and
  `_fill_versions` before the commit in `_apply_edits` / `_run_script`; "added by the attempt" = `g:a` absent from
  `ws.file_at(pre, path)`'s `<dependencies>`. Extra safety rules: step 1 also counts `<dependencyManagement>` of the pom and its
  in-project parents, and the declaring module's managed list wins over the merged one; a pre-existing version-less dependency is
  filled only when this round's build reported its version missing (otherwise the build already resolved it); a version the
  attempt removed comes back from the as-is build only when the capture exists; new poms and poms whose parent / BOM imports the
  attempt changed are left alone. The zero-LLM try (`TrySource` gained `"sbmig-version"`) hooks into `_handle_case` right after
  the triage decision, before the forced-block check and the deferral; it runs when the decision is SCRIPT or when triage
  blocked only for "LLM unavailable / budget used up" (a person's pinned block is respected), once per (pom or module, `g:a`)
  per run; it is NOT a ladder step (`_previous(ladder=True)` leaves it out of the LLM-try counts and the blocked policy, the LLM
  still sees it under "Previous attempts"); a partial try is not rolled back before the case's next try (the pins it found
  stay); commit `fix(sbmig-version): <cluster id>`; also in `fix.mode: edits`. Prompt facts: no capture → no lines (never
  "not declared"); a version found only among the as-is direct dependencies is labelled `(declared)`; ≤ 12 artifacts per case;
  the edits prompt omits "answer `<no_script>`" (that token is banned there) and both prompts explain the `as-is build:` /
  `new parent chain:` lines in one sentence.
- Known limit: without the post-restructure capture (it fails while any version is still missing) an LLM-added dependency is
  filled from the as-is build or the repository even if the new parent manages it — the structural-regression rule would
  otherwise revert the whole try on the next build. A re-capture after the first zero-LLM try would close this; not done.
- Recipes / fixture as in the contract; the catalog already had entries for `security6.UpgradeSpringSecurity_6_0 … 6_5`
  (round 10), only their `sets:` changed.
- After the review (2026-10-30, built): in the restructure an entry the as-is capture does not resolve is pinned from the
  repository BEFORE the after-capture (`_repository_release`, one `RepositoryLookup` per call, newest non-prerelease release;
  row `version chosen from the repository: g:a <v>`, marker `← repository newest release (the as-is build did not resolve it)`,
  a follow-up asking the owners to confirm it; such a pin is emptied whenever the new chain manages the artifact at ANY version,
  and goes back to "unknown" when undone); the merged as-is view is read managed → direct for version-less entries (per-module
  entries direct → managed); no Maven capture when nothing was pinned; a carried-over pin newer than the managed version is
  reported as `carried-over version newer than the parent-managed one kept: …`. In the loop the zero-LLM try runs after the
  round's deferral and a `_version_deferral` (another case's fix changed or ran on that pom / the root pom this round → wait);
  `asis_fact` labels a `direct` hit "(declared)" and a `managed` hit "(managed by <parent>)" and reads module direct → module
  managed → merged managed → merged direct; a (pom, g:a) pair is remembered only once its edit applied (a pair where nothing
  was found is remembered at once), with at most `VERSION_TRY_APPLY_FAILURES` = 3 failed applies per pair.
- Review follow-ups (2026-10-30, not changed): for a dependency the attempt ADDED, `_fill_versions` takes the repository's
  newest non-prerelease release of any major (no as-is major exists to stay on; the Boot BOM covers the common libraries first) —
  the policy for new dependencies; the restructure and the loop keep their own chain / protected-key / as-is lookup helpers
  (`Restructurer._chain` / `_protected_keys` / `_asis_version` vs `VersionResolver`), so the parent named in "managed by …" can
  differ between the Handover and the prompt facts; the pinned-then-emptied idempotency check compares raw bytes, so a
  whitespace-only re-serialisation can still list a pom as changed.

## v2.1m — binary files under Maven resource filtering: `nonFilteredFileExtensions` written by sbmig, not by the LLM (2026-10-30)

Company failure (user, 2026-10-30): with an empty KB a project whose filtered resources hold binary files (`.dmdb` and other
company extensions, webfonts, archives) failed in maven-resources-plugin's copy (`MalformedInputException: Input length = 1`,
`filtering <f> to <t> failed with …`); the LLM wrote three different scripts for the same error and the case hit the cap. The
accumulated KB had masked it (a stored script from an earlier project). v1 fixed this pre-emptively with handler E12
`resources-binary-filtering` (its docstring already names `.dm/.items.dmdb`), deleted in v2 step 4; v2 only showed the review a
fact line. v2.1m puts the deterministic rule back, inside the restructure, and keeps a reactive zero-LLM try in the loop.

### Shared rule (`sbmig/restructure/resources_filtering.py`, new)
- `binary_resource_plan(ws, cfg, poms) -> ResourcePlan`: uses the analyzer's scan
  (`sbmig.analyze.properties.filtered_binary_resources` — known extensions of `BINARY_RESOURCE_EXTENSIONS` plus files of an
  unknown extension whose CONTENT is binary, includes / excludes honoured) on the CURRENT tree. For every pom that declares a
  `<filtering>true</filtering>` resource (`<build><resources>` / `<testResources>`; `<webResources>` of maven-war-plugin → the war
  plugin too) the plan lists the extensions to write: the extensions of the binary files found under that pom's filtered
  directories + `build.non_filtered_extensions` (company additions, always written when the pom filters anything) — never the
  plugin's own defaults (`jpg jpeg gif bmp png`), never `TEXT_RESOURCE_EXTENSIONS`. A filtered binary file WITHOUT an
  extension → `plan.followups` ("exclude it from filtering or rename it"). A module whose filtering comes only from a parent
  outside the tree is not in the plan (nothing declares it) — the reactive try covers it.
- `apply_non_filtered_extensions(ws, plan, *, poms=None) -> list[str]` (changed poms): through `PomEditor` (never text
  surgery): the `<build><plugins>` declaration of `org.apache.maven.plugins:maven-resources-plugin` (created without a version
  when absent — the parent manages it; an existing `<pluginManagement>` entry is extended instead when the pom has no
  `<build><plugins>` one and is not a module of a role parent) gets `<configuration><nonFilteredFileExtensions>` with the missing
  `<nonFilteredFileExtension>` entries appended (existing ones kept, plugin-level configuration only — an execution's own
  `<configuration>` is left alone); same for maven-war-plugin when `<webResources>` filter. Idempotent.
- `reactive_targets(ws, errors) -> list[str]`: for a build error of maven-resources-plugin / maven-war-plugin (message matching
  `MalformedInputException|UnmappableCharacterException|Input length = \d+|filtering \S.* to \S.* failed with`) the pom of the
  module the error names (`on project <artifactId>` / the file path), else every pom declaring filtering; the plan for those
  poms also takes the extension of the file the message names, even when the scan did not flag it.

### Restructure (`sbmig/restructure/restructurer.py`)
- `_non_filtered_extensions(c)` runs in `_layout` (both passes), `_boot_pom` and `_msa` right before `_pin_asis_versions`,
  i.e. after the module poms are final; `RestructureResult.non_filtered_extensions: list[str]`
  ("<pom>: ext1, ext2 → maven-resources-plugin[, maven-war-plugin]"); a pom-verify row per pom
  (`nonFilteredFileExtensions added to maven-resources-plugin in <pom>: ext1, ext2`), one note, journal
  `{"event": "non_filtered_extensions", "poms": {...}, "followups": [...]}`; the structure review's fact line stays (it now
  reports what is left, normally nothing).
- Config: `build.non_filtered_extensions: list[str] = []` (company additions such as `dmdb`; lower-case, no dot; also accepted
  as `SBMIG_BUILD__NON_FILTERED_EXTENSIONS='[dmdb, xyz]'`).

### Fix loop (`sbmig/loop/fixloop.py`)
- A case whose errors match `reactive_targets` is tried first WITHOUT the LLM, like `sbmig-version`: `TrySource`
  `"sbmig-resources"`, edits = the plan's pom changes applied through the same path as `_apply_edits` (guards, commit
  `fix(sbmig-resources): <cluster id>`, scoring, rollback), after the round's deferral, once per (pom, extension set) per run,
  zero LLM calls; nothing to add → the normal ladder. The prompt's facts for such a case name the files the scan found.

### Fixture
`fixtures/projects/filtered-binary-resources/` (e2e): a Spring Framework 6 jar app, `src/main/resources` filtered
(`application.properties` with `@project.version@`), holding `fonts/app.woff2` (known extension), `data/items.dmdb` (unknown
extension, binary content with NUL bytes) and `data/README.txt`. Expected: `<name>-back/pom.xml` gets maven-resources-plugin
`nonFilteredFileExtensions` `dmdb, woff2`, BUILD SUCCESS with 1 LLM call, `file_same_as` on both binaries in `target/classes`,
the properties file still filtered (`file_contains` the version).

### As built (2026-10-30, restructure part)
- `sbmig/restructure/resources_filtering.py`: `ResourcePlan` (pydantic; `.empty`, `.lines(only=)`), `binary_resource_plan(ws, cfg,
  poms=None, *, errors=None)` — with `errors=` it is the reactive plan (only `reactive_targets` poms, the named file's extension
  added unless it is a text extension, only the plugin that failed extended; a module whose filtering comes from a parent outside
  the tree is scanned on `src/main/resources` as if filtered), `apply_non_filtered_extensions(ws, plan, *, poms=None)`,
  `reactive_targets(ws, errors, poms=None)`; none of them raises.
- Plugin element: (1) an existing `<build><plugins>` declaration, else (2) an existing `<pluginManagement>` declaration when the pom
  has no parent or an in-tree parent (any outside parent counts as a role parent, base-parent on the root included), else (3) a new
  `<build><plugins><plugin>` without a version whose `<nonFilteredFileExtensions>` carries `combine.children="append"` so a list an
  outside parent already has is kept (existing lists do not get the attribute). Plugin-level configuration only; existing entries
  kept (case-insensitive), missing ones appended. maven-resources-plugin for `<resources>` / `<testResources>` filtering (profiles
  and copy-resources included), maven-war-plugin for `<webResources>`.
- Which pom: the one declaring the filtering; the plan moves up to an in-tree parent while that parent filters, the child has no
  plugin-level list of its own and the parent's plugin is not `<inherited>false`; a module on a role parent gets its own entry; a
  child without `<resources>` of its own is scanned with the parent's `<resources>` against its own directory.
- `BuildConfig.non_filtered_extensions` lower-cases, strips the dot and accepts a plain string; `_non_filtered_extensions(c)` runs
  before `_pin_asis_versions(c)` at the four call sites; follow-ups for extension-less files are capped at 10 + a summary line;
  `sbmig/review/inputs.py:filtered_binary_lines` now lists only what is still uncovered (`binary_resource_plan(...).files`).
- Fixture note: in layout mode `application.properties` moves to the boot module (the boot parent filters it); the filtered text
  that proves filtering stays on is `data/catalog.properties` in the back module.

### As built (2026-10-30, fix-loop part)
- The two zero-LLM pre-tries share one path: `_pre_tries()` (a registry of source → "which poms" → run), `_pre_try_for`,
  `_pre_try_deferral(st, poms)` (was `_version_deferral`), `_pre_attempt` (decision, attempt, `_begin_try`, `_apply_edits`,
  tried-key settlement, deferral bookkeeping, recording); `_apply_edits` takes an optional `apply_fn` — `apply_non_filtered_extensions`
  writes the poms itself and runs in place of the edit applier with the same failure handling, commit, scoring and rollback. Tried
  keys and the 3-failed-applies cap are shared, keyed per source; `_previous(ladder=True)` and the keep-a-partial-try rule cover both
  sources. Hook: the `_handle_case` slot of `sbmig-version` (after the round's deferral and the pre-try deferral).
- Resources: a cheap regex on the error text, then `reactive_targets(ws, errors)` non-empty; plan `binary_resource_plan(ws, cfg,
  errors=…)`; only (pom, extension set) pairs not tried yet are applied; `plan.followups` → `att.notes` + the loop's follow-ups
  (Handover); a plan with nothing to add is journaled once per case, not remembered, and the ladder continues at once; an unwritable
  pom shows as "no file changed" (`apply_failed`, counts toward the cap). Prompt facts when the pre-try could not apply: one
  `binary resources under filtering: <plan line> (nonFilteredFileExtensions)` line per pom, then `binary file under resource
  filtering: <file>` for up to 10 files. `TrySource` gained `"sbmig-resources"`.

## v2.1n — optional in-house artifacts applied when a trigger dependency is present (2026-10-30)

User: "옵셔널로 넣는 artifacts.yml 의 의존성에 대해, pom.xml 에 그에 해당하는 의존성이 있으면 그 옵셔널을 적용하는 완화책" — the 8
optional artifacts are the company's curated replacements (e.g. `hibernate-core` → the company's JPA starter); today an optional
is only version-aligned when the project already declares it (S6: nothing arrives transitively), so a project that still declares
the raw library never gets the curated one and the LLM has to discover the swap. Both stay: alignment of what is used (to the 4.0.0
line, `optional_version_policy` floor | pin) AND, new, introduction of the optional when one of its triggers is declared.

### Config (`sbmig/core/config.py: ArtifactSpec`, `config/artifacts.example.yml`)
- `when_present: list[str] = []` — `g:a` keys or globs (`org.hibernate*:hibernate-core`) of as-is dependencies that call for this
  optional artifact. Matched against the project's own poms' `<dependencies>` (any scope; `dependencyManagement`-only entries do
  not count — nothing is used).
- `replaces: bool = True` — the matched declarations are removed when the optional is added (the optional / its BOM brings the
  library); `false` keeps them (the optional is added next to them).
- `aliases` keep their meaning (an old key of the SAME artifact → renamed, not a trigger).

### Restructure (`sbmig/restructure/restructurer.py`, all modes through `_all_poms_pass`)
- New `_apply_optional_triggers(c, eds, editable)` runs right before `_bump_optionals(c, eds, editable)`: for every optional spec with
  `when_present`, for every editable project pom whose `<dependencies>` match a trigger: if the optional's key is not declared in
  that pom (its own `<dependencies>`; a declaration in an in-repo ancestor counts too), declare it there — version per the spec
  (`version: null` → no `<version>`, the parent manages it; else the configured version), `scope` / `type` per the spec, else the
  matched declaration's scope; with `replaces: true` remove the matched `<dependencies>` entries of that pom (their `<exclusions>` go
  with them) and, when nothing else references it, the version property they used. Idempotent (a second run changes nothing).
  `_bump_optionals` then aligns the version like any declared optional.
- `RestructureResult.optional_added: list[str]` ("g:a → <pom> (for <matched g:a>[, …])") and `optional_replaced: list[str]`
  ("<matched g:a> removed from <pom> → g:a"); a pom-verify row per pom (`optional <g:a> added for <matched> in <pom>`), one note,
  journal `{"event": "optional_triggers", "added": [...], "replaced": [...]}`; the Handover's "added" / "removed" lists carry the
  reason `optional (trigger: <matched g:a>)` (`sbmig/report/handover.py`: a new `REASON_OPTIONAL_TRIGGER`); the structure review sees
  the lines next to `optional_bumped`.
- MSA library roots: triggers apply there too (they are pom-only passes), mandatory artifacts still do not.
- Never on the generated front / boot poms unless they themselves declare a trigger (they normally do not).

### Fixture
`legacy-external-parent` gains a trigger scenario in its `sbmig.e2e.yml` only (no new fixture): `artifacts.optional` adds
`{ group_id: org.apache.commons, artifact_id: commons-lang3, version: 3.17.0, when_present: ["org.apache.commons:commons-text"],
replaces: false }` — a public stand-in: the project declares commons-text, so commons-lang3 3.17.0 is added next to it;
expected `pom_dependency_present` commons-lang3 in `extparent-back/pom.xml`, `optional_added` in the journal, BUILD SUCCESS.
(The real company mapping — e.g. `org.hibernate*:hibernate-core` → the company JPA starter with `replaces: true` — goes into the
company artifacts yml; a `replaces: true` unit test covers the removal.)

### As built (2026-10-30)
- `ArtifactSpec.when_present` / `replaces` (a single string is accepted; `aliases` too), `ArtifactSpec.triggered_by(key)` (exact
  key or `fnmatchcase` glob, never the optional's own key or an alias). `_apply_optional_triggers(c, eds, editable)` runs inside
  `_all_poms_pass` right before `_bump_optionals` (every mode, MSA library roots included; `_all_poms_pass` precedes
  `_pin_asis_versions` / `_drop_managed_versions` in all three modes, and an added optional is a protected key for both).
- Decisions: an optional already declared in an in-repo ancestor → the pom is skipped entirely (with `replaces: true` the child's
  trigger then stays); scope = the spec's, else the first trigger's (`system` / `import` dropped → no scope); a version property is
  removed only from the trigger's own pom or its in-repo ancestors and only when no project pom still references it; own reactor
  modules and `${…}` coordinates never match; the optional is appended at the end of `<dependencies>`.
- Reporting: `RestructureResult.optional_added` / `optional_replaced`; pom-verify row `optional <g:a> added for <matched> in <pom>`;
  the `restructure` journal event carries both lists; `restructure_lines` shows them after "optional bumped"; Handover
  `REASON_OPTIONAL_TRIGGER = "optional (trigger: {matched})"` (Korean entry in `sbmig/report/i18n.py`, filled reasons translated
  through `handover.tr_reason`) on the added optional and the removed trigger, parsed from the restructure note like the bumps.
- `legacy-external-parent` stop-after-restructure: `extparent-back/pom.xml` = spring-context (managed), commons-text 1.10.0,
  commons-csv 1.10.0, commons-lang3 3.17.0 (added, trigger commons-text); journal `optional_triggers`; Handover "Added …
  (optional (trigger: org.apache.commons:commons-text))".

## v2.1o — no build before the restructure, and the Boot BOM's facts without Maven (2026-10-30)

User (company runs, 2026-10-30): migrated poms still showed Boot `4.0.9` (what the OpenRewrite umbrella writes: `4.0.x` = the
newest 4.0 patch in Nexus) or the as-is `2.5.15`, although the company root parent carries `spring-boot.version=4.1.0` /
`spring-framework.version=7.0.8`; and "Boot 레시피 실행 다 하고 구조화를 다 한 후에 빌드를 시작해야 맞을 거 같다". Two causes, two changes:
(1) the one `mvn clean compile` between the rewrite and the restructure (v2.1) runs on a half-migrated tree (Boot 4.0.9 parent /
starters, company parent not yet applied) and only adds noise to the error list; (2) the cleanup that removes explicit Boot / Spring
versions and the as-is version PROPERTIES that override the parent's (`spring-boot.version=2.5.15` in a module pom beats
`base-parent`'s 4.1.0 — Maven property inheritance) depends on the post-restructure `help:effective-pom` capture; when that capture
fails at the company nothing is cleaned and the old versions win.

### (1) `plan.compile_after_rewrite: bool = false` (`sbmig/core/config.py: PlanConfig`, `config/default.yml`, `sbmig/plan/planner.py`)
- `false` (new default): no `transform.compile` step — the flow is rewrite → restructure → review → `verify.build`; the error list
  starts with the restructure's pom-verify rows and the first build. `true`: the v2.1 compile step as before (single, MSA root, per
  service). Plan note either way. Nothing else changes (the error list, moved-file matching and the KB handle an empty compile stage).

### (2) Boot BOM facts (`sbmig/build/boot_bom.py`, new) and their use in the restructure
- `BootBomFacts(version, managed: dict[g:a, version], properties: dict[name, value], sources: list[str])` from
  `org.springframework.boot:spring-boot-dependencies:<cfg.target.spring_boot_version>` — the pom from the local repository
  (`<local repo>/org/springframework/boot/spring-boot-dependencies/<v>/*.pom`), else `RepositoryLookup(cfg).pom_text(...)`;
  `${…}` resolved from the BOM's own properties; its `<scope>import</scope>` BOMs (spring-framework-bom, spring-security-bom,
  spring-data-bom, jackson-bom, hibernate … ~45) fetched the same way ONE level deep so their managed keys count too. Cached per
  process (one dict per version); never raises; `BootBomFacts.empty` when nothing could be read; `as_capture()` returns the
  `load_effective` shape (`managed`, `properties`, `by_module` = {}) so the existing code can take it in place of a capture.
- Fallback for the managed-version steps: when the post-restructure capture is None (Maven missing / failed) and
  `artifacts.parent_manages_spring_boot` (the parent chain is promised to carry this BOM) OR the project imports the BOM itself
  (`parent_manages_spring_boot: false` — the restructure wrote it), `_drop_managed_versions` and the v2.1l pin step use
  `BootBomFacts.as_capture()` instead of giving up: pins the BOM manages are emptied, the others stay (no undo); explicit
  versions the BOM manages are dropped (gte as before). A note says which source decided (`effective pom` / `Boot BOM facts` /
  `none → undone`). With a real capture nothing changes.
- New `_drop_bom_property_overrides(c)` (all three modes, right after `_drop_managed_versions`): when
  `parent_manages_spring_boot` (parent inheritance — an imported BOM is not affected by the importer's properties), every
  `<properties>` entry of a project pom whose NAME is a property of the Boot BOM (`spring-boot.version`, `spring-framework.version`,
  `spring-security.version`, `hibernate.version`, `jackson-bom.version`, `tomcat.version`, …) is removed, unless a project pom still
  references `${name}` after the version drops (then it stays and is noted: `property X kept: still referenced by <pom>`).
  `RestructureResult.bom_properties_removed: list[str]` ("<pom>: name=value"), one pom-verify row each
  (`property <name>=<value> removed: it overrides the Spring Boot <ver> BOM's <name>`), one note, journal
  `{"event": "bom_property_overrides", "removed": [...], "kept": [...], "source": "effective pom|Boot BOM facts"}`. The source of the
  property-name set is the post-restructure capture's `properties` when present (it already contains the parent chain's), else the
  BOM facts. `layout.drop_bom_property_overrides: bool = true` switches it off.
- The structure review's "property of the same name as the parent's" critical finding stays as the last line of defence.

### Fixture
`spring5-security-batch` (as-is `spring-batch.version`, `spring-security.version`, `spring.version` overriding the Boot parent's): the
restructure now removes them (`bom_properties_removed` 3) → the review finds no critical issue (expected: `critical: 0`,
`script_status: none`, one LLM call fewer); the rest of the subset unchanged.

### As built (2026-10-30)
- (1) `PlanConfig.compile_after_rewrite = False`, `config/default.yml` `plan.compile_after_rewrite: false`; the planner skips
  `_compile` at its three call sites and adds `compile: no build before the restructure (plan.compile_after_rewrite=false, v2.1o) —
  the error list starts with the restructure's pom corrections and the first verify build`; `tests/plan/conftest.py` keeps the v2.1
  order under test (autouse `compile_after_rewrite = True`), `test_default_plans_no_compile_before_restructure` covers the default.
- Also in this increment (user, 2026-10-30, "넣는 게 좋을 것 같아"): `openrewrite.recipes.spring_framework_7` default =
  `UpgradeSpringFramework_7_0` for `spring_legacy` (MSA library roots too); catalog `sets: [spring_framework_7]`; a non-Boot project
  is made Boot by the restructure and the Boot 4.1 BOM brings Spring 7.0.8 / Jackson 3, so the Framework 6.x removals, Jackson 2 → 3
  and the recipe's other code changes no longer fall to the LLM (Boot projects already ran it inside the umbrella). `junit5` stays
  empty. Reminder of the order argument: OpenRewrite's type-attributed recipes only recognise the OLD types on the as-is classpath,
  so recipes run BEFORE the restructure; the restructure then removes what they leave in the poms.
- (2) `sbmig/build/boot_bom.py`: `BootBomFacts(version, managed, properties, sources)`, `.empty`, `as_capture()`,
  `load_boot_bom_facts(cfg, version=None)`, `clear_cache()`; local repo (`build.local_repo_path`) then `RepositoryLookup.pom_text`;
  the 46 imported BOMs one level deep (Boot's own entries win, then the imports in order); ≤ `MAX_POMS` = 60 poms; the real 4.1.0 BOM
  gives 1,872 managed keys and 196 properties in ~40 ms; only successful loads are cached (per version + local repo). Restructurer:
  `_boot_bom_facts(c)`, `_bom_importers(c)`, `_bom_capture(c)`, `_drop_bom_property_overrides(c)` (after `_drop_managed_versions`
  at the four call sites), shared `_referencing_poms` (also used by `_drop_unused_properties`); `_drop_managed_versions` and the pin
  settlement fall back to the facts when the capture is None — with `parent_manages_spring_boot: false` only for poms that import
  `spring-boot-dependencies` (or inherit from one in the repo); a run with a real capture gets the note `managed-version source:
  effective pom of the new parent chain`, the undo note ends "(and no Boot BOM facts)". `LayoutConfig.drop_bom_property_overrides`,
  `RestructureResult.bom_properties_removed`, pom-verify row, the `restructure` journal event carries the list.
- Deviations: the property NAMES always come from the BOM facts, never from the capture's `properties` (those include each project
  pom's own properties, so everything would count as an override), hence journal `source` is always `Boot BOM facts` and the step
  also runs with a real capture; `spring.version` is not a Boot BOM property (`spring-framework.version` is) — it is handled by the
  "unused after the version drop" rule (`RestructureResult.version_properties_removed`, see below once built); `tests/conftest.py`
  gained an autouse `_no_boot_bom_facts` so the undo / snapshot tests do not silently read this machine's `~/.m2` BOM.
- Dead version properties (built after the review of the fixture shape): `_drop_dead_version_properties(c, eds, refs)` runs inside
  `_drop_managed_versions` (every mode): every `${prop}` that an explicit version the drop step removed had referenced is removed
  from the pom that defines it (itself or an in-repo ancestor) when `_referencing_poms` finds no remaining `${prop}` in any project
  pom; names the override step owns (Boot BOM property names, when that step is active) are left to it.
  `RestructureResult.version_properties_removed` ("<pom>: name=value"), pom-verify row `version property <name>=<value> removed: no
  dependency references it after the parent-managed versions were dropped`, one note, in the `restructure` journal event and the
  review's restructure lines. This reverses the round-9 "properties stay" rule (`tests/restructure/test_managed_versions.py` updated:
  `jaxb.version` is now removed with its only user). In the `spring5-security-batch` shape all three as-is properties go:
  `spring-security.version` / `spring-batch.version` through the BOM override step, `spring.version` through this rule; a
  `junit.version` used by a test-scoped, unmanaged junit stays. The Handover's "Spring / Boot after" read the final poms' own
  properties, so nothing else changed there.
- `layout.parent_property_names` (default `["spring-boot.version"]`): names the company parent chain defines by convention (not Boot
  BOM properties) that project poms must not override; `_override_name_sets(c)` returns the BOM names (need
  `parent_manages_spring_boot` + readable facts) and the parent names (need neither — they apply with
  `parent_manages_spring_boot: false` too, harmless because a still-referenced property is never removed); both behind
  `layout.drop_bom_property_overrides`; one note per origin, the journal `source` names only the origins that matched; pom-verify
  wording `property <name>=<value> removed: it overrides the in-house parent's <name>` for parent names (a name in both lists counts
  as a parent name). The company lists its own names (e.g. `spring.version` when base-parent uses it).
- Recipe-raised versions (found by the full e2e, `legacy-external-parent`: the Framework 7 recipe bumped `spring.version` to 7.0.10 while
  the Boot 4.1 BOM manages 7.0.8 and gte kept it): in gte mode an explicit version newer than the managed one is dropped when the
  as-is build's version of the artifact (`_asis_version`, through the shared `_asis_capture(c)`) was at or below the managed one or the
  artifact was absent as-is — the newer value came from a recipe, not from the owners; kept (with the "kept newer" note) when the
  as-is project itself was above the managed version, when there is no as-is capture or when the versions cannot be compared.
  `RestructureResult.recipe_versions_dropped` ("g:a <explicit> (as-is <v | ->, managed <m>, <pom>)"), note `recipe-raised version
  dropped: …`, pom-verify row `explicit version dropped: g:a <explicit> → managed <m> (raised by a recipe from the as-is <v>)` /
  `(added by a recipe: not in the as-is build)`, in the `restructure` journal event and the review lines; the dead-property rule then
  removes the unreferenced property.
- Fixture expectations changed by v2.1m / v2.1o: `spring5-xml` (the Framework 7 recipe converts `WebMvcConfigurerAdapter` /
  `HandlerInterceptorAdapter` itself → 0 scripts, 1 LLM call), `servlet-nonstandard-dirs` (the restructure writes `nonFilteredFileExtensions`
  → the review replay no longer reports a critical issue, no script), `boot2-qlrm-javax` (the replayed fix answer now carries the
  `<verify>` block rule 14 requires for a library replacement; without it the v2.1d guide call made 3 LLM calls).

## v2.1p — the company parent chain's facts come from its poms, not from a fixed list (2026-10-30)

User: "layout 을 parent pom 파일에서 읽어 와서 세팅해야 하는 거 아냐? parent 버전이 올라가면서 프로퍼티에 뭐가 추가될지 모르는데 고정으로
박으면 이상하지 않아?" — right. v2.1o read the Boot BOM's facts dynamically but took the in-house parent's own property names from
`layout.parent_property_names`. The configured company parents are poms in the same repository, so they are read the same way.

### `sbmig/build/parent_chain.py` (new; `boot_bom.py` becomes its Boot-only special case or is folded in)
- `ParentChainFacts(roots: list[str], managed: dict[g:a, version], properties: dict[name, value], sources: list[str])` and
  `load_parent_chain_facts(cfg) -> ParentChainFacts`: for each configured parent (`artifacts.parent`, `boot_parent`, `comp_parent`,
  de-duplicated) read its pom (local repository → `RepositoryLookup.pom_text`), then follow `<parent>` upwards and every
  `<scope>import</scope>` BOM (the BOM's own imports one level deep, as in `boot_bom.py`); `${…}` resolved from the poms' own properties
  (child-most pom wins for a name defined twice); bounded (≤ 80 poms), cached per process per (parent GAVs, local repo), never raises,
  `.empty`, `as_capture()` in the `load_effective` shape. When `parent_manages_spring_boot` and the chain does not reach
  `spring-boot-dependencies` (an unusual company parent), the Boot BOM of `target.spring_boot_version` is added so nothing is lost
  against v2.1o. `sources` names every pom read and every pom that could not be read.
- The restructure uses it everywhere v2.1o used `load_boot_bom_facts`: the fallback managed source when the post-restructure
  capture is missing, and the property-override names (= every property name the chain defines). `layout.parent_property_names`
  stays as an escape hatch, default `[]` (a name the company wants removed even though no pom of the chain defines it).
- Journal `parent_chain_facts` once per restructure (`roots`, `poms`, `managed`, `properties` counts, `unreadable`); the
  override step's `source` says `parent chain` (or `parent chain + parent_property_names`).
- Local e2e: the configured parents are `spring-boot-starter-parent:4.1.0` → the chain reaches `spring-boot-dependencies` → the same
  facts as v2.1o; no fixture change expected.

### As built (2026-10-30)
- `sbmig/build/parent_chain.py`: `ParentChainFacts(roots, managed, properties, property_sources (name → defining pom), sources,
  unreadable, poms, boot_bom_added)`, `.empty`, `as_capture()`, `load_parent_chain_facts(cfg)`, `clear_cache()`; `boot_bom.py`'s helpers
  are shared and public (`PomSummary` with the `<parent>` coordinates, `parse_pom`, `make_resolver`, `pom_builtins`, `PomReader`,
  `local_repository`, `read_import_boms`), `load_boot_bom_facts` unchanged. Local e2e parent `spring-boot-starter-parent:4.1.0`: 48 poms,
  1,872 managed keys, 202 properties.
- Merge rules: roots = parent, boot_parent, comp_parent (duplicates / versionless skipped); `<parent>` followed by repository
  coordinates only, ≤ 12 hops per root, ≤ 80 poms; properties: the child-most definition wins (its pom recorded), an imported BOM's
  properties are NOT inherited (Maven), with several roots the first wins; managed: the declaration nearest the root parent wins and
  every declared entry beats imported BOMs (which only add missing keys); the Boot BOM is added at the lowest priority only when
  `parent_manages_spring_boot` and no chain pom is / imports `spring-boot-dependencies`; unreadable poms listed, never raised; only
  successful loads cached (roots, local repo, the Boot switch and version).
- Restructurer: `_chain_facts(c)` (once per restructure, journal `parent_chain_facts` with roots, counts, `boot_bom_added`, unreadable),
  `_fallback_capture(c)` replaces `_bom_capture` (with `parent_manages_spring_boot: false` the BOM-importing poms get the Boot BOM plus
  the chain, declared chain entries winning), `_override_name_sets` = chain names ∪ `parent_property_names`;
  `RestructureResult.bom_property_sources`; pom-verify wording by defining pom: `the Spring Boot <v> BOM's <name>` / `the in-house
  parent chain's <name> (<g:a:v>)` / `the in-house parent's <name>` (escape hatch). `layout.parent_property_names` default `[]`.
- Deviations: chain-derived override names are limited to `*.version` properties and exclude `java.version` / `maven.compiler.*`
  (`spring-boot-starter-parent` defines `java.version=17` and `maven.compiler.release` — without the filter the step would delete the
  `maven.compiler.release=21` the restructure writes; escape-hatch names are not filtered); chain names apply whatever
  `parent_manages_spring_boot` says (modules inherit the chain's properties either way); `tests/conftest.py` autouse
  `_no_parent_chain_facts` keeps this machine's `~/.m2` out of the tests.

## v2.1q — embedded Tomcat is the target: wording, and LLM-pinned versions the chain manages (2026-10-30)
- User: "외장 톰캣을 내장 톰캣으로 변환하는 게 목적 중 하나". Verified on the `sibling-aggregator-dup` e2e output: `java -jar dup-boot-1.0.0-SNAPSHOT.war`
  starts the embedded Tomcat 11.0.22 (`spring-boot-starter-tomcat` / `tomcat-embed-jasper` are `provided` → the Boot repackage puts
  them in `WEB-INF/lib-provided`), `/hello` (servlet registered from web.xml) and `/index.jsp` (front jar, `META-INF/resources`)
  answer 200. So the JSP case is an executable WAR, not an external-Tomcat WAR (Spring Boot cannot serve JSPs from an executable
  jar); nothing structural changed. Follow-up wording in `sbmig/restructure/restructurer.py` (EAR context root, overlay WAR, JSP WAR,
  container dependencies note, `SpringBootServletInitializer`) now presents `java -jar` + `server.servlet.context-path` first and the
  external deployment as the alternative; three restructure tests updated; CLAUDE.md company-layout sentence updated.
- Loop (`_fill_versions`): a dependency the attempt ADDED with an explicit literal version that the new chain manages (capture, else
  the parent-chain facts) loses its `<version>` (the company LLM had written `org.springframework:spring-webmvc:7.0.0`, the chain
  manages 7.0.8); pre-existing versions are the restructure's business; journal `version_filled` with `source: managed`, `removed`.
- As built (loop): `VersionResolver` falls back to `parent_chain.load_parent_chain_facts(cfg).as_capture()` when the post-restructure
  capture is missing (through the module attribute, so the test stand-in applies); the removal covers only dependencies the attempt
  added (absent at `pre`), with a literal or `${…}` version, jar type, no classifier, not protected / own-module / `system`; note
  `version removed by sbmig: g:a <explicit> in <pom> (the new parent chain manages it at <v>)`; a version property the attempt added
  only for that dependency is dropped when nothing references it (`_drop_added_property`); pre-existing explicit versions, poms whose
  parent / BOM imports the attempt changed, and unmanaged artifacts are left alone. Side effect: without a capture the chain facts
  also decide whether an added version-less dependency stays empty, whether a dropped version is restored, and the `new parent
  chain: manages …` prompt facts. No version or coordinate is hard-coded anywhere (user: "7.0.0 이 틀렸다고 프롬프트에 하드코딩 안 된다").

## v2.1r — Windows file locks on `target/` are an environment error: retry the build, never ask the LLM (2026-10-30)

Company run (Windows): `Error assembling WAR: Problem creating war: … java.nio.file.FileSystemException: …\target\…\WEB-INF\lib\
wagon-http-shared-1.0-beta-2.jar: 다른 프로세스가 파일을 사용 중이기 때문에 프로세스가 파일에 액세스할 수 없습니다` several builds in a row
(an antivirus / indexer or a lingering JVM holding a freshly written jar); the loop treated it as a case, asked the LLM 15 times,
BLOCKED it (한도 도달), and the final build — lock gone — passed. Two layers:

### Maven runner (`sbmig/build/runner.py: MavenRunner.run`)
- After a failed `_run_once`, when the log matches a file-lock pattern — `java.nio.file.FileSystemException` /
  `AccessDeniedException` / `FileAlreadyExistsException` naming a path under `target` (or `\\target\\`), or the messages
  `다른 프로세스가 파일을 사용 중`, `being used by another process`, `The process cannot access the file`, `Unable to delete`
  with `target` — wait `build.file_lock_retry_wait_s` (default 5) and run the same command again, up to
  `build.file_lock_retries` (default 3) times; log `maven: Windows file lock on <path> → retrying in Ns (k/N)` (Korean first line
  like the other operator lines), journal `{"event": "build_file_lock_retry", "goal", "module", "path", "attempt", "wait_s"}`;
  the result returned is the last run's. Patterns live in `knowledge/config/build_env_errors.yml` (`file_lock:` list of regexes)
  so the company can add its antivirus' wording without a redeploy; the parser falls back to the built-in list when the file is
  missing.
### Triage (`sbmig/triage/blocked.py: _classify_infra`, `sbmig/core/models.py`, `config/default.yml`)
- `BlockReason.FILE_LOCK = "file_lock"`, `loop.blocked_policy.file_lock: 0` → a cluster whose errors match the same patterns is
  BLOCKED at once (no LLM, no script), suggestion (Korean, `sbmig/report/i18n.py` / the triage suggestion text):
  "빌드 산출물(target/)을 다른 프로세스가 잠갔습니다 — 백신 실시간 검사에서 프로젝트의 target/ 를 예외 처리하고, 열려 있는 IDE/탐색기/이전
  java 프로세스를 닫은 뒤 다시 실행하세요"; the final build still runs (build gate) and, as in the company run, usually passes.
- `SBMIG-RESULT … attn=…` gets `infra:file-lock` the way repository failures get `infra:repository` (`sbmig/report/result_line.py`).
### Tests
`tests/build/test_runner_file_lock.py` (a stub `run_logged` failing twice with the Korean / English messages then succeeding →
3 runs, journal rows, result ok; exhausted retries → last failure returned), `tests/triage/test_v21r_file_lock.py` (cluster →
BLOCKED `file_lock`, policy 0, the suggestion), result-line test for `infra:file-lock`.
### As built (2026-10-30)
- `knowledge/config/build_env_errors.yml` (`file_lock:` regexes; built-in fallback `BUILTIN_FILE_LOCK` in the new
  `sbmig/build/env_errors.py`: `file_lock_patterns(cfg)`, `file_lock_hit(text, cfg)` → the locked path / `?` / None, `clear_cache()`;
  bad regexes logged and skipped). Patterns: `java.nio.file.(FileSystemException|AccessDeniedException|FileAlreadyExistsException)`
  naming a `target` path, `다른 프로세스가 파일을 사용 중`, `being used by another process`, `The process cannot access the file`,
  `Unable to delete…target`, `Failed to delete…\target\` (maven-clean-plugin); a lock outside `target/` (e.g. `.m2`) is not one.
- `MavenRunner._retry_file_lock` runs after the first `_run_once` and before the negative-cache retry; a timed-out build is not
  retried; log line Korean first then `maven: Windows file lock on <path> → retrying in Ns (k/N)`; journal `build_file_lock_retry`
  (goal, module, path, attempt, wait_s, log); `sleep_seconds` is a module-level hook for tests. `build.file_lock_retries: 3`,
  `build.file_lock_retry_wait_s: 5`.
- `BlockReason.FILE_LOCK`, `loop.blocked_policy.file_lock: 0`, `_classify_file_lock` checked FIRST in `classify_blocked` (a lock
  hides everything else), evidence = the locked path, `FILE_LOCK_SUGGESTION` in `blocked_registry.compose_suggestion` with the Korean
  text in `i18n.PATTERNS`; `attention_codes` → `infra:file-lock` on a non-green run, and `orchestrator._file_lock_trouble()` flags it
  like `infra:repository` (the fleet re-runs such a project later).
- Regression guard without touching the loop: `errors/entries.py: error_stage()` returns None for a file-lock error, so the loop's
  "new earlier-stage error hides the targeted ones" rule never reverts a good attempt over a lock; a build that failed only on a
  lock scores inconclusive, not regressed.
