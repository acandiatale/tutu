# Library advisories

Some libraries are end of life or no longer version-managed by the company parent or the Boot 4 BOM, yet they
**still compile** (springfox, Hystrix, Sleuth, old Spring Cloud trains, iBATIS 2, …). Without advisories such a run
looks green. Advisories make them visible.

Advisories are **guide-only**: they never add a recipe, never fail a run and never change the exit code. There is no
`block` policy and there will not be one (user: "정책상 빌드로 보지 않는건 없을거야" — 500 projects cannot all be
perfect). The build goes as far as it can; the owning team gets a compressed Handover ("from here on, your team
replaces X with Y").

Files:

- `knowledge/libraries/advisories.yml` — the curated list (67 entries).
- `sbmig/advisory/` — `catalog.py` (load + merge), `evaluate.py` (match + follow-ups), `hints.py` (script-prompt
  hints), `verify.py` (final outcome), `cli.py` (`sbmig advisories`).
- `knowledge/notes/*.md` — migration notes; an advisory's `notes:` points at one.

## File format (version 1)

```yaml
version: 1
advisories:
  - id: springfox                      # unique; later files override by id
    library: springfox                 # analyzer rule name (sbmig/analyze/libraries.py LIBRARY_NAMES)
    artifacts: ["io.swagger:swagger-annotations"]   # extra group:artifact-glob patterns
    status: eol                        # eol | unmanaged | incompatible | superseded
    title: Springfox (Swagger 2) is discontinued
    summary: one paragraph for the report
    replacement: org.springdoc:springdoc-openapi-starter-webmvc-ui
    replacement_version: "3.x (Boot 4)"
    followup: the "from here on, your team …" sentence (concrete, short)
    docs: [https://springdoc.org/#migrating-from-springfox]
    applies_to: []                     # case types (empty = all)
    applies_when_version_below: null   # e.g. "2025.1"; unparsable versions (Hoxton.SR12) count as below
    resolved_min_version: null         # still present but at least this version → outcome "replaced"
    notes: knowledge/notes/spring-cloud.md
```

The model is `LibraryAdvisory` in `sbmig/core/models.py`; a `policy` / `recipes` key left in a file is ignored. An
entry needs `library` or `artifacts` (artifact globs such as `com.company:legacy-*` work without an analyzer rule).
A broken entry is skipped and reported by `sbmig advisories check`; a missing file means no findings.

## From advisory to finding, hint and Handover line

1. **Evaluate (analyze).** `evaluate_advisories(profile, cfg)` matches an entry when the analyzer found its
   `library`, or an `artifacts` glob matches a dependency, a dependencyManagement entry or a parent of any pom — or a
   **bundled jar** (a jar in the tree no pom declares) with those coordinates. Then `applies_to`,
   `applies_when_version_below` (only concrete versions count, `${…}` is ignored) and the off switch. Each match is an
   `AdvisoryFinding` (`policy: guide`, outcome `unknown`), ordered eol → incompatible → unmanaged → superseded. Findings
   go to `report.json` (`advisories`), the journal (`advisories`) and one plan note each
   (`advisory: <id> → guide (LLM hints + Handover)`).
2. **Script-prompt hint (fix loop).** For each case, `hints_for(errors, findings, profile)` picks the findings whose
   library the errors mention: an import package of the library rule (segment boundary, e.g. `com.netflix.hystrix`),
   an as-is artifact `g:a`, or the replacement artifact. One line per finding (≤ 500 chars):
   `<library> (<status>): replace with <replacement> (<replacement_version>). <followup>`, plus an excerpt (≤ 600 chars)
   of the note's `## fix-loop: <library>` section (else the first heading naming the library). The prompt shows them
   as "Library migration guidance (what to migrate TO)"; system-prompt rule 10 tells the LLM those libraries were
   removed on purpose — port the code to the replacement instead of re-adding the library. The attempt records
   `advisory_ids`.
3. **Blocked items.** When a javax-era artifact is blocked (no jakarta build found), the suggestion names its EOL
   advisory and the replacement.
4. **Verify (finalize).** `verify_advisories(findings, ws, cfg)` re-detects the libraries on the final tree:
   `replaced` (gone, or at / above `resolved_min_version`), `still-present`, or `unknown` (re-scan failed).
5. **Attribute.** Successful applied LLM scripts whose prompt carried the hint fill the finding's
   `llm_fixed_files` / `llm_fix_notes`.
6. **Handover.** `followups_for(findings)` gives one sentence per finding, not-replaced first; the report's Handover
   section lists them first in "from here on", then the files the LLM rewrote for each library (review them), the
   versions carried over from the as-is build (`advisories.report_carried_over`), and the other follow-ups, capped at
   `advisories.max_from_here` lines.

## `knowledge/notes` and `replacement: true`

Notes are Markdown with a front-matter (`title`, `keywords` regexes, `cases`, `phases`, `always`, optionally
`replacement`), indexed in `knowledge/notes/index.yml` (`tests/fix/test_fix_notes.py` keeps the two in sync).
`sbmig/fix/context.py` (`ContextPacker`) selects the notes whose keywords match a case's errors and puts them in the
script prompt; a `## fix-loop: <library>` section is guidance written for the LLM.

`replacement: true` (today `spring-mobile.md`) means sbmig knows the fix is **project code**: the library has no
jakarta build and must be replaced, not carried. For a case whose javac text or whose files' import lines
(`sbmig.triage.blocked.note_text`) match such a note:

- the triage never blocks it as `external_javax_api` / `external_api_incompatible`;
- an LLM decline is rejected and re-asked with the note's title as the contradicting fact; a rejected decline that
  still carries edits is applied;
- the answer is usually `<no_script>` + `<edits>` (new classes are not reusable by another project), capped by
  `script.max_edit_tries_per_case`.

## Configuration

```yaml
advisories:
  enabled: true
  file: knowledge/libraries/advisories.yml
  extra_files: [/etc/sbmig/company-advisories.yml]   # same schema, merged by id, later file wins
  policy_overrides: {zuul: off}                      # only "off" has an effect (switches the entry off)
  report_carried_over: true
  max_from_here: 40
```

Use extra files for in-house EOL libraries or to rewrite a follow-up sentence for the company's teams.

## What the list covers

`sbmig advisories list` prints every entry (id, library, status, state, replacement, notes). By status:

- **eol** — springfox, sleuth, hystrix, ribbon, zuul, netflix-feign, spring-security-oauth2-legacy, log4j1,
  commons-lang2, tiles, jersey1, spring-portlet, spring-mobile, struts1, shiro-legacy, hibernate-ehcache, ehcache2,
  commons-dbcp1, bitronix, commons-collections3, orika, elasticsearch-rest-high-level, solr-legacy, jackson1 /
  jackson1-direct, json-lib, jdom1, axis1, commons-configuration1, cglib, commons-httpclient3, jexcelapi, powermock.
- **incompatible** — undertow, jdk-removed-apis (`sun.misc` …), ibatis2, jdk-generified-apis (`javax.swing.tree`),
  hibernate-commons-annotations, velocity, commons-fileupload, jasperreports6, struts2, egovframe, hibernate-template,
  hibernate-search-legacy, querydsl-hibernate-native, hazelcast-legacy, druid, xstream, commons-email1, jjwt-legacy,
  jxls1, slf4j-1-pin.
- **superseded** — spring-cloud-bom (below 2025.1), httpclient4 / httpclient4-direct, thymeleaf-legacy,
  spring-hateoas-legacy, webjars-locator-core, hibernate-entitymanager, hypersistence-utils, dozer, poi-legacy,
  mockito-legacy.
- **unmanaged** — commons-chain, jasypt-spring-boot, atomikos.

Several of these libraries are migrated by recipes the plan already runs (springfox, Sleuth, the Spring Cloud train,
HttpClient 5, Jackson 1 → 2 inside the Boot umbrella / Framework 7 — see [RECIPES.md](RECIPES.md)); the advisory then
ends `replaced` and the Handover says to review the result. Libraries with a `## fix-loop:` section (ibatis2,
jdk-removed-apis, jdk-generified-apis, hibernate-commons-annotations, the era libraries in
`knowledge/notes/era-libraries.md`, …) get a porting guide in the script prompt.

## CLI

```
sbmig advisories list [--json] [-c cfg.yml]
sbmig advisories show <id> [--json]
sbmig advisories check      # unknown library, bad artifact pattern, missing notes file, duplicate id, bad applies_to; exit 1
```
