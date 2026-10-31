---
title: JUnit 4 -> JUnit 5/Jupiter (Boot 4 ships JUnit Jupiter 6)
keywords:
  - "org\\.junit\\.(Test|Before|After|BeforeClass|AfterClass|Ignore|Rule|ClassRule|Assert|runner|Assume|experimental)"
  - "RunWith|SpringRunner|SpringJUnit4ClassRunner|MockitoJUnitRunner|\\bParameterized\\b"
  - "junit:junit|junit-vintage|vintage"
  - "ExpectedException|TemporaryFolder|@Rule"
  - "org\\.junit\\.jupiter"
  - "org\\.hamcrest|\\bMatchers\\."
  - "PowerMock|powermock"
cases: [pure_java, spring_legacy, spring_boot, servlet_tomcat, msa]
phases: [spring, jdk21]
---
# JUnit 4 → JUnit Jupiter (OpenRewrite JUnit4to5Migration does most of it)

sbmig's build does not compile tests (`-Dmaven.test.skip=true`): this note matters only when a project turns test
compilation back on. Never change test code to fix a main-code compile error.

Boot 4 manages JUnit Jupiter 6.x (`org.junit.jupiter`); the vintage engine is not on the classpath by default, so JUnit 4
tests either fail to compile (`package org.junit does not exist`) or are silently not run. Prefer migrating the test;
temporary alternative: add `org.junit.vintage:junit-vintage-engine` (test) + `junit:junit` (test), both Boot-managed
(`version: null`), and note it.

## Annotation / API map
| JUnit 4 | Jupiter |
|---|---|
| `org.junit.Test` | `org.junit.jupiter.api.Test` |
| `@Test(expected = X.class)` | `assertThrows(X.class, () -> …)` |
| `@Test(timeout = 1000)` | `@Timeout(value = 1000, unit = MILLISECONDS)` or `assertTimeout` |
| `@Before` / `@After` | `@BeforeEach` / `@AfterEach` |
| `@BeforeClass` / `@AfterClass` (static) | `@BeforeAll` / `@AfterAll` (static, or `@TestInstance(PER_CLASS)`) |
| `@Ignore("why")` | `@Disabled("why")` (only to mirror an existing @Ignore — never add new ones) |
| `@Category(X.class)` | `@Tag("x")` (on `package org.junit.experimental.categories does not exist` prefer converting the test to `@Tag`; if JUnit 4 must stay, re-add `junit:junit` test-scope and `org.junit.vintage:junit-vintage-engine` (Boot-managed, test) to run it) |
| `org.junit.Assert.assertEquals(msg, expected, actual)` | `Assertions.assertEquals(expected, actual, msg)` — message moves LAST |
| `Assert.assertThat(x, matcher)` | `org.hamcrest.MatcherAssert.assertThat` or AssertJ `assertThat(x)` |
| `org.junit.Assume.assumeTrue` | `org.junit.jupiter.api.Assumptions.assumeTrue` |
| `@Rule ExpectedException` | `assertThrows` (returns the exception for message checks) |
| `@Rule TemporaryFolder` | `@TempDir Path tempDir` |
| `@Rule TestName` | `TestInfo` parameter |
| `@RunWith(SpringRunner.class)` / `SpringJUnit4ClassRunner` | remove (`@SpringBootTest`/`@WebMvcTest` include `@ExtendWith(SpringExtension.class)`); plain Spring: `@SpringJUnitConfig(Config.class)` |
| `@RunWith(MockitoJUnitRunner.class)` | `@ExtendWith(MockitoExtension.class)` (`mockito-junit-jupiter`); strict stubs → `@MockitoSettings(strictness = LENIENT)` if needed |
| `@RunWith(Parameterized.class)` + `@Parameters` | `@ParameterizedTest` + `@MethodSource("data")` / `@CsvSource` (`junit-jupiter-params`) |
| `@FixMethodOrder` | `@TestMethodOrder(MethodOrderer.MethodName.class)` |

- Test classes and methods may be package-private in Jupiter; `public` is fine too.
- Dependencies: remove `junit:junit`; add `org.junit.jupiter:junit-jupiter` (test) unless `spring-boot-starter-test` is present
  (it brings Jupiter, AssertJ, Hamcrest, Mockito, mockito-junit-jupiter).
- Surefire ≥ 3.0 runs Jupiter natively (the parent chain's version).
- PowerMock (`@RunWith(PowerMockRunner.class)`, `@PrepareForTest`) is incompatible with JDK 17+/Jupiter →
  `Mockito.mockStatic(X.class)` in try-with-resources; `Whitebox.setInternalState` → `ReflectionTestUtils.setField`.
- Spring Boot 4 test changes (`@MockBean` → `@MockitoBean`): boot4-packages.md.
