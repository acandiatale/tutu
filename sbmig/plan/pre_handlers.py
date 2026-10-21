"""Pre-emptive handler selection from the profile (handlers themselves live in sbmig.triage.handlers).

Handler ids (contract with workstream E):
  lombok-version-floor, mapstruct-version-floor, querydsl-jakarta-classifier, maven-compiler-plugin-floor,
  surefire-plugin-floor, javax-servlet-api-to-jakarta, jaxb-runtime-add, remove-classic-loader-implementation,
  mysql-connector-relocation, compiler-legacy-config (round 9: fork/executable/bootclasspath compiler config)
"""
from __future__ import annotations

import re
from typing import Optional

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
    "jstl-to-jakarta",
)

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
    return out


def pre_handlers_for(profile: ProjectProfile, cfg: Config) -> list[str]:
    """Handler ids to run pre-emptively (before the first build of a phase)."""
    return [hid for hid, _ in pre_handler_reasons(profile, cfg)]
