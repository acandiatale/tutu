# boot2-qlrm-javax fixture (scenario X1b: a third-party library that moved to jakarta.* in a later release)

Copy of `boot2` (Boot 2.7.18, Java 11, security, JPA, lombok, commons-lang3; tests dropped) plus **QLRM 1.7.1 under its
old coordinates `ch.simas.qlrm:qlrm`** (2017): `query/MemberSummaryQuery` maps a native query to `MemberSummary`
constructors with `new JpaResultMapper().list(q, MemberSummary.class)`, and `JpaResultMapper.list` takes
`javax.persistence.Query` in every QLRM release up to 3.0.4.

This is the fourth company project of 2026-10-22: after the javax → jakarta migration the project sources are fine,
but the call fails with `incompatible types: jakarta.persistence.Query cannot be converted to javax.persistence.Query`
(javac names the javax *type*, so the report attributed the API jar `javax.persistence-api:2.2` instead of QLRM).
QLRM 4.0.1 (2023-02) moved to `jakarta.persistence` (requires Java 17); 4.2.1 (2026-10-07) is the latest release.

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
```

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

Success, exit 0, **2 LLM calls**: the structure review (one warning on QLRM 1.7.1) and one new fix script for the case
`compile|cannot_access|Query` (`854a1d9f13`) that relocates `ch.simas.qlrm:qlrm:1.7.1` → `org.qlrm:qlrm:4.2.1` (v1 did
this with the handler `jakarta-library-floor`, gone in v2). `post_checks`: `org.qlrm:qlrm` declared with a version,
`ch.simas.qlrm:qlrm` gone, `MemberSummaryQuery.java` on jakarta.
