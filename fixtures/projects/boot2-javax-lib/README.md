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

Since v2.1z-e (2026-11-02) sbmig searches the repository for a jakarta build before it asks the LLM (`sbmig-jakarta`
pre-try, `libraries.discover_jakarta_builds`). `fixtures/libs` also installs `legacy-web-lib` 2.0.0 / 3.0.0 (jakarta
builds, used by `boot2-javax-lib-optional`), so with the discovery on this fixture would build after a `1.0.0 → 2.0.0`
major bump instead of ending BLOCKED; its `sbmig.e2e.yml` turns the discovery off to keep the decline → BLOCKED path
under test (`servlet-javax-grid` is the discovery fixture).

## Build as-is

Install the in-house library first (`fixtures/libs/install.sh`, idempotent), then:

```bash
fixtures/libs/install.sh
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
```

Tests also pass on JDK 11 (`mvn -q test`).

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

**Blocked** `external_javax_api` `com.company.legacy:legacy-web-lib` for both cases
(`compile|incompatible_types|HttpServletRequest->HttpServletRequest` `61d819f63f`, `compile|cannot_access|HttpServletRequest`
`f2b133edd9`) after the blocked policy's 2 fix-script requests per case, which the replayed LLM answers without a script
(no script can fix a library binary). The restructure still runs; the final build fails → `success = false`, exit code
**2**, 5 LLM calls (review + 4).
