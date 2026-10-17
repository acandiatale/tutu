# euckr-legacy fixture (scenario E1: legacy MS949 / CP949 source encoding)

`pure-java`-like plain Java 8 jar (`maven.compiler.source/target 1.8`, compiler plugin 3.8.1, commons-lang3 3.9,
JUnit 4.12) whose **sources are encoded in CP949 (MS949)** — Korean comments and string literals
(`"안녕하세요"`, `"좋은 아침입니다"`, `"yyyy년 MM월 dd일"` …) and — common in Korean legacy code — **Korean
identifiers** (`NoticeFormatter.등급 { 일반, 긴급 }`), written with Python `encoding="cp949"`.
The pom declares **no `project.build.sourceEncoding`** and no compiler `<encoding>`: the as-is team built on Korean
Windows, where the platform encoding is MS949.

> The `.java` files in this directory are intentionally **not UTF-8**. Do not open-and-save them with a UTF-8 editor;
> `tests/fixtures/test_fixture_projects.py` asserts they fail to decode as UTF-8 and decode as cp949.

## Build as-is

The macOS JDK 8 default encoding is UTF-8, so a plain build fails: javac logs `unmappable character for encoding
UTF8` for every Korean byte and then `illegal character: '\ufffd'` for the Korean identifiers. (The unmappable-character
lines alone do **not** fail the build — comments/strings would just be silently corrupted, tests included — which is
why the fixture has Korean identifiers: the build must break loudly.)
Reproduce the as-is Windows environment by setting the platform encoding of the Maven JVM to MS949:

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
JAVA_TOOL_OPTIONS=-Dfile.encoding=MS949 \
  mvn -q -DskipTests package
```

(Tests pass the same way with `mvn -q test`.) Without `JAVA_TOOL_OPTIONS` the same command fails — this is the
error sbmig sees on JDK 21 (JDK 18+ defaults to UTF-8, JEP 400): `unmappable character (0xC0) for encoding UTF-8`
followed by `illegal character: '\ufffd'` in `notice/NoticeFormatter.java`.

## Expected sbmig outcome (asserted by `scripts/e2e.py`)

* handler `source-encoding` detects cp949 and sets `project.build.sourceEncoding` (or converts to UTF-8) →
  flag matching `source-encoding-set:(cp949|ms949|euc-kr|euc_kr)`
* `success = true`, exit 0, `llm_usage.calls == 0`
* post-check: the migrated `Greeting.java` (anywhere under the run's `project/`) still decodes as **cp949** and
  contains `안녕하세요` — i.e. no step (rewrite, handler, restructure) re-encoded or corrupted the Korean text.
  If the engine chooses `convert-utf8` instead, this post-check must be changed to `"encoding": "utf-8"`.
