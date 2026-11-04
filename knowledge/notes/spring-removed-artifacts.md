---
title: Spring Framework artifacts that no longer exist on the target line (spring-dao, spring-mock, spring-hibernate3/4, ...)
keywords:
  - "spring-dao|spring-mock|spring-hibernate3|spring-hibernate4|spring-webmvc-portlet|spring-support|spring-struts|spring-ibatis"
  - "org\\.springframework\\.dao\\.support\\.DaoSupport|SqlMapClientDaoSupport|HibernateDaoSupport"
  - "dependency\\.version.*org\\.springframework:spring-"
cases: [spring_legacy, servlet_tomcat, spring_boot, msa]
phases: [spring]
always: false
---
# Spring Framework artifacts that no longer exist on the target line

`org.springframework:spring-dao` (Spring 2.x — `DaoSupport` / `JdbcDaoSupport` live in `spring-tx` / `spring-jdbc` since 3.0),
`spring-mock` (→ `spring-test`), `spring-hibernate3` / `spring-hibernate4` (gone in 5.x — `spring-orm` + Hibernate 7),
`spring-webmvc-portlet`, `spring-support`, `spring-struts`, `spring-ibatis` (iBATIS 2 is unsupported, see ibatis2.md) are not part
of Spring Framework 7, and no parent chain or BOM manages them.

**A missing / unresolvable version on one of them is not fixed by a version.** `'dependencies.dependency.version' for
org.springframework:spring-dao:jar is missing` means the as-is pin was removed (the parent chain manages the Spring families,
but not this artifact, because it does not exist on the target line). The repository's newest release (spring-dao 2.0.8, 2007)
would drag Spring 2 next to Spring 7 — never pin it, never write `${spring-framework.version}` on it (there is no 7.x).

**Do instead**: remove the dependency; keep the classes that still exist through the artifacts that hold them now (`spring-tx`,
`spring-jdbc`, `spring-orm`, `spring-test` — managed by the chain / BOM, declared without a version); migrate the removed APIs
(`SqlMapClientDaoSupport` → MyBatis or plain JDBC, `HibernateDaoSupport` → `SessionFactory` / JPA, `spring-mock` test helpers →
`spring-test`). Project code only → `<no_script>` + `<edits>` (rule 12); say in `<solution>` what was removed and what replaced it.
