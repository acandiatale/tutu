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

Projects built on the in-house framework run in the reduced scope `jdk-jakarta` (Java 21 + jakarta recipes, verify
build + fix loop, chained remediation; no restructure, no structure review, parent pom and framework version kept).
Details: `agent-doc/13-framework-projects.md`. To validate in the company environment:

| Item | What to do / look for |
|---|---|
| Switch it on | `framework.artifacts` in the company `config/artifacts.yml` with the real coordinates (placeholder: `["com.example.fw:example-fw-core"]`; a bare artifactId matches any groupId, matching is case-sensitive). In the cluster the file is baked into the image: rebuild and push the image (`deploy/README.md` §7) |
| Recipe sets | `framework.recipe_sets` (default `[jdk21, jakarta]`). The jakarta set moves the servlet API to the Jakarta EE 9 line (servlet 5.0); for projects that will run on Tomcat 10.1+ add `servlet_jakarta_10` — the company decides |
| First run on a real framework project | `SBMIG-RESULT … scope=jdk-jakarta`; `sbmig.log` INFO `분석: 사내 프레임워크 프로젝트 (…) - JDK와 jakarta만 적용`; journal `framework_project` (`evidence`, `unchecked`), `plan` with `"scope": "jdk-jakarta"`, (MS949 / EUC-KR sources) `source_encodings` before the rewrite and `scopes_restored` after it, commits in the order `transform: source encodings written` → `rewrite: transform.rewrite` → `transform: as-is provided scopes restored` → `transform: JSP javax → jakarta`, notes `source encoding written: <pom>: project.build.sourceEncoding=MS949 (…)` — an encoding the kept parent sets differently stays and becomes a hand-over item (「확인해 주세요」); `report.md` block `## 사내 프레임워크 프로젝트`, the two corrections listed under 「코드 수정 내역」 → 자동 변환 ("sbmig가 변환 단계에서 직접 고친 pom") and in `errors.md` as `seen: transform` entries; the page shows 프레임워크 프로젝트 and 재구성 / 구조 검토 as 하지 않음 |
| Not recognised | journal `framework_check` with `unchecked`: `parent: no as-is effective pom` (the intake capture failed, so inheritance from a parent pom was not checked), `transitive: <why>` (`dependency:tree` failed), nothing = the pattern does not match the real groupId / artifactId. Both `parent:` and `transitive:` present = the check could not look: flag `framework-unchecked`, WARNING `분석: 사내 프레임워크 여부를 확인하지 못함 (…)`, Handover line "framework check could not run (the parent pom and the dependency tree could not be read): if this project uses the in-house framework it must not be migrated in full — confirm before using the result"; the run goes on in full scope, the exit code is unchanged |
| Cost of the transitive check | when the poms, the parent capture and the bundled jars show nothing, every project pays one `mvn dependency:tree -Dincludes=…` on the as-is tree at analyze (one per service root for an MSA without a covering aggregator); `framework.transitive: off` saves it |
| A framework still on javax | BLOCKED `external_javax_api` without an LLM call, the suggestion names the framework artifact and says its owner must provide a jakarta build — expected, not an sbmig failure |
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
| `blocked:repository_unreachable` in `attn`, BLOCKED `repository_unreachable` | a repository did not answer (time-out, refused or reset connection, unknown host, no route, PKIX, 5xx): `<g:a:v> could not be downloaded: the repository did not answer (<repos>)`. No LLM try, nothing credited in the KB (`loop.blocked_policy.repository_unreachable: 0`). Not a project problem: check the repository and the network, then rerun. A 401 / 403 or a "not found" answer keeps its own reason |
| flag `framework-unchecked`, Handover "framework check could not run (…)" | the framework check could read neither the parent pom (no as-is effective pom) nor the dependency tree, and found nothing; the run went on in full scope. If the project uses the in-house framework, do not use the result: fix the repository / settings and rerun (journal `framework_check`) |
| `run aborted: ZIP refused: …` (exit 3) | journal `intake_refused`; the sentence names the `intake.*` limit that was exceeded |
| a web run refused at once, 「웹 파드와 빌드 파드가 서로 다른 이미지로 실행되고 있어 …」 | the image was pushed and the web pod not restarted: §5 |
| upload answers HTTP 507 / HTTP 403 `cross-site request refused: …` | §5 |

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
  journal / log, download `report.md` and `result.zip`. A run's folder name starts with a slug of the uploaded file's
  name; a run refused at intake, or aborted by Maven's HTTP repository blocker, shows its reason as a sentence. Sharing on the LAN needs a token
  (`SBMIG_WEB_TOKEN` or `--token`; `--open` only on a trusted intranet). The configs every upload runs with are the
  server's choice (`--config` / `SBMIG_WEB_CONFIGS`). In-process mode runs one migration at a time (the others are
  `queued`); "중단" takes effect between graph nodes, and a stop that arrives after every planned final build has a
  result is ignored (journal `stop_request` with `ignored`; a fix loop left on the request ends the run as aborted with
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
off in the code), the same build pod scans the migrated project with IVAS, changes only the version text of
vulnerable libraries in the poms (or inserts one `<dependencyManagement>` override entry), rebuilds and rescans. With the
shipped configuration every proposal is applied without approval and the remediation ends in the same build pod; approval
is switched on with `SBMIG_REMEDIATION__REQUIRE_APPROVAL: "true"` in `sbmig-env` (every proposal) or
`remediation.always_approve` in the company yml baked into the image (single classes). Files
go to `<run dir>/remediation/`; the outcome never changes the migration's exit code and is appended to `SBMIG-RESULT`
as `remediation=<STATUS|skipped>`. Approvals (when switched on), upload confirmations and resume (a new build pod) happen
in the web UI. Every remediation (chained, `--flow remediation`, a resume in a new pod) writes
`remediation/remediation.log`. In the Korean remediation report 조치 완료 and 「해결된 취약점」 count only findings a complete
final scan no longer reports (a change whose finding is still reported shows 「-」). Operator text:
`deploy/README.md` §6; flow, states, API and diagnosis: `agent-doc/11-remediations.md`; `sbmig remediation-resume
--run-dir <dir>` resumes outside Kubernetes.

**Remediation only** (a project migrated earlier, checked again): `sbmig run <zip|dir> --config config/artifacts.yml
--flow remediation` on a PC, or the page's 취약점 해결 choice (in the cluster an ordinary build Job in the same queue; nothing
to set in the manifests). No migration step, no LLM call, no KB access: the upload is built as it is (a project that does
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
| `script.max_llm_tries_per_case` / `script.max_augments_per_case` | 20 / 15 | LLM asks per case and run; then BLOCKED `repeated_failure` |
| `script.max_declines_per_case` | 1 | an LLM decline → BLOCKED `needs_owner` |
| `script.max_edit_tries_per_case` | 4 | `<no_script>` + edits answers per case |
| `loop.blocked_policy.<reason>` | 0–2 | script attempts before a case of that reason is blocked (`repository_unreachable`: 0) |
| `intake.max_unpacked_mb` / `intake.max_entry_mb` / `intake.max_compression_ratio` | 4096 / 1024 / 200 | unpacking limits of an uploaded ZIP (0 = no limit) |
| `llm.escalation_model` (`LLM_ESCALATION_MODEL`) | empty = `llm.model` | stronger model from a case's third ask or after a stall |
| `llm.case_thread` | false | agent gateway: augments continue the case's session with the delta only |
| `llm.pricing.<model>` | list prices | per-run cost in the report and the fleet summary |
| `build.skip_test_compile` | true | `-Dmaven.test.skip=true` on every build |
| `build.require_java_major` | 21 | preflight: abort when Maven runs on another Java |
| `build.retry_negative_cache` | true | a build that failed on Maven's negative cache runs once more with `-U` |
| `build.file_lock_retries` | see yml | retries when Windows locks `target/` (then `infra:file-lock`) |
| `plan.advisor` | none | optional LLM plan review (additive, catalog-only; [RECIPES.md](RECIPES.md)) |
| `openrewrite.download_retry` / `download_retry_wait_s` | 1 / 30 | recipe run retried after a download failure |
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
