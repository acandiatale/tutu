# Fleet: one agent, many projects, sequential learning

`sbmig fleet` migrates a list of projects **one after the other** with a single agent and a single knowledge base
(KB). Nothing runs in parallel: every project's fixes are harvested into the KB before the next project starts,
so project *k+1* already benefits from what projects *1…k* taught the agent.

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
sbmig fleet resume out/fleet                 # = sbmig fleet run fleet.yml --config … --out out/fleet
sbmig fleet resume out/fleet --retry-failed  # also re-run incomplete / aborted projects

# 5. disk space (round 10): what finished runs left behind
sbmig clean --fleet out/fleet --older-than 7 --keep-failed --dry-run
```

Manifest forms:

| form | content |
|---|---|
| directory | every `*.zip` and every subdirectory containing a `pom.xml`, sorted by name |
| `.txt` | one path per line, `#` comments |
| `.yml` | `projects: [{name, input, configs: [...], overrides: {...}, tags: [...]}]` |
| `.csv` | header `name,input,tags` |

Paths are relative to the manifest file. Manifest files are read whatever the editor saved (round 10): UTF-8 with or
without BOM (Excel "CSV UTF-8"), UTF-16 (PowerShell 5.1 `>`), else cp949. Project names become run ids and directory
names, so they are ASCII: a name with non-ASCII characters (`주문관리-v2`) becomes its ASCII slug plus a 6-character
hash of the input path (`v2-1a2b3c`), and a name that collides with an earlier one gets the hash too; plain ASCII
names are unchanged. A missing input is marked `skipped` (it runs on a later resume once it exists); it never stops
the fleet. A Gradle / Ant / no-build project rejected at intake (flag
`unsupported-build-tool:<tool>`, exit 3) is `skipped` too (round 9): the reason is in the row's `error`, it is never
re-run by `--retry-failed`, and `--stop-on-abort` does not stop the fleet for it. Per-project `configs` / `overrides` are merged over the fleet config, except
`kb.dir`: the KB is always fleet-wide.

`sbmig fleet run` exit codes: `0` all projects succeeded, `2` some are incomplete / skipped / not run yet,
`3` stopped on an aborted project (`--stop-on-abort`) or by the infrastructure guard (state saved; a plain re-run
resumes). `--dry-run` prints the order and the resume decision per project without running anything.

## What is persisted where

| path | content |
|---|---|
| `<out>/fleet/state.json` | `FleetState`: one `FleetProjectResult` per project (status, exit code, run id, LLM calls, blocked reasons, KB cases/candidates/promotions/used). Rewritten atomically after every project. Projects are identified by their normalized input path (`manifest.input_key`: absolute, case-folded on Windows), not by the name — a renamed or added folder never inherits another project's success; older state files match unchanged. |
| `<out>/fleet/summary.md` / `.csv` / `.json` | the progress table (+ totals, "still running: X"), rewritten after every project. Round 10: `summary.csv` is UTF-8 with BOM and has the columns `attention` (`;`-separated codes, below) and `input`; `summary.md` starts with "Needs attention" (projects grouped by their first code); each file is written on its own, so a `summary.csv` Excel keeps open is skipped with a log line and the others are still written |
| `<out>/fleet/sbmig.log` | the fleet's INFO log (preflight, one `SBMIG-RESULT` line per project, infrastructure waits) |
| `<out>/fleet/learning.md` | what the KB learned during this fleet (see below) |
| `<out>/fleet/fleet-config.json` | manifest + config paths + overrides of the last `run` (used by `resume`) |
| `<out>/<fleet-id>-<NNN>-<name>/` | the normal run directory of each project (`report.md`, `report.json`, `result.zip`, journal, logs, and since round 10 its own DEBUG `sbmig.log`); `fleet.cleanup` thins it after the report (below) |
| `<kb.dir>/cases/…`, `recipes/…`, `index.json` | the KB (as for single runs) |
| `<kb.dir>/ledger.jsonl` | append-only resolution ledger, one line per resolved / blocked / unresolved cluster |
| `<kb.dir>/symbols.json` | error-case symbol → how it was fixed, in how many projects |
| `<kb.dir>/.lock` | round 10: cross-process KB lock — the fleet, the web server and `sbmig kb …` may share one KB; every write waits for it (`kb.lock_timeout_seconds`, stale locks are broken) |

Resume rules: `success` projects are never re-run; `incomplete` / `aborted` only with `--retry-failed`
(`fleet.retry_failed`); `skipped` for an unsupported build tool stays skipped; a project left in `running` (the
fleet process died) is re-run; a project deferred by the infrastructure guard (`pending`, `error: infra:<kind>`) is
re-run first. Retries get a new run id (`…-r2`) and `attempt` is incremented.

## Infrastructure guard (round 10)

500 projects in a row must not all fail because the gateway, Nexus, the JDK or the disk is gone for an hour.

* **Preflight** (`fleet.preflight`, `sbmig/fleet/preflight.py`): before the first project — and again before resuming
  after a pause — one ASCII line per check, `preflight: <check> ok|FAIL|skip <detail>`: `disk` (free space on the
  output drive vs `fleet.min_free_gb`), `maven` (`mvn -v`), `java` (the major Maven runs on vs
  `build.require_java_major`), `settings` (`build.settings_file` exists, its catch-all mirror), `repository` (one GET
  to that mirror the way Maven goes: a settings `<proxy>` is used, environment proxies are not; any answer below 500
  counts as up, an unverifiable TLS handshake too), `llm` (one `probe_llm` call, the same as `sbmig llm-probe`;
  skipped for the fake provider or `loop.max_llm_calls: 0`). A failure names its kind (`disk | maven | jdk |
  repository | llm`) and whether it can recover by itself (an outage, a full disk) or is a configuration error (no
  Maven, Java 17, settings file missing, LLM key / model rejected).
* **Breaker**: a project whose run ends with an `infra:<kind>` flag (docs/ERROR-HANDLING.md X12; also the older
  `llm-unavailable` / `llm-config-error` / `mvn-not-found` / `jdk-not-21` on a run that is not green) or crashes with
  an infrastructure exception (full disk, requests / LLM error, mvn missing) is not counted as failed: it goes back
  to `pending` with `error: infra:<kind>`. After `fleet.infra_failure_limit` (3) such projects in a row the fleet
  prints `fleet: infrastructure <kind> unavailable since <project> - waiting N min, then re-checking (i/max)`, waits
  `fleet.infra_retry_minutes` (15), re-runs the preflight and resumes with the deferred projects (each is re-run at
  most twice per fleet process). After `fleet.infra_max_waits` (4) waits without a healthy project in between, at
  once with `infra_retry_minutes: 0`, or at once when the re-check finds a configuration error, it stops with one line
  `fleet stopped: infrastructure <kind> unavailable since <project> - fix it and run sbmig fleet again` (exit 3,
  state saved).
* **Disk**: before each project the free space is checked (`fleet.min_free_gb`, 5; below → kind `disk`, handled like
  an infrastructure failure). After each project's report and `result.zip`, `fleet.cleanup` runs
  (`sbmig/fleet/cleanup.py`): `build-output` (default) deletes the `target/` directories next to a `pom.xml` in the run
  copy (a Java package named `target` is never touched); `workspace` also deletes `<run dir>/project` (git history
  included) when `result.zip` exists, keeping `result.zip`, `report.*`, `changes.patch`, `journal.jsonl`, `sbmig.log`
  and `logs/`; `none` keeps everything.
* **`sbmig clean [--out DIR] [--fleet DIR] [--older-than DAYS] [--keep-failed] [--dry-run] [--records DIR]`**: frees the
  project copies of finished runs, the web UI's uploads of finished runs, stale `.part` uploads and the LLM records
  (`llm-*.json`); running / queued runs are never touched; prints count and size per kind, then `would free …` /
  `freed …`.

## Attention codes and the one-line result (round 10)

Every project prints (and the fleet log records) one ASCII line operators can retype from a cp949 console:

```
SBMIG-RESULT run=<id> exit=<n> build=<ok|FAIL> unresolved=<n> blocked=<n> llm=<calls> attn=<codes|->
```

`sbmig run` ends with the same line (`attn=crash` when it raised), and so does a web run's `sbmig.log`. The codes
(`sbmig/report/result_line.py`), most important first: `infra:<kind>`, `skipped:<tool|input>`, `crash`,
`aborted[:<reason>]`, `blocked:<reason>` (one per distinct reason), `unresolved:<n>`, `restructure-failed`,
`llm-budget-exhausted`, `time-budget-exhausted`, `javax-api-on-classpath`, `tests-skipped`, `test-compile-skipped`,
`tests-not-compiled`, `incomplete` (exit 2 with none of the above). A green run (exit 0) never gets `infra:`. They
fill `summary.csv`'s `attention` column, the "Needs attention" section of `summary.md`, and `sbmig fleet status
--attention` (only those projects, grouped by their first code, ≤ 50 per code).

## The learning loop across projects

```
project k:   build error ─▶ LLM fix ─▶ build improves ─▶ KB case
                                               │  labels (deterministic, sbmig.kb.labels):
                                               │    symbol "JsonComponent", labels [annotation-replace, type-rename],
                                               │    replacement "…jackson.JsonComponent → …jackson.JacksonComponent"
                                               ├─▶ mechanical & 1:1?  ─▶ recipe candidate COMPILED from the labels (no LLM)
                                               │   otherwise          ─▶ LLM recipe candidate (recipe_model), or none
                                               ├─▶ symbols.json + ledger.jsonl
project k+1: plan runs candidate recipes in its OpenRewrite step (kb.try_candidates_in_plan)
             → phase build OK → candidate credited for project k+1
             → success in kb.auto_promote_after_projects distinct projects, no failure → ACTIVE
project k+2…: active recipe is part of every plan; the error never reaches triage, 0 LLM calls.
             Errors not covered by a recipe still get the symbol hint in their LLM prompt.
```

* **Fix labels** (`FixLabel` in `sbmig/core/models.py`) describe *how* a fix changed the code:
  `import-rename`, `type-rename`, `package-rename`, `annotation-replace`, `method-rename`,
  `method-signature-adapt`, `override-signature`, `dependency-add/-remove/-replace/-version`, `plugin-version`,
  `pom-property`, `spring-property-rename`, `config-value`, `remove-code`, `add-code`, `generic-rewrite`.
  They are computed from the `FileChange` diff (pom fragments, `.properties`/`.yml` keys, Java import / token
  diffs) — no LLM, most specific first.
* **Deterministic recipe candidates** (`kb.deterministic_candidates`, default on): `compile_recipe(case)` emits a
  whitelisted declarative recipe (`ChangeType`, `ChangePackage`, `ChangeMethodName`, `AddDependency`,
  `ChangeDependency`, `UpgradeDependencyVersion`, `UpgradePluginVersion`, `ChangePropertyValue`,
  `ChangeSpringPropertyKey`) only for a single, confident 1:1 change. Project-owned types (outside
  `EXTERNAL_PACKAGE_PREFIXES` + the in-house prefixes) never become recipes. Anything else falls back to the LLM
  candidate prompt (which now also shows the labels).
* **`method-rename` → `ChangeMethodName`** (round 5): all call sites (`X.old(` and method references `x::old`) must
  agree on `old → new`; the recipe is `methodPattern: "<owner FQCN> <old>(..)"`, `newMethodName`,
  `matchOverrides: true`, named `<OwnerSimple><Old>To<New>`. javac prints imported types by their
  simple name (`location: class LegacyRequestHelper`) and argument types too, so harvest resolves the owner into
  `KBCase.symbol_owner` from the changed files' imports (explicit import, then wildcard via `locate_class`, then
  same package) and the pattern never carries argument types.
* **Promotion** is unchanged: a candidate becomes active after success in `kb.auto_promote_after_projects`
  distinct projects without a failure; `HarvestResult.recipes_promoted` / `FleetProjectResult.kb_recipes_promoted`
  record when it happened. `sbmig kb promote / reject` stay available for manual curation.

## The ledger

`kb/ledger.jsonl` holds one `ResolutionRecord` per attempt (`handler`, `kb_recipe`, `kb_patch`, `llm_patch`, with
the attempt outcome), per KB recipe that changed files in a plan's OpenRewrite step (`plan_recipe`), per blocked
item (`blocked`, `resolver_id` = block reason, `symbol` = owner class or artifact `g:a`) and per remaining error
signature at the end of a run (`unresolved`). File names are basenames; no absolute paths or secrets. Example
(from `scripts/fleet-demo.sh`):

```json
{
 "id": "3ecc753544c5a058",
 "at": "2026-10-18T04:49:10.613055Z",
 "project": "order-api",
 "project_fingerprint": "04ec0c98792d3471",
 "run_id": "fleet-20261018-134722-003-boot3",
 "case_type": "spring_boot",
 "phase": "spring",
 "signature": "compile|cannot_find_symbol|class JsonComponent|package org.springframework.boot.jackson",
 "signature_generic": "compile|cannot_find_symbol|JsonComponent",
 "error_kind": "compile",
 "symbol": "JsonComponent",
 "resolution": "llm_patch",
 "resolver_id": "fake",
 "fix_labels": [
  "annotation-replace",
  "type-rename"
 ],
 "replacement": "org.springframework.boot.jackson.JsonComponent → org.springframework.boot.jackson.JacksonComponent",
 "outcome": "build_ok",
 "files": [
  "MoneyJsonComponent.java"
 ],
 "case_id": "20261018-f16da61b53",
 "recipe_id": "JsonComponentToJacksonComponent",
 "model": "fake",
 "notes": []
}
```

Query it with `sbmig kb ledger` (`--project --symbol --label --resolution --signature` are regexes, `--json`
prints one record per line, `--limit N` the last N):

```bash
sbmig kb ledger --symbol JsonComponent
sbmig kb ledger --resolution blocked --json | jq -r .symbol | sort | uniq -c | sort -rn
```

## The symbol index and prompt hints

`kb/symbols.json` maps each error-case symbol (javac `symbol:` simple name, `package X`, artifact `g:a`) to the
fixes seen for it: labels, replacement, number of distinct projects, case ids and the recipe compiled/generated
from it. It is updated on every harvest and rebuilt by `sbmig kb reindex`. `FileKnowledgeBase.match()` puts a
symbol hint **first** in `KBMatch.hints` (within `kb.max_hints`), so the LLM prompt for a new occurrence — even
with a different L1/L2 signature — starts with e.g.

```
[symbol JsonComponent] fixed in 3 project(s) by annotation-replace, type-rename:
org.springframework.boot.jackson.JsonComponent → org.springframework.boot.jackson.JacksonComponent
(recipe JsonComponentToJacksonComponent)
```

`sbmig kb symbols` prints the index.

## learning.md and how to act on it

`<out>/fleet/learning.md` (same content as `sbmig kb learning --md`, restricted to this fleet's runs) answers:

1. **Write a recipe / handler for these** — symbols the LLM fixed ≥ 2 times that still have no KB recipe. Every
   further project pays an LLM call for them. Usually they are `method-signature-adapt` / `override-signature` /
   `generic-rewrite` fixes the recipe whitelist cannot express: write an OpenRewrite recipe in
   `knowledge/recipes/base/` (+ `catalog.yml`) or a deterministic handler (`sbmig/triage/handlers/`), or promote a candidate by hand.
2. **Still resolved by the LLM** — the full list with label, replacement and whether a KB recipe exists now.
3. **Blocking most projects** — blocked artifacts / owner classes with the number of projects they block and
   the project names: hand this table to the library owners (e.g. "publish a jakarta build of
   `com.company:legacy-web`"); every project behind it unblocks at once.
4. **Libraries needing owner action (advisories)** — library advisories (springfox, hystrix, sleuth, old Spring
   Cloud trains …; `resolution=advisory` ledger lines, `sbmig kb ledger --resolution advisory`) with the number of
   projects, how many were replaced by a recipe and how many are still present. Advisories never block a run.
5. **KB recipes / patches that paid off** — which learned recipes fired in how many projects.

It also lists the KB counts and the cases / candidates / promotions created during the fleet.

### Library advisories and carried-over versions

`summary.md` / `summary.csv` have two more columns: `advisories` (library advisory findings of the project — the
outcomes are in `state.json` as `"<advisory_id>:<outcome>"`) and `carried_over` (dependencies pinned at the
version the as-is parent managed, because the new parent / Boot BOM no longer manages them). `summary.md` adds
**Libraries flagged across the fleet** (advisory → projects, replaced, still present). Each project's `report.md`
starts with a **Handover** section: the versions we replaced (⚠ = carried over) and the numbered "from here on,
your team" list for the owners.

## Knobs

| config | default | meaning |
|---|---|---|
| `fleet.order` | `manifest` | `simple-first`: pure_java → servlet → spring_legacy → boot → msa (cheap analysis, cached in the state) |
| `fleet.stop_on_abort` | `false` | stop the fleet when a project aborts (exit 3) |
| `fleet.retry_failed` | `false` | re-run incomplete / aborted projects on resume |
| `fleet.max_projects` | `0` | run at most N projects in this invocation (0 = all) |
| `fleet.max_llm_calls_total` | `0` | fleet-wide LLM budget; each project gets `min(loop.max_llm_calls, remaining)` |
| `fleet.reindex_every` | `10` | `kb reindex` (index.json + symbols.json) after every N projects |
| `fleet.summary_formats` | `[md, csv, json]` | summary files (learning.md is always written) |
| `fleet.preflight` | `true` | infrastructure checks before the first project and on every re-check |
| `fleet.infra_failure_limit` | `3` | projects in a row that ended for an infrastructure reason before the fleet waits |
| `fleet.infra_retry_minutes` | `15` | wait before re-checking (`0` = stop at once, state kept) |
| `fleet.infra_max_waits` | `4` | re-checks without a healthy project before the fleet stops |
| `fleet.cleanup` | `build-output` | after each project: `none` \| `build-output` (`target/` of the run copy) \| `workspace` (also the project copy, when `result.zip` exists) |
| `fleet.min_free_gb` | `5` | free space on the output drive before each project (`0` = no check) |
| `kb.ledger` | `true` | write `ledger.jsonl` |
| `kb.symbol_index` | `true` | maintain `symbols.json` and the symbol hints |
| `kb.deterministic_candidates` | `true` | compile recipe candidates from labels without the LLM |
| `kb.auto_promote_after_projects` | `2` | candidate → active after success in N distinct projects |

CLI flags of `fleet run`: `--order`, `--max-projects`, `--retry-failed`, `--stop-on-abort`,
`--max-llm-calls-total`, `--llm-provider`, `--dry-run`, `--config` (repeatable), `--out`. `fleet status` takes
`--attention`.

Offline demo: `scripts/fleet-demo.sh` (pure-java, boot2, boot3, spring5-xml, boot3-again with a fresh KB and the
replayed LLM of `scripts/demo-kb`): boot3 is fixed by the LLM once, the candidate is compiled from the labels,
boot3-again needs no LLM and promotes it.
