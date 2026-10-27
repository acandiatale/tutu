# boot2-fix-failures fixture (error cases: failed fix attempts are recorded, 2026-10-23)

Byte-for-byte the same project as `boot2-javax-lib` (Boot 2.7.18, Java 11, `com.company.legacy:legacy-web-lib:1.0.0`
called from `ClientIpFilter` and `MemberController`), configured like `boot2-method-rename` (`sbmig.e2e.yml`:
legacy-web-lib bumped to **3.0.0**, where `LegacyRequestHelper.clientIp(..)` is renamed to `remoteClientIp(..)`;
`llm.provider: fake` with its own replay directory; its own `kb.dir: out/e2e/kb-boot2-fix-failures`). The difference
is the scripted LLM, whose first fix script is **deliberately too narrow** so that the run leaves a failed attempt
behind and has to augment it — the business requirement of 2026-10-23 (every build error gets its own record: what it
was, what was tried, how it was resolved or why not, failed attempts included, with the code that was applied) and
the v2 augment path (docs/v2/PLAN-v2.md §3.1).

| replay | request | answer | attempt outcome |
|---|---|---|---|
| `llm-001.json` | structure review | verdict `ok` | — |
| `llm-002.json` | case `compile\|cannot_find_symbol\|clientIp` (`8bb4fd4a4a`, both files), mode `new` → v1 | an exact replace written for `clientIp(request)` only, so `MemberController`'s `clientIp(http)` stays | `partial`, rolled back before the next attempt |
| `llm-003.json` | the same case, mode `augment` → v2 (parent v1, sees v1's log) | renames the call on every reported line, whatever the argument | `build_ok` |

## Build as-is

```bash
fixtures/libs/install.sh
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
```

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

Success, exit 0, **3 LLM calls** (review, v1, v2). Both error records (`ClientIpFilter.java`, `MemberController.java`)
are `resolved` by `script_augment` with the attempt history `partial → build_ok`; both versions stay in
`<kb>/scripts/8bb4fd4a4a/` (v1 `partials: 1`, v2 `successes: 1`, `parent: 1`); `<run dir>/error_cases.jsonl`,
`fix_attempts.jsonl` (every attempt with its diff) and `error_cases.csv` exist and the KB holds the same records and the
`clientIp` case (`expect.error_cases`). legacy-web-lib is bumped `1.0.0→3.0.0` inside the restructure.
