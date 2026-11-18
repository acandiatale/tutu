# Migration report — legacy-batch

| | |
|---|---|
| **Result** | ❌ NOT BUILDING (final `mvn clean package`) |
| SBMIG-RESULT | `SBMIG-RESULT run=run-abort exit=3 build=FAIL unresolved=0 blocked=0 open=0 resolved=0 llm=0 attn=infra:maven` |
| Case | `unknown` |
| Run id | `run-abort` |
| Started / finished | 2026-11-15 09:37:59 UTC / 2026-11-15 09:37:59 UTC (0.0s) |
| LLM | 0 call(s), 0 in / 0 out tokens |
| Flags | `mvn-not-found`, `aborted` |

## Handover — what we replaced, what is yours

No version changes and no library advisories recorded for this run.

## Error list

None. No structured errors were captured; check the last build log.

## Manual follow-ups

- run aborted: 'mvn' is not runnable (install Maven or set build.mvn)

## Flags

- `mvn-not-found`: Maven was not runnable on this machine
- `aborted`: the run stopped early (see follow-ups for the reason)
