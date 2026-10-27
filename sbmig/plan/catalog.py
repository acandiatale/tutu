"""Recipe catalog (`knowledge/recipes/catalog.yml`) — the single managed list of known recipe ids.

The planner derives the LLM advisor whitelist / coverage / gates from it and `sbmig recipes …` reads it. It
describes (and `check()` cross-validates) the config recipe sets (`openrewrite.recipes.*`) and the configured recipe
artifacts. v2 runs OpenRewrite recipes only: no project recipes, no library recipes, no KB recipes. See
docs/RECIPES.md.
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
    origin: str = "openrewrite"                # v2 knows OpenRewrite recipes only (`check` flags anything else)
    artifact: Optional[str] = None
    provided_by: Optional[str] = None          # configured artifact that brings `artifact` in transitively
    phase: Literal["jdk21", "spring", "restructure"] = "spring"
    description: str = ""
    docs: Optional[str] = None
    covers: list[str] = Field(default_factory=list)
    gate: Optional[str] = None
    advisor: bool = False
    sets: list[str] = Field(default_factory=list)
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
        """Catalog first, then the prefix fallback (None for core / unknown recipes)."""
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
    def check(self, cfg: Config) -> list[str]:
        """Problems between the catalog, the config recipe sets and the recipe artifacts ([] = consistent)."""
        problems: list[str] = []
        sets = config_recipe_sets(cfg)
        artifacts = set(cfg.openrewrite.recipe_artifacts)

        for name, ids in sets.items():
            for rid in ids:
                if rid not in self:
                    problems.append(f"config set openrewrite.recipes.{name}: {rid} is not in the catalog")
                elif name not in self._by_id[rid].sets:
                    problems.append(f"catalog {rid}: sets lacks {name!r} (referenced by openrewrite.recipes.{name})")

        for e in self.entries:
            for s in e.sets:
                if s not in sets or e.id not in sets[s]:
                    problems.append(f"catalog {e.id}: lists set {s!r} but openrewrite.recipes.{s} does not contain it")
            if e.gate is not None and e.gate not in GATES:
                problems.append(f"catalog {e.id}: unknown gate {e.gate!r} (known: {', '.join(GATES)})")
            for c in e.covers:
                if c not in self:
                    problems.append(f"catalog {e.id}: covers unknown recipe {c}")
            if e.origin != "openrewrite":
                problems.append(f"catalog {e.id}: origin {e.origin!r} is not supported — sbmig runs OpenRewrite "
                                "recipes only (no project / KB recipes)")
            elif e.artifact and not e.artifact.startswith(CORE_GROUP):
                if e.artifact not in artifacts and (e.provided_by or "") not in artifacts:
                    problems.append(f"catalog {e.id}: artifact {e.artifact} is not in openrewrite.recipe_artifacts")
        return problems

    # ---- listing -------------------------------------------------------
    def rows(self) -> list[dict[str, Any]]:
        return [e.model_dump() for e in self.entries]


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
_SEC7_DESCRIPTION = ("Spring Security 6.x → 7.0 (version bump, SecurityConfigurer init/configure lose `throws Exception`, "
                     "AuthorizationManager.check → authorize); planned after the 6.0 … 6.5 steps")
# round 10: what the Framework 7 recipe / the Boot umbrella already run (verified in rewrite-spring 6.37.0 & co)
_FW7_COVERS = ["org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta",
               "org.openrewrite.java.migrate.jakarta.RemovalsServletJakarta10",
               "org.openrewrite.apache.httpclient5.UpgradeApacheHttpClient_5",
               "org.openrewrite.java.jackson.CodehausToFasterXML"]
_BOOT4_ROUND10_COVERS = [*(f"org.openrewrite.java.spring.security6.UpgradeSpringSecurity_6_{v}" for v in range(6)),
                         "org.openrewrite.java.spring.batch.SpringBatch5To6Migration",
                         "org.openrewrite.hibernate.MigrateToHibernate71",
                         "org.openrewrite.java.spring.boot3.MigrateThymeleafDependencies",
                         "org.openrewrite.java.migrate.jakarta.RemovalsServletJakarta10",
                         "org.openrewrite.apache.httpclient5.UpgradeApacheHttpClient_5",
                         "org.openrewrite.java.jackson.CodehausToFasterXML"]

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
                "org.openrewrite.java.spring.boot4.SpringBootProperties_4_0",
                "org.openrewrite.java.springdoc.SpringFoxToSpringDoc",
                "org.openrewrite.java.springdoc.UpgradeSpringDoc_3_0",
                "org.openrewrite.java.spring.cloud2022.MigrateCloudSleuthToMicrometerTracing",
                "org.openrewrite.java.spring.cloud2025.UpgradeSpringCloud_2025_1", *_BOOT4_ROUND10_COVERS]},
    {"id": "org.openrewrite.java.spring.boot4.SpringBootProperties_4_1", "artifact": _SPRING, "advisor": True,
     "gate": "is_boot", "description": "Spring Boot 4.1 property renames"},
    {"id": "org.openrewrite.java.spring.boot4.ReplaceMockBeanAndSpyBean", "artifact": _SPRING, "advisor": True,
     "gate": "has_tests", "description": "@MockBean/@SpyBean → @MockitoBean/@MockitoSpyBean"},
    {"id": "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0", "artifact": _SPRING,
     "advisor": True, "gate": "is_spring",
     "description": "Spring Framework 5.x/6.x → 7.0 (non-Boot Spring projects)", "covers": list(_FW7_COVERS)},
    {"id": "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0", "artifact": _SPRING,
     "advisor": True, "gate": "has_spring_security", "description": _SEC7_DESCRIPTION},
    {"id": "org.openrewrite.java.springdoc.SpringFoxToSpringDoc", "artifact": _SPRING},
    {"id": "org.openrewrite.java.springdoc.UpgradeSpringDoc_3_0", "artifact": _SPRING},
    {"id": "org.openrewrite.java.spring.cloud2022.MigrateCloudSleuthToMicrometerTracing", "artifact": _SPRING},
    {"id": "org.openrewrite.java.spring.cloud2025.UpgradeSpringCloud_2025_1", "artifact": _SPRING},
    {"id": "org.openrewrite.java.testing.junit5.JUnit4to5Migration", "artifact": _TESTING, "advisor": True,
     "gate": "has_junit4", "description": "JUnit 4 → JUnit 5"},
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
