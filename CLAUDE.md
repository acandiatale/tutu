# sbmig — working notes for Claude (read this first)

In-house **JDK 21 + Spring Boot 4.1.0 migration agent** for company Maven projects. Python 3.10+, CLI `sbmig`.
Read in this order: `README.md` → `docs/PLAN.md` (one "Status — round N" table per round, newest at the bottom)
→ `docs/INTERFACES.md` (contracts) → the doc for the area you touch (`ARCHITECTURE`, `FLEET`, `ADAPTERS`,
`RECIPES`, `ADVISORIES`, `ERROR-HANDLING`, `RUNBOOK`).

## Why this exists
- The user takes this code into their company to test it there. Company source code is **not** reachable from here.
- The previous in-house agent failed for two reasons we must not repeat: the fix LLM "finished" without running
  the build, and recipe selection by a small LLM was weak. Hence: deterministic planning, build-gated phases.

## Company constraints (stated by the user; not derivable from the code)
- Four project cases: pure Java, Spring 4/5 + Boot 2/3, Tomcat servlet-only, MSA (several deployable services).
- Non-MSA target: root pom inherits **1 in-house parent**, add **2 mandatory** dependencies, **8 optional** in-house
  artifacts bumped only when already used; sources split into `module/boot-jar` + `module/comp-jar`.
- MSA: same parent / mandatory / optional per service, directory structure untouched (pom-only mode).
- Scope = packaging + build passing (`mvn clean package` → BUILD SUCCESS). Runtime errors belong to the as-is owners.
- Real in-house coordinates are unknown here → placeholders in `config/artifacts.example.yml`; public stand-ins in
  `config/artifacts.local-e2e.yml` for local e2e. Never invent "real" company coordinates.
- The company has its own LLM API gateway and its own agent-building framework. They plug in through the adapter
  seams (`llm.plugin` tier 1, `fix.plugin` tier 2, see `docs/ADAPTERS.md`), not through new HTTP clients.
- Operating model: **one agent migrates ~500 projects strictly sequentially**. Every fix is recorded
  (error-case symbol → fix label → recipe) so later projects benefit: `sbmig fleet`, `kb/ledger.jsonl`, `kb/symbols.json`.
- EOL / unmanaged libraries (springfox, hystrix, sleuth, old Spring Cloud trains, …): **no `block` policy, ever**
  (user: "정책상 빌드로 보지 않는건 없을거야" — 500 projects cannot all be perfect). Build as far as possible, then
  hand over a compressed summary: report **Handover** section first, advisory policies `guide` | `recipe` only.
- LLM keys come only from the environment (`LLM_API_KEY`). Never write a key into the repo, a config file, or a log.
  The user knowingly keeps using a free Anthropic test key; do not remind them to rotate it.

## Invariants (do not break)
1. **Build gate**: a phase is done only on BUILD SUCCESS; a run never ends without a final `mvn clean package`
   attempt and a report that lists unresolved errors.
2. Company specifics live in config / knowledge YAML, not in Python code.
3. The knowledge base is **file-based** (no database allowed).
4. Plugin seams stay stable: `LLMRequest` / `LLMResponse`, `FixerLike`, `sbmig.api.migrate(...)`.
5. `knowledge/recipes/catalog.yml` is the single source of recipe ids; `sbmig recipes check` and a snapshot test
   guard drift. Recipe BOM pinned to **3.37.0** (3.38.0 is inconsistent on Maven Central). rewrite-spring 6.37.1.
6. Test and demo runs write under `out/` only, never into the repo's `kb/` (local e2e KB is `out/e2e/kb`).

## Status — 2026-10-18, round 4 complete (`main` @ fe80828)
- Round 1: pipeline + 6 fixtures e2e. Round 2: blocked-cluster triage, 6 failure fixtures, real-LLM validation
  (fixes `claude-sonnet-5`, recipe work `claude-haiku-4-5`, `config/llm-anthropic.yml`). Round 3: adapter seams,
  recipe catalog, fleet mode + learning ledger + deterministic fix labels. Round 4: library advisories + Handover.
- 801 offline tests green; asserted fixture e2e **13/13** (`scripts/e2e.sh`); fleet demo 5/5 with 1 LLM call total.
- Per-round tables with what was verified: `docs/PLAN.md`.

## Known follow-ups (candidates for the next round; the user decides)
- `method-rename` fix label is not compiled to `ChangeMethodName` yet; label heuristics are deliberately simple
  (whitespace-only edits → `generic-rewrite`).
- `ADVISOR_CATALOG` is small by design (verified ids only); extend it with the company recipe inventory.
- Catalog `rewrite-java-dependencies` `provided_by` link is an e2e-backed assumption, not a jar check.
- hystrix / ribbon / zuul have no OpenRewrite recipe → `guide` only (Resilience4j / LoadBalancer / Gateway).
- Items to validate in the company environment: `docs/RUNBOOK.md` §0, `docs/ARCHITECTURE.md` §7.5.

## How to work here
- **Working mode the user asked for**: Claude plans and acts as PM in the main session. Write shared contracts
  first (`sbmig/core/{models,config,interfaces}.py`, docs), then fan implementation out to parallel `model: opus`
  subagents on **disjoint directories** with explicit briefs. Subagents never commit. Integration, test runs and
  commits happen in the main session. Per round: append a "Status — round N" table to `docs/PLAN.md`, update
  `docs/INTERFACES.md` on contract changes, add a fixture + `expected.json` for every new scenario.
- Setup: `uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"` (plain `pip install -e ".[dev]"` works too).
- Fast check (Python only, ~75 s): `python -m pytest -q -m "not integration"` → 801 passed, 22 deselected.
  `ruff check sbmig tests` has ~1,000 pre-existing style findings (mostly `UP045`); lint is not a gate here,
  so fix only what you touch and never run a repo-wide `ruff --fix` inside a feature round.
- e2e needs **JDK 21** (`JAVA21_HOME`), Maven and network to Maven Central: `scripts/e2e.sh` → `out/e2e/summary.md`
  (`--only boot2,msa` for a subset); KB loop `scripts/demo-kb/run.sh`; fleet `scripts/fleet-demo.sh`.
  Integration tests: `python -m pytest -q -m integration`.
- Real-LLM runs: `--config config/llm-anthropic.yml` with `LLM_API_KEY` from the environment. In a cloud session,
  set it as an environment variable of the cloud environment, never in the repo.
- Cloud sessions start from a fresh clone with no JDK / Maven installed: install Temurin 21 + Maven before e2e,
  or run the offline suite only. Commit and push to a `claude/...` branch; the user merges to `main` locally.
