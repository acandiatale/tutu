# sbmig — JDK 21 / Spring Boot 4.1 migration agent

`sbmig` takes an as-is **Maven** project (zip or directory: pure Java, Spring 4/5, servlet / Tomcat, Spring Boot 2/3,
MSA; Ant projects are refused with the notice "Maven 변환 후 신청 부탁드립니다." unless `intake.ant_to_maven` is
switched on), migrates it to JDK 21 + Spring Boot 4.1, restructures it into
the company layout (in-house parent, 2 mandatory dependencies, optional in-house artifacts, `{name}-boot` / `-back` /
`-front` modules) and stops only after a final `mvn clean package` (tests skipped). Build errors are fixed by
**LLM-written Python fix scripts** that are stored per error case in a file-based knowledge base, so a later project with
the same case runs the stored script first — without an LLM call. With `report.language: ko` (the company setting)
`report.md` is a Korean guide for the person who asked for the migration (`sbmig/report/guide_ko.py`): what was converted
(Java / Boot / parent, dependency versions with ⚠ pins carried over), 「확인해 주세요」 (EOL libraries, the hand-over items),
the code changes (one net diff per file the fix loop or the review changed, `sbmig/report/changes.py`), open errors, blocked
items, then reference blocks. `report.language: en` keeps the diagnostic layout with the **Handover** first. Every error with
every try and diff is in `errors.md` / `errors.jsonl`.

## The flow

```
intake       Maven only (Ant: refused with `intake.ant_message`, exit 3 — pom synthesis behind `intake.ant_to_maven`;
             Gradle / trees with neither pom.xml nor build.xml: rejected, exit 3; a ZIP over `intake.max_unpacked_mb` /
             `max_entry_mb` / `max_compression_ratio`: refused before anything is unpacked, exit 3);
             root aggregator generated for sibling poms, stray pom copies ignored, preflight (Maven, JDK 21)
analyze      case (pure_java | servlet_tomcat | spring_legacy | spring_boot | msa), libraries, advisories, bundled jars,
             ADF stop (exit 3), hollow reactor (exit 2), framework project (scope jdk-jakarta: no restructure / review / fix loop)
plan         the recipes of the one OpenRewrite run (Java 21, javax → jakarta, Spring Framework 7 / Security 7, Boot 4.0 + 4.1)
transform    one OpenRewrite run — no build in between
restructure  company layout (parent / mandatory / optional, module split, also for a single-module Boot app,
             or poms only for several-module Boot / MSA), versions carried
             over from the as-is effective pom, bundled jars wired, JSP javax → jakarta
review       the LLM checks poms, dependencies and modules; critical issues are fixed by its edits and committed
verify       mvn clean package -Dmaven.test.skip=true; on failure the script fix loop, then the final build again
finalize     report.md (ko: guide for the requester) / report.json, errors.md / errors.jsonl, KB records (kb-v2/)
remediation  when switched on, after BUILD SUCCESS (a framework run without the fix loop: after a failed build too): IVAS vulnerability scan, version-text fixes, rescan
```

Script fix loop, per error case (case = L2 signature, one script for every file that has it): zero-LLM **pre-tries**
(missing version, binary resource filtering, jakarta library swap, bundled jar) → **reuse** the best stored script (no
LLM) → **augment** this run's last script (LLM, ≤ 15 per case: `script.max_augments_per_case`) → a fresh **new** script
(LLM) → BLOCKED after 20 LLM asks per case (`script.max_llm_tries_per_case`) or a decline (`needs_owner`). Every attempt
is committed and scored by a rebuild; what did not help is rolled back (git), and when the loop ends a case's last
attempt that did not help is rolled back before the final build; every script version and its outcome is recorded in
`kb-v2/cases/<case_id>/`. A repository that does not answer is not "not found": such a case is blocked
`repository_unreachable` without an LLM call.

The remediation step (vulnerable library versions, IVAS) is a separate graph chained after a successful migration; it
never changes the migration's verdict. Details: `agent-doc/11-remediations.md`.

A run has one of two **flows**: `full` (the default: the migration above, then the chained remediation) or
`remediation` (`sbmig run <zip|dir> --flow remediation`, web `POST /api/runs?name=…&project=<project name>&flow=remediation`, the page's 취약점 해결
choice) — for a project migrated earlier and checked again: no migration step, no LLM call, no KB access; the upload is
built as it is, scanned by IVAS, remediated and rescanned under `<run dir>/remediation/`. `SBMIG-RESULT … flow=remediation
… remediation=<STATUS>`; exit 0 = SUCCEEDED, 2 = any other saved status, 3 = not started (refused at intake, or
`remediation:not-ready` when IVAS values / Maven readiness are missing).

**Framework projects.** A project that depends on an in-house framework artifact (`framework.artifacts` in the company
yml; empty = no check) runs in the reduced scope `jdk-jakarta`: the analyzer looks for the artifact in the poms'
dependencies, in what a parent pom outside the project brings, in bundled jars and, when none of these has it, with one
`dependency:tree`; the plan then holds only the recipe sets of `framework.recipe_sets` (default Java 21 + jakarta), the
JSP javax → jakarta rename and ONE build: build errors are not fixed (no pre-try, no stored script, no LLM call) but
reported, and the converted tree is delivered (a failed build ends with exit 2; a failed conversion too, even when the
unconverted tree builds: `exit=2 build=ok … attn=rewrite-failed`). The chained remediation follows whether the
build passed or failed (`remediation.after_failed_build: framework`, default; `never` = only after a passing build). After a
failed build the candidate build may fail only with the errors the project's build already had; when it changed versions,
no build confirmed them, so it ends `INCOMPLETE` (`baseline_build_failed`; the page says 조치함 · 빌드 미확인); with nothing
changed, or a candidate that builds, it ends as an ordinary remediation. The Korean report lists the remaining errors under 「변환 뒤 남은 빌드 오류」. No restructure, no structure review; the project keeps its parent pom, its layout and the framework version.
`framework.fix_loop: true` brings the fix loop back (it rolls back an attempt that touches the framework or a parent).
The framework's display name (`framework.name`, else derived from the artifact name: `example-fw-core` → `Example`)
goes into the sentence the page and the report show: `<name> 프레임워크는 JDK 21, Jakarta만 적용합니다.`
`SBMIG-RESULT … scope=jdk-jakarta`. Details: `agent-doc/13-framework-projects.md`.

## Business decisions (kept in config / knowledge, not in code)

* Scope = packaging + build passing. Runtime errors belong to the as-is owners; tests are not compiled.
* OpenRewrite only for Java 21, javax → jakarta and Spring (Boot / Framework / Security); everything else is fixed by LLM scripts.
* EOL / unmanaged libraries are never a blocker: advisories (`guide`) give the LLM hints and go into the report's hand-over items (「확인해 주세요」).
* Versions are never guessed: they come from the as-is build, the company parent chain or the repository.
* One agent migrates ~500 projects (`sbmig fleet`, or the web UI's build Jobs); the KB is file-based, no database.
* Real in-house coordinates live in `config/artifacts.yml` (from `config/artifacts.example.yml`); LLM keys only in
  the environment / `.env` (`LLM_API_KEY`), never in a file of the repo.

## Quick start

```bash
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"
cp config/artifacts.example.yml config/artifacts.yml   # in-house parent / role parents / mandatory / optional
cp .env.example .env                                    # LLM_BASE_URL / LLM_API_KEY / LLM_MODEL (git-ignored)
sbmig llm-probe                                         # one request to the gateway, prints request and reply
sbmig run <project dir|zip> --config config/artifacts.yml
```

Needs JDK 21 (`build.java_home` or `JAVA21_HOME`) and Maven. Fix scripts run in a separate process of sbmig's own
Python unless `script.python` names another one (it needs pydantic + lxml, `docs/RUNBOOK.md`).

## Commands

| command | what it does |
|---|---|
| `sbmig run <input>` | migrate one project → `out/<run id>/` (`report.md`, `report.json`, `result.zip`, `changes.patch`, `codediff/`, `sbmig.log`); exit 0 ok, 2 blocked / unresolved, 3 aborted. `--flow remediation`: no migration, only the vulnerability remediation of the project as uploaded (below) |
| `sbmig fleet run <manifest>` / `resume` / `status` | many projects, one after the other, one shared KB → `<out>/fleet/summary.md` |
| `sbmig serve` | web UI (FastAPI on uvicorn, LangGraph engine): upload a zip, choose 전체 진행 or 취약점 해결, watch the run live, read the report rendered on the page and download it (`migration-report.md`; the run folder keeps `report.md`), read the code changes file by file (코드 변경) |
| `sbmig analyze` / `plan` / `inventory` | read-only: project profile, migration plan, a pom-less tree described for its owners |
| `sbmig kb cases list\|show\|retire\|activate\|pin\|unpin` | stored fix scripts per case (versions, successes, lineage) |
| `sbmig kb errors …` / `stats` / `validate` / `reindex` / `export` / `import` | error list, KB upkeep |
| `sbmig recipes …` / `advisories …` | the OpenRewrite recipe catalog, the library advisories |
| `sbmig llm-probe` / `repo-probe` / `build` / `parse-log` / `clean` | gateway check, repository lookup, Maven debugging aids, disk cleanup (manual; project copies, uploads and LLM records of finished runs only) |
| `sbmig version` | the build identity `<version>+<12 hex>` (a digest of the code, `config/*.yml` and `knowledge/`) |
| `sbmig remediation-resume` | continue a remediation that stopped (upload confirmation, time budget, stop; used by the build Job) |

In-house LLM gateway or agent platform through the adapter seams (`docs/ADAPTERS.md`): `--llm-plugin pkg.mod:attr`
(tier 1, chat completion) or `--fix-plugin pkg.mod:attr` (tier 2, a fixer with `propose_script`;
example `examples/adapters/agent_fixer.py`).

## Web UI and Kubernetes

`sbmig serve` runs migrations in-process (one at a time). In the cluster the web pod dispatches one build **Job** per
upload (`SBMIG_WEB_MODE=k8s-job`), up to `SBMIG_JOB_MAX_PARALLEL` at a time, the rest wait in upload order. Requests from
another web site are refused (403), an upload is refused with 507 when the output volume has less than
`fleet.min_free_gb` free, and a build pod that runs another image than the web pod refuses the run (restart the web pod
after pushing an image). Image, manifests and the operator text: `deploy/README.md`.

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
| `CLAUDE.md` | working notes: constraints, invariants, current status |
| `docs/v2/PLAN-v2.md` | the business request, the decisions, the step table (Korean) |
| `docs/migration-agent/README.md` | the design per flow / node with diagrams (Korean) + `migration-agent.pptx` |
| `docs/INTERFACES.md` | contracts between packages, config keys (section "v2 — flow-v2") |
| `docs/PLAN.md` | one status table per round |
| `docs/ARCHITECTURE.md`, `ERROR-HANDLING.md`, `RECIPES.md`, `ADVISORIES.md`, `FLEET.md`, `ADAPTERS.md` | design areas |
| `docs/RUNBOOK.md` | installing and running in the company environment |
| `docs/kb-map/` | the KB data map for the business team (`index.html` opens offline) |
| `agent-doc/` | Korean document pack for in-house LLMs (diagnose runs, change the code) |
| `deploy/README.md` | container image, Kubernetes manifests, Job mode |
| `fixtures/projects/README.md` | the test projects and their outcomes |
