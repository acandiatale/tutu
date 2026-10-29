"""분석(analyze) 패키지 — pom 그래프·소스 스캔·라이브러리 탐지·케이스 분류로
ProjectProfile(프로필)을 만든다. 흐름상 위치: 접수(intake) 직후 오케스트레이터의 ``step_analyze`` 가
:func:`analyze_project` 를 부르고, 접수 단계도 여기의 :func:`detect_build_tool` (Maven 만 받음)과
``aggregator`` (형제 pom 묶기 / EAR 단독 거부)를 쓴다. 핵심 진입점: ``analyze_project`` / ``analyze_path``
(프로필), ``classify`` (케이스 타입), ``detect_libraries`` (권고 근거), ``scan_properties`` (외부 설정),
``scan_webapp_tree`` (``sbmig inventory``: pom 없는 트리를 설명만 함 — 아무것도 바꾸지 않음), v2.1j 의 ``describe_ant`` /
``synthesize_pom`` (Ant build.xml → pom.xml, 접수가 부름) 과 ``resolve_jar`` (jar → Maven 좌표). 분석은 읽기
전용이다(예외: 접수 단계에서 부르는 aggregator 의 ``write_aggregator`` 와 ``synthesize_pom``).

sbmig.analyze — pom graph, source scan, library detection and case classification → ProjectProfile."""
from .analyzer import PathView, analyze_path, analyze_project
from .ant import AntProject, describe_ant, synthesize_pom
from .build_tool import BuildToolDetection, detect_build_tool, find_ant_build_file
from .classifier import (classify, classify_single, deployable_services, is_library_root, library_roots,
                         library_services)
from .edges import (SystemDep, detect_encoding, normalize_java_version, resolve_system_path, system_dependencies)
from .jar_coords import Sha1Lookup, jar_packages, mark_used, resolve_jar, stem_and_version
from .libraries import LIBRARY_NAMES, LIBRARY_RULES, detect_libraries
from .pom_parser import module_order, parse_poms
from .properties import ExternalizedRules, load_rules, scan_properties
from .source_scanner import ScanResult, scan_source_stats, scan_sources
from .webapp_inventory import guess_jar, jar_names, scan_webapp_tree
from .webxml import parse_web_xml

__all__ = [
    "analyze_project", "analyze_path", "PathView", "BuildToolDetection", "detect_build_tool", "classify", "classify_single", "detect_libraries",
    "LIBRARY_NAMES", "LIBRARY_RULES", "module_order", "parse_poms", "scan_sources", "scan_source_stats",
    "ScanResult", "is_library_root", "library_services", "deployable_services", "library_roots", "SystemDep",
    "detect_encoding", "normalize_java_version", "resolve_system_path", "system_dependencies",
    "scan_properties", "load_rules", "ExternalizedRules", "scan_webapp_tree", "jar_names", "guess_jar",
    "parse_web_xml", "AntProject", "describe_ant", "synthesize_pom", "find_ant_build_file", "resolve_jar",
    "jar_packages", "mark_used", "stem_and_version", "Sha1Lookup",
]
