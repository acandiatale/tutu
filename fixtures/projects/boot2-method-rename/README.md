# boot2-method-rename fixture (scenario X2: in-house library renamed a method → LLM fix → KB `ChangeMethodName`)

Byte-for-byte the same project as `boot2-javax-lib` (Boot 2.7.18, Java 11, `com.company.legacy:legacy-web-lib:1.0.0`
with a `javax.servlet` API used from `ClientIpFilter` and `MemberController`). The difference is the configuration:
`sbmig.e2e.yml` in this directory is a per-fixture override that `scripts/e2e.py` appends as an extra `--config`:

* the optional list is the one of `boot2-javax-lib-optional` (still exactly 8 entries) but with
  **`com.company.legacy:legacy-web-lib` 3.0.0** (`fixtures/libs/legacy-web-lib/v3.0.0`): the jakarta build in which
  `static LegacyRequestHelper.clientIp(HttpServletRequest)` was **renamed** to `remoteClientIp(HttpServletRequest)`
  (`clientIp()` → `remoteClientIp()` too; `requestKey()`, `isAjax()`, `MaskingUtils.maskEmail` unchanged)
* `llm.provider: fake` with `llm.base_url: "replay:fixtures/projects/boot2-method-rename/replay"` — a scripted LLM, so
  the run is offline and deterministic (`replay/llm-NNN.json`, consumed in order, the last one repeats)
* `kb.dir: out/e2e/kb-method-rename` — its own knowledge base (every KB candidate is tried in every later plan, so a
  shared KB would leak this fixture's recipe into the other fixtures); `scripts/e2e.py` clears it before the run

## Build as-is

```bash
fixtures/libs/install.sh
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
```

## Expected sbmig outcome (asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

* `legacy-web-lib` is bumped right after the spring rewrite: `optional_bumped` contains
  `com.company.legacy:legacy-web-lib 1.0.0→3.0.0`
* the spring-phase build then fails in both call sites with
  `cannot find symbol … symbol: method clientIp(HttpServletRequest) … location: class LegacyRequestHelper`
  (catalog **X2**; javac prints the simple class name, so it is not blocked as `external_api_incompatible`)
* clusters are per file, so the fix loop asks the LLM **twice in the same round**: `replay/llm-001.json` renames the
  call in `ClientIpFilter.java` (cluster `compile:5e0f2951`, sorted first), `replay/llm-002.json` the one in
  `MemberController.java` (`compile:937b2a7c`); each SEARCH block is the single call-site line, which the
  OpenRewrite spring phase leaves untouched (only the `javax.servlet` imports become `jakarta.servlet`)
* BUILD SUCCESS → `success = true`, exit code 0, nothing blocked, 0 unresolved, `llm_usage.calls ≤ 2`
* harvest labels the case `method-rename` and compiles — **without the LLM** — the candidate
  `com.company.kb.LegacyRequestHelperClientIpToRemoteClientIp` into `out/e2e/kb-method-rename/recipes/candidates/`:
  `org.openrewrite.java.ChangeMethodName {methodPattern: "com.company.legacy.web.LegacyRequestHelper clientIp(..)",
  newMethodName: remoteClientIp, matchOverrides: true}` (`expect.kb`; `report.harvest.candidates_created` ≥ 1)
