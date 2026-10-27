# boot2-method-rename fixture (scenario X2: in-house library renamed a method → one LLM fix script for both call sites)

Byte-for-byte the same project as `boot2-javax-lib` (Boot 2.7.18, Java 11, `com.company.legacy:legacy-web-lib:1.0.0`
with a `javax.servlet` API used from `ClientIpFilter` and `MemberController`). The difference is the configuration:
`sbmig.e2e.yml` in this directory is a per-fixture override that `scripts/e2e.py` appends as an extra `--config`:

* the optional list is the one of `boot2-javax-lib-optional` (still exactly 8 entries) but with
  **`com.company.legacy:legacy-web-lib` 3.0.0** (`fixtures/libs/legacy-web-lib/v3.0.0`): the jakarta build in which
  `static LegacyRequestHelper.clientIp(HttpServletRequest)` was **renamed** to `remoteClientIp(HttpServletRequest)`
  (`clientIp()` → `remoteClientIp()` too; `requestKey()`, `isAjax()`, `MaskingUtils.maskEmail` unchanged)
* `llm.provider: fake` with `llm.base_url: "replay:fixtures/projects/boot2-method-rename/replay"` — a scripted LLM, so
  the run is offline and deterministic (`llm-001.json` = the structure review, `llm-002.json` = the fix script,
  matched by `case: <case_id>`)
* `kb.dir: out/e2e/kb-boot2-method-rename` — its own knowledge base (a shared KB would let other fixtures reuse its
  stored script); `scripts/e2e.py` clears it before the run

## Build as-is

```bash
fixtures/libs/install.sh
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
```

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

legacy-web-lib is bumped inside the restructure (`1.0.0→3.0.0`); the build then fails at both call sites with
`cannot find symbol … method clientIp(HttpServletRequest) … location: class LegacyRequestHelper` (catalog **X2**).
v2 groups errors by case, so both files are **one** case (`compile|cannot_find_symbol|clientIp`, `8bb4fd4a4a`) and one
fix script renames the call on the reported lines in both. BUILD SUCCESS, exit 0, nothing blocked, **2 LLM calls**
(review + script); the script is stored as `<kb>/scripts/8bb4fd4a4a/v1.py`.
