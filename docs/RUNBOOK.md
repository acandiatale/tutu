# Runbook — operating sbmig in the company environment

## 0. Prerequisites on the migration host
| Item | Requirement | Why |
|---|---|---|
| Python | ≥ 3.10 (+ `uv` or `pip`) | agent runtime |
| JDK | **21** (set `build.java_home`) | OpenRewrite plugin and all builds run on 21; as-is projects are never built on their old JDK |
| Maven | 3.9.x, `settings.xml` pointing at Nexus | builds + OpenRewrite plugin |
| Nexus proxies | Maven Central incl. `org.openrewrite.*` (plugin 6.46.1, recipe BOM 3.38.0 and its artifacts), `org.springframework.boot:*:4.1.0`, jakarta artifacts | recipes and target BOM |
| LLM endpoint | OpenAI-compatible `/v1/chat/completions` (or Anthropic Messages) | residual fixes + recipe candidates |
| git | any | per-attempt snapshots / revert / diff |

```bash
cd migration-agent
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"
cp config/artifacts.example.yml config/artifacts.yml      # fill in parent / mandatory ×2 / optional ×8
export LLM_BASE_URL=https://llm.internal/v1 LLM_API_KEY=... LLM_MODEL=<model>
```

Smoke test without touching anything: `sbmig analyze path/to/project.zip --config config/artifacts.yml`.

## 1. Running a migration
```bash
sbmig run path/to/project.zip --config config/artifacts.yml --out out/
#  --stop-after jdk21|spring|restructure   stop early (always still writes a report)
#  --dry-run                                analyze + plan + decisions only, no edits
#  --java-home /usr/lib/jvm/java-21 --settings /etc/maven/nexus-settings.xml
```
Exit code 0 = final `mvn clean package` passed. 2 = finished with unresolved errors (read `report.md`).

Outputs in `out/<run_id>/`:
* `project/` migrated working copy (git branch `sbmig/<run_id>`, one commit per step/attempt) and `result.zip`
* `report.md` / `report.json` — what changed, per-phase iterations, every fix attempt with outcome + commit, **unresolved errors**, **manual follow-ups** for the as-is owner (web.xml, JSP, XML bean wiring, skipped tests…)
* `changes.patch`, `journal.jsonl` (machine-readable event log), `logs/` (every build / rewrite / LLM call)

## 2. Reading a failed run
1. `report.md` → "Unresolved errors" lists clusters by file with the last javac/maven messages.
2. `sbmig parse-log out/<run>/logs/0NN-build-*.log` re-parses any log with signatures (useful to craft a KB pattern).
3. `git -C out/<run>/project log --oneline` shows the attempt chain; `reverted` attempts are gone from the tree but in the report.
4. Typical causes: Nexus missing an artifact (DEPENDENCY_RESOLUTION cluster → fix proxy, rerun), in-house parent not managing a version (`'dependencies.dependency.version' is missing` → add to `optional` with a version), LLM budget exhausted (`loop.max_llm_calls`).

## 3. Knowledge base workflow (no DB — files in `kb/`, keep them in git or sync as zip)
* Every successful LLM fix becomes `kb/cases/YYYY-MM/<id>.json`. Mechanical ones also produce `kb/recipes/candidates/<name>.yml`.
* Review candidates: `sbmig kb list --status candidate`, `sbmig kb show <id>`, then `sbmig kb promote <id>` (or `reject`). Promoted recipes are composed into **every** run's OpenRewrite execution and tried before the LLM when their signature matches.
* Auto-promotion: a candidate that succeeded in `kb.auto_promote_after_projects` distinct projects with no failures is promoted automatically. Set to 0 to require manual review.
* Move the KB between machines: `sbmig kb export kb-YYYYMMDD.zip` → `sbmig kb import kb-YYYYMMDD.zip`. `sbmig kb reindex` after manual edits.
* Hand-written shortcuts go in `kb/patterns/*.yml` (signature regex → recipes / handler ids / hints).
* Offline demonstration of the whole loop: `scripts/demo-kb/run.sh` (replayed LLM answers in `scripts/demo-kb/replay/`; run 1 harvests a case + candidate recipe, run 2 is fixed by that recipe without the LLM and promotes it).
* Recording real sessions: set `llm.record_dir` (every request/response is saved as `llm-<n>.json`); a recorded directory can be replayed with `llm.provider: fake` + `llm.base_url: replay:<dir>` for regression tests.

## 4. Tuning knobs (config/default.yml)
| Knob | Default | Effect |
|---|---|---|
| `loop.phases.<phase>.max_iterations` | 3 / 6 / 6 | build→fix rounds per phase |
| `loop.max_llm_calls`, `loop.max_total_minutes` | 60 / 180 | hard budgets per run |
| `loop.stall_limit` | 2 | identical error set N times → escalation model + widen context |
| `llm.escalation_model` | null | stronger model after a stall |
| `build.allow_skip_test_compile_fallback` | true | last resort `-Dmaven.test.skip=true`, flagged in the report |
| `layout.split_policy` | minimal | `web-layer` moves controllers/config into boot-jar |
| `plan.intermediate_builds` | true | build after the JDK 21 phase before Spring recipes |
| `kb.generate_recipe_candidates` | true | LLM proposes declarative recipes from fixes |

## 5. Safety notes
* The agent only edits the copy under `out/<run>/project`; the input zip/dir is never modified.
* No LLM call contains credentials; prompts include only the files/errors needed for the cluster.
* `llm.record_dir` records every request/response for audit and offline replay tests.
