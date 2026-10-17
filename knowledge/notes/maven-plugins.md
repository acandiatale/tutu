---
title: Maven plugins and compiler settings safe for JDK 21
keywords:
  - "maven-(compiler|surefire|failsafe|jar|war|resources|source|javadoc|enforcer|shade|assembly|dependency|antrun)-plugin"
  - "Plugin .* could not be resolved|Failed to execute goal|PluginResolutionException|Unsupported class file major version"
  - "release|<source>|<target>|maven\\.compiler|invalid flag|--release|bootclasspath"
  - "annotationProcessorPaths|maven\\.compiler\\.proc|-proc"
  - "enforcer|RequireJavaVersion|requireJavaVersion|requireMavenVersion"
  - "jacoco|spotbugs|checkstyle|pmd|errorprone|animal-sniffer"
  - "spring-boot-maven-plugin|repackage|loaderImplementation|layout"
  - "argLine|add-opens"
cases: [pure_java, spring_legacy, spring_boot, servlet_tomcat, msa]
phases: [jdk21, spring, restructure]
---
# Maven plugins for JDK 21 (versions known to work; Boot parent manages most of them — prefer no explicit version then)

| Plugin | Version | Notes |
|---|---|---|
| maven-compiler-plugin | 3.14.0 (Boot 4.1 parent: 3.15) | `<release>21</release>`; drop `<source>/<target>`/`<compilerVersion>`/`<fork>` + `<executable>` |
| maven-surefire-plugin / failsafe | 3.5.3 | ≥ 3.2 needed for JDK 21; `<argLine>` keeps `@{argLine}` when JaCoCo is used |
| maven-jar-plugin | 3.4.2 | |
| maven-war-plugin | 3.4.0 | `failOnMissingWebXml=false` only for WAR modules |
| maven-resources-plugin | 3.3.1 | |
| maven-source-plugin | 3.3.1 | |
| maven-javadoc-plugin | 3.11.2 | javadoc lint errors on 21 → `<doclint>none</doclint>` (build-only setting, allowed) |
| maven-enforcer-plugin | 3.5.0 | `requireJavaVersion` `[21,)`; drop rules pinning `1.8` |
| maven-shade-plugin | ≥ 3.5.1 | older ASM fails on class version 65 |
| maven-assembly-plugin | 3.7.1 | |
| maven-dependency-plugin | 3.8.1 | |
| maven-antrun-plugin | 3.1.0 | |
| jacoco-maven-plugin | ≥ 0.8.11 (0.8.13) | older: "Unsupported class file major version 65" |
| spotbugs-maven-plugin | ≥ 4.8.x | |
| maven-checkstyle-plugin / pmd | 3.6.0 / 3.26.0 | |
| spring-boot-maven-plugin | managed (= Boot version) | no `<version>` under the Boot/company parent |

## Compiler configuration
- `<release>` (javac `--release`) vs `<source>/<target>`: release also restricts the API to the target JDK and hides
  `sun.*`/internal packages; use `<maven.compiler.release>21</maven.compiler.release>` property or plugin `<release>`.
  Both set → release wins; remove the old `<source>1.8</source>` to avoid confusion. `java.version` property is used
  by the Boot parent to set release.
- Old `<compilerArguments><bootclasspath>`, `-Xlint:-options` hacks, `<compilerId>eclipse</compilerId>` → remove/upgrade.
- Annotation processing: `maven.compiler.proc` / `<proc>` = `full` | `none` | `only`. JDK 21 warns when processors are
  found on the classpath implicitly → declare them in `<annotationProcessorPaths>` (lombok, mapstruct-processor,
  lombok-mapstruct-binding, hibernate-processor, querydsl-apt:jakarta, spring-boot-configuration-processor) with versions
  (Boot-managed versions can be referenced as `${lombok.version}` etc. under the Boot parent).
- `-parameters` is set by the Boot parent; without it Spring 6.1+/7 cannot resolve `@PathVariable`/`@RequestParam` names
  by reflection → add `<parameters>true</parameters>`.

## spring-boot-maven-plugin (Boot 4)
- Remove `<loaderImplementation>CLASSIC</loaderImplementation>` and `<executable>true</executable>` (removed features).
- `<layout>ZIP</layout>`/`PropertiesLauncher` still exist; `<includeOptional>true</includeOptional>` if optional deps are needed at runtime.
- Only the boot module (boot-jar) runs `repackage`; library modules (comp-jar) must not.

## Resolution errors
- "Plugin X:Y could not be resolved" with an old version → bump to the table version (Nexus may not proxy ancient versions).
- `Failed to execute goal ...:enforce` → adjust the enforcer rule to 21 rather than skipping the plugin.
