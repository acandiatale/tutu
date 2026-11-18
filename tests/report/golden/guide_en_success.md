# Migration report — ops-console

| | |
|---|---|
| **Result** | ✅ BUILD SUCCESS (final `mvn clean package`) |
| SBMIG-RESULT | `SBMIG-RESULT run=run-20261115-183759-8bb06b exit=0 build=ok unresolved=0 blocked=0 open=0 resolved=2 llm=7 attn=tests-not-compiled review=issues` |
| **Error list** | 3 error(s): resolved 2, note 1 (see "Error list") |
| Case | `spring_legacy` |
| As-is | java 1.8, spring boot 2.7.18, spring -, 1 pom(s) |
| To-be | java 21, spring boot 4.1.0, parent `org.springframework.boot:spring-boot-starter-parent:4.1.0` |
| Run id | `run-20261115-183759-8bb06b` |
| Started / finished | 2026-11-15 09:37:59 UTC / 2026-11-15 09:38:33 UTC (34.0s) |
| LLM | 7 call(s), 52242 in / 2609 out tokens (gpt-x) |
| LLM cost | $0.0065 (see "LLM usage and cost") |
| Flags | `tests-not-compiled` |
| Structure review | 0 critical, 1 warning(s) |

## Handover — what we replaced, what is yours

### Versions replaced

- parent: - → `org.springframework.boot:spring-boot-starter-parent:4.1.0`
- java: `1.8` → `21`
- spring boot: - → `4.1.0`

| what | before | after | why |
|---|---|---|---|
| dependency `org.springframework:spring-webmvc` | 5.3.31 | managed | spring boot 4 upgrade |
| plugin `org.apache.maven.plugins:maven-war-plugin` | 3.4.0 | 3.5.1 | recipe |
| ⚠ dependency `org.springframework.cloud:spring-cloud-starter-netflix-hystrix` | 2.2.10.RELEASE (managed) | 2.2.10.RELEASE | carried over (no longer managed) |

⚠ 1 dependency pinned at the as-is version: the new parent / Boot BOM no longer manages it.

Added: `parent org.springframework.boot:spring-boot-starter-parent:4.1.0`, `jakarta.servlet:jakarta.servlet-api`, `org.springframework.boot:spring-boot-starter-actuator (mandatory)`

Removed: `javax.servlet:javax.servlet-api:4.0.1`

### Libraries needing your attention

| library | as-is | status | what we did | outcome | what you do next | docs |
|---|---|---|---|---|---|---|
| **hystrix** | 2.2.10.RELEASE | eol | built with the as-is version pinned | ⚠ still present | Configure the circuit breakers by name. → `org.springframework.cloud:spring-cloud-starter-circuitbreaker-resilience4j` managed by the Spring Cloud BOM | [1](https://docs.example.com/r4j), [2](https://docs.example.com/r4j-start) |
| **jdk-removed-apis** | - | incompatible | replaced during the run | ✅ replaced | Review the ported JDK API calls. → `java.util.Base64` | [1](https://openjdk.org/jeps/320) |

### From here on, your team

1. Netflix Hystrix is discontinued (not in any current Spring Cloud train): Configure the circuit breakers by name. (replacement: org.springframework.cloud:spring-cloud-starter-circuitbreaker-resilience4j managed by the Spring Cloud BOM)
2. verify jdk-removed-apis → java.util.Base64 (replaced during the run): Review the ported JDK API calls.
3. code rewritten by the LLM for jdk-removed-apis: module/comp-jar/src/main/java/com/example/ops/util/TokenCodec.java — review these changes (changes.patch, Fixes by case)
4. org.springframework.cloud:spring-cloud-starter-netflix-hystrix pinned at 2.2.10.RELEASE — the new parent/Boot BOM no longer manages it: confirm the version or replace
5. structure review warning — module/comp-jar/pom.xml: spring-batch-core follows the parent's Batch line → check the Batch jobs
6. test sources were neither compiled nor run (build.skip_test_compile: true): the owning team compiles and fixes the tests (`mvn test-compile`) before relying on CI
7. web.xml not migrated

## Error list

3 error(s) in this run, one line each (all of them with every try and diff: `errors.md` / `errors.jsonl` in the run directory): resolved 2, note 1.

### build — first `mvn clean package`, new errors only (3)

| # | case | file:line | message | status | tries | resolved |
|---|---|---|---|---|---|---|
| 1 | `case000001` | `module/comp-jar/src/main/java/com/example/A.java:11` | cannot find symbol | resolved | - | - |
| 2 | `case000002` | `module/comp-jar/src/main/java/com/example/A.java:12` | cannot find symbol | resolved | - | - |
| 3 | `case000003` | `module/comp-jar/src/main/java/com/example/A.java:13` | warning: batch line | note | - | - |

Last build log: `/out/run-1/logs/010-build-verify.log`

## Fixes by case

| case | errors | cause | solution | changes (files, +/−) | edits | script |
|---|---|---|---|---|---|---|
| `case000001` cannot find symbol | 1/1 resolved | - | - | - | - | - |
| `case000002` cannot find symbol | 1/1 resolved | - | - | - | - | - |

_Stored scripts live in the KB (`<kb>/cases/<case>/fix_vN.py`); every try with its diff is in `errors.md`, the whole change set in `changes.patch`._

## Structure review

After the restructure and before the first build, the LLM checked the poms, dependencies and module layout against the company rules. It fixes critical issues itself (committed, revertible) and leaves a script for the KB; warnings are only recorded.

- verdict: `issues` — 0 critical, 1 warning(s)
- model: `gpt-x`, 0 LLM call(s)

| severity | file | problem | fix hint |
|---|---|---|---|
| warning | `module/comp-jar/pom.xml` | spring-batch-core follows the parent's Batch line | check the Batch jobs |


## Manual follow-ups

- test sources were neither compiled nor run (build.skip_test_compile: true): the owning team compiles and fixes the tests (`mvn test-compile`) before relying on CI
- web.xml not migrated
- Netflix Hystrix is discontinued (not in any current Spring Cloud train): Configure the circuit breakers by name.

## Flags

- `tests-not-compiled`: every build ran with `-Dmaven.test.skip=true` (build.skip_test_compile): test sources were neither compiled nor run — the owning team compiles and fixes them

## What we changed

Plan steps executed:
1. `transform.rewrite` — OpenRewrite in one run: Java 21 + jakarta
1. `restructure.apply` — restructure into module/comp-jar, module/boot-jar with the in-house parent
1. `restructure.review` — 
1. `verify.build` — final mvn clean package

OpenRewrite runs:

| phase | recipes | changed files | ok |
|---|---|---|---|
| transform | org.openrewrite.java.migrate.UpgradeToJava21, org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta, org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0 | 5 | yes |

## LLM usage and cost

| Model | Calls | Input tokens | Output tokens | Price in / out (per 1M) | Cost |
|---|---|---|---|---|---|
| `gpt-x` | 7 | 52,242 | 2,609 | $0.1000 / $0.5000 | $0.0065 |
| **Total** | | | | | **$0.0065** |

_Cost = input tokens × input price + output tokens × output price, per 1M tokens, in USD; cached-input discounts are not visible to sbmig._

## Notes

- plan: case spring_legacy (confidence 0.75)
- restructure: restructure: managed-version source: effective pom of the new parent chain

## Outputs

- run_dir: `/out/run-1`
- zip: `/out/run-1/result.zip`
- diff: `/out/run-1/changes.patch`
- errors_md: `/out/run-1/errors.md`
- errors: `/out/run-1/errors.jsonl`
- logs: `/out/run-1/logs`
