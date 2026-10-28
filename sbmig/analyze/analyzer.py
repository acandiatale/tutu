"""분석(analyze) 단계의 본체 — ``analyze_project(ws, cfg)`` 가 pom 그래프·소스 스캔·라이브러리·
케이스 분류를 합쳐 ProjectProfile(프로필)을 만든다. 흐름상 위치: 접수 다음 오케스트레이터 ``step_analyze`` 가
부르고, 결과 ``profile.json`` 을 계획(plan)·재구성(restructure)·구조 검토·보고서가 모두 읽는다. 핵심 진입점:
:func:`analyze_project` (실행 작업공간), :func:`analyze_path` + :class:`PathView` (복사 없이 폴더 그대로 — CLI
``sbmig analyze`` / 플릿). 특이 사항(system scope jar, Kotlin/Groovy, 레거시 인코딩, Java ≤ 1.7, pom 만 있는
프로젝트)은 실행을 막지 않고 ``profile.warnings`` + ``LibraryUsage`` 로만 남긴다(차단 정책 없음). MSA 는
``profile.services`` 에 배포 서비스 다음에 공유 라이브러리 루트(``main_classes == []``)를 붙인다.

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
    """폴더를 복사하지 않고 읽기만 하는 Workspace 흉내 객체(git·실행 디렉토리 없음). ``analyze_path`` 가 쓴다.

    Read-only Workspace-like view over a directory (no copy, no git, no run dir)."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def abs(self, rel: str | Path) -> Path:
        """상대 경로 → 루트 기준 절대 경로(이미 절대면 그대로)."""
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    def rel(self, path: str | Path) -> str:
        """경로 → 루트 기준 posix 상대 경로(루트 밖이면 원래 문자열)."""
        p = Path(path)
        try:
            return p.resolve().relative_to(self.root).as_posix() if p.is_absolute() else p.as_posix()
        except ValueError:
            return str(p)

    def exists(self, rel: str | Path) -> bool:
        """파일 / 디렉토리가 있는지."""
        return self.abs(rel).exists()

    def read(self, rel: str | Path, encoding: str = "utf-8") -> str:
        """텍스트로 읽는다(디코딩 못 하는 바이트는 대체 문자 — 분석은 깨진 인코딩에도 멈추지 않는다)."""
        return self.abs(rel).read_text(encoding=encoding, errors="replace")

    def glob(self, pattern: str) -> list[str]:
        """``.git`` 과 Maven 빌드 출력(pom 옆 ``target/``)을 뺀 상대 경로 목록.
        ``target`` 이라는 이름의 자바 패키지는 남긴다."""
        return sorted(self.rel(p) for p in self.root.rglob(pattern)
                      if ".git" not in p.relative_to(self.root).parts
                      and not is_build_output_path(p.relative_to(self.root), self.root))   # a `target` package stays

    def java_files(self) -> list[str]:
        """모든 ``*.java`` (빌드 출력 제외)."""
        return self.glob("*.java")

    def pom_files(self) -> list[str]:
        """모든 ``pom.xml`` (빌드 출력 제외)."""
        return self.glob("pom.xml")


def _artifact_hits(poms: list[PomInfo], keys: dict[str, ArtifactSpec]) -> list[GAV]:
    """pom 들의 dependencies / dependencyManagement 에서 ``keys`` (사내 아티팩트) 에 해당하는 것을 모은다.
    같은 키가 여러 번 나오면 버전이 적힌 쪽을 남긴다(선택 아티팩트는 "이미 쓰는 것만 올린다" 의 근거)."""
    out: dict[str, GAV] = {}
    for p in poms:
        for d in [*p.dependencies, *p.dependency_management]:
            if d.key in keys and (d.key not in out or (not out[d.key].version and d.version)):
                out[d.key] = GAV(group_id=d.group_id, artifact_id=d.artifact_id, version=d.version,
                                 scope=d.scope, type=d.type, classifier=d.classifier)
    return [out[k] for k in sorted(out)]


def _mandatory_keys(cfg: Config) -> dict[str, ArtifactSpec]:
    """필수 사내 아티팩트의 키 + 별칭(aliases) → 스펙. 옛 좌표로 이미 선언된 것도 "이미 있음" 으로 본다."""
    out: dict[str, ArtifactSpec] = {}
    for spec in cfg.artifacts.mandatory:
        out[spec.key] = spec
        for alias in spec.aliases:
            out[alias] = spec
    return out


def _pick(values: list[Optional[str]]) -> Optional[str]:
    """빈 값을 뺀 가장 흔한 값(없으면 None)."""
    vals = [v for v in values if v]
    if not vals:
        return None
    return Counter(vals).most_common(1)[0][0]


def analyze_project(ws: WorkspaceView, cfg: Config) -> ProjectProfile:
    """분석 단계 본체: pom 파싱 → 모듈 순서 → 소스 스캔 → 라이브러리 → 특이 사항 경고 → 케이스 분류
    → 프로필. 프로젝트를 바꾸지 않는다. 비-Maven 이면 ``warnings[0]`` 에 이유를 넣고 그대로 돌려준다
    (오케스트레이터가 ``build_tool`` 을 보고 중단)."""
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
    # MSA: 배포 서비스마다 ServiceInfo, 그 뒤에 어느 서비스에도 속하지 않는 공유 라이브러리 루트 (예:
    # common/)를 붙인다 — 계획이 라이브러리 루트를 서비스보다 먼저 변환하므로 구분 필요(is_library_root)
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
    # 루트에 pom.xml 이 없으면 is_root 인 pom 이 정확히 하나일 때만 루트로 본다(여럿이면 이름 = 폴더명)
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

    # 루트 pom 의 값이 우선, 없으면 모듈들 중 가장 흔한 Java 수준 / 처음 보이는 Boot·Spring 버전
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
    """폴더를 실행 작업공간으로 복사하지 않고 그 자리에서 분석한다(읽기 전용).

    Analyze a directory in place (no copy into a run workspace)."""
    return analyze_project(PathView(path), cfg)
