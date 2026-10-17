---
title: JDK 8/11/17 -> JDK 21 compile issues
keywords:
  - "sun\\.misc"
  - "com\\.sun\\."
  - "sun\\.(reflect|security|net|nio)"
  - "javax\\.xml\\.bind"
  - "javax\\.xml\\.ws|javax\\.jws|javax\\.activation|org\\.omg|javax\\.rmi"
  - "javax\\.annotation\\.(Generated|PostConstruct|PreDestroy|Resource)"
  - "javax\\.script|nashorn|ScriptEngine"
  - "BASE64(En|De)coder"
  - "SecurityManager|AccessController"
  - "Thread\\.(stop|suspend|resume)|finalize"
  - "lombok|mapstruct|annotation process"
  - "release version|invalid target release|source option|target option|class file (major )?version"
  - "add-opens|InaccessibleObjectException|IllegalAccessError"
  - "restricted identifier|'(var|record|yield|sealed|permits)'"
  - "jacoco|maven-compiler-plugin"
  - "is deprecated and marked for removal|has been deprecated"
cases: [pure_java, spring_legacy, spring_boot, servlet_tomcat, msa]
phases: [jdk21]
---
# JDK 21 migration notes (from 8/11/17)

## Removed Java EE / CORBA modules (JDK 11, JEP 320) — add jars, then fix packages
| Missing package | Fix (Boot 4 manages versions) |
|---|---|
| `javax.xml.bind.*` (JAXB) | `jakarta.xml.bind:jakarta.xml.bind-api` + `org.glassfish.jaxb:jaxb-runtime` (runtime); imports → `jakarta.xml.bind.*` |
| `javax.xml.ws.*`, `javax.jws.*` (JAX-WS) | `jakarta.xml.ws:jakarta.xml.ws-api` + `com.sun.xml.ws:jaxws-rt`; imports → `jakarta.xml.ws.*`, `jakarta.jws.*` |
| `javax.activation.*` | `jakarta.activation:jakarta.activation-api` (→ `jakarta.activation.*`) |
| `javax.annotation.PostConstruct/PreDestroy/Resource` | `jakarta.annotation:jakarta.annotation-api` → `jakarta.annotation.*` |
| `javax.annotation.Generated` | `javax.annotation.processing.Generated` (JDK 9+, no dep) or `jakarta.annotation.Generated` |
| `org.omg.*`, `javax.rmi.CORBA` | CORBA removed; no drop-in (glassfish-corba). Escalate unless trivially unused import |
| `javax.transaction.xa` | still in JDK (`java.transaction.xa` module) — keep |
Old pre-jakarta `javax.xml.bind:jaxb-api` 2.3 still works on JDK 21 but conflicts with Boot 4 (Jakarta EE 11): replace.

## Encapsulated / removed internals (JEP 403 strong encapsulation since JDK 17)
- `sun.misc.BASE64Encoder/BASE64Decoder` (removed) → `java.util.Base64.getEncoder().encodeToString(b)` /
  `Base64.getDecoder().decode(s)`; MIME variant `Base64.getMimeEncoder()`.
- `sun.misc.Unsafe` still in `jdk.unsupported` (compiles, warns); memory methods deprecated for removal → VarHandle / FFM.
- `sun.reflect.Reflection.getCallerClass` → `StackWalker.getInstance(RETAIN_CLASS_REFERENCE).getCallerClass()`.
- `sun.security.*`, `sun.net.*`, `com.sun.org.apache.*.internal.*` (internal Xerces/Xalan) → standard JAXP
  (`javax.xml.parsers`, `javax.xml.transform`), `java.net.http.HttpClient`, `java.security` APIs.
- `com.sun.image.codec.jpeg` → `javax.imageio.ImageIO`.
- `com.sun.net.httpserver` is exported (OK). `com.sun.management` OK (jdk.management).
- Runtime `InaccessibleObjectException` / `IllegalAccessError` from reflection-heavy libs (old cglib, xstream, kryo,
  javassist, PowerMock) → upgrade the lib; last resort surefire `<argLine>--add-opens java.base/java.lang=ALL-UNNAMED</argLine>`
  (+ `// MIGRATION-TODO` for runtime JVM flags).

## Removed / changed APIs
- Nashorn (`jdk.nashorn.*`, `ScriptEngineManager().getEngineByName("nashorn")` returns null) removed in 15 →
  `org.openjdk.nashorn:nashorn-core:15.4` (same engine name) or GraalJS.
- `Thread.stop()`, `suspend()`, `resume()` throw `UnsupportedOperationException` (19–21) → interrupt-based cancel.
- `Object.finalize` deprecated for removal (JEP 421) → `java.lang.ref.Cleaner` / try-with-resources (warning only).
- `SecurityManager`/`AccessController` deprecated for removal (JEP 411) — compile warning only; `System.setSecurityManager` throws at runtime.
- Deprecated-for-removal constructors `new Integer(x)`, `new Long(x)`, `new Double(x)`, `new Boolean(x)` → `valueOf`.
- `new Locale(..)` deprecated (19) → `Locale.of(..)`; `new URL(String)` deprecated (20) → `URI.create(s).toURL()`.
  These are warnings; they only fail with `-Werror` (`<failOnWarning>`/`-Werror` in compilerArgs → keep, fix call).
- `java.security.acl`, RMI activation (`java.rmi.activation`), `javax.security.cert` → removed/deprecated.
- Restricted identifiers: a type named `var`, `record`, `yield`, `sealed`, `permits` is illegal; `_` is not an
  identifier; unqualified call `yield(x)` must be `this.yield(x)` / `Thread.yield()`.
- `String.formatted(...)`, text blocks, records, sealed classes, switch patterns are fine on 21.

## Build tooling floors for JDK 21
- `maven-compiler-plugin` ≥ 3.13 (3.14.0 / Boot-managed) with `<release>21</release>` (or `maven.compiler.release=21`);
  remove `<source>/<target>` 1.8 and `<compilerArguments><bootclasspath>`; `--release` also hides internal
  `sun.*` APIs that `-source/-target` exposed (`package sun.misc does not exist`) → fix the code, not the flag.
- `invalid target release: 21` → the build runs on an older JDK (environment problem, not code).
- Lombok ≥ 1.18.30 (JDK 21 support; older fails with `java.lang.NoSuchFieldError: ... JCTree` / `TypeTag :: UNKNOWN`).
- MapStruct ≥ 1.6 (1.6.3); with Lombok add `org.projectlombok:lombok-mapstruct-binding:0.2.0` to
  `<annotationProcessorPaths>` (order: lombok, binding, mapstruct-processor).
- When `<annotationProcessorPaths>` is declared, every processor (lombok, mapstruct, hibernate-processor,
  querydsl-apt, spring-boot-configuration-processor) must be listed there.
- Surefire/failsafe ≥ 3.2 (3.5.3); JaCoCo ≥ 0.8.11; SpotBugs ≥ 4.8; maven-shade ≥ 3.5; Byte Buddy ≥ 1.14.
  "Unsupported class file major version 65" = an ASM-based tool is too old → bump it.
- Mockito 5 (inline mock maker default); PowerMock does not work on 17+ → Mockito `mockStatic`.
