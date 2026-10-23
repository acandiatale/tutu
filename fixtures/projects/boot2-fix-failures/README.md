# boot2-fix-failures fixture (error cases: failed fix attempts are recorded, 2026-10-23)

Byte-for-byte the same project as `boot2-javax-lib` (Boot 2.7.18, Java 11, `com.company.legacy:legacy-web-lib:1.0.0`
called from `ClientIpFilter` and `MemberController`), configured like `boot2-method-rename` (`sbmig.e2e.yml`:
legacy-web-lib bumped to **3.0.0**, where `LegacyRequestHelper.clientIp(..)` is renamed to `remoteClientIp(..)`;
`llm.provider: fake` with its own replay directory; its own `kb.dir: out/e2e/kb-fix-failures`). The difference is
the scripted LLM, which is **deliberately imperfect** so that the run leaves failed attempts behind — the business
requirement of 2026-10-23: every build error gets its own record (what it was, what was tried, how it was resolved
or why not), failed attempts included, with the code that was applied.

The fixer re-asks once when a SEARCH block does not match, so one attempt can consume two responses.

| replay | cluster (round) | answer | attempt outcome |
|---|---|---|---|
| `llm-001.json` + `llm-002.json` | `ClientIpFilter.java` (spring, round 1; the second is the feedback round) | right idea, SEARCH block not in the file — twice | `apply_failed` |
| `llm-003.json` | `MemberController.java` (round 1) | adds a comment, keeps `clientIp(http)` | `no_change` |
| `llm-004.json` | `ClientIpFilter.java` (round 2) | the real rename (text replace `clientIp` → `remoteClientIp`) | `improved` |
| `llm-005.json` | `MemberController.java` (round 2, repeated by the verify loop) | extracts a local variable, still `clientIp` | `no_change`, then `apply_failed` |

## Build as-is

```bash
fixtures/libs/install.sh
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
```

## Expected sbmig outcome (asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

* `ClientIpFilter.java`: one error record, `status: resolved`, `fix_method: llm`, `change_kind: text_replace`,
  replace pair `clientIp` → `remoteClientIp`, `replace_rule_candidate: true` (an LLM fix a rule could have done), and
  an attempt history `apply_failed` → `improved` (the first attempt's failure reason is kept)
* `MemberController.java`: still failing. The spring loop blocks it as `repeated_failure` (the attempt limit is per
  signature, so the ClientIpFilter attempts count too); the final verify loop releases it, tries the KB candidate
  recipe and the LLM again (`apply_failed`: the file has moved into the comp module) and leaves it **unresolved** —
  history `no_change` → `apply_failed` … with a `failure_reason`; the final build fails → `success: false`, exit 2
* `<run dir>/error_cases.jsonl`, `fix_attempts.jsonl` (every attempt with its diff) and `error_cases.csv` exist;
  `out/e2e/kb-fix-failures/errors.jsonl` / `fix_attempts.jsonl` hold the same records (`expect.error_cases`)
