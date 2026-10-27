# sbmig — JDK 21 / Spring Boot 4.1 migration agent

`sbmig` takes an as-is **Maven** project (zip or directory: pure Java, Spring 4/5, servlet / Tomcat, Spring Boot 2/3,
MSA), migrates it to JDK 21 + Spring Boot 4.1, restructures it into the company layout (in-house parent, 2 mandatory
dependencies, 8 optional bumps, `{name}-boot` / `-back` / `-front` modules) and stops only after a final
`mvn clean package` (tests skipped). Build errors are fixed by **LLM-written Python fix scripts** that are stored per
error case in a file-based knowledge base, so a later project with the same case runs the stored script first —
without an LLM call. The report starts with a **Handover** for the as-is owners (versions replaced, ⚠ pins carried over,
EOL libraries that still compile, properties to set, blocked items).

This branch is **v2** (`flow-v2`, docs/v2/PLAN-v2.md). The previous flow (handlers, KB recipes, SEARCH/REPLACE fixes) is
the git tag **`company-v1`**.

## The flow

```
intake      Maven only (Gradle / Ant / pom-less trees are rejected, exit 3)
analyze     case (pure_java | servlet_tomcat | spring_legacy | spring_boot | msa), libraries, advisories, properties
plan        the recipes of the one OpenRewrite run: UpgradeToJava21, JavaxMigrationToJakarta, Boot 4.0 + 4.1 properties
transform   one OpenRewrite run — no build in between
restructure company layout (parent / mandatory / optional, module split or poms only for Boot / MSA)
review      the LLM checks poms, dependencies and modules; critical issues are fixed by its script and committed
verify      mvn clean package -Dmaven.test.skip=true; on failure the script fix loop, then the final build again
finalize    report.md / report.json (Handover first), KB records (errors, attempts, scripts, reviews)
```

Script fix loop, per error case (case = L2 signature, one script for every file that has it): **reuse** the best stored
version (no LLM) → **augment** the previous version (LLM, up to 3) → **new** (LLM) → BLOCKED → Handover. An attempt that
makes things worse is rolled back (git); every script version and its outcome is recorded.

## Business decisions (kept in config / knowledge, not in code)

* Scope = packaging + build passing. Runtime errors belong to the as-is owners; tests are not compiled.
* OpenRewrite only for Java 21, javax → jakarta and Spring Boot; everything else is fixed by LLM scripts.
* EOL / unmanaged libraries are never a blocker: advisories (`guide`) give the LLM hints and go into the Handover.
* One agent migrates ~500 projects strictly sequentially (`sbmig fleet`); the KB is file-based, no database.
* Real in-house coordinates live in `config/artifacts.yml` (from `config/artifacts.example.yml`); LLM keys only in
  the environment / `.env` (`LLM_API_KEY`), never in a file of the repo.

## Quick start

```bash
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"
cp config/artifacts.example.yml config/artifacts.yml   # in-house parent / role parents / mandatory ×2 / optional ×8
cp .env.example .env                                    # LLM_BASE_URL / LLM_API_KEY / LLM_MODEL (git-ignored)
sbmig llm-probe                                         # one request to the gateway, prints request and reply
sbmig run <project dir|zip> --config config/artifacts.yml
```

Needs JDK 21 (`build.java_home` or `JAVA21_HOME`) and Maven. Fix scripts run in a separate process of sbmig's own
Python unless `script.python` names another one (it needs pydantic + lxml, docs/RUNBOOK.md).

## Commands

| command | what it does |
|---|---|
| `sbmig run <input>` | migrate one project → `out/<run id>/` (`report.md`, `report.json`, `result.zip`, `changes.patch`, `sbmig.log`); exit 0 ok, 2 blocked / unresolved, 3 aborted |
| `sbmig fleet run <manifest>` / `resume` / `status` | many projects, one after the other, one shared KB → `<out>/fleet/summary.md`, `learning.md` |
| `sbmig serve` | web UI (FastAPI on uvicorn, LangGraph engine): upload a zip, watch the run live, download the report |
| `sbmig analyze` / `plan` / `inventory` | read-only: project profile, migration plan, a pom-less Tomcat tree described for its owners |
| `sbmig kb scripts list\|show\|retire\|activate` | stored fix scripts per case (versions, successes, lineage) |
| `sbmig kb errors …` / `cases …` / `reviews` / `stats` / `validate` / `export` / `import` | error cases, case catalog, structure reviews, KB upkeep |
| `sbmig recipes …` / `advisories …` | the OpenRewrite recipe catalog, the library advisories |
| `sbmig llm-probe` / `build` / `parse-log` / `clean` | gateway check, Maven debugging aids, disk cleanup |

In-house LLM gateway or agent platform through the adapter seams (docs/ADAPTERS.md): `--llm-plugin pkg.mod:attr`
(tier 1, chat completion) or `--fix-plugin pkg.mod:attr` (tier 2, a fixer with `propose_script`;
example `examples/adapters/agent_fixer.py`).

## Local demos (offline: public artifacts + replayed LLM answers)

```bash
export JAVA21_HOME=/path/to/jdk-21
scripts/e2e.sh                       # every fixture (fixtures/projects/README.md) → out/e2e/summary.md
scripts/e2e.sh --only boot3,msa      # a subset
scripts/demo-kb/run.sh               # boot3 twice, one KB: run 1 writes the fix script, run 2 reuses it (0 fix LLM calls)
scripts/fleet-demo.sh                # six fixtures as one fleet: later projects reuse the stored scripts
```

Tests: `python -m pytest -m "not integration"` (offline), `python -m pytest -m integration` (real Maven, JDK 21).

## Docs

| doc | content |
|---|---|
| `CLAUDE.md` | working notes: constraints, invariants, status per round |
| `docs/v2/PLAN-v2.md` | the v2 flow: decisions, script contract, structure review, KB v2 (Korean) |
| `docs/migration-agent/README.md` | the current design per flow / node with diagrams (Korean) + deck |
| `docs/INTERFACES.md` | contracts between packages, config keys (section "v2 — flow-v2") |
| `docs/ARCHITECTURE.md`, `ERROR-HANDLING.md`, `RECIPES.md`, `ADVISORIES.md`, `FLEET.md`, `ADAPTERS.md` | design areas |
| `docs/RUNBOOK.md` | installing and running in the company environment |
| `docs/handbook/` | the Korean operator handbook (+ pptx / xlsx) |
| `fixtures/projects/README.md` | the 32 test projects and their v2 outcomes |
