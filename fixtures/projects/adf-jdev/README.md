# adf-jdev — an Oracle ADF (JDeveloper) workspace behind a packaging pom: recognised, sized, stopped (v2.1z-d, 2026-11-02)

Modelled on the company's first batch (2026-11-02): about ten of the fifty projects are JDeveloper 12c / ADF 11 applications.
The upload root holds `APP/`, `DEV/`, `STATIC/`, `DB/` and no pom; `APP/pom.xml` aggregates `./WEB`, whose pom only
**packages** what the IDE produced (`${DIR_S10}` properties pointing at `../../DEV/s10`, `maven-war-plugin`
`webappDirectory`); the application itself is the JDeveloper project `DEV/s10`:

* `s10.jpr` (technology scope `ADF_FACES`, `ADF_PAGE_FLOW`, `ADFbc`, `ADFm`);
* ADF BC in `src/model`: `Emp.xml` (Entity, `DBObjectName="EMP"`), `EmpView.xml` (ViewObject with a `SQLQuery`),
  `HRModule.xml` (AppModule), `EmpImpl.java` (`oracle.jbo.server.EntityImpl`);
* ADF Model in `adfmsrc`: `adfm.xml`, `DataBindings.cpx`, `view/mainPageDef.xml`; `view/EmpBean.java`
  (`oracle.adf.model.BindingContext`), `view/Formats.java` (plain java, no ADF import);
* ADF Faces / controller in `public_html`: `main.jspx`, `frag.jsff`, `WEB-INF/adfc-config.xml` (two views), `WEB-INF/web.xml`;
* `STATIC/css/app.css`, `DB/schema.sql` (start-up SQL, referenced by no pom).

Business decision (2026-11-02): **an ADF project is not converted** — the run ends with
**"ADF 프로젝트라 변환이 불가능합니다"**; nothing else is planned. The decision must be strict (no false positives): four
independent evidence families — A `bc4j` (BC4J XML in the `http://xmlns.oracle.com/bc4j` namespace), B `java` (`oracle.jbo` /
`oracle.adf` / `oracle.binding` imports), C `model` (`adfm.xml`, `DataBindings.cpx`, `*PageDef.xml`), D `faces` (`jspx` / `jsff`
with the `http://xmlns.oracle.com/adf/faces` namespace, `adfc-config.xml`, bounded `*-flow.xml`) — and a project is rejected
only when **two or more** are present; one family is a trace (`adf-traces`, the run goes on), a `.jpr` name or `adf-config.xml`
alone is nothing. This fixture carries all four families, so it is rejected. Expected (`intake.adf_policy: stop`, the default):

* intake generates the root aggregator (`adf-web-aggregator`, modules `APP`) as for any rootless tree;
* analyze finds the four families and sizes the workspace: 1 entity (1 table `EMP`), 1 view object, 1 application module,
  1 page definition, 1 task flow (2 views), 2 ADF pages, 2 of 3 java files on ADF — `ProjectProfile.adf`;
* the run **stops there**: flags `adf-project` + `aborted`, journal `adf_project` (families, evidence paths, counts), Handover
  first line `ADF project (Oracle JDeveloper / ADF): conversion is not possible — ADF 프로젝트라 변환이 불가능합니다 (evidence: bc4j,
  java, model, faces; 1 entities / 1 tables, …)` (Korean report: `ADF 프로젝트라 변환이 불가능합니다 (근거: …; 엔티티 1개 / …)`),
  report section `## ADF sizing` / `## ADF 규모` with the evidence per family ("판정 근거 / evidence"), exit **3**,
  `SBMIG-RESULT … attn=aborted,adf-project`; no OpenRewrite, no Maven build, no LLM call (`max_llm_calls: 0`), nothing moved
  (no `adf-web-back`).

With `intake.adf_policy: continue` the flag and the sizing stay and the run goes on (report only, not recommended); the
hollow-reactor guard then also fires, since the reactor compiles nothing.

## Build as-is

```bash
cd APP && JAVA_HOME=$(/usr/libexec/java_home -v 1.8) mvn -q -DskipTests package   # the WAR packages the empty shell (nothing to compile)
```
