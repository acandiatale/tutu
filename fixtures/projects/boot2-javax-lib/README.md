# boot2-javax-lib fixture (scenario X1: in-house library with a javax API — blocked)

Copy of `boot2` (Boot 2.7.18, Java 11, security, JPA, lombok 1.18.24, commons-lang3) plus the simulated in-house
library **`com.company.legacy:legacy-web-lib:1.0.0`** (`fixtures/libs/legacy-web-lib/v1.0.0`, not on Maven Central),
whose public API takes `javax.servlet.http.HttpServletRequest`:

* `web/ClientIpFilter` (`OncePerRequestFilter`) calls `LegacyRequestHelper.clientIp(request)`
* `web/MemberController.join(…, HttpServletRequest http)` calls `new LegacyRequestHelper(http)` and
  `LegacyRequestHelper.clientIp(http)`

`legacy-web-lib` is **not** one of the 8 optional artifacts in `config/artifacts.local-e2e.yml`, so sbmig has no
jakarta build to bump to. After the javax → jakarta rewrite the call sites fail with
`incompatible types: jakarta.servlet.http.HttpServletRequest cannot be converted to javax.servlet.http.HttpServletRequest`
and `cannot access HttpServletRequest … class file for javax.servlet.http.HttpServletRequest not found`
(the servlet API is `provided` in the library, i.e. not transitive).

## Build as-is

Install the in-house library first (`fixtures/libs/install.sh`, idempotent), then:

```bash
fixtures/libs/install.sh
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
```

Tests also pass on JDK 11 (`mvn -q test`).

## Expected sbmig outcome (asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

* **blocked**: one `BlockedItem` with `reason = external_javax_api`, `artifact = com.company.legacy:legacy-web-lib`,
  0 LLM calls for it (catalog policy: block immediately), suggestion "upgrade to a Jakarta build or add it to
  `artifacts.optional`"
* everything else is migrated (jakarta, Security 7 DSL, lombok floor …) and the **restructure still runs**
* final build fails → `success = false`, exit code **2**, `llm_usage.calls == 0`
