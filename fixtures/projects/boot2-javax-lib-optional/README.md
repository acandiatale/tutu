# boot2-javax-lib-optional fixture (scenario X1, fixed path: in-house library bumped to its jakarta build)

Byte-for-byte the same project as `boot2-javax-lib` (Boot 2.7.18, Java 11, `com.company.legacy:legacy-web-lib:1.0.0`
with a `javax.servlet` API used from `ClientIpFilter` and `MemberController`). The difference is the configuration:
`sbmig.e2e.yml` in this directory is a per-fixture override that `scripts/e2e.py` appends as an extra `--config`.
It replaces `org.slf4j:slf4j-api` in the optional list with **`com.company.legacy:legacy-web-lib` 2.0.0**
(`fixtures/libs/legacy-web-lib/v2.0.0`, same API on `jakarta.servlet`), so the list still has exactly 8 entries.

## Build as-is

```bash
fixtures/libs/install.sh
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
```

## Expected sbmig outcome (asserted by `scripts/e2e.py`)

* `legacy-web-lib` is bumped **right after the spring rewrite** (`plan.optional_bump_phase = spring`), so the jakarta
  call sites compile against 2.0.0: `optional_bumped` contains `com.company.legacy:legacy-web-lib 1.0.0→2.0.0`
* nothing blocked, `success = true`, exit code 0, `llm_usage.calls == 0`
