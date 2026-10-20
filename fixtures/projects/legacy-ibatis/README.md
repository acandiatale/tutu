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

No `sbmig.e2e.yml`: the base e2e config's `llm.provider: fake` without a replay dir answers every fix request with
"no fix".

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
  mvn -q -DskipTests package
```

## Expected migration

Spring 3.2.18 → 7.0.x drops `org.springframework.orm.ibatis` → `CatalogDao.java` no longer compiles (4 errors, one
cluster) in the spring phase. The fake LLM answers "no fix" twice (`loop.max_llm_attempts_per_cluster: 2`; the
`ibatis2` advisory hint is in both prompts) → the cluster is blocked `repeated_failure`; restructure and the final
`mvn clean package` still run (invariant 1) and fail on the same errors → `success: false`, exit **2**, 2 LLM calls,
advisory `ibatis2` (`guide`, `still-present`, `ibatis-sqlmap` kept with its version), the advisory's MyBatis
follow-up in the Handover.
