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
