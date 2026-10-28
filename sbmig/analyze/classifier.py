"""케이스 분류 — 회사의 네 가지 프로젝트 유형(pure_java / servlet_tomcat / spring_legacy /
spring_boot)과 MSA (배포 서비스 여럿)를 pom·소스 신호로 판정하고, MSA 면 서비스 루트와 공유 라이브러리 루트를
찾는다. 흐름상 위치: 분석기(``analyze_project``)가 부르고, 결과 ``case_type`` 이 계획(레시피 세트: Boot 만
``UpgradeSpringBoot_4_0``, jakarta 는 Boot 가 아닐 때)과 재구성(boot / back / front 분리냐 pom 만이냐)을
정한다. 핵심 진입점: :func:`classify` (전체), :func:`classify_single`
(비-MSA, 우선순위 Boot > Spring > servlet > 순수
Java), :func:`msa_deployables` / :func:`service_roots` / :func:`library_roots`, :func:`is_library_root`,
:func:`boot_module_findings` (회사 이름 규칙 ``layout.boot_module_pattern``
의 boot 모듈, round 9c). 불변조건: 라이브러리 루트는 절대 SPRING_BOOT 가 되지 않는다(``LIBRARY_CASE_MAP``) —
Boot 레시피를 받으면 안 된다.

Case classification (pure_java / servlet_tomcat / spring_legacy / spring_boot / msa) + MSA service discovery."""
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
    """모든 pom 의 ``<dependencies>`` (관리 의존성 제외)."""
    for p in poms:
        yield from p.dependencies


def _signals(poms: list[PomInfo], source: SourceStats, libraries: list[LibraryUsage],
             scan: Optional[ScanResult] = None) -> dict[str, list[str]]:
    """분류 근거를 boot / spring / servlet 세 갈래로 모은다(각 항목 = 보고서 ``reasons`` 에 실리는
    한 줄). Boot: Boot 부모·BOM·플러그인·의존성·@SpringBootApplication. Spring: spring-* 의존성·XML
    빈 설정·스테레오타입. servlet: war 패키징·servlet import·서블릿 클래스·web.xml·JSP."""
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
    # 스테레오타입(@Service 등)은 이름만 같은 다른 어노테이션일 수 있어서, 파일별 스캔이 있으면
    # org.springframework import 가 실제로 있을 때만 Spring 신호로 센다
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
    """신뢰도: 기본값 + 근거 하나 늘 때마다 0.1, 최대 0.99."""
    return round(min(0.99, base + 0.1 * max(0, n - 1)), 2)


def classify_single(poms: list[PomInfo], source: SourceStats, libraries: list[LibraryUsage],
                    scan: Optional[ScanResult] = None) -> tuple[CaseType, float, list[str]]:
    """비-MSA 분류. 우선순위 Boot > 레거시 Spring > servlet > 순수 Java (앞의 신호가 하나라도
    있으면 그 케이스). 소스 없이 pom 만 있으면 낮은 신뢰도의 pure_java, 아무것도 없으면 UNKNOWN.

    Non-MSA classification. Precedence: Boot > legacy Spring > servlet > pure Java."""
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
    """``path`` 가 디렉토리 ``dir_`` 안(또는 같은 곳)인지. ``dir_ == ""`` (루트)는 모든 것을 포함한다."""
    return dir_ == "" or path == dir_ or path.startswith(dir_ + "/")


def _boot_app_modules(poms: list[PomInfo], scan: Optional[ScanResult]) -> dict[str, list[str]]:
    """@SpringBootApplication 클래스(테스트 제외)를 가진 모듈 pom 경로 → 그 클래스 FQCN 목록."""
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
    """배포 단위로 보이는지: war, Boot 플러그인, Boot 앱 클래스,
    또는 (pom 패키징이 아니면서) 하위에 Boot 플러그인 pom."""
    if p.packaging == "war" or p.has_spring_boot_plugin or p.path in app_modules:
        return True
    d = pom_dir(p.path)
    if p.packaging == "pom":
        return False
    return any(q.has_spring_boot_plugin and _contains(d, pom_dir(q.path)) for q in poms)


def msa_deployables(poms: list[PomInfo], scan: Optional[ScanResult]) -> tuple[list[PomInfo], list[str]]:
    """저장소가 배포 서비스 여럿(MSA)으로 보이면 (배포 pom 목록, 근거), 아니면 ([], []).
    판정 순서: Boot 플러그인 pom 2개 이상 → 서로 다른 모듈의 @SpringBootApplication 2개 이상 → 각자 배포
    가능해 보이는 독립 루트 pom 2개 이상.

    Return (deployable poms, reasons) when the repo looks like several deployable services, else ([], [])."""
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
    """각 배포 pom 에서 위로 올라가며, 다른 배포 단위를 품지 않는 가장 높은 조상 pom 을 서비스 루트로 삼는다.

    Walk up from each deployable pom while the ancestor directory pom contains no other deployable."""
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
    """서비스 루트마다 ServiceInfo 를 만든다: 하위 pom·소스만으로 다시
    ``classify_single`` 해서 서비스별 케이스를 정하고, 이름은 디렉토리명(같은 이름이
    또 있으면 ``-2``, ``-3`` …). MSA 는 서비스마다 같은 회사 규칙이 적용된다."""
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
    # 같은 이름의 두 번째부터 ``-2``, ``-3`` … 을 붙이되, 원래부터 그 이름인 서비스(예: 디렉토리 ``a-2``)나 앞에서 만든
    # 이름과 겹치지 않을 때까지 번호를 올린다(A25 — 예전에는 ``a``, ``a``, ``a-2`` 가 ``a-2`` 두 개가 됐다). 순서는 그대로
    taken = {s.name for s in services}
    seen: set[str] = set()
    for s in services:
        if s.name in seen:
            n = 2
            while f"{s.name}-{n}" in taken:
                n += 1
            s.name = f"{s.name}-{n}"
            taken.add(s.name)
        seen.add(s.name)
    return services


# ---------------------------------------------------------------------------
# MSA shared library roots (S1)
# ---------------------------------------------------------------------------
# 공유 라이브러리 루트의 케이스 바꿔치기: 라이브러리는 배포 단위가 아니므로 Boot(→ spring_legacy)나 servlet(→
# pure_java)으로 두지 않는다 — Boot 레시피·웹 모듈 분리가 라이브러리에 적용되면 안 된다
LIBRARY_CASE_MAP = {CaseType.SPRING_BOOT: CaseType.SPRING_LEGACY, CaseType.SERVLET_TOMCAT: CaseType.PURE_JAVA,
                    CaseType.UNKNOWN: CaseType.PURE_JAVA, CaseType.MSA: CaseType.SPRING_LEGACY}


def library_roots(poms: list[PomInfo], service_root_poms: list[str], scan: Optional[ScanResult]) -> list[PomInfo]:
    """어느 서비스에도 속하지 않는 공유 비배포 모듈(예: ``common/``)의 최상위 pom 들
    (MSA 의 라이브러리 루트, S1). 후보는 jar 패키징·Boot 플러그인 없음·Boot 앱 클래스 없음·서비스 밖인 pom
    이고, 서비스를 품지 않는 가장 높은 조상 pom 까지 끌어올린다.

    Top-most in-repo poms of shared, non-deployable modules (e.g. `common/`) that live outside every service.

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
    """라이브러리 루트를 ServiceInfo(``kind="library"``, ``main_classes == []``,
    ``spring_boot_version None``)로 만든다. 케이스는 ``LIBRARY_CASE_MAP`` 으로 바꿔 Boot 가 되지 않게 하고,
    이름은 서비스 이름과 겹치지 않게 한다.

    Library roots as `ServiceInfo` entries (convention: `main_classes == []`, `spring_boot_version is None`,
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
    """MSA ``services`` 항목이 배포 서비스가 아니라 공유 라이브러리 루트이면 True. ``kind == "library"``
    가 우선이고, 옛 기록은 관례(main_classes 없음 + Boot 버전 없음 + WAR / Boot 플러그인 없음)로 판정한다.
    프로필을 알면 꼭 넘길 것 — 그래야 Boot 없는 WAR 서비스를 라이브러리로 오인하지 않는다.

    True for MSA `services` entries that are shared library roots (not deployables).

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
    """프로필의 ``services`` 중 공유 라이브러리 루트만."""
    return [s for s in profile.services if is_library_root(s, profile)]


def deployable_services(profile: "ProjectProfile") -> list[ServiceInfo]:
    """프로필의 ``services`` 중 배포 서비스만(라이브러리 루트 제외)."""
    return [s for s in profile.services if not is_library_root(s, profile)]


def classify(poms: list[PomInfo], source: SourceStats, libraries: list[LibraryUsage],
             scan: Optional[ScanResult] = None) -> tuple[CaseType, float, list[str]]:
    """저장소 전체 분류: 배포 단위가 둘 이상이고 서비스 루트도 둘
    이상으로 갈리면 MSA, 아니면 :func:`classify_single`. ``scan`` (파일별 정보)이 있어야 모듈 단위
    @SpringBootApplication 규칙이 동작한다.

    Classify the whole repo. `scan` (per-file facts) enables the module-aware MSA rule."""
    deployables, reasons = msa_deployables(poms, scan)
    if deployables:
        roots = service_roots(deployables, poms)
        # 서비스 루트로 끌어올린 결과가 둘 이상일 때만 MSA — 아니면 아래의 단일 프로젝트 분류로 넘어간다
        if len(roots) >= 2:
            conf = 0.9 if "spring-boot-maven-plugin" in reasons[0] else 0.75
            return CaseType.MSA, conf, reasons + [f"msa: services rooted at {', '.join(r.path for r in roots)}"]
    return classify_single(poms, source, libraries, scan)


# ---------------------------------------------------------------------------
# Boot modules by the company naming rule (round 9c)
# ---------------------------------------------------------------------------
def _pom_module_path(p: PomInfo, m: str) -> str:
    """``<module>`` 값을 루트 기준 pom 경로로(디렉토리면 ``/pom.xml`` 을 붙인다)."""
    target = posixpath.normpath(posixpath.join(pom_dir(p.path) or ".", m))
    if not target.endswith(".xml"):
        target = posixpath.join(target, "pom.xml")
    return posixpath.normpath(target)


def _module_dirs(start: PomInfo, by_path: dict[str, PomInfo]) -> list[str]:
    """``start`` 에서 ``<modules>`` 를 따라(재귀) 닿는 모든 pom 의 디렉토리. ``start`` 자신은 뺀다.

    Dirs of every pom reachable from `start` through <modules> (recursively), `start` itself excluded."""
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
    """``ProjectProfile.boot_modules`` 용 ``(boot 모듈, 경고, 근거)`` (회사 레이아웃 round 9c).
    boot 모듈 = 이름이 ``pattern`` (예: ``*-boot``)에 맞고 **그리고** pom 이 Boot 앱(플러그인 또는
    ``src/main/java`` 아래 @SpringBootApplication)인 모듈. 둘 중 하나만 맞으면 경고만 남긴다(차단 없음).
    프로젝트 루트는 후보가 아니다 — 단일 모듈 Boot 앱은 재구성이 ``{name}-boot`` 로 감싼다.

    `(boot_modules, warnings, reasons)` for `ProjectProfile.boot_modules`.

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
