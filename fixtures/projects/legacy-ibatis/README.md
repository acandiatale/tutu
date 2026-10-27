# legacy-ibatis fixture (Spring 3.2 XML + iBATIS 2, advisory `ibatis2`, policy `guide`)

A 2012-era batch jar (case `spring_legacy`): Spring **3.2.18.RELEASE** XML context, `<source>/<target>` **1.6**
(`maven-compiler-plugin` 2.3.2), no web, no tests.

* `org.springframework:spring-context` / `spring-jdbc` / `spring-orm` 3.2.18.RELEASE (`spring.version` property),
  `org.apache.ibatis:ibatis-sqlmap` **2.3.4.726** (still on Maven Central), `com.h2database:h2` 1.4.200 (runtime).
* `CatalogDao extends SqlMapClientDaoSupport` (`org.springframework.orm.ibatis.support`, removed in Spring 4.0):
  `getSqlMapClientTemplate().queryForList("catalog.findAll")` / `queryForObject("catalog.findById", id)`.
* `applicationContext.xml`: H2 `DriverManagerDataSource`, `<jdbc:initialize-database>` (`schema.sql`),
  `org.springframework.orm.ibatis.SqlMapClientFactoryBean` (`configLocation` = `sqlmap-config.xml`), the DAO bean.
* `sqlmap-config.xml` (`useStatementNamespaces="true"`) + `sqlmap/catalog-sqlmap.xml` (namespace `catalog`).
* `Main` loads the context and prints the rows (`1:pencil`, `2:notebook` as-is on JDK 8).

`sbmig.e2e.yml` replays the LLM (own KB `out/e2e/kb-legacy-ibatis`): `llm-001.json` = the structure review (a warning on
iBATIS 2), `llm-002.json` answers every case of `CatalogDao.java` with an analysis and **no script** (the LLM declines
a blind MyBatis port).

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
  mvn -q -DskipTests package
```

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

Spring 3.2.18 → 7.0.x drops `org.springframework.orm.ibatis`, so the first (final) build fails in `CatalogDao.java` with
three cases (`package_does_not_exist org.springframework.orm.ibatis.support`, `cannot_find_symbol SqlMapClientDaoSupport`,
`cannot_find_symbol getSqlMapClientTemplate`). Each case gets 4 fix-script requests (all `new`: without a stored
version there is nothing to augment; `script.max_augments_per_case` 3), every one answered without a script → **BLOCKED
`repeated_failure` ×3**; the final build runs again and fails (invariant 1) → `success: false`, exit **2**, 13 LLM calls.
Advisory `ibatis2` (`guide`, `still-present`: `ibatis-sqlmap` kept with its version, `CatalogDao` still
`extends SqlMapClientDaoSupport`) and its MyBatis follow-up in the Handover.
