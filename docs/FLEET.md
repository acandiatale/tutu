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

# 4. after a crash / Ctrl-C / reboot: continue where it stopped (same manifest + configs)
sbmig fleet resume out/fleet                 # = sbmig fleet run fleet.yml --config … --out out/fleet
sbmig fleet resume out/fleet --retry-failed  # also re-run incomplete / aborted projects
```

Manifest forms:

| form | content |
|---|---|
| directory | every `*.zip` and every subdirectory containing a `pom.xml`, sorted by name |
| `.txt` | one path per line, `#` comments |
| `.yml` | `projects: [{name, input, configs: [...], overrides: {...}, tags: [...]}]` |
| `.csv` | header `name,input,tags` |

Paths are relative to the manifest file. A missing input is marked `skipped` (it runs on a later resume once it
exists); it never stops the fleet. Per-project `configs` / `overrides` are merged over the fleet config, except
`kb.dir`: the KB is always fleet-wide.

`sbmig fleet run` exit codes: `0` all projects succeeded, `2` some are incomplete / skipped / not run yet,
`3` stopped on an aborted project (`--stop-on-abort`). `--dry-run` prints the order and the resume decision per
project without running anything.

## What is persisted where

| path | content |
|---|---|
| `<out>/fleet/state.json` | `FleetState`: one `FleetProjectResult` per project (status, exit code, run id, LLM calls, blocked reasons, KB cases/candidates/promotions/used). Rewritten atomically after every project. |
| `<out>/fleet/summary.md` / `.csv` / `.json` | the progress table (+ totals, "still running: X"), rewritten after every project |
| `<out>/fleet/learning.md` | what the KB learned during this fleet (see below) |
| `<out>/fleet/fleet-config.json` | manifest + config paths + overrides of the last `run` (used by `resume`) |
| `<out>/<fleet-id>-<NNN>-<name>/` | the normal run directory of each project (`report.md`, `report.json`, `result.zip`, journal, logs) |
| `<kb.dir>/cases/…`, `recipes/…`, `index.json` | the KB (as for single runs) |
| `<kb.dir>/ledger.jsonl` | append-only resolution ledger, one line per resolved / blocked / unresolved cluster |
| `<kb.dir>/symbols.json` | error-case symbol → how it was fixed, in how many projects |

Resume rules: `success` projects are never re-run; `incomplete` / `aborted` only with `--retry-failed`
(`fleet.retry_failed`); a project left in `running` (the fleet process died) is re-run. Retries get a new run id
(`…-r2`) and `attempt` is incremented.

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
  `matchOverrides: true`, named `com.company.kb.<OwnerSimple><Old>To<New>`. javac prints imported types by their
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
 "recipe_id": "com.company.kb.JsonComponentToJacksonComponent",
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
(recipe com.company.kb.JsonComponentToJacksonComponent)
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
| `kb.ledger` | `true` | write `ledger.jsonl` |
| `kb.symbol_index` | `true` | maintain `symbols.json` and the symbol hints |
| `kb.deterministic_candidates` | `true` | compile recipe candidates from labels without the LLM |
| `kb.auto_promote_after_projects` | `2` | candidate → active after success in N distinct projects |

CLI flags of `fleet run`: `--order`, `--max-projects`, `--retry-failed`, `--stop-on-abort`,
`--max-llm-calls-total`, `--llm-provider`, `--dry-run`, `--config` (repeatable), `--out`.

Offline demo: `scripts/fleet-demo.sh` (pure-java, boot2, boot3, spring5-xml, boot3-again with a fresh KB and the
replayed LLM of `scripts/demo-kb`): boot3 is fixed by the LLM once, the candidate is compiled from the labels,
boot3-again needs no LLM and promotes it.
