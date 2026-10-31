---
title: Maven plugins and compiler settings safe for JDK 21
keywords:
  - "maven-(compiler|surefire|failsafe|jar|war|resources|source|javadoc|enforcer|shade|assembly|dependency|antrun)-plugin"
  - "Plugin .* could not be resolved|Failed to execute goal|PluginResolutionException|Unsupported class file major version"
  - "--release|<release>|release version|invalid target release|maven\\.compiler\\.release|<source>|<target>|invalid flag|bootclasspath"
  - "annotationProcessorPaths|maven\\.compiler\\.proc|-proc"
  - "enforcer|RequireJavaVersion|requireJavaVersion|requireMavenVersion"
  - "jacoco|spotbugs|checkstyle|pmd|errorprone|animal-sniffer"
  - "spring-boot-maven-plugin|repackage|loaderImplementation|<layout>|PropertiesLauncher"
  - "argLine|add-opens"
  - "MalformedInputException|Input length = 1|nonFilteredFileExtensions|<filtering>"
cases: [pure_java, spring_legacy, spring_boot, servlet_tomcat, msa]
phases: [jdk21, spring, restructure]
---
# Maven plugins for JDK 21 (floors; the versions come from the parent chain)

Plugin versions come from the `<pluginManagement>` of the poms a module INHERITS (`<parent>` chain: the company
parents, or `spring-boot-starter-parent` / `spring-boot-dependencies` when they are a parent). A BOM imported with
`<scope>import</scope>` manages dependency versions only — never plugin versions, and it defines no properties such
as `${lombok.version}` / `${maven-compiler-plugin.version}` (those exist only when the parent chain defines them).
- A plugin the parent chain manages: remove an explicit `<version>` that is below its JDK 21 floor.
- A plugin nobody manages and below its floor: raise it to a release at or above the floor that the repository has
  (the Dependency versions section / parent-chain facts when they name one); never downgrade a plugin.

| Plugin | JDK 21 floor | Notes |
|---|---|---|
| maven-compiler-plugin | 3.13 | `<release>21</release>`; drop `<source>/<target>` / `<compilerVersion>` / `<fork>` + `<executable>` |
| maven-surefire-plugin / failsafe | 3.2 | `<argLine>` keeps `@{argLine}` when JaCoCo is used |
| maven-war-plugin | — | WAR modules only; 3.x already defaults `failOnMissingWebXml=false` |
| maven-javadoc-plugin | — | javadoc lint errors on 21 → `<doclint>none</doclint>` (build-only setting, allowed) |
| maven-enforcer-plugin | — | `requireJavaVersion` `[21,)`; drop rules pinning `1.8` |
| maven-shade-plugin | 3.5.1 | older ASM fails on class version 65 |
| jacoco-maven-plugin | 0.8.11 | older: "Unsupported class file major version 65" |
| spotbugs-maven-plugin | 4.8 | |
| spring-boot-maven-plugin | — | no `<version>` where the parent chain manages it (= the Boot version) |
"—" and jar / resources / source / assembly / dependency / antrun / checkstyle / pmd: no JDK 21 floor known — keep
what the parent chain gives.

## Compiler configuration
- `<release>` (javac `--release`) vs `<source>/<target>`: release also restricts the API to the target JDK and hides
  `sun.*` / internal packages; use the `<maven.compiler.release>21</maven.compiler.release>` property or plugin
  `<release>`. Both set → release wins; remove the old `<source>1.8</source>`. Only `spring-boot-starter-parent` maps
  `java.version` to the compiler release — under a company parent set `maven.compiler.release` explicitly unless the
  parent chain facts show it.
- Old `<compilerArguments><bootclasspath>`, `-Xlint:-options` hacks, `<compilerId>eclipse</compilerId>` → remove.
- Annotation processing: `maven.compiler.proc` / `<proc>` = `full` | `none` | `only`. JDK 21 warns when processors are
  found on the classpath implicitly → declare them in `<annotationProcessorPaths>` (lombok, mapstruct-processor,
  lombok-mapstruct-binding, hibernate-processor, querydsl-apt:jakarta, spring-boot-configuration-processor). With
  maven-compiler-plugin ≥ 3.12 an entry may omit `<version>` and takes it from dependencyManagement (a BOM import counts).
- `-parameters` is set only by `spring-boot-starter-parent` (the Framework recipes add `maven.compiler.parameters`);
  without it Spring 6.1+ / 7 cannot resolve `@PathVariable` / `@RequestParam` names by reflection → add
  `<parameters>true</parameters>` (or the property) where neither is present.

## spring-boot-maven-plugin (Boot 4)
- Remove `<loaderImplementation>CLASSIC</loaderImplementation>` and `<executable>true</executable>` (removed features).
- `<layout>ZIP</layout>` / `PropertiesLauncher` still exist; `<includeOptional>true</includeOptional>` if optional deps are needed at runtime.
- Only the boot module (boot parent) runs `repackage`; library modules (comp parent) must not.

## Resource filtering and binary files
- `<filtering>true</filtering>` over a directory holding webfonts (eot/ttf/woff/woff2), keystores, archives or Office
  templates: maven-resources-plugin ≥ 3.2 fails with `filtering … failed with MalformedInputException: Input length = 1`;
  2.x silently corrupted those files instead. sbmig already writes `nonFilteredFileExtensions` for the binary files it
  found; for one it missed add `<nonFilteredFileExtensions><nonFilteredFileExtension>eot</nonFilteredFileExtension>…`
  on the plugin (`maven-war-plugin` has the same parameter for filtered `<webResources>`); jpg/jpeg/gif/bmp/png are
  already exempt. Never turn filtering off (`@project.version@` stops resolving) and never delete the files.

## Resolution errors
- "Plugin X:Y could not be resolved" with an old version → remove the explicit version when the parent chain manages
  the plugin, else a release at or above the floor that the repository has (Nexus may not proxy ancient versions).
- `Failed to execute goal ...:enforce` → adjust the enforcer rule to 21 rather than skipping the plugin.
