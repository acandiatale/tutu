---
title: JUnit 4 -> JUnit 5/Jupiter (Boot 4 ships JUnit Jupiter 6)
keywords:
  - "org\\.junit\\.(Test|Before|After|BeforeClass|AfterClass|Ignore|Rule|ClassRule|Assert|runner|Assume|experimental)"
  - "RunWith|SpringRunner|SpringJUnit4ClassRunner|MockitoJUnitRunner|Parameterized"
  - "junit:junit|junit-vintage|vintage"
  - "ExpectedException|TemporaryFolder|@Rule"
  - "org\\.junit\\.jupiter"
  - "assertThat|Matchers|hamcrest"
  - "PowerMock|powermock"
cases: [pure_java, spring_legacy, spring_boot, servlet_tomcat, msa]
phases: [spring, jdk21]
---
# JUnit 4 → JUnit Jupiter (OpenRewrite JUnit4to5Migration does most of it)

Boot 4 manages JUnit Jupiter 6.x (`org.junit.jupiter`); the vintage engine is not on the classpath by default, so JUnit 4
tests either fail to compile (`package org.junit does not exist`) or are silently not run. Prefer migrating the test;
temporary alternative: add `org.junit.vintage:junit-vintage-engine` (test) + `junit:junit:4.13.2` (test) and note it.

## Annotation / API map
| JUnit 4 | Jupiter |
|---|---|
| `org.junit.Test` | `org.junit.jupiter.api.Test` |
| `@Test(expected = X.class)` | `assertThrows(X.class, () -> …)` |
| `@Test(timeout = 1000)` | `@Timeout(value = 1000, unit = MILLISECONDS)` or `assertTimeout` |
| `@Before` / `@After` | `@BeforeEach` / `@AfterEach` |
| `@BeforeClass` / `@AfterClass` (static) | `@BeforeAll` / `@AfterAll` (static, or `@TestInstance(PER_CLASS)`) |
| `@Ignore("why")` | `@Disabled("why")` (only to mirror an existing @Ignore — never add new ones) |
| `@Category(X.class)` | `@Tag("x")` (a leftover `package org.junit.experimental.categories does not exist` after the recipe: sbmig re-adds `junit:junit` test-scope first — prefer converting the test to `@Tag`, and if JUnit 4 must stay, `org.junit.vintage:junit-vintage-engine` (Boot-managed, test) is needed to run it) |
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
- Surefire ≥ 3.0 runs Jupiter natively (use 3.5.3 / Boot-managed).
- PowerMock (`@RunWith(PowerMockRunner.class)`, `@PrepareForTest`) is incompatible with JDK 17+/Jupiter →
  `Mockito.mockStatic(X.class)` in try-with-resources; `Whitebox.setInternalState` → `ReflectionTestUtils.setField`.
- Spring Boot 4 test changes (`@MockBean` → `@MockitoBean`, MockMvc auto-config) are in the boot3-to-4 note.
