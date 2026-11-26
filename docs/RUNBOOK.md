# Runbook — operating sbmig in the company environment

## 0. Prerequisites and what to validate on a new host

| Item | Requirement | Check |
|---|---|---|
| Python | ≥ 3.10 (+ `uv` or `pip`); `script.python` (default: the interpreter running sbmig) must have pydantic and lxml | `python --version` |
| JDK | **21** (`build.java_home` or `--java-home`). The as-is project is never built on its old JDK | the run preflight compares `mvn -v` with `build.require_java_major` (21); a mismatch aborts with exit 3, flag `jdk-not-21` |
| Maven | 3.9.x; `build.settings_file` = a settings.xml with a **catch-all mirror** to the Nexus group (§0.1); in the cluster the file is the plain file `/data/.m2/settings.xml` on the data volume, copied once from ConfigMap `sbmig-config` (mounted in the web pod at `/etc/sbmig/settings.xml`): `kubectl -n agent exec deploy/sbmig -- sh -c "mkdir -p /data/.m2 && cp /etc/sbmig/settings.xml /data/.m2/settings.xml"` (`deploy/README.md` §3). A set but missing or empty file stops the run at intake, before any Maven call (exit 3, flag `settings-not-found`) | `mvn -v`; `sbmig repo-probe` |
| Maven HTTP blocker | Maven ≥ 3.8.1 refuses `http://` repositories through `maven-default-http-blocker` in `<maven home>/conf/settings.xml` (`mvn -v` prints the Maven home). If a company repository is `http://`, the user settings must redefine that mirror id (`<mirrorOf>none</mirrorOf>`), exempt the repository (`,!<repository id>` in that mirror's `mirrorOf`) or mirror the repository with `<blocked>false</blocked>`. A settings file that redefines the id as a real mirror (a repository URL, not blocked) answers every repository its `mirrorOf` covers from that one URL: exempt the repositories that live elsewhere (snapshots, public), or their artifacts are "not found" | the run's preflight WARNING `Maven's HTTP blocker covers …`; a blocked run ends `attn=infra:http-blocked,aborted:http-blocked` with the fix as the first item of the report's 「확인해 주세요」 |
| Nexus | proxies Maven Central incl. `org.openrewrite.*` (rewrite-maven-plugin 6.46.1, `rewrite-recipe-bom` **3.37.0** → rewrite-migrate-java 3.42.0, rewrite-spring 6.37.0, …), `org.springframework.boot:*:4.1.0`, the jakarta artifacts, the in-house parents | `sbmig recipes check --jars -c config/artifacts.yml` |
| LLM gateway | `.env` with `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` (agent gateway: + `LLM_GATEWAY=agent`, `LLM_ASSET_ID`, `LLM_TOKEN_URL`, `LLM_APP_TOKEN`) — see [ADAPTERS.md](ADAPTERS.md) | `sbmig llm-probe` → last line `LLM-PROBE ok …` |
| Company config | `config/artifacts.yml` from `config/artifacts.example.yml`: `base-parent`, `boot-jar` / `comp-jar` role parents, the 2 mandatory and 8 optional in-house artifacts | `sbmig analyze <project> -c config/artifacts.yml` |
| git | any | per-attempt commits / rollbacks / `changes.patch` |

Validate in this order on the company PC: `sbmig llm-probe` → `sbmig repo-probe <g:a:v> --project <dir>` →
`sbmig analyze` → one `sbmig run` on a small project → read `SBMIG-RESULT` and the report's 「확인해 주세요」.

### 0.1 Repositories declared inside the project poms

Legacy poms declare `<repositories>` of their own (an old Nexus host, Central, Spring milestones); on an air-gapped
host every one costs a resolution failure or a timeout. Copy `config/settings.nexus.example.xml`, set the group URL
and point `build.settings_file` at it: its `<mirrorOf>external:*</mirrorOf>` rewrites every remote repository —
the poms' included — to the group, and Maven's default `http://` blocker is overridden for it. sbmig passes `-s` to
every Maven call, and with it `-gs <maven home>/conf/settings.xml` (`build.ignore_project_settings`, default true; the
home comes from the preflight's `mvn -v`, else from the installation beside `build.mvn`; not found → no `-gs`), so
settings a project names in its `.mvn/maven.config` (`-s` or `-gs`) are never used. Beyond that, Maven reads no part
of a project's `.mvn/` folder (`build.ignore_project_mvn`, default true): every Maven call gets `MAVEN_BASEDIR=<an empty
folder of the sbmig process, in the system temp folder>`, so a JDK 8 `jvm.config` (`-XX:MaxPermSize`), a Maven 3.8
style `maven.config` (several options on one line) or an `extensions.xml` cannot stop or change the build; the `-D` /
`-P` options of the `maven.config` Maven would have found are passed on the command line (before sbmig's own `-D`, so
sbmig's win; never `maven.repo.local`, `maven.multiModuleProjectDirectory`, `maven.test.skip`, `skipTests`), and
`-Dmaven.multiModuleProjectDirectory=<project root>` gives the poms their root back. The run notes it once (flag
`project-mvn-ignored`, one Handover line, journal `project_mvn`; with the switch off: flag
`project-maven-settings-ignored` for settings named there) and the folder is delivered byte for byte. The pom URLs are still read for the jakarta-library lookup (`sbmig repo-probe` lists them unless
the mirror covers them).

If a build says an artifact "was cached from a remote repository ID that is unavailable in current build context",
add `-llr` to `build.extra_args` or use a fresh `build.local_repo`.

### 0.2 Setup

```bash
cd migration-agent
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"
cp config/artifacts.example.yml config/artifacts.yml      # fill in the company coordinates
cp .env.example .env                                     # LLM_BASE_URL / LLM_API_KEY / LLM_MODEL (git-ignored)
sbmig llm-probe --prompt "안녕"                            # exact request, raw response, extracted reply
sbmig repo-probe com.example.grid8:grid8:1.0.6 --project <project>   # repositories asked, versions, jakarta verdicts
sbmig run <project> --config config/artifacts.yml
```

```powershell
cd D:\migration-agent
uv venv
Set-ExecutionPolicy -Scope Process Bypass        # only if Activate.ps1 is blocked
.venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"
sbmig llm-probe --prompt "안녕"                   # last line: LLM-PROBE ok … / LLM-PROBE FAIL <kind> …
sbmig repo-probe com.example.grid8:grid8:1.0.6 --project D:\asis\order-api   # last line: REPO-PROBE ok|none …
sbmig run D:\asis\order-api.zip --config config\artifacts.yml --out out
Get-Content out\<run id>\sbmig.log -Encoding UTF8 -Tail 30   # Windows PowerShell 5.1 needs -Encoding UTF8
```

- **`.env`**: read from `SBMIG_ENV_FILE`, then every `.env` from the package folder and from the cwd up to the git
  root, nearest first (so a repo that keeps sbmig under `src/migration-agent/` can keep `.env` at its root); shell
  variables win. UTF-8 (with or without BOM), UTF-16 or cp949; an edited file is read again by the next run. Keys
  never go into a yml.
- **Gateway URL** is used verbatim. If `requests.post` works by hand but sbmig fails, compare with
  `sbmig llm-probe --bare` (same payload, plain `requests.post`) and `--payload-extra '{}' --no-system --model <m>`.
  An OpenAI / Azure-shaped answer is read without settings; `LLM_RESPONSE_PATH` is only for other shapes.
- **Windows**: `build.mvn` is resolved through PATH / `MAVEN_HOME`; otherwise set it to `…/bin/mvn.cmd`. Git and
  Maven output is decoded as UTF-8, every Maven call gets `-Dstdout.encoding=UTF-8`, console output uses
  `errors="replace"`, and the console summary prints `ok` / `FAIL`.
- Every command except `llm-probe`, `clean` and the `kb` / `recipes` / `advisories` groups needs
  `--config config/artifacts.yml` (the company artifacts section has no default).

### 0.3 Framework projects (`framework.*`)

Projects built on the in-house framework run in the reduced scope `jdk-jakarta`: the Java 21 + jakarta recipes, then ONE
build; build errors are not fixed (no pre-try, no stored script, no LLM call) but reported, and the converted tree is
delivered (`framework.fix_loop: false`, default). `framework.fix_loop: true` brings the fix loop back (pre-tries without
the company parent, stored scripts, LLM; an attempt that changes a `<parent>` or the framework's declarations / version
is rolled back). The chained remediation follows whether the build passed or failed (below). No restructure, no structure review, parent pom and
framework version kept. The page and the report name the framework: `<name> 프레임워크는 JDK 21, Jakarta만 적용합니다.`
(contract: `docs/INTERFACES.md` "v2.1z-aw — framework projects: conversion only, and the framework is named").
A run whose conversion is done reads as done even when the build after it fails: 「완료」 on the page (title 「JDK 21, Jakarta
변환을 마쳤습니다. 변환 뒤 빌드는 통과하지 않았습니다」, the build step in the warning colour, not a red cross), 「변환 완료」 on the
admin page, 「✅ JDK 21, Jakarta 변환 완료 (변환 뒤 빌드는 실패)」 in the report's head table. The result line
still says `exit=2 build=FAIL`; a run whose conversion failed (`rewrite-failed`) or that was stopped is not shown as done
(contract: `docs/INTERFACES.md` "v2.1z-ck").
Details: `agent-doc/13-framework-projects.md`. To validate in the company environment:

| Item | What to do / look for |
|---|---|
| Switch it on | `framework.artifacts` in the company `config/artifacts.yml` with the real coordinates (placeholder: `["com.example.fw:example-fw-core"]`; a bare artifactId matches any groupId, matching is case-sensitive). In the cluster the file is baked into the image: rebuild and push the image (`deploy/README.md` §7) |
| Framework name | `framework.name` (default `""`): empty = derived from the artifact name — the artifact found in the project, else the configured pattern (a pattern with `*` is skipped); trailing role words (`core`, `framework`, `fw`, `starter`, `parent`, `bom`, `boot`, `common`, `base`, `api`) are cut and the first letter upper-cased: `example-fw-core` → `Example`. Derived name right → nothing to do; wrong → one line `name: "<name>"` under `framework:` in the company `config/artifacts.yml`, then rebuild and push the image. The real name is a company name: never in this repository |
| Fix loop | `framework.fix_loop` (default `false`): one build, errors reported, no LLM call by the migration (`llm=0` unless the chained remediation's fix loop called the LLM). Set `true` only when the company decides that framework projects get the fix loop too (version facts from the project's own chain, the framework guard, journal `framework_guard`) |
| Recipe sets | `framework.recipe_sets` (default `[jdk21, jakarta]`). The jakarta set moves the servlet API to the Jakarta EE 9 line (servlet 5.0); for projects that will run on Tomcat 10.1+ add `servlet_jakarta_10` — the company decides |
| First run on a real framework project | `SBMIG-RESULT … scope=jdk-jakarta`; `sbmig.log` INFO `분석: 사내 프레임워크 프로젝트 (…) - JDK와 jakarta만 적용`; journal `framework_project` (`name`, `fix_loop`, `evidence`, `unchecked`), `plan` with `"scope": "jdk-jakarta"` and `name` / `fix_loop`, (MS949 / EUC-KR sources) `source_encodings` before the rewrite and `scopes_restored` after it, commits in the order (`transform: pom encodings corrected` when a pom's bytes did not match its declaration) → `transform: source encodings written` → `rewrite: transform.rewrite` → `transform: as-is provided scopes restored` → `transform: JSP javax → jakarta`, notes `source encoding written: <pom>: project.build.sourceEncoding=MS949 (…)` — an encoding the kept parent sets differently stays and becomes a hand-over item (「확인해 주세요」); `report.md` block `## <name> 프레임워크 프로젝트` right under the head table, starting with the sentence above in bold, the two corrections listed under 「코드 수정 내역」 → 자동 변환 ("sbmig가 변환 단계에서 직접 고친 pom") and in `errors.md` as `seen: transform` entries; the page shows a highlighted notice with the sentence (run card and result card; the upload card shows it whenever the server's default configuration names framework artifacts with `framework.scope: jdk-jakarta`; `GET /api/configs` → `framework {configured, name, fix_loop, remediation_always}`), followed by 「취약점 조치는 빌드 결과와 관계없이 이어서 합니다.」 when `remediation_always` is true (`remediation.after_migration` on, `remediation.after_failed_build: framework`; the fix loop setting does not matter); `GET /api/runs/{id}` → `remediation` carries `baseline_tolerated` and `reasons`. The 「취약점 조치」 block shows the ZIP badge 빌드 미확인 and its note only for a delivery with `build_tolerated` and applied changes; a change set that added a build error keeps 빌드 실패본. The stage rail shows 재구성 / 구조 검토 as 하지 않음 |
| Build failed | `scope=jdk-jakarta`, exit 2, `build=FAIL`. With the default `framework.fix_loop: false` (`llm=0`) nothing was tried: the Korean report lists the errors under 「변환 뒤 남은 빌드 오류」 (「확인해 주세요」 starts with a pointer to it; the en report has them as open entries of the "Error list"). With the fix loop switched on the loop could not fix every error: the Korean report lists what is left under 「해결하지 못한 오류」 like any run, and the migration report's remediation section says the remediation was chained although the build still failed after the automatic build fix. Either way the errors are for the project's and the framework's owners; `result.zip` is the converted tree. The chained remediation still runs (`remediation.after_failed_build: framework`, default). When it changed versions it ends `remediation=INCOMPLETE` with first reason `baseline_build_failed` (no build confirmed them; with no other reason the page shows 조치함 · 빌드 미확인, the run list adds nothing after the migration's outcome): the owners fix the build errors, then build once with the changed versions. With nothing changed, or when the candidate build passed, it ends as an ordinary remediation (SUCCEEDED or its own reasons). A candidate build that adds an error is `FAILED` (`candidate_build_failed`, `baseline_build_failed`); a baseline build that timed out is not tolerated. `never` = no remediation after a failed build |
| Not recognised | journal `framework_check` with `unchecked`: `parent: no as-is effective pom` (the intake capture failed, so inheritance from a parent pom was not checked), `transitive: <why>` (`dependency:tree` failed), nothing = the pattern does not match the real groupId / artifactId. Both `parent:` and `transitive:` present = the check could not look: flag `framework-unchecked`, WARNING `분석: 사내 프레임워크 여부를 확인하지 못함 (…)`, Handover line "framework check could not run (the parent pom and the dependency tree could not be read): if this project uses the in-house framework it must not be migrated in full — confirm before using the result"; the run goes on in full scope, the exit code is unchanged |
| Cost of the transitive check | when the poms, the parent capture and the bundled jars show nothing, every project pays one `mvn dependency:tree -Dincludes=…` on the as-is tree at analyze (one per service root for an MSA without a covering aggregator); `framework.transitive: off` saves it |
| A framework still on javax (`framework.fix_loop: true` only) | BLOCKED `external_javax_api` without an LLM call, the suggestion names the framework artifact and says its owner must provide a jakarta build — expected, not an sbmig failure |
| An unresolvable parent | exit 3, `aborted:parent-not-found`: a framework project keeps its own parent, so Nexus must serve it (or fix the settings / mirror) |

## 1. Running a migration

```bash
sbmig run path/to/project.zip --config config/artifacts.yml --out out/
#  --stop-after intake|analyze|plan|transform|restructure|verify   stop early (still writes a report)
#  --dry-run                       compute and journal decisions, apply nothing
#  --java-home <jdk21> --settings <settings.xml> --llm-provider <p> --fix-plugin pkg.mod:attr
#  --flow full|remediation         full (default) = migration + chained remediation; remediation = §6, no migration
```

The flow: intake → analyze → plan → one OpenRewrite run → company restructure → LLM structure review →
`mvn clean package -Dmaven.test.skip=true` → on failure the script fix loop (stored script reuse → augment → new;
every attempt rebuilt, scored, rolled back if it did not help; when the loop ends, each case's last applied attempt
that scored `no_change` / `inconclusive` is rolled back too, so nothing that did not help is delivered) → final build →
report. Tests are never compiled (`build.skip_test_compile: true`, flag `tests-not-compiled`). A restructure step that
fails resets the tree to the commit before the step (journal `restructure_reset`) and the run goes on with the as-is
layout (flag `restructure-failed`, exit 2).

Inputs: a Maven tree (a missing root pom gets a generated aggregator). An uploaded ZIP is checked against
`intake.max_unpacked_mb` (4096), `intake.max_entry_mb` (1024) and `intake.max_compression_ratio` (200, per entry from
1 MiB unpacked; 0 = no limit), first from its directory and again while unpacking; a ZIP over a limit is refused
before anything is written (`run aborted: ZIP refused: …`, journal `intake_refused`, exit 3, no Maven, no LLM). An Ant tree (`build.xml`, no pom) is refused
at intake with the notice "Maven 변환 후 신청 부탁드립니다." (`intake.ant_message`; exit 3, flag
`unsupported-build-tool:ant`); the pom synthesis comes back with `intake.ant_to_maven: true` (section "Ant 프로젝트" below).
Gradle and trees with neither pom nor build.xml are rejected (exit 3, flag `unsupported-build-tool:<tool>`); for
the latter `sbmig inventory <dir> --out out/inventory/<name>` writes a read-only description for the owners. Oracle
ADF projects stop at analyze (exit 3, flag `adf-project`).

**Exit codes**: `0` final `mvn clean package` passed and nothing is blocked; `2` finished with blocked and / or
unresolved errors (or `hollow-reactor`, `restructure-failed`); `3` aborted (no Maven, Maven not on Java 21, unsupported
input, a ZIP over the unpacking limits, ADF, in-house parent not resolvable, a stop request — one that arrives after
every planned final build has a result is ignored); `1` crash. Library advisories never change the exit code. The last console line
(also in `sbmig.log`, ASCII, retypeable):

```text
SBMIG-RESULT run=<id> exit=<n> build=<ok|FAIL> unresolved=<n> blocked=<n> open=<n> resolved=<n> llm=<calls> attn=<codes|-> [review=<v>] [major=<n>] [remediation=<STATUS|skipped>]
```

`llm` = the migration's LLM calls plus the chained (or remediation-only) remediation's fix-loop calls.
`attn` lists why a human has to look, most important first (`infra:<kind>`, `crash`, `aborted[:<reason>]`,
`owner-decision`, `blocked:<reason>`, `unresolved:<n>`, `major-upgrade`, `hollow-reactor`, …; full list in
[FLEET.md](FLEET.md)). `-` = nothing to look at.

Outputs in `out/<run id>/`:

| file | content |
|---|---|
| `report.md` / `report.json` | the report: with `report.language: ko` a Korean guide for the requester (below, "What the owners read"); `report.json` is the same data in English |
| `errors.md` / `errors.jsonl` | one entry per error with every try (cause, solution, edits, diff, result) |
| `journal.jsonl` | machine-readable event log (decisions, attempts, scores, blocks, LLM calls) |
| `sbmig.log` | DEBUG log, one stage tag per line (`[fix r3 <case_id>]`) |
| `logs/NNN-<label>.log` | every Maven / OpenRewrite call — the real error messages |
| `project/` | the migrated working copy (git branch `sbmig/<run id>`, one commit per step / attempt) |
| `changes.patch`, `result.zip` | full diff vs as-is, the migrated project |
| `codediff/` | what the page's 「코드 변경」 block shows: each file of the migrated tree at its final path against the as-is file it came from, with the old place of a moved file; only paths the last commit has (untracked leftovers of the final build are not "added", journal `code_diff.untracked_left_out`); a run that changed no file writes no record (journal `code_diff_skipped`, "the run changed no file") and a record of an earlier attempt is removed (`report.code_diff: false` leaves it out) |

`report.language: ko` writes `report.md` as the Korean guide, `en` as the diagnostic layout. LLM requests / responses go to `llm.record_dir` (`LLM_RECORD_DIR`)
as `llm-NNNN.json`, without keys.

## 2. Reading a run

1. **`SBMIG-RESULT`**: `exit`, `build`, `attn`. `infra:` means the environment (LLM, Maven, repository, file lock),
   not the code.
2. **`report.md`**: 「확인해 주세요」 (what the owning team must know), 「해결하지 못한 오류」, 「진행하지 않은 항목」
   (reason and suggestion per item), 참고 → 진단용 기록 (the sentence saying why the loop ended, e.g. `max iterations (50)
   reached with N error(s) left`). With `en`: Handover, Notes, "Blocked — needs human / library action", "Fixes by case".
3. **`errors.md`**: every error (resolved ones too) with its tries; reverted attempts stay listed here with their diff.
4. **`journal.jsonl`**: the case's `decision` (ladder step, KB lookup), `attempt_scored`, `revert`, `stall` /
   `oscillation`, `library_hints`, `declined` / `decline_rejected`.
5. **`logs/`**: the highest-numbered `NNN-build-verify*.log` — the `[ERROR]` lines, javac's `symbol:` / `location:`.
6. **`project/`**: `git log --oneline` pairs attempt commits (`fix(script:<case_id>:v<N> …)`) with rollbacks.
7. **LLM records**: was a fact missing from the prompt, did the LLM ignore it, or did it decline?

Symptoms an operator meets and where to look:

| You see | Look at / do |
|---|---|
| `Could not transfer artifact <g>:<a>:pom:<v> from/to <repository> (…)` with 502 / a time-out for an in-house library (`Failed to read artifact descriptor for <g>:<a>:jar:<v>`); flag `pomless-library` | The repository has the library's jar but no pom, and one repository (typically a proxy of Maven Central in a closed network) answers the pom request with a transfer error, which stops Maven (when every repository says "not found", Maven only warns and uses the jar). sbmig handles it by itself (`build.pomless_stub`, default on; at most `build.pomless_stub_max` = 20 per run): when the jar is obtainable and the pom is obtainable from no repository, it writes a minimal pom into the pod's local repository and runs the same Maven command once more — no project file changes, no LLM call, also in a framework project. Journal `pomless_stub {ga, version, path, jar_from, created}` (`created: false` = the file was already there), one INFO line per stub (`maven: <g:a:v> has a jar but no pom in the repository — …`), flag `pomless-library`, one hand-over line, in the Korean report 「저장소에 jar만 있고 pom이 없는 라이브러리가 있습니다: `<g:a:v>`. 빌드 서버의 Maven 로컬 저장소에 최소한의 pom을 만들어 두고 빌드했습니다. 이 pom에는 라이브러리가 쓰는 다른 라이브러리가 적혀 있지 않아 Maven은 그 목록을 알지 못합니다. 이 라이브러리가 동작하는 데 필요한 라이브러리가 있으면 프로젝트의 pom.xml에 직접 추가해 주세요. 근본적인 해결은 라이브러리 담당자가 저장소에 pom을 올리는 것입니다.」. The proof asks the pom with GET and the jar with HEAD (401 / 403 = denied: the pom counts as not obtained, the jar as not obtainable). The retry order in `MavenRunner.run`: HTTP blocker (no retry) → Windows file lock → stub pom → `-U` (skipped once a stub was written); reruns are logged as `<label>-pomless<k>`. A later run that uses a stub written earlier says it too (journal `pomless_stub_used`, same flag and line; up to 300 coordinates checked, a library reached only transitively can be missed without an unfiltered dependency tree). The remediation-only flow records a stub in the journal and the log only. When sbmig declines, the journal has `pomless_check {ga, version, reason}`: `pom-available` = the pom exists somewhere, so this is a real transfer problem (fix the repository / network, then rerun); `jar-unavailable` = the jar cannot be obtained either (the repository is down or the library does not exist: check the coordinates and the repository); `no-repository` = no repository to ask, or the local repository path could not be resolved (check `build.settings_file` / `build.repository_url` / `build.local_repo`); `bad-coordinate` = not a plain coordinate; `cap` = the per-run limit was reached (raise `build.pomless_stub_max` or fix the libraries); `off` = the switch is off. Parent poms, imported BOMs, plugins and a plain `Could not find artifact` are never handled this way. Files, listing and removal: `deploy/README.md` §3. Manual way for a declined case or with the switch off: below the table |
| exit 2 but `build=ok`, `attn=rewrite-failed` (a framework project, `scope=jdk-jakarta`) | The automatic conversion (OpenRewrite) failed, so nothing was converted, and a conversion-only run is not a success even though the unconverted tree builds: head cell 「❌ 변환 실패 (빌드는 통과)」 (en `❌ NOT CONVERTED (the build passed)`), the framework block leaves the conversion out of what was done, the first item of 「확인해 주세요」 is 「자동 변환 실패」 with the reason (journal `rewrite_failed {step, sets, reason}`, flag `rewrite-failed`). Fix the cause (often a pom OpenRewrite cannot parse; see the next row) and rerun. The chained remediation still runs (`remediation.after_failed_build: framework`). In an ordinary run the same flag is the first attention code and the first hand-over line, and the build decides the verdict. The fleet summary (`sbmig/fleet/runner.py`) calls this case a failed build |
| `transform.rewrite: the automatic conversion failed …: not reachable: <repository> (<reason>); Unable to download POM: <g>:<a>:<v>; tried <repository>: HTTP <code>` while `mvn clean package` passes | OpenRewrite downloads poms with its own code and reads the settings mirrors differently from Maven: `external:http:*` matches nothing for it (it asks the original repository url), `<blocked>` is ignored, of two matching mirrors it takes the first in the file. The line names what it could not reach and what answered 404. The bare warning `Failed to download <g>:<a>:<v> from … Attempted URIs:` is printed on passing runs too and means nothing by itself | Look at the journal's `rewrite_settings` event: `shadows` are the repositories sbmig redirected for OpenRewrite, `skipped` the ones it could not (with the reason). Write the mirrors as `*`, `external:*`, explicit ids or `!id`; one `*` mirror at the repository group is the simplest form (`deploy/README.md` §3). `openrewrite.translate_settings: false` passes the original settings |
| `Unable to download POM: <g>:<a>:<v>. Tried repositories:` with `HTTP 404` from every repository, for a library the project declares with a `<classifier>`, while `mvn clean package` passes (only the warning `The POM for … is missing`) | The repository holds only `<a>-<v>-<classifier>.jar` for that version: no pom, no main jar. OpenRewrite accepts a missing pom only when `<a>-<v>.jar` is in the local repository or a repository answers for it; Maven does not care. sbmig writes a minimal pom with `packaging` pom into the pod's local repository and runs the same call again (journal `pomless_stub {…, form: "classified", jar}`, flag `pomless-library`, `build.pomless_stub_max` per run). A `pomless_check` line says why none was written: `snapshot` (a `-SNAPSHOT` version: not handled), `pom-present` (a pom is there, the cause is another one; a parent pom or BOM ends here), `main-jar-available`, `jar-unavailable` (no jar with a classifier in the local folder), `pom-available` (a repository has the pom: a transfer or mirror problem). Each slot has its own local repository. The lasting fix is the library's pom in the repository; then delete the file. `docs/INTERFACES.md` "v2.1z-cj" |
| `Failed to parse pom: Invalid UTF-8 middle byte …` in the OpenRewrite log; flag `pom-encoding-corrected` | A pom whose Korean text is saved as MS949 / EUC-KR under a UTF-8 header (or none). Maven builds it, OpenRewrite cannot parse it and the whole recipe run fails. Before the conversion sbmig corrects it (`layout.legacy_context.correct_pom_encoding`, default on): strict EUC-KR bytes → only the declaration becomes `EUC-KR`; MS949-only syllables → saved again as UTF-8; the Korean text never changes. Commit `transform: pom encodings corrected`, journal `pom_encodings {step, entries: [{pom, declared, actual, how}]}`, hand-over line 「자동 변환 전에 pom 파일의 글자 인코딩을 바로잡았습니다. …」. Left alone: a file with a BOM, a consistent pom, XML that is not well-formed, bytes only latin-1 or shift_jis explain. Details: `agent-doc/07-config-and-knowledge.md` |
| `blocked:repository_unreachable` in `attn`, BLOCKED `repository_unreachable` | a repository did not answer (time-out, refused or reset connection, unknown host, no route, PKIX, 5xx): `<g:a:v> could not be downloaded: the repository did not answer (<repos>)`. No LLM try, nothing credited in the KB (`loop.blocked_policy.repository_unreachable: 0`). Not a project problem: check the repository and the network, then rerun. A 401 / 403 or a "not found" answer keeps its own reason |
| flag `framework-unchecked`, Handover "framework check could not run (…)" | the framework check could read neither the parent pom (no as-is effective pom) nor the dependency tree, and found nothing; the run went on in full scope. If the project uses the in-house framework, do not use the result: fix the repository / settings and rerun (journal `framework_check`) |
| `run aborted: ZIP refused: …` (exit 3) | journal `intake_refused`; the sentence names the `intake.*` limit that was exceeded |
| a web run refused at once, 「웹 파드와 빌드 파드가 서로 다른 이미지로 실행되고 있어 …」 | the image was pushed and the web pod not restarted: §5 |
| upload answers HTTP 507 / HTTP 403 `cross-site request refused: …` | §5 |
| `remediation=FAILED`, `the round <r> candidate could not be put back to its expected state after the build (<n> path(s), first: <p>: <ErrorType>); nothing of this round was delivered` (report: 「라운드 <r>의 수정본을 빌드 뒤 원래 상태로 되돌리지 못해 …」) | The build (or a later Maven call) changed a manifest path of the candidate and sbmig could not write it back, even after making its folders writable once. Journal `candidate_sync_failed` / `candidate_not_restored` name the first 5 paths and error types; look at the permissions / owner of `remediation/rounds/<r>/candidate/<p>` (a plugin that leaves read-only files, a file owned by another user on the volume). Nothing of that round was committed or delivered; an earlier round's verified `result.zip` stays. Fix the cause, then upload the project again |
| `remediation=FAILED`, `the result ZIP of round <r> could not be written (<ErrorType>: …); delivery/result.zip is still the result ZIP of round <k>` (or `no result ZIP exists; the original is preserved as original/source.zip`; report: 「라운드 <r>의 결과 ZIP을 쓰지 못했습니다(…).」) | Writing the new result ZIP failed (full disk, a read-only or foreign-owned `remediation/delivery/`). Nothing was replaced: `delivery/result.zip`, `delivery.json` and the state still describe the ZIP the sentence names (round 0 = the unchanged project). Journal `delivery_not_written` (`round`, `error`, `kept_round`). Free space or fix the folder's owner, then upload the project again. Leftover `delivery/.result.next.zip` / `.result.next.json` after a pod was killed are cleaned up (or the swap completed) by the next entry of that run folder; never remove or rename them by hand while a run holds the folder |
| `remediation=FAILED`, `delivery/result.zip is now the result ZIP of round <r>, but delivery/delivery.json (and / or state.json) could not be written (<ErrorType>: …); the record is completed the next time this run is opened` (report: 「결과 ZIP(delivery/result.zip)은 이제 라운드 <r>의 결과입니다. 다만 기록 파일(…)을 쓰지 못했습니다(…). 이 실행을 다음에 열 때 기록을 마저 맞춥니다.」) | The new result ZIP was already in place when writing its records failed (full disk). The swap is rolled forward: `result.zip` is round <r>'s ZIP, the marker `delivery/.result.next.json` stays. Journal `delivery_record_not_written` (`round`, `records`, `error`). Free space; the next time anything opens the run (opening its status on the page, a download, a resume, a web-pod restart) the records are completed (journal `delivery_recovered`); never edit `delivery.json` or remove the marker by hand |
| `remediation=FAILED zip_hash_mismatch`, `the scan source ZIP (original/scan-source.zip) could not be made again with the sha256 recorded in the state` (report: 「검사용 ZIP(original/scan-source.zip)을 처음 검사한 것과 같은 바이트로 다시 만들 수 없습니다.」) | The upload carried build output, so the baseline scan was of `original/scan-source.zip` (a ZIP of `accepted/`); on a later entry that ZIP was missing or changed and could not be made again with the same bytes, so the scanned bytes cannot be vouched for. Something changed `accepted/` or `original/` outside the run. Upload the project again. `IVAS … differs from the scan source ZIP` is the same check failing on IVAS's side |
| journal `candidate_rebuilt` with `why` (`no record that the applied files were stored completely`, `no completeness marker`, …) after an image was deployed | A remediation run stopped or waiting at `build` by the previous version resumed in the new one: its run folder lacks the proof, so the candidate is made again from `accepted/` and the plan's edits are applied again. Expected once per such run, not an error; the same `why` on a run the current version started means a kill during `apply` |
| On a Windows PC: `remediation=FAILED`, `PermissionError: [WinError 5]` (or `[WinError 32]`) naming a path under `remediation\`; `sbmig.log` has `remediation: <op> of <name> still refused after 60.0 s` | Another program had a file of the run folder open (a virus scanner, an indexer, an editor that has the folder open). The remediation's renames and removals wait up to 60 s for it (`… refused (WinError <n>) … waiting up to 60 s`, then `… went through after <t> s`); this run waited the whole time. The `still refused` line names the programs that had the file open (`… : open in <application> [<executable>] (pid <n>)`, `this process` for sbmig itself); `(no program found …)` means the lookup ran and saw none (a scanner's driver, a memory mapping, a process with its working directory in the tree), `(the lookup of the holding program failed: <reason>)` that it could not run. The file most often met is `delivery\result.zip` of an earlier round, replaced when a later round delivers | Close the named program, keep it away from the output folder, or start the server from a console outside the editor, then run 취약점 해결 with the migration's result ZIP (no second migration). A leftover `remediation\.accepted.partial` shows which files were held. On Linux (the cluster) an open file can be replaced, so this cannot happen there |

**A minimal pom by hand** (a library whose repository has the jar but no pom, when sbmig declined or the switch is off). Write a
file with the library's coordinates (placeholders here):

```xml
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example.lib</groupId><artifactId>example-lib</artifactId><version>1.0.0</version>
  <packaging>jar</packaging>
</project>
```

and put it where Maven looks first, in the local repository of the build pods (`<group path>` = the groupId with `/`):

```bash
D=com/example/lib/example-lib/1.0.0
for R in repository repository-slot2 repository-slot3; do
  kubectl -n agent exec deploy/sbmig -- mkdir -p /data/.m2/$R/$D
  kubectl -n agent exec -i deploy/sbmig -- sh -c "cat > /data/.m2/$R/$D/example-lib-1.0.0.pom" < example-lib-1.0.0.pom
done
```

Each slot has its own local repository (`repository`, `repository-slot2`, `repository-slot3`), so the file goes into all three; then rerun. Remove the file once the library's owner uploads
a real pom (the hand-written file has no sbmig comment, so the listing in `deploy/README.md` §3 does not show it).

Details per file, log line and journal event: `agent-doc/03-run-debug-and-logs.md`. `sbmig parse-log <log>`
re-parses any Maven log into errors with signatures.

**What the owners read** (`report.language: ko`, the company setting — `sbmig/report/guide_ko.py`): a head table
(결과, `SBMIG-RESULT`, 전환 후, 실행 ID, 시작 / 종료, 플래그; an aborted run reads "❌ 실행 중단 (빌드까지 진행하지 못했습니다)" only
when no build ran, else "❌ 실행 중단 (마지막 빌드는 통과했습니다)" / "(마지막 빌드는 통과하지 않았습니다)"), then 「전환 내용」 (Java / Boot / parent as-is → final, every
changed dependency; **⚠** = pinned at the version the as-is parent managed because the new chain does not — confirm or
replace), 「확인해 주세요」 (one numbered list: the library advisories with their Korean texts, see
[ADVISORIES.md](ADVISORIES.md), then the hand-over items and follow-ups, capped by `advisories.max_from_here`),
「코드 수정 내역」 (the automatic conversion in a few lines; every file the fix loop or the structure review changed, with
its net diff from the commit right after the automatic conversion to the final tree — journal `code_changes_base`,
`sbmig/report/changes.py`; diffs for the first 30 files, 80 lines each), 「해결하지 못한 오류」 (open errors only),
「진행하지 않은 항목」 (blocked), the special sections when they apply (운영자 확인 = major upgrades to check at runtime,
pom 합성 (Ant), ADF, 포함된 jar, javax API jar, 외부 설정 값, 취약점 조치) and 「참고」 (collapsed: 실행 정보 with the
structure review's verdict and each flag explained, LLM 사용량과 비용, 산출물, 진단용 기록 = the plan / phase notes).
Resolved errors are listed only in `errors.md` / `errors.jsonl`. `report.language: en` renders the diagnostic layout of
`sbmig/report/markdown.py` (Handover with Versions replaced, Libraries needing your attention, Properties to set before
running and "From here on, your team"; Error list, Fixes by case, Structure review, Blocked, Manual follow-ups, Flags,
What we changed, LLM usage and cost, Notes, Outputs). 외부 설정 값 (en: Properties to set before running) lists the keys
the external Tomcat used to supply (JNDI, context-param, `-D`), written as `key=__SET_ME__` placeholders in the boot
module's `application.properties`. Runtime behaviour is never verified by sbmig; before deploying, the owners check
`grep -rn __SET_ME__` and supply the values outside the jar (env variables, `-D`, an external `application.properties`).

### "Is it stuck?"

1. `tail -f <run dir>/sbmig.log`: a slow call keeps logging — `llm call still in flight: Ns elapsed` every 30 s,
   `<label> still running: Ns elapsed … log: <file>` every 60 s for Maven. An LLM call is abandoned after
   `llm.timeout_seconds` + 30 s; a build after `build.timeout_minutes`.
2. The fix-loop watchdog: `fix loop quiet for Ns since '<milestone>' …` names the code that is running; it is silent
   while a build, a rewrite or an LLM call runs (they have their own heartbeats). After 5 minutes of silence it writes
   a thread dump into the run directory.
3. Thread dump on demand: create a file `dump-threads` in the run directory (`New-Item <run dir>\dump-threads
   -ItemType File`) → `threads-<timestamp>.txt` within 2 s; or `GET /api/threads` on the web server; POSIX
   `kill -USR1 <pid>`; Ctrl+Break in a Windows `sbmig run` console. Ask for the bottom ~15 lines of the run thread's
   block, not the file.

```powershell
$run = "out\run-20261026-101500-ab12cd"
Select-String -Path "$run\sbmig.log" -Pattern "SBMIG-RESULT|run aborted:" -Encoding UTF8 | Select-Object -Last 2
Select-String -Path "$run\sbmig.log" -Pattern "fix loop quiet|KB 탐색" -Encoding UTF8 | Select-Object -Last 5
Select-String -Path "$run\sbmig.log" -Pattern "cause=|finish_reason=length|LLM configuration error" -Encoding UTF8 | Select-Object -Last 2
New-Item "$run\dump-threads" -ItemType File; Start-Sleep 3; Get-ChildItem $run -Filter "threads-*.txt"
```

LLM lines worth retyping: a transport failure carries `cause=<Class>: <message>`; an empty answer with
`finish_reason=length` is a rejection of that request (prompt too big — lower `llm.max_context_chars`); the same
HTTP 400 body for two prompts stops the LLM for the run (`llm-config-error`; check the model names in `.env`). A
failed token exchange (`token exchange failed: HTTP <code> (status=<code>)`, `… transport (<Class>) (cause=…)`, no
JSON, no `access_token`) is retried like a chat call and counts as an LLM outage (cool-down, then `llm_unavailable`),
never as an answer; the token endpoint's body is not logged. An answer cut at the output token limit whose `<script>` /
`<edits>` block is not closed is neither used nor stored: the feedback round asks for a shorter answer, a second cut
fails the attempt (`the answer was cut off at the output token limit (finish_reason=<r>) before <script> closed`).

On the company PC without copy-paste, ask for specific lines (`SBMIG-RESULT`, `preflight: … FAIL`, `run aborted:`,
`cause=`, `LLM-PROBE`, `REPO-PROBE`), each found with one `Select-String -Encoding UTF8` command — not for files.

## 3. Knowledge base (`kb.dir`, files only)

- Layout: `errors.jsonl` (every run's error list), `cases.md`, `cases/<case_id>/case.json` + `fix_v<N>.py` (the
  stored fix scripts per error case, with lineage and successes / failures / projects). Default `kb-v2` inside the
  checkout; in production set an absolute path outside it. Tests and demos write under `out/` only.
- Inspect: `sbmig kb cases list`, `sbmig kb cases show <case_id>`, `sbmig kb errors list|show`, `sbmig kb stats`,
  `sbmig kb validate`.
- Curate: `sbmig kb cases retire <case_id> <N>` (never run version N again), `activate <case_id> <N>` (undo),
  `pin <case_id> block[:REASON]` (no script / LLM tries; the report's 「진행하지 않은 항목」 shows the reason), `unpin`.
- Move / back up: `sbmig kb export kb-YYYYMMDD.zip` → `sbmig kb import kb-YYYYMMDD.zip [--replace]` (the old KB is
  kept as `.bak-*`; every imported script is written as `fix_v<N>.py` inside its case folder whatever the imported
  `case.json` names it); `sbmig kb reindex` after manual edits. Back up with export, not git. Fields a `case.json`
  holds that this image does not know (top level, per script version, per run record) survive a re-save.
- Every write runs under `<kb>/.lock` (`kb.lock_timeout_seconds`), so the web server, a fleet and `sbmig kb …` can
  share one KB. Secrets are masked before anything is stored (built-in rules + `knowledge/kb/redact.yml`).
- Offline demo: `scripts/demo-kb/run.sh` (run 2 reuses run 1's stored script without the LLM).

More: `agent-doc/04-cases-scripts-kb.md`.

## 4. Fleet — many projects, one agent, sequentially

```bash
sbmig fleet run projects.yml --config config/artifacts.yml --out out/fleet   # manifest: dir | .txt | .yml | .csv
sbmig fleet status out/fleet --attention
sbmig fleet resume out/fleet [--retry-failed]
```

Projects run strictly one after another with one shared KB. A preflight (`preflight: <check> ok|FAIL|skip …`) runs
before the first project; infrastructure outages defer projects instead of failing them (`fleet stopped: …` after
the configured waits — fix the cause and run the same command again). `out/fleet/fleet/summary.{md,csv,json}` and
`learning.md` are rewritten after each project. Everything else: [FLEET.md](FLEET.md).

```powershell
Select-String -Path out\fleet\fleet\sbmig.log -Pattern "preflight: .* FAIL|fleet stopped:|fleet: infrastructure" -Encoding UTF8 | Select-Object -Last 3
sbmig clean --fleet out\fleet --older-than 7 --keep-failed --dry-run
```

## 5. Web UI and Kubernetes Job mode

- **Local web UI**: `sbmig serve --port 8765 --out out/web` (FastAPI on uvicorn, bound to 127.0.0.1): upload a zip,
  choose 전체 진행 (migration, then the remediation) or 취약점 해결 (remediation only, §6), follow the stage rail /
  journal / log, read the reports rendered in the result card (「마이그레이션 보고서 미리보기」 at the end of the card, 「조치 결과
  보고서 미리보기」 inside the remediation block showing exactly `remediation/report.md`; each with 「보기」 / 「원문」 and
  「크게 보기」; a remediation-only run shows its short run report as 「보고서 미리보기」 until the remediation report
  exists), download them (received as `migration-report.md`; the remediation report as
  `remediation-report.md`; the files in the run folder keep their names) and `result.zip`. Every upload needs a project name (letters, digits, `_`, `-`, at most 30 characters; rule and refusals in
  `deploy/README.md` §2), and a run's folder name starts with it (`<project>-YYYYmmdd-HHMMSS-<hex 6>`; `ls /data/out | grep <project>`); a run refused at intake, or aborted by Maven's HTTP repository blocker, shows its reason as a sentence. Sharing on the LAN needs a token
  (`SBMIG_WEB_TOKEN` or `--token`; `--open` only on a trusted intranet). The configs every upload runs with are the
  server's choice (`--config` / `SBMIG_WEB_CONFIGS`). In-process mode runs one migration at a time (the others are
  `queued`); "중단" takes effect between graph nodes, and a stop that arrives after every planned final build has a
  result is ignored (journal `stop_request` with `ignored`); inside the build fix loop the request (and the run time budget)
  is checked between builds and before each LLM request (journal `llm_ask_skipped`); a fix loop left on the request ends the run as aborted with
  "… the fix loop was left early; the last build shows the tree as it was left").
- **Requests from another web site are refused**: any request other than GET / HEAD / OPTIONS carrying
  `Sec-Fetch-Site: cross-site`, or an `Origin` whose host is not the request's own (`Host` / `X-Forwarded-Host`;
  `Origin: null` counts as foreign), gets HTTP 403 `{"error": "cross-site request refused: a page of another web site
  cannot start, stop or change a run (Origin / Sec-Fetch-Site)"}` before the token and open-mode checks, on every route.
  The page itself and clients that send neither header (curl, scripts) are unaffected.
- **Uploads**: below `fleet.min_free_gb` (5 GB; 0 = off) free on the output volume an upload is refused with HTTP 507
  before the body is read (the page: 「서버의 저장 공간이 부족해 시작하지 못했습니다. 운영 담당자에게 알려 주세요.」). Nothing deletes
  old run folders; `sbmig clean` is manual and removes only `project/` copies, uploads and LLM records of finished runs
  (uploads of running, waiting and, with `--keep-failed`, failed runs are kept; never a run folder, `source.zip` or
  `result.zip`). An uploaded ZIP over the unpacking limits (§1) ends the run at intake; the in-process web answers 400.
- **Command-line values win over the environment**: `--settings`, `--java-home`, … and a fleet manifest's per-project
  overrides are applied after the `SBMIG_<SECTION>__<KEY>` values (INFO `config: <key> given explicitly replaces
  <VAR> from the environment`); the environment still wins over the yml files.
- **Containers / Kubernetes**: `deploy/README.md` — the image, the web pod, and `sbmig serve --mode k8s-job`
  (one build Job per upload, up to `SBMIG_JOB_MAX_PARALLEL` at a time, the shared file KB, env pins). Operator rule:
  image only → build, push, restart the web pod; `20-configmap.yaml` → apply it, restart the web pod;
  `40-deployment.yaml` → apply it; `81-web-job.template.yaml` → build and push the image, restart the web pod (the web
  pod reads the image's copy `/app/deploy/k8s/81-web-job.template.yaml`, filled in the checkout the image is built
  from; a value still containing `CHANGE_ME` stops the web pod at startup with `MIGRATION_TEMPLATE_INVALID`; a cluster
  that must change the template without a new image mounts it as a ConfigMap and sets `SBMIG_JOB_TEMPLATE`, then
  re-creates that ConfigMap instead). Build pods pull at start, so **after pushing an image restart the web pod**. The build identity is
  `sbmig version` (`0.1.0+<12 hex>`, a digest of `sbmig/**/*.py`, `config/*.yml` and `knowledge/**`); the Main that
  creates a Job records its config digests and version in `control/state.json`, and a build pod that differs refuses to
  run (exit 3, journal `worker_refused`, "config <name> differs between the web pod and the build pod" / "sbmig version
  differs: web pod <v>, build pod <v>" + "the web pod and the build pod run different images: restart the web pod after
  pushing an image (and upload the project again)"; the page shows the Korean sentence). A run uploaded between the push
  and the restart has to be uploaded again; runs that waited in line across the restart and remediation resumes of runs
  an older image made are accepted when the two pods agree now. Operations summary for in-house LLMs:
  `agent-doc/09-deploy-and-ops.md`.

## 6. Vulnerability remediation

When a migration ends BUILD SUCCESS and `remediation.after_migration` is on (shipped on in the Kubernetes manifests,
off in the code) — and, with `remediation.after_failed_build: framework` (default; `never` switches it off), also when a
framework project (§0.3) ended with a failed build, whether or not its fix loop ran — the same build pod scans the migrated project with IVAS, changes only the version text of
vulnerable libraries in the poms (or inserts one `<dependencyManagement>` override entry), rebuilds and rescans. It applies
its changes and goes to the end without asking anybody, normally in the same build pod. There is no approval step: a yml
that still carries `require_approval` / `always_approve` under `remediation:`, or a ConfigMap that still carries
`SBMIG_REMEDIATION__REQUIRE_APPROVAL`, is not an error — each is ignored with one WARNING (`config: remediation.require_approval=True is
ignored: the approval step of the vulnerability remediation was removed (every proposal is applied without waiting) —
remove it`; for the variable `config: env override SBMIG_*__*: remediation.require_approval is ignored: …`), and the
variable is left out of the web pod / build pod environment comparison. Clean-up on a cluster: delete the key from
ConfigMap `sbmig-env` if it is there (apply 20, restart the web pod) and delete its forward line from the cluster's own
copy of `81-web-job.template.yaml` (rebuild and push the image). A remediation an older version left waiting for approval
is shown as ended without the word 승인 anywhere on the page (nothing in 「지난 실행」; opened: 「조치 미완료」 and 「이 실행의 취약점 조치는 끝나지 않았습니다. 프로젝트를 다시 올리면 끝까지 진행합니다.」);
its files stay downloadable, a resume is refused (409): upload the project again. Files
go to `<run dir>/remediation/`; the outcome never changes the migration's exit code and is appended to `SBMIG-RESULT`
as `remediation=<STATUS|skipped>`. Upload confirmations and resume (a new build pod) happen
in the web UI. Every remediation (chained, `--flow remediation`, a resume in a new pod) writes
`remediation/remediation.log`. Findings IVAS makes in files of the project (static front-end files, a jar lying outside the pom) are listed
in the report's 「프로젝트 파일에서 찾은 취약점」 and are neither remediated nor part of the verdict (`remediation.file_components: report`,
default; `count` = they count as remaining, as residuals); a failure while reading the file lists (`…/result/filecomponents/{id}/matchedfiles`)
is a note, not an incomplete scan. A run that ended `INCOMPLETE scan_incomplete … http_error, page_missing` on a
matched-files call cannot be resumed: run it again. In the Korean remediation report 조치 완료 and 「해결된 취약점」 count only findings a complete
final scan no longer reports (a change whose finding is still reported shows 「-」).
When the candidate build fails after versions were raised and the baseline build passed, the build fix loop tries to
fix the source (`remediation.fix_loop`, default true; `remediation.fix_loop_max_iterations`, 10, 1–50;
`remediation.fix_loop_max_minutes`, 30, 1–240). It runs on a work copy (`remediation/rounds/<r>/fixloop/tree/`, without
any `.git` path of the upload), never on the candidate: it may change existing source files only — never a pom (also
one Maven reads under another name), a path with a segment starting with `.`, `mvnw` / shell / batch scripts or a
jar / war / ear / class / zip file — and creates or deletes no file; it uses no stored script and records nothing in
the KB (its journal and logs stay in `fixloop/`). Its time is `min(fix_loop_max_minutes, time left − a reserve for the
confirming build and the delivery)`; with less than a minute it does not start (checked once, by the graph:
`fix_loop_max_minutes: 1` with enough time left starts it). Fixed → the fixed bytes are kept in
`fixloop/fixed/`, the state is saved, the graph copies them over the candidate (the only way the loop changes the
candidate), a confirming build (`rounds/<r>/build-fixed.log`) runs and the round is committed; each fixed round's diff
is `rounds/<r>/fix.patch`, and `remediation/fix.patch` (downloadable as `fix.patch`) combines every committed fixed
round; the report has 「빌드 오류 수정」 and the page the chip 「소스 <n>개 수정」; journal `remediation_fix_loop`
(`phase: start|end|not_started`), status field `fix`, `rounds[].fix` in `report.json`. A first build failure that a
second build passes without a change is recorded as such (no fix). Not fixed → the candidate was never touched and the
round ends `candidate_build_failed` as before, the status message ending in ` | automatic build fix: <reason>` (or
` | automatic build fix not started: <reason>`) and the report saying 「빌드 오류 자동 수정을 시도했지만 고치지
못했습니다(<사유>). 시도한 소스 수정은 결과에 넣지 않았습니다.」 (not started: 「빌드 오류 자동 수정은 시작하지 않았습니다(<사유>).」). A
stop during the confirming build keeps the fix: the run halts `STOPPED` and a resume confirms it again. A loop that
ended because of a stop request is not a result: a resume may run it again (`time budget used up` stays final); the
loop checks the stop request and its time before each LLM request too, and a stop during the loop halts `STOPPED` with
`stop requested during the round <r> build fix` (「라운드 <r>의 빌드 오류 자동 수정 중에 중지 요청을 받았습니다.」). A
loop that never started because of a stop or the time budget may start on resume; one whose folder could not be prepared
stays not started. A
loop that started but made no attempt (the LLM unavailable at once, out of time before the first attempt) ends the
message with ` | automatic build fix made no attempt: <reason>` and the report says 「빌드 오류 자동 수정을 하지
못했습니다(<사유>).」; a loop whose folder could not be prepared counts as not started. A fix is
discarded (record line, LLM calls still counted) when its confirming build fails, when the round's candidate is remade
after an ineffective override entry was dropped, or when it no longer fits the candidate on resume; a loop the pod was
killed in is kept as `rounds/<r>/fixloop-interrupted-<n>/` and its LLM calls are counted once, at the next entry of the
build node (or the report node) whatever happens next; its work copy `tree/` is then removed (journal and logs stay). A
re-entry after a kill never redoes what the uninterrupted run would not: a fix the loop had just returned that could not
be applied ended the round and is marked so (`ended_round` on the discarded result), so the loop is not run again; a
stored fix found not usable on a resume runs the loop again, also when a stop or a kill cut that resume short before the
loop started; and a stored complete baseline scan of the same bytes is reused (journal `baseline_scan_reused`), never uploaded again. A candidate is built only with the proof that its applied files
were stored completely: `rounds/<r>/applied/` (the edited poms), `rounds/<r>/expected.json` (size, sha256 and exec bit
of every manifest path) and `rounds/<r>/candidate.ready.json` naming that file's sha256; without it (a kill during `apply`, or a
run folder an older version left) the candidate is made again from `accepted/` (journal `candidate_rebuilt` with
`why`). After every candidate build, and again right before the round is committed or a failed candidate is frozen,
the candidate is brought back to the files the remediation left (`accepted/` + the applied poms + a reported fix),
compared against `expected.json` so each file is read at most once: files a build or a later Maven call removed or
rewrote (a formatter, sortpom), an exec bit a build flipped and a file a build left unreadable never reach a delivery; the record says 「빌드가 바꾼 파일 <n>개는 결과에 넣지 않고 원래대로
두었습니다.」, journal `build_side_changes`. A path that cannot be put back even after its folders were made writable
ends the run `FAILED internal_error` with nothing of that round delivered (§2, symptoms table). No loop when the baseline
build already failed; an error that needs a pom change (an exclusion, a new artifact) stays unfixed; a project whose
build needs its own `.git` (git-commit-id) cannot build in the work copy, so its loop ends not fixed; the loop's Maven
calls that do not go through the build runner (effective pom, dependency tree) keep their own timeouts; the
per-attempt guard does not see a new file under a path the project's `.gitignore` ignores (the check at the end of the
loop does). The loop's LLM
calls count in `SBMIG-RESULT … llm=` (after an exception inside the loop they are read from the loop's journal; calls
the loop counted without token counts, a failed LLM call for instance, get a row of their own marked `tokens_unknown`,
never priced, beside the known row of the same model, which keeps its price; a killed loop's journal with a line that
cannot be parsed, or one that cannot be read to the end, makes the loop `usage_unknown` with what was read kept (a
missing journal means no LLM request was made: a known zero); a project row, the totals and a model row that sum such
a run carry the same 「일부 알 수 없음」 note; `llm=` then holds the known
calls only), in `GET /api/runs/{id}` (`llm_calls`, of which
`remediation_llm_calls`; `remediation_llm_usage_unknown: true` when part is unknown) and on the admin page (「조치 N회
포함」; when part is unknown 「일부 알 수 없음」 under the calls and the cost is the sum of the priced rows with the same
note under it, or 「알 수 없음」 when no row is priced; a known row without a configured price still leaves the cost
empty; the project and overall totals carry no such note; the report says 「LLM 사용량 일부는 알 수 없음」 and a round
total 「(버린 시도까지 알려진 것만 T회)」); the migration's `report.json` usage does not hold them. Operator text:
`deploy/README.md` §6; flow, states, API and diagnosis: `agent-doc/11-remediations.md`; `sbmig remediation-resume
--run-dir <dir>` resumes outside Kubernetes.

**Run folder and the scanned bytes.** `remediation/` holds `original/source.zip` (the upload, never changed; served as
`source.zip`), `original/manifest.json`, `accepted/`, `baseline/`, `rounds/<n>/`, `scans/`, `delivery/` (`result.zip`,
`delivery.json`, a kept `result-r<k>-verified.zip` + `.json`), `state.json`, `journal.jsonl`, `remediation.log`, the two reports and
`fix.patch`. When the upload carried build output (`target/` beside a pom, left out of `accepted/` and the manifest), the
baseline scan uploads `original/scan-source.zip`, a deterministic ZIP of `accepted/` (its sha256 in `state.json`
`scan_source_sha256`, journal `scan_source_made`, not downloadable), and a run that changes nothing delivers exactly those
bytes; without build output the upload itself is scanned and delivered. A run started by an earlier version that had
already scanned the original keeps the original. Every result ZIP is written under `delivery/.result.next.zip` first,
with the marker `delivery/.result.next.json`, and only then replaces `result.zip`; both temporary files exist only during
that swap (or after a failure to write the records once the ZIP was replaced), and every entry that sees the run folder
again completes or rolls back the swap (journal `delivery_recovered`; a leftover `.result-r*-verified.zip.copy-tmp` is
removed too). A final state whose report node was killed, or whose swap record is pending, is repaired on the next entry
(journal `report_rewritten`; `chain.resume` journals `remediation_report_repair` instead of refusing it) and, once nothing
drives the run any more, by the web service in every mode: when the page asks for the remediation's status, report,
`result.zip` or `fix.patch`, and at startup for standalone runs (journal `remediation_final_repair`; status, reasons and
message unchanged; a fresh claim of a killed pod delays it up to 600 s).

**Remediation only** (a project migrated earlier, checked again): `sbmig run <zip|dir> --config config/artifacts.yml
--flow remediation` on a PC, or the page's 취약점 해결 choice (in the cluster an ordinary build Job in the same queue; nothing
to set in the manifests). No migration step, no KB access, the LLM only for the build fix loop on a failed candidate: the upload is built as it is (a project that does
not build ends `FAILED baseline_build_failed` before any scan), scanned, remediated and rescanned; files under
`<run dir>/remediation/`, `SBMIG-RESULT … flow=remediation … remediation=<STATUS>`. `remediation.after_migration` and
`remediation.trusted_projects` are not consulted. Exit 0 = `SUCCEEDED`, 2 = any other saved status, 3 = not started
(refused at intake, or flag `remediation:not-ready` when the IVAS values or Maven readiness — e.g. `build.settings_file`
on a PC — are missing; the report names what is missing). `--stop-after` is refused with this flow.

## 7. Tuning knobs (`config/default.yml`, Korean comments there)

| Knob | Default | Effect |
|---|---|---|
| `loop.phases.verify.max_iterations` | 50 | fix-loop rounds (one round = one build) |
| `loop.max_llm_calls`, `loop.max_total_minutes` | 0 (no cap) / 240 | budgets per run |
| `loop.stall_limit` | 2 | the same error set N times → escalation model |
| `script.max_llm_tries_per_case` / `script.max_augments_per_case` | 20 / 15 | LLM asks per case, run and set of places the case shows at (over all places: twice the first number); then BLOCKED `repeated_failure` |
| `script.max_declines_per_case` | 1 | an LLM decline → BLOCKED `needs_owner` |
| `script.max_edit_tries_per_case` | 4 | `<no_script>` + edits answers per case |
| `loop.blocked_policy.<reason>` | 0–2 | script attempts before a case of that reason is blocked (`repository_unreachable`: 0) |
| `intake.max_unpacked_mb` / `intake.max_entry_mb` / `intake.max_compression_ratio` | 4096 / 1024 / 200 | unpacking limits of an uploaded ZIP (0 = no limit) |
| `llm.escalation_model` (`LLM_ESCALATION_MODEL`) | empty = `llm.model` | stronger model from a case's third ask or after a stall |
| `llm.case_thread` | false | agent gateway: augments continue the case's session with the delta only |
| `llm.pricing.<model>` | list prices | per-run cost in the report and the fleet summary; a row is priced by its asked name (the agent gateway's role, e.g. `build_error_fixer`) first, then by the model names the gateway reported, so a role key wins |
| `build.skip_test_compile` | true | `-Dmaven.test.skip=true` on every build |
| `build.require_java_major` | 21 | preflight: abort when Maven runs on another Java |
| `build.retry_negative_cache` | true | a build that failed on Maven's negative cache runs once more with `-U` |
| `build.file_lock_retries` | see yml | retries when Windows locks `target/` (then `infra:file-lock`) |
| `plan.advisor` | none | optional LLM plan review (additive, catalog-only; [RECIPES.md](RECIPES.md)) |
| `openrewrite.download_retry` / `download_retry_wait_s` | 1 / 30 | recipe run retried after a transfer problem (429 / 5xx, reset, timeout, unknown host); never for a 404 or for OpenRewrite's download warnings |
| `openrewrite.translate_settings` | true | the OpenRewrite call gets a copy of the settings file with shadow mirrors so that it chooses the mirrors Maven chooses |
| `layout.*` | see yml | company layout (role parents, module names, JSP jakarta rename, bundled-jar wiring) |
| `advisories.*` | see yml | advisory files, off switches, carried-over listing, cap of the hand-over list |
| `kb.dir` / `kb.lock_timeout_seconds` | kb-v2 / 300 | KB folder, cross-process lock |
| `fleet.*` | see [FLEET.md](FLEET.md) | fleet order, budgets, infrastructure guard, cleanup; `fleet.min_free_gb` (5) is also the free-space floor of web uploads |
| `report.language` | en | `ko` = the Korean guide for the requester (`sbmig/report/guide_ko.py`), `en` = the diagnostic layout (`sbmig/report/markdown.py`); the company config sets `ko` |

## 8. Safety notes

- sbmig edits only the copy under `out/<run>/project`; the input zip / directory is never modified.
- Fix scripts run through `sbmig.script`: static check, `python -I` child process, helper API limited to the project
  folder, timeout and process-tree kill.
- No LLM call contains credentials; prompts include only the files and errors of the case.

## sbmig 를 다른 레포의 하위 폴더에 둘 때 (`<root>/src/migration-agent/`)

회사 레포가 `pyproject.toml` 과 `.env` 를 루트에 두고 sbmig 전체(`config/` … `sbmig/`, `tests/`, `fixtures/`, `scripts/`)를
`src/migration-agent/` 아래에 두는 경우. import 는 전부 `sbmig.…` 라서 폴더 이름은 상관없고, 설정·지식·KB 는 `sbmig/` 의
두 단계 위(`src/migration-agent/`)를 기준으로 찾는다. 루트 `.env` 는 git 루트까지 올라가며 읽으므로 그대로 쓰인다.
루트 `pyproject.toml` 에는 패키지 위치만 알려 주면 된다:

```toml
[tool.setuptools.packages.find]
where = ["src/migration-agent"]
include = ["sbmig*"]

[tool.setuptools.package-data]
sbmig = ["**/*.yml", "**/*.yaml", "**/*.md", "**/*.j2", "**/*.json", "**/*.html"]

[project.scripts]
sbmig = "sbmig.cli:app"

[tool.pytest.ini_options]
testpaths = ["src/migration-agent/tests"]
addopts = "-q"
markers = ["integration: needs mvn/java/network (deselect with -m 'not integration')", "llm: needs a real LLM endpoint"]
```

그리고 루트에서 `pip install -e .`, `python -m pytest -m "not integration"`, `sbmig run …`. `readme = "src/migration-agent/README.md"`
로 바꾸고, `[tool.ruff]` 는 그대로 둔다. e2e 는 `python src/migration-agent/scripts/e2e.py …` (스크립트가 자기 위치로 루트를 찾는다).

## Ant 프로젝트

Ant 프로젝트(`build.xml` 만 있고 pom.xml 이 없음)는 받지 않습니다(회사 결정: Ant → Maven 변환은 하지 않는다). 접수에서 멈추고
(종료 코드 3, 플래그 `unsupported-build-tool:ant`) 신청자에게 `intake.ant_message` 안내 문구 "Maven 변환 후 신청 부탁드립니다." 를
보여 줍니다. 콘솔 ERROR 줄, 보고서 첫 줄, 웹 화면에 같은 문장이 나옵니다.

pom 합성 기능은 지우지 않았습니다. 회사 설정 yml 에 `intake: {ant_to_maven: true}` 를 넣으면 접수가 build.xml 로 pom.xml 을
합성해 커밋하고 나머지는 Maven 프로젝트와 같습니다(`ant-webapp` 픽스처가 이 스위치를 켭니다). 그때는 실행 전에 두 가지만:
- 회사 설정 yml 에 `intake: {synth_group_id: <사내 groupId>}` 를 넣습니다(기본값은 자리표시자).
- 사내 공용 jar 가 `lib/` 에 많으면 `<kb>/jar_names.yml` 에 `{stem, artifact}` 행을 적어 두면 좌표를 바로 찾습니다(형식은
  `knowledge/libraries/jar_names.yml` 과 같고, 재배포 없이 다음 실행부터 반영).
실행 뒤 보고서 맨 앞 "Ant 프로젝트: pom.xml 합성" 블록에 좌표를 못 찾은 jar 와 `mvn install:install-file …` 명령이 있으면 Nexus 에
올리고 pom 의 `system` 항목을 보통 의존성으로 바꿔 주십시오(빌드는 통과하지만 system 범위 jar 는 WAR 에 포장되지 않습니다).
`build.xml` 의 코드 생성·복사 규칙·여러 WAR 타깃은 번역하지 않고 같은 블록에 "번역하지 않은 타깃" 으로 적습니다.
