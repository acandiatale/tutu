"""Pre-emptive handler selection from the profile (handlers themselves live in sbmig.triage.handlers).

Handler ids (contract with workstream E):
  lombok-version-floor, mapstruct-version-floor, querydsl-jakarta-classifier, maven-compiler-plugin-floor,
  surefire-plugin-floor, javax-servlet-api-to-jakarta, jaxb-runtime-add, remove-classic-loader-implementation,
  mysql-connector-relocation, compiler-legacy-config (round 9: fork/executable/bootclasspath compiler config),
  jstl-to-jakarta (9c), resources-binary-filtering (binary files under a filtered resource dir → the resources
  plugin's nonFilteredFileExtensions), strip-pom-repositories, jakarta-namespace-sweep (E13: javax.* EE references the
  recipes left behind → jakarta.*), spring-data-crud-repository (E14: ListCrudRepository for paging-only repositories)
"""
from __future__ import annotations

import posixpath
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from sbmig.core.config import Config
from sbmig.core.models import ProjectProfile

from .versions import version_lt

LOMBOK_FLOOR = "1.18.30"
MAPSTRUCT_FLOOR = "1.6.0"
COMPILER_FLOOR = "3.13.0"
SUREFIRE_FLOOR = "3.0.0"

HANDLER_IDS = (
    "lombok-version-floor", "mapstruct-version-floor", "querydsl-jakarta-classifier", "maven-compiler-plugin-floor",
    "surefire-plugin-floor", "javax-servlet-api-to-jakarta", "jaxb-runtime-add",
    "remove-classic-loader-implementation", "mysql-connector-relocation", "compiler-legacy-config",
    "jstl-to-jakarta", "resources-binary-filtering", "strip-pom-repositories", "jakarta-namespace-sweep",
    "spring-data-crud-repository", "dependency-relocation", "jakarta-library-floor",
)

# the handler's own table (read directly: importing sbmig.triage here would pull the whole triage package)
_JAKARTA_TABLE = Path(__file__).resolve().parent.parent / "triage" / "handlers" / "jakarta_packages.yml"


@lru_cache(maxsize=1)
def _jakarta_froms() -> tuple[str, ...]:
    try:
        doc = yaml.safe_load(_JAKARTA_TABLE.read_text(encoding="utf-8")) or {}
    except OSError:
        return ()
    return tuple(str(p["from"]) for p in doc.get("packages") or [])


def javax_ee_imports(profile: ProjectProfile) -> list[str]:
    """`profile.source.javax_imports` keys equal to / under a package of the jakarta table (sorted)."""
    froms = _jakarta_froms()
    return sorted(k for k, n in (profile.source.javax_imports or {}).items()
                  if n and any(k == f or k.startswith(f + ".") for f in froms))

# the dependency-relocation table (read directly for the same reason)
_RELOCATION_TABLE = Path(__file__).resolve().parent.parent / "triage" / "handlers" / "relocations.yml"
SERVLET_JAVAX_KEYS = ("javax.servlet:javax.servlet-api", "javax.servlet:servlet-api", "javax.servlet.jsp:javax.servlet.jsp-api",
                      "javax.servlet.jsp:jsp-api", "javax.servlet:jsp-api")


@lru_cache(maxsize=1)
def javax_api_relocation_keys() -> tuple[str, ...]:
    """`from` keys of the relocations.yml rows with `packages` (the javax → jakarta API rows) — the only rows
    dependency-relocation applies pre-emptively. Servlet / JSTL rows belong to their own handlers."""
    try:
        doc = yaml.safe_load(_RELOCATION_TABLE.read_text(encoding="utf-8")) or {}
    except OSError:
        return ()
    skip = set(SERVLET_JAVAX_KEYS) | set(JSTL_JAVAX_KEYS)
    return tuple(str(r["from"]) for r in doc.get("relocations") or []
                 if r.get("packages") and str(r.get("from")) not in skip)


def _dep_version(pom, version: Optional[str]) -> Optional[str]:
    m = re.fullmatch(r"\s*\$\{([^}]+)\}\s*", version or "")
    if m and m.group(1) in (pom.properties or {}):
        return pom.properties[m.group(1)]
    return version


def jakarta_library_reason(profile: ProjectProfile) -> Optional[str]:
    """Why `jakarta-library-floor` must run (None = nothing below its first jakarta release). Mirrors
    `sbmig.triage.handlers.jakarta_libraries_below` (not imported: the triage package is heavy)."""
    from sbmig.core.jakarta_libraries import jakarta_libraries

    hits: list[tuple[str, str, str]] = []
    for lib in jakarta_libraries():
        olds = [(d.key, _dep_version(p, d.version)) for p in profile.poms
                for d in [*p.dependencies, *p.dependency_management] if d.key in lib.old_keys]
        if olds:                                # earlier coordinates: below the jakarta release whatever the version
            k, v = next(((k, v) for k, v in olds if v), olds[0])
            hits.append((k, f"{v or 'unversioned'} → {lib.key}", lib.jakarta_since))
            continue
        vers = [_dep_version(p, d.version) for p in profile.poms for d in [*p.dependencies, *p.dependency_management]
                if d.key == lib.key]
        if not vers:
            continue
        low = [v for v in vers if version_lt(v, lib.jakarta_since) is True]
        if low:
            hits.append((lib.key, low[0] or "?", lib.jakarta_since))
        elif not any(version_lt(v, lib.jakarta_since) is False for v in vers):
            hits.append((lib.key, next((v for v in vers if v), "version unknown"), lib.jakarta_since))
    if not hits:
        return None
    keys = ", ".join(k for k, _, _ in hits)
    k, v, since = hits[0]
    return f"{keys} declared below their first jakarta release ({k} {v} < {since})"


JSTL_JAVAX_KEYS = ("javax.servlet:jstl", "jstl:jstl", "taglibs:standard", "org.apache.taglibs:taglibs-standard-impl",
                   "org.apache.taglibs:taglibs-standard-spec", "org.glassfish.web:javax.servlet.jsp.jstl",
                   "javax.servlet.jsp.jstl:jstl-api", "javax.servlet.jsp.jstl:javax.servlet.jsp.jstl-api")

_LEGACY_ARG = re.compile(r"-(bootclasspath|Xbootclasspath|extdirs|endorseddirs|source\b|target\b)")


def compiler_legacy_config_reason(profile: ProjectProfile) -> Optional[str]:
    """Why `compiler-legacy-config` must run (None = clean): javac 21 rejects -bootclasspath / -Xbootclasspath /
    -extdirs / -endorseddirs / -source / -target together with --release, and `<fork>` + `<executable>` point at a
    JDK that is not there any more. `PluginInfo.configuration` is flat (nested <compilerArguments> children keep only
    their values), so the presence of the container is enough to select the handler — it only removes what matches."""
    for p in profile.poms:
        for pl in p.plugins:
            if pl.artifact_id != "maven-compiler-plugin":
                continue
            c = pl.configuration or {}
            found: list[str] = []
            if str(c.get("fork", "")).strip().lower() == "true" or c.get("executable"):
                found.append("fork/executable")
            if c.get("compilerVersion"):
                found.append("compilerVersion")
            if "compilerArguments" in c:
                found.append("compilerArguments")
            for key in ("compilerArgument", "compilerArgs"):
                v = c.get(key)
                vals = v if isinstance(v, list) else ([v] if isinstance(v, str) else [])
                if any(_LEGACY_ARG.search(str(x)) for x in vals):
                    found.append(key)
            if found:
                return f"{p.path}: maven-compiler-plugin {', '.join(found)} — rejected by javac 21 with --release"
    return None


def _lib_version(profile: ProjectProfile, name: str) -> tuple[bool, Optional[str]]:
    for lib in profile.libraries:
        if lib.name == name:
            return True, lib.version
    return False, None


def _has_dep(profile: ProjectProfile, *keys: str) -> bool:
    return any(d.key in keys for p in profile.poms for d in [*p.dependencies, *p.dependency_management])


def pre_handler_reasons(profile: ProjectProfile, cfg: Config) -> list[tuple[str, str]]:
    """[(handler_id, reason)] in a stable order."""
    out: list[tuple[str, str]] = []
    present, v = _lib_version(profile, "lombok")
    if present and (v is None or version_lt(v, LOMBOK_FLOOR) in (True, None)):
        out.append(("lombok-version-floor", f"lombok {v or 'version unknown'} < {LOMBOK_FLOOR} (JDK 21 needs ≥ {LOMBOK_FLOOR})"))
    present, v = _lib_version(profile, "mapstruct")
    if present and version_lt(v, MAPSTRUCT_FLOOR) is True:
        out.append(("mapstruct-version-floor", f"mapstruct {v} < {MAPSTRUCT_FLOOR}"))
    if profile.has_library("querydsl"):
        out.append(("querydsl-jakarta-classifier", "querydsl present: needs the :jakarta classifier after javax→jakarta"))
    compilers = [pl for p in profile.poms for pl in p.plugins if pl.artifact_id == "maven-compiler-plugin"]
    if profile.poms:
        if not compilers:
            out.append(("maven-compiler-plugin-floor", f"maven-compiler-plugin not declared (need ≥ {COMPILER_FLOOR})"))
        else:
            low = [pl.version or "unversioned" for pl in compilers if version_lt(pl.version, COMPILER_FLOOR) is not False]
            if low:
                out.append(("maven-compiler-plugin-floor",
                            f"maven-compiler-plugin {', '.join(sorted(set(low)))} < {COMPILER_FLOOR}"))
    surefire = [pl for p in profile.poms for pl in p.plugins
                if pl.artifact_id in ("maven-surefire-plugin", "maven-failsafe-plugin")
                and version_lt(pl.version, SUREFIRE_FLOOR) is True]
    if surefire:
        out.append(("surefire-plugin-floor", "surefire/failsafe " + ", ".join(
            sorted({f"{pl.artifact_id}:{pl.version}" for pl in surefire})) + f" < {SUREFIRE_FLOOR}"))
    if _has_dep(profile, "javax.servlet:javax.servlet-api", "javax.servlet:servlet-api"):
        out.append(("javax-servlet-api-to-jakarta", "javax servlet-api dependency must become jakarta.servlet-api"))
    if profile.source.javax_imports.get("javax.xml.bind"):
        out.append(("jaxb-runtime-add", "javax.xml.bind imports: JAXB was removed from the JDK"))
    if _has_dep(profile, *JSTL_JAVAX_KEYS):
        out.append(("jstl-to-jakarta", "javax JSTL dependency must become jakarta.servlet.jsp.jstl (Boot-managed)"))
    api = [k for k in javax_api_relocation_keys() if _has_dep(profile, k)]
    if api:
        out.append(("dependency-relocation", f"javax API artifact(s) declared: {', '.join(api)} → jakarta coordinates "
                                             "after the spring recipes"))
    lib_reason = jakarta_library_reason(profile)
    if lib_reason:
        out.append(("jakarta-library-floor", lib_reason))
    ee = javax_ee_imports(profile)
    if ee:
        out.append(("jakarta-namespace-sweep", f"javax EE imports in the as-is sources ({', '.join(ee)}): rename what "
                                               "the recipes leave behind after the spring rewrite"))
    if any(lib.name.startswith("spring-data") or lib.name == "jpa" for lib in profile.libraries):
        out.append(("spring-data-crud-repository",
                    "Spring Data repositories present: PagingAndSortingRepository lost its CRUD methods in Spring Data 3"))
    for p in profile.poms:
        for pl in p.plugins:
            if pl.artifact_id == "spring-boot-maven-plugin" and \
                    str(pl.configuration.get("loaderImplementation", "")).upper() == "CLASSIC":
                out.append(("remove-classic-loader-implementation",
                            f"{p.path}: spring-boot-maven-plugin loaderImplementation=CLASSIC is gone in Boot 4"))
                break
        else:
            continue
        break
    if _has_dep(profile, "mysql:mysql-connector-java"):
        out.append(("mysql-connector-relocation", "mysql:mysql-connector-java relocated to com.mysql:mysql-connector-j"))
    legacy = compiler_legacy_config_reason(profile)
    if legacy:
        out.append(("compiler-legacy-config", legacy))
    binaries = list(getattr(profile.properties, "filtered_binary_files", None) or [])
    if binaries:
        exts = sorted({posixpath.basename(b).rsplit(".", 1)[-1].lower() for b in binaries if "." in posixpath.basename(b)})
        shown = ", ".join(exts[:6]) + (", …" if len(exts) > 6 else "")
        out.append(("resources-binary-filtering",
                    (f"{len(binaries)} binary file(s) under a filtered resource dir ({shown}; e.g. {binaries[0]}): "
                     "maven-resources-plugin ≥ 3.2 fails on them with MalformedInputException — "
                     "nonFilteredFileExtensions keeps filtering on")))
    if getattr(cfg.build, "pom_repositories", "keep") == "remove":
        urls: list[str] = []
        for p in profile.poms:
            urls += [u for u in getattr(p, "repositories", []) or [] if u not in urls]
        if urls:
            shown = ", ".join(urls[:4]) + (", …" if len(urls) > 4 else "")
            out.append(("strip-pom-repositories",
                        (f"build.pom_repositories=remove: {len(urls)} repository URL(s) declared in the poms ({shown}) "
                         "are removed before the first build")))
    return out


def pre_handlers_for(profile: ProjectProfile, cfg: Config) -> list[str]:
    """Handler ids to run pre-emptively (before the first build of a phase)."""
    return [hid for hid, _ in pre_handler_reasons(profile, cfg)]
