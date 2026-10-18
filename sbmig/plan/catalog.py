"""Recipe catalog (`knowledge/recipes/catalog.yml`) — the single managed list of known recipe ids.

The planner derives the LLM advisor whitelist / coverage / gates from it and `sbmig recipes …` reads it.
It describes (and `check()` cross-validates) the other recipe sources: config recipe sets, reactive rules
(`sbmig/triage/recipe_rules.yml`), project recipes (`knowledge/recipes/base/*.yml`) and the configured recipe
artifacts. Learned KB recipes are not listed in the file; `with_kb()` merges them in live. See docs/RECIPES.md.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Literal, Optional

import yaml
from pydantic import BaseModel, Field

from sbmig.core.config import PACKAGE_ROOT, Config
from sbmig.core.models import CaseType, ProjectProfile

log = logging.getLogger("sbmig.plan.catalog")

DEFAULT_CATALOG_PATH = PACKAGE_ROOT / "knowledge" / "recipes" / "catalog.yml"
CORE_GROUP = "org.openrewrite:"       # core recipes ship with rewrite-maven-plugin (never a configured artifact)

# prefix → artifact fallback for ids that are not in the catalog (longest prefix wins)
PREFIX_ARTIFACTS: dict[str, str] = {
    "org.openrewrite.java.spring": "org.openrewrite.recipe:rewrite-spring",
    "org.openrewrite.java.migrate": "org.openrewrite.recipe:rewrite-migrate-java",
    "org.openrewrite.java.testing": "org.openrewrite.recipe:rewrite-testing-frameworks",
    "org.openrewrite.staticanalysis": "org.openrewrite.recipe:rewrite-static-analysis",
}


# ---------------------------------------------------------------------------
# named gates (profile facts an advisor suggestion must be backed by)
# ---------------------------------------------------------------------------
_ADVISOR_EE = ("javax.servlet", "javax.persistence", "javax.validation", "javax.annotation", "javax.inject",
               "javax.transaction", "javax.ws", "javax.jms", "javax.mail", "javax.websocket", "javax.el",
               "javax.xml.bind")


def _has_javax_ee(profile: ProjectProfile) -> bool:
    return any(k.startswith(_ADVISOR_EE) for k in profile.source.javax_imports)


def _has_javax_xml_bind(p: ProjectProfile) -> bool:
    return any(k.startswith("javax.xml.bind") for k in p.source.javax_imports)


def _has_xml_bind_any(p: ProjectProfile) -> bool:
    return any(k.startswith(("javax.xml.bind", "jakarta.xml.bind"))
               for k in {**p.source.javax_imports, **p.source.jakarta_imports})


def _is_spring(p: ProjectProfile) -> bool:
    return p.case_type in (CaseType.SPRING_LEGACY, CaseType.SPRING_BOOT) or p.spring_version is not None


GATES: dict[str, Callable[[ProjectProfile], bool]] = {
    "has_junit4": lambda p: p.has_library("junit4"),
    "has_javax_ee": _has_javax_ee,
    "has_javax_xml_bind": _has_javax_xml_bind,
    "has_xml_bind_any": _has_xml_bind_any,
    "has_spring_security": lambda p: p.has_library("spring-security"),
    "is_spring": _is_spring,
    "is_boot": lambda p: p.spring_boot_version is not None,
    "has_tests": lambda p: p.source.test_files > 0,
    "always": lambda p: True,
}


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------
class CatalogEntry(BaseModel):
    id: str
    origin: Literal["openrewrite", "project", "kb"] = "openrewrite"
    artifact: Optional[str] = None
    provided_by: Optional[str] = None          # configured artifact that brings `artifact` in transitively
    file: Optional[str] = None                 # project recipes: yml relative to the repo root
    phase: Literal["jdk21", "spring", "restructure"] = "spring"
    description: str = ""
    docs: Optional[str] = None
    covers: list[str] = Field(default_factory=list)
    gate: Optional[str] = None
    advisor: bool = False
    sets: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    verified: Optional[str] = None
    notes: Optional[str] = None


_RECIPE_SET_NAMES = ("jdk21", "jakarta", "spring_boot_4", "spring_framework_7", "spring_security_7", "junit5")


def config_recipe_sets(cfg: Config) -> dict[str, list[str]]:
    """set name → ids, as configured (`extra.<case>` sets appear as `extra.<case>`)."""
    rs = cfg.openrewrite.recipes
    out = {name: list(getattr(rs, name)) for name in _RECIPE_SET_NAMES}
    for case, ids in (rs.extra or {}).items():
        out[f"extra.{case}"] = list(ids)
    return out


def _rules_path() -> Path:
    try:
        from sbmig.triage.rules import DEFAULT_RULES_PATH
        return Path(DEFAULT_RULES_PATH)
    except Exception:  # pragma: no cover - triage package broken
        return PACKAGE_ROOT / "sbmig" / "triage" / "recipe_rules.yml"


def reactive_rules(path: Optional[Path] = None) -> dict[str, list[str]]:
    """rule id → recipe ids (sbmig/triage/recipe_rules.yml)."""
    p = path or _rules_path()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("cannot read recipe rules %s: %s", p, e)
        return {}
    return {str(r.get("id")): [str(x) for x in (r.get("recipes") or [])] for r in data.get("rules") or []}


def _recipe_list_id(item: Any) -> Optional[str]:
    if isinstance(item, str):
        return item
    if isinstance(item, dict) and len(item) == 1:
        return str(next(iter(item)))
    return None


def base_recipes(base_dir: Path) -> dict[str, dict[str, Any]]:
    """declared project recipe name → {"file": path, "uses": [recipeList ids]} for every yml in `base_dir`."""
    out: dict[str, dict[str, Any]] = {}
    if not base_dir.is_dir():
        return out
    for f in sorted([*base_dir.glob("*.yml"), *base_dir.glob("*.yaml")]):
        try:
            docs = list(yaml.safe_load_all(f.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError) as e:
            log.warning("cannot read project recipe file %s: %s", f, e)
            continue
        for d in docs:
            if isinstance(d, dict) and d.get("name"):
                uses = [i for i in (_recipe_list_id(x) for x in d.get("recipeList") or []) if i]
                out[str(d["name"])] = {"file": f, "uses": uses}
    return out


class RecipeCatalog:
    """In-memory view of the catalog file (entries in file order)."""

    def __init__(self, entries: list[CatalogEntry], path: Optional[Path] = None, version: int = 1):
        self.entries = list(entries)
        self.path = path
        self.version = version
        self._by_id = {e.id: e for e in self.entries}

    # ---- loading -------------------------------------------------------
    _cache: dict[tuple[str, float], "RecipeCatalog"] = {}

    @classmethod
    def from_dict(cls, data: dict, path: Optional[Path] = None) -> "RecipeCatalog":
        entries = [CatalogEntry.model_validate(r) for r in (data.get("recipes") or [])]
        seen: set[str] = set()
        for e in entries:
            if e.id in seen:
                raise ValueError(f"duplicate recipe id {e.id} in catalog {path}")
            seen.add(e.id)
        return cls(entries, path=path, version=int(data.get("version", 1)))

    @classmethod
    def load(cls, path: str | Path) -> "RecipeCatalog":
        p = Path(path).resolve()
        key = (str(p), p.stat().st_mtime)
        hit = cls._cache.get(key)
        if hit is not None:
            return hit
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        cat = cls.from_dict(data, path=p)
        cls._cache = {k: v for k, v in cls._cache.items() if k[0] != str(p)}
        cls._cache[key] = cat
        return cat

    # ---- lookups -------------------------------------------------------
    def get(self, recipe_id: str) -> Optional[CatalogEntry]:
        return self._by_id.get(recipe_id)

    def ids(self) -> list[str]:
        return [e.id for e in self.entries]

    def __contains__(self, recipe_id: object) -> bool:
        return recipe_id in self._by_id

    def __len__(self) -> int:
        return len(self.entries)

    def advisor_catalog(self) -> dict[str, str]:
        return {e.id: e.description for e in self.entries if e.advisor}

    def coverage(self) -> dict[str, list[str]]:
        return {e.id: list(e.covers) for e in self.entries if e.covers}

    def gates(self) -> dict[str, Callable[[ProjectProfile], bool]]:
        out: dict[str, Callable[[ProjectProfile], bool]] = {}
        for e in self.entries:
            if e.gate is None:
                continue
            fn = GATES.get(e.gate)
            if fn is None:
                log.warning("catalog: unknown gate %r for %s (treated as always allowed)", e.gate, e.id)
                continue
            out[e.id] = fn
        return out

    def artifact_for(self, recipe_id: str) -> Optional[str]:
        """Catalog first, then the prefix fallback (None for project / core / unknown recipes)."""
        e = self._by_id.get(recipe_id)
        if e is not None:
            return e.artifact
        return prefix_artifact(recipe_id)

    def covered_by(self, planned_ids: set[str] | list[str]) -> dict[str, str]:
        """recipe id → the planned composite that already includes it."""
        planned = set(planned_ids)
        out: dict[str, str] = {}
        for comp, inner in self.coverage().items():
            if comp in planned:
                for rid in inner:
                    out.setdefault(rid, comp)
        return out

    # ---- validation ----------------------------------------------------
    def check(self, cfg: Config, *, rules_path: Optional[Path] = None) -> list[str]:
        """Problems between the catalog and the other recipe sources ([] = consistent)."""
        problems: list[str] = []
        sets = config_recipe_sets(cfg)
        rules = reactive_rules(rules_path)
        base = base_recipes(cfg.base_recipe_dir)
        artifacts = set(cfg.openrewrite.recipe_artifacts)

        for name, ids in sets.items():
            for rid in ids:
                if rid not in self:
                    problems.append(f"config set openrewrite.recipes.{name}: {rid} is not in the catalog")
                elif name not in self._by_id[rid].sets:
                    problems.append(f"catalog {rid}: sets lacks {name!r} (referenced by openrewrite.recipes.{name})")
        for rule_id, ids in rules.items():
            for rid in ids:
                if rid not in self:
                    problems.append(f"reactive rule {rule_id}: {rid} is not in the catalog")
                elif rule_id not in self._by_id[rid].rules:
                    problems.append(f"catalog {rid}: rules lacks {rule_id!r} (recipe_rules.yml uses it)")
        for name, info in base.items():
            if name not in self:
                problems.append(f"project recipe {name} ({info['file'].name}) is not in the catalog")
            for rid in info["uses"]:
                if rid not in self and rid not in base:
                    problems.append(f"project recipe {name} uses {rid}, which is not in the catalog")

        for e in self.entries:
            for s in e.sets:
                if s not in sets or e.id not in sets[s]:
                    problems.append(f"catalog {e.id}: lists set {s!r} but openrewrite.recipes.{s} does not contain it")
            for r in e.rules:
                if r not in rules or e.id not in rules[r]:
                    problems.append(f"catalog {e.id}: lists rule {r!r} but recipe_rules.yml rule does not use it")
            if e.gate is not None and e.gate not in GATES:
                problems.append(f"catalog {e.id}: unknown gate {e.gate!r} (known: {', '.join(GATES)})")
            for c in e.covers:
                if c not in self:
                    problems.append(f"catalog {e.id}: covers unknown recipe {c}")
            if e.origin == "kb":
                problems.append(f"catalog {e.id}: origin kb entries are read live from kb/, not listed in the catalog")
            if e.origin == "project":
                if not e.file:
                    problems.append(f"catalog {e.id}: project recipe without file")
                else:
                    f = cfg.resolve_path(e.file)
                    info = base.get(e.id)
                    if not f.exists():
                        problems.append(f"catalog {e.id}: file {e.file} does not exist")
                    elif info is None or Path(info["file"]).resolve() != f.resolve():
                        problems.append(f"catalog {e.id}: file {e.file} does not declare it")
            elif e.artifact and not e.artifact.startswith(CORE_GROUP):
                if e.artifact not in artifacts and (e.provided_by or "") not in artifacts:
                    problems.append(f"catalog {e.id}: artifact {e.artifact} is not in openrewrite.recipe_artifacts")
        return problems

    # ---- listing -------------------------------------------------------
    def rows(self) -> list[dict[str, Any]]:
        return [e.model_dump() for e in self.entries]

    def with_kb(self, kb: Any) -> list[dict[str, Any]]:
        """Catalog rows plus live KB recipes (origin kb, status, credits, signatures)."""
        rows = self.rows()
        try:
            recipes = kb.list_recipes() if kb is not None else []
        except Exception as e:  # listing must not fail because of a broken KB
            log.warning("cannot list kb recipes: %s", e)
            recipes = []
        for r in recipes:
            rows.append({
                "id": r.id, "origin": "kb", "artifact": None, "file": r.path, "phase": None,
                "description": r.description, "gate": None, "advisor": False, "sets": [], "rules": [],
                "status": r.status, "success_count": r.success_count, "fail_count": r.fail_count,
                "projects": len(r.projects), "signatures": list(r.signatures),
            })
        return rows


def prefix_artifact(recipe_id: str) -> Optional[str]:
    best, best_len = None, -1
    for prefix, art in PREFIX_ARTIFACTS.items():
        if recipe_id.startswith(prefix) and len(prefix) > best_len:
            best, best_len = art, len(prefix)
    return best


# ---------------------------------------------------------------------------
# built-in fallback (used only when the catalog file is missing / unreadable)
# ---------------------------------------------------------------------------
_MIGRATE = "org.openrewrite.recipe:rewrite-migrate-java"
_SPRING = "org.openrewrite.recipe:rewrite-spring"
_TESTING = "org.openrewrite.recipe:rewrite-testing-frameworks"

_BUILTIN: list[dict[str, Any]] = [
    {"id": "org.openrewrite.java.migrate.UpgradeToJava21", "artifact": _MIGRATE, "phase": "jdk21",
     "covers": ["org.openrewrite.java.migrate.javax.AddJaxbDependenciesWithRuntime"]},
    {"id": "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta", "artifact": _MIGRATE, "advisor": True,
     "gate": "has_javax_ee",
     "description": "javax.* EE packages → jakarta.* (servlet, persistence, validation, annotation, inject …)"},
    {"id": "org.openrewrite.java.migrate.javax.AddJaxbDependenciesWithRuntime", "artifact": _MIGRATE,
     "phase": "jdk21", "advisor": True, "gate": "has_xml_bind_any",
     "description": "add JAXB API + runtime when javax.xml.bind is used (removed from the JDK)"},
    {"id": "org.openrewrite.java.migrate.jakarta.JavaxXmlBindMigrationToJakartaXmlBind", "artifact": _MIGRATE,
     "advisor": True, "gate": "has_javax_xml_bind", "description": "javax.xml.bind → jakarta.xml.bind"},
    {"id": "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0", "artifact": _SPRING, "advisor": True,
     "gate": "is_boot", "description": "Spring Boot 2.x/3.x → 4.0 umbrella (Boot projects only)",
     "covers": ["org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta",
                "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0",
                "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0",
                "org.openrewrite.java.spring.boot4.ReplaceMockBeanAndSpyBean",
                "org.openrewrite.java.spring.boot4.SpringBootProperties_4_0"]},
    {"id": "org.openrewrite.java.spring.boot4.SpringBootProperties_4_1", "artifact": _SPRING, "advisor": True,
     "gate": "is_boot", "description": "Spring Boot 4.1 property renames"},
    {"id": "org.openrewrite.java.spring.boot4.ReplaceMockBeanAndSpyBean", "artifact": _SPRING, "advisor": True,
     "gate": "has_tests", "description": "@MockBean/@SpyBean → @MockitoBean/@MockitoSpyBean"},
    {"id": "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0", "artifact": _SPRING,
     "advisor": True, "gate": "is_spring",
     "description": "Spring Framework 5.x/6.x → 7.0 (non-Boot Spring projects)"},
    {"id": "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0", "artifact": _SPRING,
     "advisor": True, "gate": "has_spring_security",
     "description": "Spring Security → 7 (lambda DSL, requestMatchers, no WebSecurityConfigurerAdapter)"},
    {"id": "org.openrewrite.java.testing.junit5.JUnit4to5Migration", "artifact": _TESTING, "advisor": True,
     "gate": "has_junit4", "description": "JUnit 4 → JUnit 5"},
    {"id": "com.company.migrate.ServletToJakartaAndBoot", "origin": "project",
     "file": "knowledge/recipes/base/spring-boot-41-version.yml",
     "covers": ["org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta"]},
]


def builtin_catalog() -> RecipeCatalog:
    return RecipeCatalog.from_dict({"version": 1, "recipes": _BUILTIN})


def default_catalog(cfg: Optional[Config] = None) -> RecipeCatalog:
    """`cfg.recipe_catalog_file` (or the repo's catalog); built-in fallback + warning if missing. Never raises."""
    path = cfg.recipe_catalog_file if cfg is not None else DEFAULT_CATALOG_PATH
    try:
        return RecipeCatalog.load(path)
    except FileNotFoundError:
        log.warning("recipe catalog %s not found; using the built-in minimal catalog", path)
    except Exception as e:
        log.warning("recipe catalog %s unreadable (%s: %s); using the built-in minimal catalog",
                    path, type(e).__name__, e)
    return builtin_catalog()
