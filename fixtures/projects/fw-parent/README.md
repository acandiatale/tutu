# fw-parent fixture (framework project, inherited from the parent; reduced scope `jdk-jakarta`)

* `packaging` war, Java 8; `<parent>` = `com.example.fw:example-fw-parent:1.0.0` (`fixtures/libs/example-fw`, installed
  by `fixtures/libs/install.sh`), which declares `example-fw-core` in its `<dependencies>` and manages the compiler / war
  plugin versions; this pom never declares `example-fw-core`
* the same sources and JSP as `fw-direct`
* `sbmig.e2e.yml`: `framework.artifacts: ["com.example.fw:example-fw-core"]` (groupId:artifactId pattern)

## Build as-is

```bash
fixtures/libs/install.sh            # once: example-fw-parent + example-fw-core into the local repository
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
  mvn -q -DskipTests package
```

## Expected sbmig outcome (asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

As `fw-direct`, with evidence `parent` (from the as-is effective pom; `parent` = `com.example.fw:example-fw-parent:1.0.0`).
The `<parent>` is unchanged (never aligned to the configured company parent), `example-fw-core` is still not declared.
