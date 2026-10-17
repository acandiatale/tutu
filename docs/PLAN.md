# Delivery plan (PM view)

## Scope
Build `sbmig`, an in-house CLI/library that migrates Maven projects to JDK 21 + Spring Boot 4.1.0 and the company
`module/{boot-jar,comp-jar}` layout, with: deterministic analysis & planning, OpenRewrite execution, a
triage-driven build-fix loop that calls an LLM only for residual errors, and a file-based knowledge base that
turns resolved cases into reusable recipes. Success = `mvn clean package` passes. Runtime correctness is out of scope.

Improvements over the as-is agent:
1. Recipe selection is rule-based from a structured profile (LLM optional as reviewer) → reproducible.
2. The fixer cannot "finish" without a build: the orchestrator runs `mvn` after every change and gates phases.
3. Triage separates resolution/plugin/compile/test-compile errors, batches fixes, detects stalls and regressions
   (git revert per attempt).
4. Every resolved case is persisted (file KB); mechanical ones become OpenRewrite recipes run on the next project.

## Workstreams (parallel, disjoint directories)

| WS | Owner dirs | Deliverable |
|---|---|---|
| A analyze+plan | `sbmig/analyze`, `sbmig/plan`, `tests/analyze`, `tests/plan` | pom graph parser, source scanner, classifier, library detection, rule planner |
| B restructure | `sbmig/restructure`, `tests/restructure` | lxml `PomEditor`, layout transform, Application templates, MSA pom-only mode |
| C rewrite+build | `sbmig/rewrite`, `sbmig/build`, `tests/rewrite`, `tests/build` | OpenRewrite runner (rewrite.yml composition), Maven runner, log parser, signatures |
| D fix+notes | `sbmig/fix`, `knowledge/notes`, `tests/fix` | LLM clients (openai-compatible/anthropic/fake), context packer, protocol parser, patch applier |
| E triage+loop+report+cli | `sbmig/triage`, `sbmig/loop`, `sbmig/report`, `sbmig/cli.py`, `tests/triage`, `tests/loop` | clustering, handlers, decision engine, orchestrator, report, CLI |
| F kb+fixtures | `sbmig/kb`, `fixtures/projects`, `scripts`, `tests/kb` | file KB, matcher, harvester, promotion, 6 fixture projects, e2e script |
| G integration | everything | wire, run unit + e2e on fixtures with real mvn/OpenRewrite + fake LLM, fix seams |

Contracts all WS code against: `sbmig/core/{models,config,interfaces,workspace}.py` (owned by PM).

## Acceptance criteria
* `pytest -m "not integration"` green; each WS ≥ 80% branch coverage on its own package.
* `sbmig analyze fixtures/projects/<each>` classifies all 6 fixtures correctly.
* `sbmig run fixtures/projects/boot2 --config config/artifacts.local-e2e.yml` reaches `BUILD SUCCESS` on the
  restructured project with the fake LLM (handlers + recipes only) or reports exactly which clusters escalated.
* A run never ends without a final `mvn clean package` attempt and a report listing unresolved errors.
* KB: after one run with ≥1 LLM fix, `kb/cases/` has the case and `sbmig kb list` shows it; a second run on a
  project with the same error signature consults it before calling the LLM.

## Risks / open items for the company environment
* Exact in-house parent/mandatory/optional coordinates and whether the parent manages Boot/Java versions.
* Nexus must proxy `org.openrewrite.*` artifacts (plugin 6.46.1, recipe BOM 3.38.0) and Boot 4.1.0.
* Projects that need JDK 8 to *build as-is* are never built as-is; we only build after `UpgradeToJava21`.
* Non-Maven (Ant/Gradle) projects are detected and rejected with a clear message.
* web.xml / JSP / XML bean wiring are compile-neutral; they are reported as manual follow-ups for runtime owners.
