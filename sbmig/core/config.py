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
    parent: ArtifactSpec
    parent_manages_spring_boot: bool = True   # if False we import spring-boot-dependencies BOM ourselves
    parent_manages_java_version: bool = True  # if False we set maven.compiler.release
    mandatory: list[ArtifactSpec] = Field(default_factory=list)   # exactly 2 in production config
    optional: list[ArtifactSpec] = Field(default_factory=list)    # 8 in production config
    remove_when_present: list[str] = Field(default_factory=list)  # "g:a" keys to drop (e.g. old in-house parents' BOMs)


class LayoutConfig(BaseModel):
    module_dir: str = "module"
    boot_module: str = "boot-jar"
    comp_module: str = "comp-jar"
    boot_artifact_suffix: str = "-boot"
    comp_artifact_suffix: str = "-comp"
    split_policy: Literal["minimal", "web-layer"] = "minimal"
    multi_module_policy: Literal["wrap", "merge"] = "wrap"
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
        "com.company.migrate.Boot4Jackson2Bridge",      # project recipe: Jackson 2 support classes → jackson2 module
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
    timeout_minutes: int = 30
    env: dict[str, str] = Field(default_factory=dict)
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


class LLMConfig(BaseModel):
    provider: Literal["openai_compatible", "anthropic", "fake"] = "openai_compatible"
    base_url: str = "${LLM_BASE_URL}"
    api_key_env: str = "LLM_API_KEY"
    model: str = "${LLM_MODEL}"
    escalation_model: Optional[str] = None   # stronger model used after stall
    temperature: float = 0.0
    max_output_tokens: int = 8000
    max_context_chars: int = 60000           # budget for packed context per request
    timeout_seconds: int = 180
    max_retries: int = 3
    max_context_rounds: int = 2              # how many times the LLM may ask for more files per cluster
    extra_headers: dict[str, str] = Field(default_factory=dict)
    prompt_version: str = "v1"
    record_dir: Optional[str] = None         # record requests/responses for replay tests


class KBConfig(BaseModel):
    dir: str = "kb"                          # relative to repo root unless absolute
    use_active_recipes: bool = True
    use_patch_replay: bool = True
    use_hints: bool = True
    harvest: bool = True
    generate_recipe_candidates: bool = True
    auto_promote_after_projects: int = 2     # candidate → active after success in N distinct projects (0 = never)
    max_hints: int = 5


class PlanConfig(BaseModel):
    advisor: Literal["none", "llm"] = "none"   # optional LLM review of the rule-based plan
    intermediate_builds: bool = True           # build after jdk21 phase before spring phase


class ReportConfig(BaseModel):
    formats: list[str] = Field(default_factory=lambda: ["md", "json"])
    zip_result: bool = True
    include_diff: bool = True


class Config(BaseModel):
    target: TargetConfig = Field(default_factory=TargetConfig)
    artifacts: ArtifactsConfig
    layout: LayoutConfig = Field(default_factory=LayoutConfig)
    openrewrite: OpenRewriteConfig = Field(default_factory=OpenRewriteConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    kb: KBConfig = Field(default_factory=KBConfig)
    plan: PlanConfig = Field(default_factory=PlanConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
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


def load_config(*paths: str | Path, overrides: Optional[dict] = None, repo_root: Optional[Path] = None) -> Config:
    """Load default.yml, then each given yml (artifacts yml, site yml ...), then dict overrides, then env."""
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
