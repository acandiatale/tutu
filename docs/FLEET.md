# Fleet: one agent, many projects, sequential learning

`sbmig fleet` migrates a list of projects **one after the other** with a single agent and a single file knowledge
base (KB). Nothing runs in parallel: every project's error list and fix scripts are in the KB before the next project
starts, so project *k+1* runs the script project *k* stored for the same error case first, without an LLM call.

## Workflow

```bash
# 1. manifest: a directory, a .txt (one path per line), a .yml or a .csv (name,input,tags)
cat fixtures/fleet/manifest.example.yml

# 2. run (resumable by default)
sbmig fleet run fleet.yml --config config/artifacts.yml --out out/fleet

# 3. progress at any time (also while it runs, from another shell)
sbmig fleet status out/fleet
sbmig fleet status out/fleet --attention     # only the projects that need a human, grouped by attention code

# 4. after a crash / Ctrl-C / reboot / an infrastructure stop: continue where it stopped (same manifest + configs)
sbmig fleet resume out/fleet
sbmig fleet resume out/fleet --retry-failed  # also re-run incomplete / aborted projects

# 5. disk space: what finished runs left behind
sbmig clean --fleet out/fleet --older-than 7 --keep-failed --dry-run
```

Manifest forms:

| form | content |
|---|---|
| directory | every `*.zip` and every subdirectory with a `pom.xml` at its top or one level down (a folder with neither is logged and left out), sorted by name |
| `.txt` | one path per line, `#` comments |
| `.yml` | `projects: [{name, input, configs: [...], overrides: {...}, tags: [...]}]` |
| `.csv` | header `name,input,tags` |

Paths are relative to the manifest file. Manifests are read in UTF-8 (with or without BOM), UTF-16 or cp949. Project
names become run ids and directory names, so they are ASCII: a non-ASCII name (`주문관리-v2`) becomes its ASCII slug
plus a short hash of the input path, and a colliding name gets the hash too. Per-project `configs` / `overrides` are
merged over the fleet config, except `kb.dir`: the KB is always fleet-wide.

A missing input is `skipped` (it runs on a later resume once it exists). A project rejected at intake as an
unsupported build tool (Gradle, no build file; flag `unsupported-build-tool:<tool>`) is `skipped` too: the reason is
in the row's `error`, `--retry-failed` never re-runs it and `--stop-on-abort` does not stop the fleet for it.

`sbmig fleet run` exit codes: `0` all projects succeeded, `2` some are incomplete / skipped / not run yet, `3` stopped
on an aborted project (`--stop-on-abort`) or by the infrastructure guard (state saved; a plain re-run resumes).
`--dry-run` prints the order and the resume decision per project without running anything.

## What is persisted where

| path | content |
|---|---|
| `<out>/fleet/state.json` | `FleetState`: one `FleetProjectResult` per project (status, exit code, run id, LLM calls / tokens / cost summed over every attempt, blocked reasons, error counts, flags, advisories, carried-over count), rewritten atomically after every project. Projects are identified by their normalized input path (`manifest.input_key`), not by name |
| `<out>/fleet/summary.md` / `.csv` / `.json` | the progress table + totals, rewritten after every project. `summary.csv` is UTF-8 with BOM and has the `attention` and `input` columns; `summary.md` starts with "Needs attention" and has "Libraries flagged across the fleet". A file Excel keeps open is skipped with a log line |
| `<out>/fleet/learning.md` | this fleet's error-list counts and the KB cases its projects met, with every stored script version (status, successes / failures) |
| `<out>/fleet/sbmig.log` | the fleet's INFO log (preflight, one `SBMIG-RESULT` line per project, infrastructure waits) |
| `<out>/fleet/fleet-config.json` | manifest + config paths + overrides of the last `run` (used by `resume`) |
| `<out>/<fleet-id>-<NNN>-<name>/` | the normal run directory of each project (`report.md`, `report.json`, `result.zip`, `journal.jsonl`, `errors.md`, its own DEBUG `sbmig.log`); `fleet.cleanup` thins it after the report |
| `<kb.dir>/errors.jsonl`, `cases.md`, `cases/<case_id>/` (`case.json` + `fix_v<N>.py`) | the KB, shared with single runs and the web UI |
| `<kb.dir>/.lock` | cross-process KB lock: every write waits for it (`kb.lock_timeout_seconds`; stale locks are broken) |

Resume rules: `success` projects are never re-run; `incomplete` / `aborted` only with `--retry-failed`
(`fleet.retry_failed`); a project left in `running` (the fleet process died) is re-run; a project deferred by the
infrastructure guard (`pending`, `error: infra:<kind>`) is re-run first. Retries get a new run id (`…-r2`) and
`attempt` is incremented.

## Reuse of stored scripts across projects

Each fix attempt is a Python script stored per error case (L2 signature) in `<kb.dir>/cases/<case_id>/fix_v<N>.py`
with lineage and credits in `case.json`. On a later project the fix loop tries, per case: **reuse** the best stored
script (no LLM) → **augment** this run's last script (LLM) → a fresh **new** script (LLM). Every attempt is scored
by a rebuild and credited to the stored version (successes / failures / projects; a version that works elsewhere
becomes `proven`). `fleet.reindex_every` re-indexes the KB every N projects. Inspect it with
`sbmig kb cases list` / `sbmig kb cases show <case_id>`; `retire`, `activate`, `pin`, `unpin` curate it. Details:
`agent-doc/04-cases-scripts-kb.md`.

## Infrastructure guard

500 projects in a row must not all fail because the gateway, Nexus, the JDK or the disk is gone for an hour.

- **Preflight** (`fleet.preflight`, `sbmig/fleet/preflight.py`): before the first project — and again before
  resuming after a pause — one ASCII line per check, `preflight: <check> ok|FAIL|skip <detail>`: `disk` (free space vs
  `fleet.min_free_gb`), `maven` (`mvn -v`), `java` (the major Maven runs on vs `build.require_java_major`), `settings`
  (`build.settings_file` exists, its catch-all mirror), `repository` (one GET to that mirror through a settings
  `<proxy>`; any answer below 500 counts as up), `llm` (one probe call, the same as `sbmig llm-probe`; skipped for the
  fake provider). A failure names its kind (`disk | maven | jdk | repository | llm`) and whether it can recover by
  itself (an outage, a full disk) or is a configuration error (no Maven, another JDK, settings missing, key rejected).
- **Breaker**: a project whose run ends with an `infra:<kind>` flag (or `llm-unavailable` / `llm-config-error` /
  `mvn-not-found` / `jdk-not-21` on a run that is not green), or crashes with an infrastructure exception, is not
  counted as failed: it goes back to `pending` with `error: infra:<kind>`. After `fleet.infra_failure_limit` such
  projects in a row the fleet prints `fleet: infrastructure <kind> unavailable since <project> - waiting N min, then
  re-checking (i/max)`, waits `fleet.infra_retry_minutes`, re-runs the preflight and resumes with the deferred
  projects. After `fleet.infra_max_waits` waits without a healthy project, at once with `infra_retry_minutes: 0`, or
  when the re-check finds a configuration error, it stops: `fleet stopped: infrastructure <kind> unavailable since
  <project> - fix it and run sbmig fleet again` (exit 3, state saved).
- **Disk**: before each project the free space is checked (`fleet.min_free_gb`; below → kind `disk`). After each
  project's report and `result.zip`, `fleet.cleanup` runs (`sbmig/fleet/cleanup.py`): `build-output` (default)
  deletes the `target/` directories next to a `pom.xml` in the run copy; `workspace` also deletes `<run dir>/project`
  when `result.zip` exists, keeping `result.zip`, `report.*`, `changes.patch`, `journal.jsonl`, `sbmig.log` and
  `logs/`; `none` keeps everything.
- **`sbmig clean [--out DIR] [--fleet DIR] [--older-than DAYS] [--keep-failed] [--dry-run] [--records DIR]`** frees
  the project copies of finished runs, the web UI's uploads, stale `.part` uploads and the LLM records
  (`llm-*.json`); running / queued runs are never touched.

## The one-line result and attention codes

Every project prints (and the fleet log records) one ASCII line operators can retype from a cp949 console:

```
SBMIG-RESULT run=<id> exit=<n> build=<ok|FAIL> unresolved=<n> blocked=<n> open=<n> resolved=<n> llm=<calls> attn=<codes|->
```

`sbmig run` ends with the same line (with ` review=…` / ` major=…` / ` remediation=…` appended when they apply). The
codes (`sbmig/report/result_line.py`), most important first: `infra:<kind>`, `skipped:<why>`, `crash`,
`aborted[:<reason>]`, `owner-decision`, `blocked:<reason>`, `unresolved:<n>`, then the flags `restructure-failed`,
`llm-budget-exhausted`, `time-budget-exhausted`, `javax-api-on-classpath`, `tests-not-compiled`, `major-upgrade`,
`jar-coordinates-unknown`, `parent-chain-no-boot-bom`, `hollow-reactor`, `adf-project`, … and `incomplete` (exit 2
with none of the above). A green run never gets `infra:`. They fill `summary.csv`'s `attention` column, "Needs
attention" in `summary.md`, and `sbmig fleet status --attention`.

## Advisories and carried-over versions in the summary

`summary.md` / `summary.csv` have the columns `advisories` (library advisory findings; outcomes in `state.json` as
`"<advisory_id>:<outcome>"`) and `carried_over` (dependencies pinned at the version the as-is parent managed).
`summary.md` adds **Libraries flagged across the fleet** (advisory → projects, replaced, still present). Each
project's `report.md` starts with the **Handover** section for the owners. See [ADVISORIES.md](ADVISORIES.md).

## Knobs

| config | default | meaning |
|---|---|---|
| `fleet.order` | `manifest` | `simple-first`: pure_java → servlet → spring_legacy → boot → msa |
| `fleet.stop_on_abort` | `false` | stop the fleet when a project aborts (exit 3) |
| `fleet.retry_failed` | `false` | re-run incomplete / aborted projects on resume |
| `fleet.max_projects` | `0` | run at most N projects in this invocation (0 = all) |
| `fleet.max_llm_calls_total` | `0` | fleet-wide LLM budget; each project gets `min(loop.max_llm_calls, remaining)` |
| `fleet.reindex_every` | `10` | KB re-index after every N projects (0 = never) |
| `fleet.summary_formats` | `[md, csv, json]` | summary files (learning.md is always written) |
| `fleet.preflight` | `true` | infrastructure checks before the first project and on every re-check |
| `fleet.infra_failure_limit` | `3` | projects in a row that ended for an infrastructure reason before the fleet waits |
| `fleet.infra_retry_minutes` | `15` | wait before re-checking (`0` = stop at once, state kept) |
| `fleet.infra_max_waits` | `4` | re-checks without a healthy project before the fleet stops |
| `fleet.cleanup` | `build-output` | after each project: `none` \| `build-output` \| `workspace` |
| `fleet.min_free_gb` | `5` | free space on the output drive before each project (`0` = no check) |
| `kb.dir` | `kb-v2` | the shared KB folder; in production an absolute path outside the checkout |
| `kb.lock_timeout_seconds` | `300` | wait for the KB lock; past it only that write is given up (logged) |

CLI flags of `fleet run`: `--order`, `--max-projects`, `--retry-failed`, `--stop-on-abort`,
`--max-llm-calls-total`, `--llm-provider`, `--dry-run`, `--config` (repeatable), `--out`. `fleet status` takes
`--attention` and `--json`.

Offline demo: `scripts/fleet-demo.sh` (a fresh KB under `out/`, replayed LLM answers): a later project reuses the
script an earlier one stored.
