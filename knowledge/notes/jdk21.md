---
title: JDK 8/11/17 -> JDK 21 compile issues
keywords:
  - "sun\\.misc"
  - "com\\.sun\\."
  - "sun\\.(reflect|security|net|nio)"
  - "javax\\.xml\\.bind"
  - "javax\\.swing\\.tree"
  - "DefaultMutableTreeNode"
  - "inference variable T has incompatible"
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
  - "-Werror|failOnWarning|warnings found and -Werror specified"
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
Old pre-jakarta `javax.xml.bind:jaxb-api` 2.3 still works on JDK 21: replace it with the jakarta pair for project code,
but keep it where a javax-only library needs it at runtime (jjwt 0.9.x, era-libraries.md) — both packages coexist.

## Encapsulated / removed internals (JEP 403 strong encapsulation since JDK 17)
- `sun.misc.BASE64Encoder/BASE64Decoder` (removed) → `java.util.Base64.getEncoder().encodeToString(b)` /
  `Base64.getDecoder().decode(s)`; MIME variant `Base64.getMimeEncoder()` (`BASE64Encoder.encode` wrapped lines at 76
  chars like the MIME encoder; `com.sun.org.apache.xerces.internal.impl.dv.util.Base64` → `java.util.Base64` too).
- `sun.misc.Unsafe` still in `jdk.unsupported` (compiles, warns); memory methods deprecated for removal → VarHandle / FFM.
- `sun.reflect.Reflection.getCallerClass` → `StackWalker.getInstance(RETAIN_CLASS_REFERENCE).getCallerClass()`.
- `sun.security.*`, `sun.net.*`, `com.sun.org.apache.*.internal.*` (internal Xerces/Xalan) → standard JAXP
  (`javax.xml.parsers`, `javax.xml.transform`), `java.net.http.HttpClient`, `java.security` APIs.
- `com.sun.image.codec.jpeg` → `javax.imageio.ImageIO`.
- `com.sun.net.httpserver` is exported (OK). `com.sun.management` OK (jdk.management).
- Runtime `InaccessibleObjectException` / `IllegalAccessError` from reflection-heavy libs (old cglib, xstream, kryo,
  javassist, PowerMock) → upgrade the lib; last resort surefire `<argLine>--add-opens java.base/java.lang=ALL-UNNAMED</argLine>`
  (+ `// MIGRATION-TODO` for runtime JVM flags).

## fix-loop: jdk-removed-apis
Pre-Java-8 code on JDK 21 (`--release 21` hides `sun.*`): port the call, never re-add a flag or an old JDK.
`UseJavaUtilBase64` is part of `UpgradeToJava21`, but OpenRewrite parses on JDK 21, where `sun.misc.BASE64Encoder`
no longer exists, so those calls usually reach you unchanged. sbmig ships no compatibility shim classes.
- `sun.misc.BASE64Encoder().encode(b)` → `java.util.Base64.getMimeEncoder().encodeToString(b)` when the output is
  stored or compared (the JDK 8 encoder wrapped lines at 76 chars; the MIME encoder wraps the same way with `\r\n`),
  otherwise `Base64.getEncoder().encodeToString(b)` (no line breaks) + `// MIGRATION-TODO: BASE64Encoder line breaks dropped`;
  `new BASE64Decoder().decodeBuffer(s)` → `Base64.getMimeDecoder().decode(s)` (tolerates line breaks; drop the
  `IOException` from `throws`/catch only if nothing else throws it — keep public signatures).
- `sun.misc.Unsafe` compiles (jdk.unsupported): leave it, add `// MIGRATION-TODO: Unsafe → VarHandle`.
- `javax.xml.bind.*`: the jdk21 recipe adds `jakarta.xml.bind:jakarta.xml.bind-api` + `org.glassfish.jaxb:jaxb-runtime`;
  if it is still missing, add those (Boot-managed, no version) — do not hand-write a date/base64 parser.
  `DatatypeConverter.printBase64Binary/parseBase64Binary` → `java.util.Base64`.
- `javax.annotation.PostConstruct/PreDestroy/Resource` → `jakarta.annotation-api`; `javax.activation` →
  `jakarta.activation:jakarta.activation-api`; `com.sun.image.codec.jpeg` → `javax.imageio.ImageIO`.
- `_` as identifier → rename (`ignored`, `unused`); `Thread.stop/suspend/resume` → interrupt + flag.

## fix-loop: jdk-generified-apis
JDK 9 generified `javax.swing.tree`: `DefaultMutableTreeNode.children()`, `preorderEnumeration()`,
`depthFirstEnumeration()`, `breadthFirstEnumeration()`, `pathFromAncestorEnumeration()` and `TreeNode.children()`
return `Enumeration<TreeNode>`. javac 21 reports "incompatible types: inference variable T has incompatible equality
constraints" or "Enumeration<TreeNode> cannot be converted to Enumeration<DefaultMutableTreeNode>".
- `Enumeration<DefaultMutableTreeNode> e = node.children()` → `Enumeration<TreeNode> e = node.children()` and cast
  where the element is used: `DefaultMutableTreeNode child = (DefaultMutableTreeNode) e.nextElement();` — or
  `for (TreeNode t : Collections.list(node.children())) { DefaultMutableTreeNode child = (DefaultMutableTreeNode) t; … }`.
- Raw `Enumeration e = node.children()` + `(DefaultMutableTreeNode) e.nextElement()` already compiles: leave it.
- Keep method signatures; do not add `@SuppressWarnings` to hide the error and never change the JDK level.

## Removed / changed APIs
- Nashorn (`jdk.nashorn.*`, `ScriptEngineManager().getEngineByName("nashorn")` returns null) removed in 15 →
  `org.openjdk.nashorn:nashorn-core` (same engine name; not Boot-managed: `version: null`) or GraalJS.
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
- `maven-compiler-plugin` ≥ 3.13 (the parent chain's version when it manages the plugin) with `<release>21</release>`
  (or `maven.compiler.release=21`);
  remove `<source>/<target>` 1.8 and `<compilerArguments><bootclasspath>`; `--release` also hides internal
  `sun.*` APIs that `-source/-target` exposed (`package sun.misc does not exist`) → fix the code, not the flag.
- `invalid target release: 21` → the build runs on an older JDK (environment problem, not code).
- Lombok ≥ 1.18.30 (JDK 21 support; older fails with `java.lang.NoSuchFieldError: ... JCTree` / `TypeTag :: UNKNOWN`).
- MapStruct: keep the as-is version line; with Lombok, `org.projectlombok:lombok-mapstruct-binding` belongs in
  `<annotationProcessorPaths>` (order: lombok, binding, mapstruct-processor; its version from the Dependency versions
  section — never invent one).
- When `<annotationProcessorPaths>` is declared, every processor (lombok, mapstruct, hibernate-processor,
  querydsl-apt, spring-boot-configuration-processor) must be listed there.
- Surefire/failsafe ≥ 3.2; JaCoCo ≥ 0.8.11; SpotBugs ≥ 4.8; maven-shade ≥ 3.5.1; Byte Buddy ≥ 1.14 (floors, not
  versions to write: maven-plugins.md).
  "Unsupported class file major version 65" = an ASM-based tool is too old → bump it.
- Mockito 5 (inline mock maker default); PowerMock does not work on 17+ → Mockito `mockStatic`.
