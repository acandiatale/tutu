"""Case classification (pure_java / servlet_tomcat / spring_legacy / spring_boot / msa) + MSA service discovery."""
from __future__ import annotations

import fnmatch
import posixpath
from typing import TYPE_CHECKING, Iterable, Optional

from sbmig.core.models import CaseType, LibraryUsage, PomInfo, ServiceInfo, SourceStats

from .libraries import detect_libraries
from .pom_parser import BOOT_GROUP, owning_pom, pom_dir
from .source_scanner import ScanResult

if TYPE_CHECKING:  # pragma: no cover
    from sbmig.core.models import ProjectProfile

SPRING_GROUPS = ("org.springframework", "org.springframework.security", "org.springframework.data",
                 "org.springframework.batch", "org.springframework.integration")
SPRING_STEREOTYPES = ("Controller", "RestController", "Configuration", "Service", "Repository", "Component")


def _all_deps(poms: Iterable[PomInfo]):
    for p in poms:
        yield from p.dependencies


def _signals(poms: list[PomInfo], source: SourceStats, libraries: list[LibraryUsage],
             scan: Optional[ScanResult] = None) -> dict[str, list[str]]:
    boot: list[str] = []
    spring: list[str] = []
    servlet: list[str] = []
    for p in poms:
        if p.parent and p.parent.group_id == BOOT_GROUP and p.parent.artifact_id.startswith("spring-boot"):
            boot.append(f"{p.path}: parent {p.parent.coordinates}")
        if any(d.group_id == BOOT_GROUP and d.artifact_id == "spring-boot-dependencies" for d in p.dependency_management):
            boot.append(f"{p.path}: imports spring-boot-dependencies BOM")
        if p.has_spring_boot_plugin:
            boot.append(f"{p.path}: spring-boot-maven-plugin")
        if p.packaging == "war":
            servlet.append(f"{p.path}: war packaging")
    boot_deps = sorted({d.key for d in _all_deps(poms) if d.group_id == BOOT_GROUP and d.artifact_id.startswith("spring-boot")})
    if boot_deps:
        boot.append("spring-boot dependencies: " + ", ".join(boot_deps[:5]))
    if source.main_classes:
        boot.append("@SpringBootApplication: " + ", ".join(source.main_classes[:5]))
    spring_deps = sorted({d.key for d in _all_deps(poms)
                          if d.group_id in SPRING_GROUPS and d.artifact_id.startswith("spring-")})
    if spring_deps:
        spring.append("spring dependencies: " + ", ".join(spring_deps[:5]))
    if source.xml_bean_configs:
        spring.append("xml bean configs: " + ", ".join(source.xml_bean_configs[:3]))
    spring_imports = False
    if scan is not None:
        spring_imports = any(i.startswith("org.springframework.") for j in scan.files for i in j.imports)
    stereo = {a: source.spring_annotations.get(a, 0) for a in SPRING_STEREOTYPES if source.spring_annotations.get(a)}
    if stereo and (spring_imports or scan is None):
        spring.append("spring stereotypes: " + ", ".join(f"@{k}×{v}" for k, v in stereo.items()))
    elif spring_imports:
        spring.append("org.springframework imports")
    servlet_imports = source.javax_imports.get("javax.servlet", 0) + source.jakarta_imports.get("jakarta.servlet", 0)
    if servlet_imports:
        servlet.append(f"servlet imports ×{servlet_imports}")
    if source.servlet_classes:
        servlet.append("servlets: " + ", ".join(source.servlet_classes[:3]))
    if source.has_web_xml:
        servlet.append("web.xml: " + ", ".join(source.web_xml_paths[:2]))
    if source.jsp_count:
        servlet.append(f"{source.jsp_count} JSP file(s)")
    return {"boot": boot, "spring": spring, "servlet": servlet}


def _conf(n: int, base: float) -> float:
    return round(min(0.99, base + 0.1 * max(0, n - 1)), 2)


def classify_single(poms: list[PomInfo], source: SourceStats, libraries: list[LibraryUsage],
                    scan: Optional[ScanResult] = None) -> tuple[CaseType, float, list[str]]:
    """Non-MSA classification. Precedence: Boot > legacy Spring > servlet > pure Java."""
    s = _signals(poms, source, libraries, scan)
    if s["boot"]:
        return CaseType.SPRING_BOOT, _conf(len(s["boot"]), 0.7), ["spring_boot: " + r for r in s["boot"]]
    if s["spring"]:
        reasons = ["spring_legacy (no Boot): " + r for r in s["spring"]]
        reasons += ["also servlet signals (Spring wins): " + r for r in s["servlet"]]
        return CaseType.SPRING_LEGACY, _conf(len(s["spring"]), 0.65), reasons
    if s["servlet"]:
        return CaseType.SERVLET_TOMCAT, _conf(len(s["servlet"]), 0.65), ["servlet_tomcat (no Spring): " + r
                                                                          for r in s["servlet"]]
    if source.java_files:
        return CaseType.PURE_JAVA, 0.8 if poms else 0.6, [
            f"pure_java: {source.java_files} java file(s), no Spring/servlet signals"]
    if poms:
        # pom-only aggregator / BOM / parent: classified by its poms; nothing to rewrite (S3)
        return CaseType.PURE_JAVA, 0.3, ["pure_java (low confidence): pom-only project, no java sources and no "
                                         "Spring/servlet signals in the poms"]
    return CaseType.UNKNOWN, 0.2, ["no java sources and no Spring/servlet signals"]


# ---------------------------------------------------------------------------
# MSA
# ---------------------------------------------------------------------------
def _contains(dir_: str, path: str) -> bool:
    return dir_ == "" or path == dir_ or path.startswith(dir_ + "/")


def _boot_app_modules(poms: list[PomInfo], scan: Optional[ScanResult]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if scan is None:
        return out
    for j in scan.files:
        if j.is_test or not j.is_boot_app:
            continue
        owner = owning_pom(j.path, poms)
        if owner is not None:
            out.setdefault(owner.path, []).append(j.fqcn)
    return out


def _looks_deployable(p: PomInfo, poms: list[PomInfo], app_modules: dict[str, list[str]]) -> bool:
    if p.packaging == "war" or p.has_spring_boot_plugin or p.path in app_modules:
        return True
    d = pom_dir(p.path)
    if p.packaging == "pom":
        return False
    return any(q.has_spring_boot_plugin and _contains(d, pom_dir(q.path)) for q in poms)


def msa_deployables(poms: list[PomInfo], scan: Optional[ScanResult]) -> tuple[list[PomInfo], list[str]]:
    """Return (deployable poms, reasons) when the repo looks like several deployable services, else ([], [])."""
    plugin_poms = [p for p in poms if p.has_spring_boot_plugin]
    if len(plugin_poms) >= 2:
        return plugin_poms, [f"msa: {len(plugin_poms)} poms with spring-boot-maven-plugin: "
                             + ", ".join(p.path for p in plugin_poms)]
    app_modules = _boot_app_modules(poms, scan)
    if len(app_modules) >= 2:
        by_path = {p.path: p for p in poms}
        return [by_path[k] for k in sorted(app_modules)], [
            "msa: @SpringBootApplication classes in different modules: "
            + ", ".join(f"{k} ({', '.join(v)})" for k, v in sorted(app_modules.items()))]
    roots = [p for p in poms if p.is_root and _looks_deployable(p, poms, app_modules)]
    if len(roots) >= 2:
        return roots, ["msa: independent root poms that each look deployable: " + ", ".join(p.path for p in roots)]
    return [], []


def service_roots(deployables: list[PomInfo], poms: list[PomInfo]) -> list[PomInfo]:
    """Walk up from each deployable pom while the ancestor directory pom contains no other deployable."""
    dep_dirs = [pom_dir(d.path) for d in deployables]
    by_dir = {pom_dir(p.path): p for p in poms}
    out: list[PomInfo] = []
    for d in deployables:
        cur = d
        dir_ = pom_dir(d.path)
        while dir_:
            dir_ = posixpath.dirname(dir_)
            anc = by_dir.get(dir_)
            if anc is None:
                continue
            if sum(1 for x in dep_dirs if _contains(dir_, x)) > 1:
                break
            cur = anc
        if cur not in out:
            out.append(cur)
    return out


def build_services(roots: list[PomInfo], poms: list[PomInfo], scan: ScanResult) -> list[ServiceInfo]:
    services: list[ServiceInfo] = []
    for r in roots:
        d = pom_dir(r.path)
        sub_poms = [p for p in poms if _contains(d, pom_dir(p.path))]
        sub_scan = scan.subset(d)
        stats = sub_scan.stats()
        libs = detect_libraries(sub_poms, sub_scan)
        case, _, _ = classify_single(sub_poms, stats, libs, sub_scan)
        name = posixpath.basename(d) if d else r.artifact_id
        services.append(ServiceInfo(
            name=name, root_pom=r.path, case_type=case, poms=[p.path for p in sub_poms],
            main_classes=stats.main_classes,
            spring_boot_version=r.spring_boot_version or next(
                (p.spring_boot_version for p in sub_poms if p.spring_boot_version), None),
            java_version=r.java_version or next((p.java_version for p in sub_poms if p.java_version), None),
        ))
    # disambiguate duplicate names
    seen: dict[str, int] = {}
    for s in services:
        if s.name in seen:
            seen[s.name] += 1
            s.name = f"{s.name}-{seen[s.name]}"
        else:
            seen[s.name] = 1
    return services


# ---------------------------------------------------------------------------
# MSA shared library roots (S1)
# ---------------------------------------------------------------------------
LIBRARY_CASE_MAP = {CaseType.SPRING_BOOT: CaseType.SPRING_LEGACY, CaseType.SERVLET_TOMCAT: CaseType.PURE_JAVA,
                    CaseType.UNKNOWN: CaseType.PURE_JAVA, CaseType.MSA: CaseType.SPRING_LEGACY}


def library_roots(poms: list[PomInfo], service_root_poms: list[str], scan: Optional[ScanResult]) -> list[PomInfo]:
    """Top-most in-repo poms of shared, non-deployable modules (e.g. `common/`) that live outside every service.

    A candidate is a jar-packaged pom without spring-boot-maven-plugin and without @SpringBootApplication, not inside
    a service directory; it is lifted to the highest ancestor directory pom that contains no service."""
    svc_dirs = [pom_dir(p) for p in service_root_poms]
    app_modules = _boot_app_modules(poms, scan)

    def in_service(d: str) -> bool:
        return any(_contains(sd, d) for sd in svc_dirs)

    def holds_service(d: str) -> bool:
        return any(_contains(d, sd) for sd in svc_dirs)

    by_dir = {pom_dir(p.path): p for p in poms}
    out: list[PomInfo] = []
    for p in poms:
        d = pom_dir(p.path)
        if p.packaging in ("pom", "war") or p.has_spring_boot_plugin or p.path in app_modules or in_service(d) \
                or holds_service(d):
            continue
        cur = p
        while d:
            d = posixpath.dirname(d)
            anc = by_dir.get(d)
            if anc is None:
                continue
            if holds_service(d) or anc.packaging == "war" or anc.has_spring_boot_plugin:
                break
            cur = anc
        if cur not in out:
            out.append(cur)
    return out


def build_library_services(roots: list[PomInfo], poms: list[PomInfo], scan: ScanResult,
                           taken: Iterable[str] = ()) -> list[ServiceInfo]:
    """Library roots as `ServiceInfo` entries (convention: `main_classes == []`, `spring_boot_version is None`,
    case mapped so a library never gets the Boot umbrella)."""
    out: list[ServiceInfo] = []
    names = set(taken)
    for r in roots:
        d = pom_dir(r.path)
        sub_poms = [p for p in poms if _contains(d, pom_dir(p.path))]
        sub_scan = scan.subset(d)
        case, _, _ = classify_single(sub_poms, sub_scan.stats(), detect_libraries(sub_poms, sub_scan), sub_scan)
        case = LIBRARY_CASE_MAP.get(case, case)
        name = posixpath.basename(d) if d else r.artifact_id
        base, i = name, 1
        while name in names:
            i += 1
            name = f"{base}-{i}"
        names.add(name)
        out.append(ServiceInfo(name=name, root_pom=r.path, case_type=case, kind="library", poms=[p.path for p in sub_poms],
                               main_classes=[], spring_boot_version=None,
                               java_version=r.java_version or next((p.java_version for p in sub_poms
                                                                    if p.java_version), None)))
    return out


def is_library_root(svc: ServiceInfo, profile: Optional["ProjectProfile"] = None) -> bool:
    """True for MSA `services` entries that are shared library roots (not deployables).

    Convention (no dedicated model field): `main_classes == []` and `spring_boot_version is None`; with `profile`
    given, additionally its poms must be known to `profile.poms` and none of them is a WAR or carries spring-boot-maven-plugin (so a plain WAR service
    without Boot is never mistaken for a library). Always pass the profile when you have it."""
    if getattr(svc, "kind", "service") == "library":      # explicit model field (set by the analyzer)
        return True
    if svc.main_classes or svc.spring_boot_version is not None:
        return False
    if profile is None:
        return True
    by_path = {p.path: p for p in profile.poms}
    sub = [by_path[x] for x in (svc.poms or [svc.root_pom]) if x in by_path]
    if not sub:          # poms unknown to the profile → cannot prove it is a library; treat as a deployable
        return False
    return not any(p.packaging == "war" or p.has_spring_boot_plugin for p in sub)


def library_services(profile: "ProjectProfile") -> list[ServiceInfo]:
    return [s for s in profile.services if is_library_root(s, profile)]


def deployable_services(profile: "ProjectProfile") -> list[ServiceInfo]:
    return [s for s in profile.services if not is_library_root(s, profile)]


def classify(poms: list[PomInfo], source: SourceStats, libraries: list[LibraryUsage],
             scan: Optional[ScanResult] = None) -> tuple[CaseType, float, list[str]]:
    """Classify the whole repo. `scan` (per-file facts) enables the module-aware MSA rule."""
    deployables, reasons = msa_deployables(poms, scan)
    if deployables:
        roots = service_roots(deployables, poms)
        if len(roots) >= 2:
            conf = 0.9 if "spring-boot-maven-plugin" in reasons[0] else 0.75
            return CaseType.MSA, conf, reasons + [f"msa: services rooted at {', '.join(r.path for r in roots)}"]
    return classify_single(poms, source, libraries, scan)


# ---------------------------------------------------------------------------
# Boot modules by the company naming rule (round 9c)
# ---------------------------------------------------------------------------
def _pom_module_path(p: PomInfo, m: str) -> str:
    target = posixpath.normpath(posixpath.join(pom_dir(p.path) or ".", m))
    if not target.endswith(".xml"):
        target = posixpath.join(target, "pom.xml")
    return posixpath.normpath(target)


def _module_dirs(start: PomInfo, by_path: dict[str, PomInfo]) -> list[str]:
    """Dirs of every pom reachable from `start` through <modules> (recursively), `start` itself excluded."""
    out: list[str] = []
    seen = {start.path}
    stack = [start]
    while stack:
        p = stack.pop(0)
        for m in p.modules:
            child = by_path.get(_pom_module_path(p, m))
            if child is None or child.path in seen:
                continue
            seen.add(child.path)
            out.append(pom_dir(child.path))
            stack.append(child)
    return out


def boot_module_findings(poms: list[PomInfo], root_pom: Optional[PomInfo], scan: Optional[ScanResult],
                         pattern: str, services: Iterable[ServiceInfo] = ()) -> tuple[list[str], list[str], list[str]]:
    """`(boot_modules, warnings, reasons)` for `ProjectProfile.boot_modules`.

    Candidates are the module dirs reachable from the root pom's `<modules>` (recursively); for MSA also every
    service root dir and its modules. A candidate is a boot module when its basename matches `pattern` (fnmatch,
    e.g. `*-boot`) AND its pom is a boot app (spring-boot-maven-plugin, or a @SpringBootApplication class under its
    `src/main/java`). Boot apps with another name and pattern-named modules without a boot pom are warnings only.
    The project root itself is never a candidate (a single-module Boot app is wrapped by the restructure)."""
    by_path = {p.path: p for p in poms}
    by_dir = {pom_dir(p.path): p for p in poms}
    cands: list[str] = []

    def add(d: str) -> None:
        if d and d not in cands and not d.startswith(".."):
            cands.append(d)

    starts: list[PomInfo] = [root_pom] if root_pom is not None else [p for p in poms if p.is_root]
    for s in starts:
        for d in _module_dirs(s, by_path):
            add(d)
    for svc in services:
        sp = by_path.get(svc.root_pom)
        if sp is None:
            continue
        add(pom_dir(sp.path))
        for d in _module_dirs(sp, by_path):
            add(d)
    app_dirs: set[str] = set()
    if scan is not None:
        for j in scan.files:
            if j.is_test or not j.is_boot_app:
                continue
            for d in cands:
                if j.path.startswith(d + "/src/main/java/"):
                    app_dirs.add(d)
    boot: list[str] = []
    warnings: list[str] = []
    reasons: list[str] = []
    for d in sorted(cands):
        p = by_dir.get(d)
        if p is None:
            continue
        is_app = p.has_spring_boot_plugin or d in app_dirs
        named = fnmatch.fnmatch(posixpath.basename(d), pattern)
        if named and is_app:
            boot.append(d)
        elif is_app:
            msg = f"boot module {d} is not named {pattern} (company naming rule)"
            warnings.append(msg)
            reasons.append(msg)
        elif named:
            warnings.append(f"{d} is named like a boot module but its pom is not a Boot app")
    if boot:
        reasons.append(f"boot modules ({pattern}): " + ", ".join(boot))
    return boot, warnings, reasons
