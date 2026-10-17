# sbmig — JDK 21 / Spring Boot 4.1 migration agent

`sbmig` takes an as-is Maven project (zip or directory), runs OpenRewrite for everything mechanical, then drives a
**triage → fix → build** loop until `mvn` says `BUILD SUCCESS`, restructures the project into the company layout
(in-house parent, 2 mandatory dependencies, 8 optional bumps, `module/{comp-jar,boot-jar}`), and writes a report the
as-is owner can act on. Every LLM fix that worked is stored in a file-based knowledge base and, when mechanical,
promoted to an OpenRewrite recipe for the next project.

```
intake ─► analyze ─► plan ─► JDK21 (rewrite + build-fix loop) ─► SPRING (rewrite + loop) ─► RESTRUCTURE (+ loop) ─► verify ─► report + KB harvest
```

| Doc | Content |
|---|---|
| `docs/ARCHITECTURE.md` | pipeline, case → recipe matrix, loop algorithm, triage, signatures, LLM protocol, KB layout |
| `docs/RUNBOOK.md` | installing in the company environment, running, reading reports, KB workflow, tuning |
| `docs/PLAN.md` | delivery plan, workstreams, acceptance criteria, risks |
| `docs/INTERFACES.md` | contracts between packages + change log |

## Quick start
```bash
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"
cp config/artifacts.example.yml config/artifacts.yml        # in-house parent / mandatory ×2 / optional ×8
export LLM_BASE_URL=https://llm.internal/v1 LLM_API_KEY=... LLM_MODEL=<model>

sbmig analyze  path/to/project.zip --config config/artifacts.yml      # case type, versions, libs, optional artifacts in use
sbmig plan     path/to/project.zip --config config/artifacts.yml      # ordered steps + recipes (no changes made)
sbmig run      path/to/project.zip --config config/artifacts.yml --out out/   # full migration; exit 0 = final build passed
sbmig kb list --status candidate && sbmig kb promote <id>             # review harvested recipes
```
Local end-to-end without the company Nexus/LLM (public artifacts + fake LLM):
```bash
export JAVA21_HOME=/path/to/jdk-21
scripts/e2e.sh                       # all fixtures → out/e2e/summary.md
scripts/e2e.sh --only boot2,msa      # subset
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
fixtures/projects six as-is sample projects (one per case + MSA) with expected.json
```

## Tests
```bash
python -m pytest -q -m "not integration"      # ~520 offline tests
python -m pytest -q -m integration            # real mvn + OpenRewrite on tiny projects (needs JDK 21, network)
```
