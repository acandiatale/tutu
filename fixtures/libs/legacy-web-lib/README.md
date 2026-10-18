# legacy-web-lib — simulated in-house library

`com.company.legacy:legacy-web-lib` stands in for a company library that is **not** on Maven Central and whose
public API exposes servlet types (the real-world case behind catalog entry **X1** in `docs/ERROR-HANDLING.md`).

| dir | version | Java | servlet API (provided) |
|---|---|---|---|
| `v1.0.0/` | 1.0.0 | 8 (`source/target 1.8`) | `javax.servlet:javax.servlet-api:3.1.0` |
| `v2.0.0/` | 2.0.0 | 17 (`release 17`) | `jakarta.servlet:jakarta.servlet-api:6.1.0` |
| `v3.0.0/` | 3.0.0 | 17 (`release 17`) | `jakarta.servlet:jakarta.servlet-api:6.1.0` — **`clientIp` renamed to `remoteClientIp`** |

Same API in 1.0.0 and 2.0.0:

* `com.company.legacy.web.LegacyRequestHelper(HttpServletRequest request)` — constructor
* `static String LegacyRequestHelper.clientIp(HttpServletRequest req)`, `clientIp()`, `requestKey()`, `isAjax()`
* `com.company.legacy.web.MaskingUtils.maskEmail(String)` — servlet-free util

3.0.0 is the 2.0.0 jakarta build with a breaking rename (no deprecated alias): `static clientIp(HttpServletRequest)` →
`static remoteClientIp(HttpServletRequest)` and `clientIp()` → `remoteClientIp()`; `requestKey()`, `isAjax()` and
`MaskingUtils.maskEmail` are unchanged. A consumer bumped to 3.0.0 fails with `cannot find symbol … symbol: method
clientIp(HttpServletRequest) … location: class LegacyRequestHelper` — the `method-rename` scenario of the fixture
`boot2-method-rename` (fix label `method-rename` → KB recipe candidate `org.openrewrite.java.ChangeMethodName`).

The servlet API is `provided`, so it is **not** transitive: a Boot 4 (Tomcat 11, jakarta-only) consumer of 1.0.0
fails with `cannot access javax.servlet.http.HttpServletRequest` / `incompatible types: jakarta… cannot be converted
to javax…` at the call sites.

Install all three into the local repository (idempotent):

```bash
fixtures/libs/install.sh                       # ~/.m2/repository
fixtures/libs/install.sh /tmp/some-local-repo  # -Dmaven.repo.local=/tmp/some-local-repo
```
