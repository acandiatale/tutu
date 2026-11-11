# Runbook — operating sbmig in the company environment

## 0. Prerequisites and what to validate on a new host

| Item | Requirement | Check |
|---|---|---|
| Python | ≥ 3.10 (+ `uv` or `pip`); `script.python` (default: the interpreter running sbmig) must have pydantic and lxml | `python --version` |
| JDK | **21** (`build.java_home` or `--java-home`). The as-is project is never built on its old JDK | the run preflight compares `mvn -v` with `build.require_java_major` (21); a mismatch aborts with exit 3, flag `jdk-not-21` |
| Maven | 3.9.x; `build.settings_file` = a settings.xml with a **catch-all mirror** to the Nexus group (§0.1) | `mvn -v`; `sbmig repo-probe` |
| Maven HTTP blocker | Maven ≥ 3.8.1 refuses `http://` repositories through `maven-default-http-blocker` in `<maven home>/conf/settings.xml` (`mvn -v` prints the Maven home). If a company repository is `http://`, the user settings must redefine that mirror id (`<mirrorOf>none</mirrorOf>`) or mirror the repository with `<blocked>false</blocked>` | the run's preflight WARNING `Maven's HTTP blocker covers …`; a blocked run ends `attn=infra:http-blocked,aborted:http-blocked` with the fix in the Handover's first line |
| Nexus | proxies Maven Central incl. `org.openrewrite.*` (rewrite-maven-plugin 6.46.1, `rewrite-recipe-bom` **3.37.0** → rewrite-migrate-java 3.42.0, rewrite-spring 6.37.0, …), `org.springframework.boot:*:4.1.0`, the jakarta artifacts, the in-house parents | `sbmig recipes check --jars -c config/artifacts.yml` |
| LLM gateway | `.env` with `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` (agent gateway: + `LLM_GATEWAY=agent`, `LLM_ASSET_ID`, `LLM_TOKEN_URL`, `LLM_APP_TOKEN`) — see [ADAPTERS.md](ADAPTERS.md) | `sbmig llm-probe` → last line `LLM-PROBE ok …` |
| Company config | `config/artifacts.yml` from `config/artifacts.example.yml`: `base-parent`, `boot-jar` / `comp-jar` role parents, the 2 mandatory and 8 optional in-house artifacts | `sbmig analyze <project> -c config/artifacts.yml` |
| git | any | per-attempt commits / rollbacks / `changes.patch` |

Validate in this order on the company PC: `sbmig llm-probe` → `sbmig repo-probe <g:a:v> --project <dir>` →
`sbmig analyze` → one `sbmig run` on a small project → read `SBMIG-RESULT` and the report's Handover.

### 0.1 Repositories declared inside the project poms

Legacy poms declare `<repositories>` of their own (an old Nexus host, Central, Spring milestones); on an air-gapped
host every one costs a resolution failure or a timeout. Copy `config/settings.nexus.example.xml`, set the group URL
and point `build.settings_file` at it: its `<mirrorOf>external:*</mirrorOf>` rewrites every remote repository —
the poms' included — to the group, and Maven's default `http://` blocker is overridden for it. sbmig passes `-s` to
every Maven call. The pom URLs are still read for the jakarta-library lookup (`sbmig repo-probe` lists them unless
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

## 1. Running a migration

```bash
sbmig run path/to/project.zip --config config/artifacts.yml --out out/
#  --stop-after intake|analyze|plan|transform|restructure|verify   stop early (still writes a report)
#  --dry-run                       compute and journal decisions, apply nothing
#  --java-home <jdk21> --settings <settings.xml> --llm-provider <p> --fix-plugin pkg.mod:attr
```

The flow: intake → analyze → plan → one OpenRewrite run → company restructure → LLM structure review →
`mvn clean package -Dmaven.test.skip=true` → on failure the script fix loop (stored script reuse → augment → new;
every attempt rebuilt, scored, rolled back if it did not help) → final build → report. Tests are never compiled
(`build.skip_test_compile: true`, flag `tests-not-compiled`).

Inputs: a Maven tree (a missing root pom gets a generated aggregator), or an Ant tree (`build.xml`, no pom: a pom is
synthesized at intake — set `intake.synth_group_id`; company jar coordinates can be added to `<kb>/jar_names.yml`).
Gradle and trees with neither pom nor build.xml are rejected (exit 3, flag `unsupported-build-tool:<tool>`); for
the latter `sbmig inventory <dir> --out out/inventory/<name>` writes a read-only description for the owners. Oracle
ADF projects stop at analyze (exit 3, flag `adf-project`).

**Exit codes**: `0` final `mvn clean package` passed and nothing is blocked; `2` finished with blocked and / or
unresolved errors (or `hollow-reactor`); `3` aborted (no Maven, Maven not on Java 21, unsupported input, ADF,
in-house parent not resolvable); `1` crash. Library advisories never change the exit code. The last console line
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
| `report.md` / `report.json` | **Handover** first, then error list, fixes by case, structure review, blocked items, manual follow-ups, flags, what changed, LLM usage and cost, Notes (why the fix loop ended) |
| `errors.md` / `errors.jsonl` | one entry per error with every try (cause, solution, edits, diff, result) |
| `journal.jsonl` | machine-readable event log (decisions, attempts, scores, blocks, LLM calls) |
| `sbmig.log` | DEBUG log, one stage tag per line (`[fix r3 <case_id>]`) |
| `logs/NNN-<label>.log` | every Maven / OpenRewrite call — the real error messages |
| `project/` | the migrated working copy (git branch `sbmig/<run id>`, one commit per step / attempt) |
| `changes.patch`, `result.zip` | full diff vs as-is, the migrated project |

`report.language: ko` writes `report.md` in Korean. LLM requests / responses go to `llm.record_dir` (`LLM_RECORD_DIR`)
as `llm-NNNN.json`, without keys.

## 2. Reading a run

1. **`SBMIG-RESULT`**: `exit`, `build`, `attn`. `infra:` means the environment (LLM, Maven, repository, file lock),
   not the code.
2. **`report.md`**: the Handover (what the owning team must know), Notes (the sentence saying why the loop ended,
   e.g. `max iterations (50) reached with N error(s) left`), "Blocked — needs human / library action" (reason and
   suggestion per item), "Fixes by case".
3. **`errors.md`**: the tries of one error; reverted attempts stay listed here with their diff.
4. **`journal.jsonl`**: the case's `decision` (ladder step, KB lookup), `attempt_scored`, `revert`, `stall` /
   `oscillation`, `library_hints`, `declined` / `decline_rejected`.
5. **`logs/`**: the highest-numbered `NNN-build-verify*.log` — the `[ERROR]` lines, javac's `symbol:` / `location:`.
6. **`project/`**: `git log --oneline` pairs attempt commits (`fix(script:<case_id>:v<N> …)`) with rollbacks.
7. **LLM records**: was a fact missing from the prompt, did the LLM ignore it, or did it decline?

Details per file, log line and journal event: `agent-doc/03-run-debug-and-logs.md`. `sbmig parse-log <log>`
re-parses any Maven log into errors with signatures.

**Handover sections**: Versions replaced (parent / Java / Boot as-is → final, every changed dependency; **⚠ carried
over** = pinned at the version the as-is parent managed because the new chain does not — confirm or replace), major
upgrades to check at runtime, Libraries needing your attention (advisories, see [ADVISORIES.md](ADVISORIES.md)),
Properties to set before running (keys the external Tomcat used to supply: JNDI, context-param, `-D`; written as
`key=__SET_ME__` placeholders in the boot module's `application.properties`), and the numbered "From here on, your
team" list (`advisories.max_from_here`). Runtime behaviour is never verified by sbmig; before deploying, the owners
check `grep -rn __SET_ME__` and supply the values outside the jar (env variables, `-D`, an external
`application.properties`).

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
HTTP 400 body for two prompts stops the LLM for the run (`llm-config-error`; check the model names in `.env`).

On the company PC without copy-paste, ask for specific lines (`SBMIG-RESULT`, `preflight: … FAIL`, `run aborted:`,
`cause=`, `LLM-PROBE`, `REPO-PROBE`), each found with one `Select-String -Encoding UTF8` command — not for files.

## 3. Knowledge base (`kb.dir`, files only)

- Layout: `errors.jsonl` (every run's error list), `cases.md`, `cases/<case_id>/case.json` + `fix_v<N>.py` (the
  stored fix scripts per error case, with lineage and successes / failures / projects). Default `kb-v2` inside the
  checkout; in production set an absolute path outside it. Tests and demos write under `out/` only.
- Inspect: `sbmig kb cases list`, `sbmig kb cases show <case_id>`, `sbmig kb errors list|show`, `sbmig kb stats`,
  `sbmig kb validate`.
- Curate: `sbmig kb cases retire <case_id> <N>` (never run version N again), `activate <case_id> <N>` (undo),
  `pin <case_id> block[:REASON]` (no script / LLM tries; the Handover shows the reason), `unpin`.
- Move / back up: `sbmig kb export kb-YYYYMMDD.zip` → `sbmig kb import kb-YYYYMMDD.zip [--replace]` (the old KB is
  kept as `.bak-*`); `sbmig kb reindex` after manual edits. Back up with export, not git.
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
  follow the stage rail / journal / log, download `report.md` and `result.zip`. Sharing on the LAN needs a token
  (`SBMIG_WEB_TOKEN` or `--token`; `--open` only on a trusted intranet). The configs every upload runs with are the
  server's choice (`--config` / `SBMIG_WEB_CONFIGS`). In-process mode runs one migration at a time (the others are
  `queued`); "중단" takes effect between graph nodes.
- **Containers / Kubernetes**: `deploy/README.md` — the image, the web pod, and `sbmig serve --mode k8s-job`
  (one build Job per upload, up to `SBMIG_JOB_MAX_PARALLEL` at a time, the shared file KB, env pins). Operator rule:
  after changing ConfigMap `sbmig-env` restart the web pod; after a new Job template re-create ConfigMap
  `sbmig-job-template`. Operations summary for in-house LLMs: `agent-doc/09-deploy-and-ops.md`.

## 6. Vulnerability remediation after a migration

When a migration ends BUILD SUCCESS and `remediation.after_migration` is on (shipped on in the Kubernetes manifests,
off in the code), the same build pod scans the migrated project with IVAS, changes only the version text of
vulnerable libraries in the poms (a managed-version override always waits for approval), rebuilds and rescans. Files
go to `<run dir>/remediation/`; the outcome never changes the migration's exit code and is appended to `SBMIG-RESULT`
as `remediation=<STATUS|skipped>`. Approvals and resume (a new build pod) happen in the web UI. Operator text:
`deploy/README.md` §6; flow, states, API and diagnosis: `agent-doc/11-remediations.md`; `sbmig remediation-resume
--run-dir <dir>` resumes outside Kubernetes.

## 7. Tuning knobs (`config/default.yml`, Korean comments there)

| Knob | Default | Effect |
|---|---|---|
| `loop.phases.verify.max_iterations` | 50 | fix-loop rounds (one round = one build) |
| `loop.max_llm_calls`, `loop.max_total_minutes` | 0 (no cap) / 240 | budgets per run |
| `loop.stall_limit` | 2 | the same error set N times → escalation model |
| `script.max_llm_tries_per_case` / `script.max_augments_per_case` | 20 / 15 | LLM asks per case and run; then BLOCKED `repeated_failure` |
| `script.max_declines_per_case` | 1 | an LLM decline → BLOCKED `needs_owner` |
| `script.max_edit_tries_per_case` | 4 | `<no_script>` + edits answers per case |
| `loop.blocked_policy.<reason>` | 0–2 | script attempts before a case of that reason is blocked |
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
| `advisories.*` | see yml | advisory files, off switches, carried-over listing, Handover cap |
| `kb.dir` / `kb.lock_timeout_seconds` | kb-v2 / 300 | KB folder, cross-process lock |
| `fleet.*` | see [FLEET.md](FLEET.md) | fleet order, budgets, infrastructure guard, cleanup |
| `report.language` | en | `ko` writes `report.md` in Korean |

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

`build.xml` 만 있는 프로젝트는 그대로 넣으면 됩니다. 접수가 pom.xml 을 합성해 커밋하고 나머지는 같습니다. 실행 전에 두 가지만:
- 회사 설정 yml 에 `intake: {synth_group_id: <사내 groupId>}` 를 넣습니다(기본값은 자리표시자).
- 사내 공용 jar 가 `lib/` 에 많으면 `<kb>/jar_names.yml` 에 `{stem, artifact}` 행을 적어 두면 좌표를 바로 찾습니다(형식은
  `knowledge/libraries/jar_names.yml` 과 같고, 재배포 없이 다음 실행부터 반영).
실행 뒤 보고서 맨 앞 "Ant 프로젝트: pom.xml 합성" 블록에 좌표를 못 찾은 jar 와 `mvn install:install-file …` 명령이 있으면 Nexus 에
올리고 pom 의 `system` 항목을 보통 의존성으로 바꿔 주십시오(빌드는 통과하지만 system 범위 jar 는 WAR 에 포장되지 않습니다).
`build.xml` 의 코드 생성·복사 규칙·여러 WAR 타깃은 번역하지 않고 같은 블록에 "번역하지 않은 타깃" 으로 적습니다.
