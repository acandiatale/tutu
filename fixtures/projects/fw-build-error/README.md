# fw-build-error fixture (framework project, build still fails after the conversion; v2.1z-aw)

* `packaging` jar, Java 8, declares the framework core stand-in `com.example.fw:example-fw-core:1.0.0`
  (`fixtures/libs/example-fw`, installed by `fixtures/libs/install.sh`)
* `src/main/java/com/example/fwapp/App.java` calls `System.runFinalizersOnExit(true)`: in the JDK up to Java 10,
  removed in Java 11. No OpenRewrite recipe of the reduced run replaces it, so the JDK 21 build fails with
  `cannot find symbol`.
* `sbmig.e2e.yml`: `framework.artifacts: ["example-fw-core"]`; `framework.fix_loop: false` set explicitly (the default is `true` since v2.1z-bi)

## Expected sbmig outcome (asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

Flag `framework-project`, journal `framework_project` / `plan` with scope `jdk-jakarta`, name `Example` and `fix_loop`
false. The run converts (Java 21 recipes) and builds ONCE: journal `verify_loop_skipped` (reason
`framework.fix_loop: false`), no `verify_loop_start`, **0 LLM calls**, no fix attempt. Not a success: exit 2, the error
in `App.java` is the unresolved error (its error-list entry is unresolved, without a try, with the note
`not tried: framework project …`), and the first hand-over line is
`framework project: build errors are not fixed automatically (framework.fix_loop: false) — 1 error(s) remain after the
JDK 21 + jakarta conversion; see the error list`. The converted tree is delivered (`result.zip`). `report.md` opens
the block `## Example framework project` with "**Example framework projects get JDK 21 and Jakarta only.**".

## Build as-is

```bash
fixtures/libs/install.sh            # once: example-fw-core into the local repository
JAVA_HOME=<a JDK 8 home> mvn -q -DskipTests package
```

On JDK 21 the same command fails in `App.java` (`cannot find symbol: method runFinalizersOnExit(boolean)`).
