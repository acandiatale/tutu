"""Configuration schema + loader.

Layering (later wins):  config/default.yml  →  --config file(s)  →  environment overrides (SBMIG_* / ${VAR} expansion).
Company-specific coordinates live in a separate artifacts yml so the code base stays generic.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field

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
    module_dir: str = "module"
    boot_module: str = "boot-jar"
    comp_module: str = "comp-jar"
    boot_artifact_suffix: str = "-boot"
    comp_artifact_suffix: str = "-comp"
    split_policy: Literal["minimal", "web-layer"] = "minimal"
    multi_module_policy: Literal["wrap", "merge"] = "merge"
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
    import_xml_bean_configs: bool = True   # @ImportResource for detected <beans> xml files
    msa_parent_policy: Literal["per-service-root", "top-aggregator"] = "per-service-root"


class RecipeSetConfig(BaseModel):
    jdk21: list[str] = Field(default_factory=lambda: ["org.openrewrite.java.migrate.UpgradeToJava21"])
    jakarta: list[str] = Field(default_factory=lambda: ["org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta"])
    spring_boot_4: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0",
        "org.openrewrite.java.spring.boot4.SpringBootProperties_4_1",
        "com.company.migrate.SpringBoot41Version",       # project recipe in knowledge/recipes/base
        "com.company.migrate.Boot4JacksonTwins",        # project recipe: Boot Jackson types → Jackson 3 twins
    ])
    spring_framework_7: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0",
    ])
    spring_security_7: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0",
    ])
    junit5: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.java.testing.junit5.JUnit4to5Migration",
    ])
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
    base_recipe_dir: str = "knowledge/recipes/base"    # project-owned declarative recipes (relative to repo root)
    timeout_minutes: int = 40
    jvm_args: list[str] = Field(default_factory=lambda: ["-Xmx4g"])
    oom_retry_jvm_args: list[str] = Field(default_factory=lambda: ["-Xmx8g"])   # one retry after OutOfMemoryError
    # round 9: the recipe run failed (or left literal `x.y.x` versions) because a download failed (proxy 429/5xx,
    # reset, timeout) → wait and retry the run; 0 = no retry
    download_retry: int = 1
    download_retry_wait_s: int = 30
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
    allow_skip_test_compile_fallback: bool = True       # -Dmaven.test.skip=true as last resort (flagged in report)
    allow_skip_tests_fallback: bool = True              # -DskipTests when only runtime test failures remain
                                                        # (flag `tests-skipped`, failing tests listed as follow-ups)
    timeout_minutes: int = 30
    env: dict[str, str] = Field(default_factory=dict)
    # legacy source encodings (JDK 18+ defaults to UTF-8 → "unmappable character" on MS949/EUC-KR sources)
    source_encoding_policy: Literal["set-property", "convert-utf8"] = "set-property"
    encoding_candidates: list[str] = Field(default_factory=lambda: ["utf-8", "cp949", "euc-kr", "shift_jis", "latin-1"])
    enforcer_policy: Literal["relax-rule", "skip", "keep"] = "relax-rule"    # requireJavaVersion/convergence failures
    werror_policy: Literal["remove", "keep"] = "remove"                       # -Werror makes JDK 21 deprecations fatal
    # package prefixes treated as *library* code in error signatures (project-owned FQCNs are masked as '*').
    # In-house library namespaces (parent/mandatory/optional group ids) are added automatically.
    external_package_prefixes: list[str] = Field(default_factory=list)


class PhaseLoopConfig(BaseModel):
    goal: str = "compile"
    max_iterations: int = 5
    enabled: bool = True


class LoopConfig(BaseModel):
    phases: dict[str, PhaseLoopConfig] = Field(default_factory=lambda: {
        "jdk21": PhaseLoopConfig(goal="compile", max_iterations=3),
        "spring": PhaseLoopConfig(goal="test-compile", max_iterations=6),
        "restructure": PhaseLoopConfig(goal="package", max_iterations=6),
    })
    max_llm_calls: int = 60
    max_total_minutes: int = 180
    stall_limit: int = 2                     # identical error multiset N times → escalate strategy
    max_clusters_per_iteration: int = 8
    revert_on_regression: bool = True
    regression_ratio: float = 1.5            # errors_after > errors_before * ratio → regression
    always_final_verify: bool = True         # `mvn clean package` at the end no matter what
    engine: Literal["graph", "inline"] = "graph"   # graph: LangGraph StateGraph drives the phases (company
                                             # convention, sbmig.graph); inline: the original in-process loop
    oscillation_window: int = 4              # error-set history compared for A↔B toggling (≥ stall detection window)
    max_llm_attempts_per_cluster: int = 2    # LLM tries per cluster signature set before it is BLOCKED(repeated_failure)
    # how many LLM attempts a cluster of this BlockReason may get before it is blocked (0 = block immediately)
    blocked_policy: dict[str, int] = Field(default_factory=lambda: {
        "external_javax_api": 0, "external_api_incompatible": 1, "artifact_not_found": 0, "parent_not_found": 0,
        "plugin_incompatible": 1, "unsupported_source": 0, "encoding": 0,
    })
    structural_regression_kinds: list[str] = Field(default_factory=lambda: [
        "pom_invalid", "parent_resolution", "plugin_resolution"])   # appearing after a pom edit ⇒ revert regardless of count


BUILTIN_LLM_PROVIDERS = ("company", "openai_compatible", "anthropic", "fake")


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


class FixConfig(BaseModel):
    """Second adapter tier: replace the whole fixer (our SEARCH/REPLACE protocol over a chat completion) with an
    in-house coding agent. `plugin` = "pkg.module:attr" where attr is a FixerLike class or a factory
    `fn(options: dict, cfg: Config, llm: LLMClientLike | None) -> FixerLike`."""
    plugin: Optional[str] = None
    plugin_options: dict[str, Any] = Field(default_factory=dict)


class KBConfig(BaseModel):
    dir: str = "kb"                          # relative to repo root unless absolute
    ledger: bool = True                      # append one ResolutionRecord per resolved/blocked cluster to kb/ledger.jsonl
    symbol_index: bool = True                # maintain kb/symbols.json: error symbol → fix labels / replacements
    deterministic_candidates: bool = True    # compile recipe candidates from labeled fixes without the LLM when possible
    use_active_recipes: bool = True
    use_patch_replay: bool = True
    use_hints: bool = True
    harvest: bool = True
    generate_recipe_candidates: bool = True
    auto_promote_after_projects: int = 2     # candidate → active after success in N distinct projects (0 = never)
    try_candidates_in_plan: bool = True      # run candidate recipes inside the plan's OpenRewrite step (as-is classpath →
                                             # type attribution works); credited success/fail by the phase's build
    max_hints: int = 5


class PlanConfig(BaseModel):
    advisor: Literal["none", "llm"] = "none"   # optional LLM review of the rule-based plan
    catalog_file: str = "knowledge/recipes/catalog.yml"   # single source of truth for known recipes (advisor
                                             # whitelist, coverage, gates, descriptions); relative to repo root
    intermediate_builds: bool = True           # build after jdk21 phase before spring phase
    optional_bump_phase: Literal["spring", "restructure"] = "spring"   # bump in-house optional artifacts right after the
                                             # spring rewrite so jakarta builds of in-house libs are on the classpath before the first build


class FleetConfig(BaseModel):
    """`sbmig fleet`: one agent, N projects, strictly sequential, KB fed between projects."""
    order: Literal["manifest", "simple-first"] = "manifest"   # simple-first: pure_java → servlet → spring_legacy → boot → msa
    stop_on_abort: bool = False              # stop the whole fleet when one project aborts (exit 3)
    retry_failed: bool = False               # re-run projects whose last status was incomplete/aborted
    max_projects: int = 0                    # 0 = all
    max_llm_calls_total: int = 0             # 0 = unlimited; fleet-wide LLM budget across projects
    reindex_every: int = 10                  # kb reindex after every N projects (0 = never)
    summary_formats: list[str] = Field(default_factory=lambda: ["md", "csv", "json"])


class ReportConfig(BaseModel):
    formats: list[str] = Field(default_factory=lambda: ["md", "json"])
    zip_result: bool = True
    include_diff: bool = True


class AdvisoryConfig(BaseModel):
    """Library advisories (knowledge/libraries/advisories.yml → sbmig.advisory): EOL / no-longer-managed libraries
    such as springfox, hystrix, sleuth. Never blocks a run — the build goes as far as it can and the report's
    Handover section says what the owning team must do next."""
    enabled: bool = True
    file: str = "knowledge/libraries/advisories.yml"      # relative to repo root unless absolute
    extra_files: list[str] = Field(default_factory=list)  # company additions / overrides (same schema, merged by id,
                                                          # later files win)
    allow_recipes: bool = True                            # False → every advisory behaves as policy "guide"
    policy_overrides: dict[str, Literal["guide", "recipe", "off"]] = Field(default_factory=dict)
                                                          # advisory id → policy ("off" disables the entry)
    report_carried_over: bool = True                      # list as-is-version pins (unmanaged after the parent swap)
    ledger: bool = True                                   # write advisory findings to kb/ledger.jsonl (resolution=advisory)
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
    def base_recipe_dir(self) -> Path:
        return self.resolve_path(self.openrewrite.base_recipe_dir)

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


_DOTENV_LOADED: set[str] = set()


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
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        found.append(f)
        if key in _DOTENV_LOADED:
            continue
        _DOTENV_LOADED.add(key)
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
            else:
                v = v.split(" #", 1)[0].rstrip()
            if k and k not in os.environ:
                os.environ[k] = v
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
