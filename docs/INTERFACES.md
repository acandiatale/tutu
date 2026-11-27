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
| intake / analyze | Base "Intake and analyze", "Externalized configuration"; v2.1j (Ant), v2.1k + v2.1z "Review follow-ups" (rootless trees), v2.1z-c (hollow reactor), v2.1z-d (ADF), v2.1z-h (stray poms), v2.1z-i (bundled jars), v2.1z-z (build extensions); v2.1z-aj (Ant refused by default); v2.1z-ak (framework projects: detection, scope); v2.1z-aw (framework projects: conversion only, the framework's name) |
| plan | v2 "Plan (L)"; v2.1o (1) (`plan.compile_after_rewrite`); v2.1u … v2.1y / v2.1w (recipe sets); v2.1z-ak (the reduced plan `jdk-jakarta`) |
| transform (OpenRewrite) | Base "Plan, transform"; v2.1l "Recipes"; v2.1o (Framework 7); v2.1w; v2.1z-br (`openrewrite.fail_on_dry_run_results` removed); v2.1z-ci (OpenRewrite reads mirrors differently from Maven: a translated settings copy for its call, the failed pom named, no retry for a warning); v2.1z-cj (a classifier-only library without a pom: a minimal pom with packaging `pom` in the local repository, then the same call again) |
| restructure & layout | Base "Restructure and the company layout"; v2.1g, v2.1h (JSP jakarta), v2.1l / v2.1s (version pins), v2.1m (resource filtering), v2.1n (optional triggers), v2.1o / v2.1p / v2.1t (parent-chain facts, BOM imports), v2.1q (embedded Tomcat), v2.1x (legacy Spring context), v2.1z-j (bundled jars), v2.1z-k, v2.1z-l; v2.1z-cc (annotation processor paths without a version) |
| structure review | v2 "Structure review (R)"; v2.1u … v2.1y (review prompt, invariant check, unsupported issues); v2.1z-h (review guard) |
| verify & script fix loop | Base "Fix-loop invariants"; v2 "Script fix loop (L)", "Script contract (S)", "As built (step 1)"; v2.1d (versions, operator checks), v2.1l / v2.1m pre-tries, v2.1r (file locks), v2.1y (declines, idempotency), v2.1z (ladder caps, case keys), v2.1z-e (jakarta discovery), v2.1z-i, v2.1z-n, v2.1z-y, v2.1z-z, v2.1z-aa; v2.1z-ak (reduced run: as-is chain facts, framework guard); v2.1z-av (stop around the final build, the last unhelpful try rolled back, `repository_unreachable`, cut answers, managed versions per parent); v2.1z-bo (stop request and time budget checked before every LLM request, `llm_ask_skipped`); v2.1z-bp (the remediation's 60 s loop minimum checked by the graph only); v2.1z-br (the framework guard rejects an attempt it cannot check, a builder keyword decided by the callee's signature: `sbmig/loop/callee.py`); v2.1z-by (the earlier fix in the prompt), v2.1z-bz (trade-back), v2.1z-cb (an error's place: file, else module) |
| cases & KB | v2 intro + "Script KB (K)"; Base "KB plumbing"; v2.1e (learning records), v2.1f (probable fixers), v2.1z (masked case keys), v2.1z-ae / v2.1z-af (several pods on one KB), v2.1z-ai (rule versions, member-symbol keys, re-sign, exact-key guard); v2.1z-ca (a version that took part in a round trip: credit `round_trip`, never reused on its own) |
| LLM gateway | Base "LLM client tier and adapter seams"; v2.1z-m (agent gateway, token exchange), v2.1z-n, v2.1z-o, v2.1z-p (case thread), v2.1z-q, v2.1z-x, v2.1z-af (busy wait, auth retries) |
| report & result line | Base "Report and the result line", "Advisories and the Handover"; v2 "As built (step 1)" (`review=`); v2.1d (`major=`); remediations (`remediation=`); v2.1z-ak (`scope=`, the framework block); v2.1z-bl (`llm=` includes the remediation's fix-loop calls); v2.1z-ck (a framework run whose conversion is done reads as done in the result cell; `exit=` / `build=` unchanged) |
| fleet | Base "Fleet" |
| web app | Base "Graph engine", "Web app"; v2.1z-w (page, server-chosen configs), v2.1z-ae (queue / slots on the page), v2.1z-af (page re-attach), v2.1z-aj (run flows, page header); v2.1z-ak (framework label, skipped stages); v2.1z-ao (admin page `/admin`: token usage per project, log ZIPs); v2.1z-au (the code-change view: artifact `codediff/`, `/api/runs/{id}/codediff`, the page block); v2.1z-av (cross-site requests refused, the free-space floor for uploads, unpacking limits); v2.1z-bg (report view); v2.1z-bl (two report previews, 고급 설정 and the other-files list gone, LLM counts); v2.1z-bm (the hidden controls are not sent); v2.1z-bo (admin page: 「일부 알 수 없음」 / cost 「알 수 없음」 for unknown remediation usage, `remediation_llm_usage_unknown`); v2.1z-bp (the run cost = the priced rows with 「일부 알 수 없음」, 「알 수 없음」 only when nothing is priced); v2.1z-br (`GET /api/runs` never answers 500 because of one run, no list refresh while the tab is hidden, no `run_dir` in the status JSON, the report view linear in a line's length); v2.1z-bs (a non-finite number in a run file is sent as `null` by the single-run status, the upload answer, the code-change routes, the admin usage and the event stream's `status` event: `sbmig/web/jsonsafe.py`; `status()` reads `report.json` once); v2.1z-bt (result ZIPs received as `<project>-migration.zip` / `<project>-remediation.zip`: `contract.zip_download_name`, `zip_name` in both statuses); v2.1z-ch (the admin page's log ZIP of one run carries `logs/`, the reports, the error list and the state files); v2.1z-ck (`conversion_complete` in the run status, the page's 완료, the admin result word `converted`) |
| Job mode (Main / Worker, slots, env pins) | v2.1z-z "k8s Job dispatch mode", v2.1z-ab (kubeconfig, template ConfigMap), v2.1z-ac (Main ↔ Worker contract), v2.1z-ad (end confirmation, ownership, env pins, write-time redaction), v2.1z-ae (parallel slots), v2.1z-af, v2.1z-aq (a build pod that does not start: stop, the reason notice); v2.1z-av (the build identity, pins taken at Job creation, the resume attempt) |
| deploy | v2.1z-s (build hooks), v2.1z-t (one Job per request), v2.1z-u, deploy (baked config, container paths), v2.1z-ad "Shipped mode"; v2.1z-br (what the image build context leaves out in both layouts, `config/default.yml` lists every settable key); v2.1z-cg (the image's JDK: Amazon Corretto 21 at a pinned version, checksum checked) |
| framework conversion (not wired into a run yet) | v2.1z-cl (the converters of `sbmig/fwconvert/`; rules, records and module table in `docs/FW-CONVERT.md`) |
| remediations | remediations; v2.1z-ak (in-house group `framework`); v2.1z-am (case-insensitive matching, per-module in-house split, module sources, variant overrides, 들어온 경로); v2.1z-an (override proposals in module poms, the owners' choice); v2.1z-ar (no approval unless switched on); v2.1z-av (fixed = gone from the rescan, the planner's new residual groups, `remediation.log` for every run); v2.1z-bi part B, v2.1z-bj, v2.1z-bk (the fix loop on a failed candidate: work copy, overlay, build-side sync, one `fix.patch`); v2.1z-bl (fix-loop LLM usage, titles with the entered project); v2.1z-bm (the complete-apply proof `expected.json`, the sync by table before commit / freeze, a candidate that cannot be put back, `stop requested` runs again, leftover loops settled); v2.1z-bn (one delivery swap + `recover_delivery`, the no-delivery sentences, usage after an exception, the exec bit in `expected.json` schema 2, the pending report, the scan source `original/scan-source.zip`); v2.1z-bo (the swap rolled forward + `DeliveryRecordNotWritten`, no re-upload after a kill and the table of mid-node saves, `repair_final` by every entry and by the web service in every mode, unknown usage, report = state message, the round's call total, a stop during the build fix); v2.1z-bp (`FixResult.ended_round`: the loop is not skipped on resume, an own row for calls without tokens, strict journal reading, the budget outcome saved and stamped, 「알려진 것만」, the report-not-written note dropped on repair, the bounded repair memory); v2.1z-br (unused code removed, `compare_trees` reads only what an attempt changed, `freeze_delivery` hashes while it writes); v2.1z-bs (`intake.sync_tree` removed; the fix loop still starts for a failure only a pom can fix: "Not changed"); v2.1z-ce (`fsops`: a rename or removal Windows refuses while another program has a file open is waited for); v2.1z-cf (a refusal that lasts names the programs that have the file open; a test that fails when this process holds a path it renames) |

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
  `rewrite-parse-problems`, phase note (a reduced-scope run also gets a hand-over line naming the files: v2.1z-ck).
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
  `metadata=` (clients may ignore them). `LLMResponse.model` = the name the call asked with (on the agent gateway the
  role), `LLMResponse.served_models` = the real model names the answer reported (v2.1z-bg). Roles: `fix`, `fix_escalation`, `plan_advisor`, `structure_review`,
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
pushed per run; runs sequential) or a PVC. (2) The Maven local repository on an emptyDir downloads Boot 4 / OpenRewrite / plugins from Nexus on
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
  once `status.state == finished`: `보고서 받기` (report.md, received as `migration-report.md` since v2.1z-bg) and `변환된 프로젝트 받기` (result.zip, received as `<project>-migration.zip` since v2.1z-bt) as download buttons (disabled when the
  file is absent), the other served files as small links, the report under 보고서 미리보기 (rendered as Markdown since v2.1z-bg) → 지난 실행 (collapsed list; `#<run id>`
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
  `CHANGE_ME_REGISTRY/sbmig:CHANGE_ME_TAG` + `imagePullPolicy: Always` (the company pushes the same tag again: a pod start checks the registry and
  pulls only a changed image; `IfNotPresent` kept each node on the build it pulled first; the node-import path is README §1.1);
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
- **ConfigMap `sbmig-config`** carries `settings.xml` only (since v2.1z-al the source of the plain file `/data/.m2/settings.xml`: mounted at `/etc/sbmig/settings.xml` in 40 and copied
  once; 80 mounts it at `/data/.m2/settings.xml`;
  the `artifacts.yml` key and its mounts are commented alternatives in 20 / 40 /
  80 / 81). The baked file must hold no secret (an image is readable by whoever pulls it).
- **Machine paths** of a PC-tested yml are overridden by literal `env` lines in each manifest (env beats every yml):
  `SBMIG_BUILD__SETTINGS_FILE=/data/.m2/settings.xml` (v2.1z-al; `/etc/sbmig/settings.xml` before), `SBMIG_BUILD__JAVA_HOME=/opt/java21`, `SBMIG_BUILD__MVN=mvn`,
  `SBMIG_BUILD__LOCAL_REPO=/data/.m2/repository` (v2.1z-al; `null` before), `SBMIG_SCRIPT__PYTHON=null` in 40 / 80 / 81; `SBMIG_KB__DIR=/data/kb-v2` and
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
  local repository, and the three pods share one Maven volume: the `/data/.m2` folder of the data PVC since v2.1z-al). Slot 1, or no `SBMIG_JOB_SLOT`: nothing changes — today's repository,
  byte-identical command lines. Slot k ≥ 2: a sibling directory `<base>.parent / f"{base.name}-slot{k}"` (`…/.m2/repository-slot2`),
  where `<base>` is the repository the run would use otherwise: the Worker's own `SBMIG_BUILD__LOCAL_REPO` when it is a non-empty
  string after `yaml.safe_load`, else `sbmig.build.mvn.local_repo_path(load_config(<the run's configs>, <worker-paths.yml>))`
  (`build.local_repo` → settings.xml `<localRepository>` → `~/.m2/repository`). The Worker creates the directory and sets
  `SBMIG_BUILD__LOCAL_REPO=<that path>` **in the child's environment only**, after its environment-pin check (the pins compare the
  pod's own environment, where 81 keeps its literal, `/data/.m2/repository` since v2.1z-al). It never fails a run over this: when the base cannot be resolved or the
  directory cannot be created, the run goes on with the shared repository and says so (stdout line + journal). Journal event
  **`worker_slot`** `{slot, local_repo, shared: bool, note?}` (only when `SBMIG_JOB_SLOT` is set). A slot's first runs download
  everything once; the repositories persist on the PVC.
- **Status** (`RunManager.status`): every answer carries `parallel: {slots, running, waiting}`; a run waiting in this process's line
  carries `queue: {position, ahead}` (`position` 1 = next, `ahead = position - 1`); a Job run that holds or held a slot carries `slot`
  (memory, else `ControlState.slot`). `GET /api/runs[?days=N&keep=<run id>]` → `{runs: [...], parallel: {...}, days, hidden}` (the list cut: "v2.1z-az"; each listed run as before, plus `queue` /
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
  `fsGroup` of that folder on the shared volume that holds `build.local_repo`); the shared repository only when that fails too. This replaces v2.1z-ae's "the run goes
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

## v2.1z-ag — an as-is version of the company parent that the repository does not have

OpenRewrite runs on the as-is poms, before the restructure sets the company parents. A project that already inherits a
configured company parent g:a (`artifacts.parent` / `boot_parent` / `comp_parent`, or one of their `aliases`) at another
version — e.g. an old `1.0.0-SNAPSHOT` left by an earlier in-house tool — fails there when the repository lacks that version.
That is not the configured parent missing, and it no longer aborts the run.

- **Classification** (`sbmig/restructure/asis_parent.py`): `classify_parent_misses(text, cfg, ws)` reads the unresolvable
  parent coordinates from Maven's text (`sbmig.build.log_parser.parent_coordinates_in`: `Non-resolvable parent POM for …:`
  followed by `Could not find artifact g:a:pom:v`, `The following artifacts could not be resolved: g:a:pom:v`, `Failed to read
  artifact descriptor for g:a:pom:v` or `Could not transfer artifact g:a:pom:v`, with or without the `'parent.relativePath'
  points at …` tail) and gives one `ParentMiss` per parent: `configured` (the g:a of a spec at its configured version; also when
  no version can be read and the poms on disk declare the configured one, and when the spec has no version) · `asis-version`
  (a spec's g:a at another version, or an alias g:a at any version) · `foreign` (not a company parent). `ParentMiss.target` is
  the configured coordinates (root parent first when several specs share a g:a), `spec_name` the config key
  (`artifacts.parent` …).
- **Switch** `openrewrite.unresolvable_asis_parent: align | continue | abort` (`OpenRewriteConfig`, default `align`) decides
  `asis-version`; `configured` always aborts, `foreign` never does.
- **Alignment** `align_asis_parent(ws, miss)`: every pom whose `<parent>` is that g:a at that version (version resolved
  against the pom's properties, then any project pom's) gets the configured version through `PomEditor.set_parent_version`
  (only the `<version>` text; CRLF, comments and `relativePath` unchanged). An alias g:a gets all three coordinates. A `${prop}`
  version defined in a project pom and referenced by nothing but parent versions is aligned at the property; a property used
  elsewhere too, or defined outside the poms (`.mvn/maven.config`, `-D`), gets the literal. Returns `AlignResult(poms,
  properties)`.
- **Transform** (`Orchestrator._step_rewrite`): a failed run with `asis-version` misses only, under `align` → the poms are
  aligned and committed `transform: as-is parent <g:a> <old> → <configured> (as-is version not in the repository)`, journal
  `{"event": "asis_parent_aligned", "step", "parent": g:a, "from", "to", "target", "spec", "poms", "properties", "commit",
  "where": "rewrite"}`, flag `asis-parent-aligned`, phase note, one Handover line (`as-is parent `<g:a:old>` is not in the
  repository: sbmig set the configured `<g:a:v>` (artifacts.parent) in <poms> before the recipes — the recipes ran on the target
  parent's classpath, not the as-is one …`), then OpenRewrite runs once more. That run failing on the configured version →
  abort; failing otherwise → the usual "continuing" note. A miss left unaligned (`continue`, nothing to change) or a `foreign`
  one → journal `{"event": "rewrite_skipped_asis_parent", "step", "parent", "company_parent", "policy"}`, flag
  `rewrite-skipped-asis-parent`, phase note, Handover line `OpenRewrite did not run: the as-is parent `<g:a:v>` is not in the
  repository — …`; the run continues.
- **Fix loop**: `BuildFixLoop._parent_abort` aborts only for `configured` (or `asis-version` under `abort`). The zero-LLM pre-try
  `sbmig-parent` (first in `_pre_tries`, `TrySource` `sbmig-parent`, commit `fix(sbmig-parent): <cluster id>`, once per parent
  coordinates per run) aligns an `asis-version` miss under `align`, although triage blocks the case `parent_not_found`; journal
  `asis_parent_aligned` with `where: fix loop`, `phase`, `iteration`, `applied`.
- **Abort wording** (`aborted:parent-not-found`, follow-up `run aborted: …`): `Maven cannot resolve the parent <g:a:v>, which is
  the configured version (artifacts.parent = <g:a:v>) — the repository does not serve it (settings: <file>); fix the repository
  / settings, or the version in artifacts.parent, and rerun`; under `abort` for an as-is version: `Maven cannot resolve the as-is
  parent <g:a:v>; the configured version is <g:a:v> (artifacts.parent) — the run stops because
  openrewrite.unresolvable_asis_parent is abort …`.
- **Neighbours**: `infra:repository` comes from `aborted:parent-not-found` or a failed final build only, so an aligned run that
  builds carries none. Triage (`sbmig/triage/blocked.py`) still classifies every parent-resolution case as `parent_not_found`
  (it has no config; the pre-try runs before the block is registered). The intake's as-is `help:effective-pom` stays non-fatal;
  its reason reads `the parent <g:a:v> is not in the repository` (journal `effective_pom.reason`, one WARNING).
  `sbmig repo-probe g:a:v` shows whether the repository has a parent version.
- Fixture `boot-asis-parent-unpublished` (the company-parent stand-in g:a at `2.7.18-NOT-PUBLISHED`).

## v2.1z-ah — Maven's HTTP repository blocker is an environment abort of its own

Maven ≥ 3.8.1 ships the mirror `maven-default-http-blocker` (mirrorOf `external:http:*`, url `http://0.0.0.0/`, blocked) in
`<maven home>/conf/settings.xml`. A repository declared with an `http://` URL is then refused — even for an artifact already in
the local repository (cached under the repository's id; the request now goes to the blocker). Maven 3.9.10 prints:
`Could not transfer artifact g:a:pom:v from/to maven-default-http-blocker (http://0.0.0.0/): Blocked mirror for repositories:
[company-nexus (http://nexus.invalid/repository/maven-public/, default, releases+snapshots), …]` (inside the `Non-resolvable
parent POM …` wrapper for a parent, after `Failed to read artifact descriptor for …` for a dependency). Neither the project nor
sbmig can change that.

- **Classifier** (`sbmig/build/env_errors.py`): `http_blocker_hit(text) -> HttpBlockerHit | None` — `mirror_id` (the mirror
  named after `from/to`, `in` or `update interval of`, else `maven-default-http-blocker`) and
  `repositories: [BlockedRepository(id, url)]` from every bracket list in the text (deduplicated by id; `user:password@` removed
  by `strip_userinfo`). A hit needs evidence that Maven blocked: the phrase `Blocked mirror for repositories` (any mirror id) or
  the built-in blocker's pseudo address in one of Maven's own message shapes — `from/to <id> (http://0.0.0.0/)`,
  `in <id> (http://0.0.0.0/)`, `… was not found in | failed to transfer from http://0.0.0.0/ during a previous attempt … update
  interval of <id> …` (the whole build log is searched, so a `http://0.0.0.0:8080` line of a plugin or an application is no
  evidence). The id
  `maven-default-http-blocker` alone is not a hit: a settings file may redefine that id as an ordinary mirror (real URL,
  `<blocked>false</blocked>`), and Maven then names it in every plain resolution failure, which goes to the normal handling.
  The repository lookup (`RepositoryLookup`) and the preflight treat a mirror as blocking by `<blocked>true</blocked>` or the
  `0.0.0.0` URL only, and drop a global mirror whose id the user settings redefine (Maven's merge). `http_blocked_reason(hit)` (one sentence) and `http_blocked_followup(hit, cfg)` (the Handover line:
  `Maven (3.8.1+) refuses the HTTP repository `<id>` (<url>) via the blocked mirror `<mirror>` — not a project or sbmig problem.
  Fix the Maven settings: (a) an https URL, or (b) in <settings file> (build.settings_file | default) exempt `<id>` from mirror
  `<mirror>` (append `,!<repository id>` to its `<mirrorOf>`), or add a mirror for `<id>` (same URL, `<blocked>false</blocked>`); blocker from <file>` —
  the file that defines the mirror, for the default blocker `global_settings_file(cfg)` or `<maven home>/conf/settings.xml`).
  Korean in `sbmig/report/i18n.py` PATTERNS.
- **Precedence**: `BuildFixLoop.http_blocked_abort(pr, phase, build_or_rewrite_result, iteration, where=…)` runs first —
  in `Orchestrator._step_rewrite` before `_align_asis_parent_and_retry` / `_check_parent_in_rewrite`, in `_step_verify` before
  `_verify_fix_loop`, and in the loop before `_parent_abort`, clustering, the pre-tries and the LLM. `MavenRunner.run` returns a
  blocked build without the file-lock / `-U` retries; `OpenRewriteRunner.run` returns a blocked run without the OOM / download /
  `-U` / runNoFork retries.
- **Outcome**: `loop.aborted` = the reason + `— run aborted: fix the Maven settings (see the Handover)` (one ERROR line), flag
  `aborted:http-blocked` (exit 3), journal `{"event": "maven_http_blocked", "phase", "iteration", "where", "mirror",
  "repositories": [{"id", "url"}], "settings", "log"}` + `abort`, ONE manual follow-up (the Handover line; no `run aborted: …`
  line), no BlockedItem. Finalize adds `infra:http-blocked` first (also when a failed final build shows the blocker without the
  loop) and then no `infra:repository`. `SBMIG-RESULT … exit=3 … attn=infra:http-blocked,aborted:http-blocked`. The Handover
  puts the line first (`handover.HTTP_BLOCKED_PREFIX`) and never cuts it; flag help in "Flags".
- **Preflight** (`Orchestrator._check_http_blocker`, real Maven only): `active_http_blockers(cfg, project_root)` merges the user
  settings (`build.settings_file`, else `~/.m2/settings.xml`) and the global settings like Maven (user mirrors first; a global
  mirror whose id the user settings redefine is dropped), selects the mirror per repository like Maven (mirrorOf equal to the
  repository id first, then the first pattern match) for every `http://` repository of the settings' active profiles
  (repositories + pluginRepositories) and the project poms, and returns those whose mirror is blocking → one WARNING
  `preflight: Maven's HTTP blocker covers <id> (<url>) (mirror … from …) …` + journal `{"event": "maven_http_blocker_active",
  "repositories": [{"id", "url", "declared", "mirror", "mirror_origin"}]}`. Never aborts: an artifact may never be requested
  from that repository.
- **Remediation** (`sbmig/remediations/maven.py`): a blocked baseline / candidate build gets the same Handover sentence as the
  first line of `BuildOutcome.summary` (and so of the remediation message); a blocked `dependency:tree` adds it to
  `ResolvedModel.notes` and skips the package-assisted second attempt; no `-U` retry.

## v2.1z-ai — case identity: signature rule versions, member symbols keyed by their owner, re-signing, the exact-key guard

A case is one L2 (`signature_generic`); its id is `sha1(L2)[:10]` (the folder name `cases/<case_id>/`). The id is a hash
of the whole L2, so matching is exact on the L2 text; the ten characters only name the folder.

- **Rule version** (`sbmig/build/signatures.py`): `SIGNATURE_VERSION` (int, now 2) names the rules `signature_for`
  applies; `SIGNATURE_CHANGES: {version: frozenset(error classes)}` lists, per version, the error classes (2nd L2 segment)
  whose L2 that version changed; `classes_changed_since(version)` = the classes that may differ between `version` and now
  (0 and 1 are the same rules). A rule change that alters a produced L2 raises the version and lists its classes.
- **Rules 2 — a member symbol is keyed by the library type that owns it.** Rules 1 keyed `cannot_find_symbol` by the simple
  name alone, so `keySet()` on `HttpHeaders` and `keySet()` on any other type were one case.
  - `cannot_find_symbol`: when the symbol is a member (`method …`, `variable …`, `constructor …`, `static …`) AND the
    `location` names a library-owned type — `variable <x> of type <T>` or `class | interface | enum | record | @interface <T>`
    with `<T>` under `EXTERNAL_PACKAGE_PREFIXES` / the registered prefixes — the L2 is
    `<kind>|cannot_find_symbol|<T>.<name>` (`<T>` = the qualified type without type arguments and array brackets, `<name>` as
    before). Otherwise the L2 is unchanged: `<kind>|cannot_find_symbol|<name>` (class-like symbols whatever their location —
    an import line and the usages of one missing class stay one case; members whose location is project-owned or absent).
  - `no_suitable_method`: the first candidate line of `raw` (`method <T>.<name>(…) is not applicable`,
    `method <T>.<…>name(…)`, `constructor <T>.<Simple>(…) is not applicable`) whose `<T>` is library-owned and whose
    member name is the one javac printed after `found for` gives the L2 `<kind>|no_suitable_method|<T>.<name>`
    (a project-owned candidate listed first is skipped); no such line → unchanged (`…|<name>`).
  - L1 is unchanged. Every other class is unchanged, so every id not in those two classes is the same under rules 1 and 2.
- **Stored facts.** `ErrorEntry` += `sig` (the rule version of its `l2`; 0 = stored before versions), `l1`, `symbol`,
  `location`, `artifact` (`g:a[:v]`), `goal` — filled from the `BuildError` when the entry is made, masked like `message` —
  and `was` (earlier case ids, oldest first). `CaseInfo` += `sig`, `aliases` (earlier case ids whose content moved here).
  A later rule version recomputes from these facts; entries without them are read from `raw` (`symbol:` / `location:` /
  candidate lines).
- **Re-sign** (`sbmig/errors/resign.py`; `CaseStore.resign(dry_run=False) -> ResignReport`): inside the KB lock, the
  records of `errors.jsonl` whose `sig` is older than `SIGNATURE_VERSION` AND whose class is in
  `classes_changed_since(sig)` are recomputed (a `BuildError` rebuilt from the stored facts, else from `raw`;
  `signature_for` + `refine_signature`; a recomputation that yields another class or no key, or lacks the facts the rule
  reads, keeps the stored L2) and stamped with the current version. Records of unchanged classes are not touched: their
  `sig` stays, which `classes_changed_since` reads as current. An entry whose L2 changed gets the new `case_id` / `l2`,
  its old id appended to `was`, the `cases/<old>/` prefix of its `script` rewritten; its `id` never changes (the
  import / record de-duplication key). Case folders follow the entries:
  - every entry of case O now belongs to one case N → the case moves: folder renamed (or merged into an existing N —
    scripts de-duplicated by content, a clashing number takes the next free one and the entries' `tries[].script_version`
    follow, runs merged), `case.json` gets the new `case_id` / `l2` / `sig`, `aliases` += O and O's aliases, human keys
    (`pin`) travel;
  - the entries of O split over several cases → each stored script version goes to every case one of whose entries has a
    try that ran it (`FixTry.script_version`), a version no moved entry ran stays with O (or, when O keeps no entry, with
    the case holding most of O's entries); counts and status of a version that was copied are recomputed from its rebuilt
    runs (an estimate: runs are capped at `errors.script_runs`); a `pin` is copied to every part;
  - a case without entries in `errors.jsonl` keeps its id. Nothing is merged by an `aliases` entry alone: an old id can be
    a valid key of the current rules again (`…|cannot_find_symbol|keySet` on a project-owned type) and a running pod may
    have just created that case.
  A case that cannot be read, or whose target id is held by another signature, is skipped: its entries keep their id and
  `sig`, it is not recounted, and `kb.json` names it under `skipped` (`{"<case id>": "<reason>"}`). The automatic
  triggers leave skipped cases alone; `sbmig kb reindex` tries them again; `sbmig kb validate` lists them.
  The library prefixes the owner check reads (`build.external_package_prefixes` + the in-house group ids) are registered
  from the store's config before anything is recomputed, as a build does: a re-sign from the CLI and a run give the same
  L2. A KB is therefore shared only between runs of one artifacts configuration.
  Order of writes: `<kb>/kb.json` with `"sig": 0` and `"resigning": [<affected ids>]` (a kill at any later step sends
  the next run through the whole procedure again), then new folders and scripts, then the `case.json` files, then
  `<kb>/resign-pending.json` (the folders to clean up), then `errors.jsonl` (one atomic rewrite by streaming copy:
  untouched lines byte for byte, an unparsable line kept in place; the torn prefix in front of a recovered record that is
  rewritten goes to `errors.jsonl.corrupt`) and the per-entry JSON files of the moved entries, then the emptied old folders
  (a script file is removed only when a byte-identical copy is in a target; `case.json` last; a folder holding files the
  KB does not know is kept with a WARNING), then `<kb>/kb.json` (`{"sig": <version>, "resigned_at": <iso>, "skipped":
  {…}}`, always written when the lock was held), then the recount of the affected cases and `cases.md`. Interrupted, it
  leaves duplicates at worst and the next run finishes the clean-up from `resign-pending.json` (copies are recognised by
  content, also files no `case.json` lists yet); `resign()` run twice changes no byte the second time. `dry_run` writes
  nothing and takes no lock. Only the lines of the affected cases are parsed (one regex pass finds them), so memory
  follows the affected cases, not the file (measured on a 103 MB / 32,000-line file: three older lines among current
  ones 0.9 s and 49 MB peak; everything old, 3,000 moves, 12 s and 190 MB). `ResignReport`: `version`,
  `entries_stamped` (recomputed records, moved or not), `entries_resigned` (L2 changed), `moves: [(old id, new id,
  l2)]`, `splits: [(old id, [ids])]`, `merged`, `cases_stamped` (`case.json` files rewritten), `unreadable`, `error`,
  `summary()`, `lines(limit=50)`; one INFO line `kb re-sign: case <old> → <new> (<l2>)` per moved case. Known limit: an
  export taken before a move and imported into the KB that already made it brings the old case folder back under its old
  id, without entries (a duplicate script, nothing lost).
- **When it runs.** `sbmig kb reindex` re-signs, then recounts everything (`--dry-run` prints what would move and writes
  nothing). Automatically, never failing a run: on the first `FileKnowledgeBase.match` of a process when `<kb>/kb.json` is
  missing or older while the KB has records (one small file read when it is current; once per KB path and process), and
  inside `record_run` when the file still holds a record signed by older rules whose class changed since and whose case is
  not in `skipped` (a pod of an older image wrote after the re-sign). `record_run` writes `kb.json` when it creates
  `errors.jsonl`, so a new KB never re-signs. `sbmig kb import` re-signs inside the merge, under the same lock, before its
  recount. A failure, or a lock not taken within `kb.lock_timeout_seconds`, is one WARNING; the run goes on with the KB as
  it is.
- **Exact-key guard.** A stored case whose `l2` is not the L2 asked for (two L2s with the same ten characters, or a
  hand-edited `case.json`) is not that case: `CaseStore.same_case(case_id, l2)` records the id as foreign for the
  process (`FileKnowledgeBase.match` asks it with every error's L2 before the ladder looks anything up); `best` /
  `latest` / `match` return nothing for a foreign id, `credit` and `record_run` write nothing into it (the entry still
  goes to `errors.jsonl`), `add_script` raises `CaseKeyMismatch` (an `OSError`: the loop's store adapter logs it and
  stores nothing), `kb import` does not merge into it. One WARNING per id and process: `case id <id> is held by another
  signature: stored "…" asked "…"`. `sbmig kb validate` lists a case whose `case.json` L2 differs from the L2 of entries
  stored under its id, and one line `N record(s) in M case(s) signed by older rules (version < V): run sbmig kb reindex`
  (the records a re-sign would move).
- **Stored fact sizes.** `symbol` / `location` / `l1` / `artifact` / `goal` are cleaned like `message` and cut at
  `FACT_CHARS` (2,000); a fact that was cut is not used for a recomputation (`raw` is read instead). Known limit: a
  `no_suitable_method` whose library candidate line lies beyond the stored `raw` (`errors.raw_chars`) keeps the bare key
  when it is recomputed.
- **Lookups by an old id.** `sbmig kb cases show | pin | unpin <id>` resolve an id found in a case's `aliases` to that
  case (a real folder with that id wins); `retire` / `activate` refuse an alias, because a merge may have renumbered the
  versions, and name the current id.

## v2.1z-aj — run flows (`full` | `remediation`), Ant projects refused with a notice, the page header

**Run flow.** A run is one of `RUN_FLOWS` (`sbmig/core/models.py`): `full` — the migration, followed by the chained
remediation when it ends BUILD SUCCESS and `remediation.after_migration` is on — or `remediation` — no migration; the
project is remediated as it was uploaded (already-migrated projects that are checked against IVAS again later).

- **Entry points**: `sbmig run <zip | dir> --flow full|remediation` (default `full`; another value is a usage error),
  `sbmig.api.migrate(..., flow="full")`, `Orchestrator.run(stop_after=None, dry_run=False, flow="full")`,
  `run_graph` / `stream_graph(orch, stop_after, dry_run, flow="full")`; `sbmig.loop.parse_flow` / `check_flow`;
  `Orchestrator(..., source=<the uploaded zip or directory>, remediation_components=None)` (without `source` the path is
  read from the workspace's `intake` journal line). `RunReport.flow` records it. `--stop-after` and `--dry-run` are
  refused together with `--flow remediation`.
- **`remediation` flow — what runs**: intake only as far as it changes nothing in the project (workspace, preflight, the
  build-tool check with its usual refusals: Gradle, no build file, Ant), then the remediation (`chain.run_only`), then
  the report. No analyze / plan / transform / restructure / review / verify / fix loop, no LLM call, no KB read or write,
  no intake step that edits the project (no generated aggregator, no extension stripping, no pom synthesis).
- **Source**: the uploaded ZIP's own bytes (`intake.accept_zip`); a directory input is zipped as it is. Build targets: the
  root pom when the tree has one, else what the chained flow would build for that tree (derived from a read-only look at
  the accepted tree). The remediation's own baseline build is the build gate: a project that does not build as uploaded
  ends `FAILED baseline_build_failed` before any scan.
- **Readiness**: `remediation.after_migration` is not consulted (the flow was asked for). `remediation.trusted_projects`
  is not consulted either (the run builds the upload exactly as a `full` run would). Missing IVAS values or Maven
  readiness → the run does not start: exit 3, flag `remediation:not-ready`, the missing names (never values) in the
  report and journal `remediation_skipped`.
- **Files**: under `<run dir>/remediation/`, exactly as a chained run (state id = run id), so status / proposals /
  approvals / resume in a new build pod work unchanged (`kind: chained` for the page).
- **Result**: `SBMIG-RESULT … flow=remediation … remediation=<STATUS>` (the `flow=` field is printed only for
  `remediation`). Exit code: 0 = `SUCCEEDED`; 2 = any other saved status (`INCOMPLETE`, `AWAITING_APPROVAL`,
  `AWAITING_CONFIRMATION`, `STOPPED`, `FAILED`, `INTERRUPTED`); 3 = not started (intake refusal, not ready).
  The run's `report.md` is short: one line saying the migration was not run, the remediation section, the path of
  `remediation/report.md`; no migration sections. `RunReport.success` = remediation `SUCCEEDED`.
- **Engine**: graph nodes `intake → remediation → finalize` (`sbmig.graph.REMEDIATION_NODES`; the page's stage rail
  follows the `node` events; an intake refusal goes straight to `finalize`); journal `{"event": "flow", "flow":
  "remediation"}` first. Of the intake only the setup hook, the config warnings, the preflight and the build-tool check
  run; no git snapshot, aggregator, de-duplication, extension stripping or effective-pom capture. `chain.run_only(run_dir,
  source, cfg, *, run_id, journal, stop, components=None, build_targets=())` raises `chain.RemediationNotReady` (with
  `.missing`) and nothing else. Build targets without a root pom: the directories of the reactor roots read from the
  accepted tree (`pom_parser.parse_poms`; a pom no other pom lists as a module), re-rooted onto the ZIP (`chain.zip_targets`).
  A ZIP whose project sits two or more folders deep is not accepted by this flow (`no_pom`). `sbmig fleet` runs `full` only.

**Web.** `POST /api/runs?flow=full|remediation` (default `full`; another value → 400; `project` is required as well: "v2.1z-be"). The flow is a per-run value like
`stop_after`: `RequestRecord.flow` (pinned at intake), Job env `SBMIG_FLOW` (`contract.FLOW_ENV`, in `contract.RUN_ENV`;
set by Main when it creates the Job, checked by the Worker against `request.json` like `SBMIG_STOP_AFTER`), `sbmig run
--flow remediation` in the Worker's command; in-process mode passes it to the orchestrator. `status()` and the run list
carry `flow`. Queue, slots and stop work as for any run. Nothing changes in the 81 template.

**Run ids and names.** (Since "v2.1z-be" an upload names its project and the id starts with THAT name; the slug rule
below is what a caller without a project name still gets, and what older run folders look like.) A web run's id is
`<slug>-YYYYMMDD-HHMMSS-<6 hex>` (`runs.run_id_for`): the slug is the uploaded
file's base name without `.zip`, lowercased, every run of characters outside `[a-z0-9]` turned into one `-`, trimmed, cut
to 24 characters (`RUN_ID_SLUG_MAX`: the Job name `sbmig-<id>` and the resume name `…-rem<attempt>` stay within the
63-character label limit); a slug without a letter (a Korean name, digits only) is `web`, the earlier shape. The run
directory is claimed by an exclusive `mkdir`. The name as uploaded (Unicode kept, control characters dropped, 255
characters) is `RequestRecord.upload_name` / `upload` in `web-status.json`; `status()` and the list carry it as
`upload_name`. `status()` also carries `abort_reason` (the first `run aborted: …` follow-up of `report.json`) and, when
`sbmig.report.i18n.tr_line` knows it, `abort_reason_ko`.

**Page** (`sbmig/web/static/index.html`): the header shows the name `panacea-remediating-oss` and no description line
(a temporary name; `<title>` the same); there is no footer. Every run is named by its uploaded file name (the run id small
under it). A run that was refused shows its reason as a sentence in the result area (`abort_reason_ko || abort_reason`). The upload card has two choices, 전체 진행 (default: 이관 후 취약점 조치) and 취약점
해결 (이미 이관된 프로젝트의 취약점 조치만); the start button and the stage rail follow the choice (전체 진행: as before;
취약점 해결: 접수 → 취약점 조치 → 마무리). A run of the `remediation` flow shows the remediation result block as its
result, and its download buttons are the remediation report and the remediation result ZIP. The run list names the flow.

**Ant.** `intake.ant_to_maven` is `false` by default (company decision 2026-11-12: no Ant → Maven conversion). An Ant
project (build.xml, no pom.xml) is refused at intake: exit 3, flag `unsupported-build-tool:ant`, and the abort reason is
`intake.ant_message` ("Maven 변환 후 신청 부탁드립니다.") — the ERROR line, the first line of the report and what the page
shows. The synthesis code (v2.1j, `sbmig/analyze/ant.py`) is kept and runs with `intake.ant_to_maven: true`; the
`ant-webapp` fixture sets it, so the tests and the real-Maven e2e keep exercising it.

## v2.1z-ak — framework projects run in the reduced scope `jdk-jakarta`

**Rule.** A project that depends on an in-house framework artifact (`framework.artifacts`) is not converted to the
company layout / Spring Boot 4. It runs in the scope `jdk-jakarta`: the Java 21 and jakarta recipes, the verify build
with the script fix loop, then the chained remediation. No restructure, no structure review; the project keeps its
parent pom, its module layout, its Spring line and the framework artifact's version. One run has one scope (an MSA in
which one service uses the framework is reduced as a whole).

**Config** (`framework`, `sbmig/core/config.py FrameworkConfig`; company values go into the company yml):

| key | default | meaning |
|---|---|---|
| `framework.artifacts` | `[]` | patterns naming the framework artifacts: `artifactId`, `groupId:artifactId`, `*` globs in either part; empty = no check |
| `framework.scope` | `jdk-jakarta` | `jdk-jakarta` = the reduced run; `full` = the evidence is reported, the ordinary migration runs |
| `framework.transitive` | `apply` | a framework artifact that only another library brings: `apply` = like a declared one; `note` = reported, full migration; `off` = not looked for (no extra Maven call) |
| `framework.recipe_sets` | `[jdk21, jakarta]` | the `openrewrite.recipes` sets a reduced run executes (any set name except `extra`; an unknown name is a config error). `jakarta` keeps its case rule; `servlet_jakarta_10` runs only together with the jakarta set |
| `remediation.inhouse_groups` | `[mandatory, optional, parents, framework]` | `framework` = the artifacts matching `framework.artifacts` |

`FrameworkConfig.matches(group_id, artifact_id)`, `matches_key("g:a…")`, `matches_jar(file_name)` (a jar without
coordinates: `<artifactId>[-<version>].jar`), `maven_includes()` (`-Dincludes` patterns) are the one matching rule every
part uses.

**Models** (`sbmig/core/models.py`): `FrameworkEvidence {source: direct | parent | bundled | transitive, artifact,
version, where, parent, via}`, `FrameworkUse {patterns, evidence, unchecked, scope: full | jdk-jakarta, reason}` with
`found`, `reduced`, `keys()`; `ProjectProfile.framework` (None when the check is off), `ProjectProfile.reduced_scope`;
`MigrationPlan.scope`; `FixContext.scope` / `scope_facts` (`sbmig/core/interfaces.py`).

**Detection** (`sbmig/analyze/framework.py`; `analyze_project` attaches the result, the orchestrator adds the
transitive step):

| source | where it is looked for | Maven call |
|---|---|---|
| `direct` | `<dependencies>` of every project pom (a `<dependencyManagement>` entry alone is not evidence) | none |
| `parent` | a module's effective `<dependencies>` in the as-is effective pom captured at intake (`logs/effective-depmgmt.json`) that no project pom declares: inherited from a parent pom outside the project | none (the intake capture) |
| `bundled` | a jar in the tree that no pom declares (`profile.bundled_jars`): by its coordinates, else by file name | none |
| `transitive` | `mvn dependency:tree -Dincludes=<patterns>` on the as-is tree; only when the three above found nothing, `framework.transitive` is not `off`, Maven is real and the run is not a dry run | one |

`detect_framework(ws, cfg, poms, bundled_jars, effective=None) -> FrameworkUse | None`, `transitive_evidence(ws, cfg,
profile) -> (evidence, why_not | None)`, `decide_scope(use, cfg)`, `summary_text(use)`, `framework_followup(use)` (the
Korean Handover line). A source that could not be read is named in `unchecked` (`parent: no as-is effective pom`,
`transitive: <why>`); nothing here stops a run. Scope rule: `jdk-jakarta` when `framework.scope` is `jdk-jakarta` and
there is `direct` / `parent` / `bundled` evidence, or `transitive` evidence with `framework.transitive: apply`; else `full`.

**Analyze step** (`Orchestrator._check_framework`): found → flag `framework-project` (not an attention code), journal
`framework_project {scope, artifacts, evidence, unchecked, reason}`, one operator log line, the Handover line; enabled
and not found → journal `framework_check {found: false, unchecked}`. `profile.json` carries `framework`; the `plan`
journal event carries `scope`.

**Plan** (`MigrationPlan.scope = "jdk-jakarta"`): `transform.rewrite[@svc]` with the sets of `framework.recipe_sets` —
by default the `jdk21` set and, unless the case is `pure_java` / `unknown` without Java EE javax imports, the `jakarta`
set (the standard jakarta recipe ends on the Jakarta EE 9 line, servlet API 5.0; `servlet_jakarta_10` adds the Servlet 6
removals) — and no set that is not listed (never `extra`);
`transform.jsp_jakarta` (kind `restructure`, phase `transform`, `params.mode = "jsp_jakarta"`: the JSP javax → jakarta
rename of `layout.jsp_jakarta_rename`, planned only with JSPs and the jakarta set); `verify.build[@svc]`. MSA: one
transform at the root when the root aggregator covers every target, else per target (library roots first). Never
`restructure.*`, `restructure.review`, `transform.compile`. A `full` plan is unchanged.

**Run.** Around each `transform.rewrite[@svc]` a reduced run makes the two corrections the restructure would have made
and that the upgrade itself causes (`Orchestrator._write_encodings_reduced`, `_restore_scopes_reduced`; neither fails
the step, dry run = note only, both idempotent across OpenRewrite retries and several transform steps):

| correction | when | why | code, switch | commit / journal |
|---|---|---|---|---|
| `project.build.sourceEncoding` written | BEFORE OpenRewrite | javac's default source encoding is UTF-8 since JDK 18: MS949 / EUC-KR sources without a declared encoding stop compiling, and OpenRewrite skips a file it cannot decode (its javax imports would stay) | `sbmig/restructure/source_encoding.py write_source_encodings`; `layout.legacy_context.write_source_encoding` | `transform: source encodings written`; `source_encodings {step, entries}` |
| as-is `provided` scopes restored | after a successful rewrite | the jakarta recipe turns `javax.servlet-api` `provided` into `jakarta.servlet-api` `compile`, which would put the servlet API into the WAR | `sbmig/restructure/scope_restore.py restore_asis_scopes`; `layout.legacy_context.restore_asis_scopes` | `transform: as-is provided scopes restored`; `scopes_restored {step, entries}` |

Commit order of a reduced run: `transform: source encodings written` → `rewrite: transform.rewrite` → `transform: as-is
provided scopes restored` → `transform: JSP javax → jakarta`. Both corrections produce the full flow's phase note and
pom-verify entries recorded with `seen = "transform"` (`ErrorList.add_pom_verify(file, note, seen="restructure")`; the
next build verifies them like the restructure's). An encoding the kept external parent already sets is read from the
as-is capture's root properties: a matching value leaves the pom alone, a conflicting one is a Handover line (the
parent's value is kept); a module with another external parent, or a parent that sets only the compiler plugin's
`<encoding>`, falls back to the in-project rule. Everything else the restructure corrects belongs to the parent swap
and is not done. The full flow's order is unchanged (its restructure writes the encoding after the rewrite).

`transform.jsp_jakarta` runs the rename directly (commit `transform: JSP javax → jakarta`, journal `jsp_jakarta`), not
through the restructurer. The as-is parent is never aligned to the configured company parent: a
parent pom Maven cannot resolve aborts a reduced run (`aborted:parent-not-found`, no LLM call) in the transform step and
in the fix loop alike. A reduced run that ends BUILD SUCCESS is a success (exit 0) and goes on to the chained
remediation like any other.

**Fix loop** (reduced run; the loop reads `profile.reduced_scope` / `profile.framework`):

- **Version facts** come from the project's own chain: `VersionResolver.for_workspace(..., reduced=True)` uses the as-is
  capture as both `asis` and `after`, without the company parent-chain facts and without the target Boot BOM. Order:
  the kept parent manages it → no version; else the as-is version; else the newest repository release where that is
  allowed. The prompt's fact lines read `parent chain: manages g:a at v` (no "new", no Boot BOM line).
- **The framework artifact is untouchable**: its keys (the evidence's g:a and any g:a the patterns match) are in
  `_protected_version_keys`; `sbmig-version`, `sbmig-jakarta` and the version fill skip it; `sbmig-parent` is not
  offered. `_framework_guard` runs before the commit of every script / edits attempt (not for sbmig's own pre-tries):
  each touched pom is compared with its text before the attempt — framework declarations in `<dependencies>` and
  `<dependencyManagement>` (g:a, classifier, type, version with the pom's own properties resolved) and the `<parent>`
  block (g:a:v, relativePath). A changed version, a removed declaration, a framework declaration added at a version the
  project never used, or any `<parent>` change fails the attempt: the tree is restored, journal `framework_guard`, and
  the next ask sees the sentence (prefix `framework project:`). Exclusions and `<scope>` are not compared.
- **Parents**: any `PARENT_RESOLUTION` error (company parent or not; a parent that is one of the project's own modules
  excepted) aborts without an LLM call: `aborted:parent-not-found`, a `parent_not_found` blocked item, the same reason
  sentence as the transform step (`Maven cannot resolve the parent pom <g:a:v>; a framework project keeps its own parent
  pom, …`). The company-chain build-extension check is off.
- **javax-era framework**: a case whose library is a framework artifact is blocked at once (`external_javax_api`, no
  pre-try, no LLM call) with a suggestion naming the framework's owner; a javax API jar that reaches the classpath only
  through a framework artifact gets the same suggestion in the final pass (`framework_via_suggestions`, after
  `fill_via`).
- **Prompt**: `FixContext.scope = "jdk-jakarta"`, `layout_facts = []`, `scope_facts` (≤ 1,200 characters) rendered as
  the section "Scope of this run" (never truncated) where "Project after the restructure" would be; the system prompt
  (`script_system_v1.md`, variable `scope`) has a reduced-scope introduction, rule 3 ending, rule 8 and rule 14 — the
  `full` rendering is byte-identical to the one before this increment; the verify guide's target reads `JDK 21 and
  jakarta.* only (framework project: …)`.

**Remediation.** A library that is a framework artifact, or has one on its dependency path, is a residual
`inhouse_artifact` (group `framework`, label 사내 프레임워크): never a proposal, never a `managed_override`.
`sbmig/remediations/inhouse.py`: `inhouse_keys(cfg)` returns `InhouseKeys` — the exact `g:a` keys of the yml groups as
dict entries plus the framework patterns, which `in` / `get` / `group_of` understand (an exact key of another group
wins; iteration and `len` see the exact keys only); `framework_patterns(cfg)` = the patterns when the group is listed.
The policy hash gets the field `inhouse_framework` only when patterns exist (a configuration without patterns keeps its
hash). `remediation/report.json` marks such a group with `"group": "framework"`; `report.md` titles it
`(사내 프레임워크)` and adds one sentence that the framework version is not changed.

**Report / result line / page.** `SBMIG-RESULT … [ major=<n>][ scope=jdk-jakarta][ flow=remediation][ remediation=…]`
(the `scope=` token only for a reduced run; read from `plan.scope`, else `profile.reduced_scope`). `report.md`: the
header row `범위`, the block `## 사내 프레임워크 프로젝트` right after the header table (one line per evidence entry, what
the run did — build error fixes and the remediation only when they happened — and did not do, the `unchecked` sources),
or `## 사내 프레임워크` with one sentence for a run that found the artifact and ran in full by configuration. In a
reduced run the To-be values come from the final poms only (no target Boot fallback); the error list has the group
`transform — pom corrections by sbmig, verified by the next build`; the header row counts sbmig's pom
corrections apart from build errors; the Handover has the subsection `pom corrections by sbmig` (sbmig가 고친 pom); a
version row's reason is `recipe` when no fix attempt ran. "Versions replaced" leaves out a row whose before and after
are equal (any scope). The blocked table translates the three framework suggestions and the
page translates the abort reason. The page shows the label 프레임워크 프로젝트 and the sentence "JDK 21과 jakarta만
적용합니다. 재구성과 구조 검토는 하지 않습니다." from the journal events `framework_project` / `plan` (scope
`jdk-jakarta`), and draws 재구성 / 구조 검토 as skipped steps (하지 않음).

**Fixtures and e2e.** `fixtures/libs/example-fw` (stand-ins `example-fw-core`, `example-fw-web`, `example-fw-parent`,
installed by `fixtures/libs/install.sh`); `fixtures/projects/fw-direct`, `fw-parent`, `fw-transitive` (reduced runs) and
`fw-transitive-note` (`framework.transitive: note`, the full migration). `scripts/e2e.py` expectations added for them:
dotted keys into nested journal values (`evidence.0.source`), `regex:` values, `journal_events_absent`, `phases:
"absent"`, an exact `version` in `pom_dependency_versioned`.

## v2.1z-al — the Maven local repository and settings.xml live at /data/.m2 on the data volume

The user (2026-11-17): "/data/.m2에 뒀으면 좋겠어 일관되게" — the Maven local repository cannot live in the pod, and everything
else that persists is already under `/data`; right after: "Settings도 저기로 옮겨가야하지않아!?" — `/data/.m2` is to the pods what
`~/.m2` is on a PC: `settings.xml`, `repository/`, `repository-slot2/`, `repository-slot3/`; and on reading the operator steps:
"configmap 에 설정되어있으면 파드 배포할때 자동으로 나가는게 아니야?" and "어차피 … 웹 파드 재배포 하면 복사 필요 없는거 아냐? 만들어지는거
아니야?"; a ConfigMap mount nested in `/data` then needed a mount point, and the initContainer that made it hit a file owned by
another user. The user, finally: "어차피 /data 볼륨 마운트를 하는데 … 하나 마운트 된곳에다가 다같이 쓰면 안되는거야?" and "어차피
settings.xml은 바뀔일이 거의 없잖아? 일반 파일로 두고 가는것도 좋을거 같은데!" — so settings.xml is a plain file on the data volume.

- **Manifests.** `SBMIG_BUILD__LOCAL_REPO: "/data/.m2/repository"` in 40, 80 and 81 (was `"null"` = Maven's `~/.m2/repository`).
  40 and 81 have no `m2` volume any more: the repository is a folder of PVC `sbmig-data`, and PVC `sbmig-m2` is gone from
  `10-storage.yaml`. Not the old PVC mounted at `/data/.m2`: a mount nested in the NFS data volume needs the runtime to create the
  mount point on NFS as root, which a root-squashing export can refuse (the pod does not start), and with `nfs-csi` both PVCs were
  folders of the same export (no space isolation). A comment in `10-storage.yaml` keeps the way out when `.m2` on NFS is too slow:
  its own volume mounted at `/data/.m2` in 40 and 81 alike. 80 has no data PVC: its `m2` volume (emptyDir, PVC optional) is
  mounted at `/data/.m2`, so the path is the same in every pod.
- **settings.xml.** `SBMIG_BUILD__SETTINGS_FILE: "/data/.m2/settings.xml"` in 40, 80 and 81 (an env pin: the same literal in 40 and
  81). In 40 / 81 it is a PLAIN FILE on the data volume, like `~/.m2/settings.xml` on a PC: nothing is mounted inside `/data`,
  no initContainer, no mount point. 81 mounts no settings at all (its `config` volume + `artifacts.yml` mount are commented lines).
- **Source and copy.** ConfigMap `sbmig-config` keeps the reviewed content (passwords as `${env.NEXUS_USER}` /
  `${env.NEXUS_PASSWORD}`, values in the Secret); 40 mounts it read-only at `/etc/sbmig/settings.xml` (outside `/data`) and the
  operator copies it once: `kubectl -n agent exec deploy/sbmig -- sh -c "mkdir -p /data/.m2 && cp /etc/sbmig/settings.xml
  /data/.m2/settings.xml"` — as the pod's user, so folder and file belong to that user (Maven creates `repository` beside it).
  To change it: edit `sbmig-config`, apply 20, restart the web pod, run the same line. sbmig has no copy code. 80 (no data PVC,
  nothing survives between Jobs) keeps mounting the ConfigMap file with `subPath` at `/data/.m2/settings.xml` inside its emptyDir.
  `/data/.m2` must be writable by uid 1000 (check `id; ls -lnd /data/.m2` in the pod; a folder made by another user is removed or
  chowned from the shell that made it).
- **Preflight `settings-not-found`.** `Orchestrator._preflight` first checks `build.settings_file`: set (not empty / `null`) and
  not a readable file at the absolute path Maven would get as `-s` → flag `settings-not-found` (+ `infra:maven`), journal
  `preflight {settings_file: "missing (<path>)"}`, no Maven call; `_step_intake` aborts with
  `Maven settings file not found: <path> (build.settings_file) — put the settings.xml there and rerun` (exit 3). Both flows
  (the remediation flow's intake runs the preflight before `chain.run_only`, so its own readiness line `server settings.xml not
  found (<path>)` is never reached for this). Korean (`sbmig.report.i18n`): `Maven settings.xml을 찾지 못했습니다(<path>). 이 경로에
  settings.xml을 넣은 뒤 다시 실행해 주세요.` An EMPTY file (created but never filled) is the same flag with
  `Maven settings file is empty: <path> (build.settings_file) — copy the settings.xml there and rerun` (journal `settings_file:
  "empty (<path>)"`; Korean `Maven settings.xml이 비어 있습니다(<path>). 이 경로에 settings.xml 내용을 넣은 뒤 다시 실행해 주세요.`).
  Unset = Maven's default settings as on a PC, unchanged. The web pod logs one WARNING at startup
  (`RunManager._warn_missing_settings_file`, not in `local-job`): `Maven settings file <path> is missing — copy settings.xml there
  before the first run (deploy/README.md §3)` / `… is empty — …` (same tail); never fatal.
- **Pins.** `env_pins` hash the VALUE of `SBMIG_BUILD__SETTINGS_FILE` (the path), `request.json` digests the config files only;
  the content of settings.xml is pinned nowhere (it was not with the ConfigMap either). Both pods read the same file on the
  shared volume, so nothing changes there.
- **Slots** follow the v2.1z-ae rule: `/data/.m2/repository-slot2`, `-slot3`. The env pin of `SBMIG_BUILD__LOCAL_REPO` is now a real
  path, the same literal in 40 and 81; the slot override still happens in the child's environment after the pin check.
- **Image.** `VOLUME ["/data"]` only; `/data` is created and owned by uid 1000 in the image, so `/data/.m2` can be created without
  a mounted volume too.
- **Worker message.** The pod-local fallback WARNING names "the shared volume that holds build.local_repo (ownership / fsGroup of
  that folder)" instead of the old PVC; `POD_LOCAL_REPO_NAME` (`sbmig-m2-slot<k>`) is only a temp-folder name and stays.
- **local-job.** Both template values are container paths; `LocalJobClient._env_of` passes only `contract.RUN_ENV`, so a
  developer-PC Worker gets neither (`tests/web/test_local_job_mode.py`).
- **Operator steps** for an existing deployment (the manifest lines, `sbmig-job-template`, apply 40 + restart, the copy line):
  `deploy/README.md` §3 "Maven 설정과 로컬 저장소 — `/data/.m2`". Tests: `tests/deploy/test_manifests.py`,
  `tests/loop/test_preflight_settings_file.py`, `tests/web/test_settings_file_warning.py`, `tests/report/test_abort_reasons_ko.py`.

## v2.1z-am — the remediation says where a library comes from

The user, after reading a real report from the company cluster: the 조치 완료 and 사내 라이브러리 sections read well; five
things did not — "루트 pom을 상속하지 않는 모듈이라는게 뭔지 모르겠어", "일부 모듈은 사내 라이브러리에서 가져옴. 이것도 결국 사내
라이브러리에 속해야 하는거 아니야?", "얘도 좀 어디서 발생 되었는지 알아야 조치를 할거 같아!", "IVAS에서는 2.2.0.release의 버전을
보고했는데 메이븐은 2.2.0.RELEASE를 사용해서 남겨뒀다는데 이건 upper건 lower건 적용하게 해줘야", and "classifier가 붙은 의존성은
뭐야?".

- **Matching ignores case.** One helper decides equality wherever an IVAS-reported `(g:a, version)` meets a Maven-resolved one
  (inventory, the rescan evaluation, every comparison): exact first, else `str.casefold` on the version and — only when no exact
  g:a exists — on the g:a (Maven compares qualifiers case-insensitively). Findings that differ only by case are one group. Items,
  proposals and residuals carry **Maven's** spelling in `ga` / `version` (pom edits and the resolution check look for that text);
  the IVAS spelling is kept in `InventoryItem.reported_ga` / `reported_version` and `Residual.reported_version` when it differs.
  A fixed library reported with another case is not "new". `report.md` counts and scores rows against findings case-insensitively.
- **The in-house split is per module.** When only some modules of an item get the library through an in-house artifact (same
  per-module rules as before: the `parents` group's unambiguous-provenance rule, the `policy.pins` exception), those modules form
  their own item → residual `inhouse_artifact` naming them and that artifact; the remaining modules form the other item and are
  planned as if the in-house modules did not exist (proposal, override or residual by their own facts). `Residual.inhouse` is
  non-empty only on `inhouse_artifact` residuals; a split library left for review is two residual rows, one per section.
  `counts` (`graph.fill_counts`): `baseline_findings` / `final_findings` count scan rows, so a finding of a split library
  counts once; `fixed` = baseline findings missing from a complete final scan; `residual` counts residual rows (a split
  library = two); `residual_inhouse` the `inhouse_artifact` rows; `proposals*` approval units (a variant override is its own).
- **`ModuleSource`** (`sbmig/remediations/models.py`): how ONE module gets the library — `module`, `how` (the module's own
  origin code), `via` (g:a:v path, the module's own dependency first, the library itself not included), `declared`
  (`PomLocation` where a project pom declares the dependency for this module, or none), `manager` (parent / BOM g:a:v that sets
  the version), `inhouse` (in-house artifacts on this module's path), `variant` (`""` = plain jar, else `"<type>/<classifier>"`).
  `InventoryItem.sources` holds one per module of `modules` (also the merged `also_modules`), `InventoryItem.variants` the
  `"<type>/<classifier>"` of the resolved dependencies (`[]` = plain jar only); `Residual.sources` copies the item's. Every new
  field defaults to empty, so a `state.json` written before loads and renders as before.
- **Variant-aware override entries.** Maven's management key is `groupId:artifactId:type[:classifier]`, so an entry without the
  classifier does not govern a dependency declared with one. A `managed_override` entry carries the resolved dependency's `<type>`
  (when not `jar`) and `<classifier>`: `<dependency><groupId/><artifactId/><version/>[<type/>][<classifier/>]</dependency>` — ONE
  proposal per management key (g:a + type + classifier) that resolves at the vulnerable version, each its own insert at the
  same anchor, a proposal of its own (decided on its own when approval is switched on, v2.1z-ar); still one fixed shape, still
  kind `managed_override`; such a proposal's note says why the entry names the type / classifier; the plain jar's entry text is unchanged byte for byte. The existing-entry check, the
  "declared version wins" check and the resolution check after the build compare the full key. `apply.locate_override` takes the
  target pom as a parameter (default the root pom). The residual reason `variant` remains for what still cannot be overridden.
- **Report (`report.md`, Korean).** Under every table of 사내 라이브러리에서 발생한 취약점 and 직접 확인이 필요한 취약점 a list
  "들어온 경로:" says per row how the library arrives: a direct declaration "`back/pom.xml` 123행에 직접 선언", a transitive path
  "`a:b:1` > `c:d:2` 경로로 가져옴", a managed version "`g:parent:1.2.3`에서 버전 지정" (with the declaring line when known), a
  variant "classifier `jakarta`" / "형식 `test-jar`", several (`ModuleSource.variant` "jar/, jar/jakarta": the plain jar listed
  first) "사용 형태 2가지(일반 jar, classifier `jakarta`)" — a raw `jar/` is never printed. The source is printed once for the row when every module shares it, else one
  sub-item per group of modules with the same source (`` `app-a`, `app-b` 모듈: … ``). In the 사내 section, whose table has
  the `의존 경로` column, only rows whose modules differ or whose source has a declaration line / variant get an entry. When a
  module source's `how` differs from the row's origin, the origin is not lifted above the table ("아래 항목은 모두 …") and
  that row's 구분 cell reads "모듈마다 다름 (들어온 경로 참고)". Rows without `sources` get no entry, and a
  table without any gets no list (old states render as before apart from the wording below). A row whose `reported_version` is
  set shows Maven's version and once `(IVAS 표기: <reported_version>)`. The note "일부 모듈은 사내 라이브러리에서 가져옴" is gone.
  When an item was split, the result block adds "그중 N건은 모듈에 따라 사내 라이브러리 담당자와 프로젝트 양쪽에서 조치해야 해서
  두 절에 모두 나옵니다." and counts that finding once against the remaining total.
- **Wording.** The group `not_inheriting_root` is titled "모듈 pom에 직접 지정해야 하는 버전" and says, without Maven inheritance
  words, why a version line in the project's top pom.xml does not reach these modules (they take their settings from the
  company parent pom), that no module-pom proposal was made (`remediation.module_overrides` off, v2.1z-an) and what to do by hand (in the module's own pom.xml: a dependencyManagement
  entry with the fixed version, or a higher version of the dependency that brings it — 들어온 경로 shows which). The group
  `variant` starts with what a classifier is ("같은 버전의 jar를 용도별로 구분하는 이름입니다(예: jakarta)"). Stored data and the
  API keep the English sentences and codes; `sbmig/remediations/i18n.py` translates for display, and a new English sentence needs
  a `ko_text` rule and a sample in `tests/remediations/test_i18n.py`. Operator text: `agent-doc/12-remediation-report.md` (2절
  "들어온 경로", 3절, 5절), `agent-doc/11-remediations.md`. Tests: `tests/remediations/test_report_sources.py`, `test_report.py`,
  `test_i18n.py`.

## v2.1z-an — override proposals in a module's own pom

In the company layout the modules inherit the company parent poms, not the project's root pom, so an override entry in the
root pom reaches none of them. The tool proposes the entry in the module's own pom. Whether such a proposal is applied on its
own or waits for the owners' choice on the approval screen follows the one approval rule (v2.1z-ar: applied on its own with
the shipped configuration).

- **Which libraries.** Exactly those that reach the override route (`planner._override_change`): origin transitive or
  parent- / BOM-managed, not in-house (in-house items never get there), no editable version text. Per module of the item: a
  module inheriting from the root pom → the root entry (one proposal for all such modules, as before); a module that does not
  → an entry in **that module's own pom** (`<module dir>/pom.xml`). One proposal per (target pom, management key g:a + type +
  classifier). `Proposal.modules` = the modules that entry governs, `Proposal.sources` = their `ModuleSource`s (not in
  `proposal_hash`; empty for an older state).
- **Per-pom checks.** Everything the root route checks is checked per target pom: an existing `<dependencyManagement>` entry for
  the key in that pom → the existing-entry route (its version text changes; the note names that pom); a version declared on
  the dependency in the module's in-project chain → residual "a declared version wins"; a pom that cannot be read or edited
  safely (DTD / entities, no safe insertion line) → residual `cannot place …` for that module only ("the pom.xml of module
  `<m>` is not readable"). The target version and its evidence are chosen once per item.
- **Approval.** Kind and risk `managed_override` (+ `major` when it applies); `requires_approval` by the rule of v2.1z-ar.
  When approval is switched on for that class the run saves `AWAITING_APPROVAL`; the owners decide at once or after their
  own review (the state waits; the resume runs in a new build pod). Checked = applied; unchecked = `rejected`: not applied,
  not proposed again within that run (residual `rejected_proposals`, "… was rejected in an earlier decision"), and proposed
  again by a later run.
- **Apply and verify.** `apply` inserts the fixed-shape entry (or the section holding it) into any project pom the plan names —
  never a pom outside the project; the structure check after applying runs per target pom. `verify_resolution` requires
  from → to in each proposal's governed modules only. An inserted entry that did not change the resolved version (a
  dependency declared with a version, in that pom or in a parent, wins over `<dependencyManagement>`) is removed again, the
  candidate is rebuilt once and the rest of the round is kept; the proposal becomes a residual under 직접 확인 with its path
  ("the override entry in `<pom>` did not change the resolved version of `<ga>` …; the entry was removed again"). Stored facts:
  the proposal's plan `decision` becomes `"ineffective"`, it is not in `rounds[r].applied`; one residual (`manual_review`) per
  (g:a, version) naming every pom whose entry was dropped ("the override entries in `<pom1>`, `<pom2>` … were removed again"),
  with the proposals' module sources; journal `override_ineffective {round, proposal, pom, ga, modules}` per entry; a later
  round of the run does not propose that pom's entry again ("… had no effect in an earlier round of this run; not proposed
  again"). Conditions: only inserted entries are dropped; any other verification problem, or a second failure after the
  rebuild, keeps `FAILED resolution_mismatch` (message suffix " | after removing the ineffective override entr(ies) …", or
  " | the candidate without … could not be rebuilt: …"); a stop request or no time left → `FAILED resolution_mismatch` with
  " | the ineffective override entr(ies) … were not removed and rebuilt: …"; nothing left to apply → the round ends like a round
  with nothing applied (INCOMPLETE). In `report.md` the residual is the 직접 확인 group "버전 지정을 넣어도 바뀌지 않는 버전" with
  its 들어온 경로, and the record shows the decision as "효과가 없어 다시 뺌" (neither applied nor "선택하지 않은 변경"). A
  version TEXT change without effect is still `resolution_mismatch` and fails the round.
- **Switch and hashes.** `remediation.module_overrides` (default `true`): `false` = no module-pom proposal; a module outside the
  root's reach leaves the residual "module(s) … do not inherit from the root pom.xml" and the plan is byte-identical to the one
  made before this increment. The switch enters `policy_hash` only when `false` (a default configuration keeps its hash); a root
  proposal keeps its `proposal_hash`.
- **Report (`report.md`).** 조치 완료 and 승인 대기: `managed_override` proposals that carry module sources or edit a module pom are grouped per
  (library, from, to) — one table row whose "수정할 곳" lists every pom ("최상위 pom.xml" for the root), then under the library one
  line per pom: "`app-back/pom.xml`에 버전 지정 추가 (제안 `p-1-2`) · 항목 `g:a:2.1` classifier `jakarta` · `app-back` 모듈: … 경로로
  가져옴". A root proposal of an older state without sources keeps its one-row form. The result block and the first step of
  해야 할 일 ("적용할 변경 고르기") say to open the run's page, check the changes and press the apply button (「선택한 N건 적용」), and that nothing is applied before
  that, so the owners may review first. A new section "선택하지 않아 적용하지 않은 변경 — 제안 N건" (after 승인 대기) lists the
  rejected proposals with the same facts as the owners' choice, not a failure: "조치 화면에서 선택하지 않은 변경입니다. 이번 실행에서는
  다시 제안하지 않습니다. 검토한 뒤 조치를 다시 실행해 선택하거나, 아래 내용을 해당 pom.xml에 직접 추가할 수 있습니다."; 요약 gets
  the row "선택하지 않은 변경", 해야 할 일 "선택하지 않은 변경 검토하기 (N건)", the record's decision column "선택하지 않음". The
  group `not_inheriting_root` now says the module-pom proposal is switched off; `rejected_earlier` is "선택하지 않은 변경" ("이번
  실행에서는 다시 제안하지 않습니다. 조치를 다시 실행하면 다시 제안합니다."); the planner sentences that named "the root pom.xml"
  (`adds a <dependencyManagement> entry … to <pom>`, `uses the existing … entry of <pom>`, `<pom> already has …`) are translated
  for a module pom too. The approval screen (`sbmig/web/static/index.html`) groups the proposals per library (+ type + classifier)
  with a group checkbox, says per row `app-back/pom.xml에 버전 지정 추가` (root: `최상위 pom.xml에 버전 지정 추가`) and 들어온 경로,
  has `전체 선택` / `전체 해제`, a notice that nothing is applied until the apply button `선택한 N건 적용` is pressed (the run waits
  in 승인 대기), and beside the button `체크하지 않은 항목은 적용하지 않고 진행합니다.` Operator text:
  `agent-doc/12-remediation-report.md` ("승인 대기 읽는 법"), `agent-doc/11-remediations.md`. Tests:
  `tests/remediations/test_report_module_overrides.py`, `test_i18n.py`.

## v2.1z-ao — the admin page: token usage per project, log download

The user: "현재까지 프로젝트 별로 사용된 토큰량 볼 수 있는 페이지가 필요한데, 하드코딩으로 [a fixed password]으로 접속 할 수 있는
url하나 fastapi에 붙여서 해당 비밀번호로 접속해서 볼 수 있도록 하나 하자. 그리고 총 로그들 다운로드 받을 수 있도록도 하고!" — one
URL on the existing app, behind a fixed password, no manifest change. Code: `sbmig/web/admin.py` (router, auth, aggregation, ZIPs),
`GET /admin` in `sbmig/web/app.py`, the page `sbmig/web/static/admin.html`. Tests: `tests/web/test_admin.py`.

- **Routes.** `GET /admin` = `static/admin.html` (404 text "admin page not installed …" when the file is missing; the page shows
  its own login form after `GET /admin/api/session`). `POST /admin/api/login` body `{"password": "…"}` → 200
  `{"authenticated": true}` + cookie, 401 `{"error": "bad_password"}`, 429 `{"error": "too_many_attempts", "retry_after_s": n}`
  (+ `Retry-After`). `POST /admin/api/logout` → `{"authenticated": false}`, the session is forgotten, the cookie deleted.
  `GET /admin/api/session` → `{"authenticated": bool}`. `GET /admin/api/usage`, `GET /admin/api/logs.zip`,
  `GET /admin/api/runs/{run_id}/logs.zip` → 401 `{"error": "login_required"}` without a valid session (never 403);
  `GET /admin/api/session` always answers 200 once the site guard let the request through, so a 401 / 403 there is the site
  guard's. The main page does not link to `/admin`; the admin page links a run as `/#<run id>`.
- **Auth.** Built-in password = `admin.ADMIN_PASSWORD_PBKDF2` (PBKDF2-HMAC-SHA256, 200,000 iterations, salt + hash in hex; the
  plain text is nowhere in the repository); a non-empty `SBMIG_ADMIN_PASSWORD` (web pod, Secret `sbmig-llm`) replaces it —
  not a config override, not an env pin, a secret name for the redaction (`contract.is_secret_name`: `PASSWORD`), dropped from a
  `local-job` child's environment (`localjob.WEB_ONLY_ENV`), absent from the 81 template. Comparison with
  `hmac.compare_digest`; PBKDF2 runs in a worker thread. Session = random token (`secrets.token_urlsafe(32)`) in the process's
  memory, 12 hours; cookie `sbmig_admin`, HttpOnly, SameSite=Strict, Path=/admin, Max-Age 43200, `Secure` only when the request
  came over https (the intranet serves plain http). A web-pod restart logs everybody out. Failed logins: each waits 1 s
  (`asyncio.sleep`, `AdminAuth.fail_delay_s`); 5 failures from one `request.client.host` within 10 minutes → 429 for every
  further attempt (the right password too) until the oldest of the five is 10 minutes old; a success clears the address's
  count; per process. Behind a Kubernetes Service every browser may share one address — the limit is then global. The attempted
  password is never logged. The site guard (`TokenGuard`, `SBMIG_WEB_TOKEN` / `SBMIG_WEB_OPEN`) is unchanged in front: with a
  site token the admin routes need it AND the admin cookie; `/admin` is a page path of the guard (`?token=` → 303 to `/admin`,
  denial as HTML), like `/`.
- **Usage (`GET /admin/api/usage`).** Read from disk at request time, no cache: every real directory directly under the output
  root (`RunManager.out_dir`) whose name matches `RUN_ID_RE` and does not start with `_` / `.`; symlinks are never followed (the
  folder, and any file inside a run folder reached through a symlink, are skipped). A folder is a run when it has
  `control/request.json`, `report.json` or `web-status.json`, or the manager holds it in memory (an in-process run). Files are
  read as plain JSON with `.get` (an older / newer report never fails the page); an unreadable / half-written request.json or
  report.json sets `"partial": true` and the run is listed with what could be read. Shape:
  ```json
  {"generated_at": "ISO", "out_root": "/data/out",
   "totals": {"projects": 0, "runs": 0, "calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
              "cost": null, "currency": null, "unpriced_runs": 0},
   "by_model": [{"model": "…", "calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost": null,
                 "served": ["…"], "unnamed": true}],
   "projects": [{"project": "…", "runs": 0, "calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                 "cost": null, "unpriced_runs": 0, "first_run_at": "ISO", "last_run_at": "ISO",
                 "run_list": [{"run_id": "…", "accepted_at": "ISO", "flow": "full|remediation",
                               "scope": "full|jdk-jakarta|null", "status": "queued|running|finished|unknown",
                               "result": "ok|incomplete|aborted|null", "calls": 0, "input_tokens": 0, "output_tokens": 0,
                               "total_tokens": 0, "cost": null, "by_model": [], "usage_known": true,
                               "partial": false, "has_logs": true}]}]}
  ```
  Project = the entered project name when the run has one (`admin.project_of`), else the uploaded file name without its last extension (`upload_name`, else `upload`, else the manager's / web-status
  upload), else `report.project_name`, else the run id's slug (`<slug>-YYYYMMDD-HHMMSS-<hex>`), else the run id. `accepted_at`
  = request.json, else `report.started_at`, else the folder's mtime. `status` = `RunManager.status()["state"]`; `result` = the
  result line's words for the exit code (0 `ok`, 2 `incomplete`, 3 `aborted`, another code `exit <n>`; from the manager /
  `job-status.json` / `web-status.json`, else computed from the report like `report_exit_code`), null while unknown. `scope` =
  `report.plan.scope`, else `jdk-jakarta` when `report.profile.reduced_scope`, else null. A run's numbers come from
  `report.llm_usage` (the report's own total, so `calls` also counts calls that returned no usage), its `by_model` from
  `report.llm_usage_by_model`. **A run without a readable report** (running, or stopped before the report) counts the
  `llm call provider=… model=… in=… out=… latency=… finish=…[ served=…]` lines of its `sbmig.log` (one per answered call of the built-in LLM clients:
  fix, structure review, plan advisor, verify guide; read line by line), else the `llm_script` / `llm_verify_guide` events of
  `journal.jsonl` (fix loop only); nothing found → zeros and `"usage_known": false`. **Cost**: a run's `cost` = `llm_cost.total`
  when the report priced every model it used (no `unpriced_models`, no `usage_missing`); a run with known zero usage costs
  0.0; otherwise null. A sum (project, model, totals) is null as soon as one run in it is null — never a partial sum — and
  null when the priced runs use different currencies. `totals.currency` = the currency every priced run shares, even when
  `totals.cost` is null (the page labels each priced row with it); null when nothing is priced or the currencies differ.
  `unpriced_runs` (totals and each project, an integer) = how many runs in that sum have `cost: null`. Order:
  projects by `total_tokens` descending (then name), `run_list` newest first, `by_model` by `total_tokens` descending.
  **Model names (v2.1z-bg).** `model` is the asked name (the agent gateway's role); `served` (only when known) the models
  the gateway reported; `unnamed: true` (only on that row) marks the one row that collects old runs' `unknown` / `?` /
  empty names, shown as 「이름 없음」. A run's `by_model` (in `run_list`) has the same entry shape; the page shows it in a
  모델 column. Old names are recovered from `sbmig.log`'s `params.role=` request lines only on an exact match (see
  "v2.1z-bg" As coded).
- **Log ZIPs.** Members per run, as `<run id>/<file>`: `sbmig.log`, `journal.jsonl`, `control/request.json`,
  `remediation/remediation.log`; never raw LLM records, `result.zip` / `source.zip`, `logs/`, `report.md`. Why each is safe:
  `sbmig.log` / `journal.jsonl` are redacted line by line as written when `SBMIG_REDACT_OUTPUT=1` (`LIVE_FILES`, the run file
  route's rule); `control/request.json` is written once, atomically, by Main at intake and holds names, digests and the sbmig
  version only; `remediation/remediation.log` is redacted only by the final pass, so it is left out while ANYTHING may still
  write that run's remediation — the remediation API's own rule object `RemediationManager._withheld` (the migration run before
  its final pass, or a remediation resume queued / creating / running, in memory or recovered from
  `control/remediation-resume.json` after a web-pod restart); the router takes it from `app.state.remediations` (or the
  `remediations` argument of `create_admin_router`); no rule reachable or an error → left out. A run without `sbmig.log` /
  `journal.jsonl` / `remediation.log` is left out; nothing left → a valid ZIP holding only `README.txt`. No serve-time
  redaction exists for the web's files, so the bytes are the files'. Names: all runs `sbmig-logs-YYYYMMDD-HHMMSS.zip`
  (server local time), one run `sbmig-logs-<run id>.zip`. The one-run route picks the folder from the root's own listing
  (never a path built from the URL): an id that is not a real run folder → 404 `unknown run`.
- **Log ZIP limits (v2.1z-ap).** The temp file `sbmig-logs-*.zip` lives in the pod's LOCAL temp dir (`admin.zip_tmp_dir()` =
  `tempfile.gettempdir()`, `/tmp` in the container), never on the shared volume and with no fallback to it; it is streamed in
  64 KiB chunks with `Content-Length` and deleted in `finally` (`TempFileResponse`: finished, client gone, error). There is no
  periodic sweep any more; at startup `create_admin_router` removes, once, the files matching `ZIP_TMP_NAME` that an earlier
  version left in `<out root>/_admin/` (regular files only, never through a symlink, nothing else; the folder itself only when
  it is then empty). One archive per process (`admin.ZIP_GATE`, held from the size check until the temp file is deleted, so at
  most one temp file exists): another request meanwhile → 409 `{"error": "busy"}` at once (not queued). Before building, the
  members are planned with their sizes (`plan_logs_zip`): a single file over `ZIP_FILE_LIMIT` (512 MiB) is left out and listed
  in a `SKIPPED.txt` member (`<arc name>\t<bytes>`); uncompressed total over `ZIP_TOTAL_LIMIT` (2 GiB) → 413
  `{"error": "too_large", "bytes", "limit_bytes"}`; free space of the temp dir (`shutil.disk_usage`) below the worst-case
  archive (total + deflate / header overhead) + `ZIP_FREE_FLOOR` (1 GiB) → 507 `{"error": "no_space", "needed_bytes",
  "free_bytes"}` (`needed_bytes` = worst case + floor). Each member is copied only up to the size it had when planned (and
  never past its size when opened), so a log being appended cannot grow the archive. Preflight without building:
  `GET /admin/api/logs/check` and `GET /admin/api/runs/{run_id}/logs/check` → 200 `{"ok": true, "bytes": n, "runs": n,
  "skipped": n}` or the same 409 / 413 / 507 bodies (401 / 404 as the ZIP routes). The page asks the preflight after its session
  check and shows a Korean sentence instead of navigating: busy "다른 로그 ZIP을 만드는 중입니다. 잠시 뒤에 다시 해 주세요.",
  too large "로그가 너무 커서 한 번에 받을 수 없습니다. 실행별 로그를 받아 주세요." (one run over the cap: "이 실행의 로그가 너무 커서
  ZIP으로 받을 수 없습니다. 운영 담당자에게 문의해 주세요."), no space "서버에 임시 공간이 부족해 ZIP을 만들 수 없습니다.".

## v2.1z-ap — what the three-pods review found

**Admin page.** (W1) The log ZIP shipped `remediation/remediation.log` while a resume build pod was still writing it
before its final redaction pass: the router asked `RunManager.file_withheld`, which knows only the migration run. It now
asks the remediation API's own rule object (`RemediationManager._withheld`: migration final pass pending OR a resume
queued / creating / running, also after a web-pod restart). (W2) The all-runs ZIP could fill `/data` (built under
`<out>/_admin/`, no free-space check, no cap, any number at once): now one archive per process (409 `busy`), the temp file
in the pod's local temp dir with a 1 GiB free-space floor (507 `no_space`), a 2 GiB uncompressed cap (413 `too_large`), files
over 512 MiB listed in `SKIPPED.txt`, members copied only up to their planned size, preflight routes `…/logs/check` the page
asks first. (W3) The hour-old sweep that could unlink a ZIP still being streamed is gone (the temp file is deleted in
`finally`; `<out>/_admin/` leftovers are removed once at startup). Contract: "v2.1z-ao", bullets "Log ZIPs" and "Log ZIP
limits". Tests: `tests/web/test_admin.py`, `tests/web/test_admin_page.py`.

**KB.** (K1) Full-migration and reduced-scope (`jdk-jakarta`) scripts of one case are told apart. `CaseScript.scope` =
the scope of the run that wrote the version, `ScriptRun.scope` = the scope of each credited run (`full` /
`jdk-jakarta`; `""` = written before scopes were recorded = `full`; an older-image pod's writes are such legacy records,
nothing is migrated). Reuse (`CaseStore.best(case_id, scope)`, `sbmig.triage.scripts.stored_next` / `plan_script_step`,
the scope read from the fix loop's `CaseScripts.run_scope`) ranks **(same scope, proven, version)**: every version of
the asking run's scope comes first; a version of the other scope is still tried when none of the run's own is left
(a full-migration script for a pure `javax → jakarta` import case works in a reduced run too — the KB is not
partitioned), at most ONE per case and run, inside `script.reuse_versions`. `successes` / `failures` / `projects` /
`last_failure` / `last_result` and the status (unverified → proven) count only runs of the version's own scope; a run
of the other scope stays in `runs[]` with its scope. New run result **`out_of_scope`** (`sbmig.errors.cases.OUT_OF_SCOPE`,
in neither `SUCCESS_RESULTS` nor `FAILURE_RESULTS`): the framework guard rolled back a reused version of the other
scope (journal `script_credit` carries `credited: out_of_scope`; the attempt itself stays `apply_failed` in the report,
`errors.md` and the ladder); on a version of the run's own scope the same rollback is recorded as `apply_failed`. A
rebuild from `errors.jsonl` (`kb reindex`) reads a try whose note holds the guard sentence the same way. The hint line
of `kb.match` labels a last solution written by a run of the other scope (`last solution [from a full-migration run] —
…`; `case.json` key `solution_scope`); the augment prompt's version history says `written by a full run` for such
versions. `sbmig kb cases show` prints each version's scope (`scope full (not recorded)` for legacy) and each run's;
`cases.md` / `kb cases list` mark reduced-scope versions (`v2 jdk-jakarta proven 1/0`). Everything stays inside the
`<kb>/.lock` read-modify-write. (K2) A case id a re-sign moved while a pod held it: `credit` resolves it through the
moved case's `aliases` (the version whose stored code is the code that ran — the fix loop passes `code`; the same number
without code), else ONE WARNING naming the id; a credit whose `code` is not the version found under the id is not
recorded there. `add_script(old id, …, parent=N)` stores into the alias target when its L2 is the L2 asked for; a case
created fresh under the old id drops a `parent` it does not have (WARNING) and numbers the new version above it.
Tests: `tests/kb/test_case_scope.py`, `tests/loop/test_fixloop_scope_credit.py`.

**Remediation.** (R1) One driver per run folder: whatever drives the state — the chained first run, `chain.run_only`,
`chain.resume`, the in-process service's worker — holds `<remediation dir>/claim.json` (`chain.RunClaim`: created with
`O_CREAT | O_EXCL`, `{pid, host, attempt (resume: control/remediation-resume.json), started, purpose, token}`), removed in
`finally` on every end (`AWAITING_*`, `STOPPED`, terminal, exceptions; a default SIGTERM handler is swapped for one raising
`SystemExit` in the main thread while held). A fresh claim of another process → `StateConflict`
`another process is already working on this run: pid … on host … since … (attempt N)`, nothing touched; the CLI exits 3
(`SBMIG-REMEDIATION … status=REFUSED reason=state_conflict`, the sentence in `sbmig.log`), the Worker writes
`remediation-job-status.json` with exit code 3 like any refused resume; the chained first run / `run_only` return without
creating anything. A claim is stale when its pid is dead on the same host or its mtime (file system clock, probe file)
is older than `chain.CLAIM_STALE_SECONDS` (600 s); a keep-alive thread touches it every 30 s
(`CLAIM_REFRESH_SECONDS`), so a Maven build or an IVAS wait never looks silent; a stale claim is moved aside, inode-checked,
removed, journal `remediation_claim_taken_over` naming the old holder. The web pod never takes it: `chain.claim_info`
(read-only) makes `decide` answer 409 "다른 프로세스가 이 실행을 진행하고 있습니다. 끝난 뒤에 다시 시도해 주세요." while a live
claim exists (besides the active-resume refusal). (R2) `_drop_ineffective` saves `applied` without the dropped
proposals, their "did not change the resolved version" residual and `next_node=apply` BEFORE writing `ineffective` into
`plan.json` and removing the candidate; a resume rebuilds it from `accepted/` (`apply` skips `ineffective`); a build that
meets a drop again in a round that already dropped once fails as before (`after removing the ineffective override
entr(ies) …`). Completeness marker `rounds/<n>/candidate.ready.json` (`{round, applied}`; written after copy + apply + the
text-only check, removed before every rebuild): `build` without a matching marker goes back to `apply` (journal
`candidate_rebuilt`), `deliver_failed_candidate` freezes nothing without it (FAILED `candidate_build_failed`,
`the round N candidate was not delivered: …`, journal `candidate_not_vouched`). (R3) A resume that finds another policy
hash and drops a plan with human decisions says so: `state.message` = `the server policy changed since these proposals were
approved: the earlier approvals were not applied and the plan was made again` (+ ` | N proposal(s) of round R wait for
approval`), journal `approvals_dropped`; Korean via `i18n` (`policy_replanned`), shown by the page's `message_ko`. (M1)
In-process mode, a resumed chained run builds with the migration's Maven repository (no `-remediation` suffix); the
standalone upload keeps `<repo>-remediation`. Tests: `tests/remediations/test_resume_claim.py`,
`tests/remediations/test_drop_crash_safety.py`, `tests/remediations/test_service.py` (claim / M1), `test_i18n.py`.

## v2.1z-aq — a build pod that does not start

Company cluster, two uploads at once (`SBMIG_JOB_MAX_PARALLEL=3`): one build pod ran, the other stayed in `ContainerCreating`
for 28 minutes (a Kubernetes-side cause: volume attach / mount on another node, image pull, sandbox). The stop button did
nothing useful (`stop.request` has no reader before the container starts; the Job was deleted only after
`SBMIG_JOB_STOP_GRACE_S`, 2700 s) and nothing said why the pod did not start.

**Not started.** `JobState.started` is False for `Creating` (no pod) and `Pending` (no container started)
(`k8s.NOT_STARTED_PHASES`). `JobState` grew `waiting_reason` / `waiting_message` (the container's `state.waiting`) and
`sched_reason` / `sched_message` (pod condition `PodScheduled=False`); the existing fields keep their meaning.

**Stop before start.** `_watch_job` with `run.cancel` set writes `stop.request` first (unchanged), then, when that poll's
state is `Creating` / `Pending`, `_delete_before_start`: the `_owned` rule (both labels; an unowned Job is never deleted,
journal `job_delete_refused`, run STOPPED with `job … is not owned by this request — not deleted`, as for the grace delete),
journal `job_deleted_before_start` `{run_id, job, phase, reason[, action]}` (`reason` = the notice's reason when one was
found, else the waiting reason / unschedulable reason / Kubernetes message / phase), `_hold_until_ended(delete_after=0.0)`
(the slot is held until the Job is gone; journal `job_deleted` as before), run STOPPED with error
`stopped: the build pod had not started — the job was deleted` (`runs.STOP_BEFORE_START_ERROR`), `job` node update
`{state: "stopped-before-start", phase, message_ko}`. `status()` adds `stop_reason_ko` = `빌드 파드가 시작되기 전이라 작업을 바로
정리했습니다.` for a run whose error starts with that text (in memory and from `control/state.json`); the page keeps the title
`중단되었습니다` and shows that line in the result card's 사유 box. A container that starts in the same second reads
`stop.request` or gets the delete's SIGTERM (the Worker handles both). A stop while `Running` keeps the grace. `stop()`
wakes the watcher (`run.cond`) so the delete does not wait for the next poll. The same code runs for an adopted Job after
a web-pod restart (the adoption shares `_watch_job`; a `stop.request` left by the earlier Main does not set `cancel` by
itself, the user presses 중단 again).

**Remediation resume.** Shares `_worker` → `_run_job` → `_watch_job`. `stop_remediation` still writes `stop.request` and
returns `"requested"`, and now also sets the resume run's `cancel`: while its pod has not started the watcher deletes the
Job at once exactly as above (`job_deleted_before_start` carries `action`; the watcher does not write or journal
`stop.request` a second time). A RUNNING resume pod is never deleted: for a resume run the grace delete is skipped (it stops
at its next step boundary), so "never a Job deletion" stays true for it.

**Why it does not start.** `SBMIG_JOB_PENDING_NOTICE_S` (web pod env, read at startup, default `300`, `0` = never; not in the
81 template, not an env pin). While the Job is `Creating` / `Pending`, counted from the first such poll of this watch (an
adopted Job starts counting again), past the threshold `_note_pod_waiting` looks for the reason, in this order
(`k8s.pod_wait_reason`): (1) a container `waiting.reason` other than `ContainerCreating` / `PodInitializing`
(`ErrImagePull` / `ImagePullBackOff` / … → image, `CreateContainerConfigError` → config, others → unknown) with its
message; (2) `PodScheduled=False` → unschedulable; (3) the pod's events, `K8sClient.pod_events(pod)` = `GET
/api/v1/namespaces/{ns}/events?fieldSelector=involvedObject.kind=Pod,involvedObject.name=<pod>`, newest first, asked at
most every 15 s (`runs.PENDING_EVENTS_EVERY_S`) and only when (1) / (2) found nothing: the newest `Warning` of
`FailedMount` / `FailedAttachVolume` (a `Multi-Attach` message → multiattach, else mount) / `FailedCreatePodSandBox` →
sandbox / `Failed` naming an image or pull → image / `FailedScheduling` → unschedulable, else the newest `Normal`
`Pulling` → pulling; nothing → unknown. `pod_events` returns `[]` and sets `events_readable = False` for the process on
a 403 (events need their own RBAC) or an odd body; a transport error or another status is "no events this time".
Startup asks `can_i("list", "events")` once after the required verbs: not allowed = ONE info line `pod events are not
readable with this account — the page will not show why a pod does not start`, never a failure (not in
`REQUIRED_PERMISSIONS`).
What goes out: `run.job_pending` = **`{seconds, reason, message, hint_ko, family, source, pod, phase}`** (`family` =
mount | multiattach | image | pulling | unschedulable | sandbox | config | unknown, `source` = waiting | condition | event |
none; `message` capped at 300 characters and scrubbed like other event strings), refreshed every poll; `status().job.pending`
(and `remediation_job.pending` for a resume) carries it while the run is live, so a re-attached page shows it. The `job`
node update `{state: "pod-pending", job, pod, pending}` is pushed when the reason (`family`, `reason`) changes or every
5 minutes (`runs.PENDING_RESEND_S`) with the new waiting time; journal `job_pod_pending` `{run_id, job, pod, phase,
seconds, family, reason, message, source}` and ONE WARNING `run … job … pod … has not started for Ns: <Reason>: <message>`
per distinct reason. When the pod leaves `Creating` / `Pending` the notice is cleared (`job_pending` None, update
`{state: "pod-started"}`). It never fails the run, frees the slot or deletes anything.
`hint_ko` (`k8s.pod_wait_hint_ko`, `k8s.POD_WAIT_KO`): `빌드 파드가 아직 시작되지 못했습니다(N분째).` + one sentence per family
(mount `볼륨을 붙이지 못하고 있습니다. 인프라 담당자에게 문의해 주세요.`, multiattach `볼륨이 다른 노드에 붙어 있어 이 노드에서 쓸 수
없습니다. 인프라 담당자에게 문의해 주세요.`, image `이미지를 받지 못하고 있습니다.`, pulling `이미지를 받는 중입니다. 이미지가 크면 몇 분
걸립니다.`, unschedulable `파드를 띄울 노드가 없습니다. 자원이 부족할 수 있습니다.`, sandbox `노드에서 파드 네트워크를 준비하지 못하고
있습니다. 인프라 담당자에게 문의해 주세요.`, config `파드 설정을 읽지 못하고 있습니다. ConfigMap과 Secret을 확인해 주세요.`, unknown
`이유는 kubectl describe pod <파드>의 Events에서 볼 수 있습니다.`, `describe job <job>` while no pod exists). The page shows it in
a box under the rail (`#podwait`, directly below the 파드 step), then `Reason: message` in a muted line and
`중단을 누르면 이 실행을 바로 정리합니다.`; the box goes on `pod-started`, on any other pod-step state than creating / pending /
created / api-*, and when the run ends.
RBAC: `deploy/k8s/45-web-rbac.yaml` grants `events: list`; the kubeconfig model's permissions are the infra team's.
Operator text: `deploy/README.md` §3 "동시에 여러 건" (the Events table, how to end such a run, ReadWriteMany), `deploy/ENV.md`.
Tests: `tests/web/test_pod_not_started.py`; `tests/deploy/test_manifests.py` (the RBAC line).

## v2.1z-ar — no proposal waits for approval unless an operator switches it on

The project teams asked for the result only (the user, 2026-11-18): "취약점진단 승인 없애고 그냥 쭉 다 일괄 승인해서 진행해서 결과만
알려 달래! 여기 빌드파드 제한이 3개니까 이쪽까지 승인까지 하긴 힘들다고!" An approval ends the build pod and its resume takes a
second pod out of three, so the shipped configuration applies every proposal on its own.

- **Defaults.** `remediation.require_approval: false` (as before) and `remediation.always_approve: []` (was `[major, parent,
  bom, managed_override]`), in `RemediationConfig` and in `config/default.yml`.
- **One rule.** `Proposal.requires_approval = require_approval or (risks ∩ always_approve ≠ ∅)`; `decision` is `pending` when
  it is true, else `auto`. The planner's fixed rule "a `managed_override` proposal always waits" is removed: an override entry
  (root pom or module pom, v2.1z-an) waits only when `require_approval` is true or one of its risk flags (`managed_override`,
  plus `major` when it crosses a major) is listed. The note sentence `always needs approval: an inserted <dependencyManagement>
  entry is never applied automatically` is no longer written (`planner.OVERRIDE_APPROVAL` is gone; `i18n` keeps its rule
  `override_approval`, so a plan saved earlier still reads in Korean).
- **With the shipped configuration** no run saves `AWAITING_APPROVAL`: `plan → apply → build → freeze_delivery → rescan →
  evaluate → report`, every round, without a stop. A chained remediation ends in the build pod that ran the migration; a
  further pod is needed only after a stop, the time budget (`chain_budget`) or `AWAITING_CONFIRMATION`.
- **Switching approval on.** `require_approval: true` = every proposal waits (`SBMIG_REMEDIATION__REQUIRE_APPROVAL: "true"` in
  `sbmig-env`, the key both pods already read; restart the web pod). Single classes: `remediation.always_approve` in the
  company yml baked into the image, e.g. `[major, parent, bom, managed_override]` = the classes that waited before this
  increment (no env line: the Job template `81` passes no `…__ALWAYS_APPROVE` key, and a key only the web pod has makes the
  Worker's environment comparison refuse the Job). Everything under "Approval" below — decisions, hashes, the page's checkbox
  list, the resume in a new pod — then works unchanged.
- **What stands between a proposal and the delivery without a reviewer** (all as before): the isolated candidate, the text-only
  check, the candidate build (a change set that breaks it fails as a whole: `FAILED candidate_build_failed`, delivered or not
  by `failed_candidate`; nothing is retried without single proposals), `verify_resolution` (an override entry without effect is
  removed again, v2.1z-an), the rescan of the frozen delivery ZIP.
- **Policy hash.** Its inputs are unchanged, so a default configuration gets a new hash. A run an earlier version saved in
  `AWAITING_APPROVAL`: the page's decisions are accepted as before (chained: checked against the hash in `state.json`); the
  resume then plans again (`policy_changed`, `approvals_dropped`, `POLICY_CHANGED_NOTE`), the proposals the owners left
  unchecked stay rejected for that run, every other one is applied.
- **Report (`report.md`).** 조치 완료: when at least one committed proposal carries a risk flag, the table has the column 「주의」
  before 「해결된 취약점」 with the labels 승인 대기 uses (메이저 버전 변경 / 부모 pom 변경 / BOM 변경 / dependencyManagement 항목 추가;
  `-` for a row without a flag; a grouped row shows the union). With no flag at all the table is byte-identical to the earlier
  one. 해야 할 일 gets the step "**자동으로 적용한 변경 확인하기 (N건)**" when N ≥ 1 committed proposals have decision `auto` and a
  risk flag — a proposal a person approved is not counted. Its place: after the result-ZIP step and after the steps a failed /
  stopped / waiting-for-confirmation state puts first, before the approval, in-house and manual-review steps. It names each
  class with its count (a proposal with two flags counts in both classes, once in N) and points to the 「주의」 column. `report.json` is unchanged (`rounds[].plan.proposals[]`
  already carry `risks` and `decision`).
- **Page.** No new element: with nothing pending the checkbox list is never shown. The rail note of the remediation step no
  longer speaks of approved changes.
- Tests: `tests/remediations/test_planner.py`, `test_module_overrides.py`, `test_override_variants.py`, `test_integration.py`,
  `test_report.py`, `test_report_module_overrides.py`; operator text `agent-doc/11-remediations.md`,
  `agent-doc/12-remediation-report.md`, `deploy/ENV.md`.

## v2.1z-as — the Korean report.md is a guide for the person who asked for the migration

The user, after reading a company report (2026-11-18): the report reads like machine output ("너무 기계적으로 리포트가
나왔다"). Its purpose: "마이그레이션을 요청한 담당자한테 우리 이렇게 마이그레이션 했고 이런거 확인해주세요~ 라고 가이드를
줄려고 만드는거라 에러 목록들 안보여줘도 되고 … 수정 적용된 최종 코드들 java파일이랑 diff만 보여주면돼! 굳이 fix r1,r2
이렇게 보여주지 않아도 되구!" Named points: the head table holds 결과, SBMIG-RESULT, 전환 후, 실행 ID, 시작 / 종료, 플래그
only; the column name 사유 and the word 관리됨 are odd; "의존성 3개를 as-is 버전으로 고정했다: …" is not a sentence a
person writes.

- **Two renderers.** `report.language: ko` (the company setting) renders the guide: `sbmig/report/guide_ko.py`
  (`render_guide_ko(report, cfg=None) -> str`), written in Korean directly, not translated line by line.
  `report.language: en` keeps the diagnostic layout of `sbmig/report/markdown.py` unchanged. `render_markdown(report,
  language, cfg)` stays the one entry: `ko` → the guide (a `remediation` flow run keeps `render_remediation_flow`).
  `report.json`, `errors.md`, `errors.jsonl`, `changes.patch`, the console and the `SBMIG-RESULT` line do not change.
- **Order of the guide.** Title `# 마이그레이션 보고서 — <project>` → the intake notice (Ant refusal) → head table → one
  sentence saying whom the document is for → 사내 프레임워크 (when the scope is reduced) → `## 전환 내용` → `## 확인해
  주세요` → `## 코드 수정 내역` → `## 해결하지 못한 오류` (only with open entries) → `## 진행하지 않은 항목` (blocked, only when
  present) → the special sections when they apply (운영자 확인, pom 합성 (Ant), ADF, 포함된 jar, javax API jar, 외부 설정
  값, 취약점 조치) → `## 참고` (collapsed `<details>` blocks). A section without content is left out; sections are not
  numbered.
- **Head table.** Exactly these rows, in this order: `**결과**` (✅ 빌드 성공 / ❌ 빌드 실패; an aborted run reads
  `❌ 실행 중단 (빌드까지 진행하지 못했습니다)`), `SBMIG-RESULT` (the
  line, unchanged), `전환 후`, `실행 ID`, `시작 / 종료`, `플래그` (only when there are flags). The rows 에러 목록, 유형, 전환 전,
  LLM, LLM 비용, 차단, javax API jar and 구조 점검 are not in the head table (유형, 전환 전, LLM and 구조 점검 move to 참고).
- **전환 내용.** A table 항목 / 변경 전 / 변경 후 for Java, Spring Boot and the parent pom (rows without a change left out;
  an absent value reads 없음 or 사용하지 않음). `### 의존성 버전`: columns 항목 / 변경 전 / 변경 후 / 비고 (never 사유). A
  version the pom does not write because a parent or BOM gives it reads `부모 pom에서 지정` (with the version in front when it
  is known: `2.2.10.RELEASE (부모 pom에서 지정)`), never 관리됨; under the table one sentence explains the phrase when it
  occurs. A carried-over row keeps its ⚠ and reads `<version> (pom에 직접 지정)` with the 비고 `새 부모 pom에 버전이 없어 기존
  버전을 유지`; under the table: "⚠ 표시한 의존성 N개는 새 부모 pom과 Spring Boot BOM에 버전이 없습니다. 빌드가 되도록 기존에
  쓰던 버전을 pom에 직접 적어 두었으며, Spring Boot 4에서 동작하는지는 확인이 필요합니다." Then 추가한 의존성 / 제거한 의존성.
- **확인해 주세요.** ONE numbered list replaces today's three places (the library table, "From here on" and "Manual
  follow-ups"); no item twice. First the library advisories, one item each, built from `RunReport.advisories`: the
  advisory's title sentence in bold (`**Netflix Hystrix는 지원이 종료되었습니다.**`), then what the run did by outcome
  (`still-present`: "이번 전환에서는 자동으로 바꾸지 않고 기존 버전(…)을 그대로 두었습니다. 아래 「대체할 라이브러리」로 바꾸고
  「할 일」을 확인해 주세요." — the half about the replacement only when the advisory names one;
  `replaced`: "이번 전환에서 교체했습니다. 아래 「할 일」대로 동작을 확인해 주세요."), and under it sub-bullets — 대체할 /
  교체한 라이브러리, 설명 (`summary_ko`), 할 일 (`followup_ko`), 수정한 파일 (the files the LLM rewrote for it), 문서 —
  each left out when empty. On a failed build the pointers to 「해결하지 못한 오류」 and 「진행하지 않은 항목」 come before
  the advisories. Then the remaining hand-over items, each with a short bold lead where its kind is known (`HandoverSummary.from_here` without the lines derived from an
  advisory, then `manual_followups` not already said), through the polished line rules of `i18n`. Nothing to check →
  one sentence saying so.
- **Advisory texts in Korean.** `LibraryAdvisory` / `AdvisoryFinding` carry `title_ko`, `summary_ko`, `followup_ko`,
  `replacement_ko` (default ""; the loader copies them into the finding). `knowledge/libraries/advisories.yml` gives them
  for every shipped entry. The guide shows the `_ko` text and falls back to the English one when it is empty.
- **코드 수정 내역.** `### 자동 변환`: one line per automatic step with its counts (OpenRewrite: files and what the recipes
  cover in words; the restructure; the JSP javax → jakarta rename; the pom synthesis) and a pointer to `changes.patch` — no
  diff. `### 직접 수정한 파일 — N개`: the files of `RunReport.code_changes`, each with its path and `(+a −r)`, the sentences
  of `notes` as a list, `수정 이유:` with `reasons`, and the diff in a fenced `diff` block; files stored without a diff
  follow in one table (파일 / 변경) with the pointer to `changes.patch`. No error rows, no case ids, no rounds, no tries.
- **`RunReport.code_changes`** (`CodeChange`: `path`, `status`, `added`, `removed`, `notes`, `reasons`, `diff`,
  `truncated`), `code_changes_total`, `code_changes_base`: built at finalize by `sbmig.report.changes.collect_code_changes`
  from the workspace's git history — the NET change of every file between the commit right after the automatic conversion
  (OpenRewrite, restructure; before the structure review's fix and the first fix try) and the final tree. One entry per
  file however many tries touched it; a file whose changes were rolled back is not in the list. Order: `.java`, then
  `pom.xml`, then the rest, each by path. Caps (module constants): diff text for the first 30 files, 80 diff lines per
  file (`truncated`), 500 listed files; `code_changes_total` counts all. `notes` = the kept tries' `EditNote`s the LLM
  wrote for the path (`source == "llm"`; notes made from the diff are left out), `reasons` = the `analysis` of each case
  whose kept try changed the file, shortened to its first sentences. Build output, binary files and `.git` are never
  listed. Text goes through the same scrubbing as `errors.md`. A run that never reached the fix loop has an empty list.
- **해결하지 못한 오류.** Only the entries still open after the final build, as a table 파일 / 줄 / 오류 (grouped by file,
  capped, the rest counted) with the pointer to `errors.md`. Resolved errors are not listed anywhere in the guide.
- **참고.** `<details>` blocks: 실행 정보 (유형, 전환 전, the executed steps in words, the recipes, the structure review's
  verdict, each flag with its explanation), LLM 사용량과 비용, 산출물 (file names relative to the run directory), 진단용 기록
  (the plan / phase notes as they are, English).
- **Language of the LLM's explanations.** The fix prompt asks for `<analysis>` and `<solution>` in the language it
  already names for the change descriptions (`describe_language`: Korean with `report.language: ko`); the structure
  review prompt asks the same for its problem / hint / analysis texts. Explanations stored in the KB earlier keep their
  language.
- **Wording.** Workplace Korean: polite 합니다체 sentences a senior developer would write to a colleague; no word-for-word
  rendering of code vocabulary (as-is 버전으로 고정, 관리됨, 사유, 케이스, 시도, 라운드 do not appear in the guide); Maven and
  product words stay as they are (pom, BOM, parent 는 "부모 pom", dependencyManagement, OpenRewrite). The line rules of
  `sbmig/report/i18n.py` that the guide still uses (follow-ups, flags, blocked rows, the special sections) are reworded
  to the same standard.
- Tests: `tests/report/test_guide_ko.py`, `tests/report/test_code_changes.py`, `tests/advisory/`; operator text
  `agent-doc/03-run-debug-and-logs.md`, `agent-doc/10-*.md`.

## v2.1z-at — Hystrix is replaced with Resilience4j by a recipe again

Hystrix stayed in migrated projects: no recipe touched it, its version was carried over from the as-is build
(v2.1l), so it compiled against the old jar, the build was green, and the library does not work on Boot 4. The user
(2026-11-18), on hearing that the automatic replacement of an earlier round had been deleted with the v1 flow: "이게
동작하지 않는건 되살려서 동작하도록 해주는건 맞는거 같아!"

- **The recipe.** `knowledge/recipes/base/hystrix-to-resilience4j.yml`: the declarative composite
  `com.company.migrate.HystrixToResilience4j` — `@HystrixCommand` → Resilience4j `@CircuitBreaker` (`commandKey` → `name`,
  `name = "default"` when there was none, `fallbackMethod` kept, the Hystrix-only attributes and nested `@HystrixProperty`
  removed), `@EnableHystrix` / `@EnableCircuitBreaker` / `@EnableHystrixDashboard` removed, the starters
  `spring-cloud-starter-netflix-hystrix` / `spring-cloud-starter-hystrix` → `spring-cloud-starter-circuitbreaker-resilience4j`,
  `hystrix-javanica` / `hystrix-core` / the dashboard starter removed. Its building blocks are in
  `knowledge/recipes/catalog.yml` like every recipe id.
- **When it runs.** Recipe set `openrewrite.recipes.hystrix_resilience4j` (default: the composite; `[]` switches it off).
  The planner adds the set when the analyzer found the library `hystrix`, for every case type whose plan has the
  transform step, in the ONE OpenRewrite run and before the Boot / Framework sets (the swap happens while the as-is
  Spring Cloud BOM is still in the pom). One plan note names it.
- **Versions are not made up.** The recipe writes no version of its own. The as-is Hystrix version never stays on the
  new starter; the starter is left without a version and the rules of v2.1l decide: managed by the new parent chain or
  the (upgraded) Spring Cloud BOM → no version in the pom; otherwise the restructure takes the newest release the
  repository has and the existing "confirm …" follow-up asks the owners to check it.
- **Code the recipe cannot port** (`extends HystrixCommand` / `HystrixObservableCommand`, `HystrixRequestContext`,
  dashboards): with the Hystrix artifacts gone it does not compile; those errors reach the fix loop, which ports them
  with the `## fix-loop: hystrix` note. What the fix loop cannot port stays as open errors — the build is not green
  with dead Hystrix code any more.
- **Advisory `hystrix`.** Outcome `replaced` when no Hystrix artifact is left. Its texts (English and `_ko`) say what
  replaces what — not who did it, since the report item's lead says whether this run replaced it — and what the owners review at runtime (instance names, timeouts, the `Throwable` parameter of fallback
  methods, `spring-boot-starter-aspectj`). `still-present` remains for a configuration that switched the set off.
- **Reference projects.** `fixtures/projects/boot2-cloud-legacy`: `hystrix` ends `replaced`, the sources carry
  `@CircuitBreaker`, no Hystrix artifact is carried over. `fixtures/projects/boot2-hystrix-command`: the starter is
  swapped and the `HystrixCommand` subclass is ported by the fix loop.
- Sleuth and Springfox are unchanged: the Boot 4 umbrella replaces `spring-cloud-starter-sleuth` (Micrometer Tracing)
  and Springfox (springdoc); other Sleuth-family artifacts the umbrella does not know are carried over and reported.

## v2.1z-au — the code-change view: every file of the migrated tree against where it came from

Relayed by the user (2026-11-18): "마이그레이션 완료하면 github나 bitbucket처럼 소스코드 수정 화면으로 볼 수 있게 … 부트쪽은
표현이 편할텐데, 스프링이나 논프레임워크 같은 경우 변환기준으로 보여주고 어디 있던 java 파일이 이 주소로 왔다도 표시 … 수정분만
보여주지 말고 위 아래 코드들도 좀 보이도록", in a section of the result card that can become a modal later.

Three parts: the build side writes one artifact per run (`sbmig/report/codediff.py`), the web serves it
(`sbmig/web/codediff.py`), the page shows it (component `CodeDiff` in `sbmig/web/static/index.html`). The web pod runs
no git and no diff: it reads the artifact and, for more context, the final file out of `result.zip`.

**What is compared.** The as-is tree (the first commit of the run's workspace: the upload as intake stored it) and
the final tree (what `result.zip` holds: the working tree at finalize, build output left out). Every file of the
final tree is shown AT ITS FINAL PATH against its **origin**, the as-is file it came from; a file that changed place
says where it was. `changes.patch` is unchanged and still written.

**Origin of a final file** (decided in this order; each as-is file is the origin of at most one final file):
1. the as-is file at the same path;
2. *same content*: an as-is file with the same file name and identical bytes whose own path no longer exists in the
   final tree; among several the one sharing the longest run of trailing path components, then the smallest path;
3. *similar content*: an as-is file with the same file name, gone from its path and not taken by rule 2; candidates
   by the longest shared trailing path, at most 5 tried, the first that passes is taken; it passes when at least half
   of the final file's lines occur in it (`SIMILARITY` 0.5; lines as a multiset, surrounding whitespace trimmed, blank
   lines not counted; never for a binary file or a blob over 8 MiB);
4. none: the file is new.
Where two final files want the same as-is file (rules 2 and 3), the pairs are decided best first: longer shared
trailing path, then the final path, then the as-is path. An as-is file that is gone and nobody's origin is a deleted
file. Only regular files (git modes 100644 / 100755); a file's mode plays no part (a moved file whose mode alone
changed is `moved` with `changed: false`).

**Config** (`report.*`): `code_diff: true` (write the artifact), `code_diff_context: 10` (unchanged lines kept above
and below each change, 0 … 50). Not written for a dry run, for `--flow remediation`, or when the workspace has no git
repository. Writing it never fails a run (one WARNING, no artifact).

**Artifact** `<run dir>/codediff/` (not redacted: it is the project's own source, like `changes.patch` and
`result.zip`; untouched by `fleet.cleanup`):
- `index.json`
  ```json
  {"version": 1, "run_id": "…", "generated_at": "<UTC ISO-8601>", "base": "<first commit sha>", "context": 10,
   "zip_root": "project",
   "summary": {"files": 0, "modified": 0, "added": 0, "deleted": 0, "moved": 0, "moved_only": 0,
               "added_lines": 0, "removed_lines": 0, "binary": 0},
   "moves": [{"from": "src/", "to": "demo-back/src/main/java/", "files": 412}], "moves_total": 1,
   "files": [{"id": 0, "path": "demo-back/src/main/java/com/x/Foo.java", "old_path": "src/com/x/Foo.java",
              "status": "moved", "changed": true, "binary": false, "added": 3, "removed": 3,
              "encoding": "utf-8", "patch": [0, 512], "omitted": ""}],
   "files_total": 1}
  ```
  - `status`: `modified` (same path, content changed) | `added` (no origin) | `deleted` (`path` = the as-is path) |
    `moved` (origin at another path; `old_path` set, `changed` says whether the content differs too). A file whose
    path and content are both unchanged is not listed; neither is a mode-only change.
  - `summary`: counted over every entry BEFORE the `MAX_FILES` cut (`summary.files` = `files_total`); `moved` =
    all `moved` entries, `moved_only` = those with `changed: false`; line counts over text files; `binary` = binary
    entries.
  - `moves`: where folders went. Per moved file the shared trailing components of `old_path` and `path` are cut
    off; what is left is `from` → `to` (`""` = the project root, otherwise with a trailing `/`), grouped, most files
    first; at most 200 groups, `moves_total` = the number before the cut.
  - `files` sorted by `path`; `id` = position. At most `MAX_FILES` (20,000) entries, `files_total` = the number
    before the cut (changed files are kept before moved-only ones when cutting).
  - `encoding`: the file's detected encoding (`Workspace.encoding_of`; a deleted file's from its bytes).
  - `patch`: `[offset, length]` into `patch.bin`, or `null` with `omitted` = `binary` | `too_large` (one file's
    hunks over 1 MiB) | `cap` (`patch.bin` would pass 64 MiB) | `unavailable` (git's patch sections and its name
    list did not agree, so no range of that diff is trusted; the line counts are still right) | `""` (nothing to
    show: a moved-only file, or an added / deleted empty file).
  - `old_path` is `null` unless `status` is `moved`.
- `patch.bin`: per file the unified hunks, from its first `@@ -a,b +c,d @@` line to the end of its section, in the
  file's own bytes (`git diff --no-renames --histogram -U<context>`; the `diff --git` / `---` / `+++` header lines
  are not in the file at all). Never larger than 64 MiB.
- `summary.json`: `index.json` without `files` (what the status call reads).
- Written in the order `patch.bin`, `summary.json`, `index.json`, each through a temporary file; a failure removes
  the folder.
- Journal: `{"event": "code_diff", "files": n, "moved": n, "added_lines": n, "removed_lines": n, "seconds": s}`,
  or `{"event": "code_diff_skipped", "reason": "…"}`.

**How it is built** (no rename detection of git, no per-file process: about twenty git calls whatever the size of
the tree): `Workspace.snapshot_tree()` writes the working tree as a tree object through a temporary index (a copy of
the real one WITH its timestamp, else git's racy-clean check misses a same-size edit made in the index's own second;
ignored jars are added as `commit()` adds them; HEAD and the real index untouched); the origins are computed from
`Workspace.tree_entries(<first commit>)` and `tree_entries(<snapshot>)`; `Workspace.make_tree(entries)` builds a base
tree holding each origin's blob at the FINAL path; one `git diff <base tree> <snapshot>` gives every modified, moved
and added file at its final path, a second one (deleted files against the empty tree) the deletions.

**HTTP** (behind the site token like every route; ids through the run-id check; 409 while the run's final redaction
pass is pending, like the other result files; 404 `{"error": "code diff not available"}` without the artifact):
- `GET /api/runs/{id}/codediff` → `index.json`.
- `GET /api/runs/{id}/codediff/files/{n}` →
  `{id, path, old_path, status, changed, binary, added, removed, encoding, omitted, expandable,
    hunks: [{old_start, old_lines, new_start, new_lines, section, lines: [[kind, old_no, new_no, text], …]}],
    no_newline: {old, new}}` — `kind` is `" "`, `"+"` or `"-"`; a number is `null` on the side a line does not exist;
  `text` is decoded with the entry's encoding (replacement characters, never an error), without the line end, cut at
  5,000 characters with `…`; `expandable` = the final file can be read from `result.zip` (false for a deleted or
  binary file, a missing ZIP or member, a member over 32 MiB). A stored range that cannot be read or parsed gives
  `hunks: []` with `omitted: "unreadable"` (one WARNING), never a 500.
- `GET /api/runs/{id}/codediff/files/{n}/lines?start=<a>&end=<b>` → `{start, end, total, lines: ["…"]}`: lines
  `a … b` (1-based, inclusive) of the FINAL file, read from the member `<zip_root>/<path>` of `result.zip`. Whole
  numbers are clamped (below 1 → 1, past the file → the last line, a span over 5,000 lines is cut); the returned
  `end` is `start + len(lines) - 1`; defaults `start=1`, `end=start+199`; text that is not a whole number → 400.
  404 `{"error": "lines not available", "reason": …}` for a deleted or binary file, a missing ZIP or member, a
  member over 32 MiB, an unsafe path in the index.
- 404 bodies carry `error` and, where there is one, `reason`; a bad run id is 400, an unknown run 404 `unknown run`,
  an `n` outside the list 404 `no such file in the code diff`. Every response is `Cache-Control: no-store`. The
  parsed index (4 runs) and each `result.zip`'s member list (8) are kept in small caches keyed by the file's
  mtime, size and inode; no file handle stays open between requests.
- `GET /api/runs/{id}` gains `code_diff`: the `summary` object, only when the artifact can be served.

**Page** (the layout of "v2.1z-bd" below replaces the first form, in which the files stood one under the other
inside the card). One row 「코드 변경」 in the result card, after 「취약점 조치」 and before the report preview, shown when
`status.code_diff` is there (never for a remediation-only run): the summary chips and the button 「코드 변경 보기」,
which opens a large dialog; nothing is fetched before that. In the dialog: the summary, 「폴더 구조가 바뀌었습니다」 (the
`moves` table, foldable) when files moved, filters (전체 / 수정 / 추가 / 삭제 / 이동, a path search, 「위치만 바뀐 파일도
보기」), a file tree by final path, and the ONE file selected in it. The file's head says its state and, when it
moved, 「원래 위치: …」; its body shows the hunks with both line numbers, the differing part of a changed pair of lines
marked, and rows that load more lines above, below and between hunks (`…/lines`; 「파일 전체 보기」 in the head); a
change of more than 400 lines starts folded. Each state has its sentence (loading, a failed request with 「다시 시도」,
results not released yet, binary, left out because of its size, `unavailable` / `unreadable`, moved only, nothing
matches, the list cut at 20,000). The block is the
component `CodeDiff` (`mount(host, runId)` / `unmount()`; its own state, its DOM built inside `host`, styles under
`.cdiff`), so a dialog can host it later without a rewrite. It reads `code_diff` from the status the page already
gets (the SSE `status` event and `GET /api/runs/{id}`). Needs a browser with CSS container queries and `:has()`
(current Chrome / Edge).

## v2.1z-av — what the whole-code review found

The user (2026-11-18), after a long series of feature rounds: "전체 코드 검사 한번 해보자! 문제될 사항들이 보이는지 비어있는곳이
있는지!" Seven read-only reviews (the newest changes; the run loop; the LLM / case / KB side; analyze / restructure / build;
web / Job / deploy; remediation and report; the seams between the parts) plus one pass over the modules nobody had reached.
Every finding was reproduced before it was reported, and fixed test first. What changed, by area; where an earlier section
says otherwise, this one counts.

**The run.**
- *A stop request around the final build* (`Orchestrator.late_stop`, the same in both engines). A planned final build
  without a result → the run is aborted with `STOP_REQUEST_REASON` as before. The fix loop left because of the request →
  aborted with `STOP_LOOP_REASON` ("… the fix loop was left early; the last build shows the tree as it was left"); the final
  build is not repeated when the tree is unchanged since the loop's last build (that build stands as `final_build`).
  Every planned final build has a result → the request is ignored and the run ends as what it is; a chained remediation
  still skips itself with `stop_requested`. Journal `stop_request {node: finalize, ignored | after}`.
- *The last try of a case that did not help is not delivered.* When the fix loop ends, each case's last applied try with
  the outcome `no_change` or `inconclusive` is rolled back before the final build (`BuildFixLoop._rollback_unhelpful`,
  journal `script_rollback {where: "loop end"}`, attempt note "rolled back before the final build (it did not help)"). A
  partial try stays, also when its case did not end blocked. Before a case's next try the rule is unchanged.
- *`report.final_build` of an MSA run* is the first failing final build when one failed (it was the last one run).
- *A fixer plugin that returns no proposal* fails that attempt (`apply_failed`), not the run.
- *Framework guard* (reduced scope): the framework artifacts' versions are compared before and after an attempt with
  `${…}` resolved through the project's own parent chain, so a version changed through a property of another pom, or a
  property a framework-matched import uses, is rolled back like a direct change.
- *Framework check that could not look* (framework artifacts configured, nothing found, the parent pom AND the transitive
  lookup both unreadable): flag `framework-unchecked` and the Handover line "framework check could not run (…): if this
  project uses the in-house framework it must not be migrated in full — confirm before using the result". The run goes on
  in full scope; the exit code is unchanged.
- *A repository that did not answer is not "not found".* A resolution error with a network cause (time-out, refused or
  reset connection, unknown host, no route, PKIX, a 5xx) is `BlockReason.REPOSITORY_UNREACHABLE` (`sbmig/triage/blocked.py`
  `classify_transfer`): `loop.blocked_policy.repository_unreachable: 0`, no LLM try, decided before stored-script reuse so
  that a rebuild that passes because the network came back credits nothing. 401 / 403 and a "not found" answer keep their
  reasons. The `sbmig-bundled` pre-try follows the same rule (below).
- *A restructure that fails leaves the as-is tree.* `Restructurer.apply` records HEAD at its start (when the tree is clean)
  and, when the step fails after it moved files or made commits, resets to it; the result carries the note "restructure
  undone: the tree was reset to <sha>, …" (or "restructure not undone: …" when it could not). The orchestrator does the
  same for a restructure that raises (journal `restructure_reset`).
- *The stage note of an abort inside the loop* carries the reason only (no exception class name).

**The project side.**
- *Source-encoding sample*: a module that gets no sample because the total budget (1,500 files) is used up is reported
  "not sampled" (`ModuleEncoding.not_sampled`); the modules before it keep their result. (It used to raise, and no module
  got its `project.build.sourceEncoding`.)
- *Hollow reactor*: a module's build directories (`sourceDirectory`, test sources, `<resources>`, the war plugin's
  directories) are taken from its parent chain inside the project, the child's own values first.
- *Optional-artifact bump*: the property edited is the one the module inherits (its own pom, then its parents inside the
  project); with none there, the literal version is written into the module.
- *Orphan version properties*: a property is kept when any text file of the project other than the poms holds `${name}`
  or `@name@` (filtered resources, assembly descriptors); the same for dead version properties.
- *Pom encoding*: a pom is saved in its declared encoding when that can hold the text, else in the encoding it was read
  with; character references only as the last resort.
- *Bundled jars*: content is compared at every candidate name before a jar in `lib/` is reused. The repository lookup has
  three answers (`restructure.bundled_jars.repository_state_fn`: present / absent / unreadable): an unreadable repository
  means the jar is left alone with the reason "repository unreadable …"; nothing asks the owners to publish it. The
  `sbmig-bundled` pre-try of the fix loop asks the same way: an unreadable lookup blocks the case
  `repository_unreachable` (nothing wired, no LLM try, nothing credited).
- *`-Werror`*: the compile error's message is the cause line ("error: warnings found and -Werror specified"); refined case
  keys are unchanged.
- *Managed versions per company parent*: `ParentChainFacts.root_managed` / `addon_managed` and
  `managed_for(parent, *, own=False)`. Where the capture after the restructure is unavailable, each pom is judged against
  the parent it has (the root pom → the base parent, a module → its role parent): the fallback capture, the Spring Cloud
  BOM import, the as-is version pin, own management entries, and the fix loop's `VersionResolver` (full scope only;
  a reduced `jdk-jakarta` run keeps taking its facts from the project's own chain).
- *Binary resources under filtering*: the extensions are collected over every matching file; only the reported list is
  capped (`ResourcePlan.files_total`).

**LLM answers, cases, the KB.**
- *Structure review*: the JSON is decoded leniently (control characters inside strings); when only a fragment decodes or
  the answer was cut (`finish_reason` length / max tokens) the answer is unreadable and the one retry runs (with a request
  for a shorter answer); two unreadable answers = verdict `failed`. Never a review reduced to its first issue.
- *Token exchange*: a failed exchange (5xx, 429, 401 / 403 from the token endpoint, a non-JSON body, no `access_token`,
  a transport error) is `TokenExchangeError`, retried like a chat call, and its text carries `status=<code>` or `cause=…`,
  so the fix loop counts it as an LLM outage (cool-down), not as an answer. The response body of the token endpoint is
  never logged.
- *An answer cut at the token limit* whose `<script>` / `<edits>` block is not closed is not used and not stored; the
  feedback round asks for a shorter answer; cut again → the attempt fails as an answer problem.
- *Script blocks*: an opening tag with attributes is recognised; of several blocks the last complete one is taken (warning).
- *The feedback round failing* keeps the usable part of the first answer.
- *Requested files* (`<need_files>`) are cut by characters as well as lines; the context message respects
  `llm.max_context_chars`.
- *`case.json`*: unknown fields of a script version and of a run record survive a re-save (as top-level ones did).
  `kb import` writes each script to `fix_v<N>.py` inside its case folder whatever the imported file names.
- New replay fixtures need an entry in `tests/fix/data/replay_parse_golden.json`.

**Remediation.**
- *Fixed means gone from the rescan.* One key for "the same finding" (kind, id, case-folded identifier; no version) in
  the counts and the Korean report: 요약, 조치 완료 and 「해결된 취약점」 show only what a complete final scan no longer
  reports; a change whose finding is still reported shows "-" with one explaining sentence.
- *Planner rules that keep a wrong version out* (all end as residuals for a person, groups in `i18n.GROUPS`): a version
  property that several artifacts share is changed only when the target exists for each of them (`shared_property`); a
  jump to another major only when a scanner hint names that major (`major_not_hinted`); a version whose first number is
  over 9999 is a date, never an upgrade (`dated_current`, "(date-stamped)"); a current version that already meets every
  hint is not bumped (`meets_hint`; a policy pin still wins); a declaration only inside a profile that is not active by
  default (`profile_only`) and a g:a declared twice in one `<dependencies>` block (`declared_twice`) are not edited.
- *`remediation/remediation.log`* is written for every remediation that ran: chained, `--flow remediation`, and a resume
  in a new pod (`chain.remediation_log`). An in-process chained resume builds on `<build.local_repo>-remediation`.

**Reports and the code-change view.**
- The Korean guide's head says "빌드까지 진행하지 못했습니다" only for a run without any build; an aborted run that built
  says "실행 중단 (마지막 빌드는 통과했습니다 / 통과하지 않았습니다)".
- The code-change record lists only paths the last commit has (leftovers of the final build are not "added"; journal
  field `untracked_left_out`); a run that changed no file writes no record (`code_diff_skipped`, "the run changed no
  file") and a record of an earlier attempt is removed.

**Web, Job mode, deployment.**
- *Requests from another web site are refused.* The token guard answers 403 to any request other than GET / HEAD / OPTIONS
  that carries `Sec-Fetch-Site: cross-site` or an `Origin` whose host is not the request's own (`Host` /
  `X-Forwarded-Host`; `Origin: null` counts as foreign), before the token and open-mode checks, for every route (runs,
  remediations, admin). A client that sends neither header (curl, scripts) and the page itself are unaffected.
  `Sec-Fetch-Site: same-origin` (the browser's own statement) is accepted without comparing hosts, so a proxy that
  rewrites `Host` cannot lock the page out; on a plain-http page (browsers send no `Sec-Fetch-Site` there) such a proxy
  needs `X-Forwarded-Host` or the page's host in `SBMIG_WEB_ALLOWED_ORIGINS` (web pod; `host` / `host:port`,
  comma-separated; `*` switches the check off).
- *A build identity.* `sbmig_version()` is `<package version>+<12 hex>`: a digest of the `sbmig/**/*.py` files,
  `config/*.yml` and `knowledge/**` of the running tree, computed once per process (`sbmig version` prints it). Two
  different builds differ; the same image gives the same value in Main and Worker with no build step.
- *Pins are taken when the Job is created.* The Main that creates a Job records `config_digests` and `sbmig_version` in
  `control/state.json` (and `remediation-resume.json`) with the `creating` transition; the Worker compares against those
  (the ZIP's sha256 stays pinned from intake; a state file without them falls back to `request.json`). A run that waited
  in line across a web-pod restart, and a remediation resume of a run an older image made, are accepted when Main and
  Worker agree now. A mismatch is refused (`worker_refused`, exit 3) with "config <name> differs between the web pod and
  the build pod" / "sbmig version differs: web pod <v>, build pod <v>" and the closing sentence "the web pod and the build
  pod run different images: restart the web pod after pushing an image (and upload the project again)"; the status
  carries the Korean sentence as `abort_reason_ko`. Operator rule: after pushing an image, restart the web pod; a run
  uploaded between the push and the restart is refused and has to be uploaded again.
- *Unpacking limits of an uploaded ZIP* (`intake.max_unpacked_mb` 4096, `max_entry_mb` 1024, `max_compression_ratio` 200;
  0 = no limit; the ratio is judged per entry from 1 MiB unpacked, as in the remediation intake): checked from the ZIP's
  directory before anything is written and again while unpacking. A refused ZIP: `Workspace.intake_refusal`, journal
  `intake_refused`, the run ends at intake (`run aborted: ZIP refused: …`, exit 3, no Maven, no LLM; `Orchestrator.run`
  itself honours it); the in-process web answers 400.
- *Free space before an upload*: `fleet.min_free_gb` (5) is also the floor of the web path: below it an upload is refused
  with HTTP 507 before the body is read (the page shows a Korean notice); 0 = off. Nothing deletes old run folders
  (`sbmig clean` is manual and removes `project/` copies and uploads of finished runs only).
- *Redaction keeps structured files intact*: in `.json` a secret is replaced inside string values only (keys, numbers and
  layout untouched), `.jsonl` line by line, anything that does not parse as text; a secret value shorter than 6
  characters is replaced only as a whole token (also at write time).
- *Remediation resume*: the previous `remediation-job-status.json` is removed before a new attempt is queued; Main passes
  `SBMIG_JOB_ATTEMPT` and the Worker writes that attempt on every status.
- *Abort reason of an http-blocked run* reaches the status (`abort_reason`, Korean through the existing rule).
- *Explicit values win over the environment*: a command-line option (`--settings`, `--java-home`, …) and a fleet
  manifest's per-project override are applied after the `SBMIG_<SECTION>__<KEY>` values (one INFO line per replaced value);
  the environment still wins over the yml files.
- *`sbmig clean`* recognises uploads of today's run ids (`<slug>-YYYYMMDD-HHMMSS-<hex>`; since "v2.1z-be" also a
  project name with upper-case letters and `_` in front), so uploads of running, waiting
  and (with `--keep-failed`) failed runs are kept.
- *Image*: the Dockerfile no longer passes `--max-upload-mb` (`SBMIG_MAX_UPLOAD_MB` from the ConfigMap takes effect).
- *The Job template is read from the web pod's own image* (`deploy/k8s/81-web-job.template.yaml` under `/app`, the code's
  default path): the shipped Deployment sets no `SBMIG_JOB_TEMPLATE` and mounts no template. The image is built from a
  checkout in which 81 is filled; `validate_worker_template` refuses, in the real cluster mode, a template in which a
  value a Job would use still contains `CHANGE_ME` (`MIGRATION_TEMPLATE_INVALID: the job template … still has the
  repository placeholder CHANGE_ME (…)`; commented lines do not count). ConfigMap `sbmig-job-template` mounted at
  `/templates` with `SBMIG_JOB_TEMPLATE` pointing at it remains possible (commented in 40) for a cluster that must change
  the template without a new image; where earlier sections describe that ConfigMap as the way the template arrives, this
  is the shape that ships now. What an operator does after a change: image only → restart the web pod; ConfigMap
  `sbmig-env` → apply it, restart the web pod; the Deployment → apply it; the Job template → build and push the image,
  restart the web pod.
- *The "pod does not start" notice* waits 300 seconds by default (`SBMIG_JOB_PENDING_NOTICE_S`): on the company cluster a
  healthy build pod needs more than three minutes to start.
- *A project's own Maven settings are not used.* A project may carry `.mvn/maven.config` (the `mvn` launcher reads it
  from the project root by itself) pointing at a settings file of its own. Measured with Maven 3.9: a `-s` there loses
  to the `-s` on the command line, a `-gs` there was read as the global settings and merged with the server's. Every
  Maven command sbmig builds (`sbmig.build.mvn.base_command`: the build, OpenRewrite, `dependency:tree`,
  `help:effective-pom`, the classpath and jar checks, the remediation's builds) now passes
  `-gs <maven home>/conf/settings.xml` whenever it passes `-s` (`build.ignore_project_settings`, default true; false =
  the command line without `-gs`). The Maven home is the `Maven home:` line of the preflight's `mvn -v`, else the
  installation beside the resolved executable; when it cannot be found no `-gs` is passed (one WARNING), never a
  guessed path. Without `build.settings_file` nothing changes. A `-D` of the project's file also loses to the same
  `-D` on the command line (the local repository). The migration says so once: journal `project_maven_settings`, flag
  `project-maven-settings-ignored` and the hand-over line "the project's .mvn/maven.config points Maven at its own
  settings (<what it says>): ignored here — every build of this run used the server's settings" (flag
  `project-maven-settings-used` and "… Maven read them in this run as its global settings, merged with the server's
  (…)" when no `-gs` could be passed; one more sentence when the file turns on offline mode, which the command line
  cannot take back). The project's file is delivered as its owners wrote it; the exit code does not change.
  `remediation.project_maven_config` defaults to `ignore`: a project with `.mvn/maven.config` / `jvm.config` /
  `extensions.xml` is no longer refused; the finding is one line in the report's 처리 기록, never under 해야 할 일
  (`refuse` still works for an operator who sets it).
- *A project's `.mvn/` folder is not read by any Maven call* (`build.ignore_project_mvn`, default true; the user:
  "프로젝트에 .mvn이 있으면 복사파일에는 없앨까? 운영 빌드 되도록만 맞춰주면 될거같은데 나중에 압축할때 동봉만 해주면
  될거같고"). Measured with Maven 3.9 on JDK 21: a `.mvn/jvm.config` with JDK 8 options stops the JVM before Maven
  starts; Maven 3.9 reads one argument per line of `maven.config` and stops on a file written for 3.8; with the
  environment variable `MAVEN_BASEDIR` pointing at an empty folder none of `jvm.config`, `maven.config` and
  `extensions.xml` is read. So nothing is moved or deleted: `sbmig.build.mvn.mvn_env` gives every Maven child
  `MAVEN_BASEDIR=<an empty folder of this process>` (a temp folder created on first use, removed at exit; an inherited
  value is dropped), and `with_project_mvn(cfg, cmd, cwd, top=…)` puts on the command line what a build needs from the
  `maven.config` Maven would have found from that directory: its `-D<name>=<value>` properties and `-P` profiles (never
  `maven.repo.local`, `maven.multiModuleProjectDirectory`, `maven.test.skip`, `skipTests` or a name the command sets
  itself; sbmig's own options come later and win; both file styles are read) and
  `-Dmaven.multiModuleProjectDirectory=<the project's root>` so that a pom using that property sees the project, not
  the empty folder. Covered: the build, OpenRewrite, `dependency:tree`, `help:effective-pom`, the classpath check, the
  remediation's package / resolve calls; the calls that run outside a project (`mvn -v`, the jar check) get the
  environment only. Told once per run, in every flow that builds: journal `project_mvn` (`basedir`, `carried`, per
  folder `maven_config` / `jvm_config` / `extensions` / `wrapper` / `other_files`), flag `project-mvn-ignored`, and the
  hand-over line "the project's .mvn/ folder was not used for the builds of this run (<what it holds>); carried over
  to the command line: <options | nothing>; the folder is delivered unchanged — check it against JDK <N> and the Maven
  version in use before building with it" (it replaces the settings line above; with the switch off that line and
  `-gs` are what remains). `result.zip` holds the folder byte for byte. The remediation's boundary findings for `.mvn`
  files say "not used: the build did not read the project's .mvn folder" (+ the carried options). Not checked: `mvn.cmd`
  on Windows (it takes `MAVEN_BASEDIR` the same way by its script) and a temp path with non-ASCII characters.

**Left as they are** (the owner's decisions): a second web pod is not detected (the Deployment's `replicas: 1` and
`strategy: Recreate` are what keeps it at one: do not change them); no retention of old run folders; untracked leftovers
of the final build outside `target/` (`node/`, `node_modules/`, a shade plugin's `dependency-reduced-pom.xml`) are in
`result.zip` and in the remediation's source ZIP (not in the code-change view); the admin password in open mode guards the
aggregated views only.

## v2.1z-aw — framework projects: conversion only, and the framework is named

The user (2026-11-19): "사내 프레임워크는 llm도 돌리지 말고 그냥 jdk - jakarta까지만 하고 파일을 내려주도록 하자. llm이
이걸 해결해주기 쉽지 않을거 같아. 사내 라이브러리쪽도 개입이 많으니까! 그리고 프레임워크 프로젝트로 나오는데 <이름>
프레임워크라도 명시 할 수 있어? 사람들이 모를거 같아서. 이거 좀 강조를 해야 할 것 같아서." This refines "v2.1z-ak".

**No fix loop** (`framework.fix_loop`, default `false`). A run in the reduced scope `jdk-jakarta` does the conversion
(`transform.rewrite` with `framework.recipe_sets`, the source-encoding and provided-scope corrections, the JSP rename)
and then ONE build (`verify.build`): no zero-LLM pre-try, no stored script, no LLM call, nothing written to a pom or a
source file after the conversion. The build's errors are the run's unresolved errors (error list entries stay open with
the note that a framework project's errors are not fixed here); the hand-over says so in one line. The build gate holds
(the final build is attempted and reported). Build passes → the run is a success; build fails → exit 2, `result.zip` is
the converted tree. The chained remediation runs in both cases (below). `SBMIG-RESULT … scope=jdk-jakarta … llm=0`. With
`framework.fix_loop: true` the run behaves as "v2.1z-ak" describes (fix loop with the project's own chain facts and the
framework guard). `FrameworkUse.fix_loop` records which of the two a run did.

**The framework has a name.** `framework.name` (company yml; a company name never goes into this repository) or, when
empty, a name derived from the artifact (`FrameworkConfig.display_name(found)`: the artifact found in the project first,
then the configured patterns; a pattern with a glob is skipped; trailing role words `core` / `framework` / `fw` /
`starter` / `parent` / `bom` / `boot` / `common` / `base` / `api` are cut, first letter upper-cased: `example-fw-core`
→ `Example`). `FrameworkUse.name` carries it (profile.json, report.json); the journal events `framework_project` and
`plan` carry `name` and `fix_loop` beside `scope`. `""` = no name: texts say 사내 프레임워크 / "in-house framework".

**The sentence** shown wherever a person meets such a run, with the name in it:
`<이름> 프레임워크는 JDK 21, Jakarta만 적용합니다.` (no name: `사내 프레임워크 프로젝트는 JDK 21, Jakarta만 적용합니다.`)
- `report.md` (`ko`): the block `## <이름> 프레임워크 프로젝트` directly under the head table starts with that sentence in
  bold, then what the run did (JDK 21, Jakarta 변환, 빌드 1회 / 빌드 오류 수정 when `fix_loop`, 취약점 조치 when it ran) and
  what it did not (구조 정리, 구조 검토, 빌드 오류 자동 수정 unless `fix_loop`; 부모 pom과 프레임워크 버전은 그대로). `en`:
  `## <Name> framework project` with "**<Name> framework projects get JDK 21 and Jakarta only.**".
- The page: a highlighted notice (not the small chip line) in the run card as soon as the journal says so and in the
  result card, with the sentence in bold and, below it, what is not done; the upload card shows the sentence as a standing
  notice when the server's configuration names framework artifacts.
- HTTP: `GET /api/configs` gains `framework: {"configured": bool, "name": str, "fix_loop": bool}` (from the server's
  default configs); `GET /api/runs/{id}` gains `framework: {"name": str, "scope": "jdk-jakarta", "fix_loop": bool}` for a
  run whose report or journal says it ran in the reduced scope.

**The remediation runs after a failed build too.** The user (2026-11-19, after reading that a passing build is needed):
"취약점은 빌드가 실패해도 이어가도록! 어차피 오픈소스 라이브러리 검사라!" Switch `remediation.after_failed_build`:
`framework` (default) | `never`.
- *Chain condition* (`sbmig/graph/engine.py::_chain_remediation`): besides a successful migration, the remediation is
  chained when the run is in the reduced scope with `FrameworkUse.fix_loop` false and its final build was ATTEMPTED and
  FAILED. Every other condition stays (switch `remediation.after_migration` on, no dry run, no `stop_after`, the plan was
  reached, not aborted, not stopped by the user, no leftover `stop.request`). It then calls
  `chain.run_after_migration(..., baseline_may_fail=True)`. An ordinary run (or a framework run with the fix loop on)
  whose build failed is not chained, as before. The migration's verdict and exit code never depend on the remediation.
- *The remediation with `baseline_may_fail`* (`RemediationState.baseline_tolerated`, persisted, so a resume keeps the
  mode): a failing `baseline_build` is recorded (`baseline_build.ok` false, journal `remediation_baseline_failed` with
  `tolerated: true`) and the flow goes on to `baseline_scan` instead of ending `FAILED`. The scan, the inventory, the plan
  and the apply step are the ordinary ones. The candidate build of a round is compared with the baseline build: it may
  fail only with errors the baseline build already had; then the round goes on (resolution check as far as it can be
  made without packaged artifacts, commit, frozen delivery, rescan). An error the baseline did not have (a version that
  does not resolve, a new compile error) is a `candidate_build_failed` round as today. A baseline that passes although
  the migration's build failed is an ordinary remediation.
- *How it ends*. Versions that no build confirmed never end `SUCCEEDED`: when a committed round applied a proposal, the
  run ends `INCOMPLETE` with first reason `baseline_build_failed` (`candidate_build_failed` is not among the reasons when
  the candidate added no error). When that is the only reason, people read "remediated, not confirmed by a build"
  instead of "partly remediated" (`report.build_unconfirmed(summary)`: `baseline_tolerated`, `INCOMPLETE`, reasons exactly
  `[baseline_build_failed]`): 「조치함 · 빌드 미확인」 on the page's chip (the short form 「취약점 조치함(빌드 미확인)」 of the run card's sub line
  and 「지난 실행」 existed until "v2.1z-bd" took the suffix away), 「조치했지만 빌드로 확인하지 못했습니다」 in the migration report. When NOTHING is unconfirmed — nothing
  was changed (no finding to fix, or none that can be changed; the delivery is the original), or every candidate build
  PASSED (a bumped version removed the error) — there is nothing a build would still have to confirm
  (`_Holder.nothing_unconfirmed`):
  the run ends as an ordinary one, `SUCCEEDED` when the scan is complete and nothing remains, else `INCOMPLETE` with its
  own reasons, without `baseline_build_failed`; the report says in one sentence that the project did not build and that
  no version was changed. The summary in `report.remediation` (and what the page reads) carries `baseline_tolerated:
  true` in every case. The remediation report (`ko`) of a run with changed versions says at its head, in plain words,
  that the project did not build before the remediation either, that the versions were changed and scanned again, and
  that the build could not confirm them (with what the comparison showed); 해야 할 일 = fix the build errors, then build
  once with the changed versions; the list of what was fixed / what remains comes from the rescan as always.
- The remediation-only flow (`--flow remediation`, `POST /api/remediations`) is unchanged: a project that does not build
  is refused there.

**Names of this section, as coded.**
- *Run*: `Orchestrator.no_fix_loop` (reduced scope and `FrameworkUse.fix_loop` false) gates `_verify_fix_loop` and the
  plan advisor; `Orchestrator.remediation_after_failed_build(report)` is the extra chain condition. Journal
  `verify_loop_skipped {step, target, reason: "framework.fix_loop: false", errors, entries}`. Entry note
  (`framework.NO_FIX_LOOP_NOTE`): `not tried: framework project — build errors are not fixed automatically
  (framework.fix_loop: false)` (in `errors.jsonl` and `report.json`). Hand-over line, first among the follow-ups:
  `framework project: build errors are not fixed automatically (framework.fix_loop: false) — <N> error(s) remain after
  the JDK 21 + jakarta conversion; see the error list` (no parsed error: `… — the build failed after the JDK 21 + jakarta
  conversion; see the build log`). KB: `record_run` stores the entries as observations; nothing is matched, stored as a
  script or credited. Korean guide: the error section of such a run is `## 변환 뒤 남은 빌드 오류 — N건`
  (`guide_ko.NO_FIX_TITLE`; the remediation report's 해야 할 일 names that heading, a test keeps the two together).
  Result line of a failed run that was remediated: `… exit=2 build=FAIL … llm=0 … scope=jdk-jakarta
  remediation=INCOMPLETE`.
- *Remediation*: `RemediationState.baseline_tolerated`, `RoundState.build_compare` / `build_tolerated`,
  `DeliveryInfo.build_tolerated` (older files read as false / empty). Journal: `remediation_start` gains
  `baseline_may_fail: true`; `remediation_baseline_failed {tolerated, errors, log, exit_code}`;
  `remediation_candidate_compared {round, same, baseline_errors, candidate_errors, new, why}`. The comparison
  (`maven.compare_with_baseline`): both build logs through `build.log_parser.parse_maven_log`, paths relative to each
  tree, one key per error (kind, module, goal, file, line, L1 signature); "same" = same build target, same failing
  reactor module, every candidate key among the baseline's. Its limit is said in the report: Maven stops at the failing
  module and javac caps its list, so "no new error seen" is not "the changes compile". A baseline build that timed out
  or could not run is not tolerated (`FAILED baseline_build_failed` as before). Ends: changed versions → `INCOMPLETE
  [baseline_build_failed, …]`; a new error → `FAILED [candidate_build_failed, baseline_build_failed]` with the new
  errors in the message; nothing changed → the original delivered with `build_tolerated`, the status of an ordinary run. `chain.summary` carries
  `baseline_tolerated: true` and `delivery.build_tolerated: true` only when true; `GET /api/remediations/{id}` carries
  `baseline_tolerated` always and `delivery.build_tolerated`.
- *Page*: `GET /api/configs` → `framework.remediation_always` (framework configured, `fix_loop` false,
  `remediation.after_migration` on, `after_failed_build: framework`): the notices add 「취약점 조치는 빌드 결과와 관계없이
  이어서 합니다.」 `GET /api/runs/{id}` → `remediation.baseline_tolerated` and `remediation.reasons` when the summary has them. In the 「취약점 조치」
  block the note 「…바꾼 라이브러리 버전을 빌드로 확인하지 못했습니다…」 and the ZIP badge 빌드 미확인 appear only for a
  delivery with `build_tolerated` (and applied changes); a change set that added a build error keeps 빌드 실패본. The run
  card's sub line and 「지난 실행」 named the remediation's outcome after the migration's ("v2.1z-bd": only for the
  states a person must act on).

## v2.1z-ax — a library whose repository has the jar but no pom

**Why.** The user's cluster (2026-11-19): an in-house library sits in the releases repository as a jar only. The build
stopped with `Could not transfer artifact G:A:pom:V from/to maven-central (…)`, status 502. Measured with Maven 3.9.10:
Maven asks EVERY repository for the pom of a dependency. When all answer "not found" it warns (`The POM for … is
missing, no dependency information available`) and uses the jar. When one answers with a transfer error (a proxy of
Maven Central in a closed network answers 502 for a path it never cached; a refused connection), the build stops:
`Failed to read artifact descriptor for G:A:jar:V` caused by `Could not transfer artifact G:A:pom:V from/to <id>
(<url>): …`. With a file `<local repository>/<g path>/<a>/<v>/<a>-<v>.pom` present, Maven asks no repository for the
descriptor and downloads the jar from the repository that has it, in either repository order (a leftover
`.pom.lastUpdated` does not matter). The user asked for a way that needs nobody else ("자체 라이브러리라 요청이 될지
모르겠네. 이거는 해결 방법이 없어?"); a system-scoped jar in the project pom was rejected (such a jar is not packaged into
the WAR, and the project would change).

**What.** `build.pomless_stub` (default `true`), `build.pomless_stub_max` (default 20 per run; 60 since v2.1z-cj). `sbmig/build/pomless.py`.
- *Detection* in the log of a failed Maven command: a descriptor failure of a `jar` dependency without classifier whose
  cause is a transfer error on its `pom` (the first form above and the cached repeat of it). Never: a parent pom
  (`Non-resolvable parent POM`), an imported BOM (`Non-resolvable import POM`), a plugin or build extension, a plain
  `Could not find artifact`.
- *Proof before anything is written* (the repository set, mirrors and credentials of `RepositoryLookup`, with the project
  poms' `<repositories>`): the JAR is obtainable from at least one repository (or is already in the local repository)
  AND the POM is obtainable from none. Otherwise nothing is written and today's handling applies; journal
  `pomless_check {ga, version, reason}` with `reason` = `pom-available` (a real transfer problem), `jar-unavailable`
  (the repository is down or the library does not exist), `no-repository`, `cap`, `off`.
- *The stub*: `<local repository>/<g path>/<a>/<v>/<a>-<v>.pom`, written atomically, never over an existing file (a
  file another pod wrote counts as done): `modelVersion`, `groupId`, `artifactId`, `version`, `packaging` jar, and an XML
  comment saying that sbmig wrote it because the repository has no pom for this library and that it must be deleted when
  a real pom is uploaded. No dependencies: what Maven itself assumes for a missing pom. No other file is touched.
- *Then* the same Maven command runs once more. A library is handled once per run; a command may need several rounds
  (each failure names few libraries) up to the cap.
- *Where*: one shared function behind every Maven call of sbmig that resolves dependencies: `MavenRunner.run` (builds,
  `dependency:tree` …), the OpenRewrite run, the effective-pom capture of intake, the remediation's Maven calls.
- It is a repair of the environment, not of the project: no project file changes, no LLM call, it also runs for a
  framework project (no fix loop) and before any blocked-item classification (`repository_unreachable`).
- *Said*: journal `pomless_stub {ga, version, path, jar_from, created}` (`created` false = the file was already there),
  flag `pomless-library`, one INFO line per stub, one hand-over line naming the libraries (they have no pom in the
  repository; a minimal pom was written on the server; their own dependencies are not known to Maven; the lasting fix is
  a pom in the repository), with a Korean rule.

**Names of this section, as coded.**
- *Detection* (`pomless.py`): the descriptor line must name exactly `G:A:jar:V`, and a transfer clause for the same
  `G:A:pom:V` must stand on that line or within the next three. Maven 3.9 prints the same pair of lines on the first
  attempt and on every repeat (it does not cache a transfer error: there is no "was cached" form of this failure); the
  one-line Maven 3.8 form and the resolver's "during a previous attempt" form are accepted too. Captured logs:
  `tests/build/logs/real-pomless-*.log`.
- *Proof* (`RepositoryLookup.file_answers(g, a, v, ext)`): the pom is asked with GET, the jar with HEAD (a server that
  refuses HEAD gets a one-byte ranged GET; a jar is never downloaded). 200 / 206 = obtained, 404 / 410 = not there,
  401 / 403 = denied (counts as "not obtained" for the pom and as "not obtainable" for the jar), anything else = failed.
  Two more `pomless_check` reasons: `bad-coordinate` (not a plain coordinate), and `no-repository` also when the local
  repository path cannot be resolved.
- *The local repository* (`pomless.maven_local_repo`): `build.local_repo` → `-Dmaven.repo.local` in `build.extra_args`
  → `<localRepository>` of the settings Maven uses → the global settings → `~/.m2/repository`. On the cluster each
  slot has its own (`/data/.m2/repository`, `repository-slot2`, `repository-slot3`): a stub is written into the
  repository of the pod that met the failure.
- *The file*: starts with the comment `Written by sbmig: the repository has a jar but no pom for …`
  (`pomless.STUB_MARKER`); written with a temp file and a hard link, so it never replaces a file.
- *Order of the retries* in `MavenRunner.run`: HTTP blocker (no retry) → Windows file lock → stub pom → `-U` (which
  cannot help with this failure and is skipped once a stub was written). Reruns are labelled `-pomless<k>`.
- *Call sites*: `MavenRunner.run`, `rewrite/runner.py`, `build/effective.py`, `build/deptree.py`,
  `build/classpath.py`, `plan/jarcheck.py`, `remediations/maven.py` (`package`, `resolve`). `rewrite/versions.py`
  (the recipe BOM) is not wrapped.
- *A later run is told too*: at finalize the run's known dependencies (an unfiltered dependency tree when one was
  captured, else the effective-pom captures' direct dependencies and the declared ones) are checked against the local
  repository; a pom that starts with the marker joins the same flag and line; journal `pomless_stub_used {ga, version,
  path}`; at most 300 coordinates are checked. A library reached only transitively can be missed when no unfiltered
  tree exists.
- *Not said yet*: the remediation-only flow writes the stub and records it in the journal and the log only.

## v2.1z-ay — a pom whose bytes do not match its XML declaration; a failed conversion is said

**Why.** The user's cluster (2026-11-19): the chained remediation refused a project with 「읽을 수 없는 pom」, detail
`OSError`. Measured: lxml raises `OSError` when a pom's bytes do not match its declaration (the header says UTF-8, or
says nothing, and the Korean text is saved as MS949 / EUC-KR), and for `encoding="MS949"`. Maven 3.9.10 never refuses
a pom for its bytes and never warns: it builds with the header UTF-8, none, EUC-KR, MS949, x-windows-949, CP949 over
MS949 bytes (names print garbled when the header is wrong); it refuses only XML that is not well-formed and an
encoding name Java does not know. Taking such a pom through the migration showed the larger defect: OpenRewrite
cannot parse it (`Failed to parse pom: Invalid UTF-8 middle byte …`), the whole recipe run fails with 0 changed files,
and the run went on to `exit=0 build=ok` with the failure only in the English recipe table and a phase note.

**The remediation reads every pom Maven reads** (`sbmig/remediations/`). One way of reading, the migration's
(`analyze.pom_parser.pom_encoding`: BOM → strict UTF-8 → the declared encoding, the Korean family as cp949 → the first
of the candidates that decodes the whole file), behind `apply.read_pom_xml` / `apply.pom_xml_bytes`: the boundary
check, the inventory, the planner's locations, the apply step and its structure check all use it. An applied change
is written back in the encoding the file was read with: only the bytes of the changed text differ, the declaration
and every other byte stay (tests compare bytes). A pom that really cannot be read is still refused, and the finding
says what is wrong: `unparsable_pom: <path>: <detail>` with `not well-formed XML (line N): …`, `cannot be opened
(permission denied | missing | a folder, not a file)`, `character encoding unknown (…)`; Korean on the page and in the
report: 「pom 파일을 읽을 수 없습니다. app-core/pom.xml: XML 문법 오류(줄 12).」, step 「pom 파일 고치기」.

**Before the conversion, such a pom is corrected** (`layout.legacy_context.correct_pom_encoding`, default `true`; both
flows, before the first OpenRewrite run, beside the source-encoding step). A project pom whose bytes a strict XML
reader cannot read under the pom's own declaration is made consistent, in the way that changes the fewest bytes while
every later tool reads it right (OpenRewrite parses it and writes it back with the Korean text intact, Maven builds
it): preferably only the declaration is changed to the encoding the bytes are in; when the measurement with the real
rewrite plugin shows that is not enough, the file is saved again as UTF-8. The Korean text is never lost. Recorded
like the source-encoding correction: its own commit (`transform: pom encodings corrected`), journal `pom_encodings
{entries: [{pom, declared, actual, how}]}`, one error-list entry per pom with `seen = transform`, flag
`pom-encoding-corrected`, one hand-over line with a Korean rule. A pom that is consistent is never touched.

**A failed conversion is said, and a conversion-only run is not a success without its conversion.** When an OpenRewrite
run fails (for any reason that does not already abort the run): flag `rewrite-failed`, one hand-over line FIRST among
the follow-ups naming the recipe sets that were not applied and the cleaned reason, with a Korean rule; the Korean
guide's lead says it; `SBMIG-RESULT` shows it among the attention codes. An ordinary run keeps its build-defined
verdict (the restructure and the fix loop may still reach a passing build). A run in the reduced scope (a framework
project: the conversion is all it does) whose conversion failed is NOT a success even when its build passes
(`Orchestrator.build_succeeded`), ends exit 2, and its report block does not claim "JDK 21과 Jakarta 변환" among what
was done. The chained remediation still follows such a run when `remediation.after_failed_build` is `framework`, with
`baseline_may_fail` only if the build itself failed.

**Names of this section, as coded.**
- *What the rewrite plugin does* (measured: rewrite-maven-plugin 6.46.1, `UpgradeToJava21`, JDK 21, Maven 3.9.10): a
  UTF-8 header or no declaration over MS949 bytes is not parsed (0 files changed); a pom declared `EUC-KR` with EUC-KR
  bytes is parsed and written back with only the changed lines different; a pom declared `MS949`, `x-windows-949` or
  `CP949` is parsed, but when its text holds a syllable EUC-KR cannot encode the plugin writes replacement characters
  over the Korean text. lxml accepts `EUC-KR` and `CP949` and refuses `MS949` and `x-windows-949` as names.
- *The correction* (`sbmig/restructure/pom_charset.py`, `Orchestrator._correct_pom_encodings`, at the start of every
  `transform.rewrite`, before the source-encoding step): every project pom outside `src/`. Left alone: a file with a
  BOM, a pom a strict parse accepts whose Korean-family declaration holds only EUC-KR bytes, XML that is not
  well-formed, bytes only latin-1 or shift_jis explain. Bytes that are strict EUC-KR → only the declaration changes,
  to `EUC-KR` (the one Korean name Maven, OpenRewrite and lxml all accept; added with the file's own line ending when
  there was none). Anything else (MS949-only syllables) → saved again as UTF-8 with a UTF-8 declaration. Before a
  write: the result parses strictly and the text without the declaration is identical. Journal `pom_encodings {step,
  entries: [{pom, declared, actual, how}]}` with `how` = `declaration` | `utf-8`; the later writers (`PomEditor.save`,
  the scope restore, the fix loop's pom helpers, `Workspace.write`) keep such a file consistent (tests).
- *The standard library's XML parser* refuses a multi-byte declaration on bytes: every reader of a project pom
  decodes first (`pom_encoding`), also `log_parser.reactor_artifact_ids_from_poms` and `scripts/e2e.py`.
- *A failed conversion* (`sbmig/rewrite/failure.py`): journal `rewrite_failed {step, sets, reason}` (the configured
  recipe-set names; the reason is the first line, scrubbed, paths made relative, ≤ 300 characters), flag
  `rewrite-failed` (first among the attention codes of `SBMIG-RESULT`), the hand-over line placed before the
  no-fix-loop line, Korean lead 「자동 변환 실패」 as the first item of 「확인해 주세요」.
- *The success rule*: `Orchestrator.build_succeeded` adds `not (self.reduced_scope and self.conversion_failed)`.
  Such a run prints `exit=2 build=ok … attn=rewrite-failed`, head cell 「❌ 변환 실패 (빌드는 통과)」 /
  `❌ NOT CONVERTED (the build passed)`, and its framework block leaves the conversion out of what was done.
  `remediation_after_failed_build` is true for a reduced run with `framework` whose build failed without the fix loop,
  or whose build passed and whose conversion failed; `_chain_remediation` passes `baseline_may_fail` only when the
  build failed. Open: `sbmig/fleet/runner.py` names its column from `report.success`, so the fleet summary calls that
  case a failed build.
- Fixture `fw-pom-ms949` (a framework project whose pom is MS949 under a UTF-8 header).

## v2.1z-az — the page: a stop a person asked for is orange; 「지난 실행」 shows three days

The user (2026-11-19): "중단 요청은 프로젝트에 표시 할 때 주황색으로 바꿔줘 ㅠ 너무 빨가니까 문제 투성이 에이전트 같아 ㅠㅠ
지난 실행은 3일치만 보여주도록 변경도 해줘!" and "최근 3일내에 등록한 프로젝트만 보입니다. 라고 문구도 하나 넣어주고!"

- **Stopped on request.** `GET /api/runs/{id}` (and the list) carries `user_stop: true` for a finished run without a
  result (exit 3 or none) when a stop was requested, Main recorded the control state `stopped` (withdrawn while
  waiting, deleted before its pod started or after the grace period), the abort reason starts with
  `STOP_REQUEST_REASON`, or the error starts with `STOP_BEFORE_START_ERROR`. A stop that came too late (exit 0 or 2) is
  not one. The page shows such a run in the caution colour (`warn`) everywhere: the title 「중단되었습니다」, its line in
  「지난 실행」, the rail step (state `.stopped`: a pause mark), the note under the rail, the 중단 요청됨 button, the
  log line of the stop click, the toasts, the reason box of the result card, and the remediation chip when its reason
  is `stop_requested`. Red stays for a refused intake and every other abort of the engine, a pod that failed with no
  stop asked, a lost pod, a failed stop request, `FAILED`.
- **Three days.** `GET /api/runs?days=N[&keep=<run id>]`: runs that started at or after now − N × 24 h (inclusive);
  `0` or no parameter = all; anything that is not a non-negative integer → 400; an invalid `keep` → 400. Listed
  whatever their age: queued or running runs, a remediation in `AWAITING_APPROVAL` / `AWAITING_CONFIRMATION` / `QUEUED`
  / `RUNNING` or with an active resume, and the run named by `keep` (the one the page has open). The response adds
  `days` and `hidden` (how many were left out). The page asks for `RUNS_DAYS = 3`: heading count 「6건 · 최근 3일」, the
  standing line 「최근 3일 내에 등록한 프로젝트만 보입니다.」 under the heading, 「이 기간에 등록한 프로젝트가 없습니다.」 for
  an empty window with older runs. Older results stay reachable: 「이전 실행 보기」 (with 「N건 더」) at the end of the
  list loads everything (count 「8건 · 전체」, line 「전체 실행을 보고 있습니다.」, control 「최근 3일만 보기」); the choice
  lasts until the page is reloaded. Nothing is deleted: the cut is a view.
- **「처리 불가」 instead of 「진행 못 함」.** The user: "진행 못 함은 우리 문제 때문에 진행 못하는 느낌이야 ㅋㅋㅋ 색은
  주황색으로 하자!" A run the engine could not take on or carry through (finished, exit 3, an engine reason in
  `abort_reason`, not an internal error, not a user stop: `engineRefusal(st)`) reads 「처리할 수 없습니다」 (title),
  「처리 불가」 (list, sub line, the admin page), 「처리 불가: <reason>」 under the rail, in the caution colour, with its
  own rail mark (`.refused`: a bar, not the pause mark of a user stop) and an orange reason box. A fault of the serving
  side stays red and has its own words, 「실행 중 오류가 났습니다」 / 「실행 오류」 / 「오류: <reason>」: an abort reason that
  starts with `internal error:` or `missing component:` or is an exception class name (`isInternalReason`), or no
  engine reason at all (a lost pod, a result that could not be verified, a refused worker). A stop that left the fix
  loop early counts as a user stop (`USER_STOP_PREFIX`). Open: environment problems the operator must fix
  (`settings-not-found`, `mvn-not-found`, `jdk-not-21`, a failed setup command, `aborted:http-blocked`,
  `aborted:parent-not-found`, `remediation:not-ready`) are engine aborts with a plain reason and therefore also read
  「처리 불가」; the status carries no field that tells them from a refusal of the project (the flags are in
  `report.json` only).

## v2.1z-ba — the approval step of the remediation is removed

The user (2026-11-19): "우리 approval awaiting 기능 아예 빼버리자. 이렇게 안쓸거래. 환경변수도 없애고 기능도 없애고 화면에
보여주는것도 없애버리게! 아주 넣었다 빼달라하는데 그냥 코드도 없애버려 나중에 본인들이 구축하겠지 사용자한테 승인받는건".
Since "v2.1z-ar" nothing waited for approval by default; now the mechanism itself is gone. Wherever an older section of
this document describes approval (`AWAITING_APPROVAL`, `require_approval`, `always_approve`, `POST …/approvals`, the
approval list of the page), this section wins: it no longer exists.

**Gone.**
- Settings `remediation.require_approval`, `remediation.always_approve`; the environment variable
  `SBMIG_REMEDIATION__REQUIRE_APPROVAL` (its line in `deploy/k8s/20-configmap.yaml` and in
  `81-web-job.template.yaml`, its row in `deploy/ENV.md`).
- The state `AWAITING_APPROVAL`; the proposal decisions `pending`, `approved`, `rejected` (every proposal of a plan is
  applied: one decision, `auto`; `ineffective` stays); the reasons `pending_approval`, `rejected_proposals`; everything
  in the planner, the graph, the service and the report that computed, stored, waited for or rendered an approval.
  The risk labels of a proposal (major, parent, bom, managed_override) stay where the report shows them as facts.
- HTTP: `POST /api/remediations/{id}/approvals` and `GET /api/remediations/{id}/proposals` (404 like any unknown
  route); `pending_approvals`, `links.approvals`, `links.proposals` in the status; resuming "after approvals".
- The page: the approval list with its check boxes and buttons, the words 승인 대기 in every chip, title, short form
  and count, the fetch of proposals; the rule that keeps an approval-waiting run in the three-day list; the admin page's
  word for it.
- The report and the documents: sections and sentences about proposals waiting for approval or rejected by a person.

**Stays.** `AWAITING_CONFIRMATION` (an IVAS upload whose outcome is unknown; a person checks IVAS and resumes with
`confirm`) is another mechanism and is untouched, with its resume and its always-listed rule. `STOPPED` /
`INTERRUPTED` and their resume stay.

**Old data and old settings never break anything.**
- A `state.json`, plan or report file written before this change may hold `AWAITING_APPROVAL`, the old decisions and
  the old reasons: every reader accepts them. Such a remediation is shown as ended, without any action and WITHOUT the
  word 승인 anywhere on the page (the user, the same evening: after a deployment people saw 승인 대기 again — old runs
  of an earlier incident, which the list had started to label and to keep listed — "이거 왜뜨냐고 그래서 내가 스트레스
  받아서"): 「지난 실행」 and the run card's sub line add nothing for it; its block shows the neutral chip 「조치 미완료」
  and 「이 실행의 취약점 조치는 끝나지 않았습니다. 프로젝트를 다시 올리면 끝까지 진행합니다.」; the old reasons are not
  rendered; the files it had already produced stay downloadable; a resume request for it is refused with 409 and a
  plain reason. It never counts as active (no always-listed rule, no polling).
- `remediation.require_approval` / `remediation.always_approve` in a yml and `SBMIG_REMEDIATION__REQUIRE_APPROVAL` in
  the environment are ignored with one WARNING each ("the approval step … was removed … remove it"), whatever their
  value (`config._warn_removed_keys`); never a configuration error. The variable is left out of the comparison of the
  web pod's and the build pod's environment (`contract.ENV_PIN_SKIP`), so a ConfigMap that still carries it while the
  81 template no longer forwards it does not refuse a Job.
- The policy hash no longer contains the two settings: a remediation that was stopped before the upgrade and is resumed
  after it plans again from its last scan (say what happens in the report of the implementation).

## v2.1z-bb — the admin page hands out the uploaded original

The user (2026-11-19): "우리 어드민에 원본 소스 받을 수 있는 기능 하나만 만들어주라. 이거 scp를 2번을 땡겨와야해서 너무
불편해서 웹 기능으로 만들게!" Until now the uploaded ZIP could only be taken off the data volume by hand.

- `GET /admin/api/runs/{run id}/source` — the bytes a person uploaded for that run, unchanged. Admin session required
  (the same answer as the other admin routes without one); an invalid run id → 400; an unknown run → 404; the upload is
  not on disk any more → 404 `{"error": "source not available"}`. Sent as a stream (never read into memory as a whole)
  with `Content-Length`, `application/zip`, and `Content-Disposition: attachment` naming `<run id>-<the upload's own
  file name>` (`<run id>-source.zip` when the name is not known; non-ASCII names per RFC 5987, the name cleaned of
  path parts). Where the upload lives depends on the run mode (the run folder's `source.zip` in the Job modes, the
  upload folder in-process, the remediation-only flow's own copy): the route finds it by the run's record, confined to
  the output root, never following a link.
- `GET /admin/api/runs/{run id}/source/check` — the same rules without the bytes: `{ok, bytes, filename}`; the page asks
  it first, so a file that is gone gives a sentence instead of a broken download.
- Lookup order (`admin.find_source`): the run folder's `source.zip` (the pinned upload of a Job run) → the upload
  folder's file by the run's recorded name (else the single file starting with the run id) → the remediation-only
  flow's `remediation/original/source.zip` (never for a full run: there that folder holds the migrated tree).
- `GET /admin/api/usage`: every run row gains `has_source` and `source_bytes`.
- The admin page: 「원본 받기」 beside the run's log download, with the size; disabled with the reason when the file is
  gone. The public page gets nothing: the original is company source and stays behind the admin password.
- One INFO log line per download (run id and size, no client data). The logs bundle still leaves `source.zip` out.
- The 「프로젝트별」 table opens in the order the uploads were received, newest first (the user: "접수받은 순서대로 …
  최신이 제일 위로 오게!"): the page sorts by `last_run_at` (the latest of a project's runs' receive times), ties by
  name, a project without a time last; the marker sits on 「마지막 실행」. The columns stay clickable (합계 토큰 sorts by
  tokens). The API's own order (by tokens) is unchanged and never reaches the screen.

## v2.1z-bc — IVAS file components: the right address, and report only

The user's cluster (2026-11-19): one project ended `INCOMPLETE scan_incomplete — baseline scan incomplete: http_error,
page_missing` although the upload answered 200. The matched-files call got 404: sbmig asked for
`…/result/filecomponent/{id}/matchedfiles`; the address is `…/result/filecomponents/{id}/matchedfiles` ("원래는
filecomponents 여야 하는데 filecomponent로 요청을 보냈네!"). The call had been written from the API notes and never met
the real IVAS. And the user's policy: "우리 백엔드로직만 라이브러리 검사하고 프론트는 검사 안하려고 하는데 … 이건 적용은
하지말고 리포트만 보내도록 할까?"

- **The address**: `filecomponents` (plural). The component id is still sent as one encoded path segment and kept as it
  came. The user checked the real IVAS the same evening with the value sbmig had sent (`833|222121`, the form the
  company calls ossId): the corrected path accepts it ("ossId는 | 받아준다!"). The client still takes the value from
  `fileComponent.id` and only when that is missing from `ossId` (`ivas._file_component_id`): whether the two fields can
  differ in a real answer is not known. Not seen yet: a whole scan of sbmig going through that call on the cluster.
- **`remediation.file_components`**: `report` (default) | `count` (`SBMIG_REMEDIATION__FILE_COMPONENTS`, forwarded by
  81, listed in 20 and ENV.md). The remediation changes library versions in poms only; a finding IVAS made in a FILE of
  the project (kinds `file_main`, `file_potential`) was never fixed by it either way.
  - `report`: such findings are information. They are listed in the report in a section of their own (component,
    version, the matched files, the patched version when IVAS names one), produce no inventory item, no proposal and no
    residual, are not counted as remaining findings, do not hold the verdict and do not start another round. The
    verdict and the counts a person reads (`baseline_findings`, `final_findings`, fixed, remaining) are about libraries.
    A failure while reading the file-component lists or the matched files of one component does NOT make the scan
    incomplete: the library part is planned and applied as usual; the failure is a note of the scan and the section
    says what could not be read. Completeness (`scan_incomplete`) is decided by the summary and the library and `sw`
    lists as before.
  - `count`: the behaviour before this change (file findings are `vendored` residuals that keep the result
    `INCOMPLETE`; a page that cannot be read makes the scan incomplete).
- A file component can be a static file of the front end or a jar that lies in the project outside the pom; `report`
  takes both out of the verdict (the section shows the paths, so a reader sees which it is). Told to the user.
- **Names, as coded.** `ivas.split_files` / `_Holder.split()` take the file findings out first, then split by
  `ignore_actions` (a file finding with an ignored action code stays in the file group). Counts in `state.counts` and the
  summary, only under `report`: `baseline_file_findings`, `final_file_findings`, and `file_lists_unread` (scans whose file
  lists could not be read; such a scan gets no `*_file_findings` rather than a misleading 0). Journal:
  `file_findings_reported {label, findings, unread, file_complete}` after each scan; `file_residuals_dropped` when a
  resumed older state's file residuals leave. Scan result: `ScanResult.file_complete` (false: the file lists, a malformed
  or duplicate item in them, or the `fileComponents` summary comparison failed) and `Finding.files_unread` (the matched
  files of that component could not be read; `file_complete` stays true). `report.json`: `file_components`,
  `file_findings`, `file_findings_complete`. Under `count`, a scan stored under `report` whose file part was not read is
  incomplete (`ivas.count_view`, reason `file_components_unread`); the file on disk is not changed. The setting is not
  part of `planner.policy_hash`: it changes no proposal, only what counts as remaining, and that is decided when a run
  reads the scans.
- **The report (`ko`)**: under the summary, when N > 0: 「프로젝트 파일에서 찾은 취약점 N건은 위 개수에 넣지 않고 아래 「프로젝트
  파일에서 찾은 취약점」에 따로 적었습니다.」; the section `## 프로젝트 파일에서 찾은 취약점 — N건` with its lead and one row per
  component (위험도, 컴포넌트, 버전, 패치된 버전, 취약점, 파일 위치; a long file list cut to three with 「외 N개」;
  「파일 위치를 읽지 못했습니다」; 「(추정)」 for potential components); when the lists could not be read, the section has one
  sentence instead of a table. The migration report's 취약점 조치 section adds one line when N > 0.
- **Old data**: a state or scan written before this change loads (`file_complete` true, `files_unread` false). A
  remediation resumed under `report` drops the file residuals an earlier version stored and recomputes its counts. A
  run that ended `INCOMPLETE` at the baseline scan (the cluster's case) cannot be resumed: it needs a new run.
- The fake IVAS of the integration tests answers 404 to an address it does not know (it used to answer an empty
  page, which is how the singular path was never caught).

## v2.1z-bd — the page: 「완료」, and the code changes in a large dialog

The user (2026-11-19): "우리 웹페이지에 성공, 취약점 일부만 조치 이부분 그냥 완료 로만 나오게 해줘!"; "소스 변경사항
보여주는거 크게 모달로 변경 가능해? 안에 내용 그대로 때서 모달로 만들 수 있지?"; "지금은 내용쪽에 휠로 내리면 계속 파일들
보이잖아? 선택한 파일만 볼 수 있도록 해줘! 정적자원들은 위치 이동 표시만 하고 컨텐츠 내용은 없어도 돼!"

- **「완료」.** In 「지난 실행」 a run whose migration succeeded reads 「완료」 (green) instead of 「성공」; the run card's title
  keeps its sentence. The remediation's short form after it (and in the run card's sub line) is shown only for the
  states a person must act on or know before opening the run: `FAILED` 「취약점 조치 실패」 (red), `AWAITING_CONFIRMATION`
  「취약점 확인 필요」, `STOPPED` 「취약점 조치 멈춤」, `INTERRUPTED` 「취약점 조치 중단」 (orange), `QUEUED` 「취약점 조치 대기」,
  `RUNNING` 「취약점 조치 중」. Nothing is appended for `SUCCEEDED`, `INCOMPLETE` (the build-unconfirmed form included),
  skipped, none, an old approval-waiting state or an unknown value; the same rule follows every migration outcome
  (미완료, 처리 불가, 중단, 실행 오류). The 「취약점 조치」 block of the result card keeps its chip and detail. A
  remediation-only run keeps its own words (「조치 완료」, 「일부 조치」 …).
- **The dialog.** The native `<dialog>` with `showModal()`: `min(96vw, 1680px)` wide, `92vh` tall, the whole screen at
  640 px and below; a header (「코드 변경」, the upload's name, the summary chips, 「닫기」) that stays while the body
  works; closed by the button, Esc or a click on the backdrop, each ending in the `close` event, which unmounts
  `CodeDiff` and returns the focus to 「코드 변경 보기」; Tab stays inside; the page behind does not scroll; opening
  another run closes it first. The summary and the folder-structure box take at most 28 % of the body (the box's head
  「폴더 구조가 바뀌었습니다 · 묶음 N개, 파일 M개」 is a button with `aria-expanded`: open for at most three groups on a
  screen of 640 px or more, folded otherwise; its table scrolls inside a bounded height); the file list and the pane
  take the rest, each scrolling on its own.
- **One file at a time.** The pane shows the file selected in the list, from its top; the wheel stays inside that
  file. On opening, the first file in list order that has lines to show is selected; 「이전 파일」 / 「다음 파일」 and
  「N / M」 stand above it. The list is one Tab stop: ↑ / ↓, Home / End, → / ← for folders, Enter or Space to select.
  Drawing files 30 at a time (`PAGE_FILES`) and fetching "near the viewport" are gone; folders are drawn when opened
  (all open below 150 files) and loaded diffs are cached.
- **Static files show their move, not their content.** By extension (`js mjs cjs css scss sass less map html htm`,
  images, fonts, media, archives including `jar war ear`, office documents and `pdf`; pinned by a test): never
  requested from `/codediff/files/{n}`, never rendered as a diff. Selected, such a file shows 「위치만 옮긴 파일입니다.」
  with 원래 위치 and 옮긴 위치, or that it was added / deleted, or that its content changed (with the line counts)
  and that static files are not shown line by line. Everything else — JSP, tag files, Java, XML, properties, YAML, SQL,
  build files, and any extension not on the list — shows its content.

## v2.1z-be — an upload needs a project name

The user (2026-11-19): "우리 접수 받을때 파일 올리고 프로젝트명 안적으면 거절당하게 해줄 수 있어!? 프론트, 백엔드 둘 다 체크
하도록!" Until now a run was known by the name of the uploaded file only.

- **HTTP.** `POST /api/runs?name=<file name>&project=<프로젝트명>[&flow=…]`: `project` is required for every flow. The
  user, a little later: "영어랑 _, - 로만 허용 가능하도록 해줄래! 리눅스에서 쉘 접속 했을때 치기 귀찮아서! 공백도 비허용!" So
  the value must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,29}$`: English letters, digits (my addition, told to the user),
  `_` and `-`; 1 to 30 characters; it starts with a letter or a digit (a leading `-` reads as an option in a shell); no
  space, no Korean, no dot. It is NOT trimmed or repaired: a value with a space before or after is refused like any
  other that does not match. A missing or empty value → 400 `{"error": "project name is required", "message_ko":
  "프로젝트명을 입력해 주세요."}`; any other mismatch → 400 `{"error": "project name: letters, digits, _ and - only, at most
  30 characters, starting with a letter or a digit", "message_ko": "프로젝트명은 영문, 숫자, _, - 만 쓸 수 있습니다(공백
  없이 30자까지, 첫 글자는 영문이나 숫자)."}`. The check comes BEFORE the body is read (as the flow check does), so a
  refused upload costs no transfer.
- **The run id starts with it.** The reason for the narrow alphabet is the shell: the run's folder is
  `<project>-YYYYmmdd-HHMMSS-xxxxxx` instead of `run-…` (`ls /data/out | grep <project>`, tab completion). The id still
  matches `RUN_ID_RE`; everything derived from a run id (the Job name and its 63-character limit, labels, the upload
  folder's file name, the remediation's id) must stay valid and unique for a 30-character name with upper-case letters
  and `_` in it.
- **Kept with the run.** The name is written into the run's request record and status file and survives a restart of
  the web pod; `GET /api/runs` and `GET /api/runs/{id}` carry `project`. A run made before this change has none: every
  reader falls back to what it showed before (the upload's file name).
- **The page.** A field 「프로젝트명」 in the upload card, marked as required, empty at first (not filled from the file
  name: the point is that a person names the project), `maxlength` 30, with the rule as its hint and the same check
  as the server (a character outside the alphabet is said at once, beside the field). 「마이그레이션 시작」 stays disabled until a file is chosen AND a
  name is typed; trying to start without a name puts the focus on the field with 「프로젝트명을 입력해 주세요.」 beside it;
  the server's refusal is shown the same way when it comes anyway. The run card and 「지난 실행」 show the project name
  as the run's name, with the file name beside it.
- **The admin page.** Runs are grouped by the entered project name; older runs keep their grouping by file name.
- **As coded.** One pattern, `runs.PROJECT_RE`, used by `runs.check_project()`; the route calls it right after the flow
  check and before the size check, the free-space check and the body (tests call the app with a `receive()` that
  counts: 0 reads on a refusal). The page has the same pattern. The name is stored in `control/request.json`
  (`project`), `web-status.json`, the in-memory run and the journal's `web_run` line; a restart gets it back from
  those. Job names: `job_name(run_id, limit)` cuts the FRONT of a long id and always keeps the
  `-YYYYmmdd-HHMMSS-<hex6>` tail, also for the resume name `…-rem<attempt>` (the earlier rule cut the tail, so two
  runs of one long-named project in one second could have collided); ids of the earlier shapes give the names they gave
  before. The label `sbmig-run-id` holds the id unchanged. `sbmig clean` ties an upload to its run for such ids too
  (`fleet.cleanup._UPLOAD_RE` takes the characters `RUN_ID_RE` takes): without that the upload of a run that is still
  going could have been planned for deletion. `POST /api/remediations` (in-process only; the page does not use it) is
  unchanged.

## v2.1z-bf — a single-module Spring Boot app is split into the company's modules

The user (2026-11-19): "parent가 spring boot로 시작하는 프로젝트가 xxx-boot만 생성되었는데 여기 정적자원이랑 다 있거든!? 이거
xxx-comp-jar, xxx-front-jar 로도 변환을 해줘야해! 이 케이스를 생각을 못했네!" Since round 9c a Spring Boot as-is project
kept its directories and a root that was the app itself was WRAPPED: everything went into `{name}-boot`.

- **`layout.boot_single_module`**: `split` (default) | `wrap` (the earlier behaviour). It applies when
  `layout.boot_as_is` is `in-place` and the root pom is the application itself (no `<modules>`, sources present). A
  Boot project with several modules and an MSA keep their directories whatever this says.
- **`split`** gives the modules a non-Boot project gets, with the same names and parents (`layout.boot_module`,
  `comp_module`, `front_module`; `artifacts.boot_parent` for the boot module, `comp_parent` for the two others; the
  root becomes the aggregator on `artifacts.parent`):
  - `{name}-boot`: the `@SpringBootApplication` class (and what must stay beside it to start the app) and the Boot
    configuration (`application*.yml` / `.properties`, `bootstrap*`, banner, logging configuration); the Boot plugin and
    packaging; it depends on the two other modules.
  - the source module (`comp_module`): every other Java source and the classpath resources code reads (mapper XML,
    message bundles, SQL …); the project's dependencies.
  - the front module (`front_module`), only when such files exist: static files (`src/main/resources/static`,
    `public`, `resources`, `META-INF/resources`, a `src/main/webapp` web root), JSPs and server-side templates
    (`src/main/resources/templates`), each at the place Boot serves or resolves it from a jar on the classpath, so a URL
    and a view name stay what they were. `front_module: null` keeps these files in the boot module, as for a non-Boot
    project.
  - Tests go where the classes they test go; a test that needs the application context goes to the boot module.
  - The application must start with the same component scan as before (the base package of the application class
    still covers the moved classes); when it would not, the restructure says so instead of guessing.
- Recorded as every restructure is (journal `restructure` with its mode, the moves in the code-change record, hand-over
  lines for what a person must check). The name of the mode and the notes are the implementer's to report.
- The user first named the modules `xxx-comp-jar` / `xxx-front-jar` and corrected it the same day: `xxx-boot` /
  `xxx-back` / `xxx-front`. That is what the layout settings give (`{name}-back` / `{name}-front`), as for every other
  project.
- **As coded.** The planner decides (`planner.boot_single_split`): for such a project the plan step is
  `restructure.apply` with `{"mode": "layout", "starter": "auto", "boot_single": True}` (not `restructure.boot_pom`),
  plan note `restructure: rule boot-single-module=split (…)`; the journal's `restructure` event says `mode: layout`, so
  the fix loop's layout facts, the KB's re-keying after moves and the Korean guide treat it as any split project. The
  restructurer reuses the single-module split of non-Boot projects (`Restructurer._single`) and adds, only for this
  case (`_Ctx.boot_split`): Boot's other static folders (`resources`, `META-INF/resources`) go to the front module;
  JSPs keep their web path inside the front jar's `META-INF/resources`, so the application's own
  `spring.mvc.view.prefix` stays valid (the boot module is then an executable WAR, `jsp_boot_packaging`); with
  `front_module: null` the web files go into the boot module at their as-is paths; a source that names the
  application class moves to the boot module with it (the source module cannot depend on the boot module); slice tests
  (`@WebMvcTest`, `@DataJpaTest` …) and tests that name a class which moved go to the boot module beside
  `@SpringBootTest` classes. Summary note: `boot split: single-module Spring Boot app split into <back>, <front>, <boot>
  (layout.boot_single_module: split) — …`. Three hand-over lines, each with a Korean rule, when they apply: classes
  moved with the application class; packages of the source module outside the application class's package without an
  explicit scan (`scanBasePackages` / `@ComponentScan`) — the scan itself is not changed by the split; sources that read
  web files by a file path (`getRealPath`, `src/main/webapp`).
- **Checked** with the real Maven: the fixture `company-boot-single` (a controller, a template, a static file, a
  `@SpringBootTest`, a unit test) builds; the boot jar holds the application class, `application.properties` and the
  two other modules' jars; the front jar holds `static/css/shop.css` and `templates/home.html`; `java -jar` starts
  and answers `/` (the template), the CSS and the API. `company-boot-wrap` pins `wrap`;
  `boot-asis-parent-unpublished` is split too.
- **Limits**: the analyzer does not count `resources` / `META-INF/resources` as web files, so the plan's note can say
  there is no front module while the restructure creates one; classpath reads from the source module's tests of files
  that moved to the front module are not looked for; scans arranged through `@Import` or `spring.factories` are not
  recognised as explicit; `front_module: null` together with a WAR was not built with the real Maven; under
  `boot_as_is: layout` with `front_module: null` a static file still stays in the source module.

## remediations (취약점 조치) — `/api/remediations`, IVAS, version-text fixes + managed overrides (2026-11-11)

A second, independent flow beside the migration: an uploaded Maven project ZIP is built as it is (baseline), scanned by
**IVAS** (the company's vulnerability scanner), the vulnerable libraries whose version is written in the project's own poms
get a new **version text** on an isolated candidate copy (transitive / parent- / BOM-managed ones get one
`<dependencyManagement>` override entry in the root pom, or in the module's own pom for a module that does not inherit from
it (v2.1z-an), instead — the single exception, below), the candidate is built and
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
- **Same build targets** (2026-11-11, real-Maven run): the node passes `build_targets=migration_build_targets(orch)` (the `target`
  of every `verify.build` step, `"."` for the root); `chain.clean_targets` stores `[]` for a root build and the directories otherwise
  (`RemediationState.build_targets`, journal `remediation_build_targets`). An MSA without a root aggregator (the migration built
  each service in its own directory) is accepted without a root pom (`accept_zip(…, build_targets=…)`: every target has a
  pom.xml) and built / resolved per service; an override entry then has no root pom to go to (residual `manual_review`).
- **Baseline failed although the migration passed**: the message starts `the migration's final build passed on this same tree, but
  the remediation's baseline build failed …`, names the two logs and what the build copy lacks (`.git/`, `.sbmig/`, `target/`
  output, ignored folders — e.g. git-commit-id-maven-plugin fails with `.git directory is not found!`); journal
  `remediation_baseline_differs`.
- **Verdict**: the remediation's outcome never changes the migration's success, exit code or report verdict; no exception escapes the
  node. `SBMIG-RESULT … remediation=<STATUS|skipped>`. Skipped (journal `remediation_skipped`, names only) when disabled or not
  ready: readiness on this path = the three IVAS values + Maven readiness; `remediation.trusted_projects` is **not** required here
  (the migration already built the project) — it gates the standalone upload only.
- **Waiting states**: a build pod cannot wait for a human. `AWAITING_APPROVAL` (only with approval switched on, v2.1z-ar) /
  `AWAITING_CONFIRMATION` (and `STOPPED`) are saved and the pod ends.
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
- **Maven**: on this path the local repository is the pod's slot repository (slot 1 `/data/.m2/repository`, slot k `repository-slot<k>`);
  the `-remediation` suffix applies to the in-process standalone path only.
- **Time**: the Job's `activeDeadlineSeconds` stays 14400. A chained remediation adds a baseline build, an IVAS upload + analysis
  (≤ `remediation.ivas_analysis_timeout_minutes`, 30) and per round a candidate build + rescan; typical 20–75 min, worst case of the
  configured caps ≈ 8 h (60-min builds, 30-min uploads and analyses, 3 rounds) — beyond any deadline, like the migration's own
  240-min cap; `remediation.chain_budget_minutes` (90) bounds the chained part per pod (above). `deploy/README.md` §6.
- **IVAS logins**: several logins / tokens at once work (the user tested three) — one login per pod is fine with three build pods.

### Hard rules (company, all bind)

- Only **Maven version text** is changed, to a version the server policy or an approved repository names. Application sources, the project structure and every non-pom file are
  never modified; the delivery ZIP has exactly the original's file set, only version text inside existing `pom.xml` files differs.
  A `replace` edit (`TextEdit.op="replace"`, the default) is one line of one pom, `old` → `new`, both plain version strings (no
  tag, no `${…}`); bytes outside the replaced span (encoding, BOM, CRLF, final newline) are preserved.
- **The one exception** (user decision 2026-11-11): a `managed_override` proposal inserts ONE `<dependency>` entry
  (groupId / artifactId / version, plus `<type>` / `<classifier>` for a variant) into the top-level
  `<dependencyManagement><dependencies>` of the **root** `pom.xml` or, for a module that does not inherit from it, of that
  module's own pom (v2.1z-an) — or a whole `<dependencyManagement>` section holding that one entry when that pom has none (placed before the top-level
  `<dependencies>`, else `<build>`, else `</project>`). It is a `TextEdit` with `op="insert"`, `old=""`, `line` (insert before),
  `anchor` (that line's current stripped text, re-checked when applied) and `override` (`{groupId, artifactId, version[, type][, classifier]}`);
  `new` must equal exactly what `apply.render_override` produces from `override` (indentation copied from sibling entries, the
  file's line ending) — no other XML can be inserted. Whether such a proposal waits for approval follows
  `require_approval` and `always_approve` like every other proposal (v2.1z-ar). `verify_text_only` refuses a candidate with any other difference: untouched files
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
| `delivery/result-r<k>-verified.zip` (+ `.json`) | only with `failed_candidate: include`: round k's built + rescanned delivery, moved aside when a later failed candidate took `result.zip` (superseded by v2.1z-bn: copied, inside the one delivery swap; `original/scan-source.zip` and `delivery/.result.next.*` are listed there) |
| `state.json` | `RemediationState`, replaced atomically at every node boundary |
| `approvals.jsonl` | append-only decision log |
| `journal.jsonl`, `remediation.log`, `report.md`, `report.json` | events, log, Korean operator report, machine report (terminal states only) |

### Statuses and reason codes (exact strings; `models.py`)

| Status | Meaning | Next |
|---|---|---|
| `QUEUED` / `RUNNING` | accepted, waiting for / held by the worker thread | stop → `STOPPED` at the next boundary |
| `AWAITING_APPROVAL` | proposals of the current round are `pending` (only with approval switched on, v2.1z-ar) | `GET …/proposals` → `POST …/approvals` → `POST …/resume` |
| `AWAITING_CONFIRMATION` | an IVAS upload was sent but its outcome is unknown (`upload_uncertain`) | operator checks IVAS → `resume` with `confirm` |
| `STOPPED` | a stop was honoured at a boundary | `resume` |
| `INTERRUPTED` | the server went down while the job was QUEUED / RUNNING | explicit `resume` only |
| `SUCCEEDED` | every success condition holds (below) | terminal |
| `INCOMPLETE` | finished, not a success (residuals, manual review, incomplete scan, rejected / pending proposals …) | terminal |
| `FAILED` | baseline / candidate build failed, verification failed, boundary refused, not ready, internal error | terminal |

`TERMINAL = SUCCEEDED, INCOMPLETE, FAILED`; `RESUMABLE = AWAITING_APPROVAL, AWAITING_CONFIRMATION, STOPPED, INTERRUPTED`.
`state.reason` is the first reason, `state.reasons` all of them (ordered: `baseline_build_failed`, `scan_incomplete`,
`zip_hash_mismatch`, `candidate_build_failed`, `pending_approval`, `rejected_proposals`, `residual_findings`, `manual_review`,
`no_candidate_version`, `inhouse_artifact`, the rest sorted, `rounds_exhausted` last). Reason codes: `baseline_build_failed`,
`candidate_build_failed`, `scan_incomplete`, `upload_uncertain`, `zip_hash_mismatch`, `text_only_violation`,
`resolution_mismatch`, `execution_boundary_refused`, `not_ready`, `pending_approval`, `rejected_proposals`,
`residual_findings`, `manual_review`, `no_candidate_version`, `rounds_exhausted`, `stop_requested`, `internal_error`,
`policy_denied`, `unknown_provenance`, `stale_location`, `inventory_incomplete`, `inhouse_artifact`, and on the chained path only `chain_budget`
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

Proposal scope: one g:a at one version may resolve in several modules through different origins. When the project holds a
version text for it, the inventory item's `modules` lists every module and `also_modules` the ones that text is not known to
govern (the same g:a:version reached as a transitive / parent- / BOM-managed dependency). The proposal carries `modules`
(governed = `modules − also_modules`) and `also_modules`; neither is part of `proposal_hash`. `verify_resolution` requires
`from_version → to_version` in the governed modules and in the artifacts those modules package (the module is the part of
`PackagedLib.artifact` before `/target/`). An also-module — or the artifact of one — that stays at `from_version` is an `info:`
line in the round's verify memo: the round commits, and the next round's inventory plans what the rescan still reports for
those modules alone (override entry or residual). A `managed_override` / existing-root-entry proposal governs all of the
item's modules; a proposal without `modules` (parent / BOM version, a state written without the field) is verified over
every module that resolved `from_version`.

In-house artifacts (`sbmig/remediations/inhouse.py`; company rule: what comes from a company artifact is fixed by that
artifact's owner, never by the project). `inhouse_keys(cfg)` maps "g:a" → group for the groups of
`remediation.inhouse_groups` (default `[mandatory, optional, parents]`; `artifacts.mandatory` / `artifacts.optional` with
their aliases, `parents` = `artifacts.parent` / `boot_parent` / `comp_parent`; `[]` = off). The inventory
(`build_inventory(…, inhouse=keys)`) fills `InventoryItem.inhouse` (the in-house g:a's a library comes from, sorted) and
`inhouse_modules`: a g:a of group mandatory / optional that is the library itself or on its dependency path
(`ResolvedDep.path`), and — for a module whose external parent is a `parents` key — that parent when the result is
`parent_managed` with an unambiguous provenance, or a transitive whose path head no project pom of the module's chain
declares (inherited from the parent). Never attributed: editable origins, `unmapped`, a BOM the project itself imports.
The planner makes an `inhouse_artifact` residual (no proposal, no override) when the library is itself an in-house
artifact or a transitive / parent- / BOM-managed item reaches every one of its modules through one; an item that is
in-house only through a parent is first offered to `_managed_change` (a `policy.pins` entry for that parent still
gives the parent-version proposal, always approved). Anything else is planned as before. `Residual` carries `modules`,
`via` (the shortest chain) and `inhouse`; `state.counts.residual_inhouse` counts those residuals; the sorted in-house keys
are part of the policy hash. A run whose remaining findings are all in-house is `INCOMPLETE` with `inhouse_artifact` among
its reasons.

Presentation (`sbmig/remediations/i18n.py`, `report.py`): stored data and API answers keep their codes and English
sentences; `report.md` is Korean throughout, written as a Korean engineer writes a result report for colleagues — not a
word-for-word rendering of the code vocabulary. The glossary (code word → the word the report uses) is the table in
`agent-doc/12-remediation-report.md`: fixed = 조치 완료, pending = 승인 대기, manual review = 직접 확인 필요, in-house = 사내
라이브러리 담당자 요청, baseline / final = 조치 전 / 조치 후, candidate = 수정본, delivery = 결과 ZIP, governed modules = 적용
범위, path = 의존 경로, version text = 버전 표기. A particle is attached to the word before it and never follows a value
filled from data unless its form is the same after any sound (`tests/remediations/test_report.py` checks the rejected words
and stand-alone particles). Label tables for origin / kind / risk / decision / reason, `ko_text` (pattern rules,
identifiers kept verbatim, an unknown sentence returned unchanged; the report prints such a sentence as `원문: …`),
`detail_group` (residuals grouped by reason and kind of detail, the explanation printed once per group).
Sections, an empty one left out: the result block (one bold verdict per status, then the numbers or the cause) → 요약 (a
`구분 | 취약점 | 라이브러리` table, the severity table 조치 전 / 조치 후, the result file) → 해야 할 일 (numbered, only the
actions this state has) → 조치 완료 → 승인 대기 → 사내 라이브러리에서 발생한 취약점 (`remediation.inhouse_message` first, one
table per in-house artifact, the module list once above a table when every row shares it, 의존 경로 as artifactIds) → 직접
확인이 필요한 취약점 (one group per reason) → 조치 대상에서 제외한 항목 → 변경하지 않은 것 → 처리 기록 (collapsed: job id,
status and reason codes, the counts, 조치 전 빌드, 조치 전 검사, each round, 결과 파일, 빌드 설정 점검) → 남은 취약점 전체
목록 (collapsed). Status and reason codes appear only in the collapsed record. `report.json` keeps every earlier key and
adds `inhouse {message, artifacts[{ga, libraries[{ga, version, modules, via, finding_ids}]}]}`. `GET …/proposals` adds
`note_ko` per proposal and `detail_ko` / `origin_ko` / `reason_ko` per residual; the status payload adds `message_ko`.
Severity marks: a finding whose IVAS score (`Finding.score`, rounded to one decimal) is ≥ `remediation.severity_red` /
`severity_orange` / `severity_yellow` carries 🔴 심각 / 🟠 높음 / 🟡 보통 in `report.md`; below yellow or without a score it
carries none (낮음·점수 없음 in the summary). Every library table opens with a 위험도 column (the row's highest score) and
sorts its rows by it, each CVE id has its mark and its score in parentheses (ordered by score, the first three shown), 요약
has the per-level counts before and after with the configured bounds. `report.json` adds
`severity {thresholds{red, orange, yellow}, baseline{red, orange, yellow, none}, final | null}` (counts only).

### The graph (`sbmig/remediations/graph.py`, a LangGraph separate from `sbmig/graph/`)

Ten nodes, state saved at every boundary; an entry router jumps to `state.next_node` (= resume). A stop request is checked
before every node and inside builds / IVAS polling.

| Node | Does | Goes to |
|---|---|---|
| `baseline_build` | Maven readiness (else `FAILED not_ready`); boundary check of `accepted/` (any `refuse` → `FAILED execution_boundary_refused`); `package` on a copy | `baseline_scan`; build failed → `FAILED baseline_build_failed` |
| `baseline_scan` | original ZIP sha256 re-checked; IVAS scan of `original/source.zip` | actionable findings → `inventory`; none (or only `ignore_actions` codes) → delivery = byte copy of the original, `evaluate`; incomplete → `INCOMPLETE scan_incomplete`; uncertain upload → `AWAITING_CONFIRMATION` |
| `inventory` | `dependency:tree` of the before-tree; findings → `Inventory` | `plan`; resolution / inventory incomplete → `INCOMPLETE manual_review` |
| `plan` | proposals + residuals, approval classes, hashes; a plan whose tree / policy hash no longer matches is kept as `plan.stale.json` and re-made | pending → `AWAITING_APPROVAL`; something to apply → `apply`; nothing → `evaluate` |
| `apply` | copy `accepted/` → candidate, apply the `auto` and `approved` edits, write `candidate.patch`, text-only check | `build`; violation → `FAILED text_only_violation`; nothing applied → `evaluate` |
| `build` | `package` the candidate, resolve again, list packaged jars, `verify_resolution`; then commit into `accepted/` | `freeze_delivery`; build failed → `FAILED candidate_build_failed` (with `include` the failed candidate is frozen as `result.zip` first, not rescanned); a governed module (or its artifact) not at the planned version → `FAILED resolution_mismatch` (never delivered) |
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
  (complete, same sha256), it is not deleted but moved to `delivery/result-r<k>-verified.zip` (+ `.json`, `scanned=true`; superseded by
  v2.1z-bn: COPIED after the new ZIP was frozen under a temporary name, `install_delivery`) and is
  downloadable as `result-verified.zip`; the report names it. Journal: `failed_candidate_delivered` (round, sha256),
  `verified_delivery_kept` (round, delivery round, file, sha256).
- **`discard`**: the failing candidate is never in `result.zip`; the failing change is kept only as `candidate.patch`
  (+ `build.log`); an earlier round's verified delivery stays `result.zip` (journal `earlier_delivery_kept`), else there is no
  delivery.
- Under **either** value a candidate that fails the text-only check (`text_only_violation`) or the resolution check
  (`resolution_mismatch`) is never delivered — it is discarded exactly as with `discard`.

`failed_candidate` is part of the policy hash (changing it invalidates open proposals).

### Approval

`remediation.require_approval` defaults to **false** and `remediation.always_approve` to **`[]`**: with the shipped
configuration every proposal is applied without approval and no run waits (v2.1z-ar). A proposal waits for approval when
`require_approval` is true, or when it carries a risk flag listed in `always_approve` — the flags are `major` (major change),
`parent` (`<parent><version>`), `bom` (an imported BOM's version) and `managed_override` (an inserted
`<dependencyManagement>` entry). There is no class that waits whatever the configuration says. Parent / BOM version
changes are proposed only when the server pins that parent / BOM itself in `policy.pins` (a pinned manager takes the
`parent_version` / `bom_version` route, never an override).

Managed-version override (user decision 2026-11-11): a `transitive`, `parent_managed` or `bom_managed` finding whose manager is
not pinned gets a target by the same rules as a direct dependency (an IVAS patched-version hint is required unless a policy pin
names the g:a, evidence from the policy or an approved repository host, same-major preferred, `policy.deny` /
`deny_versions` / `allow_major` apply) and then `managed_override` proposals: one `op="insert"` edit per target pom (the root
pom, or a non-inheriting module's own pom, v2.1z-an) and management key (v2.1z-am) (shape in "Hard rules"); findings of the
same g:a share one entry per pom and key. When the target pom already has a `<dependencyManagement>` entry
for that g:a, no second entry is ever added: a single top-level entry whose version text (literal or project property) equals the
resolved version takes the replace route (`own_management_version` / `property_version`), anything else is residual. Residual
(`manual_review` / `no_candidate_version`) instead of an override when: no hint / no candidate version; the resolved dependency
is a non-jar type or has a classifier (an override entry without type / classifier would not govern it); a module holding it does
not inherit from the root pom and `remediation.module_overrides` is false; a module declares the dependency with its own version (a declared version wins over management);
no safe insertion line (the anchor tag not on its own line, e.g. single-line XML; more than one `<dependencyManagement>`); the
root pom is unreadable. The proposal `note` states what the entry pins (the transitive chain or the manager) and always says that
later parent / BOM / direct-dependency upgrades will NOT move this version any more and that the owners must remove the override
when those catch up; `report.md` shows every proposal note under the proposals table.
Never proposed, always residual / manual review: `plugin`, `vendored`, `unmapped`, `policy.deny` matches, majors when
`policy.allow_major` is false. A `property_version` proposal notes that the property may govern other artifacts. With
`require_approval: true` every proposal waits. **A deployment that needs human approval must switch it on explicitly**
(`SBMIG_REMEDIATION__REQUIRE_APPROVAL=true` or `remediation.require_approval: true` in the company yml for every change;
`always_approve` for single classes) — the shipped default applies every proposal on its own.

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
checkbox list (grouped per library since v2.1z-an) with one apply button that reads `선택한 N건 적용` (N = checked rows, 0 allowed): checked = approved, unchecked = rejected, a confirm step ("N건 적용, M건은 적용하지
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
`always_approve` (default `[]`; the classes that wait although `require_approval` is false, v2.1z-ar),
`max_rounds` (3), `failed_candidate` (`include` default | `discard`, see "Failed candidate"), `ignore_actions` (default `[]`,
see "IVAS action codes"; part of the policy hash), `inhouse_groups` (default `[mandatory, optional, parents]`; the resolved keys are
part of the policy hash) and `inhouse_message` (the sentence the report puts over the in-house section), `severity_red` / `severity_orange` /
`severity_yellow` (9.0 / 7.0 / 4.0: lower bounds of the report's severity marks; presentation only, not in the policy hash), `chain_budget_minutes` (90; 0 = none; chained path only), `project_maven_config` /
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
| matched files | `GET …/result/filecomponents/{componentId}/matchedfiles?page=&size=` (plural: "v2.1z-bc") | `content[].file.fullPath`. The id may contain `\|` (`11560\|934982`, the company calls it ossId): it is sent as ONE encoded path segment (`11560%7C934982`) and kept unchanged in `Finding.component_id` |

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
`<pluginRepository>` on another host → `remediation.foreign_repository`, default refuse), tests never run (`-Dmaven.test.skip=true`
on every command), `clean package` as the gate, `dependency:tree` per module for resolution.
**Same command as the migration (first real-Maven run, 2026-11-11)**: the command base is `sbmig.build.mvn.base_command` (the
migration's: `build.extra_args`, `-s`, `-Dmaven.repo.local`, `-o`) and the environment `mvn_env` minus `IVAS_*` / `LLM_*`.
Insecure-TLS properties and another `-s` / `-f` in `build.extra_args` are **refused, never dropped** (`maven.unsafe_build_args`:
a readiness problem naming the arguments — `build.extra_args contains argument(s) the remediation never passes to Maven: …`; the
chained run is then `remediation_skipped not_ready`), because a silent drop made the remediation build differently and fail as
"artifact not found". A cached resolution failure (`negative_cache_hits`, incl. `Non-resolvable parent POM … Could not find artifact`)
is retried once with `-U` like `MavenRunner.run` (journal `remediation_negative_cache_retry`, log `<name>-U.log`). Every call logs
`remediation mvn <args> (cwd=<dir>)` beside the migration's `mvn … (cwd=…)` lines. **Build targets**: `MavenGateway.package /
resolve / packaged` take `targets` (the migration's final-build target dirs, `RemediationState.build_targets`; the graph passes
them only when non-empty): per target like `fixloop.build_target_args` — the root pom lists it → root with `-pl <t> -am`, else
cwd = `<t>`; one log per target (`build-<t>.log`, the failing one in `BuildOutcome.log`, summary `build target <t>: …`), first
failure stops; `resolve` runs one `dependency:tree` per target (`deptree-<t>.txt`) and records `ResolvedModel.roots`, which
`inventory.ProjectPoms(project, roots=…)` reads as its reactor roots. The standalone (in-process) path uses its own Maven local repository,
`<local repo>-remediation`, so a remediation build never shares a repository with a migration running in the same pod; the chained
path uses the build pod's slot repository (the migration in that pod has finished).
**`.mvn/` cannot be disabled on Maven 3.9** (the launcher always reads `.mvn/maven.config`, `.mvn/jvm.config` and
`.mvn/extensions.xml` from the first ancestor holding `.mvn`), so the intake boundary refuses them by default
(`remediation.project_maven_config: refuse`); with `ignore` every build logs a WARNING and journal `maven_project_config`.
Symlinks are always refused; a `settings.xml` inside the project is recorded and ignored.

### Open questions for the company

Answered by the user (2026-11-11), no longer open: the failed-candidate policy (`include`, `discard` kept as an option); which
classes need approval (none: the project teams want the result only, v2.1z-ar); the multipart file field (`file`); the analysis id
field (`lbrdrAnlId`, else `analysisId`); transitive / managed findings (override entries in the root pom, or in a module's own pom since v2.1z-an); the meaning
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
4. When approval is switched on: who may approve (`by` is free text behind the TokenGuard)?
5. Managed overrides: may two overrides share one newly created `<dependencyManagement>` section in one round (today:
   yes — both inserts target the same line and become one section holding both entries in proposal-id order)? Is the inserted
   indentation style (copied from the sibling entries, else the closing tag + one indentation step of the file) acceptable? May
   sbmig later learn which parent / BOM version fixes a finding (so the override can be dropped)?
6. Retention: how long original ZIPs, delivery ZIPs and scans stay on the volume; `DELETE` removes everything.

Not built yet: plugin / vendored fixes; a run against the real IVAS and Nexus.

## v2.1z-bg — a rendered report view, report names a person can tell apart, model names in the admin page

The user (2026-11-20): "우리 보고서 미리보기 md파일 viewer로 만들 수 있어? 추가로 report 이름이 동일한데,
migration-report와 remediation-report로 만들 수 있도록 해줄래! 아 그리고 관리자에 모델 이름이 unknown이랑 ? 2개로
나오는데 … 우리 role에 들어가는 escalation_fixer나 build_error_fixer 이름이 나왔으면 좋겠는데... 챗 응답에
usage.by_model.model에 모델 이름이 나오는데 그걸로 적는게 좋을까 그냥 role 모델 이름으로 적는게 좋을까".

**1. Report view.** The page shows a report as rendered Markdown, not as raw text: the migration report in the
result card and the remediation report wherever the page offers it. Rendering happens in the page with its own small
renderer (no library, no network: the page ships as one HTML file to an air-gapped cluster). It covers what our two
reports actually use (headings, paragraphs, emphasis, inline code, fenced code, lists incl. nested and task lists,
tables, block quotes, rules, links, `<details>` if the reports use it) and escapes everything else: report text comes
from project files and build output, so no HTML from the report may reach the DOM as markup; links open only
`http(s)` and relative run files. The raw text stays one click away, and the download gives the file as written.

**2. Names.** What a person receives is named `migration-report.md` / `remediation-report.md` (and `.json` where a
JSON report is handed out): download names, the labels on the page, entries of a ZIP or bundle that carries a report.
The files inside a run directory keep their names (`report.md`, `report.json`, `remediation/report.md`): they are
markers that the web layer, the admin page, the cleanup and runs already on the volume depend on. Addresses keep
working; a new address may be added but the old one answers as before.

**3. Model names.** The name of a usage row is the name we ASKED with (the configured model of that call: on the
company's agent gateway that is the role, e.g. `build_error_fixer` / `escalation_fixer`), never `unknown` or `?`.
What the gateway says it used (`model` of the answer when it is a real name, `usage.by_model[].model`) is recorded
beside it as the served model(s) and shown next to the name in the admin page. Reason: the role is what the company
configures, prices and recognises, and it stays the same when the gateway changes the model behind it; the served name
is the fact needed to explain a cost. Usage sums, the cost calculation (price looked up by the asked name, then by a
served name) and the admin's per-model tables follow this. Runs already on the volume that carry `unknown` / `?` / an
empty name are shown under one neutral label, not two.

**As coded (2026-11-20).** *Report view* — `sbmig/web/static/index.html`, block "3d": `MdView` (`parse(text)` → a block
tree, `render(tree, document, {resolve})` → a DocumentFragment, `safeHref`, `inline`; no other page code is used, the
test cuts the block out and runs it in node) builds elements and text nodes only, never `innerHTML`. Covered: ATX
headings, paragraphs, emphasis, inline and fenced code, nested and task lists, tables (a header row of empty cells, the
two-column head table of our reports, is not drawn), block quotes, rules, links, `<details>` with one `<summary>` line
(`<code>` accepted inside the summary only); any other HTML is shown as text. A link becomes an anchor only for
`http(s)` (checked again through `URL`) or a relative path that the `resolve` function maps to a file of this run
(`runFileLink`: in the run's served list; the remediation report: in its file list → `…/files/<name>`); everything
else stays text. An image (`![…](…)`) is never loaded: its alt text is shown. The result card keeps the preview
(`#preview`, hidden until a report arrived) with the 「보기」 / 「원문」 toggle (`.rvseg`; both toggles share one mode)
and 「크게 보기」 (`#report-big`); the dialog `#rv-dialog` (title 「마이그레이션 보고서」, 「실행 보고서」 for a
remediation-only run, 「취약점 조치 보고서」) is shared by both reports, has its own download link with the received
name, closes by button, Esc or the backdrop and returns focus. The remediation block gains 「보고서 보기」
(`#remed-view`), which fetches `…/remediations/{id}/files/report.md` when it is opened (a failure is a toast).
*Names* — `runs.DOWNLOAD_NAMES` (`report.md` → `migration-report.md`, `report.json` → `migration-report.json`) is used by
`GET /api/runs/{id}/files/{name}` when it attaches (`?download=1` or an attach-type file); the remediation router has
its own `DOWNLOAD_NAMES` (`remediation-report.md` / `.json`) and its files route gains `?download=1`, which attaches any
file (inline stays the default, so the page can fetch the text). The page sets the same names on its `download`
attributes (`MIG_DL`, `REM_DL`) and labels 「보고서 받기」 with `migration-report.md`. Run-directory files, addresses
and every ZIP stay as they were (no ZIP carries a report). In the Korean report the sentences that name a report for
the reader use the received names (`markdown.received_name`, `RECEIVED_NAMES`: the chained remediation's 자세한 결과 /
다른 파일 lines, the remediation-only run's pointer; "… 외 n건 (migration-report.json 참고)", the structure-review
pointers; `_TOLERATED_KO` in `sbmig/remediations/report.py` names `migration-report.md`); the English layout keeps the
run-directory paths (`remediation/report.md`). `deploy/docker/run-job.sh` (git mode, the result branch) keeps
`report.md`. *Model names* — `LLMResponse.model` is the asked name (built-in clients, `AdapterClient`,
`KwargFilterClient` all pass through `client.named_response`); `LLMResponse.served_models` = the real names the answer
reported (`client.served_models_of`: a top-level `model` that is not a placeholder, every `usage.by_model[].model`),
the asked name left out. On the agent gateway the asked name is `params.role` (`CompanyClient.asked_name`; the
top-level `model` is the asset id). `core.models`: `PLACEHOLDER_MODEL_NAMES` (`""`, `?`, `-`, `unknown`, `none`,
`null`, `n/a`, `default`), `real_model_name`, `merge_model_names`, `asked_model_name` (first real candidate, else
`DEFAULT_MODEL_LABEL` = `gateway-default`), `served_beside`. `LLMUsage.served` / `ModelCost.served` (merged in
first-seen order by `add`, left out of the JSON when empty, so a report without served names keeps its JSON shape);
`RunReport.llm_usage` carries an asked name too (its own, else the first row's) and the union of the rows' `served`. Every place that named a row (`_BudgetedLLM`, the
structure reviewer, the plan advisor, `LLMFixer`, `Orchestrator._usage_by_model`) uses `asked_model_name` with the
configured model as fallback, never `?`. Journal `llm_script` and `llm_verify_guide` gain `served`. The log line is
`llm call provider=<p> model=<asked> in=<n> out=<n> latency=<s>s finish=<r>[ served=<a,b>]` (`client.served_log_suffix`,
after `finish=` so the admin page's older expression still reads it); `sbmig llm-probe`'s `LLM-PROBE ok …` line ends
with ` served=…` when known (`ProbeResult.served`). The report's model cell is `` `<asked>` (<served>) ``
(`markdown.model_cell`, both renderers). *Price* — `cost.price_for_usage`: the asked name first, then each served name
in order; the first key `price_for` matches wins, so a role key in `llm.pricing` wins over a served-model key, and a
table keyed by real model names keeps working while the gateway reports them. *Admin* — `admin.model_row`: `model`,
counts, `served` (when known), `unnamed: true` for the row of old runs whose name was `unknown` / `?` / empty (all such
rows merged into one, costs summed or null); the page shows the asked name with the served names beside it in grey,
the unnamed row as 「이름 없음」 (hint 「이전 실행」), and the per-project run table gains a 모델 column (one line per
model). Names of old runs are recovered (`_names_from_old_log`, `usage_from_log`) only when every unnamed `llm call …
in=… out=…` line of `sbmig.log` follows a request line with `params.role=` and the recovered calls and tokens equal
the report's unnamed row exactly; otherwise the row stays 「이름 없음」. *Project field* — placeholder 「레포지토리 이름」,
hint 「6자리 체인코드가 포함된 레포지토리 이름으로 작성해 주세요.」 above the character rule; the rule itself is
unchanged. Tests: `tests/web/test_page_report_view.py`, `tests/web/test_admin_model_names.py`,
`tests/fix/test_model_names.py`, `tests/loop/test_model_names_report.py`, additions in `tests/web/test_project_name.py`,
`tests/web/test_error_list_files.py`, `tests/remediations/test_api.py`, `tests/report/test_guide_ko.py`. *Known limits*:
the tokens of one row are not split per served model (a row with two served names carries one sum; `by_model[].model`
of the answer is kept as a name only); the page has no dark token set, so the rendered report is light only; Edge is
not checked; that the agent gateway answers `model: unknown` is inferred from the admin page's rows, no recorded real
answer is in the repository.

## v2.1z-bh — build output in an uploaded ZIP is left out of the remediation's file list

**Why.** A remediation-only upload that carried `<module>/target/` (classes, a packaged jar) put those files into
`original/manifest.json`. Every build of the remediation is `mvn clean package`, so a build that fails before a module
compiles leaves manifest files missing; freezing the failed candidate then ended the run as `internal_error`
(`file listed in the manifest is missing: …/target/classes/….class`) instead of 「수정본 빌드 실패」. Stale build output
also reached IVAS and the result ZIP although it no longer matches the changed poms.

**Contract.**
- `intake.build_output_paths(files: list[str]) -> list[str]` (pure, sorted): the file paths that lie under a Maven
  build-output folder. A path is build output when one of its segments is exactly `target` and the folder holding
  that segment has a `pom.xml` among `files` (`a/b/target/x` with `a/b/pom.xml`; `target/x` with `pom.xml`). The
  outermost such segment decides. A `target` folder without a sibling `pom.xml` is ordinary content; a `pom.xml`
  inside a build-output folder never makes another folder a module.
- `accept_zip` validates the whole ZIP as before (limits, names, collisions count every entry), decides
  `project_root` on all entries, then leaves the build-output files out of BOTH `accepted/` and
  `original/manifest.json`. `original/source.zip` keeps them. A ZIP whose files are all build output is rejected as
  `empty`.
- `IntakeResult.files` / `unpacked_bytes` / `tree_hash` count the kept files only. `IntakeResult.build_output_files:
  int = 0` (left out of the JSON when 0, read with default 0) and one entry in `notes`:
  `build output left out: <n> file(s) under <folder>[, <folder> …]` (at most 5 folders, then `+<k> more`; folders
  are the `…/target` paths, sorted). Log line at INFO with the same text. Journal: no new event.
- No switch. Applies to every caller of `accept_zip` (remediation-only upload, chained remediation, restart).
- Report (`remediations/report.py`, Korean) and the page keep working unchanged; the Korean report shows the note
  once, where the intake facts are listed, as 「업로드한 ZIP의 빌드 산출물 <n>개(`<folder>` …)는 검사와 결과 ZIP에서
  제외했습니다. 원본 ZIP에는 그대로 있습니다.」 through the existing i18n pattern table. (Superseded by v2.1z-bn: until then
  the baseline scan still uploaded `original/source.zip`; now it uploads `original/scan-source.zip`, and the note is left
  out of a run whose baseline scan was of the original.)
- The failed-candidate path itself is not changed; with build output out of the list, `mvn clean` cannot remove a
  listed file.

**Tests.** `tests/remediations/test_intake_build_output.py`: the pure function (nested modules, root pom, `target`
without a pom, a pom inside `target`), `accept_zip` (manifest and `accepted/` without the files, counts, note, all
build output → `empty`, single-top-level-folder ZIP), and one graph-level test: a ZIP with `target/classes/A.class`
whose candidate build fails ends `FAILED candidate_build_failed` with a frozen delivery, not `internal_error`.

## v2.1z-bi — build errors are fixed by the loop in two more places: framework projects, and a remediation's candidate

**Why.** The company asked for both: a framework project gets only JDK 21 + jakarta, "so at least fix the build";
and a remediation that raises library versions can break the build, so the fix loop should try.

### A. Framework projects: `framework.fix_loop` defaults to `true`

> **Turned back the same day:** `framework.fix_loop` is `false` by default again (the user keeps framework projects
> conversion-only for now; code and `config/default.yml`). The switch stays; with it on, the loop below runs. The chain
> rule (a failed final build chains whether or not the loop ran) and the report's two variants stay as coded.

- Default `true` (code and `config/default.yml`; done in the contract commit). Everything the `true` branch already
  does stays: full loop (pre-tries without the company parent, stored scripts, LLM), `_framework_guard` (no
  `<parent>` change, no change of the framework's declarations / version), the reduced-scope prompt.
- **A failed final build no longer stops the chain for a framework run with the loop on.**
  `Orchestrator.remediation_after_failed_build`: with `remediation.after_failed_build: framework`, a reduced run whose
  final build was attempted and failed chains whether or not the loop ran. `web/runs.py` `remediation_always` no
  longer depends on `fix_loop`. The report's "chained after a failed build" sentence must not say the loop was off
  when it ran (two variants).
- The notice 「<이름> 프레임워크는 JDK 21, Jakarta만 적용합니다.」 stays as is (it states the scope). Texts that say
  build errors are not fixed appear only when `fix_loop` is false, as today.
- Tests that assumed the old default set `framework.fix_loop: false` explicitly; the default itself gets one test.

### B. Remediation: the fix loop on a failed candidate build

> **Superseded in part by "v2.1z-bj" and "v2.1z-bk".** The loop no longer runs on the candidate: it runs on a work copy,
> the candidate is changed only by the graph's overlay of the fixed bytes, and after every candidate build the tree is
> synchronised with accepted + applied + reported fix. Steps 1 and 6 below (`.gitignore` saved and restored, `tree/.git`
> removed, the tree reset to `base`) describe the first cut and no longer hold; where this part and those two sections
> disagree, they count.

**Switches.** `remediation.fix_loop: bool = True`, `remediation.fix_loop_max_iterations: int = 10` (1–50).

**When.** In `graph._build`, after the candidate build of round `r` failed and `_baseline_errors_only` is false, and
ALL of: the switch is on; the baseline build passed (`not h.baseline_failed_tolerated` and the baseline outcome is
ok); no stop is pending; the round applied at least one proposal. Otherwise today's path, unchanged.

**Generic hook in the loop (`sbmig/loop/fixloop.py`).** `BuildFixLoop(..., edit_guard: Callable[[Workspace, str,
list[str]], str | None] | None = None)`. Called with `(ws, pre_head, touched_paths)` after ANY change was applied to
the working tree and before it is committed: at the two sites where `_framework_guard` is called, and for sbmig's own
pre-tries too (`_framework_guard` skips those; this hook does not). `touched_paths` = repo-relative paths added,
modified or deleted since `pre_head`. A non-empty string rejects the attempt exactly like a `_framework_guard`
rejection (change discarded, the reason recorded on the attempt and given to the next LLM try as feedback). `None`
default = today's behaviour, byte for byte.

**`sbmig/remediations/fixer.py` (new).** `run_fix_loop(cfg, tree, work_dir, *, run_id, targets, applied, manifest_paths,
first_outcome, stop, deadline, journal) -> FixResult`; reachable from the graph through the components object
(`h.c.fixer.run(...)`, protocol `Fixer` in `interfaces.py`) so graph tests use a fake.
1. `Workspace(tree, work_dir, run_id)` with `work_dir = rounds/<r>/fixloop/` (its own `journal.jsonl` and `logs/`,
   never the run's). The root `.gitignore` is saved (bytes, or "absent") before `git_init()` and restored after the
   loop; `tree/.git` is removed at the end, success or not. `ws.stop_requested` observes `stop` (superseded by v2.1z-bo:
   the loop checks it, and the time budget, between builds and before each LLM request).
2. `analyze_project(ws, cfg)` for the profile. Parts built like the orchestrator's: `MavenRunner`, `Triager`,
   `LazyFixer(LLMFixer(cfg, make_llm_client(cfg)))`, `LazyScriptRunner`, `NullKnowledgeBase` (no stored scripts used,
   nothing learned), `LoopBudget` limited to `deadline`.
3. `plan_notes`: one English paragraph: the build broke after a vulnerability remediation raised these library
   versions (`g:a old -> new` per applied proposal); fix the project's source so it compiles against the new
   versions; `pom.xml` must not be edited; no file may be created or deleted.
4. `edit_guard` rejects, with a reason naming the path: any touched path whose file name is `pom.xml`; any touched
   path not in `manifest_paths`; any deleted path.
5. `loop.run(Phase.VERIFY, "clean package", cfg.remediation.fix_loop_max_iterations, target=t)` per build target that
   failed, in order; stop at the first target the loop cannot fix.
6. After the loop: fixed = every target's result is ok. Changed files = `ws.changed_files_since(base)` minus the
   restored `.gitignore`. A final check repeats rule 4 over the whole diff; a violation counts as not fixed.
   Fixed → `rounds/<r>/fix.patch` (unified diff base..head) is written. Not fixed (or stop, deadline, LLM
   unavailable, any exception inside the fixer) → the tree is reset to `base` byte for byte, no patch.
7. `FixResult` (Record in `models.py`): `attempted: bool`, `fixed: bool`, `iterations: int`, `attempts: int`,
   `llm_calls: int`, `input_tokens: int`, `output_tokens: int`, `models: list[str]` (asked names),
   `changed: list[str]` (sorted repo-relative paths, empty unless fixed), `patch: str` (run-relative path or ""),
   `reason: str` (English, empty when fixed: `not fixed: <n> error(s) left`, `llm unavailable: …`,
   `stop requested`, `time budget used up`, `rejected: <guard reason>`, `error: <Type>: <msg>`), `remaining: int`.
   `RoundState.fix: FixResult | None = None` (left out of the JSON when None).

**Graph.** Journal `remediation_fix_loop` (`round`, `phase: start|end`, at end `fixed`, `iterations`, `llm_calls`,
`changed` count, `reason`). Fixed → one more `c.maven.package` of the candidate into `rounds/<r>/build-fixed.log`
through the remediation's own runner (the outcome that counts; `rs.build` becomes it, the first failed outcome is
kept as `rs.fix_first_build`), then `_verify` and commit as today. That build failing → not fixed (tree reset,
`reason: error: confirming build failed`). Not fixed → `_candidate_failed` exactly as today, with
` | automatic build fix: <reason>` appended to the state message. The delivery of a fixed round has
`build_ok = True` and carries the source changes (they are manifest paths). The time left for the loop is the
run's remaining budget; a loop that cannot start for lack of time is `attempted: false`.

**Report and page (`remediations/report.py`, `i18n.py`, `web/static/index.html`).**
- Fixed round: a section 「빌드 오류 수정」 after the applied changes: 「버전을 올린 뒤 빌드가 실패해 소스를 고쳤습니다.」,
  the changed files (table: 파일), iterations / LLM calls (superseded by v2.1z-bo: the round's total when other loops
  ran, 「…(버린 시도까지 모두 T회).」), the patch file name; leads the review list:
  「자동으로 고친 소스 <n>개를 확인해 주세요.」.
- 「변경하지 않은 것」: when any round has `fix.fixed`, the sentence 「애플리케이션 소스와 프로젝트 구조(파일·폴더)는
  변경하지 않았습니다.」 becomes 「프로젝트 구조(파일·폴더)는 변경하지 않았습니다. 소스는 빌드 오류를 고치느라
  <n>개 파일을 바꿨습니다.」 and "달라진 것은 … 버전 표기뿐입니다" gains 「와 고친 소스 <n>개」.
- Not fixed: one line under the failed-build warning: 「빌드 오류 자동 수정을 시도했지만 고치지 못했습니다(<사유>). 소스
  수정은 되돌렸습니다.」 with the reason through the i18n table (superseded by "v2.1z-bm": the second sentence is
  「시도한 소스 수정은 결과에 넣지 않았습니다.」).
- `report.json` carries `rounds[].fix`. Files route: `fix.patch` of the last fixed round is servable as
  `fix.patch` (allow-list addition in `remediations/api.py`).
- Page: the remediation card shows 「소스 <n>개 수정」 chip when fixed; the run header's LLM call count includes the
  remediation's calls (`SBMIG-RESULT … llm=` and wherever the page reads it for a remediation-only run).

**Not in this round.** Pom edits by the loop (an upgrade that needs an exclusion or a new artifact stays a failed
candidate); a loop when the baseline build already failed; learning into the KB; prompts written for this case
beyond `plan_notes`.

**As coded (2026-11-20).** Deviations from the text above, all in the code as committed:
- `first_outcome` is only logged: `run_fix_loop` writes its log path into the loop's own journal
  (`remediation_fix_loop_start`, beside `targets` and `max_iterations`); the loop builds once itself before it fixes.
- The `fix.patch` route is an entry of `_FIXED_FILES` in `sbmig/remediations/service.py`, not in
  `remediations/api.py`. It serves the combined `remediation/fix.patch` that `report.write_fix_patch` writes, and only
  while the saved state has a committed round with a fixed, non-empty fix ("v2.1z-bk").
- The loop does not start with less than `fixer.MIN_LOOP_SECONDS` (60) left (`attempted: false`, reason
  `time budget used up`; superseded by v2.1z-bp: only the graph checks the 60 s, the fixer refuses only a deadline
  already passed), nor when a stop is already pending (`attempted: false`, `stop requested`). The work copy
  holds no `.git` path of the upload ("v2.1z-bj"), so a root or nested `.git` no longer stops the loop. The work copy's
  commit of the uploaded files names the loop paths explicitly, so a file the project's `.gitignore` hides is still
  tracked.
- `Components.fixer` defaults to `None`; `None` means the loop is never called (`_fix_wanted` is false), which is what
  every graph test that does not set a fixer gets. `default_components` sets `fixer.RemediationFixer()`.
- The candidate's completeness marker is not touched by the loop (it only reads the candidate); a fixer that raises
  ends the round as not fixed and the failed candidate is delivered ("v2.1z-bk"). A failing confirming build brings
  the candidate back to the expected files (`intake.sync_tree`: applied edits, else `accepted/`), saves the state and
  only then deletes the patch and `fixed/`.
- Time: the loop gets `min(remediation.fix_loop_max_minutes × 60, time_left − reserve)` (`graph.fix_loop_seconds`,
  "v2.1z-bj"); `time_left` exists only in a chained run (`chain._Budget.time_left`, `remediation.chain_budget_minutes`),
  elsewhere the cap alone counts, together with `loop.max_total_minutes` and `remediation.fix_loop_max_iterations`.
- `report.fix_summary(state)` is the digest the service's status carries as `fix` (None when no round ran the loop):
  `fixed`, `changed` (files of committed fixed rounds), `llm_calls` (every round's loop, discarded fixes included),
  `round` (last fixed round). The page draws the chip from it. The remediation's calls are counted in
  `SBMIG-RESULT … llm=`, `GET /api/runs/{id}` and the admin page ("v2.1z-bl"); the migration report's own
  `llm_usage` never holds them.
- "Fixed" in the report, the chip and `fix_summary.changed` counts only committed rounds (`_fixed_rounds`); a fixed
  round that did not commit (for example a later verify problem) contributes nothing to the result.
- A loop that never started (`attempted: false`) is 「빌드 오류 자동 수정은 시작하지 않았습니다(<사유>).」 under the
  failed-build warning and 「빌드 오류 자동 수정: 시작하지 않음(<사유>)」 in the processing record, 사유 = 중지 요청 /
  남은 시간 부족 (`i18n.fix_not_started_ko`). The status message's ` | automatic build fix: <reason>` and
  ` | automatic build fix not started: <reason>` are translated by the `auto_fix_*` rules of `i18n.py`.
- The processing record of a fixed round shows the first failed build (「처음 수정본 빌드」) above the confirming build.
- Not covered: the fix loop's calls are not part of the migration report's `llm_usage` (they are added where the
  result line, the run status and the admin page count; "v2.1z-bl").

**Tests.** `tests/loop/test_fixloop_edit_guard.py` (the hook: LLM edits, stored script, pre-tries, a raising guard,
`None` unchanged), `tests/remediations/test_fixer.py` (guard rules, fixed / not fixed / exception / time / stop,
`.gitignore` and `.git` restored, patch), `tests/remediations/test_graph_fix_loop.py` (when the loop runs, confirming
build, `candidate_build_failed` with the suffix, state JSON without `fix`), `tests/remediations/test_report_fix_loop.py`
(section, 「변경하지 않은 것」, the not-fixed and not-started lines, `report.json`, `fix_summary`),
`tests/remediations/test_i18n.py` (the reasons), `tests/web/test_page_remediation_fix.py` (chip, LLM count,
`fix.patch` link); part A: `tests/loop/test_framework_no_loop.py`, `tests/web/test_framework_scope.py`,
`tests/analyze/test_framework.py` (`test_framework_fix_loop_defaults_to_true`), `tests/fixtures/test_fixture_projects.py`.

## v2.1z-bj — the remediation's fix loop works on a copy; findings of the review of v2.1z-bh / v2.1z-bi

**Why.** A review of the two rounds reproduced these: (1) a failed git reset / cleanup left the loop's partial edits
(or sbmig's `.gitignore` lines, or `.git`) in the candidate, which was then delivered as the failed candidate; (2) a
git checkout nested in the upload hid edits from the guard and the reset; (3) a fixed round that then dropped an
ineffective override lost the fix while the report claimed it; (4) the loop used up the run's whole time budget, so
"not fixed" became STOPPED without a delivery; (5) a pod killed after the fix and before the commit delivered
LLM-changed sources with no word of it; (6) `build_output_paths` dropped real files under `src/**/target/`;
(7) `fix.patch` was served for rounds whose fix is not in the result. This section replaces what "v2.1z-bi" part B
says where they differ. `framework.fix_loop` is `false` by default again (the user, same day); the switch stays.

**Invariant.** The candidate tree is changed by the fix loop only through `overlay` below. Whatever the loop does
(git, scripts, a crash, a kill) happens in a work copy that is never delivered.

### Fixer (`sbmig/remediations/fixer.py`)

`run_fix_loop(cfg, tree, work_dir, *, …)` no longer touches `tree`:
1. **Work copy.** `work_dir/tree/` is made fresh (an older one is removed first) from `tree`, with exactly the
   *loop paths*: the manifest paths (project-relative) that have no `.git` path segment. The loop (Workspace,
   `git_init`, analysis, builds) runs there. Nothing is restored afterwards; the work copy is removed at the end
   (a failed removal is logged at WARNING and is not an error).
2. **Editable paths** = loop paths that are none of: a file named `pom.xml`; a pom Maven reads for this build under
   another name (the inventory's pom list — reuse the existing reader); a path with a segment starting with `.`;
   `mvnw`, `mvnw.cmd`, `*.sh`, `*.bat`, `*.cmd`, `*.ps1`; a jar / war / ear / class / zip file. The edit guard rejects
   a touched path that is not editable (reasons: `pom.xml must not be edited: <p>`, `<p> must not be edited`,
   `<p> must not be deleted`, `<p> is not a file of the uploaded project`).
3. **Changed files** are found by comparing bytes of every editable path between `tree` and the work copy, not by
   git. A missing editable path in the work copy, or a difference in a non-editable loop path other than the root
   `.gitignore`, is a violation → not fixed, `reason: rejected: …`.
4. **Fixed** → the changed files' new bytes are stored under `work_dir/fixed/<path>` (durable; the only thing the
   graph reads), `fix.patch` is written (unified diff, `a/<path>` → `b/<path>`, project-relative, made from the two
   byte versions with `difflib`; a file that is not UTF-8 is listed as `Binary files differ`), `FixResult.changed`
   lists them, `FixResult.fixed_dir` is the run-relative path of `fixed/`, `FixResult.tree_hash` =
   `intake.tree_hash` of the candidate as it will be after the overlay (computed by the graph, see below).
   **Fixed with no changed file** (the loop's own build passed without a change): `fixed: true`, `changed: []`,
   `retry_passed: true`, no patch, no `fixed/`.
5. **Time.** `deadline` is honoured before the stop check: the reason of a loop that ran out of its own time is
   `time budget used up`, never `stop requested`. Each build inside the loop gets a timeout no longer than the time
   left to the deadline (and no longer than `remediation.build_timeout_minutes`). `stop requested` only for a real
   stop (`stop()` true while the deadline has not passed).
6. `reason` for an unavailable LLM is exactly `llm unavailable` (the detail stays in the loop's own log under
   `work_dir`); every other reason passes `scrub_message`.
7. The fixer may raise only for a programming error; every operational failure is a `FixResult`. Because it never
   touches `tree`, any `FixResult` with `fixed: false` leaves the candidate exactly as it was.

### Graph (`sbmig/remediations/graph.py`)

- **Time for the loop** = `min(fix_loop_max_minutes × 60, time_left − reserve)` where `reserve = max(300, 2 ×
  seconds of the round's first candidate build, 2 × seconds of the baseline build)`; `time_left` None → only the
  cap. Less than `MIN_LOOP_SECONDS` → not started: `attempted: false`, `reason: time budget used up`.
  A pending stop → not started, `reason: stop requested`. (v2.1z-bo: only these two not-started results are forgotten on
  re-entry; a failed folder setup stays the verdict.)
- **State is saved right after the loop returns** (`rs.fix`, then `h.save()`), before anything touches the candidate.
- **Overlay** (`_overlay_fix(h, r)`): copies every file of `fixed/` over the candidate's file of the same path
  (only paths in `rs.fix.changed`; each must be an existing manifest file), idempotent. `_remove_overlay(h, r)`
  copies the same paths back from `accepted/`.
- **`_build` re-entry** (resume, or after `apply` remade the candidate): `rs.fix` is NOT cleared.
  `rs.fix.fixed` with changed files → overlay first, then the build is the confirming build (log
  `build-fixed.log`); it passes → verify, commit. `rs.fix.attempted` and not fixed → the build runs, and when it
  fails the loop is not run again (`_candidate_failed` with the recorded reason; superseded by "v2.1z-bm" for the
  reason `stop requested`, which is moved to `fix_discarded` so the loop may run again). `rs.fix` None → as specified in
  "v2.1z-bi". The loop therefore runs at most once per round and candidate, except after a kill during the loop
  itself (nothing was saved) and in the next bullet.
- **Confirming build fails** → `_remove_overlay`, `rs.fix.fixed = False`, `reason: error: confirming build failed`,
  `changed` moved to `rs.fix.discarded_changed`, `fix.patch` and `fixed/` removed; then `_candidate_failed`.
- **Dropping an ineffective override in a fixed round**: the fix of this candidate is discarded before the candidate
  is remade: `rs.fix` is appended to `rs.fix_discarded: list[FixResult]` (left out of the JSON when empty; its LLM
  usage still counts in `fixer.fix_usage`) with `fixed = False`, `reason: discarded: the candidate was remade`, its
  patch and `fixed/` removed, and `rs.fix = None`. The remade candidate's build that fails may run the loop once
  more (same conditions).
- **Missing manifest files after any build of the candidate** (first build, confirming build, the build after a
  dropped override): `intake.restore_missing(cand, accepted, paths) -> list[str]` copies each manifest path that
  no longer exists in the candidate back from `accepted/` (bytes and exec bit; never a path that exists; refuses
  symlinks like `copy_tree`). Journal `manifest_files_restored` (`round`, `count`, first 5 paths) when any. A build
  can therefore never make freezing or committing fail with `missing_file`.
- **Not fixed** → `mark`/deliver as before (the candidate was never touched). The message suffix is
  ` | automatic build fix: <reason>` when the loop ran and ` | automatic build fix not started: <reason>` when
  `attempted` is false (v2.1z-bn adds ` | automatic build fix made no attempt: <reason>` for a loop with no attempt). Because of the reserve, a loop that used its time does not make `h.stop()` true by itself;
  the outcome is `FAILED candidate_build_failed` with the failed candidate delivered, as before v2.1z-bi.
- `fix_first_build`, the journal events and `Components.fixer None` stay as coded.

### Intake (`sbmig/remediations/intake.py`)

- `build_output_paths`: a `target` segment counts only when its holding folder has a `pom.xml` AND no segment
  before it is `src` (the rule of `core.workspace.is_build_output_path`). `src/test/resources/proj/target/…` and
  `src/it/demo/target/…` stay ordinary content. The note's folder list follows the same rule.
- `restore_missing` (above), listed in the intake protocol.

### Files route, report, page

- `fix.patch` is served only for the last round that is `committed` with `fix.fixed` and a non-empty `changed`
  (decided from the state, not from the files on disk); otherwise 404 and not in the status' file list.
- Report / `fix_summary` / chip: a fix is shown only for such rounds. `retry_passed` rounds get one line in the
  record: 「처음 빌드는 실패했지만 다시 빌드하니 통과해 소스는 고치지 않았습니다.」 and no fix section.
  Not started: 「빌드 오류 자동 수정은 시작하지 않았습니다(<사유>).」 with 사유 = 중지 요청 / 남은 시간 부족 by `reason`;
  it never says the sources were reverted. Discarded fixes (`fix_discarded`, a failed confirming build) get a record
  line each, and their LLM calls are in the counts.
- i18n: rules for ` | automatic build fix not started: <reason>`, `llm unavailable` (bare and the old form),
  `discarded: the candidate was remade`, the new guard reason `<p> must not be edited`.

**Tests.** The review's reproductions (`out/scratch/review-bi/`, not tracked) are turned into regression tests that
now assert the right outcome: tree integrity with a failing reset / cleanup, a nested `.git`, a root `.git`, a
fixed round with a dropped override, the budget case (FAILED with a delivery, not STOPPED), the kill-and-resume
case (fix kept and reported), `src/**/target`, the `fix.patch` route per state, the not-started wording per reason.

**As coded (2026-11-20).** Deviations from the text above and names it did not give (later amended by "v2.1z-bk"
where noted):
- Fixer: the per-round patch is `rounds/<r>/fix.patch` (`run_fix_loop(…, patch_file=None)` → `work_dir.parent /
  "fix.patch"`), the fixed bytes `rounds/<r>/fixloop/fixed/<path>`; the work copy is `fixloop/tree/` and is removed at
  the end even when the loop fixed (`_rmtree_quiet`, a failure is a WARNING). `loop_paths`, `is_editable`,
  `maven_pom_paths` (the inventory's `ProjectPoms`: a `<relativePath>` or a `<module>` of the root or a profile naming an
  `.xml` file other than `pom.xml`, and an `.xml` build target), `check_paths`, `make_edit_guard`, `compare_trees` and
  `unified_patch` are module functions. A symlink in the work copy is a violation like a non-editable path. A missing
  manifest file in the candidate makes the copy fail: `error: …`, not fixed.
- Journal of the loop (`fixloop/journal.jsonl`): `remediation_fix_loop_start` carries `loop_paths` and `editable`
  counts besides `targets`, `first_build_log`, `max_iterations`; `remediation_fix_llm_unavailable` (`detail`, 300
  characters) holds what the bare `llm unavailable` reason leaves out. Remediation journal: `remediation_fix_rejected`
  (`reason`) for an end-check violation; `remediation_fix_loop` also has `phase: not_started` (`reason`);
  `remediation_fix_discarded` (`round`, `reason`, `changed` count) whenever a fix is dropped.
- Graph: `graph.fix_loop_seconds(h, first)`, `FIX_RESERVE_MIN_SECONDS = 300`. A recorded loop that never started
  (`attempted: false`) is forgotten when `_build` runs again, so a resume after a stop may start the loop (the text
  only covered a loop that ran). `_overlay_fix` writes each file through a temporary file and `os.replace`, keeps the
  candidate file's permissions, and changes nothing (returns False) when any path is not an existing candidate file;
  the round then treats the fix as unusable ("v2.1z-bk"). Before a fixed fix is discarded for a remade candidate the
  candidate's completeness marker is taken off (`unmark_candidate`), so a kill in between never builds a candidate
  that still holds the dropped fix.
- `restore_missing(tree, source, paths)` is in `intake.py` and the intake protocol, journal `manifest_files_restored`
  (`round`, `count`, `paths` = the first 5); "v2.1z-bk" replaced its call by `sync_tree`.
- Models: `FixResult.fixed_dir`, `tree_hash`, `retry_passed`, `discarded_changed` and `RoundState.fix_discarded` are
  left out of the JSON when empty / False; older JSON reads back. `remediation.fix_loop_max_minutes` is
  `Field(default=30, ge=1, le=240)` (`config/default.yml` the same).
- i18n: `fix_not_started_ko` / `fix_not_started_reason_ko` (중지 요청, 남은 시간 부족) and the `auto_fix_*` /
  `auto_fix_not_started*` rules; the reasons of a loop that ran and did not fix still end in 「소스 수정은
  되돌렸습니다.」 (`fix_failed_ko`), meaning the loop's edits were dropped (superseded by "v2.1z-bm": 「시도한 소스 수정은
  결과에 넣지 않았습니다.」, since the candidate was never touched).
- Tests: `tests/remediations/test_fix_loop_review.py` (the review's reproductions: failing cleanup / reset, nested and
  root `.git`, dropped override, the time reserve, kill-and-resume, retry passed, a pending stop, files a build
  removed), `test_fixer.py` (work copy, editable paths, byte comparison, deadline per build, `llm unavailable`),
  `test_graph_fix_loop.py` (reserve and cap, a raising fixer), `test_fix_patch_route.py`, `test_intake_build_output.py`
  (`src/**/target`, `restore_missing`), `test_report_fix_loop.py`, `test_i18n.py`.

## v2.1z-bk — second review of the remediation's fix loop: stop, kill windows, build-side changes, patch

**Why.** A second review reproduced: (1) a stop during the confirming build threw the fix away as "confirming
build failed" and the resumed run ended FAILED without trying again; (2) in the failed-confirm path the fixed files
were deleted before the state was saved, so a kill in between ran the loop a second time under a false reason;
(3) a fixer that raises still took the candidate's completeness marker off, so the run delivered nothing although
the candidate was untouched; (4) older than the loop: files a build writes into manifest paths (a formatter bound to
the lifecycle, sortpom) were committed and delivered unreported, and inside the loop such edits were credited to the
LLM or made every attempt fail the guard; (5) the LLM calls of a loop killed mid-way were never counted and its
folder was deleted; (6) a source that is not UTF-8 showed as `Binary files differ` in `fix.patch`; (7) with two
fixed rounds the report named each round's patch while the route served only the last.

### Graph and fixer

- **Stop and the confirming build.** After the confirming build, `h.stop()` true → nothing is discarded: the overlay
  and `rs.fix` stay, the node halts STOPPED with `next_node = build`; a resume overlays again (idempotent) and
  confirms again. Only a confirming build that failed while no stop is pending discards the fix.
- **Save, then delete.** Wherever a fix is dropped (failed confirm, discarded, unusable on re-entry) the state is
  saved before `fixed/` and the patch are removed.
- **A raising fixer** leaves the candidate's completeness marker alone; the round ends as "not fixed" with
  `reason: error: <Type>: <msg>` and the failed candidate is delivered as for any other reason.
- **Build-side changes never reach a delivery.** At the end of `apply` the poms the proposals edited are stored as
  `rounds/<r>/applied/<path>` (bytes of the candidate after apply). The *expected* bytes of a manifest path in
  round `r` are: `fixloop/fixed/<path>` when the path is in `rs.fix.changed` of a fixed fix; else
  `applied/<path>` when stored; else `accepted/<path>`. After EVERY build of the candidate (first, confirming,
  after a dropped override) the candidate is synchronised with the expected state: a missing manifest path is
  restored (as `restore_missing` did) and a manifest path whose bytes differ is overwritten with the expected
  bytes. `intake.sync_tree(tree, expected: Callable[[str], Path], paths) -> tuple[list[str], list[str]]` (missing,
  changed; refuses symlinks; exec bit from the expected file) replaces the direct `restore_missing` call
  (`restore_missing` stays as a function). `RoundState.build_changed: list[str]` (sorted union over the round's
  builds, left out when empty); journal `build_side_changes` (`round`, `missing`, `changed`, first 5 changed
  paths) when any. `verify`, commit and freezing therefore always see: accepted + applied edits (+ the reported fix).
  (Superseded by "v2.1z-bm": the sync also runs right before commit and before a failed candidate is frozen, so
  `build_changed` also holds what a later Maven call changed; it compares against the stored table
  `rounds/<r>/expected.json` (`intake.sync_tree_table`) instead of re-reading `accepted/`; a path it cannot put back
  is no longer only logged: the round fails `internal_error` and nothing of it is delivered.)
- **Inside the loop's work copy** the build runner wrapper puts back, after each build, every tracked file the
  build modified or deleted that was clean before it, so an attempt is never blamed for (or credited with) the
  build's own edits. `FixResult.changed` = editable paths whose bytes differ from the candidate AND that the work
  copy's history records as changed by an attempt since the base commit; a byte difference outside that set is a
  build leftover and is ignored. A file that exists in the work copy but is neither a loop path nor build output
  (`target/` beside a pom, the work copy's `.git`) and was created by an attempt is a violation
  (`<p> must not be created`), found by walking the work copy at the end.
- **A loop that was killed.** When `_run_fix` finds an older `fixloop/` folder of the same round, it does not delete
  it: the folder is renamed `fixloop-interrupted-<n>/`, its LLM usage is recovered from the loop's own journal
  (the same reader the admin page uses for `llm call` lines, or the journal's usage events; whichever the code
  already has), and one `FixResult(attempted=True, fixed=False, reason="interrupted: the run ended during the
  loop", usage=…)` is appended to `rs.fix_discarded` and saved before the new loop starts. Usage that cannot be
  recovered is recorded as zero with `reason` ending ` (usage unknown)`. (Superseded by "v2.1z-bm": a leftover
  folder is settled at the start of the build node, of `_run_build` and of the report node too, whatever happens
  next, and the result is saved before the rename.)
- **`_drop_ineffective`** moves `rs.fix` into `fix_discarded` only when it was a fixed fix; a recorded not-fixed
  result stays where it is.
- **`_overlay_fix`** re-checks every path it copies with the fixer's `is_editable` rule and the manifest; a path
  that fails is treated as an unusable fix (discarded, reason `discarded: the stored fix is not usable`).
- `rs.fix.tree_hash` is computed and saved in the same save as `rs.fix` (the hash of the candidate's manifest
  paths with the fix's bytes substituted), not at the first overlay. (Superseded by "v2.1z-bm": computed from
  `rounds/<r>/expected.json` plus the fix's files, without reading the candidate.)
- **`fix.patch` of a source that is not UTF-8**: decoded with the detection the pom-charset code uses (UTF-8, then
  cp949); the patch is written as UTF-8 with a first line `# <path>: shown as UTF-8, the file is <encoding>` for
  such a file. Only bytes that decode under neither are `Binary files differ`.

### Report, route, i18n

- **One patch for the reader.** `write_report` also writes `remediation/fix.patch`: the patches of every committed
  fixed round, in round order, each under a line `# round <r>`; removed when there is none. The files route serves
  THAT file under the name `fix.patch` (state says such a round exists and the file exists; else 404, not listed).
  The report names the download once (`fix.patch`) and no longer points at per-round paths in the to-do line; the
  record keeps `rounds/<r>/fix.patch` per round.
- Record lines: 「빌드가 바꾼 파일 <n>개는 결과에 넣지 않고 원래대로 두었습니다.」 for a round with `build_changed`
  (with the first paths); the interrupted loop: 「버린 수정(실행이 수정 도중에 끝남) · LLM 호출 K회」.
- i18n reasons: `interrupted: the run ended during the loop` (with and without ` (usage unknown)`),
  `discarded: the stored fix is not usable`, guard reason `<p> must not be created`.

**Known limits, stated in the report's code comments and the runbook, not fixed:** a project whose build needs its
own `.git` (git-commit-id) cannot be built in the work copy, so its loop ends "not fixed"; Maven calls of the loop
that do not go through the build runner (effective pom, dependency tree) keep their own timeouts; the per-attempt
guard does not see a new file under a path the project's `.gitignore` ignores (the end check does).

**Tests.** The review's reproductions (`out/scratch/review-bj/test_repro_bj.py`, untracked) become regression tests
asserting the right outcome, plus: a build that rewrites a source and a pom (first build, confirming build, inside
the loop), the interrupted loop's usage, the cp949 patch, two fixed rounds and the one served patch.

**As coded (2026-11-20).** Deviations from the text above and names it did not give:
- **Telling a killed loop from a recorded one.** Before a loop starts the graph writes `fixloop/loop.json`
  (`{"loop": <token>, "round": r}`) and every result of that loop carries the token as `FixResult.loop_id` (left out
  of the JSON when empty). `_settle_old_loop`: an old folder whose token is on no recorded result of the round
  (`rs.fix`, `rs.fix_discarded`) was killed; a folder without the file counts as killed when the round records no
  attempted loop without a token. Killed → renamed `fixloop-interrupted-<n>/` (a failed rename: the folder is read in
  place, then removed), journal `remediation_fix_interrupted` (`round`, `folder`, `llm_calls`, `reason`).
  (Superseded by "v2.1z-bm": the usage is read, recorded and saved BEFORE the rename, a folder without `loop.json`
  gets a fresh token first, and a folder whose token is already on an interrupted result is only renamed.)
- `fixer.interrupted_result(folder, cfg)` reads the loop's own `journal.jsonl` with the admin page's
  `sbmig.web.admin.usage_from_journal` (its `llm_script` / `llm_verify_guide` events; the loop writes no
  `sbmig.log`) and prices the rows like a finished loop. No journal = the loop never reached an LLM call: zero,
  without ` (usage unknown)`; a journal that cannot be read → zero and ` (usage unknown)`. (Superseded by v2.1z-bp:
  the strict reader `admin.journal_usage_checked(strict=True)`; a damaged line or a journal missing beside `loop.json`
  is unknown with what was read kept; zero and known only for a folder with neither file.)
- `intake.sync_tree` leaves a path alone when its expected file does not exist, compares sizes before bytes, writes
  through a temporary file. `graph._package` is the one place a candidate is built: the build, then
  `_sync_candidate`. Journal `build_side_changes` = `round`, `missing` (count), `changed` (count), `paths` (first 5
  changed), `missing_paths` (first 5); restored files also still give `manifest_files_restored`. The state message is
  not changed by build-side changes; `sbmig.log` has one WARNING per build that changed files. (Superseded by
  "v2.1z-bm": `_package` is still the one place a candidate is built, but not the only sync: `_commit_round` and
  `deliver_failed_candidate` sync too; `sync_tree` tries every path and raises `SyncFailed`, which the graph no
  longer swallows.)
- `rounds/<r>/applied/` (`graph.APPLIED_DIR`, `_store_applied`) holds `ApplyResult.changed_files` plus every edit's
  `pom` whatever its name, made anew at each apply and removed with the candidate when an override is dropped
  (with `rounds/<r>/expected.json` since "v2.1z-bm").
- Inside the work copy the build wrapper (`fixer._DeadlineRunner`) restores build-side edits with the existing
  `Workspace.restore_build_byproducts` and records the untracked entries each build created (ignored ones included),
  which the end check does not count as created by an attempt; journal `remediation_fix_build_side` (`restored`, the
  first 20) in the loop's journal. "Changed by an attempt" = `ws.changed_between(base, head)`.
- On re-entry `_confirm_fix(fresh=False)` synchronises the candidate with the fix included and compares
  `intake.tree_hash` with `rs.fix.tree_hash` (since "v2.1z-bm" the hash comes from the stored table, see there): a different hash or a path that fails `_fix_paths` → discarded
  (`discarded: the stored fix is not usable`), the candidate synchronised without it, and the round builds as if no
  loop had run. A fix the loop has just returned that fails the check ends the round not fixed with that reason (no
  second loop on the same candidate). A stop right before the confirming build also halts STOPPED with
  `next_node = build` without building.
- `fix.patch` encoding line: `# <path>: shown as UTF-8, the file is MS949`. The combined `remediation/fix.patch`
  (`report.write_fix_patch`, `report.fix_patch_path`, `FIX_PATCH_NAME`) names a missing per-round file with a line
  `# round <r>: patch file missing` and a WARNING; the files route asks `report.served_fix_round` and that the file
  exists, so a combined file left by an earlier report is not served.
- Report: a fixed round that did not commit gets the record line 「빌드 오류 자동 수정: 수정본을 채택하지 않아 결과에
  넣지 않음 …」; the build-side line names the first paths; the to-do line points at 「빌드 오류 수정」 and `fix.patch`.
- Tests: `tests/remediations/test_fix_loop_review2.py` (stop around the confirming build, kill windows, a raising
  fixer, build-side changes in the first build / confirming build / loop, a pom under another name in `applied/`,
  the interrupted loop and its usage, cp949, the overlay re-check, the tree hash), `test_fix_patch_route.py` (one
  combined patch), `test_intake_build_output.py` (`sync_tree`), `test_report_fix_loop.py` (two fixed rounds,
  build-side line, interrupted line), `test_graph_fix_loop.py`, `test_i18n.py`; `tests/remediations/fakes.py` gives
  the fake intake the real `restore_missing` / `sync_tree`.

## v2.1z-bl — page: two report previews, trimmed areas, project name in the remediation titles; LLM usage totals

**Why.** The user, 2026-11-20: "보고서는 migration, remediation 각각 따로 보여질 수 있도록!" (a remediation-only run had
shown its short run report in the preview), "관리자에서 로그 파일들 받을 수 있으니까 그 밖의 파일 영역은 빼달래! 고급설정
영역이 빼줘!", and the remediation's fix-loop LLM calls were missing from the counts. Contract and as coded in one.

- **LLM usage totals** (`4054960`). `FixResult.usage: list[FixUsage]` (`model` = asked name, `served`, `calls`,
  `input_tokens`, `output_tokens`, `cost`) and `currency`, priced with `llm.pricing` like the migration's
  (`fixer._usage`; calls the loop's budget counted beyond the attempts go to the configured model's row). One reader:
  `fixer.fix_usage(state)` / `run_fix_usage(run_dir)` over `rounds[].fix` and `rounds[].fix_discarded` of
  `remediation/state.json` → `{calls, input_tokens, output_tokens, by_model}`. `chain.summary` adds `fix_llm_calls`
  (only when > 0); `result_line.report_llm_calls` makes `SBMIG-RESULT … llm=` the migration's calls plus those (chained
  and remediation-only runs). `GET /api/runs/{id}`: `llm_calls` = report calls + remediation calls
  (`runs.run_llm_calls`), `remediation_llm_calls` = the remediation part; the page replaces that part with the live
  `fix.llm_calls` of `GET /api/remediations/{id}` so a call is counted once. Admin page (`admin.add_remediation_usage`):
  per run the calls / tokens / cost include the remediation's rows (same asked name merged, cost unknown when a
  remediation row is unpriced or in another currency), the row field `remediation_calls` (only when > 0) and the cell
  note 「조치 N회 포함」; per model and per project totals include them. The migration's `report.json` is never
  rewritten. Tests: `tests/remediations/test_fix_usage.py`, `tests/web/test_admin_model_names.py`,
  `tests/web/test_page_remediation_fix.py`.
- **Two report previews** (`796eec2`). 「마이그레이션 보고서 미리보기」 (`#preview`, label `#preview-label`) stays at the
  end of the result card and exists only when a migration ran. 「조치 결과 보고서 미리보기」 (`#rem-preview`) sits inside
  the remediation card whenever the remediation's file list has `report.md`, and shows exactly `GET
  /api/remediations/{id}/files/report.md` (the bytes of the 「조치 결과 보고서」 download), fetched again only when the
  status's `updated` changes, not while a resume runs; a late answer for a run the user left is dropped. A
  remediation-only run shows its short run report under 「보고서 미리보기」 until the remediation report exists, then
  only the remediation preview. Each preview has 「보기」 / 「원문」 and 「크게 보기」 (the dialog shows the document of the
  preview that opened it). The remediation card's 「보고서 보기」 button is gone. Report text goes in only as text or
  DOM nodes. Tests: `tests/web/test_page_remediation_preview.py` (node against a small fake DOM),
  `tests/web/test_page_report_view.py`.
- **Trimmed areas** (`63cece1`). 고급 설정 (`<details id="adv" hidden>`) is not shown; its fields stay and their
  defaults are sent (superseded by "v2.1z-bm": while the block is hidden the start request carries none of them). The list of other run files (`#otherfiles`) is empty; only the notice about a missing result ZIP
  is left there. Logs and other files come from the admin page. Tests: `tests/web/test_page_trimmed_areas.py`.
- **Project name in the remediation titles** (`f107cd1`). `sbmig/web/contract.py`: `entered_project(run_dir)` = the
  name entered at upload from `control/request.json` (Job modes), else the `project` of the journal's `web_run` line
  (in-process; first 50 lines); `display_project(run_dir, run_id)` = that, else the project part of a web run id
  (`RUN_SLUG`, the admin page's pattern), None without any web trace (a direct CLI run). Display only, never a check.
  Remediation-only run: the short run report's title (`Orchestrator._web_project_name`) and the remediation report's
  title (`RemediationState.name`, 「취약점 조치 결과 — {name}」) use `display_project`; a chained remediation uses
  `entered_project` and keeps `source.zip` without one. A direct CLI run is unchanged. Tests:
  `tests/web/test_display_project.py`, `tests/remediations/test_chain_project_name.py`,
  `tests/loop/test_remediation_flow_project_name.py`.

## v2.1z-bm — third review of the remediation's fix loop: runs that span a deploy, sync failures, stop, usage, cost

**Why.** A third review of the fix loop (2026-11-20) reproduced five faults (`out/scratch/review-bk/test_repro_bk.py`,
untracked, asserted the faulty behaviour): (1) **a run that spans a deploy**: a run folder stopped at `build` by the
previous version has no `rounds/<r>/applied/`; after the resumed build the sync took `accepted/` as the expected bytes,
put the applied pom edits back to the old versions and filed them as build-side changes, so the round delivered (or
committed) a tree without the version change; (2) **a sync failure was swallowed**: `sync_tree` stopped at the first
path it could not write (a read-only folder a plugin left, a permission error), the graph only logged it, and the
candidate holding the build's edits was committed or frozen as the failed delivery; (3) **a stop during the loop was a
verdict**: a loop that ended `stop requested` was kept as the round's result, so the resumed run ended FAILED
`… automatic build fix: stop requested` without running the loop; (4) **a killed loop's LLM usage was lost** unless a
new loop started on resume (a resumed build that passed, the loop switched off or a stop during the build counted
nothing), and a kill between the folder's rename and the save lost it for good; (5) **cost**: every sync compared each
manifest path with `accepted/` byte for byte (two reads per same-size file per build), and `_fix_tree_hash` read the
whole candidate. Two more things came with them: Maven calls after the build's sync (`verify`'s resolve) could change
files that were then committed, and the page sent values a browser had restored into the hidden 고급 설정 controls.
Contract and as coded in one.

### The proof that a candidate is complete (`graph.py`)

- **Order in `_apply`** (and in `_drop_ineffective`, which remakes the candidate): `unmark_candidate` → remove the
  candidate, `rounds/<r>/applied/` and `rounds/<r>/expected.json` → copy from `accepted/`, apply, patch, text-only
  check → `_store_applied` (the edited files, hashed from the bytes already in memory) → `_write_expected` →
  `mark_candidate`. A kill anywhere before the marker leaves no marker.
- **`rounds/<r>/expected.json`** (`graph.EXPECTED_FILE`, written atomically) = `{schema: 1, round, applied,
  applied_files, files: {path: [size, sha256]}}` (superseded by v2.1z-bn: schema 2, rows `[size, sha256, exec]`; a
  schema-1 table is no proof): the table of `accepted/` (`_accepted_table`) with the files stored
  under `applied/` substituted; `applied_files` = the stored paths.
- **`rounds/<r>/candidate.ready.json`** (`CANDIDATE_READY`) = `{round, applied, expected}`, `expected` = the sha256 of
  `expected.json`. The key is the proof; a marker without it was written by an earlier version.
- **`_Holder.vouch_problem(r)`** returns "" or the reason, in this order: `WHY_NO_MARKER` 「no completeness marker」,
  `WHY_OTHER_APPLIED` 「the completeness marker names other proposals than the state」, `WHY_NO_CANDIDATE` 「the
  candidate folder is missing」, `WHY_NO_PROOF` 「no record that the applied files were stored completely」 (no or
  wrong `expected` sha, `expected.json` missing, naming another round or `applied`, without `files`, or an
  `applied_files` entry missing under `applied/`). `candidate_vouched(r)` = no problem.
- **The build node** first settles a leftover loop (below), then: a problem → journal `candidate_rebuilt` (`round`,
  `why` = the text above), WARNING `remediation round <r>: the candidate is made again from accepted/ (<why>)`, back
  to `apply`. A run folder of an older version is therefore re-applied (the applied edits come back from the plan, not
  from the old candidate). `deliver_failed_candidate` refuses an unvouched candidate as before (`candidate_not_vouched`).

### The sync (`intake.py`, `graph._sync_candidate`)

- **Against a stored table.** `intake.sync_tree_table(tree, expected, table, paths)`: per path, a candidate file whose
  size equals the table's is hashed once and left alone when the sha256 matches; only a missing or differing path opens
  `expected(path)`, and the copied bytes must hash to the recorded sha256 (else code `expected_changed`, the path
  fails). A path the table does not list falls back to `sync_tree`'s byte comparison. The graph uses it whenever
  `expected.json` exists (always for a vouched candidate), with the fix's files substituted (`_expected_table`,
  `_fix_rows`); without the table, `sync_tree`. `accepted/` is no longer re-read by the sync.
- **Where it runs.** After every build (`_package`: first build, confirming build, the build after a dropped
  override), right before commit (`_commit_round`) and right before a failed candidate is frozen
  (`deliver_failed_candidate`), each recording into `RoundState.build_changed` / journal `build_side_changes` as before.
  So a later Maven call's edit is put back and named too.
- **Every path is tried.** `sync_tree` and `sync_tree_table` go through all paths; a write refused with
  `PermissionError` is tried once more after the existing folders from the tree root down to the file and the file
  itself got the owner's write bit (folders also read + search; links never touched; `_make_writable_on_path`). Any
  path still not put back → `intake.SyncFailed` (a `RemediationError`: `missing`, `changed` = what was put back,
  `failed` = `[(path, error type, code)]` in path order; `code` = the first failure's code or `sync_failed`).
- **A candidate that cannot be put back is never committed or delivered.** `_sync_candidate` records what was put
  back, logs ERROR, journals `candidate_sync_failed` (`round`, `count`, `paths` = first 5, `errors` = their types) and
  raises `graph.CandidateNotRestored` (any other exception from the sync counts as one failed path `-`). The build node
  catches it: journal `candidate_not_restored` (same fields), `discard_candidate`, run FAILED `internal_error` with
  `the round <r> candidate could not be put back to its expected state after the build (<n> path(s), first: <p>:
  <ErrorType>); nothing of this round was delivered` (`CANDIDATE_NOT_RESTORED`; Korean 「라운드 <r>의 수정본을 빌드 뒤
  원래 상태로 되돌리지 못해 이 라운드의 결과는 전달하지 않았습니다(<n>개 경로, 첫 번째: <p>).」, i18n rule
  `candidate_not_restored`). Before a freeze the sync raises before anything is moved, so `failed_candidate: include`
  freezes nothing; an earlier verified delivery stays (`earlier_delivery_kept`).
- **Building the table without reading `accepted/`.** `_accepted_table` takes, in this order, the expected table of the
  last committed round (with that round's fixed files substituted, since a round commits its synced candidate), then
  `original/manifest.json`; each only when its `intake.table_hash` equals the round plan's `source_tree_hash`. When
  neither matches (a round committed by an older version), `intake.file_table(accepted/)` hashes it once.
- **`_fix_tree_hash`** = `table_hash` of `expected.json` with the fix's rows; it reads only the fix's files (the
  candidate only without a table). New intake functions: `file_table`, `table_hash`, `sync_tree_table`, `SyncFailed`
  (in the intake protocol beside `sync_tree`).

**Reads per round** (a passing round without the loop): `accepted/` once by the plan's tree hash and once by the copy
into the candidate, as before; `_write_expected` reads no tree when a recorded table matches (else `accepted/` once
more); each sync (two in such a round: after the build, before commit) reads each candidate file at most once (not at
all when the size differs) and opens an expected file only for a path that differs. A round with a fix adds one sync
per confirming build (and one on re-entry); a failed round syncs after the build and before the freeze.

### Stop, a killed loop, the page, wording

- **`stop requested` is not a verdict.** On re-entry `_run_build` moves a recorded result that ran and ended
  `stop requested` to `rs.fix_discarded` (usage still counted), saves, journals `remediation_fix_discarded`
  (`reason: stop requested`, `changed: 0`) and may run the loop again. `time budget used up` and every other
  not-fixed reason stay final.
- **A leftover `fixloop/` is settled** at the start of the build node, of `_run_build` and of the report node
  (`_settle_leftover_loop` → `_settle_old_loop(remove_recorded=False)`; the report node's failure is a WARNING only),
  whatever happens next. A folder without `loop.json` (killed before it was written) gets a fresh token written into
  it first. A killed loop's result (token as `loop_id`) is appended to `fix_discarded` and SAVED, journal
  `remediation_fix_interrupted`, and only then is the folder renamed `fixloop-interrupted-<n>/` (a failed rename removes
  it). A folder whose token is already on an interrupted result is only renamed, so the usage is counted once whatever
  the kill point. The folder of a normally recorded loop is removed only before a new loop starts.
- **The page's start request** ignores the hidden advanced controls: `advShown()` (= `#adv` not hidden);
  `chosenConfigs()` returns the server's default list while hidden; `startQuery(o)` adds `configs` (when not the
  default), `stop_after` (full flow) and `llm_provider` only when `o.adv`; the click handler sets `project` and `flow`
  beside it. The three controls and the config checkboxes carry `autocomplete="off"`.
- **Wording.** `i18n.fix_failed_ko` ends 「시도한 소스 수정은 결과에 넣지 않았습니다.」 (was 「소스 수정은
  되돌렸습니다.」: the loop never touched the candidate); the record line says 「… · 시도한 소스 수정은 결과에 넣지 않음」.
  (A loop that made no attempt has its own wording since v2.1z-bn: 「빌드 오류 자동 수정을 하지 못했습니다(<사유>).」.)

**Tests.** `tests/remediations/test_fix_loop_review3.py` (the reproductions turned around: an old-version run folder
resumed at `build` with a failing and a passing build; every kill point of `apply` with its `why`; the write order
applied → expected → marker; a read-only folder left by the build; a path that cannot be put back after a passing
build, after a failed build, in round 2 with round 1's verified delivery kept; `sync_tree` trying every path; a
read-only file in a read-only folder for both syncs; stop during the loop and resume, with the loop off, out of time;
a killed loop's usage when the resumed build passes, the loop is off, a stop comes during the build; a kill between
save and rename; a folder without `loop.json`; the report node; the open counts of `sync_tree_table`; a changed
expected file; a two-round run that never calls `file_table`; the fallback read; `_fix_tree_hash` reading no tree; the
sync before commit and before freeze), `tests/web/test_page_trimmed_areas.py` (`autocomplete`, `startQuery` run in
node), `test_i18n.py`, `test_report_fix_loop.py`, `test_intake_build_output.py`; `tests/remediations/fakes.py` gives the
fake intake the real `sync_tree_table`.

**Known limits.** The permission retry is not tested on Windows (`os.chmod` there toggles only the read-only flag);
`expected.json` grows with the number of files of the upload (one row per manifest path, written once per apply); a
fourth review and a real-Maven smoke run were still running when this was written.

## v2.1z-bn — fourth review of the remediation: one delivery swap, truthful end states, exec bit, pending report; the scan source

**Why.** A fourth review of the remediation (2026-11-20) reproduced its findings first
(`out/scratch/review-bm/test_repro_bm.py`, untracked, asserted the faulty behaviour): (1) **result ZIPs were written in
place**: the freeze node deleted `result.zip` before freezing, `deliver_failed_candidate` MOVED a verified ZIP aside
before freezing, and a freeze that raised (full disk, read-only folder) or a kill between the steps left a state naming
a ZIP that did not exist or had other bytes; (2) **a candidate that could not be put back after a PASSING build** was
reported as a failed build (the build outcome was recorded only after the sync, which raised first), and the report's
「결과 파일 — 없음」 sentence always blamed a failed build; (3) a failure while **preparing the fix loop's folder** escaped
as `internal_error` although the candidate was untouched; (4) **usage after an exception inside the loop** was priced
from the budget's call count with zero tokens (cost 0, not unknown); (5) the **exec bit** was not in the expected table,
so a build that only flipped it (or left a file `chmod 000`) went unnoticed; (6) a loop that **made no attempt** (LLM
unavailable at once) was worded as if source changes had been tried; (7) a **kill during the report node** left a final
state without `report.md` for good; (8) `intake.file_table` left out a non-regular path, so its `table_hash` differed
from `tree_hash`. The **real-Maven smoke run** (same day) found one more: with uploaded build output left out
(v2.1z-bh), the baseline scan still uploaded `original/source.zip` (which holds `<module>/target/…`) and a no-change
run delivered those bytes, while the report said the build output was left out of the scan and the result ZIP. Contract
and as coded in one.

### One delivery swap (`graph.py`)

- **`_Holder.install_delivery(make, r, *, build_ok, build_tolerated, keep_verified) -> str`** is the only writer of
  `delivery/result.zip`. Callers: the freeze node (`_freeze_delivery`), `deliver_failed_candidate`, `deliver_original`.
  Durable order, each step atomic:
  1. `make(delivery/.result.next.zip)` freezes the new ZIP under a temporary name (`graph.NEXT_ZIP`);
  2. the marker `delivery/.result.next.json` (`NEXT_JSON`) = `{delivery: <new DeliveryInfo>, verified: <kept path or "">}`;
  3. failed-candidate path with an earlier verified delivery (`keep_verified`): the current `result.zip` is COPIED
     (`_copy_atomic`: temporary file, fsync on a writable handle, `os.replace`) to `result-r<k>-verified.zip`, then its
     `.json` (`scanned: true`) is written;
  4. `os.replace(.result.next.zip, result.zip)`;
  5. `delivery.json` = the new info; 6. `st.delivery` = the new info and the state saved; 7. the marker removed.
  (Superseded by v2.1z-bo: 5. `st.delivery` + the caller's `after`, 6. `delivery.json`, 7. the state saved, 8. the
  marker removed; after the replace the swap is rolled forward and a record that cannot be written raises
  `DeliveryRecordNotWritten` with the marker kept.)
  Returns the kept verified ZIP's run-relative path or "".
- **A freeze that raises** (steps 1–4) removes what the call made and raises `graph.DeliveryNotWritten`; the previous
  `result.zip`, `delivery.json` and `state.delivery` are untouched. Journal `delivery_not_written` (`round`, `error` = the
  type, `kept_round` = the delivery's round or null), log ERROR `remediation round <r>: <message>`. The run ends FAILED
  `internal_error` with `DELIVERY_NOT_WRITTEN` = `the result ZIP of round {r} could not be written ({ErrorType}:
  {error}); {left}`, `left` = `delivery/result.zip is still the result ZIP of round {k}` (a delivery exists) or `no result
  ZIP exists; the original is preserved as original/source.zip`. In `_build_failed` the build's summary follows after
  ` | `. Korean (i18n rule `delivery_not_written`): 「라운드 {r}의 결과 ZIP을 쓰지 못했습니다({ErrorType}).」 + 「결과
  ZIP(delivery/result.zip)은 라운드 {k}의 결과 그대로입니다.」, for k = 0 「결과 ZIP(delivery/result.zip)은 바꾼 것이 없는
  이전 결과 그대로입니다.」, without a delivery 「결과 ZIP은 없고, 원본은 original/source.zip에 그대로 보관합니다.」.
- **`recover_delivery(paths, state) -> bool`** runs first in `run_remediation` (the state is saved when it changed;
  superseded by v2.1z-bo: also from `graph.repair_final`, i.e. every entry that sees the run folder again). With
  a readable marker: `result.zip` already has the marker's sha256 → rolled forward (`state.delivery` and `delivery.json`
  become the marker's info; WARNING `… a result ZIP swap that the run did not finish was completed (round <r>)`);
  otherwise rolled back (a verified copy this swap made is removed when it is a byte copy of the `result.zip` still
  there; WARNING `… was rolled back …`). Leftover `.result.next.zip*` files and the marker are always removed; an
  unreadable marker never raises. So a kill before the replace leaves the previous delivery, a kill after it ends with
  the new one, and the state always names a ZIP that exists with that sha256.

### Truthful end states (`graph.py`, `report.py`)

- **The build outcome is recorded before the sync can raise** (`_package`): a first build sets `rs.build` (and clears
  `build_compare`, `build_tolerated`, `fix_first_build`); a confirming build sets `rs.build` only when it passed. A
  `CandidateNotRestored` after a passing build therefore leaves the passing build on record.
- **「결과 파일 — 없음」** of a FAILED run without a delivery (`report._no_delivery_why`), followed by 「원본은 그대로입니다.」:
  message rule `candidate_not_restored` → 「수정본을 빌드 뒤 원래 상태로 되돌리지 못해 결과 ZIP을 만들지 않았습니다.」;
  rule `delivery_not_written` → 「라운드 <r>의 결과 ZIP을 쓰지 못했습니다.」; else by reason: `candidate_build_failed`
  「빌드에 실패한 수정본은 결과 ZIP으로 만들지 않았습니다.」, `baseline_build_failed` 「조치 전 빌드가 실패해 검사와 조치를
  진행하지 않았습니다.」, `text_only_violation` 「버전 표기 외의 변경이 발견된 수정본은 결과 ZIP으로 만들지 않았습니다.」,
  `resolution_mismatch` 「의존성 버전이 계획과 다르게 적용된 수정본은 결과 ZIP으로 만들지 않았습니다.」, `zip_hash_mismatch`
  「검사한 파일과 가진 파일이 같은지 확인하지 못해 결과 ZIP을 만들지 않았습니다.」, `execution_boundary_refused` / `not_ready`
  「조치를 시작하기 전에 멈춰 결과 ZIP을 만들지 않았습니다.」, `internal_error` 「도구 내부 오류로 결과 ZIP을 만들지
  못했습니다.」, any other code 「결과 ZIP을 만들지 않았습니다.」. Other statuses: 「없음」 alone.
- **A failing fix-loop folder setup** (settling an old `fixloop/`, removing it, `mkdir`, writing `loop.json`) is a loop
  that did not start: `FixResult(attempted=False, fixed=False, reason="error: <Type>: <message>")` (scrubbed), state saved,
  journal `remediation_fix_loop` `phase: not_started`, WARNING `remediation round <r>: the fix loop folder could not be
  prepared: <Type>`; the untouched failed candidate is delivered as for any not-fixed round.
- **A loop that made no attempt** (`graph.fix_made_no_attempt`: `attempted`, not `fixed`, `attempts == 0`, and not a
  result that had been a fix — a failed confirming build or a `discarded:` reason): message suffix
  ` | automatic build fix made no attempt: <reason>` (`FIX_NO_ATTEMPT`); Korean 「빌드 오류 자동 수정을 하지
  못했습니다(<사유>).」 (`i18n.fix_no_attempt_ko`, rule `auto_fix_no_attempt`; never 「시도한 소스 수정」); record line
  「- 빌드 오류 자동 수정: 하지 못함(<사유>) · LLM 호출 <n>회」 (+ the raw reason). `not started` (attempted false) and
  `automatic build fix:` (attempts made) keep their wording.

### Usage after an exception (`fixer.py`, `models.py`)

- When an exception escapes `run_fix_loop` before a result carried usage, the usage is read from the loop's own journal
  (`fixer.journal_usage(folder, cfg) -> (rows, currency) | None`, the reader `interrupted_result` uses too: `([], "")` without
  a journal, None when it cannot be read). Calls the budget counted beyond the journal's (all of them when the journal
  is unreadable) go to one row of the configured model with `FixUsage.tokens_unknown = True` and `cost: None`.
- `FixUsage.tokens_unknown: bool = False` (left out of the JSON when false). `fixer.fix_usage_rows` carries
  `tokens_unknown: true` on a row with such calls; that row's `cost` is None (unknown, never 0). (Superseded by
  v2.1z-bo: an unreadable journal marks the result `usage_unknown` instead of a zero-token row when the fixer itself
  raised; extra calls join a same-name row, which is then unpriced; budget-only calls of a normal result are
  `tokens_unknown` too. Superseded again by v2.1z-bp: `journal_usage` returns `(rows, currency, known)` and the
  extra calls always get a row of their own.)

### The exec bit and unreadable files (`intake.py`, `graph.py`)

- **`rounds/<r>/expected.json`** = `{schema: 2, round, applied, applied_files, files: {path: [size, sha256, exec]}}`.
  `graph._rows` accepts only three-element rows with a boolean exec bit; a schema-1 table (two elements, written by the
  never-deployed code of v2.1z-bm) is no proof (`WHY_NO_PROOF`), so the round goes back through `apply`. Fixed files
  take the exec bit of their row (an overwrite keeps the candidate file's mode); rows built from `original/manifest.json`
  take it from an lstat of the `accepted/` file (no read).
- **The sync** (`sync_tree`, `sync_tree_table`): a file with the right bytes whose exec bit differs from the expected
  one gets the canonical mode back (0755 / 0644) and counts as changed (`build_changed`, journal `build_side_changes`).
  A file a build left unreadable (`PermissionError` while comparing) is made readable once (`_make_writable_on_path` now
  adds the owner's read bit too), compared again, and its mode put back (also counted as changed).
- **`intake.file_table`** records a path that is not a regular file (folder, link) as `(0, "NOT_REGULAR", False)`, like
  `tree_hash`, so `table_hash(file_table(t)) == tree_hash(t)` in that case too.

### A final state always gets its report (`graph.py`, `chain.py`, `service.py`)

- `graph.report_written(paths, state)`: `report.md` exists and `report.json["state"]["updated"] == state.updated`.
  `graph.report_pending(paths, state)`: status in `TERMINAL`, `next_node == "report"`, not written (a state that ended
  outside the graph — an intake refusal, a failed start — is never pending). `graph.write_pending_report(state, paths,
  cfg, *, save, journal) -> bool` runs only the report node (through `_wrap`, same stamps and saves, the combined
  `fix.patch` included) with journal `report_rewritten` (`status`) and WARNING `remediation <id>: the report of the
  <STATUS> run was not written; writing it now`.
- Called on the next entry of the run folder: `run_remediation` (a terminal state: report only, nothing else runs);
  `chain.run_after_migration` / `chain.run_only` for an existing state (`_repair_report`, under the run claim; another
  holder → the state as it is; a failure is a WARNING `… the report was not written: …`); `chain.resume` — a pending
  final state is no longer refused: journal `remediation_report_repair` (`remediation`, `status`), the report written,
  the state returned; `service._recover` at startup in in-process mode (`_write_pending_report`, under a `RunClaim`
  `purpose="service"`; a failure never stops the server). (Superseded by v2.1z-bo: startup recovery in every mode,
  and the web service repairs a final state whose owner is gone at `status` / `result` / `file_path`; all through
  `graph.repair_final`.)

### The scan source (`graph.py`, `models.py`, `report.py`)

- **When `intake.build_output_files > 0`** the baseline scan uploads `original/scan-source.zip` (`RunPaths.scan_source`):
  `intake.freeze_delivery(accepted/, …, round 0, manifest paths)`, deterministic. `_Holder.scan_source() -> (path,
  sha256)`: made on first use, its sha256 recorded in `RemediationState.scan_source_sha256` (left out of the JSON when
  empty; older states read back), saved, journal `scan_source_made` (`sha256`, `files`, `left_out`), INFO `remediation
  <id>: the scan source ZIP leaves the upload's <n> build output file(s) out (<sha>)`. Later uses take the file when it
  has the recorded sha256, else make it again; another sha256 raises `graph.ScanSourceChanged` → FAILED
  `zip_hash_mismatch` `the scan source ZIP (original/scan-source.zip) could not be made again with the sha256 recorded
  in the state` (Korean 「검사용 ZIP(original/scan-source.zip)을 처음 검사한 것과 같은 바이트로 다시 만들 수 없습니다.」).
- **A no-change delivery** (round 0, `deliver_original`) is a copy of the scan source through `install_delivery`; the
  copy must hash to the scan source's sha256 (else `ScanSourceChanged`). Every "the scanned bytes are the bytes we hold"
  check compares against it: IVAS answers `IVAS analysed bytes whose sha256 differs from the scan source ZIP` /
  `IVAS scanned a ZIP whose sha256 differs from the scan source ZIP` (Korean 「IVAS가 분석한 파일의 sha256이 검사용 ZIP과
  다릅니다.」 / 「IVAS가 검사한 ZIP의 sha256이 검사용 ZIP과 다릅니다.」); the original's sha check before the upload is
  made only when the original is what is uploaded.
- **Without left-out build output nothing changes**: the upload is scanned and delivered byte for byte, no
  `scan-source.zip`. **A run the deployed version started** (no recorded scan-source sha, and its pending upload, receipt
  or baseline scan names the original's sha256: `_scanned_original_before`) keeps the original.
- `original/source.zip` stays the upload; the files route still serves it as `source.zip`; `scan-source.zip` is not
  served. A chained source ZIP (`chain.source_zip`) never carries build output, so a chained run scans the original.
- **Report.** The note 「업로드한 ZIP의 빌드 산출물 <n>개(…)는 검사와 결과 ZIP에서 제외했습니다. …」 is left out when the
  baseline scan was of the original (its `zip_sha256` equals the upload's). The round-0 line says 「결과 ZIP은 원본 ZIP에서
  빌드 산출물만 뺀 것입니다.」 only when build output was left out AND the delivery is not the upload's bytes, else
  「결과 ZIP은 원본 ZIP과 같은 파일입니다.」.

### Run folder (as now)

`original/source.zip` (the upload, never changed), `original/manifest.json`, `original/scan-source.zip` (only with
left-out build output), `accepted/`, `baseline/`, `rounds/<n>/` (`expected.json` schema 2), `scans/`, `delivery/`
(`result.zip`, `delivery.json`, a kept `result-r<k>-verified.zip` + `.json`, and only during a swap `.result.next.zip` +
`.result.next.json`), `state.json`, `journal.jsonl`, `remediation.log`, `report.md`, `report.json`, `fix.patch`.
New journal events: `delivery_not_written`, `scan_source_made`, `report_rewritten`, `remediation_report_repair`.

**Tests.** `tests/remediations/test_fix_loop_review4.py` (the reproductions turned around: a failed freeze of the failed
candidate keeps the verified delivery, a failed freeze in the freeze node keeps round 1, a failed first freeze says
there is none; every kill point of the swap leaves or repairs a truthful delivery; recovery without a marker; not
restored after a passing build; every no-delivery sentence; a failing folder setup for `loop.json` and an old folder;
usage from the loop's journal, an unreadable journal, calls beyond the journal; the exec bit in both syncs, an
unreadable file, a build that only flips a bit, a schema-1 table; the no-attempt wording per reason and the results it
spares; a kill during the report node with and without `fix.patch`, a state ended outside the graph, `chain.resume`, the
in-process service at startup; `table_hash` of `file_table` for a folder at a manifest path),
`tests/remediations/test_scan_source.py` (left-out output neither scanned nor delivered; a finding compares like with
like; nothing left out = byte for byte; deployed-version runs waiting on a receipt or past the baseline keep the
original; an uncertain upload then a receipt keeps the sha; made again = same bytes; cannot be made again → FAILED; the
chained source ZIP; the files route; old `state.json`), `test_i18n.py`; earlier review tests adjusted for schema 2.

**Still open** (as the code's author reported; the first two are covered by v2.1z-bo's web-service repair, which runs when the page reads the run): in Job mode a chained remediation whose report node was killed gets its
report only when something calls `chain.resume` / `run_after_migration` again, and the page offers resume only for
resumable states; in-process chained runs are not repaired at startup (`service._recover` reads the standalone folder);
the permission and read retry is not tested on Windows; a fifth review and a second real-Maven smoke run were running
when this was written.

## v2.1z-bo — fifth review of the remediation and the second real-Maven run: swap records, no re-upload, report repair in every mode, unknown usage, stop before an LLM request

**Why.** A fifth review of the remediation (2026-11-20) reproduced its findings first (`out/scratch/review-bn/test_bn_a.py`,
untracked, asserted the faulty behaviour): (1) a failure **after the result ZIP was replaced** (`delivery.json` or the
state could not be written, full disk) escaped as a plain `internal_error` whose message said nothing about the new
ZIP, and the swap marker was removed only on success; (2) **mid-node saves** left a state that a re-entry read
differently from the uninterrupted run: a kill right after the no-change delivery's save re-entered `baseline_scan` and
**uploaded the baseline to IVAS a second time**; a kill after an unusable stored fix was discarded ran the loop again;
a failed loop-folder setup was forgotten and the loop ran on re-entry; (3) a chained (Job-mode) final state whose report
node was killed got its report only when something called `chain.resume` / `run_after_migration` again; (4) **usage that
could not be read** was shown as 0 calls and priced as 0; (5) after `chain._baseline_outcome` changed the message the
report still carried the old one. The **second real-Maven smoke run** (on `9dc249f`) noticed the rest: a stop or the time
budget was checked between builds only, so a loop kept asking the LLM after a stop; the fix section counted only the
fixing loop's calls; a stop during the build fix was worded like a stop during the candidate build; the round-0 to-do
step said 「재검사를 마친」 for a ZIP that was scanned once; an interrupted loop's work copy (with its own `.git` and
`target/`) stayed on disk. Contract and as coded in one.

### The delivery swap after the replace (`graph.py`)

- **`_Holder.install_delivery(make, r, *, build_ok, build_tolerated, keep_verified, after=None) -> str`.** Steps 1–4
  as in "v2.1z-bn" (temporary ZIP, marker, verified copy, `os.replace`). Then, rolled FORWARD (never back):
  5. `st.delivery` = the new info and `after(info)` — the caller's other fields that describe the new ZIP (the
     no-change delivery sets `final_scan` = the baseline scan and `delivery.scanned`), so the state saved next is
     complete for the node's re-entry; 6. `delivery.json` written; 7. the state saved; 8. the marker removed (a failure
     to remove it is a WARNING; the next entry removes it).
  Steps 6 and 7 are both tried; when either fails the marker stays and **`graph.DeliveryRecordNotWritten(round,
  failed, root)`** is raised (`failed` = [(run-relative record path, exception)]), with `st.delivery` already the new
  ZIP. Message `DELIVERY_RECORD_NOT_WRITTEN` = `delivery/result.zip is now the result ZIP of round {r}, but {what} could
  not be written ({ErrorType}: {error}); the record is completed the next time this run is opened`, `what` =
  `delivery/delivery.json`, `state.json` or both joined by ` and `. `_wrap` (and `_build_failed`, with the build summary
  after ` | `) ends the run FAILED `internal_error` with it. Journal `delivery_record_not_written` (`round`, `records`,
  `error` = the first type), log ERROR `remediation round <r>: <message>`. Korean (i18n rule
  `delivery_record_not_written`): 「결과 ZIP(delivery/result.zip)은 이제 라운드 {r}의 결과입니다.」 (round 0: 「…이제 바꾼
  것이 없는 결과입니다.」) + 「다만 기록 파일({what, ", "})을 쓰지 못했습니다({ErrorType}). 이 실행을 다음에 열 때 기록을 마저
  맞춥니다.」.
- A **`ScanSourceChanged`** raised by `make` (the no-change copy) is cleaned up like a write failure but raised as it is,
  so the run ends `zip_hash_mismatch`, not `internal_error`.
- **`recover_delivery(paths, state) -> bool`** also removes a leftover `delivery/.result-r*-verified.zip.copy-tmp` (a
  kill during the verified copy). **`graph.delivery_swap_pending(paths) -> bool`**: the marker, `.result.next.zip`, a
  `.copy-tmp` or a `..result.next.zip.*` temporary is left.
- **Who calls it** (every entry that can see a run folder again): `run_remediation` (first, state saved when changed),
  and **`graph.repair_final(state, paths, cfg, *, save, journal) -> bool`** = `recover_delivery` (state saved, journal
  `delivery_recovered` with `round`) then `write_pending_report`; the caller holds the run claim; status, reasons and
  message are left as they are. `repair_final` runs from `run_remediation` for a terminal state, `chain._repair_report`,
  `chain.resume` (a final state with a pending swap or report: journal `remediation_report_repair`, not refused), the
  existing-state paths of `chain.run_after_migration` / `chain.run_only` (terminal states only), the service's startup
  recovery and `RemediationManager.repair_final`. **`graph.final_repair_needed(paths, state)`** = status in `TERMINAL`
  and (`delivery_swap_pending` or `report_pending`).

### No re-entry does what the uninterrupted run would not (`graph.py`)

Rule: everything a node sets is in place before any save inside the node, so a kill right after that save re-enters the
node (`next_node` is the node while it runs) with a state that already says what the uninterrupted node would have
left. In particular **never a second IVAS upload**.

- **`_baseline_scan`** reuses a stored baseline scan (`_stored_baseline(h, sha)`): the state names
  `scans/baseline.json`, no `pending_upload` / `pending_receipt` for `baseline`, and the stored result is complete with
  the sha256 about to be uploaded → no upload, journal `baseline_scan_reused` (`sha256`), INFO `remediation <id>: the
  stored baseline scan of the same bytes is used; nothing is uploaded again`.
- **Forgetting a loop result** on entry to `_run_build`: only a result that did not start (`attempted: false`) because
  of `stop requested` or `time budget used up` is cleared so the loop may start; a folder setup that failed
  (`error: …`, not started) stays the round's verdict. A result that ran and ended `stop requested` moves to
  `fix_discarded` as in "v2.1z-bm".
- **`_run_build(h, r, cand, *, rerun_ok=False)`**: when the round's last discarded fix is `discarded: the stored fix is
  not usable` and no fix is recorded, the loop is not run again at the node's entry and the round ends as the
  uninterrupted run did, the message ending ` | automatic build fix: discarded: the stored fix is not usable`;
  `rerun_ok=True` (the call right after a stored fix of an earlier entry was found not usable on this one) allows it.
  (Superseded by v2.1z-bp: the skip rests on `FixResult.ended_round`, set only when a fresh loop's fix was unusable,
  not on the reason text; a stored fix found unusable on resume no longer blocks the loop on the next entry.)
- **`deliver_failed_candidate`** re-entered after its own swap (`st.delivery` is round r, `build_ok` and
  `build_tolerated` false) names the verified copy the earlier entry made (`find_verified_zip`, round < r) in its
  message.

| Save inside a node | What is in the state | A kill right after it, then the re-entry |
|---|---|---|
| `install_delivery` step 7 from `deliver_original` in `baseline_scan` (no actionable finding) | delivery round 0, `final_scan` = baseline, `delivery.scanned`, `baseline_scan`, no pending upload | `baseline_scan` again: the stored scan is reused (`baseline_scan_reused`), the same bytes delivered, `evaluate` → the uninterrupted verdict; IVAS saw one upload |
| the same from `plan` / `apply` / the ineffective-override remake (nothing left to apply) | as above | the node again; `deliver_original` is skipped because a delivery exists → `evaluate`, same verdict |
| `install_delivery` step 7 in `freeze_delivery` | the new delivery (round r) | the node again: the same deterministic ZIP of `accepted/` (same sha256) is installed once more → `rescan` |
| `install_delivery` step 7 in `deliver_failed_candidate` (`build` node) | the failed candidate as delivery, a verified copy made | `build` again (a build, no upload; the recorded loop result is not re-run); the failed candidate is delivered again and the message names the earlier copy (`again`) |
| right after the fix loop returns (`_run_fix`, v2.1z-bk) | `rs.fix` with usage and `tree_hash` | the recorded result is used (overlay + confirming build, or not fixed); the loop does not run again |
| `_discard_fix` before `_drop_fix_files` | the fix in `fix_discarded`, `rs.fix` None | an unusable stored fix: no second loop (`rerun_ok`), FAILED `candidate_build_failed` with the uninterrupted suffix (superseded by v2.1z-bp: only when the discard carries `ended_round`; see its table); a fix discarded for a remade candidate (the candidate was unmarked first) sends the round back through `apply`, as the uninterrupted run remakes it |
| confirming build failed, before `_drop_fix_files` | `rs.fix` not fixed, `confirming build failed` | the loop is not run again; `fixed/` and `fix.patch` may stay on disk unreferenced (still open) |
| a loop folder that could not be prepared | `rs.fix` `attempted: false`, `error: …` | the verdict stays; no loop (`automatic build fix not started: error: …`) |
| a loop not started because of a stop or the time budget | `rs.fix` `attempted: false`, `stop requested` / `time budget used up` | the result is forgotten and the loop may start |
| `_settle_old_loop` before the rename (v2.1z-bm) | the interrupted result with its usage | only the rename (and the `tree/` removal) is redone; usage counted once |
| the ineffective-override decision (`next_node = apply`, v2.1z-ap/bm) | the kept proposals | `apply` remakes the candidate from `accepted/` |
| `chain._baseline_outcome` (new message, new `updated`) | the message the report must carry | the report is pending → written on the next entry or by the web service |

### A final state is repaired in every mode (`service.py`, `chain.py`)

- **`RemediationManager.repair_final(rid, paths, st, chained) -> RemediationState`**, called at the top of `status()`,
  `result()` and `file_path()` (for `report.md`, `report.json`, `result.zip`, `fix.patch`; a repair failure never fails
  the file). For a `TERMINAL` state with `graph.final_repair_needed` whose owner is gone (`_owner_gone`: not in this
  process's `_active`; for a chained run no resume Job queued or running and the migration run ended — the same
  detection as the page's "its build pod is gone"), under `_run_lock(rid)` and `RunClaim(paths.root,
  purpose="service")` (a fresh claim of another process → nothing now), the state is read again and `graph.repair_final`
  runs with the run's configs; journal `remediation_final_repair` (`remediation`, `status`, `by: "service"`), WARNING
  `remediation <id>: its run has ended with the <STATUS> state not fully recorded; the web server completes it`. Status,
  reasons and message are not changed. Remembered in `_repair_seen[folder] = (updated, checked at, nothing left)`: a
  folder found in order is not read again until `updated` changes; one that still has something to do is tried again at
  most every `REPAIR_RETRY_S` = 60 s.
- **At startup** (`_recover` → `_write_pending_report`), in every mode now: a standalone final folder with
  `final_repair_needed` gets `graph.repair_final` once under a `RunClaim(purpose="service")`.

### Unknown usage (`models.py`, `fixer.py`, `chain.py`, `report.py`, `runs.py`, `admin.py`, `admin.html`)

- **`FixResult.usage_unknown: bool = False`** (left out of the JSON when false): the loop's usage could not be read at
  all, so its zero calls / tokens are not exact. Set by `fixer.usage_from_folder(res, folder, cfg) -> bool` (an
  unreadable loop journal: no usage rows, `usage_unknown`, returns False), used by `interrupted_result` (reason still
  ends ` (usage unknown)`) and by the graph when the fixer raises; and by `run_fix_loop` when counting the usage after an
  exception raised itself.
- Rows with `tokens_unknown` are never priced: `_usage` gives the calls the budget counted beyond the attempts' usage to
  the configured model's row with `tokens_unknown: true` and no cost; `_usage_after_error` adds such calls to a row of
  the same name (then unpriced) or a new row; `fix_usage_rows` sets `cost` None on such a row; the currency is dropped
  when no row is priced. (Superseded by v2.1z-bp: such calls get a row of their OWN, the known row of the same name
  keeps its tokens and price; `fix_usage_rows` keys rows by name and unknown; an unreadable or damaged journal keeps
  what was read, `fixer.journal_usage` returns `(rows, currency, known)`.)
- **`fixer.fix_usage(state)`** adds `calls_unknown` (a result with `usage_unknown`; `calls` is a lower bound),
  `tokens_unknown` (a row with calls without token counts) and `usage_unknown` (either).
- Readers: `chain.summary` → `fix_usage_unknown: true` (only then; `fix_llm_calls` = the known calls, so the
  `SBMIG-RESULT … llm=` token still prints an integer of KNOWN calls); `GET /api/runs/{id}` →
  `remediation_llm_usage_unknown` (bool, always present); `GET /api/remediations/{id}` → `fix.llm_usage_unknown: true`
  (`report.fix_summary`, only then); admin run row → `usage_partly_unknown: true` (only then) and `cost: null` (a
  remediation whose usage is partly unknown makes the run's cost unknown; a run without calls is 0 only when nothing is
  unknown); the admin page writes 「일부 알 수 없음」 under the calls cell and 「알 수 없음」 in the cost cell (title
  「사용량 일부를 알 수 없어 비용을 계산하지 않았습니다」). (Superseded by v2.1z-bp: `cost` is the sum of the priced rows
  when at least one row is priced, with 「일부 알 수 없음」 under it too; `null` / 「알 수 없음」 only when nothing is priced.)
- Report: a record line of a result with `usage_unknown` → 「- 빌드 오류 자동 수정: <결과> · LLM 사용량 일부는 알 수 없음」;
  a call count with `tokens_unknown` rows gets 「 · LLM 사용량 일부는 알 수 없음」; an interrupted loop → 「LLM 호출 수는
  알 수 없음」; the fix section's call sentence ends 「 LLM 사용량 일부는 알 수 없습니다.」.

### The report and the state agree (`chain.py`, `graph.py`, `report.py`)

- **`chain._baseline_outcome(paths, state, jr, excluded, cfg=None)`**: after it rewrites the message it stamps a new
  `updated` (the report becomes pending), saves, and writes the report once more (`write_pending_report`, with `cfg`); a
  failure is a WARNING `chained remediation <id>: the report was not written again: …` and the next entry repairs it.
- A report node that fails appends ` | report not written: <Type>: <msg>` (`graph.REPORT_NOT_WRITTEN`) **once**; a
  later failing entry keeps the message and the earlier `updated`.
- The kept copy of a tolerated-baseline run (`build_tolerated` and not `build_ok` in its `.json`) is never called
  verified: message `…; the scanned delivery of an earlier round (its build was not confirmed) is kept as <path>`
  (`_Holder.kept_unconfirmed`), Korean 「이전 라운드에서 재검사한 결과 ZIP(빌드로는 확인하지 못함)은 <path>에 있습니다.」;
  report 「라운드 <k>에서 재검사를 마친 결과 ZIP(빌드로는 확인하지 못함)은 …」 / 「- 라운드 <k>에서 재검사한 결과 ZIP(빌드로는
  확인하지 못함): …」 (`_verified_kept` → `unconfirmed`).
- **The fix section's call sentence** (`report._fix_calls_sentence`): 「- 빌드와 수정을 N회 반복했고, LLM을 M회
  호출했습니다.」, and when the round had other loops (`fix_discarded`: discarded, interrupted, stopped) 「…LLM을 M회
  호출했습니다(버린 시도까지 모두 T회).」. (v2.1z-bp: 「알려진 것만 T회」 when part of the usage is unknown.)
- **A stop during the build fix**: a loop that ran and ended `stop requested` while the stop is still pending halts
  STOPPED `stop_requested` at node `build` with `FIX_STOPPED` = `stop requested during the round {r} build fix` (no
  suffix); Korean (rule `stop_round_fix`) 「라운드 {r}의 빌드 오류 자동 수정 중에 중지 요청을 받았습니다.」. A stop
  during a candidate build keeps `stop requested during the round {r} candidate build`.
- **Round-0 to-do step**: a SUCCEEDED run whose delivery is round 0 says 「**결과 ZIP 받기** — 검사를 마친
  `delivery/result.zip` 파일을 받아 사용해 주세요.」 (other rounds: 「재검사를 마친」).

### The fix loop checks stop and time before every LLM request (`sbmig/loop/fixloop.py`)

The migration's `BuildFixLoop`, so the migration and the remediation alike. **`BuildFixLoop._halt_before_ask() -> str`**
runs before every LLM request (`propose_script` for a step that needs the LLM, `verify_guide`): the workspace's stop
check first (`ws.stop_requested`: the web's `stop.request`, the remediation's stop and deadline; an exception reading it
is no stop), then `budget.time_exceeded()`. A hit returns `"stop"` / `"time"`, sticks for the rest of that loop run
(`_ask_halt`, reset at each `_run`), journals `llm_ask_skipped` (`phase`, `why`) and logs INFO. The case is not tried
(`_handle_cluster` → `"halted"`), the remaining cases of the round are not asked, the inner re-ask loop ends, and when
nothing was applied in that round the loop ends through the same path as a check between builds
(`_leave_on_stop`: note, journal `stop_request` `where: "fix loop"`, `left_on_stop` / `stopped_by_request`;
`_leave_on_time`: note, flag `time-budget-exhausted`, journal `budget_exhausted`, the remaining clusters blocked
`BUDGET_EXHAUSTED`); the tree is the last build's, so the existing "the loop was left on a stop request and the tree is
unchanged → that build is the final build" rule holds. When something was applied, the check after the next build ends
it the same way. A `verify_guide` that would be asked is skipped.

### Interrupted loops (`graph.py`)

After `_settle_old_loop` renamed a killed loop's folder to `fixloop-interrupted-<n>/` (its usage recorded and saved),
`_drop_work_copy` removes its `tree/` (the work copy with its own `.git` and `target/`); the journal and the logs stay.
Best effort: WARNING `remediation round <r>: the work copy of the interrupted fix loop was not removed (<path>):
<Type>`.

### Names

Journal (remediation): `delivery_record_not_written`, `delivery_recovered`, `baseline_scan_reused`,
`remediation_final_repair`. Journal (the loop's own, also the migration's): `llm_ask_skipped`. API / JSON:
`FixResult.usage_unknown`; `fix_usage()` `calls_unknown` / `tokens_unknown` / `usage_unknown`;
`chain.summary.fix_usage_unknown`; `GET /api/runs/{id}` `remediation_llm_usage_unknown`; `GET /api/remediations/{id}`
`fix.llm_usage_unknown`; admin run row `usage_partly_unknown`. Code: `graph.DeliveryRecordNotWritten`,
`DELIVERY_RECORD_NOT_WRITTEN`, `REPORT_NOT_WRITTEN`, `FIX_STOPPED`, `delivery_swap_pending`, `repair_final`,
`final_repair_needed`, `_stored_baseline`, `_drop_work_copy`; `fixer.usage_from_folder`;
`RemediationManager.repair_final`, `_owner_gone`, `REPAIR_RETRY_S`; `BuildFixLoop._halt_before_ask`, `_leave_on_stop`,
`_leave_on_time`. Files: `delivery/.result-r<k>-verified.zip.copy-tmp` (only during a verified copy).

**Tests.** `tests/remediations/test_fix_loop_review5.py` (the reproductions turned around: a full disk writing
`delivery.json` after the swap rolls forward and says so, a failing state save names `state.json` and keeps the marker,
`chain.resume` of a final state repairs `delivery.json`; a kill after a no-finding delivery uploads once; a kill after the
no-change swap in `plan` resumes like the uninterrupted run; a kill after an unusable fix was discarded does not run the
loop again; a failed folder setup stays the verdict; a loop not started because of a stop may start on resume; the web
service repairs a chained final state whose build pod is gone, keeps away from a live owner and from a fresh claim,
repairs a standalone final state at startup in Job mode, completes a half-done swap; unknown usage from an unreadable
journal and from a fixer exception, budget-only calls never priced, the readers and the report say unknown; the
baseline-outcome message reaches the report; a failing report noted once; `ScanSourceChanged` in the no-change copy; a
leftover copy temp file; the tolerated run's kept copy wording; the round total in the fix section; the stop wording;
the round-0 step; the interrupted work copy removed, and a removal failure a WARNING only),
`tests/loop/test_fixloop_ask_halt.py` (a stop before the first ask asks nothing; a stop between two no-op asks ends the
loop; the time budget passing between two asks ends it like between builds; the remediation's loop stops asking after a
stop during an ask), `test_fix_usage.py`, `test_i18n.py`. 7,176 passed.

**Still open** (as the code's author reported): a stored fix whose files vanish between the save and a kill runs the
loop once more; after a kill between the confirm-failed save and the file removal, `fixed/` and `fix.patch` stay on disk
unreferenced; the upload page's chip does not show the unknown-usage marker; a killed pod's fresh claim keeps the web
repair waiting up to 600 s (the claim's takeover time); a call in flight when a pod is killed is never counted. A third
smoke pass on `4600c02` and a narrow verification review of this commit were running when this was written.

## v2.1z-bp — verification of v2.1z-bo and the real-Maven passes: the loop is not skipped on resume, usage rows, strict journal reading

**Why.** The verification review of `4600c02` (v2.1z-bo) found one defect: `_run_build` skipped the fix loop whenever
the round's last discarded fix had the reason `discarded: the stored fix is not usable`. That reason is written in two
situations — a fix the loop had JUST returned that could not be applied (the uninterrupted run ends the round there), and
a fix stored by an earlier entry found unusable on resume (the uninterrupted resume runs the loop again). A resume of
the second kind cut short by a stop or a kill before the loop restarted therefore ended FAILED `candidate_build_failed`
on its next entry. The same review confirmed that the change in `sbmig/loop/fixloop.py` leaves the migration loop's
behaviour unchanged when no stop is requested and the time budget has not passed. The third real-Maven pass (on
`4600c02`) found two more: a killed loop whose journal could not be parsed or was missing was counted as an exact zero
(the reader skipped bad lines), and `remediation.fix_loop_max_minutes: 1` never started the loop (the fixer checked
the 60 s minimum again, a few milliseconds after the graph had granted exactly 60 s). The review also noted that one
failed LLM call (calls the budget counted without token counts) made the whole same-model row unpriced and the admin
run cost unknown, and smaller wording / save points listed below. Contract and as coded in one.

### Rules

- **A decision at a node's entry rests on a marker written for that purpose, never on the reason text of the last
  record.** `FixResult.ended_round: bool = False` (left out of the JSON when false; older JSON reads false) is set by
  `graph._discard_fix(h, r, reason, *, ends_round=False)` in the same save that moves the fix to `rs.fix_discarded`.
  Only `_confirm_fix` passes `ends_round=True`, and only on the fresh path (`fresh` and a first build recorded): the fix
  the loop had just returned could not be overlaid. `_run_build` skips the loop at the node's entry when no fix is
  recorded, `rerun_ok` is false and `rs.fix_discarded[-1].ended_round`. A stored fix found unusable on resume is
  discarded with `ended_round` false, so the next entry runs the loop — also when a stop or a kill cut that resume short
  before the loop restarted.
- **Calls without token counts get their own row.** `fixer._usage`: calls the loop's budget counted beyond the
  attempts' usage are a separate `FixUsage(model=asked_model_name(llm.model), calls=extra, cost=None,
  tokens_unknown=True)`; every known row keeps its tokens and its price (`cost.compute_llm_cost`); `models` lists each
  name once. `_usage_after_error` does the same for the calls beyond the journal's. `fix_usage_rows` keys rows by
  (lower-case name, unknown), so a known row and an unknown row of the same name stay apart; `tokens_unknown: true` and
  `cost: null` only on the unknown row.
- **The admin run cost.** `admin.add_remediation_usage`: rows with `tokens_unknown` add their calls to the row of their
  name and nothing to any cost; the run's `cost` is the sum of the priced rows when at least one row with calls is
  priced (migration or remediation), with `usage_partly_unknown: true`; `null` only when nothing is priced. A known row
  that has no configured price still makes the cost `null` (as before). The page writes 「일부 알 수 없음」 under the call
  count and, when the cost is a number, under the cost too; 「알 수 없음」 (title 「사용량 일부를 알 수 없어 비용을 계산하지
  않았습니다」) only for a `null` cost with the flag.
- **Strict journal reading for a loop's usage.** `admin.journal_usage_checked(run_dir, strict=False) -> (rows,
  complete)`: the rows of `usage_from_journal` (which now calls it, non-strict, unchanged for its other callers) and
  whether the file was read to the end with every parsed line a JSON object; `strict` parses every non-blank line, not
  only the lines mentioning `"llm_`; a missing journal is `({}, True)`. `fixer.journal_usage(folder, cfg) -> (rows,
  currency, known) | None`: `([], "", True)` for a folder with neither `journal.jsonl` nor `loop.json` (no loop started);
  `known` false when a line cannot be parsed (a line a kill cut off, garbage), the file cannot be read to the end, or
  `journal.jsonl` is missing beside `loop.json`; the rows read are kept; None when the journal exists but cannot be
  opened. `usage_from_folder` keeps what was read and sets `usage_unknown` when not known; `interrupted_result`'s reason
  then ends ` (usage unknown)`.
- **One place for the minimum loop time.** `fixer.run_fix_loop` refuses (`attempted: false`, `time budget used up`)
  only when the deadline has already passed; `MIN_LOOP_SECONDS` (60) is checked only in `graph._run_fix`, which knows
  the budget. `remediation.fix_loop_max_minutes: 1` with enough time left starts the loop.
- **The budget outcome is saved like the baseline outcome.** `chain._budget_outcome(paths, state, budget, real_stop,
  jr=None, cfg=None)` (STOPPED `stop_requested` → `chain_budget` when the chain's time budget, not a person, stopped
  it) calls the new `chain._report_again(paths, state, jr, cfg)`, shared with `_baseline_outcome`: a new `updated`, the
  state saved, `graph.write_pending_report` once when `cfg` is given (a failure is a WARNING `chained remediation <id>:
  the report was not written again: …`). A STOPPED state is not `TERMINAL`, has no report node and no remediation
  report, so nothing is rewritten there.
- **A report written on a later entry drops the earlier failure note.** `graph._without_report_note(message)` cuts the
  tail ` | report not written: …` (or a message that is only that note); `_wrap("report")` applies it before the node
  runs, so a report node that succeeds on a later repair saves a message (and writes a report) without the note; a node
  that fails again restores the earlier message and `updated` (the note is still added once).
- **Bounded repair memory.** `RemediationManager._repair_seen` is an `OrderedDict` kept to the most recent
  `REPAIR_SEEN_MAX` = 2000 folders (`_remember_repair`, under `_lock`); an evicted folder is checked once more when it
  is viewed again.

### Values per case

| Case | `FixResult` / `fix_usage()` | `by_model` rows (`fix_usage_rows`) | admin run row | report |
|---|---|---|---|---|
| all known | `usage_unknown` absent | one priced row per name | `cost` = the sum, no `usage_partly_unknown` | 「…LLM을 M회 호출했습니다.」 |
| one failed call beside known ones (`llm_calls` 3, attempts recorded 2) | `fix_usage`: `calls` 3, `tokens_unknown`, `usage_unknown`, not `calls_unknown` | `(m, 2, 2.0)` and `(m, 1, null, tokens_unknown: true)` | `calls` incl. the failed call, `cost` = the known sum (test: 0.25 + 0.5 = 0.75), `usage_partly_unknown: true`, the model row's cost the known sum | 「… LLM 사용량 일부는 알 수 없습니다.」 |
| nothing known (only `tokens_unknown` rows, or an unread loop with no migration calls) | `usage_unknown` | the unknown row, `cost: null` | `cost: null`, `usage_partly_unknown: true` (page 「알 수 없음」) | as above |
| unread loop beside a priced migration | `usage_unknown`, 0 calls | — | `cost` = the migration's, `usage_partly_unknown: true` | as above |
| damaged journal of a killed loop (a cut-off or garbage line) | `usage_unknown`, the calls / tokens read (test: 1 call, 40 input tokens), reason `interrupted: the run ended during the loop (usage unknown)` | the rows read, priced | the known sum, flagged | 「LLM 호출 수는 알 수 없음」 on the record line; round total 「알려진 것만」 |
| `journal.jsonl` missing beside `loop.json` | known zero (superseded by v2.1z-bq: no journal means no LLM request was made) | — | unchanged | no unknown sentence |
| neither file (the loop never started) | known zero, reason `interrupted: the run ended during the loop` | — | unchanged | no unknown sentence |

`chain.summary.fix_usage_unknown`, `GET /api/runs/{id}` `remediation_llm_usage_unknown` and `GET /api/remediations/{id}`
`fix.llm_usage_unknown` follow `fix_usage()["usage_unknown"]` as in "v2.1z-bo"; `SBMIG-RESULT … llm=` holds the known
calls.

### Mid-node saves (changed rows of the "v2.1z-bo" table)

| Save inside a node | What is in the state | A kill right after it, then the re-entry |
|---|---|---|
| `_discard_fix(…, ends_round=True)` before `_drop_fix_files` (the fix the loop had just returned is unusable) | the fix in `fix_discarded` with `ended_round: true`, `rs.fix` None | no second loop; FAILED `candidate_build_failed` with ` | automatic build fix: discarded: the stored fix is not usable`, as the uninterrupted run |
| `_discard_fix(…)` of a stored fix found unusable on resume | the fix in `fix_discarded`, `ended_round` absent | the loop runs (as the uninterrupted resume does); the same after a stop during the rerun's candidate build |
| `chain._budget_outcome` (reason `chain_budget`, new message) | new `updated`, saved | STOPPED: nothing pending; the next entry resumes |

### Messages

- Report (`report._fix_calls_sentence`): with other loops in the round and part of the usage unknown 「- 빌드와 수정을
  N회 반복했고, LLM을 M회 호출했습니다(버린 시도까지 알려진 것만 T회). LLM 사용량 일부는 알 수 없습니다.」; all known
  「…(버린 시도까지 모두 T회).」.
- State message / report after a repair: no ` | report not written: …` tail once the report is written.
- Admin page: 「일부 알 수 없음」 beside the call count and beside a known cost.

### Names

`FixResult.ended_round`; `graph._discard_fix(…, ends_round=…)`, `graph._without_report_note`;
`chain._report_again`, `chain._budget_outcome(…, jr, cfg)`; `fixer.journal_usage` → `(rows, currency, known)`,
`fixer._LOOP_FILE`; `admin.journal_usage_checked(run_dir, strict)`; `RemediationManager.REPAIR_SEEN_MAX`,
`_remember_repair`.

**Tests.** `tests/remediations/test_fix_loop_review6.py` (a resume with an unusable stored fix runs the loop and the
discard has no `ended_round`; a kill after that discard and a stop during the rerun's build both run the loop on the
next entry; the fresh unusable discard is marked in the same save; `ended_round` left out when false, old JSON false;
the budget outcome stamped, saved, report asked once, no remediation report for STOPPED; one failed call → own unpriced
row, known row keeps its price; admin rows: all known, one failed call beside known ones, nothing known, an unread loop
beside a priced migration; the note beside a known cost in `admin.html`; damaged, garbage, missing journal → unknown;
neither file → known zero; blank lines are fine; the fixer starts with a few ms under 60 s left; `fix_loop_max_minutes:
1` starts the loop; 「알려진 것만」; the note dropped on repair, also from a message prefixed later; the repair memory
bounded at 2000), adjusted `test_fix_loop_review5.py`, `test_fixer.py`. 7,203 passed.

**Real-Maven passes** (Maven 3.9.10 + JDK 21 on a developer Mac, fake scanner, scripted LLM; a one-module project on
`org.yaml:snakeyaml` 1.33 → 2.0 with a stale `target/` and an MS949 source):
- Third pass, on `4600c02`: the 15 scenarios of the second pass again, delivered ZIPs identical per file, with the
  differences `4600c02` explains (no LLM ask after a stop: 0 instead of 8; the STOPPED wording; the round total; the
  interrupted loop's `tree/` removed; 「검사를 마친」). New: a kill right after the no-change delivery was installed →
  exactly one upload over run and resume (`baseline_scan_reused`); the loop's time budget at 2 minutes with slow answers
  → no ask after the deadline, FAILED `candidate_build_failed`, sources identical to the upload; a killed loop whose
  journal was made unreadable → usage unknown. It found the strict-reading and one-minimum defects above. Also a real
  migration run (fixture `pure-java`, its replay LLM, real Maven): a normal run ended `exit=0 build=ok` with the verify
  fix loop running one round; a stop between two LLM asks ended `exit=3` (aborted) with `llm_ask_skipped`, no further
  ask, a clean workspace and nothing half-applied.
- Fourth pass, on `d85f7a4`: 17 scenarios (happy, wrong, pom, noloop, kill + resume, stop + resume, notarget,
  nofinding, exec2, buildwrite, killloop, stoploop, reactor, nofinding-kill, killloop with a garbage journal, killloop
  with a deleted journal, time budget at the 1-minute minimum) ended in the expected state and passed their file
  checklists, except two false alarms of the checklist itself (a build duration that differs by seconds between two
  reports; the loop's own journal labels a time-out as a stop request while the state and the report say time budget).
  With `fix_loop_max_minutes: 1` the loop starts (2 LLM calls, reason `time budget used up`); a garbage or deleted
  journal of a killed loop is recorded as `interrupted: the run ended during the loop (usage unknown)` and the report
  says 「알려진 것만」.
- Not exercised by any pass: the company cluster, a real LLM, real IVAS, large projects and real build times, Windows,
  the web service's repair and Job mode, the admin page, the migration loop's time-budget check before an LLM request.

**Still open.** (The first two items of this list at the time of writing — no "partly unknown" note on the admin
page's project and overall cost; a loop killed before its first journal line read as unknown — are closed by
"v2.1z-bq".) Rows that are known but have no configured price still make a run's cost `null`; the loop's own journal labels a
time-out as a stop request (the state and the report are right). The items still open after "v2.1z-bo" stay open. The
verification review of `d85f7a4` ended "safe to push for a non-production deploy: yes" (see "v2.1z-bq").

## v2.1z-bq — verification of v2.1z-bp: a loop killed during its setup is a known zero; the note reaches the admin totals

**Why.** The verification review of `d85f7a4` found no defect that blocks a push ("safe to push for a non-production
deploy: yes") and nothing a healthy run does differently from its parent. It named two small regressions of that
commit, visible only when a pod was killed or usage could not be read, and they are fixed here (commit `020b11f`):
(1) the graph writes `fixloop/loop.json` before it calls the fixer, and the fixer copies the tree and runs `git init`
(seconds on a large project) before its first journal line, so a pod killed in that window left `loop.json` without
a journal and was reported as "usage unknown" although no LLM request can have been made; (2) a run with partly
unknown usage had a numeric cost (the priced part) that was summed into the admin page's project cost, overall
total and model cost without any note.

**Rules, as coded.**
- `fixer.journal_usage`: a folder without `journal.jsonl` is `([], "", True)` — known, zero — with or without
  `loop.json`. This rests on an order that must hold: `run_fix_loop` writes its first journal line
  (`remediation_fix_loop_start`) before `BuildFixLoop.run`, the only place an LLM request is made. A journal that
  exists but cannot be opened, cannot be read to the end, or has a line that cannot be parsed is still unknown, with
  what was read kept.
- `admin.add_remediation_usage`: a model row that receives calls without token counts gets
  `usage_partly_unknown: true`. `admin.collect_usage`: a project row, `totals` and a `by_model` row that sum a run
  with partly unknown usage carry `usage_partly_unknown: true` (left out when false; for a model row only when that
  run's row of the model received such calls); their numbers, the cost included, are the known part.
  `admin.html`: `sumCallsCell`, `costCell` and the two tiles show the existing 「일부 알 수 없음」 note; when the
  overall cost is null because of unknown usage the note line reads 「사용량 일부를 알 수 없는 실행이 있어 전체
  비용은 표시하지 않습니다.」. A healthy page renders byte for byte as before (checked by rendering both versions).
- The third finding of that review is left as it is: a `state.json` written by an undeployed commit of 2026-11-20
  (a discard with `R_FIX_UNUSABLE` and no `ended_round`) can run the loop once more on resume.

**Tests.** `tests/remediations/test_fix_loop_review6.py` (`test_a_missing_journal_beside_loop_json_is_known_zero`,
`test_a_kill_during_the_loop_setup_is_a_known_zero_at_graph_level`), `tests/web/test_admin_partly_unknown.py`
(the JSON in four cases, the page wiring, two node renders). Full suite: 7,211 passed. The coordinator read the
diff of this commit instead of running another review round.

**Real Maven, fourth pass (on `d85f7a4`).** 17 scenarios with Maven 3.9.10 and JDK 21, fake scanner and scripted LLM:
all ended in the expected state and passed their file checklists, apart from two false alarms of the checklist
itself (a build duration that differs by seconds between two reports; the loop's own journal labelling a time-out as
a stop request). With `fix_loop_max_minutes: 1` the loop starts; a garbage journal of a killed loop is recorded as
usage unknown and the report says 「알려진 것만」. (The "deleted journal" scenario of that pass showed "unknown";
after this section's first rule it is a known zero.)


## v2.1z-br — the safe sweep: three small defects, dead code, repeated reads, what enters the image

**Why.** A read-only review of the whole tool listed a few small defects that are older than the remediation work
and a set of changes that alter nothing a run, a report or a reader depends on. This section is the list a reviewer
checks the round against: whatever a commit of this round changes beyond it is a defect of the round. Existing
tests stay as they are; an assertion is changed only where an item of "Changes with a visible effect" says so, and
a test is removed only when it covered nothing but removed code.

**Changes with a visible effect.**
1. `GET /api/runs` never answers 500 because of one run. A `report.json` that is valid JSON but not an object
   (`[]`, `null`, a number, a string) is treated exactly like one that cannot be read or parsed: the row is listed
   from the run's other files, without the fields that come from the report. The same holds for the single-run
   status. A report field that should be a list and is not (`blocked`, `unresolved`, `error_entries`,
   `manual_followups`, `flags`) counts as absent, and a journal line that is JSON but not an object is skipped
   like a line that does not parse. As the last net, an unexpected error while one row of the list is built is
   logged as one warning with the run id and the exception type, and that run is listed like a run whose files
   cannot be read: the other rows are not affected. The net holds for a folder the server cannot enter as well
   (its state is then `unknown`); an entry of the output folder that cannot even be examined (a stale handle) is
   left out of the list. The single-run status has no net: an unexpected error there stays visible. Nothing else
   about a row changes.
2. The framework guard of a reduced run (`BuildFixLoop._framework_guard`) fails closed. The documented case stays:
   a pom that cannot be read (an `OSError` from reading it) or does not parse is left to the build (handled where
   the pom is read, answer "" for that pom, before the attempt and after it). Any other exception
   inside the check rejects the attempt like a guard finding: the sentence names that the check could not be
   completed (no exception text of the project in it), the working tree is restored by the caller as for every
   guard finding, and one warning line is logged with the exception type. sbmig's own pre-tries and full-scope runs
   are still not checked.
3. A `TypeError` raised while a build is running is no longer answered by starting the build a second time. The
   three call sites that retried a call without a keyword after `TypeError` decide from the callee's signature
   before the call, once; an exception from the build itself travels as any other exception does today.
   `sbmig/loop/callee.py::takes_keyword` is the rule: a callee that takes `**kwargs` counts as accepting the
   keyword, an override that only forwards `**kwargs` to a base method of the same name is decided by the base,
   and a signature that cannot be read counts as accepting. Every builder and runner the tool ships gets the call
   it got before. A component injected through `sbmig.api.migrate(components=…)` that hides a runner without the
   keyword behind `**kwargs` now sees the `TypeError` instead of a second call.
4. The inline part of the page's report view (`MdView`) takes time linear in the length of a line. The rendered
   nodes for every report the tool writes are unchanged. A crafted line of about 195 KB that took a minute is
   rendered in well under ten seconds (the test's bound; the expected time is milliseconds). One family is not
   linear, as before: labels of images or links nested inside each other cost the nesting depth times the length
   (a few seconds at 2,500 levels), and past about 3,000 levels the browser's stack ends the rendering of that
   report, also as before. No report the tool writes nests them.
5. The run list's 15-second refresh does not fetch while the tab is hidden (`document.hidden`). When the tab
   becomes visible one refresh runs at once and the interval goes on. The stream of an open run is not touched.
6. The status JSON no longer carries `run_dir`: no page reads it. Server-side code that needs the folder keeps
   using the run manager. `out_dir` in `GET /api/configs` stays: the page reads it.
7. Fewer files enter the image build context: `agent-doc/`, `CLAUDE.md`, `scripts/handbook/`, test and tool caches
   (`__pycache__` at any depth, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, coverage files), `out/`. The same
   holds for the layout where the tool sits under `src/migration-agent` and `deploy/` at the repository root:
   `deploy/docker/root.dockerignore.example` carries the same exclusions at any depth, and a repository that
   copied an older example keeps building as before until it copies the new one. The Dockerfile is not changed:
   its two `COPY` lines for `deploy/` serve the two layouts. Every path the Dockerfile copies is still present
   after either ignore file (a test reads the files and checks it). The files under the application folder of the
   image are the same as before except those named here.

**Changes without a visible effect.**
- Removed from `sbmig/remediations/`: the flag `rerun_ok` (it decided nothing), `_Holder.nothing_changed`,
  `intake.restore_missing`, `models.versions_of`, `planner._root_entries`, the constants `SUMMARY_CHECKS`,
  `STATUSES`, `AUTO_ORIGINS`, `_DECISION_KO`, `UNCONFIRMED_STATUS_KO`, the `sync_tree` fallback and the branch of
  `_fix_tree_hash` for a tree without a table. Each one is removed only after a search of `sbmig/`, `tests/`,
  `scripts/` and `deploy/` shows no reader other than tests of the removed code itself; one that has a reader stays.
- `openrewrite.fail_on_dry_run_results` leaves the config model (no code reads it). A config file that still sets
  it loads with every other value applied, and the loader names it among the removed keys with its usual sentence
  for a removed key instead of the warning for a misspelled one.
- `fixer.compare_trees` checks `by_attempt` before it reads file bytes: a path no attempt changed is not read at
  all. The result is the same for every tree the graph hands it; where such a path could not be read, or was
  absent from the original tree, the comparison raised before and now goes on. `intake.freeze_delivery` hashes
  each file while it is written into the ZIP instead of hashing the tree again afterwards: the ZIP bytes, its
  sha256 and its size are identical, and `tree_hash` is the hash of the bytes that went into the ZIP (it differed
  from that only when a file changed while the ZIP was being written).
- The loop reads "tests were skipped" from the typed state instead of a substring of a note.
- `RunManager.status` reads `control/request.json` and `web-status.json` once per call.
- Maps that only grow (`RunManager._fw_scans`, the admin page's `_failures`) drop entries that are expired or can
  be computed again. `resumes` stays: the answer for a finished resume carries fields its file does not hold.
  `_run_locks` is not touched: dropping a lock that is in use is a defect and the map costs nothing.
- `config/default.yml` lists every key of the config model that a person may set, at the model's default value,
  with a Korean comment for the person deciding the value; a test compares the file with the model defaults.
  `deploy/ENV.md` names every environment variable the manifests set.

**Left out on purpose.** A hard link for the delivered original (two trees would share file contents; it belongs
with the measurements of disk use on the cluster). The pip layer of the image (the build at the company cannot be
tried here). The KB credit read from note text (it needs a typed failure, a structural change). Bounding
`_run_locks`.

**Pinned, not changed.** One test records what the remediation does today when the raised version cannot be
resolved and the candidate build fails before compilation: whether the fix loop is called and how many LLM
requests it makes. It changes no behaviour; the numbers decide a later round.

## v2.1z-bs — answers that carry a file's content never fail on a non-finite number; `intake.sync_tree` removed

**Why.** Two small leftovers of v2.1z-br. A third item was implemented and taken out again in review; what the
review found is kept below because it is the specification of a later round.

**Rules.**
1. `GET /api/runs/{id}` does not answer 500 for a `report.json` that holds a non-finite number (`NaN`,
   `Infinity`, `-Infinity`): it is sent as `null`, as the run list already sends it. The same holds for the other
   answers that carry a file's content: the answer of `POST /api/runs`, the code-change routes, the admin page's
   usage answer (a non-finite cost in one report made it fail), and the `status` event of the run's event stream
   (it sent the bare word `NaN`, which a browser cannot parse). `sbmig/web/jsonsafe.py` is the one place. For a
   payload without such a value the bytes sent are the ones sent before, and no extra pass over the payload is
   made.
2. `RunManager.status` reads `report.json` once per call.
3. `intake.sync_tree` is removed with its entry in the interface, its fake and the tests that covered only it:
   nothing calls it. `sync_tree_table` is unchanged, and what those tests proved about the code the two shared
   (a path the table does not list: sizes compared before bytes, an absent expected file left alone, the exec bit
   taken from the expected file, a second call changing nothing, no temporary file left) is tested on
   `sync_tree_table`.

**Not changed: the fix loop still starts for a failure only a pom can fix.** The pin of v2.1z-br stays
(`tests/remediations/test_v21zbr_pins.py`: the loop is started once, 2 LLM requests, 3 builds, FAILED
`candidate_build_failed`). A first version of this round skipped the loop whenever every parsed error of the failed
candidate build had one of the kinds `dependency_resolution`, `parent_resolution`, `plugin_resolution`,
`pom_invalid`. A review reproduced two defects of that rule, and it was withdrawn:
- The loop's first step is to build again, and a build that passes then ends the round as fixed without an LLM
  request (`retry_passed`). A repository that fails for a moment (a 502 from a proxy, a connect timeout, any
  "Could not transfer artifact") is parsed as `dependency_resolution`, so the skip turned a run that passes on the
  second build into a failed one.
- The recorded decision survived a resume although the resume builds the candidate again: the report then
  explained the failure of one build with the errors of another.
A later round that wants to save those requests has to keep three things true: the candidate is built once more
without an LLM request before giving up, and a pass ends the round as `retry_passed` does today; the decision is
taken from the log of that second build and only when every parsed error is of the four kinds; no decision is
carried across a rebuild.

## v2.1z-bt — result ZIPs are received under the project name

**Why.** The user, 2026-11-23: "변환된 프로젝트 받기랑 결과 ZIP 받기도 프로젝트 명으로 다운 받을 수 있도록 하자! 둘다
result.zip은 좀 아닌거 같아! 프로젝트명-migration.zip, 프로젝트명-remediation.zip으로!" Both buttons handed out a file
called `result.zip`, so two downloads could not be told apart in a Downloads folder.

**Rules.**
1. One rule, `sbmig.web.contract.zip_download_name(run_id, kind, project=None)` (no I/O): `<project>-migration.zip`
   for the migration's `result.zip`, `<project>-remediation.zip` for the remediation's `delivery/result.zip`.
   `project` is the name entered at upload. When it is missing or not a safe file-name part
   (`^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$`), the run id stands in (a CLI run, a run uploaded before v2.1z-be, a
   standalone remediation: its own id; a chained or remediation-flow remediation: the migration run id). When neither
   fits, `result.zip`. No slug is cut out of the run id.
2. `GET /api/runs/{id}/files/result.zip` sends `Content-Disposition: attachment; filename="<project>-migration.zip"`.
   The project is the one the run's status reports (in memory, else `control/request.json`, `web-status.json`, the
   journal's `web_run` line). Every other file of that route keeps its name (`report.md` / `report.json` as
   `migration-report.*`, the rest unchanged).
3. `GET /api/remediations/{id}/files/result.zip` sends `filename="<project>-remediation.zip"`. For a chained or
   remediation-flow run the project is `contract.entered_project(<migration run dir>)`; a standalone remediation has
   none and uses its id. `result-verified.zip`, the reports and every other file keep their names.
4. The statuses carry the name so the page shows exactly what the browser will save: `GET /api/runs/{id}` (and every
   status the run list and the event stream send) gets `zip_name` (the migration name, present whether or not the ZIP
   exists yet); `GET /api/remediations/{id}` gets `zip_name` (the remediation name). The migration status computes it from the project it
   already has, so the run list reads no extra file; the remediation status of a chained run reads the migration run's
   `control/request.json` (and, in-process, the journal's first lines) once per call, a failed read leaving the run id.
   The list rows carry the migration name only.
5. Page: the migration button 「변환된 프로젝트 받기」 shows `zip_name` as its small file name and uses it as the
   `download` attribute; the remediation button 「결과 ZIP 받기」 does the same with the remediation status's
   `zip_name`. A status without the key falls back to `result.zip`. The code-change view's sentence that pointed at
   「변환된 프로젝트(result.zip)」 names the ZIP without the old file name.
6. The remediation report no longer tells the reader to take `delivery/result.zip` (a path inside the run folder the
   reader never sees): the steps read 「**결과 ZIP 받기** — 재검사를 마친 결과 ZIP을 받아 사용해 주세요.」, round 0
   「검사를 마친 결과 ZIP」, and the tolerated-build step 「결과 ZIP의 pom.xml 버전을 그대로 두고」; the summary's delivery line says 「결과 파일: 결과 ZIP — …」 and the
   failed-delivery warning 「결과 ZIP에는 …」. The Korean of the status messages a person sees on the page and in the report's 「상세:」 line
   (`sbmig/remediations/i18n.py`: a failed candidate delivered anyway, a discarded round, a delivery that could not be
   written or recorded) says 「결과 ZIP」 without the parenthetical `(delivery/result.zip)`. The collapsed processing
   record keeps `delivery/result.zip`, and the Korean migration report's collapsed 「참고」 block keeps `result.zip`
   (both name the files in the run folder for operators).

**Not changed.** Files in the run folder keep their names (`result.zip`, `delivery/result.zip`): the web, the admin
page, the code-change view, cleanup and older runs read them. The admin page's log ZIPs. `result-verified.zip`.

## v2.1z-bu — Jackson 3 WebFlux codecs in the fix notes; does the conversion leave a WebFlux module without Jackson 3?

**Why.** A company run (2026-11-23) ended with a WebFlux module unfixed: the fix loop alternated between error sets
and was blocked as `oscillation`. Read from the run: the module's code used Jackson 3 (`tools.jackson`) while its
codec configuration still built `Jackson2JsonDecoder` / `Jackson2JsonEncoder`
(`tools.jackson.databind.ObjectMapper cannot be converted to com.fasterxml.jackson.databind.ObjectMapper` in builds 5
and 8); build 4 showed `package tools.jackson.databind does not exist` (and `tools.jackson.databind.json`,
`tools.jackson.core.type`) after an attempt that edited a pom and was scored `improved`. No attempt was reverted.
`knowledge/` holds no rule for the Spring 7 Jackson 3 codecs (the note `jackson-2-3.md` covers the MVC converters
only), and the recipes (rewrite-spring 6.37.0) do not migrate Spring's codec classes.

**Facts (checked on spring-web 7.0.10 and spring-boot-jackson 4.1.0 with `javap`, and the Boot 4.1.0 starter pom).**
- `org.springframework.http.codec.json.JacksonJsonDecoder` / `JacksonJsonEncoder` (Jackson 3) have constructors
  `()`, `(JsonMapper)`, `(JsonMapper.Builder)`, each also with `MimeType...`; the type is
  `tools.jackson.databind.json.JsonMapper`, not `tools.jackson.databind.ObjectMapper`.
- `Jackson2JsonDecoder` / `Jackson2JsonEncoder` remain (deprecated) and take `com.fasterxml.jackson.databind.ObjectMapper`.
- `CodecConfigurer.DefaultCodecs.jacksonJsonDecoder(Decoder<?>)` / `jacksonJsonEncoder(Encoder<?>)` are the
  registration methods; the deprecated `jackson2JsonDecoder` / `jackson2JsonEncoder` still compile and call them.
- Smile / CBOR twins: `org.springframework.http.codec.smile.JacksonSmileDecoder` / `JacksonSmileEncoder`,
  `org.springframework.http.codec.cbor.JacksonCborDecoder` / `JacksonCborEncoder`.
- Boot 4.1 auto-configures a `JsonMapper` bean (`JacksonAutoConfiguration.jacksonJsonMapper`);
  `spring-boot-starter-webflux` 4.1.0 depends on `spring-boot-starter-jackson` (Jackson 3).

**Rules.**
1. The note `knowledge/notes/jackson-2-3.md`, Rule A ("the module is on Jackson 3: finish the move"), gains the
   WebFlux codecs: `Jackson2JsonDecoder` → `JacksonJsonDecoder`, `Jackson2JsonEncoder` → `JacksonJsonEncoder` (Smile,
   CBOR alike); the codec takes `JsonMapper`, so the mapper handed to it is declared / injected as `JsonMapper`
   (Boot provides that bean) or built with `JsonMapper.builder()…build()`; register with
   `defaultCodecs().jacksonJsonDecoder(…)` / `jacksonJsonEncoder(…)`. The error
   `tools.jackson.databind.ObjectMapper cannot be converted to com.fasterxml.jackson.databind.ObjectMapper` at a codec
   is this case: never move the module, the file or the mapper back to Jackson 2 for it, and never remove a Jackson 3
   dependency from a pom to make it compile. The note's keywords (front-matter and `knowledge/notes/index.yml`, kept
   in sync) select it for the codec class names and the `jackson2Json…` registration names.
2. A local reproduction with real Maven answers the open question: does the conversion (OpenRewrite + restructure,
   before any fix attempt) leave a WebFlux module whose code imports `tools.jackson` without Jackson 3 on its
   classpath? Shapes: a Boot 2 multi-module project whose WebFlux module uses `spring-boot-starter-webflux`, one that
   uses `spring-webflux` with an explicit `jackson-databind`, and one where Jackson 2 arrives only transitively. The
   answer decides whether a conversion change is needed; it is recorded below when known.

**Found later the same day (company run).** Build 6 failed with `cannot find symbol` / `class JacksonJsonDecoder` /
`location: package org.springframework.http.codec.json` in the module's WebClient configuration: the LLM had moved to
the Jackson 3 codec, and the class does not exist on that module's classpath. spring-web 6.1.21 and 6.2.12 have no
`JacksonJsonDecoder`, 7.0.8 / 7.0.10 have it (checked on the jars), so that module's spring-web was older than 7.0
after the conversion: an as-is pin kept Spring below the target BOM there while the recipe had moved the code to
Jackson 3. The oscillation was the LLM going between the two codec classes, neither of which can work in that state.
Rule 1 therefore also tells the LLM that this `cannot find symbol` means an older spring-web and that the fix is to
remove the pin (the BOM manages Spring), never to go back to Jackson 2. Which pom construct kept the old version (an
explicit version, a property, an imported BOM, a parent the conversion did not replace) is asked of the user and
tested in the reproduction (Rule 2).

**Reproduction result (Rule 2, real Maven 3.9.10, JDK 21, local-e2e stand-ins; `out/scratch/webflux-jackson/`).**
Boot 2.7.18 reactors with a WebFlux module holding the codec configuration, a WebClient factory and a class using
`ObjectMapper` / `JsonMapper` / `TypeReference`. The recipe (`UpgradeJackson_2_3` under `UpgradeSpringFramework_7_0`,
rewrite-jackson 1.28.4) moves the imports to `tools.jackson.*` in every shape and never touches Spring's codec classes.
- Jackson 3 is on the module's classpath after the conversion when the module declares `spring-boot-starter-webflux`,
  a `jackson-databind` (with or without an explicit version or a version property: the recipe changes the groupId and
  drops the version), or gets `spring-boot-starter-json` through another module or the root pom. The first build then
  shows only the codec mismatch (`tools.jackson.databind.ObjectMapper cannot be converted to
  com.fasterxml.jackson.databind.ObjectMapper`).
- Jackson 3 is missing when the module declares no Jackson artifact and no Boot starter and gets Jackson 2 only
  through a non-Jackson library (`jjwt-jackson` stood in): `UpgradeJackson_2_3_Dependencies` rewrites only declared
  Jackson artifacts. The first build then shows `package tools.jackson.databind does not exist` (and
  `tools.jackson.core.type`, `tools.jackson.databind.json`) at the same positions as the company's build 4. Adding a
  version-less `tools.jackson.core:jackson-databind` to that module turns it into the codec mismatch. sbmig's
  restructure neither causes nor repairs this.
- spring-web on the module was 7.0.x in every shape, so `JacksonJsonDecoder` resolves there; the company's older
  spring-web comes from something the stand-ins do not have (asked of the user).
Not changed in this round: the conversion. A deterministic repair of the transitive-Jackson shape (add
`tools.jackson.core:jackson-databind`, version-less, to a module whose sources the recipe moved to `tools.jackson`
when it has no Jackson 3 of its own) is a candidate for a later round; in the company run the LLM's attempt added
the dependency in one try, so this shape was not what blocked the run.

## v2.1z-bv — Jackson 3 exceptions in catch clauses (fix note)

**Why.** The user's local rerun of the same company project (2026-11-23, `2a13d5c`) was blocked as `oscillation` on
exceptions: the LLM's analysis named `tools.jackson.core.JacksonException` and its subclass `DatabindException` as
runtime exceptions caught after an earlier `catch (RuntimeException …)`. The note's Rule A said only "unchecked: drop
a `throws` / `catch` that only existed for it", which does not say what to do when a broader catch comes first, and
"drop" loses the specific handling.

**Facts (checked with `javap` / `javac` on jackson-core / jackson-databind 3.1.8, JDK 21).**
- `tools.jackson.core.JacksonException extends RuntimeException`; `tools.jackson.databind.DatabindException extends
  JacksonException`.
- `ObjectMapper.readValue(String, Class)` and `writeValueAsString(Object)` declare `throws JacksonException` only.
- A `catch (JacksonException e)` after `catch (RuntimeException e)` in the same try: `exception JacksonException has
  already been caught`. A `catch (java.io.IOException e)` whose try only calls Jackson 3: `exception IOException is
  never thrown in body of corresponding try statement`.

**Rule.** The note's Rule A gains: on `has already been caught` for a Jackson exception, move that catch block,
unchanged, above the broader `catch (RuntimeException …)` / `catch (Exception …)` of the same try (keeps the
specific handling; delete neither block; never a multi-catch with `RuntimeException` / `Exception`, which does not
compile). On `IOException is never thrown` where the try's only I/O source was a Jackson call: change that catch's
type to `JacksonException` and keep its body; when the same try already catches `JacksonException`, remove the dead
`IOException` block. Keywords select the note for these two messages when they name a Jackson exception or come with
Jackson imports.

**v2.1z-bu, later the same day.** The user's local rerun of the same project on `2a13d5c` got past the WebFlux module
(the next block was v2.1z-bv's catch clauses). Between the cluster run's `ed0ed61` and `2a13d5c` the conversion code is
unchanged (only the result ZIP names and the note), so the spring-web below 7.0 in the cluster run most likely came
from one of that run's fix attempts that edited the module's pom, not from the conversion. Not confirmed (the PC and
the cluster use different settings); no conversion change is planned for it.

## v2.1z-bw — Jackson 2 → 3 in the fix notes: one direction, which rule applies, Jackson's own API, Redis, Kafka and the Spring twins

**Why.** The two oscillations of v2.1z-bu / v2.1z-bv came from the note: Rule A ("the module is on Jackson 3") and
Rule B ("a signature naming `com.fasterxml.jackson.databind.*`") both matched one file, or no rule matched. A survey
of what else the recipes leave (evidence under `out/scratch/jackson-libs/`, git-ignored: jars, `javap`, the recipe
yml, `javac` output of a real `UpgradeJackson_2_3` run on Jackson 3.1.4 / Boot 4.1.0) found the same shape in
Jackson's own API and across the Spring portfolio. Measured with the real selector on the 141 compiler errors of that
survey: the note `jackson-2-3.md` was already selected for 113 of them, so the gap was what the note says, not when
it is selected. The user: Kafka and Redis are used a lot in the company projects, and a run of 2026-11-23 oscillated
on Redis too; the other libraries have not been seen yet.

**Which rule applies (note `jackson-2-3.md`, system prompt rule 8).** Three things hold under every rule, and they
are what makes the choice converge: code on `tools.jackson` is never moved back to `com.fasterxml.jackson` (one
direction only); a missing line is added as a dependency, never avoided by moving code; a type written fully
qualified as `com.fasterxml.jackson.…` in a file on `tools.jackson` marks the boundary to a Jackson 2 only library
and stays. The rule is chosen by the class the failing call talks to, never by the message alone:
0. no file and no pom of the module uses `tools.jackson`: Rule C, whatever the class;
1. a Spring Framework / Spring Boot / Spring portfolio class (`org.springframework.*`): Rule A. The modules the
   notes name each have a Jackson 3 counterpart on the target line; a Spring class no note names (Spring Cloud was
   not surveyed) is judged like another library (3);
2. Jackson 3's own API (a call on a `tools.jackson` object): Rule A;
3. another library: Rule A when a class of it that takes Jackson 3 types is on the classpath (a note, the error's
   candidate list or the class name shows it); otherwise Rule B. The library's artifact is never swapped for another
   to get a Jackson 3 class.
Rule B adds Jackson 2 databind and never moves a file back: a file the recipe left on Jackson 2 stays as it is; a
file on Jackson 3 keeps its imports and names the Jackson 2 types the library takes, returns or declares fully
qualified; a call on such a Jackson 2 object follows Jackson 2 (its checked exceptions are caught or declared, Rule A
does not apply to it); a variable of the file that a Jackson 3 call also uses is never retyped and nothing is cast
between the lines: the library gets its own Jackson 2 object and trees cross as text; a mapper the library itself
hands out stays, together with the Spring Jackson 2 class it feeds (the one exception to "use the counterpart").
Rule C brings the Jackson 2 module artifacts its files import. `package tools.jackson.… does not exist` /
`class file for tools.jackson.… not found` means Jackson 3 is missing from the module (the shape of v2.1z-bu): the
Jackson 3 databind artifact is added.
`cannot access ObjectMapper … class file for com.fasterxml.jackson.databind.ObjectMapper not found` means "a class
compiled against Jackson 2 is used and Jackson 2 databind is not on the classpath": the note no longer reads it as
"add Jackson 2", it sends the reader through the choice first. Several of these messages name no Spring class
(`cannot access ObjectMapper`, `cannot access JavaType`, `cannot access TypeReference`, `… cannot be converted to
com.fasterxml.jackson.databind.ObjectMapper`), so only `jackson-2-3.md` reaches their prompt: its Rule A therefore
carries the answers for the modules the company uses (MVC converters, WebFlux codecs, Spring Data Redis, Spring
Kafka) itself. The text of v2.1z-bu (WebFlux codecs) and v2.1z-bv (catch clauses) stays as it is: a company rerun
passed with it.

**New notes.** Both are ordinary keyword-selected notes (`cases: []`, `phases: [spring]`, at most 160 lines, index
entry and front matter in sync). Every class, method and message in them comes from the evidence folder (a `javap`
listing, a snippet that compiles, a stored `javac` message); what could not be confirmed there is left out.
- `jackson-3-api.md`: what the recipe leaves in code that uses Jackson itself. The mapper is immutable (setters and
  `registerModule` on an existing mapper are gone: `rebuild()` / `JsonMapper.builder()`), features that moved or
  were removed, the modules that are built into databind 3 (`JavaTimeModule`, `Jdk8Module`,
  `ParameterNamesModule`: never re-add the Jackson 2 artifacts), custom serializers and deserializers (`throws
  IOException` on overrides and on the helpers they call, the `SerializationContext` and generator renames,
  `ObjectCodec`), default typing (`ObjectMapper.DefaultTyping`, the validator that is no longer public), and the
  rewrites the recipe itself gets wrong (a method or type name that does not exist in 3.1).
- `jackson-3-spring.md`: the Jackson 3 counterparts in the Spring portfolio. Spring Data Redis (the recipe renames
  none of its classes: both serializers, the builder, the missing setter and no-arg constructor, default typing) and
  Spring Kafka (the serializer, deserializer and serde classes the recipe renames, and what it leaves: the mapper
  helper, the type mapper interface, the mapper type the constructors take, the `cannot infer type arguments for
  …<>` message) in full, then one line per other module that has a counterpart. Boot's own support types stay in `boot4-packages.md`, Spring Security's modules in
  `spring-security-7.md`, MVC converters and WebFlux codecs in `jackson-2-3.md`.

**Selection.** At most four notes reach a prompt, ranked by the number of the note's keywords that match (three
points in the error text, one in the imports / pom), and a note is left out whole from the lowest rank when the
prompt is over its budget. So `jackson-2-3.md` keeps its size (it is the first-ranked note for most Jackson errors and
carries the rule), and each new note has keywords for the symbols of its own errors.

**Review.** Three independent reviews against the jars, with their own compiles (JDK 21) and, for the API note,
runs on Jackson 2.21.4 and 3.1.4. Fixed from them:
- replacements that compiled and changed the output: property inclusion needs value and content inclusion together;
  `AUTO_DETECT_CREATORS` also needs the scalar-constructor visibility; `writePOJOProperty` on a generator made from a
  bare factory throws; a `rebuild()` copy does not configure the shared Spring bean; a Jackson exception is wrapped
  so that callers still catch the same type;
- loops the rule text itself produced, each reproduced with a stand-in jar compiled against Jackson 2: "once per
  file" with "keep that file on Jackson 2" on a file that also uses a Spring Jackson 3 class; a file moved back to
  Jackson 2 whose next error (a checked Jackson 2 exception) was then sent to Rule A; one declaration feeding a
  Jackson 3 class and a Jackson 2 only library. They are why Rule B no longer moves a file back at all;
- dead ends: no text added Jackson 3 to a module that lacks it; a library with a separate Jackson 3 artifact had no
  rule; Rule C had no place in the choice; Kafka's and Redis' Jackson 2 classes give messages that name no Spring
  class, so their answers were not in the prompt;
- facts: the Kafka type mapper interface the recipe does not rename; Kafka's Jackson 2 serializers do not compile
  without Jackson 2 for their common constructors; AMQP's `JacksonUtils` has no counterpart;
- keywords that matched other libraries' output (`getFactory(`, `getProcessor(`, an unqualified `JacksonUtils`, any
  `Jackson…<>` diamond);
- from the third round, after the rule was rebuilt around the two invariants (its Redis, Kafka, in-house jar,
  missing Jackson 3 and Rule C walks all reached a clean compile): fixes that compiled and were wrong at run time.
  Default typing must keep every argument (without `JsonTypeInfo.As.PROPERTY` the stored JSON no longer reads);
  `new GenericJackson2JsonRedisSerializer()` becomes `RedisSerializer.json()`, never a bare builder (it reads stored
  entries as maps); "delete `JavaTimeModule`" applies to Jackson 3 code only, a file that stays on Jackson 2 gets the
  Jackson 2 module artifact; a mapper that an in-house factory hands out is not replaced;
- from the fourth round, a check of those corrections (four of six held; the Redis walk still reached a clean
  compile): rule 8 said "that dependency only" for Rule C; with the Jackson 3 Redis class already in place nothing
  said to put the Jackson 2 class back for an in-house mapper; a Jackson 3 call that receives an argument fixed to a
  Jackson 2 type was sent to Rule A, where reverting the type made its own error disappear. Hence the third
  invariant: a type written fully qualified as `com.fasterxml.jackson.…` in a file on `tools.jackson` stays. These
  last three corrections were checked by compile states the reviewers had produced, not by a further review round.

**Not covered.** A project class that extends a Jackson 2 base class of an in-house jar and is registered in a
Jackson 3 module has no sentence (it needs a Jackson 3 twin of that class). Several of these situations share one
error case id although they need different rules (`cannot access ObjectMapper` for Redis, Kafka and an in-house
factory; both directions of the `ObjectMapper` conversion message; `cannot access JavaType` with Jackson 2 missing
and with Jackson 3 missing; `JavaTimeModule` on either line), so a stored
script of one can be reused for another: the case identity rules are not changed here.

**Not changed.** No code: selection, scoring, the fix loop and the recipes are as before. No deterministic repair of
the recipe's wrong rewrites and no change to what the prompt says about another case's attempt (both reviewed on
2026-11-24, not built). Third-party libraries beyond the examples of rule 3 have no section.

**Not checked.** A real LLM with the new notes; how any of this behaves at run time; the wording of these errors
inside Maven's output (the survey used `javac` directly); the full recipe chain of a run (the survey ran
`UpgradeJackson_2_3` alone).

## v2.1z-bx — Boot 4 renamed the Redis auto-configuration classes (fix note)

**Why.** The user read out the Redis oscillation of the run of 2026-11-23: the LLM's analysis said that
`RedisConfig` used `RedisConnectionDetails` "under the name moved to the Boot 4 location" and that the type was not
on the Boot 4.1 classpath, then ported the code to `RedisProperties` with a `MIGRATION-TODO`, and the loop was blocked
as an oscillation. So this was not the serializer case of v2.1z-bw. The note `boot4-packages.md` said only that
auto-configuration classes moved from `org.springframework.boot.autoconfigure.<tech>` to
`org.springframework.boot.<tech>.autoconfigure`: for Redis that rule produces a name that does not exist.

**Facts (`javap` / jar listings, spring-boot-autoconfigure 3.3.13 against spring-boot-data-redis, spring-boot-kafka,
spring-boot-data-jpa, spring-boot-jpa and spring-boot-hibernate 4.1.0).**
- `org.springframework.boot.autoconfigure.data.redis.RedisConnectionDetails` is
  `org.springframework.boot.data.redis.autoconfigure.DataRedisConnectionDetails` (`getStandalone()`, `getSentinel()`,
  `getCluster()` still there); `RedisProperties` is `DataRedisProperties`; `RedisAutoConfiguration`,
  `RedisReactiveAutoConfiguration`, `RedisRepositoriesAutoConfiguration` got the same `Data` prefix. The Lettuce /
  Jedis / client-resources customizers kept their names.
- Kafka's classes (`KafkaProperties`, `KafkaConnectionDetails`, the listener container factory configurer, the
  factory customizers) only moved to `org.springframework.boot.kafka.autoconfigure`.
- `autoconfigure.data.jpa.JpaRepositoriesAutoConfiguration` is `…boot.data.jpa.autoconfigure.DataJpaRepositoriesAutoConfiguration`;
  `JpaProperties` is in `…boot.jpa.autoconfigure`; `HibernateJpaAutoConfiguration`, `HibernateProperties`,
  `HibernatePropertiesCustomizer` are in `…boot.hibernate.autoconfigure`.

**Rule.** `boot4-packages.md` lists these and says: a moved name that is not found in the new package was renamed,
not removed; the code is never ported to another type for it. A keyword selects the note for the old and new names.

**Not changed, not checked.** No code. Why the oscillation could happen at all (a fix that trades one missing symbol
for another is scored as improved) is the subject of the next rounds. Other Boot modules whose classes may have been
renamed were not surveyed. Not run with a real LLM.

## v2.1z-by — what a case is told about the earlier fix its errors come from

**Why.** Three company runs ended blocked as an oscillation (WebFlux codecs, catch clauses, Redis connection
details). The notes of v2.1z-bu to v2.1z-bx give the answers for those; this round is about the loop itself. Read in
the code: when a kept attempt A (case X) leaves a new error Y in a file it touched, case Y's prompt gets one hint
line, `introduced by script:<case>:v<N> (attempt <id>) — prefer augmenting that script` (or, for an edits attempt,
`… its change is in the tree; fix what it left, do not undo it`). It does not say which errors A was fixing nor what A
changed, and X is no longer in the build, so "Other error cases in this build" does not show it either: the shortest
change that makes Y disappear is to undo A, and nothing in the prompt says that this brings X back. When X is back
after such an undo, case X's prompt lists A, whose outcome reads `improved`, under the title "Previous attempts on
this case (they did NOT resolve it — do not repeat them)".

**Behaviour.**
- A case whose errors appeared in a file that a kept attempt of ANOTHER case touched gets a prompt section of its
  own, directly after its errors and never truncated (`BuildFixLoop._introduced`: `_score` records, for an error that
  is new in a file an applied attempt touched, the latest such attempt, and forgets the entry when a build no longer
  shows that error, so an entry never outlives the error it explains):
  "Earlier fix these errors come from (it is in the tree)". Per such attempt, at most two, it holds: the attempt (id
  and name), the errors it was fixing (at most three lines in the prompt's error-line format), what it changed in
  the files of this case (the lines of its commit's diff, capped), and the sentences that undoing that change brings
  those errors back, that the answer must leave both sets of errors resolved, and that a fix which went the wrong
  way is replaced in this answer by one that leaves neither. The section as a whole is capped (constant in
  `sbmig/loop/fixloop.py`); what does not fit is left out line by line, the sentences last.
- The section is built only for an attempt that resolved its own case (`improved` / `build_ok`), is still in the
  tree (applied, committed, not reverted, not rolled back) and whose commit range is known. An attempt that was kept
  although it did not resolve its case (`regressed` behind a later commit, `partial`, `inconclusive`) is not "a fix
  that must not be undone": it keeps the old one-line hint, as does one whose diff does not read. A hint line and a
  section for the same attempt never appear together.
- An earlier attempt of the case itself that had made its errors disappear (`improved` / `build_ok`) AND whose
  errors are among the case's errors again (the same file and signature) is no longer listed under "they did NOT
  resolve it". It gets a section of its own, "These errors were resolved before and are back", which says that a
  later change brought them back and that repeating that attempt alone may be undone again. Every other attempt
  stays under the old title, also an `improved` one whose errors are not back (the same case reported in a file
  the build had not reached before).
- System prompt rule 13 says both: attempts listed as not having resolved the case did not work, do something
  different; where the prompt names an earlier fix the errors come from, or says the errors were resolved before and
  are back, the answer must leave both sets of errors resolved and never simply undo the other change (the one case
  in which an answer changes more than its own case's errors need).
- Journal event `earlier_fix_context` (case, attempt asked for, the attempts described, characters) when a section
  is built.
- `FixContext` gains the optional fields the packer needs (empty by default: a tier-2 fixer that ignores them
  behaves as before). The reduced-scope prompt (framework projects) gets the same sections.

**Not changed.** Scoring, rollback, the KB credit, stall / oscillation detection and its limits, the selection of
notes. A fix that undoes another one is still scored `improved` when its own errors disappear: that is the next
round. The prompt of a case that has neither an earlier fix to describe nor an attempt whose resolved errors are
back is byte-identical to before, apart from rule 13.

**As coded.** `FixContext.earlier_fix` (the section body, built and capped by the loop: `BuildFixLoop.
_earlier_fix_context`, `sbmig.fix.context.earlier_fix_text`, constants `EARLIER_FIX_*` in `sbmig/loop/fixloop.py`:
6,000 characters, two attempts, three error lines, 40 diff lines per attempt, only `@@` / `+` / `-` lines) and
`FixContext.resolved_before` (the case's `improved` / `build_ok` attempts; `previous_attempts` stays complete for a
tier-2 fixer). Sections `earlier_fix` (order 1.1, directly after the errors, not truncatable) and `resolved_before`
(5.9, before `previous_attempts`; truncated after it). Over the cap the section loses diff lines first, then the
line naming the commit's other files, then error lines; the head lines and the sentences stay. An attempt whose
commit changed no line in the case's files keeps the one-line hint. A continued gateway session
(`llm.case_thread`) sends both sections in its turn (`LLMFixer.THREAD_TURN_KEYS`).

**Not checked.** Whether a real LLM then finds a fix that leaves both sets resolved.

## v2.1z-bz — a fix that undoes an earlier kept fix and brings its errors back (trade-back)

**Why.** `_score` calls an attempt `regressed` only when its own targeted errors are NOT resolved. An attempt whose
targets disappear is `improved` even when a new error appears in the file it touched. So when attempt A (case X)
leaves error Y, and attempt B (case Y) removes Y by undoing A, both are `improved`: the loop rewards the round trip,
both scripts are credited to the KB as successes, and the run ends only when the build-level oscillation check sees
the whole error set repeat (`loop.oscillation_limit`). This is what happened in three company runs.

**What counts as a trade-back.** An attempt B whose own targeted errors are gone, and an earlier attempt A of
ANOTHER case that was scored `improved` in this run of the loop and is still in the tree, when ALL of these hold for
one error E that A had resolved (a (file, signature) pair with the line the build before A reported it on):
1. **E is back**: the build after B shows that pair in that file, new compared with the previous build (the
   occurrence rule `_new_in_files` uses today; errors that a rollback of the case's own previous try brought back
   are baseline, as today);
2. **A had changed E's line**: a hunk of A's diff in that file covers the line E was reported on (the hunk's old-side
   range);
3. **B reversed that hunk**: ONE hunk of B's own diff removes every line that hunk of A had added and puts back
   every line it had removed (lines compared without surrounding whitespace; blank lines and lines without a letter
   or digit do not count; a hunk without any countable line is never reversed);
4. **E is reported where B put the lines back**: the build after B reports the pair on one of the lines that hunk
   of B added back (for a hunk of A that only added lines: at the position where B deleted them). The hunk's
   new-side range as a whole is not enough: `-U0` joins adjacent changes into one hunk, so a hunk can hold a
   restored line and the error on another line of it.
An error without a line, a diff that cannot be read (git, decoding) or a build that cannot tell gives no verdict.

Why this narrow. Three review rounds each found a legitimate run that a wider form of the rule turned from a
successful build into a blocked case, all reproduced: the pair alone (a correct fix that causes a NEW error of the
same kind elsewhere in the file has the same pair; a previous build that had stopped at an earlier module says
nothing about who brought an error back); any line in common (a common line such as `return null;`, an edit of a
line A had added); a whole hunk anywhere in the file (javac reports flow errors such as `exception … is never
thrown` only when no attribution error such as `cannot find symbol` comes first: A can make an error of its case
disappear by hiding it, and the correct fix B, restoring an import that A had removed, makes it visible again far
from that hunk). The four conditions say: B put back the very lines the error was on, and the error is on them
again. An undo that rewrites those lines instead of restoring them is not seen: the build-level oscillation check
stays for it. The rule prefers missing an undo to reverting a legitimate fix.

**Modes (`loop.trade_back`).**
- `observe` (default): the verdict is computed and recorded, nothing else changes. The attempt keeps the outcome it
  had (`improved`), gets a note that it would have been a trade-back, and the journal gets the event `trade_back`
  with `"mode": "observe"` (attempt, its case, the attempt and case whose fix was undone, the error, the count for
  this ordered pair of cases, whether the count has reached `loop.oscillation_limit`). No revert, no block, the KB
  credit as before. This is how the rule meets real runs first: what it would have done can be read from the
  journal before it is allowed to act.
- `revert`: the attempt is `regressed` with `introduced_errors` = the errors that are back and a note naming the
  attempt whose fix it undid; it goes through the existing handling of a regressed attempt (the reverted tree is
  rebuilt, the stored script is credited as a failure, the case's next try sees the attempt with its `introduced:`
  lines). It is reverted also when a later commit of the same round was kept (`git revert` of its commit, as for a
  structural regression). A revert that fails is a note, and then only that note. Trade-backs are counted per
  ordered pair of cases (the case that undid, the case whose fix was undone; a zero-LLM pre-try and the LLM tries of
  one case are the same case); when the count reaches `loop.oscillation_limit` (2), the case that keeps undoing is
  blocked with `BlockReason.OSCILLATION` at the next clustering of a build that still shows it; the evidence names
  both cases and the attempt whose fix was undone. With `loop.revert_on_regression: false` the attempt is scored
  `regressed` and counted, and stays in the tree like every other regression then.
- `off`: no verdict is computed: the scoring, the journal and the git calls of before, byte for byte.

**State.** What the rule remembers (resolved pairs with their lines, counts, cases waiting for the block) belongs to
one `run` of the loop: it is cleared when a `run` starts, so nothing carries over to another phase, another MSA
service or another target of the remediation.

**Deployment.** `SBMIG_LOOP__TRADE_BACK` is an optional key of ConfigMap `sbmig-env` like the other loop overrides:
a commented line with the three values in `20-configmap.yaml`, an optional `configMapKeyRef` line in
`81-web-job.template.yaml`, a row in `deploy/README.md`.

**Not changed.** With `observe` and with `off`: every verdict, revert, rollback, block and KB credit. In every mode:
the first trade (A leaves Y while resolving X) is `improved`, and so is every fix that causes a follow-up error
without undoing another fix; scoring of attempts whose targets are not resolved, `partial`, `inconclusive` (an
attempt the build could not judge is not checked by this rule either), structural regressions, the rollback before
a case's next try, the caps per case, the build-level oscillation check.

**What a reader can rely on.** With `revert`, an attempt that put back the lines a kept fix of another case had
changed, so that the error that fix had resolved is on those lines again, is not credited as a success, and is not
left in the tree unless its revert failed (noted on the attempt) or `loop.revert_on_regression` is off. With the
default nothing is reverted or blocked because of this rule.

**As coded.** `BuildFixLoop._resolved_by` ((file, signature) → the `improved` attempt that resolved it) and
`_resolved_lines` (the lines that attempt's own errors were reported on), `_trade_backs` (count per ordered pair of
case keys; `_case_key` drops the `case:` prefix), `_trade_back_blocks` (cases waiting for the block): all cleared at
the start of `_run`. `_trade_back_mode` reads the setting (an unknown value is `observe`; YAML's bare `off` / `on`
map to `off` / `revert`). In `_score` the check sits in the branch that says `improved`, so it only ever concerns
that verdict: `_trade_back_of` takes the candidates of condition 1 from `new_in_touched` and reads, for those only
and once per (range, file), `git diff -U0 --no-renames` of the file for A's range (`base_commit..commit`) and for
the scored attempt's own range (`pre_sha..post_sha`, after any rollback of its case's previous try) through
`_diff_sides` (strict decoding with the workspace's encoding candidates; an unreadable diff of one file leaves the
other files of the attempt judged). `_diff_hunks` returns one `_DiffHunk` per `@@` with both ranges, the stripped
countable lines of each side and the new-side line number of every added line; `_covers(start, length, line)` is
`start <= line < start + length`, and for a length of 0 the single position `start` (git's "after line l");
`_reverses(g, h)` compares ONE hunk of the attempt with one hunk of A; `_put_back_at` is condition 4.
`_settle_trade_backs` runs after the reverts (count, journal `trade_back` with `mode`, `count`, `limit`,
`at_limit`, `outcome`; in `revert` also the WARNING log line and the block queue; it forgets resolvers no longer in
the tree and records this build's resolvers); `_apply_trade_back_blocks` runs in `_run` right after the clustering.
In `revert` the attempt not at HEAD is undone by the `git revert` path of structural regressions (`"trade_back":
true` on that `revert` event). The block's suggestion is the existing oscillation sentence; the evidence is on the
blocked item. In `observe` the attempt carries the note `trade-back observed (loop.trade_back: observe, scored as
before): …`, which also shows in the run's `errors.md`.

**Checked.** Real Maven 3.9.10 / JDK 21 on a small project with a scripted LLM that fixes X leaving Y, fixes Y by
restoring the old lines, then fixes Y properly (`out/scratch/trade-back/`, git-ignored), once per mode: `revert`
reverts the undo (journal `trade_back`, credited as a failure) and ends BUILD SUCCESS with 4 LLM calls; `observe`
and `off` give the same builds, outcomes and result (both fixes `improved`, the same answers repeat, BLOCKED
`repeated_failure` after 37 LLM calls), `observe` with one `trade_back` event more. The loop, report, core and
remediation test folders pass with each of the three values in the environment.

**Review.** Two independent rounds on the rule, each with reproductions: the first found three legitimate runs
broken by the pair alone and state carried over between runs; the second one more (the hidden flow error) and the
pooling of the attempt's hunks. The four conditions and the per-run state are their result. The last form of the
rule (conditions 2 and 4, the three modes) was then reviewed by a third independent reader, with reproductions on the
fakes and on real Maven (`out/scratch/trade-back-review3/`). Result: **`revert` is not fit to be the default.** The
four conditions prove that B put A's lines back; they cannot tell which of the two fixes was right, and `revert`
always keeps A. When A was the wrong fix (it deleted a method to make its own error disappear, which left an error
in another file), the correct fix B restores that method, meets all four conditions, is reverted, and its case is
blocked as OSCILLATION at the second try: `off` and `observe` end BUILD SUCCESS in that run (case X is asked again
and answers properly), `revert` ends blocked with A's deletion in the tree, or, when B's case then deletes the
caller, with a green build that lost code. The case of B gets no section about the earlier fix there, because its
errors are in a file A did not touch. No other false verdict was found (whenever all four conditions hold, the
error is on text restored verbatim), `off` is the old behaviour line for line, and `observe` adds the journal event
and a note only (plus the KB credit of "v2.1z-ca"). Also found, recall only: the lines remembered for A
(`_resolved_lines`) come from the build before A, which still had the rolled-back previous try of A's case in the
tree, while A's diff starts after that rollback; when the rollback shifts the lines, a real undo gives no verdict.
And the count per pair of cases spans files: two trade-backs in two unrelated files block the case everywhere.

**Not checked.** A real LLM; the company projects; multi-module and MSA projects with real Maven; the remediation's
fix loop and framework runs with the loop on (both use `BuildFixLoop`); how often a real undo restores the old lines
exactly (when it does not, this rule does not see it); errors that javac reports on another line than the one a fix
changed.

## v2.1z-ca — a stored script that took part in a round trip is not a success and is not reused on its own

**Why.** Reproduced with real Maven 3.9.10 / JDK 21 (`out/scratch/kb-replay/`, the KB that the `observe` run of
`out/scratch/trade-back/` left): after a run in which two cases undid each other, both stored versions read
`successes 1, failures 0`. A second run of the same project on that KB reuses both without an LLM call
(`"source": "kb-script"`), repeats the round trip and credits both once more. The fix notes and the prompt section
about the earlier fix are only read when the LLM is asked, so on such a KB they never reach the case. Reuse does not
even need a success: `CaseStore.best` picks the newest version that is not `retired` and not `non_idempotent`.

**The rule.** It acts on the verdict of "v2.1z-bz" (the four conditions; `loop.trade_back: observe` or `revert`;
with `off` there is no verdict and nothing of this section happens). For an attempt B with that verdict against an
attempt A of another case:
- **B.** When B ran a stored version and that run decided its outcome (the condition `_credit_script` uses), the
  version is credited for this try with the result `round_trip`, never with `improved`. In `revert` the attempt is
  reverted as before; its credit is `round_trip` too (it was `reverted`).
- **A.** When B stays in the tree after the scoring (its outcome is not `reverted`: mode `observe`, a revert that
  failed, `loop.revert_on_regression: false`) the errors A had resolved are in the build again: when A ran a stored
  version the same way, the credit A got for its try at the earlier build is replaced by `round_trip`. When B was
  reverted, A is left as it is: its fix is in the tree.
- One attempt is credited `round_trip` at most once per try. Unstored scripts (v0), answers applied as edits, a dry
  run and a run without a KB are untouched, as for every credit.

**The store (`CaseStore.credit`, result `round_trip`).**
- `round_trip` is a failure result (`FAILURE_RESULTS`): `failures` +1, `last_failure` = the reason, `last_result`.
- It sets `CaseScript.round_trip = True` (new field, default false; a `case.json` without it reads as false).
- When the same try (`run_id`, `round`, `n`) is already recorded on that version with a success result of the
  version's own scope, that success is taken back before the failure is counted: `successes` −1 (never below 0),
  the project leaves `projects` unless another kept success run of the version's own scope names it, and a `proven`
  version goes back to `unverified` unless `_resolved_elsewhere` still holds (`retired` stays `retired`). The run
  record of the try is replaced (the existing rule of `_add_run`). Only `round_trip` takes a success back: no other
  result changes what an earlier credit of the same try counted.
  The project that leaves is the one of the run taken back (the credit's own project when that run names none);
  "another kept success run" means one still in `runs[]` (the history is capped at `errors.script_runs`).
- When the same try is already recorded on that version as a failure of its own scope (`round_trip` included),
  the record is replaced and nothing more is counted: one try counts as one failure.
- A run of the other scope (v2.1z-ap) is kept in `runs[]` without a count or a status change, as for every result,
  but it does set the mark: the mark is about reuse, and a full run may reuse a `jdk-jakarta` version.
- **Reuse.** `CaseStore.best`, `sbmig.triage.scripts.stored_best` and `stored_next` never select a version whose
  `round_trip` is true, exactly as for `non_idempotent`. `latest` (the base of an augment) is unchanged.
- **The mark goes away** when the version is credited with a success that did not come from a reuse, whatever the
  scope of the run (the counts stay own-scope; `add_script` finds a version by its code across scopes, so a mark
  that only a success of the version's own scope could clear would never clear for runs of the other scope): an
  LLM answer whose code is the stored code (`add_script` returns the existing version). The loop says
  which it was (`credit(..., reused=True)` for an attempt whose `script_mode` is `reuse`); a success of a reuse
  counts as a success and leaves the mark (another pod may have planned the reuse before the mark was set). And
  when a person runs `sbmig kb cases activate <case> <version>`.
- `scripts_text` shows ` round-trip` after the status (as ` non-idempotent`), so the "KB 탐색" log line of a case
  whose only versions are marked reads `케이스는 있으나 쓸 스크립트 없음 (v1 unverified round-trip 0/1)`. A KB import
  merges the mark like `non_idempotent` (true on either side stays true). The merge of the counts stays "the larger
  count of the two sides", corrected by the take-backs the merge can see: for every try that one side records as
  `round_trip` and the other as a success of the version's own scope, the side that still counts that success
  loses it (and gains the failure) before the larger counts are taken; the project of such a run leaves
  `projects` unless another kept own-scope success run names it, and `proven` goes back to `unverified` unless
  `_resolved_elsewhere` still holds (`retired` stays). The `round_trip` record replaces the success record of
  that try in the merged history. Both records of such a pair are of the version's own scope (a `round_trip` of
  the other scope never takes a success back, as in `credit`); when the success record names no project, the
  project of the `round_trip` record is the one that leaves. Without such a try the merge is what it was before. (Counting again from the
  run records was tried and withdrawn: the history is capped at `errors.script_runs`, so it dropped the successes
  of a widely reused version.)
  `sbmig kb cases show` names the marks `round-trip` and `non-idempotent` on a version's line.

**The loop.** `BuildFixLoop._round_trips` (attempt → reason, cleared at the start of `_run`) is filled by
`_settle_trade_backs` for every verdict. `_credit_script` credits an attempt found there with `round_trip` and that
reason instead of its outcome, and does so once more for an attempt that was already credited with another result
(the resolver A); the journal event is the existing `script_credit` with `"credited": "round_trip"` and the reason.
Reasons, one line each:
`round trip: put back the lines attempt <A> (case <case of A>) had changed in <file>; the error(s) it had resolved came back`
(B) and
`round trip: the error(s) this version resolved came back when attempt <B> (case <case of B>) put back the lines it had changed in <file>`
(A). Each attempt gets the note `stored v<N> credited as round_trip: not reused without an LLM answer`. The
attempts' outcomes, the reverts, the counts per pair of cases, the blocks and the error list are those of
"v2.1z-bz": this section changes KB credit only.
`_record_round_trip` does the recording per verdict. When B undid the fixes of several attempts, B's reason names
the first of them and every one of them gets its own reason and credit. The first reason recorded for an attempt
stays (an attempt can be B in one verdict and A in a later one). A resolver that was never credited before simply
gets its one `round_trip` credit. A failure of this bookkeeping is a WARNING and never reaches the scoring.

**`observe`, restated.** The run itself behaves as without the rule (no revert, no block). The KB credit of the two
versions is the one thing a verdict changes there.

**Not changed.** Without a verdict: every credit, every reuse decision. `loop.trade_back: off`: byte for byte as
before. A `non_idempotent` or `retired` version, the ranking of the versions that may be reused (scope, proven,
version), `script.reuse_versions`, the ladder caps.

**What a reader can rely on.** After a run in which the rule saw one case undo another's fix, neither stored
version involved counts that try as a success, and neither is run on a later project without the LLM being asked.

**Why both versions.** The rule cannot tell which of the two fixes was right (review in "v2.1z-bz"): A may be the
wrong one (it deleted code to silence its error) and B the correct restore. Marking costs a wrongly marked version
one LLM ask on a later project, after which a success clears the mark; reusing the wrong one repeats the damage
without anyone being asked.

**Checked.** Real Maven 3.9.10 / JDK 21, the project and scripted LLM of `out/scratch/trade-back/`, two runs in a
row on one KB with the default `observe` (`out/scratch/round-trip-kb/`): after run 1 both versions read
`successes 0, failures 1, round_trip true`; run 2 has no `kb-script` attempt for the two cases, asks the LLM for
both, and ends with both marked again (the scripted LLM repeats its answers, so the run still ends blocked).
Before the change the second run reused both without an LLM call (`out/scratch/kb-replay/`). On that older KB
(both versions `successes 1`) the first run with the change replays both once, marks them (the old successes of the
earlier run stay counted) and the second run reuses neither.

**Review.** One independent round with reproductions (`out/scratch/round-trip-review/`) found three paths that
broke the sentence above: a version of the other scope was not marked, a success of a reuse cleared the mark, and
a `kb import` of an earlier export brought the taken-back success back. The rules for the other scope, `reused`
and the import in "The store" are their fixes. A verification of those fixes (`out/scratch/round-trip-verify/`)
confirmed the first two and found that the first form of the import fix (a recount from the run records) dropped
counts the capped history no longer held, and that a mark set from a run of the other scope could not be cleared
by an LLM answer in that scope: the import rule and the clearing rule above are the result.

**Limits.**
- A KB written before this section may hold such pairs as successes. They are marked the first time a run replays
  them and the verdict is reached (that run still makes the round trip once); `sbmig kb cases retire`, or a new KB
  folder, avoids that replay.
- An undo that rewrites the lines instead of restoring them gives no verdict (v2.1z-bz) and so no mark.
- A `kb import` of an export made while a version was marked brings the mark back after a later success had
  cleared it (true on either side stays true): one more LLM ask clears it again.
- A build pod on an older image does not honour the mark: it reuses such a version, and a success it credits
  does not clear the mark. It keeps the field when it rewrites the `case.json` (unknown fields of a script
  version survive a re-save).

## v2.1z-cb — the same error in another module is another error (stall, oscillation, scoring, the error list)

**Why.** A company run on a multi-module project (2026-11-24, read from the journal and `errors.md`). Maven stops at
the first failing module, so a module-level error shows in one module per build. Here it was maven-compiler-plugin's
`Resolution of annotationProcessorPath dependencies failed: Cannot find version for annotation processor path
'<g>:<a>.jar'`, once per module whose pom lists a processor path without a version. The loop fixed the pom of module
1 (build 3), other errors followed, the same message showed for module 2 (build 6) and was fixed, other errors
followed, it showed for module 3 (build 9). Three poms, real progress, and the run ended there blocked as
OSCILLATION, because:
1. the stall and oscillation checks compared `BuildResult.signature_multiset()`: counts per L1 signature, in which
   the module name is masked. Build 6 "equalled" build 3 and build 9 "equalled" build 6: two oscillations,
   `loop.oscillation_limit` reached, every case the phase had worked on that was still present was blocked;
2. `_pair` gave every error without a file the place `''`, so the scoring could not tell the error of module 1
   from the one of module 2 either (had the two builds followed each other directly, the fix of module 1 would have
   read as "its target is still there");
3. the error list matched the occurrence in module 2 to the entry of module 1 (key: case, file, line, message) and
   recorded "reappeared".

**The rule.** The place of an error is its file. For an error without a file it is its module
(`BuildError.module`), written `@<module>`; an error with neither keeps the place `''`. The loop compares errors by
(place, signature) everywhere:
- `_pair`, and with it the scoring: what is left of an attempt's targets, what is new, `introduced`, the resolved
  pairs of the trade-back rule;
- the stall and oscillation checks: the multiset compared between builds is the count per (place, signature), not
  per signature. The same kind of error in another file or another module is not the same error set.
  `IterationRecord.signatures` (the report) stays `signature_multiset()`;
- the error list: `_match` pairs a file-less error only with an entry of the same module (the exact key and the
  "line moved" step); an occurrence in another module is a new entry carrying its module.

**Not changed.** Errors that have a file (their place was the file already). Case ids and the L1 / L2 signatures: the
module stays masked there, the KB reuses a case across modules and projects. The ladder per case (see "Open").

**What a reader can rely on.** Fixing a module-level error in one module and meeting the same message for the next
module is progress: it is no stall and no oscillation, the first fix is not scored as if its target were still
there, and the error list shows one entry per module.

**As coded.** `_place(e)` in `sbmig/loop/fixloop.py` (the normalised file, else `@<module>`, else `''`); `_pair`
returns `(_place(e), signature)` and every comparison goes through it (`_new_in_files` stays about the files an
attempt touched). The stall / oscillation value is `Counter(_pair(e) for e in build.errors)`. In the error list
`_same_module` guards the first two steps of `_match` (an entry or error without a module matches as before; the
third step, a file the restructure moved, concerns errors with a file), and the check for the same error twice in
one build in `add_build` carries the module too, so two modules failing in one build are two entries.

**Checked.** Real Maven 3.9.10 / JDK 21 on a three-module project whose modules each list a processor path without
a version (`out/scratch/module-place/`), scripted LLM. The parsed error is `kind compile, file None, module m1`
(then `m2`, `m3`); its L1 signature holds no module name. With answers that differ per module the run fixes the
three poms one after the other, the journal has no `stall` and no `oscillation` event, the list has one entry per
module, and the build passes. Before the change the unit test of the same sequence gave two oscillation events,
and the direct case (module 1 fixed, the next build shows module 2) scored the fix `no_change` and rolled it back.

**Review.** One independent round on a copy of the commit (`out/scratch/module-place-review/`): the stall and
oscillation checks lost nothing that costs LLM calls (50 rounds per sequence, old against new: an error that
alternates between two places is now blocked as OSCILLATION after 3 calls where it used to run to the cap of 20;
an error that never repeats a place ends at the same cap in both). One defect, older for errors with a file and
now reachable for module-level ones: after a case's previous try was rolled back, `_rollback_previous` added to
the next try's baseline every targeted error absent from the build, also those the build could not show (it had
stopped at an earlier module). Identical builds then read "1 of 2 gone": `partial`, and a `partial` try is kept
at the end of the loop, so a change that broke an earlier module stayed in the tree. Fixed: only errors the build
could have shown (`_visible_in`) count as restored.

**Open (closed by "v2.1z-cd").** The ladder still read the occurrence in the next module as the same case going
on: with answers whose code is the same for every module, the real-Maven run ended blocked `repeated_failure`,
every augment refused as "identical to the version it should extend, which did not fix the case".

## v2.1z-cc — an annotation processor path without a version gets one in the restructure

**Why.** The same company run as "v2.1z-cb". The module poms list Lombok under maven-compiler-plugin's
`<annotationProcessorPaths>` without a `<version>`; the as-is parent's dependency management used to supply it.
After the parent swap the target chain does not manage `org.projectlombok:lombok`. The version step of the
restructure ("v2.1l") writes a version into the `<dependencies>` entry, but a processor `<path>` is not a
dependency and stayed empty, so maven-compiler-plugin stopped every module in turn with `Resolution of
annotationProcessorPath dependencies failed: Cannot find version for annotation processor path '<g>:<a>.jar'. The
version needs to be either provided directly in the plugin configuration or via dependency management.` Each module
cost the fix loop an LLM ask and two builds.

**The rule.** In every restructure mode that runs the version step, right after the dependency versions are settled
(`_drop_managed_versions`): for every project pom and every `<path>` under a maven-compiler-plugin
`<annotationProcessorPaths>` (build plugins and plugin management; `PomEditor.artifact_refs()` section
`annotationProcessorPath`) that names a groupId and an artifactId, has no `<version>` and no `${…}` in its
coordinates, the first of these that applies:
1. **managed: left as it is.** The g:a has a managed version in this pom's own chain inside the project (a
   `<dependencyManagement>` entry with a version in the pom or a project ancestor) or in the company parent chain
   this pom inherits (its managed versions, imported BOMs included): maven-compiler-plugin takes the version from
   the dependency management. The managed versions are keyed by g:a, so this holds for a plain jar path only; a
   path with a classifier or another type is "managed" only through an entry of the project's own chain with the
   same classifier and type.
   **Maybe managed: left as it is, with a note.** When the effective pom of the new chain is not available (only
   the parent chain facts are), the step cannot see what a BOM imported inside the project (a `<scope>import`
   entry in the pom or a project ancestor) or a `<dependencyManagement>` inside a profile manages. A pom with
   either gets no version from steps 2 to 4: writing one could override the version Maven would have taken from
   that management (measured: a Lombok path left empty builds through an imported BOM; the same path with the
   as-is 1.18.12 written fails on JDK 21). The profiles of project ancestors count like their imports. One import
   is not such a reason: the `spring-boot-dependencies` import of a pom whose Boot BOM versions the parent chain
   facts already hold (the import the restructure itself writes when no parent manages Boot).
   `_drop_managed_versions` records whether its managed map is the effective pom's (`c.drop_managed_exact`).
2. **the pom's own dependency.** This pom's `<dependencies>` entry of the same g:a (same classifier and type) has a
   version now: the path gets the same text (a literal or a `${property}`), so the processor and the library stay
   on one version. When that version is one the dependency step took from the repository's newest release in this
   run, the line says so (`the pom's own dependency (repository newest release)`): the dependency's follow-up
   asks the owners to confirm it.
3. **what the dependency step would write here.** The target Boot BOM's version when the chain does not manage the
   g:a, unless the as-is build resolved a newer one; else the version the as-is build resolved for this module
   (`_asis_version`). Without an as-is capture the dependency step writes nothing, and neither does this step.
4. **the project's other poms.** Other project poms declare exactly one literal version for that g:a (a dependency,
   a managed dependency, a processor path): that version. A version the dependency step took from the
   repository's newest release in this run does not count as declared (one written by an earlier run of the tool
   is an ordinary declared version by then).
5. **unknown: left as it is**, with one note naming the path and the pom. The build reports it and the fix loop
   takes it from there.
Never the repository's newest release: an empty path version does not break the effective-pom capture, so nothing
forces a guess. Steps 3 and 4 are skipped for a path with a `<classifier>` or a `<type>` other than `jar` (the
captures key g:a only). Not touched: a path that has a version; the keys the dependency step protects (parents,
role parents, mandatory / optional / removed in-house artifacts, the project's own modules).

**Recorded.** Each version written is a line of `RestructureResult.versions_pinned`
(`<g:a>: <version> ← <source> (annotationProcessorPaths, <pom>)`), a row of the error list (`annotation processor
path version written: <g:a> <version> (<source>)`, on that pom), one restructure note with the count, and one
journal event `versions_pinned` with `"section": "annotationProcessorPaths"`. The structure review's material
lists the lines with the dependency pins. The requester's Korean report shows the change in the pom's diff; its
check list of carried-over versions is built from the poms' `<dependencies>` and does not name processor paths.

**Not changed.** Runs without a restructure (a framework run keeps its parent, so its management still covers the
path). Paths with a version, also when it differs from the dependency's (the structure review reports that). The
fix loop's handling of the message when it still appears.

**What a reader can rely on.** After the restructure no project pom has a version-less annotation processor path
for which the pom's own dependency, the dependency step's sources or the project's other poms name one version
while the pom's dependency management is known and names none. And the step never writes a version into a path
that Maven would have resolved from the dependency management.

**Checked.** Real Maven 3.9.10 / JDK 21 with the real pipeline (`out/scratch/processor-paths/`): a three-module
Boot project whose as-is parent managed the artifact of a version-less processor path in every module. With the
step switched off the first builds stop with the message for one module after the other (`build=FAIL`); with the
step the journal lists three written versions (two from the as-is build, one from the pom's own dependency), no
build shows the message, and the run ends `build=ok`. The artifact was commons-text, not a real processor: Maven
resolves the path's version the same way whatever jar it names, and the stand-in parents of the local setup
manage Lombok.

**Limits.**
- maven-compiler-plugin resolves a version-less path from the dependency management from 3.12.0 on (measured:
  3.8.1 and 3.11.0 fail on any path without a version, 3.13 / 3.14 resolve it from the pom's own management, an
  imported BOM and an active-by-default profile). Under an older plugin a "managed" path that this step leaves
  empty still fails; the fix loop takes it from there.
- Inserting the version into a `<path>` written on one line reformats that element.
- A pom path that contains parentheses is not recognised by the error-list row parser (as for the dependency
  pins): the row lands on the root pom.

**Review.** One independent round on a copy of the commit (`out/scratch/processor-path-review/`, sbmig probes and
real Maven): the refactoring of the dependency step's version choice is behaviour-preserving (72-case
differential), MS949 / CRLF poms are edited byte-true, a `${property}` copied by step 2 survives the version
cleanup, the four call sites are right. Found and fixed: an imported BOM not seen without the effective pom (the
"maybe managed" rule), a repository-guessed version reaching paths through steps 2 and 4, a classifier path
left empty because the plain g:a was managed, a Boot BOM version written without an as-is capture.

## v2.1z-cd — a case that shows up at a new place starts with the script that resolved it elsewhere in this run

**Why.** The run of "v2.1z-cb" again. The tries of a case were counted per case and run, wherever its errors were:
when the same module-level error showed for module 2 after module 1 was fixed, the ladder read it as "the case is
still not resolved" and asked the LLM to augment the last script. The right step was to run, for the new module,
the script that had just resolved the case in the other one. The real-Maven reproduction
(`out/scratch/module-place/`, answers with the same code for every module) ended blocked `repeated_failure` after 37
LLM calls, every augment refused as "identical to the version it should extend, which did not fix the case",
although that version had resolved its module and was scored `improved`.

**Places.** The places of a set of errors are their `_place` values ("v2.1z-cb": the file, else `@<module>`, else
`''`; files through the registry's canonical-file mapping, so a file the restructure moved is one place). An earlier
attempt of the case was made **here** when one of the places of the errors it targeted (`cluster_errors`) is a place
of the case's errors in the current build, and **elsewhere** otherwise.

**The rule.**
1. **The ladder counts the tries made here.** `BuildFixLoop._previous(cluster)` with `ladder=True` (what
   `plan_script_step` reads through `TriageContext.previous_attempts`: the versions already run, the LLM tries, the
   augments, the stored versions used, the edit tries, the declines) holds the attempts made here only. With
   nothing made here the case is on the first rung again. The caps `script.max_llm_tries_per_case`,
   `script.max_augments_per_case`, `script.max_edit_tries_per_case`, `script.max_declines_per_case` and
   `script.reuse_versions` therefore hold per case, run and set of places. The prompt's "Previous attempts on this
   case" (`ladder=False`) keeps every attempt of the case, wherever it was made, with its outcome.
2. **First rung at a new place: what resolved the case elsewhere in this run.** When no attempt was made here and an
   attempt made elsewhere ended `improved` or `build_ok` by RUNNING a stored version (version > 0, the script ran:
   not an answer applied as edits), the step is a reuse of that version (the newest such attempt), ahead of the
   KB's own choice (`best`): no LLM call, source `kb-script`, the rationale and the "KB 탐색" log line say that the
   version resolved the case at `<place>` in this run. Not when `script.reuse` is off, the version is `retired`,
   `non_idempotent` or `round_trip`, or its code is gone. That reuse counts as the stored-version step of the new
   place (`script.reuse_versions`). If it does not help there, the ladder goes on as after any reuse that did not
   help (it is an attempt made here now): augment, then new.
3. **Identical code is refused only for the places it ran on.** The check "the LLM answered with v<N> again
   (identical code), which already ran in this run" looks at the attempts made here only: an answer whose code is a
   version that ran elsewhere runs.
4. **A bound across places.** The LLM asks of a case in one run, counted over every place, are capped at twice
   `script.max_llm_tries_per_case` (40 with the default). A case that has used them is blocked
   `repeated_failure` at its next step, whatever place it shows at; the rationale and the "KB 탐색" line name the
   total and both numbers. Reruns of rule 2 are not LLM asks and do not count.

**Not changed.** A case whose errors are at a place an earlier attempt targeted: every step as before. The
rollback of a case's previous try, the scoring, the blocks, the KB credit (a rerun is credited like any reuse, with
`reused`). A script that changes nothing at the new place ends `apply_failed` without a build, as any script that
changes nothing, and the ladder goes on in the same round.

**What a reader can rely on.** A fix script that resolved a case in one module or file is run again, without an LLM
call, when the same case shows at another place in the same run; tries that were made for other places do not use up
the tries of the place at hand.

**As coded.** `sbmig.triage.decide`: `error_place`, `error_places`, `made_here(cluster, attempt, canon)` (an
attempt without placed errors, or a case whose errors have no place, counts as made here).
`BuildFixLoop._previous(ladder=True)` keeps the attempts made here; `_resolved_elsewhere(cluster)` lists the
candidates of rule 2 and travels as `TriageContext.resolved_elsewhere` (optional, empty by default: a plugin
triager is unaffected) and as `plan_script_step(..., elsewhere=)`; `sbmig.triage.scripts.resolved_elsewhere`
reads the version and its code from the store and gives up (no older candidate is tried) when the store says
`retired`, `non_idempotent`, `round_trip` or the code is gone. Scoped to here: the ladder (`_decide`,
`_script_step`), the identical-code check with its journal field `duplicate_of`, the decline count
(`_decline_count(case, c)`), and the LLM-failure count of `loop.blocked_policy` (it reads `previous_attempts`).
Whole case as before: `_rollback_previous`, the evidence count of a block, `_block_oscillation`, the script
history, `_sibling_hints`, the prompt history. The log line: `KB 탐색: 이번 실행의 v<N> (<attempt>, <place>에서
해결) → <place>에 다시 실행`.

**Checked.** Real Maven 3.9.10 / JDK 21, the three-module project of "v2.1z-cb" with a scripted LLM
(`out/scratch/ladder-per-place/`). Answers with the same code for every module: module 1 by the LLM, modules 2
and 3 by reruns (`kb-script`), also for the file-level case of each module; the run ends `build=ok` with 3 LLM
calls (the structure review and one per case), where it had ended blocked `repeated_failure` after 37. Answers
that differ per module: the same sequence, 3 calls instead of 6.

**Limits.**
- With `script.reuse: false` there is no rerun: the first step at a new place is an augment whose base is the
  stored version, and an answer with the same code is refused as before this section.
- A rerun that changes nothing at the new place is credited to the version as `apply_failed` (a reuse), as any
  stored script that changes nothing on a project.
- The caps hold per set of places, so a case can use more LLM calls in total than
  `script.max_llm_tries_per_case`, up to the bound of rule 4.
- A script that resolves one place and breaks a new one every time is rerun (rule 2) without an LLM call until
  the rounds end (`loop.phases.verify.max_iterations`): builds only, and each rerun is credited to the version
  as a success of that one project.
- An answer applied as edits leaves a stored script that never ran: it is not a rerun candidate; the new place
  then starts with the KB's own choice.

**Review.** One independent round on copies of the commit and its parent (`out/scratch/ladder-review/`): no path
that runs a wrong script (reruns go through the same script runner, guards included; another MSA target's attempts
are no candidates; a `non_idempotent` version is never rerun), and the count of tries made here is right for a
case with errors in many files. Found: a case cycling through five places, more than `loop.oscillation_window`,
was no longer stopped by the per-case cap (45 LLM calls where the parent commit blocked after 20): rule 4. And a
rerun candidate was checked against the store's `round_trip` mark only: an attempt the loop itself recorded as
part of a round trip (`_round_trips`) is no candidate either, also when that credit did not reach the store.

## v2.1z-ce — a rename or a removal that Windows refuses while another program has a file open is waited for

**Why.** A company PC run of 2026-11-24 (Windows, local web run): the migration ended BUILD SUCCESS and the chained
remediation ended `FAILED internal_error` 27 seconds after its start, with `PermissionError: [WinError 5]` at the
last step of the intake, the rename of `remediation/.accepted.partial` to `remediation/accepted`
(`intake.accept_zip`). The same rename had passed on that PC on 2026-11-12 and the code of the step had not changed
since, so the refusal depends on timing. Windows refuses to rename a directory while a file inside it is open in
another process, and to replace or delete a file another process has open without the delete-share flag. A virus
scanner, an indexer or an editor that has the folder open opens freshly written files for a moment, and the intake
had just written every file of the project. Nothing in this process holds a file of the tree at that point
(`_extract` closes each file it writes). Which program held the file is not known.

**The rule.** New module `sbmig/remediations/fsops.py` (standard library only, imported by the other modules of the
package, never the other way round):

| function | is |
|---|---|
| `replace(src, dst)` | `os.replace` |
| `rename(src, dst)` | `os.rename` |
| `unlink(path)` | `os.unlink` |
| `rmtree(path)` | `shutil.rmtree` |

1. **Waiting.** When the call raises an `OSError` whose `winerror` is 5 (access denied) or 32 (sharing violation),
   it is made again: after 0.05 s, the pause doubling up to 2 s, until the wait limit has passed since the first
   refusal (`fsops.HOLD_WAIT_S`, 60 s; the last pause is cut to the time that is left). Then the last exception is
   raised, the same object with one mark: `fsops.waited_out(exc)` is True for it. Not waited for: a `replace` whose
   destination is a read-only file. Windows refuses that every time, and the caller that expects it
   (`intake._write_back`) makes the file writable and tries again at once, as before.
2. **A caller that repairs and tries again does not do so after a wait.** `intake._write_back` catches
   `PermissionError`, makes the path writable and writes once more. For an exception with the mark it raises at
   once: the refusal was not a permission, and the wait has been spent.
3. **A holder that lasts is not waited for once per file, inside one batch.** `fsops.one_wait()` is a context
   manager: inside it, once a call has waited out its limit, every later refusal is waited for
   `fsops.SHORT_WAIT_S` (2 s) at most. Outside it every call has the full 60 s, whatever happened before: the
   memory belongs to the block (a context variable), never to the process, so a give-up in one place (a run that
   failed, a removal whose failure is only logged) takes nothing from another call, another run or another thread.
   Two users, both of which go on after a refusal: `intake.sync_tree_table`, which tries every path before it
   fails (with n held paths it waits about 60 + 2(n − 1) s), and `fixer._remove_leftovers`, the removals at the
   end of a fix loop whose failure is only logged (the unused patch, the unused fixed files, the work copy).
4. **Everything else is raised at once.** An exception without such a `winerror` is not waited for: on POSIX no
   `OSError` carries one, so on Linux (the cluster) the four functions behave exactly as the calls they stand for,
   without any pause.
5. **Removals (`rmtree`, `unlink`).**
   - Read-only entries: Windows does not delete a read-only file, and `original/source.zip` is read-only. On
     `winerror` 5 from removing one entry, the entry is made writable (`os.chmod(p, stat.S_IWRITE)`) and that one
     removal is made again, before the waiting applies. Only for that `winerror`: POSIX is unchanged.
   - `rmtree` also waits for `winerror` 145 (directory not empty) from the removal of a directory. Where a deleted
     file keeps its name until the last program closes it (a holder that opened it with delete sharing on a file
     system without POSIX delete semantics: FAT, a network share, older Windows), the directory is refused for
     that moment. On current NTFS the name goes at once and this wait is not needed. A directory another process
     keeps writing into is retried for the whole wait, each try removing what that process wrote in the meantime,
     and then fails with the 145. The other three functions do not wait for 145.
   - On a later try a path that no longer exists is success. On the first try a missing path raises as
     `shutil.rmtree` does.
6. **Log lines** (logger `sbmig.remediations.fsops`, so they reach `sbmig.log` and `remediation/remediation.log`).
   `<name>` is the last component of the path (the source of a `replace` / `rename`), never the full path; `<s>` is
   the wait limit of that call (60, or 2 after a give-up inside `one_wait()`):
   - at the first refusal, INFO: `remediation: <op> of <name> refused (WinError <n>): another program has a file
     open there; waiting up to <s> s`
   - when it goes through after waiting, INFO: `remediation: <op> of <name> went through after <t> s (<k> tries)`
   - when the time is up, WARNING: `remediation: <op> of <name> still refused after <t> s (<k> tries, WinError
     <n>): a virus scanner, an indexer or an editor that has the run folder open keeps a file open there`
7. **Call sites.** Every `os.replace`, `os.rename` and `shutil.rmtree` in `sbmig/remediations/` goes through
   `fsops` (also the report's `Path.replace`, both branches of the removal of an old work copy before the fix loop
   starts, and `fixer._rmtree_quiet`, whose failure is still only logged), and so do the removals of
   `graph._safe_rmtree` and `chain._clear_root` (their `unlink` branch too). Left as they are:
   - `chain.RunClaim._take_over` keeps a bare `os.rename`: a takeover acts on a judgment of staleness made just
     before it, and a rename that had waited could move aside a claim another process made in the meantime. A
     refusal there fails at once, as before.
   - `shutil.rmtree(…, ignore_errors=True)` in `intake._make_writable_and_remove` and the service's removal of a
     job folder (it has its own make-writable handler).

   A test reads the package's syntax trees and keeps new bare forms out: any use of `os.replace`, `os.rename`,
   `os.renames`, `shutil.rmtree`, `shutil.move` (called, assigned or imported by name) and a method call
   `.replace(x)` / `.rename(x)` with one argument (the `Path` forms; `str.replace` takes two), outside `fsops.py`
   and the three sites above.

**Not changed.** What a refusal that lasts means: the step fails as before (`FAILED internal_error` with the
`PermissionError` text at the intake; `_recover_accepted` after a commit that got through one of its two renames).
No copy in place of the rename. Single-file `unlink` calls outside the helpers named in rule 7 (among them
`graph.discard_delivery`, which fails at once where the swap that installs a delivery waits). The migration's own
`sbmig.errors.common.atomic_write_bytes` (six tries within about 1.5 s) and the web package.

**Limits.**
- A program that keeps a file open for more than 60 s still fails the step.
- A refusal that reports `winerror` 5 and can never go away (a folder's access rights, a process of a killed run
  whose working directory is inside the tree) is waited for like a holder: 60 s per call.
- While a call waits, neither a stop request nor the time budget is looked at. At the end of a fix loop
  (`fixer._remove_leftovers`) a holder that lasts delays the answer to a stop by about a minute.
- From Python 3.13 on `os.chmod` follows a symbolic link on Windows, so a read-only link refused on removal has its
  target made writable. The intake refuses links, so the trees of a run hold none of their own.
- Nothing here was run on Windows: the tests raise the Windows error from the patched call, and what Windows
  reports in each case is taken from its documentation and from CPython's source.

**Checked.** `tests/remediations/test_v21zce_fsops.py` (42 tests; the Windows errors are raised from the patched
`os` calls, a fake clock stands for the waiting). Full suite 7,807 passed on a developer Mac.

**Review.** Three rounds by one independent reviewer, each finding reproduced by a scratch test
(`out/scratch/fsops-review/`). Linux: 450 cases comparing each function with the call it stands for (exception
type, errno, file names, the tree left on disk; missing paths, links, read-only folders, `str` / `Path` / `bytes`)
on Python 3.12 and 3.13, no difference and no pause. Found and changed:
- round 1: `intake._write_back` waited twice for one held file and the sync multiplied it (600 s for five held
  paths): rules 2 and 3. A stale-claim takeover that had waited could remove a claim another process made in the
  meantime: `_take_over` keeps the bare rename (rule 7). Tests that missed four changed versions of the module, a
  guard that did not see the `Path.replace` form, and a fake clock on which the loop could not end: the tests of
  today and the end rule of the loop (the sum of the pauses counts as time waited).
- round 2: the first form of rule 3 remembered a give-up for the whole process for 300 s, so a retry of a failed
  run, or the delivery swap after a removal that had only been logged, got 2 s: the memory now belongs to a block.
- round 3: the removals at the end of a fix loop waited in full three times: `fixer._remove_leftovers` is a block.

Accepted without a test: the check that a block's state is used only by the thread that opened it (on the
installed Pythons a new thread starts with an empty context).

## v2.1z-cf — a refusal that lasts names the programs that have the file open

**Why.** A company PC run of 2026-11-25 (Windows, local web run, the fix of v2.1z-ce in place): the intake's rename
passed, the remediation reached its second round, and the swap that installs a delivery (`graph._Holder.install_delivery`)
failed at `fsops.replace(delivery/.result.next.zip, delivery/result.zip)`: refused for the whole 60 s, 36 tries,
`WinError 5`. The destination was the result ZIP of round 1, so some program had that file open for more than a
minute. The log line could only guess ("a virus scanner, an indexer or an editor"). Two things are not known: which
program it was, and whether it was this process. Between the two swaps this package reads that file for the rescan
(the upload `ivas.upload`, `models.sha256_file`) and both close it, by reading the code (`graph._copy_atomic` reads
it too, but only when the earlier verified ZIP is kept, which was not the case in that run); no test here can
notice a handle of our own, because POSIX lets an open file be replaced. A first round never meets the case: there
is no `result.zip` to replace yet.

**The rule.**

1. **`fsops.holders(paths) -> list[str]`.** The programs that have one of the given files open, one string each:
   `<application name> [<executable's base name>] (pid <n>)`, with `, this process` inside the brackets of the pid
   part when `<n>` is `os.getpid()` (`python.exe (pid 4312, this process)` when the two names are the same or one is
   missing). Windows only: the Restart Manager (`rstrtmgr.dll`: `RmStartSession`, `RmRegisterResources`,
   `RmGetList`, `RmEndSession`) through `ctypes`, standard library only, no administrator right needed. The
   executable's name is asked for with `QueryFullProcessImageNameW` and left out when the process cannot be opened.
   Only base names are returned, never a path: a name that looks like a path (a backslash, a drive prefix, a
   leading slash, or a slash and no blank) is cut to its last component. Off Windows it returns `[]` without loading anything. It never raises:
   any failure is `[]` and one DEBUG line with the reason. A single path (`str`, `bytes`, `os.PathLike`) is taken as a list of one. Directories are not registered (the Restart Manager takes
   files): for a directory the regular files under it are taken, in `os.walk` order, at most
   `fsops.HOLDERS_MAX_FILES` (500) over all given paths.
2. **One lookup, when the time is up; never while the call is retried.** While `fsops._waiting` retries, nothing
   is looked up: a lookup of a directory lists it, and on Windows a listing in progress is itself a handle under
   the directory the call is trying to rename. When the time is up the lookup runs in a daemon thread and the
   call waits for it `fsops.HOLDERS_WAIT_S` (5 s) at most. No lookup is started inside a `one_wait()` block after
   a give-up (the calls that wait `SHORT_WAIT_S`): the first give-up of the block has named the holder, so a
   block with n held paths waits about 60 + 2(n − 1) s as before, plus 5 s at most once. No lookup is started
   either while an earlier lookup of this process has not returned: that is a failed lookup with the reason
   `a lookup started <t> s ago has not returned`. A thread that cannot be started is a failed lookup, never an
   exception of the call (an interrupt or an exit request is not caught). Nothing is logged from the helper
   thread (the web's per-run log is filtered by thread): the calling thread writes every line.
3. **Which files are looked at.** `replace` and `rename`: the source and the destination; `unlink`: the path;
   `rmtree`: the path. A path that does not exist is left out.
4. **Log lines** (the beginnings are those of v2.1z-ce, so `Select-String "refused|went through"` finds them as
   before):
   - first refusal, INFO: unchanged.
   - went through, INFO: unchanged (a call that goes through looks nothing up).
   - time is up, WARNING, when the lookup found programs:
     `remediation: <op> of <name> still refused after <t> s (<k> tries, WinError <n>): open in <names>`
   - time is up, the lookup ran and found nothing: the sentence of v2.1z-ce, then ` (no program found with the <f>
     file(s) looked at open)`.
   - time is up, the lookup failed (a call of the Restart Manager answered an error, the library could not be
     loaded, the thread could not be started, an earlier lookup has not returned): the sentence of v2.1z-ce, then
     ` (the lookup of the holding program failed: <reason>)`. `<reason>` is a function name with its return code,
     an exception's type name or the sentence of rule 2, never a path. A failed lookup is never worded as "no
     program found".
   - time is up, the lookup did not return in time: ` (the lookup of the holding program did not return in <s> s)`.
   - time is up inside a `one_wait()` block after a give-up (no lookup was started): the sentence of v2.1z-ce alone.
   `<names>` is at most eight entries joined by `, `, then ` and <m> more`.
5. **Nothing else changes.** The waiting, the exception raised, `waited_out`, `one_wait` and every caller are those
   of v2.1z-ce. The failure text that reaches the state, the report and the page does not carry the names.
6. **A test that stands for Windows' rule on POSIX** (`tests/remediations/`, test code only). A fixture makes every
   `os.replace`, `os.rename`, `os.unlink` and `shutil.rmtree` that `fsops` makes fail the test when this process has
   a descriptor open on the source, on the destination, or on a file under a directory being renamed or removed
   (descriptors read from `/proc/self/fd` on Linux, with `fcntl.F_GETPATH` on macOS). The flow tests that reach a
   second round and the delivery swap run under it. What it cannot see: a child process (Maven), another process
   (the web pod reading a run folder), a memory mapping.

**Limits.**
- The Restart Manager reports programs that have a handle on a registered file. A process whose working directory
  is inside the tree, a kernel component (a scanner's file system filter) and a file that is only memory-mapped may
  not be reported; "no program found" is then itself the clue.
- The `ctypes` part runs only on Windows and was not run there before the push: the tests drive it through
  callbacks with the documented signatures. A mistake there shows as `the lookup of the holding program failed:
  <reason>` in the WARNING, never as a failed step.
- A lookup that never returns (a call of the Restart Manager that blocks) ends the lookups of that process: every
  later give-up says how long ago it started. Restarting the process brings them back.
- A lookup of a directory stops after 500 files, but lists every folder it passes through until then (a tree of
  20,000 empty folders took 1.2 s on a developer Mac). When the listing takes longer than the 5 s (a slow share, a
  huge tree with few files), it may still run while the caller goes on to the next path.
- Two calls whose time is up at the same moment: one looks up, the other logs that a lookup started 0 s ago has
  not returned.
- Rule 5 of v2.1z-ce's limits stands: a holder that lasts still fails the step.

**Checked.** `tests/remediations/test_v21zcf_holders.py`: the Restart Manager calls are driven through `ctypes`
callbacks built from the documented signatures (so every argument goes through the declared `argtypes`), the
structure sizes and offsets, the five endings of the WARNING, a lookup that never returns followed by later calls,
a thread that cannot be started, an interrupt at thread start, and a refused directory rename retried under the
guard of rule 6 with nothing of ours open under it. `tests/remediations/test_v21zcf_own_handles.py` (rule 6): two
rounds with round 2 replacing round 1's `result.zip` (also with the verified ZIP kept), one round from intake to
report, the remediation-only flow, `accept_zip`, the fix loop on a failed candidate, a resume after round 1 and
after a killed fix loop, and `IvasHttpClient.upload` after success and after each of its error paths: no
descriptor of this process on any path it replaces, renames or removes. The guard is proved on a reader that
never closes (it names `install_delivery`'s replace of `result.zip`).

**Review.** Four rounds by one independent reviewer, every finding reproduced first
(`out/scratch/fsops-holders-review/`). The Windows calls were compared with Microsoft's documentation function by
function (signatures, constants, the structure layout, in / out meanings): no difference found.
- round 1: a failed lookup was worded "no program found"; inside a block every give-up started a lookup and
  waited for it; a thread that could not be started broke the retries; a single path made `holders()` raise; the
  fake library never applied the declared argument types.
- round 2: one lookup that never returned ended all later ones under a false sentence; the lookup started at the
  first refusal listed the directory the call was retrying to rename, which on Windows is a handle under it (our
  own lookup could cause the refusals it reported): the design was cut down to one lookup when the time is up;
  an interrupt at thread start was swallowed; a forward-slash path as an application name was logged in full.
- round 3: nothing in the production code. The guard of rule 6 counted the descriptors of the test run's own disk
  watch (the walk of `scripts/bounded.py`), which made a guard test fail at random: the walk and the guard's
  reading now exclude each other (`bounded.WALK_LOCK`).
- round 4, on that lock: nothing. The walls still stop a run (the temp cap with exit 125, the time cap with 124),
  the watch is delayed by milliseconds at most, the guard still reports a descriptor of our own, and the three
  test files passed 15 runs in a row.

**Not checked.** Windows itself: the `ctypes` part has never run there. Whether a caller without administrator
rights is told about a handle held by a service of the system account (a virus scanner), paths over 260
characters, a network share, a subst drive.


## v2.1z-cg — the image's JDK is Amazon Corretto 21 at a pinned version

**Why.** Relayed by the user on 2026-11-25: the image must carry a named JDK 21 build in which Java4Shell is fixed,
fetched directly at that version. Java4Shell is CVE-2026-114938 (an out-of-bounds write in HotSpot's Base64
intrinsic). The change log of Amazon Corretto 21 lists it under `21.0.13.1.2` (released 2026-11-17) and not under
the builds before it. Until now the image installed Debian's `openjdk-21-jdk-headless`: which build that is depends
on the state of the apt mirror on the day of the build (today the candidate in trixie is `21.0.13.1+2-1~deb13u1`,
the same upstream level), and nothing in the Dockerfile says it.

**The rule** (`deploy/docker/Dockerfile`).

| build argument | default | is |
|---|---|---|
| `CORRETTO_VERSION` | `21.0.13.1.2` | the Corretto 21 build |
| `CORRETTO_BASE_URL` | `https://corretto.aws/downloads/resources` | where it is fetched from; an internal mirror keeps the layout below |
| `CORRETTO_SHA256_AMD64` | `c365469853d9296728bb19e629225fef04b45e595e3467d8a0778450367381e0` | SHA-256 of the amd64 package |
| `CORRETTO_SHA256_ARM64` | `d251fb09557ccf7992d36b810a96df859531821679d21d0ab1eddc2b8e8c1ba0` | SHA-256 of the arm64 package |
| `CORRETTO_TLS_VERIFY` | `1` | `0` = the download does not check the server's certificate (a network that inspects HTTPS); the checksum is checked either way |

1. The file is `<CORRETTO_BASE_URL>/<CORRETTO_VERSION>/java-21-amazon-corretto-jdk_<CORRETTO_VERSION>-1_<arch>.deb`,
   `<arch>` from `dpkg --print-architecture` (`amd64`, `arm64`; any other stops the build).
2. Its SHA-256 is compared with the argument of that architecture before anything is installed; a difference stops
   the build. A build that changes `CORRETTO_VERSION` must give the two checksums of that version (from the
   release page of `corretto/corretto-21`), otherwise it stops here.
3. The package is installed with apt from the local file BEFORE `maven`. It provides `java7-runtime-headless`, which
   Debian's `maven` accepts in place of `default-jre-headless`, so no OpenJDK package of Debian enters the image.
   The same `RUN` fails when a package named `openjdk-*` is installed at its end, and when `java -version` does not
   print `CORRETTO_VERSION`.
4. `/opt/java21` is a link to `/usr/lib/jvm/java-21-amazon-corretto` (the same path on both architectures);
   `JAVA_HOME` / `JAVA21_HOME` stay `/opt/java21`. Maven, git, unzip and curl come from Debian as before.
5. Messages of the build are English (a Windows console), comments Korean.

**Not changed.** No manifest, no environment variable, no setting (`build.java_home` still wins when set). The
operator rule for an image change stands: build, push, restart the web pod.

**Notes.**
- Corretto reads its own certificate store (`lib/security/cacerts`), not Debian's. A company CA added to the image
  with `update-ca-certificates` reaches curl and git, not Java; it has to be imported with `keytool`. The image adds
  no CA today.
- The values of the table were taken from the release page of `21.0.13.1.2` and checked by downloading both
  packages on 2026-11-25. Per the reports of the disclosure only x86-64 processors with AVX-512 VBMI are exposed,
  and `-XX:-UseBASE64Intrinsics` is the stopgap for a JVM that cannot be replaced; neither was checked here.

**Checked** (Docker 29 on a developer Mac, 2026-11-25; logs under `out/scratch/corretto-cg/`).
- arm64, default arguments, from the public address: built; apt installed `java-21-amazon-corretto-jdk` and
  `java-common` and, with `maven`, no Java package of Debian. In the image `java -version` prints
  `Corretto-21.0.13.1.2 (build 21.0.13.1+2-LTS)`, `mvn -version` Maven 3.9.9 on that runtime, `dpkg -l` one Java
  package, `/opt/java21` the Corretto folder, and `python -m sbmig.cli version` starts.
- amd64 under emulation: built, the same lines.
- A mirror (`CORRETTO_BASE_URL` at a local HTTP server with the `<version>/<file>` layout): built.
- A wrong checksum: the build stops before anything is unpacked, with the message naming the argument.
- `CORRETTO_TLS_VERIFY=0` against the public address: built (the switch is wired; a network that inspects HTTPS
  was not available).
- `tests/deploy/test_v21zcg_image_jdk.py` (8 tests) reads the Dockerfile and holds the arguments, their defaults
  and the order of the steps.

**Not checked.** A build through a proxy or on a network that inspects HTTPS; the company's registry and cluster;
the three stop messages whose condition never held here (another architecture, an OpenJDK package found,
`java -version` not the pinned build).

## v2.1z-ch — the admin page's log ZIP of one run carries the run's diagnostic files

**Why.** The user on 2026-11-25, reading a failed cluster run: "관리자 로그 다운로드 너무 한정적으로만 받아진다..
분석에 필요한게 많이 비어있네". The ZIP held `sbmig.log`, `journal.jsonl`, `control/request.json` and the
remediation's log. The name of the pom an OpenRewrite run could not read was in `logs/NNN-rewrite-run.log`, which
could only be reached with `kubectl exec` and a hand-typed `grep` on a console without copy and paste.

**The rule** (`sbmig/web/admin.py`, `sbmig/web/static/admin.html`).

1. **Which download.** `GET /admin/api/runs/{id}/logs.zip` and `…/logs/check` (one run). The archive of every run
   (`GET /admin/api/logs.zip`, `…/logs/check`) is unchanged: it would outgrow its size limit.
2. **What is added**, beside the four members of v2.1z-ao, each at its path in the run folder (only files with a
   text suffix the end-of-run redaction pass rewrites: `.md .json .jsonl .log .txt .yml`):
   - the files under `logs/` (the output of each Maven and OpenRewrite call, the dependency trees, the captured
     facts, the recipe files);
   - at the top of the run folder: `errors.md`, `errors.jsonl`, `report.md`, `report.json`, `plan.json`,
     `profile.json`, `job-status.json`, `web-status.json`, `remediation-job-status.json`, `job-local.log`,
     `worker-paths.yml`;
   - of the remediation: `state.json`, `journal.jsonl`, `report.md`, `report.json`, `claim.json`,
     `delivery/delivery.json`, `scans/*.json`, `baseline/build.log`, a round's `*.json`, `build.log`,
     `build-fixed.log`, the dependency trees of `work-before/` and `work-after/`, and a fix loop's `loop.json`,
     `journal.jsonl` and `logs/`.
   The principle decides a file not named here: a text file that says what the run did is in; a copy of the
   project is out. `control/state.json` and `control/remediation-resume.json` are NOT added: the end-of-run pass
   does not rewrite `control/` (rule 4).
3. **Never in the archive:** `project/`, `codediff/`, `merge-conflicts/`, `target/`, `llm-records/`, any tree of
   the remediation (`original/`, `accepted/`, `baseline/tree/`, a round's `candidate/` and `applied/`, a fix
   loop's `tree/` and `fixed/`), any hidden file or folder (`.git` among them), any `*.zip`, `*.jar`, `*.class`,
   `*.xml`, every `*.patch` / `*.diff` (diffs of source), and LLM request / response records (`llm-<n>.json`,
   `logs/review-input.md`). Excluded folders are never entered. The uploaded ZIP keeps its own download
   (v2.1z-bb).
4. **Redaction.** No byte leaves through this archive unredacted, and the archive does not rely on the
   end-of-run pass having run or succeeded (that pass skips a file it cannot rewrite, stops at an exception, and
   nothing records its outcome; a run folder without a readable control state never had one).
   - **Every added member is redacted while it is copied,** with the secret values of the web process's
     environment (every spelling of `expand_values`) and URL userinfo, which is masked also when no secret
     value is set. A member of at most `admin.RUN_WHOLE_REDACT_BYTES` (8 MiB) is redacted as a whole with the
     functions the end-of-run pass uses (`redact_text`; `redact_json_text` for `.json` and for each line of a
     `.jsonl`, so a value behind a JSON escape and every line of a multi-line value are masked as the pass
     masks them). A larger member is redacted line by line through `redact.Redactor`, as the write-time hook
     does; there a line shorter than `redact.MIN_LINE_LEN` that belongs to a multi-line value is not
     recognised. The three always-in members (`sbmig.log`, `journal.jsonl`, `control/request.json`) are copied
     as before.
   - A member is cut at the size it had when planned; when the file holds more by the time it is read, its
     last, possibly incomplete line is not written.
   - A member the redaction cannot handle is left out with its reason, never a failed archive: the redaction
     raised (a `.json` nested too deep), a line over `admin.RUN_LINE_LIMIT` (1 MiB) in a member redacted line by
     line, or a run of letters and digits longer than `admin.RUN_TOKEN_LIMIT` (4,096): the userinfo pattern of
     `redact.py` takes time quadratic in the length of such a run (0.4 s for 16,000 characters).
   - A member that cannot be redacted line by line is not added and is named in `SKIPPED.txt`: a file the pass
     takes for binary (a NUL byte in its first 8 KiB, `not a text file`) and a suffix outside the pass's list.
   - The added members are still left out while the run's end-of-run pass is pending or a remediation resume is
     active (`not redacted yet: …`): such a file may still be written.
   - Known limit, as before this increment: a secret the run obtained while it ran (a gateway token, an IVAS
     token) is not among the web process's values; it is masked only where the run's own output hooks wrote it.
5. **Limits.** Those of v2.1z-ap stand (one archive at a time, the local temp folder, the free-space floor, a
   file over 512 MiB named in `SKIPPED.txt`, each member copied up to the size it had when planned). New:
   - at most `admin.RUN_ZIP_MAX_FILES` members per run (2,000), counting only files that can go in;
   - the 2 GiB total: where the one-run archive would pass it, members are left out until it fits (never the
     three always-in ones), instead of the 413 the archive of all runs answers;
   - the order in which members are left out, for both caps: the numbered files directly under `logs/`, lowest
     number first, then the other files under `logs/`, then the rest from the end;
   - planning opens no file and lists at most `admin.RUN_ZIP_SCAN_LIMIT` folder entries (20,000) of which `logs/`
     may use at most half, so the remediation's files are always reached; every folder that was not listed
     completely gets a line in `SKIPPED.txt`, a folder that could not be read among them;
   - links are never followed. A member must be a regular file with one name (a hard link, `st_nlink` > 1, is
     refused) inside the run folder, and the file opened for copying must be the file that was planned (same
     device and inode): a folder or file swapped after planning is refused and named in `SKIPPED.txt`;
   - a file name that cannot be written into the archive (not UTF-8) is left out and named, never a failed
     archive; the names of rule 3 are matched without regard to case.
6. **The page.** The button of one run says what it gives (logs, reports and state files of that run, no source);
   the reasons of `SKIPPED.txt` are English, the page's sentences Korean.

**Not changed.** The usage table, the login, the archive of all runs, the download of the uploaded ZIP.

**Left as it is.**
- Past the `logs/` half of the listing budget (more than 10,000 entries there), which files are listed follows
  the file system's order, not their numbers: keeping the newest would mean listing the whole folder.
- The quadratic userinfo pattern itself (`redact._URL_USERINFO`): the end-of-run pass and the write-time hook meet
  it on a line that holds such a run. Found while this archive was built; not changed here.
- The always-in members are left out, with their line in `SKIPPED.txt`, when the end-of-run pass replaces them
  between planning and copying (a narrow race; asking again gives the file).

## v2.1z-ci — OpenRewrite gets a settings file it reads the way Maven does

**Why.** A cluster run of 2026-11-25: `transform.rewrite` failed twice with `Failed to parse or resolve the Maven
POM file or one of its dependencies; We can not reliably continue without this information.` while `mvn clean
package` of the same tree, with the same `-s`, passed; the operator had changed the mirrors that day. Measured the
same evening with the plugin sbmig runs (rewrite-maven-plugin 6.46.1, rewrite-maven 8.89.0, Maven 3.9.10; scratch
`out/scratch/rewrite-settings/`):
- OpenRewrite takes Maven's own settings (the session's merge of `-s` and `-gs`; no parameter names another file)
  but downloads poms with its own code and applies the mirrors itself, and differently: a `mirrorOf` containing
  `external:http:*` matches nothing (it asks the ORIGINAL repository URL), `<blocked>` is ignored (it downloads
  from the blocked mirror's URL), and of two matching mirrors it takes the first in file order where Maven takes the
  exact id. `*`, `external:*`, explicit ids and `!id` behave as in Maven.
- A pom in the local repository counts for OpenRewrite only when a non-empty `<a>-<v>.jar` lies beside it, so a
  dependency used with a classifier always goes remote. With the settings shape the company uses (the blocker id
  redefined as `external:http:*`, plus a `central` mirror) and an original repository that is not reachable, that
  ends `Unable to download POM: <g>:<a>:<v>. Tried repositories:` although Maven builds through the mirror.
- The warning `Failed to download <g>:<a>:<v> from <pom>. Attempted URIs:` is printed on passing runs too. A
  repository OpenRewrite could not reach is not in "Tried repositories": only `[WARNING] Failed to access maven
  repository <url> due to: …` names it.
Which pom failed on the cluster is not known yet.

**The rule** (`sbmig/rewrite/`).

1. **A copy for the OpenRewrite call that only ADDS mirrors Maven cannot match.** When `build.settings_file` is
   set and `openrewrite.translate_settings` is true (default), every call of `OpenRewriteRunner` passes
   `-s <copy>`; every other Maven call of sbmig keeps the original. The same `-s` is read by Maven itself inside
   that call (the forked lifecycle of `rewrite:run` resolves the project's dependencies), so the copy must not
   change Maven's behaviour in any case, known or not. It therefore moves and edits nothing: it inserts, at the
   front of `<mirrors>`, one **shadow mirror** per repository id whose mirror OpenRewrite would choose
   differently from Maven:
   `<mirror><id>(new)</id><mirrorOf>sbmig:<repository id></mirrorOf><url>(the url of the mirror Maven selects for
   that repository; the repository's own url when Maven selects none)</url></mirror>`,
   and a `<server>` with the new id copied from the selected mirror's `<server>` when it has one.
   - Maven compares each `mirrorOf` token with the repository id as a whole, so `sbmig:<id>` matches no repository
     and the shadow is inert there, whatever else the project or its dependencies declare; the one exception is a
     repository whose own id begins `sbmig:`, and a project that has one gets no copy at all.
   - OpenRewrite (`MavenRepositoryMirror`: everything up to the first `:` is dropped, the rest is the id list,
     the mirror applies to non-local repositories only, the first matching mirror in file order wins) reads it as
     "repository `<id>` goes to this url", before any other mirror.
   - A later OpenRewrite that reads `mirrorOf` as Maven does would find the shadows inert too: then its own choice
     is Maven's already.
2. **What a shadow is made for.** The known repositories are those of the settings' active profiles (user and
   global; `activeProfiles` and `activeByDefault`), of the project's poms including their `<profiles>` (each
   (id, url) pair; a `${…}` in a url filled from the properties of the pom and of its parents inside the project,
   an unfilled one kept as written) and `central`. For each, Maven's choice is
   computed by Maven's rule (an exact id first, then the patterns in file order; `*`, `external:*`,
   `external:http:*`, `!id`; the user file's mirrors, then the global file's) and OpenRewrite's by its rule. A
   shadow is written when they differ and Maven's choice is not a blocked mirror. No shadow, and one journal
   reason per id, when: the id or a mirror's `id` / `mirrorOf` holds a `${…}`; two known repositories share the
   id and Maven sends them to different places; the repository is local (`localhost`, `127.0.0.1`, `file:`),
   which a shadow cannot reach; its url is unfilled and Maven's choice would depend on it. Blocked mirrors are
   not imitated: where Maven refuses a repository the run fails in Maven either way.
3. **The copy holds what the original holds** (credentials of `<servers>` among them), so it is written with mode
   0600 into a private temporary folder of the process, never into the run folder, and removed when the call
   returns, also on an exception. The original text is kept byte for byte; only the inserted elements are new. A
   settings file that cannot be parsed, or any error while reading, computing or writing: the original file is
   passed and the journal says why. A translation never fails a run.
4. **Records.** Journal `rewrite_settings {translated: bool, shadows: [{repository, mirror}], skipped: [{repository,
   reason}], reason}` once per run (ids only, never a url or a credential); one INFO line. Nothing when the file
   needs no shadow.
5. **A failed recipe run says which pom.** When the plugin's output has `Unable to download POM: <g>:<a>:<v>`, the
   run's error text (and so the `transform.rewrite: the automatic conversion failed …` line of `sbmig.log`, the
   journal and the report's first hand-over line) carries, in this order: the repositories of the `Failed to
   access maven repository` warnings as "not reachable" (with the reason), the coordinates of at most three poms
   and a count of the rest, and the distinct tried repositories with their answers (`<host and path>: HTTP
   <code>`, userinfo stripped), each named once. That line is cut at 300 characters, so what an operator needs
   first comes first; the full text is in the journal's `rewrite` event.
6. **The download retry does not start for a warning.** `openrewrite.download_retry` (wait, run again) is for a
   transfer problem: a 429 / 5xx answer, a reset, a timeout, a host name that did not resolve, `Could not transfer
   artifact`. The bare words `Failed to download` no longer start it (OpenRewrite prints them on passing runs).
   An `Unable to download POM` starts it only when something points at a transfer problem: a tried repository
   whose answer is not `HTTP 4xx` (a 429 counts as a problem), or a `Failed to access maven repository` warning
   whose reason is a timeout, a reset or an unknown host (the pom may be on that repository). All tried
   repositories answering 404 and unreachable ones only refusing the connection: no retry.

**Not covered, and said so in the operator text:** repositories declared in the poms of dependencies or in a
parent pom outside the project (the company parent): OpenRewrite asks their own url unless their id has a shadow,
and a repository of a dependency that reuses a shadowed id with another url is sent to the shadow's url by
OpenRewrite (never by Maven); local repositories; a
settings profile activated by a property, a file or the OS (OpenRewrite does not see it); what Central costs when
it is not reachable and not mirrored (about a minute per pom that falls through to it).

**The rule for operators** (`deploy/README.md`): mirrors as `*`, `external:*`, explicit ids or `!id` only; an
exact-id mirror before a `*` mirror; no `external:http:*`, no `<blocked>`. One `*` mirror at the repository group
is the simplest form for a closed network.

## v2.1z-cj — a library that exists only as a jar with a classifier, without a pom, stops OpenRewrite

**Why.** The user's cluster (2026-11-25 / 26): a framework project declares each module of the in-house framework as
`<dependency>… <version>V</version><classifier>C</classifier></dependency>`. The repository holds `<a>-<V>-<C>.jar`
and nothing else for that version: no pom, no main jar. Maven warns (`The POM for … is missing, no dependency
information available`) and builds. OpenRewrite stopped the conversion: `Unable to download POM: <g>:<a>:<V>. Tried
repositories:` with `HTTP 404` from every repository, for a dozen modules, on every run. Read in the plugin
(`MavenPomDownloader.download`, rewrite-maven 8.89.0) and measured (rewrite-maven-plugin 6.46.1, Maven 3.9.10; scratch
`out/scratch/classifier-pom/`): a pom that is found nowhere is accepted only when a non-empty `<a>-<V>.jar` (exactly
that name, no classifier) lies in the local repository or a repository answers HEAD for it; a pom in the local
repository whose packaging is `jar` (or absent) counts only with that jar beside it; a local pom whose packaging is
`pom` is taken as it is.

| measured | result |
|---|---|
| classifier-only library, no pom anywhere, empty local repository: recipe run | fails, the text above |
| the same: `mvn clean package` | passes (the warning) |
| recipe run again, `<a>-<V>-<C>.jar` now in the local repository | fails again |
| a minimal pom with `packaging` jar in the local repository (the stub of v2.1z-ax) | fails again |
| a minimal pom with `packaging` pom in the local repository | passes; the recipes change the pom and the sources |
| `mvn clean package` with that pom present | passes |

**What.** The stub loop of v2.1z-ax (`sbmig/build/pomless.py`, behind every Maven call that resolves dependencies)
learns a second form. Nothing else changes: same switch `build.pomless_stub`, same memo, same cap, same rerun of the
same command, same flag `pomless-library`, same hand-over line.

1. **Detection** in the log of a failed command: `Unable to download POM: <g>:<a>:<v>. Tried repositories:` (only
   OpenRewrite prints it). Each coordinate once, in log order. The answers printed under it are not read: a later
   block of the same library says `Did not attempt to download because of a previous failure …`, and the proof
   below asks the repositories itself. The sentence `Unable to download POM <gav>. Version contains unresolved
   property placeholder.` is another failure and is not matched.
   OpenRewrite stops at the first module whose pom it cannot resolve and names only that module's libraries
   (measured with two modules and the sentence alone: three recipe runs for two libraries; with the warnings
   below: two). So every library of Maven's own warning in the same log, `The POM for
   <g>:<a>:jar:<classifier>:<v> is missing` (exactly five parts, type `jar`), is a further candidate, each once:
   Maven resolves the dependencies of every module before the plugin's goal ends, and prints that warning on
   every build. The warnings are read only in a round in which at least one library of the sentence got its stub:
   without the sentence the warning alone starts nothing, and when every library of the sentence is refused the
   next run would fail on it again, so nothing is written for the others and nothing runs again (measured: a
   refused snapshot beside two healthy classified libraries cost one useless recipe run before this rule). All
   candidates go through the same proof.
2. **Proof before anything is written**, cheapest first; the first that fails ends it with one journal line
   `pomless_check {ga, version, reason}`:
   - the coordinate is a plain one (`bad-coordinate`), the cap is not reached (`cap`), the local repository is
     known (`no-repository`);
   - the version is not a snapshot (`snapshot`): it does not end in `-SNAPSHOT` and is not the dated form
     `<base>-<yyyymmdd.hhmmss>-<n>` (Maven's warning names a snapshot that way, so one snapshot library can leave
     two such lines). OpenRewrite asks for the dated file name of a snapshot, which a file named `<a>-<v>.pom`
     does not answer (measured: the stub was written and the rerun failed the same way);
   - no file at `<local repository>/<g path>/<a>/<v>/<a>-<v>.pom` (`pom-present`: a pom is there and OpenRewrite
     failed all the same, so the cause is another one; no rerun for this library);
   - no file `<a>-<v>.jar` in that folder (`main-jar-available`);
   - a regular, non-empty file `<a>-<v>-<classifier>.jar` in that folder (not a link), the classifier one or
     more of `[A-Za-z0-9_.-]`, `sources` and `javadoc` in any case not counted, a name holding `--` not counted
     (it goes into an XML comment) (`jar-unavailable` when there is none, also when the folder cannot be listed). Maven resolves
     the project's dependencies before the plugin's goal runs, so the jar is there after the failed call; no
     repository is asked for it and the project's poms are not read for the classifier. With several such files
     the first by name is the one recorded;
   - `RepositoryLookup.file_answers`: no repository returns the pom (`pom-available`), and none answers `ok` for
     the main jar (`main-jar-available`). A lookup without `file_answers` or without repositories: `no-repository`.
   A parent pom or an imported BOM never gets a stub: Maven needs it before the plugin's goal starts, so its pom
   is normally in the local repository already (`pom-present`), and when it is not, no jar with a classifier lies
   in its folder (`jar-unavailable`).
3. **The stub**: the same path, written by the same routine (temp file, hard link, never over an existing file).
   It starts with the comment of `STUB_MARKER` (so `is_stub` and the later-run check recognise it) and says that
   only a jar with a classifier exists for this version (the file's name), that OpenRewrite needs a pom for a
   library without a main jar, and that it must be deleted when the real pom is uploaded. `modelVersion`,
   `groupId`, `artifactId`, `version`, `packaging` **pom** (this version has no main artifact), no dependencies.
4. **Records.** Journal `pomless_stub {ga, version, path, jar_from: "local repository", created, form:
   "classified", jar: "<file name>"}`; the form of v2.1z-ax carries no `form` key. One INFO line per stub that says
   "only a jar with a classifier". New `pomless_check` reasons: `snapshot`, `pom-present`, `main-jar-available`.
5. **The cap.** `build.pomless_stub_max` default 20 → 60: a framework has more than twenty modules, and a run that
   stops at the cap fails the conversion once more than needed.
6. **A later run is told too.** `known_coordinates` no longer leaves out a jar dependency that has a classifier, so
   `stubs_used` finds these stubs and the report of a later run names the libraries.

**Not covered.** A repository that answers the pom of such a library with a transfer error (5xx, refused) stops
Maven itself (`Failed to read artifact descriptor for G:A:jar:C:V`): v2.1z-ax still reads only the form without a
classifier. Once a stub of this section lies in the local repository Maven no longer asks for that pom. A
classifier-only library whose jar is not yet in the local repository when OpenRewrite fails (a goal that resolved
nothing) gets no stub. A zero-length `<a>-<v>.jar` in the local folder ends at `main-jar-available` although
OpenRewrite does not count an empty jar. A snapshot version. A warning does not say who asked: a plugin's own
dependency that has only a jar with a classifier gets a stub too when it stands in such a log (measured;
unnecessary, harmless, named in the hand-over line, not seen by a later run's check). As in v2.1z-ax, nothing removes a stub: with the file
in place Maven no longer asks a repository for that pom, so a pom uploaded later is used only after the file is
deleted (the file and the hand-over line say so). Measured with a stub in place: a dependency on the same
coordinate without a classifier, with another classifier or with another `<type>` fails as it did before (`Could
not find artifact`); `dependency:tree` prints the same line; only the warning `The POM for … is missing` is gone.

## v2.1z-ck — a framework project whose conversion is done is shown as done, also when the build after it fails

**Why.** The first framework project that got through the conversion on the company cluster (2026-11-26) ended
with a failed build, as designed: such a project gets JDK 21 and Jakarta only, one build, no automatic fix
(v2.1z-aw). The page called it 「미완료」 in the warning colour. The user: "사내 프레임워크는 이정도면 완료니까
미완료로 하지말고 사내 프레임워크 java 21, jakarta 변환 완료면 완료로 화면에 표시되도록 해줘! 빌드가 안되더라도
프로세스상으로는 완료니까!" For these projects the conversion is the whole job; the build errors that remain are
the owners' and are reported, not a sign that the run did not finish.

**What.** One rule, read by every place that words the outcome of such a run.

1. **The rule**: `sbmig.report.result_line.conversion_complete(report)` (a `RunReport` or the dict read from
   `report.json`). True when all of these hold:
   - the run is a migration (`flow` is not `remediation`) planned with `plan.scope == "jdk-jakarta"`;
   - no flag `aborted`, `aborted:<reason>` or `mvn-not-found` (the run was not refused, stopped or cut short);
   - no flag `rewrite-failed` and no flag `hollow-reactor`;
   - every planned conversion step has a result and none failed: the number of OpenRewrite results that are not
     dry runs (`phases[].rewrite_results[]`) reaches the number of planned steps of kind `openrewrite` that carry
     recipes (`plan.steps`), is at least one, and every such result is `ok`. One result is recorded per executed
     step; a step skipped for the time budget records none, which matters for an MSA plan with one step per
     service (one converted service and one skipped is not a done conversion);
   - `final_build` is a build result (an object whose `ok` is a boolean): the build was attempted; it may have
     failed.
   A value that cannot be read counts as not satisfied (a `flags` that is not a list, `plan.steps` that
   is not a list or holds an entry without a kind, `phases` or `rewrite_results` that are not lists, a result
   entry that is not an object, a `final_build` without a boolean `ok`); the function does not raise.
   It is derived from what a report already stores, so a run that finished before this rule existed is shown
   the new way after the image is replaced. It applies to every reduced run, with `framework.fix_loop` on or
   off (the PM's choice: what makes a framework project done is the conversion; with the loop on the wording
   below still says that the build does not pass).
2. **What does not change.** `RunReport.success`, the exit code (2 for a failed build), every token of
   `SBMIG-RESULT` (`exit=2 build=FAIL unresolved=<n> … scope=jdk-jakarta`), `attn`, the fleet's status
   (`incomplete`), the Job's result, `status()["success"]`, the error list, the report's sections on the errors
   that remain, and the chained remediation's own state and wording. The build did fail; only the name of the
   run's outcome changes. A run of another scope is worded as before in every place.
3. **Run status** (`GET /api/runs/{id}`, each row of `GET /api/runs`, the `status` events): the key
   `conversion_complete: true` when the rule holds for the run's `report.json` and the run's exit code is 2, 0 or
   not recorded; absent otherwise (no report yet, a report that cannot be read, any other run, a remediation-flow
   run, a Job whose container ended with another code after the report was written, e.g. a pod killed during
   the chained remediation).
4. **The page** (`static/index.html`), only when `conversion_complete === true`, `success === false`, the exit
   code is 2, 0 or not recorded (the same codes as the status key; a Worker ends with 0 after a failed build
   unless `SBMIG_FAIL_JOB_ON_BUILD_FAIL=1`), and the run is not one of the earlier cases of `describe` (waiting, running,
   stopped by a person, exit code 3); any other exit code reads as it did before:
   - title in the success colour: 「JDK 21, Jakarta 변환을 마쳤습니다. 변환 뒤 빌드는 통과하지 않았습니다」, followed
     as before by 「남은 오류 N건」 / 「막힌 항목 N건」 when there are any;
   - the row of 「지난 실행」: 「완료」 in the success colour (the suffix of a chained remediation as before);
   - the stage rail: the build step is not drawn as failed (red cross). It gets the class `buildwarn` (an
     exclamation mark in the warning colour), and the note under the rail reads 「변환 뒤 빌드는 통과하지
     않았습니다. 남은 오류는 보고서에 있습니다.」 (only in place of the note 「빌드 검증 단계가 실패로 끝났습니다」;
     a stop, refusal or remediation note is never replaced, and a step already drawn as stopped or refused keeps
     its mark). `markVerify()` decides, each time one of its inputs changes. While the run is still going (the
     chained remediation follows the build), the page decides from what it has seen: a reduced-scope run
     (`framework_project` / `plan` journal events, or the status's `framework`) whose journal shows no failed
     conversion: a `rewrite_failed` event, or a `rewrite` event that carries `step` with `ok: false` (a `rewrite`
     event without `step` is one try that may be repeated and is not a result). The final status
     (`conversion_complete`) corrects the mark either way. Opening another run starts from nothing
     (`resetRail`).
   - the chips of the result card (남은 오류, 막힌 항목, LLM 호출), the framework notice, the download buttons
     and the remediation block are unchanged.
5. **The admin page**: the result word of such a run is `converted` (instead of `incomplete`), shown as
   「변환 완료」 in the success colour; the 범위 column still says 「JDK·jakarta만」.
6. **The reports.** `SBMIG-RESULT`, the flags and every section about the errors that remain are unchanged.
   - Korean guide (`guide_ko.py`): the result cell reads 「✅ JDK 21, Jakarta 변환 완료 (변환 뒤 빌드는 실패)」 (a noun
     phrase like its sibling 「❌ 변환 실패 (빌드는 통과)」), without the trailing 「(최종 `mvn clean package`)」 of the other labels. The paragraph under the
     head table begins 「JDK 21과 Jakarta 변환을 마쳤습니다. <이름> 프레임워크 프로젝트는 변환까지가 이번 작업의
     범위입니다.」 and goes on with the build: without the fix loop 「변환 뒤 빌드는 통과하지 않았고 빌드 오류는
     자동으로 고치지 않았습니다.」 and the pointer to 「변환 뒤 남은 빌드 오류」 (or to the last build log when no
     error row is left); with the loop on 「빌드 오류를 자동으로 고쳐 보았지만 최종 빌드는 아직 통과하지 않습니다.」
     (「변환 뒤 빌드는 아직 통과하지 않습니다.」 when the loop made no attempt) and the pointer to 「해결하지 못한
     오류」 / 「진행하지 않은 항목」. Both end by naming who fixes what is left. A run the guide treats as stopped
     (the abort flag or the abort hand-over line) keeps 「❌ 실행 중단 …」.
   - English layout (`markdown.py`): `result_label` → `✅ CONVERTED (the build does not pass)` (order: NOT
     CONVERTED, BUILD SUCCESS, CONVERTED, NOT BUILDING); the head row is `| **Result** | ✅ CONVERTED (the build
     does not pass) |` without `(final `mvn clean package`)`. Nothing else of the layout moves.
   - Console summary of `sbmig run` (`writer.py`): `CONVERTED (the build does not pass)` in yellow in place of
     the red `NOT BUILDING`.
   A run whose conversion failed keeps 「❌ 변환 실패 (빌드는 통과)」 / 「❌ 빌드 실패」 (v2.1z-ay).

7. **What the label must not hide.** A conversion counts as done although OpenRewrite could not read some files
   (the recipes skip them without an error; flag `rewrite-parse-problems`). In a reduced-scope run those files
   stay unconverted in the delivered tree, so `_report_parse_problems` adds a hand-over line for each conversion step that names new files: `OpenRewrite
   could not read N source file(s) (a, b, …): the JDK 21 / Jakarta recipes skipped them, so they are not converted
   — check and convert them by hand` (the first five by name and `+N`; files an earlier step of the same run
   already named are left out, so an MSA plan does not repeat a list; the set of named files is per-run state).
   When the plugin gave no file names (the runner's placeholder `PARSE_PROBLEMS_UNKNOWN`), the line carries no
   count and points at that step's rewrite log instead; the placeholder is never counted as a file and never
   remembered as named. The Korean guide shows it in 「확인해 주세요」 with the lead 「변환되지 않은 파일」, after the pointers to
   the remaining errors: 「OpenRewrite가 읽지 못한 소스 파일이 N개 있습니다: …. 이 파일(들)에는 JDK 21, Jakarta
   변환이 적용되지 않았습니다. 직접 확인해 변환해 주세요.」 A full run gets no such line (its fix loop covers what
   the build finds). Three sentences were corrected because they said something untrue for a run without the fix
   loop or with a failed build:
   - the flag text of `rewrite-parse-problems` no longer says that the fix loop covered the errors: `… skipped
     them; what that leaves shows up in the build` (「…건너뛰었고, 그 때문에 생기는 오류는 빌드에서 드러납니다.」),
     for every run; the phase note of a run without the fix loop ends `the build shows what follows`;
   - the hand-over line `JSP javax references remain (…)` says `(the build passes, Tomcat 10+ fails at runtime)`
     only when the final build passed; otherwise `(Tomcat 10+ fails at runtime on them)`
     (「(Tomcat 10 이상에서 실행하면 실패합니다)」);
   - the hand-over line about version strings a recipe left unresolved (`the recipe run could not download Maven
     metadata …`) has a Korean rule now; it was shown in English in the Korean guide.

**Not covered.** The label says nothing about whether the project works: the converted code was not compiled
to the end. Files OpenRewrite could not parse, versions it left as literals and JSP files the rename step
could not change do not make the rule false (the PM's choice): each has a hand-over line that the Korean guide
shows in 「확인해 주세요」 (item 7), and a conversion that leaves such files is still shown as done. The
scope is named 「JDK 21, Jakarta」 also when the plan left the jakarta set out because the project has no Java EE
javax import. With the non-default `loop.always_final_verify: false`, an MSA run whose later service builds were
skipped still has a `final_build` and can satisfy the rule (the report stores no per-step build results). A framework run that was stopped, refused or cut short, whose conversion failed, or whose reactor
compiles none of the tree's sources is worded as before. The wording of the chained remediation after such a
build (「조치함 · 빌드 미확인」, v2.1z-aw) is not touched. `sbmig fleet` still counts the project as incomplete and
`--retry-failed` runs it again.

## v2.1z-cl — a framework project converted into a generated Spring Boot project: the converters

The second way to handle a project written on the in-house framework (the first is the reduced run of v2.1z-ak /
v2.1z-aw: the framework stays, JDK 21 and jakarta only). The conversion generates a new project (a parent pom and the
modules boot / domain / service / store) from the project's service XML, SQL files and web files; the framework is
gone from the result. The rules come from the company's guide to an earlier in-house tool and are kept, with the
framework's name as a token, in **`docs/FW-CONVERT.md`**, which is the contract of this area: input recognition,
output layout and names, the decision tables, hand-over items, what is not ported, open decisions, acceptance
scenarios, the module table.

**What exists.** `sbmig/fwconvert/`: `model.py` (records), `rules.py` + `knowledge/fwconvert/rules.yml` (the
vocabulary; `load_rules(name, overlay)` fills in the framework's name and merges company values), `naming.py`,
`identity.py`, `inventory.py`, `sqlmap.py`, `servicexml.py`, `resources.py`, `javagen.py`, `frontcopy.py`,
`apimap.py`, `ajax.py`. Functions with unit tests (`tests/fwconvert/`); `tests/fwconvert/test_chain.py` runs them
one after another on a small project and checks the borders (a store's statement exists in a mapper, a rewritten
script's address is served by a controller, a JSP's page link reaches a page controller).

**What does not exist yet.** No run calls these modules: no config key, CLI command, plan step, scope value, report
section or page control. The skeleton and poms, the datasource step, carried-over classes, the gate, the pipeline and
the real-Maven run are the next increment (`docs/FW-CONVERT.md` section 9). The deployed behaviour for framework
projects is unchanged.

**Rules of this area.** The framework's name and company class names never enter the repository: rules carry
`{Fw}` / `{fw}`, company values arrive through the overlay, tests use `Example` / `com.example`. No version from
the guide is written into a generated pom: the target line, the parent chain and the repository decide. Generators
return records and write nothing (the front-end copy and the ajax rewrite work on files). Project files are
untrusted input: a bad file is an error entry or a hand-over item, never an exception.
