# Interfaces — the deployed flow-v2 system

The contracts of the system that ships (branch `flow-v2`): module entry points, models, config keys, files, log / journal lines,
HTTP routes and manifests other parts rely on. The v1 flow lives only in git (tag `company-v1`).

**How it is organised.** "Base contracts" holds what was settled before v2 and is still in force. "v2 — flow-v2" is the v2 flow
itself. After it, one section per increment in the order they were built (`v2.1d` … `v2.1z-af`, `deploy`, `remediations`); other
files and the code cite them by name (`docs/INTERFACES.md "v2.1l"`). A later section refines an earlier one; where they disagree the
later one counts. A section whose content was removed keeps a one-line stub so its name still resolves. Source of truth for every
type: `sbmig/core/models.py`, `sbmig/core/interfaces.py`, `sbmig/core/config.py` (mirrored in `config/default.yml`).

| topic | sections |
|---|---|
| run directory & workspace | Base "Run directory and workspace"; v2.1z-h (`ignored_dirs`) |
| intake / analyze | Base "Intake and analyze", "Externalized configuration"; v2.1j (Ant), v2.1k + v2.1z "Review follow-ups" (rootless trees), v2.1z-c (hollow reactor), v2.1z-d (ADF), v2.1z-h (stray poms), v2.1z-i (bundled jars), v2.1z-z (build extensions) |
| plan | v2 "Plan (L)"; v2.1o (1) (`plan.compile_after_rewrite`); v2.1u … v2.1y / v2.1w (recipe sets) |
| transform (OpenRewrite) | Base "Plan, transform"; v2.1l "Recipes"; v2.1o (Framework 7); v2.1w |
| restructure & layout | Base "Restructure and the company layout"; v2.1g, v2.1h (JSP jakarta), v2.1l / v2.1s (version pins), v2.1m (resource filtering), v2.1n (optional triggers), v2.1o / v2.1p / v2.1t (parent-chain facts, BOM imports), v2.1q (embedded Tomcat), v2.1x (legacy Spring context), v2.1z-j (bundled jars), v2.1z-k, v2.1z-l |
| structure review | v2 "Structure review (R)"; v2.1u … v2.1y (review prompt, invariant check, unsupported issues); v2.1z-h (review guard) |
| verify & script fix loop | Base "Fix-loop invariants"; v2 "Script fix loop (L)", "Script contract (S)", "As built (step 1)"; v2.1d (versions, operator checks), v2.1l / v2.1m pre-tries, v2.1r (file locks), v2.1y (declines, idempotency), v2.1z (ladder caps, case keys), v2.1z-e (jakarta discovery), v2.1z-i, v2.1z-n, v2.1z-y, v2.1z-z, v2.1z-aa |
| cases & KB | v2 intro + "Script KB (K)"; Base "KB plumbing"; v2.1e (learning records), v2.1f (probable fixers), v2.1z (masked case keys), v2.1z-ae / v2.1z-af (several pods on one KB) |
| LLM gateway | Base "LLM client tier and adapter seams"; v2.1z-m (agent gateway, token exchange), v2.1z-n, v2.1z-o, v2.1z-p (case thread), v2.1z-q, v2.1z-x, v2.1z-af (busy wait, auth retries) |
| report & result line | Base "Report and the result line", "Advisories and the Handover"; v2 "As built (step 1)" (`review=`); v2.1d (`major=`); remediations (`remediation=`) |
| fleet | Base "Fleet" |
| web app | Base "Graph engine", "Web app"; v2.1z-w (page, server-chosen configs), v2.1z-ae (queue / slots on the page), v2.1z-af (page re-attach) |
| Job mode (Main / Worker, slots, env pins) | v2.1z-z "k8s Job dispatch mode", v2.1z-ab (kubeconfig, template ConfigMap), v2.1z-ac (Main ↔ Worker contract), v2.1z-ad (end confirmation, ownership, env pins, write-time redaction), v2.1z-ae (parallel slots), v2.1z-af |
| deploy | v2.1z-s (build hooks), v2.1z-t (one Job per request), v2.1z-u, deploy (baked config, container paths), v2.1z-ad "Shipped mode" |
| remediations | remediations |

## Base contracts (in force; written before v2)

The parts of the system whose contract predates flow-v2 and that flow-v2 still runs. Later sections refine them; when a
later section says otherwise, the later section wins.

### Entry points

```python
from sbmig.analyze import analyze_project          # (ws: Workspace, cfg: Config) -> ProjectProfile
from sbmig.plan import build_plan                   # (profile, cfg, kb=None, advisories=None) -> MigrationPlan
from sbmig.restructure import PomEditor             # PomEditor(path)  implements PomEditorLike
from sbmig.restructure import Restructurer          # Restructurer().apply(ws, profile, cfg, mode=...) -> RestructureResult
from sbmig.rewrite import OpenRewriteRunner         # OpenRewriteRunner(ws, cfg, kb=None) implements RewriteRunnerLike
from sbmig.build import MavenRunner                 # MavenRunner(ws, cfg) implements BuildRunnerLike
from sbmig.build import parse_maven_log             # (text, project_root) -> list[BuildError]  (signatures filled)
from sbmig.build import signature_for               # (error) -> tuple[str, str]  (L1, L2)
from sbmig.fix import make_llm_client               # (cfg) -> LLMClientLike  (provider switch, incl. the fake client)
from sbmig.fix import LLMFixer                      # implements FixerLike / ScriptFixerLike
from sbmig.triage import Triager                    # implements TriagerLike
from sbmig.loop import Orchestrator                 # Orchestrator(cfg, ws, ...).run() -> RunReport
from sbmig.report import write_report               # (report, ws) -> dict[str, str] paths
from sbmig.kb import FileKnowledgeBase              # the file KB facade (see v2 "Script KB")
import sbmig.api; sbmig.api.migrate(...)            # embedding entry point (invariant 4)
# sbmig/cli.py: typer app — run | analyze | plan | build | rewrite | parse-log | inventory | llm-probe | repo-probe |
#               serve | clean | remediation-resume | version; sub-apps kb | recipes | fleet | advisories
```

Source of truth for models and seams: `sbmig/core/models.py`, `sbmig/core/interfaces.py`, `sbmig/core/config.py`,
`sbmig/core/workspace.py`. A new field gets a default so nobody breaks.

### Conventions
* Paths inside models are **relative to `ws.root`** with POSIX separators (`Workspace.rel()` / `glob()` / `java_files()` /
  `pom_files()`, `sbmig.core.workspace.posix_rel`); logs / outputs are absolute.
* No `print`; `logging.getLogger("sbmig.<pkg>")`. Console / logging handlers use `errors="replace"` (a cp949 console never
  kills a run).
* Subprocesses: `cwd=ws.root` (or the module dir), captured output, timeout from config, full log to
  `ws.new_log_path(label)`. On Windows mvn / git children start with `CREATE_NO_WINDOW`; a timed-out child is killed with
  its tree (`taskkill /PID <pid> /T /F`).
* Every project mutation goes through `ws.write/move/delete` or a `PomEditor` and is committed by the orchestrator
  (the Restructurer may commit intermediate steps).
* Tests: `tests/<area>/`, pure Python by default; `@pytest.mark.integration` for anything needing mvn / java / network.
  Python ≥ 3.10, type hints, pydantic v2.

### Run directory and workspace (`sbmig/core/workspace.py`)
* Run layout is the module docstring of `sbmig/core/workspace.py` (`<out>/<run id>/project`, `logs/`, `errors.jsonl` /
  `errors.md`, `journal.jsonl`, `sbmig.log`, `profile.json`, `plan.json`, `report.json`, `report.md`, `changes.patch`,
  `result.zip`; `merge-conflicts/` and `web-status.json` when they apply).
* `Workspace.read/write(encoding=None)` detect and keep legacy encodings (`detect_encoding`, `encoding_of`,
  `set_encoding_candidates`; `build.encoding_candidates`); CRLF bytes are kept; zip names re-decoded cp437 → cp949. The
  encoding-detection cache is a thread-safe LRU.
* `Workspace.walk()` prunes `.git` and build output; `Workspace.is_build_output(rel)` / `is_build_output_path(rel,
  root=None)`: a `target` dir is build output only when no `src` component precedes it and a `pom.xml` sits beside it (or
  it holds `maven-status/` / `maven-archiver/`); packages named `target` stay everywhere. `Workspace.changed_between(a, b)`
  uses NUL-separated name lists.
* Generated `.gitignore` (part of `changes.patch` / `result.zip`): `target/`, `!**/src/**/target/`(`/**`), `!/<dir>/target/`
  for every non-output `target` dir, `.sbmig/`, `*.class` and `GENERATED_IGNORES` unless the as-is tree has them. Jars a
  project `.gitignore` hides are force-added to every snapshot (`_force_add_ignored_jars`).
* Workspace git is never interactive: `stdin=DEVNULL`, `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=echo`, `GCM_INTERACTIVE=never`,
  `GIT_EDITOR=true`, and every call passes `-c commit.gpgsign=false -c tag.gpgsign=false -c core.longpaths=true
  -c core.autocrlf=false -c core.quotepath=false -c safe.directory=* -c core.hooksPath=<run dir>/no-hooks`;
  `.git/info/attributes` = `* -text -eol -filter -ident -working-tree-encoding`; `GIT_TIMEOUT_S` (600 s) → `GitError`.
* Windows javac paths (`/C:/…`, `file:///C:/…`, percent-encoded, lower-case drives) are relativized; raw text and build
  summaries are scrubbed in every spelling.

### Intake and analyze
* `sbmig.analyze.detect_build_tool(ws) -> BuildToolDetection(tool, reason)`; `Orchestrator.step_intake` stops a Gradle tree
  (or a tree without pom.xml / build.xml) before the effective-pom capture: flag `unsupported-build-tool:<tool>`, exit 3,
  reason in `manual_followups[0]` (Ant: see "v2.1j"). The fleet treats that flag as a `skipped` project, never retried.
* Rootless trees (`sbmig/analyze/aggregator.py`): `plan_aggregator(ws) -> AggregatorPlan | None`, `aggregator_pom_text`,
  `write_aggregator(ws, plan)` (property `sbmig.project-name` = `PROJECT_NAME_PROPERTY`), `ear_without_modules(ws)`;
  `layout.generate_aggregator` (true). `Orchestrator._intake_layout`: EAR alone → flag `ear-without-modules`, exit 3;
  plan → written + committed, flag `aggregator-generated`, journal `aggregator_generated` (refined by "v2.1k").
* Effective dependency management (`sbmig.build.effective`): `capture_effective_depmgmt(ws, cfg, module=None,
  name=DEPMGMT_FILE)`, `load_effective`, `effective_version`, `effective_pom_provider(ws, cfg, module=None,
  name=AFTER_RESTRUCTURE_FILE)`.
* Classpath facts (`sbmig.build.classpath`): `module_classpath`, `locate_class`, `locate_type_owner`, `owner_fqcn`,
  `type_mismatch`, `gav_for_jar`. Dependency tree (`sbmig/build/deptree.py`): `dependency_tree(ws, cfg, module=None,
  includes=())` (cached under `logs/`, never raises; journal `deptree`), `parse_tree`, `paths_to`, `is_javax_api`,
  `javax_api_inventory(ws, cfg, profile=None) -> list[JavaxApiJar]`.
* javax API inventory at finalize: flag `javax-api-on-classpath`, report section "javax API jars still on the
  classpath", `RunReport.javax_api_jars`, `BlockedItem.via` / `via_direct` (`report.javax_api_inventory: true`).
  Knowledge `knowledge/libraries/jakarta_libraries.yml` via `sbmig/core/jakarta_libraries.py` (`jakarta_libraries()`,
  `jakarta_library_for(key)`, `JakartaLibrary.classifier`).
* Pom repositories: `PomInfo.repositories` (`sbmig.analyze.pom_parser.repository_urls`), `build.pom_repositories: keep |
  remove` (keep), `sbmig.build.mvn.settings_catch_all_mirror(cfg)`, `repository_notes(poms, cfg)` (plan notes).
  Template `config/settings.nexus.example.xml`.
* Source scan (`sbmig/analyze/source_scanner.py`, `scan_sources(ws, poms=None)`): `SourceStats.jsp_paths`,
  `web_static_dirs`, `web_static_count`, `webapp_dirs` (default `src/main/webapp` + maven-war-plugin `warSourceDirectory` /
  `<webResources>`), `large_media_files` / `large_media_bytes`; helpers `web_static_root`, `WebDir`, `pom_web_dirs`,
  `pom_build_dirs`, `clean_pom_dir(raw, allow_parent=False)`, `webapp_dirs(ws, poms)`.
* `ProjectProfile.boot_modules` (`sbmig.analyze.classifier.boot_module_findings`): module dirs whose basename matches
  `layout.boot_module_pattern` AND whose pom has spring-boot-maven-plugin or a non-test `@SpringBootApplication`.
* web.xml (`sbmig/analyze/webxml.py`): `parse_web_xml(text, path) -> WebXmlModel` (namespace-agnostic, never raises),
  `SourceStats.web_xml_*`, `ScanResult.web_xml_models`.
* `sbmig inventory <dir|zip> [--out] [--config] [--json]` (read-only description of a pom-less tree):
  `sbmig/analyze/webapp_inventory.py` `scan_webapp_tree`, `jar_names()` (`knowledge/libraries/jar_names.yml`),
  `guess_jar`; writer `sbmig/report/inventory.py` `write_inventory`.

### Externalized configuration (placeholders)
* Models `PropertyRef`, `PropertyFinding` (status defined | defaulted | external; `service` in MSA mode),
  `PropertyInventory` (`findings`, `services`, `filtered_resources`, `filtered_binary_files`, `placeholder_files`,
  `placeholders_written`, …); `ProjectProfile.properties`, `RunReport.properties`, `HandoverSummary.properties_to_set /
  properties_guide`. Analysis: `sbmig.analyze.properties.scan_properties(ws, poms, scan, rules, cfg=None, *,
  services=None)` (edge table in its docstring; per-service in MSA).
* Config `externalized` (`enabled`, `sentinel="__SET_ME__"`, `write_placeholders`, `placeholder_file=application.properties`,
  `rules_file=knowledge/config/externalized.yml`, `include_test_refs`, `max_refs_per_key`). Company rules go into the
  rules YAML (`jndi[]`, `context_param`, `system_property.skip`, `boot_defaults`, `guide[]`).
* The restructure writes an idempotent block between `# --- sbmig externalized-config` and `# --- end sbmig
  externalized-config` into `<boot module|service>/src/main/resources/<placeholder_file>`: one `key=<sentinel>` per
  `boot_key` of each `external` finding (a context-param with an as-is value writes that value). Journal `placeholders`.
* Handover block "Properties to set before running" (only when an external finding needs a value); `report.json`
  carries `properties`.

### Plan, transform (OpenRewrite)
* Recipe catalog `knowledge/recipes/catalog.yml` (`plan.catalog_file`) is the single source of recipe ids;
  `sbmig.plan.catalog` (`RecipeCatalog`, `GATES`, `default_catalog`); `sbmig recipes list|show|check [--jars]`
  (`sbmig/plan/jarcheck.py` `check_jars`). `RecipeSetConfig` defaults equal `openrewrite.recipes` of `config/default.yml`
  (drift test `tests/core/test_recipe_set_defaults.py`). Recipe BOM pinned to 3.37.0.
* `openrewrite.exclusions` (default `**/*.jsp, **/*.jspx, **/*.html, **/*.htm` → `-Drewrite.exclusions`).
* `OpenRewriteRunner.run`: retries once on OutOfMemoryError with `openrewrite.oom_retry_jvm_args`; a transfer failure
  (not `Could not find artifact`) waits and re-runs up to `openrewrite.download_retry` times (`download_retry_wait_s`;
  journal `rewrite_download_retry` / `_result`; error prefix `download-retry: `). Literal versions (`4.0.x`,
  `latest.release`) left in a changed pom → one re-run, else flag `rewrite-unresolved-versions` + follow-up.
* `RewriteResult.parse_problems` (files rewrite-maven-plugin could not parse): journal `rewrite_parse_problems`, flag
  `rewrite-parse-problems`, phase note.
* `sbmig.loop.parse_stop_after(value)` is the one validator for `--stop-after` / web `stop_after` /
  `api.migrate(stop_after=)`; values per "v2 — flow-v2".

### Restructure and the company layout (`sbmig/restructure/restructurer.py`)
The modes and module paths are the module docstring of `restructurer.py`: `layout` (non-Boot: root aggregator +
`{name}-boot` / `{name}-back` / optional `{name}-front`), `boot_pom` (Spring Boot as-is, poms only), `msa_pom` (per
service root, also pom-only projects), `optional_bump`. Optional bumps and `remove_when_present` apply in every mode.
* Config `LayoutConfig` (its docstring is the rule): `module_dir` (`""`), `boot_module` `{name}-boot`, `comp_module`
  `{name}-back`, `front_module` `{name}-front` (None = none), `project_name`, `front_static_dir`
  (`src/main/resources/static`), `front_jsp_dir` (`src/main/resources/META-INF/resources/WEB-INF/jsp`), `boot_as_is:
  in-place | layout`, `boot_module_pattern` `*-boot`, `jsp_boot_packaging: war | jar`, `comp_artifact_suffix`,
  `multi_module_policy: merge | wrap` (merge), `merge_collision: first-wins | fail`, `web_xml_registrations: generate |
  followup`, `drop_managed_versions: gte | any | off`; helpers `module_name(which, name)` / `module_path(which, name)`.
  `{name}` = `layout.project_name` → root property `sbmig.project-name` → root artifactId. `config/artifacts.local-e2e.yml`
  pins the older `module/boot-jar` + `module/comp-jar` layout.
* `ArtifactsConfig`: `parent` (root pom's parent, company `base-parent`), `boot_parent` / `comp_parent` (role parents;
  `role_parents`, `parent_specs()`, `parent_keys()`), `mandatory_target: root | modules` (root), mandatory artifacts with
  `type` / `scope` (`scope: import` + `type: pom` → `<dependencyManagement>`), optional artifacts. Every "configured
  in-house parent" check covers the role parents (an unresolvable one → `parent_not_found`, preflight checks them).
* `RestructureResult`: `parents_set` (`"<pom>: <old g:a:v or '-'> → <new g:a:v>"`), `managed_versions_dropped`,
  `merged_modules`, `kept_boot_module`, `merge_conflicts`, `boot_module` / `comp_module` / `front_module` /
  `boot_packaging` / `front_files`, `placeholder_files` / `placeholders_written`, `flags` (added to `RunReport.flags`).
* Layout with role parents: root = aggregator on `artifacts.parent` (modules back, (front), boot; only the properties it
  still references); boot module on `boot_parent`, back / front on `comp_parent`, explicit groupId / version +
  `<relativePath/>`; the module poms get the Boot BOM / release property when the parent chain does not manage them, plus
  the root's management, pluginManagement and repositories. A project pom whose parent has the g:a of a role parent at an
  old version moves to the configured GAV (idempotent).
* Front rules (front module set and web files exist): `src/main/webapp/**` minus `WEB-INF` / `META-INF` →
  `<front>/<front_static_dir>`; `src/main/resources/{static,public,templates}` → front; `WEB-INF` JSPs →
  `<front>/<front_jsp_dir>`; JSPs outside `WEB-INF` → `META-INF/resources/<path>`; `WEB-INF/tags/**` + `*.tld` →
  `META-INF/resources/WEB-INF/…`; other `WEB-INF` files → `<boot>/src/main/resources/asis-webinf/`; `WEB-INF/lib|classes`
  stay in back (follow-up); `warSourceDirectory` / `webResources` are stripped from the generated poms (refined by
  "v2.1g").
* WAR rules (JSPs moved and `jsp_boot_packaging: war`): boot packaging `war`, `spring-boot-starter-tomcat` +
  `tomcat-embed-jasper`, JSTL → `jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api` +
  `org.glassfish.web:jakarta.servlet.jsp.jstl`, the generated Application extends `SpringBootServletInitializer`
  (template `servlet_initializer`), `spring.mvc.view.prefix/suffix` from an as-is view resolver else a follow-up
  (embedded Tomcat is the target: "v2.1q").
* web.xml → Boot registrations (`sbmig/restructure/webxml_registrations.py`: `load_skip_rules`
  (`knowledge/config/web_xml_rules.yml`), `plan_registrations`, template `WebXmlRegistrations.java.j2`): flag
  `web-xml-registered`, journal `web_xml_registrations`. Boot 4.1 FQCNs:
  `org.springframework.boot.web.servlet.{ServletRegistrationBean, FilterRegistrationBean,
  ServletListenerRegistrationBean}`, `org.springframework.boot.web.error.{ErrorPage, ErrorPageRegistrar}`,
  `@ServletComponentScan` = `org.springframework.boot.web.server.servlet.context.ServletComponentScan`.
* `merge` (multi-module, default): an as-is module named like the boot module is the kept boot module (its dependencies
  on merged modules become one dependency on the back module); every other module and the root `src/` fold into back;
  first module in root `<modules>` order wins a collision, identical copies dropped silently, differing copies saved as
  `<run dir>/merge-conflicts/<module>/src/...` (`MergeConflict(path, kept_from, dropped_from, identical, saved_copies,
  union)`; line-list registries merged as the union). `RunReport.merge_conflicts`, Handover block "Merge conflicts".
  Pom-packaging sub-modules merged depth-first; `../x` modules skipped (note + follow-up); EAR modules not merged (files
  → `<boot>/src/main/resources/asis-ear/`, flag `ear-module-dropped`); `<type>war</type>` dependencies go to the boot WAR
  with the as-is `<overlays>` (flag `war-overlay-moved`); JSPs left outside every module → flag `web-content-left`.
* Merge-conflict hints in the fix loop: a case whose file is a first-wins kept file, or whose errors name that class, gets
  `FixContext.reference_notes` + `reference_files` (the saved copy, ≤ 12,000 chars, ≤ 3) — read-only, not in the build;
  writes to those paths are undone. `sbmig.report.handover.resolve_current_path(ws, path, cfg, name)` maps old paths to
  the module they moved to.
* `layout.drop_managed_versions` (after parent / mandatory / optional edits): explicit `<dependencies>` versions the
  restructured parent chain manages are dropped (`gte`: only when managed ≥ explicit). Never touched: in-house artifacts,
  the project's own modules, system scope, non-jar type / classifier, dependencyManagement, plugins, properties.
  `params["effective_pom"]` is a zero-arg provider; the declaring module's entry is used (refined by "v2.1l" … "v2.1t").
* MSA placeholders: each service file gets the findings of that service plus the shared ones.
* A failed / crashed `layout` / `boot_pom` / `msa_pom` step → flag `restructure-failed`, follow-up (the final build ran on
  the as-is layout), journal `restructure_failed`, `report.success = False` (exit 2); a failed `optional_bump` is a
  follow-up only.

### Build: Maven runner, log parser, signatures (`sbmig/build/`)
* `BuildConfig.external_package_prefixes` + `sbmig.build.register_external_prefixes` / `external_prefixes_from_config`;
  `MavenRunner.__init__` registers the in-house group ids so they are not masked in signatures.
* `BuildRunnerLike.run(..., skip_tests=False)`; `build.allow_skip_tests_fallback` (a goal that runs tests and leaves only
  `TEST_FAILURE` errors rebuilds with `-DskipTests`, flag `tests-skipped`) — inactive while `build.skip_test_compile`
  (`-Dmaven.test.skip=true`) is on.
* `build.retry_negative_cache` (true): a build failing on a resolution failure that "was cached in the local repository"
  (`sbmig.build.runner.negative_cache_hits`) runs once more with `-U` (once per artifact per run, never offline); journal
  `build_negative_cache_retry`, log `NNN-build-<label>-U.log`.
* `mvn_env` appends `-Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8` to `MAVEN_OPTS` (`with_utf8_output`); a value set in
  `build.env` / `MAVEN_OPTS` wins.
* **Signatures (L1 / L2; L2 = the case key)**: `package_does_not_exist` keeps the whole package;
  `method_does_not_override` / `static_import_only` carry the method / class read from the source line into
  `BuildError.symbol`; `illegal_start` the javac phrase (first 8 words); `plugin_execution` = plugin goal + normalized
  cause (`CAUSE_WORDS`); L1 masks project FQCNs inside the symbol (reactor artifactIds: "v2.1z"). A rule change changes
  case ids.

### LLM client tier and adapter seams
* Tier 1 (`sbmig/core/interfaces.py`): `LLMRequest` (messages, model, max_tokens, temperature, stop, `role: LLMRole`,
  `metadata`), `LLMAdapterLike.complete(request) -> LLMResponse`; `LLMClientLike.complete` accepts `role=` /
  `metadata=` (clients may ignore them). Roles: `fix`, `fix_escalation`, `plan_advisor`, `structure_review`,
  `verify_guide`, `other`. `LLMRequest.metadata` = `run_id`, `project`, `phase`, `iteration`, `cluster_id`
  (`LLMFixer.call_tags`) — never secrets.
* `llm.provider`: builtin (`company` default, `anthropic`, `openai_compatible`, `fake`), `plugin` (+ `llm.plugin:
  "pkg.module:attr"`, `llm.plugin_options`), or an entry point in group `sbmig.llm_adapters` (`sbmig.llm`: `LLMAdapter`,
  `AdapterClient`, `load_plugin`, `resolve_llm_plugin`, `resolve_fixer_plugin`). `RecordingClient` records requests to
  `llm.record_dir`.
* Tier 2: `FixConfig(plugin, plugin_options)` as `cfg.fix` — a `FixerLike` class or factory `fn(options, cfg, llm)`
  (entry-point group `sbmig.fixers`) replaces `LLMFixer`; flow-v2 needs `propose_script` (see "v2 — flow-v2").
* Provider `company` = `sbmig.fix.company.CompanyClient` (`requests.post`, `llm.payload_extra`, `response_path` /
  `LLM_RESPONSE_PATH` via `sbmig.fix.client.extract_reply`, records when `LLM_RECORD_DIR` is set); gateway variants in
  "v2.1z-m" … "v2.1z-x". `.env` is read as UTF-8 (BOM or not), UTF-16 or cp949 and re-read when edited (shell variables win).
* `LLMError` texts the loop classifies: `status=<code>` (`status=None` = no HTTP answer), `cause=<ExceptionClass>: <msg ≤ 120
  ASCII chars>` (`sbmig.fix.company.transport_cause`); a plugin exception is wrapped as `LLM adapter <name> failed
  (status=<s>, cause=<Class>): …`; an empty answer with `finish_reason=length` raises (not retried). A gateway
  content-filter answer raises `LLMError("content filtered by the gateway …")` (`content_filter_reason`).
* `sbmig.loop.fixloop.is_llm_transport_error`: transport / configuration only on `status=None|401|403|404|408|429|5xx`,
  `cause=…`, a known exception class name or explicit config phrases; `status=400|413|422`, a content filter and
  `finish_reason=length|max_tokens` are request rejections. The same HTTP 400 body for `LLM_CONFIG_ERROR_PROMPTS` (2)
  different prompts → flag `llm-config-error`, no more LLM calls, journal `llm_config_error`.
* LLM cool-down: the second consecutive transport failure pauses once per run for `llm.cooldown_seconds` (journal
  `llm_cooldown`, flag `llm-cooldown`); a second streak marks the LLM unavailable.
* `sbmig llm-probe` ends with `LLM-PROBE ok|FAIL <kind> provider=… model=… status=…` (`ProbeResult.line()`, exit 0 / 1);
  `probe_llm(cfg)` is shared with the fleet preflight.

### Fix-loop invariants (`sbmig/loop/`, `sbmig/triage/`)
* Blocked items: `DecisionKind.BLOCKED`, `BlockReason`, `BlockedItem` (`via`, `via_direct`), `PhaseResult.blocked`,
  `RunReport.blocked`; registry keys = (L1, full posix path) (`sbmig/loop/blocked_registry.py`; `note_moves` /
  `canonical_file` follow restructure renames, journal `restructure_moves`; `fill_via`, `compose_suggestion`).
  `loop.blocked_policy`, `loop.oscillation_window`, `loop.structural_regression_kinds` (incl. `dependency_resolution`).
* Scoring (`BuildFixLoop._score`) outcomes: `build_ok`; `improved` (every targeted error gone and the build reached their
  stage); `partial` (some gone, nothing new in the touched files); `no_change`; `regressed` (new errors in a touched file
  by (file, signature) occurrences, a new structural kind after a pom edit, or earlier-stage errors after a pom edit
  that hide the targeted ones); `inconclusive` (timed out, failed without a parsed error, stopped at an earlier stage the
  attempt did not cause). `SUCCESS_OUTCOMES` = improved, build_ok; `PROGRESS_OUTCOMES` = partial; `FAILED_OUTCOMES` =
  no_change, regressed, reverted, apply_failed, inconclusive. Same-case matching: `sbmig.triage.decide.same_cluster`.
* **Invariant 1**: after an internal abort every planned final-build step without a result runs
  (`_final_build_after_abort`); an interrupted phase stays in the report (journal `phase_end.interrupted`);
  `report.success` is False when the run aborted or a planned final build has no result (`_verify_missing`); `_abort`
  logs one ERROR line `run aborted: <reason>`.
* Java preflight (`Orchestrator._check_java_major`, `build.require_java_major` 21): another major aborts before any build
  (exit 3, flag `jdk-not-21`).
* Finalize order: KB records and the report first, `changes.patch` / `result.zip` last (a path whose file could not be
  written is dropped and the report rewritten).
* Infrastructure flags (`Orchestrator._infra_flags`): `infra:llm`, `infra:maven`, `infra:jdk`, `infra:repository`
  (`aborted:parent-not-found`, or every final-build error is a repository / network resolution error), `infra:disk`
  (`infra:file-lock`: "v2.1r").
* Operators: fix-loop milestones (`_mark`) and a watchdog thread (`WATCHDOG_S` 30 s, `WATCHDOG_QUIET_S` 60 s, auto thread
  dump after `WATCHDOG_DUMP_S` 300 s into the run dir) that stays silent while a build, a rewrite run, an LLM call
  (`sbmig.fix.client.llm_call_in_flight`) or a Maven child runs. `fixer.grep` is bounded (`MAX_GREP_FILES`,
  `MAX_GREP_FILE_BYTES`, `GREP_BUDGET_S`).
* Thread dumps (`sbmig.core.diag`: `thread_dump_text`, `dump_threads(out_dir)`, `install_thread_dump_signal`): `SIGUSR1`,
  Ctrl+Break (`SIGBREAK`) on Windows, a trigger file `dump-threads` / `dump-threads.txt` (start dir, out dir or a recent
  run dir; polled every 2 s), `GET /api/threads` under `sbmig serve`.

### Advisories and the Handover
* Models: `AdvisoryPolicy` (guide | recipe — **no block**), `AdvisoryStatus`, `AdvisoryOutcome`, `LibraryAdvisory` (one
  entry of `knowledge/libraries/advisories.yml`), `AdvisoryFinding` (`llm_fixed_files`, `llm_fix_notes`),
  `VersionChange`, `HandoverSummary`; `RunReport.advisories`, `RunReport.handover`. Config `advisories` (`enabled`,
  `file`, `extra_files`, `policy_overrides`, `report_carried_over`, `max_from_here`; `allow_recipes` false in v2).
* `sbmig/advisory`: `load_advisories(cfg)`, `evaluate_advisories(profile, cfg, catalog=None)`, `verify_advisories`,
  `followups_for`, `hints_for(errors, findings, profile=None) -> (hints, ids)` (an advisory matches a case by import
  package, artifact or symbol owner; rendered as `FixContext.advisory_hints`, recorded as `FixAttempt.advisory_ids`),
  `sbmig advisories list|show|check`. Per-library notes `knowledge/notes/*.md` (`## fix-loop: <library>`, `notes/index.yml`).
* Report first section "Handover" (`sbmig/report/handover.py: build_handover(profile, view, report, effective=None,
  cfg=None)`): versions replaced, libraries needing attention, properties to set, merge conflicts, "from here on, your team".

### Report and the result line
* `report.md` / `report.json` (`sbmig.report.write_report`), Korean strings in `sbmig/report/i18n.py` (drift test);
  sections as in "v2 — flow-v2" ("Structure review") and later sections.
* `SBMIG-RESULT run=<id> exit=<n> build=<ok|FAIL> unresolved=<n> blocked=<n> open=<n> resolved=<n> llm=<calls>
  attn=<codes>[ review=<v>][ major=<n>][ remediation=<STATUS|skipped>]` — one ASCII line at the end of `sbmig run` (also on
  a crash: `attn=crash`), one per fleet project, in a web run's `sbmig.log`. `sbmig/report/result_line.py` (`result_line`,
  `report_result_line`, `attention_codes`, `report_exit_code`: 0 success, 2 build failed, 3 refused / aborted); its module
  docstring is the list of attention codes, most important first. New fields go at the end, only when they have a value.

### Fleet (`sbmig/fleet/`)
* `sbmig fleet <manifest>` runs N projects strictly sequentially with one agent, resumable (`fleet/state.json`, keyed by
  `input_key(path)`), KB shared between projects; `fleet/summary.{md,csv,json}` + `fleet/learning.md` after every project
  (`summary.csv` UTF-8 with BOM, columns `attention`, `input`; "Needs attention" grouped by the first code; `sbmig fleet
  status --attention`). Models `FleetConfig`, `FleetProjectResult`, `FleetState`.
* Manifests (`sbmig.fleet.manifest.read_text_any`): UTF-8, UTF-16, else cp949; non-ASCII / colliding names → ASCII slug
  + 6-char hash (`name_with_hash`).
* Preflight (`sbmig/fleet/preflight.py` `run_preflight`, lines `preflight: <check> ok|FAIL|skip <detail>`): disk, maven,
  java, settings, repository (one GET to the settings mirror, settings `<proxy>` honoured), llm. `fleet.preflight` (true).
* Infrastructure breaker (`FleetRunner`): a project ending with `infra:<kind>` goes back to `pending`; after
  `fleet.infra_failure_limit` (3) in a row the fleet waits `fleet.infra_retry_minutes` (15), re-runs the preflight and
  resumes (each project re-run ≤ `MAX_INFRA_REQUEUES` = 2); after `fleet.infra_max_waits` (4) it stops: exit 3
  (`EXIT_STOPPED_ON_INFRA`), a plain re-run resumes. Free space checked before each project (`fleet.min_free_gb`, 5).
* Cleanup (`sbmig/fleet/cleanup.py`): `cleanup_run(run_dir, mode)` per `fleet.cleanup` (`none` | `build-output` |
  `workspace`; keeps `KEEP_FILES` + `logs/`); `sbmig clean [--out] [--fleet] [--older-than DAYS] [--keep-failed]
  [--dry-run] [--records DIR]`.
* Logging: every project logs DEBUG into its own `<run dir>/sbmig.log`; `fleet/sbmig.log` is INFO.

### KB plumbing (`sbmig/kb/`)
* Secret masking (`sbmig/kb/redact.py`: `redact(text)`, `Redactor`, `get_redactor(cfg)`): built-in contexts +
  `kb.redact_file` (`knowledge/kb/redact.yml`: `key_words`, `key_patterns`, `patterns`, `allow_keys`). Only VALUES are
  masked (`***`), never comparisons, non-literal assignments or placeholders.
* Append-only jsonl (`sbmig/kb/jsonl.py`: `append_lines`, `repair_tail`, `parse_record`): a torn last line is moved to
  `<file>.corrupt` before the next append; one write + fsync per batch.
* Cross-process lock (`sbmig/kb/lock.py`: `kb_lock(root, timeout=None)`): `<kb>/.lock` created exclusively, holding
  pid / host / time / token / cmd; re-entrant per process; stale after `kb.lock_timeout_seconds` (300) or when the pid is
  gone; never fails a run (hardened for several pods: "v2.1z-ae" / "v2.1z-af").

### Graph engine (`sbmig/graph/`)
* `MigrationState` (TypedDict: `run_id`, `stop_after`, `dry_run`, `phases` (remaining), `phase`, `aborted`, `stopped`,
  `summary`). Nodes `intake` → `analyze` → `plan` → `phase` (one invocation per plan phase group, looping while phases
  remain) → `finalize` → END. `build_graph(orch)`, `run_graph(orch, stop_after=None, dry_run=False) -> RunReport`,
  `stream_graph(orch, ...) -> Iterator[(node, update)]`.
* `Orchestrator.run()` dispatches on `loop.engine` (`graph` default, `inline`); the step bodies are shared methods
  (`step_intake` / `step_analyze` / `step_plan` / `step_phase` / `step_finish`), so both engines give identical reports,
  journals and exit codes.

### Web app (`sbmig/web/`)
* FastAPI `create_app(out_dir, repo_root, ...)`, module-level `app` for `uvicorn sbmig.web.app:app`; `sbmig serve`
  launches uvicorn. Static page `sbmig/web/static/` at `/static` and `/`. Routes: `GET /api/configs`, `GET|POST /api/runs`
  (zip body), `GET /api/runs/{id}`, `GET /api/runs/{id}/events` (SSE), `POST /api/runs/{id}/stop`,
  `GET /api/runs/{id}/files/{name}` (`SERVED_FILES`), `GET /api/threads`, `/api/remediations…` ("remediations").
* `RunManager` (`sbmig/web/runs.py`; its module docstring is the contract): one worker thread per run through
  `stream_graph`; SSE events `status`, `node`, `error`, `journal`, `log`, `report`, `ping`, `end` (replayable per run);
  per-run log via `attach_file_log(path, thread_id=...)`. Stop takes effect between graph nodes (exit 3, no final build);
  a stop after the verify phase changes nothing. `max_finished_runs` (200) bounds memory; evicted runs answer from
  `report.json` + `web-status.json`. Job mode: "v2.1z-ac" … "v2.1z-af".
* Web token (`sbmig.web.app.TokenGuard`): without a token only loopback clients are served; `sbmig serve --token` /
  `SBMIG_WEB_TOKEN` is required for a non-loopback host (else exit 2); `Authorization: Bearer <token>` or `?token=` once
  (HttpOnly SameSite=Strict cookie). Open mode `create_app(open_network=True)` / `sbmig serve --open` / `SBMIG_WEB_OPEN=1`
  serves every client without a token (one warning at start); a configured token still wins. The k8s Deployment sets
  `SBMIG_WEB_OPEN=1`.

## v2 — flow-v2 (2026-10-26, branch `flow-v2`, plan `docs/v2/PLAN-v2.md`)

The business team's flow: every OpenRewrite recipe in ONE run without a build, the company restructure, an LLM
structure review, then `mvn clean package`; every build error is fixed by an **LLM-written Python fix script** stored
per case (L2) and reused / augmented on later projects. The KB and run-directory records of the deployed system are the
v2.1 ones (`docs/v2/PLAN-v2.1.md` §3.3 – §3.5; docstrings of `sbmig/errors/` and `sbmig/kb/store.py`): `<kb>/errors.jsonl`,
`cases.md`, `cases/<case_id>/case.json` + `fix_v<N>.py`, `.lock`; the run's `errors.jsonl` / `errors.md` (`ErrorList`,
`ErrorEntry`). Packages: `sbmig/script/` (runner), `sbmig/errors/` + `sbmig/kb/` (KB), `sbmig/fix/` (prompts, fixer),
`sbmig/review/`, `sbmig/report/`, `sbmig/loop/` + `sbmig/plan/` + `sbmig/triage/` + `sbmig/graph/`.

### Models (`sbmig/core/models.py`)
* `Phase.TRANSFORM = "transform"` (the one rewrite run; ordered between `plan` and `jdk21`), `StepKind.REVIEW`,
  `DecisionKind.SCRIPT`, `CaseActionKind` += `script` (`CaseAction.script_version` = the case's best version).
* `ErrorCluster.case_id` — v2 clusters are ONE case (L2) across files (`file` None, id `case:<case_id>`).
* `ScriptRunResult` (`status` ok | error | timeout | rejected, `changed_files`, `log`, `error`, `problems`,
  `duration_s`),   `ScriptBase` (the version an augment extends + what it did in this project + `history` lines), `ScriptProposal`
  (code, analysis, usage, `error` when there is no script), `ReviewIssue` / `StructureReview` (verdict ok | issues |
  skipped | failed, issues, script + result + commit, usage, note); `RunReport.structure_review`.
* `FixAttempt` += `script_mode` (new | reuse | augment), `script_case_id`, `script_version`, `script_parent`,
  `script_code`, `script_result`.
* `FixMethod` += `script` (new LLM script), `script_reuse` (stored version, no LLM call), `script_augment`; the
  resolver id of a script attempt is `script:<case_id>:v<N>`.

### Seams (`sbmig/core/interfaces.py`)
* `ScriptRunnerLike.check(code) -> list[str]`, `.run(ws, code, *, errors=(), timeout_s=None) -> ScriptRunResult`
  (changes stay uncommitted; never raises).
* `ScriptStoreLike` — implemented by `sbmig.errors.cases.CaseStore` (its docstring in `sbmig/core/interfaces.py` is the
  contract): `find`, `best(case_id) -> (CaseScript, code) | None` (proven newest, else unverified newest; retired never),
  `code`, `add_script`, `credit(case_id, version, result, project, run_id)`, `retire` / `activate`, `pin_of` / `set_pin`,
  `describe`, `record_run(entries)`, `stats()`.
* `ScriptFixerLike.propose_script(ws, cluster, ctx) -> ScriptProposal` — `FixContext` += `script_mode` (new |
  augment), `case_id`, `signature_generic`, `script_base`. An augment answer is the COMPLETE new script (it replaces
  the base and runs on a tree where the base was rolled back). A tier-2 plugin without `propose_script` cannot fix
  (flag `fixer-without-scripts`, no fallback).
* `StructureReviewerLike.review(ws, profile, cfg, *, run_id, restructure_notes, restructure_results)
  -> StructureReview`. `LLMRole` += `structure_review`.

### Config keys (mirrored in `config/default.yml`; `tests/core/test_v2_contracts.py`)
| key | default | meaning |
|---|---|---|
| `openrewrite.recipes.*` | jdk21 `UpgradeToJava21`; jakarta `JavaxMigrationToJakarta`; spring_boot_4 `UpgradeSpringBoot_4_0`, `SpringBootProperties_4_1`; framework 7 / security 7 / junit5 empty | v2 recipe scope (business decision) |
| `loop.phases.{jdk21,spring,restructure}.enabled` | false | no intermediate loops |
| `loop.phases.verify` | package, 50 rounds | the script fix loop |
| `loop.max_llm_calls` / `max_total_minutes` | 0 (= no limit; -1 = never call the LLM) / 240 | |
| `build.skip_test_compile` | true | `-Dmaven.test.skip=true` on every build |
| `kb.dir` | `kb-v2` | the file KB |
| `advisories.allow_recipes` | false | advisories = LLM hints + Handover only |
| `script.timeout_seconds` | 60 | per run, then the process tree is killed |
| `script.python` | null | interpreter of the script process (null = sys.executable) |
| `script.extra_allowed_imports` | [] | added to the import whitelist |
| `script.max_code_chars` / `script.log_lines` | 30000 / 200 | |
| `script.max_files_changed_warn` | 40 | a run changing more files gets a warning note |
| `script.reuse` / `script.reuse_versions` | true / 1 | stored versions tried before the LLM is asked |
| `script.max_augments_per_case` / `script.max_llm_tries_per_case` | 15 / 20 | augments per case and run / all LLM asks per case and run, then BLOCKED `repeated_failure` ("v2.1z") |
| `review.enabled` / `fix_critical` / `ask_script` | true / true / false | structure review; critical findings fixed by the answer's edits (a script only with `ask_script: true`); `review.record` is read by nothing |
| `review.max_pom_chars` / `max_poms` | 60000 / 40 | prompt size |

### Plan (L)
Non-MSA: `transform.rewrite` (OPENREWRITE, TRANSFORM; jdk21 set + jakarta set when the case needs it — spring_legacy,
servlet_tomcat, pure_java with Java EE javax imports; never for spring_boot, the Boot umbrella migrates jakarta — +
spring_boot_4 set for spring_boot; one run; no transform step without java sources) →
`restructure.apply` | `restructure.boot_pom` | `restructure.pom_only` (RESTRUCTURE) → `restructure.review` (REVIEW,
RESTRUCTURE) → `verify.build`. MSA: `transform.rewrite@<target>` per library root (never the Boot set) / service
(one un-suffixed run under `top-aggregator`), `restructure.msa_pom`, `restructure.review`, `verify.build[@<svc>]`.
No BUILD_FIX_LOOP steps before verify, no pre-emptive handlers, no project (`com.company.migrate.*`) / library / KB /
advisory recipes.

### Script fix loop (L; `loop.phases.verify` after a failed final build)
* Clusters by L2 (`signature_generic`) across files; errors without an L2 get `case_id =
  sha1(kind|normalized file|L1)[:10]`. Triage priorities: pom / parent / dependency resolution first, masking.
  Decisions: SCRIPT, or BLOCKED / SKIP / ESCALATE (`loop.blocked_policy`, budget, LLM unavailable, already
  blocked). One attempt per case and round; the rebuild scores every pending attempt (`_score`, the scoring rules in "Base contracts").
* Ladder per case and run: (1) **reuse** — `script.reuse` and the store's best version not yet tried in this run (no
  LLM call); (2) **augment** — base = this run's last script attempt of the case (code, result, remaining / introduced
  errors) or else the store's latest version; ask `propose_script(mode augment)`, store as a child version, run; at most
  `script.max_augments_per_case`; then (3) a fresh **new**; all LLM asks ≤ `script.max_llm_tries_per_case` → BLOCKED
  `repeated_failure` ("v2.1z").
* A script attempt: `base_commit = ws.head()`; `ScriptRunner.run(ws, code, errors=cluster.errors)`; status rejected /
  error / timeout → `apply_failed` with the working tree restored (no partial application); ok without a change →
  `apply_failed` "no file changed"; ok with changes → commit `fix(script:<case_id>:v<N> <mode>): <cluster id>`.
* Before a case's next attempt, its previous attempt is rolled back unless it was improved / build_ok (regressed was
  already reverted by scoring) — every version is measured from the same base, so a stored version is complete.
* Every LLM script is stored before it runs (failures keep their lineage); after scoring the store credits every
  script attempt. Records: `FixAttempt.script_*`; resolver `script:<id>:v<N>`.

### Script contract (S)
Answer: `<analysis>…</analysis>` + `<script>…</script>` (fences inside allowed) defining `def fix(p)`;
`<need_files>` (`<need_file>` accepted) / `<need_grep>` context requests. Helper `p` (relative posix paths; absolute, `..`,
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
v2.1 layout (see the section intro): `<kb>/cases/<case_id>/case.json` (lineage, successes / failures / projects, pin) +
`fix_v<N>.py`; every write under the KB lock; analysis / logs redacted. CLI `sbmig kb reindex | stats | validate |
export | import`, `sbmig kb errors list | show`, `sbmig kb cases list | show | retire | activate | pin | unpin`.

### Structure review (R) — `StructureReviewer(cfg, llm, runner, kb)`
Input: every pom after the restructure (≤ `review.max_poms`, ≤ `review.max_pom_chars`, head + tail cut), module dirs
and packaging, restructure notes, the company rules (`artifacts.parent` / `boot_parent` / `comp_parent`, mandatory
target, optional artifacts, `layout.*`), case type, detected source encodings. Prompt `sbmig/review/prompts/
review_v1.md`, role `structure_review`; answer `<review>` JSON `{verdict, issues[{severity, file, problem, fix_hint}],
analysis}` next to the v2.1 four-part answer (`<analysis>`, `<solution>`, `<edits>`, optional `<script>` only with
`review.ask_script: true`). Non-UTF-8 sources without a matching `project.build.sourceEncoding` are critical (JDK 21
garbles them with a warning only); sbmig adds that issue itself (`(sbmig check) …`) when the LLM misses it. Critical +
`review.fix_critical` → the edits are applied through `sbmig.script.apply_edits` (a script-only answer runs through the
`ScriptRunnerLike`), uncommitted; the orchestrator commits `review: …` and fills `commit`; failure → working tree
restored, note. At most 3 LLM calls (review, unreadable-answer retry, failed-edits retry). The review is recorded in the
run's error list and the report section "Structure review" (ko/en); warnings become Handover follow-ups. The module
docstring of `sbmig/review/reviewer.py` is the contract.

### As built (step 1)

**Plan and flow**
* Plan note (English): `transform: one OpenRewrite run (…) — no build until the restructure is done (v2)`; an old
  setting set again is named as ignored (`v2: no intermediate build-fix loops (…ignored)`). `--stop-after` / web stop:
  intake | analyze | plan | transform | restructure | verify.
* The failed final build is the verify loop's build 1 (`loop.run(..., first_build=…)`, one Maven build saved): INFO
  `verify: final build failed with N error(s) in M case(s) → script fix loop (mvn package, up to N round(s))`, then
  `[verify] build 1 = the failed final build (N error(s), <log>) — not built again`; after the loop the final build runs
  again (invariant 1).

**Script fix loop** (`sbmig/loop/fixloop.py`, ladder `sbmig/triage/scripts.py` `plan_script_step`, clusters
`sbmig.triage.cluster.cluster_cases`)
* Cluster id `case:<case_id>`; a targeted MSA loop appends `@<target>` (`case:<id>@<svc>`: two services never count
  against each other), `ErrorCluster.case_id` stays the plain id (store key). Order: triage priority, then error count.
* Ladder details: **reuse** = the stored best version not run in this run yet — no LLM call, so it also works with the
  LLM down or the budget used up, and it comes before `loop.blocked_policy`; **augment** base = this run's last attempt
  of the case that had code, else the store's latest (a stored but unproven version is augmented, not re-run); **new**
  only when neither exists. A `new` answer without a script does not open an augment (the next request is `new` again).
  Caps: `script.max_llm_tries_per_case` (20) in total, `script.max_augments_per_case` (15) for the augments. Escalation
  model on a stall (`loop.stall_limit`) or from a case's second augment on (refined by "v2.1z-x").
* `loop.blocked_policy` v2 (LLM script attempts before BLOCKED): `external_javax_api`, `external_api_incompatible`,
  `artifact_not_found`, `plugin_incompatible` 2; `parent_not_found`, `unsupported_source`, `encoding` 0.
* **Rollback before the next try** (`BuildFixLoop._rollback_previous`): the case's previous applied script attempt is
  rolled back when its outcome is partial / no_change / inconclusive (`NO_ROLLBACK` = improved, build_ok, reverted,
  pending, apply_failed, blocked): HEAD = its commit → **reset** to its `base_commit`, else **`git revert --no-edit`**
  (a failing revert is aborted and noted; the next version then runs on top of it). The errors it had removed come
  back into the round and into the next scoring baseline (never counted as regressions); journal `script_rollback
  {attempt, case, version, outcome, commit, mode: reset|git revert, restored_errors}`, note `rolled back before …`. When the next attempt changes nothing the rollback
  is **undone** (tree back to the HEAD the last build saw, journal `script_rollback_undone`). A partial attempt of a
  case that ends BLOCKED stays in the tree.
* Attempt extras: an augment identical to its base, or an LLM answer identical to a version already run (same sha1),
  is `apply_failed` without running; writes to a merge-conflict reference copy are undone before the commit; more
  than `script.max_files_changed_warn` changed files → note + WARNING line; journal `script_stored {case, version,
  parent, mode, attempt}`, `script_credit`. Attempt ids stay `<phase>-<round>-<n>` (`verify-1-1`).
* `LLMFixer.propose_script` (`sbmig/fix/fixer.py`, parser `sbmig/fix/script_protocol.py`, prompts
  `sbmig/fix/prompts/script_system_v1.md`, `script_user_v1.md.j2`): `<need_files>` / `<need_grep>` context rounds, then
  **one feedback round** when the script cannot be used (no `<script>`, no `def fix(p)`, static-check findings,
  identical to the base). The parser is tolerant (fences, CRLF, a block indented as a whole,
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
  ko` → the model answers in Korean. An LLM error → verdict `failed` and the build goes ahead.
* Guards: not applied when the tree has uncommitted changes; changes that touch sources (`.java .kt .groovy .scala .jsp
  .jspx .tag`) or leave a pom that no longer parses are refused / restored. The orchestrator commits `review: fix
  critical structure issues (<n>)` (uncommitted changes without an ok apply are discarded). Journal `structure_review
  {step, verdict, issues, critical, script_status, commit, note}`.
* Report: section "Structure review" / "구조 점검" after Handover, before Blocked; header row; console line; Handover
  follow-ups (what was changed + commit, each unfixed critical issue, warnings; at most 8). `SBMIG-RESULT …
  attn=<codes> review=<ok|issues|skipped|failed>`: the `review=` token is appended only when the report has a review.

**E2E replay** (fixtures): `replay/llm-*.json` = `{"note", "match", "replay_role", "response"}`; `match` is a
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
  False = Handover follow-up only.
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

## v2.1i — removed (superseded by v2.1z)

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
  Maven project; not ok → abort exit 3 with `res.error`.
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
parent, or any external parent) managed are lost when the restructure moves a pom to `comp-jar` / `boot-jar` / `base-parent`,
and the fix prompt's rule 14 (no invented versions) left the LLM with nothing to say. The as-is effective capture
(`logs/effective-depmgmt.json`, `sbmig.build.effective`) still exists and is now used again; the LLM is told to leave unknown versions
empty and sbmig fills them. Decisions (user): the pins are reviewed by the LLM structure review like every other pom change
and verified by the build; the non-Boot WAR services of an MSA tree stay pom-only for now; Spring Security recipes run only
for projects that use Spring Security; Spring Cloud needs nothing new (the Boot 4 umbrella already runs
`UpgradeSpringCloud_2025_1`, a non-Boot project cannot use Spring Cloud).

### Resolution order (one rule, used by the restructure and by the fix loop)
For a `<dependencies>` entry without `<version>` (and not: the in-house parent / role parents / mandatory / optional /
`remove_when_present` artifacts, the project's own modules, `system` scope):
1. the new parent chain manages it (post-restructure effective pom, `depmgmt-after-restructure.json`) → stays empty;
2. the as-is build resolved it (`logs/effective-depmgmt.json`: the module's `by_module[...].direct`, then merged `direct`, then merged
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
  listed in that set (`spring_framework_7`: "v2.1o").

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
  stay); commit `fix(sbmig-version): <cluster id>`. Prompt facts: no capture → no lines (never
  "not declared"); a version found only among the as-is direct dependencies is labelled `(declared)`; ≤ 12 artifacts per case;
  the prompts explain the `as-is build:` / `new parent chain:` lines in one sentence.
- Known limit: without the post-restructure capture (it fails while any version is still missing) an LLM-added dependency is
  filled from the as-is build or the repository even if the new parent manages it — the structural-regression rule would
  otherwise revert the whole try on the next build. A re-capture after the first zero-LLM try would close this; not done.
- Recipes / fixture as in the contract; the catalog already had entries for `security6.UpgradeSpringSecurity_6_0 … 6_5`
  already, only their `sets:` changed.
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
accumulated KB had masked it (a stored script from an earlier project). v2.1m applies a deterministic rule inside the restructure
and keeps a reactive zero-LLM try in the loop.

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
  the error list starts with the restructure's pom corrections and the first verify build`.
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
  review's restructure lines (`jaxb.version` is removed with its only user). In the `spring5-security-batch` shape all three as-is properties go:
  `spring-security.version` / `spring-batch.version` through the BOM override step, `spring.version` through this rule; a
  `junit.version` used by a test-scoped, unmanaged junit stays. The Handover's "Spring / Boot after" read the final poms' own
  properties, so nothing else changed there.
- `layout.parent_property_names` (default `[]` since v2.1p — an escape hatch): names the company parent chain defines by convention (not Boot
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
  external deployment as the alternative.
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

## v2.1s — a version the new chain does not manage follows the TARGET Boot BOM before the as-is value (2026-10-30)

Company run: `jakarta.servlet:jakarta.servlet-api: 4.0.3 ← as-is managed by xxx:xxx-boot-jar:1.0.0-SNAPSHOT` — the pin
rule worked, but it pinned the as-is value because the company parent chain does not import the Spring Boot BOM
(`parent_chain_facts … boot_bom_added: true`); the project is moving to Boot 4.1 (Servlet 6.1), so the to-be line must win ("as-is 를
보면 안 되고 to-be 를 봐야 해"). Same journal line showed the structural issue: `parent_manages_spring_boot: true` while the chain
manages no Boot artifact.

### Resolution order (restructure pins and the loop's `VersionResolver`, replacing v2.1l's)
1. the new parent chain manages it (post-restructure capture, else the chain facts WITHOUT the Boot add-on) → no version;
2. the TARGET Boot BOM (`spring-boot-dependencies:<target.spring_boot_version>`, `load_boot_bom_facts`) manages it → that version,
   unless the as-is build's version is newer (then the as-is one — never a downgrade); source `boot-bom`, marker
   `← Boot <v> BOM (not managed by the parent chain)`;
3. the as-is build's version (`← as-is managed by … | declared`);
4. repository (loop-added dependencies and restructure unknowns, as before);
5. unknown.
- Rows / Handover: a `boot-bom` pin is reported like a carried-over pin but with the BOM wording; the Handover marks it ⚠ only when it
  differs from the as-is version (a jump the owners should know about).
### Warning when the configured parent chain carries no Boot BOM
- Restructure: when `parent_manages_spring_boot` is true and `ParentChainFacts.boot_bom_added` is true → flag
  `parent-chain-no-boot-bom`, note, journal, and the Handover's first "From here on, your team" line: "사내 부모 체인(<roots>)이 Spring
  Boot BOM 을 import 하지 않습니다 — `spring-boot.version` 속성만으로는 버전이 통일되지 않습니다. base-parent 의 dependencyManagement 에
  `spring-boot-dependencies:${spring-boot.version}` (type pom, scope import) 를 넣거나, 그때까지 회사 설정에 `parent_manages_spring_boot:
  false` 를 두세요 (sbmig 이 BOM 을 직접 import 합니다). 이 실행의 버전 고정은 Boot <target> BOM 을 따랐습니다." `SBMIG-RESULT attn=` gets
  `parent-chain-no-boot-bom`.

### As built (2026-10-30)
- `ParentChainFacts.chain_managed` (filled only when the Boot add-on happened) and the property `own_managed` (= `managed` otherwise);
  the loop's `chain_own_managed(facts, boot)` reads `own_managed` (or an older `managed_by_chain`), else drops the entries equal to
  the target BOM's from `managed`.
- Restructure: `_pin_asis_versions` — chain-managed (`own_managed`) → pinned as-is then emptied by the drop step as in v2.1l; else the
  target Boot BOM's version (marker `← Boot <v> BOM (not managed by the parent chain)`, as-is kept when newer by `compare_versions`);
  else as-is / repository / unknown. A `boot-bom` pin is never emptied by the facts fallback and never undone; only a REAL
  post-restructure capture that manages the artifact empties it (the effective pom is the truth) — a small deviation from the
  contract's "never emptied"; it gets no gte comparison and no carried-over note. `_fallback_capture` uses the chain's own entries
  (the add-on would otherwise drop explicit versions nothing manages afterwards). No as-is capture → the whole pin step still skips.
  Journal `versions_pinned.from_boot_bom`; pom-verify row `version pinned from the Spring Boot <v> BOM: g:a <version> (the parent
  chain does not manage it)`; Handover reason `REASON_BOOT_BOM_PIN` (Korean), ⚠ (`VersionChange.boot_bom_pin`) only when the version
  differs from the as-is one, never in the "pinned at as-is versions" follow-up.
- Warning: `_warn_no_boot_bom` once per restructure when `parent_manages_spring_boot` and `boot_bom_added` → flag
  `parent-chain-no-boot-bom`, note, journal `parent_chain_no_boot_bom` (roots, target), follow-up `parent_no_boot_bom_followup` (Korean,
  roots + target filled in) placed FIRST in the Handover's "From here on" (prefix `사내 부모 체인(`, de-duplicated), `attention_codes` +
  `FLAG_HELP` entry.
- Loop: `VersionResolver.resolve` order managed → `boot-bom` (as-is kept when newer, `sbmig.plan.versions.compare_versions`) → as-is →
  repository → unknown; the step applies even when step 1 is skipped because the build reported the version missing; prompt line
  `target Boot BOM: manages g:a at <v>`; journal `source: "boot-bom"`.

## v2.1t — BOM imports and in-house versions reference the parent chain's own properties (2026-10-30)

User (no rights on base-parent; its properties are `spring-boot.version`, `spring-framework.version`, `spring-cloud.version`,
`spring-data.version` — confirmed names — and the in-house `*-reuse` version lives in artifacts.yml): "어차피 에이전트 기동할 때
pom.xml 리드 가능하잖아? 그걸 불러와서 세팅하면 더 유연하지 않을까 … 사내 라이브러리 버전은 네이밍이랑 변할 수 있으니까 … 안 되면 spring 쪽만".
The parent-chain facts (v2.1p) already hold every property the chain defines; v2.1t uses them to WRITE, not only to clean.

### `artifacts.parent_manages_spring_boot: true | false | auto` (default **auto**)
- `auto`: the chain facts decide — the chain imports / inherits `spring-boot-dependencies` → behave as `true`; it does not
  (`boot_bom_added`) → behave as `false` with the property-referencing import below. Explicit `true` / `false` keep their meaning
  (`true` with a chain that has no BOM still gets the v2.1s warning).
### BOM imports written by the restructure (`_boot_bom`, root pom's `<dependencyManagement>`, first entry; every module inherits)
- Spring Boot: `org.springframework.boot:spring-boot-dependencies:${<prop>}` (type pom, scope import) where `<prop>` is the first
  name of `layout.bom_version_properties.spring_boot` (default `[spring-boot.version, spring.boot.version, springboot.version,
  spring-boot-version]`) that the chain defines; none → the literal `target.spring_boot_version` as today. Idempotent: an existing
  import of the same artifact is re-pointed to the property (or left when it already references it).
- Spring Cloud: when the project uses `org.springframework.cloud:*` (analyzer library `spring-cloud`) and neither the chain nor the
  project manages it (chain facts / project dependencyManagement), `org.springframework.cloud:spring-cloud-dependencies:${<prop>}`
  with `layout.bom_version_properties.spring_cloud` (default `[spring-cloud.version, spring.cloud.version, spring-cloud-version]`);
  no such property → the release of `knowledge/config/spring_cloud_bom.yml` matching `target.spring_boot_version` (Boot 4.1 →
  2025.1.x; the file names the pairing and is the single place to update), else a Handover follow-up and nothing written.
- Each import → pom-verify row (`BOM imported: <g:a> ${<prop>} (property of <defining pom>)` / `… <literal> (no property in the
  parent chain)`), note, journal `bom_imports`.
### In-house artifact versions (`_bump_optionals`, mandatory / optional specs)
- For a mandatory / optional spec, the version written is, in order: the spec's `property_name` when the chain defines it →
  the first chain property named `<artifactId>.version` / `<artifactId>-version` / `<artifactId stem>.version` (stem = artifactId
  without a trailing `-starter` / `-bom` / `-parent`) → the spec's `version` (literal) → `null` (managed). A property hit is written as
  `<version>${<prop>}</version>` (never the value), so a renamed or re-numbered property on the parent follows automatically; note
  `optional <g:a>: version ${<prop>} (property of <defining pom>)`. Journal `optional_bumped` entries show `${prop}`.
### Prompt facts and rule (fix loop)
- "Dependency versions" gains, once per case, the chain's `*.version` properties relevant to the case's artifacts (name = value,
  defining pom; ≤ 20 lines, families matched by artifactId stem or group suffix: `spring-boot`, `spring-framework`, `spring-cloud`,
  `spring-data`, in-house groups), and rule 14 gets: "When the parent chain defines a version property for the library's family,
  write `${that property}` instead of a literal version." `_fill_versions`: an added in-house dependency (in-house groups per
  `is_in_house`) whose family property the chain defines gets `${prop}` instead of the resolver's literal.
### Fixture
`legacy-external-parent`'s stand-in parent gains the properties `spring-boot.version` (= the Boot target), `spring-cloud.version` and
`legacy-parent-test.version`; with the local e2e stand-ins the chain already carries the Boot BOM (auto → true), so the Boot import
is not written there — a unit test with a chain lacking the BOM covers `${spring-boot.version}`; the Cloud import is covered by a
unit test (the analyzer's `spring-cloud` library + a chain property).

### As built (2026-10-30)
- `artifacts.parent_manages_spring_boot: bool | "auto"` (default `auto`; `default.yml` header note, `artifacts.example.yml` documented);
  `Restructurer._manages_boot(c)` resolves it once: explicit bool as is; `auto` → `ParentChainFacts.reaches_boot_bom` (inherited or
  imported `spring-boot-dependencies`; `boot_bom_added` as the fallback signal), no chain facts at all → the old default `true`; one
  note; every former read (`_boot_bom`, `_fix_boot_plugin`, both plugin-version sites, `_fallback_capture`, the v2.1s name exclusion)
  uses the effective value, exposed as `RestructureResult.parent_manages_spring_boot_effective` and in the `restructure` journal; the
  v2.1s warning fires only for an explicit `true`.
- Boot import (effective false): in the pom `_boot_bom` already handles (each role-module pom, or the root when it is not an
  aggregator — with role parents the modules do not inherit the root), `${<prop>}` = the first `layout.bom_version_properties.spring_boot`
  name the pom's OWN root chain defines (`ParentChainFacts.root_properties`, per root — a module on comp-jar cannot resolve a property only
  base-parent's chain defines), else the literal target; an existing import is re-pointed, idempotent; the spring-boot-maven-plugin
  version follows the same property. Spring Cloud (`_cloud_bom`): analyzer library `spring-cloud`, no `org.springframework.cloud:*` in the
  chain's own managed entries and no project pom managing it → `${spring-cloud.version}`-style when the chain defines it, else the
  pairing of `knowledge/config/spring_cloud_bom.yml` (`sbmig/build/cloud_bom.py`), else a follow-up only. Verified pairing: Boot 4.1 →
  2025.1.4 (Central lists 2025.1.0 … 2025.1.4; no GA train on Boot 4.1 yet — 2026.0.0 is RC1 on Boot 4.2 — re-check when 2026.0 ships),
  4.0 → 2025.1.4, 3.5 → 2025.0.3, 3.4 → 2024.0.3, 3.3 / 3.2 → 2023.0.6, 3.1 / 3.0 → 2022.0.5. `RestructureResult.bom_imports`, row
  `BOM imported: g:a ${prop} (property of <pom>)` / `… <literal> (no property in the parent chain)`, note, journal `bom_imports`.
- In-house versions: `_spec_property` / `_spec_version` — `property_name` → `<artifactId>.version` / `-version` / `<stem>.version` →
  literal → null; a hit is written `${prop}` (floor: a newer literal is kept); a project-defined property of the same name is removed
  (it would shadow the chain's); in `_bump_optionals` (trigger-added optionals too) and the mandatory add / align.
- Loop: `artifact_stem`, `version_property_matches` (name ends in `.version` / `-version`; starts with the artifactId / stem, or the
  group's family keyword — `spring-boot` / `spring-cloud` / `spring-data` incl. sub-groups, `spring-framework` only for
  `org.springframework`; in-house: the artifact stem starts with the property base + `-`, so `xxx-reuse-web` ↔ `xxx-reuse.version`);
  `_chain_property_facts` lines `parent chain property: <name>=<value> (<pom>)` (≤ 20, after the version facts); rule 14 sentence +
  pointer in both prompts; `_fill_versions` writes `${prop}` for an added in-house dependency (version-less or literal; chain-managed →
  version removed first), journal `source: "chain-property"`, `property`, `replaced` — this rule also reaches the configured mandatory /
  optional coordinates. Known limit: the loop's chain facts merge all roots (the restructure scopes per root).
- Fixture `legacy-parent` 1.0.0: `spring-boot.version=4.1.0`, `spring-cloud.version=2025.1.4`, `legacy-parent-test.version=9.9.9`
  (reinstalled); `legacy-external-parent` post-check: no `spring-boot-dependencies` import (auto → true with the local stand-ins).
  Note: with `plan.compile_after_rewrite: false` a `--stop-after restructure` run shows `attn=incomplete` instead of `tests-not-compiled`.
- Correction after the e2e (`boot2`, local chain = `spring-boot-starter-parent`): the Boot BOM's own `lombok.version` /
  `commons-lang3.version` had counted as "chain-defined" and the optionals came out as `${lombok.version}` — at the company such a
  reference would not resolve. Rule: a property is referenced (restructure `_spec_property`, the BOM-import property, the shadow removal;
  loop facts and `_fill_versions`) only when its DEFINING pom is one of the company's own chain poms (defining pom's groupId = a root's
  groupId or `is_in_house`), never an `org.springframework*` pom nor the Boot add-on — `ParentChainFacts.company_properties` /
  `company_property_sources`; `properties` stays complete for the override-name step. With the local stand-ins no company property
  exists, so the optionals keep their literal (3.17.0) as before v2.1t.
- Built: `parent_chain.is_company_group()` (a root's group or under it, or a mandatory / optional spec's group; anything under
  `org.springframework` excluded even in the root slot), `company_properties` / `company_property_sources`; the Boot add-on's
  properties never enter `properties`; the restructure's `_chain_names_for` (per root) and the loop's `chain_company_properties(facts,
  cfg)` (fallback filter when the field is empty) read only those. Follow-up not done: the loop could use `root_properties` to write
  `${prop}` only when the module's own root chain defines it (the restructure already scopes per root).

## v2.1u … v2.1y — follow-through of the prompt review (2026-10-31)

Source: `docs/v2/PROMPT-REVIEW-2026-10-31.md` (Korean consolidation; §6 increments, §7 decisions) and the raw reports
`docs/v2/reviews/2026-10-31/review-{A..E}-*.md`. The user (2026-10-31): "쭉 일자로 진행하자! 주말에 해서 월요일에 가서 돌려보면
되니까!" — every increment, in one go. The seven §7 decisions were taken by the PM as defaults (each is a config switch or a
documented rule, so the company can turn it back):

| # | decision | PM default | where |
|---|---|---|---|
| 1 | `spring.main.allow-bean-definition-overriding` / `allow-circular-references` for converted legacy projects | **true, written by the restructure** (as-is Framework behaviour); duplicate bean ids in the Handover | `layout.legacy_context.allow_bean_overriding` / `allow_circular_references` |
| 2 | review `<script>` | **edits-only** (the script section and helper table leave the prompt; the parser still accepts `<script>`) | `review.ask_script: false` |
| 3 | `fix-policy.md` | **deleted** (never reached the LLM; `always:` notes are gone; the design README's "always included" sentence corrected) | `knowledge/notes/`, `tests/fix/test_fix_notes.py` |
| 4 | a javax-only filter / servlet in the generated `WebXmlRegistrations` | **allowed to be commented out with `MIGRATION-TODO`** — the file is sbmig's own, not the owners' code; the pom fix (jakarta build / classifier) comes first | note `servlet-to-boot.md` |
| 5 | JSPs collected under `WEB-INF/jsp/` | **kept** (company rule 9c); the legacy-config copies get their view-resolver prefix rewritten like `spring.mvc.view.prefix` | restructure D10 |
| 6 | webmvc starter for `spring_legacy` | **only with web evidence** (web.xml / spring-webmvc / servlet-api / @Controller / JSP), else `spring-boot-starter` | `layout.legacy_context.web_starter_by_evidence` |
| 7 | recipe sets | **conditional, like the Security chain** ("쓰는 경우만"): `spring_batch_6` with `spring-batch`, `hibernate_7` with native `hibernate`, `servlet_jakarta_10` for every `servlet_tomcat` | `openrewrite.recipes.*`, planner, catalog `sets:` |

Shared contracts written by the PM before the fan-out (already in the tree): `FixContext.layout_facts`, `RestructureResult.{starter,
web_evidence, xml_imports, xml_skipped, legacy_properties_written, scope_restored, source_encodings_written, webinf_locations_rewritten,
beans_removed, duplicate_bean_ids, conversion_facts}`, `ScriptProposal.blocked_reason`, `ReviewIssue.unsupported`, `BlockReason.NEEDS_OWNER`,
`StoredScript.non_idempotent`, `LegacyContextConfig` (`layout.legacy_context.*`), `ScriptConfig.max_declines_per_case` /
`idempotency_check`, `ReviewConfig.ask_script` / `invariant_check` / `save_input`, `RecipeSetConfig.spring_batch_6` / `hibernate_7` /
`servlet_jakarta_10`, `loop.blocked_policy.needs_owner: 0` — all mirrored in `config/default.yml`.

### v2.1u — prompt and note text (no new plumbing)

**Fix prompts (`sbmig/fix/prompts/*.md`, `sbmig/fix/fixer.py` loaders, `sbmig/fix/context.py`)**
- The target is rendered, never written: `{{ target }}` in `script_system_v1.md` / `verify_system_v1.md` is filled
  from `target_text(cfg)` (JDK / Boot from `cfg.target`); the intro no longer names "Framework 7, Security 7, Hibernate 7" nor "the Boot
  4.0/4.1 recipes" — it says "the recipes this run executed are listed under Plan notes". Rule 8: "the target Boot line's relocated
  packages (see the Migration notes)"; its Jackson sentence becomes two lines pointing at the note `jackson-2-3.md` (rule A/B/C/D there).
- Rule 10 adds: "… or a Migration note's `fix-loop:` section" (a note may ask for a port rule 14 would otherwise forbid).
- Rule 14 replaced by the ~1.35K-character text of report B (resolution order chain-managed → target Boot BOM → as-is → repository →
  `null`; the `target Boot BOM:` line explained; no "look releases up yourself").
- Output format, last line: "If the case cannot be fixed inside the project — a library with no jakarta build, a framework the owning
  team must replace, an artifact the repository does not have — answer `<analysis>` and `<blocked>one sentence naming the blocker and who
  decides</blocked>`; no `<edits>`, no `<script>`." The parser fills `ScriptProposal.blocked_reason`. Rule 9's wording stays.
- Deleted: the repeated `<verify>` explanation, the restated intro items and the "Script or no script" paragraph (B-L2, ~1.4K chars);
  `verify_system_v1.md` "The build passes" → "The build has not been re-run yet".
- `profile_summary`: the header line is labelled `as-is (before sbmig): …`; `javax-imports=` and advisory ids are dropped (the
  jakarta rewrite already happened; advisories arrive as hints). `summarize_script_attempt` shortens the analysis with
  `textwrap.shorten` (300 chars, "…").
- Plan notes in the prompt: only lines starting with `transform`, `restructure`, `warning`, `msa`, `followup` (B-L1); `case …
  (confidence …)`, `optional artifacts: bumped`, `review:`, `advisory: … → guide` stay out.

**Structure review prompt (`sbmig/review/prompts/review_v1.md`, `sbmig/review/inputs.py` rule lines)** — see v2.1v for the fact lines;
the text fixes are: packaging rule 3 / the layout line "war (JSPs served by an external Tomcat)" → "executable WAR (java -jar runs the
embedded Tomcat; deploys to an external Tomcat too); `spring-boot-starter-tomcat` / `tomcat-embed-jasper` stay `provided` (packaged
under WEB-INF/lib-provided), a Boot as-is / MSA `war` keeps its packaging; report packaging only when it deviates from these"; the
MSA line states the code's rule (every service root → boot parent + mandatory; library roots → comp parent, no mandatory; names from
`profile.services`); each mandatory spec line ends `→ <dependencyManagement>` only for `scope: import`, else `→ <dependencies>`, plus
"with role parents a module does not inherit the root pom's declarations"; the optional line says v2.1n / v2.1t (`${<artifact>.version}`
when the chain defines it, as-is newer literal kept, `when_present` adds / `replaces` removes); the "What the restructure did" version
decisions are final (no re-pinning to "same major"); the boot module's starter is named and "do not add Security / Batch / JPA starters";
the user prompt's last line asks for `<analysis>`, `<solution>`, `<edits>`.

**Notes (`knowledge/notes/`, `index.yml`, `knowledge/libraries/advisories.yml`)**
- W1–W12 of the review corrected (HttpComponents `setConnectTimeout` removed / `setReadTimeout` kept; JSP = executable WAR; jboss-logging
  runtime scope → add `org.jboss.logging:jboss-logging` version null; leading `**` accepted by 7.0.8; Boot 4.1 "removed" list; `ant_path_matcher`
  exists; versioned XSDs resolve; commons-collections recipe does not run; mybatis-spring 4.x line; JUnit vintage paragraph deleted;
  raw `createQuery(String)` exists; `spring-boot-starter-batch-data-mongodb`).
- `servlet-to-boot.md` rewritten (B-M9): sbmig already moved the JSPs to the front module's `META-INF/resources/WEB-INF/jsp` and made
  the boot module an executable WAR; do not change packaging, JSP location or the provided scopes; A-B1 (javax-only filter in the
  generated `WebXmlRegistrations`: pom fix first, else comment that one `@Bean` out with `MIGRATION-TODO` — decision 4) and A-B2
  (`CommonsMultipartResolver` → delete + `spring.servlet.multipart.max-file-size` / `max-request-size` from the as-is limits).
- Boot 4 package / starter relocations split out of `boot3-to-4.md` / `boot40-to-41.md` into `boot4-packages.md` with `cases: []` (every
  case) keyed on `org\.springframework\.boot\.` / `spring-boot-starter-` / the moved class names, so `spring_legacy` / `servlet_tomcat`
  receive them.
- `fix-policy.md` deleted (decision 3): index entry, `always:` expectations in `tests/fix/test_fix_notes.py`, the design README
  sentence; `ContextPacker` keeps its "always-notes are left out" line (harmless, no always-note exists).
- Deleted-handler wording gone (B-M7): `advisories.yml` jdk-removed-apis (no `sbmig.compat` shim exists; `UseJavaUtilBase64` does NOT
  reach `sun.misc.BASE64Encoder` — OpenRewrite parses on JDK 21 where the class is gone, `docs/ADVISORIES.md` says so — the LLM ports
  it to `java.util.Base64` in the fix loop; MIME output needs `Base64.getMimeEncoder()`), querydsl ("handler querydsl-jakarta-classifier"), `spring-security-7.md`
  "`add-known-dependency` adds it", `jakarta_libraries.yml` header "handler"; a test asserts that none of `sbmig.compat`,
  `add-known-dependency`, `jdk-base64-shim`, `querydsl-jakarta-classifier`, `jakarta-library-floor`, `carry-over-managed-version` appears
  under `knowledge/notes/` or in `knowledge/libraries/*.yml`.
- Literal versions as instructions (maven-plugins.md table, jdk21.md, jakarta.md table, era-libraries.md, ibatis2.md two lines) →
  compatibility facts stay, "use X" becomes "`version: null` / the Dependency versions section / the parent chain property".
- Keywords: the 17 gaps and 5 false positives of report C §4 (`release` → `maven-releases` URL, `layout` → `com.x.layout`, `Optional<`,
  `is deprecated` javac warnings, `Matchers` → `antMatchers`); `jakarta.md` `Filter` → `\b(Filter|FilterChain|FilterConfig)\b`.

### v2.1v — the facts reach the LLM

**Fix loop (`sbmig/loop/fixloop.py`, `sbmig/loop/orchestrator.py`, `sbmig/build/repo_versions.py`)**
- `FixContext.layout_facts` (rendered by the packer as section "Project after the restructure", order 0.5, never truncated, ≤ ~1,200
  chars): built by the loop from the orchestrator's `RestructureResult`s (`self.loop.restructure_results = list(self._restructure_results)`
  next to the merge-conflict handoff) and `cfg.artifacts` / `cfg.layout`. Lines (each only when the value exists):
  `converted by sbmig: <case> → Spring Boot application (company layout)`;
  `boot module <dir>: artifactId <a>, packaging <jar|war (executable, java -jar)>, parent <g:a:v>, starter <starter>`;
  `back module <dir>: every source incl. controllers, parent <g:a:v>`; `front module <dir>: static under <dir>, JSPs under <dir>`;
  `generated: <Application FQCN>, legacy-config/ (N XML imported via @ImportResource: …), <WebXmlRegistrations FQCN>`;
  `XML not imported: …` (xml_skipped); `legacy properties written: …`; `scopes restored: …`; `source encodings written: …`;
  `company artifacts: root parent <g:a:v>; mandatory <keys> declared in <root|modules>; optional in-house artifacts only where already
  used; versions managed by the parent chain — never write a literal for them`; `BOM imported: …` (bom_imports);
  `parents set: …` (≤ 6); Boot as-is / MSA pom-only mode: the parents / mandatory / BOM lines only. Values come from the result and the
  config, never from literals in code.
- `_version_facts` / `_chain_property_facts` also cover the GAVs `_case_libraries` locates on the classpath (compile errors carry no
  `e.artifact`): `as-is build: spring-batch-core 4.3.10 (…)`, `new parent chain: manages spring-batch-core at 6.0.4 (major 4 → 6)`.
- `_package_facts` for `package <p> does not exist` (no javax): the longest-prefix groupId among the chain's / Boot BOM's managed keys →
  `managed artifacts under <group> (add the one that holds <pkg>, version-less): a, b, c` (≤ 25 artifactIds).
- `_verdict_line` (`repo_versions.py`) last branch: "no jakarta build is known on any line — the API changed inside the same major:
  fix the code against the version the parent chain / Boot BOM manages and leave the version" (no `<no_script>`). `_library_hints`: the
  repository candidates + verdict only when the case's errors mention `javax.` (the v2.1d scope) or the artifact is in the javax table;
  for an artifact the `VersionResolver` knows (chain-managed / Boot BOM) one line: `new parent chain: manages g:a at v — the API changed,
  fix the code and leave the version`.
- `other_errors_summary` is cut at a line boundary with `(… N more lines)`; a case that appeared after a script's commit gets the kb hint
  `introduced by script:<case>:v<N> (attempt <id>) — prefer augmenting that script` (E-L9).

**Packer (`sbmig/fix/context.py`)**
- Note selection: a keyword hit in the error text (messages / symbols / locations / raw) counts 3, in the imports / pom text 1; a note
  with 0 error-text hits is taken only when no note has one; `SCRIPT_TRUNCATE_ORDER` starts with
  `other_errors, plan_notes, notes, …` (notes were cut first).

**Structure review (`sbmig/review/inputs.py`, `reviewer.py`)**
- `version_fact_lines(poms, cfg, results)` from `sbmig.build.parent_chain.load_parent_chain_facts(cfg)` / `boot_bom.load_boot_bom_facts`
  (read-only): `parent chain property: <name>=<value> (<defining pom>)` (company properties), `explicit version on a managed artifact:
  <pom> g:a <v> — managed <m> by <pom>`, `Spring Boot artifact on another line: <pom> g:a <v> (chain / target <t>)`, `property shadows the
  parent chain: <pom> <name>=<value> (chain: <value> by <pom>)`; the system prompt says the last two are critical. Normally the restructure
  leaves nothing → zero lines.
- The auto line uses the effective value (`results[].parent_manages_spring_boot_effective`, `bom_imports`): "the parent chain reaches the
  Boot BOM: no Boot BOM import and no Boot version property belongs in the project poms" | "the restructure imported `<g:a> ${prop}`
  (= <value>, defined by <pom>) in <poms> — do not define that property in a project pom".
- "What the restructure did" renders every new `RestructureResult` field (v2.1x list above, `conversion_facts` as `- key: value`) and
  the run notes only when not already in a result (D-M3); `parent_manages_spring_boot_effective`, `non_filtered_extensions`,
  `jsp_jakarta_*` included.
- `review.save_input: true` → the user prompt is written to `<run dir>/logs/review-input.md` (`sbmig.core.workspace.logs_dir(ws)`).

### v2.1w — new notes, recipe sets

- `knowledge/notes/jackson-2-3.md` (rule A: the module is on Jackson 3 → finish the file's move — `JsonProcessingException` →
  `tools.jackson.core.JacksonException`, converters → `JacksonJsonHttpMessageConverter(JsonMapper)`, `Jackson2ObjectMapperBuilder` →
  `JsonMapper.builder()`, never undo the recipe; rule B: a library needs Jackson 2 types → that file stays on 2 + `jackson-databind`
  version null (the Boot 4.1 BOM manages both lines); rule C: no Jackson 3 in the module and `package com.fasterxml.jackson.databind does
  not exist` → add Jackson 2; rule D: never add `spring-boot-jackson2` to a `spring_legacy` project; XML `MappingJackson2JsonView` → Handover).
- `knowledge/notes/spring-batch-6.md` (package moves `item.*` → `infrastructure.item.*`, `core.Job/Step` → `core.job/step`, listeners,
  explorer, repeat; `JobBuilderFactory` → `new JobBuilder(name, jobRepository)`; `.chunk(n).transactionManager(tm)`;
  `ItemWriter.write(Chunk)`; `DefaultBatchConfigurer` gone; runtime items → `fix-loop:` says Handover).
- `spring-framework-7.md` additions (C-M3: `APPLICATION_JSON_UTF8`, removed `HttpStatus` constants, `getStatusCodeValue()`,
  `org.springframework.remoting.*` → TODO / Handover, `OkHttp3ClientHttpRequestFactory`, `@Required`, Theme support, `Assert.notNull(x)`,
  `Log4jConfigurer`, `CommonsMultipartFile`, `NestedIOException`, `OpenSessionInViewFilter`; Spring 4 → 5: `AbstractExcelView`,
  `queryForInt/Long`, `SimpleJdbcTemplate`); Spring Data 1.x → 2+ (`findOne` → `findById(..).orElse(null)`, `PageRequest.of`), Security 3/4
  `authentication.encoding.*` (no hash-compatible replacement → `<verify>`), commons-lang 2 / log4j 1 ("re-declare the as-is coordinate
  with version null; the replacement is the owners' — Handover") in `era-libraries.md`; keywords for all of them.
- Recipe sets (decision 7): planner `_case_buckets` — `spring_legacy` + library `spring-batch` → `rs.spring_batch_6` (bucket
  `framework`, after the Framework set); `spring_legacy` + library `hibernate` (native ORM, not JPA-only) → `rs.hibernate_7`;
  `servlet_tomcat` → `rs.servlet_jakarta_10` in the jakarta bucket; catalog `sets:` + `_RECIPE_SET_NAMES` + gates `has_spring_batch` /
  `has_hibernate`; plan notes name the reason ("spring-batch detected").

### v2.1x — the legacy Spring context at conversion time (`sbmig/restructure/`, `sbmig/analyze/`, `knowledge/config/`)

All behind `layout.legacy_context.*` (default on); every switch off = the Handover sentence only. Facts go into the new
`RestructureResult` fields; wording follows v2.1q (executable WAR, `java -jar`).

| # | what | how |
|---|---|---|
| D2 | which XML the Boot context imports | web.xml `contextConfigLocation` (context-param = root, DispatcherServlet init-param = servlet) resolved to files (`classpath*:`, `/WEB-INF/…`, comma / whitespace lists, `*` globs) → only those go into `legacy-config/` + `@ImportResource`; files reached by `<import resource>` are copied next to them (relative paths kept) but not listed; `src/test/**` never; no web.xml / nothing resolved → every `xml_bean_configs` file as before. `xml_imports` / `xml_skipped` filled; duplicate `id=` across imported files → `duplicate_bean_ids` + Handover |
| D1 | overriding / circular | boot `application.properties`: `spring.main.allow-bean-definition-overriding=true`, `spring.main.allow-circular-references=true` with a `# sbmig:` comment naming the as-is behaviour (decision 1); `legacy_properties_written` |
| D3 | EUC-KR / MS949 XML | the copy into `legacy-config/` is a byte copy (`_Ctx.copy`, not `c.write(c.ws.read())`); the XML declaration's encoding stays true |
| D4 | skipped web.xml filters with init-params | `web_xml_rules.yml` rows get `properties:` (init-param → Boot key: `CharacterEncodingFilter` `encoding` → `spring.servlet.encoding.charset`, `forceEncoding` → `spring.servlet.encoding.force` — the `server.servlet.encoding.*` names are deprecated=error in Boot 4.1; `HiddenHttpMethodFilter` → `spring.mvc.hiddenmethod.filter.enabled=true`; `FormContentFilter` → `spring.mvc.formcontent.filter.enabled=true`; `<multipart-config>` → `spring.servlet.multipart.max-file-size` / `max-request-size` / `file-size-threshold`) and `requires:` (D11: `DelegatingFilterProxy` is skipped only when its filter-name is `springSecurityFilterChain` AND the project uses spring-security; otherwise a `DelegatingFilterProxyRegistrationBean("<name>")` is generated; `OpenEntityManagerInViewFilter` skipped only when JPA is used, else Handover) |
| D5 | JNDI DataSource | `<jee:jndi-lookup>` / `JndiObjectFactoryBean` beans in the copies: a `<!-- MIGRATION-TODO (sbmig): … -->` comment right above the bean, the Handover sentence "JNDI DataSource `<id>` has no JNDI in embedded Tomcat: remove the bean and set spring.datasource.* (spring-boot-starter-jdbc present: yes/no)"; the bean itself is left (the owners decide) |
| D6 | `/WEB-INF/…` property locations | in the copies: `PropertyPlaceholderConfigurer` / `<context:property-placeholder location>` / `@PropertySource` values `/WEB-INF/x` → `classpath:asis-webinf/x` (the stash already lands there); the analyzer counts such files as definitions (no `__SET_ME__` for keys they define); `webinf_locations_rewritten` |
| D7 | `${profile}` locations | `classpath:config-${env}.properties` → `classpath*:config-*.properties` glob in the copies (`profile_globs`) + Handover line naming the as-is placeholder |
| D8 | `<mvc:default-servlet-handler/>` | `server.servlet.register-default-servlet=true` + a sentence |
| D9 | XML `CommonsMultipartResolver` | removed from the copy, `spring.servlet.multipart.*` from `maxUploadSize` / `maxInMemorySize`; `beans_removed` |
| D10 | `@EnableWebMvc` / `WebMvcConfigurationSupport`, `<mvc:resources>` | analyzer flag (`SourceStats` via `conversion_facts["enable_web_mvc"]` — `SourceStats` has no field; the restructure scans `comp` sources) → no `spring.mvc.view.*` written, Handover instead; the copy's `InternalResourceViewResolver` prefix rewritten like `spring.mvc.view.prefix` (decision 5 keeps the JSP move); `<mvc:resources mapping=… location="/resources/">` → sentence naming the front `static/` path |
| D12 | `*.do` mappings, `<jsp-config>`, `WebApplicationInitializer`, WAR context root, log4j 1, Security XML, `spring.profiles.active` context-param | Handover sentences (report A §2b wording); `*.do` → "`spring.mvc.servlet.path` is a prefix only; the as-is `*.do` suffix mapping needs `DispatcherServlet` registration on `*.do` — WebXmlRegistrations does it when generated, else …" |
| E-M6 | `provided` scopes | after the recipes a dependency whose as-is declaration (`logs/effective-depmgmt.json` capture, else the as-is pom text before the rewrite commit) had `scope provided` (javax.servlet-api → jakarta.servlet-api …) gets `provided` back; `scope_restored`; pom-verify row `scope restored: provided (as-is)` |
| E-M9 | starter | `web_evidence` from web.xml / `spring-webmvc` / servlet-api / `@Controller` / JSP; `spring_legacy` without evidence → `spring-boot-starter` (note + Handover "no web evidence: no embedded Tomcat"); `starter` filled in every layout run |
| D-M7 | source encoding | a module whose sampled `.java` files are MS949 / EUC-KR and whose in-project pom chain declares no matching `project.build.sourceEncoding` → the restructure writes it into the module pom (`source_encodings_written`); the review's encoding check then sees nothing |

Stale wording fixed in the same increment: `Application.java.j2` ("Packaged as a WAR for an external Tomcat" → executable WAR,
`SpringBootServletInitializer` for the external deployment), `web_xml_rules.yml` rows 13 / 14 / 17, `webxml_registrations.py:252-255 /
374 / 387`, `restructurer.py:2507` (the `@ServletComponentScan` claim must follow the generated file, not the flag), `:2933`
(context.xml advice), `:3131`, `externalized.yml:53`; the Handover no longer says "registered via @ServletComponentScan" for web.xml
declarations; the view-prefix follow-up is written only when the project has `@Controller`s or `spring-webmvc`; duplicate follow-ups
de-duplicated.

Fixture `fixtures/projects/spring5-legacy-startup` (new): a Spring 5 XML web app (JDK 8, javax) with root + servlet XML named by
`contextConfigLocation`, an `<import>`ed XML, a `src/test` XML, an EUC-KR XML, `CharacterEncodingFilter` (EUC-KR, forceEncoding), a
`<jee:jndi-lookup>` DataSource, `/WEB-INF/config/app.properties` + `classpath:config-${env}.properties` placeholders,
`<mvc:default-servlet-handler/>`, `<mvc:resources>`, XML `CommonsMultipartResolver`, a non-security `DelegatingFilterProxy` (xss filter
bean), `*.do` mapping, `spring.profiles.active` context-param, a log4j 1 `log4j.properties`, `javax.servlet-api` provided, two JSPs,
MS949 Java sources in one package. `expected.json` asserts the build, the `RestructureResult` facts (journal / report rows), the Handover
sentences and `scope restored`; `replay/` answers the review (and any fix case) by hand.

### v2.1y — loop and review policy

- **Declined answers (E-M1)**: an answer with `blocked_reason`, or with neither edits nor a script and only `<analysis>`, is classified
  `declined` (journal event `declined`, attempt outcome `blocked`, `FixAttempt.notes`); after `script.max_declines_per_case` such answers
  the case is BLOCKED `needs_owner` (Korean suggestion = the blocker sentence + "소유팀 결정 필요"); cases whose errors are in the same files
  or name the same artifact are blocked in the same round as "same cause as <case>" without an LLM call.
  `loop.blocked_policy.needs_owner: 0`.
- **Idempotency (E-H3)**: `script.idempotency_check: true` — after the first run's commit the same script runs once more; a second change
  → `discard_uncommitted()`, the stored version gets `non_idempotent: true` (+ a note on the attempt, journal `script_non_idempotent`),
  `CaseStore.best(...)` never selects such a version; the attempt's own result stays as scored.
- **Review invariant check (D-M4)**: after the edits apply (before the commit): no `<properties>` entry whose name the company chain defines
  (`ParentChainFacts.company_properties`), no explicit `<version>` on a chain-managed artifact that had none before the edits, no `<parent>`
  that is neither a configured parent nor an in-project pom, no removal of a mandatory dependency; a violation restores the tree and asks
  once more with the reason (the second answer is checked the same way; a second violation → handed over as a critical issue).
- **Review encoding check (D-M7)**: an issue counts as resolved only through an in-project chain pom (role parents: a module does not
  inherit the root); recomputed after the edits.
- **Unsupported review issues (E-M5)**: `parse` post-processing marks an issue whose text names a version / property / coordinate that
  appears nowhere in the review material (`ReviewIssue.unsupported`); such issues stay in the report with "(sbmig check) not in the
  material", are never applied and never reach the Handover; `expected.json` can assert `issues_not_contain` (`scripts/e2e.py`).
- **Edits-only review (decision 2)**: `review.ask_script: false` removes the script paragraph, the `<script>` example and the helper table
  from the system prompt (`system_prompt(cfg)`); `ReviewInput.render()`'s last line matches; `parse_review_answer` still reads `<script>`
  (older replays).

### As built (2026-10-31, agents N / P1 / P2 / K; R and X below)
- **Notes (N)**: 15 notes corrected, `index.yml` rebuilt from the front-matter, `fix-policy.md` deleted (no `always:` note remains — the
  packer's filter stays as dead code), new `boot4-packages.md` (every case; the relocation parts moved out of `boot3-to-4.md`, which keeps
  `cases: [spring_boot, msa]`), `jackson-2-3.md` ("Jackson 2 → 3 under the target Boot BOM (decision rules A-D)" — rule 8 cites that
  title), `spring-batch-6.md` (with a `fix-loop:` section). Verified against the local jars: `setConnectTimeout` removed in 7.0 (javap
  spring-web 7.0.8), jboss-logging `runtime` in hibernate-core 7.4.1, `PathPatternParser` 7.0.8 accepts `/**/*.do`, Boot 4.1.0 BOM still
  has `spring-boot-jackson2` / `HttpMessageConverters` / `-starter-web` (deprecated), `DaoAuthenticationProvider` no-arg ctor gone in Security
  7.1, Spring 7 ignores `javax.annotation` / `javax.inject`. **Correction of the contract text above**: `UseJavaUtilBase64` never reaches
  `sun.misc.BASE64Encoder` (OpenRewrite parses on JDK 21 where the class is gone) — the LLM ports it in the fix loop; `advisories.yml`,
  `jdk21.md`, `docs/ADVISORIES.md` and the fixture comments say so now. Advisory `notes:` pointers: log4j1 → era-libraries.md, commons-lang2
  added, struts2 / egovframe dropped (no content). Not verified locally: `OAuth2ClientHttpRequestInterceptor`, Batch 6 `BATCH_JOB_SEQ`, the
  Security 3/4 `password{salt}` format, what `spring.jackson.use-jackson2-defaults` does.
- **Fix prompts (P1)**: `load_script_prompts(version, extra_imports, timeout_s, target="")` / `load_verify_prompt(version, target="")`
  render `{{ target }}` (empty = `TargetConfig` defaults; `LLMFixer` passes `target_text(cfg)`); no prompt file carries a target
  literal outside the placeholder. Rule 14 (facts authoritative in the order `new parent chain:` → `target Boot BOM:` → `as-is build:`;
  `parent chain property:` → `${name}`; `managed artifacts under <group>` → add the one holding the package, version-less; otherwise
  `"version": null`); rule 3 allows `p.replace` on exact plugin XML; rule 10 names the note `fix-loop:` sections; rule 12 absorbs "Script or
  no script". `<blocked>`: `ScriptProposal.blocked_reason` non-empty exactly when the tag is present (fence-stripped, ≤ 400 chars,
  `BLOCKED_MAX_CHARS`; the tag is masked so a `<script>` inside it never opens a script); blocked-only answers return `code == ""`,
  `edits == []`, `error == "blocked: <reason[:200]>"` after ONE call (no feedback / context round); an answer with `<blocked>` AND edits
  keeps both (`error == ""`); analysis-only without the tag keeps `error == "no <script> block"` / `"no <edits> block"`; journal
  `llm_script` carries `blocked_reason`. Layout facts: section "Project after the restructure" (`LAYOUT_FACTS_TITLE`), order 0.5 (after
  "Case", before the errors), never truncated, counts as fixed text, lines rendered `- <line>`. Header `as-is (before sbmig): case=…`;
  only the import-only advisory markers (rules without coordinate patterns, e.g. `jdk-removed-apis`) are dropped from `libs=` — real
  libraries with advisories (`hystrix:2.2.10`) stay. `PROMPT_PLAN_NOTE_PREFIXES = ("transform", "restructure", "warning", "msa",
  "followup")` (also drops `compile:` / `verify:` / `advisor…`). `NoteEntry.score_parts(error_text, context_text)` (3 / 1); notes with an
  error-text hit win when any has one; `score(text)` unchanged for the index tests. The user templates' closing line deliberately does
  not mention `<blocked>` (the system prompt's output format does).
- **Fix loop (P2)**: new `sbmig/loop/layout_facts.py` (budget **1,500** chars — the company-artifacts line plus the front paths already take
  ~1,170; Boot as-is / MSA get the artifacts / BOM / parents lines only); the orchestrator hands `restructure_results` + modes to the loop
  in `_step_restructure` and after the loop is created (both engines), journal `layout_facts`. `_verdict_line` last branch reworded; the
  repository list + verdict only when an error mentions `javax.` or the library is in the javax table; a chain- / BOM-managed library gets
  one line `new parent chain: manages g:a at v (major X → Y) — the API changed, fix the code and leave the version`; `_version_facts` /
  `_chain_property_facts` also cover the located classpath GAVs; `managed artifacts under <group> (add the one that holds <pkg>,
  version-less): …`; `other_errors` cut at a line with `(… N more lines)`; kb hint `introduced by script:<case>:v<N> (attempt <id>) —
  prefer augmenting that script`. **Declined**: `blocked_reason`, or no code + no edits + analysis only (transport errors, protocol
  garbage, context requests and broken edits exempt) → outcome `blocked`, note `declined: …`, journal `declined`; an answer with
  `<blocked>` beside edits is declined too (edits not applied, noted); after `script.max_declines_per_case` (≤ 0 = off) the case is
  BLOCKED — **the triage classifier's specific reason wins when it has one** (`external_javax_api`, `external_api_incompatible`,
  `artifact_not_found`, `plugin_incompatible`; the artifact attribution and the composed suggestion stay, the LLM's sentence goes into
  the item's rationale as `the LLM declined: …`; errors the classifier does not cover are blocked `needs_owner`), `needs_owner`
  otherwise — suggestion `<sentence> — 소유팀 결정 필요`; never an augment for a declined case; later cases whose error files all lie inside the declined case's files, or that name
  the same artifact, are blocked as `same cause as <case>: …` without a call (journal `same_cause_blocked`). **Idempotency**: the second
  run happens right after the first `ok` run, BEFORE `_fill_versions` and the commit (sbmig's own version fill must not look like a
  second change); a second change is undone from a byte snapshot, `CaseStore.mark_non_idempotent` (field `CaseScript.non_idempotent`),
  journal `script_non_idempotent`; `best()` / `stored_best` /
  `stored_next` skip such versions, `latest()` still offers them as an augment base; the `sbmig-version` / `sbmig-resources`
  pre-tries never run it; every successful script run logs `fix script ok …` twice.
- **Recipe sets (K)**: buckets jdk21 → jakarta (`RemovalsServletJakarta10` right after `JavaxMigrationToJakarta` for every
  `servlet_tomcat`, `pure_java` with `javax.servlet` imports, and `spring_legacy` only when the Framework 7 set is empty — the Framework
  recipe already includes it) → framework → security → **batch** (`spring_legacy` + library `spring-batch`) → **hibernate** (`spring_legacy`
  + library `hibernate`: `hibernate-core` / `hibernate-entitymanager` / `org.hibernate.orm:*` or `org.hibernate.{Session,SessionFactory,
  query,type,cfg,annotations}` imports; JPA-only projects — `jpa` / `spring-data-jpa` only — get nothing) → boot → junit5 → extra; Boot
  projects unchanged (the umbrella covers all three); plan notes `+ Spring Batch 6 set … (spring-batch detected)` etc.; catalog `sets:` +
  gates `has_spring_batch` / `has_hibernate`; `v2_recipe_scope` lists the sets (rewrite-spring 6.37.0, rewrite-hibernate 2.25.0,
  rewrite-migrate-java 3.42.0). `docs/RECIPES.md` has the rule.
- **Structure review (R)**: `review_v1.md` rewritten (checks 3–8, "the restructure's version decisions are final", the new critical items);
  the script parts sit between `<!-- script:begin -->` … `<!-- script:end -->` and leave the prompt unless `review.ask_script: true`;
  `ReviewInput.render()` and the
  retry texts ask for `<analysis>`, `<solution>`, `<edits>`; `parse_review_answer` still reads `<script>`. Fact lines
  (`version_fact_lines`): `parent chain property: <name>=<value> (<pom>)` (company `*.version` properties, ≤ 20), `explicit version on a
  managed artifact: <pom> g:a <v> — managed <m> by …` (a pin NEWER than the managed version is left out — that is the restructure's
  `gte` decision — as are in-house specs and the versions the restructure pinned), `Spring Boot artifact on another line: …` (critical),
  `property shadows the parent chain: …` (critical; `project.build.sourceEncoding` never), `container API declared compile …` (warning),
  `repackaged module used as a dependency: … spring-boot-maven-plugin repackage without a classifier` (critical); the Boot rule line comes
  from the effective value ("the parent chain reaches the Boot BOM: no Boot BOM import and no Boot version property belongs in the
  project poms" | "BOM import by the restructure: `g:a` `${prop}` (= value, defined by …) — never define `prop` in a project pom"); the
  rules section never contains the target literal; the MSA line follows `_msa` (with / without role parents); mandatory specs end `→
  <dependencies>` or `→ <dependencyManagement> (BOM import)`; optional wording v2.1n/t; the boot starter line; every new `RestructureResult`
  field in "What the restructure did"; run notes only when no result already says it.
  `review.save_input` → `<run dir>/logs/review-input.md`. **Invariant check** (after the edits apply, on the changed poms): (a) a company
  chain property named `*.version` (not `java.version`) added or changed — limited to `*.version` because the chain may define
  `project.build.sourceEncoding`, which the review must be able to set; (b) an explicit `<version>` on a chain-managed / imported-BOM
  artifact that had none (in-house specs and `${company prop}` exempt); (c) a `<parent>` that is neither a configured parent (with its
  version) nor a project pom; (d) a mandatory dependency removed → restore + one retry naming the violation, a second violation hands the
  critical issues over. **Unsupported issues** (`parse.mark_unsupported`): versions checked against the whole prompt plus every pom in
  full (a pom cut for the budget never causes a false mark), property names against everything except "What the restructure did"
  (so "property X is left" is caught when the restructure removed X); a marked issue keeps its severity with the prefix "(sbmig check) not
  in the material: ", is never applied (edits carrying the missing token are dropped), never reaches the Handover (`sbmig/report/handover.py`
  / `markdown.py`), and the orchestrator's `_review_entries` records it as a note instead of "fixed by the review". **Encoding check** counts
  only an in-project chain pom and is recomputed after the edits. `needs_owner`:
  attention code `owner-decision` (before the `blocked:*` codes, `sbmig/report/result_line.py`), two Korean i18n rows (the "same cause as …"
  sibling text and `compose_suggestion`'s fallback in `sbmig/loop/blocked_registry.py`).
- **Restructure (X)**: new `sbmig/restructure/legacy_context.py` (pure XML / location helpers), `sbmig/restructure/legacy_apply.py` (a
  mixin `Restructurer` inherits), `knowledge/config/legacy_context_rules.yml` (class, element and Boot-property tables, the javax →
  jakarta scope successors), `web_xml_rules.yml` rows with `properties` / `set` / `requires` / `otherwise` / `register_requires` /
  `followup`; journal events `legacy_context` (counts) and `legacy_xml_imports`; `conversion_facts` keys: contexts, jndi_beans,
  profile_locations, resources_locations, view_prefix_rewritten, default_servlet, delegating_filter_proxies, dispatcher_mappings,
  jsp_config, log4j1_files, enable_web_mvc, web_initializers, security_xml. D2: root `contextConfigLocation` + each DispatcherServlet's own
  entry (or the Spring default `<servlet-name>-servlet.xml`); `<import>`ed files copied next to their importer, an absolute / escaping
  import rewritten to the copy; D3 byte copy (post-checked byte-equal); D6 copies the WEB-INF file to `asis-webinf/` itself when the front
  move did not, the analyzer (`sbmig/analyze/properties.py`) counts those files as definitions and the placeholder only that location used
  is no longer written; **D7 departure**: a `${…}` location becomes a `classpath*:` glob only when exactly ONE packaged file matches —
  with several (config-dev / config-prod) a glob would merge the environments, so the location stays and the sentence names the key;
  placeholders Boot can resolve (a default value, or a key Boot gets a value for) are left alone; D9 limits `10MB` / `1MB` from the bean,
  a Java `new CommonsMultipartResolver` gets a sentence only; D10 the copy's view prefix rewritten, `<mvc:resources>` location gains
  `classpath:/static/<dir>/`; **D11 departure**: `OpenEntityManagerInViewFilter` is skipped only when the as-is poms already carry the Boot
  JPA starter — JPA without it keeps the filter registered as-is (lazy loading in JSPs keeps working), no JPA → sentence; **`*.do`
  departure**: the generated `WebXmlRegistrations` does not register on `*.do` — Boot's DispatcherServlet on `/` receives those requests
  and the sentence says exactly that; E-M6 restores `provided` unless another as-is pom declared the artifact compile; E-M9 a `spring_legacy`
  without web evidence gets `starter_by_case.pure_java`; D-M7 samples with `review.inputs.source_encodings`, a module mixing MS949 and
  UTF-8 gets a sentence only; pom-verify rows `scope restored: provided (as-is): g:a (was compile)` and `source encoding written: …`
  (orchestrator). Fixture `spring5-legacy-startup`: as-is build JDK 8 + `-Dfile.encoding=MS949`, no fix script; euckr-legacy: the
  restructure writes the encoding, the review answers `ok`.

## v2.1z — the v2.1i switches removed, generous ladder caps, reactor artifactIds masked in case keys (2026-11-02)

### User decisions (2026-11-02, in this order)
1. **The token-comparison switches go** (v2.1i / 2026-10-29): `fix.mode: edits` ("LLM 직접 수정만" — the LLM answers with edits
   only, no script storage / reuse / augment) and `--no-kb` ("저장 스크립트 안 씀" — a `NullKnowledgeBase` for the run). The fix is
   always the script ladder with the KB.
2. **Generous ladder caps, lowered later** ("여유롭게 잡고 진행하다가 점차 줄여가는 방식"): 15 augments and 20 LLM asks per case and
   run. The company already ran with `script.max_augments_per_case: 15`; the new default matches it.
3. **No project token in a case key**: every reactor module artifactId the Maven log shows is masked as `<module>` in the L1 / L2
   message text — the KB exists for cross-project script reuse, and an artifactId in the L2 makes the case per-project.

### Config keys (`sbmig/core/config.py`, `config/default.yml`)

| key | before | v2.1z | meaning |
|---|---|---|---|
| `fix.mode` | `script` | **deleted** | (v2.1i) `script` \| `edits` |
| `fix.edits_max_asks_per_case` | 0 | **deleted** | (2026-10-29) edits-mode asks per case, 0 = no cap |
| `fix.plugin` / `fix.plugin_options` | null / {} | unchanged | adapter tier 2 (`docs/ADAPTERS.md`); the `fix:` block keeps only these |
| `script.max_augments_per_case` | 3 (counted new + augment) | **15, augment steps only** | LLM extensions of this run's last code-bearing try, per case and run; used up → the next ask is a fresh `new`. `0` = never augment |
| `script.max_llm_tries_per_case` | — | **new, 20** | every LLM ask for a case in one run (new + augment); at the cap → BLOCKED `repeated_failure` → Handover. `0` = never ask the LLM for the case |
| `script.reuse` / `reuse_versions` | true / 1 | unchanged | stored versions run before the LLM (no call) |
| `script.max_declines_per_case` | 1 | unchanged | v2.1y: declined answers before BLOCKED `needs_owner` (or the classifier's reason) |
| `loop.phases.verify.max_iterations` / `loop.max_total_minutes` | 50 / 240 | unchanged | rounds / minutes of a run |
| `loop.blocked_policy` | javax / api / artifact / plugin 2; parent / encoding / file_lock / reactor_duplicate / needs_owner 0 | unchanged | reason-specific script attempts (a stored script is reused regardless) |

Both new caps are plain caps (no `max(1, …)`, no "first ask not counted"). A config that still carries `fix.mode` /
`fix.edits_max_asks_per_case` is a config of an older version; the keys mean nothing now.

### The ladder per case and run (`sbmig/triage/scripts.py plan_script_step`, `sbmig/triage/decide.py`)
1. **reuse** — the best stored version(s) not yet run here (`script.reuse`, `script.reuse_versions`); no LLM call, before
   `loop.blocked_policy`, works with the LLM down or the budget used.
2. LLM asks so far ≥ `script.max_llm_tries_per_case` → **BLOCKED `repeated_failure`** (Handover).
3. **augment** — while augments used < `script.max_augments_per_case`: extend this run's last try of the case that had code (a stored
   version that failed here, or an LLM script); with `script.reuse: false` and no such try, the stored best version.
4. **new** — nothing to extend, or the augments are used up (a fresh script from scratch, not a 16th extension).

Unchanged around it: declined answers (`script.max_declines_per_case` 1 → BLOCKED, siblings "same cause as …" without a call), the
escalation model from a case's second augment or on a stall, `sbmig-version` / `sbmig-resources` pre-tries (no LLM), the run-wide
`loop.max_llm_calls`. A case without a stored script therefore costs at most 20 LLM asks (1 new + 15 augments + 4 new, when every
answer carries code); a case the LLM declines costs 1 (legacy-ibatis).

Log lines (`ScriptStep.kb_line`; the loop's agent finalises the wording — shape):
- `KB 탐색: … → LLM 보강 요청 (3/20, 보강 2/15, v2 기준)` — the 3rd LLM ask of 20, the 2nd augment of 15, base v2;
- `KB 탐색: … → 보강 한도(15) 소진 → LLM 에 새 수정 요청 (17/20)`;
- `KB 탐색: … → 한도 도달 (저장본 1 + LLM 20회) → blocked`.
`ScriptStep.llm_limit` is now `script.max_llm_tries_per_case`; new `ScriptStep.augment_limit` = `script.max_augments_per_case`.

### What stays (only the switches go)
- `sbmig.api.migrate(kb=False)` (programmatic; `NO_KB_OVERRIDES`) and the class `NullKnowledgeBase` (the fallback when `sbmig.kb`
  cannot be loaded).
- Script-mode features that merely say "edits": an answer without a script still has its edits applied (`TrySource "llm-edit"`,
  `FixAttempt.edits_applied`, commit `fix(edits:<case> <source>): <cluster>`), the structure review's edits, `script.max_declines_per_case`.

### Operator-visible changes
`sbmig run` / `sbmig fleet run` have no `--fix-mode` / `--no-kb`; `POST /api/runs` ignores `fix_mode` / `no_kb`; `fleet-config.json`,
the report and `SBMIG-RESULT` carry no fix mode; `FixContext.fix_mode` / `previous_edits` remain only as read-only compatibility
properties (`"script"` / `{}`).

### Case keys: reactor artifactIds masked (`sbmig/build/signatures.py`, `sbmig/build/log_parser.py`)
- The log parser registers, at the start of every parse, the reactor artifactIds: the caller's `known_modules` (the build runner, the
  rewrite runner and the CLI log tool pass `reactor_artifact_ids_from_poms(root)` = every artifactId the project's pom tree declares,
  so the set does not depend on how far a fail-fast reactor got) plus every artifactId the Maven log shows (`-----< g:a >-----` headers,
  `--- plugin:ver:goal (id) @ <artifactId> ---` mojo markers, `Failed to execute goal … on project <artifactId>`; never the `Building
  <name>` display name) — `set_reactor_modules(names)`, a process-wide set **replaced** per parse, emptied when a `MavenRunner` is made
  and after a successful build (a fleet's next project never sees the previous project's names).
- `normalize_message` masks the error's OWN module **always** (the pre-v2.1z rule, whatever its name) and a registry name only when it
  looks like an artifactId — a separator (`-`, `_`, `.`), a digit or an uppercase letter (`pricing-core`, `app_web`, `svc2`, `DEV`) —
  is at least 3 characters (`MIN_MODULE_NAME_CHARS`) and is not in `MODULE_NAME_STOPLIST` (`web`, `app`, `api`, `core`, `common`,
  `test`, `main`, `build`, `parent`, `jar`, `war`, `plugin`, `boot`, `back`, `front`, `service`, `util`, …). A plain lowercase module
  name (`user`, `config`, `model`) is never masked from the registry: it would eat ordinary message words (`package user.model does not
  exist`) and merge or split cases on wording, not on project identity (review R2). Case-sensitive, word boundaries
  (`(?<![\w.-])name(?![\w-])`), longest name first (`pricing-core-comp` before `pricing-core`).
- Real example (e2e KB): `enforcer|requirejavaversion: pricing-core must be built with` — the root artifactId leaked, the error's
  module was `pricing-core-comp` — becomes `enforcer|requirejavaversion: <module> must be built with`.
- Effect on stored KBs: only L2s that contained another module's artifactId change id; those never matched across projects anyway.
  Every other case id is unchanged: the error's own module is masked exactly as before v2.1z.

### As built (v2.1z, 2026-11-02)

- **Ladder** (`sbmig/triage/scripts.py` `plan_script_step`, `ScriptStep.augment_limit` beside `llm_limit` = `max_llm_tries_per_case`):
  reuse rungs unchanged → `augment {n}/{L} (augment {a}/{A})` while this run has a code-bearing try and `a < A` (kb_line
  `KB 탐색: … → LLM 보강 요청 ({n}/{L}, 보강 {a}/{A}, v2 기준)`) → augments used up: `new fix for case X (augments used up:
  script.max_augments_per_case 15; request {n}/{L})` (kb_line `… → 보강 한도(15) 소진 → LLM 에 새 수정 요청 ({n}/{L})`) → at the total
  cap `mode None`, why `… did not resolve case X (script.max_llm_tries_per_case 20)`, kb_line `… → 한도 도달 (저장본 N + LLM M회) →
  blocked`, BlockedItem notes name `max_llm_tries_per_case`. `max_llm_tries_per_case = 0` blocks at once (a stored script still runs
  first); `max_augments_per_case = 0` never augments. The escalation rule (stall, or from the case's second augment) is unchanged.
- **Reactor masking (D):** `log_parser.reactor_artifact_ids(text)` (`-----< g:a >-----` headers, `@ <artifactId>` mojo markers,
  `on project <a>` lines; `Building <name>` lines are not used) → `parse_maven_log` calls `signatures.set_reactor_modules(...)` once
  before parsing (replaced per parse, cleared by a log without markers); `normalize_message` masks the error's own `module` plus the
  registry as `<module>` (boundary `(?<![\w.-])name(?![\w-])`, case-sensitive, longest first, `MIN_MODULE_NAME_CHARS = 3`,
  `MODULE_NAME_STOPLIST` = web app api core common test tests main build parent module project resources compile jar war pom plugin
  source target class error file java lib util utils base root server client service services batch boot back front). The refined
  keys of `sbmig/triage/cluster.py` inherit it through `normalize_message`. PM additions: `MavenRunner.run` clears the registry on a
  successful build (a success is not parsed, so nothing lingers into a fleet's next project).
- **v2.1z-g ranked javax owners** (2026-11-02, the first in-house diagnosis card: the blocked suggestion named `org.slf4j.Logger`
  while the javax-requiring vendor class — the only one worth mapping to a dependency — was another hit of the same cluster):
  `_classify_javax` ranks the cluster's hits — the owner whose API REQUIRES javax first, neutral types a javax value merely passes
  through (`java.`, `jakarta.`, `org.slf4j.`, `ch.qos.`, `org.apache.logging.`, `org.springframework.`, `lombok.`, …) last —, sets
  `owner_class` to the first and carries all of them in the new `BlockedCandidate.owner_classes`; the fix loop's dependency mapping
  (`_javax_libraries`) tries them in that order, so the discovery / Handover name the real library.
- **v2.1z-f `loop.oscillation_limit`** (2026-11-02, company run: a `FilterConfig` case was BLOCKED `oscillation` after two A↔B toggles
  although the ladder allows 20 asks): the number of repeated toggles that blocks the involved cases is a config key (default 2, the old
  hard-coded value); `loop.oscillation_window` stays the comparison range (a wider window detects toggles sooner — it does not loosen
  the guard). The blocked reason text names both keys.

### Review follow-ups (v2.1z)
No old run file (`report.json`, `web-status.json`, `fleet-config.json`, `state.json`) fails to load. Rules:
- `FixContext.fix_mode` / `previous_edits` are back as **read-only properties** (`"script"` / `{}`; not fields, never serialized or
  rendered) so a tier-2 plugin written against v2.1i does not raise — such a raise would have been swallowed per try and burnt the 20
  asks of every case (R1).
- `load_config` logs a WARNING for `fix.mode` / `fix.edits_max_asks_per_case` left in a yml ("ignored since v2.1z") and for a
  `script.max_augments_per_case` without `script.max_llm_tries_per_case` (augments only now; the per-case total is the new key, default
  20) — `_warn_removed_keys` (R1, R2).
- `sbmig fleet resume` names the dropped switches of an older `fleet-config.json` once (`v2.1z: no_kb, overrides.fix.mode in
  fleet-config.json ignored — …`, stderr) and drops them from the rewritten overrides instead of silently changing the fleet's settings
  mid-way (R1).
- Masking (R2): the error's own module is masked **always** again; registry names only when artifactId-like (see above); the registry
  is seeded from the pom tree (`reactor_artifact_ids_from_poms`) so masking no longer depends on reactor order; emptied per
  `MavenRunner` and after a success.
- Escalation (R2): the stronger model from the case's **third LLM ask** on, whatever the rung — the fresh `new` asks after the 15
  augments were going to the base model.
- `script.max_llm_tries_per_case: 0` reads `LLM asks are switched off for case … (script.max_llm_tries_per_case 0)` / `KB 탐색: … → LLM
  요청 없음 … → blocked` instead of "0 tries did not resolve" (R2).
- **Rootless tree with a lone aggregator (user's company run, 2026-11-02):** root holds only `.gitignore`, `APP/pom.xml` aggregates
  `./WEB` / `./EAR`, and another pom (`APP/BATCH/pom.xml`, or one under `DEV/`) is listed by nobody and depended on by nobody → the
  planner saw "two unlinked top poms" and the run aborted `no-root-pom` before any Maven call. Rule now (`sbmig/analyze/aggregator.py`
  `_plan`): among several top poms, **exactly one aggregator** (a top whose `<modules>` reach other poms) is the application root —
  the generated root lists it plus every top linked to it by dependency (dependency order), and names the rest as left out
  (`AggregatorPlan.excluded`; the Handover gets a dedicated line: add them to that aggregator's `<modules>` and run again — they were
  not migrated). Two or more unlinked aggregators still give None (`no_root_pom_reason` now names them: independent applications).
  e2e fixture `rootless-unlisted-module` (the `sibling-aggregator-dup` tree plus an
  unlisted `APP/TOOLS` jar).
- Accepted as is: `max_llm_tries_per_case` 20 with an `llm.escalation_model` configured sends up to 18 asks per case to the stronger
  model (cost is the company's call; the model is empty by default); the pre-existing `이번 실행에서 v1 실패` wording for a `partial`
  reuse.

## v2.1z-c — hollow reactor: sources outside the modules are not "migrated" (2026-11-02)

User's company run (2026-11-02, the APP / DB / DEV / STATIC project): the build passed and the report said
success, but `STATIC/`'s html / css / js and `DEV/`'s java / jsp were nowhere — only pom.xml and web.xml had reached
`back` / `boot` / `front`. `APP/WEB/pom.xml` is a packaging pom over a JDeveloper workspace: its `<build>` only names
`${DIR_xxxJAR}` / `${DIR_xxxWEB}` output directories (environment properties), the java lives in `DEV/s10/src`, the web root
is `DEV/s10/public_html`. To Maven that module has no sources, so `clean package` has nothing to compile and "succeeds".

**Contract**
- `sbmig/analyze/hollow.py` — `detect_hollow(ws, poms) -> HollowResult`, pure file reads, never raises, no directory NAME
  interpreted. Per pom the roots it compiles / packages: `src/main/java` or `<sourceDirectory>`, `<scriptSourceDirectory>`,
  `src/main/resources` or every `<resources><resource><directory>`, `src/main/webapp`, maven-war-plugin `warSourceDirectory`
  and `<webResources>` directories; `${…}` interpolated from the pom's own `<properties>` plus its in-repo parent chain
  (`basedir` / `project.basedir` = `.`), an expression that stays unresolved is recorded (`HollowResult.unresolved`) and not
  used; absolute paths and paths that leave the project are skipped; `src/test` / `<testSourceDirectory>` are ignored roots.
  Counts `*.java`, JSP / tag files and html / htm / css / js inside vs outside the roots (walk prunes `.git` / `.svn` /
  `.idea` / `.settings` / `node_modules` and `target` beside a pom). `outside_by_dir` = counts per top-level directory.
- Verdicts: `hollow` = inside java 0 ∧ outside java > 0, or (a WAR module exists ∧ inside jsp + web 0 ∧ outside jsp > 0);
  `sources_outside` = not hollow ∧ outside java > inside java > 0 (soft). No poms → nothing.
- Orchestrator (`_check_hollow`, right after the analyze journal): `hollow` → flag **`hollow-reactor`**, journal event
  `hollow_reactor` `{inside, outside, outside_by_dir, roots, unresolved, war}`, Handover follow-up inserted FIRST:
  `the Maven reactor compiles no sources: <n java, n jsp, n web files> lie outside its modules (<dir: kind n, …; …>) and were
  NOT migrated — the as-is build only packaged files produced elsewhere[ (unresolved directories: ${…}, …)]; the sources must
  be mapped into the modules first (sbmig: tree profile, next increment)` (Korean through `i18n.PATTERNS`:
  `Maven 리액터가 컴파일하는 소스가 없다: 모듈 밖에 … 이관되지 않았다 — …`). The run goes on (review, builds, loop) but
  **`success` requires `hollow-reactor` ∉ flags** (beside `restructure-failed`, not aborted, every final build ok) → exit 2,
  `SBMIG-RESULT … exit=2 build=FAIL … attn=hollow-reactor` (`build=` mirrors `success`, as for `restructure-failed`:
  mvn passed, the deliverable did not; `ATTENTION_FLAGS`), flag help in the report. `sources_outside` → flag
  `sources-outside-reactor` + follow-up `sources outside the Maven reactor: … check whether they belong to the application`,
  success untouched. The Handover's "from here on" list opens with the hollow line (before the Boot-BOM line).
- Not done here (next increment, the tree profile): mapping those sources into `{name}-back` / `-front` / `-boot`.

### As built (v2.1z-c)
`sbmig/analyze/hollow.py` (`HollowResult`, `module_roots`, `detect_hollow`, `hollow_followup`, `outside_followup`, the
constants `HOLLOW_FLAG` / `OUTSIDE_FLAG` / `HOLLOW_PREFIX`), `sbmig/loop/orchestrator.py` (`_check_hollow`, the success rule),
`sbmig/report/result_line.py` (`hollow-reactor` attention code), `sbmig/report/markdown.py` (flag help for both flags),
`sbmig/report/handover.py` (`HOLLOW_PREFIX` / `HOLLOW_FLAG`, the (-2) rung of `_from_here`), `sbmig/report/i18n.py` (two
PATTERNS for the hollow line — with / without the unresolved parenthetical — and one for the soft line).
Fixture `fixtures/projects/hollow-reactor` (root WAR pom with `src/main/webapp/WEB-INF/web.xml` only, `DEV/s10/src` servlet,
`DEV/s10/public_html` web root, `STATIC/css`, `DB/schema.sql`): builds, review ok, `hollow-reactor`, success false, exit 2.

## v2.1z-d — ADF projects are not converted: strict detection, the message, the sizing (2026-11-02)

The user's first batch (2026-11-02): about ten of the fifty projects are Oracle JDeveloper 12c / ADF 11 applications
(`DEV/s10`: 1,546 java, `s10.jpr`, `adfm.xml`, `public_html`; `DEV/p0600`: 32 java with `adfm.xml`) behind a packaging pom
(`APP/WEB/pom.xml`). ADF Faces / ADF Business Components / ADF Model / task flows run only on the ADF runtime (WebLogic); Spring Boot
has none. **Business decision (2026-11-02): ADF projects are not converted** — the run must end with the message
**"ADF 프로젝트라 변환이 불가능합니다"**, nothing else is planned. Second refinement: reject **only** projects that are clearly ADF — no
false positives; the decision must be auditable.

**Contract**
- Four independent evidence families (`sbmig/analyze/adf.py`, `FAMILIES = ("bc4j", "java", "model", "faces")`), read from Oracle's own
  file names, namespaces and imports — never a directory name:
  **A `bc4j`** — XML whose root element is `Entity` / `ViewObject` / `AppModule` / `Association` / `ViewLink` in the
  `http://xmlns.oracle.com/bc4j` namespace (file head read, declaration / DOCTYPE / comments skipped);
  **B `java`** — java files whose first 120 lines import `oracle.jbo.` / `oracle.adf.` / `oracle.binding.`;
  **C `model`** — `adfm.xml`, `DataBindings.cpx`, `*PageDef.xml` with root `pageDefinition`;
  **D `faces`** — `*.jspx` / `*.jsff` whose head carries the `http://xmlns.oracle.com/adf/faces` namespace (`/adf/faces/rich`
  included; a plain JSPX is not evidence), `adfc-config.xml`, bounded `*-flow.xml` in the `http://xmlns.oracle.com/adf/controller`
  namespace with a `task-flow-definition`.
  `.jpr` technology names and `adf-config.xml` are **hints** only (listed, never counted). The walk prunes `.git` / `.svn` / `.hg` /
  `.idea` / `.settings` / `node_modules` / `target`; archives are never opened; ≤ 200,000 files; pure reads, never raises.
- `AdfInventory` → **`is_adf` = at least two different families present** (A+B, C+D, A+C … → "ADF 프로젝트라 변환이 불가능합니다");
  exactly one family → `traces`; none → nothing. Counts for the census: entities + distinct `DBObjectName` tables, view objects,
  application modules, associations / view links, page definitions, task flows (`adfc-config.xml` with views / rules + bounded
  flows) and their `<view>`s, ADF pages, java total / java on ADF, the same per top-level directory. `AdfInventory.summary()` →
  **`AdfSummary`** (`sbmig/core/models.py`: `is_adf`, `traces`, `families` (A→D order), `evidence` family → ≤ 8 paths,
  `evidence_counts`, `evidence_total`, `hints` ≤ 10, the counts, `tables` ≤ 50 + `tables_total`, `by_dir`).
- `ProjectProfile.adf: Optional[AdfSummary]` — set by `analyze_project` whenever a family is present (`profile.json` carries it);
  `WebappInventory.adf` the same for `sbmig inventory`.
- `intake.adf_policy: stop | continue` (`IntakeConfig`, default **`stop`**, mirrored in `config/default.yml`): `continue` = report
  only, the run goes on (not recommended).
- Orchestrator `_check_adf` (right after the analyze journal, BEFORE the hollow check). **Two or more families** → flag
  **`adf-project`**, Handover follow-up inserted first — English stored string `ADF project (Oracle JDeveloper / ADF): conversion is
  not possible — ADF 프로젝트라 변환이 불가능합니다 (evidence: bc4j, java, model, faces; E entities / T tables, V view objects, A
  application modules, PD page definitions, F task flows, P pages, J of X java files on ADF)` (Korean through `i18n.PATTERNS`:
  `ADF 프로젝트라 변환이 불가능합니다 (근거: …; 엔티티 E개 / 테이블 T개, 뷰 오브젝트 V개, 애플리케이션 모듈 A개, pageDef PD개, 태스크플로 F개,
  페이지 P개, java X개 중 J개가 ADF)`; under `continue` the line ends ` — intake.adf_policy: continue (report only, the run goes on;
  not recommended)`), journal `adf_project` `{policy, families, evidence (per family, capped), entities, tables, view_objects,
  app_modules, page_defs, task_flows, pages, java_total, java_adf, by_dir}`, one `log.error` line. `stop` → `_abort(report, "ADF
  project — conversion is not possible (ADF 프로젝트라 변환이 불가능합니다; intake.adf_policy: stop)")`: flag `aborted`, exit **3**,
  `SBMIG-RESULT … exit=3 … attn=aborted,adf-project` (`adf-project` in `ATTENTION_FLAGS`), the hollow check does not run, no plan /
  Maven / LLM. **One family** → flag `adf-traces` (soft, not an attention code), a Handover note appended (not first): `ADF traces
  (one evidence family only — faces: <paths>) — the conversion goes on but needs a check: ADF 흔적: 변환은 계속하되 확인 필요 (<counts>)`
  (Korean: `ADF 흔적(근거 계열 하나 — faces: …) — 변환은 계속하되 확인 필요 (…)`), journal `adf_traces` (same keys, no policy); the run
  goes on unchanged.
- Report: `## ADF sizing` (`## ADF 규모`) right after the Handover whenever `profile.adf` exists — one verdict line (rejected / traces),
  **one** counts table, `Tables: …`, then `### Evidence` (`### 판정 근거 / evidence`): per family its count and capped paths, plus the
  hints marked "not evidence" — a person can audit the decision; flag help for `adf-project` and `adf-traces` (English + Korean).
  The Handover "from here on" list opens with the rejection line (rung -3, before the hollow line). `sbmig inventory <dir>` prints
  `ADF: <verdict>; families: …; <counts>` (+ evidence / hints lines) on the console and an `## ADF sizing` block in `inventory.md`.

### As built (v2.1z-d)
`sbmig/analyze/adf.py` (`AdfInventory`, `detect_adf`, `counts_text`, `adf_followup`, `traces_followup`, `adf_abort_reason`,
`adf_sizing_lines`, `ADF_FLAG` / `TRACES_FLAG` / `ADF_PREFIX` / `TRACES_PREFIX` / `ADF_MESSAGE_KO` / `TRACES_MESSAGE_KO`; `_root_dir`
never takes `PurePath.root` for a plain path), `sbmig/core/models.py` (`AdfSummary`, `ProjectProfile.adf`, `WebappInventory.adf`),
`sbmig/core/config.py` + `config/default.yml` (`intake.adf_policy`), `sbmig/analyze/analyzer.py` (sets `profile.adf` when a family
is present), `sbmig/analyze/webapp_inventory.py` (`inv.adf`), `sbmig/analyze/__init__.py` (exports), `sbmig/loop/orchestrator.py`
(`_check_adf` before `_check_hollow`; the hollow line slots after an ADF first line), `sbmig/report/result_line.py` (attention
code), `sbmig/report/markdown.py` (`ADF_TITLE`, `adf_lines`, flag help), `sbmig/report/handover.py` (`ADF_PREFIX` / `ADF_FLAG`,
rung -3), `sbmig/report/i18n.py` (STRINGS for the section and the flag help; PATTERNS for the rejection line — stop / continue —
and the traces note), `sbmig/report/inventory.py` + `sbmig/cli.py` (`sbmig inventory` block / line).
Fixture `fixtures/projects/adf-jdev`
(rootless `APP` aggregator + `APP/WEB` packaging WAR, `DEV/s10` JDeveloper project with all four families, `STATIC`, `DB`):
aggregator generated, `adf-project`, exit 3, `max_llm_calls: 0`, nothing moved (`expect.kb.may_be_empty` since the run writes no KB).

## v2.1z-e — jakarta build discovery: sbmig finds the jakarta build of a javax-era library itself (2026-11-02)

**Why.** A company run blocked a commercial UI library compiled against `javax.*`: the LLM declined with "no jakarta
build exists" and the report told a person to find one — while the vendor's Nexus held the SAME coordinates with
`1.1.x` (javax) and **`2.0.5-jakarta`** (jakarta). Three gaps, all closed here: (1) the run's repository lookup had
silently fallen back to `~/.m2` (its first URL did not answer) and even then only asked the first mirror of one
settings file; (2) nothing verified a release whose pom says nothing (vendor poms declare no dependencies); (3) a
verified jakarta build was handed to the LLM as a fact at best, never applied, and a "no jakarta build" decline was
taken at its word. The user (2026-11-02): "어차피 nexus 에 올라와 있을 거란 말이지 … 있으면 그 버전 적용하는 게 좋지 않을까".

**Repository set (`sbmig/build/repo_versions.py`).** `RepositoryLookup.repository_urls()` → `[(url, credentials,
reason)]`, computed once per run, logged at INFO (`repository lookup: <url> (<reason>)`):
1. an explicit `build.repository_url` FIRST (the operator's decision — the company PC is configured this way);
   credentials from the `<server>` of the mirror / repository with the same URL, else `build.repository_server_id`
   (new, optional); when it differs from the settings-derived primary URL one WARNING names both: `Maven itself
   resolves from <settings url> (settings); sbmig's lookups use build.repository_url <url>`;
2. the settings-derived set by Maven's rules: the repositories (the settings' active-profile `<repositories>` and, since
   2026-11-06, `<pluginRepositories>` — Maven resolves plugins and build extensions there; a URL already listed is asked once, the
   project poms' `<repositories>` incl. the in-repo parent chain with `${…}` interpolated, Maven Central as
   `central`) each replaced by the FIRST `<mirror>` whose `mirrorOf` covers it (`*`, `external:*`, `external:http:*`,
   explicit ids, `!id` exclusions — `mirror_covers`); with `mirrorOf *` a pom repository is never contacted, exactly
   as Maven behaves; unmirrored Central dropped when `build.offline`;
3. settings files: `build.settings_file`, else `~/.m2/settings.xml`, plus Maven's global
   `$MAVEN_HOME|M2_HOME/conf/settings.xml` (or the installation behind `mvn` on PATH) — user entries first
   (`read_settings`, `global_settings_file`; `lookup.settings_files` says which were read).
`_lookup(g, a)` asks every URL in order until a `maven-metadata.xml` lists versions, records the outcome per URL
(`lookup_trace(g, a)`: `ok (N release(s))`, `not found (HTTP 404)`, `HTTP 401 (credentials?)`, `timeout`,
`unreachable`, `skipped: did not answer earlier in this run` — unreachability is counted per URL), then the local
repository. `RepositoryVersions.urls_tried` carries the trace; `remote_failures(g, a)` → `remote repositories unreadable:
<url> <outcome>, …` when no remote URL answered and some failed for a reason other than "not found". The hint line
says so instead of "no newer release": `… (remote repositories unreadable: <url1> HTTP 401, <url2> timeout; local
repository only)`. `pom_text` / `jar_bytes` (new: a bounded jar download, `fetch_bytes` injectable) try the URL that
answered first.

**Discovery (`sbmig/build/jakarta_discovery.py`, `discover_jakarta_builds(lookup, g, a, current, table_entry, cfg,
rv)` → `JakartaDiscovery`).** Sources in order, every candidate verified, cached per (g, a, current) on the lookup,
never raising: (a) `table` — the people's / shipped table entry's **`jakarta_key`** (new `JakartaLibrary` field: the
jakarta build under OTHER coordinates) or `classifier` → verified by the table itself; (b) `same` — the same
coordinates' newer releases from `candidates` (same major, then the higher majors: the vendor line `1.0.6 → 1.1.15
(javax) → 2.0.5-jakarta`); (c) `nexus-search` — every repository URL that looks like Nexus 3 (`…/repository/<name>/`,
deduplicated by host): `service/rest/v1/search?repository=<name>&name=*<stem>*` and `group=*<stem>*` (stem = the
name without trailing digits; ≤ 3 pages each; results filtered to names sharing the stem); (d) `name-probe` —
`maven-metadata.xml` of ≤ `libraries.max_jakarta_probes` (12) sibling names: `{a}-jakarta`, the trailing digits of the
artifactId AND the group's last segment raised together (`com.example.grid8:grid8` → `com.example.grid9:grid9`), the
artifactId alone (`grid9`), `{a}_jakarta`, `{a}.jakarta`, `jakarta-{a}`, `{a}-jakarta-ee`, `{a}-ee10`, `{a}-ee9` (the
group alone, `com.vendor9:vendor8`, is skipped). **Verification** per candidate: a `jakarta` version qualifier
(`2.0.5-jakarta`) or classifier → verified ("version qualifier" / "jakarta classifier"); else the pom (`pom_is_jakarta`:
"pom declares jakarta.*" / "pom declares javax.*"); else a **jar scan** (≤ `libraries.max_jar_scans` (2) downloads of
≤ `libraries.max_jar_scan_mb` (20) MB: the class files' bytes referencing `jakarta/<ee root>/` vs `javax/<ee root>/`,
`javax/annotation/processing/` not counted) → "jar scan ✓" / "jar scan: javax" / "pom silent, jar inconclusive".
Unverified candidates are reported, never applied; javax-verified ones are never shown as candidates. `best()`: the
table's → the same coordinates on the same major (newest) → the same coordinates on the LOWEST higher major (newest of
it) → a Nexus-search / name-probe sibling. `searched` / `probed` keep a human-readable record of every query;
`libraries.discover_jakarta_builds: false` is the kill switch — the v2.1d behaviour (the same coordinates' releases are
still listed by `hint_lines`; no verification, no table / sibling search, no `sbmig-jakarta` pre-try, no decline
rejection; the search record reads `discovery off (libraries.discover_jakarta_builds: false)`); `libraries.nexus_search:
false` skips the API. Hint lines: `repository also has <g:a> <v> (jakarta ✓ — version qualifier; found by same coordinates, a higher
major) — a jakarta build of <g:a>: switch the declaration to it …`, `repository also lists … (jakarta unverified — pom
silent, jar inconclusive …) — not applied automatically`, `no jakarta build of <g:a> was found in the repository (<searched>)`.

**Fix loop (`sbmig/loop/fixloop.py`).**
- **Library attribution fallbacks** (the real run had no repository line at all because the owner class could not be
  mapped): `_case_libraries` (classpath) → `_javax_libraries`: for a cluster the triage classifies `external_javax_api`,
  the owner class's package tokens against the declared dependencies' groupId / artifactId tokens (stems without
  trailing digits count: `com.example.grid.GridExporter` → `com.example.grid8:grid8`), then the local repository's jars
  of the declared dependencies for the class file (`_owner_dependency`); journal `library_mapped` / `library_unmapped`
  (with the owner class). Journal `library_lookup` per library: `{key, current, source, versions_found, jakarta_builds,
  unverified, searched, urls_tried, settings_files, repositories, error}` — a run is diagnosable from `journal.jsonl`.
- **Zero-LLM pre-try `sbmig-jakarta`** (`PRE_TRY_SOURCES`, after `sbmig-version` / `sbmig-resources`, commit
  `fix(sbmig-jakarta): <cluster id>`, once per (pom, old → new) per run): for an `external_javax_api` case whose
  library has a verified build, every pom declaring the old coordinates is rewritten — `replace_dependency` for other
  coordinates, version / classifier for the same; version-less when the new parent chain / Boot BOM manages the new
  key; configured in-house artifacts (`_protected_version_keys`) are never swapped. **A higher major of the same
  coordinates is applied**: javax → jakarta is the one major change v2.1d allows, and the read-back lists it for the
  operator (`major_upgrades`, ⚠ Handover line) with sbmig's synthesized guide — a pre-try never costs an LLM call for
  the guide (`_verify_guide` skips the LLM for `PRE_TRY_SOURCES`). Journal `jakarta_try` with the plan and the
  searched record. The decline check: an LLM answer declining with a jakarta / javax reason while discovery has a
  verified build is **not a decline** — note `decline rejected: the repository has <g:a:v> (…)`, journal
  `decline_rejected`, an ordinary failed try, the case is asked again with the fact (the decline counter untouched).
- **Blocked / Handover evidence**: an `external_javax_api` or javax `needs_owner` item's suggestion (and notes) ends
  with `; repository search for a jakarta build of <g:a>: <searched> — verified build(s): … | none verified`, or `; the
  library behind <owner class> could not be matched to a dependency (classpath, package tokens, local jars) — run mvn
  dependency:tree …` (Korean via `PATTERNS`; `tr_line` translates such a tail on its own).
- `knowledge/notes/jakarta.md`: the LLM switches to a listed build and never claims "no jakarta build" while a line
  lists one.

**Diagnostics.** `sbmig repo-probe <groupId:artifactId[:version]> [--project <dir>] [--config …]` prints the settings
files read, the repository set in order with the reason for each URL (and the both-URLs note), the per-URL outcome of
the metadata lookup, the versions (prereleases marked), the candidates' jakarta verdicts, the discovery's builds and
searched record; last line `REPO-PROBE ok|none <g:a> versions=N jakarta=<coordinates|-> repositories=N` — for the
air-gapped PC where nobody can show us `settings.xml` (`docs/RUNBOOK.md`).

**Config.** `libraries.discover_jakarta_builds` (true), `libraries.nexus_search` (true), `libraries.max_jakarta_probes`
(12), `libraries.max_jar_scans` (2), `libraries.max_jar_scan_mb` (20), `build.repository_server_id` (null);
`JakartaLibrary.jakarta_key`; `TrySource` += `sbmig-jakarta`; `RepositoryVersions.urls_tried`.

**Fixture / libs.** `fixtures/libs/grid8`: `com.example.grid8:grid8` `1.0.6` and `1.1.15` (javax.servlet, JDK 8) and
`2.0.5-jakarta` (jakarta.servlet, JDK 21, a silent pom — the jakarta API comes from a build-time profile consumers
never see); fixture `servlet-javax-grid` (a servlet WAR calling `GridExporter.export(request, response)`): after the
recipes → `external_javax_api` → `sbmig-jakarta` swaps `gridapp-back/pom.xml` to `2.0.5-jakarta` (⚠ major) → build
ok, 1 LLM call (the review). The digit-variant (`grid9`) and `-jakarta` artifactId variants are unit tests only.

### As built (v2.1z-e)
`sbmig/build/jakarta_discovery.py` (new: `JakartaBuild` / `JakartaDiscovery` (`best`, `hint_lines`, `searched_text`), `stem`,
`digit_variants`, `sibling_names`, `qualifier_is_jakarta`, `scan_jar`, `discover_jakarta_builds`, `probe_report`; the `_Verifier`
qualifier → pom → jar-scan ladder; Nexus search over every Nexus-looking base deduplicated by host), `sbmig/build/repo_versions.py`
(`read_settings` / `SettingsData`, `global_settings_file`, `mirror_covers`, `pom_repositories`, `RepositoryLookup(cfg, fetch=…,
fetch_bytes=…, project_root=…)`: `repository_urls` (explicit URL first, then the settings-derived set), `repository_url` = its first
entry, per-URL `_fetch` reachability, `_lookup` over the set with `lookup_trace` / `remote_failures`, `pom_text` / `jar_bytes` asking
the answering URL first, `settings_files`, `url_notes`; `hint_lines` says "remote repositories unreadable: …"),
`sbmig/core/models.py` (`TrySource` += `sbmig-jakarta`, `RepositoryVersions.urls_tried`), `sbmig/core/config.py` +
`config/default.yml` (`libraries.discover_jakarta_builds` / `nexus_search` / `max_jakarta_probes` / `max_jar_scans` /
`max_jar_scan_mb`, `build.repository_server_id`; the `repository_url` comment), `sbmig/core/jakarta_libraries.py` +
`knowledge/libraries/jakarta_libraries.yml` + `libraries.company.example.yml` (`jakarta_key`), `sbmig/loop/fixloop.py`
(`SBMIG_JAKARTA_SOURCE`, `JAKARTA_SEARCH_PREFIX`, `UNMAPPED_PREFIX`, `_GENERIC_TOKENS`; `_discover`, `_discovery_lines`,
`_journal_lookup`, `_javax_cluster`, `_declared_dependencies`, `_owner_dependency`, `_owner_from_source`, `_javax_libraries`,
`_jakarta_evidence`, `_decline_contradiction`, `_jakarta_plan` / `_jakarta_targets` / `_jakarta_attempt`; `_library_lines(key,
current, javax)` + the discovery lines; the decline check in `_llm_try`; the evidence on the blocked item in `_register_blocked`;
`_verify_guide` never calls the LLM for a pre-try; the lookup gets `project_root`), `sbmig/report/i18n.py` (four PATTERNS + the
tail split in `tr_line`), `sbmig/cli.py` (`repo-probe`), `knowledge/notes/jakarta.md`, `docs/RUNBOOK.md`.
Fixture `servlet-javax-grid` + `fixtures/libs/grid8` (install.sh builds the
three versions; the jakarta one with `-Dsbmig.standin=1`); `boot2-javax-lib`'s `sbmig.e2e.yml` sets
`libraries.discover_jakarta_builds: false` so the fixture still ends BLOCKED `external_javax_api` (the local libs hold
`legacy-web-lib` 2.0.0 / 3.0.0, jakarta builds the pre-try would otherwise apply as a `1.0.0 → 2.0.0` major).

## v2.1z-h — stray pom copies: a module's copy in an IDE output / backup folder is not a module (2026-11-03)

**Company case.** The first error after a good run of company projects: the input zip held `boot/bin/` — a whole copy of the
`boot` module (`src/`, `.project`, `pom.xml` with the module's coordinates), the folder an IDE or a backup left behind and git
ignores. The pom scan read it as a project pom; the structure review's material then said "Poms no aggregator lists as a
module: `boot/bin/pom.xml`", the LLM called that critical and its edit added `<module>boot/bin</module>` (commit `review: fix
critical structure issue (1)`); Maven refused the reactor (`Project 'xxx-boot:1.0.0-SNAPSHOT' is duplicated in the reactor`)
→ `reactor_duplicate` (0 LLM tries) → run over. Verified from the run's git history: the copy was in the as-is snapshot, the
listing came from the review.

**Rule (`sbmig/analyze/pom_parser.py stray_pom_copies`).** A pom is a *stray copy* when (1) it is not the root and no pom's
`<modules>` points at it, (2) another pom has the same effective `groupId:artifactId`, and (3) it lies inside that pom's
directory (a nested copy, `boot/bin/pom.xml` ↔ `boot/pom.xml`) or that pom is reactor-reachable (the root, or listed; a copy
beside the module, `backup/boot/pom.xml`). Two unlisted siblings with equal coordinates in a rootless tree are left alone (the
aggregator planning and Maven see them as before); a LISTED duplicate is never dropped here — that stays `reactor_duplicate`
(intake dedupe / blocked, handed over). No folder names in the rule (`bin`, `build`, `backup` … are all caught by content).

**What agrees.** `parse_poms` drops the strays and appends one warning each (`pom ignored (stray copy): <why> — not a module`;
it reaches the review facts as a `pom parse warning` line), so analysis, the aggregator plan, the hollow / ADF detectors, the
structure review, the restructure and the Handover all see the same pom set. `find_stray_pom_copies(ws)` gives the intake the
list (`StrayPom(path, duplicate_of, coordinates, nested)`, `.dir`, `.why()`).

**Intake (`Orchestrator._intake_stray_poms`, first step of `_intake_layout`).** The copies' directories go into
`Workspace.ignored_dirs` (`is_ignored(rel)`; `walk` / `glob` / `java_files` / `pom_files`, `PathView.glob`, the hollow and ADF
walks prune them like `target/` — the copy's java is neither "outside the reactor" nor ADF evidence; `find_pom_files` filters
them too, so a later parse is consistent even without re-detection), flag `stray-pom-ignored`, journal `stray_pom_ignored`
(`poms`, `duplicates_of`, `coordinates`, `dirs`), one Handover follow-up per copy — `pom copy ignored: \`boot/bin/pom.xml\` has
the same coordinates as \`boot/pom.xml\` (\`g:a\`), lies inside the directory of \`boot/pom.xml\` and no pom lists it as a module —
treated as a stray copy (an IDE output / backup folder): not built, not migrated, left where it is; delete it from the source,
or give it its own coordinates and list it in <modules> if it is a real module` (Korean: i18n `PATTERNS`, two forms: inside /
beside) — and the operator line `접수: pom 복사본 N개 무시 — …`. Nothing is deleted, moved or committed: the copy stays where it
is in the result.

**Review guard (`sbmig/review/inputs.py reactor_duplicate_lines`, rule (e) of `invariant_violations`).** After the review's
edits the poms are re-read; the parse drops only UNLISTED copies, so a copy an edit listed is parsed and shows up as two
reactor-reachable poms (root + listed, transitively through `<modules>`) with one `groupId:artifactId` that did not exist
before the edit → one violation sentence (`\`pom.xml\`: the reactor now holds 2 projects with the coordinates \`g:a\` (…) —
Maven refuses that ("is duplicated in the reactor"); a copy of a module (an IDE output / backup folder) is not a module: leave
it unlisted`) → the existing D-M4 path restores the tree and asks the LLM once more with the reason. A pair that existed
before the edits is not the review's doing.

**Not done (on purpose).** No Eclipse `.classpath` output-folder rule (the copy here was a full project copy, not compiler
output; a `kind="output"` rule can come when a run shows it); the restructure's own `rglob`s over module directories are
untouched (a copy inside a module directory moves with it and stays unbuilt).

### As built (v2.1z-h)
`sbmig/analyze/pom_parser.py` (`_module_pom_path`, `StrayPom`, `stray_pom_copies`, `_read_raw_poms`, `find_stray_pom_copies`;
`find_pom_files` honours `ws.is_ignored`; `parse_poms` drops strays + warnings), `sbmig/core/workspace.py` (`ignored_dirs`,
`is_ignored`, `walk` / `glob` pruning), `sbmig/analyze/analyzer.py` (`PathView.ignored_dirs` / `is_ignored` / `glob`),
`sbmig/analyze/hollow.py` + `sbmig/analyze/adf.py` (prune), `sbmig/loop/orchestrator.py` (`STRAY_POM_TAIL`, `stray_pom_followup`,
`_intake_stray_poms`), `sbmig/review/inputs.py` (`_reactor_duplicate_keys`, `reactor_duplicate_lines`, rule (e)),
`sbmig/report/markdown.py` (flag text) + `sbmig/report/i18n.py` (flag + two PATTERNS).
Fixture `boot-stray-pom-copy` (Boot 2.7 aggregator + `shop-boot`, the copy in
`shop-boot/bin/`; build ok, 1 LLM call, the Handover line, the copy left in place).

## v2.1z-i — bundled jars: a jar in the tree that no pom declares is wired, not declared missing (2026-11-04)

**Why.** Company run 2026-11-04 (diagnosis card from the in-house LLM, class (d) sbmig bug): a `spring_legacy` WAR kept
its vendor crypto / SSO jars in `APP/WEB/src/main/webapp/WEB-INF/lib/` (`Crypt.jar`, `ssolib.jar`, … — no pom.properties,
no Maven metadata, declared by no pom). The as-is IDE build compiled against them (web-app libraries container); Maven
never did. The restructure moved the folder into the back module and left the jars where they were (Handover: "declare
the jars as Maven dependencies"), the build failed with `package com….crypto.seed does not exist`, and the fix LLM —
which never heard that the jars exist — declined for lack of coordinates → BLOCKED `needs_owner` (8 items) although the
classes sat in the project. 704 other errors were fixed in that run.

**Decisions (PM).** (1) Content-based: a *bundled jar* is any `*.jar` in the tree outside build output / `.git` / the
intake's ignored dirs that no pom declares as a `system` dependency — no folder-name rule (`WEB-INF/lib` is where they
usually are). (2) sbmig wires it with **0 LLM calls** when a build error needs it (pre-try `sbmig-bundled`, like
`sbmig-version` / `sbmig-resources` / `sbmig-jakarta`): coordinates are **never guessed** — a jar whose own
`pom.properties` coordinates the repository SET has (v2.1z-e lookup) becomes a plain dependency; every other jar is
copied to `<module>/lib/<name>` and declared `system` scope with the Ant-intake placeholder coordinates (v2.1j rule:
project groupId + file stem, version from the file name or "0") plus `includeSystemScope` on the boot module's
spring-boot-maven-plugin (repackage leaves system jars out by default), and the Handover asks the owners to publish the
jar to Nexus and replace the entry. (3) The LLM is told the fact ("the project bundles `<jar>` which provides package
`<p>`") and a decline that claims a missing library / unknown coordinates while a bundled jar provides the case's
package is **rejected** (no decline count), like the v2.1z-e jakarta contradiction. (4) The scan is a profile fact
(`profile.bundled_jars`, flag `bundled-jars`, report section "Bundled jars", `logs/bundled-jars.json`) whether or not a
build error ever needs it.

### Contract

* **Model** (`sbmig/core/models.py`): `JarCoordinate.provides: list[str]` — every package holding a class, uncapped
  (`packages` stays the capped display list; `sha1` already existed); `ProjectProfile.bundled_jars: list[JarCoordinate]`.
  `TrySource` += `sbmig-bundled` (the error list's `FixTry.source` literal; `PRE_TRY_SOURCES ⊆ TrySource` is guarded).
* **Config**: `intake.bundled_jars: true` (the scan; off → `[]`, no pre-try, no facts); the lookup follows
  `intake.jar_lookup`; the unknown-jar policy follows `intake.unknown_jar` (`system` → wire; `skip` → facts + Handover only).
* **Analyze** (`sbmig/analyze/bundled_jars.py`): `declared_system_jars(ws, poms) -> set[str]`,
  `scan_bundled_jars(ws, cfg, poms, *, lookup=None) -> list[JarCoordinate]` (sorted by path; `resolve_jar` for the
  coordinates; `provides` filled from the zip listing; never raises), `providers(jars, package) -> list[JarCoordinate]`
  (exact `provides` entry first, then jars holding only sub-packages). `analyze_project` fills `profile.bundled_jars`
  after the ADF detection; `step_analyze` (orchestrator) adds flag `bundled-jars`, journal
  `{"event": "bundled_jars", "jars": [path…], "coordinates": {path: "g:a:v"|null}}`, operator log
  `분석: 동봉 jar N개 (pom 미선언)`, and writes `logs/bundled-jars.json` (the `JarCoordinate` list as JSON).
* **Fix loop** (`sbmig/loop/fixloop.py`): `SBMIG_BUNDLED_SOURCE = "sbmig-bundled"`; `_bundled_targets(c)` → the module poms
  (`owning_pom` of the error files) for a cluster whose `package <p> does not exist` packages (`_PACKAGE_MISSING_RE`) or
  `cannot find symbol … class X` imports (`import <p>.X;` in the error's file) have a provider in `profile.bundled_jars`,
  else None; `_bundled_attempt(pr, st, c)` → `_pre_attempt(... source=SBMIG_BUNDLED_SOURCE, apply_fn=...)` calling
  `sbmig.restructure.system_jars.wire_bundled_jar` per (module pom, jar), `repository_has` = the jar's own
  `pom_properties` / `sha1_lookup` coordinates verified in the repository set (v2.1z-e `jakarta_discovery` lookup; an
  unreadable repository counts as "not there" → system scope), `boot_poms` = the boot modules that package the module
  (`profile.boot_modules` / poms carrying the Boot plugin); once per (module pom, jar) per run; commit
  `fix(sbmig-bundled): <cluster id>`; journal `{"event": "bundled_jar_wired", "case", "jar", "pom", "coordinates",
  "scope", "placeholder"}`; Handover line `bundled_jar_followup(w)` (⚠ when placeholder). Registered in `_pre_tries`
  **before** `sbmig-jakarta` (a bundled javax-era jar is still wired first; the jakarta swap sees the declaration next round).
  `_package_facts(c)` adds `the project bundles \`<jar path>\` which provides package \`<p>\` (coordinates: <g:a:v from
  pom.properties | unknown — no pom.properties, not in the jar name table>) — wire it as a dependency (system scope at
  \`${project.basedir}/lib/<name>\` when the repository lacks it) instead of calling the library missing` per (jar,
  package) ≤ `PACKAGE_FACTS_ARTIFACTS`. `_decline_contradiction(c, reason)` → when the reason matches
  `(?i)coordinates|artifact|library|jar|좌표|라이브러리|아티팩트` and a provider exists for one of the case's missing
  packages: `\`<jar>\` in the project provides package \`<p>\` — the library is not missing, it is bundled; wire it`.
* **Restructure** (`sbmig/restructure/system_jars.py`): `wire_bundled_jar(ws, cfg, *, module_pom, jar, repository_has,
  boot_poms=None, project_group="") -> WiredJar`, `ensure_include_system_scope(ws, boot_pom) -> bool`,
  `bundled_jar_followup(w) -> str` (text shape: `bundled jar \`<name>\` (<why>) wired into \`<module pom>\` as a system
  dependency \`<g:a:v>\` — <BUNDLED_TAIL>` | `… as a compile dependency \`<g:a:v>\` (the repository has it)`). System scope: the SAME system dependency is also declared in each of `boot_poms` (Maven does not pass a `system`
  dependency on transitively; one copy, `systemPath` = `${project.basedir}/<relpath from the boot pom dir to
  <module dir>/lib/<name>>`), then `ensure_include_system_scope` on each of them, so the repackaged boot archive (jar or
  war) carries the jar (amendment 2026-11-04, found by the fixture agent). The copy logic is shared with
  `_relocate_system_dep` (one helper). The webapp-move follow-up ("N file(s) under WEB-INF/lib …
  stay in …") names the jar files and says the loop wires a jar a build error needs.
* **Report** (`sbmig/report/markdown.py`): section "Bundled jars (declared by no pom)" after the structure review: one row
  per jar — path, coordinates (or "unknown"), `provides` count, used-by (module / "—"), wired (scope / "—"); flag text
  `bundled-jars`; i18n for the flag, the section, the Handover PATTERNS (two shapes) and the fact line.
* **Fixture** `fixtures/projects/webapp-bundled-jar` (from `company-spring5-web`): `src/main/webapp/WEB-INF/lib/acme-seed-1.0.jar`
  (committed, tiny: `com.acme.seed.Seed` with a static `encrypt(String)`, no pom.properties) used by a source class; build
  ok with 1 LLM call (the review); flags `bundled-jars`; journal `bundled_jars` + `bundled_jar_wired`; a fix attempt
  with source `sbmig-bundled`; Handover regex `bundled jar \`acme-seed-1.0.jar\``; post_checks: `<name>-back/lib/acme-seed-1.0.jar`
  exists, the back pom holds `<systemPath>${project.basedir}/lib/acme-seed-1.0.jar</systemPath>`, the boot pom holds
  `<includeSystemScope>true</includeSystemScope>` and the same system dependency; the repackaged boot archive (a jar —
  the fixture has no JSP) contains `acme-seed-1.0.jar` (the integrator checks it with `unzip -l`; the e2e post_checks
  cannot look inside an archive).
* **Not in scope**: `WEB-INF/classes` (compiled classes without sources — Handover only, as today); a jar whose
  pom.properties coordinates the repository has under a *different* version; Gradle; the structure review does not
  wire jars (the loop does, on evidence of a build error).

### As built (v2.1z-i, 2026-11-04)

* **Analyze** — `sbmig/analyze/bundled_jars.py`: `declared_system_jars` reuses `edges.system_dependencies` /
  `resolve_system_path` (`${project.basedir}` / `${basedir}` / relative paths against the declaring pom); `scan_bundled_jars`
  walks `ws.glob("*.jar")` (both `Workspace` and `PathView` prune `.git` / build output / `ignored_dirs`), re-checks
  `is_build_output_path` and `ws.is_ignored`, drops the declared system jars, resolves each with `resolve_jar_scan` (one
  jar-name table and one `Sha1Lookup` per run, `False` when `intake.jar_lookup` is off), `provides` = the uncapped
  `JarScan.packages`, `used` via `mark_used` over the tree's `.java` files; a duplicate sha1 is kept with the note
  `same jar as <first path>`; an unreadable jar is listed with empty `provides`. `analyze_project` fills
  `profile.bundled_jars` after the ADF block; the orchestrator's `_note_bundled_jars` (in `_step_analyze`, after the hollow
  check) sets the flag, writes the journal event, the operator line and `logs/bundled-jars.json` (`logs_dir(ws)`).
* **Fix loop** — `sbmig/loop/fixloop.py`: `SBMIG_BUNDLED_SOURCE` (also in `PRE_TRY_SOURCES`), `_bundled_missing_packages`
  (`package <p> does not exist` + the `import <p>.X;` of a `symbol: class X` error, `javax.*` / `jakarta.*` skipped),
  `_bundled_wireable` (no `provided`-scope jar — a container API copy is never wired), `_bundled_targets` (None also when
  every (pom, jar) pair was already tried, so a dead match does not hide the jakarta pre-try), `_bundled_plan`,
  `_bundled_in_repository` (the loop's `RepositoryLookup`: `pom_text(g, a, v)` or `v in versions(g, a)`; only for
  `pom_properties` / `sha1_lookup` / `name_table` coordinates; any failure → system scope), `_bundled_boot_poms(module_pom)`
  (the current poms carrying the Boot plugin + `profile.boot_modules`, narrowed to the ones that depend on the module or
  are it — several boot modules (MSA) must not all carry the system dependency; none matching → all), `_bundled_attempt`
  (`_pre_attempt` with an `apply_fn` calling `wire_bundled_jar` per (module pom, jar); keys `(sbmig-bundled, pom, jar.path)`;
  `intake.unknown_jar: skip` → a jar the repository lacks is not wired, Handover line only; journal `bundled_jar_wired`;
  `self._followup(bundled_jar_followup(w))`), `_bundled_facts` (in front of `_package_facts`), `_bundled_contradiction`
  (first inside `_decline_contradiction`). Registered between `sbmig-resources` and `sbmig-jakarta`.
  **`TrySource` += `sbmig-bundled`** (`sbmig/core/models.py`).
* **Restructure / report** — `sbmig/restructure/system_jars.py`: `copy_jar_to_module_lib` (shared with `_relocate_system_dep`),
  `wire_bundled_jar` (locates the jar after the restructure by sha1, then by a path ending in the as-is path, then a
  single name match — `ValueError` when none; placeholder coordinates = module / project groupId + `maven_artifact_id(stem)` +
  file-name version or "0"; system case: copy, `<dependency>` + the Ant-intake comment, the same dependency in each boot pom
  with `systemPath` `${project.basedir}/<relpath>` to the one copy, `includeSystemScope` on their Boot plugin; idempotent),
  `ensure_include_system_scope`, `bundled_jar_followup`, `webinf_left_followup` (the webapp-move line names the jars and
  says the loop wires one a build error needs). `sbmig/report/markdown.py`: section "Bundled jars (declared by no pom)"
  (`bundled_jars_lines`, the "wired" column read from the Handover lines), flag text; i18n for all of it.
* **Fixture** `webapp-bundled-jar` (from `company-spring5-web`, `com.example:vault`): `WEB-INF/lib/acme-seed-1.0.jar`
  (749 bytes, `com.acme.seed.Seed`, no pom.properties; built with JDK 21 `javac --release 8`, commands in the README) used by
  `CryptoUtil`; the as-is Maven build fails by design (`BUILD_MATRIX` needs value `fails`, asserted by the integration test).
  e2e: exit 0, build ok, 1 LLM call (the review), `new 1` = the pre-try, `resolved 15`, flags `bundled-jars`; the back pom
  holds the system dependency, `vault-boot/pom.xml` the same dependency (`${project.basedir}/../vault-back/lib/…`) +
  `includeSystemScope`, and `vault-boot/target/vault.jar` contains `BOOT-INF/lib/acme-seed-1.0.jar` (checked with `unzip -l`;
  the fixture's boot module is a jar, not a war — no JSP).
* An Ant project is flagged `bundled-jars` too: the jars the pom synthesis deliberately leaves undeclared (container API copies,
  unused jars) are listed in the section; none is wired unless a build error needs it, and a `provided`-scope copy never.
* **Superseded jars (user review 2026-11-04, "있다고 다 참조 걸어주는 방식은 안티패턴")**: a real `WEB-INF/lib` holds sitemesh,
  spring-aop / -beans / -context / -webmvc … — a bundled jar whose known g:a the target parent chain / Boot BOM MANAGES
  (`_managed_keys`) is **superseded**: never wired (not as system scope, not pinned to its old version — two lines of one
  library never coexist), excluded from `_bundled_wireable` / the providers / the decline contradiction; `_bundled_facts`
  gives the LLM the opposite fact ("the as-is tree bundled `spring-webmvc-4.3.30.jar` … the chain manages it at a newer
  line — do NOT re-add the old jar or pin the old version; migrate the code or answer <blocked>"); the orchestrator's
  `_classify_bundled_jars` (before the report, from `effective-depmgmt-after-restructure.json`, else the loop's keys) notes
  `superseded: g:a is managed by the target parent chain` and the report table gains a `status` column (superseded /
  coordinates known / coordinates unknown). The three buckets: managed → superseded (nothing to do), known & in the
  repository → a plain dependency when a build error needs it, unknown → lib/ + system scope + Nexus ask.
* **Not done / open**: the "as-is view for the LLM" idea (serve `asis:<path>` from the first commit, an automatic as-is → now
  diff of the error file) goes into the case-thread increment; `WEB-INF/classes` stays Handover-only.

## v2.1z-j — bundled jars are wired at the restructure, before the first build (2026-11-04)

**Why.** The user's review of a real `WEB-INF/lib` (2026-11-04): "lib 안에 jar 가 있으면 jar 의 메타데이터 / pom 을 읽어 g:a:v 를 가져와
Nexus 에 물어보고, 있으면 import, 없으면 jar 추가 — 이게 마이그레이션 방향 아닌가?" It is, and it is more complete than the v2.1z-i
pre-try alone: a pre-try moves only when a **compile** error names the package, but **runtime-only jars** (JDBC drivers, a
sitemesh filter registered in web.xml, taglib implementations, logging bridges) never produce one — the as-is WAR packaged
them (the war plugin copies `WEB-INF/lib` whole), the restructure moves the web root and they silently vanish. The Ant intake
(v2.1j) already synthesizes dependencies from jars this way; Maven projects with undeclared jars now get the same rule, at the
restructure. Cost: a few dozen small repository requests per project. The pre-try `sbmig-bundled` stays as the safety net.

**Decisions (PM).** Three buckets plus three filters, applied to every `profile.bundled_jars` entry once the layout is in
place: (filters) a jar whose known g:a the target parent chain / Boot BOM **manages** → *superseded*, untouched (the managed
version stands in; note for the report); a **container API** copy (`scope == "provided"`, `is_container_api`) → untouched
(Tomcat supplies it); a jar **no source / JSP / XML / properties references** (`mark_used` over the module's source roots +
web / resource dirs, the Ant intake's rule) → not declared, named in the Handover ("unused — not declared; add it by hand if
a runtime needs it"); a g:a the target pom **already declares** → untouched; (buckets) known coordinates the repository SET has
→ a plain `compile` dependency on `g:a:v` (the jar's own version; the version omitted when the chain manages it — but then it
is superseded anyway); unknown coordinates (or known but absent from the repository, `intake.unknown_jar: system`) →
`wire_bundled_jar` system scope (copy to `<module>/lib`, the boot poms depending on the module get the same dependency +
`includeSystemScope`, Handover ⚠ Nexus upload); `intake.unknown_jar: skip` → Handover only. Target pom = the module that now
holds the jar's web root / sources (`owning_pom` of the jar's post-move path; the comp module in the layout split, the module
itself in pom-only mode), boot poms = `c.boot_rel` (layout) / the Boot-plugin poms depending on the module.

### Contract

* **Config**: `layout.wire_bundled_jars: true` (the restructure wiring; `false` → v2.1z-i behaviour: facts + pre-try only).
  `intake.bundled_jars` (the scan), `intake.unknown_jar`, `intake.jar_lookup` as before.
* **Restructure** (`sbmig/restructure/bundled_jars.py`, new): `plan_bundled_jars(ws, cfg, jars, *, module_pom, boot_poms,
  managed_keys, repository, source_roots, extra_dirs) -> BundledPlan` (pure: per jar one of `superseded | container |
  unused | declared | compile | system | skip`, with the reason) and `apply_bundled_jars(ws, cfg, plan, *, project_group)
  -> list[WiredJar]` (writes through `wire_bundled_jar` for `compile` / `system`; never raises past a per-jar WARNING).
  Called by the restructurer at the end of the webapp move (where the `left` jars are known) for the jars under that web
  root, and once more after the layout for bundled jars elsewhere in the tree (`lib/`, …) that still sit undeclared.
  `_Ctx.repo_lookup` (the v2.1l `RepositoryLookup`) answers "does the repository have `g:a:v`" (`pom_text` / `versions`);
  `self._chain_facts(c).managed` + the target Boot BOM give `managed_keys`. `RestructureResult.jars_wired: list[dict]`
  (`path, pom, coordinates, scope, placeholder, boot_poms`) and `RestructureResult.jars_skipped: list[dict]` (`path, why`)
  carry the outcome; the orchestrator journals `{"event": "bundled_jar_wired", "stage": "restructure", …}` per wired jar
  (the loop's event gains `"stage": "loop"`) and `{"event": "bundled_jars_skipped", "jars": [{path, why}…]}`; the Handover
  gets `bundled_jar_followup(w)` per wired jar and one line per unused jar. The webapp-move follow-up ("N file(s) under
  WEB-INF/lib … stay") now lists only what the rule did NOT wire.
* **Report**: the "Bundled jars" table's `wired` column reads "restructure" / "loop" from the journal stage when available
  (else the Handover line as today); `status` as in v2.1z-i plus `unused` / `container API`.
* **Fix loop**: unchanged; `_bundled_plan` skips (pom, jar) pairs the restructure already wired (the g:a is declared → the
  package exists → no error; defensive: `has_dependency`).
* **Fixture** `webapp-bundled-jar`: the jar is wired at the restructure → the first build passes: `scripts.attempts_min` → none,
  the `compile|package_does_not_exist|com.acme.seed` case block removed (the error never happens), journal
  `bundled_jar_wired` with `stage: restructure`, the same post_checks (lib copy, systemPath, includeSystemScope, the boot jar
  carries the jar). A second scenario in the same fixture: `WEB-INF/lib/servlet-api-2.5.jar`-like container copy → untouched
  (status `container API`), and an unreferenced `WEB-INF/lib/unused-tool-1.0.jar` → Handover "unused — not declared".

### As built (v2.1z-j, 2026-11-04)

* **Planner / applier** — `sbmig/restructure/bundled_jars.py`: `plan_bundled_jars(ws, cfg, jars, *, module_pom, boot_poms,
  managed_keys, repository, source_roots, extra_dirs, current, managed_known)` is pure; first rule wins: `superseded` (g:a ∈
  managed keys) → `container` (`scope == "provided"`, `provided_scope(g, a)` or `is_container_api(provides)`) → `unused`
  (`mark_used` on a COPY of the jar over the module's source roots + module / boot / front dirs; the analyzer's `used` is not
  changed) → `declared` (`PomEditor.has_dependency`) → `skip` when `managed_known` is False and the coordinates are known
  (the chain's facts could not be read: a jar that MAY be a managed library is not wired blind — integrator's guard) →
  `compile` (coordinates from `pom_properties` / `sha1_lookup` / `name_table` with g, a, v AND `repository(g, a, v)`:
  `RepositoryLookup.pom_text`, else `v in versions(g, a)`) → else `system` (`intake.unknown_jar: skip` → `skip`). A second jar
  with the same sha1 follows the first. `apply_bundled_jars` writes `compile` / `system` through `wire_bundled_jar`; a failing
  jar becomes `skip: wiring failed: …` with one WARNING; `BundledPlan.wired()` / `skipped()` give the dict shapes of
  `RestructureResult.jars_wired` / `jars_skipped`.
* **Hooks** — `restructurer.py`: `_populate_front` can NOT wire (the comp / boot poms do not exist yet), so it parks its
  WEB-INF leftovers on `c.webinf_left`; one pass `_wire_bundled_jars(c, layout=…)` runs at the end of each mode (`_layout`
  — both branches — after `_drop_bom_property_overrides` and before the final commit; `_boot_pom`; `_msa` before the
  placeholders) for every `profile.bundled_jars` entry: the as-is path is mapped through `c.new_location(path)` (`c.moves`),
  the target pom is `owning_pom` of the current path (a packaging-pom aggregator owner → the comp pom in the layout; pom-only
  modes skip with a reason), the boot poms are the Boot-plugin poms (+ `profile.boot_modules`) that are the module or depend
  on its artifactId (layout: the generated boot pom when none matches), `managed_keys` = `_chain_facts(c).managed` ∪
  `_boot_bom_facts(c).managed`, the repository callable wraps the lazily made `c.repo_lookup` (as `_repository_release`).
  Changed poms and the copy join `c.changed` (the mode's final commit). Follow-ups: `⚠ ` + `bundled_jar_followup(w)` (system;
  compile without ⚠), `unused_jar_followup(name)` = "bundled jar `<name>` is referenced by no source / JSP / XML / properties
  file — not declared (add it by hand if a runtime needs it)"; notes `bundled jars not wired (<verdict>): …` and `bundled jars:
  N wired at the restructure …`; `webinf_left_followup` is written afterwards with the wired jars removed.
* **Orchestrator** — `_journal_restructure_jars(res)` right after the `restructure` journal event: `bundled_jar_wired` with
  `stage: restructure` per wired jar, one `bundled_jars_skipped` when anything was skipped, and a note on each matching
  `profile.bundled_jars` entry (`restructure: wired (<scope>)` / `restructure: <why>`) — the report has no journal access and
  reads these notes. The loop's `bundled_jar_wired` carries `stage: loop`; `_bundled_plan` skips a (pom, jar) whose known g:a
  the pom already declares (`_bundled_declared`).
* **Report** — `wired` column `system (restructure)` / `system (loop)` / the scope word; `status` adds `container API` and
  `unused`; an intro sentence when any restructure note exists; Korean for all of it.
* **Fixture** `webapp-bundled-jar` now holds three jars: `acme-seed-1.0.jar` (unknown coordinates, used → system at the
  restructure: back pom + boot pom `${project.basedir}/../vault-back/lib/…` + `includeSystemScope`), `servlet-api-2.5.jar`
  (javax.servlet classes + pom.properties → container, untouched), `unused-tool-1.0.jar` (referenced by nothing → unused,
  Handover line). The first build passes: no loop attempt, no error case; journal `bundled_jar_wired` (stage restructure) +
  `bundled_jars_skipped`.

## v2.1z-k — orphan version properties, EOL advisories on blocked items, bundled jars in the advisory match (2026-11-04)

**Why (company re-run after v2.1z-j).** (1) `xxx-boot/pom.xml` still held the as-is `java-version=1.8` and
`org.springSecurity-version=4.2.7.RELEASE`: the v2.1o/p cleanups are name-based — a property goes when its NAME is one the
parent chain defines (`_drop_bom_property_overrides`) or when a `<version>` this pass dropped referenced it
(`_drop_dead_version_properties`) — so an as-is spelling (`-version`, camelCase) no pom references any more survives and misleads
people and the review. (2) `org.springframework.mobile` blocked with "no jakarta build found" only: the advisory table has known
that Spring Mobile is EOL (attic, 2016) with a replacement, but the blocked line did not say so, and because the jar
came from `WEB-INF/lib` (no pom declared it) the advisory match (`evaluate_advisories` over `pom_gavs`) never saw it.

**Decisions.** No "spring filter": the property rule is about the VALUE and the references, not the name. A property whose value
looks like a version or a Java level (`VERSION_LIKE_RE`: `1.8`, `17`, `4.2.7.RELEASE`, `2.5.15`, `3.0.0-M1`) and that no project
pom references as `${name}` is an **orphan** and is removed — but only when `layout.drop_bom_property_overrides` is on AND the
chain's facts were read (without them nothing can be known about names the chain uses: nothing is removed, as before); chain-defined
names (the override step's job), `JAVA_PROPS` (`_fix_java` writes them) and names the chain references are skipped. This changes
the v2.1o expectation that a dead CUSTOM version property (`my.version`) is kept — it goes now (`version_properties_removed`,
note `N orphan version propert(ies) removed: …`). A blocked `external_javax_api` / `external_api_incompatible` / `needs_owner`
item whose artifact the advisory table knows gets `; EOL: <title> — replacement: <replacement>` appended to its suggestion
(`_with_eol_advisory`: the profile's findings first, else the catalog's `artifacts` globs). `evaluate_advisories` also matches the
bundled jars' known coordinates, so the Handover, the prompt hints and the blocked line all know the library. Spring Mobile gets a
knowledge note (`knowledge/notes/spring-mobile.md`, keywords `org\.springframework\.mobile`, `DeviceUtils|getCurrentDevice`, …;
linked from the advisory) that tells the fix LLM to write the replacement as a script — a small in-house `compat.device` package
with the same type / method names (`Device`, `DeviceUtils.getCurrentDevice`, `LiteDeviceResolver`,
`DeviceResolverHandlerInterceptor implements HandlerInterceptor`) so call sites only change imports — never to transform the jar's
bytecode (the Eclipse Transformer would rename javax → jakarta but `HandlerInterceptorAdapter` is gone from Spring 6+: a runtime
failure) and never to keep the old jar. The KB stores that script per case, so the next project with `DeviceUtils.getCurrentDevice`
reuses it without an LLM call.

### As built

* `sbmig/restructure/restructurer.py`: `VERSION_LIKE_RE`, `orphan_version_properties(eds, skip)` (pure), `_drop_orphan_version_properties(c)`
  called right after each `_drop_bom_property_overrides(c)` (both `_layout` branches, `_boot_pom`, `_msa`).
* `sbmig/loop/fixloop.py`: `_with_eol_advisory(suggestion, artifact)`; `sbmig/advisory/evaluate.py`: bundled jars join `gavs`;
  `knowledge/libraries/advisories.yml` spring-mobile → `notes: knowledge/notes/spring-mobile.md`; `knowledge/notes/spring-mobile.md`.

## v2.1z-l — as-is dependencyManagement entries that shadow the chain / the imported Boot BOM (2026-11-04)

**Why (company re-run, `attn=… parent-chain-no-boot-bom`).** The user's facts: all in `xxx-back/pom.xml` — `java-version=1.8`,
`org.springSecurity-version=4.2.7.RELEASE`, `<version>${org.springSecurity-version}</version>` on ~4 Spring Security entries and
`${org.springframework-version}` on others — while the company parent manages the lines through `spring-boot.version` /
`spring-framework.version`. Mechanism: with role parents a module does not inherit the root pom, so `_role_module` copies the
as-is root's `<dependencyManagement>` into every module pom (`_inherit_root_content`) and, because the company chain carries no
Boot BOM, `_boot_bom` imports `spring-boot-dependencies` into the same poms. Maven lets a pom's OWN management entry win over an
imported BOM — so the as-is `spring-security-*:4.2.7.RELEASE` shadowed the BOM's 7.x, and the v2.1l/o version cleanup only ever
looked at `<dependencies>` (`dependency_elements(False)`), never at management entries. The properties survived because those
entries still referenced them (not orphans — the v2.1z-k rule was right to leave them).

**Decisions.** The same gte rule for management entries: a project pom's own `<dependencyManagement>` entry whose g:a the new
chain (the chain's full managed set when it reaches the Boot BOM, else its own entries + the Boot BOM this pom imports) manages
at an **equal or newer** version is removed (`_drop_managed_management_entries`, called by `_drop_managed_versions` unless
`layout.drop_managed_versions: off`); BOM imports (scope import / type pom), own modules, protected coordinates, system scope,
unresolvable `${…}` and entries NEWER than the managed version stay; the properties the removed entries referenced go through the
existing dead-property step (`dropped_refs`), the rest through the v2.1z-k orphan rule. Recorded in `managed_versions_dropped`
as `g:a: <v> → managed <m> (<pom>, dependencyManagement)` + note `N as-is dependencyManagement entr(ies) removed: …`. The
structure review (second net, the user's ask "spring 관련은 parent 기준이 맞으니 수정해"): the Company rules block states that
the Spring Framework / Security / Data / Batch / Cloud lines are decided by the parent chain (its family property NAMES, never
values — no version literal in a rule) and the Boot BOM, and that a project pin on another line — in `<dependencies>` or its own
`<dependencyManagement>` — is critical; prompt rule 5 says the same.

### As built

* `sbmig/restructure/restructurer.py`: `_drop_managed_management_entries(c, eds, props_of, protected, own, dropped_refs)`;
  `_drop_managed_versions` keeps `props_of` per pom and calls it before `_drop_dead_version_properties`.
* `sbmig/review/inputs.py boot_version_lines`: the Spring-family line; `sbmig/review/prompts/review_v1.md` rule 5 extended.

## v2.1z-m — the in-house GPT agent gateway: asset id + `params.role`, token exchange (2026-11-04)

**Why.** The company moves the fix / review calls from the legacy gateway to its GPT agent gateway. Same request schema as
discussed on 2026-11-03 (`{model, messages, stream, session_id, thread_id, trace_id, params, async}`) with two differences the
user named today: the top-level `model` is the gateway **asset id**, and the model is chosen by `params.role` — the "model call
name" the gateway maps itself: `build_error_fixer` → gpt-5.5, `escalation_fixer` → gpt-5.6-sol. And a pre-step: a POST to a
token URL with `Authorization: Bearer <app token>` answers `{access_token, token_type: Bearer, expires_in: 3600}`; every LLM call
carries that access token, refreshed before it expires.

**Decisions.** No new HTTP client (the adapter seams stay): `CompanyClient` gains a `gateway` mode and a `TokenManager`.
* `llm.gateway: legacy | agent` (`LLM_GATEWAY`, default legacy — nothing changes for the old gateway). Agent payload:
  `{model: <LLM_ASSET_ID>, messages: [{role, content}], stream: false, async: false, params: {role: <the chosen model name>},
  trace_id: <uuid4 per call>, max_tokens, temperature, **agent_extra}` (`session_id` only to continue a session: "v2.1z-n" … "v2.1z-p")
  — `payload_extra` (`need_origin`) is legacy-only; `thread_id` is never sent ("v2.1z-o"). The model choice stays the loop's: `LLM_MODEL=build_error_fixer` for the fix asks 1–2 and the structure review,
  `LLM_ESCALATION_MODEL=escalation_fixer` from the 3rd ask / after a stall — those names are what goes into `params.role`. The
  reply text (`_REPLY_PATHS` / `LLM_RESPONSE_PATH`), `usage.prompt_tokens` / `completion_tokens` and `choices[0].finish_reason`
  are read as today; the response's `model` (the gateway's real model name, e.g. `gpt-5.5`) is what the cost table prices.
* `llm.token_url` (`LLM_TOKEN_URL`): when set, `TokenManager` POSTs it with `Authorization: Bearer <app token>` (`LLM_APP_TOKEN`,
  else `LLM_API_KEY` is the app token — `llm.app_token_env`), keeps `access_token` + `expires_in` in memory (thread-safe), and
  refreshes it **before** an LLM call when less than `llm.token_refresh_margin_s` (300 s) is left or it has expired; a 401 from
  the LLM call invalidates the token and the call is retried once with a fresh one (one extra attempt beyond `max_retries`).
  Neither token ever appears in a log, a repr or a record (`LLM_RECORD_DIR` records payloads, never headers). A failed exchange
  is an `LLMError` (HTTP status / not JSON / no `access_token`).
* `sbmig llm-probe` prints `gateway= asset_id= token_url= app_token_env= escalation_model=`, performs the token exchange as a
  step of its own (`token exchange: ok (expires_in 3600s, refreshed 300s before expiry)` or `TOKEN ERROR: …` with a hint), then
  the usual request / response dump (the payload shows the asset id and `params.role`; no token).
* Config / env: `llm.gateway`, `llm.asset_id`, `llm.token_url`, `llm.app_token_env`, `llm.token_refresh_margin_s`, `llm.agent_extra`;
  `.env.example` documents `LLM_GATEWAY`, `LLM_ASSET_ID`, `LLM_TOKEN_URL`, `LLM_APP_TOKEN` and the two role names.

### As built

`sbmig/fix/company.py`: `TokenManager` (`token()`, `refresh()`, `invalidate()`, `valid()`, `expires_in`, `refreshes`; lock;
`clock` injectable), `CompanyClient(gateway=, asset_id=, token_manager=, agent_extra=)`, `headers()` takes the exchanged token
when a manager exists, `payload(messages, model, *, max_tokens, temperature, metadata)` builds the agent shape, `_label`,
the 401 branch in `_complete`, the probe's token step (`ProbeResult.token`). `sbmig/fix/client.py make_llm_client`: validates
`gateway`, requires `LLM_ASSET_ID` in agent mode, builds the `TokenManager` when `token_url` is set.

## v2.1z-n — gateway session id from the answer; EOL-library replacements as direct edits; obsolete Spring artifacts (2026-11-04)

**Why (user, after the first agent-gateway run).** (1) The agent gateway creates a session when none is sent and returns its id
in the answer — sbmig should not invent one. (2) A Spring Mobile replacement is project code (new classes under the project's
package, rewritten imports): a script for it is reusable by nobody, and the run showed the cost — script → augment → rollback →
ask, 20 asks burned (`repeated_failure`, `llm=29`). The LLM should fix it by **direct edits**, and the report / case history should
record what it did; no stored script. (3) The run ended on `'dependencies.dependency.version' for org.springframework:spring-dao:jar
is missing`: `spring-dao` is a Spring 2.x artifact that does not exist on the target line; "remove the version, the chain manages
Spring" is wrong for it, and the `sbmig-version` pre-try would have pinned the repository's newest release (2.0.8, 2007).

**Decisions.** (1) `CompanyClient` sends no `session_id`; `LLMResponse.session_id` (new optional tier-1 field) carries the one the
gateway returns (top level, or under `data` / `result`), logged once per value and kept on `CompanyClient.last_session_id` for the
case thread ("v2.1z-p"; `thread_id` is never sent). (2) Prompt rule 12 and
`knowledge/notes/spring-mobile.md`: replacing an EOL library by project code is a `<no_script>` + `<edits>` answer (`op: write` for
new files, `replace` for imports, `pom` for the dependency) — the attempt is recorded as edits in `errors.md` / the KB's case
history (`FixTry.applied = edits`, the edit descriptions, `no_script_reason`), never as a script version. New cap
`script.max_edit_tries_per_case` (4): when every LLM attempt of a case so far was edits-only and their count reaches the cap, the
ladder ends with `repeated_failure` ("N direct-edit attempt(s) … did not resolve …") instead of walking on to 20. (3)
`knowledge/notes/spring-removed-artifacts.md` (keywords `spring-dao|spring-mock|spring-hibernate3|…`, `DaoSupport`, the Maven
"version … missing" text for `org.springframework:spring-*`): the artifact does not exist on the target line — remove it, keep the
classes through `spring-tx` / `spring-jdbc` / `spring-orm` / `spring-test` (managed, no version), migrate the removed APIs; and
`VersionResolver.resolve` never pins an `org.springframework*` artifact that nothing manages from the repository (unknown on purpose).

### As built
`sbmig/fix/company.py` (`_session_of`, `last_session_id`, no `session_id` in the agent payload), `sbmig/core/interfaces.py`
(`LLMResponse.session_id`), `sbmig/fix/prompts/script_system_v1.md` rule 12, `knowledge/notes/spring-mobile.md`,
`knowledge/notes/spring-removed-artifacts.md` + `index.yml`, `sbmig/triage/scripts.py plan_script_step` (the edit cap),
`sbmig/core/config.py` / `config/default.yml` `script.max_edit_tries_per_case`, `sbmig/loop/fixloop.py VersionResolver.resolve`.

## v2.1z-o — gateway sessions: `thread_id` is deprecated, continuation = the returned `session_id` sent back (2026-11-04)

**Facts from the user.** The agent gateway's `thread_id` is deprecated — never sent. A call without `session_id` opens a new
session and the answer carries its id; a call that sends that id back **continues** the conversation; a call that omits it
starts afresh. That is the transport the agreed **case-thread** increment needs (one conversation per case and run; a fresh
`new` after the augments = a new session).

**Built now (plumbing, no behaviour change).** `CompanyClient.payload` sends `session_id` only when the caller passes
`metadata["session_id"]` (the continuation); `LLMResponse.session_id` is read from every answer; the `llm-NNNN.json` record and
the fixer's transcript carry `session_id`; `ScriptProposal.session_id` / `FixAttempt.llm_session_id` keep the id of the answer
an attempt came from, so the next rung of the same case can continue it.

The thread mode itself is "v2.1z-p".

## v2.1z-p — the case thread: one gateway session per case and run (2026-11-04)

**Facts (user, 2026-11-04 night).** The agent gateway's session remembers the system prompt and the earlier turns ("system 롤로
올린 걸 기억한다"); no idle expiry is visible; whether a continued session re-bills the earlier turns is undocumented. `thread_id` is
deprecated. Continuation = send the returned `session_id` back; a fresh start = send none.

**Design (the increment agreed 2026-11-02/03, now built).** `llm.case_thread: true` (default false; needs `llm.gateway: agent`):
* The **first ask** of a case in a run (`new`) sends the full prompt (system + user) with no session id; the answer's
  `session_id` is kept on `ScriptProposal.session_id` → `FixAttempt.llm_session_id`.
* An **augment** of the same case (`ScriptBase.session_id` = the base attempt's) **continues** that session with one user
  message, the **delta** only (`LLMFixer.build_thread_turn`, keys `THREAD_TURN_KEYS`: what the base script did, the errors now,
  the file index and source windows, the poms, the version facts) — no system prompt, no case header / company rules / hints /
  notes / base code (the session has them), then the request for the COMPLETE new script in the same output format.
* **Context and feedback rounds** inside one try send one user message continuing the session of the last answer
  (`_follow_up`); without a session id in the last answer (legacy gateway, replay) the stateless chain goes out as before.
* A fresh `new` after the augments, another case, a stored-script base (no attempt) → no session id → a new session.
* **Fallback** (`llm.thread_fallback_full: true`): a continued turn rejected with HTTP 4xx (unknown / expired session) → the
  full prompt again in a new session, one WARNING; `false` → the error surfaces like any LLM failure.
* **Measurement**: every turn in a session logs `thread turn (session …): sent N chars in M message(s) → prompt_tokens=K` and
  the transcript / `llm-NNNN.json` carry `sent_chars` + `in_session` — compare `prompt_tokens` with `sent_chars` across the
  turns of one case: tokens ≈ the delta → the history is not re-billed; tokens ≈ the whole history → it is (then the saving is
  only the wire, and the mode is a quality tool, not a cost tool).
* Off (default): everything as before — the replay fixtures and the legacy gateway are untouched.

### As built
`sbmig/fix/fixer.py` (`_thread_on`, `build_thread_turn`, `_follow_up`, the continuation + fallback around the first call, the
measurement in `_call`), `sbmig/fix/context.py` (`PackedContext.keys`), `sbmig/core/models.py` (`ScriptBase.session_id`),
`sbmig/loop/fixloop.py` (the base attempt's session on the `ScriptBase`), `sbmig/core/config.py` / `config/default.yml`
(`llm.case_thread`, `llm.thread_fallback_full`).

## v2.1z-q — the asset id in the gateway URL (2026-11-04)

The agent gateway's chat URL carries the asset id in its path (`https://<host>/api/serv/v1/agents/<asset id>/chat/completions`).
`LLM_BASE_URL` is still used verbatim, but a `{asset_id}` (or `${LLM_ASSET_ID}`) placeholder in it is filled from `LLM_ASSET_ID`
(`sbmig.fix.client.agent_url`), so the id lives in one place; a placeholder without `LLM_ASSET_ID` is an `LLMConfigError`.

## v2.1z-s — per-run build hooks (2026-11-04)

Generic per-run hooks (the Kubernetes split layout they were made for is gone: "v2.1z-u"); unset in both deployed shapes.
`build.setup_command` / `build.teardown_command` (argv lists, no shell; default empty = nothing), `build.hook_timeout_s` (600).
`Orchestrator._run_guard` runs the setup once before the run's first step — a non-zero exit, a timeout or an unrunnable command
aborts the run (`build.setup_command failed (rc=N): <output tail>`) — and the teardown once after `_finalize`, whatever happened
(a failure is a WARNING). Both get `SBMIG_RUN_ID`, `SBMIG_RUN_DIR`, `SBMIG_PROJECT_DIR` in their environment; journal
`{"event": "build_hook", "hook": setup|teardown, "rc", "seconds", "command"}`. Dry runs skip the hooks.

## v2.1z-t — the infra team's "one build Job per request" model (guide of 2026-11-05)

**The model.** The Agent (any pod / host with `kubectl` and a kubeconfig at `/etc/kube/config`) creates one Kubernetes **Job**
per migration request from a template (`generateName` → `kubectl create`, not apply); the Job's pod does the WHOLE run
("소스 처리, 빌드, 분석, 결과 저장 및 저장소 커밋") on an **emptyDir** `/workspace` and exits; the build pod gets no kubeconfig.
sbmig fits it as the pod's program: `deploy/docker/run-job.sh` (in the single image) = source in (git clone / zip URL /
mounted `/workspace/in`) → KB in (`SBMIG_KB_GIT_URL` cloned to `/workspace/kb`) → `sbmig run … --config <company yml(s)>
--config <paths yml: kb.dir, llm.record_dir> --out /workspace/out` → results out (the run dir's `project/` git repo pushed to
the source repository as `sbmig/<run id>`; `report.md` / `errors.md` / `sbmig.log` / `journal.jsonl` / `logs/` to
`SBMIG_RESULT_GIT_URL` branch `<run id>`, or `RESULT_PUSH_CMD`; the KB committed and pushed back) → exit (0 when a report
exists — sbmig exit 0 or 2, the report is the result; 3 = sbmig aborted; 4 = results could not leave the pod;
`SBMIG_FAIL_JOB_ON_BUILD_FAIL=1` → exit 2 on a failed build). Secrets (GIT_TOKEN, LLM_*) stay in the environment and in-memory
URLs. Files: `deploy/k8s/80-build-job.template.yaml` (their CHANGE_ME points + sbmig's env), `deploy/k8s/submit-job.sh`
(can-i → `--dry-run=server` → create → `logs -f` → result line), `deploy/README.md` §6 (mapping table, the three decisions).

**Three things the guide does not cover and sbmig needs.** (1) The file KB must outlive the pod — a git repository (pulled /
pushed per run; runs sequential) or a PVC. (2) `~/.m2` on an emptyDir downloads Boot 4 / OpenRewrite / plugins from Nexus on
every Job — a PVC or a warmed image layer. (3) The Agent role in the guide (submit a Job, watch, collect) is not what `sbmig
serve` does: the web UI's own Job dispatch is "v2.1z-z" / "v2.1z-ac". sbmig itself calls no Kubernetes API in this model (the
v2.1z-s hooks stay unset).

## v2.1z-u — deploy/ trimmed to the two shapes the company runs; Korean comments (2026-11-04)

The infra team's one-Job-per-request model (v2.1z-t) supersedes the exec-into-a-builder layout of v2.1z-r/s: `Dockerfile.builder`,
`Dockerfile.agent`, `mvn-k8s`, the sidecar / RBAC / per-run builder pod manifests and the `60-job-run` example were removed.
What stays: one image (`Dockerfile`, now with `curl` for zip downloads and `/workspace`), `run-job.sh`, the storage / config / secret
manifests (the Secret example gained `sbmig-git`), the web-UI Deployment + Service, the Job template and `submit-job.sh`. Every
comment in `deploy/` is Korean (the company's reviewers do not want English comments). The `build.setup_command` /
`teardown_command` hooks (v2.1z-s) remain in sbmig as generic per-run hooks, unset in both shapes.

## v2.1z-v — Korean config comments, the `agent-doc/` pack, the Job dispatch design (2026-11-04)

- `config/*.yml` and `config/settings.nexus.example.xml`: comments rewritten as Korean explanations (what a value decides, who changes it when,
  what happens); keys / values byte-identical (checked by yaml / xml equality). No version tags in comments any more — this file and PLAN.md hold the history.
  `review.record` and `loop.phases.{jdk21,spring,restructure}` are documented as keys without a consumer (kept for compatibility).
- `agent-doc/` (12 Korean files, README = index + rules for the LLM): the pack the company feeds to its in-house LLMs; `tests/docs/test_agent_doc.py`
  checks backticked repo paths, `section.key` config keys, `sbmig <command>` names, relative links, ≤ 350 lines / 30 KB per file, and that the
  README lists every file. Dates / version tags only in `10-open-items-and-history.md`. `docs/v2/LLM-DIAGNOSIS-BRIEF-KO.md` points at `agent-doc/03`.
- Operating model (user): project owners upload a zip in the web UI; the CLI is for developers / tests / diagnosis (Job dispatch:
  "v2.1z-z", "v2.1z-ac").

## v2.1z-w — the web UI redesigned for the project owners; the server decides the config files (2026-11-05)

- `sbmig/web/static/index.html` (one file: HTML + CSS + JS, no web font / CDN — the company network is offline): the company's blue; the drop
  zone is the page (drag-and-drop or 파일 선택, zip only, size vs the upload limit checked before the POST) → `마이그레이션 시작` →
  the run card (status sentence, run id, LLM calls; `중단`) with the stage rail (intake → analyze → plan → one dot per planned phase →
  finalize, lit by `node` events; Korean labels, the node name underneath) and two tabs: 진행 기록 (journal rows, last 400 kept in the
  DOM) and 로그 (the `log` / `console` events go into a ring buffer of 20,000 lines; at most the last 1,500 are rendered, once per
  animation frame, only while the tab is visible; DEBUG hidden by default; the full file through the `sbmig.log` link) → the result card
  once `status.state == finished`: `보고서 받기` (report.md) and `변환된 프로젝트 받기` (result.zip) as download buttons (disabled when the
  file is absent), the other served files as small links, the report text under 보고서 미리보기 → 지난 실행 (collapsed list; `#<run id>`
  still re-attaches). 고급 설정 (collapsed) holds the config checkboxes, the LLM provider (`fake`) and stop_after — developer options.
  Element ids the tests look for: `drop`, `zip`, `start`, `stop`, `rail`, `timeline`, `log`, `dl-report`, `dl-zip`, `adv`.
- The config files a run uses are the **server's** choice: `sbmig serve --config <name>` (repeatable) → `$SBMIG_WEB_CONFIGS`
  (comma-separated) → the first existing of `config/artifacts.yml`, `config/artifacts.local-e2e.yml` → none (default.yml only).
  `RunManager(default_configs=…)` / `RunManager.default_configs()` (existing files only; a missing name is warned once and comes back when the
  file appears); `create_app(default_configs=…)`. `GET /api/configs` adds `"default": [...]`; `POST /api/runs` without a `configs` parameter
  runs with the default, `configs=` (empty) means default.yml only, a given list wins (the page sends the list only when 고급 설정 changed it).
  ConfigMap `sbmig-env` (`deploy/k8s/20-configmap.yaml`) sets `SBMIG_WEB_CONFIGS: "artifacts.yml"`.

## v2.1z-x — a gateway session keeps its model: the case thread opens a new session when the model changes (2026-11-05)

**Fact (user, 2026-11-05).** An open agent-gateway session cannot change its model: `params.role` is fixed when the session is
created; a different model needs a new session.

**Why it matters.** The fix ladder asks the escalation model (`llm.escalation_model`, role `escalation_fixer`) from a case's third
LLM ask on and after a stall (`loop.stall_limit`), and goes back to `llm.model` when the stall ends. With `llm.case_thread: true`
the augment continued the base attempt's session regardless — a continued session would have kept the model it was opened with
(the escalation silently not happening) or been rejected (4xx → the full-prompt fallback, one wasted call per rung).

**Rule.** The model a session runs on travels beside its id: `ScriptProposal.session_model` (the `model` sent with the call; `""` =
the gateway default) → `FixAttempt.llm_session_model` → `ScriptBase.session_model`. `LLMFixer.propose_script` continues the base
session only when this ask's model (`_model(ctx)`) equals `base.session_model`; otherwise it logs
`case thread for <case>: the model changes (<a> → <b>) and a gateway session keeps its model — the full prompt opens a new session`
and sends the full augment prompt without a session id. The next augment on the same model continues the new session. An
escalation request without `llm.escalation_model` configured runs on the same model and keeps the session. Context / feedback
rounds inside one ask never change the model, so they are unaffected. Thread off: nothing changes.

**Cost shape with the thread on.** A case's ladder is now at most two or three sessions (base model → escalation model → back),
each opened with one full prompt; the deltas go on inside each.

## v2.1z-y — a POM problem inherited from a parent pom names the parent pom (2026-11-05)

**Why (company run, 2026-11-05).** `'dependencies.dependency.version' for org.springframework:spring-dao:jar is missing` kept
coming back although the LLM's script (the case's version 24) "removed it": the version-less `spring-dao` sat in the **root pom**,
Maven reports an inherited problem once per child module (`The project com.company:app-back:1.0 (…/app-back/pom.xml) has 1 error`
→ `… is missing. @ com.company:app:1.0, …/pom.xml, line 88, column 21`), and the parser recorded the error with `file =
app-back/pom.xml` (the project that failed), so the prompt said "app-back/pom.xml: … spring-dao … is missing" and the script edited
the child pom. The declaring pom is the path in Maven's location tail.

**Rule.** `_Parser._pom_location`: the location tail `@ [<gav>, ][<path>, ]line N[, column M]` of every POM problem (missing
version, non-resolvable parent / import POM, other model problems) gives the error its `line` / `column`, and when the tail carries
a path (an inherited problem: the parent pom) that path, relativised to the project root, is the error's `file`; without a path the
project's own pom stays the file. The message and the signatures are unchanged (no KB case moves). The missing-version pre-try
(`_declaring_pom`) and the script prompt (`fmt_error`: `pom.xml:88 …`) start from the pom that holds the declaration.

**Open (the user decides, KB impact).** The L2 of a missing-version error is the generic `pom_invalid|missing_version` (the L1
carries the g:a), so every version-less artifact of every project shares ONE case id — the company's case reached version 24 with
scripts written for different artifacts. The errors the pre-try cannot fill (an obsolete Spring artifact to remove, a company
artifact) are per-artifact problems; `pom_invalid|missing_version|<g:a>` as the L2 would give each its own case — a signature change
that orphans the stored case (see the KB-matching review: `kb reindex --resign` first).

## v2.1z-z — a pom fix that uncovers the next build stage is not a regression; the global settings behind `build.mvn` (2026-11-05)

**Why (company run, 2026-11-05, continued).** The `spring-dao … is missing` error sat in `xxx-back/pom.xml` itself (Maven's tail
`@ line 506, column 21`, no path) and the LLM's script did remove it — the attempt was **reverted by the scorer**: once the
stage-0 pom problem was gone the build advanced to dependency resolution and reported `Could not find artifact
<group>:SSOClient_Common:jar:1.0.0` for the first time; `dependency_resolution` is a structural kind the previous build had never
reached, and the rule "a new structural error kind after a pom edit → regressed regardless of counts" (a made-up
dependency masked every compile error) reverted the fix. Next round the same error, another augment — the case reached script
version 24. And the repository lookup had no Nexus: `sbmig repo-probe` listed only a pom repository and Central, because the
company PC keeps Maven off PATH (`build.mvn` = the full path of `mvn.cmd`) and the Nexus mirror lives in that installation's
`conf/settings.xml`, which `global_settings_file()` looked for only through `MAVEN_HOME` / `M2_HOME` / PATH.

**Rules.**
* `BuildFixLoop._uncovered_stage`: when a pom-editing attempt removed EVERY targeted error and all the new structural errors are
  of a LATER stage than the targets (`STAGE`: pom 0 → dependency / plugin resolution 1 → enforcer 2 → compile 3 …), they were
  hidden behind the fixed problem, not caused by the attempt: the attempt is scored by the normal rules (improved), with the note
  `the build advanced past the fixed pom problem: N <kinds> error(s) of a later stage were hidden behind it — not a regression of
  this attempt`. A new structural kind of the same or an earlier stage, or targets still present, keep the old verdict (regressed →
  reverted).
* `global_settings_file(cfg)`: the Maven installation is also taken from the configured `build.mvn` (resolved like the build runner,
  `mvn_executable`), so `<that installation>/conf/settings.xml` joins the settings files; `RepositoryLookup.repository_urls` passes
  its config.

* The lookup says what it looked for and could not use (same day, the user's probe still showed no Nexus although
  `~/.m2/settings.xml` exists): `RepositoryLookup.settings_missing` lists every settings candidate that was missing or
  unreadable (`<path> (~/.m2/settings.xml): missing`), logged as `repository lookup: settings …`; a settings.xml that does not
  parse is a **WARNING** (`settings <path> could not be read (<error>) — its mirrors / repositories / servers are NOT used …`)
  instead of a debug line; `sbmig repo-probe` prints `settings looked for but not read: …` and, without `--project`,
  `pom <repositories>: not included (no --project given; a run reads the project's poms)` — a probe run from the sbmig checkout
  does not see the project poms' `<repositories>` that a run does, which is the usual reason "the run saw Nexus, the probe does not".

* **Maven's HTTP blocker (the company PC's actual cause, read from the probe the user typed back).** `settings read:` listed the
  user settings AND `C:\apache-maven-3.9.16\conf\settings.xml`; the repository list was `1. http://0.0.0.0/ [settings mirror
  'maven-default-http-blocker' (mirrorOf external:http:*) covering xxx-internal-releases — global settings]`, `2. Maven Central`.
  The company Nexus is the user settings' active-profile repository over plain **http**; Maven 3.8.1+ ships the blocker mirror in
  the global settings, and sbmig applied it as Maven does — asking `http://0.0.0.0/` (WinError 10049) instead of the Nexus; the
  404s came from Central. **Why it "worked before":** until v2.1z-e (2026-11-02) sbmig never read the global settings, so the
  blocker was invisible and the http Nexus was asked directly; the user changed nothing. Now (`is_blocking_mirror`: `<blocked>true`
  or host `0.0.0.0`; `SettingsData.blocked`) a repository covered by a blocking mirror is **not asked** and `url_notes` says
  `<id> (<url>) is blocked by Maven's HTTP blocker (mirror '<id>', mirrorOf external:http:*, global settings): Maven 3.8.1+
  refuses plain-http external repositories — use an https URL, override the mirror in the user settings.xml, or set
  build.repository_url (asked first, never mirrored)`; the probe prints it as `note:`. A user settings mirror that redefines the
  blocker (same id / mirrorOf, the real URL, `<blocked>false</blocked>`) wins by order, as in Maven. The explicit
  `build.repository_url` bypasses mirrors on purpose (the operator's decision) — the user set it the same day; the `<server>`
  login of the blocked repository still reaches it when both name the same URL (the blocked repository has left the set, so the
  credential lookup also reads the settings' repositories, not only the set and the mirrors), else `build.repository_server_id`.
  **Deployment consequence:** a Job pod starts with an empty `~/.m2`; if the company Nexus is http, Maven 3.9 in the image blocks
  it too and every in-house artifact fails with `Blocked mirror for repositories` — the ConfigMap's settings.xml must override
  `maven-default-http-blocker` (or the Nexus must be https). `config/settings.nexus.example.xml` and `deploy/README.md` say so.

* **Build extensions (2026-11-05, company run right after the mirror fix: `Unresolveable build extension: Plugin
  org.apache.maven.wagon:wagon-webdav:jar:1.0-beta-2 or one of its dependencies could not be resolved: Failed to read artifact
  descriptor …`).** A `<build><extensions>` entry Maven cannot resolve stops the model build before any goal. The parser
  (`_BUILD_EXT_RE`) now reads the extension's coordinates into `BuildError.artifact` (kind stays `pom_invalid`, stage 0), merges the
  summary line and the project-block line of the same extension into ONE error keeping the longer message (the block carries the
  transfer reason: 401 / 404 / timeout), and the signature is per extension: L1 `pom_invalid|build_extension|g:a:v`, L2
  `pom_invalid|build_extension|g:a` (a new shape — no stored case moves). The new note `knowledge/notes/maven-build-extensions.md`
  (keywords: the Maven line, `org.apache.maven.wagon` / `wagon-*`, the `dav:` / `scp:` / `ftp:` schemes) tells the LLM that Wagon
  providers are deploy transports `mvn clean package` never needs — remove the extension (and a `dav:`-style
  `<distributionManagement>` left in a module pom; the company parent supplies the deploy target), never add a repository /
  version / credentials for it, never decline; a non-transport extension is treated like an unresolvable plugin. Why the PC saw
  it only now: the artifact had lived in `~/.m2` under the old repository id; the new user-settings mirror changed the ids Maven
  checks (`_remote.repositories`), so it was fetched again — and the `<server>` credentials keyed by the OLD repository id no
  longer apply to the mirror id (the `<server><id>` must equal the mirror's id). **Same night, the rerun hit it again:** the
  fix loop can only remove the extension AFTER OpenRewrite, and OpenRewrite is a Maven run too — it failed on the same
  model error, no recipe ran, and the loop then met the extension as the first error. So the intake removes deploy-transport
  extensions BEFORE the first Maven call: `sbmig/analyze/build_extensions.py` (`strip_deploy_extensions`) reads the knowledge
  list `knowledge/libraries/build_extensions.yml` (`intake.build_extensions_file`; rows `group` exact + `artifact` glob —
  shipped: `org.apache.maven.wagon:wagon-*`), removes every matching `<extension>` from every pom's `build/extensions` and
  `profiles/*/build/extensions` (resolving `${…}` for the record, dropping an emptied `<extensions>`, Maven-skipped paths and
  intake-ignored dirs untouched), commits `intake: deploy transport extension(s) removed (N)` with one body line per removal,
  flag `deploy-extensions-removed`, journal `build_extensions` (`removed`, `kept`, `poms`), one Handover line per removal;
  unlisted extensions (os-maven-plugin …) are logged and kept — an unresolvable one still reaches the loop as the
  per-extension case above. `intake.strip_deploy_extensions: false` turns it off. The note now also says the declaration may
  sit in a parent pom (Maven names the module that failed to load) — search every pom. **The actual PC case (read back the
  same night):** the project's poms declare no extension at all — it is inherited from the as-is in-house parent pom in Nexus
  (nothing to strip; the restructure's parent swap drops it later) — and the pom's download had failed under the HTTP
  blocker, so `~/.m2` held `wagon-webdav-1.0-beta-2.pom.lastUpdated` and Maven refused to retry for a day although Nexus now
  serves it; the model-level message carries no "was cached" wording, so no `-U` retry fired. Now `negative_cache_hits` also
  counts `Failed to read artifact descriptor for g:a:…` and `Non-resolvable parent / import POM … Could not find artifact g:a`
  (a dependency-level `Could not find artifact` still does not), the build runner retries those once with `-U` as before,
  and the two Maven calls that precede the fix loop do the same: the as-is effective-pom capture (`effective_pom_force_update`
  journal, log `effective-pom-…-U`) and the OpenRewrite run (`OpenRewriteRunner._force_update_retry`: journal
  `rewrite_force_update_retry` / `…_result`, a final failure prefixed `force-update-retry: `, off with
  `build.retry_negative_cache: false` or `-o`). Operator remedy on a PC: delete the `*.lastUpdated` files under `~/.m2`, never the
  whole `~/.m2`. **And the declaring pom turned out to be the migration TARGET parent itself (`base-parent 4.0.0`):** every
  migrated project will inherit that extension, so Maven must be able to download it everywhere (a fresh Job pod `~/.m2` included).
  Intake pre-check `sbmig/build/parent_extensions.py` (`check_parent_build_extensions`, called in `_step_intake` before the
  effective-pom capture): `ParentChainFacts.build_extensions` / `build_extension_sources` (new; `PomSummary.extensions` from
  `<build><extensions>`, `${…}` resolved with the declaring pom's properties) → per extension `PomReader.pom` (local repository,
  then the repository set; since 2026-11-06 `ExtensionCheck.where` names the URL that served it — `repository <url>` from
  `RepositoryLookup.pom_source`, the lookup remembering the source of every `pom_text` — and the intake log adds that Maven asks
  its settings mirror, not that URL: the company run read the pom from `build.repository_url` while Maven got a 404 from the
  mirror's repository and then PKIX from Central); readable from the repository but stale `*.lastUpdated` markers in `<local repo>/<g>/<a>/<v>/` → the
  markers are deleted (that folder only, `build.retry_negative_cache` governs it) so the first Maven call succeeds without waiting
  for a `-U` retry; readable nowhere → WARNING, flag `parent-extension-unresolvable`, one Handover line (`the company parent
  <g:a:v> declares the build extension <g:a:v>, which neither the local repository nor the repository set serves …`); journal
  `parent_build_extensions` {checks, unresolvable, markers_removed}. The check follows the extension pom's `<parent>` chain as
  well (Maven reads it — "or one of its dependencies could not be resolved"): a missing ancestor makes the extension
  "NOT readable — its parent pom <g:a:v> is not readable …", served ancestors are listed (`ExtensionCheck.ancestors`) and their
  stale markers cleared too; the extension's dependency jars are left to Maven (the error line's 401 / 404 reason, the -U retry).
  Recommendation to the parent's owners (not sbmig's call):
  drop deploy transports from the parent — Maven 3.9 needs none for Nexus over http(s) and the 2007 artifact is a liability.

* **k8s Job dispatch mode (2026-11-05 night — everything on one shared PVC; parallel Jobs: "v2.1z-ae").** `sbmig serve --mode k8s-job` / `SBMIG_WEB_MODE=k8s-job`
  (`RunManager(mode=, job_client=, job_template=)`; `SBMIG_JOB_TEMPLATE` default `deploy/k8s/81-web-job.template.yaml`,
  `SBMIG_K8S_NAMESPACE`, `SBMIG_JOB_POLL_S` 2, `SBMIG_JOB_STOP_GRACE_S` 600 — 2700 since the review round of the same night). Upload → `<out>/<run id>/source.zip` + a `web_run`
  journal line (mode k8s-job) → the FIFO queue (`SBMIG_JOB_MAX_PARALLEL` slots, "v2.1z-ae") → `_run_job`: `render_job` (metadata.name `job_name(run id)`
  = `sbmig-<run id>` RFC 1123, label `sbmig-run-id`, env SBMIG_SOURCE_ZIP / SBMIG_OUT / SBMIG_RUN_ID / SBMIG_CONFIG
  (`<repo root>/config/<name>` — same image, same mounts) + SBMIG_STOP_AFTER / SBMIG_LLM_PROVIDER) → `K8sClient.create_job` →
  poll `job_state` (Creating / Pending / Running / Succeeded / Failed / Missing; pod name, waiting reason, container exit code),
  each change a `node` event `{"node": "job", "update": {"state", "job", "pod", "message"}}` (the UI's '파드' step before 접수,
  from `status.mode`) → end on the pod's `job-status.json {exit_code}` (run-job.sh writes it) or the Job's terminal state;
  Missing / Failed without a code → exit 3 with `Run.error`. Stop: `stop.request` written into the run dir
  (`sbmig.graph.engine.STOP_REQUEST_FILE`; every node but finalize checks it first → `orch._abort(report, STOP_REQUEST_REASON)`,
  the node skipped, finalize skips the final build, exit 3, journal `stop_request`), the Job deleted after the grace period;
  a server shutdown leaves Jobs running (`shutdown()` cancels in-process runs only). The pod's progress reaches the UI through
  the shared run dir: `events()` tails `journal.jsonl` / `sbmig.log` as before and turns `graph_node_end` lines (new: the
  engine's `_staged(name, fn, orch)` journals {node, phase, phases, aborted, stopped} after every node) into `node` events for
  job runs and file replays. `status()` carries `mode` and `job {name, phase, pod}` and ends a run on `job-status.json`.
  `sbmig/web/k8s.py` is a thin in-cluster client over `requests` (SA token / CA, `KUBERNETES_SERVICE_HOST`) — no `kubernetes`
  wheel for the offline network; `K8sClient.in_cluster()` raises `K8sError` outside a cluster and the run fails readably.
  run-job.sh shared-volume mode: `SBMIG_SOURCE_ZIP`, `SBMIG_OUT` + `SBMIG_RUN_ID` (`--run-id`), `SBMIG_KB_DIR` (else the config's
  kb.dir), records beside the run, no push, `job-status.json`. Manifests: `81-web-job.template.yaml` (same PVC `/data`, RWX); the
  shipped access model and mode are in "v2.1z-ab" and "v2.1z-ad" (Shipped mode); operator text deploy/README §3.
  **Development without a cluster (same night, the user: "개발 모드일 때는 파드 생성 없이"):** `SBMIG_WEB_MODE=local-job`, and
  `k8s-job` outside a cluster (`in_cluster_available()` false, no injected client) degrades to it with one WARNING
  (`RunManager.effective_mode`, reported as `status.mode`); `sbmig/web/localjob.py LocalJobClient` implements the same three
  calls with a subprocess: `create_job` runs the command run-job.sh would run (`python -m sbmig.cli run <zip> --out … --run-id …
  --config …` + stop_after / llm_provider) with stdout in `<run dir>/job-local.log`, `job_state` maps the process (Running →
  Succeeded for sbmig exit 0 / 2, Failed otherwise) and writes `job-status.json` like run-job.sh, `delete_job` terminates it; a
  missing template file is replaced by a minimal manifest in local-job mode.
  Not built yet: the external intake API (`/api/migrations`, remediation's
  contract: zip + project_name / name + Idempotency-Key — the implementation brief for the in-house LLM is
  `docs/v2/API-MIGRATIONS-KO.md`, 2026-11-06: raw zip body + query, `<out>/_idempotency/` files, one state-translation dict,
  `RunManager.start_file(extra=)` + `migration.json` as the only run-manager change), cluster measurements. The Main-pod ↔ Job-pod
  run-state contract (who writes which file, ids, exit codes, stop, what a restart recovers, the FIFO slots, how a second Job kind
  reuses the dispatcher) is `docs/v2/JOB-RUN-STATE-CONTRACT-KO.md` (2026-11-06, for the remediation team's integration plan).

**Operator note.** When `sbmig repo-probe` does not list the company Nexus, first run it as a run would:
`sbmig repo-probe g:a:v -c config/<company>.yml --project <run dir>/project` and read `settings read:` / `settings looked for but
not read:` / `build.repository_url: <url> | not set` (2026-11-05: the value the lookup actually saw — `not set` with a value in the
yml means the yml did not arrive: no `-c`, or the key is not under `build:`) / the `[origin]` of every URL. The config loader now
names the two silent traps behind that: `config: <file>: key 'build' appears twice (lines A and B) — YAML keeps the LAST block
only …` (`_YamlLoader`; a second `build:` block replaces the first) and `config: repository_url is not a known key and is ignored
(check the indentation / spelling — did you mean build.repository_url?)` (`_warn_unknown_keys`: the yml keys only — section fields and the entries of
`dict[str, Model]` fields such as `loop.phases` are checked, dict / list fields are not; `SBMIG_*` env overrides are not checked
because `SBMIG_WEB_CONFIGS` / `SBMIG_WEB_TOKEN` / `SBMIG_MAX_UPLOAD_MB` land in the same dict and are dropped on purpose; the
`SBMIG_<SECTION>__<KEY>` overrides — the names with a double underscore — ARE checked separately: `config: env override SBMIG_*__*:
loop.stal_limit is not a known key …`). **Deployment env list (2026-11-05, the infra team asked for the ConfigMap variables):**
ConfigMap `sbmig-env` holds everything that is not a secret (web pod: `SBMIG_WEB_CONFIGS`, `SBMIG_WEB_OPEN`, `SBMIG_MAX_UPLOAD_MB`,
`NO_PROXY`; gateway: `LLM_GATEWAY`, `LLM_BASE_URL`, `LLM_ASSET_ID`, `LLM_TOKEN_URL`, `LLM_MODEL`, `LLM_ESCALATION_MODEL`;
the tuning knobs as commented `SBMIG_<SECTION>__<KEY>` entries with their defaults), Secret `sbmig-llm` only the
secrets (`LLM_APP_TOKEN` / `LLM_API_KEY`, `NEXUS_USER` / `NEXUS_PASSWORD` read by settings.xml's `${env.…}`, optional
`SBMIG_WEB_TOKEN`); the Deployment and the 80 Job template read both through `envFrom` with the Secret last (it wins); the 81
template has one `configMapKeyRef` / `secretKeyRef` line per key ("deploy"). The JVM heap is `openrewrite.jvm_args` (there is no
`build.jvm_args`).
The table operators read is deploy/README.md "env 목록". A failure while the repository set is
built is a `note:` / WARNING `repository set incomplete: building it failed midway (…)` instead of a silent Central-only list. If the Nexus is still absent, the lookup set is wrong for every library: set
`build.repository_url` (the group URL) — and `build.settings_file` when the settings are not at `~/.m2/settings.xml` — in the
company yml; `config/settings.nexus.example.xml` says the same. A bundled jar whose coordinates sbmig could not read (no
`pom.properties`) is declared with placeholder coordinates `<project group>:<jar stem>:<file version>`; Nexus answers 404 for those
even when it holds the jar under its real coordinates — the real ones go into `<kb>/jar_names.yml`.

## v2.1z-aa — a library sbmig knows how to replace is neither a library block nor an accepted decline (2026-11-06)

Company run (spring-mobile, 2026-11-06): the LLM's first answer wrote half of the replacement (compat classes under the library's own
package `org.springframework.mobile.device`, no import rewrite, the jar still in the pom); the second answer declined (`<blocked>`).
The loop accepted the decline (`script.max_declines_per_case` 1) → `_block_needs_owner` → `classify_blocked` → BLOCKED
`external_javax_api` ("a dependency API requires javax.servlet.http.HttpServletRequest …; the LLM declined: …"), and the same-cause rule
blocked the neighbouring cases without an LLM call ("blocked (needs_owner): same cause as …"). sbmig had the fix in
`knowledge/notes/spring-mobile.md` all along. Contract:

- **`NoteEntry.replacement: bool`** (front matter and `index.yml` key `replacement: true`; today only `spring-mobile.md`): "sbmig knows
  the fix — it is project code". `sbmig.fix.context.replacement_notes(notes, text, case, phase)` = the matching flagged notes;
  `sbmig.triage.blocked.note_text(ws, errors)` = the text they are matched against: the javac messages **plus the import lines of the
  errors' files** (`imports_text`, ≤ 12 files), because a javax/jakarta type mismatch never names the library — its import line does.
- **Triage** (`DecisionEngine.notes`, attached by the loop through `Triager.attach_run(notes=…)`; `BuildFixLoop._knowledge_notes()`
  loads `knowledge/notes` once): an `external_javax_api` / `external_api_incompatible` candidate whose case a replacement note matches
  (errors, import lines, owner classes) is **not** blocked by `loop.blocked_policy`; the SCRIPT rationale reads
  `… looks like external_javax_api (…) but the migration note '<title>' gives the replacement as project code — not blocked,
  script.max_edit_tries_per_case applies`. The edit-tries cap (4) and the LLM cap (20) still end the case (`repeated_failure`).
- **Loop**: `_decline_contradiction` asks `_note_contradiction` first — a decline on such a case is rejected like the v2.1z-e
  "no jakarta build" decline (attempt note `decline rejected: the migration note '<title>' (<file>) gives the replacement as project code
  — the fix is inside the project, not a blocker`; journal `decline_rejected` now carries `applied`). **A rejected decline that carries
  edits / a script is applied as an ordinary answer** (before: "the answer named a blocker (<blocked>): its edits / script were not
  applied"); one without is `apply_failed` and asked again with the fact. Accepted declines are unchanged.
- **The note** gained three rules for the LLM: never `<blocked>` for this library; compat classes under the project's package, never
  under `org.springframework.mobile.*` (shadowing the library package leaves every class the LLM did not write coming from the old jar
  with javax signatures — the residual errors of the company run); one answer = compat files + every import rewrite + the pom edit
  (a `no_change` answer is rolled back).
- Not changed: for a library without such a note, `loop.blocked_policy` still counts edits-only LLM answers as "LLM script attempts".

## v2.1z-ab — the infra team's agent model: kubeconfig access, the Job template as a ConfigMap, company cluster values (2026-11-09)

The infra team's temporary manifest for "the agent" (deployed 2026-11-05 in namespace `agent`) showed their model: the agent pod gets a
**kubeconfig Secret** mounted at `/etc/kube/config` (env `KUBECONFIG`) instead of a ServiceAccount, the Job template as a **ConfigMap** mounted
at `/templates` (env `JOB_TEMPLATE_PATH`), `hostAliases` for a gateway host the cluster DNS does not resolve, the image from their Harbor
registry. Contract:

- **`sbmig.web.k8s.kubeconfig_path()`** = the file `KUBECONFIG` names, when it exists (first existing entry of an `os.pathsep` list); nothing
  implicit — `~/.kube/config` is never picked up on its own. **`cluster_access_available()`** = `in_cluster_available() or kubeconfig_path()`.
- **`K8sClient.from_kubeconfig(path=None, namespace=None, session=None)`**: current-context's cluster (`server`, `certificate-authority[-data]`,
  `insecure-skip-tls-verify`) and user (`token` / `tokenFile`, `client-certificate[-data]` + `client-key[-data]`, `username` + `password`);
  base64 `*-data` is written to a 0600 file in a `TemporaryDirectory` the client keeps alive (`_tmp`), relative paths resolve against the
  kubeconfig's folder (as kubectl does); an `exec` / `auth-provider` user is refused with `K8sError` (sbmig runs no external command).
  Namespace: argument → `SBMIG_K8S_NAMESPACE` → the context's namespace → `default`. `K8sClient.cert` (a `(crt, key)` pair) is passed to every
  request; the `Authorization` header is only set when there is a token (Bearer) or a password (Basic). `K8sClient.source` says which
  ("in-cluster service account" / "kubeconfig <path> (context <name>)").
- **`K8sClient.connect(namespace=None, session=None)`**: explicit kubeconfig first, else `in_cluster()`. `RunManager` uses it for the first Job;
  `SBMIG_WEB_MODE=k8s-job` degrades to `local-job` only when **neither** a service-account token nor a `KUBECONFIG` file is present (so a developer
  can point the web at a real cluster — the Mac Studio's colima — by exporting `KUBECONFIG`). Inside a cluster without either the start still fails
  (message names both).
- **Job template**: in `k8s-job` a missing template file is a readable run failure (`job template not found: <path> — mount the ConfigMap
  sbmig-job-template at /templates (deploy/README.md §3) …`, exit 3, no Job created). The Deployment mounts ConfigMap `sbmig-job-template`
  (`optional: true`) at `/templates` and sets `SBMIG_JOB_TEMPLATE=/templates/81-web-job.template.yaml`; the operator fills the template and runs
  `kubectl -n agent create configmap sbmig-job-template --from-file=deploy/k8s/81-web-job.template.yaml --dry-run=client -o yaml | kubectl apply -f -`.
  `.dockerignore` no longer drops `deploy/k8s/` (the default template path exists in the image again), only the filled `deploy/k8s/30-secret.yaml`.
- **Manifests** (`deploy/k8s/`): `namespace: agent` on every resource; both PVCs `ReadWriteMany` + `storageClassName: nfs-csi`; image
  `CHANGE_ME_REGISTRY/sbmig:CHANGE_ME_TAG` + `imagePullPolicy: IfNotPresent` (the registry exists after all; the node-import path is README §1.1);
  the kubeconfig access model and `k8s-job` mode switched on as shipped ("v2.1z-ad", Shipped mode); `hostAliases` as commented placeholders
  in the Deployment and the Job template (the Job pod is the one calling the LLM gateway). Real company hostnames / IPs stay out of the repo.
- **One config file (same day, user)**: the company edits only `artifacts.yml` — the ConfigMap key, the Deployment mount (`/app/config/artifacts.yml`),
  `SBMIG_WEB_CONFIGS=artifacts.yml` and the Job template's `SBMIG_CONFIG` all name it; `company.yml` (the 2026-11-04 name) is gone from `deploy/`.
- **Kept from the company's Worker contract**: `llm.record_dir` / `LLM_RECORD_DIR` are commented out in `20-configmap.yaml`; both Job
  templates carry `automountServiceAccountToken: false`.
- **Worker template rules (same day, from `worker_template` / `worker_secret_bindings`)**: one container, `backoffLimit 0`, `restartPolicy Never`,
  parallelism / completions 1, no init / ephemeral containers; **no `envFrom`**; exactly one writable whole-PVC mount that contains the output dir
  (`/data` ⊇ `/data/out`), no mount under it; env named `LLM_RECORD_DIR` / `SBMIG_WEB_TOKEN` / `WEB_API_TOKEN` forbidden, any name containing
  TOKEN / PASSWORD / SECRET / API_KEY must be `valueFrom`. `81-web-job.template.yaml` follows these rules (explicit `configMapKeyRef` /
  `secretKeyRef` entries with `optional: true`); Main validates them at startup ("v2.1z-ac" `validate_worker_template`).

## v2.1z-ac — production hardening of the Job dispatch, built here from the deployment team's spec (2026-11-09)

The company's deployment engineer had an AI write a Main/Worker hardening on a company branch (persistent queue, ownership, hash
pinning, result verification, secret scrubbing) and left on holiday with it unverified; the closed network cannot export it. The user's
decision: build the same spec in flow-v2, where it can be tested and shipped as the image, and park the company branch. Main does not
read Secret revision files; Secrets are immutable and versioned by name. Contract:

- **`sbmig/web/contract.py`** — the Main ↔ Worker file contract. Only Main writes `<run dir>/control/`: `request.json`
  (`RequestRecord`: run_id, accepted_at, upload, configs, `zip_sha256`, `config_digests` = sha256 of `config/default.yml` + each selected
  yml, `sbmig_version`, mode, stop_after, llm_provider, `attempt` = 1) written once at intake, and `state.json` (`ControlState`: state
  `queued → creating → created → running → finished | stopped | lost | failed`, `job`, `owner` = 16-hex per-request token, `attempt`,
  `accepted_at`, `updated_at`, `exit_code`, `error`; v2.1z-ad adds `env_pins` / `env_pin_policy`, see below) rewritten atomically (temp
  file + `os.replace`) on every transition. The Worker
  writes the run's outputs and `job-status.json` (`JobStatus`: `exit_code`, `finished_at`, `run_id`, `attempt`, `sbmig_version`,
  optional `error`); `JobStatus.matches(run_id, attempt)` is what Main trusts (a run-job.sh style file without ids still matches).
  Labels `sbmig-run-id` / `sbmig-owner` (`RUN_LABEL` / `OWNER_LABEL`), `WORKER_COMMAND = ["python", "-m", "sbmig.web.worker"]`,
  `LLM_RECORDS_ENV = "SBMIG_WORKER_LLM_RECORDS"`, `secret_env_names()` / `secret_values()` (env names containing TOKEN / PASSWORD /
  PASSWD / SECRET / API_KEY, values ≥ 6 chars, longest first), `verify_request(record, repo_root, zip_path, run_id) -> [problems]`.
- **`sbmig/web/worker.py`** (`python -m sbmig.web.worker`, the Job template's `command`; `main(argv=None, *, environ=None,
  run_child=None) -> int`): env `SBMIG_SOURCE_ZIP` / `SBMIG_OUT` / `SBMIG_RUN_ID` (required), `SBMIG_CONFIG` (paths or names; default =
  `request.json` configs under `<repo root>/config/`), `SBMIG_STOP_AFTER`, `SBMIG_LLM_PROVIDER`, `SBMIG_KB_DIR`, `SBMIG_WORKER_LLM_RECORDS`
  ("1" keeps `llm.record_dir`; otherwise `worker-paths.yml` forces `record_dir: ""`), `SBMIG_FAIL_JOB_ON_BUILD_FAIL`, `SBMIG_PYTHON`,
  `SBMIG_APP_DIR`. Steps: read `request.json` (missing → refused), `verify_request` (any mismatch → journal `worker_refused`,
  `job-status.json {exit_code: 3, error}`, exit 3), journal `worker_started`, run `python -m sbmig.cli run <zip> --config … --config
  worker-paths.yml --out <out> --run-id <id> [--stop-after] [--llm-provider]` with cwd = repo root, redact the run dir
  (`sbmig/web/redact.py`: secret values + URL userinfo → `***` in top-level `*.md|json|jsonl|log|txt|yml` and `logs/`; never `control/`,
  `source.zip`, `result.zip`, `project/`; changed in v2.1z-ad, see below: this is now the final pass after write-time redaction, and an
  environment check runs before `worker_started`), print the last `SBMIG-RESULT` line, journal `worker_finished`, write
  `JobStatus(exit_code=rc, run_id, attempt, sbmig_version)`; exit 3 for rc 3, 2 for rc 2 only with `SBMIG_FAIL_JOB_ON_BUILD_FAIL=1`, else
  0. A child crash still
  writes a status file (exit 3). `deploy/docker/run-job.sh` keeps its shared-volume branch for manual use; the infra one-Job model (git
  clone / push, `80-build-job.template.yaml`) is unchanged.
- **`sbmig/web/k8s.py`**: `render_job(template, run_id, env, namespace=None, owner=None)` labels the Job and the pod template with
  `sbmig-run-id` and (when given) `sbmig-owner`; `JobState.labels` (from the Job's `metadata.labels`; changed in v2.1z-ad, see below:
  `None` = a client that cannot report labels, `{}` = a Job without labels, which is not ours); `K8sClient.can_i(verb, resource,
  group="")` = one SelfSubjectAccessReview; `validate_worker_template(template, out_dir) -> dict` raises `K8sError("MIGRATION_TEMPLATE_INVALID:
  …")` for: unreadable / not `batch/v1 Job`; ≠ 1 container or init/ephemeral containers; `backoffLimit` ≠ 0, `parallelism` / `completions` ≠ 1,
  `restartPolicy` ≠ Never; container without name / image; `envFrom`; env `LLM_RECORD_DIR` / `SBMIG_WEB_TOKEN` / `WEB_API_TOKEN`, or a
  secret-named env with a literal `value`; with `out_dir` given: not exactly one writable whole-PVC mount containing it, or any mount under it
  (`out_dir=None` skips the mount rules — injected test clients). Constants `TEMPLATE_INVALID`, `FORBIDDEN_WORKER_ENV`, `REQUIRED_PERMISSIONS`,
  `STATUS_GRACE_ENV`.
- **`sbmig/web/runs.py`** (`RunManager`): **startup** in `k8s-job` — `K8sClient.connect()` now (not lazily), `validate_worker_template(template,
  out_dir)` (the validated dict is what Jobs are rendered from), RBAC self-check (create/get/list/watch/delete `jobs`, get/list/watch `pods`);
  any failure is a `ValueError` and the app does not start (the message names the rule / the denied verbs). **Intake** writes `request.json` +
  `queued` before the worker thread. **Create**: render with the owner label → write `creating` + job name → `create_job`; `HTTP 409` or a
  transport error (`RequestException` / `ConnectionError` / `Timeout` / `ChunkedEncodingError` / `ProtocolError`) → look the Job up: present
  and ours → adopt (journal `job_adopted`), present but another owner → error, absent → re-raise (changed in v2.1z-ad, see below:
  "ours" = both labels, the lookup asks until the API answers); then `created`; `running` when the pod runs. **Result**: `job-status.json`
  accepted only when it matches run id + attempt (a foreign file is ignored with one warning); after the
  Job ends, Main waits `SBMIG_JOB_STATUS_GRACE_S` (90 s; NFS attribute cache) for the file before falling back to the container exit code
  with `verified: false`; `Missing` → `lost` (after a stop request `stopped`, v2.1z-ad); `success` comes from `report.json` only for a
  verified result (else `None` and the error says "result not verified"). **Stop**: after `SBMIG_JOB_STOP_GRACE_S`, delete only a Job
  whose `sbmig-owner` / `sbmig-run-id` labels are ours
  (else `job_delete_refused`; both labels must match — changed in v2.1z-ad, see below), then wait until it is `Missing` / terminal
  (bounded by `SBMIG_JOB_API_GRACE_S` until v2.1z-ad; now without a deadline, and an API outage no longer fails the run or frees the slot —
  see below) → `stopped`. **Shutdown** leaves
  the Job and its on-disk state (`created` / `running` / `queued`) for the next Main. **`recover()`** at startup (k8s-job, local-job): every
  non-terminal `control/state.json` with a readable `request.json`, in `accepted_at` order — `queued` creates, `creating` probes then adopts
  or creates, `created` / `running` adopts and watches (v2.1z-ad: only a Job carrying this request's labels, see below);
  local-job marks them `lost` (`LOCAL_LOST`). `status()` adds `attempt`, `verified`, `control` and answers directory-only runs from the
  control state first. Journal events: `web_run`, `job_created` (+ `attempt`),
  `job_adopted`, `stop_request_sent`, `job_deleted`, `job_delete_refused`; Worker: `worker_started`, `worker_refused`, `worker_finished`.
- **`sbmig/web/localjob.py`**: the dev "pod" runs `python -m sbmig.web.worker` (env passthrough + `SBMIG_APP_DIR`; changed in v2.1z-ad, see
  below: only the manifest's per-run `contract.RUN_ENV` values), so the Worker path is exercised on a laptop too; its `job-status.json`
  fallback is written only when the Worker left none.
- **Manifests**: `81-web-job.template.yaml` `command: ["python", "-m", "sbmig.web.worker"]`, no `envFrom` (explicit `valueFrom`, secrets via
  `secretKeyRef`), no revision annotation; `30-secret.example.yaml` `immutable: true`; `SBMIG_WORKER_LLM_RECORDS` / `SBMIG_JOB_STATUS_GRACE_S`
  in `deploy/ENV.md`; README §3 "Main 이 지키는 것"; `docs/v2/JOB-RUN-STATE-CONTRACT-KO.md`.
- **`sbmig.web.app.app` is lazy (PEP 562 `__getattr__`)** — found by the kind end-to-end test: `sbmig serve` imports the module and builds its
  own app from `--out`; the import-time app (default `out/web`) ran the new k8s-job startup validation first and failed
  (`exactly one volume mount must contain the output directory /app/out/web`), so the pod crash-looped. Plain `uvicorn sbmig.web.app:app`
  still works (built on first access). `SBMIG_JOB_STATUS_GRACE_S` (90 s): after the Job ends Main waits that long for the Worker's
  `job-status.json` before calling the result unverified — NFS attribute caching shows new files late.

## deploy — the company config is baked into the image; container paths come from the manifests (2026-11-10)

User ("일단 굽자!"): the Dockerfile already copies `config/` into `/app/config`, so the `artifacts.yml` the migration team tested on the PC
is the image's file — one copy, no hand-kept twin in a ConfigMap.

- **Image**: `ARG REQUIRE_CONFIG=` (declared after the app `COPY`, so its value never breaks the apt / pip layers): every name in it must
  exist under `/app/config/`, else the build stops (`ERROR: config/<name> is missing from the build context (REQUIRE_CONFIG)…`). The
  company build passes `--build-arg REQUIRE_CONFIG=artifacts.yml` (the file is git-ignored — a fresh checkout would silently build an image
  without the company config, and `RunManager.default_configs()` only warns about a missing `SBMIG_WEB_CONFIGS` name). Empty = no check.
- **ConfigMap `sbmig-config`** carries `settings.xml` only (the `artifacts.yml` key and its mounts are commented alternatives in 20 / 40 /
  80 / 81). The baked file must hold no secret (an image is readable by whoever pulls it).
- **Machine paths** of a PC-tested yml are overridden by literal `env` lines in each manifest (env beats every yml):
  `SBMIG_BUILD__SETTINGS_FILE=/etc/sbmig/settings.xml`, `SBMIG_BUILD__JAVA_HOME=/opt/java21`, `SBMIG_BUILD__MVN=mvn`,
  `SBMIG_BUILD__LOCAL_REPO=null`, `SBMIG_SCRIPT__PYTHON=null` in 40 / 80 / 81; `SBMIG_KB__DIR=/data/kb-v2` and
  `SBMIG_LLM__RECORD_DIR=${LLM_RECORD_DIR:-}` in 40 only (the 81 Worker and the 80 `run-job.sh` write `kb.dir` / `llm.record_dir` into their
  own last `--config`). They are not in `sbmig-env`, because 80 reads that ConfigMap with `envFrom`.
- **`sbmig-env` → the 81 Job pod**: the template has no `envFrom`, so every key the ConfigMap documents (LLM gateway values, proxies, the
  `SBMIG_<SECTION>__<KEY>` tuning keys — active or commented) has one `configMapKeyRef {optional: true}` line in the template. A new key in
  the ConfigMap needs its line in 81.
- **`contract.is_secret_name(name)`** (new; `secret_env_names` and `k8s.validate_worker_template` use it): a config override
  `SBMIG_<A>__<B>` (double underscore) is never a secret name — no config key holds a secret value, and `…__MAX_OUTPUT_TOKENS` /
  `…__TOKEN_URL` would otherwise be refused as a literal `value` in the template and redacted out of the logs.
- **`worker.child_environ(environ)`** (new): without `SBMIG_WORKER_LLM_RECORDS=1` the Worker drops `LLM_RECORD_DIR` and
  `SBMIG_LLM__RECORD_DIR` from the child `sbmig run` (an env override would beat the `llm.record_dir: ""` of `worker-paths.yml`). Raw
  records in Job mode = the switch plus `SBMIG_LLM__RECORD_DIR` in the template (two commented lines there).

## v2.1z-ad — review of the Job dispatch code: four findings (2026-11-10)

A reviewer read the Job dispatch code (Main = `RunManager` in the web pod, Worker = `python -m sbmig.web.worker` in the Job pod) and
listed four findings, all judged real: (1) the next run could start before the previous Job had ended, (2) a Job without labels was
treated as ours, (3) a difference between the two pods' environments went unnoticed, (4) secrets were visible before the post-run
redaction. A local-job regression found while testing is fixed in the same round. Contract:

- **(1) Job end confirmation — the single slot is handed on only after the Job is confirmed ended.** The turn (`RunManager._turns`, one
  migration at a time) passes to the next run when `_run_job` returns. `_watch_job` now returns only once the Kubernetes API confirmed
  the Job terminal (`Succeeded` / `Failed`) or gone (`Missing`) — `_hold_until_ended(run, client, name, *, delete_after, why)` — with
  two exceptions: a server shutdown (`run.keep_state = True`; `control/state.json` stays as it is and the next Main adopts and watches
  the Job) and a Job that is not this request's (left alone and not waited for, see (2)).
  - *Result first, then the pod*: a matching `job-status.json` is still the result, but the run ends only after the pod ended too. A
    pod still present `SBMIG_JOB_EXIT_GRACE_S` (new, default 120 s) after its `job-status.json` appeared is deleted (our labels only;
    the Worker writes that file last, so nothing is lost) and waited for until it is gone. UI `node` update `ending` meanwhile.
  - *Stop*: after `SBMIG_JOB_STOP_GRACE_S` the delete is followed by a wait **without a deadline** (v2.1z-ac bounded it by
    `SBMIG_JOB_API_GRACE_S`); a delete request that fails is retried every `DELETE_RETRY_S` (15 s), a WARNING every `HOLD_WARN_S` (60 s)
    while the Job has not ended. A Job that vanished after a stop request ends `stopped`, not `lost`.
  - *A failure in the web process* (any exception in `_run_job`, not only `K8sError` / `OSError`) still fails the run (`failed`), but
    first deletes our Job and waits until it is gone.
  - **A Kubernetes API outage never fails a run and never frees the slot** (v2.1z-ac failed the run after `SBMIG_JOB_API_GRACE_S` and
    handed the slot on while the pod could still be running). `_poll_job` returns nothing while the API does not answer. Past
    `SBMIG_JOB_API_GRACE_S` (default 300 s — **its meaning changed: a notice only**) one loud notice: log ERROR, journal
    `job_api_unavailable` {job, seconds, error}, UI `node` update `api-unavailable`, `status()["job"]["note"]`; then a log reminder every
    `API_REMIND_S` (600 s). When the API answers again: journal `job_api_back` {job, seconds}, UI `api-back`. Every decision that needs
    the API's answer (the uncertain create, the restart probe / adopt) asks until it answers (`_state_when_answered`, no deadline); a
    shutdown meanwhile leaves the control state for the next Main.
  - *Uncertain create* (HTTP 409 / transport error): the lookup asks until the API answers and, while the Job is absent,
    `CREATE_CONFIRM_POLLS` (3) times in total one poll interval apart (a lost request may land late); present + ours → adopt, absent →
    the original error.
  - Control-state writes once the Job exists (`created`, `running`) go through `_transition_soft`: an `OSError` is logged, never raised
    (stopping the watch over a write error would hand the slot on with a live pod). `creating` is still written strictly before the
    create (not written → no Job).
  - Known limits: a create request that lands after the last confirm poll makes a Job nobody watches (it runs with its own labels next to
    the next run's Job); an API that never answers again holds the queue until Main's access is repaired — deliberate: two migrations
    overlapping on the shared KB and volume is worse than waiting.
- **(2) Ownership — both labels.** `RunManager._owned(run, st)`: a Job is this request's only when `sbmig-owner` equals the request's
  token (`control/state.json` `owner`) **and** `sbmig-run-id` equals the run id. `JobState.labels` is `dict | None`: `None` = the
  client cannot report labels (the local subprocess client, test fakes) — only then is the name trusted; `K8sClient.job_state` always
  answers a dict (`{}` for a Job without labels and for `Missing`), and `{}` is **not** ours (v2.1z-ac read an empty dict as "unknown" →
  ours). A same-name Job without our labels is neither adopted (create 409, restart probe, restart adopt — `_job_exists` now runs before
  an adopt too) nor deleted (stop → `job_delete_refused`; exit grace and failure cleanup → left alone, logged, not waited for); the
  create and restart cases fail the run with a message naming the Job.
- **(3) Environment pins — the Worker compares its environment with the Main that created its Job.** The configuration is not only
  files: the `${VAR}` placeholders of the yml files and the `SBMIG_<SECTION>__<KEY>` overrides take their values from the pod's
  environment. The web pod reads ConfigMap `sbmig-env` once at start (`envFrom`), a Job pod when it is created (one `configMapKeyRef` per
  key in `81-web-job.template.yaml`); a changed ConfigMap, a stale `sbmig-job-template` or a hand-edited manifest made the two pods run
  with different settings and nothing said so. The contract lives in `sbmig/web/contract.py`:
  - *What is pinned*: `env_pin_names(repo_root, configs, environ=None)` = the placeholders of `config/default.yml` + the run's selected
    configs (`config_env_names`; yml values only, via `sbmig.core.config.placeholder_names`), every `SBMIG_<SECTION>__<KEY>` override
    present in the environment and the placeholders inside its value, a whole-section override `SBMIG_<SECTION>` (one word that names a
    top-level config section — `_apply_env_overrides` replaces that section with its value; `SBMIG_WEB_MODE`, `SBMIG_OUT` … name no section
    and are not pinned), and an alias-only name (below). **Never pinned**: secret-looking
    names (`is_secret_name` — no fingerprint of a credential on the shared volume; a wrong credential fails loudly at the gateway or Nexus
    anyway), `ENV_PIN_SKIP` (`SBMIG_LLM__RECORD_DIR`, `LLM_RECORD_DIR` — may differ per pod), proxy variables (not sbmig configuration, no
    config reads them; they can carry userinfo), and whatever the configuration does not read (`SBMIG_WEB_*`, `SBMIG_JOB_*`, the per-run `RUN_ENV`). With the
    shipped `default.yml` that is `LLM_ASSET_ID`, `LLM_BASE_URL`, `LLM_ESCALATION_MODEL`, `LLM_GATEWAY`, `LLM_MODEL`, `LLM_RECIPE_MODEL`,
    `LLM_RESPONSE_PATH` plus the overrides the pod sets.
  - *Fingerprint*: `env_pins(repo_root, configs, environ=None)` = `{name: sha256(value)[:ENV_PIN_HASH_LEN]}` (16 hex, UTF-8) for the
    names that are **set** (set to `""` is a value; unset is absent). Names and hashes only — never a value.
    `compare_env_pins(pinned, actual)` → `{"differs", "main_only", "worker_only"}` (sorted name lists).
  - *KB-dir alias*: `ENV_PIN_ALIASES = {"SBMIG_KB__DIR": ("SBMIG_KB_DIR",)}` — the web pod sets the KB dir as the override
    `SBMIG_KB__DIR` (40), the Job pod as `SBMIG_KB_DIR` (81, written into `worker-paths.yml`); `env_pin_value` reads the alias when the
    name itself is unset, so both compare as `SBMIG_KB__DIR`. A template without its KB line shows up as `main_only` (the Worker would
    otherwise write the KB to the baked yml's path inside the pod and lose it with the pod).
  - *Where*: `control/state.json` (`ControlState`) gained `env_pins: {name: hash} | null` and `env_pin_policy: "refuse" | "warn" | "off" |
    null`, written by `RunManager._create_job` **with the `creating` transition** — by the Main that creates the Job, from its own
    environment at that moment, not at intake: a queued run carried across a Main restart is pinned by the new Main. A run adopted or
    probed after a restart (the Job exists) keeps the pins on disk; later transitions keep the fields. Not in `request.json`, because
    that file is the immutable intake record and the reference is the Main that renders the Job (same ConfigMap generation as the Job
    pod). `env_pins` is null when the policy is `off` or when computing the pins failed (a WARNING; the Job is created anyway — an
    unreadable config is caught by the config digests of `request.json`). One INFO line per Job: the number of pinned names and the
    policy (no values, no hashes). Older readers ignore the two fields (`ControlState.from_json` drops unknown keys).
  - *Policy*: env `SBMIG_JOB_ENV_PIN` on the **web pod** (`ENV_PIN_POLICY_ENV`), read once when `RunManager` starts
    (`contract.env_pin_policy(value, default)`, case-insensitive) → `RunManager.env_pin_policy`. Default `refuse` in `k8s-job`, `off` in
    `local-job` (the local Worker inherits Main's environment; a `.env` edit between accept and spawn would only raise false alarms). A
    non-empty unknown value → WARNING naming it, the default applies.
  - *Worker*: right after `verify_request` passed and before `worker_started`, the Worker reads `ControlState` (read-only; it never
    writes under `control/`). No state, `env_pins` null or policy `off` → no check (runs created by an older Main, local-job). Pins
    without a policy count as `refuse`. Checked: (a) the pins, recomputed from the Worker's own environment for `request.json`'s
    configs; (b) the per-run values against `request.json` — the config files `SBMIG_CONFIG` resolves to (`resolve_configs`) must be
    the same files, in the same order, as `<repo root>/config/<name>` of the request (`realpath` + `normcase`; the digests cover the
    request's names only, another `SBMIG_CONFIG` would run unverified files), and `SBMIG_STOP_AFTER` / `SBMIG_LLM_PROVIDER` (empty =
    unset) must equal the request's `stop_after` / `llm_provider`. The problem text names variables (and config file names) only, never
    a value or a hash, and passes the Worker's redactor. An exception inside the check is a problem under the same policy (its type
    only): `refuse` stays fail-closed, `warn` goes on.
  - *Outcomes*: `refuse` → the existing refusal path: journal `worker_refused` {run_id, reason}, `job-status.json` {exit_code: 3, error},
    exit 3 — Main records a verified exit 3 with the reason as the run's error. The reason lists the names per category, ends with a
    fixed hint (restart the web pod / re-create `sbmig-job-template`; `SBMIG_JOB_ENV_PIN=warn` or `off` on the web pod relaxes the check)
    and names no namespace or Deployment. `warn` → a `WARNING:` line in the pod log, journal `worker_env_differs` {run_id, policy,
    differs, main_only, worker_only, run_values} (names only), and the run goes on with the Worker's own values. No difference → silent.
    `worker_started` carries `env_pin: <policy>` whenever a check ran (absent = no check); when the check itself failed,
    `worker_env_differs` also has `check_error` (the exception's type name, never its message).
  - **Operator rule (replaces "the Job pod picks the new value up at the next upload")**: after changing ConfigMap `sbmig-env`, restart
    the web pod — else the next Job is refused within seconds, naming the keys. After changing the Job template, re-create ConfigMap
    `sbmig-job-template` and restart the web pod too (Main validates the template and keeps it from startup, v2.1z-ac).
  - **Shipped mode (2026-11-10)**: `deploy/k8s/20-configmap.yaml` carries `SBMIG_WEB_MODE: "k8s-job"` as an active key (it had been a
    commented line; the company's first web pod ran migrations itself). Applying the manifests therefore needs the web pod's
    Kubernetes access and ConfigMap `sbmig-job-template` before the Deployment — the startup validation stops the web pod otherwise.
    Commenting the line out is the in-process mode. The mode is a web-pod knob and stays outside the env pins. **Access (same day)**:
    the company's model is the shipped one — `40-deployment.yaml` has env `KUBECONFIG=/etc/kube/config`, the `kubeconfig` mount at
    `/etc/kube` and the Secret volume `agent-kubeconfig` (key `config`, mode 0400, readable through `fsGroup`) switched on, no
    `serviceAccountName`, and `sbmig-env` carries `SBMIG_K8S_NAMESPACE: "agent"` (with a kubeconfig the namespace is argument → that
    variable → the context's → `default`). `45-web-rbac.yaml` + the commented `serviceAccountName: sbmig-web` line are the alternative
    for a cluster without a kubeconfig Secret. The Job template (81) needs neither: only the web pod calls the Kubernetes API.
    **Retention (same day)**: the template's `ttlSecondsAfterFinished` is 3600 (was 86400). A finished Job is deleted by nobody — the pod
    exits and stays listed until Kubernetes' TTL controller removes Job and pod; with tens of runs a day an hour keeps the namespace
    listing short. The web pod deletes its own Job only after the stop grace, after the exit grace, or after a failure in the web process.
  - Known limits: secret-looking names are not compared — among them `LLM_TOKEN_URL` (its name contains TOKEN; a wrong URL fails at the
    token exchange) and `LLM_APP_TOKEN` / `LLM_API_KEY` / `NEXUS_*`; proxy variables (`HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`) are not
    compared; an override left empty (`SBMIG_X__Y=""`) is YAML null for `_apply_env_overrides` and replaces the yml value with None — it
    is pinned like any value (empty on both sides = equal), so leave an unused key commented out (`20-configmap.yaml` says so); only the
    names THIS run's configs read are compared.
- **(4) Write-time redaction.** Before: the child's console output reached the pod log as it was and the run dir was redacted only after
  the run, while the pod log and the web UI's log tab follow those files live. Now:
  - *Switch*: `SBMIG_REDACT_OUTPUT=1` (`scrub.REDACT_OUTPUT_ENV`). The entry points install the hook — the CLI callback
    (`sbmig.cli.install_redaction_if_requested()`, before logging is set up; when it cannot be installed: one stderr line and exit 3,
    never a run without it) and, under plain `uvicorn`, the web app's lifespan (`_ensure_console_logging`; a failure keeps the server
    from starting). Installed = `sbmig.web.redact.install_output_redaction()` (a live `Redactor(live_secret_values, refresh_s=1)`;
    `live_secret_values` = `contract.secret_values()` + the noted secrets) + `scrub.install_scrubbed_excepthooks()`.
  - *Core hook* `sbmig/core/scrub.py` (core never imports web): `set_output_scrubber`, `output_scrubbing()`, `scrub(text)`,
    `scrub_json(obj)` (value by value, keys included — the JSON stays valid), `note_secret(value)`, `refresh_secrets()`,
    `install_scrubbed_excepthooks()`. What passes through it: every formatted log line (`StageFormatter.format` / `formatMessage` — the
    run's `sbmig.log`, the console, the pod log; rich tracebacks are off under the switch so tracebacks pass the formatter),
    `Workspace.journal` and Main's own journal lines (`runs._append_journal`), Main's SSE events (`RunManager._push`), the error text of
    a run whose worker thread crashed, uncaught tracebacks (`sys.excepthook`, `threading.excepthook`).
  - *Fail closed*: a scrubber exception yields `scrub.WITHHELD` instead of the line; a journal line becomes
    `{"event": "output_withheld", "message": WITHHELD}` (`Workspace.journal` keeps its `ts` / `run_id`). Never the raw text.
  - *Derived secrets*: `redact.expand_values(values)` adds the spellings a value takes in output — stripped, JSON-escaped (ASCII and
    not), percent-encoded, `repr`-escaped, each line ≥ `MIN_LINE_LEN` (12) of a multi-line value; derived spellings shorter than
    `contract.MIN_SECRET_LEN` (6) are dropped. Secrets born during the run: the exchanged gateway token (`TokenManager` →
    `note_secret`); a `.env` read later (`load_dotenv_files` → `refresh_secrets`).
  - *Worker*: forces `SBMIG_REDACT_OUTPUT=1` on its child (`child_environ`; not optional under the Worker) and `COLUMNS=200` when unset
    (fewer wrapped console lines); redacts the child's stdout / stderr pipe line by line before the pod log (`Redactor.line` with
    `contract.secret_values(env)`; unchanged bytes pass as they are, a line that cannot be redacted → `WITHHELD`); its own echo and error
    texts pass the same redactor; the final `redact_run_dir` stays. `sbmig run` does its own final pass (`sbmig.cli.final_redaction`,
    a no-op without the switch).
  - *Web*: `LIVE_FILES = ("sbmig.log", "journal.jsonl")` (redacted as written) are served at any time; the other `SERVED_FILES` answer
    **HTTP 409** on `GET /api/runs/{run_id}/files/{name}` (`RunManager.file_withheld`) and are left out of `status()["files"]` until
    the final pass is over — for a Job run until it is finished, for an in-process run only while this process redacts output, for a
    run not in memory while its `control/state.json` is not terminal. The final pass is the Worker's (before `job-status.json`); Main
    does it itself for an in-process run under the switch and for a Job run that ended (terminal / `Missing`) without a verified
    result — never for a Job left running at shutdown or one that is not ours.
  - *Manifests*: `SBMIG_REDACT_OUTPUT=1` literal in `deploy/k8s/40-deployment.yaml` (web pod) and `80-build-job.template.yaml` (one-Job
    model); 81 needs none (the Worker forces it on its child).
  - Known limits: a secret split across wrapped console lines; a token the child obtains itself is unknown to the Worker's pipe redactor
    (the child's own hook covers its log lines); values shorter than 6 characters are never redacted as text (a short / numeric secret);
    Maven output under `logs/` is cleaned by the final pass only (it is not served); uvicorn's access log is not scrubbed; `run-job.sh`'s
    own shell output is not sbmig's; `LLM_TOKEN_URL`'s value is masked because its name matches the secret-name rule; under plain
    `uvicorn sbmig.web.app:app` the hook is installed at lifespan start, after `create_app` / `RunManager.__init__` logged (startup
    validation, recovery) — `sbmig serve` installs it first.
- **(5) local-job regression.** `LocalJobClient._env_of(manifest)` passes only the `contract.RUN_ENV` names with a literal value
  (`SBMIG_SOURCE_ZIP`, `SBMIG_OUT`, `SBMIG_RUN_ID`, `SBMIG_CONFIG`, `SBMIG_STOP_AFTER`, `SBMIG_LLM_PROVIDER`) on top of the PC's own
  environment, and `SBMIG_APP_DIR` is always this Main's repo root. Before, every `SBMIG_*` entry of the template reached the local
  Worker: since the container paths entered the template (2026-11-10) `SBMIG_KB_DIR=/data/kb-v2`, `SBMIG_BUILD__JAVA_HOME=/opt/java21`
  … ran a PC migration with pod paths, and the `configMapKeyRef` entries arrived as `""` (an empty `SBMIG_<A>__<B>` overrides the yml
  value with None).
- **Deliberately not changed**: `_apply_env_overrides` still reads an empty value as YAML null (documented, not "fixed"); the secret-name
  rule has no `_URL` exemption (`LLM_TOKEN_URL` stays masked and unpinned); the uvicorn access log is not scrubbed.

Operator-visible names of this round:

| Name | Where | Meaning |
|---|---|---|
| `SBMIG_JOB_EXIT_GRACE_S` (new, 120) | web pod env | seconds a pod may linger after its `job-status.json` before Main deletes the Job |
| `SBMIG_JOB_API_GRACE_S` (300) | web pod env | changed: seconds of API silence before one loud notice — no longer fails the run |
| `SBMIG_JOB_ENV_PIN` (new) | web pod env | `refuse` / `warn` / `off`; default `refuse` in k8s-job, `off` in local-job |
| `SBMIG_REDACT_OUTPUT` (new) | env of any sbmig process | `1` = log / journal lines redacted as written; 40 / 80 set it, the Worker forces it on its child |
| `env_pins`, `env_pin_policy` (new) | `control/state.json` | the creating Main's fingerprints (name → 16 hex) and policy; `env_pins: null` = no check |
| `job_api_unavailable`, `job_api_back` (new) | journal (Main) | the API was silent past the grace / answers again |
| `job_deleted` (+ `reason`) | journal (Main) | also written after the exit grace and after a failure in the web process |
| `worker_env_differs` (new) | journal (Worker) | `warn`: the names that differ; the run goes on |
| `worker_refused` | journal (Worker) | also for `refuse`: the reason names the differing keys, exit 3 |
| `output_withheld` (new) | journal | a journal line that could not be redacted |
| `ending`, `api-unavailable`, `api-back` (new) | UI `node` updates (`node: job`) | waiting for the pod to end / API silent / API back — the run keeps its turn |
| `completed` (new, 2026-11-10); `failed` narrowed | UI `node` updates (`node: job`) | The pod step means "did the pod do its work", not the migration's exit code. The Worker passes sbmig's exit 3 (ADF reject, intake refusal) on as the container exit code, so such a Job ends `Failed` in Kubernetes; that raw phase is no longer pushed. `completed` {job, pod, exit_code, phase?} = this run / attempt's `job-status.json` accepted without `error`, pushed once per watch in either order (status first, or `Failed` first and the file late on NFS). `failed` only when the Job ended `Failed` with no verified status within `SBMIG_JOB_STATUS_GRACE_S` (the pod died; {message} = Kubernetes') or the verified status carries `error` (the Worker refused the request or could not start sbmig; {exit_code, message}). `status()["job"]["phase"]` keeps the raw phase; log line `Failed is the Worker's exit code N (job-status.json verified) — not a pod failure`. Page: `completed` ticks the pod step, any engine node event ticks it too unless it failed, the abort reason (`중단: …`) stays on the rail note |
| `job.note` (new) | `GET /api/runs/{run_id}` | why the run still holds its turn (API silent) |
| HTTP 409 (new) | `GET /api/runs/{run_id}/files/{name}` | that file waits for the end-of-run redaction pass |

## v2.1z-ae — up to N build Jobs at a time; the rest wait with a notice (2026-11-10)

The user: "초반엔 무서우니까 3명까지는 빌드파드가 돌아가는 구조 … 나머지는 대기하라고 알림" and "3명이 동시에 시작하면 … 코드 쓰기나 케이스 쓰기
파일 락 생기지 않을까?". Until now one migration ran at a time (`RunManager._turns`, a FIFO of one). Contract:

- **`SBMIG_JOB_MAX_PARALLEL`** (web pod, read once at startup; code default **1**; shipped **`"3"`** in ConfigMap `sbmig-env`). An
  integer ≥ 1; anything else → 1 with a WARNING. It counts only in `k8s-job` / `local-job`: an `in-process` server always runs one
  migration at a time (the LLM call counter and the log filter are process-wide) and logs a WARNING when more was asked.
  `RunManager(…, max_parallel: int | None = None)` (the argument wins over the environment; tests use it) → `RunManager.max_parallel`
  = the effective number. The key is the web pod's alone: no line in `81-web-job.template.yaml`, not part of the environment pins.
  **Operator rule unchanged: restart the web pod after changing `sbmig-env`.**
- **Slots** (`runs._FifoTurns(slots=1)`): a waiting line plus the runs that hold a slot. `turn(key)` (context manager) waits until
  `key` is the **head of the line** and fewer than `slots` runs hold one (strict FIFO, nobody overtakes), then **yields the slot
  number**; slot numbers are the lowest free ones from 1, unique among the runs holding one. `occupy(key, prefer=None) -> int` takes a
  slot **at once, whatever the limit** — restart recovery only: a Job that exists (or may exist) in the cluster already runs and must
  be counted; `prefer` (the recorded slot) is kept when free. `turn()` on a key that already holds a slot yields it without waiting.
  `leave(key)` drops the key from both and wakes the waiters. `position(key)` = 1-based place among the waiting (None when not
  waiting), `slot_of(key)`, `counts() -> (running, waiting)`. A limit lowered while Jobs run: more runs than `slots` may hold one after
  a restart; the waiting ones wait until the count is below the limit. With `slots == 1` the behaviour is the old one.
- **A run's slot**: `Run.slot` (memory), **`ControlState.slot: int | None`** (written with the `creating` transition, beside
  `env_pins`; a `state.json` without it reads as `None` = a Job created before this section = slot 1's repository),
  **`SBMIG_JOB_SLOT`** in the Job's env (`RunManager._job_env`; the name joins `contract.RUN_ENV`, so the local Worker gets it too; a
  Job template needs no line for it, `render_job` appends it). Recovery (`_resume`): `creating` / `created` / `running` → `occupy(run,
  prefer=cs.slot or 1)` synchronously in `recover()` (before any upload can take the number), `queued` → the line, in `accepted_at`
  order as before. Everything of v2.1z-ad (1) holds per slot: a slot is handed on only after its Job is confirmed ended, a Kubernetes
  API outage frees no slot.
- **One Maven local repository per slot** (the Worker; Maven's default setup does not promise that several processes can write one
  local repository, and the three pods share PVC `sbmig-m2`). Slot 1, or no `SBMIG_JOB_SLOT`: nothing changes — today's repository,
  byte-identical command lines. Slot k ≥ 2: a sibling directory `<base>.parent / f"{base.name}-slot{k}"` (`…/.m2/repository-slot2`),
  where `<base>` is the repository the run would use otherwise: the Worker's own `SBMIG_BUILD__LOCAL_REPO` when it is a non-empty
  string after `yaml.safe_load`, else `sbmig.build.mvn.local_repo_path(load_config(<the run's configs>, <worker-paths.yml>))`
  (`build.local_repo` → settings.xml `<localRepository>` → `~/.m2/repository`). The Worker creates the directory and sets
  `SBMIG_BUILD__LOCAL_REPO=<that path>` **in the child's environment only**, after its environment-pin check (the pins compare the
  pod's own environment, where 81 keeps the literal `"null"`). It never fails a run over this: when the base cannot be resolved or the
  directory cannot be created, the run goes on with the shared repository and says so (stdout line + journal). Journal event
  **`worker_slot`** `{slot, local_repo, shared: bool, note?}` (only when `SBMIG_JOB_SLOT` is set). A slot's first runs download
  everything once; the repositories persist on the PVC.
- **Status** (`RunManager.status`): every answer carries `parallel: {slots, running, waiting}`; a run waiting in this process's line
  carries `queue: {position, ahead}` (`position` 1 = next, `ahead = position - 1`); a Job run that holds or held a slot carries `slot`
  (memory, else `ControlState.slot`). `GET /api/runs` → `{runs: [...], parallel: {...}}` (each listed run as before, plus `queue` /
  `slot` when known); `GET /api/configs` adds `max_parallel`. **SSE**: `events()` sent `status` only when a stream opened and when the
  run ended; it now sends `status` again whenever the run's `(state, queue position)` changes — a waiting page sees itself move up
  and start without a reload.
- **The page** (`sbmig/web/static/index.html`, the operator-visible texts): after an upload that waits, the toast says how many are
  being migrated and how many are ahead; the title of a waiting run is `대기 중 — 다음 차례입니다` or `대기 중 — 앞에 N건이 기다립니다`;
  the note under the stage rail says `동시에 N건까지 이관합니다. …` (or the old one-at-a-time sentence when `slots == 1`); the upload
  card shows a notice while every slot is taken (`parallel.running >= parallel.slots`, from `/api/runs`, polled every 15 s): how many
  are running, the limit, and the waiting number a new upload would get; the run list labels a waiting run `대기 N번`.
- **Knowledge base — what three pods writing at once rely on, and what this round hardens.** Unchanged: every mutation of the KB
  takes the one cross-process lock (`<kb>/.lock`, `sbmig/kb/lock.py`), re-reads the file inside the lock and writes by temp file +
  `os.replace`; a script's version number is chosen inside the lock from `case.json` and the `fix_v*.py` on disk; reads take no lock.
  Hardened: (a) **a new case met by two pods** — an NFS client may still answer "no such file" from its cache for a `case.json`
  another pod just created; inside the lock, a missing case is confirmed by exclusive creates, which the server decides, not the
  client's cache (`CaseStore._confirm_new`): `mkdir` of the case folder (made = really new); when the folder exists, an `O_EXCL`
  create of `case.json` (removed at once when it succeeds); when the file is there it is read again (up to `CONFIRM_SECONDS` 1 s)
  and that case is used — no case file is overwritten blind. A folder holding only per-error files is a new case quietly; a folder
  a writer left without `case.json` gets a fresh one with a WARNING and its scripts keep their numbers; (b) **staleness on one
  clock** — a lock looked stale when `time.time()` (the pod) minus the file's mtime (the NFS server) passed `kb.lock_timeout_seconds`;
  with skewed clocks every lock would look stale. A lock's age is now measured on the file system's clock: a waiter writes a probe
  file beside the lock (`.lock.probe.<pid>.<random>`, removed at once), reads its mtime and keeps the offset to its own clock per KB
  directory for 60 s (only a waiter probes: an uncontended acquire costs nothing extra; an offset over 2 s is said once at INFO);
  when the probe fails the old comparison stays. A same-host lock of a dead pid is stale without an age, as before. Two edges closed
  on the way: a lock that turns stale within 2 s of the waiter's deadline (the lock of a pod killed inside its section, seen from
  its first moment) is waited for instead of "going on without it", and `_break` reads the token again right before it moves the
  lock aside (a lock taken after the judgement is left alone); (c) **LLM records** —
  `_Recorder` chose `llm-NNNN.json` by `exists()` and then wrote; two pods sharing one record directory could pick one number. The
  file is now created exclusively (`open(…, "x")`), the next number on `FileExistsError`; (d) **script files** — `fix_v<N>.py` is
  claimed by a hard link of a temp file in the case folder (`CaseStore._claim_script`: atomic, fails when the name exists), so a
  number taken by a writer that went on without the lock is never replaced (WARNING, the next number); where the file system has
  no hard links the atomic replace stays (the lock picked the number). Not changed: two runs that meet the same
  new case in the same minute both ask the LLM and store `fix_v1.py` / `fix_v2.py`; a pod killed inside a locked section makes the
  others wait once, about `kb.lock_timeout_seconds` (300 s, + ≤ 2.25 s at the edge); a pod frozen (not dead) longer than that has its
  lock broken and may overwrite a `case.json` written meanwhile when it wakes (the limit of a lease lock); `sbmig kb import --replace`
  swaps the KB directory, lock file included, and must not run while Jobs run; `errors.jsonl` is appended in place under the lock (a
  reader without the lock may see a partial last line, which the parser skips). Relied on, not testable here: the NFS mount keeps
  close-to-open consistency (the default, no `nocto`), `mkdir` / `O_EXCL` / `link` / `rename` are decided atomically by the server,
  pod hostnames are unique.
- **Not changed**: a stop on a waiting run still takes effect when its turn comes (no Job is created); the Job template; the env pins;
  `in-process` mode; `sbmig fleet` (sequential).
- **As built** (what the code added to the text above). Web: `create_app(…, max_parallel=None)` passes the value on (tests; the
  server reads the variable); the rows of `GET /api/runs` do not repeat `parallel` (the route adds it once, beside `runs`); the Job
  environment always carries `SBMIG_JOB_SLOT` (1 with the default limit); the Kubernetes client is built once under the manager's lock
  and uses one `requests.Session` per thread (three watcher threads share the client); after a web-pod restart the runs that held a
  slot (`creating` / `created` / `running`) are resumed before the queued ones, each group in `accepted_at` order; on shutdown a
  waiting thread stays blocked until the running ones leave. Worker: a blank `SBMIG_JOB_SLOT` is "not set" (no line, no journal); a
  value that is not an integer ≥ 1 gives `worker_slot {slot: null, local_repo: null, shared: true, note}` + a WARNING and the shared
  repository; a relative base is resolved against the child's working directory; a `${VAR}` placeholder in the Worker's own
  `SBMIG_BUILD__LOCAL_REPO` is left to the config resolver; a resolver failure is reported by exception type only (no value in the
  note). Page: the upload toast leaves out the "지금 N건이 이관 중이라" clause when nothing is running; the notice on the upload card is
  an empty `#busynote` paragraph when a slot is free (hidden by `:empty`); the place in line is taken at upload on the server, so
  closing the page does not lose it.

## v2.1z-af — review of the parallel round: what three pods at a time broke, fixed (2026-11-10)

The user, after v2.1z-ae: "서브에이전트 돌리면서 코드 검토 동시성 이슈 될만한거나 로직에서 문제 생길법한거 찾아봐줘!" Five independent
read-only reviews (the slot scheduler, the Worker + Kubernetes client, the KB lock + case store, every other shared location of
a run, the page + SSE), each finding reproduced before it was fixed. Where this section contradicts v2.1z-ae, this section counts.

- **Slots (Main).** Invariant: two live pods never use one slot number, and the number a run holds in memory is the one its pod was
  started with. (1) A Job run's terminal `control/state.json` is written **while its slot is still held** (`_close_job_run` inside
  the turn); a failed write is logged and the slot is still freed. Before, the slot was freed first: a web-pod death in between
  left two non-terminal states with one slot. (2) Recovery honours the recorded slot **even when another recovered run holds it**
  (`_FifoTurns.occupy`; WARNING `recovery: run A and run B both recorded slot N — both keep it …`); a new run only gets a number no
  run holds; `parallel.running` counts runs. This replaces v2.1z-ae's "numbers unique among the holders". (3) A poll of the
  Kubernetes API that fails in ANY way is "API unavailable" (never a failed run, never a freed slot): `K8sClient` turns an
  unreadable / oddly shaped body into `K8sError`, `_poll_job` and the hold loop catch every exception; a 2xx create with an
  unreadable body counts as created (WARNING). (4) A Job is ENDED only when its phase is terminal AND no pod of it is still running
  or terminating (`JobState.active_pods`: pods not Succeeded / Failed, `status.active`, `status.terminating`); a `Failed` Job whose
  pod is inside its termination grace is held (`ending`, INFO `job … is Failed but N pod(s) of it are still running or terminating`),
  bounded by `SBMIG_JOB_EXIT_GRACE_S` and then the usual delete-and-wait. No new RBAC verb. (5) **A stop on a WAITING Job run takes
  effect at once** (`_FifoTurns.withdraw`): it leaves the line, ends `stopped` (exit 3, error `stopped before the job was created`,
  no Job), and the runs behind it move up. This replaces v2.1z-ae's "takes effect when its turn comes" for the Job modes;
  `in-process` is unchanged. (6) Shutdown no longer waits for a just-ended Job's result file (`keep_state`: the next Main reads it).
- **Worker.** (1) **SIGTERM / SIGINT**: the Worker is PID 1 and ignored SIGTERM, so a deleted pod or a Job past its deadline kept
  Maven running until SIGKILL. Now it stops the whole process tree of its child (the signal to every descendant and its process
  group, 5 s, then SIGKILL, 3 s — Maven runs in its own session, so the tree is found from the process table), redacts, and writes
  `job-status.json {exit_code: 3, error: "the Worker was terminated by SIGTERM (pod deleted, Job deadline or stop) before sbmig
  finished — …"}` and journal `worker_finished {…, terminated: "SIGTERM"}`; a signal before the child starts means the child never
  starts; one after it ended is only noted. (2) **Slot repository fallback**: when `<base>-slot<k>` cannot be created or written
  (write-tested with a probe file), or the base cannot be resolved, slot k ≥ 2 uses a **pod-local** repository
  `<tempdir>/sbmig-m2-slot<k>` (`worker_slot {…, shared: false, pod_local: true, note}` + a WARNING that names the fix: ownership /
  `fsGroup` of the folder on PVC `sbmig-m2`); the shared repository only when that fails too. This replaces v2.1z-ae's "the run goes
  on with the shared repository".
- **Knowledge base.** (1) `_confirm_new` never writes a fresh `case.json` over one the server says exists: the case folder is listed
  and the file opened directly (no `is_file()` first) for up to `CONFIRM_SECONDS` **75** (longer than an NFS attribute cache can
  answer "no such file"), the lock kept alive meanwhile; a file that opens but stays empty / does not parse for 1 s is repaired as
  before; "exists on the server, never readable here" raises `CaseUnreadable(OSError)` and nothing is written — `add_script` then
  returns None through the loop's wrapper (the script runs unstored, the migration goes on), `record_run` / `recount` / `merge_from`
  skip that case with a WARNING (`kb import` counter `cases_skipped`). An empty `case.json` reads as "not there yet" (DEBUG).
  (2) **Breaking a stale lock is serialized** by `<kb>/.lock.breaker` (`O_EXCL`): only its holder re-reads, judges again and moves
  the lock aside, verifies what it moved (dev, inode, mtime, token) and puts a wrongly moved lock back by an exclusive link, never
  a replace. Before, three contenders around a stale lock could end with two holders. A breaker of another host expires after 30 s
  on the file system's clock, one of this host only when its pid is dead. Windows that remain are listed in the module docstring.
  (3) A stale lock that cannot be moved (EACCES / EPERM) is waited for like a live one (it spun at 100 % CPU forever). (4)
  `_claim_script` falls back from the hard link only on "hard links unsupported" errors, and the fallback is an exclusive create;
  EIO / ESTALE / EACCES / ENOSPC propagate (they replaced an existing `fix_v<N>.py`). (5) `_release` removes the lock only when it
  reads our token. (6) **Heartbeat**: `kb.lock.keep_alive(root)` refreshes the lock's mtime (at most every 10 s, only while the
  token is ours) from the long sections (`recount`, `record_run`, `merge_from`, `export_zip`, the wait in (1)); a
  `kb.lock_timeout_seconds` ≤ 0 means the default 300 with one WARNING (it made every lock instantly stale). (7) A clock probe that
  itself took more than 1 s is discarded. (8) An LLM record that cannot be written is a WARNING, never a lost answer. (9) A failed
  `kb import --replace` swap restores the previous KB and says where everything is (`<kb>.intruder-<stamp>` holds what a pod
  created meanwhile); the rule "not while Jobs run" stays.
- **LLM gateway, shared by the pods** (`sbmig/fix/company.py`; what the gateway really does is the company's to confirm).
  **`llm.busy_wait_seconds`** (600; 0 = the old behaviour): a 429, or a 502 / 503 / 504 carrying `Retry-After`, means busy — that
  call is re-sent with capped backoff + jitter (2 … 60 s; `Retry-After` honoured up to 120 s) until that many seconds were slept,
  apart from `max_retries`; INFO `llm gateway busy (HTTP N): waiting for a free slot …`, `llm gateway still busy: waited …` about
  once a minute, then the same `LLMError` as before (the fix loop's cool-down / `llm_unavailable` rules are unchanged; before, a
  third pod exhausted its retries in 7 s and its whole run ended "LLM unavailable"). The waiting call counts as in flight for the
  watchdog. **`llm.auth_retries`** (2): a 401 right after a fresh token exchange → 1–5 s pause, another exchange, up to that many
  extra rounds; WARNING once per process `LLM gateway rejected a token right after it was exchanged — another client may be
  exchanging the same app token (parallel build pods?)`. A rejected token is dropped only if it is still the one held. The other
  retry sleeps carry jitter. Not covered: the token endpoint itself answering 429.
- **The page.** After `end` with a `reason` (web-pod restart) or a dropped stream the card says `서버가 다시 시작되는 중입니다` /
  `서버와 연결이 끊겼습니다`, and re-attaches by itself after 2, 4, 8 … 30 s (probe `GET /api/runs/<id>`; three 404s → `서버가 이 실행을
  더 이상 알지 못합니다`); it stayed "진행 중" forever. A stop is bound to the run it was clicked on, and `{"stopped": false}` is said
  as such (`이미 끝난 이관입니다. 중단할 것이 없습니다.`). `/api/runs` answers are applied in request order and calls within 300 ms are
  one request.
- **Reviewed, nothing to fix**: every Maven invocation and every direct reader of the local repository follows the slot repository;
  no other shared file with a fixed name outside the run directory; process-level caches are per pod; XSS / path traversal on the
  page and routes.
- **Known, not changed**: Main and the Worker both append to `journal.jsonl` from different hosts (NFS appends are not atomic
  across clients; lines may interleave); a fresh slot repository has no parent poms yet, so parent-chain facts come from the remote
  lookup only on a slot's first runs; a browser on HTTP/1.1 allows six connections per host, each open run tab holds one; a signal
  that arrives after the child wrote a successful report still shows exit 3 with `success` from the report.

## remediations (취약점 조치) — `/api/remediations`, IVAS, version-text fixes + approved managed overrides (2026-11-11)

A second, independent flow beside the migration: an uploaded Maven project ZIP is built as it is (baseline), scanned by
**IVAS** (the company's vulnerability scanner), the vulnerable libraries whose version is written in the project's own poms
get a new **version text** on an isolated candidate copy (transitive / parent- / BOM-managed ones get one approved
`<dependencyManagement>` override entry in the root pom instead — the single exception, below), the candidate is built and
verified, the frozen delivery ZIP is rescanned, and a report + `result.zip` come out. Package `sbmig/remediations/` (package map in its `__init__.py`); the
contract types are `sbmig/remediations/models.py` and `sbmig/remediations/interfaces.py`; settings `cfg.remediation.*`
(`RemediationConfig` / `RemediationPolicyConfig` in `sbmig/core/config.py`, section `remediation:` of `config/default.yml`).
It never touches the migration KB; since the later decision of the same day it is **chained after a migration in the build pod**
(below), the migration graph calling it through one node.

### Execution model — the build pod, chained after BUILD SUCCESS (user, 2026-11-11, later the same day)

- **Where**: in the BUILD POD, not the web pod. When a migration ended BUILD SUCCESS and `remediation.after_migration` is true, the
  migration LangGraph's `remediation` node (after `finalize`) calls the separate remediation graph (`run_after_migration` in
  sbmig/remediations/chain.py) inside the same `sbmig run` process. Code default false; the k8s manifests ship it **true** in
  ConfigMap `sbmig-env` (group 3), so the web pod (40, `envFrom`), the Job pod (81, one `configMapKeyRef` line) and the 80 model
  (`envFrom`) carry the same value — it is a config override and therefore env-pinned (`SBMIG_JOB_ENV_PIN`): an override on one
  side only is refused.
- **Files**: `<migration run dir>/remediation/` (`RunPaths` below, rooted there); the remediation id = the migration run id. The chain
  zips the migrated project tree itself and feeds it through `intake.accept_zip`.
- **Verdict**: the remediation's outcome never changes the migration's success, exit code or report verdict; no exception escapes the
  node. `SBMIG-RESULT … remediation=<STATUS|skipped>`. Skipped (journal `remediation_skipped`, names only) when disabled or not
  ready: readiness on this path = the three IVAS values + Maven readiness; `remediation.trusted_projects` is **not** required here
  (the migration already built the project) — it gates the standalone upload only.
- **Waiting states**: a build pod cannot wait for a human. `AWAITING_APPROVAL` / `AWAITING_CONFIRMATION` (and `STOPPED`) are saved
  and the pod ends.
- **Resume in a NEW build pod** (Wave 4): `POST …/resume` (or `POST …/approvals` with `"resume": true`) on a chained remediation in a
  Job mode dispatches a new Job from the same template into the same run dir, Worker action `remediation-resume`, which calls
  `chain.resume(run_dir, cfg, …)`. It takes one of the `SBMIG_JOB_MAX_PARALLEL` slots and waits in the same FIFO line as uploads.
  Everything it needs is on the shared volume: `state.json` carries `pending_receipt` (the IVAS `projectId` / `analysisId`), so a
  scan that was in flight is polled again and nothing is uploaded twice. For `AWAITING_CONFIRMATION` the web pod applies the
  request's `confirm` / `label` / `project_id` / `analysis_id` to `state.json` **before** the Job is created.
- **Time budget** (`remediation.chain_budget_minutes`, default 90; 0 = none): `run_after_migration` and `resume` wrap the `stop`
  check so it also turns true once this invocation has run that long in this pod (measured again by every resume). The graph stops
  at the next boundary as `STOPPED` with reason **`chain_budget`** (`chain.R_CHAIN_BUDGET`; a real stop request keeps
  `stop_requested`), journal `remediation_budget` once; resumable like any stop. A scan in flight keeps its receipt, so the resume
  continues the same IVAS analysis.
- **Web pod**: status, proposals, approvals, files only (`/api/remediations/<migration run id>/…`). The standalone
  `POST /api/remediations` runs only in `in-process` mode (the web pod then runs it as before, under `<web out dir>/_remediations/<id>/`);
  in `k8s-job` / `local-job` it answers 409 `not_available`.
- **Environment in the Job pod**: `IVAS_BASE_URL` / `IVAS_USER_ID` / `IVAS_PASSWORD` come from Secret `sbmig-llm` — 81 by three
  `secretKeyRef` lines (`optional: true`: a cluster without IVAS still migrates), 80 by `envFrom`. The Worker passes them to its
  child unchanged (`sbmig.web.worker.child_environ` drops only the record-dir variables). `IVAS_USER_ID` is a secret name although
  it holds no secret word (`sbmig.web.contract.SECRET_NAMES`, redacted from 4 characters on — `MIN_NAMED_SECRET_LEN`); the IVAS
  token is registered through `scrub.note_secret` in the child. No `IVAS_*` value is ever env-pinned (two are secret names;
  `IVAS_BASE_URL` is neither an `SBMIG_` override nor a `${VAR}` placeholder of a config file). The Worker's final redaction pass
  (`sbmig.web.redact.redact_run_dir`) also walks `remediation/` (text suffixes + `.patch`; atomic replace) and skips its project trees
  (`accepted/`, `rounds/<n>/candidate/`), `original/` (the read-only `source.zip` + the hash-bound `manifest.json`), every ZIP and
  every symlink. A stop signal during the chained remediation (the Job deadline) still ends the Worker with exit 3; the error text then
  carries the migration's `SBMIG-RESULT` line (the migration report is in the run dir).
- **Maven**: on this path the local repository is the pod's slot repository (slot 1 `~/.m2/repository`, slot k `repository-slot<k>`);
  the `-remediation` suffix applies to the in-process standalone path only.
- **Time**: the Job's `activeDeadlineSeconds` stays 14400. A chained remediation adds a baseline build, an IVAS upload + analysis
  (≤ `remediation.ivas_analysis_timeout_minutes`, 30) and per round a candidate build + rescan; typical 20–75 min, worst case of the
  configured caps ≈ 8 h (60-min builds, 30-min uploads and analyses, 3 rounds) — beyond any deadline, like the migration's own
  240-min cap; `remediation.chain_budget_minutes` (90) bounds the chained part per pod (above). `deploy/README.md` §6.
- **IVAS logins**: several logins / tokens at once work (the user tested three) — one login per pod is fine with three build pods.

### Hard rules (company, all bind)

- Only **approved Maven version text** is changed. Application sources, the project structure and every non-pom file are
  never modified; the delivery ZIP has exactly the original's file set, only version text inside existing `pom.xml` files differs.
  A `replace` edit (`TextEdit.op="replace"`, the default) is one line of one pom, `old` → `new`, both plain version strings (no
  tag, no `${…}`); bytes outside the replaced span (encoding, BOM, CRLF, final newline) are preserved.
- **The one exception** (user decision 2026-11-11): a `managed_override` proposal inserts ONE `<dependency>` entry
  (groupId / artifactId / version only) into the **root** `pom.xml`'s top-level `<dependencyManagement><dependencies>` — or a
  whole `<dependencyManagement>` section holding that one entry when the root pom has none (placed before the top-level
  `<dependencies>`, else `<build>`, else `</project>`). It is a `TextEdit` with `op="insert"`, `old=""`, `line` (insert before),
  `anchor` (that line's current stripped text, re-checked when applied) and `override` (`{groupId, artifactId, version}`);
  `new` must equal exactly what `apply.render_override` produces from `override` (indentation copied from sibling entries, the
  file's line ending) — no other XML can be inserted. Such a proposal **always waits for approval**, whatever
  `require_approval` and `always_approve` say. `verify_text_only` refuses a candidate with any other difference: untouched files
  byte-identical, edited poms equal to the original with exactly the planned replaces / inserts applied (inserts before the same
  line in proposal-id order; two whole sections at one place become one section), the XML structure grown by exactly the
  planned override elements.
- The original ZIP (`original/source.zip`, never rewritten), the original-file manifest (`original/manifest.json`: path, size,
  sha256 per file) and the working tree `accepted/` are kept apart. Changes go to `rounds/<n>/candidate/` first; only a candidate
  that built and whose resolved dependencies + packaged jars match the plan is committed into `accepted/` (copy, then two renames;
  a crash between them is recovered at the next start). A candidate whose build failed may still be delivered (as a failed
  candidate, `remediation.failed_candidate: include`, below) but is never committed into `accepted/`. Build output (`target/`) is never hashed, copied or delivered: every
  tree hash / copy / delivery ZIP is restricted to the manifest's path list.
- A failed baseline build ends the run (`FAILED baseline_build_failed`); it never proceeds to the scan or a plan.
- IVAS: an **uncertain upload is never re-sent automatically**. An incomplete scan, a missing page, a changed `totalElements`,
  a duplicate result, a summary that disagrees with the pages, an unsupported category or a ZIP hash mismatch are never read as
  "0 findings": `ScanResult.complete=False` and neither planning nor success uses it.
- Findings are linked to what Maven **actually resolved** (`dependency:tree` per module) and to the exact pom line that sets the
  version. Vendored files, build plugins and anything unmappable stay residual / manual review.
- A target version comes only from the server policy (`remediation.policy.pins`) or a version the approved repository lists
  (evidence `policy:pins` / `repository:<host>` / `repository:local` on the proposal; never a URL with credentials). IVAS's
  patched version is a hint (a floor), never evidence; without that hint and without a policy pin nothing is proposed (`no_candidate_version`). A parent / BOM of unknown
  provenance is never changed.
- The delivery ZIP is frozen (deterministic entry order, fixed timestamps) and **the same bytes** (same sha256) are uploaded for
  the final scan; the receipt's sha256 is computed over the bytes actually sent.
- Stop takes effect at the next workflow boundary; a running remote IVAS analysis is never reported as cancelled (sbmig only
  stops polling; resume continues polling the same analysis).
- A server restart never re-sends or resumes anything: jobs left QUEUED / RUNNING by an earlier process become `INTERRUPTED`;
  only an explicit resume continues them.
- No secret (IVAS user / password / token, Nexus credentials) is written to code, config, state, journal, log, report or an
  exception message; the IVAS token lives in memory only, response bodies quoted in errors are scrubbed and cut to 300 chars.

### Run directory (`RunPaths`)

Chained (build pod): `<migration run dir>/remediation/`. Standalone (in-process mode only): `<web out dir>/_remediations/<id>/`
(the leading `_` keeps it out of the migration run list):

| Path | What |
|---|---|
| `original/source.zip`, `original/manifest.json` | the upload as received; the manifest (paths, sizes, sha256) that defines the file set |
| `accepted/` | working tree: the original plus committed (built + verified) version edits only |
| `baseline/build.log` | baseline `package` build (run on a throw-away copy, so `accepted/` never gets `target/`) |
| `rounds/<n>/` | `candidate/` (isolated copy), `before.json` / `inventory.json`, `plan.json` (+ `plan.stale.json` when a plan was invalidated), `apply.json`, `candidate.patch` (accepted → candidate), `build.log`, `resolved.json` |
| `scans/<label>.json` | one `ScanResult` per scan; labels `baseline`, `final-r<n>` |
| `delivery/result.zip`, `delivery/delivery.json` | the frozen delivery (`DeliveryInfo`: sha256, size, files, tree hash, round, `build_ok` — false for a failed candidate, `scanned` — the final scan was complete and of these same bytes) |
| `delivery/result-r<k>-verified.zip` (+ `.json`) | only with `failed_candidate: include`: round k's built + rescanned delivery, moved aside when a later failed candidate took `result.zip` |
| `state.json` | `RemediationState`, replaced atomically at every node boundary |
| `approvals.jsonl` | append-only decision log |
| `journal.jsonl`, `remediation.log`, `report.md`, `report.json` | events, log, Korean operator report, machine report (terminal states only) |

### Statuses and reason codes (exact strings; `models.py`)

| Status | Meaning | Next |
|---|---|---|
| `QUEUED` / `RUNNING` | accepted, waiting for / held by the worker thread | stop → `STOPPED` at the next boundary |
| `AWAITING_APPROVAL` | proposals of the current round are `pending` | `GET …/proposals` → `POST …/approvals` → `POST …/resume` |
| `AWAITING_CONFIRMATION` | an IVAS upload was sent but its outcome is unknown (`upload_uncertain`) | operator checks IVAS → `resume` with `confirm` |
| `STOPPED` | a stop was honoured at a boundary | `resume` |
| `INTERRUPTED` | the server went down while the job was QUEUED / RUNNING | explicit `resume` only |
| `SUCCEEDED` | every success condition holds (below) | terminal |
| `INCOMPLETE` | finished, not a success (residuals, manual review, incomplete scan, rejected / pending proposals …) | terminal |
| `FAILED` | baseline / candidate build failed, verification failed, boundary refused, not ready, internal error | terminal |

`TERMINAL = SUCCEEDED, INCOMPLETE, FAILED`; `RESUMABLE = AWAITING_APPROVAL, AWAITING_CONFIRMATION, STOPPED, INTERRUPTED`.
`state.reason` is the first reason, `state.reasons` all of them (ordered: `baseline_build_failed`, `scan_incomplete`,
`zip_hash_mismatch`, `candidate_build_failed`, `pending_approval`, `rejected_proposals`, `residual_findings`, `manual_review`,
`no_candidate_version`, the rest sorted, `rounds_exhausted` last). Reason codes: `baseline_build_failed`,
`candidate_build_failed`, `scan_incomplete`, `upload_uncertain`, `zip_hash_mismatch`, `text_only_violation`,
`resolution_mismatch`, `execution_boundary_refused`, `not_ready`, `pending_approval`, `rejected_proposals`,
`residual_findings`, `manual_review`, `no_candidate_version`, `rounds_exhausted`, `stop_requested`, `internal_error`,
`policy_denied`, `unknown_provenance`, `stale_location`, `inventory_incomplete`, and on the chained path only `chain_budget`
(`STOPPED`, the time budget was used up; defined in `chain.py`). Scan `incomplete_reasons`: `analysis_failed`,
`analysis_timeout`, `page_missing`, `total_mismatch`, `duplicate_result`, `summary_mismatch`, `unsupported_sw`,
`zip_hash_mismatch`, `id_mismatch`, `http_error`, `stop_requested`. Intake rejection codes (HTTP 400): `not_zip`, `encrypted`,
`path_traversal`, `absolute_path`, `symlink`, `special_file`, `duplicate_entry`, `normalized_collision`, `too_many_entries`,
`too_large`, `compression_ratio`, `no_pom`, `empty`. Boundary finding codes: `project_maven_config`, `maven_extensions`,
`maven_wrapper`, `symlink`, `foreign_repository`, `foreign_plugin_repository`, `settings_in_project`, `unparsable_pom`.

Inventory origins: `direct_literal`, `direct_property`, `own_management` (version text in the project poms),
`parent_managed`, `bom_managed`, `transitive` (planned as a `managed_override` — or `parent_version` / `bom_version` when the
manager is pinned — or left residual) and `plugin`, `vendored`, `unmapped` (never proposed, always residual). Change kinds:
`direct_version`, `property_version`, `own_management_version`, `managed_override` (the only `op="insert"` edit),
`parent_version`, `bom_version`. Edit ops (`TextEdit.op`): `replace`, `insert`. Risk flags (= values of
`remediation.always_approve`): `major`, `parent`, `bom`, `managed_override`. Decisions: `auto`, `pending`, `approved`, `rejected`.

### The graph (`sbmig/remediations/graph.py`, a LangGraph separate from `sbmig/graph/`)

Ten nodes, state saved at every boundary; an entry router jumps to `state.next_node` (= resume). A stop request is checked
before every node and inside builds / IVAS polling.

| Node | Does | Goes to |
|---|---|---|
| `baseline_build` | Maven readiness (else `FAILED not_ready`); boundary check of `accepted/` (any `refuse` → `FAILED execution_boundary_refused`); `package` on a copy | `baseline_scan`; build failed → `FAILED baseline_build_failed` |
| `baseline_scan` | original ZIP sha256 re-checked; IVAS scan of `original/source.zip` | actionable findings → `inventory`; none (or only `ignore_actions` codes) → delivery = byte copy of the original, `evaluate`; incomplete → `INCOMPLETE scan_incomplete`; uncertain upload → `AWAITING_CONFIRMATION` |
| `inventory` | `dependency:tree` of the before-tree; findings → `Inventory` | `plan`; resolution / inventory incomplete → `INCOMPLETE manual_review` |
| `plan` | proposals + residuals, approval classes, hashes; a plan whose tree / policy hash no longer matches is kept as `plan.stale.json` and re-made | pending → `AWAITING_APPROVAL`; something to apply → `apply`; nothing → `evaluate` |
| `apply` | copy `accepted/` → candidate, apply auto + approved edits, write `candidate.patch`, text-only check | `build`; violation → `FAILED text_only_violation`; nothing applied → `evaluate` |
| `build` | `package` the candidate, resolve again, list packaged jars, `verify_resolution`; then commit into `accepted/` | `freeze_delivery`; build failed → `FAILED candidate_build_failed` (with `include` the failed candidate is frozen as `result.zip` first, not rescanned); mismatch → `FAILED resolution_mismatch` (never delivered) |
| `freeze_delivery` | deterministic `delivery/result.zip` from `accepted/` (manifest paths only) | `rescan` |
| `rescan` | delivery sha256 re-checked; IVAS scan of the same bytes (`final-r<n>`) | `evaluate`; incomplete → `INCOMPLETE scan_incomplete`; sha differs → `FAILED zip_hash_mismatch`; uncertain → `AWAITING_CONFIRMATION` |
| `evaluate` | actionable findings remain, last round committed and `round < remediation.max_rounds` → next round | `inventory` (round + 1) or the success decision → `report` |
| `report` | `report.md` / `report.json` (terminal states only) | end |

Scan bookkeeping: before an upload `state.pending_upload[label] = sha` is saved; on the receipt it moves to
`state.pending_receipt[label]` (saved at once); both are cleared when the scan ends. A resume with a pending receipt polls that
analysis without uploading; a resume with a pending upload and no confirmation halts again in `AWAITING_CONFIRMATION`.

### SUCCEEDED

Only when all hold: the baseline build passed; every round that applied something built, verified and committed; the final scan
is complete; `delivery.sha256 == final scan zip_sha256`; `delivery.build_ok` (the delivery is not a failed candidate) and
`delivery.scanned` (the complete final scan was of these bytes); no proposal is `pending` or `rejected`; no residual (manual
review, unknown provenance, policy denied, no candidate version, stale location, skipped proposal); no actionable finding left in
the final scan (findings whose IVAS action code is in `remediation.ignore_actions` do not count — "IVAS action codes" below). Anything else is `INCOMPLETE` (or `FAILED` from a node) with every reason listed (a delivery with `build_ok=false` adds
`candidate_build_failed`, one with `scanned=false` adds `zip_hash_mismatch` or `scan_incomplete`).

### Failed candidate (`remediation.failed_candidate: include` (default) | `discard`)

User decision 2026-11-11 (after comparing with the company's older implementation, which shipped the failed change set too):

- **`include`** (default): a candidate whose **build** failed is frozen into `delivery/result.zip` (manifest paths only, same
  deterministic ZIP) with `DeliveryInfo.build_ok=false`, `scanned=false`; it is **not rescanned** (it cannot be a success, so no
  IVAS upload is spent). Status `FAILED`, reason `candidate_build_failed`; `accepted/` and `original/source.zip` stay as they
  were; `rounds/<n>/candidate.patch` and `rounds/<n>/build.log` stay. `report.md` starts, right under the title, with a Korean
  warning that `result.zip` is the 빌드가 실패한 수정본 (round, applied proposals, build log, not rescanned, original kept); the
  report's delivery section says `build_ok` / `scanned`. If an earlier round's delivery was built, committed and rescanned
  (complete, same sha256), it is not deleted but moved to `delivery/result-r<k>-verified.zip` (+ `.json`, `scanned=true`) and is
  downloadable as `result-verified.zip`; the report names it. Journal: `failed_candidate_delivered` (round, sha256),
  `verified_delivery_kept` (round, delivery round, file, sha256).
- **`discard`**: the failing candidate is never in `result.zip`; the failing change is kept only as `candidate.patch`
  (+ `build.log`); an earlier round's verified delivery stays `result.zip` (journal `earlier_delivery_kept`), else there is no
  delivery.
- Under **either** value a candidate that fails the text-only check (`text_only_violation`) or the resolution check
  (`resolution_mismatch`) is never delivered — it is discarded exactly as with `discard`.

`failed_candidate` is part of the policy hash (changing it invalidates open proposals).

### Approval

`remediation.require_approval` defaults to **false**. What is then applied without approval: a proposal of kind
`direct_version`, `property_version` or `own_management_version` (the version text sits in the project's own poms) whose target
stays **within the same major** version. Always waiting for approval regardless of `require_approval`: every proposal carrying a
risk flag listed in `remediation.always_approve` — shipped: `major` (major change), `parent` (`<parent><version>`), `bom` (an
imported BOM's version), `managed_override` — and, as a fixed rule in the planner, **every `managed_override` proposal** even
when an operator removes `managed_override` from `always_approve` (an XML insertion is never automatic). Parent / BOM version
changes are proposed only when the server pins that parent / BOM itself in `policy.pins` (a pinned manager takes the
`parent_version` / `bom_version` route, never an override).

Managed-version override (user decision 2026-11-11): a `transitive`, `parent_managed` or `bom_managed` finding whose manager is
not pinned gets a target by the same rules as a direct dependency (an IVAS patched-version hint is required unless a policy pin
names the g:a, evidence from the policy or an approved repository host, same-major preferred, `policy.deny` /
`deny_versions` / `allow_major` apply) and then ONE `managed_override` proposal: one `op="insert"` edit into the root pom
(shape in "Hard rules"); findings of the same g:a share one entry. When the root pom already has a `<dependencyManagement>` entry
for that g:a, no second entry is ever added: a single top-level entry whose version text (literal or project property) equals the
resolved version takes the replace route (`own_management_version` / `property_version`), anything else is residual. Residual
(`manual_review` / `no_candidate_version`) instead of an override when: no hint / no candidate version; the resolved dependency
is a non-jar type or has a classifier (an override entry without type / classifier would not govern it); a module holding it does
not inherit from the root pom; a module declares the dependency with its own version (a declared version wins over management);
no safe insertion line (the anchor tag not on its own line, e.g. single-line XML; more than one `<dependencyManagement>`); the
root pom is unreadable. The proposal `note` states what the entry pins (the transitive chain or the manager) and always says that
later parent / BOM / direct-dependency upgrades will NOT move this version any more and that the owners must remove the override
when those catch up, and that it always needs approval; `report.md` shows every proposal note under the proposals table.
Never proposed, always residual / manual review: `plugin`, `vendored`, `unmapped`, `policy.deny` matches, majors when
`policy.allow_major` is false. A `property_version` proposal notes that the property may govern other artifacts; it is still
automatic when same-major. With `require_approval: true` every proposal waits. **A production deployment that needs human approval of every
change must set `SBMIG_REMEDIATION__REQUIRE_APPROVAL=true` (or `remediation.require_approval: true` in the company yml)
explicitly** — the shipped default applies same-major own-pom changes on its own.

A decision is accepted only when the **proposal hash, the source tree hash, the policy hash and the diff** all equal the stored
proposal's, the stored proposal still matches its own hash, the current `accepted/` tree hash and policy hash (not stale), it is
still `pending`, and the decision is `approved` or `rejected`. `proposal_hash` = sha256 of the canonical JSON of
`{ga, from, to, kind, sorted risks, edits, diff}` (each edit in full: pom, line, old, new, `op`, `anchor`, `override` — a plan
saved before the override change no longer matches its own hash, so a decision on its open proposals is refused with 409
`approval_mismatch` / `proposal_hash`; upload such a project again); `source_tree_hash` = hash of `accepted/` over the manifest paths when the
plan was made; `policy_hash` = sha256 of `remediation.policy` + `require_approval` + sorted `always_approve` + `failed_candidate`
+ sorted, upper-cased `ignore_actions` (changing any of them invalidates every open proposal; adding `ignore_actions` to the hash
made every plan saved before it stale once — it is re-made at the next resume). A request is **all-or-nothing**: one mismatching decision → HTTP 409
`approval_mismatch` naming the `fields`, nothing written. Decisions are appended to `approvals.jsonl`; approving does not resume.
A rejected `(g:a, version)` is not proposed again in later rounds of that job (residual `rejected_proposals`). The `by` field is
optional (empty allowed) and recorded as given; identity is whatever the app's TokenGuard admits. The page's approval is a
checkbox list with one button `선택한 항목 적용`: checked = approved, unchecked = rejected, a confirm step ("N건 적용, M건은 적용하지
않음"), then the resume happens automatically (`"resume": true`).

### HTTP API

Served by the existing FastAPI app (the web pod), separate from `/api/runs`. A chained remediation's id is the migration run id
(status / proposals / files / approvals; its resume dispatches a new build pod in a Job mode, "Execution model"). The
standalone upload runs in worker threads of the web pod in `in-process` mode only (one at a time, further uploads wait `QUEUED`);
in `k8s-job` / `local-job` it is 409 `not_available`. All routes sit behind the app's TokenGuard. Errors are
`{"error": "<message>", "code": "<code>", …}`.

| Route | Result |
|---|---|
| `POST /api/remediations?name=<file.zip>[&configs=a.yml,b.yml]`, body = zip bytes | 202 `{id, status, url}`; 400 intake rejection (code as above); 413 over `remediation.max_upload_mb`; 503 `not_ready` + `missing` (names only) |
| `GET /api/remediations?page=1&size=20` | `{items, page, size, total}`, newest first, size ≤ 100 |
| `GET /api/remediations/readiness` | `{ready, missing}` — names of what is missing, never values |
| `GET /api/remediations/{id}` | status: id, name, status, reason, reasons, message, node, next_node, round, counts, created, updated, stop_requested, pending_approvals, delivery `{sha256, size, round, build_ok, scanned}` or null, files, links |
| `GET /api/remediations/{id}/result` | 200 `report.json` when terminal (incl. `delivery` `{path, sha256, size, round, build_ok, scanned}` and `verified_delivery` `{path, sha256, round, size}` or null); else 409 `not_finished` + status |
| `GET /api/remediations/{id}/proposals[?all=1]` | `{status, round, source_tree_hash, policy_hash, proposals, residuals}` for the current round (all rounds with `all=1`); proposals in full incl. diff, the three hashes and the decision |
| `POST /api/remediations/{id}/approvals` | body `{by?, resume?, decisions: [{proposal_id, decision, proposal_hash, source_tree_hash, policy_hash, diff, comment}]}` → 200 `{accepted, pending}`; 409 `approval_mismatch` (+ `fields`), 409 `state_conflict` outside `AWAITING_APPROVAL`. Without `resume` it does not resume; with `"resume": true` it resumes after writing the decisions and adds `resumed` (success) or `resume_error` (the code / message the resume answered — the decisions stay written) |
| `POST /api/remediations/{id}/resume` | body optional `{"confirm": "reupload"}` or `{"confirm": "receipt", label, project_id, analysis_id}` → 202 status (a chained remediation in a Job mode: a new build pod, queued in the upload FIFO line); 409 `state_conflict` when not resumable, when proposals are still pending, or when `AWAITING_CONFIRMATION` without `confirm` |
| `POST /api/remediations/{id}/stop` | 202 `{stop_requested: true, note}` (takes effect at the next boundary; a running IVAS analysis is not cancelled); 409 unless QUEUED / RUNNING |
| `GET /api/remediations/{id}/files/{name}` | whitelist: `report.md`, `report.json`, `result.zip`, `source.zip`, `manifest.json`, `journal.jsonl`, `remediation.log`, `delivery.json`, `result-verified.zip` (the kept `delivery/result-r<k>-verified.zip`, highest k; 404 when none), `plan-r<n>.json`, `candidate-r<n>.patch`, `build-r<n>.log`, `baseline-build.log`, `scan-<label>.json`; 404 otherwise |
| `DELETE /api/remediations/{id}` | 204; 409 `state_conflict` while QUEUED / RUNNING; removes the whole directory incl. the original ZIP |

Confirmation after an uncertain upload: `reupload` = the operator checked IVAS and no analysis of that ZIP exists → sbmig uploads
once more; `receipt` = the operator found the analysis → sbmig records that project / analysis id for the scan `label` and
continues polling it without uploading. Restart rule: at manager start every `state.json` that is QUEUED / RUNNING with another
process's `owner` token is rewritten to `INTERRUPTED` (reason kept, message: the server restarted); nothing is resumed or re-sent.

### Configuration

Precedence as for every sbmig key: `config/default.yml` → the company yml (`artifacts.yml`, baked into the image) →
`SBMIG_REMEDIATION__<KEY>` environment overrides (e.g. `SBMIG_REMEDIATION__REQUIRE_APPROVAL=true`,
`SBMIG_REMEDIATION__POLICY__ALLOW_MAJOR=false`). Keys: `trusted_projects` (the operator's confirmation that uploaded projects may be
built on this server — Maven runs the project's plugin code, this is not a sandbox; default **false** = nothing is accepted,
readiness names it; set `SBMIG_REMEDIATION__TRUSTED_PROJECTS=true` explicitly), `max_upload_mb`, `max_entries`, `max_unpacked_mb`, `max_entry_mb`,
`max_compression_ratio` (intake limits; the ratio applies to entries ≥ 1 MiB unpacked), `require_approval` (default false),
`always_approve` (default `[major, parent, bom, managed_override]`; `managed_override` proposals wait even if it is removed),
`max_rounds` (3), `failed_candidate` (`include` default | `discard`, see "Failed candidate"), `ignore_actions` (default `[]`,
see "IVAS action codes"; part of the policy hash), `chain_budget_minutes` (90; 0 = none; chained path only), `project_maven_config` /
`maven_wrapper` / `foreign_repository` (`refuse` | `ignore`), `allowed_repository_hosts`, `build_timeout_minutes`,
`ivas_timeout_seconds`, `ivas_upload_timeout_seconds`, `ivas_poll_seconds`, `ivas_analysis_timeout_minutes` (default **30** =
1,800 s; past it the scan is incomplete, never 0 findings), `ivas_page_size`, `ivas_project_prefix` (IVAS project name `<prefix>-<id>-<label>`),
`ivas_summary_check` (`exact` default | `nonzero`, see "Completeness"), and
`policy.pins` / `policy.deny` / `policy.deny_versions` / `policy.allow_prerelease` / `policy.allow_major`.

Secret (Kubernetes Secret, environment only, never in yml): `IVAS_USER_ID`, `IVAS_PASSWORD`; `IVAS_BASE_URL` travels with them
(it is a company host, not a credential, but it must not land in the repo or the baked yml). Nexus credentials stay where the
migration has them: the server `settings.xml` `<server>` entries reading `${env.NEXUS_USER}` / `${env.NEXUS_PASSWORD}`. Plain
config: every `remediation.*` key, `build.settings_file`, `build.java_home`, `build.mvn`, `build.local_repo`, `build.extra_args`.
The deployment puts the three `IVAS_*` keys into Secret `sbmig-llm`: the Job template `81-web-job.template.yaml` reads them by
`secretKeyRef` (optional), the 80 model and the web pod by `envFrom` (the web pod uses them only in `in-process` mode). They are not
in ConfigMap `sbmig-env`. Remediation overrides that must reach the build pod (`SBMIG_REMEDIATION__AFTER_MIGRATION`, and if set
`…__REQUIRE_APPROVAL` / `…__FAILED_CANDIDATE` / `…__IVAS_ANALYSIS_TIMEOUT_MINUTES` / `…__CHAIN_BUDGET_MINUTES` /
`…__IGNORE_ACTIONS` / `…__IVAS_SUMMARY_CHECK`, the last six shipped as commented lines) live in `sbmig-env` with a line each in 81 —
a literal in 40 only would be a `main_only` env-pin difference and stop every Job.

Readiness (`GET …/readiness`, also checked on upload and at `baseline_build`): `remediation.trusted_projects` true (named first
when not; standalone path only — the chained path does not ask it); `IVAS_BASE_URL` set, a valid `https` URL (plain
`http` only for loopback), no credentials / query / fragment in it; `IVAS_USER_ID` and `IVAS_PASSWORD` set; Maven runnable and
not a wrapper; JDK 21 at `build.java_home` / `JAVA_HOME`; `build.settings_file` set, present and readable; at least one approved
repository host (`remediation.allowed_repository_hosts`, else the mirrors / repositories of the server settings.xml).

### IVAS HTTP contract (as given by the company; `sbmig/remediations/ivas.py`)

| Call | Request | Used from the answer |
|---|---|---|
| login | `POST {IVAS_BASE_URL}/session`, JSON `{userId, password}` | `token` → header `X-API-KEY` on every later call; a fresh login right before each upload |
| upload | `POST /projects/SELF/sourcecode`, multipart: part `analysisRequest` (JSON `{"projectNm": "<prefix>-<id>-<label>", "source": {"type": "ZIP_FILE"}}`) + part `file` (the ZIP, streamed with Content-Length; field name `file` confirmed by the company) | `projectId` + analysis id: `lbrdrAnlId`, else `analysisId` (confirmed by the company); the code also still accepts the document's other spelling `lbrdAnlId` (`ANALYSIS_ID_KEYS`) |
| status | `GET /projects/SELF/{projectId}/analysis/{analysisId}/status` | `anlStatusDivCd`: `PREPARING` / `WAITING` / `ANALYSIS` = in progress, `DONE`, `FAILED` / `ERROR` / `CANCELED` / `CANCELLED` = failed; ids in the answer must equal the request (`id_mismatch`) |
| summary | `GET …/analysis/{analysisId}/result/summary` | `summary.resultCount.vulnerabilities.<cat>.total` for `components`, `swComponents`, `fileComponents`, `codeSnippets`, `file`, `functionComponents`, `ospkgComponents`, `binaryComponents` (all eight must be integers) |
| library | `GET …/result/libraryvulnerabilities?page=&size=` | `content[]`: `id`, `library.identifier` (Maven `g:a`), `library.version`, `library.info.patchedVersion`, `detectedLocations[].filePath`, ratings, action; `totalElements` |
| sw | `GET …/result/swvulnerabilities?page=&size=` | any item → scan incomplete (`unsupported_sw`) |
| file components | `GET …/result/filecomponentvulnerabilities/main` and `/potential` `?page=&size=` | `id`, `fileComponent.id` (only when it is missing: `fileComponent.ossId`, else top-level `ossId`, with a scan note), version, patched version; always `vendored` |
| matched files | `GET …/result/filecomponent/{componentId}/matchedfiles?page=&size=` | `content[].file.fullPath`. The id may contain `\|` (`11560\|934982`, the company calls it ossId): it is sent as ONE encoded path segment (`11560%7C934982`) and kept unchanged in `Finding.component_id` |

Completeness: pages start at 0 and are read until `totalElements` items arrived (safety cap total/size + 2); a page that cannot be
read, an early empty page, a `totalElements` that changes between pages, more or fewer items than announced, a duplicate
`(kind, id, identifier, version)` or matched path, an item without id → incomplete. Summary reconciliation
(`remediation.ivas_summary_check`, default `exact`; the company's existing IVAS client compares `components.total` with the number
of `libraryvulnerabilities` detail rows, user 2026-11-11): `components.total` must EQUAL the library rows returned over all pages
(counted before de-duplication; skipped when the library pages already failed — `page_missing` / `total_mismatch`), else
`summary_mismatch` with both numbers in the note. `swComponents` ↔ sw and `fileComponents` ↔ file main + potential are compared
zero / non-zero only (that mapping is not confirmed; the note says so). `nonzero` compares `components` zero / non-zero as well —
the operator's switch if the real count means something else; not in the policy hash. The five categories without a detail
route must be 0. GETs retry up to 3 times on connection errors / timeouts / 5xx / 429; three status polls failing in a row, or no
`DONE` within `remediation.ivas_analysis_timeout_minutes`, make the scan incomplete (the remote analysis keeps running). The
upload is sent **once**: a failure clearly before the body left (login refused, file unreadable, connection never opened) or a
clear 4xx is `IvasError` (scan incomplete); every ambiguity after the body may have left (timeout, dropped connection, 5xx, a 2xx
without ids or not JSON) is `IvasUploadUncertain` → `AWAITING_CONFIRMATION`, never a retry. TLS verification is never disabled
(`REQUESTS_CA_BUNDLE` is honoured by requests for an internal CA).

**IVAS action codes** (`action.actionDivCd`, the company's document; the table lives once in `ivas.ACTION_MEANINGS`): `NE` 조치 필요,
`NP` 조치계획 미등록, `PL` 조치예정, `OT` 조치일 경과, `PC` 패치 완료, `IG` 조치 예외. `remediation.ignore_actions` (default **empty**
since 2026-11-11 — the company's existing IVAS client does not filter PC / IG either, so every code is actionable; `[PC, IG]` leaves
out what the security team closed; compared trimmed and case-insensitively): a finding whose code it names is KEPT in `scans/<label>.json` and shown in the report (own table
"조치 대상에서 뺀 항목" with the code's meaning; `report.json` `ignored_findings`, `ignore_actions`, per scan `actionable` / `ignored`)
but is not handed to the inventory / planner and does not count as a remaining finding (SUCCEEDED, the next-round decision,
`counts`). Counts: `baseline_findings` / `final_findings` = actionable only, plus `baseline_ignored` / `final_ignored`. An empty or
unknown code is actionable (never silently ignored; an unknown code is ignored only when `ignore_actions` names it) and noted
once (journal `ivas_action_unknown`, a line in the report).

### Maven boundary (`sbmig/remediations/maven.py`, `intake.check_boundary`)

The server's Maven only (`build.mvn`; a wrapper path is "not ready"; the project's `mvnw` is never run), JDK 21, the server
`settings.xml` passed with `-s` and required, approved repository hosts / mirrors only (a pom `<repository>` /
`<pluginRepository>` on another host → `remediation.foreign_repository`, default refuse), insecure-TLS properties and other
`-s` / `-f` stripped from `build.extra_args`, tests never run (`-Dmaven.test.skip=true` on every command), `clean package` as the
gate, `dependency:tree` per module for resolution. The standalone (in-process) path uses its own Maven local repository,
`<local repo>-remediation`, so a remediation build never shares a repository with a migration running in the same pod; the chained
path uses the build pod's slot repository (the migration in that pod has finished).
**`.mvn/` cannot be disabled on Maven 3.9** (the launcher always reads `.mvn/maven.config`, `.mvn/jvm.config` and
`.mvn/extensions.xml` from the first ancestor holding `.mvn`), so the intake boundary refuses them by default
(`remediation.project_maven_config: refuse`); with `ignore` every build logs a WARNING and journal `maven_project_config`.
Symlinks are always refused; a `settings.xml` inside the project is recorded and ignored.

### Open questions for the company

Answered by the user (2026-11-11), no longer open: the failed-candidate policy (`include`, `discard` kept as an option); which
classes need approval (shipped `always_approve`, `managed_override` always); the multipart file field (`file`); the analysis id
field (`lbrdrAnlId`, else `analysisId`); transitive / managed findings (approved override entries in the root pom); the meaning
of the action codes (`actionDivCd`, table above); several logins / tokens at once (they work); whether PC / IG are left out (no:
`ignore_actions` is empty by default, like the existing client).

1. IVAS: which field is the matched-files id — `fileComponent.id` (used today) or `ossId` (the company's word; used only when
   `fileComponent.id` is missing); the shape of `patchedVersion` (one version, a list, a range?); what `components.total` counts
   (assumed = library vulnerability rows, as the older in-house implementation compares; relax with
   `remediation.ivas_summary_check: nonzero` if wrong); the complete list of `anlStatusDivCd` values, the SW
   vulnerability fields (today any SW item makes the scan incomplete), the display-name field of a file component, whether pages
   are 0-based.
2. IVAS: how an operator checks whether an uncertain upload arrived (search by project name?), the token lifetime, rate limits /
   concurrent analyses per account, whether one IVAS project per scan (`<prefix>-<id>-<label>`) is acceptable, the internal CA.
3. Which repository is "the approved internal repository" for version evidence, and who owns `remediation.policy` (pins / deny).
4. Approval in production: `require_approval: true`? Who may approve (`by` is free text behind the TokenGuard)?
5. Managed overrides: may two approved overrides share one newly created `<dependencyManagement>` section in one round (today:
   yes — both inserts target the same line and become one section holding both entries in proposal-id order)? Is the inserted
   indentation style (copied from the sibling entries, else the closing tag + one indentation step of the file) acceptable? May
   sbmig later learn which parent / BOM version fixes a finding (so the override can be dropped)?
6. Retention: how long original ZIPs, delivery ZIPs and scans stay on the volume; `DELETE` removes everything.

Not built yet: plugin / vendored fixes; a run against the real IVAS and Nexus.
