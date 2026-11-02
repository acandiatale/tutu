# grid8 — a vendor grid library with a javax line and a `-jakarta` release (v2.1z-e, 2026-11-02)

Stand-in for a commercial UI component of a company project (placeholder coordinates: `com.example.grid8:grid8`;
the real vendor's coordinates carry a version-line digit in the groupId AND the artifactId the same way).
Installed into the local repository by `fixtures/libs/install.sh`:

| version | API | pom | built with |
|---|---|---|---|
| `1.0.6` | `javax.servlet` (`GridExporter.export(HttpServletRequest, HttpServletResponse)`) | declares `javax.servlet-api` provided | JDK 8 |
| `1.1.15` | `javax.servlet` | declares `javax.servlet-api` provided | JDK 8 |
| `2.0.5-jakarta` | `jakarta.servlet`, same class / method | **silent** — no `<dependencies>` (the jakarta API comes from a build-time profile that consumers never see) | JDK 21, release 17 |

What it proves (fixture `servlet-javax-grid`): after the recipes the servlet is on `jakarta.servlet`, the call into
`GridExporter` fails as `external_javax_api`, and sbmig's jakarta build discovery (`sbmig/build/jakarta_discovery.py`)
finds `2.0.5-jakarta` under the SAME coordinates — a higher major whose pom says nothing, so the **version qualifier**
(and the jar scan of the class files' `jakarta/servlet/` references) is what verifies it — and the zero-LLM pre-try
`sbmig-jakarta` swaps the declaration `1.0.6 → 2.0.5-jakarta` (⚠ major upgrade for the operator) before any LLM call.
The digit-variant (`com.example.grid9:grid9`) and `-jakarta` artifactId variants are covered by unit tests of the discovery
only (`tests/build/test_jakarta_discovery.py`).
