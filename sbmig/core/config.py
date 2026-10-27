"""Configuration schema + loader.

Layering (later wins):  config/default.yml  →  --config file(s)  →  environment overrides (SBMIG_* / ${VAR} expansion).
Company-specific coordinates live in a separate artifacts yml so the code base stays generic.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationInfo, field_validator

log = logging.getLogger("sbmig.config")

from .models import GAV

PACKAGE_ROOT = Path(__file__).resolve().parents[2]          # repo root when running from source
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "config" / "default.yml"


class TargetConfig(BaseModel):
    java_version: str = "21"
    spring_boot_version: str = "4.1.0"
    spring_framework_version: str = "7.0.x"


class ArtifactSpec(BaseModel):
    group_id: str
    artifact_id: str
    version: Optional[str] = None            # None → version managed by the parent
    scope: Optional[str] = None
    type: Optional[str] = None
    # optional artifacts: how to find the as-is usage (defaults to same group/artifact)
    aliases: list[str] = Field(default_factory=list)   # old "g:a" keys that should be treated as this artifact
    property_name: Optional[str] = None       # if the version is conventionally kept in a property

    def gav(self) -> GAV:
        return GAV(group_id=self.group_id, artifact_id=self.artifact_id, version=self.version,
                   scope=self.scope, type=self.type)

    @property
    def key(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"


class ArtifactsConfig(BaseModel):
    parent: ArtifactSpec                      # the root pom's parent (company: `base-parent`)
    # round 9b — the company layout has role parents: `module/boot-jar/pom.xml` inherits the in-house `boot-jar`
    # parent and `module/comp-jar/pom.xml` the in-house `comp-jar` parent (all in-house artifacts share one line,
    # e.g. 4.0.0); the root pom (base-parent) is then an aggregator the modules do NOT inherit from. None = the
    # modules inherit the root pom (pre-9b behaviour). An as-is module whose parent has the same groupId:artifactId
    # as a role parent (an older migration on 1.0.0) is always moved to the configured version.
    boot_parent: Optional[ArtifactSpec] = None
    comp_parent: Optional[ArtifactSpec] = None
    parent_manages_spring_boot: bool = True   # if False we import spring-boot-dependencies BOM ourselves
    parent_manages_java_version: bool = True  # if False we set maven.compiler.release
    mandatory: list[ArtifactSpec] = Field(default_factory=list)   # exactly 2 in production config
    optional: list[ArtifactSpec] = Field(default_factory=list)    # 8 in production config
    remove_when_present: list[str] = Field(default_factory=list)  # "g:a" keys to drop (e.g. old in-house parents' BOMs)
    # round 9c: where the mandatory dependencies are declared — `root`: every root pom (the project root; in MSA each
    # service root and the top aggregator), the company rule; `modules`: every role module pom (pre-9c with role parents)
    mandatory_target: Literal["root", "modules"] = "root"
    # floor: only raise an optional artifact to its configured version (never downgrade a newer as-is/recipe version)
    # pin  : always set exactly the configured version
    optional_version_policy: Literal["floor", "pin"] = "floor"

    @property
    def role_parents(self) -> bool:
        """True when the modules inherit their own in-house parents (boot_parent / comp_parent both set)."""
        return self.boot_parent is not None and self.comp_parent is not None

    def parent_specs(self) -> list["ArtifactSpec"]:
        """Every configured in-house parent: root parent, then boot_parent / comp_parent when set."""
        return [s for s in (self.parent, self.boot_parent, self.comp_parent) if s is not None]

    def parent_keys(self) -> list[str]:
        """`g:a` keys (incl. aliases) that mean "a configured in-house parent" — an unresolvable one aborts the run."""
        out: list[str] = []
        for s in self.parent_specs():
            for k in (s.key, *s.aliases):
                if k not in out:
                    out.append(k)
        return out


class LayoutConfig(BaseModel):
    """Round 9c — the company layout (stated 2026-10-21): modules directly under the root named `{name}-boot`
    (Boot configuration only, parent `boot_parent`), `{name}-back` (every source incl. controllers, parent
    `comp_parent`) and — only when the as-is project has web resources — `{name}-front` (static files under
    `front_static_dir`, JSPs under `front_jsp_dir` per Servlet 3, parent `comp_parent`); `{name}` = the root pom's
    artifactId (`project_name` overrides). Spring Boot as-is projects (incl. MSA) keep their directories
    (`boot_as_is: in-place`): only the poms change — root → `artifacts.parent`, modules named like
    `boot_module_pattern` whose pom confirms a boot app → `boot_parent`, every other module → `comp_parent`; a
    single-module Boot app is wrapped into `{name}-boot` under an aggregator root. The pre-9c layout
    (`module/boot-jar` + `module/comp-jar`, Boot projects merged like the others) is `module_dir: module`,
    `boot_module: boot-jar`, `comp_module: comp-jar`, `front_module: null`, `boot_as_is: layout`
    (config/artifacts.local-e2e.yml keeps it for the older fixtures)."""
    module_dir: str = ""                                # "" = modules directly under the root
    boot_module: str = "{name}-boot"
    comp_module: str = "{name}-back"
    front_module: Optional[str] = "{name}-front"       # None = no front module (web resources stay in the boot module)
    project_name: Optional[str] = None                  # `{name}`; default = the root pom's artifactId
    front_static_dir: str = "src/main/resources/static"                          # HTML / css / js / images
    front_jsp_dir: str = "src/main/resources/META-INF/resources/WEB-INF/jsp"     # JSPs (Servlet 3 jar resources)
    boot_as_is: Literal["in-place", "layout"] = "in-place"   # Spring Boot as-is: keep directories (poms only) | split
    boot_module_pattern: str = "*-boot"                 # module dirs that are boot apps in Boot as-is / MSA (pom confirms)
    jsp_boot_packaging: Literal["war", "jar"] = "war"   # JSPs present → the boot module is a WAR for external Tomcat
    boot_artifact_suffix: str = "-boot"
    comp_artifact_suffix: str = "-back"                 # artifactId = <root artifactId> + suffix (front: "-front")
    split_policy: Literal["minimal", "web-layer"] = "minimal"
    multi_module_policy: Literal["wrap", "merge"] = "merge"
    # 2026-10-23: no pom.xml at the project root but sibling poms that depend on each other (e.g. APP/BATCH, APP/WEB,
    # APP/EAR built one by one and packaged by the EAR) → intake writes a root aggregator pom listing them, so they
    # build in one reactor and the merge policy applies (sbmig.analyze.aggregator)
    generate_aggregator: bool = True
    # merge: a path present in more than one module → first module in <modules> order wins, the other copies are
    # saved under <run dir>/merge-conflicts/ and listed in the Handover ("first-wins"); "fail" = restructure error
    merge_collision: Literal["first-wins", "fail"] = "first-wins"
    # round 9: after the in-house parent is set, drop explicit <version>s the new parent chain manages
    # (`mvn help:effective-pom` after the pom edits): gte = only when the managed version is ≥ the explicit one
    # (a deliberately newer pin stays), any = always, off = keep every explicit version. Mandatory / optional /
    # parent artifacts from `artifacts` and <scope>system</scope> dependencies are never touched.
    drop_managed_versions: Literal["gte", "any", "off"] = "gte"
    application_class_name: str = "Application"
    boot_resources_glob: list[str] = Field(default_factory=lambda: [
        "application*.yml", "application*.yaml", "application*.properties",
        "bootstrap*.yml", "bootstrap*.properties", "logback*.xml", "log4j2*.xml", "banner.txt",
    ])
    starter_by_case: dict[str, str] = Field(default_factory=lambda: {
        "pure_java": "spring-boot-starter",
        "servlet_tomcat": "spring-boot-starter-webmvc",
        "spring_legacy": "spring-boot-starter-webmvc",
        "spring_boot": "auto",          # keep whatever starters the as-is project already has
    })
    add_servlet_component_scan: bool = True
    # 2026-10-23: web.xml servlets / filters / listeners / error pages / session timeout → a generated
    # `WebXmlRegistrations` @Configuration in the boot module (`generate`), or follow-ups only (`followup`)
    web_xml_registrations: Literal["generate", "followup"] = "generate"
    import_xml_bean_configs: bool = True   # @ImportResource for detected <beans> xml files
    msa_parent_policy: Literal["per-service-root", "top-aggregator"] = "per-service-root"

    def module_name(self, which: Literal["boot", "comp", "front"], name: str) -> Optional[str]:
        """The module directory name for a role (`{name}` expanded); None when the role has no module (front off)."""
        tpl = {"boot": self.boot_module, "comp": self.comp_module, "front": self.front_module}[which]
        return tpl.replace("{name}", name) if tpl else None

    def module_path(self, which: Literal["boot", "comp", "front"], name: str) -> Optional[str]:
        """Root-relative posix path of a role module (`module_dir` + name), e.g. `shop-boot` or `module/boot-jar`."""
        n = self.module_name(which, name)
        if n is None:
            return None
        d = self.module_dir.strip("/")
        return f"{d}/{n}" if d else n


class RecipeSetConfig(BaseModel):
    # mirrors `openrewrite.recipes` of config/default.yml (a Config() built without the yml plans the same recipes).
    # v2 (flow-v2, business decision 2026-10-26): only Java 21, javax → jakarta and — for Spring Boot projects — the
    # Boot 4 recipes run, all in ONE OpenRewrite run before the restructure ("레시피가 무엇을 바꿀지 모르니 넣지
    # 않는다"); Spring Framework 7 / Security / JUnit 5 / project / library / KB recipes are off, everything else is
    # fixed by LLM scripts. The empty sets stay so a company config can opt back in.
    jdk21: list[str] = Field(default_factory=lambda: ["org.openrewrite.java.migrate.UpgradeToJava21"])
    jakarta: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta",
    ])
    spring_boot_4: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0",
        "org.openrewrite.java.spring.boot4.SpringBootProperties_4_1",
    ])
    spring_framework_7: list[str] = Field(default_factory=list)
    spring_security_7: list[str] = Field(default_factory=list)
    junit5: list[str] = Field(default_factory=list)
    extra: dict[str, list[str]] = Field(default_factory=dict)


class OpenRewriteConfig(BaseModel):
    plugin_version: str = "6.46.1"
    recipe_bom_version: str = "3.37.0"   # 3.38.0 references unpublished artifacts (2026-10)
    recipe_artifacts: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.recipe:rewrite-migrate-java",
        "org.openrewrite.recipe:rewrite-spring",
        "org.openrewrite.recipe:rewrite-testing-frameworks",
        "org.openrewrite.recipe:rewrite-static-analysis",
    ])
    recipe_artifact_versions: dict[str, str] = Field(default_factory=dict)   # override per artifact; else BOM-managed
    recipes: RecipeSetConfig = Field(default_factory=RecipeSetConfig)
    timeout_minutes: int = 40
    jvm_args: list[str] = Field(default_factory=lambda: ["-Xmx4g"])
    oom_retry_jvm_args: list[str] = Field(default_factory=lambda: ["-Xmx8g"])   # one retry after OutOfMemoryError
    # round 9: the recipe run failed (or left literal `x.y.x` versions) because a download failed (proxy 429/5xx,
    # reset, timeout) → wait and retry the run; 0 = no retry
    download_retry: int = 1
    download_retry_wait_s: int = 30
    # round 9c: source globs the plugin must not parse (`-Drewrite.exclusions`). OpenRewrite's XML parser also accepts
    # .jsp/.jspx/.html/.htm; JSP scriptlets and `<` inside attribute strings only produce ANTLR "token recognition
    # error" noise (the file is skipped) and no recipe of ours touches them
    exclusions: list[str] = Field(default_factory=lambda: ["**/*.jsp", "**/*.jspx", "**/*.html", "**/*.htm"])
    fail_on_dry_run_results: bool = False
    plugin_goal: str = "run"                            # run | dryRun


class BuildConfig(BaseModel):
    mvn: str = "mvn"
    settings_file: Optional[str] = None
    local_repo: Optional[str] = None
    java_home: Optional[str] = None                     # JDK 21 home; None → inherit
    offline: bool = False
    extra_args: list[str] = Field(default_factory=lambda: ["-B", "-ntp", "-Dstyle.color=never"])
    skip_tests: bool = True                             # -DskipTests (tests still compile)
    # -Dmaven.test.skip=true on EVERY build: test sources are neither compiled nor run, so a test-source error can
    # never block the migration (the company's decision, 2026-10-22: the fleet does not fix tests). The report flags
    # `tests-not-compiled` and lists the follow-up; the L7 / L7b fallbacks become moot.
    skip_test_compile: bool = True                      # v2 default (the company decision of 2026-10-22)
    allow_skip_test_compile_fallback: bool = True       # -Dmaven.test.skip=true as last resort (flagged in report)
    allow_skip_tests_fallback: bool = True              # -DskipTests when only runtime test failures remain
                                                        # (flag `tests-skipped`, failing tests listed as follow-ups)
    timeout_minutes: int = 30
    env: dict[str, str] = Field(default_factory=dict)
    # legacy source encodings (JDK 18+ defaults to UTF-8 → "unmappable character" on MS949/EUC-KR sources): the
    # detection candidates the structure review and the script helper use
    encoding_candidates: list[str] = Field(default_factory=lambda: ["utf-8", "cp949", "euc-kr", "shift_jis", "latin-1"])
    # <repositories> / <pluginRepositories> declared in the project poms (old Nexus URLs, Central, Spring milestones)
    # are left as they are; route them through a `settings_file` mirror (mirrorOf external:*) — the plan notes say
    # whether the mirror covers them
    # package prefixes treated as *library* code in error signatures (project-owned FQCNs are masked as '*').
    # In-house library namespaces (parent/mandatory/optional group ids) are added automatically.
    external_package_prefixes: list[str] = Field(default_factory=list)
    # round 10: the preflight aborts (flag `jdk-not-21`) when `mvn -v` reports another Java major (0 = no check)
    require_java_major: int = 21
    # "… was cached in the local repository, resolution will not be reattempted …" (a Nexus 404 remembered for a day)
    # → the same build once more with -U before the error is triaged
    retry_negative_cache: bool = True


class PhaseLoopConfig(BaseModel):
    goal: str = "compile"
    max_iterations: int = 5
    enabled: bool = True


class LoopConfig(BaseModel):
    # v2: no intermediate builds — the jdk21 / spring / restructure loops are off; the first build is the final
    # `mvn clean package` after transform + restructure + review, and its fix loop (`verify`) does all the fixing
    phases: dict[str, PhaseLoopConfig] = Field(default_factory=lambda: {
        "jdk21": PhaseLoopConfig(goal="compile", max_iterations=3, enabled=False),
        "spring": PhaseLoopConfig(goal="test-compile", max_iterations=6, enabled=False),
        "restructure": PhaseLoopConfig(goal="package", max_iterations=6, enabled=False),
        "verify": PhaseLoopConfig(goal="package", max_iterations=50),   # v2.1: "라운드(빌드) 50회"
    })
    max_llm_calls: int = 0                   # 0 = no limit on LLM calls (v2.1); > 0 = a hard cap per project
    max_total_minutes: int = 240
    stall_limit: int = 2                     # identical error multiset N times → escalate strategy
    max_clusters_per_iteration: int = 8
    revert_on_regression: bool = True
    regression_ratio: float = 1.5            # errors_after > errors_before * ratio → regression
    always_final_verify: bool = True         # `mvn clean package` at the end no matter what
    engine: Literal["graph", "inline"] = "graph"   # graph: LangGraph StateGraph drives the phases (company
                                             # convention, sbmig.graph); inline: the original in-process loop
    oscillation_window: int = 4              # error-set history compared for A↔B toggling (≥ stall detection window)
    # how many LLM attempts a cluster of this BlockReason may get before it is blocked (0 = block immediately).
    # v2: no deterministic handler swaps a javax library, bumps a missing version or a JDK-21-incompatible plugin any
    # more, so those get LLM script attempts (a stored script is reused regardless); an unresolvable in-house parent
    # stays 0 (settings / Nexus — no code change fixes it)
    blocked_policy: dict[str, int] = Field(default_factory=lambda: {
        "external_javax_api": 2, "external_api_incompatible": 2, "artifact_not_found": 2, "parent_not_found": 0,
        "plugin_incompatible": 2, "unsupported_source": 0, "encoding": 0,
    })
    structural_regression_kinds: list[str] = Field(default_factory=lambda: [
        "pom_invalid", "parent_resolution", "plugin_resolution", "dependency_resolution"])
                                             # appearing after a pom edit ⇒ revert regardless of count (round 10: a
                                             # made-up dependency an LLM added masked every compile error)


BUILTIN_LLM_PROVIDERS = ("company", "openai_compatible", "anthropic", "fake")


class ModelPrice(BaseModel):
    """`llm.pricing.<model>`: list price per 1M tokens (currency = llm.pricing_currency)."""
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: Optional[float] = None   # informational: sbmig cannot see cache hits in the gateway reply


class LLMConfig(BaseModel):
    """`provider` is one of BUILTIN_LLM_PROVIDERS, `plugin` (→ `plugin` = "pkg.module:factory"), or the name of an
    adapter registered under the `sbmig.llm_adapters` entry-point group (in-house gateway / agent platform)."""
    provider: str = "openai_compatible"
    plugin: Optional[str] = None             # "pkg.module:attr" — attr is an LLMAdapterLike/LLMClientLike class or a
                                             # factory `fn(options: dict, cfg: Config) -> adapter | client`
    plugin_options: dict[str, Any] = Field(default_factory=dict)   # passed verbatim to the plugin factory
    base_url: str = "${LLM_BASE_URL}"
    api_key_env: str = "LLM_API_KEY"
    model: str = "${LLM_MODEL}"
    escalation_model: Optional[str] = None   # stronger model used after stall
    recipe_model: Optional[str] = None       # cheaper model for recipe selection advice + KB recipe candidates (None → model)

    @field_validator("model", "escalation_model", "recipe_model", mode="before")
    @classmethod
    def _clean_model_name(cls, v: Any, info: ValidationInfo) -> Any:
        """A model name is one token: an inline `# comment` tail and surrounding whitespace are dropped, and a
        value that is only a comment (`LLM_RECIPE_MODEL= # cheaper model`, `set X=# ...` on Windows) is unset.
        Without this the gateway answers `HTTP 400 … "# cheaper model" is an unsupported model` mid-run."""
        if not isinstance(v, str):
            return v
        cleaned = v.split("#", 1)[0].strip() if not v.lstrip().startswith("${") else v.strip()
        if cleaned != v.strip():
            log.warning("llm.%s=%r is not a plain model name: using %r", info.field_name, v, cleaned or None)
        if not cleaned:
            return None if info.field_name != "model" else ""
        return cleaned
    temperature: float = 0.0
    max_output_tokens: int = 8000
    max_context_chars: int = 60000           # budget for packed context per request
    timeout_seconds: int = 180
    max_retries: int = 3
    # round 9 (L5): the second consecutive transport/config failure pauses the loop once per run for this long and
    # retries; only a second streak marks the LLM unavailable for the rest of the run. 0 = no cool-down (old behaviour)
    cooldown_seconds: int = 60
    max_context_rounds: int = 2              # how many times the LLM may ask for more files per cluster
    extra_headers: dict[str, str] = Field(default_factory=dict)
    prompt_version: str = "v1"
    record_dir: Optional[str] = None         # record requests/responses for replay tests
    # provider `company` (default): requests.post(LLM_BASE_URL, headers={Authorization: Bearer LLM_API_KEY},
    # json={"model", "messages", **payload_extra}, timeout=timeout_seconds) — see sbmig/fix/company.py
    payload_extra: dict[str, Any] = Field(default_factory=dict)      # extra top-level payload fields, sent verbatim
    response_path: Optional[str] = None      # dot path to the reply text, e.g. "data.result.content"; None → auto-detect
    # 2026-10-22: per-project API cost in the report. Keys are model ids as the gateway names them (exact match first,
    # then the longest key that is a prefix of / contained in the model id, case-insensitive); unmatched models are
    # listed as unpriced. Company specifics live in config/default.yml / the company config, never in code.
    pricing: dict[str, ModelPrice] = Field(default_factory=dict)
    pricing_currency: str = "USD"
    pricing_note: str = ""                   # printed under the cost table (source + date of the prices)


class ScriptConfig(BaseModel):
    """v2 fix scripts (sbmig.script): the LLM answers with `def fix(p)`; it runs in a separate `python -I` process
    against the helper API `p` (project-folder files only), after a static check (allowed imports only; no open /
    exec / eval / os / subprocess / socket / dunder access). Not a sandbox against a hostile author — a guard
    against a script that reaches outside the project by mistake (docs/v2/PLAN-v2.md §3.2)."""
    timeout_seconds: int = 60                # per run; the process tree is killed after it (status timeout)
    python: Optional[str] = None             # interpreter for the script process; None = the one running sbmig
    extra_allowed_imports: list[str] = Field(default_factory=list)   # added to the built-in whitelist (company call)
    max_code_chars: int = 30000              # a longer script is refused (status rejected)
    log_lines: int = 200                     # p.log lines kept per run
    max_files_changed_warn: int = 40         # a run that changes more files gets a warning note (the diff is kept)
    reuse: bool = True                       # same case seen before → run its best stored version first (no LLM)
    reuse_versions: int = 1                  # stored versions tried per case and run before the LLM is asked
    max_augments_per_case: int = 3           # LLM extensions per case and run; then the case is BLOCKED
                                             # (repeated_failure) and handed over


class ErrorsConfig(BaseModel):
    """v2.1 error list (docs/v2/PLAN-v2.1.md §3.3): <run dir>/errors.jsonl + errors.md, appended to <kb>/errors.jsonl."""
    diff_chars: int = 6000                   # cap of a try's stored diff (0 = no cap); the full diff is in changes.patch
    raw_chars: int = 600                     # cap of an entry's raw error block
    md_view: bool = True                     # write <run dir>/errors.md and <kb>/cases.md


class ReviewConfig(BaseModel):
    """v2 structure review (sbmig.review): after the restructure, before the first build, the LLM checks the poms,
    dependencies and module layout against the company rules. Critical findings → one fix script (committed, can be
    reverted); warnings → the report section "Structure review" and <kb>/reviews.jsonl."""
    enabled: bool = True
    fix_critical: bool = True                # run the review's script when it reports critical issues
    max_pom_chars: int = 60000               # all pom text in the prompt; longer poms are cut (head + tail)
    max_poms: int = 40
    record: bool = True                      # one line per run in <kb>/reviews.jsonl


class FixConfig(BaseModel):
    """Second adapter tier: replace the whole fixer (our SEARCH/REPLACE protocol over a chat completion) with an
    in-house coding agent. `plugin` = "pkg.module:attr" where attr is a FixerLike class or a factory
    `fn(options: dict, cfg: Config, llm: LLMClientLike | None) -> FixerLike`."""
    plugin: Optional[str] = None
    plugin_options: dict[str, Any] = Field(default_factory=dict)


class KBConfig(BaseModel):
    dir: str = "kb-v2"                       # relative to repo root unless absolute: errors.jsonl, cases.md, cases/<id>/
    # secret masking rules (built-ins + this yml: company additions, invariant 2), relative to the repo root
    redact_file: str = "knowledge/kb/redact.yml"
    lock_timeout_seconds: int = 300          # cross-process kb/.lock wait before a write gives up (logged, never fatal)


class PlanConfig(BaseModel):
    advisor: Literal["none", "llm"] = "none"   # optional LLM review of the rule-based plan
    catalog_file: str = "knowledge/recipes/catalog.yml"   # single source of truth for known recipes (advisor
                                             # whitelist, coverage, gates, descriptions); relative to repo root


class FleetConfig(BaseModel):
    """`sbmig fleet`: one agent, N projects, strictly sequential, KB fed between projects."""
    order: Literal["manifest", "simple-first"] = "manifest"   # simple-first: pure_java → servlet → spring_legacy → boot → msa
    stop_on_abort: bool = False              # stop the whole fleet when one project aborts (exit 3)
    retry_failed: bool = False               # re-run projects whose last status was incomplete/aborted
    max_projects: int = 0                    # 0 = all
    max_llm_calls_total: int = 0             # 0 = unlimited; fleet-wide LLM budget across projects
    reindex_every: int = 10                  # kb reindex after every N projects (0 = never)
    # round 10: infrastructure guard for the 500-project run
    preflight: bool = True                   # check Java / Maven / settings / LLM / disk once before the first project
    infra_failure_limit: int = 3             # this many projects in a row ended for an infrastructure reason → wait
    infra_retry_minutes: int = 15            # wait between infrastructure re-checks (0 = stop the fleet right away)
    infra_max_waits: int = 4                 # re-checks before the fleet stops (state saved; `fleet resume` goes on)
    cleanup: Literal["none", "build-output", "workspace"] = "build-output"   # after a project: build-output = delete
                                             # target/ dirs of its run copy; workspace = also the project copy (result.zip,
                                             # report and patch stay)
    min_free_gb: float = 5.0                 # free space needed on the output drive before a project starts (0 = no check)
    summary_formats: list[str] = Field(default_factory=lambda: ["md", "csv", "json"])


class ReportConfig(BaseModel):
    formats: list[str] = Field(default_factory=lambda: ["md", "json"])
    zip_result: bool = True
    include_diff: bool = True
    language: Literal["en", "ko"] = "en"   # report.md language (report.json, console and logs stay English)
    # after the final build: `mvn dependency:tree` per module → javax EE API jars still on the classpath and the
    # direct dependency that drags each in (the library still compiled against javax); also fills BlockedItem.via
    javax_api_inventory: bool = True


class AdvisoryConfig(BaseModel):
    """Library advisories (knowledge/libraries/advisories.yml → sbmig.advisory): EOL / no-longer-managed libraries
    such as springfox, hystrix, sleuth. Never blocks a run — the build goes as far as it can and the report's
    Handover section says what the owning team must do next."""
    enabled: bool = True
    file: str = "knowledge/libraries/advisories.yml"      # relative to repo root unless absolute
    extra_files: list[str] = Field(default_factory=list)  # company additions / overrides (same schema, merged by id,
                                                          # later files win)
    policy_overrides: dict[str, Literal["guide", "off"]] = Field(default_factory=dict)
                                                          # advisory id → "off" disables the entry ("guide" = as is)
    report_carried_over: bool = True                      # list as-is-version pins (unmanaged after the parent swap)
    max_from_here: int = 40                               # cap of the handover's "from here on" list


class ExternalizedConfig(BaseModel):
    """Round 8: settings the code reads but no packaged resource defines (they came from the container before:
    JNDI, context-param, -D, catalina.properties). sbmig lists them, writes sentinel placeholders so the packaged
    configuration is complete, and the Handover tells the owners which values to set and how."""
    enabled: bool = True
    sentinel: str = "__SET_ME__"
    write_placeholders: bool = True                   # append the block to the boot-jar application.properties
    placeholder_file: str = "application.properties"  # relative to src/main/resources of boot-jar / each MSA service
    rules_file: str = "knowledge/config/externalized.yml"   # JNDI / context-param / system-property → Boot key rules + guide
    include_test_refs: bool = True                    # keys read only by tests still get placeholders (tests must load)
    max_refs_per_key: int = 3                         # references shown per key in the Handover


class Config(BaseModel):
    target: TargetConfig = Field(default_factory=TargetConfig)
    artifacts: ArtifactsConfig
    layout: LayoutConfig = Field(default_factory=LayoutConfig)
    openrewrite: OpenRewriteConfig = Field(default_factory=OpenRewriteConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    fix: FixConfig = Field(default_factory=FixConfig)
    script: ScriptConfig = Field(default_factory=ScriptConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    errors: ErrorsConfig = Field(default_factory=ErrorsConfig)
    kb: KBConfig = Field(default_factory=KBConfig)
    plan: PlanConfig = Field(default_factory=PlanConfig)
    fleet: FleetConfig = Field(default_factory=FleetConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    advisories: AdvisoryConfig = Field(default_factory=AdvisoryConfig)
    externalized: ExternalizedConfig = Field(default_factory=ExternalizedConfig)
    repo_root: str = str(PACKAGE_ROOT)

    # ---- helpers -----------------------------------------------------
    def resolve_path(self, p: str | Path) -> Path:
        p = Path(p)
        return p if p.is_absolute() else Path(self.repo_root) / p

    @property
    def kb_dir(self) -> Path:
        return self.resolve_path(self.kb.dir)

    @property
    def recipe_catalog_file(self) -> Path:
        return self.resolve_path(self.plan.catalog_file)

    @property
    def advisories_file(self) -> Path:
        return self.resolve_path(self.advisories.file)

    @property
    def advisory_files(self) -> list[Path]:
        """Base advisories file followed by the company extra files (later wins, merged by id)."""
        return [self.advisories_file, *(self.resolve_path(p) for p in self.advisories.extra_files)]

    def optional_keys(self) -> dict[str, ArtifactSpec]:
        out: dict[str, ArtifactSpec] = {}
        for spec in self.artifacts.optional:
            out[spec.key] = spec
            for alias in spec.aliases:
                out[alias] = spec
        return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else m.group(0))
        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_env_overrides(data: dict) -> dict:
    """SBMIG_LLM__MODEL=foo  →  data["llm"]["model"] = "foo"  (double underscore = nesting)."""
    for key, val in os.environ.items():
        if not key.startswith("SBMIG_"):
            continue
        path = [p.lower() for p in key[len("SBMIG_"):].split("__") if p]
        if not path:
            continue
        cur = data
        for p in path[:-1]:
            cur = cur.setdefault(p, {})
            if not isinstance(cur, dict):
                break
        else:
            cur[path[-1]] = yaml.safe_load(val)
    return data


_DOTENV_LOADED: dict[str, float] = {}     # resolved path → mtime it was read at
_DOTENV_SET: dict[str, str] = {}          # keys this loader put into os.environ (a later edit of the file may update them)


def _read_env_text(f: Path) -> str:
    """`.env` text whatever the editor saved: UTF-8 (BOM or not), UTF-16 (PowerShell 5.1 `>`), else cp949."""
    data = f.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp949", errors="replace")


def load_dotenv_files(*extra: str | Path) -> list[Path]:
    """Read `KEY=VALUE` lines from `$SBMIG_ENV_FILE`, `<repo root>/.env` and `<cwd>/.env` into `os.environ`
    (existing variables win; `export KEY=...`, quotes and `#` comments are accepted). The files are git-ignored
    and are never read into the config itself: keys still reach sbmig only through environment variables."""
    found: list[Path] = []
    candidates = [os.environ.get("SBMIG_ENV_FILE"), PACKAGE_ROOT / ".env", Path.cwd() / ".env", *extra]
    for c in candidates:
        if not c:
            continue
        f = Path(c).expanduser()
        try:
            key = str(f.resolve())
            if not f.is_file():
                continue
            mtime = f.stat().st_mtime
            if _DOTENV_LOADED.get(key) == mtime:          # read before and unchanged since
                found.append(f)
                continue
            text = _read_env_text(f)
        except (OSError, UnicodeError):
            continue
        found.append(f)
        _DOTENV_LOADED[key] = mtime                       # an edited file is read again (web server, fleet)
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            elif v.startswith("#"):
                v = ""           # `KEY=   # comment` is an empty value (2026-10-23: a company .env sent the comment
                                 # text of LLM_RECIPE_MODEL / LLM_ESCALATION_MODEL as the model name → HTTP 400)
            else:
                v = v.split(" #", 1)[0].rstrip()
            # shell variables win; a value this loader set earlier follows the edited file
            if k and (k not in os.environ or (k in _DOTENV_SET and os.environ[k] == _DOTENV_SET[k])):
                os.environ[k] = v
                _DOTENV_SET[k] = v
    return found


def load_config(*paths: str | Path, overrides: Optional[dict] = None, repo_root: Optional[Path] = None) -> Config:
    """Load default.yml, then each given yml (artifacts yml, site yml ...), then dict overrides, then env
    (`.env` files are read into the environment first, see `load_dotenv_files`)."""
    load_dotenv_files()
    data: dict = {}
    files = [DEFAULT_CONFIG_PATH, *[Path(p) for p in paths]]
    for f in files:
        if f and Path(f).exists():
            with open(f, "r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            data = deep_merge(data, loaded)
    if overrides:
        data = deep_merge(data, overrides)
    data = _apply_env_overrides(data)
    data = _expand_env(data)
    if repo_root:
        data["repo_root"] = str(repo_root)
    return Config.model_validate(data)
