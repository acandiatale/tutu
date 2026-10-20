# sbmig — JDK 21 / Spring Boot 4.1 migration agent

`sbmig` takes an as-is Maven project (zip or directory), runs OpenRewrite for everything mechanical, then drives a
**triage → fix → build** loop until `mvn` says `BUILD SUCCESS`, restructures the project into the company layout
(in-house parent, 2 mandatory dependencies, 8 optional bumps, `module/{comp-jar,boot-jar}`), and writes a report the
as-is owner can act on — starting with a **Handover** (versions replaced, ⚠ pins carried over, EOL / unmanaged
libraries such as springfox, sleuth, hystrix that still compile, and a numbered "from here on, your team" list;
`recipe`-policy advisories like springfox → springdoc are migrated by OpenRewrite, none ever fails the build). Every LLM fix that worked is stored in a file-based knowledge base and, when mechanical,
promoted to an OpenRewrite recipe for the next project.

```
intake ─► analyze ─► advisories ─► plan ─► JDK21 (rewrite + build-fix loop) ─► SPRING (rewrite + loop) ─► RESTRUCTURE (+ loop) ─► verify ─► handover + report + KB harvest
```

| Doc | Content |
|---|---|
| `docs/ARCHITECTURE.md` | pipeline, case → recipe matrix, loop algorithm, triage, signatures, LLM protocol, KB layout |
| `docs/RUNBOOK.md` | installing in the company environment, running, reading reports, KB workflow, tuning |
| `docs/PLAN.md` | delivery plan, workstreams, acceptance criteria, risks |
| `docs/INTERFACES.md` | contracts between packages + change log |
| `docs/ADAPTERS.md` | plugging in the in-house LLM gateway (`llm.plugin`) or an in-house coding agent (`fix.plugin`); Python embedding via `sbmig.api.migrate` |
| `docs/RECIPES.md` | how recipe lists are managed: `knowledge/recipes/catalog.yml` (single source), config sets, base recipes, reactive rules, KB recipes |
| `docs/FLEET.md` | one agent over hundreds of projects, sequentially: `sbmig fleet`, resume, the learning ledger (`kb/ledger.jsonl`), symbol → fix labels |
| `docs/ADVISORIES.md` | library advisories: EOL / no-longer-managed libraries that still compile (`knowledge/libraries/advisories.yml`), `guide` / `recipe` policies, company overrides, `sbmig advisories` |
| `docs/ERROR-HANDLING.md` | error-case catalog: what is fixed, what is blocked and reported, loop safety, library advisories + carried-over pins |

## Quick start
```bash
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"
cp config/artifacts.example.yml config/artifacts.yml        # in-house parent / mandatory ×2 / optional ×8
export LLM_BASE_URL=https://llm.internal/v1 LLM_API_KEY=... LLM_MODEL=<model>
# in-house gateway that only accepts a plain chat POST ({model, need_orgin, messages}): use the profile
#   export LLM_BASE_URL=http://<gateway>/<chat endpoint> LLM_API_KEY=...   # http or https, used verbatim
#   sbmig llm-probe --config config/llm-company.yml      # raw response + extracted reply
#   sbmig run <project> --config config/artifacts.yml --config config/llm-company.yml

sbmig analyze  path/to/project.zip --config config/artifacts.yml      # case type, versions, libs, optional artifacts in use, library advisories
sbmig advisories list --config config/artifacts.yml                   # EOL / unmanaged libraries we flag (guide | recipe), `show <id>`, `check`
sbmig plan     path/to/project.zip --config config/artifacts.yml      # ordered steps + recipes (no changes made)
sbmig run      path/to/project.zip --config config/artifacts.yml --out out/   # full migration; exit 0 = final build passed
sbmig kb list --status candidate && sbmig kb promote <id>             # review harvested recipes
sbmig recipes list --kb && sbmig recipes check                        # every known recipe + where it is used
sbmig fleet run projects.yml --config config/artifacts.yml --out out/fleet   # 500 projects, one after another, resumable
sbmig kb learning                                                     # what was learned, what still needs the LLM, what blocks most projects
```

Web UI (FastAPI app served by uvicorn): `sbmig serve --port 8765` → http://127.0.0.1:8765 (or
`SBMIG_WEB_OUT=out/web uvicorn sbmig.web.app:app --port 8765`) — upload a project zip, tick the config files, watch
the LangGraph nodes (intake → analyze → plan → phase × N → finalize), the journal (rewrite / builds / LLM fixes) and
the log live (SSE), download `report.md` and `result.zip`. Runs execute in-process (one worker thread each) under
`out/web/<run id>/`, each with its own `sbmig.log`.
In-house LLM gateway / agent platform instead of our HTTP clients (see `docs/ADAPTERS.md`):
```bash
sbmig run project.zip --config config/artifacts.yml --llm-plugin mypkg.adapter:make_adapter     # tier 1: chat completion
sbmig run project.zip --config config/artifacts.yml --fix-plugin  mypkg.agent:make_fixer        # tier 2: whole fixer = your agent
```
Local end-to-end without the company Nexus/LLM (public artifacts + fake LLM):
```bash
export JAVA21_HOME=/path/to/jdk-21
scripts/e2e.sh                       # all fixtures → out/e2e/summary.md
scripts/e2e.sh --only boot2,msa      # subset
scripts/demo-kb/run.sh               # KB learning loop: run 1 LLM fix → harvested → run 2 fixed by the KB recipe, 0 LLM calls
```

## Project cases handled
| Case | Detection | Spring phase recipes | Restructure |
|---|---|---|---|
| `pure_java` | sources, no Spring/servlet | jakarta (if `javax.*` EE imports), JUnit 4→5 | layout + `spring-boot-starter`, generated `Application` |
| `servlet_tomcat` | `HttpServlet`/web.xml/war, no Spring | javax→jakarta + servlet-api swap | layout + `webmvc` starter, `@ServletComponentScan`, web.xml follow-up |
| `spring_legacy` | Spring 4/5 without Boot | jakarta + Spring Framework 7 (+ Security 7) | layout + `@ImportResource` for XML bean files |
| `spring_boot` | Boot 2.x/3.x | `UpgradeSpringBoot_4_0` + `SpringBootProperties_4_1` + 4.1 version pin | layout; existing `@SpringBootApplication` moves to boot-jar |
| `msa` | several deployable services | per service by its own sub-case | pom-only: parent + mandatory + optional, directories untouched |

## Layout
```
sbmig/core        models, config, workspace (git snapshots), interfaces   ← contracts
sbmig/analyze     pom graph + source scan + classifier → ProjectProfile
sbmig/plan        rule-based planner → MigrationPlan, pre-emptive handler ids
sbmig/rewrite     OpenRewrite runner (rewrite.yml composition, BOM version resolution)
sbmig/build       Maven runner, log parser (javac/dependency/plugin/enforcer…), error signatures
sbmig/triage      clustering, deterministic handlers, recipe rules, decision engine
sbmig/fix         LLM clients (openai-compatible / anthropic / fake), context packer, patch protocol + applier
sbmig/loop        orchestrator + build-fix loop (stall, regression revert, budgets, test-compile fallback)
sbmig/restructure PomEditor (lxml, format-preserving) + layout transform + MSA pom mode
sbmig/kb          file knowledge base: cases, index, recipe candidates/active, harvest, CLI
sbmig/report      report.md / report.json / console summary
knowledge/notes   migration notes fed to the LLM (JDK 21, jakarta, Boot 2→3→4→4.1, Framework 7, Security 7…)
knowledge/recipes project-owned declarative OpenRewrite recipes
sbmig/advisory    library advisories: match → policy → verify on the final tree → handover
knowledge/libraries advisories.yml (EOL / unmanaged library catalog)
fixtures/projects as-is sample projects (one per case + MSA + failure / advisory scenarios) with expected.json
```

## Tests
```bash
python -m pytest -q -m "not integration"      # ~520 offline tests
python -m pytest -q -m integration            # real mvn + OpenRewrite on tiny projects (needs JDK 21, network)
```
