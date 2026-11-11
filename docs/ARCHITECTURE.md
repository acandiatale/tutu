# sbmig Architecture

Goal: take an as-is Maven (or Ant) project, as a zip or a directory, and produce a JDK 21 + Spring Boot 4.1.0 project in the
company layout that **builds** (`mvn clean package` → BUILD SUCCESS). OpenRewrite runs the mechanical migration once; every
build error that remains is fixed by an LLM-written Python fix script that the knowledge base stores per error case and reuses
on later projects. A run never ends without a final build and a report that lists what is still broken. Runtime behaviour and
tests are out of scope (every build runs with `-Dmaven.test.skip=true`).

Contracts (models, config keys, journal events) live in `docs/INTERFACES.md`; the Korean pack `agent-doc/` describes the same
code node by node (`agent-doc/02-flow-and-nodes.md`, `agent-doc/04-cases-scripts-kb.md`, `agent-doc/06-company-rules-restructure.md`).

## 1. Pipeline

```
intake ─► analyze ─► plan ─► transform ─► restructure ─► structure review ─► verify (+ script fix loop) ─► finalize ─► remediation
```

| Node | What runs | Stops the run when |
|---|---|---|
| intake | copy the input into `out/<run id>/project/`, `git init`, commit `sbmig: as-is snapshot`; preflight (Maven runs, `mvn -v` reports Java `build.require_java_major`, company parents set); build-tool check (Ant → pom.xml synthesized by `sbmig/analyze/ant.py`); stray pom copies ignored, a root aggregator generated for a tree without a root pom, duplicate reactor listings removed, deploy transport extensions removed; as-is effective dependencyManagement captured to `logs/effective-depmgmt.json` | `mvn-not-found`, `jdk-not-21`, `unsupported-build-tool:<tool>`, `ear-without-modules` (exit 3) |
| analyze | pom graph (`sbmig/analyze/pom_parser.py`, no Maven) + regex source scan → `ProjectProfile` (case type, versions, libraries, MSA services, bundled jars, externalized settings, warnings); EOL advisories matched (guide only) | Oracle ADF with two or more evidence families (`intake.adf_policy: stop`), no root pom (exit 3); a hollow reactor goes on but is never a success |
| plan | rules only (`sbmig/plan/planner.py`) → ordered `PlanStep`s: `transform.rewrite`, `restructure.apply` / `restructure.boot_pom` / `restructure.pom_only` / `restructure.msa_pom`, `restructure.review`, `verify.build` (MSA: per service) | – |
| transform | one OpenRewrite run of every planned recipe on the as-is classpath, no build (`sbmig/rewrite/runner.py`) | never (the build shows what is left) |
| restructure | the company layout and every pom cleanup, no LLM (§8) | never; a failed step flags `restructure-failed` and the run is not a success |
| structure review | one LLM call checks the restructured poms against the company rules; critical findings are fixed by edits, warnings go to the report (`sbmig/review/reviewer.py`) | never |
| verify | `mvn clean package -Dmaven.test.skip=true`; on failure the script fix loop (§3), then the final build again | never |
| finalize | success verdict, advisories verified on the final tree, Handover, error list, KB records, `report.md` / `report.json`, then `changes.patch` / `result.zip` | – |
| remediation | only when `remediation.after_migration` is on and the final build succeeded: the vulnerability-remediation graph (§11) | – |

Why the recipes run **before** the restructure: OpenRewrite needs type attribution against the as-is classpath (javax types,
Boot 2/3 classes). Swapping the parent first would resolve a Boot 4.1 classpath and leave old types unattributed, so many
`ChangeType`-style recipes would silently do nothing.

Recipe sets per case (`openrewrite.recipes`; every id must be in `knowledge/recipes/catalog.yml`, details in `docs/RECIPES.md`):
`jdk21` for everyone; `jakarta` for `spring_legacy`, `servlet_tomcat` and `pure_java` with Java EE javax imports;
`servlet_jakarta_10`; `spring_framework_7` for `spring_legacy`, plus `spring_security_7` when it uses spring-security;
`spring_batch_6` / `hibernate_7` when used; `spring_boot_4` (`UpgradeSpringBoot_4_0` + `SpringBootProperties_4_1`) for `spring_boot`.

### Workspace and run directory

`Workspace` (`sbmig/core/workspace.py`) owns the run: the project copy is a git repository on branch `sbmig/<run id>` and every
change is a commit (`rewrite: …`, `restructure: …`, `review: …`, `fix(script:<case>:v<N> …)`), so any attempt can be reverted and
`changes.patch` is the diff from the as-is snapshot. Files are read and written in their detected encoding. The run directory:

```
out/<run id>/
  project/            the migrated tree (git)
  logs/               NNN-<label>.log per Maven / OpenRewrite call, effective-depmgmt*.json, bundled-jars.json, review-input.md
  journal.jsonl       one event per line
  sbmig.log           DEBUG log, a [stage] tag per line
  errors.jsonl / errors.md   the run's error list (§9)
  profile.json  plan.json  report.json  report.md  changes.patch  result.zip
```

## 2. Package map

| Package | Role |
|---|---|
| `sbmig/core/` | shared contracts: `models.py`, `config.py` (`load_config`: `config/default.yml` → `--config` files → `SBMIG_*` env), `interfaces.py` (plugin protocols), `workspace.py` |
| `sbmig/analyze/` | intake helpers (build tool, Ant → pom, aggregator, stray poms, bundled jars, ADF, hollow reactor) and the profile |
| `sbmig/plan/` | the planner and the recipe catalog (`sbmig recipes …`) |
| `sbmig/rewrite/` | the single OpenRewrite run, composed `rewrite.yml`, recipe BOM 3.37.0 |
| `sbmig/restructure/` | company layout, lxml pom editor, bundled-jar wiring, resource filtering, JSP javax → jakarta, web.xml registrations, Spring XML context |
| `sbmig/review/` | structure review (inputs, prompt, `<review>` parser) |
| `sbmig/build/` | Maven runner, log parser, signatures, effective pom, parent-chain facts, repository lookups, jakarta discovery, dependency tree |
| `sbmig/triage/` | clustering per case, decisions, the script ladder, blocked classification |
| `sbmig/loop/` | `Orchestrator` (the run) and `BuildFixLoop` (the script fix loop), blocked registry, budgets |
| `sbmig/fix/` | `LLMFixer` (`propose_script`), prompt context, answer parser, LLM clients incl. the company gateway |
| `sbmig/llm/` | tier-1 adapter seam (`LLMAdapterLike`) |
| `sbmig/script/` | the fix-script runtime (§7.6) |
| `sbmig/errors/`, `sbmig/kb/` | run error list, case store, file KB, `sbmig kb …` |
| `sbmig/advisory/` | EOL library advisories (guide only, `docs/ADVISORIES.md`) |
| `sbmig/report/` | `report.md` / `report.json`, Handover, `SBMIG-RESULT` line, Korean wording |
| `sbmig/graph/` | LangGraph engine (§7.7) |
| `sbmig/fleet/` | sequential multi-project runs (§7.5) |
| `sbmig/web/` | FastAPI web UI, run manager, Kubernetes Job dispatch, Main / Worker contract (§10) |
| `sbmig/remediations/` | vulnerability remediation (§11) |

Entry points: `sbmig/cli.py` (`sbmig run`, `serve`, `analyze`, `plan`, `inventory`, `build`, `parse-log`, `rewrite`, `llm-probe`,
`repo-probe`, sub-apps `kb`, `recipes`, `fleet`, `advisories`) and `sbmig.api.migrate(...)`.

## 3. Script fix loop

`BuildFixLoop` (`sbmig/loop/fixloop.py`) runs inside the verify node on `loop.phases.verify` (goal `package`, `max_iterations` 50).
The failed first verify build is round 1.

```
for round in 1..max_iterations:
    build                                  # always a real mvn run
    score the previous round's attempts    # rebuild decides; regressions reverted, KB credited
    BUILD SUCCESS → stop
    errors → cases (L2), root causes first; cases whose files / module pom this round already touched wait
    per case: pre-try (zero LLM) → reuse stored script → augment (LLM) → new (LLM) → BLOCKED
    commit each attempt
stop also when no case is left to try, only blocked cases remain, or the time budget is spent
```

Hard rules: the orchestrator owns the loop and the LLM never decides whether to build; an attempt counts only by what the next
build shows; every attempt is a commit; after the loop the final `mvn clean package` always runs again. Budgets:
`loop.max_llm_calls`, `loop.max_total_minutes`, the per-case caps in `script.*`. The ladder, the caps, the scoring outcomes and
the blocked policies are in `docs/ERROR-HANDLING.md`.

## 4. Triage

`sbmig/triage/cluster.py` groups the errors of a build into cases (one per L2 signature, `case:<case_id>`) and orders them:
PARENT_RESOLUTION < POM_INVALID < DEPENDENCY_RESOLUTION < PLUGIN_RESOLUTION < ENFORCER < REWRITE < COMPILE (reactor order) <
TEST_COMPILE < PLUGIN_EXECUTION < TEST_FAILURE; inside one priority the root cause first (`package … does not exist` before
`cannot find symbol` before type mismatches before override errors). At most `loop.max_clusters_per_iteration` cases per round.
`sbmig/triage/decide.py` gives each case `SCRIPT`, `BLOCKED`, `SKIP` (wait for the rebuild) or `ESCALATE`;
`sbmig/triage/scripts.py` picks the ladder step; `sbmig/triage/blocked.py` classifies cases the project cannot fix.

## 5. Signatures

`sbmig/build/signatures.py` fills two signatures on every `BuildError` the log parser makes:

* **L1 (specific)** `kind|normalized message|symbol|location` — project-owned packages and types masked as `*`.
* **L2 (generic)** `kind|message class|key` — e.g. `compile|cannot_find_symbol|WebSecurityConfigurerAdapter`,
  `compile|package_does_not_exist|javax.xml.bind`, `dependency_resolution|g:a`, `pom_invalid|missing_version`,
  `plugin_execution|war:war|<normalized cause>`. A coarse `build` / `other` L2 is refined from the first meaningful javac line.
* Paths, line numbers, URLs, versions and numbers are stripped; reactor artifactIds are masked as `<module>`.
* **case id** = `sha1(L2)[:10]`. The same L2 on another project is the same case, which is what makes a stored script reusable.
  Changing a signature rule gives existing errors new ids (stored cases become orphans; `sbmig kb reindex` does not re-sign).

## 6. LLM seam and fix protocol

The fix prompt (`sbmig/fix/prompts/script_system_v1.md`, `script_user_v1.md.j2`, packed by `sbmig/fix/context.py`) carries the
case's errors, the files around them, pom excerpts, layout facts after the restructure, migration notes chosen from
`knowledge/notes/`, advisory hints, repository facts (jakarta builds found, bundled jars) and the earlier attempts on the case.
The answer (`sbmig/fix/script_protocol.py`) has `<analysis>`, optional `<edits>` (direct literal edits) and a `<script>` with
`def fix(p)`; `<no_script>` + `<edits>` is a project-only edit that is never stored; `<blocked>` declines with a reason; the
model may ask for more files first (bounded context rounds).

Two replaceable units (`docs/ADAPTERS.md`): tier 1 `LLMClientLike` / `LLMAdapterLike` (`llm.provider: plugin` + `llm.plugin`,
`complete(LLMRequest) -> LLMResponse`, recording and budgets still apply) and tier 2 `FixerLike.propose_script` (`fix.plugin`, an
in-house agent that returns a `ScriptProposal`). Built-in providers: `company` (default; `sbmig/fix/company.py`, URL / key / model
from the environment or `.env`, `llm.gateway: legacy | agent`), `anthropic`, `openai_compatible`, `fake` (replay of
`llm-NNN.json` files for offline fixtures). `llm.escalation_model` takes over on the third ask of a case or on a stall;
`llm.case_thread` (off by default) continues a gateway session per case. Details: `agent-doc/05-llm-gateway-and-prompts.md`.

## 7. Knowledge base (file-based)

```
kb-v2/                                  kb.dir
  errors.jsonl                          every run's error entries (the source of truth; appended at the end of a run)
  cases.md                              human table: case, projects, seen / resolved, script versions, pin, last result
  cases/<case_id>/case.json             CaseInfo: l2, name, projects, counts, scripts[] (status, successes, failures, lineage, runs)
  cases/<case_id>/fix_v<N>.py           stored fix scripts (same code stored once)
  cases/<case_id>/errors/<run>-<id>.json   one readable file per error entry
  libraries.yml, jar_names.yml          company tables edited without a redeploy
  .lock                                 cross-process write lock
```

`CaseStore` (`sbmig/errors/cases.py`) and `FileKnowledgeBase` (`sbmig/kb/store.py`) own it. A script is stored `unverified`,
becomes `proven` when it fixes the case on a project other than the one that made it, and `retired` by a person
(`sbmig kb cases retire`). Reuse picks the newest proven version, else the newest unverified one; non-idempotent scripts are
never reused. Every write runs under `<kb>/.lock` (re-read + atomic replace inside), so the web server, a fleet, several build
pods and `sbmig kb …` share one folder; secret values are masked before anything is stored (`sbmig/kb/redact.py`,
`knowledge/kb/redact.yml`). No database: files are the source of truth. Commands: `agent-doc/04-cases-scripts-kb.md` §8.

## 7.5 Fleet mode (one agent, sequential)

`sbmig fleet run <manifest>` (`sbmig/fleet`) migrates projects strictly one after another with one shared KB, so project N+1
reuses the scripts stored by 1..N. `fleet/state.json` is rewritten after every project (resumable); `summary.{md,csv,json}` and
`learning.md` (the KB cases these projects met) are regenerated each time. The fleet also runs the infrastructure preflight, a
breaker that defers projects ending with `infra:<kind>`, a disk check and `fleet.cleanup`. See `docs/FLEET.md`.

## 7.6 Script runtime

Fix scripts run only through `sbmig/script/` (`ScriptRunner`): a static check (`check.py`: one module-level `def fix(p)`, an import
whitelist, no `open` / `exec` / `eval` / dunder access, `script.max_code_chars`), then `python -I -X utf8` in a child process in
a temporary directory outside the project, its own process group, `script.timeout_seconds` with a process-tree kill, secrets
removed from the environment. The helper API `p` (`api.py`: `errors`, `files`, `read`, `write`, `replace`, `sub`, `create`,
`delete`, `move`, `pom(path)`, `log`) refuses paths outside the project, `.git/` and `target/`. With `script.idempotency_check` a
script that changes the tree again on a second run is marked non-idempotent. `<edits>` are compiled into a literal `def fix(p)`
and run the same way (`edits.py`). This guards against mistakes; it is not a sandbox against a hostile author.

## 7.7 Graph engine (LangGraph)

The company deploys agents on LangGraph, so a `StateGraph` drives the run (`sbmig/graph/engine.py`, `loop.engine: graph`, the
default). Nodes `intake → analyze → plan → phase (once per phase group) → finalize → remediation → END`; each node calls one
`Orchestrator.step_*` method, so `loop.engine: inline` (`Orchestrator._run`, no LangGraph) produces the same report, flags,
exit code and journal (asserted by `tests/graph/test_engine.py`).

- The graph state holds small JSON values (`run_id`, remaining phase groups, `aborted`, `stopped`, a summary); the `RunReport`,
  the plan and the `BuildFixLoop` stay on the orchestrator.
- Nodes never catch. Exceptions leave the graph and reach the orchestrator's run guard, which finalizes the report for both
  engines; after an internal error the planned final build still runs (`_final_build_after_abort`), and an aborted run or a
  missing final build is never a success.
- `run_graph` (CLI) and `stream_graph` (web UI: `(node, state update)` pairs); each node start writes journal `graph_node`.
- `--stop-after intake|analyze|plan|transform|restructure|verify` and `--dry-run` behave the same in both engines.

## 8. Restructure (company layout)

`Restructurer.apply(ws, profile, cfg, mode=…)` (`sbmig/restructure/restructurer.py`), no LLM. `{name}` = `layout.project_name` or
the root artifactId; module paths come from `layout.module_dir` / `boot_module` / `comp_module` / `front_module`. Company rules
in detail: `agent-doc/06-company-rules-restructure.md`.

**Modes.**

* `layout` (pure Java, servlet, Spring Framework; Boot only with `layout.boot_as_is: layout`): the root becomes an aggregator on
  `artifacts.parent` declaring the mandatory dependencies (`artifacts.mandatory_target: root`); `{name}-back` (every as-is source,
  parent `artifacts.comp_parent`), `{name}-boot` (generated `Application`, Boot configuration, parent `artifacts.boot_parent`,
  spring-boot-maven-plugin) and, only when web resources exist, `{name}-front` (static files under `src/main/resources/static`,
  JSPs under `src/main/resources/META-INF/resources/WEB-INF/jsp`). JSPs make the boot module an executable WAR
  (`layout.jsp_boot_packaging: war`: `SpringBootServletInitializer`, Tomcat + Jasper, JSTL managed by the Boot BOM). Spring XML
  bean files go to `legacy-config/` (`@ImportResource`), `web.xml` and other `WEB-INF` files to `asis-webinf/`.
  Multi-module as-is projects are merged into the back module (`layout.multi_module_policy: merge`; collisions
  `layout.merge_collision: first-wins`, differing copies saved under `<run dir>/merge-conflicts/`, `META-INF/services` /
  `spring.factories` / `*.imports` merged as a union); `wrap` keeps the modules and adds a boot module.
* `boot_pom` (Spring Boot as-is, `layout.boot_as_is: in-place`, also MSA services): directories stay, poms only — root →
  `artifacts.parent`, modules matching `layout.boot_module_pattern` whose pom is a Boot app → `boot_parent`, every other module →
  `comp_parent`, explicit coordinates and `<relativePath/>`; a single-module Boot app is wrapped into `{name}-boot`.
* `msa_pom`: per service root the same parents / mandatory / optional artifacts, structure untouched; library roots →
  `comp_parent`, the top aggregator → `artifacts.parent`.
* `optional_bump`: only the optional in-house artifact versions.

Role parents make the root a pure aggregator the modules do not inherit, so what used to flow through inheritance (dependency
and plugin management, repositories, referenced properties) is copied into the module poms. Old role-parent versions are moved
to the configured ones. Every parent set is listed in `RestructureResult.parents_set`. A second run changes nothing.

**Optional artifacts.** An optional in-house artifact is aligned when the project already declares it; one with `when_present`
is added when a matching dependency is declared (`replaces: true` removes the matched declarations).

**Pom cleanup** (every mode, in this order): `nonFilteredFileExtensions` for binary files under filtered resources
(`resources_filtering.py`) → as-is managed versions pinned from `logs/effective-depmgmt.json` (`versions_pinned`, Handover ⚠
"carried over") → explicit versions the new parent chain manages dropped (`layout.drop_managed_versions: gte`;
`managed_versions_dropped`) → version properties that override the chain dropped (`layout.drop_bom_property_overrides`) →
orphan version properties dropped → bundled jars wired (`layout.wire_bundled_jars`, `bundled_jars.py`: known coordinates the
repository has → dependency, else copied to `<module>/lib/` with `system` scope; superseded / container-API / unreferenced jars
untouched) → JSP / tag files renamed javax → jakarta (`layout.jsp_jakarta_rename`). The managed versions come from
`help:effective-pom` of the restructured tree, or, when that capture fails, from the company parents' own poms read from the
repository (`sbmig/build/parent_chain.py`). Versions are never guessed.

**web.xml → `WebXmlRegistrations`** (`layout.web_xml_registrations: generate`): Boot never reads web.xml, so the restructure
parses it (`sbmig/analyze/webxml.py`) and generates one `ServletRegistrationBean` / `FilterRegistrationBean` /
`ServletListenerRegistrationBean` per entry plus an `ErrorPageRegistrar` in the boot module; entries Boot already provides
(`knowledge/config/web_xml_rules.yml`) become follow-ups, annotated classes stay with `@ServletComponentScan`. Flag
`web-xml-registered`.

**Spring XML contexts** (`layout.legacy_context`, `legacy_context.py` / `legacy_apply.py`, rules
`knowledge/config/legacy_context_rules.yml`) and **externalized settings** (`externalized.*`: keys the as-is code reads but no
packaged file defines get `key=__SET_ME__` lines in the boot module's `externalized.placeholder_file`, per service in MSA) are
also written here.

## 9. Outputs

* `report.md` — header table and `SBMIG-RESULT` line, then **Handover** (operator checks → Ant pom synthesis → versions
  replaced (⚠ carried over) → libraries needing attention → "From here on, your team"), then Error list, Fixes by case,
  Structure review, Bundled jars, Blocked, javax API jars, Manual follow-ups, Flags, What we changed, LLM usage and cost, Notes,
  Outputs (`sbmig/report/markdown.py`). `report.language: ko` for the company.
* `report.json` — the serialized `RunReport` (`sbmig/core/models.py`), always English.
* `errors.jsonl` / `errors.md` — every error of the run with every try (cause, solution, diff); `errors.jsonl` is appended to
  `<kb>/errors.jsonl` at the end.
* One ASCII line `SBMIG-RESULT run=… exit=… build=… unresolved=… blocked=… llm=… attn=… review=…` (`sbmig/report/result_line.py`);
  exit 0 success, 2 finished with blocked / unresolved / attention, 3 aborted.

## 10. Web app and Job mode

`sbmig serve` (uvicorn `sbmig.web.app:app`) serves one HTML page (`sbmig/web/static/index.html`, no CDN) and streams `status`,
`node`, `journal`, `log`, `report` events over SSE. The config files every upload runs with are the server's choice
(`sbmig serve --config` / `SBMIG_WEB_CONFIGS`). Stop takes effect between graph nodes. `RunManager` (`sbmig/web/runs.py`) has three
modes (`SBMIG_WEB_MODE`):

* `in-process` — the web process runs one migration at a time in a worker thread through `stream_graph`; further uploads queue.
* `k8s-job` — the web pod (**Main**) writes the upload to the shared volume and creates a Kubernetes Job from the template
  (`deploy/k8s/81-web-job.template.yaml`, ConfigMap `sbmig-job-template`); the Job pod (**Worker**, `python -m sbmig.web.worker`)
  runs `sbmig run` into the same run directory and Main tails its journal and log. `sbmig/web/k8s.py` is a requests-based client
  (in-cluster service account or `KUBECONFIG`).
* `local-job` — the same dispatch with local processes (dev PCs).

The Main / Worker file contract (`sbmig/web/contract.py`): only Main writes `<run dir>/control/` — `request.json` pins the zip
sha256, config digests and sbmig version at intake; `state.json` is the control state (queued → creating → created → running →
finished | stopped | lost | failed), written atomically with the intent recorded before the Job is created, plus `env_pins`
(fingerprints of the environment values the config reads). The Worker verifies the pins before running anything
(`SBMIG_JOB_ENV_PIN = refuse | warn | off`), writes the outputs and `job-status.json` for its attempt, and redacts secrets
from every line (`SBMIG_REDACT_OUTPUT`). Main owns only Jobs carrying both its `sbmig-owner` and run labels, adopts an existing Job
after a restart, and never reports an unverified result as success.

Slots: `SBMIG_JOB_MAX_PARALLEL` build Jobs at a time (FIFO, `_FifoTurns`); each gets a slot number (`SBMIG_JOB_SLOT`), and
slot k ≥ 2 uses its own Maven local repository `<base>-slot<k>`; the rest wait with their place in line. The KB stays one shared
folder under `<kb>/.lock`. Operator text: `deploy/README.md`; contract detail: `docs/INTERFACES.md` (sections v2.1z-ac … v2.1z-af);
`agent-doc/09-deploy-and-ops.md`.

## 11. Remediation (vulnerabilities)

`sbmig/remediations/` is a separate LangGraph (`graph.py`) that scans a project with the company's IVAS service, applies approved
Maven version-text fixes and managed-version overrides, rebuilds, rescans and delivers a report + `result.zip`. It runs in the
build pod as the migration graph's last node when `remediation.after_migration` is on and the migration ended BUILD SUCCESS
(`sbmig/remediations/chain.py`); its outcome never changes the migration's verdict (`SBMIG-RESULT … remediation=<status>`). The web
pod serves status, proposals and approvals under `/api/remediations/…`, and resumes an approved remediation in a new build pod.
Settings `remediation.*`. Contract: `docs/INTERFACES.md` section "remediations"; Korean: `agent-doc/11-remediations.md`.
