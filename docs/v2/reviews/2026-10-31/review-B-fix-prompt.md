<!-- Raw sub-agent review report (English), 2026-10-31, commit 1b30159. Consolidated Korean summary: docs/v2/PROMPT-REVIEW-2026-10-31.md. Scratchpad paths named below were session-local. -->

# Review B — fix-loop LLM prompt (system prompts, policy note, injected facts)

Commit `1b30159` (`flow-v2`). Read-only; nothing in the repo changed.

**Evidence base.** The two named runs have no prompt records. `llm.record_dir` was unset, and the replay files hold only answers. I re-ran the same fixtures into the scratchpad with `llm.record_dir` set. The replayed answers were identical: 9 calls each, the same `SBMIG-RESULT … resolved=67`.
- `…/scratchpad/rerun/rec-ssb/` — spring5-security-batch as e2e runs it. The local e2e config pins the pre-9c layout.
- `…/scratchpad/rerun/rec-ssb9c/` — the same fixture on the company layout (`{name}-boot` / `{name}-back`). This is the only recorded fix prompt on the company layout.
- `…/scratchpad/rerun/rec-ibatis/` — legacy-ibatis, spring_legacy, BLOCKED.

`legacy-external-parent` made no fix call (`llm=1`, the structure review only).

**Sizes.**
- The system prompt is 17,771 chars and is sent on every fix call.
- The user prompts are 8–30K chars. Journal: 11,949 input tokens for the first case.
- No truncation marker appears in any recorded prompt (budget 60,000).

**fix-policy.md is not "always included".** `pack_script` drops always-notes on purpose (`sbmig/fix/context.py:770-772`, asserted by `tests/fix/test_fix_context.py:110,151`). So none of its stale lines reach the LLM (see M8).

---

## H1 — "Dependency versions" tells the LLM to bump chain-managed Spring jars, or to answer `<no_script>`
**Evidence**
- Recorded prompts `rec-ssb9c/llm-0007.json` and `llm-0008.json` (API-removal cases `setUseSuffixPatternMatch` / `keySet`) carry:
  `org.springframework:spring-webmvc — current 7.0.8; same major (7.x) in the repository, newer than current: 7.0.9, 7.0.10`
  `org.springframework:spring-webmvc → verdict: no jakarta build is known on any line — stay on 7.x, say so in <analysis> and answer <no_script> if the case cannot be fixed without it`
- Count over all 2026-10-30 e2e journals (`"event": "library_hints"`): 22 such verdicts, all on `spring-web` / `spring-webmvc`. Those two are jakarta-native and managed by the parent chain.
- Cause: `_library_hints` (`sbmig/loop/fixloop.py:2839`) finds a library on the classpath for every compile error. It then appends the repository listing and the verdict (`sbmig/build/repo_versions.py:792-793`). v2.1d built that verdict for javax libraries.

**Why it hurts**
- The listing invites `set_dependency_version(...,"7.0.10")` on a dependency that already exists without a version. v2.1q strips versions only on *added* dependencies, so the result would be mixed Spring 7.0.8 / 7.0.10 jars.
- The verdict is a scripted refusal ("answer `<no_script>`"). In `fix.mode: edits` `<no_script>` is not even allowed.
- This is the same kind of refusal as the company loop "정책상 버전을 추정하여 추가할 수 없습니다".

**Fix (code: `_library_hints` / `_library_lines`, `_verdict_line`)**
1. For each library from `_case_libraries`: when the run's `VersionResolver` reports `managed_version(key, module)` or `boot_version(key, module)`, emit only the existing fact form plus a tail. No repository listing, no verdict: `new parent chain: manages g:a at <v> — keep it version-less; this error is an API change of that line: fix the code` (or the `target Boot BOM: manages …` form).
2. Call `_library_lines` only when an error of the case names `javax.` (message / raw / `class file for javax…` / `cannot access javax…`). That is v2.1d's real scope.
3. Replace the last branch of `_verdict_line` with: `f"{rv.key} → verdict: no jakarta build is known — keep {cm}.x and fix the code against it; if it cannot compile without one, name the class in <analysis>"`

**Effect on spring_legacy.** API-removal cases no longer carry a bump list or a refusal instruction, and mixed Spring versions can no longer enter the poms.

## H2 — The case header shows the AS-IS project as if it were current; the converted layout is never stated
**Evidence**
- `rec-ssb9c/llm-0002.json`: `project: case=spring_legacy java=1.8 spring=5.3.31 packaging=jar modules=1 libs=spring-security:5.7.11,…,javax-servlet:4.0.1,…,jdk-removed-apis javax-imports=javax.servlet | target: JDK 21, Spring Boot 4.1.0`
  - The tree has 3 poms.
  - Java is 21 and Spring is 7.0.8.
  - OpenRewrite already renamed the javax imports.
  - `jdk-removed-apis` is an advisory id, not a library.
- Across all 8 fix prompts: `Application` appears once (only in one note), `legacy-config` 0 times, the boot pom is never shown, and the company artifact rules appear 0 times. The boot module is named only inside a plan note (`layout split into ops-console-back, ops-console-boot, starter …`).
- `kind: compile, module(s): ops-console-comp` gives the artifactId, while every path says `ops-console-back/`.
- Code
  - The summary is built once from the analyze-time profile (`fixloop._summary` :3844 → `sbmig/fix/fixer.py profile_summary`).
  - `RestructureResult` already has the facts: `boot_module`, `comp_module`, `front_module`, `boot_packaging`, `generated_files`, `mandatory_added`, `bom_imports`, `parents_set` (`sbmig/core/interfaces.py:175-235`).
  - The orchestrator passes only `merge_conflicts` to the loop (`sbmig/loop/orchestrator.py:1395`).

**Fix**
1. `profile_summary`: label it and drop what is stale. Replace `parts` joining with `"as-is (before sbmig): " + " ".join(parts)`, remove the `javax-imports=` part, and keep advisory ids out of `libs=`.
2. Add a new `FixContext.layout_facts: list[str]`.
   - Fill it in the orchestrator after each restructure step, next to line 1395: `self.loop.layout_facts = layout_facts(res, cfg)`.
   - Pass it into `FixContext(...)` in `_ask_llm` (:2449).
   - Pack it as section **"Project after the restructure"**, order 0.5, not truncatable, about 800 chars.
   - Every value comes from `res` / `cfg.artifacts` / `cfg.layout`, never literal. For the ssb9c run it renders as:
```
converted by sbmig: spring_legacy (non-Boot Spring) → Spring Boot application, layout rule non-boot
boot module: ops-console-boot/ (artifactId ops-console-boot, packaging jar, parent <artifacts.boot_parent g:a>) — sbmig-generated: <res.generated_files>
back module: ops-console-back/ (artifactId ops-console-comp, parent <artifacts.comp_parent g:a>) — every as-is source
front module: <res.front_module or "none">
XML bean configs in use: <boot>/src/main/resources/legacy-config/*.xml (@ImportResource on the Application)
company artifacts: root parent <artifacts.parent g:a>; mandatory at the root pom: <g:a list>; optional in-house artifacts only where already used — never remove or re-version them
```
   - The `legacy-config` line follows `restructurer.py:2442-2476` (the copy in boot is the one imported). Confirm before wording it as "edit these, not the back originals".
3. Header: `module(s): ops-console-comp (ops-console-back/, back)` — map the artifactId to its dir and role from `layout_facts`.

**Effect.** The LLM stops treating Spring 5.3.31 / Java 1.8 as the current state. Boot-side fixes (properties, registrations, starters) go to the boot module, and XML edits hit the imported copy.

## H3 — Notes are picked by import lines and dropped first, so relevant notes lose to irrelevant ones
**Evidence**
- `rec-ssb9c/llm-0002.json` (`package org.springframework.security.access.vote does not exist`) carries `jakarta.md` (9.7K: Hibernate query API, Spring Data) and `spring-framework-7.md` (8K). Neither matches the error text. Their only hits are on imports: jakarta's `Filter` keyword matches `FilterInvocationSecurityMetadataSource`, and `org\.springframework\.(…context…)\.` matches an import line.
- Simulation with the real packer: a 1,200-line controller with 25 `CommonsMultipartResolver` errors.
  - `notes:Migration note: Raw servlet / JSP … (dropped)` and `notes:…Spring Framework 5 -> 6 -> 7 … (dropped)`.
  - `spring-framework-7.md` is the note that names `CommonsMultipartResolver`.
  - `jakarta.md` stays (imports only), and so do "Other error cases" (2.3K) and "Plan notes" (1.5K).
- Code
  - `match_text` includes `import_text` (`context.py:765-769`).
  - The score counts distinct keyword hits (`select_notes` :588).
  - `SCRIPT_TRUNCATE_ORDER` starts with `"notes"`, ahead of `plan_notes` / `other_errors` (:267; same in `EDITS_TRUNCATE_ORDER`).
  - Budget design: `SCRIPT_OPTIONAL_RESERVE` reserves 35% for optional sections, but notes are the first thing cut.

**Fix**
1. `select_notes(text, …, imports=…)`: score = 3 × hits in the error text (message, symbol, location, raw, artifact) + 1 × hits in imports / poms. Keep a note with zero error-text hits only when no note has one.
2. `knowledge/notes/index.yml` + front matter of `jakarta.md`: change `HttpServletRequest|HttpServletResponse|HttpSession|ServletContext|Filter` to `\b(HttpServletRequest|HttpServletResponse|HttpSession|ServletContext|Filter|FilterChain)\b`.
3. Truncation order:
   - `SCRIPT_TRUNCATE_ORDER = ("other_errors", "plan_notes", "notes", "previous_attempts", "kb_hints", "reference_files", "advisory_hints", "reference_notes", "library_hints", "related_files", "files", "pom", "base_result")`
   - `EDITS_TRUNCATE_ORDER`: the same swap.

**Effect.** About 18K chars (~4.5K tokens) less on cases like llm-0002. The note that names the error symbol survives on large company files.

## M1 — Rule 8's Jackson wording contradicts the recipes that ran
**Evidence**
- `script_system_v1.md:48-49` (and `edits_system_v1.md:42-43`) say: "`tools.jackson.*` only when the project is already on Boot 4's Jackson 3; otherwise keep `com.fasterxml.jackson.*`".
- Since v2.1o every spring_legacy project runs `UpgradeSpringFramework_7_0`, which runs `UpgradeJackson_2_3` (`knowledge/recipes/catalog.yml:164`). `spring-framework-7.md:65` says that recipe "moves the project's Jackson code to `tools.jackson.*`".
- The Boot 4.1 BOM manages both Jackson lines (local `spring-boot-dependencies-4.1.0.pom:82-83,3351-3360`).
- The LLM cannot tell whether the project "is already on Jackson 3", so it may undo recipe output.

**Replacement (both prompts)** "Jackson: follow the module, not habit — the recipes already moved most Jackson code to Jackson 3 (`tools.jackson.*`) and the Boot starters bring Jackson 3; never rename `tools.jackson.*` back. A `com.fasterxml.jackson.databind…` error in a module whose pom has no `com.fasterxml.jackson.core:jackson-databind` → port that code to `tools.jackson.*`; with that dependency present, keep it. `com.fasterxml.jackson.annotation.*` never changes."

## M2 — The system prompt hard-codes the target, and its recipe sentence is wrong for spring_legacy
**Evidence**
- `script_system_v1.md:1-4` / `edits_system_v1.md:1-4`: "**JDK 21 + Jakarta EE 11 + Spring Boot 4.1** (Spring Framework 7, Spring Security 7, Hibernate 7). OpenRewrite ran once (Java 21, javax → jakarta and, for Spring Boot projects, the Spring Boot 4.0 / 4.1 recipes)".
  - This misses `UpgradeSpringFramework_7_0` (v2.1o) and the Security chain (v2.1l). Plan notes list them correctly.
  - It also contradicts A20 / the user rule "nothing hard-coded".
- `verify_system_v1.md:1-2` has the same target sentence.

**Fix.** Pass `target=target_text(cfg)` into each prompt loader:
- `load_script_prompts` (`fixer.py:172`): it already renders Jinja (StrictUndefined), so add `target=` to the render call.
- `load_edits_prompts` (`fixer.py:194`): it reads raw text and is `lru_cache`d. Render it the same way, keyed by target.
- The verify prompt: add a `{{ target }}` `.replace` next to the existing `{{ language }}` one (`fixer.py:674`).

**Replacement for lines 1-5** "…migrated automatically to **{{ target }}** and the stack its Spring Boot BOM manages. OpenRewrite ran once (the recipes are listed under "Plan notes"); the company restructure then set the parents and the module layout — a non-Boot project became a Spring Boot application (see "Project after the restructure"). Now `mvn clean package -Dmaven.test.skip=true` fails. Tests are not compiled in this migration: never edit `src/test/` and never try to make tests pass."

Rule 8: change "relocated Spring Boot 4 packages" to "the target Boot line's relocated packages".

## M3 — Rule 14 is long, partly stale, and explains facts that almost never appear
**Evidence**
- 2,251 bytes (`script_system_v1.md:64-84`).
- "(the as-is build's version, else the newest release the repository has)" is stale. v2.1s order is chain-managed → target Boot BOM → as-is → repository.
- The `target Boot BOM: manages …` lines (`_version_facts` :3638) are never explained.
- "If you can look releases up yourself, do so and name the source" invites invented lookups; the company gateway has no tools.
- In all 2026-10-30 e2e `library_hints` the facts it explains appeared as: `as-is build:` 2 times, `new parent chain:` 0 times, `parent chain property:` 0 times. `_version_facts` / `_chain_property_facts` only see `e.artifact`, which a compile error never has.

**Replacement (script prompt; the edits prompt is the same without the `<no_script>` words)** "14. Dependency versions (메이저를 올리지 않는다). Never change a dependency's MAJOR version and never swap a library for another on your own (porting what rule 10 names is not "on your own"). The "Dependency versions" facts are authoritative: `new parent chain: manages g:a at v` / `target Boot BOM: manages g:a at v` → declare it without a version; `parent chain property: name=value` → write `${name}`, never the literal; `as-is build: g:a v` = what the old build used; a `verdict:` line decides a javax library's version (`approved` = a person allowed it). Without a fact write `"version": null`: sbmig fills it before the build (none when managed, else the as-is version, else the newest release in the repository). Never refuse, answer `<no_script>` or leave an error unfixed because a version is unknown. A library still compiled against `javax.*`: the newest jakarta release of the SAME major; only when that major has none, the newest release of a higher major that has one — then port what that major changes (configuration files, filter / servlet / listener registrations, JSP tags, renamed packages), mark what you could not port with `MIGRATION-TODO`, and add `<verify>`: one line per runtime check, naming the file / screen / job. `<verify>` is required whenever a major changed or a library was replaced (also under rule 10); otherwise leave it out."

That is about 1,350 chars. Also align the `"version": null` cell of the pom row in both edit tables: `"version": null` = sbmig decides (no version when the parent chain or the target Boot BOM manages it, else it fills one).

## M4 — No managed-artifact facts for "package … does not exist" (the case where the LLM must ADD a dependency)
**Evidence.** `rec-ssb9c/llm-0002.json` and `llm-0003.json`:
- `package org.springframework.security.access.vote does not exist` and `…batch.item.support does not exist`.
- No "Dependency versions" section, because `e.artifact` is None.
- The LLM must guess the artifact (spring-security-access) from notes.

**Fix (new `_package_facts(c)` in `fixloop.py`, appended in `_library_hints`)**
- For each `package_does_not_exist|<pkg>` / `location: package <pkg>`, find the longest managed groupId that is a prefix of `<pkg>`. The source is the `VersionResolver` chain capture, else the Boot BOM facts.
- List up to 25 artifactIds: `target Boot BOM manages in group org.springframework.security: spring-security-access, spring-security-config, … — add the one that holds <pkg>, version-less`
- No coordinates hard-coded.

**Effect.** Fewer wrong-artifact attempts. It also gives rule 14's "managed → no version" something to act on.

## M5 — The last line of the output format invites a free refusal
**Evidence.** `script_system_v1.md:196` / `edits_system_v1.md:127`: "If nothing can fix this case safely, answer with only `<analysis>` explaining why". Under the v2 policy ("no block policy, ever") this costs a try and a repeated_failure without naming a blocker.

**Replacement** "Only when the fix needs something outside the project — an artifact the repository does not have, a decision only the owning team can make — answer with only `<analysis>` naming that blocker (coordinates, class, file). An unknown version, starter or package location is never such a blocker: use `"version": null` or `<need_grep>`."

## M6 — Rule 10 does not cover a note's `fix-loop:` section, so rule 14 forbids the port the note asks for
**Evidence**
- legacy-ibatis rerun, `sbmig.log`: only case `70cf5d17c7` got "1 advisory hint(s)". The same-cause cases `4b636ce784` (`getSqlMapClientTemplate`) and `f67d8b305c` (`SqlMapClientDaoSupport`) got 0.
- The prompt `rec-ibatis/llm-0003.json` has the ibatis2 note ("port the DAO to MyBatis 3") but no "Library migration guidance" section.
- Rule 3/14 say "never replace a library on your own". Result: 3 cases × 3 LLM calls, then `blocked (repeated_failure)` ×3.

**Replacement for rule 10 (both prompts)** "10. When a "Library migration guidance" section, or a Migration note's `fix-loop:` section, names the replacement of a library the errors use, that library was removed on purpose: port the code to the named replacement (the smallest port that compiles, public signatures kept) instead of re-adding it — this is not "replacing a library on your own" (rule 14)."

Also worth checking, outside this dimension: `sbmig/advisory/hints.py hints_for` should also match by the erroring files' imports (not checked).

## M7 — Stale advisory text reaches the LLM as authoritative guidance
**Evidence.** `rec-ssb9c/llm-0005.json` "Library migration guidance" says "sun.misc.BASE64Encoder/Decoder (now the generated sbmig.compat shims …)". The source is `knowledge/libraries/advisories.yml:259-262,266`. Handler `jdk-base64-shim` was deleted in v2 step 4: `sbmig/compat` does not exist, and grep finds the name only in advisories.yml. The case itself is `cannot find symbol BASE64Encoder`, so there is no shim.

**Fix**
- `summary`: delete "— except sun.misc.BASE64Encoder / BASE64Decoder and com.sun.org.apache.xerces…Base64, which the handler jdk-base64-shim (round 10) points at generated drop-in classes (sbmig.compat.*, …)".
- `followup`: the opening sentence becomes "Review the ported JDK API calls: sun.misc.BASE64Encoder/Decoder → java.util.Base64 (getMimeEncoder() / getMimeDecoder() when callers depend on the 76-character line breaks);".

## M8 — `fix-policy.md` is dead and stale, and the docs say it is always sent
**Evidence**
- Excluded at `context.py:770-772` (since `748fd0f`).
- Docs still claim it is always sent: `docs/migration-agent/README.md:1152,1178,1588` ("fix-policy 항상"), handbook 04/05 (update only on request).
- Stale lines, if any consumer such as a company `fix.plugin` reads it (unverified):
  - :10 "re-runs Maven (`compile` / `test-compile` / `package`)" → "re-runs `mvn clean package -Dmaven.test.skip=true`"
  - :21 "Fixing tests so they compile … Tests are never deleted." → delete. Tests are not compiled; never edit `src/test/`.
  - :25 "`-Dmaven.test.skip`" in Forbidden → delete (sbmig itself runs with it).
  - :27-29 "Adding new third-party libraries unless … a note names the replacement" → "Adding a library: only one the errors need, version-less when the parent chain / target Boot BOM manages it, else `"version": null` (rule 14)".
  - :47 "Confidence: 0.9+ …" → delete (v2 has no confidence field).
  - :9 "(a Python fix script)" → "(`<edits>` plus a reusable `def fix(p)` script)".

**Recommendation.** Since the system prompts carry the policy: delete the note, its `index.yml` entry and the matching `tests/fix/test_fix_notes.py` expectations, and fix the README lines. Otherwise apply the line replacements above.

## M9 — `servlet-to-boot.md` contradicts the 9c layout and v2.1q
**Evidence**
- :55-58 "only works with WAR packaging or with JSPs moved to `src/main/resources/META-INF/resources/WEB-INF/...` in an executable jar … do not move JSPs". CLAUDE.md says JSPs cannot be served from an executable jar, and sbmig already moved them to `{name}-front/src/main/resources/META-INF/resources/WEB-INF/jsp`.
- :62 "`war` → executable jar … in the company layout". With JSPs the boot module is an executable WAR.
- :63 "`spring-boot-starter-tomcat-runtime` as provided". The restructure writes `spring-boot-starter-tomcat` + `tomcat-embed-jasper` provided (`restructurer.py:3006`, v2.1q e2e).

**Replacement for "## JSP" first bullet and "## Packaging" first two bullets** "- sbmig already placed JSPs in the front module under `src/main/resources/META-INF/resources/WEB-INF/jsp` and made the boot module an executable WAR (`java -jar`; the same file deploys to an external Tomcat) with `spring-boot-starter-tomcat` and `tomcat-embed-jasper` as `provided` — do not change packaging, move JSPs or rename these dependencies.
- Without JSPs the boot module is an executable jar; remove `maven-war-plugin` configuration only from jar modules."

The note is selected for spring_legacy (`cases: [servlet_tomcat, spring_legacy]`), so this text reaches the company case.

## L1 — Plan notes: 1.5–2.1K chars per prompt, about half of it noise
**Evidence.** `rec-ssb9c/llm-0002.json` "Plan notes". The lines `case spring_legacy (confidence 0.75): …`, `optional artifacts: bumped …`, `compile: no build before …`, `review: …`, `advisory: … → guide` and the first `transform:` line (repeated by the second) change no answer.

**Fix (`_ask_llm`)**
- Pass `[n for n in self.plan_notes if n.startswith(("transform", "restructure", "warning", "msa", "followup:"))]`, minus the `→ jakarta set …` duplicate.
- Rename the section "What sbmig already did (plan)".
- Saves about 700 chars per call.

## L2 — Duplicate paragraphs in the script system prompt (about 1.4K of 17.8K chars, on every call)
- Lines 180-182 (352 B) restate rule 14(b) on `<verify>`. Delete.
- Lines 184-188 (513 B) restate intro item 4 and the "Describing every change" `<changes>` bullet. Keep only "The script must define a module-level `def fix(p)`; it may define helper functions and constants (plain Python inside `<script>`)." and delete the rest.
- "## Script or no script" (544 B) repeats intro item 4 and rule 12. Fold its two bullets into rule 12 as one sentence: "`<no_script>` only when the fix depends on this project's own code (business logic, a one-off module structure)."
- Keep the `<verify>` example line in the output format: it is the only example of the tag.
- The helper-API fallback (lines 133-150) is never rendered because `helper_api` is passed. It costs nothing; leave it.

## L3 — Small wording conflicts
- Rule 3 (script) says "only through `p.pom(path)`", but `p.pom` has no plugin-configuration call. Use: "through `p.pom(path)` when an editor call can do it (plugin configuration: `p.replace` on the exact XML)". This matches the Edits bullet at :102.
- `verify_system_v1.md:3` and `fixer.py:676` say "The build passes". The guide runs right after the apply, before the rebuild (`fixloop.py:2714-2715`). Use: "The change was applied; the rebuild decides whether it is kept; the runtime is unverified."
- `junit4-to-5.md` is still selectable (all cases) although tests are never compiled (user 2026-10-22). Set `cases: []` plus a filter, or drop it from `index.yml`.
- `boot3-to-4.md` excludes spring_legacy, but after the conversion the project is a Boot 4 app. Add `spring_legacy` and `servlet_tomcat` to its `cases` (it is keyword-gated). Its row 51 (`spring-boot-starter-tomcat` → `-tomcat-runtime`) disagrees with what the restructure writes. The Boot 4.1 BOM has both; which is right was not checked.
- `summarize_script_attempt` (`context.py:316`) shows only "N file(s) changed" + `analysis[:300]`, which gives rule 13 ("do something different") nothing to go on. Add the attempt's first 5 `changes` desc lines.
- `other_errors` is cut with `[:4000]` mid-line and without a marker (`fixloop.py:2425`). Cut at a line boundary and add `… (+N more cases)`.

## Not found / fine
- The file-lock loop is closed in code (v2.1r: BLOCKED `file_lock` before any LLM call). No prompt rule is needed.
- `p.errors` is stressed in rule 1, in the header ("every one of them is in p.errors") and in the user preamble. No rule invites ignoring it.
- Drift between the script and edits prompts is intentional and small (rules 1, 2, 4-7, 12). Rules 8 and 14 are copied verbatim, so every change above must be made in both files. A shared Jinja include would stop future drift.
