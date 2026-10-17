# legacy-web-lib — simulated in-house library

`com.company.legacy:legacy-web-lib` stands in for a company library that is **not** on Maven Central and whose
public API exposes servlet types (the real-world case behind catalog entry **X1** in `docs/ERROR-HANDLING.md`).

| dir | version | Java | servlet API (provided) |
|---|---|---|---|
| `v1.0.0/` | 1.0.0 | 8 (`source/target 1.8`) | `javax.servlet:javax.servlet-api:3.1.0` |
| `v2.0.0/` | 2.0.0 | 17 (`release 17`) | `jakarta.servlet:jakarta.servlet-api:6.1.0` |

Same API in both versions:

* `com.company.legacy.web.LegacyRequestHelper(HttpServletRequest request)` — constructor
* `static String LegacyRequestHelper.clientIp(HttpServletRequest req)`, `clientIp()`, `requestKey()`, `isAjax()`
* `com.company.legacy.web.MaskingUtils.maskEmail(String)` — servlet-free util

The servlet API is `provided`, so it is **not** transitive: a Boot 4 (Tomcat 11, jakarta-only) consumer of 1.0.0
fails with `cannot access javax.servlet.http.HttpServletRequest` / `incompatible types: jakarta… cannot be converted
to javax…` at the call sites.

Install both into the local repository (idempotent):

```bash
fixtures/libs/install.sh                       # ~/.m2/repository
fixtures/libs/install.sh /tmp/some-local-repo  # -Dmaven.repo.local=/tmp/some-local-repo
```
