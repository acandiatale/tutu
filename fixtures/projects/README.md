# Fixture projects

Small but real Maven projects used as the sbmig test corpus (unit tests read them; integration tests and
`scripts/e2e.py` build and migrate them). Every artifact is public on Maven Central. Each fixture has a
`README.md` (exact as-is build command) and an `expected.json` (what `sbmig analyze` should report).

| fixture | case | as-is | build JDK | expected migration hot spots |
|---|---|---|---|---|
| `pure-java` | `pure_java` | Java 8 jar, commons-lang3 3.9, guava 28.0-jre, junit 4.12 | 8 | JAXB (`javax.xml.bind`) removed from JDK 11+, compiler plugin 3.8.1, JUnit 4→5, generated `Application`, optional bumps |
| `servlet-tomcat` | `servlet_tomcat` | Java 8 war, servlet-api 3.1.0, web.xml servlet/filter/listener, JSP, commons-io 2.6 | 8 | javax→jakarta servlet, war→boot jar (`@ServletComponentScan`, web.xml/JSP follow-ups), JUnit 4→5 |
| `spring5-xml` | `spring_legacy` | Java 8 war, Spring 5.3.31 XML (`ContextLoaderListener` + `DispatcherServlet`), jackson 2.13.5 | 8 | `WebMvcConfigurerAdapter` / `HandlerInterceptorAdapter` removed (LLM / KB recipe target), Framework 7, `@ImportResource`, jakarta |
| `boot2` | `spring_boot` | Boot 2.7.18, Java 11, security (`WebSecurityConfigurerAdapter`, `antMatchers`, `.and()`), JPA javax, lombok 1.18.24, `spring.redis.*` | 11 (fails on 21: lombok) | lombok floor pre-handler, jakarta, Security 7 DSL, property rename, `@MockBean` |
| `boot3` | `spring_boot` | Boot 3.3.13, Java 17, actuator, JPA jakarta, AOP, `RestTemplateBuilder`, config-props record, `@JsonComponent`, guava 32 | 21 | Boot 4 starter split, RestTemplateBuilder timeouts, Jackson 3, `@MockBean` |
| `msa` | `msa` | aggregator + `order-service` (Boot 2.7.18/Java 11/lombok) + `user-service` (Boot 3.3.13/Java 17) | 11 / 21 per service (root: 17–18) | MSA pom-only mode, per-service sub-cases |

Optional artifacts from `config/artifacts.local-e2e.yml` used directly (→ "optional bump" path):

| artifact | fixtures |
|---|---|
| `org.apache.commons:commons-lang3` | pure-java, boot2 |
| `com.google.guava:guava` | pure-java, boot3 |
| `org.projectlombok:lombok` | boot2, msa/order-service |
| `com.fasterxml.jackson.core:jackson-databind` | spring5-xml |
| `commons-io:commons-io` | servlet-tomcat |
| `org.slf4j:slf4j-api` | spring5-xml |

Integration check: `python -m pytest tests/fixtures -m integration` builds every fixture with its documented JDK.
