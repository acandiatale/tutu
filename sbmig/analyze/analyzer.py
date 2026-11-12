"""분석 단계의 실제 처리를 하는 모듈.
pom 구조, 소스 스캔, 라이브러리 탐지, 프로젝트 유형 분류 결과를 합쳐 ProjectProfile을 만든다.
오케스트레이터의 ``step_analyze``가 호출하고, 결과인 ``profile.json``은 이후 단계와 보고서가 모두 읽는다.
- 진입점은 :func:`analyze_project`(실행 작업공간)와 :func:`analyze_path` + :class:`PathView`(폴더를 그대로 분석)다
- 특이 사항이 있어도 실행을 막지 않고 ``profile.warnings``와 ``LibraryUsage``에만 남긴다
- MSA 프로젝트는 ``profile.services``에 배포 서비스를 먼저, 공유 라이브러리 루트(``main_classes == []``)를 뒤에 넣는다

`analyze_project(ws, cfg) -> ProjectProfile`: pom graph + source scan + libraries + classification.

Edge cases (docs/ERROR-HANDLING.md) surface as `profile.warnings` plus `LibraryUsage` entries:
`system-scope-jars` (E7), `kotlin` / `groovy` / `scala` / `codegen:<plugin artifactId>` (X6), `legacy-encoding` (E1),
pom-only projects (S3), Gradle / Ant (S4: `warnings[0]` explains, `build_tool != "maven"`), Java ≤ 1.7 levels.

MSA library-root convention (S1): `profile.services` lists the deployables **and** the shared library roots
(in-repo modules outside every service without spring-boot-maven-plugin / @SpringBootApplication, e.g. `common/`).
A library root is a `ServiceInfo` with `main_classes == []` and `spring_boot_version is None`, `name` = its
directory name, `case_type` PURE_JAVA or SPRING_LEGACY (never SPRING_BOOT). Library entries come after the
deployables; use `sbmig.analyze.is_library_root(svc, profile)` / `library_services(profile)` to tell them apart.


"""
from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Optional

from sbmig.core.config import ArtifactSpec, Config
from sbmig.core.models import GAV, CaseType, PomInfo, ProjectProfile
from sbmig.core.workspace import is_build_output_path

from .build_tool import detect_build_tool
from .classifier import (boot_module_findings, build_library_services, build_services, classify, library_roots,
                         msa_deployables, service_roots)
from .edges import (encoding_findings, java_level_findings, language_findings, normalize_java_version,
                    system_dep_findings, system_dependencies)
from .libraries import detect_libraries
from .pom_parser import WorkspaceView, module_order, parse_poms
from .properties import load_rules, scan_properties
from .source_scanner import scan_sources

log = logging.getLogger("sbmig.analyze")


class PathView:
    """폴더를 복사하지 않고 읽기만 하는 Workspace 대용 클래스. ``analyze_path``가 쓴다.

    Read-only Workspace-like view over a directory (no copy, no git, no run dir)."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.ignored_dirs: set[str] = set()      # v2.1z-h: like Workspace.ignored_dirs (stray pom copies)

    def is_ignored(self, rel: str) -> bool:
        """``rel``이 무시할 폴더(``ignored_dirs``) 안에 있는지 확인한다."""
        r = str(rel).replace("\\", "/").strip("/")
        return any(r == d or r.startswith(d + "/") for d in self.ignored_dirs if d)

    def abs(self, rel: str | Path) -> Path:
        """상대 경로를 루트 기준 절대 경로로 바꾼다. 이미 절대 경로면 그대로 반환한다."""
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    def rel(self, path: str | Path) -> str:
        """경로를 루트 기준 posix 상대 경로로 바꾼다. 루트 밖 경로면 원래 문자열을 반환한다."""
        p = Path(path)
        try:
            return p.resolve().relative_to(self.root).as_posix() if p.is_absolute() else p.as_posix()
        except ValueError:
            return str(p)

    def exists(self, rel: str | Path) -> bool:
        """파일이나 디렉토리가 있는지 확인한다."""
        return self.abs(rel).exists()

    def read(self, rel: str | Path, encoding: str = "utf-8") -> str:
        """파일을 텍스트로 읽는다.
        인코딩이 깨져도 멈추지 않도록 디코딩하지 못한 바이트는 대체 문자로 바꾼다."""
        return self.abs(rel).read_text(encoding=encoding, errors="replace")

    def glob(self, pattern: str) -> list[str]:
        """패턴에 맞는 상대 경로 목록을 반환한다.
        ``.git``과 pom 옆의 ``target/``은 빼지만, ``target``이라는 이름의 자바 패키지는 남긴다."""
        return sorted(r for r in (self.rel(p) for p in self.root.rglob(pattern)
                                  if ".git" not in p.relative_to(self.root).parts
                                  and not is_build_output_path(p.relative_to(self.root), self.root))  # a `target` package stays
                      if not self.is_ignored(r))

    def java_files(self) -> list[str]:
        """빌드 출력을 뺀 모든 ``*.java`` 파일을 반환한다."""
        return self.glob("*.java")

    def pom_files(self) -> list[str]:
        """빌드 출력을 뺀 모든 ``pom.xml`` 파일을 반환한다."""
        return self.glob("pom.xml")


def _artifact_hits(poms: list[PomInfo], keys: dict[str, ArtifactSpec]) -> list[GAV]:
    """pom에서 사내 아티팩트(``keys``) 선언을 모은다.
    같은 아티팩트가 여러 번 나오면 버전이 있는 쪽을 남긴다."""
    out: dict[str, GAV] = {}
    for p in poms:
        for d in [*p.dependencies, *p.dependency_management]:
            if d.key in keys and (d.key not in out or (not out[d.key].version and d.version)):
                out[d.key] = GAV(group_id=d.group_id, artifact_id=d.artifact_id, version=d.version,
                                 scope=d.scope, type=d.type, classifier=d.classifier)
    return [out[k] for k in sorted(out)]


def _mandatory_keys(cfg: Config) -> dict[str, ArtifactSpec]:
    """필수 사내 아티팩트의 키와 별칭으로 스펙을 찾는 표를 만든다.
    이전 좌표로 선언된 것도 이미 있는 것으로 본다."""
    out: dict[str, ArtifactSpec] = {}
    for spec in cfg.artifacts.mandatory:
        out[spec.key] = spec
        for alias in spec.aliases:
            out[alias] = spec
    return out


def _pick(values: list[Optional[str]]) -> Optional[str]:
    """빈 값을 빼고 가장 많이 나온 값을 반환한다. 값이 없으면 None을 반환한다."""
    vals = [v for v in values if v]
    if not vals:
        return None
    return Counter(vals).most_common(1)[0][0]


def analyze_project(ws: WorkspaceView, cfg: Config) -> ProjectProfile:
    """프로젝트를 분석해 ProjectProfile을 반환한다. 프로젝트 파일은 바꾸지 않는다.
    Maven 프로젝트가 아니면 ``warnings[0]``에 이유를 넣어 반환하고, 오케스트레이터가 ``build_tool``을 보고 중단한다."""
    root = Path(ws.root)
    poms, warnings = parse_poms(ws)
    order = module_order(poms)
    by_path = {p.path: p for p in poms}
    poms = [by_path[p] for p in order]

    scan = scan_sources(ws, poms)
    source = scan.stats()
    libraries = detect_libraries(poms, scan)

    # build tool (S4): the explanation must be warnings[0] — the orchestrator aborts on build_tool != "maven"
    # (it already rejects non-Maven builds at intake with the same detection; this is the fallback)
    det = detect_build_tool(ws)
    build_tool = det.tool
    if det.tool != "maven":
        warnings.insert(0, det.reason)
    elif "ignored" in det.reason:
        warnings.append(det.reason)
    if source.java_files == 0:
        warnings.append("no java sources found")
        if poms:
            warnings.append("pom-only project (aggregator / BOM / parent): no OpenRewrite or build-fix steps are "
                            "planned, only pom edits and the final verify")
    lang_warn, lang_libs = language_findings(ws, poms)
    warnings += lang_warn
    sys_warn, sys_lib = system_dep_findings(system_dependencies(ws, poms)) if poms else ([], None)
    warnings += sys_warn
    enc_warn, enc_lib = encoding_findings(ws, poms, [j.path for j in scan.files]) if scan.files else ([], None)
    warnings += enc_warn
    warnings += java_level_findings(poms)
    libraries = libraries + lang_libs + [x for x in (sys_lib, enc_lib) if x is not None]

    case, confidence, reasons = classify(poms, source, libraries, scan)
    services = []
    # 계획 단계가 공유 라이브러리 루트를 먼저 변환하므로 배포 서비스와 구분해 뒤에 붙인다
    if case == CaseType.MSA:
        deployables, _ = msa_deployables(poms, scan)
        roots = service_roots(deployables, poms)
        services = build_services(roots, poms, scan)
        libs = build_library_services(library_roots(poms, [r.path for r in roots], scan), poms, scan,
                                      taken=[s.name for s in services])
        if libs:
            reasons.append("msa: shared library roots (migrated before the services): "
                           + ", ".join(f"{s.name} ({s.root_pom})" for s in libs))
        services += libs

    root_pom = by_path.get("pom.xml")
    # 루트에 pom.xml이 없으면 is_root인 pom이 하나일 때만 루트로 본다.
    # 여럿이면 폴더 이름을 프로젝트 이름으로 쓴다
    if root_pom is None:
        roots = [p for p in poms if p.is_root]
        root_pom = roots[0] if len(roots) == 1 else None
    name = root_pom.artifact_id if root_pom else root.name

    # round 9c: boot modules by the company naming rule (layout.boot_module_pattern), confirmed by their pom
    boot_modules, bm_warn, bm_reasons = boot_module_findings(
        poms, root_pom, scan, cfg.layout.boot_module_pattern,
        [s for s in services if s.kind == "service"] if case == CaseType.MSA else ())
    warnings += bm_warn
    reasons += bm_reasons

    # 루트 pom 값을 우선하고, 없으면 모듈 중 가장 많이 쓰는 Java 버전과 처음 보이는 Boot / Spring 버전을 쓴다
    java_version = normalize_java_version(
        (root_pom.java_version if root_pom else None) or _pick([p.java_version for p in poms]))
    boot_version = (root_pom.spring_boot_version if root_pom else None) or next(
        (p.spring_boot_version for p in poms if p.spring_boot_version), None)
    spring_version = (root_pom.spring_version if root_pom else None) or next(
        (p.spring_version for p in poms if p.spring_version), None)

    profile = ProjectProfile(
        root=str(root),
        name=name,
        case_type=case,
        confidence=confidence,
        reasons=reasons,
        build_tool=build_tool,  # type: ignore[arg-type]
        poms=poms,
        root_pom=root_pom,
        source=source,
        libraries=libraries,
        java_version=java_version,
        spring_boot_version=boot_version,
        spring_version=spring_version,
        packaging=root_pom.packaging if root_pom else None,
        is_multi_module=len(poms) > 1,
        services=services,
        optional_artifacts_in_use=_artifact_hits(poms, cfg.optional_keys()),
        mandatory_already_present=_artifact_hits(poms, _mandatory_keys(cfg)),
        warnings=warnings,
        boot_modules=boot_modules,
        fingerprint=ProjectProfile.make_fingerprint([p.path for p in poms],
                                                    root_pom.artifact_id if root_pom else root.name),
    )
    # ADF 근거가 하나라도 있으면 요약을 프로필에 남긴다. 두 계열 이상이면 오케스트레이터가 실행을 중단한다
    # v2.1z-d: ADF detection; the verdict + sizing go on the profile whenever an evidence family is present
    try:
        from .adf import detect_adf
        adf = detect_adf(ws)
        if adf.present:
            profile.adf = adf.summary()
    except Exception as e:  # noqa: BLE001 - the analyzer must never fail on a sizing
        log.warning("ADF detection skipped: %s: %s", type(e).__name__, e)
    # 트리에 있지만 어떤 pom도 선언하지 않은 jar를 프로필에 기록한다. 이 검사로 분석이 실패하지는 않는다
    # v2.1z-i: bundled jars (in the tree, declared by no pom); a profile fact, never fails the analysis
    if getattr(getattr(cfg, "intake", None), "bundled_jars", True):
        try:
            from .bundled_jars import scan_bundled_jars
            profile.bundled_jars = scan_bundled_jars(ws, cfg, poms)
        except Exception as e:  # noqa: BLE001 - the analyzer must never fail on the bundled-jar scan
            log.warning("bundled-jar scan skipped: %s: %s", type(e).__name__, e)
    if cfg.externalized.enabled:
        # round 8: settings the code reads that only the external container supplied (JNDI, context-param, -D …)
        rules = load_rules(cfg.resolve_path(cfg.externalized.rules_file))
        # round 9: MSA → per-service inventory over the deployables (library roots are not services)
        deployable = [s for s in services if s.kind == "service"] if case == CaseType.MSA else None
        profile.properties = scan_properties(ws, poms, scan, rules, cfg, services=deployable or None)
    log.info("analyzed %s: case=%s confidence=%.2f poms=%d java=%d", name, case.value, confidence,
             len(poms), source.java_files)
    return profile


def analyze_path(path: str | Path, cfg: Config) -> ProjectProfile:
    """폴더를 실행 작업공간에 복사하지 않고 그 자리에서 읽기 전용으로 분석한다.

    Analyze a directory in place (no copy into a run workspace)."""
    return analyze_project(PathView(path), cfg)
